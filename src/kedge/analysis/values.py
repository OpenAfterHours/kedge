"""The dual-view sheet scan: formulas and the values Excel last cached, zipped together.

One pass over each sheet reads the formula view and the ``data_only`` view in lockstep and
accumulates everything the rest of the analyser needs: the R1C1 form of every formula cell,
the literal cells around them, per-column type tallies, error values, and how much of the
value cache is actually present.

Two design constraints shape this module:

- **Memory is bounded per cell, not per workbook.** A 500,000-formula workbook must not
  produce a per-cell Python object graph. R1C1 strings are interned (a whole-column fill
  is one string held half a million times), the A1 formula text is never retained — it is
  reconstructed from R1C1 on demand via :func:`~kedge.analysis.formulas.to_a1` — and the
  literal map is capped.
- **An absent value cache is reported, never assumed.** openpyxl calculates nothing, so a
  workbook written by a tool rather than saved by Excel returns ``None`` for every formula
  cell. A vacuous reconciliation that reports "passed" is the worst failure mode in this
  project, so coverage is computed honestly here and turned into a loud finding upstream
  (PLAN §1.5, §4.5).

References:
- PLAN.md §1.5 (dual load), §2.1 (R1C1 compression), §4.5 (reconciliation baseline).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Any

from kedge.analysis.formulas import formula_text, to_r1c1
from kedge.analysis.model import CachedValueCoverage

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping

    from kedge.analysis.workbook import WorkbookHandle

logger = logging.getLogger(__name__)

__all__ = [
    "ERROR_VALUES",
    "SheetScan",
    "build_scan",
    "cached_value_coverage",
    "cell_ref",
    "scan_sheet",
]

ERROR_VALUES = frozenset(
    {
        "#REF!",
        "#VALUE!",
        "#DIV/0!",
        "#N/A",
        "#NAME?",
        "#NULL!",
        "#NUM!",
        "#SPILL!",
        "#CALC!",
        "#GETTING_DATA",
    }
)
"""The Excel error literals. A cached value equal to one of these is a live error."""

_DEFAULT_LITERAL_CAP = 250_000
_HEAD_ROWS = 25
_LONG_TEXT = 60
_ERROR_CELL_CAP = 200
_TOTALS_ROW_CAP = 50
_TOTAL_LABEL_RE = re.compile(r"^\s*(grand\s+|sub[\s-]*)?total", re.IGNORECASE)
_NUMERIC_TEXT_RE = re.compile(r"^\s*[-+(]?\s*[\$£€]?\s*[0-9][0-9,]*(\.[0-9]+)?\s*%?\s*\)?\s*$")


def _column_letters(index: int) -> str:
    """1-based column index to letters."""
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _column_index(letters: str) -> int:
    """Column letters to a 1-based index."""
    index = 0
    for char in letters.upper():
        index = index * 26 + (ord(char) - 64)
    return index


def cell_ref(row: int, col: int) -> str:
    """Render a 1-based (row, column) pair as an A1 reference."""
    return f"{_column_letters(col)}{row}"


# =============================================================================
# THE SCAN
# =============================================================================


@dataclass(slots=True)
class SheetScan:
    """Everything one pass over a sheet produced.

    This is the shared intermediate between :mod:`kedge.analysis.values` and the modules
    that reason over it — regions, classification, findings and the graph all read a scan
    rather than the workbook.
    """

    name: str
    max_row: int = 0
    max_col: int = 0

    formulas: dict[tuple[int, int], str] = field(default_factory=dict)
    """(row, col) -> interned R1C1 string. The A1 text is not retained."""
    group_cached: dict[str, list[int]] = field(default_factory=dict)
    """R1C1 -> ``[cached_present, total]`` for the cells sharing that formula."""
    literals: dict[tuple[int, int], Any] = field(default_factory=dict)
    """Non-formula, non-empty cells. Capped; see :attr:`literals_truncated`."""
    literals_truncated: bool = False

    formula_count: int = 0
    cached_present_count: int = 0
    value_cell_count: int = 0
    text_cell_count: int = 0
    numeric_cell_count: int = 0
    date_cell_count: int = 0
    bool_cell_count: int = 0
    long_text_cell_count: int = 0
    """Text cells long enough to be prose rather than a label (see :data:`_LONG_TEXT`)."""
    text_length_total: int = 0

    error_cells: list[tuple[str, Any]] = field(default_factory=list)
    error_cell_count: int = 0
    text_number_columns: dict[int, int] = field(default_factory=dict)
    """Column index -> count of text cells whose content is really a number."""
    column_kinds: dict[int, dict[str, int]] = field(default_factory=dict)
    """Column index -> tally of ``number`` / ``text`` / ``date`` / ``bool`` / ``formula``."""

    head_rows: list[list[Any]] = field(default_factory=list)
    """The first few rows of the value view, for header and preamble detection."""
    totals_rows: list[int] = field(default_factory=list)
    """Rows whose leading label reads like a totals row."""
    last_populated_row: int = 0
    rows_truncated: bool = False
    """True when the scan hit its row cap and stopped short of the sheet's end."""

    @property
    def blank_cell_count(self) -> int:
        """Cells inside the used rectangle holding neither a value nor a formula."""
        used = self.max_row * self.max_col
        return max(0, used - self.value_cell_count - self.formula_count)

    @property
    def used_range(self) -> str | None:
        """The used rectangle in A1 terms, or None for an empty sheet."""
        if self.max_row < 1 or self.max_col < 1:
            return None
        return f"A1:{cell_ref(self.max_row, self.max_col)}"

    def r1c1_at(self, row: int, col: int) -> str | None:
        """The R1C1 formula at a cell, or None when the cell holds no formula."""
        return self.formulas.get((row, col))

    def group_coordinates(self) -> dict[str, list[tuple[int, int]]]:
        """Invert :attr:`formulas` into R1C1 -> the coordinates sharing it.

        Built on demand rather than held, because the inverted view doubles the memory of
        the largest structure in the analyser.
        """
        groups: dict[str, list[tuple[int, int]]] = {}
        for coordinate, r1c1 in self.formulas.items():
            groups.setdefault(r1c1, []).append(coordinate)
        for coordinates in groups.values():
            coordinates.sort()
        return groups


def _classify(value: Any) -> str:
    """Bucket a cached value into a coarse kind."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, (datetime, date, time)):
        return "date"
    if isinstance(value, str):
        return "error" if value in ERROR_VALUES else "text"
    return "other"


def _looks_numeric(text: str) -> bool:
    """Whether a text cell is really a number that Excel is storing as a string."""
    return bool(_NUMERIC_TEXT_RE.match(text)) and any(char.isdigit() for char in text)


class _Accumulator:
    """Mutable state for one sheet scan, kept out of :class:`SheetScan`'s public surface."""

    def __init__(self, name: str, *, literal_cap: int) -> None:
        self.scan = SheetScan(name=name)
        self.literal_cap = literal_cap
        self.intern: dict[str, str] = {}

    def add_formula(self, row: int, col: int, r1c1: str, cached: Any) -> None:
        """Record one formula cell and whether Excel left a cached result behind."""
        scan = self.scan
        r1c1 = self.intern.setdefault(r1c1, r1c1)
        scan.formulas[(row, col)] = r1c1
        scan.formula_count += 1
        counts = scan.group_cached.get(r1c1)
        if counts is None:
            counts = [0, 0]
            scan.group_cached[r1c1] = counts
        counts[1] += 1
        if cached is not None:
            counts[0] += 1
            scan.cached_present_count += 1
        scan.column_kinds.setdefault(col, {})
        scan.column_kinds[col]["formula"] = scan.column_kinds[col].get("formula", 0) + 1
        if isinstance(cached, str) and cached in ERROR_VALUES:
            self.add_error(row, col, cached)

    def add_literal(self, row: int, col: int, value: Any) -> None:
        """Record one non-formula cell."""
        scan = self.scan
        scan.value_cell_count += 1
        kind = _classify(value)
        kinds = scan.column_kinds.setdefault(col, {})
        kinds[kind] = kinds.get(kind, 0) + 1

        if kind == "number":
            scan.numeric_cell_count += 1
        elif kind == "date":
            scan.date_cell_count += 1
        elif kind == "bool":
            scan.bool_cell_count += 1
        elif kind == "error":
            self.add_error(row, col, value)
        elif kind == "text":
            scan.text_cell_count += 1
            scan.text_length_total += len(value)
            if len(value) >= _LONG_TEXT:
                scan.long_text_cell_count += 1
            if _looks_numeric(value):
                scan.text_number_columns[col] = scan.text_number_columns.get(col, 0) + 1
            if (
                col <= 3
                and _TOTAL_LABEL_RE.match(value)
                and len(scan.totals_rows) < _TOTALS_ROW_CAP
                and (not scan.totals_rows or scan.totals_rows[-1] != row)
            ):
                scan.totals_rows.append(row)

        if len(scan.literals) < self.literal_cap:
            scan.literals[(row, col)] = value
        else:
            scan.literals_truncated = True

    def add_error(self, row: int, col: int, value: Any) -> None:
        """Record an error value, keeping only a bounded sample."""
        self.scan.error_cell_count += 1
        if len(self.scan.error_cells) < _ERROR_CELL_CAP:
            self.scan.error_cells.append((cell_ref(row, col), value))

    def finish(self, max_row: int, max_col: int) -> SheetScan:
        """Close out the scan with the sheet's used extent."""
        self.scan.max_row = max_row
        self.scan.max_col = max_col
        return self.scan


def _dual_rows(
    formula_sheet: Any,
    value_sheet: Any,
    *,
    max_rows: int | None,
) -> Iterator[tuple[int, int, tuple[Any, ...], tuple[Any, ...]]]:
    """Yield ``(row_index, first_col, formula_values, cached_values)`` for each row.

    Both views parse the same XML with the same dimensions, so openpyxl pads them
    identically and the two row tuples line up position for position. Where the sheet has
    no ``<dimension>`` element the rows are ragged, and the slower coordinate-carrying path
    is used instead.
    """
    min_row = getattr(formula_sheet, "min_row", 1) or 1
    min_col = getattr(formula_sheet, "min_column", 1) or 1
    known = bool(getattr(formula_sheet, "max_row", None)) and bool(
        getattr(formula_sheet, "max_column", None)
    )

    if known:
        rows = zip(
            formula_sheet.iter_rows(values_only=True),
            value_sheet.iter_rows(values_only=True),
            strict=False,
        )
        for offset, (formula_row, value_row) in enumerate(rows):
            row_index = min_row + offset
            if max_rows is not None and row_index > max_rows:
                return
            yield row_index, min_col, formula_row, value_row
        return

    logger.debug(
        "sheet %r has no dimension element; using the coordinate path", formula_sheet.title
    )
    rows = zip(formula_sheet.iter_rows(), value_sheet.iter_rows(values_only=True), strict=False)
    for formula_row, value_row in rows:
        if not formula_row:
            continue
        row_index = next((cell.row for cell in formula_row if getattr(cell, "row", None)), None)
        first_col = next((cell.column for cell in formula_row if getattr(cell, "column", None)), 1)
        if row_index is None:
            continue
        if max_rows is not None and row_index > max_rows:
            return
        yield row_index, first_col, tuple(cell.value for cell in formula_row), value_row


def scan_sheet(
    handle: WorkbookHandle,
    sheet: str,
    *,
    max_rows: int | None = None,
    literal_cap: int = _DEFAULT_LITERAL_CAP,
) -> SheetScan:
    """Read one sheet in both views and accumulate the facts the analyser needs.

    Args:
        handle: The open workbook handle.
        sheet: The sheet name.
        max_rows: Stop after this row, marking the scan truncated. None reads the sheet.
        literal_cap: How many literal cells to retain for inconsistency detection.

    Returns:
        A :class:`SheetScan`. A sheet that cannot be read at all comes back empty rather
        than raising, so one damaged sheet never costs the whole analysis.
    """
    accumulator = _Accumulator(sheet, literal_cap=literal_cap)
    names = handle.defined_name_keys
    try:
        formula_sheet = handle.formula_sheet(sheet)
        value_sheet = handle.value_sheet(sheet)
    except (KeyError, ValueError) as exc:
        logger.warning("cannot read sheet %r: %s", sheet, exc)
        return accumulator.finish(0, 0)

    max_row_seen = 0
    max_col_seen = 0
    try:
        for row_index, first_col, formula_row, value_row in _dual_rows(
            formula_sheet, value_sheet, max_rows=max_rows
        ):
            width = len(formula_row)
            if len(accumulator.scan.head_rows) < _HEAD_ROWS:
                accumulator.scan.head_rows.append(list(value_row[: min(width, 64)]))
            for offset in range(width):
                raw = formula_row[offset]
                if raw is None:
                    continue
                col = first_col + offset
                cached = value_row[offset] if offset < len(value_row) else None
                formula = formula_text(raw)
                if formula is not None:
                    accumulator.add_formula(
                        row_index, col, to_r1c1(formula, row_index, col, names=names), cached
                    )
                else:
                    accumulator.add_literal(row_index, col, raw)
                max_row_seen = row_index
                max_col_seen = max(max_col_seen, col)
                accumulator.scan.last_populated_row = row_index
    except Exception as exc:
        logger.warning("scan of sheet %r stopped early: %s", sheet, exc)
        accumulator.scan.rows_truncated = True

    declared_rows = getattr(formula_sheet, "max_row", None) or 0
    declared_cols = getattr(formula_sheet, "max_column", None) or 0
    if max_rows is not None and declared_rows > max_rows:
        accumulator.scan.rows_truncated = True
    logger.debug(
        "scanned %r: %d formula cells, %d literal cells",
        sheet,
        accumulator.scan.formula_count,
        accumulator.scan.value_cell_count,
    )
    return accumulator.finish(
        max(max_row_seen, 0 if accumulator.scan.rows_truncated else declared_rows),
        max(max_col_seen, declared_cols),
    )


def build_scan(
    sheet: str,
    cells: Mapping[str, Any],
    *,
    cached: Mapping[str, Any] | None = None,
    literal_cap: int = _DEFAULT_LITERAL_CAP,
) -> SheetScan:
    """Build a scan from an explicit ``{"A1": value}`` map rather than from a workbook.

    The same accumulator the real scan uses, reachable without a file. Used by the tests and
    by any caller that already holds cell values in memory.

    Args:
        sheet: Sheet name to record in the scan.
        cells: A1 reference to value. A string starting with ``=`` is treated as a formula.
        cached: A1 reference to the value Excel cached for that formula cell. Absent keys
            mean no cached value, which is exactly what an openpyxl-written workbook gives.
        literal_cap: How many literal cells to retain.

    Returns:
        A :class:`SheetScan` indistinguishable from one produced by :func:`scan_sheet`.
    """
    accumulator = _Accumulator(sheet, literal_cap=literal_cap)
    cached = cached or {}
    max_row = 0
    max_col = 0
    parsed: list[tuple[int, int, str, Any]] = []
    for ref, value in cells.items():
        match = re.match(r"^\$?([A-Za-z]{1,3})\$?([0-9]{1,7})$", ref.strip())
        if match is None:
            raise ValueError(f"build_scan: {ref!r} is not a single-cell A1 reference")
        col = _column_index(match.group(1))
        row = int(match.group(2))
        parsed.append((row, col, ref, value))
        max_row = max(max_row, row)
        max_col = max(max_col, col)

    head: dict[int, dict[int, Any]] = {}
    for row, col, ref, value in sorted(parsed):
        formula = formula_text(value)
        if formula is not None:
            accumulator.add_formula(row, col, to_r1c1(formula, row, col), cached.get(ref))
            head.setdefault(row, {})[col] = cached.get(ref)
        else:
            accumulator.add_literal(row, col, value)
            head.setdefault(row, {})[col] = value
        accumulator.scan.last_populated_row = max(accumulator.scan.last_populated_row, row)

    for row in range(1, min(max_row, _HEAD_ROWS) + 1):
        values = head.get(row, {})
        accumulator.scan.head_rows.append([values.get(col) for col in range(1, max_col + 1)])
    return accumulator.finish(max_row, max_col)


# =============================================================================
# CACHED VALUE COVERAGE
# =============================================================================


def cached_value_coverage(scans: Iterable[SheetScan]) -> CachedValueCoverage:
    """Measure how much of the workbook's value cache survived.

    Coverage is the proportion of formula cells for which Excel left a calculated value
    behind. Zero coverage over a non-empty formula population means the workbook was
    written by a tool rather than saved by Excel, and reconciliation is impossible
    (PLAN §1.5).

    Args:
        scans: One scan per sheet.

    Returns:
        The coverage record. ``not_applicable`` when the workbook has no formulas at all,
        which is a different statement from "the cache is missing".
    """
    formula_cells = 0
    present = 0
    for scan in scans:
        formula_cells += scan.formula_count
        present += scan.cached_present_count

    if formula_cells == 0:
        return CachedValueCoverage(status="not_applicable")

    coverage = present / formula_cells
    if present == 0:
        status: str = "absent"
    elif coverage >= 0.995:
        status = "present"
    else:
        status = "partial"
    return CachedValueCoverage(
        formula_cell_count=formula_cells,
        cached_present_count=present,
        coverage=coverage,
        status=status,  # type: ignore[arg-type]
    )
