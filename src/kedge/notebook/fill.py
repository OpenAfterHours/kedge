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

**The prompt is assembled, never paraphrased.** :data:`FILL_PROMPT_PARTS` is
:data:`~kedge.agent.prompts.SYSTEM_PARTS` minus ``tools.md``, subtracted by name so a part added
to the product prompt tomorrow is in this seam tomorrow without anybody remembering a second list
exists. The subtraction is deliberate and it is the one place this module departs from the
proposal that asked for it, which argued that "in the product the subtraction is unnecessary --
the tools are real". They are real in the *chat* seam. They are not real here: ``kedge convert``
is headless, writes through :class:`~kedge.notebook.filedriver.FileNotebookDriver`, and offers no
``list_cells``, no ``probe`` and no ``propose_cell``. A model told to call a tool that does not
exist answers with a tool call in prose, the reply holds no cell body, and a *harness* mismatch is
recorded as a model failure. So ``tools.md`` goes, exactly as it does in the eval, and for exactly
the same reason.

**Dropping the file drops the rules with it, so one section is carried across.** ``tools.md``'s
``## Validation`` heading is where the product states what :class:`~kedge.agent.validate.Policy`
refuses -- shell, network, database connections, writes outside the working directory, credentials
in literals -- and this seam still runs the real ``Policy``. :func:`policy_rules` quotes that
section out of the shipped file at load time rather than restating it, because a paraphrase is a
second copy of a rule and a second copy rots. It raises rather than returning nothing if the
heading ever moves: silently sending no rules is the exact failure it exists to prevent.

**Six outcomes, not two.** :class:`FillOutcome` has five members and :attr:`FilledCell.first_time`
splits the successful one, because the answers differ. A hole nobody asked about, a hole filled
first time, a hole filled after two rejections, a hole the gate refused every time, a hole the
model answered with prose, and a hole the endpoint never answered at all are six different things
to do next -- and the last of them is not the model's judgement. Collapsing a transport failure
into "the model could not write this cell" attributes a dead endpoint to a model, which is the one
mistake a conversion report must not make. It is also why a transport failure abandons the run
(see ``stop_on_error``): asking a dead endpoint five more times costs five timeouts and reports
one fact five times.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from kedge.agent.context import (
    CellFacts,
    NameRegistry,
    build_analysis_block,
    build_plan_block,
)
from kedge.agent.prompts import SYSTEM_PARTS, build_system_prompt, load_prompt
from kedge.agent.validate import (
    MAX_VALIDATION_ATTEMPTS,
    Policy,
    RoundingContext,
    ValidationReport,
    validate_cell,
)
from kedge.notebook.codegen import analyse_cells
from kedge.notebook.scaffold import (
    TODO_MARKER,
    ScaffoldCell,
    build_cells,
    holes_in,
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
    "FILL_PROMPT_PARTS",
    "FILL_TASK",
    "POLICY_SOURCE",
    "TODO_MARKER",
    "FillAttempt",
    "FillOutcome",
    "FillReport",
    "FilledCell",
    "cell_messages",
    "convert_notebook",
    "fill_holes",
    "policy_rules",
    "system_prompt",
]

_FENCE = re.compile(r"```(?:[a-zA-Z0-9_+-]*)\n(.*?)```", re.DOTALL)


# =============================================================================
# THE PROMPT
# =============================================================================


FILL_PROMPT_PARTS: tuple[str, ...] = tuple(part for part in SYSTEM_PARTS if part != "tools.md")
"""The product's system prompt parts, less the one describing tools this seam does not offer.

Subtracted rather than enumerated. A new part added to
:data:`~kedge.agent.prompts.SYSTEM_PARTS` -- another file of Excel semantics, say -- reaches this
seam the moment it ships. Listing the five wanted files here would have made this the place a
conversion silently stopped tracking the chat prompt.
"""

POLICY_SOURCE = ("tools.md", "## Validation")
"""Where the policy rules are quoted from: the shipped file, and the heading inside it."""


def policy_rules(source: tuple[str, str] = POLICY_SOURCE) -> str:
    """The shipped ``## Validation`` section, quoted verbatim out of ``tools.md``.

    Dropping ``tools.md`` from the prompt drops the only statement of what
    :class:`~kedge.agent.validate.Policy` refuses, while the gate itself stays. A model that
    reaches for ``duckdb.connect(...)`` is warned in the chat and ambushed here, so a conversion
    would refuse cells the chat would have talked the model out of writing -- and the fix must not
    be a paraphrase, because a paraphrase is a second copy of a rule that drifts out of step with
    the first and nobody notices until a cell is blamed for it.

    Args:
        source: The prompt file, and the Markdown heading whose section to quote.

    Returns:
        The section, heading line included, exactly as the shipped file holds it.

    Raises:
        LookupError: when the heading is not in the file. A prompt assembled without the rules
            would run and would refuse cells it never warned about; refusing to build one is the
            whole point of checking.
    """
    name, heading = source
    text = load_prompt(name)
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip() == heading), None)
    if start is None:
        msg = (
            f"the prompt part {name!r} no longer has a {heading!r} section, so a conversion cannot "
            f"tell a model what the validation gate refuses. Point POLICY_SOURCE at wherever those "
            f"rules live now -- do not restate them here, or `kedge convert` will start rejecting "
            f"cells it never warned about."
        )
        raise LookupError(msg)
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].startswith(("## ", "# "))),
        len(lines),
    )
    return "\n".join(lines[start:end]).rstrip()


FILL_TASK = """
# This turn: one cell body, and nothing else

This is not the chat pane and you have no tools. There is no `list_cells`, no `probe`, no
`propose_cell`; there is one cell, and your reply *is* its body. Everything you are given about
the workbook and the plan is below -- there is no way to ask for more, so work from it and say
what you assumed rather than guessing quietly.

The notebook has already been scaffolded from the approved plan. Its structure is settled: the
cells exist, they are named, and the ones above this one have been written. What is left is the
translation inside this cell, marked `TODO(kedge)`.

Reply with **Python only**: the lines that replace the placeholder body, at zero indentation, no
Markdown fence, no explanation before or after. The comment header above the `TODO(kedge)` line
is kept for you -- do not repeat it. The cell must define the name the placeholder defines,
because the cells below read it; give anything else you need a leading underscore, which marimo
treats as cell-local.

Your reply goes through kedge's validation gate before it is accepted. If it is rejected you get
the violations back and a limited number of further attempts -- read them and fix the cause,
because a cell that never passes is a hole in the notebook rather than a cell that came out
badly. The gate's rules are below, quoted from the file the chat sends: the tool names in them do
not apply here, but every rule does, because it is the same gate.
""".strip()
"""The one block this seam adds, appended through ``build_system_prompt(extra=...)``.

It states the mechanics of the seam and deliberately nothing else. Every rule about *how to
translate* -- polars, LazyFrames, ``kedge.xl``, ``kedge.sql``, the single-definition rule -- is in
the shipped parts above it, and restating one here would create a second copy to keep in step with
the first. It ends up out of step; they always do. The rules of the gate itself are not restated
either: :func:`policy_rules` quotes them.
"""


def system_prompt(*, parts: Sequence[str] = FILL_PROMPT_PARTS, extra: Sequence[str] = ()) -> str:
    """The system prompt for the cell-filling seam.

    Args:
        parts: Which shipped prompt files to send. Defaults to :data:`FILL_PROMPT_PARTS`.
        extra: Blocks appended after :data:`FILL_TASK` and the quoted policy rules, for a caller
            with something further to say.

    Returns:
        The assembled prompt, built by the product's own
        :func:`~kedge.agent.prompts.build_system_prompt`.

    Raises:
        LookupError: when the policy rules can no longer be quoted. See :func:`policy_rules`.
    """
    return build_system_prompt(parts=parts, extra=(FILL_TASK, policy_rules(), *extra))


def cell_messages(
    *,
    cell: ScaffoldCell,
    plan: ProcessPlan,
    analysis: WorkbookAnalysis | None,
    registry: NameRegistry,
    history: Sequence[tuple[str, str]] = (),
) -> list[dict[str, str]]:
    """The messages for one hole, in the roles the chat loop puts them in.

    One system message carrying the prompt and the pinned blocks together, then a user message
    carrying the cell, then any retry traffic. That is what
    :meth:`kedge.agent.context.ConversationWindow._render` does -- it joins the system prompt and
    every pinned block into a single ``{"role": "system"}`` entry before any conversation message
    -- and splitting them here would put some eight thousand tokens in a role the chat never puts
    them in.

    The pinned blocks are in :meth:`kedge.agent.loop.KedgeAgent._window_for`'s own
    least-volatile-first order: the analysis, then the plan, then the registry. A prompt cache keys
    on the prefix, so anything ahead of a block that changes stays cached; the registry is the one
    that changes between holes, and it goes last. The one pinned block left out is
    :meth:`~kedge.agent.context.NotebookState.render`, which instructs the reader to call
    ``list_cells`` and exists to describe a live kernel's staleness -- neither of which is a thing
    that can happen here.

    Args:
        cell: The scaffolded cell, comment header and placeholder body together. Sent whole: the
            intent, the sources, the assumptions and the Excel-pattern hint are the brief.
        plan: The approved plan, rendered as the model's standing instructions.
        analysis: The workbook analysis. ``None`` renders the block that says so, which is what
            the chat does too.
        registry: Every public name the notebook already owns, including the holes filled above
            this one.
        history: Prior ``(role, content)`` pairs for this cell -- the rejected body and the gate's
            verdict on it.

    Returns:
        The message list, ready for a :class:`~kedge.plan.propose.CompletionRequest`.
    """
    head = "\n\n".join(
        [
            system_prompt(),
            build_analysis_block(analysis),
            build_plan_block(plan),
            registry.render(),
        ]
    )
    task = (
        f"## The cell to write: `{cell.name}`\n\n"
        f"This is the scaffolded cell as it stands. Everything above the "
        f"`{TODO_MARKER}` line is kept as it is; reply with what replaces the "
        f"placeholder below it, Python only.\n\n"
        f"```python\n{cell.code}\n```"
    )
    messages = [
        {"role": "system", "content": head},
        {"role": "user", "content": task},
    ]
    messages.extend({"role": role, "content": content} for role, content in history)
    return messages


# =============================================================================
# WHAT HAPPENED TO EACH HOLE
# =============================================================================


class FillOutcome(StrEnum):
    """How one hole came out. Five states, because they need five different answers.

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
        >>> FillReport(names=(), codes=(), cells=()).complete
        True
    """

    names: tuple[str, ...]
    codes: tuple[str, ...]
    cells: tuple[FilledCell, ...]
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
    def unfilled(self) -> tuple[FilledCell, ...]:
        """Every hole still carrying the scaffolder's placeholder."""
        return tuple(cell for cell in self.cells if cell.outcome is not FillOutcome.FILLED)

    @property
    def complete(self) -> bool:
        """Whether every hole was filled. Not the same question as whether it converted well.

        True for a plan with no holes at all, which is honest -- there was nothing to leave
        unwritten -- and is why a caller reporting a conversion checks :attr:`holes` separately. A
        conversion that asked the model nothing has measured nothing.
        """
        return self.filled == self.holes

    def counts(self) -> dict[str, int]:
        """One count per :class:`FillOutcome`, every member present even at zero.

        A missing key is how a category quietly stops being reported.
        """
        tally = {outcome.value: 0 for outcome in FillOutcome}
        for cell in self.cells:
            tally[cell.outcome.value] += 1
        return tally

    def summary_line(self) -> str:
        """The one line a caller prints when it prints nothing else."""
        tally = ", ".join(f"{count} {name}" for name, count in self.counts().items() if count)
        detail = f" ({tally})" if tally else ""
        return (
            f"{self.filled}/{self.holes} hole(s) filled{detail}; "
            f"{self.first_time} first time, {self.after_retries} after retries, "
            f"{self.completions} completion(s) over {self.seconds:.1f}s"
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
            "scaffolded": self.scaffolded_summary,
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


_MISSING_NAME_STAGE = "definition"
"""The one check this driver makes that the shipped gate does not. See :func:`_undefined_name`."""


def _undefined_name(report: ValidationReport, hole: ScaffoldCell) -> tuple[str, ...]:
    """Refuse a body that does not define the name the cells below it read.

    The one thing checked here that :func:`~kedge.agent.validate.validate_cell` does not, and it
    is a stand-in rather than an addition. In the chat the kernel catches this: the cell is
    accepted, flushed, and the cells downstream of it fail on a name that was never bound, which
    marimo reports precisely. ``kedge convert`` runs no kernel at all -- it writes through
    :class:`~kedge.notebook.filedriver.FileNotebookDriver` -- so without this check the failure
    surfaces the next time somebody opens the notebook, three cells below the one that caused it,
    and the blame lands on a cell that was written correctly.

    Args:
        report: The gate's verdict, whose ``names`` hold what the body defines.
        hole: The cell being filled, whose name the body has to bind.

    Returns:
        One violation naming the cell that failed to define the name, or nothing when the body
        defines it.
    """
    if hole.name in report.names.public_defs:
        return ()
    defined = ", ".join(report.names.public_defs) or "nothing"
    return (
        f"definition: this cell must define '{hole.name}' -- the cells below read it by that "
        f"name. It defines {defined}.",
    )


def _missing_name_verdict(hole: ScaffoldCell, violations: tuple[str, ...]) -> str:
    """The block returned to the model, matching ``ValidationReport.render``'s shape."""
    return "\n".join(
        [
            f"The cell was rejected: it does not define '{hole.name}'. Fix the cause and "
            f"resubmit; you have a limited number of attempts.",
            "",
            *(f"  - {message}" for message in violations),
        ]
    )


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
        max_attempts: How many times one hole may be resubmitted after a rejection. Defaults to
            :data:`~kedge.agent.validate.MAX_VALIDATION_ATTEMPTS`.
        policy: What generated code may reach for. Defaults to the shipped default -- no network,
            no database, no writes outside the working directory. Whatever is passed, the model is
            told what it refuses: see :func:`policy_rules`.
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
    names = [cell.name for cell in scaffolded]
    codes = [cell.code for cell in scaffolded]
    positions = {cell.name: index for index, cell in enumerate(scaffolded)}
    rounding = RoundingContext.from_analysis(analysis)
    resolved_policy = policy or Policy()

    results: list[FilledCell] = []
    started = time.perf_counter()
    completions = 0
    abandoned = False
    for hole in holes_in(scaffolded):
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
        codes[positions[hole.name]] = result.code
        abandoned = stop_on_error and result.outcome is FillOutcome.ERROR

    seconds = time.perf_counter() - started
    report = FillReport(
        names=tuple(names),
        codes=tuple(codes),
        cells=tuple(results),
        scaffolded=tuple(scaffolded),
        seconds=seconds,
        completions=completions,
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
        missing = _undefined_name(report, hole)
        violations = report.messages or missing
        if report.stage is not None:
            stage: str | None = report.stage.value
        elif missing:
            stage = _MISSING_NAME_STAGE
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
        verdict = report.render() if report.messages else _missing_name_verdict(hole, missing)
        history.extend([("assistant", body), ("user", verdict)])

    detail = (
        f"the gate rejected every one of {len(attempts)} attempt(s); last: "
        + "; ".join(attempts[-1].violations[:3])
        if last is FillOutcome.REJECTED
        else f"no cell body in any of {len(attempts)} reply(ies)"
    )
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
        max_attempts: Attempts per hole, including validation repairs.
        policy: What generated code may reach for.
        stop_on_error: Abandon the run the first time the endpoint fails. See :func:`fill_holes`.
        sync: Scaffold the plan into the notebook first. Off leaves the file exactly as it is and
            fills only the holes already in it.
        workbook_path: The workbook whose cached values are the reconciliation baseline.
        handins_dir: Where hand-ins are persisted.
        contract_path: Where the hand-in contract lives.

    Returns:
        The :class:`FillReport`, with :attr:`FillReport.written` naming the cells spliced back in.

    Raises:
        PlanNotApprovedError: when the plan is not approved.
    """
    summary = ""
    if sync:
        result = await sync_notebook(
            plan,
            driver,
            handins_dir=handins_dir,
            workbook_path=workbook_path,
            contract_path=contract_path,
        )
        summary = result.summary(plan.version)
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
    return FillReport(
        names=report.names,
        codes=report.codes,
        cells=report.cells,
        scaffolded=report.scaffolded,
        seconds=report.seconds,
        completions=report.completions,
        written=tuple(written),
        scaffolded_summary=summary,
    )
