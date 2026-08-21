"""Profile a hand-in, and diff its shape against the last accepted one.

Hand-ins from another team change without warning: a column renamed, an extra preamble row,
a totals row appended at the bottom, a date column that arrives as text this month. In the
Excel process this replaces, every one of those produces a quietly wrong number rather than
an error, which is why this module is a large part of the practical value on offer.

Two rules shape it.

**Report drift before the contract check.** "Column ``EAD`` became ``EAD_GBP``" is a far
more useful message than a dtype mismatch three checks deep in a schema report, so
:func:`check_drift` runs first and the contract cell runs after it (PLAN 2.8).

**A rename is one finding, not two.** A drop plus an add is technically accurate and
practically useless -- the reader has to work out for themselves that the two are the same
column. :func:`compare` pairs unmatched columns with a similarity score before reporting
them, so the common case reads as the single sentence it is. The scoring is deliberately
explicit and weighted rather than clever, so a surprising pairing can be reasoned about.

The one drift that gets error severity is a numeric column arriving as text. That is the
silent join-failure cause from PLAN 2.6: the join key stops matching, rows vanish, and the
total is simply smaller than it should be with nothing anywhere saying so.
"""

from __future__ import annotations

import csv
import difflib
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import fastexcel
import polars as pl

from kedge.analysis.model import ColumnProfile, NumericStats, Severity
from kedge.errors import IngestError
from kedge.ingest import store
from kedge.ingest.coerce import Coercion, coerce_numeric_text
from kedge.ingest.model import (
    DriftItem,
    DriftKind,
    DriftReport,
    HandIn,
    HandInProfile,
    sort_drift,
    utcnow,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from kedge.config import RedactionConfig, SamplingConfig

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_NULL_RATE_TOLERANCE",
    "DEFAULT_ROW_COUNT_TOLERANCE",
    "RENAME_THRESHOLD",
    "HandInReadError",
    "Layout",
    "check_drift",
    "compare",
    "detect_layout",
    "detect_totals_row",
    "dtype_name",
    "profile_frame",
    "profile_handin",
    "read_data",
    "read_frame",
]

DEFAULT_ROW_COUNT_TOLERANCE = 0.20
"""Relative row-count change that counts as drift. Monthly extracts breathe; 20% is a lurch."""

DEFAULT_NULL_RATE_TOLERANCE = 0.10
"""Null-rate change, in absolute proportion, that counts as drift."""

RENAME_THRESHOLD = 0.60
"""Similarity at or above which a removed and an added column are reported as one rename."""

_EXCEL_SUFFIXES = frozenset({".xlsx", ".xlsm", ".xlsb", ".xls", ".ods"})
_CSV_SUFFIXES = frozenset({".csv", ".txt"})
_TSV_SUFFIXES = frozenset({".tsv", ".tab"})
_PARQUET_SUFFIXES = frozenset({".parquet", ".pq"})

_READ_ERRORS: tuple[type[Exception], ...] = (
    OSError,
    ValueError,
    pl.exceptions.PolarsError,
    fastexcel.FastExcelError,
)
"""Everything a reader raises when a file will not parse.

``fastexcel.FastExcelError`` is in the list because calamine's errors are fastexcel's own
type rather than a ``PolarsError``: a workbook that is not a zip archive raises
``CalamineError`` straight out through ``pl.read_excel``, and without this a hand-in that is
not really a spreadsheet would reach the user as a bare traceback instead of a
:class:`HandInReadError` (CONVENTIONS non-negotiable 4).
"""

_LAYOUT_SCAN_ROWS = 25
_NUMERIC_STRIP = re.compile(r"[,\s_$£€%()]")
_ALPHANUMERIC = re.compile(r"[^a-z0-9]+")
_TOTALS_LABELS = frozenset(
    {
        "total",
        "totals",
        "grand total",
        "grand totals",
        "sum",
        "subtotal",
        "sub-total",
        "sub total",
        "all",
    }
)
_MIN_ROWS_FOR_ARITHMETIC_TOTALS = 5
_NUMERIC_DTYPES = frozenset(
    {
        "Int8",
        "Int16",
        "Int32",
        "Int64",
        "Int128",
        "UInt8",
        "UInt16",
        "UInt32",
        "UInt64",
        "Float32",
        "Float64",
        "Decimal",
    }
)
_TEMPORAL_DTYPES = frozenset({"Date", "Datetime", "Time", "Duration"})


class HandInReadError(IngestError):
    """A hand-in could not be read into a frame."""


# =============================================================================
# READING
# =============================================================================


def _without_time_zones(frame: pl.DataFrame) -> pl.DataFrame:
    """Return the frame with every timezone-aware Datetime column converted to naive UTC.

    Nothing downstream wants the zone -- :func:`dtype_name` collapses
    ``Datetime(time_zone='UTC')`` to "Datetime" and a profile records instants, not offsets --
    but a great deal downstream turns cells into Python objects, and polars builds a
    :class:`zoneinfo.ZoneInfo` to do it. On an interpreter with no IANA database (a Windows
    CPython without ``tzdata``, which is the ordinary case) that fails inside Rust and arrives
    as ``pyo3_runtime.PanicException`` -- a ``BaseException``, so it walks straight past
    :data:`_READ_ERRORS` and every other degrade-gracefully path here and takes the process
    with it. It is not hypothetical: a parquet hand-in with an ``as_of`` column killed
    :func:`detect_totals_row` on ``frame.row(-1)`` before profiling was even reached.

    Converting to UTC rather than simply dropping the zone keeps the instant exactly, and
    keeps two months' hand-ins comparable when the sending team switches zone.
    """
    aware = [
        name
        for name, dtype in frame.schema.items()
        if isinstance(dtype, pl.Datetime) and dtype.time_zone is not None
    ]
    if not aware:
        return frame
    logger.debug("converting %d timezone-aware column(s) to naive UTC", len(aware))
    return frame.with_columns(
        pl.col(name).dt.convert_time_zone("UTC").dt.replace_time_zone(None) for name in aware
    )


def read_frame(
    path: Path,
    *,
    sheet: str | None = None,
    header_row: int | None = None,
) -> pl.DataFrame:
    """Read a hand-in into an eager polars frame.

    Excel goes through ``pl.read_excel``, which defaults to the calamine engine -- fast, and
    with no Excel install required (PLAN 2.5). CSV and parquet are handled directly.

    Args:
        path: The managed hand-in path.
        sheet: Worksheet name for spreadsheet formats. ``None`` reads the first sheet.
        header_row: Zero-based index of the header row, skipping any preamble above it.
            ``None`` means row 0. :func:`detect_layout` works this out from the file.

    Returns:
        The frame, headers applied.

    Raises:
        HandInReadError: If the format is unsupported or the file will not parse.
    """
    suffix = path.suffix.lower()
    skip = header_row or 0
    try:
        if suffix in _EXCEL_SUFFIXES:
            options = {"header_row": skip} if skip else None
            read = pl.read_excel(path, sheet_name=sheet, read_options=options)
            frame = read if isinstance(read, pl.DataFrame) else next(iter(read.values()))
            return _without_time_zones(frame)
        if suffix in _CSV_SUFFIXES or suffix in _TSV_SUFFIXES:
            separator = "\t" if suffix in _TSV_SUFFIXES else ","
            return _without_time_zones(pl.read_csv(path, separator=separator, skip_rows=skip))
        if suffix in _PARQUET_SUFFIXES:
            return _without_time_zones(pl.read_parquet(path))
    except _READ_ERRORS as exc:
        msg = f"could not read the hand-in {path.name}: {exc}"
        raise HandInReadError(msg) from exc

    supported = sorted(_EXCEL_SUFFIXES | _CSV_SUFFIXES | _TSV_SUFFIXES | _PARQUET_SUFFIXES)
    msg = (
        f"cannot read a hand-in with extension '{suffix}' ({path.name}). "
        f"Supported: {', '.join(supported)}."
    )
    raise HandInReadError(msg)


def _read_raw(path: Path, *, sheet: str | None, rows: int) -> list[tuple[Any, ...]]:
    """Read the top of a file with no header applied, every value as text."""
    suffix = path.suffix.lower()
    if suffix in _CSV_SUFFIXES or suffix in _TSV_SUFFIXES:
        return _read_raw_delimited(
            path, separator="\t" if suffix in _TSV_SUFFIXES else ",", rows=rows
        )
    if suffix not in _EXCEL_SUFFIXES:
        return []
    try:
        frame = pl.read_excel(
            path,
            sheet_name=sheet,
            has_header=False,
            drop_empty_rows=False,
            drop_empty_cols=False,
        )
        raw = frame if isinstance(frame, pl.DataFrame) else next(iter(frame.values()))
    except _READ_ERRORS as exc:
        logger.warning("could not scan %s for preamble rows: %s", path.name, exc)
        return []
    text = raw.head(rows).select(pl.all().cast(pl.String, strict=False))
    return list(text.iter_rows())


def _read_raw_delimited(path: Path, *, separator: str, rows: int) -> list[tuple[Any, ...]]:
    """Read the top rows of a delimited file with the stdlib reader, one width per row.

    Deliberately not ``pl.read_csv``: with ``has_header=False`` polars fixes the frame's
    width from the first line, so a one-cell title sitting above the header truncates every
    data row to one column and the preamble becomes invisible to :func:`detect_layout` --
    which then reads the title as the header and the file will not parse at all. An extra
    preamble row is one of the named drift cases in PLAN 2.8, so the scan has to see it.
    """
    try:
        # utf-8-sig because an Excel-exported CSV carries a byte-order mark, and polars
        # strips it: leaving it on here would make the scan's view of the first cell differ
        # from the one read_frame goes on to produce.
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle, delimiter=separator)
            return [tuple(row) for _, row in zip(range(rows), reader, strict=False)]
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        logger.warning("could not scan %s for preamble rows: %s", path.name, exc)
        return []


def _looks_numeric(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    candidate = _NUMERIC_STRIP.sub("", stripped)
    if not candidate or candidate in ("-", "+", "."):
        return False
    try:
        float(candidate)
    except ValueError:
        return False
    return True


def _filled_width(row: tuple[Any, ...]) -> int:
    """How many cells of a scanned row carry anything at all."""
    return sum(1 for cell in row if cell is not None and str(cell).strip())


def _blocks(row: tuple[Any, ...]) -> list[list[str]]:
    """The row split into runs of populated cells, separated by empty ones.

    A spreadsheet is not a table; it is a sheet with tables drawn on it. A grid with a small
    block of scalars parked to its right -- ``Minimum fee | 750``, ``Maximum fee | 250000`` --
    is completely ordinary, and the blank column between them is what a reader uses to tell the
    two apart. So does this.
    """
    blocks: list[list[str]] = []
    current: list[str] = []
    for cell in row:
        text = "" if cell is None else str(cell).strip()
        if text:
            current.append(text)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _principal_block(row: tuple[Any, ...]) -> list[str]:
    """The run of populated cells most likely to be the grid: the widest, leftmost on a tie."""
    return max(_blocks(row), key=len, default=[])


def _usual_field_count(rows: list[tuple[Any, ...]]) -> int:
    """How many fields the scanned rows mostly agree on.

    The modal count, not the maximum: a delimited file with one malformed row has exactly one
    row wider than the rest, and the majority is what the file is really shaped like. Ties
    break towards the narrower count, so a two-line file whose second line is ragged still
    reads as "line one is the header" -- and so fails loudly on the read rather than promoting
    the ragged line.
    """
    counts = Counter(len(row) for row in rows)
    return min(counts, key=lambda width: (-counts[width], width))


def detect_layout(path: Path, *, sheet: str | None = None) -> tuple[int, int]:
    """Work out which row carries the headers, and how many preamble rows sit above it.

    An extra preamble row -- a title, a "generated on" stamp, a blank -- is one of the most
    common ways a hand-in drifts, and it shifts every column by a row without changing
    anything a schema check would notice.

    The header is taken to be the first row that is as wide as the widest row scanned, whose
    cells are all distinct, and none of whose *principal block* parses as a number. That last
    condition is what stops an all-text data row being mistaken for a header. Rows carrying
    more fields than the file otherwise agrees on are ruled out before any of that: an
    unquoted delimiter inside a text value ("Acme, Inc") makes one data row wider than every
    other, and the widest row is then the *malformed* one. Promoting it would discard the real
    header as preamble and lose a row from the frame -- where the honest outcome is the loud
    read failure a ragged file deserves.

    **Why the principal block rather than the whole row.** A worksheet is a sheet with tables
    drawn on it, not a table, and a small block of scalars parked beside the grid -- ``Minimum
    fee | 750``, ``Maximum fee | 250000``, in the two columns to the right of a rate card -- is
    completely ordinary. Testing every populated cell for numerality rejected the header row
    for carrying ``250000`` three columns away from it, and the fallback then read the sheet's
    *title* as the header, so a real hand-in parsed as one unusable column. The blank column
    between the two blocks is what a person uses to tell them apart, and :func:`_blocks` is
    that reading: the numeric test applies to the widest run of adjacent cells, which is the
    grid, and a detached caption-and-scalar block beside it no longer disqualifies the row.
    Distinctness is still checked across the whole row, because two identical labels anywhere
    in it break the read whichever block they sit in.

    Args:
        path: The file to inspect.
        sheet: Worksheet name for spreadsheet formats.

    Returns:
        ``(header_row, preamble_rows)``, both zero-based; ``(0, 0)`` when the file starts
        with its headers or the layout cannot be determined.
    """
    rows = _read_raw(path, sheet=sheet, rows=_LAYOUT_SCAN_ROWS)
    if not rows:
        return 0, 0
    # Every row a spreadsheet scan returns is the same length, so this rules out nothing
    # there; it is the delimited reader, which reports each line at its own width, that
    # needs it.
    usual = _usual_field_count(rows)
    scanned = [(index, row) for index, row in enumerate(rows) if len(row) <= usual]
    widths = {index: _filled_width(row) for index, row in scanned}
    widest = max(widths.values(), default=0)
    if widest == 0:
        return 0, 0
    for index, row in scanned:
        if widths[index] < widest:
            continue
        values = [str(cell).strip() for cell in row if cell is not None and str(cell).strip()]
        if any(_looks_numeric(value) for value in _principal_block(row)):
            continue
        if len({value.lower() for value in values}) != len(values):
            continue
        return index, index
    return 0, 0


def detect_totals_row(frame: pl.DataFrame) -> bool:
    """Whether the last row of a frame is a totals row rather than data.

    Two signals. A text cell that reads exactly "Total" (or one of its siblings) is the
    obvious one and is sufficient on its own. The other is arithmetic, for the totals row
    somebody appended without labelling, and it is deliberately hard to satisfy: a detected
    totals row is *excluded from the profile*, so a false positive quietly discards a real
    row of data, which is a worse outcome than missing an unlabelled total. It therefore
    requires a reasonable number of rows, agreement across every numeric column that could
    disagree, and a corroborating blank in a text column that is otherwise fully populated --
    the gap where the counterparty name would be.

    Args:
        frame: The frame as read, headers applied.

    Returns:
        True when the final row looks like a total.
    """
    if frame.height < 2:
        return False
    if _has_totals_label(frame):
        return True
    return _sums_to_the_rows_above(frame) and _has_corroborating_blank(frame)


def _has_totals_label(frame: pl.DataFrame) -> bool:
    """Whether any cell of the last row is nothing but a totals word.

    Matched exactly rather than by prefix: a counterparty called "Total Energies SE" is a
    row of data, not a summary.
    """
    return any(
        isinstance(value, str) and value.strip().lower().rstrip(":.").strip() in _TOTALS_LABELS
        for value in frame.row(-1)
    )


def _sums_to_the_rows_above(frame: pl.DataFrame) -> bool:
    """Whether every summable numeric column's last value equals the sum of the rows above."""
    if frame.height < _MIN_ROWS_FOR_ARITHMETIC_TOTALS:
        return False
    agreed = False
    for name, dtype in frame.schema.items():
        if not dtype.is_numeric():
            continue
        series = frame[name]
        last, above = series[-1], series[:-1].sum()
        if last is None or above is None or float(above) == 0.0:
            continue
        if not math.isclose(float(last), float(above), rel_tol=1e-9, abs_tol=1e-6):
            return False
        agreed = True
    return agreed


def _has_corroborating_blank(frame: pl.DataFrame) -> bool:
    """Whether a text column that is otherwise complete is empty on the last row."""
    for name, dtype in frame.schema.items():
        if dtype.is_numeric():
            continue
        series = frame[name]
        last = series[-1]
        if last is not None and str(last).strip():
            continue
        if series[:-1].null_count() == 0:
            return True
    return False


@dataclass(frozen=True, slots=True)
class Layout:
    """What reading a hand-in had to work around to get at its data."""

    header_row: int
    preamble_rows: int
    totals_row_excluded: bool
    coercions: tuple[Coercion, ...] = ()
    """Text columns read as numbers. See :mod:`kedge.ingest.coerce` for what qualifies.

    Carried here rather than reported separately because this is already the record of "what
    had to be done to the file to get at its data", and a type conversion is exactly that. It
    also means the notebook's existing layout panel surfaces it with no new wiring -- a
    conversion nobody was told about is the thing worth avoiding.
    """

    def notes(self) -> list[str]:
        """Return plain-English notes on anything non-obvious about the layout."""
        notes: list[str] = []
        if self.preamble_rows:
            notes.append(f"skipped {self.preamble_rows} preamble row(s) above the header")
        if self.totals_row_excluded:
            notes.append("excluded a totals row from the bottom")
        notes.extend(coercion.note() for coercion in self.coercions)
        return notes


def read_data(
    path: Path,
    *,
    sheet: str | None = None,
    header_row: int | None = None,
    coerce: bool = True,
    keep_as_text: Sequence[str] = (),
) -> tuple[pl.DataFrame, Layout]:
    """Read the *data* of a hand-in: preamble skipped, totals row excluded.

    The single reader that profiling, contract validation and contract inference all go
    through, so that the three of them cannot disagree about what the file contains. That
    matters more than it sounds: if inference profiled 400 rows and validation checked 401,
    a generated contract would fail against the very file it was generated from.

    Args:
        path: The hand-in to read.
        sheet: Worksheet name for spreadsheet formats. ``None`` reads the first sheet.
        header_row: Override the detected header row.
        coerce: Read unambiguously numeric text columns as numbers, reporting each on the
            layout. On by default, because a hand-in that arrived through the paste box is all
            text and the alternative is an arithmetic error four operations downstream.
        keep_as_text: Columns to leave as text whatever they look like. A contract that declares
            a column ``string`` passes its name here: the agreement outranks the guess.

    Returns:
        ``(frame, layout)`` -- the data, what had to be skipped to reach it, and what had to be
        converted.

    Raises:
        HandInReadError: If the file cannot be read.
    """
    if header_row is None:
        header_row, preamble = detect_layout(path, sheet=sheet)
    else:
        preamble = header_row
    frame = read_frame(path, sheet=sheet, header_row=header_row)
    has_totals = detect_totals_row(frame)
    if has_totals:
        logger.info("excluding a totals row from the data view of %s", path.name)
        frame = frame.head(frame.height - 1)

    # Typed after the totals row is dropped, not before: a totals row often carries a label in
    # the first column and a formatted number in the rest, and judging a column's type against a
    # row that is not data is how a numeric column gets left as text.
    coercions: tuple[Coercion, ...] = ()
    if coerce:
        frame, coercions = coerce_numeric_text(frame, skip=keep_as_text)

    return frame, Layout(
        header_row=header_row,
        preamble_rows=preamble,
        totals_row_excluded=has_totals,
        coercions=coercions,
    )


# =============================================================================
# PROFILING
# =============================================================================


def dtype_name(dtype: Any) -> str:
    """Return a stable, comparable name for a polars dtype, e.g. ``'Float64'``.

    Parameterised dtypes collapse to their base name, so ``Datetime(time_unit='us')`` and
    ``Datetime(time_unit='ms')`` compare equal. A contract that cares about the unit says so
    in prose; a contract that says "Datetime" means "a datetime".
    """
    try:
        return str(dtype.base_type().__name__)
    except AttributeError:  # pragma: no cover - defensive
        return str(dtype)


def _column_letter(index: int) -> str:
    """Return the Excel column letter for a zero-based index: 0 -> A, 26 -> AA."""
    letters = ""
    position = index + 1
    while position > 0:
        position, remainder = divmod(position - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _jsonable(value: Any) -> Any:
    """Reduce a cell value to something that survives a JSON round trip.

    Profiles are persisted and travel to the model, so a ``datetime.date`` in a sample list
    would break both. Converting here keeps every consumer simple.
    """
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    return str(value)


def _is_redacted(header: str, redaction: RedactionConfig | None) -> bool:
    if redaction is None or not redaction.enabled:
        return False
    return any(re.search(pattern, header, re.IGNORECASE) for pattern in redaction.column_patterns)


def _numeric_stats(series: pl.Series) -> NumericStats:
    values = series.drop_nulls()
    return NumericStats(
        min=_finite(values.min()),
        max=_finite(values.max()),
        mean=_finite(values.mean()),
        sum=_finite(values.sum()),
        zero_count=int((values == 0).sum()),
        negative_count=int((values < 0).sum()),
    )


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _format_anomalies(series: pl.Series, dtype: pl.DataType) -> list[str]:
    """Spot the two text-shaped problems that produce silently wrong numbers downstream."""
    if dtype != pl.String:
        return []
    values = series.drop_nulls()
    if values.len() == 0:
        return []
    sample = values.head(1000).to_list()
    numeric = sum(1 for value in sample if _looks_numeric(str(value)))
    anomalies: list[str] = []
    if numeric >= 0.8 * len(sample):
        anomalies.append("numbers stored as text")
    if any(str(value) != str(value).strip() for value in sample):
        anomalies.append("values carry leading or trailing whitespace")
    return anomalies


def _profile_column(
    frame: pl.DataFrame,
    name: str,
    index: int,
    *,
    sheet: str,
    sampling: SamplingConfig | None,
    redaction: RedactionConfig | None,
    seed: int,
) -> ColumnProfile:
    series = frame[name]
    dtype = frame.schema[name]
    head_rows = 5 if sampling is None else sampling.head_rows
    tail_rows = 5 if sampling is None else sampling.tail_rows
    random_rows = 5 if sampling is None else sampling.random_rows
    top_k = 10 if sampling is None else sampling.top_k

    if _is_redacted(name, redaction):
        # Dtype and null count still travel; values do not (PLAN 2.3).
        return ColumnProfile(
            sheet=sheet,
            column=_column_letter(index),
            index=index,
            header=name,
            dtype=dtype_name(dtype),
            row_count=frame.height,
            null_count=int(series.null_count()),
            distinct_count=int(series.n_unique()),
            redacted=True,
        )

    # Renamed before counting: value_counts names its tally column "count", and a hand-in
    # column already called "count" -- an ordinary thing for an extract to contain -- makes
    # polars raise DuplicateError rather than produce a profile. Fixing both names here means
    # neither can collide with whatever the sending team called theirs.
    counts = (
        series.drop_nulls().rename("value").value_counts(sort=True, name="count").head(top_k)
        if frame.height
        else pl.DataFrame()
    )
    frequent = (
        [(_jsonable(row[0]), int(row[1])) for row in counts.iter_rows()] if counts.height else []
    )

    sample_size = min(random_rows, frame.height)
    sample = series.sample(n=sample_size, seed=seed).to_list() if sample_size else []

    return ColumnProfile(
        sheet=sheet,
        column=_column_letter(index),
        index=index,
        header=name,
        dtype=dtype_name(dtype),
        row_count=frame.height,
        null_count=int(series.null_count()),
        distinct_count=int(series.n_unique()),
        numeric=_numeric_stats(series) if dtype.is_numeric() else None,
        top_k=frequent,
        head=[_jsonable(v) for v in series.head(head_rows).to_list()],
        tail=[_jsonable(v) for v in series.tail(tail_rows).to_list()],
        sample=[_jsonable(v) for v in sample],
        format_anomalies=_format_anomalies(series, dtype),
    )


def profile_frame(
    frame: pl.DataFrame,
    *,
    sheet: str = "",
    sampling: SamplingConfig | None = None,
    redaction: RedactionConfig | None = None,
    seed: int = 0,
) -> list[ColumnProfile]:
    """Profile every column of a frame, reusing the analyser's ``ColumnProfile`` contract.

    Frames read by this module arrive with no timezone-aware column, but this one is public
    and takes any frame, so it repeats the conversion rather than trusting its caller -- see
    :func:`_without_time_zones` for what happens if a tz-aware column reaches ``to_list``.
    """
    frame = _without_time_zones(frame)
    return [
        _profile_column(
            frame, name, index, sheet=sheet, sampling=sampling, redaction=redaction, seed=seed
        )
        for index, name in enumerate(frame.columns)
    ]


def profile_handin(
    handin: HandIn,
    *,
    sheet: str | None = None,
    header_row: int | None = None,
    sampling: SamplingConfig | None = None,
    redaction: RedactionConfig | None = None,
    seed: int = 0,
) -> HandInProfile:
    """Read a hand-in and describe its shape.

    Profiling on ingest serves two readers at once: the user sees what actually arrived
    before any processing happens, and the agent gets the schema in context, so it writes
    code against the real columns rather than guessed ones (PLAN 2.8).

    A detected totals row is excluded from the profile. It is layout, not data, and leaving
    it in would skew every sum and maximum -- which next month would surface as several
    columns' worth of statistical drift instead of the single finding that is actually true.

    Args:
        handin: The received hand-in. Its managed path is what gets read.
        sheet: Worksheet name for spreadsheet formats. ``None`` reads the first sheet.
        header_row: Override the detected header row.
        sampling: Row and top-k caps; defaults to the built-in profile sizes.
        redaction: Optional column masking, off unless configured (PLAN 2.3).
        seed: Seed for the random sample, so a profile is reproducible.

    Returns:
        The hand-in's :class:`~kedge.ingest.model.HandInProfile`.

    Raises:
        HandInReadError: If the file cannot be read.
    """
    frame, layout = read_data(handin.path, sheet=sheet, header_row=header_row)
    return HandInProfile(
        sha256=handin.sha256,
        original_name=handin.original_name,
        profiled_at=utcnow(),
        sheet=sheet or "",
        row_count=frame.height,
        column_count=frame.width,
        header_row=layout.header_row,
        preamble_rows=layout.preamble_rows,
        has_totals_row=layout.totals_row_excluded,
        columns=tuple(
            profile_frame(
                frame,
                sheet=sheet or "",
                sampling=sampling,
                redaction=redaction,
                seed=seed,
            )
        ),
    )


# =============================================================================
# RENAME DETECTION
# =============================================================================


def _normalise_name(name: str) -> str:
    return _ALPHANUMERIC.sub("", name.lower())


def _name_similarity(left: str, right: str) -> float:
    """Similarity of two column names, ignoring case, spacing and punctuation.

    Floored at 0.85 when one name is a prefix or suffix of the other, because that is what a
    unit or currency suffix looks like -- ``EAD`` becoming ``EAD_GBP`` -- and it is the single
    most common way a hand-in column is renamed.
    """
    a, b = _normalise_name(left), _normalise_name(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= 3 and (longer.startswith(shorter) or longer.endswith(shorter)):
        return max(ratio, 0.85)
    return ratio


def _dtype_similarity(left: ColumnProfile, right: ColumnProfile) -> float:
    if left.dtype == right.dtype:
        return 1.0
    for family in (_NUMERIC_DTYPES, _TEMPORAL_DTYPES):
        if left.dtype in family and right.dtype in family:
            return 0.5
    return 0.0


def _stats_similarity(left: ColumnProfile, right: ColumnProfile) -> float:
    """How far the two columns' contents agree, which is the strongest rename evidence there is."""
    if left.redacted or right.redacted:
        return 0.0
    if left.numeric is not None and right.numeric is not None:
        pairs = [
            (left.numeric.min, right.numeric.min),
            (left.numeric.max, right.numeric.max),
            (left.numeric.sum, right.numeric.sum),
        ]
        comparable = [(a, b) for a, b in pairs if a is not None and b is not None]
        if not comparable:
            return 0.0
        agreed = sum(1 for a, b in comparable if math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9))
        return agreed / len(comparable)
    left_values = {value for value, _ in left.top_k}
    right_values = {value for value, _ in right.top_k}
    if not left_values or not right_values:
        return 0.0
    overlap = left_values & right_values
    return len(overlap) / len(left_values | right_values)


def _rename_score(before: ColumnProfile, after: ColumnProfile) -> float:
    """Score a possible rename in [0, 1]. See the module docstring for why this is explicit."""
    name = _name_similarity(before.header or before.column, after.header or after.column)
    position = 1.0 if before.index == after.index else 0.0
    dtype = _dtype_similarity(before, after)
    stats = _stats_similarity(before, after)
    score = 0.50 * name + 0.18 * position + 0.12 * dtype + 0.20 * stats
    if name >= 0.80 and dtype > 0.0:
        # A near-identical name with a compatible dtype is a rename wherever the column sits.
        return max(score, 0.80)
    return score


def _match_renames(
    removed: list[ColumnProfile], added: list[ColumnProfile]
) -> list[tuple[ColumnProfile, ColumnProfile]]:
    """Pair removed with added columns, best score first, each column used at most once."""
    candidates = [
        (_rename_score(before, after), before, after) for before in removed for after in added
    ]
    ranked = sorted(
        (c for c in candidates if c[0] >= RENAME_THRESHOLD),
        key=lambda c: (-c[0], c[1].index, c[2].index),
    )
    used_before: set[int] = set()
    used_after: set[int] = set()
    pairs: list[tuple[ColumnProfile, ColumnProfile]] = []
    for _score, before, after in ranked:
        if before.index in used_before or after.index in used_after:
            continue
        used_before.add(before.index)
        used_after.add(after.index)
        pairs.append((before, after))
    return pairs


# =============================================================================
# COMPARISON
# =============================================================================


def _name_of(profile: ColumnProfile) -> str:
    return profile.header or profile.column


def _null_rate(profile: ColumnProfile) -> float | None:
    if profile.row_count <= 0:
        return None
    return profile.null_count / profile.row_count


def _dtype_item(before: ColumnProfile, after: ColumnProfile, name: str) -> DriftItem | None:
    if before.dtype == after.dtype:
        return None
    if before.dtype in _NUMERIC_DTYPES and after.dtype == "String":
        anomaly = (
            " The values still parse as numbers, so this is a formatting change at source "
            "rather than genuinely textual data."
            if "numbers stored as text" in after.format_anomalies
            else ""
        )
        return DriftItem(
            kind=DriftKind.NUMERIC_TO_TEXT,
            severity=Severity.ERROR,
            column=name,
            before=before.dtype,
            after=after.dtype,
            message=(f"column '{name}' was {before.dtype} and is now text.{anomaly}"),
            remediation=(
                "Cast it back before any join or aggregate. A text join key matches nothing, "
                "rows drop out silently, and the total is simply smaller than it should be "
                "(PLAN 2.6). utils.frames.coerce_numeric handles the usual formatting."
            ),
        )
    return DriftItem(
        kind=DriftKind.DTYPE_CHANGED,
        severity=Severity.WARNING,
        column=name,
        before=before.dtype,
        after=after.dtype,
        message=f"column '{name}' changed dtype from {before.dtype} to {after.dtype}.",
        remediation="Check any arithmetic or join that uses this column.",
    )


def _null_item(
    before: ColumnProfile, after: ColumnProfile, name: str, tolerance: float
) -> DriftItem | None:
    was, now = _null_rate(before), _null_rate(after)
    if was is None or now is None or abs(now - was) <= tolerance:
        return None
    direction = "rose" if now > was else "fell"
    return DriftItem(
        kind=DriftKind.NULL_RATE_SHIFT,
        severity=Severity.WARNING if now > was else Severity.INFO,
        column=name,
        before=f"{was:.1%}",
        after=f"{now:.1%}",
        message=f"null rate for column '{name}' {direction} from {was:.1%} to {now:.1%}.",
        remediation=(
            "Confirm the sending team has not started omitting a field." if now > was else None
        ),
    )


def _layout_items(baseline: HandInProfile, current: HandInProfile) -> list[DriftItem]:
    items: list[DriftItem] = []
    if baseline.sheet != current.sheet:
        items.append(
            DriftItem(
                kind=DriftKind.SHEET_CHANGED,
                severity=Severity.WARNING,
                message=f"read sheet '{current.sheet}' this time, '{baseline.sheet}' last time.",
                before=baseline.sheet,
                after=current.sheet,
            )
        )
    if baseline.preamble_rows != current.preamble_rows:
        items.append(
            DriftItem(
                kind=DriftKind.PREAMBLE_ROWS_CHANGED,
                severity=Severity.WARNING,
                message=(
                    f"the header moved: {current.preamble_rows} preamble row(s) above it this "
                    f"time, {baseline.preamble_rows} last time."
                ),
                before=str(baseline.preamble_rows),
                after=str(current.preamble_rows),
                remediation=(
                    "kedge skipped the preamble automatically. Any hand-written read that "
                    "assumes a fixed header row needs updating."
                ),
            )
        )
    if current.has_totals_row and not baseline.has_totals_row:
        items.append(
            DriftItem(
                kind=DriftKind.TOTALS_ROW_APPENDED,
                severity=Severity.WARNING,
                message="a totals row has been appended at the bottom of this hand-in.",
                before="absent",
                after="present",
                remediation=(
                    "kedge excluded it from the profile. Make sure any read of this file "
                    "drops it too, or every total will be double counted."
                ),
            )
        )
    elif baseline.has_totals_row and not current.has_totals_row:
        items.append(
            DriftItem(
                kind=DriftKind.TOTALS_ROW_REMOVED,
                severity=Severity.INFO,
                message="the totals row present in the last hand-in is gone.",
                before="present",
                after="absent",
            )
        )
    return items


def _row_count_item(
    baseline: HandInProfile, current: HandInProfile, tolerance: float
) -> DriftItem | None:
    was, now = baseline.row_count, current.row_count
    if was == 0:
        return None
    change = (now - was) / was
    if abs(change) <= tolerance:
        return None
    return DriftItem(
        kind=DriftKind.ROW_COUNT_SHIFT,
        severity=Severity.ERROR if now == 0 else Severity.WARNING,
        message=(
            f"row count moved from {was:,} to {now:,} ({change:+.1%}), beyond the "
            f"{tolerance:.0%} tolerance."
        ),
        before=f"{was:,}",
        after=f"{now:,}",
        remediation=(
            "An empty hand-in usually means the extract ran against the wrong period."
            if now == 0
            else "Confirm the extract covers the period you expect."
        ),
    )


def _order_item(
    baseline: HandInProfile, current: HandInProfile, matched: dict[str, str]
) -> DriftItem | None:
    """Report reordering once, for the columns present in both, or not at all."""
    before_order = [n for n in baseline.column_names if n in matched]
    after_order = [matched[n] for n in before_order]
    current_order = [n for n in current.column_names if n in set(after_order)]
    if after_order == current_order:
        return None
    return DriftItem(
        kind=DriftKind.COLUMN_REORDERED,
        severity=Severity.INFO,
        message=(
            f"columns arrived in a different order: {', '.join(current_order)} "
            f"(was {', '.join(before_order)})."
        ),
        before=", ".join(before_order),
        after=", ".join(current_order),
        remediation="Harmless if code selects columns by name, which generated code does.",
    )


def compare(
    baseline: HandInProfile | None,
    current: HandInProfile,
    *,
    row_count_tolerance: float = DEFAULT_ROW_COUNT_TOLERANCE,
    null_rate_tolerance: float = DEFAULT_NULL_RATE_TOLERANCE,
) -> DriftReport:
    """Diff a hand-in's shape against the last accepted one.

    Args:
        baseline: The last accepted shape, or ``None`` on the first hand-in, in which case
            the report is empty and says so rather than reporting every column as new.
        current: This hand-in's shape.
        row_count_tolerance: Relative row-count change that counts as drift.
        null_rate_tolerance: Absolute null-rate change that counts as drift.

    Returns:
        A :class:`~kedge.ingest.model.DriftReport`, ordered with the dangerous findings first.
    """
    if baseline is None:
        logger.info("no accepted hand-in to compare against; recording this one as the baseline")
        return DriftReport(current_sha256=current.sha256)

    items: list[DriftItem] = _layout_items(baseline, current)

    before_by_name = {_name_of(c): c for c in baseline.columns}
    after_by_name = {_name_of(c): c for c in current.columns}
    matched: dict[str, str] = {n: n for n in before_by_name if n in after_by_name}

    removed = [c for n, c in before_by_name.items() if n not in after_by_name]
    added = [c for n, c in after_by_name.items() if n not in before_by_name]
    for before, after in _match_renames(removed, added):
        before_name, after_name = _name_of(before), _name_of(after)
        matched[before_name] = after_name
        items.append(
            DriftItem(
                kind=DriftKind.COLUMN_RENAMED,
                severity=Severity.WARNING,
                column=after_name,
                before=before_name,
                after=after_name,
                message=f"column '{before_name}' became '{after_name}'.",
                remediation=(
                    f"Rename it back on read, or update the contract and every reference "
                    f"to '{before_name}'."
                ),
            )
        )

    renamed_before = {b for b in matched if b not in after_by_name}
    renamed_after = set(matched.values()) - set(before_by_name)
    for profile in removed:
        name = _name_of(profile)
        if name in renamed_before:
            continue
        items.append(
            DriftItem(
                kind=DriftKind.COLUMN_REMOVED,
                severity=Severity.ERROR,
                column=name,
                before=profile.dtype,
                message=f"column '{name}' ({profile.dtype}) is no longer present.",
                remediation="Any code reading it will fail. Ask the sending team, or drop it.",
            )
        )
    for profile in added:
        name = _name_of(profile)
        if name in renamed_after:
            continue
        items.append(
            DriftItem(
                kind=DriftKind.COLUMN_ADDED,
                severity=Severity.INFO,
                column=name,
                after=profile.dtype,
                message=f"new column '{name}' ({profile.dtype}) has appeared.",
                remediation="Harmless unless it should be used; add it to the contract if so.",
            )
        )

    for before_name, after_name in sorted(matched.items()):
        before, after = before_by_name[before_name], after_by_name[after_name]
        dtype_change = _dtype_item(before, after, after_name)
        if dtype_change is not None:
            items.append(dtype_change)
        null_change = _null_item(before, after, after_name, null_rate_tolerance)
        if null_change is not None:
            items.append(null_change)

    order_change = _order_item(baseline, current, matched)
    if order_change is not None:
        items.append(order_change)

    row_change = _row_count_item(baseline, current, row_count_tolerance)
    if row_change is not None:
        items.append(row_change)

    return DriftReport(
        current_sha256=current.sha256,
        baseline_sha256=baseline.sha256,
        items=sort_drift(items),
    )


def check_drift(
    handin: HandIn,
    *,
    store_dir: Path,
    sheet: str | None = None,
    header_row: int | None = None,
    sampling: SamplingConfig | None = None,
    redaction: RedactionConfig | None = None,
    row_count_tolerance: float = DEFAULT_ROW_COUNT_TOLERANCE,
    null_rate_tolerance: float = DEFAULT_NULL_RATE_TOLERANCE,
) -> tuple[HandInProfile, DriftReport]:
    """Profile a hand-in and diff it against the last accepted shape in the store.

    The whole ingest check in one call, for the notebook cell that sits between receipt and
    contract validation. It stores the new profile but does *not* accept it -- accepting is a
    decision, made by :func:`kedge.ingest.store.accept_profile` once the drift has been read.

    Args:
        handin: The received hand-in.
        store_dir: The managed store root, which is also where accepted shapes live.
        sheet: Worksheet name for spreadsheet formats.
        header_row: Override the detected header row. Pass the contract's ``header_row`` where
            there is one, so the shape being diffed is the shape about to be checked -- otherwise
            the profile detects a header the contract has already pinned somewhere else, and the
            two disagree about the row count for reasons nobody can see.
        sampling: Row and top-k caps for the profile.
        redaction: Optional column masking.
        row_count_tolerance: Relative row-count change that counts as drift.
        null_rate_tolerance: Absolute null-rate change that counts as drift.

    Returns:
        ``(profile, report)`` -- this hand-in's shape, and how it differs from the last
        accepted one.

    Raises:
        HandInReadError: If the hand-in cannot be read.
    """
    profile = profile_handin(
        handin, sheet=sheet, header_row=header_row, sampling=sampling, redaction=redaction
    )
    store.store_profile(store_dir, profile)
    report = compare(
        store.last_accepted_profile(store_dir),
        profile,
        row_count_tolerance=row_count_tolerance,
        null_rate_tolerance=null_rate_tolerance,
    )
    if report.errors:
        logger.warning(
            "hand-in %s has %d error-level drift item(s)", handin.original_name, len(report.errors)
        )
    return profile, report
