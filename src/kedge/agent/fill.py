"""Fill the holes a scaffold leaves: ask a model for each unwritten cell body, and gate every one.

The scaffolder writes a notebook whose structure is settled and whose arithmetic is not. Every
stage it cannot translate comes out as a documented passthrough carrying ``TODO(kedge)``, and a
notebook full of them *runs* -- the passthroughs are passthroughs on purpose, so the hand-in
machinery works from the moment the plan is approved. That is also why nothing on screen counts
them. Until this module there was no code in ``src/`` that read the markers back: the loop that
enumerates holes and asks for each one lived in ``evals/harness/cellgen.py`` and shipped nowhere,
so kedge could write a hole and had no way to say it had one.

This is that loop, in the product. Given an approved plan it takes the scaffolder's cells -- built
here, or read back off a notebook that has already been scaffolded -- finds the holes in the
scaffolder's own order, and asks for one body at a time with the analysis, the plan and the
registry of names as it stands *after the holes above it were filled*. Order is not cosmetic: a
hole is filled against the names defined above it, so filling them out of order asks for a
translation of a frame that does not exist yet.

**It belongs to the agent, not to the notebook.** It was written under ``notebook/`` and had to
import ``kedge.agent.context``, ``kedge.agent.prompts`` and ``kedge.agent.validate`` to work,
inverting the documented layering ``analysis/ -> plan/ -> notebook/ -> agent/ -> server/``. There
was no runtime cycle only because ``notebook/__init__.py`` did not import it, and the blast radius
of adding that one line was the whole ``agent`` package rather than one module, because
``agent/__init__.py`` eagerly aggregates ``context``. What this module *is* settles where it goes:
a model-driving loop, owning reply parsing, retry history, a :class:`~kedge.plan.propose.Completer`
and a check that extends the gate. That is ``agent/``'s job. What is genuinely notebook work is in
``notebook/scaffold.py`` -- the ``TODO(kedge)`` seam of :func:`~kedge.notebook.scaffold.holes_in`,
:func:`~kedge.notebook.scaffold.split_hole` and :func:`~kedge.notebook.scaffold.strip_marker` --
and this module calls it rather than carrying a copy. ``scripts/guardrails.py`` now fails if
anything under ``kedge/notebook/`` imports ``kedge.agent`` again.

**The prompt is assembled, never paraphrased**, and it is assembled in
:mod:`kedge.agent.fillprompt` -- which is also what the eval sends, by calling it rather than by
copying it.

**Seven outcomes, not two.** :class:`FillOutcome` has six members and
:attr:`FilledCell.first_time` splits the successful one, because the answers differ. A hole filled
first time, a hole filled after two rejections, a hole the gate refused every time, a hole the
model answered with prose, a hole the endpoint never answered, a hole nobody asked about because
an earlier one killed the endpoint, and a hole nothing *can* be asked about are seven different
things to do next -- and only four of them are the model's judgement. Collapsing a transport
failure into "the model could not write this cell" attributes a dead endpoint to a model, which is
the one mistake a conversion report must not make. It is also why a transport failure abandons the
run by default (see ``stop_on_error``): asking a dead endpoint five more times costs five timeouts
and reports one fact five times.

**Every one of them is in the denominator.** The seventh --
:attr:`FillOutcome.UNFILLABLE` -- exists because it was not. A hole with no placeholder under its
header cannot be put to a model, and it used to be dropped where it was found: not in ``cells``,
not in :attr:`FillReport.holes`, not in :attr:`FillReport.complete`. So a conversion that left a
``TODO(kedge)`` on disk exited 0 saying "nothing is left unwritten" -- the pass nobody earned that
this project names as its most dangerous failure mode, one layer up in ``reconcile``.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from kedge.agent.context import CellFacts, NameRegistry
from kedge.agent.fillprompt import cell_messages
from kedge.agent.validate import (
    MAX_VALIDATION_ATTEMPTS,
    MISSING_NAME_STAGE,
    Policy,
    RoundingContext,
    undefined_name,
    validate_cell,
)
from kedge.errors import NotebookError
from kedge.notebook.codegen import analyse_cells
from kedge.notebook.scaffold import (
    TODO_MARKER,
    ScaffoldCell,
    build_cells,
    is_unwritten,
    split_hole,
    strip_marker,
    sync_notebook,
)
from kedge.plan.propose import CompletionRequest

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from kedge.analysis.model import WorkbookAnalysis
    from kedge.notebook.scaffold import CellSyncer
    from kedge.plan.model import ProcessPlan
    from kedge.plan.propose import Completer

logger = logging.getLogger(__name__)

__all__ = [
    "NO_PLACEHOLDER",
    "TODO_MARKER",
    "FillAttempt",
    "FillOutcome",
    "FillReport",
    "FilledCell",
    "convert_notebook",
    "fill_holes",
]

_FENCE = re.compile(r"```(?:[a-zA-Z0-9_+-]*)\n(.*?)```", re.DOTALL)


# =============================================================================
# WHAT HAPPENED TO EACH HOLE
# =============================================================================


class FillOutcome(StrEnum):
    """How one hole came out. Six states, because they need six different answers.

    Example:
        >>> bool(FillOutcome.FILLED), bool(FillOutcome.REJECTED)
        (True, False)
    """

    FILLED = "filled"
    """The model wrote a body and the validation gate accepted it."""

    REJECTED = "rejected"
    """The model wrote a body every time and the gate refused every one. Its problem."""

    EMPTY = "empty"
    """The model answered with nothing a cell body could be read out of. Also its problem."""

    ERROR = "error"
    """The completer raised. A transport failure, a refused request, an exhausted script -- not
    the model's judgement, and it must never be scored as though it were."""

    SKIPPED = "skipped"
    """Never asked. The run was abandoned after :attr:`ERROR`, so this hole was never put to the
    model at all -- which is a different thing from a hole the model could not write, and reads
    as one only if the two are collapsed."""

    UNFILLABLE = "unfillable"
    """A hole this driver cannot put to a model at all, and which the notebook still owes.

    Today there is one shape: a hand-off whose statement *is* the marker, which
    :func:`~kedge.notebook.scaffold.split_hole` refuses to split because there is no placeholder
    body under the header -- asking a model to replace "the rest of the cell" would take the
    display block with it. It is exactly what a plan declaring a hand-off with no statement
    produces, which is exactly what a model writes when it types ``kind: handoff`` and stops.

    It exists because leaving such a cell out of the report was a **shrinking denominator**. The
    hole was skipped silently, so it was not in ``cells``, not in :attr:`FillReport.holes`, not in
    :attr:`FillReport.unfilled` and not in :attr:`FillReport.complete` -- and a conversion that
    left a ``TODO(kedge)`` on disk exited 0 saying "nothing is left unwritten". Worse where it is
    measured: the model that wrote the statement-less hand-off had that stage removed from its own
    generation denominator and was reported as filling every hole it was given."""

    def __bool__(self) -> bool:
        """True only for :attr:`FILLED`, so an unfilled hole cannot read as a success."""
        return self is FillOutcome.FILLED


@dataclass(frozen=True, slots=True)
class FillAttempt:
    """One request-and-verdict for one hole.

    Kept per attempt rather than summarised, because "passed on the third try after two pandas
    rejections" and "passed first time" are the same score and different conversions.
    """

    number: int
    code: str
    violations: tuple[str, ...] = ()
    stage: str | None = None
    seconds: float = 0.0
    error: str | None = None

    @property
    def accepted(self) -> bool:
        """Whether this attempt is the one that was spliced into the notebook."""
        return self.error is None and not self.violations and bool(self.code.strip())

    def render(self) -> str:
        """One line for the terminal, naming the verdict and what caused it."""
        if self.error is not None:
            return f"    attempt {self.number}: the model endpoint failed -- {self.error}"
        if self.accepted:
            return f"    attempt {self.number}: accepted ({self.seconds:.1f}s)"
        if not self.code.strip():
            return f"    attempt {self.number}: empty reply ({self.seconds:.1f}s)"
        head = "; ".join(self.violations[:3])
        return f"    attempt {self.number}: rejected at {self.stage} -- {head}"


@dataclass(frozen=True, slots=True)
class FilledCell:
    """One hole, and everything that happened to it."""

    name: str
    stage_id: str | None
    outcome: FillOutcome
    code: str
    """The cell body as it stands: the model's where one was accepted, the scaffolder's
    placeholder where none was. Never empty -- a hole left as a hole still has to produce a
    notebook that parses, or the failure attributes itself to the wrong place."""
    attempts: tuple[FillAttempt, ...] = ()
    detail: str = ""

    @property
    def tries(self) -> int:
        """How many completions this hole cost."""
        return len(self.attempts)

    @property
    def first_time(self) -> bool:
        """Accepted on the first attempt. The measurement everybody actually wants."""
        return self.outcome is FillOutcome.FILLED and self.tries == 1

    @property
    def after_retries(self) -> bool:
        """Accepted, but not before the gate sent it back at least once."""
        return self.outcome is FillOutcome.FILLED and self.tries > 1

    def render(self) -> str:
        """This hole and every attempt on it, one line each."""
        head = f"  [{self.outcome.value.upper()}] {self.name} ({self.tries} attempt(s))"
        if self.detail:
            head += f" -- {self.detail}"
        return "\n".join([head, *(attempt.render() for attempt in self.attempts)])


@dataclass(frozen=True, slots=True)
class FillReport:
    """A scaffold with its holes filled, or not, and the record of the filling.

    ``names`` and ``codes`` are in the scaffolder's own order, which is the order kedge writes the
    notebook in.

    Example:
        A plan is required; ``model_construct`` is used here only because this example needs one
        to exist rather than to say anything, and a real one is a validated document.

        >>> from kedge.plan.model import ProcessPlan
        >>> report = FillReport(names=(), codes=(), cells=(), plan=ProcessPlan.model_construct())
        >>> report.holes, report.complete
        (0, True)
    """

    names: tuple[str, ...]
    codes: tuple[str, ...]
    cells: tuple[FilledCell, ...]
    plan: ProcessPlan
    """The plan this conversion was of. Required, and required on purpose.

    Carried for the same reason :attr:`scaffolded` is: a report that cannot name its own subject
    cannot say which stage a finding is about. What a hand-off declares, and whether the cell that
    came out of it matches, is a question about the plan and the codes together -- and reaching
    for the plan afterwards means the caller holding the report has to have kept it too.

    It is not ``| None``. A default would make every reader of ``report.plan.stages`` decide
    whether to guard, and the honest answer is that there is no report without a plan: every
    construction here has one in hand."""
    scaffolded: tuple[ScaffoldCell, ...] = ()
    """The cells as they stood before filling, roles and stage ids intact. Kept because the role
    of a cell -- head, stage, checkpoint, hand-off, reconciliation -- is what lets a report say
    *which* part of a notebook a finding is about."""
    seconds: float = 0.0
    completions: int = 0
    written: tuple[str, ...] = ()
    """The cells actually spliced back into a notebook, where one was written. Empty for
    :func:`fill_holes`, which touches no file."""
    scaffolded_summary: str = ""
    """What the sync did before the filling started, in its own words.

    Carried rather than logged because a cell the scaffolder could not write is not a hole and
    never reaches the loop below -- so a conversion that reports only on holes reports nothing
    at all about the one part of the notebook it silently failed to produce."""
    refused: tuple[str, ...] = ()
    """Cells the notebook would not accept when the plan was scaffolded into it.

    A refusal is not a hole and never becomes one, which is exactly why it has to be carried
    here. ``kedge_setup`` is refused whenever the notebook already binds ``mo`` -- one unnamed
    cell doing ``import marimo as mo``, the single most likely thing a user types -- and it is
    the cell that imports ``pl``, ``kedge.xl``, ``kedge.sql`` and every path constant below it.
    Every hole can then be filled perfectly and the notebook still not run. :attr:`complete`
    reads this for that reason: "nothing is left unwritten" over a notebook that cannot execute
    is a pass it has not earned."""

    @property
    def holes(self) -> int:
        """How many cells the scaffolder left unwritten. The denominator."""
        return len(self.cells)

    @property
    def filled(self) -> int:
        """Holes the gate accepted a body for."""
        return sum(1 for cell in self.cells if cell.outcome is FillOutcome.FILLED)

    @property
    def first_time(self) -> int:
        """Holes accepted on the first attempt."""
        return sum(1 for cell in self.cells if cell.first_time)

    @property
    def after_retries(self) -> int:
        """Holes accepted only after the gate sent something back."""
        return sum(1 for cell in self.cells if cell.after_retries)

    @property
    def errored(self) -> int:
        """Holes the endpoint never answered. Not the model's judgement."""
        return sum(1 for cell in self.cells if cell.outcome is FillOutcome.ERROR)

    @property
    def skipped(self) -> int:
        """Holes never put to the model, because the run was abandoned."""
        return sum(1 for cell in self.cells if cell.outcome is FillOutcome.SKIPPED)

    @property
    def unmeasured(self) -> bool:
        """Whether **no request to the model was ever answered**. Nothing else.

        The claim is deliberately narrow, because the obvious wider one is false. "Not one hole
        came back with a body the gate could have an opinion about" is what a caller wants to say,
        and it is wrong on a run where the first hole's first attempt was rejected and the second
        attempt was the one the endpoint dropped: that hole ends :attr:`FillOutcome.ERROR`, every
        later one is skipped, and the gate's opinion is sitting in ``cells[0].attempts[0].
        violations`` all the while. Reporting that as "nothing was measured" **exonerates** a model
        whose one answered request breached the house style, which is the wrong direction for a
        mistake of this kind to run in.

        So it asks the question a transport failure actually answers: did anything come back at
        all. An attempt with no ``error`` is a reply, however bad -- an empty one included, since
        "said nothing" is a fact about the model too. This is the sentence a caller may print, and
        the two now say the same thing.

        False when there were no holes at all: that is a statement about the plan, and a caller
        that wants it asks :attr:`holes`.
        """
        answered = any(attempt.error is None for cell in self.cells for attempt in cell.attempts)
        return bool(self.cells) and not answered

    @property
    def unfilled(self) -> tuple[FilledCell, ...]:
        """Every hole still carrying the scaffolder's placeholder."""
        return tuple(cell for cell in self.cells if cell.outcome is not FillOutcome.FILLED)

    @property
    def complete(self) -> bool:
        """Whether the notebook now holds the whole plan. Not whether it converted *well*.

        Two conditions, and the second was missing: every hole filled, **and** no cell the
        scaffolder could not write. A refused cell is not a hole -- it never reaches the loop --
        so ``filled == holes`` was a statement about holes reported as a statement about the
        notebook, and a conversion whose ``kedge_setup`` was refused exited 0 saying "nothing is
        left unwritten" over a file where nothing was imported and every stage referenced a name
        that was never bound. Reconciliation's rule, one layer up: never report a pass that was
        not earned.

        True for a plan with no holes at all, which is honest -- there was nothing to leave
        unwritten -- and is why a caller reporting a conversion checks :attr:`holes` separately. A
        conversion that asked the model nothing has measured nothing.
        """
        return self.filled == self.holes and not self.refused

    def counts(self) -> dict[str, int]:
        """One count per :class:`FillOutcome`, every member present even at zero.

        A missing key is how a category quietly stops being reported.
        """
        tally = {outcome.value: 0 for outcome in FillOutcome}
        for cell in self.cells:
            tally[cell.outcome.value] += 1
        return tally

    def summary_line(self) -> str:
        """The one line a caller prints when it prints nothing else.

        A refusal is named here rather than only in :attr:`scaffolded_summary`, because this is
        the line that gets quoted and that one is the line that gets skipped.
        """
        tally = ", ".join(f"{count} {name}" for name, count in self.counts().items() if count)
        detail = f" ({tally})" if tally else ""
        refused = (
            f"; {len(self.refused)} cell(s) the notebook refused: " + ", ".join(self.refused)
            if self.refused
            else ""
        )
        return (
            f"{self.filled}/{self.holes} hole(s) filled{detail}; "
            f"{self.first_time} first time, {self.after_retries} after retries, "
            f"{self.completions} completion(s) over {self.seconds:.1f}s{refused}"
        )

    def render(self) -> str:
        """The summary and every hole beneath it."""
        return "\n".join([f"conversion: {self.summary_line()}", *(c.render() for c in self.cells)])

    def as_dict(self) -> dict[str, Any]:
        """The machine-readable form, for ``--json`` and for anything logging a conversion."""
        return {
            "holes": self.holes,
            "filled": self.filled,
            "first_time": self.first_time,
            "after_retries": self.after_retries,
            "complete": self.complete,
            "completions": self.completions,
            "seconds": round(self.seconds, 3),
            "counts": self.counts(),
            "written": list(self.written),
            "scaffold_summary": self.scaffolded_summary,
            "refused": list(self.refused),
            "cells": [
                {
                    "name": cell.name,
                    "stage_id": cell.stage_id,
                    "outcome": cell.outcome.value,
                    "attempts": cell.tries,
                    "detail": cell.detail,
                    "violations": [
                        list(attempt.violations) for attempt in cell.attempts if attempt.violations
                    ],
                }
                for cell in self.cells
            ],
        }


# =============================================================================
# READING A REPLY
# =============================================================================


def _body_of(response: str) -> str:
    """The cell body inside a model's reply, fence or no fence.

    The chat never needs this: ``propose_cell`` takes code as a tool argument. Without a tool
    surface the body arrives as prose, and a model that wraps it in ``` and is then recorded as
    having produced nothing would be a driver bug reported as a model failure. Stripping is
    conservative -- the first fenced block if there is one, the whole reply otherwise -- and a
    reply that is only prose still comes back as prose and still fails the syntax stage, which is
    the honest outcome.
    """
    fenced = _FENCE.search(response)
    text = fenced.group(1) if fenced else response
    return text.strip("\n").rstrip()


def _without_echoed_header(body: str) -> str:
    """Drop a comment header the model repeated back, marker and all.

    The instruction says the header is kept for it; some models send it anyway. Prepending a
    second copy leaves a translated cell carrying two copies of the stage documentation, and --
    since :func:`~kedge.notebook.scaffold.strip_marker` only strips the marker kedge itself wrote
    -- an echoed ``TODO(kedge): translate this stage`` above working code, which is the state
    :func:`~kedge.notebook.scaffold.holes_in` reads as an unfinished cell.

    Only a *leading* comment block containing the marker is dropped. A model that opens its
    translation with a comment of its own keeps it, which is what the house style wants.
    """
    lines = body.splitlines()
    end = 0
    while end < len(lines) and (not lines[end].strip() or lines[end].lstrip().startswith("#")):
        end += 1
    if end and any(TODO_MARKER in line for line in lines[:end]):
        return "\n".join(lines[end:]).lstrip("\n")
    return body


_SURVIVING_MARKER_STAGE = "marker"
"""The one check this driver makes that neither the gate nor the kernel does.

See :func:`_surviving_marker`. The other one -- the name the cells below read -- is
:func:`~kedge.agent.validate.undefined_name`, which sits beside the gate it extends because
leaving it here guarantees a third copy the day anything else needs it.
"""


def _surviving_marker(code: str) -> tuple[str, ...]:
    """Refuse an otherwise-acceptable body that leaves the marker in the file.

    The driver's own invariant, and the cheapest one it has: **nothing it writes may read as
    unwritten**. A cell that did was accepted, reported FILLED, and then counted as a hole for
    ever -- and truncated at the marker by the next conversion, with the accepted body written
    over. ``FILL_TASK`` says "marked ``TODO(kedge)``" in as many words, so a model repeating those
    characters back is not a remote possibility.

    Most shapes are caught before this: :func:`_without_echoed_header` drops a header the model
    repeated back, and the seam no longer reads a trailing ``#`` comment mentioning the marker as
    a hole at all. What is left is the marker reaching the file *outside* a Python comment --
    ``alpha = "TODO(kedge): ask Phil which entities"`` -- which is the one shape
    :func:`~kedge.notebook.scaffold.is_unwritten` must keep treating as unwritten, because it is
    how a hand-off with no statement is written.

    Checked on the spliced cell rather than on the reply, because the header kedge keeps is part
    of what lands on disk.

    Args:
        code: The cell as it would be written, header included.

    Returns:
        One violation, or nothing when the marker is gone.
    """
    if not is_unwritten(code):
        return ()
    return (
        f"marker: the cell still carries a '{TODO_MARKER}' marker, which is how kedge counts what "
        f"a notebook has left to write -- a finished cell that keeps one reads as unfinished for "
        f"ever and is truncated at it by the next run. Say what you mean without those characters.",
    )


def _rejection(hole: ScaffoldCell, violations: tuple[str, ...]) -> str:
    """The block returned to the model, matching ``ValidationReport.render``'s shape."""
    return "\n".join(
        [
            f"The cell '{hole.name}' was rejected. Fix the cause and resubmit; you have a limited "
            f"number of attempts.",
            "",
            *(f"  - {message}" for message in violations),
        ]
    )


def _unfilled_detail(attempts: Sequence[FillAttempt], last: FillOutcome) -> str:
    """Why a hole was left unfilled, in terms of what actually happened to it.

    It used to read the last attempt only, so a hole whose first reply was rejected and whose
    second was blank was reported as "no cell body in any of 2 reply(ies)" -- a false statement
    about the run, printed next to a violation list that contradicts it. Both counts are given
    now, and the "any of" claim is made only when it is true.
    """
    total = len(attempts)
    rejected = [attempt for attempt in attempts if attempt.violations]
    blank = [
        attempt
        for attempt in attempts
        if attempt.error is None and not attempt.violations and not attempt.code.strip()
    ]
    if last is FillOutcome.REJECTED:
        return f"the gate rejected {len(rejected)} of {total} attempt(s); last: " + "; ".join(
            attempts[-1].violations[:3]
        )
    if rejected:
        return (
            f"{total} attempt(s): {len(rejected)} rejected by the gate, {len(blank)} holding no "
            f"cell body, the last of them blank; last rejection: "
            + "; ".join(rejected[-1].violations[:3])
        )
    return f"no cell body in any of {total} reply(ies)"


NO_PLACEHOLDER = (
    "no body could be asked for: the cell carries the marker but the scaffolder left no "
    "placeholder under the header to replace, which is what a hand-off declaring no statement "
    "comes out as. Put the statement in the plan and approve it again, or translate this cell by "
    "hand -- either way the notebook still owes this one."
)
"""Why a hole is :attr:`FillOutcome.UNFILLABLE`. Reported, never silently skipped."""


def _holes(cells: Sequence[ScaffoldCell]) -> tuple[tuple[int, ScaffoldCell, str], ...]:
    """Every hole the notebook owes a body for, each with its **position** and its blocker.

    Positions, not names. Keying anything by cell name assumes names are unique and they are not:
    every cell a user creates in marimo is ``def _()``, and the file bridge reports that name as
    ``"_"`` rather than as blank. Two such cells collapsed a ``{name: index}`` map to one entry,
    so the second cell's filled body was written over the first's slot -- a name vanished from the
    registry, and the gate then accepted a later cell redefining it. A multiply-defined breach
    written to the file, past the one check whose whole job is catching it.

    Two cells carrying the marker cannot be put to a model, and they are **not** the same case:

    * **A hole :func:`~kedge.notebook.scaffold.split_hole` will not split** is still a hole. There
      is nothing to ask for -- a hand-off whose statement is itself the marker has no placeholder
      body, and asking a model to replace "the rest of the cell" would take the display block with
      it -- but the notebook owes it, so it comes back with :data:`NO_PLACEHOLDER` and is counted.
      It used to be dropped here, which took it out of the denominator as well as out of the work
      list: see :attr:`FillOutcome.UNFILLABLE`.
    * **A cell with no public name of its own** is not a hole at all, and is the one case this
      still drops. ``_`` and anything underscore-prefixed is cell-local to marimo, so
      :func:`~kedge.agent.validate.validate_cell` can never see it defined and the driver would
      spend every attempt rejecting a body for not defining a name it is not allowed to define --
      then report ``- _: rejected``, which names nothing anybody can act on, and repeat it on
      every later run for ever. The difference that settles it: **kedge emits no unnamed cell**,
      so a marker in one is a note in somebody's own cell rather than work kedge left behind, and
      counting it would peg the exit code at 1 over a file kedge never wrote.

    Args:
        cells: The notebook's cells, in order.

    Returns:
        ``(position, cell, blocker)`` per hole in the scaffolder's own order, the blocker empty
        for a hole a model can be asked to fill.
    """
    holes: list[tuple[int, ScaffoldCell, str]] = []
    for position, cell in enumerate(cells):
        # `holes_in`'s own predicate, applied positionally. Calling it and then looking the
        # results back up by name is what created the collision this function exists to avoid.
        if not is_unwritten(cell.code):
            continue
        if not cell.name or cell.name.startswith("_") or not cell.name.isidentifier():
            logger.info("cell %r carries the marker but has no public name to fill", cell.name)
            continue
        if not split_hole(cell.code)[0]:
            logger.warning(
                "cell %s carries the marker but has no placeholder to replace", cell.name
            )
            holes.append((position, cell, NO_PLACEHOLDER))
            continue
        holes.append((position, cell, ""))
    return tuple(holes)


def _registry_for(names: Sequence[str], codes: Sequence[str]) -> NameRegistry:
    """The name registry for a notebook read as text rather than off a kernel graph.

    Built from marimo's own analysis of every cell, which is why this needs no kernel. It is the
    whole notebook and not "the cells above" on purpose: marimo is a dataflow graph, so a name
    defined below is a name this cell may not redefine, and the single-definition rule the gate
    enforces is about the file rather than about position in it. The registry is rebuilt after
    every hole is filled, so a cell is also told about the names the holes above it just defined.
    """
    analyses = analyse_cells(list(codes))
    return NameRegistry(
        CellFacts(id=str(index), name=name, defs=analysis.defs, refs=analysis.refs)
        for index, (name, analysis) in enumerate(zip(names, analyses, strict=True))
    )


# =============================================================================
# THE LOOP
# =============================================================================


def fill_holes(
    plan: ProcessPlan,
    *,
    completer: Completer,
    analysis: WorkbookAnalysis | None = None,
    cells: Sequence[ScaffoldCell] | None = None,
    model: str = "",
    temperature: float = 0.2,
    max_attempts: int = MAX_VALIDATION_ATTEMPTS,
    policy: Policy | None = None,
    stop_on_error: bool = True,
    workbook_path: Path | None = None,
    handins_dir: Path | None = None,
    contract_path: Path | None = None,
) -> FillReport:
    """Fill every hole in a scaffold, in the scaffolder's order. Touches no file.

    Args:
        plan: An approved plan. The scaffolder refuses an unapproved one and there is no flag here
            to talk it out of that.
        completer: Anything with ``complete(CompletionRequest) -> str``. Indifferent to whether it
            is live, metered or scripted.
        analysis: The workbook analysis, pinned into every request. Strongly recommended: without
            it the model is told in as many words that it is guessing at the workbook's structure,
            and ``.round()`` cannot be checked against Excel's rounding.
        cells: Cells that are already scaffolded -- typically read back off the notebook, so a
            stage somebody has already translated is not a hole and is never asked about again.
            The plan is scaffolded afresh when this is omitted.
        model: The model name put on the request. The completer may override it.
        temperature: Sent on every request, subject to the completer's own negotiation.
        max_attempts: How many times one hole may be put to the model in **total**, the first ask
            included -- not how many retries follow a rejection. Defaults to
            :data:`~kedge.agent.validate.MAX_VALIDATION_ATTEMPTS`. Below 1 is refused rather than
            clamped: it would report every hole as unfilled with no model ever asked.
        policy: What generated code may reach for. Defaults to the shipped default -- no network,
            no database, no writes outside the working directory. Whatever is passed, the model is
            told what it refuses: see :func:`~kedge.agent.fillprompt.policy_rules`.
        stop_on_error: Abandon the run the first time the completer raises, recording every
            remaining hole as :attr:`FillOutcome.SKIPPED`. On by default because a transport
            failure is a fact about the endpoint rather than about the cell: asking it five more
            times costs five timeouts and reports one fact five times. Turn it off to press on.
        workbook_path: Passed to the scaffolder for the reconciliation cell's baseline.
        handins_dir: Passed to the scaffolder. Defaults to the scaffolder's own default.
        contract_path: Passed to the scaffolder. It need not exist.

    Returns:
        The :class:`FillReport`. A hole the model never filled keeps the scaffolder's placeholder
        and is recorded as unfilled -- never dropped, and never quietly counted as written.

    Raises:
        PlanNotApprovedError: when ``cells`` is omitted and the plan is not approved.
    """
    scaffolded = (
        list(cells)
        if cells is not None
        else build_cells(
            plan,
            handins_dir=handins_dir,
            workbook_path=workbook_path,
            contract_path=contract_path,
        )
    )
    if max_attempts < 1:
        msg = (
            f"max_attempts is {max_attempts}; it is the total number of times one hole may be put "
            f"to the model, so it has to be at least 1. Zero would report every hole as having "
            f"produced no cell body without a model ever having been asked."
        )
        raise NotebookError(msg)

    names = [cell.name for cell in scaffolded]
    codes = [cell.code for cell in scaffolded]
    rounding = RoundingContext.from_analysis(analysis)
    resolved_policy = policy or Policy()

    results: list[FilledCell] = []
    started = time.perf_counter()
    completions = 0
    abandoned = False
    for position, hole, blocker in _holes(scaffolded):
        if blocker:
            # Recorded before the abandonment check: whether this hole can be asked about is a
            # fact about the scaffold, not about the endpoint, and it is true either way.
            results.append(
                FilledCell(
                    name=hole.name,
                    stage_id=hole.stage_id,
                    outcome=FillOutcome.UNFILLABLE,
                    code=hole.code,
                    detail=blocker,
                )
            )
            continue
        if abandoned:
            results.append(
                FilledCell(
                    name=hole.name,
                    stage_id=hole.stage_id,
                    outcome=FillOutcome.SKIPPED,
                    code=hole.code,
                    detail="never asked: the run was abandoned after the endpoint failed",
                )
            )
            continue
        registry = _registry_for(names, codes)
        result = _fill_one(
            hole,
            plan=plan,
            analysis=analysis,
            registry=registry,
            completer=completer,
            model=model,
            temperature=temperature,
            max_attempts=max_attempts,
            policy=resolved_policy,
            rounding=rounding,
        )
        results.append(result)
        completions += result.tries
        codes[position] = result.code
        abandoned = stop_on_error and result.outcome is FillOutcome.ERROR

    seconds = time.perf_counter() - started
    report = FillReport(
        names=tuple(names),
        codes=tuple(codes),
        cells=tuple(results),
        scaffolded=tuple(scaffolded),
        seconds=seconds,
        completions=completions,
        plan=plan,
    )
    logger.info(
        "filled %d of %d hole(s) in plan v%d for %s",
        report.filled,
        report.holes,
        plan.version,
        plan.workbook,
    )
    return report


def _fill_one(
    hole: ScaffoldCell,
    *,
    plan: ProcessPlan,
    analysis: WorkbookAnalysis | None,
    registry: NameRegistry,
    completer: Completer,
    model: str,
    temperature: float,
    max_attempts: int,
    policy: Policy,
    rounding: RoundingContext,
) -> FilledCell:
    """Ask for one cell body, gate it, and retry on violations exactly as the chat loop does."""
    header, _placeholder = split_hole(hole.code)
    kept = strip_marker(header)
    history: list[tuple[str, str]] = []
    attempts: list[FillAttempt] = []
    last = FillOutcome.EMPTY

    for number in range(1, max(1, max_attempts) + 1):
        request = CompletionRequest(
            messages=cell_messages(
                cell=hole, plan=plan, analysis=analysis, registry=registry, history=history
            ),
            model=model,
            json_schema=None,
            temperature=temperature,
        )
        began = time.perf_counter()
        try:
            response = completer.complete(request)
        # Broad on purpose. Anything the completer raises -- a transport failure, an SDK error, a
        # scripted stand-in running out of answers -- is a fact about the endpoint, and the whole
        # argument for ERROR as its own outcome is that it must be attributed rather than
        # re-raised into the caller's lap as though the cell were at fault.
        except Exception as exc:
            elapsed = time.perf_counter() - began
            detail = f"{type(exc).__name__}: {exc}"
            attempts.append(FillAttempt(number=number, code="", seconds=elapsed, error=detail))
            logger.warning("the model endpoint failed on cell %s: %r", hole.name, exc)
            return FilledCell(
                name=hole.name,
                stage_id=hole.stage_id,
                outcome=FillOutcome.ERROR,
                code=hole.code,
                attempts=tuple(attempts),
                detail=detail,
            )
        elapsed = time.perf_counter() - began

        body = _without_echoed_header(_body_of(response))
        if not body.strip():
            attempts.append(FillAttempt(number=number, code="", seconds=elapsed))
            last = FillOutcome.EMPTY
            history.extend(
                [
                    ("assistant", response),
                    (
                        "user",
                        "That reply held no cell body. Reply with Python only -- the lines that "
                        "replace the placeholder, at zero indentation, no fence and no prose.",
                    ),
                ]
            )
            continue

        code = f"{kept}\n{body}" if kept else body
        report = validate_cell(
            code,
            registry=registry,
            cell=hole.name,
            policy=policy,
            rounding=rounding,
            frame_names=registry.frame_names(),
        )
        missing = undefined_name(report, hole.name)
        surviving = _surviving_marker(code)
        violations = report.messages or missing or surviving
        if report.stage is not None:
            stage: str | None = report.stage.value
        elif missing:
            stage = MISSING_NAME_STAGE
        elif surviving:
            stage = _SURVIVING_MARKER_STAGE
        else:
            stage = None
        attempts.append(
            FillAttempt(
                number=number,
                code=code,
                violations=violations,
                stage=stage,
                seconds=elapsed,
            )
        )
        if not violations:
            return FilledCell(
                name=hole.name,
                stage_id=hole.stage_id,
                outcome=FillOutcome.FILLED,
                code=code,
                attempts=tuple(attempts),
            )
        last = FillOutcome.REJECTED
        verdict = report.render() if report.messages else _rejection(hole, violations)
        history.extend([("assistant", body), ("user", verdict)])

    detail = _unfilled_detail(attempts, last)
    logger.info("cell %s left unfilled: %s", hole.name, detail)
    return FilledCell(
        name=hole.name,
        stage_id=hole.stage_id,
        outcome=last,
        code=hole.code,
        attempts=tuple(attempts),
        detail=detail,
    )


# =============================================================================
# THE WHOLE CONVERSION, ONTO A NOTEBOOK FILE
# =============================================================================


async def convert_notebook(
    plan: ProcessPlan,
    driver: CellSyncer,
    *,
    completer: Completer,
    analysis: WorkbookAnalysis | None = None,
    model: str = "",
    temperature: float = 0.2,
    max_attempts: int = MAX_VALIDATION_ATTEMPTS,
    policy: Policy | None = None,
    stop_on_error: bool = True,
    sync: bool = True,
    workbook_path: Path | None = None,
    handins_dir: Path | None = None,
    contract_path: Path | None = None,
) -> FillReport:
    """Bring a notebook into line with an approved plan, then fill what the scaffold left.

    Three steps, and the order matters. :func:`~kedge.notebook.scaffold.sync_notebook` first, so a
    plan approved since the last open lands as the cells it was missing and every hole the
    conversion is about actually exists. Then the notebook is **read back**, and the holes are
    found in what is on disk rather than in a fresh scaffold -- so a stage somebody has already
    translated carries no marker, is not a hole, and is never asked about or overwritten. Then each
    accepted body is spliced into its own cell.

    Nothing but a hole is ever written. ``sync_notebook`` refuses to overwrite a cell that differs
    from what the plan says now and from what it said before, and the splice below only touches
    cells this run filled.

    Args:
        plan: The approved plan.
        driver: A notebook driver -- :class:`~kedge.notebook.filedriver.FileNotebookDriver` for
            the headless path, which needs no kernel and no marimo process.
        completer: The model seam. See :func:`fill_holes`.
        analysis: The workbook analysis, pinned into every request.
        model: The model name put on each request.
        temperature: Sent on every request.
        max_attempts: Total attempts per hole, the first ask included. See :func:`fill_holes`.
        policy: What generated code may reach for.
        stop_on_error: Abandon the run the first time the endpoint fails. See :func:`fill_holes`.
        sync: Scaffold the plan into the notebook first. Off leaves the file exactly as it is and
            fills only the holes already in it.
        workbook_path: The workbook whose cached values are the reconciliation baseline.
        handins_dir: Where hand-ins are persisted.
        contract_path: Where the hand-in contract lives.

    Returns:
        The :class:`FillReport`, with :attr:`FillReport.written` naming the cells spliced back in
        and :attr:`FillReport.refused` naming any the notebook would not accept at all.

    Raises:
        PlanNotApprovedError: when the plan is not approved.
    """
    summary = ""
    refused: tuple[str, ...] = ()
    if sync:
        result = await sync_notebook(
            plan,
            driver,
            handins_dir=handins_dir,
            workbook_path=workbook_path,
            contract_path=contract_path,
        )
        summary = result.summary(plan.version)
        # A refusal is carried out of here rather than logged and forgotten. It is not a hole and
        # never becomes one, so nothing the loop below reports can mention it -- and the cell most
        # likely to be refused is the one that imports everything the rest of the notebook uses.
        refused = tuple(cell.name for cell in result.cells if cell.outcome == "refused")
        if refused:
            logger.warning(
                "the notebook refused %d cell(s) of plan v%d: %s",
                len(refused),
                plan.version,
                ", ".join(refused),
            )
        logger.info("scaffolded before filling: %s", summary)

    # `with_code=True` deliberately: the bodies are what a hole is found in, and reading them
    # records the read that marimo's staleness guard wants before the splices below.
    listed = await driver.list_cells(with_code=True)
    # Not `allow_unapproved`: this is also the guard that keeps `sync=False` from converting a
    # plan nobody approved, which is the one thing every path into the notebook refuses.
    roles = {
        cell.name: cell
        for cell in build_cells(
            plan,
            handins_dir=handins_dir,
            workbook_path=workbook_path,
            contract_path=contract_path,
        )
    }
    existing = [
        ScaffoldCell(
            name=cell.name,
            code=cell.code or "",
            role=roles[cell.name].role if cell.name in roles else "stage",
            stage_id=roles[cell.name].stage_id if cell.name in roles else None,
        )
        for cell in listed
        if cell.name
    ]

    report = fill_holes(
        plan,
        completer=completer,
        analysis=analysis,
        cells=existing,
        model=model,
        temperature=temperature,
        max_attempts=max_attempts,
        policy=policy,
        stop_on_error=stop_on_error,
    )

    written: list[str] = []
    for cell in report.cells:
        if cell.outcome is not FillOutcome.FILLED:
            continue
        await driver.edit_cell(cell.name, cell.code, run=False)
        written.append(cell.name)
    logger.info("wrote %d filled cell(s) to the notebook", len(written))
    # `replace` rather than ten fields copied across by hand: a field added to `FillReport`
    # tomorrow arrives here without anybody remembering that this constructor exists. The
    # hand-copied version had already forgotten one.
    return replace(report, written=tuple(written), scaffolded_summary=summary, refused=refused)
