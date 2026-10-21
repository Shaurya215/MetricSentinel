"""Small DuckDB helpers shared by the pipeline modules."""

from __future__ import annotations

from typing import Any

import duckdb


def first_row(cursor: duckdb.DuckDBPyConnection) -> tuple[Any, ...]:
    """The first row of a result that must have one (aggregate queries always do)."""
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("query unexpectedly returned no rows")
    return row
