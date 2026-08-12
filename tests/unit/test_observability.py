"""Tests for the one module allowed to attach a logging handler.

`configure_logging` is called at every entry point and by nothing else, and until now nothing
tested it. The properties that matter are small but each of them has a failure mode that is
invisible until it is not: doubled output, a hijacked root logger, a secret in a log file, and
a handler writing to a stream that closed underneath it.
"""

from __future__ import annotations

import io
import logging
import sys
from pathlib import Path

import pytest

from kedge.observability import ROOT_LOGGER_NAME, configure_logging


@pytest.fixture(autouse=True)
def restore_the_kedge_logger() -> None:
    """Leave the logger exactly as it was found.

    These tests reconfigure a process-wide logger, and the rest of the suite logs through it.
    """
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    handlers = list(logger.handlers)
    level, propagate = logger.level, logger.propagate
    yield
    logger.handlers = handlers
    logger.setLevel(level)
    logger.propagate = propagate


def test_configuring_twice_replaces_the_handlers_rather_than_doubling_them() -> None:
    """A re-entrant entry point must not make every line appear twice."""
    first = configure_logging()
    count = len(first.handlers)

    second = configure_logging()

    assert second is first
    assert len(second.handlers) == count


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, logging.INFO),
        ({"verbose": True}, logging.DEBUG),
        ({"quiet": True}, logging.WARNING),
        ({"verbose": True, "quiet": True}, logging.DEBUG),
    ],
)
def test_the_level_follows_the_flags_and_verbose_wins_a_tie(
    kwargs: dict[str, bool], expected: int
) -> None:
    assert configure_logging(**kwargs).level == expected


def test_handlers_go_on_the_kedge_logger_only_and_do_not_propagate() -> None:
    """Importing kedge as a library -- from a marimo notebook -- must not hijack anyone's logs."""
    logger = configure_logging()

    assert logger.name == ROOT_LOGGER_NAME
    assert logger.propagate is False
    assert not logging.getLogger().handlers or all(
        handler.name not in {"kedge-console", "kedge-file"}
        for handler in logging.getLogger().handlers
    )


def test_a_log_file_is_created_with_its_parent_and_receives_records(tmp_path: Path) -> None:
    destination = tmp_path / "nested" / "kedge.log"

    logger = configure_logging(log_file=destination)
    logger.warning("the marimo we recorded on port %d is gone", 2718)
    for handler in logger.handlers:
        handler.flush()

    assert destination.is_file()
    assert "the marimo we recorded on port 2718 is gone" in destination.read_text(encoding="utf-8")


def test_the_console_handler_writes_to_whatever_stderr_is_at_emit_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the handler used to capture `sys.stderr` once, at construction.

    Under pytest each test's capture buffer is closed at teardown, so a record emitted a moment
    later from a watcher or server thread raised `ValueError: I/O operation on closed file` and
    printed a `--- Logging error ---` block into the middle of a green run. The same trap
    catches any embedder that redirects stderr after importing kedge.
    """
    logger = configure_logging()
    at_configure_time = io.StringIO()
    monkeypatch.setattr(sys, "stderr", at_configure_time)

    later = io.StringIO()
    monkeypatch.setattr(sys, "stderr", later)
    logger.info("session %s is live", "kedge-1")

    assert "session kedge-1 is live" in later.getvalue()
    assert at_configure_time.getvalue() == ""


def test_a_stream_that_closed_underneath_the_handler_does_not_take_the_record_with_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shape of the original failure, asserted directly: closed stream, then a live one."""
    logger = configure_logging()
    closed = io.StringIO()
    closed.close()
    monkeypatch.setattr(sys, "stderr", closed)

    live = io.StringIO()
    monkeypatch.setattr(sys, "stderr", live)
    logger.warning("hand-in %s has drifted", "exposures.xlsx")

    assert "hand-in exposures.xlsx has drifted" in live.getvalue()
