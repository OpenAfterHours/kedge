"""The two wire dialects, and kedge's refusal to let either of them end a turn.

``OpenAIClient`` is the only thing in kedge that knows what a request looks like on the wire, and
since it learned to speak the responses API it is also the only thing that negotiates. Both halves
are tested here against a stand-in SDK: no key, no network, no endpoint.

The negotiation is the part worth pinning. kedge is pointed at whatever the user has, and the two
things it cannot know in advance -- whether ``/responses`` exists at all, and whether the model
will accept a reasoning setting -- are exactly the two that used to end a turn with a raw 400 in
the chat pane.

The transport tests at the foot pin the same property one layer down. The SDK translates a timeout
around the *request* and not around the body, so a stall part-way through an answer used to arrive
as a bare ``httpx.ReadTimeout`` and reach the user as "The turn stopped unexpectedly: ReadTimeout:"
-- a sentence with no cause in it, no remedy, and nothing to search for.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from kedge.agent.loop import (
    AgentError,
    OpenAIClient,
    _describe,
    _status_detail,
    responses_delta,
    responses_input,
    responses_tools,
)
from kedge.errors import KedgeError

# ── fakes ────────────────────────────────────────────────────────────────────────────────────


def _not_found() -> type[Exception]:
    from openai import NotFoundError

    return NotFoundError


def _status_error(kind: type[Exception], status: int, message: str) -> Exception:
    """Build a real SDK error, so the client is tested against what it will actually catch."""
    request = httpx.Request("POST", "http://127.0.0.1:1/v1/responses")
    return kind(message, response=httpx.Response(status, request=request), body=None)  # type: ignore[call-arg]


def _text_event(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="response.output_text.delta", delta=text)


def _call_opened(index: int, call_id: str, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="response.output_item.added",
        output_index=index,
        item=SimpleNamespace(type="function_call", call_id=call_id, name=name),
    )


def _call_arguments(index: int, fragment: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="response.function_call_arguments.delta", output_index=index, delta=fragment
    )


def _chat_chunk(text: str) -> SimpleNamespace:
    delta = SimpleNamespace(content=text, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


class _Stream:
    """An opened stream. An exception among the events is raised where it sits.

    That is how a stall part-way through an answer is expressed, and the position matters: by the
    time the body is being drained the SDK's error translation is behind us, so what the loop
    actually meets is a raw httpx exception rather than an ``APIError``.
    """

    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def __aiter__(self) -> _Stream:
        return self

    async def __anext__(self) -> Any:
        if not self._events:
            raise StopAsyncIteration
        event = self._events.pop(0)
        if isinstance(event, Exception):
            raise event
        return event


class FakeSDK:
    """Stands in for ``AsyncOpenAI``, recording what was sent and raising what the test says to.

    ``responses_errors`` is consumed one entry per call: an exception is raised, ``None`` succeeds.
    That is how a downgrade is expressed -- refuse the first attempt, accept the retry.

    ``chat_refuses_effort`` models the endpoint that started all this: it does not reject the
    reasoning parameter, it rejects the *effort it has already applied by default*, so a request
    that says nothing is refused just as firmly as one asking for ``"high"``. Only an explicit
    ``"none"`` satisfies it.
    """

    def __init__(
        self,
        *,
        responses_errors: list[Exception | None] | None = None,
        responses_events: list[Any] | None = None,
        chat_chunks: list[Any] | None = None,
        chat_refuses_effort: bool = False,
    ) -> None:
        self.responses_payloads: list[dict[str, Any]] = []
        self.chat_payloads: list[dict[str, Any]] = []
        self._responses_errors = list(responses_errors or [])
        self._responses_events = list(responses_events or [])
        self._chat_chunks = list(chat_chunks or [])
        self._chat_refuses_effort = chat_refuses_effort
        self.responses = SimpleNamespace(create=self._create_response)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create_chat))

    async def _create_response(self, **payload: Any) -> _Stream:
        self.responses_payloads.append(payload)
        if self._responses_errors:
            error = self._responses_errors.pop(0)
            if error is not None:
                raise error
        return _Stream(list(self._responses_events))

    async def _create_chat(self, **payload: Any) -> _Stream:
        from openai import BadRequestError

        self.chat_payloads.append(payload)
        if (
            self._chat_refuses_effort
            and payload.get("tools")
            and payload.get("reasoning_effort") != "none"
        ):
            raise _status_error(
                BadRequestError,
                400,
                "Function tools with reasoning_effort are not supported for gpt-5.6-terra in "
                "/v1/chat/completions. To use function tools, use /v1/responses or set "
                "reasoning_effort to 'none'.",
            )
        return _Stream(list(self._chat_chunks))

    async def close(self) -> None:
        return None


def _client(sdk: FakeSDK, **kwargs: Any) -> OpenAIClient:
    return OpenAIClient(base_url="http://127.0.0.1:1/v1", api_key="k", client=sdk, **kwargs)


async def _drain(client: OpenAIClient, *, tools: list[dict[str, Any]] | None = None) -> list[Any]:
    return [
        delta
        async for delta in client.stream(
            model="m", messages=[{"role": "user", "content": "hello"}], tools=tools or []
        )
    ]


# ── translation ──────────────────────────────────────────────────────────────────────────────


def test_a_plain_exchange_translates_role_for_role() -> None:
    assert responses_input(
        [
            {"role": "system", "content": "you are kedge"},
            {"role": "user", "content": "translate D2:D40"},
        ]
    ) == [
        {"role": "system", "content": "you are kedge"},
        {"role": "user", "content": "translate D2:D40"},
    ]


def test_a_tool_call_becomes_its_own_item_alongside_the_prose() -> None:
    # Chat completions hangs tool calls off one assistant message; responses models the turn as a
    # list of output items, so the prose and each call are siblings.
    items = responses_input(
        [
            {
                "role": "assistant",
                "content": "Reading the reference sheet.",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "probe", "arguments": '{"sheet": "Ref"}'},
                    }
                ],
            }
        ]
    )

    assert items == [
        {"role": "assistant", "content": "Reading the reference sheet."},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "probe",
            "arguments": '{"sheet": "Ref"}',
        },
    ]


def test_an_assistant_turn_that_is_only_a_tool_call_carries_no_empty_message() -> None:
    items = responses_input(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c",
                        "type": "function",
                        "function": {"name": "probe", "arguments": "{}"},
                    }
                ],
            }
        ]
    )

    assert [item.get("type") for item in items] == ["function_call"]


def test_a_tool_result_is_keyed_by_call_id_rather_than_by_role() -> None:
    assert responses_input([{"role": "tool", "tool_call_id": "call_1", "content": "12 rows"}]) == [
        {"type": "function_call_output", "call_id": "call_1", "output": "12 rows"}
    ]


def test_tool_schemas_are_flattened_and_never_strict() -> None:
    # kedge's tools have genuinely optional arguments, and strict mode demands every property be
    # required -- left unsaid it can default true and reject a schema that is correct.
    flattened = responses_tools(
        [
            {
                "type": "function",
                "function": {
                    "name": "probe",
                    "description": "sample a range",
                    "parameters": {"type": "object", "properties": {"sheet": {"type": "string"}}},
                },
            }
        ]
    )

    assert flattened == [
        {
            "type": "function",
            "name": "probe",
            "description": "sample a range",
            "parameters": {"type": "object", "properties": {"sheet": {"type": "string"}}},
            "strict": False,
        }
    ]


def test_streamed_events_translate_into_the_deltas_the_loop_reassembles() -> None:
    assert responses_delta(_text_event("the haircut ")).text == "the haircut "

    opened = responses_delta(_call_opened(1, "call_9", "propose_cell"))
    assert (opened.index, opened.call_id, opened.name) == (1, "call_9", "propose_cell")

    fragment = responses_delta(_call_arguments(1, '{"name":'))
    assert (fragment.index, fragment.arguments) == (1, '{"name":')


def test_an_event_type_kedge_does_not_know_is_ignored_rather_than_fatal() -> None:
    # The set of event types grows with the SDK, and a turn must not die on a new one.
    assert responses_delta(SimpleNamespace(type="response.audio.delta", delta="...")) is None
    assert responses_delta(SimpleNamespace(type="response.completed")) is None


# ── the dialect, and what happens when it is refused ─────────────────────────────────────────


async def test_responses_is_what_kedge_reaches_for_first() -> None:
    sdk = FakeSDK(responses_events=[_text_event("hello")])

    deltas = await _drain(_client(sdk))

    assert [delta.text for delta in deltas] == ["hello"]
    assert sdk.responses_payloads and not sdk.chat_payloads


async def test_the_configured_reasoning_effort_rides_along() -> None:
    sdk = FakeSDK(responses_events=[_text_event("...")])

    await _drain(_client(sdk, reasoning_effort="low"))

    assert sdk.responses_payloads[0]["reasoning"] == {"effort": "low"}


async def test_nothing_is_said_about_reasoning_unless_it_is_configured() -> None:
    # The parameter is meaningless to a non-reasoning model and rejected outright by some
    # endpoints, so the default is silence rather than a value.
    sdk = FakeSDK(responses_events=[_text_event("...")])

    await _drain(_client(sdk))

    assert "reasoning" not in sdk.responses_payloads[0]


async def test_an_endpoint_with_no_responses_route_falls_back_to_chat_completions() -> None:
    from openai import NotFoundError

    sdk = FakeSDK(
        responses_errors=[_status_error(NotFoundError, 404, "Not Found")],
        chat_chunks=[_chat_chunk("hello from chat")],
    )
    client = _client(sdk)

    assert [delta.text for delta in await _drain(client)] == ["hello from chat"]

    # And not asked again: the endpoint's dialect does not change under us, so probing every turn
    # would be a wasted round trip on every single one.
    await _drain(client)
    assert len(sdk.responses_payloads) == 1
    assert len(sdk.chat_payloads) == 2


async def test_a_refusal_naming_reasoning_retries_without_it() -> None:
    # The failure that started this: "Function tools with reasoning_effort are not supported ...
    # set reasoning_effort to 'none'". It arrived as a fatal error in the chat pane.
    from openai import BadRequestError

    sdk = FakeSDK(
        responses_errors=[
            _status_error(
                BadRequestError,
                400,
                "Function tools with reasoning_effort are not supported for this model",
            )
        ],
        responses_events=[_text_event("recovered")],
    )
    client = _client(sdk, reasoning_effort="high")

    assert [delta.text for delta in await _drain(client)] == ["recovered"]
    assert "reasoning" in sdk.responses_payloads[0]
    assert "reasoning" not in sdk.responses_payloads[1]

    await _drain(client)
    assert "reasoning" not in sdk.responses_payloads[2], "and it is not tried again"


async def test_a_refusal_that_is_not_about_reasoning_is_raised() -> None:
    # Degrading around every 400 would turn a real complaint -- a bad schema, an unknown model --
    # into silence, and the user would see a turn that did nothing rather than a reason.
    from openai import BadRequestError

    sdk = FakeSDK(
        responses_errors=[_status_error(BadRequestError, 400, "unknown model 'nope'")],
        responses_events=[_text_event("never reached")],
    )

    # Raised as an AgentError rather than the SDK's own: the loop reports a KedgeError as
    # recoverable, and the endpoint's complaint has to survive into the message either way.
    with pytest.raises(AgentError, match="unknown model 'nope'"):
        await _drain(_client(sdk, reasoning_effort="high"))


TOOLS = [{"type": "function", "function": {"name": "probe", "parameters": {}}}]


async def test_the_gateway_that_defaults_reasoning_is_answered_with_an_explicit_none() -> None:
    """The failure this whole negotiation exists for, end to end.

    The endpoint has no ``/responses`` route *and* applies a default reasoning effort to the
    model, so kedge arrives at chat completions and is refused for a parameter it never sent.
    Saying nothing cannot fix that. Only ``"none"`` can, which is what the endpoint's own error
    message asks for.
    """
    from openai import NotFoundError

    sdk = FakeSDK(
        responses_errors=[_status_error(NotFoundError, 404, "Not Found")],
        chat_refuses_effort=True,
        chat_chunks=[_chat_chunk("done")],
    )
    client = _client(sdk)

    assert [delta.text for delta in await _drain(client, tools=TOOLS)] == ["done"]

    assert "reasoning_effort" not in sdk.chat_payloads[0], "first it says nothing, as before"
    assert sdk.chat_payloads[1]["reasoning_effort"] == "none", "then it says so explicitly"


async def test_the_working_rung_is_kept_for_the_rest_of_the_session() -> None:
    from openai import NotFoundError

    sdk = FakeSDK(
        responses_errors=[_status_error(NotFoundError, 404, "Not Found")],
        chat_refuses_effort=True,
        chat_chunks=[_chat_chunk("done")],
    )
    client = _client(sdk)
    await _drain(client, tools=TOOLS)
    settled = len(sdk.chat_payloads)

    await _drain(client, tools=TOOLS)

    assert len(sdk.chat_payloads) == settled + 1, "no re-probing once something works"
    assert sdk.chat_payloads[-1]["reasoning_effort"] == "none"


async def test_a_configured_effort_is_tried_before_it_is_given_up_on() -> None:
    # The user asked for it, so it is asked for. It just cannot be the reason the turn dies.
    from openai import NotFoundError

    sdk = FakeSDK(
        responses_errors=[_status_error(NotFoundError, 404, "Not Found")],
        chat_refuses_effort=True,
        chat_chunks=[_chat_chunk("done")],
    )

    await _drain(_client(sdk, reasoning_effort="high"), tools=TOOLS)

    assert [payload.get("reasoning_effort") for payload in sdk.chat_payloads] == ["high", "none"]


async def test_an_endpoint_that_never_complains_stays_on_the_first_rung() -> None:
    # Most endpoints. They must not pay for this negotiation with an extra round trip.
    sdk = FakeSDK(
        responses_errors=[_status_error(_not_found(), 404, "Not Found")],
        chat_chunks=[_chat_chunk("done")],
    )

    await _drain(_client(sdk), tools=TOOLS)

    assert len(sdk.chat_payloads) == 1
    assert "reasoning_effort" not in sdk.chat_payloads[0]


async def test_chat_completions_gives_up_once_the_ladder_is_exhausted() -> None:
    # A model that refuses every rung is a real problem, and the user is told rather than left
    # watching a turn that quietly retried for ever.
    from openai import BadRequestError, NotFoundError

    class AlwaysRefuses(FakeSDK):
        async def _create_chat(self, **payload: Any) -> _Stream:
            self.chat_payloads.append(payload)
            raise _status_error(BadRequestError, 400, "reasoning is not supported here at all")

    sdk = AlwaysRefuses(responses_errors=[_status_error(NotFoundError, 404, "Not Found")])

    with pytest.raises(AgentError, match="reasoning is not supported here at all"):
        await _drain(_client(sdk, reasoning_effort="high"), tools=TOOLS)
    assert [p.get("reasoning_effort") for p in sdk.chat_payloads] == ["high", "none", None]


async def test_pinning_the_dialect_skips_the_probe_entirely() -> None:
    sdk = FakeSDK(chat_chunks=[_chat_chunk("hello")])

    await _drain(_client(sdk, api="chat_completions"))

    assert not sdk.responses_payloads


async def test_a_pinned_responses_endpoint_does_not_fall_back() -> None:
    # Pinning is how a user says "I know what this endpoint is". Quietly downgrading it would
    # hide a broken deployment behind a working-looking turn.
    from openai import NotFoundError

    sdk = FakeSDK(
        responses_errors=[_status_error(NotFoundError, 404, "Not Found")],
        chat_chunks=[_chat_chunk("should not be used")],
    )

    # And the message says the pin is why, rather than sending the user to check base_url.
    with pytest.raises(AgentError, match="pinned to `responses`"):
        await _drain(_client(sdk, api="responses"))
    assert not sdk.chat_payloads


# ── transport ────────────────────────────────────────────────────────────────────────────────


async def test_a_stall_part_way_through_an_answer_names_the_setting_that_governs_it() -> None:
    # The one that reached users as "The turn stopped unexpectedly: ReadTimeout:". The SDK wraps
    # timeouts around the *request*; draining the body is ordinary httpx, so this arrives raw --
    # and with an empty message, because httpx maps it from a bare TimeoutError.
    sdk = FakeSDK(responses_events=[_text_event("here is what I fo"), httpx.ReadTimeout("")])

    with pytest.raises(AgentError) as caught:
        await _drain(_client(sdk, timeout=90.0))

    message = str(caught.value)
    assert "90s" in message
    assert "timeout_seconds" in message
    assert "reasoning_effort" in message
    # The turn is reported and retried, not lost: KedgeError is what the loop treats as
    # recoverable, and a bare ReadTimeout is not one.
    assert isinstance(caught.value, KedgeError)


async def test_a_timeout_opening_the_stream_is_explained_the_same_way() -> None:
    # Here the SDK *has* translated it, into APITimeoutError. Same cause, same remedy, so the same
    # message -- the user should not have to care which side of the first byte they landed on.
    from openai import APITimeoutError

    request = httpx.Request("POST", "http://127.0.0.1:1/v1/responses")
    sdk = FakeSDK(responses_errors=[APITimeoutError(request=request)])

    with pytest.raises(AgentError, match="timeout_seconds"):
        await _drain(_client(sdk))


async def test_a_timeout_is_not_mistaken_for_an_endpoint_without_the_responses_route() -> None:
    # Degrading the dialect over a network problem would pin the blame on the endpoint's shape and
    # leave the user chasing a fallback that was never the issue.
    sdk = FakeSDK(
        responses_events=[httpx.ReadTimeout("")],
        chat_chunks=[_chat_chunk("should not be used")],
    )

    with pytest.raises(AgentError):
        await _drain(_client(sdk))
    assert not sdk.chat_payloads


async def test_a_dropped_connection_points_at_doctor_rather_than_at_the_timeout() -> None:
    # A connection that dies mid-answer is not a timeout and telling the user to raise one would
    # send them the wrong way. doctor is what unwraps a proxy's certificate failure.
    sdk = FakeSDK(
        responses_events=[_text_event("partial"), httpx.RemoteProtocolError("peer closed")]
    )

    with pytest.raises(AgentError) as caught:
        await _drain(_client(sdk))

    message = str(caught.value)
    assert "kedge doctor" in message
    assert "peer closed" in message
    assert "timeout_seconds" not in message


async def test_an_exception_with_no_message_is_named_rather_than_left_dangling() -> None:
    # What produced the trailing "ReadTimeout: ." in the chat pane.
    assert _describe(httpx.ReadTimeout("")) == "ReadTimeout"
    assert _describe(ValueError("no columns")) == "ValueError: no columns"


# ── refusals ─────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kind", "status", "expected"),
    [
        ("RateLimitError", 429, "rate limiting"),
        ("InternalServerError", 503, "its own side"),
        ("AuthenticationError", 401, "API key"),
        ("PermissionDeniedError", 403, "API key"),
        ("BadRequestError", 400, "does not retry a refusal"),
    ],
)
async def test_a_refusal_is_recoverable_and_says_whose_problem_it_is(
    kind: str, status: int, expected: str
) -> None:
    # Left raw these reach the loop's catch-all, which reports every exception as unrecoverable --
    # and app.js renders that as "Fatal". A rate limit is the most recoverable thing there is.
    import openai

    sdk = FakeSDK(responses_errors=[_status_error(getattr(openai, kind), status, "endpoint says")])

    with pytest.raises(AgentError) as caught:
        await _drain(_client(sdk))

    assert isinstance(caught.value, KedgeError)
    message = str(caught.value)
    assert expected in message
    assert "endpoint says" in message


async def test_a_transient_refusal_says_the_waiting_is_already_done() -> None:
    # "Ask again" reads as advice to wait. The SDK has already spent the wait, so say so.
    from openai import RateLimitError

    sdk = FakeSDK(responses_errors=[_status_error(RateLimitError, 429, "slow down")])

    with pytest.raises(AgentError, match="already retried 3 times"):
        await _drain(_client(sdk, max_retries=3))


async def test_a_refusal_the_negotiation_declined_is_explained_rather_than_re_raised_raw() -> None:
    # _recover() declines a 400 that is not about the dialect or about reasoning. That path used
    # to re-raise the SDK error untouched, and an unknown model name is the commonest way to hit it.
    from openai import BadRequestError

    sdk = FakeSDK(
        responses_errors=[
            _status_error(BadRequestError, 400, "The model `gpt-5.6-terra` does not exist")
        ]
    )

    with pytest.raises(AgentError) as caught:
        await _drain(_client(sdk))

    message = str(caught.value)
    assert "gpt-5.6-terra" in message
    assert "`model` under `[model]`" in message
    assert not sdk.chat_payloads


async def test_a_refusal_quotes_the_endpoints_prose_rather_than_the_sdks_wrapper() -> None:
    # The SDK's message is 'Error code: 429 - {the whole body as a repr}'. Quoting that hands the
    # user a Python dict literal to read.
    from openai import RateLimitError

    body = {"error": {"message": "Limit 30000 TPM. Retry after 12s.", "type": "tokens"}}
    request = httpx.Request("POST", "http://127.0.0.1:1/v1/responses")
    exc = RateLimitError(
        f"Error code: 429 - {body!r}", response=httpx.Response(429, request=request), body=body
    )

    assert _status_detail(exc) == "Limit 30000 TPM. Retry after 12s"

    with pytest.raises(AgentError) as caught:
        await _drain(_client(FakeSDK(responses_errors=[exc])))

    message = str(caught.value)
    assert "Limit 30000 TPM. Retry after 12s." in message
    assert "'error':" not in message
    assert ".." not in message


async def test_a_refusal_that_returns_an_error_page_does_not_fill_the_chat_pane() -> None:
    # A gateway answering with HTML has no business being quoted in full.
    from openai import InternalServerError

    body = {"error": {"message": "<html>" + "x" * 5_000 + "</html>"}}
    request = httpx.Request("POST", "http://127.0.0.1:1/v1/responses")
    exc = InternalServerError(
        "Error code: 502", response=httpx.Response(502, request=request), body=body
    )

    detail = _status_detail(exc)
    assert len(detail) < 400
    assert detail.endswith("[truncated]")
