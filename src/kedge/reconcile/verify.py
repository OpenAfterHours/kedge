"""Checking that a change did what the notebook said it would.

:mod:`kedge.reconcile.compare` answers "does the Python reproduce the workbook's own numbers?"
by holding the notebook against Excel's cached values. This module answers a different question,
and it is the one a *runbook* raises: the notebook computed what the data should look like after
an adjustment, the user went away and applied that adjustment somewhere kedge cannot reach, and
came back with a fresh extract. **Does the extract match the prediction?**

That check is worth more than it looks. In the workbook this replaces, verification was somebody
putting two tabs side by side and eyeballing a total -- so an update that hit forty rows instead
of the intended thirty-eight, or landed a penny out on each of them, agreed on the total and was
signed off. Predicting per row before the change and comparing per row after it is a control the
spreadsheet never had, and it is available only because the prediction was written down first.

The invariant is :mod:`kedge.reconcile`'s, restated for this shape and just as load-bearing:
**a verification with nothing to compare is never a pass.** No rows, no keys, no shared columns,
keys that do not line up -- every one of those is ``NOT_RECONCILED`` with a reason, never
``PASSED``. A green panel here is a claim that somebody will act on: it says the production data
now holds what this notebook says it holds.

Four ways the answer can be "no", and they are kept apart because the remedies differ:

* **Breaks** -- the row is there and the value is wrong. The update did something other than
  what was predicted.
* **Missing** -- predicted, but absent from the re-extract. The update did not reach those rows,
  or the ``WHERE`` clause was narrower than the adjustment.
* **Unexpected** -- present in the re-extract, not predicted. Usually the ``WHERE`` clause was
  *wider* than the adjustment, which is the dangerous direction and the reason this is reported
  rather than ignored.
* **Duplicated** -- the key is not unique in one of the frames, so no row-wise comparison is
  meaningful at all. Reported as ``NOT_RECONCILED`` rather than guessed at, because a comparison
  against an arbitrary one of several matching rows is worse than no comparison.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import polars as pl
from pydantic import Field

from kedge.reconcile.model import (
    ReconciliationStatus,
    Tolerance,
    _Frozen,
    signed_delta,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_REPORTED",
    "RowBreak",
    "VerificationReport",
    "verify",
]

MAX_REPORTED = 50
"""How many breaks, missing keys and unexpected keys are carried in a report.

The counts are always exact; it is the itemised lists that are capped, and every report says so
in :meth:`VerificationReport.summary_line`. Fifty is what somebody will actually read before
deciding, and the alternative -- a report holding forty thousand break records -- is one that
gets truncated somewhere further downstream by something that does not mention it.
"""


class RowBreak(_Frozen):
    """One row where the re-extract disagrees with what was predicted."""

    key: tuple[Any, ...] = Field(description="The row's key values, in the declared key order.")
    column: str
    predicted: Any = None
    actual: Any = None
    delta: float | None = Field(
        default=None,
        description="actual - predicted, where both are numeric. None for text columns, where "
        "the difference is not a quantity and reporting one would invent a scale.",
    )

    def render(self) -> str:
        """One line for a panel: which row, which column, and by how much."""
        key = ", ".join(str(part) for part in self.key)
        if self.delta is None:
            return f"{key} | {self.column}: expected {self.predicted!r}, found {self.actual!r}"
        # `.12g` rather than `+`: subtracting two doubles that differ by five pence yields
        # 0.04999999999999982, and a break report that renders that has buried the finding in
        # the noise of the thing it is reporting.
        return (
            f"{key} | {self.column}: expected {self.predicted}, found {self.actual} "
            f"({self.delta:+.12g})"
        )


class VerificationReport(_Frozen):
    """What the re-extract proved, or why nothing was proved.

    ``status`` is falsy unless it is ``PASSED``, inherited from
    :class:`~kedge.reconcile.model.ReconciliationStatus`, so ``if report.status:`` cannot
    accidentally treat an unverifiable comparison as a success.
    """

    status: ReconciliationStatus
    keys: tuple[str, ...] = ()
    compared_columns: tuple[str, ...] = ()
    rows_predicted: int = 0
    rows_actual: int = 0
    matched: int = 0
    breaks: tuple[RowBreak, ...] = ()
    break_count: int = 0
    missing: tuple[tuple[Any, ...], ...] = ()
    missing_count: int = 0
    unexpected: tuple[tuple[Any, ...], ...] = ()
    unexpected_count: int = 0
    tolerance: Tolerance = Tolerance()
    reason: str | None = Field(
        default=None,
        description="Why nothing could be compared. Set exactly when the status is "
        "`not_reconciled`, and written to be read by somebody who has to fix it.",
    )

    @property
    def ok(self) -> bool:
        """Whether the re-extract matched the prediction, row for row."""
        return bool(self.status)

    def summary_line(self) -> str:
        """One sentence stating the claim, or stating that there is none."""
        if self.status is ReconciliationStatus.NOT_RECONCILED:
            return f"**Not verified.** {self.reason}"
        if self.ok:
            return (
                f"**Verified.** All {self.matched:,} rows match the prediction across "
                f"{len(self.compared_columns)} column(s), within {self.tolerance.describe()}."
            )
        parts = [f"{self.matched:,} of {self.rows_predicted:,} rows match"]
        if self.break_count:
            parts.append(f"{self.break_count:,} differ")
        if self.missing_count:
            parts.append(f"{self.missing_count:,} missing from the re-extract")
        if self.unexpected_count:
            parts.append(f"{self.unexpected_count:,} present but not predicted")
        return "**Not verified.** " + ", ".join(parts) + "."

    def render(self) -> str:
        """The whole report as markdown, for the notebook panel."""
        lines = ["### Verification", self.summary_line()]
        if self.breaks:
            lines.extend(["", "**Rows that differ**", ""])
            lines.extend(f"- {item.render()}" for item in self.breaks)
            if self.break_count > len(self.breaks):
                lines.append(f"- ...and {self.break_count - len(self.breaks):,} more.")
        for label, shown, total in (
            ("Missing from the re-extract", self.missing, self.missing_count),
            ("Present but not predicted", self.unexpected, self.unexpected_count),
        ):
            if not shown:
                continue
            lines.extend(["", f"**{label}**", ""])
            lines.extend(f"- {', '.join(str(part) for part in key)}" for key in shown)
            if total > len(shown):
                lines.append(f"- ...and {total - len(shown):,} more.")
        return "\n".join(lines)


def verify(
    predicted: pl.DataFrame | pl.LazyFrame,
    actual: pl.DataFrame | pl.LazyFrame,
    *,
    keys: Sequence[str],
    columns: Sequence[str] | None = None,
    tolerance: Tolerance | None = None,
) -> VerificationReport:
    """Compare a re-extract against what the notebook predicted, row by row.

    Args:
        predicted: What the data should look like after the change. Normally the frame a
            hand-off's statement was rendered from.
        actual: The re-extract, as a hand-in.
        keys: Columns identifying a row in both frames. Must be unique in both.
        columns: Value columns to compare. Defaults to every column both frames share that is
            not a key -- which is usually right, and is deliberately not "every column of
            ``predicted``": a re-extract that dropped a column should be reported as a missing
            column rather than as every row breaking.
        tolerance: How close counts as equal. Defaults to
            :class:`~kedge.reconcile.model.Tolerance`'s own, which is absolute and tight enough
            to catch a penny.

    Returns:
        The :class:`VerificationReport`. ``NOT_RECONCILED`` with a ``reason`` wherever a
        comparison could not honestly be made.

    Example:
        >>> before = pl.DataFrame({"id": ["A", "B"], "amount": [1.0, 2.0]})
        >>> verify(before, before, keys=["id"]).ok
        True
    """
    limits = tolerance or Tolerance()
    left = predicted.lazy().collect()
    right = actual.lazy().collect()
    key_list = list(keys)

    unverifiable = _why_not_verifiable(left, right, key_list)
    if unverifiable is not None:
        logger.info("verification not possible: %s", unverifiable)
        return VerificationReport(
            status=ReconciliationStatus.NOT_RECONCILED,
            keys=tuple(key_list),
            rows_predicted=left.height,
            rows_actual=right.height,
            tolerance=limits,
            reason=unverifiable,
        )

    value_columns = _value_columns(left, right, key_list, columns)
    if not value_columns:
        shared = sorted(set(left.columns) & set(right.columns))
        return VerificationReport(
            status=ReconciliationStatus.NOT_RECONCILED,
            keys=tuple(key_list),
            rows_predicted=left.height,
            rows_actual=right.height,
            tolerance=limits,
            reason=(
                f"the two frames share no value columns to compare. They have "
                f"{shared or 'no columns'} in common, and every one of those is a key. "
                f"Name the columns explicitly if the re-extract renamed them."
            ),
        )

    # Presence markers, added before the join. The obvious shortcut -- deciding a row is absent
    # because its first value column came back null -- is wrong on exactly the row this whole
    # module exists for: a re-extract that legitimately holds NULL is present *and* different,
    # and reading it as missing loses the break. Two explicit boolean columns cannot be
    # confused with data.
    here, there = _marker_names(left, right)
    joined = (
        left.with_columns(pl.lit(value=True).alias(here))
        .join(
            right.with_columns(pl.lit(value=True).alias(there)),
            on=key_list,
            how="full",
            suffix="__actual",
            coalesce=True,
        )
        .with_columns(
            pl.col(here).fill_null(value=False),
            pl.col(there).fill_null(value=False),
        )
    )

    missing_rows = joined.filter(pl.col(here) & ~pl.col(there))
    unexpected_rows = joined.filter(~pl.col(here) & pl.col(there))
    both = joined.filter(pl.col(here) & pl.col(there))

    breaks = _collect_breaks(both, key_list, value_columns, limits)
    matched = both.height - len({tuple(item.key) for item in breaks})
    status = (
        ReconciliationStatus.PASSED
        if not breaks and not missing_rows.height and not unexpected_rows.height and matched
        else ReconciliationStatus.FAILED
    )

    report = VerificationReport(
        status=status,
        keys=tuple(key_list),
        compared_columns=tuple(value_columns),
        rows_predicted=left.height,
        rows_actual=right.height,
        matched=matched,
        breaks=tuple(breaks[:MAX_REPORTED]),
        break_count=len(breaks),
        missing=_key_tuples(missing_rows, key_list),
        missing_count=missing_rows.height,
        unexpected=_key_tuples(unexpected_rows, key_list),
        unexpected_count=unexpected_rows.height,
        tolerance=limits,
    )
    logger.info("verification: %s", report.summary_line())
    return report


# =============================================================================
# INTERNALS
# =============================================================================


def _marker_names(left: pl.DataFrame, right: pl.DataFrame) -> tuple[str, str]:
    """Two column names neither frame already uses.

    A hand-in is somebody's extract and may hold a column called anything at all, including the
    obvious choice. Colliding would silently overwrite real data with a boolean.
    """
    taken = set(left.columns) | set(right.columns)
    names = []
    for stem in ("_kedge_predicted", "_kedge_actual"):
        name = stem
        suffix = 2
        while name in taken:
            name = f"{stem}_{suffix}"
            suffix += 1
        taken.add(name)
        names.append(name)
    return names[0], names[1]


def _why_not_verifiable(left: pl.DataFrame, right: pl.DataFrame, keys: list[str]) -> str | None:
    """The reason no honest comparison can be made, or ``None`` if one can.

    Every branch here is a case where the tempting thing to do is compare anyway and report
    something. Each would produce a number that looks like a result and is not one.
    """
    if not keys:
        return (
            "no key columns were given, so there is no way to say which predicted row "
            "corresponds to which re-extracted one. Name the columns that identify a row."
        )
    for label, frame in (("prediction", left), ("re-extract", right)):
        absent = [key for key in keys if key not in frame.columns]
        if absent:
            return (
                f"the {label} has no column(s) {', '.join(absent)}, which the keys name. "
                f"It holds: {', '.join(frame.columns) or 'nothing'}."
            )
        if frame.height == 0:
            return f"the {label} is empty, so there is nothing to compare."
        duplicates = frame.height - frame.select(keys).unique().height
        if duplicates:
            return (
                f"the {label} has {duplicates:,} duplicate key(s) on "
                f"{', '.join(keys)}, so no row-wise comparison is meaningful. Comparing "
                f"against an arbitrary one of several matching rows would be worse than not "
                f"comparing at all."
            )
    return None


def _value_columns(
    left: pl.DataFrame,
    right: pl.DataFrame,
    keys: list[str],
    requested: Sequence[str] | None,
) -> list[str]:
    """The columns to compare: those asked for, or every non-key column both frames hold."""
    if requested is not None:
        return [column for column in requested if column not in keys]
    shared = [column for column in left.columns if column in right.columns]
    return [column for column in shared if column not in keys]


def _collect_breaks(
    joined: pl.DataFrame,
    keys: list[str],
    columns: list[str],
    tolerance: Tolerance,
) -> list[RowBreak]:
    """Every column of every matched row that disagrees, in frame order."""
    breaks: list[RowBreak] = []
    for row in joined.iter_rows(named=True):
        key = tuple(row[name] for name in keys)
        for column in columns:
            expected = row.get(column)
            found = row.get(f"{column}__actual")
            if _agrees(expected, found, tolerance):
                continue
            breaks.append(
                RowBreak(
                    key=key,
                    column=column,
                    predicted=expected,
                    actual=found,
                    delta=signed_delta(expected, found) if _both_numeric(expected, found) else None,
                )
            )
    return breaks


def _agrees(expected: Any, found: Any, tolerance: Tolerance) -> bool:
    """Whether two values match, numerically within tolerance or exactly otherwise.

    Two nulls agree. One null and a value do not, and that is deliberate: a re-extract that
    nulled a column is a break, not a match, however tempting it is to treat missing as equal.
    """
    if expected is None and found is None:
        return True
    if expected is None or found is None:
        return False
    if _both_numeric(expected, found):
        return tolerance.matches(float(expected), float(found))
    return bool(expected == found)


def _both_numeric(expected: Any, found: Any) -> bool:
    return isinstance(expected, int | float) and isinstance(found, int | float)


def _key_tuples(frame: pl.DataFrame, keys: list[str]) -> tuple[tuple[Any, ...], ...]:
    """The first :data:`MAX_REPORTED` key tuples, for the itemised part of a report."""
    return tuple(tuple(row) for row in frame.select(keys).head(MAX_REPORTED).iter_rows())
