"""Metric catalogue, metric store and series plan (FR-010 to FR-012)."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import pytest
import yaml
from pydantic import ValidationError

from ds.catalogue import Catalogue, DetectionConfig, load_catalogue, load_detection_config

pytestmark = pytest.mark.unit


def test_catalogue_has_the_eight_planned_kpis() -> None:
    names = [k.name for k in load_catalogue().kpis]
    assert names == [
        "orders",
        "gmv",
        "aov",
        "freight_ratio",
        "late_delivery_rate",
        "avg_delivery_days",
        "avg_review_score",
        "low_review_rate",
    ]
    for kpi in load_catalogue().kpis:  # FR-010 fields
        assert kpi.unit and kpi.owner and kpi.description and kpi.min_sample >= 1


def test_catalogue_rejects_a_count_kpi_with_a_denominator() -> None:
    raw = yaml.safe_load(Path("config/metrics.yaml").read_text(encoding="utf-8"))
    raw["kpis"][0]["denominator"] = "count(*)"
    with pytest.raises(ValidationError, match="no denominator"):
        Catalogue.model_validate(raw)


def test_catalogue_rejects_unknown_fields() -> None:
    raw = yaml.safe_load(Path("config/metrics.yaml").read_text(encoding="utf-8"))
    raw["kpis"][0]["treshold"] = 3  # typo must not be silently ignored
    with pytest.raises(ValidationError):
        Catalogue.model_validate(raw)


def test_detection_config_keeps_backtest_before_evaluation() -> None:
    cfg = load_detection_config()
    assert cfg.backtest.end < cfg.evaluation.start
    bad = cfg.model_dump()
    bad["evaluation"]["start"] = cfg.backtest.start
    with pytest.raises(ValidationError, match="backtest period must end"):
        DetectionConfig.model_validate(bad)


def test_every_series_is_dense_over_the_window(dense_con: duckdb.DuckDBPyConnection) -> None:
    cfg = load_detection_config()
    days = (cfg.monitoring_window.end - cfg.monitoring_window.start).days + 1
    counts = dense_con.execute(
        "SELECT count(*) FROM metrics.daily_kpi GROUP BY kpi, dimension, segment"
    ).fetchall()
    assert {c[0] for c in counts} == {days}


def test_segments_add_up_to_the_total(dense_con: duckdb.DuckDBPyConnection) -> None:
    bad = dense_con.execute(
        """
        WITH seg AS (SELECT kpi, dimension, date, sum(numerator) num, sum(denominator) den, sum(n) n
                     FROM metrics.daily_kpi WHERE dimension <> 'total' AND segment <> '__other__' GROUP BY ALL)
        SELECT count(*) FROM seg JOIN metrics.daily_kpi t ON t.kpi = seg.kpi AND t.date = seg.date
          AND t.dimension = 'total'
        WHERE abs(t.numerator - seg.num) > 1e-6 OR t.n <> seg.n
           OR abs(coalesce(t.denominator, 0) - coalesce(seg.den, 0)) > 1e-6
        """
    ).fetchall()[0][0]
    assert bad == 0


def test_kpi_values_match_a_direct_computation(dense_con: duckdb.DuckDBPyConnection) -> None:
    day = "2017-11-07"
    direct = dense_con.execute(
        "SELECT count(*), sum(order_gmv) / count(*) FROM marts.fct_orders WHERE purchase_date = ? AND has_items",
        [day],
    ).fetchall()[0]
    store = dict(
        dense_con.execute(
            "SELECT kpi, CASE WHEN denominator IS NULL THEN numerator ELSE numerator / denominator END "
            "FROM metrics.daily_kpi WHERE dimension = 'total' AND date = ? AND kpi IN ('aov', 'gmv')",
            [day],
        ).fetchall()
    )
    assert store["aov"] == pytest.approx(float(direct[1]))
    late = dense_con.execute(
        "SELECT avg(CAST(is_late AS DOUBLE)) FROM marts.fct_orders WHERE is_delivered AND delivered_date = ?",
        [day],
    ).fetchall()[0][0]
    stored_late = dense_con.execute(
        "SELECT numerator / denominator FROM metrics.daily_kpi WHERE kpi = 'late_delivery_rate' "
        "AND dimension = 'total' AND date = ?",
        [day],
    ).fetchall()[0][0]
    assert stored_late == pytest.approx(late)


def test_weekly_values_are_sums_of_full_weeks(dense_con: duckdb.DuckDBPyConnection) -> None:
    week_end = "2017-11-12"  # a Sunday
    weekly = dense_con.execute(
        "SELECT numerator, n FROM metrics.weekly_kpi WHERE kpi = 'orders' AND dimension = 'total' AND date = ?",
        [week_end],
    ).fetchall()[0]
    daily = dense_con.execute(
        "SELECT sum(numerator), sum(n) FROM metrics.daily_kpi WHERE kpi = 'orders' AND dimension = 'total' "
        "AND date BETWEEN DATE '2017-11-06' AND DATE '2017-11-12'"
    ).fetchall()[0]
    assert weekly == daily
    weekdays = {
        r[0]
        for r in dense_con.execute(
            "SELECT DISTINCT isodow(date) FROM metrics.weekly_kpi"
        ).fetchall()
    }
    assert weekdays == {7}


def test_plan_classifies_series_by_the_coverage_rule(dense_con: duckdb.DuckDBPyConnection) -> None:
    cfg, catalogue = load_detection_config(), load_catalogue()
    frame = dense_con.execute(
        "SELECT kpi, dimension, segment, date, n FROM metrics.daily_kpi "
        "WHERE kpi = 'aov' AND dimension = 'customer_state' AND segment <> '__other__'"
    ).df()
    plan = (
        dense_con.execute(
            "SELECT segment, granularity, daily_coverage FROM metrics.series_plan "
            "WHERE kpi = 'aov' AND dimension = 'customer_state' AND segment <> '__other__'"
        )
        .df()
        .set_index("segment")
    )
    minimum = catalogue.kpi("aov").min_sample
    for segment, g in frame.groupby("segment"):
        coverage = (g["n"] >= minimum).mean()
        assert plan.loc[segment, "daily_coverage"] == pytest.approx(coverage)
        if coverage >= cfg.sample_rule.coverage:
            assert plan.loc[segment, "granularity"] == "daily"
        else:
            assert plan.loc[segment, "granularity"] in ("weekly", "pooled")


def test_pooled_segments_form_the_other_series(dense_con: duckdb.DuckDBPyConnection) -> None:
    pooled = dense_con.execute(
        "SELECT count(*) FROM metrics.series_plan WHERE granularity = 'pooled'"
    ).fetchall()[0][0]
    assert pooled > 0  # the synthetic data has small segments too
    bad = dense_con.execute(
        """
        WITH m AS (SELECT k.kpi, k.dimension, k.date, sum(k.n) n FROM metrics.daily_kpi k
                   JOIN metrics.series_plan p USING (kpi, dimension, segment) WHERE p.granularity = 'pooled' GROUP BY ALL)
        SELECT count(*) FROM m JOIN metrics.daily_kpi o ON o.kpi = m.kpi AND o.dimension = m.dimension
          AND o.date = m.date AND o.segment = '__other__' WHERE o.n <> m.n
        """
    ).fetchall()[0][0]
    assert bad == 0


def test_totals_are_always_monitored_unless_impossible(
    dense_con: duckdb.DuckDBPyConnection,
) -> None:
    totals = dict(
        dense_con.execute(
            "SELECT kpi, granularity FROM metrics.series_plan WHERE dimension = 'total'"
        ).fetchall()
    )
    assert set(totals) == {k.name for k in load_catalogue().kpis}
    assert "pooled" not in totals.values()  # a total can never be pooled


def test_series_view_only_exposes_monitored_series(dense_con: duckdb.DuckDBPyConnection) -> None:
    grans = {
        r[0]
        for r in dense_con.execute("SELECT DISTINCT granularity FROM metrics.series").fetchall()
    }
    assert grans <= {"daily", "weekly"}
    frame = dense_con.execute(
        "SELECT * FROM metrics.series WHERE granularity = 'weekly' LIMIT 50"
    ).df()
    assert (pd.to_datetime(frame["date"]).dt.dayofweek == 6).all()
