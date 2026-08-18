"""Ask a model for the cell bodies a scaffold left unwritten, and gate every answer.

`harness/convert.py` owns the pipeline; this module owns the half of it a model can be blamed
for. Given an approved plan it scaffolds the notebook, finds the ``TODO(kedge)`` holes
:func:`kedge.notebook.scaffold._stage_cell` leaves, asks for each one in scaffold order -- with
the analysis, the plan and the name registry as it stands *after the holes above it were filled*
-- and runs every reply through :func:`kedge.agent.validate.validate_cell`, the same five-stage
gate ``propose_cell`` runs, which needs no kernel.

What is sent, and why it is the product's own prompt rather than one written here, is
:mod:`harness.cellprompt`.

Every attempt is recorded rather than summarised. A model that needed three tries to stop writing
pandas is a different result from one that got it right first time, and a report that renders
those the same is lying by omission. One check is added beyond the shipped gate, and it is a
stand-in rather than an addition -- see :func:`_undefined_name`, which says at the point it
happened what the kernel would otherwise have said three cells later.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from harness.cellprompt import TODO_MARKER, cell_messages
from kedge.agent.context import CellFacts, NameRegistry
from kedge.agent.validate import (
    MAX_VALIDATION_ATTEMPTS,
    Policy,
    RoundingContext,
    ValidationReport,
    validate_cell,
)
from kedge.notebook.codegen import analyse_cells
from kedge.notebook.scaffold import build_cells
from kedge.plan.propose import CompletionRequest

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    from kedge.analysis.model import WorkbookAnalysis
    from kedge.notebook.scaffold import ScaffoldCell
    from kedge.plan.model import ProcessPlan
    from kedge.plan.propose import Completer

logger = logging.getLogger(__name__)

__all__ = [
    "TODO_MARKER",
    "CellAttempt",
    "CellOutcome",
    "ConversionResult",
    "GeneratedCell",
    "convert",
    "holes_in",
]

_GATE_LINE = re.compile(r"^_gate_\w+\s*=")
_FENCE = re.compile(r"```(?:[a-zA-Z0-9_+-]*)\n(.*?)```", re.DOTALL)


class CellOutcome(StrEnum):
    """How one hole came out. Four states, because they need four different answers.

    Example:
        >>> bool(CellOutcome.FILLED), bool(CellOutcome.REJECTED)
        (True, False)
    """

    FILLED = "filled"
    """The model wrote a body and the validation gate accepted it."""

    REJECTED = "rejected"
    """The model wrote a body every time and the gate refused every one. Its problem."""

    EMPTY = "empty"
    """The model answered with nothing a cell body could be read out of. Also its problem."""

    ERROR = "error"
    """The completer raised. A transport failure, an exhausted script, a refused request -- not
    the model's judgement, and it must never be scored as though it were."""

    def __bool__(self) -> bool:
        """True only for :attr:`FILLED`, so an unfilled hole cannot read as a success."""
        return self is CellOutcome.FILLED


@dataclass(frozen=True, slots=True)
class CellAttempt:
    """One request-and-verdict for one hole.

    Kept per attempt rather than summarised, because "passed on the third try after two pandas
    rejections" and "passed first time" are the same score and different models.
    """

    number: int
    code: str
    violations: tuple[str, ...] = ()
    stage: str | None = None
    seconds: float = 0.0
    error: str | None = None

    @property
    def accepted(self) -> bool:
        return self.error is None and not self.violations and bool(self.code.strip())

    def render(self) -> str:
        if self.error is not None:
            return f"    attempt {self.number}: the completer raised -- {self.error}"
        if self.accepted:
            return f"    attempt {self.number}: accepted ({self.seconds:.1f}s)"
        if not self.code.strip():
            return f"    attempt {self.number}: empty reply ({self.seconds:.1f}s)"
        head = "; ".join(self.violations[:3])
        return f"    attempt {self.number}: rejected at {self.stage} -- {head}"


@dataclass(frozen=True, slots=True)
class GeneratedCell:
    """One hole, and everything that happened to it."""

    name: str
    stage_id: str | None
    outcome: CellOutcome
    code: str
    """The cell body as it will be rendered: the model's where one was accepted, the
    scaffolder's placeholder where none was. Never empty -- a hole left as a hole still has to
    produce a notebook that parses, or the failure attributes itself to the wrong place."""
    attempts: tuple[CellAttempt, ...] = ()
    detail: str = ""

    @property
    def tries(self) -> int:
        return len(self.attempts)

    @property
    def first_time(self) -> bool:
        """Accepted on the first attempt. The measurement everybody actually wants."""
        return self.outcome is CellOutcome.FILLED and self.tries == 1

    def render(self) -> str:
        head = f"  [{self.outcome.value.upper()}] {self.name} ({self.tries} attempt(s))"
        if self.detail:
            head += f" -- {self.detail}"
        return "\n".join([head, *(attempt.render() for attempt in self.attempts)])


@dataclass(frozen=True, slots=True)
class ConversionResult:
    """A scaffold with its holes filled, or not, and the record of the filling.

    ``names`` and ``codes`` are in the scaffolder's own order, which is the order kedge would
    write the file in. :func:`harness.render.plan_layout` is what decides the order the eval's
    linear driver needs, and it reports the difference as a finding rather than tidying it away.
    """

    plan: ProcessPlan
    names: tuple[str, ...]
    codes: tuple[str, ...]
    generated: tuple[GeneratedCell, ...]
    scaffolded: tuple[ScaffoldCell, ...] = ()
    """The cells as the scaffolder emitted them, roles and stage ids intact. Kept because the
    role of a cell -- head, stage, checkpoint, hand-off, reconciliation -- is what lets the
    report say *which* part of a scaffolded notebook a finding is about."""
    seconds: float = 0.0
    completions: int = 0

    @property
    def holes(self) -> int:
        """How many cells the scaffolder left for the model. The denominator."""
        return len(self.generated)

    @property
    def filled(self) -> int:
        return sum(1 for cell in self.generated if cell.outcome is CellOutcome.FILLED)

    @property
    def first_time(self) -> int:
        return sum(1 for cell in self.generated if cell.first_time)

    @property
    def attempts(self) -> int:
        return sum(cell.tries for cell in self.generated)

    @property
    def errored(self) -> int:
        """Holes the completer never answered. Not the model's judgement -- see :attr:`errors`."""
        return sum(1 for cell in self.generated if cell.outcome is CellOutcome.ERROR)

    @property
    def complete(self) -> bool:
        """Whether every hole was filled. Not the same question as whether it graded well.

        True for a plan with no holes at all, which is honest -- there was nothing to leave
        unwritten -- and is why :func:`harness.convert.convert_and_grade` checks :attr:`holes`
        separately before it reports a score. A conversion that asked the model nothing has
        measured nothing.
        """
        return self.filled == self.holes

    def counts(self) -> dict[str, int]:
        """One count per :class:`CellOutcome`, every member present even at zero.

        A missing key is how a category quietly stops being reported.
        """
        tally = {outcome.value: 0 for outcome in CellOutcome}
        for cell in self.generated:
            tally[cell.outcome.value] += 1
        return tally

    def summary_line(self) -> str:
        tally = ", ".join(f"{count} {name}" for name, count in self.counts().items() if count)
        return (
            f"{self.filled}/{self.holes} hole(s) filled ({tally}); "
            f"{self.first_time} first time, {self.attempts} completion(s) over "
            f"{self.seconds:.1f}s"
        )

    def render(self) -> str:
        blocks = [f"generation: {self.summary_line()}"]
        blocks.extend(cell.render() for cell in self.generated)
        return "\n".join(blocks)


def holes_in(cells: Iterable[ScaffoldCell]) -> tuple[ScaffoldCell, ...]:
    """The cells the scaffolder left for the model, found by its own marker.

    By marker rather than by role, so a scaffolder that grows a second kind of unwritten cell is
    measured by this eval on the day it ships rather than on the day somebody notices.
    """
    return tuple(cell for cell in cells if TODO_MARKER in cell.code)


def _split_hole(code: str) -> tuple[str, str]:
    """Split a scaffolded stage cell into the part kedge wrote and the part it left.

    The header is every line up to and including the ``TODO(kedge)`` comment, plus any
    ``_gate_...`` assignment below it. The gate is not translation: it is the line that makes a
    cell downstream of a checkpoint invisible until the checkpoint is recorded, and a model that
    rewrote the body without it would silently un-gate the notebook -- the exact defect
    ``harness/drive.py:visible_cells`` was written to catch. It is kept out of the hole so it
    cannot be lost, and so a model is not marked down for a line it was never asked to write.

    Returns:
        ``(header, placeholder)``. The header is returned with no trailing newline.
    """
    lines = code.splitlines()
    marker = next((index for index, line in enumerate(lines) if TODO_MARKER in line), None)
    if marker is None:
        return "", code
    end = marker + 1
    while end < len(lines) and (
        _GATE_LINE.match(lines[end]) or lines[end].lstrip().startswith("#")
    ):
        end += 1
    return "\n".join(lines[:end]), "\n".join(lines[end:])


def _without_the_marker(header: str) -> str:
    """The header with the ``TODO(kedge)`` instruction removed, for a hole that got filled.

    The marker is an instruction to a model, not documentation of the stage, and leaving it above
    working code has two costs. :func:`holes_in` finds holes by that marker, so every translated
    cell goes on reading as unfinished for ever -- the eval's own report said five holes and five
    filled while the artifact it kept still declared five holes. And the kept notebook is meant to
    be *read*: a reviewer opening it finds "TODO(kedge): translate this stage" above a finished
    translation and cannot tell what was actually left undone.

    Only the marker's own comment run is dropped, plus the bare ``#`` separator above it if there
    is one. Everything the scaffolder wrote about the stage -- intent, sources, assumptions, the
    operations it implements -- is documentation and stays.
    """
    lines = header.splitlines()
    start = next((index for index, line in enumerate(lines) if TODO_MARKER in line), None)
    if start is None:
        return header
    end = start + 1
    while end < len(lines) and lines[end].lstrip().startswith("#"):
        end += 1
    if start and lines[start - 1].strip() == "#":
        start -= 1
    kept = [*lines[:start], *lines[end:]]
    return "\n".join(kept).rstrip("\n")


def _body_of(response: str) -> str:
    """The cell body inside a model's reply, fence or no fence.

    The product never needs this: ``propose_cell`` takes code as a tool argument. Without a tool
    surface the body arrives as prose, and a model that wraps it in ``` and is then scored as
    having produced nothing would be a harness bug reported as a model failure. Stripping is
    conservative -- the first fenced block if there is one, the whole reply otherwise -- and a
    reply that is only prose still comes back as prose and still fails the syntax stage, which is
    the honest outcome.
    """
    fenced = _FENCE.search(response)
    text = fenced.group(1) if fenced else response
    return text.strip("\n").rstrip()


def _without_echoed_header(body: str) -> str:
    """Drop a comment header the model repeated back, marker and all.

    The instruction says the header is kept for it; some models will send it anyway. Prepending
    a second copy leaves a translated cell carrying two copies of the stage documentation, and
    -- since :func:`_without_the_marker` only strips the marker kedge itself wrote -- an echoed
    ``TODO(kedge): translate this stage`` above working code, which is the state
    :func:`holes_in` reads as an unfinished cell.

    Only a *leading* comment block containing the marker is dropped. A model that opens its
    translation with a comment of its own keeps it, which is the behaviour the house style
    actually wants.
    """
    lines = body.splitlines()
    end = 0
    while end < len(lines) and (not lines[end].strip() or lines[end].lstrip().startswith("#")):
        end += 1
    if end and any(TODO_MARKER in line for line in lines[:end]):
        return "\n".join(lines[end:]).lstrip("\n")
    return body


_MISSING_NAME_STAGE = "definition"
"""The one check this eval makes that the shipped gate does not. See :func:`_undefined_name`."""


def _undefined_name(report: ValidationReport, hole: ScaffoldCell) -> tuple[str, ...]:
    """Refuse a body that does not define the name the cells below it read.

    The one thing checked here that :func:`~kedge.agent.validate.validate_cell` does not, and it
    is a stand-in rather than an addition. In the product the kernel catches this: the cell is
    accepted, flushed, and the cells downstream of it fail on a name that was never bound, which
    marimo reports precisely. There is no kernel here, so without this check the failure surfaces
    as a run that stopped three cells later and the blame lands on a cell that was written
    correctly. Naming it at the point it happened is the whole of requirement 1.

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
    """The name registry for a notebook that only exists as text.

    Built from marimo's own analysis of every cell in the file rather than from a kernel graph,
    which is the whole reason this eval needs no kernel. It is the whole notebook and not "the
    cells above" on purpose: marimo is a dataflow graph, so a name defined below is a name this
    cell may not redefine, and the single-definition rule the gate enforces is about the file
    rather than about position in it. The registry is rebuilt after every hole is filled, so a
    cell is also told about the names the holes above it have just defined.
    """
    analyses = analyse_cells(list(codes))
    return NameRegistry(
        CellFacts(id=str(index), name=name, defs=analysis.defs, refs=analysis.refs)
        for index, (name, analysis) in enumerate(zip(names, analyses, strict=True))
    )


def convert(
    plan: ProcessPlan,
    *,
    completer: Completer,
    analysis: WorkbookAnalysis | None = None,
    model: str = "",
    temperature: float = 0.2,
    max_attempts: int = MAX_VALIDATION_ATTEMPTS,
    policy: Policy | None = None,
    workbook_path: Path | None = None,
    handins_dir: Path | None = None,
    contract_path: Path | None = None,
) -> ConversionResult:
    """Scaffold the plan and have the model fill every hole, in the scaffolder's order.

    Args:
        plan: An approved plan. The scaffolder refuses an unapproved one and there is no flag
            here to talk it out of that, because a conversion of a plan nobody approved is not
            the thing being measured.
        completer: Anything with ``complete(CompletionRequest) -> str``. Indifferent to whether
            it is metered, scripted or live -- that is the seam between this module and
            ``harness/live.py``.
        analysis: The workbook analysis, pinned into every request. Strongly recommended: without
            it the model is told in as many words that it is guessing at the workbook's
            structure, and ``.round()`` cannot be checked against Excel's rounding.
        model: The model name put on the request. The completer may override it.
        temperature: Sent on every request, subject to the completer's own negotiation.
        max_attempts: How many times one hole may be resubmitted after a rejection. Defaults to
            the product's :data:`~kedge.agent.validate.MAX_VALIDATION_ATTEMPTS`.
        policy: What generated code may reach for. Defaults to the shipped default -- no network,
            no database, no writes outside the working directory. Whatever is passed, the model
            is told what it refuses: see :func:`policy_rules`.
        workbook_path: Passed to the scaffolder for the reconciliation cell's baseline.
        handins_dir: Passed to the scaffolder. Defaults to the scaffolder's own default.
        contract_path: Passed to the scaffolder. It need not exist.

    Returns:
        The :class:`ConversionResult`. A hole the model never filled keeps the scaffolder's
        placeholder and is recorded as unfilled -- never dropped, and never quietly counted as
        written.
    """
    cells = build_cells(
        plan,
        handins_dir=handins_dir,
        workbook_path=workbook_path,
        contract_path=contract_path,
    )
    names = [cell.name for cell in cells]
    codes = [cell.code for cell in cells]
    positions = {cell.name: index for index, cell in enumerate(cells)}
    rounding = RoundingContext.from_analysis(analysis)
    resolved_policy = policy or Policy()

    generated: list[GeneratedCell] = []
    started = time.perf_counter()
    completions = 0
    for hole in holes_in(cells):
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
        generated.append(result)
        completions += result.tries
        codes[positions[hole.name]] = result.code

    seconds = time.perf_counter() - started
    logger.info(
        "converted plan v%d for %s: %d of %d hole(s) filled",
        plan.version,
        plan.workbook,
        sum(1 for cell in generated if cell.outcome is CellOutcome.FILLED),
        len(generated),
    )
    return ConversionResult(
        plan=plan,
        names=tuple(names),
        codes=tuple(codes),
        generated=tuple(generated),
        scaffolded=tuple(cells),
        seconds=seconds,
        completions=completions,
    )


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
) -> GeneratedCell:
    """Ask for one cell body, gate it, and retry on violations exactly as the real loop does."""
    header, _placeholder = _split_hole(hole.code)
    kept = _without_the_marker(header)
    history: list[tuple[str, str]] = []
    attempts: list[CellAttempt] = []
    last = CellOutcome.EMPTY

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
        except Exception as exc:
            elapsed = time.perf_counter() - began
            attempts.append(
                CellAttempt(
                    number=number, code="", seconds=elapsed, error=f"{type(exc).__name__}: {exc}"
                )
            )
            logger.warning("the completer raised on cell %s: %r", hole.name, exc)
            return GeneratedCell(
                name=hole.name,
                stage_id=hole.stage_id,
                outcome=CellOutcome.ERROR,
                code=hole.code,
                attempts=tuple(attempts),
                detail=f"{type(exc).__name__}: {exc}",
            )
        elapsed = time.perf_counter() - began

        body = _without_echoed_header(_body_of(response))
        if not body.strip():
            attempts.append(CellAttempt(number=number, code="", seconds=elapsed))
            last = CellOutcome.EMPTY
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
            CellAttempt(
                number=number,
                code=code,
                violations=violations,
                stage=stage,
                seconds=elapsed,
            )
        )
        if not violations:
            return GeneratedCell(
                name=hole.name,
                stage_id=hole.stage_id,
                outcome=CellOutcome.FILLED,
                code=code,
                attempts=tuple(attempts),
            )
        last = CellOutcome.REJECTED
        verdict = report.render() if report.messages else _missing_name_verdict(hole, missing)
        history.extend([("assistant", body), ("user", verdict)])

    detail = (
        f"the gate rejected every one of {len(attempts)} attempt(s); last: "
        + "; ".join(attempts[-1].violations[:3])
        if last is CellOutcome.REJECTED
        else f"no cell body in any of {len(attempts)} reply(ies)"
    )
    logger.info("cell %s left unfilled: %s", hole.name, detail)
    return GeneratedCell(
        name=hole.name,
        stage_id=hole.stage_id,
        outcome=last,
        code=hole.code,
        attempts=tuple(attempts),
        detail=detail,
    )
