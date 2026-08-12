"""Judge the planning loop on plans alone, across the whole M1 corpus (PLAN 7.4).

*"If the plans are sensible on five dissimilar workbooks, the code generation is a solvable
problem; if they aren't, no amount of downstream engineering fixes it."*

Every assertion here is **structural or relational**. None of them compares a string to one
model's phrasing, because the same workbook planned twice will not produce the same words and a
test that demanded it would go red on a model upgrade rather than on a regression. What is
asserted instead is the shape of the answer: that the decomposition varies with the workbook,
that the things the model must not do silently are not done silently, and that the honest
answers — a question, a checkpoint, a low convertible score — actually appear where the workbook
warrants them.

The plans themselves are written to disk and the path is printed at the end of the run. Reading
them is the other half of PLAN 7.4, and no assertion here substitutes for it.

**Read the calibration block below before believing a red run.** As of writing, this harness has
never been pointed at a real model: every threshold in it was chosen where a genuine regression
would trip it, not where observed behaviour sits, and none has met live output even once. The
first person to run `pytest -m llm` should read the plans before touching a number.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _sweep import PlanOutcome

from kedge.analysis.model import WorkbookAnalysis
from kedge.plan.model import ProcessPlan, StageKind
from kedge.plan.propose import ProposalRefusedError, ScriptedCompleter, propose_plan
from kedge.plan.store import plan_from_yaml, plan_to_yaml
from kedge.plan.triage import triage

pytestmark = pytest.mark.llm

CLEAN = "clean_pipeline.xlsx"
MOSTLY_MANUAL = "mostly_manual.xlsx"
HOSTILE = "hostile.xlsx"

_AMBIGUOUS = (HOSTILE, MOSTLY_MANUAL)
"""Workbooks nobody could plan without asking something.

`hostile.xlsx` has a genuine three-cell cycle, an unresolvable external link, an `INDIRECT` whose
target is not statically knowable and a subtotal row buried in the middle of the data.
`mostly_manual.xlsx` has fourteen typed override rows with no formula and no provenance.
"""

_LAZY_DROP_REASONS = {"unused", "not used", "dead", "empty", "unnecessary", "not needed", "n/a"}
"""What the system prompt explicitly rejects: "'Unused' is not a reason"."""


# =============================================================================
# CALIBRATION
# =============================================================================
#
# Three judgement calls, gathered here rather than buried in the assertions that use them, and
# every one of them **unproven**: no plan produced by a real model has been generated or read, so
# none of these numbers has been checked against live output even once. They are deliberately set
# where a genuine regression trips them rather than where current behaviour sits, which is the
# right place to start and the wrong place to leave them.
#
# After the first live sweep: read `index.md` and the eight `plan.txt` files FIRST, then move a
# number only where the plans are good and the number disagrees with them. A threshold relaxed
# because it went red is a threshold that no longer tests anything.
#
# Not listed here, because they are not thresholds: the operation-id check below is a strict
# subset and stays strict — a stage written against an operation the analysis never found is a
# defect at any calibration — and the lazy-drop-reason set above is a fixed vocabulary rather
# than a number.

MIN_DISTINCT_STAGE_COUNTS = 3
"""How many different stage counts the corpus must produce before it stops reading as a template.

PLAN 2.2: "a workbook that wants four stages gets four; one that wants fifteen gets fifteen."
Eight dissimilar workbooks landing on two shapes is the failure this harness exists to catch. A
subset sweep is never asked to be more varied than it can be; see the test.
"""

MIN_DROP_REASON_WORDS = 3
"""Words a drop reason needs before it is a reason rather than a restatement.

"Unused" is one word and is rejected outright by name; "no downstream references" is three and is
something a reviewer can agree or disagree with. A word count rather than a character count
because the property is content, not length.
"""

COMPLEX_ENOUGH_TO_EXPECT_A_QUESTION = 0.35
"""Triage complexity above which a plan with no open questions must at least raise a warning.

PLAN 2.2: "an empty one on a complex workbook is itself suspicious". This is the cut-off for
"complex", and it is the number most likely to be wrong: it was chosen without ever having seen
the complexity scores the corpus actually produces alongside real plans.
"""


def _planned(outcomes: dict[str, PlanOutcome]) -> dict[str, ProcessPlan]:
    """The workbooks that produced a plan, keyed by filename."""
    return {name: outcome.plan for name, outcome in outcomes.items() if outcome.plan is not None}


def _require(outcomes: dict[str, PlanOutcome], filename: str) -> ProcessPlan:
    """The plan for one named fixture, or a skip when it was not part of this sweep."""
    outcome = outcomes.get(filename)
    if outcome is None:
        pytest.skip(f"{filename} was not part of this sweep")
    if outcome.plan is None:
        pytest.fail(f"no plan was produced for {filename}: {outcome.error}")
    return outcome.plan


# =============================================================================
# THE SWEEP RAN AT ALL
# =============================================================================


def test_every_corpus_workbook_produced_a_plan(corpus_plans: dict[str, PlanOutcome]) -> None:
    """The first question, and the one that makes every other failure here readable."""
    failed = {name: outcome.error for name, outcome in corpus_plans.items() if outcome.plan is None}
    assert not failed, "the planning loop produced no plan for:\n" + "\n".join(
        f"  {name}: {error}" for name, error in failed.items()
    )


def test_every_plan_survives_the_round_trips_it_is_persisted_through(
    corpus_plans: dict[str, PlanOutcome],
) -> None:
    """Not `ProcessPlan` validation, which cannot fail here and would be a vacuous thing to
    assert: `propose_plan` returns a model that has already passed it, and the stage DAG and the
    duplicate-id check are validators that ran on construction.

    What real model output genuinely can break is serialisation, which is why both round trips
    below are the ones kedge actually depends on — `store.py` writes the plan as YAML beside the
    notebook, and the agent moves it as JSON. A stage id YAML reads back as a bool, an unpaired
    surrogate in a question, a reason long enough to be line-folded: none of those are reachable
    from a hand-written fixture and all of them are reachable from a model.
    """
    for name, plan in _planned(corpus_plans).items():
        assert ProcessPlan.model_validate_json(plan.model_dump_json()) == plan, (
            f"{name} does not survive the JSON round trip the agent moves it through"
        )
        assert plan_from_yaml(plan_to_yaml(plan)) == plan, (
            f"{name} does not survive the YAML round trip the plan store persists it through"
        )


def test_every_plan_arrives_unapproved(corpus_plans: dict[str, PlanOutcome]) -> None:
    """Nothing the model produces reaches a notebook without a human (PLAN 2.2)."""
    for name, plan in _planned(corpus_plans).items():
        assert not plan.approval.approved, f"{name} arrived pre-approved"


# =============================================================================
# NO TEMPLATE: THE DECOMPOSITION VARIES WITH THE WORKBOOK
# =============================================================================


def test_the_stage_count_varies_across_dissimilar_workbooks(
    corpus_plans: dict[str, PlanOutcome],
) -> None:
    """PLAN 2.2: "a workbook that wants four stages gets four; one that wants fifteen gets
    fifteen. No template."

    A constant stage count across eight dissimilar workbooks is the templating failure this whole
    harness exists to catch, and it is invisible from any single plan.
    """
    plans = _planned(corpus_plans)
    if len(plans) < 3:
        pytest.skip(f"only {len(plans)} plan(s) in this sweep; variance needs at least three")
    # A subset sweep cannot be asked for more variety than it has room for: three workbooks can
    # differ in at most three ways and demanding all three be distinct makes KEDGE_LLM_FIXTURES
    # unusable for iterating.
    required = max(2, min(MIN_DISTINCT_STAGE_COUNTS, len(plans) - 1))
    counts = {name: len(plan.stages) for name, plan in plans.items()}
    assert len(set(counts.values())) >= required, (
        f"only {len(set(counts.values()))} distinct stage count(s) across "
        f"{len(counts)} dissimilar workbooks, which reads as a template rather than a plan: "
        f"{counts} (threshold: MIN_DISTINCT_STAGE_COUNTS={MIN_DISTINCT_STAGE_COUNTS})"
    )


def test_no_two_workbooks_get_the_same_decomposition(
    corpus_plans: dict[str, PlanOutcome],
) -> None:
    """Stage ids are free-form and name the business step, so two unrelated workbooks producing
    the same list of them means the model is filling in a shape rather than reading a workbook."""
    seen: dict[tuple[str, ...], str] = {}
    for name, plan in _planned(corpus_plans).items():
        key = tuple(plan.stage_ids)
        assert key not in seen, (
            f"{name} and {seen[key]} were given identical decompositions: {list(key)}"
        )
        seen[key] = name


def test_stages_are_named_after_business_steps_not_after_cells(
    corpus_plans: dict[str, PlanOutcome],
) -> None:
    """A stage called `Calc!D2:D501` is a transliteration, which is what the plan exists to
    prevent (PLAN 2.2: "47 cells mirroring 47 columns")."""
    for name, outcome in corpus_plans.items():
        if outcome.plan is None:
            continue
        sheets = {sheet.name.lower() for sheet in outcome.analysis.sheets}
        for stage in outcome.plan.stages:
            assert "!" not in stage.id, f"{name}: stage {stage.id!r} is a range, not a step"
            assert stage.id.lower() not in sheets, (
                f"{name}: stage {stage.id!r} is named after a sheet, not after what it does"
            )


# =============================================================================
# TRIAGE, AND THE HONEST REFUSAL
# =============================================================================


@pytest.mark.xfail(
    strict=False,
    reason=(
        "triage scores mostly_manual.xlsx at 1.00, the same as clean_pipeline.xlsx. Its typed "
        "rows sit on Input and Overrides, which are classified `data`, and "
        "_check_manual_ratio only counts calculation/output/unknown sheets — where the "
        "workbook has 24 formula cells and fewer than 50 populated cells, below the threshold "
        "that makes the ratio meaningful. manifest.py states the expectation this asserts: "
        "'Triage should score this workbook as only partly convertible (PLAN section 2.2).' "
        "Owned by kedge/plan/triage.py, which this workstream does not own."
    ),
)
def test_mostly_manual_triages_materially_below_clean_pipeline(
    corpus_analyses: dict[str, WorkbookAnalysis],
) -> None:
    """Triage is deterministic and needs no model, but it belongs in this harness because PLAN
    7.4 asks for triage and propose judged together over the same corpus. It therefore depends on
    the analyses rather than on the plans, and runs whether or not an endpoint is configured."""
    for name in (CLEAN, MOSTLY_MANUAL):
        if name not in corpus_analyses:
            pytest.skip(f"{name} was not part of this sweep")
    clean = triage(corpus_analyses[CLEAN]).convertible
    manual = triage(corpus_analyses[MOSTLY_MANUAL]).convertible
    assert manual < clean - 0.1, (
        f"a workbook that is mostly typed values ({manual:.2f}) scored no lower than a clean "
        f"formula pipeline ({clean:.2f})"
    )


def test_the_plan_for_mostly_manual_claims_less_than_the_plan_for_clean_pipeline(
    corpus_plans: dict[str, PlanOutcome],
) -> None:
    """The model's own judgement, which is what PLAN 7.4 is actually asking about.

    `mostly_manual.xlsx` is fourteen typed override rows and a thin calculation layer;
    `clean_pipeline.xlsx` is 4,006 formula cells compressing to fourteen operations. A model that
    claims to convert as much of the first as the second has not read either.
    """
    clean = _require(corpus_plans, CLEAN)
    manual = _require(corpus_plans, MOSTLY_MANUAL)
    assert manual.assessment.convertible < clean.assessment.convertible, (
        f"the plan claims to convert {manual.assessment.convertible:.2f} of a mostly-manual "
        f"workbook against {clean.assessment.convertible:.2f} of a clean formula pipeline"
    )


def test_a_stop_verdict_is_refused_unless_the_caller_forces_it(
    corpus_plans: dict[str, PlanOutcome],
) -> None:
    """The sweep forces STOP workbooks so their plans can be read; the refusal must still hold."""
    stopped = [outcome for outcome in corpus_plans.values() if outcome.triage.should_stop]
    if not stopped:
        pytest.skip("no corpus workbook triaged to STOP in this sweep")
    for outcome in stopped:
        completer = ScriptedCompleter([])
        with pytest.raises(ProposalRefusedError):
            propose_plan(outcome.analysis, completer=completer, triage_result=outcome.triage)
        assert not completer.requests, (
            f"{outcome.filename}: a refused workbook still spent a model call"
        )


# =============================================================================
# THE THINGS THAT MUST NOT HAPPEN SILENTLY
# =============================================================================


def test_a_workbook_of_manual_overrides_yields_at_least_one_checkpoint_stage(
    corpus_plans: dict[str, PlanOutcome],
) -> None:
    """PLAN 2.2: forcing a judgement call into code either fabricates logic that was never there
    or silently drops a control. `mostly_manual.xlsx` carries fourteen typed override rows with no
    formula and no provenance, on a sheet called `Overrides`.

    Note when this fails: the analyser does **not** currently raise the `manual_override_block`
    finding that `manifest.py` declares for this fixture and calls "the checkpoint-stage trigger
    from PLAN section 2.2". The model is therefore inferring the checkpoint from the sheet's shape
    — 61 typed values, zero formulas — rather than being told. Check whether the missing finding
    is the cause before blaming the prompt.
    """
    plan = _require(corpus_plans, MOSTLY_MANUAL)
    checkpoints = [stage for stage in plan.stages if stage.kind is StageKind.CHECKPOINT]
    assert checkpoints, (
        "no checkpoint stage on a workbook whose overrides sheet is fourteen rows of typed "
        f"numbers; the plan is: {[(s.id, s.kind.value) for s in plan.stages]}"
    )
    for stage in checkpoints:
        assert stage.effective_checkpoint().question.strip(), (
            f"checkpoint {stage.id!r} asks the user nothing"
        )


def test_every_dropped_range_carries_a_reason_a_reviewer_could_accept(
    corpus_plans: dict[str, PlanOutcome],
) -> None:
    """A drop the user must acknowledge is only acknowledgeable if it says why. The system prompt
    is explicit that "Unused" alone is not a reason."""
    for name, plan in _planned(corpus_plans).items():
        for drop in plan.dropped:
            reason = drop.reason.strip()
            assert reason.lower().rstrip(".") not in _LAZY_DROP_REASONS, (
                f"{name}: {drop.range} is dropped for {reason!r}, which is a restatement rather "
                f"than a reason"
            )
            assert len(reason.split()) >= MIN_DROP_REASON_WORDS, (
                f"{name}: {drop.range} is dropped for {reason!r} (threshold: "
                f"MIN_DROP_REASON_WORDS={MIN_DROP_REASON_WORDS})"
            )
            assert not drop.acknowledged, (
                f"{name}: {drop.range} arrived pre-acknowledged; only a user may acknowledge a drop"
            )


def test_open_questions_are_raised_where_the_workbook_is_genuinely_ambiguous(
    corpus_plans: dict[str, PlanOutcome],
) -> None:
    """PLAN 2.2 and 6.2: an empty `open_questions` on a complex workbook is itself suspicious,
    because a model that asks nothing has usually invented a purpose for something."""
    for name in _AMBIGUOUS:
        plan = _require(corpus_plans, name)
        assert plan.open_questions, (
            f"{name} produced no open questions at all, on a workbook nobody could plan without "
            f"asking something"
        )
        for question in plan.open_questions:
            assert question.question.strip(), f"{name} raised a blank question"
            assert not question.answered, f"{name} answered its own question"


def test_the_empty_questions_warning_fires_on_any_complex_plan_that_asked_nothing(
    corpus_plans: dict[str, PlanOutcome],
) -> None:
    """The safety net rather than the behaviour: wherever a plan on a complex workbook did come
    back with no questions, review must be told so a human looks.

    The safety net is only reachable when a plan asked nothing, which on a good run is never. A
    green result here therefore has to distinguish "the net held" from "the net was never
    touched", so the reached plans are counted and a sweep that reached none skips rather than
    reporting a pass it did not earn.
    """
    reached: list[str] = []
    for name, outcome in corpus_plans.items():
        if outcome.plan is None or outcome.plan.open_questions:
            continue
        if outcome.triage.complexity < COMPLEX_ENOUGH_TO_EXPECT_A_QUESTION:
            continue
        reached.append(name)
        warning = outcome.plan.open_questions_warning(complexity=outcome.triage.complexity)
        assert warning is not None, f"{name} asked nothing and review said nothing about it"
    if not reached:
        pytest.skip(
            "no plan in this sweep combined a complexity of at least "
            f"{COMPLEX_ENOUGH_TO_EXPECT_A_QUESTION} with an empty open_questions, so the warning "
            "was never reachable and nothing here was checked"
        )


def test_no_plan_references_an_operation_the_analysis_never_found(
    corpus_plans: dict[str, PlanOutcome],
) -> None:
    """Operation ids are the link back to the facts and what reconciliation later compares
    against. An invented one is a stage written against a region that does not exist.

    Strict on purpose, and not a calibration knob: there is no acceptable rate of invented ids,
    and a near-miss ("net_exposure" against the analysis's "net_exposure_calc") is exactly the
    failure a looser check would wave through.
    """
    for name, outcome in corpus_plans.items():
        if outcome.plan is None:
            continue
        known = {operation.id for operation in outcome.analysis.operations}
        claimed = {op for stage in outcome.plan.stages for op in stage.operations}
        assert claimed <= known, (
            f"{name}: stages reference operation id(s) not in the analysis: "
            f"{sorted(claimed - known)}. The analysis found: {sorted(known)}"
        )


def test_a_checkpoint_never_claims_a_confidence_it_cannot_have(
    corpus_plans: dict[str, PlanOutcome],
) -> None:
    """`n/a` is a positive statement about a human judgement; `high` on one is a category error
    the schema tolerates and a reviewer should not."""
    for name, plan in _planned(corpus_plans).items():
        for stage in plan.checkpoints:
            assert stage.confidence.value in ("n/a", "unknown"), (
                f"{name}: checkpoint {stage.id!r} claims confidence {stage.confidence.value!r}"
            )


# =============================================================================
# THE ARTIFACT PLAN 7.4 ASKS A HUMAN TO READ
# =============================================================================


def test_the_plans_are_written_somewhere_a_human_can_read_them(
    corpus_plans: dict[str, PlanOutcome], artifact_dir: Path
) -> None:
    """The judgement step is a person reading these. A sweep that asserts and discards has done
    the cheap half and skipped the point."""
    index = artifact_dir / "index.md"
    assert index.is_file()
    assert "Plan judgement sweep" in index.read_text(encoding="utf-8")

    for name, outcome in corpus_plans.items():
        directory = artifact_dir / name.removesuffix(".xlsx")
        assert (directory / "triage.txt").is_file(), f"{name}: no triage written"
        if outcome.plan is None:
            assert (directory / "FAILED.txt").is_file()
            assert not (directory / "plan.txt").exists(), (
                f"{name}: a stale plan from an earlier run is sitting beside this run's failure"
            )
            continue
        rendered = (directory / "plan.txt").read_text(encoding="utf-8")
        assert "STAGES" in rendered
        assert "OPEN QUESTIONS" in rendered
        assert (directory / "plan.yaml").is_file()
        captured = sorted((directory / "responses").glob("response-*.json"))
        assert captured, (
            f"{name}: the raw model output was not captured, so the run cannot be replayed"
        )
        assert len(captured) == outcome.attempts, (
            f"{name}: {outcome.attempts} model response(s) this run but {len(captured)} on disk, "
            f"so the directory is a mix of two runs"
        )
