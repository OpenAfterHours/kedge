"""Grade the *code generation* seam: an approved plan in, a driven notebook out, no kernel.

`run.py` grades a conversion somebody committed. That proves the graders work; it says nothing
about whether a model can write one. This module closes that for the half of the conversion a
model actually writes -- the cell bodies -- and it does it without a marimo kernel, without the
chat loop, and without any tool surface at all.

The pipeline is five steps, and every one of them is machinery that already exists:

1. **Scaffold the approved plan and fill its holes.** That is :func:`kedge.agent.fill.fill_holes`
   -- the driver ``kedge convert`` runs, called rather than reimplemented, reached through the
   eval's own names in :mod:`harness.cellgen`. :func:`kedge.notebook.scaffold.build_cells` decides
   the notebook's structure, the model is asked only for what sits under each ``TODO(kedge)``
   marker, and every answer goes through :func:`kedge.agent.validate.validate_cell`. The
   measurement is worth only as much as that identity: a model's score here is a score on the
   thing a user runs.
2. **Lay the cells out and render the file** -- :mod:`harness.render`, which also reports every
   place the scaffolder's own order would not have worked.
3. **Re-key the case's script** onto the scaffolder's widget names -- :mod:`harness.align`.
4. **Drive it** with :mod:`harness.drive`, the case's own graders included, through
   :func:`~harness.findings.aligned_drives`.
5. **Grade it** with :func:`harness.grade.grade` and the case's own rubric, unmodified, and
   report what the run said about kedge as well as about the model -- :mod:`harness.findings`.

## Seven outcomes, not two

A model that scores nothing must be distinguishable from a harness that could not measure it, so
a hole ends in one of six states (:class:`~harness.cellgen.CellOutcome`) and a whole conversion
in one of seven (:class:`ConversionOutcome`). "No plan was ever proposed", "nothing was ever
asked", "the endpoint failed part way through", "the model never satisfied the validation gate",
"the notebook would not render", "the notebook rendered and then stopped halfway" and "the
notebook ran and graded badly" are seven different things to tell somebody choosing a model, and
only the last of them is about the model's judgement.

Two per-hole states arrived with the product's driver. ``SKIPPED`` is what a run abandoned after a
transport failure records for the holes it never reached; :func:`~harness.cellgen.convert` does
not abandon, so it does not arise here -- see that module for why the eval presses on where
``kedge convert`` stops. ``UNFILLABLE`` is a hole nothing can be asked to fill, and it matters
here more than anywhere: a stage whose hand-off declares no statement scaffolds to a cell with no
placeholder, that cell used to be dropped where it was found, and on ``--convert M --plan-from M``
the model that wrote the statement-less hand-off had the consequence taken out of **its own**
generation denominator and was printed as filling every hole it was given.

:attr:`ConversionOutcome.NO_PLAN` exists for the composed path, where the plan is asked of a model
rather than read off disk (``--convert MODEL --plan-from MODEL``). A run that never got a plan
never scaffolded a cell, and folding it into ``INCOMPLETE`` -- whose docstring reads "the gaps are
the model's" -- would file an unreachable endpoint as a model that writes bad code.

:attr:`ConversionOutcome.INTERRUPTED` earns its place from a measured failure. A completer that
answers the first hole and then raises on every request used to come back as ``INCOMPLETE`` --
whose docstring reads "the notebook is a scaffold with gaps in it", which sounds like the model's
fault -- with a score of ``22/37`` printed beside it and no word about where the other sixteen
rubric points went. Only the all-errors case reached ``NO_MODEL``. A partial transport failure is
now its own outcome, and :class:`~harness.findings.Coverage` states the denominator it moved. Its
own docstring is now literally rather than approximately true: the holes after the failure really
were never asked for, and say so.

## What this does not measure

Nothing about the chat loop, nothing about the plan (that is Phase 1), and nothing about token
cost -- ``complete()`` is taken as a :class:`~kedge.plan.propose.Completer` and this module is
indifferent to whether it is metered. Wrap it before handing it over and the numbers appear
without a line changing here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harness.align import align_inputs
from harness.cellgen import convert
from harness.drive import run_notebook, unused_inputs, workspace_overrides
from harness.findings import ReDriveLog, aligned_drives, coverage_for, scaffold_defects
from harness.grade import grade
from harness.render import plan_layout, write_notebook
from kedge.agent.validate import MAX_VALIDATION_ATTEMPTS
from kedge.notebook.scaffold import PlanNotApprovedError, ScaffoldError

if TYPE_CHECKING:
    from harness.align import Alignment
    from harness.cellgen import ConversionResult
    from harness.drive import NotebookRun
    from harness.findings import Coverage, Defect
    from harness.model import EvalReport
    from harness.render import Layout
    from kedge.analysis.model import WorkbookAnalysis
    from kedge.plan.model import ProcessPlan
    from kedge.plan.propose import Completer

logger = logging.getLogger(__name__)

__all__ = [
    "ConversionOutcome",
    "ConversionReport",
    "convert_and_grade",
    "driven_run",
    "no_plan_proposed",
]


class ConversionOutcome(StrEnum):
    """How a whole conversion came out, from "never asked a model" to "graded".

    Ordered by how far the pipeline got. Anything short of :attr:`GRADED` means the eval score,
    if there is one at all, is over a notebook that is missing something -- which is why
    :meth:`ConversionReport.headline` prints this beside the score rather than instead of it.
    """

    GRADED = "graded"
    """Every hole filled, the notebook rendered, the run reached the end, the rubric was applied."""

    STOPPED = "stopped"
    """It rendered, and the driven run halted or raised before the end. Partly gradeable."""

    UNRENDERABLE = "unrenderable"
    """The cells would not render as a notebook file. Nothing downstream could run."""

    INCOMPLETE = "incomplete"
    """At least one hole was never filled: the model wrote bodies the gate would not accept, or
    wrote nothing, or wrote a plan whose hand-off left a cell nothing could be asked to fill. The
    notebook is a scaffold with gaps in it, and the gaps are the model's -- on the composed path
    that is true of the last case too, since the plan is the same model's."""

    INTERRUPTED = "interrupted"
    """Some -- not all -- requests to the model failed outright, so part of the notebook was
    never asked for. A transport failure, a refused request, an exhausted script: not the model's
    judgement, and a score printed against it is over a rubric that shrank for reasons nothing to
    do with quality. Told apart from :attr:`INCOMPLETE` because reporting the two the same is how
    an endpoint's bad afternoon gets written down as a model's."""

    NO_MODEL = "no_model"
    """No request to the model was ever answered, or there were no holes. Nothing was measured.

    Asked as :attr:`kedge.agent.fill.FillReport.unmeasured` rather than by counting
    :attr:`~harness.cellgen.CellOutcome.ERROR`, and the two differ twice. A run abandoned at the
    first transport failure leaves the tail ``SKIPPED``, so an all-errors test never fires -- this
    member would be unreachable for any caller that stops, and a dead endpoint would come back as
    ``INTERRUPTED``, whose docstring says *some, not all* of the requests failed. And a hole whose
    first attempt was rejected and whose retry was the one the endpoint dropped ends ``ERROR``
    with the gate's verdict sitting in its first attempt: counting outcomes calls that "nothing
    was measured" and clears a model that wrote pandas."""

    NO_PLAN = "no_plan"
    """No plan the pipeline could use, so nothing was ever scaffolded and no cell was ever asked for.

    Two ways in, and they are the same result. On the composed path
    (``--convert MODEL --plan-from MODEL``) the plan is asked of a model and none arrived. Either
    way, or on any path, the plan that *did* arrive may be one :func:`~kedge.notebook.scaffold.
    build_cells` refuses -- and that used to be a traceback out of ``main()`` with no report, no
    outcome and no statement of what the proposal had already cost.

    It earns a member of its own for the same reason :attr:`INTERRUPTED` did: a run that ended
    before a single cell was requested must not be reported as a model writing bad cells. Whether
    the *plan* failure was the model's judgement is a question :class:`~harness.live.Failure`
    already answers, and :attr:`ConversionReport.detail` carries that attribution rather than
    re-deciding it here."""


@dataclass(frozen=True, slots=True)
class ConversionReport:
    """A generated conversion, driven and graded, with the denominator attached at every level.

    Two scores, deliberately not folded into one. :attr:`result` says how much of the notebook
    the model managed to write; :attr:`report` says how good what it wrote turned out to be. A
    single number would make "wrote four of five cells, and they were right" and "wrote five, one
    of them wrong" indistinguishable, and they are not the same result. :attr:`coverage` says
    what the second number was taken over, and :attr:`defects` says what went wrong that no model
    could have caused.
    """

    case: str
    result: ConversionResult | None
    """``None`` only for :attr:`ConversionOutcome.NO_PLAN`, where nothing was ever scaffolded and
    so there is no generation denominator to state. Every other outcome has one, including the
    ones where it is zero."""
    outcome: ConversionOutcome
    report: EvalReport | None = None
    notebook: Path | None = None
    run_summary: str = ""
    alignment: Alignment | None = None
    layout: Layout | None = None
    coverage: Coverage | None = None
    defects: tuple[Defect, ...] = ()
    detail: str = ""
    plan_origin: str = ""
    """Where the plan came from -- a model, or a file somebody committed. Empty says nothing.

    :meth:`headline` renders it on a line of its own, above the score, whenever it is set, and
    :attr:`plan_is_the_models` decides which of two very different caveats goes with it. Both
    matter because the structural tier is graded against *the plan*, whoever wrote it: a plain
    ``--convert`` figure is not "the cell bodies alone", it is the model's cell bodies over a
    human's plan, and a quarter of its points are the human's. That is the same confound
    ``--plan-from`` without ``--convert`` is refused for, only smaller, and the fix is to say so
    rather than to refuse a useful mode."""

    plan_is_the_models: bool = False
    """Whether the model being measured also wrote the plan. Chooses the caveat, not the score."""

    @property
    def ok(self) -> bool:
        """Every hole filled, the notebook driven to the end, and nothing gradeable failed."""
        return (
            self.outcome is ConversionOutcome.GRADED and self.report is not None and self.report.ok
        )

    def headline(self) -> str:
        """One line carrying the score, the generation denominator, and how far it got."""
        generation = self.result.summary_line() if self.result is not None else "no plan to convert"
        left = f"{self.case}: {self.outcome.value}, {generation}"
        if self.defects:
            left += f"; {len(self.defects)} scaffolder defect(s) found"
        if self.report is None:
            head = f"{left}; not graded -- {self.detail}" if self.detail else f"{left}; not graded"
            return "\n".join([head, *self._provenance()])
        lines = [left, *self._provenance(), self.report.headline()]
        if self.coverage is not None:
            lines.append(self.coverage.headline())
        return "\n".join(lines)

    def _provenance(self) -> list[str]:
        """The line that stops one path's score being read as the other's.

        Rendered above the number rather than below it, because a caveat under a total is a
        caveat nobody has read by the time they have read the total.
        """
        if not self.plan_origin:
            return []
        structural = self._structural_share()
        if self.plan_is_the_models:
            return [
                f"COMPOSED PATH -- plan {self.plan_origin}, and the cell bodies are the same "
                f"model's. Every point on the board is the model's{structural}. Not comparable "
                f"with a plain --convert figure, whose structural tier is a human's plan."
            ]
        return [
            f"PLAN NOT THE MODEL'S -- {self.plan_origin}. The model wrote the cell bodies and "
            f"nothing else{structural}, so this total is not a whole conversion's."
        ]

    def _structural_share(self) -> str:
        """How many of the graded points came from the plan rather than from the cell bodies."""
        if self.report is None:
            return ""
        for tier in self.report.tiers:
            if tier.name == "structural" and tier.available:
                return f"; {tier.available} of {self.report.available} points are structural"
        return ""

    def render(self) -> str:
        blocks = [self.headline(), ""]
        if self.defects:
            blocks.append("Defects in the scaffolded conversion (kedge's, not the model's)")
            blocks.extend(defect.render() for defect in self.defects)
            blocks.append("")
        if self.result is not None:
            blocks.extend([self.result.render(), ""])
        if self.coverage is not None and (self.coverage.undriven or self.coverage.ungradeable):
            blocks.extend([self.coverage.render(), ""])
        if self.report is not None:
            blocks.append(self.report.render())
        else:
            blocks.extend(line for line in (f"run: {self.run_summary}", self.detail) if line)
        return "\n".join(blocks).rstrip() + "\n"

    def exit_code(self) -> int:
        return 0 if self.ok else 1


def _case_name(case: Any) -> str:
    """What to call the case in a report: ``adjustment_signoff``, not ``adjustment_signoff.case``."""
    return getattr(case, "__name__", "case").split(".")[0]


def no_plan_proposed(
    case: Any, *, plan_origin: str, detail: str, plan_is_the_models: bool = True
) -> ConversionReport:
    """A composed run that ended before anything was scaffolded, with the reason kept.

    The composed path asks a model for the plan first, and that request can fail every way a
    request can: the key was never in the keyring, the endpoint refused the JSON schema, a proxy
    returned a 404 for an id nobody enabled, or the model's own output never validated as a plan
    after every repair round. Only the last of those is the model's judgement, and
    :attr:`~harness.live.Failure.about_the_model` is where that is already decided -- so this
    takes the sentence rather than re-deriving it, and refuses to invent a conversion score for a
    conversion nobody attempted.

    Args:
        case: The eval case, for its name.
        plan_origin: Who the plan was asked of.
        detail: What happened, already attributed.
        plan_is_the_models: Whether the plan was to be the measured model's. True on the only
            path that reaches this today; a parameter so a caller cannot mislabel a run silently.

    Returns:
        A :class:`ConversionReport` with no result, no report and a non-zero exit code.
    """
    return ConversionReport(
        case=_case_name(case),
        result=None,
        outcome=ConversionOutcome.NO_PLAN,
        plan_origin=plan_origin,
        plan_is_the_models=plan_is_the_models,
        detail=detail,
    )


def _script_keys(case: Any) -> tuple[str, ...]:
    """The names a case uses for the human's actions, without needing real hand-in files.

    ``script_for`` takes the two hand-in paths because its *values* are those paths; its keys
    never depend on them, and alignment is only ever about keys. Asking for them with placeholder
    paths keeps :func:`align_inputs` free of the temporary directory the real drive needs.
    """
    placeholder = Path("pre.csv")
    return tuple(case.script_for(placeholder, Path("post.csv")))


class _DrivenCase:
    """A case whose human script has been re-keyed onto one notebook's widget names.

    ``harness.grade.grade`` takes the case as data, so standing in front of two of its attributes
    is all it takes to drive a differently-named notebook through the unmodified graders:
    ``script_for``, and the grader tables, which are wrapped so that every drive a grader makes
    can be attributed to it. Everything else -- the rubric, the hand-in writer, the workbook --
    passes straight through to the real case, which is the point: nothing about the scoring is
    touched.

    What this deliberately does *not* try to do is reach the module-level functions a grader
    calls by global name. It cannot, and pretending otherwise is what left five graders driving a
    notebook that had stopped in its third cell. :func:`aligned_drives` handles those.
    """

    def __init__(self, case: Any, alignment: Alignment, log: ReDriveLog) -> None:
        self._case = case
        self._alignment = alignment
        self._log = log

    def script_for(self, pre: Path, post: Path) -> dict[str, Any]:
        return self._alignment.apply(self._case.script_for(pre, post))

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._case, name)
        if name in ("DETERMINISTIC", "STRUCTURAL") and isinstance(value, dict):
            return {item: self._log.watching(item, grader) for item, grader in value.items()}
        return value


def convert_and_grade(
    case: Any,
    plan: ProcessPlan,
    *,
    completer: Completer,
    notebook: Path,
    analysis: WorkbookAnalysis | None = None,
    model: str = "",
    temperature: float = 0.2,
    max_attempts: int = MAX_VALIDATION_ATTEMPTS,
    plan_origin: str = "",
    plan_is_the_models: bool = False,
) -> ConversionReport:
    """The whole seam: scaffold, generate, lay out, render, drive, grade.

    Stops at the first step that cannot proceed and says which one, rather than carrying a broken
    artifact into the next step and letting the rubric take the blame for it. A conversion with an
    unfilled hole is still rendered and still graded -- a scaffold with a passthrough in it runs,
    and what the rest of the notebook got right is worth knowing -- but the outcome says
    ``INCOMPLETE`` and the headline leads with it, so no reader mistakes the score for a whole
    conversion's score.

    Args:
        case: An eval case module, as ``harness.grade.load_case`` returns.
        plan: The approved plan to convert. The reference plan for a baseline; a model-written
            one, from Phase 1, to measure the two seams end to end.
        completer: The model, metered or not.
        notebook: Where to write the generated notebook. It is kept: a conversion nobody can read
            afterwards cannot be argued with.
        analysis: The workbook analysis. Falls back to analysing ``case.WORKBOOK``.
        model: The model name on each request.
        temperature: Sent with each request.
        max_attempts: Validation attempts per hole.
        plan_origin: Where the plan came from, in words. Carried onto the report so neither
            path's total can be read as the other's.
        plan_is_the_models: Whether ``completer``'s model also wrote the plan. Chooses which
            caveat the report prints; it never changes a score.

    Returns:
        The :class:`ConversionReport`.
    """
    if analysis is None:
        from kedge.analysis import analyse

        analysis = analyse(case.WORKBOOK)

    try:
        result = convert(
            plan,
            completer=completer,
            analysis=analysis,
            model=model,
            temperature=temperature,
            max_attempts=max_attempts,
            workbook_path=case.WORKBOOK,
        )
    except (ScaffoldError, PlanNotApprovedError) as exc:
        # `convert` scaffolds before it asks for anything, so this is reached with nothing spent
        # on cells -- but on the composed path a plan proposal has already been billed, and a
        # traceback out of `main()` reported neither the cost nor the reason. A plan kedge will
        # not build a notebook from is a result about whoever wrote the plan, and this is the
        # outcome that says so.
        return ConversionReport(
            case=_case_name(case),
            result=None,
            outcome=ConversionOutcome.NO_PLAN,
            plan_origin=plan_origin,
            plan_is_the_models=plan_is_the_models,
            detail=(
                f"kedge will not scaffold this plan, so no cell was ever asked for and nothing "
                f"about the model's code was measured: {type(exc).__name__}: {exc}"
            ),
        )

    if not result.holes:
        return ConversionReport(
            case=_case_name(case),
            result=result,
            outcome=ConversionOutcome.NO_MODEL,
            plan_origin=plan_origin,
            plan_is_the_models=plan_is_the_models,
            detail=(
                "the scaffolder left no TODO(kedge) holes in this plan, so the model was never "
                "asked for anything and nothing about it was measured. That is a statement about "
                "the plan, not about the model."
            ),
        )
    if result.unmeasured:
        # Not "every hole errored": the driver abandons a run after the first transport failure,
        # so the rest are SKIPPED and that test would never fire again. `unmeasured` asks the
        # question the outcome is named for -- did any hole come back with a body at all. The
        # first hole is the one that failed: nothing is skipped until something has errored.
        first = result.cells[0]
        return ConversionReport(
            case=_case_name(case),
            result=result,
            outcome=ConversionOutcome.NO_MODEL,
            plan_origin=plan_origin,
            plan_is_the_models=plan_is_the_models,
            detail=(
                "no request to the model was answered, so nothing about the model was measured. "
                f"First failure: {first.detail}"
            ),
        )

    layout = plan_layout(result.names, result.codes)
    try:
        write_notebook(result, notebook, layout)
    except (ValueError, OSError) as exc:
        return ConversionReport(
            case=_case_name(case),
            result=result,
            outcome=ConversionOutcome.UNRENDERABLE,
            plan_origin=plan_origin,
            plan_is_the_models=plan_is_the_models,
            layout=layout,
            detail=f"the conversion would not render as a notebook: {exc}",
        )

    alignment = align_inputs(notebook, _script_keys(case))
    log = ReDriveLog()
    driven = _DrivenCase(case, alignment, log)
    with aligned_drives(alignment, log):
        report = grade(driven, notebook=notebook, plan=plan)

    defects = scaffold_defects(result, layout, alignment)
    report = replace(report, notes=(*report.notes, *alignment.notes()))
    coverage = coverage_for(report, log)

    return ConversionReport(
        case=_case_name(case),
        result=result,
        outcome=_outcome_of(result, log.run),
        report=report,
        notebook=notebook,
        run_summary=log.run.summary_line() if log.run is not None else "",
        alignment=alignment,
        layout=layout,
        coverage=coverage,
        defects=defects,
        plan_origin=plan_origin,
        plan_is_the_models=plan_is_the_models,
    )


def _outcome_of(result: ConversionResult, run: NotebookRun | None) -> ConversionOutcome:
    """How far the pipeline got, decided from objects rather than from prose.

    ``STOPPED`` used to be decided by ``run_summary.startswith("ran ")``, where ``run_summary``
    was scraped out of a note the grader happened to write -- so one edit to
    :meth:`harness.drive.NotebookRun.summary_line` would have made every conversion in every
    report ``STOPPED``, silently and for ever. The run itself is reached through the graders'
    own context (:class:`ReDriveLog`), and ``completed`` is a property of it.

    A run nobody observed is not reported as stopped: an unobserved run and a halted one are
    different, and guessing between them is what this is here to stop.
    """
    if result.errored:
        return ConversionOutcome.INTERRUPTED
    if not result.complete:
        return ConversionOutcome.INCOMPLETE
    if run is not None and not run.completed:
        return ConversionOutcome.STOPPED
    return ConversionOutcome.GRADED


def driven_run(notebook: Path, case: Any, root: Path) -> tuple[NotebookRun, tuple[str, ...]]:
    """Drive a generated notebook once, the way :func:`harness.grade.grade` drives one.

    The run without the rubric. "Did it reach the end" is worth asking on its own -- it is the
    difference between :attr:`ConversionOutcome.STOPPED` and a bad score -- and answering it
    needs no workbook read and no duckdb. It re-states ``grade``'s four-line override block
    rather than sharing it, because ``harness/grade.py`` inlines that block inside ``grade``
    and is not this module's to refactor.

    Args:
        notebook: The generated notebook.
        case: The eval case, for its hand-ins and its script.
        root: A directory of this run's own, for the managed store and the run records.

    Returns:
        The :class:`~harness.drive.NotebookRun`, and every scripted action that was not played --
        the ones alignment could find no widget for *and* the ones the run never bound. The first
        set used to be invisible here: :meth:`Alignment.bind` drops them before
        :func:`~harness.drive.unused_inputs` can see them, so the second return value was
        structurally incapable of holding what its docstring promised, and the test asserting it
        was empty was asserting nothing.
    """
    alignment = align_inputs(notebook, _script_keys(case))
    pre, post = case.write_handins(root / "handins-in")
    script, unplayed = alignment.bind(case.script_for(pre, post))
    run = run_notebook(
        notebook,
        inputs=script,
        overrides=workspace_overrides(root, case.WORKBOOK),
    )
    return run, tuple(sorted({*unplayed, *unused_inputs(run, script)}))
