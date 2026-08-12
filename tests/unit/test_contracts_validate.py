"""The gate every downstream number passes through.

Three principles from the module docstring, and each gets its own assertion rather than
being taken on trust. **Every check runs** -- a missing column does not stop the row-count
check, because somebody correcting a hand-in wants the whole list, not four more rounds of
the same conversation. **A check that cannot run says so** -- SKIP, not FAIL, so it is
obvious the column itself is the problem. **Nothing is a bare boolean** -- every result names
the check, the column, what was expected and what was found.

Then the way it has to fail: `raise_for_status` stopping the dataflow graph dead, which is
the entire point of putting the contract cell first (PLAN M5).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest

from kedge.contracts.model import (
    CheckStatus,
    ChecksumMode,
    ColumnContract,
    Contract,
    ContractViolationError,
)
from kedge.contracts.validate import validate, validate_frame, validate_path
from kedge.ingest.drift import HandInReadError, Layout
from kedge.ingest.model import HandIn
from kedge.ingest.store import hash_file

STAMP = datetime(2026, 7, 25, 8, 14, 22, tzinfo=UTC)

CONFORMING = pl.DataFrame(
    {
        "counterparty": ["cp001", "cp002", "cp003"],
        "EAD": [1_000.0, 2_000.0, 3_000.0],
        "rating": ["A", "BBB", "A"],
    }
)


def _contract(**overrides: object) -> Contract:
    fields: dict[str, object] = {
        "name": "exposures",
        "columns": [
            ColumnContract(name="counterparty", dtype="String", nullable=False, unique=True),
            ColumnContract(name="EAD", dtype="Float64", nullable=False, min_value=0.0),
            ColumnContract(name="rating", dtype="String", allowed_values=["A", "BBB", "BB"]),
        ],
        "min_rows": 1,
        "max_rows": 100,
        "unique_key": ["counterparty"],
    }
    fields.update(overrides)
    return Contract(**fields)  # type: ignore[arg-type]


def _status(report: object, check: str, column: str | None = None) -> CheckStatus:
    """The status of one named check, so an assertion reads as the sentence it is."""
    matches = [
        c
        for c in report.checks  # type: ignore[attr-defined]
        if c.check == check and (column is None or c.column == column)
    ]
    assert len(matches) == 1, f"expected exactly one {check} check, found {len(matches)}"
    return matches[0].status


def _run(frame: pl.DataFrame, contract: Contract, **kwargs: object):
    return validate_frame(frame, contract, handin_name="exposures.xlsx", **kwargs)  # type: ignore[arg-type]


def _csv(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _handin(path: Path) -> HandIn:
    return HandIn(
        path=path,
        original_name=path.name,
        sha256=hash_file(path),
        size_bytes=path.stat().st_size,
        received_at=STAMP,
        source="selected",
    )


# ── the hand-in that conforms ───────────────────────────────────────────────


def test_a_conforming_hand_in_passes_every_check() -> None:
    report = _run(CONFORMING, _contract())

    assert report.ok
    assert report.failures == ()
    assert all(c.status is CheckStatus.PASS for c in report.checks)


def test_the_report_names_the_contract_and_the_file_it_was_run_against() -> None:
    report = validate_frame(
        CONFORMING, _contract(), handin_name="july.xlsx", handin_sha256="a" * 64
    )

    assert (report.contract_name, report.handin_name) == ("exposures", "july.xlsx")
    assert report.handin_sha256 == "a" * 64
    assert report.validated_at.tzinfo is not None


# ── presence ────────────────────────────────────────────────────────────────


def test_a_missing_required_column_fails_and_points_at_the_drift_report() -> None:
    """A column reported as missing here has usually been renamed, not removed."""
    report = _run(CONFORMING.drop("EAD"), _contract())

    failure = next(c for c in report.failures if c.check == "columns_present")
    assert "required column(s) absent: EAD" in failure.message
    assert "drift report" in (failure.detail or "")


def test_an_absent_column_skips_its_own_checks_rather_than_failing_them_twice() -> None:
    """Two reports of the same underlying problem is noise."""
    report = _run(CONFORMING.drop("EAD"), _contract())

    assert _status(report, "dtype", "EAD") is CheckStatus.SKIP
    assert [c.check for c in report.failures] == ["columns_present"]


def test_an_absent_optional_column_is_not_a_failure_at_all() -> None:
    contract = _contract(
        columns=[
            ColumnContract(name="counterparty", dtype="String"),
            ColumnContract(name="netting_set", dtype="String", required=False),
        ],
        unique_key=[],
    )

    report = _run(CONFORMING, contract)

    assert report.ok
    assert not any(c.column == "netting_set" for c in report.checks)


def test_an_extra_column_is_permitted_by_default_but_still_reported() -> None:
    frame = CONFORMING.with_columns(pl.lit("EMEA").alias("region"))

    report = _run(frame, _contract())

    assert report.ok
    assert _status(report, "no_extra_columns") is CheckStatus.PASS
    assert "region" in next(c for c in report.checks if c.check == "no_extra_columns").message


def test_an_extra_column_fails_when_the_contract_forbids_one() -> None:
    frame = CONFORMING.with_columns(pl.lit("EMEA").alias("region"))

    report = _run(frame, _contract(allow_extra_columns=False))

    assert _status(report, "no_extra_columns") is CheckStatus.FAIL


def test_column_order_is_only_checked_when_the_contract_asks_for_it() -> None:
    reordered = CONFORMING.select(["EAD", "counterparty", "rating"])

    assert _run(reordered, _contract()).ok
    assert _status(_run(reordered, _contract(require_column_order=True)), "column_order") is (
        CheckStatus.FAIL
    )


# ── dtypes ──────────────────────────────────────────────────────────────────


def test_a_wrong_dtype_fails_and_names_both_sides() -> None:
    frame = CONFORMING.with_columns(pl.col("EAD").cast(pl.Int64))
    contract = _contract(
        columns=[ColumnContract(name="EAD", dtype="Date")], unique_key=[], min_rows=None
    )

    report = _run(frame, contract)

    failure = next(c for c in report.failures if c.check == "dtype")
    assert (failure.expected, failure.actual) == ("Date", "Int64")
    assert "should be Date but arrived as Int64" in failure.message


def test_a_whole_numbered_column_satisfies_a_float_expectation() -> None:
    """A whole-numbered month is a real and harmless occurrence."""
    frame = CONFORMING.with_columns(pl.col("EAD").cast(pl.Int64))

    assert _status(_run(frame, _contract()), "dtype", "EAD") is CheckStatus.PASS


def test_a_float_column_does_not_satisfy_an_integer_expectation() -> None:
    contract = _contract(
        columns=[ColumnContract(name="EAD", dtype="Int64")], unique_key=[], min_rows=None
    )

    assert _status(_run(CONFORMING, contract), "dtype", "EAD") is CheckStatus.FAIL


@pytest.mark.parametrize("wildcard", ["Any", "any", "*"])
def test_the_wildcard_dtype_accepts_whatever_arrived(wildcard: str) -> None:
    contract = _contract(
        columns=[ColumnContract(name="EAD", dtype=wildcard)], unique_key=[], min_rows=None
    )

    assert _status(_run(CONFORMING, contract), "dtype", "EAD") is CheckStatus.PASS


def test_a_number_arriving_as_text_fails_and_explains_the_silent_join_failure() -> None:
    """The widening that is emphatically not permitted (PLAN 2.6)."""
    frame = CONFORMING.with_columns(pl.col("EAD").cast(pl.String))

    report = _run(frame, _contract())

    failure = next(c for c in report.failures if c.check == "dtype")
    assert failure.actual == "String"
    assert "PLAN 2.6" in (failure.detail or "")
    assert "rows drop out" in (failure.detail or "")


def test_a_parameterised_dtype_compares_on_its_base_name() -> None:
    frame = pl.DataFrame({"as_of": [datetime(2026, 7, 25, tzinfo=UTC)]})
    contract = Contract(name="d", columns=[ColumnContract(name="as_of", dtype="Datetime")])

    assert _status(_run(frame, contract), "dtype", "as_of") is CheckStatus.PASS


# ── nullability, uniqueness, ranges, permitted values ───────────────────────


def test_a_null_in_a_not_null_column_fails_and_counts_them() -> None:
    frame = CONFORMING.with_columns(
        pl.when(pl.col("counterparty") == "cp002").then(None).otherwise(pl.col("EAD")).alias("EAD")
    )

    report = _run(frame, _contract())

    failure = next(c for c in report.failures if c.check == "not_null")
    assert failure.column == "EAD"
    assert failure.actual == "1 nulls"


def test_a_nullable_column_is_not_null_checked_at_all() -> None:
    contract = _contract(
        columns=[ColumnContract(name="rating", dtype="String")], unique_key=[], min_rows=None
    )

    assert not any(c.check == "not_null" for c in _run(CONFORMING, contract).checks)


def test_a_duplicated_value_in_a_unique_column_fails() -> None:
    frame = CONFORMING.with_columns(pl.lit("cp001").alias("counterparty"))

    report = _run(frame, _contract())

    failure = next(c for c in report.failures if c.check == "unique")
    assert failure.actual == "2 duplicate value(s)"
    assert "inflates every total downstream" in (failure.detail or "")


def test_a_duplicated_key_fails_before_any_join_can_multiply_the_rows() -> None:
    """An exact VLOOKUP translated to a left join multiplies rows when the key repeats."""
    frame = pl.concat([CONFORMING, CONFORMING.head(1)])

    report = _run(frame, _contract(columns=[]))

    failure = next(c for c in report.failures if c.check == "unique_key")
    assert failure.actual == "1 duplicate key(s)"
    assert "PLAN 2.6" in (failure.detail or "")


def test_a_key_whose_columns_are_absent_is_skipped_rather_than_passed() -> None:
    """Reporting a key as unique when it was never checked would be the wrong kind of quiet."""
    report = _run(CONFORMING.drop("counterparty"), _contract(columns=[]))

    assert _status(report, "unique_key") is CheckStatus.SKIP
    assert (
        "counterparty absent" in next(c for c in report.checks if c.check == "unique_key").message
    )


def test_a_composite_key_is_checked_across_its_columns() -> None:
    frame = pl.DataFrame({"book": ["a", "a", "b"], "date": ["d1", "d2", "d1"]})
    contract = Contract(name="k", unique_key=["book", "date"])

    assert _run(frame, contract).ok
    assert (
        _status(_run(pl.concat([frame, frame.head(1)]), contract), "unique_key") is CheckStatus.FAIL
    )


def test_a_value_below_the_contracted_floor_fails() -> None:
    frame = CONFORMING.with_columns(pl.Series("EAD", [1_000.0, -5.0, 3_000.0]))

    report = _run(frame, _contract())

    failure = next(c for c in report.failures if c.check == "value_range")
    assert "1 value(s) below and 0 above" in failure.message


def test_a_range_check_on_a_column_with_no_numeric_values_is_skipped() -> None:
    contract = _contract(
        columns=[ColumnContract(name="rating", dtype="String", min_value=0.0)],
        unique_key=[],
        min_rows=None,
    )

    assert _status(_run(CONFORMING, contract), "value_range", "rating") is CheckStatus.SKIP


def test_a_value_outside_the_permitted_set_fails_and_lists_the_offenders() -> None:
    frame = CONFORMING.with_columns(pl.Series("rating", ["A", "CCC", "D"]))

    report = _run(frame, _contract())

    failure = next(c for c in report.failures if c.check == "allowed_values")
    assert failure.actual == "CCC, D"


def test_a_long_list_of_unexpected_values_is_truncated_with_a_count() -> None:
    frame = pl.DataFrame({"rating": [f"X{i}" for i in range(9)]})
    contract = Contract(
        name="r", columns=[ColumnContract(name="rating", dtype="String", allowed_values=["A"])]
    )

    failure = next(c for c in _run(frame, contract).failures if c.check == "allowed_values")

    assert "... and 4 more" in (failure.actual or "")


# ── row counts ──────────────────────────────────────────────────────────────


def test_a_row_count_inside_the_bounds_passes() -> None:
    assert _status(_run(CONFORMING, _contract()), "row_count") is CheckStatus.PASS


def test_too_few_rows_fails_and_names_the_usual_cause() -> None:
    report = _run(CONFORMING, _contract(min_rows=10))

    failure = next(c for c in report.failures if c.check == "row_count")
    assert failure.expected == "10 to 100"
    assert "wrong period" in (failure.detail or "")


def test_too_many_rows_fails() -> None:
    assert _status(_run(CONFORMING, _contract(max_rows=2)), "row_count") is CheckStatus.FAIL


def test_a_contract_with_no_bounds_does_not_check_the_row_count() -> None:
    contract = _contract(min_rows=None, max_rows=None)

    assert not any(c.check == "row_count" for c in _run(CONFORMING, contract).checks)


def test_one_sided_bounds_render_the_open_end_as_a_dash() -> None:
    report = _run(CONFORMING, _contract(min_rows=None, max_rows=2))

    assert next(c for c in report.checks if c.check == "row_count").expected == "- to 2"


# ── the checksum ────────────────────────────────────────────────────────────


def test_a_matching_checksum_passes() -> None:
    report = validate_frame(
        CONFORMING,
        _contract(checksum="a" * 64, checksum_mode=ChecksumMode.REQUIRE),
        handin_name="exposures.xlsx",
        handin_sha256="a" * 64,
    )

    assert _status(report, "checksum") is CheckStatus.PASS


def test_a_different_file_only_warns_by_default_because_that_is_the_monthly_case() -> None:
    report = validate_frame(
        CONFORMING,
        _contract(checksum="a" * 64),
        handin_name="exposures.xlsx",
        handin_sha256="b" * 64,
    )

    assert _status(report, "checksum") is CheckStatus.WARN
    assert report.ok
    assert "Set checksum_mode: require" in (report.warnings[0].detail or "")


def test_a_different_file_fails_when_the_contract_requires_the_bytes() -> None:
    report = validate_frame(
        CONFORMING,
        _contract(checksum="a" * 64, checksum_mode=ChecksumMode.REQUIRE),
        handin_name="exposures.xlsx",
        handin_sha256="b" * 64,
    )

    assert not report.ok
    assert _status(report, "checksum") is CheckStatus.FAIL


@pytest.mark.parametrize(
    "contract",
    [_contract(checksum=None), _contract(checksum="a" * 64, checksum_mode=ChecksumMode.IGNORE)],
)
def test_no_checksum_check_runs_when_there_is_nothing_to_compare(contract: Contract) -> None:
    report = validate_frame(
        CONFORMING, contract, handin_name="exposures.xlsx", handin_sha256="b" * 64
    )

    assert not any(c.check == "checksum" for c in report.checks)


# ── every check runs ────────────────────────────────────────────────────────


def test_one_broken_column_does_not_stop_the_rest_of_the_report() -> None:
    """The whole list, not the first problem in file order."""
    frame = pl.DataFrame(
        {
            "counterparty": ["cp001", "cp001", None],
            "EAD": ["1000", "2000", "3000"],
            "rating": ["A", "CCC", "A"],
        }
    )

    report = _run(frame, _contract(max_rows=2))

    assert {c.check for c in report.failures} == {
        "dtype",
        "not_null",
        "unique",
        "allowed_values",
        "row_count",
        "unique_key",
    }


# ── what the reader had to skip ─────────────────────────────────────────────


def test_a_layout_the_reader_worked_around_is_reported_rather_than_left_silent() -> None:
    layout = Layout(header_row=2, preamble_rows=2, totals_row_excluded=True)

    report = _run(CONFORMING, _contract(), layout=layout)

    note = next(c for c in report.checks if c.check == "layout")
    assert note.status is CheckStatus.PASS
    assert "skipped 2 preamble row(s)" in note.message
    assert "excluded a totals row" in note.message
    assert "A totals row is layout, not data" in (note.detail or "")


def test_a_layout_with_nothing_to_report_adds_no_check() -> None:
    layout = Layout(header_row=0, preamble_rows=0, totals_row_excluded=False)

    assert not any(c.check == "layout" for c in _run(CONFORMING, _contract(), layout=layout).checks)


# ── validating a real file ──────────────────────────────────────────────────


def test_validating_a_hand_in_reads_through_the_same_data_view_profiling_uses(
    tmp_path: Path,
) -> None:
    """Preamble skipped, totals row excluded -- otherwise the totals row fails not-null."""
    path = _csv(
        tmp_path / "exposures.csv",
        "Monthly exposures extract\n"
        "counterparty,EAD,rating\n"
        "cp001,1000.0,A\ncp002,2000.0,BBB\ncp003,3000.0,A\n"
        "Total,6000.0,\n",
    )

    report = validate(_handin(path), _contract())

    assert report.ok
    assert (
        "skipped 1 preamble row(s)" in next(c for c in report.checks if c.check == "layout").message
    )


def test_validating_a_hand_in_compares_the_checksum_against_the_receipt(tmp_path: Path) -> None:
    path = _csv(tmp_path / "exposures.csv", "counterparty,EAD,rating\ncp001,1000.0,A\n")
    handin = _handin(path)

    report = validate(handin, _contract(checksum=handin.sha256, checksum_mode=ChecksumMode.REQUIRE))

    assert _status(report, "checksum") is CheckStatus.PASS


def test_validating_a_path_hashes_the_file_itself(tmp_path: Path) -> None:
    """There is no receipt, so there is no audit trail proving which bytes were read."""
    path = _csv(tmp_path / "exposures.csv", "counterparty,EAD,rating\ncp001,1000.0,A\n")

    report = validate_path(path, _contract(checksum=hash_file(path)))

    assert report.handin_sha256 == hash_file(path)
    assert report.handin_name == "exposures.csv"
    assert _status(report, "checksum") is CheckStatus.PASS


def test_a_hand_in_that_cannot_be_read_raises_rather_than_reporting_a_failed_check(
    tmp_path: Path,
) -> None:
    """A failure to run the checks is not a check result."""
    path = tmp_path / "exposures.xlsx"
    path.write_bytes(b"this was never a workbook")

    with pytest.raises(HandInReadError):
        validate_path(path, _contract())


def test_the_contract_can_pin_the_sheet_and_the_header_row(tmp_path: Path) -> None:
    path = _csv(
        tmp_path / "exposures.csv",
        "junk\ncounterparty,EAD,rating\ncp001,1000.0,A\n",
    )

    report = validate_path(path, _contract(header_row=1, min_rows=1, max_rows=1))

    assert report.ok


# ── failing loudly ──────────────────────────────────────────────────────────


def test_validate_returns_a_report_rather_than_raising_so_the_cell_renders_both_states() -> None:
    report = _run(CONFORMING.drop("EAD"), _contract())

    assert not report.ok
    assert report.to_rows()


def test_raise_for_status_is_what_stops_the_graph(tmp_path: Path) -> None:
    path = _csv(tmp_path / "exposures.csv", "counterparty,EAD,rating\ncp001,-5.0,ZZZ\n")

    with pytest.raises(ContractViolationError, match="value_range"):
        validate(_handin(path), _contract()).raise_for_status()


# ── awkward frames ──────────────────────────────────────────────────────────


def test_a_frame_with_no_rows_is_checked_rather_than_skipped() -> None:
    empty = CONFORMING.head(0)

    report = _run(empty, _contract())

    assert _status(report, "row_count") is CheckStatus.FAIL
    assert _status(report, "not_null", "EAD") is CheckStatus.PASS
    assert _status(report, "value_range", "EAD") is CheckStatus.SKIP


def test_a_frame_with_no_columns_fails_on_presence_rather_than_raising() -> None:
    report = _run(pl.DataFrame(), _contract())

    assert _status(report, "columns_present") is CheckStatus.FAIL
    assert _status(report, "unique_key") is CheckStatus.SKIP


def test_a_contract_with_no_columns_still_checks_the_shape_it_does_declare() -> None:
    report = _run(CONFORMING, Contract(name="rows-only", min_rows=1, max_rows=2))

    assert _status(report, "row_count") is CheckStatus.FAIL
    assert _status(report, "columns_present") is CheckStatus.PASS


def test_a_date_column_range_checks_against_its_underlying_number() -> None:
    """Casting a temporal column to a float is legal in polars, so the check runs."""
    frame = pl.DataFrame({"as_of": [date(2026, 7, 25)]})
    contract = Contract(
        name="d", columns=[ColumnContract(name="as_of", dtype="Date", min_value=0.0)]
    )

    assert _status(_run(frame, contract), "value_range", "as_of") is CheckStatus.PASS
