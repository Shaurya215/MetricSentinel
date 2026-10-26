"""Protocol-driven detection evaluation (Phase 3b, ADR-phase3b.md).

    uv run python -m evaluation.detection --protocol eval2018    (default; `make eval-detection`)
    uv run python -m evaluation.detection --protocol dev2017 --detector config/detector_dev2017_baseline.json

Two protocols share one harness:

* eval2018: the 2018 evaluation period and the 30-scenario Phase 3 benchmark. Every report is
  labelled "post-diagnosis, not held-out", because the 2018 false alarms were diagnosed by hand.
* dev2017: the 2017 test period (2017-08-28 to 2017-12-31) and the 54-scenario development
  benchmark. Black Friday week is excluded from the clean days for every configuration.

Recall is reported two ways, next to each other:

* LENIENT (the Phase 3 definition): a fault-caused alert on an affected KPI, on ANY monitored
  series, dated from the first day the fault measurably changed the total to 7 days after it ends.
* STRICT: a fault-caused alert on an affected KPI, on the targeted segment's OWN series (the
  total, for faults not aimed at a monitored dimension), within 3 days of the first affected day.

NFR-021 is judged on the LENIENT figure, because that is how the baseline was measured and a
comparison must be like for like. The strict figure is the more demanding reading and is shown
beside it; if it were the criterion, the target would be missed by more.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from core.logging import configure_logging, get_logger, run_context
from core.settings import REPO_ROOT, get_settings
from ds.calendar import calendar_windows
from ds.catalogue import (
    Catalogue,
    DateRange,
    DetectionConfig,
    load_catalogue,
    load_detection_config,
    with_flags,
)
from ds.detection import Detector, detect_range
from ds.feed_guard import is_guard_alert, last_reliable_day
from ds.forecasting import load_series
from evaluation.breakdown import (
    CAUSES,
    ClassifyContext,
    breakdown,
    classify,
    fraction,
    load_known_events,
)
from evaluation.detection import (
    MONITORED_DIMENSIONS,
    ScenarioExtras,
    _with_alpha,
    band,
    clean_forecasts,
    daterange,
    flag_keys,
    run_scenario,
    summarise,
)
from pipelines.faults import SCENARIOS_PATH, Scenario, load_scenarios

log = get_logger(__name__)
ALPHAS = (0.01, 0.02, 0.05, 0.10, 0.20)
SEVERITIES = (0.0, 0.5, 1.0, 2.0)
CUMULATIVE_DAYS = (0, 1, 3, 7, 14)
STRICT_DAYS = 3
SIZE_ORDER = ("large", "medium", "small", "total")
LABEL_2018 = "post-diagnosis, not held-out"


@dataclass(frozen=True)
class Protocol:
    name: str
    label: str
    period: DateRange
    scenarios_path: Path
    detector_path: Path
    out_name: str


def protocols(cfg: DetectionConfig) -> dict[str, Protocol]:
    found = {
        "eval2018": Protocol(
            name="eval2018",
            label=f"2018 evaluation period - {LABEL_2018}",
            period=cfg.evaluation,
            scenarios_path=SCENARIOS_PATH,
            detector_path=REPO_ROOT / "config" / "detector.json",
            out_name="detection_eval",
        )
    }
    if cfg.development is not None:
        found["dev2017"] = Protocol(
            name="dev2017",
            label="2017 development test period (fit period 2017-04-26 to 2017-08-27; Black Friday week excluded)",
            period=cfg.development.test,
            scenarios_path=REPO_ROOT / "config" / "scenarios" / "dev2017.yaml",
            detector_path=REPO_ROOT / "config" / "detector_dev2017_baseline.json",
            out_name="dev2017_baseline",
        )
    return found


def target_share(con: duckdb.DuckDBPyConnection, s: Scenario, period: DateRange) -> float | None:
    """The target's share of all orders in the period, from CLEAN data (the attached warehouse)."""
    if s.dimension == "data_quality":
        return 1.0
    total = con.execute(
        "SELECT count(*) FROM marts.fct_orders WHERE purchase_date BETWEEN ? AND ?",
        [period.start, period.end],
    ).fetchall()[0][0]
    if not total:
        return None
    if s.dimension in MONITORED_DIMENSIONS:
        n = con.execute(
            f"SELECT count(*) FROM wh.metrics.order_dims d JOIN marts.fct_orders o USING (order_id) "
            f"WHERE o.purchase_date BETWEEN ? AND ? AND d.{s.dimension} = ?",
            [period.start, period.end, s.value],
        ).fetchall()[0][0]
    elif s.dimension == "seller_state":
        n = con.execute(
            """
            WITH prim AS (
                SELECT order_id, seller_id,
                       row_number() OVER (PARTITION BY order_id ORDER BY price DESC, item_seq) AS rn
                FROM marts.fct_order_items
            )
            SELECT count(*) FROM prim p
            JOIN marts.dim_sellers s USING (seller_id)
            JOIN marts.fct_orders o USING (order_id)
            WHERE p.rn = 1 AND o.purchase_date BETWEEN ? AND ? AND s.state = ?
            """,
            [period.start, period.end, s.value],
        ).fetchall()[0][0]
    else:
        return None
    return n / total


@dataclass
class ProtocolResult:
    protocol: Protocol
    rows: list[dict[str, Any]]
    results: pd.DataFrame
    clean_anoms: pd.DataFrame
    clean_tests: int
    clean_days: list[date]
    summary: dict[str, Any]
    causes: list[str]
    meta: dict[str, str]


def run_protocol(
    proto: Protocol,
    *,
    warehouse: Path,
    detector: Detector,
    scenarios: list[Scenario],
    cache_dir: Path,
    catalogue: Catalogue | None = None,
    cfg: DetectionConfig | None = None,
    scenarios_hash: str = "",
) -> ProtocolResult:
    catalogue = catalogue or load_catalogue()
    cfg = cfg or load_detection_config()
    con = duckdb.connect(str(warehouse), read_only=True)
    try:
        data_hash = con.execute("SELECT value FROM meta.build WHERE key = 'data_hash'").fetchall()[
            0
        ][0]
        series = load_series(con, catalogue)
    finally:
        con.close()

    fc = clean_forecasts(series, detector, catalogue, cfg, data_hash, cache_dir, proto.period)
    clean_days = [
        d for d in daterange(proto.period.start, proto.period.end) if cfg.event_on(d) is None
    ]
    clean_by_alpha: dict[float, pd.DataFrame] = {}
    clean_tests = 0
    guard_diag: dict[str, Any] = {}
    for alpha in ALPHAS:
        diag: dict[str, Any] = {}
        frame, tests = detect_range(
            series, fc, clean_days, detector, catalogue, _with_alpha(cfg, alpha), diagnostics=diag
        )
        clean_by_alpha[alpha] = frame
        if abs(alpha - cfg.detection.fdr_q) < 1e-12:
            clean_tests = tests
            guard_diag = diag
    base_alpha = min(ALPHAS, key=lambda a: abs(a - cfg.detection.fdr_q))
    clean_anoms = clean_by_alpha[base_alpha]
    # Days past the last reliable day are silently suppressed: not clean TESTED days (ADR section 15).
    tested_clean_days = len(clean_days) - len(guard_diag.get("guard_past_data_end", []))
    log.info(
        "clean_run_done",
        protocol=proto.name,
        anomalies=len(clean_anoms),
        tests=clean_tests,
        days=len(clean_days),
    )

    totals = series[series["unique_id"] == "orders|total|all|daily"].set_index("ds")["y"]
    ctx = ClassifyContext(
        last_reliable=last_reliable_day(totals),
        windows=calendar_windows(proto.period.start.year, proto.period.end.year),
        events=load_known_events(),
    )
    extras = ScenarioExtras(
        strict_days=STRICT_DAYS,
        alphas=ALPHAS,
        clean_by_alpha=clean_by_alpha,
        clean_flag_keys=flag_keys(clean_anoms),
        share=lambda con_, s: target_share(con_, s, proto.period),
    )
    rows: list[dict[str, Any]] = []
    for s in scenarios:
        row = run_scenario(
            s,
            series,
            fc,
            clean_anoms,
            detector,
            catalogue,
            cfg,
            warehouse=warehouse,
            period=proto.period,
            extras=extras,
        )
        log.info(
            "scenario_done",
            id=s.id,
            detected=row["detected"],
            strict=row["strict_detected"],
            delay=row["delay_days"],
            seconds=row["seconds"],
        )
        rows.append(row)
    results = pd.DataFrame(rows)
    summary = summarise(results, clean_anoms, tested_clean_days, clean_tests)
    summary["extended"] = extended_summary(
        results,
        clean_anoms,
        tested_clean_days,
        clean_by_alpha,
        ctx,
        base_alpha,
        guard_info={
            "enabled": cfg.improvements.feed_guard,
            "diagnostics": guard_diag,
            "clean_days_total": len(clean_days),
        },
    )
    causes = [classify(r.date, r.kpi, r.direction, ctx) for r in clean_anoms.itertuples()]
    from evaluation.tracking import git_sha

    meta = {
        "git_sha": git_sha(),
        "data_hash": data_hash,
        "detector_version": detector.version,
        "scenarios_hash": scenarios_hash,
        "protocol": proto.name,
        "label": proto.label,
        "period": f"{proto.period.start} to {proto.period.end}",
        "last_reliable_day": str(ctx.last_reliable),
        "flags": ",".join(n for n, on in cfg.improvements.model_dump().items() if on) or "none",
    }
    return ProtocolResult(
        proto, rows, results, clean_anoms, clean_tests, clean_days, summary, causes, meta
    )


def _mask_ge20(results: pd.DataFrame) -> pd.Series:
    return results["measured_magnitude"].map(band) == ">=20%"


def _recall(results: pd.DataFrame, column: str, mask: pd.Series | None = None) -> dict[str, Any]:
    subset = results if mask is None else results[mask]
    return fraction(int(subset[column].sum()), len(subset))


def _within(
    results: pd.DataFrame, delay_column: str, days: int, mask: pd.Series | None
) -> dict[str, Any]:
    subset = results if mask is None else results[mask]
    hit = subset[delay_column].notna() & (subset[delay_column] <= days)
    return fraction(int(hit.sum()), len(subset))


def extended_summary(
    results: pd.DataFrame,
    clean_anoms: pd.DataFrame,
    clean_days_n: int,
    clean_by_alpha: dict[float, pd.DataFrame],
    ctx: ClassifyContext,
    base_alpha: float,
    guard_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ge20 = _mask_ge20(results)
    guard_info = guard_info or {
        "enabled": False,
        "diagnostics": {},
        "clean_days_total": clean_days_n,
    }
    diag = guard_info["diagnostics"]
    guard_clean = clean_anoms[is_guard_alert(clean_anoms)] if len(clean_anoms) else clean_anoms
    per30 = (lambda n: n / clean_days_n * 30) if clean_days_n else (lambda n: float("nan"))
    lenient_ge20, strict_ge20 = (
        _recall(results, "detected", ge20),
        _recall(results, "strict_detected", ge20),
    )
    fa = breakdown(clean_anoms, ctx)
    curve = []
    for alpha, frame in clean_by_alpha.items():
        for sev in SEVERITIES:
            hit = [
                bool(len(v) and max(v) >= sev)
                for v in (r[str(alpha)] for r in results["curve_hit_severities"])
            ]
            hit_series = pd.Series(hit, index=results.index)
            n_alarms = int((frame["severity"] >= sev).sum()) if len(frame) else 0
            curve.append(
                {
                    "alpha": alpha,
                    "severity_min": sev,
                    "clean_alarms": n_alarms,
                    "false_alarms_per_30_days": per30(n_alarms),
                    "recall_ge20": fraction(int(hit_series[ge20].sum()), int(ge20.sum())),
                    "recall_all": fraction(int(hit_series.sum()), len(results)),
                }
            )
    return {
        "recall_definitions": {
            "lenient": "fault-caused alert on an affected KPI, any monitored series, from the first affected day to 7 days after the fault ends (the Phase 3 definition)",
            "strict": f"fault-caused alert on an affected KPI, on the targeted segment's own series (total for faults not aimed at a monitored dimension), within {STRICT_DAYS} days of the first affected day",
        },
        "lenient": {"all": _recall(results, "detected"), "ge20": lenient_ge20},
        "strict": {"all": _recall(results, "strict_detected"), "ge20": strict_ge20},
        "nfr_021": {
            "criterion_used": "lenient",
            "why": "the baseline and the README were measured with the lenient definition; a like-for-like comparison needs the same one. Strict is shown beside it as the more demanding reading.",
            "target_recall": 0.80,
            "lenient_ge20_rate": lenient_ge20["rate"],
            "strict_ge20_rate": strict_ge20["rate"],
            "lenient_met": bool(lenient_ge20["rate"] is not None and lenient_ge20["rate"] >= 0.80),
            "strict_met": bool(strict_ge20["rate"] is not None and strict_ge20["rate"] >= 0.80),
            "false_alarms_per_30_days": per30(len(clean_anoms)),
            "false_alarms_met": per30(len(clean_anoms)) <= 2,
        },
        "by_size_band": {
            b: {
                "lenient_all": _recall(results, "detected", results["size_band"] == b),
                "strict_all": _recall(results, "strict_detected", results["size_band"] == b),
                "lenient_ge20": _recall(results, "detected", ge20 & (results["size_band"] == b)),
                "strict_ge20": _recall(
                    results, "strict_detected", ge20 & (results["size_band"] == b)
                ),
            }
            for b in SIZE_ORDER
            if (results["size_band"] == b).any()
        },
        "by_measured_magnitude": {
            m: {
                "lenient": _recall(
                    results, "detected", results["measured_magnitude"].map(band) == m
                ),
                "strict": _recall(
                    results, "strict_detected", results["measured_magnitude"].map(band) == m
                ),
            }
            for m in ("<10%", "10-20%", ">=20%")
        },
        "detected_within_days": {
            str(d): {
                "lenient_all": _within(results, "delay_days", d, None),
                "lenient_ge20": _within(results, "delay_days", d, ge20),
                "strict_all": _within(results, "strict_delay_days", d, None),
                "strict_ge20": _within(results, "strict_delay_days", d, ge20),
            }
            for d in CUMULATIVE_DAYS
        },
        "false_alarms": {
            **fa,
            "clean_days": clean_days_n,
            "per_30_days": per30(fa["total"]),
            "alarm_days_per_30_days": per30(fa["alarm_days"]),
            "guard_alerts": len(guard_clean),
            "alerts_excluding_guard": int(fa["total"] - len(guard_clean)),
        },
        "guard": {
            "enabled": bool(guard_info["enabled"]),
            "clean_alerts": len(guard_clean),
            "clean_days_fired": [str(d) for d in guard_clean["date"]] if len(guard_clean) else [],
            "silently_suppressed_days": [str(d) for d in diag.get("guard_past_data_end", [])],
            "clean_days_total": guard_info["clean_days_total"],
            "scenarios_hit_by_guard_alert": int(results["guard_alert_hit"].sum())
            if "guard_alert_hit" in results
            else 0,
            "lenient_ex_guard": {
                "all": _recall(results, "detected_ex_guard"),
                "ge20": _recall(results, "detected_ex_guard", ge20),
            },
            "strict_ex_guard": {
                "all": _recall(results, "strict_detected_ex_guard"),
                "ge20": _recall(results, "strict_detected_ex_guard", ge20),
            },
        },
        "operating_curve": curve,
    }


def _fmt(f: dict[str, Any]) -> str:
    if f["n"] == 0:
        return "n/a"
    return f"{f['rate']:.1%} ({f['k']}/{f['n']}; 95% CI {f['ci_low']:.0%}-{f['ci_high']:.0%})"


def render_protocol(r: ProtocolResult) -> str:
    x = r.summary["extended"]
    fa, nfr = x["false_alarms"], x["nfr_021"]
    lines = [
        f"# Detection evaluation - {r.protocol.name}",
        "",
        f"**{r.meta['label']}**",
        "",
        f"git `{r.meta['git_sha']}` · data `{r.meta['data_hash'][:12]}` · detector `{r.meta['detector_version']}` · "
        f"scenarios `{r.meta['scenarios_hash']}` · period {r.meta['period']} · last reliable day {r.meta['last_reliable_day']} · "
        f"flags: {r.meta['flags']}",
        "",
        "## Headline",
        "",
        "| | Lenient (NFR-021 comparator) | Strict |",
        "|---|---|---|",
        f"| Recall, faults with measured magnitude >= 20% | **{_fmt(x['lenient']['ge20'])}** | {_fmt(x['strict']['ge20'])} |",
        f"| Recall, all scenarios | {_fmt(x['lenient']['all'])} | {_fmt(x['strict']['all'])} |",
        "",
        f"- Lenient: {x['recall_definitions']['lenient']}.",
        f"- Strict: {x['recall_definitions']['strict']}.",
        f"- **NFR-021 is judged on the lenient figure**: {nfr['why']}",
        f"- NFR-021 recall target 80%: lenient **{'met' if nfr['lenient_met'] else 'NOT met'}**, strict {'met' if nfr['strict_met'] else 'NOT met'}.",
        f"- False alarms on clean days: **{fa['per_30_days']:.2f} per 30 days** ({fa['total']} alarms on {fa['alarm_days']} "
        f"alarm-days in {fa['clean_days']} days; target <= 2: {'met' if nfr['false_alarms_met'] else 'NOT met'}).",
        f"- Precision of fault-caused alerts: {r.summary['precision_fault_caused'] if r.summary['precision_fault_caused'] is None else format(r.summary['precision_fault_caused'], '.1%')}",
        f"- Guard alerts on clean days: {fa['guard_alerts']} (included in the figure above; "
        f"{fa['alerts_excluding_guard']} alerts without them)",
        f"- Alarm-days per 30 days: {fa['alarm_days_per_30_days']:.2f}",
        "",
        "## False alarms by cause",
        "",
        "| cause | alarms |",
        "|---|---|",
    ]
    lines += [f"| {c} | {fa['exclusive'][c]} |" for c in CAUSES]
    lines += [
        f"| *matches an unlabelled real event (non-exclusive, never reclassified)* | {fa['matches_unlabelled_real_event']} |",
        "",
        "## Recall by target size (share of orders: large >= 5%, medium 1-5%, small < 1%)",
        "",
        "| size | lenient, all | strict, all | lenient, >= 20% | strict, >= 20% |",
        "|---|---|---|---|---|",
    ]
    for b, v in x["by_size_band"].items():
        lines.append(
            f"| {b} | {_fmt(v['lenient_all'])} | {_fmt(v['strict_all'])} | {_fmt(v['lenient_ge20'])} | {_fmt(v['strict_ge20'])} |"
        )
    g = x["guard"]
    lines += ["", "## Guard (change A)", ""]
    if g["enabled"]:
        fired = ", ".join(g["clean_days_fired"]) or "none"
        suppressed = g["silently_suppressed_days"]
        lines += [
            f"- Guard alerts on clean days: **{g['clean_alerts']}** on: {fired}. Each counts as a false alarm.",
            f"- Days silently suppressed (past the last reliable day): {len(suppressed)}"
            + (
                f" ({suppressed[0]} to {suppressed[-1]}); not counted as clean tested days."
                if suppressed
                else "."
            ),
            f"- Scenarios where a guard alert was part of the detection: {g['scenarios_hit_by_guard_alert']}",
            "",
            "| recall | with guard alerts | without guard alerts |",
            "|---|---|---|",
            f"| lenient, >= 20% | {_fmt(x['lenient']['ge20'])} | {_fmt(g['lenient_ex_guard']['ge20'])} |",
            f"| lenient, all | {_fmt(x['lenient']['all'])} | {_fmt(g['lenient_ex_guard']['all'])} |",
            f"| strict, >= 20% | {_fmt(x['strict']['ge20'])} | {_fmt(g['strict_ex_guard']['ge20'])} |",
            f"| strict, all | {_fmt(x['strict']['all'])} | {_fmt(g['strict_ex_guard']['all'])} |",
        ]
    else:
        lines.append("Not enabled for this run.")
    lines += [
        "",
        "## Recall by measured magnitude",
        "",
        "| band | lenient | strict |",
        "|---|---|---|",
    ]
    lines += [
        f"| {m} | {_fmt(v['lenient'])} | {_fmt(v['strict'])} |"
        for m, v in x["by_measured_magnitude"].items()
    ]
    lines += [
        "",
        "## Detected within N days of the first affected day",
        "",
        "| N | lenient, all | lenient, >= 20% | strict, all | strict, >= 20% |",
        "|---|---|---|---|---|",
    ]
    for d, v in x["detected_within_days"].items():
        lines.append(
            f"| {d} | {_fmt(v['lenient_all'])} | {_fmt(v['lenient_ge20'])} | {_fmt(v['strict_all'])} | {_fmt(v['strict_ge20'])} |"
        )
    lines += [
        "",
        "## Operating curve (recall at >= 20% vs false alarms)",
        "",
        "| FDR level | min severity | clean alarms | false alarms / 30 days | recall >= 20% (lenient) |",
        "|---|---|---|---|---|",
    ]
    for c in x["operating_curve"]:
        lines.append(
            f"| {c['alpha']} | {c['severity_min']} | {c['clean_alarms']} | {c['false_alarms_per_30_days']:.2f} | {_fmt(c['recall_ge20'])} |"
        )
    lines += [
        "",
        "## Scenarios",
        "",
        "| id | target | share | band | measured | lenient | delay | strict | strict delay |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in r.results.itertuples():
        mag = "n/a" if pd.isna(row.measured_magnitude) else f"{row.measured_magnitude:.1%}"
        share = (
            "n/a"
            if row.target_share is None or pd.isna(row.target_share)
            else f"{row.target_share:.1%}"
        )
        lines.append(
            f"| {row.id} | {row.dimension}={row.value} | {share} | {row.size_band} | {mag} | {'yes' if row.detected else 'no'} | "
            f"{'' if pd.isna(row.delay_days) else int(row.delay_days)} | {'yes' if row.strict_detected else 'no'} | "
            f"{'' if pd.isna(row.strict_delay_days) else int(row.strict_delay_days)} |"
        )
    lines += ["", "## Clean-run alarms (all counted as false alarms)", ""]
    if r.clean_anoms.empty:
        lines.append("None.")
    else:
        lines += [
            "| date | kpi | granularity | headline | observed | expected | q | severity | cause |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for a, cause in zip(r.clean_anoms.itertuples(), r.causes, strict=True):
            lines.append(
                f"| {a.date} | {a.kpi} | {a.granularity} | {a.dimension}={a.segment} | {a.observed:.4g} | "
                f"{a.expected:.4g} | {a.q_value:.2g} | {a.severity:.2f} | {cause} |"
            )
    return "\n".join(lines) + "\n"


def write_outputs(r: ProtocolResult, out_dir: Path, name: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    md, js = out_dir / f"{name}.md", out_dir / f"{name}.json"
    md.write_text(render_protocol(r), encoding="utf-8")
    js.write_text(
        json.dumps(
            {
                "meta": r.meta,
                "summary": r.summary,
                "scenarios": r.rows,
                "clean_anomalies": r.clean_anoms.astype(str).to_dict("records"),
                "clean_causes": r.causes,
            },
            indent=2,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )
    return md, js


def main(argv: list[str] | None = None) -> None:
    cfg = load_detection_config()
    available = protocols(cfg)
    parser = argparse.ArgumentParser(description="Detection evaluation (Phase 3 / 3b).")
    parser.add_argument("--protocol", choices=sorted(available), default="eval2018")
    parser.add_argument("--detector", type=Path, help="detector json (default: the protocol's)")
    parser.add_argument("--scenarios", type=Path, help="scenario yaml (default: the protocol's)")
    parser.add_argument("--name", help="output name under data/reports (default: the protocol's)")
    parser.add_argument(
        "--flags",
        default="",
        help="comma-separated 3b flags to switch on (feed_guard, log_gmv, share_test, calendar_downgrade)",
    )
    args = parser.parse_args(argv)
    flag_names = [f for f in args.flags.split(",") if f]
    if flag_names:
        cfg = with_flags(cfg, flag_names)
    proto = available[args.protocol]
    detector_path = args.detector or proto.detector_path
    scenarios_path = args.scenarios or proto.scenarios_path
    name = args.name or proto.out_name

    configure_logging()
    settings = get_settings()
    detector = Detector.load(detector_path)
    scenarios = load_scenarios(scenarios_path)
    scenarios_hash = hashlib.sha256(scenarios_path.read_bytes()).hexdigest()[:12]
    with run_context(component="detection_eval", protocol=proto.name):
        from evaluation.tracking import tracked_run

        result = run_protocol(
            proto,
            warehouse=settings.duckdb_path,
            detector=detector,
            scenarios=scenarios,
            cache_dir=settings.reports_dir / "cache",
            cfg=cfg,
            scenarios_hash=scenarios_hash,
        )
        md, js = write_outputs(result, settings.reports_dir, name)

        import mlflow

        ext = result.summary["extended"]
        with tracked_run(
            "detection_eval",
            name,
            {
                "data_hash": result.meta["data_hash"],
                "detector_version": detector.version,
                "scenarios_hash": scenarios_hash,
                "protocol": proto.name,
                "label": proto.label,
                "detector_file": detector_path.name,
                "flags": ",".join(flag_names) or "none",
            },
        ):
            mlflow.log_params(
                {
                    "fdr_q": cfg.detection.fdr_q,
                    "interval_level": detector.level,
                    "scenarios": len(scenarios),
                    "period": result.meta["period"],
                }
            )
            numeric = {
                k: float(v)
                for k, v in result.summary.items()
                if isinstance(v, int | float) and not isinstance(v, bool) and v is not None
            }
            numeric.update(
                {
                    "lenient_ge20_rate": ext["lenient"]["ge20"]["rate"] or 0.0,
                    "strict_ge20_rate": ext["strict"]["ge20"]["rate"] or 0.0,
                    "lenient_all_rate": ext["lenient"]["all"]["rate"] or 0.0,
                    "strict_all_rate": ext["strict"]["all"]["rate"] or 0.0,
                    "false_alarms_per_30_days_all": ext["false_alarms"]["per_30_days"],
                    "alarm_days_per_30_days": ext["false_alarms"]["alarm_days_per_30_days"],
                }
            )
            mlflow.log_metrics(numeric)
            mlflow.log_artifact(str(md))
            mlflow.log_artifact(str(js))
        print(md.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
