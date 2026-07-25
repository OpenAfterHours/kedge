"""Spawn, health-poll, session-bootstrap, and tear down the marimo subprocess kedge owns.

kedge never attaches to a marimo server it did not start. The registry scan that ``marimo-pair``
performs exists so an agent can latch onto whatever notebook a user happens to have open, and
anyone running kedge will plausibly have other marimo notebooks open for unrelated work
(PLAN 2.9, CONVENTIONS non-negotiable 5). So: pick a free port, generate a token, spawn, assert
our own session id onto that server, and target it explicitly for the rest of the run.

The registry is not read at all, for our own process or anyone else's. It is only written for
servers started with ``--no-token``, and kedge always launches with a token, so a kedge-owned
marimo never appears in it (docs/marimo-api.md 7.4). The marker file is the sole mechanism for
spotting a server left behind by a crashed run.

This module talks to marimo over HTTP only. It does not import ``marimo._code_mode`` and must
never do so — driving the kernel is ``notebook/driver.py``'s job alone, and this module's job
stops at "the process is up, the session exists, here is its id".

Every assumption about marimo's HTTP API is isolated in one small function, so correcting one
against a future marimo is a one-function change. All of them are verified against 0.23.15 in
docs/marimo-api.md, which is authoritative over PLAN 1.1-1.3.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import httpx

from kedge.errors import NotebookError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from kedge.workspace import MarimoMarker, Workspace

logger = logging.getLogger(__name__)

__all__ = [
    "MARIMO_PIN",
    "MarimoHealthTimeoutError",
    "MarimoLaunchError",
    "MarimoSessionNotFoundError",
    "OrphanOutcome",
    "bootstrap_session",
    "build_marimo_argv",
    "cleanup_orphan",
    "confirm_session",
    "establish_session",
    "fetch_server_token",
    "generate_token",
    "health_check",
    "installed_marimo_version",
    "launch_marimo",
    "list_sessions",
    "pick_free_port",
    "register_teardown",
    "request_shutdown",
    "stop_marimo",
    "teardown",
    "wait_for_health",
]

MARIMO_PIN = "0.23.15"
"""The exact marimo version kedge is verified against.

Pinned rather than ranged for two reasons: ``_code_mode`` is a private API with no versioning
guarantees (PLAN 6.1), and ``GET /sse`` — the WebSocket-free session bootstrap this module
depends on — does not exist before 0.23.15.
"""

_TOKEN_BYTES = 32
_IS_WINDOWS = os.name == "nt"
_SERVER_TOKEN_PATTERN = re.compile(
    r"<marimo-server-token[^>]*\bdata-token\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE
)


class MarimoLaunchError(NotebookError):
    """The marimo subprocess could not be started, or exited before becoming usable."""


class MarimoHealthTimeoutError(MarimoLaunchError):
    """The marimo server did not answer ``GET /health`` within the allowed time."""


class MarimoSessionNotFoundError(NotebookError):
    """No usable marimo session could be established or confirmed."""


# ── port and token ───────────────────────────────────────────────────────────────────────────


def pick_free_port(host: str = "127.0.0.1") -> int:
    """Return a free TCP port on ``host``.

    Binds to port 0, reads back what the OS assigned, and releases it. There is an unavoidable
    race between releasing and marimo binding; it is small, and losing it produces a clear
    "address in use" failure from marimo rather than a silent misconnection. ``SO_REUSEADDR`` is
    deliberately not set, so a port still in ``TIME_WAIT`` is not handed out.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        port = int(sock.getsockname()[1])
    logger.debug("selected free port %d on %s", port, host)
    return port


def generate_token() -> str:
    """Return a cryptographically random token for marimo's ``--token-password``.

    A fresh token per launch is the whole of kedge's auth story: the server binds to loopback
    and the token is the only credential (PLAN 2.9). It is also the proof of ownership used by
    :func:`cleanup_orphan`.
    """
    return secrets.token_urlsafe(_TOKEN_BYTES)


def installed_marimo_version() -> str | None:
    """Return the installed marimo version, or ``None`` if it is not installed."""
    try:
        return metadata.version("marimo")
    except metadata.PackageNotFoundError:
        return None


# ── argv ─────────────────────────────────────────────────────────────────────────────────────


def build_marimo_argv(
    notebook_path: Path,
    *,
    port: int,
    token_file: Path | None = None,
    token: str | None = None,
    host: str = "127.0.0.1",
    timeout_minutes: float | None = 30.0,
    session_ttl_seconds: int | None = None,
    watch: bool = True,
) -> list[str]:
    """Build the ``marimo edit`` command line for a kedge-owned server.

    Invoked as ``python -m marimo`` rather than the ``marimo`` console script, so the server
    lands in the same interpreter as kedge regardless of what is on ``PATH``.

    Pass ``token_file`` in preference to ``token``: on Windows any local process can read another
    process's command line, so a token passed as ``--token-password`` is readable by anything
    running as this user, whereas ``--token-password-file`` keeps it in a file under the user
    profile. The two are mutually exclusive in marimo and exactly one must be given here.

    ``--timeout`` is the load-bearing flag and is in *minutes*: it shuts the server down after
    that long with no connection, which is what makes an orphaned server self-clearing after a
    crash. On Windows that matters more than usual, because no signal is reliably delivered to a
    process whose parent was force-quit (PLAN 2.9, 6.2).

    ``--no-sandbox`` is explicit so that inline PEP 723 metadata in a notebook can never divert
    the kernel into a separate uv environment where kedge and polars are not importable.
    """
    if (token_file is None) == (token is None):
        msg = "exactly one of token_file or token must be given; marimo rejects both together"
        raise ValueError(msg)

    argv = [
        sys.executable,
        "-m",
        "marimo",
        "edit",
        str(notebook_path),
        "--headless",
        "--host",
        host,
        "--port",
        str(port),
    ]
    if token_file is not None:
        argv += ["--token-password-file", str(token_file)]
    else:
        argv += ["--token-password", str(token)]
    argv += ["--skip-update-check", "--no-sandbox"]
    if watch:
        argv.append("--watch")
    if timeout_minutes is not None:
        argv += ["--timeout", str(timeout_minutes)]
    if session_ttl_seconds is not None:
        argv += ["--session-ttl", str(session_ttl_seconds)]
    return argv


def redacted_argv(argv: list[str]) -> list[str]:
    """Return ``argv`` with any inline token replaced, for logging and error messages."""
    out = list(argv)
    for index, item in enumerate(out[:-1]):
        if item == "--token-password":
            out[index + 1] = "<redacted>"
    return out


# ── HTTP surface ─────────────────────────────────────────────────────────────────────────────
#
# Everything kedge assumes about marimo's HTTP API lives below. Verified against 0.23.15:
#
#   GET  /health               unauthenticated; {"status": "healthy"}
#   GET  /sse?session_id=<id>  creates the session; it survives the stream closing
#   GET  /api/sessions         requires auth; {session_id: {"filename": str|None, "path": str|None}}
#   GET  /?file=<nb>           serves <marimo-server-token data-token="..."> for skew protection
#   POST /api/kernel/shutdown  requires auth AND the Marimo-Server-Token header


@contextlib.contextmanager
def _http(client: httpx.Client | None, timeout: float | httpx.Timeout) -> Iterator[httpx.Client]:
    if client is not None:
        yield client
        return
    with httpx.Client(timeout=timeout) as owned:
        yield owned


def _auth_headers(token: str, session_id: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if session_id:
        headers["Marimo-Session-Id"] = session_id
    return headers


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


def list_sessions(
    base_url: str,
    token: str,
    *,
    timeout: float = 5.0,
    client: httpx.Client | None = None,
) -> dict[str, dict[str, Any]]:
    """Return the server's active sessions, keyed by session id.

    Each value carries ``filename`` and ``path`` for the notebook that session has open, and the
    mapping is empty when no client has ever connected. Raises :class:`MarimoSessionNotFoundError` if
    the request is rejected — for an authenticated request, that means the server on that port is
    not ours.
    """
    try:
        with _http(client, timeout) as http:
            response = http.get(
                f"{base_url}/api/sessions",
                headers=_auth_headers(token),
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
    id must be stable across reconnects. :attr:`Workspace.session_id` is derived, not generated,
    for exactly this reason.
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
                headers=_auth_headers(token, session_id),
                timeout=timeout,
            ) as response,
        ):
            if response.status_code != httpx.codes.OK:
                msg = (
                    f"marimo refused the session bootstrap at {base_url}/sse with HTTP "
                    f"{response.status_code}; the token may be wrong or the server may not "
                    f"be a marimo {MARIMO_PIN} edit server"
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

    The best teardown primitive available and the only one that behaves identically on every
    platform: an authenticated HTTP request rather than a signal. Unlike the execute endpoint it
    is *not* exempt from skew protection, so the ``Marimo-Server-Token`` is fetched first.
    Failure is never fatal — the caller escalates to terminating the process.
    """
    if server_token is None:
        server_token = fetch_server_token(
            base_url, token, notebook_path, timeout=timeout, client=client
        )

    headers = _auth_headers(token, session_id)
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


def wait_for_health(
    base_url: str,
    *,
    timeout: float,
    interval: float = 0.25,
    process: subprocess.Popen[bytes] | None = None,
    log_path: Path | None = None,
    client: httpx.Client | None = None,
) -> float:
    """Poll ``GET /health`` until the server answers, returning how long it took.

    Bounded by ``timeout``. If the subprocess exits while we are waiting, that is reported
    immediately rather than after the full timeout, with the tail of its log, because "marimo
    died on startup" and "marimo is slow to start" want very different responses from the user.
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


def _normalise(path: Path) -> str:
    """Return a comparable form of a path: absolute where possible, case-folded on Windows."""
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        resolved = path
    text = str(resolved)
    return text.casefold() if _IS_WINDOWS else text


# ── launch ───────────────────────────────────────────────────────────────────────────────────


def launch_marimo(
    workspace: Workspace,
    *,
    kedge_version: str,
    client: httpx.Client | None = None,
) -> subprocess.Popen[bytes]:
    """Spawn marimo for ``workspace`` and wait for it to serve, attaching it to the workspace.

    On return the workspace has a :class:`~kedge.workspace.MarimoSession` carrying the port,
    token and pid, and a marker file has been written. The kernel session does *not* exist yet —
    call :func:`establish_session` for that.
    """
    settings = workspace.config.marimo
    workspace.ensure_dirs()

    port = settings.port or pick_free_port(settings.host)
    token = generate_token()
    token_file = workspace.write_token_file(token)
    argv = build_marimo_argv(
        workspace.notebook_path,
        port=port,
        token_file=token_file,
        host=settings.host,
        timeout_minutes=settings.timeout_minutes,
        session_ttl_seconds=settings.session_ttl_seconds,
        watch=settings.watch,
    )
    log_path = workspace.logs_dir / f"marimo-{port}.log"
    logger.info("starting marimo: %s", " ".join(redacted_argv(argv)))

    # Output goes to a file rather than a pipe: nothing in kedge drains marimo's stdout, and an
    # undrained pipe eventually blocks the child. The file is also what the failure messages
    # quote from when startup goes wrong.
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if _IS_WINDOWS else 0
    try:
        with log_path.open("ab") as log_handle:
            process = subprocess.Popen(
                argv,
                cwd=workspace.project_dir,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
            )
    except OSError as exc:
        workspace.clear_token_file()
        msg = f"could not start marimo ({' '.join(redacted_argv(argv))}): {exc}"
        raise MarimoLaunchError(msg) from exc

    base_url = f"http://{settings.host}:{port}"
    workspace.attach_marimo(
        host=settings.host,
        port=port,
        token=token,
        pid=process.pid,
        process=process,
    )
    # Written before the health wait, so a run killed during startup still leaves a trail.
    workspace.write_marker(kedge_version=kedge_version)

    try:
        wait_for_health(
            base_url,
            timeout=settings.health_timeout_seconds,
            interval=settings.health_poll_interval_seconds,
            process=process,
            log_path=log_path,
            client=client,
        )
    except NotebookError:
        _stop_process(process.pid, process=process, grace=settings.shutdown_grace_seconds)
        workspace.detach_marimo()
        workspace.clear_marker()
        workspace.clear_token_file()
        raise

    logger.info("marimo ready for %s at %s", workspace.notebook_path, base_url)
    return process


def establish_session(workspace: Workspace, *, client: httpx.Client | None = None) -> str:
    """Create and confirm this workspace's kernel session, recording the id on the workspace.

    Uses the workspace's derived, stable session id rather than a fresh one, because edit mode
    holds at most one session and a second id would evict the first.
    """
    session = workspace.require_marimo()
    session_id = workspace.session_id
    bootstrap_session(session.base_url, session.token, session_id, client=client)
    confirm_session(
        session.base_url,
        session.token,
        session_id,
        workspace.notebook_path,
        client=client,
    )
    workspace.set_session_id(session_id)
    return session_id


# ── teardown ─────────────────────────────────────────────────────────────────────────────────


def _stop_process(
    pid: int,
    *,
    process: subprocess.Popen[bytes] | None = None,
    grace: float,
    base_url: str | None = None,
    client: httpx.Client | None = None,
) -> bool:
    """Stop a process and its children, escalating. Returns whether it is gone.

    Windows has no ``SIGTERM``. ``Popen.terminate()`` maps to ``TerminateProcess``, which is an
    immediate kill with no chance to clean up *and* which does not touch the process's children,
    so terminating the marimo server alone would leave its kernel subprocess running and holding
    resources. The escalation here is therefore:

    1. ``CTRL_BREAK_EVENT`` — the nearest Windows equivalent of a polite ``SIGTERM``, and the
       reason the process is spawned with ``CREATE_NEW_PROCESS_GROUP``: the event goes to a whole
       process group, so without a group of its own we would be signalling ourselves.
    2. ``taskkill /F /T`` — ``/T`` is the part that matters, killing the process tree rather than
       just the parent. There is no ``Popen`` API for this.

    On POSIX the same shape is ``SIGTERM`` then ``SIGKILL``.
    """
    if process is not None and process.poll() is not None:
        return True

    try:
        if _IS_WINDOWS:
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        logger.debug("could not signal pid %d: %s", pid, exc)

    if _wait_for_exit(pid, process=process, timeout=grace, base_url=base_url, client=client):
        logger.debug("marimo pid %d exited after the graceful signal", pid)
        return True

    logger.warning("marimo pid %d did not exit within %.1fs; killing the process tree", pid, grace)
    if _IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                check=False,
                capture_output=True,
                timeout=grace,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.error("taskkill on pid %d failed: %s", pid, exc)
    elif process is not None:
        process.kill()
    else:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)

    return _wait_for_exit(pid, process=process, timeout=grace, base_url=base_url, client=client)


def _wait_for_exit(
    pid: int,
    *,
    process: subprocess.Popen[bytes] | None,
    timeout: float,
    base_url: str | None = None,
    client: httpx.Client | None = None,
) -> bool:
    """Wait for a marimo server to be gone, by whatever evidence is available.

    With a process handle, wait on it. Without one — an orphan from a previous run, where all we
    have is a marker file — prefer watching the port go quiet over watching the PID, because the
    question that actually matters is "is the server still there", and on Windows a PID answers
    that badly. The PID poll is the last resort.
    """
    if process is not None:
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
        return True

    deadline = time.monotonic() + timeout
    while True:
        if base_url is not None:
            if not health_check(base_url, timeout=1.0, client=client):
                return True
        elif not _pid_exists(pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


def _pid_exists(pid: int) -> bool:
    """Return whether a process id is still running.

    Deliberately does *not* use ``os.kill(pid, 0)``. That is the standard POSIX liveness idiom,
    but on Windows CPython implements ``os.kill`` for any signal other than the two console
    control events by calling ``TerminateProcess`` — so ``os.kill(pid, 0)`` would kill the
    process it was meant to be asking about. The Windows path instead opens a handle and asks
    whether it is signalled.
    """
    if pid <= 0:
        return False

    if _IS_WINDOWS:
        import ctypes  # Windows-only, and not worth importing on POSIX

        synchronize = 0x00100000
        wait_timeout = 0x00000102
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        try:
            # Signalled means exited; still waiting means running.
            return bool(kernel32.WaitForSingleObject(handle, 0) == wait_timeout)
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def stop_marimo(workspace: Workspace, *, client: httpx.Client | None = None) -> bool:
    """Stop the marimo server attached to ``workspace``, clearing its marker and token file.

    Asks the server to shut itself down first, which is graceful and platform-independent, and
    escalates to process termination only if it is still there afterwards. Safe to call when
    nothing is attached.
    """
    session = workspace.marimo
    if session is None:
        return False

    grace = workspace.config.marimo.shutdown_grace_seconds
    request_shutdown(
        session.base_url,
        session.token,
        session_id=session.session_id,
        notebook_path=session.notebook_path,
        client=client,
    )
    stopped = _wait_for_exit(
        session.pid,
        process=session.process,
        timeout=min(grace, 5.0),
        base_url=session.base_url,
        client=client,
    )
    if not stopped:
        stopped = _stop_process(
            session.pid,
            process=session.process,
            grace=grace,
            base_url=session.base_url,
            client=client,
        )

    workspace.detach_marimo()
    with contextlib.suppress(Exception):
        workspace.clear_marker()
    with contextlib.suppress(Exception):
        workspace.clear_token_file()
    logger.info("marimo on port %d stopped: %s", session.port, stopped)
    return stopped


def teardown(workspace: Workspace, *, client: httpx.Client | None = None) -> None:
    """Tear down everything this workspace owns. Never raises."""
    try:
        stop_marimo(workspace, client=client)
    except Exception:  # teardown runs from atexit; it must not raise
        logger.exception("error while tearing down the marimo server")


def register_teardown(workspace: Workspace) -> None:
    """Register ``atexit`` and signal handlers that tear this workspace down.

    These cover the normal exits only. A force-quit, a power cut, or ``taskkill /F`` on kedge
    itself delivers nothing we can handle, which is precisely why the marker file and marimo's
    own ``--timeout`` exist: the next launch cleans up what the last one could not, and an
    unattended orphan eventually shuts itself down (PLAN 2.9).
    """
    atexit.register(teardown, workspace)

    def _handler(signum: int, _frame: object) -> None:
        logger.info("received signal %d; tearing down", signum)
        teardown(workspace)
        raise SystemExit(128 + signum)

    # SIGBREAK is the Windows-only Ctrl-Break. SIGTERM exists on Windows but is never delivered
    # by anything except an explicit os.kill, so registering it there is for completeness only —
    # it is emphatically not a teardown guarantee.
    names = ["SIGINT", "SIGTERM", "SIGBREAK"] if _IS_WINDOWS else ["SIGINT", "SIGTERM", "SIGHUP"]
    for name in names:
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, _handler)


# ── orphan cleanup ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class OrphanOutcome:
    """What :func:`cleanup_orphan` did about a marker left by a previous run."""

    action: Literal["no_marker", "cleared_stale_marker", "stopped_orphan", "left_alone"]
    detail: str
    marker: MarimoMarker | None = None

    @property
    def freed_port(self) -> bool:
        """Whether a port was reclaimed by stopping an orphaned server."""
        return self.action == "stopped_orphan"


def cleanup_orphan(workspace: Workspace, *, client: httpx.Client | None = None) -> OrphanOutcome:
    """Deal with a marimo server left behind by a previous run of this workspace.

    Ownership is established over HTTP, never from the recorded PID:

    * no marker — nothing to do;
    * marker present but ``/health`` silent — the server is gone. Delete the marker and touch no
      process. The recorded PID may since have been recycled by something unrelated, and killing
      it on the strength of a stale file is exactly the bug this ordering exists to avoid;
    * marker present, ``/health`` answers, recorded token accepted — the server is demonstrably
      ours, because that token was generated by us and never left this machine. Stop it;
    * marker present, ``/health`` answers, token rejected — somebody else's marimo has taken the
      port. Leave it strictly alone and delete our marker.
    """
    marker = workspace.read_marker()
    if marker is None:
        return OrphanOutcome(action="no_marker", detail="no marker file from a previous run")

    base_url = marker.base_url
    if not health_check(base_url, client=client):
        workspace.clear_marker()
        workspace.clear_token_file()
        detail = (
            f"a previous run recorded marimo on port {marker.port}, but nothing is answering "
            f"there; removed the stale marker without touching pid {marker.pid}"
        )
        logger.info(
            "cleared stale marker for port %d (pid %d not touched)", marker.port, marker.pid
        )
        return OrphanOutcome(action="cleared_stale_marker", detail=detail, marker=marker)

    try:
        sessions = list_sessions(base_url, marker.token, client=client)
    except MarimoSessionNotFoundError as exc:
        workspace.clear_marker()
        detail = (
            f"something is serving port {marker.port}, but it did not accept the token from our "
            f"marker, so it is not a kedge server ({exc}). Left it alone and removed the marker."
        )
        logger.warning("port %d is held by a marimo server that is not ours", marker.port)
        return OrphanOutcome(action="left_alone", detail=detail, marker=marker)

    if workspace.has_marimo:
        workspace.detach_marimo()
    workspace.attach_marimo(
        host=marker.host,
        port=marker.port,
        token=marker.token,
        pid=marker.pid,
    )
    stopped = stop_marimo(workspace, client=client)
    detail = (
        f"stopped an orphaned kedge marimo server on port {marker.port} "
        f"(pid {marker.pid}, {len(sessions)} session(s)) left by a previous run"
    )
    if not stopped:
        detail += "; it did not confirm exit, so port reuse may still fail"
    logger.info("stopped orphaned marimo on port %d", marker.port)
    return OrphanOutcome(action="stopped_orphan", detail=detail, marker=marker)
