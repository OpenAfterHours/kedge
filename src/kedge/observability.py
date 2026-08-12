"""Logging setup for the kedge entry points.

The one place in the codebase that attaches a handler. Library modules only ever call
``logging.getLogger(__name__)`` and log against it; they never configure handlers, never call
``logging.basicConfig``, and never touch the root logger. Handler installation is the entry
point's job, which in practice means the CLI calls :func:`configure_logging` once at startup.

Handlers are attached to the ``kedge`` namespace logger only, so importing kedge as a library
(from a marimo notebook, say) never hijacks anybody else's logging.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["ROOT_LOGGER_NAME", "configure_logging"]

ROOT_LOGGER_NAME = "kedge"

_HANDLER_NAME = "kedge-console"
_FILE_HANDLER_NAME = "kedge-file"
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATE_FORMAT = "%H:%M:%S"


class _StderrHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """A stream handler that resolves ``sys.stderr`` when it emits, not when it is built.

    ``logging.StreamHandler()`` captures whatever ``sys.stderr`` is at construction and holds
    that object forever. kedge configures logging once at entry, and by the time a record is
    written that object may be gone: under pytest each test's capture buffer is closed at
    teardown, so a record emitted from a watcher or server thread a moment later raised
    ``ValueError: I/O operation on closed file`` and printed a ``--- Logging error ---`` block
    into the middle of an otherwise green run. The same trap catches any embedder that
    redirects stderr after importing kedge.

    The standard library solves this the same way for its own last-resort handler
    (``logging._StderrHandler``); this is that, named and documented.
    """

    def __init__(self) -> None:
        super().__init__()

    @property
    def stream(self) -> Any:
        """Whatever ``sys.stderr`` is right now."""
        return sys.stderr

    @stream.setter
    def stream(self, _value: Any) -> None:
        """Ignored. ``StreamHandler.__init__`` assigns this, and the point is not to keep it."""


def configure_logging(
    *,
    verbose: bool = False,
    quiet: bool = False,
    log_file: Path | None = None,
) -> logging.Logger:
    """Attach handlers to the ``kedge`` logger and return it.

    Idempotent: calling it twice replaces the handlers rather than doubling every line, so a
    test or a re-entrant entry point cannot produce duplicate output. ``verbose`` selects DEBUG,
    ``quiet`` selects WARNING, and the default is INFO; ``verbose`` wins if both are set.
    """
    logger = logging.getLogger(ROOT_LOGGER_NAME)

    if verbose:
        level = logging.DEBUG
    elif quiet:
        level = logging.WARNING
    else:
        level = logging.INFO
    logger.setLevel(level)

    # Replace rather than append, so repeat calls cannot double up handlers.
    for existing in list(logger.handlers):
        if existing.name in (_HANDLER_NAME, _FILE_HANDLER_NAME):
            logger.removeHandler(existing)
            existing.close()

    console = _StderrHandler()
    console.name = _HANDLER_NAME
    console.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT))
    logger.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.name = _FILE_HANDLER_NAME
        file_handler.setFormatter(logging.Formatter(_FORMAT))
        logger.addHandler(file_handler)

    # kedge's own handlers are the only ones that should see these records.
    logger.propagate = False
    return logger
