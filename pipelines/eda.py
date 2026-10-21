"""Exploratory analysis that informs the detection design (Phase 2 EDA deliverable).

Reads the built warehouse and writes `eda_findings.json` and `eda_findings.md`. It
derives, from the data and not from assumptions:

  * the dense monitoring window, the warm-up start and the first detection date
  * weekday, month and holiday seasonality
  * daily KPI distributions for the eight catalogue KPIs
  * segment sizes per dimension, and the minimum-sample rule with its evidence
  * whether each expected data issue in DATA_PLAN.md section 3 is confirmed or refuted

    uv run python -m pipelines.eda [--warehouse FILE] [--out DIR]

IMPORTANT: results are only findings when the warehouse was built from the real
Olist data. On synthetic data this runs for testing and says so in its output.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from core.settings import get_settings
from pipelines.db import first_row

MIN_SAMPLE = 30  # DATA_PLAN.md section 6: minimum n per day for ratio and mean KPIs
DAILY_COVERAGE_RULE = 0.80  # a series is monitored daily if >= 80% of window days meet MIN_SAMPLE
WARMUP_DAYS = 90  # DATA_PLAN.md section 9
MIN_DAILY_ORDERS = 20  # floor for what counts as a dense day
DENSITY_FRACTION = 0.25  # ... and at least this fraction of the typical (median) day
EXPECTED_CARDINALITY = {
    "customer_state": 27,
    "category_en": 70,
    "payment_type": 5,
    "seller_tier": 4,
}


def _scalar(con: duckdb.DuckDBPyConnection, sql: str, *params: Any) -> Any:
    return first_row(con.execute(sql, list(params)))[0]


def _is_synthetic(con: duckdb.DuckDBPyConnection) -> bool:
    return _scalar(con, "SELECT value FROM meta.build WHERE key = 'synthetic'") == "true"


# ---------------------------------------------------------------------------
# 1. Window
# ---------------------------------------------------------------------------
def monitoring_window(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    """Longest run of dense days, where dense means a 7-day mean above a data-derived floor."""
    daily = con.execute(
        "SELECT d.date, count(o.order_id) AS n FROM marts.dim_date d "
        "LEFT JOIN marts.fct_orders o ON o.purchase_date = d.date "
        "WHERE d.date <= (SELECT max(purchase_date) FROM marts.fct_orders) GROUP BY d.date ORDER BY d.date"
    ).df()
    typical = float(daily.loc[daily["n"] > 0, "n"].median())
    floor = max(MIN_DAILY_ORDERS, DENSITY_FRACTION * typical)
    daily["rolling7"] = daily["n"].rolling(7, min_periods=7).mean()
    daily["dense"] = daily["rolling7"] >= floor
    best_start = best_len = run_start = run_len = 0
    for i, dense in enumerate(daily["dense"].tolist()):
        if dense:
            run_start, run_len = (i, 1) if run_len == 0 else (run_start, run_len + 1)
            if run_len > best_len:
                best_start, best_len = run_start, run_len
        else:
            run_len = 0
    by_month = (
        con.execute(
            "SELECT strftime(purchase_date, '%Y-%m') AS month, count(*) AS orders "
            "FROM marts.fct_orders GROUP BY 1 ORDER BY 1"
        )
        .df()
        .to_dict("records")
    )
    result: dict[str, Any] = {
        "first_order_date": str(daily["date"].iloc[0].date()),
        "last_order_date": str(daily["date"].iloc[-1].date()),
        "typical_daily_orders_median": typical,
        "dense_day_floor": floor,
        "rule": f"dense day = 7-day mean >= max({MIN_DAILY_ORDERS}, {DENSITY_FRACTION} x median daily orders); "
        "window = longest run of dense days",
        "orders_by_month": by_month,
    }
    if best_len == 0:
        result["window"] = None
        return result
    start = daily["date"].iloc[best_start].date()
    end = daily["date"].iloc[best_start + best_len - 1].date()
    result.update(
        window={"start": str(start), "end": str(end), "days": best_len},
        warmup_start=str(start),
        first_detection_date=str(start + timedelta(days=WARMUP_DAYS)),
        monitored_days=max((end - (start + timedelta(days=WARMUP_DAYS))).days + 1, 0),
        days_outside_window_with_orders=int(
            (
                (daily["n"] > 0) & ((daily["date"].dt.date < start) | (daily["date"].dt.date > end))
            ).sum()
        ),
    )
    return result


# ---------------------------------------------------------------------------
# 2. Seasonality
# ---------------------------------------------------------------------------
def seasonality(con: duckdb.DuckDBPyConnection, start: str, end: str) -> dict[str, Any]:
    daily = con.execute(
        "SELECT d.date, d.weekday_name, d.month, d.is_holiday_br, count(o.order_id) AS n "
        "FROM marts.dim_date d LEFT JOIN marts.fct_orders o ON o.purchase_date = d.date "
        "WHERE d.date BETWEEN ? AND ? GROUP BY ALL ORDER BY d.date",
        [start, end],
    ).df()
    weekday = daily.groupby("weekday_name")["n"].mean()
    overall = float(daily["n"].mean())
    holiday = (
        float(daily.loc[daily["is_holiday_br"], "n"].mean())
        if daily["is_holiday_br"].any()
        else None
    )
    non_holiday = float(daily.loc[~daily["is_holiday_br"], "n"].mean())
    black_friday = daily.loc[daily["date"] == pd.Timestamp("2017-11-24"), "n"]
    prior = daily.loc[
        (daily["date"] >= pd.Timestamp("2017-11-03"))
        & (daily["date"] < pd.Timestamp("2017-11-24")),
        "n",
    ]
    return {
        "mean_daily_orders": overall,
        "weekday_index": {k: round(float(v) / overall, 3) for k, v in weekday.items()},
        "strongest_weekday_effect": round(float(weekday.max() / weekday.min()), 3),
        "holiday_mean_orders": holiday,
        "non_holiday_mean_orders": non_holiday,
        "holiday_ratio": round(holiday / non_holiday, 3) if holiday and non_holiday else None,
        "black_friday_2017_ratio_to_prior_3_weeks": (
            round(float(black_friday.iloc[0] / prior.mean()), 3)
            if len(black_friday) and len(prior) and prior.mean()
            else None
        ),
        "monthly_mean_orders": {
            str(k): round(float(v), 1) for k, v in daily.groupby("month")["n"].mean().items()
        },
    }


# ---------------------------------------------------------------------------
# 3. KPI distributions
# ---------------------------------------------------------------------------
KPI_SQL = {
    "orders": (
        "SELECT purchase_date AS date, count(*) AS value, count(*) AS n FROM marts.fct_orders GROUP BY 1"
    ),
    "gmv": (
        "SELECT purchase_date AS date, CAST(sum(order_gmv) AS DOUBLE) AS value, count(*) AS n "
        "FROM marts.fct_orders WHERE has_items GROUP BY 1"
    ),
    "aov": (
        "SELECT purchase_date AS date, CAST(sum(order_gmv) AS DOUBLE) / count(*) AS value, count(*) AS n "
        "FROM marts.fct_orders WHERE has_items GROUP BY 1"
    ),
    "freight_ratio": (
        "SELECT purchase_date AS date, CAST(sum(freight_total) AS DOUBLE) / "
        "nullif(CAST(sum(order_gmv) AS DOUBLE), 0) AS value, count(*) AS n "
        "FROM marts.fct_orders WHERE has_items GROUP BY 1"
    ),
    "late_delivery_rate": (
        "SELECT delivered_date AS date, avg(CAST(is_late AS DOUBLE)) AS value, count(*) AS n "
        "FROM marts.fct_orders WHERE is_delivered GROUP BY 1"
    ),
    "avg_delivery_days": (
        "SELECT delivered_date AS date, avg(delivery_days) AS value, count(*) AS n "
        "FROM marts.fct_orders WHERE is_delivered GROUP BY 1"
    ),
    "avg_review_score": (
        "SELECT review_date AS date, avg(score) AS value, count(*) AS n "
        "FROM marts.fct_reviews GROUP BY 1"
    ),
    "low_review_rate": (
        "SELECT review_date AS date, avg(CAST(score <= 2 AS DOUBLE)) AS value, count(*) AS n "
        "FROM marts.fct_reviews GROUP BY 1"
    ),
}
# Which KPIs need MIN_SAMPLE, and what the sample is (DATA_PLAN.md section 6)
KPI_MIN_SAMPLE = {
    "aov": MIN_SAMPLE,
    "freight_ratio": MIN_SAMPLE,
    "late_delivery_rate": MIN_SAMPLE,
    "avg_delivery_days": MIN_SAMPLE,
    "avg_review_score": MIN_SAMPLE,
    "low_review_rate": MIN_SAMPLE,
}


def kpi_distributions(con: duckdb.DuckDBPyConnection, start: str, end: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for kpi, sql in KPI_SQL.items():
        frame = con.execute(f"SELECT * FROM ({sql}) WHERE date BETWEEN ? AND ?", [start, end]).df()
        days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
        values = frame["value"].dropna()
        entry: dict[str, Any] = {"days_with_data": len(values), "days_in_window": days}
        if len(values):
            q = values.quantile([0.05, 0.5, 0.95])
            entry.update(
                mean=float(values.mean()),
                std=float(values.std()) if len(values) > 1 else 0.0,
                min=float(values.min()),
                p05=float(q[0.05]),
                median=float(q[0.5]),
                p95=float(q[0.95]),
                max=float(values.max()),
                coefficient_of_variation=float(values.std() / values.mean())
                if values.mean() and len(values) > 1
                else None,
            )
        if kpi in KPI_MIN_SAMPLE:
            entry["share_of_days_with_sample_ge_30"] = float(
                (frame["n"] >= MIN_SAMPLE).sum() / days
            )
        out[kpi] = entry
    return out


# ---------------------------------------------------------------------------
# 4. Segments and the minimum-sample rule
# ---------------------------------------------------------------------------
def segment_analysis(con: duckdb.DuckDBPyConnection, start: str, end: str) -> dict[str, Any]:
    """Per dimension: cardinality, and how many series meet the sample rule daily, weekly or never."""
    con.execute(
        """
        CREATE OR REPLACE TEMP VIEW order_dim AS
        SELECT order_id, 'customer_state' AS dimension, customer_state AS value FROM marts.fct_orders
        UNION ALL SELECT order_id, 'payment_type', primary_payment_type FROM marts.fct_orders
        UNION ALL SELECT DISTINCT order_id, 'category_en', category_en FROM marts.fct_order_items
        UNION ALL SELECT DISTINCT i.order_id, 'seller_tier', t.tier
                  FROM marts.fct_order_items i
                  JOIN marts.seller_tier_daily t ON t.seller_id = i.seller_id AND t.date = i.purchase_date
        """
    )
    kinds = {
        "orders": "SELECT od.dimension, od.value, o.purchase_date AS date, count(DISTINCT o.order_id) AS n "
        "FROM order_dim od JOIN marts.fct_orders o USING (order_id) WHERE od.value IS NOT NULL GROUP BY ALL",
        "deliveries": "SELECT od.dimension, od.value, o.delivered_date AS date, count(DISTINCT o.order_id) AS n "
        "FROM order_dim od JOIN marts.fct_orders o USING (order_id) "
        "WHERE o.is_delivered AND od.value IS NOT NULL GROUP BY ALL",
        "reviews": "SELECT od.dimension, od.value, r.review_date AS date, count(DISTINCT r.order_id) AS n "
        "FROM order_dim od JOIN marts.fct_reviews r USING (order_id) WHERE od.value IS NOT NULL GROUP BY ALL",
    }
    n_days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    result: dict[str, Any] = {
        "sample_rule": {
            "min_sample_per_period": MIN_SAMPLE,
            "daily_coverage_to_monitor_daily": DAILY_COVERAGE_RULE,
            "rule": f"monitor a series daily if >= {DAILY_COVERAGE_RULE:.0%} of window days have n >= {MIN_SAMPLE}; "
            f"otherwise weekly if >= {DAILY_COVERAGE_RULE:.0%} of weeks do; otherwise exclude",
        },
        "dimensions": {},
    }
    for dimension in EXPECTED_CARDINALITY:
        values = [
            r[0]
            for r in con.execute(
                "SELECT DISTINCT value FROM order_dim WHERE dimension = ? AND value IS NOT NULL ORDER BY 1",
                [dimension],
            ).fetchall()
        ]
        entry: dict[str, Any] = {
            "cardinality": len(values),
            "expected_cardinality": EXPECTED_CARDINALITY[dimension],
            "kinds": {},
        }
        for kind, sql in kinds.items():
            frame = con.execute(
                f"SELECT * FROM ({sql}) WHERE dimension = ? AND date BETWEEN ? AND ?",
                [dimension, start, end],
            ).df()
            frame["week"] = pd.to_datetime(frame["date"]).dt.to_period("W").astype(str)
            n_weeks = len(pd.period_range(start, end, freq="W"))
            counts = {"daily": 0, "weekly": 0, "excluded": 0}
            for value in values:
                series = frame[frame["value"] == value]
                daily_cov = float((series["n"] >= MIN_SAMPLE).sum() / n_days)
                weekly_cov = float(
                    (series.groupby("week")["n"].sum() >= MIN_SAMPLE).sum() / n_weeks
                )
                if daily_cov >= DAILY_COVERAGE_RULE:
                    counts["daily"] += 1
                elif weekly_cov >= DAILY_COVERAGE_RULE:
                    counts["weekly"] += 1
                else:
                    counts["excluded"] += 1
            entry["kinds"][kind] = counts
        result["dimensions"][dimension] = entry
    return result


# ---------------------------------------------------------------------------
# 5. Expected data issues (DATA_PLAN.md section 3)
# ---------------------------------------------------------------------------
def expected_issues(con: duckdb.DuckDBPyConnection, window: dict[str, Any]) -> list[dict[str, Any]]:
    def verdict(observed: int | float, expected_positive: bool = True) -> str:
        return "CONFIRMED" if (observed > 0) == expected_positive else "REFUTED"

    outside = window.get("days_outside_window_with_orders", 0)
    delivered_no_date = _scalar(
        con, "SELECT count(*) FROM marts.fct_orders WHERE status = 'delivered' AND NOT is_delivered"
    )
    reviews_multi = _scalar(
        con,
        "SELECT count(*) FROM (SELECT order_id FROM raw.order_reviews GROUP BY 1 HAVING count(*) > 1)",
    )
    reviews_shared = _scalar(
        con,
        "SELECT count(*) FROM (SELECT review_id FROM raw.order_reviews GROUP BY 1 HAVING count(DISTINCT order_id) > 1)",
    )
    no_items = _scalar(con, "SELECT count(*) FROM marts.fct_orders WHERE NOT has_items")
    no_payment = _scalar(con, "SELECT count(*) FROM marts.fct_orders WHERE NOT has_payment")
    unknown_products = _scalar(
        con, "SELECT count(*) FROM marts.dim_products WHERE category_en = 'unknown'"
    )
    not_defined = _scalar(
        con, "SELECT count(*) FROM staging.order_payments WHERE payment_type = 'not_defined'"
    )
    geo_rows, geo_zips = first_row(
        con.execute(
            "SELECT count(*), count(DISTINCT geolocation_zip_code_prefix) FROM raw.geolocation"
        )
    )
    return [
        {
            "issue": "Sparse start and cut-off at the end of the date range",
            "observed": f"{outside} day(s) with orders fall outside the dense window",
            "status": verdict(outside),
        },
        {
            "issue": "Delivered orders with a missing delivery date",
            "observed": delivered_no_date,
            "status": verdict(delivered_no_date),
        },
        {
            "issue": "Several reviews per order",
            "observed": reviews_multi,
            "status": verdict(reviews_multi),
        },
        {
            "issue": "review_id reused across orders",
            "observed": reviews_shared,
            "status": verdict(reviews_shared),
        },
        {"issue": "Orders with no items", "observed": no_items, "status": verdict(no_items)},
        {"issue": "Orders with no payments", "observed": no_payment, "status": verdict(no_payment)},
        {
            "issue": "Products with an unknown category (null or untranslated)",
            "observed": unknown_products,
            "status": verdict(unknown_products),
        },
        {
            "issue": "payment_type = not_defined",
            "observed": not_defined,
            "status": verdict(not_defined),
        },
        {
            "issue": "Geolocation has many rows per zip prefix",
            "observed": f"{geo_rows} rows for {geo_zips} prefixes",
            "status": "CONFIRMED" if geo_rows > geo_zips else "REFUTED",
        },
    ]


# ---------------------------------------------------------------------------
def run(warehouse: Path, out_dir: Path) -> dict[str, Any]:
    con = duckdb.connect(str(warehouse), read_only=True)
    try:
        window = monitoring_window(con)
        findings: dict[str, Any] = {
            "synthetic": _is_synthetic(con),
            "data_hash": _scalar(con, "SELECT value FROM meta.build WHERE key = 'data_hash'"),
            "monitoring_window": window,
        }
        if window.get("window"):
            start, end = window["window"]["start"], window["window"]["end"]
            findings["seasonality"] = seasonality(con, start, end)
            findings["kpi_distributions"] = kpi_distributions(con, start, end)
            findings["segments"] = segment_analysis(con, start, end)
        findings["expected_issues"] = expected_issues(con, window)
    finally:
        con.close()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "eda_findings.json").write_text(
        json.dumps(findings, indent=2, default=str) + "\n", encoding="utf-8"
    )
    (out_dir / "eda_findings.md").write_text(render_markdown(findings), encoding="utf-8")
    return findings


def render_markdown(f: dict[str, Any]) -> str:
    lines = ["# EDA findings", ""]
    if f["synthetic"]:
        lines += [
            "> **SYNTHETIC (FAKE) DATA.** These numbers describe generated test data, not Olist.",
            "",
        ]
    lines += [f"Warehouse `data_hash`: `{f['data_hash']}`", ""]
    w = f["monitoring_window"]
    lines += ["## Monitoring window", "", f"Rule: {w['rule']}.", ""]
    if w.get("window"):
        win = w["window"]
        lines += [
            f"- Dense window: **{win['start']} to {win['end']}** ({win['days']} days)",
            f"- Warm-up start: **{w['warmup_start']}** (history used for training only)",
            f"- First detection date: **{w['first_detection_date']}** (after {WARMUP_DAYS} warm-up days)",
            f"- Monitored days: {w['monitored_days']}",
            f"- Median daily orders: {w['typical_daily_orders_median']:.0f}; dense-day floor: {w['dense_day_floor']:.1f}",
            "",
        ]
    else:
        lines += ["No dense window found.", ""]
    if "seasonality" in f:
        s = f["seasonality"]
        lines += [
            "## Seasonality (inside the window)",
            "",
            f"- Weekday index (1.0 = average day): {s['weekday_index']}",
            f"- Strongest weekday effect (max/min): {s['strongest_weekday_effect']}",
            f"- Holiday vs normal day orders: {s['holiday_ratio']}",
            f"- Black Friday 2017 vs the 3 weeks before: {s['black_friday_2017_ratio_to_prior_3_weeks']}",
            "",
        ]
    if "kpi_distributions" in f:
        lines += [
            "## Daily KPI distributions (inside the window)",
            "",
            "| KPI | days | mean | std | p05 | median | p95 | CV | days with n>=30 |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for kpi, d in f["kpi_distributions"].items():
            if "mean" not in d:
                lines.append(f"| {kpi} | {d['days_with_data']} | - | - | - | - | - | - | - |")
                continue
            cv = (
                f"{d['coefficient_of_variation']:.2f}"
                if d["coefficient_of_variation"] is not None
                else "-"
            )
            cov = (
                f"{d['share_of_days_with_sample_ge_30']:.0%}"
                if "share_of_days_with_sample_ge_30" in d
                else "n/a"
            )
            lines.append(
                f"| {kpi} | {d['days_with_data']} | {d['mean']:.3g} | {d['std']:.3g} | {d['p05']:.3g} | "
                f"{d['median']:.3g} | {d['p95']:.3g} | {cv} | {cov} |"
            )
        lines.append("")
    if "segments" in f:
        seg = f["segments"]
        lines += [
            "## Segments and the minimum-sample rule",
            "",
            f"Rule: {seg['sample_rule']['rule']}.",
            "",
            "| dimension | values (expected) | kind | daily | weekly | excluded |",
            "|---|---|---|---|---|---|",
        ]
        for dim, d in seg["dimensions"].items():
            for kind, c in d["kinds"].items():
                lines.append(
                    f"| {dim} | {d['cardinality']} ({d['expected_cardinality']}) | {kind} | {c['daily']} | {c['weekly']} | {c['excluded']} |"
                )
        lines.append("")
    lines += [
        "## Expected data issues (DATA_PLAN.md section 3)",
        "",
        "| issue | observed | status |",
        "|---|---|---|",
    ]
    for item in f["expected_issues"]:
        lines.append(f"| {item['issue']} | {item['observed']} | {item['status']} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Run the EDA against the built warehouse.")
    parser.add_argument("--warehouse", type=Path, default=settings.duckdb_path)
    parser.add_argument("--out", type=Path, default=settings.reports_dir)
    args = parser.parse_args()
    findings = run(args.warehouse, args.out)
    print((args.out / "eda_findings.md").read_text(encoding="utf-8"))
    if findings["synthetic"]:
        print("NOTE: SYNTHETIC data. These are not findings about Olist.")


if __name__ == "__main__":
    main()
