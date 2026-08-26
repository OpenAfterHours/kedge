"""The eval harness grades correctly, and can actually fail.

Two halves, and the second is the one that matters.

The first is the positive control: the reference conversion and the reference plan score full
marks. That proves the harness runs, that the graders read the right names, and that a good
notebook is recognised as one.

The second is a set of **negative controls**. A grader that has only ever been watched pass is a
grader whose polarity nobody has checked -- invert the comparison, key it to the wrong column,
compare a frame with itself, and it still reports green on the reference. So each defect this
eval exists to catch is injected into a copy of the reference notebook, one at a time, and the
test asserts that *that* item fails and that the injection did not knock over anything else.
The mutations are textual and deliberately small, because a mutation big enough to break the run
would prove nothing beyond "the notebook stopped".

The rubric is checked against the graders in both directions. An id in ``expected.yaml`` with no
grader is allowed at runtime -- a rubric may describe something nobody has automated -- but a
*grader* with no rubric entry is not, because that is a check whose reason was never written
down.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

EVAL_ROOT = Path(__file__).resolve().parents[2] / "evals"
if str(EVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(EVAL_ROOT))

from adjustment_signoff import case as adjustment_case  # noqa: E402
from harness.grade import grade, load_plan  # noqa: E402
from harness.model import Outcome  # noqa: E402

PLAN_PATH = adjustment_case.CASE_DIR / "plan.yaml"


@pytest.fixture(scope="module")
def rubric() -> dict[str, Any]:
    return yaml.safe_load(adjustment_case.RUBRIC.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def reference_report() -> Any:
    """One full run of the reference conversion, shared: it drives a whole notebook."""
    return grade(
        adjustment_case,
        notebook=adjustment_case.REFERENCE_NOTEBOOK,
        plan=load_plan(PLAN_PATH),
    )


def outcome_of(report: Any, item_id: str) -> Outcome:
    for tier in report.tiers:
        for item in tier.items:
            if item.id == item_id:
                return item.outcome
    msg = f"no item {item_id!r} in the report"
    raise AssertionError(msg)


def detail_of(report: Any, item_id: str) -> str:
    for tier in report.tiers:
        for item in tier.items:
            if item.id == item_id:
                return item.detail
    return ""


def mutated(tmp_path: Path, *replacements: tuple[str, str]) -> Path:
    """A copy of the reference notebook with one defect injected."""
    target = tmp_path / "mutated_notebook.py"
    source = adjustment_case.REFERENCE_NOTEBOOK.read_text(encoding="utf-8")
    for old, new in replacements:
        assert old in source, f"mutation anchor not found: {old!r}"
        source = source.replace(old, new)
    target.write_text(source, encoding="utf-8")
    shutil.copy(adjustment_case.WORKBOOK, tmp_path / adjustment_case.WORKBOOK.name)
    return target


def grade_mutant(notebook: Path) -> Any:
    return grade(adjustment_case, notebook=notebook, plan=load_plan(PLAN_PATH))


# ── the rubric and the graders agree ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("tier", "graders"), [("deterministic", "DETERMINISTIC"), ("structural", "STRUCTURAL")]
)
def test_every_grader_has_a_rubric_entry(rubric: dict[str, Any], tier: str, graders: str) -> None:
    """The direction that is not allowed to be loose: a check nobody wrote the reason for."""
    declared = {entry["id"] for entry in rubric[tier]}
    implemented = set(getattr(adjustment_case, graders))

    assert implemented <= declared, f"graders with no rubric entry: {implemented - declared}"


@pytest.mark.parametrize(
    ("tier", "graders"), [("deterministic", "DETERMINISTIC"), ("structural", "STRUCTURAL")]
)
def test_every_rubric_item_has_a_grader(rubric: dict[str, Any], tier: str, graders: str) -> None:
    declared = {entry["id"] for entry in rubric[tier]}
    implemented = set(getattr(adjustment_case, graders))

    assert declared <= implemented, f"rubric items with no grader: {declared - implemented}"


# ── positive control ─────────────────────────────────────────────────────────


def test_the_reference_conversion_scores_full_marks(reference_report: Any) -> None:
    """If this ever goes red, read the failures before touching a grader.

    It is the only assertion here that says the harness works at all -- everything else says a
    particular way of being wrong is caught.
    """
    assert reference_report.ok, "\n".join(
        f"{item.id}: {item.detail}" for item in reference_report.failures
    )
    assert reference_report.earned == reference_report.available
    assert reference_report.available > 30


def test_only_the_knowledge_pack_item_skips_on_the_reference(reference_report: Any) -> None:
    """Every other skip means an item silently stopped being measured."""
    assert [item.id for item in reference_report.skipped] == ["consults_the_knowledge_pack"]


def test_the_headline_shows_the_denominator(reference_report: Any) -> None:
    """A bare percentage over a partly-skipped rubric is the number this must not print."""
    headline = reference_report.headline()

    assert f"{reference_report.earned}/{reference_report.available}" in headline
    assert "1 skipped" in headline


# ── negative controls, one defect at a time ──────────────────────────────────


def test_a_bare_round_fails_the_penny_check(tmp_path: Path) -> None:
    """The four rows that need Excel's 15-significant-digit collapse, and nothing else."""
    report = grade_mutant(mutated(tmp_path, (".xl.round(2)", ".round(2)")))

    assert outcome_of(report, "totals_to_the_penny") is Outcome.FAIL
    assert "15-significant-digit" in detail_of(report, "totals_to_the_penny")
    assert outcome_of(report, "ran_to_completion") is Outcome.PASS


def test_letting_the_null_propagate_fails_the_null_check(tmp_path: Path) -> None:
    """polars' answer is null; Excel's is zero, and the workbook's cache holds zero."""
    report = grade_mutant(
        mutated(tmp_path, ('pl.col("accrual_gbp").xl.empty_as_zero()', 'pl.col("accrual_gbp")'))
    )

    assert outcome_of(report, "null_is_not_zero") is Outcome.FAIL
    assert "empty_as_zero" in detail_of(report, "null_is_not_zero")


def test_forgetting_the_cancelled_filter_fails_the_row_count(tmp_path: Path) -> None:
    # Anchored on `pre_adjustment.filter(` so only the `adjust` cell moves. The `verification`
    # cell restates the same predicate, and mutating both would move the goalposts as well as
    # the shot -- 80 rows predicted and 80 compared still verifies.
    scoped = (
        "pre_adjustment.filter(\n"
        '            pl.col("entity_id").is_in(ENTITIES_IN_SCOPE)'
        ' & (pl.col("status") != "CANCELLED")\n        )'
    )
    report = grade_mutant(
        mutated(
            tmp_path,
            (
                scoped,
                'pre_adjustment.filter(\n            pl.col("entity_id").is_in(ENTITIES_IN_SCOPE)'
                "\n        )",
            ),
        )
    )

    assert outcome_of(report, "cancelled_rows_excluded") is Outcome.FAIL
    assert "80" in detail_of(report, "cancelled_rows_excluded")


def test_concatenating_the_sql_fails_the_execution_check(tmp_path: Path) -> None:
    """The workbook's own approach, reproduced faithfully, and it does not parse.

    ``O'Brien & Partners`` is the row. This is the one place the eval rewards departing from
    Excel, so it is the one place a mutation has to prove the grader is not simply agreeing with
    whatever it is given.
    """
    naive = """update_statement = "\\n".join(
        "UPDATE fin.accruals SET accrual_gbp = %.2f, adjustment_note = '%s' WHERE trade_id = '%s';"
        % (row["accrual_gbp_after"], row["adjustment_note"], row["trade_id"])
        for row in _rows.iter_rows(named=True)
    )"""
    original_start = "    update_statement = kedge.sql.script("
    source = adjustment_case.REFERENCE_NOTEBOOK.read_text(encoding="utf-8")
    start = source.index(original_start)
    end = source.index("    mo.vstack(", start)
    target = tmp_path / "mutated_notebook.py"
    target.write_text(
        source[:start] + "    " + naive + "\n" + source[end:],
        encoding="utf-8",
    )

    report = grade_mutant(target)

    assert outcome_of(report, "generated_sql_is_valid") is Outcome.FAIL
    assert adjustment_case.APOSTROPHE_TRADE in detail_of(report, "generated_sql_is_valid")


def test_verifying_a_frame_against_itself_fails_the_verification_check(tmp_path: Path) -> None:
    """The dangerous direction: a green panel claiming production holds what the notebook says.

    Comparing the prediction with itself is the shape of every accidental self-verification, and
    it is exactly what a grader keyed to "did it pass" rather than "what did it find" would wave
    through.
    """
    report = grade_mutant(
        mutated(
            tmp_path,
            (
                "kedge.reconcile.verify(_predicted, _actual",
                "kedge.reconcile.verify(_predicted, _predicted",
            ),
        )
    )

    assert outcome_of(report, "verification_finds_exactly_one_break") is Outcome.FAIL
    assert "signed claim" in detail_of(report, "verification_finds_exactly_one_break")


def test_carrying_the_memos_stale_figures_forward_fails(tmp_path: Path) -> None:
    """The impact summary was signed and is wrong. Reproducing it reproduces that."""
    stale = yaml.safe_load(adjustment_case.RUBRIC.read_text(encoding="utf-8"))["facts"]
    report = grade_mutant(
        mutated(
            tmp_path,
            (
                '"movement": float(adjust_totals["uplift"][0]),',
                f'"movement": {stale["stale_movement"]},',
            ),
        )
    )

    assert outcome_of(report, "does_not_trust_the_impact_summary") is Outcome.FAIL
    assert "stale" in detail_of(report, "does_not_trust_the_impact_summary")


def test_a_notebook_that_stops_fails_completion_and_skips_the_rest(tmp_path: Path) -> None:
    """One problem reported once. The items about cells that never ran are skips, not failures."""
    target = tmp_path / "stops.py"
    source = adjustment_case.REFERENCE_NOTEBOOK.read_text(encoding="utf-8")
    target.write_text(
        source.replace(
            "pre_adjustment_pick = mo.ui.file_browser(", "unwatched_pick = mo.ui.file_browser("
        ),
        encoding="utf-8",
    )

    report = grade_mutant(target)

    assert outcome_of(report, "ran_to_completion") is Outcome.FAIL
    assert outcome_of(report, "totals_to_the_penny") is Outcome.SKIP
    assert outcome_of(report, "verification_finds_exactly_one_break") is Outcome.SKIP
    # The structural tier grades the plan, which is unaffected by the notebook stopping.
    assert outcome_of(report, "hands_over_rather_than_pretends") is Outcome.PASS


# ── negative controls on the plan ────────────────────────────────────────────


def test_dropping_the_sql_column_fails_the_sharpest_structural_item() -> None:
    """A plausible, well-reasoned, catastrophic edit: the analyser calls that region dead."""
    from kedge.plan.model import DroppedRange

    plan = load_plan(PLAN_PATH)
    dropped = plan.model_copy(
        update={
            "dropped": [
                *plan.dropped,
                DroppedRange(
                    range="Adjustment!G17:G92",
                    reason="dead region: nothing in the workbook reads it",
                    acknowledged=True,
                    accepted=True,
                ),
            ]
        }
    )

    report = grade(adjustment_case, notebook=adjustment_case.REFERENCE_NOTEBOOK, plan=dropped)

    assert outcome_of(report, "does_not_drop_the_sql_column") is Outcome.FAIL
    assert "clipboard" in detail_of(report, "does_not_drop_the_sql_column")


def test_a_plan_with_no_handoffs_fails_the_handover_item() -> None:
    plan = load_plan(PLAN_PATH)
    stripped = plan.model_copy(
        update={"stages": [stage for stage in plan.stages if not stage.is_handoff]}
    )

    report = grade(adjustment_case, notebook=adjustment_case.REFERENCE_NOTEBOOK, plan=stripped)

    assert outcome_of(report, "hands_over_rather_than_pretends") is Outcome.FAIL


def test_no_plan_skips_the_structural_tier_rather_than_failing_it() -> None:
    """A tier nobody supplied an input for is unmeasured, not failed."""
    report = grade(adjustment_case, notebook=adjustment_case.REFERENCE_NOTEBOOK, plan=None)

    structural = next(tier for tier in report.tiers if tier.name == "structural")
    assert all(item.outcome is not Outcome.FAIL for item in structural.items), (
        "a missing plan must not be reported as a failed conversion"
    )
    assert report.ok


# ── negative controls on the shape the scaffolder consumes ───────────────────
#
# The four items below used to grade *presence*, and a real model-written plan passed five of the
# structural tier's eight while scaffolding into a notebook with no briefing, no hand-off, a
# checkpoint below the update rather than above it, and one place to put a grid in a process that
# brings back two. Each mutation here is one of those defects, reproduced on the reference plan
# rather than invented -- so the polarity of the tightened graders is checked against the shape
# that actually got past the loose ones.


def restaged(plan: Any, changes: dict[str, dict[str, Any]]) -> Any:
    """The reference plan with named stages rebuilt through the schema's own validators.

    ``model_copy`` alone would skip them, and two of the mutations below turn on a validator
    running: retyping a stage as a ``checkpoint`` is only faithful if the confidence default
    moves with it, and ``depends_on`` is only faithful if the generated hand-off's ``built_from``
    edge is re-added the way a loaded plan would have it.
    """
    from kedge.plan.model import Stage

    stages = [
        Stage.model_validate({**stage.model_dump(mode="python"), **changes[stage.id]})
        if stage.id in changes
        else stage
        for stage in plan.stages
    ]
    return plan.model_copy(update={"stages": stages})


def _with_handoff(plan: Any, stage_id: str, **changes: Any) -> Any:
    """The reference plan with one stage's hand-off altered field by field.

    Built through ``Handoff`` rather than by hand so a mutation that the schema would refuse --
    both a statement and a template, say -- fails here rather than producing a plan no model could
    have written and a negative control that proves nothing about real inputs.
    """
    from kedge.plan.model import Handoff

    stage = next(item for item in plan.stages if item.id == stage_id)
    handoff = Handoff(**{**stage.effective_handoff().model_dump(mode="python"), **changes})
    return restaged(plan, {stage_id: {"handoff": handoff.model_dump(mode="python")}})


def graded_plan(plan: Any) -> Any:
    return grade(adjustment_case, notebook=adjustment_case.REFERENCE_NOTEBOOK, plan=plan)


def test_a_plan_with_no_briefing_fails_the_briefing_item() -> None:
    """The asymmetry the item exists for: an invented briefing is refused, an absent one was not.

    ``Briefing`` will not validate prose without citations, so a fabricated background cannot
    reach a plan at all. Nothing anywhere noticed one that never arrived -- and the notebook it
    scaffolds opens by telling its reader the workbook carried no description of what the process
    is for, which the Sign-off tab flatly contradicts.
    """
    report = graded_plan(load_plan(PLAN_PATH).model_copy(update={"briefing": None}))

    assert outcome_of(report, "the_briefing_survives_the_workbook") is Outcome.FAIL
    assert "Sign-off" in detail_of(report, "the_briefing_survives_the_workbook")
    # The notebook is the reference one, which has its own briefing written in. The plan-side
    # item and the notebook-side item are about the same loss at two different points, and only
    # the one whose input was mutated may move.
    assert outcome_of(report, "the_notebook_says_why_this_process_exists") is Outcome.PASS


def test_a_briefing_that_cites_nothing_fails_even_though_the_schema_allowed_it() -> None:
    """``cadence`` and ``audience`` are prose the validator does not ask for sources for.

    So there is a shape of briefing that is non-empty, valid, and unattributable, and the rubric
    has to catch it because the schema deliberately does not.
    """
    from kedge.plan.model import Briefing

    briefing = Briefing(cadence="Quarterly, after the reforecast", audience="Financial Control")
    assert not briefing.is_empty and not briefing.sources, "the schema stopped allowing this"

    report = graded_plan(load_plan(PLAN_PATH).model_copy(update={"briefing": briefing}))

    assert outcome_of(report, "the_briefing_survives_the_workbook") is Outcome.FAIL
    assert "cites nothing" in detail_of(report, "the_briefing_survives_the_workbook")


def test_a_briefing_with_no_background_fails_because_that_is_the_irrecoverable_half() -> None:
    from kedge.plan.model import Briefing

    briefing = Briefing(purpose="Records the quarterly uplift.", sources=["Sign-off!A3:A4"])

    report = graded_plan(load_plan(PLAN_PATH).model_copy(update={"briefing": briefing}))

    assert outcome_of(report, "the_briefing_survives_the_workbook") is Outcome.FAIL
    assert "background" in detail_of(report, "the_briefing_survives_the_workbook")


def test_declaring_the_update_read_only_fails_the_mutates_item() -> None:
    """``mutates`` is a claim; the statement is the fact, and the card renders the claim.

    The notebook now compensates -- ``needs_confirmation`` is ``mutates or statement_writes`` --
    which is precisely why the rubric has to say something. A contradiction the product silently
    survives is one nobody has to fix, and a reviewer reading "changes nothing" over an
    ``UPDATE fin.accruals`` is being told something false at the moment they are deciding.
    """
    report = graded_plan(_with_handoff(load_plan(PLAN_PATH), "update_statement", mutates=False))

    assert outcome_of(report, "mutates_agrees_with_the_statement") is Outcome.FAIL
    assert "changes_data" in detail_of(report, "mutates_agrees_with_the_statement")
    # The checkpoint item must not move: it reads the statement, never the flag, so a plan cannot
    # escape it by under-declaring.
    assert outcome_of(report, "has_a_checkpoint_before_the_update") is Outcome.PASS


def test_over_declaring_mutates_on_a_read_only_query_costs_nothing() -> None:
    """The direction the rubric explicitly accepts, and which the tier used to fail.

    ``_mutating_handoffs`` asked ``Handoff.needs_confirmation``, which is
    ``mutates or statement_writes`` -- so a plan declaring the read-only ``SELECT`` extract
    ``mutates: true`` was failed for wanting a checkpoint in front of it, in a message asserting
    the ``SELECT`` writes. Erring towards a tick-box is the safe direction, the sibling item
    accepts it in as many words, and the propose prompt nudges towards it.
    """
    plan = _with_handoff(load_plan(PLAN_PATH), "extract_query", mutates=True)

    report = graded_plan(plan)

    structural = next(tier for tier in report.tiers if tier.name == "structural")
    assert [item.id for item in structural.items if item.outcome is Outcome.FAIL] == []
    assert "safe direction" in detail_of(report, "mutates_agrees_with_the_statement")


def test_retyping_the_update_as_text_does_not_get_it_past_the_mutates_item() -> None:
    """One field, and the product's own check stops reading.

    ``Handoff.statement_writes`` returns ``False`` for anything that is not ``sql`` without
    reading a character of it -- right in the product, where ``text`` is a filename to request or
    a colleague to ask. Here it was a one-field escape: ``UPDATE fin.accruals SET ...`` under
    ``medium: text`` with ``mutates: false`` passed every item in the tier, and the approval card
    would have rendered "changes nothing" over a production write.
    """
    plan = _with_handoff(load_plan(PLAN_PATH), "update_statement", medium="text", mutates=False)

    report = graded_plan(plan)

    assert outcome_of(report, "mutates_agrees_with_the_statement") is Outcome.FAIL
    detail = detail_of(report, "mutates_agrees_with_the_statement")
    assert "medium: text" in detail
    assert "not a defence" in detail


def test_a_handoff_stage_that_supplies_no_statement_fails_rather_than_passing_vacuously() -> None:
    """Two marks for supplying nothing, which is how the item read before.

    ``effective_handoff()`` synthesises a ``-- TODO(kedge)`` placeholder for a stage that declares
    no hand-off, ``changes_data`` reads the placeholder as read-only, and so nothing contradicted
    anything. ``mutates`` over a hand-off with no statement is a claim about text that is not
    there.
    """
    plan = restaged(load_plan(PLAN_PATH), {"update_statement": {"handoff": None}})
    update = next(stage for stage in plan.stages if stage.id == "update_statement")
    assert update.is_handoff and update.handoff is None, "the mutation removed the stage instead"

    report = graded_plan(plan)

    assert outcome_of(report, "mutates_agrees_with_the_statement") is Outcome.FAIL
    assert "TODO(kedge)" in detail_of(report, "mutates_agrees_with_the_statement")


def test_two_stages_declaring_one_hand_in_ask_for_one_grid_twice() -> None:
    """Cells are not grids. The item's own check text claimed distinctness; nothing tested it."""
    plan = restaged(
        load_plan(PLAN_PATH),
        {"post_adjustment": {"sources": [{"origin": "handin", "ref": "pre-adjustment extract"}]}},
    )

    report = graded_plan(plan)

    assert outcome_of(report, "takes_two_handins") is Outcome.FAIL
    assert "one grid twice" in detail_of(report, "takes_two_handins")


def test_a_citation_that_points_nowhere_fails_the_briefing_item() -> None:
    """``Briefing`` asks only that ``sources`` be non-empty, so this validates and says nothing.

    The asymmetry the item exists for, one level down: an unattributable briefing is refused by
    the schema, an unfollowable attribution is not. A citation the next reader cannot follow is
    the appearance of attribution rather than attribution.
    """
    from kedge.plan.model import Briefing

    plan = load_plan(PLAN_PATH)
    unfollowable = Briefing(
        **{
            **plan.briefing.model_dump(mode="python"),
            "sources": ["nowhere in particular", "somebody told me"],
        }
    )
    assert unfollowable.sources, "the schema stopped accepting this"

    report = graded_plan(plan.model_copy(update={"briefing": unfollowable}))

    assert outcome_of(report, "the_briefing_survives_the_workbook") is Outcome.FAIL
    assert "nowhere in particular" in detail_of(report, "the_briefing_survives_the_workbook")


def test_a_checkpoint_below_the_update_fails_although_the_plan_still_has_one() -> None:
    """The observed defect: one checkpoint, in the plan, recorded after the write it is about."""
    plan = restaged(
        load_plan(PLAN_PATH),
        {
            "update_statement": {"depends_on": ["adjust"]},
            "approve_adjustment": {"depends_on": ["update_statement"]},
        },
    )
    assert any(stage.is_checkpoint for stage in plan.stages), "the mutation removed the checkpoint"

    report = graded_plan(plan)

    assert outcome_of(report, "has_a_checkpoint_before_the_update") is Outcome.FAIL
    assert "already happened" in detail_of(report, "has_a_checkpoint_before_the_update")


def test_a_checkpoint_gating_only_the_read_only_extract_fails() -> None:
    """The second hole in the presence version, and the one that would not look like a defect.

    The old grader asked whether *any* hand-off named a checkpoint in ``depends_on``. A plan that
    puts an approval in front of the extract query and nothing in front of the UPDATE satisfies
    that and hands over a production write with no decision behind it. The chain from the extract
    to the update is broken here deliberately, because with it intact the checkpoint really is
    upstream and the plan really is fine.
    """
    plan = restaged(
        load_plan(PLAN_PATH),
        {
            "approve_adjustment": {"depends_on": []},
            "extract_query": {"depends_on": ["approve_adjustment"]},
            "pre_adjustment": {"depends_on": []},
            "update_statement": {"depends_on": ["adjust"]},
        },
    )

    report = graded_plan(plan)

    assert outcome_of(report, "has_a_checkpoint_before_the_update") is Outcome.FAIL
    assert "update_statement" in detail_of(report, "has_a_checkpoint_before_the_update")


def test_a_checkpoint_two_stages_upstream_still_passes() -> None:
    """Reachability, not one edge. A correct plan must not be marked down for its decomposition.

    Approve the scope, then compute the uplift, then hand over the statement: a defensible
    decomposition in which the checkpoint is two edges above the UPDATE and never named in its
    ``depends_on``. A one-hop grader calls that ungated, which is the false-negative half of
    grading position rather than presence.
    """
    plan = restaged(
        load_plan(PLAN_PATH),
        {
            "approve_adjustment": {"depends_on": ["pre_adjustment"]},
            "adjust": {"depends_on": ["approve_adjustment"]},
            "update_statement": {"depends_on": ["adjust"]},
        },
    )
    update = next(stage for stage in plan.stages if stage.id == "update_statement")
    assert "approve_adjustment" not in update.depends_on, "the mutation left a direct edge"

    report = graded_plan(plan)

    assert outcome_of(report, "has_a_checkpoint_before_the_update") is Outcome.PASS


def test_a_re_extract_that_does_not_wait_for_the_update_fails_although_all_else_passes() -> None:
    """One edge, and the notebook invites the mistake nobody can detect afterwards.

    ``post_adjustment`` pointed at ``pre_adjustment`` instead of at ``update_statement``. Every
    other structural item still passes -- the hand-in is declared, emitted, distinct, mid-process;
    the checkpoint is upstream of the UPDATE; the briefing is intact -- and ``build_cells`` puts
    the re-extract selector seven cells *above* the statement with no gate token in it. Replaying
    the reference cell bodies through that plan scored exactly what the correct plan scored, which
    is the whole reason this item exists: the rubric could not tell a correct runbook from one
    that puts the re-extract box on screen before the UPDATE.
    """
    plan = restaged(load_plan(PLAN_PATH), {"post_adjustment": {"depends_on": ["pre_adjustment"]}})

    report = graded_plan(plan)

    assert outcome_of(report, "the_re_extract_waits_for_the_update") is Outcome.FAIL
    detail = detail_of(report, "the_re_extract_waits_for_the_update")
    assert "before the UPDATE" in detail
    # The point of the item, asserted as well as its own verdict: everything else is still green,
    # so nothing but this would have caught the plan.
    others = {
        item.id: item.outcome
        for tier in report.tiers
        if tier.name == "structural"
        for item in tier.items
        if item.id != "the_re_extract_waits_for_the_update"
    }
    assert Outcome.FAIL not in others.values(), others


def test_a_hand_in_below_the_update_that_reads_no_token_also_fails() -> None:
    """Position in the file is not the gate. Reading the token is.

    marimo hides a cell on a dataflow edge and nothing else, so a selector that constructs
    ``mo.ui`` elements and references no upstream name renders immediately however far down the
    file it sits. A grader that checked only emission order would call this notebook correct, and
    the box would still be on screen from the moment it opened.

    The shape is an ordinary one: a step between the update and the re-extract. ``post_adjustment``
    then depends on *that* rather than on the hand-off, ``_gate_tokens`` reads ``depends_on`` and
    finds nothing gated in it, and the selector is emitted below the UPDATE carrying no token.
    """
    from kedge.plan.model import Stage

    plan = load_plan(PLAN_PATH)
    interposed = Stage.model_validate(
        {
            "id": "note_the_update",
            "intent": "Record that the statement was handed over",
            "kind": "transform",
            "depends_on": ["update_statement"],
            "sources": [{"origin": "stage", "ref": "adjust"}],
        }
    )
    plan = plan.model_copy(update={"stages": [*plan.stages, interposed]})
    plan = restaged(plan, {"post_adjustment": {"depends_on": ["note_the_update"]}})

    report = graded_plan(plan)

    assert outcome_of(report, "the_re_extract_waits_for_the_update") is Outcome.FAIL
    detail = detail_of(report, "the_re_extract_waits_for_the_update")
    assert "no dataflow edge" in detail
    assert "position in the file is not the gate" in detail.lower()


def test_a_re_extract_declared_on_a_checkpoint_fails_the_two_handins_item() -> None:
    """The sharpest of the four, because the loose version was green on exactly this plan.

    ``build_cells`` returns early for a ``checkpoint`` stage before ``_named_handin`` is
    consulted, so the hand-in is read by the schema, rendered on the approval card, and emitted
    as nothing. The plan still declares two hand-ins; the notebook has one place to put a grid.
    """
    plan = restaged(load_plan(PLAN_PATH), {"post_adjustment": {"kind": "checkpoint"}})
    handins = [
        source
        for stage in plan.stages
        for source in stage.sources
        if source.origin.value == "handin" and source.ref
    ]
    assert len(handins) == 2, "the mutation removed a declared hand-in rather than stranding it"

    report = graded_plan(plan)

    assert outcome_of(report, "takes_two_handins") is Outcome.FAIL
    assert "nowhere to arrive" in detail_of(report, "takes_two_handins")


def test_a_plan_that_declares_no_handin_of_its_own_still_fails_for_the_old_reason() -> None:
    """The original check survives the tightening: a re-extract cannot be the head hand-in."""
    plan = load_plan(PLAN_PATH)
    stripped = restaged(
        plan,
        {stage.id: {"sources": []} for stage in plan.stages if stage.id.endswith("_adjustment")},
    )

    report = graded_plan(stripped)

    assert outcome_of(report, "takes_two_handins") is Outcome.FAIL
    assert "head hand-in" in detail_of(report, "takes_two_handins")


def test_the_plan_that_got_past_the_loose_tier_does_not_get_past_this_one() -> None:
    """The real artifact, not a mutation of the reference: one model-written plan, verbatim.

    ``tests/unit/observed_conversion.py`` carries the plan a model actually proposed through the
    hub and a user actually approved. It is a better input than anything a test author would
    invent -- the mistyped ``kind`` on the re-extract is not a mistake anyone writes on purpose,
    and it is the one that cost the notebook its second hand-in silently.

    Graded the way a sweep grades a plan -- :func:`harness.sweep.grade_structural`, no notebook --
    because that is the only honest measurement of a *plan*. Grading it beside the reference
    conversion credited it with ``does_not_trust_the_impact_summary``, which reads a driven
    notebook this plan had no hand in writing: two points it could neither earn nor lose, and a
    published figure of "7 of 21" that was two points and one denominator wrong.

    The item-by-item outcome is asserted as well as the total, because the total moves whenever a
    weight does and what is pinned here is *which* items see the defect. Three are worth reading
    twice. ``takes_two_handins`` was **green** on this plan while the notebook it scaffolded had
    one place to put a grid. And two items **skip** rather than failing: a plan that hands nothing
    over has no statement for ``mutates`` to contradict and no update for a re-extract to wait
    for, so reporting either as a failure would name the wrong defect.
    ``hands_over_rather_than_pretends`` is the item about that, and it is red.
    """
    from harness.sweep import Bench, grade_structural
    from observed_conversion import observed_plan

    tier = grade_structural(Bench.load(), observed_plan())
    outcomes = {item.id: item.outcome for item in tier.items}

    assert outcomes == {
        "hands_over_rather_than_pretends": Outcome.FAIL,
        "takes_two_handins": Outcome.FAIL,
        "generates_the_update_from_the_frame": Outcome.FAIL,
        "does_not_drop_the_sql_column": Outcome.PASS,
        "raises_the_memo_discrepancy": Outcome.PASS,
        "does_not_trust_the_impact_summary": Outcome.SKIP,
        "has_a_checkpoint_before_the_update": Outcome.FAIL,
        "the_re_extract_waits_for_the_update": Outcome.SKIP,
        "mutates_agrees_with_the_statement": Outcome.SKIP,
        "the_briefing_survives_the_workbook": Outcome.FAIL,
        "consults_the_knowledge_pack": Outcome.SKIP,
    }, tier.render()
    assert (tier.earned, tier.available) == (5, 19), tier.render()
    details = {item.id: item.detail for item in tier.items}
    assert "nowhere to arrive" in details["takes_two_handins"]
    assert "Sign-off" in details["the_briefing_survives_the_workbook"]
