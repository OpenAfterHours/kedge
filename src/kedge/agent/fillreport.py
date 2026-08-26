"""What a conversion did to each hole, and how it reads. The vocabulary, and nothing else.

Pure data and rendering: nothing here reaches a model, opens a notebook or touches a file. That is
why it is a module rather than the top third of :mod:`kedge.agent.fill`. A count that appears in
``kedge convert --json``, a line the terminal prints, and the words a sweep grades a model by are
one job with one reason to change, and it is not the reason a driving loop changes. The eval reads
these four types under its own names -- see ``evals/harness/cellgen.py`` -- and reads nothing else
off a conversion.

**Seven outcomes, not two.** :class:`FillOutcome` has six members and
:attr:`FilledCell.first_time` splits the successful one, because the answers differ. A hole filled
first time, a hole filled after two rejections, a hole the gate refused every time, a hole the
model answered with prose, a hole the endpoint never answered, a hole nobody asked about because
an earlier one killed the endpoint, and a hole nothing *can* be asked about are seven different
things to do next -- and only four of them are the model's judgement. Collapsing a transport
failure into "the model could not write this cell" attributes a dead endpoint to a model, which is
the one mistake a conversion report must not make. It is also why a transport failure abandons the
run by default (:func:`~kedge.agent.fill.fill_holes`'s ``stop_on_error``): asking a dead endpoint
five more times costs five timeouts and reports one fact five times.

**Every one of them is in the denominator.** The seventh -- :attr:`FillOutcome.UNFILLABLE` --
exists because it was not. A hole with no placeholder under its header cannot be put to a model,
and it used to be dropped where it was found: not in ``cells``, not in :attr:`FillReport.holes`,
not in :attr:`FillReport.complete`. So a conversion that left a ``TODO(kedge)`` on disk exited 0
saying "nothing is left unwritten" -- the pass nobody earned that this project names as its most
dangerous failure mode, one layer up in ``reconcile``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

    from kedge.notebook.scaffold import ScaffoldCell
    from kedge.plan.model import ProcessPlan

__all__ = [
    "FillAttempt",
    "FillOutcome",
    "FillReport",
    "FilledCell",
    "unfilled_detail",
]


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
    :func:`~kedge.agent.fill.fill_holes`, which touches no file."""
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


def unfilled_detail(attempts: Sequence[FillAttempt], last: FillOutcome) -> str:
    """Why a hole was left unfilled, in terms of what actually happened to it.

    It used to read the last attempt only, so a hole whose first reply was rejected and whose
    second was blank was reported as "no cell body in any of 2 reply(ies)" -- a false statement
    about the run, printed next to a violation list that contradicts it. Both counts are given
    now, and the "any of" claim is made only when it is true.

    Args:
        attempts: Every attempt on this hole, in order. Never empty here: a hole with no attempts
            was never asked about, which is :attr:`FillOutcome.SKIPPED` or
            :attr:`FillOutcome.UNFILLABLE` and carries its own reason.
        last: The outcome the attempt loop settled on, which decides which sentence is the true
            one.

    Returns:
        The prose for :attr:`FilledCell.detail`, counting rather than asserting a single cause.
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
