"""One real marimo server per module, and the live drivers the contract tests drive it with.

These tests are the automated form of ``docs/marimo-api.md`` and PLAN 6.1 mitigation 3. Everything
else in the suite asserts on generated *strings*: ``tests/unit/test_driver_codegen.py`` proves the
programs kedge builds say what they should, and nothing at all proves the kernel agrees. That is
the gap a private, explicitly unversioned API leaves open, and it is the gap these fixtures close.

**The server is module-scoped.** A ``marimo edit`` takes roughly four seconds to spawn and
bootstrap, and a driver call takes about a tenth of one, so a server per test would spend all its
time starting up. The cost is shared notebook state, and the tests are written for it: each one
creates cells under names nobody else uses, and reads state back rather than assuming it.

**Two servers, deliberately.** The read-before-write guard (``StaleCellError``) is armed per cell
and disarmed by reading a cell's code — including by ``list_cells(with_code=True)``, which reads
every cell in the notebook at once. A guard test sharing a server with everything else would
therefore pass or fail depending on what ran before it. :func:`guard_marimo` gives it a server of
its own so the property is structural rather than a convention about test ordering.

**Teardown is the part that must not be clever.** Windows delivers no ``SIGTERM``, so a leaked
server keeps its port until marimo's own ``--timeout`` expires. Every server is stopped in a
``finally`` through :func:`kedge.lifecycle.stop_marimo`, which escalates from an authenticated
``POST /api/kernel/shutdown`` to ``CTRL_BREAK_EVENT`` to ``taskkill /F /T``; the process handle is
killed directly if that somehow leaves it running; an ``atexit`` hook covers an interpreter that
dies mid-module; and the fixture asserts afterwards that nothing answers on the port.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import kedge
from kedge.config import Config, LoadedConfig
from kedge.lifecycle import (
    establish_session,
    health_check,
    launch_marimo,
    stop_marimo,
    teardown,
)
from kedge.notebook.driver import NotebookDriver
from kedge.workspace import Workspace

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

logger = logging.getLogger(__name__)

__all__ = [
    "GUARD_FORCED_CODE",
    "GUARD_NOTEBOOK",
    "MAIN_NOTEBOOK",
    "LiveMarimo",
    "serving",
    "wait_for_notebook",
]

# A notebook has to exist before ``marimo edit`` will serve it. The two seeded cells make the file
# a realistic notebook rather than an empty shell, and the unnamed one is there so a test can
# assert marimo really does report it as "_" rather than "" (docs/marimo-api.md 7.8). Neither is
# edited by any test: both are unread, and editing an unread cell is the guard server's subject.
MAIN_NOTEBOOK = """import marimo

app = marimo.App()


@app.cell
def scaffold():
    scaffold_ready = True
    return (scaffold_ready,)


@app.cell
def _():
    scaffold_unnamed = 0
    return (scaffold_unnamed,)


if __name__ == "__main__":
    app.run()
"""

# Cells kedge has never read and never wrote: exactly the state the staleness guard exists for.
# Each defines its own name so editing one cannot trip the single-definition rule in another.
GUARD_NOTEBOOK = """import marimo

app = marimo.App()


@app.cell
def guard_unread():
    guard_unread_value = 1
    return (guard_unread_value,)


@app.cell
def guard_forced():
    guard_forced_value = 2
    return (guard_forced_value,)


@app.cell
def guard_listed():
    guard_listed_value = 3
    return (guard_listed_value,)


if __name__ == "__main__":
    app.run()
"""

GUARD_FORCED_CODE = "guard_forced_value = 2"
"""The seeded body of ``guard_forced``, as marimo reports it: the decorated function's body only."""

_SHUTDOWN_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True, slots=True)
class LiveMarimo:
    """A running marimo server a test module owns, and the details needed to drive it."""

    workspace: Workspace
    session_id: str

    @property
    def base_url(self) -> str:
        """The server's origin."""
        return self.workspace.require_marimo().base_url

    @property
    def token(self) -> str:
        """The token this server was launched with."""
        return self.workspace.require_marimo().token

    @property
    def notebook_path(self) -> Path:
        """The notebook ``.py`` the server has open, on disk."""
        return self.workspace.notebook_path

    def driver(self) -> NotebookDriver:
        """Build a driver bound to this server's session.

        A fresh driver per test rather than a shared one, because the driver owns an
        ``httpx.AsyncClient`` and pytest-asyncio gives each test its own event loop: a client
        created on a loop that has since closed fails in ways that look like kernel flakiness.
        The session id is the same for every driver, since edit mode holds at most one session
        and asserting a second id would evict the first (docs/marimo-api.md 5.3).
        """
        return NotebookDriver(
            base_url=self.base_url,
            token=self.token,
            session_id=self.session_id,
            verify=True,
        )


@contextlib.contextmanager
def serving(root: Path, source: str) -> Iterator[LiveMarimo]:
    """Spawn a real marimo on ``source``, yield it, and make sure it is gone afterwards."""
    workbook = root / "process.xlsx"
    workbook.write_bytes(b"")
    workspace = Workspace.for_workbook(
        workbook,
        loaded_config=LoadedConfig(config=Config()),
        user_directory=root / "kedge-home",
    )
    workspace.ensure_dirs()
    workspace.notebook_path.write_text(source, encoding="utf-8")

    process = None
    base_url = None
    try:
        process = launch_marimo(workspace, kedge_version=kedge.__version__)
        # Covers an interpreter that exits without unwinding this contextmanager. teardown()
        # never raises and is a no-op once the workspace has been detached.
        atexit.register(teardown, workspace)
        base_url = workspace.require_marimo().base_url
        session_id = establish_session(workspace)
        yield LiveMarimo(workspace=workspace, session_id=session_id)
    finally:
        with contextlib.suppress(Exception):
            if workspace.has_marimo:
                stop_marimo(workspace)
        if process is not None and process.poll() is None:
            # stop_marimo already escalated as far as taskkill /F /T; if the handle is still
            # live after that, kill it directly rather than leaving the port held.
            logger.warning("marimo pid %d survived stop_marimo; killing the handle", process.pid)
            with contextlib.suppress(Exception):
                process.kill()
                process.wait(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
        if base_url is not None:
            assert not health_check(base_url), (
                f"a marimo server is still answering on {base_url} after teardown; the contract "
                f"tests have leaked a process and the port with it"
            )


@pytest.fixture(scope="module")
def live_marimo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LiveMarimo]:
    """The shared server most contract tests drive."""
    with serving(tmp_path_factory.mktemp("kedge-contract-live"), MAIN_NOTEBOOK) as server:
        yield server


@pytest.fixture(scope="module")
def guard_marimo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LiveMarimo]:
    """A server whose notebook holds cells kedge has neither written nor read."""
    with serving(tmp_path_factory.mktemp("kedge-contract-guard"), GUARD_NOTEBOOK) as server:
        yield server


@pytest.fixture
async def driver(live_marimo: LiveMarimo) -> AsyncIterator[NotebookDriver]:
    """A driver on the shared server, closed after the test whatever happened in it."""
    async with live_marimo.driver() as live:
        yield live


@pytest.fixture
async def guard_driver(guard_marimo: LiveMarimo) -> AsyncIterator[NotebookDriver]:
    """A driver on the guard server."""
    async with guard_marimo.driver() as live:
        yield live


def wait_for_notebook(path: Path, needle: str, *, timeout: float = 30.0) -> str:
    """Return the notebook source once ``needle`` appears in it on disk.

    Kernel-side mutations are persisted by the *server*, on a worker thread, after the execute
    response has already been sent — so the file is eventually consistent with the kernel and a
    bare read straight after a mutation races it.

    Args:
        path: The notebook ``.py``.
        needle: Text that proves the write landed.
        timeout: How long to wait before giving up.

    Returns:
        The file's contents.

    Raises:
        AssertionError: The text never appeared.
    """
    deadline = time.monotonic() + timeout
    source = ""
    while time.monotonic() < deadline:
        source = path.read_text(encoding="utf-8")
        if needle in source:
            return source
        time.sleep(0.2)
    message = f"{needle!r} never reached {path} within {timeout:g}s; the file holds:\n{source}"
    raise AssertionError(message)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark everything under ``tests/contract`` as ``contract``, marker or no marker.

    The test modules carry ``pytestmark`` themselves. This is the backstop: the default
    ``addopts`` deselects ``contract``, and a future test file that forgot the marker would
    otherwise spawn a real marimo during an ordinary ``uv run pytest``.
    """
    here = Path(__file__).parent
    for item in items:
        path = getattr(item, "path", None)
        if path is not None and here in path.parents:
            item.add_marker(pytest.mark.contract)
