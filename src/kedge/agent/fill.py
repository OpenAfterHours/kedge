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
``agent/__init__.py`` eagerly aggregates ``context``. What this **seam** *is* settles where it
goes: a model-driving loop, owning reply parsing, retry history, a
:class:`~kedge.plan.propose.Completer` and a check that extends the gate. Since the split those
four sit in :mod:`kedge.agent.fillhole` rather than in this file, which changes nothing about the
argument -- the seam is four modules and every one of them is agent work. What is notebook work is in
``notebook/scaffold.py`` -- the ``TODO(kedge)`` seam of :func:`~kedge.notebook.scaffold.holes_in`,
:func:`~kedge.notebook.scaffold.split_hole` and :func:`~kedge.notebook.scaffold.strip_marker` --
and this module calls it rather than carrying a copy. ``scripts/guardrails.py`` now fails if
anything under ``kedge/notebook/`` imports ``kedge.agent`` again.

## What is here, and what is next door

This module is the **run**: it drives the holes :func:`~kedge.agent.fillhole.holes_to_fill` finds,
keeps the name registry current between them, decides what a dead endpoint costs the rest of the
run, accumulates the report -- and, in :func:`convert_notebook`, puts an accepted body into a
file. Three modules beside it hold the jobs that change for their own reasons, and each carries
its own argument rather than a share of this one:

* :mod:`kedge.agent.fillreport` -- what happened to each hole and how it reads. Pure data and
  rendering. Why there are seven outcomes rather than two, and why every one of them is in the
  denominator, is argued there, beside the enum it is about.
* :mod:`kedge.agent.fillprompt` -- **every word this seam says to a model**, the rejection block
  and the empty-reply nudge included. It is assembled, never paraphrased, and it is what the eval
  sends too, by calling it rather than by copying it.
* :mod:`kedge.agent.fillhole` -- one hole: which cells are holes and in what order, what came back
  from the model, and the check the driver makes that the gate does not.

:func:`convert_notebook` sits here beside :func:`fill_holes`, and it could perfectly well have a
module of its own -- it is the only ``async def`` in the seam, the only thing that takes a
:class:`~kedge.notebook.scaffold.CellSyncer`, and the only thing that writes. What keeps the two
together is that they are one job at two altitudes: fill an approved plan's holes, in memory or
onto a file. They share ten parameters, every one of them documented twice, the second time as
"see :func:`fill_holes`". A parameter added to one and forgotten in the other is a thing a reader
notices while they are adjacent and does not while they are a file apart.

If that stops being true, the module to make is ``fillconvert`` and its import arrow points **at**
this one: it imports :func:`fill_holes` from here, and ``cli.py`` and ``agent/__init__.py`` take
:func:`convert_notebook` from it. Nothing here may import it back. A re-export from this module is
the one arrangement that does not work, because that is a cycle inside ``kedge.agent`` -- which is
the failure this seam was moved out of ``notebook/`` to avoid, three paragraphs up.
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from typing import TYPE_CHECKING

from kedge.agent.fillhole import NO_PLACEHOLDER, fill_hole, holes_to_fill, registry_for
from kedge.agent.fillreport import FillAttempt, FilledCell, FillOutcome, FillReport
from kedge.agent.validate import MAX_VALIDATION_ATTEMPTS, Policy, RoundingContext
from kedge.errors import NotebookError
from kedge.notebook.scaffold import TODO_MARKER, ScaffoldCell, build_cells, sync_notebook

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


# =============================================================================
# THE RUN
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
            remaining hole as :attr:`~kedge.agent.fillreport.FillOutcome.SKIPPED`. On by default
            because a transport failure is a fact about the endpoint rather than about the cell:
            asking it five more times costs five timeouts and reports one fact five times. Turn it
            off to press on.
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
    for position, hole, blocker in holes_to_fill(scaffolded):
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
        registry = registry_for(names, codes)
        result = fill_hole(
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
        # `position` indexes `scaffolded`, and `codes` is built from `scaffolded` in one pass
        # above -- same length, same order, so the two indices are the same index. That is the
        # whole contract `holes_to_fill` returns positions for rather than names, and it now
        # crosses a module boundary with nothing but this comment and
        # `test_two_unnamed_cells_do_not_overwrite_one_anothers_bodies` holding it. Anything that
        # filters, sorts or re-derives either list breaks it silently: two cells named `_` is all
        # it takes, and the symptom is one cell's body written into another's slot.
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


# ===========================================================================
# THE WHOLE CONVERSION, ONTO A NOTEBOOK FILE
# ===========================================================================


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
