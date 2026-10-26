"""Rolling-origin forecast backtest and model selection (FR-035, AI_ML_PLAN.md section 2.2).

Uses ONLY the backtest period in config/detection.yaml (2017). The 2018 evaluation
period is never touched here, so the detector is chosen before any fault is seen.

Procedure, per series group (KPI family x granularity):
  1. Re-fit every Monday, forecast the next week, for every candidate model.
  2. Score each forecast point: MASE, sMAPE, raw interval coverage.
  3. Split the backtest in half. On the first half, learn a calibration factor c
     (the 95th percentile of |error| / interval half-width), so intervals reach
     nominal coverage. On the second half, measure calibrated coverage and width.
  4. Choose, per group, the model with the narrowest calibrated interval among
     models whose calibrated coverage on the second half is at least 90%.
     Narrow, well-calibrated intervals are what give the detector power.
  5. Re-learn c for the chosen model on the whole backtest. Write config/detector.json.

AutoARIMA costs about 1.3 s per fit here, so it is only run on the total series and
reported in a separate "totals only" comparison; it is not eligible for selection.

    uv run python -m ds.backtest
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from core.logging import configure_logging, get_logger, run_context
from core.settings import REPO_ROOT, get_settings
from ds.catalogue import (
    Catalogue,
    DetectionConfig,
    load_catalogue,
    load_detection_config,
    share_kpis_for,
    transforms_for,
    with_flags,
)
from ds.forecasting import (
    DAILY_CANDIDATES,
    WEEKLY_CANDIDATES,
    ForecastSpec,
    apply_transforms,
    clean_history,
    forecast_origins,
    group_name,
    load_series,
    mondays,
)

log = get_logger(__name__)
DETECTOR_PATH = REPO_ROOT / "config" / "detector.json"
MIN_HOLDOUT_COVERAGE = 0.90
FAMILY_GROUP = {
    "count": "volume",
    "sum": "volume",
    "ratio": "ratio",
    "rate": "rate",
    "mean": "mean",
}


def group_of(kpi_family: str, granularity: str) -> str:
    return f"{FAMILY_GROUP[kpi_family]}|{granularity}"


def candidate_models(series: pd.DataFrame, include_arima: bool = True) -> dict[str, list[str]]:
    models: dict[str, list[str]] = {}
    for uid, granularity, dimension in (
        series[["unique_id", "granularity", "dimension"]].drop_duplicates().itertuples(index=False)
    ):
        if granularity == "weekly":
            models[uid] = list(WEEKLY_CANDIDATES)
        elif dimension == "total" and include_arima:
            models[uid] = list(DAILY_CANDIDATES)
        else:
            models[uid] = [m for m in DAILY_CANDIDATES if m != "AutoARIMA"]
    return models


def mase_scale(
    series: pd.DataFrame,
    before: pd.Timestamp,
    cfg: DetectionConfig,
    spec: ForecastSpec,
    transforms: dict[str, str] | None = None,
) -> pd.Series:
    """Per-series MASE denominator: in-sample MAE of the seasonal naive forecast before the backtest.

    Series forecast on a log scale (change B) get their denominator on that scale too.
    """
    hist, _ = apply_transforms(clean_history(series[series["ds"] < before], cfg), transforms)
    scales = {}
    for uid, g in hist.groupby("unique_id"):
        m = spec.daily_season if g["granularity"].iloc[0] == "daily" else spec.weekly_season
        diffs = (g["y"] - g["y"].shift(m)).abs().dropna()
        scales[uid] = diffs.mean() if len(diffs) and diffs.mean() > 0 else np.nan
    return pd.Series(scales, name="scale")


def score(
    fc: pd.DataFrame,
    series: pd.DataFrame,
    scales: pd.Series,
    catalogue: Catalogue,
    cfg: DetectionConfig,
    transforms: dict[str, str] | None = None,
    share_kpis: tuple[str, ...] = (),
) -> pd.DataFrame:
    actual = series[["unique_id", "ds", "y", "n", "min_n", "kpi", "dimension", "granularity"]]
    pts = fc.merge(actual, on=["unique_id", "ds"], how="inner")
    pts = pts[(pts["n"] >= pts["min_n"]) & pts["y"].notna()]
    for event in cfg.events:
        week_start = pts["ds"] - pd.to_timedelta(
            np.where(pts["granularity"] == "weekly", 6, 0), unit="D"
        )
        hit = (week_start <= pd.Timestamp(event.end)) & (pts["ds"] >= pd.Timestamp(event.start))
        pts = pts[~hit]
    family = {k.name: k.family for k in catalogue.kpis}
    # Series forecast on a log scale are scored on that scale (error, interval, coverage); the
    # sMAPE is always computed in the original units.
    is_log = (
        pts["model_scale"].eq("log1p")
        if "model_scale" in pts.columns
        else pd.Series(False, index=pts.index)
    )
    y_model = np.where(is_log, np.log1p(pts["y"].clip(lower=0)), pts["y"])
    yhat_orig = np.where(is_log, np.expm1(pts["yhat"]), pts["yhat"])
    pts = pts.assign(
        group=[
            group_name(k, d, family[k], g, transforms, share_kpis)
            for k, d, g in zip(pts["kpi"], pts["dimension"], pts["granularity"], strict=True)
        ],
        err=y_model - pts["yhat"],
        half=(pts["hi"] - pts["lo"]) / 2,
    )
    pts = pts.join(scales, on="unique_id")
    pts["abs_scaled_err"] = pts["err"].abs() / pts["scale"]
    pts["smape"] = (
        2 * np.abs(pts["y"] - yhat_orig) / (pts["y"].abs() + np.abs(yhat_orig)).replace(0, np.nan)
    )
    pts["covered"] = (y_model >= pts["lo"]) & (y_model <= pts["hi"])
    pts["ratio"] = pts["err"].abs() / pts["half"].where(pts["half"] > 0)
    return pts


def calibration_factor(ratios: pd.Series, level: int) -> float:
    r = ratios.replace([np.inf, -np.inf], np.nan).dropna()
    return float(np.quantile(r, level / 100)) if len(r) else 1.0


def summarise(pts: pd.DataFrame, split: pd.Timestamp, level: int) -> pd.DataFrame:
    rows = []
    for (group, model), g in pts.groupby(["group", "model"]):
        first, second = g[g["ds"] < split], g[g["ds"] >= split]
        c = calibration_factor(first["ratio"], level)
        rows.append(
            {
                "group": group,
                "model": model,
                "series": g["unique_id"].nunique(),
                "points": len(g),
                "mase": g["abs_scaled_err"].mean(),
                "smape": g["smape"].mean(),
                "raw_coverage": g["covered"].mean(),
                "calibration_first_half": c,
                "calibrated_coverage_second_half": (
                    second["err"].abs() <= c * second["half"]
                ).mean(),
                "calibrated_scaled_width_second_half": (
                    2 * c * second["half"] / second["scale"]
                ).mean(),
            }
        )
    return pd.DataFrame(rows).sort_values(["group", "model"]).reset_index(drop=True)


def select(summary: pd.DataFrame, pts: pd.DataFrame, level: int) -> dict[str, Any]:
    chosen: dict[str, Any] = {}
    for group, g in summary.groupby("group"):
        ok = g[g["calibrated_coverage_second_half"] >= MIN_HOLDOUT_COVERAGE]
        pick = (
            (ok if len(ok) else g)
            .sort_values(
                ["calibrated_scaled_width_second_half", "mase"]
                if len(ok)
                else ["calibrated_coverage_second_half"],
                ascending=[True, True] if len(ok) else [False],
            )
            .iloc[0]
        )
        model = pick["model"]
        full = pts[(pts["group"] == group) & (pts["model"] == model)]
        chosen[group] = {
            "model": model,
            "calibration": round(calibration_factor(full["ratio"], level), 4),
            "met_coverage_floor": bool(len(ok)),
            "mase": round(float(pick["mase"]), 4),
            "calibrated_coverage_second_half": round(
                float(pick["calibrated_coverage_second_half"]), 4
            ),
        }
    return chosen


def fit_skewness(series: pd.DataFrame, start: Any, end: Any) -> dict[str, dict[str, float]]:
    """Skewness of every KPI's daily total in the fit period (reported for change B; GMV is the only one transformed)."""
    from scipy.stats import skew

    window = series[
        (series["dimension"] == "total")
        & (series["granularity"] == "daily")
        & (series["ds"] >= pd.Timestamp(start))
        & (series["ds"] <= pd.Timestamp(end))
    ]
    out: dict[str, dict[str, float]] = {}
    for kpi, g in window.groupby("kpi"):
        values = g["y"].dropna().to_numpy(dtype=float)
        if len(values) > 2:
            out[kpi] = {
                "skewness": float(skew(values)),
                "skewness_log1p": float(skew(np.log1p(np.clip(values, 0, None)))),
            }
    return out


def run_backtest(
    con: duckdb.DuckDBPyConnection,
    *,
    catalogue: Catalogue | None = None,
    cfg: DetectionConfig | None = None,
    include_arima: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    catalogue = catalogue or load_catalogue()
    cfg = cfg or load_detection_config()
    spec = ForecastSpec(
        cfg.forecast.interval_level,
        cfg.forecast.daily_season_length,
        cfg.forecast.weekly_season_length,
    )
    transforms, share = transforms_for(cfg), share_kpis_for(cfg)
    series = load_series(con, catalogue, share)
    series = series[series["ds"] <= pd.Timestamp(cfg.backtest.end)]
    origins = [
        o
        for o in mondays(cfg.backtest.start, cfg.backtest.end)
        if o + pd.Timedelta(days=6).to_pytimedelta() >= cfg.backtest.start
    ]
    started = time.perf_counter()
    fc = forecast_origins(
        series, origins, candidate_models(series, include_arima), spec, cfg, transforms or None
    )
    fc = fc[
        (fc["ds"] >= pd.Timestamp(cfg.backtest.start))
        & (fc["ds"] <= pd.Timestamp(cfg.backtest.end))
    ]
    log.info(
        "backtest_forecasts_done", rows=len(fc), seconds=round(time.perf_counter() - started, 1)
    )
    scales = mase_scale(series, pd.Timestamp(cfg.backtest.start), cfg, spec, transforms or None)
    pts = score(fc, series, scales, catalogue, cfg, transforms, share)
    split = (
        pd.Timestamp(cfg.backtest.start)
        + (pd.Timestamp(cfg.backtest.end) - pd.Timestamp(cfg.backtest.start)) / 2
    )
    split = split.normalize()
    selectable = pts[pts["model"] != "AutoARIMA"]
    summary = summarise(selectable, split, spec.level)
    totals = summarise(pts[pts["dimension"] == "total"], split, spec.level)
    chosen = select(summary, selectable, spec.level)
    detector = {
        "version": 1,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "interval_level": spec.level,
        "backtest": {
            "start": str(cfg.backtest.start),
            "end": str(cfg.backtest.end),
            "split": str(split.date()),
        },
        "selection_rule": f"narrowest calibrated interval among models with >= {MIN_HOLDOUT_COVERAGE:.0%} calibrated coverage on the second half",
        "groups": chosen,
        "transforms": transforms,
        "share_kpis": list(share),
        "flags": [n for n, on in cfg.improvements.model_dump().items() if on],
        "skewness_fit": fit_skewness(series, cfg.backtest.start, cfg.backtest.end),
    }
    return summary, totals, detector


def render(
    summary: pd.DataFrame, totals: pd.DataFrame, detector: dict[str, Any], data_hash: str
) -> str:
    def table(df: pd.DataFrame) -> str:
        cols = [
            "group",
            "model",
            "series",
            "points",
            "mase",
            "smape",
            "raw_coverage",
            "calibrated_coverage_second_half",
            "calibrated_scaled_width_second_half",
        ]
        head = "| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n"
        body = ""
        for r in df[cols].itertuples(index=False):
            body += (
                "| "
                + " | ".join(f"{v:.3f}" if isinstance(v, float) else str(v) for v in r)
                + " |\n"
            )
        return head + body

    chosen = "\n".join(
        f"- **{g}**: {v['model']} (calibration x{v['calibration']}, MASE {v['mase']}, "
        f"calibrated coverage {v['calibrated_coverage_second_half']:.1%})"
        + ("" if v["met_coverage_floor"] else " - did NOT reach the 90% coverage floor")
        for g, v in detector["groups"].items()
    )
    return (
        f"# Forecast backtest\n\nData hash `{data_hash}`. Backtest {detector['backtest']['start']} to "
        f"{detector['backtest']['end']}, split at {detector['backtest']['split']}. Rule: {detector['selection_rule']}.\n\n"
        f"## Chosen models\n\n{chosen}\n\n## All series (selectable models)\n\n{table(summary)}\n"
        f"## Total series only (includes AutoARIMA, not selectable)\n\n{table(totals)}"
    )


def main(argv: list[str] | None = None) -> None:
    """Default (no arguments): the Phase 3 backtest, exactly as before.

    `--dev NAME` runs the Phase 3b development backtest: model choice and calibration use only
    the development FIT period (config/detection.yaml `development.fit`), the result is written
    to config/detector_dev2017_NAME.json (config/detector.json is never touched), and AutoARIMA is
    skipped because it is report-only and never selectable.
    """
    parser = argparse.ArgumentParser(description="Rolling-origin forecast backtest.")
    parser.add_argument(
        "--dev", metavar="NAME", help="development backtest on the 2017 fit period only"
    )
    parser.add_argument("--flags", default="", help="comma-separated 3b flags to switch on")
    args = parser.parse_args(argv)
    configure_logging()
    settings = get_settings()
    cfg = load_detection_config()
    flag_names = [f for f in args.flags.split(",") if f]
    if flag_names:
        cfg = with_flags(cfg, flag_names)
    detector_path, suffix, include_arima = DETECTOR_PATH, "", True
    if args.dev:
        if cfg.development is None:
            raise SystemExit("config/detection.yaml has no development protocol")
        cfg = cfg.model_copy(update={"backtest": cfg.development.fit})
        detector_path = REPO_ROOT / "config" / f"detector_dev2017_{args.dev}.json"
        suffix, include_arima = f"_dev2017_{args.dev}", False
    with run_context(component="backtest"):
        con = duckdb.connect(str(settings.duckdb_path), read_only=True)
        try:
            from evaluation.tracking import tracked_run, warehouse_data_hash

            data_hash = warehouse_data_hash(con)
            summary, totals, detector = run_backtest(con, cfg=cfg, include_arima=include_arima)
        finally:
            con.close()
        detector["data_hash"] = data_hash
        detector_path.write_text(json.dumps(detector, indent=2) + "\n", encoding="utf-8")
        report = render(summary, totals, detector, data_hash)
        out = settings.reports_dir / f"backtest{suffix}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report, encoding="utf-8")
        summary.to_csv(settings.reports_dir / f"backtest_summary{suffix}.csv", index=False)

        import mlflow

        with tracked_run(
            "forecast_backtest",
            f"backtest{suffix}" if suffix else "backtest",
            {
                "data_hash": data_hash,
                "seed": "none (deterministic)",
                "protocol": args.dev or "phase3",
            },
        ):
            mlflow.log_params(
                {
                    "interval_level": detector["interval_level"],
                    "backtest_start": detector["backtest"]["start"],
                    "backtest_end": detector["backtest"]["end"],
                    "split": detector["backtest"]["split"],
                }
            )
            for r in pd.concat(
                [summary, totals.assign(group=totals["group"] + "|totals")]
            ).itertuples(index=False):
                key = f"{r.group}.{r.model}".replace("|", ".")
                mlflow.log_metrics(
                    {
                        f"{key}.mase": float(r.mase),
                        f"{key}.raw_coverage": float(r.raw_coverage),
                        f"{key}.cal_coverage": float(r.calibrated_coverage_second_half),
                        f"{key}.cal_width": float(r.calibrated_scaled_width_second_half),
                    }
                )
            mlflow.log_artifact(str(detector_path))
            mlflow.log_artifact(str(out))
        print(report)


if __name__ == "__main__":
    main()
