"""Tests for the one module that speaks marimo's HTTP API.

Everything here goes through ``httpx.MockTransport`` rather than monkeypatching kedge's own
functions, so each test asserts on the request marimo would actually receive: the path, the
headers, the query string. That is the point of the module -- it is the written-down form of an
undocumented API -- and a test that stubbed out the request would assert nothing about it.

The stand-in server below is deliberately strict about auth. Several of the properties these
tests exist to protect are of the form "kedge must not touch a server that is not ours", and a
mock that answered every request regardless of the bearer token could not tell them apart.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
import pytest

from kedge import marimo_http
from kedge.errors import NotebookError
from kedge.marimo_http import (
    EXECUTE_PATH,
    MarimoHealthTimeoutError,
    MarimoLaunchError,
    MarimoSessionNotFoundError,
    auth_headers,
    bootstrap_session,
    confirm_session,
    fetch_server_token,
    health_check,
    list_sessions,
    request_shutdown,
    wait_for_health,
)

TOKEN = "a-token-nobody-else-could-guess"
BASE = "http://127.0.0.1:2718"


class _Server:
    """A stand-in marimo server, recording every request it receives."""

    def __init__(
        self,
        *,
        healthy: bool = True,
        token: str = TOKEN,
        sessions: dict[str, dict[str, Any]] | None = None,
        server_token: str | None = "skew-token",
        sessions_payload: Any = None,
        sessions_status: int = 200,
    ) -> None:
        self.healthy = healthy
        self.token = token
        self.sessions = sessions if sessions is not None else {}
        self.server_token = server_token
        self.sessions_payload = sessions_payload
        self.sessions_status = sessions_status
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
                # setdefault, not assignment: a bootstrap against a session that already has a
                # notebook open does not unbind it.
                self.sessions.setdefault(session_id, {"filename": None, "path": None})
            return httpx.Response(200, content=b"event: kernel-ready\ndata: {}\n\n")

        if path == "/api/sessions":
            if self.sessions_status != 200:
                return httpx.Response(self.sessions_status, json={"detail": "no"})
            if self.sessions_payload is not None:
                return httpx.Response(200, json=self.sessions_payload)
            return httpx.Response(200, json=self.sessions)

        if path == "/":
            element = (
                ""
                if self.server_token is None
                else f'<marimo-server-token data-token="{self.server_token}"></marimo-server-token>'
            )
            return httpx.Response(200, html=f"<html>{element}</html>")

        if path == "/api/kernel/shutdown":
            # A real server always holds a skew token; ``server_token=None`` models a page we
            # could not scrape it from, and marimo refuses the POST in that case.
            sent = request.headers.get("Marimo-Server-Token")
            if self.server_token is None or sent != self.server_token:
                return httpx.Response(403, json={"detail": "skew protection"})
            self.healthy = False
            return httpx.Response(200, json={"success": True})

        return httpx.Response(404)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]

    def request_to(self, path: str) -> httpx.Request:
        return next(request for request in self.requests if request.url.path == path)


def _refusing_client() -> httpx.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return httpx.Client(transport=httpx.MockTransport(handle))


def _status_client(status: int, *, text: str = "") -> httpx.Client:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=text)

    return httpx.Client(transport=httpx.MockTransport(handle))


class _Dead:
    """A subprocess handle that has already exited, satisfying ``ProcessLike``."""

    returncode = 3

    def poll(self) -> int:
        return 3


class _Alive:
    """A subprocess handle that is still running."""

    returncode: int | None = None

    def poll(self) -> int | None:
        return None


# ── the module contract ──────────────────────────────────────────────────────────────────────


def test_every_marimo_error_is_a_notebook_error() -> None:
    """One name catches "anything kedge did wrong with the notebook process" (CONVENTIONS)."""
    for error in (MarimoLaunchError, MarimoHealthTimeoutError, MarimoSessionNotFoundError):
        assert issubclass(error, NotebookError)


def test_a_health_timeout_is_a_launch_failure() -> None:
    """A server that never answers is a failed launch; callers catch both with one name."""
    assert issubclass(MarimoHealthTimeoutError, MarimoLaunchError)


def test_the_execute_path_is_defined_here_and_nowhere_else() -> None:
    """Finding 2: every marimo URL is written down in this module, execute included.

    ``notebook/kernel.py`` builds the request -- an async SSE stream is a different animal from
    the small synchronous calls here -- but it imports the path, so a bump that moves the
    endpoint is still a one-file correction.
    """
    from kedge.notebook import kernel

    assert EXECUTE_PATH == "/api/kernel/execute"
    assert kernel.EXECUTE_PATH is EXECUTE_PATH


def test_auth_headers_carry_the_bearer_token() -> None:
    assert auth_headers(TOKEN) == {"Authorization": f"Bearer {TOKEN}"}


def test_auth_headers_name_the_session_when_there_is_one() -> None:
    headers = auth_headers(TOKEN, "kedge-abc")

    assert headers["Marimo-Session-Id"] == "kedge-abc"
    assert headers["Authorization"] == f"Bearer {TOKEN}"


def test_auth_headers_omit_an_empty_session_id() -> None:
    """A blank id would address a session that does not exist rather than the server itself."""
    assert "Marimo-Session-Id" not in auth_headers(TOKEN, "")


def test_auth_headers_returns_a_fresh_mapping_each_time() -> None:
    """Callers add Content-Type and Accept to it, so a shared dict would leak between requests."""
    first = auth_headers(TOKEN)
    first["Content-Type"] = "application/json"

    assert "Content-Type" not in auth_headers(TOKEN)


def test_a_call_with_no_client_opens_and_closes_one_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``client`` is an optional connection-pool share, not a requirement."""
    server = _Server()
    created: list[httpx.Client] = []

    class _Owned(httpx.Client):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(transport=httpx.MockTransport(server.handle), **kwargs)
            created.append(self)

    monkeypatch.setattr(httpx, "Client", _Owned)

    assert health_check(BASE) is True
    assert len(created) == 1
    assert created[0].is_closed, "an owned client must not be left open"


# ── liveness ─────────────────────────────────────────────────────────────────────────────────


def test_health_check_is_true_when_the_server_answers() -> None:
    server = _Server()

    assert health_check(BASE, client=server.client()) is True
    assert server.paths() == ["/health"]


def test_health_check_needs_no_token() -> None:
    """/health is unauthenticated, so it works against any marimo, not just ours."""
    server = _Server(token="a-completely-different-token")

    assert health_check(BASE, client=server.client()) is True
    assert "Authorization" not in server.request_to("/health").headers


def test_health_check_is_false_when_nothing_is_listening() -> None:
    assert health_check(BASE, client=_refusing_client()) is False


def test_health_check_is_false_when_something_else_holds_the_port() -> None:
    """Another program answering on the port is not a marimo, whatever it returns."""
    assert health_check(BASE, client=_status_client(503)) is False


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
    """ "marimo died" and "marimo is slow" want different responses, so they are different types."""
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
    assert not isinstance(excinfo.value, MarimoHealthTimeoutError), (
        "a death must be distinguishable from a timeout by type, not only by message"
    )


def test_a_process_that_is_still_running_does_not_abort_the_wait() -> None:
    server = _Server()

    elapsed = wait_for_health(
        BASE,
        timeout=2.0,
        interval=0.01,
        process=_Alive(),  # ty: ignore[invalid-argument-type]
        client=server.client(),
    )

    assert elapsed >= 0.0


def test_the_log_tail_is_omitted_when_there_is_no_log(tmp_path: Path) -> None:
    with pytest.raises(MarimoLaunchError) as excinfo:
        wait_for_health(
            BASE,
            timeout=30.0,
            interval=0.01,
            process=_Dead(),  # ty: ignore[invalid-argument-type]
            log_path=tmp_path / "never-written.log",
            client=_refusing_client(),
        )

    assert "Last lines of" not in str(excinfo.value)


def test_an_empty_log_contributes_no_tail(tmp_path: Path) -> None:
    log = tmp_path / "marimo.log"
    log.write_text("", encoding="utf-8")

    with pytest.raises(MarimoHealthTimeoutError) as excinfo:
        wait_for_health(BASE, timeout=0.1, interval=0.05, log_path=log, client=_refusing_client())

    assert "Last lines of" not in str(excinfo.value)


def test_a_log_that_cannot_be_read_does_not_mask_the_real_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tail is a courtesy. A locked or vanished log must not become the error the user sees."""
    log = tmp_path / "marimo.log"
    log.write_text("something", encoding="utf-8")

    def _refuse(*_args: object, **_kwargs: object) -> str:
        raise OSError("the file is locked by another process")

    monkeypatch.setattr(Path, "read_text", _refuse)

    with pytest.raises(MarimoHealthTimeoutError) as excinfo:
        wait_for_health(BASE, timeout=0.1, interval=0.05, log_path=log, client=_refusing_client())

    assert "did not answer" in str(excinfo.value)
    assert "Last lines of" not in str(excinfo.value)


def test_the_log_tail_is_the_last_fifteen_lines(tmp_path: Path) -> None:
    """A whole startup log in an exception message buries the part that matters."""
    log = tmp_path / "marimo.log"
    log.write_text("\n".join(f"line {n}" for n in range(40)), encoding="utf-8")

    with pytest.raises(MarimoHealthTimeoutError) as excinfo:
        wait_for_health(BASE, timeout=0.1, interval=0.05, log_path=log, client=_refusing_client())

    message = str(excinfo.value)
    assert "line 39" in message
    assert "line 24" not in message


# ── sessions ─────────────────────────────────────────────────────────────────────────────────


def test_list_sessions_is_empty_on_a_freshly_launched_server() -> None:
    """A marimo server creates no session until a transport connects (docs/marimo-api.md 5.3)."""
    server = _Server()

    assert list_sessions(BASE, TOKEN, client=server.client()) == {}


def test_list_sessions_returns_what_the_server_reports() -> None:
    server = _Server(sessions={"kedge-abc": {"filename": "process.py", "path": "/tmp/process.py"}})

    listed = list_sessions(BASE, TOKEN, client=server.client())

    assert listed["kedge-abc"]["filename"] == "process.py"
    assert server.request_to("/api/sessions").headers["Authorization"] == f"Bearer {TOKEN}"


def test_a_server_that_rejects_our_token_is_not_ours() -> None:
    server = _Server(token="somebody-elses-token")

    with pytest.raises(MarimoSessionNotFoundError) as excinfo:
        list_sessions(BASE, TOKEN, client=server.client())

    assert "must not be touched" in str(excinfo.value)


def test_a_forbidden_session_listing_is_also_not_ours() -> None:
    with pytest.raises(MarimoSessionNotFoundError, match="must not be touched"):
        list_sessions(BASE, TOKEN, client=_status_client(403))


def test_an_unexpected_status_from_the_session_listing_names_the_code() -> None:
    server = _Server(sessions_status=503)

    with pytest.raises(MarimoSessionNotFoundError, match="HTTP 503"):
        list_sessions(BASE, TOKEN, client=server.client())


def test_a_session_listing_that_is_not_an_object_is_rejected() -> None:
    """Something else on the port could answer 200 with anything at all."""
    server = _Server(sessions_payload=["not", "an", "object"])

    with pytest.raises(MarimoSessionNotFoundError, match="expected an object"):
        list_sessions(BASE, TOKEN, client=server.client())


def test_an_unreachable_server_cannot_be_listed() -> None:
    with pytest.raises(MarimoSessionNotFoundError, match="could not list"):
        list_sessions(BASE, TOKEN, client=_refusing_client())


def test_bootstrap_session_creates_the_session_we_asked_for() -> None:
    server = _Server()

    bootstrap_session(BASE, TOKEN, "kedge-abc", hold_seconds=0.05, client=server.client())

    assert "/sse" in server.paths()
    sse = server.request_to("/sse")
    assert sse.url.params["session_id"] == "kedge-abc"
    assert sse.headers["Authorization"] == f"Bearer {TOKEN}"
    assert sse.headers["Marimo-Session-Id"] == "kedge-abc"
    assert "kedge-abc" in server.sessions


def test_bootstrap_session_reports_a_refused_bootstrap() -> None:
    server = _Server(token="somebody-elses-token")

    with pytest.raises(MarimoSessionNotFoundError, match="401"):
        bootstrap_session(BASE, TOKEN, "kedge-abc", hold_seconds=0.05, client=server.client())


def test_a_refused_bootstrap_names_the_version_that_first_served_sse() -> None:
    """GET /sse is 0.23.15-only, and a 404 here usually means an older marimo (docs 7.3)."""
    with pytest.raises(MarimoSessionNotFoundError) as excinfo:
        bootstrap_session(BASE, TOKEN, "kedge-abc", hold_seconds=0.05, client=_status_client(404))

    assert "0.23.15" in str(excinfo.value)


def test_bootstrap_session_reports_an_unreachable_server() -> None:
    with pytest.raises(MarimoSessionNotFoundError, match="could not bootstrap"):
        bootstrap_session(BASE, TOKEN, "kedge-abc", hold_seconds=0.05, client=_refusing_client())


def test_a_stream_that_stays_open_for_the_whole_hold_is_a_success() -> None:
    """A healthy SSE transport is quiet; the read timeout firing is what "held open" looks like."""

    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("no data", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handle))

    bootstrap_session(BASE, TOKEN, "kedge-abc", hold_seconds=0.05, client=client)


def test_bootstrap_returns_when_a_short_stream_ends_of_its_own_accord() -> None:
    """marimo may close the stream before the hold window is up; that is a success, not a stall."""

    def two_events() -> Any:
        yield b"event: kernel-ready\ndata: {}\n\n"
        yield b"event: ping\ndata: {}\n\n"

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=two_events())

    client = httpx.Client(transport=httpx.MockTransport(handle))

    bootstrap_session(BASE, TOKEN, "kedge-abc", hold_seconds=5.0, client=client)


def test_bootstrap_drains_a_stream_that_keeps_talking() -> None:
    """The hold window bounds the drain, so a chatty server cannot wedge the bootstrap."""

    def chatter() -> Any:
        while True:
            yield b"event: ping\ndata: {}\n\n"

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=chatter())

    client = httpx.Client(transport=httpx.MockTransport(handle))

    bootstrap_session(BASE, TOKEN, "kedge-abc", hold_seconds=0.05, client=client)


def test_confirm_session_accepts_a_session_open_on_our_notebook(tmp_path: Path) -> None:
    notebook = tmp_path / "process.py"
    notebook.write_text("", encoding="utf-8")
    server = _Server(sessions={"kedge-abc": {"path": str(notebook), "filename": "process.py"}})

    info = confirm_session(BASE, TOKEN, "kedge-abc", notebook, timeout=0.5, client=server.client())

    assert info["path"] == str(notebook)


def test_confirm_session_falls_back_to_the_filename_when_there_is_no_path(tmp_path: Path) -> None:
    notebook = tmp_path / "process.py"
    notebook.write_text("", encoding="utf-8")
    server = _Server(sessions={"kedge-abc": {"path": None, "filename": str(notebook)}})

    info = confirm_session(BASE, TOKEN, "kedge-abc", notebook, timeout=0.5, client=server.client())

    assert info["filename"] == str(notebook)


def test_confirm_session_accepts_a_session_not_yet_bound_to_a_file_but_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    notebook = tmp_path / "process.py"
    notebook.write_text("", encoding="utf-8")
    server = _Server(sessions={"kedge-abc": {"path": None, "filename": None}})

    with caplog.at_level(logging.WARNING, logger="kedge.marimo_http"):
        info = confirm_session(
            BASE, TOKEN, "kedge-abc", notebook, timeout=0.5, client=server.client()
        )

    assert info == {"path": None, "filename": None}
    assert "reports no filename yet" in caplog.text


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

    with pytest.raises(MarimoSessionNotFoundError, match="sessions present: none"):
        confirm_session(
            BASE, TOKEN, "kedge-abc", notebook, timeout=0.2, interval=0.05, client=server.client()
        )


def test_a_path_reported_in_a_different_case_still_matches_on_windows(tmp_path: Path) -> None:
    """Windows reports whatever case the caller used; a case-sensitive compare would reject it."""
    notebook = tmp_path / "Process.py"
    notebook.write_text("", encoding="utf-8")
    reported = str(notebook).swapcase()
    server = _Server(sessions={"kedge-abc": {"path": reported, "filename": None}})

    if marimo_http._IS_WINDOWS_PATHS:
        assert confirm_session(
            BASE, TOKEN, "kedge-abc", notebook, timeout=0.2, client=server.client()
        )
    else:
        with pytest.raises(MarimoSessionNotFoundError, match="did not open"):
            confirm_session(BASE, TOKEN, "kedge-abc", notebook, timeout=0.2, client=server.client())


def test_a_path_that_will_not_resolve_is_compared_as_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolution is a normalisation, not a precondition: a path kedge cannot resolve is still
    a path, and refusing to confirm the session over it would be worse than comparing it raw."""
    notebook = tmp_path / "process.py"
    notebook.write_text("", encoding="utf-8")

    def _refuse(_self: Path) -> Path:
        raise OSError("the filesystem is unavailable")

    monkeypatch.setattr(Path, "resolve", _refuse)
    server = _Server(sessions={"kedge-abc": {"path": str(notebook), "filename": None}})

    info = confirm_session(BASE, TOKEN, "kedge-abc", notebook, timeout=0.5, client=server.client())

    assert info["path"] == str(notebook)


def test_a_relative_path_is_resolved_before_it_is_compared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """marimo reports the path it was given; kedge holds an absolute one."""
    notebook = tmp_path / "process.py"
    notebook.write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    server = _Server(sessions={"kedge-abc": {"path": "process.py", "filename": None}})

    info = confirm_session(BASE, TOKEN, "kedge-abc", notebook, timeout=0.5, client=server.client())

    assert info["path"] == "process.py"


# ── shutdown ─────────────────────────────────────────────────────────────────────────────────


def test_fetch_server_token_scrapes_the_skew_protection_token() -> None:
    server = _Server(server_token="abc123")

    assert fetch_server_token(BASE, TOKEN, client=server.client()) == "abc123"


def test_fetch_server_token_asks_for_the_notebook_page_when_it_knows_which(tmp_path: Path) -> None:
    notebook = tmp_path / "process.py"
    server = _Server()

    fetch_server_token(BASE, TOKEN, notebook, client=server.client())

    page = server.request_to("/")
    assert page.url.params["file"] == str(notebook)
    assert page.url.params["access_token"] == TOKEN


def test_fetch_server_token_returns_none_when_the_page_has_no_token() -> None:
    server = _Server(server_token=None)

    assert fetch_server_token(BASE, TOKEN, client=server.client()) is None


def test_fetch_server_token_returns_none_when_the_page_is_refused() -> None:
    """Failure is not an error here: the caller falls back to terminating the process."""
    assert fetch_server_token(BASE, TOKEN, client=_status_client(401)) is None


def test_fetch_server_token_returns_none_when_the_server_is_unreachable() -> None:
    assert fetch_server_token(BASE, TOKEN, client=_refusing_client()) is None


def test_the_server_token_is_read_from_single_quoted_markup_too() -> None:
    """The element is generated HTML, so the quoting style is not ours to depend on."""
    client = _status_client(200, text="<marimo-server-token DATA-TOKEN='xyz'>")

    assert fetch_server_token(BASE, TOKEN, client=client) == "xyz"


def test_request_shutdown_sends_the_skew_protection_header() -> None:
    """POST /api/kernel/shutdown is not exempt from skew protection, unlike /execute."""
    server = _Server()

    assert request_shutdown(BASE, TOKEN, client=server.client()) is True

    shutdown = server.request_to("/api/kernel/shutdown")
    assert shutdown.headers["Marimo-Server-Token"] == "skew-token"
    assert shutdown.headers["Authorization"] == f"Bearer {TOKEN}"


def test_request_shutdown_names_the_session_when_it_has_one() -> None:
    server = _Server()

    request_shutdown(BASE, TOKEN, session_id="kedge-abc", client=server.client())

    assert server.request_to("/api/kernel/shutdown").headers["Marimo-Session-Id"] == "kedge-abc"


def test_a_supplied_server_token_saves_a_round_trip() -> None:
    server = _Server()

    assert request_shutdown(BASE, TOKEN, server_token="skew-token", client=server.client()) is True
    assert "/" not in server.paths(), "the page is only fetched to scrape a token we lack"


def test_shutdown_is_attempted_even_when_the_skew_token_cannot_be_found() -> None:
    """Better to try and be refused than to skip straight to killing the process."""
    server = _Server(server_token=None)

    assert request_shutdown(BASE, TOKEN, client=server.client()) is False
    assert "/api/kernel/shutdown" in server.paths()


def test_request_shutdown_reports_failure_rather_than_raising() -> None:
    assert request_shutdown(BASE, TOKEN, client=_refusing_client()) is False
