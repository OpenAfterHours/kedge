"""The per-model planning sweep, exercised end to end with no endpoint and no keyring.

Three groups of tests, and the first two matter more than the third.

**The denominator.** ``harness.grade.grade`` drives a notebook and grades both tiers, so handing
it a model's plan alongside the committed reference notebook produces a near-perfect total in
which forty-five of the points were earned by a conversion a human wrote. That is the single worst
thing this deliverable could do -- it would report exactly the false confidence the sweep exists to
remove, and it would be almost impossible to spot in a tidy table. So the tests assert what is
*absent*: no deterministic item id appears anywhere in a sweep report or its JSON, the available
mark is the structural tier's, and the two structural items that need something a plan cannot
supply skip with a reason rather than passing quietly.

Every mark below is **derived from ``expected.yaml``**, never typed -- see
:data:`NEEDS_MORE_THAN_A_PLAN`, which also reconciles the two structural totals this repo now
prints. A sweep grades a plan and reports the tier out of 24; an offline run drives the notebook
as well and reports it out of 26. Both are right, the gap is one named item, and the reason to
write that down is that two different totals for the same tier is exactly how a number goes wrong
quietly.

**Negative controls.** A sweep that has only ever seen a good run is a sweep whose polarity nobody
has checked, and the failure this project has already met once was a *misattribution*: an endpoint
refusing an explicit temperature on every request, read as "structured output unsupported", with
no plans produced anywhere and the diagnosis pointed at the models (``tests/llm/README.md``). So
every failure the table can name is provoked here -- through the real ``kedge.plan.propose`` code
path, with a stubbed SDK client raising what a real endpoint raises -- and each is asserted to
land in its own row with its own attribution. The distinction the whole history turns on is
asserted directly: a plan that validates and is *wrong* is the model's judgement failing and reads
as ``none``; a plan that never arrived because the endpoint refused the schema is not.

**Skips.** An unconfigured leg produces a row, contributes no score, no tokens and no seconds, and
does not make the sweep fail. Folding it into a failure blames a model nobody could reach; folding
its zeros into a mean crowns it the fastest and cheapest in the sweep.

Everything is scripted or stubbed. The keyring is patched where it is reached at all, so this file
passes on a machine with no backend, no endpoint and no network.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import yaml
from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    RateLimitError,
)

from kedge.plan.model import DroppedRange, ProcessPlan
from kedge.plan.propose import OpenAICompleter, ScriptedCompleter
from kedge.plan.triage import TriageVerdict

EVAL_ROOT = Path(__file__).resolve().parents[2] / "evals"
if str(EVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(EVAL_ROOT))

from adjustment_signoff import case as adjustment_case  # noqa: E402
from harness.grade import load_plan  # noqa: E402
from harness.live import Failure, ModelSpec, ResolvedModel  # noqa: E402
from harness.model import Outcome  # noqa: E402
from harness.sweep import (  # noqa: E402
    TIER,
    Bench,
    NotebookRequiredError,
    Price,
    as_json,
    grade_structural,
    load_analysis,
    render,
    sweep,
)

URL = "https://example.invalid/v1"


def _weights(tier: str) -> dict[str, int]:
    """Every item's weight in one tier of ``expected.yaml``, read rather than transcribed.

    The rubric is the only place these figures exist, and a test that retypes one is a second copy
    of it. Not hypothetical: the structural tier grew from 19 points to 24 when it stopped grading
    the presence of a plan's fields and started grading the shape the scaffolder consumes, then to
    27 for the item that grades *where* a hand-in is emitted. Six tests in this file went red on
    their literals the first time, rather than on anything about a sweep.
    """
    rubric = yaml.safe_load(adjustment_case.RUBRIC.read_text(encoding="utf-8"))
    return {str(entry["id"]): int(entry.get("weight", 1)) for entry in rubric[tier]}


STRUCTURAL_WEIGHTS = _weights(TIER)

DETERMINISTIC_POINTS = sum(_weights("deterministic").values())
"""The marks a sweep pointedly does not award. Printed above the table, and asserted below."""

NEEDS_MORE_THAN_A_PLAN = ("does_not_trust_the_impact_summary", "consults_the_knowledge_pack")
"""The two structural items a plan-only leg cannot grade -- named, not counted, because they are
ungradeable for two different reasons.

``does_not_trust_the_impact_summary`` reads a *driven notebook*: the Sign-off tab's stale figures
are either carried into the output or they are not, and a plan cannot say which.
``consults_the_knowledge_pack`` skips for everybody, sweep or not, because no pack describes
``fin.accruals``.

Naming them is also the answer to a figure that would otherwise disagree with itself across the
repo. A full offline run -- ``evals/run.py adjustment_signoff --plan ...``, which drives the
notebook -- reports the structural tier out of **26**: everything declared, less the knowledge
pack. A sweep reports **24**, because the notebook-reading item skips as well. Two different
structural totals, both correct, and the difference is exactly these two entries.

Both halves are pinned. This tuple is asserted to be the *whole* skip set by
:func:`test_the_items_a_plan_cannot_answer_skip_with_a_reason`, so a new rubric item that also
needs a notebook goes red here rather than silently shrinking :data:`FULL_MARKS`; and the driven
figure is pinned exactly by ``TIER_SCORES`` in ``tests/unit/test_evals_convert.py``."""

FULL_MARKS = sum(STRUCTURAL_WEIGHTS.values()) - sum(
    STRUCTURAL_WEIGHTS[item] for item in NEEDS_MORE_THAN_A_PLAN
)
"""What the structural tier is worth to a plan alone: everything declared, less the two above.

Derived, never typed, so a rubric change moves it here and every test below goes on asserting the
thing it was written to assert. It was a literal ``16``, and the rubric moved underneath it."""

DROPPED_COLUMN_COSTS = STRUCTURAL_WEIGHTS["does_not_drop_the_sql_column"]
"""What a plan pays for deleting the step that changes the data -- the eval's sharpest single
discrimination, and the only item this file's negative control makes a model fail."""

DROPS_THE_COLUMN = FULL_MARKS - DROPPED_COLUMN_COSTS
"""What that plan scores: full marks less that one item, and nothing else moving.

Kept as arithmetic rather than as a number because the arithmetic is the claim. A bare ``18`` says
a plan lost some marks; ``FULL_MARKS - DROPPED_COLUMN_COSTS`` says which one it lost, and a
mutation that made the plan fail a *second* item fails this test instead of quietly agreeing with
whatever the new total happens to be."""


# ── the bench, and the plans a leg can be scripted to produce ────────────────


@pytest.fixture(scope="module")
def bench() -> Bench:
    """One analysis of the eval workbook, shared: two models graded against two different
    analyses of the same file are not two measurements of the same thing."""
    return Bench.load()


@pytest.fixture(scope="module")
def reference_plan() -> ProcessPlan:
    return load_plan(adjustment_case.CASE_DIR / "plan.yaml")


@pytest.fixture(scope="module")
def good(reference_plan: ProcessPlan) -> str:
    """The response of a model that plans this workbook correctly."""
    return reference_plan.to_draft().model_dump_json()


@pytest.fixture(scope="module")
def drops_the_column(reference_plan: ProcessPlan) -> str:
    """The response of a model whose plan validates and deletes the step that changes the data.

    The sharpest discrimination the eval has, and the one shape of failure that really is the
    model's judgement rather than the plumbing's.
    """
    mutated = reference_plan.model_copy(
        update={
            "dropped": [
                *reference_plan.dropped,
                DroppedRange(
                    range="Adjustment!G17:G92",
                    reason="dead region: nothing in the workbook reads it",
                ),
            ]
        }
    )
    return mutated.to_draft().model_dump_json()


# ── stand-ins for an endpoint ────────────────────────────────────────────────


def _answer(content: str, prompt: int = 0, completion: int = 0) -> SimpleNamespace:
    usage = SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=completion, prompt_tokens_details=None
    )
    envelope = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage
    )
    if not prompt and not completion:
        # The endpoint that volunteers nothing: llama.cpp, a thin proxy, an internal gateway.
        del envelope.usage
    return envelope


def _refusal(message: str, body: dict[str, Any] | None = None) -> BadRequestError:
    response = httpx.Response(400, request=httpx.Request("POST", URL))
    return BadRequestError(message, response=response, body=body)


def _temperature_refusal() -> BadRequestError:
    """What a reasoning model answers an explicit temperature with, verbatim."""
    message = (
        "Unsupported value: 'temperature' does not support 0.2 with this model. "
        "Only the default (1) value is supported."
    )
    body = {"error": {"message": message, "param": "temperature", "code": "unsupported_value"}}
    return _refusal(message, body)


def _status(kind: type, message: str, code: int = 429) -> Any:
    return kind(
        message, response=httpx.Response(code, request=httpx.Request("POST", URL)), body=None
    )


def _stubbed(*outcomes: Any) -> OpenAICompleter:
    """A real ``OpenAICompleter`` with its SDK client replaced -- the seam being faked is the
    transport, so the negotiation and the metering under test are the shipped ones."""
    completer = OpenAICompleter(base_url=URL, api_key="k", model="m")
    remaining = list(outcomes)

    def create(**_: Any) -> Any:
        outcome = remaining.pop(0) if remaining else remaining
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    completer._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    return completer


def _leg(completer: Any = None, failure: Failure = Failure.NONE, detail: str = "") -> Any:
    """A resolver for a one-model sweep."""

    def resolve(spec: ModelSpec) -> ResolvedModel:
        return ResolvedModel(spec=spec, completer=completer, failure=failure, detail=detail)

    return resolve


def _legs(table: dict[str, Any]) -> Any:
    """A resolver for a many-model sweep, keyed by model id."""

    def resolve(spec: ModelSpec) -> ResolvedModel:
        built = table[spec.model]
        if isinstance(built, Failure):
            return ResolvedModel(spec=spec, failure=built, detail=f"{built.value} for {spec.name}")
        return ResolvedModel(spec=spec, completer=built)

    return resolve


def _one(bench: Bench, completer: Any, *, repeats: int = 1, model: str = "m", **kwargs: Any) -> Any:
    return sweep(
        [ModelSpec(model=model)],
        bench=bench,
        repeats=repeats,
        resolve=_leg(completer),
        **kwargs,
    )


def _ids(report: Any) -> list[str]:
    return [
        item.id
        for leg in report.legs
        for repeat in leg.repeats
        if repeat.tier is not None
        for item in repeat.tier.items
    ]


# =============================================================================
# THE DENOMINATOR, AND THE POINTS THAT ARE NOT ON OFFER
# =============================================================================


def test_a_plan_is_scored_out_of_the_structural_tiers_own_denominator(
    bench: Bench, good: str
) -> None:
    report = _one(bench, ScriptedCompleter([good]))

    leg = report.legs[0]
    assert leg.outcome is Outcome.PASS
    assert leg.scores == (FULL_MARKS,)
    assert leg.available == FULL_MARKS
    assert report.available == FULL_MARKS
    declared = sum(STRUCTURAL_WEIGHTS.values())
    withheld = declared - FULL_MARKS
    assert report.points_declared == declared, (
        f"the tier declares {declared}; {withheld} of those points sit on "
        f"{', '.join(NEEDS_MORE_THAN_A_PLAN)}, which a plan alone cannot answer"
    )


def test_no_deterministic_mark_is_ever_credited_to_a_model(bench: Bench, good: str) -> None:
    """The trap this module was written around.

    ``grade(case, notebook=..., plan=...)`` drives a notebook and grades both tiers. Given the
    committed reference conversion and a model's plan it prints a near-perfect total, forty-five
    points of which a human earned. A sweep that did that would report the exact false confidence
    it exists to remove, in a table nobody could see the mistake in.
    """
    report = _one(bench, ScriptedCompleter([good]))

    graded = set(_ids(report))
    assert graded.isdisjoint(adjustment_case.DETERMINISTIC), (
        "a deterministic item was graded without a notebook being driven"
    )
    assert graded <= set(adjustment_case.STRUCTURAL)
    assert report.available is not None
    committed = DETERMINISTIC_POINTS + sum(STRUCTURAL_WEIGHTS.values())
    assert report.available < committed, (
        f"{committed} is what the whole rubric declares over a driven notebook, and it is the "
        f"committed reference run's number, not a model result"
    )

    document = json.loads(as_json(report))
    assert document["tier"] == TIER
    assert document["not_graded"]["tier"] == "deterministic"
    assert document["not_graded"]["points"] == DETERMINISTIC_POINTS
    assert "totals_to_the_penny" not in as_json(report)


def test_the_items_a_plan_cannot_answer_skip_with_a_reason(bench: Bench, good: str) -> None:
    """Two items sit outside the denominator, and both say why rather than vanishing."""
    report = _one(bench, ScriptedCompleter([good]))

    skipped = {item.id: item.detail for item in report.ungradeable}
    assert set(skipped) == set(NEEDS_MORE_THAN_A_PLAN)
    assert "harness/convert.py" in skipped["does_not_trust_the_impact_summary"]
    assert "knowledge pack" in skipped["consults_the_knowledge_pack"]


def test_a_grader_reaching_for_a_notebook_skips_rather_than_reading_an_empty_one(
    bench: Bench, reference_plan: ProcessPlan
) -> None:
    """A permissive stand-in would hand the grader an empty namespace, and an empty namespace
    passes ``does_not_trust_the_impact_summary``: there are no stale figures in a notebook that
    does not exist. That is a pass awarded for nothing, which is the reconciliation sin."""
    tier = grade_structural(bench, reference_plan)

    item = next(item for item in tier.items if item.id == "does_not_trust_the_impact_summary")
    assert item.outcome is Outcome.SKIP
    assert "driven notebook" in item.detail
    assert tier.available == FULL_MARKS


def test_a_leg_the_sweep_could_not_grade_in_full_never_prints_full_marks(
    bench: Bench,
    good: str,
    reference_plan: ProcessPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal must not arrive as a PASS at a denominator only one leg has.

    A plan the scaffolder refuses used to make items *skip* that every other leg was graded on.
    The leg then earned everything that was left and rendered, verbatim, ``PASS 18/18`` -- directly
    under a header saying legs are scored out of 21, with the line saying kedge could not build a
    notebook from that plan appearing nowhere in the report. That is the reconciliation sin in a
    table: a measurement nobody took, printed as a result somebody got.

    It is now fixed one layer lower down, which is why this test reads the way it does. A plan the
    scaffolder refuses is the *plan's* defect, so those items are ``BLOCKED`` rather than skipped
    and keep their weight: the leg is scored out of the whole tier like everybody else and simply
    earns less of it. The denominators no longer diverge, so there is nothing left here for
    :meth:`LegResult.score_cell`'s short-leg marker to mark --
    :func:`test_a_leg_scored_on_less_than_the_sweep_says_so_in_its_cell` covers that guard
    directly, because a guard whose only exercise has been deleted is a guard nobody is watching.

    What still has to be true: the refusal reaches the graders, the leg is scored out of the full
    tier, the score renders against the sweep's denominator, the preamble's list of what nobody
    can be graded on is not taken from this leg, and the reason is named under "Why".
    """
    from kedge.notebook import scaffold

    # The mechanism, provoked rather than hunted for. Every plan shape that once made
    # `build_cells` raise has since been made to build -- unbalanced quotes and newlines in a
    # hand-in `ref`, ids colliding with head cells and with reserved satellites -- which is good
    # news for the scaffolder and useless as a fixture: a control that skips because its input
    # stopped being defective proves nothing. So the refusal is injected at the seam the graders
    # actually call, for the second leg only, and the plan itself stays a perfectly good one.
    marked = reference_plan.model_copy(update={"summary": f"{reference_plan.summary} [refuse]"})
    original = scaffold.build_cells

    def build_cells(plan: Any, **kwargs: Any) -> Any:
        if "[refuse]" in (plan.summary or ""):
            msg = "cell 'post_adjustment_input' would not parse"
            raise scaffold.ScaffoldError(msg)
        return original(plan, **kwargs)

    monkeypatch.setattr(scaffold, "build_cells", build_cells)
    report = sweep(
        [ModelSpec(model="good-model"), ModelSpec(model="unscaffoldable")],
        bench=bench,
        repeats=1,
        resolve=_legs(
            {
                "good-model": ScriptedCompleter([good]),
                "unscaffoldable": ScriptedCompleter([marked.to_draft().model_dump_json()]),
            }
        ),
    )

    healthy, short = report.legs
    # The control used to be `short.available < FULL_MARKS`: the refusal was detected by the
    # denominator shrinking under it. It does not shrink any more -- a plan that will not scaffold
    # is the plan's defect, so those items are BLOCKED and keep their points -- and the refusal is
    # now looked for where it actually is, in the reasons the graders gave.
    refused = [
        item for repeat in short.repeats if repeat.tier is not None for item in repeat.tier.blocked
    ]
    assert refused and any("would not parse" in item.detail for item in refused), (
        "the injected refusal did not reach a grader, so this control measures nothing"
    )
    assert short.available == FULL_MARKS, (
        "a plan that will not scaffold must be scored out of the whole tier, not out of the part "
        "that survived it"
    )
    assert report.available == FULL_MARKS
    assert healthy.score_cell(report.available) == f"{FULL_MARKS}/{FULL_MARKS}"
    cell = short.score_cell(report.available)
    assert cell.endswith(f"/{FULL_MARKS}"), cell
    assert short.scores and max(short.scores) < FULL_MARKS, (
        f"the refused plan still printed full marks: {cell}"
    )

    table = render(report)
    # The reason has to be in the report. It used to arrive as the short-leg line; the points now
    # come off the score instead, so the sentence saying why comes with the blocked items.
    assert "would not parse" in table, table
    assert "BLOCKED" in table, table
    # And the preamble must not adopt the short leg's skips as everybody's.
    assert {item.id for item in report.ungradeable} == set(NEEDS_MORE_THAN_A_PLAN)


def test_a_leg_scored_on_less_than_the_sweep_says_so_in_its_cell() -> None:
    """The short-leg marker, exercised directly rather than through a scaffold refusal.

    A refusal no longer shortens a denominator -- it blocks items, which keep their weight -- so
    the sweep test above cannot reach this branch any more. The branch is still right and still
    reachable: a genuine ``SKIP`` leaves the denominator, and one leg skipping something the
    others were graded on is exactly the case that once printed ``18/18 PASS`` under a header
    saying 21.
    """
    from harness.live import MeteredCall
    from harness.model import ItemResult, Outcome, TierResult
    from harness.sweep import LegResult, ModelSpec, RepeatResult

    short = LegResult(
        spec=ModelSpec(model="short"),
        repeats=(
            RepeatResult(
                spec=ModelSpec(model="short"),
                repeat=1,
                call=MeteredCall(),
                tier=TierResult(
                    name="structural",
                    items=(
                        ItemResult(id="graded", outcome=Outcome.PASS, weight=18),
                        ItemResult(id="ungradeable", outcome=Outcome.SKIP, weight=3),
                    ),
                ),
            ),
        ),
    )

    assert short.available == 18
    assert short.score_cell() == "18/18", "its own denominator, when nobody asked for another"
    assert short.score_cell(21) == "18/21 (3 unmeasured)"
    assert "3 point(s) could not be measured on this plan" in " ".join(short.why(21))


def test_the_stand_in_notebook_refuses_every_read() -> None:
    from harness.sweep import _NoNotebook

    with pytest.raises(NotebookRequiredError, match="definitions"):
        _NoNotebook().definitions  # noqa: B018 - the read is the assertion


# =============================================================================
# THE MODEL'S JUDGEMENT FAILING, WHICH IS NOT AN INTEGRATION FAILURE
# =============================================================================


def test_a_plan_that_drops_the_sql_column_loses_marks_and_is_attributed_to_the_model(
    bench: Bench, drops_the_column: str
) -> None:
    """The one failure in the table that really is about the model, and it must not look like the
    others: the endpoint answered, first time, and what came back deletes the step that changes
    the data."""
    report = _one(bench, ScriptedCompleter([drops_the_column]))

    leg = report.legs[0]
    assert leg.outcome is Outcome.FAIL
    assert leg.scores == (DROPS_THE_COLUMN,)
    assert leg.attributions == {Failure.NONE: 1}
    assert leg.attribution_cell() == "answered first time"
    assert any("does_not_drop_the_sql_column FAIL" in line for line in leg.why())
    assert not report.ok
    assert report.exit_code() == 1


def test_repeats_that_disagree_are_reported_as_a_range_and_never_as_a_mean(
    bench: Bench, good: str, drops_the_column: str
) -> None:
    """A model that scores full marks, then drops the SQL column, then scores full marks again is
    not a model that scores the average of the three, and a table showing only the mean says it
    is. Every figure below is derived, so what is asserted is the *shape* -- best, worst, spread,
    and each repeat listed in order -- rather than three digits that a rubric change invalidates.
    """
    completer = ScriptedCompleter([good, drops_the_column, good])

    report = _one(bench, completer, repeats=3)

    leg = report.legs[0]
    assert leg.scores == (FULL_MARKS, DROPS_THE_COLUMN, FULL_MARKS)
    assert leg.spread == DROPPED_COLUMN_COSTS
    assert leg.score_cell() == f"{DROPS_THE_COLUMN}-{FULL_MARKS}/{FULL_MARKS}"
    assert leg.outcome is Outcome.FAIL, "two good runs do not cancel a bad one"

    table = render(report)
    assert f"{DROPS_THE_COLUMN}-{FULL_MARKS}/{FULL_MARKS}" in table
    assert f"{FULL_MARKS}, {DROPS_THE_COLUMN}, {FULL_MARKS}" in table


# =============================================================================
# NEGATIVE CONTROLS: EVERY WAY A LEG CAN PRODUCE NO PLAN
# =============================================================================


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (APITimeoutError(request=httpx.Request("POST", URL)), Failure.TIMEOUT),
        (_status(RateLimitError, "slow down"), Failure.RATE_LIMITED),
        (_status(AuthenticationError, "bad key", 401), Failure.AUTHENTICATION),
        (
            APIConnectionError(request=httpx.Request("POST", URL)),
            Failure.TRANSPORT,
        ),
    ],
)
def test_an_endpoint_failure_is_attributed_rather_than_scored_as_zero(
    bench: Bench, outcome: Any, expected: Failure
) -> None:
    """Four different findings, none of them about the model's ability to plan a workbook.

    A table rendering them as ``0`` out of full marks, alongside a model that answered badly, is
    how a proxy problem gets a model replaced -- so none of them scores, and none is a FAIL.
    They are SKIPs, because a FAIL in this table means "this model's judgement was wrong" and a
    timeout means "kedge's read gap elapsed while a reasoning model was thinking".

    The skip has to be loud or it becomes the other failure mode: a blank row that reads as a
    model nobody has tested yet. So the leg is named under the table, with what it cost.
    """
    report = _one(bench, _stubbed(outcome))

    leg = report.legs[0]
    assert leg.attributions == {expected: 1}
    assert leg.scores == ()
    assert leg.score_cell() == "-", "no score, rather than a zero somebody will average"
    assert leg.outcome is Outcome.SKIP, "the endpoint failed; the model was not measured"
    assert leg.ran, "an endpoint was asked -- this is not an unconfigured leg"
    assert report.skipped_after_asking == (leg,)

    table = render(report)
    assert expected.value in table
    assert "Asked and did not answer" in table
    assert "not untested" in table


def test_a_leg_that_answered_twice_and_timed_out_once_still_explains_the_timeout(
    bench: Bench, good: str
) -> None:
    """A timeout no longer fails a leg, so it must not disappear with the failure it used to be.

    Two answers and one timeout is a PASS for the leg as a whole, and the "Why" block used to be
    printed only for legs that were not a clean pass -- which would leave the repeat that spent
    seconds and produced nothing explained nowhere, on a row whose score column looks perfect.
    """
    completer = _stubbed(
        _answer(good),
        APITimeoutError(request=httpx.Request("POST", URL)),
        _answer(good),
    )

    report = _one(bench, completer, repeats=3)

    leg = report.legs[0]
    assert leg.outcome is Outcome.PASS
    assert leg.attributions == {Failure.NONE: 2, Failure.TIMEOUT: 1}
    assert any("repeat 2: timeout" in line for line in leg.why())
    assert "repeat 2: timeout" in render(report)
    assert report.ok, "nothing gradeable failed; the endpoint went quiet once"


def test_a_schema_refusal_is_told_apart_from_a_temperature_refusal(bench: Bench) -> None:
    """Both exhaust the ladder and both raise the same exception class. Reading one as the other
    is the mistake ``tests/llm/README.md`` records the cost of."""
    refusals = [_refusal("response_format is not supported") for _ in range(3)]

    report = _one(bench, _stubbed(*refusals))

    assert report.legs[0].attributions == {Failure.SCHEMA_REFUSED: 1}


def test_a_recovered_temperature_refusal_is_a_pass_that_says_what_it_gave_up(
    bench: Bench, good: str
) -> None:
    """The endpoint said no, kedge dropped the parameter, the model answered. Reporting that as a
    failure is the recorded mistake; not reporting it at all is how it goes unnoticed twice."""
    report = _one(
        bench, _stubbed(_temperature_refusal(), _answer(good, prompt=8000, completion=900))
    )

    leg = report.legs[0]
    assert leg.outcome is Outcome.PASS
    assert leg.scores == (FULL_MARKS,)
    assert leg.attributions == {Failure.NONE: 1}
    assert leg.attribution_cell() == "answered (no temperature)"
    assert leg.usage.requests == 2
    assert leg.usage.wasted_requests == 1
    assert leg.repeats[0].call.negotiated


def test_prose_no_repair_round_can_fix_is_the_models_judgement_failing(bench: Bench) -> None:
    report = _one(bench, ScriptedCompleter(["I would start with the Pre-Adjustment tab."] * 3))

    leg = report.legs[0]
    assert leg.attributions == {Failure.REPAIRS_EXHAUSTED: 1}
    assert leg.outcome is Outcome.FAIL
    assert leg.usage.completions == 3, "every repair round was still billed"


def test_an_empty_response_is_its_own_finding(bench: Bench) -> None:
    report = _one(bench, _stubbed(SimpleNamespace(choices=[], usage=None)))

    assert report.legs[0].attributions == {Failure.EMPTY_RESPONSE: 1}


def test_triage_declining_the_workbook_is_never_attributed_to_the_model(
    bench: Bench, good: str
) -> None:
    """kedge itself said stop. The model was never asked, so it cannot have failed."""
    stopped = dataclasses.replace(bench.triage_result, verdict=TriageVerdict.STOP)
    refusing = dataclasses.replace(bench, triage_result=stopped)

    report = _one(refusing, ScriptedCompleter([good]))

    leg = report.legs[0]
    assert leg.attributions == {Failure.TRIAGE_REFUSED: 1}
    assert leg.usage.completions == 0, "no completion was spent on a workbook triage refused"


# =============================================================================
# SKIPS
# =============================================================================


def test_an_unconfigured_leg_is_a_skip_that_still_produces_a_row(bench: Bench) -> None:
    resolve = _leg(None, Failure.NOT_CONFIGURED, "no keyring entry named 'sweep-key'")

    report = sweep([ModelSpec(model="absent")], bench=bench, resolve=resolve)

    leg = report.legs[0]
    assert leg.outcome is Outcome.SKIP
    assert leg.scores == ()
    assert "keyring" in render(report)
    # The leg itself has failed nothing -- but this sweep graded nothing either, and that is a
    # different question, settled by the two tests below.
    assert report.available is None


def test_a_skip_among_graded_legs_does_not_fail_the_sweep(bench: Bench, good: str) -> None:
    """The rule that must not be lost to the one below: an unreachable model is not a failure."""
    table = {"answers": ScriptedCompleter([good]), "unreachable": Failure.NOT_CONFIGURED}

    report = sweep([ModelSpec(model=name) for name in table], bench=bench, resolve=_legs(table))

    assert [leg.outcome for leg in report.legs] == [Outcome.PASS, Outcome.SKIP]
    assert report.ok, "one model answered and passed; the one nobody could reach costs nothing"
    assert report.exit_code() == 0


def test_a_sweep_that_graded_nothing_does_not_report_success(bench: Bench) -> None:
    """The reconciliation rule, in a sweep: no baseline degrades to *not checked*, never *passed*.

    Every leg skipping is what an endpoint outage, a lapsed key or one typo in a shared
    ``--base-url`` looks like. None of them leaves a failure to count, so an exit code derived
    from failures alone would report success -- and would go on reporting it for exactly as long
    as the outage lasted, which is when somebody most needs to be told (CLAUDE.md
    non-negotiable 6).
    """
    table = {"unreachable": Failure.NOT_CONFIGURED, "also-unreachable": Failure.TIMEOUT}

    report = sweep([ModelSpec(model=name) for name in table], bench=bench, resolve=_legs(table))

    assert all(leg.outcome is Outcome.SKIP for leg in report.legs)
    assert report.available is None
    assert not report.ok, "a sweep that scored nothing has not passed; it has not run"
    assert report.exit_code() == 1
    assert "nothing was scored" in report.headline()


def test_a_skip_beside_a_failure_leaves_both_legible(
    bench: Bench, good: str, drops_the_column: str
) -> None:
    """Six models where two have no key must still print six rows, and the two must not read as
    models that answered badly."""
    table = {
        "answers": ScriptedCompleter([good]),
        "unreachable": Failure.NOT_CONFIGURED,
        "answers-badly": ScriptedCompleter([drops_the_column]),
    }
    specs = [ModelSpec(model=name) for name in table]

    report = sweep(specs, bench=bench, resolve=_legs(table))

    assert [leg.outcome for leg in report.legs] == [Outcome.PASS, Outcome.SKIP, Outcome.FAIL]
    assert [leg.name for leg in report.legs] == list(table)
    assert not report.ok, "the leg that answered badly still fails the sweep"


def test_a_leg_that_never_ran_averages_into_nothing(bench: Bench) -> None:
    """Zeros from a leg nobody could run would crown it the fastest and cheapest in the sweep."""
    resolve = _leg(None, Failure.NOT_CONFIGURED, "no keyring entry")

    report = sweep([ModelSpec(model="absent")], bench=bench, repeats=3, resolve=resolve)

    leg = report.legs[0]
    assert leg.mean_seconds is None
    assert leg.mean_score is None
    assert leg.seconds == 0.0
    assert not leg.usage.any_reported
    assert all(not repeat.ran for repeat in leg.repeats)


# =============================================================================
# COST, HONESTLY
# =============================================================================


def test_an_endpoint_that_reports_no_usage_is_never_rendered_as_zero_tokens(
    bench: Bench, good: str
) -> None:
    report = _one(bench, ScriptedCompleter([good]))

    assert not report.legs[0].usage.any_reported
    assert "not reported" in render(report)
    assert "0 tokens" not in render(report)

    usage = json.loads(as_json(report))["legs"][0]["usage"]
    assert usage["measured"] is False
    assert usage["total"] is None, "null, so a consumer cannot sum it as a measurement"


def test_the_endpoints_own_numbers_are_summed_across_repeats(bench: Bench, good: str) -> None:
    completer = _stubbed(
        _answer(good, prompt=8_000, completion=900),
        _answer(good, prompt=8_100, completion=950),
    )

    report = _one(bench, completer, repeats=2)

    usage = report.legs[0].usage
    assert usage.any_reported
    assert usage.prompt == 16_100
    assert usage.completion == 1_850
    assert usage.total == 17_950
    assert usage.completions == 2


def test_the_negotiation_is_resolved_once_a_leg_rather_than_once_a_repeat(
    bench: Bench, good: str
) -> None:
    """A user pays for the structured-output ladder once a session, not once a proposal, and a
    sweep that re-resolved every repeat would report a cost nobody pays."""
    completer = _stubbed(
        _refusal("response_format is not supported"),
        _answer(good, prompt=100, completion=10),
        _answer(good, prompt=100, completion=10),
    )

    report = _one(bench, completer, repeats=2)

    usage = report.legs[0].usage
    assert usage.requests == 3
    assert usage.completions == 2
    assert usage.wasted_requests == 1
    assert "1 wasted" in render(report)
    assert report.legs[0].attribution_cell() == "answered (json_object)"


def test_a_price_is_the_callers_and_an_unmeasured_leg_never_gets_one(
    bench: Bench, good: str
) -> None:
    """A price table committed to this repository would be wrong within a month and wrong
    invisibly."""
    table = {"measured": _stubbed(_answer(good, prompt=1_000_000, completion=100_000))}
    table["silent"] = ScriptedCompleter([good])
    specs = [ModelSpec(model=name) for name in table]
    prices = {"measured": Price(prompt=2.0, completion=10.0), "silent": Price(2.0, 10.0)}

    report = sweep(specs, bench=bench, resolve=_legs(table), prices=prices)

    assert report.legs[0].cost(prices["measured"]) == pytest.approx(3.0)
    assert report.legs[1].cost(prices["silent"]) is None
    assert "3.0000 USD" in render(report)
    assert "no tokens reported" in render(report)


# =============================================================================
# THE MACHINE-READABLE FORM
# =============================================================================


def test_the_json_carries_the_denominator_the_spread_and_the_attribution(
    bench: Bench, good: str, drops_the_column: str
) -> None:
    report = _one(bench, ScriptedCompleter([good, drops_the_column]), repeats=2)

    document = json.loads(as_json(report))
    leg = document["legs"][0]
    assert document["available"] == FULL_MARKS
    assert document["points_declared"] == sum(STRUCTURAL_WEIGHTS.values())
    assert leg["scores"] == [FULL_MARKS, DROPS_THE_COLUMN]
    assert leg["best"] == FULL_MARKS
    assert leg["worst"] == DROPS_THE_COLUMN
    assert leg["spread"] == DROPPED_COLUMN_COSTS
    assert leg["attribution"] == {"none": 2}
    assert leg["outcome"] == "fail"
    assert [repeat["available"] for repeat in leg["repeats"]] == [FULL_MARKS, FULL_MARKS]
    failed = [
        item["id"]
        for repeat in leg["repeats"]
        for item in repeat["items"]
        if item["outcome"] == "fail"
    ]
    assert failed == ["does_not_drop_the_sql_column"]


def test_the_json_of_a_failed_leg_names_the_failure_and_reports_no_score(bench: Bench) -> None:
    report = _one(bench, _stubbed(_status(RateLimitError, "slow down")))

    leg = json.loads(as_json(report))["legs"][0]
    assert leg["scores"] == []
    assert leg["available"] is None
    assert leg["mean"] is None, "null, never 0.0 -- there is nothing to average"
    assert leg["attribution"] == {"rate_limited": 1}
    assert leg["repeats"][0]["items"] == []


def test_the_json_says_whether_a_leg_ran_and_whether_the_model_is_to_blame(
    bench: Bench, good: str
) -> None:
    """``"outcome": "skip"`` alone cannot tell a rate-limited leg from an unconfigured one.

    Both are skips and they need different actions -- pace the sweep, or set a key -- so the two
    facts that separate them are carried explicitly rather than left to a consumer to re-derive
    from a list of failure names it would have to keep in step with the enum. ``about_the_model``
    is the same question the outcome answers, in the form a CI step wants it.
    """
    table = {
        "paced-out": _stubbed(_status(RateLimitError, "slow down")),
        "unconfigured": Failure.NOT_CONFIGURED,
        "answers": ScriptedCompleter([good]),
    }
    specs = [ModelSpec(model=name) for name in table]

    document = json.loads(as_json(sweep(specs, bench=bench, resolve=_legs(table))))

    paced, unconfigured, answered = document["legs"]
    assert (paced["outcome"], paced["ran"]) == ("skip", True)
    assert (unconfigured["outcome"], unconfigured["ran"]) == ("skip", False)
    assert paced["repeats"][0]["about_the_model"] is False
    assert unconfigured["repeats"][0]["about_the_model"] is False
    assert answered["repeats"][0]["about_the_model"] is False, "it answered; nothing failed"


# =============================================================================
# PLUMBING
# =============================================================================


def test_repeats_below_one_is_refused(bench: Bench) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        sweep([ModelSpec(model="m")], bench=bench, repeats=0, resolve=_leg(None))


def test_every_model_is_resolved_once_and_the_environment_is_left_alone(
    bench: Bench, good: str
) -> None:
    """A leg that set ``KEDGE_HOME`` would be read by every other leg in the process, which is how
    a sweep silently measures one model six times."""
    before = dict(os.environ)
    calls: list[str] = []

    def resolve(spec: ModelSpec) -> ResolvedModel:
        calls.append(spec.model)
        return ResolvedModel(spec=spec, completer=ScriptedCompleter([good, good, good]))

    report = sweep(
        [ModelSpec(model="a"), ModelSpec(model="b")],
        bench=bench,
        repeats=3,
        kedge_home=Path("does-not-exist"),
        resolve=resolve,
    )

    assert calls == ["a", "b"], "resolved once a leg, not once a repeat"
    assert [len(leg.repeats) for leg in report.legs] == [3, 3]
    assert dict(os.environ) == before


def test_a_label_names_the_row_when_one_model_id_is_run_twice(bench: Bench, good: str) -> None:
    specs = [
        ModelSpec(model="m", label="via-proxy"),
        ModelSpec(model="m", label="direct"),
    ]

    def resolve(spec: ModelSpec) -> ResolvedModel:
        return ResolvedModel(spec=spec, completer=ScriptedCompleter([good]))

    report = sweep(specs, bench=bench, resolve=resolve)

    assert [leg.name for leg in report.legs] == ["via-proxy", "direct"]


def test_a_recorded_analysis_can_be_pinned_instead_of_re_analysing(
    bench: Bench, tmp_path: Path
) -> None:
    """The default is to re-analyse -- ``evals/*/*.kedge/`` is gitignored, so a CI runner has no
    recorded analysis -- but a caller pinning every leg to one byte-identical context can."""
    recorded = tmp_path / "analysis.json"
    recorded.write_text(bench.analysis.model_dump_json(), encoding="utf-8")

    loaded = load_analysis(adjustment_case.WORKBOOK, analysis_path=recorded)

    assert loaded.workbook.sha256 == bench.analysis.workbook.sha256
    assert len(loaded.operations) == len(bench.analysis.operations)


def test_a_sweep_of_nothing_still_renders(bench: Bench) -> None:
    """Renders rather than dividing by zero -- and does not call an empty comparison a success."""
    report = sweep([], bench=bench)

    assert report.available is None
    assert not report.ok
    assert "nothing was scored" in render(report)
