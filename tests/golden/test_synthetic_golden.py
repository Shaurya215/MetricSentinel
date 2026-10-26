"""Refactor safety: the evaluation code must reproduce the Phase 3 output EXACTLY (synthetic data).

`synthetic_baseline.json` was captured from the Phase 3 code (commit 5720997) before any 3b change.
Discrete values (alert keys, counts, outcomes, delays) must match exactly; floats to a relative 1e-9
(DuckDB multi-threaded sums differ in the last digit between runs).
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from ds.catalogue import load_catalogue, load_detection_config
from ds.detection import detect_range
from ds.forecasting import load_series
from evaluation.detection import clean_forecasts, daterange, run_scenario
from evaluation.protocol import ALPHAS, SEVERITIES, protocols, run_protocol
from pipelines.build import BuildResult
from tests.golden.synthetic_inputs import (
    assert_same,
    fingerprint,
    synthetic_detector,
    synthetic_scenarios,
)

GOLDEN = json.loads(Path(__file__).with_name("synthetic_baseline.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def cache_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("golden_cache")


def test_phase3_entry_points_are_unchanged(dense_built: BuildResult, cache_dir: Path) -> None:
    """The functions Phase 3 called, called the Phase 3 way, give the Phase 3 answer."""
    catalogue, cfg, detector = load_catalogue(), load_detection_config(), synthetic_detector()
    con = duckdb.connect(str(dense_built.warehouse_path), read_only=True)
    try:
        series = load_series(con, catalogue)
        data_hash = con.execute("SELECT value FROM meta.build WHERE key = 'data_hash'").fetchall()[
            0
        ][0]
    finally:
        con.close()
    fc = clean_forecasts(series, detector, catalogue, cfg, data_hash, cache_dir)
    clean, tests = detect_range(
        series, fc, daterange(cfg.evaluation.start, cfg.evaluation.end), detector, catalogue, cfg
    )
    rows = [
        run_scenario(
            s, series, fc, clean, detector, catalogue, cfg, warehouse=dense_built.warehouse_path
        )
        for s in synthetic_scenarios()
    ]
    assert_same(fingerprint(clean, tests, rows), GOLDEN)


def test_protocol_runner_with_flags_off_reproduces_phase3(
    dense_built: BuildResult, cache_dir: Path
) -> None:
    """The new protocol runner (extended reporting on) must not change a single Phase 3 number."""
    cfg = load_detection_config()
    assert not any(cfg.improvements.model_dump().values()), "all 3b flags must be off"
    result = run_protocol(
        protocols(cfg)["eval2018"],
        warehouse=dense_built.warehouse_path,
        detector=synthetic_detector(),
        scenarios=synthetic_scenarios(),
        cache_dir=cache_dir,
        cfg=cfg,
    )
    assert_same(fingerprint(result.clean_anoms, result.clean_tests, result.rows), GOLDEN)

    # the extended figures agree with the Phase 3 ones they extend
    ext = result.summary["extended"]
    assert ext["lenient"]["all"]["k"] == sum(r["detected"] for r in GOLDEN["scenarios"])
    assert ext["false_alarms"]["total"] == len(GOLDEN["clean_anomalies"])
    base = next(
        c for c in ext["operating_curve"] if c["alpha"] == 0.05 and c["severity_min"] == 0.0
    )
    assert base["clean_alarms"] == len(GOLDEN["clean_anomalies"])
    assert base["recall_all"]["k"] == ext["lenient"]["all"]["k"]
    assert {c["alpha"] for c in ext["operating_curve"]} == set(ALPHAS)
    assert {c["severity_min"] for c in ext["operating_curve"]} == set(SEVERITIES)
    assert sum(ext["false_alarms"]["exclusive"].values()) == ext["false_alarms"]["total"]
