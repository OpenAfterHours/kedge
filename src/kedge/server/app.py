"""The kedge FastAPI application: one shell page, one API, loopback only.

Three processes, one machine (PLAN 2). This is the middle one: it owns the chat, the session
store and the agent loop, and it serves a shell page whose right-hand pane is an ``<iframe>``
pointing straight at the marimo server. The browser talks to marimo directly; nothing proxies it.

marimo is emphatically **not** mounted inside this app. ``create_asgi_app`` exists and is
tempting, and its own docstring says it "only works for applications that are in Run mode" — Run
mode hides code and disables editing, which is the opposite of the point (PLAN 1.4). Separate
process, always; if single-origin is ever wanted, reverse-proxy it.

Binding is checked rather than documented. ``create_app`` and :func:`run_server` refuse anything
that is not loopback, because "no auth" is only a defensible position while the socket is not
reachable from anywhere else.

The workspace is attached rather than required. ``create_app(workspace, agent=...)`` is unchanged
and is still what ``kedge open`` uses, but :func:`create_hub_app` builds the same application with
no workbook at all: the browser lands on the hub (``static/hub.html``), picks a workbook, and
:meth:`ServerState.attach` swaps the workspace and the agent into the running process. One
process, one port, one origin — the alternative, a second app mounted at ``/hub``, would have
meant two session stores and a redirect across origins for no gain.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from kedge.errors import KedgeError
from kedge.observability import configure_logging
from kedge.server.agent_seam import AgentLoop, AgentUnavailableError, ScriptedAgent, TurnRegistry
from kedge.server.events import EventBus, NotebookNotifier, notebook_mirror
from kedge.server.hub import router as hub_router
from kedge.server.routes import router
from kedge.server.sessions import SessionStore
from kedge.workspace import Workspace

logger = logging.getLogger(__name__)

__all__ = [
    "LOOPBACK_HOSTS",
    "STATIC_DIR",
    "ServerError",
    "ServerState",
    "WorkspaceNotAttachedError",
    "create_app",
    "create_demo_app",
    "create_hub_app",
    "main",
    "run_server",
]

STATIC_DIR = Path(__file__).parent / "static"

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
"""The only hosts kedge will bind. There is no deployment story here and there should not be."""


class ServerError(KedgeError):
    """The server could not be built or started as asked."""


class WorkspaceNotAttachedError(ServerError):
    """A request needed a workbook and the server has not been given one yet.

    Not an internal error: it is the normal state of a server started with ``kedge hub``, and the
    right response is to send the user to the hub to pick a workbook.
    """


@dataclass
class ServerState:
    """Everything a request handler needs, hung off ``app.state.kedge``.

    Not module-level globals: process-wide state belongs to a :class:`~kedge.workspace.Workspace`
    (PLAN 2.9, CONVENTIONS "no module-level mutable global state"), and a test that wants a whole
    server stands one of these up in a ``tmp_path``.

    ``workspace`` and ``agent`` are ``None`` only in hub mode, between the server starting and the
    user choosing a workbook. Every handler that needs them goes through :meth:`require_workspace`
    or :meth:`require_agent`, so "no workbook yet" is a 409 with an explanation rather than an
    ``AttributeError`` on ``None``.
    """

    workspace: Workspace | None
    store: SessionStore
    agent: AgentLoop | None
    bus: EventBus = field(default_factory=EventBus)
    turns: TurnRegistry = field(default_factory=TurnRegistry)
    static_dir: Path = STATIC_DIR
    demo: bool = False
    version: str = "0.1.0"
    user_directory: Path | None = None
    """Overrides ``~/.kedge`` for the hub's registry. ``None`` means the real one."""

    opens: dict[str, Any] = field(default_factory=dict)
    """In-flight and finished workbook opens, keyed by job id (see :mod:`kedge.server.hub`)."""

    @property
    def attached(self) -> bool:
        """Whether a workbook is open on this server."""
        return self.workspace is not None

    def require_workspace(self) -> Workspace:
        """Return the attached workspace, or explain that there is not one."""
        if self.workspace is None:
            msg = (
                "no workbook is open on this kedge server. Choose one from the hub, or start "
                "kedge against a workbook with `kedge open <workbook>`."
            )
            raise WorkspaceNotAttachedError(msg)
        return self.workspace

    def require_agent(self) -> AgentLoop:
        """Return the attached agent loop, or explain that there is not one."""
        if self.agent is None:
            msg = "no agent loop is attached, because no workbook is open. Choose one from the hub."
            raise WorkspaceNotAttachedError(msg)
        return self.agent

    def attach(
        self,
        workspace: Workspace,
        *,
        agent: AgentLoop,
        demo: bool = False,
        notifier: NotebookNotifier | None = None,
    ) -> None:
        """Adopt a workbook and the loop that drives it, in a server that had neither.

        Called once, by the hub, when the user opens a workbook. Switching to a *different*
        workbook is deliberately not supported in-process: the marimo subprocess, the notebook
        driver and the audit log are all bound to the workspace that owns them, and swapping one
        out from under a turn in flight is a class of bug worth not having. The hub restarts the
        open sequence instead, which is honest and takes the same few seconds it took first time.
        """
        if self.workspace is not None and self.workspace.key != workspace.key:
            msg = (
                f"this kedge server already has {self.workspace.workbook_path.name} open. Stop it "
                f"and start again for {workspace.workbook_path.name}; one server owns one "
                f"workbook and one marimo process (PLAN 2.9)."
            )
            raise ServerError(msg)
        self.workspace = workspace
        self.agent = agent
        self.demo = demo
        if notifier is not None:
            self.bus.add_observer(notebook_mirror(notifier))
        logger.info("attached workbook %s", workspace.workbook_path)


def create_app(
    workspace: Workspace,
    *,
    agent: AgentLoop,
    store: SessionStore | None = None,
    notifier: NotebookNotifier | None = None,
    demo: bool = False,
    version: str = "0.1.0",
) -> FastAPI:
    """Build the application for ``workspace``.

    Args:
        workspace: Owns the paths, the config and the marimo session. The server reads all three
            from here and never consults config itself.
        agent: The tool-calling loop. Anything satisfying
            :class:`~kedge.server.agent_seam.AgentLoop` will do, which is what lets the UI be
            exercised end to end against :class:`~kedge.server.agent_seam.ScriptedAgent`.
        store: The session store. Defaults to the workspace's ``sessions.sqlite``.
        notifier: The notebook driver's notification hook. When supplied, the important events
            are mirrored into the notebook pane so the user gets feedback in whichever pane they
            happen to be looking at (PLAN M3).
        demo: Marks the server as running without a model endpoint, so ``/api/models`` answers
            from a canned list instead of reaching for a key that is not there.
        version: Reported by ``/api/context``.

    Returns:
        A configured :class:`fastapi.FastAPI` application.
    """
    resolved_store = store or SessionStore(workspace.sessions_db_path)
    state = ServerState(
        workspace=workspace,
        store=resolved_store,
        agent=agent,
        demo=demo,
        version=version,
    )
    if notifier is not None:
        state.bus.add_observer(notebook_mirror(notifier))
    return _build(state)


def create_hub_app(
    *,
    store: SessionStore | None = None,
    user_directory: Path | None = None,
    version: str = "0.1.0",
) -> FastAPI:
    """Build the application with no workbook attached, landing on the hub.

    This is what ``kedge hub`` runs. The application is the same one ``create_app`` builds — same
    routes, same session store, same static assets — with :attr:`ServerState.workspace` empty
    until the user opens a workbook from the hub, at which point :meth:`ServerState.attach` fills
    it in and the browser moves to the chat view.

    Args:
        store: The session store. Defaults to ``~/.kedge/sessions.sqlite``, which is the same
            file every workspace uses; sessions are keyed by notebook path, not by server.
        user_directory: Overrides ``~/.kedge``, for tests.
        version: Reported by ``/api/context``.

    Returns:
        A configured :class:`fastapi.FastAPI` application with nothing open.
    """
    from kedge.config import user_dir

    directory = (user_directory or user_dir()).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    state = ServerState(
        workspace=None,
        store=store or SessionStore(directory / "sessions.sqlite"),
        agent=None,
        version=version,
        user_directory=directory,
    )
    return _build(state)


def _build(state: ServerState) -> FastAPI:
    """Assemble the FastAPI application around a prepared :class:`ServerState`."""

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if state.workspace is None:
            logger.info("kedge hub ready; no workbook open yet")
        else:
            logger.info("kedge server ready for %s", state.workspace.workbook_path)
        try:
            yield
        finally:
            state.store.close()
            logger.info("kedge server stopped")

    app = FastAPI(
        title="kedge",
        summary="Turn manual Excel processes into reviewable, reproducible marimo notebooks.",
        version=state.version,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.kedge = state
    app.include_router(hub_router)
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def create_demo_app(
    workbook: Path | str | None = None,
    *,
    delay: float = 0.05,
    store: SessionStore | None = None,
) -> FastAPI:
    """Build a server driven by the scripted agent, with no model and no marimo.

    The point of this is that the UI can be judged before either exists. Every event type in
    PLAN M3 is exercised, so the activity trail, the streamed markdown and the cancel path are
    all real; only the thing producing the events is fake.
    """
    resolved = Path(workbook) if workbook is not None else _demo_workbook()
    workspace = Workspace.for_workbook(resolved)
    workspace.ensure_dirs()
    demo_store = store or SessionStore(workspace.user_dir / "sessions-demo.sqlite")
    logger.warning(
        "demo mode: the scripted agent is answering, no model endpoint will be contacted"
    )
    return create_app(
        workspace,
        agent=ScriptedAgent(delay=delay),
        store=demo_store,
        demo=True,
    )


def _demo_workbook() -> Path:
    """Return a workbook path for the demo, preferring a committed fixture if one is present."""
    fixture = Path.cwd() / "tests" / "fixtures" / "clean_pipeline.xlsx"
    if fixture.is_file():
        return fixture
    return Path.cwd() / "demo_process.xlsx"


def require_loopback(host: str) -> str:
    """Return ``host`` if it is loopback, and raise otherwise."""
    if host not in LOOPBACK_HOSTS:
        msg = (
            f"kedge binds loopback only, and {host!r} is not loopback. The server has no accounts, "
            f"no cookies and no auth by design (PLAN 2.9); that is only defensible while nothing "
            f"off this machine can reach it. Use 127.0.0.1."
        )
        raise ServerError(msg)
    return host


def run_server(
    app: FastAPI,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    log_level: str = "warning",
) -> None:
    """Serve ``app`` on loopback with uvicorn, blocking until it stops."""
    import uvicorn

    require_loopback(host)
    logger.info("kedge is serving http://%s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level=log_level)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m kedge.server.app",
        description="Run the kedge server. Without a wired-up agent loop, use --demo.",
    )
    parser.add_argument("--workbook", type=Path, default=None, help="workbook to work against")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="use the scripted stand-in agent; contacts no model and needs no marimo",
    )
    parser.add_argument("--host", default="127.0.0.1", help="loopback only")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--delay", type=float, default=0.05, help="demo pacing, in seconds")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m kedge.server.app``."""
    args = _parse_args(argv)
    configure_logging(verbose=args.verbose)

    if not args.demo:
        msg = (
            "the agent loop (kedge.agent.loop, PLAN M4) is not wired up yet. Run with --demo to "
            "exercise the whole interface against the scripted stand-in agent."
        )
        raise AgentUnavailableError(msg)

    require_loopback(args.host)
    app = create_demo_app(args.workbook, delay=args.delay)
    logger.warning("kedge demo server on http://%s:%d", args.host, args.port)
    run_server(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
