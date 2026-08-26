"""The wire format for kedge's events, the hub's own vocabulary, and the bus that fans them out.

This module used to hold two unrelated things. The first was the vocabulary of a *turn* -- the
eleven typed events an agent emits while running one -- which the agent produces and the server
merely consumes; that now lives in :mod:`kedge.turn`, below both layers, and is re-exported here so
that every import site, every ``isinstance`` check and every discriminated-union parse keeps
referring to the same objects. Importing :class:`~kedge.turn.DoneEvent` from here and from
:mod:`kedge.turn` gets you one class, not two.

What is left is genuinely the server's:

* the SSE wire format (:func:`encode_sse`, :func:`sse_comment`) -- one frame per event, ``event:
  <type>`` naming the variant and ``data:`` carrying the model's JSON;
* the hub's own progress vocabulary for opening a workbook, which shares the encoder with a turn's
  events and nothing else;
* the notebook mirror -- which of a turn's events are worth pushing into the marimo pane, and what
  they should say there;
* :class:`EventBus`, because a turn's events have more than one consumer. The SSE response is the
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
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from kedge.turn import (
    MAX_ARGS_SUMMARY_CHARS,
    AnyEvent,
    BaseEvent,
    CellCreatedEvent,
    CellResultEvent,
    CellRunningEvent,
    DoneEvent,
    ErrorEvent,
    Event,
    PausedEvent,
    Phase,
    StatusEvent,
    TokenEvent,
    ToolCallEvent,
    ToolResultEvent,
    ValidationEvent,
    parse_event,
    summarise_args,
)

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
    "PausedEvent",
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


class OpenProgressEvent(BaseEvent):
    """One step of opening a workbook changed state."""

    type: Literal["open_progress"] = "open_progress"
    step: OpenStep
    state: StepState
    detail: str = ""


class OpenReadyEvent(BaseEvent):
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
    cell, a failed run, a rejected validation, an error, a turn that has paused for an answer, and
    the end of a turn are all things a user watching the notebook pane needs to see without
    looking away.
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
    if isinstance(event, PausedEvent):
        return NotebookNotification(
            title="kedge is waiting for you",
            description=event.message,
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
