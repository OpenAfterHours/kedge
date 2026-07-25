"""Sheet role classification, and the shape heuristics that feed it.

PLAN §2.4 gives five roles and the signals behind each. This module implements them as a
transparent additive score: every rule that fires contributes points *and* a sentence, so a
user who disagrees that a sheet is `parameters` can see exactly which signals said so. That
matters more than the score itself — an unexplained classification is one the user has to
take on trust, and this whole project is an argument against taking spreadsheets on trust.

The heuristic is deliberately the only thing here. PLAN §2.4 calls for LLM confirmation, and
that is a later milestone: it arrives as a step that takes these
:class:`Classification` records and revises ``role`` and ``role_confidence``, leaving
``signals`` as the evidence it was given. Nothing in this module reaches the network.

References:
- PLAN.md §2.4 (roles and signals), §M1.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from kedge.analysis.model import SheetRole

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from kedge.analysis.model import NamedRange
    from kedge.analysis.regions import DetectedRegion
    from kedge.analysis.values import SheetScan
    from kedge.analysis.workbook import SheetStructure

logger = logging.getLogger(__name__)

__all__ = [
    "Classification",
    "SheetSignals",
    "classify_sheet",
    "classify_sheets",
    "column_dtype",
    "detect_embedded_totals",
    "detect_header_row",
    "duplicate_headers",
    "sheet_signals",
]

_MIN_HEADER_CELLS = 2
_DATA_ROWS = 20
_SMALL_SHEET_ROWS = 60
_SMALL_SHEET_COLS = 12


# =============================================================================
# SHAPE HEURISTICS
# =============================================================================


def _is_texty(value: Any) -> bool:
    """Whether a cell value looks like a label rather than a measurement."""
    return isinstance(value, str) and bool(value.strip())


def detect_header_row(scan: SheetScan) -> tuple[int | None, int]:
    """Find the header row and how many preamble rows sit above it.

    A header is the first row that is mostly text, has at least two populated cells, and is
    followed by a row that is not mostly text. Report titles, run dates and blank spacers
    above it are preamble — a hand-in with a two-row preamble is one of the most common
    causes of a silently wrong load (PLAN §2.8).

    Args:
        scan: The sheet scan; only :attr:`~kedge.analysis.values.SheetScan.head_rows` is used.

    Returns:
        ``(header_row, preamble_rows)`` with a 1-based row number, or ``(None, 0)`` when no
        header could be identified.
    """
    rows = scan.head_rows
    for index, row in enumerate(rows):
        populated = [value for value in row if value is not None and value != ""]
        if len(populated) < _MIN_HEADER_CELLS:
            continue
        texty = sum(1 for value in populated if _is_texty(value))
        if texty / len(populated) < 0.6:
            continue
        following = rows[index + 1] if index + 1 < len(rows) else []
        following_populated = [value for value in following if value is not None and value != ""]
        if not following_populated:
            continue
        following_texty = sum(1 for value in following_populated if _is_texty(value))
        if following_texty / len(following_populated) >= 0.9 and len(following_populated) >= len(
            populated
        ):
            continue  # two header-like rows running together: keep looking
        return index + 1, index
    return None, 0


def detect_embedded_totals(scan: SheetScan) -> list[int]:
    """Rows that look like a totals row with more data underneath them.

    A totals row at the bottom is normal; one in the middle breaks every aggregation
    written against the sheet and is worth a finding.

    Args:
        scan: The sheet scan.

    Returns:
        The 1-based rows of any mid-sheet totals rows, in order.
    """
    last = scan.last_populated_row
    return [row for row in scan.totals_rows if row < last - 1]


def duplicate_headers(scan: SheetScan, header_row: int | None) -> list[str]:
    """Header labels that appear more than once on the header row."""
    if header_row is None or header_row > len(scan.head_rows):
        return []
    seen: dict[str, int] = {}
    for value in scan.head_rows[header_row - 1]:
        if not _is_texty(value):
            continue
        key = str(value).strip().casefold()
        seen[key] = seen.get(key, 0) + 1
    return sorted(label for label, count in seen.items() if count > 1)


# =============================================================================
# CROSS-SHEET SIGNALS
# =============================================================================


@dataclass(frozen=True, slots=True)
class SheetSignals:
    """What the rest of the workbook says about one sheet."""

    referenced_by: tuple[str, ...] = ()
    """Other sheets holding formulas that read from this one."""
    references: tuple[str, ...] = ()
    """Other sheets this one reads from."""
    named_ranges: int = 0
    """Defined names whose target lands on this sheet."""


def sheet_signals(
    regions: Sequence[DetectedRegion],
    sheets: Iterable[str],
    named_ranges: Sequence[NamedRange] = (),
) -> dict[str, SheetSignals]:
    """Work out which sheets read from which, and where the named ranges point.

    Args:
        regions: Every region in the workbook.
        sheets: Every sheet name, so sheets with no formulas still get an entry.
        named_ranges: The workbook's defined names.

    Returns:
        One :class:`SheetSignals` per sheet name.
    """
    referenced_by: dict[str, set[str]] = {name: set() for name in sheets}
    references: dict[str, set[str]] = {name: set() for name in referenced_by}

    for region in regions:
        home = region.operation.sheet
        for reference in region.operation.references:
            target = reference.sheet
            if target is None or target == home or reference.is_external:
                continue
            if target in referenced_by:
                referenced_by[target].add(home)
            references.setdefault(home, set()).add(target)

    counts: dict[str, int] = dict.fromkeys(referenced_by, 0)
    for named in named_ranges:
        target = named.refers_to.lstrip("=").split("!", 1)[0].strip().strip("'").replace("''", "'")
        if target in counts:
            counts[target] += 1

    return {
        name: SheetSignals(
            referenced_by=tuple(sorted(referenced_by[name])),
            references=tuple(sorted(references.get(name, ()))),
            named_ranges=counts.get(name, 0),
        )
        for name in referenced_by
    }


# =============================================================================
# CLASSIFICATION
# =============================================================================


@dataclass(frozen=True, slots=True)
class Classification:
    """One sheet's role, how sure the heuristic is, and why."""

    role: SheetRole
    confidence: float
    signals: list[str] = field(default_factory=list)


class _Scores:
    """Accumulates role scores alongside the sentence that justified each one."""

    def __init__(self) -> None:
        self.scores: dict[SheetRole, float] = dict.fromkeys(SheetRole, 0.0)
        self.reasons: list[str] = []

    def add(self, role: SheetRole, points: float, reason: str) -> None:
        """Award points to a role and record the reason once."""
        self.scores[role] += points
        if reason not in self.reasons:
            self.reasons.append(reason)


def classify_sheet(
    scan: SheetScan,
    structure: SheetStructure | None,
    regions: Sequence[DetectedRegion],
    signals: SheetSignals,
) -> Classification:
    """Classify one sheet against the PLAN §2.4 signal table.

    Args:
        scan: The sheet scan.
        structure: Merged ranges and visibility, when available.
        regions: The regions detected on this sheet.
        signals: What the rest of the workbook says about this sheet.

    Returns:
        A :class:`Classification`. An empty sheet is ``UNKNOWN`` with zero confidence.
    """
    rows = max(scan.last_populated_row, scan.max_row)
    populated = scan.value_cell_count + scan.formula_count
    if populated == 0:
        return Classification(SheetRole.UNKNOWN, 0.0, ["the sheet is empty"])

    formula_share = scan.formula_count / populated
    text_share = scan.text_cell_count / populated
    header_row, preamble = detect_header_row(scan)
    scores = _Scores()

    # ── data ─────────────────────────────────────────────────────────────
    if rows >= _DATA_ROWS:
        scores.add(SheetRole.DATA, 2.0, f"{rows:,} rows")
    if scan.formula_count == 0:
        scores.add(SheetRole.DATA, 3.0, "no formulas at all")
    elif formula_share < 0.02:
        scores.add(SheetRole.DATA, 2.0, f"formulas in only {formula_share:.1%} of populated cells")
    if header_row is not None:
        scores.add(SheetRole.DATA, 1.0, f"header row at row {header_row}")
    if preamble:
        scores.add(SheetRole.DATA, 0.5, f"{preamble} preamble row(s) above the header")
    if scan.numeric_cell_count > scan.text_cell_count and rows >= _DATA_ROWS:
        scores.add(SheetRole.DATA, 1.0, "mostly numeric values")

    # ── calculation ──────────────────────────────────────────────────────
    if scan.formula_count >= 20 or formula_share > 0.25:
        scores.add(
            SheetRole.CALCULATION,
            3.0,
            f"{scan.formula_count:,} formula cells ({formula_share:.0%} of populated cells)",
        )
    if signals.references:
        scores.add(SheetRole.CALCULATION, 2.0, f"reads from {', '.join(signals.references)}")
    if len(regions) >= 3:
        scores.add(SheetRole.CALCULATION, 1.0, f"{len(regions)} distinct formula regions")

    # ── parameters ───────────────────────────────────────────────────────
    small = rows <= _SMALL_SHEET_ROWS and scan.max_col <= _SMALL_SHEET_COLS
    if small and scan.formula_count <= 5:
        scores.add(SheetRole.PARAMETERS, 2.0, f"small sheet ({rows} rows) of mostly literals")
    if signals.named_ranges:
        scores.add(SheetRole.PARAMETERS, 2.0, f"{signals.named_ranges} named range(s) point here")
    if len(signals.referenced_by) >= 2:
        scores.add(
            SheetRole.PARAMETERS,
            1.5,
            f"referenced by {len(signals.referenced_by)} other sheets",
        )

    # ── documentation ────────────────────────────────────────────────────
    if text_share >= 0.7 and scan.formula_count == 0:
        scores.add(SheetRole.DOCUMENTATION, 2.0, f"{text_share:.0%} of cells are text")
    if scan.long_text_cell_count >= 3:
        scores.add(
            SheetRole.DOCUMENTATION,
            2.5,
            f"{scan.long_text_cell_count} cells hold prose-length text",
        )
    if scan.text_cell_count and scan.text_length_total / max(scan.text_cell_count, 1) > 40:
        scores.add(SheetRole.DOCUMENTATION, 1.0, "long average text length")

    # ── output ───────────────────────────────────────────────────────────
    if scan.formula_count and not signals.referenced_by and signals.references:
        scores.add(
            SheetRole.OUTPUT,
            2.0,
            "formulas read other sheets but nothing reads this one",
        )
    if structure is not None and len(structure.merged_ranges) >= 3:
        scores.add(SheetRole.OUTPUT, 1.0, f"{len(structure.merged_ranges)} merged ranges")
    if scan.formula_count and rows < _DATA_ROWS and signals.references:
        scores.add(SheetRole.OUTPUT, 1.0, "small, formula-driven and terminal")

    ranked = sorted(scores.scores.items(), key=lambda item: -item[1])
    best, best_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    if best_score <= 0:
        return Classification(SheetRole.UNKNOWN, 0.0, scores.reasons)

    confidence = min(1.0, 0.45 + 0.55 * (best_score - runner_up) / best_score)
    logger.debug("sheet %r classified %s (%.2f)", scan.name, best.value, confidence)
    return Classification(best, round(confidence, 3), scores.reasons)


def classify_sheets(
    scans: Mapping[str, SheetScan],
    structures: Mapping[str, SheetStructure],
    regions: Sequence[DetectedRegion],
    named_ranges: Sequence[NamedRange] = (),
) -> dict[str, Classification]:
    """Classify every sheet in the workbook.

    Args:
        scans: Sheet name to its scan.
        structures: Sheet name to its structure.
        regions: Every region in the workbook.
        named_ranges: The workbook's defined names.

    Returns:
        One :class:`Classification` per sheet.
    """
    by_sheet: dict[str, list[DetectedRegion]] = {name: [] for name in scans}
    for region in regions:
        by_sheet.setdefault(region.operation.sheet, []).append(region)

    signals = sheet_signals(regions, scans.keys(), named_ranges)
    return {
        name: classify_sheet(
            scan, structures.get(name), by_sheet.get(name, []), signals.get(name, SheetSignals())
        )
        for name, scan in scans.items()
    }


def column_dtype(scan: SheetScan, col: int) -> str:
    """Name the dominant type in a column, for the mixed-type findings.

    Args:
        scan: The sheet scan.
        col: 1-based column index.

    Returns:
        The most common kind seen in that column, or ``"empty"``.
    """
    kinds = scan.column_kinds.get(col)
    if not kinds:
        return "empty"
    return max(kinds.items(), key=lambda item: item[1])[0]
