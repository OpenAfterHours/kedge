"""The orchestrator: one workbook in, one complete :class:`WorkbookAnalysis` out.

The contract this module keeps is absolute: **it always returns a complete analysis.** Every
extractor runs behind a guard, and a failure becomes an
:class:`~kedge.analysis.model.ExtractionStatus` plus a
:class:`~kedge.analysis.model.Finding` rather than an exception. A workbook with no
connections part is normal; a workbook whose DataMashup will not decode is a finding; an
``.xlsb`` is a refusal carrying ``UNSUPPORTED_FORMAT`` at ERROR severity — not a traceback,
and never a half-analysis presented as whole (PLAN §M1, §6.2).

The one exception is a file that does not exist, which is a caller error rather than a
workbook defect and raises :class:`~kedge.errors.AnalysisError`.

Order of work, because it matters: scan each sheet in both views, collapse formulas into
regions, classify the sheets (classification needs the cross-sheet reference counts the
regions produced), build the dependency graph, then run the detectors, which need all of
the above. The three extractors owned elsewhere — connections, Power Query, process notes —
and the column profiler are imported defensively so that a module which is not present yet
degrades to ``SKIPPED`` instead of breaking the analysis.

References:
- PLAN.md §M1 (deliverables and graceful degradation), §1.5, §2.1, §2.4.
"""

from __future__ import annotations

import importlib
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kedge.analysis.classify import classify_sheets, detect_embedded_totals, detect_header_row
from kedge.analysis.findings import collect_findings
from kedge.analysis.graph import build_graph
from kedge.analysis.model import (
    CachedValueCoverage,
    ColumnProfile,
    Connection,
    DependencyGraph,
    ExtractionStatus,
    Finding,
    FindingKind,
    LogicalOperation,
    NamedRange,
    PowerQueryExtraction,
    ProcessNote,
    Severity,
    SheetInfo,
    WorkbookAnalysis,
    WorkbookIdentity,
)
from kedge.analysis.regions import DEFAULT_GAP_TOLERANCE, DetectedRegion, detect_regions
from kedge.analysis.values import SheetScan, cached_value_coverage, scan_sheet
from kedge.analysis.workbook import (
    SUPPORTED_FORMATS,
    SheetStructure,
    WorkbookHandle,
    WorkbookOpenError,
    detect_format,
    open_workbook,
    read_identity,
)
from kedge.errors import AnalysisError

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

__all__ = ["AnalysisOptions", "analyse"]


def _kedge_version() -> str:
    """The installed kedge version, or a placeholder when running from a source tree."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("kedge")
    except (ImportError, PackageNotFoundError):  # pragma: no cover - packaging accident
        return "0.0.0+unknown"


@dataclass(frozen=True, slots=True)
class AnalysisOptions:
    """Knobs for one analysis run. The defaults are what ``kedge inspect`` uses."""

    gap_tolerance: int = DEFAULT_GAP_TOLERANCE
    """Rows of gap tolerated inside one region before it splits in two."""
    max_scan_rows: int | None = None
    """Stop scanning each sheet after this row. None reads the sheet."""
    literal_cap: int = 250_000
    """Literal cells retained per sheet for inconsistency detection."""
    profile_columns: bool = True
    """Whether to run the column profiler (PLAN §2.3)."""
    redact_patterns: list[str] = field(default_factory=list)
    """Column-name patterns whose values the profiler must hash (PLAN §2.3)."""
    structure_budget: int = 64 * 1024 * 1024
    """Decompressed bytes of a sheet part to stream when hunting for merged ranges."""


# =============================================================================
# GUARDED EXTERNAL EXTRACTORS
# =============================================================================


def _optional(module: str, attribute: str) -> Callable[..., Any] | None:
    """Import one of the concurrently-developed extractors, or None if it is not there yet."""
    try:
        loaded = importlib.import_module(f"kedge.analysis.{module}")
    except ImportError:
        logger.debug("extractor %s is not available; skipping", module)
        return None
    function = getattr(loaded, attribute, None)
    if function is None or not callable(function):
        logger.warning("kedge.analysis.%s has no callable %s; skipping", module, attribute)
        return None
    return function


def _unparseable(what: str, exc: Exception, location: str | None = None) -> Finding:
    """Turn an extractor's exception into a finding, per CONVENTIONS non-negotiable 4."""
    logger.warning("%s extraction failed: %s: %s", what, type(exc).__name__, exc)
    return Finding(
        kind=FindingKind.UNPARSEABLE_PART,
        severity=Severity.WARNING,
        message=f"The {what} extractor failed on this workbook: {exc}",
        location=location,
        detail=f"{type(exc).__name__}: {exc}",
        remediation=(
            "The rest of the analysis is unaffected. Report this workbook if the part matters."
        ),
    )


def _extract_connections(
    handle: WorkbookHandle, findings: list[Finding]
) -> tuple[list[Connection], ExtractionStatus]:
    """Legacy external data connections, via the connections extractor."""
    extract = _optional("connections", "extract_connections")
    if extract is None:
        return [], ExtractionStatus.SKIPPED
    try:
        connections, status, extra = extract(handle.zf)
    except Exception as exc:
        findings.append(_unparseable("connections", exc, "xl/connections.xml"))
        return [], ExtractionStatus.UNPARSEABLE
    findings.extend(extra or [])
    return list(connections or []), status


def _extract_power_query(handle: WorkbookHandle, findings: list[Finding]) -> PowerQueryExtraction:
    """Power Query M source, via the DataMashup extractor."""
    extract = _optional("powerquery", "extract_power_query")
    if extract is None:
        return PowerQueryExtraction(status=ExtractionStatus.SKIPPED)
    try:
        extraction, extra = extract(handle.zf)
    except Exception as exc:
        findings.append(_unparseable("Power Query", exc, "customXml/"))
        return PowerQueryExtraction(status=ExtractionStatus.UNPARSEABLE, detail=str(exc))
    findings.extend(extra or [])
    return extraction


def _extract_notes(handle: WorkbookHandle, findings: list[Finding]) -> list[ProcessNote]:
    """In-sheet and Word process notes, via the docs extractor."""
    extract = _optional("docs", "extract_notes")
    if extract is None:
        return []
    try:
        notes, extra = extract(handle)
    except Exception as exc:
        findings.append(_unparseable("process notes", exc))
        return []
    findings.extend(extra or [])
    return list(notes or [])


def _profile(
    handle: WorkbookHandle,
    sheets: list[SheetInfo],
    options: AnalysisOptions,
    findings: list[Finding],
) -> list[ColumnProfile]:
    """Column profiles for every populated sheet, via the profiler."""
    if not options.profile_columns:
        return []
    profile_sheet = _optional("profile", "profile_sheet")
    if profile_sheet is None:
        return []

    profiles: list[ColumnProfile] = []
    patterns = options.redact_patterns or None
    for sheet in sheets:
        if sheet.max_row == 0 or sheet.max_column == 0:
            continue
        try:
            profiles.extend(profile_sheet(handle, sheet, redact_patterns=patterns) or [])
        except Exception as exc:
            findings.append(_unparseable(f"profile of sheet {sheet.name!r}", exc, sheet.name))
    return profiles


# =============================================================================
# ASSEMBLY
# =============================================================================


def _sheet_info(
    name: str,
    index: int,
    scan: SheetScan,
    structure: SheetStructure | None,
    regions: list[DetectedRegion],
    classification: Any,
    table_ranges: tuple[str, ...],
) -> SheetInfo:
    """Build one :class:`SheetInfo` from everything gathered about a sheet."""
    header_row, preamble = detect_header_row(scan)
    return SheetInfo(
        name=name,
        index=index,
        role=classification.role,
        role_confidence=classification.confidence,
        role_signals=list(classification.signals),
        max_row=scan.max_row,
        max_column=scan.max_col,
        used_range=scan.used_range,
        formula_cell_count=scan.formula_count,
        value_cell_count=scan.value_cell_count,
        blank_cell_count=scan.blank_cell_count,
        text_cell_count=scan.text_cell_count,
        is_hidden=structure.is_hidden if structure else False,
        is_very_hidden=structure.is_very_hidden if structure else False,
        hidden_columns=list(structure.hidden_columns) if structure else [],
        merged_ranges=list(structure.merged_ranges) if structure else [],
        header_row=header_row,
        preamble_rows=preamble,
        has_embedded_totals=bool(detect_embedded_totals(scan)),
        table_ranges=list(table_ranges),
        operation_ids=[region.operation.id for region in regions],
    )


def _with_fan_out(regions: list[DetectedRegion], graph: DependencyGraph) -> list[LogicalOperation]:
    """Stamp each operation with its downstream reference count."""
    return [
        region.operation.model_copy(
            update={"downstream_ref_count": graph.fan_out.get(region.operation.id, 0)}
        )
        for region in regions
    ]


def _with_reference_counts(
    named_ranges: list[NamedRange], regions: list[DetectedRegion]
) -> list[NamedRange]:
    """Count how many formula cells use each defined name."""
    counts: dict[str, int] = {}
    for region in regions:
        for name in region.parsed.names:
            key = name.upper()
            counts[key] = counts.get(key, 0) + region.operation.cell_count
    return [
        named.model_copy(update={"reference_count": counts.get(named.name.upper(), 0)})
        for named in named_ranges
    ]


def _refusal(
    identity: WorkbookIdentity, findings: list[Finding], started: float
) -> WorkbookAnalysis:
    """A complete analysis for a workbook that could not be read at all."""
    return WorkbookAnalysis(
        kedge_version=_kedge_version(),
        generated_at=datetime.now(UTC),
        workbook=identity,
        cached_values=CachedValueCoverage(status="not_applicable"),
        findings=findings,
        analysis_duration_seconds=round(time.perf_counter() - started, 3),
    )


# =============================================================================
# ENTRY POINT
# =============================================================================


def analyse(path: Path | str, *, options: AnalysisOptions | None = None) -> WorkbookAnalysis:
    """Analyse one workbook, offline and deterministically.

    Args:
        path: Path to an ``.xlsx`` or ``.xlsm`` workbook.
        options: Run options; the defaults are what ``kedge inspect`` uses.

    Returns:
        A complete :class:`~kedge.analysis.model.WorkbookAnalysis`, always. A workbook that
        cannot be read comes back carrying an ERROR finding that says why, with the sheets,
        operations and graph left empty.

    Raises:
        AnalysisError: The path does not point at a file. Every other failure mode is a
            finding.
    """
    started = time.perf_counter()
    options = options or AnalysisOptions()
    resolved = Path(path)
    if not resolved.is_file():
        raise AnalysisError(
            f"cannot analyse {resolved}: the file does not exist. Check the path and try again."
        )

    file_format = detect_format(resolved)
    identity = read_identity(resolved)
    findings: list[Finding] = []

    if file_format not in SUPPORTED_FORMATS:
        logger.warning("refusing %s: unsupported format %s", resolved.name, file_format)
        from kedge.analysis.findings import _format_findings

        return _refusal(identity, _format_findings(identity, None), started)

    try:
        handle = open_workbook(resolved, structure_budget=options.structure_budget)
    except WorkbookOpenError as exc:
        logger.warning("could not open %s: %s", resolved.name, exc)
        findings.append(
            Finding(
                kind=FindingKind.UNPARSEABLE_PART,
                severity=Severity.ERROR,
                message=str(exc),
                location=resolved.name,
                remediation="Open the workbook in Excel and re-save it, then analyse the copy.",
            )
        )
        return _refusal(identity, findings, started)

    try:
        return _analyse_open(handle, identity, options, findings, started)
    finally:
        handle.close()


def _analyse_open(
    handle: WorkbookHandle,
    identity: WorkbookIdentity,
    options: AnalysisOptions,
    findings: list[Finding],
    started: float,
) -> WorkbookAnalysis:
    """Analyse a workbook that opened cleanly."""
    sheet_names = handle.sheet_names
    known_sheets = frozenset(sheet_names)
    names = handle.defined_name_keys
    logger.info("analysing %s: %d sheets", handle.path.name, len(sheet_names))

    scans: dict[str, SheetScan] = {}
    structures: dict[str, SheetStructure] = {}
    regions_by_sheet: dict[str, list[DetectedRegion]] = {}
    all_regions: list[DetectedRegion] = []
    used_ids: set[str] = set()

    for name in sheet_names:
        try:
            scan = scan_sheet(
                handle, name, max_rows=options.max_scan_rows, literal_cap=options.literal_cap
            )
        except Exception as exc:
            findings.append(_unparseable(f"scan of sheet {name!r}", exc, name))
            scan = SheetScan(name=name)
        scans[name] = scan

        try:
            structures[name] = handle.structure(name)
        except Exception as exc:
            findings.append(_unparseable(f"structure of sheet {name!r}", exc, name))

        try:
            regions = detect_regions(
                scan,
                names=names,
                known_sheets=known_sheets,
                gap_tolerance=options.gap_tolerance,
                used_ids=used_ids,
            )
        except Exception as exc:
            findings.append(_unparseable(f"region detection on sheet {name!r}", exc, name))
            regions = []
        regions_by_sheet[name] = regions
        all_regions.extend(regions)

    named_ranges = _with_reference_counts(handle.defined_names(), all_regions)
    classifications = classify_sheets(scans, structures, all_regions, named_ranges)
    graph = build_graph(all_regions, scans, names=names, known_sheets=known_sheets)
    coverage = cached_value_coverage(scans.values())

    sheets = [
        _sheet_info(
            name,
            index,
            scans[name],
            structures.get(name),
            regions_by_sheet.get(name, []),
            classifications[name],
            handle.table_ranges(name),
        )
        for index, name in enumerate(sheet_names)
    ]

    findings.extend(
        collect_findings(
            identity=identity,
            handle=handle,
            scans=scans,
            structures=structures,
            regions=all_regions,
            graph=graph,
            classifications=classifications,
            named_ranges=named_ranges,
            coverage=coverage,
        )
    )

    connections, connections_status = _extract_connections(handle, findings)
    power_query = _extract_power_query(handle, findings)
    notes = _extract_notes(handle, findings)
    profiles = _profile(handle, sheets, options, findings)

    analysis = WorkbookAnalysis(
        kedge_version=_kedge_version(),
        generated_at=datetime.now(UTC),
        workbook=identity,
        sheets=sheets,
        operations=_with_fan_out(all_regions, graph),
        graph=graph,
        connections=connections,
        connections_status=connections_status,
        power_query=power_query,
        named_ranges=named_ranges,
        notes=notes,
        profiles=profiles,
        cached_values=coverage,
        findings=findings,
        analysis_duration_seconds=round(time.perf_counter() - started, 3),
    )
    logger.info(
        "analysed %s in %.2fs: %d operations, %d findings",
        handle.path.name,
        analysis.analysis_duration_seconds or 0.0,
        len(analysis.operations),
        len(analysis.findings),
    )
    return analysis
