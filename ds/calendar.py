"""Calendar windows from the calendar alone (ADR-phase3b section 9).

Defined before any evaluation and independent of the data:

* Christmas and New Year: 20 December to 7 January.
* Carnival: the Saturday before Carnival Tuesday to Ash Wednesday (Carnival Tuesday is 47
  days before Easter).
* Black Friday week: the Thursday before Black Friday to the Wednesday after. Black Friday
  is the Friday after the fourth Thursday of November.

All values are plain calendar dates in Brazilian local time. Olist timestamps carry no
timezone, and nothing here converts one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from dateutil.easter import easter


@dataclass(frozen=True)
class CalendarWindow:
    name: str
    start: date
    end: date

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end


def black_friday(year: int) -> date:
    thursdays = [date(year, 11, d) for d in range(1, 31) if date(year, 11, d).weekday() == 3]
    return thursdays[3] + timedelta(days=1)


def carnival_tuesday(year: int) -> date:
    return easter(year) - timedelta(days=47)


def calendar_windows(first_year: int, last_year: int) -> list[CalendarWindow]:
    """Every window that starts or ends in [first_year, last_year], sorted by start."""
    windows: list[CalendarWindow] = []
    for year in range(first_year - 1, last_year + 1):
        windows.append(
            CalendarWindow("christmas_new_year", date(year, 12, 20), date(year + 1, 1, 7))
        )
    for year in range(first_year, last_year + 1):
        tue = carnival_tuesday(year)
        windows.append(CalendarWindow("carnival", tue - timedelta(days=3), tue + timedelta(days=1)))
        bf = black_friday(year)
        windows.append(
            CalendarWindow("black_friday_week", bf - timedelta(days=1), bf + timedelta(days=5))
        )
    return sorted(windows, key=lambda w: w.start)


def window_on(day: date, windows: list[CalendarWindow]) -> CalendarWindow | None:
    return next((w for w in windows if w.contains(day)), None)
