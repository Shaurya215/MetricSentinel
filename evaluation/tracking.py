"""MLflow helpers. Every logged run carries its versions (FR-086, NFR-016)."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import duckdb

from core.settings import REPO_ROOT, get_settings

os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")


def git_sha() -> str:
    """Current commit, with `-dirty` when the working tree has uncommitted changes."""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{sha}-dirty" if dirty else sha


def warehouse_data_hash(con: duckdb.DuckDBPyConnection) -> str:
    row = con.execute("SELECT value FROM meta.build WHERE key = 'data_hash'").fetchone()
    return str(row[0]) if row else "unknown"


@contextmanager
def tracked_run(experiment: str, run_name: str, tags: dict[str, Any]) -> Iterator[Any]:
    """Start an MLflow run with the version tags required by FR-086."""
    import mlflow

    mlflow.set_tracking_uri(get_settings().mlflow_tracking_uri)
    mlflow.set_experiment(experiment)
    with mlflow.start_run(run_name=run_name) as run:
        mlflow.set_tags({"git_sha": git_sha(), **{k: str(v) for k, v in tags.items()}})
        yield run
