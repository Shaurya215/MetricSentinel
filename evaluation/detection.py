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

import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from core.logging import get_logger
from ds.catalogue import (
    Catalogue,
    DateRange,
    DetectionConfig,
)
from ds.detection import Detector, detect_range, forecast_period
from ds.forecasting import load_series
from pipelines.faults import (
    Scenario,
    ScenarioWorkspace,
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
    period: DateRange | None = None,
) -> pd.DataFrame:
    """Forecasts for the whole period on clean data, cached by data, detector version and period.

    `period` defaults to the 2018 evaluation period, and then the cache name is the Phase 3 one.
    """
    period = period or cfg.evaluation
    suffix = "" if period == cfg.evaluation else f"_{period.start}_{period.end}"
    cache = cache_dir / f"clean_forecasts_{data_hash[:12]}_{detector.version}{suffix}.parquet"
    if cache.is_file():
        return pd.read_parquet(cache)
    started = time.perf_counter()
    fc = forecast_period(series, period.start, period.end, detector, catalogue, cfg)
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


@dataclass(frozen=True)
class ScenarioExtras:
    """Optional Phase 3b measurements. Without it, `run_scenario` returns exactly the Phase 3 fields."""

    strict_days: int = 3
    alphas: tuple[float, ...] = ()
    clean_by_alpha: dict[float, pd.DataFrame] = field(default_factory=dict)
    clean_flag_keys: set[tuple[str, ...]] = field(default_factory=set)
    share: Callable[[duckdb.DuckDBPyConnection, Scenario], float | None] | None = None


FLAG_COLUMNS = [*KEY, "severity"]
MONITORED_DIMENSIONS = ("customer_state", "category_en", "payment_type")


def flagged_series(anoms: pd.DataFrame) -> pd.DataFrame:
    """One row per flagged series: each anomaly's headline plus every related segment."""
    rows: list[tuple[Any, ...]] = []
    for r in anoms.itertuples():
        rows.append((r.date, r.kpi, r.granularity, r.dimension, r.segment, r.severity))
        for related in json.loads(r.related_segments):
            dimension, segment = related.split("=", 1)
            rows.append((r.date, r.kpi, r.granularity, dimension, segment, r.severity))
    return pd.DataFrame(rows, columns=FLAG_COLUMNS)


def flag_keys(anoms: pd.DataFrame) -> set[tuple[str, ...]]:
    flags = flagged_series(anoms)
    return set(map(tuple, flags[KEY].astype(str).to_numpy()))


def targeted_mask(
    flags: pd.DataFrame, s: Scenario, plan: dict[tuple[str, str, str], str]
) -> np.ndarray:
    """Which flagged series are the scenario's OWN series (strict recall).

    A monitored dimension's own series is the target segment's series, or the dimension's pooled
    `__other__` series when that segment is pooled for the KPI. Faults whose target is not a monitored
    dimension (seller delay, pipeline bug) are targeted at the total series.
    """
    if s.dimension in MONITORED_DIMENSIONS:
        own = (flags["dimension"] == s.dimension) & (flags["segment"] == s.value)
        pooled = (
            (flags["dimension"] == s.dimension)
            & (flags["segment"] == "__other__")
            & np.array(
                [plan.get((k, s.dimension, s.value)) == "pooled" for k in flags["kpi"]], dtype=bool
            )
        )
        return (own | pooled).to_numpy()
    return (flags["dimension"] == "total").to_numpy()


def _with_alpha(cfg: DetectionConfig, alpha: float) -> DetectionConfig:
    return cfg.model_copy(update={"detection": cfg.detection.model_copy(update={"fdr_q": alpha})})


def _extra_fields(
    s: Scenario,
    effect: dict[str, Any],
    anomalies: pd.DataFrame,
    plan: dict[tuple[str, str, str], str],
    share: float | None,
    extras: ScenarioExtras,
    match_end: date,
    fc: pd.DataFrame,
    scn_series: pd.DataFrame,
    days: list[date],
    detector: Detector,
    catalogue: Catalogue,
    cfg: DetectionConfig,
) -> dict[str, Any]:
    from pipelines.dev_benchmark import size_band

    flags = flagged_series(anomalies)
    if len(flags):
        caused = np.array(
            [tuple(k) not in extras.clean_flag_keys for k in flags[KEY].astype(str).to_numpy()],
            dtype=bool,
        )
        window = (flags["date"] >= effect["effect_start"]) & (
            flags["date"] <= effect["effect_start"] + timedelta(days=extras.strict_days)
        )
        strict = flags[
            caused
            & targeted_mask(flags, s, plan)
            & flags["kpi"].isin(s.affected_kpis).to_numpy()
            & window.to_numpy()
        ]
    else:
        strict = flags
    first = strict["date"].min() if len(strict) else None

    curve: dict[str, list[float]] = {}
    for alpha in extras.alphas:
        at_alpha = (
            anomalies
            if math.isclose(alpha, cfg.detection.fdr_q)
            else detect_range(scn_series, fc, days, detector, catalogue, _with_alpha(cfg, alpha))[0]
        )
        clean_keys = set(map(tuple, extras.clean_by_alpha[alpha][KEY].astype(str).to_numpy()))
        caused_a = at_alpha[fault_caused_mask(at_alpha, clean_keys)]
        hits_a = caused_a[
            caused_a["kpi"].isin(s.affected_kpis)
            & (caused_a["date"] >= effect["effect_start"])
            & (caused_a["date"] <= match_end)
        ]
        curve[str(alpha)] = sorted(float(x) for x in hits_a["severity"])

    return {
        "target_share": share,
        "size_band": "total"
        if s.dimension == "data_quality"
        else (None if share is None else size_band(share)),
        "strict_detected": bool(len(strict)),
        "strict_delay_days": (first - effect["effect_start"]).days if first is not None else None,
        "strict_series": sorted(
            {f"{r.kpi}:{r.granularity}:{r.dimension}={r.segment}" for r in strict.itertuples()}
        )[:6],
        "curve_hit_severities": curve,
    }


def run_scenario(
    s: Scenario,
    clean_series: pd.DataFrame,
    clean_fc: pd.DataFrame,
    clean_anoms: pd.DataFrame,
    detector: Detector,
    catalogue: Catalogue,
    cfg: DetectionConfig,
    *,
    warehouse: Path | None = None,
    period: DateRange | None = None,
    extras: ScenarioExtras | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    period = period or cfg.evaluation
    plan: dict[tuple[str, str, str], str] = {}
    share: float | None = None
    with ScenarioWorkspace(warehouse) as ws:
        ws.inject(s)
        ws.build_metrics()
        effect = measure_effect(ws, s)
        scn_series = load_series(ws.con, catalogue)
        if extras is not None:
            plan = {
                (k, d, g): gr
                for k, d, g, gr in ws.con.execute(
                    "SELECT kpi, dimension, segment, granularity FROM metrics.series_plan"
                ).fetchall()
            }
            share = extras.share(ws.con, s) if extras.share else None
    changed = changed_series(clean_series, scn_series)
    start = max(effect["effect_start"], period.start)
    end = min(effect["effect_end"] + timedelta(days=RUN_TAIL_DAYS), period.end)
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
    row: dict[str, Any] = {
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
    if extras is not None:
        row.update(
            _extra_fields(
                s,
                effect,
                anomalies,
                plan,
                share,
                extras,
                match_end,
                fc,
                scn_series,
                days,
                detector,
                catalogue,
                cfg,
            )
        )
    return row


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


def main(argv: list[str] | None = None) -> None:
    """Entry point for `python -m evaluation.detection`: delegates to the protocol runner.

    With no arguments it runs the 2018 protocol on the Phase 3 benchmark, as before, now with
    the extended breakdowns (strict recall, size bands, operating curve, false-alarm causes).
    """
    from evaluation.protocol import main as protocol_main

    protocol_main(argv)


if __name__ == "__main__":
    main()
