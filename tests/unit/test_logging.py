"""Every log line inside a run context must carry the run id (NFR-017)."""

from __future__ import annotations

import json

import pytest

from core.logging import configure_logging, get_logger, new_run_id, run_context


@pytest.mark.unit
def test_run_id_is_attached_to_every_line(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", fmt="json")
    log = get_logger("test")

    with run_context("abc123") as run_id:
        log.info("first")
        log.info("second", extra_field=1)

    assert run_id == "abc123"
    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert len(lines) == 2
    assert all(line["run_id"] == "abc123" for line in lines)
    assert lines[1]["extra_field"] == 1


@pytest.mark.unit
def test_run_id_is_cleared_after_the_context(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="INFO", fmt="json")
    log = get_logger("test")

    with run_context():
        log.info("inside")
    log.info("outside")

    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert "run_id" in lines[0]
    assert "run_id" not in lines[1]


@pytest.mark.unit
def test_new_run_id_is_unique() -> None:
    assert new_run_id() != new_run_id()
