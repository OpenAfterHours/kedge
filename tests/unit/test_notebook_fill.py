"""Tests for the conversion driver: ``kedge.notebook.fill``.

Every completer here is a fake. Nothing in this module reaches a model endpoint, spawns a marimo
process or opens a kernel -- which is the point of the seam as much as it is the point of the
tests: ``kedge convert`` writes through the file bridge, so a conversion is exercisable end to
end with a dictionary of canned replies.

What is asserted, in order: that every hole is asked about once and in the scaffolder's order;
that an accepted body is spliced under the header with the marker gone; that a rejection comes
back to the model and a second answer is taken; and that the six outcomes stay six. That last one
is the part most worth protecting. A transport failure is not a model failing to write code, and
a report that renders the two the same sends whoever reads it to fix the wrong thing.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from conftest import approved_plan_store, make_analysis, make_approved_plan, make_plan
from kedge import cli
from kedge.agent.prompts import SYSTEM_PARTS, load_prompt
from kedge.cli import app
from kedge.notebook.filedriver import FileNotebookDriver
from kedge.notebook.fill import (
    FILL_PROMPT_PARTS,
    FillOutcome,
    FillReport,
    convert_notebook,
    fill_holes,
    policy_rules,
    system_prompt,
)
from kedge.notebook.scaffold import (
    TODO_MARKER,
    PlanNotApprovedError,
    ScaffoldCell,
    build_cells,
    holes_in,
    is_unwritten,
)
from kedge.plan.store import PlanStore

if TYPE_CHECKING:
    from pathlib import Path

    from kedge.plan.propose import CompletionRequest


# ── fakes ────────────────────────────────────────────────────────────────────────────────────


class Scripted:
    """Answers with the next canned reply, repeating the last one once they run out.

    Repeating rather than raising: several tests want the same rejected body on every attempt,
    and an exhausted script raising would arrive as :attr:`FillOutcome.ERROR` -- a completely
    different outcome from the one being asserted.
    """

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies) or [""]
        self.requests: list[CompletionRequest] = []

    def complete(self, request: CompletionRequest) -> str:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.replies) - 1)
        return self.replies[index]

    @property
    def cells_asked_about(self) -> list[str]:
        """The cell each request was about, read off the user message the driver sends."""
        return [request.messages[1]["content"].split("`")[1] for request in self.requests]


class Obliging:
    """Answers every cell with the smallest body that defines the name the scaffold promised."""

    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []

    def complete(self, request: CompletionRequest) -> str:
        self.requests.append(request)
        name = request.messages[1]["content"].split("`")[1]
        return f"{name} = 1"


class Broken:
    """Raises the way a dead endpoint does: once per request, before anything is generated."""

    def __init__(self, message: str = "connection refused") -> None:
        self.calls = 0
        self.message = message

    def complete(self, request: CompletionRequest) -> str:
        del request
        self.calls += 1
        msg = self.message
        raise ConnectionError(msg)


PANDAS = "import pandas as pd\n\nvalue = pd.DataFrame()"
"""A body the shipped gate refuses at the style stage. Non-negotiable 1, machine enforced."""


def one_hole(name: str = "alpha") -> list[ScaffoldCell]:
    """A single hand-made hole, for the tests that are about the loop rather than the scaffold."""
    return [
        ScaffoldCell(
            name=name,
            code=(
                f"# Stage 1 of 1: {name}\n"
                f"# Intent: something the workbook did\n"
                f"#\n"
                f"# {TODO_MARKER}: translate this stage. LazyFrame throughout.\n"
                f"{name} = 0  # passthrough until translated"
            ),
            stage_id=name,
        )
    ]


def filled(report: FillReport, name: str) -> str:
    """The code the report ended up with for one cell."""
    return report.codes[report.names.index(name)]


# ── the loop ─────────────────────────────────────────────────────────────────────────────────


def test_every_hole_is_asked_about_once_in_the_scaffolders_own_order() -> None:
    """Order is the contract: a hole is filled against the names defined above it."""
    plan = make_approved_plan()
    expected = [cell.name for cell in holes_in(build_cells(plan))]
    completer = Obliging()

    report = fill_holes(plan, completer=completer, analysis=make_analysis())

    asked = [request.messages[1]["content"].split("`")[1] for request in completer.requests]
    assert asked == expected
    assert [cell.name for cell in report.cells] == expected


def test_an_accepted_body_is_spliced_under_the_header_with_the_marker_gone() -> None:
    completer = Scripted("alpha = 42")

    report = fill_holes(
        make_approved_plan(), completer=completer, cells=one_hole(), analysis=make_analysis()
    )

    code = filled(report, "alpha")
    assert code.startswith("# Stage 1 of 1: alpha")
    assert "# Intent: something the workbook did" in code
    assert TODO_MARKER not in code
    assert not is_unwritten(code)
    assert code.endswith("alpha = 42")
    assert report.cells[0].outcome is FillOutcome.FILLED
    assert report.cells[0].first_time


def test_a_rejection_goes_back_to_the_model_and_the_next_answer_is_taken() -> None:
    completer = Scripted(PANDAS, "alpha = 42")

    report = fill_holes(
        make_approved_plan(), completer=completer, cells=one_hole(), analysis=make_analysis()
    )

    cell = report.cells[0]
    assert cell.outcome is FillOutcome.FILLED
    assert cell.tries == 2
    assert cell.after_retries and not cell.first_time
    assert report.first_time == 0
    assert report.after_retries == 1
    # The gate's own words, not a paraphrase of them, are what the model was sent back.
    verdict = completer.requests[1].messages[-1]["content"]
    assert "polars, never pandas" in verdict


def test_a_cell_the_gate_never_accepts_keeps_the_scaffolders_placeholder() -> None:
    """A hole left as a hole still has to produce a notebook that parses."""
    completer = Scripted(PANDAS)
    cells = one_hole()

    report = fill_holes(
        make_approved_plan(),
        completer=completer,
        cells=cells,
        analysis=make_analysis(),
        max_attempts=2,
    )

    cell = report.cells[0]
    assert cell.outcome is FillOutcome.REJECTED
    assert cell.tries == 2
    assert cell.code == cells[0].code
    assert is_unwritten(filled(report, "alpha"))
    assert not report.complete
    assert report.unfilled == (cell,)


def test_a_reply_with_no_cell_body_is_empty_rather_than_rejected() -> None:
    """Different problem, different answer: nothing was written, so nothing was checked."""
    completer = Scripted("")

    report = fill_holes(
        make_approved_plan(),
        completer=completer,
        cells=one_hole(),
        analysis=make_analysis(),
        max_attempts=2,
    )

    cell = report.cells[0]
    assert cell.outcome is FillOutcome.EMPTY
    assert cell.tries == 2
    assert cell.detail == "no cell body in any of 2 reply(ies)"
    assert "Reply with Python only" in completer.requests[1].messages[-1]["content"]


def test_prose_that_does_not_parse_is_the_gate_rejecting_it_not_an_empty_reply() -> None:
    """A model that answered with a question said something; it just said the wrong thing."""
    report = fill_holes(
        make_approved_plan(),
        completer=Scripted("Certainly! Which sheet holds the uplift?"),
        cells=one_hole(),
        analysis=make_analysis(),
        max_attempts=1,
    )

    assert report.cells[0].outcome is FillOutcome.REJECTED
    assert report.cells[0].attempts[0].stage == "syntax"


def test_a_body_that_does_not_define_the_cells_name_is_named_at_the_point_it_happened() -> None:
    """No kernel here, so nobody else will say it. Three cells later is the wrong place."""
    completer = Scripted("_working = 1")

    report = fill_holes(
        make_approved_plan(),
        completer=completer,
        cells=one_hole(),
        analysis=make_analysis(),
        max_attempts=1,
    )

    cell = report.cells[0]
    assert cell.outcome is FillOutcome.REJECTED
    assert cell.attempts[0].stage == "definition"
    assert "must define 'alpha'" in cell.attempts[0].violations[0]


def test_an_echoed_header_is_dropped_so_the_marker_does_not_survive_the_splice() -> None:
    """Otherwise a translated cell reads as unwritten for ever and every count is wrong."""
    hole = one_hole()[0]
    completer = Scripted(f"{hole.code.rsplit(chr(10), 1)[0]}\nalpha = 42")

    report = fill_holes(
        make_approved_plan(), completer=completer, cells=[hole], analysis=make_analysis()
    )

    code = filled(report, "alpha")
    assert TODO_MARKER not in code
    assert code.count("# Stage 1 of 1: alpha") == 1


def test_a_fenced_reply_is_unwrapped_rather_than_scored_as_prose() -> None:
    """There is no tool argument to put code in, so a fence is a model being helpful."""
    completer = Scripted("Here you go:\n\n```python\nalpha = 42\n```\n")

    report = fill_holes(
        make_approved_plan(), completer=completer, cells=one_hole(), analysis=make_analysis()
    )

    assert report.cells[0].outcome is FillOutcome.FILLED
    assert filled(report, "alpha").endswith("alpha = 42")


def test_a_hole_is_told_about_the_names_the_hole_above_it_just_defined() -> None:
    """The registry is rebuilt after every fill; filling out of order would be filling blind."""
    completer = Scripted("alpha_lookup = 5\nalpha = alpha_lookup", "beta = alpha")

    report = fill_holes(
        make_approved_plan(),
        completer=completer,
        cells=[*one_hole("alpha"), *one_hole("beta")],
        analysis=make_analysis(),
    )

    assert report.filled == 2
    registries = [
        request.messages[0]["content"].split("## Name registry")[-1]
        for request in completer.requests
    ]
    assert "alpha_lookup" not in registries[0]
    assert "alpha_lookup" in registries[1]


# ── six outcomes, not two ────────────────────────────────────────────────────────────────────


def test_an_endpoint_failure_is_attributed_to_the_endpoint_and_abandons_the_run() -> None:
    """A dead endpoint is one fact. Asking it five more times reports it five times."""
    plan = make_approved_plan()
    holes = len(holes_in(build_cells(plan)))
    completer = Broken()

    report = fill_holes(plan, completer=completer, analysis=make_analysis())

    assert completer.calls == 1
    assert report.errored == 1
    assert report.skipped == holes - 1
    assert report.cells[0].outcome is FillOutcome.ERROR
    assert "ConnectionError: connection refused" in report.cells[0].detail
    assert all(cell.outcome is FillOutcome.SKIPPED for cell in report.cells[1:])
    assert all(cell.tries == 0 for cell in report.cells[1:])
    # Never the model's judgement: nothing here is a rejection.
    assert report.counts()["rejected"] == 0
    assert report.completions == 1


def test_keep_going_puts_every_hole_to_the_endpoint_even_after_one_fails() -> None:
    plan = make_approved_plan()
    holes = len(holes_in(build_cells(plan)))
    completer = Broken()

    report = fill_holes(plan, completer=completer, analysis=make_analysis(), stop_on_error=False)

    assert completer.calls == holes
    assert report.errored == holes
    assert report.skipped == 0


def test_counts_names_every_outcome_even_at_zero() -> None:
    """A missing key is how a category quietly stops being reported."""
    report = fill_holes(
        make_approved_plan(),
        completer=Scripted("alpha = 42"),
        cells=one_hole(),
        analysis=make_analysis(),
    )

    assert report.counts() == {
        "filled": 1,
        "rejected": 0,
        "empty": 0,
        "error": 0,
        "skipped": 0,
    }


def test_only_a_filled_hole_reads_as_a_success() -> None:
    assert bool(FillOutcome.FILLED)
    assert not any(bool(outcome) for outcome in FillOutcome if outcome is not FillOutcome.FILLED)


def test_a_conversion_with_no_holes_is_complete_and_says_it_asked_nothing() -> None:
    """Honest rather than flattering: there was nothing to leave unwritten."""
    completer = Scripted("alpha = 42")

    report = fill_holes(
        make_approved_plan(),
        completer=completer,
        cells=[ScaffoldCell(name="alpha", code="alpha = 1")],
        analysis=make_analysis(),
    )

    assert report.holes == 0
    assert report.complete
    assert completer.requests == []


def test_the_report_renders_every_attempt_rather_than_a_verdict() -> None:
    report = fill_holes(
        make_approved_plan(),
        completer=Scripted(PANDAS, "alpha = 42"),
        cells=one_hole(),
        analysis=make_analysis(),
    )

    rendered = report.render()
    assert "[FILLED] alpha (2 attempt(s))" in rendered
    assert "attempt 1: rejected at style" in rendered
    assert "attempt 2: accepted" in rendered

    payload = report.as_dict()
    assert payload["holes"] == 1
    assert payload["filled"] == 1
    assert payload["after_retries"] == 1
    assert payload["counts"]["filled"] == 1
    assert payload["cells"][0]["name"] == "alpha"


# ── the prompt ───────────────────────────────────────────────────────────────────────────────


def test_the_prompt_is_the_shipped_one_minus_the_tools_it_does_not_offer() -> None:
    """Subtracted by name, so a part added to the chat prompt tomorrow is sent tomorrow."""
    assert tuple(part for part in SYSTEM_PARTS if part != "tools.md") == FILL_PROMPT_PARTS
    assert "tools.md" in SYSTEM_PARTS
    assert len(FILL_PROMPT_PARTS) == len(SYSTEM_PARTS) - 1


def test_the_prompt_carries_every_shipped_part_it_keeps_verbatim() -> None:
    prompt = system_prompt()

    for part in FILL_PROMPT_PARTS:
        text = load_prompt(part)
        assert text and text in prompt, f"{part} is not sent verbatim"


def test_dropping_tools_does_not_drop_the_rules_the_gate_still_enforces() -> None:
    """A model warned in the chat and ambushed here would be refused for a rule nobody sent."""
    prompt = system_prompt()

    assert policy_rules() in prompt
    assert "Policy rejects shell execution" in prompt
    # The rest of tools.md is gone: a tool surface that does not exist here.
    assert "## The notebook" not in prompt


def test_policy_rules_refuses_to_invent_the_rules_when_the_heading_moves() -> None:
    """Silently sending no rules is the exact failure the check exists to prevent."""
    with pytest.raises(LookupError, match="POLICY_SOURCE"):
        policy_rules(("tools.md", "## Somewhere Else"))


def test_the_task_block_says_there_are_no_tools_after_every_shipped_part() -> None:
    """``role.md`` names tools in passing and no subtraction removes that; this answers it."""
    prompt = system_prompt()

    assert "you have no tools" in prompt
    assert prompt.index("you have no tools") > prompt.index(load_prompt("role.md"))


def test_the_pinned_blocks_are_in_the_chat_loops_least_volatile_first_order() -> None:
    """Analysis, plan, registry. A prompt cache keys on the prefix; reordering costs tokens."""
    completer = Scripted("alpha = 42")
    fill_holes(
        make_approved_plan(), completer=completer, cells=one_hole(), analysis=make_analysis()
    )

    head = completer.requests[0].messages[0]["content"]
    assert head.index("## Workbook analysis") < head.index("## Process plan")
    assert head.index("## Process plan") < head.index("## Name registry")


def test_the_prompt_and_the_pinned_blocks_travel_as_one_system_message() -> None:
    """As ``ConversationWindow._render`` does. A split moves 8k tokens into another role."""
    completer = Scripted("alpha = 42")
    fill_holes(
        make_approved_plan(), completer=completer, cells=one_hole(), analysis=make_analysis()
    )

    messages = completer.requests[0].messages
    assert [message["role"] for message in messages] == ["system", "user"]
    assert "## Workbook analysis" in messages[0]["content"]
    assert "The cell to write: `alpha`" in messages[1]["content"]


def test_a_missing_analysis_is_said_rather_than_guessed_at() -> None:
    completer = Scripted("alpha = 42")

    fill_holes(make_approved_plan(), completer=completer, cells=one_hole(), analysis=None)

    assert "No analysis is loaded" in completer.requests[0].messages[0]["content"]


# ── onto a notebook file ─────────────────────────────────────────────────────────────────────


NOTEBOOK = """import marimo

app = marimo.App(width="medium")


@app.cell
def _():
    import marimo as mo

    return (mo,)


if __name__ == "__main__":
    app.run()
"""


@pytest.fixture
def notebook(tmp_path: Path) -> Path:
    path = tmp_path / "process.py"
    path.write_text(NOTEBOOK, encoding="utf-8")
    return path


async def test_convert_notebook_scaffolds_then_fills_and_writes_the_bodies(
    notebook: Path,
) -> None:
    """The headless path end to end: no kernel, no marimo process, one file on disk."""
    plan = make_approved_plan()
    driver = FileNotebookDriver(notebook)

    report = await convert_notebook(plan, driver, completer=Obliging(), analysis=make_analysis())

    assert report.complete
    assert report.holes > 0
    assert set(report.written) == {cell.name for cell in report.cells}
    source = notebook.read_text(encoding="utf-8")
    assert TODO_MARKER not in source
    assert "load_handin = 1" in source


async def test_a_cell_already_translated_is_never_asked_about_again(notebook: Path) -> None:
    """Holes are found in what is on disk, not in a fresh scaffold. Nobody's work is redone."""
    plan = make_approved_plan()
    driver = FileNotebookDriver(notebook)
    await convert_notebook(plan, driver, completer=Obliging(), analysis=make_analysis())

    second = Obliging()
    again = await convert_notebook(
        plan, FileNotebookDriver(notebook), completer=second, analysis=make_analysis()
    )

    assert again.holes == 0
    assert again.complete
    assert second.requests == []


async def test_a_hole_left_unfilled_is_named_and_the_cell_is_left_as_it_was(
    notebook: Path,
) -> None:
    plan = make_approved_plan()

    report = await convert_notebook(
        plan,
        FileNotebookDriver(notebook),
        completer=Scripted(PANDAS),
        analysis=make_analysis(),
        max_attempts=1,
        stop_on_error=False,
    )

    assert not report.complete
    assert report.written == ()
    assert {cell.outcome for cell in report.cells} == {FillOutcome.REJECTED}
    assert TODO_MARKER in notebook.read_text(encoding="utf-8")


async def test_convert_notebook_refuses_a_plan_nobody_approved(notebook: Path) -> None:
    """The one thing every path into the notebook refuses, and there is no flag past it."""
    completer = Obliging()

    with pytest.raises(PlanNotApprovedError):
        await convert_notebook(
            make_plan(), FileNotebookDriver(notebook), completer=completer, sync=False
        )

    assert completer.requests == []


# ── the command line ─────────────────────────────────────────────────────────────────────────
#
# `kedge convert` is the headless entry point, so these drive the whole verb: an approved plan on
# disk, a fake completer in place of the endpoint, a notebook file at the end of it. The exit code
# is part of the surface -- 1 while anything is still unwritten -- because that is what lets a
# script tell a finished conversion from one that needs a person.


@pytest.fixture
def converting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workbook with an approved plan beside it, in an isolated home and working directory."""
    home = tmp_path / "kedge-home"
    home.mkdir()
    monkeypatch.setenv("KEDGE_HOME", str(home))
    working = tmp_path / "cwd"
    working.mkdir()
    monkeypatch.chdir(working)

    workbook = working / "rwa_monthly_v14.xlsx"
    workbook.write_bytes(b"")
    approved_plan_store(cli._workspace_for(workbook).plans_dir)
    return workbook


def pretend_endpoint(monkeypatch: pytest.MonkeyPatch, completer: object) -> None:
    """Stand in for the model endpoint. Nothing here reads the keyring or opens a socket."""
    monkeypatch.setattr("kedge.plan.propose.completer_from_config", lambda config: completer)


def test_convert_refuses_a_workbook_with_no_plan_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEDGE_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    workbook = tmp_path / "nothing.xlsx"
    workbook.write_bytes(b"")

    result = CliRunner().invoke(app, ["convert", str(workbook)])

    assert result.exit_code == 1
    assert "no process plan saved" in " ".join(result.output.split())


def test_convert_refuses_a_plan_nobody_approved_and_names_the_verb_that_would(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Propose one" is useless advice to somebody who already has one sitting unapproved."""
    monkeypatch.setenv("KEDGE_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    workbook = tmp_path / "rwa_monthly_v14.xlsx"
    workbook.write_bytes(b"")
    PlanStore(cli._workspace_for(workbook).plans_dir).save(make_plan())

    result = CliRunner().invoke(app, ["convert", str(workbook)])

    flattened = " ".join(result.output.split())
    assert result.exit_code == 1
    assert "is 'draft', not 'approved'" in flattened
    assert "kedge plan approve" in flattened


def test_convert_fills_every_hole_writes_the_notebook_and_exits_zero(
    converting: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pretend_endpoint(monkeypatch, Obliging())

    result = CliRunner().invoke(app, ["convert", str(converting)])

    notebook = cli._workspace_for(converting).notebook_path
    assert result.exit_code == 0, result.output
    assert notebook.is_file()
    assert TODO_MARKER not in notebook.read_text(encoding="utf-8")
    assert "nothing is left unwritten" in " ".join(result.output.split())


def test_convert_exits_non_zero_and_names_what_it_could_not_write(
    converting: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pretend_endpoint(monkeypatch, Scripted(PANDAS))

    result = CliRunner().invoke(app, ["convert", str(converting), "--max-attempts", "1"])

    flattened = " ".join(result.output.split())
    assert result.exit_code == 1
    assert "cell(s) still to write" in flattened
    assert "load_handin: rejected" in flattened
    assert TODO_MARKER in cli._workspace_for(converting).notebook_path.read_text(encoding="utf-8")


def test_convert_reports_a_dead_endpoint_as_a_dead_endpoint(
    converting: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not as six cells the model could not write. Attribution is the whole argument."""
    pretend_endpoint(monkeypatch, Broken())

    result = CliRunner().invoke(app, ["convert", str(converting), "--json"])

    # ``CliRunner`` folds stderr into ``output`` and kedge logs to stderr, so the payload is
    # picked out rather than parsed whole. On a real terminal stdout carries the JSON alone --
    # which is why the command prints none of its own chatter under ``--json``.
    payload = json.loads(result.output[result.output.index("{") :])
    assert result.exit_code == 1
    assert payload["counts"]["error"] == 1
    assert payload["counts"]["skipped"] == payload["holes"] - 1
    assert payload["counts"]["rejected"] == 0
    assert payload["written"] == []
