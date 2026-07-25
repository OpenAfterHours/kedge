"""Excel ground truth for empty-cell arithmetic, division by zero, and text coercion.

The tables below are transcribed Excel behaviour, and the tripwire tests at the foot assert
what polars currently does so that a version bump which changes it fails here.
"""

from __future__ import annotations

import math

import polars as pl
import pytest

from kedge.xl import (
    EXCEL_ERROR_LITERALS,
    add,
    div,
    empty_as_zero,
    error_text_to_null,
    iferror,
    is_error,
    mul,
    sub,
    sum_horizontal,
    to_number,
)

# ══════════════════════════════════════════════════════════════════════════════════════
# EXCEL GROUND TRUTH: an empty cell in arithmetic is zero
#
# These are Excel's actual results for `=A1+B1`, `=A1-B1` and `=A1*B1` where an empty cell
# stands in for a null. polars propagates the null instead and returns null for every row
# below that contains one -- which is how a row silently vanishes from a downstream sum.
#
# Note the multiplication column. "Empty as zero" means exactly that: `=5*<empty>` is 0,
# not 5. The empty operand is coerced, not skipped.
# ══════════════════════════════════════════════════════════════════════════════════════

EXCEL_EMPTY_CELL_CASES: list[tuple[float | None, float | None, float, float, float]] = [
    #  a       b      a+b     a-b     a*b
    (1.0, 2.0, 3.0, -1.0, 2.0),  # no empties: ordinary arithmetic
    (1.0, None, 1.0, 1.0, 0.0),  # =1+empty is 1, =1-empty is 1, =1*empty is 0
    (None, 3.0, 3.0, -3.0, 0.0),  # =empty+3 is 3, =empty-3 is -3
    (None, None, 0.0, 0.0, 0.0),  # all-empty row: Excel gives 0, polars gives null
    (0.0, None, 0.0, 0.0, 0.0),  # an explicit zero and an empty are indistinguishable
    (-4.5, None, -4.5, -4.5, 0.0),  # sign is preserved through the coercion
    (2.5, 4.0, 6.5, -1.5, 10.0),
]

# ══════════════════════════════════════════════════════════════════════════════════════
# EXCEL GROUND TRUTH: division
#
# Excel raises #DIV/0! for a zero denominator and for an empty one, since an empty cell
# coerces to zero like any other. kedge represents every Excel error as null. An empty
# numerator over a valid denominator is 0, again because an empty cell is zero.
# ══════════════════════════════════════════════════════════════════════════════════════

EXCEL_DIVISION_CASES: list[tuple[float | None, float | None, float | None]] = [
    (6.0, 2.0, 3.0),  # ordinary
    (-6.0, 2.0, -3.0),
    (1.0, 3.0, 1.0 / 3.0),
    (6.0, 0.0, None),  # #DIV/0!; polars gives inf
    (-6.0, 0.0, None),  # #DIV/0!; polars gives -inf
    (0.0, 0.0, None),  # #DIV/0!; polars gives NaN
    (6.0, None, None),  # empty denominator coerces to zero, so #DIV/0!
    (None, 2.0, 0.0),  # empty numerator is zero, so the quotient is zero
    (None, 0.0, None),  # #DIV/0! wins
    (None, None, None),  # #DIV/0!
]

# ══════════════════════════════════════════════════════════════════════════════════════
# EXCEL GROUND TRUTH: text coerced to number
#
# Excel converts text to a number at the point of use: `="1,234"+0` is 1234 and
# `=VALUE("$1,000")` is 1000. Genuinely non-numeric text is #VALUE!, which kedge represents
# as null so the expression stays usable over a dirty column.
#
# Cases marked (LIBERAL) are places where kedge accepts text Excel would reject. They fall
# out of stripping separators before parsing, and the trade is documented in `to_number`.
# ══════════════════════════════════════════════════════════════════════════════════════

EXCEL_TEXT_TO_NUMBER_CASES: list[tuple[str | None, float | None]] = [
    # ── plain numeric text
    ("1", 1.0),
    ("2.5", 2.5),
    ("-7", -7.0),
    ("+8", 8.0),
    ("0", 0.0),
    ("1e3", 1000.0),  # scientific notation
    # ── whitespace, including the non-breaking kind that survives a copy-paste out of a
    #    web report. Written as escapes so the bytes on the page are unambiguous.
    (" 3 ", 3.0),
    ("\u00a05\u00a0", 5.0),  # non-breaking space
    ("\u202f6", 6.0),  # narrow non-breaking space
    # ── thousands separators and currency
    ("1,234", 1234.0),
    ("1,234.56", 1234.56),
    ("$5.00", 5.0),
    ("£1,000.50", 1000.5),
    ("€9", 9.0),
    # ── percentages
    ("12%", 0.12),
    ("100%", 1.0),
    ("-50%", -0.5),
    # ── parenthesised negatives, the accounting convention
    ("(6)", -6.0),
    ("(1,234.50)", -1234.5),
    ("($20.00)", -20.0),
    ("(6", None),  # unmatched: not a number
    ("6)", None),  # unmatched: not a number
    # ── genuine garbage
    ("abc", None),
    ("", None),
    ("   ", None),
    (None, None),
    ("1.2.3", None),
    ("N/A", None),
    ("#DIV/0!", None),  # an Excel error literal is not a number
    ("12-34", None),
    # ── accepted more readily than Excel would
    ("12 345", 12345.0),  # (LIBERAL) interior whitespace stripped as a group separator
    ("5$", 5.0),  # (LIBERAL) Excel gives #VALUE! for a trailing currency symbol
]


def _collect(frame: pl.LazyFrame, expr: pl.Expr) -> list:
    return frame.select(result=expr).collect().to_series().to_list()


# ── empty cells in arithmetic ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("a", "b", "expected_add", "expected_sub", "expected_mul"), EXCEL_EMPTY_CELL_CASES
)
def test_arithmetic_should_treat_nulls_as_empty_cells(
    a: float | None,
    b: float | None,
    expected_add: float,
    expected_sub: float,
    expected_mul: float,
) -> None:
    """`add`, `sub` and `mul` should treat a null operand as Excel treats an empty cell."""
    frame = pl.LazyFrame({"a": [a], "b": [b]}, schema={"a": pl.Float64, "b": pl.Float64})
    result = frame.select(
        added=add(pl.col("a"), pl.col("b")),
        subtracted=sub(pl.col("a"), pl.col("b")),
        multiplied=mul(pl.col("a"), pl.col("b")),
    ).collect()
    assert result.item(0, "added") == pytest.approx(expected_add)
    assert result.item(0, "subtracted") == pytest.approx(expected_sub)
    assert result.item(0, "multiplied") == pytest.approx(expected_mul)


def test_arithmetic_should_accept_scalar_operands() -> None:
    """A bare number should be usable as an operand without wrapping it in `pl.lit`."""
    frame = pl.LazyFrame({"a": [10.0, None]}, schema={"a": pl.Float64})
    assert _collect(frame, add(pl.col("a"), 5)) == pytest.approx([15.0, 5.0])
    assert _collect(frame, sub(pl.col("a"), 5)) == pytest.approx([5.0, -5.0])
    assert _collect(frame, mul(pl.col("a"), 2)) == pytest.approx([20.0, 0.0])


def test_add_should_keep_an_all_null_row_in_downstream_aggregates() -> None:
    """The point of empty-as-zero: rows must not vanish from a later sum.

    With plain polars addition every row containing a null becomes null, and the column
    total then reflects only the complete rows.
    """
    frame = pl.LazyFrame({"a": [1.0, None, 3.0], "b": [10.0, 20.0, None]})
    excel_total = frame.select(add(pl.col("a"), pl.col("b")).sum()).collect().item()
    polars_total = frame.select((pl.col("a") + pl.col("b")).sum()).collect().item()
    assert excel_total == pytest.approx(34.0)  # 11 + 20 + 3
    assert polars_total == pytest.approx(11.0)  # only the complete row survives


def test_empty_as_zero_should_replace_nulls_with_zero() -> None:
    """`empty_as_zero` should be the plain primitive the arithmetic helpers are built from."""
    frame = pl.LazyFrame({"a": [1.5, None, -2.0]})
    assert _collect(frame, empty_as_zero(pl.col("a"))) == pytest.approx([1.5, 0.0, -2.0])


# ── division ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("numerator", "denominator", "expected"), EXCEL_DIVISION_CASES)
def test_div_should_return_null_where_excel_raises_div_by_zero(
    numerator: float | None, denominator: float | None, expected: float | None
) -> None:
    """`div` should give null exactly where Excel shows #DIV/0!, and never inf or NaN."""
    frame = pl.LazyFrame(
        {"n": [numerator], "d": [denominator]},
        schema={"n": pl.Float64, "d": pl.Float64},
    )
    result = frame.select(result=div(pl.col("n"), pl.col("d"))).collect().item()
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


def test_div_should_never_produce_inf_or_nan() -> None:
    """No row of a division should be non-finite, whatever the denominator holds."""
    frame = pl.LazyFrame(
        {
            "n": [1.0, -1.0, 0.0, 5.0, None, 2.0],
            "d": [0.0, 0.0, 0.0, 4.0, 0.0, None],
        }
    )
    checks = (
        frame.select(quotient=div(pl.col("n"), pl.col("d")))
        .select(
            nan=pl.col("quotient").is_nan().any(),
            infinite=pl.col("quotient").is_infinite().any(),
        )
        .collect()
    )
    assert checks.item(0, "nan") is False
    assert checks.item(0, "infinite") is False


def test_div_should_not_poison_a_downstream_aggregate() -> None:
    """A zero denominator must not wreck a column total, as native division does.

    This is the concrete harm PLAN 2.6 describes. Native division leaves an inf in the
    column and the total becomes inf; add a 0/0 row and it becomes NaN instead. Both are
    useless, and neither raises.
    """
    frame = pl.LazyFrame({"n": [10.0, 20.0, 30.0], "d": [2.0, 0.0, 3.0]})
    safe_total = frame.select(div(pl.col("n"), pl.col("d")).sum()).collect().item()
    native_total = frame.select((pl.col("n") / pl.col("d")).sum()).collect().item()
    assert safe_total == pytest.approx(15.0)  # 5 + 10, the #DIV/0! row skipped
    assert math.isinf(native_total)  # inf swallowed the total

    with_nan = pl.LazyFrame({"n": [10.0, 0.0], "d": [2.0, 0.0]})
    assert with_nan.select(div(pl.col("n"), pl.col("d")).sum()).collect().item() == pytest.approx(
        5.0
    )
    assert math.isnan(with_nan.select((pl.col("n") / pl.col("d")).sum()).collect().item())


def test_div_should_treat_a_nan_denominator_as_unusable() -> None:
    """Excel has no NaN, so one arriving from elsewhere must not propagate through a divide."""
    frame = pl.LazyFrame({"n": [1.0], "d": [float("nan")]})
    assert frame.select(result=div(pl.col("n"), pl.col("d"))).collect().item() is None


# ── row-wise addition ─────────────────────────────────────────────────────────────────


def test_sum_horizontal_should_match_excel_row_wise_addition() -> None:
    """`sum_horizontal` should ignore empties, matching Excel's `=A1+B1+C1`."""
    frame = pl.LazyFrame(
        {"a": [1.0, None, None, 5.0], "b": [2.0, 3.0, None, None], "c": [4.0, None, None, 5.0]}
    )
    result = _collect(frame, sum_horizontal("a", "b", "c"))
    assert result == pytest.approx([7.0, 3.0, 0.0, 10.0])


def test_sum_horizontal_should_return_zero_for_an_all_null_row() -> None:
    """Three empty cells summed in Excel give 0, not blank -- and so must this."""
    frame = pl.LazyFrame({"a": [None], "b": [None]}, schema={"a": pl.Float64, "b": pl.Float64})
    assert _collect(frame, sum_horizontal("a", "b")) == [0.0]


def test_sum_horizontal_should_accept_expressions_as_well_as_names() -> None:
    """Column names and expressions should both work, since arguments are forwarded to polars."""
    frame = pl.LazyFrame({"a": [1.0, None], "b": [2.0, 4.0]})
    result = _collect(frame, sum_horizontal(pl.col("a"), pl.col("b") * 2))
    assert result == pytest.approx([5.0, 8.0])


# ── text coercion ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("text", "expected"), EXCEL_TEXT_TO_NUMBER_CASES)
def test_to_number_should_coerce_text_the_way_excel_does(
    text: str | None, expected: float | None
) -> None:
    """`to_number` should read text-formatted numbers and give null for genuine garbage."""
    frame = pl.LazyFrame({"t": [text]}, schema={"t": pl.String})
    result = frame.select(result=to_number(pl.col("t"))).collect().item()
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


def test_to_number_should_return_a_float_column() -> None:
    """The whole point is a numeric dtype, so assert it rather than only the values."""
    frame = pl.LazyFrame({"t": ["1", "abc"]})
    assert frame.select(to_number(pl.col("t"))).collect_schema()["t"] == pl.Float64


def test_to_number_should_pass_a_numeric_column_through_unchanged() -> None:
    """Defensive use on an already-numeric column should be a no-op, not a corruption."""
    values = [1.5, None, -0.25, 1e-9, 123456789.12345679]
    frame = pl.LazyFrame({"x": values}, schema={"x": pl.Float64})
    assert _collect(frame, to_number(pl.col("x"))) == pytest.approx(values, nan_ok=True)


def test_to_number_should_fix_a_join_key_dtype_mismatch() -> None:
    """The PLAN 2.6 use case: a text-formatted key column joined against a numeric one."""
    left = pl.LazyFrame({"key": ["1", "2", "3"], "amount": [10.0, 20.0, 30.0]})
    right = pl.LazyFrame({"key": [1, 2, 3], "label": ["a", "b", "c"]})

    joined = (
        left.with_columns(to_number(pl.col("key")).cast(pl.Int64).alias("key"))
        .join(right, on="key", how="left")
        .collect()
    )
    assert joined.height == 3
    assert joined["label"].to_list() == ["a", "b", "c"]


# ── Excel error values ────────────────────────────────────────────────────────────────


def test_is_error_should_flag_every_value_that_stands_in_for_an_excel_error() -> None:
    """Null, NaN and infinity should all read as errors; ordinary numbers should not."""
    frame = pl.LazyFrame({"x": [1.0, 0.0, None, float("nan"), float("inf"), float("-inf")]})
    assert _collect(frame, is_error(pl.col("x"))) == [False, False, True, True, True, True]


def test_is_error_should_never_return_null() -> None:
    """A tri-state condition would silently drop rows from a `when`, so it must be boolean."""
    frame = pl.LazyFrame({"x": [None, None]}, schema={"x": pl.Float64})
    assert _collect(frame, is_error(pl.col("x"))) == [True, True]


def test_iferror_should_substitute_the_fallback_only_where_there_is_an_error() -> None:
    """`iferror` should reproduce Excel's IFERROR, leaving good values alone."""
    frame = pl.LazyFrame({"x": [1.5, None, float("inf")]})
    assert _collect(frame, iferror(pl.col("x"), 0.0)) == pytest.approx([1.5, 0.0, 0.0])


def test_iferror_should_treat_a_string_fallback_as_a_literal_not_a_column() -> None:
    """`IFERROR(x, "n/a")` means the text, which is why this argument breaks polars convention."""
    frame = pl.LazyFrame({"x": [1.0, None], "n/a": ["wrong", "wrong"]})
    assert _collect(frame, iferror(pl.col("x").cast(pl.String), "n/a")) == ["1.0", "n/a"]


def test_iferror_should_be_composable_with_div_to_reproduce_iferror_of_a_division() -> None:
    """The common workbook idiom `=IFERROR(A1/B1, 0)`, which must not leak an inf."""
    frame = pl.LazyFrame({"n": [10.0, 5.0], "d": [2.0, 0.0]})
    result = _collect(frame, iferror(div(pl.col("n"), pl.col("d")), 0.0))
    assert result == pytest.approx([5.0, 0.0])


@pytest.mark.parametrize("literal", sorted(EXCEL_ERROR_LITERALS))
def test_error_text_to_null_should_null_every_excel_error_literal(literal: str) -> None:
    """Cached values loaded with `data_only=True` carry errors as these exact strings."""
    frame = pl.LazyFrame({"t": [literal]}, schema={"t": pl.String})
    assert _collect(frame, error_text_to_null(pl.col("t"))) == [None]


def test_error_text_to_null_should_leave_other_text_alone() -> None:
    """Only the error literals should be nulled; genuine text must survive for later checks."""
    frame = pl.LazyFrame({"t": ["#DIV/0!", "12", "not an error", None]})
    assert _collect(frame, error_text_to_null(pl.col("t"))) == [None, "12", "not an error", None]


# ══════════════════════════════════════════════════════════════════════════════════════
# POLARS TRIPWIRES
#
# What polars 1.43.0 does today. If one of these fails, the corresponding workaround in
# kedge.xl.nulls may have become redundant -- or newly wrong.
# ══════════════════════════════════════════════════════════════════════════════════════


def test_polars_native_arithmetic_should_still_propagate_nulls() -> None:
    """polars' `+`, `-` and `*` should still return null when either operand is null."""
    frame = pl.LazyFrame({"a": [1.0], "b": [None]}, schema={"a": pl.Float64, "b": pl.Float64})
    result = frame.select(
        added=pl.col("a") + pl.col("b"),
        subtracted=pl.col("a") - pl.col("b"),
        multiplied=pl.col("a") * pl.col("b"),
    ).collect()
    assert result.row(0) == (None, None, None)


def test_polars_native_division_should_still_produce_non_finite_values() -> None:
    """polars' `/` should still give inf, -inf and NaN for a zero denominator."""
    frame = pl.LazyFrame({"n": [1.0, -1.0, 0.0], "d": [0.0, 0.0, 0.0]})
    result = frame.select(quotient=pl.col("n") / pl.col("d")).collect().to_series().to_list()
    assert result[0] == float("inf")
    assert result[1] == float("-inf")
    assert result[2] != result[2]  # NaN


def test_polars_native_integer_floor_division_should_still_return_null_on_zero() -> None:
    """PLAN 2.6 says divide-by-zero gives inf; for integer `//` polars gives null instead.

    Recorded because the plan's table is not the whole story: the failure shape depends on
    the operator as well as the dtype, and `//` is already null-safe.
    """
    frame = pl.LazyFrame({"n": [1, -1, 0], "d": [0, 0, 0]})
    result = frame.select(quotient=pl.col("n") // pl.col("d")).collect().to_series().to_list()
    assert result == [None, None, None]


def test_polars_sum_horizontal_should_still_ignore_nulls_by_default() -> None:
    """PLAN 2.6 claims `pl.sum_horizontal` already matches Excel. Verified here, directly.

    `sum_horizontal` in kedge.xl adds no behaviour -- it only makes the choice greppable --
    so this test rather than the wrapper is what actually guards the claim.
    """
    frame = pl.LazyFrame({"a": [1.0, None, None], "b": [2.0, 3.0, None]})
    result = frame.select(
        default=pl.sum_horizontal("a", "b"),
        propagating=pl.sum_horizontal("a", "b", ignore_nulls=False),
    ).collect()
    assert result["default"].to_list() == pytest.approx([3.0, 3.0, 0.0])
    assert result["propagating"].to_list() == [3.0, None, None]


def test_polars_column_sum_should_still_ignore_nulls_like_excel() -> None:
    """PLAN 2.6 says column `SUM()` matches between Excel and polars. It does.

    Including the all-empty case, where Excel's SUM gives 0 and so does polars.
    """
    mixed = pl.LazyFrame({"x": [1.0, None, 3.0]}).select(pl.col("x").sum()).collect().item()
    empty = (
        pl.LazyFrame({"x": [None, None]}, schema={"x": pl.Float64})
        .select(pl.col("x").sum())
        .collect()
        .item()
    )
    assert mixed == pytest.approx(4.0)
    assert empty == pytest.approx(0.0)


def test_polars_join_on_mismatched_key_dtypes_should_still_raise_not_coerce() -> None:
    """PLAN 2.6 calls the text-key mismatch "silent". In polars 1.43 it is not -- it raises.

    Recorded as a correction to the plan. The silent damage from a text-formatted numeric
    column is elsewhere: lexicographic ordering, and group_by treating "1" and "1.0" as
    different keys.
    """
    left = pl.LazyFrame({"key": ["1", "2"]})
    right = pl.LazyFrame({"key": [1, 2], "label": ["a", "b"]})
    with pytest.raises(pl.exceptions.SchemaError, match="datatypes of join keys don't match"):
        left.join(right, on="key", how="left").collect()


def test_text_formatted_numbers_should_still_sort_lexicographically() -> None:
    """The genuinely silent failure: "10" sorts before "9" until the column is coerced."""
    frame = pl.LazyFrame({"key": ["9", "10", "2"]})
    assert frame.select(pl.col("key").sort()).collect().to_series().to_list() == ["10", "2", "9"]
    coerced = frame.select(to_number(pl.col("key")).sort()).collect().to_series().to_list()
    assert coerced == pytest.approx([2.0, 9.0, 10.0])
