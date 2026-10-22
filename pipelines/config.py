"""Constants for the data pipeline: source files, expected sizes, thresholds.

Thresholds below were set from the first real-data run (data_hash 97f3df47...,
see docs/DATA_PLAN.md section 14). Re-check them if the dataset ever changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PIPELINES_DIR = Path(__file__).resolve().parent
SQL_DIR = PIPELINES_DIR / "sql_models"

KAGGLE_DATASET = "olistbr/brazilian-ecommerce"
KAGGLE_DOWNLOAD_URL = f"https://www.kaggle.com/api/v1/datasets/download/{KAGGLE_DATASET}"
CHECKSUMS_FILENAME = "checksums.json"


@dataclass(frozen=True)
class SourceTable:
    """One Olist CSV file and the `raw` table it is loaded into."""

    table: str  # name inside the `raw` schema
    filename: str
    expected_rows: int  # published row count; the hard row-count check allows +/-1%


# Row counts are the published Olist counts (Kaggle dataset description), used as the
# reference for the +/-1% hard check in DATA_PLAN.md section 8.
SOURCE_TABLES: tuple[SourceTable, ...] = (
    SourceTable("orders", "olist_orders_dataset.csv", 99_441),
    SourceTable("order_items", "olist_order_items_dataset.csv", 112_650),
    SourceTable("order_payments", "olist_order_payments_dataset.csv", 103_886),
    SourceTable("order_reviews", "olist_order_reviews_dataset.csv", 99_224),
    SourceTable("customers", "olist_customers_dataset.csv", 99_441),
    SourceTable("sellers", "olist_sellers_dataset.csv", 3_095),
    SourceTable("products", "olist_products_dataset.csv", 32_951),
    SourceTable("geolocation", "olist_geolocation_dataset.csv", 1_000_163),
    SourceTable("category_translation", "product_category_name_translation.csv", 71),
)
TABLES_BY_NAME = {t.table: t for t in SOURCE_TABLES}
ROW_COUNT_TOLERANCE = 0.01

ORDER_STATUSES = frozenset(
    {
        "created",
        "approved",
        "invoiced",
        "processing",
        "shipped",
        "delivered",
        "unavailable",
        "canceled",
    }
)
PAYMENT_TYPES = frozenset({"credit_card", "boleto", "voucher", "debit_card", "not_defined"})

# ---- Soft-check thresholds, set from the real-data EDA ----
# Observed: 8 of 96,478 delivered orders (0.0083%) lack a delivery date; limit ~12x that.
SOFT_MAX_DELIVERED_MISSING_DATE_RATE = 0.001
# Observed: 1.44% of items map to category 'unknown'; limit ~1.7x that.
SOFT_MAX_UNKNOWN_CATEGORY_SHARE = 0.025
# Dense monitoring window found by the EDA (pipelines/eda.py); used by the "no gaps" soft check.
MONITORING_WINDOW = ("2017-01-26", "2018-08-31")

SELLER_TIER_WINDOW_DAYS = 90
REVIEW_TEXT_MAX_CHARS = 500
