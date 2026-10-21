"""Transformation edge cases from DATA_PLAN.md section 3, on planted synthetic orders."""

from __future__ import annotations

from datetime import date

import duckdb
import pytest

from pipelines.db import first_row
from pipelines.synthetic import EDGE, EDGE_REPEAT_CUSTOMER_UNIQUE_ID

pytestmark = pytest.mark.unit


def one(con: duckdb.DuckDBPyConnection, sql: str, *params: object):
    return first_row(con.execute(sql, list(params)))


def order(con: duckdb.DuckDBPyConnection, key: str) -> dict:
    cursor = con.execute("SELECT * FROM marts.fct_orders WHERE order_id = ?", [EDGE[key]])
    names = [d[0] for d in cursor.description]
    return dict(zip(names, first_row(cursor), strict=True))


def test_every_raw_order_reaches_the_mart_once(con: duckdb.DuckDBPyConnection) -> None:
    raw = one(con, "SELECT count(*) FROM raw.orders")[0]
    mart, distinct = one(con, "SELECT count(*), count(DISTINCT order_id) FROM marts.fct_orders")
    assert mart == distinct == raw


def test_order_without_items_has_no_gmv(con: duckdb.DuckDBPyConnection) -> None:
    row = order(con, "no_items")
    assert row["has_items"] is False
    assert row["item_count"] == 0
    assert row["order_gmv"] is None  # excluded from GMV and AOV, not counted as zero


def test_order_without_payment_is_flagged(con: duckdb.DuckDBPyConnection) -> None:
    row = order(con, "no_payment")
    assert row["has_payment"] is False
    assert row["primary_payment_type"] is None
    assert row["has_items"] is True


def test_delivered_order_without_delivery_date_is_not_delivered(
    con: duckdb.DuckDBPyConnection,
) -> None:
    row = order(con, "delivered_no_date")
    assert row["status"] == "delivered"
    assert row["is_delivered"] is False
    assert row["is_late"] is False
    assert row["delivery_days"] is None


def test_two_reviews_for_one_order_keep_the_latest(con: duckdb.DuckDBPyConnection) -> None:
    rows = con.execute(
        "SELECT review_id, score FROM marts.fct_reviews WHERE order_id = ?", [EDGE["dup_review"]]
    ).fetchall()
    assert rows == [("review_dup_new", 5)]


def test_review_id_shared_by_two_orders_keeps_both(con: duckdb.DuckDBPyConnection) -> None:
    n = one(
        con,
        "SELECT count(*) FROM marts.fct_reviews WHERE review_id = 'review_shared' "
        "AND order_id IN (?, ?)",
        EDGE["shared_review_a"],
        EDGE["shared_review_b"],
    )[0]
    assert n == 2


def test_reviews_are_unique_per_order(con: duckdb.DuckDBPyConnection) -> None:
    assert one(con, "SELECT count(*) - count(DISTINCT order_id) FROM marts.fct_reviews")[0] == 0


@pytest.mark.parametrize("key", ["unknown_category", "null_category"])
def test_missing_or_untranslated_category_becomes_unknown(
    con: duckdb.DuckDBPyConnection, key: str
) -> None:
    category = one(
        con, "SELECT category_en FROM marts.fct_order_items WHERE order_id = ?", EDGE[key]
    )[0]
    assert category == "unknown"


def test_translated_category_is_english(con: duckdb.DuckDBPyConnection) -> None:
    values = {
        r[0] for r in con.execute("SELECT DISTINCT category_en FROM marts.dim_products").fetchall()
    }
    assert {"health_beauty", "toys", "unknown"} <= values
    assert "beleza_saude" not in values  # the Portuguese name must not leak into category_en


def test_primary_payment_is_the_largest_value(con: duckdb.DuckDBPyConnection) -> None:
    row = order(con, "split_payment")
    assert row["primary_payment_type"] == "credit_card"  # not the voucher that came first
    assert row["payment_count"] == 2


def test_not_defined_payment_type_is_kept(con: duckdb.DuckDBPyConnection) -> None:
    assert order(con, "not_defined_payment")["primary_payment_type"] == "not_defined"


def test_late_means_a_later_calendar_date_than_estimated(con: duckdb.DuckDBPyConnection) -> None:
    assert order(con, "late")["is_late"] is True
    # Delivered 23:30 on the estimated day: on time under the date rule.
    on_time = order(con, "on_time")
    assert on_time["is_delivered"] is True
    assert on_time["is_late"] is False


def test_delivery_days_is_fractional_days_from_purchase(con: duckdb.DuckDBPyConnection) -> None:
    # Purchased 2017-06-15 10:00, delivered 2017-06-25 23:30 -> 10 days 13.5 hours.
    assert order(con, "on_time")["delivery_days"] == pytest.approx(10 + 13.5 / 24)


def test_multiline_review_survives_csv_parsing(con: duckdb.DuckDBPyConnection) -> None:
    text = one(
        con,
        "SELECT comment_text FROM marts.fct_reviews WHERE order_id = ?",
        EDGE["multiline_review"],
    )[0]
    assert text == 'Linha um, "entre aspas"\nlinha dois'


def test_review_text_is_truncated_in_the_mart(con: duckdb.DuckDBPyConnection) -> None:
    longest = one(con, "SELECT max(length(comment_text)) FROM marts.fct_reviews")[0]
    assert longest <= 500


def test_raw_layer_is_loaded_unchanged_as_text(con: duckdb.DuckDBPyConnection) -> None:
    types = {r[0]: r[1] for r in con.execute("DESCRIBE raw.order_items").fetchall()}
    assert set(types.values()) == {"VARCHAR"}
    assert "product_name_lenght" in {r[0] for r in con.execute("DESCRIBE raw.products").fetchall()}
    assert "name_length" in {r[0] for r in con.execute("DESCRIBE staging.products").fetchall()}


def test_money_sums_tie_out_exactly(con: duckdb.DuckDBPyConnection) -> None:
    orders_total, items_total = one(
        con,
        "SELECT (SELECT sum(order_gmv) FROM marts.fct_orders), (SELECT sum(price) FROM marts.fct_order_items)",
    )
    assert orders_total == items_total  # DECIMAL arithmetic: exact, no tolerance


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------
def test_customer_identifiers_are_pseudonymised(con: duckdb.DuckDBPyConnection) -> None:
    keys = [r[0] for r in con.execute("SELECT customer_key FROM marts.dim_customers").fetchall()]
    assert all(len(k) == 32 and not k.startswith("unique_") for k in keys)
    refs = [r[0] for r in con.execute("SELECT customer_ref FROM staging.customers").fetchall()]
    assert all(not r.startswith("cust_") for r in refs)


def test_source_identifiers_and_zip_codes_do_not_reach_staging_or_marts(
    con: duckdb.DuckDBPyConnection,
) -> None:
    columns = con.execute(
        "SELECT table_schema || '.' || table_name || '.' || column_name FROM information_schema.columns "
        "WHERE table_schema IN ('staging', 'marts')"
    ).fetchall()
    names = {c[0].split(".")[-1] for c in columns}
    assert not {"customer_id", "customer_unique_id"} & names
    assert not any("zip" in n for n in names)


def test_same_person_gets_one_key_across_orders(con: duckdb.DuckDBPyConnection) -> None:
    rows = con.execute(
        "SELECT order_id, customer_key, customer_state FROM marts.fct_orders "
        "WHERE order_id LIKE 'edge_repeat_%' ORDER BY order_id"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0][1] == rows[1][1]  # same person
    assert [r[2] for r in rows] == ["SP", "RJ"]  # state at the time of each order
    state = one(con, "SELECT state FROM marts.dim_customers WHERE customer_key = ?", rows[0][1])[0]
    assert state == "RJ"  # dim_customers holds the latest known state
    assert EDGE_REPEAT_CUSTOMER_UNIQUE_ID not in rows[0][1]


# ---------------------------------------------------------------------------
# Calendar and seller tier
# ---------------------------------------------------------------------------
def test_dim_date_is_contiguous_and_flags_brazilian_holidays(
    con: duckdb.DuckDBPyConnection,
) -> None:
    first, last, n = one(con, "SELECT min(date), max(date), count(*) FROM marts.dim_date")
    assert n == (last - first).days + 1
    holidays_by_date = dict(
        con.execute("SELECT date, is_holiday_br FROM marts.dim_date").fetchall()
    )
    assert holidays_by_date[date(2017, 9, 7)] is True  # Independence Day
    assert holidays_by_date[date(2017, 12, 25)] is True
    assert holidays_by_date[date(2017, 6, 14)] is False
    assert one(con, "SELECT weekday, is_weekend FROM marts.dim_date WHERE date = '2017-06-17'") == (
        6,
        True,
    )


def test_seller_tier_uses_only_the_past(con: duckdb.DuckDBPyConnection) -> None:
    """Recompute trailing GMV independently and compare; day D must not count towards D."""
    mismatches = one(
        con,
        """
        WITH daily AS (
            SELECT seller_id, purchase_date AS d, sum(price) AS gmv FROM marts.fct_order_items GROUP BY 1, 2
        )
        SELECT count(*) FROM marts.seller_tier_daily t
        WHERE t.trailing_gmv <> coalesce((
            SELECT sum(gmv) FROM daily x
            WHERE x.seller_id = t.seller_id AND x.d >= t.date - INTERVAL 90 DAY AND x.d < t.date
        ), 0)
        """,
    )[0]
    assert mismatches == 0


def test_seller_is_unrated_on_its_first_sale_day(con: duckdb.DuckDBPyConnection) -> None:
    bad = one(
        con,
        """
        WITH first_day AS (SELECT seller_id, min(purchase_date) AS d FROM marts.fct_order_items GROUP BY 1)
        SELECT count(*) FROM first_day f JOIN marts.seller_tier_daily t
          ON t.seller_id = f.seller_id AND t.date = f.d
        WHERE t.tier <> 'unrated' OR t.trailing_gmv <> 0
        """,
    )[0]
    assert bad == 0


def test_seller_tiers_are_quartiles_of_the_day(con: duckdb.DuckDBPyConnection) -> None:
    tiers = {
        r[0] for r in con.execute("SELECT DISTINCT tier FROM marts.seller_tier_daily").fetchall()
    }
    assert tiers == {"T1", "T2", "T3", "T4", "unrated"}
    # On a day with at least 8 rated sellers, quartile sizes differ by at most one seller.
    sizes = con.execute(
        """
        SELECT date, tier, count(*) FROM marts.seller_tier_daily WHERE tier <> 'unrated' GROUP BY 1, 2
        """
    ).fetchall()
    by_day: dict = {}
    for day, tier, n in sizes:
        by_day.setdefault(day, {})[tier] = n
    checked = 0
    for counts in by_day.values():
        if sum(counts.values()) >= 8:
            assert (
                max(counts.values()) - min(counts.get(t, 0) for t in ("T1", "T2", "T3", "T4")) <= 1
            )
            checked += 1
    assert checked > 0
