"""Each diagnostic rule, against mismatches that genuinely exhibit its signature.

Nothing here hand-builds a `Mismatch`. Every case computes the two sides the way the defect
being diagnosed would actually compute them -- polars' half-to-even round against Excel's
half-away-from-zero, a naive floor(x * 100 + 0.5) against Excel's 15-significant-digit
collapse, a null-propagating addition against `sum_horizontal` -- and then runs them through
the real comparison engine. A rule that fires on a hand-typed pair but not on the defect is
worth nothing.
"""

from __future__ import annotations

import math
from datetime import date

import polars as pl
import pytest

from kedge.reconcile.compare import compare_vectors
from kedge.reconcile.diagnose import decimals_retained, diagnose
from kedge.reconcile.model import DiagnosticCause, Tolerance
from kedge.xl import round_half_away

TOLERANCE = Tolerance(absolute=1e-6, relative=1e-9)


def _diagnose(expected: list, actual: list, *, column: str = "amount") -> list:
    """Run the real engine, then the real diagnostics."""
    comparison = compare_vectors(expected, actual, tolerance=TOLERANCE)
    return diagnose(
        comparison.mismatches,
        rows_compared=comparison.rows_compared,
        rows_expected=comparison.rows_expected,
        rows_actual=comparison.rows_actual,
        column=column,
    )


def _excel_round(values: list[float], digits: int = 2) -> list[float]:
    """What Excel's ROUND produces, via kedge.xl."""
    frame = pl.DataFrame({"x": values})
    return frame.select(round_half_away(pl.col("x"), digits)).to_series().to_list()


def _polars_round(values: list[float], digits: int = 2) -> list[float]:
    """What a translation that reached for `.round()` produces: half to even."""
    frame = pl.DataFrame({"x": values})
    return frame.select(pl.col("x").round(digits)).to_series().to_list()


def _naive_half_away(values: list[float], digits: int = 2) -> list[float]:
    """Half away from zero, hand-rolled: the right mode without the 15-digit collapse."""
    factor = 10.0**digits
    return [math.floor(abs(v) * factor + 0.5) / factor * (1.0 if v >= 0 else -1.0) for v in values]


# ── rounding: mode, collapse, and never applied ─────────────────────────────

#: Operands that are exact ties at two decimal places *and* on which half-to-even and
#: half-away-from-zero disagree. They are all dyadic rationals, so the tie is real in binary
#: and not an artefact of decimal display.
TIE_OPERANDS = [2.625, 0.125, 5.625, 8.125, -2.625]

#: Operands sitting a hair below a two-decimal boundary. Excel collapses each to 15
#: significant decimal digits, which lands it *on* the boundary, and rounds away from zero.
NEAR_BOUNDARY_OPERANDS = [4491760.575 - 1e-9, 12345.675 - 1e-11, 1234.565 - 1e-12]


def test_half_to_even_rounding_is_diagnosed_as_the_wrong_rounding_mode() -> None:
    """polars' default round against Excel's: every row is an exact tie."""
    expected = _excel_round(TIE_OPERANDS)
    actual = _polars_round(TIE_OPERANDS)
    assert expected != actual, "the fixture no longer exercises the mode difference"

    diagnoses = _diagnose(expected, actual, column="rwa")

    assert diagnoses, "a whole column out by one unit in the last place is diagnosable"
    top = diagnoses[0]
    assert top.cause is DiagnosticCause.ROUNDING_MODE
    assert top.confidence >= 0.85
    assert top.remedy_symbol == "kedge.xl.round_half_away"
    assert "half away from zero" in top.headline
    assert "rwa" in top.remedy
    assert any("odd" in item and "even" in item for item in top.evidence)


def test_a_missing_fifteen_digit_collapse_is_told_apart_from_the_wrong_mode() -> None:
    """The remedies differ, so the diagnosis must too.

    These rows are not ties. Excel's answer has an even last digit in at least one of them,
    which half-to-even can never produce -- it only ever moves *to* the even neighbour. So
    the mode is already right and what is missing is the 15-significant-digit collapse.
    """
    expected = _excel_round(NEAR_BOUNDARY_OPERANDS)
    actual = _naive_half_away(NEAR_BOUNDARY_OPERANDS)
    assert expected != actual, "the fixture no longer exercises the collapse"

    diagnoses = _diagnose(expected, actual, column="capital_charge")

    top = diagnoses[0]
    assert top.cause is DiagnosticCause.ROUNDING_15_DIGIT_COLLAPSE
    assert top.cause is not DiagnosticCause.ROUNDING_MODE
    assert "15" in top.headline
    assert top.remedy_symbol == "kedge.xl.round_half_away"
    assert any("cannot be a half-to-even tie" in item for item in top.evidence)


def test_the_one_penny_case_that_actually_happened_is_diagnosed_as_the_collapse() -> None:
    """4491760.574999999 is 4491760.575 to Excel, so ROUND gives .58 and naive code gives .57."""
    expected = _excel_round([4491760.575 - 1e-9])
    actual = _naive_half_away([4491760.575 - 1e-9])
    assert expected == [4491760.58]
    assert actual == [4491760.57]

    diagnoses = _diagnose(expected, actual)

    assert diagnoses[0].cause is DiagnosticCause.ROUNDING_15_DIGIT_COLLAPSE
    assert diagnoses[0].affected_rows == 1


def test_forgetting_to_round_at_all_is_diagnosed_as_such() -> None:
    """Excel keeps two decimals, the notebook keeps all of them, every delta is under half a unit."""
    operands = [1234.5678, 99.14159, 7.005001, 45678.98765]
    expected = _excel_round(operands)

    diagnoses = _diagnose(expected, operands, column="net")

    top = diagnoses[0]
    assert top.cause is DiagnosticCause.ROUNDING_NOT_APPLIED
    assert "ROUND" in top.headline
    assert 'round_half_away(pl.col("net"), 2)' in top.remedy
    assert top.affected_rows == len(operands)


def test_a_rounding_diagnosis_names_the_tolerance_free_facts_it_used() -> None:
    """Evidence has to be checkable: the unit, the row counts, and a row the reader can look at."""
    expected = _excel_round(TIE_OPERANDS)
    actual = _polars_round(TIE_OPERANDS)

    top = _diagnose(expected, actual)[0]

    assert any("0.01" in item for item in top.evidence)
    assert any(str(len(TIE_OPERANDS)) in item for item in top.evidence)


def test_differences_larger_than_one_unit_in_the_last_place_are_not_called_rounding() -> None:
    """A rule that fires on everything diagnoses nothing."""
    diagnoses = _diagnose([10.0, 20.0, 30.0], [10.5, 21.0, 27.0])

    assert not any(
        d.cause
        in (
            DiagnosticCause.ROUNDING_MODE,
            DiagnosticCause.ROUNDING_NOT_APPLIED,
            DiagnosticCause.ROUNDING_15_DIGIT_COLLAPSE,
        )
        for d in diagnoses
    )


# ── null versus empty ───────────────────────────────────────────────────────


def test_scattered_nulls_are_diagnosed_as_excels_empty_cell_being_zero() -> None:
    """`=A1+B1` ignores an empty B1; `col('a') + col('b')` propagates the null."""
    frame = pl.DataFrame(
        {
            "a": [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0],
            "b": [1.0, None, 3.0, None, 5.0, 6.0, 7.0, 8.0],
        }
    )
    excel = frame.select(pl.sum_horizontal("a", "b")).to_series().to_list()
    polars_way = frame.select(pl.col("a") + pl.col("b")).to_series().to_list()
    assert polars_way.count(None) == 2

    diagnoses = _diagnose(excel, polars_way, column="total")

    top = diagnoses[0]
    assert top.cause is DiagnosticCause.NULL_VS_EMPTY
    assert top.affected_rows == 2
    assert "sum_horizontal" in top.remedy
    assert top.remedy_symbol == "kedge.xl.sum_horizontal"


def test_a_column_that_is_entirely_null_is_diagnosed_as_a_join_that_matched_nothing() -> None:
    """Text-formatted keys against numeric ones produce nulls in every row, not some."""
    keys = pl.DataFrame({"key": ["1001", "1002", "1003", "1004", "1005"]})
    lookup = pl.DataFrame(
        {"key": [1001, 1002, 1003, 1004, 1005], "rate": [0.1, 0.2, 0.3, 0.4, 0.5]}
    )
    joined = keys.join(lookup.with_columns(pl.col("key").cast(pl.Utf8) + "x"), on="key", how="left")
    actual = joined.get_column("rate").to_list()
    assert actual == [None] * 5

    diagnoses = _diagnose([0.1, 0.2, 0.3, 0.4, 0.5], actual, column="rate")

    assert diagnoses[0].cause is DiagnosticCause.TEXT_FORMATTED_NUMBER
    assert diagnoses[0].remedy_symbol == "kedge.xl.to_number"
    assert any(d.cause is DiagnosticCause.NULL_VS_EMPTY for d in diagnoses), (
        "the empty-cell reading stays on the list, ranked below"
    )
    assert diagnoses[0].confidence > next(
        d.confidence for d in diagnoses if d.cause is DiagnosticCause.NULL_VS_EMPTY
    )


# ── divide by zero ──────────────────────────────────────────────────────────


def test_infinity_where_excel_raised_an_error_is_diagnosed_as_divide_by_zero() -> None:
    frame = pl.DataFrame({"n": [10.0, 20.0, 30.0], "d": [2.0, 0.0, 5.0]})
    actual = frame.select(pl.col("n") / pl.col("d")).to_series().to_list()
    assert math.isinf(actual[1])

    diagnoses = _diagnose([5.0, "#DIV/0!", 6.0], actual, column="ratio")

    top = diagnoses[0]
    assert top.cause is DiagnosticCause.DIVIDE_BY_ZERO
    assert top.remedy_symbol == "kedge.xl.div"
    assert "#DIV/0!" in " ".join(top.evidence)


def test_an_excel_error_the_notebook_did_not_reproduce_is_still_reported() -> None:
    """A workbook error cell is never quietly accepted, even when Python produced a number."""
    diagnoses = _diagnose(["#DIV/0!", 2.0, 3.0], [0.0, 2.0, 3.0])

    assert diagnoses[0].cause is DiagnosticCause.DIVIDE_BY_ZERO


# ── join keys ───────────────────────────────────────────────────────────────


def test_more_rows_than_the_workbook_range_is_diagnosed_as_a_duplicated_join_key() -> None:
    """VLOOKUP takes the first hit; a left join emits one row per match."""
    trades = pl.DataFrame({"key": ["a", "b", "c"], "notional": [100.0, 200.0, 300.0]})
    lookup = pl.DataFrame({"key": ["a", "b", "b", "c"], "rate": [1.0, 2.0, 2.5, 3.0]})
    joined = trades.join(lookup, on="key", how="left")
    assert joined.height == 4

    diagnoses = _diagnose([100.0, 200.0, 300.0], joined.get_column("notional").to_list())

    top = next(d for d in diagnoses if d.cause is DiagnosticCause.JOIN_KEY_NOT_UNIQUE)
    assert top.affected_rows == 1
    assert "is_duplicated" in top.remedy
    assert "4" in top.evidence[0] and "3" in top.evidence[0]


def test_a_row_count_that_matches_produces_no_join_diagnosis() -> None:
    diagnoses = _diagnose([1.0, 2.0], [1.0, 2.0])

    assert diagnoses == []


# ── dates ───────────────────────────────────────────────────────────────────


def test_every_date_out_by_one_day_is_diagnosed_as_the_1900_leap_year_bug() -> None:
    expected = [date(1900, 3, 1), date(1900, 6, 15), date(1901, 1, 20)]
    actual = [date(1900, 2, 28), date(1900, 6, 14), date(1901, 1, 19)]

    diagnoses = _diagnose(expected, actual, column="trade_date")

    top = diagnoses[0]
    assert top.cause is DiagnosticCause.DATE_SERIAL_OFFSET
    assert "leap" in top.headline
    assert top.remedy_symbol == "kedge.xl.serial_to_date"


def test_a_serial_number_against_a_date_is_diagnosed_as_an_unconverted_serial() -> None:
    diagnoses = _diagnose([46203.0, 46204.0], [date(2026, 7, 25), date(2026, 7, 26)])

    top = diagnoses[0]
    assert top.cause is DiagnosticCause.DATE_SERIAL_OFFSET
    assert "never converted" in top.headline


def test_dates_out_by_varying_amounts_are_not_called_a_serial_offset() -> None:
    diagnoses = _diagnose(
        [date(2026, 1, 1), date(2026, 2, 1)], [date(2026, 1, 2), date(2026, 2, 9)]
    )

    assert not any(d.cause is DiagnosticCause.DATE_SERIAL_OFFSET for d in diagnoses)


# ── text-formatted numbers ──────────────────────────────────────────────────


def test_a_string_against_the_same_number_is_no_longer_a_mismatch_at_all() -> None:
    """This used to be the rule's headline case, and the expectation was wrong.

    Excel coerces a text-formatted number at the point of use, so ``'1234.5'`` and ``1234.5``
    are the same value. The engine now reads them as one, which leaves nothing to diagnose --
    and leaving it as a mismatch made a workbook whose ``VLOOKUP`` cached a pasted text cell
    unreconcilable however right the conversion was.
    """
    assert _diagnose(["1234.5", "99.0"], [1234.5, 99.0], column="notional") == []


def test_digits_that_are_an_identifier_against_the_number_are_diagnosed_the_other_way_round() -> (
    None
):
    """What is left for the rule to fire on, and the advice is the opposite one.

    ``00417`` is a client code. ``to_number`` would turn it into 417 and break every join it
    takes part in, so the remedy names the text side as the correct one -- and no
    ``kedge.xl`` symbol, because there is no Excel-semantics function that fixes this.
    """
    diagnoses = _diagnose(["00417", "00099"], [417.0, 99.0], column="client_code")

    top = diagnoses[0]
    assert top.cause is DiagnosticCause.TEXT_FORMATTED_NUMBER
    assert top.remedy_symbol is None
    assert "client_code" in top.remedy
    assert "keep_as_text" in top.remedy
    assert any("00417" in item for item in top.evidence)


def test_a_two_bps_rate_difference_is_not_reported_as_a_date_epoch_offset() -> None:
    """A whole-number delta on a *text* expected side used to answer the date rule's filter.

    It could not arise before, because a VALUE_DIFFERS with non-numeric expected carried no
    delta. Now that text spelling a number is compared as the number, a rate one or two out
    would have been diagnosed as the 1900 leap-year bug.
    """
    diagnoses = _diagnose(["20.0", "27.5"], [22.0, 29.5], column="tier_bps")

    assert not any(d.cause is DiagnosticCause.DATE_SERIAL_OFFSET for d in diagnoses)


def test_a_string_that_is_not_a_number_is_not_a_text_formatting_problem() -> None:
    diagnoses = _diagnose(["Equity", "Cash"], ["equity", "cash"])

    assert not any(d.cause is DiagnosticCause.TEXT_FORMATTED_NUMBER for d in diagnoses)


# ── ranking and limits ──────────────────────────────────────────────────────


def test_diagnoses_come_back_most_confident_first_and_are_capped() -> None:
    frame = pl.DataFrame({"a": [1.0, 2.0, 3.0, 4.0], "b": [1.0, None, 3.0, None]})
    actual = frame.select(pl.col("a") + pl.col("b")).to_series().to_list()

    diagnoses = _diagnose([2.0, 2.0, 6.0, 4.0], [*actual, 99.0])

    assert len(diagnoses) <= 4
    assert [d.confidence for d in diagnoses] == sorted(
        (d.confidence for d in diagnoses), reverse=True
    )


def test_nothing_is_claimed_when_no_signature_fits() -> None:
    assert _diagnose([1.0, 2.0, 3.0], [17.0, 2.0, 3.0]) == []


@pytest.mark.parametrize(
    ("value", "places"),
    [(4491760.58, 2), (120.0, 0), (0.12345, 5), (2.675, 3), (-1.48, 2)],
)
def test_decimals_retained_reads_the_fifteen_significant_digit_view(
    value: float, places: int
) -> None:
    assert decimals_retained(value) == places
