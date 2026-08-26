"""One hole: what counts as one, what came back from the model, and what the driver makes of it.

The mechanics of filling, with none of the bookkeeping. :func:`~kedge.agent.fill.fill_holes` owns
the *run* -- which scaffold, in what order, and whether to carry on after a dead endpoint -- and
hands one cell at a time to :func:`fill_hole`, which asks, gates, retries and comes back with a
:class:`~kedge.agent.fillreport.FilledCell`. They change for different reasons. The order holes are
filled in, and what a transport failure costs the rest of the run, are decisions about a
conversion; how many times one cell may be put to a model, and what is checked beyond the gate,
are decisions about an attempt.

**Two checks live outside the gate, and only one of them lives here.**
:func:`_surviving_marker` is this module's, because it is about kedge's own ``TODO(kedge)`` seam
rather than about generated code. The other is
:func:`~kedge.agent.validate.undefined_name`, beside the gate it extends. See both docstrings.

**Reading a reply is this seam's own problem.** The chat never has it: a cell body reaches
``propose_cell`` as a tool argument. Without a tool surface the body arrives as prose, fenced as
often as not, sometimes carrying back the comment header the prompt said would be kept for it --
so :func:`body_of` and :func:`_without_echoed_header` sit between the endpoint and the gate. What
is *said* to the model, the retries included, is :mod:`kedge.agent.fillprompt`'s; this module only
reads what comes back.
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING

from kedge.agent.context import CellFacts, NameRegistry
from kedge.agent.fillprompt import EMPTY_REPLY_NUDGE, cell_messages, rejection_block
from kedge.agent.fillreport import FillAttempt, FilledCell, FillOutcome, unfilled_detail
from kedge.agent.validate import MISSING_NAME_STAGE, undefined_name, validate_cell
from kedge.notebook.codegen import analyse_cells
from kedge.notebook.scaffold import TODO_MARKER, is_unwritten, split_hole, strip_marker
from kedge.plan.propose import CompletionRequest

if TYPE_CHECKING:
    from collections.abc import Sequence

    from kedge.agent.validate import Policy, RoundingContext
    from kedge.analysis.model import WorkbookAnalysis
    from kedge.notebook.scaffold import ScaffoldCell
    from kedge.plan.model import ProcessPlan
    from kedge.plan.propose import Completer

logger = logging.getLogger(__name__)

__all__ = [
    "NO_PLACEHOLDER",
    "body_of",
    "fill_hole",
    "holes_to_fill",
    "registry_for",
]
# Five names, and the three private ones are the point: `_surviving_marker`,
# `_SURVIVING_MARKER_STAGE` and `_without_echoed_header` are called from nowhere but this file,
# and splitting a module is no reason to promise them to anybody. `_SURVIVING_MARKER_STAGE` costs
# most: it is a validation-stage label, and what keeps `kedge.agent.validate`'s argument honest --
# one gate, no third copy of a stage name -- is that this second source of one stays private.

_FENCE = re.compile(r"```(?:[a-zA-Z0-9_+-]*)\n(.*?)```", re.DOTALL)


# =============================================================================
# READING A REPLY
# =============================================================================


def body_of(response: str) -> str:
    """The cell body inside a model's reply, fence or no fence.

    The chat never needs this: ``propose_cell`` takes code as a tool argument. Without a tool
    surface the body arrives as prose, and a model that wraps it in ``` and is then recorded as
    having produced nothing would be a driver bug reported as a model failure. Stripping is
    conservative -- the first fenced block if there is one, the whole reply otherwise -- and a
    reply that is only prose still comes back as prose and still fails the syntax stage, which is
    the honest outcome.

    Args:
        response: The model's reply, exactly as the completer returned it.

    Returns:
        The body, fence and surrounding prose removed. Empty where the reply held nothing, which
        the caller reads as :attr:`~kedge.agent.fillreport.FillOutcome.EMPTY`.
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


# ===========================================================================
# THE CHECK THE GATE DOES NOT MAKE
# ===========================================================================


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


# ===========================================================================
# WHAT COUNTS AS A HOLE
# ===========================================================================


NO_PLACEHOLDER = (
    "no body could be asked for: the cell carries the marker but the scaffolder left no "
    "placeholder under the header to replace, which is what a hand-off declaring no statement "
    "comes out as. Put the statement in the plan and approve it again, or translate this cell by "
    "hand -- either way the notebook still owes this one."
)
"""Why a hole is :attr:`~kedge.agent.fillreport.FillOutcome.UNFILLABLE`. Reported, never silently
skipped."""


def holes_to_fill(cells: Sequence[ScaffoldCell]) -> tuple[tuple[int, ScaffoldCell, str], ...]:
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
      list: see :attr:`~kedge.agent.fillreport.FillOutcome.UNFILLABLE`.
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


def registry_for(names: Sequence[str], codes: Sequence[str]) -> NameRegistry:
    """The name registry for a notebook read as text rather than off a kernel graph.

    Built from marimo's own analysis of every cell, which is why this needs no kernel. It is the
    whole notebook and not "the cells above" on purpose: marimo is a dataflow graph, so a name
    defined below is a name this cell may not redefine, and the single-definition rule the gate
    enforces is about the file rather than about position in it. The registry is rebuilt after
    every hole is filled, so a cell is also told about the names the holes above it just defined.

    Args:
        names: Every cell's name, in file order.
        codes: Every cell's body, same order and same length. Zipped strictly, so a caller that
            has let the two drift apart is told here rather than registering a name against
            another cell's definitions.

    Returns:
        The registry, covering the whole notebook rather than the cells above any one hole.

    Raises:
        ValueError: when ``names`` and ``codes`` are of different lengths.
    """
    analyses = analyse_cells(list(codes))
    return NameRegistry(
        CellFacts(id=str(index), name=name, defs=analysis.defs, refs=analysis.refs)
        for index, (name, analysis) in enumerate(zip(names, analyses, strict=True))
    )


# ===========================================================================
# ONE HOLE, ASKED AND GATED
# ===========================================================================


def fill_hole(
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
    """Ask for one cell body, gate it, and retry on violations exactly as the chat loop does.

    One hole, from request to verdict. Every attempt is kept -- see
    :class:`~kedge.agent.fillreport.FillAttempt` -- because "passed on the third try after two
    pandas rejections" and "passed first time" are the same score and different conversions.

    Args:
        hole: The scaffolded cell, comment header and placeholder together. The header is kept
            and the model is asked only for what replaces the placeholder.
        plan: The approved plan, rendered into the request as standing instructions.
        analysis: The workbook analysis, pinned into every request. ``None`` renders the block
            that says so, and means ``.round()`` cannot be checked against Excel's rounding.
        registry: Every public name the notebook owns, the holes filled above this one included.
            Rebuilt by the caller between holes; this function never mutates it.
        completer: Anything with ``complete(CompletionRequest) -> str``.
        model: The model name put on each request. The completer may override it.
        temperature: Sent on every request, subject to the completer's own negotiation.
        max_attempts: How many times this hole may be put to the model in **total**, the first ask
            included. Below 1 is clamped to 1 here; :func:`~kedge.agent.fill.fill_holes` refuses it
            outright, which is where a caller meets the error.
        policy: What generated code may reach for. Resolved by the caller, never ``None`` here.
        rounding: The Excel rounding context the gate checks ``.round()`` against.

    Returns:
        The :class:`~kedge.agent.fillreport.FilledCell`. ``code`` holds the accepted body where
        there was one and the scaffolder's placeholder where there was not, so a hole left as a
        hole still produces a notebook that parses. A completer that raises comes back as
        :attr:`~kedge.agent.fillreport.FillOutcome.ERROR` rather than propagating; why that is an
        outcome and not an exception is argued beside the enum.
    """
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

        body = _without_echoed_header(body_of(response))
        if not body.strip():
            attempts.append(FillAttempt(number=number, code="", seconds=elapsed))
            last = FillOutcome.EMPTY
            history.extend([("assistant", response), ("user", EMPTY_REPLY_NUDGE)])
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
        verdict = report.render() if report.messages else rejection_block(hole, violations)
        history.extend([("assistant", body), ("user", verdict)])

    detail = unfilled_detail(attempts, last)
    logger.info("cell %s left unfilled: %s", hole.name, detail)
    return FilledCell(
        name=hole.name,
        stage_id=hole.stage_id,
        outcome=last,
        code=hole.code,
        attempts=tuple(attempts),
        detail=detail,
    )
