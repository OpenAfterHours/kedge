"""Why the numbers differ: pattern-matching mismatches against the PLAN 2.6 semantics table.

PLAN 4.5 sets the bar: "if every mismatch is within half a unit of the last retained decimal
place, say 'this looks like ROUND semantics' rather than making the user work it out". This
module is that instruction taken literally. It reads a region's mismatches, tries each known
Excel-versus-polars divergence against them, and returns the ones whose signature fits, most
confident first, each with the concrete remedy.

Every rule earns its place by being *discriminating*. "The numbers differ a bit" is not a
diagnosis; "every difference is exactly one unit in the last retained decimal place, Excel's
value is the odd one and Python's the even one in every single row, which is precisely what
half-to-even rounding does and nothing else does" is.

**The two rounding rules, and why they are separate.** Excel's ``ROUND`` does two things
polars' ``.round`` does not, and they fail differently:

1. It rounds *half away from zero* where polars rounds *half to even*. When these disagree,
   they disagree only at an exact tie, and at a tie the away-from-zero candidate is always
   the odd one — otherwise both methods would pick the same neighbour. So a banker's-rounding
   defect leaves a fingerprint: **Excel odd, Python even, in every mismatching row.**
2. It collapses the operand to **15 significant decimal digits before rounding**. So
   ``4491760.574999999`` is ``4491760.575`` to Excel and rounds to ``.58``, where a correct
   but naive half-away-from-zero implementation gives ``.57``. This one is not a tie at all,
   so the parity fingerprint is absent: a single mismatching row where Excel's last digit is
   even proves the disagreement cannot have come from half-to-even.

That second rule is not in PLAN 2.6. It was found by driving real Excel, it caused a genuine
one-penny error, and :func:`kedge.xl.round_half_away` implements both halves. The remedies
differ in what the reader should look for, which is why the diagnoses are separate causes.
Where a translation used ``pl.Expr.round`` and the data contains both exact ties and
near-boundary values, the parity test reports the 15-digit collapse; the remedy
(``kedge.xl.round_half_away``) is correct for both, and the evidence says which rows decided
it.

References:
- PLAN.md 2.6 (the semantics table), 4.5 (automatic diagnostics), CLAUDE.md ("Things that have
  already bitten").
"""

from __future__ import annotations

import logging
import math
from datetime import date
from typing import TYPE_CHECKING, Any

from kedge.analysis.values import ERROR_VALUES
from kedge.reconcile.model import Diagnosis, DiagnosticCause, Mismatch, MismatchKind

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

__all__ = ["MAX_DIAGNOSES", "decimals_retained", "diagnose"]

#: How many hypotheses one region reports. Beyond a handful the ranking stops meaning
#: anything and the user is back to working it out themselves.
MAX_DIAGNOSES = 4

#: Slack on the "one unit in the last place" test, as a fraction of that unit. Wide enough to
#: absorb the float error in scaling by a power of ten, narrow enough that a delta of two
#: units never reads as one.
_UNIT_SLACK = 0.02

#: The largest number of decimal places a rounding rule will reason about. Beyond this the
#: "last retained decimal place" is not a rounding decision, it is float noise.
_MAX_DECIMALS = 10

#: Excel's 1900 date system spans serial 1 (1900-01-01) to 2958465 (9999-12-31). A number in
#: this range sitting opposite a date is an unconverted serial.
_MAX_SERIAL = 2_958_465


def _is_number(value: Any) -> bool:
    """Whether a value is a real number. ``bool`` is not, despite subclassing ``int``."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite(value: Any) -> bool:
    """Whether a value is a finite number."""
    return _is_number(value) and math.isfinite(float(value))


def decimals_retained(value: float) -> int | None:
    """How many decimal places a number carries in Excel's 15-significant-digit view.

    Args:
        value: The number to inspect.

    Returns:
        The count of significant decimal places, or None when the value is only expressible
        in exponent form and the question does not apply.

    Example:
        >>> decimals_retained(4491760.58)
        2
        >>> decimals_retained(120.0)
        0
    """
    text = f"{float(value):.15g}"
    if "e" in text or "E" in text:
        return None
    _, _, fraction = text.partition(".")
    return len(fraction.rstrip("0")) if fraction else 0


def _last_digit(value: float, decimals: int) -> int | None:
    """The last retained decimal digit of a number, or None when it cannot be recovered."""
    scaled = abs(float(value)) * (10.0**decimals)
    if not math.isfinite(scaled) or scaled >= 2**53:
        return None
    return round(scaled) % 10


def _column_expr(column: str | None) -> str:
    """A ``pl.col(...)`` fragment naming the column under discussion."""
    return f'pl.col("{column}")' if column else "pl.col(<column>)"


# =============================================================================
# THE RULES
# =============================================================================


def _rounding(
    mismatches: Sequence[Mismatch], column: str | None, rows_compared: int
) -> Diagnosis | None:
    """Rounding semantics: mode, the 15-digit collapse, or rounding never applied at all."""
    numeric = [
        m
        for m in mismatches
        if m.kind in (MismatchKind.VALUE_DIFFERS, MismatchKind.TYPE_DIFFERS)
        and _finite(m.expected)
        and _finite(m.actual)
    ]
    if not numeric or len(numeric) != len(
        [m for m in mismatches if m.kind != MismatchKind.EXTRA_ROW]
    ):
        return None

    places = [decimals_retained(float(m.expected)) for m in numeric]
    if any(place is None for place in places):
        return None
    decimals = max(place for place in places if place is not None)
    if decimals > _MAX_DECIMALS:
        return None

    unit = 10.0**-decimals
    deltas = [abs(float(m.actual) - float(m.expected)) for m in numeric]
    if any(delta > unit * (1.0 + _UNIT_SLACK) for delta in deltas):
        return None

    place_text = f"{decimals} decimal place{'s' if decimals != 1 else ''}"
    scope = f"all {len(numeric)} mismatching rows" if len(numeric) > 1 else "the mismatching row"

    # Rounding never applied: Python still carries more precision than Excel kept, and every
    # difference is under half a unit -- the classic "forgot the ROUND()" shape.
    unrounded = [m for m in numeric if (decimals_retained(float(m.actual)) or 0) > decimals]
    if len(unrounded) == len(numeric) and all(
        delta <= unit * 0.5 * (1.0 + _UNIT_SLACK) for delta in deltas
    ):
        return Diagnosis(
            cause=DiagnosticCause.ROUNDING_NOT_APPLIED,
            confidence=0.9,
            headline=(
                f"This looks like a missing ROUND: Excel keeps {place_text} and the notebook "
                f"keeps more, and every difference is under half a unit of the last place."
            ),
            evidence=[
                f"Excel's values carry {place_text}; the notebook's carry more in {scope}.",
                f"Worst difference {max(deltas):g}, under the half-unit {unit / 2:g}.",
                f"{len(numeric)} of {rows_compared} compared rows differ.",
            ],
            remedy=(
                f"Round the column the way the workbook does: "
                f"round_half_away({_column_expr(column)}, {decimals}) from kedge.xl, or "
                f"{_column_expr(column)}.xl.round({decimals}) in notebook code. Do not use "
                f".round({decimals}): polars rounds half to even."
            ),
            remedy_symbol="kedge.xl.round_half_away",
            reference="PLAN 2.6 (rounding at .5)",
            affected_rows=len(numeric),
        )

    at_one_unit = [delta >= unit * (1.0 - _UNIT_SLACK) for delta in deltas]
    if not all(at_one_unit):
        return Diagnosis(
            cause=DiagnosticCause.ROUNDING_MODE,
            confidence=0.6,
            headline=(
                f"This looks like ROUND semantics: every difference is within one unit of the "
                f"last retained decimal place ({place_text})."
            ),
            evidence=[
                f"Worst difference {max(deltas):g} against a last-place unit of {unit:g}.",
                f"{len(numeric)} of {rows_compared} compared rows differ.",
                "The pattern is not clean enough to name the exact rounding defect.",
            ],
            remedy=(
                f"Route the rounding through kedge.xl: "
                f"round_half_away({_column_expr(column)}, {decimals}). If the column is a sum "
                f"of already-rounded values, check that each term is rounded where Excel "
                f"rounds it, not only the total."
            ),
            remedy_symbol="kedge.xl.round_half_away",
            reference="PLAN 2.6 (rounding at .5)",
            affected_rows=len(numeric),
        )

    away_from_zero = all(abs(float(m.expected)) > abs(float(m.actual)) for m in numeric)
    expected_digits = [_last_digit(float(m.expected), decimals) for m in numeric]
    actual_digits = [_last_digit(float(m.actual), decimals) for m in numeric]
    tie_shaped = (
        away_from_zero
        and all(digit is not None and digit % 2 == 1 for digit in expected_digits)
        and all(digit is not None and digit % 2 == 0 for digit in actual_digits)
    )

    if tie_shaped:
        return Diagnosis(
            cause=DiagnosticCause.ROUNDING_MODE,
            confidence=0.9,
            headline=(
                "Wrong rounding mode: polars rounds half to even, Excel rounds half away from zero."
            ),
            evidence=[
                f"Every difference is exactly one unit in the last place ({unit:g}).",
                f"Excel's last digit is odd and the notebook's is even in {scope} -- the "
                f"fingerprint of half-to-even, which can only disagree at an exact tie and "
                f"only ever by picking the even neighbour.",
                "Excel's value is the larger magnitude in every row.",
            ],
            remedy=(
                f"Replace .round({decimals}) with "
                f"round_half_away({_column_expr(column)}, {decimals}) from kedge.xl, or "
                f"{_column_expr(column)}.xl.round({decimals}) in notebook code."
            ),
            remedy_symbol="kedge.xl.round_half_away",
            reference="PLAN 2.6 (rounding at .5)",
            affected_rows=len(numeric),
        )

    witness = next(
        (
            m
            for m, digit in zip(numeric, expected_digits, strict=True)
            if digit is not None and digit % 2 == 0
        ),
        numeric[0],
    )
    return Diagnosis(
        cause=DiagnosticCause.ROUNDING_15_DIGIT_COLLAPSE,
        confidence=0.8,
        headline=(
            "Right rounding mode, missing Excel's 15-significant-digit collapse: Excel "
            "rounds what it displays, not the exact binary double."
        ),
        evidence=[
            f"Every difference is exactly one unit in the last place ({unit:g}), and Excel's "
            f"value is the larger magnitude."
            if away_from_zero
            else f"Every difference is exactly one unit in the last place ({unit:g}).",
            f"Row {witness.row}: Excel {witness.expected!r}, notebook {witness.actual!r}. "
            f"Excel's last digit is even here, so this row cannot be a half-to-even tie -- "
            f"the operand must have sat just below the boundary, and Excel collapsed it to "
            f"15 significant decimal digits before rounding.",
            "A hand-rolled floor(x * 100 + 0.5) / 100 reproduces the mode but not the "
            "collapse, and is out by a penny on exactly these rows.",
        ],
        remedy=(
            f"Use round_half_away({_column_expr(column)}, {decimals}) from kedge.xl rather "
            f"than a hand-rolled half-away expression: it collapses the operand to 15 "
            f"significant decimal digits first, which is the step that turns "
            f"4491760.574999999 into 4491760.58 as Excel does."
        ),
        remedy_symbol="kedge.xl.round_half_away",
        reference="CLAUDE.md (verified against Excel; not in PLAN 2.6)",
        affected_rows=len(numeric),
    )


def _nulls(
    mismatches: Sequence[Mismatch], column: str | None, rows_compared: int
) -> list[Diagnosis]:
    """Nulls in Python where Excel produced a number: empty-as-zero, or a failed join."""
    nulls = [m for m in mismatches if m.kind is MismatchKind.ACTUAL_NULL]
    if not nulls:
        return []

    fraction = len(nulls) / rows_compared if rows_compared else 1.0
    found: list[Diagnosis] = []

    if fraction >= 0.9:
        found.append(
            Diagnosis(
                cause=DiagnosticCause.TEXT_FORMATTED_NUMBER,
                confidence=0.75,
                headline=(
                    "Almost every row is null: this is the shape of a join that matched "
                    "nothing, which is usually a text-formatted key against a numeric one."
                ),
                evidence=[
                    f"{len(nulls)} of {rows_compared} compared rows are null in the notebook "
                    f"and hold a value in the workbook.",
                    "Excel coerces text-formatted numbers on the fly; polars leaves them as "
                    "String, so the join key never matches and every row comes back null.",
                ],
                remedy=(
                    "Profile the dtypes of both join keys at load. Where the workbook column "
                    "is text holding numbers, normalise it with to_number() from kedge.xl "
                    "before joining, or cast both sides to the same type explicitly."
                ),
                remedy_symbol="kedge.xl.to_number",
                reference="PLAN 2.6 (text-formatted numbers)",
                affected_rows=len(nulls),
            )
        )

    found.append(
        Diagnosis(
            cause=DiagnosticCause.NULL_VS_EMPTY,
            confidence=min(0.85, 0.5 + fraction) if fraction < 0.9 else 0.55,
            headline=(
                "Nulls where Excel produced a number: an empty cell is zero in Excel and "
                "null-propagating in polars."
            ),
            evidence=[
                f"{len(nulls)} of {rows_compared} compared rows are null in the notebook.",
                f"The workbook has a value in all of them, for example row {nulls[0].row}: "
                f"{nulls[0].expected!r}.",
                "In Excel =A1+B1 ignores an empty B1; in polars col('a') + col('b') "
                "propagates the null and the row vanishes from every downstream sum.",
            ],
            remedy=(
                f"Add the terms the way Excel does: pl.sum_horizontal(...) for a row-wise "
                f"sum, or add()/empty_as_zero() from kedge.xl for the individual operands "
                f"({_column_expr(column)}.xl.add(...) in notebook code)."
            ),
            remedy_symbol="kedge.xl.sum_horizontal",
            reference="PLAN 2.6 (empty cell in arithmetic)",
            affected_rows=len(nulls),
        )
    )
    return found


def _divide_by_zero(mismatches: Sequence[Mismatch], column: str | None) -> Diagnosis | None:
    """``inf`` or ``nan`` in Python, or a ``#DIV/0!`` the notebook did not reproduce."""
    non_finite = [m for m in mismatches if m.kind is MismatchKind.NON_FINITE]
    div_errors = [
        m
        for m in mismatches
        if m.kind is MismatchKind.EXCEL_ERROR
        and isinstance(m.expected, str)
        and m.expected in ("#DIV/0!", "#NUM!")
    ]
    if not non_finite and not div_errors:
        return None

    evidence: list[str] = []
    if non_finite:
        evidence.append(
            f"{len(non_finite)} row(s) hold inf or nan in the notebook, for example row "
            f"{non_finite[0].row}: {non_finite[0].actual!r} against Excel's "
            f"{non_finite[0].expected!r}."
        )
    if div_errors:
        evidence.append(
            f"{len(div_errors)} row(s) hold an Excel error in the workbook, for example row "
            f"{div_errors[0].row}: {div_errors[0].expected!r}."
        )
    evidence.append(
        "Excel raises #DIV/0! and propagates it visibly; polars returns inf, -inf or nan, "
        "which poisons every downstream aggregate quietly."
    )

    return Diagnosis(
        cause=DiagnosticCause.DIVIDE_BY_ZERO,
        confidence=0.9,
        headline="Division by zero: polars produced inf or nan where Excel raised an error.",
        evidence=evidence,
        remedy=(
            f"Wrap the division: div({_column_expr(column)}, pl.col(<denominator>)) from "
            f"kedge.xl returns null on a zero denominator rather than inf. Then decide "
            f"explicitly what the null means -- the workbook's own error cells are worth "
            f"asking about before the translation is signed off."
        ),
        remedy_symbol="kedge.xl.div",
        reference="PLAN 2.6 (divide by zero)",
        affected_rows=len(non_finite) + len(div_errors),
    )


def _row_counts(
    mismatches: Sequence[Mismatch], rows_expected: int, rows_actual: int
) -> Diagnosis | None:
    """More rows in the notebook than in the workbook range: a join multiplied them."""
    if rows_actual <= rows_expected or rows_expected == 0:
        return None

    extra = rows_actual - rows_expected
    ratio = rows_actual / rows_expected
    examples = [m for m in mismatches if m.kind is MismatchKind.EXTRA_ROW]
    evidence = [
        f"The notebook produced {rows_actual} rows where the workbook range holds "
        f"{rows_expected} ({extra} more, a factor of {ratio:.2f}).",
        "An exact VLOOKUP takes the first hit; a polars left join emits one row per match, "
        "so a duplicated key multiplies rows instead of picking one.",
    ]
    if examples:
        evidence.append(f"First surplus row: {examples[0].row}.")

    return Diagnosis(
        cause=DiagnosticCause.JOIN_KEY_NOT_UNIQUE,
        confidence=0.85,
        headline=(
            f"The join key is not unique: {extra} more rows came out of the notebook than "
            f"the workbook range holds."
        ),
        evidence=evidence,
        remedy=(
            "Check the lookup table before joining: "
            'lookup.select(pl.col("key").is_duplicated().any()).item(). Then either '
            'de-duplicate it with .unique(subset="key", keep="first") to match VLOOKUP\'s '
            'first-hit behaviour, or pass validate="m:1" to join() so polars refuses rather '
            "than multiplying rows silently."
        ),
        reference="PLAN 2.6 (VLOOKUP exact match)",
        affected_rows=extra,
    )


def _dates(mismatches: Sequence[Mismatch], column: str | None) -> Diagnosis | None:
    """Whole-day offsets and unconverted serials: the 1900 date system."""
    # `isinstance(..., date)` rather than "not a number", which is what this used to say. The
    # loose form only ever meant "a date" because a VALUE_DIFFERS with a non-numeric expected
    # side carried no delta to filter on; now that text spelling a number is compared as the
    # number, a rate two bps out would answer to the loose test and be reported as a 1900
    # epoch offset.
    day_offsets = [
        m
        for m in mismatches
        if m.absolute_delta is not None
        and m.kind is MismatchKind.VALUE_DIFFERS
        and isinstance(m.expected, date)
        and abs(m.absolute_delta - round(m.absolute_delta)) < 1e-9
        and 1 <= round(m.absolute_delta) <= 2
    ]
    serials = [
        m
        for m in mismatches
        if m.kind is MismatchKind.TYPE_DIFFERS
        and (
            (
                _is_number(m.expected)
                and 1 <= float(m.expected) <= _MAX_SERIAL
                and not _is_number(m.actual)
            )
            or (
                _is_number(m.actual)
                and 1 <= float(m.actual) <= _MAX_SERIAL
                and not _is_number(m.expected)
            )
        )
    ]

    if serials and len(serials) == len(
        [m for m in mismatches if m.kind is MismatchKind.TYPE_DIFFERS]
    ):
        return Diagnosis(
            cause=DiagnosticCause.DATE_SERIAL_OFFSET,
            confidence=0.8,
            headline="A date serial was never converted: one side is a number, the other a date.",
            evidence=[
                f"{len(serials)} row(s) pair a number in Excel's serial range with a date, "
                f"for example row {serials[0].row}: Excel {serials[0].expected!r}, notebook "
                f"{serials[0].actual!r}.",
                "Excel stores dates as serial numbers in the 1900 system; polars has proper "
                "temporal types and will not coerce between them.",
            ],
            remedy=(
                f"Convert explicitly with serial_to_date({_column_expr(column)}) from "
                f"kedge.xl, which implements the 1900 system including the phantom "
                f"1900-02-29 leap day, rather than adding an epoch offset by hand."
            ),
            remedy_symbol="kedge.xl.serial_to_date",
            reference="PLAN 2.6 (dates)",
            affected_rows=len(serials),
        )

    if not day_offsets or len(day_offsets) != len(
        [m for m in mismatches if m.kind is MismatchKind.VALUE_DIFFERS]
    ):
        return None

    offsets = {round(m.absolute_delta) for m in day_offsets if m.absolute_delta is not None}
    if len(offsets) != 1:
        return None
    offset = offsets.pop()
    cause_text = (
        "the 1900 leap-year bug: Excel believes 1900-02-29 existed, so serials for dates "
        "before 1901-03-01 are one day out"
        if offset == 1
        else "the epoch: Excel's serial 1 is 1900-01-01, and an offset of two days is the "
        "classic 1899-12-30 anchor applied twice"
    )
    return Diagnosis(
        cause=DiagnosticCause.DATE_SERIAL_OFFSET,
        confidence=0.85,
        headline=f"Every date is out by exactly {offset} day(s) -- this is {cause_text}.",
        evidence=[
            f"All {len(day_offsets)} differing dates are offset by {offset} day(s) in the "
            f"same direction of magnitude.",
            f"Row {day_offsets[0].row}: Excel {day_offsets[0].expected!r}, notebook "
            f"{day_offsets[0].actual!r}.",
        ],
        remedy=(
            f"Convert serials with serial_to_date({_column_expr(column)}) from kedge.xl "
            f"instead of an offset arithmetic of your own; it carries the 1900 leap-year bug "
            f"deliberately so that pre-1901 dates match the workbook."
        ),
        remedy_symbol="kedge.xl.serial_to_date",
        reference="PLAN 2.6 (dates)",
        affected_rows=len(day_offsets),
    )


def _text_numbers(mismatches: Sequence[Mismatch], column: str | None) -> Diagnosis | None:
    """Digits on one side and a number on the other, where the two are not the same thing.

    **This rule used to fire on the safe case and no longer can.** ``'20.0'`` against ``20.0``
    was a type difference; the engine now reads it as the number Excel reads it as, so a pair
    still reaching here is one :func:`kedge.xl.unambiguous_number` refused -- which means the
    text is an identifier spelled in digits, or a spelling of a number that would not survive
    the round trip. ``00417`` is not 417, and the advice for that is the opposite of the advice
    for a text-formatted quantity: keep the column as text and fix the *numeric* side.
    """
    pairs = [
        m
        for m in mismatches
        if m.kind is MismatchKind.TYPE_DIFFERS
        and (
            (isinstance(m.expected, str) and _is_number(m.actual))
            or (isinstance(m.actual, str) and _is_number(m.expected))
        )
        and _looks_numeric(m.expected if isinstance(m.expected, str) else m.actual)
    ]
    if not pairs:
        return None

    sample = pairs[0].expected if isinstance(pairs[0].expected, str) else pairs[0].actual
    return Diagnosis(
        cause=DiagnosticCause.TEXT_FORMATTED_NUMBER,
        confidence=0.85,
        headline=(
            "One side holds digits that are an identifier and the other holds them as a "
            "number. Reconciliation will not treat the two as equal."
        ),
        evidence=[
            f"{len(pairs)} row(s) pair text with a number, for example row {pairs[0].row}: "
            f"Excel {pairs[0].expected!r}, notebook {pairs[0].actual!r}.",
            f"{sample!r} is text kedge refuses to read as a quantity: a leading zero or more "
            f"than 15 significant digits means an account, a cost centre or a client code, "
            f"and coercing it loses the identity it is made of.",
            "Text that unambiguously spells a number -- '20.0' against 20.0 -- is already "
            "compared as the number Excel coerces it to, so this is not that case.",
        ],
        remedy=(
            f"Keep {_column_expr(column)} as text on both sides rather than coercing it: pass "
            f"it in keep_as_text= to read_data() so the hand-in reader leaves it alone, "
            f"declare it string in the contract, and cast the numeric side to match. If the "
            f"column really is a quantity that merely arrived formatted as text, to_number() "
            f"from kedge.xl is the conversion -- but check the leading zeros first."
        ),
        remedy_symbol=None,
        reference="PLAN 2.6 (text-formatted numbers)",
        affected_rows=len(pairs),
    )


def _looks_numeric(value: Any) -> bool:
    """Whether a string is really a number Excel would have coerced."""
    if not isinstance(value, str):
        return False
    text = value.strip().replace(",", "").replace("£", "").replace("$", "").replace("%", "")
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    if not text or text in ERROR_VALUES:
        return False
    try:
        float(text)
    except ValueError:
        return False
    return True


# =============================================================================
# ENTRY POINT
# =============================================================================


def diagnose(
    mismatches: Sequence[Mismatch],
    *,
    rows_compared: int,
    rows_expected: int = 0,
    rows_actual: int = 0,
    column: str | None = None,
    limit: int = MAX_DIAGNOSES,
) -> list[Diagnosis]:
    """Rank the likely causes of a region's mismatches, with the remedy for each.

    Args:
        mismatches: The differing rows. Pass as many as were collected, not only the ones the
            report will print: a rule that sees four rows out of four hundred cannot tell a
            systematic offset from a coincidence.
        rows_compared: How many rows were compared, which is what the fractions are of.
        rows_expected: Rows in the workbook range.
        rows_actual: Rows the notebook produced.
        column: The notebook column, so the remedies can name it in real code.
        limit: How many hypotheses to return.

    Returns:
        Diagnoses sorted by confidence, most confident first. Empty when nothing matched a
        known signature, which is itself informative: the difference is not one of the
        mechanical Excel-versus-polars gaps and needs reading.
    """
    if not mismatches and rows_actual <= rows_expected:
        return []

    found: list[Diagnosis] = []
    rounding = _rounding(mismatches, column, rows_compared)
    if rounding is not None:
        found.append(rounding)
    found.extend(_nulls(mismatches, column, rows_compared))
    divide = _divide_by_zero(mismatches, column)
    if divide is not None:
        found.append(divide)
    counts = _row_counts(mismatches, rows_expected, rows_actual)
    if counts is not None:
        found.append(counts)
    dates = _dates(mismatches, column)
    if dates is not None:
        found.append(dates)
    text = _text_numbers(mismatches, column)
    if text is not None and not any(
        d.cause is DiagnosticCause.TEXT_FORMATTED_NUMBER for d in found
    ):
        found.append(text)

    found.sort(key=lambda diagnosis: (-diagnosis.confidence, diagnosis.cause.value))
    if found:
        logger.debug("diagnosed %d likely cause(s); top is %s", len(found), found[0].cause.value)
    return found[:limit]
