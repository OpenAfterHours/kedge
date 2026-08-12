"""The typed event vocabulary the server streams, and the bus that fans it out.

"The user is not sat there wondering what is happening" is a stated requirement, and PLAN M3 is
explicit that an afterthought spinner will not satisfy it. So progress is a *vocabulary* rather
than a boolean: ten event types covering prose, tool activity, cell operations, validation and
completion, each carrying enough detail for the UI to say which tool ran, which cell was created,
and what validation actually said.

Every event is a pydantic model with a literal ``type``, gathered into a discriminated union so
that serialising to SSE and parsing back are both total and typed. The wire form is one SSE
frame per event: ``event: <type>`` naming the variant, ``data:`` carrying the model's JSON.

One event carries a deliberate data-handling decision. ``tool_call`` reports an *args summary*,
never the raw arguments — a ``propose_cell`` call carries a whole cell body and a ``read_range``
call carries a workbook range, and neither belongs in a UI trail or in the log that trail is
reconstructed from. That is enforced structurally rather than by asking callers to remember:
:class:`ToolCallEvent` forbids extra fields (so an ``args=`` keyword is a validation error, not a
silently accepted one), rejects a summary that is multi-line, over-long, or shaped like
serialised JSON, and offers :meth:`ToolCallEvent.summarising` as the blessed constructor which
derives a compliant summary from the arguments itself.

The bus exists because a turn's events have more than one consumer. The SSE response is the
obvious one; the notebook mirror (PLAN M3: "mirror the important ones into the notebook itself")
and any attached monitor stream are the others. Publishing is fan-out to bounded per-subscriber
queues plus a list of observers, and neither a slow subscriber nor a raising observer is allowed
to disturb the turn that produced the event.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_ARGS_SUMMARY_CHARS",
    "OPEN_STEPS",
    "AnyEvent",
    "CellCreatedEvent",
    "CellResultEvent",
    "CellRunningEvent",
    "DoneEvent",
    "ErrorEvent",
    "Event",
    "EventBus",
    "EventObserver",
    "HubEvent",
    "NotebookNotification",
    "NotebookNotifier",
    "OpenProgressEvent",
    "OpenReadyEvent",
    "OpenStep",
    "Phase",
    "StatusEvent",
    "StepState",
    "TokenEvent",
    "ToolCallEvent",
    "ToolResultEvent",
    "ValidationEvent",
    "encode_sse",
    "notebook_mirror",
    "notification_for",
    "parse_event",
    "sse_comment",
    "summarise_args",
]

Phase = Literal["analysing", "thinking", "editing", "running"]
"""The coarse phase of a turn, shown as a live chip at the head of the assistant message."""

MAX_ARGS_SUMMARY_CHARS = 200
"""Ceiling on ``tool_call.args_summary``. A summary that needs more than this is not a summary."""

MAX_SCALAR_CHARS = 60
"""Longest argument value reproduced verbatim; anything longer is described by size instead."""


# ── argument summarising ─────────────────────────────────────────────────────────────────────


def _describe(value: object) -> str:
    """Describe one argument value: short scalars verbatim, everything else by shape."""
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str):
        collapsed = " ".join(value.split())
        if len(collapsed) <= MAX_SCALAR_CHARS and len(collapsed) == len(value.strip()):
            return collapsed
        return f"<{len(value)} chars>"
    if isinstance(value, Mapping):
        return f"<{len(value)} keys>"
    if isinstance(value, list | tuple | set | frozenset):
        return f"<{len(value)} items>"
    return f"<{type(value).__name__}>"


def summarise_args(args: Mapping[str, object]) -> str:
    """Return a one-line, size-capped summary of a tool call's arguments.

    Short scalars survive intact, because "cell=load_handin" is exactly the specific detail the
    activity trail exists to show. Anything long or structured collapses to a shape descriptor
    such as ``<214 chars>``, so a cell body or a sampled frame cannot ride out inside a summary.

    Args:
        args: The arguments as the tool dispatcher received them.

    Returns:
        A single line of at most :data:`MAX_ARGS_SUMMARY_CHARS` characters.
    """
    summary = ", ".join(f"{key}={_describe(value)}" for key, value in args.items())
    if len(summary) > MAX_ARGS_SUMMARY_CHARS:
        summary = summary[: MAX_ARGS_SUMMARY_CHARS - 1].rstrip() + "…"
    return summary


# ── event models ─────────────────────────────────────────────────────────────────────────────


class _BaseEvent(BaseModel):
    """Shared configuration for every event.

    ``extra="forbid"`` is load-bearing rather than tidiness: it is what makes an accidental
    ``ToolCallEvent(name=..., args={...})`` a validation error instead of a field nobody notices
    until the raw arguments turn up in a log.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class StatusEvent(_BaseEvent):
    """The turn moved into a new coarse phase."""

    type: Literal["status"] = "status"
    phase: Phase


class TokenEvent(_BaseEvent):
    """A fragment of streamed assistant prose."""

    type: Literal["token"] = "token"
    text: str


class ToolCallEvent(_BaseEvent):
    """A tool is about to run, described by name and a summary of its arguments.

    The raw arguments never appear here. Build one with :meth:`summarising` and the summary is
    derived for you; build one by hand and the summary is validated on the way in.

    Example:
        >>> ToolCallEvent.summarising("propose_cell", {"name": "load_handin", "code": "x = 1"})
        ToolCallEvent(type='tool_call', name='propose_cell', args_summary='name=load_handin, code=x = 1')
    """

    type: Literal["tool_call"] = "tool_call"
    name: str
    args_summary: str = ""

    @field_validator("args_summary")
    @classmethod
    def _reject_raw_arguments(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            msg = (
                "args_summary must be a single line; it looked like a dump of the raw arguments. "
                "Build the event with ToolCallEvent.summarising(name, args) instead."
            )
            raise ValueError(msg)
        if len(value) > MAX_ARGS_SUMMARY_CHARS:
            msg = (
                f"args_summary must be at most {MAX_ARGS_SUMMARY_CHARS} characters, got "
                f"{len(value)}. Build the event with ToolCallEvent.summarising(name, args) "
                f"instead of passing the arguments through."
            )
            raise ValueError(msg)
        if value.lstrip().startswith(("{", "[")):
            msg = (
                "args_summary looks like serialised arguments rather than a summary. Build the "
                "event with ToolCallEvent.summarising(name, args) instead."
            )
            raise ValueError(msg)
        return value

    @classmethod
    def summarising(cls, name: str, args: Mapping[str, object] | None = None) -> ToolCallEvent:
        """Return an event for ``name``, summarising ``args`` rather than carrying them."""
        return cls(name=name, args_summary=summarise_args(args or {}))


class ToolResultEvent(_BaseEvent):
    """A tool finished, with a short human-readable account of what it produced."""

    type: Literal["tool_result"] = "tool_result"
    name: str
    ok: bool
    summary: str = ""


class CellCreatedEvent(_BaseEvent):
    """A notebook cell was created, with the opening of its body for context."""

    type: Literal["cell_created"] = "cell_created"
    cell_id: str
    name: str
    preview: str = ""


class CellRunningEvent(_BaseEvent):
    """A notebook cell started executing."""

    type: Literal["cell_running"] = "cell_running"
    cell_id: str


class CellResultEvent(_BaseEvent):
    """A notebook cell finished executing, successfully or not."""

    type: Literal["cell_result"] = "cell_result"
    cell_id: str
    ok: bool
    error: str | None = None


class ValidationEvent(_BaseEvent):
    """The pre-commit validation gate ran (PLAN M4), listing whatever it rejected."""

    type: Literal["validation"] = "validation"
    ok: bool
    violations: tuple[str, ...] = ()


class DoneEvent(_BaseEvent):
    """The turn finished. Always the last event of a turn."""

    type: Literal["done"] = "done"
    turn_id: str
    tokens_used: int = 0


class ErrorEvent(_BaseEvent):
    """Something went wrong. ``recoverable`` says whether the session can carry on."""

    type: Literal["error"] = "error"
    message: str
    recoverable: bool = True


AnyEvent = (
    StatusEvent
    | TokenEvent
    | ToolCallEvent
    | ToolResultEvent
    | CellCreatedEvent
    | CellRunningEvent
    | CellResultEvent
    | ValidationEvent
    | DoneEvent
    | ErrorEvent
)

Event = Annotated[AnyEvent, Field(discriminator="type")]
"""The discriminated union of every event a *turn* streams."""

_ADAPTER: TypeAdapter[AnyEvent] = TypeAdapter(Event)


def parse_event(raw: Mapping[str, Any] | str | bytes) -> AnyEvent:
    """Rebuild an event from its serialised form, dispatching on ``type``."""
    if isinstance(raw, str | bytes):
        return _ADAPTER.validate_json(raw)
    return _ADAPTER.validate_python(raw)


# ── opening a workbook ───────────────────────────────────────────────────────────────────────
#
# Opening a workbook from the hub takes several seconds and does five distinguishable things, so
# it gets the same treatment a turn does rather than a spinner (PLAN M3). These are deliberately
# *not* folded into AnyEvent: that union is the agent's turn vocabulary, and a hub progress frame
# arriving in a turn's accumulator would be a bug, not a feature. They share the wire format and
# the encoder, and nothing else.

OpenStep = Literal[
    "bridge",
    "cleanup",
    "analysing",
    "planning",
    "notebook",
    "launching",
    "session",
    "scaffolding",
    "agent",
]
"""The stages of opening a workbook, in the order :mod:`kedge.server.hub` runs them."""

OPEN_STEPS: tuple[OpenStep, ...] = (
    "bridge",
    "cleanup",
    "analysing",
    "planning",
    "notebook",
    "launching",
    "session",
    "scaffolding",
    "agent",
)
"""The steps a client should draw up front, so the checklist is visible before it starts."""

StepState = Literal["running", "ok", "skipped", "failed"]
"""How one step ended. ``skipped`` is a first-class outcome: a workbook with no approved plan
genuinely has nothing to scaffold, and saying so is more useful than a silent gap."""


class OpenProgressEvent(_BaseEvent):
    """One step of opening a workbook changed state."""

    type: Literal["open_progress"] = "open_progress"
    step: OpenStep
    state: StepState
    detail: str = ""


class OpenReadyEvent(_BaseEvent):
    """The workbook is open and attached; the browser can move to the chat view."""

    type: Literal["open_ready"] = "open_ready"
    key: str
    workbook: str
    notebook_url: str | None = None
    demo: bool = False


HubEvent = OpenProgressEvent | OpenReadyEvent | ErrorEvent
"""What the hub's open stream carries. Shares :func:`encode_sse` with the turn vocabulary."""


# ── SSE wire format ──────────────────────────────────────────────────────────────────────────


def encode_sse(event: AnyEvent | HubEvent) -> str:
    """Return the SSE frame for ``event``.

    The event's ``type`` becomes the SSE event name so a client can dispatch without parsing the
    body, and the body is the model's JSON. ``model_dump_json`` escapes newlines inside strings,
    so the payload is always a single ``data:`` line and no rejoining is needed on the far side.
    """
    return f"event: {event.type}\ndata: {event.model_dump_json()}\n\n"


def sse_comment(text: str = "kedge") -> str:
    """Return an SSE comment frame, used as a preamble and as a keep-alive.

    Comments are ignored by clients but travel the whole path, which both flushes any buffering
    proxy sitting in the way and proves the stream is open before the first real event.
    """
    return f": {text}\n\n"


# ── the notebook mirror ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class NotebookNotification:
    """A message to push into the marimo notebook UI.

    Deliberately not a marimo type. ``marimo._code_mode`` and the notification classes that go
    with it are ``notebook/driver.py``'s exclusive territory (CONVENTIONS non-negotiable 2), so
    the server describes what it wants said and the driver decides how to say it.
    """

    title: str
    description: str
    variant: Literal["danger"] | None = None


class NotebookNotifier(Protocol):
    """What the server needs from the notebook driver to mirror events into the notebook.

    Implemented in ``kedge.notebook.driver``, which is the only module permitted to touch
    ``broadcast_raw_notification``. May be synchronous or return an awaitable; the bus handles
    both. Implementations must not raise — a notebook that has gone away is a degraded
    experience, not a failed turn.
    """

    def notify(self, notification: NotebookNotification) -> Awaitable[None] | None:
        """Show ``notification`` in the notebook pane."""
        ...


def notification_for(event: AnyEvent) -> NotebookNotification | None:
    """Return the notebook notification for ``event``, or ``None`` if it is not worth mirroring.

    Only the events that answer "did something happen to my notebook?" are mirrored. Streamed
    prose and tool chatter belong in the chat pane and would be noise in the notebook; a created
    cell, a failed run, a rejected validation, an error, and the end of a turn are all things a
    user watching the notebook pane needs to see without looking away.
    """
    if isinstance(event, CellCreatedEvent):
        return NotebookNotification(
            title="kedge created a cell",
            description=f"'{event.name}' was added and is ready to run.",
        )
    if isinstance(event, CellResultEvent) and not event.ok:
        return NotebookNotification(
            title="Cell failed",
            description=event.error or f"Cell {event.cell_id} did not run cleanly.",
            variant="danger",
        )
    if isinstance(event, ValidationEvent) and not event.ok:
        count = len(event.violations)
        first = event.violations[0] if event.violations else "see the chat pane for detail"
        return NotebookNotification(
            title=f"Validation rejected the change ({count})",
            description=first,
            variant="danger",
        )
    if isinstance(event, ErrorEvent):
        return NotebookNotification(
            title="kedge hit a problem",
            description=event.message,
            variant="danger",
        )
    if isinstance(event, DoneEvent):
        return NotebookNotification(
            title="kedge finished this turn",
            description="The chat pane has the detail.",
        )
    return None


def notebook_mirror(notifier: NotebookNotifier) -> EventObserver:
    """Return a bus observer that mirrors the important events into the notebook."""

    async def observe(event: AnyEvent) -> None:
        notification = notification_for(event)
        if notification is None:
            return
        result = notifier.notify(notification)
        if inspect.isawaitable(result):
            await result

    return observe


# ── the bus ──────────────────────────────────────────────────────────────────────────────────

EventObserver = Callable[[AnyEvent], Awaitable[None] | None]
"""A side-channel consumer of every published event."""


class EventBus:
    """Fans every published event out to subscribers and observers.

    Subscribers are pull-based and bounded: each holds its own queue, and a subscriber that
    stops draining loses events rather than stalling the turn that is producing them. A browser
    tab that has been suspended must not be able to block the agent loop.

    Observers are push-based and are called inline — the notebook mirror and anything else that
    wants to see the stream without holding a queue. An observer that raises is logged and
    skipped; nothing an observer does can break the turn.

    Example:
        >>> import asyncio
        >>> async def main() -> str:
        ...     bus = EventBus()
        ...     seen: list[str] = []
        ...     bus.add_observer(lambda event: seen.append(event.type))
        ...     await bus.publish(StatusEvent(phase="thinking"))
        ...     return seen[0]
        >>> asyncio.run(main())
        'status'
    """

    def __init__(self, *, queue_size: int = 256) -> None:
        self._queue_size = queue_size
        self._subscribers: set[asyncio.Queue[AnyEvent]] = set()
        self._observers: list[EventObserver] = []
        self._dropped = 0

    @property
    def subscriber_count(self) -> int:
        """How many streams are currently attached."""
        return len(self._subscribers)

    @property
    def dropped(self) -> int:
        """How many events were discarded because a subscriber was not keeping up."""
        return self._dropped

    def add_observer(self, observer: EventObserver) -> Callable[[], None]:
        """Register ``observer`` and return a callable that removes it again."""
        self._observers.append(observer)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self._observers.remove(observer)

        return remove

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[AsyncIterator[AnyEvent]]:
        """Attach a subscriber for the duration of the block, yielding an async iterator."""
        queue: asyncio.Queue[AnyEvent] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        try:
            yield self._drain(queue)
        finally:
            self._subscribers.discard(queue)

    async def _drain(self, queue: asyncio.Queue[AnyEvent]) -> AsyncIterator[AnyEvent]:
        while True:
            yield await queue.get()

    async def publish(self, event: AnyEvent) -> None:
        """Deliver ``event`` to every subscriber and observer."""
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self._dropped += 1
                logger.warning("an event stream is not keeping up; dropped a %s event", event.type)
        for observer in list(self._observers):
            try:
                result = observer(event)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("an event observer failed on a %s event", event.type)
