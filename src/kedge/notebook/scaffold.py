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

Nothing here writes to a notebook without an approved plan. :func:`scaffold_notebook` checks
``plan.approval`` and refuses, and there is no parameter to talk it out of that.

**Driver dependency.** ``kedge.notebook.driver`` is the only module permitted to touch
``marimo._code_mode``, and it is owned elsewhere. This module depends on the narrow
:class:`CellCreator` protocol below — one method, matching the ``create_cell`` signature
verified in ``docs/marimo-api.md`` §2. At the time of writing ``driver.py`` does not exist yet;
if its surface differs, this protocol is the single place to adapt.

References:
- PLAN.md 2.2 (checkpoints), 2.5 (polars house rules), 2.6 (Excel semantics), 2.8 (hand-in
  head), 4.5 (reconciliation), M2 step 4; docs/marimo-api.md §2.
"""

from __future__ import annotations

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

HEAD_CELL_NAMES = ("kedge_setup", "handin_source", "handin", "handin_frame", "handin_check")
"""The fixed head, in emission order (PLAN 2.8).

The frame is loaded before the contract check because the check reports on the schema that
arrived. marimo would resolve the order from the dataflow graph either way; emitting them in
dependency order simply means the notebook reads top to bottom.
"""

TAIL_CELL_NAMES = ("reconciliation",)
"""The fixed tail (PLAN 4.5)."""

_RESERVED = {
    *HEAD_CELL_NAMES,
    *TAIL_CELL_NAMES,
    "handin_drop",
    "handin_pick",
    "reconciliation_frame",
    "mo",
    "pl",
    "datetime",
    "hashlib",
    "pathlib",
    "HANDIN_DIR",
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

    async def create_cell(self, code: str, *, name: str, hide_code: bool = False) -> str:
        """Create one cell and return its id."""
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


def _name_map(plan: ProcessPlan) -> dict[str, str]:
    """Map every stage id to its cell name, assigned in emission order."""
    names: dict[str, str] = {}
    for stage in plan.ordered_stages():
        names[stage.id] = cell_name_for(stage.id, names.values())
    return names


# =============================================================================
# BUILDING
# =============================================================================


def build_cells(
    plan: ProcessPlan,
    *,
    handins_dir: Path | None = None,
    allow_unapproved: bool = False,
) -> list[ScaffoldCell]:
    """Turn an approved plan into the cells that implement it.

    Pure: builds and returns cell bodies without touching a notebook, so the CLI can preview a
    scaffold and tests can assert on it with no live kernel.

    Args:
        plan: The approved plan.
        handins_dir: Where dropped hand-ins are persisted. Defaults to ``handins`` relative to
            the notebook.
        allow_unapproved: Render a preview of an unapproved plan. This writes nothing;
            :func:`scaffold_notebook` has no equivalent escape hatch, so an unapproved plan can
            be looked at but never scaffolded.

    Returns:
        The cells in creation order: fixed head, stages in dependency order, fixed tail.

    Raises:
        PlanNotApprovedError: when the plan is not approved and ``allow_unapproved`` is not set.
        ScaffoldError: when a generated cell would breach the house rules.
    """
    if not plan.approval.approved and not allow_unapproved:
        raise _not_approved(plan)

    names = _name_map(plan)
    checkpoints = {stage.id for stage in plan.stages if stage.is_checkpoint}
    cells: list[ScaffoldCell] = list(_head_cells(plan, handins_dir))

    ordered = plan.ordered_stages()
    for index, stage in enumerate(ordered, start=1):
        if stage.is_checkpoint:
            cells.extend(_checkpoint_cells(stage, index, len(ordered), names))
            continue
        cells.append(_stage_cell(stage, index, len(ordered), names, checkpoints))

    cells.extend(_tail_cells(plan, names))

    for cell in cells:
        if _BANNED_IMPORT.search(cell.code):
            msg = (
                f"cell {cell.name!r} would import pandas, which kedge does not permit anywhere. "
                f"Generated code is polars (PLAN 2.5)"
            )
            raise ScaffoldError(msg)
    logger.info("built %d cell(s) from plan v%d for %s", len(cells), plan.version, plan.workbook)
    return cells


async def scaffold_notebook(
    plan: ProcessPlan,
    driver: CellCreator,
    *,
    handins_dir: Path | None = None,
) -> list[str]:
    """Write an approved plan into the notebook, one named cell per stage.

    Refuses an unapproved plan, with no override. That refusal is the structural half of the
    review gate: approval is state on the plan, and this is the one place that consumes it.

    Args:
        plan: The approved plan.
        driver: The notebook driver, or anything satisfying :class:`CellCreator`.
        handins_dir: Where dropped hand-ins are persisted.

    Returns:
        The created cell ids, in creation order.

    Raises:
        PlanNotApprovedError: when ``plan.approval.state`` is not ``approved``.
    """
    if not plan.approval.approved:
        raise _not_approved(plan)

    cells = build_cells(plan, handins_dir=handins_dir)
    created: list[str] = []
    for cell in cells:
        cell_id = await driver.create_cell(cell.code, name=cell.name, hide_code=False)
        created.append(cell_id)
    logger.info("scaffolded %d cell(s) from plan v%d", len(created), plan.version)
    return created


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
import hashlib
import pathlib

import marimo as mo
import polars as pl

# Registers the Excel-semantics expression namespace: col("x").xl.round(2), .xl.add, .xl.div.
# Excel and polars disagree about rounding at .5, empty cells in arithmetic, and division by
# zero. Every one of those disagreements produces silently wrong numbers rather than an error
# (PLAN 2.6), so translations state their intent through this namespace rather than
# open-coding the workaround where nobody can grep for it.
from kedge import xl as _kedge_xl

HANDIN_DIR = pathlib.Path({store!r})"""

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

_HANDIN_CELL = """# Normalise either entry point into one record with a stable path and a hash. The hash is
# what makes the audit line defensible: this run consumed this file.
#
# TODO(kedge): replace this body with `kedge.ingest.receive(...)` when M5 lands (PLAN 2.8).
# The record shape below is deliberately the shape that returns, so nothing downstream moves.
_dropped = handin_drop.value
_picked = handin_pick.value
mo.stop(
    not _dropped and not _picked,
    mo.md("**Waiting for a hand-in.** Drop a file above, or select one."),
)

if _picked:
    _path = pathlib.Path(_picked[0].path)
    _origin = "selected"
else:
    _upload = _dropped[0]
    _directory = HANDIN_DIR / datetime.date.today().isoformat()
    _directory.mkdir(parents=True, exist_ok=True)
    _path = _directory / f"{hashlib.sha256(_upload.contents).hexdigest()[:12]}-{_upload.name}"
    _path.write_bytes(_upload.contents)
    _origin = "dropped"

handin = {
    "path": _path,
    "original_name": _path.name,
    "sha256": hashlib.sha256(_path.read_bytes()).hexdigest(),
    "size_bytes": _path.stat().st_size,
    "received_at": datetime.datetime.now(datetime.UTC),
    "source": _origin,
}
handin"""

_FRAME_CELL = """# LazyFrame from the start. Nothing is materialised until a boundary asks for it, which is
# what keeps the notebook's cell graph and polars' query plan aligned, and what makes this
# scale past the point where Excel gave up (PLAN 2.5).
# pl.read_excel defaults to the calamine engine: fast, and no Excel install needed.
if handin["path"].suffix.lower() in (".csv", ".txt", ".tsv"):
    handin_frame = pl.scan_csv(handin["path"])
else:
    handin_frame = pl.read_excel(handin["path"]).lazy()

handin_frame"""

_CHECK_CELL = """# The contract check. This is what turns "a notebook" into "a controlled process": the first
# thing that runs after ingestion, failing loudly on a hand-in that is not what was agreed
# (PLAN 2.8, M5).
#
# TODO(kedge): replace with `kedge.contracts.validate(handin, contract)` when M5 lands. Until
# then this reports the shape that arrived, so drift is visible from the very first run.
# collect_schema() reads the plan, not the data: no rows are materialised here.
_schema = handin_frame.collect_schema()
handin_check = mo.md(
    "\\n".join(
        [
            "### Hand-in",
            f"`{handin['original_name']}`  sha256 `{handin['sha256'][:16]}...`  ",
            f"**{len(_schema)} columns:** "
            + ", ".join(f"`{_name}` {_dtype}" for _name, _dtype in _schema.items()),
            "",
            "_No contract is configured yet, so nothing above is enforced._",
        ]
    )
)
handin_check"""


def _head_cells(plan: ProcessPlan, handins_dir: Path | None) -> list[ScaffoldCell]:
    """The selector, the ingest, the load, and the contract check. Same in every notebook."""
    store = str(handins_dir if handins_dir is not None else Path("handins"))
    return [
        ScaffoldCell(
            name="kedge_setup",
            role="setup",
            code=_SETUP_TEMPLATE.format(version=plan.version, workbook=plan.workbook, store=store),
        ),
        ScaffoldCell(name="handin_source", role="handin", code=_SOURCE_CELL),
        ScaffoldCell(name="handin", role="handin", code=_HANDIN_CELL),
        ScaffoldCell(name="handin_frame", role="handin", code=_FRAME_CELL),
        ScaffoldCell(name="handin_check", role="handin", code=_CHECK_CELL),
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


def _tail_cells(plan: ProcessPlan, names: dict[str, str]) -> list[ScaffoldCell]:
    """The reconciliation panel: the artifact that makes the notebook a controlled process."""
    ordered = [stage for stage in plan.ordered_stages() if not stage.is_checkpoint]
    final = ordered[-1] if ordered else None
    translated = len(ordered)

    lines = [
        "# Reconciliation against the workbook's cached values (PLAN 4.5). Re-runs reactively",
        "# whenever anything upstream changes, and reports per region rather than per sheet so a",
        "# failure localises. Where the workbook carries no cached values this must report",
        '# "not reconciled" -- never "passed".',
        "#",
        "# TODO(kedge): call kedge.reconcile once M4.5 lands. Until then it states, accurately,",
        "# that nothing has been verified.",
    ]
    if final is None:
        lines.append("reconciliation_frame = pl.DataFrame()")
    elif final.kind is StageKind.OUTPUT:
        lines.append(f"reconciliation_frame = {names[final.id]}  # already materialised")
    else:
        lines.append(
            f"reconciliation_frame = {names[final.id]}.collect()  # boundary: the only collect"
        )

    lines.extend(
        [
            "reconciliation = mo.md(",
            '    "\\n".join(',
            "        [",
            '            "### Reconciliation",',
            f'            "**NOT RECONCILED.** {translated} translated stage(s) have not been "',
            '            "checked against the workbook\'s cached values.",',
            '            "",',
            '            f"Final frame: {reconciliation_frame.height} rows x "',
            '            f"{reconciliation_frame.width} columns.",',
            "        ]",
            "    )",
            ")",
            "reconciliation",
        ]
    )
    return [ScaffoldCell(name="reconciliation", code="\n".join(lines), role="reconcile")]
