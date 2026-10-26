"""Load and validate `config/metrics.yaml` (FR-010) and `config/detection.yaml`.

Both files are parsed into Pydantic models, so a typo or a missing field fails at
load time instead of producing a silently wrong metric.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.settings import REPO_ROOT

CATALOGUE_PATH = REPO_ROOT / "config" / "metrics.yaml"
DETECTION_PATH = REPO_ROOT / "config" / "detection.yaml"

Source = Literal["orders", "deliveries", "reviews"]
Direction = Literal["up", "down"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Dimension(_Strict):
    description: str


class Kpi(_Strict):
    name: str
    description: str
    unit: str
    family: Literal["count", "sum", "ratio", "rate", "mean"]
    source: Source
    filter: str | None = None
    numerator: str
    denominator: str | None = None
    sample: str
    bad_direction: Direction
    alert_direction: Literal["up", "down", "both"]
    min_sample: int = Field(ge=1)
    owner: str

    @model_validator(mode="after")
    def _denominator_matches_family(self) -> Kpi:
        additive = self.family in ("count", "sum")
        if additive and self.denominator is not None:
            raise ValueError(f"{self.name}: a {self.family} KPI has no denominator")
        if not additive and self.denominator is None:
            raise ValueError(f"{self.name}: a {self.family} KPI needs a denominator")
        return self

    @property
    def is_additive(self) -> bool:
        return self.denominator is None


class Catalogue(_Strict):
    version: int
    dimensions: dict[str, Dimension]
    kpis: list[Kpi]

    @model_validator(mode="after")
    def _unique_names(self) -> Catalogue:
        names = [k.name for k in self.kpis]
        if len(names) != len(set(names)):
            raise ValueError("KPI names must be unique")
        return self

    def kpi(self, name: str) -> Kpi:
        for k in self.kpis:
            if k.name == name:
                return k
        raise KeyError(name)


class DateRange(_Strict):
    start: date
    end: date

    @model_validator(mode="after")
    def _ordered(self) -> DateRange:
        if self.end < self.start:
            raise ValueError("end is before start")
        return self

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end


class Event(DateRange):
    name: str


class SampleRule(_Strict):
    coverage: float = Field(gt=0, le=1)


class ForecastConfig(_Strict):
    refit_every_days: int = Field(ge=1)
    interval_level: int = Field(gt=50, lt=100)
    daily_season_length: int = Field(ge=1)
    weekly_season_length: int = Field(ge=1)


class DetectionRule(_Strict):
    fdr_q: float = Field(gt=0, lt=1)


IMPLEMENTED_FLAGS = frozenset({"feed_guard", "log_gmv", "share_test", "calendar_downgrade"})


class FeedGuardConfig(_Strict):
    """Parameters of change A (ADR-phase3b section 8). Fixed in advance; not to be tuned."""

    collapse_fraction: float = Field(default=0.35, gt=0, lt=1)
    norm_days: int = Field(default=28, ge=7)
    # Last reliable day: computed by the rule in ds/feed_guard.py, checked by `--check`.
    data_end: date | None = None


class ImprovementFlags(_Strict):
    """Phase 3b switches (docs/decisions/ADR-phase3b.md). All off reproduces Phase 3 exactly.

    A flag is rejected until its change is implemented, so a flag can never be switched on
    silently doing nothing.
    """

    feed_guard: bool = False  # A: volume-collapse guard and last reliable day
    log_gmv: bool = False  # B: forecast GMV on a log scale
    share_test: bool = False  # C: test segments as a share of the parent total
    calendar_downgrade: bool = False  # D: tag calendar windows, lower severity

    @model_validator(mode="after")
    def _only_implemented(self) -> ImprovementFlags:
        pending = [
            name
            for name in type(self).model_fields
            if getattr(self, name) and name not in IMPLEMENTED_FLAGS
        ]
        if pending:
            raise ValueError(f"not implemented yet: {', '.join(pending)}")
        return self


class DevelopmentProtocol(_Strict):
    """The 2017-only development protocol (ADR-phase3b section 4)."""

    fit: DateRange
    test: DateRange
    benchmark_seed: int
    first_fault_start: date


class DetectionConfig(_Strict):
    monitoring_window: DateRange
    backtest: DateRange
    evaluation: DateRange
    sample_rule: SampleRule
    forecast: ForecastConfig
    detection: DetectionRule
    events: list[Event] = []
    improvements: ImprovementFlags = Field(default_factory=ImprovementFlags)
    feed_guard: FeedGuardConfig = Field(default_factory=FeedGuardConfig)
    development: DevelopmentProtocol | None = None

    @model_validator(mode="after")
    def _periods_do_not_overlap(self) -> DetectionConfig:
        if self.backtest.end >= self.evaluation.start:
            raise ValueError("the backtest period must end before the evaluation period starts")
        window = self.monitoring_window
        for name, period in (("backtest", self.backtest), ("evaluation", self.evaluation)):
            if period.start < window.start or period.end > window.end:
                raise ValueError(f"{name} period must lie inside the monitoring window")
        dev = self.development
        if dev is not None and not (
            self.backtest.start <= dev.fit.start
            and dev.fit.end < dev.test.start
            and dev.test.end <= self.backtest.end
        ):
            raise ValueError(
                "development fit/test periods must lie inside the 2017 backtest period, fit before test"
            )
        return self

    def event_on(self, day: date) -> Event | None:
        return next((e for e in self.events if e.contains(day)), None)


@lru_cache
def load_catalogue(path: Path = CATALOGUE_PATH) -> Catalogue:
    return Catalogue.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


@lru_cache
def load_detection_config(path: Path = DETECTION_PATH) -> DetectionConfig:
    return DetectionConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def with_flags(cfg: DetectionConfig, names: list[str] | tuple[str, ...]) -> DetectionConfig:
    """A copy of the configuration with the named 3b flags switched on."""
    unknown = set(names) - set(ImprovementFlags.model_fields)
    if unknown:
        raise ValueError(f"unknown flag(s): {sorted(unknown)}")
    return cfg.model_copy(update={"improvements": ImprovementFlags(**dict.fromkeys(names, True))})


def transforms_for(cfg: DetectionConfig) -> dict[str, str]:
    """KPIs forecast on a transformed scale under the given flags (change B: GMV only)."""
    return {"gmv": "log1p"} if cfg.improvements.log_gmv else {}


def share_kpis_for(cfg: DetectionConfig) -> tuple[str, ...]:
    """Additive KPIs whose segment series are tested as a share of the total (change C)."""
    return ("orders", "gmv") if cfg.improvements.share_test else ()
