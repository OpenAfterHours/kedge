"""The report schema, and the invariant the whole milestone rests on.

PLAN 6.2 names "reconciliation silently vacuous" as a risk and CLAUDE.md calls a false pass
"the most dangerous failure mode in the project". These tests are the mechanical half of the
defence: a status that cannot be truthy unless it is PASSED, a region that refuses to be
constructed as a pass without evidence, and a fold that ranks "not reconciled" above "passed".
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from pydantic import ValidationError

from kedge.config import ReconciliationConfig
from kedge.reconcile.model import (
    Diagnosis,
    DiagnosticCause,
    Mismatch,
    MismatchKind,
    NotReconciledReason,
    ReconciliationReport,
    ReconciliationStatus,
    RegionResult,
    RegionSpec,
    Tolerance,
    signed_delta,
    worst_status,
)

# ── the falsy-unless-passed rule ────────────────────────────────────────────


def test_only_passed_is_truthy() -> None:
    """`if result.status:` must not be satisfiable by an absent cache.

    Every `StrEnum` member is a non-empty string and therefore truthy by default, which is
    exactly the accident that would turn "we checked nothing" into "it passed".
    """
    assert bool(ReconciliationStatus.PASSED) is True
    assert bool(ReconciliationStatus.FAILED) is False
    assert bool(ReconciliationStatus.NOT_RECONCILED) is False


def test_not_reconciled_outranks_passed_when_folding_regions() -> None:
    assert (
        worst_status([ReconciliationStatus.PASSED] * 99 + [ReconciliationStatus.NOT_RECONCILED])
        is ReconciliationStatus.NOT_RECONCILED
    )
    assert (
        worst_status([ReconciliationStatus.NOT_RECONCILED, ReconciliationStatus.FAILED])
        is ReconciliationStatus.FAILED
    )


def test_folding_no_regions_at_all_is_not_a_pass() -> None:
    """A run that compared nothing has proved nothing."""
    assert worst_status([]) is ReconciliationStatus.NOT_RECONCILED


def test_a_region_cannot_be_constructed_as_passed_without_compared_rows() -> None:
    with pytest.raises(ValidationError, match="no rows were compared"):
        RegionResult(
            spec_id="rwa",
            status=ReconciliationStatus.PASSED,
            tolerance=Tolerance(),
            rows_compared=0,
        )


def test_a_region_cannot_be_constructed_as_passed_with_rows_differing() -> None:
    with pytest.raises(ValidationError, match="rows differ"):
        RegionResult(
            spec_id="rwa",
            status=ReconciliationStatus.PASSED,
            tolerance=Tolerance(),
            rows_compared=10,
            rows_differing=1,
        )


def test_a_region_that_is_not_reconciled_must_say_why() -> None:
    with pytest.raises(ValidationError, match="does not say why"):
        RegionResult(
            spec_id="rwa", status=ReconciliationStatus.NOT_RECONCILED, tolerance=Tolerance()
        )


@pytest.mark.parametrize("reason", list(NotReconciledReason))
def test_every_not_reconciled_reason_denies_a_pass_in_words(reason: NotReconciledReason) -> None:
    """The user reads the sentence, not the enum member."""
    explanation = reason.explanation.lower()
    assert "not" in explanation
    assert len(explanation) > 40


# ── tolerance ───────────────────────────────────────────────────────────────


def test_a_row_matches_within_either_tolerance() -> None:
    absolute_only = Tolerance(absolute=0.005, relative=0.0)
    assert absolute_only.matches(1.0, 1.004)
    assert not absolute_only.matches(1.0, 1.006)

    relative_only = Tolerance(absolute=0.0, relative=1e-9)
    assert relative_only.matches(1e9, 1e9 + 0.5)
    assert not relative_only.matches(1.0, 1.1)


def test_a_non_finite_value_never_matches() -> None:
    assert not Tolerance().matches(1.0, float("inf"))
    assert not Tolerance().matches(1.0, float("nan"))


def test_the_tolerance_comes_from_config_and_is_reportable() -> None:
    tolerance = Tolerance.from_config(ReconciliationConfig())

    assert tolerance.absolute == 1e-6
    assert tolerance.relative == 0.0
    assert tolerance.describe() == "absolute 1e-06"


def test_a_coarse_tolerance_is_a_different_claim_and_says_so() -> None:
    """PLAN 4.5: a reconciliation that passed at 1e-2 is not one that passed at 1e-9."""
    coarse = Tolerance(absolute=1e-2, relative=0.0)
    report = ReconciliationReport(workbook="p.xlsx", tolerance=coarse)

    assert "1e-06" not in report.to_dict()["tolerance"]["absolute"].__str__()
    assert report.to_dict()["tolerance"] == {"absolute": 0.01, "relative": 0.0}


# ── deltas ──────────────────────────────────────────────────────────────────


def test_the_signed_delta_separates_a_systematic_offset_from_noise() -> None:
    assert signed_delta(10.0, 9.0) == pytest.approx(-1.0)
    assert signed_delta(date(2026, 1, 1), date(2026, 1, 3)) == pytest.approx(2.0)
    assert signed_delta(True, False) is None
    assert signed_delta("a", "b") is None
    assert signed_delta(1.0, float("inf")) is None


def test_a_mismatch_exposes_its_own_signed_delta() -> None:
    mismatch = Mismatch(row=3, expected=2.68, actual=2.67, absolute_delta=0.01)

    assert mismatch.signed_delta == pytest.approx(-0.01)


# ── the report ──────────────────────────────────────────────────────────────


def _region(status: ReconciliationStatus, **kwargs) -> RegionResult:
    defaults = {"spec_id": "r", "tolerance": Tolerance(), "status": status}
    if status is ReconciliationStatus.PASSED:
        defaults.update(rows_compared=10, rows_matched=10)
    if status is ReconciliationStatus.NOT_RECONCILED:
        defaults.update(reason=NotReconciledReason.NO_CACHED_VALUES)
    if status is ReconciliationStatus.FAILED:
        defaults.update(rows_compared=10, rows_matched=9, rows_differing=1)
    defaults.update(kwargs)
    return RegionResult(**defaults)


def test_an_empty_report_is_not_reconciled_and_never_passed() -> None:
    report = ReconciliationReport(workbook="p.xlsx", tolerance=Tolerance())

    assert report.status is ReconciliationStatus.NOT_RECONCILED
    assert not report.status
    assert report.colour == "amber"
    assert "This is not a pass." in report.headline()


def test_one_unreconciled_region_among_passing_ones_is_not_a_passing_report() -> None:
    report = ReconciliationReport(
        workbook="p.xlsx",
        tolerance=Tolerance(),
        regions=[
            _region(ReconciliationStatus.PASSED, spec_id="a"),
            _region(ReconciliationStatus.PASSED, spec_id="b"),
            _region(ReconciliationStatus.NOT_RECONCILED, spec_id="c"),
        ],
    )

    assert report.status is ReconciliationStatus.NOT_RECONCILED
    assert not report.status
    assert "NOT RECONCILED" in report.headline()
    assert "NOT passes" in report.headline()
    assert len(report.passed) == 2 and len(report.not_reconciled) == 1


def test_the_traffic_light_is_green_amber_red_in_that_order() -> None:
    assert ReconciliationStatus.PASSED.colour == "green"
    assert ReconciliationStatus.NOT_RECONCILED.colour == "amber"
    assert ReconciliationStatus.FAILED.colour == "red"


def test_the_report_serialises_to_json_with_the_derived_status_made_explicit() -> None:
    report = ReconciliationReport(
        workbook="p.xlsx",
        tolerance=Tolerance(),
        cached_value_status="present",
        cached_value_coverage=1.0,
        regions=[_region(ReconciliationStatus.PASSED)],
    )

    payload = report.to_dict()
    text = json.dumps(payload)

    assert payload["status"] == "passed"
    assert payload["colour"] == "green"
    assert payload["region_counts"] == {"passed": 1, "failed": 0, "not_reconciled": 0}
    assert payload["rows_compared"] == 10
    assert "passed" in text


def test_non_finite_deltas_survive_a_strict_json_encoder() -> None:
    """inf is exactly what a divide-by-zero translation produces, so it reaches the report."""
    report = ReconciliationReport(
        workbook="p.xlsx",
        tolerance=Tolerance(),
        regions=[
            _region(
                ReconciliationStatus.FAILED,
                mismatches=[
                    Mismatch(
                        row=1,
                        kind=MismatchKind.NON_FINITE,
                        expected=1.0,
                        actual=float("inf"),
                    )
                ],
            )
        ],
    )

    text = json.dumps(report.to_dict(), allow_nan=False)

    assert "Infinity" in text


def test_diagnoses_are_merged_across_regions_most_confident_first() -> None:
    weak = Diagnosis(cause=DiagnosticCause.NULL_VS_EMPTY, confidence=0.5, headline="h", remedy="r")
    strong = Diagnosis(
        cause=DiagnosticCause.ROUNDING_MODE, confidence=0.9, headline="h", remedy="r"
    )
    report = ReconciliationReport(
        workbook="p.xlsx",
        tolerance=Tolerance(),
        regions=[
            _region(ReconciliationStatus.FAILED, spec_id="a", diagnoses=[weak]),
            _region(ReconciliationStatus.FAILED, spec_id="b", diagnoses=[strong]),
        ],
    )

    assert [d.cause for d in report.diagnoses] == [
        DiagnosticCause.ROUNDING_MODE,
        DiagnosticCause.NULL_VS_EMPTY,
    ]


def test_a_diagnosis_renders_on_one_line_for_a_cli_row() -> None:
    """A caller with one line to spend still has to be able to say what to do about it."""
    diagnosis = Diagnosis(
        cause=DiagnosticCause.ROUNDING_MODE,
        confidence=0.9,
        headline="Wrong rounding mode: polars rounds half to even.",
        remedy="Use round_half_away from kedge.xl.",
    )

    assert diagnosis.summary == (
        "90% Wrong rounding mode: polars rounds half to even. "
        "Fix: Use round_half_away from kedge.xl."
    )


def test_the_plain_text_rendering_carries_the_tolerance_and_the_mismatches() -> None:
    report = ReconciliationReport(
        workbook="p.xlsx",
        tolerance=Tolerance(absolute=1e-2, relative=0.0),
        cached_value_status="partial",
        cached_value_coverage=0.5,
        regions=[
            _region(
                ReconciliationStatus.FAILED,
                reference="Calc!G2:G11",
                mismatches=[Mismatch(row=4, cell="Calc!G5", expected=2.68, actual=2.67)],
                mismatches_truncated=True,
            )
        ],
    )

    text = str(report)

    assert "absolute 0.01" in text
    assert "cached values: partial (coverage 50.0%)" in text
    assert "excel=2.68 python=2.67" in text
    assert "further mismatching rows omitted" in text


def test_a_region_spec_falls_back_to_its_id_for_a_label() -> None:
    assert RegionSpec(id="rwa", reference="Calc!G2:G9").label_or_id == "rwa"
    assert RegionSpec(id="rwa", reference="Calc!G2:G9", label="RWA").label_or_id == "RWA"


def test_the_schema_forbids_unknown_keys() -> None:
    """A renamed field must fail loudly rather than being silently dropped on load."""
    with pytest.raises(ValidationError):
        Tolerance(absolute=1e-6, relatve=1e-9)


def test_a_penny_break_is_caught_at_every_magnitude() -> None:
    """The reason `relative` defaults to 0, pinned so it is not quietly reverted.

    A row matches within *either* tolerance, so a non-zero relative tolerance widens what
    passes as the numbers get bigger. At the old 1e-9 default, `0.01 / 5e7` is 2e-10 -- inside
    tolerance -- so a one-penny break on fifty million reconciled as PASSED. A reconciliation
    that forgives a penny on a large balance is worse than none, because it gets trusted.
    """
    tolerance = Tolerance()

    for magnitude in (100.0, 1_000_000.0, 50_000_000.0, 1_000_000_000.0):
        assert not tolerance.matches(magnitude, magnitude + 0.01), (
            f"a penny break at {magnitude:,.0f} must not match"
        )


def test_a_relative_tolerance_still_works_when_asked_for() -> None:
    """Proportional agreement is the real claim for ratios and rates, so keep it available."""
    proportional = Tolerance(absolute=0.0, relative=1e-9)

    assert proportional.matches(1e9, 1e9 + 0.5)
    assert not proportional.matches(1.0, 1.5)
    assert "relative" in proportional.describe()
