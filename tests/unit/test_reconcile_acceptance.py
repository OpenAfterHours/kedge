"""Reconciling once and citing afterwards (:mod:`kedge.reconcile.acceptance`).

The module's whole argument is that a translation is checked against its workbook once, at
conversion, and that the record of it has to outlive the spreadsheet -- which is deleted,
superseded or archived within a few months of a conversion. These tests exist because that was
true of the record and, until recently, false of the code.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kedge.errors import ReconciliationError
from kedge.reconcile.acceptance import AcceptanceStore, check_translation
from kedge.reconcile.model import (
    NotReconciledReason,
    ReconciliationReport,
    ReconciliationStatus,
    RegionResult,
    Tolerance,
)

DIGEST = "a" * 64
ACTUALS: dict[str, object] = {"accruals": [1.0, 2.0]}


# ── fixtures and doubles ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path: Path) -> AcceptanceStore:
    return AcceptanceStore(tmp_path / "reconciliation.json")


@pytest.fixture
def workbook(tmp_path: Path) -> Path:
    """A workbook whose only relevant property is that it is there.

    The comparison engine is replaced in every test that reaches it, because what is under test
    is which branch :func:`check_translation` takes, not what ``reconcile_workbook`` computes --
    that has its own suite in ``test_reconcile_compare.py``.
    """
    path = tmp_path / "q2_accruals.xlsx"
    path.write_bytes(b"PK\x03\x04")
    return path


def _region(spec_id: str, status: ReconciliationStatus) -> RegionResult:
    return RegionResult(
        spec_id=spec_id,
        status=status,
        tolerance=Tolerance(),
        reason=(
            NotReconciledReason.NO_CACHED_VALUES
            if status is ReconciliationStatus.NOT_RECONCILED
            else None
        ),
        rows_expected=10,
        rows_actual=10,
        rows_compared=10,
        rows_matched=10 if status is ReconciliationStatus.PASSED else 0,
        rows_differing=0 if status is not ReconciliationStatus.FAILED else 10,
    )


def _report(workbook: Path, *regions: RegionResult) -> ReconciliationReport:
    return ReconciliationReport(
        workbook=str(workbook), tolerance=Tolerance(), regions=list(regions)
    )


def _engine(monkeypatch: pytest.MonkeyPatch, result: ReconciliationReport | Exception) -> list[str]:
    """Replace the comparison engine, and record every time it was reached.

    Reaching it at all is the assertion in half of these tests: a workbook that is gone must be
    cited against, not opened and failed against.
    """
    calls: list[str] = []

    def _run(workbook_path: object, *_args: object, **_kwargs: object) -> ReconciliationReport:
        calls.append(str(workbook_path))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr("kedge.reconcile.acceptance.reconcile_workbook", _run)
    return calls


def _accept(store: AcceptanceStore, workbook: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Put a genuine passing acceptance on file, the way a first run would."""
    _engine(monkeypatch, _report(workbook, _region("accruals", ReconciliationStatus.PASSED)))
    check_translation(store, workbook, ACTUALS, handin_sha256=DIGEST)
    assert store.load() is not None


# ── the ordinary cases, which must not change ────────────────────────────────────────────────


def test_the_first_run_compares_against_the_workbook_and_records_a_pass(
    store: AcceptanceStore, workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The conversion-time acceptance test. Everything else in the module is downstream of it."""
    calls = _engine(
        monkeypatch, _report(workbook, _region("accruals", ReconciliationStatus.PASSED))
    )

    check = check_translation(store, workbook, ACTUALS, handin_sha256=DIGEST)

    assert calls == [str(workbook)]
    assert check.live is not None
    assert check.translation_accepted is True
    assert check.status is ReconciliationStatus.PASSED
    recorded = store.load()
    assert recorded is not None
    assert recorded.handin_sha256 == DIGEST


def test_a_first_reconciliation_that_fails_is_a_failure_and_records_nothing(
    store: AcceptanceStore, workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a reconciliation that stands up becomes an acceptance, so the reason must not go on
    announcing that the result was recorded on exactly the runs where it was not."""
    _engine(monkeypatch, _report(workbook, _region("accruals", ReconciliationStatus.FAILED)))

    check = check_translation(store, workbook, ACTUALS, handin_sha256=DIGEST)

    assert check.translation_accepted is False
    assert check.status is ReconciliationStatus.FAILED
    assert store.load() is None
    assert "The result is recorded" not in check.reason
    assert "Nothing has been recorded" in check.reason


def test_a_later_period_cites_rather_than_comparing(
    store: AcceptanceStore, workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workbook is Q2 and the run is Q3; the numbers are supposed to differ, and comparing
    them is the permanently amber signal this module exists to remove."""
    _accept(store, workbook, monkeypatch)
    calls = _engine(monkeypatch, _report(workbook))

    check = check_translation(store, workbook, ACTUALS, handin_sha256="b" * 64)

    assert calls == []
    assert check.live is None
    assert check.translation_accepted is True
    assert check.status is ReconciliationStatus.NOT_RECONCILED
    assert not check, "a citation is falsy: nothing was compared on this run"


def test_the_accepted_month_re_run_against_a_present_workbook_is_compared_live(
    store: AcceptanceStore, workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one case where a failure means something -- somebody edited the translation -- and it
    has to keep saying so."""
    _accept(store, workbook, monkeypatch)
    _engine(monkeypatch, _report(workbook, _region("accruals", ReconciliationStatus.FAILED)))

    check = check_translation(store, workbook, ACTUALS, handin_sha256=DIGEST)

    assert check.live is not None
    assert check.translation_accepted is False
    assert "edited into disagreeing" in check.reason


# ── the workbook is gone ─────────────────────────────────────────────────────────────────────


def test_a_released_workbook_with_a_recorded_acceptance_is_cited_and_stays_accepted(
    store: AcceptanceStore, workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The successful end of a conversion: the notebook is the process and the spreadsheet is
    retired. The record was written down for exactly this moment, and has to hold."""
    _accept(store, workbook, monkeypatch)
    workbook.unlink()
    calls = _engine(monkeypatch, ReconciliationError("this must never be reached"))

    check = check_translation(store, workbook, ACTUALS, handin_sha256="c" * 64)

    assert calls == [], "a workbook that is gone is cited against, never opened and failed against"
    assert check.live is None
    assert check.acceptance is not None
    assert check.translation_accepted is True
    assert "no longer on disk" in check.reason


def test_a_released_workbook_with_no_record_is_not_reconciled(
    store: AcceptanceStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-negotiable 6 is absolute and sympathy for the reader's position does not bend it: an
    unmeasured translation is not an accepted one, whatever became of the workbook."""
    gone = tmp_path / "q2_accruals.xlsx"
    calls = _engine(monkeypatch, ReconciliationError("this must never be reached"))

    check = check_translation(store, gone, ACTUALS, handin_sha256=DIGEST)

    assert calls == []
    assert check.translation_accepted is False
    assert check.status is ReconciliationStatus.NOT_RECONCILED
    assert not check
    assert check.acceptance is None
    assert "never been reconciled" in check.summary_line()


def test_a_released_workbook_with_no_record_does_not_send_the_user_hunting_for_it(
    store: AcceptanceStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old note read "the workbook does not exist. Check the path." -- addressed to somebody
    who had just deliberately deleted it. The message has to lead with what would actually
    record an acceptance, and admit that there may never be one."""
    gone = tmp_path / "q2_accruals.xlsx"
    _engine(monkeypatch, ReconciliationError("this must never be reached"))

    rendered = check_translation(store, gone, ACTUALS).render()

    assert "Check the path" not in rendered
    assert rendered.index("To record an acceptance, restore") < rendered.index("retired")
    assert "will simply never carry an acceptance" in rendered


def test_re_running_the_accepted_month_after_a_release_accuses_nobody(
    store: AcceptanceStore, workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running the month that was accepted is the first thing a colleague does after a
    handover. It used to report that the notebook had been edited into disagreeing with a
    workbook kedge itself had deleted on the user's instruction."""
    _accept(store, workbook, monkeypatch)
    workbook.unlink()
    _engine(monkeypatch, ReconciliationError("this must never be reached"))

    check = check_translation(store, workbook, ACTUALS, handin_sha256=DIGEST)

    assert check.translation_accepted is True
    assert "edited into disagreeing" not in check.render()
    assert check.colour == "amber"


# ── a comparison that could not run is not a comparison that failed ──────────────────────────


def test_a_comparison_that_could_not_run_does_not_overrule_the_record(
    store: AcceptanceStore, workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Independent of release, and the root of it. A present-but-unreadable workbook yields a
    report with no verdict either way, and an empty report was being read as a failed one -- so
    a good acceptance was shadowed and the panel blamed the reader for a file it could not open.
    """
    _accept(store, workbook, monkeypatch)
    _engine(monkeypatch, ReconciliationError("not a readable zip archive"))

    check = check_translation(store, workbook, ACTUALS, handin_sha256=DIGEST)

    assert check.live is not None, "the workbook is present, so the comparison is still attempted"
    assert check.translation_accepted is True, "an empty report is not evidence of a regression"
    assert check.status is ReconciliationStatus.NOT_RECONCILED
    assert "edited into disagreeing" not in check.reason
    assert "establishes nothing either way" in check.reason


def test_a_region_that_was_compared_and_differs_still_overrules_the_record(
    store: AcceptanceStore, workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side of the same rule, and the more important one: forgiving a real failure
    because 'we passed once before' would mask exactly the regression this is for."""
    _accept(store, workbook, monkeypatch)
    _engine(monkeypatch, _report(workbook, _region("accruals", ReconciliationStatus.FAILED)))

    check = check_translation(store, workbook, ACTUALS, handin_sha256=DIGEST)

    assert check.translation_accepted is False


# ── the store ────────────────────────────────────────────────────────────────────────────────


def test_an_acceptance_survives_the_round_trip_through_disk(
    store: AcceptanceStore, workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record is the whole mechanism; one that does not reload is no record at all."""
    _accept(store, workbook, monkeypatch)

    reloaded = AcceptanceStore(store.path).load()

    assert reloaded is not None
    assert reloaded.passed is True
    assert reloaded.workbook == workbook.name
    assert reloaded.handin_sha256 == DIGEST
    assert workbook.name in reloaded.cite()


def test_an_unreadable_acceptance_record_reads_as_never_checked(
    store: AcceptanceStore, tmp_path: Path
) -> None:
    """ "Never checked" is both the safe reading and the true one, and a corrupt sidecar must not
    be able to stop the notebook opening."""
    store.path.write_text("{ this is not json", encoding="utf-8")

    assert store.load() is None
