"""The HTTP client for a running marimo server.

Everything kedge assumes about marimo's HTTP API lives in this module and nowhere else. That is
the point of it: the API is undocumented and partly private, so when a marimo bump moves
something, exactly one file needs correcting and the contract tests say which function.

Verified against marimo 0.23.15 (docs/marimo-api.md, which is authoritative over PLAN 1.1-1.3):

============================  ===================================================================
``GET /health``               unauthenticated; ``{"status": "healthy"}``
``GET /sse?session_id=<id>``  creates the kernel session; it survives the stream closing
``GET /api/sessions``         requires auth; ``{id: {"filename": str|None, "path": str|None}}``
``GET /?file=<nb>``           serves ``<marimo-server-token data-token="...">`` for skew protection
``POST /api/kernel/shutdown`` requires auth *and* the skew-protection server token
``POST /api/kernel/execute``  requires auth; exempt from skew protection. **The one exception**
============================  ===================================================================

The execute endpoint is the exception, and a deliberate one: its reply is a streamed
``text/event-stream`` consumed asynchronously, so the client for it is a class with a very
different shape from the small synchronous calls here, and it lives in
:mod:`kedge.notebook.kernel` (PLAN 6.1). :data:`EXECUTE_PATH` is still defined here, so that even
that endpoint's URL is written down in one place and a bump that moves it is found from this
file. Nothing else in kedge may open an HTTP connection to marimo.

This module deliberately does not import marimo, spawn processes, or know what a
:class:`~kedge.workspace.Workspace` is. It speaks HTTP to a base URL with a token.
Process supervision is :mod:`kedge.lifecycle`; driving the kernel is ``notebook/driver.py``.

Every function takes an optional ``client``, so tests drive them through
``httpx.MockTransport`` and callers can share a connection pool.
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from kedge.errors import NotebookError

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

__all__ = [
    "EXECUTE_PATH",
    "MarimoHealthTimeoutError",
    "MarimoLaunchError",
    "MarimoSessionNotFoundError",
    "auth_headers",
    "bootstrap_session",
    "confirm_session",
    "fetch_server_token",
    "health_check",
    "list_sessions",
    "request_shutdown",
    "wait_for_health",
]

EXECUTE_PATH = "/api/kernel/execute"
"""Where code is submitted to a session's kernel.

Declared in marimo's ``_server/api/endpoints/execution.py`` -- not ``execute.py``, whatever
PLAN 1.2 says -- and excluded from the OpenAPI schema, so nothing but this line and
docs/marimo-api.md 5.1 records it. The request itself is built by
:class:`kedge.notebook.kernel.KernelClient`, which imports the path from here.
"""

_IS_WINDOWS_PATHS = Path("A") == Path("a")
"""Whether path comparison on this platform is case-insensitive."""

_SERVER_TOKEN_PATTERN = re.compile(
    r"<marimo-server-token[^>]*\bdata-token\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE
)


class MarimoLaunchError(NotebookError):
    """The marimo subprocess could not be started, or exited before becoming usable."""


class MarimoHealthTimeoutError(MarimoLaunchError):
    """The marimo server did not answer ``GET /health`` within the allowed time.

    A subclass of :class:`MarimoLaunchError` because a server that never comes up is a failed
    launch, and callers supervising a launch want to catch both with one name.
    """


class MarimoSessionNotFoundError(NotebookError):
    """No usable marimo session could be established, confirmed, or listed."""


class ProcessLike(Protocol):
    """The little of a subprocess handle that :func:`wait_for_health` needs.

    Kept as a protocol so this module has no reason to import ``subprocess``.
    """

    returncode: int | None

    def poll(self) -> int | None:
        """Return the exit code if the process has finished, else ``None``."""
        ...


# ── plumbing ─────────────────────────────────────────────────────────────────────────────────


@contextlib.contextmanager
def _http(client: httpx.Client | None, timeout: float | httpx.Timeout) -> Iterator[httpx.Client]:
    if client is not None:
        yield client
        return
    with httpx.Client(timeout=timeout) as owned:
        yield owned


def auth_headers(token: str, session_id: str | None = None) -> dict[str, str]:
    """Return the headers that identify kedge to marimo.

    Two names and no more: ``Authorization: Bearer <token>`` is the edit-mode credential, and
    ``Marimo-Session-Id`` picks the session a request applies to. Public because
    :class:`kedge.notebook.kernel.KernelClient` frames its request the same way and the framing
    is marimo's, not kedge's -- a bump that renames either header is corrected here.

    Args:
        token: The ``--token-password`` the server was launched with.
        session_id: The session to address, omitted when the request is server-wide.

    Returns:
        A fresh mutable mapping the caller may add to.

    References:
        docs/marimo-api.md 5.1 and 5.5.
    """
    headers = {"Authorization": f"Bearer {token}"}
    if session_id:
        headers["Marimo-Session-Id"] = session_id
    return headers


def _normalise(path: Path) -> str:
    """Return a comparable form of a path: absolute where possible, case-folded on Windows."""
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        resolved = path
    text = str(resolved)
    return text.casefold() if _IS_WINDOWS_PATHS else text


def _log_tail(log_path: Path | None, lines: int = 15) -> str:
    if log_path is None or not log_path.is_file():
        return ""
    try:
        tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except OSError:
        return ""
    if not tail:
        return ""
    body = "\n".join(f"  {line}" for line in tail)
    return f"\nLast lines of {log_path}:\n{body}"


# ── liveness ─────────────────────────────────────────────────────────────────────────────────


def health_check(
    base_url: str, *, timeout: float = 2.0, client: httpx.Client | None = None
) -> bool:
    """Return whether a marimo server is answering at ``base_url``.

    ``/health`` is unauthenticated, so this works against any marimo, not just ours. It is the
    liveness test kedge uses everywhere, in preference to inspecting a PID: PID checks are
    unreliable on Windows and PIDs are recycled, so "that PID exists" is not evidence that our
    server is running, whereas "that port answers /health" is (PLAN 6.2).
    """
    try:
        with _http(client, timeout) as http:
            response = http.get(f"{base_url}/health", timeout=timeout)
    except httpx.HTTPError as exc:
        logger.debug("health check on %s failed: %s", base_url, exc)
        return False
    return response.status_code == httpx.codes.OK


def wait_for_health(
    base_url: str,
    *,
    timeout: float,
    interval: float = 0.25,
    process: ProcessLike | None = None,
    log_path: Path | None = None,
    client: httpx.Client | None = None,
) -> float:
    """Poll ``GET /health`` until the server answers, returning how long it took.

    Bounded by ``timeout``. If the subprocess exits while we are waiting, that is reported
    immediately rather than after the full timeout, with the tail of its log, because "marimo
    died on startup" and "marimo is slow to start" want very different responses from the user.
    The two are told apart by type as well as by message: a death is a bare
    :class:`MarimoLaunchError`, a timeout is :class:`MarimoHealthTimeoutError`.
    """
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            msg = (
                f"marimo exited with code {process.returncode} before serving {base_url}/health."
                f"{_log_tail(log_path)}"
            )
            raise MarimoLaunchError(msg)
        if health_check(base_url, timeout=min(interval * 4, 2.0), client=client):
            elapsed = time.monotonic() - started
            logger.info("marimo is serving %s after %.1fs", base_url, elapsed)
            return elapsed
        time.sleep(interval)

    msg = (
        f"marimo did not answer {base_url}/health within {timeout:g}s. The port may be in use by "
        f"another process, or startup may be blocked.{_log_tail(log_path)}"
    )
    raise MarimoHealthTimeoutError(msg)


# ── sessions ─────────────────────────────────────────────────────────────────────────────────


def list_sessions(
    base_url: str,
    token: str,
    *,
    timeout: float = 5.0,
    client: httpx.Client | None = None,
) -> dict[str, dict[str, Any]]:
    """Return the server's active sessions, keyed by session id.

    Each value carries ``filename`` and ``path`` for the notebook that session has open, and the
    mapping is empty when no client has ever connected. Raises
    :class:`MarimoSessionNotFoundError` if the request is rejected — for an authenticated
    request, that means the server on that port is not ours.
    """
    try:
        with _http(client, timeout) as http:
            response = http.get(
                f"{base_url}/api/sessions",
                headers=auth_headers(token),
                timeout=timeout,
            )
    except httpx.HTTPError as exc:
        msg = f"could not list marimo sessions at {base_url}: {exc}"
        raise MarimoSessionNotFoundError(msg) from exc

    if response.status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
        msg = (
            f"the marimo server at {base_url} rejected our token; it is not the server kedge "
            f"started and must not be touched"
        )
        raise MarimoSessionNotFoundError(msg)
    if response.status_code != httpx.codes.OK:
        msg = f"listing marimo sessions at {base_url} returned HTTP {response.status_code}"
        raise MarimoSessionNotFoundError(msg)

    payload = response.json()
    if not isinstance(payload, dict):
        msg = (
            f"listing marimo sessions at {base_url} returned {type(payload).__name__}, "
            f"expected an object"
        )
        raise MarimoSessionNotFoundError(msg)
    return payload


def bootstrap_session(
    base_url: str,
    token: str,
    session_id: str,
    *,
    hold_seconds: float = 2.0,
    connect_timeout: float = 10.0,
    client: httpx.Client | None = None,
) -> None:
    """Create a kernel session on the server by connecting a transport to it.

    A freshly launched ``marimo edit`` has *no* sessions. ``/api/sessions`` returns ``{}`` and
    ``/api/kernel/execute`` returns HTTP 500 until a client connects with a session id, so
    waiting for a session to appear on its own hangs forever. The session is created by the
    connection, and the id is chosen by the caller — which is a better fit for PLAN 2.9 than the
    plan's own design, because kedge asserts its session id rather than discovering one.

    ``GET /sse?session_id=<id>`` is the cheapest transport that does this: hold it open briefly,
    then close it. The session survives the stream closing. Note the interaction with
    ``--session-ttl``: a TTL closes the session that long after the last transport disconnects,
    which would silently invalidate the id kedge drives the kernel through, so kedge leaves the
    TTL unset by default.

    Only one session may exist in edit mode — bootstrapping a second id evicts the first — so the
    id must be stable across reconnects. :attr:`kedge.workspace.Workspace.session_id` is derived,
    not generated, for exactly this reason.
    """
    timeout = httpx.Timeout(
        connect=connect_timeout, read=hold_seconds, write=connect_timeout, pool=connect_timeout
    )
    deadline = time.monotonic() + hold_seconds
    logger.debug("bootstrapping marimo session %s at %s", session_id, base_url)
    try:
        with (
            _http(client, timeout) as http,
            http.stream(
                "GET",
                f"{base_url}/sse",
                params={"session_id": session_id},
                headers=auth_headers(token, session_id),
                timeout=timeout,
            ) as response,
        ):
            if response.status_code != httpx.codes.OK:
                msg = (
                    f"marimo refused the session bootstrap at {base_url}/sse with HTTP "
                    f"{response.status_code}; the token may be wrong, or the server may not be a "
                    f"marimo edit server at the version this module is verified against -- "
                    f"GET /sse exists from 0.23.15 onwards and not before"
                )
                raise MarimoSessionNotFoundError(msg)
            # A fully buffered response needs no draining; a real SSE stream does.
            if not response.is_stream_consumed:
                for _chunk in response.iter_raw():
                    if time.monotonic() >= deadline:
                        break
    except httpx.TimeoutException:
        # Expected and fine: the stream stayed open and quiet for the whole hold window, which
        # is exactly what a healthy SSE transport does.
        logger.debug("session bootstrap stream held open for the full %.1fs", hold_seconds)
    except httpx.HTTPError as exc:
        msg = f"could not bootstrap a marimo session at {base_url}/sse: {exc}"
        raise MarimoSessionNotFoundError(msg) from exc


def confirm_session(
    base_url: str,
    token: str,
    session_id: str,
    notebook_path: Path,
    *,
    timeout: float = 15.0,
    interval: float = 0.5,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Confirm that ``session_id`` exists and has ``notebook_path`` open.

    Two separate checks, and both matter. That the id is present proves the bootstrap worked.
    That the notebook matches proves kedge asserted its session onto the server it meant to —
    the check exists precisely so that kedge never operates on a notebook it did not open. The
    sole session is never accepted merely for being the only one (PLAN 2.9).
    """
    wanted = _normalise(notebook_path)
    deadline = time.monotonic() + timeout
    sessions: dict[str, dict[str, Any]] = {}

    while True:
        sessions = list_sessions(base_url, token, client=client)
        info = sessions.get(session_id)
        if info is not None:
            reported = info.get("path") or info.get("filename")
            if reported is None:
                # The server can report a null filename for a session it has not finished
                # binding to a file. Accept it, but say so.
                logger.warning("marimo session %s reports no filename yet", session_id)
                return info
            if _normalise(Path(str(reported))) == wanted:
                logger.info("confirmed marimo session %s on %s", session_id, notebook_path)
                return info
            msg = (
                f"marimo session {session_id} at {base_url} has {reported!r} open, not "
                f"{notebook_path}. kedge will not drive a notebook it did not open."
            )
            raise MarimoSessionNotFoundError(msg)
        if time.monotonic() >= deadline:
            break
        time.sleep(interval)

    listed = ", ".join(sessions) or "none"
    msg = (
        f"marimo session {session_id} did not appear at {base_url} within {timeout:g}s "
        f"(sessions present: {listed}). A session is only created when a transport connects, so "
        f"the bootstrap request to /sse did not take effect."
    )
    raise MarimoSessionNotFoundError(msg)


# ── shutdown ─────────────────────────────────────────────────────────────────────────────────


def fetch_server_token(
    base_url: str,
    token: str,
    notebook_path: Path | None = None,
    *,
    timeout: float = 5.0,
    client: httpx.Client | None = None,
) -> str | None:
    """Scrape the skew-protection server token from the notebook page, or return ``None``.

    Every POST except ``/api/kernel/execute`` is behind marimo's skew protection, which wants a
    ``Marimo-Server-Token`` header. The value is embedded in the page as
    ``<marimo-server-token data-token="...">``. Failure is not an error: the caller falls back to
    terminating the process.
    """
    params = {"access_token": token}
    if notebook_path is not None:
        params["file"] = str(notebook_path)
    try:
        with _http(client, timeout) as http:
            response = http.get(f"{base_url}/", params=params, timeout=timeout)
    except httpx.HTTPError as exc:
        logger.debug("could not fetch the marimo page for its server token: %s", exc)
        return None
    if response.status_code != httpx.codes.OK:
        logger.debug(
            "marimo page returned HTTP %d while fetching the server token", response.status_code
        )
        return None
    match = _SERVER_TOKEN_PATTERN.search(response.text)
    if match is None:
        logger.debug("no marimo-server-token element in the page at %s", base_url)
        return None
    return match.group(1)


def request_shutdown(
    base_url: str,
    token: str,
    *,
    session_id: str | None = None,
    notebook_path: Path | None = None,
    server_token: str | None = None,
    timeout: float = 5.0,
    client: httpx.Client | None = None,
) -> bool:
    """Ask the marimo server to shut itself down, returning whether it accepted.

    The most graceful teardown available and the only one that behaves identically on every
    platform: an authenticated HTTP request rather than a signal. Unlike the execute endpoint it
    is *not* exempt from skew protection, so the ``Marimo-Server-Token`` is fetched first.
    Failure is never fatal — the caller escalates to terminating the process.
    """
    if server_token is None:
        server_token = fetch_server_token(
            base_url, token, notebook_path, timeout=timeout, client=client
        )

    headers = auth_headers(token, session_id)
    if server_token is not None:
        headers["Marimo-Server-Token"] = server_token

    try:
        with _http(client, timeout) as http:
            response = http.post(
                f"{base_url}/api/kernel/shutdown", headers=headers, timeout=timeout
            )
    except httpx.HTTPError as exc:
        # Routinely expected: the server drops the connection as it shuts down.
        logger.debug("shutdown request to %s did not complete cleanly: %s", base_url, exc)
        return False
    accepted = response.status_code == httpx.codes.OK
    if not accepted:
        logger.debug("shutdown request to %s returned HTTP %d", base_url, response.status_code)
    return accepted
