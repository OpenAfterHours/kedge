"""Excel ``ROUND`` / ``ROUNDUP`` / ``ROUNDDOWN`` as lazy polars expressions.

This is the single most load-bearing module in ``kedge.xl``. PLAN 2.6 names rounding as
"the single most likely source of pennies-out reconciliation failures", and it is right:
polars rounds half-to-even by default, Excel rounds half-away-from-zero always.

Two independent problems are solved here, and both matter.

**1. Rounding mode.** ``0.5`` goes to ``1`` in Excel and to ``0`` in polars; ``2.5`` goes
to ``3`` and ``2`` respectively. ``round_half_away`` reproduces Excel.

**2. Decimal representation.** This is the subtler one, and the reason this module does not
simply forward to polars. ``2.15`` is not ``2.15`` in IEEE-754 -- the nearest double is
``2.149999999999999911...``. A naive half-away-from-zero implementation therefore returns
``2.1``, but Microsoft's own documentation for ``ROUND`` states ``=ROUND(2.15, 1)`` is
``2.2``. Likewise ``=ROUND(-1.475, 2)`` is documented as ``-1.48`` where the double is
``-1.47499999999999997...``. Excel rounds against the value's 15-significant-decimal-digit
representation -- what it shows you -- not against the exact binary double.

**The choice made here: match Excel, not IEEE.** The purpose of this module is
reconciliation against numbers a workbook already computed, so agreeing with Excel is the
requirement and agreeing with ``decimal`` or with Python's ``round`` is not. The
consequence is that ``round_half_away(pl.lit(2.675), 2)`` returns ``2.68`` where
``round(2.675, 2)`` in Python returns ``2.67``. That divergence is deliberate, and the
ground-truth table in ``tests/unit/test_xl_rounding.py`` pins it.

The correction is implemented as a bounded relative nudge of the scaled magnitude: a value
within roughly one part in 1e15 below a rounding boundary is treated as being on the
boundary, which is the same tolerance Excel's 15-digit model implies. The nudge is capped
at ``_MAX_CORRECTION`` so it can never move a value across a boundary by more than a
thousandth of a unit, which keeps it harmless at magnitudes where a double has no
meaningful fractional part left.

References:
    https://support.microsoft.com/en-us/office/round-function-c018c5d8-40fb-4053-90b1-b3e7f61a213c
    https://support.microsoft.com/en-us/office/roundup-function-f8bc9b23-e795-47db-8703-db171d0c42a7
    https://support.microsoft.com/en-us/office/rounddown-function-2ec94c73-241f-4b01-8c6f-17e6d7968f53
"""

from __future__ import annotations

from typing import Final

import polars as pl

from kedge.xl.errors import ExcelArgumentError

__all__ = ["MAX_DIGITS", "round_down", "round_half_away", "round_up"]


#: Largest ``digits`` magnitude accepted. A double carries about 15--17 significant decimal
#: digits, so rounding beyond this is a no-op at best and noise at worst; and ``10 ** 15``
#: is still exactly representable as a float, which keeps the scaling step exact.
MAX_DIGITS: Final[int] = 15

#: Relative tolerance for the decimal-representation correction described in the module
#: docstring. Roughly four ULPs at ``Float64``, matching Excel's 15-significant-digit view.
_REPRESENTATION_EPSILON: Final[float] = 1e-15

#: Absolute ceiling on that correction, in units of the scaled value. Without it the
#: relative nudge would exceed half a unit once the scaled magnitude passes ~5e14 and would
#: start shifting genuine values by a whole step.
_MAX_CORRECTION: Final[float] = 1e-3


def _check_digits(digits: int, function: str) -> None:
    """Reject digit counts that cannot express a meaningful Excel rounding."""
    if not isinstance(digits, int) or isinstance(digits, bool):
        msg = (
            f"{function}: digits must be an int, got {type(digits).__name__}. "
            f"Excel's second ROUND argument is a whole number of decimal places."
        )
        raise ExcelArgumentError(msg)
    if abs(digits) > MAX_DIGITS:
        msg = (
            f"{function}: digits={digits} is outside the supported range "
            f"[-{MAX_DIGITS}, {MAX_DIGITS}]. A 64-bit float carries about 15 significant "
            f"decimal digits, so rounding further cannot change the value; if you meant "
            f"to scale rather than round, do that explicitly."
        )
        raise ExcelArgumentError(msg)


def _scale(expr: pl.Expr, digits: int) -> tuple[pl.Expr, float]:
    """Scale so that rounding to ``digits`` places becomes rounding to a whole number.

    Multiplies for positive ``digits`` and divides for negative ones rather than
    multiplying by a fractional power of ten: ``10 ** 2`` is exact as a float but ``10 **
    -2`` is not, and using the exact form on both sides keeps the scaling reversible.
    """
    numeric = expr.cast(pl.Float64)
    factor = float(10 ** abs(digits))
    return (numeric * factor if digits >= 0 else numeric / factor), factor


def _unscale(rounded: pl.Expr, factor: float, digits: int) -> pl.Expr:
    """Undo ``_scale`` and normalise negative zero.

    ``-0.4`` rounds to a magnitude of ``0`` carrying a negative sign, which polars renders
    as ``-0.0``. It compares equal to ``0.0`` so it never breaks a reconciliation, but
    Excel has no such value and writing ``-0`` into an output column looks like a defect.
    Note that the obvious ``+ 0.0`` trick does not work -- polars elides it.
    """
    out = rounded / factor if digits >= 0 else rounded * factor
    return pl.when(out == 0.0).then(pl.lit(0.0)).otherwise(out)


def _correction(magnitude: pl.Expr) -> pl.Expr:
    """The bounded decimal-representation nudge, in units of the scaled magnitude."""
    return pl.min_horizontal(magnitude * _REPRESENTATION_EPSILON, pl.lit(_MAX_CORRECTION))


def round_half_away(expr: pl.Expr, digits: int = 0) -> pl.Expr:
    """Round half away from zero to ``digits`` decimal places, matching Excel's ``ROUND``.

    Excel: ``0.5`` -> ``1``, ``1.5`` -> ``2``, ``2.5`` -> ``3``, ``-0.5`` -> ``-1``,
    ``-2.5`` -> ``-3``. polars' native ``Expr.round`` defaults to half-to-even and gives
    ``0``, ``2``, ``2``, ``-0`` and ``-2`` for the same inputs -- correct for statistics,
    wrong for a workbook. Never translate an Excel ``ROUND`` region with a bare
    ``.round()``.

    Negative ``digits`` round to tens, hundreds and so on exactly as Excel does:
    ``ROUND(1250, -2)`` is ``1300``. polars' native ``round`` cannot do this at all -- it
    raises ``OverflowError`` on a negative argument.

    Values are compared against their 15-significant-digit decimal representation rather
    than their exact binary value, so ``ROUND(2.15, 1)`` is ``2.2`` as Microsoft documents
    and not the ``2.1`` that naive float arithmetic produces. See the module docstring.

    Nulls propagate: null in, null out. Excel treats an empty cell in ``ROUND`` as zero, so
    if the source region could contain blanks, compose with ``kedge.xl.empty_as_zero``
    first -- that decision belongs at the call site, not hidden in here.

    Args:
        expr: A numeric expression. Cast to ``Float64`` first, so an integer column returns
            a float column; Excel has only one numeric type and this keeps the semantics
            uniform.
        digits: Decimal places, ``-15`` to ``15``. Negative rounds left of the point.

    Returns:
        A ``Float64`` expression.

    Raises:
        ExcelArgumentError: If ``digits`` is not an int within the supported range.
    """
    _check_digits(digits, "round_half_away")
    scaled, factor = _scale(expr, digits)
    magnitude = scaled.abs()
    whole = (magnitude + _correction(magnitude) + 0.5).floor()
    return _unscale(whole * scaled.sign(), factor, digits)


def round_up(expr: pl.Expr, digits: int = 0) -> pl.Expr:
    """Round away from zero to ``digits`` places, matching Excel's ``ROUNDUP``.

    Away from zero in both signs: ``ROUNDUP(3.2, 0)`` is ``4`` and ``ROUNDUP(-3.14159, 1)``
    is ``-3.2``. This is not ``ceil`` -- ``ceil(-3.2)`` is ``-3``.

    The representation correction runs in the opposite direction from ``round_half_away``:
    the magnitude is nudged *down* before the ceiling, so a value a few ULPs above a
    boundary is not promoted a whole step. Without it ``ROUNDUP(0.1 + 0.2, 1)`` would
    return ``0.4``, because the sum is ``0.30000000000000004``; Excel returns ``0.3``.

    Nulls propagate.

    Args:
        expr: A numeric expression, cast to ``Float64``.
        digits: Decimal places, ``-15`` to ``15``. Negative rounds left of the point.

    Returns:
        A ``Float64`` expression.

    Raises:
        ExcelArgumentError: If ``digits`` is not an int within the supported range.
    """
    _check_digits(digits, "round_up")
    scaled, factor = _scale(expr, digits)
    magnitude = scaled.abs()
    whole = (magnitude - _correction(magnitude)).ceil()
    return _unscale(whole * scaled.sign(), factor, digits)


def round_down(expr: pl.Expr, digits: int = 0) -> pl.Expr:
    """Round toward zero to ``digits`` places, matching Excel's ``ROUNDDOWN``.

    Toward zero in both signs: ``ROUNDDOWN(3.9, 0)`` is ``3`` and ``ROUNDDOWN(-3.9, 0)`` is
    ``-3``. This is not ``floor`` -- ``floor(-3.9)`` is ``-4``. It is truncation.

    The magnitude is nudged *up* before the floor, so a value a few ULPs below a boundary is
    not truncated a whole step down: ``ROUNDDOWN(2.9999999999999996, 0)`` returns ``3``,
    matching Excel's 15-digit view of that double, rather than ``2``.

    Nulls propagate.

    Args:
        expr: A numeric expression, cast to ``Float64``.
        digits: Decimal places, ``-15`` to ``15``. Negative rounds left of the point.

    Returns:
        A ``Float64`` expression.

    Raises:
        ExcelArgumentError: If ``digits`` is not an int within the supported range.
    """
    _check_digits(digits, "round_down")
    scaled, factor = _scale(expr, digits)
    magnitude = scaled.abs()
    whole = (magnitude + _correction(magnitude)).floor()
    return _unscale(whole * scaled.sign(), factor, digits)
