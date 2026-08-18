"""A rectangle of text off the clipboard, and the managed CSV it becomes.

The paste path differs from every other hand-in path in one respect that the tests here exist
to pin: **the bytes stored are not the bytes that arrived**. Everywhere else in
:mod:`kedge.ingest` the hash is a claim about a file the user still holds, and rewriting the
payload would falsify it. A paste has no original, so the managed CSV is the artifact of
record and normalising on the way in is what makes it readable at all --
:func:`kedge.ingest.read_frame` picks its separator off the file's extension, so the
delimiter has to be resolved before the file is named, not after.

Two properties carry most of the weight. The delimiter is detected from the data rather than
assumed, including when the *wrong* delimiter also appears on every line; and a ragged row
raises with the line number rather than being padded, because a paste kedge quietly repaired
is a hand-in nobody checked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from kedge.ingest import read_frame
from kedge.ingest.model import Paste
from kedge.ingest.paste import PasteError, normalise_paste, sniff_delimiter
from kedge.ingest.receive import EmptySelectionError, receive

STAMP = datetime(2026, 7, 25, 8, 14, 22, tzinfo=UTC)


# =============================================================================
# SNIFFING
# =============================================================================


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("id\tamount\nA\t1\nB\t2\n", "\t"),
        ("id,amount\nA,1\nB,2\n", ","),
        ("id;amount\nA;1,50\nB;2,25\n", ";"),
        ("id|amount\nA|1\nB|2\n", "|"),
    ],
    ids=["tab", "comma", "semicolon", "pipe"],
)
def test_each_candidate_delimiter_is_detected(text: str, expected: str) -> None:
    assert sniff_delimiter(text) == expected


def test_a_single_column_paste_has_no_delimiter() -> None:
    """Not an error: one column of trade ids is a legitimate thing to paste."""
    assert sniff_delimiter("trade_id\nTRD-00001\nTRD-00002\n") is None


def test_the_delimiter_is_chosen_on_consistency_not_frequency() -> None:
    """A comma inside every description must not outvote the tab that divides the columns.

    This is the case that makes a naive "count the commas" sniffer wrong on the single most
    common real paste there is: a grid copied out of SSMS whose text column contains prose.
    """
    text = (
        "id\tdescription\tamount\n"
        "A\tone, two, three\t1\n"
        "B\tfour, five, six\t2\n"
        "C\tseven, eight, nine\t3\n"
    )
    assert sniff_delimiter(text) == "\t"


def test_a_delimiter_inside_quotes_is_not_counted() -> None:
    """Quoted commas belong to the value. Counting them makes a 2-column file look like 4."""
    text = 'id,note\nA,"one, two"\nB,"three, four"\n'
    assert sniff_delimiter(text) == ","


# =============================================================================
# NORMALISING
# =============================================================================


def test_a_tab_paste_becomes_comma_delimited_csv() -> None:
    result = normalise_paste("id\tamount\nA\t1.50\nB\t2.25\n")

    assert result.csv_bytes == b"id,amount\nA,1.50\nB,2.25\n"
    assert (result.delimiter, result.rows, result.columns) == ("\t", 2, 2)


def test_a_value_containing_a_comma_is_quoted_on_the_way_out() -> None:
    """The round trip has to survive a tab-delimited value that holds the output delimiter."""
    result = normalise_paste("id\tnote\nA\tone, two\n")

    assert result.csv_bytes == b'id,note\nA,"one, two"\n'
    assert pl.read_csv(result.csv_bytes)["note"].to_list() == ["one, two"]


def test_the_rows_affected_trailer_is_dropped_and_reported() -> None:
    """SSMS writes it under every grid, and it is the one trailer safe to recognise."""
    result = normalise_paste("id\tamount\nA\t1\nB\t2\n\n(2 rows affected)\n")

    assert result.rows == 2
    assert result.dropped == ("(2 rows affected)",)
    assert "dropped 1 trailing line" in result.summary_line()


def test_blank_lines_are_ignored() -> None:
    assert normalise_paste("id,amount\n\nA,1\n\n\nB,2\n\n").rows == 2


def test_a_ragged_row_raises_naming_the_line_and_both_widths() -> None:
    """Padding it would produce a hand-in that passed its contract by having been repaired."""
    text = "id,amount,ccy\nA,1,GBP\nB,2\nC,3,USD\n"

    with pytest.raises(PasteError) as excinfo:
        normalise_paste(text)

    message = str(excinfo.value)
    assert "line 3" in message
    assert "2 fields" in message
    assert "header has 3" in message


def test_an_empty_paste_says_what_to_copy_rather_than_raising_a_parse_error() -> None:
    with pytest.raises(PasteError, match="including its header row"):
        normalise_paste("   \n\n  \n")


def test_a_utf8_bom_does_not_become_part_of_the_first_header() -> None:
    """Excel and some clients put one on the clipboard. It must not name a column '﻿id'."""
    result = normalise_paste("﻿id,amount\nA,1\n")

    assert result.csv_bytes.startswith(b"id,amount")


def test_crlf_and_lone_cr_line_endings_both_read_as_rows() -> None:
    assert normalise_paste("id,amount\r\nA,1\r\nB,2\r\n").rows == 2


# =============================================================================
# THROUGH RECEIVE, INTO THE STORE
# =============================================================================


def test_receiving_a_paste_stores_normalised_csv_under_a_csv_name(tmp_path: Path) -> None:
    """The whole point: what lands in the store is readable by the same reader as a file.

    A paste named anything else would be read with the wrong separator -- green through
    ingestion, wrong at the first calculation.
    """
    handin = receive(
        Paste(text="id\tamount\nA\t1.50\nB\t2.25\n", name="extract.tsv"),
        store_dir=tmp_path,
        received_at=STAMP,
    )

    assert handin.source == "pasted"
    assert handin.path.suffix == ".csv"
    assert handin.original_name == "extract.csv"
    frame = read_frame(handin.path)
    assert frame.columns == ["id", "amount"]
    assert frame["amount"].to_list() == [1.50, 2.25]


def test_a_pasted_handin_is_hashed_on_what_was_stored(tmp_path: Path) -> None:
    """The hash has to describe the managed copy, since that is the only copy there is."""
    from kedge.ingest.store import hash_bytes

    handin = receive(Paste(text="id\tamount\nA\t1\n"), store_dir=tmp_path, received_at=STAMP)

    assert handin.sha256 == hash_bytes(handin.path.read_bytes())
    assert handin.size_bytes == handin.path.stat().st_size


def test_an_untouched_paste_box_is_an_empty_selection_not_an_error(tmp_path: Path) -> None:
    """A notebook's first run evaluates the cell before anyone has pasted anything."""
    with pytest.raises(EmptySelectionError):
        receive(Paste(text=""), store_dir=tmp_path, received_at=STAMP)


def test_two_identical_pastes_dedupe_to_one_stored_file(tmp_path: Path) -> None:
    first = receive(Paste(text="id,amount\nA,1\n"), store_dir=tmp_path, received_at=STAMP)
    second = receive(Paste(text="id,amount\nA,1\n"), store_dir=tmp_path, received_at=STAMP)

    assert first.path == second.path


def test_the_same_data_pasted_in_two_dialects_dedupes_to_one_file(tmp_path: Path) -> None:
    """Normalisation happens before hashing, so tab and comma pastes of one grid are one file.

    Worth pinning: it is the visible consequence of storing the normalised form, and it is the
    behaviour somebody would break by moving the normalisation downstream of the store.
    """
    tabbed = receive(Paste(text="id\tamount\nA\t1\n"), store_dir=tmp_path, received_at=STAMP)
    comma = receive(Paste(text="id,amount\nA,1\n"), store_dir=tmp_path, received_at=STAMP)

    assert tabbed.sha256 == comma.sha256
    assert tabbed.path == comma.path


def test_a_ragged_paste_reaches_the_notebook_as_an_ingest_error(tmp_path: Path) -> None:
    """PasteError is an IngestError, so the scaffolded cells already catch it."""
    from kedge.errors import IngestError

    with pytest.raises(IngestError):
        receive(Paste(text="a,b\n1\n"), store_dir=tmp_path, received_at=STAMP)
