"""Reporting helpers for the detection evaluation (ADR-phase3b sections 7, 8, 12). Nothing here
changes what the detector alerts on; it only labels alarms after the fact.

* Known events are compared as plain calendar dates in Brazilian local time. Olist timestamps
  have no timezone and nothing converts one, so there is no UTC shift. A datetime or a
  timezone-aware value is rejected rather than silently shifted.
* `last_reliable_day` implements the validity rule of ADR section 8, a rule defined after
  diagnosis of the end-of-data drop.
* `classify` assigns each alarm to exactly one cause, in a fixed priority order.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict

from core.settings import REPO_ROOT
from ds.calendar import CalendarWindow, window_on

KNOWN_EVENTS_PATH = REPO_ROOT / "config" / "known_events.yaml"
COLLAPSE_FRACTION = 0.35  # ADR section 8: below 35% of the trailing norm is a collapse day
NORM_DAYS = 28
CAUSES = ("dataset_cutoff", "calendar_window", "known_event", "gmv_spike", "other")


# ---------------------------------------------------------------------------
# Known events
# ---------------------------------------------------------------------------
class KnownEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    start: date
    end: date
    kpis: Literal["all"] | list[str]
    note: str = ""

    def matches(self, day: date, kpi: str) -> bool:
        _require_local_date(day)
        in_range = self.start <= day <= self.end
        return in_range and (self.kpis == "all" or kpi in self.kpis)


def _require_local_date(day: object) -> None:
    """Reject datetimes (including pandas Timestamps) so no timezone can sneak in."""
    if isinstance(day, datetime) or not isinstance(day, date):
        raise TypeError(
            "pass a calendar date (Brazilian local date); datetimes and timezone-aware values "
            f"are not accepted, got {type(day).__name__}"
        )


def load_known_events(path: Path = KNOWN_EVENTS_PATH) -> list[KnownEvent]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [KnownEvent.model_validate(e) for e in data["events"]]


def matching_events(events: Sequence[KnownEvent], day: date, kpi: str) -> list[str]:
    return [e.id for e in events if e.matches(day, kpi)]


# ---------------------------------------------------------------------------
# Last reliable day
# ---------------------------------------------------------------------------
def collapse_days(totals: pd.Series) -> pd.Series:
    """True for days whose total is below 35% of the median of the previous 28 days."""
    norm = totals.rolling(NORM_DAYS, min_periods=NORM_DAYS).median().shift(1)
    return (totals < COLLAPSE_FRACTION * norm).fillna(False)


def last_reliable_day(totals: pd.Series) -> date:
    """The day before the terminal run of collapse days at the end of the data.

    `totals` is total orders per day, indexed by date, with no gaps. If the last day is not a
    collapse day the data does not end in a collapse and the last day itself is returned.
    """
    flags = collapse_days(totals.sort_index())
    index = [pd.Timestamp(i).date() for i in flags.index]
    values = flags.to_list()
    position = len(values)
    while position > 0 and values[position - 1]:
        position -= 1
    if position == len(values):
        return index[-1]
    return index[position - 1] if position > 0 else index[0]


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ClassifyContext:
    last_reliable: date | None
    windows: list[CalendarWindow]
    events: list[KnownEvent]


def classify(day: date, kpi: str, direction: str, ctx: ClassifyContext) -> str:
    """One cause per alarm. Priority: cut-off, calendar window, known event, GMV spike, other."""
    _require_local_date(day)
    if ctx.last_reliable is not None and day > ctx.last_reliable:
        return "dataset_cutoff"
    if window_on(day, ctx.windows) is not None:
        return "calendar_window"
    if matching_events(ctx.events, day, kpi):
        return "known_event"
    if kpi == "gmv" and direction == "up":
        return "gmv_spike"
    return "other"


def breakdown(anoms: pd.DataFrame, ctx: ClassifyContext) -> dict[str, Any]:
    """Exclusive counts per cause, plus the separate non-exclusive known-event figure."""
    counts = dict.fromkeys(CAUSES, 0)
    event_matches = 0
    for r in anoms.itertuples():
        counts[classify(r.date, r.kpi, r.direction, ctx)] += 1
        if matching_events(ctx.events, r.date, r.kpi):
            event_matches += 1
    return {
        "exclusive": counts,
        "matches_unlabelled_real_event": event_matches,
        "total": len(anoms),
        "alarm_days": int(anoms["date"].nunique()) if len(anoms) else 0,
    }


# ---------------------------------------------------------------------------
# Intervals
# ---------------------------------------------------------------------------
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion k/n. (0, 0) when n is 0."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def fraction(k: int, n: int) -> dict[str, Any]:
    low, high = wilson(k, n)
    return {"k": k, "n": n, "rate": (k / n) if n else None, "ci_low": low, "ci_high": high}
