"""Hard validation of the raw tables (FR-003).

Raw tables are loaded as text so that nothing is changed on the way in. Validation
therefore checks that text can be read as the intended type (numbers, timestamps),
that values are in range or in an allowed set, and that keys are unique. Relational
rules (foreign keys, row counts) are checked in SQL.

A hard failure stops the build before any staging or mart table is written.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import duckdb
import pandas as pd
import pandera.pandas as pa
from pandera.errors import SchemaErrors

from pipelines.config import (
    ORDER_STATUSES,
    PAYMENT_TYPES,
    ROW_COUNT_TOLERANCE,
    SOURCE_TABLES,
    TABLES_BY_NAME,
)
from pipelines.db import first_row


@dataclass
class CheckResult:
    """The outcome of one check, as it appears in the quality report."""

    name: str
    level: str  # "hard" | "soft" | "stat"
    passed: bool | None  # None for pure statistics
    detail: str = ""
    observed: Any = None
    threshold: Any = None
    table: str = ""
    examples: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "level": self.level,
            "table": self.table,
            "passed": self.passed,
            "observed": self.observed,
            "threshold": self.threshold,
            "detail": self.detail,
            "examples": self.examples,
        }


# ---------------------------------------------------------------------------
# Column helpers (values arrive as text, possibly missing)
# ---------------------------------------------------------------------------
def _numeric(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def _is_number(s: pd.Series) -> pd.Series:
    return _numeric(s).notna()


def _is_integer(s: pd.Series) -> pd.Series:
    n = _numeric(s)
    return n.notna() & (n == n.round())


def _is_timestamp(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, errors="coerce", format="%Y-%m-%d %H:%M:%S").notna()


def _non_negative(s: pd.Series) -> pd.Series:
    return _numeric(s) >= 0


def _in_range(low: float, high: float) -> Any:
    return lambda s: (_numeric(s) >= low) & (_numeric(s) <= high)


def _col(*checks: pa.Check, nullable: bool = False, unique: bool = False) -> pa.Column:
    return pa.Column(checks=list(checks) or None, nullable=nullable, unique=unique, coerce=False)


NUMBER = pa.Check(_is_number, error="not a number")
INTEGER = pa.Check(_is_integer, error="not an integer")
TIMESTAMP = pa.Check(_is_timestamp, error="not a YYYY-MM-DD HH:MM:SS timestamp")
NON_NEGATIVE = pa.Check(_non_negative, error="negative value")


def _schemas() -> dict[str, pa.DataFrameSchema]:
    return {
        "orders": pa.DataFrameSchema(
            {
                "order_id": _col(unique=True),
                "customer_id": _col(),
                "order_status": _col(pa.Check.isin(sorted(ORDER_STATUSES))),
                "order_purchase_timestamp": _col(TIMESTAMP),
                "order_approved_at": _col(TIMESTAMP, nullable=True),
                "order_delivered_carrier_date": _col(TIMESTAMP, nullable=True),
                "order_delivered_customer_date": _col(TIMESTAMP, nullable=True),
                "order_estimated_delivery_date": _col(TIMESTAMP),
            }
        ),
        "order_items": pa.DataFrameSchema(
            {
                "order_id": _col(),
                "order_item_id": _col(INTEGER, pa.Check(_in_range(1, 1000), error="out of range")),
                "product_id": _col(),
                "seller_id": _col(),
                "shipping_limit_date": _col(TIMESTAMP, nullable=True),
                "price": _col(NUMBER, NON_NEGATIVE),
                "freight_value": _col(NUMBER, NON_NEGATIVE),
            },
            unique=["order_id", "order_item_id"],
        ),
        "order_payments": pa.DataFrameSchema(
            {
                "order_id": _col(),
                "payment_sequential": _col(
                    INTEGER, pa.Check(_in_range(1, 1000), error="out of range")
                ),
                "payment_type": _col(pa.Check.isin(sorted(PAYMENT_TYPES))),
                "payment_installments": _col(INTEGER, NON_NEGATIVE),
                "payment_value": _col(NUMBER, NON_NEGATIVE),
            },
            unique=["order_id", "payment_sequential"],
        ),
        "order_reviews": pa.DataFrameSchema(
            {
                "review_id": _col(),
                "order_id": _col(),
                "review_score": _col(pa.Check.isin(["1", "2", "3", "4", "5"])),
                "review_comment_title": _col(nullable=True),
                "review_comment_message": _col(nullable=True),
                "review_creation_date": _col(TIMESTAMP),
                "review_answer_timestamp": _col(TIMESTAMP, nullable=True),
            }
        ),
        "customers": pa.DataFrameSchema(
            {
                "customer_id": _col(unique=True),
                "customer_unique_id": _col(),
                "customer_zip_code_prefix": _col(nullable=True),
                "customer_city": _col(nullable=True),
                "customer_state": _col(pa.Check.str_length(2, 2)),
            }
        ),
        "sellers": pa.DataFrameSchema(
            {
                "seller_id": _col(unique=True),
                "seller_zip_code_prefix": _col(nullable=True),
                "seller_city": _col(nullable=True),
                "seller_state": _col(pa.Check.str_length(2, 2)),
            }
        ),
        "products": pa.DataFrameSchema(
            {
                "product_id": _col(unique=True),
                "product_category_name": _col(nullable=True),
                "product_name_lenght": _col(INTEGER, NON_NEGATIVE, nullable=True),
                "product_description_lenght": _col(INTEGER, NON_NEGATIVE, nullable=True),
                "product_photos_qty": _col(INTEGER, NON_NEGATIVE, nullable=True),
                "product_weight_g": _col(NUMBER, NON_NEGATIVE, nullable=True),
                "product_length_cm": _col(NUMBER, NON_NEGATIVE, nullable=True),
                "product_height_cm": _col(NUMBER, NON_NEGATIVE, nullable=True),
                "product_width_cm": _col(NUMBER, NON_NEGATIVE, nullable=True),
            }
        ),
        "geolocation": pa.DataFrameSchema(
            {
                "geolocation_zip_code_prefix": _col(),
                "geolocation_lat": _col(NUMBER, pa.Check(_in_range(-90, 90), error="out of range")),
                "geolocation_lng": _col(
                    NUMBER, pa.Check(_in_range(-180, 180), error="out of range")
                ),
                "geolocation_city": _col(nullable=True),
                "geolocation_state": _col(nullable=True),
            }
        ),
        "category_translation": pa.DataFrameSchema(
            {
                "product_category_name": _col(unique=True),
                "product_category_name_english": _col(),
            }
        ),
    }


def _summarise_failures(error: SchemaErrors, max_examples: int = 5) -> tuple[int, list[str]]:
    cases = error.failure_cases
    examples = []
    for _, row in cases.head(max_examples).iterrows():
        column = row.get("column")
        # Never echo free-text values (reviews are untrusted); show where, not what, for them.
        value = (
            "<hidden>"
            if column in ("review_comment_title", "review_comment_message")
            else row.get("failure_case")
        )
        examples.append(f"column={column} check={row.get('check')} value={value!r}")
    return len(cases), examples


def validate_schemas(con: duckdb.DuckDBPyConnection) -> list[CheckResult]:
    """Run the Pandera schema for every raw table. One result per table."""
    results = []
    for table, schema in _schemas().items():
        frame = con.execute(f"SELECT * FROM raw.{table}").df()
        # The raw layer is all text, but pandas may give object or string dtype.
        try:
            schema.validate(frame, lazy=True)
        except SchemaErrors as error:
            count, examples = _summarise_failures(error)
            results.append(
                CheckResult(
                    name=f"schema:{table}",
                    level="hard",
                    table=table,
                    passed=False,
                    observed=count,
                    detail=f"{count} failing value(s) across {len(frame)} rows",
                    examples=examples,
                )
            )
        else:
            results.append(
                CheckResult(
                    name=f"schema:{table}",
                    level="hard",
                    table=table,
                    passed=True,
                    observed=len(frame),
                    detail=f"{len(frame)} rows valid",
                )
            )
    return results


def validate_relationships(con: duckdb.DuckDBPyConnection) -> list[CheckResult]:
    """Foreign-key coverage between raw tables (hard)."""
    rules = [
        ("order_items.order_id in orders", "order_items", "order_id", "orders", "order_id"),
        ("order_payments.order_id in orders", "order_payments", "order_id", "orders", "order_id"),
        ("order_reviews.order_id in orders", "order_reviews", "order_id", "orders", "order_id"),
        ("orders.customer_id in customers", "orders", "customer_id", "customers", "customer_id"),
    ]
    results = []
    for name, child, child_col, parent, parent_col in rules:
        orphans = first_row(
            con.execute(
                f"SELECT count(*) FROM raw.{child} c "
                f"WHERE NOT EXISTS (SELECT 1 FROM raw.{parent} p WHERE p.{parent_col} = c.{child_col})"
            )
        )[0]
        results.append(
            CheckResult(
                name=f"fk:{name}",
                level="hard",
                table=child,
                passed=orphans == 0,
                observed=orphans,
                threshold=0,
                detail=f"{orphans} orphan row(s)",
            )
        )
    return results


def validate_row_counts(
    con: duckdb.DuckDBPyConnection, *, enforce: bool = True
) -> list[CheckResult]:
    """Loaded rows must be within +/-1% of the published count (guards against a bad parse)."""
    results = []
    for source in SOURCE_TABLES:
        loaded = first_row(con.execute(f"SELECT count(*) FROM raw.{source.table}"))[0]
        deviation = abs(loaded - source.expected_rows) / source.expected_rows
        results.append(
            CheckResult(
                name=f"row_count:{source.table}",
                level="hard" if enforce else "stat",
                table=source.table,
                passed=(deviation <= ROW_COUNT_TOLERANCE) if enforce else None,
                observed=loaded,
                threshold=f"{source.expected_rows} +/- {ROW_COUNT_TOLERANCE:.0%}",
                detail=f"deviation {deviation:.4%}"
                + ("" if enforce else " (not enforced for this run)"),
            )
        )
    assert {r.table for r in results} == set(TABLES_BY_NAME)
    return results


def failed(results: list[CheckResult]) -> list[CheckResult]:
    return [r for r in results if r.level == "hard" and r.passed is False]
