"""What a generated conversion says about kedge, and what the harness could not drive.

Two kinds of finding, kept apart from the score and printed above it.

**Defects** are things wrong with the *scaffolded* notebook, which no model could have caused.
Every one in here was found by generating a conversion and driving it rather than by reading the
scaffolder, and every one used to be either invisible or quietly worked around -- the file was
sorted into an order kedge does not emit, the duplicate head hand-in was fed from whichever
script key came first, and the missing confirmation step was a footnote about naming. A harness
that tidies up after the product hides the product's bugs and then measures models against the
tidied version.

**Coverage** is the denominator. ``evals/README.md`` reserves ``SKIP`` for an item that is
"ungradeable through nobody's fault", and this eval was using it for items the harness itself
could not drive: five graders re-drive the notebook with scripts of their own, the case stand-in
could not reach them, and ten of the rubric's points left the denominator without a word while
the headline printed 75%. :class:`Coverage` splits those apart and names them.

:func:`aligned_drives` is what makes the first problem fixable. The graders reach the driver
through ``harness.drive``'s module attributes, so standing in front of those catches every route
into it -- including ones a future case invents -- where standing in front of the case object
catches none of them.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from harness.model import Outcome
from kedge.notebook.codegen import analyse_cells
from kedge.notebook.scaffold import HEAD_CELL_NAMES

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
    from pathlib import Path

    from harness.align import Alignment
    from harness.cellgen import ConversionResult
    from harness.drive import NotebookRun
    from harness.model import EvalReport, ItemResult
    from harness.render import Layout
    from kedge.notebook.scaffold import ScaffoldCell

logger = logging.getLogger(__name__)

__all__ = [
    "Coverage",
    "Defect",
    "ReDrive",
    "ReDriveLog",
    "Ungraded",
    "aligned_drives",
    "coverage_for",
    "scaffold_defects",
]

# A local tuple of mutating statement openers used to live here. It is gone on purpose: whether a
# statement writes is now `kedge.sql.changes_data`, which judges the verb each top-level statement
# *opens with* rather than matching a prefix -- so `SELECT ... FOR UPDATE` and a comment reading
# "-- update the accruals first" no longer read as writes. Non-negotiable 3: SQL semantics belong
# in `kedge.sql`, and an eval keeping its own second opinion is how the two drift apart.


@dataclass(frozen=True, slots=True)
class ReDrive:
    """One extra run of the notebook, made by a grader rather than by the harness.

    Five of this rubric's graders drive the notebook again with a script of their own -- a
    different period, a paste instead of a file, a second pass with nothing supplied. They call
    the case module's own functions to do it, so standing in front of the case object reaches
    none of them; :func:`aligned_drives` stands in front of ``harness.drive`` instead, which is
    the one thing every route goes through.
    """

    item: str
    keys: tuple[str, ...] = ()
    unplayed: tuple[str, ...] = ()
    stopped_at: str | None = None
    failed_at: str | None = None

    @property
    def halted_in_the_head(self) -> bool:
        """Whether this run never got past the scaffolder's fixed head."""
        return (self.stopped_at or self.failed_at or "") in HEAD_CELL_NAMES

    @property
    def left_undriven(self) -> bool:
        """Whether this run's verdict is about the harness rather than about the notebook.

        Both halves are needed. Stopping in the fixed head means nothing the model wrote had run
        yet -- but a grader is entitled to stop there deliberately, and one of this rubric's does:
        it supplies no hand-in precisely to read the blocking message. What makes a stop the
        *harness's* is that the script asked for something the harness could not play. Supplied
        nothing and stopped is a finding about the notebook; supplied a hand-in, failed to bind
        it, and stopped is a finding about this file.
        """
        return self.halted_in_the_head and bool(self.unplayed)


class ReDriveLog:
    """Which grader was running when the notebook was driven, and how far it got.

    Attribution is the whole purpose. Without it a run that stopped in the third cell produces
    four skips and a failure with no way to tell them from a model that wrote bad code -- which
    is how ten of this rubric's points went missing, reported as ``SKIP``, a word
    ``evals/README.md`` reserves for "reasons that are nobody's fault".
    """

    def __init__(self) -> None:
        self.current: str = ""
        self.drives: list[ReDrive] = []
        self.run: NotebookRun | None = None

    def watching(self, item: str, grader: Callable[[Any], ItemResult]) -> Callable[[Any], Any]:
        """Wrap one grader so drives made inside it are attributed to it."""

        def graded(context: Any) -> Any:
            self.current = item
            if self.run is None:
                self.run = getattr(context, "run", None)
            try:
                return grader(context)
            finally:
                self.current = ""

        return graded

    def record(self, *, keys: Iterable[str], unplayed: Sequence[str], run: NotebookRun) -> None:
        self.drives.append(
            ReDrive(
                item=self.current,
                keys=tuple(keys),
                unplayed=tuple(unplayed),
                stopped_at=run.stopped_at,
                failed_at=run.failed_at,
            )
        )

    def for_item(self, item: str) -> tuple[ReDrive, ...]:
        return tuple(drive for drive in self.drives if drive.item == item)


@contextmanager
def aligned_drives(alignment: Alignment, log: ReDriveLog) -> Iterator[None]:
    """Re-key every drive a grader makes, for as long as the grading lasts.

    ``harness.grade.grade`` binds :func:`~harness.drive.run_notebook` at import time, so the main
    run is untouched by this -- it is already aligned through the case stand-in. A case's own
    graders import it *inside* the function, which is what makes this seam work at all, and which
    is why it is preferred to standing in front of the case module's private ``_drive``: it
    catches any route into the driver, including ones a future case invents.

    Restored on the way out, because a stand-in left installed would silently re-key every
    later run in the process.
    """
    from harness import drive

    original_run = drive.run_notebook
    original_visible = drive.visible_cells

    def aligned_run(
        path: Path,
        *,
        inputs: Mapping[str, Any] | None = None,
        overrides: Mapping[str, Any] | None = None,
    ) -> NotebookRun:
        bound, unplayed = alignment.bind(inputs or {})
        result = original_run(path, inputs=bound, overrides=overrides)
        log.record(keys=tuple(inputs or ()), unplayed=unplayed, run=result)
        return result

    def aligned_visible(
        path: Path,
        *,
        inputs: Mapping[str, Any] | None = None,
        overrides: Mapping[str, Any] | None = None,
    ) -> tuple[str, ...]:
        bound, _unplayed = alignment.bind(inputs or {})
        return original_visible(path, inputs=bound, overrides=overrides)

    drive.run_notebook = aligned_run  # type: ignore[assignment]
    drive.visible_cells = aligned_visible  # type: ignore[assignment]
    try:
        yield
    finally:
        drive.run_notebook = original_run
        drive.visible_cells = original_visible


@dataclass(frozen=True, slots=True)
class Ungraded:
    """One rubric item that did not contribute to the score, and why."""

    id: str
    weight: int
    outcome: str
    why: str

    def render(self) -> str:
        return f"    - {self.id} ({self.weight}) [{self.outcome.upper()}] -- {self.why}"


@dataclass(frozen=True, slots=True)
class Coverage:
    """What the score was taken over, and what it was not.

    Two things a bare ``40/53 (75%)`` will not tell anybody. The rubric declares more points than
    that -- items that skipped are simply absent from the denominator, by design
    (:attr:`harness.model.ItemResult.available`) -- so the headline share is over a rubric that
    shrank without saying so. And some of the shrinking was *the harness's own fault*, which
    ``SKIP`` explicitly does not mean: ``evals/README.md`` reserves it for "an item can be
    ungradeable through nobody's fault". :attr:`undriven` is the category that word was hiding.
    """

    earned: int
    graded: int
    declared: int
    undriven: tuple[Ungraded, ...] = ()
    """Items whose verdict rests on a run that never got past the scaffolder's fixed head, so
    nothing the model wrote was ever executed for them. Not the model's result, and not
    "nobody's fault" either."""

    ungradeable: tuple[Ungraded, ...] = ()
    """Items that skipped for reasons outside this eval -- a missing optional dependency, a
    knowledge pack nobody has written."""

    @property
    def undriven_points(self) -> int:
        return sum(item.weight for item in self.undriven)

    def headline(self) -> str:
        share = f" ({100.0 * self.earned / self.graded:.0f}%)" if self.graded else ""
        line = f"coverage: {self.earned}/{self.graded}{share} of a {self.declared}-point rubric"
        if self.undriven:
            line += f"; {self.undriven_points} point(s) the harness could not drive"
        return line

    def render(self) -> str:
        blocks = [self.headline()]
        if self.undriven:
            blocks.append(
                f"  NOT DRIVEN -- {len(self.undriven)} item(s) whose run stopped in the "
                f"scaffolder's fixed head, before anything the model wrote:"
            )
            blocks.extend(item.render() for item in self.undriven)
        if self.ungradeable:
            blocks.append(f"  ungradeable for reasons outside this eval ({len(self.ungradeable)}):")
            blocks.extend(item.render() for item in self.ungradeable)
        return "\n".join(blocks)


def coverage_for(report: EvalReport, log: ReDriveLog | None = None) -> Coverage:
    """Partition everything the rubric declared into scored, undriven and ungradeable.

    Args:
        report: The graded report.
        log: The record of every drive a grader made, for attribution. Without one, nothing can
            be attributed to the harness and every skip is reported as ungradeable -- which is
            the honest answer when nobody watched.

    Returns:
        The :class:`Coverage`.
    """
    undriven: list[Ungraded] = []
    ungradeable: list[Ungraded] = []
    declared = 0
    for tier in report.tiers:
        for item in tier.items:
            declared += item.weight
            if item.outcome is Outcome.PASS:
                continue
            drives = log.for_item(item.id) if log is not None else ()
            halted = [drive for drive in drives if drive.left_undriven]
            if halted:
                where = halted[0].stopped_at or halted[0].failed_at
                why = (
                    f"its run stopped in {where!r}, in the scaffolder's fixed head, with "
                    f"scripted actions the harness could not play: {', '.join(halted[0].unplayed)}"
                )
                undriven.append(
                    Ungraded(id=item.id, weight=item.weight, outcome=item.outcome.value, why=why)
                )
            elif item.outcome in (Outcome.SKIP, Outcome.BLOCKED):
                ungradeable.append(
                    Ungraded(
                        id=item.id,
                        weight=item.weight,
                        outcome=item.outcome.value,
                        why=item.detail.splitlines()[0] if item.detail else "no reason given",
                    )
                )
    return Coverage(
        earned=report.earned,
        graded=report.available,
        declared=declared,
        undriven=tuple(undriven),
        ungradeable=tuple(ungradeable),
    )


@dataclass(frozen=True, slots=True)
class Defect:
    """Something wrong with the *scaffolded* conversion, which no model could have caused.

    Kept apart from the score and printed above it. A generated conversion exercises the
    scaffolder as hard as it exercises the model, and every one of these was found by running
    this eval rather than by reading the scaffolder -- which is the argument for reporting them
    here rather than absorbing them and grading the tidied version.
    """

    id: str
    summary: str
    detail: str
    evidence: str = ""

    def render(self) -> str:
        lines = [f"  [{self.id}] {self.summary}"]
        lines.extend(f"      {line}" for line in self.detail.splitlines())
        if self.evidence:
            lines.append(f"      evidence: {self.evidence}")
        return "\n".join(lines)


def _decision_tokens(cells: Sequence[ScaffoldCell]) -> dict[str, str]:
    """Name of each gate token, to the cell whose approval or confirmation it records.

    A checkpoint's token is the decision cell itself -- the ``_ui`` cell beside it only builds
    the widgets -- and a mutating hand-off's is its ``_confirmed`` cell. Both mean the same thing
    to whatever reads them: a person has done something, and until they had, you were not to be
    shown.
    """
    approvals = {
        cell.name for cell in cells if cell.role == "checkpoint" and not cell.name.endswith("_ui")
    }
    confirmations = {cell.name for cell in cells if cell.name.endswith("_confirmed")}
    return {name: name for name in approvals | confirmations}


def _reachable(start: str, names: Sequence[str], codes: Sequence[str]) -> set[str]:
    """Every name a cell reads, transitively, through the cells that define them."""
    analyses = analyse_cells(list(codes))
    defined_by = {name: index for index, analysis in enumerate(analyses) for name in analysis.defs}
    position = next((index for index, name in enumerate(names) if name == start), None)
    if position is None:
        return set()
    seen: set[str] = set()
    frontier = list(analyses[position].refs)
    while frontier:
        name = frontier.pop()
        if name in seen:
            continue
        seen.add(name)
        source = defined_by.get(name)
        if source is not None:
            frontier.extend(analyses[source].refs)
    return seen


def scaffold_defects(
    result: ConversionResult,
    layout: Layout,
    alignment: Alignment,
) -> tuple[Defect, ...]:
    """Everything this run found wrong with the notebook kedge scaffolded.

    Every one of these is the author's call to fix, not this harness's. What the harness owes
    them is a report that names each one at the point it was found instead of routing round it.
    """
    defects: list[Defect] = []
    names, codes = result.names, result.codes

    if layout.forward_references:
        rendered = "; ".join(reference.render() for reference in layout.forward_references)
        defects.append(
            Defect(
                id="forward-reference",
                summary="the scaffolder emits a cell above a name it reads",
                detail=(
                    "marimo decides when a cell runs from the graph, and where it appears from "
                    "the file. So a cell emitted above the name it reads renders in the wrong "
                    "place for the user, and in this harness -- which executes in file order, "
                    "with no kernel -- it is a NameError that reports the whole notebook as "
                    "broken. The file was rewritten in dependency order to grade it at all, "
                    f"which moved: {', '.join(layout.moved) or 'nothing'}. The layout graded "
                    "below is therefore not the layout kedge emits."
                ),
                evidence=rendered,
            )
        )

    tokens = _decision_tokens(result.scaffolded)
    panels = [cell.name for cell in result.scaffolded if cell.role == "reconcile"]
    for panel in panels:
        gated = sorted(tokens.keys() & _reachable(panel, names, codes))
        if gated:
            defects.append(
                Defect(
                    id="reconciliation-behind-its-own-checkpoint",
                    summary=f"{panel} is a dataflow descendant of {', '.join(gated)}",
                    detail=(
                        "scaffold._with_reconciliation exists to put the panel *before* the "
                        "decision it informs -- 'putting it before the checkpoint makes it an "
                        "argument'. But the panel maps the hand-off's operation to the rendered "
                        "statement, and the statement reads the checkpoint's decision, so marimo "
                        "hides the panel until the approval is recorded. The evidence for a "
                        "decision arrives after the decision, which is the failure the function "
                        "was written to prevent."
                    ),
                )
            )

    if alignment.head is not None:
        reads_head = any("handin" in _reachable(panel, names, codes) for panel in panels)
        detail = (
            "every scaffolded notebook opens with the fixed head hand-in of "
            "scaffold.HEAD_CELL_NAMES, whose mo.stop halts the whole run until a file is "
            "supplied -- even when, as here, every hand-in in the plan is declared on a stage "
            "and has its own selector further down. The user is asked for a file no step of the "
            f"process names. The harness fed it from the {alignment.head.donor!r} stage's "
            "hand-in so that anything at all could be graded; nothing in the product does that."
        )
        if reads_head:
            detail += (
                " The digest that reaches the reconciliation panel is that duplicate hand-in's, "
                "not the one the stage actually computes on, so the acceptance is recorded "
                "against a file the process never reads."
            )
        defects.append(
            Defect(
                id="duplicate-head-handin",
                summary="the fixed head asks for a hand-in the plan does not declare",
                detail=detail,
                evidence=", ".join(
                    f"{widget} <- {donor}" for widget, donor in alignment.head.pairs
                ),
            )
        )

    defects.extend(_resume_defects(result))
    defects.extend(_confirmation_defects(result, alignment))
    return tuple(defects)


_RESUME_CALL = "previous_handin"
"""What a hand-in cell calls to pick a run up where it was left.

Looked for in the text rather than in the AST, and that is not laziness: it is reached as
``kedge.runs.previous_handin(...)``, so marimo's own analysis records a reference to ``kedge``
and nothing more. The name is stable, it is public API, and a rename that broke this check would
break every scaffolded notebook at the same time.
"""


def _resume_defects(result: ConversionResult) -> tuple[Defect, ...]:
    """The fixed head hand-in is the one hand-in in a runbook that cannot be resumed.

    Every stage hand-in asks ``kedge.runs.previous_handin`` whether this run already took a file
    at that step, and carries on if it did -- which is what makes a two-day process one somebody
    can leave. The head hand-in does not, so reopening the notebook stops in the third cell and
    every step below it, decisions included, waits for a file the run record already holds.

    Found by generating a conversion and reopening it: the ``the_run_resumes_after_the_kernel_dies``
    grader fails on a notebook whose stage hand-ins all resume correctly.
    """
    by_name = dict(zip(result.names, result.codes, strict=True))
    head = by_name.get("handin")
    stages = {
        name: code
        for name, code in by_name.items()
        if name.endswith("_handin") and name != "handin"
    }
    if head is None or not stages:
        return ()
    resuming = [name for name, code in stages.items() if _RESUME_CALL in code]
    if _RESUME_CALL in head or not resuming:
        return ()
    return (
        Defect(
            id="head-handin-does-not-resume",
            summary="the fixed head hand-in is the only one that asks for its file twice",
            detail=(
                "marimo's state dies with the kernel, so a runbook that says 'run this update, "
                "then re-extract tomorrow' has to come back off disk. Every stage hand-in does: "
                f"it calls kedge.runs.{_RESUME_CALL} and carries on with the file this run "
                "already took. The head hand-in has no such call, so reopening the notebook "
                "stops in the third cell and nothing below it -- including decisions already "
                "recorded -- is shown again."
            ),
            evidence=f"{', '.join(sorted(resuming))} resume; handin does not",
        ),
    )


def _confirmation_defects(result: ConversionResult, alignment: Alignment) -> tuple[Defect, ...]:
    """A hand-off that changes data but is declared read-only, and what follows from it.

    Detected from the statement rather than from the eval's expectations, so it holds for any
    plan: a hand-off whose text opens with UPDATE, INSERT, DELETE or MERGE and carries
    ``mutates: false`` has told the scaffolder to emit no confirmation step. Two consequences,
    and the second is the one nobody would predict: with no confirmation there is no token, and
    the next stage's selector has nothing to read -- so it constructs widgets, references
    nothing, and marimo renders it from the moment the notebook opens, inviting a re-extract
    taken before the statement was ever run.
    """
    defects: list[Defect] = []
    for stage in result.plan.stages:
        if not stage.is_handoff:
            continue
        handoff = stage.effective_handoff()
        if not handoff.contradicts_its_own_statement:
            continue
        text = (handoff.statement or handoff.template or "").strip()
        unplayed = ", ".join(alignment.unplayed) or "none"
        defects.append(
            Defect(
                id="mutating-handoff-declared-read-only",
                summary=f"hand-off {stage.id!r} changes data and declares mutates: false",
                detail=(
                    "the notebook is safe: `Handoff.needs_confirmation` answers from the "
                    "statement, so a confirmation cell is scaffolded whatever the flag claims, "
                    "and the selector below it has a token to gate on. What remains is a plan "
                    "that says the opposite of what its statement does -- and the approval card "
                    "renders `mutates`, so a reviewer is told the one stage that touches live "
                    "data changes nothing. Fix the plan; nothing downstream depends on it any "
                    "more."
                ),
                evidence=(
                    f"statement opens {text.split()[0].upper() if text else '(empty)'}; "
                    f"kedge.sql.changes_data says it writes; scripted actions with no widget: "
                    f"{unplayed}"
                ),
            )
        )
    return tuple(defects)
