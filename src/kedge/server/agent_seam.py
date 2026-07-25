"""The seam between the server and ``kedge.agent.loop``, and a scripted stand-in for it.

The agent loop (PLAN M4) is a separate piece of work. Rather than wait for it, the server states
the interface it needs as a :class:`Protocol` and ships a fake that satisfies it, so the whole UI
can be built, exercised and judged with no model endpoint and no marimo process. The real loop
then builds to the Protocol rather than to whatever the server happened to call first.

The interface is deliberately narrow. The server hands over a :class:`TurnRequest` — one user
message, the conversation so far, and an optional model override — and receives an async iterator
of typed events. Everything else the loop needs (config, the analysis, the marimo session, the
notebook driver) it gets from the :class:`~kedge.workspace.Workspace` it was constructed with;
none of it belongs in a per-turn request, and none of it belongs to the server.

Cancellation is cooperative, via :class:`CancelToken`. There is no way to interrupt a blocking
call from outside it, so the loop is expected to check the token between steps and, in
particular, around anything that takes time: model calls, cell runs, tool dispatch. The server
also closes the iterator when the client disconnects, which covers the "user shut the tab" case.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from kedge.server.events import (
    AnyEvent,
    CellCreatedEvent,
    CellResultEvent,
    CellRunningEvent,
    DoneEvent,
    ErrorEvent,
    StatusEvent,
    TokenEvent,
    ToolCallEvent,
    ToolResultEvent,
    ValidationEvent,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AgentLoop",
    "AgentUnavailableError",
    "CancelToken",
    "ScriptedAgent",
    "TurnMessage",
    "TurnRegistry",
    "TurnRequest",
]


class AgentUnavailableError(RuntimeError):
    """No agent loop is wired up, and the server was not started in demo mode."""


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


@runtime_checkable
class AgentLoop(Protocol):
    """What the server requires of ``kedge.agent.loop``.

    Implemented by an object constructed with a :class:`~kedge.workspace.Workspace`, so that
    config, the workbook analysis, the approved plan and the notebook driver are all reachable
    without the server knowing about any of them.

    ``run`` is an async generator function: calling it returns the iterator without awaiting,
    and each yielded event is streamed to the browser as it arrives. Implementations should:

    * emit a :class:`~kedge.server.events.StatusEvent` before any slow step, so the UI can say
      what is happening rather than showing a spinner;
    * emit :class:`~kedge.server.events.TokenEvent` fragments as prose arrives from the model,
      not in one block at the end;
    * build :class:`~kedge.server.events.ToolCallEvent` via ``ToolCallEvent.summarising``, which
      keeps raw arguments out of the stream by construction;
    * check ``cancel.cancelled`` between steps and finish promptly when it is set;
    * finish with exactly one :class:`~kedge.server.events.DoneEvent`, including after an error.

    The server publishes everything yielded to the event bus, mirrors the notebook-relevant
    events into the notebook pane, and persists the trail against the assistant message, so an
    implementation only has to yield.
    """

    def run(self, request: TurnRequest, *, cancel: CancelToken) -> AsyncIterator[AnyEvent]:
        """Run one turn, yielding events as they happen."""
        ...


@dataclass
class TurnRegistry:
    """The turns currently in flight, so the UI can cancel one by id."""

    _tokens: dict[str, CancelToken] = field(default_factory=dict)

    def start(self, turn_id: str) -> CancelToken:
        """Register a turn and return its cancellation token."""
        token = CancelToken()
        self._tokens[turn_id] = token
        return token

    def cancel(self, turn_id: str) -> bool:
        """Ask a turn to stop. Returns whether that turn was in flight."""
        token = self._tokens.get(turn_id)
        if token is None:
            return False
        token.cancel()
        logger.info("cancellation requested for turn %s", turn_id)
        return True

    def finish(self, turn_id: str) -> None:
        """Forget a turn that has ended."""
        self._tokens.pop(turn_id, None)

    @property
    def active(self) -> tuple[str, ...]:
        """The ids of every turn currently in flight."""
        return tuple(self._tokens)


# ── the scripted stand-in ────────────────────────────────────────────────────────────────────

_PROSE_OPENING = (
    "I have read the analysis for this workbook. The haircut lookup on `Calc!H2:H50000` is an "
    "exact `VLOOKUP`, which is a left join, and the R1C1 compression collapses it to a single "
    "logical operation over 49,999 rows.\n\n"
)

_PROSE_PLAN = (
    "### Proposed change\n\n"
    "I will add one cell, `apply_haircuts`, between `load_handin` and `reconcile`. It stays lazy "
    "so the query plan and the cell graph line up, and the division is wrapped because Excel "
    "returns `#DIV/0!` where polars would quietly produce `inf`.\n\n"
)

_PROSE_CODE = (
    "```python\n"
    "apply_haircuts = (\n"
    "    load_handin\n"
    '    .join(reference_haircuts, left_on="asset_class", how="left")\n'
    "    .with_columns(\n"
    '        pl.col("exposure")\n'
    '        .xl.mul(pl.col("haircut"))\n'
    "        .xl.round(2)\n"
    '        .alias("haircut_exposure")\n'
    "    )\n"
    ")\n"
    "```\n\n"
)

_PROSE_CLOSE = (
    "Reconciliation is clean at the configured tolerance, so this region is signed off. The "
    "next unconverted stage is `manual_overrides`, which the plan marks as a **checkpoint** — "
    "fourteen values typed by the analyst each month, with no formula behind them. I will not "
    "invent logic for it; it wants an approval cell instead.\n"
)

_CELL_PREVIEW = (
    "apply_haircuts = (\n"
    "    load_handin\n"
    '    .join(reference_haircuts, left_on="asset_class", how="left")'
)


class ScriptedAgent:
    """An :class:`AgentLoop` that acts out a realistic turn without a model or a kernel.

    Its purpose is to make the UI judgeable before the real loop exists: every event type in
    PLAN M3 appears, in a plausible order, at a plausible pace, so the activity trail, the
    streamed markdown, the code rendering and the cancel path can all be exercised end to end.

    A message containing "fail" or "error" takes the failure branch, which is the only way to see
    what a rejected validation and a failed cell actually look like.

    Example:
        >>> import asyncio
        >>> async def main() -> str:
        ...     agent = ScriptedAgent(delay=0.0)
        ...     request = TurnRequest(turn_id="t1", session_id="s1", message="hello")
        ...     events = [event async for event in agent.run(request, cancel=CancelToken())]
        ...     return events[-1].type
        >>> asyncio.run(main())
        'done'
    """

    def __init__(self, *, delay: float = 0.05) -> None:
        self._delay = delay

    async def run(self, request: TurnRequest, *, cancel: CancelToken) -> AsyncIterator[AnyEvent]:
        """Act out one turn."""
        failing = any(word in request.message.lower() for word in ("fail", "error", "broken"))
        tokens = 0
        try:
            async for event in self._script(request, cancel, failing=failing):
                if isinstance(event, TokenEvent):
                    tokens += max(1, len(event.text) // 4)
                yield event
        except asyncio.CancelledError:
            # Only swallow our own cooperative stop. A genuine task cancellation is not ours to
            # convert into a tidy finish.
            if not cancel.cancelled:
                raise
            yield ErrorEvent(message="Turn cancelled at your request.", recoverable=True)
            yield DoneEvent(turn_id=request.turn_id, tokens_used=tokens)
            return
        yield DoneEvent(turn_id=request.turn_id, tokens_used=tokens + 480)

    async def _script(
        self,
        request: TurnRequest,
        cancel: CancelToken,
        *,
        failing: bool,
    ) -> AsyncIterator[AnyEvent]:
        await self._beat(cancel, 2)
        yield StatusEvent(phase="analysing")

        async for event in self._stream_prose(_PROSE_OPENING, cancel):
            yield event

        await self._beat(cancel)
        yield ToolCallEvent.summarising("list_cells", {})
        await self._beat(cancel, 3)
        yield ToolResultEvent(
            name="list_cells",
            ok=True,
            summary="6 cells: handin_source, handin, handin_check, load_handin, "
            "reference_haircuts, reconcile",
        )

        yield StatusEvent(phase="thinking")
        async for event in self._stream_prose(_PROSE_PLAN, cancel):
            yield event

        yield StatusEvent(phase="editing")
        await self._beat(cancel)
        # Note the argument summary: the cell body is 214 characters and does not appear.
        yield ToolCallEvent.summarising(
            "propose_cell",
            {"name": "apply_haircuts", "after": "load_handin", "code": "x" * 214},
        )
        await self._beat(cancel, 3)

        if failing:
            yield ValidationEvent(
                ok=False,
                violations=(
                    "apply_haircuts redefines 'exposure', already defined in cell 'MJUe' "
                    "(load_handin) — marimo requires exactly one owning cell per public name",
                    "bare .round(2) on a value derived from an Excel ROUND region; use "
                    "col(...).xl.round(2) so the half-away-from-zero intent is greppable",
                ),
            )
            yield ToolResultEvent(
                name="propose_cell", ok=False, summary="rejected by the validation gate (2)"
            )
            yield ErrorEvent(
                message="The proposed cell did not pass validation twice running, so I have "
                "stopped rather than keep guessing. The violations are above.",
                recoverable=True,
            )
            return

        yield ValidationEvent(ok=True)
        yield ToolResultEvent(name="propose_cell", ok=True, summary="accepted; 1 cell queued")
        await self._beat(cancel)
        yield CellCreatedEvent(cell_id="UMIT", name="apply_haircuts", preview=_CELL_PREVIEW)

        yield StatusEvent(phase="running")
        yield CellRunningEvent(cell_id="UMIT")
        await self._beat(cancel, 6)
        yield CellResultEvent(cell_id="UMIT", ok=True)

        await self._beat(cancel)
        yield ToolCallEvent.summarising(
            "reconcile", {"variable": "apply_haircuts", "region": "Calc!H2:H50000"}
        )
        await self._beat(cancel, 4)
        yield ToolResultEvent(
            name="reconcile",
            ok=True,
            summary="49,999 rows compared, 0 differing (abs 1e-06, rel 1e-09); worst delta 4.5e-09",
        )

        yield StatusEvent(phase="thinking")
        async for event in self._stream_prose(_PROSE_CODE + _PROSE_CLOSE, cancel):
            yield event

    async def _stream_prose(self, text: str, cancel: CancelToken) -> AsyncIterator[AnyEvent]:
        """Yield ``text`` a word at a time, the way a model streams it."""
        for word in text.split(" "):
            cancel.raise_if_cancelled()
            if self._delay:
                await asyncio.sleep(self._delay / 4)
            yield TokenEvent(text=word + " ")

    async def _beat(self, cancel: CancelToken, units: int = 1) -> None:
        cancel.raise_if_cancelled()
        if self._delay:
            await asyncio.sleep(self._delay * units)
        cancel.raise_if_cancelled()
