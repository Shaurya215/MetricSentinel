"""Forecasting (FR-030), detection (FR-031 to FR-034) and evaluation scoring (FR-080)."""

from __future__ import annotations

import json
from datetime import date

import duckdb
import numpy as np
import pandas as pd
import pytest

from ds.catalogue import load_catalogue, load_detection_config
from ds.detection import ANOMALY_COLUMNS, Detector, detect_day, detect_range, score_points
from ds.forecasting import ForecastSpec, clean_history, forecast_origins, load_series, mondays
from evaluation.detection import band, changed_series, fault_caused_mask, summarise

pytestmark = pytest.mark.unit
SPEC = ForecastSpec(level=95, daily_season=7, weekly_season=1)
DETECTOR = Detector(
    groups={
        g: {"model": "SeasonalNaive", "calibration": 1.0}
        for g in (
            "volume|daily",
            "volume|weekly",
            "ratio|daily",
            "ratio|weekly",
            "rate|daily",
            "rate|weekly",
            "mean|daily",
            "mean|weekly",
        )
    },
    level=95,
    version="test",
)


def test_mondays_cover_the_period() -> None:
    days = mondays(date(2018, 1, 3), date(2018, 1, 29))
    assert days == [
        date(2018, 1, 1),
        date(2018, 1, 8),
        date(2018, 1, 15),
        date(2018, 1, 22),
        date(2018, 1, 29),
    ]


def test_event_days_are_removed_from_training() -> None:
    cfg = load_detection_config()
    frame = pd.DataFrame(
        {
            "unique_id": "x",
            "granularity": "daily",
            "ds": pd.date_range("2017-11-20", "2017-12-02"),
            "y": 100.0,
            "n": 100,
            "min_n": 10,
        }
    )
    frame.loc[frame["ds"] == "2017-11-24", "y"] = 1000.0  # Black Friday spike
    cleaned = clean_history(frame, cfg)
    assert cleaned["y"].max() == 100.0


def test_small_sample_days_are_removed_from_training() -> None:
    cfg = load_detection_config()
    frame = pd.DataFrame(
        {
            "unique_id": "x",
            "granularity": "daily",
            "ds": pd.date_range("2018-02-01", periods=5),
            "y": [1.0, 1.0, 9.0, 1.0, 1.0],
            "n": [50, 50, 3, 50, 50],
            "min_n": 30,
        }
    )
    assert clean_history(frame, cfg)["y"].tolist() == [1.0] * 5


def test_forecasts_never_use_the_origin_or_later(dense_con: duckdb.DuckDBPyConnection) -> None:
    cfg, catalogue = load_detection_config(), load_catalogue()
    series = load_series(dense_con, catalogue)
    series = series[series["unique_id"] == "orders|total|all|daily"]
    origin = date(2017, 11, 6)
    models = {"orders|total|all|daily": ["SeasonalNaive", "AutoETS"]}
    first = forecast_origins(series, [origin], models, SPEC, cfg)
    tampered = series.copy()
    tampered.loc[tampered["ds"] >= pd.Timestamp(origin), "y"] *= 50  # change the future only
    second = forecast_origins(tampered, [origin], models, SPEC, cfg)
    pd.testing.assert_frame_equal(first, second)
    assert first["ds"].min() == pd.Timestamp(origin) and len(first) == 2 * 7


def _points(
    y: list[float], kpi: str = "orders", dims: list[tuple[str, str]] | None = None
) -> pd.DataFrame:
    dims = dims or [("total", "all")] * len(y)
    return pd.DataFrame(
        {
            "unique_id": [f"{kpi}|{d}|{s}|daily" for d, s in dims],
            "kpi": kpi,
            "dimension": [d for d, _ in dims],
            "segment": [s for _, s in dims],
            "granularity": "daily",
            "ds": pd.Timestamp("2018-03-07"),
            "y": y,
            "n": 500,
            "min_n": 10,
            "yhat": 100.0,
            "lo": 80.4,
            "hi": 119.6,
            "model": "SeasonalNaive",
        }
    )


def test_a_large_deviation_is_flagged_and_noise_is_not() -> None:
    cfg, catalogue = load_detection_config(), load_catalogue()
    scored = score_points(
        _points([40.0, 101.0], dims=[("total", "all"), ("customer_state", "SP")]),
        DETECTOR,
        catalogue,
        cfg,
    )
    anomalies = detect_day(scored, cfg)
    assert len(anomalies) == 1
    row = anomalies.iloc[0]
    assert (row["dimension"], row["direction"]) == ("total", "down")
    assert row["observed"] == 40.0 and row["expected"] == 100.0
    assert row["severity"] == pytest.approx(0.6 * np.log(500))


def test_one_sided_kpis_ignore_good_deviations() -> None:
    cfg, catalogue = load_detection_config(), load_catalogue()
    good = score_points(
        _points([0.01], kpi="late_delivery_rate"), DETECTOR, catalogue, cfg
    )  # far BELOW: good news
    assert detect_day(good, cfg).empty
    bad = score_points(_points([400.0], kpi="late_delivery_rate"), DETECTOR, catalogue, cfg)
    assert len(detect_day(bad, cfg)) == 1


def test_volume_kpis_alert_on_spikes_too() -> None:
    cfg, catalogue = load_detection_config(), load_catalogue()
    assert len(detect_day(score_points(_points([200.0]), DETECTOR, catalogue, cfg), cfg)) == 1


def test_segment_flags_are_grouped_under_one_anomaly() -> None:
    cfg, catalogue = load_detection_config(), load_catalogue()
    pts = _points(
        [30.0, 20.0, 25.0],
        dims=[("total", "all"), ("customer_state", "SP"), ("payment_type", "boleto")],
    )
    anomalies = detect_day(score_points(pts, DETECTOR, catalogue, cfg), cfg)
    assert len(anomalies) == 1
    assert anomalies.iloc[0]["dimension"] == "total"
    assert json.loads(anomalies.iloc[0]["related_segments"]) == [
        "customer_state=SP",
        "payment_type=boleto",
    ]


def test_benjamini_hochberg_controls_many_marginal_tests() -> None:
    """200 series each just outside the interval: BH must stop most of them."""
    cfg, catalogue = load_detection_config(), load_catalogue()
    dims = [("customer_state", f"S{i}") for i in range(200)]
    rng = np.random.default_rng(0)
    pts = _points(list(100 + rng.normal(0, 10, 200)), dims=dims)  # pure noise, sigma = 10
    flagged = detect_day(score_points(pts, DETECTOR, catalogue, cfg), cfg)
    assert flagged.empty


def test_known_event_days_are_not_tested() -> None:
    cfg, catalogue = load_detection_config(), load_catalogue()
    pts = _points([400.0]).assign(ds=pd.Timestamp("2017-11-24"))
    scored = score_points(pts, DETECTOR, catalogue, cfg)
    assert not scored["eligible"].any()
    assert detect_day(scored, cfg).empty


def test_small_samples_are_not_tested() -> None:
    cfg, catalogue = load_detection_config(), load_catalogue()
    pts = _points([400.0], kpi="aov").assign(n=5, min_n=30)
    assert not score_points(pts, DETECTOR, catalogue, cfg)["eligible"].any()


def test_detect_range_end_to_end_on_synthetic_data(dense_con: duckdb.DuckDBPyConnection) -> None:
    from ds.detection import forecast_period

    cfg, catalogue = load_detection_config(), load_catalogue()
    series = load_series(dense_con, catalogue)
    days = [date(2018, 3, d) for d in range(5, 12)]
    spiked = series.copy()
    hit = (spiked["unique_id"] == "orders|total|all|daily") & (
        spiked["ds"] == pd.Timestamp("2018-03-08")
    )
    spiked.loc[hit, "y"] *= 4
    fc = forecast_period(spiked, days[0], days[-1], DETECTOR, catalogue, cfg)
    anomalies, tests = detect_range(spiked, fc, days, DETECTOR, catalogue, cfg)
    assert tests > 0
    found = anomalies[(anomalies["kpi"] == "orders") & (anomalies["date"] == date(2018, 3, 8))]
    assert len(found) == 1 and found.iloc[0]["direction"] == "up"


# ---------------------------------------------------------------------------
# Evaluation scoring
# ---------------------------------------------------------------------------
def test_magnitude_bands() -> None:
    assert [band(0.05), band(0.15), band(0.2), band(0.9), band(None)] == [
        "<10%",
        "10-20%",
        ">=20%",
        ">=20%",
        "unknown",
    ]


def test_changed_series_finds_only_differences() -> None:
    clean = pd.DataFrame(
        {
            "unique_id": ["a", "a", "b"],
            "ds": pd.to_datetime(["2018-01-01", "2018-01-02", "2018-01-01"]),
            "y": [1.0, 2.0, 3.0],
            "n": [1, 1, 1],
        }
    )
    scn = clean.copy()
    scn.loc[2, "y"] = 3.5
    assert changed_series(clean, scn) == {"b"}


def test_summary_counts_recall_and_false_alarms() -> None:
    results = pd.DataFrame(
        {
            "measured_magnitude": [0.5, 0.3, 0.05, 0.25],
            "detected": [True, False, False, True],
            "delay_days": [1, None, None, 3],
            "fault_type": ["a", "a", "b", "b"],
            "size_tier": ["large"] * 4,
            "true_positive_anomalies": [2, 0, 0, 1],
            "caused_anomalies": [2, 1, 0, 1],
        }
    )
    clean = pd.DataFrame({"x": range(4)})
    s = summarise(results, clean, clean_days=60, clean_tests=1000)
    assert s["recall"] == 0.5
    assert s["recall_magnitude_ge_20pct"] == pytest.approx(2 / 3)
    assert s["false_alarms_per_30_days"] == 2.0
    assert s["precision_fault_caused"] == 0.75
    assert s["median_delay_days"] == 2.0
    assert s["nfr_021"]["false_alarms_met"] and not s["nfr_021"]["recall_met"]


def test_fault_caused_filter_works_on_empty_and_full_frames() -> None:
    empty = pd.DataFrame(columns=ANOMALY_COLUMNS)
    assert list(empty[fault_caused_mask(empty, set())].columns) == ANOMALY_COLUMNS
    one = pd.DataFrame([dict.fromkeys(ANOMALY_COLUMNS)]).assign(
        date=date(2018, 3, 1), kpi="orders", granularity="daily", dimension="total", segment="all"
    )
    seen: set[tuple[str, ...]] = {("2018-03-01", "orders", "daily", "total", "all")}
    assert len(one[fault_caused_mask(one, seen)]) == 0
    assert len(one[fault_caused_mask(one, set())]) == 1


def test_sample_gate_does_not_apply_to_count_and_sum_kpis(
    dense_con: duckdb.DuckDBPyConnection,
) -> None:
    """Regression (found in the Phase 3 evaluation): a day with ZERO orders was never tested,
    because 0 is below the minimum sample. For count/sum KPIs the sample is the value."""
    series = load_series(dense_con, load_catalogue())
    gates = series.groupby("kpi")["min_n"].first()
    assert gates["orders"] == 0 and gates["gmv"] == 0
    assert gates["aov"] == 30 and gates["late_delivery_rate"] == 30


def test_a_day_with_zero_orders_is_flagged() -> None:
    cfg, catalogue = load_detection_config(), load_catalogue()
    pts = _points([0.0]).assign(n=0, min_n=0)
    scored = score_points(pts, DETECTOR, catalogue, cfg)
    assert scored["eligible"].all()
    anomalies = detect_day(scored, cfg)
    assert len(anomalies) == 1 and anomalies.iloc[0]["direction"] == "down"
