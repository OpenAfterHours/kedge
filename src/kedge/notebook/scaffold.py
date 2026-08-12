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

Nothing here writes to a notebook without an approved plan. :func:`scaffold_notebook` checks
``plan.approval`` and refuses, and there is no parameter to talk it out of that.

**Driver dependency.** ``kedge.notebook.driver`` is the only module permitted to touch
``marimo._code_mode``, and it is owned elsewhere. This module depends on the narrow
:class:`CellCreator` protocol below — one method, matching the ``create_cell`` signature
verified in ``docs/marimo-api.md`` §2. If that surface moves, this protocol is the single place
to adapt.

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
from kedge.errors import NotebookError
from kedge.plan.model import ProcessPlan, Stage, StageKind

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)

__all__ = [
    "HEAD_CELL_NAMES",
    "TAIL_CELL_NAMES",
    "CellCreator",
    "CellRole",
    "PlanNotApprovedError",
    "ScaffoldCell",
    "ScaffoldError",
    "build_cells",
    "cell_name_for",
    "scaffold_notebook",
]

CellRole = Literal["setup", "handin", "stage", "checkpoint", "reconcile"]

HEAD_CELL_NAMES = (
    "kedge_setup",
    "handin_source",
    "handin",
    "handin_contract",
    "handin_drift",
    "handin_check",
    "handin_frame",
)
"""The fixed head, in emission order (PLAN 2.8).

The contract is loaded before the drift panel because it names the sheet and the header row,
and the profile, the check and the frame all have to read the same rows or the check is a
statement about data nobody used. Loading is not checking, so drift is still *reported* before
the contract check: a renamed column explains a failure in one line where a schema report
explains it in fifteen. The check then comes before the frame so that the frame cell can
``mo.stop`` on a failed contract, which is what makes the check a gate rather than a comment:
nothing downstream of the frame runs on a hand-in that was rejected.
"""

TAIL_CELL_NAMES = ("reconciliation",)
"""The fixed tail (PLAN 4.5)."""

_RESERVED = {
    *HEAD_CELL_NAMES,
    *TAIL_CELL_NAMES,
    "handin_drop",
    "handin_pick",
    "handin_profile",
    "reconciliation_values",
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


class CellCreator(Protocol):
    """The slice of the notebook driver this module needs.

    Matches ``AsyncCodeModeContext.create_cell`` as verified in ``docs/marimo-api.md`` §2, which
    is the signature ``kedge.notebook.driver`` is expected to expose. Deliberately narrow: the
    scaffolder should not be able to delete or run anything.
    """

    async def create_cell(self, code: str, *, name: str, hide_code: bool = False) -> object:
        """Create one cell.

        The return type is deliberately unconstrained. Both real implementations --
        :class:`kedge.notebook.driver.NotebookDriver` and
        :class:`kedge.notebook.filedriver.FileDriver` -- return a
        :class:`~kedge.notebook.model.MutationResult`, but pinning that here would make this
        module depend on the driver's vocabulary for a value it does not read. The scaffolder
        knows the names it asked for and reports those; what came back is the caller's business.
        """
        ...


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


def _name_map(plan: ProcessPlan) -> dict[str, str]:
    """Map every stage id to its cell name, assigned in emission order."""
    names: dict[str, str] = {}
    used: set[str] = set()
    for stage in plan.ordered_stages():
        name = cell_name_for(stage.id, used)
        if stage.is_checkpoint:
            while any(satellite in used for satellite in _satellite_names(name)):
                name = cell_name_for(name, used | {name})
            used.update(_satellite_names(name))
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
            :func:`scaffold_notebook` has no equivalent escape hatch, so an unapproved plan can
            be looked at but never scaffolded.

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
    cells: list[ScaffoldCell] = list(_head_cells(plan, handins_dir, workbook_path, contract_path))

    ordered = plan.ordered_stages()
    for index, stage in enumerate(ordered, start=1):
        if stage.is_checkpoint:
            cells.extend(_checkpoint_cells(stage, index, len(ordered), names))
            continue
        cells.append(_stage_cell(stage, index, len(ordered), names, checkpoints))

    cells.extend(_tail_cells(plan, names))

    for cell in cells:
        _check_house_rules(cell)
    logger.info("built %d cell(s) from plan v%d for %s", len(cells), plan.version, plan.workbook)
    return cells


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


async def scaffold_notebook(
    plan: ProcessPlan,
    driver: CellCreator,
    *,
    handins_dir: Path | None = None,
    workbook_path: Path | None = None,
    contract_path: Path | None = None,
) -> list[str]:
    """Write an approved plan into the notebook, one named cell per stage.

    Refuses an unapproved plan, with no override. That refusal is the structural half of the
    review gate: approval is state on the plan, and this is the one place that consumes it.

    Args:
        plan: The approved plan.
        driver: The notebook driver, or anything satisfying :class:`CellCreator`.
        handins_dir: Where hand-ins are persisted.
        workbook_path: The workbook holding the reconciliation baseline.
        contract_path: Where the hand-in contract lives.

    Returns:
        The names of the cells written, in creation order. Names rather than driver-assigned
        ids: a name is what the user and the agent both address a cell by, it is stable across
        a reopen, and it is the one thing this function knows for certain whatever the driver
        hands back. ``NotebookDriver`` returns a ``MutationResult``, ``FileDriver`` returns a
        ``MutationResult``, and neither is a cell id.

    Raises:
        PlanNotApprovedError: when ``plan.approval.state`` is not ``approved``.
    """
    if not plan.approval.approved:
        raise _not_approved(plan)

    cells = build_cells(
        plan,
        handins_dir=handins_dir,
        workbook_path=workbook_path,
        contract_path=contract_path,
    )
    written: list[str] = []
    for cell in cells:
        await driver.create_cell(cell.code, name=cell.name, hide_code=False)
        written.append(cell.name)
    logger.info("scaffolded %d cell(s) from plan v%d", len(written), plan.version)
    return written


def _not_approved(plan: ProcessPlan) -> PlanNotApprovedError:
    blockers = plan.approval_blockers()
    detail = "\n" + "\n".join(f"  - {item}" for item in blockers) if blockers else ""
    return PlanNotApprovedError(
        f"plan v{plan.version} for {plan.workbook} is '{plan.approval.state.value}', not "
        f"'approved'. Nothing is written to the notebook before the plan is approved."
        f"{detail}\nReview the plan, then approve it."
    )


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
CONTRACT_PATH = pathlib.Path({contract_path!r})"""

_SOURCE_CELL = """# The hand-in selector. Swapping the file here re-runs everything below it in dataflow
# order, the contract re-validates, and the reconciliation panel goes green or red. That is
# the moment the conversion pays off, and it is precisely what the Excel process cannot do
# (PLAN 2.8).
#
# Two entry points that return different things: the drop zone holds the whole file in kernel
# memory and has no path, so an uploaded hand-in is NOT reproducible tomorrow; the browser
# returns a path and no bytes. They converge in the next cell.
handin_drop = mo.ui.file(kind="area", label="Drop the hand-in here")
handin_pick = mo.ui.file_browser(multiple=False, label="...or select one on this machine")
handin_source = mo.ui.tabs({"Drop": handin_drop, "Select": handin_pick})
handin_source"""

_HANDIN_CELL = """# Both entry points converge here, and kedge.ingest.receive does the whole job: a dropped
# file's bytes are written into the managed store, a selected path is copied into it, both are
# hashed and deduplicated against what is already there, and a receipt is recorded. `handin` is
# a HandIn record whose `path` is always the managed copy and never the transient upload --
# which is what makes this notebook re-runnable tomorrow, when the uploaded bytes are gone.
mo.stop(
    not handin_drop.value and not handin_pick.value,
    mo.md("**Waiting for a hand-in.** Drop a file above, or select one."),
)

# A selected path wins over dropped bytes where both are present: it is the reproducible one.
handin = kedge.ingest.receive(handin_pick.value or handin_drop.value, store_dir=HANDIN_DIR)
mo.md(f"**Hand-in** `{handin.audit_line()}`")"""

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
    """The three filesystem constants the head cells close over.

    Each default assumes the standard workspace layout -- a ``<workbook>.kedge`` directory
    beside the workbook, with ``handins`` inside it -- and each is emitted as a plain constant
    rather than something computed at run time, so a user whose files have moved corrects one
    line instead of reading the scaffolder.
    """
    store = handins_dir if handins_dir is not None else Path("handins")
    project = store.parent
    return {
        "store": str(store),
        "workbook_path": str(
            workbook_path if workbook_path is not None else project.parent / plan.workbook
        ),
        "contract_path": str(
            contract_path if contract_path is not None else project / "contract.yaml"
        ),
    }


def _head_cells(
    plan: ProcessPlan,
    handins_dir: Path | None,
    workbook_path: Path | None,
    contract_path: Path | None,
) -> list[ScaffoldCell]:
    """Setup, selector, receipt, contract, drift, check, frame. The same seven every time.

    The order is :data:`HEAD_CELL_NAMES` and the reasoning for it is there (PLAN 2.8).
    """
    paths = _fixed_paths(plan, handins_dir, workbook_path, contract_path)
    return [
        ScaffoldCell(
            name="kedge_setup",
            role="setup",
            code=_SETUP_TEMPLATE.format(version=plan.version, workbook=plan.workbook, **paths),
        ),
        ScaffoldCell(name="handin_source", role="handin", code=_SOURCE_CELL),
        ScaffoldCell(name="handin", role="handin", code=_HANDIN_CELL),
        ScaffoldCell(name="handin_contract", role="handin", code=_CONTRACT_CELL),
        ScaffoldCell(name="handin_drift", role="handin", code=_DRIFT_CELL),
        ScaffoldCell(name="handin_check", role="handin", code=_CHECK_CELL),
        ScaffoldCell(name="handin_frame", role="handin", code=_FRAME_CELL),
    ]


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
        f"# Stage {index} of {total}: {stage.id}  "
        f"[{stage.kind.value}, confidence {stage.confidence.value}]",
        *_comment("Intent", stage.intent),
    ]
    if stage.sources:
        lines.extend(_comment("Sources", ", ".join(stage.sources)))
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
    stage: Stage, index: int, total: int, names: dict[str, str]
) -> list[ScaffoldCell]:
    """A checkpoint: the approval UI, then the gate that blocks everything downstream.

    Two cells rather than one because a marimo cell cannot both create a UI element and read its
    value — the defining cell does not re-run on interaction. The gate cell is where ``mo.stop``
    lives, and ``mo.stop`` halts this cell *and its descendants*, so the block is real rather
    than advisory: a downstream cell referencing this stage's output is cancelled, not skipped.
    """
    name = names[stage.id]
    checkpoint = stage.effective_checkpoint()
    unblocking = checkpoint.options[0]
    decision = f"{name}_decision"
    note = f"{name}_note"
    heading = f"### Checkpoint: {stage.id}"
    blocked = f"**Blocked at checkpoint `{stage.id}`.** Record a decision above to continue."
    needs_note = f"**Blocked at checkpoint `{stage.id}`.** A reason is required, not optional."

    ui_lines = [
        f"# Checkpoint {index} of {total}: {stage.id}",
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
        f"# Gate for checkpoint '{stage.id}'. mo.stop halts this cell and every cell downstream",
        "# of it, so nothing proceeds on an unapproved checkpoint.",
        "mo.stop(",
        f"    {decision}.value != {unblocking!r},",
        f"    mo.md({blocked!r}),",
        ")",
    ]
    if checkpoint.require_note:
        gate_lines.extend(
            [
                "mo.stop(",
                f'    not ({note}.value or "").strip(),',
                f"    mo.md({needs_note!r}),",
                ")",
            ]
        )
    gate_lines.extend(
        [
            f"{name} = {{",
            f'    "stage": {stage.id!r},',
            f'    "decision": {decision}.value,',
            f'    "note": {note}.value,',
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


def _upstream_name(stage: Stage, names: dict[str, str], checkpoints: set[str]) -> str:
    """The frame a stage builds on: its first non-checkpoint dependency, else the hand-in.

    A checkpoint's output is a decision record, not a frame, so it gates a stage without feeding
    it. The reference that does the gating is emitted separately.
    """
    for dependency in stage.depends_on:
        if dependency in names and dependency not in checkpoints:
            return names[dependency]
    return "handin_frame"


def _comment(label: str, text: str, *, width: int = 92) -> list[str]:
    """Wrap prose into comment lines, so a long intent is not a 300-column line."""
    wrapped = textwrap.wrap(f"{label}: {text}", width=width) or [f"{label}:"]
    return [f"# {line}" for line in wrapped]


# =============================================================================
# THE FIXED TAIL (PLAN 4.5)
# =============================================================================


_RECONCILE_BODY = """
# There is exactly one way this cell can say "passed", and it runs through kedge.reconcile,
# which refuses to construct a pass without compared rows. Every other path -- no workbook on
# this machine, no cached values in it, no region matching a notebook column, an error part
# way through -- lands on NOT RECONCILED with a reason attached. A signed-off claim that
# nothing was checked is the most dangerous artifact this project could produce (PLAN 6.2),
# so the fallback is built from the same report type as the real thing rather than from a
# hopeful string.
_reason = None
reconciliation = None
if not WORKBOOK.is_file():
    _reason = (
        f"The workbook {WORKBOOK} is not on this machine, so nothing was compared. That is "
        f"not a pass: correct WORKBOOK in the setup cell and re-run."
    )
else:
    try:
        reconciliation = kedge.reconcile.reconcile_panel(WORKBOOK, reconciliation_values)
    except kedge.KedgeError as _error:
        _reason = f"Reconciliation could not run, so nothing was compared: {_error}"

if reconciliation is None:
    reconciliation = kedge.reconcile.ReconciliationPanel(
        kedge.reconcile.ReconciliationReport(
            workbook=str(WORKBOOK),
            tolerance=kedge.reconcile.Tolerance(),
            notes=[_reason],
        )
    )
reconciliation"""


def _reconciliation_values(plan: ProcessPlan, names: dict[str, str]) -> list[str]:
    """The ``operation id -> notebook value`` literal the panel is driven from.

    ``Stage.operations`` holds the ids of the analysis operations a stage implements, and
    :func:`kedge.reconcile.infer_regions` keys the regions it proposes by that same id. Mapping
    one to the other is therefore the whole wiring, and it is why the plan carries the link back
    to the facts at all.

    A stage that names no operation contributes nothing: there is no honest guess to make about
    which workbook range it reproduces, and a region matched to the wrong column would pass or
    fail for the wrong reason.
    """
    seen: set[str] = set()
    entries: list[str] = []
    for stage in plan.ordered_stages():
        if stage.is_checkpoint:
            continue
        for operation in stage.operations:
            if operation in seen:
                continue
            seen.add(operation)
            entries.append(f"    {operation!r}: {names[stage.id]},  # stage {stage.id!r}")
    return entries


def _tail_cells(plan: ProcessPlan, names: dict[str, str]) -> list[ScaffoldCell]:
    """The reconciliation panel: the artifact that makes the notebook a controlled process."""
    lines = [
        "# Reconciliation against the workbook's cached values (PLAN 4.5). Re-runs reactively",
        "# whenever anything upstream changes, and reports per region rather than per sheet so a",
        "# failure localises to the column that moved.",
        "#",
        "# The keys are analysis operation ids, taken from each stage's `operations` in the plan.",
        "# kedge.reconcile reads the workbook, proposes one region per formula region carrying",
        "# cached values -- keyed by that same id -- and compares the two. Add an entry here as",
        "# each stage is translated; a stage with no entry is reported as unchecked, never as",
        "# passed.",
    ]
    entries = _reconciliation_values(plan, names)
    if entries:
        lines.append("reconciliation_values = {")
        lines.extend(entries)
        lines.append("}")
    else:
        lines.extend(
            [
                "#",
                "# No stage in this plan names an analysis operation, so there is nothing to map",
                "# yet. Entries look like: '<operation id>': <the cell that reproduces it>.",
                "reconciliation_values = {}",
            ]
        )
    lines.append(_RECONCILE_BODY)
    return [ScaffoldCell(name="reconciliation", code="\n".join(lines), role="reconcile")]
