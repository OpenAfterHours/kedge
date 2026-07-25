"""The Excel 1900 serial date system, including the 1900 leap-year bug.

Excel stores a date as the number of days since an epoch. The 1900 system -- the default
on Windows -- numbers 1900-01-01 as serial 1. That would be unremarkable except that
**Excel believes 1900 was a leap year**. It was not: 1900 is divisible by 100 and not by
400, so the Gregorian calendar skips the leap day. Excel's serial 60 is therefore
1900-02-29, a date that has never existed.

The bug was inherited from Lotus 1-2-3 for compatibility and has been kept ever since,
because fixing it would shift every date in every workbook ever written by one day. The
practical consequence:

===========  ===================  ==========================================
Serial       Excel shows          Reality
===========  ===================  ==========================================
59           1900-02-28           1900-02-28
60           1900-02-29           does not exist
61           1900-03-01           1900-03-01
===========  ===================  ==========================================

So serials 1--59 sit one day *later* than a naive epoch calculation predicts, and serials
61 and above line up with the familiar "days since 1899-12-30" rule. Any conversion that
uses a single offset is off by one for every date before 1900-03-01. PLAN 2.6 calls this
"rare but real", which is fair -- but a workbook with a stray ``0`` in a date column
produces exactly this, and a date that is silently one day out is the kind of thing
reconciliation is for.

**Serial 60 converts to null.** It has no calendar equivalent, and both plausible
substitutions are worse than admitting that: mapping it to 1900-02-28 collides with serial
59, and mapping it to 1900-03-01 collides with serial 61. A null is visible, survives a
profile, and cannot be mistaken for a real date.

Out-of-range serials also convert to null, matching Excel's ``#NUM!``: anything below 1
(Excel displays serial 0 as the non-date "1900-01-00") and anything above 2958465, which
is 9999-12-31 and Excel's ceiling.

The 1904 date system used by legacy Mac workbooks is **not** supported. It has a different
epoch and no leap bug; a workbook using it is detectable from ``workbook.xml`` and should
raise a finding rather than be silently converted with the wrong offsets.
"""

from __future__ import annotations

from typing import Final

import polars as pl

from kedge.xl.rounding import round_half_away

__all__ = [
    "MAX_SERIAL",
    "MIN_SERIAL",
    "PHANTOM_LEAP_DAY_SERIAL",
    "date_to_serial",
    "serial_to_date",
    "serial_to_datetime",
]


#: Excel serial for the phantom 1900-02-29.
PHANTOM_LEAP_DAY_SERIAL: Final[int] = 60

#: Serial 1 is 1900-01-01. Serial 0 is Excel's non-date "1900-01-00".
MIN_SERIAL: Final[int] = 1

#: Serial 2958465 is 9999-12-31, the last date Excel represents.
MAX_SERIAL: Final[int] = 2958465

#: Days between the Unix epoch and Excel's notional epoch for serials at or above 61,
#: i.e. serial = epoch_days + 25569. Verified: 1970-01-01 is Excel serial 25569.
_EPOCH_OFFSET_MODERN: Final[int] = 25569

#: The same offset for serials 1--59, which sit one day later because of the leap bug.
#: Verified: 1900-01-01 is epoch day -25567 and Excel serial 1.
_EPOCH_OFFSET_EARLY: Final[int] = 25568

#: Epoch day number of 1900-03-01, the first date on the modern offset.
_EPOCH_DAY_1900_03_01: Final[int] = -25508

#: Epoch day number of 1900-01-01, the earliest date Excel can represent.
_EPOCH_DAY_1900_01_01: Final[int] = -25567

_MICROSECONDS_PER_DAY: Final[float] = 86_400_000_000.0


def _epoch_days_from_serial(whole_serial: pl.Expr) -> pl.Expr:
    """Map whole Excel serials to days since 1970-01-01, or null where no date exists."""
    before_leap_bug = (whole_serial >= MIN_SERIAL) & (whole_serial < PHANTOM_LEAP_DAY_SERIAL)
    after_leap_bug = (whole_serial > PHANTOM_LEAP_DAY_SERIAL) & (whole_serial <= MAX_SERIAL)
    return (
        pl.when(before_leap_bug)
        .then(whole_serial - _EPOCH_OFFSET_EARLY)
        .when(after_leap_bug)
        .then(whole_serial - _EPOCH_OFFSET_MODERN)
        .otherwise(pl.lit(None, pl.Float64))
    )


def serial_to_date(expr: pl.Expr) -> pl.Expr:
    """Convert Excel 1900 serial numbers to ``Date``, honouring the 1900 leap-year bug.

    Serials 1--59 use the pre-bug offset, 61 and above use the post-bug offset, and 60 --
    the phantom 1900-02-29 -- becomes null. See the module docstring for why.

    A fractional serial is truncated toward the start of the day, matching Excel's ``INT``
    treatment of a date-time. Use ``serial_to_datetime`` to keep the time component.

    Nulls, serials below 1 and serials above 2958465 all return null, matching Excel's
    ``#NUM!``.

    Args:
        expr: A numeric expression holding Excel serial numbers.

    Returns:
        A ``Date`` expression.
    """
    whole = expr.cast(pl.Float64).floor()
    return _epoch_days_from_serial(whole).cast(pl.Int32).cast(pl.Date)


def serial_to_datetime(expr: pl.Expr) -> pl.Expr:
    """Convert fractional Excel serial numbers to ``Datetime``, honouring the leap-year bug.

    Excel stores a time of day as the fractional part of a serial: ``45000.5`` is midday on
    1900-serial day 45000. The date half behaves exactly as ``serial_to_date`` describes,
    including the null at serial 60.

    The fraction is resolved to the nearest microsecond, which is well beyond the precision
    Excel itself carries -- its own time resolution is roughly a millisecond after the
    double has spent its digits on the date part. Rounding uses ``round_half_away`` rather
    than a bare ``.round()`` so that the whole module has one rounding rule.

    A **time-only** column returns null. Excel stores a bare time as a serial below 1, whose
    date part is the non-date "1900-01-00"; there is no datetime to build. Extract the
    fractional part directly and build a ``Duration`` or ``Time`` instead, and profile the
    column first -- a mix of time-only and date-time serials in one column is a data
    problem, not a conversion problem.

    Args:
        expr: A numeric expression holding Excel serial numbers, possibly fractional.

    Returns:
        A ``Datetime`` expression with microsecond precision.
    """
    serial = expr.cast(pl.Float64)
    whole = serial.floor()
    fraction = serial - whole
    microseconds = round_half_away(fraction * _MICROSECONDS_PER_DAY, 0).cast(pl.Int64)
    midnight = _epoch_days_from_serial(whole).cast(pl.Int32).cast(pl.Date).cast(pl.Datetime("us"))
    return midnight + pl.duration(microseconds=microseconds)


def date_to_serial(expr: pl.Expr) -> pl.Expr:
    """Convert dates to Excel 1900 serial numbers, honouring the 1900 leap-year bug.

    The inverse of ``serial_to_date``. Dates from 1900-03-01 onward take the modern offset;
    1900-01-01 to 1900-02-28 take the offset that is one lower, reproducing the gap Excel's
    phantom 1900-02-29 occupies. Serial 60 is therefore never produced -- there is no real
    date that maps to it, which is the round-trip property that makes the pair safe to
    compose.

    Dates before 1900-01-01 return null: Excel cannot represent them in the 1900 system at
    all, and inventing a negative serial would produce a number no workbook would accept.

    Args:
        expr: A ``Date`` or ``Datetime`` expression. A ``Datetime`` is truncated to its
            date; use the fractional form only if the time matters, in which case build it
            from ``serial_to_datetime``'s inverse arithmetic explicitly.

    Returns:
        An ``Int64`` expression holding Excel serial numbers.
    """
    epoch_days = expr.cast(pl.Date).cast(pl.Int32)
    return (
        pl.when(epoch_days >= _EPOCH_DAY_1900_03_01)
        .then(epoch_days + _EPOCH_OFFSET_MODERN)
        .when(epoch_days >= _EPOCH_DAY_1900_01_01)
        .then(epoch_days + _EPOCH_OFFSET_EARLY)
        .otherwise(pl.lit(None, pl.Int32))
        .cast(pl.Int64)
    )
