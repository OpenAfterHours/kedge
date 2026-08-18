"""Grouping R1C1-identical cells into logical operations, and naming what they do.

A contiguous block of cells sharing one R1C1 string is one logical operation regardless of
whether it spans five rows or two hundred thousand (PLAN §2.1). This module does the
grouping, splits groups that are not contiguous into separate operations, finds the cells
that break a region's uniformity, and maps each region onto the PLAN §2.7 translation
vocabulary.

Three judgements are encoded here and are worth stating plainly:

- **Small gaps do not split a region.** Two cells in the same column with the same formula
  and two blank rows between them are one operation with a hole, not two operations. That
  hole is usually the bug (see :class:`~kedge.analysis.model.Inconsistency`); splitting on
  it would hide the very thing worth reporting. The tolerance is
  :data:`DEFAULT_GAP_TOLERANCE`; a genuinely separate block further down the sheet is a
  separate operation.
- **Holes are classified, not merely counted.** A hole holding a different formula and a
  hole holding a typed-in number are different bugs and are reported differently.
- **``UNKNOWN`` is a real answer.** Pattern recognition is conservative: an unrecognised
  shape tells the planner to raise an open question, which is far better than a confident
  wrong translation (PLAN §2.7).

References:
- PLAN.md §2.1 (R1C1 compression), §2.7 (translation vocabulary), §M1 (findings).
"""

from __future__ import annotations

import itertools
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from openpyxl.formula.tokenizer import Token

from kedge.analysis.formulas import ParsedFormula, parse_formula, to_a1, tokenise
from kedge.analysis.model import ExcelPattern, Inconsistency, LogicalOperation, Orientation
from kedge.analysis.values import cell_ref

if TYPE_CHECKING:
    from kedge.analysis.values import SheetScan

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_GAP_TOLERANCE",
    "TRANSLATION_HINTS",
    "DetectedRegion",
    "classify_pattern",
    "detect_regions",
]

DEFAULT_GAP_TOLERANCE = 3
"""How many blank or overwritten rows may sit inside a region before it splits in two."""

_MAX_RANGES = 20
_MAX_INCONSISTENCIES = 50
_MAX_BLOCK_SCAN = 200_000

AGGREGATE_FUNCTIONS = frozenset(
    {
        "SUM",
        "AVERAGE",
        "AVERAGEA",
        "MIN",
        "MAX",
        "COUNT",
        "COUNTA",
        "COUNTBLANK",
        "MEDIAN",
        "MODE",
        "STDEV",
        "STDEV.S",
        "STDEV.P",
        "STDEVP",
        "VAR",
        "VARP",
        "PRODUCT",
        "LARGE",
        "SMALL",
        "PERCENTILE",
        "QUARTILE",
    }
)
TEXT_FUNCTIONS = frozenset(
    {
        "CONCATENATE",
        "CONCAT",
        "TEXTJOIN",
        "LEFT",
        "RIGHT",
        "MID",
        "TRIM",
        "UPPER",
        "LOWER",
        "PROPER",
        "TEXT",
        "SUBSTITUTE",
        "REPLACE",
        "REPT",
        "LEN",
        "FIND",
        "SEARCH",
        "VALUE",
        "CLEAN",
    }
)
DATE_FUNCTIONS = frozenset(
    {
        "DATE",
        "DATEVALUE",
        "YEAR",
        "MONTH",
        "DAY",
        "EOMONTH",
        "EDATE",
        "DATEDIF",
        "WEEKDAY",
        "WEEKNUM",
        "WORKDAY",
        "NETWORKDAYS",
        "TODAY",
        "NOW",
        "YEARFRAC",
        "HOUR",
        "MINUTE",
        "SECOND",
    }
)

TRANSLATION_HINTS: dict[ExcelPattern, str] = {
    ExcelPattern.ARITHMETIC: 'col("a") * col("b"), through kedge.xl where empty cells are possible',
    ExcelPattern.IF_SIMPLE: "pl.when(cond).then(x).otherwise(y)",
    ExcelPattern.IF_NESTED: "chained .when().then(), one per branch",
    ExcelPattern.IFERROR: (
        "pl.when(<explicit error condition>).then(fallback).otherwise(x) - "
        "state the condition, do not swallow it"
    ),
    ExcelPattern.SUMIF: '.filter(cond).select(col("x").sum()), or group_by().agg() on a key',
    ExcelPattern.SUMIFS: "group_by([...]).agg([...]) then join back",
    ExcelPattern.COUNTIFS: "group_by([...]).agg(pl.len()) then join back",
    ExcelPattern.VLOOKUP_EXACT: (
        '.join(lookup, left_on=..., right_on=..., how="left") - check key uniqueness first '
        "or rows multiply"
    ),
    ExcelPattern.VLOOKUP_APPROX: '.join_asof(lookup, on=..., strategy="backward")',
    ExcelPattern.INDEX_MATCH: '.join(lookup, left_on=..., right_on=..., how="left")',
    ExcelPattern.SUMPRODUCT: '(col("a") * col("b")).sum()',
    ExcelPattern.RUNNING_TOTAL: 'col("x").cum_sum()',
    ExcelPattern.PRIOR_ROW: 'col("x").shift(1) - a window, not a loop',
    ExcelPattern.PARAMETER_REF: "a Python constant, or a value from the parameters frame",
    ExcelPattern.AGGREGATE: '.select(col("x").sum()) at a boundary, or group_by().agg()',
    ExcelPattern.TEXT_MANIPULATION: 'col("x").str.* expressions',
    ExcelPattern.DATE_ARITHMETIC: (
        "polars temporal expressions; serial dates go through kedge.xl.serial_to_date"
    ),
    ExcelPattern.SUBTOTAL: ".filter(...) then aggregate - check which rows were actually visible",
    ExcelPattern.LITERAL: "a Python constant",
    ExcelPattern.PIVOT: ".group_by(...).agg(...) or .pivot(...)",
}

_RUNNING_TOTAL_RE = re.compile(
    r"^=SUM\(R(?P<start_row>\d+)(?P<start_col>C(?:\[-?\d+\])?|C\d+)"
    r":(?P<end_row>R|R\[-1\])(?P<end_col>C(?:\[-?\d+\])?|C\d+)\)$"
)
_PRIOR_ROW_REF_RE = re.compile(r"R\[-1\]C(?![\[\d])")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


# =============================================================================
# RESULT TYPE
# =============================================================================


@dataclass(frozen=True, slots=True)
class DetectedRegion:
    """One region: the contract-level operation plus the geometry the graph needs.

    ``coordinates`` and the bounding box never reach the serialised analysis — they exist so
    the dependency graph can test which regions a reference lands in without re-reading the
    sheet.
    """

    operation: LogicalOperation
    parsed: ParsedFormula
    coordinates: tuple[tuple[int, int], ...]
    min_row: int
    min_col: int
    max_row: int
    max_col: int

    def contains(self, row: int, col: int) -> bool:
        """Whether a cell falls inside the region's bounding box."""
        return self.min_row <= row <= self.max_row and self.min_col <= col <= self.max_col


# =============================================================================
# GROUPING
# =============================================================================


def _components(coordinates: list[tuple[int, int]], gap: int) -> list[list[tuple[int, int]]]:
    """Split cells sharing one R1C1 string into contiguity components.

    Cells are linked to their neighbours down a column and along a row when the gap between
    them is within tolerance. A rectangular fill is therefore one component, and a block
    repeated further down the sheet is another.
    """
    parent = list(range(len(coordinates)))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    by_column: dict[int, list[int]] = {}
    by_row: dict[int, list[int]] = {}
    for index, (row, col) in enumerate(coordinates):
        by_column.setdefault(col, []).append(index)
        by_row.setdefault(row, []).append(index)

    for indexes in by_column.values():
        indexes.sort(key=lambda i: coordinates[i][0])
        for first, second in itertools.pairwise(indexes):
            if coordinates[second][0] - coordinates[first][0] <= gap:
                union(first, second)
    for indexes in by_row.values():
        indexes.sort(key=lambda i: coordinates[i][1])
        for first, second in itertools.pairwise(indexes):
            if coordinates[second][1] - coordinates[first][1] <= gap:
                union(first, second)

    grouped: dict[int, list[tuple[int, int]]] = {}
    for index, coordinate in enumerate(coordinates):
        grouped.setdefault(find(index), []).append(coordinate)
    components = list(grouped.values())
    components.sort(key=lambda cells: (min(cells)[0], min(cells)[1]))
    return components


def _quote_sheet(sheet: str) -> str:
    """Quote a sheet name for a range reference if Excel would need to."""
    if re.match(r"^[A-Za-z_][A-Za-z0-9_.]*$", sheet):
        return sheet
    return "'" + sheet.replace("'", "''") + "'"


def _ranges(sheet: str, coordinates: list[tuple[int, int]]) -> list[str]:
    """Render a component's cells as a short list of sheet-qualified A1 ranges."""
    by_column: dict[int, list[int]] = {}
    for row, col in coordinates:
        by_column.setdefault(col, []).append(row)

    runs: dict[int, list[tuple[int, int]]] = {}
    for col, rows in by_column.items():
        rows.sort()
        column_runs: list[tuple[int, int]] = []
        start = previous = rows[0]
        for row in rows[1:]:
            if row == previous + 1:
                previous = row
                continue
            column_runs.append((start, previous))
            start = previous = row
        column_runs.append((start, previous))
        runs[col] = column_runs

    prefix = f"{_quote_sheet(sheet)}!"
    rendered: list[str] = []
    columns = sorted(runs)
    index = 0
    while index < len(columns):
        first = columns[index]
        last_index = index
        while (
            last_index + 1 < len(columns)
            and columns[last_index + 1] == columns[last_index] + 1
            and runs[columns[last_index + 1]] == runs[first]
        ):
            last_index += 1
        last = columns[last_index]
        for start, end in runs[first]:
            top_left = cell_ref(start, first)
            bottom_right = cell_ref(end, last)
            rendered.append(
                prefix + (top_left if top_left == bottom_right else f"{top_left}:{bottom_right}")
            )
            if len(rendered) >= _MAX_RANGES:
                return rendered
        index = last_index + 1
    return rendered


def _orientation(rows: int, cols: int, count: int) -> Orientation:
    """Describe a region's shape."""
    if count == 1:
        return "single"
    if cols == 1:
        return "column"
    if rows == 1:
        return "row"
    return "block"


def _slug(sheet: str) -> str:
    """A filesystem- and identifier-safe form of a sheet name."""
    slug = _SLUG_RE.sub("_", sheet.lower()).strip("_")
    return slug or "sheet"


# =============================================================================
# INCONSISTENCIES
# =============================================================================


def _inconsistencies(
    scan: SheetScan,
    members: set[tuple[int, int]],
    r1c1: str,
    bounds: tuple[int, int, int, int],
) -> list[Inconsistency]:
    """Find the cells inside a region's extent that break its uniformity.

    The classic bug this catches is someone typing a number over row 47 of a formula
    column. A hole holding a different formula is reported too; a hole that is simply empty
    is not, because a blank row inside a fill is usually deliberate and reporting it drowns
    out the case that matters.
    """
    min_row, min_col, max_row, max_col = bounds
    if (max_row - min_row + 1) * (max_col - min_col + 1) > _MAX_BLOCK_SCAN:
        logger.debug("region at %s is too large to scan for holes", cell_ref(min_row, min_col))
        return []

    found: list[Inconsistency] = []
    for row in range(min_row, max_row + 1):
        for col in range(min_col, max_col + 1):
            if (row, col) in members:
                continue
            other = scan.formulas.get((row, col))
            if other is not None:
                found.append(
                    Inconsistency(
                        cell=cell_ref(row, col),
                        expected_r1c1=r1c1,
                        actual_r1c1=other,
                        note="a different formula sits inside an otherwise uniform region",
                    )
                )
            elif (row, col) in scan.literals:
                found.append(
                    Inconsistency(
                        cell=cell_ref(row, col),
                        expected_r1c1=r1c1,
                        actual_r1c1=None,
                        actual_value=scan.literals[(row, col)],
                        note="a typed-in value sits where the region's formula should be",
                    )
                )
            if len(found) >= _MAX_INCONSISTENCIES:
                return found
    return found


# =============================================================================
# PATTERN RECOGNITION
# =============================================================================


def _arguments(tokens: list[Token], start: int) -> list[list[Token]]:
    """Split the arguments of the function whose ``FUNC/OPEN`` token is at *start*."""
    arguments: list[list[Token]] = [[]]
    depth = 0
    for token in tokens[start + 1 :]:
        if token.subtype == Token.OPEN:
            depth += 1
        elif token.subtype == Token.CLOSE:
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and token.type == Token.SEP and token.subtype == Token.ARG:
            arguments.append([])
            continue
        arguments[-1].append(token)
    return arguments


def _function_arguments(formula: str, name: str) -> list[list[Token]] | None:
    """Return the top-level arguments of the first call to *name*, or None if absent."""
    tokens = tokenise(formula)
    for index, token in enumerate(tokens):
        if token.type != Token.FUNC or token.subtype != Token.OPEN:
            continue
        called = token.value[:-1].upper().removeprefix("_xlfn.").removeprefix("@")
        if called == name:
            return _arguments(tokens, index)
    return None


def _is_falsey(argument: list[Token]) -> bool:
    """Whether a single-token argument is FALSE or 0."""
    meaningful = [t for t in argument if t.type != Token.WSPACE]
    if len(meaningful) != 1:
        return False
    value = meaningful[0].value.upper()
    return value in ("FALSE", "0")


def _resolve_component(component: str, current: int) -> int:
    """Resolve an R1C1 coordinate component (``C``, ``C[-2]``, ``C3``) to an index."""
    body = component[1:]
    if not body:
        return current
    if body.startswith("["):
        return current + int(body[1:-1])
    return int(body)


def _looks_like_running_total(r1c1: str, col: int) -> bool:
    """Whether an R1C1 formula is a ``SUM`` anchored above and ending at the current row.

    The two ends of the range are usually written differently — ``=SUM($B$2:B7)`` gives an
    absolute start and a relative end — so the columns are compared once resolved, not as
    text.
    """
    match = _RUNNING_TOTAL_RE.match(r1c1)
    if match is None:
        return False
    start_col = _resolve_component(match.group("start_col"), col)
    end_col = _resolve_component(match.group("end_col"), col)
    return start_col == end_col


def classify_pattern(parsed: ParsedFormula, *, col: int = 1) -> ExcelPattern:
    """Map one formula onto the PLAN §2.7 vocabulary, conservatively.

    Classification keys off the outermost function, because that is what decides the shape
    of the polars expression. Anything not recognised comes back as
    :attr:`~kedge.analysis.model.ExcelPattern.UNKNOWN`, which is a useful answer: it tells
    the planner to raise an open question rather than improvise.

    Args:
        parsed: The parsed anchor formula of a region.
        col: 1-based column of the anchor cell, needed to resolve relative column offsets
            when testing for a running total.

    Returns:
        The recognised pattern, or ``UNKNOWN``.
    """
    outer = parsed.outermost_function
    functions = set(parsed.functions)

    if outer is None:
        if not parsed.references and not parsed.names:
            return ExcelPattern.LITERAL
        if _PRIOR_ROW_REF_RE.search(parsed.r1c1):
            return ExcelPattern.PRIOR_ROW
        if parsed.references and all(
            reference.absolute_row and reference.absolute_col for reference in parsed.references
        ):
            return ExcelPattern.PARAMETER_REF
        return ExcelPattern.ARITHMETIC if parsed.references else ExcelPattern.UNKNOWN

    if outer == "IFERROR":
        return ExcelPattern.IFERROR
    if outer in ("VLOOKUP", "HLOOKUP"):
        arguments = _function_arguments(parsed.a1, outer)
        if arguments is not None and len(arguments) >= 4:
            return (
                ExcelPattern.VLOOKUP_EXACT
                if _is_falsey(arguments[3])
                else ExcelPattern.VLOOKUP_APPROX
            )
        return ExcelPattern.VLOOKUP_APPROX
    if outer == "XLOOKUP":
        return ExcelPattern.VLOOKUP_EXACT
    if outer == "INDEX" and "MATCH" in functions:
        return ExcelPattern.INDEX_MATCH
    if outer == "IF":
        return (
            ExcelPattern.IF_NESTED
            if parsed.functions.count("IF") > 1 or "IFS" in functions
            else ExcelPattern.IF_SIMPLE
        )
    if outer == "IFS":
        return ExcelPattern.IF_NESTED
    if outer in ("SUMIF", "AVERAGEIF"):
        return ExcelPattern.SUMIF
    if outer in ("SUMIFS", "AVERAGEIFS"):
        return ExcelPattern.SUMIFS
    if outer in ("COUNTIF", "COUNTIFS"):
        return ExcelPattern.COUNTIFS
    if outer == "SUMPRODUCT":
        return ExcelPattern.SUMPRODUCT
    if outer == "SUBTOTAL":
        return ExcelPattern.SUBTOTAL
    if outer == "SUM" and _looks_like_running_total(parsed.r1c1, col):
        return ExcelPattern.RUNNING_TOTAL
    if outer in AGGREGATE_FUNCTIONS:
        return ExcelPattern.AGGREGATE
    if outer in TEXT_FUNCTIONS:
        return ExcelPattern.TEXT_MANIPULATION
    if outer in DATE_FUNCTIONS:
        return ExcelPattern.DATE_ARITHMETIC
    if outer == "ROUND" and len(functions) == 1:
        return ExcelPattern.ARITHMETIC
    return ExcelPattern.UNKNOWN


def _describe(
    parsed: ParsedFormula,
    pattern: ExcelPattern,
    orientation: str,
    cell_count: int,
    sheet: str,
) -> str:
    """Write the plain-English sentence that goes in front of a human."""
    extent = {
        "column": f"filled down {cell_count} rows",
        "row": f"filled across {cell_count} columns",
        "block": f"filled over {cell_count} cells",
        "single": "a single cell",
    }[orientation]

    sheets = sorted({r.sheet for r in parsed.references if r.sheet})
    cross = f", reading from {', '.join(sheets)}" if sheets else ""
    external = " and from an external workbook" if parsed.has_external_refs else ""

    headline = {
        ExcelPattern.ARITHMETIC: "Arithmetic over neighbouring cells",
        ExcelPattern.IF_SIMPLE: "A single conditional branch",
        ExcelPattern.IF_NESTED: f"A chain of {parsed.functions.count('IF') or 2} conditional branches",
        ExcelPattern.IFERROR: "A calculation wrapped in IFERROR, so failures are replaced silently",
        ExcelPattern.SUMIF: "A conditional sum over a range",
        ExcelPattern.SUMIFS: "A conditional sum over several criteria",
        ExcelPattern.COUNTIFS: "A conditional count",
        ExcelPattern.VLOOKUP_EXACT: "An exact-match lookup into a reference table",
        ExcelPattern.VLOOKUP_APPROX: "An approximate-match lookup, which assumes the table is sorted",
        ExcelPattern.INDEX_MATCH: "An INDEX/MATCH lookup into a reference table",
        ExcelPattern.SUMPRODUCT: "A sum of pairwise products",
        ExcelPattern.RUNNING_TOTAL: "A running total accumulating down the column",
        ExcelPattern.PRIOR_ROW: "A calculation referring to the row above",
        ExcelPattern.PARAMETER_REF: "A reference to fixed parameter cells",
        ExcelPattern.AGGREGATE: f"An aggregate ({', '.join(parsed.functions[:3])}) over a range",
        ExcelPattern.TEXT_MANIPULATION: "Text manipulation",
        ExcelPattern.DATE_ARITHMETIC: "Date arithmetic",
        ExcelPattern.SUBTOTAL: "A SUBTOTAL, which respects whichever rows were filtered at the time",
        ExcelPattern.LITERAL: "A constant expression with no references",
        ExcelPattern.PIVOT: "A pivot-style aggregation",
        ExcelPattern.UNKNOWN: (
            f"An unrecognised formula shape using {', '.join(parsed.functions[:4])}"
            if parsed.functions
            else "An unrecognised formula shape"
        ),
    }[pattern]

    volatile = (
        f" Uses the volatile function(s) {', '.join(parsed.volatile_functions)}, so it "
        "recalculates on every edit."
        if parsed.volatile_functions
        else ""
    )
    return f"{headline} on {sheet}, {extent}{cross}{external}.{volatile}"


# =============================================================================
# ENTRY POINT
# =============================================================================


def detect_regions(
    scan: SheetScan,
    *,
    names: frozenset[str] = frozenset(),
    known_sheets: frozenset[str] = frozenset(),
    gap_tolerance: int = DEFAULT_GAP_TOLERANCE,
    used_ids: set[str] | None = None,
) -> list[DetectedRegion]:
    """Collapse one sheet's formula cells into logical operations.

    Args:
        scan: The sheet scan to group.
        names: Defined names in scope, upper-cased.
        known_sheets: Sheet names in the workbook, for marking unresolvable references.
        gap_tolerance: Rows or columns of gap tolerated inside one region.
        used_ids: Operation ids already handed out, so ids stay unique workbook-wide.

    Returns:
        The regions in reading order. An empty sheet yields an empty list.
    """
    used_ids = used_ids if used_ids is not None else set()
    regions: list[DetectedRegion] = []

    for r1c1, coordinates in scan.group_coordinates().items():
        for component in _components(coordinates, gap_tolerance):
            members = set(component)
            rows = {row for row, _ in component}
            cols = {col for _, col in component}
            min_row, max_row = min(rows), max(rows)
            min_col, max_col = min(cols), max(cols)
            anchor_row, anchor_col = min_row, min_col
            if (anchor_row, anchor_col) not in members:
                anchor_row, anchor_col = min(component)

            sample_a1 = to_a1(r1c1, anchor_row, anchor_col)
            parsed = parse_formula(
                sample_a1,
                anchor_row,
                anchor_col,
                sheet=scan.name,
                names=names,
                known_sheets=known_sheets,
            )
            orientation = _orientation(len(rows), len(cols), len(component))
            pattern = classify_pattern(parsed, col=anchor_col)
            anchor = cell_ref(anchor_row, anchor_col)
            operation_id = _unique_id(scan.name, anchor, max_row, max_col, len(component), used_ids)
            cached_present, cached_total = scan.group_cached.get(r1c1, [0, 0])

            operation = LogicalOperation(
                id=operation_id,
                sheet=scan.name,
                anchor=anchor,
                ranges=_ranges(scan.name, component),
                cell_count=len(component),
                orientation=orientation,
                r1c1=r1c1,
                sample_a1=sample_a1,
                functions=list(dict.fromkeys(parsed.functions)),
                references=list(parsed.references),
                excel_pattern=pattern,
                description=_describe(parsed, pattern, orientation, len(component), scan.name),
                translation_hint=TRANSLATION_HINTS.get(pattern),
                inconsistencies=_inconsistencies(
                    scan, members, r1c1, (min_row, min_col, max_row, max_col)
                ),
                is_volatile=parsed.is_volatile,
                has_external_refs=parsed.has_external_refs,
                cached_values_present=cached_present > 0 and cached_total > 0,
            )
            regions.append(
                DetectedRegion(
                    operation=operation,
                    parsed=parsed,
                    coordinates=tuple(component),
                    min_row=min_row,
                    min_col=min_col,
                    max_row=max_row,
                    max_col=max_col,
                )
            )

    regions.sort(key=lambda region: (region.min_row, region.min_col))
    logger.debug(
        "sheet %r: %d formula cells -> %d regions", scan.name, scan.formula_count, len(regions)
    )
    return regions


def _unique_id(
    sheet: str, anchor: str, max_row: int, max_col: int, count: int, used: set[str]
) -> str:
    """Mint a stable, readable, workbook-unique id for a region."""
    base = f"{_slug(sheet)}_{anchor.lower()}"
    if count > 1:
        base = f"{base}_{cell_ref(max_row, max_col).lower()}"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate
