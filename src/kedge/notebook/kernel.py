"""HTTP transport to a running marimo kernel: submit code, reassemble the SSE reply.

This module knows two things and nothing else: how to frame a request to
``POST {base}/api/kernel/execute``, and how to turn the ``text/event-stream`` that comes back
into an :class:`~kedge.notebook.model.ExecResult`. It has no opinion about what the code *says* —
that is :mod:`kedge.notebook.driver`'s job — and it never imports marimo. Keeping the split sharp
is what makes the code generator unit-testable with no server anywhere near it.

The wire format, verified against marimo 0.23.15 (docs/marimo-api.md 5.1 and 5.2):

* headers ``Marimo-Session-Id``, ``Authorization: Bearer <token>``, ``Content-Type``. The
  ``Marimo-Server-Token`` skew header that every *other* POST needs is explicitly not required
  here;
* body ``{"code": "..."}``;
* reply is SSE with exactly three event names — ``stdout``, ``stderr``, ``done``. There is **no**
  ``error`` event: a failure arrives as ``stderr`` text followed by ``done`` with
  ``success == false``, so a parser that waits for an ``error`` event waits forever.

Two traps worth stating plainly. A multi-line payload is split into one ``data:`` line per line
of text, per the SSE specification, and must be rejoined with newlines — the parser here does
that. And the session must already exist: a freshly launched server has none, and this endpoint
answers 500 until a transport has connected with the session id (docs/marimo-api.md 5.3), which
is why :func:`_status_error` says so in as many words rather than reporting a bare 500.

This is the **one** module besides :mod:`kedge.marimo_http` that opens an HTTP connection to
marimo, and the exception is deliberate: an asynchronously streamed reply is a different animal
from marimo_http's small synchronous calls, and folding it in would make that module two modules
wearing one name. The endpoint's URL and its auth framing are still imported from there, so the
"one file to correct after a marimo bump" promise holds for this endpoint too.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

import httpx

from kedge.errors import KernelError
from kedge.marimo_http import EXECUTE_PATH, auth_headers
from kedge.notebook.model import ExecResult

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "EXECUTE_PATH",
    "KernelClient",
    "KernelTransportError",
    "SseEvent",
    "SseParser",
    "iter_sse_events",
    "result_from_events",
]

DEFAULT_TIMEOUT_SECONDS = 180.0
_CONNECT_TIMEOUT_SECONDS = 10.0


class KernelTransportError(KernelError):
    """The kernel could not be reached, or answered something kedge cannot use.

    This is about the pipe, not the code: a refused connection, a rejected token, a missing
    session, a truncated stream. Code that reached the kernel and failed there comes back as a
    successful transport carrying ``success == false``.
    """


# ── SSE parsing ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SseEvent:
    """One dispatched server-sent event: its name and its rejoined data payload."""

    name: str
    data: str

    def json(self) -> Any:
        """Return the payload decoded as JSON, or ``None`` if it is not JSON."""
        try:
            return json.loads(self.data)
        except json.JSONDecodeError:
            logger.debug("SSE %s event carried a non-JSON payload", self.name)
            return None


class SseParser:
    """Incremental ``text/event-stream`` parser, fed one line at a time.

    Follows the dispatch rules from the SSE specification: ``:`` starts a comment, a field is
    ``name: value`` with one optional leading space stripped from the value, repeated ``data``
    fields accumulate and are rejoined with newlines, and a blank line dispatches. Lines arrive
    already stripped of their terminators, which is what ``httpx.Response.aiter_lines`` yields.

    Example:
        >>> parser = SseParser()
        >>> parser.feed("event: done")
        >>> parser.feed('data: {"success": true}')
        >>> parser.feed("").name
        'done'
    """

    __slots__ = ("_data", "_name", "_saw_data")

    def __init__(self) -> None:
        self._name: str | None = None
        self._data: list[str] = []
        self._saw_data = False

    def feed(self, line: str) -> SseEvent | None:
        """Consume one line, returning an event if that line completed one."""
        if line == "":
            return self._dispatch()
        if line.startswith(":"):
            return None

        field, separator, value = line.partition(":")
        if not separator:
            field, value = line, ""
        if value.startswith(" "):
            value = value[1:]

        if field == "event":
            self._name = value
        elif field == "data":
            self._data.append(value)
            self._saw_data = True
        # "id" and "retry" carry reconnection state kedge has no use for: every execute is a
        # fresh one-shot request, and a resumed stream would replay somebody else's output.
        return None

    def close(self) -> SseEvent | None:
        """Dispatch any event left buffered by a stream that ended without a blank line."""
        return self._dispatch()

    def _dispatch(self) -> SseEvent | None:
        if not self._saw_data and self._name is None:
            return None
        event = SseEvent(name=self._name or "message", data="\n".join(self._data))
        self._name = None
        self._data = []
        self._saw_data = False
        return event


def iter_sse_events(lines: Iterable[str]) -> Iterator[SseEvent]:
    """Turn an iterable of terminator-free lines into the events they frame."""
    parser = SseParser()
    for line in lines:
        event = parser.feed(line)
        if event is not None:
            yield event
    trailing = parser.close()
    if trailing is not None:
        yield trailing


def result_from_events(events: Iterable[SseEvent]) -> ExecResult:
    """Fold a stream of events into a single result.

    ``stdout`` and ``stderr`` chunks are concatenated without separators, because each carries a
    slice of the console stream with its own newlines already in place; inserting any would
    corrupt the delimited payload the driver looks for.

    Args:
        events: The events dispatched by one execute response, in arrival order.

    Returns:
        The reassembled result. ``saw_done`` is ``False`` when the stream ended without a
        terminal ``done`` event, which means it was truncated rather than completed.
    """
    stdout: list[str] = []
    stderr: list[str] = []
    success = False
    saw_done = False
    mimetype: str | None = None
    data: str | None = None

    for event in events:
        if event.name == "stdout":
            stdout.append(_console_text(event))
        elif event.name == "stderr":
            stderr.append(_console_text(event))
        elif event.name == "done":
            saw_done = True
            payload = event.json()
            if isinstance(payload, dict):
                success = bool(payload.get("success", False))
                output = payload.get("output")
                if isinstance(output, dict):
                    mimetype = _as_text(output.get("mimetype"))
                    data = _as_text(output.get("data"))
        else:
            logger.debug("ignoring unexpected SSE event %r from the kernel", event.name)

    return ExecResult(
        success=success and saw_done,
        stdout="".join(stdout),
        stderr="".join(stderr),
        output_mimetype=mimetype,
        output_data=data,
        saw_done=saw_done,
    )


def _console_text(event: SseEvent) -> str:
    payload = event.json()
    if isinstance(payload, dict):
        return str(payload.get("data", ""))
    # A console event that is not the documented ``{"data": ...}`` object is still text the user
    # may need to see, so pass it through rather than dropping it on the floor.
    return event.data


def _as_text(value: Any) -> str | None:
    return None if value is None else str(value)


# ── the client ───────────────────────────────────────────────────────────────────────────────


class KernelClient:
    """Submits code to one marimo session and returns what the kernel streamed back.

    One client is bound to one ``(base_url, session_id)`` pair for its whole life. That is
    deliberate: edit mode holds at most one session, and bootstrapping a second session id evicts
    the first, leaving the original answering 500 (docs/marimo-api.md 5.3). Rebinding a client to
    a different session would make that failure look like an intermittent bug.

    Example:
        >>> client = KernelClient("http://127.0.0.1:2718", token="t", session_id="s")
        >>> client.execute_url
        'http://127.0.0.1:2718/api/kernel/execute'
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str,
        session_id: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._session_id = session_id
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    @property
    def base_url(self) -> str:
        """The server origin, with no trailing slash."""
        return self._base_url

    @property
    def session_id(self) -> str:
        """The session every submission from this client runs in."""
        return self._session_id

    @property
    def execute_url(self) -> str:
        """The endpoint code is submitted to."""
        return f"{self._base_url}{EXECUTE_PATH}"

    def headers(self) -> dict[str, str]:
        """Return the request headers, including the bearer token.

        The two marimo-specific names come from :func:`kedge.marimo_http.auth_headers`; the other
        two are ordinary content negotiation. Notably absent is ``Marimo-Server-Token``: execute
        is the only POST exempt from skew protection (docs/marimo-api.md 5.1).
        """
        headers = auth_headers(self._token, self._session_id)
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "text/event-stream"
        return headers

    async def execute(self, code: str, *, timeout: float | None = None) -> ExecResult:
        """Run ``code`` in the session's scratchpad and return the reassembled result.

        The scratchpad is a shallow copy of kernel globals: notebook variables are readable by
        name, and every new top-level binding is discarded when the call returns. Anything the
        caller wants back must therefore be printed, not assigned (PLAN 1.2).

        Args:
            code: Python source to execute. Generated by the driver; never user input directly.
            timeout: Read timeout in seconds for this call only. Defaults to the client's.

        Returns:
            The result of the run, successful or not.

        Raises:
            KernelTransportError: The kernel was unreachable, rejected the request, or closed the
                stream before sending its terminal ``done`` event.
        """
        client = self._ensure_client()
        limits = httpx.Timeout(
            timeout or self._timeout,
            connect=_CONNECT_TIMEOUT_SECONDS,
        )
        logger.debug("submitting %d characters to %s", len(code), self.execute_url)

        try:
            async with client.stream(
                "POST",
                self.execute_url,
                json={"code": code},
                headers=self.headers(),
                timeout=limits,
            ) as response:
                if response.status_code != httpx.codes.OK:
                    body = (await response.aread()).decode("utf-8", errors="replace")
                    raise self._status_error(response.status_code, body)
                result = result_from_events(await _stream_events(response))
        except httpx.HTTPError as exc:
            msg = (
                f"could not reach the marimo kernel at {self.execute_url}: {exc}. Check that the "
                f"server kedge launched is still running."
            )
            raise KernelTransportError(msg) from exc

        if not result.saw_done:
            msg = (
                f"the marimo kernel at {self.execute_url} closed the stream without a terminal "
                f"'done' event, so the submission's outcome is unknown. The kernel was probably "
                f"interrupted or the server exited mid-run.{_tail(result.stderr)}"
            )
            raise KernelTransportError(msg)

        logger.debug(
            "kernel returned success=%s (%d bytes stdout)", result.success, len(result.stdout)
        )
        return result

    def _status_error(self, status: int, body: str) -> KernelTransportError:
        if status in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
            msg = (
                f"the marimo server at {self._base_url} rejected kedge's token (HTTP {status}). "
                f"It is not the server kedge started, or the token has been rotated."
            )
        elif status == httpx.codes.NOT_FOUND:
            msg = (
                f"{self.execute_url} does not exist on the server at {self._base_url} "
                f"(HTTP 404). kedge is pinned to marimo 0.23.15; an older marimo does not serve "
                f"this endpoint."
            )
        elif status == httpx.codes.INTERNAL_SERVER_ERROR:
            msg = (
                f"the marimo kernel returned HTTP 500 for session {self._session_id!r}. The usual "
                f"cause is that no such session exists: a freshly launched server has none, and "
                f"one is created only when a transport connects with that id "
                f"(GET {self._base_url}/sse?session_id=...). Bootstrap the session before "
                f"executing code."
            )
        else:
            msg = f"the marimo kernel at {self.execute_url} returned HTTP {status}."
        return KernelTransportError(f"{msg}{_tail(body)}")

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
            self._owns_client = True
        return self._client

    async def aclose(self) -> None:
        """Close the underlying HTTP client, if this instance created it."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> KernelClient:
        self._ensure_client()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        # The token is a credential; it has no business in a traceback.
        return (
            f"KernelClient(base_url={self._base_url!r}, session_id={self._session_id!r}, "
            f"token=<redacted>)"
        )


async def _stream_events(response: httpx.Response) -> list[SseEvent]:
    """Drain a streaming response, dispatching events as their lines arrive."""
    parser = SseParser()
    events: list[SseEvent] = []
    async for line in response.aiter_lines():
        event = parser.feed(line)
        if event is not None:
            events.append(event)
    trailing = parser.close()
    if trailing is not None:
        events.append(trailing)
    return events


def _tail(text: str, limit: int = 1500) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    if len(stripped) > limit:
        stripped = "..." + stripped[-limit:]
    return f"\nServer said:\n{stripped}"
