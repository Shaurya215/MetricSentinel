"""Structured logging (NFR-017: JSON logs carrying a run id on every line).

Usage::

    from core.logging import configure_logging, get_logger, run_context

    configure_logging()
    log = get_logger(__name__)

    with run_context() as run_id:  # or run_context("investigation-42")
        log.info("cycle_started", sim_date="2018-01-05")

The run id lives in a `contextvars` variable, so it is attached automatically to
every log line emitted inside the context — including from called modules and
from other asyncio tasks — without being passed around by hand.
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, reset_contextvars

from core.settings import get_settings

_configured = False


def configure_logging(level: str | None = None, fmt: str | None = None) -> None:
    """Configure structlog and the stdlib root logger. Idempotent."""
    global _configured
    settings = get_settings()
    level = (level or settings.log_level).upper()
    fmt = fmt or settings.log_format

    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.EventRenamer("event"),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level, force=True)
    _configured = True


def get_logger(name: str | None = None) -> Any:
    """Return a bound structlog logger, configuring logging on first use.

    The module name is bound explicitly as `logger`, because the print-logger
    factory has no name of its own for `add_logger_name` to read.
    """
    if not _configured:
        configure_logging()
    log = structlog.get_logger()
    return log.bind(logger=name) if name else log


def new_run_id() -> str:
    """Return a short, sortable-enough identifier for one run."""
    return uuid.uuid4().hex[:12]


@contextmanager
def run_context(run_id: str | None = None, **extra: Any) -> Iterator[str]:
    """Bind `run_id` (and any extra fields) to every log line inside the block."""
    run_id = run_id or new_run_id()
    tokens = bind_contextvars(run_id=run_id, **extra)
    try:
        yield run_id
    finally:
        reset_contextvars(**tokens)
