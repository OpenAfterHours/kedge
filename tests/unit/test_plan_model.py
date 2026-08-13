"""The `ProcessPlan` schema: free-form stages, required questions, acknowledged drops."""

from __future__ import annotations

import json
from typing import Any

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
    SourceOrigin,
    Stage,
    StageKind,
    StageSource,
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


# ── where a stage's inputs come from ────────────────────────────────────────


def _pre_1_1_payload(**overrides: Any) -> dict[str, Any]:
    """A plan exactly as schema 1.0 wrote it: `sources` is a list of bare strings.

    The four shapes that vocabulary allowed are all here, because all four are on somebody's disk:
    the literal `handin`, sheet-qualified ranges, the id of an upstream stage, and — undocumented
    but inevitable — a name kedge cannot classify at all.

    Spelled out rather than dumped. This used to be `json.loads(make_plan().model_dump_json())`
    with the stamp changed and three stages overwritten by index, which made it a *reflection of
    today's schema wearing a 1.0 label*: adding a field to `Stage` silently added it to the "1.0"
    payload, and reordering `make_draft`'s stages moved the assertions onto different ones. The
    whole point of the test is a file somebody committed months ago, so it is written the way that
    file is — the same rule `_PLAN_V1_0_YAML` in `test_plan_store.py` already follows.
    """
    payload: dict[str, Any] = {
        "plan_schema_version": "1.0",
        "version": 1,
        "created_at": "2026-06-30T09:00:00Z",
        "workbook": "rwa_monthly_v13.xlsx",
        "workbook_sha256": "b" * 64,
        "analysis_schema_version": "1.0",
        "generated_by": "llm",
        "assessment": {"convertible": 0.8, "blockers": []},
        "stages": [
            {
                "id": "load_handin",
                "intent": "Read counterparty exposures from the hand-in",
                "kind": "load",
                "sources": ["handin"],
                "confidence": "high",
            },
            {
                "id": "apply_haircuts",
                "intent": "Collateral haircut lookup by asset class",
                "kind": "transform",
                "sources": ["Calc!H2:H500", "Ref!A1:D50", "load_handin"],
                "depends_on": ["load_handin"],
                "confidence": "high",
            },
            {
                "id": "manual_overrides",
                "intent": "Overrides typed by the analyst each month",
                "kind": "checkpoint",
                "depends_on": ["apply_haircuts"],
            },
            {
                "id": "write_output",
                "intent": "Produce the monthly RWA summary",
                "kind": "output",
                "sources": ["HaircutTable"],
                "depends_on": ["apply_haircuts", "manual_overrides"],
                "confidence": "low",
            },
        ],
        "open_questions": [
            {"question": "Column AF is computed but never referenced. Dead, or read manually?"}
        ],
        "dropped": [],
        "approval": {"state": "draft"},
    }
    payload.update(overrides)
    return payload


def test_a_plan_written_before_sources_had_origins_still_loads() -> None:
    """Plans are versioned artifacts on disk that users review and commit. Last month's opens."""
    plan = ProcessPlan.model_validate(_pre_1_1_payload())

    load, haircuts, _, output = plan.stages
    assert [source.origin for source in load.sources] == [SourceOrigin.HANDIN]
    assert [source.origin for source in haircuts.sources] == [
        SourceOrigin.RANGE,
        SourceOrigin.RANGE,
        SourceOrigin.STAGE,
    ]
    assert [source.ref for source in haircuts.sources] == [
        "Calc!H2:H500",
        "Ref!A1:D50",
        "load_handin",
    ]
    # Not a stage id, not a range, not the hand-in: `unknown` keeps the text and refuses to guess,
    # which is the only reading that cannot turn an old plan into a validation error.
    assert output.sources == [StageSource(origin=SourceOrigin.UNKNOWN, ref="HaircutTable")]


def test_loading_an_old_plan_leaves_the_version_it_was_written_at_alone() -> None:
    """The stamp dates the file. Rewriting it on load would claim the author wrote 1.1."""
    assert ProcessPlan.model_validate(_pre_1_1_payload()).plan_schema_version == "1.0"
    assert make_plan().plan_schema_version == PLAN_SCHEMA_VERSION == "1.1"


@pytest.mark.parametrize(
    "text", ["Calc!H2:H500", "Ref!A1:D50", "$A$1:$D$50", "AK:AP", "H2", "'Fee Calc'!B2:B99"]
)
def test_a_bare_string_that_names_a_range_is_read_as_one(text: str) -> None:
    source = StageSource.model_validate(text)
    assert source.origin is SourceOrigin.RANGE
    assert source.ref == text


@pytest.mark.parametrize("text", ["handin", "hand-in", "Hand In", "HANDIN"])
def test_the_literal_handin_is_read_as_the_hand_in_it_always_meant(text: str) -> None:
    """PLAN 2.2's own worked example writes `sources: [handin]`."""
    source = StageSource.model_validate(text)
    assert source.origin is SourceOrigin.HANDIN
    assert source.ref is None


def test_a_bare_string_is_read_as_a_stage_only_when_the_plan_holds_that_stage() -> None:
    """`StageSource` alone cannot tell an upstream stage from a named range; the plan can."""
    alone = StageSource.model_validate("load_handin")
    assert alone.origin is SourceOrigin.UNKNOWN

    plan = ProcessPlan.model_validate(_pre_1_1_payload())
    resolved = plan.stages[1].sources[2]
    assert resolved.origin is SourceOrigin.STAGE
    assert resolved.ref == "load_handin"


def test_a_bare_source_naming_a_stage_that_was_renamed_since_loads_as_unknown() -> None:
    """The rule that makes the reading safe: only exact matches are upgraded, so nothing fails."""
    raw = _pre_1_1_payload()
    raw["stages"][1]["sources"] = ["load_the_handin"]
    plan = ProcessPlan.model_validate(raw)
    assert plan.stages[1].sources == [
        StageSource(origin=SourceOrigin.UNKNOWN, ref="load_the_handin")
    ]


# ── the order of the classification tests ───────────────────────────────────
#
# Three plans that loaded under 1.0 stopped loading, or quietly lost an edge, because the tests
# ran in the order the vocabulary was memorable in rather than the order the evidence is strong
# in. Each of the next four tests is one of them.


def test_a_1_0_stage_naming_its_own_id_as_a_source_still_loads() -> None:
    """The worst outcome available: a committed artifact refused by a rule written after it.

    Under 1.0 `sources` was free-form and a stage was routinely named after the region it reads,
    so `id: adjustments` with `sources: [adjustments]` meant "the adjustments region" and was
    unremarkable. A stage's own id is therefore excluded from the candidates when its bare sources
    are read, and the string falls through to `unknown` with its text intact.
    """
    raw = _pre_1_1_payload()
    raw["stages"].append(
        {
            "id": "adjustments",
            "intent": "Manual overrides typed each month",
            "sources": ["adjustments"],
        }
    )

    plan = ProcessPlan.model_validate(raw)

    stage = plan.stage("adjustments")
    assert stage is not None
    assert stage.sources == [StageSource(origin=SourceOrigin.UNKNOWN, ref="adjustments")]
    assert stage.upstream_stage_ids == []


def test_a_self_source_written_deliberately_in_1_1_is_still_an_error() -> None:
    """The exclusion is a reading of an old bare string, not a hole in the graph check."""
    raw = _pre_1_1_payload()
    raw["stages"].append(
        {
            "id": "adjustments",
            "intent": "Manual overrides typed each month",
            "sources": [{"origin": "stage", "ref": "adjustments"}],
        }
    )
    with pytest.raises(ValidationError, match="lists itself"):
        ProcessPlan.model_validate(raw)


@pytest.mark.parametrize("stage_id", ["q1", "fx1", "vat2", "p3", "s1"])
def test_a_stage_id_shaped_like_a_cell_reference_is_still_read_as_a_stage(stage_id: str) -> None:
    """The plan is stronger evidence than a shape: `q1` is a cell *shape* and a good stage name.

    Read as a range, the edge disappears in silence — `upstream_stage_ids` empties, the graph
    check never sees it, the reads-but-does-not-depend warning never fires, and a later rename
    leaves the source pointing at nothing.
    """
    raw = _pre_1_1_payload()
    raw["stages"].append({"id": stage_id, "intent": "Quarterly figures"})
    raw["stages"][1]["sources"] = [stage_id]
    raw["stages"][1]["depends_on"] = ["load_handin", stage_id]

    plan = ProcessPlan.model_validate(raw)

    assert plan.stages[1].sources == [StageSource(origin=SourceOrigin.STAGE, ref=stage_id)]
    assert plan.stages[1].upstream_stage_ids == [stage_id]


def test_a_cell_shaped_string_the_plan_has_no_stage_for_is_still_a_range() -> None:
    """The shape test did not go away; it moved behind the one piece of evidence that beats it."""
    raw = _pre_1_1_payload()
    raw["stages"][1]["sources"] = ["H2", "AK:AP"]
    plan = ProcessPlan.model_validate(raw)
    assert [source.origin for source in plan.stages[1].sources] == [
        SourceOrigin.RANGE,
        SourceOrigin.RANGE,
    ]


def test_a_stage_actually_named_handin_keeps_both_its_edge_and_its_text() -> None:
    """`handin` is the one lossy branch — it drops the ref — so it has to be tested last.

    `load_handin` is the PLAN's own worked stage name, so a stage called `handin` is entirely
    plausible. Classified as the hand-in, the ref is gone: the text cannot survive a round trip,
    `approval_blockers` can never match it, and a rename cannot be followed.
    """
    raw = _pre_1_1_payload()
    raw["stages"][0]["id"] = "handin"
    raw["stages"][1]["sources"] = ["handin"]
    raw["stages"][1]["depends_on"] = ["handin"]
    raw["stages"][2]["depends_on"] = ["apply_haircuts"]

    plan = ProcessPlan.model_validate(raw)

    assert plan.stages[1].sources == [StageSource(origin=SourceOrigin.STAGE, ref="handin")]
    assert plan.stages[1].upstream_stage_ids == ["handin"]
    assert plan.stages[1].sources[0].render() == "stage handin"


def test_a_stage_named_handin_reading_the_hand_in_still_reads_the_hand_in() -> None:
    """Its own id excluded, `handin` on the `handin` stage is the literal it always was."""
    raw = _pre_1_1_payload()
    raw["stages"][0]["id"] = "handin"
    raw["stages"][1]["depends_on"] = ["handin"]
    raw["stages"][2]["depends_on"] = ["apply_haircuts"]

    plan = ProcessPlan.model_validate(raw)

    assert plan.stages[0].sources == [StageSource(origin=SourceOrigin.HANDIN)]


@pytest.mark.parametrize(
    ("text", "origin", "ref"),
    [
        # 1. A sheet qualifier is unambiguous, and beats a stage of the same name.
        ("Calc!H2:H500", SourceOrigin.RANGE, "Calc!H2:H500"),
        ("'Fee Calc'!B2:B99", SourceOrigin.RANGE, "'Fee Calc'!B2:B99"),
        # 2. An exact match against another stage id of this plan.
        ("load_handin", SourceOrigin.STAGE, "load_handin"),
        ("q1", SourceOrigin.STAGE, "q1"),
        ("handin", SourceOrigin.STAGE, "handin"),
        # 3. An A1 shape the plan has no stage for.
        ("H2", SourceOrigin.RANGE, "H2"),
        ("$A$1:$D$50", SourceOrigin.RANGE, "$A$1:$D$50"),
        ("AK:AP", SourceOrigin.RANGE, "AK:AP"),
        # 4. The hand-in words, which drop the ref because the origin is the whole answer.
        ("hand-in", SourceOrigin.HANDIN, None),
        ("HANDIN", SourceOrigin.HANDIN, None),
        # 5. Anything else keeps its text and refuses to guess.
        ("HaircutTable", SourceOrigin.UNKNOWN, "HaircutTable"),
        ("laod_handin", SourceOrigin.UNKNOWN, "laod_handin"),
    ],
)
def test_the_whole_classification_table_in_one_place(
    text: str, origin: SourceOrigin, ref: str | None
) -> None:
    """The order is qualified, then this plan's stages, then shape, then keyword, then unknown.

    `q1` and `handin` are stages here, which is the whole point of rows 2 and 4: the same two
    strings appear again under 3 and 4 in a plan that has no such stage.
    """
    raw = _pre_1_1_payload()
    raw["stages"].append({"id": "q1", "intent": "Quarterly figures"})
    raw["stages"].append({"id": "handin", "intent": "The hand-in"})
    raw["stages"][1]["sources"] = [text]

    source = ProcessPlan.model_validate(raw).stages[1].sources[0]

    assert source.origin is origin
    assert source.ref == ref


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("Range", SourceOrigin.RANGE),
        ("POWER QUERY", SourceOrigin.POWER_QUERY),
        ("power-query", SourceOrigin.POWER_QUERY),
        ("sql", SourceOrigin.QUERY),
        ("connection", SourceOrigin.QUERY),
        ("upstream", SourceOrigin.STAGE),
        ("hand_in", SourceOrigin.HANDIN),
        ("typed", SourceOrigin.MANUAL),
        ("external_link", SourceOrigin.EXTERNAL),
    ],
)
def test_an_origin_is_read_case_insensitively_and_through_the_obvious_synonyms(
    written: str, expected: SourceOrigin
) -> None:
    """A repair round trip bought by a capital letter is a round trip bought for nothing."""
    assert StageSource(origin=written, ref="x").origin is expected


@pytest.mark.parametrize("value", [42, 3.5, ["Calc!H2:H500"], None])
def test_a_source_that_is_neither_an_object_nor_a_string_says_so_naming_both(value: Any) -> None:
    """Pydantic's own message reads as "strings are not allowed", and a string is allowed.

    "Input should be a valid dictionary or instance of StageSource" is true and misleading:
    somebody hand-editing a plan takes it as licence to rewrite a `sources` list that was fine.
    """
    with pytest.raises(ValidationError, match=r"or the bare string schema 1\.0 wrote"):
        Stage(id="s", intent="x", sources=[value])


def test_an_origin_outside_the_vocabulary_becomes_unknown_rather_than_failing() -> None:
    """Same bargain as an unrecognised Excel pattern: unknown is information, not a rejection."""
    source = StageSource(origin="sharepoint_list", ref="Approvals")
    assert source.origin is SourceOrigin.UNKNOWN
    assert source.ref == "Approvals"


@pytest.mark.parametrize("origin", [SourceOrigin.RANGE, SourceOrigin.STAGE])
def test_the_two_origins_the_schema_can_check_must_say_what_they_read(
    origin: SourceOrigin,
) -> None:
    with pytest.raises(ValidationError, match="must say what it reads"):
        StageSource(origin=origin)


@pytest.mark.parametrize("origin", [SourceOrigin.HANDIN, SourceOrigin.MANUAL, SourceOrigin.UNKNOWN])
def test_an_origin_that_is_the_whole_answer_needs_no_ref(origin: SourceOrigin) -> None:
    assert StageSource(origin=origin).ref is None


def test_a_source_naming_an_unknown_stage_is_caught_the_way_a_dependency_is() -> None:
    """Silent while `sources` was free-form: a typo and a range were the same kind of thing."""
    with pytest.raises(ValidationError, match="reads unknown stage 'laod_handin'"):
        make_draft(
            stages=[
                Stage(id="load_handin", intent="Read the hand-in"),
                Stage(
                    id="apply_haircuts",
                    intent="Look up haircuts",
                    sources=[StageSource(origin=SourceOrigin.STAGE, ref="laod_handin")],
                ),
            ],
            dropped=[],
        )


def test_a_stage_that_lists_itself_as_its_own_source_is_rejected() -> None:
    with pytest.raises(ValidationError, match="lists itself"):
        make_draft(
            stages=[
                Stage(
                    id="a",
                    intent="x",
                    sources=[StageSource(origin=SourceOrigin.STAGE, ref="a")],
                )
            ],
            dropped=[],
        )


def test_validating_one_stage_with_the_plan_in_hand_reads_a_bare_id_as_a_stage() -> None:
    """`model_validate` has no plan context; a verb editing a plan it holds has no excuse."""
    alone = Stage.model_validate(
        {"id": "apply_haircuts", "intent": "x", "sources": ["load_handin"]}
    )
    assert alone.sources == [StageSource(origin=SourceOrigin.UNKNOWN, ref="load_handin")]

    in_plan = Stage.validate_in_plan(
        {"id": "apply_haircuts", "intent": "x", "sources": ["load_handin"]},
        ["load_handin", "apply_haircuts"],
    )
    assert in_plan.sources == [StageSource(origin=SourceOrigin.STAGE, ref="load_handin")]


def test_validating_one_stage_in_a_plan_still_excludes_its_own_id() -> None:
    stage = Stage.validate_in_plan(
        {"id": "adjustments", "intent": "x", "sources": ["adjustments"]}, ["adjustments"]
    )
    assert stage.sources == [StageSource(origin=SourceOrigin.UNKNOWN, ref="adjustments")]


def test_upstream_stage_ids_are_the_stage_sources_and_nothing_else() -> None:
    stage = Stage(
        id="apply_haircuts",
        intent="x",
        sources=[
            StageSource(origin=SourceOrigin.STAGE, ref="load_handin"),
            StageSource(origin=SourceOrigin.RANGE, ref="Ref!A1:D50"),
            StageSource(origin=SourceOrigin.HANDIN),
        ],
    )
    assert stage.upstream_stage_ids == ["load_handin"]


@pytest.mark.parametrize(
    ("source", "rendered"),
    [
        (StageSource(origin=SourceOrigin.RANGE, ref="Calc!H2:H500"), "range Calc!H2:H500"),
        (StageSource(origin=SourceOrigin.STAGE, ref="load_handin"), "stage load_handin"),
        (StageSource(origin=SourceOrigin.QUERY, ref="MonthlyExposures"), "query MonthlyExposures"),
        (StageSource(origin=SourceOrigin.HANDIN), "handin"),
        (StageSource(origin=SourceOrigin.MANUAL), "manual"),
    ],
)
def test_a_source_renders_with_its_origin_leading(source: StageSource, rendered: str) -> None:
    """A bare `Calc!AK:AP` never said whether anybody knew where it came from."""
    assert source.render() == rendered
    assert str(source) == rendered


def test_a_blank_ref_is_no_ref_rather_than_an_empty_one() -> None:
    assert StageSource(origin=SourceOrigin.MANUAL, ref="   ").ref is None


def test_sources_survive_a_json_round_trip_with_their_origins_intact() -> None:
    plan = make_plan(
        draft=make_draft(
            stages=[
                Stage(
                    id="load_exposures",
                    intent="Pull exposures from the warehouse",
                    kind=StageKind.LOAD,
                    sources=[
                        StageSource(origin=SourceOrigin.QUERY, ref="MonthlyExposures"),
                        StageSource(origin=SourceOrigin.MANUAL, ref="Adjustments!B2:B15"),
                    ],
                )
            ],
            dropped=[],
        )
    )
    restored = ProcessPlan.model_validate(json.loads(plan.model_dump_json()))
    assert restored.stages[0].sources == plan.stages[0].sources


def test_a_rejected_drop_is_cleared_by_a_stage_naming_the_range_however_it_was_written() -> None:
    """The approval rule reads the ref now, so both ways of writing that range still clear it."""
    for sources in (["Calc!AK:AP"], [StageSource(origin=SourceOrigin.RANGE, ref="Calc!AK:AP")]):
        plan = make_plan(
            draft=make_draft(
                stages=[Stage(id="keep_it", intent="Still read by Finance", sources=sources)],
                dropped=[
                    DroppedRange(
                        range="Calc!AK:AP",
                        reason="no downstream refs",
                        acknowledged=True,
                        accepted=False,
                    )
                ],
            )
        )
        assert plan.approval_blockers() == []


def test_a_rejected_drop_no_stage_claims_still_blocks_approval() -> None:
    plan = make_plan(
        draft=make_draft(
            stages=[Stage(id="anything_else", intent="Reads something else")],
            dropped=[
                DroppedRange(
                    range="Calc!AK:AP",
                    reason="no downstream refs",
                    acknowledged=True,
                    accepted=False,
                )
            ],
        )
    )
    assert any("no stage lists it as a source" in b for b in plan.approval_blockers())


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
