"""Per-model sweep over the *planning* seam: can this model plan this workbook, and at what cost.

``evals/run.py`` today grades two committed artifacts -- a reference notebook and a reference
plan -- and scores 63/63 with no model anywhere in the loop. That number says the graders work.
It says nothing at all about whether the model a user has configured can do the job, which is the
question this module exists to answer, one model at a time, with the answer broken down into
"how well", "how fast", "how many tokens" and, when there is no answer, "why not".

**What is graded, and what is deliberately not.** One model call produces one
:class:`~kedge.plan.model.ProcessPlan`, and that plan is graded against the rubric's *structural*
tier and nothing else. The deterministic tier is worth more than twice as much, and every point
of it is earned by driving a notebook -- so handing :func:`harness.grade.grade` the committed
reference notebook alongside a model's plan would credit the model with forty-five points a human
wrote, and print a near-perfect total for a model that had done a fraction of the work. That is
the exact false confidence this whole exercise exists to remove, and it would be almost invisible
in a tidy table. Hence :data:`TIER`, hence the denominator is the structural tier's and is printed
above the table rather than inferred, and hence the report states in prose what it did not grade.
Measuring a model's *notebook* is a different seam, and it belongs to ``harness/convert.py``.

**Two structural items cannot be graded from a plan alone**, and they are skipped rather than
failed or quietly dropped from the rubric. ``does_not_trust_the_impact_summary`` reads the driven
notebook's definitions; ``consults_the_knowledge_pack`` has no knowledge pack to consult. Both
therefore sit outside the denominator, which is why a leg that answers perfectly scores 16 out of
the 19 points the tier declares, and why both numbers are printed. The first of those is detected
rather than listed: the notebook a grader would read is replaced by :class:`_NoNotebook`, whose
every attribute access raises :class:`NotebookRequiredError`, so a structural grader that grows a
notebook dependency later skips itself with a reason instead of silently reading an empty
namespace and reporting a pass.

**Failure attribution is the point of the table.** A model that scores nothing may have refused
the JSON schema, refused an explicit temperature, timed out, been rate-limited, been unreachable,
never been asked because there is no key in the keyring, been declined by kedge's own triage, or
returned prose that no repair round could turn into a plan. Only the last of those, and a plan
that validates but is poor, are the model's judgement failing. Everything else is a fact about the
integration or the configuration, and a table that renders them identically is how a negotiation
bug gets a model blamed -- which has happened once on this project already
(``tests/llm/README.md``). :mod:`harness.live` classifies; this module carries the classification
into every row, every aggregate and the JSON.

**A skip is never a pass and never a failure.** An unconfigured leg produces a row, a SKIP and the
reason, and it contributes no score, no tokens and no seconds to any aggregate -- averaging a leg
that never ran in as a zero would make the model nobody could reach look like the fastest and
cheapest in the sweep. So does every other failure that is not the model's judgement: a timeout, a
429, a 404 for a model this key cannot see, a 5xx (:meth:`~harness.live.Failure.as_outcome`). Those
skips are the ones that need saying out loud, and :attr:`SweepReport.skipped_after_asking` is why
-- a leg that was asked and did not answer looks exactly like a leg nobody could run, and only one
of the two means the comparison in the table is missing a competitor. The same rule governs tokens
within a leg that *did* run: an endpoint that volunteers no ``usage`` block reports nothing, and
nothing is rendered as "not reported", never as zero
(:attr:`~harness.live.MeteredUsage.any_reported`).

Nothing here calls a network by itself, and nothing here reads a CLI. ``evals/run.py`` owns the
command line; the entry points are :func:`sweep`, :func:`render` and :func:`as_json`, with
:class:`Bench` and :func:`run_leg` underneath for a caller that wants one leg at a time. The whole
of it is exercised offline in ``tests/unit/test_evals_sweep.py`` against scripted completers and
stubbed endpoints, including one leg of each failure kind, because a sweep that has only ever seen
a good run is a sweep whose polarity nobody has checked.
"""

from __future__ import annotations

import json
import logging
import textwrap
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

# `_tier` is private and imported anyway. It is what computes the committed run's denominator --
# rubric order, per-item weights, a grader that raises becoming a SKIP rather than a crash -- and a
# second copy of that arithmetic here would drift from the one `evals/run.py` prints. Two eval
# reports of the same tier disagreeing about what it is out of is precisely the confusion the
# "always carry the denominator" rule exists to prevent.
from harness.grade import _tier, load_case
from harness.live import (
    Failure,
    MeteredCall,
    MeteredUsage,
    ModelSpec,
    ResolvedModel,
    resolve_model,
)
from harness.model import ItemResult, Outcome, TierResult
from kedge.plan.propose import DEFAULT_MAX_ATTEMPTS, propose_plan

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from kedge.analysis.model import WorkbookAnalysis
    from kedge.plan.model import ProcessPlan
    from kedge.plan.triage import TriageResult

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_CASE",
    "TIER",
    "Bench",
    "LegResult",
    "NotebookRequiredError",
    "Price",
    "RepeatResult",
    "SweepReport",
    "as_json",
    "grade_structural",
    "load_analysis",
    "propose_once",
    "render",
    "run_leg",
    "sweep",
]

DEFAULT_CASE = "adjustment_signoff"

TIER = "structural"
"""The one tier a plan can be graded against, named once so nothing here can widen it by accident.

The deterministic tier grades a driven notebook. Grading it here would mean driving *some*
notebook, and the only notebook available is the committed reference conversion -- so the score
would be a human's work reported under a model's name.
"""

_DETAIL_WIDTH = 160
"""How much of an exception or a grader's detail a table shows before the JSON is the place to look."""

_UNANSWERED = (
    "These legs were asked and did not answer. They score nothing on purpose -- a timeout, a 429, "
    "a 404 or a 5xx is a fact about the endpoint, the account or this sweep's pacing, and scoring "
    "one against the model is how a proxy problem gets a model replaced. But they are not "
    "untested models either: the comparison above is missing a competitor, and the fix is to run "
    "them again rather than to read the table as it stands. A timeout in particular is the read "
    "gap in `[model] timeout_seconds` rather than a budget for the whole answer, so an endpoint "
    "that goes quiet while a reasoning model thinks trips it while working perfectly."
)
"""Printed under the table whenever a leg was asked and produced nothing.

A SKIP is the honest outcome for all of these (:meth:`harness.live.Failure.as_outcome`), and an
honest outcome rendered quietly is one nobody acts on -- the same reason a permanently amber
reconciliation signal is one people stop reading.
"""


# =============================================================================
# WHAT EVERY LEG IS RUN AGAINST
# =============================================================================


class NotebookRequiredError(Exception):
    """A grader asked for a driven notebook, and a plan-only sweep has not got one.

    Raised by :class:`_NoNotebook` rather than by anything a grader calls deliberately, so it
    reports itself: the structural tier is graded with the notebook replaced by a stand-in that
    refuses, and whatever reaches for it identifies itself in the message. The alternative -- a
    hardcoded list of the graders that need a notebook -- would go stale the first time somebody
    added another, and the failure mode of a stale list here is a grader reading an empty
    namespace and passing.
    """


class _NoNotebook:
    """Stands in for a :class:`harness.drive.NotebookRun` that was never driven.

    Every attribute raises, except the dunders, which stay absent in the ordinary way so
    ``repr()``, equality and pickling behave. A grader touching ``run.definitions`` is asking a
    question this sweep cannot answer, and the honest response is a skip naming the item -- not a
    pass over an empty ``dict``, which is what a permissive stand-in would produce and which would
    read as "the notebook does not carry the memo's stale figures" for a notebook that does not
    exist.
    """

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        msg = (
            f"this item is graded on a driven notebook (it asked for {name!r}), and a plan sweep "
            f"drives none. A model's notebook is measured by harness/convert.py."
        )
        raise NotebookRequiredError(msg)


def load_analysis(workbook: Path, *, analysis_path: Path | None = None) -> WorkbookAnalysis:
    """Analyse the case's workbook, or read a recorded analysis instead.

    Re-analysing is the default, deliberately, even though
    ``<case>/q2_accrual_adjustment.kedge/analysis.json`` usually exists on a developer's machine.
    Two reasons, and the second is the one that would hurt. ``evals/*/*.kedge/`` is gitignored, so
    that file is a local artifact: a fresh clone and a CI runner have no such thing, and a sweep
    meant to become a CI step cannot depend on one. And the analysis *is* the model's context --
    a recorded one that predates a change to the analyser would have the model reasoning over
    facts kedge no longer produces, and the resulting drop in score would look exactly like a
    model regression. Analysing this workbook costs about 1.3 seconds against a model call of
    tens of seconds, once per sweep rather than once per leg.

    Args:
        workbook: The case's ``.xlsx``, which is the committed input.
        analysis_path: A recorded ``analysis.json`` to read instead, for a caller who wants every
            leg pinned to one byte-identical context.

    Returns:
        The :class:`~kedge.analysis.model.WorkbookAnalysis` every leg reasons over.
    """
    if analysis_path is not None:
        from kedge.analysis.model import WorkbookAnalysis

        logger.info("reading the recorded analysis at %s", analysis_path)
        return WorkbookAnalysis.model_validate_json(analysis_path.read_text(encoding="utf-8"))

    from kedge.analysis.analyse import analyse

    logger.info("analysing %s", workbook)
    return analyse(workbook)


@dataclass(frozen=True, slots=True)
class Bench:
    """Everything a leg is run against, built once and shared by every leg and every repeat.

    Built once because all of it is deterministic and none of it is cheap enough to repeat
    needlessly -- but mostly because sharing it is what makes the comparison a comparison. Two
    models graded against two different analyses of the same workbook, or two different triage
    results, are not two measurements of the same thing.

    Example:
        >>> bench = Bench.load()                                     # doctest: +SKIP
        >>> bench.points_declared, bench.case.WORKBOOK.name          # doctest: +SKIP
        (19, 'q2_accrual_adjustment.xlsx')
    """

    case: Any
    rubric: dict[str, Any]
    analysis: WorkbookAnalysis
    triage_result: TriageResult
    temperature: float = 0.2
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    """Repair rounds inside one proposal, which is ``propose_plan``'s meaning of "attempt". It is
    not the sweep's repeat count -- a repair is the same question asked again with the validation
    errors attached, a repeat is the whole question asked afresh."""

    @classmethod
    def load(
        cls,
        case_name: str = DEFAULT_CASE,
        *,
        analysis_path: Path | None = None,
        temperature: float = 0.2,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> Bench:
        """Load the case, its rubric and its analysis, and run triage once."""
        from kedge.plan.triage import triage

        case = load_case(case_name)
        rubric = yaml.safe_load(case.RUBRIC.read_text(encoding="utf-8"))
        analysis = load_analysis(case.WORKBOOK, analysis_path=analysis_path)
        return cls(
            case=case,
            rubric=rubric,
            analysis=analysis,
            triage_result=triage(analysis),
            temperature=temperature,
            max_attempts=max_attempts,
        )

    @property
    def name(self) -> str:
        """The case's name, as the rubric spells it."""
        return str(self.rubric.get("workbook", DEFAULT_CASE))

    @property
    def items_declared(self) -> tuple[str, ...]:
        """Every structural item id in the rubric, in rubric order."""
        return tuple(str(entry["id"]) for entry in self.rubric.get(TIER, []))

    @property
    def points_declared(self) -> int:
        """What the structural tier is worth in full, before anything is found ungradeable."""
        return sum(int(entry.get("weight", 1)) for entry in self.rubric.get(TIER, []))

    @property
    def points_not_graded(self) -> int:
        """What the deterministic tier is worth -- the marks this sweep pointedly does not award.

        Printed above the table. A per-model score that omits the larger tier is honest only if it
        says how large the omission was.
        """
        return sum(int(entry.get("weight", 1)) for entry in self.rubric.get("deterministic", []))


# =============================================================================
# GRADING A PLAN, AND NOTHING BUT A PLAN
# =============================================================================


def grade_structural(bench: Bench, plan: ProcessPlan) -> TierResult:
    """Grade one plan against the structural tier, driving no notebook.

    The graders are the case's own, keyed by rubric id, run through
    :func:`harness.grade._tier` so the weights and the denominator are computed by the same code
    that computes them for the committed run. Each is wrapped so that a grader reaching for the
    notebook becomes a skip with a reason rather than a traceback in the middle of a sweep.

    Args:
        bench: The case and rubric to grade against.
        plan: The plan the model proposed. Unapproved, as ``propose_plan`` returns it -- approval
            is a human act and nothing here performs one.

    Returns:
        The :class:`~harness.model.TierResult` for the structural tier alone. Its ``available`` is
        the denominator every score in this module is out of.
    """
    context = bench.case.Context(
        run=_NoNotebook(),
        facts=bench.rubric["facts"],
        notebook=Path("<no notebook was driven>"),
        plan=plan,
    )
    graders = {name: _plan_only(grader) for name, grader in bench.case.STRUCTURAL.items()}
    return _tier(TIER, bench.rubric, graders, context)


def _plan_only(grader: Callable[[Any], ItemResult]) -> Callable[[Any], ItemResult]:
    """Turn a grader's demand for a driven notebook into a skip that says so.

    ``_tier`` would already catch the exception and skip the item -- it catches everything -- but
    it logs a traceback while doing it, and a sweep of six models across three repeats would print
    eighteen of them around a table nobody could then read. Catching it here also lets the skip
    carry a sentence about *why* rather than the name of an exception class.
    """

    def graded(context: Any) -> ItemResult:
        try:
            return grader(context)
        except NotebookRequiredError as exc:
            return ItemResult(id="", outcome=Outcome.SKIP, detail=str(exc))

    return graded


# =============================================================================
# ONE PROPOSAL
# =============================================================================


def propose_once(resolved: ResolvedModel, bench: Bench) -> tuple[ProcessPlan | None, MeteredCall]:
    """Ask one resolved model for one plan, and measure what that cost either way.

    Never raises. A sweep is a batch job over endpoints that fail in a dozen ways, and the
    alternative to one attributed row is no table at all -- the failure is classified, the
    requests it burned are still counted, and the caller decides what a leg of failures means.

    Args:
        resolved: A leg from :func:`~harness.live.resolve_model`. An unready one costs nothing
            and comes back carrying its own reason.
        bench: What to plan, and how hard to try.

    Returns:
        The plan and the measurement, or ``None`` and a measurement carrying the attribution.
    """
    metered = resolved.metered()
    if metered is None:
        # Nothing was asked of any endpoint. No usage, no seconds, and a failure that
        # `as_outcome()` maps to SKIP when it is a configuration one -- which is most of the time.
        return None, MeteredCall(failure=resolved.failure, detail=resolved.detail)

    try:
        plan = propose_plan(
            bench.analysis,
            completer=metered,
            triage_result=bench.triage_result,
            model=resolved.spec.model,
            max_attempts=bench.max_attempts,
            temperature=bench.temperature,
        )
    except Exception as exc:  # every failure mode here is a row, never a traceback
        call = metered.summarise(exc)
        logger.warning("%s produced no plan: %s", resolved.spec.name, call.failure.value)
        return None, call
    return plan, metered.summarise()


# =============================================================================
# RESULTS
# =============================================================================


@dataclass(frozen=True, slots=True)
class RepeatResult:
    """One repeat of one leg: what came back, what it cost, and how it graded.

    Called a repeat rather than an attempt on purpose. ``propose_plan`` already owns "attempt" for
    the repair rounds inside a single proposal, and conflating the two would make
    ``max_attempts=3`` and ``repeats=3`` look like the same knob when one re-sends the validation
    errors and the other asks the whole question again.

    Example:
        >>> RepeatResult(spec=ModelSpec(model="m"), repeat=1,
        ...              call=MeteredCall(failure=Failure.NOT_CONFIGURED)).outcome
        <Outcome.SKIP: 'skip'>
    """

    spec: ModelSpec
    repeat: int
    call: MeteredCall
    tier: TierResult | None = None
    """The structural tier as graded, or ``None`` when no plan came back to grade."""

    @property
    def ran(self) -> bool:
        """Whether an endpoint was actually asked anything.

        The guard on every aggregate. A leg with no keyring entry has no seconds and no tokens,
        and folding its zeros into a mean would report the model nobody could reach as the
        fastest and cheapest in the sweep.
        """
        return self.call.usage.completions > 0

    @property
    def earned(self) -> int | None:
        return None if self.tier is None else self.tier.earned

    @property
    def available(self) -> int | None:
        """The denominator this repeat was scored out of, or ``None`` if it was not scored."""
        return None if self.tier is None else self.tier.available

    @property
    def outcome(self) -> Outcome:
        """PASS, FAIL or SKIP for the repeat as a whole.

        A leg that produced no plan takes its outcome from the failure -- SKIP when nobody could
        run it, FAIL when somebody could and it did not work. A leg that produced one takes it
        from the grading, so a plan that validates and drops the SQL column is a FAIL with
        :attr:`~harness.live.Failure.NONE` beside it: the model answered, and its judgement is
        what was wrong. Those are the two columns that must never be collapsed into one.
        """
        if self.tier is None:
            return self.call.failure.as_outcome()
        if not self.tier.available:
            return Outcome.SKIP
        if any(item.outcome is Outcome.FAIL for item in self.tier.items):
            return Outcome.FAIL
        return Outcome.PASS

    @property
    def failed_items(self) -> tuple[ItemResult, ...]:
        if self.tier is None:
            return ()
        return tuple(item for item in self.tier.items if item.outcome is Outcome.FAIL)


@dataclass(frozen=True, slots=True)
class LegResult:
    """One model, over every repeat: the spread of its scores and the sum of its bills.

    The spread is not decoration. A model that scores 18, 12 and 18 is not a 16-scoring model, and
    a table showing only the mean says it is -- so :attr:`scores` keeps every repeat in order and
    :meth:`score_cell` renders a range whenever the ends differ.
    """

    spec: ModelSpec
    repeats: tuple[RepeatResult, ...] = ()

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def scored(self) -> tuple[RepeatResult, ...]:
        """The repeats that produced a gradeable plan."""
        return tuple(item for item in self.repeats if item.tier is not None)

    @property
    def scores(self) -> tuple[int, ...]:
        """Every score, in the order the repeats ran. Unscored repeats are absent, not zero."""
        return tuple(item.tier.earned for item in self.scored if item.tier is not None)

    @property
    def available(self) -> int | None:
        """The denominator. ``None`` when nothing was scored, so a caller cannot print ``/0``.

        Taken as the largest observed, and identical across repeats in practice: the two items
        that skip do so for reasons that have nothing to do with what the model said.
        """
        seen = [item.available for item in self.scored if item.available is not None]
        return max(seen) if seen else None

    @property
    def mean_score(self) -> float | None:
        return sum(self.scores) / len(self.scores) if self.scores else None

    @property
    def spread(self) -> int | None:
        """Best minus worst. Zero is a real and reassuring answer; ``None`` means unmeasured."""
        return max(self.scores) - min(self.scores) if self.scores else None

    @property
    def usage(self) -> MeteredUsage:
        """Every repeat's usage, summed. Honest about how much of it was reported."""
        return sum((item.call.usage for item in self.repeats), MeteredUsage())

    @property
    def seconds(self) -> float:
        """Wall-clock seconds inside the endpoint, summed over the repeats that ran."""
        return sum(item.call.seconds for item in self.repeats if item.ran)

    @property
    def mean_seconds(self) -> float | None:
        """Seconds per repeat, over the repeats that ran. ``None`` when none did."""
        ran = [item for item in self.repeats if item.ran]
        return sum(item.call.seconds for item in ran) / len(ran) if ran else None

    @property
    def ran(self) -> bool:
        """Whether any repeat of this leg actually asked an endpoint for something."""
        return any(item.ran for item in self.repeats)

    @property
    def attributions(self) -> Counter[Failure]:
        return Counter(item.call.failure for item in self.repeats)

    @property
    def outcome(self) -> Outcome:
        """FAIL if any repeat failed, else PASS if any passed, else SKIP.

        A leg that passes twice and fails once is a FAIL: a plan that is only sometimes right is
        the result being reported, and averaging it away would be the same mistake as printing the
        mean of 18, 12 and 18.
        """
        outcomes = [item.outcome for item in self.repeats]
        if Outcome.FAIL in outcomes:
            return Outcome.FAIL
        if Outcome.PASS in outcomes:
            return Outcome.PASS
        return Outcome.SKIP

    def cost(self, price: Price | None) -> float | None:
        """What the leg cost in money, or ``None`` when nobody can say."""
        return None if price is None else price.of(self.usage)

    def score_cell(self) -> str:
        """``16/16``, or ``12-16/16`` when the repeats disagreed, or ``-`` when nothing scored."""
        if not self.scores or self.available is None:
            return "-"
        low, high = min(self.scores), max(self.scores)
        head = str(low) if low == high else f"{low}-{high}"
        return f"{head}/{self.available}"

    def attribution_cell(self) -> str:
        """Why the leg came out as it did, in one cell.

        A clean answer says so; a negotiated one says what was given up to get it. Both are
        successes and neither is the same result, which is the distinction ``tests/llm/README.md``
        records the cost of losing.
        """
        counts = self.attributions
        if set(counts) == {Failure.NONE} and self.repeats:
            last = self.repeats[-1].call
            given_up = []
            if last.omit_temperature:
                given_up.append("no temperature")
            if last.degraded:
                given_up.append(str(last.mode))
            return f"answered ({', '.join(given_up)})" if given_up else "answered first time"
        return ", ".join(
            failure.value if count == 1 else f"{failure.value} x{count}"
            for failure, count in counts.most_common()
        )

    def why(self) -> tuple[str, ...]:
        """The lines a reader needs under the table when the leg is not a clean pass."""
        lines: list[str] = []
        for item in self.repeats:
            prefix = f"repeat {item.repeat}" if len(self.repeats) > 1 else "run"
            if item.call.failure and item.call.detail:
                lines.append(f"{prefix}: {item.call.failure.value}: {_clip(item.call.detail)}")
            elif item.call.failure:
                lines.append(f"{prefix}: {item.call.failure.value}")
            lines.extend(
                f"{prefix}: {failed.id} FAIL: {_clip(failed.detail)}"
                for failed in item.failed_items
            )
        return tuple(lines)


@dataclass(frozen=True, slots=True)
class SweepReport:
    """Every leg, plus the two numbers a reader needs before they read any of them.

    :attr:`points_declared` is what the structural tier is worth; the per-leg denominator is what
    was actually gradeable from a plan. Both are printed, because the gap between them is not a
    rounding error -- it is two rubric items that need something a plan sweep does not have -- and
    a reader who is told only "16" has no way to know whether that is out of 16, 19 or 63.
    """

    case: str
    legs: tuple[LegResult, ...] = ()
    repeats: int = 1
    points_declared: int = 0
    items_declared: tuple[str, ...] = ()
    points_not_graded: int = 0
    prices: Mapping[str, Price] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def available(self) -> int | None:
        """What a leg that answered was scored out of, or ``None`` if no leg answered."""
        seen = [leg.available for leg in self.legs if leg.available is not None]
        return max(seen) if seen else None

    @property
    def ungradeable(self) -> tuple[ItemResult, ...]:
        """The structural items that skipped, taken from the first leg that scored anything."""
        for leg in self.legs:
            for repeat in leg.scored:
                if repeat.tier is not None:
                    return repeat.tier.skipped
        return ()

    @property
    def skipped_after_asking(self) -> tuple[LegResult, ...]:
        """Legs that were asked and did not answer: a SKIP that must be louder than the others.

        Two very different rows carry the marker ``SKIP``. One is a model with no keyring entry --
        nothing was spent, nothing was learned, and the fix is one command. The other is a model
        that was asked and timed out, or was rate-limited, or came back 404: requests were spent,
        seconds were spent, and *the comparison in the table above is missing a competitor*.
        Rendering them identically invites the reader to treat the second as "not tested yet"
        rather than "tested, and the endpoint would not play", which is the difference between
        re-running a leg and quietly shipping a conclusion drawn from four models out of six.
        """
        return tuple(leg for leg in self.legs if leg.outcome is Outcome.SKIP and leg.ran)

    @property
    def ok(self) -> bool:
        """Whether something was measured and nothing measured failed.

        Skips do not count against a sweep -- but a sweep made *entirely* of them has not passed,
        it has not run. An endpoint down, a key that lapsed overnight, one typo in a ``--base-url``
        shared by every leg: each leaves no failures to count, and reporting that as success would
        make a CI step green on a run that compared no models at all. Worse, it would be green for
        exactly as long as the outage lasted, which is when somebody most wants to be told.

        This is the reconciliation rule in another costume, and it is a non-negotiable in this
        repository for the same reason: a check with no baseline degrades to *not checked*, never
        to *passed* (CLAUDE.md non-negotiable 6, PLAN 6.2). :meth:`headline` already says "nothing
        was scored"; the exit code has to agree with it, because the exit code is the half that
        automation reads.
        """
        if self.available is None:
            return False
        return not any(leg.outcome is Outcome.FAIL for leg in self.legs)

    def exit_code(self) -> int:
        """0 when something was graded and nothing graded failed, 1 otherwise. For a CI step."""
        return 0 if self.ok else 1

    def headline(self) -> str:
        denominator = "nothing was scored" if self.available is None else f"out of {self.available}"
        return (
            f"{self.case}: {len(self.legs)} model(s), {self.repeats} repeat(s) each, "
            f"graded on the {TIER} tier only ({denominator})"
        )


@dataclass(frozen=True, slots=True)
class Price:
    """What a model costs per million tokens, according to the caller.

    Supplied, never committed. A price table in this repository would be wrong within a month and
    wrong invisibly -- somebody would read a cost column that had quietly become fiction. So
    tokens and seconds are the defaults, currency is opt-in, and the number in the table is the
    caller's arithmetic over the caller's figures.

    Example:
        >>> Price(prompt=2.5, completion=10.0).of(MeteredUsage()) is None
        True
    """

    prompt: float
    completion: float
    currency: str = "USD"

    def of(self, usage: MeteredUsage) -> float | None:
        """The bill for this usage, or ``None`` when the endpoint reported no tokens to bill.

        The ``None`` is the point. An endpoint that volunteers no ``usage`` block has not told
        anybody it was free, and a cost column reading ``0.00`` for it would be a lie about a real
        invoice.
        """
        if not usage.any_reported:
            return None
        return (usage.prompt * self.prompt + usage.completion * self.completion) / 1_000_000


# =============================================================================
# RUNNING THE SWEEP
# =============================================================================


def run_leg(
    spec: ModelSpec,
    bench: Bench,
    *,
    repeats: int = 1,
    resolve: Callable[[ModelSpec], ResolvedModel] = resolve_model,
) -> LegResult:
    """Run one model over ``repeats`` proposals and grade each one.

    The model is resolved **once** for the leg, not once per repeat, because the structured-output
    mode and the dropped temperature are negotiated state that a real session keeps: re-resolving
    would re-burn the ladder every repeat and report a negotiation cost the user pays once as
    though they paid it every time. It also means the first repeat's ``wasted_requests`` is the
    negotiation and the rest are not, which is exactly the shape a reader should see. The corollary
    is recorded on :meth:`~harness.live.ResolvedModel.metered`, which offers ``renegotiate=`` for a
    caller who wants the opposite: repeats two and three report ``negotiated`` with no negotiation
    cost, because that negotiation is a fact about the session rather than about the repeat.

    One leg at a time here, and legs are independent -- separate completers, separate meters -- so
    a caller may run them in parallel. The repeats *inside* a leg may not: they share one completer
    on purpose, and :class:`~harness.live.MeteredCompleter` is explicit about not being thread-safe.

    Args:
        spec: Which model, and what to override on the way.
        bench: The case, rubric and analysis every leg shares.
        repeats: How many proposals to ask for. One sample is noise; the default is one anyway,
            because the cost is real and the caller should choose to spend it.
        resolve: How a spec becomes a completer. Injectable so tests can hand back a scripted
            completer, which is the whole reason this module can be exercised with no endpoint,
            no keyring and no network.

    Returns:
        The leg, with every repeat kept individually. Nothing raises: a leg that cannot run is a
        row.
    """
    resolved = resolve(spec)
    if not resolved.ready:
        logger.info("%s will not run: %s", spec.name, resolved.failure.value)

    results: list[RepeatResult] = []
    for index in range(1, max(1, repeats) + 1):
        plan, call = propose_once(resolved, bench)
        tier = grade_structural(bench, plan) if plan is not None else None
        results.append(RepeatResult(spec=spec, repeat=index, call=call, tier=tier))
        if tier is not None:
            logger.info(
                "%s repeat %d scored %d/%d in %.1fs",
                spec.name,
                index,
                tier.earned,
                tier.available,
                call.seconds,
            )
    return LegResult(spec=spec, repeats=tuple(results))


def sweep(
    specs: Sequence[ModelSpec],
    *,
    case_name: str = DEFAULT_CASE,
    repeats: int = 1,
    kedge_home: Path | None = None,
    analysis_path: Path | None = None,
    bench: Bench | None = None,
    resolve: Callable[[ModelSpec], ResolvedModel] | None = None,
    temperature: float = 0.2,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    prices: Mapping[str, Price] | None = None,
) -> SweepReport:
    """Plan one workbook with every model in turn, and report how each of them did.

    Args:
        specs: The models. One row each, in this order, whether or not they run.
        case_name: Which eval's workbook and rubric to use.
        repeats: Proposals per model.
        kedge_home: A kedge directory to read ``config.toml`` from. Passed to
            :func:`~harness.live.resolve_model` as an argument and never as an environment
            variable -- a leg that set ``KEDGE_HOME`` would be read by every other leg in the
            process, which is how a sweep silently measures one model six times.
        analysis_path: A recorded analysis to pin every leg to. See :func:`load_analysis`.
        bench: A pre-built bench, when a caller has one. Overrides ``case_name``,
            ``analysis_path``, ``temperature`` and ``max_attempts``.
        resolve: The resolver, for tests and for a caller with its own credential story. Given
            one, ``kedge_home`` is the resolver's business rather than this function's.
        temperature: Sampling temperature, negotiated away by the completer if the endpoint
            refuses it.
        max_attempts: Repair rounds inside one proposal.
        prices: Per-leg prices, keyed by :attr:`~harness.live.ModelSpec.name`. Absent by default:
            see :class:`Price`.

    Returns:
        The :class:`SweepReport`. Render it with :func:`render`, or serialise with
        :func:`as_json`.
    """
    if repeats < 1:
        msg = f"repeats must be at least 1, not {repeats}"
        raise ValueError(msg)

    resolved_bench = bench or Bench.load(
        case_name,
        analysis_path=analysis_path,
        temperature=temperature,
        max_attempts=max_attempts,
    )
    resolver = resolve or _resolver(kedge_home)

    legs = tuple(run_leg(spec, resolved_bench, repeats=repeats, resolve=resolver) for spec in specs)
    return SweepReport(
        case=resolved_bench.name,
        legs=legs,
        repeats=repeats,
        points_declared=resolved_bench.points_declared,
        items_declared=resolved_bench.items_declared,
        points_not_graded=resolved_bench.points_not_graded,
        prices=dict(prices or {}),
        notes=_notes(resolved_bench, analysis_path),
    )


def _resolver(kedge_home: Path | None) -> Callable[[ModelSpec], ResolvedModel]:
    """Bind ``kedge_home`` to the real resolver without touching the environment."""

    def resolve(spec: ModelSpec) -> ResolvedModel:
        return resolve_model(spec, kedge_home=kedge_home)

    return resolve


def _notes(bench: Bench, analysis_path: Path | None) -> tuple[str, ...]:
    """The provenance a reader needs to know what these numbers are of."""
    source = "recorded" if analysis_path is not None else "re-analysed from the workbook"
    return (
        f"workbook: {bench.case.WORKBOOK.name} ({source})",
        f"triage: {bench.triage_result.verdict.value}, "
        f"convertible {bench.triage_result.convertible:.2f}",
        f"repairs allowed per proposal: {bench.max_attempts}; temperature {bench.temperature}",
    )


# =============================================================================
# RENDERING
# =============================================================================


def render(report: SweepReport) -> str:
    """The human table, with everything it did not grade stated above it.

    The preamble is not padding. A per-model score with no denominator and no statement of what
    was left out is the artifact this module was written to avoid producing.
    """
    wrapped = [
        textwrap.fill(line, width=94, initial_indent="  ", subsequent_indent="  ")
        for line in _preamble(report)
    ]
    blocks = [report.headline(), "", *wrapped, "", _table(report)]
    unanswered = report.skipped_after_asking
    if unanswered:
        blocks.extend(["", "Asked and did not answer (skipped, not untested)"])
        blocks.extend(f"  {leg.name}: {leg.attribution_cell()}" for leg in unanswered)
        blocks.append(
            textwrap.fill(_UNANSWERED, width=94, initial_indent="  ", subsequent_indent="  ")
        )
    # Every leg with anything to explain, rather than every leg that is not a clean pass. Those
    # were the same list while any endpoint failure made a leg FAIL; now that a timeout is a SKIP,
    # a leg that answered twice and timed out once is a PASS overall -- and the repeat that cost
    # seconds and produced nothing would have been explained nowhere. `why()` is already empty for
    # a leg with nothing wrong, so this drops no filtering that was doing any work.
    troubled = [leg for leg in report.legs if leg.why()]
    if troubled:
        blocks.extend(["", "Why"])
        for leg in troubled:
            blocks.append(f"  {leg.name}")
            blocks.extend(
                textwrap.fill(line, width=94, initial_indent="    ", subsequent_indent="      ")
                for line in leg.why()
            )
    if report.notes:
        blocks.extend(["", "Notes", *(f"  - {note}" for note in report.notes)])
    return "\n".join(blocks).rstrip() + "\n"


def _preamble(report: SweepReport) -> list[str]:
    lines = [
        f"Graded: the plan each model proposed, against the {TIER} tier -- "
        f"{len(report.items_declared)} item(s), {report.points_declared} point(s) declared.",
        f"Not graded: the deterministic tier's {report.points_not_graded} point(s). Those grade a "
        f"driven notebook, and this sweep drives none; awarding them here would report a "
        f"reference conversion's marks under a model's name.",
    ]
    ungradeable = report.ungradeable
    if ungradeable:
        names = ", ".join(item.id for item in ungradeable)
        lines.append(
            f"Not gradeable from a plan alone ({len(ungradeable)}): {names}. "
            f"A leg that answered is therefore scored out of {report.available}."
        )
    return lines


def _table(report: SweepReport) -> str:
    priced = any(report.prices.get(leg.name) is not None for leg in report.legs)
    headers = [
        "model",
        "n",
        "outcome",
        "score",
        "runs",
        "attribution",
        "tokens",
        "seconds",
        "requests",
    ]
    if priced:
        headers.append("cost")

    rows = [_row(leg, report.prices.get(leg.name), priced=priced) for leg in report.legs]
    widths = [
        max([len(header), *(len(row[index]) for row in rows)])
        for index, header in enumerate(headers)
    ]
    lines = [_line(headers, widths), "  " + "  ".join("-" * width for width in widths)]
    lines.extend(_line(row, widths) for row in rows)
    return "\n".join(lines)


def _row(leg: LegResult, price: Price | None, *, priced: bool) -> list[str]:
    row = [
        leg.name,
        str(len(leg.repeats)),
        leg.outcome.marker,
        leg.score_cell(),
        ", ".join(str(score) for score in leg.scores) or "-",
        leg.attribution_cell(),
        _tokens_cell(leg.usage),
        _seconds_cell(leg),
        _requests_cell(leg.usage),
    ]
    if priced:
        row.append(_cost_cell(leg, price))
    return row


def _line(cells: Sequence[str], widths: Sequence[int]) -> str:
    padded = "  ".join(cell.ljust(width) for cell, width in zip(cells, widths, strict=True))
    return f"  {padded}".rstrip()


def _tokens_cell(usage: MeteredUsage) -> str:
    """Tokens, or an explicit statement that there are none to report.

    Never ``0``. An endpoint that volunteers no ``usage`` block is not a free one, and rendering
    it as zero crowns whichever model sits behind the least complete server as the cheapest in the
    sweep.
    """
    if not usage.any_reported:
        return "not reported"
    cached = f" ({usage.cached} cached)" if usage.cached else ""
    return f"{usage.prompt}+{usage.completion}={usage.total}{cached}"


def _seconds_cell(leg: LegResult) -> str:
    mean = leg.mean_seconds
    if mean is None:
        return "-"
    return f"{leg.seconds:.1f} ({mean:.1f}/run)"


def _requests_cell(usage: MeteredUsage) -> str:
    """HTTP requests, and how many of them a negotiation threw away.

    "Answered first time" and "answered on the third attempt after abandoning structured output"
    are both successes and are not the same efficiency, and the request count is the only place
    that difference is visible. A completer that issues no HTTP at all -- a scripted one -- says
    how many logical completions it served instead of claiming zero requests as an efficiency.
    """
    if not usage.requests:
        return f"{usage.completions} completion(s)" if usage.completions else "-"
    wasted = usage.wasted_requests
    return f"{usage.requests}" + (f" ({wasted} wasted)" if wasted else "")


def _cost_cell(leg: LegResult, price: Price | None) -> str:
    """Money, and two different reasons there might be none.

    "Nobody gave me a price for this model" and "the endpoint reported no tokens to price" are
    separate facts with separate fixes, and one cell reading ``-`` for both sends the reader to
    the wrong one.
    """
    if price is None:
        return "no price given"
    cost = price.of(leg.usage)
    if cost is None:
        return "no tokens reported"
    return f"{cost:.4f} {price.currency}"


def _clip(text: str) -> str:
    flat = " ".join(text.split())
    if len(flat) <= _DETAIL_WIDTH:
        return flat
    return f"{flat[:_DETAIL_WIDTH]}..."


# =============================================================================
# THE MACHINE-READABLE FORM
# =============================================================================


def as_json(report: SweepReport) -> str:
    """The same report as JSON, for a CI step that wants to assert on it.

    Token counts are ``null`` rather than ``0`` wherever the endpoint reported nothing, for the
    same reason the table says "not reported": a consumer that sums this file must not be handed
    a zero it cannot tell from a measurement.

    A ``failure`` here is a string, and a string is where
    :class:`~harness.live.Failure`'s truthiness stops working: ``bool(Failure.NONE)`` is ``False``
    and ``bool("none")`` is ``True``. Read one back through ``Failure(value)`` before testing it,
    or use the ``about_the_model`` flag beside it, which is the question a CI step actually has --
    "did the model fail, or did the plumbing?" -- already answered.
    """
    return json.dumps(
        {
            "case": report.case,
            "tier": TIER,
            "repeats": report.repeats,
            "points_declared": report.points_declared,
            "items_declared": list(report.items_declared),
            "available": report.available,
            "ungradeable": [
                {"id": item.id, "weight": item.weight, "detail": item.detail}
                for item in report.ungradeable
            ],
            "not_graded": {
                "tier": "deterministic",
                "points": report.points_not_graded,
                "reason": "grades a driven notebook; this sweep grades the plan only",
            },
            "ok": report.ok,
            "legs": [_leg_json(leg, report.prices.get(leg.name)) for leg in report.legs],
            "notes": list(report.notes),
        },
        indent=2,
    )


def _leg_json(leg: LegResult, price: Price | None) -> dict[str, Any]:
    cost = leg.cost(price)
    return {
        "name": leg.name,
        "model": leg.spec.model,
        "base_url": leg.spec.base_url,
        "outcome": leg.outcome.value,
        # A consumer cannot tell a leg nobody could run from one that ran and did not answer by
        # the outcome alone -- both are "skip" -- and they need different actions.
        "ran": leg.ran,
        "scores": list(leg.scores),
        "available": leg.available,
        "best": max(leg.scores) if leg.scores else None,
        "worst": min(leg.scores) if leg.scores else None,
        "mean": leg.mean_score,
        "spread": leg.spread,
        "attribution": {failure.value: count for failure, count in leg.attributions.items()},
        "usage": _usage_json(leg.usage),
        "seconds": round(leg.seconds, 3),
        "mean_seconds": None if leg.mean_seconds is None else round(leg.mean_seconds, 3),
        "cost": None if cost is None else round(cost, 6),
        "currency": None if price is None else price.currency,
        "repeats": [_repeat_json(item) for item in leg.repeats],
    }


def _repeat_json(repeat: RepeatResult) -> dict[str, Any]:
    return {
        "repeat": repeat.repeat,
        "outcome": repeat.outcome.value,
        "earned": repeat.earned,
        "available": repeat.available,
        "failure": repeat.call.failure.value,
        # The attribution, decided once in `Failure` rather than re-derived by every consumer from
        # a list of failure names it would have to keep in step with this enum.
        "about_the_model": repeat.call.failure.about_the_model,
        "ran": repeat.ran,
        "detail": repeat.call.detail,
        "mode": repeat.call.mode,
        "omit_temperature": repeat.call.omit_temperature,
        "negotiated": repeat.call.negotiated,
        "seconds": round(repeat.call.seconds, 3),
        "usage": _usage_json(repeat.call.usage),
        "items": [
            {
                "id": item.id,
                "outcome": item.outcome.value,
                "weight": item.weight,
                "detail": item.detail,
            }
            for item in (repeat.tier.items if repeat.tier is not None else ())
        ],
    }


def _usage_json(usage: MeteredUsage) -> dict[str, Any]:
    """Counts always; tokens only when somebody counted them.

    ``measured`` here is :attr:`~harness.live.MeteredUsage.any_reported` -- "there is a number in
    this object worth reading" -- and it is deliberately not the same question as
    ``fully_reported``, which is carried beside it so a consumer summing a column can tell a
    complete bill from a partial one.
    """
    any_reported = usage.any_reported
    return {
        "measured": any_reported,
        "fully_reported": usage.fully_reported,
        "prompt": usage.prompt if any_reported else None,
        "completion": usage.completion if any_reported else None,
        "cached": usage.cached if any_reported else None,
        "total": usage.total if any_reported else None,
        "requests": usage.requests,
        "answered": usage.answered,
        "completions": usage.completions,
        "reported": usage.reported,
        "wasted_requests": usage.wasted_requests,
    }
