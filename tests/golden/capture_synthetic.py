"""Produce `synthetic_baseline.json`. Run ONLY against the Phase 3 code (commit 5720997).

DUCKDB_PATH is set below so the Phase 3 harness, which reads the warehouse path from
settings, uses the synthetic warehouse. It uses only entry points that exist in the
Phase 3 code: clean_forecasts, detect_range and run_scenario.

uv run python -m tests.golden.capture_synthetic
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

if __name__ == "__main__":
    work = Path(tempfile.mkdtemp(prefix="golden_capture_"))

    import duckdb

    from core.settings import get_settings
    from ds.catalogue import load_catalogue, load_detection_config
    from ds.detection import detect_range
    from ds.forecasting import load_series
    from evaluation.detection import clean_forecasts, daterange, run_scenario
    from pipelines import synthetic
    from tests.golden.synthetic_inputs import fingerprint, synthetic_detector, synthetic_scenarios
    from tests.unit.conftest import build_synthetic

    raw = work / "raw"
    synthetic.generate(raw, n_orders=20_000, n_sellers=60, n_products=150)
    built = build_synthetic(raw, work / "wh")
    # Build first (the build refuses to write fake data to the real warehouse path), then point
    # settings at the result, because the Phase 3 harness reads the warehouse from settings.
    os.environ["DUCKDB_PATH"] = str(built.warehouse_path)
    get_settings.cache_clear()

    catalogue, cfg, detector = load_catalogue(), load_detection_config(), synthetic_detector()
    con = duckdb.connect(str(built.warehouse_path), read_only=True)
    try:
        series = load_series(con, catalogue)
        data_hash = con.execute("SELECT value FROM meta.build WHERE key = 'data_hash'").fetchall()[
            0
        ][0]
    finally:
        con.close()
    fc = clean_forecasts(series, detector, catalogue, cfg, data_hash, work / "cache")
    clean, clean_tests = detect_range(
        series, fc, daterange(cfg.evaluation.start, cfg.evaluation.end), detector, catalogue, cfg
    )
    rows = [
        run_scenario(s, series, fc, clean, detector, catalogue, cfg) for s in synthetic_scenarios()
    ]

    out = Path(__file__).with_name("synthetic_baseline.json")
    payload = fingerprint(clean, clean_tests, rows)
    out.write_text(json.dumps(payload, indent=1, default=str) + "\n", encoding="utf-8")
    print(
        f"wrote {out}: {len(payload['clean_anomalies'])} clean anomalies, "
        f"{len(rows)} scenarios, {sum(r['detected'] for r in rows)} detected",
        file=sys.stderr,
    )
