"""Soft checks, statistics, mart integrity checks, hashes and the quality report (FR-006).

Levels:
  hard  failing stops the build (raw checks live in validate.py; mart checks are here)
  soft  failing is reported and flagged, the build continues
  stat  a number worth recording, with no pass or fail
"""

from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path
from typing import Any

import duckdb

from pipelines.config import (
    MONITORING_WINDOW,
    SOFT_MAX_DELIVERED_MISSING_DATE_RATE,
    SOFT_MAX_UNKNOWN_CATEGORY_SHARE,
    SQL_DIR,
)
from pipelines.db import first_row
from pipelines.validate import CheckResult

MART_TABLES = (
    "fct_orders",
    "fct_order_items",
    "fct_payments",
    "fct_reviews",
    "dim_customers",
    "dim_sellers",
    "dim_products",
    "dim_date",
    "seller_tier_daily",
)


def _one(con: duckdb.DuckDBPyConnection, sql: str) -> Any:
    return first_row(con.execute(sql))[0]


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


# ---------------------------------------------------------------------------
# Soft checks and statistics
# ---------------------------------------------------------------------------
def soft_checks(con: duckdb.DuckDBPyConnection) -> list[CheckResult]:
    results: list[CheckResult] = []

    delivered, missing = first_row(
        con.execute(
            "SELECT count(*) FILTER (WHERE status = 'delivered'), "
            "count(*) FILTER (WHERE status = 'delivered' AND delivered_ts IS NULL) FROM staging.orders"
        )
    )
    rate = _rate(missing, delivered)
    results.append(
        CheckResult(
            name="delivered_orders_missing_delivery_date_rate",
            level="soft",
            table="staging.orders",
            passed=rate <= SOFT_MAX_DELIVERED_MISSING_DATE_RATE,
            observed=round(rate, 6),
            threshold=f"<= {SOFT_MAX_DELIVERED_MISSING_DATE_RATE}",
            detail=f"{missing} of {delivered} delivered orders have no delivery date; "
            "they are excluded from delivery KPIs",
        )
    )

    items, unknown = first_row(
        con.execute(
            "SELECT count(*), count(*) FILTER (WHERE category_en = 'unknown') FROM marts.fct_order_items"
        )
    )
    share = _rate(unknown, items)
    results.append(
        CheckResult(
            name="unknown_category_item_share",
            level="soft",
            table="marts.fct_order_items",
            passed=share <= SOFT_MAX_UNKNOWN_CATEGORY_SHARE,
            observed=round(share, 6),
            threshold=f"<= {SOFT_MAX_UNKNOWN_CATEGORY_SHARE}",
            detail=f"{unknown} of {items} items map to category 'unknown'",
        )
    )

    start, end = MONITORING_WINDOW
    gaps = con.execute(
        "WITH window_days AS ("
        "  SELECT date FROM marts.dim_date WHERE date BETWEEN ? AND ?), "
        "daily AS (SELECT purchase_date AS date, count(*) AS n FROM marts.fct_orders GROUP BY 1) "
        "SELECT w.date FROM window_days w LEFT JOIN daily d USING (date) "
        "WHERE coalesce(d.n, 0) = 0 ORDER BY w.date",
        [start, end],
    ).fetchall()
    results.append(
        CheckResult(
            name="no_gaps_in_daily_order_counts",
            level="soft",
            table="marts.fct_orders",
            passed=len(gaps) == 0,
            observed=len(gaps),
            threshold=f"0 days without orders in {start}..{end}",
            detail=f"{len(gaps)} day(s) with zero orders inside the monitoring window",
            examples=[str(g[0]) for g in gaps[:5]],
        )
    )

    for what, column, parent in (
        ("product", "product_id", "products"),
        ("seller", "seller_id", "sellers"),
    ):
        orphans = _one(
            con,
            f"SELECT count(*) FROM staging.order_items i WHERE NOT EXISTS "
            f"(SELECT 1 FROM staging.{parent} p WHERE p.{column} = i.{column})",
        )
        results.append(
            CheckResult(
                name=f"items_reference_known_{what}s",
                level="soft",
                table="staging.order_items",
                passed=orphans == 0,
                observed=orphans,
                threshold=0,
                detail=f"{orphans} item row(s) reference an unknown {what}",
            )
        )
    return results


def statistics(con: duckdb.DuckDBPyConnection) -> list[CheckResult]:
    """Numbers that describe the data issues listed in DATA_PLAN.md section 3."""
    total_orders = _one(con, "SELECT count(*) FROM marts.fct_orders")

    def stat(
        name: str, table: str, sql: str, detail: str, rate_of: int | None = None
    ) -> CheckResult:
        value = _one(con, sql)
        text = f"{detail}: {value}"
        if rate_of:
            text += f" ({_rate(value, rate_of):.4%} of {rate_of})"
        return CheckResult(
            name=name, level="stat", table=table, passed=None, observed=value, detail=text
        )

    return [
        stat(
            "orders_without_items",
            "marts.fct_orders",
            "SELECT count(*) FROM marts.fct_orders WHERE NOT has_items",
            "orders with no items (excluded from GMV and AOV)",
            total_orders,
        ),
        stat(
            "orders_without_payment",
            "marts.fct_orders",
            "SELECT count(*) FROM marts.fct_orders WHERE NOT has_payment",
            "orders with no payment",
            total_orders,
        ),
        stat(
            "delivered_status_without_delivery_date",
            "marts.fct_orders",
            "SELECT count(*) FROM marts.fct_orders WHERE status = 'delivered' AND NOT is_delivered",
            "orders with status delivered but no delivery date",
            total_orders,
        ),
        stat(
            "reviews_raw_rows",
            "raw.order_reviews",
            "SELECT count(*) FROM raw.order_reviews",
            "review rows in the source",
        ),
        stat(
            "reviews_after_dedup",
            "staging.order_reviews",
            "SELECT count(*) FROM staging.order_reviews",
            "reviews kept (one per order)",
        ),
        stat(
            "orders_with_multiple_reviews",
            "raw.order_reviews",
            "SELECT count(*) FROM (SELECT order_id FROM raw.order_reviews GROUP BY order_id HAVING count(*) > 1)",
            "orders with more than one review row",
        ),
        stat(
            "review_ids_shared_across_orders",
            "raw.order_reviews",
            "SELECT count(*) FROM (SELECT review_id FROM raw.order_reviews "
            "GROUP BY review_id HAVING count(DISTINCT order_id) > 1)",
            "review_id values reused for different orders",
        ),
        stat(
            "products_with_null_category",
            "staging.products",
            "SELECT count(*) FROM staging.products WHERE category_pt IS NULL",
            "products with no category",
        ),
        stat(
            "products_with_untranslated_category",
            "staging.products",
            "SELECT count(*) FROM staging.products p WHERE category_pt IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM staging.category_translation t WHERE t.category_pt = p.category_pt)",
            "products whose category has no English translation",
        ),
        stat(
            "payments_not_defined",
            "staging.order_payments",
            "SELECT count(*) FROM staging.order_payments WHERE payment_type = 'not_defined'",
            "payment rows with type not_defined",
        ),
        stat(
            "distinct_customer_keys",
            "marts.dim_customers",
            "SELECT count(*) FROM marts.dim_customers",
            "distinct customers (people)",
        ),
    ]


# ---------------------------------------------------------------------------
# Hard checks on the built marts
# ---------------------------------------------------------------------------
def mart_checks(con: duckdb.DuckDBPyConnection) -> list[CheckResult]:
    results: list[CheckResult] = []

    def check(name: str, table: str, bad: int, detail: str) -> None:
        results.append(
            CheckResult(
                name=name,
                level="hard",
                table=table,
                passed=bad == 0,
                observed=bad,
                threshold=0,
                detail=detail if bad else "ok",
            )
        )

    keys = {
        "fct_orders": "order_id",
        "fct_order_items": "order_id, item_seq",
        "fct_payments": "order_id, seq",
        "fct_reviews": "order_id",
        "dim_customers": "customer_key",
        "dim_sellers": "seller_id",
        "dim_products": "product_id",
        "dim_date": "date",
        "seller_tier_daily": "seller_id, date",
    }
    for table, key in keys.items():
        duplicates = _one(
            con,
            f"SELECT count(*) - count(DISTINCT ({key})) FROM marts.{table}"
            if "," not in key
            else f"SELECT count(*) - (SELECT count(*) FROM (SELECT DISTINCT {key} FROM marts.{table})) "
            f"FROM marts.{table}",
        )
        check(
            f"unique_key:{table}",
            f"marts.{table}",
            duplicates,
            f"{duplicates} duplicate key(s) on ({key})",
        )

    check(
        "fct_orders_row_count_matches_raw",
        "marts.fct_orders",
        abs(
            _one(con, "SELECT count(*) FROM marts.fct_orders")
            - _one(con, "SELECT count(*) FROM raw.orders")
        ),
        "fct_orders and raw.orders differ in row count",
    )
    check(
        "gmv_ties_out",
        "marts.fct_orders",
        int(
            _one(
                con,
                "SELECT CAST(abs(coalesce((SELECT sum(order_gmv) FROM marts.fct_orders), 0) - "
                "coalesce((SELECT sum(price) FROM marts.fct_order_items), 0)) AS DOUBLE)",
            )
            > 0
        ),
        "sum(order_gmv) differs from sum(item price)",
    )
    check(
        "freight_ties_out",
        "marts.fct_orders",
        int(
            _one(
                con,
                "SELECT CAST(abs(coalesce((SELECT sum(freight_total) FROM marts.fct_orders), 0) - "
                "coalesce((SELECT sum(freight) FROM marts.fct_order_items), 0)) AS DOUBLE)",
            )
            > 0
        ),
        "sum(freight_total) differs from sum(item freight)",
    )
    check(
        "items_have_category",
        "marts.fct_order_items",
        _one(con, "SELECT count(*) FROM marts.fct_order_items WHERE category_en IS NULL"),
        "items with a NULL category (should be 'unknown')",
    )
    check(
        "orders_have_customer_state",
        "marts.fct_orders",
        _one(con, "SELECT count(*) FROM marts.fct_orders WHERE customer_state IS NULL"),
        "orders with no customer state",
    )
    check(
        "late_implies_delivered",
        "marts.fct_orders",
        _one(con, "SELECT count(*) FROM marts.fct_orders WHERE is_late AND NOT is_delivered"),
        "orders flagged late but not delivered",
    )
    check(
        "dates_covered_by_dim_date",
        "marts.dim_date",
        _one(
            con,
            "SELECT count(*) FROM marts.fct_orders o WHERE NOT EXISTS "
            "(SELECT 1 FROM marts.dim_date d WHERE d.date = o.purchase_date)",
        ),
        "purchase dates missing from dim_date",
    )
    return results


# ---------------------------------------------------------------------------
# Table profiles
# ---------------------------------------------------------------------------
def table_profiles(con: duckdb.DuckDBPyConnection, schemas: tuple[str, ...]) -> dict[str, Any]:
    """Row count and null rate per column, for every table in the given schemas."""
    profiles: dict[str, Any] = {}
    tables = con.execute(
        "SELECT table_schema, table_name FROM information_schema.tables "
        f"WHERE table_schema IN ({', '.join('?' * len(schemas))}) ORDER BY 1, 2",
        list(schemas),
    ).fetchall()
    for schema, table in tables:
        if table.startswith("_"):
            continue
        columns = [c[0] for c in con.execute(f"DESCRIBE {schema}.{table}").fetchall()]
        exprs = ", ".join(f'count(*) FILTER (WHERE "{c}" IS NULL)' for c in columns)
        row = first_row(con.execute(f"SELECT count(*), {exprs} FROM {schema}.{table}"))
        rows = row[0]
        profiles[f"{schema}.{table}"] = {
            "rows": rows,
            "null_rate": {
                c: round(_rate(n, rows), 6) for c, n in zip(columns, row[1:], strict=True)
            },
        }
    return profiles


# ---------------------------------------------------------------------------
# Hashes
# ---------------------------------------------------------------------------
def sql_models_digest() -> str:
    """SHA-256 over every SQL model file (path and content), in a stable order."""
    digest = hashlib.sha256()
    for path in sorted(SQL_DIR.rglob("*.sql")):
        digest.update(path.relative_to(SQL_DIR).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
        digest.update(b"\0")
    return digest.hexdigest()


def compute_data_hash(
    raw_checksums: dict[str, str],
    *,
    salt: str,
    holidays_version: str,
    metrics_catalogue: Path | None = None,
) -> str:
    """The input fingerprint of a warehouse build (DATA_PLAN.md section 7).

    Covers the raw file checksums, the SQL models, the metric catalogue (once it
    exists), the holiday calendar version and a fingerprint of the pseudonymisation
    salt. The SQL models are hashed by content rather than by git SHA so that the hash
    is the same on a dirty working tree and without git.
    """
    digest = hashlib.sha256()
    for name in sorted(raw_checksums):
        digest.update(f"raw:{name}:{raw_checksums[name]}\n".encode())
    digest.update(f"sql:{sql_models_digest()}\n".encode())
    catalogue = ""
    if metrics_catalogue and metrics_catalogue.is_file():
        catalogue = hashlib.sha256(metrics_catalogue.read_bytes()).hexdigest()
    digest.update(f"metrics:{catalogue}\n".encode())
    digest.update(f"holidays:{holidays_version}\n".encode())
    digest.update(f"salt:{hashlib.sha256(salt.encode()).hexdigest()[:16]}\n".encode())
    return digest.hexdigest()


def warehouse_content_hash(con: duckdb.DuckDBPyConnection) -> tuple[str, dict[str, str]]:
    """Order-independent hash of the contents of every mart table.

    Proves the build is reproducible: same inputs must give the same rows, not just
    the same input hash.
    """
    per_table = {}
    for table in MART_TABLES:
        per_table[table] = _one(
            con,
            f"SELECT coalesce(md5(string_agg(h, '' ORDER BY h)), '') FROM "
            f"(SELECT md5(CAST(t AS VARCHAR)) AS h FROM marts.{table} t)",
        )
    combined = hashlib.sha256("\n".join(f"{k}:{v}" for k, v in sorted(per_table.items())).encode())
    return combined.hexdigest(), per_table


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def write_report(report: dict[str, Any], reports_dir: Path) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / "quality_report.json"
    html_path = reports_dir / "quality_report.html"
    json_path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    html_path.write_text(_render_html(report), encoding="utf-8")
    return json_path, html_path


def _render_html(report: dict[str, Any]) -> str:
    e = html.escape
    banner = ""
    if report.get("synthetic"):
        banner = (
            '<p class="warn"><b>SYNTHETIC (FAKE) DATA.</b> This report does not describe Olist.</p>'
        )
    rows = []
    for c in report["checks"]:
        verdict = {True: "pass", False: "FAIL", None: "info"}[c["passed"]]
        css = {"pass": "ok", "FAIL": "bad", "info": ""}[verdict]
        examples = "; ".join(c["examples"])
        rows.append(
            f'<tr class="{css}"><td>{e(c["level"])}</td><td>{e(c["name"])}</td><td>{verdict}</td>'
            f"<td>{e(str(c['observed']))}</td><td>{e(str(c['threshold'] or ''))}</td>"
            f"<td>{e(c['detail'])} {e(examples)}</td></tr>"
        )
    tables = "".join(
        f"<tr><td>{e(name)}</td><td>{p['rows']}</td></tr>" for name, p in report["tables"].items()
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>MetricSentinel data quality report</title>
<style>body{{font-family:system-ui,sans-serif;margin:2rem;color:#1b1f23}}table{{border-collapse:collapse;margin:1rem 0}}
td,th{{border:1px solid #d0d7de;padding:4px 8px;font-size:14px;vertical-align:top}}.bad{{background:#ffebe9}}
.ok td:nth-child(3){{color:#1a7f37}}.warn{{background:#fff8c5;padding:8px}}code{{font-size:13px}}</style></head><body>
<h1>Data quality report</h1>{banner}
<p>Status: <b>{e(report["status"].upper())}</b> &middot; built {e(str(report["built_at"]))}</p>
<p>data_hash: <code>{e(str(report.get("data_hash")))}</code><br>
warehouse_content_hash: <code>{e(str(report.get("warehouse_content_hash")))}</code></p>
<h2>Checks</h2><table><tr><th>level</th><th>check</th><th>result</th><th>observed</th><th>threshold</th><th>detail</th></tr>
{"".join(rows)}</table>
<h2>Tables</h2><table><tr><th>table</th><th>rows</th></tr>{tables}</table>
<p>Null rates per column are in the JSON report.</p></body></html>"""
