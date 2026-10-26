"""Changes B (log GMV), C (share of total) and D (calendar windows), ADR-phase3b sections 9 and 10."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from ds.backtest import score
from ds.catalogue import (
    ImprovementFlags,
    load_catalogue,
    load_detection_config,
    share_kpis_for,
    transforms_for,
    with_flags,
)
from ds.detection import (
    CALENDAR_SEVERITY_FACTOR,
    INVESTIGATION_THRESHOLD,
    Detector,
    detect_range,
    forecast_period,
    group_calendar_alerts,
    score_points,
    tag_calendar,
)
from ds.forecasting import (
    ForecastSpec,
    apply_transforms,
    forecast_origins,
    group_name,
    load_series,
)
from evaluation.protocol import protocols, run_protocol
from tests.golden.synthetic_inputs import synthetic_detector

pytestmark = pytest.mark.unit
PLAIN_GROUPS = {
    f"{f}|{g}" for f in ("volume", "ratio", "rate", "mean") for g in ("daily", "weekly")
}


def _detector(**extra: object) -> Detector:
    groups = {name: {"model": "SeasonalNaive", "calibration": 1.0} for name in PLAIN_GROUPS}
    groups.update(
        {
            n: {"model": "SeasonalNaive", "calibration": 1.0}
            for n in ("volume_log1p|daily", "volume_log1p|weekly", "share|daily", "share|weekly")
        }
    )
    return Detector(groups=groups, level=95, version="test", **extra)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------
def test_every_flag_is_implemented_and_off_by_default() -> None:
    cfg = load_detection_config()
    assert not any(cfg.improvements.model_dump().values())
    assert transforms_for(cfg) == {} and share_kpis_for(cfg) == ()
    on = with_flags(cfg, ["log_gmv", "share_test", "calendar_downgrade", "feed_guard"])
    assert all(on.improvements.model_dump().values())
    assert transforms_for(on) == {"gmv": "log1p"} and share_kpis_for(on) == ("orders", "gmv")
    assert ImprovementFlags(log_gmv=True).log_gmv


# ---------------------------------------------------------------------------
# B: log scale
# ---------------------------------------------------------------------------
def test_group_names_are_the_phase3_names_unless_a_change_applies() -> None:
    assert group_name("gmv", "total", "sum", "daily") == "volume|daily"
    assert group_name("orders", "customer_state", "count", "weekly") == "volume|weekly"
    assert group_name("gmv", "total", "sum", "daily", {"gmv": "log1p"}) == "volume_log1p|daily"
    assert group_name("orders", "total", "count", "daily", {"gmv": "log1p"}) == "volume|daily"
    assert (
        group_name("orders", "customer_state", "count", "daily", {}, ("orders", "gmv"))
        == "share|daily"
    )
    assert (
        group_name("orders", "total", "count", "daily", {}, ("orders", "gmv")) == "volume|daily"
    )  # total stays level
    assert (
        group_name("aov", "customer_state", "ratio", "daily", {}, ("orders", "gmv"))
        == "ratio|daily"
    )


def test_a_detector_knows_which_changes_it_was_fitted_with(tmp_path: Path) -> None:
    d = _detector(transforms={"gmv": "log1p"}, share_kpis=("orders", "gmv"))
    assert d.group_for("gmv", "total", "sum", "daily") is d.groups["volume_log1p|daily"]
    assert d.group_for("gmv", "customer_state", "sum", "daily") is d.groups["share|daily"]
    assert d.group_for("orders", "total", "count", "daily") is d.groups["volume|daily"]
    path = tmp_path / "detector.json"
    path.write_text(
        json.dumps(
            {
                "groups": d.groups,
                "interval_level": 95,
                "transforms": {"gmv": "log1p"},
                "share_kpis": ["orders", "gmv"],
            }
        ),
        encoding="utf-8",
    )
    loaded = Detector.load(path)
    assert loaded.transforms == {"gmv": "log1p"} and loaded.share_kpis == ("orders", "gmv")
    old = tmp_path / "old.json"  # a Phase 3 detector file has neither key
    old.write_text(json.dumps({"groups": d.groups, "interval_level": 95}), encoding="utf-8")
    assert Detector.load(old).transforms == {} and Detector.load(old).share_kpis == ()


def test_the_log_transform_round_trips_and_leaves_other_series_alone() -> None:
    frame = pd.DataFrame(
        {
            "unique_id": ["gmv|total|all|daily"] * 3
            + ["orders|total|all|daily"] * 3
            + ["gmv|state|SP|daily"] * 3,
            "kpi": ["gmv"] * 3 + ["orders"] * 3 + ["gmv"] * 3,
            "kind": ["level"] * 6 + ["share"] * 3,
            "y": [100.0, 2500.0, 40000.0, 5.0, 6.0, 7.0, 0.2, 0.3, 0.4],
        }
    )
    out, log_uids = apply_transforms(frame, {"gmv": "log1p"})
    assert log_uids == {"gmv|total|all|daily"}
    assert out["y"].iloc[:3].tolist() == pytest.approx(np.log1p([100.0, 2500.0, 40000.0]).tolist())
    assert (
        out["y"].iloc[3:].tolist() == frame["y"].iloc[3:].tolist()
    )  # orders and the share series untouched
    assert np.expm1(out["y"].iloc[:3]).tolist() == pytest.approx([100.0, 2500.0, 40000.0])
    untouched, none = apply_transforms(frame, None)
    assert none == set() and untouched is frame


def test_forecasts_mark_which_rows_are_on_the_log_scale(
    dense_con: duckdb.DuckDBPyConnection,
) -> None:
    cfg, catalogue = load_detection_config(), load_catalogue()
    series = load_series(dense_con, catalogue)
    keep = series[series["unique_id"].isin(["gmv|total|all|daily", "orders|total|all|daily"])]
    models = {u: ["SeasonalNaive"] for u in keep["unique_id"].unique()}
    spec = ForecastSpec(95, 7, 1)
    fc = forecast_origins(keep, [date(2018, 3, 5)], models, spec, cfg, transforms={"gmv": "log1p"})
    by = fc.groupby("unique_id")["model_scale"].first().to_dict()
    assert by == {"gmv|total|all|daily": "log1p", "orders|total|all|daily": "raw"}
    gmv = fc[fc["unique_id"] == "gmv|total|all|daily"]
    assert gmv["yhat"].between(np.log1p(100), np.log1p(1e7)).all()  # a log value, not BRL
    off = forecast_origins(keep, [date(2018, 3, 5)], models, spec, cfg)
    assert set(off["model_scale"]) == {"raw"}


def test_scoring_in_log_space_gives_an_asymmetric_interval_in_original_units() -> None:
    cfg, catalogue = load_detection_config(), load_catalogue()
    pts = pd.DataFrame(
        {
            "unique_id": ["gmv|total|all|daily"],
            "kpi": "gmv",
            "dimension": "total",
            "segment": "all",
            "granularity": "daily",
            "ds": pd.Timestamp("2018-03-07"),
            "y": 2000.0,
            "n": 300,
            "min_n": 0,
            "yhat": np.log1p(1000.0),
            "lo": np.log1p(1000.0) - 0.3,
            "hi": np.log1p(1000.0) + 0.3,
            "model": "SeasonalNaive",
            "model_scale": "log1p",
        }
    )
    scored = score_points(pts, _detector(transforms={"gmv": "log1p"}), catalogue, cfg).iloc[0]
    assert scored["yhat"] == pytest.approx(1000.0)  # reported in BRL
    assert (
        scored["upper"] - scored["yhat"] > scored["yhat"] - scored["lower"]
    )  # wider above than below
    assert scored["lower"] == pytest.approx(np.expm1(np.log1p(1000.0) - 0.3))
    sigma = 0.3 / 1.959963984540054
    assert scored["z"] == pytest.approx((np.log1p(2000.0) - np.log1p(1000.0)) / sigma)
    assert bool(scored["eligible"]) and scored["z"] > 4


def test_raw_rows_are_scored_exactly_as_in_phase3() -> None:
    cfg, catalogue = load_detection_config(), load_catalogue()
    pts = pd.DataFrame(
        {
            "unique_id": ["orders|total|all|daily"],
            "kpi": "orders",
            "dimension": "total",
            "segment": "all",
            "granularity": "daily",
            "ds": pd.Timestamp("2018-03-07"),
            "y": 40.0,
            "n": 40,
            "min_n": 0,
            "yhat": 100.0,
            "lo": 80.4,
            "hi": 119.6,
            "model": "SeasonalNaive",
            "model_scale": "raw",
        }
    )
    row = score_points(pts, synthetic_detector(), catalogue, cfg).iloc[0]
    assert (row["lower"], row["upper"], row["yhat"]) == pytest.approx((80.4, 119.6, 100.0))
    assert row["z"] == pytest.approx((40.0 - 100.0) / (19.6 / 1.959963984540054))


def test_backtest_scores_log_series_on_the_log_scale() -> None:
    cfg, catalogue = load_detection_config(), load_catalogue()
    uid = "gmv|total|all|daily"
    fc = pd.DataFrame(
        {
            "unique_id": [uid, uid],
            "ds": pd.to_datetime(["2017-09-04", "2017-09-05"]),
            "model": "SeasonalNaive",
            "yhat": [np.log1p(1000.0)] * 2,
            "lo": [np.log1p(1000.0) - 0.2] * 2,
            "hi": [np.log1p(1000.0) + 0.2] * 2,
            "model_scale": "log1p",
        }
    )
    series = pd.DataFrame(
        {
            "unique_id": uid,
            "ds": pd.to_datetime(["2017-09-04", "2017-09-05"]),
            "y": [1100.0, 1700.0],
            "n": 100,
            "min_n": 0,
            "kpi": "gmv",
            "dimension": "total",
            "granularity": "daily",
        }
    )
    pts = score(
        fc, series, pd.Series({uid: 0.1}, name="scale"), catalogue, cfg, {"gmv": "log1p"}, ()
    )
    assert pts["group"].tolist() == ["volume_log1p|daily"] * 2
    assert pts["covered"].tolist() == [
        True,
        False,
    ]  # log1p(1100) is inside +-0.2, log1p(1700) is not
    assert pts["err"].iloc[0] == pytest.approx(np.log1p(1100.0) - np.log1p(1000.0))
    assert pts["ratio"].iloc[0] == pytest.approx(abs(pts["err"].iloc[0]) / 0.2)


def test_a_detector_and_flags_that_disagree_are_rejected(tmp_path: Path) -> None:
    cfg = with_flags(load_detection_config(), ["log_gmv"])
    with pytest.raises(ValueError, match="detector and flags disagree"):
        run_protocol(
            protocols(cfg)["eval2018"],
            warehouse=tmp_path / "none.duckdb",
            detector=synthetic_detector(),
            scenarios=[],
            cache_dir=tmp_path,
            cfg=cfg,
        )


# ---------------------------------------------------------------------------
# C: share of total
# ---------------------------------------------------------------------------
def test_share_series_replace_segment_levels_for_additive_kpis_only(
    dense_con: duckdb.DuckDBPyConnection,
) -> None:
    catalogue = load_catalogue()
    level = load_series(dense_con, catalogue)
    shared = load_series(dense_con, catalogue, ("orders", "gmv"))
    assert set(level["kind"]) == {"level"}
    assert (
        len(level) == len(shared)
        and (level["unique_id"].to_numpy() == shared["unique_id"].to_numpy()).all()
    )
    is_share = shared["kind"] == "share"
    assert set(shared.loc[is_share, "kpi"]) == {"orders", "gmv"}
    assert (shared.loc[is_share, "dimension"] != "total").all()
    assert (
        shared.loc[shared["dimension"] == "total", "kind"] == "level"
    ).all()  # the total stays level-based
    assert (shared.loc[~shared["kpi"].isin(["orders", "gmv"]), "kind"] == "level").all()
    shares = shared.loc[is_share, "y"]
    assert shares.dropna().between(0, 1).all()
    # a share is undefined (NaN) only when the total itself is zero, and such rows are never tested
    assert (shared.loc[is_share & shared["y"].isna(), "n"] == 0).all()


def test_a_daily_share_is_the_segment_over_the_total_that_day(
    dense_con: duckdb.DuckDBPyConnection,
) -> None:
    shared = load_series(dense_con, load_catalogue(), ("orders",))
    row = shared[(shared["kind"] == "share") & (shared["granularity"] == "daily")].iloc[40]
    day = row["ds"].date()
    seg, tot = dense_con.execute(
        "SELECT (SELECT numerator FROM metrics.daily_kpi WHERE kpi = ? AND dimension = ? AND segment = ? AND date = ?), "
        "(SELECT numerator FROM metrics.daily_kpi WHERE kpi = ? AND dimension = 'total' AND date = ?)",
        [row["kpi"], row["dimension"], row["segment"], day, row["kpi"], day],
    ).fetchall()[0]
    assert row["y"] == pytest.approx(seg / tot)


def test_a_weekly_share_uses_the_weekly_total(dense_con: duckdb.DuckDBPyConnection) -> None:
    shared = load_series(dense_con, load_catalogue(), ("orders",))
    weekly = shared[(shared["kind"] == "share") & (shared["granularity"] == "weekly")]
    assert len(weekly) > 0 and (weekly["ds"].dt.dayofweek == 6).all()  # week-ending Sundays
    row = weekly.iloc[10]
    seg, tot = dense_con.execute(
        "SELECT (SELECT numerator FROM metrics.weekly_kpi WHERE kpi = ? AND dimension = ? AND segment = ? AND date = ?), "
        "(SELECT numerator FROM metrics.weekly_kpi WHERE kpi = ? AND dimension = 'total' AND date = ?)",
        [
            row["kpi"],
            row["dimension"],
            row["segment"],
            row["ds"].date(),
            row["kpi"],
            row["ds"].date(),
        ],
    ).fetchall()[0]
    assert row["y"] == pytest.approx(seg / tot)


def test_a_shock_that_moves_every_segment_together_leaves_shares_unchanged() -> None:
    segments = pd.DataFrame(
        {"a": [60.0, 120.0], "b": [40.0, 80.0]}
    )  # day 2 is a market-wide x2 shock
    total = segments.sum(axis=1)
    shares = segments.div(total, axis=0)
    assert shares.iloc[0].tolist() == pytest.approx(shares.iloc[1].tolist())
    assert (total.iloc[1] / total.iloc[0]) == 2.0  # the level test would see it on every segment


# ---------------------------------------------------------------------------
# D: calendar windows
# ---------------------------------------------------------------------------
def _alerts(rows: list[tuple[date, str, str, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": [r[0] for r in rows],
            "kpi": [r[1] for r in rows],
            "granularity": [r[2] for r in rows],
            "severity": [r[3] for r in rows],
            "dimension": "total",
            "segment": "all",
        }
    )


def test_alerts_in_a_calendar_window_are_tagged_and_downgraded_not_dropped() -> None:
    out = tag_calendar(
        _alerts(
            [
                (date(2017, 12, 24), "orders", "daily", 2.0),
                (date(2018, 1, 3), "orders", "daily", 1.0),
                (date(2018, 3, 20), "orders", "daily", 2.0),
                (date(2018, 2, 12), "gmv", "daily", 4.0),
            ]
        )
    )
    assert out["calendar_uncertain"].tolist() == [
        True,
        True,
        False,
        True,
    ]  # Christmas, Christmas, none, Carnival
    assert out["severity"].tolist() == pytest.approx([1.0, 0.5, 2.0, 2.0])
    assert len(out) == 4
    assert out["calendar_window"].iloc[0].startswith("christmas_new_year")
    assert tag_calendar(_alerts([])).columns.tolist()[-2:] == [
        "calendar_uncertain",
        "calendar_window",
    ]


def test_the_investigation_threshold_depends_on_raw_severity() -> None:
    assert (INVESTIGATION_THRESHOLD, CALENDAR_SEVERITY_FACTOR) == (1.0, 0.5)
    out = tag_calendar(
        _alerts([(date(2017, 12, 24), "orders", "daily", s) for s in (1.8, 2.0, 2.2)])
    )
    assert (out["severity"] >= INVESTIGATION_THRESHOLD).tolist() == [
        False,
        True,
        True,
    ]  # below only if raw < 2.0


def test_consecutive_calendar_alerts_become_one_record() -> None:
    tagged = tag_calendar(
        _alerts(
            [
                (date(2017, 12, 22), "orders", "daily", 2.0),
                (date(2017, 12, 23), "orders", "daily", 6.0),
                (date(2017, 12, 24), "orders", "daily", 3.0),
                (date(2017, 12, 27), "orders", "daily", 2.0),
                (date(2017, 12, 23), "gmv", "daily", 2.0),
                (date(2018, 3, 20), "orders", "daily", 2.0),
            ]
        )
    )
    records = group_calendar_alerts(tagged)
    orders = records[(records["kpi"] == "orders") & records["calendar_uncertain"]].sort_values(
        "date"
    )
    assert len(orders) == 2  # 22-24 December is one record; 27 December (a gap) is another
    first = orders.iloc[0]
    assert (first["date"], first["last_date"], first["n_alerts"]) == (
        date(2017, 12, 22),
        date(2017, 12, 24),
        3,
    )
    assert first["severity"] == pytest.approx(3.0)  # the most severe member (6.0 halved)
    assert json.loads(first["days"]) == ["2017-12-22", "2017-12-23", "2017-12-24"]
    assert orders.iloc[1]["n_alerts"] == 1
    assert records["n_alerts"].sum() == len(tagged)  # every alert is accounted for
    assert int(records[~records["calendar_uncertain"]]["n_alerts"].iloc[0]) == 1


@pytest.fixture
def around_christmas(
    dense_con: duckdb.DuckDBPyConnection,
) -> tuple[pd.DataFrame, pd.DataFrame, list[date]]:
    catalogue, cfg = load_catalogue(), load_detection_config()
    series = load_series(dense_con, catalogue)
    days = [
        date(2017, 12, 14) + timedelta(days=i) for i in range(14)
    ]  # spans the 20 December window start
    for shocked in (
        date(2017, 12, 16),
        date(2017, 12, 22),
    ):  # one day outside, one inside the window
        hit = (series["unique_id"] == "orders|total|all|daily") & (
            series["ds"] == pd.Timestamp(shocked)
        )
        series.loc[hit, "y"] = series.loc[hit, "y"] * 0.2
    fc = forecast_period(series, days[0], days[-1], synthetic_detector(), catalogue, cfg)
    return series, fc, days


def test_calendar_tagging_never_changes_how_many_alerts_are_raised(around_christmas) -> None:  # type: ignore[no-untyped-def]
    series, fc, days = around_christmas
    base = load_detection_config()
    off, tests_off = detect_range(series, fc, days, synthetic_detector(), load_catalogue(), base)
    on, tests_on = detect_range(
        series,
        fc,
        days,
        synthetic_detector(),
        load_catalogue(),
        with_flags(base, ["calendar_downgrade"]),
    )
    key = ["date", "kpi", "granularity", "dimension", "segment"]
    assert tests_on == tests_off and len(on) == len(off) and len(off) > 0
    assert on[key].astype(str).values.tolist() == off[key].astype(str).values.tolist()
    inside = on["calendar_uncertain"].to_numpy()
    assert inside.any() and (~inside).any()
    assert on.loc[inside, "severity"].tolist() == pytest.approx(
        (off.loc[inside, "severity"] * 0.5).tolist()
    )
    assert on.loc[~inside, "severity"].tolist() == pytest.approx(
        off.loc[~inside, "severity"].tolist()
    )


def test_known_event_days_are_tested_not_silenced_when_d_is_on() -> None:
    cfg, catalogue = load_detection_config(), load_catalogue()
    pts = pd.DataFrame(
        {
            "unique_id": ["orders|total|all|daily"],
            "kpi": "orders",
            "dimension": "total",
            "segment": "all",
            "granularity": "daily",
            "ds": pd.Timestamp("2017-11-24"),
            "y": 600.0,
            "n": 600,
            "min_n": 0,
            "yhat": 100.0,
            "lo": 80.0,
            "hi": 120.0,
            "model": "SeasonalNaive",
            "model_scale": "raw",
        }
    )
    assert not bool(
        score_points(pts, synthetic_detector(), catalogue, cfg).iloc[0]["eligible"]
    )  # Phase 3
    on = with_flags(cfg, ["calendar_downgrade"])
    assert bool(
        score_points(pts, synthetic_detector(), catalogue, on).iloc[0]["eligible"]
    )  # tested, then tagged
