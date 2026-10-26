"""Change A: the volume-collapse guard and the last reliable day (ADR-phase3b section 8)."""

from __future__ import annotations

from datetime import date, timedelta

import duckdb
import pandas as pd
import pytest

from ds.catalogue import FeedGuardConfig, load_catalogue, load_detection_config, with_flags
from ds.detection import ANOMALY_COLUMNS, detect_range, forecast_period
from ds.feed_guard import (
    COLLAPSE_FRACTION,
    GUARD_DIMENSION,
    GUARD_KPI,
    GUARD_LABEL,
    GUARD_SEGMENT,
    NORM_DAYS,
    apply_guard,
    guard_alert,
    is_guard_alert,
    total_orders,
)
from ds.forecasting import load_series
from evaluation.detection import flagged_series, targeted_mask
from pipelines.faults import Scenario
from tests.golden.synthetic_inputs import synthetic_detector

pytestmark = pytest.mark.unit
CONFIG = FeedGuardConfig(data_end=date(2018, 3, 10))


def _totals(values: list[float], start: str = "2018-01-01") -> pd.Series:
    return pd.Series(values, index=pd.date_range(start, periods=len(values)).date, dtype=float)


def test_the_preregistered_parameters_are_unchanged() -> None:
    cfg = load_detection_config()
    assert cfg.feed_guard.collapse_fraction == COLLAPSE_FRACTION == 0.35
    assert cfg.feed_guard.norm_days == NORM_DAYS == 28
    assert cfg.feed_guard.data_end == date(2018, 8, 24)  # recorded in DATA_PLAN.md


def test_alert_wording_does_not_claim_a_cause() -> None:
    assert GUARD_LABEL == "volume collapse (data feed or business)"
    row = guard_alert(date(2018, 3, 8), 10.0, 200.0, 0.35)
    assert (row["kpi"], row["dimension"], row["segment"]) == (
        GUARD_KPI,
        GUARD_DIMENSION,
        GUARD_SEGMENT,
    )
    assert (
        row["direction"] == "down"
        and row["expected"] == 200.0
        and row["lower"] == pytest.approx(70.0)
    )
    assert set(row) == set(ANOMALY_COLUMNS)  # same columns as a statistical anomaly
    assert row["severity"] > 1.0


def test_a_collapse_day_gets_one_alert_and_is_not_tested() -> None:
    totals = _totals([100.0] * 40 + [20.0] + [100.0] * 3)
    days = list(totals.index[38:43])
    result = apply_guard(totals, days, FeedGuardConfig())
    assert [a["date"] for a in result.alerts] == [totals.index[40]]
    assert totals.index[40] not in result.testable_days
    assert result.testable_days == [d for d in days if d != totals.index[40]]
    assert result.past_data_end == []


def test_days_past_the_last_reliable_day_are_silent_not_alerts() -> None:
    totals = _totals([100.0] * 60)
    days = [date(2018, 3, 9) + timedelta(days=i) for i in range(5)]
    result = apply_guard(totals, days, CONFIG)  # data_end is 2018-03-10
    assert result.testable_days == days[:2]
    assert result.past_data_end == days[2:]
    assert result.alerts == []  # past the end: nothing is raised


def test_a_day_that_is_both_past_the_end_and_a_collapse_is_silent() -> None:
    totals = _totals([100.0] * 70 + [5.0])
    day = totals.index[-1]
    result = apply_guard(totals, [day], FeedGuardConfig(data_end=totals.index[-2]))
    assert result.past_data_end == [day] and result.alerts == []


def test_the_threshold_is_exclusive_and_not_tuned() -> None:
    totals = _totals([100.0] * 40 + [35.0, 34.9])
    result = apply_guard(totals, [totals.index[40], totals.index[41]], FeedGuardConfig())
    assert [a["date"] for a in result.alerts] == [totals.index[41]]  # 35.0 is not below 35%


def test_no_history_means_no_alert() -> None:
    totals = _totals([1.0] * 10)  # fewer than 28 days of history: no norm yet
    assert apply_guard(totals, list(totals.index), FeedGuardConfig()).alerts == []


@pytest.fixture
def tampered(dense_con: duckdb.DuckDBPyConnection) -> tuple[pd.DataFrame, pd.DataFrame, date]:
    """Dense synthetic series with total orders cut to 5% on 2018-03-08, and forecasts around it."""
    catalogue, cfg = load_catalogue(), load_detection_config()
    series = load_series(dense_con, catalogue)
    day = date(2018, 3, 8)
    hit = (series["unique_id"] == "orders|total|all|daily") & (series["ds"] == pd.Timestamp(day))
    series.loc[hit, "y"] = series.loc[hit, "y"] * 0.05
    fc = forecast_period(
        series, date(2018, 3, 5), date(2018, 3, 11), synthetic_detector(), catalogue, cfg
    )
    return series, fc, day


def _days() -> list[date]:
    return [date(2018, 3, 5) + timedelta(days=i) for i in range(7)]


def test_flag_off_the_collapse_day_is_tested_like_any_other(tampered) -> None:  # type: ignore[no-untyped-def]
    series, fc, day = tampered
    cfg = load_detection_config()
    anomalies, _ = detect_range(series, fc, _days(), synthetic_detector(), load_catalogue(), cfg)
    assert not is_guard_alert(anomalies).any()
    assert (anomalies["date"] == day).any()  # an ordinary statistical alert on that day


def test_flag_on_one_guard_alert_replaces_every_other_alert_that_day(tampered) -> None:  # type: ignore[no-untyped-def]
    series, fc, day = tampered
    cfg = with_flags(load_detection_config(), ["feed_guard"])
    diagnostics: dict = {}
    anomalies, _ = detect_range(
        series, fc, _days(), synthetic_detector(), load_catalogue(), cfg, diagnostics=diagnostics
    )
    that_day = anomalies[anomalies["date"] == day]
    assert len(that_day) == 1 and bool(is_guard_alert(that_day).all())
    assert that_day.iloc[0]["observed"] == pytest.approx(float(total_orders(series).loc[day]))
    assert [h["date"] for h in diagnostics["guard_collapse"]] == [day]
    assert diagnostics["guard_past_data_end"] == []
    others = anomalies[anomalies["date"] != day]
    assert not is_guard_alert(others).any()


def test_flag_on_matches_flag_off_on_ordinary_days(tampered) -> None:  # type: ignore[no-untyped-def]
    series, fc, day = tampered
    base = load_detection_config()
    days = [d for d in _days() if d != day]
    off, tests_off = detect_range(series, fc, days, synthetic_detector(), load_catalogue(), base)
    on, tests_on = detect_range(
        series, fc, days, synthetic_detector(), load_catalogue(), with_flags(base, ["feed_guard"])
    )
    assert tests_on == tests_off
    pd.testing.assert_frame_equal(off.reset_index(drop=True), on.reset_index(drop=True))


def test_guard_alert_counts_for_strict_recall_only_for_total_targeted_faults() -> None:
    guard = guard_alert(date(2018, 3, 8), 10.0, 200.0, 0.35)
    flags = flagged_series(pd.DataFrame([guard]))
    assert bool(is_guard_alert(pd.DataFrame([guard])).all())
    spec = {
        "id": "T",
        "start": date(2018, 3, 5),
        "duration_days": 14,
        "magnitude": 0.5,
        "seed": 1,
        "affected_kpis": ["orders"],
        "value": "SP",
    }
    for fault, dim, expected in (
        ("pipeline_bug", "data_quality", True),
        ("seller_delay", "seller_state", True),
        ("segment_level_shift", "customer_state", False),
        ("payment_failure", "payment_type", False),
    ):
        s = Scenario.model_validate({**spec, "fault_type": fault, "dimension": dim})
        assert targeted_mask(flags, s, {}).tolist() == [expected], fault
