"""Tests for spawning, health-polling, session bootstrap, teardown, and orphan cleanup.

The HTTP surface itself is not tested here any more -- it lives in :mod:`kedge.marimo_http` and
is tested in ``test_marimo_http.py``, endpoint by endpoint, through ``httpx.MockTransport``.
What remains is this module's own job: choosing a port, building an argv, supervising a child
process on two operating systems that agree about almost nothing, and deciding whether a server
found on our recorded port is ours to stop. Where those paths reach marimo they still go through
a mock transport, so the requests asserted on are the ones marimo would receive.

The re-export tests near the top are load-bearing rather than decorative. ``cli.py``,
``server/`` and ``tests/contract/`` all import the HTTP names from here, and the consolidation
that moved their definitions was only safe because this module's public surface did not move
with them.
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from kedge import lifecycle, marimo_http
from kedge.config import Config, LoadedConfig, MarimoConfig
from kedge.errors import NotebookError
from kedge.lifecycle import (
    MARIMO_PIN,
    MarimoHealthTimeoutError,
    MarimoLaunchError,
    MarimoSessionNotFoundError,
    OrphanOutcome,
    build_marimo_argv,
    cleanup_orphan,
    establish_session,
    generate_token,
    health_check,
    installed_marimo_version,
    launch_marimo,
    list_sessions,
    pick_free_port,
    redacted_argv,
    register_teardown,
    stop_marimo,
    teardown,
)
from kedge.workspace import Workspace

TOKEN = "a-token-nobody-else-could-guess"
BASE = "http://127.0.0.1:2718"
_IS_WINDOWS = os.name == "nt"


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


def _impatient(**overrides: Any) -> Config:
    """A config whose waits are measured in fractions of a second rather than tens of them."""
    return Config(
        marimo=MarimoConfig(
            health_timeout_seconds=0.2,
            health_poll_interval_seconds=0.05,
            shutdown_grace_seconds=0.2,
            **overrides,
        )
    )


class _Server:
    """A stand-in marimo server, recording every request it receives."""

    def __init__(
        self,
        *,
        healthy: bool = True,
        token: str = TOKEN,
        sessions: dict[str, dict[str, Any]] | None = None,
        server_token: str | None = "skew-token",
        dies_on_shutdown: bool = True,
    ) -> None:
        self.healthy = healthy
        self.token = token
        self.sessions = sessions if sessions is not None else {}
        self.server_token = server_token
        self.dies_on_shutdown = dies_on_shutdown
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
                # setdefault, not assignment: a test that seeded a session with a notebook open
                # is describing a server that already has one, and the bootstrap does not
                # unbind it.
                self.sessions.setdefault(session_id, {"filename": None, "path": None})
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
            self.healthy = not self.dies_on_shutdown
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


class _FakeProcess:
    """A subprocess handle that never spawned anything.

    ``exits_on_signal`` decides which half of :func:`kedge.lifecycle._stop_process` a test
    exercises: a well-behaved child goes away on the polite signal, and a wedged one has to be
    escalated to.
    """

    def __init__(self, *, pid: int = 4242, exits_on_signal: bool = True) -> None:
        self.pid = pid
        self.exits_on_signal = exits_on_signal
        self.returncode: int | None = None
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired(cmd="marimo", timeout=timeout or 0.0)
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def signalled(self) -> None:
        """What a delivered CTRL_BREAK_EVENT or SIGTERM does to a cooperative child."""
        if self.exits_on_signal:
            self.returncode = 0


# ── the public surface other modules import ──────────────────────────────────────────────────

_HTTP_NAMES = (
    "bootstrap_session",
    "confirm_session",
    "fetch_server_token",
    "health_check",
    "list_sessions",
    "request_shutdown",
    "wait_for_health",
    "MarimoHealthTimeoutError",
    "MarimoLaunchError",
    "MarimoSessionNotFoundError",
)


def test_every_name_this_module_advertises_resolves() -> None:
    """``cli.py``, ``server/`` and the contract fixtures all import from here."""
    missing = [name for name in lifecycle.__all__ if not hasattr(lifecycle, name)]

    assert missing == []


@pytest.mark.parametrize("name", _HTTP_NAMES)
def test_an_http_name_imported_from_here_is_the_one_marimo_http_defines(name: str) -> None:
    """Re-exported, not reimplemented -- the whole point of review finding 2."""
    assert getattr(lifecycle, name) is getattr(marimo_http, name)


@pytest.mark.parametrize(
    ("name", "positional", "by_keyword"),
    [
        ("bootstrap_session", ["base_url", "token", "session_id"], ["hold_seconds", "client"]),
        ("cleanup_orphan", ["workspace"], ["client"]),
        ("confirm_session", ["base_url", "token", "session_id", "notebook_path"], ["client"]),
        ("establish_session", ["workspace"], ["client"]),
        ("fetch_server_token", ["base_url", "token", "notebook_path"], ["client"]),
        ("health_check", ["base_url"], ["timeout", "client"]),
        ("launch_marimo", ["workspace"], ["kedge_version", "client"]),
        ("list_sessions", ["base_url", "token"], ["timeout", "client"]),
        ("request_shutdown", ["base_url", "token"], ["session_id", "client"]),
        ("stop_marimo", ["workspace"], ["client"]),
        ("teardown", ["workspace"], ["client"]),
        ("wait_for_health", ["base_url"], ["timeout", "process", "client"]),
    ],
)
def test_the_signature_callers_depend_on_is_intact(
    name: str, positional: list[str], by_keyword: list[str]
) -> None:
    """A rename or a re-signature here breaks ``cli.py`` and ``server/`` at import time.

    The positional list is exact and ordered, because the two ways to break a positional API
    without losing a parameter are to swap two of them and to make one keyword-only, and a set
    comparison sees neither. The keyword-only list is a floor rather than an exact match: adding
    an optional keyword argument is a compatible change, dropping one a caller passes is not.
    """
    actual = inspect.signature(getattr(lifecycle, name)).parameters
    kinds = {name: parameter.kind for name, parameter in actual.items()}

    assert [
        parameter
        for parameter, kind in kinds.items()
        if kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    ] == positional, f"{name} reordered, renamed or hid a parameter callers pass positionally"
    assert set(by_keyword) <= {
        parameter for parameter, kind in kinds.items() if kind is inspect.Parameter.KEYWORD_ONLY
    }, f"{name} lost a keyword argument callers pass"


def test_a_health_timeout_can_still_be_caught_as_a_launch_failure() -> None:
    """The hierarchy is part of the surface: ``except MarimoLaunchError`` must keep catching."""
    assert issubclass(MarimoHealthTimeoutError, MarimoLaunchError)
    assert issubclass(MarimoLaunchError, NotebookError)
    assert issubclass(MarimoSessionNotFoundError, NotebookError)


def _live_strings(path: Path) -> list[str]:
    """Every string literal in a module that is not a docstring.

    By AST, for the same reason ``scripts/guardrails.py`` parses rather than greps: this module's
    prose explains at length which endpoints it used to own, and a grep cannot tell an
    explanation from an implementation. A bare string expression -- a module, class, function or
    attribute docstring -- has no runtime effect, so anything else is a string the code uses.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    inert = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in inert
    ]


def _httpx_calls(path: Path) -> list[str]:
    """Every ``httpx.<something>(...)`` call in a module. Annotations are not calls."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "httpx"
    ]


def test_lifecycle_speaks_no_http_of_its_own() -> None:
    """Review finding 2: two implementations of one undocumented API is a trap for the next bump.

    Endpoint paths, header names and the skew-token scrape belong to ``kedge.marimo_http``. This
    module supervises a process; where it needs a request it calls one of the imported names.
    Asserted against the source rather than through behaviour, because the failure being guarded
    against is somebody helpfully re-adding a one-line ``httpx.get`` here.
    """
    strings = _live_strings(Path(lifecycle.__file__))

    for endpoint in ("/health", "/api/sessions", "/sse", "/api/kernel/", "marimo-server-token"):
        offenders = [text for text in strings if endpoint in text]
        assert offenders == [], f"{endpoint} belongs in kedge.marimo_http, not in {offenders}"
    assert _httpx_calls(Path(lifecycle.__file__)) == [], (
        "lifecycle takes an httpx.Client so callers can share a pool; it must not use one itself"
    )


def test_marimo_http_is_where_those_endpoints_actually_live() -> None:
    """The other half of the invariant: the guard above must be passing for the right reason."""
    strings = _live_strings(Path(marimo_http.__file__))

    for endpoint in ("/health", "/api/sessions", "/sse", "/api/kernel/", "marimo-server-token"):
        assert any(endpoint in text for text in strings), f"{endpoint} went missing"


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


def test_a_missing_marimo_is_reported_as_absent_not_as_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _absent(_name: str) -> str:
        raise lifecycle.metadata.PackageNotFoundError("marimo")

    monkeypatch.setattr(lifecycle.metadata, "version", _absent)

    assert installed_marimo_version() is None


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


def test_redacting_an_argv_with_no_inline_token_changes_nothing(tmp_path: Path) -> None:
    argv = build_marimo_argv(tmp_path / "n.py", port=1, token_file=tmp_path / "t")

    assert redacted_argv(argv) == argv


# ── launch ───────────────────────────────────────────────────────────────────────────────────


def _fake_popen(monkeypatch: pytest.MonkeyPatch, process: _FakeProcess) -> list[list[str]]:
    """Replace ``subprocess.Popen`` with something that spawns nothing, recording the argv."""
    seen: list[list[str]] = []

    def _spawn(argv: list[str], **_kwargs: object) -> _FakeProcess:
        seen.append(argv)
        return process

    monkeypatch.setattr(lifecycle.subprocess, "Popen", _spawn)
    return seen


def test_launching_records_the_session_and_leaves_a_marker(
    kedge_home: Path, workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(workbook, _impatient())
    argvs = _fake_popen(monkeypatch, _FakeProcess())
    server = _Server()

    process = launch_marimo(workspace, kedge_version="0.1.0", client=server.client())

    assert process.pid == 4242
    session = workspace.require_marimo()
    assert session.pid == 4242
    assert session.session_id is None, "the kernel session does not exist until it is bootstrapped"
    assert workspace.marker_path.is_file()
    assert workspace.token_file_path.is_file()
    assert "--token-password-file" in argvs[0], "the token must stay out of the process table"
    assert (workspace.logs_dir / f"marimo-{session.port}.log").is_file()


def test_a_launch_that_cannot_spawn_says_so_without_leaking_the_token(
    kedge_home: Path, workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(workbook, _impatient())

    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("no such interpreter")

    monkeypatch.setattr(lifecycle.subprocess, "Popen", _refuse)

    with pytest.raises(MarimoLaunchError) as excinfo:
        launch_marimo(workspace, kedge_version="0.1.0")

    assert "no such interpreter" in str(excinfo.value)
    assert not workspace.token_file_path.exists()
    assert not workspace.has_marimo


def test_a_server_that_never_serves_is_torn_down_and_leaves_nothing_behind(
    kedge_home: Path, workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-launched server with a marker file would be cleaned up as an orphan next run."""
    workspace = _workspace(workbook, _impatient())
    process = _FakeProcess()
    _fake_popen(monkeypatch, process)
    stopped: list[int] = []
    monkeypatch.setattr(
        lifecycle, "_stop_process", lambda pid, **_kwargs: bool(stopped.append(pid)) or True
    )

    with pytest.raises(MarimoHealthTimeoutError):
        launch_marimo(workspace, kedge_version="0.1.0", client=_refusing_client())

    assert stopped == [4242]
    assert not workspace.has_marimo
    assert not workspace.marker_path.exists()
    assert not workspace.token_file_path.exists()


def test_a_configured_port_is_used_instead_of_a_free_one(
    kedge_home: Path, workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fixed port is for debugging, and picking a different one would defeat the point."""
    workspace = _workspace(workbook, _impatient(port=54_321))
    _fake_popen(monkeypatch, _FakeProcess())

    launch_marimo(workspace, kedge_version="0.1.0", client=_Server().client())

    assert workspace.require_marimo().port == 54_321


def test_establish_session_bootstraps_confirms_and_records_the_id(
    kedge_home: Path, workbook: Path
) -> None:
    workspace = _workspace(workbook)
    workspace.attach_marimo(host="127.0.0.1", port=2718, token=TOKEN, pid=1)
    server = _Server(
        sessions={workspace.session_id: {"path": str(workspace.notebook_path), "filename": None}}
    )

    session_id = establish_session(workspace, client=server.client())

    assert session_id == workspace.session_id
    assert workspace.require_marimo().session_id == session_id
    assert "/sse" in server.paths()
    assert "/api/sessions" in server.paths()


def test_establishing_a_session_on_somebody_elses_notebook_is_refused(
    kedge_home: Path, workbook: Path, tmp_path: Path
) -> None:
    workspace = _workspace(workbook)
    workspace.attach_marimo(host="127.0.0.1", port=2718, token=TOKEN, pid=1)
    theirs = tmp_path / "not-ours.py"
    theirs.write_text("", encoding="utf-8")
    server = _Server(sessions={workspace.session_id: {"path": str(theirs), "filename": None}})

    with pytest.raises(MarimoSessionNotFoundError, match="did not open"):
        establish_session(workspace, client=server.client())

    assert workspace.require_marimo().session_id is None


# ── stopping a process ───────────────────────────────────────────────────────────────────────


def test_a_process_that_has_already_gone_is_not_signalled(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _FakeProcess()
    process.returncode = 0
    signalled: list[int] = []
    monkeypatch.setattr(lifecycle.os, "kill", lambda pid, _sig: signalled.append(pid))

    assert lifecycle._stop_process(process.pid, process=process, grace=0.1) is True
    assert signalled == []


def test_the_platform_flag_the_tests_below_pretend_with_matches_reality() -> None:
    """``_pretend`` lets one machine exercise both escalations. This is what keeps it honest."""
    assert (os.name == "nt") == lifecycle._IS_WINDOWS


def _pretend(monkeypatch: pytest.MonkeyPatch, *, windows: bool) -> None:
    """Run the other platform's branch of ``_stop_process`` on this one.

    Both escalations are load-bearing and neither is exercised by the machine that cannot run
    it, so each would be dead code on half the CI matrix. ``signal`` is swapped too: on Windows
    there is no ``SIGKILL`` to name, and on POSIX no ``CTRL_BREAK_EVENT``.
    """
    monkeypatch.setattr(lifecycle, "_IS_WINDOWS", windows)
    monkeypatch.setattr(
        lifecycle,
        "signal",
        SimpleNamespace(CTRL_BREAK_EVENT=21, SIGTERM=15, SIGKILL=9),
    )


@pytest.mark.parametrize("windows", [True, False], ids=["windows", "posix"])
def test_a_cooperative_process_exits_on_the_polite_signal(
    monkeypatch: pytest.MonkeyPatch, windows: bool
) -> None:
    process = _FakeProcess(exits_on_signal=True)
    sent: list[int] = []
    _pretend(monkeypatch, windows=windows)

    def _kill(_pid: int, sig: int) -> None:
        sent.append(sig)
        process.signalled()

    monkeypatch.setattr(lifecycle.os, "kill", _kill)

    assert lifecycle._stop_process(process.pid, process=process, grace=0.1) is True
    assert process.killed is False, "escalation is for processes that ignore the signal"
    assert sent == [21 if windows else 15], (
        "CTRL_BREAK_EVENT is the nearest Windows equivalent of a polite SIGTERM"
    )


def test_a_signal_that_cannot_be_delivered_is_logged_and_the_escalation_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recycled or already-reaped pid raises here; it must not abort the teardown."""
    process = _FakeProcess(exits_on_signal=False)
    _pretend(monkeypatch, windows=False)

    def _kill(_pid: int, _sig: int) -> None:
        raise OSError("no such process")

    monkeypatch.setattr(lifecycle.os, "kill", _kill)

    lifecycle._stop_process(process.pid, process=process, grace=0.05)

    assert process.killed is True


def test_a_wedged_process_tree_is_killed_with_taskkill(monkeypatch: pytest.MonkeyPatch) -> None:
    """``/T`` is the part that matters: Popen.terminate() would orphan marimo's kernel child."""
    process = _FakeProcess(exits_on_signal=False)
    commands: list[list[str]] = []
    _pretend(monkeypatch, windows=True)

    monkeypatch.setattr(lifecycle.os, "kill", lambda *_a: None)
    monkeypatch.setattr(
        lifecycle.subprocess,
        "run",
        lambda argv, **_kwargs: commands.append(argv) or process.kill(),
    )

    lifecycle._stop_process(process.pid, process=process, grace=0.05)

    assert commands == [["taskkill", "/F", "/T", "/PID", "4242"]]


def test_a_taskkill_that_will_not_run_is_reported_rather_than_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _FakeProcess(exits_on_signal=False)
    _pretend(monkeypatch, windows=True)

    def _explode(*_args: object, **_kwargs: object) -> None:
        raise OSError("taskkill is not on PATH")

    monkeypatch.setattr(lifecycle.os, "kill", lambda *_a: None)
    monkeypatch.setattr(lifecycle.subprocess, "run", _explode)

    assert lifecycle._stop_process(process.pid, process=process, grace=0.05) is False


def test_a_wedged_process_is_killed_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _FakeProcess(exits_on_signal=False)
    _pretend(monkeypatch, windows=False)
    monkeypatch.setattr(lifecycle.os, "kill", lambda *_a: None)

    lifecycle._stop_process(process.pid, process=process, grace=0.05)

    assert process.killed is True


def test_an_orphan_with_no_handle_is_killed_by_pid_on_posix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server from a previous run leaves a pid in a marker file and no Popen object."""
    sent: list[int] = []
    _pretend(monkeypatch, windows=False)

    def _kill(_pid: int, sig: int) -> None:
        sent.append(sig)

    monkeypatch.setattr(lifecycle.os, "kill", _kill)
    monkeypatch.setattr(lifecycle, "_pid_exists", lambda _pid: 9 not in sent)

    assert lifecycle._stop_process(4242, grace=0.05) is True
    assert sent == [15, 9], "SIGTERM, and only then SIGKILL"


# ── waiting for a process to be gone ─────────────────────────────────────────────────────────


def test_a_handle_that_will_not_exit_reports_failure_rather_than_hanging() -> None:
    process = _FakeProcess(exits_on_signal=False)

    assert lifecycle._wait_for_exit(4242, process=process, timeout=0.05) is False


def test_without_a_handle_the_port_going_quiet_is_the_evidence() -> None:
    """ "Is the server still there" is the real question, and on Windows a pid answers it badly."""
    assert (
        lifecycle._wait_for_exit(
            4242, process=None, timeout=0.5, base_url=BASE, client=_refusing_client()
        )
        is True
    )


def test_a_port_that_keeps_answering_means_the_server_is_still_there() -> None:
    server = _Server()

    assert (
        lifecycle._wait_for_exit(
            4242, process=None, timeout=0.15, base_url=BASE, client=server.client()
        )
        is False
    )


def test_with_neither_a_handle_nor_a_url_the_pid_is_the_last_resort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lifecycle, "_pid_exists", lambda _pid: False)

    assert lifecycle._wait_for_exit(4242, process=None, timeout=0.05) is True


def test_a_pid_that_stays_put_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle, "_pid_exists", lambda _pid: True)

    assert lifecycle._wait_for_exit(4242, process=None, timeout=0.05) is False


def test_this_very_process_is_alive_and_a_nonsense_pid_is_not() -> None:
    """``os.kill(pid, 0)`` is the POSIX idiom and would *kill* the process on Windows."""
    assert lifecycle._pid_exists(os.getpid()) is True
    assert lifecycle._pid_exists(0) is False
    assert lifecycle._pid_exists(-1) is False


def test_a_pid_that_cannot_be_running_is_reported_as_gone() -> None:
    # Above every platform's pid_max, so it cannot be a live process on any of them.
    assert lifecycle._pid_exists(4_294_967_294) is False


@pytest.mark.parametrize(
    ("raises", "expected"),
    [
        (None, True),
        (ProcessLookupError("no such process"), False),
        (PermissionError("not yours"), True),
        (OSError("something else entirely"), True),
    ],
    ids=["running", "gone", "someone-elses", "unknown"],
)
def test_the_posix_liveness_probe_reads_every_answer_conservatively(
    monkeypatch: pytest.MonkeyPatch, raises: Exception | None, expected: bool
) -> None:
    """Anything but "no such process" means "assume it is still there", which is the safe way
    round: a false "gone" frees a port kedge then fails to bind."""
    monkeypatch.setattr(lifecycle, "_IS_WINDOWS", False)

    def _probe(_pid: int, _sig: int) -> None:
        if raises is not None:
            raise raises

    monkeypatch.setattr(lifecycle.os, "kill", _probe)

    assert lifecycle._pid_exists(4242) is expected


# ── stopping the server ──────────────────────────────────────────────────────────────────────


def test_stopping_with_nothing_attached_is_a_no_op(kedge_home: Path, workbook: Path) -> None:
    assert stop_marimo(_workspace(workbook)) is False


def test_stopping_asks_the_server_first_and_clears_the_workspace(
    kedge_home: Path, workbook: Path
) -> None:
    workspace = _workspace(workbook, _impatient())
    workspace.attach_marimo(host="127.0.0.1", port=2718, token=TOKEN, pid=4242)
    workspace.write_marker(kedge_version="0.1.0")
    server = _Server()

    assert stop_marimo(workspace, client=server.client()) is True

    assert "/api/kernel/shutdown" in server.paths()
    assert workspace.marimo is None
    assert not workspace.marker_path.exists()
    assert not workspace.token_file_path.exists()


def test_a_server_that_ignores_the_shutdown_request_is_terminated(
    kedge_home: Path, workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The HTTP shutdown is the graceful path, not the only one."""
    workspace = _workspace(workbook, _impatient())
    workspace.attach_marimo(host="127.0.0.1", port=2718, token=TOKEN, pid=4242)
    server = _Server(dies_on_shutdown=False)
    stopped: list[int] = []
    monkeypatch.setattr(
        lifecycle, "_stop_process", lambda pid, **_kwargs: bool(stopped.append(pid)) or True
    )

    assert stop_marimo(workspace, client=server.client()) is True
    assert stopped == [4242]


def test_teardown_never_raises(
    kedge_home: Path, workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It runs from ``atexit``, where an exception is printed at the user and helps nobody."""
    workspace = _workspace(workbook)

    def _explode(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("the network went away")

    monkeypatch.setattr(lifecycle, "stop_marimo", _explode)

    teardown(workspace)


def test_register_teardown_hooks_atexit_and_the_signals_that_exist(
    kedge_home: Path, workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(workbook)
    registered: list[object] = []
    handlers: dict[int, Any] = {}
    monkeypatch.setattr(lifecycle.atexit, "register", lambda fn, *_a: registered.append(fn))
    monkeypatch.setattr(lifecycle.signal, "signal", lambda sig, fn: handlers.setdefault(sig, fn))

    register_teardown(workspace)

    assert registered == [teardown]
    assert signal.SIGINT in handlers
    assert signal.SIGTERM in handlers


def test_the_signal_handler_tears_down_and_exits_with_the_conventional_code(
    kedge_home: Path, workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(workbook)
    handlers: dict[int, Any] = {}
    torn_down: list[Workspace] = []
    monkeypatch.setattr(lifecycle.atexit, "register", lambda *_a: None)
    monkeypatch.setattr(lifecycle.signal, "signal", lambda sig, fn: handlers.setdefault(sig, fn))
    monkeypatch.setattr(lifecycle, "teardown", lambda ws, **_k: torn_down.append(ws))

    register_teardown(workspace)

    with pytest.raises(SystemExit) as excinfo:
        handlers[signal.SIGINT](int(signal.SIGINT), None)

    assert excinfo.value.code == 128 + int(signal.SIGINT)
    assert torn_down == [workspace]


def test_a_signal_the_platform_does_not_have_is_skipped(
    kedge_home: Path, workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGBREAK is Windows-only and SIGHUP is not; neither absence is an error."""
    workspace = _workspace(workbook)
    handlers: dict[int, Any] = {}
    monkeypatch.setattr(lifecycle.atexit, "register", lambda *_a: None)
    monkeypatch.setattr(
        lifecycle,
        "signal",
        SimpleNamespace(
            SIGINT=signal.SIGINT,
            SIGTERM=signal.SIGTERM,
            signal=lambda sig, fn: handlers.setdefault(sig, fn),
        ),
    )

    register_teardown(workspace)

    assert set(handlers) == {signal.SIGINT, signal.SIGTERM}


def test_a_signal_the_platform_will_not_accept_is_skipped(
    kedge_home: Path, workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Registering a handler off the main thread raises; that is not a reason to fail a launch."""
    workspace = _workspace(workbook)
    monkeypatch.setattr(lifecycle.atexit, "register", lambda *_a: None)

    def _refuse(_sig: int, _fn: object) -> None:
        raise ValueError("signal only works in main thread")

    monkeypatch.setattr(lifecycle.signal, "signal", _refuse)

    register_teardown(workspace)


# ── orphan cleanup ───────────────────────────────────────────────────────────────────────────


def test_no_marker_means_nothing_to_clean_up(kedge_home: Path, workbook: Path) -> None:
    outcome = cleanup_orphan(_workspace(workbook), client=_refusing_client())

    assert outcome.action == "no_marker"
    assert outcome.freed_port is False


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


def test_an_orphan_that_will_not_confirm_its_exit_says_so(
    kedge_home: Path, workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Port reuse may still fail, and the caller has to be told before it tries to bind."""
    workspace = _workspace(workbook, _impatient())
    _write_marker(workspace)
    monkeypatch.setattr(lifecycle, "_stop_process", lambda *_a, **_k: False)

    outcome = cleanup_orphan(workspace, client=_Server(dies_on_shutdown=False).client())

    assert outcome.action == "stopped_orphan"
    assert "port reuse may still fail" in outcome.detail


def test_an_orphan_is_stopped_even_when_a_session_is_already_attached(
    kedge_home: Path, workbook: Path
) -> None:
    """``kedge doctor`` may have attached the current run before the orphan check ran."""
    workspace = _workspace(workbook)
    _write_marker(workspace)
    workspace.attach_marimo(host="127.0.0.1", port=9999, token="other", pid=1)
    server = _Server()

    outcome = cleanup_orphan(workspace, client=server.client())

    assert outcome.action == "stopped_orphan"
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


def test_only_a_stopped_orphan_counts_as_a_freed_port() -> None:
    for action in ("no_marker", "cleared_stale_marker", "left_alone"):
        assert OrphanOutcome(action=action, detail="").freed_port is False  # ty: ignore[invalid-argument-type]
    assert OrphanOutcome(action="stopped_orphan", detail="").freed_port is True


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
