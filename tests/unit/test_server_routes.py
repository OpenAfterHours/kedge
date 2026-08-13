"""The HTTP surface: the shell, context, health, sessions, and the turn stream."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import make_analysis, make_draft, make_operation, make_plan
from kedge import config as config_module
from kedge.agent.context import TokenCounter
from kedge.agent.loop import KedgeAgent
from kedge.agent.tools import (
    PendingAmendment,
    PendingDeletion,
    PendingProposal,
    ToolContext,
    ToolRegistry,
)
from kedge.analysis.model import CachedValueCoverage, WorkbookAnalysis
from kedge.plan.model import DroppedRange, ProcessPlan, Stage
from kedge.plan.review import (
    acknowledge_all_drops,
    add_stage,
    approve,
    reject,
    request_changes,
)
from kedge.plan.store import PlanStore
from kedge.plan.triage import triage
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


class _ClosableRegistry:
    """A registry that records being closed. The route needs nothing else of one."""

    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


def test_deleting_a_session_clears_what_the_agent_still_holds(
    workspace: Workspace, tmp_path: Path
) -> None:
    """The rows were only ever half of it.

    The agent keeps a digest, the tool traffic of the last turn and the session's tool registry,
    and the held turn is now populated on essentially every turn rather than only a paused one —
    a couple of hundred messages of sampled workbook rows, resident for the life of the process.
    A user who deletes a conversation and still has its sampled data in memory is the leak
    `SECURITY.md` is about: the sensitivity of the workbook is the sensitivity of the conversation.

    The private dicts are reached for deliberately. They are what `reset_session` names, and a
    rename that quietly stopped clearing one of them should fail here rather than in a memory dump.
    """
    agent = KedgeAgent(
        client=SimpleNamespace(),
        context=ToolContext(),
        counter=TokenCounter(allow_download=False),
        system_prompt="SYSTEM PROMPT",
    )
    app = create_app(workspace, agent=agent, store=SessionStore(tmp_path / "sessions.sqlite"))
    with TestClient(app) as client:
        session_id = _new_session(client)
        registry = _ClosableRegistry()
        agent.registries[session_id] = registry
        held = agent._suspended
        digests = agent._digests
        held[session_id] = ["sampled rows from Calc!A1:H500"]
        digests[session_id] = "a summary of the conversation so far"

        assert client.delete(f"/api/sessions/{session_id}").status_code == 200

        assert agent.registries == {}, "the tool registry and its result cache are gone"
        assert held == {}, "so is the held turn, which is where the sampled rows live"
        assert digests == {}
        assert registry.closed is True, "the workbook handle is released, not merely dropped"


def test_deleting_a_session_succeeds_when_no_agent_is_attached(client: TestClient) -> None:
    """Sessions are deleted from the hub with no workbook open and no model configured.

    Answering 409 there would be refusing to delete a conversation because there is nobody to
    tell about it. The demo agent has no per-session state at all, and the delete is unaffected.
    """
    session_id = _new_session(client)

    assert client.delete(f"/api/sessions/{session_id}").status_code == 200
    assert client.get("/api/sessions").json()["sessions"] == []


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
        "proposals": [],
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
    """`approve` refusing is the gate working, not an error to route around.

    The plan on disk is approved — nothing else is amendable — but carries a drop nobody has
    acknowledged, which is what a hand-edit of the YAML looks like. The plan file is a review
    artifact users are invited to edit, so a plan that is approved and no longer approvable is a
    state the route has to survive.
    """
    client, registries = pending_client
    session_id = _new_session(client)
    store = PlanStore(tmp_path / "plans")
    approved = approve(acknowledge_all_drops(make_plan(), note="read"), by="phil")
    store.save(
        approved.model_copy(
            update={"dropped": [DroppedRange(range="Calc!BB:BD", reason="added by hand")]}
        )
    )
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


# ── an amendment amends what is in force, and nothing else ───────────────────────────────────
#
# An amendment card shows one sentence. Approving it must therefore put one sentence into force,
# never a decomposition the user has not read: the plan it is written against has to be the
# *approved* one. Reached through `latest_approved() or latest()` it was not, and approving
# "mention the FX rate source" against a rejected v2 wrote v3 approved carrying the whole of v2.


@pytest.mark.parametrize(
    ("state", "prepare"),
    [
        pytest.param("draft", lambda plan: plan, id="draft"),
        pytest.param(
            "changes_requested",
            lambda plan: request_changes(plan, by="phil", note="split the haircut stage"),
            id="changes_requested",
        ),
        pytest.param(
            "rejected",
            lambda plan: reject(plan, by="phil", reason="the decomposition is wrong"),
            id="rejected",
        ),
    ],
)
def test_an_amendment_refuses_against_a_plan_that_is_not_in_force(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]],
    tmp_path: Path,
    state: str,
    prepare,
) -> None:
    """One approved sentence must not carry an unread decomposition into force with it."""
    client, registries = pending_client
    session_id = _new_session(client)
    store = PlanStore(tmp_path / "plans")
    store.save(prepare(acknowledge_all_drops(make_plan(), note="read")))
    before = store.versions()
    registry = _tool_registry(plans=store)
    registry.pending_amendments.append(
        PendingAmendment(rationale="the rate moved", change="mention the FX rate source")
    )
    registries[session_id] = registry

    response = client.post(f"/api/sessions/{session_id}/pending/amendments/0", json={})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert state in detail, "the user is told which state the plan is actually in"
    assert "in full" in detail
    assert store.versions() == before, "nothing was written"


def test_an_amendment_against_the_approved_plan_still_works(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]], tmp_path: Path
) -> None:
    """The other half of the parametrised refusal: the gate closes on everything but `approved`."""
    client, registries = pending_client
    session_id = _new_session(client)
    store = _approved_plan_store(tmp_path / "plans")
    registry = _tool_registry(plans=store)
    registry.pending_amendments.append(
        PendingAmendment(rationale="the rate moved", change="mention the FX rate source")
    )
    registries[session_id] = registry

    payload = client.post(f"/api/sessions/{session_id}/pending/amendments/0", json={}).json()

    assert payload["approved"] is True


def test_an_amendment_never_writes_a_later_unapproved_version_into_force(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]], tmp_path: Path
) -> None:
    """`latest()` is not `latest_approved()`: v2 being newer does not make it reviewed."""
    client, registries = pending_client
    session_id = _new_session(client)
    store = PlanStore(tmp_path / "plans")
    approved = approve(acknowledge_all_drops(make_plan(), note="read"), by="phil")
    store.save(approved)
    superseding = add_stage(
        approved, Stage(id="a_stage_nobody_reviewed", intent="Invented after approval")
    )
    store.save(superseding)
    registry = _tool_registry(plans=store)
    registry.pending_amendments.append(
        PendingAmendment(rationale="the rate moved", change="mention the FX rate source")
    )
    registries[session_id] = registry

    payload = client.post(f"/api/sessions/{session_id}/pending/amendments/0", json={}).json()

    written = store.load(payload["version"])
    assert "a_stage_nobody_reviewed" not in written.stage_ids
    assert written.stage_ids == approved.stage_ids


# ── the user's half of propose_plan ──────────────────────────────────────────────────────────
#
# `propose_plan` authors a whole plan and writes nothing. These routes are where the user reads it
# and decides, and what is asserted is the same gate the amendment routes assert: approval writes
# a *version* through the store, a plan that is not approvable on its own terms lands as a draft
# with its blockers said out loud, and nothing at all reaches disk without a request from the
# browser. One thing more is asserted here that has no amendment equivalent — that a proposal
# cannot be written over a plan approved after it was made, which is the refusal in the tool
# holding at the other end of the wait.


def _proposed_plan(**overrides: object) -> ProcessPlan:
    """A plan as `propose_plan` records it: authored by the model, unapproved."""
    return make_plan(generated_by="llm", llm_model="gpt-5.6-terra", **overrides)


def test_a_recorded_proposal_is_surfaced_as_a_plan_the_user_can_read(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]],
) -> None:
    client, registries = pending_client
    session_id = _new_session(client)
    registry = _tool_registry()
    registry.pending_proposals.append(PendingProposal(plan=_proposed_plan()))
    registries[session_id] = registry

    proposal = client.get(f"/api/sessions/{session_id}/pending").json()["proposals"][0]

    assert proposal["index"] == 0
    assert [stage["id"] for stage in proposal["stages"]] == [
        "load_handin",
        "apply_haircuts",
        "manual_overrides",
        "write_output",
    ]
    assert any(stage["checkpoint"] for stage in proposal["stages"])
    assert proposal["open_questions"], "an open question the user never sees is not a question"
    assert proposal["dropped"] == [
        {
            "range": "Calc!AK:AP",
            "reason": "no downstream refs, all zero since 2023",
            # The decision travels with the drop: the card draws a control for an outstanding one
            # and the recorded outcome for a decided one, and cannot tell them apart without this.
            "acknowledged": False,
            "accepted": True,
            "note": None,
        }
    ]


def test_approving_a_proposal_writes_the_plan_the_model_authored(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]], tmp_path: Path
) -> None:
    client, registries = pending_client
    session_id = _new_session(client)
    store = PlanStore(tmp_path / "plans")
    registry = _tool_registry(plans=store)
    registry.pending_proposals.append(
        PendingProposal(plan=_proposed_plan(draft=acknowledge_all_drops(make_plan()).to_draft()))
    )
    registries[session_id] = registry

    response = client.post(f"/api/sessions/{session_id}/pending/proposals/0", json={})

    assert response.status_code == 200
    payload = response.json()
    assert payload["approved"] is True
    written = store.load(payload["version"])
    assert written.approval.approved is True
    assert written.generated_by == "llm", "the history shows where this plan came from"
    assert written.llm_model == "gpt-5.6-terra"
    assert registry.pending_proposals == []


def test_a_proposal_is_approved_by_a_click_that_carries_no_body(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]], tmp_path: Path
) -> None:
    """The request the pane actually sends, which no other test here sends.

    Every test around this one posts ``json={}``; the browser posts nothing at all. Declaring the
    body required therefore passed the suite and failed every decision button in the pane -- a 422
    raised before the handler ran, so the card stayed and the plan was never written, and FastAPI's
    list of validation errors reached `notice` as "[object Object]".
    """
    client, registries = pending_client
    session_id = _new_session(client)
    store = PlanStore(tmp_path / "plans")
    registry = _tool_registry(plans=store)
    registry.pending_proposals.append(
        PendingProposal(plan=_proposed_plan(draft=acknowledge_all_drops(make_plan()).to_draft()))
    )
    registries[session_id] = registry

    response = client.post(
        f"/api/sessions/{session_id}/pending/proposals/0",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["approved"] is True
    assert store.load(payload["version"]).approval.approved is True
    assert store.load(payload["version"]).approval.note is None, "no body means no note"
    assert registry.pending_proposals == []


def test_no_decision_route_requires_a_body(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]],
) -> None:
    """Every one of them, checked at the schema rather than one at a time through a fixture.

    A body carrying one optional note is not worth failing a click over on any of them, and the
    route that regresses next is the one nobody wrote the fixture for.
    """
    client, _registries = pending_client
    paths = client.get("/openapi.json").json()["paths"]

    drops = "/api/sessions/{session_id}/pending/proposals/{index}/drops"
    required = {
        f"{name}s": paths[f"/api/sessions/{{session_id}}/pending/{name}s/{{index}}"]["post"]
        .get("requestBody", {})
        .get("required", False)
        for name in ("deletion", "amendment", "proposal")
    } | {
        tail: paths[f"{drops}/{tail}"]["post"].get("requestBody", {}).get("required", False)
        for tail in ("{drop}/acknowledge", "{drop}/refuse", "acknowledge-all")
    }

    assert required == {
        "deletions": False,
        "amendments": False,
        "proposals": False,
        "{drop}/acknowledge": False,
        "{drop}/refuse": False,
        "acknowledge-all": False,
    }


def test_an_approval_note_is_the_reviewers_or_nobodys(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]], tmp_path: Path
) -> None:
    """`approval.note` reads months later as the reviewer's account of what they checked.

    Defaulting it to the plan's own summary filled that field with the model's prose -- an
    account of the work written by the thing being reviewed. Absent is honest; borrowed is not.
    """
    client, registries = pending_client
    session_id = _new_session(client)
    store = PlanStore(tmp_path / "plans")
    draft = acknowledge_all_drops(make_plan()).to_draft()
    registry = _tool_registry(plans=store)
    registry.pending_proposals.append(PendingProposal(plan=_proposed_plan(draft=draft)))
    registries[session_id] = registry

    silent = client.post(f"/api/sessions/{session_id}/pending/proposals/0", json={}).json()

    written = store.load(silent["version"])
    assert written.approval.note is None
    assert written.summary not in (written.approval.note or "")


def test_a_proposal_that_cannot_be_approved_lands_as_a_draft_with_the_blockers(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]], tmp_path: Path
) -> None:
    """An unacknowledged drop blocks approval, and the gate refusing is the gate working."""
    client, registries = pending_client
    session_id = _new_session(client)
    store = PlanStore(tmp_path / "plans")
    registry = _tool_registry(plans=store)
    registry.pending_proposals.append(PendingProposal(plan=_proposed_plan()))
    registries[session_id] = registry

    payload = client.post(f"/api/sessions/{session_id}/pending/proposals/0", json={}).json()

    assert payload["approved"] is False
    assert any("Calc!AK:AP" in blocker for blocker in payload["blockers"])
    written = store.load(payload["version"])
    assert written.approval.approved is False
    assert written.generated_by == "llm"


def test_declining_a_proposal_leaves_no_trace(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]], tmp_path: Path
) -> None:
    client, registries = pending_client
    session_id = _new_session(client)
    store = PlanStore(tmp_path / "plans")
    registry = _tool_registry(plans=store)
    registry.pending_proposals.append(PendingProposal(plan=_proposed_plan()))
    registries[session_id] = registry

    response = client.delete(f"/api/sessions/{session_id}/pending/proposals/0")

    assert response.status_code == 200
    assert store.versions() == [], "a declined plan is not history, it never happened"
    assert registry.pending_proposals == []
    assert response.json()["pending"]["proposals"] == []


def test_a_proposal_cannot_be_written_over_a_plan_approved_while_it_waited(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]], tmp_path: Path
) -> None:
    """The tool's refusal has to hold at approval time too: the click can arrive much later."""
    client, registries = pending_client
    session_id = _new_session(client)
    store = _approved_plan_store(tmp_path / "plans")
    before = store.versions()
    registry = _tool_registry(plans=store)
    registry.pending_proposals.append(PendingProposal(plan=_proposed_plan()))
    registries[session_id] = registry

    response = client.post(f"/api/sessions/{session_id}/pending/proposals/0", json={})

    assert response.status_code == 422
    assert "amendment" in response.json()["detail"]
    assert store.versions() == before
    assert registry.pending_proposals == [], "a decided request is no longer pending"


def test_two_pending_proposals_cannot_both_be_written(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]], tmp_path: Path
) -> None:
    """One turn can record two; approving the first puts a plan in force and closes the door.

    `propose_plan` refuses once a plan is approved, but that check ran when the tool was called.
    Both of these were recorded before either was decided, so only the re-read in `_write_proposal`
    stands between the second click and a replacement decomposition landing as though nothing were
    in force.
    """
    client, registries = pending_client
    session_id = _new_session(client)
    store = PlanStore(tmp_path / "plans")
    registry = _tool_registry(plans=store)
    approvable = _proposed_plan(draft=acknowledge_all_drops(make_plan()).to_draft())
    registry.pending_proposals.append(PendingProposal(plan=approvable))
    registry.pending_proposals.append(PendingProposal(plan=approvable))
    registries[session_id] = registry

    first = client.post(f"/api/sessions/{session_id}/pending/proposals/0", json={})
    second = client.post(f"/api/sessions/{session_id}/pending/proposals/0", json={})

    assert first.json()["approved"] is True
    assert second.status_code == 422
    assert "amendment" in second.json()["detail"]
    assert len(store.versions()) == 1, "the second proposal reached no version of its own"


# ── the card has to support the decision it asks for ─────────────────────────────────────────
#
# The bar is `plan.review.render_plan`: what the CLI puts in front of a reviewer before the same
# decision. Anything the payload omits is something the user is asked to approve unseen, and the
# omissions were not small -- the assumptions a reviewer checks first, the DAG, the question a
# checkpoint actually asks, and every warning that checks the decomposition covers the workbook.


def _proposal_view(
    client: TestClient,
    registries: dict[str, ToolRegistry],
    plan: ProcessPlan,
    **context: object,
) -> dict:
    session_id = _new_session(client)
    registry = _tool_registry(**context)
    registry.pending_proposals.append(PendingProposal(plan=plan))
    registries[session_id] = registry
    return client.get(f"/api/sessions/{session_id}/pending").json()["proposals"][0]


def test_the_card_carries_what_render_plan_shows_a_reviewer(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]],
) -> None:
    client, registries = pending_client

    proposal = _proposal_view(client, registries, _proposed_plan(), analysis=make_analysis())

    stages = {stage["id"]: stage for stage in proposal["stages"]}
    assert stages["load_handin"]["assumptions"] == ["header on row 1", "one row per counterparty"]
    assert stages["apply_haircuts"]["depends_on"] == ["load_handin"]
    assert stages["apply_haircuts"]["operations"] == ["calc_h2_h500"]
    assert stages["apply_haircuts"]["excel_pattern"] == "vlookup_exact"
    assert stages["load_handin"]["checkpoint"] is None
    # Rendered rather than structured, because the card is a reading surface and `app.js` joins
    # these into a line. `render_plan` shows the same words.
    assert stages["load_handin"]["sources"] == ["handin"]
    assert stages["apply_haircuts"]["sources"] == ["range Calc!H2:H500", "range Ref!A1:D50"]


def test_the_card_carries_the_question_a_checkpoint_will_ask(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]],
) -> None:
    """`render_plan` prints it; the card said ", not automated". The question is the control."""
    client, registries = pending_client

    proposal = _proposal_view(client, registries, _proposed_plan(), analysis=make_analysis())

    checkpoint = next(stage["checkpoint"] for stage in proposal["stages"] if stage["checkpoint"])
    assert checkpoint["question"] == "Have this month's overrides been agreed with Risk?"
    assert checkpoint["options"] == ["approve", "reject"]


def test_the_card_carries_the_review_warnings_in_full(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]],
) -> None:
    """Including the only automatic check that the decomposition covers the workbook."""
    client, registries = pending_client
    analysis = make_analysis(operations=[make_operation(), make_operation("calc_z9_z99")])

    proposal = _proposal_view(client, registries, _proposed_plan(), analysis=analysis)

    assert proposal["warnings_complete"] is True
    assert any("claimed by no stage" in warning for warning in proposal["warnings"])
    assert any("low or unstated confidence" in warning for warning in proposal["warnings"])


def test_the_card_says_so_when_the_coverage_checks_could_not_run(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]],
) -> None:
    """A shorter list of warnings must not read as a cleaner plan."""
    client, registries = pending_client

    proposal = _proposal_view(client, registries, _proposed_plan())

    assert proposal["warnings_complete"] is False
    assert proposal["verdict"] is None


def test_the_card_pre_flights_the_approval_blockers(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]],
) -> None:
    """The button says "Approve"; without this the user learns it saved a draft afterwards."""
    client, registries = pending_client

    proposal = _proposal_view(client, registries, _proposed_plan(), analysis=make_analysis())

    assert proposal["unacknowledged_drops"] == 1
    assert any("Calc!AK:AP" in blocker for blocker in proposal["approval_blockers"])


def _triaged_plan(analysis: WorkbookAnalysis) -> ProcessPlan:
    """A proposal as `propose_plan` records one: the assessment is kedge's triage, not the model's."""
    return _proposed_plan(draft=make_draft(assessment=triage(analysis).as_assessment()))


def test_the_card_leads_with_a_stop_verdict(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]],
) -> None:
    """`propose_plan` refuses a STOP; the tool path never looked, so the word never arrived."""
    client, registries = pending_client
    analysis = make_analysis(workbook_fields={"file_format": "xlsb"})

    proposal = _proposal_view(client, registries, _triaged_plan(analysis), analysis=analysis)

    assert proposal["verdict"] == "stop"
    assert any("xlsb" in blocker for blocker in proposal["blockers"])


def test_a_workbook_with_no_baseline_reports_it_as_its_own_blocker(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]],
) -> None:
    """Non-negotiable 6. 1.00 convertible with "cannot be reconciled" trailing after a colon
    inverts the emphasis: the score is arithmetically right and reads exactly wrong."""
    client, registries = pending_client
    analysis = make_analysis(
        cached_values=CachedValueCoverage(
            formula_cell_count=400_000, cached_present_count=0, coverage=0.0, status="absent"
        )
    )

    proposal = _proposal_view(client, registries, _triaged_plan(analysis), analysis=analysis)

    assert proposal["convertible"] == 1.0, "a missing baseline does not make it harder to convert"
    assert any("no cached calculated values" in item for item in proposal["verification_blockers"])
    assert proposal["blockers"] == [], "and it is not restated as a conversion blocker"


def test_the_card_flags_an_analysis_that_predates_the_workbook(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]], workspace: Workspace
) -> None:
    """A plan scored from an analysis of a file the user has since re-saved is a plan about
    a workbook that no longer exists."""
    client, registries = pending_client
    workspace.analysis_path.parent.mkdir(parents=True, exist_ok=True)
    workspace.analysis_path.write_text("{}", encoding="utf-8")
    stale = _proposal_view(
        client, registries, _proposed_plan(), workspace=workspace, analysis=make_analysis()
    )
    assert stale["analysis_stale"] is False

    os.utime(workspace.workbook_path, ns=(time.time_ns(), time.time_ns() + 5_000_000_000))
    fresh = _proposal_view(
        client, registries, _proposed_plan(), workspace=workspace, analysis=make_analysis()
    )
    assert fresh["analysis_stale"] is True


def test_a_card_with_no_workspace_says_it_cannot_tell_rather_than_current(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]],
) -> None:
    """ "Cannot tell" is not "current", and rendering it as one would be the quieter lie."""
    client, registries = pending_client

    proposal = _proposal_view(client, registries, _proposed_plan())

    assert proposal["analysis_stale"] is None


# ── the model does not sign off its own deletions ────────────────────────────────────────────
#
# `unacknowledged_drops` is the only structural blocker a plan has: the thing standing between
# "kedge silently deleted six columns" and a bug report. `parse_draft` is where a model-authored
# draft loses the fields that record a *reviewer's* decision, and this asserts it end to end --
# the real tool records the proposal, the real route writes it -- because that is the path a
# forged acknowledgement would actually travel.


async def test_a_proposed_plan_cannot_acknowledge_its_own_dropped_ranges(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]], tmp_path: Path
) -> None:
    client, registries = pending_client
    session_id = _new_session(client)
    store = PlanStore(tmp_path / "plans")
    registry = _tool_registry(analysis=make_analysis(), plans=store)
    registries[session_id] = registry
    forged = json.dumps(
        {
            "stages": [{"id": "load_handin", "intent": "Read the hand-in", "kind": "load"}],
            "open_questions": [],
            "dropped": [
                {
                    "range": "Calc!AK:AP",
                    "reason": "unused",
                    "acknowledged": True,
                    "accepted": True,
                    "note": "signed off by the analyst",
                }
            ],
        }
    )

    # `propose_plan` refuses a plan written from nothing, so the model reads first, as it would.
    assert (await registry.dispatch("inspect_workbook", {"section": "operations"})).ok
    result = await registry.dispatch("propose_plan", {"plan": forged})
    assert result.ok, "the proposal is legitimate; only the signature on it is not"

    payload = client.post(f"/api/sessions/{session_id}/pending/proposals/0", json={}).json()

    assert payload["approved"] is False
    assert any("Calc!AK:AP" in blocker for blocker in payload["blockers"])
    written = store.load(payload["version"])
    assert written.dropped[0].acknowledged is False
    assert written.dropped[0].note is None
    assert "signed off by the analyst" not in written.model_dump_json()


# ── the pane raised a gate; these are the key to it ──────────────────────────────────────────
#
# An unacknowledged drop is the only structural blocker a plan has, and the card pre-flighted it
# honestly -- "Save as draft, 1 drop needs acknowledging" -- while offering nothing that could
# acknowledge one. The only remedy was `kedge plan acknowledge` at a terminal, so a user reviewing
# a plan in the pane was stuck in it. These routes decide a drop *within* the pending proposal:
# the revised plan replaces the one held in memory and nothing reaches the store until approve.


def _drop_route(session_id: str, tail: str, index: int = 0) -> str:
    return f"/api/sessions/{session_id}/pending/proposals/{index}/drops/{tail}"


def _pending_proposal(
    client: TestClient,
    registries: dict[str, ToolRegistry],
    *,
    plan: ProcessPlan | None = None,
    **context: object,
) -> tuple[str, ToolRegistry]:
    """A session holding one unapproved proposal, with one outstanding drop unless told otherwise."""
    session_id = _new_session(client)
    registry = _tool_registry(**context)
    registry.pending_proposals.append(PendingProposal(plan=plan if plan else _proposed_plan()))
    registries[session_id] = registry
    return session_id, registry


def _multi_drop_plan() -> ProcessPlan:
    """A proposal with three outstanding drops, for the decisions that only bite in a run."""
    return _proposed_plan(
        draft=make_draft(
            dropped=[
                DroppedRange(range="Calc!AK:AP", reason="no downstream refs"),
                DroppedRange(range="Calc!BB:BD", reason="scratch working, never read"),
                DroppedRange(range="Data!Z1:Z9", reason="a stale import, blank since 2024"),
            ]
        )
    )


def test_acknowledging_the_drop_lets_the_next_click_approve_the_plan(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]], tmp_path: Path
) -> None:
    """The whole point. Without this the pane could raise the blocker and never clear it."""
    client, registries = pending_client
    store = PlanStore(tmp_path / "plans")
    session_id, _registry = _pending_proposal(client, registries, plans=store)

    acknowledged = client.post(_drop_route(session_id, "0/acknowledge"), json={"note": "checked"})
    approved = client.post(f"/api/sessions/{session_id}/pending/proposals/0", json={})

    assert acknowledged.status_code == 200
    assert acknowledged.json()["approval_blockers"] == []
    payload = approved.json()
    assert payload["approved"] is True
    written = store.load(payload["version"])
    assert written.approval.approved is True
    assert written.dropped[0].acknowledged is True
    assert written.dropped[0].note == "checked"
    assert written.generated_by == "llm", "and the drop decision did not rewrite the authorship"


def test_a_drop_decision_is_recorded_on_the_proposal_and_written_nowhere(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]], tmp_path: Path
) -> None:
    """Approving is still the only route that writes; a drop decided and then discarded is gone."""
    client, registries = pending_client
    store = PlanStore(tmp_path / "plans")
    session_id, registry = _pending_proposal(client, registries, plans=store)

    response = client.post(_drop_route(session_id, "0/acknowledge"), json={"note": "agreed"})

    assert store.versions() == [], "nothing is written until the plan is approved"
    assert len(registry.pending_proposals) == 1, "deciding a drop does not decide the proposal"
    held = registry.pending_proposals[0].plan
    assert held.dropped[0].acknowledged is True
    assert held.dropped[0].accepted is True
    assert held.unacknowledged_drops == []
    # The refreshed card comes back with the decision on it, so the pane re-renders showing the
    # outcome rather than asking again.
    drop = response.json()["pending"]["proposals"][0]["dropped"][0]
    assert drop["acknowledged"] is True
    assert drop["note"] == "agreed"


def test_refusing_a_drop_keeps_the_range_and_says_approval_is_still_blocked(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]], tmp_path: Path
) -> None:
    """Refusing is not the mirror image of confirming, and the pane must not draw it as one.

    `acknowledge_drop` raises the open question of which stage consumes the kept range, and
    `approval_blockers` replaces "not acknowledged" with "no stage lists it as a source". Adding
    that stage is a plan edit the pane does not do, so the plan lands as a draft -- which is the
    gate working, and is what the button says it will do.
    """
    client, registries = pending_client
    store = PlanStore(tmp_path / "plans")
    session_id, registry = _pending_proposal(client, registries, plans=store)

    refused = client.post(_drop_route(session_id, "0/refuse"), json={"note": "still read by Risk"})
    approved = client.post(f"/api/sessions/{session_id}/pending/proposals/0", json={})

    assert refused.status_code == 200
    assert refused.json()["accepted"] is False
    blockers = refused.json()["approval_blockers"]
    assert any("Calc!AK:AP" in blocker and "no stage lists it" in blocker for blocker in blockers)
    payload = approved.json()
    assert payload["approved"] is False
    assert any("Calc!AK:AP" in blocker for blocker in payload["blockers"])
    written = store.load(payload["version"])
    assert written.dropped[0].accepted is False
    assert written.dropped[0].note == "still read by Risk"
    assert any("Calc!AK:AP" in question.question for question in written.open_questions)
    assert registry.pending_proposals == []


def test_a_drop_is_acknowledged_by_a_click_that_carries_no_body(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]],
) -> None:
    """The request the pane actually sends: a header announcing JSON and nothing behind it."""
    client, registries = pending_client
    session_id, registry = _pending_proposal(client, registries)

    response = client.post(
        _drop_route(session_id, "0/acknowledge"), headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 200
    held = registry.pending_proposals[0].plan
    assert held.dropped[0].acknowledged is True
    assert held.dropped[0].note is None, "no body means no note, not a failed click"


def test_acknowledging_every_outstanding_drop_clears_them_in_one_request(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]],
) -> None:
    """The pane's equivalent of `kedge plan acknowledge --all`, with the same per-drop trail."""
    client, registries = pending_client
    session_id = _new_session(client)
    registry = _tool_registry()
    draft = make_draft(
        dropped=[
            DroppedRange(range="Calc!AK:AP", reason="no downstream refs"),
            DroppedRange(range="Calc!BB:BD", reason="scratch working, never read"),
        ]
    )
    registry.pending_proposals.append(PendingProposal(plan=_proposed_plan(draft=draft)))
    registries[session_id] = registry

    response = client.post(_drop_route(session_id, "acknowledge-all"), json={"note": "read them"})

    assert response.status_code == 200
    assert response.json()["acknowledged"] == ["Calc!AK:AP", "Calc!BB:BD"]
    assert response.json()["approval_blockers"] == []
    held = registry.pending_proposals[0].plan
    assert [drop.note for drop in held.dropped] == ["read them", "read them"]
    assert all(drop.acknowledged for drop in held.dropped)


def test_a_drop_route_404s_on_an_index_that_is_not_there(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]],
) -> None:
    """Both indices, and a session the agent never made a registry for."""
    client, registries = pending_client
    stranger = _new_session(client)
    session_id, _registry = _pending_proposal(client, registries)

    assert client.post(_drop_route(stranger, "0/acknowledge")).status_code == 404
    assert client.post(_drop_route(session_id, "0/acknowledge", index=3)).status_code == 404
    missing = client.post(_drop_route(session_id, "5/refuse"))
    assert missing.status_code == 404
    assert "position 5" in missing.json()["detail"]


# ── a proposal is not a stored version, and deciding a drop must not pretend it is ───────────
#
# `kedge.plan.review` is shaped for a plan that is on disk: `_revise` bumps the version, restamps
# `created_at` and sets `generated_by="human"`, which is right for `kedge plan acknowledge`
# because the model's row was written before the drop was signed off. A pending proposal has no
# row anywhere -- the card promises nothing is written until approval -- so re-stamping here left
# the store holding one version, authored "human", with nothing recording that a model wrote the
# decomposition, and `save_next` turned the bumped version into a v1 deriving from v2.


def test_a_drop_decided_in_the_pane_leaves_the_plans_provenance_alone(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]], tmp_path: Path
) -> None:
    """Who wrote the decomposition has to survive the reviewer signing off a drop.

    `cli._plan_author` shows the model id only for ``generated_by == "llm"``, so a plan that
    reached disk stamped "human" renders as a bare "human" in `kedge plan history` -- for the only
    version there is, with no earlier row naming the model. The drop decision is the reviewer's;
    the decomposition is not, and the file has to say both.
    """
    client, registries = pending_client
    store = PlanStore(tmp_path / "plans")
    session_id, registry = _pending_proposal(client, registries, plans=store)
    proposed = registry.pending_proposals[0].plan

    client.post(_drop_route(session_id, "0/acknowledge"), json={"note": "read it"})
    payload = client.post(f"/api/sessions/{session_id}/pending/proposals/0", json={}).json()

    written = store.load(payload["version"])
    assert written.generated_by == "llm"
    assert written.llm_model == "gpt-5.6-terra"
    assert written.created_at == proposed.created_at
    assert written.version == proposed.version
    assert written.based_on_version == proposed.based_on_version
    assert written.summary == proposed.summary
    # The decision, and nothing else, is what moved.
    assert written.dropped[0].acknowledged is True
    assert written.dropped[0].note == "read it"


@pytest.mark.parametrize("decisions", [1, 2, 3])
def test_a_run_of_drop_decisions_never_writes_a_version_derived_from_a_later_one(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]], tmp_path: Path, decisions: int
) -> None:
    """`save_next` renumbers a plan down to the next free version and records the number it
    *arrived* with as `based_on_version`. A revision that bumped the in-memory version therefore
    came back inverted -- v1 claiming it derives from v2 -- and each further decision widened it.
    """
    client, registries = pending_client
    store = PlanStore(tmp_path / "plans")
    session_id, _registry = _pending_proposal(
        client, registries, plan=_multi_drop_plan(), plans=store
    )

    for position in range(decisions):
        assert client.post(_drop_route(session_id, f"{position}/acknowledge")).status_code == 200
    payload = client.post(f"/api/sessions/{session_id}/pending/proposals/0", json={}).json()

    written = store.load(payload["version"])
    assert written.version == 1
    assert written.based_on_version is None, "nothing on disk for it to derive from"
    assert payload["approved"] is (decisions == 3)


def test_a_decided_proposal_still_lands_at_the_next_free_version(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]], tmp_path: Path
) -> None:
    """Restoring the identity must not disarm the renumbering: v2 derives from v1, not the reverse."""
    client, registries = pending_client
    store = PlanStore(tmp_path / "plans")
    store.save(make_plan())
    session_id, _registry = _pending_proposal(client, registries, plans=store)

    client.post(_drop_route(session_id, "0/acknowledge"))
    payload = client.post(f"/api/sessions/{session_id}/pending/proposals/0", json={}).json()

    written = store.load(payload["version"])
    assert written.version == 2
    assert written.based_on_version == 1
    assert written.generated_by == "llm"


def test_clicking_the_same_drop_decision_twice_changes_nothing(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]],
) -> None:
    """A double click is a real event: the buttons are live for the length of the round trip.

    `acknowledge_drop` appends an open question on every refusal with nothing to stop it stacking
    a second identical one, and every pass through `_revise` moved the plan on again.
    """
    client, registries = pending_client
    session_id, registry = _pending_proposal(client, registries)
    same = {"note": "Risk still read it"}

    first = client.post(_drop_route(session_id, "0/refuse"), json=same)
    held = registry.pending_proposals[0].plan
    second = client.post(_drop_route(session_id, "0/refuse"), json=same)

    assert second.status_code == 200
    assert second.json()["approval_blockers"] == first.json()["approval_blockers"]
    again = registry.pending_proposals[0].plan
    assert again is held, "a decision the plan already carries revises nothing"
    assert again.version == held.version, "so there is no version to drift"
    assert sum("must be kept" in item.question for item in again.open_questions) == 1
    assert again.dropped[0].note == "Risk still read it"


def test_re_deciding_a_drop_with_a_corrected_note_records_the_correction(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]],
) -> None:
    """The note is part of the decision, not a label on it.

    Keyed on the verdict alone, the no-op guard answered 200 to a re-post carrying a corrected
    reason, reported the decision back, and kept the old words. The pane cannot send this, but an
    API that reports a change it did not make is an API that lies.
    """
    client, registries = pending_client
    session_id, registry = _pending_proposal(client, registries)

    client.post(_drop_route(session_id, "0/acknowledge"), json={"note": "dead since 2023"})
    client.post(_drop_route(session_id, "0/acknowledge"), json={"note": "dead since 2022, in fact"})

    assert registry.pending_proposals[0].plan.dropped[0].note == "dead since 2022, in fact"


def test_acknowledging_every_drop_leaves_a_range_the_user_refused_kept(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]],
) -> None:
    """ "Outstanding" is the operative word: a convenience must not overturn a deliberate refusal."""
    client, registries = pending_client
    session_id, registry = _pending_proposal(client, registries, plan=_multi_drop_plan())

    client.post(_drop_route(session_id, "0/refuse"), json={"note": "Risk still read it"})
    response = client.post(_drop_route(session_id, "acknowledge-all"), json={"note": "read them"})

    assert response.json()["acknowledged"] == ["Calc!BB:BD", "Data!Z1:Z9"]
    held = registry.pending_proposals[0].plan
    assert [drop.accepted for drop in held.dropped] == [False, True, True]
    assert held.dropped[0].note == "Risk still read it", "the refusal keeps its own reason"
    assert any("must be kept" in item.question for item in held.open_questions)
    assert any("Calc!AK:AP" in blocker for blocker in response.json()["approval_blockers"])


def test_overturning_a_refusal_takes_its_open_question_with_it(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]],
) -> None:
    """A confirmed drop next to "which stage consumes the range we must keep?" says both at once.

    Which also matters because the card offers the way back at all: without it a mis-click on
    "Keep the range" has no exit but approving a draft or discarding the whole proposal, and a
    gate with no key is the complaint this feature exists to answer.
    """
    client, registries = pending_client
    session_id, registry = _pending_proposal(client, registries)
    asked = len(registry.pending_proposals[0].plan.open_questions)

    client.post(_drop_route(session_id, "0/refuse"), json={"note": "mis-click"})
    response = client.post(_drop_route(session_id, "0/acknowledge"), json={"note": "meant it"})

    assert response.json()["approval_blockers"] == []
    held = registry.pending_proposals[0].plan
    assert held.dropped[0].accepted is True
    assert not any("must be kept" in item.question for item in held.open_questions)
    assert len(held.open_questions) == asked, "and the model's own questions are left alone"


def test_the_way_back_from_a_refusal_leaves_a_question_the_model_wrote(
    pending_client: tuple[TestClient, dict[str, ToolRegistry]],
) -> None:
    """The withdrawal matches by identity, and this is the case a prefix rule destroyed.

    A model that opens a legitimate question by naming the range it proposed dropping is doing
    exactly what it should. Refuse then confirm from the card and it went, silently, out of the
    plan that reached disk -- unannounced data loss on the surface whose whole point is that
    nothing is decided quietly. `kedge.plan.review.acknowledge_drop` now removes only the exact
    sentence it composed itself.
    """
    client, registries = pending_client
    theirs = (
        "Calc!AK:AP was proposed for dropping, but the 2023 archive still references it. Should "
        "the archive be re-pointed before it goes?"
    )
    plan = _proposed_plan(draft=make_draft(open_questions=[theirs]))
    session_id, registry = _pending_proposal(client, registries, plan=plan)

    client.post(_drop_route(session_id, "0/refuse"), json={"note": "mis-click"})
    client.post(_drop_route(session_id, "0/acknowledge"), json={"note": "meant it"})

    held = registry.pending_proposals[0].plan
    assert [question.question for question in held.open_questions] == [theirs]
    assert held.approval_blockers() == []
