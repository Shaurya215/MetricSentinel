"""Fixed inputs and fingerprinting for the synthetic golden test.

The golden file `synthetic_baseline.json` was produced by the Phase 3 evaluation code
(commit 5720997, before any 3b change) on the inputs below. The test re-runs the current
code on the same inputs and requires identical alerts, counts and outcomes.

Everything here is FAKE data (see pipelines/synthetic.py). It checks that refactoring did
not change behaviour; it says nothing about the real Olist data.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pandas as pd

from ds.detection import Detector
from pipelines.faults import Scenario

GOLDEN_COMMIT = "5720997"
GROUPS = ("volume", "ratio", "rate", "mean")


def synthetic_detector() -> Detector:
    """Seasonal-naive everywhere, no calibration: fast, deterministic, and not tuned to anything."""
    groups = {
        f"{family}|{gran}": {"model": "SeasonalNaive", "calibration": 1.0}
        for family in GROUPS
        for gran in ("daily", "weekly")
    }
    return Detector(groups=groups, level=95, version="golden-synthetic")


def _scn(
    idx: int,
    fault: str,
    dimension: str,
    value: str,
    start: date,
    days: int,
    mag: float,
    kpis: list[str],
    mode: str | None = None,
) -> Scenario:
    return Scenario.model_validate(
        {
            "id": f"G{idx}-{fault}",
            "fault_type": fault,
            "dimension": dimension,
            "value": value,
            "start": start,
            "duration_days": days,
            "magnitude": mag,
            "mode": mode,
            "seed": 9000 + idx,
            "affected_kpis": kpis,
            "size_tier": "golden",
        }
    )


def synthetic_scenarios() -> list[Scenario]:
    return [
        _scn(
            1,
            "segment_level_shift",
            "customer_state",
            "SP",
            date(2018, 3, 5),
            14,
            0.6,
            ["orders", "gmv"],
            "drop",
        ),
        _scn(
            2,
            "payment_failure",
            "payment_type",
            "credit_card",
            date(2018, 4, 9),
            14,
            0.6,
            ["orders", "gmv"],
        ),
        _scn(
            3,
            "seller_delay",
            "seller_state",
            "SP",
            date(2018, 2, 12),
            14,
            7.0,
            ["avg_delivery_days", "late_delivery_rate"],
        ),
        _scn(
            4,
            "category_price_change",
            "category_en",
            "toys",
            date(2018, 5, 7),
            14,
            0.5,
            ["aov", "gmv", "freight_ratio"],
        ),
        _scn(
            5,
            "review_collapse",
            "category_en",
            "health_beauty",
            date(2018, 6, 4),
            14,
            0.9,
            ["avg_review_score", "low_review_rate"],
        ),
        _scn(
            6,
            "pipeline_bug",
            "data_quality",
            "fct_orders",
            date(2018, 5, 14),
            1,
            1.0,
            ["orders", "gmv"],
            "drop",
        ),
    ]


SCENARIO_KEYS = (
    "id",
    "fault_type",
    "dimension",
    "value",
    "nominal_magnitude",
    "start",
    "effect_start",
    "effect_end",
    "segment_relative_change",
    "total_relative_change",
    "measured_magnitude",
    "detected",
    "delay_days",
    "true_positive_anomalies",
    "caused_anomalies",
    "changed_series",
    "tests",
    "detected_by",
)


def _plain(value: Any) -> Any:
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "item"):
        return _plain(value.item())
    return value


def fingerprint(
    clean: pd.DataFrame, clean_tests: int, rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Everything that defines the evaluation outcome, in plain JSON types."""
    return {
        "clean_tests": int(clean_tests),
        "clean_anomalies": [{k: _plain(v) for k, v in r.items()} for r in clean.to_dict("records")],
        "scenarios": [{k: _plain(r[k]) for k in SCENARIO_KEYS} for r in rows],
    }


def assert_same(actual: dict[str, Any], golden: dict[str, Any], *, float_rel: float = 1e-9) -> None:
    """Discrete values must match exactly; floats to a relative 1e-9 (platform rounding only)."""
    import math

    def same(a: Any, b: Any, path: str) -> None:
        if isinstance(b, float) and isinstance(a, float):
            assert math.isclose(a, b, rel_tol=float_rel, abs_tol=1e-12), f"{path}: {a!r} != {b!r}"
        elif isinstance(b, dict):
            assert isinstance(a, dict) and set(a) == set(b), (
                f"{path}: keys {sorted(a)} != {sorted(b)}"
            )
            for k in b:
                same(a[k], b[k], f"{path}.{k}")
        elif isinstance(b, list):
            assert isinstance(a, list) and len(a) == len(b), f"{path}: length {len(a)} != {len(b)}"
            for i, (x, y) in enumerate(zip(a, b, strict=True)):
                same(x, y, f"{path}[{i}]")
        else:
            assert a == b, f"{path}: {a!r} != {b!r}"

    same(json.loads(json.dumps(actual, default=str)), golden, "$")
