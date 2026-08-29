"""The hub: the landing page, the workbook registry over HTTP, and the open sequence.

Until this existed, the only way into kedge was ``kedge open <workbook>`` — the server could not
be built before the CLI had chosen a workbook, and the notebook pane's placeholder told the user
to go back to the terminal. The hub is the other end: start the server with nothing open, list
what kedge has seen, add a workbook by browsing or dropping one, and open it from the browser.

Three parts:

* **The registry surface** — thin HTTP over :mod:`kedge.registry`, which does the deriving. State
  is recomputed from disk on every list, so a workbook deleted in Explorer shows as missing rather
  than as a working row that fails when clicked.
* **The file browser** — a server-side directory listing. This is a local, single-user, loopback
  tool, so browsing the real filesystem is the correct behaviour and is exactly what marimo's own
  ``mo.ui.file_browser`` does. Nothing is restricted by *location*; what is enforced is that the
  file is really an OOXML workbook before it is accepted (:func:`kedge.registry.validate_workbook`).
* **The open sequence** — the same steps ``kedge open`` runs, as a background job whose progress
  is streamed with the typed SSE machinery in :mod:`kedge.server.events`. It takes several seconds
  and does eight distinguishable things; PLAN M3 is explicit that a spinner will not do.
* **Closing** — the counterpart, and the reason opening a second workbook can go on being refused.
  One server owns one workbook and one marimo process; ``/api/hub/close`` is how the first one is
  let go, so picking the wrong file from the list costs a click rather than a restart.
* **Forgetting and releasing** — the two ways a conversion ends, and they are opposites.
  Forgetting deletes the workbook *and* everything kedge derived from it. Releasing deletes only
  the workbook: the notebook has become the monthly process and the spreadsheet is obsolete, which
  is the successful ending and, until this existed, the one the product could only render as
  breakage. Each is previewed before it is carried out and each stamps the registry last.

The job runs in an :class:`asyncio.Task` rather than inside the streaming response, and every
frame it emits is retained, so a browser that reloads mid-open reattaches and catches up instead
of orphaning a marimo process it can no longer see.

**Reattachment is only ever to our own process.** A workbook already showing a live marimo is
offered "reattach", and that offer is built from *our* marker file, confirmed over HTTP with the
token we generated. kedge never adopts a marimo it did not start (PLAN 2.9).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import string
import sys
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from kedge import purge
from kedge.errors import KedgeError
from kedge.registry import RegistryEntry, RegistryError, WorkbookRegistry, report_path_for
from kedge.server.events import (
    OPEN_STEPS,
    ErrorEvent,
    HubEvent,
    OpenProgressEvent,
    OpenReadyEvent,
    OpenStep,
    StepState,
    encode_sse,
    sse_comment,
)
from kedge.workspace import Workspace

if TYPE_CHECKING:
    from kedge.server.app import ServerState

logger = logging.getLogger(__name__)

__all__ = ["OpenJob", "open_workbook", "plan_workbook_name", "router"]

router = APIRouter()

_STREAM_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

_SHELL_HEADERS = {"Cache-Control": "no-store"}
"""Same reasoning as :data:`kedge.server.routes.SHELL_HEADERS`, which owns the explanation.

Repeated rather than imported for the same reason ``_STREAM_HEADERS`` is: ``routes`` and ``hub``
are siblings that both hang off ``app``, and a dependency between them to share two words of
header would be the wrong shape.
"""

_KEEPALIVE_SECONDS = 15.0
_MAX_UPLOAD_BYTES = 512 * 1024 * 1024
_UPLOAD_CHUNK = 1 << 20
_IS_WINDOWS = sys.platform == "win32"

_HIDDEN_DIRS = frozenset(
    {"__pycache__", "node_modules", "$RECYCLE.BIN", "System Volume Information"}
)
"""Directories that are never where a user keeps a workbook, and only make the listing longer."""


def _state(request: Request) -> ServerState:
    return request.app.state.kedge


def _registry(state: ServerState) -> WorkbookRegistry:
    return WorkbookRegistry.for_user(state.user_directory)


# ── request bodies ───────────────────────────────────────────────────────────────────────────


class AddWorkbookBody(BaseModel):
    """Body for registering a workbook already on this machine."""

    path: str


class OpenWorkbookBody(BaseModel):
    """Body for starting an open.

    ``reattach`` asks to adopt the marimo server our own marker file records for this workbook,
    rather than spawning a second one. It is honoured only when that server answers and accepts
    our token; otherwise the open falls through to a normal launch.
    """

    key: str | None = None
    path: str | None = None
    reattach: bool = False


# ── the pages ────────────────────────────────────────────────────────────────────────────────


@router.get("/hub", include_in_schema=False)
def hub_page(request: Request) -> FileResponse:
    """Serve the hub, whether or not a workbook is open.

    Reachable from the chat view too, so "which workbook am I in, and what else is there?" is one
    click rather than a restart.
    """
    return FileResponse(_state(request).static_dir / "hub.html", headers=_SHELL_HEADERS)


# ── the registry ─────────────────────────────────────────────────────────────────────────────


@router.get("/api/hub/state")
async def hub_state(request: Request) -> dict[str, Any]:
    """Everything the hub page needs to draw itself: the workbooks, and what is open here.

    The status sweep touches the filesystem for every entry and probes every recorded marimo over
    HTTP, so it runs in a threadpool rather than blocking the loop. The assistant lockdown is read
    in the same pass and for the same reason -- see :func:`_assistant_of`.
    """
    state = _state(request)
    registry = _registry(state)
    statuses = await run_in_threadpool(registry.statuses)
    rows = await run_in_threadpool(_workbook_rows, state, statuses)
    workspace = state.workspace
    return {
        "version": state.version,
        "attached": workspace is not None,
        "demo": state.demo,
        "open_workbook": None
        if workspace is None
        else {
            "key": workspace.key,
            "path": str(workspace.workbook_path),
            "name": workspace.workbook_path.name,
        },
        "registry_path": str(registry.path),
        "steps": list(OPEN_STEPS),
        "workbooks": rows,
    }


def _workbook_rows(state: ServerState, statuses: Sequence[Any]) -> list[dict[str, Any]]:
    """The derived status of every workbook, with the assistant lockdown merged onto each.

    Merged here rather than added to :class:`~kedge.registry.WorkbookStatus`, because that class
    is deliberately about what the *registry* can derive from the workbook and its project
    directory, and marimo's own configuration is a fact about a different tool.
    """
    return [status.to_dict() | _assistant_of(state, status.entry) for status in statuses]


def _assistant_of(state: ServerState, entry: RegistryEntry) -> dict[str, Any]:
    """Whether marimo's own AI assistant is live for one workbook, and what is stored beside it.

    Two independent facts, and the hub shows them under different conditions because they are
    dangerous under different conditions. ``assistant_enforced`` being false only matters where a
    kernel is actually up -- a workbook nobody has opened has no ``.marimo.toml`` at all and would
    otherwise wear the warning for ever, which is how a signal becomes one people stop reading.
    ``assistant_keys`` is a credential sitting in plaintext in the project directory, which is an
    exposure whether or not anything is running.

    Read on every sweep rather than captured at open, and that is the whole point of
    :func:`~kedge.lifecycle.assistant_status`: marimo rewrites that file whenever a setting changes
    in its own editor, so the assistant can be switched back on, or a key typed into its settings
    panel, an hour after a launch that reported the notebook clean. The hub polls, so what it shows
    is current rather than a snapshot of the open.

    Returns:
        ``assistant_enforced`` and ``assistant_keys`` -- dotted key *names*, never values, which
        is a guarantee of the API and the reason nothing downstream can render a credential. An
        empty mapping for a row whose path no longer resolves to a workspace: there is no project
        directory to have left anything in.
    """
    from kedge import lifecycle

    try:
        workspace = Workspace.for_workbook(entry.path, user_directory=state.user_directory)
    except KedgeError:  # pragma: no cover - describe() has already logged the same failure
        return {}
    lockdown = lifecycle.assistant_status(workspace)
    return {
        "assistant_enforced": lockdown.enforced,
        "assistant_keys": list(lockdown.secret_keys),
    }


@router.post("/api/hub/workbooks", status_code=201)
async def add_workbook(body: AddWorkbookBody, request: Request) -> dict[str, Any]:
    """Register a workbook by path, refusing anything that is not a readable workbook."""
    registry = _registry(_state(request))
    try:
        entry = await run_in_threadpool(registry.add, Path(body.path))
    except RegistryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KedgeError as exc:  # pragma: no cover - workspace construction is already guarded
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"workbook": entry.to_dict()}


@router.post("/api/hub/upload", status_code=201)
async def upload_workbook(request: Request, file: UploadFile) -> dict[str, Any]:
    """Accept a dropped workbook, save it under ``~/.kedge/dropped``, and register it.

    A dropped file arrives as bytes with no path, which is the same problem marimo's two file
    inputs have: ``mo.ui.file`` gives bytes with no path and ``mo.ui.file_browser`` gives a path
    with no bytes. kedge resolves it the way it resolves hand-ins — by giving the bytes a managed
    location on disk — because a workbook with no path cannot be re-analysed, re-opened, or named
    in a plan. The browse-and-pick path is still the better one and the page says so.
    """
    state = _state(request)
    name = Path(file.filename or "workbook.xlsx").name
    if Path(name).suffix.lower() not in {".xlsx", ".xlsm"}:
        raise HTTPException(
            status_code=400,
            detail=f"{name} is not a .xlsx or .xlsm file. kedge reads Office Open XML workbooks.",
        )

    directory = _dropped_dir(state)
    directory.mkdir(parents=True, exist_ok=True)
    destination = _unique_path(directory / name)
    written = 0
    try:
        with destination.open("wb") as handle:
            while chunk := await file.read(_UPLOAD_CHUNK):
                written += len(chunk)
                if written > _MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"{name} is larger than {_MAX_UPLOAD_BYTES // (1024 * 1024)}MB. Add it "
                            f"by path instead — kedge reads it in place and never copies it."
                        ),
                    )
                handle.write(chunk)
    except HTTPException:
        destination.unlink(missing_ok=True)
        raise
    except OSError as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"could not save {name}: {exc}") from exc

    registry = _registry(state)
    try:
        entry = await run_in_threadpool(registry.add, destination)
    except KedgeError as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"workbook": entry.to_dict(), "saved_to": str(destination)}


@router.get("/api/hub/workbooks/{key}/deletion")
async def preview_deletion(key: str, request: Request) -> dict[str, Any]:
    """Say exactly what forgetting this workbook would destroy, without destroying any of it.

    The confirmation is the whole safety mechanism now that forgetting deletes, and a confirmation
    the user cannot check is not one. So the counts here are read off the filesystem and the
    sessions table at the moment they are asked for, rather than described in the abstract by a
    dialogue that was written once and never sees a particular workbook.
    """
    state = _state(request)
    entry = _entry_or_404(state, key)
    workspace = Workspace.for_workbook(entry.path, user_directory=state.user_directory)
    sessions = await run_in_threadpool(
        state.store.session_ids_for_notebook, str(workspace.notebook_path)
    )
    plan = await run_in_threadpool(purge.plan_purge, workspace, session_ids=sessions)
    return {
        "key": key,
        "name": entry.name,
        "workbook": str(plan.workbook),
        "workbook_exists": plan.workbook.is_file(),
        "sessions": len(sessions),
        "items": list(purge.describe(plan, sessions=len(sessions))),
        "external": [str(item.path) for item in plan.external_present],
        "open": state.workspace is not None and state.workspace.key == key,
    }


@router.delete("/api/hub/workbooks/{key}")
async def forget_workbook(key: str, request: Request) -> dict[str, Any]:
    """Forget a workbook: delete it and everything kedge derived from it.

    This used to remove the registry row alone, on the reasoning that a landing page must not be
    able to delete a user's artifacts. The reasoning was sound and the behaviour was still wrong,
    because every artifact is addressed from the workbook's resolved *path* -- so re-adding the
    same file brought the plan, the notebook, the run records and the whole conversation straight
    back, and "forget" was a word the product did not mean. It means it now.

    Three things make that safe enough to do from a browser. The workbook currently open on this
    server is refused outright, because deleting the notebook out from under a running marimo
    leaves a kernel holding a file that no longer exists. The registry row goes **last** and only
    on a clean purge, so a deletion that fails halfway leaves the card in the list where the user
    can see it and try again rather than orphaning a directory nothing points at any more. And
    the caller is expected to have shown the user :func:`preview_deletion` first.

    Configured locations *outside* the project directory are removed too, but only when no other
    registered workbook resolves to the same place: ``ingest.store_dir`` may be an absolute path
    two workbooks share, and taking one workbook's hand-ins with another's is not a thing the user
    asked for.
    """
    state = _state(request)
    entry = _entry_or_404(state, key)
    _refuse_if_open(state, key, entry.name)

    workspace = Workspace.for_workbook(entry.path, user_directory=state.user_directory)
    sessions = await run_in_threadpool(
        state.store.session_ids_for_notebook, str(workspace.notebook_path)
    )
    plan = await run_in_threadpool(purge.plan_purge, workspace, session_ids=sessions)
    shared = _externals_shared_with_others(state, key, plan)

    result = await run_in_threadpool(
        purge.execute,
        plan,
        include_workbook=True,
        include_external=not shared,
    )
    removed_sessions = await run_in_threadpool(
        state.store.delete_sessions_for_notebook, str(workspace.notebook_path)
    )

    if not result.ok:
        # The row is deliberately still there. Everything below reads as "this went wrong, the
        # workbook is still in your list, here is the file that would not go" -- which is what a
        # locked .xlsx open in Excel looks like, and is fixable by closing Excel.
        first, reason = result.failures[0]
        raise HTTPException(
            status_code=409,
            detail=(
                f"{entry.name} could not be fully deleted: {first.name} is in use ({reason}). "
                f"Close anything holding it and try again. {len(result.removed)} of "
                f"{len(result.removed) + len(result.failures)} items were removed."
            ),
        )

    registry = _registry(state)
    if not registry.forget(key):  # pragma: no cover - _entry_or_404 has already found it
        raise HTTPException(status_code=404, detail=f"No workbook with key {key!r} is registered.")
    return {
        "forgotten": key,
        "removed": len(result.removed),
        "sessions": removed_sessions,
        "left_behind": [str(item.path) for item in plan.external_present] if shared else [],
    }


@router.get("/api/hub/workbooks/{key}/release")
async def preview_release(key: str, request: Request) -> dict[str, Any]:
    """Say what releasing this workbook would delete and, far more importantly, what it keeps.

    The counterpart to :func:`preview_deletion`, and it carries the opposite burden. That
    confirmation has to make a user hesitate over a set of files they cannot see; this one has to
    make a user *confident* about deleting the spreadsheet a whole process was built on, and the
    only thing that earns that is a list of what survives, with counts read off the disk at the
    moment they are asked for.

    Chat sessions are counted and named among what is kept. They are the record of how the
    conversion was arrived at and the process is continuing rather than ending, so nothing here or
    in :func:`release_workbook` removes them -- ``session_ids`` reaches
    :func:`~kedge.purge.plan_release` so the list can say so out loud.

    The marimo marker and token are the one exception, and the list has to be honest about it.
    :func:`~kedge.purge.plan_release` keeps them, correctly, because it cannot tell a marker for a
    running server from a marker for a dead one -- but :func:`_sweep_marker` can, and does remove
    a stale pair. A dialogue that promises to keep something the very next click deletes is how a
    confirmation teaches a user to stop reading it, so when the marker is stale the two are struck
    off what is shown rather than left in it.

    **Whether the translation was ever accepted is the load-bearing fact here**, and it is the one
    the enumeration cannot reach: everything else in the list is a file that survives, while this
    is a property of the conversion that a release can destroy the possibility of. The workbook is
    the only thing the notebook's arithmetic could ever be measured against, so releasing without a
    recorded acceptance ends that question for the life of the notebook. It is read off
    :class:`~kedge.registry.WorkbookStatus`, which already derives it from the acceptance record,
    rather than reconstructed here.
    """
    # Aliased on the way in: ``purge.describe`` is a different function about the same workbook,
    # and two bare ``describe``s a few lines apart is a reading hazard rather than a naming one.
    from kedge.registry import describe as describe_workbook

    state = _state(request)
    entry = _entry_or_404(state, key)
    workspace = Workspace.for_workbook(entry.path, user_directory=state.user_directory)
    sessions = await run_in_threadpool(
        state.store.session_ids_for_notebook, str(workspace.notebook_path)
    )
    plan = await run_in_threadpool(purge.plan_release, workspace, session_ids=sessions)
    marker, port = await run_in_threadpool(_marker_state, workspace)
    status = await run_in_threadpool(describe_workbook, entry, user_directory=state.user_directory)

    # Struck off unconditionally, not merely when they are stale right now. Filtering on the
    # current answer left a race with a consequence out of all proportion to its size: a marker
    # live at preview and dead at the click was promised as kept and then swept, so the list --
    # whose entire job is to be a list of guarantees -- was the thing that turned out not to be
    # one. The marker's fate is decided at click time and belongs in the sentence about the live
    # marimo, which says why it survives, and in the POST's own report of what it did.
    #
    # Rebuilt as a plan rather than filtered as rendered lines, so there is still exactly one
    # function that turns a retained item into a phrase.
    shown = replace(
        plan,
        kept=tuple(item for item in plan.kept if item.path not in _marker_and_token(workspace)),
    )
    return {
        "key": key,
        "name": entry.name,
        "workbook": str(plan.workbook),
        "workbook_exists": plan.workbook.is_file(),
        "released": entry.released,
        "released_at": entry.released_at,
        "notebook": str(workspace.notebook_path),
        "notebook_exists": workspace.notebook_path.is_file(),
        "sessions": len(sessions),
        "kept": list(purge.describe_kept(shown, sessions=len(sessions))),
        "marker": marker,
        "marker_port": port,
        # "recorded" whatever the verdict says, because the question is whether there is anything
        # left to cite. An acceptance recorded as a failure is still a fact that outlives the
        # spreadsheet and is visible for ever; no acceptance at all is the silence a release makes
        # permanent. The verdict rides alongside so the dialogue can quote it rather than grade it.
        "acceptance": "recorded" if status.reconciliation else "none",
        "acceptance_status": status.reconciliation,
        "accepted_at": status.reconciled_at,
        "open": state.workspace is not None and state.workspace.key == key,
    }


@router.post("/api/hub/workbooks/{key}/release")
async def release_workbook(key: str, request: Request) -> dict[str, Any]:
    """Release a workbook: delete the spreadsheet, keep everything kedge derived from it.

    This is the successful end of a conversion rather than a way of tidying up after a failed one.
    The notebook is the monthly process now, the plans, contract, run records, acceptance record,
    hand-ins and chat sessions all go on being the evidence behind it, and the workbook is the one
    thing that has stopped being true. Until this route existed kedge could not express that: a
    workbook not on disk rendered as a file somebody had lost.

    **The registry is stamped last, and only on a clean result.** For a deletion "last" means the
    row is *removed* last, so a half-finished purge leaves the card in the list where it can be
    retried. Nothing is removed here, so the equivalent has to be worked out rather than copied,
    and it is this: ``released_at`` is a *claim that the spreadsheet is gone*, and kedge must not
    record a decision the filesystem has not carried out. Stamping first and then failing to
    delete would leave the hub saying "released, the notebook is the process" over a workbook
    still sitting on the share, which is the one lie this whole feature exists to stop telling.
    Stamping last inverts the failure: a delete that succeeds and a registry write that does not
    shows as ``missing``, which is the wrong framing but is exactly the state kedge was in before
    any of this existed -- and one more click of Release fixes it, because
    :func:`~kedge.purge.execute` treats an already-absent file as a success.

    The workbook this server has open is refused for the same reason a deletion is. Chat sessions
    are deliberately left alone; see :func:`preview_release`. The marimo marker and token are the
    one thing a release removes besides the workbook, and only when nothing is answering on the
    port they record -- :func:`_sweep_marker` owns that decision and says what it did in the
    response, so a release taken while a server is still up is visible rather than silent.

    **A conversion with no recorded acceptance is warned about, not refused**, and the warning
    lives in the dialogue :func:`preview_release` feeds rather than here. Refusing was the obvious
    alternative and it is wrong three times over. It would make release unreachable for exactly
    the conversions that can never be reconciled -- the ones that deliberately improve on the
    workbook and reproduce nothing, which is why ``not_reproduced`` exists at all. It would be the
    first place in kedge where an unreconciled state blocks an action, against a doctrine that
    reports reconciliation as a decision with a reason and never as a gate. And it would be walked
    around in one Explorer window, or by Forget, which takes the workbook too with no such check --
    a control that is one click to evade is not a control, it is a lesson in routing around kedge.
    What the dialogue does instead is arm its type-to-confirm box for that case alone, which is the
    criterion :func:`forget_workbook` uses and which this case meets and an ordinary release does
    not: the loss is invisible afterwards, permanent, and about the notebook rather than a file.

    Idempotent, because :meth:`~kedge.registry.WorkbookRegistry.release` is: releasing a row that
    is already released keeps the original timestamp, and re-releasing one whose delete failed
    last time is how the user retries it.
    """
    state = _state(request)
    entry = _entry_or_404(state, key)
    _refuse_if_open(state, key, entry.name, hazard=_RELEASE_HAZARD, verb="release")

    workspace = Workspace.for_workbook(entry.path, user_directory=state.user_directory)
    _refuse_if_unconverted(workspace, entry.name)
    sessions = await run_in_threadpool(
        state.store.session_ids_for_notebook, str(workspace.notebook_path)
    )
    plan = await run_in_threadpool(purge.plan_release, workspace, session_ids=sessions)
    result = await run_in_threadpool(purge.execute, plan, include_workbook=True)

    if not result.ok:
        # Nothing is stamped. The row stays linked, the card keeps offering Release, and the
        # sentence below names the one file that would not go -- which on Windows is almost
        # always the workbook still open in Excel.
        first, reason = result.failures[0]
        raise HTTPException(
            status_code=409,
            detail=(
                f"{entry.name} could not be released: {first.name} could not be deleted "
                f"({reason}). Close anything holding it and try again. Nothing else was touched, "
                f"and kedge has not recorded the release."
            ),
        )

    # Before the stamp, so the registry write stays the last thing that happens on this path and
    # the ordering rule above has no exception to carry.
    marker, marker_detail = await run_in_threadpool(_sweep_marker, workspace)

    # Counted here rather than off the plan built above, and the difference is not cosmetic:
    # `kept_present` stats the disk, so taken before the sweep it counts the marker and token this
    # very call has just deleted. Nothing renders this number today, which is exactly why it would
    # have been wrong on the day something did.
    kept = len(plan.kept_present)

    released = _registry(state).release(key)
    if released is None:  # pragma: no cover - _entry_or_404 has already found it
        raise HTTPException(status_code=404, detail=f"No workbook with key {key!r} is registered.")
    return {
        "released": key,
        "name": entry.name,
        "workbook": str(plan.workbook),
        "released_at": released.released_at,
        "removed": len(result.removed),
        "kept": kept,
        "sessions": len(sessions),
        "marker": marker,
        "marker_detail": marker_detail,
    }


def _entry_or_404(state: ServerState, key: str) -> RegistryEntry:
    """The registry row for ``key``, or a 404 naming it."""
    entry = next((item for item in _registry(state).entries() if item.key == key), None)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"No workbook with key {key!r} is registered.")
    return entry


_DELETE_HAZARD = (
    "deleting a notebook a running marimo is holding would leave the kernel on a file that no "
    "longer exists"
)

_RELEASE_HAZARD = (
    "releasing it deletes the spreadsheet while a kernel that may still be reading it is running "
    "-- the notebook's reconciliation cell re-reads the workbook whenever the translation check "
    "runs, so a release taken mid-session can leave a working notebook failing on a file that is "
    "no longer there"
)


def _refuse_if_open(
    state: ServerState,
    key: str,
    name: str,
    *,
    hazard: str = _DELETE_HAZARD,
    verb: str = "delete",
) -> None:
    """Refuse to destroy anything belonging to the workbook this server currently has open.

    Not :func:`_refuse_if_busy`, which is the opposite test: that one permits the workbook already
    open, because re-opening it is how a reattach works. Here that is precisely the case to stop.

    Args:
        state: The server state, holding whichever workspace is attached.
        key: The workspace key the caller wants to act on.
        name: The workbook's name, for the message.
        hazard: What would go wrong, in a clause that follows "and". A release and a deletion take
            different files away from a live kernel, and a refusal that describes the wrong one
            sends the user looking for a problem they do not have.
        verb: What the user was trying to do, for the closing instruction.
    """
    if state.workspace is not None and state.workspace.key == key:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{name} is the workbook this kedge server has open, and {hazard}. Close it "
                f"first, then {verb} it."
            ),
        )


def _externals_shared_with_others(state: ServerState, key: str, plan: purge.PurgePlan) -> bool:
    """Whether another registered workbook resolves to the same configured external location."""
    if not plan.external_present:
        return False
    wanted = {item.path for item in plan.external_present}
    for entry in _registry(state).entries():
        if entry.key == key:
            continue
        try:
            other = Workspace.for_workbook(entry.path, user_directory=state.user_directory)
        except KedgeError:  # pragma: no cover - an unresolvable row cannot be sharing anything
            continue
        if {other.handins_dir, other.contract_path} & wanted:
            return True
    return False


def _refuse_if_unconverted(workspace: Workspace, name: str) -> None:
    """Refuse to release a workbook kedge has built no notebook from.

    The hub already hides the button in this case, and that gate is not enough. A button is a
    convenience and the route is the contract, so anything reaching it another way -- a script, a
    stale page, a verb added later -- would get the purge of the wrong half under a word promising
    the opposite: the spreadsheet deleted and an empty project directory kept.

    The deletion is not even the worst of it. A release *writes a claim into the registry*, and
    the hub renders that claim as "the spreadsheet is gone and this notebook is the process".
    Over a workspace with no notebook that sentence is simply false, and a product stating
    something false about its own artifacts is the exact failure this whole feature exists to
    stop. Forget is the verb for a workbook with nothing derived from it, and the message says so
    -- instruction first, because a user stopped here needs the way forward before the reason.

    Raises:
        HTTPException: 409, naming both ways out.
    """
    if workspace.notebook_path.is_file():
        return
    raise HTTPException(
        status_code=409,
        detail=(
            f"Convert {name} first, or forget it instead. Releasing records that the notebook has "
            f"become the process, and kedge has not generated a notebook from this workbook -- so "
            f"a release would delete the spreadsheet, keep an empty project directory, and file a "
            f"claim that a process is running here when nothing here runs."
        ),
    )


def _marker_state(workspace: Workspace) -> tuple[str, int | None]:
    """Whether a marimo marker exists for this workspace, and whether anything answers it.

    The one question both halves of a release need and neither can answer from the plan:
    :mod:`kedge.purge` does no HTTP, so it cannot tell a marker for a running server from a
    marker for a dead one, and it is right not to try -- a second artifact list in there is the
    drift the ``kept`` inversion exists to prevent.

    Returns:
        ``absent``, ``live`` or ``stale``, and the port the marker records -- ``None`` when there
        is no marker. The port is what the two callers' sentences need; they write their own,
        because one of them is about what *would* happen and the other about what did.
    """
    from kedge.lifecycle import health_check

    marker = workspace.read_marker()
    if marker is None:
        return "absent", None
    return ("live" if health_check(marker.base_url) else "stale"), marker.port


def _marker_and_token(workspace: Workspace) -> set[Path]:
    """The two machine-scoped files a release sweeps when nothing is serving the notebook."""
    return {workspace.marker_path, workspace.token_file_path}


def _sweep_marker(workspace: Workspace) -> tuple[str, str]:
    """Remove a released workspace's marimo marker and token, but only if nothing is serving it.

    A release keeps both deliberately while a server is alive: the marker is the only record of a
    live marimo's port and token, and removing it orphans the very process
    :func:`~kedge.lifecycle.cleanup_orphan` exists to find. That reasoning stops holding the
    moment the server does. What is left then is an inert credential sitting in plaintext under
    ``~/.kedge`` on a workspace nobody will open for a month, and inert is not the same as fine.

    **Not** :func:`~kedge.lifecycle.cleanup_orphan`, though it asks the same liveness question.
    That function answers it by *stopping* the server it finds, which is right before an open,
    where the job is to reclaim a port. It is wrong twice here: a release must not kill a marimo
    another kedge is serving, and the notebook that server is serving is the process this release
    is graduating -- the one thing that should go on running. It would also clear the marker on
    the way past, which is precisely the case for keeping it.

    Never raises. The workbook is gone by the time this is called, so a marker that will not
    delete must not turn a completed release into a 500; the next open clears it either way.

    Returns:
        What happened -- ``absent``, ``kept`` or ``cleared`` -- and one sentence for the response.
        ``kept`` is reported rather than passed over, because a release taken while something is
        still serving this notebook is worth seeing rather than inferring.
    """
    condition, port = _marker_state(workspace)
    if condition == "absent":
        return "absent", "No marimo marker was recorded for this workbook."
    if condition == "live":
        return "kept", (
            f"A marimo is still serving this notebook on port {port}, so its marker and token "
            f"were left where they are -- removing them would orphan a running server."
        )
    try:
        workspace.clear_marker()
    except KedgeError as exc:
        logger.warning("could not clear the marker for %s: %s", workspace.key, exc)
        return "kept", (
            f"Nothing is answering on port {port}, but the marker could not be removed ({exc}). "
            f"The next open will clear it."
        )
    workspace.clear_token_file()
    return "cleared", (
        f"Nothing was answering on port {port}, so the stale marker and its token file went with "
        f"the workbook rather than staying readable until the next open."
    )


@router.post("/api/hub/close")
async def close_workbook(request: Request) -> dict[str, Any]:
    """Release the open workbook, so a different one can be opened without a restart.

    The counterpart to the 409 :func:`_refuse_if_busy` raises. That refusal is right — one server
    owns one workbook and one marimo process (PLAN 2.9) — but until this route existed it was also
    terminal: a user who picked the wrong file from a list of six was told to stop the server and
    start it again, from a browser, which is the one place they could not do it.

    Closing is explicit rather than implicit in the next open. Opening a workbook is a visible,
    eight-step, several-second sequence with a marimo process on the end of it, and quietly killing
    one because the user clicked a different card would be exactly the kind of unasked-for
    destruction kedge refuses everywhere else. The hub asks first; this route is the yes.
    """
    from kedge.server.app import TurnInFlightError

    state = _state(request)
    if state.workspace is None:
        return {"closed": None, "detail": "No workbook was open."}

    name = state.workspace.workbook_path.name
    try:
        released = await run_in_threadpool(state.detach)
    except TurnInFlightError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    logger.info("closed workbook %s at the user's request", name)
    return {
        "closed": None if released is None else str(released.workbook_path),
        "name": name,
        "detail": f"{name} is closed and its marimo has been stopped.",
    }


@router.get("/api/hub/report/{key}")
def workbook_report(key: str, request: Request) -> FileResponse:
    """Serve the generated HTML analysis report for one workbook.

    Served rather than linked as a ``file://`` URL, which browsers refuse to follow from an
    ``http://`` page. The report is a self-contained page written by :func:`kedge.report.write_report`
    during the analysis step of an open.
    """
    state = _state(request)
    entry = _registry(state).get(key)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"No workbook with key {key!r} is registered.")
    try:
        workspace = Workspace.for_workbook(entry.path, user_directory=state.user_directory)
    except KedgeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    path = report_path_for(workspace)
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail=(
                f"No report has been generated for {entry.name} yet. Open it, or run "
                f"`kedge inspect {entry.path} --report <file>`."
            ),
        )
    return FileResponse(path, media_type="text/html")


# ── the file browser ─────────────────────────────────────────────────────────────────────────


@router.get("/api/hub/browse")
async def browse(request: Request, path: str | None = None) -> dict[str, Any]:
    """List a directory, so a workbook can be picked without leaving the browser.

    Nothing is confined to a root. This is a single-user tool on loopback with no accounts, and a
    sandbox here would be security theatre that stopped the user reaching their own S: drive
    (PLAN 2.9). Directories and workbooks are listed; other files are counted, not named, so the
    listing stays about the job.
    """
    return await run_in_threadpool(_browse, path)


def _browse(path: str | None) -> dict[str, Any]:
    target = Path(path).expanduser() if path else Path.home()
    try:
        target = target.resolve()
    except OSError as exc:  # pragma: no cover - unresolvable path
        raise HTTPException(status_code=400, detail=f"could not resolve {path}: {exc}") from exc
    if not target.is_dir():
        raise HTTPException(status_code=404, detail=f"{target} is not a directory.")

    directories: list[dict[str, Any]] = []
    workbooks: list[dict[str, Any]] = []
    other = 0
    try:
        for item in sorted(target.iterdir(), key=lambda entry: entry.name.lower()):
            try:
                if item.is_dir():
                    if not item.name.startswith(".") and item.name not in _HIDDEN_DIRS:
                        directories.append({"name": item.name, "path": str(item)})
                    continue
                if item.suffix.lower() in {".xlsx", ".xlsm"} and not item.name.startswith("~$"):
                    workbooks.append(
                        {
                            "name": item.name,
                            "path": str(item),
                            "size_bytes": item.stat().st_size,
                        }
                    )
                else:
                    other += 1
            except OSError:  # pragma: no cover - raced or unreadable entry
                continue
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=f"{target} cannot be listed: {exc}") from exc
    except OSError as exc:  # pragma: no cover - drive vanished
        raise HTTPException(status_code=400, detail=f"{target} cannot be listed: {exc}") from exc

    parent = None if target.parent == target else str(target.parent)
    return {
        "path": str(target),
        "parent": parent,
        "roots": [str(Path.home()), *(_drive_roots())],
        "directories": directories,
        "workbooks": workbooks,
        "other_file_count": other,
    }


def _drive_roots() -> list[str]:
    """Return the filesystem roots worth offering as shortcuts.

    On Windows the process's own drive is not enough: the workbook is as likely to be on a mapped
    network drive as on C:. ``Path("/")`` is deliberately not offered there — it exists, but it
    resolves against the *current* drive and is a shortcut to somewhere the user did not ask for.
    """
    if _IS_WINDOWS:
        roots = []
        for letter in string.ascii_uppercase:
            candidate = Path(f"{letter}:/")
            with contextlib.suppress(OSError):
                if candidate.is_dir():
                    roots.append(str(candidate))
        return roots
    return ["/"]  # pragma: no cover - POSIX only


def _dropped_dir(state: ServerState) -> Path:
    from kedge.config import user_dir

    return (state.user_directory or user_dir()) / "dropped"


def _unique_path(path: Path) -> Path:
    """Return ``path``, or the first ``name (2).xlsx`` style variant that is free."""
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{path.stem}-{uuid.uuid4().hex[:8]}{path.suffix}")  # pragma: no cover


# ── opening a workbook ───────────────────────────────────────────────────────────────────────


@dataclass
class OpenJob:
    """One run of the open sequence, with everything it has said so far.

    The frames are retained rather than only broadcast, so a subscriber that attaches late — a
    reloaded tab, a second window — is caught up in full instead of joining halfway through a
    sequence it cannot infer the start of.

    ``None`` on a subscriber's queue means the job has ended. A sentinel rather than a final event
    because "the job stopped" is not something the user should see a step for, and a stream parked
    on a queue must not have to wait out a keep-alive to notice.
    """

    job_id: str
    workbook: str
    frames: list[HubEvent] = field(default_factory=list)
    subscribers: set[asyncio.Queue[HubEvent | None]] = field(default_factory=set)
    finished: bool = False
    task: asyncio.Task[None] | None = None

    def publish(self, event: HubEvent) -> None:
        """Record ``event`` and hand it to every attached subscriber."""
        self.frames.append(event)
        self._push(event)

    def step(self, name: OpenStep, condition: StepState, detail: str = "") -> None:
        """Publish one progress frame. The step functions' only way to speak."""
        self.publish(OpenProgressEvent(step=name, state=condition, detail=detail))

    def close(self) -> None:
        """Mark the job finished and release every stream parked on it."""
        self.finished = True
        self._push(None)

    def _push(self, event: HubEvent | None) -> None:
        for queue in list(self.subscribers):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)

    @contextlib.contextmanager
    def subscribe(self) -> Iterator[asyncio.Queue[HubEvent | None]]:
        """Attach a subscriber for the duration of the block."""
        queue: asyncio.Queue[HubEvent | None] = asyncio.Queue(maxsize=256)
        self.subscribers.add(queue)
        try:
            yield queue
        finally:
            self.subscribers.discard(queue)


@router.post("/api/hub/open", status_code=202)
async def start_open(body: OpenWorkbookBody, request: Request) -> dict[str, Any]:
    """Begin opening a workbook and return the job id to stream progress from.

    Answers immediately. The work happens in a background task so that a browser reload does not
    abandon a half-spawned marimo, and progress is read back from ``/api/hub/open/{job_id}``.
    """
    state = _state(request)
    registry = _registry(state)

    entry = registry.get(body.key) if body.key else None
    # The conflict is decided before anything is written, so a refused open does not leave a row
    # in the registry the user never got to use.
    _refuse_if_busy(state, entry.path if entry else body.path)

    if entry is None and body.path:
        try:
            entry = await run_in_threadpool(registry.add, Path(body.path))
        except KedgeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail="Give either the key of a registered workbook or the path of a new one.",
        )

    _prune_jobs(state)
    job = OpenJob(job_id=uuid.uuid4().hex[:12], workbook=entry.path)
    state.opens[job.job_id] = job
    job.task = asyncio.create_task(
        _run_open(state, job, entry.key, Path(entry.path), reattach=body.reattach)
    )
    logger.info("open job %s started for %s", job.job_id, entry.path)
    return {"job_id": job.job_id, "key": entry.key, "workbook": entry.path}


@router.get("/api/hub/open/{job_id}")
async def stream_open(job_id: str, request: Request) -> StreamingResponse:
    """Stream one open job's progress, replaying everything it has already said."""
    state = _state(request)
    job = state.opens.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No open job with id {job_id!r}.")

    async def stream() -> AsyncIterator[str]:
        with job.subscribe() as queue:
            # Snapshotted before the first yield, which is the only suspension point available to
            # a publisher: subscribe-then-snapshot with no await in between means every frame
            # lands in exactly one of the two, never both and never neither.
            replay = list(job.frames)
            finished = job.finished
            yield sse_comment(f"kedge open {job_id}")
            for event in replay:
                yield encode_sse(event)
            if finished:
                return
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=_KEEPALIVE_SECONDS)
                except TimeoutError:
                    yield sse_comment("keep-alive")
                    continue
                if event is None:
                    return
                yield encode_sse(event)

    return StreamingResponse(stream(), media_type="text/event-stream", headers=_STREAM_HEADERS)


async def _run_open(
    state: ServerState,
    job: OpenJob,
    key: str,
    workbook: Path,
    *,
    reattach: bool,
    workspace: Workspace | None = None,
    plan_path: Path | None = None,
) -> None:
    """Run the open sequence, publishing one progress frame per step.

    Never raises. Anything that fails becomes a failed step plus an unrecoverable
    :class:`~kedge.server.events.ErrorEvent`, because a browser waiting on this stream is the only
    thing the user is looking at.

    ``plan_path`` reaches :func:`_step_plan` and nothing else. It defaults to None so the hub's own
    route, which has no way to name a file, keeps taking the plan store's latest approved version.
    """
    from kedge import __version__

    try:
        if workspace is None:
            workspace = Workspace.for_workbook(workbook, user_directory=state.user_directory)
        workspace.ensure_dirs()

        await _step_bridge(job)
        adopted = await _step_cleanup(workspace, job, reattach=reattach)
        analysis = await _step_analyse(workspace, job)
        plan = await _step_plan(workspace, job, plan_path=plan_path)
        await _step_notebook(workspace, job)

        if adopted is None:
            await _step_launch(workspace, job, kedge_version=__version__)
        else:
            # The same warning on the reattach path. An adopted server is one kedge started, so
            # its lockdown was written -- and its `.marimo.toml` has had the whole of that
            # server's lifetime to be rewritten from inside the editor, which is the case the
            # note exists for.
            note = await run_in_threadpool(_assistant_note, workspace)
            job.step("launching", "skipped", f"reattached to our own marimo on {adopted}{note}")
        await _step_session(workspace, job)

        driver = await _step_scaffold(workspace, plan, job, plan_path=plan_path)
        demo = await _step_agent(state, workspace, analysis, driver, job)

        if key:
            _registry(state).record_open(key)
        job.publish(
            OpenReadyEvent(
                key=key,
                workbook=str(workspace.workbook_path),
                notebook_url=_notebook_url(workspace),
                demo=demo,
            )
        )
        logger.info("open job %s finished for %s", job.job_id, workbook)
    except asyncio.CancelledError:  # pragma: no cover - server shutting down
        job.close()
        raise
    except Exception as exc:
        logger.exception("open job %s failed", job.job_id)
        job.publish(ErrorEvent(message=f"Opening {workbook.name} failed: {exc}", recoverable=False))
    finally:
        job.close()


async def open_workbook(
    state: ServerState,
    workbook: Path,
    *,
    reattach: bool = True,
    workspace: Workspace | None = None,
    plan_path: Path | None = None,
    on_event: Callable[[HubEvent], None] | None = None,
) -> OpenJob:
    """Run the open sequence to completion, off the HTTP path.

    ``kedge open`` needs exactly what the hub's Open button does — clean up after a crashed run,
    analyse, plan, scaffold, spawn marimo, bootstrap the session, attach the agent — and getting
    that order wrong is not a small mistake: scaffolding needs a live driver, so it cannot precede
    the launch. Rather than keep a second copy of the sequence in the CLI and let the two drift,
    both callers run :func:`_run_open`.

    Args:
        state: The server state the opened workspace is attached to.
        workbook: The workbook to open.
        reattach: Adopt a marimo this workspace already has running, rather than starting a
            second one. True for the CLI, where a crashed previous run is the common case.
        workspace: A pre-built workspace, for a caller that needs config overrides applied --
            ``kedge open --port`` pins marimo's port this way. Built from the workbook otherwise.
        plan_path: Adopt the plan in this file rather than the plan store's latest approved
            version -- ``kedge open --plan``. It is recorded in the store, so the notebook and
            everything that reads ``latest_approved()`` agree; the file has to be approved, and
            approvable, on its own account, because naming one is not approving it.
        on_event: Called with each progress frame as it happens, so a caller with no browser can
            still show the user what is going on.

    Returns:
        The finished :class:`OpenJob`. Its ``frames`` are the whole story; the sequence never
        raises, so inspect them to see whether a step failed.
    """
    registry = _registry(state)
    try:
        key = registry.add(workbook).key
    except (RegistryError, KedgeError) as exc:
        # The CLI caller has already named the file explicitly, so a workbook the registry will
        # not take is not a reason to refuse to open it -- it just will not appear in the hub's
        # list. The hub's own route validates before it ever gets here.
        logger.warning("not registering %s: %s", workbook, exc)
        key = ""
    job = OpenJob(job_id=uuid.uuid4().hex[:12], workbook=str(workbook))

    if on_event is not None:
        with job.subscribe() as queue:
            task = asyncio.create_task(
                _run_open(
                    state,
                    job,
                    key,
                    workbook,
                    reattach=reattach,
                    workspace=workspace,
                    plan_path=plan_path,
                )
            )
            while True:
                event = await queue.get()
                if event is None:
                    break
                on_event(event)
            await task
        return job

    await _run_open(
        state, job, key, workbook, reattach=reattach, workspace=workspace, plan_path=plan_path
    )
    return job


def _refuse_if_busy(state: ServerState, path: str | None) -> None:
    """Refuse a second, different workbook on a server that already has one open.

    One server owns one workbook and one marimo process (PLAN 2.9). Re-opening the *same* one is
    allowed and is how a reattach works.
    """
    open_workspace = state.workspace
    if open_workspace is None or path is None:
        return
    try:
        wanted = Workspace.for_workbook(path, user_directory=state.user_directory).key
    except KedgeError:  # pragma: no cover - the add below reports it properly
        return
    if wanted == open_workspace.key:
        return
    raise HTTPException(
        status_code=409,
        detail=(
            f"This kedge server already has {open_workspace.workbook_path.name} open. One server "
            f"owns one workbook and one marimo process (PLAN 2.9), so close it before opening "
            f"{Path(path).name}."
        ),
        headers={"X-Kedge-Open-Workbook": open_workspace.key},
    )


_MAX_RETAINED_JOBS = 8


def _prune_jobs(state: ServerState) -> None:
    """Forget finished jobs beyond the last few, so a long session does not accumulate them."""
    finished = [job_id for job_id, job in state.opens.items() if job.finished]
    for job_id in finished[: max(0, len(finished) - _MAX_RETAINED_JOBS)]:
        state.opens.pop(job_id, None)


def _notebook_url(workspace: Workspace) -> str | None:
    if workspace.marimo is None or workspace.marimo.session_id is None:
        return None
    return workspace.notebook_url()


# ── the steps ────────────────────────────────────────────────────────────────────────────────


async def _step_bridge(job: OpenJob) -> None:
    """Assert that the installed marimo still exposes the surface the bridge drives.

    First, and before anything is spawned. ``marimo._code_mode`` is a private API, so a marimo
    upgrade can take the notebook driver away without warning; PLAN 6.1 mitigation 5 asks for that
    to surface as one clear message naming the version rather than as a ``TypeError`` from inside
    a tool call halfway through a conversation. Until this step existed the only check on this
    path sat inside :meth:`NotebookDriver.for_workspace` at ``_step_scaffold`` -- after a marimo
    process had been started and a kernel session asserted -- where it was reported as a stepped
    over warning.

    Fatal, unlike scaffolding: a notebook kedge cannot drive is not a notebook, and opening one
    anyway would hand the user a chat pane whose every tool call is going to fail.
    :func:`check_bridge` is cached, so the driver's later call costs nothing.

    Raises:
        BridgeVersionError: If the installed marimo does not match the bridge.
    """
    from kedge.notebook.driver import verify_bridge

    job.step("bridge", "running", "checking the installed marimo against kedge's notebook bridge")
    try:
        report = await run_in_threadpool(verify_bridge)
    except KedgeError as exc:
        job.step("bridge", "failed", str(exc))
        raise
    if report.version_matches_pin:
        job.step("bridge", "ok", f"marimo {report.version} matches the verified bridge")
    else:
        job.step(
            "bridge",
            "ok",
            f"marimo {report.version} still exposes everything the bridge needs, but it was "
            f"verified against {report.pinned}",
        )


async def _step_cleanup(workspace: Workspace, job: OpenJob, *, reattach: bool) -> str | None:
    """Clear up after a crashed previous run, or adopt a live server we started.

    Reattachment consults *our* marker file only, and confirms the recorded server answers before
    adopting it. That is the whole of the "never auto-discover" rule: the token in that marker was
    generated by this machine's kedge and never left it, so a server that accepts it is ours. A
    marimo somebody else started is never a candidate, whatever notebook it has open.
    """
    from kedge.lifecycle import cleanup_orphan, health_check

    job.step("cleanup", "running", "checking for a marimo left behind by a previous run")
    marker = workspace.read_marker()

    if reattach and marker is not None:
        if await run_in_threadpool(health_check, marker.base_url):
            workspace.attach_marimo(
                host=marker.host,
                port=marker.port,
                token=marker.token,
                pid=marker.pid,
            )
            job.step("cleanup", "ok", f"reattached to our own marimo on {marker.base_url}")
            return marker.base_url
        job.step(
            "cleanup",
            "ok",
            f"the marimo we recorded on port {marker.port} is gone; starting a fresh one",
        )

    outcome = await run_in_threadpool(cleanup_orphan, workspace)
    job.step("cleanup", "ok", outcome.detail)
    return None


async def _step_analyse(workspace: Workspace, job: OpenJob) -> Any:
    """Analyse the workbook offline, and write the artifacts that come off the analysis.

    The report is written here rather than on demand so that the hub's "Report" link is real the
    first time a workbook is opened, instead of being a button that generates something. The
    contract sketch is written here for a stronger reason, in :func:`_sketch_contract`.

    The sketch rides on this step's frame rather than getting its own because ``OpenStep`` is a
    closed vocabulary shared with the client (``server/events.py``), and this is the step that
    already turns the analysis into files. Its detail line says what was written.

    **A released workbook is opened from the analysis kedge kept.** Releasing is the successful
    end of a conversion, and the hub offers Open on a released row precisely because the notebook
    is the monthly process now -- but :func:`~kedge.analysis.analyse.analyse` raises on a file
    that is not there, so without :func:`_recorded_analysis` that button was a sequence that
    always died on this step. The recorded ``analysis.json`` sits inside the project directory a
    release deliberately keeps, so there is nothing to regenerate and nothing to guess.

    The frame says the workbook is not on disk rather than that it was released, because this
    function cannot tell the two absences apart -- ``released_at`` is a registry fact and a step
    is handed a workspace. It names the released reading, which is the ordinary one, and gives the
    other its remedy in the same breath.
    """
    from kedge.analysis.analyse import analyse
    from kedge.report import write_report

    recorded = _recorded_analysis(workspace)
    if recorded is not None:
        job.step(
            "analysing",
            "ok",
            f"{workspace.workbook_path.name} is not on disk, so the analysis kedge recorded at "
            f"the last open was read back rather than the workbook: {len(recorded.sheets)} "
            f"sheet(s), {len(recorded.operations)} logical operation(s). That is the ordinary "
            f"state for a released workbook — the notebook is the process now. If the file was "
            f"moved rather than retired, put it back and open again to analyse the current "
            f"version.",
        )
        return recorded

    job.step("analysing", "running", f"reading {workspace.workbook_path.name}")
    analysis = await run_in_threadpool(analyse, workspace.workbook_path)

    workspace.analysis_path.parent.mkdir(parents=True, exist_ok=True)
    workspace.analysis_path.write_text(analysis.model_dump_json(indent=2), encoding="utf-8")
    with contextlib.suppress(OSError):
        await run_in_threadpool(write_report, analysis, report_path_for(workspace))
    contract = await run_in_threadpool(_sketch_contract, workspace, analysis)

    job.step(
        "analysing",
        "ok",
        f"{len(analysis.sheets)} sheet(s), {len(analysis.operations)} logical operation(s), "
        f"{len(analysis.findings)} finding(s) of which {len(analysis.errors)} error(s). "
        f"{contract}",
    )
    return analysis


def _recorded_analysis(workspace: Workspace) -> Any | None:
    """The analysis kedge kept, for a workbook that is no longer on disk to read.

    Deliberately narrow: it answers only for a workbook that is *absent*. A file that is there is
    always re-analysed, because ``analysis.json`` is a snapshot of a spreadsheet that changes
    every month and preferring it would be a cache that goes quietly stale.

    Returns:
        The recorded analysis, or None when the workbook is on disk (analyse it) or nothing usable
        was recorded -- in which case the caller carries on and the analyser raises, naming the
        file, which is the honest answer for a released workbook that was never analysed.
    """
    if workspace.workbook_path.is_file():
        return None

    from kedge.analysis.model import WorkbookAnalysis

    try:
        return WorkbookAnalysis.model_validate_json(
            workspace.analysis_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        logger.info("no usable recorded analysis for %s: %s", workspace.key, exc)
        return None


def _sketch_contract(workspace: Workspace, analysis: Any) -> str:
    """Draft the hand-in contract from the workbook's own pasted sheet, and say what happened.

    The bootstrapping hole this closes: a process fed by a pasted query result has no hand-in
    until the user produces one, and until there is a contract the scaffolded check cell enforces
    nothing and says so. Both ends of that wait for the other. The workbook itself breaks the
    deadlock -- the pasted sheet is last month's version of the file they are about to export --
    so the notebook opens with something to read rather than with a blank where the agreement
    should be.

    Three rules keep it honest. An existing contract is never touched, because it may have been
    hand-tuned and a sketch is the weaker description. A workbook the sketch cannot speak for --
    no pasted sheet, several of them, no header row -- is left alone, and the reason is reported
    rather than guessed past. And nothing here is fatal: a notebook that opens with no contract
    is the state every workbook was in before this existed.

    Returns:
        One sentence for the step's detail line. Never raises.
    """
    from kedge.contracts.sketch import sketch, write_sketch

    path = workspace.contract_path
    if path.exists():
        return f"The contract at {path.name} was left as it is."
    try:
        drafted = sketch(analysis)
        write_sketch(drafted, path)
    except (KedgeError, OSError, ValueError) as exc:
        logger.info("no contract sketched for %s: %s", workspace.workbook_path.name, exc)
        return f"No contract was sketched: {exc}"
    return (
        f"Sketched {path.name} from the '{drafted.sheet}' sheet "
        f"({len(drafted.contract.columns)} column(s)) so the notebook has one before the first "
        f"hand-in -- it describes a paste, not an export kedge has checked, so read it."
    )


async def _step_plan(workspace: Workspace, job: OpenJob, *, plan_path: Path | None = None) -> Any:
    """Find an approved plan, or say plainly that there is not one.

    No plan is *proposed* here. Proposing one is a model call whose output the user must review
    and approve before anything is written (PLAN 2.2), and doing that silently inside a page load
    would be exactly the gate this project exists to keep shut.

    ``plan_path`` -- what ``kedge open --plan`` names -- takes the plan from that one file and
    *adopts* it: :func:`_plan_from_file` records it in this workbook's store, so the notebook and
    everything that reads :meth:`~kedge.plan.store.PlanStore.latest_approved` agree on which
    decomposition is in force. The store's own latest approved version is never quietly substituted
    for a named file that will not load; the user asked for a particular decomposition, and
    silently scaffolding a different one is the failure mode this project exists to prevent.
    """
    if plan_path is not None:
        return await _plan_from_file(workspace, job, plan_path)

    from kedge.plan.store import PlanStore, PlanStoreError

    job.step("planning", "running", "looking for an approved process plan")
    store = PlanStore.for_workspace(workspace)
    try:
        plan = await run_in_threadpool(store.latest_approved)
        latest = await run_in_threadpool(store.latest)
    except PlanStoreError as exc:
        job.step("planning", "failed", str(exc))
        return None

    if plan is not None:
        job.step(
            "planning",
            "ok",
            f"plan v{plan.version} is approved: {len(plan.stages)} stage(s), "
            f"{plan.assessment.convertible:.0%} judged convertible",
        )
        return plan
    if latest is not None:
        job.step(
            "planning",
            "skipped",
            f"plan v{latest.version} exists but is '{latest.approval.state.value}', not approved. "
            f"The notebook stays empty until you approve it — nothing is scaffolded unreviewed.",
        )
        return None
    job.step(
        "planning",
        "skipped",
        "no process plan yet. The notebook opens empty; ask kedge in the chat to propose one, "
        "or run `kedge plan propose` on the command line.",
    )
    return None


async def _plan_from_file(workspace: Workspace, job: OpenJob, path: Path) -> Any:
    """Adopt the plan in the file ``--plan`` named: check it, record it, and scaffold from it.

    **Adopting it means writing it into the workspace's plan store**, and that is not a
    convenience. Everything else that asks "is a plan in force" reads
    :meth:`~kedge.plan.store.PlanStore.latest_approved` -- the agent's write gate
    (``agent/tools.py``), ``get_plan``, ``propose_plan``'s already-approved refusal, and the
    pinned plan block in the loop. Scaffolding from a file the store has never heard of leaves the
    notebook holding cells while the chat says no plan exists and offers to author a first one over
    the top; approving *that* files a decomposition beside a notebook implementing a different one.
    A plan the notebook implements but nothing else can see is worse than the flag being ignored.

    Naming a file is still not approving what is in it. Two conditions must hold before anything is
    written: the file says ``approved``, and :attr:`~kedge.plan.model.ProcessPlan.is_approvable`
    agrees -- anything that went through :func:`kedge.plan.review.approve` satisfies the second by
    construction, so it rejects only a hand-written file claiming an approval the review gate would
    have refused, such as one with a drop nobody acknowledged. Either way nothing is recorded and
    nothing is scaffolded; the workbook opens with an empty notebook and the frame says why.

    A file that cannot be read or will not parse is the same shape of answer for the same reason:
    this runs inside a page load as well as on a terminal, and a traceback in the opening dialog
    tells the user nothing they can act on.

    Every check here except the unapproved one is also made by :func:`kedge.cli._require_usable_plan`
    before a marimo is spawned, which is where a mistake in what the user typed belongs. They are
    not redundant: this function is what any caller that did not pre-flight reaches, and the file
    can change between the two. This is the half that must never scaffold; that one is the half
    that saves the user a workbook opened around their typo.
    """
    # Read and parsed exactly as the store reads its own versions, so a hand-edited file that will
    # not load says which field is wrong. Both raise PlanStoreError, so one except covers the pair.
    from kedge.plan.store import PlanStoreError, plan_from_yaml, read_plan_text

    job.step("planning", "running", f"reading the process plan given with --plan: {path}")
    try:
        text = await run_in_threadpool(read_plan_text, path)
        plan = plan_from_yaml(text)
    except PlanStoreError as exc:
        job.step(
            "planning", "failed", f"the plan given with --plan could not be read. {path}: {exc}"
        )
        return None

    if not plan.approval.approved:
        job.step(
            "planning",
            "skipped",
            f"plan v{plan.version} given with --plan ({path}) is "
            f"'{plan.approval.state.value}', not approved. No plan from the store was used in its "
            f"place; nothing was recorded and the notebook stays empty until this one is "
            f"approved — nothing is scaffolded unreviewed.",
        )
        return None

    blockers = plan.approval_blockers()
    if blockers:
        job.step(
            "planning",
            "failed",
            f"plan v{plan.version} given with --plan ({path}) says it is approved, but the review "
            f"gate would have refused it, so it was not adopted and nothing was scaffolded. "
            f"{len(blockers)} blocker(s): " + "; ".join(blockers),
        )
        return None

    foreign = _foreign_workbook(workspace, plan, path)
    if foreign is not None:
        job.step("planning", "failed", foreign)
        return None

    try:
        adopted, record = await run_in_threadpool(_record_named_plan, workspace, plan)
    except PlanStoreError as exc:
        job.step(
            "planning",
            "failed",
            f"the plan at {path} could not be recorded in this workbook's plan store, so nothing "
            f"was scaffolded from it — a plan the notebook implements and the store has never "
            f"heard of is worse than no plan at all: {exc}",
        )
        return None

    provenance = await run_in_threadpool(_plan_identity_note, workspace, adopted)
    job.step(
        "planning",
        "ok",
        # Deliberately not led by the version in the file: it is about to be renumbered, and
        # "plan v2 ... recorded as plan v1" reads as a mistake. The version that matters from here
        # on is the one the store now holds, which is what `record` names.
        f"the plan given with --plan ({path}) is approved by "
        f"{plan.approval.by or 'nobody named'}: {len(plan.stages)} stage(s), "
        f"{plan.assessment.convertible:.0%} judged convertible. {record}{provenance}",
    )
    return adopted


_ADOPTION_STAMPS = frozenset({"version", "based_on_version", "plan_schema_version"})
"""What :meth:`~kedge.plan.store.PlanStore.save_next` rewrites as it files a plan.

Everything else is the decomposition itself, which is what decides whether a named plan is the one
already in force or a different one arriving.
"""


def _record_named_plan(workspace: Workspace, plan: Any) -> tuple[Any, str]:
    """File a ``--plan`` file in the workspace's store, and say plainly what that did.

    Through :meth:`~kedge.plan.store.PlanStore.save_next`, so the artifact is byte-identical to one
    the review verbs write and the history is retained rather than overwritten (PLAN 2.2).

    Re-opening with the same file records nothing the second time. That is the rule
    ``kedge plan approve`` already follows for approving the plan in force: nothing has changed, and
    a version whose only difference from the one before it is a timestamp fills the history with
    entries that say nothing.

    Returns:
        The plan now in force -- the one to scaffold from -- and one sentence for the step's detail
        line. A decomposition displacing another one is never allowed to happen quietly, so that
        sentence names the version superseded.

    Raises:
        PlanStoreError: if the store cannot be written to. The caller must not scaffold then.
    """
    from kedge.plan.store import PlanStore, PlanStoreError

    store = PlanStore.for_workspace(workspace)
    unreadable = ""
    try:
        in_force = store.latest_approved()
    except PlanStoreError as exc:
        # A single unparseable version somewhere in the history must not stop the user adopting a
        # plan. It does stop us claiming to know what this supersedes, so that is what is said.
        logger.warning("could not read the plan history for %s: %s", workspace.key, exc)
        in_force, unreadable = (
            None,
            f" This workbook's saved plan history could not be read ({exc})",
        )

    if in_force is not None and _same_decomposition(in_force, plan):
        return in_force, (
            f"It is already the approved plan in force for this workbook, recorded as "
            f"v{in_force.version}; nothing new was written."
        )

    stored, written = store.save_next(plan)
    if in_force is None:
        return stored, (
            f"Adopted for this workbook and recorded as plan v{stored.version} ({written.name}), "
            f"so the chat, the agent's tools and `kedge plan show` all read the plan this notebook "
            f"was built from.{unreadable}"
        )
    return stored, (
        f"Adopted for this workbook and recorded as plan v{stored.version} ({written.name}), "
        f"superseding v{in_force.version}, which was the approved plan in force until now. That "
        f"decomposition is no longer the one this notebook implements."
    )


def _same_decomposition(one: Any, other: Any) -> bool:
    """Whether two plans differ only in where they sit in the store's numbering."""
    return one.model_dump(mode="json", exclude=_ADOPTION_STAMPS) == other.model_dump(
        mode="json", exclude=_ADOPTION_STAMPS
    )


def plan_workbook_name(plan: Any) -> str:
    """The bare filename a plan says it was written for, whatever shape it is written in.

    ``PureWindowsPath`` rather than :class:`~pathlib.Path`, and not because this is Windows: it is
    the flavour that treats both separators as separators, and a plan is a file in version control
    that is read on whichever machine has it. ``Path("C:/x/rwa.xlsx").name`` is the whole string on
    Linux, so the same plan would be accepted on the author's laptop and refused in CI.

    Returns:
        The filename, or empty when the plan does not say. An empty ``workbook`` is a hand-edited
        file that dropped the field -- everything kedge writes fills it -- and it is a distinct
        answer from a name that disagrees, not a match.
    """
    return PureWindowsPath(plan.workbook).name if plan.workbook else ""


def _foreign_workbook(workspace: Workspace, plan: Any, path: Path) -> str | None:
    """Refuse a plan written for a *different* workbook, or return None.

    Here as well as in :func:`kedge.cli._require_usable_plan`, and deliberately so. This is the
    function that writes to the store, and the guard on a durable artifact belongs beside the write
    rather than only in front of the one caller that exists today: ``OpenWorkbookBody`` has no plan
    field now, and the day it gains one the refusal would be gone with no test failing. It is the
    pattern the project already states for its own gate -- ``sync_notebook`` refuses an
    unapproved plan structurally, with no parameter that talks it out of it.

    The CLI copy is not redundant. It runs before a marimo is spawned, where the user can still act;
    this one is what stops the write when nothing pre-flighted, and what catches a file edited
    between the two.

    Returns:
        The detail line for a failed step, or None when the plan is for this workbook or says
        nothing about which workbook it is for.
    """
    named = plan_workbook_name(plan)
    name = workspace.workbook_path.name
    if not named or named.casefold() == name.casefold():
        return None
    return (
        f"the plan given with --plan ({path}) was written for {plan.workbook}, not {name}, so it "
        f"was not adopted and nothing was scaffolded: filing another workbook's decomposition in "
        f"this one's plan history would leave every stage naming ranges that are not in this file. "
        f"If it is the same process under a new filename, change the plan's `workbook:` line to "
        f"{name} and open again."
    )


def _plan_identity_note(workspace: Workspace, plan: Any) -> str:
    """How well an adopted plan matches the workbook it is now in force for, in one sentence.

    Everything here is a warning rather than a refusal, matching
    :func:`kedge.cli._warn_if_the_workbook_moved_on` and the digest half of
    :func:`kedge.cli._explicit_analysis`: a monthly file that has moved on since its plan was
    written is the normal case, not the alarming one, and it cannot produce a false "passed"
    because reconciliation degrades to "not reconciled" when the baseline does not line up. The
    case that *is* refused -- a plan for a different workbook -- never reaches here; see
    :func:`_foreign_workbook`.

    The three answers are kept apart because they are three different things, and saying the wrong
    one is worse than saying nothing. A plan with no ``workbook`` and a plan with no
    ``workbook_sha256`` used to fall through to the digest branch and report that the workbook "has
    changed since this plan was written for it", which was a claim about a comparison that had not
    happened.

    Returns:
        A sentence to append to the step's detail, or empty when there is nothing to say.
    """
    from kedge.analysis.workbook import read_identity

    name = workspace.workbook_path.name
    if not plan_workbook_name(plan):
        return (
            f" Warning: this plan does not record which workbook it was written for, so nothing in "
            f"it ties it to {name}. Fill in its `workbook:` line."
        )
    if not plan.workbook_sha256:
        return (
            f" Warning: this plan records no sha256, so kedge cannot tell whether {name} is the "
            f"file it was written for or a later version of it."
        )
    try:
        digest = read_identity(workspace.workbook_path).sha256
    except (OSError, ValueError) as exc:  # pragma: no cover - the analysis step reports this too
        logger.debug("could not read the identity of %s: %s", workspace.workbook_path, exc)
        return ""
    if digest == plan.workbook_sha256:
        return ""
    return (
        f" Warning: {name} has changed since this plan was written for it — the recorded sha256 no "
        f"longer matches the file on disk — so the decomposition describes an earlier version of "
        f"the workbook."
    )


async def _step_notebook(workspace: Workspace, job: OpenJob) -> None:
    """Make sure a notebook file exists for marimo to open.

    From :data:`~kedge.notebook.codegen.EMPTY_NOTEBOOK`, which is an app and no cells. The
    placeholder this used to write carried an unnamed ``import marimo as mo``, in the belief that
    marimo needs a cell to open a file. It does not, and that cell collided with ``kedge_setup``
    -- the first cell the scaffolder writes, which imports ``mo`` too -- so on every fresh workbook
    the notebook's whole preamble was refused for a duplicate definition and the conversion
    carried on without it. The constant lives in ``codegen`` because that module owns the notebook
    file format, and because this collision was the direct cost of the same text existing twice.
    """
    from kedge.notebook.codegen import EMPTY_NOTEBOOK

    path = workspace.notebook_path
    if path.is_file():
        job.step("notebook", "ok", f"{path.name} is already there")
        return
    job.step("notebook", "running", f"creating {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(EMPTY_NOTEBOOK, encoding="utf-8")
    job.step("notebook", "ok", f"created an empty notebook at {path}")


async def _step_launch(workspace: Workspace, job: OpenJob, *, kedge_version: str) -> None:
    """Spawn the marimo server kedge owns, and register the handlers that tear it down.

    The step also reports the assistant lockdown; :func:`_assistant_note` says why that has to
    happen where a user can see it rather than only in the log.
    """
    from kedge.lifecycle import launch_marimo, register_teardown

    job.step("launching", "running", "starting a marimo server that kedge owns")
    register_teardown(workspace)
    await run_in_threadpool(launch_marimo, workspace, kedge_version=kedge_version)
    session = workspace.require_marimo()
    note = await run_in_threadpool(_assistant_note, workspace)
    job.step("launching", "ok", f"marimo is serving {session.base_url} (pid {session.pid}){note}")


def _assistant_note(workspace: Workspace) -> str:
    """One sentence about marimo's own AI assistant, or empty when there is nothing to say.

    The control fails open on purpose. A ``.marimo.toml`` that cannot be written -- a read-only
    directory, a locked file -- leaves marimo's assistant live and the launch goes ahead anyway,
    because refusing to start would not un-write a credential already on disk and would take away
    the settings panel that is the user's only route to clearing it. Failing open **into a log
    line**, though, is a control nobody can act on: the assistant stays live, everything it sends
    goes outside kedge's tool surface and outside the outbound payload log, and the only record is
    a WARNING in a file the user has no reason to open. This is that sentence, put in front of
    them.

    A credential is reported separately and by name, never by value -- the API hands back dotted
    key names for exactly that reason -- and it is worth saying whether or not the assistant was
    disabled, because a key in that file is an exposure on its own account.

    Read fresh rather than taken from the launch. The file is live: a key typed into marimo's own
    settings panel lands in a file that was clean when the server started, and this step runs
    again on every open.

    Returns:
        A sentence to append to the launching step's detail, beginning with a space, or empty when
        the assistant is off and nothing is stored. Never raises.
    """
    from kedge import lifecycle

    lockdown = lifecycle.assistant_status(workspace)
    parts = []
    if not lockdown.enforced:
        parts.append(f"Warning: {lockdown.detail}.")
    if lockdown.secret_keys:
        parts.append(
            f"Warning: {lockdown.path.name} holds {len(lockdown.secret_keys)} model credential(s) "
            f"in plain text, under {', '.join(lockdown.secret_keys)}. kedge neither reads nor "
            f"sends them; clear them in marimo's own settings panel."
        )
    return f" {' '.join(parts)}" if parts else ""


async def _step_session(workspace: Workspace, job: OpenJob) -> None:
    """Assert the workspace's stable kernel session onto the server.

    Run on the reattach path too, and idempotent there: the session id is derived from the
    workbook path, so bootstrapping it a second time re-establishes a session the previous kedge
    process left behind rather than evicting a live one.
    """
    from kedge.lifecycle import establish_session

    job.step("session", "running", "asserting a kernel session onto the server")
    session_id = await run_in_threadpool(establish_session, workspace)
    job.step("session", "ok", f"kernel session {session_id} is live")


async def _step_scaffold(
    workspace: Workspace, plan: Any, job: OpenJob, *, plan_path: Path | None = None
) -> Any:
    """Build the notebook driver, and bring the notebook into line with an approved plan.

    Through :func:`~kedge.notebook.scaffold.sync_notebook`, because this step runs on every
    open and only the first one finds an empty notebook. The scaffolder that created every cell
    unconditionally meant the second open of a scaffolded workbook died on ``CellNameError`` at
    the first name and reported the whole plan as unscaffolded; now the cells already there are
    recognised, the ones the user has worked on are left alone, and a plan approved since the
    last open lands as the cells it was missing.

    A scaffold failure is reported and stepped over rather than fatal: the notebook, the kernel
    and the chat all still work, and telling the user "the notebook is open but scaffolding
    failed, here is why" beats refusing to open the workbook at all.

    ``plan_path`` is carried this far for one sentence. "No approved plan" is the wrong account of
    a run where the user named one and it was refused, and a step that misreports why it did
    nothing sends the reader looking in the wrong place -- the planning step above has the reason.

    The step also reports how many of the plan's cells are **still unwritten**, because a
    scaffolded notebook runs: the stage bodies are deliberate passthroughs so the hand-in
    machinery can be exercised from the moment the plan is approved, and a page that opens,
    renders and steps through looks exactly like a finished conversion. Until this counted them,
    nothing anywhere said the arithmetic had not been written yet.
    """
    from kedge.notebook.driver import NotebookDriver

    job.step("scaffolding", "running", "connecting the notebook driver")
    try:
        driver = NotebookDriver.for_workspace(workspace)
    except KedgeError as exc:
        job.step("scaffolding", "failed", f"the notebook driver could not attach: {exc}")
        return None

    if plan is None:
        job.step(
            "scaffolding",
            "skipped",
            f"the plan given with --plan ({plan_path}) was not adopted — see the planning step "
            f"above — so there is nothing to scaffold"
            if plan_path is not None
            else "no approved plan, so there is nothing to scaffold",
        )
        return driver

    from kedge.notebook.scaffold import TODO_MARKER, sync_notebook

    try:
        result = await sync_notebook(
            plan,
            driver,
            handins_dir=workspace.handins_dir,
            # Both passed explicitly rather than left to the scaffold's defaults. It derives the
            # workbook from the hand-in store's grandparent, which is only right while
            # `ingest.store_dir` is unset -- and the reconciliation cell degrades to "not
            # reconciled" when the path is wrong, which is safe but needlessly unhelpful.
            workbook_path=workspace.workbook_path,
            contract_path=workspace.contract_path,
        )
    except (KedgeError, OSError) as exc:
        job.step("scaffolding", "failed", f"scaffolding plan v{plan.version} failed: {exc}")
        return driver

    detail = result.summary(plan.version)
    total = len(result.cells)
    unwritten = await _unwritten_count(driver, {cell.name for cell in result.cells})
    if unwritten:
        detail += (
            f". {total} cells scaffolded, {unwritten} still to write — they carry a "
            f"{TODO_MARKER} marker and pass their input straight through, so the notebook runs "
            f"without doing the workbook's arithmetic. Ask kedge in the chat to write them."
        )
    elif unwritten == 0:
        detail += f". All {total} scaffolded cells have been written."
    job.step("scaffolding", "ok", detail)
    return driver


async def _unwritten_count(driver: Any, names: set[str]) -> int | None:
    """How many of ``names`` are still holes in the notebook as it stands right now.

    Read back from the notebook rather than counted off the scaffold, because this step runs on
    every open and only the first one finds an empty notebook. On a reopen the cells are already
    there and some of them have been translated since, so a count taken from what a fresh scaffold
    *would* contain reports the same six holes for ever — including on the open after the last one
    was filled. Where the two disagree the notebook wins: it is the thing the user is looking at.

    Listing with the source records a read against every cell, which is exactly what
    :func:`~kedge.notebook.scaffold.sync_notebook` did on its way in a moment ago, so nothing is
    disarmed that was armed when this step began.

    Args:
        driver: The notebook driver.
        names: The cells the plan calls for. A hole outside that set is not counted -- a stage the
            plan has dropped is reported as obsolete by :meth:`SyncResult.summary`, and it is not
            work this conversion still owes.

    Returns:
        How many still carry the scaffolder's marker, or None when the notebook could not be read.
        None is silence rather than a guess: on a step whose whole job is to say the conversion is
        unfinished, a wrong number is worse than no number.
    """
    from kedge.notebook.scaffold import is_unwritten

    try:
        cells = await driver.list_cells(with_code=True)
    except (KedgeError, OSError) as exc:
        logger.debug("could not count the cells still to write: %s", exc)
        return None
    return sum(1 for cell in cells if cell.name in names and is_unwritten(cell.code or ""))


async def _step_agent(
    state: ServerState,
    workspace: Workspace,
    analysis: Any,
    driver: Any,
    job: OpenJob,
) -> bool:
    """Attach the agent loop, falling back to the scripted stand-in when there is no model.

    A missing API key is a normal state on a first run, and it must not cost the user the notebook
    they just waited eight seconds for. The fallback is the same :class:`ScriptedAgent` the demo
    server uses, and the UI already labels that plainly as demo mode rather than pretending a
    model answered.

    The same closure that builds the loop here is handed to :meth:`ServerState.attach`, so that a
    user who then fills in the settings panel gets the real agent without reopening the workbook.
    That is the whole point of falling back rather than failing: demo mode is meant to be a state
    you can climb out of.
    """
    from kedge.server.agent_seam import AgentLoop, ScriptedAgent

    def build() -> AgentLoop:
        from kedge.agent.loop import KedgeAgent

        return KedgeAgent.for_workspace(workspace, driver=driver, analysis=analysis)

    job.step("agent", "running", "wiring up the agent loop")
    demo = False
    agent: AgentLoop
    try:
        agent = await run_in_threadpool(build)
    except (KedgeError, ImportError) as exc:
        agent = ScriptedAgent(delay=0.02)
        demo = True
        job.step(
            "agent",
            "failed",
            f"no model endpoint is usable ({exc}). Opening in demo mode: the scripted agent "
            f"answers and nothing is sent to a model. Set the endpoint and key in Settings, on "
            f"the hub, and the real agent takes over without reopening the workbook.",
        )
    else:
        job.step("agent", "ok", f"agent ready on {workspace.config.model.model}")

    state.attach(workspace, agent=agent, demo=demo, agent_factory=build)
    return demo
