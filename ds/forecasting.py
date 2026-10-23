"""Forecasts with prediction intervals for the monitored series (FR-030, FR-035).

Re-fit cadence: models are re-fitted every Monday ("origin") on all data BEFORE the
origin, then forecast the next 7 days (daily series) or the next week (weekly series,
whose dates are week-ending Sundays). The forecast for day D therefore never uses D or
anything after it.

Before fitting, the history is cleaned: values on known event days (config/detection.yaml)
and on days whose sample is below the KPI's minimum are set missing, then filled by
interpolation. Black Friday would otherwise inflate every later interval.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

import duckdb
import numpy as np
import pandas as pd
from statsforecast import StatsForecast
from statsforecast.models import AutoARIMA, AutoETS, AutoTheta, SeasonalNaive

from ds.catalogue import Catalogue, DetectionConfig

DAILY_CANDIDATES = ("SeasonalNaive", "AutoETS", "AutoTheta", "AutoARIMA")
WEEKLY_CANDIDATES = ("SeasonalNaive", "AutoETS", "AutoTheta")
SEP = "|"


def series_id(kpi: str, dimension: str, segment: str, granularity: str) -> str:
    return SEP.join((kpi, dimension, segment, granularity))


def split_id(uid: str) -> tuple[str, str, str, str]:
    kpi, dimension, segment, granularity = uid.split(SEP)
    return kpi, dimension, segment, granularity


def make_model(name: str, season_length: int):  # type: ignore[no-untyped-def]
    if name == "SeasonalNaive":
        return SeasonalNaive(season_length=season_length)
    if name == "AutoETS":
        return AutoETS(season_length=season_length)
    if name == "AutoTheta":
        return AutoTheta(season_length=season_length)
    if name == "AutoARIMA":
        return AutoARIMA(season_length=season_length)
    raise ValueError(f"unknown model {name}")


def mondays(start: date, end: date) -> list[date]:
    """Every Monday from the Monday on or before `start` up to `end`."""
    first = start - timedelta(days=start.weekday())
    out, day = [], first
    while day <= end:
        out.append(day)
        day += timedelta(days=7)
    return out


def load_series(con: duckdb.DuckDBPyConnection, catalogue: Catalogue) -> pd.DataFrame:
    """All monitored observations: unique_id, ds, y (value), n, kpi, granularity, min_n."""
    frame = con.execute(
        "SELECT kpi, dimension, segment, granularity, date AS ds, value AS y, n "
        "FROM metrics.series ORDER BY kpi, dimension, segment, granularity, date"
    ).df()
    frame["ds"] = pd.to_datetime(frame["ds"])
    frame["unique_id"] = (
        frame["kpi"]
        + SEP
        + frame["dimension"]
        + SEP
        + frame["segment"]
        + SEP
        + frame["granularity"]
    )
    # Minimum sample for TESTING and TRAINING a value. It applies to ratio, rate and mean
    # KPIs, whose value is noisy when the denominator is small. For count and sum KPIs the
    # sample IS the value: a day with 0 orders is a real (and alarming) observation, so the
    # gate is 0. (The catalogue min_sample still decides daily/weekly/pooled in the plan.)
    frame["min_n"] = frame["kpi"].map(
        {k.name: 0 if k.is_additive else k.min_sample for k in catalogue.kpis}
    )
    return frame


def clean_history(frame: pd.DataFrame, cfg: DetectionConfig) -> pd.DataFrame:
    """Missing-out event days and small-sample days, then interpolate within each series."""
    out = frame.copy()
    bad = out["n"] < out["min_n"]
    for event in cfg.events:
        in_event = (out["ds"] >= pd.Timestamp(event.start)) & (out["ds"] <= pd.Timestamp(event.end))
        if "granularity" in out:
            # a weekly value is affected when its week overlaps the event
            weekly = out["granularity"] == "weekly"
            week_start = out["ds"] - pd.Timedelta(days=6)
            overlaps = (week_start <= pd.Timestamp(event.end)) & (
                out["ds"] >= pd.Timestamp(event.start)
            )
            in_event = (in_event & ~weekly) | (overlaps & weekly)
        bad |= in_event
    out.loc[bad, "y"] = np.nan
    out["y"] = out.groupby("unique_id")["y"].transform(
        lambda s: s.interpolate(limit_direction="both").ffill().bfill()
    )
    return out.dropna(subset=["y"])


@dataclass(frozen=True)
class ForecastSpec:
    """Which model to use for which series group, at what interval level."""

    level: int
    daily_season: int
    weekly_season: int


def forecast_origins(
    history: pd.DataFrame,
    origins: Iterable[date],
    models: dict[str, list[str]],
    spec: ForecastSpec,
    cfg: DetectionConfig,
) -> pd.DataFrame:
    """Fit at every origin and forecast forward.

    `models` maps unique_id -> list of model names to fit for that series.
    Returns one row per (unique_id, model, origin, ds) with yhat, lo, hi.
    """
    results = []
    for origin in origins:
        origin_ts = pd.Timestamp(origin)
        train_all = history[history["ds"] < origin_ts]
        if train_all.empty:
            continue
        for granularity, horizon, season in (
            ("daily", 7, spec.daily_season),
            ("weekly", 1, spec.weekly_season),
        ):
            part = train_all[train_all["granularity"] == granularity]
            if part.empty:
                continue
            part = clean_history(part, cfg)
            # minimum history: two full seasons (and at least 8 points) for every model
            counts = part.groupby("unique_id")["ds"].count()
            eligible = counts[counts >= max(2 * season, 8)].index
            part = part[part["unique_id"].isin(eligible)]
            by_models: dict[tuple[str, ...], list[str]] = {}
            for uid in eligible:
                names = tuple(m for m in models.get(uid, []))
                if names:
                    by_models.setdefault(names, []).append(uid)
            for names, uids in by_models.items():
                train = part[part["unique_id"].isin(uids)][["unique_id", "ds", "y"]]
                sf = StatsForecast(
                    models=[make_model(n, season) for n in names],
                    freq="D" if granularity == "daily" else "W-SUN",
                    n_jobs=1,
                )
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    fc = sf.forecast(df=train, h=horizon, level=[spec.level])
                fc = fc.reset_index() if "unique_id" not in fc.columns else fc
                for name in names:
                    block = fc[["unique_id", "ds"]].copy()
                    block["model"] = name
                    block["yhat"] = fc[name].to_numpy()
                    block["lo"] = fc[f"{name}-lo-{spec.level}"].to_numpy()
                    block["hi"] = fc[f"{name}-hi-{spec.level}"].to_numpy()
                    block["origin"] = origin_ts
                    results.append(block)
    if not results:
        return pd.DataFrame(columns=["unique_id", "ds", "model", "yhat", "lo", "hi", "origin"])
    out = pd.concat(results, ignore_index=True)
    out["ds"] = pd.to_datetime(out["ds"])
    return out
