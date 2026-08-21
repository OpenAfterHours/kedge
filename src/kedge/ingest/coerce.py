"""Typing a hand-in that arrived as text, and saying so.

A grid copied off a clipboard is **all text**. Excel puts its *displayed* values there, so a
column formatted ``#,##0.00`` arrives as ``364,422.95`` -- comma included -- and polars types it
``String``. Everything downstream then fails in a way that points nowhere near the cause: the
column survives the filter, survives the join, survives ``empty_as_zero`` (which finds no nulls
to fill because the empties are empty *strings*), and finally raises ``arithmetic on dtypes str
and dyn float is not allowed`` from inside a forty-line query plan. In marimo's app mode the
error is not even displayed -- the cell simply vanishes along with everything below it.

So the reader types the column, and reports that it did. The reporting is not a courtesy: a tool
that silently rewrites the data it was handed is a tool nobody can reconcile against.

**What will not be coerced, and why each rule is here.** ``kedge.xl.to_number`` is deliberately
liberal, because when a translation asks for it the intent is explicit. Deciding to apply it
*unasked* is a different question, and these are the cases where the liberal answer destroys
information:

* **A leading zero before a significant digit.** ``00123`` is a cost centre, an account, a sort
  code -- an identifier that happens to be spelled in digits. Coercing it to ``123.0`` loses the
  zeros and silently breaks every join it takes part in.
* **More than 15 significant digits.** A 16-digit account number does not survive a round trip
  through ``Float64``. Excel has the same limit, which is why such things are text in the source
  system too.
* **Any value that does not parse.** One ``n/a`` in a column of numbers means the column is not
  numeric; it means somebody wrote a note in it. Coercing the rest and nulling that row would
  hide the note.
* **A column with nothing in it.** Nothing to conclude from, so nothing is concluded.

The rules are conservative on purpose. A column this refuses to coerce still reaches the notebook
as text, where an explicit ``.xl.to_number()`` is available and is a translation decision with a
person behind it -- which is the right place for a judgement call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import polars as pl

from kedge.xl.nulls import to_number
from kedge.xl.text import MAX_SIGNIFICANT_DIGITS, loses_information_as_a_number

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_SIGNIFICANT_DIGITS",
    "Coercion",
    "coerce_numeric_text",
]

SAMPLE_VALUES = 3
"""How many original values a report carries. Enough to recognise the column, not a data dump."""


@dataclass(frozen=True, slots=True)
class Coercion:
    """One column that arrived as text and was read as a number.

    Carries the samples because the note a user reads should let them recognise the column
    without opening the file: "``accrual_gbp`` was read as text (``364,422.95``, ``205,878.90``)
    and converted to numbers" is actionable, and "1 column coerced" is not.
    """

    column: str
    samples: tuple[str, ...] = ()
    rows: int = 0

    def note(self) -> str:
        """One line for the layout panel."""
        shown = ", ".join(f"`{value}`" for value in self.samples)
        return (
            f"read `{self.column}` as text and converted it to numbers "
            f"({self.rows:,} values; e.g. {shown})"
        )


def coerce_numeric_text(
    frame: pl.DataFrame, *, skip: Sequence[str] = ()
) -> tuple[pl.DataFrame, tuple[Coercion, ...]]:
    """Convert text columns that are unambiguously numeric, and report which.

    Args:
        frame: The hand-in as read.
        skip: Columns to leave alone whatever they look like. This is how a contract that
            declares a column ``string`` keeps it that way -- the agreement outranks the guess.

    Returns:
        ``(frame, coercions)``. The frame is unchanged where nothing qualified, and
        ``coercions`` is empty in that case rather than absent, so a caller never has to
        distinguish "did not look" from "looked and found nothing".

    Example:
        >>> frame = pl.DataFrame({"amount": ["1,234.56", "", "89.00"], "id": ["00123", "1", "2"]})
        >>> typed, found = coerce_numeric_text(frame)
        >>> typed["amount"].to_list(), [item.column for item in found]
        ([1234.56, None, 89.0], ['amount'])
    """
    excluded = set(skip)
    coercions: list[Coercion] = []
    expressions: list[pl.Expr] = []

    for name, dtype in frame.schema.items():
        if dtype != pl.String or name in excluded:
            continue
        values = frame[name].to_list()
        if not _is_numeric_text(values):
            continue
        populated = [str(value).strip() for value in values if _populated(value)]
        coercions.append(
            Coercion(
                column=name,
                samples=tuple(populated[:SAMPLE_VALUES]),
                rows=len(populated),
            )
        )
        expressions.append(to_number(pl.col(name)).alias(name))

    if not expressions:
        return frame, ()

    logger.info(
        "coerced %d text column(s) to numeric: %s",
        len(coercions),
        ", ".join(item.column for item in coercions),
    )
    return frame.with_columns(expressions), tuple(coercions)


def _populated(value: object) -> bool:
    return value is not None and str(value).strip() != ""


def _is_numeric_text(values: Sequence[object]) -> bool:
    """Whether every populated value is a number, and none of them loses anything by becoming one.

    Parsing is delegated to :func:`kedge.xl.to_number` rather than re-implemented, so a column
    the reader coerces and a column a translation coerces cannot disagree about what a number
    is. The extra rules are all about *information loss*, which is a question ``to_number`` is
    right not to ask when it has been asked for explicitly; they live in
    :func:`kedge.xl.loses_information_as_a_number` because the comparison engine has to apply
    exactly the same rule one cell at a time.
    """
    populated = [str(value).strip() for value in values if _populated(value)]
    if not populated:
        return False
    if any(loses_information_as_a_number(value) for value in populated):
        return False

    # One pass through the real parser: a column is numeric only if every populated value is.
    parsed = (
        pl.DataFrame({"value": populated})
        .select(to_number(pl.col("value")).alias("number"))["number"]
        .null_count()
    )
    return parsed == 0
