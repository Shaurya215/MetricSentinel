"""Detection evaluation (FR-080, AI_ML_PLAN.md section 2.4). One command:

    uv run python -m evaluation.detection          (make eval-detection)

Everything runs inside the EVALUATION period (2018), which the backtest never saw.

1. Clean run. Detection on every day of the evaluation period with no injected fault.
   Every anomaly raised here counts as a false alarm -> false alarms per 30 days.
   (Real incidents in the 2018 data would also land here; they are listed so they can
   be inspected, not hidden.)
2. Scenario runs. For each of the 30 benchmark faults: inject, rebuild the metrics,
   re-forecast only the series the fault changed (the others are identical to the
   clean run, so their clean forecasts are reused exactly), and detect on every day
   from the measured effect start to effect end + 14 days.
3. Scoring.
   * detected: an anomaly on an affected KPI between effect start and effect end + 7
     days, that the clean run did not also raise (it must be caused by the fault)
   * delay: first such anomaly minus effect start, in days
   * precision: fault-caused anomalies on affected KPIs / all fault-caused anomalies
   * recall by measured magnitude band; NFR-021 uses faults with |relative change| >= 20%
     in the targeted segment (the total, when the target is not a monitored dimension)

Results go to data/reports/detection_eval.md|json and to MLflow with the git SHA,
data hash, detector version and scenario-file hash (FR-086).
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from core.logging import configure_logging, get_logger, run_context
from core.settings import get_settings
from ds.catalogue import Catalogue, DetectionConfig, load_catalogue, load_detection_config
from ds.detection import Detector, detect_range, forecast_period
from ds.forecasting import load_series
from pipelines.faults import (
    SCENARIOS_PATH,
    Scenario,
    ScenarioWorkspace,
    load_scenarios,
    measure_effect,
)

log = get_logger(__name__)
MATCH_TAIL_DAYS = 7
RUN_TAIL_DAYS = 14
KEY = ["date", "kpi", "granularity", "dimension", "segment"]


def daterange(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def clean_forecasts(
    series: pd.DataFrame,
    detector: Detector,
    catalogue: Catalogue,
    cfg: DetectionConfig,
    data_hash: str,
    cache_dir: Path,
) -> pd.DataFrame:
    """Forecasts for the whole evaluation period on clean data, cached by data and detector version."""
    cache = cache_dir / f"clean_forecasts_{data_hash[:12]}_{detector.version}.parquet"
    if cache.is_file():
        return pd.read_parquet(cache)
    started = time.perf_counter()
    fc = forecast_period(series, cfg.evaluation.start, cfg.evaluation.end, detector, catalogue, cfg)
    log.info("clean_forecasts_done", rows=len(fc), seconds=round(time.perf_counter() - started, 1))
    cache_dir.mkdir(parents=True, exist_ok=True)
    fc.to_parquet(cache, index=False)
    return fc


def changed_series(clean: pd.DataFrame, scenario: pd.DataFrame) -> set[str]:
    merged = clean[["unique_id", "ds", "y", "n"]].merge(
        scenario[["unique_id", "ds", "y", "n"]],
        on=["unique_id", "ds"],
        how="outer",
        suffixes=("_c", "_s"),
    )
    diff = (merged["n_c"] != merged["n_s"]) | ~np.isclose(
        merged["y_c"].fillna(-1e18), merged["y_s"].fillna(-1e18), rtol=1e-12, atol=1e-9
    )
    return set(merged.loc[diff, "unique_id"])


def fault_caused_mask(anomalies: pd.DataFrame, clean_keys: set[tuple[str, ...]]) -> np.ndarray:
    """True for anomalies the clean run did not also raise. A boolean array, so an empty
    frame still filters rows (an empty Python list would select columns instead)."""
    keys = anomalies[KEY].astype(str).to_numpy()
    return np.array([tuple(k) not in clean_keys for k in keys], dtype=bool)


def run_scenario(
    s: Scenario,
    clean_series: pd.DataFrame,
    clean_fc: pd.DataFrame,
    clean_anoms: pd.DataFrame,
    detector: Detector,
    catalogue: Catalogue,
    cfg: DetectionConfig,
) -> dict[str, Any]:
    started = time.perf_counter()
    with ScenarioWorkspace() as ws:
        ws.inject(s)
        ws.build_metrics()
        effect = measure_effect(ws, s)
        scn_series = load_series(ws.con, catalogue)
    changed = changed_series(clean_series, scn_series)
    start = max(effect["effect_start"], cfg.evaluation.start)
    end = min(effect["effect_end"] + timedelta(days=RUN_TAIL_DAYS), cfg.evaluation.end)
    days = daterange(start, end)
    fc = clean_fc[~clean_fc["unique_id"].isin(changed)]
    if changed:
        fc = pd.concat(
            [fc, forecast_period(scn_series, start, end, detector, catalogue, cfg, only=changed)],
            ignore_index=True,
        )
    anomalies, tests = detect_range(scn_series, fc, days, detector, catalogue, cfg)

    clean_keys = set(map(tuple, clean_anoms[KEY].astype(str).to_numpy()))
    caused = anomalies[fault_caused_mask(anomalies, clean_keys)]
    match_end = effect["effect_end"] + timedelta(days=MATCH_TAIL_DAYS)
    hits = caused[
        caused["kpi"].isin(s.affected_kpis)
        & (caused["date"] >= effect["effect_start"])
        & (caused["date"] <= match_end)
    ]
    first = hits["date"].min() if len(hits) else None
    magnitude = effect["segment_relative_change"]
    if magnitude is None:
        magnitude = effect["total_relative_change"]
    return {
        "id": s.id,
        "fault_type": s.fault_type,
        "dimension": s.dimension,
        "value": s.value,
        "size_tier": s.size_tier,
        "nominal_magnitude": s.magnitude,
        "start": s.start,
        "effect_start": effect["effect_start"],
        "effect_end": effect["effect_end"],
        "segment_relative_change": effect["segment_relative_change"],
        "total_relative_change": effect["total_relative_change"],
        "measured_magnitude": None if magnitude is None else abs(magnitude),
        "detected": bool(len(hits)),
        "delay_days": (first - effect["effect_start"]).days if first is not None else None,
        "true_positive_anomalies": len(hits),
        "caused_anomalies": len(caused),
        "changed_series": len(changed),
        "tests": tests,
        "detected_by": sorted(
            {f"{r.kpi}:{r.granularity}:{r.dimension}={r.segment}" for r in hits.itertuples()}
        )[:6],
        "seconds": round(time.perf_counter() - started, 1),
    }


def band(m: float | None) -> str:
    if m is None:
        return "unknown"
    return "<10%" if m < 0.10 else "10-20%" if m < 0.20 else ">=20%"


def summarise(
    results: pd.DataFrame, clean_anoms: pd.DataFrame, clean_days: int, clean_tests: int
) -> dict[str, Any]:
    results = results.assign(band=results["measured_magnitude"].map(band))
    big = results[results["band"] == ">=20%"]
    tp = results["true_positive_anomalies"].sum()
    caused = results["caused_anomalies"].sum()
    fa_per_30 = len(clean_anoms) / clean_days * 30 if clean_days else float("nan")
    return {
        "scenarios": len(results),
        "recall": float(results["detected"].mean()),
        "recall_magnitude_ge_20pct": float(big["detected"].mean()) if len(big) else None,
        "scenarios_magnitude_ge_20pct": len(big),
        "recall_by_band": results.groupby("band")["detected"]
        .agg(["mean", "count"])
        .round(3)
        .to_dict("index"),
        "recall_by_type": results.groupby("fault_type")["detected"]
        .agg(["mean", "count"])
        .round(3)
        .to_dict("index"),
        "recall_by_size_tier": results.groupby("size_tier")["detected"]
        .agg(["mean", "count"])
        .round(3)
        .to_dict("index"),
        "median_delay_days": float(results["delay_days"].dropna().median())
        if results["detected"].any()
        else None,
        "mean_delay_days": float(results["delay_days"].dropna().mean())
        if results["detected"].any()
        else None,
        "precision_fault_caused": float(tp / caused) if caused else None,
        "clean_days": clean_days,
        "clean_tests": clean_tests,
        "clean_anomalies": len(clean_anoms),
        "false_alarms_per_30_days": fa_per_30,
        "nfr_021": {
            "target": "recall >= 0.80 on faults with relative magnitude >= 20%, <= 2 false alarms per 30 days",
            "recall_met": bool(len(big)) and float(big["detected"].mean()) >= 0.80,
            "false_alarms_met": fa_per_30 <= 2,
        },
    }


def render(
    summary: dict[str, Any], results: pd.DataFrame, clean_anoms: pd.DataFrame, meta: dict[str, str]
) -> str:
    nfr = summary["nfr_021"]

    def pct(x: float | None) -> str:
        return "n/a" if x is None else f"{x:.1%}"

    lines = [
        "# Detection evaluation",
        "",
        f"git `{meta['git_sha']}` · data `{meta['data_hash'][:12]}` · detector `{meta['detector_version']}` · "
        f"scenarios `{meta['scenarios_hash']}`",
        "",
        "## Headline",
        "",
        f"- Recall, all {summary['scenarios']} scenarios: **{pct(summary['recall'])}**",
        f"- Recall, faults with measured magnitude >= 20% ({summary['scenarios_magnitude_ge_20pct']} scenarios): "
        f"**{pct(summary['recall_magnitude_ge_20pct'])}** (target 80%: {'met' if nfr['recall_met'] else 'NOT met'})",
        f"- False alarms on clean data: **{summary['false_alarms_per_30_days']:.2f} per 30 days** "
        f"({summary['clean_anomalies']} in {summary['clean_days']} days; target <= 2: "
        f"{'met' if nfr['false_alarms_met'] else 'NOT met'})",
        f"- Precision of fault-caused anomalies: {pct(summary['precision_fault_caused'])}",
        f"- Detection delay: median {summary['median_delay_days']} days, mean "
        f"{'n/a' if summary['mean_delay_days'] is None else round(summary['mean_delay_days'], 1)} days",
        "",
        "## Recall by measured magnitude",
        "",
        "| band | recall | scenarios |",
        "|---|---|---|",
    ]
    lines += [
        f"| {k} | {v['mean']:.0%} | {v['count']} |"
        for k, v in sorted(summary["recall_by_band"].items())
    ]
    lines += ["", "## Recall by fault type", "", "| type | recall | scenarios |", "|---|---|---|"]
    lines += [
        f"| {k} | {v['mean']:.0%} | {v['count']} |"
        for k, v in sorted(summary["recall_by_type"].items())
    ]
    lines += ["", "## Recall by target size", "", "| tier | recall | scenarios |", "|---|---|---|"]
    lines += [
        f"| {k} | {v['mean']:.0%} | {v['count']} |"
        for k, v in sorted(summary["recall_by_size_tier"].items())
    ]
    lines += [
        "",
        "## Scenarios",
        "",
        "| id | target | magnitude (measured) | detected | delay | detected by |",
        "|---|---|---|---|---|---|",
    ]
    for r in results.itertuples():
        mag = (
            "n/a"
            if r.measured_magnitude is None or pd.isna(r.measured_magnitude)
            else f"{r.measured_magnitude:.1%}"
        )
        lines.append(
            f"| {r.id} | {r.dimension}={r.value} | {mag} | {'yes' if r.detected else 'no'} | "
            f"{'' if pd.isna(r.delay_days) else int(r.delay_days)} | {', '.join(r.detected_by)} |"
        )
    lines += ["", "## Clean-run anomalies (counted as false alarms)", ""]
    if clean_anoms.empty:
        lines.append("None.")
    else:
        lines += [
            "| date | kpi | granularity | headline | observed | expected | q |",
            "|---|---|---|---|---|---|---|",
        ]
        for a in clean_anoms.itertuples():
            lines.append(
                f"| {a.date} | {a.kpi} | {a.granularity} | {a.dimension}={a.segment} | {a.observed:.4g} | "
                f"{a.expected:.4g} | {a.q_value:.2g} |"
            )
    return "\n".join(lines) + "\n"


def main() -> None:
    configure_logging()
    settings = get_settings()
    catalogue, cfg, detector = load_catalogue(), load_detection_config(), Detector.load()
    scenarios = load_scenarios()
    scenarios_hash = hashlib.sha256(SCENARIOS_PATH.read_bytes()).hexdigest()[:12]
    with run_context(component="detection_eval"):
        from evaluation.tracking import git_sha, tracked_run

        con = duckdb.connect(str(settings.duckdb_path), read_only=True)
        try:
            data_hash = con.execute(
                "SELECT value FROM meta.build WHERE key = 'data_hash'"
            ).fetchall()[0][0]
            series = load_series(con, catalogue)
        finally:
            con.close()
        fc = clean_forecasts(
            series, detector, catalogue, cfg, data_hash, settings.reports_dir / "cache"
        )
        clean_days = daterange(cfg.evaluation.start, cfg.evaluation.end)
        clean_anoms, clean_tests = detect_range(series, fc, clean_days, detector, catalogue, cfg)
        log.info("clean_run_done", anomalies=len(clean_anoms), tests=clean_tests)

        rows = []
        for s in scenarios:
            row = run_scenario(s, series, fc, clean_anoms, detector, catalogue, cfg)
            log.info(
                "scenario_done",
                id=s.id,
                detected=row["detected"],
                delay=row["delay_days"],
                seconds=row["seconds"],
            )
            rows.append(row)
        results = pd.DataFrame(rows)
        summary = summarise(results, clean_anoms, len(clean_days), clean_tests)
        meta = {
            "git_sha": git_sha(),
            "data_hash": data_hash,
            "detector_version": detector.version,
            "scenarios_hash": scenarios_hash,
        }

        out = settings.reports_dir
        out.mkdir(parents=True, exist_ok=True)
        (out / "detection_eval.md").write_text(
            render(summary, results, clean_anoms, meta), encoding="utf-8"
        )
        (out / "detection_eval.json").write_text(
            json.dumps(
                {
                    "meta": meta,
                    "summary": summary,
                    "scenarios": rows,
                    "clean_anomalies": clean_anoms.astype(str).to_dict("records"),
                },
                indent=2,
                default=str,
            )
            + "\n",
            encoding="utf-8",
        )

        import mlflow

        with tracked_run(
            "detection_eval",
            "mvp_benchmark",
            {
                "data_hash": data_hash,
                "detector_version": detector.version,
                "scenarios_hash": scenarios_hash,
                "seed": "42",
            },
        ):
            mlflow.log_params(
                {
                    "fdr_q": cfg.detection.fdr_q,
                    "interval_level": detector.level,
                    "scenarios": len(scenarios),
                    "evaluation_start": str(cfg.evaluation.start),
                    "evaluation_end": str(cfg.evaluation.end),
                }
            )
            mlflow.log_metrics(
                {
                    k: float(v)
                    for k, v in summary.items()
                    if isinstance(v, int | float) and not isinstance(v, bool) and v is not None
                }
            )
            mlflow.log_artifact(str(out / "detection_eval.md"))
            mlflow.log_artifact(str(out / "detection_eval.json"))
        print((out / "detection_eval.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
