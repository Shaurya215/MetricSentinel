"""Run detection for one simulated day: `make detect DATE=2018-03-05`.

Fits the chosen models at the Monday on or before the date, on data strictly before
that Monday, then tests the date. Results are stored in `metrics.anomaly_candidates`
and `metrics.detection_runs` (FR-034) and printed. The run time is reported, because
NFR-004 requires one day's detection over all series in under 60 seconds.
"""

from __future__ import annotations

import argparse
import time
import uuid
from datetime import UTC, date, datetime

import duckdb
import pandas as pd

from core.logging import configure_logging, get_logger, run_context
from core.settings import get_settings
from ds.catalogue import load_catalogue, load_detection_config
from ds.detection import Detector, detect_range, forecast_period
from ds.forecasting import load_series

log = get_logger(__name__)


def run(sim_date: date) -> tuple[pd.DataFrame, dict[str, object]]:
    catalogue, cfg, detector = load_catalogue(), load_detection_config(), Detector.load()
    started = time.perf_counter()
    con = duckdb.connect(str(get_settings().duckdb_path))
    try:
        series = load_series(con, catalogue)
        series = series[
            series["ds"] <= pd.Timestamp(sim_date)
        ]  # simulated clock: nothing after sim_date
        forecasts = forecast_period(series, sim_date, sim_date, detector, catalogue, cfg)
        anomalies, tests = detect_range(series, forecasts, [sim_date], detector, catalogue, cfg)
        seconds = round(time.perf_counter() - started, 2)
        data_hash = con.execute("SELECT value FROM meta.build WHERE key = 'data_hash'").fetchall()[
            0
        ][0]
        run_info: dict[str, object] = {
            "run_id": uuid.uuid4().hex[:12],
            "sim_date": sim_date,
            "data_hash": data_hash,
            "detector_version": detector.version,
            "tests": tests,
            "anomalies": len(anomalies),
            "seconds": seconds,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        con.execute(
            "CREATE TABLE IF NOT EXISTS metrics.detection_runs (run_id VARCHAR, sim_date DATE, data_hash VARCHAR, "
            "detector_version VARCHAR, tests INTEGER, anomalies INTEGER, seconds DOUBLE, created_at VARCHAR)"
        )
        con.execute(
            "INSERT INTO metrics.detection_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            list(run_info.values()),
        )
        frame = anomalies.assign(run_id=run_info["run_id"])
        con.register("new_anomalies", frame)
        con.execute(
            "CREATE TABLE IF NOT EXISTS metrics.anomaly_candidates AS SELECT * FROM new_anomalies LIMIT 0"
        )
        con.execute("DELETE FROM metrics.anomaly_candidates WHERE date = ?", [sim_date])
        con.execute("INSERT INTO metrics.anomaly_candidates SELECT * FROM new_anomalies")
    finally:
        con.close()
    return anomalies, run_info


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect anomalies for one simulated day.")
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    args = parser.parse_args()
    configure_logging()
    with run_context(component="detect", sim_date=str(args.date)):
        anomalies, info = run(args.date)
        log.info("detection_finished", **{k: str(v) for k, v in info.items()})
    print(
        f"\n{args.date}: {info['tests']} tests, {info['anomalies']} anomaly record(s), {info['seconds']} s"
    )
    if not anomalies.empty:
        cols = [
            "kpi",
            "granularity",
            "dimension",
            "segment",
            "observed",
            "expected",
            "lower",
            "upper",
            "q_value",
            "related_segments",
        ]
        print(anomalies[cols].to_string(index=False))


if __name__ == "__main__":
    main()
