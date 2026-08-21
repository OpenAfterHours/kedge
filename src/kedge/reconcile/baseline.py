"""The Excel side of a reconciliation: what the workbook says the answer is.

A baseline is a vector of the values Excel last calculated for one region, read out of the
``data_only`` view of the workbook (PLAN 1.5). It is the only evidence a reconciliation has,
which is why this module is so careful about the difference between three things that all
look like "no numbers":

===========================  ===============================================================
What was found               How it is reported
===========================  ===============================================================
The range holds cached       ``status == "present"`` — a real baseline, comparison proceeds.
values
Every cell in the range      ``status == "absent"`` — openpyxl calculates nothing, so this
came back ``None``           is a workbook written by a tool rather than saved by Excel.
                             The region degrades to NOT_RECONCILED, never to PASSED.
Some cells came back         ``status == "partial"`` — the rows that exist are compared, and
``None``                     the region is still not signed off.
===========================  ===============================================================

Nothing here guesses. A ``None`` is never read as a zero and never as a blank string,
because openpyxl cannot tell "Excel never cached anything here" from "Excel cached an empty
cell", and inventing either answer is how a reconciliation becomes vacuous.

**A region is not always a rectangle.** A column of formulas broken by an embedded subtotal
row, a blank row or a cell somebody typed over is one logical operation with several ranges,
and the enclosing rectangle is a different set of cells: it holds the subtotals, and it stops
short of the last rows by however many the breaks consumed. Reading it would hand the
comparison a vector that is the right *length* and the wrong *cells*, which is worse than no
baseline at all — the rows after the first break are compared against their neighbours, so a
correct conversion fails and an incorrect one can pass. So :class:`RegionSpec` carries the
ranges themselves and :func:`read_baseline` reads them in order, and where they cannot be
enumerated exactly the region degrades to NOT_RECONCILED rather than to the rectangle
(PLAN 4.5; CONVENTIONS non-negotiable 6).

References:
- PLAN.md 1.5 (the dual load and the cached-value caveat), 4.5 (the reconciliation loop).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from kedge.analysis.values import ERROR_VALUES, cell_ref
from kedge.analysis.workbook import parse_a1_range
from kedge.reconcile.model import NotReconciledReason, RegionSpec

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from kedge.analysis.model import LogicalOperation, WorkbookAnalysis
    from kedge.analysis.workbook import WorkbookHandle

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_BASELINE_ROWS",
    "BaselineVector",
    "column_letters",
    "infer_regions",
    "operation_ranges",
    "operation_reference",
    "read_baseline",
    "read_baselines",
    "region_cell_count",
    "split_reference",
]

#: How many rows of one region will be read before the baseline is marked truncated. Well
#: above the 500-row default on :meth:`~kedge.analysis.workbook.WorkbookHandle.read_range`,
#: which is sized for showing a model a sample rather than for reading a whole column.
MAX_BASELINE_ROWS = 250_000

_MAX_BASELINE_COLS = 256

_SHEET_QUALIFIED = re.compile(r"^(?:'(?P<quoted>(?:[^']|'')+)'|(?P<bare>[^'!]+))!(?P<a1>.+)$")


def column_letters(index: int) -> str:
    """Render a 1-based column index as its Excel letters."""
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def split_reference(reference: str) -> tuple[str | None, str]:
    """Split ``"Calc!G2:G501"`` into its sheet name and its A1 range.

    Quoted sheet names are unquoted and their doubled apostrophes collapsed, so
    ``"'Trade Data'!A1:A9"`` comes back as ``("Trade Data", "A1:A9")``.

    Args:
        reference: A sheet-qualified or bare A1 reference.

    Returns:
        ``(sheet, a1)``. The sheet is None when the reference carries no qualifier, which
        leaves it to the caller to supply one.
    """
    text = reference.strip().lstrip("=")
    match = _SHEET_QUALIFIED.match(text)
    if match is None:
        return None, text
    quoted = match.group("quoted")
    sheet = quoted.replace("''", "'") if quoted is not None else match.group("bare")
    return sheet.strip(), match.group("a1").strip()


# =============================================================================
# THE BASELINE VECTOR
# =============================================================================


@dataclass(frozen=True, slots=True)
class BaselineVector:
    """The values Excel cached for one region, in reading order.

    Internal to the comparison, so a frozen dataclass rather than a pydantic model: what
    reaches the report is a :class:`~kedge.reconcile.model.RegionResult`, not this.

    Example:
        >>> BaselineVector(spec_id="rwa", reference="Calc!G2:G4", sheet="Calc",
        ...                values=(1.0, None, 3.0), cells=("G2", "G3", "G4")).status
        'partial'
    """

    spec_id: str
    reference: str
    sheet: str
    values: tuple[Any, ...] = ()
    cells: tuple[str, ...] = ()
    truncated: bool = False
    """True when the range was longer than :data:`MAX_BASELINE_ROWS` and was cut short."""
    unreadable: bool = False
    """True when the sheet or the range could not be resolved at all."""
    incomplete: bool = False
    """True when the region's ranges do not account for every cell it holds.

    Distinct from :attr:`unreadable`, which is a reference nothing could resolve. This one
    resolved perfectly well and still describes the wrong set of cells, so no vector is read
    at all: a shorter one would be compared row against row with everything after the first
    gap out by however many cells the gap held.
    """

    @property
    def present_count(self) -> int:
        """How many cells in the range carry a cached value."""
        return sum(1 for value in self.values if value is not None)

    @property
    def missing_count(self) -> int:
        """How many cells in the range carry no cached value."""
        return len(self.values) - self.present_count

    @property
    def error_count(self) -> int:
        """How many cells hold an Excel error literal such as ``#DIV/0!``."""
        return sum(1 for value in self.values if isinstance(value, str) and value in ERROR_VALUES)

    @property
    def status(
        self,
    ) -> Literal["present", "partial", "absent", "empty", "unreadable", "incomplete"]:
        """What kind of baseline this is, in the terms the report uses."""
        if self.unreadable:
            return "unreadable"
        if self.incomplete:
            return "incomplete"
        if not self.values:
            return "empty"
        if self.present_count == 0:
            return "absent"
        return "present" if self.missing_count == 0 else "partial"

    @property
    def reason(self) -> NotReconciledReason | None:
        """The not-reconciled reason this baseline implies, or None when it is usable.

        A partial baseline returns a reason as well: the rows that exist are still compared,
        but the region cannot be signed off on a baseline with holes in it.
        """
        status = self.status
        if status == "unreadable":
            return NotReconciledReason.BASELINE_RANGE_UNREADABLE
        if status == "incomplete":
            return NotReconciledReason.BASELINE_RANGE_INCOMPLETE
        if status == "empty":
            return NotReconciledReason.BASELINE_RANGE_EMPTY
        if status == "absent":
            return NotReconciledReason.NO_CACHED_VALUES
        if status == "partial":
            return NotReconciledReason.PARTIAL_CACHED_VALUES
        return None

    def cell_at(self, index: int) -> str | None:
        """The sheet-qualified A1 reference of the row at ``index``, or None."""
        if 0 <= index < len(self.cells):
            return f"{self.sheet}!{self.cells[index]}"
        return None


# =============================================================================
# READING
# =============================================================================


@dataclass(frozen=True, slots=True)
class _Segment:
    """One resolved piece of a region: its A1 range and the rectangle it covers."""

    a1: str
    min_row: int
    min_col: int
    max_row: int
    max_col: int

    @property
    def rows(self) -> int:
        """How many rows the piece spans."""
        return self.max_row - self.min_row + 1

    @property
    def width(self) -> int:
        """How many columns the piece spans."""
        return self.max_col - self.min_col + 1

    @property
    def cell_count(self) -> int:
        """How many cells the piece covers."""
        return self.rows * self.width

    def overlaps(self, other: _Segment) -> bool:
        """Whether two pieces share a cell."""
        return not (
            self.max_row < other.min_row
            or other.max_row < self.min_row
            or self.max_col < other.min_col
            or other.max_col < self.min_col
        )


def _resolve_segments(spec: RegionSpec) -> tuple[str, list[_Segment]] | None:
    """Resolve a region's ranges to one sheet and a list of rectangles.

    Args:
        spec: The region. Its ``segments`` are read in the order given; an unqualified one
            inherits the sheet from ``reference``.

    Returns:
        ``(sheet, segments)``, or None when the ranges cannot be trusted to describe a
        vector: no sheet, a range that will not parse, two sheets in one region, or two
        pieces that overlap. Every one of those would put the wrong cell at some position,
        so none of them may fall back to reading a rectangle.
    """
    default_sheet, _ = split_reference(spec.reference)
    sheet: str | None = None
    segments: list[_Segment] = []
    for segment in spec.segments:
        named, a1 = split_reference(segment)
        named = named or default_sheet
        if named is None:
            logger.warning("region %r range %r names no sheet", spec.id, segment)
            return None
        if sheet is None:
            sheet = named
        elif named != sheet:
            logger.warning(
                "region %r spans two sheets, %r and %r; a baseline is one vector on one sheet",
                spec.id,
                sheet,
                named,
            )
            return None
        bounds = parse_a1_range(a1)
        if bounds is None:
            logger.warning("region %r has an unparseable range %r", spec.id, a1)
            return None
        segments.append(_Segment(a1, *bounds))

    if sheet is None or not segments:
        logger.warning("region %r names no range to read", spec.id)
        return None

    for index, segment in enumerate(segments):
        for other in segments[index + 1 :]:
            if segment.overlaps(other):
                logger.warning(
                    "region %r ranges %r and %r overlap, so its cells cannot be put in order",
                    spec.id,
                    segment.a1,
                    other.a1,
                )
                return None
    return sheet, segments


def read_baseline(
    handle: WorkbookHandle,
    spec: RegionSpec,
    *,
    max_rows: int = MAX_BASELINE_ROWS,
) -> BaselineVector:
    """Read the cached values for one region out of the workbook.

    A rectangle is flattened in reading order — down a column, along a row, row-major for a
    genuine block — because the notebook side of the comparison is a single vector. A region
    given as several ranges is read range by range in the order they are given, and the
    pieces are concatenated: the same vector, with the cells the region does not cover left
    out rather than read as if they were part of it.

    Args:
        handle: An open workbook handle.
        spec: The region to read. Its ``reference`` must name a sheet, either in the
            reference itself or not at all; ``ranges``, where given, are what is read.
        max_rows: Row ceiling for the region as a whole, not per range. A longer region is
            read up to the ceiling and marked truncated rather than silently shortened.

    Returns:
        A :class:`BaselineVector`. An unknown sheet or an unparseable reference comes back
        with ``unreadable`` set rather than raising: a region that cannot be located is a
        NOT_RECONCILED result, not a crash (CONVENTIONS non-negotiable 4). Ranges that do
        not add up to the region's own ``cell_count`` come back ``incomplete``, which is the
        same refusal for a different reason.
    """
    resolved = _resolve_segments(spec)
    if resolved is None:
        named, _ = split_reference(spec.reference)
        return BaselineVector(spec.id, spec.reference, named or "", unreadable=True)
    sheet, segments = resolved

    if sheet not in handle.sheet_names:
        logger.warning("region %r names sheet %r, which is not in the workbook", spec.id, sheet)
        return BaselineVector(spec.id, spec.reference, sheet, unreadable=True)

    covered = sum(segment.cell_count for segment in segments)
    if spec.cell_count is not None and covered != spec.cell_count:
        logger.warning(
            "region %r holds %d cells but its %d range(s) cover %d; refusing to compare a "
            "vector that would be out of step with the notebook",
            spec.id,
            spec.cell_count,
            len(segments),
            covered,
        )
        return BaselineVector(spec.id, spec.reference, sheet, incomplete=True)

    values: list[Any] = []
    cells: list[str] = []
    truncated = False
    budget = max_rows
    for index, segment in enumerate(segments):
        if budget <= 0:
            truncated = True
            break
        try:
            rows = handle.read_range(
                sheet, segment.a1, view="values", max_rows=budget, max_cols=_MAX_BASELINE_COLS
            )
        except Exception as exc:  # a broad catch: an unreadable range is a finding, not a crash
            logger.warning("could not read %s for region %r: %s", segment.a1, spec.id, exc)
            return BaselineVector(spec.id, spec.reference, sheet, unreadable=True)

        short = len(rows) < segment.rows
        if short and len(rows) >= budget:
            _append_rows(values, cells, segment, rows)
            truncated = True
            break
        if short and index < len(segments) - 1:
            # A piece that came back short would put every later piece one position early,
            # and a comparison one position out is a wrong answer rather than a missing one.
            # The gap is filled with the absence it is: None is a missing baseline, so the
            # region reports partial and cannot be signed off.
            logger.warning(
                "region %r: %s returned %d of %d rows; the gap is left as missing baseline",
                spec.id,
                segment.a1,
                len(rows),
                segment.rows,
            )
            _append_grid(values, cells, segment, rows)
        else:
            _append_rows(values, cells, segment, rows)
        budget -= len(rows)

    logger.debug(
        "region %r: read %d cells from %d range(s) in %s (%d cached)",
        spec.id,
        len(values),
        len(segments),
        spec.reference,
        sum(1 for value in values if value is not None),
    )
    return BaselineVector(
        spec_id=spec.id,
        reference=spec.reference,
        sheet=sheet,
        values=tuple(values),
        cells=tuple(cells),
        truncated=truncated,
    )


def _append_rows(
    values: list[Any], cells: list[str], segment: _Segment, rows: Sequence[Sequence[Any]]
) -> None:
    """Append exactly what the read returned, flattened row-major."""
    for row_offset, row in enumerate(rows):
        for col_offset, value in enumerate(row):
            values.append(value)
            cells.append(cell_ref(segment.min_row + row_offset, segment.min_col + col_offset))


def _append_grid(
    values: list[Any], cells: list[str], segment: _Segment, rows: Sequence[Sequence[Any]]
) -> None:
    """Append the segment's whole rectangle, with None wherever the read fell short."""
    for row_offset in range(segment.rows):
        row: Sequence[Any] = rows[row_offset] if row_offset < len(rows) else ()
        for col_offset in range(segment.width):
            values.append(row[col_offset] if col_offset < len(row) else None)
            cells.append(cell_ref(segment.min_row + row_offset, segment.min_col + col_offset))


def region_cell_count(spec: RegionSpec) -> int | None:
    """How many cells a region's ranges cover, or None when one of them will not parse.

    The extent the report and the frame matching reason about. It is the sum over the
    region's ranges rather than the area of ``reference``, because for a discontiguous
    region the rectangle in ``reference`` merely encloses it.

    Args:
        spec: The region.

    Returns:
        The cell count, or None when any range is unparseable.
    """
    total = 0
    for segment in spec.segments:
        _, a1 = split_reference(segment)
        bounds = parse_a1_range(a1)
        if bounds is None:
            return None
        min_row, min_col, max_row, max_col = bounds
        total += (max_row - min_row + 1) * (max_col - min_col + 1)
    return total


def read_baselines(
    handle: WorkbookHandle,
    specs: Iterable[RegionSpec],
    *,
    max_rows: int = MAX_BASELINE_ROWS,
) -> dict[str, BaselineVector]:
    """Read every region's baseline, keyed by region id.

    Args:
        handle: An open workbook handle.
        specs: The regions to read.
        max_rows: Row ceiling per region.

    Returns:
        One :class:`BaselineVector` per spec, including the unreadable ones.
    """
    return {spec.id: read_baseline(handle, spec, max_rows=max_rows) for spec in specs}


# =============================================================================
# INFERRING REGIONS FROM AN ANALYSIS
# =============================================================================


def operation_ranges(operation: LogicalOperation) -> list[str]:
    """The exact ranges a discontiguous logical operation covers, in reading order.

    The analyser already records them — a formula column broken by two subtotal rows is
    three ranges — and they are the only description of the region that is neither too big
    nor too small.

    Args:
        operation: A region from :class:`~kedge.analysis.model.WorkbookAnalysis`.

    Returns:
        The sheet-qualified ranges, or an empty list when the operation is a single
        rectangle (its ``reference`` already says everything) or when the recorded ranges
        do not parse.
    """
    if len(operation.ranges) < 2:
        return []
    for entry in operation.ranges:
        _, a1 = split_reference(entry)
        if parse_a1_range(a1) is None:
            logger.warning("operation %r has an unparseable range %r", operation.id, entry)
            return []
    return list(operation.ranges)


def _enclosing_range(operation: LogicalOperation) -> str | None:
    """The one rectangle that contains every cell of a discontiguous operation."""
    rows: list[int] = []
    cols: list[int] = []
    for entry in operation.ranges:
        sheet, a1 = split_reference(entry)
        if sheet is not None and sheet != operation.sheet:
            return None
        bounds = parse_a1_range(a1)
        if bounds is None:
            return None
        rows.extend((bounds[0], bounds[2]))
        cols.extend((bounds[1], bounds[3]))
    if not rows:
        return None
    top_left = cell_ref(min(rows), min(cols))
    bottom_right = cell_ref(max(rows), max(cols))
    sheet = _quote_sheet(operation.sheet)
    if top_left == bottom_right:
        return f"{sheet}!{top_left}"
    return f"{sheet}!{top_left}:{bottom_right}"


def operation_reference(operation: LogicalOperation) -> str | None:
    """The sheet-qualified A1 range a logical operation occupies.

    The analyser records where a region starts and how many cells it covers, not the
    rectangle it fills, so the rectangle is reconstructed here from the anchor, the cell
    count and the orientation.

    Where the operation is discontiguous there is no single range that is the operation and
    nothing else, and this returns the rectangle that *encloses* it — which is bigger than
    the operation, and is honest about being bigger. It is a label for the panel, never
    something to read a baseline from: :func:`operation_ranges` is what covers the cells
    themselves, and :func:`infer_regions` puts both on the spec.

    Args:
        operation: A region from :class:`~kedge.analysis.model.WorkbookAnalysis`.

    Returns:
        A reference such as ``"Calc!G2:G501"``, or None for a block whose shape cannot be
        recovered from a cell count alone.
    """
    if len(operation.ranges) > 1:
        enclosing = _enclosing_range(operation)
        if enclosing is not None:
            return enclosing

    bounds = parse_a1_range(operation.anchor)
    if bounds is None:
        return None
    row, col = bounds[0], bounds[1]
    count = max(operation.cell_count, 1)
    sheet = _quote_sheet(operation.sheet)

    if count == 1 or operation.orientation == "single":
        return f"{sheet}!{operation.anchor}"
    if operation.orientation == "column":
        return f"{sheet}!{operation.anchor}:{cell_ref(row + count - 1, col)}"
    if operation.orientation == "row":
        return f"{sheet}!{operation.anchor}:{cell_ref(row, col + count - 1)}"
    return None


def _quote_sheet(name: str) -> str:
    """Quote a sheet name for an A1 reference when it needs it."""
    if re.fullmatch(r"[A-Za-z0-9_]+", name):
        return name
    return "'" + name.replace("'", "''") + "'"


def _normalise_name(text: str) -> str:
    """Turn a spreadsheet header into the column name a notebook would give it."""
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", text.strip().lower()).strip("_")
    return slug or ""


def _header_above(handle: WorkbookHandle, sheet: str, anchor: str) -> str | None:
    """The text in the cell directly above a region's anchor, if there is any."""
    bounds = parse_a1_range(anchor)
    if bounds is None or bounds[0] <= 1:
        return None
    above = cell_ref(bounds[0] - 1, bounds[1])
    rows = handle.read_range(sheet, above, view="values", max_rows=1, max_cols=1)
    if not rows or not rows[0]:
        return None
    value = rows[0][0]
    return value.strip() if isinstance(value, str) and value.strip() else None


def infer_regions(
    handle: WorkbookHandle,
    analysis: WorkbookAnalysis,
    *,
    min_cells: int = 2,
    limit: int = 200,
) -> list[RegionSpec]:
    """Propose regions to reconcile from a workbook analysis.

    Every column region that carries cached values is a candidate: it is a whole-column
    formula fill, which is exactly the shape a translated notebook column has. The header
    cell above the anchor names it, and that name is what the notebook column is matched
    against.

    This is a proposal, not a contract. A notebook that says which of its columns
    corresponds to which range — see :func:`kedge.reconcile.compare.declared_regions` — is
    always preferred, because a guess that silently matches the wrong column would be worse
    than no match at all.

    Args:
        handle: An open workbook handle, used to read the header row.
        analysis: The analysis of that same workbook.
        min_cells: Ignore regions smaller than this. A one-cell region is a scalar metric
            and has no notebook column to match against.
        limit: Stop after this many regions, so a pathological workbook cannot produce a
            report nobody can read.

    Returns:
        Region specs in workbook order, each carrying the inferred column name. Empty when
        the workbook has no cached values at all.
    """
    specs: list[RegionSpec] = []
    seen: set[str] = set()
    for operation in analysis.operations:
        if len(specs) >= limit:
            logger.info("stopping region inference at the limit of %d", limit)
            break
        if not operation.cached_values_present or operation.cell_count < min_cells:
            continue
        if operation.orientation not in ("column", "row"):
            continue
        reference = operation_reference(operation)
        if reference is None or reference in seen:
            continue
        seen.add(reference)
        header = _header_above(handle, operation.sheet, operation.anchor)
        specs.append(
            RegionSpec(
                id=operation.id,
                reference=reference,
                ranges=operation_ranges(operation),
                # The analyser's own count, carried so that ranges which do not add up to it
                # -- it renders at most twenty of them, and a column broken by more breaks
                # than that has the rest missing -- degrade the region rather than shorten
                # its baseline.
                cell_count=operation.cell_count,
                column=_normalise_name(header) if header else None,
                label=header or operation.id,
                description=operation.description,
                orientation=operation.orientation,  # type: ignore[arg-type]
            )
        )

    logger.info("inferred %d reconcilable regions from the analysis", len(specs))
    return specs


def specs_from_mapping(entries: Sequence[Any]) -> list[RegionSpec]:
    """Coerce a notebook's declared region list into :class:`RegionSpec` objects.

    Accepts the specs themselves, or plain dicts, so a notebook can declare its regions
    without importing anything from kedge.

    Args:
        entries: A sequence of :class:`RegionSpec` or mapping objects.

    Returns:
        The specs, skipping entries that are neither.
    """
    specs: list[RegionSpec] = []
    for index, entry in enumerate(entries):
        if isinstance(entry, RegionSpec):
            specs.append(entry)
            continue
        if isinstance(entry, dict):
            payload = dict(entry)
            payload.setdefault("id", payload.get("column") or f"region_{index + 1}")
            try:
                specs.append(RegionSpec(**payload))
            except (TypeError, ValueError) as exc:
                logger.warning("ignoring declared region %d: %s", index, exc)
            continue
        logger.warning("ignoring declared region %d: %r is not a region spec", index, entry)
    return specs
