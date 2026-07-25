"""Excel ground truth for ROUND, ROUNDUP and ROUNDDOWN.

The tables below are the tripwire CONVENTIONS.md asks for. They are transcribed Excel
results, not values computed from the implementation, so a polars upgrade that shifts
rounding behaviour fails here rather than shifting a reconciliation by a penny six weeks
later.
"""

from __future__ import annotations

import polars as pl
import pytest

from kedge.xl import round_down, round_half_away, round_up
from kedge.xl.errors import ExcelArgumentError

# ══════════════════════════════════════════════════════════════════════════════════════
# EXCEL GROUND TRUTH
#
# These are Excel's actual results, not polars' and not Python's.
#
# Cases marked (MS) are transcribed verbatim from Microsoft's published documentation for
# the function concerned -- the example tables at the foot of the ROUND, ROUNDUP and
# ROUNDDOWN support pages. The remainder are Excel's observed behaviour on the boundary
# cases that matter for reconciliation: the `.5` boundary in both signs, negative digit
# counts, and the values whose nearest double sits just below a rounding boundary.
#
# Where Excel disagrees with naive IEEE-754 arithmetic the comment says so, because those
# rows are the ones that justify this module existing at all. Excel rounds against the
# 15-significant-decimal-digit view of a value -- what it displays -- so ROUND(2.15, 1) is
# 2.2 even though the nearest double to 2.15 is 2.14999999999999991118...
# ══════════════════════════════════════════════════════════════════════════════════════

EXCEL_ROUND_CASES: list[tuple[float, int, float]] = [
    # ── the .5 boundary, positive: half AWAY from zero, where polars rounds half to even
    (0.5, 0, 1.0),  # polars .round() gives 0.0
    (1.5, 0, 2.0),  # agrees with polars by coincidence
    (2.5, 0, 3.0),  # polars .round() gives 2.0
    (3.5, 0, 4.0),  # agrees with polars by coincidence
    (4.5, 0, 5.0),  # polars .round() gives 4.0
    # ── the .5 boundary, negative: symmetric, away from zero
    (-0.5, 0, -1.0),  # polars .round() gives -0.0
    (-1.5, 0, -2.0),
    (-2.5, 0, -3.0),  # polars .round() gives -2.0
    (-3.5, 0, -4.0),
    # ── Microsoft's own ROUND examples
    (2.15, 1, 2.2),  # (MS) nearest double is 2.1499999999999999, naive gives 2.1
    (2.149, 1, 2.1),  # (MS)
    (-1.475, 2, -1.48),  # (MS) nearest double is -1.4749999999999999, naive gives -1.47
    (21.5, -1, 20.0),  # (MS) 2.15 tens rounds to 2 tens
    (626.3, -3, 1000.0),  # (MS)
    (1.98, -1, 0.0),  # (MS)
    (-50.55, -2, -100.0),  # (MS)
    # ── decimal representation: Excel rounds what it displays, not the exact double
    (2.675, 2, 2.68),  # Python's round(2.675, 2) gives 2.67
    (1.005, 2, 1.01),  # Python's round(1.005, 2) gives 1.0
    (2.345, 2, 2.35),  # nearest double is 2.34499999999999997
    (0.045, 2, 0.05),  # nearest double is 0.04499999999999999833
    (1.45, 1, 1.5),  # nearest double is 1.44999999999999995559
    (0.15, 1, 0.2),  # nearest double is 0.1499999999999999944
    (0.125, 2, 0.13),  # exactly representable, so pure half-away-from-zero
    (0.135, 2, 0.14),  # nearest double is above the boundary, both models agree
    # ── negative digits: Excel rounds to tens, hundreds, thousands
    (15.0, -1, 20.0),
    (25.0, -1, 30.0),  # banker's rounding would give 20
    (-25.0, -1, -30.0),
    (1250.0, -2, 1300.0),
    (-1250.0, -2, -1300.0),
    (1234.5678, -2, 1200.0),
    (12345.0, -5, 0.0),  # rounds away entirely
    (-1.0, -1, 0.0),  # and the result is zero, not negative zero
    # ── ordinary cases and identities
    (0.0, 0, 0.0),
    (100.0, 0, 100.0),
    (3.7, 0, 4.0),
    (-3.7, 0, -4.0),
    (-0.2, 0, 0.0),  # magnitude rounds to zero; Excel shows 0, never -0
    (12345.6789, 4, 12345.6789),  # digits beyond the value change nothing
    (1.0e15 + 0.5, 0, 1.0e15 + 1.0),  # large magnitude: the correction must not overshoot
]

EXCEL_ROUNDUP_CASES: list[tuple[float, int, float]] = [
    # ── Microsoft's own ROUNDUP examples
    (3.2, 0, 4.0),  # (MS)
    (76.9, 0, 77.0),  # (MS)
    (3.14159, 3, 3.142),  # (MS)
    (-3.14159, 1, -3.2),  # (MS) away from zero, so ceil() would be wrong here
    (31415.92654, -2, 31500.0),  # (MS)
    # ── away from zero in both signs
    (1.1, 0, 2.0),
    (-1.1, 0, -2.0),
    (0.5, 0, 1.0),
    (-0.5, 0, -1.0),
    (0.1, 0, 1.0),
    (-0.1, 0, -1.0),
    # ── already at the target precision: no change, no spurious step
    (5.0, 0, 5.0),
    (-5.0, 0, -5.0),
    (0.0, 0, 0.0),
    (1.0, 2, 1.0),
    (2.5, 1, 2.5),
    # ── representation guard: the sum is 0.30000000000000004, Excel still gives 0.3
    (0.1 + 0.2, 1, 0.3),
]

EXCEL_ROUNDDOWN_CASES: list[tuple[float, int, float]] = [
    # ── Microsoft's own ROUNDDOWN examples
    (3.2, 0, 3.0),  # (MS)
    (76.9, 0, 76.0),  # (MS)
    (3.14159, 3, 3.141),  # (MS)
    (-3.14159, 1, -3.1),  # (MS) toward zero, so floor() would be wrong here
    (31415.92654, -2, 31400.0),  # (MS)
    # ── toward zero in both signs: truncation, not floor
    (1.9, 0, 1.0),
    (-1.9, 0, -1.0),  # floor() would give -2
    (0.5, 0, 0.0),
    (-0.5, 0, 0.0),  # Excel shows 0, not -0
    (-0.9, 0, 0.0),
    # ── already at the target precision
    (5.0, 0, 5.0),
    (-5.0, 0, -5.0),
    (0.0, 0, 0.0),
    (1234.5678, -2, 1200.0),
    # ── representation guard: the double is a hair below 3, Excel's 15-digit view is 3
    (2.9999999999999996, 0, 3.0),
]


def _evaluate(expr: pl.Expr, value: float | int | None) -> float | None:
    """Evaluate an expression over a one-row LazyFrame, proving lazy safety as it goes."""
    return pl.LazyFrame({"x": [value]}).select(result=expr).collect().item()


@pytest.mark.parametrize(("value", "digits", "expected"), EXCEL_ROUND_CASES)
def test_round_half_away_should_match_excel(value: float, digits: int, expected: float) -> None:
    """`round_half_away` should reproduce Excel's ROUND for every ground-truth case."""
    assert _evaluate(round_half_away(pl.col("x"), digits), value) == pytest.approx(expected)


@pytest.mark.parametrize(("value", "digits", "expected"), EXCEL_ROUNDUP_CASES)
def test_round_up_should_match_excel(value: float, digits: int, expected: float) -> None:
    """`round_up` should reproduce Excel's ROUNDUP for every ground-truth case."""
    assert _evaluate(round_up(pl.col("x"), digits), value) == pytest.approx(expected)


@pytest.mark.parametrize(("value", "digits", "expected"), EXCEL_ROUNDDOWN_CASES)
def test_round_down_should_match_excel(value: float, digits: int, expected: float) -> None:
    """`round_down` should reproduce Excel's ROUNDDOWN for every ground-truth case."""
    assert _evaluate(round_down(pl.col("x"), digits), value) == pytest.approx(expected)


@pytest.mark.parametrize("function", [round_half_away, round_up, round_down])
@pytest.mark.parametrize("digits", [-2, 0, 2])
def test_rounding_should_propagate_nulls(function, digits: int) -> None:
    """Every rounding function should return null for a null input, as Excel leaves blanks."""
    assert _evaluate(function(pl.col("x"), digits), None) is None


def test_rounding_should_leave_an_all_null_column_untouched() -> None:
    """Rounding an entirely null column should not invent zeros."""
    frame = pl.LazyFrame({"x": [None, None, None]}, schema={"x": pl.Float64})
    result = frame.select(round_half_away(pl.col("x"), 2)).collect().to_series()
    assert result.to_list() == [None, None, None]


def _assert_no_negative_zero(results: list[float]) -> None:
    """`-0.0 == 0.0` is True, so the sign has to be checked through the repr."""
    assert not any(str(value).startswith("-") for value in results)


@pytest.mark.parametrize("function", [round_half_away, round_down])
def test_rounding_should_never_produce_negative_zero(function) -> None:
    """A negative magnitude collapsing to zero should give 0.0; Excel has no negative zero."""
    frame = pl.LazyFrame({"x": [-0.2, -0.4, -0.0, -0.49]})
    results = frame.select(function(pl.col("x"), 0)).collect().to_series().to_list()
    assert results == [0.0, 0.0, 0.0, 0.0]
    _assert_no_negative_zero(results)


def test_round_up_should_never_produce_negative_zero() -> None:
    """ROUNDUP only reaches zero from zero, since it moves away from it.

    Kept separate because ROUNDUP(-0.2, 0) is -1, not 0 -- the away-from-zero direction
    means the collapsing-to-zero cases above do not apply.
    """
    frame = pl.LazyFrame({"x": [-0.0, 0.0]})
    results = frame.select(round_up(pl.col("x"), 0)).collect().to_series().to_list()
    assert results == [0.0, 0.0]
    _assert_no_negative_zero(results)


def test_round_half_away_should_accept_integer_columns() -> None:
    """An Int64 column should round like a float one; Excel has a single numeric type."""
    frame = pl.LazyFrame({"x": [25, -25, 15, None]}, schema={"x": pl.Int64})
    result = frame.select(round_half_away(pl.col("x"), -1)).collect().to_series()
    assert result.dtype == pl.Float64
    assert result.to_list() == [30.0, -30.0, 20.0, None]


def test_rounding_should_compose_inside_with_columns() -> None:
    """The functions should be usable as ordinary expressions in a lazy pipeline."""
    frame = pl.LazyFrame({"gross": [2.675, 1.005], "rate": [0.5, 0.5]})
    result = (
        frame.with_columns(net=round_half_away(pl.col("gross") * pl.col("rate"), 2))
        .collect()
        .to_series(2)
        .to_list()
    )
    assert result == pytest.approx([1.34, 0.5])


@pytest.mark.parametrize("function", [round_half_away, round_up, round_down])
@pytest.mark.parametrize("digits", [16, -16, 400])
def test_rounding_should_reject_digits_beyond_double_precision(function, digits: int) -> None:
    """Digit counts a 64-bit float cannot express should raise, not silently do nothing."""
    with pytest.raises(ExcelArgumentError, match="outside the supported range"):
        function(pl.col("x"), digits)


@pytest.mark.parametrize("digits", [2.0, "2", None, True])
def test_round_half_away_should_reject_non_integer_digits(digits: object) -> None:
    """A non-int digit count should raise rather than being coerced."""
    with pytest.raises(ExcelArgumentError, match="must be an int"):
        round_half_away(pl.col("x"), digits)  # type: ignore[arg-type]


# ══════════════════════════════════════════════════════════════════════════════════════
# POLARS TRIPWIRES
#
# These assert what polars 1.43.0 currently does, so that a version bump which changes it
# fails loudly here. If one of these starts failing, the workaround in kedge.xl.rounding
# may have become redundant -- or newly wrong. Either way, read before deleting.
# ══════════════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("value", "polars_result", "excel_result"),
    [
        (0.5, 0.0, 1.0),
        (2.5, 2.0, 3.0),
        (4.5, 4.0, 5.0),
        (-0.5, -0.0, -1.0),
        (-2.5, -2.0, -3.0),
    ],
)
def test_polars_native_round_should_still_use_bankers_rounding(
    value: float, polars_result: float, excel_result: float
) -> None:
    """polars' `.round()` should still round half to even, which is why this module exists."""
    native = pl.LazyFrame({"x": [value]}).select(pl.col("x").round(0)).collect().item()
    assert native == pytest.approx(polars_result)
    assert native != pytest.approx(excel_result)
    assert _evaluate(round_half_away(pl.col("x"), 0), value) == pytest.approx(excel_result)


def test_polars_native_round_should_still_reject_negative_decimals() -> None:
    """polars' `.round()` should still refuse negative decimals, which Excel's ROUND accepts.

    This is the reason the module scales by hand rather than forwarding to polars, quite
    apart from the rounding mode.
    """
    with pytest.raises(OverflowError):
        pl.LazyFrame({"x": [25.0]}).select(pl.col("x").round(-1))

    assert _evaluate(round_half_away(pl.col("x"), -1), 25.0) == pytest.approx(30.0)


def test_polars_native_round_should_still_offer_a_half_away_from_zero_mode() -> None:
    """polars 1.43 gained `mode="half_away_from_zero"`, which PLAN 2.6 does not mention.

    It fixes the rounding *mode* correctly, so this test records that the native route now
    exists. It is deliberately not used: see the next test for why it is still not enough.
    """
    native = (
        pl.LazyFrame({"x": [0.5, 2.5, -2.5]})
        .select(pl.col("x").round(0, mode="half_away_from_zero"))
        .collect()
        .to_series()
        .to_list()
    )
    assert native == pytest.approx([1.0, 3.0, -3.0])


def test_polars_native_half_away_mode_should_still_disagree_with_excel_on_representation() -> None:
    """The native half-away mode should still diverge from Excel on a 15-digit boundary.

    Excel's ROUND(1.005, 2) is 1.01 because Excel rounds its displayed 15-significant-digit
    value. polars rounds closer to the exact double and gives 1.0. If this test starts
    failing, polars has adopted Excel's model and `round_half_away` may be able to delegate
    -- but only once negative digits are also supported.
    """
    native = (
        pl.LazyFrame({"x": [1.005]})
        .select(pl.col("x").round(2, mode="half_away_from_zero"))
        .collect()
        .item()
    )
    assert native == pytest.approx(1.0)
    assert _evaluate(round_half_away(pl.col("x"), 2), 1.005) == pytest.approx(1.01)
