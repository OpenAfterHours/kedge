"""The findings detectors: everything the analyser thinks a reviewer should know.

The findings list is the "extra control" pitch made concrete (PLAN §M1), and the single
most valuable entry in it is :attr:`~kedge.analysis.model.FindingKind.INCONSISTENT_FORMULA`
— the cell where somebody typed over row 47 of a formula column. Everything here is
mechanical: each detector takes structures the rest of the analyser has already built and
turns them into :class:`~kedge.analysis.model.Finding` records with an exact location and
something the reader can act on.

Severity is used as a claim about consequence, not about tidiness:

- ``ERROR`` — the conversion cannot be trusted until a human looks: an unsupported format,
  a genuine circular reference, an absent value cache (reconciliation is impossible), a
  typed-over cell inside a formula region, an unresolvable external link.
- ``WARNING`` — translatable, but a specific decision is needed.
- ``INFO`` — worth stating in the report; no decision required.

References:
- PLAN.md §M1 (the findings list), §1.5 and §4.5 (cached values), §6.2 (refusal cases).
"""

from __future__ import annotations

import logging
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any
from xml.etree import ElementTree

from kedge.analysis.classify import (
    detect_embedded_totals,
    detect_header_row,
    duplicate_headers,
)
from kedge.analysis.model import (
    ExcelPattern,
    Finding,
    FindingKind,
    Severity,
    SheetRole,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from kedge.analysis.classify import Classification
    from kedge.analysis.model import (
        CachedValueCoverage,
        DependencyGraph,
        NamedRange,
        WorkbookIdentity,
    )
    from kedge.analysis.regions import DetectedRegion
    from kedge.analysis.values import SheetScan
    from kedge.analysis.workbook import SheetStructure, WorkbookHandle

logger = logging.getLogger(__name__)

__all__ = [
    "HEAVY_VBA_BYTES",
    "collect_findings",
    "external_link_targets",
]

HEAVY_VBA_BYTES = 100_000
"""A VBA project this large is a macro-driven application, not a spreadsheet with a helper
macro attached, and PLAN §6.2 says to refuse it rather than half-solve it."""

_PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_MAX_CELLS_LISTED = 5
_MAX_PER_REGION = 25
_INTERESTING_CONSTANT = 1000
_OVERRIDE_BLOCK = 3


def _sample(items: Sequence[Any], limit: int = _MAX_CELLS_LISTED) -> str:
    """Render a bounded, honest sample of a list for a message."""
    shown = ", ".join(str(item) for item in items[:limit])
    return shown if len(items) <= limit else f"{shown} and {len(items) - limit} more"


# =============================================================================
# FORMAT, MACROS, CALCULATION SETTINGS
# =============================================================================


def _format_findings(identity: WorkbookIdentity, handle: WorkbookHandle | None) -> list[Finding]:
    """Unsupported formats, macro presence, and iterative calculation."""
    findings: list[Finding] = []

    if identity.file_format in ("xlsb", "xls", "unknown"):
        findings.append(
            Finding(
                kind=FindingKind.UNSUPPORTED_FORMAT,
                severity=Severity.ERROR,
                message=(
                    f"{identity.filename} is a {identity.file_format} workbook, which kedge "
                    "cannot read."
                ),
                detail=(
                    "Only the Office Open XML formats (.xlsx, .xlsm) can be analysed: the binary "
                    "formats store formulas in a proprietary encoding that openpyxl does not read."
                ),
                remediation="Open the workbook in Excel and re-save it as .xlsx, then analyse that.",
            )
        )
        return findings

    vba_size = 0
    if handle is not None and handle.has_part("xl/vbaProject.bin"):
        try:
            vba_size = handle.zf.getinfo("xl/vbaProject.bin").file_size
        except KeyError:
            vba_size = 0

    if identity.has_vba or vba_size:
        findings.append(
            Finding(
                kind=FindingKind.MACRO_PRESENT,
                severity=Severity.WARNING,
                message=f"The workbook contains a VBA project ({vba_size:,} bytes).",
                location="xl/vbaProject.bin",
                detail=(
                    "Macro code is not analysed. Any logic it performs is invisible to the "
                    "analyser and will be missing from the converted process."
                ),
                remediation="Confirm what the macros do before relying on the conversion.",
            )
        )
    if vba_size >= HEAVY_VBA_BYTES:
        findings.append(
            Finding(
                kind=FindingKind.UNSUPPORTED_FORMAT,
                severity=Severity.ERROR,
                message=(
                    f"{identity.filename} is macro-driven: its VBA project is {vba_size:,} bytes."
                ),
                location="xl/vbaProject.bin",
                detail=(
                    "A workbook this dependent on VBA is an application, and converting the "
                    "worksheet layer alone would produce a process that silently omits most of "
                    "what it does."
                ),
                remediation=(
                    "Treat the macros as a separate problem: document what they do before "
                    "converting anything."
                ),
            )
        )

    if identity.iterative_calculation:
        findings.append(
            Finding(
                kind=FindingKind.ITERATIVE_CALCULATION,
                severity=Severity.WARNING,
                message="Iterative calculation is enabled for this workbook.",
                detail=(
                    "Excel only offers this setting to resolve deliberate circularity, so some "
                    "part of the workbook converges by repeated recalculation. polars has no "
                    "equivalent."
                ),
                remediation=(
                    "Find the circular block and decide with the process owner how it should be "
                    "expressed before translating it."
                ),
            )
        )
    return findings


# =============================================================================
# CACHED VALUES
# =============================================================================


def _cached_value_findings(coverage: CachedValueCoverage) -> list[Finding]:
    """The value cache is the reconciliation baseline; its absence is never silent."""
    if coverage.status == "absent" and coverage.formula_cell_count:
        return [
            Finding(
                kind=FindingKind.MISSING_CACHED_VALUES,
                severity=Severity.ERROR,
                message=(
                    f"None of the {coverage.formula_cell_count:,} formula cells carry a cached "
                    "value, so the workbook cannot be reconciled against."
                ),
                detail=(
                    "openpyxl never calculates anything: it reports the values Excel last saved. "
                    "An empty cache means this file was written by a tool rather than saved by "
                    "Excel."
                ),
                remediation=(
                    "Open the workbook in Excel, allow it to calculate, and save it. Until then "
                    "any reconciliation must report 'not reconciled', never 'passed'."
                ),
                affected_cells=coverage.formula_cell_count,
            )
        ]
    if coverage.status == "partial":
        missing = coverage.formula_cell_count - coverage.cached_present_count
        return [
            Finding(
                kind=FindingKind.PARTIAL_CACHED_VALUES,
                severity=Severity.WARNING,
                message=(
                    f"{missing:,} of {coverage.formula_cell_count:,} formula cells have no cached "
                    f"value ({coverage.coverage:.1%} coverage)."
                ),
                detail="Regions without cached values cannot serve as a reconciliation baseline.",
                remediation="Recalculate and save the workbook in Excel to restore full coverage.",
                affected_cells=missing,
            )
        ]
    return []


# =============================================================================
# REGIONS
# =============================================================================


def _inconsistency_findings(region: DetectedRegion) -> list[Finding]:
    """The typed-over cell, and the block of typed-over cells."""
    operation = region.operation
    literal_cells = [item for item in operation.inconsistencies if item.actual_r1c1 is None]
    formula_cells = [item for item in operation.inconsistencies if item.actual_r1c1 is not None]
    findings: list[Finding] = []

    if len(literal_cells) >= _OVERRIDE_BLOCK:
        findings.append(
            Finding(
                kind=FindingKind.MANUAL_OVERRIDE_BLOCK,
                severity=Severity.ERROR,
                message=(
                    f"{len(literal_cells)} cells inside the formula region at "
                    f"{operation.sheet}!{operation.anchor} hold typed-in values instead of the "
                    "region's formula."
                ),
                sheet=operation.sheet,
                location=_sample([item.cell for item in literal_cells]),
                detail=(
                    f"Expected {operation.r1c1} in every cell. Overridden values: "
                    f"{_sample([item.actual_value for item in literal_cells])}."
                ),
                remediation=(
                    "These are manual overrides. They are business logic that exists nowhere "
                    "else, so plan them as an explicit checkpoint stage rather than translating "
                    "them away."
                ),
                affected_cells=len(literal_cells),
            )
        )
    else:
        for item in literal_cells[:_MAX_PER_REGION]:
            findings.append(
                Finding(
                    kind=FindingKind.INCONSISTENT_FORMULA,
                    severity=Severity.ERROR,
                    message=(
                        f"{operation.sheet}!{item.cell} holds the typed-in value "
                        f"{item.actual_value!r} where the surrounding region's formula was "
                        "expected."
                    ),
                    sheet=operation.sheet,
                    location=item.cell,
                    detail=f"Expected {item.expected_r1c1} (as filled from {operation.anchor}).",
                    remediation=(
                        "Check with the process owner whether this override is deliberate before "
                        "translating the region."
                    ),
                    affected_cells=1,
                )
            )

    for item in formula_cells[:_MAX_PER_REGION]:
        findings.append(
            Finding(
                kind=FindingKind.INCONSISTENT_FORMULA,
                severity=Severity.WARNING,
                message=(
                    f"{operation.sheet}!{item.cell} holds a different formula from the region "
                    f"filled from {operation.anchor}."
                ),
                sheet=operation.sheet,
                location=item.cell,
                detail=f"Expected {item.expected_r1c1}, found {item.actual_r1c1}.",
                remediation=(
                    "Decide which formula is correct: one of the two is almost certainly a "
                    "mistake, and translating the region will silently pick one."
                ),
                affected_cells=1,
            )
        )
    return findings


def _region_findings(regions: Sequence[DetectedRegion]) -> list[Finding]:
    """Volatility, hardcoded constants, swallowed errors, inconsistencies, external refs."""
    findings: list[Finding] = []
    for region in regions:
        operation = region.operation
        where = f"{operation.sheet}!{operation.anchor}"

        if operation.is_volatile:
            volatile = ", ".join(region.parsed.volatile_functions)
            findings.append(
                Finding(
                    kind=FindingKind.VOLATILE_FUNCTION,
                    severity=Severity.WARNING,
                    message=f"The region at {where} calls the volatile function(s) {volatile}.",
                    sheet=operation.sheet,
                    location=operation.anchor,
                    detail=(
                        f"{operation.sample_a1} recalculates on every edit, so its cached values "
                        "may not match what the formula would produce now."
                    ),
                    remediation=(
                        "TODAY/NOW/RAND make a run non-reproducible: pass the run date in as a "
                        "parameter. OFFSET/INDIRECT hide the real dependency from the analyser, "
                        "so check what they resolve to."
                    ),
                    affected_cells=operation.cell_count,
                )
            )

        interesting = [
            value
            for value in region.parsed.numeric_literals
            if value != int(value) or abs(value) >= _INTERESTING_CONSTANT
        ]
        if interesting:
            findings.append(
                Finding(
                    kind=FindingKind.HARDCODED_CONSTANT,
                    severity=Severity.INFO,
                    message=(
                        f"The region at {where} embeds the constant(s) {_sample(interesting)} in "
                        "its formula."
                    ),
                    sheet=operation.sheet,
                    location=operation.anchor,
                    detail=operation.sample_a1,
                    remediation=(
                        "A rate or threshold written into a formula is invisible to review. Lift "
                        "it into a named parameter when translating."
                    ),
                    affected_cells=operation.cell_count,
                )
            )

        if operation.excel_pattern is ExcelPattern.IFERROR or "IFERROR" in operation.functions:
            findings.append(
                Finding(
                    kind=FindingKind.IFERROR_SWALLOW,
                    severity=Severity.WARNING,
                    message=f"The region at {where} wraps its calculation in IFERROR.",
                    sheet=operation.sheet,
                    location=operation.anchor,
                    detail=(
                        f"{operation.sample_a1} replaces every failure — a missing lookup key, a "
                        "division by zero, a text value where a number was expected — with the "
                        "same fallback, and the cached values give no clue which rows failed."
                    ),
                    remediation=(
                        "Translate the error condition explicitly (PLAN §2.7) so the converted "
                        "process reports what it could not calculate."
                    ),
                    affected_cells=operation.cell_count,
                )
            )

        if operation.has_external_refs:
            workbooks = sorted(
                {r.external_workbook or "?" for r in operation.references if r.is_external}
            )
            findings.append(
                Finding(
                    kind=FindingKind.EXTERNAL_LINK,
                    severity=Severity.WARNING,
                    message=(
                        f"The region at {where} reads from another workbook ({_sample(workbooks)})."
                    ),
                    sheet=operation.sheet,
                    location=operation.anchor,
                    detail=operation.sample_a1,
                    remediation=(
                        "The linked workbook is an input to this process and must be brought in "
                        "as a hand-in, not left as a live link."
                    ),
                    affected_cells=operation.cell_count,
                )
            )

        findings.extend(_inconsistency_findings(region))
    return findings


# =============================================================================
# SHEETS
# =============================================================================


def _sheet_findings(
    scans: Mapping[str, SheetScan],
    structures: Mapping[str, SheetStructure],
    classifications: Mapping[str, Classification],
) -> list[Finding]:
    """Hidden things, merged cells, header and totals-row hazards, and dirty types."""
    findings: list[Finding] = []
    for name, scan in scans.items():
        structure = structures.get(name)
        role = classifications[name].role if name in classifications else SheetRole.UNKNOWN

        if structure is not None and structure.is_hidden:
            findings.append(
                Finding(
                    kind=FindingKind.HIDDEN_SHEET,
                    severity=Severity.WARNING if structure.is_very_hidden else Severity.INFO,
                    message=(
                        f"Sheet {name!r} is {'very ' if structure.is_very_hidden else ''}hidden "
                        f"and holds {scan.formula_count:,} formula cells."
                    ),
                    sheet=name,
                    detail=(
                        "A very hidden sheet cannot be revealed from the Excel UI, only from VBA."
                        if structure.is_very_hidden
                        else "Hidden sheets are still part of the calculation."
                    ),
                    remediation="Confirm whether this sheet is live before dropping or keeping it.",
                )
            )

        if structure is not None and structure.hidden_columns:
            findings.append(
                Finding(
                    kind=FindingKind.HIDDEN_COLUMN,
                    severity=Severity.INFO,
                    message=(
                        f"Sheet {name!r} has {len(structure.hidden_columns)} hidden column(s): "
                        f"{_sample(structure.hidden_columns)}."
                    ),
                    sheet=name,
                    location=_sample(structure.hidden_columns),
                    remediation="Hidden columns often hold working calculations; check before dropping.",
                    affected_cells=len(structure.hidden_columns),
                )
            )

        if structure is not None and structure.merged_ranges:
            findings.append(
                Finding(
                    kind=FindingKind.MERGED_CELLS,
                    severity=Severity.WARNING,
                    message=(
                        f"Sheet {name!r} has {len(structure.merged_ranges)} merged range(s): "
                        f"{_sample(structure.merged_ranges)}."
                    ),
                    sheet=name,
                    location=_sample(structure.merged_ranges),
                    detail=(
                        "Only the top-left cell of a merged range holds a value; the rest read as "
                        "empty, which is why merged headers load as unnamed columns."
                    ),
                    remediation="Unmerge before loading, or read the header row explicitly.",
                    affected_cells=len(structure.merged_ranges),
                )
            )

        header_row, preamble = detect_header_row(scan)
        if preamble and role in (SheetRole.DATA, SheetRole.OUTPUT, SheetRole.UNKNOWN):
            findings.append(
                Finding(
                    kind=FindingKind.PREAMBLE_ROWS,
                    severity=Severity.WARNING,
                    message=(
                        f"Sheet {name!r} has {preamble} row(s) above its header at row {header_row}."
                    ),
                    sheet=name,
                    location=f"A1:A{preamble}",
                    detail="A default read that assumes a header on row 1 will mis-name every column.",
                    remediation=f"Skip {preamble} row(s) when loading this sheet.",
                    affected_cells=preamble,
                )
            )

        duplicates = duplicate_headers(scan, header_row)
        if duplicates:
            findings.append(
                Finding(
                    kind=FindingKind.DUPLICATE_HEADER,
                    severity=Severity.WARNING,
                    message=f"Sheet {name!r} repeats the header label(s) {_sample(duplicates)}.",
                    sheet=name,
                    location=f"row {header_row}",
                    detail="Duplicate column names are ambiguous and polars will rename them on load.",
                    remediation="Rename the duplicates so each column can be referred to unambiguously.",
                    affected_cells=len(duplicates),
                )
            )

        totals = detect_embedded_totals(scan)
        if totals:
            findings.append(
                Finding(
                    kind=FindingKind.EMBEDDED_TOTALS_ROW,
                    severity=Severity.WARNING,
                    message=f"Sheet {name!r} has totals row(s) mid-sheet at row(s) {_sample(totals)}.",
                    sheet=name,
                    location=_sample([f"row {row}" for row in totals]),
                    detail=(
                        "A totals row inside the data double-counts every aggregate computed over "
                        "the sheet."
                    ),
                    remediation="Filter the totals rows out on load, and recompute the totals.",
                    affected_cells=len(totals),
                )
            )

        if scan.text_number_columns:
            columns = sorted(scan.text_number_columns)
            total = sum(scan.text_number_columns.values())
            findings.append(
                Finding(
                    kind=FindingKind.TEXT_FORMATTED_NUMBER,
                    severity=Severity.WARNING,
                    message=(
                        f"Sheet {name!r} holds {total:,} number(s) stored as text in "
                        f"{len(columns)} column(s)."
                    ),
                    sheet=name,
                    location=_sample([_letters(index) for index in columns]),
                    detail=(
                        "Excel coerces these on the fly; polars leaves them as String, which "
                        "silently breaks joins and sums (PLAN §2.6)."
                    ),
                    remediation="Cast these columns explicitly on load and check the null count after.",
                    affected_cells=total,
                )
            )

        mixed = _mixed_type_columns(scan)
        if mixed:
            findings.append(
                Finding(
                    kind=FindingKind.MIXED_DATE_FORMAT,
                    severity=Severity.WARNING,
                    message=(
                        f"Sheet {name!r} has column(s) mixing dates with other types: "
                        f"{_sample(mixed)}."
                    ),
                    sheet=name,
                    location=_sample(mixed),
                    detail="Some rows are real dates and others are text or serial numbers.",
                    remediation="Normalise the column on load; watch the 1900 serial system (PLAN §2.6).",
                    affected_cells=len(mixed),
                )
            )

        if scan.error_cells:
            worst = (
                Severity.ERROR
                if any(str(value) == "#REF!" for _, value in scan.error_cells)
                else Severity.WARNING
            )
            findings.append(
                Finding(
                    kind=FindingKind.ERROR_VALUE,
                    severity=worst,
                    message=(
                        f"Sheet {name!r} holds {scan.error_cell_count:,} cell(s) evaluating to an "
                        "Excel error."
                    ),
                    sheet=name,
                    location=_sample([ref for ref, _ in scan.error_cells]),
                    detail=_sample([f"{ref}={value}" for ref, value in scan.error_cells]),
                    remediation=(
                        "A #REF! means a reference was deleted and the formula can no longer be "
                        "read. Fix the workbook before converting it."
                    ),
                    affected_cells=scan.error_cell_count,
                )
            )
    return findings


def _letters(index: int) -> str:
    """1-based column index to letters."""
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _mixed_type_columns(scan: SheetScan) -> list[str]:
    """Columns holding real dates alongside text or numbers."""
    mixed: list[str] = []
    for index, kinds in sorted(scan.column_kinds.items()):
        dates = kinds.get("date", 0)
        others = kinds.get("text", 0) + kinds.get("number", 0)
        if dates >= 2 and others >= 2:
            mixed.append(_letters(index))
    return mixed


# =============================================================================
# GRAPH
# =============================================================================


def _graph_findings(
    regions: Sequence[DetectedRegion],
    graph: DependencyGraph,
    classifications: Mapping[str, Classification],
) -> list[Finding]:
    """Circular references, and regions nothing downstream reads."""
    findings: list[Finding] = []
    for cycle in graph.cycles:
        findings.append(
            Finding(
                kind=FindingKind.CIRCULAR_REFERENCE,
                severity=Severity.ERROR,
                message=f"Circular reference across {len(cycle)} cell(s): {' -> '.join(cycle)}.",
                sheet=cycle[0].split("!", 1)[0] if "!" in cycle[0] else None,
                location=cycle[0],
                detail=(
                    "Excel resolves this by iterating to convergence. There is no polars "
                    "equivalent, so the translation has to state the intent explicitly."
                ),
                remediation=(
                    "Stop and ask the process owner what this loop is meant to compute (PLAN §2.7)."
                ),
                affected_cells=len(cycle),
            )
        )

    for region in regions:
        operation = region.operation
        role = (
            classifications[operation.sheet].role
            if operation.sheet in classifications
            else SheetRole.UNKNOWN
        )
        if graph.fan_out.get(operation.id, 0) or role in (
            SheetRole.OUTPUT,
            SheetRole.DOCUMENTATION,
        ):
            continue
        findings.append(
            Finding(
                kind=FindingKind.DEAD_REGION,
                severity=Severity.INFO,
                message=(
                    f"The region at {operation.sheet}!{operation.anchor} "
                    f"({operation.cell_count:,} cells) is not read by anything else in the "
                    "workbook."
                ),
                sheet=operation.sheet,
                location=operation.anchor,
                detail=f"{operation.sample_a1} — no other formula references this range.",
                remediation=(
                    "A candidate for the plan's `dropped` list. Confirm it is not read manually "
                    "or by something outside the workbook before removing it."
                ),
                affected_cells=operation.cell_count,
            )
        )
    return findings


# =============================================================================
# NAMED RANGES AND EXTERNAL LINKS
# =============================================================================


def _named_range_findings(named_ranges: Sequence[NamedRange]) -> list[Finding]:
    """Defined names pointing at nothing."""
    broken = [named for named in named_ranges if named.is_broken]
    return [
        Finding(
            kind=FindingKind.BROKEN_NAMED_RANGE,
            severity=Severity.WARNING,
            message=f"The defined name {named.name!r} does not resolve: {named.refers_to!r}.",
            sheet=named.scope,
            location=named.refers_to,
            detail=(
                "The sheet or range it pointed at has been deleted, so every formula using this "
                "name evaluates to an error."
            ),
            remediation="Repoint or delete the name before converting.",
        )
        for named in broken
    ]


def external_link_targets(handle: WorkbookHandle) -> list[tuple[str, str, bool]]:
    """Every external workbook this file links to, and whether it can be found on disk.

    Args:
        handle: The open workbook handle.

    Returns:
        ``(part, target, resolves)`` triples, one per external link part.
    """
    targets: list[tuple[str, str, bool]] = []
    for part in sorted(handle.part_names):
        if not part.startswith("xl/externalLinks/_rels/"):
            continue
        data = handle.read_part(part)
        if not data:
            continue
        try:
            root = ElementTree.fromstring(data)
        except ElementTree.ParseError:
            logger.warning("could not parse %s", part)
            targets.append((part, "<unparseable>", False))
            continue
        for relationship in root.iter(f"{_PKG_REL_NS}Relationship"):
            target = relationship.get("Target")
            if not target:
                continue
            targets.append((part, target, _resolves(handle.path, target)))
    return targets


def _resolves(workbook: Path, target: str) -> bool:
    """Whether an external link target can be found on this machine."""
    text = urllib.parse.unquote(target)
    if text.lower().startswith("file:"):
        parsed = urllib.parse.urlparse(text)
        candidate = Path(urllib.parse.unquote(parsed.path).lstrip("/"))
    else:
        candidate = Path(text)
    try:
        if candidate.is_absolute():
            return candidate.exists()
        return (workbook.parent / candidate).exists()
    except OSError:
        return False


def _external_link_findings(handle: WorkbookHandle | None) -> list[Finding]:
    """External workbook links that cannot be resolved from here."""
    if handle is None:
        return []
    findings: list[Finding] = []
    for part, target, resolves in external_link_targets(handle):
        if resolves:
            continue
        findings.append(
            Finding(
                kind=FindingKind.UNRESOLVABLE_EXTERNAL_LINK,
                severity=Severity.ERROR,
                message=f"The external workbook link {target!r} cannot be found from here.",
                location=part,
                detail=(
                    "Formulas reading from it fall back on values cached the last time the link "
                    "resolved, which may be arbitrarily stale."
                ),
                remediation=(
                    "Obtain the linked workbook and treat it as a hand-in, or replace the link "
                    "with the values it supplies."
                ),
            )
        )
    return findings


# =============================================================================
# ENTRY POINT
# =============================================================================


def collect_findings(
    *,
    identity: WorkbookIdentity,
    handle: WorkbookHandle | None,
    scans: Mapping[str, SheetScan],
    structures: Mapping[str, SheetStructure],
    regions: Sequence[DetectedRegion],
    graph: DependencyGraph,
    classifications: Mapping[str, Classification],
    named_ranges: Sequence[NamedRange],
    coverage: CachedValueCoverage,
) -> list[Finding]:
    """Run every detector that belongs to the core analyser.

    Detectors owned by the other extractors (connections, Power Query, process notes) return
    their own findings and are merged by :func:`~kedge.analysis.analyse.analyse`.

    Args:
        identity: The workbook identity.
        handle: The open handle, or None when the workbook could not be opened.
        scans: Sheet name to its scan.
        structures: Sheet name to its structure.
        regions: Every region in the workbook.
        graph: The dependency graph.
        classifications: Sheet name to its classification.
        named_ranges: The workbook's defined names.
        coverage: Cached value coverage.

    Returns:
        Findings ordered most severe first, and within a severity in detection order.
    """
    findings: list[Finding] = []
    findings.extend(_format_findings(identity, handle))
    findings.extend(_cached_value_findings(coverage))
    findings.extend(_region_findings(regions))
    findings.extend(_sheet_findings(scans, structures, classifications))
    findings.extend(_graph_findings(regions, graph, classifications))
    findings.extend(_named_range_findings(named_ranges))
    findings.extend(_external_link_findings(handle))

    order = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}
    findings.sort(key=lambda finding: order[finding.severity])
    logger.info("collected %d findings", len(findings))
    return findings
