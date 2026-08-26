"""One turn, as both sides see it: what the server hands in, and what the agent yields back.

This is the vocabulary of a single conversational turn -- the eleven typed events an agent emits
while it runs one, plus the three things the caller hands it: the request, the prior messages, and
the cooperative stop signal.

**Why it is here rather than under `agent/` or `server/`.** The layering is ``analysis/ -> plan/ ->
notebook/ -> agent/ -> server/``, and this vocabulary has two consumers on opposite sides of one of
those arrows: :mod:`kedge.agent.loop` produces the events and reads the request, and
:mod:`kedge.server` serialises them to SSE, mirrors them into the notebook pane and persists them
against a message. `CLAUDE.md` states the house answer for exactly that shape in the same paragraph
as the ladder -- "``reconcile/`` is consumed by the CLI, the notebook and the agent, so it sits
below all three" -- and this sits below both for the same reason.

Putting it under `agent/` would have been legal (the server is above the agent and may import it
freely) and it would still have cost something real: ``kedge/agent/__init__.py`` eagerly aggregates
the whole package, so ``from kedge.agent.events import ...`` in `server/events.py` executes
:class:`~kedge.agent.loop.KedgeAgent`'s module and everything under it. That is precisely what
:mod:`kedge.server.agent_seam` exists to avoid: the server can run its scripted stand-in, or
``--demo``, with no agent in the process at all. The guarantee is stated as a membership rather
than as a duration -- after ``import kedge.server`` there is no ``kedge.agent`` in ``sys.modules``,
and after ``import kedge.agent`` there is no ``kedge.server`` and no ``fastapi`` -- because a
number written into a docstring is stale on the next machine and cannot fail.
``tests/unit/test_layering.py`` asserts both, in their own interpreters.

**The events.** "The user is not sat there wondering what is happening" is a stated requirement,
and PLAN M3 is explicit that an afterthought spinner will not satisfy it. So progress is a
*vocabulary* rather than a boolean: eleven event types covering prose, tool activity, cell
operations, validation, pausing and completion, each carrying enough detail for the UI to say which
tool ran, which cell was created, and what validation actually said. Every one is a pydantic model
with a literal ``type``, gathered into a discriminated union so that serialising and parsing back
are both total and typed. The SSE wire format that carries them is the server's business and lives
in :mod:`kedge.server.events`.

One event carries a deliberate data-handling decision. ``tool_call`` reports an *args summary*,
never the raw arguments -- a ``propose_cell`` call carries a whole cell body and a ``read_range``
call carries a workbook range, and neither belongs in a UI trail or in the log that trail is
reconstructed from. That is enforced structurally rather than by asking callers to remember:
:class:`ToolCallEvent` forbids extra fields (so an ``args=`` keyword is a validation error, not a
silently accepted one), rejects a summary that is multi-line, over-long, or shaped like serialised
JSON, and offers :meth:`ToolCallEvent.summarising` as the blessed constructor which derives a
compliant summary from the arguments itself.

**The request.** The server hands over a :class:`TurnRequest` -- one user message, the conversation
so far, and an optional model override -- and nothing else. Everything else the loop needs (config,
the analysis, the marimo session, the notebook driver) it gets from the
:class:`~kedge.workspace.Workspace` it was constructed with; none of it belongs in a per-turn
request, and none of it belongs to the server. Notebook state is deliberately absent too: PLAN M4
is emphatic that the loop must rebuild it from the live kernel every turn rather than trusting
anything carried in history, because the user edits cells directly between turns.

**The stop signal.** Cancellation is cooperative, via :class:`CancelToken`. There is no way to
interrupt a blocking call from outside it, so the loop is expected to check the token between steps
and, in particular, around anything that takes time: model calls, cell runs, tool dispatch. The
server also closes the iterator when the client disconnects, which covers the "user shut the tab"
case.

The *interface* between the two sides -- :class:`kedge.server.agent_seam.AgentLoop` -- deliberately
stays with the server. See that module for why.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

__all__ = [
    "MAX_ARGS_SUMMARY_CHARS",
    "AnyEvent",
    "BaseEvent",
    "CancelToken",
    "CellCreatedEvent",
    "CellResultEvent",
    "CellRunningEvent",
    "DoneEvent",
    "ErrorEvent",
    "Event",
    "PausedEvent",
    "Phase",
    "StatusEvent",
    "TokenEvent",
    "ToolCallEvent",
    "ToolResultEvent",
    "TurnMessage",
    "TurnRequest",
    "ValidationEvent",
    "parse_event",
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


class BaseEvent(BaseModel):
    """Shared configuration for every event kedge streams, turn or hub.

    ``extra="forbid"`` is load-bearing rather than tidiness: it is what makes an accidental
    ``ToolCallEvent(name=..., args={...})`` a validation error instead of a field nobody notices
    until the raw arguments turn up in a log.

    The one thing in this module that is not turn-scoped, and deliberately so. The hub's
    ``open_progress`` and ``open_ready`` frames are emphatically *not* part of a turn -- they are
    kept out of :data:`AnyEvent` so that one arriving in a turn's accumulator is a validation error
    -- but they travel the same wire, through the same encoder, under the same two rules. Declaring
    that configuration twice would let the halves drift, and the half that drifts is the one where
    ``extra="forbid"`` quietly stops being set. So the base class is shared and the *union* is not,
    which is the distinction that matters.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class StatusEvent(BaseEvent):
    """The turn moved into a new coarse phase."""

    type: Literal["status"] = "status"
    phase: Phase


class TokenEvent(BaseEvent):
    """A fragment of streamed assistant prose."""

    type: Literal["token"] = "token"
    text: str


class ToolCallEvent(BaseEvent):
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


class ToolResultEvent(BaseEvent):
    """A tool finished, with a short human-readable account of what it produced."""

    type: Literal["tool_result"] = "tool_result"
    name: str
    ok: bool
    summary: str = ""


class CellCreatedEvent(BaseEvent):
    """A notebook cell was created, with the opening of its body for context."""

    type: Literal["cell_created"] = "cell_created"
    cell_id: str
    name: str
    preview: str = ""


class CellRunningEvent(BaseEvent):
    """A notebook cell started executing."""

    type: Literal["cell_running"] = "cell_running"
    cell_id: str


class CellResultEvent(BaseEvent):
    """A notebook cell finished executing, successfully or not."""

    type: Literal["cell_result"] = "cell_result"
    cell_id: str
    ok: bool
    error: str | None = None


class ValidationEvent(BaseEvent):
    """The pre-commit validation gate ran (PLAN M4), listing whatever it rejected."""

    type: Literal["validation"] = "validation"
    ok: bool
    violations: tuple[str, ...] = ()


class PausedEvent(BaseEvent):
    """The turn used its step budget and is asking whether to carry on.

    Deliberately not an :class:`ErrorEvent`. Nothing went wrong, nothing was lost, and the loop
    holds the turn's tool traffic so the next message resumes with everything it had learnt. A
    turn that ends this way is waiting for a word, not reporting a failure, and the UI should say
    so — telling a user their work has hit a problem when it has not is how people stop trusting
    the trail.

    ``steps`` is how many model round trips were spent getting here.
    """

    type: Literal["paused"] = "paused"
    message: str
    steps: int = 0


class DoneEvent(BaseEvent):
    """The turn finished. Always the last event of a turn."""

    type: Literal["done"] = "done"
    turn_id: str
    tokens_used: int = 0


class ErrorEvent(BaseEvent):
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
    | PausedEvent
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


# ── what the caller hands in ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TurnMessage:
    """One prior message, as the loop sees it when assembling context."""

    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class TurnRequest:
    """Everything the loop is told about one turn.

    Notebook state is deliberately absent. PLAN M4 is emphatic that the loop must rebuild it
    from the live kernel every turn rather than trusting anything carried in history, because the
    user edits cells directly between turns and history goes stale immediately.
    """

    turn_id: str
    session_id: str
    message: str
    model: str | None = None
    history: tuple[TurnMessage, ...] = ()


class CancelToken:
    """A cooperative stop signal for one turn.

    Example:
        >>> token = CancelToken()
        >>> token.cancelled
        False
        >>> token.cancel()
        >>> token.cancelled
        True
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def cancelled(self) -> bool:
        """Whether the user has asked for this turn to stop."""
        return self._event.is_set()

    def cancel(self) -> None:
        """Ask for the turn to stop at the next opportunity."""
        self._event.set()

    async def wait(self) -> None:
        """Block until cancellation is requested, for racing against a pending call."""
        await self._event.wait()

    def raise_if_cancelled(self) -> None:
        """Raise :class:`asyncio.CancelledError` if cancellation has been requested."""
        if self.cancelled:
            raise asyncio.CancelledError
