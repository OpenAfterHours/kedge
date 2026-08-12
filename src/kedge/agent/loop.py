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

Retries are capped. A cell rejected by the validation gate three times stops being a conversation
the model can win on its own, so kedge surfaces it to the user rather than burning the rest of the
budget rephrasing the same mistake.

**A turn that stops without answering keeps what it learnt.** The step budget is a check-in, not a
wall: at :data:`DEFAULT_MAX_STEPS` model round trips the loop pauses and asks whether to carry on,
and the turn's tool traffic is held against the session and seeded into the next turn's window.
The same applies to a Stop, and to a turn the validation cap ends. Only prose survives in the
server's history, so without that hand-off a model that had just spent forty calls reading a
workbook would come back to the next message knowing nothing about it — and would spend the
forty again.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
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
    return None


def chat_deltas(chunk: Any) -> list[ChatDelta]:
    """Translate one streamed chat-completions chunk into the fragments the loop reassembles.

    Chat completions streams anonymous deltas: prose and tool-call pieces arrive in the same
    envelope, and ``index`` is the only thing tying a call's name to the arguments that follow.
    """
    deltas: list[ChatDelta] = []
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

        Raises:
            AgentError: The endpoint timed out, the connection to it failed, or it refused the
                request outright. All are recoverable: the loop reports them and leaves the
                conversation intact.
        """
        from openai import (
            APIConnectionError,
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

        False for anything that is not about the dialect or about reasoning, because degrading
        around every 400 would turn a real complaint -- an unknown model, a malformed schema --
        into a turn that silently did nothing and said why to nobody.
        """
        if self._use_responses and not self._pinned and _missing_route(exc):
            logger.info(
                "the endpoint has no responses API (%s); using chat completions from here on", exc
            )
            self._use_responses = False
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


@dataclass(slots=True)
class _Meter:
    """Approximate token accounting for one turn, reported on the ``DoneEvent``."""

    prompt: int = 0
    output: int = 0

    @property
    def total(self) -> int:
        """Prompt plus output."""
        return self.prompt + self.output


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
        """Per session: the messages of a turn that stopped before the model answered."""

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
                    f"The turn stopped unexpectedly: {_describe(exc)}. The notebook has "
                    f"not been left half-changed — every notebook edit is a single atomic flush."
                ),
                recoverable=False,
            )
        yield DoneEvent(turn_id=request.turn_id, tokens_used=meter.total)

    # ── one turn ─────────────────────────────────────────────────────────────────────────

    async def _turn(
        self, request: TurnRequest, cancel: CancelToken, meter: _Meter
    ) -> AsyncIterator[AnyEvent]:
        """Work the turn to an answer, or to the point where it needs the user.

        Every path out of the step loop other than the model answering leaves the window held for
        the session, so the next message continues this turn rather than starting a colder one.
        That is done in ``finally`` because the paths are not all returns: a Stop and a failing
        endpoint both leave through an exception, and both have usually spent real work getting
        wherever they got to.
        """
        yield StatusEvent(phase="analysing")
        cancel.raise_if_cancelled()

        tools = self._registry_for(request.session_id)
        state = await self._notebook_state()
        tools.refresh(state)
        window = self._window_for(request, state)
        attempts: dict[str, int] = {}
        answered = False

        try:
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
                    return

            # The budget is spent but the work is not lost: the window goes on the shelf, the
            # question goes to the user, and the pause is put to the model as its own last word so
            # that "continue" reads as the answer to something it said rather than a non sequitur.
            paused = _pause_message(self._max_steps)
            window.add_assistant(paused)
            logger.info("turn %s paused after its %d step budget", request.turn_id, self._max_steps)
            yield PausedEvent(message=paused, steps=self._max_steps)
        finally:
            self._remember(request.session_id, window)
            self._carry(request.session_id, window, answered=answered)

    async def _complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        model: str | None,
        cancel: CancelToken,
        reply: _Reply,
    ) -> AsyncIterator[AnyEvent]:
        """Stream one completion, emitting prose as it arrives and collecting tool calls.

        Pulled one fragment at a time through :func:`_abandon_if_cancelled` rather than with a
        plain ``async for``, so that Stop lands while the model is still thinking rather than only
        once it has something to say. See that function for why the difference is not academic.
        """
        parts: dict[int, dict[str, str]] = {}
        stream = self._client.stream(
            model=model or self._model, messages=messages, tools=self._tools
        ).__aiter__()
        try:
            while True:
                try:
                    delta = await _abandon_if_cancelled(stream.__anext__(), cancel)
                except StopAsyncIteration:
                    break
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

        resumed = self._suspended.pop(request.session_id, None)
        history = [(message.role, message.content) for message in request.history]
        if resumed is not None:
            history = _history_before(history, resumed)
        window.load_history(history)
        if resumed is not None:
            window.resume(resumed)
            logger.info(
                "session %s resumes a paused turn: %d messages carried, %d of them tool results",
                request.session_id,
                len(resumed),
                sum(1 for message in resumed if message.kind == "tool_result"),
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
        """Hold an unfinished turn's tool traffic for the next message, or let it go.

        Held only when there is something in it worth holding. A turn that stopped before the
        model called anything has nothing the server's own history will not replay, and carrying
        an empty shell would mean trimming a turn out of history to make room for less than it
        said (:func:`_history_before`).
        """
        if answered:
            self._suspended.pop(session_id, None)
            return
        carried = window.suspend()
        if not any(message.kind == "tool_result" for message in carried):
            self._suspended.pop(session_id, None)
            return
        self._suspended[session_id] = carried
        logger.info(
            "session %s is holding %d messages from an unfinished turn for the next one",
            session_id,
            len(carried),
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


def _history_before(
    history: Sequence[tuple[str, str]], resumed: Sequence[ContextMessage]
) -> list[tuple[str, str]]:
    """Drop the persisted record of the turn ``resumed`` carries, which is about to replace it.

    The server stores a turn as one user message and the prose that answered it, so a paused turn
    is already in history — flattened, with the tool calls that motivated the prose gone and the
    interleaving with them gone too. The carried messages are that same turn intact, so the record
    is replaced rather than kept alongside and read twice.

    Left alone if the opening message cannot be found, which means history is not what the paused
    turn was built from. Two copies of a turn is a poor context; a turn silently truncated to
    resolve a disagreement about which one is real is a worse one.
    """
    opener = next((message.content for message in resumed if message.kind == "user"), None)
    if opener is None:
        return list(history)
    for index in range(len(history) - 1, -1, -1):
        role, content = history[index]
        if role == "user" and content == opener:
            return list(history[:index])
    logger.debug("the resumed turn does not appear in history; keeping every message of it")
    return list(history)


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
