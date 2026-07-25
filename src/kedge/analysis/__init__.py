"""Offline workbook analysis — pure, deterministic, no LLM and no network.

The public surface is :func:`analyse` plus the contract types in
:mod:`kedge.analysis.model`. Everything else in this package is an extractor and is an
implementation detail: extractors are invoked by :func:`analyse` and report their outcome
through an :class:`~kedge.analysis.model.ExtractionStatus` rather than by raising.

References:
- PLAN.md §M1 and §1.5.
"""

from __future__ import annotations

from kedge.analysis.model import (
    SCHEMA_VERSION,
    CachedValueCoverage,
    ColumnProfile,
    Connection,
    DependencyGraph,
    ExcelPattern,
    ExtractionStatus,
    Finding,
    FindingKind,
    LogicalOperation,
    NamedRange,
    PowerQuery,
    ProcessNote,
    Severity,
    SheetInfo,
    SheetRole,
    WorkbookAnalysis,
    WorkbookIdentity,
)

__all__ = [
    "SCHEMA_VERSION",
    "CachedValueCoverage",
    "ColumnProfile",
    "Connection",
    "DependencyGraph",
    "ExcelPattern",
    "ExtractionStatus",
    "Finding",
    "FindingKind",
    "LogicalOperation",
    "NamedRange",
    "PowerQuery",
    "ProcessNote",
    "Severity",
    "SheetInfo",
    "SheetRole",
    "WorkbookAnalysis",
    "WorkbookIdentity",
]
