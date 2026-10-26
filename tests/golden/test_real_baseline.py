"""The Phase 3 baseline on the REAL Olist data must be reproduced exactly (local only, slow).

    uv run pytest -m golden tests/golden/test_real_baseline.py       (make golden)

This re-runs the 2018 evaluation with every 3b flag off and compares it with
`evaluation/golden/phase3_baseline_2018.json`, frozen from the run on commit 11147b0. It is a
REGRESSION CHECK: it reproduces numbers that are already known and tunes nothing. It needs the
real warehouse, so it is skipped in CI.
"""

from __future__ import annotations

import json

import pytest

from core.settings import REPO_ROOT, get_settings
from ds.catalogue import load_detection_config
from ds.detection import Detector
from evaluation.protocol import protocols, run_protocol
from pipelines.faults import load_scenarios
from tests.golden.synthetic_inputs import assert_same

GOLDEN_PATH = REPO_ROOT / "evaluation" / "golden" / "phase3_baseline_2018.json"
DETECTOR = REPO_ROOT / "config" / "detector.json"
pytestmark = pytest.mark.golden


def _available() -> bool:
    return get_settings().duckdb_path.is_file() and DETECTOR.is_file()


@pytest.mark.skipif(not _available(), reason="needs the real warehouse (not available in CI)")
def test_phase3_baseline_is_reproduced_exactly() -> None:
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    cfg = load_detection_config()
    assert not any(cfg.improvements.model_dump().values()), "all 3b flags must be off"
    detector = Detector.load(DETECTOR)
    assert detector.version == golden["meta"]["detector_version"], (
        "detector.json changed since the baseline"
    )

    settings = get_settings()
    result = run_protocol(
        protocols(cfg)["eval2018"],
        warehouse=settings.duckdb_path,
        detector=detector,
        scenarios=load_scenarios(),
        cache_dir=settings.reports_dir / "cache",
        cfg=cfg,
    )
    assert result.meta["data_hash"] == golden["meta"]["data_hash"], (
        "warehouse data changed since the baseline"
    )

    # headline numbers, exactly
    s, g = result.summary, golden["summary"]
    for key in (
        "scenarios",
        "scenarios_magnitude_ge_20pct",
        "clean_days",
        "clean_tests",
        "clean_anomalies",
    ):
        assert s[key] == g[key], key
    for key in (
        "recall",
        "recall_magnitude_ge_20pct",
        "false_alarms_per_30_days",
        "precision_fault_caused",
        "median_delay_days",
        "mean_delay_days",
    ):
        assert s[key] == g[key], f"{key}: {s[key]!r} != {g[key]!r}"
    lenient = s["extended"]["lenient"]["ge20"]
    assert (lenient["k"], lenient["n"]) == (9, 16), "recall at >= 20% must be 9/16"
    assert round(s["false_alarms_per_30_days"], 1) == 9.3
    assert s["clean_anomalies"] == 75

    # the same alerts and the same scenario outcomes
    got_keys = sorted(
        [str(a["date"]), a["kpi"], a["granularity"], a["dimension"], a["segment"]]
        for a in result.clean_anoms.to_dict("records")
    )
    assert got_keys == sorted(golden["clean_anomaly_keys"])
    got = {r["id"]: r for r in result.rows}
    assert set(got) == {r["id"] for r in golden["scenarios"]}
    for want in golden["scenarios"]:
        have = {k: got[want["id"]][k] for k in want}
        assert_same(json.loads(json.dumps(have, default=str)), want)
