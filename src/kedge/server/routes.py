"""The HTTP surface: context, health, models, sessions, and the turn stream.

There is no auth here and there should not be. The server binds loopback only, there is one user,
and PLAN 2.9 is emphatic that this is licence not to build things rather than a state to engineer
around — no accounts, no cookies, no CORS configuration, nothing "for later".

The turn endpoint is the interesting one. It is a POST that answers with ``text/event-stream``
rather than the more usual "POST to start, ``EventSource`` to watch", for three reasons: the
events belong to one turn and correlating two channels buys nothing; ``EventSource`` cannot POST,
so the message would have to travel in a query string; and a ``fetch`` body reader gives an
``AbortController``, which is how Escape cancels an in-flight turn from the browser.

While a turn streams, each event goes three places: down the wire as an SSE frame, onto the bus
for the notebook mirror and any attached monitor, and into an accumulator that becomes the stored
assistant message when the turn ends. The accumulator runs in a ``finally`` block, so a turn that
is cancelled or that dies mid-stream still leaves a coherent transcript.

Three of the tools here are decisions the model is not allowed to make on its own, and this module
is the user's half of them. ``delete_cell``, ``propose_plan`` and ``amend_plan`` do not act: they
record a request and tell the model plainly that nothing has happened (``kedge.agent.tools``).
``/api/pending`` surfaces those requests and ``/api/pending/...`` acts on them, so the recorded
intent has somewhere to go other than a log file. Confirming a deletion runs it through the same
notebook driver the agent would have used; approving a proposal or an amendment writes a plan
version through :mod:`kedge.plan.store` and then carries it into the notebook through that same
driver. None of them can be reached without an explicit request from the browser, which is the
whole of PLAN 2.2's gate.

That second half of an approval is what makes the pane a place to work rather than a place to
file decisions. Scaffolding used to belong to the open sequence alone, so a plan approved here
reached the notebook only when the user closed the workbook and opened it again — and until the
notebook had cells in it there was nothing to iterate on, nothing to ask for changes to, and no
notebook to run an ad-hoc question against. The plan and the cells implementing it now land on
one click; from there ``propose_cell`` and ``edit_cell`` are unlocked and the conversation
carries on against a live notebook.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from kedge.config import get_api_key
from kedge.errors import KedgeError
from kedge.lifecycle import health_check
from kedge.server.agent_seam import TurnMessage, TurnRequest
from kedge.server.events import DoneEvent, ErrorEvent, TokenEvent, encode_sse, sse_comment
from kedge.server.sessions import ChatSession, notebook_snapshot
from kedge.server.settings import fetch_model_names

if TYPE_CHECKING:
    from kedge.server.app import ServerState
    from kedge.workspace import Workspace

logger = logging.getLogger(__name__)

__all__ = ["SHELL_HEADERS", "STREAM_HEADERS", "router"]

router = APIRouter()

STREAM_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    # Belt and braces for anyone who later puts a reverse proxy in front of this.
    "X-Accel-Buffering": "no",
}

SHELL_HEADERS = {"Cache-Control": "no-store"}
"""What the shells and the state endpoints are served with: answers about this process, not files.

``/`` returns ``hub.html`` or ``index.html`` depending on whether a workbook is open, so the one
URL has two bodies and the browser has no way to tell which it is holding. Left uncached, Starlette
sends an ``etag`` and a ``last-modified`` and nothing else, and a browser is entitled to reuse a
response with no stated freshness for a fraction of its age without asking. Opening a workbook and
then following a link back to ``/`` then re-rendered the *hub* out of the cache, so "Go to the
notebook" led anywhere but the notebook.

``no-store`` rather than ``no-cache``: there is nothing here worth revalidating -- the document is
small, the server is on loopback, and the answer depends on state this process holds rather than on
a file's mtime.
"""

_KEEPALIVE_SECONDS = 15.0
_DEMO_MODELS = (
    "gpt-4o",
    "gpt-4o-mini",
    "claude-sonnet-4-5",
    "qwen2.5-coder:32b",
)


def get_state(request: Request) -> ServerState:
    """Return the server state hung off the application."""
    return request.app.state.kedge


def require_workspace(state: ServerState) -> Workspace:
    """Return the attached workspace, or answer 409 pointing the user at the hub.

    409 rather than 404 or 500: the request was well-formed and the route exists, but the server
    is in a state — hub mode, nothing opened yet — where it cannot be satisfied. The browser turns
    this into a redirect to the hub rather than an error banner.
    """
    from kedge.server.app import WorkspaceNotAttachedError

    try:
        return state.require_workspace()
    except WorkspaceNotAttachedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# ── request bodies ───────────────────────────────────────────────────────────────────────────


class NewSessionBody(BaseModel):
    """Body for creating a chat session."""

    title: str | None = None
    model: str | None = None


class PatchSessionBody(BaseModel):
    """Body for renaming a session or changing its model."""

    title: str | None = None
    model: str | None = None


class TurnBody(BaseModel):
    """Body for starting a turn."""

    message: str = Field(min_length=1)
    model: str | None = None


# ── the shell ────────────────────────────────────────────────────────────────────────────────


@router.get("/", include_in_schema=False)
def index(request: Request) -> FileResponse:
    """Serve the two-pane shell, or the hub when no workbook is open.

    The root is the front door either way. A server started with ``kedge open`` has a workbook and
    lands on the chat; one started with ``kedge hub`` has not and lands on the list, which is the
    difference between the two commands and the whole of it.

    Because *which* page comes back is a fact about this process rather than about a file, the
    answer is served :data:`SHELL_HEADERS` -- uncached, it is the same URL holding two different
    documents and the browser cannot know which of them it kept.
    """
    state = get_state(request)
    page = "index.html" if state.attached else "hub.html"
    return FileResponse(state.static_dir / page, headers=SHELL_HEADERS)


# ── context and health ───────────────────────────────────────────────────────────────────────


@router.get("/api/context")
def context(request: Request, response: Response) -> dict[str, Any]:
    """Everything the UI needs to draw itself once, on load.

    Includes the notebook iframe URL with the access token already in the query string. That is
    the whole of PLAN 1.3: an iframe that loads unauthenticated lands on marimo's login page,
    which is the one endpoint setting ``X-Frame-Options: DENY``, and the frame breaks. Passing
    ``access_token`` as a query parameter means the login page is never reached.

    Served :data:`SHELL_HEADERS` for the same reason the shells are: this is a report on what the
    process holds right now -- whether a workbook is attached, whether marimo has answered -- and
    all of it can change while a tab sits open. A stale copy of this is a pane that says there is
    no notebook when there is one.

    ``conversion`` says how far this workbook has actually got, so the pane can offer the next
    thing to do rather than the same four things for ever. See :func:`_conversion_state` for the
    states it names, what it costs, and why it can never fail.
    """
    response.headers.update(SHELL_HEADERS)
    state = get_state(request)
    workspace = state.workspace
    if workspace is None:
        # Not an error. The shell asks for context before it knows which mode it is in, and
        # ``attached: false`` is how it learns to send the user to the hub instead of drawing an
        # empty chat against a workbook that does not exist.
        return {
            "version": state.version,
            "demo": state.demo,
            "attached": False,
            "hub_url": "/hub",
        }
    session = workspace.marimo
    return {
        "version": state.version,
        "demo": state.demo,
        "attached": True,
        "hub_url": "/hub",
        "workbook": {"path": str(workspace.workbook_path), "name": workspace.workbook_path.name},
        "notebook": {"path": str(workspace.notebook_path), "name": workspace.notebook_path.name},
        "notebook_url": workspace.notebook_url() if session is not None else None,
        "model": {
            "configured": workspace.config.model.model,
            "base_url": workspace.config.model.base_url,
        },
        "marimo": {
            "attached": session is not None,
            "base_url": None if session is None else session.base_url,
            "session_id": None if session is None else session.session_id,
        },
        "conversion": _conversion_state(workspace),
    }


_NOTEBOOK_READ_CAP = 256 * 1024
"""How large a notebook this route will parse before it gives up and says nothing.

``ast.parse`` is superlinear: the 35KB the reference conversion produces costs about five
milliseconds, 338KB costs 129, and a 1.35MB file costs two seconds -- on a route the pane polls
every five seconds whenever the kernel is absent. Seven times the largest notebook kedge has ever
written is headroom; past it the answer is silence, which the pane already knows what to do with.
"""


def _conversion_state(workspace: Workspace) -> dict[str, Any]:
    """How far this workbook's conversion has got, as one state name and one count.

    The pane keys its suggestion list off ``state`` directly. That is deliberate: which situation
    a workbook is in is a fact about the workbook rather than a presentation choice, and deciding
    it here means the branch is exercised by this route's own tests instead of being copied into
    a JavaScript the test suite cannot execute. The pane owns the wording; this owns the state.

    **This must never raise and must never be slow.** It is on the pane's load path and, whenever
    marimo is absent, on a five-second poll -- so it is answered at moments the user did not ask
    for. Every branch degrades to a state the pane has a list for, because a suggestion is a
    convenience and nothing here is worth a 500. It is worth being concrete about what a 500 would
    cost: ``boot()`` in ``app.js`` catches a failed context, prints "The kedge server is not
    answering" and returns *before* it installs the health poll, so one exception here would leave
    the pane dead until somebody reloaded the page by hand.

    ``unwritten`` is only ever counted for an approved plan. In every other state the number is
    not used, and reading a 35KB file to answer a question nobody asked is not free.

    Args:
        workspace: The attached workspace.

    Returns:
        ``state`` -- one of ``none``, ``proposed``, ``revise``, ``rejected``, ``approved``,
        ``written`` or ``unknown`` -- and ``unwritten``, the number of cells still carrying the
        scaffolder's marker, or ``None`` where it is not known or was not asked.
    """
    state, plan = _plan_status(workspace)
    if plan is None:
        return {"state": state, "unwritten": None}
    unwritten = _unwritten_cells(workspace, plan)
    if unwritten == 0:
        return {"state": "written", "unwritten": 0}
    return {"state": state, "unwritten": unwritten}


def _plan_status(workspace: Workspace) -> tuple[str, Any]:
    """Where this workbook's plan history has got to, and the approved plan if there is one.

    Four review states collapse to four answers rather than two, because the right thing to
    suggest differs at each. A **rejected** plan is terminal -- :func:`kedge.plan.review.approve`
    refuses it outright and says to propose a new one -- so reporting it as merely "proposed"
    offers "talk me through the plan you have proposed" for ever, and takes the conversion
    kickoff off the screen at the one moment it is the only useful thing to say. **Changes
    requested** is not terminal and wants a revision rather than a fresh plan. A **draft** wants
    reviewing.

    ``approved`` is asked the way the scaffolder asks it -- walking back from the newest version,
    so a plan approved at v2 and superseded by an unapproved v3 still reads as approved. That is
    the state the notebook was built from, and the one work should be offered against.

    Only the approved version is loaded through pydantic; the rest are probed with
    :func:`_approval_state`, which reads one key out of the YAML. That matters because the worst
    case is a history where *nothing* is approved -- every version has to be looked at, and at
    roughly ten milliseconds each a ten-version history cost a tenth of a second on a five-second
    poll. Measured over ten drafts it is now around five milliseconds; a workbook with no plan at
    all, which is where the pane spends its first minutes, is a directory scan and nothing else.

    Args:
        workspace: The attached workspace.

    Returns:
        The state, and the approved :class:`~kedge.plan.model.ProcessPlan` where there is one.
        ``"unknown"`` is silence rather than a guess: offering "propose a plan" over a plan that
        exists but will not parse sends the user round a loop that cannot end.
    """
    from kedge.plan.model import ApprovalState
    from kedge.plan.store import PlanStore

    seen: list[str] = []
    try:
        store = PlanStore.for_workspace(workspace)
        versions = store.versions()
        if not versions:
            return "none", None
        for version in reversed(versions):
            recorded = _approval_state(store.path_for(version))
            if recorded is None:
                return "unknown", None
            if recorded == ApprovalState.APPROVED:
                return "approved", store.load(version)
            seen.append(recorded)
    except (KedgeError, OSError, ValueError, RecursionError) as exc:
        logger.debug("could not read the plan history for the pane: %s", exc)
        return "unknown", None

    latest = seen[0]
    if latest == ApprovalState.REJECTED:
        return "rejected", None
    if latest == ApprovalState.CHANGES_REQUESTED:
        return "revise", None
    return "proposed", None


def _approval_state(path: Path) -> str | None:
    """The ``approval.state`` recorded in one plan file, without building the model.

    A :class:`~kedge.plan.model.ProcessPlan` is a large pydantic model and validating one costs
    about ten milliseconds; this question is one string. The default is reproduced rather than
    inferred: :class:`~kedge.plan.model.Approval` defaults to ``draft``, so a file recording no
    approval block at all is a draft, not an unreadable file.

    Args:
        path: The plan file.

    Returns:
        The state as written, or ``None`` when the file will not load or is not a mapping -- which
        the caller reports as "not known" rather than guessing at a default.
    """
    import yaml

    from kedge.plan.model import ApprovalState

    # ``CSafeLoader`` constructs exactly what ``SafeLoader`` does and is eight times faster on a
    # 2KB plan -- 0.43ms against 3.6. It is absent from a PyYAML built without libyaml, which is
    # the only reason for the fallback.
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    try:
        raw = yaml.load(path.read_text(encoding="utf-8"), Loader=loader)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        logger.debug("could not read the approval state from %s: %s", path, exc)
        return None
    if not isinstance(raw, dict):
        return None
    approval = raw.get("approval")
    if approval is None:
        return str(ApprovalState.DRAFT)
    if not isinstance(approval, dict):
        return None
    recorded = approval.get("state", str(ApprovalState.DRAFT))
    return recorded if isinstance(recorded, str) else None


def _unwritten_cells(workspace: Workspace, plan: Any) -> int | None:
    """How many cells in the notebook are still the scaffolder's passthroughs.

    **Counted off the file on disk, never off the kernel, and that is the whole design.** The
    obvious source is ``driver.list_cells(with_code=True)``, which is what
    :func:`kedge.server.hub._unwritten_count` uses -- but reading a cell's code records a read
    against it for marimo's staleness guard, and that guard is the only thing standing between a
    user's own edit and an ``edit_cell`` that silently overwrites it. hub.py can afford it because
    it runs once, immediately after ``sync_notebook`` has just read every cell anyway, so it
    disarms nothing that was armed when it started. This route has no such cover: it is answered
    on every page load and again whenever the health poll finds the frame missing, at moments the
    user did not ask for and may well be typing a cell through. A convenience feature must not
    re-arm the guard behind them.

    **The file lies in one direction, so a zero has to be corroborated.** The file is not a
    mirror of the kernel: ``--watch`` makes marimo *re-read* the notebook, and what writes it is
    a best-effort autosave whose failures the driver never sees -- so the file is not "a save or
    two behind" so much as arbitrarily stale. Worse, that autosave truncates and rewrites in
    place, with no temp file and no rename, which is the hazard ``codegen.write_atomically``
    exists to avoid. A read taken mid-write returns a prefix of the notebook; a prefix parses
    perfectly and is indistinguishable from a shorter notebook, and every cell it lost is a cell
    that cannot be counted. The error is therefore always an *undercount*, and an undercount of
    zero tells the user the conversion is finished. Sampled over a real 36KB scaffold with six
    holes, one truncation point in eight reports zero. Nor does it need a race: a plan whose
    scaffolding step failed leaves an empty notebook, which parses cleanly to no cells at all.

    Two checks, both cheap, and between them a zero is worth saying. Every stage in the approved
    plan must have its cell in the file -- which a prefix cannot satisfy, because the cells it
    lost are the last ones, and which an empty or unscaffolded notebook cannot satisfy either.
    And a count of zero is read a second time and must come back identical, which closes the
    window on a write still in flight. The second read is paid for only in the case that matters.

    A stage whose cell name the scaffolder had to disambiguate -- two stage ids cleaning to the
    same identifier -- will not be found by this, and the answer degrades to ``None``. That is the
    correct direction to be wrong in.

    Note this counts every hole in the *notebook*, where ``hub._unwritten_count`` counts only
    those the current plan still calls for. On a plan that has since dropped a stage the two
    disagree, and the file's answer is the honest one for a pane: the marker is in the notebook
    the user is looking at.

    Args:
        workspace: The attached workspace.
        plan: The approved plan, whose stages are what the file is corroborated against.

    Returns:
        The number of cells still carrying the marker, or ``None`` -- absent, unreadable, larger
        than :data:`_NOTEBOOK_READ_CAP`, not a marimo file yet, or a zero that could not be
        corroborated. ``hub._unwritten_count`` puts it best: on a question whose whole job is to
        say the conversion is unfinished, a wrong number is worse than no number.
    """
    from kedge.notebook.scaffold import cell_name_for, is_unwritten

    path = workspace.notebook_path
    try:
        if path.stat().st_size > _NOTEBOOK_READ_CAP:
            logger.debug("the notebook at %s is past the read cap; not counting its holes", path)
            return None
    except OSError:
        return None

    document = _read_notebook_quietly(path)
    if document is None:
        return None
    present = {cell.name for cell in document.cells}
    wanted = {cell_name_for(stage.id) for stage in plan.stages}
    if not present or not wanted <= present:
        logger.debug("the notebook at %s does not hold every stage the plan names", path)
        return None

    count = sum(1 for cell in document.cells if is_unwritten(cell.code))
    if count:
        return count
    again = _read_notebook_quietly(path)
    if again is None or again.cells != document.cells:
        return None
    return 0


def _read_notebook_quietly(path: Path) -> Any:
    """Parse the notebook at ``path``, or return ``None`` for any reason it could not be.

    The exception list is wider than it looks like it needs to be, and every addition is a route
    that reached the user as a 500. ``read_notebook`` catches ``OSError`` around its own read and
    turns a ``SyntaxError`` into a ``NotebookFormatError``, but a file truncated part-way through
    a multi-byte character raises ``UnicodeDecodeError`` -- a ``ValueError``, past both -- and
    non-ASCII in a converted notebook is routine, since the briefing cell renders the workbook's
    own prose and model-written markdown carries pound signs and em dashes. ``ast.parse`` adds
    ``RecursionError`` on a deeply nested expression, and its own ``ValueError`` on a null byte.
    None of those is an ``OSError`` and none of them is a ``KedgeError``.

    Better still would be for ``codegen.read_notebook`` to raise ``NotebookFormatError`` for a
    bad encoding the way it already does for bad syntax; that module is not this change's to edit.
    """
    from kedge.notebook.codegen import read_notebook

    try:
        return read_notebook(path)
    except (KedgeError, OSError, ValueError, RecursionError) as exc:
        logger.debug("could not read the notebook at %s: %s", path, exc)
        return None


@router.get("/api/health")
async def health(request: Request, response: Response) -> dict[str, Any]:
    """Report on the marimo kernel, which is a separate process and can die under us.

    Liveness is decided by asking the server, never by inspecting a PID: PID checks are
    unreliable on Windows and PIDs are recycled (PLAN 6.2).

    Uncached for the same reason as :func:`context`: a poll answered from a cache is not a poll.
    """
    response.headers.update(SHELL_HEADERS)
    state = get_state(request)
    session = None if state.workspace is None else state.workspace.marimo
    if session is None:
        detail = (
            "No workbook is open. Choose one from the hub."
            if state.workspace is None
            else "No marimo server is attached."
        )
        return {
            "server": "ok",
            "marimo": {"state": "absent", "detail": detail},
            "active_turns": list(state.turns.active),
        }
    alive = await run_in_threadpool(health_check, session.base_url)
    return {
        "server": "ok",
        "marimo": {
            "state": "running" if alive else "unreachable",
            "base_url": session.base_url,
            "session_id": session.session_id,
            "detail": ""
            if alive
            else f"Nothing answered {session.base_url}/health. The notebook pane will be stale.",
        },
        "active_turns": list(state.turns.active),
    }


@router.get("/api/models")
async def models(request: Request) -> dict[str, Any]:
    """List the models the configured endpoint offers, degrading to a manual override.

    PLAN M6 asks for ``/v1/models`` "where supported, manual override where not". Plenty of
    OpenAI-compatible servers do not implement it and plenty of users will not have stored a key
    yet, so every failure here is reported as a source rather than as an error, and the UI keeps
    its free-text entry either way.
    """
    state = get_state(request)
    workspace = require_workspace(state)
    configured = workspace.config.model.model
    base_url = workspace.config.model.base_url

    if state.demo:
        return {
            "models": list(_DEMO_MODELS),
            "selected": configured,
            "source": "demo",
            "detail": "Demo mode: this list is canned and no endpoint was contacted.",
        }

    try:
        key = await run_in_threadpool(get_api_key, workspace.config)
    except KedgeError as exc:
        return {
            "models": [configured],
            "selected": configured,
            "source": "configured",
            "detail": str(exc),
        }

    try:
        names = await fetch_model_names(base_url, key, ca_bundle=workspace.config.model.ca_bundle)
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("could not list models from %s: %s", base_url, exc)
        return {
            "models": [configured],
            "selected": configured,
            "source": "unavailable",
            "detail": f"{base_url}/models did not answer usefully ({exc}). Type a model name.",
        }

    if not names:
        return {
            "models": [configured],
            "selected": configured,
            "source": "unavailable",
            "detail": f"{base_url}/models returned no models. Type a model name.",
        }
    return {"models": names, "selected": configured, "source": "endpoint", "detail": ""}


# ── sessions ─────────────────────────────────────────────────────────────────────────────────


@router.get("/api/sessions")
def list_sessions(request: Request) -> dict[str, Any]:
    """List this notebook's chat sessions, newest first."""
    state = get_state(request)
    workspace = require_workspace(state)
    sessions = state.store.list_sessions(notebook_path=str(workspace.notebook_path))
    return {"sessions": [session.to_dict() for session in sessions]}


@router.post("/api/sessions", status_code=201)
def create_session(body: NewSessionBody, request: Request) -> dict[str, Any]:
    """Start a new chat, snapshotting the notebook as it stands."""
    state = get_state(request)
    workspace = require_workspace(state)
    session = state.store.create_session(
        workbook_path=str(workspace.workbook_path),
        notebook_path=str(workspace.notebook_path),
        title=body.title,
        model=body.model or workspace.config.model.model,
        snapshot=notebook_snapshot(workspace.notebook_path),
    )
    return {"session": session.to_dict()}


@router.get("/api/sessions/{session_id}")
def read_session(session_id: str, request: Request) -> dict[str, Any]:
    """Return a session with its messages, and say whether the notebook has moved since.

    A session is a conversation about a notebook at a particular state. The user edits that
    notebook directly, so resuming without checking would be a fiction — hence the snapshot
    comparison rather than a silent carry-on.
    """
    state = get_state(request)
    session = _require_session(state, session_id)
    current = notebook_snapshot(Path(session.notebook_path))
    return {
        "session": session.to_dict(),
        "messages": [message.to_dict() for message in state.store.messages(session_id)],
        "snapshot_current": current,
        "drifted": session.snapshot is not None and session.snapshot != current,
    }


@router.patch("/api/sessions/{session_id}")
def patch_session(session_id: str, body: PatchSessionBody, request: Request) -> dict[str, Any]:
    """Rename a session or change the model it uses."""
    state = get_state(request)
    _require_session(state, session_id)
    session = state.store.update_session(session_id, title=body.title, model=body.model)
    if session is None:  # pragma: no cover - the row was there a line ago
        raise HTTPException(status_code=404, detail=f"No chat session with id {session_id!r}.")
    return {"session": session.to_dict()}


@router.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str, request: Request) -> dict[str, Any]:
    """Delete a session, its messages, and everything the agent still holds about it.

    The rows were only ever half of it. The agent keeps a per-session digest, the tool traffic of
    the last turn and the session's :class:`~kedge.agent.tools.ToolRegistry` — whose result cache
    holds sampled workbook rows — and that state now survives essentially every turn rather than
    only a paused one, for the lifetime of the process. Deleting the conversation and leaving it
    resident would mean a user who removed a conversation still had its sampled data in memory,
    and ``SECURITY.md`` is explicit that the sensitivity of the workbook is the sensitivity of the
    conversation.

    The registry is closed rather than only dropped. Dropping it leaves an open workbook handle —
    a zip archive and two openpyxl views of the user's file — to be collected whenever the
    interpreter gets round to it; ``aclose`` releases it now. It is best-effort: a handle that
    will not close is a warning, never a reason a conversation cannot be deleted.

    The delete succeeds whether or not an agent is attached. A session can be deleted from the hub
    with no workbook open and no model configured, and answering 409 there would be refusing to
    delete a conversation because there is nobody to tell.
    """
    state = get_state(request)
    if not await run_in_threadpool(state.store.delete_session, session_id):
        raise HTTPException(status_code=404, detail=f"No chat session with id {session_id!r}.")

    registry = _registry_for(state, session_id)
    aclose = getattr(registry, "aclose", None)
    if callable(aclose):
        try:
            await aclose()
        except Exception:  # pragma: no cover - a stuck handle must not block the delete
            logger.warning("could not close the tool registry for session %s", session_id)

    forget = getattr(state.agent, "reset_session", None)
    if callable(forget):
        forget(session_id)
        logger.info("cleared the agent's held state for deleted session %s", session_id)
    else:
        logger.debug("no agent state to clear for deleted session %s", session_id)
    return {"deleted": session_id}


def _require_session(state: ServerState, session_id: str) -> ChatSession:
    session = state.store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"No chat session with id {session_id!r}.")
    return session


def _require_agent(state: ServerState) -> Any:
    from kedge.server.app import WorkspaceNotAttachedError

    try:
        return state.require_agent()
    except WorkspaceNotAttachedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# ── turns ────────────────────────────────────────────────────────────────────────────────────


@router.post("/api/sessions/{session_id}/turns")
async def create_turn(session_id: str, body: TurnBody, request: Request) -> StreamingResponse:
    """Run one turn, streaming its events as they happen."""
    state = get_state(request)
    session = _require_session(state, session_id)
    agent = _require_agent(state)

    turn_id = uuid.uuid4().hex[:12]
    history = tuple(
        TurnMessage(role=message.role, content=message.content)
        for message in state.store.messages(session_id)
        if message.role in ("user", "assistant") and message.content
    )
    state.store.append_message(session_id, role="user", content=body.message, turn_id=turn_id)

    turn_request = TurnRequest(
        turn_id=turn_id,
        session_id=session_id,
        message=body.message,
        model=body.model or session.model,
        history=history,
    )
    token = state.turns.start(turn_id)
    logger.info("turn %s started in session %s", turn_id, session_id)

    async def stream() -> AsyncIterator[str]:
        prose: list[str] = []
        trail: list[dict[str, Any]] = []
        tokens_used = 0
        saw_done = False
        try:
            yield sse_comment("kedge turn " + turn_id)
            async for event in agent.run(turn_request, cancel=token):
                await state.bus.publish(event)
                if isinstance(event, TokenEvent):
                    prose.append(event.text)
                    # Prose is coalesced into the trail as well as into the message body, so a
                    # reopened session replays through exactly the same renderer as a live turn
                    # and keeps the interleaving of reasoning and activity. Storing it twice is
                    # a few kilobytes; losing the ordering would cost the reviewability that is
                    # half the point of the trail.
                    if trail and trail[-1].get("type") == "token":
                        trail[-1]["text"] += event.text
                    else:
                        trail.append(event.model_dump(mode="json"))
                else:
                    trail.append(event.model_dump(mode="json"))
                if isinstance(event, DoneEvent):
                    tokens_used = event.tokens_used
                    saw_done = True
                yield encode_sse(event)
            if not saw_done:
                # A loop that forgot to finish must not leave the UI spinning for ever.
                final = DoneEvent(turn_id=turn_id, tokens_used=tokens_used)
                trail.append(final.model_dump(mode="json"))
                yield encode_sse(final)
        except asyncio.CancelledError:
            logger.info("turn %s was interrupted", turn_id)
            raise
        except Exception as exc:
            logger.exception("turn %s failed", turn_id)
            failure = ErrorEvent(message=f"The turn failed: {exc}", recoverable=True)
            await state.bus.publish(failure)
            trail.append(failure.model_dump(mode="json"))
            yield encode_sse(failure)
            final = DoneEvent(turn_id=turn_id, tokens_used=tokens_used)
            trail.append(final.model_dump(mode="json"))
            yield encode_sse(final)
        finally:
            state.turns.finish(turn_id)
            state.store.append_message(
                session_id,
                role="assistant",
                content="".join(prose),
                turn_id=turn_id,
                events=trail,
                tokens_used=tokens_used,
            )
            logger.info("turn %s finished (%d events)", turn_id, len(trail))

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={**STREAM_HEADERS, "X-Kedge-Turn-Id": turn_id},
    )


@router.post("/api/turns/{turn_id}/cancel")
def cancel_turn(turn_id: str, request: Request) -> dict[str, Any]:
    """Ask an in-flight turn to stop.

    Cooperative by necessity: there is no way to interrupt a blocking call from outside it, so
    this sets a flag the loop is expected to check between steps.
    """
    state = get_state(request)
    if not state.turns.cancel(turn_id):
        raise HTTPException(status_code=404, detail=f"Turn {turn_id!r} is not in flight.")
    return {"cancelled": turn_id}


@router.get("/api/events")
async def monitor(request: Request) -> StreamingResponse:
    """A read-only view of every event on the bus, for a second pane or a second tab."""
    state = get_state(request)

    async def stream() -> AsyncIterator[str]:
        async with state.bus.subscribe() as events:
            yield sse_comment("kedge monitor")
            iterator = events.__aiter__()
            pending: asyncio.Task[Any] | None = None
            try:
                while True:
                    # The pending pull is kept across keep-alives rather than cancelled and
                    # remade: cancelling a generator's __anext__ mid-flight would leave it in an
                    # undefined state and could swallow the very event it was waiting for.
                    if pending is None:
                        pending = asyncio.ensure_future(iterator.__anext__())
                    done, _ = await asyncio.wait({pending}, timeout=_KEEPALIVE_SECONDS)
                    if not done:
                        yield sse_comment("keep-alive")
                        continue
                    event = await pending
                    pending = None
                    yield encode_sse(event)
            finally:
                if pending is not None:
                    pending.cancel()

    return StreamingResponse(stream(), media_type="text/event-stream", headers=STREAM_HEADERS)


# ── decisions the model is not allowed to make ───────────────────────────────────────────────
#
# `delete_cell`, `propose_plan` and `amend_plan` all record and refuse: the tool appends to the
# registry's pending list, tells the model plainly that nothing has happened, and returns. Until
# these routes existed there was no way for the user to say yes, which made them write-only.
#
# The two plan routes divide the ground `propose_plan` and `amend_plan` divide in the tool layer.
# Approving a proposal writes the plan the model authored, whole, and only where no plan has been
# approved yet; approving an amendment writes one recorded change against the plan that is in
# force. Neither can become the other, which is what keeps a replacement decomposition from
# arriving as though it were an edit.
#
# The registries hang off the agent loop, keyed by chat session (`KedgeAgent.registries`). The
# server reads that attribute and requires nothing else of the loop, so the scripted stand-in —
# which has no registries at all — degrades to an empty list rather than a 500.
#
# The drop routes are the third kind and the odd one out: they decide something *within* a pending
# proposal rather than deciding the proposal. An unacknowledged drop is the only structural thing
# that blocks approval (`ProcessPlan.approval_blockers`), so the card correctly degraded its button
# to "Save as draft" — and then offered no way to clear what it had just raised. The pane raised a
# gate and handed the user no key to it; the only remedy was `kedge plan acknowledge` at a
# terminal. These routes are that remedy, taken before the plan is written rather than against a
# draft on disk afterwards, so the review the card asks for can actually be completed there.


class DecisionBody(BaseModel):
    """Body for confirming or approving a pending decision.

    Every field is optional and so is the body itself: see :data:`NO_NOTE`. Frozen, because that
    default is one shared instance.
    """

    model_config = ConfigDict(frozen=True)

    note: str | None = None


NO_NOTE = DecisionBody()
"""The default body, for a client that sends none.

Declaring the body required cost nothing in the tests -- every one of them posts ``json={}`` --
and made every decision button in the pane a 422: the browser announced ``Content-Type:
application/json`` with nothing behind it, so FastAPI rejected the click before the handler ran,
and its *list* of validation errors reached the pane as ``[object Object]``. ``app.js`` no longer
sends that header without a body; this default is the other half, so that a client which gets it
wrong again loses an optional note rather than the decision.
"""


def _registry_for(state: ServerState, session_id: str) -> Any:
    """Return the tool registry for one chat session, or ``None`` if there is not one.

    ``None`` is normal: demo mode has no registries, and a session that has never made a tool
    call has no registry until its first turn.
    """
    registries = getattr(state.agent, "registries", None)
    if not isinstance(registries, dict):
        return None
    return registries.get(session_id)


def _proposal_payload(
    index: int,
    plan: Any,
    *,
    analysis: Any = None,
    triage_result: Any = None,
    analysis_stale: bool | None = None,
) -> dict[str, Any]:
    """Render a proposed plan for review in the chat pane.

    Enough of it to *decide* on, which is more than an amendment needs: an amendment is one
    sentence and its rationale, where a proposal is the whole decomposition and approving it
    unblocks the notebook.

    The bar is :func:`kedge.plan.review.render_plan` — what the CLI puts in front of a reviewer
    before the same decision — and anything the card omits is something the user is being asked to
    approve unseen. So a stage carries its assumptions ("what a reviewer checks first", per the
    field's own docstring), its dependencies, the analysis operations it claims and the pattern it
    translates; a checkpoint carries the *question* it will ask, which is the whole content of the
    control rather than a note that one exists.

    Three things here are not on the plan at all and have to be computed:

    - :func:`~kedge.plan.review.review_warnings`, which carries the only automatic check that the
      decomposition covers the workbook — operations claimed by no stage, and operation ids that
      are not in the analysis. Without an ``analysis`` those two cannot run and the card says so
      rather than showing a shorter list as though it were complete.
    - :meth:`~kedge.plan.model.ProcessPlan.approval_blockers`, pre-flighted so the button can say
      what clicking it will actually do. A plan with an unacknowledged drop lands as a *draft*,
      and learning that afterwards is learning it too late.
    - the triage **verdict**. Both proposal paths now refuse a ``STOP`` outright, so a plan
      reaching this card has already cleared that bar; showing the verdict is the second line of
      defence rather than the only one, and ``proceed_with_care`` is the case it earns its place
      on.

    Verification blockers are kept apart from conversion blockers rather than folded into one
    list. "1.00 convertible" with "cannot be reconciled" as a trailing clause inverts the emphasis
    of the one rule that matters most (non-negotiable 6): a plan with no baseline is not a plan
    that passed.
    """
    conversion_blockers = list(plan.assessment.blockers)
    verification_blockers: list[str] = []
    if triage_result is not None:
        verification_blockers = [
            blocker.render() for blocker in triage_result.verification_blockers
        ]
        # `Assessment.blockers` is `TriageResult.blocker_lines()`, which is conversion blockers
        # followed by verification ones. Splitting them by the same rendering they were built
        # from keeps the card from stating a verification blocker twice, once de-emphasised.
        conversion_blockers = [
            blocker for blocker in conversion_blockers if blocker not in verification_blockers
        ]

    return {
        "index": index,
        "version": plan.version,
        "based_on_version": plan.based_on_version,
        "summary": plan.summary,
        "convertible": plan.assessment.convertible,
        "blockers": conversion_blockers,
        "verification_blockers": verification_blockers,
        "verdict": triage_result.verdict.value if triage_result is not None else None,
        "complexity": triage_result.complexity if triage_result is not None else None,
        "reconcilable": triage_result.reconcilable if triage_result is not None else None,
        "stages": [
            {
                "id": stage.id,
                "kind": stage.kind.value,
                "intent": stage.intent,
                "confidence": stage.confidence.value,
                # Rendered rather than structured: the card is a reading surface, and `app.js`
                # joins these into a line. `range Calc!H2:H500` says more than the bare range did.
                "sources": [source.render() for source in stage.sources],
                "depends_on": list(stage.depends_on),
                "assumptions": list(stage.assumptions),
                "operations": list(stage.operations),
                "excel_pattern": stage.excel_pattern.value if stage.excel_pattern else None,
                "notes": stage.notes,
                "checkpoint": _checkpoint_payload(stage),
            }
            for stage in plan.ordered_stages()
        ],
        "open_questions": [
            {"question": question.question, "context": question.context}
            for question in plan.open_questions
        ],
        # The decision travels with the drop, not just the proposal. The card draws a confirm/keep
        # control per outstanding drop and the recorded outcome for a decided one, and without
        # `acknowledged` it cannot tell those apart — so a drop the user had already signed off
        # would keep asking, and a refusal would be indistinguishable from a confirmation.
        "dropped": [
            {
                "range": drop.range,
                "reason": drop.reason,
                "acknowledged": drop.acknowledged,
                "accepted": drop.accepted,
                "note": drop.note,
            }
            for drop in plan.dropped
        ],
        "warnings": _review_warnings(plan, analysis, triage_result),
        "warnings_complete": analysis is not None,
        "approval_blockers": plan.approval_blockers(),
        "unacknowledged_drops": len(plan.unacknowledged_drops),
        "analysis_stale": analysis_stale,
    }


def _checkpoint_payload(stage: Any) -> dict[str, Any] | None:
    """The question a checkpoint stage will ask, or ``None`` for an automated stage.

    ``render_plan`` prints ``asks: Have this month's overrides been agreed with Risk?`` where the
    card said ", not automated". The question *is* the control; a card that omits it is asking the
    user to approve a decision point without showing them the decision.
    """
    if not stage.is_checkpoint:
        return None
    checkpoint = stage.effective_checkpoint()
    return {
        "question": checkpoint.question,
        "options": list(checkpoint.options),
        "guidance": checkpoint.guidance,
        "require_note": checkpoint.require_note,
    }


def _review_warnings(plan: Any, analysis: Any, triage_result: Any) -> list[str]:
    """Run the review warnings, degrading to an empty list rather than taking the panel down."""
    from kedge.plan.review import review_warnings

    try:
        return review_warnings(plan, analysis, triage_result=triage_result)
    except Exception:  # pragma: no cover - a warning that raises must not hide the plan
        logger.exception("could not compute review warnings for plan v%s", plan.version)
        return []


def _triage_for(analysis: Any) -> Any:
    """Score the analysis the proposal was written against, or ``None`` when there is none.

    Recomputed at render time rather than carried on the pending proposal: triage is
    deterministic and offline, so the answer is the same one the tool got, and the alternative is
    a field on a dataclass this module does not own.
    """
    if analysis is None:
        return None
    from kedge.plan.triage import triage

    return triage(analysis)


def _analysis_is_stale(workspace: Any) -> bool | None:
    """Whether the workbook has been saved since the analysis the plan was scored from was written.

    A plan records a triage score stamped "Scored deterministically from the analysis, not
    estimated." If the user has saved the workbook from Excel since, that is a reading of a file
    that no longer exists, and the figure on the card is about a different workbook. Cheap to
    detect and worth saying out loud.

    Returns ``None`` — "cannot tell" — when there is no workspace or either file is missing, which
    is not the same answer as "current" and must not be rendered as one.
    """
    if workspace is None:
        return None
    try:
        workbook = Path(workspace.workbook_path)
        analysis_file = Path(workspace.analysis_path)
        if not workbook.is_file() or not analysis_file.is_file():
            return None
        return workbook.stat().st_mtime_ns > analysis_file.stat().st_mtime_ns
    except (OSError, AttributeError):
        return None


def _pending_payload(registry: Any) -> dict[str, Any]:
    if registry is None:
        return {"proposals": [], "deletions": [], "amendments": []}
    context = getattr(registry, "context", None)
    analysis = getattr(context, "analysis", None) if registry.pending_proposals else None
    triage_result = _triage_for(analysis)
    stale = _analysis_is_stale(getattr(context, "workspace", None))
    return {
        "proposals": [
            _proposal_payload(
                index,
                item.plan,
                analysis=analysis,
                triage_result=triage_result,
                analysis_stale=stale,
            )
            for index, item in enumerate(registry.pending_proposals)
        ],
        "deletions": [
            {
                "index": index,
                "cell": item.cell,
                "reason": item.reason,
                "descendants": list(item.descendants),
            }
            for index, item in enumerate(registry.pending_deletions)
        ],
        "amendments": [
            {
                "index": index,
                "rationale": item.rationale,
                "change": item.change,
                "stage": item.stage,
            }
            for index, item in enumerate(registry.pending_amendments)
        ],
    }


@router.get("/api/sessions/{session_id}/pending")
def pending_decisions(session_id: str, request: Request) -> dict[str, Any]:
    """List the deletions and plan amendments this session is waiting on the user for.

    Deletions carry the names of the cells that read what the doomed cell defines, because
    "deleting this breaks three downstream cells" is the only part of the decision the user cannot
    work out for themselves from the chat.
    """
    state = get_state(request)
    _require_session(state, session_id)
    return _pending_payload(_registry_for(state, session_id))


@router.post("/api/sessions/{session_id}/pending/deletions/{index}")
async def confirm_deletion(
    session_id: str,
    index: int,
    request: Request,
    body: DecisionBody = NO_NOTE,
) -> dict[str, Any]:
    """Carry out a deletion the model asked for and the user has now confirmed.

    Runs through the same notebook driver the agent would have used, so the kernel stays the
    single source of truth about what the notebook contains. The pending entry is removed whether
    the deletion succeeded or failed — a request that has been decided is no longer pending, and
    leaving a failed one in the list would invite a second click at a cell that is already gone.
    """
    del body
    state = get_state(request)
    _require_session(state, session_id)
    registry = _registry_for(state, session_id)
    pending = _pop_pending(registry, "pending_deletions", index)

    driver = getattr(registry.context, "driver", None)
    if driver is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"No notebook kernel is attached, so cell {pending.cell!r} cannot be deleted. The "
                f"request has been cleared; ask again once the notebook is up."
            ),
        )
    try:
        mutation = await driver.delete_cell(pending.cell)
    except KedgeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    logger.info("user confirmed deletion of cell %s in session %s", pending.cell, session_id)
    return {
        "deleted": pending.cell,
        "ok": getattr(mutation, "ok", True),
        "detail": getattr(mutation, "error", None),
        "pending": _pending_payload(registry),
    }


@router.delete("/api/sessions/{session_id}/pending/deletions/{index}")
def dismiss_deletion(session_id: str, index: int, request: Request) -> dict[str, Any]:
    """Decline a deletion. The cell is untouched and the request is dropped."""
    state = get_state(request)
    _require_session(state, session_id)
    registry = _registry_for(state, session_id)
    pending = _pop_pending(registry, "pending_deletions", index)
    logger.info("user declined deletion of cell %s in session %s", pending.cell, session_id)
    return {"dismissed": pending.cell, "pending": _pending_payload(registry)}


@router.post("/api/sessions/{session_id}/pending/amendments/{index}")
async def approve_amendment(
    session_id: str,
    index: int,
    request: Request,
    body: DecisionBody = NO_NOTE,
) -> dict[str, Any]:
    """Write an approved plan amendment as a new plan version, and carry it into the notebook.

    The gate is not weakened by this, it is completed. The model's ``amend_plan`` call recorded a
    proposal and said so; this is the user reading it and deciding. What is written is a new
    version derived from the current plan with the amendment attached to the stage it names (or to
    the plan's summary where it names none), saved through
    :class:`~kedge.plan.store.PlanStore` so the history is retained and the change is a diff.

    Approval is recorded on that new version only when the plan is approvable on its own terms.
    An amendment to a plan with an unacknowledged dropped range lands as a **draft**, with the
    blockers reported back, because ``approve`` refusing is the gate working rather than an error
    to route around.

    The plan the notebook is synced against is the one that was in force a moment ago, which is
    what lets an amendment reach the stage cell it names: the amended note is a comment in a body
    the sync can prove kedge wrote. A stage already translated has diverged from that body and is
    reported rather than overwritten -- see :func:`_carry_the_plan_into_the_notebook`.
    """
    state = get_state(request)
    _require_session(state, session_id)
    registry = _registry_for(state, session_id)
    pending = _pop_pending(registry, "pending_amendments", index)

    store = getattr(registry.context, "plans", None)
    if store is None:
        raise HTTPException(
            status_code=409,
            detail="This session has no plan store, so there is no plan to amend.",
        )
    try:
        written = await run_in_threadpool(_write_amendment, store, pending, note=body.note)
    except KedgeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    logger.info("user approved a plan amendment in session %s", session_id)
    notebook = await _carry_the_plan_into_the_notebook(state, registry, written)
    return {**written.payload, "notebook": notebook, "pending": _pending_payload(registry)}


@router.post("/api/sessions/{session_id}/pending/proposals/{index}")
async def approve_proposal(
    session_id: str,
    index: int,
    request: Request,
    body: DecisionBody = NO_NOTE,
) -> dict[str, Any]:
    """Write a proposed process plan as a new plan version, and scaffold the notebook from it.

    The counterpart of :func:`approve_amendment`, and the same gate: ``propose_plan`` authored a
    plan and said plainly that it was not in force; this is the user reading it and deciding. What
    is written is the decomposition exactly as the model authored it — ``generated_by: llm`` with
    the model id — carrying whatever drop decisions the reviewer took on the card, which are
    recorded on the drops themselves with the reviewer's note. Those decisions leave the plan's
    identity alone (:func:`_restore_proposal_identity`), so the one version this writes still says
    who wrote the decomposition. Saved through :class:`~kedge.plan.store.PlanStore` at the next
    free version so an earlier draft is superseded rather than overwritten.

    Approval is recorded on it only when the plan is approvable on its own terms. A plan whose
    dropped ranges nobody has acknowledged lands as a **draft** with the blockers reported, for the
    same reason an amendment does: ``approve`` refusing is the gate working.

    **The notebook is written here, not at the next open.** Scaffolding used to happen only inside
    the open sequence (``hub._step_scaffold``), so approving a plan in this pane wrote a file to
    disk and left the notebook beside it empty until the user closed the workbook and opened it
    again. The decomposition and the cells implementing it now land on one click, which is also
    what makes the rest of the conversation possible: ``propose_cell`` and ``edit_cell`` unlock on
    exactly this approval, so the user can ask for changes to a notebook that is actually there.
    """
    state = get_state(request)
    _require_session(state, session_id)
    registry = _registry_for(state, session_id)
    pending = _pop_pending(registry, "pending_proposals", index)

    store = getattr(registry.context, "plans", None)
    if store is None:
        raise HTTPException(
            status_code=409,
            detail="This session has no plan store, so there is nowhere to write the plan.",
        )
    try:
        written = await run_in_threadpool(_write_proposal, store, pending, note=body.note)
    except KedgeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    logger.info("user approved a proposed plan in session %s", session_id)
    notebook = await _carry_the_plan_into_the_notebook(state, registry, written)
    return {**written.payload, "notebook": notebook, "pending": _pending_payload(registry)}


async def _carry_the_plan_into_the_notebook(
    state: ServerState, registry: Any, written: _PlanWrite
) -> dict[str, Any]:
    """Bring the notebook into line with a plan just approved, and report what that did.

    **Nothing here can fail the request.** The plan is on disk by the time this runs, that write
    is the thing the user clicked for, and answering 500 over a notebook that could not be reached
    would report a decision as not taken when it was taken and recorded. Every way this can go
    wrong comes back as ``synced: false`` and a sentence, which the pane shows beside the version
    it did write.

    Four ways it declines, and they are four different sentences because they need four different
    things done about them:

    * The plan landed as a **draft** — the blockers are already in the payload, and scaffolding
      an unapproved plan is the one thing :func:`~kedge.notebook.scaffold.sync_notebook` refuses
      structurally. Saying "the notebook is untouched because this is not in force yet" is the
      whole content of that refusal.
    * There is **no driver** — demo mode, or a marimo that never came up. The plan is still
      written and the next open scaffolds from it, which is the behaviour this route replaced and
      is a perfectly good fallback for the case where there is no kernel to write to.
    * There is **no workspace**, so the hand-in, workbook and contract paths the cells embed
      cannot be resolved. The scaffolder has defaults for all three, but they are derived from the
      plan rather than from the workspace, and a reconciliation cell pointed at the wrong workbook
      degrades to "not reconciled" — safe, and needlessly unhelpful. Declining is the better half
      of that trade when a driver without a workspace should not happen anyway.
    * The plan **will not build into cells**. That is a plan the scaffolder cannot render, and it
      is reported rather than raised for the same reason as the rest.
    """
    plan = written.plan
    if plan is None or not plan.approval.approved:
        return {
            "synced": False,
            "detail": (
                "The notebook is untouched: this landed as a draft, and nothing is scaffolded "
                "from a plan that is not in force. Clear the blockers above and approve it."
            ),
        }

    driver = getattr(registry.context, "driver", None)
    if driver is None:
        return {
            "synced": False,
            "detail": (
                f"Plan v{plan.version} is written, but no notebook kernel is attached, so nothing "
                f"was scaffolded from it. Opening this workbook will scaffold it."
            ),
        }
    workspace = state.workspace
    if workspace is None:
        return {
            "synced": False,
            "detail": (
                f"Plan v{plan.version} is written, but no workbook is open on this server, so the "
                f"paths its cells need could not be resolved and nothing was scaffolded."
            ),
        }

    from kedge.notebook.scaffold import sync_notebook

    try:
        result = await sync_notebook(
            plan,
            driver,
            previous=written.previous,
            handins_dir=workspace.handins_dir,
            workbook_path=workspace.workbook_path,
            contract_path=workspace.contract_path,
        )
    except (KedgeError, OSError) as exc:
        logger.warning("plan v%d was written but not scaffolded: %s", plan.version, exc)
        return {
            "synced": False,
            "detail": f"Plan v{plan.version} is written, but the notebook could not be built "
            f"from it: {exc}",
        }

    logger.info("scaffolded plan v%d into the live notebook", plan.version)
    return {
        "synced": True,
        "created": list(result.named("created")),
        "updated": list(result.named("updated")),
        "unchanged": list(result.named("unchanged")),
        "diverged": list(result.named("diverged")),
        "refused": list(result.named("refused")),
        "obsolete": list(result.obsolete),
        "detail": result.summary(plan.version),
    }


@router.delete("/api/sessions/{session_id}/pending/proposals/{index}")
def dismiss_proposal(session_id: str, index: int, request: Request) -> dict[str, Any]:
    """Decline a proposed plan. Nothing is written and the proposal is dropped."""
    state = get_state(request)
    _require_session(state, session_id)
    registry = _registry_for(state, session_id)
    pending = _pop_pending(registry, "pending_proposals", index)
    logger.info("user declined a proposed plan in session %s", session_id)
    return {
        "dismissed": pending.plan.summary or f"plan with {len(pending.plan.stages)} stage(s)",
        "pending": _pending_payload(registry),
    }


@router.post("/api/sessions/{session_id}/pending/proposals/{index}/drops/{drop}/acknowledge")
async def acknowledge_proposal_drop(
    session_id: str,
    index: int,
    drop: int,
    request: Request,
    body: DecisionBody = NO_NOTE,
) -> dict[str, Any]:
    """Confirm one range the proposal wants to drop, so it stops blocking approval.

    Also the way back from a refusal: confirming a range that was kept clears the blocker, and
    :func:`~kedge.plan.review.acknowledge_drop` takes the question the refusal raised with it. A
    mis-click has to have an exit, or this control is the gate-with-no-key it was written to
    remove.

    The drop is named by its position in the plan's own ``dropped`` list — the order the card
    renders — rather than by the range itself: a sheet name is free text and can carry any
    character a path segment would have to be taught to survive.

    ``async`` on all three of these is load-bearing. A synchronous path operation is handed to a
    threadpool, so two rapid clicks read, revise and write back the same slot of
    ``registry.pending_proposals`` in parallel and the second overwrites the first. The work is
    pure in-memory revision of a small object with nothing to await, so run it on the loop and it
    cannot interleave.
    """
    return _decide_drops(request, session_id, index, drop=drop, accepted=True, note=body.note)


@router.post("/api/sessions/{session_id}/pending/proposals/{index}/drops/{drop}/refuse")
async def refuse_proposal_drop(
    session_id: str,
    index: int,
    drop: int,
    request: Request,
    body: DecisionBody = NO_NOTE,
) -> dict[str, Any]:
    """Refuse one drop: the range must be kept.

    Not the mirror image of acknowledging, and the card must not draw it as one. Refusing records
    the decision and clears the "not acknowledged" blocker, then
    :func:`~kedge.plan.review.acknowledge_drop` raises the open question of which stage consumes
    the range, and :meth:`~kedge.plan.model.ProcessPlan.approval_blockers` replaces the old blocker
    with a new one until some stage lists it as a source. Adding that stage is a plan edit, which
    this pane does not do — so approval stays blocked and the plan will land as a draft to be
    finished with the plan surface. That is the gate working, and saying so on the button is the
    difference between a decision and a surprise. It is reversible: see
    :func:`acknowledge_proposal_drop`.
    """
    return _decide_drops(request, session_id, index, drop=drop, accepted=False, note=body.note)


@router.post("/api/sessions/{session_id}/pending/proposals/{index}/drops/acknowledge-all")
async def acknowledge_all_proposal_drops(
    session_id: str,
    index: int,
    request: Request,
    body: DecisionBody = NO_NOTE,
) -> dict[str, Any]:
    """Confirm every outstanding drop on this proposal at once.

    The same convenience the CLI's ``--all`` is, and the same audit trail: each drop is stamped
    individually, so a reviewer reading the plan a quarter later cannot tell this from a run of
    single clicks — which is right, because it is one. "Outstanding" is the operative word: a drop
    the user has already refused is left refused, because a convenience must not quietly overturn
    a decision somebody took deliberately.
    """
    return _decide_drops(request, session_id, index, drop=None, accepted=True, note=body.note)


def _decide_drops(
    request: Request,
    session_id: str,
    index: int,
    *,
    drop: int | None,
    accepted: bool,
    note: str | None,
) -> dict[str, Any]:
    """Record a drop decision on the pending proposal and hand back the refreshed card.

    **Nothing is written.** The revised plan replaces the one on the in-memory
    :class:`~kedge.agent.tools.PendingProposal` and goes no further;
    :func:`approve_proposal` remains the only route that reaches
    :class:`~kedge.plan.store.PlanStore`. A user who acknowledges a drop and then discards the
    proposal has left nothing behind, which is what "pending" has meant here all along.

    The *semantics* come from :func:`~kedge.plan.review.acknowledge_drop` and
    :func:`~kedge.plan.review.acknowledge_all_drops` — the same functions ``kedge plan
    acknowledge`` calls — so what a decision means, and the open question a refusal raises, are
    the CLI's and not a second implementation of them. What is **not** shared is the re-stamping
    those functions do on the way out, and :func:`_restore_proposal_identity` says why.

    A decision the plan already carries is a no-op. The card leaves its buttons live for the
    length of a round trip, so a double click is a real event, and
    :func:`~kedge.plan.review.acknowledge_drop` appends an open question on **every** refusal with
    nothing to stop it stacking a second identical one.

    Args:
        request: The live request, for the server state.
        session_id: The chat session holding the proposal.
        index: Position of the pending proposal.
        drop: Position of the drop in the plan's ``dropped`` list, or ``None`` for all outstanding.
        accepted: True to confirm the drop, False to keep the range.
        note: The reviewer's reason, recorded either way.
    """
    from kedge.plan.review import acknowledge_all_drops, acknowledge_drop

    state = get_state(request)
    _require_session(state, session_id)
    registry = _registry_for(state, session_id)
    proposal = _peek_pending(registry, "pending_proposals", index)
    plan = proposal.plan

    if drop is not None and not 0 <= drop < len(plan.dropped):
        raise HTTPException(
            status_code=404,
            detail=(
                f"This plan proposes {len(plan.dropped)} dropped range(s), so there is none at "
                f"position {drop}."
            ),
        )
    target = None if drop is None else plan.dropped[drop]
    decided = (
        [item.range for item in plan.unacknowledged_drops] if target is None else [target.range]
    )
    if _decision_stands(plan, target, accepted=accepted, note=note):
        logger.debug("drop decision in session %s changes nothing; leaving it alone", session_id)
        return _drop_response(registry, plan, decided=decided, accepted=accepted)

    try:
        revised = (
            acknowledge_all_drops(plan, note=note)
            if target is None
            else acknowledge_drop(plan, target.range, accepted=accepted, note=note)
        )
    except KedgeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    revised = _restore_proposal_identity(plan, revised)

    # `PendingProposal` is frozen, so the decision is recorded by replacing the entry rather than
    # by mutating it -- which is the point of it being frozen: the plan on a proposal changes only
    # where somebody meant it to.
    registry.pending_proposals[index] = replace(proposal, plan=revised)
    logger.info(
        "user %s %d dropped range(s) on a pending proposal in session %s",
        "confirmed" if accepted else "refused",
        len(decided),
        session_id,
    )
    return _drop_response(registry, revised, decided=decided, accepted=accepted)


def _decision_stands(plan: Any, drop: Any, *, accepted: bool, note: str | None) -> bool:
    """Whether the plan already says exactly this, so deciding again could only add noise.

    The note is part of the decision rather than a label on it. Keyed on the verdict alone, a
    re-post correcting the reason answered 200, reported the decision back, and kept the old
    words — an API that lies, even if the pane has no way to send it.

    "Every outstanding drop" is a different claim: with none outstanding it covers nothing, and
    the answer names the empty list it acknowledged, so there is no note there to correct.
    """
    if drop is None:
        return not plan.unacknowledged_drops
    return drop.acknowledged and drop.accepted == accepted and drop.note == note


def _drop_response(
    registry: Any, plan: Any, *, decided: list[str], accepted: bool
) -> dict[str, Any]:
    """The answer to a drop decision: what it covered, and the card redrawn from the result."""
    return {
        "acknowledged": decided,
        "accepted": accepted,
        "approval_blockers": plan.approval_blockers(),
        "pending": _pending_payload(registry),
    }


def _restore_proposal_identity(before: Any, after: Any) -> Any:
    """Put back the identity and provenance :func:`~kedge.plan.review._revise` re-stamps.

    Those functions are shaped for a plan that is *on disk*: ``_revise`` bumps the version, resets
    ``created_at`` and sets ``generated_by`` to ``human``, which is exactly right when the model's
    row already exists and this is the next one after it. ``kedge plan acknowledge`` writes into
    that shape — the ``llm`` version was recorded before the drop was signed off.

    A pending proposal has no row anywhere, because the card promises nothing is written until
    approval and that promise has to hold. Left to re-stamp, the store ended up holding a *single*
    version, authored ``human``, with nothing anywhere recording that a model wrote the
    decomposition — ``cli._plan_author`` shows the model id only for ``generated_by == "llm"``, so
    ``kedge plan history`` rendered a bare "human" for the only version there is. The version bump
    was doubly meaningless: :meth:`~kedge.plan.store.PlanStore.save_next` renumbers a plan down to
    the next free version and records the number it *arrived* with as ``based_on_version``, so a
    v2-in-memory proposal saved into an empty store came out as v1 deriving from v2.

    So the review verb is applied for its semantics and its numbering is undone: the only
    difference between ``before`` and what comes back is the drop decision itself, and the open
    question a refusal raises or an overturned refusal takes back. ``approval`` is deliberately
    not restored — a proposal in memory is
    never approved, and resetting to ``DRAFT`` is the safe direction if one ever were.
    """
    return after.model_copy(
        update={
            "version": before.version,
            "based_on_version": before.based_on_version,
            "generated_by": before.generated_by,
            "llm_model": before.llm_model,
            "created_at": before.created_at,
        }
    )


@router.delete("/api/sessions/{session_id}/pending/amendments/{index}")
def dismiss_amendment(session_id: str, index: int, request: Request) -> dict[str, Any]:
    """Decline an amendment. The approved plan is unchanged and the proposal is dropped."""
    state = get_state(request)
    _require_session(state, session_id)
    registry = _registry_for(state, session_id)
    pending = _pop_pending(registry, "pending_amendments", index)
    logger.info("user declined a plan amendment in session %s", session_id)
    return {"dismissed": pending.change, "pending": _pending_payload(registry)}


def _peek_pending(registry: Any, attribute: str, index: int) -> Any:
    """Return one pending decision, leaving it pending, or explain why it is not there.

    Split out of :func:`_pop_pending` for the drop routes, which decide something *inside* a
    pending proposal: acknowledging a dropped range is not deciding the proposal, and popping it
    would take the card off the panel with the plan it revised held nowhere.
    """
    if registry is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "This chat has no pending decisions. They are recorded by the agent loop during a "
                "turn; the scripted demo agent records none."
            ),
        )
    items = getattr(registry, attribute, [])
    if not 0 <= index < len(items):
        raise HTTPException(
            status_code=404,
            detail=f"There is no pending decision at position {index}; it may already be decided.",
        )
    return items[index]


def _pop_pending(registry: Any, attribute: str, index: int) -> Any:
    """Remove and return one pending decision, or explain why it is not there."""
    _peek_pending(registry, attribute, index)
    return getattr(registry, attribute).pop(index)


@dataclass(frozen=True, slots=True)
class _PlanWrite:
    """One plan version written to the store, and what the caller needs to act on it.

    ``payload`` is the JSON the route answers with. ``plan`` and ``previous`` are the objects
    behind it, which the payload cannot carry and
    :func:`_carry_the_plan_into_the_notebook` cannot do without: the first is what the notebook is
    built from, the second is what tells a cell kedge wrote from a cell somebody has since
    rewritten.
    """

    payload: dict[str, Any]
    plan: Any
    previous: Any = None


def _write_proposal(store: Any, proposal: Any, *, note: str | None) -> _PlanWrite:
    """Save the proposed plan, approving it where it is approvable on its own terms.

    The store is re-read here rather than trusted from the tool call. ``propose_plan`` refused if a
    plan was already approved, but that was a decision taken during a turn and this is a click
    that can arrive minutes later, with an amendment approved in between. The check is cheap and
    the thing it protects — that a whole replacement decomposition never displaces an approved plan
    without going through amendment review — is the point of the tool refusing at all.

    No ``previous`` comes back from here, and there is nothing to look for: this path runs only
    where ``latest_approved()`` is None, so no plan has ever been in force, nothing has been
    scaffolded, and every cell in the notebook is somebody's own work rather than kedge's.
    """
    from kedge.plan.review import PlanNotApprovableError, approve
    from kedge.plan.store import PlanStoreError

    in_force = store.latest_approved()
    if in_force is not None:
        msg = (
            f"plan v{in_force.version} was approved for this workbook after this one was "
            f"proposed, so writing it would replace an approved plan wholesale rather than amend "
            f"it. The proposal has been cleared. Ask kedge to raise what it wanted changed as an "
            f"amendment, or edit the approved plan directly."
        )
        raise PlanStoreError(msg)

    plan = proposal.plan
    approved = False
    blockers: list[str] = []
    try:
        # The user's note, or nothing. Falling back to the plan's own summary would put the
        # model's prose in the field a reader takes months later as the reviewer's account of
        # what they checked -- written by the thing being reviewed. An absent note is honest; a
        # borrowed one is not.
        plan = approve(plan, by="user", note=note)
        approved = True
    except PlanNotApprovableError:
        blockers = plan.approval_blockers()

    stamped, path = store.save_next(plan)
    return _PlanWrite(
        payload={
            "version": stamped.version,
            "based_on_version": stamped.based_on_version,
            "approved": approved,
            "blockers": blockers,
            "path": str(path),
            "stages": len(stamped.stages),
        },
        plan=stamped,
    )


def _write_amendment(store: Any, amendment: Any, *, note: str | None) -> _PlanWrite:
    """Derive, save and — where it is approvable — approve the amended plan.

    ``amend_plan``'s schema is free text: a rationale, a change stated in prose, and optionally
    the stage it affects. There is no structured edit to apply, so the maximum fidelity available
    is to *record* the approved amendment against the plan at a new version, in the field that
    belongs to it: the named stage's notes, or a plan-level open question where no stage is named.
    Reshaping the stage graph is a plan edit, and the plan editing surface
    (:mod:`kedge.plan.review`) is where that belongs.

    Every write goes through the public review functions, so the version bump, the provenance
    chain and the approval reset are the ones every other plan edit gets.

    **The plan amended is the approved one, or there is none.** An amendment card shows one
    sentence and its rationale, so approving one may only put one sentence into force. Reached
    through ``latest_approved() or latest()`` it did not: with a rejected or never-approved plan on
    disk, approving "mention the FX rate source" wrote a new version carrying that plan's entire
    stage list, approved, on the strength of a card that showed a sentence. A decomposition nobody
    approved has to be reviewed as a decomposition.
    """
    from kedge.plan.review import PlanNotApprovableError, add_question, approve, edit_stage
    from kedge.plan.store import PlanStoreError

    plan = store.latest_approved()
    if plan is None:
        raise PlanStoreError(_nothing_to_amend(store.latest()))

    stage = plan.stage(amendment.stage) if amendment.stage else None
    if stage is not None:
        stated = f"Approved amendment: {amendment.change}\nRationale: {amendment.rationale}"
        notes = f"{stage.notes}\n\n{stated}" if stage.notes else stated
        amended = edit_stage(plan, stage.id, notes=notes)
    else:
        amended = add_question(plan, amendment.change, context=amendment.rationale)

    approved = False
    blockers: list[str] = []
    try:
        # As in `_write_proposal`: the amendment's own text is not the reviewer's account of it.
        amended = approve(amended, by="user", note=note)
        approved = True
    except PlanNotApprovableError:
        blockers = amended.approval_blockers()

    stamped, path = store.save_next(amended)
    return _PlanWrite(
        payload={
            "version": stamped.version,
            "based_on_version": stamped.based_on_version,
            "approved": approved,
            "blockers": blockers,
            "path": str(path),
            "stage": amendment.stage if stage is not None else None,
        },
        plan=stamped,
        # The plan that was in force until a line ago. Its emission is what the sync compares the
        # notebook against, so the stage cell carrying the amended note is updated where nobody
        # has touched it and reported where somebody has.
        previous=plan,
    )


def _nothing_to_amend(latest: Any) -> str:
    """Explain why there is nothing for this amendment to attach to.

    Two cases, and they read differently to the person holding the card. An empty store is "there
    is no plan"; a store whose newest version was rejected, or was never approved, is "there is a
    plan and it is not in force" — and the second needs to say why approving one sentence against
    it is refused, or the refusal looks like an obstruction rather than the gate.
    """
    if latest is None:
        return (
            "there is no plan on disk to amend. Ask kedge to propose one first — an amendment to "
            "nothing is a plan, and a plan needs reviewing in full."
        )
    return (
        f"plan v{latest.version} is the newest on disk and its approval state is "
        f"'{latest.approval.state.value}', so no plan is in force and there is nothing to amend. "
        f"An amendment is one change approved against a decomposition that was already reviewed; "
        f"approving this one would put the whole of v{latest.version} — "
        f"{len(latest.stages)} stage(s) nobody has approved — into force behind it. Review "
        f"v{latest.version} in full and approve it, or ask kedge to propose a new plan. The "
        f"amendment has been cleared."
    )
