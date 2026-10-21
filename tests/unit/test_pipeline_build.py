"""Hard validation, build atomicity, determinism and the quality report (FR-003, FR-006)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from core.settings import get_settings
from pipelines.build import BuildFailedError, BuildResult, build
from tests.unit.conftest import build_synthetic

pytestmark = pytest.mark.unit
Mutate = Callable[[Path, str, Callable[[list[list[str]]], None]], None]


def failed_checks(error: BuildFailedError) -> set[str]:
    return {
        c["name"] for c in error.report["checks"] if c["level"] == "hard" and c["passed"] is False
    }


# ---------------------------------------------------------------------------
# Hard failures stop the build
# ---------------------------------------------------------------------------
def test_duplicate_primary_key_is_rejected(
    small_raw: Path, tmp_path: Path, mutate_csv: Mutate
) -> None:
    mutate_csv(small_raw, "olist_orders_dataset.csv", lambda rows: rows.append(list(rows[1])))
    with pytest.raises(BuildFailedError) as error:
        build_synthetic(small_raw, tmp_path / "out")
    assert "schema:orders" in failed_checks(error.value)


def test_negative_price_is_rejected(small_raw: Path, tmp_path: Path, mutate_csv: Mutate) -> None:
    def edit(rows: list[list[str]]) -> None:
        rows[1][rows[0].index("price")] = "-5.00"

    mutate_csv(small_raw, "olist_order_items_dataset.csv", edit)
    with pytest.raises(BuildFailedError) as error:
        build_synthetic(small_raw, tmp_path / "out")
    assert "schema:order_items" in failed_checks(error.value)


@pytest.mark.parametrize("score", ["0", "6", "great"])
def test_review_score_outside_1_to_5_is_rejected(
    small_raw: Path, tmp_path: Path, mutate_csv: Mutate, score: str
) -> None:
    def edit(rows: list[list[str]]) -> None:
        rows[1][rows[0].index("review_score")] = score

    mutate_csv(small_raw, "olist_order_reviews_dataset.csv", edit)
    with pytest.raises(BuildFailedError) as error:
        build_synthetic(small_raw, tmp_path / "out")
    assert "schema:order_reviews" in failed_checks(error.value)


def test_unparseable_timestamp_is_rejected(
    small_raw: Path, tmp_path: Path, mutate_csv: Mutate
) -> None:
    def edit(rows: list[list[str]]) -> None:
        rows[1][rows[0].index("order_purchase_timestamp")] = "15/06/2017 10:00"

    mutate_csv(small_raw, "olist_orders_dataset.csv", edit)
    with pytest.raises(BuildFailedError) as error:
        build_synthetic(small_raw, tmp_path / "out")
    assert "schema:orders" in failed_checks(error.value)


def test_unknown_order_status_is_rejected(
    small_raw: Path, tmp_path: Path, mutate_csv: Mutate
) -> None:
    def edit(rows: list[list[str]]) -> None:
        rows[1][rows[0].index("order_status")] = "teleported"

    mutate_csv(small_raw, "olist_orders_dataset.csv", edit)
    with pytest.raises(BuildFailedError) as error:
        build_synthetic(small_raw, tmp_path / "out")
    assert "schema:orders" in failed_checks(error.value)


def test_item_for_a_missing_order_is_rejected(
    small_raw: Path, tmp_path: Path, mutate_csv: Mutate
) -> None:
    def edit(rows: list[list[str]]) -> None:
        orphan = list(rows[1])
        orphan[rows[0].index("order_id")] = "order_that_does_not_exist"
        rows.append(orphan)

    mutate_csv(small_raw, "olist_order_items_dataset.csv", edit)
    with pytest.raises(BuildFailedError) as error:
        build_synthetic(small_raw, tmp_path / "out")
    assert "fk:order_items.order_id in orders" in failed_checks(error.value)


def test_published_row_counts_are_enforced_for_real_data(small_raw: Path, tmp_path: Path) -> None:
    """Synthetic data is exempt by default; asking for the check shows it really bites."""
    with pytest.raises(BuildFailedError) as error:
        build(
            raw_dir=small_raw,
            warehouse_path=tmp_path / "w.duckdb",
            reports_dir=tmp_path / "r",
            salt="s",
            enforce_row_counts=True,
        )
    assert any(name.startswith("row_count:") for name in failed_checks(error.value))


def test_failed_build_leaves_the_existing_warehouse_untouched(
    small_raw: Path, tmp_path: Path, mutate_csv: Mutate
) -> None:
    out = tmp_path / "out"
    good = build_synthetic(small_raw, out)
    before = hashlib.sha256(good.warehouse_path.read_bytes()).hexdigest()

    mutate_csv(small_raw, "olist_orders_dataset.csv", lambda rows: rows.append(list(rows[1])))
    with pytest.raises(BuildFailedError):
        build_synthetic(small_raw, out)

    assert hashlib.sha256(good.warehouse_path.read_bytes()).hexdigest() == before
    assert not list(out.glob("*.tmp*"))  # no half-built file left behind
    report = json.loads((out / "reports" / "quality_report.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed"  # the report now explains the failed run


def test_synthetic_data_cannot_overwrite_the_real_warehouse_path(
    small_raw: Path, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="SYNTHETIC"):
        build(
            raw_dir=small_raw,
            warehouse_path=get_settings().duckdb_path,
            reports_dir=tmp_path / "r",
            salt="s",
        )


# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------
def test_two_builds_of_the_same_inputs_are_identical(small_raw: Path, tmp_path: Path) -> None:
    first = build_synthetic(small_raw, tmp_path / "a")
    second = build_synthetic(small_raw, tmp_path / "b")
    assert first.data_hash == second.data_hash
    assert first.content_hash == second.content_hash
    assert first.report["mart_hashes"] == second.report["mart_hashes"]


def test_changing_the_salt_changes_the_hashes(small_raw: Path, tmp_path: Path) -> None:
    one = build_synthetic(small_raw, tmp_path / "a", salt="salt-one")
    two = build_synthetic(small_raw, tmp_path / "b", salt="salt-two")
    assert one.data_hash != two.data_hash
    assert one.content_hash != two.content_hash  # keys are salted, so the marts differ too


def test_changing_the_data_changes_the_content_hash(
    small_raw: Path, tmp_path: Path, mutate_csv: Mutate
) -> None:
    before = build_synthetic(small_raw, tmp_path / "a")

    def edit(rows: list[list[str]]) -> None:
        rows[1][rows[0].index("price")] = "999.99"

    mutate_csv(small_raw, "olist_order_items_dataset.csv", edit)
    after = build_synthetic(small_raw, tmp_path / "b")
    assert before.data_hash != after.data_hash
    assert before.content_hash != after.content_hash


# ---------------------------------------------------------------------------
# Quality report
# ---------------------------------------------------------------------------
def test_report_contains_what_fr006_requires(built: BuildResult) -> None:
    report = json.loads(built.report_json.read_text(encoding="utf-8"))
    assert report["status"] == "passed"
    assert len(report["data_hash"]) == 64 and len(report["warehouse_content_hash"]) == 64
    assert report["tables"]["marts.fct_orders"]["rows"] > 0  # row counts
    assert "order_gmv" in report["tables"]["marts.fct_orders"]["null_rate"]  # null rates
    levels = {c["level"] for c in report["checks"]}
    assert {"hard", "soft", "stat"} <= levels  # failed checks are visible
    assert all(c["passed"] for c in report["checks"] if c["level"] == "hard")


def test_report_is_labelled_synthetic_and_html_is_written(built: BuildResult) -> None:
    assert built.report["synthetic"] is True
    html = built.report_html.read_text(encoding="utf-8")
    assert "SYNTHETIC (FAKE) DATA" in html and built.data_hash in html


def test_report_statistics_describe_the_planted_issues(built: BuildResult) -> None:
    stats = {c["name"]: c["observed"] for c in built.report["checks"] if c["level"] == "stat"}
    assert stats["orders_without_items"] >= 1
    assert stats["orders_without_payment"] >= 1
    assert stats["delivered_status_without_delivery_date"] >= 1
    assert stats["orders_with_multiple_reviews"] >= 1
    assert stats["review_ids_shared_across_orders"] >= 1
    assert stats["reviews_raw_rows"] > stats["reviews_after_dedup"]
    assert stats["products_with_null_category"] >= 1
    assert stats["products_with_untranslated_category"] >= 1
    assert stats["payments_not_defined"] >= 1


def test_soft_failures_are_reported_but_do_not_stop_the_build(built: BuildResult) -> None:
    soft = {c["name"]: c["passed"] for c in built.report["checks"] if c["level"] == "soft"}
    assert built.report["status"] == "passed"
    assert False in soft.values()  # the synthetic data trips at least one soft check, on purpose


def test_raw_data_is_not_modified_by_the_build(synthetic_raw: Path, built: BuildResult) -> None:
    recorded = json.loads((synthetic_raw / "checksums.json").read_text(encoding="utf-8"))["files"]
    for filename, digest in recorded.items():
        assert hashlib.sha256((synthetic_raw / filename).read_bytes()).hexdigest() == digest
