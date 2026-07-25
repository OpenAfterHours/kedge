"""Excel error-value semantics for the ``kedge.xl`` namespace.

Excel has a family of error values -- ``#DIV/0!``, ``#N/A``, ``#VALUE!``, ``#NUM!``,
``#REF!``, ``#NAME?`` and ``#NULL!`` -- which propagate visibly through a worksheet and
which a human notices immediately. polars has no equivalent: a bad computation yields
``inf``, ``NaN`` or a null, and only the null is safe. ``inf`` and ``NaN`` poison every
downstream aggregate silently, which is exactly the failure mode PLAN 2.6 warns about.

kedge therefore represents **every Excel error value as a null**. That is a deliberate
narrowing: the distinction between ``#DIV/0!`` and ``#N/A`` is lost, but the value stays
inside polars' well-defined null semantics, aggregates keep working, and nothing is
silently wrong. Where the distinction matters, the analyser reads the cached error string
from the workbook rather than inferring it from a computed column.

The two directions this module covers:

* ``is_error`` / ``iferror`` -- detect and substitute the null-or-non-finite values that
  stand in for Excel errors in a *computed* column.
* ``error_text_to_null`` -- normalise the error *strings* that ``openpyxl``'s
  ``data_only=True`` load returns for cells Excel last evaluated to an error. Those arrive
  as the literal text ``"#DIV/0!"`` in an otherwise numeric column, and casting them
  naively produces a null anyway -- but silently, and only if the cast happens to be
  non-strict. Doing it explicitly keeps the intent greppable.
"""

from __future__ import annotations

from typing import Final

import polars as pl

__all__ = [
    "EXCEL_ERROR_LITERALS",
    "ExcelArgumentError",
    "KedgeXlError",
    "error_text_to_null",
    "iferror",
    "is_error",
]


# NOTE: CONVENTIONS.md requires exceptions to inherit from `kedge.errors.KedgeError`.
# That module is owned by another workstream and does not exist yet, so `KedgeXlError`
# is currently a bare `Exception`. Re-parent it onto `KedgeError` when `kedge/errors.py`
# lands -- it is a one-line change and no call site needs to move.
class KedgeXlError(Exception):
    """Base exception for the Excel-semantics layer."""


class ExcelArgumentError(KedgeXlError, ValueError):
    """Raised when an argument cannot express a meaningful Excel operation.

    Inherits from ``ValueError`` as well so that callers who reasonably expect a
    ``ValueError`` from a bad argument are not surprised.
    """


#: The error values Excel writes into a workbook's cached-value cache. ``openpyxl``'s
#: ``data_only=True`` load surfaces these as plain strings, so a column of cached results
#: can be a numeric column with these mixed in.
EXCEL_ERROR_LITERALS: Final[frozenset[str]] = frozenset(
    {
        "#DIV/0!",
        "#N/A",
        "#NAME?",
        "#NULL!",
        "#NUM!",
        "#REF!",
        "#VALUE!",
        "#SPILL!",
        "#CALC!",
        "#GETTING_DATA",
    }
)


def is_error(expr: pl.Expr) -> pl.Expr:
    """Return a boolean expression that is true where a value stands in for an Excel error.

    A value counts as an error if it is null, ``NaN`` or infinite. The last two never
    occur in code that goes through ``kedge.xl``, but they do occur in code that does not,
    and treating them as errors is what stops a stray ``inf`` from reaching a ``SUM``.

    This is the explicit condition PLAN 2.7 asks for when translating ``IFERROR``: the
    reviewer can see what "error" was taken to mean rather than having it hidden inside a
    swallow-everything helper.

    Args:
        expr: A numeric expression. Non-float dtypes are cast to ``Float64`` first, so an
            integer column is simply tested for nulls.

    Returns:
        A ``Boolean`` expression, never null.
    """
    numeric = expr.cast(pl.Float64, strict=False)
    return numeric.is_null() | numeric.is_nan() | numeric.is_infinite()


def iferror(value: pl.Expr, fallback: pl.Expr | int | float | str | None) -> pl.Expr:
    """Reproduce Excel's ``IFERROR(value, fallback)``.

    Substitutes ``fallback`` wherever ``value`` is null, ``NaN`` or infinite (see
    ``is_error``), and returns ``value`` otherwise.

    Use this only where the source workbook genuinely contained an ``IFERROR``. PLAN 2.7
    is explicit that error handling should not be invented during translation: a wrapper
    added for tidiness converts a loud failure into a plausible number, which is precisely
    the outcome reconciliation exists to prevent. Excel's own ``IFERROR`` is frequently a
    bug being papered over, and the analyser raises a finding when it sees one.

    Args:
        value: The expression to test.
        fallback: The replacement. A non-expression is wrapped with ``pl.lit``; note that
            unlike most polars APIs a ``str`` here is a **literal**, not a column name,
            because ``IFERROR(x, "n/a")`` is overwhelmingly the intent.

    Returns:
        An expression of the supertype of ``value`` and ``fallback``.
    """
    replacement = fallback if isinstance(fallback, pl.Expr) else pl.lit(fallback)
    return pl.when(is_error(value)).then(replacement).otherwise(value)


def error_text_to_null(expr: pl.Expr) -> pl.Expr:
    """Replace Excel error strings such as ``"#DIV/0!"`` with null in a text column.

    Cached workbook values loaded with ``openpyxl(data_only=True)`` carry error results as
    literal strings. A column that is otherwise numeric therefore loads as ``String``, and
    the naive fix -- a non-strict cast -- also silently discards genuine unparseable text.
    Doing the substitution explicitly first means a later strict cast still fails loudly on
    data that is actually malformed.

    Args:
        expr: A ``String`` expression.

    Returns:
        A ``String`` expression with the error literals replaced by null.
    """
    # Sorted so the query plan is byte-identical between runs; a frozenset's iteration
    # order is not stable across interpreter sessions.
    return pl.when(expr.is_in(sorted(EXCEL_ERROR_LITERALS))).then(None).otherwise(expr)
