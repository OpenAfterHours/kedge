"""Sketching a contract from a workbook sheet, before any hand-in exists.

Three promises, and each is asserted directly because each is a way this could quietly do
harm.

**It says what it is.** A contract drafted from a paste rather than from a real export is
weaker evidence, and the file has to say so -- otherwise the next reader tightens their
process around a guess they had no way to recognise.

**It is loose enough to survive an ordinary month.** Row bounds pinned to one observation,
a not-null drafted off one full column, a closed value set drawn from one paste: each is a
contract that fails a perfectly good export, and a contract that cries wolf gets deleted,
after which nothing is checked at all.

**A real export of that sheet passes it.** The property that matters, and the one that makes
the rest safe: the tests below build the export the user would produce and validate it.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import polars as pl
import pytest
import yaml

from conftest import make_analysis, make_profile
from kedge.analysis.analyse import analyse
from kedge.analysis.model import ColumnProfile, NumericStats, SheetInfo, SheetRole
from kedge.analysis.profile import FORMAT_ANOMALY_PREFIXES
from kedge.contracts.model import ChecksumMode, load_contract
from kedge.contracts.sketch import (
    MIN_ROWS_FOR_BOUNDS,
    ROW_BOUND_FACTOR,
    SheetSketch,
    sketch,
    write_sketch,
)
from kedge.contracts.validate import validate_path
from kedge.errors import ContractError

if TYPE_CHECKING:
    from kedge.analysis.model import WorkbookAnalysis

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
LEGACY_SQL = FIXTURES / "legacy_sql.xlsx"

EXTRACT_COLUMNS = [
    "trade_id",
    "counterparty_name",
    "notional",
    "currency",
    "trade_date",
    "rating_grade",
]


@pytest.fixture(scope="module")
def legacy_sql() -> WorkbookAnalysis:
    """The real fixture: an `Extract` sheet of pasted query output, and a `Summary` over it."""
    if not LEGACY_SQL.is_file():  # pragma: no cover - the fixture is committed
        pytest.skip("tests/fixtures/legacy_sql.xlsx has not landed yet")
    return analyse(LEGACY_SQL)


def _export(path: Path, *, rows: int = 120) -> Path:
    """The CSV a user would produce by running the query and saving the grid.

    Not the workbook's own cells: the point of the check is that a contract drafted off the
    paste survives the round trip through a real export and polars' own CSV inference, which
    is where a dtype guessed too tightly shows up.
    """
    frame = pl.DataFrame(
        {
            "trade_id": [f"TRD-{index + 1:05d}" for index in range(rows)],
            "counterparty_name": ["Fenchurch Partners", "Everton Securities"] * (rows // 2),
            "notional": [20_000.0 + index for index in range(rows)],
            "currency": ["GBP", "USD", "EUR"] * (rows // 3),
            "trade_date": ["2026-05-27"] * rows,
            "rating_grade": ["AAA", "BB"] * (rows // 2),
        }
    )
    frame.write_csv(path)
    return path


def _sheet_analysis(profiles: list[ColumnProfile], **sheet_fields: object) -> WorkbookAnalysis:
    """An analysis of one sheet, for the cases the committed fixtures do not cover."""
    fields: dict[str, object] = {
        "name": "Extract",
        "index": 0,
        "role": SheetRole.DATA,
        "header_row": 1,
        "max_row": 121,
    }
    fields.update(sheet_fields)
    return make_analysis(sheets=[SheetInfo(**fields)], profiles=profiles)


def _prose(text: str) -> str:
    """The comment lines as flowing prose.

    The emitter wraps to the file's width, so a sentence a reader sees whole is two lines in
    the text. Asserting against the wrap would make every test hostage to a word count.
    """
    return " ".join(line.lstrip("#").strip() for line in text.splitlines() if line.startswith("#"))


def _pasted(header: str, column: str, **overrides: object) -> ColumnProfile:
    fields: dict[str, object] = {
        "sheet": "Extract",
        "column": column,
        "index": ord(column) - ord("A") + 1,
        "header": header,
        "dtype": "string",
        "row_count": 120,
        "null_count": 0,
        "distinct_count": 12,
    }
    fields.update(overrides)
    return ColumnProfile(**fields)


# ── the draft ───────────────────────────────────────────────────────────────


def test_a_contract_is_drafted_from_the_pasted_sheets_own_columns(
    legacy_sql: WorkbookAnalysis,
) -> None:
    drafted = sketch(legacy_sql)

    assert drafted.sheet == "Extract"
    assert drafted.contract.column_names == EXTRACT_COLUMNS
    assert drafted.contract.name == "legacy_sql_extract"


def test_the_draft_says_it_came_from_a_workbook_rather_than_from_a_hand_in(
    legacy_sql: WorkbookAnalysis,
) -> None:
    """The reviewer has to be able to tell what evidence is behind it, from the file alone."""
    drafted = sketch(legacy_sql)

    assert "legacy_sql.xlsx" in (drafted.contract.generated_from or "")
    assert "Extract" in (drafted.contract.generated_from or "")
    assert "not a hand-in" in (drafted.contract.generated_from or "")
    assert "Sketched from the 'Extract' sheet" in (drafted.contract.description or "")

    prose = _prose(drafted.to_yaml())
    assert "SKETCHED by `kedge contract sketch`" in prose
    assert "NOT from a hand-in" in prose
    assert "kedge contract infer <export>" in prose


def test_the_draft_records_no_checksum_because_the_workbooks_is_not_the_hand_ins(
    legacy_sql: WorkbookAnalysis,
) -> None:
    contract = sketch(legacy_sql).contract

    assert contract.checksum is None
    assert contract.checksum_mode is ChecksumMode.IGNORE


def test_the_worksheet_field_is_left_null_because_an_export_is_not_the_workbook(
    legacy_sql: WorkbookAnalysis,
) -> None:
    """`sheet` names a worksheet inside the hand-in; 'Extract' would send the reader hunting."""
    drafted = sketch(legacy_sql)

    assert drafted.contract.sheet is None
    assert drafted.contract.header_row is None
    assert "null on purpose" in _prose(drafted.to_yaml())


def test_a_column_the_sending_team_adds_is_allowed_but_one_they_drop_is_not(
    legacy_sql: WorkbookAnalysis,
) -> None:
    contract = sketch(legacy_sql).contract

    assert contract.allow_extra_columns is True
    assert contract.require_column_order is False
    assert all(column.required for column in contract.columns)


# ── how confident it is ─────────────────────────────────────────────────────


def test_row_bounds_are_not_pinned_to_the_one_month_that_was_pasted(
    legacy_sql: WorkbookAnalysis,
) -> None:
    """A quiet month is a normal event, and a contract that fails one gets deleted."""
    contract = sketch(legacy_sql).contract

    assert contract.min_rows == 120 // ROW_BOUND_FACTOR
    assert contract.max_rows == 120 * ROW_BOUND_FACTOR
    assert contract.min_rows is not None and contract.min_rows < 120 // 2
    assert contract.max_rows is not None and contract.max_rows > 120 * 2


def test_too_few_pasted_rows_bounds_nothing_at_all() -> None:
    rows = MIN_ROWS_FOR_BOUNDS - 1
    analysis = _sheet_analysis([_pasted("code", "A", row_count=rows, distinct_count=rows)])

    drafted = sketch(analysis)

    assert drafted.contract.min_rows is None
    assert drafted.contract.max_rows is None
    assert "No bounds drafted" in _prose(drafted.to_yaml())


def test_a_sheet_the_profiler_did_not_finish_reading_gets_no_ceiling() -> None:
    """The paste was at least that tall and might have been far taller."""
    truncated = _pasted(
        "code",
        "A",
        format_anomalies=["profile truncated: first 50,000 of 900,000 data rows were read"],
    )

    drafted = sketch(_sheet_analysis([truncated]))

    assert drafted.contract.min_rows == 120 // ROW_BOUND_FACTOR
    assert drafted.contract.max_rows is None
    assert "no ceiling" in _prose(drafted.to_yaml())


def test_the_truncation_and_text_number_anomalies_are_still_spelt_that_way() -> None:
    """Both are matched as prefixes of an analyser string, so a rename must break here."""
    assert "profile truncated" in FORMAT_ANOMALY_PREFIXES
    assert "numbers stored as text" in FORMAT_ANOMALY_PREFIXES


def test_a_column_that_was_full_last_month_is_still_left_nullable(
    legacy_sql: WorkbookAnalysis,
) -> None:
    """One month of no blanks is thin evidence that a blank is a fault -- so it is a comment."""
    drafted = sketch(legacy_sql)

    assert drafted.contract.column("counterparty_name").nullable is True  # type: ignore[union-attr]
    assert any("left nullable" in note for note in drafted.notes["counterparty_name"])


def test_no_permitted_value_set_is_drafted_from_one_paste(
    legacy_sql: WorkbookAnalysis,
) -> None:
    """Three currencies last month is not a closed set; it is offered as a comment instead."""
    drafted = sketch(legacy_sql)

    assert drafted.contract.column("currency").allowed_values is None  # type: ignore[union-attr]
    assert any("only 3 distinct value(s)" in note for note in drafted.notes["currency"])


def test_no_numeric_bounds_are_drafted_from_one_months_range(
    legacy_sql: WorkbookAnalysis,
) -> None:
    notional = sketch(legacy_sql).contract.column("notional")

    assert notional is not None
    assert notional.min_value is None
    assert notional.max_value is None


# ── the key, which is the field with teeth ──────────────────────────────────


def test_a_column_distinct_and_never_blank_is_proposed_as_the_key(
    legacy_sql: WorkbookAnalysis,
) -> None:
    drafted = sketch(legacy_sql)

    assert drafted.contract.unique_key == ["trade_id"]
    assert drafted.contract.column("trade_id").unique is True  # type: ignore[union-attr]
    assert drafted.contract.column("trade_id").nullable is False  # type: ignore[union-attr]


def test_the_file_says_what_evidence_the_key_was_proposed_on(
    legacy_sql: WorkbookAnalysis,
) -> None:
    prose = _prose(sketch(legacy_sql).to_yaml())

    assert "PROPOSED on evidence" in prose
    assert "every pasted value of 'trade_id' was different and none was blank" in prose
    assert "PLAN 2.6" in prose


def test_a_distinct_measure_is_not_mistaken_for_a_key(legacy_sql: WorkbookAnalysis) -> None:
    """Every notional in the paste differs, but two trades of the same size are not a fault."""
    notional = sketch(legacy_sql).contract.column("notional")

    assert notional is not None
    assert notional.unique is False


def test_only_one_column_carries_a_uniqueness_assertion() -> None:
    analysis = _sheet_analysis(
        [
            _pasted("trade_id", "A", distinct_count=120),
            _pasted("reference", "B", distinct_count=120),
        ]
    )

    drafted = sketch(analysis)

    assert drafted.contract.unique_key == ["trade_id"]
    assert [column.name for column in drafted.contract.columns if column.unique] == ["trade_id"]


def test_a_column_with_blanks_is_never_the_key() -> None:
    analysis = _sheet_analysis([_pasted("trade_id", "A", null_count=1, distinct_count=119)])

    drafted = sketch(analysis)

    assert drafted.contract.unique_key == []
    assert "composite key" in _prose(drafted.to_yaml())


def test_a_column_whose_distinct_count_overflowed_is_not_evidence_of_anything() -> None:
    """`None` distinct is "the profiler stopped counting", which is not "all different"."""
    analysis = _sheet_analysis([_pasted("trade_id", "A", distinct_count=None)])

    assert sketch(analysis).contract.unique_key == []


# ── dtypes ──────────────────────────────────────────────────────────────────


def test_text_stays_text_and_a_measure_becomes_a_float(legacy_sql: WorkbookAnalysis) -> None:
    contract = sketch(legacy_sql).contract

    assert contract.column("trade_id").dtype == "String"  # type: ignore[union-attr]
    assert contract.column("notional").dtype == "Float64"  # type: ignore[union-attr]


def test_a_whole_numbered_column_is_widened_to_float_so_decimals_still_pass() -> None:
    """Int64 satisfies Float64 but not the reverse, and next month may arrive with decimals."""
    analysis = _sheet_analysis(
        [
            _pasted(
                "trade_count",
                "A",
                dtype="integer",
                numeric=NumericStats(min=1.0, max=9.0, mean=4.0, sum=480.0),
            )
        ]
    )

    drafted = sketch(analysis)

    assert drafted.contract.column("trade_count").dtype == "Float64"  # type: ignore[union-attr]
    assert any("Set Int64 if it must be whole" in n for n in drafted.notes["trade_count"])


def test_a_date_is_left_unasserted_because_an_export_writes_it_as_text(
    legacy_sql: WorkbookAnalysis,
) -> None:
    """A CSV export of a real date reads back as String, so `Date` would fail a good export."""
    drafted = sketch(legacy_sql)

    assert drafted.contract.column("trade_date").dtype == "Any"  # type: ignore[union-attr]
    assert any("contracted as Any" in note for note in drafted.notes["trade_date"])


def test_a_column_the_analyser_could_not_classify_is_omitted_rather_than_guessed_at() -> None:
    analysis = _sheet_analysis(
        [
            _pasted("currency", "A"),
            _pasted("total", "B", dtype="empty", null_count=120, distinct_count=0),
            _pasted("mess", "C", dtype="mixed"),
        ]
    )

    drafted = sketch(analysis)

    assert drafted.contract.column_names == ["currency"]
    assert any("'total'" in entry and "blank" in entry for entry in drafted.omitted)
    assert any("'mess'" in entry and "more than one kind" in entry for entry in drafted.omitted)


def test_every_omitted_column_is_named_in_the_file_with_its_reason() -> None:
    analysis = _sheet_analysis(
        [_pasted("currency", "A"), _pasted("total", "B", dtype="empty", null_count=120)]
    )

    prose = _prose(sketch(analysis).to_yaml())

    assert "NOT in this contract" in prose
    assert "- column B ('total')" in prose


def test_a_column_with_no_header_is_omitted_rather_than_named_after_its_letter() -> None:
    analysis = _sheet_analysis([_pasted("currency", "A"), _pasted(None, "B")])  # type: ignore[arg-type]

    drafted = sketch(analysis)

    assert drafted.contract.column_names == ["currency"]
    assert any("header cell is blank" in entry for entry in drafted.omitted)


def test_a_repeated_header_is_contracted_once_and_the_second_is_reported() -> None:
    analysis = _sheet_analysis([_pasted("currency", "A"), _pasted("currency", "B")])

    drafted = sketch(analysis)

    assert drafted.contract.column_names == ["currency"]
    assert any("already has that header" in entry for entry in drafted.omitted)


def test_a_header_holding_a_line_break_is_collapsed_onto_one_line() -> None:
    """Alt+Enter in a header cell is ordinary; a newline inside a YAML scalar is not."""
    analysis = _sheet_analysis([_pasted("trade\ndate", "A")])

    drafted = sketch(analysis)

    assert drafted.contract.column_names == ["trade date"]
    assert load_contract_text(drafted)["columns"][0]["name"] == "trade date"


def load_contract_text(drafted: SheetSketch) -> dict[str, object]:
    return yaml.safe_load(drafted.to_yaml())


def test_numbers_stored_as_text_are_carried_into_the_file_as_a_warning() -> None:
    analysis = _sheet_analysis(
        [
            _pasted(
                "account",
                "A",
                format_anomalies=["numbers stored as text (118 cells, 98% of the column)"],
            )
        ]
    )

    drafted = sketch(analysis)

    assert any("WARNING" in note for note in drafted.notes["account"])


# ── choosing the sheet ──────────────────────────────────────────────────────


def test_the_only_data_sheet_is_chosen_and_the_reason_is_stated(
    legacy_sql: WorkbookAnalysis,
) -> None:
    drafted = sketch(legacy_sql)

    assert "only sheet" in drafted.reason
    assert "classified as data" in drafted.reason
    assert f"Why that sheet: {drafted.reason}" in _prose(drafted.to_yaml())


def test_several_data_sheets_is_a_question_rather_than_a_guess() -> None:
    """One of them may be a reference table somebody maintains by hand."""
    analysis = make_analysis(
        sheets=[
            SheetInfo(name="Extract", index=0, role=SheetRole.DATA, header_row=1),
            SheetInfo(name="Rates", index=1, role=SheetRole.DATA, header_row=1),
        ],
        profiles=[_pasted("currency", "A"), make_profile(sheet="Rates", header="rate")],
    )

    with pytest.raises(ContractError, match="Name the one this process is handed"):
        sketch(analysis)


def test_a_workbook_with_no_pasted_sheet_says_so_and_lists_what_it_did_find() -> None:
    analysis = make_analysis(
        sheets=[SheetInfo(name="Calc", index=0, role=SheetRole.CALCULATION)],
        profiles=[],
    )

    with pytest.raises(ContractError, match=r"no sheet of .* looks like a block of pasted data"):
        sketch(analysis)


def test_a_named_sheet_is_taken_even_when_it_is_not_classified_as_data(
    legacy_sql: WorkbookAnalysis,
) -> None:
    """The user is looking at the workbook and kedge is not -- but it says what it thinks."""
    drafted = sketch(legacy_sql, sheet="Summary")

    assert drafted.sheet == "Summary"
    assert "classified it as calculation" in drafted.reason


def test_a_sheet_that_is_not_in_the_workbook_is_reported_with_the_ones_that_are(
    legacy_sql: WorkbookAnalysis,
) -> None:
    with pytest.raises(ContractError, match="has no sheet called 'Trades'"):
        sketch(legacy_sql, sheet="Trades")


def test_a_sheet_with_no_header_row_has_no_column_names_to_contract_on() -> None:
    analysis = _sheet_analysis(
        [_pasted(None, "A"), _pasted(None, "B")],  # type: ignore[arg-type]
        header_row=None,
    )

    with pytest.raises(ContractError, match="no header row"):
        sketch(analysis)


def test_a_sheet_with_no_profiles_is_a_message_rather_than_a_traceback() -> None:
    analysis = make_analysis(
        sheets=[SheetInfo(name="Extract", index=0, role=SheetRole.DATA, header_row=1)],
        profiles=[],
    )

    with pytest.raises(ContractError, match="no column profiles"):
        sketch(analysis, sheet="Extract")


def test_a_sheet_whose_every_column_is_unclassifiable_drafts_nothing() -> None:
    analysis = _sheet_analysis(
        [_pasted("total", "A", dtype="empty", null_count=120, distinct_count=0)]
    )

    with pytest.raises(ContractError, match="no column of sheet 'Extract' could be described"):
        sketch(analysis)


# ── writing it out ──────────────────────────────────────────────────────────


def test_an_existing_contract_is_never_silently_replaced(
    legacy_sql: WorkbookAnalysis, tmp_path: Path
) -> None:
    """It may have been hand-tuned by somebody who knows what the process receives."""
    existing = tmp_path / "contract.yaml"
    existing.write_text("name: tightened-by-hand\n", encoding="utf-8")

    with pytest.raises(ContractError, match="already a contract at"):
        write_sketch(sketch(legacy_sql), existing)

    assert existing.read_text(encoding="utf-8") == "name: tightened-by-hand\n"


def test_replacing_one_takes_saying_so(legacy_sql: WorkbookAnalysis, tmp_path: Path) -> None:
    existing = tmp_path / "contract.yaml"
    existing.write_text("name: tightened-by-hand\n", encoding="utf-8")

    written = write_sketch(sketch(legacy_sql), existing, overwrite=True)

    assert load_contract(written).name == "legacy_sql_extract"


def test_writing_a_sketch_creates_the_directory_it_needs(
    legacy_sql: WorkbookAnalysis, tmp_path: Path
) -> None:
    path = write_sketch(sketch(legacy_sql), tmp_path / "a" / "b" / "contract.yaml")

    assert path.is_file()


def test_a_sketch_that_cannot_be_written_is_a_contract_error(
    legacy_sql: WorkbookAnalysis, tmp_path: Path
) -> None:
    blocked = tmp_path / "contract.yaml"
    blocked.mkdir()

    with pytest.raises(ContractError, match="could not write the contract sketch"):
        write_sketch(sketch(legacy_sql), blocked, overwrite=True)


def test_the_commented_sketch_reloads_to_an_equal_contract(
    legacy_sql: WorkbookAnalysis, tmp_path: Path
) -> None:
    """The comments are the point, but a file that will not load back is worse than none."""
    drafted = sketch(legacy_sql)
    path = write_sketch(drafted, tmp_path / "contract.yaml")

    assert load_contract(path) == drafted.contract


def test_the_emitted_yaml_is_parseable_by_a_plain_yaml_loader(
    legacy_sql: WorkbookAnalysis,
) -> None:
    raw = yaml.safe_load(sketch(legacy_sql).to_yaml())

    assert raw["name"] == "legacy_sql_extract"
    assert raw["checksum_mode"] == "ignore"
    assert [column["name"] for column in raw["columns"]] == EXTRACT_COLUMNS


# ── the property that matters ───────────────────────────────────────────────


def test_a_real_export_of_that_sheet_validates_against_the_sketch(
    legacy_sql: WorkbookAnalysis, tmp_path: Path
) -> None:
    """The whole point: run the query, save the CSV, and the contract goes green."""
    drafted = sketch(legacy_sql)
    export = _export(tmp_path / "trades.csv")

    report = validate_path(export, drafted.contract)

    assert report.ok, report.render_text()


def test_a_quiet_month_still_validates_against_the_sketch(
    legacy_sql: WorkbookAnalysis, tmp_path: Path
) -> None:
    """Half the rows of the month it was drafted from is a business event, not a fault."""
    drafted = sketch(legacy_sql)
    export = _export(tmp_path / "trades.csv", rows=60)

    assert validate_path(export, drafted.contract).ok


def test_the_sketch_reloaded_from_its_file_still_validates_that_export(
    legacy_sql: WorkbookAnalysis, tmp_path: Path
) -> None:
    path = write_sketch(sketch(legacy_sql), tmp_path / "contract.yaml")
    export = _export(tmp_path / "trades.csv")

    assert validate_path(export, load_contract(path)).ok


def test_an_export_missing_a_column_is_what_the_sketch_is_for(
    legacy_sql: WorkbookAnalysis, tmp_path: Path
) -> None:
    """The bootstrapping problem stated as a test: the user is told before they run anything."""
    drafted = sketch(legacy_sql)
    export = tmp_path / "trades.csv"
    pl.read_csv(_export(tmp_path / "full.csv")).drop("currency").write_csv(export)

    report = validate_path(export, drafted.contract)

    assert not report.ok
    assert any("currency" in check.message for check in report.failures)
