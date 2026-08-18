"""Excel's coercion rules: empty cells as zero, and text-formatted numbers read as numbers.

Two of PLAN 2.6's silent-wrongness entries live here, and they are the same underlying
problem -- Excel coerces whatever is in a cell into a number at the point of use, and
polars does not.

**Empty cells.** ``=1+A1`` with ``A1`` empty is ``1`` in Excel. ``pl.col("one") +
pl.col("a")`` with a null is null in polars, and that null then propagates through every
downstream step until the row quietly disappears from a sum. The asymmetry is worth
stating plainly because it catches people repeatedly: polars is *right* about nulls and
Excel is *convenient* about them, and reconciliation needs the convenient answer.

Note the consequence for multiplication, which surprises people: an empty cell is zero, so
``=A1*B1`` with ``B1`` empty is ``0``, not ``A1``. ``mul`` reproduces that. "Empty as zero"
is not "ignore the operand".

Division is the exception. ``=A1/B1`` with ``B1`` empty is ``#DIV/0!``, because an empty
denominator coerces to zero like any other empty cell -- so ``div`` returns null for both a
zero and a null denominator.

**Text-formatted numbers.** A hand-in that arrives with a numeric column stored as text is
routine, and PLAN 2.6 flags it as a "silent type mismatch on join keys". In polars 1.43 the
join is not in fact silent (see ``to_number``'s docstring), but every arithmetic use of the
column is. ``to_number`` performs the coercion Excel does implicitly.
"""

from __future__ import annotations

from typing import Final

import polars as pl

__all__ = [
    "Operand",
    "add",
    "div",
    "empty_as_zero",
    "mul",
    "sub",
    "sum_horizontal",
    "to_number",
]


#: Operand type for the arithmetic helpers. Deliberately excludes ``str``: polars usually
#: reads a bare string as a column name, but ``iferror(x, "n/a")`` reads it as a literal,
#: and an ambiguity that changes meaning silently has no place in this module. Pass
#: ``pl.col("name")`` or ``pl.lit("text")`` and the intent is on the page.
type Operand = pl.Expr | int | float

#: Characters stripped before parsing a text-formatted number: whitespace (including the
#: non-breaking and narrow non-breaking spaces that survive a copy-paste out of a web
#: report), the comma group separator, and the currency symbols Excel accepts inline.
_STRIP_PATTERN: Final[str] = r"[\s,$£€¥]"


def _operand(value: Operand) -> pl.Expr:
    """Normalise an operand to an expression."""
    return value if isinstance(value, pl.Expr) else pl.lit(value)


def empty_as_zero(expr: pl.Expr) -> pl.Expr:
    """Treat nulls as zero, the way Excel treats an empty cell in arithmetic.

    The primitive the rest of this module is built from, exposed because a translated
    formula often needs it once and then uses ordinary polars operators.

    **Cast first, then fill, and the order is the whole point.** Given a ``String`` column this
    used to be a silent no-op: a pasted grid has no nulls, it has empty *strings*, so there was
    nothing to fill and the text passed straight through. The failure then surfaced several
    operations later as ``arithmetic on dtypes str and dyn float is not allowed``, from inside a
    query plan, pointing nowhere near the column that caused it -- and in marimo's app mode the
    cell rendered nothing at all. Casting strictly turns that into an error at this expression,
    naming this column.

    The cast is to ``Float64`` unconditionally, matching
    :func:`~kedge.xl.rounding.round_half_away`: Excel has one numeric type, and keeping the
    semantics uniform is worth more here than preserving an ``Int64``.

    If the column really is text-formatted numbers, the answer is
    :func:`~kedge.xl.nulls.to_number`, which knows about thousands separators, currency symbols
    and parenthesised negatives -- and note that :func:`kedge.ingest.read_data` now does that
    conversion on the way in, so a hand-in should not reach here as text at all.

    Args:
        expr: A numeric expression, or one castable to a number.

    Returns:
        A ``Float64`` expression with nulls replaced by ``0``.

    Raises:
        polars.exceptions.InvalidOperationError: at collect time, if the values are not numeric.
            Deliberately not caught: a caller that meant text wants ``to_number`` first, and a
            caller that did not wants to know.
    """
    return expr.cast(pl.Float64, strict=True).fill_null(0)


def add(a: Operand, b: Operand) -> pl.Expr:
    """Add, treating nulls as zero, matching Excel's ``=A1+B1``.

    Excel: ``1 + <empty>`` is ``1``, and ``<empty> + <empty>`` is ``0``. polars: both are
    null. For more than two operands prefer ``sum_horizontal``, which is one expression
    rather than a chain.

    Args:
        a: Left operand.
        b: Right operand.

    Returns:
        An expression that is never null where either input is non-null, and ``0`` where
        both are null.
    """
    return empty_as_zero(_operand(a)) + empty_as_zero(_operand(b))


def sub(a: Operand, b: Operand) -> pl.Expr:
    """Subtract, treating nulls as zero, matching Excel's ``=A1-B1``.

    Excel: ``1 - <empty>`` is ``1`` and ``<empty> - 1`` is ``-1``.

    Args:
        a: Left operand.
        b: Right operand.

    Returns:
        An expression, ``0`` where both inputs are null.
    """
    return empty_as_zero(_operand(a)) - empty_as_zero(_operand(b))


def mul(a: Operand, b: Operand) -> pl.Expr:
    """Multiply, treating nulls as zero, matching Excel's ``=A1*B1``.

    Excel: ``5 * <empty>`` is ``0``, because the empty cell coerces to zero. This is the
    one that catches people -- "empty as zero" genuinely means the product collapses, not
    that the empty operand is skipped. If the workbook intent was "leave the value alone
    when the factor is missing", that is a different formula and should be translated as
    one.

    Args:
        a: Left operand.
        b: Right operand.

    Returns:
        An expression, ``0`` where either input is null.
    """
    return empty_as_zero(_operand(a)) * empty_as_zero(_operand(b))


def div(numerator: Operand, denominator: Operand) -> pl.Expr:
    """Divide, returning null where Excel would raise ``#DIV/0!``.

    Excel raises ``#DIV/0!`` for a zero denominator and for an empty one, since an empty
    cell coerces to zero. polars returns ``inf``, ``-inf`` or ``NaN``, and PLAN 2.6 is
    right that this is the dangerous case: ``inf`` propagates into a ``SUM`` and turns the
    whole aggregate into ``NaN`` without raising anything. A null does not -- polars'
    aggregates skip nulls, exactly as Excel's ``SUM`` skips blanks.

    The implementation nulls the *denominator* before dividing rather than masking the
    result afterwards, so no ``inf`` or ``NaN`` is ever materialised, not even in a branch
    that is later discarded. A ``NaN`` denominator is treated as a zero denominator; Excel
    has no ``NaN``, and letting one through would defeat the point.

    A null numerator over a valid denominator is ``0``, not null -- again because an empty
    cell is zero. Excel's ``=A1/2`` with ``A1`` empty is ``0``.

    Args:
        numerator: Dividend. Nulls treated as zero.
        denominator: Divisor. Null, zero and ``NaN`` all yield a null result.

    Returns:
        A ``Float64`` expression which is null exactly where Excel would show ``#DIV/0!``.
    """
    top = _operand(numerator).cast(pl.Float64)
    bottom = _operand(denominator).cast(pl.Float64)
    unusable = bottom.is_null() | bottom.is_nan() | (bottom == 0.0)
    safe = pl.when(unusable).then(pl.lit(None, pl.Float64)).otherwise(bottom)
    return empty_as_zero(top) / safe


def sum_horizontal(*exprs: pl.Expr | str) -> pl.Expr:
    """Sum across columns row-wise, ignoring nulls, matching Excel's ``=A1+B1+C1``.

    PLAN 2.6 claims ``pl.sum_horizontal`` already matches Excel here. **Verified against
    polars 1.43.0 and the claim holds**, including the case the plan does not mention: a
    row where every input is null returns ``0``, which is what Excel's ``=A1+B1+C1`` gives
    for three empty cells. The default ``ignore_nulls=True`` is what does the work; passing
    ``ignore_nulls=False`` reverts to polars' null-propagating behaviour and would be
    wrong here.

    This wrapper adds no behaviour. It exists so the Excel-compatibility decision is
    greppable in generated code alongside every other ``xl.`` call, and so that a future
    polars change to ``ignore_nulls`` defaults is caught by this module's tests rather than
    by a reconciliation failure. The test suite asserts polars' behaviour directly for that
    reason.

    Args:
        *exprs: Expressions or column names. Following polars' own convention -- and
            unlike the two-operand helpers in this module -- a ``str`` here is a column
            name, because this function forwards its arguments unchanged.

    Returns:
        An expression holding the row-wise sum, ``0`` for an all-null row.
    """
    return pl.sum_horizontal(*exprs, ignore_nulls=True)


def to_number(expr: pl.Expr) -> pl.Expr:
    """Coerce text-formatted numbers to ``Float64`` the way Excel coerces on the fly.

    Excel converts text to a number at the point of use, so ``="1,234"+0`` is ``1234`` and
    ``=VALUE("$1,000")`` is ``1000``. polars leaves the column as ``String``, and PLAN 2.6
    flags the consequence as a silent join-key dtype mismatch.

    That last part is **wrong for polars 1.43.0**: joining a ``String`` key to an ``Int64``
    key raises ``SchemaError``, loudly, at collect time. The genuinely silent damage is
    elsewhere -- ``pl.col("text_amount").sum()`` on a string column concatenates or fails
    depending on context, sort order is lexicographic so ``"10" < "9"``, and a
    ``group_by`` treats ``"1"`` and ``"1.0"`` as different keys. Coerce at load, as the
    plan says; just do not expect the join to be the thing that catches it.

    Handled, each because Excel handles it:

    * Leading and trailing whitespace, including non-breaking spaces.
    * Comma group separators: ``"1,234.56"`` -> ``1234.56``.
    * Currency symbols ``$``, ``£``, ``€``, ``¥`` anywhere in the string.
    * Trailing percent: ``"12%"`` -> ``0.12``.
    * Scientific notation: ``"1e3"`` -> ``1000.0``.
    * Parenthesised negatives: ``"(1,234.50)"`` -> ``-1234.5``. **This is the ambiguous
      one.** Excel converts ``(6)`` typed into a cell to ``-6``, but whether the same
      applies to on-the-fly coercion of a text value depends on version and locale.
      Supported here because accounting exports are the main source of text-formatted
      numeric columns and getting the sign wrong is worse than being slightly liberal.
      Only a matched pair counts: ``"(6"`` and ``"6)"`` are both null.

    Genuinely non-numeric text returns null rather than raising, which is what makes this
    usable in a ``with_columns`` over a dirty column. Note the liberalisations that follow
    from stripping before parsing: ``"1,2,3"`` becomes ``123`` and ``"5$"`` becomes ``5``,
    where Excel would give ``#VALUE!``. The alternative is a full locale-aware grammar, and
    the trade is deliberate -- profile the column and check the null count.

    Args:
        expr: A ``String`` expression. A numeric expression is cast to ``String`` first and
            passes through unchanged; polars' float formatting round-trips exactly, so this
            is safe if wasteful.

    Returns:
        A ``Float64`` expression, null wherever the text is not a number.
    """
    trimmed = expr.cast(pl.String).str.strip_chars()

    parenthesised = trimmed.str.starts_with("(") & trimmed.str.ends_with(")")
    core = (
        pl.when(parenthesised)
        .then(trimmed.str.strip_prefix("(").str.strip_suffix(")"))
        .otherwise(trimmed)
    )

    percentage = core.str.ends_with("%")
    core = pl.when(percentage).then(core.str.strip_suffix("%")).otherwise(core)

    value = core.str.replace_all(_STRIP_PATTERN, "").cast(pl.Float64, strict=False)
    value = pl.when(percentage).then(value / 100.0).otherwise(value)
    return pl.when(parenthesised).then(-value).otherwise(value)
