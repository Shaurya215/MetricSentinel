"""Metric store (FR-011, FR-012): daily and weekly KPI values with sample sizes.

Reads facts from schema `marts_scn` (equal to `marts` normally, faults injected in
evaluation) and static tables (`dim_date`, `seller_tier_daily`) from `marts`. Writes:

  metrics.order_dims    one row per order: its four dimension values
  metrics.daily_kpi     kpi x dimension x segment x day, dense over the monitoring window
  metrics.weekly_kpi    the same, summed by week (weeks end on Sunday; only full weeks)
  metrics.series_plan   which series are monitored and how (daily / weekly / pooled / excluded)

Every value is stored as numerator, denominator and n, so totals, segments, pooled
segments and weeks are exact sums of the same parts.

Small segments (the Phase 2 finding): a segment is monitored daily if enough days meet
the KPI's minimum sample, otherwise weekly if enough weeks do, otherwise it is pooled
into one `__other__` series for its dimension. The plan is computed from CLEAN data
only, so an injected fault can never change which series are monitored.
"""

from __future__ import annotations

import duckdb

from ds.catalogue import Catalogue, DetectionConfig, Kpi, load_catalogue, load_detection_config

OTHER = "__other__"
TOTAL_DIMENSION = "total"
TOTAL_SEGMENT = "all"


def ensure_scenario_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Normal mode: `marts_scn` is a set of views onto `marts` (DATA_PLAN.md section 4)."""
    con.execute("CREATE SCHEMA IF NOT EXISTS marts_scn")
    for table in ("fct_orders", "fct_order_items", "fct_payments", "fct_reviews"):
        con.execute(f"CREATE OR REPLACE VIEW marts_scn.{table} AS SELECT * FROM marts.{table}")


def build_sources(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("CREATE SCHEMA IF NOT EXISTS metrics")
    con.execute(
        """
        CREATE OR REPLACE TABLE metrics.order_dims AS
        WITH primary_item AS (
            SELECT order_id, category_en, seller_id,
                   row_number() OVER (PARTITION BY order_id ORDER BY price DESC, item_seq ASC) AS rn
            FROM marts_scn.fct_order_items
        )
        SELECT
            o.order_id,
            coalesce(o.customer_state, 'unknown')                                   AS customer_state,
            coalesce(o.primary_payment_type, 'no_payment')                          AS payment_type,
            coalesce(p.category_en, 'no_items')                                     AS category_en,
            CASE WHEN p.order_id IS NULL THEN 'no_items'
                 ELSE coalesce(t.tier, 'unrated') END                               AS seller_tier
        FROM marts_scn.fct_orders o
        LEFT JOIN primary_item p ON p.order_id = o.order_id AND p.rn = 1
        LEFT JOIN marts.seller_tier_daily t ON t.seller_id = p.seller_id AND t.date = o.purchase_date
        """
    )
    con.execute(
        """
        CREATE OR REPLACE VIEW metrics.src_orders AS
        SELECT o.*, o.purchase_date AS event_date,
               d.customer_state AS dim_customer_state, d.payment_type AS dim_payment_type,
               d.category_en AS dim_category_en, d.seller_tier AS dim_seller_tier
        FROM marts_scn.fct_orders o JOIN metrics.order_dims d USING (order_id)
        """
    )
    con.execute(
        """
        CREATE OR REPLACE VIEW metrics.src_deliveries AS
        SELECT * REPLACE (delivered_date AS event_date) FROM metrics.src_orders WHERE is_delivered
        """
    )
    con.execute(
        """
        CREATE OR REPLACE VIEW metrics.src_reviews AS
        SELECT r.score, r.review_date AS event_date,
               d.customer_state AS dim_customer_state, d.payment_type AS dim_payment_type,
               d.category_en AS dim_category_en, d.seller_tier AS dim_seller_tier
        FROM marts_scn.fct_reviews r JOIN metrics.order_dims d USING (order_id)
        """
    )


def _kpi_select(kpi: Kpi, dimension: str | None, cfg: DetectionConfig) -> str:
    segment = f"dim_{dimension}" if dimension else f"'{TOTAL_SEGMENT}'"
    denominator = (
        f"CAST({kpi.denominator} AS DOUBLE)" if kpi.denominator else "CAST(NULL AS DOUBLE)"
    )
    where = [
        f"event_date BETWEEN DATE '{cfg.monitoring_window.start}' AND DATE '{cfg.monitoring_window.end}'"
    ]
    if kpi.filter:
        where.append(f"({kpi.filter})")
    return (
        f"SELECT '{kpi.name}' AS kpi, '{dimension or TOTAL_DIMENSION}' AS dimension, "
        f"CAST({segment} AS VARCHAR) AS segment, event_date AS date, "
        f"CAST({kpi.numerator} AS DOUBLE) AS numerator, {denominator} AS denominator, "
        f"CAST({kpi.sample} AS BIGINT) AS n "
        f"FROM metrics.src_{kpi.source} WHERE {' AND '.join(where)} GROUP BY ALL"
    )


def build_daily(con: duckdb.DuckDBPyConnection, catalogue: Catalogue, cfg: DetectionConfig) -> None:
    parts = []
    for kpi in catalogue.kpis:
        parts.append(_kpi_select(kpi, None, cfg))
        parts.extend(_kpi_select(kpi, dim, cfg) for dim in catalogue.dimensions)
    con.execute("CREATE OR REPLACE TEMP TABLE _daily_sparse AS " + " UNION ALL ".join(parts))
    additive = ", ".join(f"'{k.name}'" for k in catalogue.kpis if k.is_additive)
    con.execute(
        f"""
        CREATE OR REPLACE TABLE metrics.daily_kpi AS
        WITH series AS (SELECT DISTINCT kpi, dimension, segment FROM _daily_sparse),
        days AS (
            SELECT CAST(d AS DATE) AS date
            FROM generate_series(DATE '{cfg.monitoring_window.start}', DATE '{cfg.monitoring_window.end}',
                                 INTERVAL 1 DAY) AS t(d)
        )
        SELECT s.kpi, s.dimension, s.segment, days.date,
               coalesce(x.numerator, 0) AS numerator,
               CASE WHEN s.kpi IN ({additive}) THEN NULL ELSE coalesce(x.denominator, 0) END AS denominator,
               coalesce(x.n, 0) AS n
        FROM series s CROSS JOIN days
        LEFT JOIN _daily_sparse x USING (kpi, dimension, segment, date)
        """
    )


def build_series_plan(
    con: duckdb.DuckDBPyConnection, catalogue: Catalogue, cfg: DetectionConfig
) -> None:
    """Decide daily / weekly / pooled / excluded per series. Call on CLEAN data only."""
    min_sample = " ".join(f"WHEN '{k.name}' THEN {k.min_sample}" for k in catalogue.kpis)
    coverage = cfg.sample_rule.coverage
    con.execute(
        f"""
        CREATE OR REPLACE TABLE metrics.series_plan AS
        WITH params AS (
            SELECT kpi, dimension, segment, n, date,
                   date + to_days(7 - isodow(date)) AS week_end,
                   CASE kpi {min_sample} END AS min_n
            FROM metrics.daily_kpi
        ),
        full_weeks AS (
            SELECT week_end FROM params GROUP BY week_end HAVING count(DISTINCT date) = 7
        ),
        weekly AS (
            SELECT kpi, dimension, segment, week_end, sum(n) AS n, any_value(min_n) AS min_n
            FROM params WHERE week_end IN (SELECT week_end FROM full_weeks) GROUP BY ALL
        ),
        daily_cov AS (
            SELECT kpi, dimension, segment, avg(CAST(n >= min_n AS DOUBLE)) AS daily_coverage
            FROM params GROUP BY ALL
        ),
        weekly_cov AS (
            SELECT kpi, dimension, segment, avg(CAST(n >= min_n AS DOUBLE)) AS weekly_coverage
            FROM weekly GROUP BY ALL
        )
        SELECT d.kpi, d.dimension, d.segment, d.daily_coverage, w.weekly_coverage,
               CASE WHEN d.daily_coverage >= {coverage} THEN 'daily'
                    WHEN w.weekly_coverage >= {coverage} THEN 'weekly'
                    WHEN d.dimension = '{TOTAL_DIMENSION}' THEN 'excluded'
                    ELSE 'pooled' END AS granularity
        FROM daily_cov d JOIN weekly_cov w USING (kpi, dimension, segment)
        """
    )
    # The pool of each (kpi, dimension) is itself a series, classified by the same rule.
    _add_other_to_daily(con)
    con.execute(
        f"""
        INSERT INTO metrics.series_plan
        WITH params AS (
            SELECT kpi, dimension, segment, n, date, date + to_days(7 - isodow(date)) AS week_end,
                   CASE kpi {min_sample} END AS min_n
            FROM metrics.daily_kpi WHERE segment = '{OTHER}'
        ),
        full_weeks AS (SELECT week_end FROM params GROUP BY week_end HAVING count(DISTINCT date) = 7),
        weekly AS (
            SELECT kpi, dimension, week_end, sum(n) AS n, any_value(min_n) AS min_n FROM params
            WHERE week_end IN (SELECT week_end FROM full_weeks) GROUP BY ALL
        ),
        d AS (SELECT kpi, dimension, avg(CAST(n >= min_n AS DOUBLE)) AS daily_coverage FROM params GROUP BY ALL),
        w AS (SELECT kpi, dimension, avg(CAST(n >= min_n AS DOUBLE)) AS weekly_coverage FROM weekly GROUP BY ALL)
        SELECT kpi, dimension, '{OTHER}', daily_coverage, weekly_coverage,
               CASE WHEN daily_coverage >= {coverage} THEN 'daily'
                    WHEN weekly_coverage >= {coverage} THEN 'weekly' ELSE 'excluded' END
        FROM d JOIN w USING (kpi, dimension)
        """
    )


def _add_other_to_daily(con: duckdb.DuckDBPyConnection) -> None:
    """Add the `__other__` series: the sum of a dimension's pooled segments, per day."""
    con.execute(f"DELETE FROM metrics.daily_kpi WHERE segment = '{OTHER}'")
    con.execute(
        f"""
        INSERT INTO metrics.daily_kpi
        SELECT k.kpi, k.dimension, '{OTHER}', k.date, sum(k.numerator),
               CASE WHEN bool_and(k.denominator IS NULL) THEN NULL ELSE sum(k.denominator) END, sum(k.n)
        FROM metrics.daily_kpi k
        JOIN metrics.series_plan p USING (kpi, dimension, segment)
        WHERE p.granularity = 'pooled'
        GROUP BY k.kpi, k.dimension, k.date
        """
    )


def build_weekly(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE OR REPLACE TABLE metrics.weekly_kpi AS
        WITH tagged AS (
            SELECT *, date + to_days(7 - isodow(date)) AS week_end FROM metrics.daily_kpi
        ),
        full_weeks AS (SELECT week_end FROM tagged GROUP BY week_end HAVING count(DISTINCT date) = 7)
        SELECT kpi, dimension, segment, week_end AS date, sum(numerator) AS numerator,
               CASE WHEN bool_and(denominator IS NULL) THEN NULL ELSE sum(denominator) END AS denominator,
               sum(n) AS n
        FROM tagged WHERE week_end IN (SELECT week_end FROM full_weeks)
        GROUP BY kpi, dimension, segment, week_end
        """
    )


def build_series_view(con: duckdb.DuckDBPyConnection) -> None:
    """`metrics.series`: every monitored observation, daily and weekly, with its value."""
    con.execute(
        """
        CREATE OR REPLACE VIEW metrics.series AS
        SELECT p.kpi, p.dimension, p.segment, p.granularity, v.date, v.numerator, v.denominator, v.n,
               CASE WHEN v.denominator IS NULL THEN v.numerator
                    WHEN v.denominator = 0 THEN NULL
                    ELSE v.numerator / v.denominator END AS value
        FROM metrics.series_plan p
        JOIN metrics.daily_kpi v USING (kpi, dimension, segment)
        WHERE p.granularity = 'daily'
        UNION ALL
        SELECT p.kpi, p.dimension, p.segment, p.granularity, v.date, v.numerator, v.denominator, v.n,
               CASE WHEN v.denominator IS NULL THEN v.numerator
                    WHEN v.denominator = 0 THEN NULL
                    ELSE v.numerator / v.denominator END AS value
        FROM metrics.series_plan p
        JOIN metrics.weekly_kpi v USING (kpi, dimension, segment)
        WHERE p.granularity = 'weekly'
        """
    )


def build_metric_store(
    con: duckdb.DuckDBPyConnection,
    *,
    catalogue: Catalogue | None = None,
    cfg: DetectionConfig | None = None,
    plan_from: str | None = None,
) -> None:
    """Build every metrics table from `marts_scn`.

    `plan_from` names an existing `series_plan` table to copy (scenario mode: the plan
    must come from clean data). When it is None the plan is computed here, which is
    only correct when `marts_scn` holds clean data.
    """
    catalogue = catalogue or load_catalogue()
    cfg = cfg or load_detection_config()
    build_sources(con)
    build_daily(con, catalogue, cfg)
    if plan_from is None:
        build_series_plan(con, catalogue, cfg)
    else:
        con.execute(f"CREATE OR REPLACE TABLE metrics.series_plan AS SELECT * FROM {plan_from}")
        _add_other_to_daily(con)
    build_weekly(con)
    build_series_view(con)


def main() -> None:
    from core.logging import configure_logging, get_logger, run_context
    from core.settings import get_settings

    configure_logging()
    log = get_logger(__name__)
    with run_context(component="metric_store"):
        con = duckdb.connect(str(get_settings().duckdb_path))
        try:
            ensure_scenario_schema(con)
            build_metric_store(con)
            summary = con.execute(
                "SELECT granularity, count(*) FROM metrics.series_plan GROUP BY 1 ORDER BY 1"
            ).fetchall()
        finally:
            con.close()
        log.info("metric_store_built", series_plan=dict(summary))
        print("Series plan:", dict(summary))


if __name__ == "__main__":
    main()
