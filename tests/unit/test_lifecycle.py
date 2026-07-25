"""Tests for spawning, health-polling, session bootstrap, and orphan cleanup.

The HTTP surface is exercised through ``httpx.MockTransport`` rather than by monkeypatching our
own functions, so the tests assert on the requests marimo would actually receive.
"""

from __future__ import annotations

import contextlib
import socket
import sys
from pathlib import Path

import httpx
import pytest

from kedge import lifecycle
from kedge.config import Config, LoadedConfig
from kedge.lifecycle import (
    MARIMO_PIN,
    MarimoHealthTimeoutError,
    MarimoLaunchError,
    MarimoSessionNotFoundError,
    bootstrap_session,
    build_marimo_argv,
    cleanup_orphan,
    confirm_session,
    fetch_server_token,
    generate_token,
    health_check,
    installed_marimo_version,
    list_sessions,
    pick_free_port,
    redacted_argv,
    request_shutdown,
    wait_for_health,
)
from kedge.workspace import Workspace

TOKEN = "a-token-nobody-else-could-guess"
BASE = "http://127.0.0.1:2718"


@pytest.fixture
def kedge_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "kedge-home"
    monkeypatch.setenv("KEDGE_HOME", str(home))
    return home


@pytest.fixture
def workbook(tmp_path: Path) -> Path:
    path = tmp_path / "process.xlsx"
    path.write_bytes(b"")
    return path


def _workspace(workbook: Path, config: Config | None = None) -> Workspace:
    workspace = Workspace.for_workbook(
        workbook, loaded_config=LoadedConfig(config=config or Config())
    )
    workspace.ensure_dirs()
    return workspace


class _Server:
    """A stand-in marimo server, recording every request it receives."""

    def __init__(
        self,
        *,
        healthy: bool = True,
        token: str = TOKEN,
        sessions: dict[str, dict[str, str | None]] | None = None,
        server_token: str | None = "skew-token",
    ) -> None:
        self.healthy = healthy
        self.token = token
        self.sessions = sessions if sessions is not None else {}
        self.server_token = server_token
        self.requests: list[httpx.Request] = []

    def _authorised(self, request: httpx.Request) -> bool:
        header = request.headers.get("Authorization", "")
        if header == f"Bearer {self.token}":
            return True
        return request.url.params.get("access_token") == self.token

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path

        if not self.healthy:
            raise httpx.ConnectError("connection refused", request=request)

        if path in ("/health", "/healthz"):
            return httpx.Response(200, json={"status": "healthy"})

        if not self._authorised(request):
            return httpx.Response(401, json={"detail": "Unauthorized"})

        if path == "/sse":
            session_id = request.url.params.get("session_id")
            if session_id:
                self.sessions[session_id] = {"filename": None, "path": None}
            return httpx.Response(200, content=b"event: kernel-ready\ndata: {}\n\n")

        if path == "/api/sessions":
            return httpx.Response(200, json=self.sessions)

        if path == "/":
            element = (
                ""
                if self.server_token is None
                else f'<marimo-server-token data-token="{self.server_token}"></marimo-server-token>'
            )
            return httpx.Response(200, html=f"<html>{element}</html>")

        if path == "/api/kernel/shutdown":
            if request.headers.get("Marimo-Server-Token") != self.server_token:
                return httpx.Response(403, json={"detail": "skew protection"})
            self.healthy = False
            return httpx.Response(200, json={"success": True})

        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]


def _refusing_client() -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return httpx.Client(transport=httpx.MockTransport(handle))


# ── port and token ───────────────────────────────────────────────────────────────────────────


def test_pick_free_port_returns_a_port_that_can_be_bound() -> None:
    port = pick_free_port()

    assert 1024 < port < 65_536
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))


def test_generate_token_is_long_and_unique() -> None:
    tokens = {generate_token() for _ in range(50)}

    assert len(tokens) == 50
    assert all(len(token) >= 32 for token in tokens)


def test_the_installed_marimo_matches_the_pin() -> None:
    """The private _code_mode API and the /sse bootstrap are both version-specific."""
    assert installed_marimo_version() == MARIMO_PIN


# ── argv ─────────────────────────────────────────────────────────────────────────────────────


def test_build_marimo_argv_is_exactly_what_we_expect(tmp_path: Path) -> None:
    notebook = tmp_path / "process.py"
    token_file = tmp_path / "run.token"

    argv = build_marimo_argv(
        notebook,
        port=54321,
        token_file=token_file,
        host="127.0.0.1",
        timeout_minutes=30.0,
        session_ttl_seconds=None,
        watch=True,
    )

    assert argv == [
        sys.executable,
        "-m",
        "marimo",
        "edit",
        str(notebook),
        "--headless",
        "--host",
        "127.0.0.1",
        "--port",
        "54321",
        "--token-password-file",
        str(token_file),
        "--skip-update-check",
        "--no-sandbox",
        "--watch",
        "--timeout",
        "30.0",
    ]


def test_timeout_is_passed_in_minutes_and_can_be_disabled(tmp_path: Path) -> None:
    """--timeout is what makes an orphaned server self-clearing, and marimo reads it as minutes."""
    with_timeout = build_marimo_argv(tmp_path / "n.py", port=1, token=TOKEN, timeout_minutes=45.0)
    assert with_timeout[with_timeout.index("--timeout") + 1] == "45.0"

    without = build_marimo_argv(tmp_path / "n.py", port=1, token=TOKEN, timeout_minutes=None)
    assert "--timeout" not in without


def test_session_ttl_is_only_passed_when_configured(tmp_path: Path) -> None:
    """A TTL would close the session kedge drives the kernel through, so it is off by default."""
    default = build_marimo_argv(tmp_path / "n.py", port=1, token=TOKEN)
    assert "--session-ttl" not in default

    explicit = build_marimo_argv(tmp_path / "n.py", port=1, token=TOKEN, session_ttl_seconds=300)
    assert explicit[explicit.index("--session-ttl") + 1] == "300"


def test_watch_can_be_turned_off(tmp_path: Path) -> None:
    assert "--watch" not in build_marimo_argv(tmp_path / "n.py", port=1, token=TOKEN, watch=False)


def test_token_and_token_file_are_mutually_exclusive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        build_marimo_argv(tmp_path / "n.py", port=1, token=TOKEN, token_file=tmp_path / "t")

    with pytest.raises(ValueError, match="exactly one"):
        build_marimo_argv(tmp_path / "n.py", port=1)


def test_an_inline_token_is_redacted_before_logging(tmp_path: Path) -> None:
    argv = build_marimo_argv(tmp_path / "n.py", port=1, token=TOKEN)

    assert TOKEN not in " ".join(redacted_argv(argv))
    assert "<redacted>" in redacted_argv(argv)


# ── health ───────────────────────────────────────────────────────────────────────────────────


def test_health_check_is_true_when_the_server_answers() -> None:
    server = _Server()

    assert health_check(BASE, client=server.client()) is True
    assert server.paths() == ["/health"]


def test_health_check_needs_no_token() -> None:
    server = _Server(token="a-completely-different-token")

    assert health_check(BASE, client=server.client()) is True


def test_health_check_is_false_when_nothing_is_listening() -> None:
    assert health_check(BASE, client=_refusing_client()) is False


def test_wait_for_health_returns_once_the_server_answers() -> None:
    server = _Server()

    assert wait_for_health(BASE, timeout=2.0, interval=0.01, client=server.client()) >= 0.0


def test_wait_for_health_times_out_with_a_message_naming_the_url() -> None:
    with pytest.raises(MarimoHealthTimeoutError) as excinfo:
        wait_for_health(BASE, timeout=0.2, interval=0.05, client=_refusing_client())

    message = str(excinfo.value)
    assert BASE in message
    assert "port may be in use" in message


def test_wait_for_health_reports_an_early_exit_rather_than_waiting_out_the_timeout(
    tmp_path: Path,
) -> None:
    class _Dead:
        returncode = 3

        def poll(self) -> int:
            return 3

    log = tmp_path / "marimo.log"
    log.write_text("Traceback: address already in use\n", encoding="utf-8")

    with pytest.raises(MarimoLaunchError) as excinfo:
        wait_for_health(
            BASE,
            timeout=30.0,
            interval=0.01,
            process=_Dead(),  # ty: ignore[invalid-argument-type]
            log_path=log,
            client=_refusing_client(),
        )

    message = str(excinfo.value)
    assert "exited with code 3" in message
    assert "address already in use" in message, "the log tail is what makes this diagnosable"


# ── sessions ─────────────────────────────────────────────────────────────────────────────────


def test_list_sessions_is_empty_on_a_freshly_launched_server() -> None:
    """A marimo server creates no session until a transport connects (docs/marimo-api.md 5.3)."""
    server = _Server()

    assert list_sessions(BASE, TOKEN, client=server.client()) == {}


def test_a_server_that_rejects_our_token_is_not_ours() -> None:
    server = _Server(token="somebody-elses-token")

    with pytest.raises(MarimoSessionNotFoundError) as excinfo:
        list_sessions(BASE, TOKEN, client=server.client())

    assert "must not be touched" in str(excinfo.value)


def test_bootstrap_session_creates_the_session_we_asked_for(tmp_path: Path) -> None:
    server = _Server()

    bootstrap_session(BASE, TOKEN, "kedge-abc", hold_seconds=0.05, client=server.client())

    assert "/sse" in server.paths()
    sse = next(request for request in server.requests if request.url.path == "/sse")
    assert sse.url.params["session_id"] == "kedge-abc"
    assert sse.headers["Authorization"] == f"Bearer {TOKEN}"
    assert "kedge-abc" in server.sessions


def test_bootstrap_session_reports_a_refused_bootstrap() -> None:
    server = _Server(token="somebody-elses-token")

    with pytest.raises(MarimoSessionNotFoundError, match="401"):
        bootstrap_session(BASE, TOKEN, "kedge-abc", hold_seconds=0.05, client=server.client())


def test_confirm_session_accepts_a_session_open_on_our_notebook(tmp_path: Path) -> None:
    notebook = tmp_path / "process.py"
    notebook.write_text("", encoding="utf-8")
    server = _Server(sessions={"kedge-abc": {"path": str(notebook), "filename": "process.py"}})

    info = confirm_session(BASE, TOKEN, "kedge-abc", notebook, timeout=0.5, client=server.client())

    assert info["path"] == str(notebook)


def test_confirm_session_refuses_a_session_open_on_a_different_notebook(tmp_path: Path) -> None:
    """kedge must never drive a notebook it did not open (PLAN 2.9)."""
    ours = tmp_path / "ours.py"
    theirs = tmp_path / "someone-elses.py"
    for path in (ours, theirs):
        path.write_text("", encoding="utf-8")
    server = _Server(sessions={"kedge-abc": {"path": str(theirs), "filename": "someone-elses.py"}})

    with pytest.raises(MarimoSessionNotFoundError) as excinfo:
        confirm_session(BASE, TOKEN, "kedge-abc", ours, timeout=0.5, client=server.client())

    assert "did not open" in str(excinfo.value)


def test_confirm_session_never_settles_for_the_only_session_there_is(tmp_path: Path) -> None:
    """The sole session is not accepted merely for being the only one."""
    notebook = tmp_path / "process.py"
    notebook.write_text("", encoding="utf-8")
    server = _Server(
        sessions={"someone-elses-session": {"path": str(notebook), "filename": "process.py"}}
    )

    with pytest.raises(MarimoSessionNotFoundError) as excinfo:
        confirm_session(
            BASE, TOKEN, "kedge-abc", notebook, timeout=0.2, interval=0.05, client=server.client()
        )

    assert "did not appear" in str(excinfo.value)
    assert "someone-elses-session" in str(excinfo.value)


def test_confirm_session_times_out_when_no_session_was_created(tmp_path: Path) -> None:
    notebook = tmp_path / "process.py"
    notebook.write_text("", encoding="utf-8")
    server = _Server()

    with pytest.raises(MarimoSessionNotFoundError, match="did not appear"):
        confirm_session(
            BASE, TOKEN, "kedge-abc", notebook, timeout=0.2, interval=0.05, client=server.client()
        )


# ── shutdown ─────────────────────────────────────────────────────────────────────────────────


def test_fetch_server_token_scrapes_the_skew_protection_token() -> None:
    server = _Server(server_token="abc123")

    assert fetch_server_token(BASE, TOKEN, client=server.client()) == "abc123"


def test_fetch_server_token_returns_none_when_the_page_has_no_token() -> None:
    server = _Server(server_token=None)

    assert fetch_server_token(BASE, TOKEN, client=server.client()) is None


def test_request_shutdown_sends_the_skew_protection_header() -> None:
    """POST /api/kernel/shutdown is not exempt from skew protection, unlike /execute."""
    server = _Server()

    assert request_shutdown(BASE, TOKEN, client=server.client()) is True

    shutdown = next(
        request for request in server.requests if request.url.path == "/api/kernel/shutdown"
    )
    assert shutdown.headers["Marimo-Server-Token"] == "skew-token"
    assert shutdown.headers["Authorization"] == f"Bearer {TOKEN}"


def test_request_shutdown_reports_failure_rather_than_raising() -> None:
    assert request_shutdown(BASE, TOKEN, client=_refusing_client()) is False


# ── orphan cleanup ───────────────────────────────────────────────────────────────────────────


def test_no_marker_means_nothing_to_clean_up(kedge_home: Path, workbook: Path) -> None:
    outcome = cleanup_orphan(_workspace(workbook), client=_refusing_client())

    assert outcome.action == "no_marker"


def _write_marker(workspace: Workspace, *, port: int = 2718, pid: int = 999_999) -> None:
    workspace.attach_marimo(host="127.0.0.1", port=port, token=TOKEN, pid=pid)
    workspace.write_marker(kedge_version="0.1.0")
    workspace.detach_marimo()


def test_a_dead_server_clears_the_marker_and_kills_nothing(
    kedge_home: Path, workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recycled PID must never be killed on the strength of a stale file."""
    workspace = _workspace(workbook)
    _write_marker(workspace)
    killed: list[int] = []
    monkeypatch.setattr(lifecycle, "_stop_process", lambda pid, **_: killed.append(pid) or True)

    outcome = cleanup_orphan(workspace, client=_refusing_client())

    assert outcome.action == "cleared_stale_marker"
    assert killed == []
    assert workspace.read_marker() is None
    assert "not touched" in outcome.detail or "without touching" in outcome.detail


def test_a_live_server_that_accepts_our_token_is_ours_and_is_stopped(
    kedge_home: Path, workbook: Path
) -> None:
    workspace = _workspace(workbook)
    _write_marker(workspace)
    server = _Server()

    outcome = cleanup_orphan(workspace, client=server.client())

    assert outcome.action == "stopped_orphan"
    assert outcome.freed_port is True
    assert "/api/kernel/shutdown" in server.paths()
    assert workspace.read_marker() is None
    assert workspace.marimo is None


def test_a_live_server_that_rejects_our_token_is_left_strictly_alone(
    kedge_home: Path, workbook: Path
) -> None:
    """Someone else's marimo may have taken the port; kedge does not touch what it did not start."""
    workspace = _workspace(workbook)
    _write_marker(workspace)
    server = _Server(token="somebody-elses-token")

    outcome = cleanup_orphan(workspace, client=server.client())

    assert outcome.action == "left_alone"
    assert outcome.freed_port is False
    assert "/api/kernel/shutdown" not in server.paths()
    assert workspace.read_marker() is None


def test_ownership_is_decided_over_http_not_from_the_pid(kedge_home: Path, workbook: Path) -> None:
    """The recorded pid is never consulted to decide whether the server is alive or ours."""
    workspace = _workspace(workbook)
    _write_marker(workspace, pid=1)  # pid 1 exists on POSIX and is nothing to do with us
    server = _Server(token="somebody-elses-token")

    outcome = cleanup_orphan(workspace, client=server.client())

    assert outcome.action == "left_alone"


# ── the private API stays private ────────────────────────────────────────────────────────────


def test_lifecycle_never_imports_marimo() -> None:
    """The private kernel API belongs to notebook/driver.py alone (CONVENTIONS non-negotiable 2).

    This module supervises the process and talks HTTP; it has no business importing marimo, and
    the mentions that remain in it are prose explaining why.
    """
    source = Path(lifecycle.__file__).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith(("#", '"""', "*"))
    )

    assert "import marimo" not in code
    assert "get_context" not in code


# ── contract: a real marimo server ───────────────────────────────────────────────────────────


@pytest.mark.contract
def test_a_real_marimo_server_starts_serves_health_and_shuts_down(
    kedge_home: Path, workbook: Path
) -> None:
    """Spawn a real marimo, health-poll it, bootstrap a session, and tear it down."""
    from kedge.lifecycle import establish_session, launch_marimo, stop_marimo

    workspace = _workspace(workbook)
    workspace.notebook_path.write_text(
        "import marimo\n\napp = marimo.App()\n\n\n@app.cell\ndef _():\n    return\n",
        encoding="utf-8",
    )

    process = None
    try:
        process = launch_marimo(workspace, kedge_version="0.1.0")
        session = workspace.require_marimo()

        assert health_check(session.base_url) is True
        assert workspace.marker_path.is_file()
        assert workspace.token_file_path.is_file()

        session_id = establish_session(workspace)
        assert session_id == workspace.session_id
        assert workspace.require_marimo().session_id == session_id
        assert session_id in list_sessions(session.base_url, session.token)

        assert stop_marimo(workspace) is True
        assert health_check(session.base_url) is False
        assert not workspace.marker_path.exists()
        assert not workspace.token_file_path.exists()
    finally:
        # Must clean up even when an assertion above failed part way through, and the cleanup
        # itself must not mask the original failure.
        with contextlib.suppress(Exception):
            if workspace.has_marimo:
                stop_marimo(workspace)
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
