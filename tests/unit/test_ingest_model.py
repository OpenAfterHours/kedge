"""The records a hand-in reduces to, and the report describing how its shape moved.

`HandIn` is the convergence point of PLAN 2.8 -- two marimo entry points that return
different things, one record. These tests pin what the record promises: a managed path, a
digest that is what the audit line claims, and a stored form that survives a round trip
through the append-only index. A record that will not round trip is a receipt that cannot
be read back next month, which is the whole audit story gone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kedge.analysis.model import ColumnProfile, Severity
from kedge.ingest.model import (
    DriftItem,
    DriftKind,
    DriftReport,
    HandIn,
    HandInProfile,
    Upload,
    sort_drift,
    utcnow,
)

RECEIVED_AT = datetime(2026, 7, 25, 8, 14, 22, tzinfo=UTC)
SHA = "4f2b91c8ad03" + "0" * 52


def _handin(**overrides: object) -> HandIn:
    fields: dict[str, object] = {
        "path": Path("C:/proj/.kedge/handins/2026-07-25/4f2b91c8ad03-exposures.xlsx"),
        "original_name": "exposures.xlsx",
        "sha256": SHA,
        "size_bytes": 18_204,
        "received_at": RECEIVED_AT,
        "source": "selected",
    }
    fields.update(overrides)
    return HandIn(**fields)  # type: ignore[arg-type]


def _column(header: str, **overrides: object) -> ColumnProfile:
    fields: dict[str, object] = {
        "sheet": "",
        "column": "A",
        "index": 0,
        "header": header,
        "dtype": "Float64",
        "row_count": 100,
        "null_count": 0,
        "distinct_count": 100,
    }
    fields.update(overrides)
    return ColumnProfile(**fields)


def _profile(**overrides: object) -> HandInProfile:
    fields: dict[str, object] = {
        "sha256": SHA,
        "original_name": "exposures.xlsx",
        "profiled_at": RECEIVED_AT,
        "sheet": "Exposures",
        "row_count": 100,
        "column_count": 1,
        "header_row": 0,
        "preamble_rows": 0,
        "has_totals_row": False,
        "columns": (_column("EAD"),),
    }
    fields.update(overrides)
    return HandInProfile(**fields)  # type: ignore[arg-type]


# ── the dropped payload ─────────────────────────────────────────────────────


def test_an_upload_never_renders_its_contents_into_a_traceback() -> None:
    """A dropped hand-in can be a hundred megabytes; a repr of it must not be."""
    upload = Upload(name="exposures.xlsx", contents=b"x" * 4096)

    assert repr(upload) == "Upload(name='exposures.xlsx', contents=<4096 bytes>)"
    assert "xxxx" not in repr(upload)


# ── the hand-in record ──────────────────────────────────────────────────────


def test_short_hash_is_the_twelve_characters_that_appear_in_the_managed_filename() -> None:
    handin = _handin()

    assert handin.short_hash == "4f2b91c8ad03"
    assert handin.path.name.startswith(handin.short_hash)


def test_suffix_comes_from_the_original_name_and_is_lowercased() -> None:
    """The managed filename is hash-prefixed, so the sender's name is what carries the type."""
    assert _handin(original_name="EXPOSURES.XLSX").suffix == ".xlsx"
    assert _handin(original_name="extract").suffix == ""


def test_exists_reports_on_the_managed_copy(tmp_path: Path) -> None:
    managed = tmp_path / "4f2b91c8ad03-exposures.xlsx"
    handin = _handin(path=managed)
    assert not handin.exists()

    managed.write_bytes(b"stub")
    assert handin.exists()


def test_a_directory_is_not_an_existing_hand_in(tmp_path: Path) -> None:
    assert not _handin(path=tmp_path).exists()


def test_with_contract_returns_a_copy_and_leaves_the_original_alone() -> None:
    handin = _handin()

    bound = handin.with_contract("exposures")

    assert bound.contract == "exposures"
    assert handin.contract is None
    assert bound.sha256 == handin.sha256


def test_the_audit_line_states_what_this_run_consumed() -> None:
    """'This run consumed this file' is the claim the hash exists to make (PLAN 2.8)."""
    line = _handin().audit_line()

    assert line == (
        "selected 2026-07-25T08:14:22+00:00 sha256:4f2b91c8ad03 exposures.xlsx (18,204 bytes)"
    )


def test_a_hand_in_round_trips_through_its_stored_form() -> None:
    handin = _handin(contract="exposures")

    assert HandIn.from_dict(handin.to_dict()) == handin


@pytest.mark.parametrize("source", ["dropped", "selected", "watched"])
def test_every_source_label_round_trips(source: str) -> None:
    handin = _handin(source=source)

    assert HandIn.from_dict(handin.to_dict()).source == source


def test_an_unknown_source_is_a_value_error_rather_than_a_silently_accepted_label() -> None:
    raw = _handin().to_dict() | {"source": "emailed"}

    with pytest.raises(ValueError, match="unknown hand-in source"):
        HandIn.from_dict(raw)


def test_a_missing_field_is_a_value_error_naming_what_was_wrong() -> None:
    raw = _handin().to_dict()
    del raw["sha256"]

    with pytest.raises(ValueError, match="missing or has a malformed field"):
        HandIn.from_dict(raw)


def test_an_unparseable_timestamp_is_a_value_error() -> None:
    raw = _handin().to_dict() | {"received_at": "last Tuesday"}

    with pytest.raises(ValueError, match="missing or has a malformed field"):
        HandIn.from_dict(raw)


# ── the shape ───────────────────────────────────────────────────────────────


def test_column_names_fall_back_to_the_column_letter_when_a_header_is_absent() -> None:
    profile = _profile(
        columns=(_column("EAD"), _column(None, column="B", index=1)),
        column_count=2,
    )

    assert profile.column_names == ("EAD", "B")


def test_a_named_column_can_be_looked_up_and_an_absent_one_returns_none() -> None:
    profile = _profile()

    assert profile.column("EAD") is not None
    assert profile.column("EAD_GBP") is None


def test_a_profile_round_trips_through_its_stored_form() -> None:
    """The accepted shape is persisted; if it will not reload, drift has no baseline."""
    profile = _profile()

    assert HandInProfile.from_dict(profile.to_dict()) == profile


def test_a_malformed_stored_profile_is_a_value_error_rather_than_a_traceback() -> None:
    raw = _profile().to_dict() | {"row_count": "lots"}

    with pytest.raises(ValueError, match="stored hand-in profile is malformed"):
        HandInProfile.from_dict(raw)


def test_a_profile_with_no_columns_still_reloads() -> None:
    raw = _profile().to_dict()
    del raw["columns"]

    assert HandInProfile.from_dict(raw).columns == ()


# ── the drift report ────────────────────────────────────────────────────────


def _item(kind: DriftKind, severity: Severity, column: str | None = None) -> DriftItem:
    return DriftItem(
        kind=kind, severity=severity, message=f"{kind.value} on {column}", column=column
    )


def test_an_empty_report_is_ok_and_a_populated_one_is_not() -> None:
    assert DriftReport(current_sha256=SHA).ok
    assert not DriftReport(
        current_sha256=SHA,
        baseline_sha256="b" * 64,
        items=(_item(DriftKind.COLUMN_ADDED, Severity.INFO, "fee"),),
    ).ok


def test_a_first_hand_in_says_there_is_nothing_to_drift_from() -> None:
    """No baseline is a distinct outcome from 'nothing changed' and must read as one."""
    report = DriftReport(current_sha256=SHA)

    assert report.is_first_hand_in
    assert report.summary_line() == (
        "no previously accepted hand-in to compare against - nothing to drift from"
    )


def test_a_second_hand_in_with_no_differences_says_so_rather_than_saying_nothing() -> None:
    report = DriftReport(current_sha256=SHA, baseline_sha256="b" * 64)

    assert not report.is_first_hand_in
    assert report.summary_line() == "no shape drift against the last accepted hand-in"


def test_errors_and_warnings_are_separable_and_the_summary_counts_both() -> None:
    report = DriftReport(
        current_sha256=SHA,
        baseline_sha256="b" * 64,
        items=(
            _item(DriftKind.NUMERIC_TO_TEXT, Severity.ERROR, "EAD"),
            _item(DriftKind.DTYPE_CHANGED, Severity.WARNING, "fee"),
            _item(DriftKind.COLUMN_ADDED, Severity.INFO, "note"),
        ),
    )

    assert [i.column for i in report.errors] == ["EAD"]
    assert [i.column for i in report.warnings] == ["fee"]
    assert "3 shape difference(s)" in report.summary_line()
    assert "1 error, 1 warning" in report.summary_line()


def test_of_kind_filters_to_the_kinds_asked_for() -> None:
    report = DriftReport(
        current_sha256=SHA,
        baseline_sha256="b" * 64,
        items=(
            _item(DriftKind.COLUMN_ADDED, Severity.INFO, "note"),
            _item(DriftKind.COLUMN_REMOVED, Severity.ERROR, "EAD"),
        ),
    )

    assert [i.column for i in report.of_kind(DriftKind.COLUMN_REMOVED)] == ["EAD"]
    assert len(report.of_kind(DriftKind.COLUMN_ADDED, DriftKind.COLUMN_REMOVED)) == 2
    assert report.of_kind(DriftKind.SHEET_CHANGED) == ()


def test_an_item_renders_as_plain_data_a_table_widget_can_take() -> None:
    item = DriftItem(
        kind=DriftKind.COLUMN_RENAMED,
        severity=Severity.WARNING,
        column="EAD_GBP",
        before="EAD",
        after="EAD_GBP",
        message="column 'EAD' became 'EAD_GBP'.",
        remediation="Rename it back on read.",
    )

    assert item.to_dict() == {
        "kind": "column_renamed",
        "severity": "warning",
        "column": "EAD_GBP",
        "before": "EAD",
        "after": "EAD_GBP",
        "message": "column 'EAD' became 'EAD_GBP'.",
        "remediation": "Rename it back on read.",
    }
    # Plain data only: the same object has to render as a widget, a rich table and JSON.
    assert all(value is None or isinstance(value, str) for value in item.to_dict().values())


def test_the_whole_report_renders_as_plain_data() -> None:
    report = DriftReport(
        current_sha256=SHA,
        baseline_sha256="b" * 64,
        items=(_item(DriftKind.COLUMN_ADDED, Severity.INFO, "note"),),
    )

    payload = report.to_dict()

    assert payload["current_sha256"] == SHA
    assert payload["baseline_sha256"] == "b" * 64
    assert payload["ok"] is False
    assert payload["items"] == report.to_rows()


def test_the_text_rendering_leads_with_severity_and_carries_the_remediation() -> None:
    report = DriftReport(
        current_sha256=SHA,
        baseline_sha256="b" * 64,
        items=(
            DriftItem(
                kind=DriftKind.NUMERIC_TO_TEXT,
                severity=Severity.ERROR,
                column="EAD",
                message="column 'EAD' was Float64 and is now text.",
                remediation="Cast it back before any join.",
            ),
        ),
    )

    lines = report.render_text().splitlines()

    assert lines[1].startswith("  ERROR")
    assert "[EAD]" in lines[1]
    assert lines[2].strip() == "-> Cast it back before any join."


# ── ordering ────────────────────────────────────────────────────────────────


def test_drift_sorts_errors_first_so_the_dangerous_finding_is_read_first() -> None:
    """A numeric column arriving as text causes the silent join failure in PLAN 2.6."""
    items = [
        _item(DriftKind.COLUMN_ADDED, Severity.INFO, "note"),
        _item(DriftKind.DTYPE_CHANGED, Severity.WARNING, "fee"),
        _item(DriftKind.NUMERIC_TO_TEXT, Severity.ERROR, "EAD"),
    ]

    assert [i.severity for i in sort_drift(items)] == [
        Severity.ERROR,
        Severity.WARNING,
        Severity.INFO,
    ]


def test_within_a_severity_drift_sorts_by_kind_then_column() -> None:
    items = [
        _item(DriftKind.COLUMN_REMOVED, Severity.ERROR, "fee"),
        _item(DriftKind.COLUMN_REMOVED, Severity.ERROR, "EAD"),
        _item(DriftKind.NUMERIC_TO_TEXT, Severity.ERROR, "AAA"),
    ]

    assert [(i.kind.value, i.column) for i in sort_drift(items)] == [
        ("column_removed", "EAD"),
        ("column_removed", "fee"),
        ("numeric_to_text", "AAA"),
    ]


def test_an_item_with_no_column_sorts_without_raising() -> None:
    items = [
        DriftItem(kind=DriftKind.ROW_COUNT_SHIFT, severity=Severity.WARNING, message="moved"),
        _item(DriftKind.DTYPE_CHANGED, Severity.WARNING, "fee"),
    ]

    assert len(sort_drift(items)) == 2


# ── the clock ───────────────────────────────────────────────────────────────


def test_utcnow_is_timezone_aware_and_in_utc() -> None:
    """Every kedge timestamp is UTC, including the date that partitions the store."""
    now = utcnow()

    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)
