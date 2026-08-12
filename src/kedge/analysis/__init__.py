"""Offline workbook analysis — pure, deterministic, no LLM and no network.

The public surface is :func:`analyse` plus the contract types in
:mod:`kedge.analysis.model`. Everything else in this package is an extractor and is an
implementation detail: extractors are invoked by :func:`analyse` and report their outcome
through an :class:`~kedge.analysis.model.ExtractionStatus` rather than by raising.

:func:`analyse` is re-exported here deliberately, and not only for convenience: the submodule
that defines it is also called ``analyse``, so without the re-export ``from kedge.analysis
import analyse`` silently binds the *module* and the first call fails with "module is not
callable" somewhere else entirely. It caught two callers before the re-export existed.

The price is worth knowing: the re-export shadows the submodule on the package, so
``kedge.analysis.analyse`` reached by attribute is the **function**, and the module has to be
reached as ``sys.modules["kedge.analysis.analyse"]`` or imported by name. Anything patching
``"kedge.analysis.analyse.analyse"`` as a dotted string will not resolve. Import the function
from either place -- they are the same object -- and reach for the module by name if you need
the module.

References:
- PLAN.md §M1 and §1.5.
"""

from __future__ import annotations

from kedge.analysis.analyse import AnalysisOptions, analyse
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
    "AnalysisOptions",
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
    "analyse",
]
