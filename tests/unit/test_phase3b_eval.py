"""Phase 3b evaluation helpers: events and dates, last reliable day, classification, strict recall,
Wilson intervals, the 2017 benchmark file, and the config flags (ADR-phase3b.md)."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest
import yaml
from pydantic import ValidationError

from core.settings import REPO_ROOT
from ds.calendar import black_friday, calendar_windows, carnival_tuesday, window_on
from ds.catalogue import ImprovementFlags, load_detection_config
from ds.feed_guard import collapse_days, last_reliable_day
from evaluation.breakdown import (
    ClassifyContext,
    breakdown,
    classify,
    fraction,
    load_known_events,
    matching_events,
    wilson,
)
from evaluation.detection import flag_keys, flagged_series, targeted_mask
from evaluation.protocol import target_share
from pipelines.build import BuildResult
from pipelines.dev_benchmark import GRID_COUNTS, allowed_starts, by_band, size_band
from pipelines.faults import Scenario, ScenarioWorkspace

pytestmark = pytest.mark.unit
EVENTS = load_known_events()


# ---------------------------------------------------------------------------
# Wilson intervals
# ---------------------------------------------------------------------------
def test_wilson_interval_matches_a_hand_calculation() -> None:
    low, high = wilson(9, 16)  # the baseline's 9 of 16
    assert low == pytest.approx(0.3318, abs=5e-4) and high == pytest.approx(0.7691, abs=5e-4)
    assert wilson(0, 0) == (0.0, 0.0)
    low0, high0 = wilson(0, 10)
    assert low0 == 0.0 and 0.2 < high0 < 0.35
    assert fraction(9, 16)["rate"] == 0.5625


# ---------------------------------------------------------------------------
# Known events: plain Brazilian local dates, no UTC shift
# ---------------------------------------------------------------------------
def test_event_list_is_the_preregistered_one() -> None:
    ids = {e.id for e in EVENTS}
    assert "truckers_strike_2018" in ids and len(EVENTS) == 6
    strike = next(e for e in EVENTS if e.id == "truckers_strike_2018")
    assert (strike.start, strike.end) == (
        date(2018, 5, 21),
        date(2018, 5, 31),
    ) and strike.kpis == "all"
    cup = sorted(e.start for e in EVENTS if e.id.startswith("world_cup"))
    assert cup == [
        date(2018, 6, 17),
        date(2018, 6, 22),
        date(2018, 6, 27),
        date(2018, 7, 2),
        date(2018, 7, 6),
    ]
    assert all(type(e.start) is date and type(e.end) is date for e in EVENTS)  # never datetimes


def test_event_matching_is_inclusive_and_scoped_by_kpi() -> None:
    assert matching_events(EVENTS, date(2018, 5, 21), "aov") == ["truckers_strike_2018"]
    assert matching_events(EVENTS, date(2018, 5, 31), "avg_delivery_days") == [
        "truckers_strike_2018"
    ]
    assert matching_events(EVENTS, date(2018, 5, 20), "orders") == []
    assert matching_events(EVENTS, date(2018, 6, 1), "orders") == []
    assert matching_events(EVENTS, date(2018, 6, 17), "orders") == ["world_cup_2018_brazil_group_1"]
    assert (
        matching_events(EVENTS, date(2018, 6, 17), "aov") == []
    )  # World Cup is orders and gmv only
    assert matching_events(EVENTS, date(2018, 6, 16), "orders") == []


def test_datetimes_and_timestamps_are_rejected_so_no_timezone_can_shift_a_date() -> None:
    with pytest.raises(TypeError, match="calendar date"):
        matching_events(EVENTS, datetime(2018, 6, 17, 12, 0, tzinfo=UTC), "orders")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="calendar date"):
        matching_events(EVENTS, pd.Timestamp("2018-06-17"), "orders")  # type: ignore[arg-type]


def test_a_utc_instant_must_be_converted_to_the_brazilian_date_first() -> None:
    """02:30 UTC on 18 June is 23:30 on 17 June in Sao Paulo. Matching uses the local date."""
    zoneinfo = pytest.importorskip("zoneinfo")
    try:
        sao_paulo = zoneinfo.ZoneInfo("America/Sao_Paulo")
    except zoneinfo.ZoneInfoNotFoundError:
        pytest.skip("no tz database on this machine")
    instant = datetime(2018, 6, 18, 2, 30, tzinfo=UTC)
    local = instant.astimezone(sao_paulo).date()
    assert local == date(2018, 6, 17) and matching_events(EVENTS, local, "orders")
    assert not matching_events(
        EVENTS, instant.date(), "orders"
    )  # the naive UTC date would be wrong


def test_olist_timestamps_carry_no_timezone() -> None:
    """The source strings are naive local time, so no conversion exists anywhere in the date path."""
    sample = "2017-10-02 10:56:33"
    assert pd.to_datetime(sample, format="%Y-%m-%d %H:%M:%S").tzinfo is None


# ---------------------------------------------------------------------------
# Calendar windows
# ---------------------------------------------------------------------------
def test_calendar_windows_come_from_the_calendar_alone() -> None:
    assert black_friday(2017) == date(2017, 11, 24) and black_friday(2018) == date(2018, 11, 23)
    assert carnival_tuesday(2018) == date(2018, 2, 13)
    windows = {(w.name, w.start.year): w for w in calendar_windows(2017, 2018)}
    bf = windows[("black_friday_week", 2017)]
    assert (bf.start, bf.end) == (
        date(2017, 11, 23),
        date(2017, 11, 29),
    )  # matches the config event
    xmas = windows[("christmas_new_year", 2017)]
    assert (xmas.start, xmas.end) == (date(2017, 12, 20), date(2018, 1, 7))
    carnival = windows[("carnival", 2018)]
    assert (carnival.start, carnival.end) == (date(2018, 2, 10), date(2018, 2, 14))
    assert window_on(date(2018, 1, 3), list(windows.values())) is xmas
    assert window_on(date(2018, 3, 20), list(windows.values())) is None


# ---------------------------------------------------------------------------
# Last reliable day (the validity rule defined after diagnosis)
# ---------------------------------------------------------------------------
def _totals(values: list[float], start: str = "2018-06-01") -> pd.Series:
    return pd.Series(values, index=pd.date_range(start, periods=len(values)).date)


def test_last_reliable_day_is_the_day_before_the_terminal_collapse() -> None:
    totals = _totals([100.0] * 40 + [100.0, 30.0, 10.0, 2.0])  # 30 < 35% of 100
    assert last_reliable_day(totals) == totals.index[40]
    assert collapse_days(totals).iloc[-3:].all() and not collapse_days(totals).iloc[:41].any()


def test_a_gradual_decline_then_collapse_shaped_like_the_real_cutoff() -> None:
    totals = _totals([120.0] * 30 + [99.0, 70.0, 67.0, 44.0, 14.0, 4.0, 1.0])
    assert last_reliable_day(totals) == totals.index[33]  # 44 is 37% of ~120: not a collapse day


def test_a_collapse_that_recovers_is_not_the_end_of_the_data() -> None:
    totals = _totals([100.0] * 40 + [5.0] + [100.0] * 5)
    assert last_reliable_day(totals) == totals.index[-1]
    assert bool(collapse_days(totals).iloc[40])


def test_no_collapse_means_the_last_day_is_reliable() -> None:
    totals = _totals([100.0, 90.0, 110.0] * 20)
    assert last_reliable_day(totals) == totals.index[-1]


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
CTX = ClassifyContext(
    last_reliable=date(2018, 8, 28), windows=calendar_windows(2017, 2018), events=EVENTS
)


def test_classification_priority_is_cutoff_calendar_event_gmv_other() -> None:
    assert classify(date(2018, 8, 30), "gmv", "up", CTX) == "dataset_cutoff"
    assert classify(date(2018, 1, 3), "gmv", "up", CTX) == "calendar_window"  # beats a GMV spike
    assert classify(date(2018, 5, 24), "gmv", "up", CTX) == "known_event"  # beats a GMV spike
    assert classify(date(2018, 4, 27), "gmv", "up", CTX) == "gmv_spike"
    assert classify(date(2018, 4, 27), "gmv", "down", CTX) == "other"
    assert classify(date(2018, 4, 27), "orders", "up", CTX) == "other"


def test_breakdown_is_exclusive_and_the_event_figure_is_separate() -> None:
    anoms = pd.DataFrame(
        {
            "date": [
                date(2018, 8, 30),
                date(2018, 1, 3),
                date(2018, 5, 24),
                date(2018, 4, 27),
                date(2018, 6, 17),
            ],
            "kpi": ["orders", "gmv", "avg_delivery_days", "gmv", "orders"],
            "direction": ["down", "up", "up", "up", "down"],
        }
    )
    result = breakdown(anoms, CTX)
    assert result["exclusive"] == {
        "dataset_cutoff": 1,
        "calendar_window": 1,
        "known_event": 2,
        "gmv_spike": 1,
        "other": 0,
    }
    assert sum(result["exclusive"].values()) == result["total"] == 5
    assert result["matches_unlabelled_real_event"] == 2  # the strike day and the World Cup day
    assert result["alarm_days"] == 5


# ---------------------------------------------------------------------------
# Strict recall: flagged series and the targeted series
# ---------------------------------------------------------------------------
def _anoms(rows: list[dict[str, object]]) -> pd.DataFrame:
    base = {"granularity": "daily", "severity": 1.0, "related_segments": "[]"}
    return pd.DataFrame([{**base, **r} for r in rows])


def _scn(**kw: object) -> Scenario:
    spec = {
        "id": "T",
        "fault_type": "segment_level_shift",
        "dimension": "customer_state",
        "value": "PA",
        "start": date(2017, 10, 2),
        "duration_days": 14,
        "magnitude": 0.5,
        "mode": "drop",
        "seed": 1,
        "affected_kpis": ["orders", "gmv"],
    }
    spec.update(kw)
    return Scenario.model_validate(spec)


def test_flagged_series_includes_related_segments_not_only_the_headline() -> None:
    a = _anoms(
        [
            {
                "date": date(2017, 10, 3),
                "kpi": "orders",
                "dimension": "total",
                "segment": "all",
                "related_segments": json.dumps(["customer_state=PA", "payment_type=boleto"]),
            }
        ]
    )
    flags = flagged_series(a)
    assert set(zip(flags["dimension"], flags["segment"], strict=True)) == {
        ("total", "all"),
        ("customer_state", "PA"),
        ("payment_type", "boleto"),
    }
    assert len(flag_keys(a)) == 3


def test_strict_target_is_the_segments_own_series() -> None:
    flags = flagged_series(
        _anoms(
            [
                {
                    "date": date(2017, 10, 3),
                    "kpi": "orders",
                    "dimension": "total",
                    "segment": "all",
                },
                {
                    "date": date(2017, 10, 3),
                    "kpi": "orders",
                    "dimension": "customer_state",
                    "segment": "PA",
                },
                {
                    "date": date(2017, 10, 3),
                    "kpi": "orders",
                    "dimension": "customer_state",
                    "segment": "SP",
                },
                {
                    "date": date(2017, 10, 3),
                    "kpi": "orders",
                    "dimension": "category_en",
                    "segment": "PA",
                },
            ]
        )
    )
    plan = {("orders", "customer_state", "PA"): "weekly"}
    assert targeted_mask(flags, _scn(), plan).tolist() == [False, True, False, False]


def test_a_pooled_target_is_represented_by_the_other_series() -> None:
    flags = flagged_series(
        _anoms(
            [
                {
                    "date": date(2017, 10, 3),
                    "kpi": "orders",
                    "dimension": "customer_state",
                    "segment": "__other__",
                },
                {
                    "date": date(2017, 10, 3),
                    "kpi": "gmv",
                    "dimension": "customer_state",
                    "segment": "__other__",
                },
            ]
        )
    )
    plan = {("orders", "customer_state", "PA"): "pooled", ("gmv", "customer_state", "PA"): "weekly"}
    assert targeted_mask(flags, _scn(), plan).tolist() == [True, False]  # pooled for orders only


def test_faults_not_aimed_at_a_monitored_dimension_target_the_total() -> None:
    flags = flagged_series(
        _anoms(
            [
                {
                    "date": date(2017, 10, 3),
                    "kpi": "orders",
                    "dimension": "total",
                    "segment": "all",
                },
                {
                    "date": date(2017, 10, 3),
                    "kpi": "orders",
                    "dimension": "customer_state",
                    "segment": "SP",
                },
            ]
        )
    )
    for kind, dim in (("seller_delay", "seller_state"), ("pipeline_bug", "data_quality")):
        s = _scn(fault_type=kind, dimension=dim, value="SP", mode=None)
        assert targeted_mask(flags, s, {}).tolist() == [True, False]


# ---------------------------------------------------------------------------
# Flags and development protocol
# ---------------------------------------------------------------------------
def test_all_3b_flags_are_off_and_only_implemented_ones_can_be_switched_on() -> None:
    cfg = load_detection_config()
    assert not any(cfg.improvements.model_dump().values())
    assert ImprovementFlags(feed_guard=True).feed_guard is True  # change A is implemented
    for flag in ("log_gmv", "share_test", "calendar_downgrade"):  # B, C, D are not (yet)
        with pytest.raises(ValidationError, match="not implemented yet"):
            ImprovementFlags(**{flag: True})


def test_development_protocol_matches_the_adr() -> None:
    dev = load_detection_config().development
    assert dev is not None
    assert (dev.fit.start, dev.fit.end) == (date(2017, 4, 26), date(2017, 8, 27))
    assert (dev.test.start, dev.test.end) == (date(2017, 8, 28), date(2017, 12, 31))
    assert dev.benchmark_seed == 3101 and dev.first_fault_start == date(2017, 9, 18)
    assert (
        dev.test.end - dev.test.start
    ).days + 1 - 7 == 119  # clean days: test period minus Black Friday week


# ---------------------------------------------------------------------------
# The 2017 benchmark file
# ---------------------------------------------------------------------------
def test_size_bands_follow_the_preregistered_thresholds() -> None:
    assert [size_band(s) for s in (0.5, 0.05, 0.0499, 0.01, 0.0099, 0.0)] == [
        "large",
        "large",
        "medium",
        "medium",
        "small",
        "small",
    ]
    assert by_band({"a": 0.2, "b": 0.02, "c": 0.001}) == {
        "large": ["a"],
        "medium": ["b"],
        "small": ["c"],
    }


def test_allowed_starts_avoid_calendar_blocks_and_stay_inside_the_period() -> None:
    blocks = [
        w
        for w in calendar_windows(2017, 2017)
        if w.name in ("black_friday_week", "christmas_new_year")
    ]
    starts = allowed_starts(date(2017, 9, 18), date(2017, 12, 31), 14, blocks)
    assert starts[0] == date(2017, 9, 18) and starts[-1] == date(2017, 11, 2)  # the ADR's range
    one_day = allowed_starts(date(2017, 9, 18), date(2017, 12, 31), 1, blocks)
    assert (
        date(2017, 11, 14) in one_day
        and date(2017, 11, 20) not in one_day
        and date(2017, 11, 30) in one_day
    )
    assert max(one_day) == date(2017, 12, 12)


def test_the_committed_2017_benchmark_is_the_preregistered_grid() -> None:
    raw = yaml.safe_load(
        (REPO_ROOT / "config" / "scenarios" / "dev2017.yaml").read_text(encoding="utf-8")
    )
    scenarios = [Scenario.model_validate(s) for s in raw["scenarios"]]
    assert raw["seed"] == 3101 and len(scenarios) == 54
    assert dict(Counter(s.fault_type for s in scenarios)) == GRID_COUNTS
    assert (
        sum(GRID_COUNTS.values()) == 54
        and GRID_COUNTS["segment_level_shift"]
        + GRID_COUNTS["category_price_change"]
        + GRID_COUNTS["review_collapse"]
        == 27
    )

    for kind in ("segment_level_shift", "category_price_change", "review_collapse"):
        group = [s for s in scenarios if s.fault_type == kind]
        assert Counter(s.size_tier for s in group) == {"large": 3, "medium": 3, "small": 3}
        assert Counter(s.magnitude for s in group) == {0.15: 3, 0.30: 3, 0.60: 3}
    pay = [s for s in scenarios if s.fault_type == "payment_failure"]
    assert Counter(s.value for s in pay) == {
        "credit_card": 3,
        "boleto": 3,
        "voucher": 3,
        "debit_card": 3,
    }
    delay = [s for s in scenarios if s.fault_type == "seller_delay"]
    assert Counter(s.magnitude for s in delay) == {3.0: 3, 7.0: 3, 14.0: 3}
    assert Counter(s.size_tier for s in delay) == {"large": 3, "medium": 3, "small": 3}
    bugs = [s for s in scenarios if s.fault_type == "pipeline_bug"]
    assert Counter((s.mode, s.magnitude) for s in bugs) == {
        (m, x): 1 for m in ("drop", "duplicate") for x in (0.3, 0.6, 1.0)
    }
    assert {s.duration_days for s in scenarios if s.fault_type != "pipeline_bug"} == {14}
    assert {s.duration_days for s in bugs} == {1}

    blocks = [
        w
        for w in calendar_windows(2017, 2017)
        if w.name in ("black_friday_week", "christmas_new_year")
    ]
    dev = load_detection_config().development
    assert dev is not None
    for s in scenarios:
        extra = int(s.magnitude) if s.fault_type == "seller_delay" else 0
        tail_end = s.start + timedelta(days=s.duration_days - 1 + extra + 7)
        assert s.start >= dev.first_fault_start and tail_end <= dev.test.end, s.id
        assert all(tail_end < b.start or s.start > b.end for b in blocks), (
            f"{s.id} overlaps a calendar window"
        )
    assert len({s.seed for s in scenarios}) == 54  # every scenario has its own row-selection seed


# ---------------------------------------------------------------------------
# Target share (size bands) against a direct count
# ---------------------------------------------------------------------------
def test_target_share_matches_a_direct_count(dense_built: BuildResult) -> None:
    dev_like = load_detection_config().evaluation
    with ScenarioWorkspace(dense_built.warehouse_path) as ws:
        sp = target_share(ws.con, _scn(value="SP"), dev_like)
        total, sp_n = ws.con.execute(
            "SELECT count(*), count(*) FILTER (WHERE customer_state = 'SP') FROM wh.marts.fct_orders "
            "WHERE purchase_date BETWEEN ? AND ?",
            [dev_like.start, dev_like.end],
        ).fetchall()[0]
        assert sp == pytest.approx(sp_n / total)
        assert (
            target_share(
                ws.con,
                _scn(fault_type="pipeline_bug", dimension="data_quality", value="x", mode=None),
                dev_like,
            )
            == 1.0
        )
        assert (
            0
            < (
                target_share(
                    ws.con,
                    _scn(
                        fault_type="seller_delay", dimension="seller_state", value="SP", mode=None
                    ),
                    dev_like,
                )
                or 0
            )
            <= 1
        )
