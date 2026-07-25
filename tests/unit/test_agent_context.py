"""Context assembly: the name registry, the live notebook block, and the eviction order.

The eviction tests assert the *order* PLAN M4 fixes, not merely that something was dropped. That
order is the whole design — a window that made room by dropping the name registry would still fit
the budget and would then produce a turn that collides on every name it writes.
"""

from __future__ import annotations

from typing import Any

import pytest

from kedge.agent.context import (
    MAX_REGISTRY_NAMES,
    CellFacts,
    ContextMessage,
    ConversationWindow,
    NameRegistry,
    NotebookState,
    TokenCounter,
    build_analysis_block,
    build_plan_block,
    summarise_messages,
)
from kedge.analysis.model import WorkbookAnalysis
from kedge.notebook.model import CellInfo, GraphNode, GraphView
from kedge.plan.model import ProcessPlan


@pytest.fixture
def graph() -> GraphView:
    return GraphView(
        nodes=(
            GraphNode(id="AAaa", name="imports", defs=("mo", "pl")),
            GraphNode(id="MJUe", name="load_handin", defs=("load_handin",), refs=("pl", "handin")),
            GraphNode(
                id="Kp2r",
                name="reconcile_panel",
                defs=("panel",),
                refs=("load_handin",),
                status="idle",
            ),
        )
    )


# ── the name registry ────────────────────────────────────────────────────────────────────────


def test_the_registry_maps_every_public_name_to_its_owning_cell(graph: GraphView) -> None:
    registry = NameRegistry.from_graph(graph)
    assert registry.names == ("load_handin", "mo", "panel", "pl")
    assert registry.owner_of("pl").label == "imports"
    assert registry.owner_of("load_handin").id == "MJUe"
    assert registry.owner_of("nothing") is None


def test_underscore_names_are_not_owned() -> None:
    registry = NameRegistry([CellFacts(id="A", name="one", defs=("_scratch", "public"))])
    assert registry.names == ("public",)
    assert registry.conflicts(["_scratch"]) == ()


def test_the_rendered_registry_states_the_rule_and_the_escape_hatch(graph: GraphView) -> None:
    rendered = NameRegistry.from_graph(graph).render()
    assert "one owning cell per public name" in rendered
    assert "underscore" in rendered
    assert "pl" in rendered and "imports" in rendered and "AAaa" in rendered


def test_an_empty_registry_says_so_rather_than_rendering_nothing() -> None:
    assert "defines nothing yet" in NameRegistry([]).render()


def test_a_very_wide_registry_is_capped_with_a_marker() -> None:
    cells = [CellFacts(id="A", name="wide", defs=tuple(f"name_{i:04d}" for i in range(500)))]
    rendered = NameRegistry(cells).render()
    assert "[… 100 more names omitted]" in rendered
    assert rendered.count("\n") <= MAX_REGISTRY_NAMES + 10


def test_conflicts_exclude_the_cell_being_edited(graph: GraphView) -> None:
    registry = NameRegistry.from_graph(graph)
    assert registry.conflicts(["load_handin"]) != ()
    assert registry.conflicts(["load_handin"], exclude="load_handin") == ()
    assert registry.conflicts(["load_handin"], exclude="MJUe") == ()


def test_would_cycle_names_the_cells_involved() -> None:
    registry = NameRegistry(
        [
            CellFacts(id="A", name="stage_one", defs=("totals",), refs=("adjusted",)),
            CellFacts(id="B", name="stage_two", defs=("adjusted",), refs=("raw",)),
        ]
    )
    assert registry.would_cycle(["raw"], ["totals"]) == ("stage_one",)
    assert registry.would_cycle(["unrelated"], ["nothing"]) == ()


def test_frame_names_is_a_heuristic_over_the_owned_names() -> None:
    registry = NameRegistry(
        [CellFacts(id="A", name="c", defs=("handin_df", "exposure_data", "rate", "raw_rows"))]
    )
    frames = registry.frame_names()
    assert {"handin_df", "exposure_data", "raw_rows"} <= frames
    assert "rate" not in frames


# ── live notebook state ──────────────────────────────────────────────────────────────────────


def test_notebook_state_is_built_from_the_graph_and_never_carries_code(graph: GraphView) -> None:
    state = NotebookState.from_graph(graph)
    rendered = state.render()
    assert "Cell bodies are not shown" in rendered
    assert "load_handin (MJUe)" in rendered
    assert "reads: pl, handin" in rendered
    assert not hasattr(state.cells[0], "code")


def test_notebook_state_falls_back_to_a_cell_listing() -> None:
    state = NotebookState.from_cells(
        [CellInfo(id="A", name="imports", code="import polars as pl", status="idle")]
    )
    assert state.cells[0].label == "imports"
    assert state.cells[0].defs == ()


def test_notebook_state_warns_about_existing_breakage() -> None:
    state = NotebookState(
        cells=(CellFacts(id="A", name="one"),),
        cycles=(("A", "B"),),
        multiply_defined=("exposure",),
    )
    rendered = state.render()
    assert "multiply defined: exposure" in rendered
    assert "dependency cycle: A -> B" in rendered


def test_an_empty_notebook_says_so() -> None:
    assert "no cells yet" in NotebookState().render()


# ── the analysis and plan blocks ─────────────────────────────────────────────────────────────


def test_the_analysis_block_is_a_summary_not_a_dump(analysis: WorkbookAnalysis) -> None:
    block = build_analysis_block(analysis)
    assert "Workbook analysis — summary" in block
    assert "inspect_workbook" in block
    assert "Calc!H" in block or "Calc!" in block
    # The R1C1 body of an operation is detail, and detail comes through a tool.
    assert "VLOOKUP(RC[-3]" not in block


def test_the_analysis_block_says_so_when_there_is_no_analysis() -> None:
    assert "kedge inspect" in build_analysis_block(None)


def test_the_plan_block_marks_checkpoints_and_open_questions(plan: ProcessPlan) -> None:
    block = build_plan_block(plan)
    assert "standing instructions" in block
    assert "CHECKPOINT — not automated" in block
    assert "amend_plan" in block


def test_the_plan_block_says_so_when_there_is_no_plan() -> None:
    assert "No approved plan" in build_plan_block(None)


# ── the token counter ────────────────────────────────────────────────────────────────────────


def test_the_counter_degrades_to_a_heuristic_without_tiktoken() -> None:
    counter = TokenCounter(allow_download=False)
    assert not counter.exact
    assert counter.count("") == 0
    assert counter.count("a" * 400) == 100


def test_counting_a_message_includes_its_framing() -> None:
    counter = TokenCounter(allow_download=False)
    assert counter.count_message({"role": "user", "content": "hello"}) > counter.count("hello")


# ── the window ───────────────────────────────────────────────────────────────────────────────


def _window(budget: int = 10_000) -> ConversationWindow:
    return ConversationWindow(
        system="SYSTEM", budget=budget, counter=TokenCounter(allow_download=False)
    )


def test_pinned_blocks_ride_in_the_system_message() -> None:
    window = _window()
    window.set_pinned(["## Name registry", "## Live notebook state"])
    window.begin_turn()
    window.add_user("convert the haircut lookup")
    messages = window.assemble()
    assert messages[0]["role"] == "system"
    assert "## Name registry" in messages[0]["content"]
    assert messages[-1] == {"role": "user", "content": "convert the haircut lookup"}


def test_tool_results_are_evicted_before_turns_are() -> None:
    window = ConversationWindow(
        system="SYSTEM", budget=400, counter=TokenCounter(allow_download=False)
    )
    window.begin_turn()
    window.add_user("first")
    window.add_assistant("", tool_calls=[{"id": "c1"}])
    window.add_tool_result(tool_call_id="c1", name="sample_data", content="x" * 8_000)
    window.begin_turn()
    window.add_user("second")

    report = window.fit()
    assert report.tool_results_evicted == 1
    assert report.tokens_after < report.tokens_before
    rendered = window.assemble()
    assert any("evicted to stay within the context budget" in m["content"] for m in rendered)
    # The user's own words survived; only the sampled data went.
    assert any(m.get("content") == "second" for m in rendered)


def test_the_pinned_blocks_are_never_evicted() -> None:
    window = ConversationWindow(
        system="SYSTEM", budget=10, counter=TokenCounter(allow_download=False)
    )
    window.set_pinned(["## Name registry\n" + "pl  imports (AAaa)\n" * 200])
    window.begin_turn()
    window.add_user("go")
    window.fit()
    head = window.assemble()[0]["content"]
    assert "## Name registry" in head
    assert "pl  imports (AAaa)" in head


def test_whole_turns_are_dropped_never_half_of_one() -> None:
    window = ConversationWindow(
        system="SYSTEM", budget=200, counter=TokenCounter(allow_download=False)
    )
    for index in range(4):
        window.begin_turn()
        window.add_user(f"question {index} " + "padding " * 60)
        window.add_assistant(f"answer {index} " + "padding " * 60)
    window.fit()
    turns = {message.turn for message in window.messages}
    # Whatever survived, no turn is present in half: an assistant message carrying tool_calls
    # without its results is rejected outright by the endpoint.
    for turn in turns:
        roles = [m.role for m in window.messages if m.turn == turn]
        assert roles in (["user", "assistant"], ["user"], ["assistant"])
    assert window.digest


def test_old_tool_results_age_out_without_budget_pressure() -> None:
    window = ConversationWindow(
        system="SYSTEM",
        budget=1_000_000,
        counter=TokenCounter(allow_download=False),
        evict_tool_results_after_turns=2,
    )
    window.begin_turn()
    window.add_tool_result(tool_call_id="c1", name="sample_data", content="rows")
    for _ in range(4):
        window.begin_turn()
        window.add_user("carry on")
    rendered = window.assemble()
    assert any("evicted to stay within the context budget" in m["content"] for m in rendered)


def test_compaction_keeps_the_current_turn_and_digests_the_rest() -> None:
    window = _window()
    window.begin_turn()
    window.add_user("convert the haircut lookup")
    window.add_assistant("Should the overrides be automated?")
    window.begin_turn()
    window.add_user("no, leave them")
    digest = window.compact()
    assert "convert the haircut lookup" in digest
    assert "Should the overrides be automated?" in digest
    assert [m.content for m in window.messages] == ["no, leave them"]


def test_reset_forgets_everything_including_the_digest() -> None:
    window = _window()
    window.begin_turn()
    window.add_user("something")
    window.compact()
    window.reset()
    assert window.messages == ()
    assert window.digest == ""
    assert window.turn == 0


def test_history_is_replayed_one_turn_per_exchange() -> None:
    window = _window()
    window.load_history([("user", "first"), ("assistant", "reply"), ("user", "second")])
    assert window.turn == 2
    assert [m.role for m in window.messages] == ["user", "assistant", "user"]


def test_the_digest_records_decisions_tools_and_open_questions() -> None:
    messages = [
        ContextMessage(role="user", content="convert Calc!H", turn=3, kind="user"),
        ContextMessage(
            role="tool", content="rows", turn=3, kind="tool_result", tool_name="sample_data"
        ),
        ContextMessage(
            role="tool", content="rows", turn=3, kind="tool_result", tool_name="sample_data"
        ),
        ContextMessage(role="assistant", content="Is column AF dead?", turn=3, kind="assistant"),
    ]
    digest = summarise_messages(messages)
    assert "turn 3" in digest
    assert "convert Calc!H" in digest
    assert "sample_datax2" in digest
    assert "Is column AF dead?" in digest


def test_an_empty_run_summarises_to_nothing() -> None:
    assert summarise_messages([]) == ""


def test_tool_messages_render_in_the_chat_completions_shape() -> None:
    message = ContextMessage(
        role="tool", content="6 cells", turn=1, kind="tool_result", tool_call_id="c1"
    )
    rendered: dict[str, Any] = message.to_openai()
    assert rendered == {"role": "tool", "tool_call_id": "c1", "content": "6 cells"}
