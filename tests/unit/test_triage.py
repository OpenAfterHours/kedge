"""Triage: a deterministic verdict, and a refusal that says why."""

from __future__ import annotations

import pytest

from conftest import make_analysis, make_finding, make_operation
from kedge.analysis.model import (
    CachedValueCoverage,
    DependencyGraph,
    ExcelPattern,
    FindingKind,
    Severity,
    SheetInfo,
    SheetRole,
)
from kedge.plan.triage import (
    CARE_THRESHOLD,
    BlockerCode,
    TriageVerdict,
    complexity,
    triage,
)

# ── the happy path ──────────────────────────────────────────────────────────


def test_a_clean_workbook_proceeds(analysis) -> None:
    result = triage(analysis)
    assert result.verdict is TriageVerdict.PROCEED
    assert result.convertible == 1.0
    assert not result.blockers
    assert not result.should_stop


def test_triage_is_deterministic(analysis) -> None:
    """The same analysis always gives the same answer, so a change means a change."""
    assert triage(analysis) == triage(analysis)


# ── refusals ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("file_format", ["xlsb", "xls"])
def test_an_xlsb_or_xls_workbook_is_refused_outright(file_format: str) -> None:
    result = triage(make_analysis(workbook_fields={"file_format": file_format}))
    assert result.verdict is TriageVerdict.STOP
    assert result.should_stop
    assert any(b.code is BlockerCode.UNSUPPORTED_FORMAT for b in result.blockers)
    assert "re-save as .xlsx" in result.explain()


def test_vba_is_a_serious_blocker_and_says_why() -> None:
    result = triage(make_analysis(workbook_fields={"has_vba": True}))
    assert any(b.code is BlockerCode.VBA_PRESENT for b in result.blockers)
    assert result.convertible < CARE_THRESHOLD
    assert "Macro logic is invisible" in result.explain()


def test_circular_references_plus_iterative_calculation_stop_the_pipeline() -> None:
    """Genuine circularity is a legitimate "don't": there is no polars equivalent."""
    result = triage(
        make_analysis(
            workbook_fields={"iterative_calculation": True},
            graph=DependencyGraph(cycles=[["Calc!B2", "Calc!C2", "Calc!B2"]]),
        )
    )
    assert result.verdict is TriageVerdict.STOP
    assert {b.code for b in result.blockers} >= {
        BlockerCode.CIRCULAR_REFERENCE,
        BlockerCode.ITERATIVE_CALCULATION,
    }


def test_circular_references_alone_are_a_blocker_but_not_automatically_fatal() -> None:
    result = triage(make_analysis(graph=DependencyGraph(cycles=[["Calc!B2", "Calc!B2"]])))
    assert any(b.code is BlockerCode.CIRCULAR_REFERENCE for b in result.blockers)
    assert result.verdict is TriageVerdict.PROCEED_WITH_CARE


def test_a_workbook_with_no_formulas_at_all_is_refused() -> None:
    """Mostly human judgement with a spreadsheet attached is a legitimate "don't"."""
    result = triage(
        make_analysis(
            operations=[],
            sheets=[
                SheetInfo(
                    name="Sheet1",
                    index=0,
                    role=SheetRole.UNKNOWN,
                    max_row=900,
                    value_cell_count=9_000,
                )
            ],
        )
    )
    assert result.verdict is TriageVerdict.STOP
    assert any(b.code is BlockerCode.NO_LOGIC for b in result.blockers)


def test_a_mostly_typed_workbook_is_flagged_as_mostly_manual() -> None:
    result = triage(
        make_analysis(
            sheets=[
                SheetInfo(
                    name="Calc",
                    index=0,
                    role=SheetRole.CALCULATION,
                    max_row=2_000,
                    formula_cell_count=200,
                    value_cell_count=9_800,
                )
            ]
        )
    )
    blocker = next(b for b in result.blockers if b.code is BlockerCode.MOSTLY_MANUAL)
    assert "typed values rather than formulas" in blocker.message
    assert "checkpoints" in (blocker.remediation or "")


def test_unresolvable_external_links_are_a_conversion_blocker() -> None:
    result = triage(
        make_analysis(
            findings=[
                make_finding(FindingKind.UNRESOLVABLE_EXTERNAL_LINK, message="[budget.xlsx]Q3!B2")
            ]
        )
    )
    assert any(b.code is BlockerCode.UNRESOLVABLE_EXTERNAL_LINK for b in result.blockers)


def test_unparseable_power_query_is_a_blocker() -> None:
    from kedge.analysis.model import ExtractionStatus, PowerQueryExtraction

    result = triage(
        make_analysis(
            power_query=PowerQueryExtraction(
                status=ExtractionStatus.UNPARSEABLE, part_path="customXml/item3.xml"
            )
        )
    )
    assert any(b.code is BlockerCode.UNPARSEABLE_POWER_QUERY for b in result.blockers)


def test_mostly_unclassified_formulas_lower_the_score() -> None:
    unknown = [
        make_operation(f"op_{index}", excel_pattern=ExcelPattern.UNKNOWN) for index in range(8)
    ]
    result = triage(make_analysis(operations=[*unknown, make_operation()]))
    assert any(b.code is BlockerCode.UNCLASSIFIED_FORMULAS for b in result.blockers)
    assert result.convertible < 1.0


# ── conversion versus verification ──────────────────────────────────────────


def test_absent_cached_values_block_verification_not_conversion() -> None:
    """The code can still be written; it simply cannot be proved right (PLAN 1.5, 4.5)."""
    result = triage(
        make_analysis(
            cached_values=CachedValueCoverage(
                formula_cell_count=400_000, cached_present_count=0, coverage=0.0, status="absent"
            )
        )
    )
    assert result.convertible == 1.0
    assert not result.conversion_blockers
    assert [b.code for b in result.verification_blockers] == [BlockerCode.NO_CACHED_VALUES]
    assert not result.reconcilable
    assert "not reconciled" in result.explain()


def test_partial_cached_values_are_a_verification_blocker_too() -> None:
    result = triage(
        make_analysis(
            cached_values=CachedValueCoverage(
                formula_cell_count=1_000,
                cached_present_count=400,
                coverage=0.4,
                status="partial",
            )
        )
    )
    assert [b.code for b in result.verification_blockers] == [BlockerCode.PARTIAL_CACHED_VALUES]
    assert result.convertible == 1.0


def test_verification_blockers_never_move_the_convertible_score() -> None:
    for blocker in triage(
        make_analysis(cached_values=CachedValueCoverage(status="absent"))
    ).verification_blockers:
        assert blocker.penalty == 0.0


# ── the refusal is first class ──────────────────────────────────────────────


def test_a_stop_explains_itself_and_offers_the_override() -> None:
    result = triage(make_analysis(workbook_fields={"file_format": "xlsb"}))
    explanation = result.explain()
    assert "kedge recommends not converting this workbook" in explanation
    assert "--force" in explanation
    assert result.fatal_blockers


def test_blocker_lines_are_ready_for_the_plans_assessment() -> None:
    result = triage(make_analysis(workbook_fields={"has_vba": True}))
    assert result.blocker_lines()
    assert all(isinstance(line, str) and line for line in result.blocker_lines())


def test_error_findings_the_other_checks_missed_are_still_counted() -> None:
    result = triage(
        make_analysis(
            findings=[
                make_finding(FindingKind.INCONSISTENT_FORMULA, Severity.ERROR),
                make_finding(FindingKind.BROKEN_NAMED_RANGE, Severity.ERROR),
            ]
        )
    )
    assert any(b.code is BlockerCode.ANALYSIS_ERRORS for b in result.blockers)


# ── complexity ──────────────────────────────────────────────────────────────


def test_complexity_rises_with_the_size_of_the_process(analysis) -> None:
    small = complexity(analysis)
    big = complexity(
        make_analysis(operations=[make_operation(f"op_{index}") for index in range(200)])
    )
    assert 0.0 <= small < big <= 1.0


def test_an_empty_workbook_has_no_complexity() -> None:
    assert complexity(make_analysis(operations=[], sheets=[], profiles=[])) == 0.0
