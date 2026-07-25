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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["ROOT_LOGGER_NAME", "configure_logging"]

ROOT_LOGGER_NAME = "kedge"

_HANDLER_NAME = "kedge-console"
_FILE_HANDLER_NAME = "kedge-file"
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATE_FORMAT = "%H:%M:%S"


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

    console = logging.StreamHandler()
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
