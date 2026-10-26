"""Volume-collapse guard and the last reliable day (change A, ADR-phase3b section 8).

Rules, fixed before any 3b result existed and not to be tuned:

* A day is a **collapse day** if total orders are below 35% of the median total orders of the
  previous 28 days.
* On a collapse day the guard raises **one** alert, "volume collapse (data feed or business)"
  (KPI `orders`, dimension `data_quality`), and suppresses every other test that day.
* The **last reliable day** is the day before the terminal run of consecutive collapse days that
  ends at the last day of the data. Days after it are not tested and raise no alert; they are
  logged as suppressed. This is a *validity rule defined after diagnosis*: it was written after
  the end-of-August-2018 drop had been seen. Its value is recorded in `config/detection.yaml`
  (`feed_guard.data_end`) and `python -m ds.feed_guard --check` recomputes it from the warehouse.

The alert wording does not claim a cause: a collapse can be the feed or the business.

    uv run python -m ds.feed_guard --check
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from ds.catalogue import FeedGuardConfig, load_catalogue, load_detection_config

COLLAPSE_FRACTION = 0.35
NORM_DAYS = 28
GUARD_KPI = "orders"
GUARD_DIMENSION = "data_quality"
GUARD_SEGMENT = "volume_collapse"
GUARD_LABEL = "volume collapse (data feed or business)"
TOTAL_ORDERS_ID = "orders|total|all|daily"


def trailing_norm(totals: pd.Series, norm_days: int = NORM_DAYS) -> pd.Series:
    """Median of the previous `norm_days` days (the day itself excluded)."""
    return totals.rolling(norm_days, min_periods=norm_days).median().shift(1)


def collapse_days(
    totals: pd.Series, fraction: float = COLLAPSE_FRACTION, norm_days: int = NORM_DAYS
) -> pd.Series:
    """True for days whose total is below `fraction` of the median of the previous `norm_days` days."""
    return (totals < fraction * trailing_norm(totals, norm_days)).fillna(False)


def last_reliable_day(
    totals: pd.Series, fraction: float = COLLAPSE_FRACTION, norm_days: int = NORM_DAYS
) -> date:
    """The day before the terminal run of collapse days at the end of the data.

    `totals` is total orders per day, indexed by date, with no gaps. If the last day is not a
    collapse day the data does not end in a collapse and the last day itself is returned.
    """
    flags = collapse_days(totals.sort_index(), fraction, norm_days)
    index = [pd.Timestamp(i).date() for i in flags.index]
    values = flags.to_list()
    position = len(values)
    while position > 0 and values[position - 1]:
        position -= 1
    if position == len(values):
        return index[-1]
    return index[position - 1] if position > 0 else index[0]


def total_orders(series: pd.DataFrame) -> pd.Series:
    """Total orders per day from a `load_series` frame, indexed by date."""
    rows = series[series["unique_id"] == TOTAL_ORDERS_ID]
    return pd.Series(
        rows["y"].to_numpy(), index=pd.to_datetime(rows["ds"]).dt.date, dtype=float
    ).sort_index()


@dataclass
class GuardResult:
    """What the guard decided for a list of days."""

    testable_days: list[date]
    alerts: list[dict[str, Any]] = field(default_factory=list)
    collapse: list[dict[str, Any]] = field(default_factory=list)  # days with a guard alert
    past_data_end: list[date] = field(default_factory=list)  # silently suppressed days


def guard_alert(day: date, observed: float, norm: float, fraction: float) -> dict[str, Any]:
    """An anomaly record for a collapse day, in the same columns as a statistical anomaly."""
    relative = (norm - observed) / norm if norm else 1.0
    return {
        "anomaly_id": f"guard-{day.isoformat()}",
        "date": day,
        "kpi": GUARD_KPI,
        "granularity": "daily",
        "dimension": GUARD_DIMENSION,
        "segment": GUARD_SEGMENT,
        "observed": float(observed),
        "expected": float(norm),
        "lower": float(fraction * norm),
        "upper": float("nan"),
        "z": float("nan"),
        "p_value": float("nan"),
        "q_value": float("nan"),
        "n": int(observed),
        "severity": float(abs(relative) * math.log(max(norm, 2.0))),
        "direction": "down",
        "related_segments": "[]",
        "model": "feed_guard",
    }


def apply_guard(totals: pd.Series, days: list[date], config: FeedGuardConfig) -> GuardResult:
    """Decide, for each day, whether it is tested, raises a guard alert, or is silently suppressed."""
    norm = trailing_norm(totals, config.norm_days)
    result = GuardResult(testable_days=[])
    for day in days:
        if config.data_end is not None and day > config.data_end:
            result.past_data_end.append(day)
            continue
        observed = totals.get(day)
        baseline = norm.get(day)
        if (
            observed is not None
            and baseline is not None
            and not np.isnan(baseline)
            and observed < config.collapse_fraction * baseline
        ):
            result.alerts.append(
                guard_alert(day, float(observed), float(baseline), config.collapse_fraction)
            )
            result.collapse.append(
                {"date": day, "observed": float(observed), "norm": float(baseline)}
            )
            continue
        result.testable_days.append(day)
    return result


def is_guard_alert(frame: pd.DataFrame) -> pd.Series:
    """Boolean mask of guard alerts in an anomaly frame."""
    return (frame["dimension"] == GUARD_DIMENSION) & (frame["segment"] == GUARD_SEGMENT)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Recompute the last reliable day and check the config."
    )
    parser.add_argument("--check", action="store_true", help="fail if feed_guard.data_end differs")
    parser.parse_args(argv)
    from core.settings import get_settings
    from ds.forecasting import load_series

    cfg = load_detection_config()
    con = duckdb.connect(str(get_settings().duckdb_path), read_only=True)
    try:
        totals = total_orders(load_series(con, load_catalogue()))
    finally:
        con.close()
    computed = last_reliable_day(totals, cfg.feed_guard.collapse_fraction, cfg.feed_guard.norm_days)
    flags = collapse_days(totals, cfg.feed_guard.collapse_fraction, cfg.feed_guard.norm_days)
    tail = [d for d, f in flags.items() if f and d > computed]
    print(
        f"rule: total orders < {cfg.feed_guard.collapse_fraction:.0%} of the median of the previous "
        f"{cfg.feed_guard.norm_days} days"
    )
    print(
        f"computed last reliable day: {computed}   (collapse days after it: {[str(d) for d in tail]})"
    )
    print(f"config feed_guard.data_end:  {cfg.feed_guard.data_end}")
    if computed != cfg.feed_guard.data_end:
        print("MISMATCH: update config/detection.yaml and docs/DATA_PLAN.md", file=sys.stderr)
        return 1
    print("OK: the configured date is what the rule computes from the warehouse")
    return 0


if __name__ == "__main__":
    sys.exit(main())
