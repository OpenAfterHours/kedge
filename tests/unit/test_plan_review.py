"""The approval gate: edits reset it, drops and questions gate it, and the diff is the record.

PLAN 2.2 makes this module load-bearing — "nothing is written to the notebook before this is
approved" — and the enforcement is structural rather than conventional. Two properties carry the
whole guarantee and are asserted here from several directions:

- **Every content edit resets approval to DRAFT.** An approval granted against one decomposition
  must not survive a change to it, or the gate protects nothing.
- **Every edit produces a new version.** That is what makes "the diff of the plan is the change
  record" true when the process changes next quarter.
"""

from __future__ import annotations

import pytest

from conftest import make_analysis, make_draft, make_operation, make_plan
from kedge.analysis.model import ExcelPattern
from kedge.plan.model import (
    ApprovalState,
    Assessment,
    Checkpoint,
    Confidence,
    DroppedRange,
    OpenQuestion,
    PlanError,
    ProcessPlan,
    SourceOrigin,
    Stage,
    StageKind,
    StageSource,
)
from kedge.plan.review import (
    PlanNotApprovableError,
    acknowledge_all_drops,
    acknowledge_drop,
    add_question,
    add_stage,
    answer_question,
    approve,
    diff_plans,
    edit_stage,
    merge_stages,
    reject,
    remove_stage,
    render_diff,
    render_plan,
    reorder_stages,
    request_changes,
    review_warnings,
    split_stage,
)
from kedge.plan.triage import TriageResult, TriageVerdict, triage

# ── helpers ─────────────────────────────────────────────────────────────────


def _approved(plan: ProcessPlan) -> ProcessPlan:
    """The plan with every drop acknowledged and approval granted."""
    return approve(acknowledge_all_drops(plan, note="read and agreed"), by="phil")


def _clean_plan(**draft_overrides) -> ProcessPlan:
    """A plan with no drops, so approval is not blocked by the shared fixture's one drop."""
    return make_plan(draft=make_draft(dropped=[], **draft_overrides))


# =============================================================================
# EVERY EDIT IS A NEW VERSION, AND EVERY EDIT RESETS APPROVAL
# =============================================================================


def test_an_edit_to_an_approved_plan_resets_it_to_draft() -> None:
    """The single most important behaviour in this module (review.py's own docstring)."""
    approved = _approved(_clean_plan())
    assert approved.approval.state is ApprovalState.APPROVED

    edited = edit_stage(approved, "apply_haircuts", intent="Haircut lookup, corrected")

    assert edited.approval.state is ApprovalState.DRAFT
    assert edited.approval.by is None
    assert edited.approval.at is None


def test_an_edit_to_a_rejected_plan_carries_the_rejection_forward() -> None:
    """A rejection is not a state an unrelated edit may clear.

    `reject` says a rejected plan can never be approved and a new one must be proposed. If
    `_revise` reset the state to DRAFT, adding a sentence to a rejected decomposition would erase
    the only record that it was turned down and re-arm `approve` against the whole of it.
    """
    rejected = reject(_clean_plan(), by="phil", reason="the decomposition is wrong")

    amended = add_question(rejected, "should the FX rate source be named?")

    assert amended.approval.state is ApprovalState.REJECTED
    assert amended.approval.by == "phil", "who rejected it, and why, survives the edit"
    assert amended.approval.note == "the decomposition is wrong"
    assert amended.version == rejected.version + 1


def test_a_rejection_cannot_be_laundered_into_an_approval_by_an_unrelated_edit() -> None:
    """The invariant behind F1: one approved sentence must not put a rejected plan into force."""
    rejected = reject(_clean_plan(), by="phil", reason="split the haircut stage first")

    amended = edit_stage(rejected, "apply_haircuts", notes="Approved amendment: name the source")

    assert any("rejected" in blocker for blocker in amended.approval_blockers())
    with pytest.raises(PlanNotApprovableError, match="propose a new one"):
        approve(amended, by="phil")


def test_a_plan_proposed_afresh_from_a_rejected_one_starts_clean() -> None:
    """The escape hatch: propose a new plan rather than editing the rejected one into force."""
    rejected = reject(_clean_plan(), by="phil", reason="wrong shape")

    fresh = ProcessPlan.from_draft(
        rejected.to_draft(), workbook=rejected.workbook, workbook_sha256=rejected.workbook_sha256
    )

    assert fresh.approval.state is ApprovalState.DRAFT
    assert approve(fresh, by="phil").approval.approved


def test_an_edit_bumps_the_version_and_records_what_it_came_from() -> None:
    plan = _clean_plan()
    edited = edit_stage(plan, "apply_haircuts", notes="checked against the Ref sheet")
    assert edited.version == plan.version + 1
    assert edited.based_on_version == plan.version


def test_a_human_edit_is_attributed_to_a_human_not_to_the_model() -> None:
    plan = _clean_plan()
    assert plan.generated_by == "llm"
    assert edit_stage(plan, "apply_haircuts", notes="x").generated_by == "human"


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(lambda p: edit_stage(p, "apply_haircuts", notes="x"), id="edit_stage"),
        pytest.param(lambda p: reorder_stages(p, list(reversed(p.stage_ids))), id="reorder_stages"),
        pytest.param(
            lambda p: add_stage(p, Stage(id="extra", intent="something new")), id="add_stage"
        ),
        pytest.param(lambda p: remove_stage(p, "write_output"), id="remove_stage"),
        pytest.param(
            lambda p: split_stage(
                p,
                "apply_haircuts",
                [Stage(id="lookup", intent="join"), Stage(id="apply", intent="multiply")],
            ),
            id="split_stage",
        ),
        pytest.param(
            lambda p: merge_stages(p, ["load_handin", "apply_haircuts"], into_id="load_and_apply"),
            id="merge_stages",
        ),
        pytest.param(lambda p: add_question(p, "Is column AF dead?"), id="add_question"),
        pytest.param(
            lambda p: answer_question(p, 0, "Dead since the 2023 migration."), id="answer_question"
        ),
    ],
)
def test_every_content_edit_resets_approval_and_bumps_the_version(edit) -> None:
    """Enumerated deliberately: a new edit function that forgets `_revise` fails here."""
    approved = _approved(_clean_plan())
    revised = edit(approved)
    assert revised.approval.state is ApprovalState.DRAFT
    assert revised.version == approved.version + 1


def test_revalidation_runs_on_every_edit_so_a_broken_graph_fails_here() -> None:
    """Not in the scaffolder, where it would be found after code had been written."""
    plan = _clean_plan()
    with pytest.raises(ValueError, match="unknown stage"):
        edit_stage(plan, "apply_haircuts", depends_on=["a_stage_that_does_not_exist"])


# =============================================================================
# STAGE EDITS
# =============================================================================


def test_reordering_keeps_every_stage_and_changes_only_the_order() -> None:
    plan = _clean_plan()
    order = ["load_handin", "apply_haircuts", "write_output", "manual_overrides"]
    reordered = reorder_stages(plan, order)
    assert reordered.stage_ids == order


def test_reordering_must_list_every_stage_exactly_once() -> None:
    plan = _clean_plan()
    with pytest.raises(PlanError) as caught:
        reorder_stages(plan, ["load_handin", "apply_haircuts"])
    assert "missing:" in str(caught.value)
    assert "manual_overrides" in str(caught.value)


def test_reordering_rejects_an_id_the_plan_does_not_have() -> None:
    with pytest.raises(PlanError, match="unknown:"):
        reorder_stages(_clean_plan(), ["load_handin", "apply_haircuts", "ghost", "write_output"])


def test_editing_a_stage_replaces_only_the_named_fields() -> None:
    plan = _clean_plan()
    edited = edit_stage(plan, "apply_haircuts", confidence=Confidence.MEDIUM)
    stage = edited.stage("apply_haircuts")
    assert stage is not None
    assert stage.confidence is Confidence.MEDIUM
    assert stage.operations == ["calc_h2_h500"]


def test_renaming_a_stage_repoints_everything_that_depended_on_it() -> None:
    """A rename that left dangling dependencies would fail revalidation, not silently pass."""
    renamed = edit_stage(_clean_plan(), "apply_haircuts", id="apply_collateral_haircuts")
    assert "apply_haircuts" not in renamed.stage_ids
    downstream = renamed.stage("write_output")
    assert downstream is not None
    assert "apply_collateral_haircuts" in downstream.depends_on


def test_renaming_a_stage_repoints_the_sources_that_read_it_as_well() -> None:
    """Sources travel with dependencies: since 1.1 both name a stage and both are validated, so a
    rename that rewrote one and not the other would fail on the reviewer's own edit."""
    plan = _clean_plan(
        stages=[
            Stage(id="load_handin", intent="Read the hand-in", kind=StageKind.LOAD),
            Stage(
                id="apply_haircuts",
                intent="Look up haircuts",
                sources=[StageSource(origin=SourceOrigin.STAGE, ref="load_handin")],
                depends_on=["load_handin"],
            ),
        ]
    )

    renamed = edit_stage(plan, "load_handin", id="read_exposures")

    downstream = renamed.stage("apply_haircuts")
    assert downstream is not None
    assert downstream.upstream_stage_ids == ["read_exposures"]


def test_editing_a_stage_reads_a_bare_source_with_the_rest_of_the_plan_in_hand() -> None:
    """The same text in YAML gives `stage`; the edit verb has no excuse for giving `unknown`.

    `Stage.model_validate` alone cannot tell an upstream stage id from a named range, and
    `edit_stage` holds the plan that can — so it goes through `Stage.validate_in_plan`. Left
    asymmetric, an edit expressed the way the CLI documents would quietly weaken the graph.
    """
    plan = _clean_plan(
        stages=[
            Stage(id="load_handin", intent="Read the hand-in", kind=StageKind.LOAD),
            Stage(id="apply_haircuts", intent="Look up haircuts", depends_on=["load_handin"]),
        ]
    )

    edited = edit_stage(plan, "apply_haircuts", sources=["load_handin"])

    stage = edited.stage("apply_haircuts")
    assert stage is not None
    assert stage.sources == [StageSource(origin=SourceOrigin.STAGE, ref="load_handin")]
    assert stage.upstream_stage_ids == ["load_handin"]


def test_renaming_a_stage_whose_id_looks_like_a_cell_still_repoints_what_reads_it() -> None:
    """The consequence of reading `q1` as a range: nothing follows the rename, and the source
    ends up pointing at a stage that is no longer in the plan."""
    plan = ProcessPlan.model_validate(
        {
            **make_plan().model_dump(mode="python"),
            "stages": [
                {"id": "q1", "intent": "Quarterly figures", "kind": "load"},
                {
                    "id": "apply_haircuts",
                    "intent": "Look up haircuts",
                    "sources": ["q1"],
                    "depends_on": ["q1"],
                },
            ],
        }
    )
    assert plan.stages[1].upstream_stage_ids == ["q1"]

    renamed = edit_stage(plan, "q1", id="quarter_one")

    downstream = renamed.stage("apply_haircuts")
    assert downstream is not None
    assert downstream.upstream_stage_ids == ["quarter_one"]


def test_editing_an_unknown_stage_lists_the_ones_that_exist() -> None:
    with pytest.raises(PlanError) as caught:
        edit_stage(_clean_plan(), "ghost", notes="x")
    assert "apply_haircuts" in str(caught.value)


def test_a_stage_can_be_appended_or_inserted_after_a_named_one() -> None:
    plan = _clean_plan()
    appended = add_stage(plan, Stage(id="archive", intent="Copy the output to the archive"))
    assert appended.stage_ids[-1] == "archive"

    inserted = add_stage(
        plan, Stage(id="sanity_check", intent="Eyeball the totals"), after="load_handin"
    )
    assert inserted.stage_ids[1] == "sanity_check"


def test_adding_a_stage_that_already_exists_is_refused() -> None:
    with pytest.raises(PlanError, match="already exists"):
        add_stage(_clean_plan(), Stage(id="load_handin", intent="again"))


def test_inserting_after_a_stage_that_does_not_exist_is_refused() -> None:
    with pytest.raises(PlanError, match="no stage 'ghost'"):
        add_stage(_clean_plan(), Stage(id="new", intent="x"), after="ghost")


def test_removing_a_stage_detaches_dependencies_rather_than_guessing_a_replacement() -> None:
    """Guessing which upstream should take its place is exactly the silent decision this whole
    module exists to prevent."""
    without = remove_stage(_clean_plan(), "apply_haircuts")
    assert "apply_haircuts" not in without.stage_ids
    downstream = without.stage("write_output")
    assert downstream is not None
    assert downstream.depends_on == ["manual_overrides"]


def test_removing_a_stage_detaches_the_sources_that_read_it_too() -> None:
    """A source pointing at a stage that is no longer in the plan would not validate."""
    plan = _clean_plan(
        stages=[
            Stage(id="load_handin", intent="Read the hand-in", kind=StageKind.LOAD),
            Stage(
                id="apply_haircuts",
                intent="Look up haircuts",
                sources=[
                    StageSource(origin=SourceOrigin.STAGE, ref="load_handin"),
                    StageSource(origin=SourceOrigin.RANGE, ref="Ref!A1:D50"),
                ],
                depends_on=["load_handin"],
            ),
        ]
    )

    without = remove_stage(plan, "load_handin")

    survivor = without.stage("apply_haircuts")
    assert survivor is not None
    assert survivor.sources == [StageSource(origin=SourceOrigin.RANGE, ref="Ref!A1:D50")]


def test_removing_the_last_stage_is_not_a_review_action() -> None:
    single = make_plan(
        draft=make_draft(stages=[Stage(id="only", intent="the whole process")], dropped=[])
    )
    with pytest.raises(PlanError, match="at least one stage"):
        remove_stage(single, "only")


def test_splitting_chains_the_parts_and_rewires_both_ends() -> None:
    split = split_stage(
        _clean_plan(),
        "apply_haircuts",
        [
            Stage(id="join_haircuts", intent="Left join the haircut table"),
            Stage(id="apply_haircut_factor", intent="Multiply notional by 1 - haircut"),
        ],
    )
    first = split.stage("join_haircuts")
    second = split.stage("apply_haircut_factor")
    downstream = split.stage("write_output")
    assert first is not None and second is not None and downstream is not None

    assert first.depends_on == ["load_handin"]
    assert second.depends_on == ["join_haircuts"]
    assert "apply_haircut_factor" in downstream.depends_on
    assert split.stage_ids.index("join_haircuts") == 1


def test_splitting_needs_at_least_two_parts() -> None:
    with pytest.raises(PlanError, match="at least two parts"):
        split_stage(_clean_plan(), "apply_haircuts", [Stage(id="one", intent="x")])


def test_merging_unions_the_content_and_takes_the_lowest_confidence() -> None:
    """A merged step is only as trustworthy as its weakest part."""
    plan = make_plan(
        draft=make_draft(
            stages=[
                Stage(
                    id="first",
                    intent="Read the hand-in",
                    kind=StageKind.LOAD,
                    sources=["handin"],
                    confidence=Confidence.HIGH,
                    assumptions=["header on row 1"],
                    operations=["op_a"],
                    excel_pattern=ExcelPattern.LITERAL,
                    notes="from the shared drive",
                ),
                Stage(
                    id="second",
                    intent="Coerce the dtypes",
                    kind=StageKind.TRANSFORM,
                    sources=[
                        StageSource(origin=SourceOrigin.STAGE, ref="first"),
                        StageSource(origin=SourceOrigin.HANDIN),
                    ],
                    depends_on=["first"],
                    confidence=Confidence.LOW,
                    assumptions=["amounts are text"],
                    operations=["op_b"],
                ),
                Stage(id="third", intent="Report", depends_on=["second"]),
            ],
            dropped=[],
        )
    )

    merged = merge_stages(plan, ["first", "second"], into_id="load_and_clean")
    combined = merged.stage("load_and_clean")
    third = merged.stage("third")
    assert combined is not None and third is not None

    # The hand-in survives; `second` reading `first` does not, because after the merge those two
    # are one stage and an edge inside a stage is not an input.
    assert combined.sources == [StageSource(origin=SourceOrigin.HANDIN)]
    assert combined.assumptions == ["header on row 1", "amounts are text"]
    assert combined.operations == ["op_a", "op_b"]
    assert combined.confidence is Confidence.LOW
    assert combined.kind is StageKind.LOAD
    assert combined.notes == "from the shared drive"
    assert third.depends_on == ["load_and_clean"]
    assert merged.stage_ids == ["load_and_clean", "third"]


def test_a_source_a_stage_legitimately_lists_twice_survives_an_unrelated_merge() -> None:
    """Repointing de-duplicates the edges *it* collapsed, and nothing else.

    A merge genuinely turns two edges into one and the second is then noise. A stage that already
    listed the same range twice said so deliberately — under 1.0 repeating a bare string was how
    you said "twice" — and a merge somewhere else in the plan is no reason to edit it.
    """
    twice = [
        StageSource(origin=SourceOrigin.RANGE, ref="Calc!H2:H500"),
        StageSource(origin=SourceOrigin.RANGE, ref="Calc!H2:H500"),
    ]
    plan = make_plan(
        draft=make_draft(
            stages=[
                Stage(id="first", intent="Read the hand-in", kind=StageKind.LOAD),
                Stage(id="second", intent="Clean it", depends_on=["first"]),
                Stage(id="bystander", intent="Reads the same block twice", sources=twice),
            ],
            dropped=[],
        )
    )

    merged = merge_stages(plan, ["first", "second"], into_id="load_and_clean")

    bystander = merged.stage("bystander")
    assert bystander is not None
    assert bystander.sources == twice


def test_a_merge_still_collapses_the_two_edges_it_turned_into_one() -> None:
    """The other half of the bargain: the de-duplication the merge does need is still there."""
    plan = make_plan(
        draft=make_draft(
            stages=[
                Stage(id="first", intent="Read the hand-in", kind=StageKind.LOAD),
                Stage(id="second", intent="Clean it", depends_on=["first"]),
                Stage(
                    id="downstream",
                    intent="Reads both",
                    sources=[
                        StageSource(origin=SourceOrigin.STAGE, ref="first"),
                        StageSource(origin=SourceOrigin.STAGE, ref="second"),
                    ],
                    depends_on=["first", "second"],
                ),
            ],
            dropped=[],
        )
    )

    merged = merge_stages(plan, ["first", "second"], into_id="load_and_clean")

    downstream = merged.stage("downstream")
    assert downstream is not None
    assert downstream.upstream_stage_ids == ["load_and_clean"]


def test_merging_accepts_an_explicit_intent_for_the_combined_stage() -> None:
    merged = merge_stages(
        _clean_plan(),
        ["load_handin", "apply_haircuts"],
        into_id="load_and_apply",
        intent="Read the hand-in and apply collateral haircuts",
    )
    stage = merged.stage("load_and_apply")
    assert stage is not None
    assert stage.intent == "Read the hand-in and apply collateral haircuts"


def test_merging_without_an_intent_joins_the_originals() -> None:
    merged = merge_stages(_clean_plan(), ["load_handin", "apply_haircuts"], into_id="both")
    stage = merged.stage("both")
    assert stage is not None
    assert "Read counterparty exposures from the hand-in" in stage.intent
    assert "Collateral haircut lookup by asset class" in stage.intent


def test_merging_a_checkpoint_into_automated_stages_is_refused() -> None:
    """The control would disappear, which is the opposite of what a checkpoint is for."""
    with pytest.raises(PlanError, match="the control would disappear"):
        merge_stages(_clean_plan(), ["apply_haircuts", "manual_overrides"], into_id="both")


def test_merging_two_checkpoints_keeps_the_first_checkpoint_spec() -> None:
    plan = make_plan(
        draft=make_draft(
            stages=[
                Stage(
                    id="sign_off_a",
                    intent="Risk sign-off",
                    kind=StageKind.CHECKPOINT,
                    checkpoint=Checkpoint(question="Agreed with Risk?"),
                ),
                Stage(id="sign_off_b", intent="Finance sign-off", kind=StageKind.CHECKPOINT),
            ],
            dropped=[],
        )
    )
    merged = merge_stages(plan, ["sign_off_a", "sign_off_b"], into_id="sign_off")
    stage = merged.stage("sign_off")
    assert stage is not None
    assert stage.effective_checkpoint().question == "Agreed with Risk?"


def test_merging_needs_at_least_two_stages() -> None:
    with pytest.raises(PlanError, match="at least two stages"):
        merge_stages(_clean_plan(), ["load_handin"], into_id="one")


# =============================================================================
# QUESTIONS
# =============================================================================


def test_a_question_can_be_answered_by_index_or_by_its_text() -> None:
    plan = _clean_plan()
    text = plan.open_questions[0].question

    by_index = answer_question(plan, 0, "Dead since the 2023 migration.")
    by_text = answer_question(plan, text, "Dead since the 2023 migration.")

    assert by_index.open_questions[0].answered
    assert by_index.open_questions[0].answered_at is not None
    assert by_text.open_questions[0].answer == by_index.open_questions[0].answer


def test_answering_a_question_that_is_not_there_says_how_many_there_are() -> None:
    with pytest.raises(PlanError, match="the plan has 1"):
        answer_question(_clean_plan(), 7, "x")


def test_answering_by_text_that_matches_nothing_quotes_what_was_asked_for() -> None:
    with pytest.raises(PlanError, match="no open question matching"):
        answer_question(_clean_plan(), "a question nobody asked", "x")


def test_a_reviewer_can_add_a_question_the_model_did_not_ask() -> None:
    added = add_question(
        _clean_plan(), "Where does the FX rate come from?", context="Params!B2 is a literal"
    )
    assert added.open_questions[-1].question == "Where does the FX rate come from?"
    assert added.open_questions[-1].context == "Params!B2 is a literal"
    assert not added.open_questions[-1].answered


# =============================================================================
# DROPS
# =============================================================================


def test_confirming_a_drop_records_who_and_when_and_unblocks_approval() -> None:
    plan = make_plan()
    assert not plan.is_approvable

    acknowledged = acknowledge_drop(plan, "Calc!AK:AP", note="checked, dead since 2023")

    drop = acknowledged.dropped[0]
    assert drop.acknowledged and drop.accepted
    assert drop.acknowledged_at is not None
    assert drop.note == "checked, dead since 2023"
    assert acknowledged.is_approvable


def test_rejecting_a_drop_raises_a_question_and_keeps_approval_blocked() -> None:
    """Silently accepting a rejection would be exactly the quiet hole this gate exists to stop."""
    rejected = acknowledge_drop(
        make_plan(), "Calc!AK:AP", accepted=False, note="Finance still reads it"
    )

    assert rejected.dropped[0].rejected
    assert any("must be kept" in question.question for question in rejected.open_questions)
    assert not rejected.is_approvable
    assert any("no stage lists it as a source" in b for b in rejected.approval_blockers())


def test_a_rejected_drop_stops_blocking_once_a_stage_claims_the_range() -> None:
    rejected = acknowledge_drop(make_plan(), "Calc!AK:AP", accepted=False)
    claimed = edit_stage(
        rejected, "apply_haircuts", sources=["Calc!H2:H500", "Ref!A1:D50", "Calc!AK:AP"]
    )
    assert claimed.is_approvable


def test_acknowledging_a_range_the_plan_never_proposed_dropping_lists_the_ones_it_did() -> None:
    with pytest.raises(PlanError) as caught:
        acknowledge_drop(make_plan(), "Calc!ZZ:ZZ")
    assert "Calc!AK:AP" in str(caught.value)


def test_acknowledging_them_all_at_once_leaves_the_same_audit_trail() -> None:
    plan = make_plan(
        draft=make_draft(
            dropped=[
                DroppedRange(range="Calc!AK:AP", reason="no downstream refs since 2023"),
                DroppedRange(range="Calc!BB:BD", reason="superseded by the Ref sheet"),
            ]
        )
    )
    acknowledged = acknowledge_all_drops(plan, note="reviewed the whole list")
    assert all(drop.acknowledged and drop.accepted for drop in acknowledged.dropped)
    assert all(drop.note == "reviewed the whole list" for drop in acknowledged.dropped)
    assert acknowledged.is_approvable


def test_acknowledging_all_drops_when_there_is_nothing_outstanding_is_a_no_op() -> None:
    """No version bump, because nothing changed and history is not padding."""
    plan = _clean_plan()
    assert acknowledge_all_drops(plan) is plan


def test_acknowledging_all_drops_leaves_an_already_acknowledged_one_alone() -> None:
    plan = make_plan(
        draft=make_draft(
            dropped=[
                DroppedRange(
                    range="Calc!AK:AP",
                    reason="dead",
                    acknowledged=True,
                    note="signed off last quarter",
                ),
                DroppedRange(range="Calc!BB:BD", reason="superseded"),
            ]
        )
    )
    acknowledged = acknowledge_all_drops(plan, note="the rest")
    assert acknowledged.dropped[0].note == "signed off last quarter"
    assert acknowledged.dropped[1].note == "the rest"


# =============================================================================
# APPROVAL
# =============================================================================


def test_approval_is_refused_while_a_drop_is_unacknowledged_and_lists_every_blocker() -> None:
    """A user told only the first blocker will fix it, retry, and be told the second."""
    plan = make_plan(
        draft=make_draft(
            dropped=[
                DroppedRange(range="Calc!AK:AP", reason="dead"),
                DroppedRange(range="Calc!BB:BD", reason="superseded"),
            ]
        )
    )
    with pytest.raises(PlanNotApprovableError) as caught:
        approve(plan, by="phil")
    message = str(caught.value)
    assert "Calc!AK:AP" in message
    assert "Calc!BB:BD" in message


def test_approval_does_not_bump_the_version_because_it_is_about_this_one() -> None:
    plan = _clean_plan()
    approved = approve(plan, by="phil", note="looks right")
    assert approved.version == plan.version
    assert approved.approval.state is ApprovalState.APPROVED
    assert approved.approval.by == "phil"
    assert approved.approval.note == "looks right"
    assert approved.approval.at is not None


def test_a_rejected_plan_can_never_be_approved() -> None:
    rejected = reject(_clean_plan(), by="phil", reason="the decomposition is wrong")
    assert rejected.approval.state is ApprovalState.REJECTED
    with pytest.raises(PlanNotApprovableError, match="propose a new one"):
        approve(rejected, by="phil")


def test_requesting_changes_records_the_note_without_editing_the_plan() -> None:
    plan = _clean_plan()
    changed = request_changes(plan, by="phil", note="split the haircut stage in two")
    assert changed.approval.state is ApprovalState.CHANGES_REQUESTED
    assert changed.approval.note == "split the haircut stage in two"
    assert changed.version == plan.version
    assert changed.stages == plan.stages


def test_a_plan_asked_for_changes_can_still_be_approved_once_it_is_edited() -> None:
    changed = request_changes(_clean_plan(), by="phil", note="rename the last stage")
    edited = edit_stage(changed, "write_output", intent="Produce the monthly summary pack")
    assert approve(edited, by="phil").approval.approved


def test_a_rejection_cannot_be_laundered_into_an_approval_by_requesting_changes() -> None:
    """The route round the gate that `_revise` closed and these two verbs did not.

    `request_changes` writes approval without going through `_revise`, so it was the one edit that
    could take a plan out of REJECTED — and a plan in CHANGES_REQUESTED approves cleanly. Alice's
    rejection, and her reason for it, disappeared and Mallory's approval put the whole of the
    turned-down decomposition into force.
    """
    rejected = reject(_clean_plan(), by="alice", reason="the override step is a judgement call")

    with pytest.raises(PlanNotApprovableError):
        request_changes(rejected, by="mallory", note="hmm")

    assert rejected.approval.state is ApprovalState.REJECTED
    assert rejected.approval.by == "alice"
    with pytest.raises(PlanNotApprovableError, match="propose a new one"):
        approve(rejected, by="mallory")


def test_sending_a_rejected_plan_back_for_changes_names_who_rejected_it_and_why() -> None:
    """ "Rejected" with nobody attached tells the caller to argue with the tool, not the rejector."""
    rejected = reject(_clean_plan(), by="alice", reason="the override step is a judgement call")

    with pytest.raises(PlanNotApprovableError) as caught:
        request_changes(rejected, by="mallory", note="hmm")

    message = str(caught.value)
    assert "alice" in message
    assert "the override step is a judgement call" in message
    assert "terminal" in message
    assert "propose a new one" in message


@pytest.mark.parametrize(
    "decision",
    [
        pytest.param(lambda plan: approve(plan, by="mallory"), id="approve"),
        pytest.param(
            lambda plan: request_changes(plan, by="mallory", note="hmm"), id="request_changes"
        ),
    ],
)
def test_no_review_verb_moves_a_plan_back_out_of_rejection(decision) -> None:
    """Enumerated deliberately: a new approval verb that forgets the guard fails here."""
    rejected = reject(_clean_plan(), by="alice", reason="the decomposition is wrong")
    with pytest.raises(PlanNotApprovableError):
        decision(rejected)


def test_rejecting_a_plan_twice_leaves_it_rejected() -> None:
    """A rejection is terminal, not a lock against saying so again."""
    once = reject(_clean_plan(), by="alice", reason="the decomposition is wrong")
    twice = reject(once, by="bob", reason="agreed, and the drop list is wrong too")
    assert twice.approval.state is ApprovalState.REJECTED
    assert twice.approval.by == "bob"


# ── withdrawing an approval is deliberate, or it does not happen ──────────────


@pytest.mark.parametrize(
    "decision",
    [
        pytest.param(lambda plan: reject(plan, by="mallory", reason="I disagree"), id="reject"),
        pytest.param(
            lambda plan: request_changes(plan, by="mallory", note="think again"),
            id="request_changes",
        ),
    ],
)
def test_un_approving_a_plan_is_refused_unless_it_is_asked_for_explicitly(decision) -> None:
    """A notebook may already have been scaffolded from the approved plan.

    Quietly taking the approval away leaves that notebook in force with nothing on the plan
    saying it ever was, so the withdrawal has to be asked for by name.
    """
    approved = _approved(_clean_plan())

    with pytest.raises(PlanNotApprovableError) as caught:
        decision(approved)

    message = str(caught.value)
    assert "phil" in message, "who approved it, so the withdrawer knows whose decision it is"
    assert "withdraw_approval" in message
    assert approved.approval.approved


def test_a_deliberate_withdrawal_records_the_approval_it_overturned() -> None:
    """The rejection is written over the approval at the same version, so if the note does not
    carry the withdrawn approval nothing does."""
    approved = _approved(_clean_plan())

    rejected = reject(
        approved, by="alice", reason="the haircut stage is wrong", withdraw_approval=True
    )

    assert rejected.approval.state is ApprovalState.REJECTED
    assert rejected.approval.by == "alice"
    assert rejected.approval.note is not None
    assert "the haircut stage is wrong" in rejected.approval.note
    assert "withdraws the approval given by phil" in rejected.approval.note


def test_a_deliberate_withdrawal_can_also_send_an_approved_plan_back_for_changes() -> None:
    approved = _approved(_clean_plan())

    returned = request_changes(
        approved, by="alice", note="split the haircut stage", withdraw_approval=True
    )

    assert returned.approval.state is ApprovalState.CHANGES_REQUESTED
    assert returned.approval.note is not None
    assert "withdraws the approval given by phil" in returned.approval.note


def test_withdrawing_an_approval_nobody_ever_gave_is_not_asked_for() -> None:
    """The flag guards approved plans only; a draft is turned down with no ceremony."""
    plan = _clean_plan()
    assert reject(plan, by="alice", reason="wrong shape").approval.state is ApprovalState.REJECTED
    assert (
        request_changes(plan, by="alice", note="split it").approval.state
        is ApprovalState.CHANGES_REQUESTED
    )


def test_re_approving_an_approved_plan_is_not_a_withdrawal() -> None:
    """Approval is the safe direction, and the flag has nothing to do with it."""
    approved = _approved(_clean_plan())
    again = approve(approved, by="bob", note="checked a second time")
    assert again.approval.by == "bob"
    assert again.approval.approved


# =============================================================================
# REVIEW WARNINGS
# =============================================================================


def test_an_empty_open_questions_list_on_a_complex_workbook_is_flagged() -> None:
    """PLAN 6.2: the model that asks nothing has probably invented a purpose for something."""
    plan = _clean_plan(open_questions=[])
    warnings = review_warnings(plan, triage_result=TriageResult(TriageVerdict.PROCEED, 1.0, 0.9))
    assert any("suspicious" in warning for warning in warnings)


def test_complexity_is_taken_from_the_analysis_when_no_triage_is_supplied(analysis) -> None:
    plan = _clean_plan(open_questions=[])
    complex_analysis = make_analysis(
        operations=[make_operation(f"op_{index}") for index in range(200)]
    )
    assert not any("suspicious" in w for w in review_warnings(plan, analysis))
    assert any("suspicious" in w for w in review_warnings(plan, complex_analysis))


def test_low_and_unstated_confidence_stages_are_named() -> None:
    warnings = review_warnings(_clean_plan())
    assert any("write_output" in warning and "review marker" in warning for warning in warnings)


def test_unanswered_questions_warn_but_never_block() -> None:
    plan = _clean_plan()
    assert any("still unanswered" in warning for warning in review_warnings(plan))
    assert plan.is_approvable


def test_a_low_convertible_score_points_the_reviewer_at_the_blockers() -> None:
    plan = _clean_plan(assessment=Assessment(convertible=0.4, blockers=["heavy VBA"]))
    warnings = review_warnings(plan)
    assert any("triage scores only 40%" in warning for warning in warnings)


def test_a_partial_plan_with_no_checkpoints_asks_whether_judgement_became_code() -> None:
    plan = _clean_plan(
        assessment=Assessment(convertible=0.6),
        stages=[Stage(id="everything", intent="Do it all", confidence=Confidence.HIGH)],
    )
    assert any("judgement call being translated" in warning for warning in review_warnings(plan))


def test_operations_the_plan_never_claims_are_reported() -> None:
    analysis = make_analysis(
        operations=[make_operation("calc_h2_h500"), make_operation("calc_j2_j500")]
    )
    warnings = review_warnings(_clean_plan(), analysis)
    assert any("claimed by no stage" in warning for warning in warnings)


def test_operation_ids_the_analysis_never_produced_are_reported() -> None:
    """An invented id is a stage written against a region that does not exist."""
    plan = _clean_plan(
        stages=[Stage(id="s", intent="x", operations=["calc_h2_h500", "invented_op"])]
    )
    warnings = review_warnings(plan, make_analysis())
    assert any(
        "not in the analysis" in warning and "invented_op" in warning for warning in warnings
    )


def test_a_plan_that_claims_no_operations_at_all_is_not_accused_of_ignoring_them() -> None:
    """A plan written entirely as checkpoints legitimately claims no analysed regions."""
    plan = _clean_plan(
        stages=[
            Stage(
                id="agree_the_numbers",
                intent="Agree this month's figures with Risk",
                kind=StageKind.CHECKPOINT,
            )
        ]
    )
    warnings = review_warnings(plan, make_analysis())
    assert not any("claimed by no stage" in warning for warning in warnings)


def test_a_stage_listed_before_something_it_depends_on_is_reported() -> None:
    plan = _clean_plan(
        stages=[
            Stage(id="second", intent="y", depends_on=["first"], confidence=Confidence.HIGH),
            Stage(id="first", intent="x", confidence=Confidence.HIGH),
        ]
    )
    assert any("is listed before" in warning for warning in review_warnings(plan))


def test_a_stage_reading_another_it_does_not_depend_on_is_reported() -> None:
    """`sources` says what a stage reads; `depends_on` says what runs first. The scaffolder emits
    in the second order, so a stage that has only the first may find no frame there."""
    plan = _clean_plan(
        stages=[
            Stage(id="first", intent="x", confidence=Confidence.HIGH),
            Stage(
                id="second",
                intent="y",
                sources=[StageSource(origin=SourceOrigin.STAGE, ref="first")],
                confidence=Confidence.HIGH,
            ),
        ]
    )
    assert any(
        "reads first but does not depend on it" in warning for warning in review_warnings(plan)
    )


def test_a_stage_that_both_reads_and_depends_on_another_is_not_reported() -> None:
    plan = _clean_plan(
        stages=[
            Stage(id="first", intent="x", confidence=Confidence.HIGH),
            Stage(
                id="second",
                intent="y",
                sources=[StageSource(origin=SourceOrigin.STAGE, ref="first")],
                depends_on=["first"],
                confidence=Confidence.HIGH,
            ),
        ]
    )
    assert not any("does not depend on" in warning for warning in review_warnings(plan))


def test_a_clean_plan_against_its_own_analysis_warns_about_nothing_structural() -> None:
    analysis = make_analysis()
    plan = _clean_plan(
        assessment=Assessment(convertible=0.95),
        stages=[
            Stage(
                id="apply_haircuts",
                intent="Collateral haircut lookup",
                confidence=Confidence.HIGH,
                operations=["calc_h2_h500"],
            )
        ],
        open_questions=[OpenQuestion(question="Is AF dead?", answer="Yes, since 2023")],
    )
    assert review_warnings(plan, analysis, triage_result=triage(analysis)) == []
    assert "REVIEW WARNINGS" not in render_plan(plan, analysis=analysis)


# =============================================================================
# RENDERING
# =============================================================================


def test_the_rendered_plan_carries_every_section_a_reviewer_needs() -> None:
    rendered = render_plan(make_plan(), show_warnings=False)
    assert "Process plan v1 for rwa_monthly_v14.xlsx" in rendered
    assert "ASSESSMENT" in rendered
    assert "STAGES (4)" in rendered
    assert "OPEN QUESTIONS (1)" in rendered
    assert "DROPPED (1)" in rendered
    assert "APPROVAL" in rendered


def test_the_rendered_stage_shows_its_kind_confidence_dependencies_and_checkpoint() -> None:
    rendered = render_plan(make_plan(), show_warnings=False)
    assert "[checkpoint, confidence n/a]" in rendered
    assert "after: apply_haircuts" in rendered
    assert "asks: Have this month's overrides been agreed with Risk?" in rendered
    assert "options: approve, reject" in rendered
    assert "pattern: vlookup_exact" in rendered
    assert "assumes: header on row 1" in rendered


def test_the_rendered_stage_says_where_each_of_its_inputs_comes_from() -> None:
    """A bare `Calc!H2:H500` never said whether anyone knew where it came from; the origin does."""
    plan = _clean_plan(
        stages=[
            Stage(
                id="load_exposures",
                intent="Pull exposures and this month's adjustments",
                kind=StageKind.LOAD,
                sources=[
                    StageSource(origin=SourceOrigin.QUERY, ref="MonthlyExposures"),
                    StageSource(origin=SourceOrigin.HANDIN),
                    StageSource(origin=SourceOrigin.MANUAL, ref="Adjustments!B2:B15"),
                ],
                confidence=Confidence.HIGH,
            ),
            Stage(
                id="apply_haircuts",
                intent="Collateral haircut lookup",
                sources=[
                    StageSource(origin=SourceOrigin.STAGE, ref="load_exposures"),
                    StageSource(origin=SourceOrigin.RANGE, ref="Ref!A1:D50"),
                ],
                depends_on=["load_exposures"],
                confidence=Confidence.HIGH,
            ),
        ]
    )

    rendered = render_plan(plan, show_warnings=False)

    assert "sources: query MonthlyExposures, handin, manual Adjustments!B2:B15" in rendered
    assert "sources: stage load_exposures, range Ref!A1:D50" in rendered


def test_a_sources_list_too_long_for_one_line_never_splits_an_origin_from_its_ref() -> None:
    """Wrapping introduced a space a 1.0 source never had, and prose wraps at any space.

    A line ending `power_query` with the ref on the next reads as an origin nobody named — the
    reviewer sees a Power Query table the plan did not identify, when the plan identified it.
    """
    plan = _clean_plan(
        stages=[
            Stage(
                id="load_everything",
                intent="Pull every input this month needs",
                kind=StageKind.LOAD,
                sources=[
                    StageSource(origin=SourceOrigin.POWER_QUERY, ref=f"CounterpartyRatings{n}")
                    for n in range(6)
                ],
                confidence=Confidence.HIGH,
            )
        ]
    )

    lines = render_plan(plan, show_warnings=False).splitlines()

    assert any(line.strip().startswith("sources:") for line in lines)
    for line in lines:
        assert line.strip() not in {"power_query", "sources: power_query"}
        assert not line.rstrip().endswith("power_query")
    for index in range(6):
        assert any(f"power_query CounterpartyRatings{index}" in line for line in lines)


def test_everything_optional_on_a_plan_renders_when_it_is_present() -> None:
    """The prose fields a reviewer actually reads: the rationale, the note, and the answer."""
    plan = _clean_plan(
        summary="A monthly regulatory calculation.",
        assessment=Assessment(
            convertible=0.7,
            blockers=["fourteen typed overrides"],
            rationale="Everything but the override block translates cleanly.",
        ),
        stages=[
            Stage(
                id="apply_haircuts",
                intent="Collateral haircut lookup",
                confidence=Confidence.HIGH,
                notes="joins on asset_class; keys checked for uniqueness first",
            )
        ],
        open_questions=[
            OpenQuestion(
                question="Is column AF dead?",
                context="Calc!AF2:AF500, no downstream references",
                answer="Dead since the 2023 migration.",
            )
        ],
    )
    rendered = render_plan(plan, show_warnings=False)
    assert "A monthly regulatory calculation." in rendered
    assert "Everything but the override block translates cleanly." in rendered
    assert "- fourteen typed overrides" in rendered
    assert "note: joins on asset_class" in rendered
    assert "context: Calc!AF2:AF500" in rendered
    assert "answer: Dead since the 2023 migration." in rendered
    assert "[answered]" in rendered


def test_a_long_operation_list_is_truncated_with_a_count_of_what_is_left() -> None:
    plan = _clean_plan(
        stages=[
            Stage(
                id="everything",
                intent="Every column on the Calc sheet",
                confidence=Confidence.HIGH,
                operations=[f"op_{index}" for index in range(10)],
            )
        ]
    )
    assert "(+4 more)" in render_plan(plan, show_warnings=False)


def test_an_unacknowledged_drop_is_shouted_at_the_bottom_where_a_reader_ends_up() -> None:
    rendered = render_plan(make_plan(), show_warnings=False)
    assert "[NOT ACKNOWLEDGED] Calc!AK:AP" in rendered
    assert "cannot be approved yet:" in rendered


def test_a_rejected_drop_says_it_must_be_kept() -> None:
    rejected = acknowledge_drop(make_plan(), "Calc!AK:AP", accepted=False, note="Finance reads it")
    rendered = render_plan(rejected, show_warnings=False)
    assert "[REJECTED - must be kept]" in rendered
    assert "note: Finance reads it" in rendered


def test_an_approvable_plan_says_nothing_reaches_the_notebook_until_approval() -> None:
    rendered = render_plan(acknowledge_all_drops(make_plan()), show_warnings=False)
    assert "nothing is written to the notebook until it is given" in rendered


def test_an_approved_plan_renders_who_approved_it() -> None:
    rendered = render_plan(_approved(make_plan()), show_warnings=False)
    assert "approved by phil at" in rendered


def test_an_empty_plan_body_still_renders_its_empty_sections_honestly() -> None:
    plan = _clean_plan(
        open_questions=[],
        assessment=Assessment(convertible=1.0),
        stages=[Stage(id="only", intent="the whole process", confidence=Confidence.HIGH)],
        summary=None,
    )
    rendered = render_plan(plan, show_warnings=False)
    assert "no blockers stated" in rendered
    assert "none — on a complex workbook that is itself worth questioning" in rendered
    assert "nothing dropped" in rendered


def test_warnings_are_rendered_when_asked_for() -> None:
    rendered = render_plan(make_plan(), analysis=make_analysis())
    assert "REVIEW WARNINGS" in rendered
    assert "still unanswered" in rendered


def test_the_model_that_wrote_the_plan_is_named_in_the_header() -> None:
    plan = make_plan(llm_model="local-7b")
    assert "(local-7b)" in render_plan(plan, show_warnings=False)


# =============================================================================
# DIFF: THE CHANGE RECORD
# =============================================================================


def test_a_plan_diffed_against_itself_is_empty() -> None:
    plan = make_plan()
    diff = diff_plans(plan, plan)
    assert diff.is_empty
    assert render_diff(diff).endswith("no material changes")


def test_an_added_and_a_removed_stage_are_both_reported() -> None:
    before = _clean_plan()
    after = add_stage(remove_stage(before, "write_output"), Stage(id="archive", intent="Archive"))
    diff = diff_plans(before, after)
    assert diff.added_stages == ("archive",)
    assert diff.removed_stages == ("write_output",)
    assert not diff.is_empty


def test_a_changed_source_diffs_as_the_line_a_reviewer_reads() -> None:
    """A diff is read, not parsed: the mapping each source dumps to is three times the width."""
    before = _clean_plan()
    after = edit_stage(
        before,
        "apply_haircuts",
        sources=[
            StageSource(origin=SourceOrigin.RANGE, ref="Calc!H2:H500"),
            StageSource(origin=SourceOrigin.RANGE, ref="Calc!AK:AP"),
        ],
    )

    rendered = render_diff(diff_plans(before, after))

    assert "sources: ['range Calc!H2:H500', 'range Ref!A1:D50']" in rendered
    assert "['range Calc!H2:H500', 'range Calc!AK:AP']" in rendered


def test_a_reorder_is_reported_as_a_reorder_not_as_four_changes() -> None:
    before = _clean_plan()
    after = reorder_stages(
        before, ["load_handin", "apply_haircuts", "write_output", "manual_overrides"]
    )
    diff = diff_plans(before, after)
    assert diff.reordered
    assert not diff.changed_stages


def test_a_changed_field_is_reported_with_both_sides() -> None:
    before = _clean_plan()
    after = edit_stage(before, "apply_haircuts", confidence=Confidence.LOW, notes="unsure")
    diff = diff_plans(before, after)
    change = next(item for item in diff.changed_stages if item.stage_id == "apply_haircuts")
    assert set(change.fields) == {"confidence", "notes"}
    assert change.before["confidence"] == "high"
    assert change.after["confidence"] == "low"


def test_questions_added_answered_and_removed_are_each_distinguished() -> None:
    before = _clean_plan()
    answered = answer_question(before, 0, "Dead since 2023.")
    after = add_question(answered, "Where does the FX rate come from?")

    diff = diff_plans(before, after)
    assert diff.answered_questions == (before.open_questions[0].question,)
    assert diff.added_questions == ("Where does the FX rate come from?",)
    assert diff.removed_questions == ()

    removed = diff_plans(before, _clean_plan(open_questions=[]))
    assert removed.removed_questions == (before.open_questions[0].question,)


def test_an_acknowledged_drop_is_distinguished_from_an_added_one() -> None:
    before = make_plan()
    after = acknowledge_drop(before, "Calc!AK:AP")
    diff = diff_plans(before, after)
    assert diff.acknowledged_drops == ("Calc!AK:AP",)
    assert diff.added_drops == ()


def test_added_and_removed_drops_are_reported() -> None:
    before = make_plan()
    after = make_plan(
        draft=make_draft(dropped=[DroppedRange(range="Calc!BB:BD", reason="superseded")])
    )
    diff = diff_plans(before, after)
    assert diff.added_drops == ("Calc!BB:BD",)
    assert diff.removed_drops == ("Calc!AK:AP",)
    rendered = render_diff(diff)
    assert "+ drop Calc!BB:BD" in rendered
    assert "- drop Calc!AK:AP" in rendered


def test_the_convertible_delta_is_reported_signed() -> None:
    before = _clean_plan(assessment=Assessment(convertible=0.8))
    after = _clean_plan(assessment=Assessment(convertible=0.55))
    diff = diff_plans(before, after)
    assert diff.convertible_delta == pytest.approx(-0.25)
    assert "-0.25" in render_diff(diff)


def test_an_approval_is_reported_even_though_nothing_material_moved() -> None:
    """`is_empty` is about content, but an approval is the most important event in the history:
    rendering it as "no material changes" would hide the only thing that happened."""
    before = acknowledge_all_drops(make_plan())
    after = approve(before, by="phil")
    diff = diff_plans(before, after)
    assert diff.approval_changed
    assert diff.is_empty
    rendered = render_diff(diff)
    assert "approval state changed" in rendered
    assert "no material changes" not in rendered


def test_a_diff_with_neither_content_nor_approval_changes_says_so() -> None:
    plan = make_plan()
    assert render_diff(diff_plans(plan, plan)).endswith("no material changes")


def test_the_rendered_diff_reads_as_a_change_record() -> None:
    before = _clean_plan()
    after = edit_stage(
        add_stage(before, Stage(id="archive", intent="Archive the output")),
        "apply_haircuts",
        notes="joins on asset_class",
    )
    rendered = render_diff(diff_plans(before, after))
    assert rendered.startswith("Plan v1 -> v3")
    assert "+ stage archive" in rendered
    assert "~ stage apply_haircuts: notes" in rendered
    assert "joins on asset_class" in rendered


def test_every_kind_of_change_has_a_line_in_the_rendered_diff() -> None:
    """One quarter's worth of review, rendered: this is what the change record actually looks
    like when the process moves."""
    before = make_plan(
        draft=make_draft(
            dropped=[
                DroppedRange(range="Calc!AK:AP", reason="no downstream refs"),
                DroppedRange(range="Calc!BB:BD", reason="superseded by the Ref sheet"),
            ]
        )
    )

    after = before
    after = acknowledge_drop(after, "Calc!AK:AP", note="agreed")
    after = answer_question(after, 0, "Dead since the 2023 migration.")
    after = add_question(after, "Where does the FX rate come from?")
    after = remove_stage(after, "write_output")
    after = add_stage(after, Stage(id="publish", intent="Publish the summary pack"))
    after = reorder_stages(after, ["apply_haircuts", "load_handin", "manual_overrides", "publish"])

    diff = diff_plans(before, after)
    rendered = render_diff(diff)

    assert "+ stage publish" in rendered
    assert "- stage write_output" in rendered
    assert "~ stages reordered" in rendered
    assert "+ question: Where does the FX rate come from?" in rendered
    assert "* answered: Column AF is computed" in rendered
    assert "* acknowledged drop Calc!AK:AP" in rendered

    removed = render_diff(
        diff_plans(before, _clean_plan(open_questions=[OpenQuestion(question="a new one")]))
    )
    assert "- question: Column AF is computed" in removed
    assert "- drop Calc!AK:AP" in removed
    assert "- drop Calc!BB:BD" in removed
