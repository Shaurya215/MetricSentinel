"""Paired comparison of two evaluation runs and the pre-registered keep/drop rule
(ADR-phase3b section 10).

    uv run python -m evaluation.compare data/reports/dev2017_baseline.json data/reports/dev2017_A.json --change A

Both runs are scored on the same clean days and the same scenarios, so the comparison is PAIRED:

* clean days: for each clean day, did a configuration raise any alarm? An exact McNemar test on the
  days where the two disagree.
* scenarios: for each scenario, was it detected? An exact McNemar test on the scenarios where the
  two disagree (lenient and strict, at >= 20% and overall).

The tests are reported, not used as gates. The only gate is the pre-registered rule:

    B and C are KEPT only if
      (clean alarms per 30 days fall by >= 25% relative AND recall at >= 20% (lenient) loses <= 2 scenarios)
      OR
      (recall at >= 20% (lenient) rises by >= 3 scenarios AND clean alarms per 30 days rise by <= 10%).

A and D are validity and labelling rules and are kept regardless; their deltas are still reported.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any

from scipy.stats import binomtest

from ds.catalogue import load_detection_config
from evaluation.detection import daterange

CAUSES = ("dataset_cutoff", "calendar_window", "known_event", "gmv_spike", "other")
ALWAYS_KEPT = {"A", "D"}


def mcnemar_exact(only_first: int, only_second: int) -> float:
    """Two-sided exact McNemar p-value from the two discordant counts."""
    n = only_first + only_second
    if n == 0:
        return 1.0
    return float(binomtest(min(only_first, only_second), n, 0.5).pvalue)


def load_result(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def clean_day_universe(result: dict[str, Any]) -> list[date]:
    """The clean days the run actually tested: its period, minus known events, minus silent suppression."""
    cfg = load_detection_config()
    protocol = result["meta"]["protocol"]
    period = cfg.development.test if protocol == "dev2017" and cfg.development else cfg.evaluation
    silent = (
        set(result["summary"]["extended"]["guard"]["silently_suppressed_days"])
        if ("guard" in result["summary"]["extended"])
        else set()
    )
    return [
        d
        for d in daterange(period.start, period.end)
        if cfg.event_on(d) is None and d.isoformat() not in silent
    ]


def alarm_days(result: dict[str, Any]) -> set[str]:
    return {str(a["date"])[:10] for a in result["clean_anomalies"]}


def _scenario_flags(result: dict[str, Any], key: str) -> dict[str, bool]:
    return {r["id"]: bool(r[key]) for r in result["scenarios"]}


def _ge20(result: dict[str, Any]) -> set[str]:
    from evaluation.detection import band

    return {r["id"] for r in result["scenarios"] if band(r["measured_magnitude"]) == ">=20%"}


def paired_scenarios(
    base: dict[str, Any], other: dict[str, Any], key: str, only: set[str] | None
) -> dict[str, Any]:
    a, b = _scenario_flags(base, key), _scenario_flags(other, key)
    ids = sorted(set(a) & set(b) if only is None else (set(a) & set(b) & only))
    gained = sum(1 for i in ids if b[i] and not a[i])
    lost = sum(1 for i in ids if a[i] and not b[i])
    return {
        "n": len(ids),
        "base_k": sum(a[i] for i in ids),
        "other_k": sum(b[i] for i in ids),
        "gained": gained,
        "lost": lost,
        "p_value": mcnemar_exact(gained, lost),
    }


def paired_days(base: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    universe = {d.isoformat() for d in clean_day_universe(base)} & {
        d.isoformat() for d in clean_day_universe(other)
    }
    a, b = alarm_days(base) & universe, alarm_days(other) & universe
    only_base, only_other = len(a - b), len(b - a)
    return {
        "days": len(universe),
        "base_alarm_days": len(a),
        "other_alarm_days": len(b),
        "only_in_base": only_base,
        "only_in_other": only_other,
        "both": len(a & b),
        "p_value": mcnemar_exact(only_base, only_other),
    }


def decide(
    change: str, base_k: int, other_k: int, base_fa: float, other_fa: float
) -> dict[str, Any]:
    """The pre-registered keep/drop rule (lenient recall at >= 20%, all alerts counted)."""
    fa_change = (other_fa - base_fa) / base_fa if base_fa else 0.0
    clause_1 = fa_change <= -0.25 and other_k >= base_k - 2
    clause_2 = other_k >= base_k + 3 and fa_change <= 0.10
    gated = change not in ALWAYS_KEPT
    keep = (clause_1 or clause_2) if gated else True
    return {
        "change": change,
        "rule": "gated (B, C)" if gated else "kept regardless (validity or labelling rule)",
        "recall_ge20_lenient": {"base": base_k, "other": other_k, "delta": other_k - base_k},
        "false_alarms_per_30_days": {
            "base": base_fa,
            "other": other_fa,
            "relative_change": fa_change,
        },
        "clause_fewer_false_alarms": clause_1,
        "clause_more_recall": clause_2,
        "keep": keep,
    }


def _count(value: Any) -> int:
    """Older result files stored a text note here instead of a count."""
    return value if isinstance(value, int) else 0


def compare(base: dict[str, Any], other: dict[str, Any], change: str) -> dict[str, Any]:
    ge20 = _ge20(base)
    bx, ox = base["summary"]["extended"], other["summary"]["extended"]
    fa_b, fa_o = bx["false_alarms"], ox["false_alarms"]
    rule = decide(
        change,
        bx["lenient"]["ge20"]["k"],
        ox["lenient"]["ge20"]["k"],
        fa_b["per_30_days"],
        fa_o["per_30_days"],
    )
    return {
        "change": change,
        "base": base["meta"]["git_sha"][:8] + " flags=" + base["meta"].get("flags", "none"),
        "other": other["meta"]["git_sha"][:8] + " flags=" + other["meta"].get("flags", "none"),
        "scenarios": {
            "lenient_ge20": paired_scenarios(base, other, "detected", ge20),
            "strict_ge20": paired_scenarios(base, other, "strict_detected", ge20),
            "lenient_all": paired_scenarios(base, other, "detected", None),
            "strict_all": paired_scenarios(base, other, "strict_detected", None),
        },
        "clean_days": paired_days(base, other),
        "false_alarms": {
            "base": {
                "alerts": fa_b["total"],
                "alarm_days": fa_b["alarm_days"],
                "per_30": fa_b["per_30_days"],
                "alarm_days_per_30": fa_b["alarm_days_per_30_days"],
                "causes": fa_b["exclusive"],
                "guard_alerts": _count(fa_b.get("guard_alerts")),
            },
            "other": {
                "alerts": fa_o["total"],
                "alarm_days": fa_o["alarm_days"],
                "per_30": fa_o["per_30_days"],
                "alarm_days_per_30": fa_o["alarm_days_per_30_days"],
                "causes": fa_o["exclusive"],
                "guard_alerts": _count(fa_o.get("guard_alerts")),
            },
        },
        "decision": rule,
    }


def _rate(k: int, n: int) -> str:
    return f"{k}/{n} ({k / n:.1%})" if n else "n/a"


def render(c: dict[str, Any]) -> str:
    s, d, fa, r = c["scenarios"], c["clean_days"], c["false_alarms"], c["decision"]
    lines = [
        f"## Change {c['change']} against the 2017 baseline",
        "",
        f"base: `{c['base']}` · this run: `{c['other']}`",
        "",
        "### Recall (paired on the same scenarios)",
        "",
        "| measure | baseline | this run | gained | lost | exact McNemar p |",
        "|---|---|---|---|---|---|",
    ]
    for label, key in (
        ("lenient, >= 20% (NFR-021 comparator)", "lenient_ge20"),
        ("strict, >= 20%", "strict_ge20"),
        ("lenient, all", "lenient_all"),
        ("strict, all", "strict_all"),
    ):
        p = s[key]
        lines.append(
            f"| {label} | {_rate(p['base_k'], p['n'])} | {_rate(p['other_k'], p['n'])} | {p['gained']} | "
            f"{p['lost']} | {p['p_value']:.3f} |"
        )
    lines += [
        "",
        "### Clean-day alarms",
        "",
        "| | baseline | this run |",
        "|---|---|---|",
        f"| alerts (all counted) | {fa['base']['alerts']} | {fa['other']['alerts']} |",
        f"| false alarms per 30 days | {fa['base']['per_30']:.2f} | {fa['other']['per_30']:.2f} |",
        f"| alarm-days | {fa['base']['alarm_days']} | {fa['other']['alarm_days']} |",
        f"| alarm-days per 30 days | {fa['base']['alarm_days_per_30']:.2f} | {fa['other']['alarm_days_per_30']:.2f} |",
        f"| of which guard alerts | {fa['base']['guard_alerts']} | {fa['other']['guard_alerts']} |",
        "",
        f"Paired over the same {d['days']} clean days: {d['only_in_base']} days alarm only in the baseline, "
        f"{d['only_in_other']} only in this run, {d['both']} in both (exact McNemar p = {d['p_value']:.3f}).",
        "",
        "### False alarms by cause",
        "",
        "| cause | baseline | this run |",
        "|---|---|---|",
    ]
    lines += [f"| {k} | {fa['base']['causes'][k]} | {fa['other']['causes'][k]} |" for k in CAUSES]
    verdict = "KEEP" if r["keep"] else "DROP"
    lines += [
        "",
        "### Pre-registered keep/drop rule",
        "",
        f"- Rule applied: {r['rule']}.",
        f"- Lenient recall at >= 20%: {r['recall_ge20_lenient']['base']} -> {r['recall_ge20_lenient']['other']} "
        f"({r['recall_ge20_lenient']['delta']:+d}).",
        f"- False alarms per 30 days: {r['false_alarms_per_30_days']['base']:.2f} -> "
        f"{r['false_alarms_per_30_days']['other']:.2f} ({r['false_alarms_per_30_days']['relative_change']:+.1%}).",
        f"- Clause 1 (false alarms down >= 25% and recall loses <= 2): {'yes' if r['clause_fewer_false_alarms'] else 'no'}.",
        f"- Clause 2 (recall up >= 3 and false alarms up <= 10%): {'yes' if r['clause_more_recall'] else 'no'}.",
        f"- **Decision: {verdict}**.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Paired comparison of two evaluation runs.")
    parser.add_argument("base", type=Path)
    parser.add_argument("other", type=Path)
    parser.add_argument("--change", required=True, help="A, B, C, D or a label for a combined run")
    parser.add_argument("--out", type=Path, help="write the markdown here as well")
    args = parser.parse_args(argv)
    result = compare(load_result(args.base), load_result(args.other), args.change)
    text = render(result)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        args.out.with_suffix(".json").write_text(
            json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8"
        )
    print(text)


if __name__ == "__main__":
    main()
