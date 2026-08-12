"""Factories for the planning-loop unit tests.

Building a `WorkbookAnalysis` by hand is verbose enough that duplicating it across five test
modules would bury the behaviour being asserted. These fixtures return builders rather than
fixed objects so each test can vary only the field it cares about.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from kedge.analysis.model import (
    CachedValueCoverage,
    ColumnProfile,
    DependencyGraph,
    ExcelPattern,
    Finding,
    FindingKind,
    LogicalOperation,
    NumericStats,
    Severity,
    SheetInfo,
    SheetRole,
    WorkbookAnalysis,
    WorkbookIdentity,
)
from kedge.plan.model import (
    Assessment,
    Checkpoint,
    Confidence,
    DroppedRange,
    PlanDraft,
    ProcessPlan,
    Stage,
    StageKind,
)
from kedge.plan.review import acknowledge_all_drops, approve
from kedge.plan.store import PlanStore


def make_operation(op_id: str = "calc_h2_h500", **overrides: Any) -> LogicalOperation:
    """One R1C1-compressed formula region."""
    fields: dict[str, Any] = {
        "id": op_id,
        "sheet": "Calc",
        "anchor": "H2",
        "ranges": ["Calc!H2:H500"],
        "cell_count": 499,
        "r1c1": "=VLOOKUP(RC[-3],Ref!R1C1:R50C4,4,FALSE)",
        "sample_a1": "=VLOOKUP(E2,Ref!$A$1:$D$50,4,FALSE)",
        "functions": ["VLOOKUP"],
        "excel_pattern": ExcelPattern.VLOOKUP_EXACT,
        "description": "Exact lookup of a haircut by asset class",
        "cached_values_present": True,
        "downstream_ref_count": 3,
    }
    fields.update(overrides)
    return LogicalOperation(**fields)


def make_profile(column: str = "H", **overrides: Any) -> ColumnProfile:
    """One column profile, of the shape the model reasons over."""
    fields: dict[str, Any] = {
        "sheet": "Calc",
        "column": column,
        "index": 8,
        "header": "haircut",
        "dtype": "float",
        "row_count": 499,
        "null_count": 0,
        "distinct_count": 12,
        "numeric": NumericStats(min=0.0, max=0.5, mean=0.12, sum=59.8),
        "top_k": [(0.1, 200), (0.15, 120)],
        "head": [0.1, 0.15, 0.1],
    }
    fields.update(overrides)
    return ColumnProfile(**fields)


def make_analysis(**overrides: Any) -> WorkbookAnalysis:
    """A plausible mid-sized workbook analysis: two sheets, one lookup, cached values present."""
    identity_overrides = overrides.pop("workbook_fields", {})
    identity: dict[str, Any] = {
        "path": "C:/processes/rwa_monthly_v14.xlsx",
        "filename": "rwa_monthly_v14.xlsx",
        "sha256": "a" * 64,
        "size_bytes": 4_194_304,
        "file_format": "xlsx",
    }
    identity.update(identity_overrides)

    fields: dict[str, Any] = {
        "kedge_version": "0.1.0",
        "generated_at": datetime(2026, 7, 24, 9, 30, tzinfo=UTC),
        "workbook": WorkbookIdentity(**identity),
        "sheets": [
            SheetInfo(
                name="Data",
                index=0,
                role=SheetRole.DATA,
                max_row=50_000,
                max_column=12,
                value_cell_count=600_000,
                header_row=1,
            ),
            SheetInfo(
                name="Calc",
                index=1,
                role=SheetRole.CALCULATION,
                max_row=50_000,
                max_column=20,
                formula_cell_count=400_000,
                value_cell_count=10_000,
                operation_ids=["calc_h2_h500"],
            ),
        ],
        "operations": [make_operation()],
        "graph": DependencyGraph(nodes=["calc_h2_h500"], roots=["calc_h2_h500"]),
        "profiles": [make_profile()],
        "cached_values": CachedValueCoverage(
            formula_cell_count=400_000,
            cached_present_count=400_000,
            coverage=1.0,
            status="present",
        ),
        "findings": [],
    }
    fields.update(overrides)
    return WorkbookAnalysis(**fields)


def make_draft(**overrides: Any) -> PlanDraft:
    """A small, valid plan draft with one checkpoint and one drop."""
    fields: dict[str, Any] = {
        "assessment": Assessment(
            convertible=0.8,
            blockers=["Sheet 'Adjustments' has 14 manually-typed overrides with no formula"],
        ),
        "stages": [
            Stage(
                id="load_handin",
                intent="Read counterparty exposures from the hand-in",
                kind=StageKind.LOAD,
                sources=["handin"],
                confidence=Confidence.HIGH,
                assumptions=["header on row 1", "one row per counterparty"],
            ),
            Stage(
                id="apply_haircuts",
                intent="Collateral haircut lookup by asset class",
                sources=["Calc!H2:H500", "Ref!A1:D50"],
                depends_on=["load_handin"],
                confidence=Confidence.HIGH,
                excel_pattern=ExcelPattern.VLOOKUP_EXACT,
                operations=["calc_h2_h500"],
            ),
            Stage(
                id="manual_overrides",
                intent="Overrides typed by the analyst each month",
                kind=StageKind.CHECKPOINT,
                depends_on=["apply_haircuts"],
                checkpoint=Checkpoint(
                    question="Have this month's overrides been agreed with Risk?"
                ),
            ),
            Stage(
                id="write_output",
                intent="Produce the monthly RWA summary",
                kind=StageKind.OUTPUT,
                depends_on=["apply_haircuts", "manual_overrides"],
                confidence=Confidence.LOW,
            ),
        ],
        "open_questions": ["Column AF is computed but never referenced. Dead, or read manually?"],
        "dropped": [
            DroppedRange(range="Calc!AK:AP", reason="no downstream refs, all zero since 2023")
        ],
        "summary": "A monthly regulatory calculation with one lookup and a manual override step.",
    }
    fields.update(overrides)
    return PlanDraft(**fields)


def make_plan(**overrides: Any) -> ProcessPlan:
    """A full plan, unapproved, at version 1."""
    draft = overrides.pop("draft", None) or make_draft()
    return ProcessPlan.from_draft(
        draft,
        workbook=overrides.pop("workbook", "rwa_monthly_v14.xlsx"),
        workbook_sha256=overrides.pop("workbook_sha256", "a" * 64),
        created_at=overrides.pop("created_at", datetime(2026, 7, 24, 10, 0, tzinfo=UTC)),
        **overrides,
    )


def make_approved_plan(**overrides: Any) -> ProcessPlan:
    """The same plan, reviewed: every drop acknowledged and the approval recorded."""
    return approve(acknowledge_all_drops(make_plan(**overrides), note="reviewed"), by="tests")


def approved_plan_store(directory: Path, **overrides: Any) -> PlanStore:
    """A real store on disk holding one approved plan.

    The notebook-writing tools refuse until ``latest_approved()`` answers, so a test whose subject
    is something else -- the caps, the validation gate, the staleness guard -- still needs a plan
    in force to reach the behaviour it is about. It goes through :meth:`PlanStore.save` and
    :func:`kedge.plan.review.approve` rather than a stub, so what the tools read is a plan that
    was actually written and actually approved.
    """
    store = PlanStore(directory)
    store.save(make_approved_plan(**overrides))
    return store


def make_finding(kind: FindingKind, severity: Severity = Severity.ERROR, **kw: Any) -> Finding:
    """One analyser finding."""
    return Finding(kind=kind, severity=severity, message=kw.pop("message", kind.value), **kw)


@pytest.fixture
def analysis_factory() -> Callable[..., WorkbookAnalysis]:
    """Build a `WorkbookAnalysis`, overriding any field by keyword."""
    return make_analysis


@pytest.fixture
def analysis() -> WorkbookAnalysis:
    """A plausible, cleanly convertible workbook analysis."""
    return make_analysis()


@pytest.fixture
def draft() -> PlanDraft:
    """A valid plan draft, as a model would return it."""
    return make_draft()


@pytest.fixture
def plan() -> ProcessPlan:
    """An unapproved plan at version 1, with one unacknowledged drop."""
    return make_plan()
