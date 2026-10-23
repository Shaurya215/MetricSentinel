"""Fault injector (FR-020 to FR-022) and simulated clock (FR-007)."""

from __future__ import annotations

import hashlib
from datetime import date

import duckdb
import pytest

from ds.simclock import set_sim_date
from pipelines.build import BuildResult
from pipelines.faults import Scenario, ScenarioWorkspace, measure_effect

pytestmark = pytest.mark.unit


def scenario(**kw: object) -> Scenario:
    base = {
        "id": "T",
        "seed": 7,
        "start": date(2017, 10, 2),
        "duration_days": 14,
        "magnitude": 0.5,
        "affected_kpis": ["orders", "gmv"],
    }
    base.update(kw)
    return Scenario.model_validate(base)


def count(ws: ScenarioWorkspace, sql: str, *params: object) -> int:
    return ws.con.execute(sql, list(params)).fetchall()[0][0]


def test_injection_never_touches_the_warehouse(dense_built: BuildResult) -> None:
    before = hashlib.sha256(dense_built.warehouse_path.read_bytes()).hexdigest()
    with ScenarioWorkspace(dense_built.warehouse_path) as ws:
        ws.inject(
            scenario(
                fault_type="pipeline_bug",
                dimension="data_quality",
                value="fct_orders",
                duration_days=1,
                magnitude=1.0,
                mode="drop",
            )
        )
        ws.build_metrics()
        with pytest.raises(duckdb.Error):  # attached read-only
            ws.con.execute("DELETE FROM wh.marts.fct_orders")
    assert hashlib.sha256(dense_built.warehouse_path.read_bytes()).hexdigest() == before


def test_level_shift_removes_a_share_of_the_segment_inside_the_window_only(
    dense_built: BuildResult,
) -> None:
    s = scenario(
        fault_type="segment_level_shift",
        dimension="customer_state",
        value="SP",
        magnitude=0.5,
        mode="drop",
        start=date(2017, 10, 1),
        duration_days=120,
    )
    sql = (
        "SELECT count(*) FROM marts_scn.fct_orders WHERE customer_state = 'SP' AND purchase_date "
        "BETWEEN ? AND ?"
    )
    with ScenarioWorkspace(dense_built.warehouse_path) as ws:
        inside = count(ws, sql, s.start, s.end)
        outside = count(ws, sql, date(2017, 8, 1), date(2017, 9, 30))
        other_state = count(
            ws, "SELECT count(*) FROM marts_scn.fct_orders WHERE customer_state <> 'SP'"
        )
        ws.inject(s)
        assert inside > 300  # enough orders for the share to be stable
        assert count(ws, sql, s.start, s.end) == pytest.approx(inside * 0.5, rel=0.12)
        assert count(ws, sql, date(2017, 8, 1), date(2017, 9, 30)) == outside
        assert (
            count(ws, "SELECT count(*) FROM marts_scn.fct_orders WHERE customer_state <> 'SP'")
            == other_state
        )
        # child rows of removed orders are removed too
        assert (
            count(
                ws,
                "SELECT count(*) FROM marts_scn.fct_order_items i WHERE NOT EXISTS "
                "(SELECT 1 FROM marts_scn.fct_orders o WHERE o.order_id = i.order_id)",
            )
            == 0
        )


def test_injection_is_deterministic(dense_built: BuildResult) -> None:
    s = scenario(
        fault_type="payment_failure", dimension="payment_type", value="credit_card", magnitude=0.3
    )
    results = []
    for _ in range(2):
        with ScenarioWorkspace(dense_built.warehouse_path) as ws:
            ws.inject(s)
            results.append(
                ws.con.execute(
                    "SELECT md5(string_agg(order_id, ',' ORDER BY order_id)) "
                    "FROM marts_scn.fct_orders"
                ).fetchall()[0][0]
            )
    assert results[0] == results[1]


def test_seller_delay_moves_deliveries_by_n_days(dense_built: BuildResult) -> None:
    s = scenario(
        fault_type="seller_delay",
        dimension="seller_state",
        value="SP",
        magnitude=5.0,
        affected_kpis=["avg_delivery_days", "late_delivery_rate"],
    )
    with ScenarioWorkspace(dense_built.warehouse_path) as ws:
        ws.inject(s)
        moved = ws.con.execute(
            "SELECT count(*), min(datediff('day', c.delivered_date, s.delivered_date)), "
            "max(datediff('day', c.delivered_date, s.delivered_date)) "
            "FROM marts_scn.fct_orders s JOIN wh.marts.fct_orders c USING (order_id) "
            "WHERE s.delivered_date <> c.delivered_date"
        ).fetchall()[0]
        assert moved[0] > 0 and moved[1] == moved[2] == 5
        late = count(
            ws,
            "SELECT count(*) FROM marts_scn.fct_orders WHERE is_late <> (delivered_date > estimated_date)",
        )
        assert late == 0


def test_price_change_cuts_prices_and_order_gmv(dense_built: BuildResult) -> None:
    s = scenario(
        fault_type="category_price_change",
        dimension="category_en",
        value="toys",
        magnitude=0.4,
        affected_kpis=["aov", "gmv", "freight_ratio"],
    )
    with ScenarioWorkspace(dense_built.warehouse_path) as ws:
        ws.inject(s)
        ratio = ws.con.execute(
            "SELECT sum(s.price) / sum(c.price) FROM marts_scn.fct_order_items s JOIN wh.marts.fct_order_items c "
            "USING (order_id, item_seq) WHERE s.category_en = 'toys' AND s.purchase_date BETWEEN ? AND ?",
            [s.start, s.end],
        ).fetchall()[0][0]
        assert float(ratio) == pytest.approx(0.6, abs=0.01)
        mismatch = count(
            ws,
            "SELECT count(*) FROM marts_scn.fct_orders o JOIN (SELECT order_id, sum(price) p FROM "
            "marts_scn.fct_order_items GROUP BY 1) i USING (order_id) WHERE o.order_gmv <> i.p",
        )
        assert mismatch == 0


def test_review_collapse_sets_scores_to_one(dense_built: BuildResult) -> None:
    s = scenario(
        fault_type="review_collapse",
        dimension="category_en",
        value="toys",
        magnitude=1.0,
        affected_kpis=["avg_review_score", "low_review_rate"],
    )
    with ScenarioWorkspace(dense_built.warehouse_path) as ws:
        ws.inject(s)
        scores = {
            r[0]
            for r in ws.con.execute(
                "SELECT DISTINCT r.score FROM marts_scn.fct_reviews r JOIN wh.metrics.order_dims d USING (order_id) "
                "WHERE d.category_en = 'toys' AND r.review_date BETWEEN ? AND ?",
                [s.start, s.end],
            ).fetchall()
        }
        assert scores == {1}


def test_pipeline_duplicate_doubles_the_day_and_effect_is_measured(
    dense_built: BuildResult,
) -> None:
    s = scenario(
        fault_type="pipeline_bug",
        dimension="data_quality",
        value="fct_orders",
        duration_days=1,
        magnitude=1.0,
        mode="duplicate",
    )
    with ScenarioWorkspace(dense_built.warehouse_path) as ws:
        before = count(
            ws, "SELECT count(*) FROM marts_scn.fct_orders WHERE purchase_date = ?", s.start
        )
        ws.inject(s)
        assert (
            count(ws, "SELECT count(*) FROM marts_scn.fct_orders WHERE purchase_date = ?", s.start)
            == 2 * before
        )
        ws.build_metrics()
        effect = measure_effect(ws, s)
    assert effect["effect_start"] == effect["effect_end"] == s.start
    assert effect["total_relative_change"] == pytest.approx(1.0)


def test_scenario_metrics_keep_the_clean_series_plan(dense_built: BuildResult) -> None:
    s = scenario(
        fault_type="segment_level_shift",
        dimension="customer_state",
        value="SP",
        magnitude=0.9,
        mode="drop",
        duration_days=60,
    )
    with ScenarioWorkspace(dense_built.warehouse_path) as ws:
        ws.inject(s)
        ws.build_metrics()
        diff = count(
            ws,
            "SELECT count(*) FROM (SELECT * FROM metrics.series_plan EXCEPT SELECT * FROM wh.metrics.series_plan)",
        )
    assert diff == 0  # a fault must never change which series are monitored


# ---------------------------------------------------------------------------
# Simulated clock
# ---------------------------------------------------------------------------
def test_sim_views_hide_the_future(dense_built: BuildResult) -> None:
    sim = date(2017, 10, 15)
    with ScenarioWorkspace(dense_built.warehouse_path) as ws:
        set_sim_date(ws.con, sim)
        q = lambda sql: ws.con.execute(sql).fetchall()[0][0]  # noqa: E731
        assert q("SELECT max(purchase_date) FROM sim.fct_orders") <= sim
        assert (
            q("SELECT count(*) FROM sim.fct_orders WHERE delivered_date > getvariable('sim_date')")
            == 0
        )
        assert (
            q("SELECT count(*) FROM sim.fct_orders WHERE is_delivered AND delivered_date IS NULL")
            == 0
        )
        assert q("SELECT max(review_date) FROM sim.fct_reviews") <= sim
        assert q("SELECT max(date) FROM sim.dim_date") == sim
        # an order bought before sim_date but delivered after it looks "shipped", not delivered
        pending = q(
            "SELECT count(*) FROM marts_scn.fct_orders WHERE purchase_date <= DATE '2017-10-15' "
            "AND delivered_date > DATE '2017-10-15'"
        )
        shown = q(
            "SELECT count(*) FROM sim.fct_orders o JOIN marts_scn.fct_orders f USING (order_id) "
            "WHERE f.delivered_date > DATE '2017-10-15' AND o.status = 'shipped' AND NOT o.is_delivered"
        )
        assert pending > 0 and shown == pending
        assert (
            q(
                "SELECT count(*) FROM sim.fct_order_items WHERE order_id NOT IN (SELECT order_id FROM sim.fct_orders)"
            )
            == 0
        )


def test_moving_the_clock_reveals_more_data(dense_built: BuildResult) -> None:
    with ScenarioWorkspace(dense_built.warehouse_path) as ws:
        set_sim_date(ws.con, date(2017, 10, 15))
        early = ws.con.execute("SELECT count(*) FROM sim.fct_orders").fetchall()[0][0]
        set_sim_date(ws.con, date(2017, 12, 15))
        late = ws.con.execute("SELECT count(*) FROM sim.fct_orders").fetchall()[0][0]
    assert late > early
