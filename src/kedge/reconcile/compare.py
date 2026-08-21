"""The reconciliation loop: notebook output against the workbook's cached values.

PLAN 4.5, step by step. Read the resulting column from the notebook, compare it against the
values Excel cached for the corresponding range with a configurable absolute *and* relative
tolerance, and report rows matched, rows differing, worst absolute and relative delta, and
the first N mismatching rows with both values side by side.

Three things about this module are deliberate.

**Regions, not sheets.** Every comparison is scoped to one range against one column, so a
failure localises to "the capital charge column is out by a penny in 12 rows" rather than to
"the Calc sheet is red".

**A missing baseline is never a pass.** The status enum is falsy unless it is ``PASSED``, a
``RegionResult`` refuses to be constructed as ``PASSED`` without compared rows, and this
module never invents an expected value: a ``None`` in the cached-value view is counted as a
missing baseline and the region degrades to ``NOT_RECONCILED`` (PLAN 1.5, 6.2).

**The tolerance travels with the claim.** It is recorded on every region and on the report,
because a reconciliation that passed at ``1e-2`` is a different statement from one that
passed at ``1e-9`` (PLAN 4.5).

The engine takes vectors, so it is testable with plain Python lists — no marimo, no kernel,
no workbook. The orchestration on top of it reads the workbook through
:mod:`kedge.reconcile.baseline` and the notebook through :func:`notebook_definitions`.

**How the notebook's values are obtained, and why.** ``kedge reconcile notebook.py
process.xlsx`` executes the notebook file through marimo's public ``App.run()`` and reads the
definitions it returns. Not the live kernel: the CLI path must work with no server running,
no session id and no model, which is the whole point of PLAN 7 step 5 ("standalone and before
any agent work"). ``App.run()`` is public API, where the live-kernel route goes through
``marimo._code_mode``, which is private and confined to ``notebook/driver.py`` by
CONVENTIONS non-negotiable 2. The agent loop, which does hold a live kernel, calls
:func:`reconcile_values` with the values it already probed.

References:
- PLAN.md 4.5 (the loop), 1.5 (the cached-value caveat), 2.6 (the semantics table), 7 step 5.
"""

from __future__ import annotations

import importlib.util
import logging
import math
import sys
from datetime import date, datetime, time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from kedge.analysis.values import ERROR_VALUES
from kedge.analysis.workbook import WorkbookOpenError, open_workbook
from kedge.errors import ReconciliationError
from kedge.reconcile.baseline import (
    BaselineVector,
    infer_regions,
    read_baselines,
    region_cell_count,
    specs_from_mapping,
)
from kedge.reconcile.diagnose import diagnose
from kedge.reconcile.model import (
    Mismatch,
    MismatchKind,
    NotReconciledReason,
    ReconciliationReport,
    ReconciliationStatus,
    RegionResult,
    RegionSpec,
    Tolerance,
    as_numeric_pair,
    signed_delta,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from kedge.analysis.model import WorkbookAnalysis
    from kedge.config import ReconciliationConfig

logger = logging.getLogger(__name__)

__all__ = [
    "DECLARED_REGIONS_NAME",
    "Comparison",
    "NotebookRunError",
    "compare_vectors",
    "declared_regions",
    "find_actual",
    "notebook_definitions",
    "reconcile_notebook",
    "reconcile_region",
    "reconcile_values",
    "reconcile_workbook",
    "resolve_actual",
    "to_vector",
]

#: The notebook variable a notebook uses to say which of its columns correspond to which
#: workbook ranges. A list of :class:`RegionSpec` or of plain dicts, so a notebook can declare
#: its regions without importing kedge at all.
DECLARED_REGIONS_NAME = "KEDGE_RECONCILE"

#: How many mismatching rows are retained for the diagnostics. The report prints far fewer,
#: but a rule that sees four rows out of four hundred cannot tell a systematic offset from a
#: coincidence, so the diagnostics get a much larger sample than the reader does.
DIAGNOSTIC_SAMPLE = 2_000

_DEFAULT_MISMATCH_ROWS = 20

#: How a report's regions were arrived at. "declared" is the notebook saying so itself,
#: "inferred" is kedge matching workbook headers to notebook columns, "provided" is a caller
#: passing specs in, and "none" means no regions were established at all.
SpecSource = Literal["declared", "inferred", "provided", "none"]


class NotebookRunError(ReconciliationError):
    """The notebook could not be executed, so it produced no values to reconcile."""


# =============================================================================
# VALUE COERCION
# =============================================================================


def _is_number(value: Any) -> bool:
    """Whether a value is a real number. ``bool`` subclasses ``int`` and is not one."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _as_datetime(value: Any) -> datetime | None:
    """Normalise a date or datetime to a naive datetime, or return None."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo is not None else value
    if isinstance(value, date):
        return datetime.combine(value, time())
    return None


def to_vector(value: Any, *, column: str | None = None) -> list[Any] | None:
    """Coerce whatever the notebook produced into a flat list of values.

    Duck-typed rather than ``isinstance``-checked against polars, so the engine can be
    exercised with plain lists and so a frame arriving from a kernel probe as some other
    object still works.

    Args:
        value: A polars ``Series``, ``DataFrame`` or ``LazyFrame``, a list or tuple, or a
            single scalar — a one-cell region such as a totals row is a legitimate region.
        column: Which column to take, when the value is a frame.

    Returns:
        The values in order, or None when the value cannot be read as a vector — a frame
        with several columns and no column named, for instance.
    """
    if value is None:
        return None

    collect = getattr(value, "collect", None)
    if callable(collect) and hasattr(value, "collect_schema"):  # LazyFrame
        value = collect()

    if hasattr(value, "get_column") and hasattr(value, "columns"):  # DataFrame
        names = list(value.columns)
        if column and column in names:
            return list(value.get_column(column).to_list())
        if len(names) == 1:
            return list(value.get_column(names[0]).to_list())
        logger.warning(
            "frame has %d columns and none was named %r; cannot reconcile it as a vector",
            len(names),
            column,
        )
        return None

    to_list = getattr(value, "to_list", None)
    if callable(to_list):  # Series
        return list(to_list())
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, (str, bytes)):
        return [value]
    if _is_number(value) or isinstance(value, (bool, date, datetime)):
        return [value]

    tolist = getattr(value, "tolist", None)
    if callable(tolist):  # anything numpy-shaped
        return list(tolist())
    logger.warning("cannot read a vector out of a %s", type(value).__name__)
    return None


def resolve_actual(definitions: Mapping[str, Any], spec: RegionSpec) -> list[Any] | None:
    """Find the values for one region among a notebook's definitions.

    Args:
        definitions: Variable name to value, as returned by :func:`notebook_definitions` or
            probed from a live kernel.
        spec: The region. ``variable`` names the frame or series; ``column`` names the column
            within it. With no ``variable``, every frame is searched for ``column``.

    Returns:
        The values, or None when nothing in the notebook corresponds to the region.
    """
    return find_actual(definitions, spec)[1]


def find_actual(
    definitions: Mapping[str, Any], spec: RegionSpec
) -> tuple[str | None, list[Any] | None]:
    """Like :func:`resolve_actual`, but also says which variable the values came from.

    Where the region names no variable, several frames can carry a column of the same name
    and picking the wrong one produces a confident, wrong answer -- a lookup table's
    six-row ``haircut`` column reconciled against a five-hundred-row region, say. So the
    candidate whose row count matches the region's extent wins; only when none matches does
    definition order decide, and then the row-count mismatch shows up in the report anyway.

    Args:
        definitions: Variable name to value.
        spec: The region.

    Returns:
        ``(variable_name, values)``, either of which is None when nothing matched. The name
        is what the report shows, so a reader can see which frame was actually compared.
    """
    if spec.variable:
        held = definitions.get(spec.variable)
        if held is None:
            logger.debug("region %r: notebook defines no %r", spec.id, spec.variable)
            return None, None
        return spec.variable, to_vector(held, column=spec.column)

    if not spec.column:
        return None, None

    wanted = region_cell_count(spec)
    fallback: str | None = None
    for name, held in definitions.items():
        if name.startswith("_") or spec.column not in _column_names(held):
            continue
        if wanted is not None and _row_count(held) == wanted:
            logger.debug("region %r: matched column %r in %r by extent", spec.id, spec.column, name)
            return name, to_vector(held, column=spec.column)
        if fallback is None:
            fallback = name

    if fallback is None:
        return None, None
    logger.debug(
        "region %r: no frame has %d rows; falling back to %r", spec.id, wanted or -1, fallback
    )
    return fallback, to_vector(definitions[fallback], column=spec.column)


def _row_count(value: Any) -> int | None:
    """The row count of a frame-shaped object without materialising it, or None."""
    height = getattr(value, "height", None)
    if isinstance(height, int):
        return height
    if isinstance(value, (list, tuple)):
        return len(value)
    length = getattr(value, "len", None)
    if callable(length) and hasattr(value, "to_list"):  # Series
        try:
            return int(length())
        except Exception:  # a broad catch: an unmeasurable object simply has no known height
            return None
    return None


def _column_names(value: Any) -> tuple[str, ...]:
    """The column names of a frame-shaped object, or an empty tuple."""
    if hasattr(value, "collect_schema") and not hasattr(value, "get_column"):
        try:
            return tuple(value.collect_schema().names())
        except Exception:  # a broad catch: a frame we cannot introspect is simply not a match
            return ()
    columns = getattr(value, "columns", None)
    if isinstance(columns, (list, tuple)):
        return tuple(str(name) for name in columns)
    return ()


# =============================================================================
# THE ENGINE
# =============================================================================


class Comparison:
    """The arithmetic outcome of comparing two vectors, before it becomes a report.

    Internal to the engine, and mutable while it accumulates, which is why it is a plain
    class rather than one of the frozen models in :mod:`kedge.reconcile.model`.

    Example:
        >>> comparison = compare_vectors([1.0, 2.0], [1.0, 2.0], tolerance=Tolerance())
        >>> comparison.rows_matched, comparison.rows_differing
        (2, 0)
    """

    __slots__ = (
        "mismatches",
        "rows_actual",
        "rows_compared",
        "rows_differing",
        "rows_expected",
        "rows_matched",
        "rows_missing_baseline",
        "worst_absolute_delta",
        "worst_at",
        "worst_relative_delta",
    )

    def __init__(self, rows_expected: int, rows_actual: int) -> None:
        self.rows_expected = rows_expected
        self.rows_actual = rows_actual
        self.rows_compared = 0
        self.rows_matched = 0
        self.rows_differing = 0
        self.rows_missing_baseline = 0
        self.worst_absolute_delta: float | None = None
        self.worst_relative_delta: float | None = None
        self.worst_at: str | None = None
        self.mismatches: list[Mismatch] = []

    def _record_delta(self, absolute: float | None, relative: float | None, where: str) -> None:
        if absolute is not None and (
            self.worst_absolute_delta is None or absolute > self.worst_absolute_delta
        ):
            self.worst_absolute_delta = absolute
            self.worst_at = where
        if relative is not None and (
            self.worst_relative_delta is None or relative > self.worst_relative_delta
        ):
            self.worst_relative_delta = relative


def compare_vectors(
    expected: Sequence[Any],
    actual: Sequence[Any],
    *,
    tolerance: Tolerance,
    cells: Sequence[str] = (),
    sample_limit: int = DIAGNOSTIC_SAMPLE,
) -> Comparison:
    """Compare a workbook vector against a notebook vector, row by row.

    Args:
        expected: The values Excel cached. A ``None`` is a missing baseline, never a zero.
        actual: The values the notebook produced.
        tolerance: The absolute and relative tolerances. A row matches within either.
        cells: Sheet-qualified A1 references, one per expected row, for the report.
        sample_limit: How many mismatches to retain.

    Returns:
        A :class:`Comparison`. Rows the two vectors do not share are counted as differing —
        a region whose row count is wrong has not reconciled, whatever the shared rows say.
    """
    comparison = Comparison(len(expected), len(actual))
    shared = min(len(expected), len(actual))

    for index in range(shared):
        baseline_value = expected[index]
        where = cells[index] if index < len(cells) else f"row {index + 1}"
        if baseline_value is None:
            comparison.rows_missing_baseline += 1
            continue

        comparison.rows_compared += 1
        mismatch, absolute, relative = _compare_cell(
            index + 1,
            cells[index] if index < len(cells) else None,
            baseline_value,
            actual[index],
            tolerance,
        )
        comparison._record_delta(absolute, relative, where)
        if mismatch is None:
            comparison.rows_matched += 1
            continue
        comparison.rows_differing += 1
        if len(comparison.mismatches) < sample_limit:
            comparison.mismatches.append(mismatch)

    for index in range(shared, len(expected)):
        if expected[index] is None:
            comparison.rows_missing_baseline += 1
            continue
        comparison.rows_differing += 1
        if len(comparison.mismatches) < sample_limit:
            comparison.mismatches.append(
                Mismatch(
                    row=index + 1,
                    cell=cells[index] if index < len(cells) else None,
                    kind=MismatchKind.MISSING_ROW,
                    expected=expected[index],
                    actual=None,
                )
            )

    for index in range(shared, len(actual)):
        comparison.rows_differing += 1
        if len(comparison.mismatches) < sample_limit:
            comparison.mismatches.append(
                Mismatch(
                    row=index + 1,
                    kind=MismatchKind.EXTRA_ROW,
                    expected=None,
                    actual=actual[index],
                )
            )

    return comparison


def _compare_cell(
    row: int,
    cell: str | None,
    expected: Any,
    actual: Any,
    tolerance: Tolerance,
) -> tuple[Mismatch | None, float | None, float | None]:
    """Compare one pair of values.

    Returns the mismatch (or None when they agree) and the absolute and relative deltas,
    which are reported for matching rows too: the worst delta in a passing region is how a
    reader judges whether the tolerance was doing any work.
    """

    def _mismatch(
        kind: MismatchKind, absolute: float | None = None, relative: float | None = None
    ) -> Mismatch:
        return Mismatch(
            row=row,
            cell=cell,
            kind=kind,
            expected=expected,
            actual=actual,
            absolute_delta=absolute,
            relative_delta=relative,
        )

    if isinstance(expected, str) and expected in ERROR_VALUES:
        return _mismatch(MismatchKind.EXCEL_ERROR), None, None
    if actual is None:
        return _mismatch(MismatchKind.ACTUAL_NULL), None, None

    if isinstance(expected, bool) or isinstance(actual, bool):
        if isinstance(expected, bool) and isinstance(actual, bool):
            return (
                (None if expected == actual else _mismatch(MismatchKind.VALUE_DIFFERS)),
                None,
                None,
            )
        return _mismatch(MismatchKind.TYPE_DIFFERS), None, None

    numbers = as_numeric_pair(expected, actual)
    if numbers is not None:
        expected_number, actual_number = numbers
        if not math.isfinite(actual_number):
            return _mismatch(MismatchKind.NON_FINITE), None, None
        delta = abs(actual_number - expected_number)
        relative = delta / abs(expected_number) if expected_number != 0.0 else None
        if tolerance.matches(expected_number, actual_number):
            return None, delta, relative
        return _mismatch(MismatchKind.VALUE_DIFFERS, delta, relative), delta, relative

    expected_dt = _as_datetime(expected)
    actual_dt = _as_datetime(actual)
    if expected_dt is not None and actual_dt is not None:
        days = signed_delta(expected, actual)
        delta = abs(days) if days is not None else None
        if expected_dt == actual_dt:
            return None, delta, None
        return _mismatch(MismatchKind.VALUE_DIFFERS, delta), delta, None

    if isinstance(expected, str) and isinstance(actual, str):
        return (None if expected == actual else _mismatch(MismatchKind.VALUE_DIFFERS)), None, None

    if type(expected) is not type(actual):
        return _mismatch(MismatchKind.TYPE_DIFFERS), None, None
    return (None if expected == actual else _mismatch(MismatchKind.VALUE_DIFFERS)), None, None


# =============================================================================
# REGIONS
# =============================================================================


def reconcile_region(
    spec: RegionSpec,
    baseline: BaselineVector | None,
    actual: Any,
    *,
    tolerance: Tolerance,
    max_mismatch_rows: int = _DEFAULT_MISMATCH_ROWS,
    not_reproduced: Mapping[str, str] | None = None,
    no_baseline: Mapping[str, str] | None = None,
) -> RegionResult:
    """Reconcile one region and explain the outcome.

    Args:
        spec: What was being compared.
        baseline: The workbook's cached values for the region, or None when the workbook
            could not be read at all.
        actual: The notebook's values: a frame, a series, a list or a scalar. Coerced to a
            vector here, so this is the one place that has to know what a frame is.
        tolerance: The tolerances to apply, recorded on the result.
        max_mismatch_rows: How many mismatching rows to print side by side.
        not_reproduced: Region id to why the notebook does not reproduce it. Honoured only
            where the notebook produced nothing, because that is what the claim means.
        no_baseline: Region id to why the workbook cannot be a baseline for it. Honoured only
            where the notebook *did* produce values -- see below.

    Returns:
        A :class:`~kedge.reconcile.model.RegionResult`. It is ``PASSED`` only when rows were
        actually compared and every one of them matched; anything else that cannot be
        checked is ``NOT_RECONCILED`` and says why.
    """
    actual = to_vector(actual, column=spec.column)
    common: dict[str, Any] = {
        "spec_id": spec.id,
        "reference": spec.reference,
        "label": spec.label,
        "variable": spec.variable,
        "column": spec.column,
        "tolerance": tolerance,
    }

    # "I compute this, and the workbook cannot check it" -- a fact about the spreadsheet, and
    # the only honest reading of a formula column whose cached cells are calculated empty
    # strings, or of a lookup that cached the text a paste left behind. Answered before the
    # baseline is looked at, because the declaration is a statement about that baseline; and
    # answered *after* `to_vector`, because it is only true if the notebook did produce the
    # values. Withhold them and it is a different, weaker claim -- one this deliberately will
    # not let a caller make by accident.
    #
    # The two declarations therefore cannot collide, however carelessly a caller fills the
    # maps in: this one applies only where values arrived, `not_reproduced` only where none
    # did, and each is the claim its own words make.
    declared_unusable = (no_baseline or {}).get(spec.id)
    if declared_unusable and actual is not None:
        reason = NotReconciledReason.NO_USABLE_BASELINE
        logger.info("region %r has no usable baseline: %s", spec.id, declared_unusable)
        return RegionResult(
            **common,
            status=ReconciliationStatus.NOT_RECONCILED,
            reason=reason,
            detail=f"{reason.explanation} {declared_unusable}",
            rows_expected=len(baseline.values) if baseline is not None else 0,
            rows_actual=len(actual),
        )

    if baseline is None or baseline.status in ("unreadable", "incomplete", "empty", "absent"):
        reason = (
            baseline.reason
            if baseline is not None and baseline.reason is not None
            else NotReconciledReason.BASELINE_RANGE_UNREADABLE
        )
        rows = len(baseline.values) if baseline is not None else 0
        logger.info("region %r not reconciled: %s", spec.id, reason.value)
        return RegionResult(
            **common,
            status=ReconciliationStatus.NOT_RECONCILED,
            reason=reason,
            detail=reason.explanation,
            rows_expected=rows,
            rows_actual=len(actual) if actual is not None else 0,
            rows_missing_baseline=rows,
        )

    if actual is None:
        # Two very different situations, and telling a user the wrong one costs them an hour.
        # "The notebook produced nothing for this, check your variable names" is right when a
        # cell failed to run; it is actively misleading when the notebook was never going to
        # reproduce the region -- a column of generated SQL the conversion improves on, say.
        # Either way it is not a pass, which is the invariant; what changes is whether the
        # reader goes looking for a bug.
        declared = (not_reproduced or {}).get(spec.id)
        reason = (
            NotReconciledReason.NOT_REPRODUCED if declared else NotReconciledReason.NO_ACTUAL_VALUES
        )
        return RegionResult(
            **common,
            status=ReconciliationStatus.NOT_RECONCILED,
            reason=reason,
            detail=f"{reason.explanation} {declared}" if declared else reason.explanation,
            rows_expected=len(baseline.values),
        )

    comparison = compare_vectors(
        baseline.values,
        actual,
        tolerance=tolerance,
        cells=[baseline.cell_at(i) or "" for i in range(len(baseline.values))],
    )

    counts: dict[str, Any] = {
        "rows_expected": comparison.rows_expected,
        "rows_actual": comparison.rows_actual,
        "rows_compared": comparison.rows_compared,
        "rows_matched": comparison.rows_matched,
        "rows_differing": comparison.rows_differing,
        "rows_missing_baseline": comparison.rows_missing_baseline,
        "worst_absolute_delta": comparison.worst_absolute_delta,
        "worst_relative_delta": comparison.worst_relative_delta,
        "worst_at": comparison.worst_at,
    }
    sample = comparison.mismatches[:max_mismatch_rows]

    if comparison.rows_compared == 0:
        return RegionResult(
            **common,
            **counts,
            status=ReconciliationStatus.NOT_RECONCILED,
            reason=NotReconciledReason.NO_CACHED_VALUES,
            detail=NotReconciledReason.NO_CACHED_VALUES.explanation,
            mismatches=sample,
            mismatches_truncated=comparison.rows_differing > len(sample),
        )

    if comparison.rows_differing > 0:
        diagnoses = diagnose(
            comparison.mismatches,
            rows_compared=comparison.rows_compared,
            rows_expected=comparison.rows_expected,
            rows_actual=comparison.rows_actual,
            column=spec.column,
        )
        logger.info(
            "region %r failed: %d of %d rows differ",
            spec.id,
            comparison.rows_differing,
            comparison.rows_compared,
        )
        return RegionResult(
            **common,
            **counts,
            status=ReconciliationStatus.FAILED,
            detail=_failure_detail(comparison, tolerance, baseline),
            mismatches=sample,
            mismatches_truncated=comparison.rows_differing > len(sample),
            diagnoses=diagnoses,
        )

    if comparison.rows_missing_baseline > 0 or baseline.truncated:
        reason = (
            NotReconciledReason.PARTIAL_CACHED_VALUES
            if comparison.rows_missing_baseline > 0
            else NotReconciledReason.BASELINE_TRUNCATED
        )
        detail = (
            f"{comparison.rows_matched} of {comparison.rows_compared} compared rows matched "
            f"within {tolerance.describe()}, but "
            + (
                f"{comparison.rows_missing_baseline} row(s) in {spec.reference} carry no "
                f"cached value. "
                if comparison.rows_missing_baseline > 0
                else f"the range {spec.reference} was longer than the read ceiling and was "
                f"cut short. "
            )
            + reason.explanation
        )
        return RegionResult(
            **common,
            **counts,
            status=ReconciliationStatus.NOT_RECONCILED,
            reason=reason,
            detail=detail,
            mismatches=sample,
        )

    return RegionResult(
        **common,
        **counts,
        status=ReconciliationStatus.PASSED,
        detail=(
            f"{comparison.rows_matched} of {comparison.rows_compared} rows matched within "
            f"{tolerance.describe()}"
            + (
                f"; worst absolute delta {comparison.worst_absolute_delta:g}"
                f" at {comparison.worst_at}"
                if comparison.worst_absolute_delta is not None
                else ""
            )
            + "."
        ),
    )


def _failure_detail(comparison: Comparison, tolerance: Tolerance, baseline: BaselineVector) -> str:
    """One paragraph a reader can act on, without reading the mismatch table."""
    parts = [
        f"{comparison.rows_differing} of {comparison.rows_compared} compared rows differ by "
        f"more than {tolerance.describe()}."
    ]
    if comparison.worst_absolute_delta is not None:
        parts.append(
            f"Worst absolute delta {comparison.worst_absolute_delta:g} at {comparison.worst_at}"
            + (
                f" (relative {comparison.worst_relative_delta:g})."
                if comparison.worst_relative_delta is not None
                else "."
            )
        )
    if comparison.rows_actual != comparison.rows_expected:
        parts.append(
            f"The notebook produced {comparison.rows_actual} rows against "
            f"{comparison.rows_expected} in the workbook range."
        )
    if comparison.rows_missing_baseline:
        parts.append(
            f"{comparison.rows_missing_baseline} further row(s) carry no cached value and "
            f"could not be checked at all."
        )
    if baseline.error_count:
        parts.append(f"{baseline.error_count} cell(s) in the range hold an Excel error value.")
    return " ".join(parts)


# =============================================================================
# REPORTS
# =============================================================================


def reconcile_values(
    specs: Iterable[RegionSpec],
    actuals: Mapping[str, Any],
    baselines: Mapping[str, BaselineVector],
    *,
    workbook: str,
    tolerance: Tolerance | None = None,
    max_mismatch_rows: int = _DEFAULT_MISMATCH_ROWS,
    notebook: str | None = None,
    workbook_sha256: str | None = None,
    analysis: WorkbookAnalysis | None = None,
    spec_source: SpecSource = "provided",
    notes: Sequence[str] = (),
    not_reproduced: Mapping[str, str] | None = None,
    no_baseline: Mapping[str, str] | None = None,
) -> ReconciliationReport:
    """Build a report from values already in hand.

    This is the entry point the agent loop uses: it has probed the kernel for the values
    already and does not want them fetched again.

    Args:
        specs: The regions to report on.
        actuals: Region id to the notebook's values for that region.
        baselines: Region id to the workbook's cached values for that region.
        workbook: Path or name of the workbook, for the report header.
        tolerance: Tolerances; the configured defaults when None.
        max_mismatch_rows: How many mismatching rows to print per region.
        notebook: Path of the notebook, when there was one.
        workbook_sha256: Content hash, so a report can be tied to a specific file.
        analysis: The workbook analysis, when the caller has one. Only its cached-value
            coverage is used, and it is what lets the report quote workbook-wide coverage.
            Without it the status is derived from the regions actually read and the coverage
            proportion is left None rather than guessed at.
        spec_source: How the regions were arrived at: declared, inferred, provided or none.
        notes: Caveats about the run itself.
        not_reproduced: Region id to why the notebook does not reproduce it.
        no_baseline: Region id to why the workbook cannot be a baseline for it. See
            :func:`reconcile_workbook` for the difference between the two.

    Returns:
        A complete :class:`~kedge.reconcile.model.ReconciliationReport`.
    """
    tolerance = tolerance or Tolerance()
    results = [
        reconcile_region(
            spec,
            baselines.get(spec.id),
            actuals.get(spec.id),
            tolerance=tolerance,
            max_mismatch_rows=max_mismatch_rows,
            not_reproduced=not_reproduced,
            no_baseline=no_baseline,
        )
        for spec in specs
    ]

    coverage = analysis.cached_values if analysis is not None else None
    report = ReconciliationReport(
        workbook=workbook,
        workbook_sha256=workbook_sha256,
        notebook=notebook,
        tolerance=tolerance,
        cached_value_status=coverage.status if coverage is not None else _derived_status(baselines),
        cached_value_coverage=coverage.coverage if coverage is not None else None,
        formula_cell_count=coverage.formula_cell_count if coverage is not None else None,
        spec_source=spec_source,
        regions=results,
        notes=list(notes),
    )
    logger.info(
        "reconciliation %s: %d region(s), %d rows compared",
        report.status.value,
        len(results),
        report.rows_compared,
    )
    return report


def _derived_status(
    baselines: Mapping[str, BaselineVector],
) -> Literal["present", "partial", "absent", "unknown"]:
    """State the cached-value position from the regions read, when no analysis was supplied.

    Strictly weaker than the analyser's workbook-wide coverage — it speaks only about the
    ranges this run looked at — but far more useful than "unknown", because the one question
    a reader has is whether there was a baseline at all.
    """
    if not baselines:
        return "unknown"
    statuses = {vector.status for vector in baselines.values()}
    if statuses <= {"absent", "empty", "unreadable"}:
        return "absent"
    if statuses == {"present"}:
        return "present"
    return "partial"


def reconcile_workbook(
    workbook_path: Path | str,
    actuals: Mapping[str, Any],
    *,
    specs: Sequence[RegionSpec] | None = None,
    tolerance: Tolerance | None = None,
    max_mismatch_rows: int = _DEFAULT_MISMATCH_ROWS,
    notebook: str | None = None,
    analysis: WorkbookAnalysis | None = None,
    spec_source: SpecSource | None = None,
    notes: Sequence[str] = (),
    not_reproduced: Mapping[str, str] | None = None,
    no_baseline: Mapping[str, str] | None = None,
) -> ReconciliationReport:
    """Read the workbook's baselines and reconcile the supplied values against them.

    Args:
        workbook_path: The workbook holding the cached expected values.
        actuals: Region id to values. Frames, series, lists and scalars are all accepted.
        specs: The regions. Inferred from the workbook analysis when None.
        tolerance: Tolerances; the configured defaults when None.
        max_mismatch_rows: How many mismatching rows to print per region.
        notebook: Path of the notebook, for the report header.
        analysis: A pre-computed analysis, if the caller has one.
        spec_source: How the regions were arrived at, when the caller knows better than this
            function can: a caller that inferred them itself passes ``"inferred"`` so the
            report does not claim they were declared.
        notes: Caveats about the run itself.
        not_reproduced: Region id to the reason the notebook does not reproduce it. Such a
            region is still ``NOT_RECONCILED`` -- nothing is claimed about it, and nothing here
            can make a region a pass -- but it is reported as a decision somebody made rather
            than as a cell that failed to run. The distinction matters: a conversion that
            deliberately improves on a workbook column, rather than copying it, would otherwise
            be told to go and fix a bug that is not there.
        no_baseline: Region id to the reason the *workbook* cannot be a baseline for it, where
            the notebook does compute the column and pass its values in. Also
            ``NOT_RECONCILED`` -- nothing checked is nothing claimed -- but a different fact
            from ``not_reproduced`` and reported as one, because the alternative is to withhold
            the values and misreport a computed column as an absent one. A formula column whose
            cached cells are calculated empty strings and a lookup that cached the text a paste
            left behind are both this, not that.

    Returns:
        A complete report. A workbook that cannot be opened produces a NOT_RECONCILED report
        rather than an exception, so a caller never has to choose between a traceback and a
        false pass.

    Raises:
        ReconciliationError: The workbook path does not exist.
    """
    path = Path(workbook_path)
    if not path.is_file():
        raise ReconciliationError(
            f"cannot reconcile against {path}: the workbook does not exist. Check the path."
        )

    tolerance = tolerance or Tolerance()
    run_notes = list(notes)
    spec_source = spec_source or ("provided" if specs else "inferred")

    try:
        handle = open_workbook(path)
    except WorkbookOpenError as exc:
        logger.warning("cannot open %s: %s", path.name, exc)
        return ReconciliationReport(
            workbook=str(path),
            notebook=notebook,
            tolerance=tolerance,
            spec_source="none",
            notes=[*run_notes, f"The workbook could not be opened, so nothing was compared: {exc}"],
        )

    try:
        if specs is None:
            if analysis is None:
                from kedge.analysis.analyse import analyse

                analysis = analyse(path)
            specs = infer_regions(handle, analysis)
            if not specs:
                run_notes.append(
                    "No region in this workbook carries cached values, so there was nothing "
                    "to reconcile against. This is not a pass."
                )
        baselines = read_baselines(handle, specs)
        return reconcile_values(
            specs,
            actuals,
            baselines,
            workbook=str(path),
            tolerance=tolerance,
            max_mismatch_rows=max_mismatch_rows,
            notebook=notebook,
            workbook_sha256=handle.identity.sha256,
            analysis=analysis,
            spec_source=spec_source,
            notes=run_notes,
            not_reproduced=not_reproduced,
            no_baseline=no_baseline,
        )
    finally:
        handle.close()


# =============================================================================
# THE NOTEBOOK SIDE
# =============================================================================


def declared_regions(definitions: Mapping[str, Any]) -> list[RegionSpec]:
    """Read the regions a notebook declares for itself.

    A notebook says which of its columns correspond to which workbook ranges by defining
    :data:`DECLARED_REGIONS_NAME`::

        KEDGE_RECONCILE = [
            {"id": "rwa", "reference": "Calc!G2:G501", "variable": "calc", "column": "rwa"},
        ]

    Declared beats inferred, always. An inferred mapping that quietly matched the wrong
    column would be worse than no mapping at all.

    Args:
        definitions: The notebook's definitions.

    Returns:
        The declared regions, or an empty list when the notebook declares none.
    """
    declared = definitions.get(DECLARED_REGIONS_NAME)
    if declared is None:
        return []
    if isinstance(declared, (RegionSpec, dict)):
        declared = [declared]
    if not isinstance(declared, (list, tuple)):
        logger.warning(
            "%s is a %s, not a list of regions", DECLARED_REGIONS_NAME, type(declared).__name__
        )
        return []
    return specs_from_mapping(list(declared))


def notebook_definitions(notebook_path: Path | str) -> dict[str, Any]:
    """Execute a notebook and return everything it defined.

    marimo notebooks are ordinary Python modules holding a ``marimo.App``, and ``App.run()``
    is public API that executes every cell and hands back the definitions. That is the whole
    mechanism: no kernel, no session, no server, and nothing private. A file with no ``app``
    is executed as a plain script and its module globals are its definitions.

    The notebook runs **in this process**, with its directory on ``sys.path`` so its own
    imports resolve. Reconciling a notebook therefore means running it, which is the same
    trust boundary as opening it in marimo.

    Args:
        notebook_path: Path to the notebook.

    Returns:
        Variable name to value.

    Raises:
        NotebookRunError: The file does not exist, cannot be imported, or raised while
            running. The message names the notebook and carries the original error.
    """
    path = Path(notebook_path).resolve()
    if not path.is_file():
        raise NotebookRunError(
            f"cannot reconcile {path}: the notebook does not exist. Check the path."
        )

    module_name = f"_kedge_reconcile_{abs(hash(str(path)))}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise NotebookRunError(f"cannot reconcile {path.name}: Python cannot import that file.")

    module = importlib.util.module_from_spec(spec)
    parent = str(path.parent)
    added = parent not in sys.path
    if added:
        sys.path.insert(0, parent)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        app = getattr(module, "app", None)
        runner = getattr(app, "run", None)
        if not callable(runner):
            logger.info("%s defines no marimo app; reading its module globals", path.name)
            return {
                name: value for name, value in vars(module).items() if not name.startswith("__")
            }
        _outputs, definitions = runner()
        logger.info("ran %s: %d definition(s)", path.name, len(definitions))
        return dict(definitions)
    except Exception as exc:
        raise NotebookRunError(
            f"cannot reconcile {path.name}: the notebook raised while running "
            f"({type(exc).__name__}: {exc}). Nothing was compared. Fix the notebook and run "
            f"reconciliation again."
        ) from exc
    finally:
        sys.modules.pop(module_name, None)
        if added and parent in sys.path:
            sys.path.remove(parent)


def reconcile_notebook(
    notebook_path: Path | str,
    workbook_path: Path | str,
    *,
    absolute_tolerance: float | None = None,
    relative_tolerance: float | None = None,
    max_mismatch_rows: int = _DEFAULT_MISMATCH_ROWS,
    config: ReconciliationConfig | None = None,
) -> ReconciliationReport:
    """Reconcile a notebook's outputs against a workbook's cached values. The CLI entry point.

    ``kedge reconcile notebook.py process.xlsx`` is this function. It needs no model and no
    running kernel: it executes the notebook (see :func:`notebook_definitions`), takes the
    regions the notebook declares — or infers them from the workbook when it declares none —
    reads the cached values for each, and compares.

    Args:
        notebook_path: The notebook whose outputs are checked.
        workbook_path: The workbook holding the cached expected values.
        absolute_tolerance: Overrides the configured absolute tolerance.
        relative_tolerance: Overrides the configured relative tolerance.
        max_mismatch_rows: How many mismatching rows to print per region.
        config: The ``[reconciliation]`` config section, when the caller has resolved it.

    Returns:
        A complete report. A notebook that fails to run produces a NOT_RECONCILED report
        naming the error, not a traceback and never a pass.

    Raises:
        ReconciliationError: The workbook does not exist.
    """
    configured = Tolerance.from_config(config) if config is not None else Tolerance()
    tolerance = Tolerance(
        absolute=absolute_tolerance if absolute_tolerance is not None else configured.absolute,
        relative=relative_tolerance if relative_tolerance is not None else configured.relative,
    )
    if config is not None and max_mismatch_rows == _DEFAULT_MISMATCH_ROWS:
        max_mismatch_rows = config.max_mismatch_rows

    notebook = Path(notebook_path)
    workbook = Path(workbook_path)

    try:
        definitions = notebook_definitions(notebook)
    except NotebookRunError as exc:
        logger.warning("notebook %s did not run: %s", notebook.name, exc)
        return ReconciliationReport(
            workbook=str(workbook),
            notebook=str(notebook),
            tolerance=tolerance,
            spec_source="none",
            regions=[
                RegionResult(
                    spec_id="notebook",
                    label=notebook.name,
                    status=ReconciliationStatus.NOT_RECONCILED,
                    reason=NotReconciledReason.NOTEBOOK_FAILED,
                    detail=f"{NotReconciledReason.NOTEBOOK_FAILED.explanation} {exc}",
                    tolerance=tolerance,
                )
            ],
            notes=[str(exc)],
        )

    specs = declared_regions(definitions)
    if specs:
        actuals = {spec.id: resolve_actual(definitions, spec) for spec in specs}
        return reconcile_workbook(
            workbook,
            actuals,
            specs=specs,
            tolerance=tolerance,
            max_mismatch_rows=max_mismatch_rows,
            notebook=str(notebook),
            spec_source="declared",
        )

    notes = [
        f"The notebook declares no {DECLARED_REGIONS_NAME}, so the regions were inferred from "
        f"the workbook's formula columns and matched to notebook columns by name. Declare "
        f"{DECLARED_REGIONS_NAME} to make the mapping explicit."
    ]
    inferred = _infer_and_resolve(workbook, definitions)
    if inferred is None:
        return reconcile_workbook(
            workbook,
            {},
            tolerance=tolerance,
            max_mismatch_rows=max_mismatch_rows,
            notebook=str(notebook),
            notes=notes,
        )
    specs, actuals, analysis = inferred
    return reconcile_workbook(
        workbook,
        actuals,
        specs=specs,
        tolerance=tolerance,
        max_mismatch_rows=max_mismatch_rows,
        notebook=str(notebook),
        analysis=analysis,
        spec_source="inferred",
        notes=notes,
    )


def _infer_and_resolve(
    workbook: Path, definitions: Mapping[str, Any]
) -> tuple[list[RegionSpec], dict[str, Any], WorkbookAnalysis] | None:
    """Infer regions from the workbook and match them against the notebook's definitions.

    Only regions the notebook actually has a column for are kept: reporting two hundred
    NOT_RECONCILED regions for a workbook whose notebook translates three of them is noise,
    and it buries the ones that matter.
    """
    from kedge.analysis.analyse import analyse

    try:
        analysis = analyse(workbook)
    except Exception as exc:  # a broad catch: a workbook we cannot analyse is a report, not a crash
        logger.warning("could not analyse %s: %s", workbook.name, exc)
        return None

    try:
        handle = open_workbook(workbook)
    except WorkbookOpenError:
        return None
    try:
        candidates = infer_regions(handle, analysis)
    finally:
        handle.close()

    specs: list[RegionSpec] = []
    actuals: dict[str, Any] = {}
    for spec in candidates:
        variable, values = find_actual(definitions, spec)
        if values is None:
            continue
        specs.append(spec.model_copy(update={"variable": variable}))
        actuals[spec.id] = values

    if not specs:
        logger.info("no inferred region matched a notebook column in %s", workbook.name)
        return [], {}, analysis
    return specs, actuals, analysis
