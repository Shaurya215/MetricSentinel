"""Anomaly detection (FR-031 to FR-034, AI_ML_PLAN.md section 2.3).

For every series tested on a day (daily series every day, weekly series on the
week-ending Sunday):

  1. Take the forecast and interval made at the latest Monday re-fit BEFORE the day,
     widened by the backtest calibration factor for the series group.
  2. z = (observed - forecast) / sigma, with sigma from the calibrated interval width.
     One-sided p-value in the KPI's alert direction (two-sided for `both`).
  3. Benjamini-Hochberg across all series tested that day (q = 0.05).
  4. Flag when q <= threshold, the value is outside the calibrated interval, the sample
     is at least the KPI's minimum, and the day is not a known event.
  5. Group flags by KPI and granularity: the total series is the headline when it is
     flagged, otherwise the most significant segment; the other flags become context.
  6. Severity = |relative deviation| x log(sample size).

    uv run python -m ds.detect --date 2018-03-05
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import norm
from statsmodels.stats.multitest import multipletests

from core.logging import get_logger
from core.settings import REPO_ROOT
from ds.calendar import calendar_windows, window_on
from ds.catalogue import Catalogue, DetectionConfig
from ds.feed_guard import apply_guard, total_orders
from ds.forecasting import ForecastSpec, forecast_origins, group_name, mondays

DETECTOR_PATH = REPO_ROOT / "config" / "detector.json"
FAMILY_GROUP = {
    "count": "volume",
    "sum": "volume",
    "ratio": "ratio",
    "rate": "rate",
    "mean": "mean",
}
ANOMALY_COLUMNS = [
    "anomaly_id",
    "date",
    "kpi",
    "granularity",
    "dimension",
    "segment",
    "observed",
    "expected",
    "lower",
    "upper",
    "z",
    "p_value",
    "q_value",
    "n",
    "severity",
    "direction",
    "related_segments",
    "model",
]


@dataclass(frozen=True)
class Detector:
    """The fitted detector: chosen model and calibration factor per series group.

    Self-describing: `transforms` (KPI -> "log1p") and `share_kpis` record which 3b changes it was
    fitted with, so a detector and a configuration that disagree can be caught.
    """

    groups: dict[str, dict[str, Any]]
    level: int
    version: str
    transforms: dict[str, str] = field(default_factory=dict)
    share_kpis: tuple[str, ...] = ()

    @classmethod
    def load(cls, path: Path = DETECTOR_PATH) -> Detector:
        raw = path.read_bytes()
        data = json.loads(raw)
        return cls(
            groups=data["groups"],
            level=int(data["interval_level"]),
            version=hashlib.sha256(raw).hexdigest()[:12],
            transforms=dict(data.get("transforms", {})),
            share_kpis=tuple(data.get("share_kpis", ())),
        )

    def group(self, family: str, granularity: str) -> dict[str, Any]:
        return self.groups[f"{FAMILY_GROUP[family]}|{granularity}"]

    def group_for(self, kpi: str, dimension: str, family: str, granularity: str) -> dict[str, Any]:
        name = group_name(kpi, dimension, family, granularity, self.transforms, self.share_kpis)
        return self.groups[name]


def forecast_period(
    series: pd.DataFrame,
    start: date,
    end: date,
    detector: Detector,
    catalogue: Catalogue,
    cfg: DetectionConfig,
    only: set[str] | None = None,
) -> pd.DataFrame:
    """Forecasts for every day in [start, end] from Monday re-fits, with the chosen models."""
    family = {k.name: k.family for k in catalogue.kpis}
    ids = series[["unique_id", "kpi", "dimension", "granularity"]].drop_duplicates()
    if only is not None:
        ids = ids[ids["unique_id"].isin(only)]
    models = {
        uid: [detector.group_for(kpi, dim, family[kpi], gran)["model"]]
        for uid, kpi, dim, gran in ids.itertuples(index=False)
    }
    spec = ForecastSpec(
        detector.level, cfg.forecast.daily_season_length, cfg.forecast.weekly_season_length
    )
    hist = series[series["unique_id"].isin(models)]
    fc = forecast_origins(
        hist, mondays(start, end), models, spec, cfg, transforms=detector.transforms or None
    )
    fc = fc[(fc["ds"] >= pd.Timestamp(start)) & (fc["ds"] <= pd.Timestamp(end))]
    return fc.drop(columns=["model"]).assign(model=fc["model"].to_numpy())


def _event_mask(frame: pd.DataFrame, cfg: DetectionConfig) -> pd.Series:
    mask = pd.Series(False, index=frame.index)
    week_start = frame["ds"] - pd.to_timedelta(
        np.where(frame["granularity"] == "weekly", 6, 0), unit="D"
    )
    for event in cfg.events:
        mask |= (week_start <= pd.Timestamp(event.end)) & (frame["ds"] >= pd.Timestamp(event.start))
    return mask


def score_points(
    points: pd.DataFrame, detector: Detector, catalogue: Catalogue, cfg: DetectionConfig
) -> pd.DataFrame:
    """Add calibrated interval, z and p-value to observation+forecast rows.

    Rows forecast on a log scale (change B) are scored in log space and reported back in the
    original units, so their interval is asymmetric (wider above).
    """
    kpis = {k.name: k for k in catalogue.kpis}
    out = points.copy()
    calib = np.array(
        [
            detector.group_for(k, d, kpis[k].family, g)["calibration"]
            for k, d, g in zip(out["kpi"], out["dimension"], out["granularity"], strict=True)
        ]
    )
    z_crit = norm.ppf(0.5 + detector.level / 200)
    half = (out["hi"] - out["lo"]).to_numpy() / 2 * calib
    is_log = (
        (out["model_scale"] == "log1p").to_numpy()
        if "model_scale" in out.columns
        else np.zeros(len(out), dtype=bool)
    )
    sigma = np.where(half > 0, half / z_crit, np.nan)
    if is_log.any():
        y_model = np.where(is_log, np.log1p(out["y"].clip(lower=0).to_numpy()), out["y"].to_numpy())
        lower_m, upper_m = out["yhat"] - half, out["yhat"] + half
        out["z"] = (y_model - out["yhat"]) / sigma
        out["lower"] = np.where(is_log, np.expm1(lower_m), lower_m)
        out["upper"] = np.where(is_log, np.expm1(upper_m), upper_m)
        out["yhat"] = np.where(is_log, np.expm1(out["yhat"]), out["yhat"])
    else:
        out["lower"], out["upper"] = out["yhat"] - half, out["yhat"] + half
        out["z"] = (out["y"] - out["yhat"]) / sigma
    direction = out["kpi"].map({k: v.alert_direction for k, v in kpis.items()})
    cdf = norm.cdf(out["z"])
    out["p_value"] = np.select(
        [direction == "up", direction == "down"],
        [1 - cdf, cdf],
        default=2 * np.minimum(cdf, 1 - cdf),
    )
    # Change D: known event days are tested (and tagged), not silenced. Their values are still
    # removed from TRAINING (clean_history), which is cleaning, not a learned effect.
    silenced = (
        pd.Series(False, index=out.index)
        if cfg.improvements.calendar_downgrade
        else _event_mask(out, cfg)
    )
    out["eligible"] = (
        (out["n"] >= out["min_n"]) & out["y"].notna() & np.isfinite(out["z"]) & ~silenced
    )
    return out


def detect_day(scored_day: pd.DataFrame, cfg: DetectionConfig) -> pd.DataFrame:
    """BH correction across the day's eligible tests, then flag and group (one day only)."""
    tests = scored_day[scored_day["eligible"]].copy()
    if tests.empty:
        return pd.DataFrame(columns=ANOMALY_COLUMNS)
    tests["q_value"] = multipletests(tests["p_value"], alpha=cfg.detection.fdr_q, method="fdr_bh")[
        1
    ]
    outside = (tests["y"] < tests["lower"]) | (tests["y"] > tests["upper"])
    flagged = tests[(tests["q_value"] <= cfg.detection.fdr_q) & outside]
    rows = []
    for (kpi, granularity), group in flagged.groupby(["kpi", "granularity"]):
        total = group[group["dimension"] == "total"]
        head = (
            total
            if len(total)
            else group.sort_values(["q_value", "p_value", "dimension", "segment"])
        ).iloc[0]
        related = [
            f"{r.dimension}={r.segment}"
            for r in group.itertuples()
            if r.unique_id != head["unique_id"]
        ]
        rel_dev = (head["y"] - head["yhat"]) / abs(head["yhat"]) if head["yhat"] else 0.0
        day = pd.Timestamp(head["ds"]).date()
        rows.append(
            {
                "anomaly_id": hashlib.sha1(f"{kpi}|{granularity}|{day}".encode()).hexdigest()[:16],
                "date": day,
                "kpi": kpi,
                "granularity": granularity,
                "dimension": head["dimension"],
                "segment": head["segment"],
                "observed": float(head["y"]),
                "expected": float(head["yhat"]),
                "lower": float(head["lower"]),
                "upper": float(head["upper"]),
                "z": float(head["z"]),
                "p_value": float(head["p_value"]),
                "q_value": float(head["q_value"]),
                "n": int(head["n"]),
                "severity": float(abs(rel_dev) * np.log(max(int(head["n"]), 2))),
                "direction": "up" if head["y"] > head["yhat"] else "down",
                "related_segments": json.dumps(sorted(related)),
                "model": head["model"],
            }
        )
    return pd.DataFrame(rows, columns=ANOMALY_COLUMNS)


log = get_logger(__name__)


def detect_range(
    series: pd.DataFrame,
    forecasts: pd.DataFrame,
    days: list[date],
    detector: Detector,
    catalogue: Catalogue,
    cfg: DetectionConfig,
    diagnostics: dict[str, Any] | None = None,
) -> tuple[pd.DataFrame, int]:
    """Detect on every day in `days`. Returns the anomalies and the number of tests run.

    With `improvements.feed_guard` on (change A), a day is first checked by the volume-collapse
    guard: a collapse day raises one guard alert and no other test runs that day, and a day past
    the last reliable day is not tested and raises nothing. Suppressed days are logged and, when
    `diagnostics` is given, returned in it (`guard_collapse`, `guard_past_data_end`). With every
    flag off this function does exactly what it did in Phase 3.
    """
    guard_alerts: list[dict[str, Any]] = []
    if cfg.improvements.feed_guard:
        guard = apply_guard(total_orders(series), days, cfg.feed_guard)
        days = guard.testable_days
        guard_alerts = guard.alerts
        for hit in guard.collapse:
            log.info(
                "feed_guard_suppressed",
                reason="volume collapse",
                **{k: str(v) for k, v in hit.items()},
            )
        if guard.past_data_end:
            log.info(
                "feed_guard_suppressed",
                reason="past the last reliable day",
                first=str(guard.past_data_end[0]),
                days=len(guard.past_data_end),
            )
        if diagnostics is not None:
            diagnostics["guard_collapse"] = guard.collapse
            diagnostics["guard_past_data_end"] = guard.past_data_end

    wanted = {pd.Timestamp(d) for d in days}
    obs = series[series["ds"].isin(wanted)]
    forecast_columns = ["unique_id", "ds", "yhat", "lo", "hi", "model"]
    if "model_scale" in forecasts.columns:
        forecast_columns.append("model_scale")
    points = obs.merge(forecasts[forecast_columns], on=["unique_id", "ds"], how="inner")
    tests = 0
    anomalies = pd.DataFrame(columns=ANOMALY_COLUMNS)
    if not points.empty:
        scored = score_points(points, detector, catalogue, cfg)
        frames = [detect_day(g, cfg) for _, g in scored.groupby("ds")]
        frames = [f for f in frames if not f.empty]
        if frames:
            anomalies = pd.concat(frames, ignore_index=True)
        tests = int(scored["eligible"].sum())
    elif not guard_alerts:
        return anomalies, 0
    if guard_alerts:
        guard_frame = pd.DataFrame(guard_alerts, columns=ANOMALY_COLUMNS)
        anomalies = (
            guard_frame
            if anomalies.empty
            else pd.concat([anomalies, guard_frame], ignore_index=True)
        )
    anomalies = anomalies.sort_values(["date", "kpi", "granularity"]).reset_index(drop=True)
    if cfg.improvements.calendar_downgrade:
        anomalies = tag_calendar(anomalies)
    return anomalies, tests


INVESTIGATION_THRESHOLD = 1.0  # ADR-phase3b section 9: severity needed to be investigated
CALENDAR_SEVERITY_FACTOR = 0.5


def tag_calendar(anomalies: pd.DataFrame) -> pd.DataFrame:
    """Change D: mark alerts inside a calendar window and lower their severity. Nothing is silenced."""
    out = anomalies.copy()
    if out.empty:
        out["calendar_uncertain"] = pd.Series(dtype=bool)
        out["calendar_window"] = pd.Series(dtype=str)
        return out
    years = [d.year for d in out["date"]]
    windows = calendar_windows(min(years), max(years))
    found = [window_on(d, windows) for d in out["date"]]
    out["calendar_uncertain"] = [w is not None for w in found]
    out["calendar_window"] = ["" if w is None else f"{w.name} {w.start}" for w in found]
    out.loc[out["calendar_uncertain"], "severity"] = (
        out.loc[out["calendar_uncertain"], "severity"] * CALENDAR_SEVERITY_FACTOR
    )
    return out


def group_calendar_alerts(anomalies: pd.DataFrame) -> pd.DataFrame:
    """FR-033 across days: consecutive-day `calendar_uncertain` alerts of one KPI and granularity
    inside one calendar window become one record (the most severe alert, with first and last date,
    the day list, and how many alerts it stands for). Other alerts pass through unchanged.
    """
    if anomalies.empty or "calendar_uncertain" not in anomalies.columns:
        return anomalies.assign(last_date=anomalies.get("date"), n_alerts=1)
    rows: list[pd.Series] = []
    plain = anomalies[~anomalies["calendar_uncertain"]]
    for _, r in plain.iterrows():
        r = r.copy()
        r["last_date"], r["days"], r["n_alerts"] = r["date"], json.dumps([str(r["date"])]), 1
        rows.append(r)
    flagged = anomalies[anomalies["calendar_uncertain"]]
    for _, group in flagged.groupby(["kpi", "granularity", "calendar_window"]):
        group = group.sort_values("date")
        block: list[pd.Series] = []
        for _, r in group.iterrows():
            if block and (r["date"] - block[-1]["date"]).days > 1:
                rows.append(_merge_block(block))
                block = []
            block.append(r)
        rows.append(_merge_block(block))
    return pd.DataFrame(rows).sort_values(["date", "kpi", "granularity"]).reset_index(drop=True)


def _merge_block(block: list[pd.Series]) -> pd.Series:
    head = max(block, key=lambda r: r["severity"]).copy()
    head["date"] = block[0]["date"]
    head["last_date"] = block[-1]["date"]
    head["days"] = json.dumps([str(r["date"]) for r in block])
    head["n_alerts"] = len(block)
    return head
