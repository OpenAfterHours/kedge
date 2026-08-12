"""Tests for the HTTP transport to a running marimo kernel.

Two halves, and they are independent on purpose. The SSE parser is a pure function over lines,
so it is tested against the specification's dispatch rules with no server anywhere near it; the
client is tested through ``httpx.MockTransport``, which lets a test assert on the request marimo
would receive and hand back a stream that is truncated, hostile, or simply wrong.

The parser cases are not hypothetical. A multi-line payload really does arrive as one ``data:``
line per line of text, and a failure really does arrive as ``stderr`` followed by ``done`` with
``success == false`` and no ``error`` event at all (docs/marimo-api.md 5.2). A parser that got
either wrong would corrupt a result rather than fail, which is why they are pinned here.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from kedge.marimo_http import EXECUTE_PATH
from kedge.notebook.kernel import (
    DEFAULT_TIMEOUT_SECONDS,
    KernelClient,
    KernelTransportError,
    SseEvent,
    SseParser,
    iter_sse_events,
    result_from_events,
)

TOKEN = "a-token-nobody-else-could-guess"
BASE = "http://127.0.0.1:2718"
SESSION = "kedge-abc123"


def _sse(*events: tuple[str, str]) -> bytes:
    """Render events as a wire-format ``text/event-stream`` body."""
    blocks = []
    for name, data in events:
        lines = [f"event: {name}"]
        lines += [f"data: {line}" for line in data.split("\n")]
        blocks.append("\n".join(lines))
    return ("\n\n".join(blocks) + "\n\n").encode()


def _client(handler: object, **kwargs: object) -> KernelClient:
    transport = httpx.MockTransport(handler)  # ty: ignore[invalid-argument-type]
    return KernelClient(
        BASE,
        token=TOKEN,
        session_id=SESSION,
        client=httpx.AsyncClient(transport=transport),
        **kwargs,  # ty: ignore[invalid-argument-type]
    )


def _responder(body: bytes, *, status: int = 200) -> object:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body)

    return handle


# ── SSE parsing ──────────────────────────────────────────────────────────────────────────────


def test_a_blank_line_dispatches_the_buffered_event() -> None:
    parser = SseParser()

    assert parser.feed("event: done") is None
    assert parser.feed('data: {"success": true}') is None
    event = parser.feed("")

    assert event is not None
    assert event.name == "done"
    assert event.json() == {"success": True}


def test_repeated_data_fields_are_rejoined_with_newlines() -> None:
    """The specification splits a multi-line payload one ``data:`` per line of text."""
    events = list(iter_sse_events(["event: stdout", "data: first", "data: second", ""]))

    assert [event.data for event in events] == ["first\nsecond"]


def test_a_comment_line_is_ignored() -> None:
    events = list(iter_sse_events([": keep-alive", "event: done", "data: {}", ""]))

    assert [event.name for event in events] == ["done"]


def test_exactly_one_leading_space_is_stripped_from_a_value() -> None:
    """Two spaces means the payload really did begin with one, and indentation is meaningful."""
    events = list(iter_sse_events(["event: stdout", "data:  indented", ""]))

    assert events[0].data == " indented"


def test_a_field_with_no_colon_is_a_field_with_an_empty_value() -> None:
    events = list(iter_sse_events(["data", ""]))

    assert events[0].data == ""
    assert events[0].name == "message"


def test_data_with_no_event_name_dispatches_as_message() -> None:
    events = list(iter_sse_events(["data: bare", ""]))

    assert events[0] == SseEvent(name="message", data="bare")


def test_reconnection_fields_are_dropped() -> None:
    """``id`` and ``retry`` resume a stream, and a resumed stream would replay someone else's."""
    events = list(iter_sse_events(["id: 7", "retry: 3000", "event: done", "data: {}", ""]))

    assert len(events) == 1
    assert events[0].name == "done"


def test_a_stream_that_ends_without_a_blank_line_still_dispatches() -> None:
    events = list(iter_sse_events(["event: done", 'data: {"success": true}']))

    assert [event.name for event in events] == ["done"]


def test_closing_an_empty_parser_dispatches_nothing() -> None:
    assert SseParser().close() is None
    assert list(iter_sse_events([])) == []
    assert list(iter_sse_events(["", "", ""])) == []


def test_a_non_json_payload_decodes_to_none_rather_than_raising() -> None:
    assert SseEvent(name="stdout", data="not json at all").json() is None


# ── folding events into a result ─────────────────────────────────────────────────────────────


def test_console_chunks_are_concatenated_without_separators() -> None:
    """Each chunk carries its own newlines; inserting any would corrupt the delimited payload."""
    result = result_from_events(
        [
            SseEvent(name="stdout", data='{"data": "one"}'),
            SseEvent(name="stdout", data='{"data": "two\\n"}'),
            SseEvent(name="done", data='{"success": true}'),
        ]
    )

    assert result.stdout == "onetwo\n"
    assert result.success is True
    assert result.saw_done is True


def test_a_failure_arrives_as_stderr_then_done_with_success_false() -> None:
    """There is no ``error`` event; a parser waiting for one waits forever."""
    result = result_from_events(
        [
            SseEvent(name="stderr", data='{"data": "NameError: total"}'),
            SseEvent(name="done", data='{"success": false}'),
        ]
    )

    assert result.success is False
    assert result.saw_done is True
    assert "NameError" in result.stderr


def test_the_done_payload_carries_the_cell_output() -> None:
    result = result_from_events(
        [
            SseEvent(
                name="done",
                data='{"success": true, "output": {"mimetype": "text/plain", "data": "42"}}',
            )
        ]
    )

    assert result.output_mimetype == "text/plain"
    assert result.output_data == "42"


def test_a_done_event_without_an_output_object_leaves_the_output_unset() -> None:
    result = result_from_events([SseEvent(name="done", data='{"success": true, "output": null}')])

    assert result.output_mimetype is None
    assert result.output_data is None


def test_a_done_event_that_is_not_json_is_not_a_success() -> None:
    result = result_from_events([SseEvent(name="done", data="the server said something else")])

    assert result.saw_done is True
    assert result.success is False


def test_a_stream_with_no_done_event_is_not_a_success_however_it_looks() -> None:
    """Truncated is not the same as failed, and neither is the same as succeeded."""
    result = result_from_events([SseEvent(name="stdout", data='{"data": "looks fine"}')])

    assert result.saw_done is False
    assert result.success is False


def test_an_unexpected_event_name_is_ignored_rather_than_fatal() -> None:
    result = result_from_events(
        [
            SseEvent(name="kernel-ready", data="{}"),
            SseEvent(name="done", data='{"success": true}'),
        ]
    )

    assert result.success is True


def test_a_console_event_that_is_not_the_documented_shape_is_passed_through() -> None:
    """Still text the user may need to see, so it is not dropped on the floor."""
    result = result_from_events(
        [
            SseEvent(name="stdout", data="bare text"),
            SseEvent(name="done", data='{"success": true}'),
        ]
    )

    assert result.stdout == "bare text"


# ── the client ───────────────────────────────────────────────────────────────────────────────


def test_the_client_is_bound_to_one_session_for_its_whole_life() -> None:
    client = KernelClient(f"{BASE}/", token=TOKEN, session_id=SESSION)

    assert client.base_url == BASE, "a trailing slash would double up in every URL"
    assert client.session_id == SESSION
    assert client.execute_url == f"{BASE}{EXECUTE_PATH}"


def test_the_headers_frame_the_request_the_way_marimo_expects() -> None:
    client = KernelClient(BASE, token=TOKEN, session_id=SESSION)

    headers = client.headers()

    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["Marimo-Session-Id"] == SESSION
    assert headers["Content-Type"] == "application/json"
    assert headers["Accept"] == "text/event-stream"


def test_execute_is_the_one_post_exempt_from_skew_protection() -> None:
    """Every other POST needs Marimo-Server-Token; sending it here is not merely unnecessary."""
    client = KernelClient(BASE, token=TOKEN, session_id=SESSION)

    assert "Marimo-Server-Token" not in client.headers()


def test_the_token_is_never_in_a_traceback() -> None:
    client = KernelClient(BASE, token=TOKEN, session_id=SESSION)

    assert TOKEN not in repr(client)
    assert "<redacted>" in repr(client)


async def test_execute_submits_the_code_and_returns_what_the_kernel_streamed() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, content=_sse(("stdout", '{"data": "hello\\n"}'), ("done", '{"success": true}'))
        )

    async with _client(handle) as client:
        result = await client.execute("print('hello')")

    assert result.success is True
    assert result.stdout == "hello\n"
    assert seen[0].url.path == EXECUTE_PATH
    assert seen[0].headers["Marimo-Session-Id"] == SESSION
    assert b"print('hello')" in seen[0].content


async def test_a_failed_submission_is_a_successful_round_trip() -> None:
    """Code that reached the kernel and failed there is a result, not a transport error."""
    body = _sse(("stderr", '{"data": "boom"}'), ("done", '{"success": false}'))

    async with _client(_responder(body)) as client:
        result = await client.execute("1/0")

    assert result.ok is False
    assert result.stderr == "boom"


async def test_a_rejected_token_says_the_server_is_not_ours() -> None:
    async with _client(_responder(b"nope", status=401)) as client:
        with pytest.raises(KernelTransportError) as excinfo:
            await client.execute("pass")

    assert "rejected kedge's token" in str(excinfo.value)


async def test_a_forbidden_execute_is_reported_the_same_way() -> None:
    async with _client(_responder(b"", status=403)) as client:
        with pytest.raises(KernelTransportError, match="rejected kedge's token"):
            await client.execute("pass")


async def test_a_missing_endpoint_points_at_the_version_pin() -> None:
    """404 on this path means an older marimo, not a bad request."""
    async with _client(_responder(b"", status=404)) as client:
        with pytest.raises(KernelTransportError) as excinfo:
            await client.execute("pass")

    assert "0.23.15" in str(excinfo.value)


async def test_a_500_explains_that_the_session_has_to_be_bootstrapped_first() -> None:
    """The single biggest gotcha in the API: a fresh server has no session (docs 7.2)."""
    async with _client(_responder(b"", status=500)) as client:
        with pytest.raises(KernelTransportError) as excinfo:
            await client.execute("pass")

    message = str(excinfo.value)
    assert "no such session exists" in message
    assert "/sse?session_id=" in message


async def test_an_unexpected_status_is_reported_with_the_body() -> None:
    async with _client(_responder(b"gateway is unhappy", status=502)) as client:
        with pytest.raises(KernelTransportError) as excinfo:
            await client.execute("pass")

    assert "HTTP 502" in str(excinfo.value)
    assert "gateway is unhappy" in str(excinfo.value)


async def test_a_very_long_error_body_is_truncated_from_the_front() -> None:
    """The end of a traceback is the useful part; the front is stack frames nobody reads."""
    body = ("filler " * 500 + "THE ACTUAL CAUSE").encode()

    async with _client(_responder(body, status=502)) as client:
        with pytest.raises(KernelTransportError) as excinfo:
            await client.execute("pass")

    message = str(excinfo.value)
    assert "THE ACTUAL CAUSE" in message
    assert "..." in message
    assert len(message) < 2000


async def test_a_blank_error_body_adds_no_server_said_section() -> None:
    async with _client(_responder(b"   ", status=502)) as client:
        with pytest.raises(KernelTransportError) as excinfo:
            await client.execute("pass")

    assert "Server said" not in str(excinfo.value)


async def test_an_unreachable_kernel_names_the_url_and_what_to_check() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with _client(handle) as client:
        with pytest.raises(KernelTransportError) as excinfo:
            await client.execute("pass")

    message = str(excinfo.value)
    assert EXECUTE_PATH in message
    assert "still running" in message


async def test_a_final_event_with_no_trailing_blank_line_is_still_dispatched() -> None:
    """A server that closes straight after its last event never sends the dispatching blank."""
    body = b'event: done\ndata: {"success": true}'

    async with _client(_responder(body)) as client:
        result = await client.execute("pass")

    assert result.success is True
    assert result.saw_done is True


async def test_a_stream_cut_short_is_an_error_not_a_silent_failure() -> None:
    """Without the terminal ``done`` the outcome is unknown, and unknown must not read as failed."""
    body = _sse(("stdout", '{"data": "half a resu"}'))

    async with _client(_responder(body)) as client:
        with pytest.raises(KernelTransportError) as excinfo:
            await client.execute("pass")

    assert "without a terminal 'done' event" in str(excinfo.value)


async def test_a_truncated_stream_quotes_whatever_stderr_arrived() -> None:
    body = _sse(("stderr", '{"data": "MemoryError"}'))

    async with _client(_responder(body)) as client:
        with pytest.raises(KernelTransportError) as excinfo:
            await client.execute("pass")

    assert "MemoryError" in str(excinfo.value)


async def test_a_per_call_timeout_overrides_the_clients_own() -> None:
    seen: list[httpx.Timeout] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, content=_sse(("done", '{"success": true}')))

    async with _client(handle, timeout=90.0) as client:
        await client.execute("pass")
        await client.execute("pass", timeout=5.0)

    assert seen[0]["read"] == 90.0
    assert seen[1]["read"] == 5.0
    assert seen[1]["connect"] == 10.0, "the connect timeout is not the read timeout"


async def test_a_client_it_created_is_closed_and_a_borrowed_one_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    borrowed = httpx.AsyncClient(
        transport=httpx.MockTransport(_responder(b""))  # ty: ignore[invalid-argument-type]
    )
    client = KernelClient(BASE, token=TOKEN, session_id=SESSION, client=borrowed)

    await client.aclose()
    assert borrowed.is_closed is False, "closing a shared pool would break its other users"

    # The client the context manager opens for itself is not reachable afterwards -- aclose()
    # drops the reference -- so catch it on the way out of the constructor instead.
    created: list[httpx.AsyncClient] = []

    class _Owned(httpx.AsyncClient):
        def __init__(self, **kwargs: Any) -> None:
            transport = httpx.MockTransport(_responder(b""))  # ty: ignore[invalid-argument-type]
            super().__init__(transport=transport, **kwargs)
            created.append(self)

    monkeypatch.setattr(httpx, "AsyncClient", _Owned)

    owned = KernelClient(BASE, token=TOKEN, session_id=SESSION)
    async with owned:
        pass

    assert len(created) == 1
    assert created[0].is_closed, "a connection pool kedge opened must not be left open"

    await owned.aclose()  # idempotent: the second close has nothing left to close

    await borrowed.aclose()


def test_the_default_timeout_is_long_enough_for_real_work() -> None:
    """A kernel call can run a whole notebook; the default is not an HTTP request's default."""
    assert DEFAULT_TIMEOUT_SECONDS >= 60.0
