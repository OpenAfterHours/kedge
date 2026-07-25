"""Frame-shaping helpers for the messes that arrive from Excel.

Every function here exists because a real workbook did the thing it guards against: a header
three rows down, a totals row in the middle of the data, two columns called ``Amount``, a join
key that turned out not to be unique. Fixing these at the ingest boundary is what stops them
becoming a silently wrong number twelve cells later.

References:
- PLAN.md §2.6 (the semantics that differ), §2.8 (hand-in drift).
"""

from __future__ import annotations

import logging
import re

import polars as pl

logger = logging.getLogger(__name__)

__all__ = [
    "assert_unique_key",
    "drop_all_null_columns",
    "normalise_headers",
    "strip_totals_row",
]

_NON_WORD = re.compile(r"[^0-9a-z]+")


class DuplicateKeyError(ValueError):
    """A column expected to be unique is not, so a join on it would multiply rows."""


def normalise_headers(frame: pl.LazyFrame) -> pl.LazyFrame:
    """Lower-case, trim and snake_case every column name, de-duplicating collisions.

    Excel headers arrive with trailing spaces, mixed case, line breaks and repeats. Two columns
    genuinely called ``Amount`` become ``amount`` and ``amount_2`` rather than one silently
    shadowing the other.

    Args:
        frame: Any lazy frame, typically straight off a hand-in.

    Returns:
        The frame with cleaned, unique column names, in the original order.
    """
    seen: dict[str, int] = {}
    mapping: dict[str, str] = {}
    for original in frame.collect_schema().names():
        cleaned = _NON_WORD.sub("_", original.strip().lower()).strip("_") or "column"
        count = seen.get(cleaned, 0) + 1
        seen[cleaned] = count
        mapping[original] = cleaned if count == 1 else f"{cleaned}_{count}"
    return frame.rename(mapping)


def drop_all_null_columns(frame: pl.LazyFrame) -> pl.LazyFrame:
    """Drop columns that are entirely null, which Excel exports scatter between real ones.

    Args:
        frame: Any lazy frame.

    Returns:
        The frame without its empty columns. A frame with no empty columns is returned unchanged.
    """
    counts = frame.select(pl.all().null_count()).collect()
    height = frame.select(pl.len()).collect().item()
    empty = [name for name in counts.columns if counts[name].item() == height]
    if empty:
        logger.info("dropping %d all-null column(s): %s", len(empty), ", ".join(empty))
    return frame.drop(empty) if empty else frame


def strip_totals_row(
    frame: pl.LazyFrame, *, key: str, markers: tuple[str, ...] = ()
) -> pl.LazyFrame:
    """Remove totals rows, wherever they sit, by looking at the key column.

    Excel totals rows appear at the bottom, in the middle, or both, and they are indistinguishable
    from data to anything that only reads values. They are recognised here by a null key or a
    marker word in it -- summing a column that still contains its own total is a doubling that
    reconciles against nothing.

    Args:
        frame: Any lazy frame.
        key: The column that every genuine row must have a value in.
        markers: Extra case-insensitive strings that mark a totals row, e.g. ``("subtotal",)``.
            ``"total"`` and ``"grand total"`` are always matched.

    Returns:
        The frame without its totals rows.
    """
    words = {"total", "grand total", "totals", *(m.lower() for m in markers)}
    stripped = pl.col(key).cast(pl.String).str.strip_chars().str.to_lowercase()
    return frame.filter(pl.col(key).is_not_null() & ~stripped.is_in(list(words)))


def assert_unique_key(
    frame: pl.LazyFrame, keys: str | list[str], *, label: str = "frame"
) -> pl.LazyFrame:
    """Fail loudly if `keys` is not unique, before a join silently multiplies rows.

    An exact ``VLOOKUP`` takes the first match; a left join takes all of them. That difference is
    the most common way a translated workbook produces a bigger number than the original, and it
    produces no error at all -- so this check is worth its cost. It collects only the key columns.

    Args:
        frame: The frame about to be joined.
        keys: Column name, or list of column names, expected to be unique together.
        label: Name used in the error message, so the failure says which frame was wrong.

    Returns:
        The frame unchanged, so the call can sit inline in a pipeline.

    Raises:
        DuplicateKeyError: If any key value occurs more than once.
    """
    columns = [keys] if isinstance(keys, str) else list(keys)
    duplicates = (
        frame.select(columns)
        .group_by(columns)
        .agg(pl.len().alias("_n"))
        .filter(pl.col("_n") > 1)
        .sort("_n", descending=True)
        .head(5)
        .collect()
    )
    if duplicates.height:
        worst = duplicates.row(0)
        msg = (
            f"{label}: {', '.join(columns)} is not unique -- {duplicates.height}+ duplicated "
            f"value(s), worst is {worst[:-1]} appearing {worst[-1]} times. A left join on this "
            f"key would multiply rows; an Excel VLOOKUP would have taken only the first match."
        )
        raise DuplicateKeyError(msg)
    return frame
