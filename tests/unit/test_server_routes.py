"""The HTTP surface: the shell, context, health, sessions, and the turn stream."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import make_plan
from kedge import config as config_module
from kedge.agent.tools import PendingAmendment, PendingDeletion, ToolContext, ToolRegistry
from kedge.plan.review import acknowledge_all_drops, approve
from kedge.plan.store import PlanStore
from kedge.server import routes as routes_module
from kedge.server.agent_seam import CancelToken, ScriptedAgent, TurnRequest
from kedge.server.app import ServerError, create_app, require_loopback
from kedge.server.events import (
    MAX_ARGS_SUMMARY_CHARS,
    AnyEvent,
    DoneEvent,
    StatusEvent,
    TokenEvent,
    ToolCallEvent,
)
from kedge.server.sessions import SessionStore
from kedge.workspace import Workspace

# ── fixtures ─────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    monkeypatch.setenv("KEDGE_HOME", str(tmp_path / "home"))
    workbook = tmp_path / "rwa_monthly_v14.xlsx"
    workbook.write_bytes(b"PK\x03\x04 not really a workbook")
    built = Workspace.for_workbook(workbook)
    built.ensure_dirs()
    built.notebook_path.write_text("import marimo\n", encoding="utf-8")
    return built


@pytest.fixture(autouse=True)
def no_real_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real Windows Credential Manager is never consulted by a test.

    Without this, a developer who has stored a key under the default entry has `/api/models`
    send it to the real ``api.openai.com`` on every ``pytest`` run.
    """
    monkeypatch.setattr(config_module.keyring, "get_password", lambda service, username: None)


@pytest.fixture
def client(workspace: Workspace, tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(
        workspace,
        agent=ScriptedAgent(delay=0.0),
        store=SessionStore(tmp_path / "sessions.sqlite"),
        demo=True,
    )
    with TestClient(app) as opened:
        yield opened


def _new_session(client: TestClient) -> str:
    response = client.post("/api/sessions", json={})
    assert response.status_code == 201
    return response.json()["session"]["id"]


def _frames(text: str) -> list[dict]:
    """Parse an SSE body into the events it carried, ignoring comments."""
    events = []
    for block in text.split("\n\n"):
        payload = "\n".join(
            line[5:].strip() for line in block.split("\n") if line.startswith("data:")
        )
        if payload:
            events.append(json.loads(payload))
    return events


def _run_turn(client: TestClient, session_id: str, message: str) -> list[dict]:
    response = client.post(f"/api/sessions/{session_id}/turns", json={"message": message})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    return _frames(response.text)


# ── the shell and the iframe trap ────────────────────────────────────────────────────────────


def test_the_shell_is_served_at_the_root(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert "<iframe" in body
    assert 'id="notebook-frame"' in body


def test_the_state_endpoints_are_never_cached(client: TestClient) -> None:
    # Both report what this process holds right now, and both can change while a tab sits open.
    # A stale /api/context is a notebook pane insisting there is no notebook when there is one.
    for path in ("/api/context", "/api/health"):
        assert client.get(path).headers["cache-control"] == "no-store", path


def test_context_reports_the_notebook_url_once_marimo_answers(
    workspace: Workspace, client: TestClient
) -> None:
    """The pane is drawn from this, and it is not always knowable when the page first loads.

    A shell opened while marimo is still coming up gets ``notebook_url: null``. That is honest,
    and it is why the browser re-reads this rather than drawing the pane once and living with it.
    """
    assert client.get("/api/context").json()["notebook_url"] is None

    workspace.attach_marimo(host="127.0.0.1", port=2718, token="tok-abc123", pid=4242)
    workspace.set_session_id("kedge-session")

    payload = client.get("/api/context").json()
    assert payload["notebook_url"] is not None
    assert "access_token=tok-abc123" in payload["notebook_url"]
    assert payload["marimo"]["attached"] is True


def test_the_shell_is_never_cached(client: TestClient) -> None:
    # `/` answers with the chat shell or with the hub depending on whether a workbook is open, so
    # a browser holding one of them and reusing it for the other sends "Go to the notebook"
    # somewhere that is not the notebook.
    for path in ("/", "/hub"):
        assert client.get(path).headers["cache-control"] == "no-store", path


def test_the_static_assets_are_served(client: TestClient) -> None:
    for path in ("/static/app.js", "/static/styles.css"):
        assert client.get(path).status_code == 200


def test_the_static_assets_are_revalidated_rather_than_assumed_fresh(client: TestClient) -> None:
    # A page and the script that drives it are separate URLs that go stale independently. Without
    # this, a browser runs today's hub.js against yesterday's hub.html and the first element that
    # is not there yet takes the page down.
    for path in ("/static/app.js", "/static/hub.js", "/static/styles.css"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["cache-control"] == "no-cache", path


def test_the_hidden_attribute_is_not_disarmed_by_a_display_rule(client: TestClient) -> None:
    # Both scripts show and hide everything conditional by assigning `hidden`, and an author
    # `display` beats the user agent's own `[hidden] { display: none }` before specificity is
    # consulted -- author styles win over UA styles by origin. So one `.phase-chip { display:
    # inline-flex }` is enough to make `chip.hidden = true` do nothing, and the symptom does not
    # read as a styling glitch: the chip goes on saying "Thinking" after the turn has finished, the
    # Stop button stays on screen with no turn behind it and does nothing when pressed, and the
    # notebook placeholder -- absolutely positioned across the pane -- sits over a working iframe
    # insisting no notebook is attached.
    #
    # One unconditional rule makes every `display` in these stylesheets safe. `!important` is load
    # bearing: `#notebook-frame { display: block }` is an id selector and would otherwise win.
    styles = client.get("/static/styles.css").text
    guard = re.compile(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important", re.DOTALL)
    assert guard.search(styles), "styles.css must keep `hidden` working; hub.css layers on it"

    # The coupling the rule above protects, asserted so that deleting it fails here rather than in
    # front of a user.
    script = client.get("/static/app.js").text
    assert "placeholder.hidden = true" in script
    assert '$("stop").hidden = !running' in script


def test_the_ui_fetches_nothing_from_off_the_machine(client: TestClient) -> None:
    # Local-first, and PLAN's house style: no CDN, no npm, no webfont. The only absolute URLs
    # allowed are loopback and the SVG namespace identifier, which is a name rather than a fetch.
    allowed = re.compile(r"^(127\.0\.0\.1|localhost|www\.w3\.org)$")
    for path in ("/", "/static/app.js", "/static/styles.css"):
        hosts = re.findall(r"https?://([\w.-]+)", client.get(path).text)
        assert [host for host in hosts if not allowed.match(host)] == [], path


def test_the_ui_contains_no_emoji(client: TestClient) -> None:
    # A standing instruction, and it applies to UI copy as much as to code.
    ranges = ((0x2600, 0x27BF), (0x2B00, 0x2BFF), (0xFE00, 0xFE0F), (0x1F000, 0x1FAFF))
    for path in ("/", "/static/app.js", "/static/styles.css"):
        offenders = [
            character
            for character in client.get(path).text
            if any(low <= ord(character) <= high for low, high in ranges)
        ]
        assert offenders == [], path


def test_context_frames_the_notebook_with_the_token_in_the_query_string(
    workspace: Workspace, client: TestClient
) -> None:
    # PLAN 1.3: an iframe that loads unauthenticated lands on marimo's login page, the one
    # endpoint that sets X-Frame-Options: DENY. Putting the token in the query string means the
    # login page is never reached.
    workspace.attach_marimo(host="127.0.0.1", port=2718, token="tok-abc123", pid=4242)
    payload = client.get("/api/context").json()
    url = payload["notebook_url"]
    assert url.startswith("http://127.0.0.1:2718/?file=")
    assert "access_token=tok-abc123" in url
    assert "/auth/login" not in url
    assert payload["marimo"]["attached"] is True


def test_context_reports_no_notebook_url_when_marimo_is_not_attached(client: TestClient) -> None:
    payload = client.get("/api/context").json()
    assert payload["notebook_url"] is None
    assert payload["marimo"]["attached"] is False
    assert payload["workbook"]["name"] == "rwa_monthly_v14.xlsx"


# ── health ───────────────────────────────────────────────────────────────────────────────────


def test_health_reports_an_absent_kernel_rather_than_failing(client: TestClient) -> None:
    payload = client.get("/api/health").json()
    assert payload["server"] == "ok"
    assert payload["marimo"]["state"] == "absent"


def test_health_asks_the_server_rather_than_inspecting_a_pid(
    workspace: Workspace, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace.attach_marimo(host="127.0.0.1", port=2718, token="tok", pid=4242)
    monkeypatch.setattr("kedge.server.routes.health_check", lambda *a, **k: True)
    assert client.get("/api/health").json()["marimo"]["state"] == "running"
    monkeypatch.setattr("kedge.server.routes.health_check", lambda *a, **k: False)
    unreachable = client.get("/api/health").json()["marimo"]
    assert unreachable["state"] == "unreachable"
    assert "2718" in unreachable["detail"]


# ── models ───────────────────────────────────────────────────────────────────────────────────


def test_models_answers_without_a_network_call_in_demo_mode(client: TestClient) -> None:
    payload = client.get("/api/models").json()
    assert payload["source"] == "demo"
    assert payload["models"]
    assert payload["selected"]


def _live_client(workspace: Workspace, tmp_path: Path) -> TestClient:
    """A client with demo mode off, so the model routes take their real branches."""
    return TestClient(
        create_app(
            workspace,
            agent=ScriptedAgent(delay=0.0),
            store=SessionStore(tmp_path / "s.sqlite"),
            demo=False,
        )
    )


def test_models_degrades_to_the_configured_model_without_a_key(
    workspace: Workspace, tmp_path: Path
) -> None:
    with _live_client(workspace, tmp_path) as opened:
        payload = opened.get("/api/models").json()

    assert payload["source"] == "configured"
    assert payload["models"] == [workspace.config.model.model]


def test_models_degrades_to_the_configured_model_when_the_endpoint_will_not_list(
    workspace: Workspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plenty of OpenAI-compatible servers do not implement /models (PLAN M6)."""
    monkeypatch.setattr(
        config_module.keyring, "get_password", lambda service, username: "sk-stored"
    )

    async def refuse(base_url: str, api_key: str, *, ca_bundle: Path | None = None) -> list[str]:
        del ca_bundle  # accepted so the route cannot silently stop passing it (kedge.tls)
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(routes_module, "fetch_model_names", refuse)

    with _live_client(workspace, tmp_path) as opened:
        payload = opened.get("/api/models").json()

    assert payload["source"] == "unavailable"
    assert payload["models"] == [workspace.config.model.model]
    assert "type a model name" in payload["detail"].lower()


# ── sessions ─────────────────────────────────────────────────────────────────────────────────


def test_a_session_can_be_created_listed_renamed_and_deleted(client: TestClient) -> None:
    session_id = _new_session(client)
    assert [s["id"] for s in client.get("/api/sessions").json()["sessions"]] == [session_id]

    renamed = client.patch(f"/api/sessions/{session_id}", json={"title": "Haircuts"})
    assert renamed.json()["session"]["title"] == "Haircuts"

    assert client.delete(f"/api/sessions/{session_id}").status_code == 200
    assert client.get("/api/sessions").json()["sessions"] == []


def test_an_unknown_session_is_a_404_naming_the_id(client: TestClient) -> None:
    response = client.get("/api/sessions/nope")
    assert response.status_code == 404
    assert "nope" in response.json()["detail"]


def test_a_session_snapshots_the_notebook_and_notices_when_it_moves(
    workspace: Workspace, client: TestClient
) -> None:
    session_id = _new_session(client)
    assert client.get(f"/api/sessions/{session_id}").json()["drifted"] is False

    workspace.notebook_path.write_text("import marimo\n\n# edited by hand\n", encoding="utf-8")
    reopened = client.get(f"/api/sessions/{session_id}").json()
    assert reopened["drifted"] is True


# ── the turn stream ──────────────────────────────────────────────────────────────────────────


def test_a_turn_streams_the_whole_m3_event_vocabulary(client: TestClient) -> None:
    events = _run_turn(client, _new_session(client), "translate the haircut region")
    kinds = [event["type"] for event in events]
    for expected in (
        "status",
        "token",
        "tool_call",
        "tool_result",
        "cell_created",
        "cell_running",
        "cell_result",
        "validation",
        "done",
    ):
        assert expected in kinds, expected
    assert kinds[-1] == "done"
    assert kinds.count("done") == 1


def test_a_turn_stream_never_carries_raw_tool_arguments(client: TestClient) -> None:
    # The scripted agent calls propose_cell with a 214-character body. None of it may appear on
    # the wire: tool_call reports a summary, and the summary is derived rather than supplied.
    body = client.post(
        f"/api/sessions/{_new_session(client)}/turns", json={"message": "translate it"}
    ).text
    calls = [event for event in _frames(body) if event["type"] == "tool_call"]
    assert calls
    for call in calls:
        assert set(call) == {"type", "name", "args_summary"}
        assert len(call["args_summary"]) <= MAX_ARGS_SUMMARY_CHARS
        assert "\n" not in call["args_summary"]
    assert "x" * 60 not in body
    assert "code=<214 chars>" in body


def test_a_failing_turn_reports_violations_and_still_finishes(client: TestClient) -> None:
    events = _run_turn(client, _new_session(client), "make this fail please")
    validation = next(event for event in events if event["type"] == "validation")
    assert validation["ok"] is False
    assert len(validation["violations"]) == 2
    assert events[-1]["type"] == "done"


def test_a_turn_is_persisted_as_a_replayable_transcript(client: TestClient) -> None:
    session_id = _new_session(client)
    _run_turn(client, session_id, "translate the haircut region")
    payload = client.get(f"/api/sessions/{session_id}").json()

    roles = [message["role"] for message in payload["messages"]]
    assert roles == ["user", "assistant"]
    assistant = payload["messages"][1]
    assert "haircut" in assistant["content"]
    # The stored trail interleaves prose with activity, so a reopened session renders through
    # exactly the same path as a live one.
    kinds = [item["type"] for item in assistant["events"]]
    assert kinds[0] == "status"
    assert "token" in kinds and "cell_created" in kinds
    assert kinds[-1] == "done"


def test_the_first_message_of_a_turn_names_the_session(client: TestClient) -> None:
    session_id = _new_session(client)
    _run_turn(client, session_id, "Translate the haircut lookup")
    assert client.get(f"/api/sessions/{session_id}").json()["session"]["title"] == (
        "Translate the haircut lookup"
    )


def test_a_turn_against_an_unknown_session_is_a_404(client: TestClient) -> None:
    response = client.post("/api/sessions/nope/turns", json={"message": "hello"})
    assert response.status_code == 404


def test_an_empty_message_is_rejected(client: TestClient) -> None:
    response = client.post(f"/api/sessions/{_new_session(client)}/turns", json={"message": ""})
    assert response.status_code == 422


def test_the_turn_id_is_returned_so_the_ui_can_cancel_it(client: TestClient) -> None:
    with client.stream(
        "POST", f"/api/sessions/{_new_session(client)}/turns", json={"message": "hello"}
    ) as response:
        assert response.headers["x-kedge-turn-id"]
        response.read()


def test_cancelling_a_turn_that_is_not_running_is_a_404(client: TestClient) -> None:
    assert client.post("/api/turns/deadbeef/cancel").status_code == 404


# ── an agent that misbehaves ─────────────────────────────────────────────────────────────────


class _ExplodingAgent:
    def run(self, request: TurnRequest, *, cancel: CancelToken) -> AsyncIterator[AnyEvent]:
        async def generate() -> AsyncIterator[AnyEvent]:
            yield StatusEvent(phase="thinking")
            msg = "the model endpoint refused the connection"
            raise RuntimeError(msg)
            yield  # pragma: no cover - unreachable, keeps this an async generator

        return generate()


class _SilentAgent:
    def run(self, request: TurnRequest, *, cancel: CancelToken) -> AsyncIterator[AnyEvent]:
        async def generate() -> AsyncIterator[AnyEvent]:
            yield TokenEvent(text="I forgot to finish.")

        return generate()


def _client_for(workspace: Workspace, agent: object, tmp_path: Path) -> TestClient:
    app = create_app(workspace, agent=agent, store=SessionStore(tmp_path / "x.sqlite"), demo=True)
    return TestClient(app)


def test_an_agent_that_raises_becomes_an_error_event_not_a_dropped_connection(
    workspace: Workspace, tmp_path: Path
) -> None:
    with _client_for(workspace, _ExplodingAgent(), tmp_path) as opened:
        events = _run_turn(opened, _new_session(opened), "hello")
    kinds = [event["type"] for event in events]
    assert kinds == ["status", "error", "done"]
    assert "refused the connection" in events[1]["message"]


def test_an_agent_that_forgets_to_finish_still_gets_a_done(
    workspace: Workspace, tmp_path: Path
) -> None:
    # Otherwise the UI spins for ever on a loop with a missing branch.
    with _client_for(workspace, _SilentAgent(), tmp_path) as opened:
        events = _run_turn(opened, _new_session(opened), "hello")
    assert [event["type"] for event in events] == ["token", "done"]


# ── genuine streaming ────────────────────────────────────────────────────────────────────────


class _GatedAgent:
    """Yields one event, waits to be told the client saw it, then yields the rest.

    A buffered response cannot satisfy this: the client cannot open the gate until it has read
    the first event, and the server cannot finish until the gate is open.
    """

    def __init__(self) -> None:
        self.gate = asyncio.Event()

    def run(self, request: TurnRequest, *, cancel: CancelToken) -> AsyncIterator[AnyEvent]:
        gate = self.gate

        async def generate() -> AsyncIterator[AnyEvent]:
            yield StatusEvent(phase="analysing")
            await asyncio.wait_for(gate.wait(), timeout=5.0)
            yield TokenEvent(text="the client was already reading")
            yield DoneEvent(turn_id=request.turn_id, tokens_used=7)

        return generate()


@contextlib.asynccontextmanager
async def _serve(app: FastAPI) -> AsyncIterator[str]:
    """Run ``app`` on a real loopback socket, yielding its base URL.

    A real server rather than an in-process transport, because neither ``TestClient`` nor
    ``httpx.ASGITransport`` streams — both collect the whole response body before handing it
    back, which would make an assertion about incremental delivery vacuous.
    """
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


async def test_events_arrive_incrementally_rather_than_buffered(
    workspace: Workspace, tmp_path: Path
) -> None:
    agent = _GatedAgent()
    app = create_app(workspace, agent=agent, store=SessionStore(tmp_path / "g.sqlite"), demo=True)
    async with _serve(app) as base_url, httpx.AsyncClient(base_url=base_url) as opened:
        session_id = (await opened.post("/api/sessions", json={})).json()["session"]["id"]

        seen: list[str] = []
        async with opened.stream(
            "POST", f"/api/sessions/{session_id}/turns", json={"message": "hello"}
        ) as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if not line.startswith("event: "):
                    continue
                seen.append(line.removeprefix("event: ").strip())
                if seen == ["status"]:
                    # Proof of incremental delivery: the server cannot produce anything else
                    # until this runs, and this cannot run until the first frame has arrived.
                    agent.gate.set()
        assert seen == ["status", "token", "done"]


async def test_a_turn_in_flight_can_be_cancelled(workspace: Workspace, tmp_path: Path) -> None:
    app = create_app(
        workspace,
        agent=ScriptedAgent(delay=0.02),
        store=SessionStore(tmp_path / "c.sqlite"),
        demo=True,
    )
    async with _serve(app) as base_url, httpx.AsyncClient(base_url=base_url) as opened:
        session_id = (await opened.post("/api/sessions", json={})).json()["session"]["id"]

        kinds: list[str] = []
        async with opened.stream(
            "POST", f"/api/sessions/{session_id}/turns", json={"message": "translate it"}
        ) as response:
            turn_id = response.headers["x-kedge-turn-id"]
            async for line in response.aiter_lines():
                if not line.startswith("event: "):
                    continue
                kinds.append(line.removeprefix("event: ").strip())
                if len(kinds) == 3:
                    cancelled = await opened.post(f"/api/turns/{turn_id}/cancel")
                    assert cancelled.status_code == 200

    assert kinds[-1] == "done"
    assert "error" in kinds
    # Cancelling has to actually shorten the turn, not merely be acknowledged.
    assert len(kinds) < 40


# ── binding ──────────────────────────────────────────────────────────────────────────────────


def test_only_loopback_may_be_bound() -> None:
    assert require_loopback("127.0.0.1") == "127.0.0.1"
    with pytest.raises(ServerError, match="loopback only"):
        require_loopback("0.0.0.0")


# ── the scripted stand-in ────────────────────────────────────────────────────────────────────


async def test_the_scripted_agent_satisfies_the_agent_loop_protocol() -> None:
    from kedge.server.agent_seam import AgentLoop

    assert isinstance(ScriptedAgent(), AgentLoop)


async def test_the_scripted_agent_never_emits_a_raw_argument() -> None:
    request = TurnRequest(turn_id="t1", session_id="s1", message="translate it")
    events = [event async for event in ScriptedAgent(delay=0.0).run(request, cancel=CancelToken())]
    calls = [event for event in events if isinstance(event, ToolCallEvent)]
    assert calls
    assert all(len(call.args_summary) <= MAX_ARGS_SUMMARY_CHARS for call in calls)
    assert isinstance(events[-1], DoneEvent)


# ── pending decisions: the user's half of delete_cell and amend_plan ─────────────────────────
#
# Both tools record a request and refuse to act. Until these routes existed there was no way to
# say yes, which made both write-only. What is asserted here is the gate: an approval writes a
# new plan *version* and never edits the approved one in place, and a deletion reaches the
# notebook only through the driver and only on an explicit request from the browser.


class _RecordingDriver:
    """The narrowest thing the deletion route needs of a notebook bridge."""

    def __init__(self, *, ok: bool = True) -> None:
        self.deleted: list[str] = []
        self._ok = ok

    async def delete_cell(self, target: str) -> SimpleNamespace:
        self.deleted.append(target)
        return SimpleNamespace(ok=self._ok, error=None if self._ok else "the kernel said no")


class _AgentWithRegistries:
    """A loop that is a ScriptedAgent plus the per-session registries the real one carries."""

    def __init__(self, registries: dict) -> None:
        self.registries = registries
        self._inner = ScriptedAgent(delay=0.0)

    def run(self, request: TurnRequest, *, cancel: CancelToken) -> AsyncIterator[AnyEvent]:
        return self._inner.run(request, cancel=cancel)


def _tool_registry(**context_kwargs: object) -> ToolRegistry:
    return ToolRegistry(ToolContext(**context_kwargs))


@pytest.fixture
def pending_client(
    workspace: Workspace, tmp_path: Path
) -> Iterator[tuple[TestClient, dict[str, ToolRegistry]]]:
    """A server whose agent carries a real ToolRegistry, keyed by session id."""
    registries: dict[str, ToolRegistry] = {}
    app = create_app(
        workspace,
        agent=_AgentWithRegistries(registries),
        store=SessionStore(tmp_path / "pending.sqlite"),
    )
    with TestClient(app) as opened:
        yield opened, registries


def _approved_plan_store(directory: Path) -> PlanStore:
    """Save an approved plan and return its store."""
    store = PlanStore(directory)
    store.save(approve(acknowledge_all_drops(make_plan(), note="reviewed"), by="tests"))
    return store


def test_a_session_with_no_registry_reports_no_pending_decisions(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]],
) -> None:
    """Normal, not broken: demo mode has no registries and a fresh session has made no calls."""
    client, _registries = pending_client
    session_id = _new_session(client)

    assert client.get(f"/api/sessions/{session_id}/pending").json() == {
        "deletions": [],
        "amendments": [],
    }


def test_a_recorded_deletion_is_surfaced_with_what_reads_the_doomed_cell(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]],
) -> None:
    client, registries = pending_client
    session_id = _new_session(client)
    registry = _tool_registry()
    registry.pending_deletions.append(
        PendingDeletion(cell="load_handin", reason="superseded", descendants=("apply_haircuts",))
    )
    registries[session_id] = registry

    payload = client.get(f"/api/sessions/{session_id}/pending").json()

    assert payload["deletions"] == [
        {
            "index": 0,
            "cell": "load_handin",
            "reason": "superseded",
            "descendants": ["apply_haircuts"],
        }
    ]


def test_confirming_a_deletion_runs_it_through_the_driver_and_clears_the_request(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]],
) -> None:
    client, registries = pending_client
    session_id = _new_session(client)
    driver = _RecordingDriver()
    registry = _tool_registry(driver=driver)
    registry.pending_deletions.append(PendingDeletion(cell="stale_cell", reason="dead code"))
    registries[session_id] = registry

    response = client.post(f"/api/sessions/{session_id}/pending/deletions/0", json={})

    assert response.status_code == 200
    assert driver.deleted == ["stale_cell"]
    assert response.json()["pending"]["deletions"] == []


def test_declining_a_deletion_leaves_the_cell_alone(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]],
) -> None:
    client, registries = pending_client
    session_id = _new_session(client)
    driver = _RecordingDriver()
    registry = _tool_registry(driver=driver)
    registry.pending_deletions.append(PendingDeletion(cell="keep_me", reason="the model guessed"))
    registries[session_id] = registry

    response = client.delete(f"/api/sessions/{session_id}/pending/deletions/0")

    assert response.status_code == 200
    assert driver.deleted == []
    assert registry.pending_deletions == []


def test_confirming_a_deletion_with_no_kernel_says_so_rather_than_failing_silently(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]],
) -> None:
    client, registries = pending_client
    session_id = _new_session(client)
    registry = _tool_registry()  # no driver
    registry.pending_deletions.append(PendingDeletion(cell="orphan", reason="no kernel"))
    registries[session_id] = registry

    response = client.post(f"/api/sessions/{session_id}/pending/deletions/0", json={})

    assert response.status_code == 409
    assert "cannot be deleted" in response.json()["detail"]


def test_a_decision_that_is_already_decided_404s_rather_than_repeating(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]],
) -> None:
    client, registries = pending_client
    session_id = _new_session(client)
    registries[session_id] = _tool_registry()

    response = client.post(f"/api/sessions/{session_id}/pending/deletions/0", json={})

    assert response.status_code == 404


def test_approving_an_amendment_writes_a_new_plan_version_and_keeps_the_old_one(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]], tmp_path: Path
) -> None:
    client, registries = pending_client
    session_id = _new_session(client)
    store = _approved_plan_store(tmp_path / "plans")
    before = store.versions()
    registry = _tool_registry(plans=store)
    registry.pending_amendments.append(
        PendingAmendment(
            rationale="the workbook rounds after the join, not before",
            change="round in apply_haircuts rather than in write_output",
            stage="apply_haircuts",
        )
    )
    registries[session_id] = registry

    response = client.post(f"/api/sessions/{session_id}/pending/amendments/0", json={})

    assert response.status_code == 200
    payload = response.json()
    assert payload["version"] > max(before)
    assert set(store.versions()) > set(before), "history is retained, never overwritten"

    written = store.load(payload["version"])
    notes = written.stage("apply_haircuts").notes or ""
    assert "round in apply_haircuts" in notes
    assert "the workbook rounds after the join" in notes
    assert registry.pending_amendments == []


def test_an_approved_amendment_lands_approved_so_the_gate_is_completed_not_bypassed(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]], tmp_path: Path
) -> None:
    """The user read it and clicked approve; that is the gate, and it is theirs to close."""
    client, registries = pending_client
    session_id = _new_session(client)
    store = _approved_plan_store(tmp_path / "plans")
    registry = _tool_registry(plans=store)
    registry.pending_amendments.append(
        PendingAmendment(rationale="because", change="a plan-level note", stage=None)
    )
    registries[session_id] = registry

    payload = client.post(f"/api/sessions/{session_id}/pending/amendments/0", json={}).json()

    assert payload["approved"] is True
    assert payload["blockers"] == []
    written = store.load(payload["version"])
    assert written.approval.approved is True
    assert written.based_on_version is not None
    assert any(question.question == "a plan-level note" for question in written.open_questions)


def test_an_amendment_to_a_plan_that_cannot_be_approved_lands_as_a_draft_with_the_blockers(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]], tmp_path: Path
) -> None:
    """`approve` refusing is the gate working, not an error to route around."""
    client, registries = pending_client
    session_id = _new_session(client)
    store = PlanStore(tmp_path / "plans")
    store.save(make_plan())  # unapproved, with an unacknowledged dropped range
    registry = _tool_registry(plans=store)
    registry.pending_amendments.append(
        PendingAmendment(rationale="because", change="something", stage=None)
    )
    registries[session_id] = registry

    payload = client.post(f"/api/sessions/{session_id}/pending/amendments/0", json={}).json()

    assert payload["approved"] is False
    assert payload["blockers"], "the reasons must be reported, not swallowed"
    assert store.load(payload["version"]).approval.approved is False


def test_declining_an_amendment_leaves_the_plan_history_untouched(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]], tmp_path: Path
) -> None:
    client, registries = pending_client
    session_id = _new_session(client)
    store = _approved_plan_store(tmp_path / "plans")
    before = store.versions()
    registry = _tool_registry(plans=store)
    registry.pending_amendments.append(
        PendingAmendment(rationale="because", change="no thanks", stage=None)
    )
    registries[session_id] = registry

    response = client.delete(f"/api/sessions/{session_id}/pending/amendments/0")

    assert response.status_code == 200
    assert store.versions() == before
    assert registry.pending_amendments == []


def test_approving_an_amendment_with_no_plan_on_disk_explains_rather_than_500s(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]], tmp_path: Path
) -> None:
    client, registries = pending_client
    session_id = _new_session(client)
    registry = _tool_registry(plans=PlanStore(tmp_path / "empty-plans"))
    registry.pending_amendments.append(
        PendingAmendment(rationale="because", change="something", stage=None)
    )
    registries[session_id] = registry

    response = client.post(f"/api/sessions/{session_id}/pending/amendments/0", json={})

    assert response.status_code == 422
    assert "no plan on disk" in response.json()["detail"]
