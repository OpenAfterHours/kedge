"""Approved plan to notebook cells. Plan-driven, not analysis-driven. No LLM.

Notebook shape follows the *process*, not the spreadsheet's layout. The fixed parts stay fixed —
the hand-in selector at the head (PLAN 2.8), the contract check, the reconciliation cell at the
tail — and everything between is whatever the plan says it is (PLAN M2 step 4).

Four rules govern what is emitted, and each of them is a rule because breaking it has bitten
somebody:

- **Stage order is cell order, and every stage gets a named cell.** Named cells are what give
  stable addressing across turns; cell ids are opaque and churn. The name is derived from the
  stage id and guaranteed unique and identifier-safe (PLAN 1.1).
- **``hide_code=False`` on every cell.** ``create_cell`` defaults to hiding code, and the whole
  point of kedge is that the user can read and edit what was written.
- **polars, LazyFrame throughout, ``.collect()`` only at boundaries**, with Excel semantics
  routed through ``kedge.xl``. ``import pandas`` never appears, and :func:`build_cells` refuses
  to return a cell that mentions it rather than trusting the rule to hold.
- **Checkpoints block for real.** A checkpoint scaffolds to an ``mo.ui`` cell plus a gate cell
  that calls ``mo.stop`` until a decision *and* a reason are recorded. ``mo.stop`` halts the
  cell and everything downstream of it in the dataflow graph, so a downstream cell referencing
  an unapproved checkpoint's output cannot silently proceed.

**The head and the tail call the real machinery.** The hand-in cells go through
:mod:`kedge.ingest` — ``receive`` for the managed record, ``check_drift`` for the shape diff,
``read_data`` for the frame — the check cell through :mod:`kedge.contracts`, and the tail
through :mod:`kedge.reconcile`. Nothing here re-implements any of it, and the frame cell in
particular makes the *identical* ``read_data`` call ``kedge.contracts.validate`` makes rather
than an equivalent-looking one, because "equivalent-looking" is a refactor away from a green
contract panel describing rows nobody computed on.

The head is emitted receive, contract, drift, check, frame. The contract is *loaded* ahead of
the other three because which sheet and which header row to read is part of the agreement and
all three of them need it; loading a contract is not checking a hand-in against it, so
PLAN 2.8's ordering still holds — drift is *reported* before the contract check, because
"column ``EAD`` became ``EAD_GBP``" is a far better message than a schema-validation traceback.
The check then comes before the frame so that ``mo.stop`` in the frame cell can keep a hand-in
that failed its contract out of every calculation below it.

The tail cell is the one place in this module where a mistake would be dangerous rather than
merely wrong. Where there is no baseline — no workbook, no cached values, no region matching a
notebook column — it reports "not reconciled" and says why. It has no path that reports
"passed" (PLAN 6.2).

Nothing here writes to a notebook without an approved plan. :func:`sync_notebook` checks
``plan.approval`` and refuses, and there is no parameter to talk it out of that.

**One way in, and it assumes nothing about what is already there.** :func:`sync_notebook` creates
the cells the plan calls for that are missing, updates only those kedge wrote and nobody has
touched since, and deletes nothing. It replaced a ``scaffold_notebook`` that created every cell
unconditionally — right for the empty notebook the open sequence used to be the only route to,
and wrong everywhere else: the second open of a scaffolded workbook died on ``CellNameError`` at
the first cell, and a plan approved in the chat could not reach the notebook at all. Being
indifferent to what is already in the notebook is what lets an approval — or an amendment — land
while the user is looking at it.

**Driver dependency.** ``kedge.notebook.driver`` is the only module permitted to touch
``marimo._code_mode``, and it is owned elsewhere. This module depends on the narrow
:class:`CellSyncer` protocol below — three methods, matching the ``create_cell``, ``edit_cell``
and ``list_cells`` signatures verified in ``docs/marimo-api.md`` §2, and deliberately no
``delete_cell``. If that surface moves, this protocol is the single place to adapt.

References:
- PLAN.md 2.2 (checkpoints), 2.5 (polars house rules), 2.6 (Excel semantics), 2.8 (hand-in
  head), 4.5 (reconciliation), 6.2 (never a false pass), M2 step 4, M5 (contracts);
  docs/marimo-api.md §2.
"""

from __future__ import annotations

import ast
import keyword
import logging
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from kedge.analysis.model import ExcelPattern
from kedge.errors import KedgeError, NotebookError
from kedge.plan.model import Handoff, ProcessPlan, SourceOrigin, Stage, StageKind

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "HEAD_CELL_NAMES",
    "TAIL_CELL_NAMES",
    "TODO_MARKER",
    "CellRole",
    "CellSource",
    "CellSync",
    "CellSyncer",
    "PlanNotApprovedError",
    "ScaffoldCell",
    "ScaffoldError",
    "SyncOutcome",
    "SyncResult",
    "build_cells",
    "cell_name_for",
    "head_handin_is_read",
    "head_handin_reader",
    "holes_in",
    "is_unwritten",
    "split_hole",
    "strip_marker",
    "sync_notebook",
]

CellRole = Literal["setup", "handin", "stage", "checkpoint", "handoff", "reconcile"]

HEAD_CELL_NAMES = (
    "kedge_setup",
    "kedge_briefing",
    "kedge_run_mode",
    "kedge_run",
    "handin_source",
    "handin",
    "handin_contract",
    "handin_drift",
    "handin_check",
    "handin_frame",
)
"""The head, in emission order (PLAN 2.8). Every name it may claim, not every name it will.

The four setup cells are emitted for every plan. The six hand-in cells below them are emitted
only when something in the plan reads the notebook's own hand-in -- see
:func:`head_handin_is_read`, which is where the argument for that lives. This tuple stays the
full catalogue because it is what tells anything reading a notebook which cells are kedge's
fixture and which are the plan's.

The briefing comes first because it is what somebody opening this in eight months needs
before anything else: what the process is for, and what the conversion left unsettled. The run
cells follow because everything after them is *part of* a run: which hand-in was consumed
and which decision was recorded are facts about a pass through the process, not about the
notebook. Resuming has to happen before anything reads a hand-in, and the banner they render is
what a user coming back after two days needs before they read anything else.

The contract is loaded before the drift panel because it names the sheet and the header row,
and the profile, the check and the frame all have to read the same rows or the check is a
statement about data nobody used. Loading is not checking, so drift is still *reported* before
the contract check: a renamed column explains a failure in one line where a schema report
explains it in fifteen. The check then comes before the frame so that the frame cell can
``mo.stop`` on a failed contract, which is what makes the check a gate rather than a comment:
nothing downstream of the frame runs on a hand-in that was rejected.
"""

TAIL_CELL_NAMES = ("reconciliation_values", "reconciliation")
"""The tail, in emission order (PLAN 4.5).

Two cells because the map and the panel are owned by different people: the map is a judgement
about the translation and carries a ``TODO(kedge)``, the panel is machinery kedge owns whole.
:func:`_values_cell` argues it.
"""

_RESERVED = {
    *HEAD_CELL_NAMES,
    *TAIL_CELL_NAMES,
    "KEDGE_RUNS",
    "KEDGE_RUN_ID",
    "RUNS_DIR",
    "handin_drop",
    "handin_pick",
    "handin_profile",
    "kedge",
    "mo",
    "pl",
    "datetime",
    "pathlib",
    "HANDIN_DIR",
    "WORKBOOK",
    "CONTRACT_PATH",
}

_IDENTIFIER_UNSAFE = re.compile(r"[^0-9a-zA-Z_]+")
_DATE_PARAMETER = re.compile(
    r"(?:^|_)(?:date|day|period|month|quarter|year|as_of|cob)(?:_|$)", re.IGNORECASE
)
"""Parameter names that mean a date, so the input is a date picker rather than a text box."""
_BANNED_IMPORT = re.compile(r"\b(?:import\s+pandas|from\s+pandas\s+import)\b", re.IGNORECASE)

_PATTERN_HINTS: dict[ExcelPattern, str] = {
    ExcelPattern.ARITHMETIC: 'col("a") * col("b"); via .xl where empties or zero divisors are possible',
    ExcelPattern.IF_SIMPLE: "pl.when(cond).then(x).otherwise(y)",
    ExcelPattern.IF_NESTED: "chained .when().then(), one per branch",
    ExcelPattern.IFERROR: "pl.when(<explicit error condition>).then(fallback).otherwise(x); do not swallow",
    ExcelPattern.SUMIF: ".filter(cond).select(col(s).sum()), or group_by().agg() if the criterion is a key",
    ExcelPattern.SUMIFS: "group_by([...]).agg([...]) then join back",
    ExcelPattern.COUNTIFS: "group_by([...]).agg(pl.len()) then join back",
    ExcelPattern.VLOOKUP_EXACT: 'join(right, left_on=..., right_on=..., how="left"); check key uniqueness first or rows multiply',
    ExcelPattern.VLOOKUP_APPROX: 'join_asof(right, on=key, strategy="backward")',
    ExcelPattern.INDEX_MATCH: "same as an exact VLOOKUP: a left join",
    ExcelPattern.SUMPRODUCT: '(col("a") * col("b")).sum()',
    ExcelPattern.RUNNING_TOTAL: 'col("x").cum_sum()',
    ExcelPattern.PRIOR_ROW: 'col("x").shift(1); a window, never a loop',
    ExcelPattern.PARAMETER_REF: "a Python constant, or a value from the parameters frame",
    ExcelPattern.AGGREGATE: ".select(...) or .group_by(...).agg(...)",
    ExcelPattern.TEXT_MANIPULATION: "string expressions under col(...).str",
    ExcelPattern.DATE_ARITHMETIC: "temporal expressions; .xl.serial_to_date() where Excel serials appear",
    ExcelPattern.PIVOT: ".group_by(...).agg(...) or .pivot(...)",
    ExcelPattern.SUBTOTAL: ".filter(...) then aggregate; check which rows were actually visible",
    ExcelPattern.LITERAL: "a parameters frame or module constants",
    ExcelPattern.UNKNOWN: "no known pattern; this is why the plan carries an open question",
}


class ScaffoldError(NotebookError):
    """The plan could not be turned into notebook cells."""


class PlanNotApprovedError(ScaffoldError):
    """Scaffolding was attempted on a plan nobody has approved.

    The review gate is the whole reason the plan exists. A plan that reaches the notebook
    unreviewed is forty cells the user has to read backwards to discover the decomposition was
    wrong (PLAN 2.2, M2 step 3).
    """


class CellSource(Protocol):
    """The two things :func:`sync_notebook` reads off a cell already in the notebook.

    Matches :class:`kedge.notebook.model.CellInfo` structurally. ``code`` is ``None`` when the
    cell was listed without its source, which is a state the sync never asks for and treats as
    "not what kedge wrote" if it ever sees one.
    """

    @property
    def name(self) -> str:
        """The cell's name, or empty for an unnamed cell."""
        ...

    @property
    def code(self) -> str | None:
        """The cell's source, or ``None`` if it was listed without it."""
        ...


class CellSyncer(Protocol):
    """The slice of the notebook driver :func:`sync_notebook` needs.

    Three methods, and no more. Reading the notebook is what makes the sync non-destructive,
    creating is the scaffold itself, and editing is what lets an amendment reach a cell nobody has
    touched. ``delete_cell`` is absent, and deliberately: a plan that no longer mentions a stage
    is not the user consenting to lose the code that implemented it.

    The return types are deliberately unconstrained. Both real implementations --
    :class:`kedge.notebook.driver.NotebookDriver` and
    :class:`kedge.notebook.filedriver.FileNotebookDriver` -- return a
    :class:`~kedge.notebook.model.MutationResult`, but pinning that here would make this module
    depend on the driver's vocabulary for a value it does not read. The sync knows the names it
    asked for and reports those; what came back is the caller's business.
    """

    async def list_cells(self, *, with_code: bool = True) -> Sequence[CellSource]:
        """Return every cell, in notebook order."""
        ...

    async def create_cell(
        self,
        code: str,
        *,
        name: str,
        after: str | None = None,
        hide_code: bool = False,
    ) -> object:
        """Create one cell, optionally behind a named neighbour."""
        ...

    async def edit_cell(self, target: str, code: str, *, run: bool = True) -> object:
        """Replace one cell's source."""
        ...


SyncOutcome = Literal["created", "updated", "unchanged", "diverged", "refused"]
"""What became of one cell the plan calls for.

``diverged`` and ``refused`` both mean "left as it was", and they are kept apart because they
read differently to the person holding the result: the first is a cell somebody has worked on,
which is the normal and expected state of a notebook being converted, and the second is the
kernel saying no, which is not.
"""


@dataclass(frozen=True, slots=True)
class CellSync:
    """What :func:`sync_notebook` did about one cell, and why where that needs saying."""

    name: str
    outcome: SyncOutcome
    detail: str = ""


@dataclass(frozen=True, slots=True)
class SyncResult:
    """Everything one :func:`sync_notebook` call did, in emission order."""

    cells: tuple[CellSync, ...] = ()
    obsolete: tuple[str, ...] = ()
    """Cells the superseded plan put in the notebook that this one does not call for.

    Named rather than removed. See :func:`sync_notebook` on why nothing here deletes.
    """

    def named(self, outcome: SyncOutcome) -> tuple[str, ...]:
        """The names of the cells with this outcome, in emission order."""
        return tuple(cell.name for cell in self.cells if cell.outcome == outcome)

    @property
    def wrote_anything(self) -> bool:
        """Whether the notebook changed at all."""
        return bool(self.named("created") or self.named("updated"))

    def summary(self, version: int) -> str:
        """One sentence for a step's detail line, a log record or a chat notice.

        Silence about a cell that was left alone would be the wrong kind of quiet: "12 cells
        written" beside a stage the sync declined to touch reads as a notebook that matches the
        plan when it does not.
        """
        parts = []
        for outcome, phrasing in (
            ("created", "written"),
            ("updated", "updated"),
            ("unchanged", "already in line"),
        ):
            found = self.named(outcome)
            if found:
                parts.append(f"{len(found)} {phrasing}")
        head = f"plan v{version}: " + (", ".join(parts) if parts else "nothing to write")

        diverged = self.named("diverged")
        if diverged:
            head += (
                f". {len(diverged)} cell(s) have been edited since kedge wrote them and were left "
                f"alone ({', '.join(diverged)}) -- ask kedge in the chat to bring them into line "
                f"if that is what you want"
            )
        refused = self.named("refused")
        if refused:
            head += f". The kernel refused {len(refused)}: " + "; ".join(
                f"{cell.name} ({cell.detail})" for cell in self.cells if cell.outcome == "refused"
            )
        if self.obsolete:
            head += (
                f". {len(self.obsolete)} cell(s) implement stages this plan no longer has "
                f"({', '.join(self.obsolete)}); nothing was deleted"
            )
        return head


@dataclass(frozen=True, slots=True)
class ScaffoldCell:
    """One cell to be created, before it reaches the driver.

    Building these separately from writing them is what makes the scaffolder testable without a
    live kernel, and what lets the CLI show the user what would be written before it is.
    """

    name: str
    code: str
    role: CellRole = "stage"
    stage_id: str | None = None
    hide_code: bool = False
    """Always False. The user must be able to read what was written (PLAN 1.1)."""


# =============================================================================
# HOLES
# =============================================================================
#
# The scaffolder writes cells it deliberately leaves unwritten, and marks each one. Until now
# nothing in `src/` read those marks: the code that finds them lived in `evals/harness/cellgen.py`
# and shipped nowhere, so the product could write a hole and had no way to say it had one. These
# four functions are that seam, in the module that writes the marker, so the thing that finds a
# hole and the thing that makes one cannot drift apart.
#
# The rule they enforce is narrower than it first looks, and the narrowing is the point. Finding
# the marker *anywhere* in a body -- a plain substring test -- read a reviewer's own
# `# TODO(kedge): ...` note under a finished translation as an unwritten cell, and the conversion
# driver then truncated the translation at the note and wrote a model's answer over it. A hole is
# the marker in a shape the scaffolder writes: in the cell's leading comment run, or embedded in
# code where no comment character precedes it. Neither is a shape a person produces by accident.


TODO_MARKER = "TODO(kedge)"
"""The marker written above every body the scaffolder leaves for somebody else to write.

Three places emit it and a hole is found by the marker rather than by the role, so a fourth
that ships tomorrow is counted tomorrow: :func:`_stage_cell` for an untranslated stage,
:func:`_values_cell` for the region map, and
:meth:`kedge.plan.model.Stage.effective_handoff` for a hand-off the plan gave no statement.

The first two write it as a Python comment in the cell's **leading comment run**; the third
embeds it in the statement string itself, as SQL's own ``--`` comment. Those two shapes are what
:func:`is_unwritten` looks for, and looking for the marker *anywhere* instead was a real bug --
see that function.
"""

_GATE_LINE = re.compile(r"^_gate_\w+\s*=")
"""A checkpoint gate assignment, which sits below the marker and is not translation."""


def _marker_in_leading_comments(code: str) -> int | None:
    """The line the scaffolder's own marker comment sits on, or ``None``.

    "Leading" is the whole of it. The scaffolder writes its marker into the run of comments at
    the top of a cell, above the passthrough body -- always, in both places that write a Python
    comment. So a marker that appears *after* an executable statement was put there by somebody
    else, and treating it as a hole is destructive rather than merely wrong: everything above it
    becomes the header, the note itself is deleted by :func:`strip_marker`, and a model's answer
    is appended below a translation that is now dead code. That is not hypothetical -- a
    reviewer's ``# TODO(kedge): E-12 still needs the statutory-only filter`` under a finished
    translation was truncated and overwritten exactly that way.

    Args:
        code: The cell's source.

    Returns:
        The index of the marker's line, or ``None`` when the leading comment run holds none.
    """
    for index, line in enumerate(code.splitlines()):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return None  # the first statement: the leading comment run is over.
        if TODO_MARKER in line:
            return index
    return None


def _marker_outside_a_comment(code: str) -> bool:
    """Whether the marker sits in code rather than in a Python comment.

    The one shape a person does not write by accident. A hand-off the plan gave no statement gets
    ``-- TODO(kedge): ... Paste the query or command this step runs`` *inside the statement
    literal*, so the marker reaches the file with no ``#`` in front of it. A human's note always
    has one, wherever in the cell it sits.

    Such a cell still owes work -- there is no statement to hand anybody -- but there is nothing
    a splice could safely replace, which is why :func:`split_hole` refuses it and the conversion
    driver leaves it alone. The plan is where that one is fixed.
    """
    return any(
        TODO_MARKER in line and "#" not in line[: line.index(TODO_MARKER)]
        for line in code.splitlines()
    )


def is_unwritten(code: str) -> bool:
    """Whether a cell body still carries work the scaffolder left for somebody else.

    A substring test over the whole body was wrong in one direction that costs a user their work:
    a finished translation carrying a reviewer's ``TODO(kedge)`` note read as a hole, and the
    conversion driver truncated it at the note and appended a model's answer over the top. So the
    marker is recognised in the two shapes the scaffolder writes and in neither of the shapes a
    person does -- see :func:`_marker_in_leading_comments` and :func:`_marker_outside_a_comment`.

    Args:
        code: The cell's source.

    Returns:
        True when the marker is one the scaffolder wrote.
    """
    return _marker_in_leading_comments(code) is not None or _marker_outside_a_comment(code)


def holes_in(cells: Iterable[ScaffoldCell]) -> tuple[ScaffoldCell, ...]:
    """The cells the scaffolder left for somebody else, in the order it emitted them.

    Order matters and is the scaffolder's own: a hole is filled against the names defined above
    it, so filling them out of order asks for a translation of a frame that does not exist yet.

    Not every hole can be *filled* -- see :func:`split_hole`, which refuses the one shape that has
    no placeholder to replace. A caller writing cells has to honour that refusal; a caller
    counting what a notebook still owes should not.

    Args:
        cells: The scaffolded cells.

    Returns:
        Those still carrying the marker.
    """
    return tuple(cell for cell in cells if is_unwritten(cell.code))


def split_hole(code: str) -> tuple[str, str]:
    """Split a scaffolded cell into the part kedge wrote and the part it left.

    The header is every line up to and including the marker's comment run, plus any ``_gate_...``
    assignment below it. The gate is not translation: it is the line that makes a cell downstream
    of a checkpoint invisible until the checkpoint is recorded, and a body rewritten without it
    would silently un-gate the notebook. It is kept out of the hole so it cannot be lost, and so
    nobody is asked to write a line they were never shown the reason for.

    **The header can never contain a statement kedge did not write.** Everything above the marker
    is comments, by construction: the marker is only recognised inside the leading comment run, so
    a cell whose marker follows executable code is not a hole at all and comes back unsplit. Below
    the marker only comments and ``_gate_`` assignments are absorbed, and both are kedge's own.

    Args:
        code: The cell's source.

    Returns:
        ``(header, placeholder)``, the header with no trailing newline. A cell this cannot split
        comes back as ``("", code)`` -- it is not a fillable hole, and reporting it as an empty one
        would invite a caller to overwrite a finished cell. Two cases reach that: a cell with no
        marker at all, and a hand-off whose marker is embedded in its statement string, where
        there is no placeholder body to replace.
    """
    marker = _marker_in_leading_comments(code)
    if marker is None:
        return "", code
    lines = code.splitlines()
    end = marker + 1
    while end < len(lines) and (
        _GATE_LINE.match(lines[end]) or lines[end].lstrip().startswith("#")
    ):
        end += 1
    return "\n".join(lines[:end]), "\n".join(lines[end:])


def strip_marker(header: str) -> str:
    """The header with the marker's own instruction removed, for a hole that has been filled.

    The marker is an instruction to whoever writes the body, not documentation of the stage, and
    leaving it above working code costs twice. :func:`holes_in` finds holes by it, so a
    translated cell would read as unfinished for ever and every count of what is left would be
    wrong. And the notebook is meant to be *read*: a reviewer who finds "TODO(kedge): translate
    this stage" above a finished translation cannot tell what was actually left undone.

    Only the marker's own comment run goes, plus the bare ``#`` separator above it. Everything
    else the scaffolder wrote -- intent, sources, assumptions, the operations it implements --
    is documentation of the stage and stays.

    Args:
        header: The header half of :func:`split_hole`.

    Returns:
        The header without the marker. One that never had it is returned unchanged.
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
    return "\n".join([*lines[:start], *lines[end:]]).rstrip("\n")


# =============================================================================
# NAMES
# =============================================================================


def cell_name_for(stage_id: str, taken: Iterable[str] = ()) -> str:
    """Derive a valid, unique Python identifier from a stage id.

    The cell name is the stable address for every later edit, so it has to be an identifier, it
    has to be unique, and it must not collide with the fixed head and tail cells.

    Args:
        stage_id: The plan's stage id, which is free-form by design.
        taken: Names already in use.

    Returns:
        A valid identifier, suffixed with a number where the obvious name was taken.
    """
    cleaned = _IDENTIFIER_UNSAFE.sub("_", stage_id.strip()).strip("_").lower()
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"stage_{cleaned}" if cleaned else "stage"
    if keyword.iskeyword(cleaned) or keyword.issoftkeyword(cleaned):
        cleaned = f"{cleaned}_stage"

    used = set(taken) | _RESERVED
    if cleaned not in used:
        return cleaned
    for suffix in range(2, 1000):
        candidate = f"{cleaned}_{suffix}"
        if candidate not in used:
            return candidate
    msg = f"could not derive a unique cell name for stage {stage_id!r}"
    raise ScaffoldError(msg)


def _satellite_names(name: str) -> tuple[str, ...]:
    """The names a checkpoint claims beyond the stage name itself.

    A checkpoint scaffolds to two cells, not one: a cell called ``<name>_ui`` that defines
    ``<name>_decision`` and ``<name>_note``, and a gate cell called ``<name>``. So it is those
    three derived names that have to be unique across the notebook as well, not only the stage
    names. A plan with a checkpoint ``review`` beside a stage ``review_decision`` would
    otherwise scaffold two cells defining one name, which marimo rejects as multiply defined
    and which reaches the user as a notebook that will not open.
    """
    return (f"{name}_ui", f"{name}_decision", f"{name}_note")


def _claimed_names(stage: Stage, name: str) -> tuple[str, ...]:
    """Every name this stage's cells define beyond the stage name itself.

    A union rather than a choice, because a stage is allowed to be more than one of these at
    once. A checkpoint that declares a hand-in of its own scaffolds the three receiver cells
    *and* the approval pair, so it claims both sets of derived names. Reserving only the last
    match -- which is what a chain of reassignments did -- left a checkpoint's ``<name>_ui``,
    ``<name>_decision`` and ``<name>_note`` unreserved the moment it also carried a hand-in, so
    a plan with a checkpoint ``verify`` beside a stage ``verify_decision`` would scaffold two
    cells defining one name. marimo rejects that as multiply defined, and the user meets it as a
    notebook that will not open.
    """
    claimed: tuple[str, ...] = ()
    if stage.is_checkpoint:
        claimed += _satellite_names(name)
    if stage.is_handoff:
        claimed += _handoff_satellites(name)
    if _named_handin(stage) is not None:
        claimed += _handin_satellites(name)
    return claimed


def _name_map(plan: ProcessPlan) -> dict[str, str]:
    """Map every stage id to its cell name, assigned in emission order."""
    names: dict[str, str] = {}
    used: set[str] = set()
    for stage in plan.ordered_stages():
        name = cell_name_for(stage.id, used)
        while any(taken in used for taken in _claimed_names(stage, name)):
            name = cell_name_for(name, used | {name})
        used.update(_claimed_names(stage, name))
        names[stage.id] = name
        used.add(name)
    return names


# =============================================================================
# BUILDING
# =============================================================================


def build_cells(
    plan: ProcessPlan,
    *,
    handins_dir: Path | None = None,
    workbook_path: Path | None = None,
    contract_path: Path | None = None,
    allow_unapproved: bool = False,
) -> list[ScaffoldCell]:
    """Turn an approved plan into the cells that implement it.

    Pure: builds and returns cell bodies without touching a notebook, so the CLI can preview a
    scaffold and tests can assert on it with no live kernel.

    Args:
        plan: The approved plan.
        handins_dir: Where hand-ins are persisted. Defaults to ``handins`` relative to the
            notebook.
        workbook_path: The workbook the notebook was converted from, whose cached values are
            the reconciliation baseline. Defaults to the standard workspace layout — the plan's
            workbook filename beside the project directory. Wrong is survivable: the
            reconciliation cell reports "not reconciled" rather than guessing.
        contract_path: Where the hand-in contract lives. Defaults to ``contract.yaml`` in the
            project directory. It need not exist.
        allow_unapproved: Render a preview of an unapproved plan. This writes nothing;
            :func:`sync_notebook` has no equivalent escape hatch, so an unapproved plan can be
            looked at but never scaffolded.

    Returns:
        The cells in creation order: fixed head, stages in dependency order, fixed tail.

    Raises:
        PlanNotApprovedError: when the plan is not approved and ``allow_unapproved`` is not set.
        ScaffoldError: when a generated cell would breach the house rules or would not parse.
    """
    if not plan.approval.approved and not allow_unapproved:
        raise _not_approved(plan)

    names = _name_map(plan)
    checkpoints = {stage.id for stage in plan.stages if stage.is_checkpoint}
    gated = _gate_map(plan, names)
    cells: list[ScaffoldCell] = list(
        _head_cells(plan, names, checkpoints, handins_dir, workbook_path, contract_path)
    )

    ordered = plan.ordered_stages()
    for index, stage in enumerate(ordered, start=1):
        # A stage that reads a hand-in of its own gets the three receiver cells first, and its
        # own cell then computes on the frame they define. Emitted before the stage rather than
        # instead of it, so a stage that both receives and transforms keeps both -- and asked
        # ahead of the kind branches, because *any* kind may declare one.
        #
        # This used to sit below the checkpoint branch, which `continue`d past it. A `kind:
        # checkpoint` stage declaring `{origin: handin, ref: ...}` therefore got no selector, no
        # receipt and no frame: the source was read, validated, rendered on the approval card,
        # and then dropped. The shape that needed it most was the one shape that could not have
        # it -- a runbook's re-extract arrives precisely at the checkpoint that asks "did the
        # update do what we predicted?", and a real hub conversion stopped dead at that step
        # with the file it was waiting for having nowhere to go.
        label = _named_handin(stage)
        if label is not None:
            cells.extend(_handin_cells(stage, index, len(ordered), names[stage.id], label, gated))
        if stage.is_checkpoint:
            cells.extend(_checkpoint_cells(stage, index, len(ordered), names, gated))
            continue
        if stage.is_handoff:
            cells.extend(_handoff_cells(stage, index, len(ordered), names, gated))
            continue
        cells.append(_stage_cell(stage, index, len(ordered), names, checkpoints))

    cells = _with_reconciliation(cells, _tail_cells(plan, names, checkpoints), ordered, names)

    for cell in cells:
        _check_house_rules(cell)
    logger.info("built %d cell(s) from plan v%d for %s", len(cells), plan.version, plan.workbook)
    return cells


def _gate_map(plan: ProcessPlan, names: dict[str, str]) -> dict[str, str]:
    """Stage id to the token a downstream cell must read to be allowed to render.

    Two kinds of stage produce one. A checkpoint's token is its decision record; a mutating
    hand-off's is its confirmation. Both mean the same thing to the cell that reads it -- *a
    person has done something, and until they had, you were not to be shown* -- which is why one
    map covers both rather than the scaffolder branching on kind at every use.
    """
    gates: dict[str, str] = {}
    for stage in plan.stages:
        if stage.is_checkpoint:
            gates[stage.id] = names[stage.id]
        elif stage.is_handoff and stage.effective_handoff().needs_confirmation:
            gates[stage.id] = f"{names[stage.id]}_confirmed"
    return gates


def _with_reconciliation(
    cells: list[ScaffoldCell],
    tail: list[ScaffoldCell],
    ordered: Sequence[Stage],
    names: dict[str, str],
) -> list[ScaffoldCell]:
    """Put the reconciliation panel after the last stage it can report on, not at the very end.

    It used to be the last cell in the file, and in a pipeline that is right -- everything is
    computed by then. In a *runbook* it is wrong twice over.

    It renders too early and in the wrong place. The panel depends only on the computing stages,
    so marimo runs it as soon as those are done -- which means a user blocked at an approval
    three steps earlier gets a wall of reconciliation output directly beneath the sentence
    telling them what to type, including regions reported as NOT RECONCILED because the steps
    that would fill them have not happened yet. That reads as a broken notebook.

    And it is in the wrong place *for what it is*. Reconciliation against the workbook's own
    cached values is exactly the evidence somebody wants in front of them when they approve a
    change: the arithmetic matches the spreadsheet this replaced, now decide. Putting it before
    the checkpoint makes it an argument; leaving it at the bottom makes it a footnote nobody
    reaches until the decision is already made.

    So it is emitted immediately after the last stage that declares any analysis operations --
    the last stage it could possibly have something to say about. A plan whose stages declare no
    operations keeps the old position, because there is nothing to place it relative to.
    """
    last = next(
        (
            stage.id
            for stage in reversed(ordered)
            if stage.operations and not stage.generates_no_code
        ),
        None,
    )
    if last is None:
        return [*cells, *tail]
    anchor = names[last]
    position = next(
        (index for index, cell in enumerate(cells) if cell.name == anchor),
        len(cells) - 1,
    )
    return [*cells[: position + 1], *tail, *cells[position + 1 :]]


def _check_house_rules(cell: ScaffoldCell) -> None:
    """Refuse a cell that would not compile, or that breaks the one rule about pandas.

    Both are the scaffolder marking its own homework rather than trusting itself. A cell body is
    assembled from free-form plan text, and a cell that will not parse reaches the user as a
    notebook that does not open — far worse than a refusal here naming the cell.
    """
    if _BANNED_IMPORT.search(cell.code):
        msg = (
            f"cell {cell.name!r} would import pandas, which kedge does not permit anywhere. "
            f"Generated code is polars (PLAN 2.5)"
        )
        raise ScaffoldError(msg)
    try:
        ast.parse(cell.code)
    except SyntaxError as exc:
        msg = (
            f"cell {cell.name!r} would not parse: {exc.msg} at line {exc.lineno}. "
            f"This is a scaffolder bug, not a plan error; nothing was written."
        )
        raise ScaffoldError(msg) from exc


def _not_approved(plan: ProcessPlan) -> PlanNotApprovedError:
    blockers = plan.approval_blockers()
    detail = "\n" + "\n".join(f"  - {item}" for item in blockers) if blockers else ""
    return PlanNotApprovedError(
        f"plan v{plan.version} for {plan.workbook} is '{plan.approval.state.value}', not "
        f"'approved'. Nothing is written to the notebook before the plan is approved."
        f"{detail}\nReview the plan, then approve it."
    )


# =============================================================================
# SYNCING AN ALREADY-SCAFFOLDED NOTEBOOK
# =============================================================================


async def sync_notebook(
    plan: ProcessPlan,
    driver: CellSyncer,
    *,
    previous: ProcessPlan | None = None,
    handins_dir: Path | None = None,
    workbook_path: Path | None = None,
    contract_path: Path | None = None,
) -> SyncResult:
    """Bring a live notebook into line with an approved plan, without destroying anything.

    The scaffolder this replaced created every cell unconditionally, which is right exactly once
    and wrong every time after. It made the scaffold a thing that happened during the open
    sequence and nowhere else: the second open of a scaffolded workbook died on ``CellNameError``
    at the first cell and reported the whole plan as unscaffolded, and approving a plan in the
    chat was a decision with no visible effect — the plan landed on disk and the notebook beside
    it stayed empty until the user closed the workbook and opened it again. This is the same
    emission, indifferent to what is already in the notebook.

    **Nothing is overwritten unless kedge wrote it and nobody has touched it since.** That is the
    whole rule, and it is the rule because by the time a plan is amended the stage cells have
    usually been translated: the scaffolded body is a documented passthrough with a
    ``TODO(kedge)`` in it, and the body beside it a week later is the user's and the agent's work.
    A cell whose source differs from both what the plan says now and what the plan said before is
    reported as diverged and left exactly as it is. There is no parameter that talks this out of
    it, for the same reason there is none for approval.

    **Nothing is deleted, ever.** A stage that has left the plan leaves its cell behind, named in
    :attr:`SyncResult.obsolete`. Deleting a cell is a decision the user takes explicitly through
    the pending-deletion gate (``kedge.agent.tools``), and a plan edit is not consent to lose the
    code that implemented the old one.

    Args:
        plan: The approved plan the notebook should implement.
        previous: The plan the notebook was last built from, where the caller knows it. Its
            emission is what "kedge wrote this and nobody has touched it" is measured against, so
            without it an unchanged scaffolded cell that the new plan would word differently is
            reported as diverged rather than updated. Safe to omit; never required.
        driver: The notebook driver, or anything satisfying :class:`CellSyncer`.
        handins_dir: Where hand-ins are persisted.
        workbook_path: The workbook holding the reconciliation baseline.
        contract_path: Where the hand-in contract lives.

    Returns:
        What happened to every cell the plan calls for, in emission order.

    Raises:
        PlanNotApprovedError: when ``plan.approval.state`` is not ``approved``.
        ScaffoldError: when the plan will not build into cells at all. A *single* cell the kernel
            refuses does not raise -- it is recorded as ``refused`` and the rest are still
            written, because a notebook that is nine tenths in line with the plan is worth more
            than one that gave up at the first name collision.
    """
    if not plan.approval.approved:
        raise _not_approved(plan)

    target = build_cells(
        plan,
        handins_dir=handins_dir,
        workbook_path=workbook_path,
        contract_path=contract_path,
    )
    pristine = _previous_bodies(
        previous,
        handins_dir=handins_dir,
        workbook_path=workbook_path,
        contract_path=contract_path,
    )

    # `with_code=True` deliberately. Reading a cell's source records a read at its current
    # version, which is exactly what marimo's staleness guard wants before the edits below, and
    # it is the only way to tell a cell kedge wrote from a cell somebody has since rewritten.
    existing = {cell.name: cell for cell in await driver.list_cells(with_code=True) if cell.name}

    # Seeded from the listing and added to as cells are created, because the cell a new one should
    # sit behind is as likely to be one this same sync just wrote -- an amendment adding two
    # consecutive stages -- as one that was already there. Anchoring against the opening snapshot
    # alone put every cell of a first scaffold at the end, in order by luck rather than by
    # placement, and would have put the second of two new stages under the reconciliation tail.
    placed = set(existing)
    results: list[CellSync] = []
    anchor: str | None = None
    for cell in target:
        results.append(await _sync_one(cell, driver, existing, pristine, placed, after=anchor))
        anchor = cell.name

    obsolete = tuple(
        name for name in pristine if name in existing and name not in {cell.name for cell in target}
    )
    result = SyncResult(cells=tuple(results), obsolete=obsolete)
    logger.info("synced the notebook to plan v%d: %s", plan.version, result.summary(plan.version))
    return result


async def _sync_one(
    cell: ScaffoldCell,
    driver: CellSyncer,
    existing: dict[str, CellSource],
    pristine: dict[str, str],
    placed: set[str],
    *,
    after: str | None,
) -> CellSync:
    """Create, update or leave alone one cell, and say which of the three it was.

    ``after`` places a newly created cell behind the one the plan emits before it, so a stage
    added by an amendment lands where the plan puts it rather than beneath the reconciliation
    tail. It is dropped when that neighbour is not in the notebook, because marimo appends in
    that case anyway and position is presentation: execution order is the dataflow graph.
    """
    current = existing.get(cell.name)
    if current is None:
        try:
            await driver.create_cell(
                cell.code,
                name=cell.name,
                after=after if after in placed else None,
                hide_code=False,
            )
        except KedgeError as exc:
            return CellSync(cell.name, "refused", str(exc))
        placed.add(cell.name)
        return CellSync(cell.name, "created")

    if current.code == cell.code:
        return CellSync(cell.name, "unchanged")
    if current.code != pristine.get(cell.name):
        return CellSync(
            cell.name,
            "diverged",
            "the cell in the notebook is not the one kedge wrote, so it was left alone",
        )

    try:
        await driver.edit_cell(cell.name, cell.code, run=True)
    except KedgeError as exc:
        # StaleCellError lands here when the user edited the cell between the listing above and
        # this call. That is a divergence found a moment late, and it is reported as one.
        return CellSync(cell.name, "refused", str(exc))
    return CellSync(cell.name, "updated")


def _previous_bodies(
    previous: ProcessPlan | None,
    *,
    handins_dir: Path | None,
    workbook_path: Path | None,
    contract_path: Path | None,
) -> dict[str, str]:
    """What the superseded plan would have emitted, keyed by cell name.

    ``allow_unapproved`` because the caller is asking what this plan *renders as*, not writing it
    anywhere -- and a plan whose approval was later withdrawn still explains the cells that were
    scaffolded from it while it stood.

    A previous plan that will not build is not an error here. It only costs the sync its evidence
    that a cell is untouched, so every difference is reported as a divergence and left alone --
    which is the safe direction, and the same answer as passing no previous plan at all.
    """
    if previous is None:
        return {}
    try:
        return {
            cell.name: cell.code
            for cell in build_cells(
                previous,
                handins_dir=handins_dir,
                workbook_path=workbook_path,
                contract_path=contract_path,
                allow_unapproved=True,
            )
        }
    except (ScaffoldError, KedgeError) as exc:
        logger.info("could not render plan v%d to compare against: %s", previous.version, exc)
        return {}


# =============================================================================
# THE FIXED HEAD (PLAN 2.8)
# =============================================================================

_SETUP_TEMPLATE = """# Generated by kedge from process plan v{version} for {workbook}.
# These cells are yours to edit. kedge writes them; it does not own them.
import datetime
import pathlib

import marimo as mo
import polars as pl

# kedge's own packages. `import kedge.x` binds exactly one name -- `kedge` -- so this cell owns
# the whole surface under marimo's single-definition rule, and every cell below reads the way
# the package docstrings do: kedge.ingest.receive(...), kedge.contracts.validate(...),
# kedge.reconcile.reconcile_panel(...).
import kedge
import kedge.contracts
import kedge.ingest
import kedge.reconcile

# One JSON file per pass through this process: which hand-ins were consumed, what was
# approved, what was confirmed as run, and when. marimo's own state dies with the kernel, so
# this is what lets somebody close the notebook halfway through and come back to it -- and it
# is what the sign-off's audit line is derived from rather than asserted.
import kedge.runs

# Statements a hand-off cell hands the user are rendered here and nowhere else. kedge runs
# none of them; what this buys is literals that are correct -- an apostrophe in a
# counterparty name, a null, a date, a money value at the edge of exponent notation. Every
# one of those breaks a statement built by concatenation, at the point somebody is about to
# run it against production.
import kedge.sql

# Importing kedge.xl registers the Excel-semantics expression namespace: col("x").xl.round(2),
# .xl.add, .xl.div. Excel and polars disagree about rounding at .5, empty cells in arithmetic,
# and division by zero. Every one of those disagreements produces silently wrong numbers rather
# than an error (PLAN 2.6), so translations state their intent through this namespace rather
# than open-coding the workaround where nobody can grep for it.
import kedge.xl

# The managed hand-in store: every file this notebook has consumed, hashed, dated and
# receipted, so "this run consumed this file" is a defensible claim (PLAN 2.8).
HANDIN_DIR = pathlib.Path({store!r})

# The workbook this notebook was converted from. Its cached values are the only baseline the
# reconciliation cell has, so if the workbook moves, correct the path here -- the panel reports
# "not reconciled" rather than guessing (PLAN 4.5).
WORKBOOK = pathlib.Path({workbook_path!r})

# Where the hand-in contract lives. It does not have to exist: with no contract the notebook
# still runs and the check cell says, in as many words, that nothing is enforced (PLAN M5).
CONTRACT_PATH = pathlib.Path({contract_path!r})

# Where this notebook's runs are recorded. One file per run, never overwritten.
RUNS_DIR = pathlib.Path({runs_dir!r})

# Whether this translation was ever reconciled against the workbook it came from. One record,
# written once and cited afterwards -- see the translation-check cell for why it is not a live
# comparison on every run.
ACCEPTANCE_PATH = pathlib.Path({acceptance_path!r})"""

_RUN_MODE_CELL = """# Which run is this? A runbook takes hours or days -- run the extract, go to a meeting, come
# back, get an approval, run the update the next morning. marimo's own state does not survive
# any of that: close the tab and every paste box is empty and every dropdown is unset.
#
# So the run is kept on disk (kedge.runs), and reopening the notebook resumes it. Starting
# fresh is a deliberate choice rather than what happens by accident, and it never erases
# anything -- a new run is written beside the old one, because last quarter's record is
# evidence.
kedge_run_mode = mo.ui.dropdown(
    options=["resume the run in progress", "start a new run"],
    value="resume the run in progress",
    label="Run",
)
kedge_run_mode"""

_RUN_CELL = """# The run this notebook is working through, resumed or started.
#
# Starting a new run is idempotent on purpose. This cell re-executes whenever anything above it
# changes, and a naive `if starting_fresh: start()` would mint a run every time -- so a fresh
# run is started only when the current one has something in it. Re-running then finds the new,
# empty run and resumes that instead.
KEDGE_RUNS = kedge.runs.RunStore(RUNS_DIR)
_latest = KEDGE_RUNS.latest()
_wants_fresh = kedge_run_mode.value == "start a new run"
if _latest is None or (_wants_fresh and (_latest.handins or _latest.decisions)):
    kedge_run = KEDGE_RUNS.start(workbook=WORKBOOK.name)
else:
    kedge_run = _latest
KEDGE_RUN_ID = kedge_run.run_id

_done = kedge_run.audit_lines()
_lines = [f"### Run `{KEDGE_RUN_ID}`"]
if _done:
    _lines.append(
        f"Resumed, started {kedge_run.started_at:%d %b %Y at %H:%M} UTC. "
        f"{len(kedge_run.handins)} hand-in(s) and {len(kedge_run.decisions)} decision(s) so far:"
    )
    _lines.append("")
    _lines.extend(f"- {_line}" for _line in _done)
    _lines.append("")
    _lines.append(
        "_Everything below picks up from here. Choose **start a new run** above to begin "
        "again; this record is kept either way._"
    )
else:
    _lines.append("New run. Work down the page: each step appears once the one above it is done.")
mo.md("\\n".join(_lines))"""

_SOURCE_CELL = """# The hand-in selector. Swapping the file here re-runs everything below it in dataflow
# order, the contract re-validates, and the reconciliation panel goes green or red. That is
# the moment the conversion pays off, and it is precisely what the Excel process cannot do
# (PLAN 2.8).
#
# Three entry points that return different things. The drop zone holds the whole file in
# kernel memory and has no path, so an uploaded hand-in is NOT reproducible tomorrow; the
# browser returns a path and no bytes; the paste box returns text and no file at all. They
# converge in the next cell, where kedge.ingest.receive turns all three into one record.
#
# The paste box is there because the step before this one is usually "run this query", and
# what comes back is a result grid on the clipboard rather than a file on disk. Requiring a
# save-as first is friction with nothing behind it.
handin_drop = mo.ui.file(kind="area", label="Drop the hand-in here")
handin_pick = mo.ui.file_browser(multiple=False, label="...or select one on this machine")
handin_paste = mo.ui.text_area(
    label="...or paste the result grid, header row included",
    placeholder="trade_id,counterparty,notional -- or tab-separated, straight from SSMS",
    full_width=True,
    rows=8,
)
handin_source = mo.ui.tabs(
    {"Drop": handin_drop, "Select": handin_pick, "Paste": handin_paste}
)
handin_source"""

_HANDIN_STEP = "handin"
"""The step id the notebook's own hand-in is recorded against in the run.

A constant because two cells have to agree on it and they are two string literals in two
templates. It is the cell's name, which is the only thing about the head hand-in that is stable
-- there is no stage id to use, because the head hand-in belongs to the notebook rather than to
any one step of the process.
"""


def _handin_cell_code(blocking: str) -> str:
    """The head hand-in's body, with the message it renders when it is waiting.

    A function rather than a constant for one interpolation, and that interpolation is the point.
    This was the only ``mo.stop`` in the scaffold that did not name its step -- it said "Waiting
    for a hand-in", full stop -- because the head hand-in belongs to the notebook rather than to
    any one stage and there was no number to quote. But in app mode a stopped cell is the entire
    user interface, and the rule the rest of the file follows exists precisely for that moment:
    a page that says "waiting" and a page that has died look identical. The step it is waiting
    *for* is knowable -- it is the first stage that reads the frame -- so it is quoted.
    """
    return _HANDIN_TEMPLATE.replace("{{BLOCKING}}", repr(blocking))


_HANDIN_TEMPLATE = f"""# All three entry points converge here, and kedge.ingest.receive does the whole job: a
# dropped file's bytes are written into the managed store, a selected path is copied into it,
# pasted text is sniffed for its delimiter and normalised to CSV, every one of them is hashed
# and deduplicated against what is already there, and a receipt is recorded. `handin` is a
# HandIn record whose `path` is always the managed copy and never the transient upload --
# which is what makes this notebook re-runnable tomorrow, when the uploaded bytes are gone
# and the clipboard holds something else entirely.
#
# `previous_handin` is what makes reopening the notebook work at all. marimo's state dies with
# the kernel: come back tomorrow and the paste box is empty and the file browser is unset. If
# this run already took a hand-in here, and the managed copy is still on disk and still hashes
# the same, it is used and the user is not asked again -- otherwise the third cell of every
# runbook blocks on a file the run record is already holding, and everything below it,
# decisions included, waits behind that. A file that has changed underneath is not reused:
# resuming onto different data silently is worse than asking twice.
_resumed = kedge.runs.previous_handin(KEDGE_RUNS, KEDGE_RUN_ID, {_HANDIN_STEP!r})
_pasted = (handin_paste.value or "").strip()
mo.stop(
    _resumed is None
    and not handin_drop.value
    and not handin_pick.value
    and not _pasted,
    mo.md({{{{BLOCKING}}}}),
)

# Precedence is by reproducibility, most reproducible first. A selected path is a file that
# still exists; dropped bytes are a file that existed; a paste is neither, and is last because
# leaving a stale paste in the box must not override a file the user has just chosen. The
# resumed file comes last of all: supplying one again is how a user corrects an extract they
# got wrong.
handin = kedge.ingest.receive(
    handin_pick.value
    or handin_drop.value
    or (kedge.ingest.Paste(text=_pasted) if _pasted else None)
    or _resumed,
    store_dir=HANDIN_DIR,
)
kedge.runs.record_handin(KEDGE_RUNS, KEDGE_RUN_ID, {_HANDIN_STEP!r}, handin)
mo.md(f"**Hand-in** `{{handin.audit_line()}}`")"""

_CONTRACT_CELL = """# The contract, loaded once and read by all three cells below it: the shape profile, the check,
# and the frame every stage computes on. *Which sheet and which header row* is part of the
# agreement, so all three have to read the same rows -- a contract that goes green against 500
# rows of 'Data' while the notebook computes on the cover sheet is worse than no contract at
# all, because it looks controlled and is not.
#
# Loading a contract is not checking a hand-in against it, so this does not put the contract
# ahead of the drift panel. PLAN 2.8 is about what gets reported first, and drift still does.
#
# No contract is not an error. It is the state every new notebook starts in, and everything
# below degrades to saying so rather than pretending anything was enforced. Draft one from a
# real hand-in with `kedge contract infer`, save it at CONTRACT_PATH, and nothing else changes.
handin_contract = None
_problem = None
if CONTRACT_PATH.is_file():
    try:
        handin_contract = kedge.contracts.load(CONTRACT_PATH)
    except kedge.ContractError as _error:
        _problem = str(_error)

_lines = ["### Contract"]
if handin_contract is not None:
    _sheet = f"sheet `{handin_contract.sheet}`" if handin_contract.sheet else "the first sheet"
    _header = (
        "the detected header row"
        if handin_contract.header_row is None
        else f"header row {handin_contract.header_row}"
    )
    _lines.append(
        f"`{handin_contract.name}`, from `{CONTRACT_PATH}`. Everything below reads {_sheet}, "
        f"{_header}, with any preamble above it skipped and any totals row excluded."
    )
elif _problem is not None:
    # A contract that exists but will not load is worse than no contract at all, for the same
    # reason. It does not block -- a typo in the YAML should not put the data out of reach --
    # but it says so at the top of the panel.
    _lines.append(f"**The contract at `{CONTRACT_PATH}` could not be loaded.** {_problem}")
    _lines.append("")
    _lines.append("_Nothing below is enforced until this is fixed._")
else:
    _lines.append(f"_No contract at_ `{CONTRACT_PATH}`_, so nothing below is enforced._")
    _lines.append("")
    _lines.append(f"Draft one: `kedge contract infer {handin.path} --out {CONTRACT_PATH}`")
mo.md("\\n".join(_lines))"""

_DRIFT_CELL = """# What arrived, and how it differs from last time -- reported BEFORE the contract check.
# "column EAD became EAD_GBP" is a far more useful message than a schema-validation traceback
# three checks deep, and it is the difference between a two-minute fix and an afternoon. Seeing
# the shape before any processing happens is the other half of PLAN 2.8.
#
# Profiled off the sheet and header row the contract names, so the shape being diffed is exactly
# the shape being checked. With no contract that is the first sheet with a detected header, which
# is what the check would read too.
#
# check_drift stores the new profile but deliberately does not accept it: accepting is a
# decision, made by calling kedge.ingest.accept_profile(HANDIN_DIR, handin_profile) once
# somebody has read what changed.
handin_profile = None
handin_drift = None
_problem = None
try:
    handin_profile, handin_drift = kedge.ingest.check_drift(
        handin,
        store_dir=HANDIN_DIR,
        sheet=handin_contract.sheet if handin_contract is not None else None,
        header_row=handin_contract.header_row if handin_contract is not None else None,
    )
except kedge.IngestError as _error:
    _problem = str(_error)

_lines = ["### Hand-in shape"]
if handin_drift is None:
    _lines.append(f"**Could not profile the hand-in.** {_problem}")
else:
    # Capped, because a 200-column extract renders as a wall rather than as information.
    # The whole profile is bound as `handin_profile` either way.
    _shown = handin_profile.columns[:24]
    _more = handin_profile.column_count - len(_shown)
    _lines.append(
        f"{handin_profile.row_count:,} rows, {handin_profile.column_count} columns: "
        + ", ".join(f"`{_column.header or _column.column}` {_column.dtype}" for _column in _shown)
        + (f", and {_more} more -- see `handin_profile`" if _more else "")
    )
    _lines.append("")
    _lines.append(handin_drift.summary_line())
    _lines.append("")
    _lines.extend(
        f"- **{_item.severity.value}** `{_item.column or '-'}` -- {_item.message}"
        for _item in handin_drift.items
    )
mo.md("\\n".join(_lines))"""

_CHECK_CELL = """# The contract check: the first thing that runs after ingestion, and what turns "a notebook"
# into "a controlled process". A hand-in that is not what was agreed is rejected before a single
# number is computed (PLAN 2.8, M5). When this fails, read the shape panel above first -- it
# usually explains the failure in one line.
handin_check = None
_problem = None
if handin_contract is not None:
    try:
        handin_check = kedge.contracts.validate(handin, handin_contract)
    except (kedge.ContractError, kedge.IngestError) as _error:
        _problem = str(_error)

_lines = ["### Contract check"]
if handin_check is not None:
    _lines.append(handin_check.summary_line())
    _lines.append("")
    _lines.extend(
        f"- **{_check.status.value}** `{_check.column or '-'}` {_check.check}: {_check.message}"
        for _check in (*handin_check.failures, *handin_check.warnings)
    )
elif _problem is not None:
    _lines.append(f"**The contract could not be applied to this hand-in.** {_problem}")
    _lines.append("")
    _lines.append("_Nothing below is enforced until this is fixed._")
else:
    _lines.append("_No contract loaded, so nothing was checked. See the panel above._")
mo.md("\\n".join(_lines))"""

_FRAME_CELL = """# The contract gates the data. mo.stop halts this cell and every cell downstream of it in the
# dataflow graph, so a hand-in that failed its contract cannot reach a single calculation. A
# notebook with no contract configured passes straight through: handin_check is None, and None
# is not a failure (PLAN 2.8).
mo.stop(
    handin_check is not None and not handin_check.ok,
    mo.md(
        "**Blocked: the hand-in does not satisfy its contract.** Fix the hand-in, correct the "
        "contract, or remove it if it no longer describes what this process receives."
    ),
)

# read_data is the one reader that profiling, contract validation and contract inference all go
# through, and this is the identical call kedge.contracts.validate makes: the contract's own
# sheet and header row, preamble above it skipped, a trailing totals row excluded. That the two
# calls are the same call is the whole point -- read the file any other way and the frame the
# notebook computes on is not the frame that was validated, so the green panel above becomes a
# statement about rows nobody used. Mirroring these arguments here instead would put the two one
# refactor apart, which is exactly how that bug gets in.
_data, _layout = kedge.ingest.read_data(
    handin.path,
    sheet=handin_contract.sheet if handin_contract is not None else None,
    header_row=handin_contract.header_row if handin_contract is not None else None,
)

# LazyFrame from here on. Nothing is materialised again until a boundary asks for it, which is
# what keeps the notebook's cell graph and polars' query plan aligned (PLAN 2.5).
handin_frame = _data.lazy()

# Say what had to be skipped to reach the data, because a row count that does not match the
# file is otherwise a mystery the user has to solve twice.
_notes = _layout.notes()
mo.vstack(
    [mo.md("_Layout: " + "; ".join(_notes) + "._"), handin_frame] if _notes else [handin_frame]
)"""


def _fixed_paths(
    plan: ProcessPlan,
    handins_dir: Path | None,
    workbook_path: Path | None,
    contract_path: Path | None,
) -> dict[str, str]:
    """The four filesystem constants the head cells close over.

    Each default assumes the standard workspace layout -- a ``<workbook>.kedge`` directory
    beside the workbook, with ``handins`` and ``runs`` inside it -- and each is emitted as a
    plain constant rather than something computed at run time, so a user whose files have moved
    corrects one line instead of reading the scaffolder.
    """
    store = handins_dir if handins_dir is not None else Path("handins")
    project = store.parent
    return {
        "store": str(store),
        "runs_dir": str(project / "runs"),
        "acceptance_path": str(project / "reconciliation.json"),
        "workbook_path": str(
            workbook_path if workbook_path is not None else project.parent / plan.workbook
        ),
        "contract_path": str(
            contract_path if contract_path is not None else project / "contract.yaml"
        ),
    }


def _briefing_cell(plan: ProcessPlan) -> ScaffoldCell:
    """What this process is for, and what the conversion of it left unsettled.

    The first cell in the notebook, because it is the first thing anybody opening it needs. A
    converted notebook outlives the person who converted it: eight months on, the workbook it
    came from has been superseded, the analyst who wrote the Sign-off tab has moved teams, and
    what is left is a page of polars that reproduces a calculation nobody can explain the reason
    for. The workbook *did* explain it -- Purpose, Background, Known issues, written by somebody
    for exactly this reader -- and the conversion used to discard every word.

    Two halves, and they are reliable in different ways.

    The **business half** comes from :class:`~kedge.plan.model.Briefing`, which the model fills
    from the workbook's own documentation and which refuses to hold prose without citing where
    it came from. Rendered with those citations attached, so a reader can weigh a sentence
    rather than take it on trust.

    The **conversion half** is derived here from the plan, with no model anywhere near it: which
    workbook, which plan version, what was dropped and acknowledged, what the plan asked that
    nobody answered, and which stages were translated at low confidence. That is the part a
    reader most needs and is least likely to be told -- an unanswered open question is a caveat
    on every number below it, and it should not be discoverable only by opening a YAML file.

    In an accordion, collapsed. It is orientation, not instruction: essential once, then in the
    way. The unanswered questions are pulled out above it, because those are not orientation --
    they qualify the output every time it runs.
    """
    briefing = plan.briefing
    unanswered = [question for question in plan.open_questions if not question.answer]

    body: list[str] = []
    if briefing is not None and not briefing.is_empty:
        if briefing.purpose:
            body.extend(["**What this is for**", "", briefing.purpose, ""])
        if briefing.background:
            body.extend(["**Why it exists**", "", briefing.background, ""])
        for label, value in (
            ("How often", briefing.cadence),
            ("Who relies on it", briefing.audience),
        ):
            if value:
                body.extend([f"**{label}**", "", value, ""])
        if briefing.watch_for:
            body.extend(["**Watch for**", ""])
            body.extend(f"- {item}" for item in briefing.watch_for)
            body.append("")
        if briefing.sources:
            body.extend(
                [
                    "_Taken from " + "; ".join(briefing.sources) + "._",
                    "",
                ]
            )
    else:
        body.extend(
            [
                "The workbook this was converted from carried no description of what the "
                "process is for, so none is offered here. That is a gap in the original, not "
                "something kedge can fill in: an invented rationale in a finance notebook is "
                "worse than none, because the next reader cannot tell it from the real thing.",
                "",
            ]
        )

    body.extend(["**Where this came from**", ""])
    approved = f", approved {plan.approval.at:%d %b %Y}" if plan.approval.at is not None else ""
    body.append(f"- Converted from `{plan.workbook}` by kedge, plan v{plan.version}{approved}.")
    if plan.summary:
        body.append(f"- {plan.summary}")
    for dropped in plan.dropped:
        body.append(f"- Dropped `{dropped.range}`: {dropped.reason}")
    low = [stage.id for stage in plan.stages if stage.needs_review_marker]
    if low:
        body.append(
            f"- Translated at low or unstated confidence, and marked in the cells themselves: "
            f"{', '.join(low)}."
        )

    lines = [
        "# What this process is, and what the conversion of it left open. Read once; it is",
        "# collapsed because it is orientation rather than instruction.",
        "#",
        "# The prose comes from the workbook's own documentation and carries its citations. The",
        "# provenance below it is derived from the approved plan, so it cannot drift from what",
        "# was actually agreed.",
    ]
    if unanswered:
        # Not in the accordion. An unanswered question is a caveat on every number below it, and
        # a caveat nobody opens is a caveat nobody has.
        lines.extend(
            [
                "_open = [",
                *(f"    {question.question!r}," for question in unanswered),
                "]",
            ]
        )
    lines.extend(
        [
            f"kedge_briefing = mo.accordion({{{f'About this process: {plan.workbook}'!r}: mo.md(",
            f"    {chr(10).join(body)!r}",
            ")})",
        ]
    )
    if unanswered:
        lines.extend(
            [
                "mo.vstack(",
                "    [",
                "        mo.md(",
                '            "**Questions this conversion raised and nobody has answered.** They '
                'apply to "',
                '            "every number below."',
                "        ),",
                '        mo.md("\\n".join(f"- {_q}" for _q in _open)),',
                "        kedge_briefing,",
                "    ]",
                ")",
            ]
        )
    else:
        lines.append("kedge_briefing")
    return ScaffoldCell(name="kedge_briefing", code="\n".join(lines), role="setup")


def head_handin_is_read(
    plan: ProcessPlan, names: dict[str, str] | None = None, checkpoints: set[str] | None = None
) -> bool:
    """Whether anything in this plan reads the notebook's own hand-in.

    The head hand-in of PLAN 2.8 assumes a process with **one** input: a workbook that is handed
    a file, computes on it, and reports. For that shape it is exactly right, and it is the shape
    almost every plan written before hand-offs existed had.

    A runbook is not that shape. ``extract, adjust, re-extract, sign off`` takes its inputs at
    the points the process reaches them, each declared on the stage that reads it
    (:func:`_named_handin`), each with its own selector further down the page. Emitting the head
    as well asked the user for a file no step of the process names, and its ``mo.stop`` halted
    the whole notebook until they supplied one -- so the first thing a runbook did was block on
    an input it had no use for. Worse, the reconciliation panel cited *its* digest to
    :func:`kedge.reconcile.check_translation`, which is what decides whether a later run may
    re-compare itself against the workbook: keyed to a file that takes no part in the
    computation, that decision is meaningless.

    So it is emitted when something reads it and left out when nothing does, which is the
    narrowest rule that fixes both. "Reads it" is deliberately generous, because the stage
    bodies are holes somebody has still to fill: a stage whose ``sources`` name the hand-in
    counts even if its scaffolded passthrough happens not to mention the frame yet.

    Args:
        plan: The plan being scaffolded.
        names: Stage id to cell name, as :func:`_name_map` assigns them. Computed when absent.
        checkpoints: The ids of the checkpoint stages. Computed when absent.

    Returns:
        Whether ``handin_frame`` -- or a stage source naming the notebook's own hand-in -- is
        read by anything the plan calls for.
    """
    return head_handin_reader(plan, names, checkpoints) is not None


def head_handin_reader(
    plan: ProcessPlan, names: dict[str, str] | None = None, checkpoints: set[str] | None = None
) -> Stage | None:
    """The first stage that reads the notebook's own hand-in, or ``None`` when nothing does.

    :func:`head_handin_is_read` is this question asked as a yes or no, and everything about *why*
    is documented there. The stage itself is wanted for one reason: the head hand-in's ``mo.stop``
    has to name the step it is waiting for, and this is the step.

    Note what a caller must not read into the answer. A stage reaching the head hand-in is often
    a **fall-through** rather than a declaration -- ``_upstream_name`` returns ``handin_frame``
    when nothing else matched -- so this names the stage that will consume the file, not
    necessarily one that asked for it.

    A checkpoint is skipped by the branch below, and it stays skipped now that a checkpoint may
    carry a hand-in of its own: :func:`_checkpoint_cells` renders a decision, and where the stage
    declares a hand-in of its own, :func:`_handin_cells` gives it ``<name>_frame`` under its own
    name. Neither reads ``handin_frame``. Note that the branch *above* it does not skip: a stage
    declaring the bare ``{origin: handin}`` is the head hand-in's reader whatever kind it is, and
    a checkpoint may say so as legitimately as anything else.

    **What this does not promise is that a checkpoint's own hand-in keeps the head cells out.**
    ``_upstream_name`` never treats a checkpoint as a frame, so a stage whose only dependency is
    one falls through to ``handin_frame`` -- and then something genuinely does read the head
    hand-in, and it is emitted, correctly, beside the checkpoint's own file box. Two boxes for a
    process that declared one file is the plan's shape showing through rather than this walk's
    doing, and it is :func:`kedge.plan.review._stranded_handin_warnings` that says so on the
    approval card: move the file onto the stage that computes on it, and the fall-through and the
    second box both go with it.
    """
    resolved = names if names is not None else _name_map(plan)
    gates = (
        checkpoints
        if checkpoints is not None
        else {stage.id for stage in plan.stages if stage.is_checkpoint}
    )
    for stage in plan.ordered_stages():
        if any(source.origin is SourceOrigin.HANDIN and not source.ref for source in stage.sources):
            return stage
        if stage.is_checkpoint:
            continue
        if stage.is_handoff:
            handoff = stage.effective_handoff()
            # A generated hand-off with no resolvable `built_from` falls back to the head frame.
            if handoff.is_generated and (handoff.built_from or "") not in resolved:
                return stage
            continue
        if _upstream_name(stage, resolved, gates) == "handin_frame":
            return stage
    return None


def _head_cells(
    plan: ProcessPlan,
    names: dict[str, str],
    checkpoints: set[str],
    handins_dir: Path | None,
    workbook_path: Path | None,
    contract_path: Path | None,
) -> list[ScaffoldCell]:
    """Setup, briefing, run, then -- where the plan reads it -- the hand-in and its checks.

    The order is :data:`HEAD_CELL_NAMES` and the reasoning for it is there (PLAN 2.8). The four
    setup cells are unconditional: every notebook has a briefing and every notebook belongs to a
    run. The six hand-in cells are not, and :func:`head_handin_is_read` says why.
    """
    paths = _fixed_paths(plan, handins_dir, workbook_path, contract_path)
    cells = [
        ScaffoldCell(
            name="kedge_setup",
            role="setup",
            code=_SETUP_TEMPLATE.format(version=plan.version, workbook=plan.workbook, **paths),
        ),
        _briefing_cell(plan),
        ScaffoldCell(name="kedge_run_mode", role="setup", code=_RUN_MODE_CELL),
        ScaffoldCell(name="kedge_run", role="setup", code=_RUN_CELL),
    ]
    reader = head_handin_reader(plan, names, checkpoints)
    if reader is None:
        logger.info(
            "plan v%d declares every hand-in on a stage, so the fixed head hand-in is not emitted",
            plan.version,
        )
        return cells
    # Emitted, and said so. This used to happen silently, and it is most often a *fall-through*
    # -- no stage declared `origin: handin`, so `_upstream_name` reached the head frame because
    # nothing else matched. Six cells and a blocking `mo.stop` then arrived at the top of the
    # notebook for an input no step of the plan names, and nothing anywhere reported it. The
    # scaffold report is where a reader finds out.
    ordered = list(plan.ordered_stages())
    logger.info(
        "plan v%d reaches the notebook's own hand-in at stage %r (step %d of %d), so the fixed "
        "head hand-in is emitted",
        plan.version,
        reader.id,
        ordered.index(reader) + 1,
        len(ordered),
    )
    blocking = _waiting(
        ordered.index(reader) + 1,
        len(ordered),
        _one_line(reader.id),
        "Drop the file above, select one on this machine, or paste the grid. Nothing below "
        "this runs until it arrives.",
    )
    cells.extend(
        [
            ScaffoldCell(name="handin_source", role="handin", code=_SOURCE_CELL),
            ScaffoldCell(name="handin", role="handin", code=_handin_cell_code(blocking)),
            ScaffoldCell(name="handin_contract", role="handin", code=_CONTRACT_CELL),
            ScaffoldCell(name="handin_drift", role="handin", code=_DRIFT_CELL),
            ScaffoldCell(name="handin_check", role="handin", code=_CHECK_CELL),
            ScaffoldCell(name="handin_frame", role="handin", code=_FRAME_CELL),
        ]
    )
    return cells


# =============================================================================
# STAGES
# =============================================================================


def _stage_cell(
    stage: Stage,
    index: int,
    total: int,
    names: dict[str, str],
    checkpoints: set[str],
) -> ScaffoldCell:
    """One automated stage: a documented, named cell with the translation still to write.

    The body is a passthrough rather than a ``raise NotImplementedError`` so that the notebook
    runs end to end from the moment it is scaffolded and the hand-in machinery can be exercised.
    Every such cell carries a ``TODO(kedge)`` marker and the reconciliation cell reports "not
    reconciled", so nothing here can be mistaken for finished work.
    """
    name = names[stage.id]
    upstream = _upstream_name(stage, names, checkpoints)
    gates = [names[item] for item in stage.depends_on if item in checkpoints]

    lines = [
        f"# Stage {index} of {total}: {_one_line(stage.id)}  "
        f"[{stage.kind.value}, confidence {stage.confidence.value}]",
        *_comment("Intent", stage.intent),
    ]
    if stage.sources:
        lines.extend(_comment_items("Sources", [source.render() for source in stage.sources]))
    if stage.depends_on:
        lines.extend(_comment("After", ", ".join(names[item] for item in stage.depends_on)))
    if stage.excel_pattern is not None:
        hint = _PATTERN_HINTS.get(stage.excel_pattern, "")
        lines.extend(_comment("Excel pattern", f"{stage.excel_pattern.value} -> {hint}"))
    for assumption in stage.assumptions:
        lines.extend(_comment("Assumes", assumption))
    if stage.operations:
        lines.extend(_comment("Implements analysis operations", ", ".join(stage.operations)))
    if stage.notes:
        lines.extend(_comment("Note", stage.notes))

    if stage.needs_review_marker:
        lines.extend(
            [
                "#",
                f"# REVIEW: the plan records confidence '{stage.confidence.value}' here. This",
                "# stage is scaffolded like the rest but is not trusted like the rest. Check the",
                "# assumptions above against the workbook before relying on what it produces.",
            ]
        )

    lines.extend(
        [
            "#",
            "# TODO(kedge): translate this stage. LazyFrame throughout, expressions rather than",
            "# loops, .collect() only at a boundary, Excel semantics through .xl (PLAN 2.5, 2.6).",
        ]
    )
    for gate in gates:
        lines.append(
            f'_gate_{name} = {gate}["decision"]  # blocked until that checkpoint is approved'
        )

    if stage.kind is StageKind.OUTPUT:
        lines.append(f"{name} = {upstream}.collect()  # boundary: materialise for the output")
    else:
        lines.append(f"{name} = {upstream}  # passthrough until translated")
    lines.append(name)
    return ScaffoldCell(name=name, code="\n".join(lines), role="stage", stage_id=stage.id)


def _checkpoint_cells(
    stage: Stage,
    index: int,
    total: int,
    names: dict[str, str],
    gated: Mapping[str, str] | None = None,
) -> list[ScaffoldCell]:
    """A checkpoint: the approval UI, then the gate that blocks everything downstream.

    Two cells rather than one because a marimo cell cannot both create a UI element and read its
    value — the defining cell does not re-run on interaction. The gate cell is where ``mo.stop``
    lives, and ``mo.stop`` halts this cell *and its descendants*, so the block is real rather
    than advisory: a downstream cell referencing this stage's output is cancelled, not skipped.

    Where the checkpoint declares a hand-in of its own, the UI cell reads that hand-in's frame as
    well as whatever gates it. The decision is *about* that file -- "does the re-extract say what
    we predicted?" -- so offering the dropdown before the file has arrived asks somebody to sign
    off on data nobody has seen. And nothing else could hide it: a cell that only builds ``mo.ui``
    elements has no dataflow edges, so it renders from the moment the notebook opens whatever
    sits above it. Reading the frame is what creates the edge.
    """
    name = names[stage.id]
    checkpoint = stage.effective_checkpoint()
    unblocking = checkpoint.options[0]
    decision = f"{name}_decision"
    note = f"{name}_note"
    heading = f"### Checkpoint: {stage.id}"
    blocked = _waiting(
        index,
        total,
        stage.id,
        "Record a decision above to continue. Nothing below runs until you do.",
    )
    needs_note = _waiting(
        index,
        total,
        stage.id,
        "A reason is required, not optional -- it is the whole "
        "improvement over somebody typing a number into Excel with no record of why.",
    )

    gates = _gate_tokens(stage, names, gated or {})
    reads_a_handin = _named_handin(stage) is not None
    if reads_a_handin:
        gates = [f"{name}_frame", *gates]
    ui_lines = [
        f"# Checkpoint {index} of {total}: {_one_line(stage.id)}",
        *(
            [
                "# Dark until the hand-in above it has arrived. This decision is about that",
                "# file, and a card that only builds `mo.ui` elements reads nothing, so nothing",
                "# could hide it -- it would offer a sign-off on data nobody has supplied yet.",
                "# Reading the frame is what creates the edge marimo hides this cell on.",
            ]
            if reads_a_handin
            else []
        ),
        *_after_lines(name, gates),
        "# Deliberately NOT automated. Forcing a judgement call into code either fabricates",
        "# logic that was never there or silently drops a control (PLAN 2.2). Recording the",
        "# decision and the reason here is better than the Excel original, where the same step",
        "# was someone typing a number with no record of why.",
        *_comment("Intent", stage.intent),
    ]
    if checkpoint.guidance:
        ui_lines.extend(_comment("Guidance", checkpoint.guidance))
    ui_lines.extend(
        [
            f"{decision} = mo.ui.dropdown(",
            f"    options={list(checkpoint.options)!r},",
            '    label="Decision",',
            ")",
            f"{note} = mo.ui.text_area(",
            '    label="Reason (recorded with the decision)",',
            '    placeholder="Why is this the right call, this time?",',
            "    full_width=True,",
            ")",
            "mo.vstack(",
            "    [",
            f"        mo.md({heading!r}),",
            f"        mo.md({checkpoint.question!r}),",
            f"        {decision},",
            f"        {note},",
            "    ]",
            ")",
        ]
    )

    gate_lines = [
        f"# Gate for checkpoint '{_one_line(stage.id)}'. mo.stop halts this cell and every cell",
        "# downstream of it, so nothing proceeds on an unapproved checkpoint.",
        "#",
        "# A decision already recorded against this run stands: reopening the notebook must not",
        "# ask again for something somebody already signed, and the dropdown above comes back",
        "# empty because marimo's state died with the kernel, not because nobody decided.",
        f"_recorded = kedge_run.decision_for({stage.id!r})",
        f"_decision = {decision}.value or (_recorded.decision if _recorded else None)",
        f'_note = ({note}.value or "").strip() or (_recorded.note if _recorded else "")',
        "mo.stop(",
        f"    _decision != {unblocking!r},",
        f"    mo.md({blocked!r}),",
        ")",
    ]
    if checkpoint.require_note:
        gate_lines.extend(
            [
                "mo.stop(",
                "    not _note,",
                f"    mo.md({needs_note!r}),",
                ")",
            ]
        )
    gate_lines.extend(
        [
            "# Recorded only when it is new, so re-running the cell does not append the same",
            "# decision again on every keystroke upstream.",
            "if _recorded is None or (_recorded.decision, _recorded.note) != (_decision, _note):",
            "    kedge.runs.record_decision(",
            f"        KEDGE_RUNS, KEDGE_RUN_ID, {stage.id!r}, _decision, _note",
            "    )",
            f"{name} = {{",
            f'    "stage": {stage.id!r},',
            '    "decision": _decision,',
            '    "note": _note,',
            '    "decided_at": datetime.datetime.now(datetime.UTC),',
            "}",
            name,
        ]
    )

    return [
        ScaffoldCell(
            name=f"{name}_ui", code="\n".join(ui_lines), role="checkpoint", stage_id=stage.id
        ),
        ScaffoldCell(name=name, code="\n".join(gate_lines), role="checkpoint", stage_id=stage.id),
    ]


def _handin_satellites(name: str) -> tuple[str, ...]:
    """The names a stage with its own named hand-in claims beyond the stage name.

    Three cells and six names: the selector cell defines the three widgets and the tab strip,
    the receipt cell defines the record, the frame cell defines the LazyFrame. All six have to
    be unique across the notebook for the same reason a checkpoint's three do -- marimo rejects
    a notebook that defines one name in two cells, and the user meets that as a file that will
    not open rather than as a scaffolding error.
    """
    return (
        f"{name}_input",
        f"{name}_drop",
        f"{name}_pick",
        f"{name}_paste",
        f"{name}_handin",
        f"{name}_frame",
    )


def _named_handin(stage: Stage) -> str | None:
    """The label of this stage's own hand-in, or ``None`` if it reads the notebook's.

    The distinction is carried by :attr:`~kedge.plan.model.StageSource.ref`, and it is the
    difference between a process with one input and a process with several.
    ``{origin: handin}`` means the hand-in at the top of the notebook -- one file, the ordinary
    case, and what every plan written before hand-offs existed meant.
    ``{origin: handin, ref: 'post-adjustment extract'}`` means *another* one, arriving later, at
    a point in the runbook the user has not reached yet.

    That second shape is what a workbook whose process is "extract, adjust, re-extract, check"
    needs, and the head hand-in cannot express it: the re-extract does not exist when the
    notebook is opened, and the cell asking for it has to sit below the cell that told the user
    how to produce it.
    """
    for source in stage.sources:
        if source.origin is SourceOrigin.HANDIN and source.ref:
            return source.ref
    return None


def _handin_cells(
    stage: Stage,
    index: int,
    total: int,
    name: str,
    label: str,
    gated: Mapping[str, str] | None = None,
) -> list[ScaffoldCell]:
    """A stage's own hand-in: selector, receipt, frame.

    The same three steps the notebook head performs for its primary input, emitted again under
    this stage's name because a runbook asks for evidence more than once. It deliberately does
    *not* repeat the drift and contract cells: those describe the notebook's declared input, and
    a mid-process re-extract is a different shape under a different agreement. What it does keep
    is the managed store, so the second hand-in is hashed and receipted exactly like the first
    and "this run consumed these files" stays a defensible claim about all of them.
    """
    heading = f"### {label}"
    drop_label = f"Drop {label} here"
    audit_label = f"**{label}**"
    waiting = _waiting(
        index,
        total,
        label,
        "Drop a file, select one, or paste the result grid -- header row included.",
    )
    gates = _gate_tokens(stage, {}, gated or {})
    selector_lines = [
        f"# Stage {index} of {total}: {_one_line(stage.id)} -- the hand-in this stage reads.",
        *_comment("Intent", stage.intent),
        "#",
        "# A second hand-in, arriving later than the notebook's own. Everything below this cell",
        "# stays dark until it does, which is what makes this a runbook rather than a report:",
        "# the next instruction appears once the previous one has been carried out, not before.",
        *(
            [
                "#",
                "# And this cell is itself hidden until the step above it is confirmed. Reading",
                "# that token is what creates the edge marimo hides the cell on -- a selector",
                "# that constructed widgets and read nothing would render from the moment the",
                "# notebook opened, inviting a re-extract taken before the statement ever ran.",
            ]
            if gates
            else []
        ),
        *_after_lines(name, gates),
        # `!r` rather than interpolation into a quoted literal, exactly as `heading` and
        # `waiting` are handled three lines down. A hand-in ref is plan-supplied free text, and
        # `the "after" extract` -- an entirely ordinary thing to call one -- closed the literal
        # early and took the whole scaffold down with `cell ... would not parse`. It is the same
        # class of bug as non-negotiable 3's apostrophe in a counterparty name; the concatenation
        # here happens to be building Python rather than SQL.
        f'{name}_drop = mo.ui.file(kind="area", label={drop_label!r})',
        f"{name}_pick = mo.ui.file_browser(",
        '    multiple=False, label="...or select it on this machine"',
        ")",
        f"{name}_paste = mo.ui.text_area(",
        '    label="...or paste the result grid, header row included",',
        '    placeholder="Paste straight from the query client. Tab, comma, semicolon or pipe.",',
        "    full_width=True,",
        "    rows=8,",
        ")",
        f"{name}_input = mo.ui.tabs(",
        f'    {{"Drop": {name}_drop, "Select": {name}_pick, "Paste": {name}_paste}}',
        ")",
        f"mo.vstack([mo.md({heading!r}), {name}_input])",
    ]

    receipt_lines = [
        f"# The receipt for {_one_line(label)}. Same store, same hashing, same audit line as",
        "# the hand-in at the top of the notebook -- a mid-process extract is evidence too.",
        "#",
        "# `previous_handin` is what makes resuming real: if this run already took a hand-in at",
        "# this step, and the managed copy is still on disk and still hashes the same, it is",
        "# used and the user is not asked again. A file that has changed underneath is not",
        "# reused -- resuming onto different data silently is worse than asking twice.",
        f"_resumed = kedge.runs.previous_handin(KEDGE_RUNS, KEDGE_RUN_ID, {stage.id!r})",
        f'_pasted = ({name}_paste.value or "").strip()',
        "mo.stop(",
        "    _resumed is None",
        f"    and not {name}_drop.value",
        f"    and not {name}_pick.value",
        "    and not _pasted,",
        f"    mo.md({waiting!r}),",
        ")",
        "",
        "# Precedence by reproducibility, most reproducible first -- see the head hand-in cell.",
        "# A fresh selection outranks the resumed one: supplying a file again is how a user",
        "# corrects an extract they got wrong.",
        f"{name}_handin = kedge.ingest.receive(",
        f"    {name}_pick.value",
        f"    or {name}_drop.value",
        "    or (kedge.ingest.Paste(text=_pasted) if _pasted else None)",
        "    or _resumed,",
        "    store_dir=HANDIN_DIR,",
        ")",
        "kedge.runs.record_handin(",
        f"    KEDGE_RUNS, KEDGE_RUN_ID, {stage.id!r}, {name}_handin",
        ")",
        # The label is a repr'd constant concatenated with the f-string rather than interpolated
        # into it, for the reason the selector's `drop_label` carries: it is plan-supplied text.
        f'mo.md({audit_label!r} + f" `{{{name}_handin.audit_line()}}`")',
    ]

    frame_lines = [
        f"# {_one_line(label)} as a frame. read_data is the reader the contract check uses, so a",
        "# layout note here means what it means at the top of the notebook.",
        f"_data, _layout = kedge.ingest.read_data({name}_handin.path)",
        f"{name}_frame = _data.lazy()",
        "_notes = _layout.notes()",
        "mo.vstack(",
        f'    [mo.md("_Layout: " + "; ".join(_notes) + "._"), {name}_frame]',
        "    if _notes",
        f"    else [{name}_frame]",
        ")",
    ]

    return [
        ScaffoldCell(
            name=f"{name}_input",
            code="\n".join(selector_lines),
            role="handin",
            stage_id=stage.id,
        ),
        ScaffoldCell(
            name=f"{name}_handin",
            code="\n".join(receipt_lines),
            role="handin",
            stage_id=stage.id,
        ),
        ScaffoldCell(
            name=f"{name}_frame",
            code="\n".join(frame_lines),
            role="handin",
            stage_id=stage.id,
        ),
    ]


def _handoff_satellites(name: str) -> tuple[str, ...]:
    """Every name a hand-off may claim beyond the statement itself.

    Reserved as one set whether or not this particular hand-off uses all of them. The
    alternative -- reserving exactly what each shape needs -- means a plan that later gains a
    confirmation quietly collides with a stage named after one of these, and marimo reports
    that as a notebook which will not open.
    """
    return (
        f"{name}_inputs",
        f"{name}_parameters",
        f"{name}_ran",
        f"{name}_ran_note",
        f"{name}_confirmed",
    )


def _gate_tokens(stage: Stage, names: dict[str, str], gated: Mapping[str, str]) -> list[str]:
    """The tokens this stage must read before it may render.

    Reading a token is what creates the dataflow edge, and the edge is the only thing that
    hides a cell in marimo. This was the bug the eval missed entirely: the post-adjustment
    selector *constructed widgets and read nothing*, so there was no edge, so nothing could gate
    it -- and it rendered from the moment the notebook opened, inviting the user to paste a
    re-extract taken before the update had been run.
    """
    return [gated[item] for item in stage.depends_on if item in gated]


def _after_lines(name: str, gates: Sequence[str]) -> list[str]:
    """The assignment that reads every token this cell waits for. One line, or none.

    Reading the name is the whole job; the assignment exists only so that the reading happens.
    One line per token, each assigning the *same* ``_after_<name>``, is what this was, and it
    made every line but the last a dead store -- which marimo does not care about, because it
    took the edges either way, and a reviewer does, because these cells are meant to be read.
    """
    if not gates:
        return []
    if len(gates) == 1:
        return [f"_after_{name} = {gates[0]}"]
    return [f"_after_{name} = ({', '.join(gates)})"]


def _waiting(index: int, total: int, title: str, instruction: str) -> str:
    """The text a blocked cell renders.

    In app mode a stopped cell is the *only* thing the user sees at that point -- everything
    below it is gone, with no indication whether it is waiting, broken or finished. So the stop
    message carries the position as well as the instruction: "Step 4 of 8" is the difference
    between a page that has stalled and a page that is waiting for you.
    """
    return f"**Step {index} of {total}: {title}.** {instruction}"


def _confirmation_cells(stage: Stage, index: int, total: int, name: str) -> list[ScaffoldCell]:
    """The two cells that record a mutating statement as having been run.

    A read-only query needs none of this: the hand-in that follows *is* the evidence it was run,
    and asking as well is friction with nothing behind it. A statement that changes data is
    different in both directions. Nothing downstream is evidence it ran -- a re-extract taken
    beforehand looks exactly like one taken after -- and there is no way to detect that
    afterwards. So the user says so, the assertion goes in the run record with a time against
    it, and everything downstream hangs off that.

    The gate **reads** that record as well as writing it, exactly as a checkpoint's does. It
    said it did for as long as it existed and it did not, which nobody found because no plan in
    the repository declared a mutating hand-off: the tick-box comes back empty on a new kernel,
    so a run resumed the next morning stopped here and asked somebody to assert a second time
    that they had run a production UPDATE.
    """
    ran = f"{name}_ran"
    note = f"{name}_ran_note"
    confirmed = f"{name}_confirmed"

    ui_lines = [
        f"# Confirmation for '{_one_line(stage.id)}'. The statement above changes data, so nothing",
        "# below this appears until somebody says it has been run -- and what they say is recorded.",
        "#",
        "# Reads the statement so this cell is itself hidden until there is something to",
        "# confirm. A tick-box offering 'I have run it' above a statement that has not",
        "# been generated yet is not useless -- it is a way to record a lie.",
        f"_awaiting_{name} = {name}",
        f'{ran} = mo.ui.checkbox(label="I have run the statement above")',
        f"{note} = mo.ui.text_area(",
        '    label="Anything worth recording about the run (rows affected, ticket, who ran it)",',
        '    placeholder="e.g. 76 rows affected, ticket FC-2291, run by R. Advani at 09:14",',
        "    full_width=True,",
        ")",
        "mo.vstack(",
        "    [",
        f"        mo.md({f'#### Confirm: {stage.id}'!r}),",
        f"        {ran},",
        f"        {note},",
        "    ]",
        ")",
    ]

    waiting = _waiting(
        index,
        total,
        stage.id,
        "Run the statement above, then tick the box to confirm it. Nothing below this point "
        "is shown until you do -- a re-extract taken before the statement ran looks exactly "
        "like one taken after.",
    )
    gate_lines = [
        "# The gate. Recorded into the run before anything downstream sees it, so closing the",
        "# notebook here and reopening it tomorrow resumes past this point rather than asking",
        "# again -- which means reading the record as well as writing it. The tick-box comes back",
        "# empty because marimo's state died with the kernel, not because nobody ran the",
        "# statement, and a gate that only looked at the box would make somebody assert a second",
        "# time that they had run a production UPDATE. They would either tick it without",
        "# thinking or run it twice.",
        f"_confirmed = kedge_run.decision_for({stage.id!r})",
        f"_ran = bool({ran}.value) or _confirmed is not None",
        f'_ran_note = ({note}.value or "").strip() or (_confirmed.note if _confirmed else "")',
        "mo.stop(",
        "    not _ran,",
        f"    mo.md({waiting!r}),",
        ")",
        "# Recorded only when it is new, so an upstream keystroke does not append the same",
        "# confirmation again on every re-run.",
        "if _confirmed is None or _confirmed.note != _ran_note:",
        "    kedge.runs.record_decision(",
        f'        KEDGE_RUNS, KEDGE_RUN_ID, {stage.id!r}, "ran", _ran_note',
        "    )",
        f"{confirmed} = {{",
        f'    "stage": {stage.id!r},',
        '    "decision": "ran",',
        '    "note": _ran_note,',
        '    "confirmed_at": datetime.datetime.now(datetime.UTC),',
        "}",
        f"mo.md({f'Confirmed: the statement for `{stage.id}` has been run.'!r})",
    ]

    return [
        ScaffoldCell(
            name=f"{name}_ran", code="\n".join(ui_lines), role="handoff", stage_id=stage.id
        ),
        ScaffoldCell(name=confirmed, code="\n".join(gate_lines), role="handoff", stage_id=stage.id),
    ]


def _identifier(text: str) -> str:
    """A parameter name reduced to something that can be a Python variable."""
    cleaned = _IDENTIFIER_UNSAFE.sub("_", text.strip()).strip("_").lower()
    return cleaned or "parameter"


def _parameter_cell(stage: Stage, name: str, handoff: Handoff) -> ScaffoldCell:
    """The inputs a parameterised statement is built from.

    A separate cell because marimo's single-definition rule forbids one cell from both creating
    a UI element and reading it -- and because these belong at the *top* of the process. "Which
    period are you running this for" is the first thing anybody is asked when they do this by
    hand, and it is the thing that must not be a constant somebody edits inside a SQL string
    every month.

    Every input is a text box unless its name suggests otherwise, because the plan says what a
    parameter is called and not what shape it is. A date is worth detecting: it is the parameter
    almost every periodic process has, and `mo.ui.date` yields a `datetime.date` that
    `kedge.sql.literal` renders as a proper `DATE` literal.
    """
    lines = [
        f"# Inputs for '{_one_line(stage.id)}'. Fill these in first -- the statement below is built",
        "# from them, and the run records what they were.",
    ]
    for parameter in handoff.parameters:
        variable = f"{name}_{_identifier(parameter)}"
        label = parameter.replace("_", " ")
        if _DATE_PARAMETER.search(parameter):
            lines.append(f"{variable} = mo.ui.date(label={label!r})")
        else:
            lines.append(f"{variable} = mo.ui.text(label={label!r}, full_width=True)")
    shown = ", ".join(f"{name}_{_identifier(parameter)}" for parameter in handoff.parameters)
    lines.append(f"mo.vstack([mo.md({f'#### Inputs: {stage.id}'!r}), {shown}])")
    return ScaffoldCell(
        name=f"{name}_inputs", code="\n".join(lines), role="handoff", stage_id=stage.id
    )


def _handoff_cells(
    stage: Stage,
    index: int,
    total: int,
    names: dict[str, str],
    gated: Mapping[str, str],
) -> list[ScaffoldCell]:
    """A hand-off: the statement the user takes away, and nothing else.

    The one stage kind that computes nothing. What it emits is text plus an instruction, and it
    binds that text to the stage's name -- so the rest of the notebook, and anything reading the
    notebook's definitions, can get at exactly what the user was told to run.

    Two shapes. A fixed statement is a string literal, optionally with ``{placeholders}`` filled
    from a parameters dict the user edits in place. A generated one is rendered row by row out
    of an upstream frame through :mod:`kedge.sql`, which is the reviewable form of the
    ``="UPDATE ... "&F2&"..."`` column real workbooks do this with: the arithmetic happens in
    polars where it can be checked, and the SQL becomes a rendering of the result rather than
    the place the result lives.
    """
    name = names[stage.id]
    handoff = stage.effective_handoff()
    gates = _gate_tokens(stage, names, gated)

    lines = [
        f"# Stage {index} of {total}: {_one_line(stage.id)}  [handoff]",
        "# kedge does not run this. It holds no connection and issues no statement -- what it",
        "# does is work out exactly what needs running, say where, and wait for what comes back.",
        *_comment("Intent", stage.intent),
        *_comment("Do this", handoff.instruction),
    ]
    if handoff.connection:
        lines.extend(_comment("Against", handoff.connection))
    if stage.notes:
        lines.extend(_comment("Note", stage.notes))
    for gate in gates:
        lines.append(f"_gate_{name} = {gate}  # this cell stays hidden until that step is recorded")

    if handoff.is_generated:
        upstream = names.get(handoff.built_from or "", "handin_frame")
        header = f"Generated by kedge from stage '{handoff.built_from}'. Read it before running it."
        if handoff.connection:
            header = f"{header}\nRun against: {handoff.connection}"
        lines.extend(
            [
                "#",
                "# One statement per row of the frame above. kedge.sql renders the literals: a",
                "# name with an apostrophe in it, a null, a date and a money value all have forms",
                "# that string concatenation gets wrong -- quietly, at the moment somebody is",
                "# about to run it against production.",
                "_rows = (",
                f"    {upstream}.collect() if isinstance({upstream}, pl.LazyFrame) else {upstream}",
                ")",
                f"{name} = kedge.sql.script(",
                f"    kedge.sql.render_all(_rows.iter_rows(named=True), {handoff.template!r}),",
                f"    header={header!r},",
                ")",
            ]
        )
    elif handoff.parameters:
        lines.extend(
            [
                "#",
                "# The statement's parameters, read off the inputs above and substituted as",
                "# properly quoted literals. Nobody edits the SQL: a period end typed into a box",
                "# once reaches every statement that needs it, and the run records what it was.",
                f"{name}_parameters = {{",
                *(
                    f"    {parameter!r}: {name}_{_identifier(parameter)}.value,"
                    for parameter in handoff.parameters
                ),
                "}",
                "mo.stop(",
                f'    any(value in (None, "") for value in {name}_parameters.values()),',
                f"    mo.md({_waiting(index, total, stage.id, 'Fill in the inputs above before this statement can be built.')!r}),",
                ")",
                f"kedge.runs.record_parameters(KEDGE_RUNS, KEDGE_RUN_ID, **{name}_parameters)",
                f"{name} = kedge.sql.render({handoff.statement!r}, {name}_parameters)",
            ]
        )
    else:
        lines.append(f"{name} = {handoff.statement!r}")

    fence = handoff.medium.value
    body = 'f"```' + fence + "\\n{" + name + '}\\n```"'
    lines.extend(
        [
            "mo.vstack(",
            "    [",
            f"        mo.md({f'### Run this: {stage.id}'!r}),",
            f"        mo.md({handoff.instruction!r}),",
            f"        mo.md({body}),",
            "    ]",
            ")",
        ]
    )
    cells: list[ScaffoldCell] = []
    if handoff.parameters and not handoff.is_generated:
        cells.append(_parameter_cell(stage, name, handoff))
    cells.append(ScaffoldCell(name=name, code="\n".join(lines), role="handoff", stage_id=stage.id))
    if handoff.needs_confirmation:
        cells.extend(_confirmation_cells(stage, index, total, name))
    return cells


def _upstream_name(stage: Stage, names: dict[str, str], checkpoints: set[str]) -> str:
    """The frame a stage builds on: its first non-checkpoint dependency, else the hand-in.

    A checkpoint's output is a decision record, not a frame, so it gates a stage without feeding
    it. The reference that does the gating is emitted separately.
    """
    # A stage's own hand-in outranks its dependencies: it is the thing this stage was written
    # to read, and the dependency is usually the hand-off cell that told the user to produce it.
    if _named_handin(stage) is not None:
        return f"{names[stage.id]}_frame"
    for dependency in stage.depends_on:
        if dependency in names and dependency not in checkpoints:
            return names[dependency]
    return "handin_frame"


def _one_line(text: str) -> str:
    """Plan-supplied text flattened onto one line, for interpolation into a comment.

    A ``#`` comment ends at a newline, so a stage id or a hand-in label carrying one turns the
    rest of kedge's own sentence into a line of code -- and :func:`_check_house_rules` then
    refuses the whole scaffold with a syntax error naming a cell nobody can see. Prose routed
    through :func:`_comment` is already safe, because ``textwrap.wrap`` collapses whitespace;
    this is for the lines that build a comment by hand.

    It is the comment half of the same rule ``{value!r}`` enforces for a generated *literal*:
    plan text is free-form, and free-form text put into code unescaped breaks the code.
    """
    return " ".join(text.split())


def _comment(label: str, text: str, *, width: int = 92) -> list[str]:
    """Wrap prose into comment lines, so a long intent is not a 300-column line."""
    wrapped = textwrap.wrap(f"{label}: {text}", width=width) or [f"{label}:"]
    return [f"# {line}" for line in wrapped]


def _comment_items(label: str, items: list[str], *, width: int = 92) -> list[str]:
    """Comment lines for a list, one item per line where the joined list would wrap.

    :func:`_comment` breaks prose at any space, and since schema 1.1 a rendered source *has* a
    space in it — ``power_query Ratings``. Wrapped, a long list could end a line at
    ``power_query`` and put the ref on the next, which reads as an origin nobody named. An item
    is never split across two lines here; where they all fit on one, nothing changes.
    """
    joined = _comment(label, ", ".join(items), width=width)
    if len(joined) <= 1:
        return joined
    continuation = " " * (len(label) + 2)
    return [f"# {label}: {items[0]}", *(f"# {continuation}{item}" for item in items[1:])]


# =============================================================================
# THE TAIL (PLAN 4.5)
# =============================================================================


_RECONCILE_VALUES_HEAD = [
    "# Which of this notebook's values reproduce which of the workbook's own cached ranges.",
    "# The panel below is driven entirely from this map, and it is the one part of",
    "# reconciliation nobody but a translator can write: the keys are facts, taken from the",
    "# plan, but which column of which frame reproduces the range behind each key is a",
    "# judgement about the translation.",
    "#",
    "# The keys are analysis operation ids, taken from each stage's `operations` in the plan.",
    "# kedge.reconcile reads the workbook, proposes one region per formula region carrying",
    "# cached values -- keyed by that same id -- and compares the two. A key with no entry is",
    "# reported as unchecked, never as passed.",
]


def _reconciliation_values(plan: ProcessPlan, names: dict[str, str]) -> list[str]:
    """The ``operation id -> notebook value`` literal the panel is driven from.

    ``Stage.operations`` holds the ids of the analysis operations a stage implements, and
    :func:`kedge.reconcile.infer_regions` keys the regions it proposes by that same id. Mapping
    one to the other is therefore the whole wiring, and it is why the plan carries the link back
    to the facts at all.

    Two kinds of stage are left out, for two different reasons.

    A stage that names no operation contributes nothing: there is no honest guess to make about
    which workbook range it reproduces, and a region matched to the wrong column would pass or
    fail for the wrong reason.

    A stage that :attr:`~kedge.plan.model.Stage.generates_no_code` -- a checkpoint or a hand-off
    -- computes nothing, so it has no values to offer. Hand-offs used to be mapped anyway, and
    the entry that produced was harmful in three ways at once. The value bound to a hand-off's
    name is *a statement*, so the region it claimed to reproduce came back **failed** rather
    than unchecked -- one rendered script against seventy-six cached cells -- which is the
    permanently amber signal a correct notebook has no way to clear. Reading that name made the
    panel a dataflow descendant of the checkpoint gating the hand-off, so marimo would not
    render the evidence until after the decision it is evidence for, defeating
    :func:`_with_reconciliation` entirely. And because a hand-off is emitted *below* the last
    computing stage the panel is anchored to, the panel read a name defined beneath it. They go
    to ``not_reproduced`` instead: see :func:`_not_reproduced`.
    """
    seen: set[str] = set()
    entries: list[str] = []
    for stage in plan.ordered_stages():
        if stage.generates_no_code:
            continue
        for operation in stage.operations:
            if operation in seen:
                continue
            seen.add(operation)
            entries.append(f"    {operation!r}: {names[stage.id]},  # stage {stage.id!r}")
    return entries


def _not_reproduced(plan: ProcessPlan) -> list[tuple[str, str]]:
    """Workbook regions this conversion deliberately does not reproduce, and why.

    A hand-off's ``operations`` are real: the workbook had a column of ``="UPDATE ... "&F17&``
    filled down four hundred rows, and this stage is what became of it. What it became is not a
    reproduction. kedge renders the statement through :mod:`kedge.sql`, which is the whole point
    -- the workbook's own concatenation produces invalid SQL the moment a value contains an
    apostrophe -- so there is nothing here that could match the cached text, and there should
    not be.

    Saying so is not a formality. Left unmapped, the region comes back with "the notebook
    produced no values for this region, check that the cell ran and that the variable names
    match", which sends a reader hunting a bug that is not there, on every run, for ever.
    Declared, it is still not a pass -- nothing can make an unchecked region one -- but the
    panel reports it as a decision with a reason and leads with ``CHECKED WITH EXCEPTIONS``
    rather than the same amber as a genuine gap.

    Returns:
        ``(operation id, reason)`` pairs, in plan order.
    """
    declared: list[tuple[str, str]] = []
    seen: set[str] = set()
    for stage in plan.ordered_stages():
        if not stage.is_handoff:
            continue
        handoff = stage.effective_handoff()
        how = (
            "rendered through kedge.sql rather than reproduced as text"
            if handoff.is_generated or handoff.parameters
            else "handed over as a fixed statement"
        )
        for operation in stage.operations:
            if operation in seen:
                continue
            seen.add(operation)
            declared.append(
                (
                    operation,
                    f"Stage {stage.id!r} hands this over for somebody to run; it is {how}, so "
                    f"there is nothing here to compare against the workbook's own cached text. "
                    f"Reproducing that text would reproduce whatever is wrong with it: a "
                    f"statement built by concatenation is invalid SQL the moment a value "
                    f"contains an apostrophe.",
                )
            )
    return declared


def _values_cell(plan: ProcessPlan, names: dict[str, str]) -> ScaffoldCell:
    """The region map, as a cell of its own with the translation still to write.

    Separate from the panel because the two are owned by different people. The panel is
    machinery -- the one cell in this module where a mistake is dangerous rather than merely
    wrong -- and it stays kedge's, whole and unedited. The map is a judgement about the
    translation, and it is the only part of reconciliation nobody but a translator can make: the
    scaffolder knows the operation ids but not which column of which frame reproduces each one.

    So the map carries a ``TODO(kedge)`` marker, exactly as a stage cell does. It did not, and
    that omission was worth more than it looks: everything that fills the scaffolder's holes --
    the agent, the eval, and a human reading the notebook for what is left to do -- finds them
    by that marker, so nobody was ever asked to finish this, and a notebook reported regions as
    unmapped for ever without ever saying they were unfinished rather than unreproducible.

    The default value is the stage's whole frame, which is a starting point rather than an
    answer. It happens to resolve wherever the workbook's own header for the region matches a
    column of that frame, because :func:`kedge.reconcile.compare.to_vector` will take a named
    column out of a frame; a totals row, or a column this notebook names differently, needs
    ``<frame>.select("<column>")`` or a list of scalars instead.
    """
    lines = list(_RECONCILE_VALUES_HEAD)
    entries = _reconciliation_values(plan, names)
    if not entries:
        lines.extend(
            [
                "#",
                "# No stage in this plan names an analysis operation, so there is nothing to map",
                "# and nothing to write here. Entries look like:",
                "#     '<operation id>': <the values that reproduce it>",
                "reconciliation_values = {}",
                "reconciliation_values",
            ]
        )
        return ScaffoldCell(name="reconciliation_values", code="\n".join(lines), role="reconcile")
    lines.extend(
        [
            "#",
            "# TODO(kedge): give each key the values that reproduce its range -- a Series, a",
            "# one-column frame, or a list of scalars for a totals row. The whole frame below is",
            "# a starting point: it resolves only where the workbook's own header for the region",
            "# names one of that frame's columns, and never for a totals row.",
            "reconciliation_values = {",
            *entries,
            "}",
            "reconciliation_values",
        ]
    )
    return ScaffoldCell(name="reconciliation_values", code="\n".join(lines), role="reconcile")


def _baseline_handin(plan: ProcessPlan, names: dict[str, str], head: bool) -> str | None:
    """The hand-in whose digest the acceptance is keyed to, as the notebook names it.

    :func:`kedge.reconcile.check_translation` re-runs the live comparison only when this run is
    working on the same data the acceptance was measured on, and it decides that by digest. So
    the digest has to be of the data the reconciled values were **computed from**. Citing the
    head hand-in when the arithmetic ran on a stage's own one is not a near miss: it keys the
    decision to a file that takes no part in the computation, so the check either re-runs on a
    period it cannot describe or declines to re-run on the one period it can.

    Resolved as the first hand-in feeding anything the map reports on. First rather than
    nearest, because a process with several inputs is reconciled against the position it starts
    from, and that is the extract the workbook itself was built on.

    **A stage that generates no code is passed over even when it declares a hand-in**, and that
    is the paragraph above enforced rather than restated. Being an ancestor of the arithmetic is
    not the same as feeding it: :func:`_upstream_name` never resolves to a checkpoint or a
    hand-off, so a stage depending on one is built on whatever *it* found -- usually
    ``handin_frame`` -- while its own ``<name>_frame`` is read by its approval card and by
    nothing that computes. Walking past that distinction cited ``<checkpoint>_handin.sha256``
    over a comparison that ran on the head hand-in, which is exactly the file-that-takes-no-part
    this function exists to refuse. It was a ``NameError`` before a checkpoint's hand-in
    scaffolded any cells; quiet and wrong is the worse of the two (CLAUDE.md non-negotiable 6).

    The fall-through is then consistent by construction rather than by coincidence: whatever
    made the computation reach ``handin_frame`` is what made :func:`head_handin_is_read` true,
    so ``head`` is set exactly when ``handin`` is the record to cite.

    Args:
        plan: The plan being scaffolded.
        names: Stage id to cell name.
        head: Whether the notebook's own hand-in cells were emitted.

    Returns:
        The name of the ``HandIn`` record to take the digest from, or ``None`` when nothing the
        map reports on was computed from a hand-in at all.
        :func:`kedge.reconcile.check_translation` takes ``None`` as "this run's data cannot be
        identified" and declines to re-compare, which is the safe direction: it cites the
        recorded acceptance rather than passing a comparison it has no business making.
    """
    contributing = {
        stage.id
        for stage in plan.ordered_stages()
        if stage.operations and not stage.generates_no_code
    }
    by_id = {stage.id: stage for stage in plan.stages}
    ancestors: set[str] = set()
    frontier = list(contributing)
    while frontier:
        stage_id = frontier.pop()
        if stage_id in ancestors or stage_id not in by_id:
            continue
        ancestors.add(stage_id)
        frontier.extend(by_id[stage_id].depends_on)
    for stage in plan.ordered_stages():
        if stage.generates_no_code:
            continue
        if (not contributing or stage.id in ancestors) and _named_handin(stage) is not None:
            return f"{names[stage.id]}_handin"
    return "handin" if head else None


def _watching_this_run(plan: ProcessPlan, head: bool) -> list[str]:
    """What is checking *this* run's numbers, given the panel may be citing an old acceptance.

    A citation with nothing beside it reads as "nothing is being checked", which is both wrong
    and the kind of wrong that stops people reading the panel at all. Derived from what the plan
    actually emits rather than asserted: a runbook whose hand-ins are all declared on stages has
    no contract cell at the top of it, and claiming one would be worse than claiming nothing.
    """
    watching: list[str] = []
    if head:
        watching.append("the hand-in contract and the drift report, at the top of this notebook")
    if any(_named_handin(stage) is not None for stage in plan.stages):
        watching.append("the receipt on every hand-in this run consumed, hashed and dated")
    if plan.checkpoints:
        watching.append("the checkpoints, which record who decided what and why")
    if not watching:
        watching.append("the run record, which says what this run did and when")
    return watching


def _wrapped(text: str, *, width: int = 84) -> list[str]:
    """Prose split into string literals that fit a line, each keeping its trailing space."""
    pieces = textwrap.wrap(text, width=width) or [text]
    return [f"{piece} " if index < len(pieces) - 1 else piece for index, piece in enumerate(pieces)]


def _panel_cell(plan: ProcessPlan, names: dict[str, str], head: bool) -> ScaffoldCell:
    """The reconciliation panel: the artifact that makes the notebook a controlled process."""
    digest = _baseline_handin(plan, names, head)
    lines = [
        "# Reconciliation asks whether the *translation* is faithful: does this Python reproduce",
        "# the numbers the workbook itself holds? That is a question about the **conversion**,",
        "# and it has one answer, measured once, against the data the spreadsheet contains.",
        "#",
        "# It is not a question about this month's run. The workbook was one period; a run is",
        "# another, and the numbers are supposed to differ. Comparing anyway fails on every run",
        "# after the first -- a red panel saying the figures do not match, on a run where nothing",
        "# is wrong -- and a few months later it is pointing at a spreadsheet nobody has opened",
        "# since the process changed.",
        "#",
        "# So the outcome is recorded once and cited afterwards, and the live comparison re-runs",
        "# only when this run is working on the same data the acceptance was measured on. A",
        "# failure *there* means somebody edited the notebook into disagreeing with the workbook,",
        "# which is worth knowing. There is still exactly one way to say that this passed, and it",
        "# runs through kedge.reconcile, which refuses to construct one without compared rows",
        "# (PLAN 6.2).",
        "reconciliation = kedge.reconcile.check_translation(",
        "    kedge.reconcile.AcceptanceStore(ACCEPTANCE_PATH),",
        "    WORKBOOK,",
        "    reconciliation_values,",
        f"    handin_sha256={f'{digest}.sha256' if digest else 'None'},",
        "    notebook=__file__,",
    ]
    declared = _not_reproduced(plan)
    if declared:
        lines.append("    not_reproduced={")
        for operation, reason in declared:
            lines.append(f"        {operation!r}: (")
            lines.extend(f"            {piece!r}" for piece in _wrapped(reason))
            lines.append("        ),")
        lines.append("    },")
    lines.append("    watching_this_run=[")
    lines.extend(f"        {item!r}," for item in _watching_this_run(plan, head))
    lines.extend(["    ],", ")", "reconciliation"])
    return ScaffoldCell(name="reconciliation", code="\n".join(lines), role="reconcile")


def _tail_cells(
    plan: ProcessPlan, names: dict[str, str], checkpoints: set[str]
) -> list[ScaffoldCell]:
    """The region map and the panel it drives, in that order.

    Two cells rather than one, and the split is the point: see :func:`_values_cell`.
    """
    head = head_handin_is_read(plan, names, checkpoints)
    return [_values_cell(plan, names), _panel_cell(plan, names, head)]
