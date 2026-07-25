"""Excel ground truth for the 1900 serial date system, including the leap-year bug.

The boundary that matters is serials 59, 60 and 61. Excel believes 1900-02-29 exists and
numbers it 60, so every serial below it sits one day later than a single-offset calculation
predicts. Get this wrong and pre-March-1900 dates are silently one day out.
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from kedge.xl import (
    MAX_SERIAL,
    MIN_SERIAL,
    PHANTOM_LEAP_DAY_SERIAL,
    date_to_serial,
    serial_to_date,
    serial_to_datetime,
)

# ══════════════════════════════════════════════════════════════════════════════════════
# EXCEL GROUND TRUTH: serial number to date
#
# These are the dates Excel displays for these serial numbers. The anchors -- 25569 for
# 1970-01-01, 36526 for 2000-01-01, 44927 for 2023-01-01 and 2958465 for 9999-12-31 -- are
# the widely published ones; the rest follow from Excel's rule that serial 1 is 1900-01-01
# and that serial 60 is the phantom 1900-02-29.
#
# kedge converts serial 60 to null. Excel shows 1900-02-29, a date that has never existed,
# and neither neighbouring day can stand in for it without colliding with a real serial.
# ══════════════════════════════════════════════════════════════════════════════════════

EXCEL_SERIAL_TO_DATE_CASES: list[tuple[float | None, dt.date | None]] = [
    # ── the leap-bug boundary, which is the whole reason this module exists
    (58, dt.date(1900, 2, 27)),
    (59, dt.date(1900, 2, 28)),  # last date before the phantom day
    (60, None),  # Excel shows 1900-02-29; it does not exist
    (61, dt.date(1900, 3, 1)),  # naive "epoch + serial" arithmetic gives 1900-02-28 here
    (62, dt.date(1900, 3, 2)),
    # ── the pre-bug range, one offset lower than everything after it
    (1, dt.date(1900, 1, 1)),  # Excel's first representable date
    (2, dt.date(1900, 1, 2)),
    (31, dt.date(1900, 1, 31)),
    (32, dt.date(1900, 2, 1)),
    # ── the post-bug range
    (366, dt.date(1900, 12, 31)),  # 1900 has 366 serials thanks to the phantom day
    (367, dt.date(1901, 1, 1)),
    (25569, dt.date(1970, 1, 1)),  # the Unix epoch, the usual sanity anchor
    (36526, dt.date(2000, 1, 1)),
    (43831, dt.date(2020, 1, 1)),
    (44927, dt.date(2023, 1, 1)),
    (45000, dt.date(2023, 3, 15)),
    (2958465, dt.date(9999, 12, 31)),  # Excel's last representable date
    # ── outside the representable range: Excel gives #NUM!
    (0, None),  # Excel displays serial 0 as the non-date "1900-01-00"
    (-1, None),
    (2958466, None),
    (None, None),
    # ── fractional serials truncate to the start of the day, as Excel's INT does
    (45000.75, dt.date(2023, 3, 15)),
    (61.999, dt.date(1900, 3, 1)),
    (60.5, None),  # still the phantom day
]

# ══════════════════════════════════════════════════════════════════════════════════════
# EXCEL GROUND TRUTH: date to serial number
#
# The inverse. Note that serial 60 is unreachable -- no real date maps to it -- which is
# the property that makes the round trip safe.
# ══════════════════════════════════════════════════════════════════════════════════════

EXCEL_DATE_TO_SERIAL_CASES: list[tuple[dt.date | None, int | None]] = [
    (dt.date(1900, 1, 1), 1),
    (dt.date(1900, 2, 27), 58),
    (dt.date(1900, 2, 28), 59),
    (dt.date(1900, 3, 1), 61),  # 60 is skipped: the phantom day sits in the gap
    (dt.date(1900, 3, 2), 62),
    (dt.date(1900, 12, 31), 366),
    (dt.date(1901, 1, 1), 367),
    (dt.date(1970, 1, 1), 25569),
    (dt.date(2000, 1, 1), 36526),
    (dt.date(2023, 3, 15), 45000),
    (dt.date(9999, 12, 31), 2958465),
    (dt.date(1899, 12, 31), None),  # Excel cannot represent it in the 1900 system
    (dt.date(1899, 12, 30), None),
    (dt.date(1066, 10, 14), None),
    (None, None),
]

EXCEL_SERIAL_TO_DATETIME_CASES: list[tuple[float | None, dt.datetime | None]] = [
    (45000.0, dt.datetime(2023, 3, 15, 0, 0, 0)),  # midnight
    (45000.25, dt.datetime(2023, 3, 15, 6, 0, 0)),
    (45000.5, dt.datetime(2023, 3, 15, 12, 0, 0)),  # midday
    (45000.75, dt.datetime(2023, 3, 15, 18, 0, 0)),
    (59.5, dt.datetime(1900, 2, 28, 12, 0, 0)),  # pre-bug offset carries through
    (61.5, dt.datetime(1900, 3, 1, 12, 0, 0)),  # post-bug offset carries through
    (60.5, None),  # the phantom day, at any time of day
    (0.5, None),  # a time-only serial has no date; see the docstring
    (None, None),
]


def _evaluate(expr: pl.Expr, value: object, dtype: pl.DataType) -> object:
    return pl.LazyFrame({"x": [value]}, schema={"x": dtype}).select(result=expr).collect().item()


# ── serial to date ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("serial", "expected"), EXCEL_SERIAL_TO_DATE_CASES)
def test_serial_to_date_should_match_excel(serial: float | None, expected: dt.date | None) -> None:
    """`serial_to_date` should reproduce Excel's 1900 system for every ground-truth case."""
    assert _evaluate(serial_to_date(pl.col("x")), serial, pl.Float64) == expected


def test_serial_to_date_should_null_the_phantom_leap_day() -> None:
    """Serial 60 is 1900-02-29, which never existed, so it must not become a real date.

    Stated as its own test because the alternative implementations -- mapping it to
    1900-02-28 or 1900-03-01 -- would each collide with a genuine serial and are the exact
    off-by-one PLAN 2.6 warns about.
    """
    frame = pl.LazyFrame({"x": [59, PHANTOM_LEAP_DAY_SERIAL, 61]})
    result = frame.select(serial_to_date(pl.col("x"))).collect().to_series().to_list()
    assert result == [dt.date(1900, 2, 28), None, dt.date(1900, 3, 1)]


def test_serial_to_date_should_return_a_date_column() -> None:
    """The output dtype should be Date, not an integer that merely looks like one."""
    frame = pl.LazyFrame({"x": [45000]})
    assert frame.select(serial_to_date(pl.col("x"))).collect_schema()["x"] == pl.Date


def test_serial_to_date_should_reject_serials_outside_excels_range() -> None:
    """Serials Excel answers with #NUM! should be null, not a wildly wrong date."""
    frame = pl.LazyFrame({"x": [MIN_SERIAL - 1, MIN_SERIAL, MAX_SERIAL, MAX_SERIAL + 1]})
    result = frame.select(serial_to_date(pl.col("x"))).collect().to_series().to_list()
    assert result == [None, dt.date(1900, 1, 1), dt.date(9999, 12, 31), None]


# ── date to serial ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("date", "expected"), EXCEL_DATE_TO_SERIAL_CASES)
def test_date_to_serial_should_match_excel(date: dt.date | None, expected: int | None) -> None:
    """`date_to_serial` should reproduce Excel's serial numbers for every ground-truth case."""
    assert _evaluate(date_to_serial(pl.col("x")), date, pl.Date) == expected


def test_date_to_serial_should_never_produce_the_phantom_serial() -> None:
    """No real date maps to serial 60, so scanning the whole of 1900 must never emit it."""
    dates = [dt.date(1900, 1, 1) + dt.timedelta(days=offset) for offset in range(365)]
    serials = (
        pl.LazyFrame({"x": dates}, schema={"x": pl.Date})
        .select(date_to_serial(pl.col("x")))
        .collect()
        .to_series()
        .to_list()
    )
    assert PHANTOM_LEAP_DAY_SERIAL not in serials
    assert serials[:3] == [1, 2, 3]
    assert max(serials) == 366


def test_date_to_serial_and_serial_to_date_should_round_trip() -> None:
    """The pair should compose to the identity for every date Excel can represent."""
    dates = [
        dt.date(1900, 1, 1),
        dt.date(1900, 2, 28),
        dt.date(1900, 3, 1),
        dt.date(1901, 1, 1),
        dt.date(1970, 1, 1),
        dt.date(2023, 3, 15),
        dt.date(9999, 12, 31),
        None,
    ]
    frame = pl.LazyFrame({"x": dates}, schema={"x": pl.Date})
    result = (
        frame.select(serial_to_date(date_to_serial(pl.col("x")))).collect().to_series().to_list()
    )
    assert result == dates


# ── serial to datetime ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("serial", "expected"), EXCEL_SERIAL_TO_DATETIME_CASES)
def test_serial_to_datetime_should_match_excel(
    serial: float | None, expected: dt.datetime | None
) -> None:
    """`serial_to_datetime` should carry the time of day as Excel's fractional part."""
    assert _evaluate(serial_to_datetime(pl.col("x")), serial, pl.Float64) == expected


def test_serial_to_datetime_should_resolve_seconds() -> None:
    """A one-second fraction should land on the second, not a microsecond either side."""
    one_second = 1.0 / 86_400.0
    frame = pl.LazyFrame({"x": [45000.0 + one_second, 45000.0 + 3661 * one_second]})
    result = frame.select(serial_to_datetime(pl.col("x"))).collect().to_series().to_list()
    assert result == [
        dt.datetime(2023, 3, 15, 0, 0, 1),
        dt.datetime(2023, 3, 15, 1, 1, 1),
    ]


# ══════════════════════════════════════════════════════════════════════════════════════
# TRIPWIRE
#
# The naive conversion, asserted so the cost of getting it wrong stays visible in the
# suite rather than being described in a comment.
# ══════════════════════════════════════════════════════════════════════════════════════


def test_naive_single_offset_conversion_should_still_be_wrong_before_march_1900() -> None:
    """A single-offset epoch calculation should still be one day out for serials 1 to 59.

    This is the whole leap-bug problem in one assertion: "1899-12-30 plus serial days" is
    correct from serial 61 onward and wrong before it.
    """
    frame = pl.LazyFrame({"x": [1, 59, 61, 45000]})
    naive = (
        frame.select(pl.lit(dt.date(1899, 12, 30)) + pl.duration(days=pl.col("x")))
        .collect()
        .to_series()
        .to_list()
    )
    correct = frame.select(serial_to_date(pl.col("x"))).collect().to_series().to_list()

    assert naive[0] == dt.date(1899, 12, 31)  # wrong: Excel says 1900-01-01
    assert naive[1] == dt.date(1900, 2, 27)  # wrong: Excel says 1900-02-28
    assert naive[2] == correct[2] == dt.date(1900, 3, 1)  # agrees from here on
    assert naive[3] == correct[3] == dt.date(2023, 3, 15)
    assert correct[0] == dt.date(1900, 1, 1)
    assert correct[1] == dt.date(1900, 2, 28)
