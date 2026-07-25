"""Money helpers, where rounding is a correctness concern rather than a presentation one.

Both functions here defer to :mod:`kedge.xl` for the actual rounding, so the Excel-compatible
behaviour is stated in exactly one place and stays greppable.

References:
- PLAN.md §2.6 (Excel rounding is half away from zero, and collapses to 15 significant digits).
"""

from __future__ import annotations

import logging

import polars as pl

import kedge.xl  # noqa: F401  -- importing registers the .xl expression namespace

logger = logging.getLogger(__name__)

__all__ = ["allocate_residual", "to_thousands"]


def to_thousands(column: str | pl.Expr, *, digits: int = 0) -> pl.Expr:
    """Convert a monetary column to thousands, rounded the way Excel would round it.

    Args:
        column: Column name or expression, in base currency units.
        digits: Decimal places to keep after dividing by 1000.

    Returns:
        An expression giving the value in thousands.

    Example:
        df.with_columns(to_thousands("ead_gbp").alias("ead_gbp_k"))
    """
    expr = pl.col(column) if isinstance(column, str) else column
    return (expr / 1000).xl.round(digits)


def allocate_residual(
    frame: pl.LazyFrame,
    *,
    column: str,
    total: float,
    digits: int = 2,
    into: str | None = None,
) -> pl.LazyFrame:
    """Round a column so that it still sums exactly to `total`, using largest remainder.

    Rounding each row independently and then summing gives a figure that disagrees with the
    rounded total by a few pence -- the classic "the parts do not add up to the whole" complaint
    on any allocated schedule. This distributes the residual one unit at a time to the rows with
    the largest discarded fraction, which is the convention Excel users reach for by hand.

    Args:
        frame: The frame to adjust.
        column: The unrounded numeric column to allocate.
        total: The target the rounded column must sum to exactly.
        digits: Decimal places to round to.
        into: Column to write. Defaults to overwriting `column`.

    Returns:
        The frame with the allocated column, summing exactly to `total` at `digits` places.

    Example:
        allocate_residual(schedule, column="charge", total=1_000_000.00)
    """
    target = into or column
    unit = 10.0**-digits
    step = round(10.0**digits)

    # Work in integer units of the last decimal place so the comparison is exact rather than
    # floating point, which is the whole point of the exercise.
    prepared = frame.with_columns(
        (pl.col(column) * step).floor().alias("_floor_units"),
        ((pl.col(column) * step) - (pl.col(column) * step).floor()).alias("_remainder"),
    )

    materialised = prepared.collect()
    if materialised.height == 0:
        return frame.with_columns(pl.col(column).alias(target)).drop(
            [c for c in ("_floor_units", "_remainder") if c in materialised.columns]
        )

    floor_units = materialised["_floor_units"].to_list()
    remainders = materialised["_remainder"].to_list()
    target_units = round(total * step)
    shortfall = target_units - sum(int(u) for u in floor_units)

    order = sorted(range(len(remainders)), key=lambda i: remainders[i], reverse=True)
    allocated = [int(u) for u in floor_units]
    for position in range(abs(shortfall)):
        index = order[position % len(order)]
        allocated[index] += 1 if shortfall > 0 else -1

    if abs(shortfall) > len(order):
        logger.warning(
            "residual of %d units spread over only %d rows; check the total is right",
            shortfall,
            len(order),
        )

    return (
        materialised.lazy()
        .with_columns(pl.Series(target, [u * unit for u in allocated]))
        .drop("_floor_units", "_remainder")
    )
