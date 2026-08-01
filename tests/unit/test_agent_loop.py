"""The agent loop, driven by a scripted model client rather than by a live endpoint.

Everything here runs with no key, no network and no marimo. That is the point: PLAN M4's loop is
the part of kedge most easily left untested "because it needs a model", and a loop nobody tests is
a loop that silently emits two ``DoneEvent``s on the error path and corrupts a session transcript.

The choreography is asserted against the same shape ``ScriptedAgent`` establishes, because that
fake is what the UI was built and judged against, and a real loop that streams a different shape
would be a regression the UI would show but nothing would catch.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from kedge.agent.context import TokenCounter
from kedge.agent.loop import ChatDelta, KedgeAgent
from kedge.agent.tools import ToolContext
from kedge.errors import KedgeError
from kedge.notebook.model import CellInfo, CellRef, GraphNode, GraphView, MutationResult
from kedge.server.agent_seam import AgentLoop, CancelToken, TurnMessage, TurnRequest

CLEAN_CELL = "apply_haircuts = load_handin.join(reference_haircuts, on='asset_class', how='left')\n"
PANDAS_CELL = "import pandas as pd\nframe = pd.read_excel('handin.xlsx')\n"


# ── fakes ────────────────────────────────────────────────────────────────────────────────────


class ScriptedClient:
    """A model that says exactly what the test tells it to, one list of deltas per round trip."""

    def __init__(self, rounds: list[list[ChatDelta]]) -> None:
        self._rounds = list(rounds)
        self.seen: list[list[dict[str, Any]]] = []
        self.tools_offered: list[str] = []

    async def stream(self, *, model: str, messages: Any, tools: Any) -> Any:
        del model
        self.seen.append([dict(message) for message in messages])
        self.tools_offered = [tool["function"]["name"] for tool in tools]
        for delta in self._rounds.pop(0) if self._rounds else []:
            yield delta


class RaisingClient:
    """A model endpoint that fails the way a real one does: mid-stream."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def stream(self, **_kwargs: Any) -> Any:
        yield ChatDelta(text="Let me look at ")
        raise self._error


class CancellingClient:
    """A model that keeps talking while the user presses stop."""

    def __init__(self, cancel: CancelToken) -> None:
        self._cancel = cancel

    async def stream(self, **_kwargs: Any) -> Any:
        yield ChatDelta(text="Working ")
        self._cancel.cancel()
        yield ChatDelta(text="on it ")
        yield ChatDelta(text="still ")


class ThinkingClient:
    """A reasoning model: a long silence on the wire before anything the loop can translate.

    This is the shape that made Stop inert, and it is the ordinary case rather than a contrived
    one. ``responses_delta`` maps every reasoning event to ``None``, so for the whole of a model's
    thinking nothing reaches the loop — and a cancellation check written inside ``async for delta``
    does not run once during exactly the stretch a user reaches for Stop.
    """

    def __init__(self) -> None:
        self.finished = False

    async def stream(self, **_kwargs: Any) -> Any:
        await asyncio.sleep(30)  # thinking, none of which becomes a delta
        self.finished = True  # pragma: no cover - the turn is stopped long before this
        yield ChatDelta(text="...")  # pragma: no cover


class FakeDriver:
    def __init__(self) -> None:
        self.graphs_read = 0
        self.created: list[str] = []

    async def read_graph(self) -> GraphView:
        self.graphs_read += 1
        return GraphView(
            nodes=(
                GraphNode(id="AAaa", name="imports", defs=("pl",)),
                GraphNode(id="MJUe", name="load_handin", defs=("load_handin",), refs=("pl",)),
            )
        )

    async def list_cells(self, *, with_code: bool = True) -> tuple[CellInfo, ...]:
        return (CellInfo(id="AAaa", name="imports", code="import polars as pl"),)

    async def create_cell(self, code: str, *, name: str, **_kw: Any) -> MutationResult:
        self.created.append(name)
        return MutationResult(
            operation="create_cell", cell=CellRef(id="U1", name=name), ran=True, status="idle"
        )


def call(name: str, arguments: dict[str, Any], *, index: int = 0) -> list[ChatDelta]:
    """The deltas a model emits for one tool call, split the way a real stream splits them."""
    encoded = json.dumps(arguments)
    midpoint = len(encoded) // 2
    return [
        ChatDelta(index=index, call_id=f"call_{index}", name=name),
        ChatDelta(index=index, arguments=encoded[:midpoint]),
        ChatDelta(index=index, arguments=encoded[midpoint:]),
    ]


def build(client: Any, *, driver: Any = None, **kwargs: Any) -> KedgeAgent:
    return KedgeAgent(
        client=client,
        context=ToolContext(driver=driver),
        counter=TokenCounter(allow_download=False),
        system_prompt="SYSTEM PROMPT",
        **kwargs,
    )


async def drive(
    agent: KedgeAgent, message: str = "convert the haircut lookup", **kwargs: Any
) -> list[Any]:
    request = TurnRequest(turn_id="t1", session_id="s1", message=message, **kwargs)
    return [event async for event in agent.run(request, cancel=CancelToken())]


# ── the Protocol ─────────────────────────────────────────────────────────────────────────────


def test_the_loop_satisfies_the_server_s_protocol() -> None:
    assert isinstance(build(ScriptedClient([])), AgentLoop)


async def test_the_tools_offered_are_the_ones_plan_m4_lists() -> None:
    client = ScriptedClient([[ChatDelta(text="Nothing to do.")]])
    await drive(build(client))
    assert "propose_cell" in client.tools_offered
    assert "reconcile" in client.tools_offered
    assert len(client.tools_offered) == 15


# ── exactly one DoneEvent ────────────────────────────────────────────────────────────────────


async def test_exactly_one_done_event_on_the_success_path() -> None:
    events = await drive(build(ScriptedClient([[ChatDelta(text="Here is what I found.")]])))
    assert [event.type for event in events].count("done") == 1
    assert events[-1].type == "done"
    assert events[-1].turn_id == "t1"


async def test_exactly_one_done_event_on_the_error_path() -> None:
    events = await drive(build(RaisingClient(EndpointRefusedError())))
    types = [event.type for event in events]
    assert types.count("done") == 1
    assert types[-1] == "done"
    errors = [event for event in events if event.type == "error"]
    assert len(errors) == 1
    assert errors[0].recoverable is True


async def test_an_unexpected_exception_is_reported_as_unrecoverable() -> None:
    events = await drive(build(RaisingClient(ValueError("the SDK changed shape"))))
    errors = [event for event in events if event.type == "error"]
    assert len(errors) == 1
    assert errors[0].recoverable is False
    assert "the SDK changed shape" in errors[0].message
    assert [event.type for event in events].count("done") == 1


async def test_exactly_one_done_event_on_the_cancellation_path() -> None:
    token = CancelToken()
    agent = build(CancellingClient(token))
    request = TurnRequest(turn_id="t1", session_id="s1", message="go")
    events = [event async for event in agent.run(request, cancel=token)]
    types = [event.type for event in events]
    assert types.count("done") == 1
    assert types[-1] == "done"
    cancelled = [event for event in events if event.type == "error"]
    assert cancelled[0].message == "Turn cancelled at your request."
    assert cancelled[0].recoverable is True


async def test_a_pre_cancelled_turn_still_finishes_cleanly() -> None:
    token = CancelToken()
    token.cancel()
    agent = build(ScriptedClient([[ChatDelta(text="never seen")]]))
    request = TurnRequest(turn_id="t1", session_id="s1", message="go")
    events = [event async for event in agent.run(request, cancel=token)]
    assert [event.type for event in events].count("done") == 1
    assert not any(event.type == "token" for event in events)


async def test_stop_lands_while_the_model_is_still_thinking() -> None:
    """Cancellation must not wait for the model to say something first.

    The check used to sit inside ``async for delta``, so a reasoning model that spends a minute
    thinking before its first token left Stop doing nothing for that whole minute — and the chat
    went on saying "Thinking" while the user pressed it again. The wait here is 30 seconds of model
    call against a 5 second deadline: it only passes if the turn abandons the call rather than
    outliving it.
    """
    token = CancelToken()
    client = ThinkingClient()
    agent = build(client)
    request = TurnRequest(turn_id="t1", session_id="s1", message="go")

    async def collect() -> list[Any]:
        return [event async for event in agent.run(request, cancel=token)]

    async def press_stop() -> None:
        await asyncio.sleep(0.05)
        token.cancel()

    stopper = asyncio.create_task(press_stop())
    events = await asyncio.wait_for(collect(), timeout=5)
    await stopper

    types = [event.type for event in events]
    assert types.count("done") == 1
    assert types[-1] == "done"
    stopped = next(event for event in events if event.type == "error")
    assert stopped.message == "Turn cancelled at your request."
    assert client.finished is False, "the model call was abandoned, not waited out"


async def test_a_task_cancellation_that_is_not_the_user_s_is_re_raised() -> None:
    class Torn:
        async def stream(self, **_kwargs: Any) -> Any:
            raise asyncio.CancelledError
            yield  # pragma: no cover

    agent = build(Torn())
    request = TurnRequest(turn_id="t1", session_id="s1", message="go")
    with pytest.raises(asyncio.CancelledError):
        [event async for event in agent.run(request, cancel=CancelToken())]


# ── the choreography ─────────────────────────────────────────────────────────────────────────


async def test_a_realistic_turn_streams_the_same_shape_as_the_scripted_agent() -> None:
    driver = FakeDriver()
    client = ScriptedClient(
        [
            [
                ChatDelta(text="The lookup is an exact VLOOKUP, "),
                ChatDelta(text="which is a left join.\n\n"),
                *call("propose_cell", {"name": "apply_haircuts", "code": CLEAN_CELL}),
            ],
            [ChatDelta(text="Reconciliation is clean.")],
        ]
    )
    events = await drive(build(client, driver=driver))
    types = [event.type for event in events]

    assert types[0] == "status"
    assert "token" in types
    assert types.index("tool_call") < types.index("tool_result")
    assert "validation" in types
    assert "cell_created" in types
    assert "cell_running" in types
    assert "cell_result" in types
    assert types[-1] == "done"

    created = next(event for event in events if event.type == "cell_created")
    assert created.name == "apply_haircuts"
    assert created.preview.startswith("apply_haircuts =")
    assert driver.created == ["apply_haircuts"]


async def test_the_tool_call_event_never_carries_the_cell_body() -> None:
    driver = FakeDriver()
    body = "apply_haircuts = 1  # " + "x" * 500 + "\n"
    client = ScriptedClient(
        [[*call("propose_cell", {"name": "apply_haircuts", "code": body})], [ChatDelta(text="ok")]]
    )
    events = await drive(build(client, driver=driver))
    tool_call = next(event for event in events if event.type == "tool_call")
    assert "xxxx" not in tool_call.args_summary
    assert "chars>" in tool_call.args_summary


async def test_two_tool_calls_in_one_reply_are_both_dispatched() -> None:
    driver = FakeDriver()
    client = ScriptedClient(
        [
            [
                *call("list_cells", {}, index=0),
                *call("probe", {"code": "1 + 1"}, index=1),
            ],
            [ChatDelta(text="done")],
        ]
    )
    events = await drive(build(client, driver=driver))
    names = [event.name for event in events if event.type == "tool_call"]
    assert names == ["list_cells", "probe"]


async def test_the_status_phase_matches_what_the_tool_is_doing() -> None:
    driver = FakeDriver()
    client = ScriptedClient(
        [[*call("propose_cell", {"name": "c", "code": "c = 1\n"})], [ChatDelta(text="ok")]]
    )
    events = await drive(build(client, driver=driver))
    phases = [event.phase for event in events if event.type == "status"]
    assert phases[0] == "analysing"
    assert "editing" in phases


# ── the validation gate in the loop ──────────────────────────────────────────────────────────


async def test_a_rejected_cell_comes_back_to_the_model_as_violations() -> None:
    driver = FakeDriver()
    client = ScriptedClient(
        [
            [*call("propose_cell", {"name": "loader", "code": PANDAS_CELL})],
            [ChatDelta(text="Rewritten with polars.")],
        ]
    )
    events = await drive(build(client, driver=driver))
    validation = next(event for event in events if event.type == "validation")
    assert validation.ok is False
    assert any("polars, never pandas" in violation for violation in validation.violations)
    assert driver.created == []

    # The violations were handed back, so the model's next prompt contains them verbatim.
    second_prompt = json.dumps(client.seen[1])
    assert "polars, never pandas" in second_prompt


async def test_validation_retries_are_capped_at_three() -> None:
    driver = FakeDriver()
    rounds = [[*call("propose_cell", {"name": "loader", "code": PANDAS_CELL})] for _ in range(5)]
    events = await drive(build(client := ScriptedClient(rounds), driver=driver))
    types = [event.type for event in events]

    assert types.count("validation") == 3
    assert types.count("tool_call") == 3
    errors = [event for event in events if event.type == "error"]
    assert len(errors) == 1
    assert "3 times running" in errors[0].message
    assert types.count("done") == 1
    assert len(client.seen) == 3  # it stopped rather than spending the rest of the budget


async def test_the_step_budget_stops_a_loop_that_never_answers() -> None:
    driver = FakeDriver()
    rounds = [[*call("list_cells", {}, index=0)] for _ in range(10)]
    events = await drive(build(ScriptedClient(rounds), driver=driver, max_steps=3))
    assert [event.type for event in events].count("tool_call") == 3
    errors = [event for event in events if event.type == "error"]
    assert "all 3 steps" in errors[0].message
    assert [event.type for event in events].count("done") == 1


# ── context ──────────────────────────────────────────────────────────────────────────────────


async def test_notebook_state_is_rebuilt_from_the_kernel_every_turn() -> None:
    driver = FakeDriver()
    agent = build(ScriptedClient([[ChatDelta(text="ok")], [ChatDelta(text="ok")]]), driver=driver)
    await drive(agent)
    assert driver.graphs_read == 1
    await drive(agent)
    assert driver.graphs_read == 2


async def test_the_name_registry_and_notebook_state_are_pinned_into_every_turn() -> None:
    client = ScriptedClient([[ChatDelta(text="ok")]])
    await drive(build(client, driver=FakeDriver()))
    system = client.seen[0][0]
    assert system["role"] == "system"
    assert "SYSTEM PROMPT" in system["content"]
    assert "one owning cell per public name" in system["content"]
    assert "load_handin (MJUe)" in system["content"]
    assert "Cell bodies are not shown" in system["content"]


async def test_history_is_replayed_but_the_notebook_is_not_taken_from_it() -> None:
    driver = FakeDriver()
    client = ScriptedClient([[ChatDelta(text="ok")]])
    await drive(
        build(client, driver=driver),
        history=(
            TurnMessage(role="user", content="what does Calc do?"),
            TurnMessage(role="assistant", content="It applies a haircut."),
        ),
    )
    prompt = client.seen[0]
    assert {"role": "user", "content": "what does Calc do?"} in prompt
    # The notebook facts came from the kernel, not from what the conversation remembered.
    assert driver.graphs_read == 1


async def test_a_workspace_without_a_kernel_still_answers() -> None:
    client = ScriptedClient(
        [[*call("list_cells", {})], [ChatDelta(text="I cannot see the notebook.")]]
    )
    events = await drive(build(client))
    result = next(event for event in events if event.type == "tool_result")
    assert result.ok is False
    assert [event.type for event in events].count("done") == 1


async def test_each_session_gets_its_own_tool_registry() -> None:
    agent = build(ScriptedClient([[ChatDelta(text="a")], [ChatDelta(text="b")]]))
    await drive(agent)
    request = TurnRequest(turn_id="t2", session_id="s2", message="hello")
    [event async for event in agent.run(request, cancel=CancelToken())]
    assert set(agent.registries) == {"s1", "s2"}
    agent.reset_session("s1")
    assert set(agent.registries) == {"s2"}


async def test_tokens_used_is_reported_on_the_done_event() -> None:
    events = await drive(build(ScriptedClient([[ChatDelta(text="a fairly long answer " * 20)]])))
    done = events[-1]
    assert done.type == "done"
    assert done.tokens_used > 0


# ── the server, driven by this loop rather than by the scripted stand-in ──────────────────────


def test_the_server_streams_this_loop_where_it_streamed_the_scripted_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The definition-of-done claim, exercised rather than asserted in prose.

    ``create_app`` is handed a :class:`KedgeAgent` in exactly the place ``create_demo_app`` hands
    it a ``ScriptedAgent``, and the SSE stream that comes back is parsed frame by frame. If the
    loop ever drifts from the seam's Protocol this is the test that notices.
    """
    from fastapi.testclient import TestClient
    from openpyxl import Workbook as OpenpyxlWorkbook

    from kedge.server.app import create_app
    from kedge.server.sessions import SessionStore
    from kedge.workspace import Workspace

    monkeypatch.setenv("KEDGE_HOME", str(tmp_path / "home"))
    workbook = tmp_path / "process.xlsx"
    book = OpenpyxlWorkbook()
    sheet = book.active
    sheet.title = "Calc"
    sheet.append(["haircut"])
    sheet.append([0.02])
    book.save(workbook)

    workspace = Workspace.for_workbook(workbook)
    workspace.ensure_dirs()
    client = ScriptedClient(
        [
            [
                ChatDelta(text="The haircut column is a flat rate.\n"),
                *call("read_range", {"sheet": "Calc", "range": "A1:A2"}),
            ],
            [ChatDelta(text="Two rows, header on row 1.")],
        ]
    )
    agent = KedgeAgent(
        client=client,
        context=ToolContext.for_workspace(workspace),
        counter=TokenCounter(allow_download=False),
    )
    app = create_app(workspace, agent=agent, store=SessionStore(tmp_path / "sessions.sqlite"))

    with TestClient(app) as http:
        session = http.post("/api/sessions", json={"title": "smoke"}).json()["session"]["id"]
        with http.stream(
            "POST", f"/api/sessions/{session}/turns", json={"message": "describe Calc"}
        ) as response:
            assert response.status_code == 200
            body = "".join(response.iter_text())

    frames = [line[len("event: ") :] for line in body.splitlines() if line.startswith("event: ")]
    assert frames[-1] == "done"
    assert frames.count("done") == 1
    assert "tool_call" in frames and "tool_result" in frames

    # The value reached the model, and only the model: the SSE trail carries a summary, never
    # the payload, so a browser tab and the session transcript never hold workbook data.
    assert "0.02" in json.dumps(client.seen[1])
    assert "0.02" not in body

    # And the audit line for that payload landed under the machine directory, values excluded.
    logs = sorted((workspace.logs_dir).glob("outbound-*.jsonl"))
    assert len(logs) == 1
    recorded = logs[0].read_text(encoding="utf-8")
    assert '"tool":"read_range"' in recorded
    assert "0.02" not in recorded


class EndpointRefusedError(KedgeError):
    """A kedge-shaped failure from the model endpoint, for the recoverable-error path."""

    def __init__(self) -> None:
        super().__init__("the model endpoint refused the request: no API key is configured")
