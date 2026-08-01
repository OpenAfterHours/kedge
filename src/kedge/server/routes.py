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

Two endpoints here are the user's half of decisions the model is not allowed to make on its own.
``delete_cell`` and ``amend_plan`` do not act: they record a request and tell the model plainly
that nothing has happened (``kedge.agent.tools``). ``/api/pending`` surfaces those requests and
``/api/pending/...`` acts on them, so the recorded intent has somewhere to go other than a log
file. Confirming a deletion runs it through the same notebook driver the agent would have used;
approving an amendment writes a new plan version through :mod:`kedge.plan.store`. Neither can be
reached without an explicit request from the browser, which is the whole of PLAN 2.2's gate.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

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
    }


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
        names = await fetch_model_names(base_url, key)
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
def delete_session(session_id: str, request: Request) -> dict[str, Any]:
    """Delete a session and its messages."""
    state = get_state(request)
    if not state.store.delete_session(session_id):
        raise HTTPException(status_code=404, detail=f"No chat session with id {session_id!r}.")
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
# `delete_cell` and `amend_plan` both record and refuse: the tool appends to the registry's
# pending list, tells the model plainly that nothing has happened, and returns. Until now there
# was no way for the user to say yes, which made both of them write-only. These four routes are
# the other half.
#
# The registries hang off the agent loop, keyed by chat session (`KedgeAgent.registries`). The
# server reads that attribute and requires nothing else of the loop, so the scripted stand-in —
# which has no registries at all — degrades to an empty list rather than a 500.


class DecisionBody(BaseModel):
    """Body for confirming or approving a pending decision."""

    note: str | None = None


def _registry_for(state: ServerState, session_id: str) -> Any:
    """Return the tool registry for one chat session, or ``None`` if there is not one.

    ``None`` is normal: demo mode has no registries, and a session that has never made a tool
    call has no registry until its first turn.
    """
    registries = getattr(state.agent, "registries", None)
    if not isinstance(registries, dict):
        return None
    return registries.get(session_id)


def _pending_payload(registry: Any) -> dict[str, Any]:
    if registry is None:
        return {"deletions": [], "amendments": []}
    return {
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
    body: DecisionBody,
    request: Request,
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
def approve_amendment(
    session_id: str,
    index: int,
    body: DecisionBody,
    request: Request,
) -> dict[str, Any]:
    """Write an approved plan amendment as a new plan version.

    The gate is not weakened by this, it is completed. The model's ``amend_plan`` call recorded a
    proposal and said so; this is the user reading it and deciding. What is written is a new
    version derived from the current plan with the amendment attached to the stage it names (or to
    the plan's summary where it names none), saved through
    :class:`~kedge.plan.store.PlanStore` so the history is retained and the change is a diff.

    Approval is recorded on that new version only when the plan is approvable on its own terms.
    An amendment to a plan with an unacknowledged dropped range lands as a **draft**, with the
    blockers reported back, because ``approve`` refusing is the gate working rather than an error
    to route around.
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
        result = _write_amendment(store, pending, note=body.note)
    except KedgeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    logger.info("user approved a plan amendment in session %s", session_id)
    return {**result, "pending": _pending_payload(registry)}


@router.delete("/api/sessions/{session_id}/pending/amendments/{index}")
def dismiss_amendment(session_id: str, index: int, request: Request) -> dict[str, Any]:
    """Decline an amendment. The approved plan is unchanged and the proposal is dropped."""
    state = get_state(request)
    _require_session(state, session_id)
    registry = _registry_for(state, session_id)
    pending = _pop_pending(registry, "pending_amendments", index)
    logger.info("user declined a plan amendment in session %s", session_id)
    return {"dismissed": pending.change, "pending": _pending_payload(registry)}


def _pop_pending(registry: Any, attribute: str, index: int) -> Any:
    """Remove and return one pending decision, or explain why it is not there."""
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
    return items.pop(index)


def _write_amendment(store: Any, amendment: Any, *, note: str | None) -> dict[str, Any]:
    """Derive, save and — where it is approvable — approve the amended plan.

    ``amend_plan``'s schema is free text: a rationale, a change stated in prose, and optionally
    the stage it affects. There is no structured edit to apply, so the maximum fidelity available
    is to *record* the approved amendment against the plan at a new version, in the field that
    belongs to it: the named stage's notes, or a plan-level open question where no stage is named.
    Reshaping the stage graph is a plan edit, and the plan editing surface
    (:mod:`kedge.plan.review`) is where that belongs.

    Every write goes through the public review functions, so the version bump, the provenance
    chain and the approval reset are the ones every other plan edit gets.
    """
    from kedge.plan.review import PlanNotApprovableError, add_question, approve, edit_stage
    from kedge.plan.store import PlanStoreError

    plan = store.latest_approved() or store.latest()
    if plan is None:
        msg = (
            "there is no plan on disk to amend. Ask kedge to propose one first — an amendment to "
            "nothing is a plan, and a plan needs reviewing in full."
        )
        raise PlanStoreError(msg)

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
        amended = approve(amended, by="user", note=note or amendment.change)
        approved = True
    except PlanNotApprovableError:
        blockers = amended.approval_blockers()

    stamped, path = store.save_next(amended)
    return {
        "version": stamped.version,
        "based_on_version": stamped.based_on_version,
        "approved": approved,
        "blockers": blockers,
        "path": str(path),
        "stage": amendment.stage if stage is not None else None,
    }
