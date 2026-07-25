"""Registers the ``.xl`` polars expression namespace as a thin façade.

**This module contains no logic.** Every method is a one-line delegation to a plain typed
function in ``kedge.xl.rounding``, ``kedge.xl.nulls``, ``kedge.xl.dates`` or
``kedge.xl.errors``. Those functions are the real, tested, importable surface; this class
is sugar over them.

That split is deliberate, and it is worth recording why, because the obvious design is to
put the implementation in the namespace class.

A sibling project in the same estate did exactly that -- a large registered namespace that
grew to a few thousand lines with only a handful of production call sites, a long tail of
methods nobody called, and one cost that is easy to underestimate: because a namespace
attribute is attached at runtime by a decorator, no static type checker can see it, and
supporting it meant disabling ``unresolved-attribute`` and several sibling rules across the
whole project. Namespace registration is now banned there. Keeping the logic in plain
functions means kedge can adopt the same position later by deleting this one file: the
tests, the call sites that use ``.pipe(...)``, and the whole implementation survive
untouched. A rewrite becomes a removal.

What the namespace buys in the meantime is real, though, and specific to this project:
generated notebook code is read by a human reviewer, and ``col("amount").xl.round(2)``
states its Excel-compatibility intent at the point of use in a way that
``round_half_away(col("amount"), 2)`` does not quite manage. PLAN 2.6 asks for exactly
that greppability.

**Type checking.** ``pl.col("x").xl.round(2)`` will raise ``unresolved-attribute`` under
``ty``. The recommendation is *not* to disable that rule: hand-written kedge code should
call the plain functions, optionally through ``.pipe()``, and reserve ``.xl.*`` for
generated notebook cells, which ``ty`` never sees. Where hand-written code does want the
sugar, silence it per call site with ``# ty: ignore[unresolved-attribute]`` so the cost
stays visible.
"""

from __future__ import annotations

import polars as pl

from kedge.xl.dates import date_to_serial, serial_to_date, serial_to_datetime
from kedge.xl.errors import iferror, is_error
from kedge.xl.nulls import Operand, add, div, empty_as_zero, mul, sub, to_number
from kedge.xl.rounding import round_down, round_half_away, round_up

__all__ = ["ExcelExpr"]


@pl.api.register_expr_namespace("xl")
class ExcelExpr:
    """Excel-compatible operations on a polars expression, reached through ``.xl``.

    Registered on import of ``kedge.xl``. Holds nothing but the expression it wraps -- no
    configuration, no state -- so that every call is a pure function of its arguments and
    the namespace can be removed without consequence.

    Example:
        >>> import polars as pl
        >>> import kedge.xl  # registers `.xl` as an import side effect
        >>> frame = pl.LazyFrame({"amount": [2.5], "serial": [45000], "text": ["1,234"]})
        >>> frame.with_columns(
        ...     rounded=pl.col("amount").xl.round(0),
        ...     as_date=pl.col("serial").xl.serial_to_date(),
        ...     as_number=pl.col("text").xl.to_number(),
        ... ).collect().to_dicts()
        [{'amount': 2.5, 'serial': 45000, 'text': '1,234', 'rounded': 3.0, 'as_date': datetime.date(2023, 3, 15), 'as_number': 1234.0}]
    """

    __slots__ = ("_expr",)

    def __init__(self, expr: pl.Expr) -> None:
        self._expr = expr

    # ── rounding ────────────────────────────────────────────────────────────────────

    def round(self, digits: int = 0) -> pl.Expr:
        """Excel ``ROUND``: half away from zero. See ``kedge.xl.round_half_away``."""
        return round_half_away(self._expr, digits)

    def roundup(self, digits: int = 0) -> pl.Expr:
        """Excel ``ROUNDUP``: away from zero. See ``kedge.xl.round_up``."""
        return round_up(self._expr, digits)

    def rounddown(self, digits: int = 0) -> pl.Expr:
        """Excel ``ROUNDDOWN``: toward zero. See ``kedge.xl.round_down``."""
        return round_down(self._expr, digits)

    # ── arithmetic with empty cells ─────────────────────────────────────────────────

    def empty_as_zero(self) -> pl.Expr:
        """Treat nulls as zero. See ``kedge.xl.empty_as_zero``."""
        return empty_as_zero(self._expr)

    def add(self, other: Operand) -> pl.Expr:
        """Excel ``=A1+B1``: nulls as zero. See ``kedge.xl.add``."""
        return add(self._expr, other)

    def sub(self, other: Operand) -> pl.Expr:
        """Excel ``=A1-B1``: nulls as zero. See ``kedge.xl.sub``."""
        return sub(self._expr, other)

    def mul(self, other: Operand) -> pl.Expr:
        """Excel ``=A1*B1``: nulls as zero, so a null operand gives zero. See ``kedge.xl.mul``."""
        return mul(self._expr, other)

    def div(self, other: Operand) -> pl.Expr:
        """Excel ``=A1/B1``: null instead of ``#DIV/0!``, never ``inf``. See ``kedge.xl.div``."""
        return div(self._expr, other)

    # ── coercion ────────────────────────────────────────────────────────────────────

    def to_number(self) -> pl.Expr:
        """Coerce text-formatted numbers as Excel does. See ``kedge.xl.to_number``."""
        return to_number(self._expr)

    # ── dates ───────────────────────────────────────────────────────────────────────

    def serial_to_date(self) -> pl.Expr:
        """Excel 1900 serial to ``Date``, leap bug included. See ``kedge.xl.serial_to_date``."""
        return serial_to_date(self._expr)

    def serial_to_datetime(self) -> pl.Expr:
        """Fractional serial to ``Datetime``. See ``kedge.xl.serial_to_datetime``."""
        return serial_to_datetime(self._expr)

    def date_to_serial(self) -> pl.Expr:
        """``Date`` to Excel 1900 serial. See ``kedge.xl.date_to_serial``."""
        return date_to_serial(self._expr)

    # ── error values ────────────────────────────────────────────────────────────────

    def is_error(self) -> pl.Expr:
        """True where a value stands in for an Excel error. See ``kedge.xl.is_error``."""
        return is_error(self._expr)

    def iferror(self, fallback: pl.Expr | int | float | str | None) -> pl.Expr:
        """Excel ``IFERROR``. Read the warning in ``kedge.xl.iferror`` before using it."""
        return iferror(self._expr, fallback)
