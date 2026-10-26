"""Fault injector and scenario benchmark (FR-020 to FR-023, DATA_PLAN.md section 10).

Faults are injected into `marts_scn`, a private in-memory copy of the fact tables.
The warehouse is attached READ-ONLY, so `raw` and `marts` can never be modified
(FR-020). Selection of affected rows is a deterministic hash of the order id and the
scenario seed, so every scenario is reproducible.

Ground truth (FR-022). Each scenario records its type, the KPIs it should move, the
dimension and value it targets, its start date and magnitude. After injection the
workspace also MEASURES the effect: which days of the affected KPIs actually changed
(`effect_start`, `effect_end`) and how large the relative change is in the targeted
segment and in the total. Detection is scored against these measured values.

    uv run python -m pipelines.faults generate      # writes config/scenarios/mvp.yaml
"""

from __future__ import annotations

import argparse
import random
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

import duckdb
import yaml
from pydantic import BaseModel, ConfigDict, Field

from core.settings import REPO_ROOT, get_settings
from ds.catalogue import load_detection_config
from ds.metric_store import build_metric_store

FaultType = Literal[
    "segment_level_shift",
    "seller_delay",
    "payment_failure",
    "category_price_change",
    "review_collapse",
    "pipeline_bug",
]
FACT_TABLES = ("fct_orders", "fct_order_items", "fct_payments", "fct_reviews")
MART_TABLES = (
    *FACT_TABLES,
    "dim_customers",
    "dim_sellers",
    "dim_products",
    "dim_date",
    "seller_tier_daily",
)
SCENARIOS_PATH = REPO_ROOT / "config" / "scenarios" / "mvp.yaml"

AFFECTED_KPIS: dict[str, list[str]] = {
    "segment_level_shift": ["orders", "gmv"],
    "seller_delay": ["avg_delivery_days", "late_delivery_rate"],
    "payment_failure": ["orders", "gmv"],
    "category_price_change": ["aov", "gmv", "freight_ratio"],
    "review_collapse": ["avg_review_score", "low_review_rate"],
    "pipeline_bug": ["orders", "gmv"],
}


class Scenario(BaseModel):
    """One labelled fault. Fields up to `affected_kpis` are the ground-truth label (FR-022)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    fault_type: FaultType
    dimension: str  # customer_state | category_en | payment_type | seller_state | data_quality
    value: str
    start: date
    duration_days: int = Field(ge=1)
    magnitude: float = Field(gt=0)  # share of rows, price cut, or delay in days (seller_delay)
    mode: Literal["drop", "duplicate"] | None = None
    seed: int
    affected_kpis: list[str]
    size_tier: str = ""  # large / medium / small target segment, for analysis
    target_share: float | None = None  # target's share of orders in the benchmark period (3b)

    @property
    def end(self) -> date:
        return self.start + timedelta(days=self.duration_days - 1)


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------
class ScenarioWorkspace:
    """An in-memory DuckDB with the warehouse attached read-only and a private marts_scn."""

    def __init__(self, warehouse: Path | None = None) -> None:
        self.warehouse = warehouse or get_settings().duckdb_path
        self.con = duckdb.connect()
        self.con.execute(f"ATTACH '{self.warehouse.as_posix()}' AS wh (READ_ONLY)")
        self.con.execute("CREATE SCHEMA marts")
        for table in MART_TABLES:
            self.con.execute(f"CREATE VIEW marts.{table} AS SELECT * FROM wh.marts.{table}")
        self.con.execute("CREATE SCHEMA marts_scn")
        for table in FACT_TABLES:
            self.con.execute(f"CREATE TABLE marts_scn.{table} AS SELECT * FROM wh.marts.{table}")
        self.con.execute("CREATE SCHEMA meta")
        self.con.execute("CREATE TABLE meta.build AS SELECT * FROM wh.meta.build")

    def __enter__(self) -> ScenarioWorkspace:
        return self

    def __exit__(self, *exc: object) -> None:
        self.con.close()

    def inject(self, scenario: Scenario) -> None:
        INJECTORS[scenario.fault_type](self.con, scenario)

    def build_metrics(self) -> None:
        build_metric_store(self.con, plan_from="wh.metrics.series_plan")


# ---------------------------------------------------------------------------
# Row selection helpers
# ---------------------------------------------------------------------------
def _picked(alias: str, scenario: Scenario, share: float) -> str:
    """Deterministic pseudo-random choice of `share` of rows, keyed on order id and seed."""
    return f"(md5_number({alias}.order_id || ':{scenario.seed}') % 10000) < {round(share * 10000)}"


def _window(alias: str, column: str, scenario: Scenario) -> str:
    return f"{alias}.{column} BETWEEN DATE '{scenario.start}' AND DATE '{scenario.end}'"


def _order_ids_in_segment(scenario: Scenario, share: float) -> str:
    """Orders purchased in the window whose (clean) dimension value matches, sampled by share."""
    return (
        f"SELECT o.order_id FROM marts_scn.fct_orders o JOIN wh.metrics.order_dims d USING (order_id) "
        f"WHERE {_window('o', 'purchase_date', scenario)} AND d.{scenario.dimension} = '{scenario.value}' "
        f"AND {_picked('o', scenario, share)}"
    )


def _delete_orders(con: duckdb.DuckDBPyConnection, ids_sql: str) -> None:
    con.execute(f"CREATE OR REPLACE TEMP TABLE _hit AS {ids_sql}")
    for table in FACT_TABLES:
        con.execute(f"DELETE FROM marts_scn.{table} WHERE order_id IN (SELECT order_id FROM _hit)")


def _duplicate_orders(con: duckdb.DuckDBPyConnection, ids_sql: str) -> None:
    con.execute(f"CREATE OR REPLACE TEMP TABLE _hit AS {ids_sql}")
    for table in FACT_TABLES:
        con.execute(
            f"INSERT INTO marts_scn.{table} SELECT * REPLACE (order_id || '#dup' AS order_id) "
            f"FROM marts_scn.{table} WHERE order_id IN (SELECT order_id FROM _hit)"
        )


# ---------------------------------------------------------------------------
# The six fault types (FR-021)
# ---------------------------------------------------------------------------
def segment_level_shift(con: duckdb.DuckDBPyConnection, s: Scenario) -> None:
    ids = _order_ids_in_segment(s, s.magnitude)
    (_duplicate_orders if s.mode == "duplicate" else _delete_orders)(con, ids)


def payment_failure(con: duckdb.DuckDBPyConnection, s: Scenario) -> None:
    """A share of orders with one payment type fails and is never placed."""
    _delete_orders(con, _order_ids_in_segment(s, s.magnitude))


def seller_delay(con: duckdb.DuckDBPyConnection, s: Scenario) -> None:
    """Deliveries (originally due in the window) from sellers in one state arrive N days later."""
    days = int(s.magnitude)
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE _hit AS
        WITH primary_seller AS (
            SELECT order_id, seller_id,
                   row_number() OVER (PARTITION BY order_id ORDER BY price DESC, item_seq) AS rn
            FROM marts_scn.fct_order_items
        )
        SELECT o.order_id FROM marts_scn.fct_orders o
        JOIN primary_seller p ON p.order_id = o.order_id AND p.rn = 1
        JOIN marts.dim_sellers sl ON sl.seller_id = p.seller_id
        WHERE o.is_delivered AND {_window("o", "delivered_date", s)} AND sl.state = '{s.value}'
        """
    )
    con.execute(
        f"""
        UPDATE marts_scn.fct_orders SET
            delivered_date = delivered_date + INTERVAL {days} DAY,
            delivery_days = delivery_days + {days},
            is_late = (delivered_date + INTERVAL {days} DAY) > estimated_date
        WHERE order_id IN (SELECT order_id FROM _hit)
        """
    )


def category_price_change(con: duckdb.DuckDBPyConnection, s: Scenario) -> None:
    """Prices in one category are cut by `magnitude` (a pricing bug); order GMV follows."""
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE _hit AS
        SELECT DISTINCT i.order_id FROM marts_scn.fct_order_items i
        WHERE i.category_en = '{s.value}' AND {_window("i", "purchase_date", s)}
        """
    )
    con.execute(
        f"""
        UPDATE marts_scn.fct_order_items
        SET price = CAST(round(price * {1 - s.magnitude}, 2) AS DECIMAL(12, 2))
        WHERE category_en = '{s.value}' AND order_id IN (SELECT order_id FROM _hit)
        """
    )
    con.execute(
        """
        UPDATE marts_scn.fct_orders o SET order_gmv = t.gmv
        FROM (SELECT order_id, sum(price) AS gmv FROM marts_scn.fct_order_items
              WHERE order_id IN (SELECT order_id FROM _hit) GROUP BY order_id) t
        WHERE o.order_id = t.order_id
        """
    )


def review_collapse(con: duckdb.DuckDBPyConnection, s: Scenario) -> None:
    """A share of reviews created in the window, for one segment, become score 1."""
    con.execute(
        f"""
        UPDATE marts_scn.fct_reviews r SET score = 1
        WHERE {_window("r", "review_date", s)} AND {_picked("r", s, s.magnitude)}
          AND r.order_id IN (SELECT order_id FROM wh.metrics.order_dims WHERE {s.dimension} = '{s.value}')
        """
    )


def pipeline_bug(con: duckdb.DuckDBPyConnection, s: Scenario) -> None:
    """One day's orders are duplicated or partly dropped by a faulty load."""
    ids = (
        f"SELECT o.order_id FROM marts_scn.fct_orders o "
        f"WHERE {_window('o', 'purchase_date', s)} AND {_picked('o', s, s.magnitude)}"
    )
    (_duplicate_orders if s.mode == "duplicate" else _delete_orders)(con, ids)


INJECTORS = {
    "segment_level_shift": segment_level_shift,
    "seller_delay": seller_delay,
    "payment_failure": payment_failure,
    "category_price_change": category_price_change,
    "review_collapse": review_collapse,
    "pipeline_bug": pipeline_bug,
}


# ---------------------------------------------------------------------------
# Measured ground truth
# ---------------------------------------------------------------------------
def measure_effect(ws: ScenarioWorkspace, s: Scenario) -> dict[str, Any]:
    """Compare the scenario's metrics with the clean ones for the affected KPIs."""
    kpis = ", ".join(f"'{k}'" for k in s.affected_kpis)
    changed = ws.con.execute(
        f"""
        SELECT min(c.date), max(c.date), count(DISTINCT c.date)
        FROM metrics.daily_kpi c JOIN wh.metrics.daily_kpi k USING (kpi, dimension, segment, date)
        WHERE c.kpi IN ({kpis}) AND c.dimension = 'total'
          AND (abs(c.numerator - k.numerator) > 1e-9 OR c.n <> k.n
               OR abs(coalesce(c.denominator, 0) - coalesce(k.denominator, 0)) > 1e-9)
        """
    ).fetchone()
    primary = s.affected_kpis[0]
    start, end = (changed[0], changed[1]) if changed and changed[0] else (s.start, s.end)

    def rel(dimension: str, segment: str) -> float | None:
        row = ws.con.execute(
            """
            SELECT sum(c.numerator), sum(c.denominator), sum(k.numerator), sum(k.denominator)
            FROM metrics.daily_kpi c JOIN wh.metrics.daily_kpi k USING (kpi, dimension, segment, date)
            WHERE c.kpi = ? AND c.dimension = ? AND c.segment = ? AND c.date BETWEEN ? AND ?
            """,
            [primary, dimension, segment, start, end],
        ).fetchone()
        if not row or row[2] in (None, 0):
            return None
        scn = row[0] / row[1] if row[1] else row[0]
        clean = row[2] / row[3] if row[3] else row[2]
        return None if not clean else round((scn - clean) / clean, 4)

    segment_change = (
        rel(s.dimension, s.value)
        if s.dimension in ("customer_state", "category_en", "payment_type")
        else None
    )
    return {
        "effect_start": start,
        "effect_end": end,
        "effect_days": int(changed[2]) if changed and changed[2] else 0,
        "segment_relative_change": segment_change,
        "total_relative_change": rel("total", "all"),
    }


# ---------------------------------------------------------------------------
# Benchmark generation (FR-023)
# ---------------------------------------------------------------------------
def _segments_by_size(
    con: duckdb.DuckDBPyConnection, column: str, start: date, end: date
) -> list[str]:
    rows = con.execute(
        f"SELECT d.{column} FROM metrics.order_dims d JOIN marts.fct_orders o USING (order_id) "
        f"WHERE o.purchase_date BETWEEN ? AND ? AND d.{column} NOT IN ('no_items', 'no_payment', 'unknown') "
        f"GROUP BY 1 ORDER BY count(*) DESC, 1",
        [start, end],
    ).fetchall()
    return [r[0] for r in rows]


def _seller_states(con: duckdb.DuckDBPyConnection, start: date, end: date) -> list[str]:
    rows = con.execute(
        "SELECT s.state FROM marts.fct_order_items i JOIN marts.dim_sellers s USING (seller_id) "
        "WHERE i.purchase_date BETWEEN ? AND ? GROUP BY 1 ORDER BY count(*) DESC, 1",
        [start, end],
    ).fetchall()
    return [r[0] for r in rows]


def _tiered(ranked: list[str], tier: str) -> list[str]:
    bands = {"large": ranked[:3], "medium": ranked[3:10], "small": ranked[10:25]}
    return bands[tier] or ranked[:3]


def generate_benchmark(
    con: duckdb.DuckDBPyConnection, *, per_type: int = 5, seed: int = 42
) -> list[Scenario]:
    """Stratified across fault types, magnitudes and target-segment sizes, inside the evaluation period."""
    cfg = load_detection_config()
    rng = random.Random(seed)
    ev = cfg.evaluation
    latest_start = ev.end - timedelta(days=45)  # room for delayed deliveries and a weekly check
    states = _segments_by_size(con, "customer_state", ev.start, ev.end)
    categories = _segments_by_size(con, "category_en", ev.start, ev.end)
    payments = [
        p for p in _segments_by_size(con, "payment_type", ev.start, ev.end) if p != "not_defined"
    ]
    seller_states = _seller_states(con, ev.start, ev.end)
    tiers = ["large", "medium", "small", "medium", "large", "small", "large", "medium"]
    shares = [0.1, 0.2, 0.3, 0.5, 0.7, 0.4, 0.15, 0.6]
    delays = [2, 4, 6, 10, 15, 8, 3, 12]
    scenarios: list[Scenario] = []

    def start_day() -> date:
        span = (latest_start - (ev.start + timedelta(days=14))).days
        return ev.start + timedelta(days=14 + rng.randrange(span))

    def add(
        fault: FaultType,
        dimension: str,
        value: str,
        magnitude: float,
        *,
        duration: int,
        mode: Literal["drop", "duplicate"] | None = None,
        tier: str = "",
    ) -> None:
        idx = len(scenarios) + 1
        scenarios.append(
            Scenario(
                id=f"S{idx:02d}-{fault}",
                fault_type=fault,
                dimension=dimension,
                value=value,
                start=start_day(),
                duration_days=duration,
                magnitude=magnitude,
                mode=mode,
                seed=seed * 1000 + idx,
                affected_kpis=AFFECTED_KPIS[fault],
                size_tier=tier,
            )
        )

    for i in range(per_type):
        tier = tiers[i % len(tiers)]
        dim, ranked = ("customer_state", states) if i % 2 == 0 else ("category_en", categories)
        add(
            "segment_level_shift",
            dim,
            rng.choice(_tiered(ranked, tier)),
            shares[i],
            duration=rng.choice([7, 14, 21]),
            mode="drop",
            tier=tier,
        )
    for i in range(per_type):
        tier = tiers[i % len(tiers)]
        add(
            "seller_delay",
            "seller_state",
            rng.choice(_tiered(seller_states, tier)),
            float(delays[i]),
            duration=14,
            tier=tier,
        )
    for i in range(per_type):
        value = payments[i % len(payments)]
        add(
            "payment_failure",
            "payment_type",
            value,
            shares[i],
            duration=rng.choice([7, 14, 21]),
            tier="large" if payments.index(value) < 2 else "small",
        )
    for i in range(per_type):
        tier = tiers[i % len(tiers)]
        add(
            "category_price_change",
            "category_en",
            rng.choice(_tiered(categories, tier)),
            shares[i],
            duration=rng.choice([7, 14, 21]),
            tier=tier,
        )
    for i in range(per_type):
        tier = tiers[i % len(tiers)]
        add(
            "review_collapse",
            "category_en",
            rng.choice(_tiered(categories, tier)),
            shares[i],
            duration=rng.choice([7, 14, 21]),
            tier=tier,
        )
    bug_modes: list[tuple[Literal["drop", "duplicate"], float]] = [
        ("duplicate", 1.0),
        ("drop", 0.5),
        ("drop", 1.0),
        ("duplicate", 0.3),
        ("drop", 0.3),
    ]
    for i in range(per_type):
        mode, share = bug_modes[i % len(bug_modes)]
        add(
            "pipeline_bug", "data_quality", "fct_orders", share, duration=1, mode=mode, tier="total"
        )
    return scenarios


def save_scenarios(
    scenarios: list[Scenario],
    path: Path,
    *,
    seed: int,
    data_hash: str,
    description: str = "MVP fault benchmark (FR-023). Generated by `python -m pipelines.faults generate`.",
) -> None:
    payload = {
        "description": description,
        "seed": seed,
        "generated_from_data_hash": data_hash,
        "scenarios": [s.model_dump(mode="json", exclude_none=True) for s in scenarios],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")


def load_scenarios(path: Path = SCENARIOS_PATH) -> list[Scenario]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [Scenario.model_validate(s) for s in data["scenarios"]]


def main() -> None:
    parser = argparse.ArgumentParser(description="Fault benchmark tools.")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate", help="write the stratified scenario benchmark")
    gen.add_argument("--per-type", type=int, default=5)
    gen.add_argument("--seed", type=int, default=42)
    gen.add_argument("--out", type=Path, default=SCENARIOS_PATH)
    args = parser.parse_args()

    con = duckdb.connect(str(get_settings().duckdb_path), read_only=True)
    try:
        scenarios = generate_benchmark(con, per_type=args.per_type, seed=args.seed)
        data_hash = con.execute("SELECT value FROM meta.build WHERE key = 'data_hash'").fetchall()[
            0
        ][0]
    finally:
        con.close()
    save_scenarios(scenarios, args.out, seed=args.seed, data_hash=data_hash)
    print(f"Wrote {len(scenarios)} scenarios to {args.out}")


if __name__ == "__main__":
    main()
