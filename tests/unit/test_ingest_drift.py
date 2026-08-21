"""Reading a hand-in, profiling it, and saying what moved since last month.

PLAN 2.8's argument for this module is a single sentence: "column EAD became EAD_GBP" beats
a schema traceback. So the assertions below are about the *message*, not only the detection
-- a rename must be one finding rather than a drop plus an add, a preamble row must be named
as a preamble row, and a numeric column arriving as text must carry error severity, because
that is the silent join failure in PLAN 2.6 where rows drop out and the total is simply
smaller with nothing anywhere saying so.

The reader half is tested against real files rather than mocks. Whether a CSV with a title
row above the header can be read at all is not a question a stubbed reader can answer.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import openpyxl
import polars as pl
import pytest

from kedge.analysis.model import Severity
from kedge.config import RedactionConfig, SamplingConfig
from kedge.ingest import store
from kedge.ingest.drift import (
    DEFAULT_ROW_COUNT_TOLERANCE,
    HandInReadError,
    check_drift,
    compare,
    detect_layout,
    detect_totals_row,
    dtype_name,
    profile_frame,
    profile_handin,
    read_data,
    read_frame,
)
from kedge.ingest.model import DriftKind, HandIn, HandInProfile

STAMP = datetime(2026, 7, 25, 8, 14, 22, tzinfo=UTC)

COUNTERPARTIES = [f"cp{index:03d}" for index in range(1, 41)]
EXPOSURES = [float(index * 1_000) for index in range(1, 41)]


def _csv(path: Path, *, header: str, rows: list[str], preamble: list[str] | None = None) -> Path:
    lines = [*(preamble or []), header, *rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _exposures_csv(path: Path, **kwargs: object) -> Path:
    rows = [f"{name},{ead}" for name, ead in zip(COUNTERPARTIES, EXPOSURES, strict=True)]
    return _csv(path, header="counterparty,EAD", rows=rows, **kwargs)  # type: ignore[arg-type]


def _xlsx(path: Path, rows: list[list[object]], *, sheet: str = "Sheet1") -> Path:
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = sheet
    for row in rows:
        worksheet.append(row)
    workbook.save(path)
    return path


def _handin(path: Path, *, name: str | None = None, sha256: str | None = None) -> HandIn:
    digest = sha256 or store.hash_file(path)
    return HandIn(
        path=path,
        original_name=name or path.name,
        sha256=digest,
        size_bytes=path.stat().st_size,
        received_at=STAMP,
        source="selected",
    )


def _profile(frame: pl.DataFrame, **overrides: object) -> HandInProfile:
    """A `HandInProfile` over a frame built in memory, so compare() sees real column profiles."""
    fields: dict[str, object] = {
        "sha256": "a" * 64,
        "original_name": "exposures.csv",
        "profiled_at": STAMP,
        "sheet": "",
        "row_count": frame.height,
        "column_count": frame.width,
        "header_row": 0,
        "preamble_rows": 0,
        "has_totals_row": False,
        "columns": tuple(profile_frame(frame)),
    }
    fields.update(overrides)
    return HandInProfile(**fields)  # type: ignore[arg-type]


BASELINE_FRAME = pl.DataFrame({"counterparty": COUNTERPARTIES, "EAD": EXPOSURES})


# ── reading ─────────────────────────────────────────────────────────────────


def test_a_csv_reads_with_its_headers_applied(tmp_path: Path) -> None:
    frame = read_frame(_exposures_csv(tmp_path / "exposures.csv"))

    assert frame.columns == ["counterparty", "EAD"]
    assert frame.height == 40


def test_a_tab_separated_hand_in_reads_on_its_extension(tmp_path: Path) -> None:
    path = tmp_path / "exposures.tsv"
    path.write_text("counterparty\tEAD\ncp001\t1000.0\n", encoding="utf-8")

    assert read_frame(path).columns == ["counterparty", "EAD"]


def test_a_parquet_hand_in_reads(tmp_path: Path) -> None:
    path = tmp_path / "exposures.parquet"
    BASELINE_FRAME.write_parquet(path)

    assert read_frame(path).height == 40


def test_a_workbook_reads_through_calamine_with_no_excel_installed(tmp_path: Path) -> None:
    path = _xlsx(tmp_path / "exposures.xlsx", [["counterparty", "EAD"], ["cp001", 1000.0]])

    assert read_frame(path).columns == ["counterparty", "EAD"]


def test_a_file_that_is_not_a_spreadsheet_names_what_would_have_worked(tmp_path: Path) -> None:
    path = tmp_path / "handover.pdf"
    path.write_bytes(b"%PDF-1.4")

    with pytest.raises(HandInReadError) as excinfo:
        read_frame(path)

    assert "'.pdf'" in str(excinfo.value)
    assert ".xlsx" in str(excinfo.value)


def test_a_workbook_that_is_not_a_zip_archive_is_a_hand_in_read_error(tmp_path: Path) -> None:
    """calamine raises fastexcel's own error type, which is not a PolarsError. It must not
    escape as a bare traceback (CONVENTIONS non-negotiable 4)."""
    path = tmp_path / "exposures.xlsx"
    path.write_bytes(b"this was renamed from a .csv by somebody in a hurry")

    with pytest.raises(HandInReadError, match=r"could not read the hand-in exposures\.xlsx"):
        read_frame(path)


def test_an_empty_file_is_a_hand_in_read_error(tmp_path: Path) -> None:
    path = tmp_path / "exposures.csv"
    path.write_bytes(b"")

    with pytest.raises(HandInReadError, match="could not read the hand-in"):
        read_frame(path)


def test_a_sheet_that_is_not_in_the_workbook_is_a_hand_in_read_error(tmp_path: Path) -> None:
    path = _xlsx(tmp_path / "exposures.xlsx", [["counterparty", "EAD"], ["cp001", 1000.0]])

    with pytest.raises(HandInReadError, match="could not read the hand-in"):
        read_frame(path, sheet="Exposures")


# ── layout detection ────────────────────────────────────────────────────────


def test_a_file_that_starts_with_its_headers_has_no_preamble(tmp_path: Path) -> None:
    assert detect_layout(_exposures_csv(tmp_path / "exposures.csv")) == (0, 0)


def test_a_title_row_above_a_csv_header_is_found_and_skipped(tmp_path: Path) -> None:
    """An extra preamble row shifts every column by a row without changing anything a schema
    check would notice, and is one of the named drift cases in PLAN 2.8."""
    path = _exposures_csv(
        tmp_path / "exposures.csv", preamble=["Monthly exposures extract", "generated 2026-07-25"]
    )

    assert detect_layout(path) == (2, 2)

    frame, layout = read_data(path)
    assert frame.columns == ["counterparty", "EAD"]
    assert frame.height == 40
    assert layout.preamble_rows == 2


def test_a_title_row_above_a_workbook_header_is_found_and_skipped(tmp_path: Path) -> None:
    path = _xlsx(
        tmp_path / "exposures.xlsx",
        [["Monthly exposures extract"], [], ["counterparty", "EAD"], ["cp001", 1000.0]],
    )

    assert detect_layout(path) == (2, 2)
    assert read_data(path)[0].columns == ["counterparty", "EAD"]


def test_a_data_row_carrying_an_unquoted_delimiter_does_not_become_the_header(
    tmp_path: Path,
) -> None:
    """An unquoted comma inside a company name is the commonest bad CSV export there is, and
    it makes exactly one data row wider than the header. Ranking rows by the widest would
    promote that row, discard the real header as preamble and lose a row from the frame --
    a quietly wrong answer where a loud read failure is the honest one."""
    path = tmp_path / "exposures.csv"
    path.write_text(
        "counterparty,rating\nAcme, Inc,A\nBeta Ltd,BBB\nGamma plc,A\n", encoding="utf-8"
    )

    assert detect_layout(path) == (0, 0)

    with pytest.raises(HandInReadError, match=r"could not read the hand-in exposures\.csv"):
        read_data(path)


def test_a_ragged_row_below_a_preamble_still_leaves_the_header_findable(tmp_path: Path) -> None:
    """The two cases have to coexist: the preamble is narrower than the file's usual shape and
    the malformed row is wider, so the shape the rows agree on is what identifies the header."""
    path = tmp_path / "exposures.csv"
    path.write_text(
        "Monthly exposures extract\ncounterparty,rating\nAcme, Inc,A\nBeta Ltd,BBB\n",
        encoding="utf-8",
    )

    assert detect_layout(path) == (1, 1)


def test_an_all_text_data_row_is_not_mistaken_for_a_header(tmp_path: Path) -> None:
    """The header must have no cell that parses as a number, which is what rules a data row out."""
    path = _csv(
        tmp_path / "exposures.csv",
        header="counterparty,EAD",
        rows=["cp001,1000.0", "cp002,2000.0"],
    )

    assert detect_layout(path) == (0, 0)


def test_a_scalar_block_beside_the_grid_does_not_hide_the_header_row(tmp_path: Path) -> None:
    """A worksheet is a sheet with tables drawn on it, not a table.

    A rate card with `Minimum fee | 750` and `Maximum fee | 250000` parked two columns to its
    right is completely ordinary, and the header row shares a row with one of them. Testing
    every populated cell for numerality rejected that row for carrying `250000` three columns
    away, `detect_layout` fell back to (0, 0), and `read_data` then read the sheet's *title*
    as the header -- one unusable column where a rate card should be.
    """
    path = _xlsx(
        tmp_path / "schedule.xlsx",
        [
            ["Fee schedule -- effective 2026-11-01"],
            ["Do not edit."],
            [None, None, None, "Minimum fee", 750.0],
            ["client_code", "negotiated_bps", None, "Maximum fee", 250000.0],
            ["00001", "20.0", None, "Standard discount", 0.025],
            ["00006", "27.5"],
            ["00011", "21.5"],
        ],
    )

    assert detect_layout(path) == (3, 3)

    frame, layout = read_data(path)
    assert frame.columns[:2] == ["client_code", "negotiated_bps"]
    assert layout.preamble_rows == 3
    assert frame["client_code"].to_list()[:2] == ["00001", "00006"], (
        "the leading zeros are the client code, and must survive the read"
    )


def test_a_data_row_beside_a_text_side_block_is_still_not_a_header(tmp_path: Path) -> None:
    """The narrowing must not let a data row through on the strength of a caption beside it.

    The grid is the widest run of adjacent cells, so it is the grid that gets the numeric
    test -- a text label parked to the right cannot vouch for a row of numbers.
    """
    path = _xlsx(
        tmp_path / "schedule.xlsx",
        [
            ["00001", "20.0", None, "Standard discount", 0.025],
            ["client_code", "negotiated_bps", None, "Maximum fee", 250000.0],
            ["00006", "27.5"],
        ],
    )

    assert detect_layout(path) == (1, 1)


def test_a_layout_that_cannot_be_determined_degrades_to_the_first_row(tmp_path: Path) -> None:
    path = tmp_path / "exposures.xlsx"
    path.write_bytes(b"not a workbook")

    assert detect_layout(path) == (0, 0)


def test_a_format_with_no_raw_view_degrades_to_the_first_row(tmp_path: Path) -> None:
    path = tmp_path / "exposures.parquet"
    BASELINE_FRAME.write_parquet(path)

    assert detect_layout(path) == (0, 0)


def test_a_numeric_looking_row_of_full_width_is_stepped_over(tmp_path: Path) -> None:
    path = tmp_path / "exposures.csv"
    path.write_text("1,2\ncounterparty,EAD\ncp001,1000.0\n", encoding="utf-8")

    assert detect_layout(path) == (1, 1)


def test_a_row_with_repeated_labels_is_not_taken_for_a_header(tmp_path: Path) -> None:
    path = tmp_path / "exposures.csv"
    path.write_text("total,total\ncounterparty,EAD\ncp001,1000.0\n", encoding="utf-8")

    assert detect_layout(path) == (1, 1)


def test_a_file_where_no_row_looks_like_a_header_degrades_to_the_first_row(
    tmp_path: Path,
) -> None:
    path = tmp_path / "exposures.csv"
    path.write_text("1,2\n3,4\n5,6\n", encoding="utf-8")

    assert detect_layout(path) == (0, 0)


def test_a_file_of_blank_lines_degrades_to_the_first_row(tmp_path: Path) -> None:
    path = tmp_path / "exposures.csv"
    path.write_text(",\n,\n,\n", encoding="utf-8")

    assert detect_layout(path) == (0, 0)


def test_a_delimited_file_that_will_not_decode_degrades_rather_than_raising(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A hand-in exported as cp1252 is somebody else's mistake, not a traceback."""
    path = tmp_path / "exposures.csv"
    path.write_bytes("counterparty,préavis\ncp001,1\n".encode("cp1252"))

    with caplog.at_level(logging.WARNING, logger="kedge.ingest.drift"):
        assert detect_layout(path) == (0, 0)

    assert "could not scan exposures.csv for preamble rows" in caplog.text


def test_an_explicit_header_row_overrides_detection(tmp_path: Path) -> None:
    path = _exposures_csv(tmp_path / "exposures.csv", preamble=["Monthly exposures extract"])

    frame, layout = read_data(path, header_row=1)

    assert frame.columns == ["counterparty", "EAD"]
    assert layout.header_row == 1


# ── totals rows ─────────────────────────────────────────────────────────────


def test_a_labelled_totals_row_is_detected_on_the_label_alone() -> None:
    frame = pl.DataFrame({"counterparty": ["cp001", "cp002", "Total"], "EAD": [1.0, 2.0, 99.0]})

    assert detect_totals_row(frame)


@pytest.mark.parametrize("label", ["Total", "TOTALS", " grand total ", "Sum", "Subtotal", "Total:"])
def test_every_spelling_of_a_totals_label_is_recognised(label: str) -> None:
    frame = pl.DataFrame({"counterparty": ["cp001", label], "EAD": [1.0, 2.0]})

    assert detect_totals_row(frame)


def test_a_counterparty_whose_name_starts_with_total_is_a_row_of_data() -> None:
    """Matched exactly rather than by prefix: Total Energies SE is a counterparty."""
    frame = pl.DataFrame({"counterparty": ["cp001", "Total Energies SE"], "EAD": [1.0, 2.0]})

    assert not detect_totals_row(frame)


def test_an_unlabelled_totals_row_needs_the_arithmetic_and_a_corroborating_blank() -> None:
    frame = pl.DataFrame(
        {
            "counterparty": ["cp001", "cp002", "cp003", "cp004", "cp005", None],
            "EAD": [1.0, 2.0, 3.0, 4.0, 5.0, 15.0],
        }
    )

    assert detect_totals_row(frame)


def test_an_unlabelled_last_row_whose_numbers_do_not_add_up_is_data() -> None:
    """A false positive discards a real row, which is worse than missing an unlabelled total."""
    frame = pl.DataFrame(
        {
            "counterparty": ["cp001", "cp002", "cp003", "cp004", "cp005", None],
            "EAD": [1.0, 2.0, 3.0, 4.0, 5.0, 16.0],
        }
    )

    assert not detect_totals_row(frame)


def test_a_column_that_sums_to_zero_neither_confirms_nor_denies_a_totals_row() -> None:
    """A net-zero column agrees with any last value, so it is no evidence either way."""
    frame = pl.DataFrame(
        {
            "counterparty": ["cp001", "cp002", "cp003", "cp004", "cp005", None],
            "net": [1.0, -1.0, 2.0, -2.0, 0.0, 0.0],
            "EAD": [1.0, 2.0, 3.0, 4.0, 5.0, 15.0],
        }
    )

    assert detect_totals_row(frame)


def test_a_frame_of_nothing_but_numbers_has_no_corroborating_blank_to_find() -> None:
    frame = pl.DataFrame({"EAD": [1.0, 2.0, 3.0, 4.0, 5.0, 15.0]})

    assert not detect_totals_row(frame)


def test_the_corroborating_blank_has_to_come_from_an_otherwise_complete_column() -> None:
    """A column with gaps of its own says nothing about the gap on the last row."""
    frame = pl.DataFrame(
        {
            "counterparty": ["cp001", None, "cp003", "cp004", "cp005", None],
            "EAD": [1.0, 2.0, 3.0, 4.0, 5.0, 15.0],
        }
    )

    assert not detect_totals_row(frame)


def test_a_text_column_populated_on_the_last_row_does_not_corroborate() -> None:
    frame = pl.DataFrame(
        {
            "counterparty": ["cp001", "cp002", "cp003", "cp004", "cp005", "cp006"],
            "EAD": [1.0, 2.0, 3.0, 4.0, 5.0, 15.0],
        }
    )

    assert not detect_totals_row(frame)


def test_a_frame_too_short_to_judge_has_no_totals_row() -> None:
    assert not detect_totals_row(pl.DataFrame({"EAD": [1.0]}))
    assert not detect_totals_row(pl.DataFrame({"EAD": []}))


def test_the_data_view_excludes_a_totals_row_and_says_it_did(tmp_path: Path) -> None:
    rows = [f"{name},{ead}" for name, ead in zip(COUNTERPARTIES, EXPOSURES, strict=True)]
    path = _csv(
        tmp_path / "exposures.csv",
        header="counterparty,EAD",
        rows=[*rows, f"Total,{sum(EXPOSURES)}"],
    )

    frame, layout = read_data(path)

    assert frame.height == 40
    assert layout.totals_row_excluded
    assert layout.notes() == ["excluded a totals row from the bottom"]


def test_a_layout_with_nothing_to_work_around_has_no_notes(tmp_path: Path) -> None:
    _frame, layout = read_data(_exposures_csv(tmp_path / "exposures.csv"))

    assert layout.notes() == []


# ── profiling ───────────────────────────────────────────────────────────────


def test_dtype_names_collapse_parameterised_dtypes_to_their_base() -> None:
    """A contract that says "Datetime" means "a datetime", not a time unit."""
    assert dtype_name(pl.Datetime(time_unit="us")) == "Datetime"
    assert dtype_name(pl.Datetime(time_unit="ms")) == "Datetime"
    assert dtype_name(pl.Float64) == "Float64"


def test_a_profile_carries_the_column_letter_alongside_the_header() -> None:
    columns = profile_frame(pl.DataFrame({f"c{i}": [1] for i in range(28)}))

    assert [c.column for c in columns[:3]] == ["A", "B", "C"]
    assert columns[26].column == "AA"
    assert columns[27].column == "AB"


def test_a_numeric_column_profiles_its_range_and_its_nulls() -> None:
    frame = pl.DataFrame({"EAD": [1.0, None, 3.0, -2.0, 0.0]})

    profile = profile_frame(frame)[0]

    assert profile.dtype == "Float64"
    assert profile.null_count == 1
    assert profile.numeric is not None
    assert (profile.numeric.min, profile.numeric.max, profile.numeric.sum) == (-2.0, 3.0, 2.0)
    assert (profile.numeric.zero_count, profile.numeric.negative_count) == (1, 1)


def test_numbers_stored_as_text_are_flagged_as_a_format_anomaly() -> None:
    frame = pl.DataFrame({"EAD": ["1,000.00", "2,000.00", "3,000.00"]})

    assert "numbers stored as text" in profile_frame(frame)[0].format_anomalies


def test_placeholders_that_are_not_numbers_are_not_mistaken_for_them() -> None:
    """A dash or a blank is how an extract writes "nothing here", not a value."""
    frame = pl.DataFrame({"EAD": ["-", "", "n/a"]})

    assert profile_frame(frame)[0].format_anomalies == []


def test_untrimmed_values_are_flagged_as_a_format_anomaly() -> None:
    frame = pl.DataFrame({"counterparty": ["cp001 ", "cp002"]})

    assert "values carry leading or trailing whitespace" in profile_frame(frame)[0].format_anomalies


def test_a_redacted_column_still_reports_its_dtype_and_null_count_but_no_values() -> None:
    """Dtype and null count travel; values do not (PLAN 2.3)."""
    frame = pl.DataFrame({"customer_name": ["Alice", None], "EAD": [1.0, 2.0]})
    redaction = RedactionConfig(enabled=True, column_patterns=("name",))

    profiles = profile_frame(frame, redaction=redaction)

    assert profiles[0].redacted
    assert profiles[0].null_count == 1
    assert profiles[0].head == []
    assert profiles[0].top_k == []
    assert not profiles[1].redacted


def test_redaction_that_is_configured_but_disabled_redacts_nothing() -> None:
    frame = pl.DataFrame({"customer_name": ["Alice"]})
    redaction = RedactionConfig(enabled=False, column_patterns=("name",))

    assert not profile_frame(frame, redaction=redaction)[0].redacted


def test_sampling_caps_control_how_much_of_a_column_travels() -> None:
    frame = pl.DataFrame({"EAD": list(range(100))})
    sampling = SamplingConfig(head_rows=2, tail_rows=1, random_rows=3, top_k=4)

    profile = profile_frame(frame, sampling=sampling)[0]

    assert len(profile.head) == 2
    assert len(profile.tail) == 1
    assert len(profile.sample) == 3
    assert len(profile.top_k) == 4


def test_a_sample_is_reproducible_for_a_given_seed() -> None:
    frame = pl.DataFrame({"EAD": list(range(100))})

    assert profile_frame(frame, seed=7)[0].sample == profile_frame(frame, seed=7)[0].sample


def test_a_column_the_sending_team_called_count_still_profiles() -> None:
    """`value_counts` names its tally column "count". A hand-in column of the same name --
    an ordinary thing for an extract to contain -- used to make polars raise DuplicateError
    straight out of profiling."""
    frame = pl.DataFrame({"count": [1, 1, 2], "value": ["a", "a", "b"]})

    profiles = profile_frame(frame)

    assert [p.header for p in profiles] == ["count", "value"]
    assert profiles[0].top_k[0] == (1, 2)
    assert profiles[1].top_k[0] == ("a", 2)


def test_a_hand_in_with_no_rows_profiles_without_raising() -> None:
    frame = pl.DataFrame(schema={"counterparty": pl.String, "EAD": pl.Float64})

    profiles = profile_frame(frame)

    assert [p.row_count for p in profiles] == [0, 0]
    assert profiles[0].head == []
    assert profiles[0].top_k == []


def test_a_frame_with_no_columns_profiles_as_nothing() -> None:
    assert profile_frame(pl.DataFrame()) == []


def test_a_column_of_dates_survives_the_json_round_trip_a_profile_has_to_make() -> None:
    frame = pl.DataFrame({"as_of": [datetime(2026, 7, 25, tzinfo=UTC).date()]})

    profile = profile_frame(frame)[0]

    assert profile.head == ["2026-07-25"]


def test_a_timezone_aware_column_reads_as_the_same_instant_with_no_zone(tmp_path: Path) -> None:
    """polars builds a `zoneinfo.ZoneInfo` to hand a tz-aware cell back to Python, and on an
    interpreter with no IANA database that fails inside Rust and arrives as a PanicException --
    a BaseException, so it walks past every degrade-gracefully path in the module and takes
    the process with it. It killed `detect_totals_row` on the last row before profiling was
    even reached, so the conversion belongs on the read."""
    path = tmp_path / "exposures.parquet"
    pl.DataFrame({"as_of": [STAMP], "EAD": [1000.0]}).write_parquet(path)

    frame, _layout = read_data(path)

    assert dtype_name(frame.schema["as_of"]) == "Datetime"
    assert frame.schema["as_of"].time_zone is None
    assert frame["as_of"].to_list() == [STAMP.replace(tzinfo=None)]


def test_a_zone_other_than_utc_is_converted_rather_than_simply_dropped(tmp_path: Path) -> None:
    """Dropping the zone would record the local wall time, which moves an hour when the clocks
    do; converting keeps the instant, so two months' hand-ins stay comparable."""
    path = tmp_path / "exposures.parquet"
    pl.DataFrame({"as_of": [STAMP]}).with_columns(
        pl.col("as_of").dt.convert_time_zone("Europe/London")
    ).write_parquet(path)

    frame, _layout = read_data(path)

    assert frame["as_of"].to_list() == [STAMP.replace(tzinfo=None)]


def test_profiling_a_hand_in_with_a_time_zone_produces_a_profile_not_a_crash(
    tmp_path: Path,
) -> None:
    path = tmp_path / "exposures.parquet"
    pl.DataFrame({"as_of": [STAMP], "EAD": [1000.0]}).write_parquet(path)

    as_of = profile_handin(_handin(path)).column("as_of")

    assert as_of is not None
    assert as_of.dtype == "Datetime"
    assert as_of.head == ["2026-07-25T08:14:22"]


def test_an_in_memory_frame_carrying_a_zone_is_converted_by_profiling_too() -> None:
    """`profile_frame` is public and takes any frame, so it cannot assume its caller read the
    file through this module."""
    frame = pl.DataFrame({"as_of": [STAMP]})

    assert frame.schema["as_of"].time_zone == "UTC"
    assert profile_frame(frame)[0].head == ["2026-07-25T08:14:22"]


def test_every_awkward_cell_type_reduces_to_something_json_can_carry() -> None:
    """Profiles are persisted and travel to the model, so nothing exotic may survive."""
    frame = pl.DataFrame(
        {
            "amount": pl.Series([Decimal("1.50")], dtype=pl.Decimal(precision=8, scale=2)),
            "blob": pl.Series([b"\x00\x01\x02"], dtype=pl.Binary),
            "window": pl.Series([timedelta(hours=2)], dtype=pl.Duration),
        }
    )

    profiles = profile_frame(frame)

    assert profiles[0].head == [1.5]
    assert profiles[1].head == ["<3 bytes>"]
    assert isinstance(profiles[2].head[0], str)


def test_an_infinite_value_is_dropped_rather_than_written_into_json() -> None:
    frame = pl.DataFrame({"ratio": [1.0, float("inf")]})

    profile = profile_frame(frame)[0]

    assert profile.head == [1.0, None]
    assert profile.numeric is not None
    assert profile.numeric.max is None


def test_profiling_a_hand_in_excludes_the_totals_row_from_the_row_count(tmp_path: Path) -> None:
    """A totals row left in would poison every sum and surface next month as false drift."""
    rows = [f"{name},{ead}" for name, ead in zip(COUNTERPARTIES, EXPOSURES, strict=True)]
    path = _csv(
        tmp_path / "exposures.csv",
        header="counterparty,EAD",
        rows=[*rows, f"Total,{sum(EXPOSURES)}"],
    )

    profile = profile_handin(_handin(path))

    assert profile.row_count == 40
    assert profile.has_totals_row
    ead = profile.column("EAD")
    assert ead is not None
    assert ead.numeric is not None
    assert ead.numeric.sum == pytest.approx(sum(EXPOSURES))


def test_profiling_an_unreadable_hand_in_raises_rather_than_returning_a_hollow_profile(
    tmp_path: Path,
) -> None:
    path = tmp_path / "exposures.xlsx"
    path.write_bytes(b"not a workbook")

    with pytest.raises(HandInReadError):
        profile_handin(_handin(path))


# ── comparison: no baseline ─────────────────────────────────────────────────


def test_the_first_hand_in_reports_no_drift_rather_than_every_column_as_new() -> None:
    report = compare(None, _profile(BASELINE_FRAME))

    assert report.ok
    assert report.is_first_hand_in
    assert report.items == ()


def test_an_unchanged_hand_in_reports_nothing() -> None:
    baseline = _profile(BASELINE_FRAME, sha256="b" * 64)

    report = compare(baseline, _profile(BASELINE_FRAME))

    assert report.ok
    assert not report.is_first_hand_in
    assert report.baseline_sha256 == "b" * 64


# ── comparison: columns ─────────────────────────────────────────────────────


def test_a_renamed_column_is_one_finding_and_not_a_drop_plus_an_add() -> None:
    """The whole argument of PLAN 2.8: this message beats a schema traceback."""
    current = BASELINE_FRAME.rename({"EAD": "EAD_GBP"})

    report = compare(_profile(BASELINE_FRAME, sha256="b" * 64), _profile(current))

    renames = report.of_kind(DriftKind.COLUMN_RENAMED)
    assert len(renames) == 1
    assert renames[0].message == "column 'EAD' became 'EAD_GBP'."
    assert renames[0].severity is Severity.WARNING
    assert report.of_kind(DriftKind.COLUMN_ADDED, DriftKind.COLUMN_REMOVED) == ()


def test_a_new_column_is_information_rather_than_a_problem() -> None:
    current = BASELINE_FRAME.with_columns(pl.lit("EMEA").alias("region"))

    report = compare(_profile(BASELINE_FRAME, sha256="b" * 64), _profile(current))

    added = report.of_kind(DriftKind.COLUMN_ADDED)
    assert [i.column for i in added] == ["region"]
    assert added[0].severity is Severity.INFO


def test_a_removed_column_is_an_error_because_the_code_reading_it_will_fail() -> None:
    current = BASELINE_FRAME.drop("EAD")

    report = compare(_profile(BASELINE_FRAME, sha256="b" * 64), _profile(current))

    removed = report.of_kind(DriftKind.COLUMN_REMOVED)
    assert [i.column for i in removed] == ["EAD"]
    assert removed[0].severity is Severity.ERROR
    assert "no longer present" in removed[0].message


def test_two_unrelated_columns_swapping_out_are_reported_as_a_drop_and_an_add() -> None:
    """A rename is only claimed when the evidence supports it."""
    current = BASELINE_FRAME.drop("EAD").with_columns(pl.Series("booking_desk", ["rates"] * 40))

    report = compare(_profile(BASELINE_FRAME, sha256="b" * 64), _profile(current))

    assert [i.column for i in report.of_kind(DriftKind.COLUMN_REMOVED)] == ["EAD"]
    assert [i.column for i in report.of_kind(DriftKind.COLUMN_ADDED)] == ["booking_desk"]
    assert report.of_kind(DriftKind.COLUMN_RENAMED) == ()


def test_a_column_respelled_with_punctuation_is_still_the_same_column() -> None:
    """Names are compared ignoring case, spacing and punctuation."""
    current = BASELINE_FRAME.rename({"EAD": "e_a_d"})

    report = compare(_profile(BASELINE_FRAME, sha256="b" * 64), _profile(current))

    assert [i.message for i in report.of_kind(DriftKind.COLUMN_RENAMED)] == [
        "column 'EAD' became 'e_a_d'."
    ]


def test_a_renamed_column_that_also_changed_dtype_within_a_family_is_still_paired() -> None:
    baseline = pl.DataFrame({"as_of": [date(2026, 7, 25)] * 3, "EAD": [1.0, 2.0, 3.0]})
    current = pl.DataFrame(
        {"as_of_date": [datetime(2026, 7, 25, 9, 0)] * 3, "EAD": [1.0, 2.0, 3.0]}
    )

    report = compare(_profile(baseline, sha256="b" * 64), _profile(current))

    assert [i.column for i in report.of_kind(DriftKind.COLUMN_RENAMED)] == ["as_of_date"]


def test_a_removed_column_is_paired_with_only_its_best_match() -> None:
    """Each column is used at most once, so two similar names cannot both claim one rename."""
    baseline = pl.DataFrame({"EAD": [1.0, 2.0, 3.0]})
    current = pl.DataFrame({"EAD_GBP": [1.0, 2.0, 3.0], "EAD_USD": [9.0, 9.0, 9.0]})

    report = compare(_profile(baseline, sha256="b" * 64), _profile(current))

    assert [i.column for i in report.of_kind(DriftKind.COLUMN_RENAMED)] == ["EAD_GBP"]
    assert [i.column for i in report.of_kind(DriftKind.COLUMN_ADDED)] == ["EAD_USD"]


def test_redacted_columns_are_paired_on_their_names_alone() -> None:
    """Values were withheld, so there are no contents to agree on (PLAN 2.3)."""
    frame = pl.DataFrame({"customer_name": ["Alice", "Bob"], "EAD": [1.0, 2.0]})
    redaction = RedactionConfig(enabled=True, column_patterns=("name",))
    baseline = _profile(frame, sha256="b" * 64)
    current = HandInProfile(
        sha256="a" * 64,
        original_name="exposures.csv",
        profiled_at=STAMP,
        sheet="",
        row_count=2,
        column_count=2,
        header_row=0,
        preamble_rows=0,
        has_totals_row=False,
        columns=tuple(
            profile_frame(frame.rename({"customer_name": "customer_names"}), redaction=redaction)
        ),
    )

    report = compare(baseline, current)

    assert [i.column for i in report.of_kind(DriftKind.COLUMN_RENAMED)] == ["customer_names"]


def test_a_column_named_only_with_punctuation_offers_no_name_evidence() -> None:
    """Names are compared with punctuation stripped, so these two normalise to nothing."""
    baseline = pl.DataFrame({"#": ["a", "b"]})
    current = pl.DataFrame({"%": ["a", "b"]})

    report = compare(_profile(baseline, sha256="b" * 64), _profile(current))

    assert report.of_kind(DriftKind.COLUMN_RENAMED) == ()
    assert [i.column for i in report.of_kind(DriftKind.COLUMN_REMOVED)] == ["#"]
    assert [i.column for i in report.of_kind(DriftKind.COLUMN_ADDED)] == ["%"]


def test_two_all_null_numeric_columns_are_paired_on_their_names_alone() -> None:
    baseline = pl.DataFrame({"fee": pl.Series([None, None], dtype=pl.Float64)})
    current = pl.DataFrame({"fee_gbp": pl.Series([None, None], dtype=pl.Float64)})

    report = compare(_profile(baseline, sha256="b" * 64), _profile(current))

    assert [i.column for i in report.of_kind(DriftKind.COLUMN_RENAMED)] == ["fee_gbp"]


def test_two_all_infinite_numeric_columns_are_paired_on_their_names_alone() -> None:
    """An infinity is dropped rather than written into a profile, so a column of nothing but
    infinities has no comparable statistic to offer and the pairing rests on the name."""
    baseline = pl.DataFrame({"fee": [float("inf"), float("inf")]})
    current = pl.DataFrame({"fee_gbp": [float("inf"), float("inf")]})

    report = compare(_profile(baseline, sha256="b" * 64), _profile(current))

    assert [i.column for i in report.of_kind(DriftKind.COLUMN_RENAMED)] == ["fee_gbp"]


def test_two_empty_text_columns_are_paired_on_their_names_alone() -> None:
    baseline = pl.DataFrame({"note": pl.Series([None], dtype=pl.String)})
    current = pl.DataFrame({"notes": pl.Series([None], dtype=pl.String)})

    report = compare(_profile(baseline, sha256="b" * 64), _profile(current))

    assert [i.column for i in report.of_kind(DriftKind.COLUMN_RENAMED)] == ["notes"]


def test_reordered_columns_are_reported_once_and_are_harmless() -> None:
    current = BASELINE_FRAME.select(["EAD", "counterparty"])

    report = compare(_profile(BASELINE_FRAME, sha256="b" * 64), _profile(current))

    reordered = report.of_kind(DriftKind.COLUMN_REORDERED)
    assert len(reordered) == 1
    assert reordered[0].severity is Severity.INFO
    assert "generated code" in (reordered[0].remediation or "")


# ── comparison: dtypes ──────────────────────────────────────────────────────


def test_a_numeric_column_arriving_as_text_is_an_error_not_a_warning() -> None:
    """PLAN 2.6's silent join failure: the key matches nothing, rows drop, the total shrinks."""
    current = BASELINE_FRAME.with_columns(pl.col("EAD").cast(pl.String))

    report = compare(_profile(BASELINE_FRAME, sha256="b" * 64), _profile(current))

    items = report.of_kind(DriftKind.NUMERIC_TO_TEXT)
    assert len(items) == 1
    assert items[0].severity is Severity.ERROR
    assert items[0].column == "EAD"
    assert (items[0].before, items[0].after) == ("Float64", "String")
    assert "coerce_numeric" in (items[0].remediation or "")


def test_a_numeric_column_arriving_as_text_says_so_when_the_values_still_parse() -> None:
    current = BASELINE_FRAME.with_columns(pl.col("EAD").cast(pl.String))

    report = compare(_profile(BASELINE_FRAME, sha256="b" * 64), _profile(current))

    assert "formatting change at source" in report.of_kind(DriftKind.NUMERIC_TO_TEXT)[0].message


def test_the_numeric_to_text_finding_sorts_above_every_lesser_one() -> None:
    current = BASELINE_FRAME.with_columns(pl.col("EAD").cast(pl.String)).with_columns(
        pl.lit("EMEA").alias("region")
    )

    report = compare(_profile(BASELINE_FRAME, sha256="b" * 64), _profile(current))

    assert report.items[0].kind is DriftKind.NUMERIC_TO_TEXT
    assert report.errors[0] is report.items[0]


def test_a_widening_between_two_numeric_dtypes_is_a_warning() -> None:
    baseline = pl.DataFrame({"count": [1, 2, 3]})
    current = pl.DataFrame({"count": [1.0, 2.0, 3.0]})

    report = compare(_profile(baseline, sha256="b" * 64), _profile(current))

    changed = report.of_kind(DriftKind.DTYPE_CHANGED)
    assert changed[0].severity is Severity.WARNING
    assert (changed[0].before, changed[0].after) == ("Int64", "Float64")


# ── comparison: layout and size ─────────────────────────────────────────────


def test_an_extra_preamble_row_is_reported_as_the_header_moving() -> None:
    baseline = _profile(BASELINE_FRAME, sha256="b" * 64, preamble_rows=0)
    current = _profile(BASELINE_FRAME, preamble_rows=2, header_row=2)

    report = compare(baseline, current)

    moved = report.of_kind(DriftKind.PREAMBLE_ROWS_CHANGED)
    assert len(moved) == 1
    assert moved[0].severity is Severity.WARNING
    assert (moved[0].before, moved[0].after) == ("0", "2")
    assert "kedge skipped the preamble automatically" in (moved[0].remediation or "")


def test_a_totals_row_appended_at_the_bottom_is_a_warning_about_double_counting() -> None:
    baseline = _profile(BASELINE_FRAME, sha256="b" * 64, has_totals_row=False)
    current = _profile(BASELINE_FRAME, has_totals_row=True)

    report = compare(baseline, current)

    appended = report.of_kind(DriftKind.TOTALS_ROW_APPENDED)
    assert len(appended) == 1
    assert appended[0].severity is Severity.WARNING
    assert "double counted" in (appended[0].remediation or "")


def test_a_totals_row_disappearing_is_only_information() -> None:
    baseline = _profile(BASELINE_FRAME, sha256="b" * 64, has_totals_row=True)
    current = _profile(BASELINE_FRAME, has_totals_row=False)

    removed = compare(baseline, current).of_kind(DriftKind.TOTALS_ROW_REMOVED)

    assert removed[0].severity is Severity.INFO


def test_reading_a_different_sheet_is_reported() -> None:
    baseline = _profile(BASELINE_FRAME, sha256="b" * 64, sheet="Exposures")
    current = _profile(BASELINE_FRAME, sheet="Exposures v2")

    changed = compare(baseline, current).of_kind(DriftKind.SHEET_CHANGED)

    assert (changed[0].before, changed[0].after) == ("Exposures", "Exposures v2")


def test_a_row_count_within_tolerance_is_not_drift() -> None:
    """Monthly extracts breathe; 20% is a lurch."""
    current = BASELINE_FRAME.head(36)

    report = compare(_profile(BASELINE_FRAME, sha256="b" * 64), _profile(current))

    assert report.of_kind(DriftKind.ROW_COUNT_SHIFT) == ()


def test_a_row_count_beyond_tolerance_is_a_warning() -> None:
    current = BASELINE_FRAME.head(20)

    report = compare(_profile(BASELINE_FRAME, sha256="b" * 64), _profile(current))

    shift = report.of_kind(DriftKind.ROW_COUNT_SHIFT)
    assert shift[0].severity is Severity.WARNING
    assert "40 to 20" in shift[0].message
    assert f"{DEFAULT_ROW_COUNT_TOLERANCE:.0%}" in shift[0].message


def test_an_empty_hand_in_is_an_error_and_says_what_usually_caused_it() -> None:
    current = BASELINE_FRAME.head(0)

    report = compare(_profile(BASELINE_FRAME, sha256="b" * 64), _profile(current))

    shift = report.of_kind(DriftKind.ROW_COUNT_SHIFT)
    assert shift[0].severity is Severity.ERROR
    assert "wrong period" in (shift[0].remediation or "")


def test_a_baseline_with_no_rows_gives_nothing_to_measure_a_shift_against() -> None:
    """Dividing by last month's zero rows would be a percentage of nothing."""
    baseline = _profile(BASELINE_FRAME.head(0), sha256="b" * 64)

    report = compare(baseline, _profile(BASELINE_FRAME))

    assert report.of_kind(DriftKind.ROW_COUNT_SHIFT) == ()


def test_a_null_rate_rising_beyond_tolerance_is_a_warning() -> None:
    baseline = pl.DataFrame({"rating": ["A"] * 10})
    current = pl.DataFrame({"rating": ["A"] * 5 + [None] * 5})

    report = compare(_profile(baseline, sha256="b" * 64), _profile(current))

    shift = report.of_kind(DriftKind.NULL_RATE_SHIFT)
    assert shift[0].severity is Severity.WARNING
    assert (shift[0].before, shift[0].after) == ("0.0%", "50.0%")
    assert "omitting a field" in (shift[0].remediation or "")


def test_a_null_rate_falling_is_only_information() -> None:
    baseline = pl.DataFrame({"rating": ["A"] * 5 + [None] * 5})
    current = pl.DataFrame({"rating": ["A"] * 10})

    shift = compare(_profile(baseline, sha256="b" * 64), _profile(current)).of_kind(
        DriftKind.NULL_RATE_SHIFT
    )

    assert shift[0].severity is Severity.INFO


# ── the whole ingest check ──────────────────────────────────────────────────


def test_check_drift_stores_the_profile_but_does_not_accept_it(tmp_path: Path) -> None:
    """Accepting is a decision, made once the drift has been read."""
    root = tmp_path / "handins"
    handin = _handin(_exposures_csv(tmp_path / "exposures.csv"))

    profile, report = check_drift(handin, store_dir=root)

    assert store.load_profile(root, handin.sha256) == profile
    assert store.last_accepted_profile(root) is None
    assert report.is_first_hand_in


def test_check_drift_compares_this_months_hand_in_against_the_accepted_one(
    tmp_path: Path,
) -> None:
    root = tmp_path / "handins"
    july = _handin(_exposures_csv(tmp_path / "july.csv"))
    august_path = _csv(
        tmp_path / "august.csv",
        header="counterparty,EAD_GBP",
        rows=[f"{n},{e}" for n, e in zip(COUNTERPARTIES, EXPOSURES, strict=True)],
    )

    july_profile, _first = check_drift(july, store_dir=root)
    store.accept_profile(root, july_profile)
    _august_profile, report = check_drift(_handin(august_path), store_dir=root)

    assert not report.is_first_hand_in
    assert report.baseline_sha256 == july.sha256
    assert [i.message for i in report.of_kind(DriftKind.COLUMN_RENAMED)] == [
        "column 'EAD' became 'EAD_GBP'."
    ]


def test_check_drift_logs_when_the_hand_in_carries_error_level_drift(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "handins"
    july = _handin(_exposures_csv(tmp_path / "july.csv"))
    august = _handin(
        _csv(
            tmp_path / "august.csv",
            header="counterparty",
            rows=list(COUNTERPARTIES),
        )
    )

    store.accept_profile(root, check_drift(july, store_dir=root)[0])
    with caplog.at_level(logging.WARNING, logger="kedge.ingest.drift"):
        _profile_out, report = check_drift(august, store_dir=root)

    assert report.errors
    assert "error-level drift item(s)" in caplog.text


def test_check_drift_on_an_unreadable_hand_in_raises_a_typed_error(tmp_path: Path) -> None:
    path = tmp_path / "exposures.xlsx"
    path.write_bytes(b"not a workbook")

    with pytest.raises(HandInReadError):
        check_drift(_handin(path), store_dir=tmp_path / "handins")
