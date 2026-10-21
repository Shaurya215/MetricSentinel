"""The EDA derives the monitoring window and sample-size rule from the warehouse."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from pipelines import synthetic
from pipelines.eda import MIN_SAMPLE, WARMUP_DAYS, run
from tests.unit.conftest import build_synthetic

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def findings(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict[str, Any], Path]:
    """EDA over a denser fake dataset (the default one has too few orders per day for a window)."""
    raw = tmp_path_factory.mktemp("dense_raw")
    synthetic.generate(raw, n_orders=20_000, n_sellers=60, n_products=150)
    built = build_synthetic(raw, tmp_path_factory.mktemp("dense_built"))
    out = tmp_path_factory.mktemp("eda_out")
    return run(built.warehouse_path, out), out


def test_synthetic_data_is_labelled_in_every_output(findings: tuple[dict[str, Any], Path]) -> None:
    result, out = findings
    assert result["synthetic"] is True
    assert "SYNTHETIC (FAKE) DATA" in (out / "eda_findings.md").read_text(encoding="utf-8")


def test_window_warmup_and_first_detection_date_are_consistent(
    findings: tuple[dict[str, Any], Path],
) -> None:
    window = findings[0]["monitoring_window"]
    start, end = (
        date.fromisoformat(window["window"]["start"]),
        date.fromisoformat(window["window"]["end"]),
    )
    assert (
        date.fromisoformat(window["first_order_date"])
        <= start
        < end
        <= date.fromisoformat(window["last_order_date"])
    )
    assert window["warmup_start"] == window["window"]["start"]
    assert date.fromisoformat(window["first_detection_date"]) == start + timedelta(days=WARMUP_DAYS)
    assert (
        window["monitored_days"]
        == (end - date.fromisoformat(window["first_detection_date"])).days + 1
    )


def test_sparse_period_is_left_out_of_the_window(findings: tuple[dict[str, Any], Path]) -> None:
    assert findings[0]["monitoring_window"]["days_outside_window_with_orders"] > 0


def test_a_dataset_too_sparse_for_a_window_says_so(built: Any, tmp_path: Path) -> None:
    result = run(built.warehouse_path, tmp_path)  # ~4 orders/day: below the dense-day floor
    assert result["monitoring_window"]["window"] is None
    assert "No dense window found" in (tmp_path / "eda_findings.md").read_text(encoding="utf-8")


def test_all_eight_catalogue_kpis_are_profiled(findings: tuple[dict[str, Any], Path]) -> None:
    kpis = findings[0]["kpi_distributions"]
    assert set(kpis) == {
        "orders",
        "gmv",
        "aov",
        "freight_ratio",
        "late_delivery_rate",
        "avg_delivery_days",
        "avg_review_score",
        "low_review_rate",
    }
    assert kpis["orders"]["mean"] > 0
    assert 0 <= kpis["late_delivery_rate"]["mean"] <= 1
    assert 1 <= kpis["avg_review_score"]["mean"] <= 5
    assert (
        "share_of_days_with_sample_ge_30" in kpis["aov"]
        and "share_of_days_with_sample_ge_30" not in kpis["orders"]
    )


def test_sample_rule_partitions_every_series(findings: tuple[dict[str, Any], Path]) -> None:
    segments = findings[0]["segments"]
    assert segments["sample_rule"]["min_sample_per_period"] == MIN_SAMPLE
    assert set(segments["dimensions"]) == {
        "customer_state",
        "category_en",
        "payment_type",
        "seller_tier",
    }
    for dimension in segments["dimensions"].values():
        for counts in dimension["kinds"].values():
            assert (
                sum(counts.values()) == dimension["cardinality"]
            )  # every series is daily, weekly or excluded


def test_seasonality_is_reported(findings: tuple[dict[str, Any], Path]) -> None:
    s = findings[0]["seasonality"]
    assert set(s["weekday_index"]) == {
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    }
    assert s["strongest_weekday_effect"] >= 1.0


def test_expected_issues_are_confirmed_or_refuted_from_data(
    findings: tuple[dict[str, Any], Path],
) -> None:
    issues = {i["issue"]: i["status"] for i in findings[0]["expected_issues"]}
    assert len(issues) == 9
    assert set(issues.values()) <= {"CONFIRMED", "REFUTED"}
    assert issues["Delivered orders with a missing delivery date"] == "CONFIRMED"
    assert (
        issues["Geolocation has many rows per zip prefix"] == "REFUTED"
    )  # the fake data has one row each


def test_json_output_round_trips(findings: tuple[dict[str, Any], Path]) -> None:
    result, out = findings
    assert (
        json.loads((out / "eda_findings.json").read_text(encoding="utf-8"))["data_hash"]
        == result["data_hash"]
    )
