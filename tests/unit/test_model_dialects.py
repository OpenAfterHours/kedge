"""The two wire dialects, and kedge's refusal to let either of them end a turn.

``OpenAIClient`` is the only thing in kedge that knows what a request looks like on the wire, and
since it learned to speak the responses API it is also the only thing that negotiates. Both halves
are tested here against a stand-in SDK: no key, no network, no endpoint.

The negotiation is the part worth pinning. kedge is pointed at whatever the user has, and the two
things it cannot know in advance -- whether ``/responses`` exists at all, and whether the model
will accept a reasoning setting -- are exactly the two that used to end a turn with a raw 400 in
the chat pane.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from kedge.agent.loop import OpenAIClient, responses_delta, responses_input, responses_tools

# ── fakes ────────────────────────────────────────────────────────────────────────────────────


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
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    def __aiter__(self) -> _Stream:
        return self

    async def __anext__(self) -> Any:
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


class FakeSDK:
    """Stands in for ``AsyncOpenAI``, recording what was sent and raising what the test says to.

    ``responses_errors`` is consumed one entry per call: an exception is raised, ``None`` succeeds.
    That is how a downgrade is expressed -- refuse the first attempt, accept the retry.
    """

    def __init__(
        self,
        *,
        responses_errors: list[Exception | None] | None = None,
        responses_events: list[Any] | None = None,
        chat_chunks: list[Any] | None = None,
    ) -> None:
        self.responses_payloads: list[dict[str, Any]] = []
        self.chat_payloads: list[dict[str, Any]] = []
        self._responses_errors = list(responses_errors or [])
        self._responses_events = list(responses_events or [])
        self._chat_chunks = list(chat_chunks or [])
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
        self.chat_payloads.append(payload)
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

    with pytest.raises(BadRequestError):
        await _drain(_client(sdk, reasoning_effort="high"))


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

    with pytest.raises(NotFoundError):
        await _drain(_client(sdk, api="responses"))
    assert not sdk.chat_payloads
