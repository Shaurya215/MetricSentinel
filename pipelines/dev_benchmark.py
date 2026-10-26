"""The 2017 development benchmark (ADR-phase3b section 5): 54 scenarios, seed 3101.

    uv run python -m pipelines.dev_benchmark          (make scenarios-dev2017)

Grid (fixed before any result existed):

    segment level shift      3 sizes x 3 magnitudes (state / category alternating)       9
    category price change    3 sizes x 3 magnitudes                                      9
    review collapse          3 sizes x 3 magnitudes                                      9
    payment failure          4 payment types x 3 magnitudes                            12
    seller delay             3 seller-state sizes x delays of 3, 7, 14 days              9
    pipeline bug             {drop, duplicate} x shares 0.3, 0.6, 1.0                    6
                                                                                       --
                                                                                       54

Size bands by the target's share of orders in the test period: large >= 5%, medium
1% to under 5%, small under 1%. Magnitudes 0.15, 0.30, 0.60. Duration 14 days (1 day
for a pipeline bug). Start dates are drawn by the seed from the days on which the
fault window plus its 7-day matching tail does not overlap Black Friday week or the
Christmas window, and ends by the last day of the test period.
"""

from __future__ import annotations

import argparse
import random
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

import duckdb

from core.settings import REPO_ROOT, get_settings
from ds.calendar import CalendarWindow, calendar_windows
from ds.catalogue import load_detection_config
from pipelines.faults import AFFECTED_KPIS, FaultType, Scenario, save_scenarios

DEV_SCENARIOS_PATH = REPO_ROOT / "config" / "scenarios" / "dev2017.yaml"
MAGNITUDES = (0.15, 0.30, 0.60)
DELAYS = (3.0, 7.0, 14.0)
PAYMENT_TYPES = ("credit_card", "boleto", "voucher", "debit_card")
PIPELINE_BUGS: tuple[tuple[Literal["drop", "duplicate"], float], ...] = tuple(
    (mode, share) for mode in ("drop", "duplicate") for share in (0.3, 0.6, 1.0)
)
SIZES = ("large", "medium", "small")
TAIL_DAYS = 7
DURATION = 14
EXCLUDED_VALUES = {"no_items", "no_payment", "unknown", "not_defined"}
GRID_COUNTS = {
    "segment_level_shift": 9,
    "category_price_change": 9,
    "review_collapse": 9,
    "payment_failure": 12,
    "seller_delay": 9,
    "pipeline_bug": 6,
}


def size_band(share: float) -> str:
    return "large" if share >= 0.05 else "medium" if share >= 0.01 else "small"


def order_shares(
    con: duckdb.DuckDBPyConnection, column: str, start: date, end: date
) -> dict[str, float]:
    """Share of all orders in [start, end] whose dimension `column` takes each value."""
    rows = con.execute(
        f"SELECT d.{column}, count(*) FROM metrics.order_dims d JOIN marts.fct_orders o USING (order_id) "
        f"WHERE o.purchase_date BETWEEN ? AND ? GROUP BY 1",
        [start, end],
    ).fetchall()
    total = sum(n for _, n in rows)
    return {value: n / total for value, n in rows if value not in EXCLUDED_VALUES}


def seller_state_shares(con: duckdb.DuckDBPyConnection, start: date, end: date) -> dict[str, float]:
    """Share of all orders in [start, end] whose most expensive item is sold by a seller in each state."""
    total = con.execute(
        "SELECT count(*) FROM marts.fct_orders WHERE purchase_date BETWEEN ? AND ?", [start, end]
    ).fetchall()[0][0]
    rows = con.execute(
        """
        WITH prim AS (
            SELECT order_id, seller_id,
                   row_number() OVER (PARTITION BY order_id ORDER BY price DESC, item_seq) AS rn
            FROM marts.fct_order_items
        )
        SELECT s.state, count(*) FROM prim p
        JOIN marts.dim_sellers s USING (seller_id)
        JOIN marts.fct_orders o USING (order_id)
        WHERE p.rn = 1 AND o.purchase_date BETWEEN ? AND ? GROUP BY 1
        """,
        [start, end],
    ).fetchall()
    return {state: n / total for state, n in rows if state}


def by_band(shares: dict[str, float]) -> dict[str, list[str]]:
    bands: dict[str, list[str]] = {size: [] for size in SIZES}
    for value in sorted(shares):
        bands[size_band(shares[value])].append(value)
    return bands


def allowed_starts(
    first: date, last: date, span_days: int, blocks: list[CalendarWindow]
) -> list[date]:
    """Start days whose effect span plus the matching tail ends by `last` and avoids every block."""
    days: list[date] = []
    day = first
    while True:
        end = day + timedelta(days=span_days - 1 + TAIL_DAYS)
        if end > last:
            break
        if all(end < b.start or day > b.end for b in blocks):
            days.append(day)
        day += timedelta(days=1)
    return days


class _Builder:
    def __init__(self, con: duckdb.DuckDBPyConnection, seed: int) -> None:
        cfg = load_detection_config()
        assert cfg.development is not None, "config/detection.yaml has no development protocol"
        self.dev = cfg.development
        self.seed = seed
        self.rng = random.Random(seed)
        test = self.dev.test
        self.test = test
        self.shares = {
            dim: order_shares(con, dim, test.start, test.end)
            for dim in ("customer_state", "category_en", "payment_type")
        }
        self.shares["seller_state"] = seller_state_shares(con, test.start, test.end)
        self.bands = {dim: by_band(s) for dim, s in self.shares.items()}
        windows = calendar_windows(test.start.year, test.end.year)
        self.blocks = [
            w
            for w in windows
            if w.name in ("black_friday_week", "christmas_new_year")
            and not (w.end < self.dev.first_fault_start or w.start > test.end)
        ]
        self.scenarios: list[Scenario] = []
        self.used: dict[tuple[str, str, str], set[str]] = {}

    def pick(self, fault: str, dimension: str, size: str) -> str:
        candidates = self.bands[dimension][size]
        if not candidates:
            raise RuntimeError(f"no {size} value in {dimension} for the test period")
        used = self.used.setdefault((fault, dimension, size), set())
        fresh = [c for c in candidates if c not in used] or candidates
        value = self.rng.choice(fresh)
        used.add(value)
        return value

    def start_day(self, span: int) -> date:
        options = allowed_starts(self.dev.first_fault_start, self.test.end, span, self.blocks)
        if not options:
            raise RuntimeError(f"no allowed start for a {span}-day span")
        return self.rng.choice(options)

    def add(
        self,
        fault: FaultType,
        dimension: str,
        value: str,
        magnitude: float,
        *,
        mode: Literal["drop", "duplicate"] | None = None,
        duration: int = DURATION,
        size: str,
        share: float | None,
    ) -> None:
        idx = len(self.scenarios) + 1
        extra = int(magnitude) if fault == "seller_delay" else 0  # a delay shifts deliveries later
        self.scenarios.append(
            Scenario(
                id=f"S{idx:02d}-{fault}",
                fault_type=fault,
                dimension=dimension,
                value=value,
                start=self.start_day(duration + extra),
                duration_days=duration,
                magnitude=magnitude,
                mode=mode,
                seed=self.seed * 1000 + idx,
                affected_kpis=AFFECTED_KPIS[fault],
                size_tier=size,
                target_share=None if share is None else round(share, 6),
            )
        )


def generate_dev2017(con: duckdb.DuckDBPyConnection, *, seed: int | None = None) -> list[Scenario]:
    cfg = load_detection_config()
    assert cfg.development is not None
    b = _Builder(con, cfg.development.benchmark_seed if seed is None else seed)

    for i, (size, mag) in enumerate((s, m) for s in SIZES for m in MAGNITUDES):
        dim = "customer_state" if i % 2 == 0 else "category_en"
        value = b.pick("segment_level_shift", dim, size)
        b.add(
            "segment_level_shift",
            dim,
            value,
            mag,
            mode="drop",
            size=size,
            share=b.shares[dim][value],
        )
    for fault in ("category_price_change", "review_collapse"):
        for size, mag in ((s, m) for s in SIZES for m in MAGNITUDES):
            value = b.pick(fault, "category_en", size)  # type: ignore[arg-type]
            b.add(fault, "category_en", value, mag, size=size, share=b.shares["category_en"][value])  # type: ignore[arg-type]
    for pay in PAYMENT_TYPES:
        share = b.shares["payment_type"].get(pay)
        if share is None:
            raise RuntimeError(f"payment type {pay} has no orders in the test period")
        for mag in MAGNITUDES:
            b.add("payment_failure", "payment_type", pay, mag, size=size_band(share), share=share)
    for size in SIZES:
        for delay in DELAYS:
            state = b.pick("seller_delay", "seller_state", size)
            b.add(
                "seller_delay",
                "seller_state",
                state,
                delay,
                size=size,
                share=b.shares["seller_state"][state],
            )
    for mode, share in PIPELINE_BUGS:
        b.add(
            "pipeline_bug",
            "data_quality",
            "fct_orders",
            share,
            mode=mode,
            duration=1,
            size="total",
            share=1.0,
        )

    counts: dict[str, int] = {}
    for s in b.scenarios:
        counts[s.fault_type] = counts.get(s.fault_type, 0) + 1
    assert counts == GRID_COUNTS and len(b.scenarios) == 54, counts
    return b.scenarios


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the 2017 development benchmark.")
    parser.add_argument("--out", type=Path, default=DEV_SCENARIOS_PATH)
    args = parser.parse_args()
    cfg = load_detection_config()
    assert cfg.development is not None
    con = duckdb.connect(str(get_settings().duckdb_path), read_only=True)
    try:
        scenarios = generate_dev2017(con)
        data_hash = con.execute("SELECT value FROM meta.build WHERE key = 'data_hash'").fetchall()[
            0
        ][0]
    finally:
        con.close()
    save_scenarios(
        scenarios,
        args.out,
        seed=cfg.development.benchmark_seed,
        data_hash=data_hash,
        description=(
            "2017 development benchmark (ADR-phase3b section 5): 54 scenarios, test period "
            f"{cfg.development.test.start} to {cfg.development.test.end}. Generated by "
            "`python -m pipelines.dev_benchmark`; do not edit by hand."
        ),
    )
    print(f"Wrote {len(scenarios)} scenarios to {args.out}")


if __name__ == "__main__":
    main()
