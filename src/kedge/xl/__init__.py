"""Excel semantics as polars expressions -- the ``kedge.xl`` layer from PLAN 2.6.

Excel and polars disagree, quietly, in about a dozen places. Rounding at ``.5``, empty
cells in arithmetic, division by zero, text-formatted numbers, and the 1900 date system all
produce a *plausible* number in polars that is not the number the workbook produced. None
of them raises. This package makes each of those choices explicit, so that a reviewer
reading generated notebook code can see exactly where Excel compatibility was assumed
rather than having to infer it from a ``.round()`` that happens to be wrong.

Two surfaces, same behaviour:

**Plain functions** -- the real implementation, typed, importable, composable, and what
hand-written kedge code should use::

    from kedge.xl import round_half_away
    frame.with_columns(round_half_away(pl.col("amount"), 2))
    frame.with_columns(pl.col("amount").pipe(round_half_away, 2))

**The ``.xl`` namespace** -- a thin façade over those functions, for generated notebook
code where reading at a glance matters most::

    frame.with_columns(pl.col("amount").xl.round(2))

.. warning::

   **Importing this package registers the ``.xl`` namespace as a side effect.** That is a
   footgun and it is worth being blunt about it: ``pl.col("x").xl`` raises
   ``AttributeError`` until something, somewhere, has executed ``import kedge.xl``. The
   registration happens when ``kedge.xl.namespace`` is imported, which this module does
   below. Generated notebook cells must therefore carry the import themselves -- and under
   marimo's single-definition rule exactly one cell may own it. Code that only uses the
   plain functions imports them normally and never depends on the side effect at all, which
   is a further reason to prefer them in library code.

Everything here is lazy-safe. These are expressions, not eager helpers, and they compose
inside ``LazyFrame.with_columns`` without forcing a collect.

Version tripwire: the unit tests assert Excel's documented outputs *and* assert polars' own
current behaviour where kedge works around it. A polars upgrade that changes either one
fails the suite rather than shifting a reconciliation by a penny.
"""

from __future__ import annotations

from kedge.xl.dates import (
    MAX_SERIAL,
    MIN_SERIAL,
    PHANTOM_LEAP_DAY_SERIAL,
    date_to_serial,
    serial_to_date,
    serial_to_datetime,
)
from kedge.xl.errors import (
    EXCEL_ERROR_LITERALS,
    ExcelArgumentError,
    KedgeXlError,
    error_text_to_null,
    iferror,
    is_error,
)

# Importing the namespace module is what executes the `@pl.api.register_expr_namespace`
# decorator. `ExcelExpr` is re-exported so the import is a named dependency rather than a
# bare side-effecting import that a tidy-up would delete.
from kedge.xl.namespace import ExcelExpr
from kedge.xl.nulls import (
    Operand,
    add,
    div,
    empty_as_zero,
    mul,
    sub,
    sum_horizontal,
    to_number,
)
from kedge.xl.rounding import MAX_DIGITS, round_down, round_half_away, round_up

__all__ = [
    "EXCEL_ERROR_LITERALS",
    "MAX_DIGITS",
    "MAX_SERIAL",
    "MIN_SERIAL",
    "PHANTOM_LEAP_DAY_SERIAL",
    "ExcelArgumentError",
    "ExcelExpr",
    "KedgeXlError",
    "Operand",
    "add",
    "date_to_serial",
    "div",
    "empty_as_zero",
    "error_text_to_null",
    "iferror",
    "is_error",
    "mul",
    "round_down",
    "round_half_away",
    "round_up",
    "serial_to_date",
    "serial_to_datetime",
    "sub",
    "sum_horizontal",
    "to_number",
]
