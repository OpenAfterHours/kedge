"""Delimited text pasted into the notebook, normalised into a managed hand-in.

The runbook a converted workbook becomes hands the user a query, the user runs it somewhere
kedge cannot reach, and the result comes back. Sometimes that result is a file. Very often it
is a rectangle of text on the clipboard, because the user selected the grid in SSMS, DBeaver
or a browser and pressed Ctrl+C -- and telling them to save that to disk first is friction
kedge has no right to impose.

**A paste is normalised to RFC 4180 CSV and stored, and the stored file is the artifact of
record.** That is a deliberate departure from :class:`~kedge.ingest.model.Upload`, whose bytes
are written through untouched. An upload has an original somewhere -- the hash is a claim
about a file the user still holds, so rewriting it would make the claim false. A paste has no
original: the clipboard is gone the moment it is overwritten, and text that arrived
tab-delimited with a trailing ``(120 rows affected)`` is not a thing anyone can produce again.
The managed CSV *is* the evidence, so it is written in the one dialect everything downstream
reads, and what was detected on the way in is recorded rather than inferred later.

Normalising also buys correctness that a verbatim write cannot. :func:`kedge.ingest.read_frame`
picks its separator from the file's **extension**, so storing a tab-delimited paste under
``.csv`` would read as one column and storing it under ``.tsv`` would strand every paste that
was comma-delimited to begin with. Round-tripping through :mod:`csv` settles it once: quoting,
embedded delimiters and embedded newlines all come out in the dialect the reader expects.

Three behaviours worth knowing before changing anything here:

- **The delimiter is detected, never assumed.** Tab, comma, semicolon and pipe are the four
  candidates, scored on how *consistently* they divide the sample rather than on how often they
  appear. A comma-delimited extract whose every row carries one pipe inside a description field
  scores both, and consistency alone cannot separate them; the tie breaks toward the delimiter
  producing more columns, then toward :data:`CANDIDATES` order.
- **A trailing ``(120 rows affected)`` is dropped, and nothing else is.** It is the one ragged
  final line that is an artifact of the tool rather than the data, it is unambiguous, and it
  arrives on a large share of real pastes. Any *other* ragged row raises
  :class:`PasteError` naming the line and its width -- because a paste kedge quietly repaired
  is a hand-in nobody checked.
- **Raggedness is detected here rather than at the reader.** ``pl.read_csv`` on a ragged file
  reports a parse failure against a byte offset, which is true and useless. Line 41 has 7
  fields where the header has 8 is what somebody can act on.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kedge.errors import IngestError

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "CANDIDATES",
    "PasteError",
    "PasteResult",
    "normalise_paste",
    "sniff_delimiter",
]

CANDIDATES: tuple[str, ...] = ("\t", ",", ";", "|")
"""The delimiters worth guessing between, in tie-break order.

Tab leads because a grid copied out of a database client is tab-delimited, and that is the
paste this module exists for. Comma follows because it is what every export produces.
Semicolon is the same thing under a European locale, where the comma is the decimal separator.
Pipe is the one people reach for when the data itself contains all three.
"""

SAMPLE_LINES = 40
"""How many non-empty lines the sniffer scores. Enough to be confident, cheap on a 200MB paste."""

_ROWS_AFFECTED = re.compile(r"^\(\s*\d+\s+rows?\s+affected\s*\)$", re.IGNORECASE)
"""SSMS and sqlcmd write this under a result grid. It is the one trailer safe to recognise."""

_DELIMITER_NAMES = {"\t": "tab", ",": "comma", ";": "semicolon", "|": "pipe"}


class PasteError(IngestError):
    """Pasted text could not be read as a table.

    Carries what was detected and which line disagreed, because the fix is always in the paste
    and the user is looking at the source grid while they read this.
    """


@dataclass(frozen=True, slots=True)
class PasteResult:
    """Normalised CSV, plus what had to be worked out to produce it.

    ``csv_bytes`` is what gets stored. The rest is what the notebook shows the user so the
    normalisation is visible rather than magic -- a paste that silently lost its last row is
    exactly the failure this whole module is trying not to be.
    """

    csv_bytes: bytes
    delimiter: str
    rows: int
    """Data rows, excluding the header."""
    columns: int
    dropped: tuple[str, ...] = ()
    """Lines removed as tool noise, verbatim, so the user can see what went."""

    @property
    def delimiter_name(self) -> str:
        """``tab``, ``comma``, ``semicolon``, ``pipe`` -- or a quoted fallback."""
        return _DELIMITER_NAMES.get(self.delimiter, repr(self.delimiter))

    def summary_line(self) -> str:
        """One line for the notebook panel, naming everything that was inferred."""
        base = (
            f"{self.rows:,} rows, {self.columns} columns, read as {self.delimiter_name}-delimited"
        )
        if not self.dropped:
            return f"{base}."
        return f"{base}; dropped {len(self.dropped)} trailing line(s): " + "; ".join(
            f"`{line}`" for line in self.dropped
        )


def sniff_delimiter(text: str, *, candidates: Sequence[str] = CANDIDATES) -> str | None:
    """Work out which character divides the columns, or ``None`` for a single column.

    Scored on consistency, not frequency. A delimiter that splits every sampled line into the
    same number of fields is the delimiter; one that appears more often but produces a
    different width on every line is punctuation inside the data.

    Args:
        text: The pasted text.
        candidates: Characters to consider, in tie-break order.

    Returns:
        The detected delimiter, or ``None`` when no candidate divides the sample at all --
        a genuinely single-column paste, which is a legitimate hand-in.

    Example:
        >>> sniff_delimiter("a\\tb\\n1\\t2\\n")
        '\\t'
        >>> sniff_delimiter("just\\none\\ncolumn\\n") is None
        True
    """
    lines = [line for line in _physical_lines(text) if line.strip()][:SAMPLE_LINES]
    if not lines:
        return None

    best: tuple[float, int, int] | None = None
    winner: str | None = None
    for rank, delimiter in enumerate(candidates):
        counts = [_count_outside_quotes(line, delimiter) for line in lines]
        if not any(counts):
            continue
        # Consistency, measured as the share of lines agreeing with the most common width.
        # The header is one of them, so a real delimiter scores at or near 1.0.
        modal = max(set(counts), key=counts.count)
        if modal == 0:
            continue
        consistency = counts.count(modal) / len(counts)
        score = (consistency, modal, -rank)
        if best is None or score > best:
            best, winner = score, delimiter
    return winner


def normalise_paste(text: str) -> PasteResult:
    """Turn pasted text into canonical CSV bytes, or say precisely why it is not a table.

    Args:
        text: The clipboard contents, as the notebook's text area yields them.

    Returns:
        The :class:`PasteResult` to store and to show the user.

    Raises:
        PasteError: The paste is empty, or a row's width disagrees with the header's.

    Example:
        >>> normalise("id\\tamount\\nA\\t1\\nB\\t2\\n").columns
        2
    """
    stripped = text.lstrip("﻿")
    if not stripped.strip():
        msg = (
            "nothing was pasted. Copy the result grid -- including its header row -- and "
            "paste it into the box above."
        )
        raise PasteError(msg)

    delimiter = sniff_delimiter(stripped) or ","
    rows, dropped = _parse(stripped, delimiter)
    if not rows:
        msg = (
            "the paste held no rows once blank lines were removed. Copy the result grid "
            "including its header row."
        )
        raise PasteError(msg)

    width = len(rows[0])
    _reject_ragged(rows, width=width, delimiter=delimiter)

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerows(rows)
    result = PasteResult(
        csv_bytes=buffer.getvalue().encode("utf-8"),
        delimiter=delimiter,
        rows=len(rows) - 1,
        columns=width,
        dropped=dropped,
    )
    logger.info("normalised a paste: %s", result.summary_line())
    return result


# =============================================================================
# INTERNALS
# =============================================================================


def _physical_lines(text: str) -> list[str]:
    """Split on newlines without interpreting quotes. Sniffing only, never parsing."""
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _count_outside_quotes(line: str, delimiter: str) -> int:
    """Count ``delimiter`` in ``line``, ignoring anything inside double quotes.

    A quoted description holding four commas would otherwise make the comma look like the most
    consistent delimiter in a tab-delimited paste.
    """
    count = 0
    quoted = False
    for character in line:
        if character == '"':
            quoted = not quoted
        elif character == delimiter and not quoted:
            count += 1
    return count


def _parse(text: str, delimiter: str) -> tuple[list[list[str]], tuple[str, ...]]:
    """Read the text as delimited data, dropping blank lines and recognised tool noise.

    Returns the rows and, separately, the lines that were thrown away -- which the caller
    reports, because a row count that does not match what the user selected in the grid is
    otherwise a mystery they have to solve themselves.
    """
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    rows: list[list[str]] = []
    dropped: list[str] = []
    for row in reader:
        if not row or all(not field.strip() for field in row):
            continue
        joined = delimiter.join(row).strip()
        if len(row) == 1 and _ROWS_AFFECTED.match(joined):
            dropped.append(joined)
            continue
        rows.append(row)

    # The trailer is only noise where it trails. One in the middle means the paste holds two
    # result sets, which is a different problem and not one to silently discard half of.
    return rows, tuple(dropped)


def _reject_ragged(rows: list[list[str]], *, width: int, delimiter: str) -> None:
    """Raise on the first row whose width disagrees with the header's.

    Reported against the paste's own line numbering, one-based and counting the header, because
    that is what the user is looking at.
    """
    for index, row in enumerate(rows[1:], start=2):
        if len(row) != width:
            name = _DELIMITER_NAMES.get(delimiter, repr(delimiter))
            preview = delimiter.join(row)
            if len(preview) > 120:
                preview = preview[:117] + "..."
            msg = (
                f"line {index} of the paste has {len(row)} fields where the header has "
                f"{width}, reading it as {name}-delimited. kedge will not guess which "
                f"columns are missing.\n"
                f"  {preview}\n"
                f"Check for a wrapped line, an unquoted {name} inside a value, or a footer "
                f"under the grid."
            )
            raise PasteError(msg)
