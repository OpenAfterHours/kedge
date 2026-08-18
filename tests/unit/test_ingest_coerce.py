"""A grid off a clipboard is all text, and the reader has to say what it did about that.

This is the bug that got through the whole eval: a workbook column formatted `#,##0.00` copies
to the clipboard as `364,422.95`, polars types it `String`, and the failure surfaces four
operations later as `arithmetic on dtypes str and dyn float is not allowed` -- from inside a
forty-line query plan, in a marimo app-mode cell that renders nothing at all. Every assertion
here is about making that impossible or making it visible.

The rules are asymmetric on purpose. `kedge.xl.to_number` is liberal because a translation that
calls it has said what it wants; deciding to call it *unasked* is a different question, and the
tests below are mostly about the cases where the liberal answer would destroy something.
"""

from __future__ import annotations

import polars as pl
import pytest

from kedge.ingest.coerce import MAX_SIGNIFICANT_DIGITS, coerce_numeric_text


def frame(**columns: list) -> pl.DataFrame:
    return pl.DataFrame(columns)


# ── what gets coerced ────────────────────────────────────────────────────────


def test_an_excel_formatted_money_column_becomes_numeric() -> None:
    """The exact shape that broke the reference notebook: thousands separators from a paste."""
    typed, coercions = coerce_numeric_text(
        frame(accrual_gbp=["364,422.95", "205,878.90", "277,203.34"])
    )

    assert typed["accrual_gbp"].to_list() == [364422.95, 205878.90, 277203.34]
    assert [item.column for item in coercions] == ["accrual_gbp"]


def test_an_empty_cell_becomes_null_not_zero() -> None:
    """Typing is not the place to decide what a blank means -- that is `empty_as_zero`'s job."""
    typed, _ = coerce_numeric_text(frame(amount=["1.50", "", "2.25"]))

    assert typed["amount"].to_list() == [1.50, None, 2.25]


def test_accounting_negatives_and_currency_symbols_are_handled() -> None:
    typed, _ = coerce_numeric_text(frame(amount=["(1,234.50)", "£900", "0.25"]))

    assert typed["amount"].to_list() == [-1234.5, 900.0, 0.25]


def test_a_column_already_numeric_is_left_alone() -> None:
    typed, coercions = coerce_numeric_text(frame(amount=[1.0, 2.0]))

    assert not coercions
    assert typed["amount"].dtype == pl.Float64


# ── what does not, and why ───────────────────────────────────────────────────


def test_a_leading_zero_means_an_identifier_and_is_left_as_text() -> None:
    """`00123` is a cost centre. Coercing it to 123.0 breaks every join it takes part in."""
    typed, coercions = coerce_numeric_text(frame(cost_centre=["00123", "00456", "00789"]))

    assert typed["cost_centre"].dtype == pl.String
    assert not coercions


def test_a_plain_zero_and_a_decimal_below_one_are_still_numbers() -> None:
    """The leading-zero rule must not catch `0`, `0.5` or `-0.25`."""
    typed, coercions = coerce_numeric_text(frame(rate=["0", "0.5", "-0.25"]))

    assert typed["rate"].to_list() == [0.0, 0.5, -0.25]
    assert coercions


def test_more_than_fifteen_significant_digits_is_left_as_text() -> None:
    """A 16-digit account number does not survive Float64, which is why it is text at source."""
    long_number = "1" * (MAX_SIGNIFICANT_DIGITS + 1)
    typed, coercions = coerce_numeric_text(frame(account=[long_number, "2" * 16]))

    assert typed["account"].dtype == pl.String
    assert not coercions


def test_one_unparseable_value_leaves_the_whole_column_alone() -> None:
    """An `n/a` in a column of numbers is somebody's note, and nulling it would hide it."""
    typed, coercions = coerce_numeric_text(frame(amount=["1.50", "n/a", "2.25"]))

    assert typed["amount"].dtype == pl.String
    assert not coercions


def test_an_entirely_empty_column_is_not_coerced() -> None:
    """Nothing to conclude from, so nothing is concluded."""
    _typed, coercions = coerce_numeric_text(frame(spare=["", "", ""]))

    assert not coercions


def test_a_text_column_is_left_alone() -> None:
    _typed, coercions = coerce_numeric_text(frame(entity=["Northgate", "Brightwater"]))

    assert not coercions


def test_a_named_column_can_be_kept_as_text_whatever_it_looks_like() -> None:
    """A contract that declares a column `string` outranks the guess."""
    typed, coercions = coerce_numeric_text(frame(ref=["1001", "1002"]), skip=["ref"])

    assert typed["ref"].dtype == pl.String
    assert not coercions


# ── the report ───────────────────────────────────────────────────────────────


def test_the_report_names_the_column_the_count_and_a_sample() -> None:
    """ "1 column coerced" is not actionable; naming the values lets a user recognise it."""
    _typed, coercions = coerce_numeric_text(frame(accrual_gbp=["364,422.95", "", "205,878.90"]))

    note = coercions[0].note()
    assert "accrual_gbp" in note
    assert "364,422.95" in note
    assert "2 values" in note


def test_several_columns_are_each_reported(tmp_path: object) -> None:
    _typed, coercions = coerce_numeric_text(frame(a=["1,000"], b=["2,000"], name=["Northgate"]))

    assert [item.column for item in coercions] == ["a", "b"]


# ── through the reader, which is what the notebook uses ──────────────────────


def test_read_data_types_a_pasted_grid_and_puts_it_on_the_layout(tmp_path) -> None:
    """The path the runbook actually takes: paste, receive, read, compute."""
    from kedge.ingest import read_data

    path = tmp_path / "extract.csv"
    path.write_text(
        'trade_id,accrual_gbp\nACC-1,"364,422.95"\nACC-2,"205,878.90"\n', encoding="utf-8"
    )

    data, layout = read_data(path)

    assert data["accrual_gbp"].to_list() == [364422.95, 205878.90]
    assert any("read `accrual_gbp` as text" in note for note in layout.notes())


def test_read_data_can_be_told_not_to_coerce(tmp_path) -> None:
    from kedge.ingest import read_data

    path = tmp_path / "extract.csv"
    path.write_text('trade_id,amount\nA,"1,000"\n', encoding="utf-8")

    data, layout = read_data(path, coerce=False)

    assert data["amount"].dtype == pl.String
    assert layout.coercions == ()


def test_the_arithmetic_that_used_to_fail_now_works(tmp_path) -> None:
    """The regression, stated as the thing the user was trying to do.

    Before the reader typed the column this raised `InvalidOperationError: arithmetic on dtypes
    str and dyn float is not allowed`, and in marimo's app mode the cell rendered nothing --
    so the user saw a runbook that stopped, with no reason given.
    """
    import kedge.xl  # noqa: F401
    from kedge.ingest import read_data

    path = tmp_path / "extract.csv"
    path.write_text(
        'trade_id,accrual_gbp\nACC-1,"364,422.95"\nACC-2,\nACC-3,"1,200.00"\n', encoding="utf-8"
    )

    data, _layout = read_data(path)
    uplifted = data.with_columns(
        (pl.col("accrual_gbp").xl.empty_as_zero() * 1.045).xl.round(2).alias("after")
    )

    assert uplifted["after"].to_list() == [380821.98, 0.0, 1254.0]


@pytest.mark.parametrize("delimiter", ["\t", ",", ";"])
def test_a_paste_in_any_dialect_reaches_the_same_typed_frame(tmp_path, delimiter: str) -> None:
    """Excel pastes tab-delimited; an export is comma; a European locale is semicolon."""
    from kedge.ingest import Paste, read_data, receive

    text = f"trade_id{delimiter}amount\nACC-1{delimiter}1000.50\n"
    handin = receive(Paste(text=text), store_dir=tmp_path / "store")
    data, _layout = read_data(handin.path)

    assert data["amount"].to_list() == [1000.50]
