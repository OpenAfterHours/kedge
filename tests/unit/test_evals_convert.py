"""The code-generation eval grades a generated conversion, and can attribute a bad one.

Three parts, and the second and third are the ones that matter.

The first is the **positive control**, and it is the Phase 2 equivalent of committing the
reference conversion at all: a scripted model replaying the reference conversion's own cell
bodies is driven through the whole pipeline -- scaffold, fill, gate, render, run, grade -- with
no endpoint, no keyring and no marimo kernel. Machinery whose graders have never watched a good
run pass is machinery nobody has checked the polarity of.

The second is a set of **negative controls**, in the style of ``test_evals_harness.py``. A model
that writes pandas, one that redefines a name another cell owns, one that answers with nothing, a
completer that raises on every request, and one that raises only after the first: each must come
back as its own outcome, and none may be quietly scored as a conversion that merely graded badly.
Five different things happened and a user choosing a model needs all five told apart.

The third is a set of **anti-flattery controls**, added after a review found the score was not as
honest as it looked. A harness that re-keys a script onto a notebook's own names can flatter it in
two ways -- by binding an action to something that is not a widget, where the action then vanishes
silently, and by sorting a badly-ordered notebook into an order it would never be emitted in --
and it had been doing both. Each of those now has a test that fails if the guard is removed, and
the score itself is asserted as an exact pair per tier, because a bound on failures leaves a
regression from PASS to SKIP invisible.

Nothing here touches the network. ``ScriptedCompleter`` lives in the library precisely so an eval
can replay canned responses, and every completer below is one of those or a two-line fake.

What is being driven is :mod:`kedge.agent.fill` -- the loop ``kedge convert`` runs -- reached
through the eval's own names in :mod:`harness.cellgen`. It used to be a near-identical copy of
that loop, with a prompt that had already drifted six bytes from the shipped one, so the number
these tests protect was a number about the copy. Two tests near the top assert the identity
directly, because everything below is worth only as much as it.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

EVAL_ROOT = Path(__file__).resolve().parents[2] / "evals"
if str(EVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(EVAL_ROOT))

from adjustment_signoff import case as adjustment_case  # noqa: E402
from harness.align import Alignment, Widgets, align_inputs  # noqa: E402
from harness.cellgen import (  # noqa: E402
    TODO_MARKER,
    CellOutcome,
    convert,
)
from harness.cellprompt import (  # noqa: E402
    CELL_PROMPT_PARTS,
    CELL_TASK,
    cell_messages,
    policy_rules,
    system_prompt,
)
from harness.convert import ConversionOutcome, convert_and_grade, driven_run  # noqa: E402
from harness.findings import (  # noqa: E402
    Coverage,
    ReDrive,
    ReDriveLog,
    coverage_for,
)
from harness.model import EvalReport, ItemResult, Outcome, TierResult  # noqa: E402
from harness.render import plan_layout, write_notebook  # noqa: E402

from kedge.agent.prompts import SYSTEM_PARTS, load_prompt  # noqa: E402
from kedge.agent.validate import MAX_VALIDATION_ATTEMPTS  # noqa: E402
from kedge.notebook.scaffold import holes_in  # noqa: E402
from kedge.plan.propose import CompletionRequest, ScriptedCompleter  # noqa: E402
from kedge.plan.store import plan_from_yaml  # noqa: E402

PLAN_PATH = adjustment_case.CASE_DIR / "plan.yaml"


# =============================================================================
# THE REFERENCE BODIES, AS A MODEL WOULD HAVE HAD TO WRITE THEM
# =============================================================================
#
# These are ``adjustment_signoff/build_notebook.py``'s own cell bodies, moved into the holes the
# scaffolder leaves. Two adaptations, and both are consequences of the scaffold deciding the
# structure rather than of anything being loosened:
#
# * The reference notebook has a hand-written `scope` cell holding UPLIFT_RATE and
#   ENTITIES_IN_SCOPE. The scaffold has no such stage, so the constants are declared in the cell
#   that uses them -- which is what a model filling this hole would have to do.
# * The reference names its period widgets `period_end` and `ledger`. The scaffolder derives
#   every widget name from the stage id, so the sign-off reads
#   `extract_query_period_end` / `extract_query_ledger` instead. Same values, the scaffolder's
#   names.

PRE_ADJUSTMENT = "pre_adjustment = pre_adjustment_frame\npre_adjustment"

POST_ADJUSTMENT = "post_adjustment = post_adjustment_frame\npost_adjustment"

ADJUST = """UPLIFT_RATE = 0.045
ENTITIES_IN_SCOPE = ["E-04", "E-07", "E-09", "E-12"]
adjust = (
    pre_adjustment.filter(
        pl.col("entity_id").is_in(ENTITIES_IN_SCOPE) & (pl.col("status") != "CANCELLED")
    )
    .with_columns(
        pl.col("accrual_gbp").xl.empty_as_zero().alias("accrual_gbp_before"),
    )
    .with_columns(
        (pl.col("accrual_gbp_before") * UPLIFT_RATE).xl.round(2).alias("uplift_gbp"),
        (pl.col("accrual_gbp_before") * (1 + UPLIFT_RATE)).xl.round(2).alias("accrual_gbp_after"),
        pl.format("Q2 uplift for {}", pl.col("entity_name")).alias("adjustment_note"),
    )
    .select(
        "trade_id",
        "entity_id",
        "entity_name",
        "accrual_gbp_before",
        "uplift_gbp",
        "accrual_gbp_after",
        "adjustment_note",
    )
)
adjust_totals = adjust.select(
    pl.len().alias("rows"),
    pl.col("accrual_gbp_before").sum().xl.round(2).alias("before"),
    pl.col("uplift_gbp").sum().xl.round(2).alias("uplift"),
    pl.col("accrual_gbp_after").sum().xl.round(2).alias("after"),
).collect()
mo.vstack([mo.md("### The uplift, computed"), adjust_totals])"""

VERIFICATION = """_predicted = adjust.select("trade_id", pl.col("accrual_gbp_after").alias("accrual_gbp"))
_actual = post_adjustment.filter(
    pl.col("entity_id").is_in(ENTITIES_IN_SCOPE) & (pl.col("status") != "CANCELLED")
).select("trade_id", "accrual_gbp")
verification = kedge.reconcile.verify(_predicted, _actual, keys=["trade_id"])
mo.md(verification.render())"""

SIGNOFF = """_final = KEDGE_RUNS.load(KEDGE_RUN_ID)
signoff = {
    "run_id": KEDGE_RUN_ID,
    "period_end": extract_query_period_end.value,
    "ledger": extract_query_ledger.value,
    "entities_in_scope": ENTITIES_IN_SCOPE,
    "rows_adjusted": int(adjust_totals["rows"][0]),
    "accrual_total_before": float(adjust_totals["before"][0]),
    "accrual_total_after": float(adjust_totals["after"][0]),
    "movement": float(adjust_totals["uplift"][0]),
    "decision": approve_adjustment["decision"],
    "verified": verification.ok,
    "verification": verification.summary_line(),
    "audit": _final.audit_lines(),
}
mo.md(chr(10).join(f"- {_line}" for _line in signoff["audit"]))"""

RECONCILIATION_VALUES = """reconciliation_values = {
    "adjustment_c17_c92": adjust.select("entity_name").collect(),
    "adjustment_d17_d92": adjust.select("accrual_gbp_before").collect(),
    "adjustment_e17_e92": adjust.select("uplift_gbp").collect(),
    "adjustment_f17_f92": adjust.select("accrual_gbp_after").collect(),
    "adjustment_d94_f94": [
        float(adjust_totals["before"][0]),
        float(adjust_totals["uplift"][0]),
        float(adjust_totals["after"][0]),
    ],
}
reconciliation_values"""
"""The region map, which is a hole like any other and has to be filled like any other.

The scaffolder cannot write this one: it knows the operation ids, but which column of which frame
reproduces a given workbook range is a translation judgement. Its default maps every id to the
whole ``adjust`` frame, which resolves for four of the five only because the workbook's own header
happens to name a column of that frame -- and never for ``adjustment_d94_f94``, the totals row
under columns D to F, which needs three scalars in workbook order instead. That is why the default
carries a ``TODO(kedge)``: an answer that is right four times out of five is exactly the kind that
gets mistaken for finished.

``adjustment_g17_g92`` is deliberately absent. It is the workbook's column of concatenated UPDATE
statements, and the scaffolder routes a hand-off's operations to ``not_reproduced`` on its own --
reproducing that text would reproduce invalid SQL for the entity whose name holds an apostrophe.
"""

REFERENCE_BODIES = [
    PRE_ADJUSTMENT,
    ADJUST,
    RECONCILIATION_VALUES,
    POST_ADJUSTMENT,
    VERIFICATION,
    SIGNOFF,
]
"""In the order the scaffolder emits the holes: the load, the uplift, the region map, the
re-extract, the check, the sign-off. The map sits third because the panel that reads it is
anchored immediately after the last stage it can report on, which is ``adjust``."""

MODEL_WROTE_THESE = (
    "ran_to_completion",
    "totals_to_the_penny",
    "generated_sql_is_valid",
    "null_is_not_zero",
    "verification_finds_exactly_one_break",
    "cancelled_rows_excluded",
    "no_pandas",
    "the_period_is_an_input",
    "a_paste_out_of_excel_works",
)
"""Deterministic items decided by the cell bodies rather than by the scaffold's structure.

Every one of these has to pass on the positive control, because every one of them is a question
about the translation: the pennies, the escaped apostrophe, the blank read as zero, the four
cancelled rows, the single break the re-extract carries, the period reaching the statement, the
grid pasted the way Excel puts it on the clipboard. If one of them goes red, the seam this eval
measures has actually regressed.

The last two were on the other list until a review found they were not being *driven* at all --
they build their own input dicts and re-drive the notebook, which the harness's case stand-in
could not reach, so they came back SKIP and FAIL for reasons that had nothing to do with any
model. They are graded now.
"""

TIER_SCORES = {"deterministic": (45, 45), "structural": (26, 26)}
"""Exactly what the reference bodies score, per tier: ``(earned, available)``.

An exact pair rather than a bound on the failures. ``KNOWN_STRUCTURAL_GAPS`` bounds only what
goes red, so forcing ``does_not_drop_the_sql_column`` -- the sharpest discrimination this eval
has -- from PASS to SKIP took the score from 40/53 to 37/50 and left the suite green: nothing
asserted the total. The structural tier had the same hole, ``earned == available`` being as true
of 15/15 as of 18/18.

The deterministic half moved from ``(42, 45)`` to ``(45, 45)`` when the scaffolder stopped taking
a hand-off's whole step off the page while its parameters were unfilled, and started ordering a
stage's hand-in box below the hand-off that produces the file. Those were the last three points
between a scaffolded conversion and the rubric's ceiling; see ``KNOWN_STRUCTURAL_GAPS``, which is
now empty.

The structural half moved from ``(18, 18)`` to ``(23, 23)`` when that tier stopped grading the
presence of a field and started grading the shape the scaffolder consumes: two new items (the
briefing, and ``mutates`` against the statement), one reweighted (the checkpoint's *position*
relative to the mutating hand-off), and ``takes_two_handins`` re-grounded on the hand-in cells
``build_cells`` actually emits. The reference plan passes all five, which is the point of
asserting them here as well as in ``test_evals_harness.py``: this path scaffolds the plan rather
than reading a committed notebook, so it is the one that would notice if a rubric item were true
of the file on disk and false of what kedge builds from it.

Moving these numbers is allowed. Moving them without saying so in the commit message is what this
prevents.
"""

EXPECTED_SKIPS = {"consults_the_knowledge_pack"}
"""Every item the reference bodies leave ungraded, and there is only one.

It skips because no knowledge pack describes ``fin.accruals``, which is the sense
``evals/README.md`` reserves the word for: nobody's fault, nothing to fix here. Four more used to
sit in this set for a reason that *was* somebody's fault -- the harness could not drive them --
and that is the regression this exact set exists to catch.
"""

KNOWN_STRUCTURAL_GAPS: set[str] = set()
"""Items a perfect set of cell bodies still cannot pass, and why. There are none left.

It held ``progressive_disclosure``, recorded as a difference rather than a defect on the argument
that a scaffolded hand-off blocking with *"Step 1 of 8: extract_query. Fill in the inputs above"*
was the behaviour CLAUDE.md asks for everywhere else, and that the reference conversion only
passed by defaulting its date picker -- which is how somebody extracts the wrong period.

Both halves of that were wrong. Defaulting is not the alternative on offer: the choice is between
withholding the *statement* until it can be scoped, which is right, and withholding the *step*,
which took the runbook's opening instruction off the page and left a box asking for the extract
above the query that produces it. A real user met that as "where is the sql to run to get the
starting data?". The heading and the instruction now render either way, and a stage's hand-in
selector reads the hand-off's token so it can never precede it.

Asserted as an upper bound rather than an equality: a new failure appearing outside it should fail
this test loudly. Every entry names a defect in kedge rather than in a model, and the ones that
are detectable from the notebook itself are also reported by the harness as a
:class:`~harness.convert.Defect`.
"""

EXPECTED_DEFECTS: set[str] = set()
"""What the eval reports about the scaffolder, having generated a conversion with it: nothing.

It held five, and the list is worth keeping in the history of this file because every one of them
was found by *running* this eval rather than by reading the scaffolder, and every one had survived
because the committed reference conversion is hand-written and works around them all:

* ``forward-reference`` -- the reconciliation panel read ``update_statement``, emitted below it.
* ``reconciliation-behind-its-own-checkpoint`` -- reading that name made the panel a dataflow
  descendant of the approval, so the evidence for a decision could not render until after it.
* ``duplicate-head-handin`` -- the fixed head demanded a hand-in no step of the process named, and
  its digest was the one cited to ``check_translation``.
* ``head-handin-does-not-resume`` -- so reopening a runbook stopped in the third cell.
* ``mutating-handoff-declared-read-only`` -- an ``UPDATE`` with ``mutates: false``, hence no
  confirmation, hence no token for the re-extract selector to gate on.

Asserted exactly, and empty. A regression puts an entry back rather than quietly lowering a score,
and each of the four tests below pins the *mechanism* rather than the absence of a string, so a
defect that came back by another route would still be caught.
"""


def plan() -> Any:
    return plan_from_yaml(PLAN_PATH.read_text(encoding="utf-8"))


def _with_read_only_update(source: Any) -> Any:
    """The reference plan with its ``UPDATE`` hand-off declaring ``mutates: false`` again.

    The defect this reproduces has been fixed in the fixture, which is a problem for the detector
    that found it: an input nobody can make wrong any more is an input nobody is testing against.
    So it is put back here, deliberately and in one place, rather than the detector being trusted
    on the strength of having fired once.
    """
    stages = [
        stage.model_copy(
            update={"handoff": stage.effective_handoff().model_copy(update={"mutates": False})}
        )
        if stage.id == "update_statement"
        else stage
        for stage in source.stages
    ]
    return source.model_copy(update={"stages": stages})


def outcome_of(report: Any, item_id: str) -> Outcome:
    for tier in report.tiers:
        for item in tier.items:
            if item.id == item_id:
                return item.outcome
    msg = f"no item {item_id!r} in the report"
    raise AssertionError(msg)


@pytest.fixture(scope="module")
def reference_conversion() -> Any:
    """One full pass of the pipeline on the reference bodies. Shared: it drives a whole notebook."""
    with tempfile.TemporaryDirectory(prefix="kedge-convert-") as workspace:
        yield convert_and_grade(
            adjustment_case,
            plan(),
            completer=ScriptedCompleter(list(REFERENCE_BODIES)),
            notebook=Path(workspace) / "converted.py",
            model="scripted",
        )


@pytest.fixture(scope="module")
def reference_notebook() -> Any:
    """A generated notebook on disk, without the grading. For the parts, not the whole."""
    with tempfile.TemporaryDirectory(prefix="kedge-notebook-") as workspace:
        result = convert(
            plan(),
            completer=ScriptedCompleter(list(REFERENCE_BODIES)),
            workbook_path=adjustment_case.WORKBOOK,
            model="scripted",
        )
        yield write_notebook(result, Path(workspace) / "converted.py")


# =============================================================================
# THE PROMPT
# =============================================================================


def test_the_eval_sends_the_products_prompt_and_not_a_copy_of_it() -> None:
    """Byte for byte, because six bytes is how far the copy had already drifted.

    "You are not in the chat pane" against "This is not the chat pane"; "the file the product
    sends" against "the file the chat sends". Neither difference matters. A copy that can drift by
    six bytes with a green suite is what matters, because the number the eval exists to produce --
    what a model scores writing a whole conversion -- was then being produced by a copy of the
    driver rather than by ``kedge convert``.

    Asserted as object identity as well as equality: equality would still pass over a second
    implementation that happens to agree today, which is precisely the state this replaced.
    """
    from kedge.agent import fillprompt

    assert system_prompt is fillprompt.system_prompt
    assert cell_messages is fillprompt.cell_messages
    assert CELL_PROMPT_PARTS is fillprompt.FILL_PROMPT_PARTS
    assert CELL_TASK is fillprompt.FILL_TASK
    assert system_prompt() == fillprompt.system_prompt()


def test_the_eval_drives_the_product_and_not_a_copy_of_it() -> None:
    """The same argument one layer up: the loop is the shipped one, not a reimplementation.

    A reviewer comparing the two modules' syntax trees found six functions byte-identical and
    ``_fill_one`` different by two renamed types and a log string. An eval that measures a
    near-identical reimplementation measures the reimplementation.
    """
    from kedge.agent import fill as agent_fill
    from kedge.agent.fill import fill_holes

    assert convert.func is fill_holes, "the eval runs the shipped loop, keywords aside"
    assert CellOutcome is agent_fill.FillOutcome


def test_the_prompt_is_the_products_own_parts_less_the_tool_file() -> None:
    """Every part the product ships is sent, except the one describing tools that do not exist.

    Subtraction rather than a second list: this is the assertion that catches somebody adding
    ``sql.md`` to the product prompt and the eval quietly continuing to measure the old one.
    """
    assert set(CELL_PROMPT_PARTS) == set(SYSTEM_PARTS) - {"tools.md"}


def test_every_shipped_part_reaches_the_model_verbatim() -> None:
    prompt = system_prompt()
    for part in CELL_PROMPT_PARTS:
        text = load_prompt(part)
        assert text and text in prompt, f"{part} did not reach the prompt"


def test_the_tool_catalogue_does_not_reach_a_seam_that_has_no_tools() -> None:
    """``tools.md``'s own content is gone: the tables, the caps, the probe discipline.

    ``role.md`` still mentions ``propose_cell`` and ``probe`` in passing, and that is left alone
    deliberately -- it is the product's role prompt and editing a copy of it here would be the
    trap this whole design avoids. What must not survive is the *catalogue*: a model handed a
    table of eleven tools and no way to call one has been set up to answer with a tool call.
    """
    prompt = system_prompt()
    tools_md = load_prompt("tools.md")

    assert tools_md not in prompt
    for heading in ("Your tools, and the discipline", "| Tool | Use it for |", "## Caps"):
        assert heading not in prompt, f"the tool catalogue is still in the prompt: {heading!r}"


def test_the_gates_own_rules_survive_the_file_they_were_dropped_with() -> None:
    """Dropping ``tools.md`` dropped the statement of what ``Policy`` refuses. The gate stayed.

    That combination punishes models the product would pass: a cell reaching for
    ``duckdb.connect(...)`` is warned in the chat pane and ambushed here. The rules are carried
    across quoted rather than paraphrased, so this asserts the quote is genuinely the shipped
    text and not a copy that has drifted.
    """
    quoted = policy_rules()
    shipped = load_prompt("tools.md")

    assert quoted in shipped, "the rules sent to the model are not the shipped ones"
    assert quoted in system_prompt()
    for rule in ("shell", "network", "database", "working directory", "credentials"):
        assert rule in quoted, f"the quoted rules no longer mention {rule!r}"


def test_the_prompt_refuses_to_build_when_the_shipped_rules_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prompt that silently lost the policy rules is the failure this whole quote prevents.

    So the quote is not allowed to fail quietly. Move the heading and the eval stops, naming the
    file and the heading, rather than carrying on and marking models down for a rule it never
    told them.
    """
    from kedge.agent import fillprompt

    monkeypatch.setattr(fillprompt, "load_prompt", lambda _name: "## Something Else\n\nprose")
    with pytest.raises(LookupError, match="no longer has a"):
        policy_rules()


def test_the_task_block_says_there_are_no_tools() -> None:
    """It is appended after every shipped part, so where they differ it is what the model reads."""
    prompt = system_prompt()

    assert prompt.index(CELL_TASK) > prompt.index(load_prompt("role.md"))
    assert "no tools" in prompt
    assert "Python only" in prompt
    assert "TODO(kedge)" in prompt


def test_the_pinned_blocks_ride_in_the_system_message_as_the_product_sends_them() -> None:
    """Same blocks, same order, same *role* -- and the role is the part that was wrong.

    ``ConversationWindow._render`` emits the system prompt and every pinned block as one system
    message. Sending them as a user message here put some eight thousand tokens in a different
    role from production, which model families weight differently -- a per-model-family confound
    in the one number this eval exists to produce.
    """
    from kedge.agent.context import NameRegistry
    from kedge.notebook.scaffold import build_cells

    approved = plan()
    cells = build_cells(approved, workbook_path=adjustment_case.WORKBOOK)
    hole = holes_in(cells)[1]
    messages = cell_messages(
        cell=hole, plan=approved, analysis=None, registry=NameRegistry(()), history=()
    )

    assert [message["role"] for message in messages] == ["system", "user"]
    head = messages[0]["content"]
    assert head.index("Workbook analysis") < head.index("Process plan")
    assert head.index("Process plan") < head.index("Name registry")
    assert "Apply the agreed 4.5% Q2 uplift" in head
    task = messages[1]["content"]
    assert hole.code in task
    assert "Workbook analysis" not in task, "a pinned block leaked into the user turn"


def test_the_product_really_does_pin_into_the_system_message() -> None:
    """The assertion above, pinned to the product rather than to a memory of it.

    If ``ConversationWindow`` ever starts sending pinned blocks as a user message, this goes red
    beside the test that copies its placement -- which is the only way the two stay in step.
    """
    from kedge.agent.context import ConversationWindow

    window = ConversationWindow(system="be useful", budget=100_000)
    window.set_pinned(["## Workbook analysis\n\nthe pinned block"])
    window.add_user("convert the haircut lookup")
    messages = window.assemble()

    assert [message["role"] for message in messages] == ["system", "user"]
    assert "the pinned block" in messages[0]["content"]
    assert "the pinned block" not in messages[1]["content"]


# =============================================================================
# THE POSITIVE CONTROL
# =============================================================================


def test_the_reference_bodies_fill_every_hole_first_time(reference_conversion: Any) -> None:
    result = reference_conversion.result
    assert result.holes == len(REFERENCE_BODIES)
    assert result.filled == result.holes, result.render()
    assert result.first_time == result.holes, result.render()
    assert result.counts() == {
        "filled": 6,
        "rejected": 0,
        "empty": 0,
        "error": 0,
        "skipped": 0,
        "unfillable": 0,
    }


def test_a_translated_cell_stops_declaring_itself_unfinished(reference_conversion: Any) -> None:
    """``holes_in`` finds holes by the marker, so a filled cell that keeps it is a hole for ever.

    The kept notebook is also meant to be read. A reviewer opening a conversion and finding
    "TODO(kedge): translate this stage" above a finished translation cannot tell what was
    actually left undone.
    """
    result = reference_conversion.result
    filled = [cell for cell in result.cells if cell.outcome is CellOutcome.FILLED]
    assert filled
    for cell in filled:
        assert TODO_MARKER not in cell.code, f"{cell.name} still declares itself a hole"
    assert reference_conversion.notebook is not None
    assert TODO_MARKER not in reference_conversion.notebook.read_text(encoding="utf-8")
    # The documentation above the marker is not the marker. It stays.
    body = next(cell.code for cell in filled if cell.name == "adjust")
    assert "# Intent: Apply the agreed 4.5% Q2 uplift" in body


def test_the_generated_notebook_runs_to_completion(reference_conversion: Any) -> None:
    assert reference_conversion.outcome is ConversionOutcome.GRADED
    assert reference_conversion.run_summary.startswith("ran ")


def test_every_item_the_cell_bodies_decide_passes(reference_conversion: Any) -> None:
    report = reference_conversion.report
    for item_id in MODEL_WROTE_THESE:
        assert outcome_of(report, item_id) is Outcome.PASS, (
            f"{item_id} did not pass on the reference bodies:\n{report.render()}"
        )
    failures = {item.id for item in report.failures}
    assert failures <= KNOWN_STRUCTURAL_GAPS, (
        f"a new failure outside the known scaffold gaps: {sorted(failures - KNOWN_STRUCTURAL_GAPS)}"
        f"\n{report.render()}"
    )


def test_the_reference_conversion_scores_exactly_this(reference_conversion: Any) -> None:
    """The exact pair per tier, and the exact set of skips. Not a bound on the failures.

    A bound catches a PASS going red and nothing else. It let a forced SKIP on
    ``does_not_drop_the_sql_column`` -- the eval's own sharpest discrimination -- take ten points
    out of the denominator with the suite still green.
    """
    report = reference_conversion.report
    scores = {tier.name: (tier.earned, tier.available) for tier in report.tiers}

    assert scores == TIER_SCORES, report.render()
    assert {item.id for item in report.skipped} == EXPECTED_SKIPS, report.render()


def test_the_report_carries_the_denominator(reference_conversion: Any) -> None:
    headline = reference_conversion.headline()
    assert "6/6 hole(s) filled" in headline
    assert "/" in headline.splitlines()[1]


def test_the_coverage_states_what_the_score_was_taken_over(reference_conversion: Any) -> None:
    """A bare ``40/53`` hides both halves of the denominator, and one half was the harness's."""
    coverage = reference_conversion.coverage
    assert coverage is not None

    assert coverage.declared > coverage.graded, "the rubric declares more than it graded"
    assert coverage.undriven == (), (
        "an item was graded on a run that never got past the scaffolder's head:\n"
        + coverage.render()
    )
    assert {item.id for item in coverage.ungradeable} == EXPECTED_SKIPS
    assert f"{coverage.declared}-point rubric" in coverage.headline()


def test_the_alignment_is_reported_rather_than_applied_silently(reference_conversion: Any) -> None:
    """A widget re-keyed behind a reader's back is a harness that flatters the notebook.

    The re-keying that remains is the legitimate kind: the scaffolder derives widget names from
    stage ids, so a script written against the reference conversion's ``period_end`` has to reach
    ``extract_query_period_end``. Every one is reported.

    ``head`` is now ``None``, and that is the fix rather than a gap: the scaffolder no longer emits
    a hand-in cell no step of the process names, so there is no longer a duplicate to feed. The
    donor binding existed only to get past it.
    """
    alignment = reference_conversion.alignment
    assert ("period_end", "extract_query_period_end") in alignment.aliases
    assert ("ledger", "extract_query_ledger") in alignment.aliases
    assert alignment.head is None, (
        "a head hand-in is being fed a stage's file; the duplicate should be gone"
    )
    assert any("re-keyed" in note for note in reference_conversion.report.notes)


def test_the_scaffolder_leaves_no_step_the_script_cannot_reach(
    reference_conversion: Any,
) -> None:
    """Every scripted action now finds a widget, and that was not true a fix ago.

    ``update_statement_ran`` and ``update_statement_ran_note`` had nothing to bind to, because the
    plan declared its ``UPDATE`` read-only and the scaffolder emitted no confirmation step. The
    report said so rather than hiding it -- an action that vanishes quietly is a run reported as
    faithfully driven when it was not -- and now there is nothing to say.
    """
    assert set(reference_conversion.alignment.unplayed) == set()


# =============================================================================
# WHAT THE EVAL FOUND OUT ABOUT KEDGE
# =============================================================================


def test_the_scaffolder_leaves_no_defects_in_a_generated_conversion(
    reference_conversion: Any,
) -> None:
    """The five it used to leave are fixed. See :data:`EXPECTED_DEFECTS` for what they were.

    Kept as an exact empty set rather than deleted. This is the assertion that would notice a
    scaffolder change reintroducing any of them, and it is cheaper to read than the five tests
    below -- which pin the individual mechanisms, because an id can be renamed.
    """
    found = {defect.id for defect in reference_conversion.defects}
    assert found == EXPECTED_DEFECTS, [defect.render() for defect in reference_conversion.defects]
    assert "scaffolder defect(s) found" not in reference_conversion.headline()


def test_the_generated_layout_needs_no_reordering_to_run(reference_conversion: Any) -> None:
    """marimo lays a notebook out in file order, so a rewrite changes what the user sees.

    The harness used to topologically sort the emitted cells and call it housekeeping, which moved
    the reconciliation panel from above the checkpoint -- where ``_with_reconciliation``
    deliberately puts it -- to below the hand-off, and graded a layout kedge would never emit. The
    forward reference that forced the sort is gone, so the sort is now a no-op, and *that* is the
    property worth asserting: what the eval grades is what the scaffolder wrote.
    """
    layout = reference_conversion.layout
    assert layout is not None
    assert layout.moved == (), f"the eval is grading a reordered notebook: {layout.moved}"
    assert layout.forward_references == (), layout.forward_references


def test_the_reconciliation_panel_renders_before_the_decision_it_is_evidence_for() -> None:
    """The worse half of the forward reference, and the reason it mattered.

    The panel used to map the hand-off's operation to the rendered statement; the statement reads
    the checkpoint's decision; so marimo had the panel as a *descendant of the approval* and would
    not render it until the approval was recorded. ``_with_reconciliation`` exists to put the
    evidence in front of the person making the decision -- "putting it before the checkpoint makes
    it an argument; leaving it at the bottom makes it a footnote nobody reaches until the decision
    is already made".

    Asserted through ``visible_cells`` rather than through a defect id, because this is a claim
    about what a user sees. The approval is deliberately *not* supplied.
    """
    from harness.drive import visible_cells, workspace_overrides

    with tempfile.TemporaryDirectory(prefix="kedge-eval-panel-") as workspace:
        root = Path(workspace)
        notebook = root / "generated.py"
        convert_and_grade(
            adjustment_case,
            plan(),
            completer=ScriptedCompleter(list(REFERENCE_BODIES)),
            notebook=notebook,
        )
        seen = set(
            visible_cells(
                notebook,
                inputs=adjustment_case.script_for(*adjustment_case.write_handins(root / "in")),
                overrides=workspace_overrides(root, adjustment_case.WORKBOOK),
            )
        )

    assert "reconciliation" in seen, (
        "the evidence for the approval does not render until after the approval; "
        f"visible: {sorted(seen)}"
    )


def test_the_head_asks_for_no_handin_the_process_does_not_name(reference_conversion: Any) -> None:
    """Every hand-in in this plan is declared on a stage, and the head used to ask for one anyway.

    Two consequences, and the second is the one that mattered: the extra cells had no consumers so
    the contract-check ``mo.stop`` gated nothing, and ``check_translation`` was handed *that*
    hand-in's digest rather than the one the arithmetic ran on -- which defeats re-checking an
    acceptance only against the data it was measured on.
    """
    names = set(reference_conversion.result.names)

    assert "handin" not in names, f"the fixed head hand-in is still emitted: {sorted(names)}"
    assert "pre_adjustment_handin" in names, "the stage's own hand-in should still be there"


def test_a_mutating_handoff_declared_read_only_is_detected_from_the_statement() -> None:
    """Detected from the SQL, not from this eval's expectations, so it holds for any plan.

    The reference plan no longer carries the defect -- it declares ``mutates: true`` over its
    ``UPDATE`` -- so the detector is proved against a plan doctored to declare the opposite. That
    is the only honest way to keep this test: a detector whose input has been fixed is a detector
    nobody is checking any more.
    """
    from harness.findings import _confirmation_defects

    doctored = _with_read_only_update(plan())
    result = convert(
        doctored,
        completer=ScriptedCompleter(list(REFERENCE_BODIES)),
        workbook_path=adjustment_case.WORKBOOK,
    )
    defects = _confirmation_defects(result, Alignment(widgets=Widgets()))

    assert [defect.id for defect in defects] == ["mutating-handoff-declared-read-only"]
    assert "UPDATE" in defects[0].evidence
    assert "changes_data" in defects[0].evidence
    # The consequence has changed, and the detail has to say so. `needs_confirmation` now answers
    # from the statement, so the notebook gates correctly even on this doctored plan -- what is
    # left is a plan that misleads the reviewer reading the approval card.
    assert "the notebook is safe" in defects[0].detail

    assert (
        _confirmation_defects(
            convert(
                plan(),
                completer=ScriptedCompleter(list(REFERENCE_BODIES)),
                workbook_path=adjustment_case.WORKBOOK,
            ),
            Alignment(widgets=Widgets()),
        )
        == ()
    ), "the reference plan no longer carries this defect"


def _with_a_statementless_handoff(source: Any) -> Any:
    """The reference plan with ``extract_query``'s hand-off block taken away.

    Exactly what a model produces when it types ``kind: handoff`` and supplies no ``statement``.
    ``Stage.effective_handoff()`` cannot guess one, so it synthesises a hand-off whose statement
    text *is* ``-- TODO(kedge): the plan marked this stage a hand-off but supplied no
    statement``, and the cell that comes out carries the marker inside a string literal on an
    executable line.
    """
    stages = [
        stage.model_copy(update={"handoff": None}) if stage.id == "extract_query" else stage
        for stage in source.stages
    ]
    return source.model_copy(update={"stages": stages})


def test_a_hole_no_model_can_be_asked_to_fill_stays_in_the_models_denominator() -> None:
    """The flattering denominator, pinned where it flatters most: the composed path.

    ``split_hole`` refuses this cell -- there is no placeholder under a header, so replacing "the
    rest of it" would take the display block with it -- and the driver used to drop it where it
    found it. The stage then appeared nowhere: not a hole, not unfilled, not skipped, not refused.
    On ``--convert M --plan-from M`` that is the model which wrote the statement-less hand-off
    having the consequence removed from **its own** generation denominator and printed as "6/6
    hole(s) filled", over a notebook still carrying a ``TODO(kedge)``.
    """
    from kedge.notebook.scaffold import build_cells

    doctored = _with_a_statementless_handoff(plan())
    cells = build_cells(doctored, workbook_path=adjustment_case.WORKBOOK)

    result = convert(
        doctored,
        completer=ScriptedCompleter(list(REFERENCE_BODIES)),
        workbook_path=adjustment_case.WORKBOOK,
    )

    assert [cell.name for cell in holes_in(cells)] == [cell.name for cell in result.cells], (
        "the scaffolder's count of what the notebook owes and the report's must agree"
    )
    blocked = next(cell for cell in result.cells if cell.name == "extract_query")
    assert blocked.outcome is CellOutcome.UNFILLABLE
    assert blocked.tries == 0, "nothing could be asked, so nothing was"
    assert not result.complete
    assert "6/6" not in result.summary_line()
    assert TODO_MARKER in result.codes[result.names.index("extract_query")]


def test_that_conversion_is_incomplete_rather_than_graded(tmp_path: Path) -> None:
    """And the outcome says so, rather than ``GRADED`` over a notebook with a marker in it."""
    report = convert_and_grade(
        adjustment_case,
        _with_a_statementless_handoff(plan()),
        completer=ScriptedCompleter(list(REFERENCE_BODIES)),
        notebook=tmp_path / "converted.py",
    )

    assert report.outcome is ConversionOutcome.INCOMPLETE
    assert report.result.counts()["unfillable"] == 1


def test_the_reference_plan_no_longer_declares_its_update_read_only() -> None:
    """The fixture half of the same defect, pinned so a regenerated plan cannot lose it.

    ``needs_confirmation`` now answers from the statement, so the notebook gates correctly even if
    this flag were wrong again -- but the approval card renders ``mutates``, and a reviewer told a
    production ``UPDATE`` changes nothing is being misled about the one stage that touches live
    data.
    """
    handoff = next(
        stage.effective_handoff() for stage in plan().stages if stage.id == "update_statement"
    )

    assert handoff.mutates
    assert handoff.statement_writes
    assert not handoff.contradicts_its_own_statement


# =============================================================================
# NEGATIVE CONTROLS
# =============================================================================


class _AlwaysCompleter:
    """Answers every request with the same body. No network, no script to exhaust."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.requests: list[CompletionRequest] = []

    def complete(self, request: CompletionRequest) -> str:
        self.requests.append(request)
        return self.response


class _BrokenCompleter:
    """Raises on every request, the way a transport failure or a refused schema does."""

    def complete(self, request: CompletionRequest) -> str:
        msg = "the model endpoint could not be reached"
        raise RuntimeError(msg)


class _FailsAfterOne:
    """Answers the first request and raises on every one after it.

    An endpoint that falls over halfway through a conversion, which is the ordinary shape of a
    rate limit or a dropped connection -- and which used to be reported as though the model had
    written the notebook badly.
    """

    def __init__(self, first: str) -> None:
        self.first = first
        self.calls = 0

    def complete(self, request: CompletionRequest) -> str:
        self.calls += 1
        if self.calls == 1:
            return self.first
        msg = "502 Bad Gateway"
        raise RuntimeError(msg)


def convert_with(completer: Any, **kwargs: Any) -> Any:
    return convert(
        plan(),
        completer=completer,
        workbook_path=adjustment_case.WORKBOOK,
        model="scripted",
        **kwargs,
    )


def test_a_model_that_writes_pandas_is_rejected_every_time_and_recorded_as_such() -> None:
    completer = _AlwaysCompleter("import pandas as pd\nadjust = pd.DataFrame()\nadjust")
    result = convert_with(completer)

    assert result.filled == 0
    assert {cell.outcome for cell in result.cells} == {CellOutcome.REJECTED}
    offender = next(cell for cell in result.cells if cell.name == "adjust")
    assert offender.tries == MAX_VALIDATION_ATTEMPTS
    assert any("pandas" in message for message in offender.attempts[0].violations)
    assert offender.attempts[0].stage == "style"


def test_the_gates_verdict_goes_back_to_the_model_between_attempts() -> None:
    """The retry is a conversation, not a resubmission: the model is told what was wrong."""
    completer = _AlwaysCompleter("import pandas as pd\nadjust = pd.DataFrame()\nadjust")
    convert_with(completer, max_attempts=2)

    retried = [request for request in completer.requests if len(request.messages) > 2]
    assert retried, "no request carried the previous attempt and its violations"
    assert any("pandas" in message["content"] for message in retried[0].messages)


def test_a_model_that_redefines_another_cells_name_is_rejected_by_the_marimo_stage() -> None:
    """marimo's single-definition rule, caught before a kernel would have caught it."""
    completer = _AlwaysCompleter("kedge_run = 1\nadjust = pre_adjustment\nadjust")
    result = convert_with(completer, max_attempts=1)

    offender = next(cell for cell in result.cells if cell.name == "adjust")
    assert offender.outcome is CellOutcome.REJECTED
    assert offender.attempts[0].stage == "marimo"
    assert any("kedge_run" in message for message in offender.attempts[0].violations)


def test_a_model_that_answers_with_nothing_is_empty_rather_than_rejected() -> None:
    """ "Said nothing" and "said something wrong" are different results and must not merge."""
    result = convert_with(_AlwaysCompleter("   \n  \n"), max_attempts=2)

    assert {cell.outcome for cell in result.cells} == {CellOutcome.EMPTY}
    assert result.filled == 0
    assert all(cell.tries == 2 for cell in result.cells)
    assert all(not attempt.violations for cell in result.cells for attempt in cell.attempts)


def test_a_model_that_answers_only_in_prose_fails_at_the_syntax_stage() -> None:
    """Prose is a body that does not parse, which is a rejection rather than an empty reply."""
    result = convert_with(
        _AlwaysCompleter("I would filter the frame and round it."), max_attempts=1
    )

    offender = next(cell for cell in result.cells if cell.name == "adjust")
    assert offender.outcome is CellOutcome.REJECTED
    assert offender.attempts[0].stage == "syntax"


def test_a_body_that_does_not_define_the_cells_name_is_rejected_where_it_happened() -> None:
    """Without this the run stops three cells later and a correct cell takes the blame.

    In the product the kernel catches it. There is no kernel here, so the eval stands in -- and
    the point is attribution, not strictness: the violation names the cell that failed to define
    the name rather than the one that read it.
    """
    completer = _AlwaysCompleter("_working = pre_adjustment.head(3)\n_working")
    result = convert_with(completer, max_attempts=1)

    offender = next(cell for cell in result.cells if cell.name == "adjust")
    assert offender.outcome is CellOutcome.REJECTED
    assert offender.attempts[0].stage == "definition"
    assert "must define 'adjust'" in offender.attempts[0].violations[0]


def test_a_model_that_echoes_the_comment_header_does_not_leave_a_todo_in_working_code() -> None:
    """``holes_in`` finds holes by the marker, so an echoed header is a cell unfinished for ever."""
    from kedge.notebook.scaffold import build_cells

    cells = build_cells(plan(), workbook_path=adjustment_case.WORKBOOK)
    header = next(cell for cell in holes_in(cells) if cell.name == "adjust").code.split(
        "TODO(kedge)"
    )[0]
    echoed = f"{header}TODO(kedge): translate this stage.\nadjust = pre_adjustment\nadjust"
    result = convert_with(_AlwaysCompleter(echoed), max_attempts=1)

    offender = next(cell for cell in result.cells if cell.name == "adjust")
    assert offender.outcome is CellOutcome.FILLED
    assert offender.code.count("TODO(kedge)") == 0, "neither copy of the marker survives"
    assert offender.code.endswith("adjust = pre_adjustment\nadjust")


def test_a_completer_that_raises_is_an_error_not_a_bad_conversion() -> None:
    """Every hole is put to the endpoint, because an eval that stops measures nothing else.

    ``convert`` binds ``stop_on_error=False``; ``kedge convert`` leaves it True. That is one
    parameter on one implementation, set differently for reasons about what each caller is for --
    a user at a terminal has learned everything a dead endpoint can teach after the first failure,
    while a sweep leg would be throwing away five paid-for measurements over one transient 429.
    Here the endpoint is genuinely dead, so all six come back ``ERROR`` and no hole is skipped.
    """
    result = convert_with(_BrokenCompleter())

    assert {cell.outcome for cell in result.cells} == {CellOutcome.ERROR}
    assert result.skipped == 0, "the eval presses on; nothing goes unasked"
    assert all(cell.tries == 1 for cell in result.cells), "a raise must not be retried blindly"
    assert "could not be reached" in result.cells[0].detail
    # Never the model's judgement: nothing here is a rejection.
    assert result.counts()["rejected"] == 0
    assert result.unmeasured, "nothing came back, so nothing was measured"


def test_the_eval_presses_on_where_the_product_stops_and_the_choice_is_visible() -> None:
    """One deliberate keyword is the whole difference, and it is asserted rather than assumed.

    The alternative -- inheriting the product's default -- means one transient 429 on the first
    hole abandons the run, reports ``NO_MODEL``, grades nothing and spends a whole sweep leg on a
    single call. ``INTERRUPTED`` and ``Coverage`` exist precisely so a partial run can be reported
    honestly instead.
    """
    from kedge.agent.fill import fill_holes

    assert convert.func is fill_holes
    assert convert.keywords == {"stop_on_error": False}

    stopping = convert_with(_BrokenCompleter(), stop_on_error=True)

    assert stopping.errored == 1
    assert stopping.skipped == stopping.holes - 1, "the product's behaviour is one keyword away"


def test_a_conversion_with_no_model_at_all_is_not_scored(tmp_path: Path) -> None:
    """Nothing was measured, so nothing is reported as measured. The reconciliation sin, applied."""
    report = convert_and_grade(
        adjustment_case,
        plan(),
        completer=_BrokenCompleter(),
        notebook=tmp_path / "converted.py",
    )

    assert report.outcome is ConversionOutcome.NO_MODEL
    assert report.report is None
    assert not report.ok
    assert report.exit_code() == 1
    assert "nothing about the model was measured" in report.detail


def test_an_endpoint_that_fails_part_way_is_interrupted_rather_than_incomplete(
    tmp_path: Path,
) -> None:
    """A transport failure that arrives after the first answer is still a transport failure.

    It used to report ``INCOMPLETE`` -- "the notebook is a scaffold with gaps in it", which reads
    as the model's fault -- with a score printed beside it over a rubric that had quietly shrunk.
    Only the all-requests-failed case reached ``NO_MODEL``.
    """
    report = convert_and_grade(
        adjustment_case,
        plan(),
        completer=_FailsAfterOne(PRE_ADJUSTMENT),
        notebook=tmp_path / "converted.py",
    )

    assert report.outcome is ConversionOutcome.INTERRUPTED
    assert report.result.errored == 5
    assert not report.result.unmeasured, "one hole did come back; this is not NO_MODEL"
    assert report.result.filled == 1
    assert report.coverage is not None
    assert report.coverage.graded < report.coverage.declared, (
        "the rubric shrank and the report has to say by how much"
    )
    assert f"{report.coverage.declared}-point rubric" in report.headline()


def test_an_unfilled_hole_is_incomplete_rather_than_a_bad_score(tmp_path: Path) -> None:
    """A scaffold with a passthrough in it still runs. It is not a conversion, and says so."""
    pandas_every_time = ["import pandas as pd"] * MAX_VALIDATION_ATTEMPTS
    completer = ScriptedCompleter(
        [
            PRE_ADJUSTMENT,
            *pandas_every_time,
            RECONCILIATION_VALUES,
            POST_ADJUSTMENT,
            VERIFICATION,
            SIGNOFF,
        ]
    )
    report = convert_and_grade(
        adjustment_case, plan(), completer=completer, notebook=tmp_path / "converted.py"
    )

    assert report.outcome is ConversionOutcome.INCOMPLETE
    assert report.result.filled == 5
    assert report.result.holes == 6
    assert report.result.errored == 0, "the model answered every time; the gate refused one"
    assert not report.ok
    assert "6 hole(s)" in report.headline()
    # It was still graded: what the rest of the notebook got right is worth knowing, and the
    # headline leads with INCOMPLETE so nobody reads the score as a whole conversion's.
    assert report.report is not None


def test_a_notebook_that_stops_is_told_apart_from_one_that_graded_badly(tmp_path: Path) -> None:
    """A stage that raises stops the run. That is not the same as running and being wrong."""
    completer = ScriptedCompleter(
        [
            PRE_ADJUSTMENT,
            ADJUST,
            RECONCILIATION_VALUES,
            POST_ADJUSTMENT,
            'verification = adjust.select("no_such_column").collect()\nverification',
            SIGNOFF,
        ]
    )
    report = convert_and_grade(
        adjustment_case, plan(), completer=completer, notebook=tmp_path / "converted.py"
    )

    assert report.result.complete, "every hole was filled; the failure is at run time"
    assert report.outcome is ConversionOutcome.STOPPED
    assert report.report is not None
    assert outcome_of(report.report, "ran_to_completion") is Outcome.FAIL
    assert outcome_of(report.report, "verification_finds_exactly_one_break") is Outcome.BLOCKED, (
        "items about cells that never ran must be blocked, not failed -- one problem reported "
        "once, and still counted against the conversion that caused it"
    )
    # The cells above the failure still ran, and what they got right is still graded. Reporting
    # one broken cell as eleven failures is what the blocked outcome exists to prevent -- and
    # keeping it in the denominator is what stops the stop being worth points.
    assert outcome_of(report.report, "totals_to_the_penny") is Outcome.PASS


def test_stopped_is_decided_from_the_run_and_not_from_a_sentence_about_it() -> None:
    """It used to be ``run_summary.startswith("ran ")``, scraped out of a grader's prose note.

    One edit to ``NotebookRun.summary_line`` would have made every conversion in every report
    ``STOPPED``, silently and for ever. The run object is what is asked now.
    """
    from harness.convert import _outcome_of

    class _Run:
        def __init__(self, completed: bool) -> None:
            self.completed = completed

        def summary_line(self) -> str:
            return "the notebook did a thing"

    result = convert_with(ScriptedCompleter(list(REFERENCE_BODIES)))

    assert _outcome_of(result, _Run(completed=True)) is ConversionOutcome.GRADED
    assert _outcome_of(result, _Run(completed=False)) is ConversionOutcome.STOPPED
    assert _outcome_of(result, None) is ConversionOutcome.GRADED, (
        "a run nobody observed is not a run that stopped"
    )


# =============================================================================
# THE COMPOSED PATH
# =============================================================================
#
# ``--convert MODEL --plan-from MODEL``: the model writes the plan, kedge scaffolds it, the model
# fills the holes, and the whole thing is graded. It is the only path a hub user takes and it was
# measured nowhere, because ``run.py`` refused ``--model`` with ``--convert`` and that refusal --
# written about a model's plan graded beside a *human's* notebook -- caught the reverse
# composition too.
#
# Nothing here calls a model. What is tested is the plumbing that keeps the composed score from
# being read as the gold-plan score, and the outcome that stops a run which never got a plan being
# filed as a model that writes bad cells.


def _relabelled(report: Any, **changes: Any) -> Any:
    from harness.convert import ConversionReport

    return ConversionReport(
        case=report.case,
        result=report.result,
        outcome=report.outcome,
        report=report.report,
        **changes,
    )


def test_both_convert_shapes_say_whose_plan_they_used(reference_conversion: Any) -> None:
    """Neither total is a whole conversion's, and each is not-a-whole-conversion in its own way.

    The composed one is every point the model's; the ordinary one is the model's cell bodies over
    a *human's* plan, with a quarter of the board earned by whoever wrote that plan. The caveat
    used to appear on the composed path only, and to describe a plain ``--convert`` figure as
    measuring "the cell bodies alone" -- which it does not, and which is the same confound
    ``--plan-from`` without ``--convert`` is refused for, only smaller.

    Both lines sit above the score. A caveat under a total is a caveat nobody has read by the time
    they have read the total.
    """
    assert "COMPOSED PATH" not in reference_conversion.headline(), (
        "the library default must stay silent; the CLI is what knows the provenance"
    )

    composed = _relabelled(
        reference_conversion, plan_origin="proposed by some-model", plan_is_the_models=True
    )
    gold = _relabelled(
        reference_conversion, plan_origin="read from plan.yaml", plan_is_the_models=False
    )

    assert "COMPOSED PATH -- plan proposed by some-model" in composed.headline()
    assert "Not comparable" in composed.headline()
    assert "PLAN NOT THE MODEL'S -- read from plan.yaml" in gold.headline()
    assert "cell bodies and nothing else" in gold.headline()

    for report in (composed, gold):
        headline = report.headline()
        assert headline.index("points are structural") < headline.index(report.report.headline()), (
            "the caveat must come before the number it qualifies"
        )
        # The share is stated rather than left to a reader who would have to add up the rubric.
        structural = next(tier for tier in report.report.tiers if tier.name == "structural")
        assert f"{structural.available} of {report.report.available}" in headline


def test_a_run_that_never_got_a_plan_is_not_a_model_that_wrote_bad_cells() -> None:
    """``INCOMPLETE`` reads "the gaps are the model's". A missing plan has no gaps; it has no cells.

    Six outcomes became seven for the same reason ``INTERRUPTED`` was split off ``INCOMPLETE``:
    an endpoint's bad afternoon must not be written down as a model's judgement, and a score
    printed against a conversion nobody attempted is worse than no score.
    """
    from harness.convert import no_plan_proposed

    report = no_plan_proposed(
        adjustment_case,
        plan_origin="proposed by some-model",
        detail="no plan from some-model: timeout (a fact about the integration).",
    )

    assert report.outcome is ConversionOutcome.NO_PLAN
    assert report.result is None, "there is no generation denominator for a run that never began"
    assert report.report is None
    assert not report.ok
    assert report.exit_code() == 1
    rendered = report.render()
    assert "timeout" in rendered
    assert "COMPOSED PATH" in rendered
    assert "incomplete" not in rendered


def test_a_plan_kedge_refuses_to_scaffold_ends_the_run_rather_than_the_process(
    tmp_path: Path,
) -> None:
    """``build_cells`` raising used to be a traceback out of ``main()``, after the money.

    On the composed path a plan proposal has already been billed by the time the scaffolder sees
    it, and the user got no report, no outcome, and not even the line saying what the proposal
    cost. A plan kedge will not build a notebook from is a result about whoever wrote the plan --
    ``NO_PLAN``, with the refusal quoted -- and never a model writing bad cells, because no cell
    was ever asked for.
    """
    from kedge.notebook.scaffold import ScaffoldError

    def refuse(*args: Any, **kwargs: Any) -> Any:
        msg = "cell 'post_adjustment_input' would not parse"
        raise ScaffoldError(msg)

    calls: list[int] = []

    class _CountingCompleter:
        def complete(self, request: CompletionRequest) -> str:
            calls.append(1)
            return "x = 1"

    from kedge.agent import fill as agent_fill

    original = agent_fill.build_cells
    agent_fill.build_cells = refuse  # type: ignore[assignment]
    try:
        report = convert_and_grade(
            adjustment_case,
            plan(),
            completer=_CountingCompleter(),
            notebook=tmp_path / "converted.py",
            model="scripted",
            plan_origin="proposed by some-model",
            plan_is_the_models=True,
        )
    finally:
        agent_fill.build_cells = original  # type: ignore[assignment]

    assert report.outcome is ConversionOutcome.NO_PLAN
    assert report.result is None
    assert calls == [], "a cell was requested for a plan that never scaffolded"
    assert "will not scaffold" in report.detail
    assert "would not parse" in report.detail
    assert report.exit_code() == 1
    assert "COMPOSED PATH" in report.render()


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("repairs_exhausted", "the model's own output never validated as a plan"),
        ("timeout", "not the model's judgement"),
        ("not_configured", "not the model's judgement"),
        ("schema_refused", "the model's own output never validated as a plan"),
        # Its own bucket, and neither of the other two: the model was never asked.
        ("triage_refused", "kedge's own triage declined to plan this workbook"),
    ],
)
def test_a_missing_plan_is_attributed_before_it_is_reported(failure: str, expected: str) -> None:
    """``Failure.about_the_model`` decides it; ``run.py`` quotes the decision rather than guessing.

    This is ``evals/README.md``'s "attribution is the point", one level up from where it was
    written. A model replaced over a proxy's 404 is the mistake; a proxy blamed for prose that
    never parsed is the other one.
    """
    import run
    from harness.live import Failure, MeteredCall

    detail = run.plan_failure_detail(
        "some-model", MeteredCall(failure=Failure(failure), detail="the endpoint said so")
    )

    assert expected in detail
    assert "some-model" in detail
    assert "the endpoint said so" in detail, "a classification with no evidence is unactionable"


def test_the_refusal_is_narrowed_to_the_confounded_composition() -> None:
    """The composed path is allowed; the one that prints a human's marks under a model is not.

    ``--plan-from`` without ``--convert`` *is* that composition -- the committed reference
    conversion graded against a model's plan, with all 45 deterministic points a human earned
    landing under the model's name. It is refused by name rather than by proxy, which is what
    lets ``--convert M --plan-from M`` through.
    """
    import run

    parser = run._parser()

    def refuse(*argv: str) -> str:
        args = parser.parse_args(["adjustment_signoff", *argv])
        with pytest.raises(SystemExit) as raised:
            run._refuse_confounded(parser, args)
        assert raised.value.code == 2
        return str(parser.format_usage())

    refuse("--plan-from", "m")
    refuse("--model", "m", "--convert", "m")
    refuse("--convert", "m", "--plan-from", "m", "--plan", str(PLAN_PATH))

    allowed = parser.parse_args(["adjustment_signoff", "--convert", "m", "--plan-from", "m"])
    run._refuse_confounded(parser, allowed)  # must not raise
    assert allowed.plan_from == "m"


@pytest.mark.parametrize(
    "ignored",
    [
        ["--repeats", "3"],
        ["--notebook", "somewhere.py"],
        ["--json"],
    ],
)
def test_a_flag_the_convert_path_would_ignore_is_refused(ignored: list[str]) -> None:
    """A flag a mode silently ignores is worse than one it refuses.

    ``--convert m --plan-from m --repeats 3`` announced "2 model(s) x 3 repeat(s)" and then did
    one proposal and one pass: a cost estimate a reader would act on, and a number that was never
    true. ``--notebook`` names a notebook to *grade* where the conversion writes one (``--out``),
    and ``--json`` is the sweep's. All three parsed cleanly and did nothing.
    """
    import run

    parser = run._parser()
    args = parser.parse_args(["adjustment_signoff", "--convert", "m", *ignored])

    with pytest.raises(SystemExit) as raised:
        run._refuse_confounded(parser, args)

    assert raised.value.code == 2


# =============================================================================
# THE PARTS
# =============================================================================


def test_the_holes_are_found_by_the_scaffolders_own_marker() -> None:
    from kedge.notebook.scaffold import build_cells

    cells = build_cells(plan(), workbook_path=adjustment_case.WORKBOOK)
    holes = holes_in(cells)

    assert [cell.name for cell in holes] == [
        "pre_adjustment",
        "adjust",
        "reconciliation_values",
        "post_adjustment",
        "verification",
        "signoff",
    ]
    # Not all stages: `reconciliation_values` is a `reconcile` cell and is a hole all the same,
    # because which column reproduces which workbook region is a translation judgement the
    # scaffolder cannot make. It carried no marker once, and so nothing ever asked anyone to
    # finish it -- which is the whole reason holes are found by the marker rather than by role.
    assert {cell.role for cell in holes} == {"stage", "reconcile"}
    assert len(holes) < len(cells), "the scaffolder decides structure; the model fills holes"


def test_a_gate_line_is_kept_out_of_the_hole() -> None:
    """A model rewriting a body must not be able to lose the line that hides the cell.

    ``_gate_<name> = <checkpoint>["decision"]`` is what keeps a stage downstream of an approval
    invisible until the approval is recorded. It is the scaffolder's, not the translation's, so
    it stays in the header the model is shown and never in the part it replaces.

    Asserted against the scaffolder's own functions, which is where they always lived: the eval
    used to import them from its copy of the driver, which imported them from here under private
    aliases, so a test about ``scaffold.py`` was reaching through two modules to get at it.
    """
    from kedge.notebook.scaffold import split_hole

    code = (
        "# Stage 4 of 8: apply\n"
        "# TODO(kedge): translate this stage. LazyFrame throughout, expressions rather than\n"
        "# loops, .collect() only at a boundary.\n"
        '_gate_apply = approve["decision"]  # blocked until that checkpoint is approved\n'
        "apply = upstream  # passthrough until translated\n"
        "apply"
    )
    header, placeholder = split_hole(code)

    assert "_gate_apply" in header
    assert "_gate_apply" not in placeholder
    assert "passthrough until translated" in placeholder
    # The marker is an instruction to a model, not documentation of the stage, so a filled cell
    # keeps the gate and the stage heading and loses the instruction.
    from kedge.notebook.scaffold import strip_marker

    kept = strip_marker(header)
    assert "_gate_apply" in kept
    assert "TODO(kedge)" not in kept
    assert "# Stage 4 of 8: apply" in kept


def test_cells_are_written_in_an_order_the_linear_driver_and_marimo_agree_on() -> None:
    """And the forward reference that forced it is reported, because marimo lays out in file order."""
    names = ("setup", "panel", "statement")
    codes = ("setup = 1", "panel = statement + setup", "statement = setup")

    layout = plan_layout(names, codes)

    assert [names[index] for index in layout.order] == ["setup", "statement", "panel"]
    assert layout.moved == ("panel",)
    assert [reference.render() for reference in layout.forward_references] == [
        "panel reads 'statement', which statement defines below it"
    ]


def test_a_layout_that_needed_no_rewrite_reports_nothing() -> None:
    """The note must not fire on a notebook that was already in order."""
    layout = plan_layout(("a", "b"), ("a = 1", "b = a"))

    assert layout.order == (0, 1)
    assert not layout.rewritten
    assert layout.forward_references == ()


def test_a_dependency_cycle_does_not_hang_the_ordering() -> None:
    names = ("one", "two")
    codes = ("one = two", "two = one")

    assert sorted(plan_layout(names, codes).order) == [0, 1]


# =============================================================================
# ANTI-FLATTERY CONTROLS
# =============================================================================


def test_a_scripted_action_never_binds_to_something_that_is_not_a_widget(
    reference_notebook: Path,
) -> None:
    """The bug that turned findings into no-ops, and it was invisible by construction.

    ``drive._apply`` binds only onto a ``Widget``. The old rule matched any defined name ending
    ``_<key>``, so ``query`` bound to ``extract_query`` -- a string -- the value went nowhere,
    ``unused_inputs`` had nothing to report because the name existed, and the run was recorded as
    faithfully driven with the scripted action discarded.
    """
    alignment = align_inputs(reference_notebook, ("query", "statement", "check", "values"))

    assert alignment.mapping == {}
    reasons = dict(alignment.unmatched)
    assert reasons.keys() == {"query", "statement", "check", "values"}
    # Three are refused because a name of that shape exists and is not a widget -- the case that
    # used to bind silently. `check` is refused for the other reason: it named `handin_check`, a
    # cell of the duplicated fixed head, and there is no such cell any more.
    for key in ("query", "statement", "values"):
        assert "not as an mo.ui element" in reasons[key], f"{key} was refused for the wrong reason"
    assert "no widget for it" in reasons["check"]
    assert "extract_query" in reasons["query"], "the report has to name what it refused to bind"
    assert "update_statement" in reasons["statement"]
    assert "reconciliation_values" in reasons["values"]


def test_an_alias_must_be_the_name_the_scaffolder_would_have_derived() -> None:
    """Ending in ``_<key>`` is not enough: it has to be *this* stage's widget.

    The stems come from the cell that defines the widget, with the scaffolder's own suffixes
    stripped. A scaffolder that invents a fourth suffix stops resolving aliases and the action is
    reported unplayed -- loudly wrong rather than quietly bound to a widget of another stage.
    """
    widgets = Widgets(
        stems=(
            ("extract_query_period_end", ("extract_query_inputs", "extract_query")),
            ("something_else_period_end", ("unrelated_cell",)),
        ),
        defined=frozenset({"extract_query_period_end", "something_else_period_end"}),
    )

    assert widgets.resolve("period_end") == ("extract_query_period_end", "")
    assert widgets.resolve("else_period_end")[0] is None
    assert widgets.resolve("nothing_like_this") == (None, "the notebook has no widget for it")


def test_two_widgets_that_could_be_meant_are_left_unmatched() -> None:
    """Picking one and saying nothing is how a script silently drives the wrong step."""
    widgets = Widgets(
        stems=(("a_note", ("a",)), ("b_note", ("b",))),
        defined=frozenset({"a_note", "b_note"}),
    )

    name, why = widgets.resolve("note")

    assert name is None
    assert "2 widgets could be meant" in why


def test_two_scripted_actions_that_claim_one_widget_bind_nothing() -> None:
    """Last write wins is how one step gets driven with another step's value, silently."""
    alignment = Alignment(
        widgets=Widgets(stems=(("stage_pick", ("stage",)),), defined=frozenset({"stage_pick"}))
    )

    inputs, unplayed = alignment.bind({"stage_pick": "a", "pick": "b"})

    assert inputs == {}
    assert unplayed == ("pick", "stage_pick")


def test_no_head_handin_is_donated_to_because_there_is_no_longer_one(
    reference_notebook: Path,
) -> None:
    """There is nothing left to donate to, and that is the fix rather than a hole in the test.

    The scaffolder used to emit a fixed head hand-in that no step of the process named, and the
    harness got past it by feeding it a stage's file. Choosing *which* stage was "the first script
    key ending ``_pick``" -- decided by a dict literal, so reordering ``script_for`` would have fed
    the post-adjustment grid into the head and computed every figure below on the wrong extract.

    Both orders are still checked. If a fixed head hand-in ever comes back, this test says the
    donor must not depend on the order the script happens to be written in.
    """
    forwards = align_inputs(reference_notebook, ("pre_adjustment_pick", "post_adjustment_pick"))
    backwards = align_inputs(reference_notebook, ("post_adjustment_pick", "pre_adjustment_pick"))

    assert forwards.head is None, "a head hand-in is being donated to; the duplicate is back"
    assert backwards.head == forwards.head


def test_a_paste_reaches_the_stage_that_asked_for_it(reference_notebook: Path) -> None:
    """A paste is a hand-in too. Feeding only the file selector strands the paste graders.

    It used to have to be re-keyed onto the fixed head's ``handin_paste``. Now the stage owns its
    own paste box and the script's key is already the right one -- so the assertion is that no
    re-keying happens, which is the stronger statement.
    """
    alignment = align_inputs(reference_notebook, ("pre_adjustment_paste",))
    inputs, unplayed = alignment.bind({"pre_adjustment_paste": "a,b\n1,2"})

    assert unplayed == ()
    assert inputs["pre_adjustment_paste"] == "a,b\n1,2"
    assert alignment.aliases == (), "the stage's own paste box needs no alias"


def test_align_inputs_resolves_the_scaffolders_prefixes_and_reports_what_it_could_not(
    reference_notebook: Path,
) -> None:
    alignment = align_inputs(
        reference_notebook,
        ("period_end", "pre_adjustment_pick", "update_statement_ran", "nothing_like_this"),
    )

    assert alignment.mapping["period_end"] == "extract_query_period_end"
    assert alignment.mapping["pre_adjustment_pick"] == "pre_adjustment_pick"
    # `update_statement_ran` resolves now: the UPDATE is declared mutating, so the confirmation
    # step and its widget exist. Only the invented key has nowhere to go.
    assert alignment.mapping["update_statement_ran"] == "update_statement_ran"
    assert set(alignment.unplayed) == {"nothing_like_this"}
    assert any("re-keyed" in note for note in alignment.notes())
    assert any("nothing_like_this" in note for note in alignment.notes())


def test_an_alignment_holds_nothing_mutable() -> None:
    """The house default is ``frozen=True, slots=True``; a frozen dataclass over a dict is neither."""
    assert hasattr(Alignment, "__slots__")
    assert hasattr(Widgets, "__slots__")


class _CaseWithAnExtraAction:
    """The eval case, with one scripted action the notebook has no step for.

    Needed because the real case no longer has one: every key it scripts now finds a widget. That
    is the right outcome and it makes the assertion below vacuous unless something is genuinely
    unplayable -- which is the exact defect this test was written for, so it is reproduced here
    rather than the channel being trusted on the strength of having carried something once.
    """

    def __init__(self, case: Any, key: str) -> None:
        self._case = case
        self._key = key

    def __getattr__(self, name: str) -> Any:
        return getattr(self._case, name)

    def script_for(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {**self._case.script_for(*args, **kwargs), self._key: "nobody asked for this"}


def test_a_driven_run_reports_the_actions_it_could_not_play(tmp_path: Path) -> None:
    """The second return value used to be structurally incapable of holding anything.

    ``Alignment.bind`` drops an unresolved key before ``unused_inputs`` can see it, so the old
    ``unused == ()`` assertion was asserting nothing at all. Both halves are checked: the real case
    now plays every action, and an invented one still comes back.
    """
    result = convert_with(ScriptedCompleter(list(REFERENCE_BODIES)))
    notebook = write_notebook(result, tmp_path / "converted.py")

    run, unplayed = driven_run(notebook, adjustment_case, tmp_path / "run")
    assert run.completed, run.summary_line()
    assert unplayed == (), "every scripted action now finds a widget"

    _, stranded = driven_run(
        notebook,
        _CaseWithAnExtraAction(adjustment_case, "no_such_step"),
        tmp_path / "run-with-extra",
    )
    assert stranded == ("no_such_step",), "an action with nowhere to go must still be reported"
    assert "signoff" in run.definitions


def test_a_grader_that_could_not_be_driven_is_named_rather_than_folded_into_skip() -> None:
    """``SKIP`` means "nobody's fault". A run that stopped in the head is somebody's.

    Five of this rubric's graders re-drive the notebook with scripts of their own, and the case
    stand-in could not reach them: four came back SKIP and one FAIL, ten points left the
    denominator, and the headline printed 75% for a run where a fifth of the rubric was never
    driven. This is the classification that refuses to let that be silent.
    """
    report = EvalReport(
        case="example",
        tiers=(
            TierResult(
                name="deterministic",
                items=(
                    ItemResult(id="fine", outcome=Outcome.PASS, weight=3),
                    ItemResult(id="undrivable", outcome=Outcome.SKIP, weight=3, detail="stopped"),
                    ItemResult(id="broken", outcome=Outcome.FAIL, weight=2, detail="wrong"),
                    ItemResult(id="no_duckdb", outcome=Outcome.SKIP, weight=1, detail="absent"),
                ),
            ),
        ),
    )
    log = ReDriveLog()
    log.drives.append(ReDrive(item="undrivable", unplayed=("period_end",), stopped_at="handin"))

    coverage = coverage_for(report, log)

    assert coverage.declared == 9
    assert coverage.graded == 5
    assert [item.id for item in coverage.undriven] == ["undrivable"]
    assert [item.id for item in coverage.ungradeable] == ["no_duckdb"]
    assert "NOT DRIVEN" in coverage.render()
    assert "period_end" in coverage.render()


def test_coverage_without_a_log_blames_nobody() -> None:
    """An unwatched run cannot attribute anything, and must not pretend to."""
    report = EvalReport(
        case="example",
        tiers=(
            TierResult(
                name="deterministic",
                items=(ItemResult(id="skipped", outcome=Outcome.SKIP, weight=2, detail="why"),),
            ),
        ),
    )

    coverage = coverage_for(report)

    assert coverage == Coverage(
        earned=0,
        graded=0,
        declared=2,
        undriven=(),
        ungradeable=coverage.ungradeable,
    )
    assert [item.id for item in coverage.ungradeable] == ["skipped"]


def test_the_conversion_eval_has_no_transport_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model arrives as an injected ``Completer``; nothing here may open a connection.

    Two guards, because the socket one alone was watching a path with no network in it. The
    import check is the one that would catch somebody reaching for ``httpx`` inside this module
    to "just call the endpoint directly", which is how an offline test suite acquires an
    endpoint.
    """
    import ast
    import socket

    import harness.align
    import harness.cellgen
    import harness.cellprompt
    import harness.convert
    import harness.findings
    import harness.render

    for module in (
        harness.cellprompt,
        harness.cellgen,
        harness.render,
        harness.align,
        harness.findings,
        harness.convert,
    ):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported = {
            (node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        } | {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        assert not imported & {"httpx", "requests", "urllib", "openai", "http"}, module.__name__

    def refuse(*args: Any, **kwargs: Any) -> None:
        msg = "the conversion eval opened a socket"
        raise AssertionError(msg)

    monkeypatch.setattr(socket.socket, "connect", refuse)
    result = convert_with(ScriptedCompleter(list(REFERENCE_BODIES)))

    assert result.filled == result.holes
