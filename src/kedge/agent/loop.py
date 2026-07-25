"""The agent loop: one turn, streamed as typed events, with the notebook rebuilt every time.

This is the real implementation of :class:`kedge.server.agent_seam.AgentLoop` — a drop-in
replacement for ``ScriptedAgent``, built to the Protocol the server already states rather than to
whatever the server happened to call first. The event choreography is deliberately the same as the
fake's, because the fake is what the UI was judged against.

**The model endpoint is behind a seam of its own.** :class:`ModelClient` is a two-field protocol
that yields :class:`ChatDelta` fragments, and :class:`OpenAIClient` is the only thing in kedge that
knows what a chat completion looks like. That is not abstraction for its own sake: a loop that can
only be exercised against a live endpoint does not get exercised, so every test in
``tests/unit/test_agent_loop.py`` drives a scripted client and none of them needs a key.

**Notebook state is rebuilt from the kernel at the top of every turn** and never taken from
history. PLAN M4 calls this the single most important context rule, and the reason is mundane: the
user edits cells in the pane next to the chat, so anything the conversation remembers about the
notebook is stale the moment they do. The name registry and the live cell listing are rebuilt from
:meth:`~kedge.notebook.model.NotebookBridge.read_graph`, pinned into the window, and never evicted.

**Exactly one ``DoneEvent`` ends every turn** — success, error and the user's cancel alike. The
server persists the trail against the assistant message when it sees one, so a turn that ends
without it leaves a half-written conversation on disk, and a turn that emits two writes it twice.

Retries are capped. A cell rejected by the validation gate three times stops being a conversation
the model can win on its own, so kedge surfaces it to the user rather than burning the rest of the
budget rephrasing the same mistake.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from kedge.agent.audit import outbound_log_for
from kedge.agent.context import (
    ConversationWindow,
    NotebookState,
    TokenCounter,
    build_analysis_block,
    build_plan_block,
)
from kedge.agent.prompts import build_system_prompt
from kedge.agent.tools import ToolContext, ToolRegistry, tool_schemas
from kedge.agent.validate import MAX_VALIDATION_ATTEMPTS
from kedge.errors import KedgeError
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

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence
    from pathlib import Path

    from fastapi import FastAPI

    from kedge.analysis.model import WorkbookAnalysis
    from kedge.config import ContextConfig
    from kedge.notebook.model import NotebookBridge
    from kedge.plan.model import ProcessPlan
    from kedge.server.agent_seam import CancelToken, TurnRequest
    from kedge.workspace import Workspace

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MAX_STEPS",
    "AgentError",
    "ChatDelta",
    "KedgeAgent",
    "ModelClient",
    "OpenAIClient",
    "PendingToolCall",
    "build_agent_app",
    "serve",
]

DEFAULT_MAX_STEPS = 8
"""How many model round trips one turn may take before kedge stops and says so."""

_EDITING_TOOLS = frozenset({"propose_cell", "edit_cell", "delete_cell"})
_RUNNING_TOOLS = frozenset({"run_cell", "probe", "reconcile"})


class AgentError(KedgeError):
    """The agent loop could not be built or could not reach its model endpoint."""


# ── the model seam ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ChatDelta:
    """One fragment of a streamed completion: prose, or a piece of a tool call.

    Tool calls arrive spread across many chunks — the id and the function name once, the arguments
    a few characters at a time — so ``index`` is what stitches them back together.

    Example:
        >>> ChatDelta(text="the haircut lookup ").text
        'the haircut lookup '
    """

    text: str = ""
    index: int = 0
    call_id: str | None = None
    name: str | None = None
    arguments: str = ""


@dataclass(frozen=True, slots=True)
class PendingToolCall:
    """A tool call reassembled from its fragments."""

    id: str
    name: str
    arguments: str

    def to_message(self) -> dict[str, Any]:
        """The shape a chat-completions assistant message carries."""
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


class ModelClient(Protocol):
    """What the loop needs from a model endpoint.

    Narrow on purpose. Everything the OpenAI SDK offers beyond "stream me deltas for these
    messages and these tools" is either configuration, which lives in
    :class:`~kedge.config.ModelConfig`, or a feature this loop does not use.
    """

    def stream(
        self,
        *,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> AsyncIterator[ChatDelta]:
        """Yield the completion's fragments as they arrive."""
        ...


class OpenAIClient:
    """A :class:`ModelClient` over an OpenAI-compatible chat-completions endpoint.

    The only place in kedge that knows the wire format. The SDK is imported inside ``__init__``
    rather than at module scope so that importing :mod:`kedge.agent` — which the CLI does — costs
    nothing until somebody actually wants to talk to a model.

    Example:
        >>> OpenAIClient.__name__
        'OpenAIClient'
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: float = 120.0,
        max_retries: int = 2,
    ) -> None:
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(
            base_url=base_url, api_key=api_key, timeout=timeout, max_retries=max_retries
        )

    @classmethod
    def from_workspace(cls, workspace: Workspace) -> OpenAIClient:
        """Build a client from config, fetching the key from the OS keyring.

        Raises:
            MissingApiKeyError: The configured keyring entry does not resolve. The message names
                the entry and the exact command that creates it.
        """
        from kedge.config import get_api_key

        model = workspace.config.model
        return cls(
            base_url=model.base_url,
            api_key=get_api_key(workspace.config),
            timeout=model.timeout_seconds,
            max_retries=model.max_retries,
        )

    async def stream(
        self,
        *,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> AsyncIterator[ChatDelta]:
        """Stream one completion, translating SDK chunks into :class:`ChatDelta`."""
        payload: dict[str, Any] = {
            "model": model,
            "messages": [dict(message) for message in messages],
            "stream": True,
        }
        if tools:
            payload["tools"] = [dict(tool) for tool in tools]
            payload["tool_choice"] = "auto"

        stream = await self._client.chat.completions.create(**payload)
        async for chunk in stream:
            for choice in chunk.choices or ():
                delta = choice.delta
                if delta is None:
                    continue
                if delta.content:
                    yield ChatDelta(text=delta.content)
                for call in delta.tool_calls or ():
                    function = call.function
                    yield ChatDelta(
                        index=call.index or 0,
                        call_id=call.id,
                        name=function.name if function is not None else None,
                        arguments=(function.arguments or "") if function is not None else "",
                    )

    async def aclose(self) -> None:
        """Release the underlying HTTP client."""
        await self._client.close()


# ── bookkeeping ──────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _Reply:
    """One model turn's output, reassembled from its fragments."""

    content: str = ""
    calls: list[PendingToolCall] = field(default_factory=list)


@dataclass(slots=True)
class _Meter:
    """Approximate token accounting for one turn, reported on the ``DoneEvent``."""

    prompt: int = 0
    output: int = 0

    @property
    def total(self) -> int:
        """Prompt plus output."""
        return self.prompt + self.output


# ── the loop ─────────────────────────────────────────────────────────────────────────────────


class KedgeAgent:
    """The tool-calling loop that drives one workbook conversion.

    Satisfies :class:`kedge.server.agent_seam.AgentLoop`, so the server can be handed one of these
    exactly where it is currently handed a ``ScriptedAgent``.

    One instance serves every session on the server; per-session state — the tool registry, its
    outbound audit log, and the compaction digest — is keyed by session id, because the audit log
    is per session by design (PLAN 2.3) and a shared one would be useless for tracing.

    Example:
        >>> from kedge.server.agent_seam import AgentLoop
        >>> class _Silent:
        ...     async def stream(self, **_kwargs):
        ...         return
        ...         yield  # pragma: no cover
        >>> isinstance(KedgeAgent(client=_Silent(), context=ToolContext()), AgentLoop)
        True
    """

    def __init__(
        self,
        *,
        client: ModelClient,
        context: ToolContext,
        model: str = "gpt-4o",
        context_config: ContextConfig | None = None,
        system_prompt: str | None = None,
        counter: TokenCounter | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        analysis: WorkbookAnalysis | None = None,
    ) -> None:
        self._client = client
        self._context = context
        self._model = model
        self._config = context_config
        self._system = system_prompt or build_system_prompt()
        self._counter = counter or TokenCounter(
            encoding=context_config.tokeniser if context_config else "cl100k_base"
        )
        self._max_steps = max(1, max_steps)
        self._analysis = analysis if analysis is not None else context.analysis
        self._tools = tool_schemas()
        self.registries: dict[str, ToolRegistry] = {}
        self._digests: dict[str, str] = {}

    # ── construction ─────────────────────────────────────────────────────────────────────

    @classmethod
    def for_workspace(
        cls,
        workspace: Workspace,
        *,
        driver: NotebookBridge | None = None,
        analysis: WorkbookAnalysis | None = None,
        client: ModelClient | None = None,
        system_prompt: str | None = None,
    ) -> KedgeAgent:
        """Build a loop for one workbook, reading everything else from the workspace.

        Args:
            workspace: Owns the config, the paths and the marimo session.
            driver: The live notebook bridge. ``None`` leaves the notebook tools degrading to a
                message saying no kernel is attached, which is the honest state before marimo is up.
            analysis: The workbook analysis. ``None`` is loaded from ``analysis.json`` if it is
                there.
            client: The model endpoint. ``None`` builds an :class:`OpenAIClient` from config and
                the OS keyring.
            system_prompt: Overrides the assembled prompt. For tests and for experiments.

        Returns:
            A ready loop.
        """
        resolved_analysis = analysis if analysis is not None else _load_analysis(workspace)
        context = ToolContext.for_workspace(workspace, analysis=resolved_analysis, driver=driver)
        return cls(
            client=client or OpenAIClient.from_workspace(workspace),
            context=context,
            model=workspace.config.model.model,
            context_config=workspace.config.context,
            system_prompt=system_prompt,
            analysis=resolved_analysis,
        )

    # ── the Protocol ─────────────────────────────────────────────────────────────────────

    async def run(self, request: TurnRequest, *, cancel: CancelToken) -> AsyncIterator[AnyEvent]:
        """Run one turn, yielding events as they happen.

        Ends with exactly one :class:`~kedge.server.events.DoneEvent` on every path that this loop
        owns. A :class:`asyncio.CancelledError` that did *not* come from the user's cancel token is
        re-raised untouched — that is the task being torn down, and converting it into a tidy
        finish would be lying about what happened.
        """
        meter = _Meter()
        try:
            async for event in self._turn(request, cancel, meter):
                if isinstance(event, TokenEvent):
                    meter.output += self._counter.count(event.text)
                yield event
        except asyncio.CancelledError:
            if not cancel.cancelled:
                raise
            logger.info("turn %s stopped at the user's request", request.turn_id)
            yield ErrorEvent(message="Turn cancelled at your request.", recoverable=True)
        except KedgeError as exc:
            logger.warning("turn %s failed: %s", request.turn_id, exc)
            yield ErrorEvent(message=str(exc), recoverable=True)
        except Exception as exc:
            logger.exception("turn %s raised", request.turn_id)
            yield ErrorEvent(
                message=(
                    f"The turn stopped unexpectedly: {type(exc).__name__}: {exc}. The notebook has "
                    f"not been left half-changed — every notebook edit is a single atomic flush."
                ),
                recoverable=False,
            )
        yield DoneEvent(turn_id=request.turn_id, tokens_used=meter.total)

    # ── one turn ─────────────────────────────────────────────────────────────────────────

    async def _turn(
        self, request: TurnRequest, cancel: CancelToken, meter: _Meter
    ) -> AsyncIterator[AnyEvent]:
        yield StatusEvent(phase="analysing")
        cancel.raise_if_cancelled()

        tools = self._registry_for(request.session_id)
        state = await self._notebook_state()
        tools.refresh(state)
        window = self._window_for(request, state)
        attempts: dict[str, int] = {}

        for step in range(self._max_steps):
            cancel.raise_if_cancelled()
            yield StatusEvent(phase="thinking")

            reply = _Reply()
            messages = window.assemble()
            meter.prompt = sum(self._counter.count_message(message) for message in messages)
            async for event in self._complete(messages, request.model, cancel, reply):
                yield event

            window.add_assistant(
                reply.content, tool_calls=[call.to_message() for call in reply.calls]
            )
            if not reply.calls:
                logger.debug("turn %s finished after %d step(s)", request.turn_id, step + 1)
                break

            stop = False
            for call in reply.calls:
                cancel.raise_if_cancelled()
                async for event in self._invoke(call, tools, window, request, attempts):
                    if isinstance(event, ErrorEvent):
                        stop = True
                    yield event
            if stop:
                self._remember(request.session_id, window)
                return
        else:
            yield ErrorEvent(
                message=(
                    f"I used all {self._max_steps} steps this turn without reaching an answer, so "
                    f"I have stopped rather than keep going. Tell me which part to focus on and I "
                    f"will pick it up from there."
                ),
                recoverable=True,
            )

        self._remember(request.session_id, window)

    async def _complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        model: str | None,
        cancel: CancelToken,
        reply: _Reply,
    ) -> AsyncIterator[AnyEvent]:
        """Stream one completion, emitting prose as it arrives and collecting tool calls."""
        parts: dict[int, dict[str, str]] = {}
        async for delta in self._client.stream(
            model=model or self._model, messages=messages, tools=self._tools
        ):
            cancel.raise_if_cancelled()
            if delta.text:
                reply.content += delta.text
                yield TokenEvent(text=delta.text)
            if delta.call_id is None and delta.name is None and not delta.arguments:
                continue
            slot = parts.setdefault(delta.index, {"id": "", "name": "", "arguments": ""})
            if delta.call_id:
                slot["id"] = delta.call_id
            if delta.name:
                slot["name"] = delta.name
            slot["arguments"] += delta.arguments

        reply.calls = [
            PendingToolCall(
                id=slot["id"] or f"call_{index}",
                name=slot["name"],
                arguments=slot["arguments"],
            )
            for index, slot in sorted(parts.items())
            if slot["name"]
        ]

    async def _invoke(
        self,
        call: PendingToolCall,
        tools: ToolRegistry,
        window: ConversationWindow,
        request: TurnRequest,
        attempts: dict[str, int],
    ) -> AsyncIterator[AnyEvent]:
        """Run one tool call and emit everything the UI needs to narrate it."""
        arguments = _safe_arguments(call.arguments)
        if call.name in _EDITING_TOOLS:
            yield StatusEvent(phase="editing")
        elif call.name in _RUNNING_TOOLS:
            yield StatusEvent(phase="running")
        yield ToolCallEvent.summarising(call.name, arguments)

        result = await tools.dispatch(call.name, arguments, turn_id=request.turn_id)

        if result.validated is not None:
            yield ValidationEvent(ok=result.validated, violations=result.violations)
        yield ToolResultEvent(name=call.name, ok=result.ok, summary=result.summary)

        if result.cell_id:
            if call.name == "propose_cell" and result.ok:
                yield CellCreatedEvent(
                    cell_id=result.cell_id,
                    name=result.cell_name or call.name,
                    preview=result.cell_preview,
                )
            if result.cell_ran:
                yield CellRunningEvent(cell_id=result.cell_id)
                yield CellResultEvent(
                    cell_id=result.cell_id,
                    ok=result.cell_ok is not False,
                    error=result.cell_error,
                )

        window.add_tool_result(tool_call_id=call.id, name=call.name, content=result.text)

        if result.validated is False:
            key = str(arguments.get("name") or arguments.get("cell") or call.name)
            attempts[key] = attempts.get(key, 0) + 1
            if attempts[key] >= MAX_VALIDATION_ATTEMPTS:
                logger.info(
                    "stopping turn %s: '%s' failed validation %d times",
                    request.turn_id,
                    key,
                    attempts[key],
                )
                yield ErrorEvent(
                    message=(
                        f"'{key}' did not pass validation {attempts[key]} times running, so I have "
                        f"stopped rather than keep guessing. The violations are above — they are "
                        f"the actual reason, not a summary of it."
                    ),
                    recoverable=True,
                )

    # ── context ──────────────────────────────────────────────────────────────────────────

    async def _notebook_state(self) -> NotebookState:
        """Read the notebook from the kernel. Never from history — history goes stale."""
        driver = self._context.driver
        if driver is None:
            return NotebookState()
        try:
            return NotebookState.from_graph(await driver.read_graph())
        except KedgeError as exc:
            logger.warning("could not read the notebook graph (%s); falling back to a listing", exc)
        try:
            return NotebookState.from_cells(await driver.list_cells(with_code=False))
        except KedgeError as exc:
            logger.warning("could not list the notebook's cells either (%s)", exc)
            return NotebookState()

    def _window_for(self, request: TurnRequest, state: NotebookState) -> ConversationWindow:
        config = self._config
        budget = 128_000
        evict_after = 6
        if config is not None:
            budget = max(1, config.max_context_tokens - config.reserve_output_tokens)
            evict_after = config.evict_tool_results_after_turns
        window = ConversationWindow(
            system=self._system,
            budget=budget,
            counter=self._counter,
            evict_tool_results_after_turns=evict_after,
        )
        window.set_pinned(
            [
                state.registry.render(),
                state.render(),
                build_analysis_block(self._analysis),
                build_plan_block(self._plan()),
            ]
        )
        window.set_digest(self._digests.get(request.session_id, ""))
        window.load_history((message.role, message.content) for message in request.history)
        window.begin_turn()
        window.add_user(request.message)
        return window

    def _plan(self) -> ProcessPlan | None:
        store = self._context.plans
        if store is None:
            return None
        try:
            return store.latest_approved() or store.latest()
        except KedgeError as exc:
            logger.warning("could not read the plan store: %s", exc)
            return None

    def _registry_for(self, session_id: str) -> ToolRegistry:
        registry = self.registries.get(session_id)
        if registry is None:
            workspace = self._context.workspace
            log = outbound_log_for(workspace, session_id) if workspace is not None else None
            registry = ToolRegistry(self._context, log=log)
            self.registries[session_id] = registry
        return registry

    def _remember(self, session_id: str, window: ConversationWindow) -> None:
        """Compact the conversation to a digest once it approaches the budget (PLAN M4)."""
        threshold = (self._config.compaction_threshold if self._config else 0.85) * window.budget
        if window.token_total() > threshold:
            self._digests[session_id] = window.compact()
            logger.info("compacted session %s to a digest", session_id)

    def reset_session(self, session_id: str) -> None:
        """Forget a session's digest and its tool registry. The ``/new`` path."""
        self._digests.pop(session_id, None)
        self.registries.pop(session_id, None)

    async def aclose(self) -> None:
        """Release every per-session workbook handle."""
        for registry in list(self.registries.values()):
            await registry.aclose()

    def __repr__(self) -> str:
        return f"KedgeAgent(model={self._model!r}, sessions={len(self.registries)})"


def _safe_arguments(raw: str) -> dict[str, Any]:
    """Decode a tool call's arguments for the activity trail, tolerating a truncated stream."""
    try:
        decoded = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"arguments": raw}
    return decoded if isinstance(decoded, dict) else {"arguments": decoded}


def _load_analysis(workspace: Workspace) -> WorkbookAnalysis | None:
    """Load ``analysis.json`` if ``kedge inspect`` has been run, else ``None``."""
    path = workspace.analysis_path
    if not path.is_file():
        logger.info("no analysis at %s; the model will be told to run `kedge inspect`", path)
        return None
    from kedge.analysis.model import WorkbookAnalysis

    try:
        return WorkbookAnalysis.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("could not load the analysis at %s: %s", path, exc)
        return None


# ── running the real server ──────────────────────────────────────────────────────────────────


def build_agent_app(
    workspace: Workspace,
    *,
    driver: NotebookBridge | None = None,
    analysis: WorkbookAnalysis | None = None,
    client: ModelClient | None = None,
    version: str = "0.1.0",
) -> FastAPI:
    """Build the kedge server driven by this loop rather than by ``ScriptedAgent``.

    The import of :mod:`kedge.server.app` is function-local on purpose. The layering is
    ``analysis -> plan -> notebook -> agent -> server``, and a module-level import here would
    invert it; deferring it keeps ``kedge.agent`` importable by the CLI with no FastAPI in the way.

    Args:
        workspace: The workbook's workspace, with directories already ensured.
        driver: A live notebook bridge, where marimo is up.
        analysis: The workbook analysis; loaded from disk when omitted.
        client: The model endpoint; built from config and the keyring when omitted.
        version: Reported by ``/api/context``.

    Returns:
        The application, ready for :func:`kedge.server.app.run_server`.
    """
    from kedge.server.app import create_app

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

        uv run python -c "from kedge.agent import serve; serve('book.xlsx', port=8731)"

    Requires an API key in the OS keyring for ``model.api_key_ref``. Without a live marimo the
    notebook tools degrade to saying so; everything that reads the workbook still works.
    """
    from kedge.server.app import run_server
    from kedge.workspace import Workspace as _Workspace

    workspace = _Workspace.for_workbook(workbook)
    workspace.ensure_dirs()
    app = build_agent_app(workspace, driver=driver)
    logger.warning("kedge on http://%s:%d against %s", host, port, workspace.workbook_path)
    run_server(app, host=host, port=port, log_level=log_level)
