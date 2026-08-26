"""The agent loop: one turn, streamed as typed events, with the notebook rebuilt every time.

This is the real implementation of :class:`kedge.server.agent_seam.AgentLoop` — a drop-in
replacement for ``ScriptedAgent``, built to the Protocol the server already states rather than to
whatever the server happened to call first. The event choreography is deliberately the same as the
fake's, because the fake is what the UI was judged against.

**The model endpoint is behind a seam of its own.** :class:`ModelClient` is a two-field protocol
that yields :class:`ChatDelta` fragments, and :class:`OpenAIClient` is the only thing in kedge that
knows what a request on the wire looks like. That is not abstraction for its own sake: a loop that
can only be exercised against a live endpoint does not get exercised, so every test in
``tests/unit/test_agent_loop.py`` drives a scripted client and none of them needs a key.

That seam is also what lets kedge speak two dialects without the loop knowing. Everything above
:class:`OpenAIClient` — the window, the audit log, the stored transcript — is chat-completions
shaped, because that is the lingua franca and the shape the conversation is kept in.
:class:`OpenAIClient` prefers the responses API on the way out, since it is the only one that
carries a reasoning model's thinking across a tool call, and falls back on its own when the
endpoint has never heard of it.

**Notebook state is rebuilt from the kernel at the top of every turn** and never taken from
history. PLAN M4 calls this the single most important context rule, and the reason is mundane: the
user edits cells in the pane next to the chat, so anything the conversation remembers about the
notebook is stale the moment they do. The name registry and the live cell listing are rebuilt from
:meth:`~kedge.notebook.model.NotebookBridge.read_graph`, pinned into the window, and never evicted.

**Exactly one ``DoneEvent`` ends every turn** — success, error and the user's cancel alike. The
server persists the trail against the assistant message when it sees one, so a turn that ends
without it leaves a half-written conversation on disk, and a turn that emits two writes it twice.

Retries are capped, and a capped turn still says something. A cell rejected by the validation gate
three times stops being a conversation the model can win on its own, so kedge surfaces it to the
user rather than burning the rest of the budget rephrasing the same mistake — and then spends one
last completion with the tools withheld (:meth:`KedgeAgent._final_word`). Stopping on its own left
a user two full turns of workbook reading, a warning chip and no prose whatsoever: the account the
model had worked out on the way to the failure is the part they can act on, and it was being thrown
away at exactly the moment it became the only thing left to hand them.

**Every turn keeps what it learnt, whether or not it answered.** Only prose survives in the
server's history, so without a hand-off a model that had just spent forty calls reading a workbook
comes back to the next message knowing nothing about it — and spends the forty again. That is as
true of the turn that succeeded as of the one that ran out of steps, and it was the more expensive
failure of the two: a user who asks for a summary and then asks for the notebook was paying twice
for one investigation. So :meth:`KedgeAgent._carry` holds the turn's tool traffic against the
session on every path out of the step loop, and :meth:`KedgeAgent._window_for` seeds it into the
next turn.

What is carried is not always carried *whole*. A turn that stopped early — the step budget, a
Stop, the validation cap — is resumed as itself, so the leg it just ran is still true and survives
entire. A turn that answered is a turn the conversation has moved past, and every leg older than
the current one has lived through a turn boundary whichever way the turn ended; for those, only
results that cannot have gone stale keep their content. :func:`kedge.agent.tools.volatility_of`
decides, and everything below :attr:`~kedge.agent.tools.Volatility.SESSION_STABLE` is reduced to
the stub that names the call and invites the model to make it again. The messages themselves stay
either way, because an assistant message whose tool results went missing is rejected by the
endpoint outright.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from kedge.agent.audit import outbound_log_for
from kedge.agent.context import (
    ContextMessage,
    ConversationWindow,
    NotebookState,
    TokenCounter,
    build_analysis_block,
    build_plan_block,
)
from kedge.agent.prompts import build_system_prompt
from kedge.agent.tools import ToolContext, ToolRegistry, Volatility, tool_schemas, volatility_of
from kedge.agent.validate import MAX_VALIDATION_ATTEMPTS
from kedge.errors import KedgeError
from kedge.notebook.codegen import read_notebook
from kedge.server.events import (
    AnyEvent,
    CellCreatedEvent,
    CellResultEvent,
    CellRunningEvent,
    DoneEvent,
    ErrorEvent,
    PausedEvent,
    StatusEvent,
    TokenEvent,
    ToolCallEvent,
    ToolResultEvent,
    ValidationEvent,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Mapping, Sequence
    from pathlib import Path

    from fastapi import FastAPI

    from kedge.agent.tools import ToolResult
    from kedge.analysis.model import WorkbookAnalysis
    from kedge.config import ContextConfig
    from kedge.notebook.model import NotebookBridge
    from kedge.plan.model import ProcessPlan
    from kedge.server.agent_seam import CancelToken, TurnRequest
    from kedge.workspace import Workspace

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MAX_STEPS",
    "MAX_DRAFT_ATTEMPTS",
    "AgentError",
    "ChatDelta",
    "KedgeAgent",
    "ModelClient",
    "OpenAIClient",
    "PendingToolCall",
    "build_agent_app",
    "chat_deltas",
    "responses_delta",
    "responses_input",
    "responses_tools",
    "serve",
]

DEFAULT_MAX_STEPS = 50
"""How many model round trips one turn takes before kedge pauses and asks whether to carry on.

A check-in rather than a wall, which is what makes it safe to set this high. A turn that reaches
it keeps its tool traffic (:meth:`KedgeAgent._carry`) and the next message resumes it, so the
budget costs a question rather than the work. Real conversions run long: eight steps was enough to
read a sheet and propose one cell, and not enough to finish anything. Configurable as
``[agent] max_steps``.
"""

MAX_DRAFT_ATTEMPTS = MAX_VALIDATION_ATTEMPTS
"""How many times one tool may reject the model's own draft before the turn stops.

Three, matching the cell path, and for the same reason: the fourth attempt at a shape the model
has got wrong three times is not the one that works. Without a cap the turn spends the whole step
budget on it — up to :data:`DEFAULT_MAX_STEPS` completions, each re-sending the entire prompt —
and ends in a pause that reads as though progress were being made. The cap ends it while the
useful part is still worth having: the model's account of the workbook, in prose, which the user
can act on whether or not it ever fitted the schema.

That account is not a hope, it is a step. :meth:`KedgeAgent._final_word` is what actually collects
it, because a cap that only stops the turn ends the conversation before the model has been given a
completion in which to write anything down.
"""

_FINAL_WORD_PROMPT = (
    "kedge has stopped this turn: the same thing has been rejected three times running, so the "
    "tools have been withdrawn and this reply is your last word on it. Answer the user directly, "
    "in prose. Say what you worked out about the process stage by stage, what each stage is for, "
    "how far you had got, and what you are unsure of or would need from them. That account is the "
    "useful part — it is what they can act on, whether or not what you drafted ever fitted the "
    "schema — so write it out rather than apologising for the failure or promising to retry. "
    "There is no further step this turn in which a retry could happen."
)
"""What the model is told when the loop has stopped it and wants prose out of it anyway.

Appended to the assembled messages for one request rather than added to the window, and the
distinction is not tidiness. A window that holds it carries it into the next turn — where it is
false, since that turn has its whole budget — and a *user* message that holds it is counted by
:func:`_history_before` as a turn to trim, which would silently cut the wrong exchange out of
history. So it is spoken once, to one request, and never stored.

It lives here rather than in :mod:`kedge.agent.prompts` because it is a property of this
control-flow decision rather than of the standing system prompt: nothing else in kedge sends it,
and it is sent at most once per turn.
"""

_NO_ACCOUNT = (
    "[I have not been able to write that up. Nothing I read on this turn has been thrown away, so "
    "ask me to summarise what I found and I will pick it up from where it stopped.]"
)
"""What the user is told when the last word comes back empty, or does not come back at all.

Silence is the whole of the defect this path exists to remove, and the account can still go missing
three ways: the completion fails, the endpoint answers with tool calls and no prose, or it answers
with nothing whatever. None of those is worth a second error chip — the turn has already explained
itself — but all three are worth a line, and the line says the one thing the user needs, which is
that the reading is still on the shelf (:meth:`KedgeAgent._carry`) and asking again is cheap.

Not emitted when the user pressed Stop. They asked for silence, and they are entitled to it.

Bracketed, because in this channel that is what says kedge is speaking rather than the model. It is
the rule :func:`_cut_short` follows for the same reason, and a rule that holds for one of the two
lines is not a rule at all: a reader has to be able to tell whose words they are without being told.

Prose alone, with no chip of its own — and :func:`_pause_message` is precedent for only half of
that. It emits a :class:`~kedge.server.events.PausedEvent` *and* records its prose in the window,
so a chip plainly need not cost the window record. What it is precedent for is the record, which is
the half that matters here: the next turn has to read this as the last thing the assistant said, or
"summarise what you found" answers nothing. The chip is left out because it would want a new event
type, which is a change to the server's event model and to ``app.js`` rather than to this file. The
brackets carry the authorship until somebody makes that change.
"""

_EDITING_TOOLS = frozenset({"propose_cell", "edit_cell", "delete_cell"})
_RUNNING_TOOLS = frozenset({"run_cell", "probe", "reconcile"})


class AgentError(KedgeError):
    """The agent loop could not be built or could not reach its model endpoint."""


# ── the model seam ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Usage:
    """What one completion actually cost, as the endpoint counted it.

    kedge's own :class:`~kedge.agent.context.TokenCounter` is an estimate over a fixed encoding,
    which is the wrong one for most current models and cannot see the thing that matters most on a
    50-step turn: how much of the prompt was served from the endpoint's cache. So the endpoint's
    own numbers are preferred wherever it volunteers them.

    Example:
        >>> Usage(prompt=8_000, completion=120, cached=7_800).total
        8120
    """

    prompt: int = 0
    completion: int = 0
    cached: int = 0
    """Prompt tokens the endpoint served from cache. Part of ``prompt``, not additional to it."""

    @property
    def total(self) -> int:
        """Prompt plus completion, cache or no cache."""
        return self.prompt + self.completion


@dataclass(frozen=True, slots=True)
class ChatDelta:
    """One fragment of a streamed completion: prose, a piece of a tool call, or the usage report.

    Tool calls arrive spread across many chunks — the id and the function name once, the arguments
    a few characters at a time — so ``index`` is what stitches them back together.

    A delta carrying ``usage`` carries nothing else: both dialects report the count once, at the
    end, in an envelope of its own.

    Example:
        >>> ChatDelta(text="the haircut lookup ").text
        'the haircut lookup '
    """

    text: str = ""
    index: int = 0
    call_id: str | None = None
    name: str | None = None
    arguments: str = ""
    usage: Usage | None = None


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


def responses_input(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Translate chat-completions messages into responses-API input items.

    The loop, the context window and the audit log all speak chat completions, and they should
    keep speaking it: it is the shape the conversation is *stored* in, and it is the one every
    other endpoint understands. Only the wire needs the other dialect, so the translation lives
    here rather than spreading a second message format back through :mod:`kedge.agent.context`.

    Three shapes differ. A tool result stops being a message with a role and becomes a
    ``function_call_output`` keyed by ``call_id``; an assistant turn that made tool calls becomes
    its prose followed by one ``function_call`` item per call, because responses models a turn as
    a list of output items rather than one message with a list hanging off it; everything else is
    a role and content, unchanged.

    Example:
        >>> responses_input([{"role": "user", "content": "hello"}])
        [{'role': 'user', 'content': 'hello'}]
    """
    items: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.get("tool_call_id") or "",
                    "output": message.get("content") or "",
                }
            )
            continue
        if role == "assistant":
            if message.get("content"):
                items.append({"role": "assistant", "content": message["content"]})
            for call in message.get("tool_calls") or ():
                function = call.get("function") or {}
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.get("id") or "",
                        "name": function.get("name") or "",
                        "arguments": function.get("arguments") or "",
                    }
                )
            continue
        items.append({"role": role, "content": message.get("content") or ""})
    return items


def responses_tools(tools: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Flatten chat-completions tool definitions into the responses-API shape.

    Chat completions nests the schema under ``function``; responses puts it at the top level.

    ``strict`` is sent explicitly false. Left unsaid it can default true, and strict mode demands
    that every property be required — kedge's tools have genuinely optional arguments, so strict
    would reject schemas that are correct (:meth:`kedge.agent.tools.ToolSpec.schema`).

    Example:
        >>> responses_tools([{"type": "function", "function": {"name": "probe"}}])[0]["name"]
        'probe'
    """
    flattened: list[dict[str, Any]] = []
    for tool in tools:
        function = tool.get("function") or {}
        flattened.append(
            {
                "type": "function",
                "name": function.get("name") or "",
                "description": function.get("description") or "",
                "parameters": function.get("parameters") or {},
                "strict": False,
            }
        )
    return flattened


def responses_delta(event: Any) -> ChatDelta | None:
    """Translate one streamed responses event into a :class:`ChatDelta`, or ``None`` to skip it.

    Responses streams typed, semantic events where chat completions streams anonymous deltas, so
    the fragments arrive already labelled: the call id and name once, on the item that opens, and
    the arguments as their own event stream afterwards. ``output_index`` is what ties them
    together, and it is what :class:`ChatDelta.index` carries.

    Read through ``getattr`` rather than by attribute, because the set of event types grows with
    the SDK and an unrecognised one must be ignored rather than raise.
    """
    kind = getattr(event, "type", "")
    if kind == "response.output_text.delta":
        return ChatDelta(text=getattr(event, "delta", "") or "")
    if kind == "response.function_call_arguments.delta":
        return ChatDelta(
            index=getattr(event, "output_index", 0) or 0,
            arguments=getattr(event, "delta", "") or "",
        )
    if kind == "response.output_item.added":
        item = getattr(event, "item", None)
        if getattr(item, "type", "") == "function_call":
            return ChatDelta(
                index=getattr(event, "output_index", 0) or 0,
                call_id=getattr(item, "call_id", None),
                name=getattr(item, "name", None),
            )
    if kind in ("response.completed", "response.incomplete"):
        # Incomplete as well as completed: a turn cut off by a length limit still cost what it
        # cost, and dropping its numbers would silently understate the expensive turns.
        usage = _usage_of(getattr(event, "response", None))
        return None if usage is None else ChatDelta(usage=usage)
    return None


def _usage_of(reported: Any) -> Usage | None:
    """Read a usage report off either dialect's envelope, or ``None`` if there is not one.

    The two dialects disagree on every name -- responses says ``input_tokens`` where chat
    completions says ``prompt_tokens`` -- and plenty of OpenAI-compatible endpoints report a
    subset, or nothing at all. Read through ``getattr`` and treat every field as optional: a
    partial report is still better than kedge's own estimate, and a missing one must not raise.

    Args:
        reported: The object carrying the counts -- a chunk, or a responses envelope.

    Returns:
        The counts, or ``None`` when the endpoint said nothing.
    """
    usage = getattr(reported, "usage", None)
    if usage is None:
        return None
    prompt = getattr(usage, "input_tokens", None)
    if prompt is None:
        prompt = getattr(usage, "prompt_tokens", 0)
    completion = getattr(usage, "output_tokens", None)
    if completion is None:
        completion = getattr(usage, "completion_tokens", 0)
    details = getattr(usage, "input_tokens_details", None) or getattr(
        usage, "prompt_tokens_details", None
    )
    cached = getattr(details, "cached_tokens", 0) or 0
    return Usage(prompt=int(prompt or 0), completion=int(completion or 0), cached=int(cached))


def chat_deltas(chunk: Any) -> list[ChatDelta]:
    """Translate one streamed chat-completions chunk into the fragments the loop reassembles.

    Chat completions streams anonymous deltas: prose and tool-call pieces arrive in the same
    envelope, and ``index`` is the only thing tying a call's name to the arguments that follow.
    """
    deltas: list[ChatDelta] = []
    # The usage chunk is the last one and carries no choices, so this is not an else: an endpoint
    # that puts usage on a chunk that also has content must have both read.
    usage = _usage_of(chunk)
    if usage is not None:
        deltas.append(ChatDelta(usage=usage))
    for choice in chunk.choices or ():
        delta = choice.delta
        if delta is None:
            continue
        if delta.content:
            deltas.append(ChatDelta(text=delta.content))
        for call in delta.tool_calls or ():
            function = call.function
            deltas.append(
                ChatDelta(
                    index=call.index or 0,
                    call_id=call.id,
                    name=function.name if function is not None else None,
                    arguments=(function.arguments or "") if function is not None else "",
                )
            )
    return deltas


def _mentions_reasoning(exc: Exception) -> bool:
    """Whether a refusal was about the reasoning parameter rather than the request as a whole."""
    return "reasoning" in str(exc).lower()


def _mentions_stream_options(exc: Exception) -> bool:
    """Whether a refusal was about the usage report kedge asked for.

    Narrow on purpose. An endpoint that has never heard of ``stream_options`` names it; anything
    that does not is a complaint about the request, and dropping the usage report would not fix it.
    """
    text = str(exc).lower()
    return "stream_options" in text or "include_usage" in text


def _missing_route(exc: Exception) -> bool:
    """Whether a refusal means this endpoint has no responses API at all.

    A server that has never heard of ``/responses`` answers 404 or 405 from its router. Some
    gateways answer 400 and say so in prose instead, so the text is checked as well -- narrowly,
    because a 400 that merely *mentions* the word is more likely to be about the request.
    """
    status = getattr(exc, "status_code", None)
    if status in (404, 405):
        return True
    text = str(exc).lower()
    return "unknown" in text and "responses" in text


class OpenAIClient:
    """A :class:`ModelClient` over an OpenAI-compatible endpoint, in either dialect.

    The only place in kedge that knows the wire format. The SDK is imported inside ``__init__``
    rather than at module scope so that importing :mod:`kedge.agent` — which the CLI does — costs
    nothing until somebody actually wants to talk to a model.

    **Responses first, chat completions when it has to be.** Every kedge turn is tool calls, and
    chat completions has nowhere to keep a reasoning model's thinking across one: the model
    reasons, calls a tool, and by the time the result comes back the reasoning that motivated the
    call is gone. Endpoints increasingly refuse the combination outright rather than quietly
    degrading. Responses keeps it, so that is what kedge asks for first.

    **Neither the dialect nor the reasoning setting may be fatal.** kedge is pointed at whatever
    the user has — a hosted API, a gateway, a llama.cpp on the next desk — and most of those
    implement chat completions only. So both are discovered rather than assumed: a missing
    ``/responses`` route downgrades the client once and permanently, and a refusal that names
    reasoning is walked down a ladder until something is accepted. Each costs one round trip, on
    the first turn only, and is logged. Pin :attr:`~kedge.config.ModelConfig.api` to skip the probe.

    **Saying nothing about reasoning is not the same as asking for none of it.** A reasoning model
    behind a gateway has a *default* effort, so a request that omits the parameter still arrives
    carrying one, and chat completions then refuses it in the presence of function tools. Sending
    nothing cannot fix that; only an explicit ``"none"`` can. That is why the chat ladder ends at
    omission rather than starting there:

    ==================  ==========================================================
    configured effort   what is tried, in order
    ==================  ==========================================================
    ``"high"``          ``"high"``, then ``"none"``, then the parameter omitted
    unset               omitted, then ``"none"``
    ==================  ==========================================================

    An endpoint that has never heard of the parameter never refuses over it, so it never leaves
    the first rung. One that defaults it lands on ``"none"``, which is the remedy its own error
    message names.

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
        api: str = "auto",
        reasoning_effort: str | None = None,
        ca_bundle: Path | None = None,
        client: Any | None = None,
    ) -> None:
        if client is None:
            from openai import AsyncOpenAI

            from kedge import tls

            # The SDK would otherwise build its own httpx client against certifi, which does not
            # carry the root a TLS-inspecting proxy re-signs with (kedge.tls).
            client = AsyncOpenAI(
                base_url=base_url,
                api_key=api_key,
                timeout=timeout,
                max_retries=max_retries,
                http_client=tls.async_client(ca_bundle=ca_bundle, timeout=timeout),
            )
        self._client = client
        self._base_url = base_url
        self._timeout = timeout
        """Seconds the endpoint may stay silent before the answer is abandoned. Kept for the error
        message rather than for the wire: a timeout the user cannot connect to a setting they can
        change is a dead end, and this one is reached during ordinary use."""

        self._max_retries = max_retries
        """Kept for the same reason. The SDK exhausts these before kedge sees a 429 or a 5xx, so a
        message that does not say so invites the user to sit and wait for a retry already spent."""

        self._include_usage = True
        """Whether chat completions is still being asked for a usage report. Dropped permanently
        if the endpoint refuses the parameter: the count is worth one round trip to discover and
        nothing at all to lose, since kedge's own estimate stands in for it."""

        self._use_responses = api != "chat_completions"
        self._pinned = api != "auto"
        self._reasoning_effort = reasoning_effort
        """What the user configured. Never mutated; the two dialects negotiate around it."""

        self._responses_effort = reasoning_effort
        """What responses is currently sending, dropped to ``None`` if it is refused."""

        self._chat_ladder: tuple[str | None, ...] = (
            (reasoning_effort, "none", None) if reasoning_effort is not None else (None, "none")
        )
        self._chat_rung = 0

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
            api=model.api,
            reasoning_effort=model.reasoning_effort,
            ca_bundle=model.ca_bundle,
        )

    async def stream(
        self,
        *,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> AsyncIterator[ChatDelta]:
        """Stream one completion, translating whatever the endpoint speaks into deltas.

        The negotiation happens around opening the stream rather than around consuming it: with
        ``stream=True`` the SDK has already sent the request and seen the status by the time it
        hands back an iterator, so a refusal arrives here, before a single fragment has been
        yielded. That is what makes retrying safe — nothing downstream has seen a partial turn.

        Transport failures are translated around *both*, because the SDK only wraps them around the
        request. Once it hands back the iterator, draining the body is ordinary httpx, so a stall
        mid-answer arrives as a bare :class:`httpx.ReadTimeout` — whose message is empty, since it
        is mapped from a bare ``TimeoutError`` — and the loop's catch-all could only report it as
        "The turn stopped unexpectedly: ReadTimeout:". Neither half of that tells the user that a
        setting they own governs it.

        A refusal the negotiation cannot absorb is translated the same way and for the same
        reason. Left raw it reaches the loop's catch-all, which reports every exception as
        unrecoverable — and the chat pane renders that as **Fatal**. A rate limit is the most
        recoverable thing an endpoint can say.

        **Nothing the SDK raises may leave here untranslated**, which is why the chain ends on
        :class:`openai.APIError` rather than on the four shapes worth their own sentence. The
        specific clauses are kept for the quality of what they say; the last one exists because the
        invariant is "every model-endpoint failure arrives as an :class:`AgentError`", and one
        exception escaping it is the whole of the difference between a recoverable turn and a
        **Fatal** one.

        Raises:
            AgentError: The endpoint timed out, the connection to it failed, it refused the request
                outright, or it failed part-way through an answer it had already begun. All are
                recoverable: the loop reports them and leaves the conversation intact.
        """
        from openai import (
            APIConnectionError,
            APIError,
            APIStatusError,
            APITimeoutError,
            BadRequestError,
            NotFoundError,
        )

        try:
            while True:
                responses = self._use_responses
                try:
                    stream = (
                        await self._open_responses(model, messages, tools)
                        if responses
                        else await self._open_chat(model, messages, tools)
                    )
                except (BadRequestError, NotFoundError) as exc:
                    if self._recover(exc):
                        continue
                    raise
                if responses:
                    async for event in stream:
                        delta = responses_delta(event)
                        if delta is not None:
                            yield delta
                else:
                    async for chunk in stream:
                        for delta in chat_deltas(chunk):
                            yield delta
                return
        # Ordered subclass-first, twice over: APITimeoutError is an APIConnectionError, and
        # httpx.TimeoutException is an httpx.TransportError.
        except (APITimeoutError, httpx.TimeoutException) as exc:
            logger.warning("the model endpoint at %s timed out", self._base_url)
            raise self._timed_out() from exc
        except (APIConnectionError, httpx.TransportError) as exc:
            logger.warning("the connection to %s failed: %r", self._base_url, exc)
            raise self._unreachable(exc) from exc
        # Last, because BadRequestError and NotFoundError are APIStatusError too: this is where
        # they land once _recover has declined them.
        except APIStatusError as exc:
            status = getattr(exc, "status_code", 0)
            logger.warning("%s refused the request with HTTP %s: %s", self._base_url, status, exc)
            raise self._refused(model, int(status), _status_detail(exc)) from exc
        # Later still, because every clause above is an APIError as well. This is the one that
        # catches a failure the endpoint reported *after* the 200: `openai/_streaming.py` raises a
        # bare APIError for an `error` event in the body, which is neither an APIStatusError (there
        # is no response left to carry a status) nor an APIConnectionError (the connection is fine
        # -- the endpoint is telling us something). A gateway announcing a context-length refusal
        # or an upstream it has just lost does exactly that, mid-answer, and it was reaching the
        # loop's catch-all: the most ordinary mid-stream failure there is, reported as **Fatal**.
        except APIError as exc:
            logger.warning("%s failed part-way through the answer: %s", self._base_url, exc)
            raise self._mid_answer(exc) from exc

    def _timed_out(self) -> AgentError:
        """Explain a timeout in terms of the setting that governs it.

        The distinction the message spends a sentence on is the one that makes this look like a
        bug: ``timeout_seconds`` is the gap httpx allows *between reads*, not a budget for the
        whole answer. An endpoint that streams steadily never approaches it however long it takes,
        and one that says nothing while a reasoning model thinks trips it while working perfectly.
        """
        return AgentError(
            f"the model endpoint at {self._base_url} sent nothing for {self._timeout:g}s, so the "
            f"answer was abandoned. That limit is the silence allowed between fragments, not a "
            f"budget for the whole answer, so an endpoint that stays quiet while a reasoning "
            f"model thinks reaches it without anything being wrong. Raise `timeout_seconds` "
            f"under `[model]` in your kedge config, or lower `reasoning_effort`. Nothing was "
            f"left half-written -- ask again to retry."
        )

    def _unreachable(self, exc: Exception) -> AgentError:
        """Explain a connection that never opened, or died part-way through an answer.

        ``kedge doctor`` is named rather than second-guessed here: a certificate rejected by a
        TLS-inspecting proxy arrives as an ordinary ``ConnectError``, and doctor is the one place
        that unwraps it and says what to do about it (:func:`kedge.tls.certificate_error`).
        """
        detail = str(exc).strip() or type(exc).__name__
        return AgentError(
            f"the connection to the model endpoint at {self._base_url} failed: {detail}. Any "
            f"answer in flight was abandoned, but nothing was left half-written. Run `kedge "
            f"doctor` to check the endpoint is reachable and its certificate verifies, then ask "
            f"again."
        )

    def _mid_answer(self, exc: Exception) -> AgentError:
        """Explain a failure the endpoint reported once it had already started answering.

        Said in terms of where it leaves the user, because that is what distinguishes this from
        every other endpoint failure: part of an answer has already been streamed and persisted,
        and the rest is not coming. The two usual causes are named rather than guessed at -- a
        context that outgrew the model's window, and an upstream a gateway lost mid-answer -- since
        one of them is fixed by asking something shorter and the other by asking again, and the
        user cannot tell which from prose the endpoint wrote for a machine.
        """
        return AgentError(
            f"the model endpoint at {self._base_url} accepted the request and then failed part-way "
            f"through the answer: {_status_detail(exc)}. Whatever had streamed before that is all "
            f"there is of it, and nothing was left half-written -- every notebook edit is a single "
            f"atomic flush. This is usually a conversation that outgrew the model's context "
            f"window, or an upstream a gateway lost while relaying: ask something narrower, or "
            f"ask again."
        )

    def _refused(self, model: str, status: int, detail: str) -> AgentError:
        """Explain a status the endpoint answered with, in terms of who can act on it.

        The split is by who has to do something. A 429 or a 5xx is the endpoint's own state and
        the user's part is to ask again; a 401 is a key they hold; a 400 is a request that will be
        refused identically however many times it is sent, so saying "try again" would be wrong.

        The retry count is named wherever the SDK has already spent it. Otherwise "ask again"
        reads as advice to wait, when the waiting is done.
        """
        spent = (
            f" The SDK already retried {self._max_retries} times before you saw this."
            if self._max_retries
            else ""
        )
        if status == 429:
            return AgentError(
                f"the model endpoint at {self._base_url} is rate limiting kedge (HTTP 429): "
                f"{detail}.{spent} The limit is the endpoint's, not kedge's, so the only remedy "
                f"is to leave it a moment and ask again. Nothing was left half-written."
            )
        if status >= 500:
            return AgentError(
                f"the model endpoint at {self._base_url} failed on its own side (HTTP {status}): "
                f"{detail}.{spent} That is the endpoint or a gateway in front of it rather than "
                f"anything about your request, so ask again. Nothing was left half-written."
            )
        if status in (401, 403):
            return AgentError(
                f"the model endpoint at {self._base_url} rejected kedge's API key (HTTP "
                f"{status}): {detail}. Set a working key in the settings pane, or under `[model]` "
                f"in your kedge config, and ask again. `kedge doctor --network` checks a key "
                f"without spending a turn."
            )
        if status == 404:
            # A 404 only reaches here when the fallback could not run: either the dialect is
            # pinned, or the route that is missing is not the one negotiation knows how to avoid.
            remedy = (
                "`api` is pinned to `responses` under `[model]` in your kedge config, so kedge "
                "did not fall back to chat completions. Unpin it to let kedge negotiate, or "
                "check the endpoint really serves that route"
                if self._pinned and self._use_responses
                else "Check `base_url` under `[model]` in your kedge config -- it should be the "
                "root the endpoint serves, usually ending `/v1`"
            )
            return AgentError(
                f"the model endpoint at {self._base_url} has no route for this request (HTTP "
                f"404): {detail}. {remedy}."
            )
        return AgentError(
            f"the model endpoint at {self._base_url} refused the request for model {model!r} "
            f"(HTTP {status}): {detail}. kedge does not retry a refusal, because the same request "
            f"would be refused the same way. The usual cause is a model the endpoint does not "
            f"serve; check `model` under `[model]` in your kedge config against what "
            f"`kedge doctor --network` lists."
        )

    def _recover(self, exc: Exception) -> bool:
        """Adjust for a refusal and report whether the request is worth sending again.

        False for anything that is not about the dialect, the usage report or reasoning, because
        degrading around every 400 would turn a real complaint -- an unknown model, a malformed
        schema -- into a turn that silently did nothing and said why to nobody.
        """
        if self._use_responses and not self._pinned and _missing_route(exc):
            logger.info(
                "the endpoint has no responses API (%s); using chat completions from here on", exc
            )
            self._use_responses = False
            return True
        if self._include_usage and _mentions_stream_options(exc):
            logger.info(
                "the endpoint does not accept stream_options (%s); kedge will estimate token "
                "counts instead of reading them",
                exc,
            )
            self._include_usage = False
            return True
        if not _mentions_reasoning(exc):
            return False
        if self._use_responses:
            if self._responses_effort is None:
                return False
            logger.warning(
                "the endpoint refused reasoning effort %r on the responses API (%s); dropping it",
                self._responses_effort,
                exc,
            )
            self._responses_effort = None
            return True
        if self._chat_rung + 1 >= len(self._chat_ladder):
            return False
        self._chat_rung += 1
        logger.warning(
            "the endpoint refused reasoning_effort=%r on chat completions (%s); trying %r",
            self._chat_ladder[self._chat_rung - 1],
            exc,
            self._chat_ladder[self._chat_rung],
        )
        return True

    async def _open_responses(
        self,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> Any:
        """Send the request in the responses dialect and return the event stream."""
        payload: dict[str, Any] = {
            "model": model,
            "input": responses_input(messages),
            "stream": True,
        }
        if tools:
            payload["tools"] = responses_tools(tools)
            payload["tool_choice"] = "auto"
        if self._responses_effort is not None:
            payload["reasoning"] = {"effort": self._responses_effort}
        return await self._client.responses.create(**payload)

    async def _open_chat(
        self,
        model: str,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
    ) -> Any:
        """Send the request in the chat-completions dialect, which most endpoints implement."""
        payload: dict[str, Any] = {
            "model": model,
            "messages": [dict(message) for message in messages],
            "stream": True,
        }
        if tools:
            payload["tools"] = [dict(tool) for tool in tools]
            payload["tool_choice"] = "auto"
        effort = self._chat_ladder[self._chat_rung]
        if effort is not None:
            payload["reasoning_effort"] = effort
        if self._include_usage:
            # Chat completions reports usage only when asked; responses reports it unprompted.
            payload["stream_options"] = {"include_usage": True}
        return await self._client.chat.completions.create(**payload)

    async def aclose(self) -> None:
        """Release the underlying HTTP client."""
        await self._client.close()


# ── bookkeeping ──────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _Reply:
    """One model turn's output, reassembled from its fragments."""

    content: str = ""
    calls: list[PendingToolCall] = field(default_factory=list)
    usage: Usage | None = None


@dataclass(slots=True)
class _Meter:
    """Token accounting for one turn, reported on the ``DoneEvent``.

    Two tallies, kept apart. The estimate is kedge's own count over a fixed encoding and is always
    available; the report is the endpoint's, is authoritative, and is the only one that can see
    what the cache served. Whichever is used, it accumulates **per step** -- a turn is up to
    ``max_steps`` completions, plus the last word a capped turn is given
    (:meth:`KedgeAgent._final_word`), and each one re-sends the whole prompt, so a single step's
    figure understates the turn by as much as that multiple. The last word re-sends everything but
    the tool schemas, which it withholds -- which makes it the one step that is also billed
    entirely uncached.
    """

    prompt: int = 0
    """Estimated prompt tokens, summed across the turn's steps."""

    output: int = 0
    """Estimated output tokens, summed across the turn's steps."""

    reported_prompt: int = 0
    reported_output: int = 0
    reported_cached: int = 0
    steps: int = 0
    steps_reported: int = 0

    pending_prompt: int = 0
    pending_output: int = 0
    """This step's estimate, held back until the step ends and it is known whether the endpoint
    reported its own. Exactly one of the two tallies takes each step; counting both would double
    it."""

    def stage_prompt(self, tokens: int) -> None:
        """Record what this step's prompt was estimated to cost."""
        self.pending_prompt = tokens

    def stage_output(self, tokens: int) -> None:
        """Add to what this step's output has been estimated to cost so far."""
        self.pending_output += tokens

    def record(self, usage: Usage | None) -> None:
        """Close one step, taking the endpoint's numbers over kedge's estimate where there are."""
        self.steps += 1
        if usage is None:
            self.prompt += self.pending_prompt
            self.output += self.pending_output
        else:
            self.steps_reported += 1
            self.reported_prompt += usage.prompt
            self.reported_output += usage.completion
            self.reported_cached += usage.cached
        self.pending_prompt = 0
        self.pending_output = 0

    @property
    def measured(self) -> bool:
        """Whether every step of the turn came back with the endpoint's own numbers."""
        return self.steps_reported > 0 and self.steps_reported == self.steps

    @property
    def total(self) -> int:
        """Prompt plus output for the whole turn, reported where reported and estimated elsewhere.

        A step abandoned part-way -- cancelled, or ended by a failing endpoint -- never closes, so
        its estimate is added from the staging fields rather than lost.
        """
        return (
            self.prompt
            + self.output
            + self.reported_prompt
            + self.reported_output
            + self.pending_prompt
            + self.pending_output
        )


async def _abandon_if_cancelled[T](work: Awaitable[T], cancel: CancelToken) -> T:
    """Await ``work``, abandoning it the moment the user asks the turn to stop.

    Checking the token *between* steps is not enough on its own, and the gap is not a corner case.
    A reasoning model spends the whole of its thinking on one await inside
    :meth:`OpenAIClient.stream`, emitting reasoning events that :func:`responses_delta` translates
    to ``None`` and the loop therefore never sees — so a check written inside ``async for delta``
    does not run once during exactly the stretch a user reaches for Stop. Racing the token against
    the work makes the stop land while the model is still thinking, which is where it was aimed.

    Cancelling is deliberately confined to the model call. A tool call is left to finish: the
    kernel has already been handed the program by the time the await is pending, so dropping the
    HTTP read would not undo the edit, it would only lose the confirmation of it — and a loop that
    believes a cell was not created when it was is a worse failure than a Stop that waits out one
    round trip.

    This races rather than replaces. A token that is already set stops the turn here without
    starting anything, and a caller that goes on to check the token itself after each result is not
    being redundant: work that finishes *and* a stop that arrives in the same moment must resolve
    as stopped, and only the caller knows what it was about to do with the result.

    Raises:
        asyncio.CancelledError: The token was set before ``work`` finished. ``work`` has been
            cancelled and awaited, so nothing is left running behind it.
    """
    cancel.raise_if_cancelled()
    task = asyncio.ensure_future(work)
    stop = asyncio.ensure_future(cancel.wait())
    try:
        await asyncio.wait({task, stop}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        task.cancel()
        raise
    finally:
        stop.cancel()

    if task.done():
        return task.result()

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        # The turn is being abandoned; how the work it was waiting on died is no longer material.
        logger.debug("abandoned work raised while being cancelled", exc_info=True)
    raise asyncio.CancelledError


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
        self._suspended: dict[str, tuple[ContextMessage, ...]] = {}
        """Per session: the messages of the turn just finished, held for the next one.

        Held whether or not the turn answered (:meth:`_carry`). What differs is how much of it is
        still true by the time it is read."""

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
            max_steps=workspace.config.agent.max_steps,
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
                    meter.stage_output(self._counter.count(event.text))
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
                    f"The turn stopped unexpectedly: {_describe(exc)}. The notebook has "
                    f"not been left half-changed — every notebook edit is a single atomic flush."
                ),
                recoverable=False,
            )
        _log_spend(request.turn_id, meter)
        yield DoneEvent(turn_id=request.turn_id, tokens_used=meter.total)

    # ── one turn ─────────────────────────────────────────────────────────────────────────

    async def _turn(
        self, request: TurnRequest, cancel: CancelToken, meter: _Meter
    ) -> AsyncIterator[AnyEvent]:
        """Work the turn to an answer, or to the point where it needs the user.

        Every path out of the step loop offers the window to the session, the answering one
        included, so the next message starts warm rather than re-reading a workbook this turn has
        already read. The *call* is unconditional; whether anything is held is not — a turn whose
        every result has expired hands on nothing (:meth:`_carry`). It is done in ``finally``
        because the paths are not all returns: a Stop and a failing endpoint both leave through an
        exception, and both have usually spent real work getting wherever they got to.

        The carry is taken before :meth:`_remember` compacts, and the order matters.
        :meth:`~kedge.agent.context.ConversationWindow.compact` drops every message older than the
        turn being built, which is precisely the resumed span, so compacting first collapses the
        carry to its newest leg on every turn a session is over the compaction threshold — the
        sessions that most need the warm start. Compaction's lasting product is the digest, which
        is unaffected by being written second, and :meth:`_carry` does not mutate the window.

        ``answered`` is the one thing the ``finally`` cannot work out for itself, since a return
        and a raise arrive there identically, so the step loop records it on the way past.
        """
        yield StatusEvent(phase="analysing")
        cancel.raise_if_cancelled()

        tools = self._registry_for(request.session_id)
        state = await self._notebook_state()
        # Resolved once and used twice, which is the whole fix: the id stamped onto anything this
        # turn produces is the same expression that chooses the endpoint it runs against, so the
        # two cannot drift. `request.model` is per session and mutable (`server/routes.py`), so
        # config is the fallback rather than the answer.
        model = request.model or self._model
        tools.refresh(state, model=model)
        window = self._window_for(request, state)
        attempts: dict[str, int] = {}
        answered = False

        try:
            for step in range(self._max_steps):
                cancel.raise_if_cancelled()
                yield StatusEvent(phase="thinking")

                reply = _Reply()
                messages = window.assemble()
                # token_total is the same count over the same messages, already memoised by the
                # window -- recounting the rendered list here was a third full tokenisation.
                meter.stage_prompt(window.token_total())
                async for event in self._complete(
                    messages, model, cancel, reply, tools=self._tools
                ):
                    yield event
                meter.record(reply.usage)

                window.add_assistant(
                    reply.content, tool_calls=[call.to_message() for call in reply.calls]
                )
                if not reply.calls:
                    logger.debug("turn %s finished after %d step(s)", request.turn_id, step + 1)
                    answered = True
                    return

                stop = False
                for call in reply.calls:
                    cancel.raise_if_cancelled()
                    async for event in self._invoke(call, tools, window, request, attempts):
                        if isinstance(event, ErrorEvent):
                            stop = True
                        yield event
                if stop:
                    # One completion, tools withheld, and then the turn is over. Returning straight
                    # after it is what makes "exactly once" structural rather than a flag somebody
                    # has to keep true: however many caps fired in this step, control leaves the
                    # step loop here and cannot come back round.
                    #
                    # `answered` deliberately stays False. Prose reaches the user, but the *work*
                    # did not finish — the next message is "try that again" against everything this
                    # turn read — so `_carry` must resume the leg it just ran as itself. Setting it
                    # True would expire the whole span from age zero and throw away the forty reads
                    # the account was written from, which is the expensive failure the carry exists
                    # to prevent, and it would be thrown away on the one path that most needs it.
                    async for event in self._final_word(window, model, cancel, meter, request):
                        yield event
                    return

            # The budget is spent but the work is not lost: the window goes on the shelf, the
            # question goes to the user, and the pause is put to the model as its own last word so
            # that "continue" reads as the answer to something it said rather than a non sequitur.
            paused = _pause_message(self._max_steps)
            window.add_assistant(paused)
            logger.info("turn %s paused after its %d step budget", request.turn_id, self._max_steps)
            yield PausedEvent(message=paused, steps=self._max_steps)
        finally:
            self._carry(request.session_id, window, answered=answered)
            self._remember(request.session_id, window)

    async def _complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        model: str,
        cancel: CancelToken,
        reply: _Reply,
        *,
        tools: Sequence[Mapping[str, Any]],
    ) -> AsyncIterator[AnyEvent]:
        """Stream one completion, emitting prose as it arrives and collecting tool calls.

        Pulled one fragment at a time through :func:`_abandon_if_cancelled` rather than with a
        plain ``async for``, so that Stop lands while the model is still thinking rather than only
        once it has something to say. See that function for why the difference is not academic.

        ``model`` is already resolved by :meth:`_turn`, and falling back to the configured default
        a second time here is the bug this signature now forbids: two places deciding which model
        runs is one place too many, and the loser stamps its answer onto the turn's artifacts.

        ``tools`` is passed rather than taken from ``self`` for the same reason, and it has one
        caller that hands it nothing: :meth:`_final_word` withholds the surface so that the only
        move left is to speak. Both dialects send no ``tools`` key at all for an empty sequence
        (:meth:`OpenAIClient._open_chat`), which is what an endpoint that rejects ``"tools": []``
        needs and what "there is nothing to call" honestly looks like on the wire.
        """
        parts: dict[int, dict[str, str]] = {}
        stream = self._client.stream(model=model, messages=messages, tools=tools).__aiter__()
        try:
            while True:
                try:
                    delta = await _abandon_if_cancelled(stream.__anext__(), cancel)
                except StopAsyncIteration:
                    break
                cancel.raise_if_cancelled()
                if delta.usage is not None:
                    reply.usage = delta.usage
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
        finally:
            # The connection is the endpoint's to hold open until somebody closes it, and an
            # abandoned turn must not leave one behind. Suppressed rather than checked: closing a
            # generator that has already unwound is a no-op, and closing one that has not is the
            # whole point.
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    await aclose()

        reply.calls = [
            PendingToolCall(
                id=slot["id"] or f"call_{index}",
                name=slot["name"],
                arguments=slot["arguments"],
            )
            for index, slot in sorted(parts.items())
            if slot["name"]
        ]

    async def _final_word(
        self,
        window: ConversationWindow,
        model: str,
        cancel: CancelToken,
        meter: _Meter,
        request: TurnRequest,
    ) -> AsyncIterator[AnyEvent]:
        """Spend one completion, with nothing to call, so a stopped turn still says something.

        Reached only from the retry caps in :meth:`_invoke`, and only once — the step loop returns
        the moment this finishes. That is the whole design: the caps exist because the model cannot
        talk its way out of the failure, so handing it the tools again would be inviting a fourth
        attempt at the shape it has got wrong three times. Withheld, the only move left is prose,
        which is the move that was wanted.

        The :class:`~kedge.server.events.ErrorEvent` has already been emitted by the time this
        runs, and the order is load-bearing rather than incidental: the user reads why the turn
        stopped and then reads the account, so the account arrives as an explanation of a stop they
        have already been told about rather than as prose that trails off for no stated reason.

        **Withholding the tools costs the prompt cache the whole request, knowingly.** The tool
        definitions sit at the front of the payload, so sending none of them changes the prompt at
        byte zero and nothing behind it can match the prefix the turn's earlier steps cached: the
        entire last-word prompt is billed fresh, some 9,000 tokens where an ordinary step's
        uncached remainder is nearer 100. That is the right trade exactly here — the path runs only
        when a turn has already failed, and it runs once — and it would be an expensive habit
        anywhere else in this file.

        **Best-effort, and contained.** The call can fail, and a turn that has already told the
        user why it stopped must not then be branded unrecoverable — which is what the loop's
        catch-all would do with anything raised here, and what ``app.js`` renders as **Fatal**. An
        account that does not arrive is not fatal to anything: the turn's own conclusion stands and
        the reading is still on the shelf. Where nothing arrives at all, :data:`_NO_ACCOUNT` says so
        rather than leaving the stop to be read as a hang.

        Tool calls that come back anyway are dropped rather than dispatched, and dropped from the
        window as well as from the loop. An assistant message carrying ``tool_calls`` with no result
        against each id is rejected outright by the endpoint, so recording them would poison every
        later turn of the session with a request that cannot be sent
        (:meth:`~kedge.agent.context.ConversationWindow.suspend`).

        Cancellation runs through :meth:`_complete` exactly as it does for any other step, which is
        the point of going through it rather than opening a stream here: Stop pressed during the
        last word abandons the call mid-thought (:func:`_abandon_if_cancelled`) and leaves the turn
        through :meth:`run`'s cancel path, ``finally`` and carry included.
        """
        yield StatusEvent(phase="thinking")
        reply = _Reply()
        messages = [*window.assemble(), {"role": "user", "content": _FINAL_WORD_PROMPT}]
        # The prompt the endpoint is billed for is the window plus the nudge, so both are staged:
        # a step left out of the meter understates the turn just as surely as one counted twice
        # overstates it, and this one is a whole prompt.
        meter.stage_prompt(window.token_total() + self._counter.count(_FINAL_WORD_PROMPT))
        failure: Exception | None = None
        try:
            async for event in self._complete(messages, model, cancel, reply, tools=()):
                yield event
        except Exception as exc:
            # Broad, and it has to be: what makes this containment rather than swallowing is that
            # `asyncio.CancelledError` is a `BaseException` and so passes straight through, leaving
            # the user's Stop to end the turn as a cancellation exactly as it would anywhere else.
            # Everything else is one extra call that did not come off, on a turn whose outcome was
            # settled before it was made.
            #
            # With the traceback, because the one class of exception this genuinely hides is our
            # own: a `TypeError` in delta translation is Fatal-with-a-stack on any ordinary step
            # and would be a single line here. The containment is for the user's sake, not for the
            # log's.
            failure = exc
            logger.warning(
                "turn %s could not fetch its last word: %s",
                request.turn_id,
                _describe(exc),
                exc_info=True,
            )

        account = reply.content
        if not account:
            account = _NO_ACCOUNT
            yield TokenEvent(text=account)
        elif failure is not None:
            # Prose arrived and then the call died -- which is exactly the shape the mid-answer
            # translation exists for, and the one failure here that leaves something behind. The
            # empty-account fallback cannot cover it, and without a marker the turn ends mid-word.
            marker = _cut_short(failure)
            account += marker
            yield TokenEvent(text=marker)
        # After the fallback rather than before it, so the step closes over a line kedge wrote
        # itself instead of leaving it pending on the turn's total. Where the endpoint reported its
        # own numbers -- the case the figure's accuracy actually rests on -- the estimate carrying
        # it is discarded with the step, and nobody is billed for prose the model never sent.
        meter.record(reply.usage)
        # Recorded so the window carries the answer the user just read, which is what makes a
        # resumed conversation replay it: `_history_before` trims the persisted exchange on the
        # understanding that the carried span reinstates it, so an account the window never held is
        # an account the next turn cannot see at all.
        window.add_assistant(account)
        logger.info(
            "turn %s spent its last word with the tools withheld: %d characters of prose",
            request.turn_id,
            len(account),
        )

    async def _invoke(
        self,
        call: PendingToolCall,
        tools: ToolRegistry,
        window: ConversationWindow,
        request: TurnRequest,
        attempts: dict[str, int],
    ) -> AsyncIterator[AnyEvent]:
        """Run one tool call and emit everything the UI needs to narrate it.

        Two retry caps share the ``attempts`` ledger, and they count different things. The
        validation gate rejecting a cell is counted per cell name, because a model fixing one cell
        while another stays broken is making progress. A tool rejecting the model's own *draft* is
        counted per tool, because there is only one draft in play. Both stop the turn at three
        with an :class:`~kedge.server.events.ErrorEvent`, which the step loop treats as a reason
        to stop spending the budget — and then to spend one last completion on prose
        (:meth:`_final_word`).

        Both messages are written for the user, and only the user. They are rendered in the trail
        as a warning chip and the model never sees them; the instruction that used to be buried in
        the second one — "say in the chat what the process does stage by stage" — was addressed to
        a reader who was never given a turn in which to do it, which is what made this defect quiet.
        The instruction now lives in :data:`_FINAL_WORD_PROMPT`, where the model actually reads it.

        **What is left promises nothing.** The account :meth:`_final_word` goes on to fetch is
        best-effort by construction — the call can fail, come back empty, or be cut short by a Stop
        that is honoured — and a chip saying prose follows is worse than a terse one on every path
        where it does not. The chip is the turn's own account of itself, read after the fact in a
        persisted trail, so it is written in the past tense about what actually happened.
        """
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

        window.add_tool_result(
            tool_call_id=call.id,
            name=call.name,
            content=result.text,
            arguments=arguments,
            shape=_result_shape(result),
        )

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

        if result.draft_rejected:
            # Namespaced so a cell that happens to share a tool's name cannot share its counter.
            key = f"draft:{call.name}"
            attempts[key] = attempts.get(key, 0) + 1
            if attempts[key] >= MAX_DRAFT_ATTEMPTS:
                logger.info(
                    "stopping turn %s: %s rejected the model's draft %d times",
                    request.turn_id,
                    call.name,
                    attempts[key],
                )
                yield ErrorEvent(
                    message=(
                        f"`{call.name}` rejected the draft {attempts[key]} times running, so I "
                        f"have stopped rather than keep reformatting it. The rejections are above "
                        f"— they are the actual reason, not a summary of it."
                    ),
                    recoverable=True,
                )

    # ── context ──────────────────────────────────────────────────────────────────────────

    async def _notebook_state(self) -> NotebookState:
        """Read the notebook from the kernel. Never from history — history goes stale."""
        driver = self._context.driver
        if driver is None:
            return NotebookState()
        bodies = await asyncio.to_thread(self._saved_bodies)
        try:
            return NotebookState.from_graph(await driver.read_graph(), bodies=bodies)
        except KedgeError as exc:
            logger.warning("could not read the notebook graph (%s); falling back to a listing", exc)
        try:
            return NotebookState.from_cells(await driver.list_cells(with_code=False), bodies=bodies)
        except KedgeError as exc:
            logger.warning("could not list the notebook's cells either (%s)", exc)
            return NotebookState()

    def _saved_bodies(self) -> dict[str, str]:
        """``cell name -> source``, read from the notebook file rather than from the kernel.

        This exists for one fact -- which cells are still ``TODO(kedge)`` holes -- and the route
        it takes is the whole point. Asking the kernel would mean ``list_cells(with_code=True)``,
        and reading a cell's code is what records a read for marimo's staleness guard: doing it
        once per turn would permanently disarm the check that stops ``edit_cell`` overwriting
        what the user typed while the model was thinking. Reading the ``.py`` is outside that
        mechanism entirely -- no kernel, no ``_code_mode``, no read recorded -- at the cost of
        being the notebook as *last saved*, which is why the block labels where the flag came
        from rather than presenting it as this turn's kernel state.

        Every failure is the same answer: an empty map, so
        :attr:`~kedge.agent.context.CellFacts.unwritten` stays unknown and the block says
        nothing. A missing file, an unparseable one, one that is not a marimo notebook, one
        written in some other encoding -- none of them is a reason to guess, and this is a
        cosmetic flag on a block whose value is that it does not lie.

        A name the file holds twice is dropped rather than resolved. marimo will not run such a
        notebook, so one on disk means the file is being written or has been hand-edited into a
        state the kernel has not accepted; picking either body would answer a question about a
        cell nobody can say is *the* cell of that name. Dropping it also makes the map stop
        covering the kernel's cell list, which is what
        :func:`~kedge.agent.context._usable` reads as "do not use this file at all" -- the same
        answer as for a half-written one, by the same rule.

        Returns:
            The named cells' bodies, or an empty map where none could be read.
        """
        workspace = self._context.workspace
        if workspace is None:
            return {}
        path = workspace.notebook_path
        if not path.exists():
            # Not a fallback: every session before a plan is approved is this, and a warning a
            # turn for the ordinary state of a new workspace is a warning nobody reads.
            logger.debug("no unwritten flags: %s has not been scaffolded yet", path)
            return {}
        try:
            document = read_notebook(path)
        except (KedgeError, OSError, ValueError) as exc:
            logger.warning(
                "could not read %s (%s); the notebook block will not say which cells are still "
                "unwritten",
                path,
                exc,
            )
            return {}
        bodies: dict[str, str] = {}
        repeated: set[str] = set()
        for cell in document.cells:
            if not cell.is_named:
                continue
            if cell.name in bodies:
                repeated.add(cell.name)
                continue
            bodies[cell.name] = cell.code
        for name in repeated:
            del bodies[name]
        if repeated:
            logger.warning(
                "%s holds %d cell name(s) more than once (%s); the notebook block will not say "
                "which cells are still unwritten",
                path,
                len(repeated),
                ", ".join(sorted(repeated)),
            )
        return bodies

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
        # Least volatile first. A prompt cache keys on the prefix, so anything placed ahead of a
        # block that changes is cached and anything behind it is not: the workbook analysis and
        # the plan hold still for a whole session, while the registry and the notebook state
        # change the moment a cell is created. Ordering them the other way round -- as this did --
        # pushed up to 1,400 tokens of analysis out of the cached prefix on every turn that
        # touched the notebook. It reads better this way too: the workbook, the plan, then where
        # the notebook has got to.
        window.set_pinned(
            [
                build_analysis_block(self._analysis),
                build_plan_block(self._plan()),
                state.registry.render(),
                state.render(),
            ]
        )
        window.set_digest(self._digests.get(request.session_id, ""))

        resumed = self._suspended.pop(request.session_id, None)
        history = [(message.role, message.content) for message in request.history]
        if resumed is not None:
            history = _history_before(history, resumed)
        window.load_history(history)
        if resumed is not None:
            window.resume(resumed)
            logger.info(
                "session %s picks up the turn it carried: %d messages, %d of them tool results, "
                "%d of those still holding their content",
                request.session_id,
                len(resumed),
                sum(1 for message in resumed if message.kind == "tool_result"),
                sum(
                    1
                    for message in resumed
                    if message.kind == "tool_result" and not message.evicted
                ),
            )
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

    def _carry(self, session_id: str, window: ConversationWindow, *, answered: bool) -> None:
        """Hold the turn's tool traffic for the next message, keeping what is still true.

        A turn that answered is carried too, and that is the point of this method rather than a
        detail of it. The user who asks for a summary of the workbook and then asks for the
        notebook was, until this, paying for the same investigation twice: the server persists
        prose only, so a successful turn's forty tool calls were thrown away precisely because it
        had gone well. The expensive turn was being punished for succeeding.

        What differs is how much of the carry keeps its content, and the axis is a turn boundary
        rather than success. A turn boundary is when the user acts: they type the next message, and
        they edit cells in the pane beside the chat while a paused turn waits — which is why
        :class:`~kedge.agent.context.NotebookState` is rebuilt from the kernel every turn and never
        read out of history. So:

        * the leg this turn just ran is resumed as itself when the turn stopped early. Nothing has
          crossed a boundary since it ran, so every result stands. When the turn answered, the
          conversation has moved past it and only
          :attr:`~kedge.agent.tools.Volatility.SESSION_STABLE` results — the workbook and the
          analysis, neither of which kedge writes — are still true;
        * every older leg in the span has crossed at least one boundary whichever way this turn
          ended, so its volatile results expire regardless. Two pauses in a row used to skip this
          entirely, and turn one's ``probe`` output was still being carried with full content at
          turn three, after turn two had created cells.

        Stale results are evicted rather than removed. A chat completion is rejected outright when
        an assistant message carrying ``tool_calls`` is not followed by a result for each of them
        (:meth:`~kedge.agent.context.ConversationWindow.suspend`), so dropping the message would
        break the request; evicting it leaves the stub that names the call, says what shape came
        back, and tells the model to make it again if it still wants the answer.

        Held only when something in it will still hold its content next turn.
        :meth:`~kedge.agent.context.ConversationWindow._recut` stamps the stub on anything that
        would arrive expired, so a result still unevicted here is one the next turn can actually
        read — which is what makes this gate honest at
        ``evict_tool_results_after_turns = 1``, where the whole span expires on arrival and a gate
        measuring on the other side of that boundary reported it as current. A turn that called
        nothing, or one whose every result has expired, is an all-volatile shell: the same user
        message and the same final answer the server's history already holds, plus interim prose
        and a stub naming a call that no longer answers anything. It costs about 2.8 times the
        flattened record and adds nothing load-bearing, so it is dropped and history is left
        untrimmed (:func:`_history_before`).

        **The audit log is not written again for a carried payload.** One line per value-returning
        call is what :mod:`kedge.agent.audit` records, and a result held here is re-sent to the
        endpoint for several turns after that line was written. The set of data that has left this
        machine is still recorded in full — nothing reaches the model that was not audited on its
        first call — so ``SECURITY.md``'s claim holds, but the count of lines is a count of calls
        and never a count of transmissions. The session cache in :mod:`kedge.agent.tools` takes the
        opposite decision for the same class of re-use, routing a cache hit back through
        ``_finalise`` so it writes its line; that asymmetry is deliberate. A cache hit is a fresh
        call the user made and would expect to see; a carry is one call the model keeps reading.
        """
        carried = window.suspend()
        carried = _expire_volatile(carried, from_age=0 if answered else 1)
        live = sum(
            1 for message in carried if message.kind == "tool_result" and not message.evicted
        )
        if not live:
            self._suspended.pop(session_id, None)
            return
        self._suspended[session_id] = carried
        logger.info(
            "session %s is holding %d messages from the turn it just %s, %d of them still current",
            session_id,
            len(carried),
            # Not "paused": a turn can now also stop at a retry cap and answer in prose anyway, and
            # a flag with two values cannot name three endings. It says what it actually knows.
            "answered" if answered else "did not finish",
            live,
        )

    def reset_session(self, session_id: str) -> None:
        """Forget a session's digest, its held turn and its tool registry. The ``/new`` path."""
        self._digests.pop(session_id, None)
        self._suspended.pop(session_id, None)
        self.registries.pop(session_id, None)

    async def aclose(self) -> None:
        """Release every per-session workbook handle."""
        for registry in list(self.registries.values()):
            await registry.aclose()

    def __repr__(self) -> str:
        return f"KedgeAgent(model={self._model!r}, sessions={len(self.registries)})"


def _pause_message(steps: int) -> str:
    """What a turn says when it reaches its step budget.

    Phrased as a question with a default, because that is what it is. The reassurance about
    nothing being lost is load-bearing rather than politeness: a user who believes the turn has
    been thrown away starts again from the beginning, which is the expensive thing this exists to
    avoid.
    """
    return (
        f"I have taken {steps} steps on this turn without finishing, so I have paused to check "
        f"with you rather than keep going on my own. Nothing is lost — everything I have read and "
        f"run so far is still here. Say 'continue' and I will pick up exactly where I stopped, or "
        f"tell me what to do differently and I will take it from there."
    )


def _cut_short(exc: Exception) -> str:
    """Mark an account the endpoint stopped part-way through — in the text, not only on the screen.

    The failure this covers is the one :meth:`OpenAIClient._mid_answer` exists for, and it is the
    only one here that leaves something behind: prose has already streamed, so :data:`_NO_ACCOUNT`
    does not fire and the last word would otherwise end mid-word with nothing to say why. On the
    screen that is a puzzle. In the record it is worse — :meth:`KedgeAgent._final_word` records what
    arrived, :meth:`KedgeAgent._carry` hands it to the next turn, and the server persists it as the
    turn's assistant message — so a fragment with no marker reads as a complete thought for as long
    as the transcript lasts. Appending it to ``account`` rather than only emitting it is therefore
    the point of this, not a detail of it.

    Bracketed because it is inserted into somebody else's paragraph: everything before it is the
    model's and this is kedge's, and a reader has to be able to tell without being told. The same
    rule as :data:`_NO_ACCOUNT`, which is the other thing kedge says in this channel.

    Args:
        exc: What ended the account. A :class:`~kedge.errors.KedgeError` is quoted whole, since it
            is already prose written for this user; anything else is named by type as well as
            message (:func:`_describe`), because a message written for a developer is better
            labelled than quoted bare. That is the same split :meth:`KedgeAgent.run` makes.

    Returns:
        The marker, with the leading blank line that separates it from the prose it follows.
    """
    reason = (str(exc) if isinstance(exc, KedgeError) else _describe(exc)).strip()
    if not reason.endswith("."):
        reason += "."
    return (
        f"\n\n[This account stops here: {reason} What is above it is all of it that arrived. "
        f"Nothing I read on this turn has been thrown away, so ask me to summarise what I found "
        f"and I will pick it up from where it stopped.]"
    )


def _history_before(
    history: Sequence[tuple[str, str]], resumed: Sequence[ContextMessage]
) -> list[tuple[str, str]]:
    """Drop the persisted record of the turn ``resumed`` carries, which is about to replace it.

    The server stores a turn as one user message and the prose that answered it, so a carried turn
    is already in history — flattened, with the tool calls that motivated the prose gone and the
    interleaving with them gone too. The carried messages are that same turn intact, so the record
    is replaced rather than kept alongside and read twice.

    Both endings need the same trim, and the answered one needs it more. A turn that paused leaves
    a user message and whatever prose had streamed before the pause; a turn that answered leaves a
    user message and the whole answer — and the answer is in the carry too, because the assistant
    message is recorded before the step loop returns. Everything from the opener onward is dropped
    rather than the opener alone, which is what keeps the answer from being read twice, once
    flattened and once in place.

    How much to trim is *counted*, not searched for. A window that has been carried repeatedly
    hands back several turns at once, one user message each, and the span always reinstates the
    last of history and nothing else — so trimming the last ``n`` exchanges, where ``n`` is the
    number of user messages the span holds, removes exactly what is about to be put back.

    Searching backwards for a message whose content equals the span's opener is what this used to
    do, and it is wrong for a reason the feature makes routine rather than exotic: the same user
    text recurs. ``_pause_message`` asks the user to type "continue", two consecutive "carry on"s
    are one span, and matching the *later* copy trims history at the wrong place — leaving every
    turn between the two copies in the prompt twice, once flattened and once in the span.

    Left alone if history is not what the span was built from, which is what a mismatched opener at
    the counted position means. Two copies of a turn is a poor context; a turn silently truncated
    to resolve a disagreement about which record is real is a worse one.
    """
    openers = [message.content for message in resumed if message.kind == "user"]
    if not openers:
        return list(history)
    starts = [index for index, (role, _) in enumerate(history) if role == "user"]
    if len(starts) < len(openers):
        logger.debug("history holds fewer turns than the carry reinstates; keeping all of it")
        return list(history)
    cut = starts[-len(openers)]
    if history[cut][1] != openers[0]:
        logger.debug("the resumed turn does not appear in history; keeping every message of it")
        return list(history)
    return list(history[:cut])


_MAX_SHAPE_SUMMARY_CHARS = 32
"""Characters of a result's own summary kept for the eviction stub's description of it."""


def _result_shape(result: ToolResult) -> str:
    """Describe what a tool result held, in the few words an eviction stub can afford.

    Read off the fields the result already carries rather than off its text. The stub exists to
    cost a fraction of what it replaced, so this has to come to the same handful of words whether
    the payload was a single number or 32KB of rows — a description that grew with the result
    would defeat the mechanism it belongs to. The row count is preferred to the prose summary
    where there is one: what the model needs in order to decide whether to call again is the size
    of what it is missing, not a restatement of the call.

    Where the prose summary is used, it may be a fragment of the payload:
    :meth:`~kedge.agent.tools.ToolResult.note` defaults ``summary`` to the result's first line. That
    is kept rather than trimmed away, and deliberately. The stub is model-bound and nothing else —
    the SSE trail carries a summary, the audit log carries no values, and the model has already read
    the whole result — so no boundary is crossed by thirty-two characters of it, and those thirty-two
    characters are the difference between a stub the model can act on ("6 cells", "reconciliation
    failed") and one that only says something has gone. The eviction it survives is an eviction for
    *cost*, and its cost is about eight tokens.

    Args:
        result: The result about to be recorded in the window.

    Returns:
        A short description. Held inside the window's own
        :data:`~kedge.agent.context.MAX_EVICTED_SHAPE_CHARS` by the caps below rather than by
        assumption — the window re-caps it at render, so a field added here can never grow a stub,
        only crowd out what is already in it.
    """
    parts: list[str] = []
    if result.row_count:
        parts.append(f"{result.row_count} rows")
    elif result.summary:
        summary = " ".join(result.summary.split())
        if len(summary) > _MAX_SHAPE_SUMMARY_CHARS:
            summary = summary[: _MAX_SHAPE_SUMMARY_CHARS - 1].rstrip() + "…"
        parts.append(summary)
    size = result.byte_count
    parts.append(f"{size}B" if size < 1024 else f"{size / 1024:.1f}KB")
    if result.truncated:
        parts.append("truncated")
    return ", ".join(parts)


def _expire_volatile(
    carried: Sequence[ContextMessage], *, from_age: int
) -> tuple[ContextMessage, ...]:
    """Evict the content of every carried result that has stopped describing anything.

    The axis is :class:`~kedge.agent.tools.Volatility`, and the question it asks is the only one
    that matters here: called again with the same arguments an hour later, could the answer have
    changed? For the workbook and the analysis it could not — kedge never writes to either — so
    ``inspect_workbook``, ``sample_data``, ``profile_column`` and ``read_range`` keep what they
    said. For a live kernel it plainly could, so ``probe``, ``list_cells``, ``reconcile`` and the
    mutating tools do not. A name this build does not offer answers volatile, which is the safe
    way round.

    Evicted, never removed: the endpoint rejects a request whose assistant message carries
    ``tool_calls`` with no result against each id, so the message has to stay. What it renders as
    is the stub — the call, its arguments, the shape of what came back, and an invitation to make
    the call again — which is exactly what wants saying about a result that has expired.

    Args:
        carried: The span :meth:`~kedge.agent.context.ConversationWindow.suspend` handed back.
        from_age: The oldest leg to leave alone, in
            :attr:`~kedge.agent.context.ContextMessage.carried_age`. ``0`` expires the whole span,
            which is what a turn that answered wants; ``1`` spares the leg that has just run, which
            is what resuming a paused turn wants, and still expires everything behind it — a leg
            with an age of one or more has lived through a turn boundary, and a turn boundary is
            where the user edits the notebook.

    Returns:
        The same span with its expired results marked. Marked on copies, so a window that is still
        assembling — the ``finally`` this runs from can be reached before the turn has ended — is
        never edited underneath itself.
    """
    return tuple(
        replace(message, evicted=True)
        if message.kind == "tool_result"
        and not message.evicted
        and message.carried_age >= from_age
        and volatility_of(message.tool_name or "") is not Volatility.SESSION_STABLE
        else message
        for message in carried
    )


def _describe(exc: Exception) -> str:
    """Name an exception for a user whose only view of it is the chat pane.

    Plenty of what reaches the catch-all carries no message at all -- every httpx timeout mapped
    from a bare ``TimeoutError`` among them -- and interpolating one of those into a sentence gets
    the user "The turn stopped unexpectedly: ReadTimeout: .", which reads like a second bug on top
    of the first. Where there is no message, the type name is the whole of what can honestly be
    said, so it is the whole of what is said.
    """
    detail = str(exc).strip()
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


def _log_spend(turn_id: str, meter: _Meter) -> None:
    """Record what the turn cost, and how much of the prompt the endpoint served from cache.

    At INFO because it is the number anyone tuning the context window needs and there is nowhere
    else to read it: the ``DoneEvent`` carries a single total, and the cache hit rate -- the one
    figure that says whether a 50-step turn is expensive or nearly free -- is not in it at all.
    """
    if meter.steps_reported:
        share = 100.0 * meter.reported_cached / max(meter.reported_prompt, 1)
        logger.info(
            "turn %s spent %d tokens over %d step(s): %d prompt (%d cached, %.0f%%), %d output%s",
            turn_id,
            meter.total,
            meter.steps,
            meter.reported_prompt,
            meter.reported_cached,
            share,
            meter.reported_output,
            "" if meter.measured else f"; {meter.steps - meter.steps_reported} step(s) estimated",
        )
        return
    logger.info(
        "turn %s spent about %d tokens over %d step(s): %d prompt, %d output. The endpoint "
        "reported no usage, so these are kedge's own estimate and say nothing about caching.",
        turn_id,
        meter.total,
        meter.steps,
        meter.prompt + meter.pending_prompt,
        meter.output + meter.pending_output,
    )


def _status_detail(exc: Exception, limit: int = 300) -> str:
    """The endpoint's own words for why it refused, without the wrapper around them.

    The SDK's ``message`` on a status error is ``"Error code: 429 - {repr of the whole body}"``,
    so quoting it hands the user a Python dict literal to read. The body almost always carries the
    prose under ``error.message``; that is what is quoted, and only if it is missing does the
    wrapper get used.

    Args:
        exc: The status error the SDK raised.
        limit: Characters to keep. A gateway that returns an HTML error page has no business
            filling the chat pane with it.

    Returns:
        One line, never empty, with any trailing full stop removed so the sentence it is quoted
        into does not end in two of them.
    """
    body = getattr(exc, "body", None)
    text = ""
    if isinstance(body, dict):
        error = body.get("error")
        candidate = error.get("message") if isinstance(error, dict) else body.get("message")
        text = str(candidate or "")
    text = text.strip() or str(getattr(exc, "message", "") or "").strip() or str(exc).strip()
    collapsed = " ".join(text.split()) or type(exc).__name__
    if len(collapsed) > limit:
        collapsed = f"{collapsed[:limit].rstrip()} [truncated]"
    return collapsed.rstrip(" .")


def _safe_arguments(raw: str) -> dict[str, Any]:
    """Decode a tool call's arguments for the activity trail, tolerating a truncated stream.

    ``RecursionError`` is caught alongside the decode error because it is the same event wearing
    a different exception: arguments this function cannot turn into a dictionary. It is not a
    ``JSONDecodeError``, so uncaught it passes :meth:`KedgeAgent._invoke` untouched and lands in
    the loop's catch-all, which the UI renders as **Fatal** — a whole turn ended, and the user
    told the notebook may be broken, over one deeply nested tool call that kedge was only trying
    to summarise. Handed back as a raw argument instead, the tool refuses it and the turn goes on.
    """
    try:
        decoded = json.loads(raw or "{}")
    except (json.JSONDecodeError, RecursionError):
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
