"""Grade the *code generation* seam: an approved plan in, a driven notebook out, no kernel.

`run.py` grades a conversion somebody committed. That proves the graders work; it says nothing
about whether a model can write one. This module closes that for the half of the conversion a
model actually writes -- the cell bodies -- and it does it without a marimo kernel, without the
chat loop, and without any tool surface at all.

The pipeline is five steps, and every one of them is machinery that already exists:

1. **Scaffold the approved plan and fill its holes.** :mod:`harness.cellgen` owns that half:
   :func:`kedge.notebook.scaffold.build_cells` decides the notebook's structure, the model is
   asked only for what sits under each ``TODO(kedge)`` marker, and every answer goes through
   :func:`kedge.agent.validate.validate_cell`.
2. **Lay the cells out and render the file** -- :mod:`harness.render`, which also reports every
   place the scaffolder's own order would not have worked.
3. **Re-key the case's script** onto the scaffolder's widget names -- :mod:`harness.align`.
4. **Drive it** with :mod:`harness.drive`, the case's own graders included, through
   :func:`~harness.findings.aligned_drives`.
5. **Grade it** with :func:`harness.grade.grade` and the case's own rubric, unmodified, and
   report what the run said about kedge as well as about the model -- :mod:`harness.findings`.

## Six outcomes, not two

A model that scores nothing must be distinguishable from a harness that could not measure it, so
a hole ends in one of four states (:class:`~harness.cellgen.CellOutcome`) and a whole conversion
in one of six (:class:`ConversionOutcome`). "Nothing was ever asked", "the endpoint failed part
way through", "the model never satisfied the validation gate", "the notebook would not render",
"the notebook rendered and then stopped halfway" and "the notebook ran and graded badly" are six
different things to tell somebody choosing a model, and only the last of them is about the
model's judgement.

:attr:`ConversionOutcome.INTERRUPTED` earns its place from a measured failure. A completer that
answers the first hole and then raises on every request used to come back as ``INCOMPLETE`` --
whose docstring reads "the notebook is a scaffold with gaps in it", which sounds like the model's
fault -- with a score of ``22/37`` printed beside it and no word about where the other sixteen
rubric points went. Only the all-errors case reached ``NO_MODEL``. A partial transport failure is
now its own outcome, and :class:`~harness.findings.Coverage` states the denominator it moved.

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
from harness.cellgen import CellOutcome, convert
from harness.drive import run_notebook, unused_inputs, workspace_overrides
from harness.findings import ReDriveLog, aligned_drives, coverage_for, scaffold_defects
from harness.grade import grade, handins_for, human_script, placeholder_handins
from harness.render import plan_layout, write_notebook
from kedge.agent.validate import MAX_VALIDATION_ATTEMPTS

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
    wrote nothing. The notebook is a scaffold with gaps in it, and the gaps are the model's."""

    INTERRUPTED = "interrupted"
    """Some -- not all -- requests to the model failed outright, so part of the notebook was
    never asked for. A transport failure, a refused request, an exhausted script: not the model's
    judgement, and a score printed against it is over a rubric that shrank for reasons nothing to
    do with quality. Told apart from :attr:`INCOMPLETE` because reporting the two the same is how
    an endpoint's bad afternoon gets written down as a model's."""

    NO_MODEL = "no_model"
    """Every hole ended in :attr:`~harness.cellgen.CellOutcome.ERROR`, or there were no holes.
    Nothing was measured about the model."""


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
    result: ConversionResult
    outcome: ConversionOutcome
    report: EvalReport | None = None
    notebook: Path | None = None
    run_summary: str = ""
    alignment: Alignment | None = None
    layout: Layout | None = None
    coverage: Coverage | None = None
    defects: tuple[Defect, ...] = ()
    detail: str = ""

    @property
    def ok(self) -> bool:
        """Every hole filled, the notebook driven to the end, and nothing gradeable failed."""
        return (
            self.outcome is ConversionOutcome.GRADED and self.report is not None and self.report.ok
        )

    def headline(self) -> str:
        """One line carrying the score, the generation denominator, and how far it got."""
        left = f"{self.case}: {self.outcome.value}, {self.result.summary_line()}"
        if self.defects:
            left += f"; {len(self.defects)} scaffolder defect(s) found"
        if self.report is None:
            return f"{left}; not graded -- {self.detail}" if self.detail else f"{left}; not graded"
        lines = [left, self.report.headline()]
        if self.coverage is not None:
            lines.append(self.coverage.headline())
        return "\n".join(lines)

    def render(self) -> str:
        blocks = [self.headline(), ""]
        if self.defects:
            blocks.append("Defects in the scaffolded conversion (kedge's, not the model's)")
            blocks.extend(defect.render() for defect in self.defects)
            blocks.append("")
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


def _script_keys(case: Any) -> tuple[str, ...]:
    """The names a case uses for the human's actions, without needing real hand-in files.

    ``script_for`` takes the hand-in paths because its *values* are those paths; its keys never
    depend on them, and alignment is only ever about keys. Asking for them with placeholder paths
    keeps :func:`align_inputs` free of the temporary directory the real drive needs.
    """
    return tuple(human_script(case, placeholder_handins(case)))


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

    def script_for(self, *handins: Path, **named: Path) -> dict[str, Any]:
        """Both spreads, because which one arrives is the wrapped case's decision, not this one's."""
        return self._alignment.apply(self._case.script_for(*handins, **named))

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

    Returns:
        The :class:`ConversionReport`.
    """
    if analysis is None:
        from kedge.analysis import analyse

        analysis = analyse(case.WORKBOOK)

    result = convert(
        plan,
        completer=completer,
        analysis=analysis,
        model=model,
        temperature=temperature,
        max_attempts=max_attempts,
        workbook_path=case.WORKBOOK,
    )

    if not result.holes:
        return ConversionReport(
            case=_case_name(case),
            result=result,
            outcome=ConversionOutcome.NO_MODEL,
            detail=(
                "the scaffolder left no TODO(kedge) holes in this plan, so the model was never "
                "asked for anything and nothing about it was measured. That is a statement about "
                "the plan, not about the model."
            ),
        )
    if all(cell.outcome is CellOutcome.ERROR for cell in result.generated):
        return ConversionReport(
            case=_case_name(case),
            result=result,
            outcome=ConversionOutcome.NO_MODEL,
            detail=(
                "every request to the model failed, so nothing about the model was measured. "
                f"First failure: {result.generated[0].detail}"
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
    script, unplayed = alignment.bind(human_script(case, handins_for(case, root / "handins-in")))
    run = run_notebook(
        notebook,
        inputs=script,
        overrides=workspace_overrides(root, case.WORKBOOK),
    )
    return run, tuple(sorted({*unplayed, *unused_inputs(run, script)}))
