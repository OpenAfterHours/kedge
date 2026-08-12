"""Two marimo entry points, a watched folder, and one record out of all three.

`mo.ui.file` gives bytes with no path; `mo.ui.file_browser` gives a path with no bytes; the
watched folder gives a bare path. An uploaded hand-in is **not reproducible** -- reopen the
notebook tomorrow and the bytes are gone -- which is the entire reason the managed store
exists (PLAN 2.8). So the property under test throughout is the same one: whatever came in,
`HandIn.path` points at the managed copy and `HandIn.sha256` is the digest of the bytes that
were actually stored there.

Nothing here imports marimo. The two payload shapes are matched structurally, so the stand-in
classes below carry only the attributes `receive` looks at -- which is exactly the contract
`receive` claims to have with marimo, stated in kedge rather than borrowed from someone
else's private module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kedge.errors import IngestError
from kedge.ingest import store
from kedge.ingest.model import Upload
from kedge.ingest.receive import (
    UPLOAD_SIZE_LIMIT_BYTES,
    EmptySelectionError,
    UnsupportedPayloadError,
    UploadTooLargeError,
    exceeds_upload_limit,
    receive,
    receive_many,
    receive_paths,
    size_guidance,
)

STAMP = datetime(2026, 7, 25, 8, 14, 22, tzinfo=UTC)
PAYLOAD = b"id,ead\n1,100.0\n2,200.0\n"


@dataclass(frozen=True)
class FileUploadResults:
    """What ``mo.ui.file(kind="area").value`` yields, verified on marimo 0.23.15."""

    name: str
    contents: bytes


@dataclass(frozen=True)
class FileBrowserFileInfo:
    """What ``mo.ui.file_browser(...).value`` yields, verified on marimo 0.23.15."""

    id: str
    path: Path
    name: str
    is_directory: bool = False


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """The managed store root, as `Workspace.handins_dir` would supply it."""
    return tmp_path / "handins"


@pytest.fixture
def shared(tmp_path: Path) -> Path:
    """A file sitting where the sending team left it, outside the store."""
    directory = tmp_path / "shared-drive"
    directory.mkdir()
    path = directory / "exposures.csv"
    path.write_bytes(PAYLOAD)
    return path


# ── the dropped payload: bytes with no path ─────────────────────────────────


def test_a_dropped_payload_lands_in_the_store_and_the_record_points_at_the_copy(
    root: Path,
) -> None:
    handin = receive(
        Upload(name="exposures.csv", contents=PAYLOAD), store_dir=root, received_at=STAMP
    )

    assert handin.source == "dropped"
    assert handin.path.is_relative_to(root)
    assert handin.path.parent.name == "2026-07-25"
    assert handin.path.read_bytes() == PAYLOAD


def test_the_recorded_digest_is_of_the_bytes_that_were_actually_stored(root: Path) -> None:
    """The audit line claims this run consumed these bytes. It has to be the stored ones."""
    handin = receive(Upload(name="exposures.csv", contents=PAYLOAD), store_dir=root)

    assert handin.sha256 == store.hash_bytes(handin.path.read_bytes())
    assert handin.size_bytes == len(PAYLOAD)
    assert handin.path.name.startswith(handin.sha256[:12])


def test_a_marimo_shaped_upload_is_matched_structurally_without_importing_marimo(
    root: Path,
) -> None:
    handin = receive(FileUploadResults(name="exposures.csv", contents=PAYLOAD), store_dir=root)

    assert handin.source == "dropped"
    assert handin.path.read_bytes() == PAYLOAD


def test_marimos_one_element_tuple_is_the_normal_case_not_the_exception(root: Path) -> None:
    """Both widgets return a tuple from `.value` even with multiple=False."""
    handin = receive((Upload(name="exposures.csv", contents=PAYLOAD),), store_dir=root)

    assert handin.original_name == "exposures.csv"


# ── the selected payload: a path with no bytes ──────────────────────────────


def test_a_selected_path_is_copied_into_the_store_by_default(root: Path, shared: Path) -> None:
    """A shared-drive path is not a stable artifact: next month it holds different numbers."""
    handin = receive(
        FileBrowserFileInfo(id="1", path=shared, name=shared.name),
        store_dir=root,
        received_at=STAMP,
    )

    assert handin.source == "selected"
    assert handin.path != shared
    assert handin.path.is_relative_to(root)
    assert handin.path.read_bytes() == PAYLOAD
    assert shared.exists()


def test_the_digest_of_a_selected_hand_in_is_of_the_file_on_disk(root: Path, shared: Path) -> None:
    handin = receive(shared, store_dir=root)

    assert handin.sha256 == store.hash_bytes(PAYLOAD)
    assert handin.size_bytes == len(PAYLOAD)


def test_copy_on_select_off_references_the_file_in_place_and_says_so(
    root: Path, shared: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="kedge.ingest.receive"):
        handin = receive(shared, store_dir=root, copy_on_select=False)

    assert handin.path == shared
    assert "may not be reproducible" in caplog.text


def test_a_plain_string_path_is_accepted_and_expanded(root: Path, shared: Path) -> None:
    handin = receive(str(shared), store_dir=root)

    assert handin.original_name == "exposures.csv"
    assert handin.path.is_relative_to(root)


def test_a_directory_the_browser_offered_is_ignored_rather_than_received(
    root: Path, tmp_path: Path
) -> None:
    payload = FileBrowserFileInfo(id="1", path=tmp_path, name=tmp_path.name, is_directory=True)

    assert receive_many(payload, store_dir=root) == []


def test_a_nominated_path_that_does_not_exist_is_an_ingest_error(
    root: Path, tmp_path: Path
) -> None:
    with pytest.raises(IngestError, match="does not exist"):
        receive(tmp_path / "never-arrived.xlsx", store_dir=root)


def test_a_nominated_path_that_is_a_directory_is_an_ingest_error(
    root: Path, tmp_path: Path
) -> None:
    with pytest.raises(IngestError, match="is a directory, not a file"):
        receive(tmp_path, store_dir=root)


# ── the watched folder ──────────────────────────────────────────────────────


def test_a_watched_file_is_recorded_as_watched(root: Path, shared: Path) -> None:
    received = receive_paths([shared], store_dir=root)

    assert [h.source for h in received] == ["watched"]
    assert received[0].path.is_relative_to(root)


def test_an_explicit_source_overrides_what_the_payload_shape_would_have_said(
    root: Path,
) -> None:
    handin = receive(
        Upload(name="exposures.csv", contents=PAYLOAD), store_dir=root, source="watched"
    )

    assert handin.source == "watched"


@pytest.mark.parametrize("expected", ["dropped", "selected", "watched"])
def test_all_three_sources_produce_the_same_record_shape(
    root: Path, shared: Path, expected: str
) -> None:
    """Only the label differs; downstream cells are indifferent to how the file arrived."""
    payloads = {
        "dropped": Upload(name="exposures.csv", contents=PAYLOAD),
        "selected": FileBrowserFileInfo(id="1", path=shared, name=shared.name),
        "watched": shared,
    }
    source = None if expected != "watched" else "watched"

    handin = receive(payloads[expected], store_dir=root, source=source)

    assert handin.source == expected
    assert handin.sha256 == store.hash_bytes(PAYLOAD)
    assert handin.path.is_relative_to(root)


# ── the receipt ─────────────────────────────────────────────────────────────


def test_every_receipt_is_recorded_in_the_index(root: Path) -> None:
    receive(Upload(name="exposures.csv", contents=PAYLOAD), store_dir=root)
    receive(Upload(name="exposures.csv", contents=PAYLOAD), store_dir=root)

    assert len(store.history(root)) == 2


def test_the_contract_name_travels_onto_the_record(root: Path) -> None:
    handin = receive(
        Upload(name="exposures.csv", contents=PAYLOAD), store_dir=root, contract="exposures"
    )

    assert handin.contract == "exposures"
    assert store.history(root)[0].contract == "exposures"


def test_receiving_the_same_bytes_twice_reuses_the_managed_copy(root: Path) -> None:
    first = receive(Upload(name="exposures.csv", contents=PAYLOAD), store_dir=root)
    second = receive(Upload(name="renamed.csv", contents=PAYLOAD), store_dir=root)

    assert second.path == first.path
    assert len(store.history(root)) == 2


def test_selecting_the_same_file_twice_reuses_the_managed_copy_rather_than_recopying(
    root: Path, shared: Path
) -> None:
    first = receive(shared, store_dir=root)
    first.path.write_bytes(b"sentinel")  # would be clobbered by a second copy

    second = receive(shared, store_dir=root)

    assert second.path == first.path
    assert first.path.read_bytes() == b"sentinel"


def test_dedupe_off_still_resolves_to_one_managed_copy_because_the_name_carries_the_hash(
    root: Path,
) -> None:
    first = receive(Upload(name="exposures.csv", contents=PAYLOAD), store_dir=root, dedupe=False)
    second = receive(Upload(name="exposures.csv", contents=PAYLOAD), store_dir=root, dedupe=False)

    assert second.path == first.path


def test_dedupe_never_hands_back_a_path_outside_the_store(root: Path, shared: Path) -> None:
    """A receipt written with copy_on_select off names an unmanaged path. Reusing it would
    break the one invariant HandIn exists to hold."""
    referenced = receive(shared, store_dir=root, copy_on_select=False)
    assert referenced.path == shared

    copied = receive(shared, store_dir=root, copy_on_select=True)

    assert copied.path.is_relative_to(root)
    assert copied.path.read_bytes() == PAYLOAD


# ── more than one file ──────────────────────────────────────────────────────


def test_receive_many_returns_one_record_per_file_in_payload_order(root: Path) -> None:
    received = receive_many(
        [
            Upload(name="january.csv", contents=b"jan"),
            Upload(name="february.csv", contents=b"feb"),
        ],
        store_dir=root,
    )

    assert [h.original_name for h in received] == ["january.csv", "february.csv"]


def test_a_directory_among_a_multi_file_selection_is_dropped_and_the_rest_received(
    root: Path, shared: Path, tmp_path: Path
) -> None:
    payload = [
        FileBrowserFileInfo(id="1", path=tmp_path, name=tmp_path.name, is_directory=True),
        FileBrowserFileInfo(id="2", path=shared, name=shared.name),
    ]

    received = receive_many(payload, store_dir=root)

    assert [h.original_name for h in received] == ["exposures.csv"]


def test_receive_refuses_to_pick_one_of_several_nominations(root: Path) -> None:
    payload = [
        Upload(name="january.csv", contents=b"jan"),
        Upload(name="february.csv", contents=b"feb"),
    ]

    with pytest.raises(IngestError, match="Use receive_many"):
        receive(payload, store_dir=root)


def test_every_file_in_one_call_shares_a_receipt_timestamp(root: Path) -> None:
    """One selection is one receipt event, so its files land in one date partition."""
    received = receive_many(
        [Upload(name="a.csv", contents=b"a"), Upload(name="b.csv", contents=b"b")], store_dir=root
    )

    assert received[0].received_at == received[1].received_at


# ── nothing selected yet ────────────────────────────────────────────────────


@pytest.mark.parametrize("empty", [None, (), [], set()])
def test_an_untouched_selector_asks_the_notebook_to_wait_rather_than_erroring(
    root: Path, empty: object
) -> None:
    """Normal on the first run: the cell evaluates before the user has picked anything."""
    with pytest.raises(EmptySelectionError, match=r"mo\.stop"):
        receive(empty, store_dir=root)


@pytest.mark.parametrize("empty", [None, (), []])
def test_receive_many_treats_an_untouched_selector_as_an_empty_list(
    root: Path, empty: object
) -> None:
    assert receive_many(empty, store_dir=root) == []


def test_nothing_is_written_to_the_store_when_nothing_was_selected(root: Path) -> None:
    receive_many(None, store_dir=root)

    assert not root.exists()


# ── payloads that are not hand-ins ──────────────────────────────────────────


@pytest.mark.parametrize("payload", [42, 3.5, object(), {"path": "a.xlsx"}, b"raw bytes"])
def test_an_unrecognised_payload_names_every_shape_that_would_have_worked(
    root: Path, payload: object
) -> None:
    with pytest.raises(UnsupportedPayloadError) as excinfo:
        receive(payload, store_dir=root)

    message = str(excinfo.value)
    assert "mo.ui.file" in message
    assert "mo.ui.file_browser" in message
    assert "kedge.ingest.Upload" in message


def test_an_unsupported_payload_error_is_an_ingest_error() -> None:
    assert issubclass(UnsupportedPayloadError, IngestError)
    assert issubclass(EmptySelectionError, IngestError)
    assert issubclass(UploadTooLargeError, IngestError)


# ── the drag-and-drop ceiling ───────────────────────────────────────────────


def test_the_ceiling_is_marimos_own_hundred_decimal_megabytes() -> None:
    assert UPLOAD_SIZE_LIMIT_BYTES == 100_000_000
    assert not exceeds_upload_limit(UPLOAD_SIZE_LIMIT_BYTES)
    assert exceeds_upload_limit(UPLOAD_SIZE_LIMIT_BYTES + 1)


def test_a_file_that_would_upload_fine_gets_no_guidance() -> None:
    assert size_guidance(1_000) is None


def test_an_over_large_file_is_routed_to_the_two_entry_points_with_no_ceiling() -> None:
    """Better to say so before the user drags 400MB into a zone that will refuse it."""
    guidance = size_guidance(400_000_000)

    assert guidance is not None
    assert "400.0MB" in guidance
    assert "mo.ui.file_browser" in guidance
    assert "watched folder" in guidance


def test_a_dropped_payload_over_the_ceiling_is_refused_with_that_guidance(root: Path) -> None:
    with pytest.raises(UploadTooLargeError, match=r"mo\.ui\.file_browser"):
        receive(Upload(name="huge.xlsx", contents=b"0123456789"), store_dir=root, upload_limit=4)


def test_the_ceiling_does_not_apply_to_a_selected_path(root: Path, shared: Path) -> None:
    """The file browser passes a path rather than bytes, and has no size limit."""
    handin = receive(shared, store_dir=root, upload_limit=1)

    assert handin.path.is_relative_to(root)
