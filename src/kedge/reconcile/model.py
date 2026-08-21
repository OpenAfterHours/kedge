"""The reconciliation report: what was compared, what matched, and what to do about it.

Every type here is plain, serialisable data. The comparison engine builds them, the CLI
prints them, the notebook panel renders them, and the agent receives them as a tool result.
None of them touch marimo, a workbook or a frame, so the whole schema is testable in
isolation.

**One import qualifies that, and it is worth being precise about which.** This module used to
name marimo, polars and openpyxl and import none of them. It now imports
:func:`kedge.xl.unambiguous_number` -- 150 lines of ``re`` and ``float``, no frame anywhere --
because :func:`as_numeric_pair` needs Excel's rule for what text spells a number, and a second
copy of that rule is exactly what non-negotiable 3 forbids. The package that re-exports it does
import polars, so the sentence above is now about what these types *do* rather than about the
import graph: nothing here opens a workbook, builds a frame or reaches a kernel, and every
model can still be constructed, validated and serialised on its own.

**The one invariant that matters more than any other.** openpyxl calculates nothing, so a
workbook written by a tool rather than saved by Excel returns ``None`` for every formula
cell. A reconciliation run against such a workbook has checked precisely nothing, and
reporting that as "passed" would be the most dangerous defect this project could ship
(PLAN 1.5, 4.5, and the 6.2 risk row "reconciliation silently vacuous"). Three mechanisms
defend against it:

1. :class:`ReconciliationStatus` has no "unknown" that reads as success, and its
   ``__bool__`` is true for :attr:`~ReconciliationStatus.PASSED` and nothing else. ``if
   result.status:`` cannot be satisfied by an absent cache, even though the member is a
   ``str`` and every non-empty string is otherwise truthy.
2. :class:`RegionResult` refuses to be constructed as ``PASSED`` unless rows were actually
   compared and every one of them matched. The invariant is a validator, not a convention.
3. :func:`worst_status` ranks ``NOT_RECONCILED`` above ``PASSED``, so a report containing
   one unreconciled region among ninety-nine passing ones is not a passing report.

An absent cached value is deliberately *not* modelled as a mismatch. openpyxl cannot
distinguish "Excel never cached anything here" from "Excel cached a blank", so a ``None``
expected value is always counted as a missing baseline rather than guessed at in either
direction.

References:
- PLAN.md 4.5 (the reconciliation loop), 1.5 (the cached-value caveat), 2.6 (the semantics
  table the diagnostics triage against), 6.2 (vacuous reconciliation).
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, date, datetime, time
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kedge.xl.text import unambiguous_number

if TYPE_CHECKING:
    from collections.abc import Iterable

    from kedge.config import ReconciliationConfig

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"

__all__ = [
    "SCHEMA_VERSION",
    "Diagnosis",
    "DiagnosticCause",
    "Mismatch",
    "MismatchKind",
    "NotReconciledReason",
    "ReconciliationReport",
    "ReconciliationStatus",
    "RegionResult",
    "RegionSpec",
    "Tolerance",
    "as_numeric_pair",
    "signed_delta",
    "worst_status",
]


class _Frozen(BaseModel):
    """Base for every model here: immutable, and rejects unknown keys on load.

    ``ser_json_inf_nan="strings"`` matters more than it looks. Pydantic's default is to
    serialise ``inf`` and ``nan`` as JSON ``null``, and ``inf`` is exactly what a
    divide-by-zero translation produces (PLAN 2.6), so the default would quietly turn the
    evidence for a diagnosis into "no value". The strings survive :func:`_json_safe` and a
    strict ``json.dumps`` alike.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", ser_json_inf_nan="strings")


# =============================================================================
# STATUS
# =============================================================================


class ReconciliationStatus(StrEnum):
    """The outcome of one region, or of a whole report.

    There are exactly three outcomes and none of them means "probably fine". ``PASSED`` is
    a positive claim backed by compared rows; ``FAILED`` is a positive claim that the
    numbers differ; ``NOT_RECONCILED`` is the honest absence of a claim.

    Example:
        >>> bool(ReconciliationStatus.PASSED)
        True
        >>> bool(ReconciliationStatus.NOT_RECONCILED)
        False
        >>> bool(ReconciliationStatus.FAILED)
        False
    """

    PASSED = "passed"
    FAILED = "failed"
    NOT_RECONCILED = "not_reconciled"

    def __bool__(self) -> bool:
        """True only for :attr:`PASSED`.

        ``StrEnum`` members are strings, and every non-empty string is truthy, so without
        this a caller writing ``if status:`` would treat an absent cache as success. That
        is the exact failure PLAN 6.2 names, so the truthiness is overridden rather than
        left to reviewer discipline.
        """
        return self is ReconciliationStatus.PASSED

    @property
    def colour(self) -> Literal["green", "amber", "red"]:
        """The traffic-light colour for the notebook panel (PLAN 4.5)."""
        if self is ReconciliationStatus.PASSED:
            return "green"
        if self is ReconciliationStatus.NOT_RECONCILED:
            return "amber"
        return "red"

    @property
    def label(self) -> str:
        """The status in the words a user reads, upper-cased for a badge."""
        return self.value.replace("_", " ").upper()


#: Ranking used to fold region outcomes into a report outcome. ``NOT_RECONCILED`` outranks
#: ``PASSED`` deliberately: a report is only as strong as its weakest region.
_SEVERITY: dict[ReconciliationStatus, int] = {
    ReconciliationStatus.PASSED: 0,
    ReconciliationStatus.NOT_RECONCILED: 1,
    ReconciliationStatus.FAILED: 2,
}


def worst_status(statuses: Iterable[ReconciliationStatus]) -> ReconciliationStatus:
    """Fold region outcomes into the outcome for the whole report.

    Args:
        statuses: The per-region outcomes, in any order.

    Returns:
        The most severe outcome present. An empty iterable returns ``NOT_RECONCILED``,
        because a run that compared no regions has proved nothing — it has not passed.
    """
    worst = ReconciliationStatus.NOT_RECONCILED
    seen = False
    for status in statuses:
        if not seen:
            worst = status
            seen = True
        elif _SEVERITY[status] > _SEVERITY[worst]:
            worst = status
    return worst


class NotReconciledReason(StrEnum):
    """Why a region could not be checked at all.

    Each member carries the sentence a user sees. They all say, in one form or another,
    that nothing was verified — none of them can be mistaken for a pass.
    """

    NO_REGIONS = "no_regions"
    NO_CACHED_VALUES = "no_cached_values"
    PARTIAL_CACHED_VALUES = "partial_cached_values"
    BASELINE_RANGE_EMPTY = "baseline_range_empty"
    BASELINE_RANGE_UNREADABLE = "baseline_range_unreadable"
    BASELINE_RANGE_INCOMPLETE = "baseline_range_incomplete"
    BASELINE_TRUNCATED = "baseline_truncated"
    NO_ACTUAL_VALUES = "no_actual_values"
    NOTEBOOK_FAILED = "notebook_failed"
    NOT_REPRODUCED = "not_reproduced"
    NO_USABLE_BASELINE = "no_usable_baseline"

    @property
    def explanation(self) -> str:
        """The user-facing sentence. Every one of them denies a pass in as many words."""
        return _REASON_TEXT[self]


_REASON_TEXT: dict[NotReconciledReason, str] = {
    NotReconciledReason.NO_REGIONS: (
        "No regions were reconciled, so nothing about this notebook has been verified "
        "against the workbook. This is not a pass."
    ),
    NotReconciledReason.NO_CACHED_VALUES: (
        "The workbook holds no cached values for this range, so there is nothing to "
        "compare the Python output against. The output has NOT been checked and this is "
        "NOT a pass. A workbook written by a tool rather than saved by Excel carries nothing "
        "cached anywhere, so this may be the whole file rather than this one range. Open the "
        "workbook in Excel, let it recalculate, save it, and run reconciliation again."
    ),
    NotReconciledReason.PARTIAL_CACHED_VALUES: (
        "Some rows in this range carry no cached value, so they could not be checked. The "
        "rows that could be checked are reported below, but the region as a whole is NOT "
        "signed off."
    ),
    NotReconciledReason.BASELINE_RANGE_EMPTY: (
        "The workbook range named for this region is empty, so there is no baseline to "
        "compare against. Check the range reference. This is not a pass."
    ),
    NotReconciledReason.BASELINE_RANGE_UNREADABLE: (
        "The workbook range named for this region could not be read, so no comparison was "
        "possible. Check the sheet name and the A1 reference. This is not a pass."
    ),
    NotReconciledReason.BASELINE_RANGE_INCOMPLETE: (
        "The ranges recorded for this region do not add up to the cells it holds, so the "
        "workbook values could not be lined up against the notebook's rows. Comparing them "
        "anyway would put the wrong cell beside the wrong row and report differences, or "
        "agreement, that mean nothing. Nothing was compared and this is NOT a pass. A region "
        "broken into very many pieces -- a formula column interrupted by dozens of subtotal "
        "rows -- is the usual cause: reconcile an unbroken stretch of it, or declare the "
        "region's ranges in the notebook."
    ),
    NotReconciledReason.BASELINE_TRUNCATED: (
        "The workbook range named for this region is longer than the reconciler will read in "
        "one pass, so only its first rows were compared. The region as a whole is NOT signed "
        "off. Split it into smaller regions, or raise the read ceiling."
    ),
    NotReconciledReason.NOT_REPRODUCED: (
        "The notebook deliberately does not reproduce this region, and says why below. Nothing "
        "is claimed about it either way -- a region nobody checked is not a pass -- but this is "
        "a decision somebody made rather than a gap to go and fix."
    ),
    NotReconciledReason.NO_USABLE_BASELINE: (
        "The notebook computes this region -- its values are counted below -- but the workbook "
        "cannot serve as a baseline for it, and says why. Nothing is claimed either way: a "
        "region that could not be checked is not a pass, whatever the reason it could not be. "
        "This is a fact about the spreadsheet, not a decision about the conversion and not a "
        "cell that failed to run."
    ),
    NotReconciledReason.NO_ACTUAL_VALUES: (
        "The notebook produced no values for this region, so there was nothing to compare. "
        "Check that the cell ran and that the variable and column names match. This is not "
        "a pass."
    ),
    NotReconciledReason.NOTEBOOK_FAILED: (
        "The notebook did not run to completion, so its outputs could not be read. Nothing "
        "was compared and nothing is claimed."
    ),
}


# =============================================================================
# TOLERANCE
# =============================================================================


class Tolerance(_Frozen):
    """Absolute and relative tolerances, applied separately.

    A row matches when it is within *either* tolerance, so a non-zero relative tolerance
    **widens** what counts as a match at large magnitudes rather than tightening it.

    **`relative` defaults to 0, so the absolute tolerance governs.** This is a deliberate
    choice for money. With the previous ``1e-9`` default, a one-penny break passed silently
    on anything above about ten million: ``0.01 / 5e7`` is ``2e-10``, inside the tolerance.
    A penny is a penny at any magnitude, and a reconciliation that quietly forgives one on a
    large balance is worse than no reconciliation, because it is trusted.

    Raise `relative` when reconciling ratios, rates or anything where proportional agreement
    is the real claim -- there it earns its place, and an absolute tolerance alone is useless.
    Near zero a relative tolerance alone is useless, which is why the two are kept separate
    rather than folded together (PLAN 4.5).

    The tolerance travels in the report because it is part of the claim being made: a
    reconciliation that passed at ``1e-2`` is a different statement from one that passed at
    ``1e-9``, and a reader who cannot see which one it was cannot judge the result.

    Example:
        >>> Tolerance(absolute=0.005, relative=0.0).matches(1.0, 1.004)
        True
        >>> Tolerance().matches(50_000_000.00, 50_000_000.01)  # a penny, caught
        False
        >>> Tolerance(absolute=0.0, relative=1e-9).matches(1e9, 1e9 + 0.5)
        True
    """

    absolute: float = Field(default=1e-6, ge=0.0)
    relative: float = Field(default=0.0, ge=0.0)

    @classmethod
    def from_config(cls, config: ReconciliationConfig) -> Tolerance:
        """Build a tolerance from the ``[reconciliation]`` config section."""
        return cls(absolute=config.absolute_tolerance, relative=config.relative_tolerance)

    def matches(self, expected: float, actual: float) -> bool:
        """Whether two finite numbers agree within either tolerance."""
        if not (math.isfinite(expected) and math.isfinite(actual)):
            return False
        delta = abs(actual - expected)
        if delta <= self.absolute:
            return True
        scale = abs(expected)
        return scale > 0.0 and delta <= self.relative * scale

    def describe(self) -> str:
        """A one-line rendering for a report header.

        Names only the tolerances actually in force, so the claim the report makes is the claim
        a reader sees. "or relative 0" would be misleading -- it does nothing.
        """
        if self.relative <= 0.0:
            return f"absolute {self.absolute:g}"
        return f"absolute {self.absolute:g} or relative {self.relative:g}"


# =============================================================================
# MISMATCHES
# =============================================================================


class MismatchKind(StrEnum):
    """What sort of disagreement one row shows.

    The kind is what the diagnostics in :mod:`kedge.reconcile.diagnose` pattern-match
    against, so it is deliberately finer-grained than "not equal".
    """

    VALUE_DIFFERS = "value_differs"
    ACTUAL_NULL = "actual_null"
    TYPE_DIFFERS = "type_differs"
    EXCEL_ERROR = "excel_error"
    NON_FINITE = "non_finite"
    EXTRA_ROW = "extra_row"
    MISSING_ROW = "missing_row"


class Mismatch(_Frozen):
    """One row where the workbook and the notebook disagree.

    Both values travel so the user can read them side by side and so the diagnostics have
    something to pattern-match on (PLAN 4.5 step 4).
    """

    row: int = Field(description="1-based position within the compared vectors.")
    cell: str | None = Field(default=None, description="Sheet-qualified A1, e.g. 'Calc!G47'.")
    kind: MismatchKind = MismatchKind.VALUE_DIFFERS
    expected: Any = Field(default=None, description="The value Excel cached.")
    actual: Any = Field(default=None, description="The value the notebook produced.")
    absolute_delta: float | None = Field(
        default=None, description="Unsigned magnitude. Days, for a date difference."
    )
    relative_delta: float | None = Field(
        default=None, description="None when the expected value is zero or non-numeric."
    )

    @property
    def signed_delta(self) -> float | None:
        """``actual - expected`` where both are numeric, else None.

        The sign is what separates a systematic offset from scattered noise, so the
        diagnostics need it even though the report headline quotes magnitudes.
        """
        return signed_delta(self.expected, self.actual)


def signed_delta(expected: Any, actual: Any) -> float | None:
    """Signed ``actual - expected`` in native units, or in days for a pair of dates.

    Public because the comparison engine needs the same arithmetic *before* it has decided
    whether a row is a mismatch: the worst delta in a report is taken across every compared
    row, including the ones that matched.

    Args:
        expected: The value Excel cached.
        actual: The value the notebook produced.

    Returns:
        ``actual - expected``, in days where both values are dates, or None where the pair is
        not comparable as a magnitude (booleans, mixed types, non-finite numbers, or text that
        does not unambiguously spell a number).

    Example:
        >>> signed_delta(2.68, 2.67)
        -0.009999999999999787
        >>> signed_delta("a", 1.0) is None
        True
        >>> signed_delta("20.0", 25.0)  # Excel would coerce the text and so does this
        5.0
    """
    numbers = as_numeric_pair(expected, actual)
    if numbers is not None:
        expected_number, actual_number = numbers
        if not (math.isfinite(expected_number) and math.isfinite(actual_number)):
            return None
        return actual_number - expected_number
    expected_dt = _as_datetime(expected)
    actual_dt = _as_datetime(actual)
    if expected_dt is not None and actual_dt is not None:
        return (actual_dt - expected_dt).total_seconds() / 86_400.0
    return None


def as_numeric_pair(expected: Any, actual: Any) -> tuple[float, float] | None:
    """The two values as floats when this is a numeric comparison, else None.

    **A number spelled as text is a number, and refusing to say so makes doing the right thing
    fail.** Excel coerces at the point of use, so a ``VLOOKUP`` that returns a text cell out of
    a pasted rate card caches the *text* -- ``'20.0'`` -- and then multiplies by it perfectly
    happily. A conversion that types the column, which it must if the arithmetic below it is
    not to fail four operations later inside a query plan, produces ``20.0``; reading that pair
    as a type difference makes the region unreconcilable **by construction**, so doing the
    right thing is what breaks the check.

    **The line is information loss, not type.** ``'00417'`` is a client code and a client code
    is not the number 417: the leading zeros are the whole point, and reporting agreement there
    would bless a join key that has already broken every join it takes part in. That
    distinction lives in :func:`kedge.xl.unambiguous_number` -- non-negotiable 3 -- and is the
    same rule :func:`kedge.ingest.coerce.coerce_numeric_text` applies to a hand-in column on
    the way in, so the reader and the reconciler cannot disagree about what a number is.

    Two strings are never coerced, even when both spell numbers: a text column against a text
    column is a text comparison, and ``'417'`` against ``'417.0'`` is a difference the reader
    is entitled to see.

    Args:
        expected: The value Excel cached.
        actual: The value the notebook produced.

    Returns:
        ``(expected, actual)`` as floats when at least one side is a genuine number and the
        other is either a number or text that unambiguously spells one; None otherwise, which
        leaves the caller to report a type difference exactly as it did before.

    Example:
        >>> as_numeric_pair("20.0", 20.0)
        (20.0, 20.0)
        >>> as_numeric_pair("00417", 417.0) is None
        True
    """
    expected_is_number = isinstance(expected, (int, float)) and not isinstance(expected, bool)
    actual_is_number = isinstance(actual, (int, float)) and not isinstance(actual, bool)
    if expected_is_number and actual_is_number:
        return float(expected), float(actual)
    if expected_is_number:
        spelled = unambiguous_number(actual)
        return (float(expected), spelled) if spelled is not None else None
    if actual_is_number:
        spelled = unambiguous_number(expected)
        return (spelled, float(actual)) if spelled is not None else None
    return None


def _as_datetime(value: Any) -> datetime | None:
    """Normalise a date or datetime to a naive datetime, or return None."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo is not None else value
    if isinstance(value, date):
        return datetime.combine(value, time())
    return None


# =============================================================================
# DIAGNOSTICS
# =============================================================================


class DiagnosticCause(StrEnum):
    """The PLAN 2.6 semantic gaps, as machine-checkable causes.

    Every member maps onto a row of the PLAN 2.6 table, plus one rule that table does not
    yet carry: :attr:`ROUNDING_15_DIGIT_COLLAPSE`. See
    :mod:`kedge.reconcile.diagnose` for the signature each one is detected by.
    """

    ROUNDING_MODE = "rounding_mode"
    ROUNDING_NOT_APPLIED = "rounding_not_applied"
    ROUNDING_15_DIGIT_COLLAPSE = "rounding_15_digit_collapse"
    NULL_VS_EMPTY = "null_vs_empty"
    DIVIDE_BY_ZERO = "divide_by_zero"
    JOIN_KEY_NOT_UNIQUE = "join_key_not_unique"
    DATE_SERIAL_OFFSET = "date_serial_offset"
    TEXT_FORMATTED_NUMBER = "text_formatted_number"


class Diagnosis(_Frozen):
    """One hypothesis about why a region failed, with the evidence and the fix.

    Written as data rather than as a formatted string because it has two audiences: the
    model receives it as a tool result and acts on ``remedy``, while the user reads
    ``headline`` and ``evidence`` in the notebook panel (PLAN 4.5).
    """

    cause: DiagnosticCause
    confidence: float = Field(ge=0.0, le=1.0, description="How well the signature fitted.")
    headline: str = Field(description="One sentence naming the suspected cause.")
    evidence: list[str] = Field(
        default_factory=list, description="What in the data supports the hypothesis."
    )
    remedy: str = Field(description="The concrete change to make, as code where possible.")
    remedy_symbol: str | None = Field(
        default=None, description="The kedge.xl function that implements the fix."
    )
    reference: str = Field(default="PLAN 2.6", description="Where the semantics are documented.")
    affected_rows: int = Field(default=0, ge=0)

    @property
    def summary(self) -> str:
        """The hypothesis and its fix on one line, for a CLI row or a log entry.

        The panel and the tool result read the fields separately; this is for the places that
        have one line to spend and must still say what to do about it.

        Example:
            >>> Diagnosis(
            ...     cause=DiagnosticCause.ROUNDING_MODE,
            ...     confidence=0.9,
            ...     headline="Wrong rounding mode.",
            ...     remedy="Use round_half_away.",
            ... ).summary
            '90% Wrong rounding mode. Fix: Use round_half_away.'
        """
        return f"{self.confidence:.0%} {self.headline} Fix: {self.remedy}"


# =============================================================================
# REGIONS
# =============================================================================


class RegionSpec(_Frozen):
    """What to reconcile: one workbook range against one notebook column.

    Regions, not sheets. A per-region pass or fail localises the failure, where a
    whole-sheet red tells the user nothing (PLAN 4.5).

    Example:
        >>> RegionSpec(id="rwa", reference="Calc!G2:G501", variable="calc", column="rwa").label_or_id
        'rwa'
    """

    id: str = Field(description="Stable slug, unique within a report.")
    reference: str = Field(
        description=(
            "Sheet-qualified A1 range, e.g. 'Calc!G2:G501'. Where `ranges` is given this is "
            "the rectangle enclosing them, and it is what the panel shows; the cells that "
            "are actually read are `ranges`."
        )
    )
    ranges: list[str] = Field(
        default_factory=list,
        description=(
            "The exact ranges the region covers, in reading order, when it is broken into "
            "more than one -- a formula column interrupted by subtotal rows, say. Empty "
            "means the region is the single rectangle named by `reference`."
        ),
    )
    cell_count: int | None = Field(
        default=None,
        ge=0,
        description=(
            "How many cells the region holds, when that is known independently of the "
            "ranges. Where they disagree the baseline cannot be built and the region "
            "degrades to NOT_RECONCILED, rather than lining a short vector up against the "
            "notebook's rows and comparing the wrong cell with the wrong row."
        ),
    )
    variable: str | None = Field(
        default=None, description="Notebook variable holding the frame or series."
    )
    column: str | None = Field(default=None, description="Column within that frame.")
    label: str | None = Field(default=None, description="Human name for the panel.")
    description: str | None = None
    orientation: Literal["column", "row", "auto"] = "auto"

    @property
    def label_or_id(self) -> str:
        """The label if one was given, otherwise the id."""
        return self.label or self.id

    @property
    def segments(self) -> tuple[str, ...]:
        """The ranges the baseline reads, in reading order.

        The explicit ``ranges`` where the region is discontiguous, and otherwise the one
        rectangle in ``reference`` -- so a caller that knows nothing about discontiguity
        reads exactly what it always did.
        """
        return tuple(self.ranges) if self.ranges else (self.reference,)


class RegionResult(_Frozen):
    """The outcome for one region: counts, worst deltas, sample rows and diagnoses.

    Constructing this as ``PASSED`` without compared rows raises. That is the mechanical
    half of "never degrade to passed" — the other half is that
    :class:`ReconciliationStatus` is falsy unless it is ``PASSED``.

    Example:
        >>> RegionResult(
        ...     spec_id="rwa",
        ...     status=ReconciliationStatus.NOT_RECONCILED,
        ...     reason=NotReconciledReason.NO_CACHED_VALUES,
        ...     tolerance=Tolerance(),
        ... ).colour
        'amber'
    """

    spec_id: str
    reference: str | None = None
    label: str | None = None
    variable: str | None = None
    column: str | None = None

    status: ReconciliationStatus
    reason: NotReconciledReason | None = None
    detail: str = Field(default="", description="One paragraph a user can act on.")
    tolerance: Tolerance

    rows_expected: int = Field(default=0, ge=0)
    rows_actual: int = Field(default=0, ge=0)
    rows_compared: int = Field(default=0, ge=0)
    rows_matched: int = Field(default=0, ge=0)
    rows_differing: int = Field(default=0, ge=0)
    rows_missing_baseline: int = Field(
        default=0, ge=0, description="Rows where the workbook cached no value."
    )

    worst_absolute_delta: float | None = None
    worst_relative_delta: float | None = None
    worst_at: str | None = Field(default=None, description="Where the worst delta was.")

    mismatches: list[Mismatch] = Field(default_factory=list)
    mismatches_truncated: bool = False
    diagnoses: list[Diagnosis] = Field(default_factory=list)

    @model_validator(mode="after")
    def _enforce_pass_requires_evidence(self) -> RegionResult:
        """Refuse the construction of a pass that nothing supports."""
        if self.status is ReconciliationStatus.PASSED:
            if self.rows_compared <= 0:
                msg = (
                    f"region {self.spec_id!r} cannot be 'passed': no rows were compared. A "
                    f"region with no baseline is 'not_reconciled' (PLAN 1.5, 4.5)."
                )
                raise ValueError(msg)
            if self.rows_differing > 0:
                msg = (
                    f"region {self.spec_id!r} cannot be 'passed': {self.rows_differing} rows "
                    f"differ."
                )
                raise ValueError(msg)
            if self.reason is not None:
                msg = (
                    f"region {self.spec_id!r} cannot be 'passed' and carry a not-reconciled reason."
                )
                raise ValueError(msg)
        elif self.status is ReconciliationStatus.NOT_RECONCILED and self.reason is None:
            msg = f"region {self.spec_id!r} is 'not_reconciled' but does not say why."
            raise ValueError(msg)
        return self

    @property
    def colour(self) -> Literal["green", "amber", "red"]:
        """The traffic-light colour for this region's row in the panel."""
        return self.status.colour

    @property
    def name(self) -> str:
        """The label if one was given, otherwise the spec id."""
        return self.label or self.spec_id

    @property
    def top_diagnosis(self) -> Diagnosis | None:
        """The highest-confidence hypothesis, or None when nothing matched."""
        return self.diagnoses[0] if self.diagnoses else None

    def headline(self) -> str:
        """One line summarising the region, suitable for a list or a log."""
        where = f" ({self.reference})" if self.reference else ""
        if self.status is ReconciliationStatus.PASSED:
            return (
                f"{self.name}{where}: PASSED - {self.rows_matched} of {self.rows_compared} "
                f"rows within {self.tolerance.describe()}"
            )
        if self.status is ReconciliationStatus.FAILED:
            return (
                f"{self.name}{where}: FAILED - {self.rows_differing} of {self.rows_compared} "
                f"rows differ"
            )
        return f"{self.name}{where}: NOT RECONCILED - {self.reason.value if self.reason else 'unknown'}"


# =============================================================================
# THE REPORT
# =============================================================================


class ReconciliationReport(_Frozen):
    """Everything one reconciliation run established, and everything it did not.

    The report is the artifact PLAN 4.5 is arguing for: "this Python reproduces the
    workbook's numbers to within 1e-6, here is the proof, and it re-checks itself on every
    run". Which is why the tolerance, the workbook hash and the cached-value coverage all
    travel with the result rather than being implied by it.

    Example:
        >>> ReconciliationReport(workbook="process.xlsx", tolerance=Tolerance()).status
        <ReconciliationStatus.NOT_RECONCILED: 'not_reconciled'>
    """

    schema_version: str = SCHEMA_VERSION
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    workbook: str
    workbook_sha256: str | None = None
    notebook: str | None = None

    tolerance: Tolerance
    cached_value_status: Literal["present", "partial", "absent", "not_applicable", "unknown"] = (
        "unknown"
    )
    cached_value_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    formula_cell_count: int | None = None

    spec_source: Literal["declared", "inferred", "provided", "none"] = "none"
    regions: list[RegionResult] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list, description="Caveats about the run itself.")

    @property
    def status(self) -> ReconciliationStatus:
        """The report outcome: the most severe region outcome, or NOT_RECONCILED if empty."""
        return worst_status(region.status for region in self.regions)

    @property
    def colour(self) -> Literal["green", "amber", "red"]:
        """The traffic-light colour for the panel as a whole."""
        return self.status.colour

    @property
    def passed(self) -> list[RegionResult]:
        """Regions that were checked and matched."""
        return [r for r in self.regions if r.status is ReconciliationStatus.PASSED]

    @property
    def failed(self) -> list[RegionResult]:
        """Regions that were checked and differ."""
        return [r for r in self.regions if r.status is ReconciliationStatus.FAILED]

    @property
    def not_reconciled(self) -> list[RegionResult]:
        """Regions that could not be checked at all."""
        return [r for r in self.regions if r.status is ReconciliationStatus.NOT_RECONCILED]

    @property
    def rows_compared(self) -> int:
        """How many rows across all regions were actually compared."""
        return sum(region.rows_compared for region in self.regions)

    @property
    def diagnoses(self) -> list[Diagnosis]:
        """Every region's diagnoses, most confident first."""
        merged = [d for region in self.regions for d in region.diagnoses]
        return sorted(merged, key=lambda d: (-d.confidence, d.cause.value))

    def region(self, spec_id: str) -> RegionResult | None:
        """Return the result for one region id, or None."""
        return next((r for r in self.regions if r.spec_id == spec_id), None)

    @property
    def declared_not_reproduced(self) -> list[RegionResult]:
        """Regions the notebook says it deliberately does not reproduce.

        Still not passes -- nothing here can make an unchecked region one. They are counted
        apart because a signal that is permanently amber is one people learn to ignore, and a
        conversion that deliberately improves on a workbook column would otherwise sit at
        NOT RECONCILED for ever with nothing anybody could do about it.
        """
        return [
            region for region in self.regions if region.reason is NotReconciledReason.NOT_REPRODUCED
        ]

    @property
    def declared_no_baseline(self) -> list[RegionResult]:
        """Regions the notebook computes but the workbook cannot be a baseline for.

        A different fact from :attr:`declared_not_reproduced` and the reader needs both. There
        the conversion chose not to reproduce something; here it did reproduce it and the
        spreadsheet has nothing usable to check it against -- a formula column whose cached
        values are empty strings, a lookup that cached the text a paste left behind. Answering
        the two with the same mechanism would misreport a computed column as an absent one.

        Still not passes. Nothing here can make an unchecked region one.
        """
        return [
            region
            for region in self.regions
            if region.reason is NotReconciledReason.NO_USABLE_BASELINE
        ]

    @property
    def declared_exceptions(self) -> list[RegionResult]:
        """Every region excused with a reason, of either kind, in report order."""
        excused = {
            NotReconciledReason.NOT_REPRODUCED,
            NotReconciledReason.NO_USABLE_BASELINE,
        }
        return [region for region in self.regions if region.reason in excused]

    def headline(self) -> str:
        """The single sentence that goes at the top of the panel and the CLI output."""
        status = self.status
        if not self.regions:
            return f"NOT RECONCILED - {NotReconciledReason.NO_REGIONS.explanation}"
        declared = self.declared_exceptions
        unchecked = [region for region in self.not_reconciled if region not in declared]
        counts = (
            f"{len(self.passed)} passed, {len(self.failed)} failed, {len(unchecked)} not reconciled"
        )
        # The two exceptions are counted apart because they say different things to a reader.
        # "Not reproduced" is a decision about the conversion; "no usable baseline" is a fact
        # about the spreadsheet, on a column the notebook did compute.
        excuses = []
        if self.declared_not_reproduced:
            excuses.append(
                f"{len(self.declared_not_reproduced)} region(s) declared not reproduced by "
                f"this notebook"
            )
        if self.declared_no_baseline:
            excuses.append(
                f"{len(self.declared_no_baseline)} region(s) the workbook cannot be a baseline for"
            )
        aside = f" {' and '.join(excuses)}, with reasons." if excuses else ""
        if status is ReconciliationStatus.PASSED:
            return (
                f"PASSED - {len(self.regions)} regions, {self.rows_compared} rows compared, "
                f"all within {self.tolerance.describe()}"
            )
        if status is ReconciliationStatus.FAILED:
            return f"FAILED - {counts} (tolerance {self.tolerance.describe()}).{aside}"
        if declared and not unchecked and not self.failed:
            # Everything that was claimed passed, and what was not claimed was declared. Still
            # not a pass -- one region of the workbook is unverified and saying otherwise would
            # be the false claim this module exists to prevent -- but the reader is not being
            # sent to look for a defect.
            return (
                f"CHECKED WITH EXCEPTIONS - {len(self.passed)} of {len(self.passed)} claimed "
                f"regions passed within {self.tolerance.describe()}.{aside} Nothing is claimed "
                f"about those, so this is not a clean pass."
            )
        return (
            f"NOT RECONCILED - {counts}. Regions that could not be checked are NOT passes; "
            f"nothing is claimed about them.{aside}"
        )

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe mapping, with the derived status fields made explicit.

        The derived properties are materialised because a consumer reading the JSON has no
        access to them otherwise, and ``status`` is the field they will look for first.
        """
        payload = self.model_dump(mode="json")
        payload["status"] = self.status.value
        payload["colour"] = self.colour
        payload["headline"] = self.headline()
        payload["region_counts"] = {
            "passed": len(self.passed),
            "failed": len(self.failed),
            "not_reconciled": len(self.not_reconciled),
        }
        payload["rows_compared"] = self.rows_compared
        return _json_safe(payload)

    def __str__(self) -> str:
        """A plain-text rendering for the CLI. No colour, no marimo, no dependencies."""
        lines = [self.headline(), f"workbook: {self.workbook}"]
        if self.notebook:
            lines.append(f"notebook: {self.notebook}")
        lines.append(
            f"cached values: {self.cached_value_status}"
            + (
                f" (coverage {self.cached_value_coverage:.1%})"
                if self.cached_value_coverage is not None
                else ""
            )
        )
        lines.append("")
        for region in self.regions:
            lines.append(region.headline())
            if region.detail:
                lines.append(f"    {region.detail}")
            for diagnosis in region.diagnoses:
                lines.append(f"    likely cause ({diagnosis.confidence:.0%}): {diagnosis.headline}")
                lines.append(f"      fix: {diagnosis.remedy}")
            for mismatch in region.mismatches:
                lines.append(
                    f"      row {mismatch.row}"
                    + (f" {mismatch.cell}" if mismatch.cell else "")
                    + f": excel={mismatch.expected!r} python={mismatch.actual!r}"
                    + (
                        f" delta={mismatch.absolute_delta:g}"
                        if mismatch.absolute_delta is not None
                        else ""
                    )
                )
            if region.mismatches_truncated:
                lines.append("      [... further mismatching rows omitted]")
        for note in self.notes:
            lines.append(f"note: {note}")
        return "\n".join(lines)


def _json_safe(value: Any) -> Any:
    """Replace non-finite floats so the payload survives a strict JSON encoder.

    ``inf`` and ``nan`` are exactly the values a divide-by-zero translation produces, so
    they turn up in real reports and must not make the ``--json`` output unparseable.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return "Infinity" if value > 0 else ("-Infinity" if value < 0 else "NaN")
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value
