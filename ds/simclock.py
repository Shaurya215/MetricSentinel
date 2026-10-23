"""Simulated clock (FR-007): only data up to the simulated date is visible.

Olist is historical, so "today" is a setting. `set_sim_date` creates a `sim` schema of
views over `marts_scn` that show the world as it was known at the end of `sim_date`:

  * orders, items and payments: only orders purchased on or before sim_date
  * delivery facts: hidden (NULL / not delivered) when the delivery happened later
  * approval timestamps: hidden when they happened later
  * reviews: only reviews created on or before sim_date
  * seller tiers, calendar and metric series: only dates on or before sim_date

Limitation (documented in DATA_PLAN.md section 14): the order *status* is the final
snapshot. A "delivered" order whose delivery is still in the future is shown as
"shipped", but a cancellation that happened after sim_date cannot be undone, because
the source has no status history.

The date lives in a DuckDB session variable, so the views always read the current
clock of the connection that queries them (Phase 4's agent tools use these views).
"""

from __future__ import annotations

from datetime import date

import duckdb

SIM = "getvariable('sim_date')"


def set_sim_date(con: duckdb.DuckDBPyConnection, sim_date: date) -> None:
    con.execute(f"SET VARIABLE sim_date = DATE '{sim_date.isoformat()}'")
    con.execute("CREATE SCHEMA IF NOT EXISTS sim")
    con.execute(
        f"""
        CREATE OR REPLACE VIEW sim.fct_orders AS
        SELECT * REPLACE (
            CASE WHEN status = 'delivered' AND (delivered_date IS NULL OR delivered_date > {SIM})
                 THEN 'shipped' ELSE status END                                   AS status,
            CASE WHEN delivered_date <= {SIM} THEN delivered_date END              AS delivered_date,
            CASE WHEN CAST(approved_at AS DATE) <= {SIM} THEN approved_at END      AS approved_at,
            coalesce(is_delivered AND delivered_date <= {SIM}, false)              AS is_delivered,
            coalesce(is_late AND delivered_date <= {SIM}, false)                   AS is_late,
            CASE WHEN delivered_date <= {SIM} THEN delivery_days END               AS delivery_days
        )
        FROM marts_scn.fct_orders
        WHERE purchase_date <= {SIM}
        """
    )
    for table in ("fct_order_items", "fct_payments"):
        con.execute(
            f"CREATE OR REPLACE VIEW sim.{table} AS SELECT t.* FROM marts_scn.{table} t "
            f"WHERE t.order_id IN (SELECT order_id FROM sim.fct_orders)"
        )
    con.execute(
        f"CREATE OR REPLACE VIEW sim.fct_reviews AS SELECT * FROM marts_scn.fct_reviews "
        f"WHERE review_date <= {SIM}"
    )
    con.execute(
        f"CREATE OR REPLACE VIEW sim.seller_tier_daily AS SELECT * FROM marts.seller_tier_daily "
        f"WHERE date <= {SIM}"
    )
    con.execute(
        f"CREATE OR REPLACE VIEW sim.dim_date AS SELECT * FROM marts.dim_date WHERE date <= {SIM}"
    )
    for table in ("dim_customers", "dim_sellers", "dim_products"):
        con.execute(f"CREATE OR REPLACE VIEW sim.{table} AS SELECT * FROM marts.{table}")
    if _has_table(con, "metrics", "series_plan"):
        con.execute(
            f"CREATE OR REPLACE VIEW sim.series AS SELECT * FROM metrics.series WHERE date <= {SIM}"
        )


def get_sim_date(con: duckdb.DuckDBPyConnection) -> date | None:
    row = con.execute("SELECT getvariable('sim_date')").fetchone()
    return row[0] if row else None


def _has_table(con: duckdb.DuckDBPyConnection, schema: str, table: str) -> bool:
    row = con.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_catalog = current_database() AND table_schema = ? AND table_name = ?",
        [schema, table],
    ).fetchone()
    return bool(row and row[0])
