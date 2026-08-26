"""Wiring the real agent loop into the real server, which is neither module's job.

`CLAUDE.md`'s layering is ``analysis/ -> plan/ -> notebook/ -> agent/ -> server/``. Building an
application out of :class:`~kedge.agent.loop.KedgeAgent` and
:func:`~kedge.server.app.create_app` needs both ends of that arrow at once, so it belongs above
both rather than inside either, and this module is that place. Nothing in kedge imports it; it is
an entry point, and it imports downward into two layers exactly as an entry point should.

It used to live in ``agent/loop.py``, and the two imports it needs were function-local with a
comment explaining that a module-level import "would invert the layering". The deferral was real
but the argument was already false when it was written: the same file imported
``kedge.server.events`` at module scope, which executes ``kedge/server/__init__.py``, which
imports ``kedge.server.app``, which imports FastAPI. So ``import kedge.agent`` pulled in the whole
server package -- 1.25s and FastAPI loaded -- to keep an import out of the way that was already in
the way. Moving the turn vocabulary down to :mod:`kedge.turn` removed that edge; moving these two
functions out removed the reason to hide the remaining ones.

The one-liner the server's own ``--demo`` flag has no equivalent for::

    uv run python -c "from kedge.serve import serve; serve('book.xlsx', port=8731)"
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kedge.agent.loop import KedgeAgent
from kedge.server.app import create_app, run_server
from kedge.workspace import Workspace

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

    from kedge.agent.loop import ModelClient
    from kedge.analysis.model import WorkbookAnalysis
    from kedge.notebook.model import NotebookBridge

logger = logging.getLogger(__name__)

__all__ = ["build_agent_app", "serve"]


def build_agent_app(
    workspace: Workspace,
    *,
    driver: NotebookBridge | None = None,
    analysis: WorkbookAnalysis | None = None,
    client: ModelClient | None = None,
    version: str = "0.1.0",
) -> FastAPI:
    """Build the kedge server driven by the real loop rather than by ``ScriptedAgent``.

    Args:
        workspace: The workbook's workspace, with directories already ensured.
        driver: A live notebook bridge, where marimo is up.
        analysis: The workbook analysis; loaded from disk when omitted.
        client: The model endpoint; built from config and the keyring when omitted.
        version: Reported by ``/api/context``.

    Returns:
        The application, ready for :func:`kedge.server.app.run_server`.
    """
    agent = KedgeAgent.for_workspace(workspace, driver=driver, analysis=analysis, client=client)
    return create_app(workspace, agent=agent, demo=False, version=version)


def serve(
    workbook: Path | str,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    driver: NotebookBridge | None = None,
    log_level: str = "warning",
) -> None:
    """Run the kedge server against the real agent loop, blocking until it stops.

    The one-liner the server's own ``--demo`` flag has no equivalent for, because
    ``kedge.server.app.main`` refuses to start without a wired-up loop and this is the wiring::

        uv run python -c "from kedge.serve import serve; serve('book.xlsx', port=8731)"

    Requires an API key in the OS keyring for ``model.api_key_ref``. Without a live marimo the
    notebook tools degrade to saying so; everything that reads the workbook still works.
    """
    workspace = Workspace.for_workbook(workbook)
    workspace.ensure_dirs()
    app = build_agent_app(workspace, driver=driver)
    logger.warning("kedge on http://%s:%d against %s", host, port, workspace.workbook_path)
    run_server(app, host=host, port=port, log_level=log_level)
