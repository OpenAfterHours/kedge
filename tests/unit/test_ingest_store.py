"""The managed store: where a hand-in lands, and what proves it landed.

Three properties carry the audit claim and each is asserted here directly. The date
partition keeps a year of monthly hand-ins readable. The hash in the filename means two
files that differ land in two places even when the sending team reuses the same name every
month, which they always do. And the index is append-only, so re-receiving bytes already
stored writes a *new receipt* pointing at the existing copy -- because the claim worth
making is "this run consumed this file", not "this file exists".

The awkward cases get as much attention as the happy path: a file that cannot be read, a
file that vanishes mid-copy, a corrupt index line, and a twelve-character hash prefix shared
by two different digests.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kedge.analysis.model import ColumnProfile
from kedge.errors import IngestError
from kedge.ingest import store
from kedge.ingest.model import HandIn, HandInProfile
from kedge.ingest.store import StoreError

STAMP = datetime(2026, 7, 25, 8, 14, 22, tzinfo=UTC)
PAYLOAD = b"id,ead\n1,100.0\n2,200.0\n"


def _receipt(
    root: Path, sha256: str, *, name: str = "exposures.csv", **overrides: object
) -> HandIn:
    fields: dict[str, object] = {
        "path": root / "2026-07-25" / store.managed_filename(sha256, name),
        "original_name": name,
        "sha256": sha256,
        "size_bytes": len(PAYLOAD),
        "received_at": STAMP,
        "source": "dropped",
    }
    fields.update(overrides)
    return HandIn(**fields)  # type: ignore[arg-type]


def _profile(sha256: str, *, name: str = "exposures.csv") -> HandInProfile:
    return HandInProfile(
        sha256=sha256,
        original_name=name,
        profiled_at=STAMP,
        sheet="",
        row_count=2,
        column_count=2,
        header_row=0,
        preamble_rows=0,
        has_totals_row=False,
        columns=(
            ColumnProfile(
                sheet="",
                column="A",
                index=0,
                header="id",
                dtype="Int64",
                row_count=2,
                null_count=0,
                distinct_count=2,
            ),
        ),
    )


# ── hashing ─────────────────────────────────────────────────────────────────


def test_hash_bytes_is_the_sha256_of_the_payload() -> None:
    assert store.hash_bytes(PAYLOAD) == hashlib.sha256(PAYLOAD).hexdigest()


def test_hashing_a_file_agrees_with_hashing_its_bytes(tmp_path: Path) -> None:
    path = tmp_path / "exposures.csv"
    path.write_bytes(PAYLOAD)

    assert store.hash_file(path) == store.hash_bytes(PAYLOAD)


def test_a_file_larger_than_one_chunk_hashes_the_same_as_its_bytes(tmp_path: Path) -> None:
    """Chunked on purpose: a selected hand-in can dwarf the drag-and-drop ceiling."""
    payload = bytes(range(256)) * 20_000  # 5.1MB, comfortably over the 1MB chunk
    path = tmp_path / "big.csv"
    path.write_bytes(payload)

    assert store.hash_file(path) == hashlib.sha256(payload).hexdigest()


def test_hashing_a_file_that_is_not_there_is_a_store_error_naming_the_path(tmp_path: Path) -> None:
    missing = tmp_path / "gone.csv"

    with pytest.raises(StoreError, match=r"could not read .*gone\.csv to hash it"):
        store.hash_file(missing)


def test_hashing_a_directory_is_a_store_error_rather_than_an_oserror(tmp_path: Path) -> None:
    with pytest.raises(StoreError):
        store.hash_file(tmp_path)


def test_a_store_error_is_an_ingest_error_so_one_except_clause_catches_the_package() -> None:
    assert issubclass(StoreError, IngestError)


# ── the managed filename ────────────────────────────────────────────────────


def test_the_managed_filename_is_the_short_hash_then_the_senders_name() -> None:
    name = store.managed_filename("4f2b91c8ad03" + "0" * 52, "exposures.xlsx")

    assert name == "4f2b91c8ad03-exposures.xlsx"


def test_two_files_with_the_same_name_and_different_bytes_get_different_filenames() -> None:
    """The sending team reuses one name every month. The hash is what keeps them apart."""
    january = store.managed_filename(store.hash_bytes(b"january"), "exposures.xlsx")
    february = store.managed_filename(store.hash_bytes(b"february"), "exposures.xlsx")

    assert january != february


@pytest.mark.parametrize(
    ("supplied", "expected"),
    [
        ("..\\..\\Windows\\system32\\evil.xlsx", "evil.xlsx"),
        ("../../etc/passwd", "passwd"),
        ("month end (final) v2.xlsx", "month-end-final-v2.xlsx"),
        ("exposures report.csv", "exposures-report.csv"),
        ("...", "handin"),
        ("", "handin"),
        ("---", "handin"),
    ],
)
def test_a_supplied_name_is_reduced_to_something_safe_to_write(
    supplied: str, expected: str
) -> None:
    """A hand-in's name arrives from another team's export and is never trusted."""
    assert store.managed_filename("0" * 64, supplied) == f"000000000000-{expected}"


def test_a_very_long_name_is_truncated_but_keeps_its_extension() -> None:
    name = store.managed_filename("0" * 64, f"{'a' * 300}.xlsx")

    assert name.endswith(".xlsx")
    assert len(name) <= len("000000000000-") + 96


def test_a_very_long_name_with_no_extension_is_simply_truncated() -> None:
    name = store.managed_filename("0" * 64, "a" * 300)

    assert len(name) == len("000000000000-") + 96


# ── writing files into the store ────────────────────────────────────────────


def test_a_dropped_payload_lands_under_the_utc_date_partition(tmp_path: Path) -> None:
    destination = store.store_bytes(
        tmp_path, data=PAYLOAD, original_name="exposures.csv", received_at=STAMP
    )

    assert destination.parent.name == "2026-07-25"
    assert destination.name == store.managed_filename(store.hash_bytes(PAYLOAD), "exposures.csv")
    assert destination.read_bytes() == PAYLOAD


def test_storing_the_same_bytes_twice_does_not_rewrite_the_file(tmp_path: Path) -> None:
    first = store.store_bytes(
        tmp_path, data=PAYLOAD, original_name="exposures.csv", received_at=STAMP
    )
    first.write_bytes(b"sentinel")  # would be clobbered if the second write went ahead

    second = store.store_bytes(
        tmp_path, data=PAYLOAD, original_name="exposures.csv", received_at=STAMP
    )

    assert second == first
    assert first.read_bytes() == b"sentinel"


def test_no_partial_file_is_left_behind_by_a_successful_write(tmp_path: Path) -> None:
    store.store_bytes(tmp_path, data=PAYLOAD, original_name="exposures.csv", received_at=STAMP)

    assert list(tmp_path.rglob("*.partial")) == []


def test_a_store_root_that_is_a_file_is_a_store_error_not_an_oserror(tmp_path: Path) -> None:
    blocked = tmp_path / "handins"
    blocked.write_bytes(b"not a directory")

    with pytest.raises(StoreError, match="could not create the hand-in store directory"):
        store.store_bytes(blocked, data=PAYLOAD, original_name="a.csv", received_at=STAMP)


def test_a_payload_that_cannot_be_written_is_a_store_error_naming_the_destination(
    tmp_path: Path,
) -> None:
    destination = (
        tmp_path / "2026-07-25" / store.managed_filename(store.hash_bytes(PAYLOAD), "exposures.csv")
    )
    destination.parent.mkdir(parents=True)
    destination.with_name(f"{destination.name}.partial").mkdir()

    with pytest.raises(StoreError, match="could not write the hand-in to"):
        store.store_bytes(tmp_path, data=PAYLOAD, original_name="exposures.csv", received_at=STAMP)


def test_a_selected_file_is_copied_into_the_store_byte_for_byte(tmp_path: Path) -> None:
    source = tmp_path / "shared" / "exposures.csv"
    source.parent.mkdir()
    source.write_bytes(PAYLOAD)
    root = tmp_path / "handins"

    destination = store.store_copy(
        root, source=source, sha256=store.hash_bytes(PAYLOAD), received_at=STAMP
    )

    assert destination.parent.name == "2026-07-25"
    assert destination.read_bytes() == PAYLOAD
    assert source.exists()


def test_copying_the_same_file_twice_does_not_recopy_it(tmp_path: Path) -> None:
    source = tmp_path / "exposures.csv"
    source.write_bytes(PAYLOAD)
    root = tmp_path / "handins"
    sha256 = store.hash_bytes(PAYLOAD)

    first = store.store_copy(root, source=source, sha256=sha256, received_at=STAMP)
    first.write_bytes(b"sentinel")
    second = store.store_copy(root, source=source, sha256=sha256, received_at=STAMP)

    assert second == first
    assert first.read_bytes() == b"sentinel"


def test_a_source_that_vanished_mid_operation_is_a_store_error(tmp_path: Path) -> None:
    """The watched folder's file can be moved away between the scan and the copy."""
    vanished = tmp_path / "gone.csv"
    root = tmp_path / "handins"

    with pytest.raises(StoreError, match="could not copy the hand-in"):
        store.store_copy(root, source=vanished, sha256="0" * 64, received_at=STAMP)


def test_a_failed_copy_leaves_no_partial_file_masquerading_as_a_hand_in(tmp_path: Path) -> None:
    root = tmp_path / "handins"

    with pytest.raises(StoreError):
        store.store_copy(root, source=tmp_path / "gone.csv", sha256="0" * 64, received_at=STAMP)

    assert [p for p in root.rglob("*") if p.is_file()] == []


# ── the receipt index ───────────────────────────────────────────────────────


def test_the_index_is_append_only_one_line_per_receipt(tmp_path: Path) -> None:
    """Re-receiving stored bytes writes a new receipt: the claim is about the run, not the file."""
    handin = _receipt(tmp_path, store.hash_bytes(PAYLOAD))

    store.record_receipt(tmp_path, handin)
    store.record_receipt(tmp_path, handin)

    lines = store.index_path(tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == json.loads(lines[1])


def test_history_reads_the_receipts_back_oldest_first(tmp_path: Path) -> None:
    first = _receipt(tmp_path, store.hash_bytes(b"january"), name="jan.csv")
    second = _receipt(tmp_path, store.hash_bytes(b"february"), name="feb.csv")
    store.record_receipt(tmp_path, first)
    store.record_receipt(tmp_path, second)

    assert [h.original_name for h in store.history(tmp_path)] == ["jan.csv", "feb.csv"]


def test_history_of_an_empty_store_is_empty_rather_than_an_error(tmp_path: Path) -> None:
    assert store.history(tmp_path) == []


def test_a_corrupt_index_line_is_skipped_and_the_readable_ones_survive(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A corrupt index must never be able to stop a hand-in being received."""
    good = _receipt(tmp_path, store.hash_bytes(PAYLOAD))
    store.record_receipt(tmp_path, good)
    with store.index_path(tmp_path).open("a", encoding="utf-8") as handle:
        handle.write("{not json at all\n")
        handle.write("\n")
        handle.write(json.dumps({"path": "x", "source": "emailed"}) + "\n")

    with caplog.at_level(logging.WARNING, logger="kedge.ingest.store"):
        recovered = store.history(tmp_path)

    assert [h.sha256 for h in recovered] == [good.sha256]
    assert sum("ignoring unusable line" in r.message for r in caplog.records) == 2


def test_an_index_that_is_not_a_file_degrades_to_no_history(tmp_path: Path) -> None:
    store.index_path(tmp_path).mkdir(parents=True)

    assert store.history(tmp_path) == []


def test_an_index_that_will_not_decode_degrades_to_no_history_rather_than_raising(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """kedge writes this file itself as ASCII, so a byte that will not decode means something
    outside kedge corrupted it -- and a corrupt index must never stop a hand-in being
    received. It used to escape as a bare UnicodeDecodeError, out through `receive`."""
    store.record_receipt(tmp_path, _receipt(tmp_path, store.hash_bytes(PAYLOAD)))
    index = store.index_path(tmp_path)
    index.write_bytes(index.read_bytes().replace(b"exposures", b"expos\xfcres"))

    with caplog.at_level(logging.WARNING, logger="kedge.ingest.store"):
        assert store.history(tmp_path) == []

    assert "could not read the hand-in index" in caplog.text


def test_an_index_that_cannot_be_appended_to_is_a_store_error(tmp_path: Path) -> None:
    store.index_path(tmp_path).mkdir(parents=True)

    with pytest.raises(StoreError, match="could not append to the hand-in index"):
        store.record_receipt(tmp_path, _receipt(tmp_path, "0" * 64))


# ── the dedupe lookup ───────────────────────────────────────────────────────


def test_the_dedupe_lookup_finds_bytes_already_stored(tmp_path: Path) -> None:
    sha256 = store.hash_bytes(PAYLOAD)
    managed = store.store_bytes(
        tmp_path, data=PAYLOAD, original_name="exposures.csv", received_at=STAMP
    )
    store.record_receipt(tmp_path, _receipt(tmp_path, sha256))

    found = store.receipt_for_hash(tmp_path, sha256)

    assert found is not None
    assert found.path == managed


def test_bytes_never_seen_here_return_no_receipt(tmp_path: Path) -> None:
    assert store.receipt_for_hash(tmp_path, "0" * 64) is None


def test_a_receipt_whose_managed_copy_has_been_pruned_is_not_reused(tmp_path: Path) -> None:
    """The index survives the store being pruned; the file it names may not."""
    sha256 = store.hash_bytes(PAYLOAD)
    store.record_receipt(tmp_path, _receipt(tmp_path, sha256))

    assert store.receipt_for_hash(tmp_path, sha256) is None


def test_the_lookup_matches_the_full_digest_not_the_twelve_characters_in_the_filename(
    tmp_path: Path,
) -> None:
    """A truncated-prefix collision must not make one hand-in be mistaken for another."""
    stored = "abcdef012345" + "1" * 52
    wanted = "abcdef012345" + "2" * 52
    directory = tmp_path / "2026-07-25"
    directory.mkdir(parents=True)
    (directory / store.managed_filename(stored, "exposures.csv")).write_bytes(PAYLOAD)
    store.record_receipt(tmp_path, _receipt(tmp_path, stored))

    assert store.receipt_for_hash(tmp_path, stored) is not None
    assert store.receipt_for_hash(tmp_path, wanted) is None


def test_the_newest_matching_receipt_wins(tmp_path: Path) -> None:
    sha256 = store.hash_bytes(PAYLOAD)
    directory = tmp_path / "2026-07-25"
    directory.mkdir(parents=True)
    older = directory / "older.csv"
    newer = directory / "newer.csv"
    older.write_bytes(PAYLOAD)
    newer.write_bytes(PAYLOAD)
    store.record_receipt(tmp_path, _receipt(tmp_path, sha256, path=older))
    store.record_receipt(tmp_path, _receipt(tmp_path, sha256, path=newer))

    found = store.receipt_for_hash(tmp_path, sha256)

    assert found is not None
    assert found.path == newer


# ── accepted shapes ─────────────────────────────────────────────────────────


def test_a_stored_profile_reads_back_unchanged(tmp_path: Path) -> None:
    profile = _profile(store.hash_bytes(PAYLOAD))

    written = store.store_profile(tmp_path, profile)

    assert written.parent.name == store.PROFILES_DIRNAME
    assert written.name == f"{profile.sha256[:12]}.json"
    assert store.load_profile(tmp_path, profile.sha256) == profile


def test_a_profile_that_was_never_stored_loads_as_none(tmp_path: Path) -> None:
    assert store.load_profile(tmp_path, "0" * 64) is None


def test_storing_a_profile_is_not_accepting_it(tmp_path: Path) -> None:
    """Accepting is a decision, made after the drift has been read."""
    store.store_profile(tmp_path, _profile(store.hash_bytes(PAYLOAD)))

    assert store.last_accepted_profile(tmp_path) is None


def test_accepting_a_profile_moves_the_pointer_drift_compares_against(tmp_path: Path) -> None:
    profile = _profile(store.hash_bytes(PAYLOAD))

    pointer = store.accept_profile(tmp_path, profile)

    assert pointer.name == store.LAST_ACCEPTED_FILENAME
    assert store.last_accepted_profile(tmp_path) == profile
    assert store.load_profile(tmp_path, profile.sha256) == profile


def test_accepting_a_second_profile_replaces_the_baseline(tmp_path: Path) -> None:
    store.accept_profile(tmp_path, _profile(store.hash_bytes(b"january"), name="jan.csv"))
    store.accept_profile(tmp_path, _profile(store.hash_bytes(b"february"), name="feb.csv"))

    accepted = store.last_accepted_profile(tmp_path)

    assert accepted is not None
    assert accepted.original_name == "feb.csv"


def test_an_unreadable_profile_degrades_to_none_rather_than_raising(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    profiles = tmp_path / store.PROFILES_DIRNAME
    profiles.mkdir()
    (profiles / store.LAST_ACCEPTED_FILENAME).write_text("{ not json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="kedge.ingest.store"):
        assert store.last_accepted_profile(tmp_path) is None

    assert "ignoring unreadable hand-in profile" in caplog.text


def test_a_malformed_profile_degrades_to_none_rather_than_raising(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    profiles = tmp_path / store.PROFILES_DIRNAME
    profiles.mkdir()
    (profiles / store.LAST_ACCEPTED_FILENAME).write_text('{"sha256": "abc"}', encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="kedge.ingest.store"):
        assert store.last_accepted_profile(tmp_path) is None

    assert "ignoring malformed hand-in profile" in caplog.text


def test_a_baseline_pointer_that_cannot_be_written_is_a_store_error(tmp_path: Path) -> None:
    """If the pointer does not persist, drift has no baseline across runs."""
    profiles = tmp_path / store.PROFILES_DIRNAME
    profiles.mkdir()
    (profiles / store.LAST_ACCEPTED_FILENAME).mkdir()

    with pytest.raises(StoreError, match="could not record the accepted hand-in shape"):
        store.accept_profile(tmp_path, _profile(store.hash_bytes(PAYLOAD)))


def test_a_profile_that_cannot_be_written_is_a_store_error(tmp_path: Path) -> None:
    profiles = tmp_path / store.PROFILES_DIRNAME
    profiles.mkdir()
    profile = _profile(store.hash_bytes(PAYLOAD))
    (profiles / f"{profile.sha256[:12]}.json").mkdir()

    with pytest.raises(StoreError, match="could not write the hand-in profile"):
        store.store_profile(tmp_path, profile)
