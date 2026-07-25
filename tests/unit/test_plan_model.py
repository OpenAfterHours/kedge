"""The `ProcessPlan` schema: free-form stages, required questions, acknowledged drops."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from conftest import make_draft, make_plan
from kedge.analysis.model import ExcelPattern
from kedge.plan.model import (
    PLAN_SCHEMA_VERSION,
    Approval,
    ApprovalState,
    Assessment,
    Checkpoint,
    Confidence,
    DroppedRange,
    OpenQuestion,
    PlanDraft,
    ProcessPlan,
    Stage,
    StageKind,
    topological_stages,
)

# ── free-form stages ────────────────────────────────────────────────────────


@pytest.mark.parametrize("count", [1, 4, 15, 40])
def test_plan_accepts_any_number_of_stages(count: int) -> None:
    """No template: four stages or fifteen, the schema does not care (PLAN 2.2)."""
    draft = make_draft(
        stages=[Stage(id=f"step_{index}", intent=f"step {index}") for index in range(count)],
        dropped=[],
    )
    assert len(draft.stages) == count


@pytest.mark.parametrize(
    "stage_id",
    ["load_handin", "Reconcile-EOD", "step 1", "compute_émissions", "STAGE_42"],
)
def test_stage_ids_are_not_drawn_from_a_fixed_vocabulary(stage_id: str) -> None:
    """Stage naming is the model's decision, not an enum (PLAN 6.2)."""
    assert Stage(id=stage_id, intent="x").id == stage_id


def test_blank_stage_id_is_rejected() -> None:
    with pytest.raises(ValidationError, match="blank"):
        Stage(id="   ", intent="x")


def test_stage_id_with_no_alphanumerics_is_rejected() -> None:
    """The id becomes a cell name, so it must be able to make an identifier."""
    with pytest.raises(ValidationError, match="alphanumeric"):
        Stage(id="---", intent="x")


def test_blank_intent_is_rejected_rather_than_silently_accepted() -> None:
    with pytest.raises(ValidationError, match="open_questions"):
        Stage(id="mystery", intent="  ")


# ── open questions are structurally required ────────────────────────────────


def test_open_questions_is_required_not_optional_with_default() -> None:
    """The model must actively decide the list is empty (PLAN 6.2)."""
    with pytest.raises(ValidationError) as caught:
        PlanDraft(
            assessment=Assessment(convertible=0.9),
            stages=[Stage(id="a", intent="x")],
        )
    assert any(error["loc"] == ("open_questions",) for error in caught.value.errors())


def test_open_question_accepts_a_bare_string() -> None:
    """Which is what the PLAN's worked example and most model output produce."""
    question = OpenQuestion.model_validate("What is column AF for?")
    assert question.question == "What is column AF for?"
    assert not question.answered


def test_empty_open_questions_warns_on_a_complex_workbook() -> None:
    plan = make_plan(draft=make_draft(open_questions=[], dropped=[]))
    assert plan.open_questions_warning(complexity=0.8) is not None
    assert "suspicious" in plan.open_questions_warning(complexity=0.8)


def test_empty_open_questions_does_not_warn_on_a_simple_workbook() -> None:
    plan = make_plan(draft=make_draft(open_questions=[], dropped=[]))
    assert plan.open_questions_warning(complexity=0.1) is None


def test_a_plan_with_questions_never_warns_about_them_being_empty() -> None:
    plan = make_plan()
    assert plan.open_questions_warning(complexity=0.99) is None


# ── dropped ranges require acknowledgement ──────────────────────────────────


def test_a_drop_starts_unacknowledged() -> None:
    drop = DroppedRange(range="Calc!AK:AP", reason="dead since 2023")
    assert not drop.acknowledged


def test_an_unacknowledged_drop_blocks_approval() -> None:
    plan = make_plan()
    blockers = plan.approval_blockers()
    assert not plan.is_approvable
    assert any("Calc!AK:AP" in blocker for blocker in blockers)


def test_acknowledging_every_drop_makes_the_plan_approvable() -> None:
    plan = make_plan()
    acknowledged = plan.model_copy(
        update={
            "dropped": [drop.model_copy(update={"acknowledged": True}) for drop in plan.dropped]
        }
    )
    assert acknowledged.is_approvable


def test_a_drop_needs_both_a_range_and_a_reason() -> None:
    with pytest.raises(ValidationError):
        DroppedRange(range="Calc!AK:AP", reason="  ")


# ── checkpoints ─────────────────────────────────────────────────────────────


def test_checkpoint_stage_defaults_to_not_applicable_confidence() -> None:
    """`n/a` is a positive statement; `unknown` is the absence of one."""
    stage = Stage(id="overrides", intent="agreed with Risk", kind=StageKind.CHECKPOINT)
    assert stage.confidence is Confidence.NOT_APPLICABLE
    assert not stage.needs_review_marker


def test_checkpoint_question_is_synthesised_when_absent() -> None:
    stage = Stage(id="overrides", intent="Overrides agreed with Risk", kind="checkpoint")
    assert "Overrides agreed with Risk" in stage.effective_checkpoint().question


def test_checkpoint_options_cannot_be_empty() -> None:
    with pytest.raises(ValidationError, match="at least one"):
        Checkpoint(question="ok?", options=["  "])


# ── confidence and review markers ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("confidence", "marked"),
    [
        (Confidence.HIGH, False),
        (Confidence.MEDIUM, False),
        (Confidence.LOW, True),
        (Confidence.UNKNOWN, True),
    ],
)
def test_unstated_confidence_is_marked_for_review_like_low(
    confidence: Confidence, marked: bool
) -> None:
    """A stage whose confidence was never stated must not look as finished as the rest."""
    stage = Stage(id="s", intent="x", confidence=confidence)
    assert stage.needs_review_marker is marked


# ── the translation vocabulary ──────────────────────────────────────────────


def test_excel_pattern_is_accepted_case_insensitively() -> None:
    """The PLAN's own worked example writes `VLOOKUP_exact`."""
    stage = Stage(id="s", intent="x", excel_pattern="VLOOKUP_exact")
    assert stage.excel_pattern is ExcelPattern.VLOOKUP_EXACT


def test_an_unrecognised_pattern_becomes_unknown_rather_than_failing() -> None:
    """Unknown is the signal to raise an open question, not an invitation to improvise."""
    stage = Stage(id="s", intent="x", excel_pattern="xlookup_with_spill")
    assert stage.excel_pattern is ExcelPattern.UNKNOWN


def test_an_absent_pattern_stays_none() -> None:
    assert Stage(id="s", intent="x", excel_pattern="n/a").excel_pattern is None


# ── the stage DAG ───────────────────────────────────────────────────────────


def test_duplicate_stage_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate stage id"):
        make_draft(stages=[Stage(id="a", intent="x"), Stage(id="a", intent="y")], dropped=[])


def test_a_dependency_on_an_unknown_stage_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown stage"):
        make_draft(stages=[Stage(id="a", intent="x", depends_on=["ghost"])], dropped=[])


def test_a_self_dependency_is_rejected() -> None:
    with pytest.raises(ValidationError, match="depends on itself"):
        make_draft(stages=[Stage(id="a", intent="x", depends_on=["a"])], dropped=[])


def test_a_dependency_cycle_is_rejected() -> None:
    with pytest.raises(ValidationError, match="cycle"):
        make_draft(
            stages=[
                Stage(id="a", intent="x", depends_on=["b"]),
                Stage(id="b", intent="y", depends_on=["a"]),
            ],
            dropped=[],
        )


def test_topological_order_keeps_plan_order_as_the_tie_break() -> None:
    stages = [
        Stage(id="a", intent="x"),
        Stage(id="b", intent="y"),
        Stage(id="c", intent="z", depends_on=["a"]),
    ]
    assert [stage.id for stage in topological_stages(stages)] == ["a", "b", "c"]


def test_a_forward_reference_is_reordered_rather_than_rejected() -> None:
    """Sequencing slips are fixed silently; the plan still warns a human about them."""
    draft = make_draft(
        stages=[
            Stage(id="second", intent="y", depends_on=["first"]),
            Stage(id="first", intent="x"),
        ],
        dropped=[],
    )
    plan = make_plan(draft=draft)
    assert [stage.id for stage in plan.ordered_stages()] == ["first", "second"]
    assert plan.ordering_warnings()


def test_a_plan_needs_at_least_one_stage() -> None:
    with pytest.raises(ValidationError, match="at least one stage"):
        make_draft(stages=[], dropped=[])


# ── provenance and versioning ───────────────────────────────────────────────


def test_a_new_plan_carries_full_provenance_and_is_unapproved() -> None:
    plan = make_plan()
    assert plan.plan_schema_version == PLAN_SCHEMA_VERSION
    assert plan.version == 1
    assert plan.workbook_sha256 == "a" * 64
    assert plan.analysis_schema_version
    assert plan.approval.state is ApprovalState.DRAFT


def test_from_analysis_draft_takes_provenance_from_the_analysis(analysis) -> None:
    plan = ProcessPlan.from_analysis_draft(make_draft(), analysis)
    assert plan.workbook == analysis.workbook.filename
    assert plan.workbook_sha256 == analysis.workbook.sha256
    assert plan.analysis_schema_version == analysis.schema_version


def test_to_draft_strips_provenance_and_approval() -> None:
    """Which is how last quarter's plan re-seeds the next one."""
    plan = make_plan().model_copy(update={"approval": Approval(state=ApprovalState.APPROVED)})
    draft = plan.to_draft()
    assert draft.stages == plan.stages
    assert not hasattr(draft, "approval")


# ── round trips ─────────────────────────────────────────────────────────────


def test_plan_round_trips_through_model_validate_json() -> None:
    plan = make_plan()
    restored = ProcessPlan.model_validate_json(plan.model_dump_json())
    assert restored == plan


def test_plan_round_trips_through_plain_json_with_no_information_lost() -> None:
    plan = make_plan()
    restored = ProcessPlan.model_validate(json.loads(plan.model_dump_json()))
    assert restored.stages == plan.stages
    assert restored.open_questions == plan.open_questions
    assert restored.dropped == plan.dropped


def test_an_unknown_field_is_rejected_so_the_model_gets_a_repair_instruction() -> None:
    """`extra="forbid"` turns a hallucinated field into a precise correction."""
    raw = json.loads(make_plan().model_dump_json())
    raw["stages"][0]["confidence_score"] = 0.9
    with pytest.raises(ValidationError, match="confidence_score"):
        ProcessPlan.model_validate(raw)


def test_plans_are_frozen() -> None:
    plan = make_plan()
    with pytest.raises(ValidationError):
        plan.version = 2


# ── accessors ───────────────────────────────────────────────────────────────


def test_accessors_expose_what_review_needs() -> None:
    plan = make_plan()
    assert plan.stage("apply_haircuts") is not None
    assert plan.stage("nope") is None
    assert [stage.id for stage in plan.checkpoints] == ["manual_overrides"]
    assert [stage.id for stage in plan.low_confidence_stages] == ["write_output"]
    assert len(plan.unanswered_questions) == 1
    assert len(plan.unacknowledged_drops) == 1
