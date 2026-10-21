"""Build the warehouse: download/verify -> raw -> hard checks -> staging -> marts -> report.

    make data
    uv run --group data python -m pipelines.build [--raw-dir DIR] [--warehouse FILE]

The build is idempotent and atomic. It writes to a temporary DuckDB file and replaces
the real warehouse only when every hard check passes, so a failed run never destroys a
good warehouse. Two runs on the same inputs produce the same `data_hash` and the same
`warehouse_content_hash`.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import holidays

from core.logging import configure_logging, get_logger, run_context
from core.settings import REPO_ROOT, get_settings
from pipelines import quality, validate
from pipelines.config import REVIEW_TEXT_MAX_CHARS, SELLER_TIER_WINDOW_DAYS, SQL_DIR
from pipelines.db import first_row
from pipelines.download import (
    ChecksumMismatchError,
    DataNotFoundError,
    RawFile,
    ensure_raw_files,
)

log = get_logger(__name__)
SYNTHETIC_MARKER = "SYNTHETIC_DATA.txt"


class BuildFailedError(RuntimeError):
    """A hard check failed. The report explaining why has already been written."""

    def __init__(self, message: str, report: dict[str, Any]) -> None:
        super().__init__(message)
        self.report = report


@dataclass
class BuildResult:
    report: dict[str, Any]
    warehouse_path: Path
    report_json: Path
    report_html: Path

    @property
    def data_hash(self) -> str:
        return self.report["data_hash"]

    @property
    def content_hash(self) -> str:
        return self.report["warehouse_content_hash"]


# ---------------------------------------------------------------------------
# SQL runner
# ---------------------------------------------------------------------------
def render_sql(text: str, params: dict[str, str]) -> str:
    """Substitute `{{ name }}` placeholders. Unknown placeholders are an error."""
    for key, value in params.items():
        text = text.replace("{{ " + key + " }}", value)
    if "{{" in text:
        raise ValueError(f"unresolved placeholder in SQL: {text[text.index('{{') :][:40]!r}")
    return text


def run_sql_dir(
    con: duckdb.DuckDBPyConnection, directory: Path, params: dict[str, str]
) -> list[str]:
    """Run every .sql file in a directory in filename order."""
    ran = []
    for path in sorted(directory.glob("*.sql")):
        con.execute(render_sql(path.read_text(encoding="utf-8"), params))
        ran.append(path.name)
        log.info("sql_model_ran", model=f"{directory.name}/{path.name}")
    return ran


def sql_params(salt: str) -> dict[str, str]:
    return {
        "salt": salt.replace("'", "''"),
        "review_text_max_chars": str(REVIEW_TEXT_MAX_CHARS),
        "tier_window_days": str(SELLER_TIER_WINDOW_DAYS),
    }


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def load_raw(con: duckdb.DuckDBPyConnection, files: list[RawFile]) -> None:
    """Load each CSV as text, unchanged (FR-002), plus a load-metadata table."""
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    con.execute(
        "CREATE TABLE raw._load_metadata (table_name VARCHAR, file_name VARCHAR, "
        "sha256 VARCHAR, size_bytes BIGINT, rows BIGINT)"
    )
    for raw_file in files:
        path = raw_file.path.as_posix().replace("'", "''")
        con.execute(
            f"CREATE TABLE raw.{raw_file.table} AS SELECT * FROM read_csv('{path}', "
            "header = true, all_varchar = true, quote = '\"', escape = '\"')"
        )
        rows = first_row(con.execute(f"SELECT count(*) FROM raw.{raw_file.table}"))[0]
        con.execute(
            "INSERT INTO raw._load_metadata VALUES (?, ?, ?, ?, ?)",
            [raw_file.table, raw_file.path.name, raw_file.sha256, raw_file.size_bytes, rows],
        )
        log.info("raw_table_loaded", table=raw_file.table, rows=rows)


def load_holidays(con: duckdb.DuckDBPyConnection) -> None:
    """Brazilian national holidays covering the years in the data (plus one year each side)."""
    first, last = first_row(
        con.execute(
            "SELECT min(CAST(substr(order_purchase_timestamp, 1, 4) AS INTEGER)), "
            "max(CAST(substr(order_purchase_timestamp, 1, 4) AS INTEGER)) FROM raw.orders"
        )
    )
    calendar = holidays.Brazil(years=range(first - 1, last + 2))
    con.execute("CREATE SCHEMA IF NOT EXISTS staging")
    con.execute("CREATE SCHEMA IF NOT EXISTS marts")
    con.execute("CREATE TABLE staging.br_holidays (date DATE, name VARCHAR)")
    con.executemany("INSERT INTO staging.br_holidays VALUES (?, ?)", sorted(calendar.items()))


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
def build(
    *,
    raw_dir: Path,
    warehouse_path: Path,
    reports_dir: Path,
    salt: str,
    kaggle_username: str | None = None,
    kaggle_key: str | None = None,
    update_checksums: bool = False,
    enforce_row_counts: bool | None = None,
) -> BuildResult:
    """Run the whole data pipeline. Raises `BuildFailedError` on a hard-check failure."""
    synthetic = (raw_dir / SYNTHETIC_MARKER).is_file()
    if enforce_row_counts is None:
        enforce_row_counts = not synthetic
    if synthetic and warehouse_path.resolve() == get_settings().duckdb_path.resolve():
        raise ValueError(
            "The raw directory holds SYNTHETIC (fake) data. Refusing to write it into the real "
            "warehouse path; pass --warehouse and --reports-dir pointing somewhere else."
        )

    files, recorded = ensure_raw_files(
        raw_dir,
        kaggle_username=kaggle_username,
        kaggle_key=kaggle_key,
        update_checksums=update_checksums,
    )
    checksums = {f.path.name: f.sha256 for f in files}

    warehouse_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = warehouse_path.with_name(warehouse_path.name + ".tmp")
    for stale in (tmp_path, tmp_path.with_name(tmp_path.name + ".wal")):
        stale.unlink(missing_ok=True)

    checks: list[validate.CheckResult] = []
    report: dict[str, Any] = {
        "status": "failed",
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "synthetic": synthetic,
        "checksums_recorded_this_run": recorded,
        "raw_files": [
            {"table": f.table, "file": f.path.name, "sha256": f.sha256, "size_bytes": f.size_bytes}
            for f in files
        ],
        "data_hash": None,
        "warehouse_content_hash": None,
        "mart_hashes": {},
        "checks": [],
        "tables": {},
    }

    con = duckdb.connect(str(tmp_path))
    try:
        load_raw(con, files)

        checks += validate.validate_row_counts(con, enforce=enforce_row_counts)
        checks += validate.validate_schemas(con)
        checks += validate.validate_relationships(con)
        hard_failures = validate.failed(checks)
        if hard_failures:
            raise _fail(report, checks, hard_failures, reports_dir)

        params = sql_params(salt)
        load_holidays(con)
        run_sql_dir(con, SQL_DIR / "staging", params)
        run_sql_dir(con, SQL_DIR / "marts", params)

        checks += quality.mart_checks(con)
        hard_failures = validate.failed(checks)
        if hard_failures:
            raise _fail(report, checks, hard_failures, reports_dir)

        checks += quality.soft_checks(con)
        checks += quality.statistics(con)

        report["tables"] = quality.table_profiles(con, ("raw", "staging", "marts"))
        content_hash, per_table = quality.warehouse_content_hash(con)
        report.update(
            status="passed",
            data_hash=quality.compute_data_hash(
                checksums,
                salt=salt,
                holidays_version=holidays.__version__,
                metrics_catalogue=REPO_ROOT / "config" / "metrics.yaml",
            ),
            warehouse_content_hash=content_hash,
            mart_hashes=per_table,
        )
        con.execute("CREATE SCHEMA IF NOT EXISTS meta")
        con.execute("CREATE TABLE meta.build (key VARCHAR, value VARCHAR)")
        con.executemany(
            "INSERT INTO meta.build VALUES (?, ?)",
            [
                ("data_hash", report["data_hash"]),
                ("warehouse_content_hash", content_hash),
                ("synthetic", str(synthetic).lower()),
            ],
        )
    except BaseException:
        con.close()
        tmp_path.unlink(missing_ok=True)
        raise
    con.close()
    tmp_path.replace(warehouse_path)

    report["checks"] = [c.as_dict() for c in checks]
    report_json, report_html = quality.write_report(report, reports_dir)
    soft_failed = [c.name for c in checks if c.level == "soft" and c.passed is False]
    log.info(
        "build_finished",
        warehouse=str(warehouse_path),
        data_hash=report["data_hash"],
        soft_check_failures=soft_failed,
    )
    return BuildResult(report, warehouse_path, report_json, report_html)


def _fail(
    report: dict[str, Any],
    checks: list[validate.CheckResult],
    hard_failures: list[validate.CheckResult],
    reports_dir: Path,
) -> BuildFailedError:
    report["checks"] = [c.as_dict() for c in checks]
    quality.write_report(report, reports_dir)
    names = ", ".join(c.name for c in hard_failures)
    log.error("build_failed", failed_checks=[c.name for c in hard_failures])
    return BuildFailedError(
        f"Hard check(s) failed: {names}. See {reports_dir / 'quality_report.html'}", report
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Build the MetricSentinel DuckDB warehouse.")
    parser.add_argument("--raw-dir", type=Path, default=settings.raw_data_dir)
    parser.add_argument("--warehouse", type=Path, default=settings.duckdb_path)
    parser.add_argument("--reports-dir", type=Path, default=settings.reports_dir)
    parser.add_argument(
        "--update-checksums",
        action="store_true",
        help="accept the current raw files as the new reference checksums",
    )
    args = parser.parse_args(argv)

    configure_logging()
    with run_context(component="data_build"):
        try:
            result = build(
                raw_dir=args.raw_dir,
                warehouse_path=args.warehouse,
                reports_dir=args.reports_dir,
                salt=settings.pseudonym_salt.get_secret_value(),
                kaggle_username=settings.kaggle_username or None,
                kaggle_key=settings.kaggle_key.get_secret_value() if settings.kaggle_key else None,
                update_checksums=args.update_checksums,
            )
        except (DataNotFoundError, ChecksumMismatchError, BuildFailedError, ValueError) as error:
            print(f"\nBUILD FAILED\n{error}", file=sys.stderr)
            return 1

    soft_failed = [
        c for c in result.report["checks"] if c["level"] == "soft" and c["passed"] is False
    ]
    print(f"\nWarehouse: {result.warehouse_path}")
    print(f"data_hash:               {result.data_hash}")
    print(f"warehouse_content_hash:  {result.content_hash}")
    print(f"Report: {result.report_html}")
    if result.report["synthetic"]:
        print("NOTE: this was SYNTHETIC (fake) data.")
    if result.report["checksums_recorded_this_run"] and not result.report["synthetic"]:
        print(f"NOTE: checksums.json was written in {args.raw_dir}; commit it.")
    if soft_failed:
        print("Soft-check warnings: " + ", ".join(c["name"] for c in soft_failed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
