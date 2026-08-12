"""The watched folder: what it picks up, what it refuses to touch, and how it stops.

Two invariants carry the weight here. A file is visible long before it is complete, so nothing
is hashed until it has stopped moving; and a sweep is idempotent, so a scheduled run over the
same folder every hour does not receive the same hand-in twice (PLAN 2.8).
"""

from __future__ import annotations

import importlib
import os
import threading
import time
from pathlib import Path

import pytest

from kedge.ingest import store
from kedge.ingest.watch import (
    WatchedFolder,
    WatchError,
    candidates,
    scan_once,
    wait_until_stable,
    watch,
)

WATCH_MODULE = importlib.import_module("kedge.ingest.watch")
"""The module, fetched the long way round on purpose.

``kedge.ingest`` re-exports the ``watch`` *function*, which shadows the submodule of the same
name: ``import kedge.ingest.watch as m`` binds the function, and so does monkeypatch's dotted
string form. Anything patching a name inside the module has to hold the module itself.
"""

# ── fixtures ─────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    """The folder another team drops files into."""
    directory = tmp_path / "inbox"
    directory.mkdir()
    return directory


@pytest.fixture
def store_dir(tmp_path: Path) -> Path:
    """The managed store. Deliberately not created: receiving must create it."""
    return tmp_path / "handins"


def _drop(directory: Path, name: str, *, content: bytes = b"a,b\n1,2\n", age: float = 0.0) -> Path:
    """Write a file into the watched folder, optionally backdated so ordering is deterministic."""
    path = directory / name
    path.write_bytes(content)
    if age:
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
    return path


# ── candidates ───────────────────────────────────────────────────────────────────────────────


def test_candidates_are_returned_oldest_first(inbox: Path) -> None:
    """Order is the delivery order: last month's file is received before this month's."""
    _drop(inbox, "third.xlsx", age=10)
    _drop(inbox, "first.xlsx", age=300)
    _drop(inbox, "second.xlsx", age=100)

    assert [path.name for path in candidates(inbox)] == [
        "first.xlsx",
        "second.xlsx",
        "third.xlsx",
    ]


def test_candidates_skip_excel_lock_files_and_half_written_copies(inbox: Path) -> None:
    """``~$name.xlsx`` is Excel's lock file, and ``.partial`` is kedge's own in-flight write."""
    _drop(inbox, "exposures.xlsx")
    _drop(inbox, "~$exposures.xlsx")
    _drop(inbox, "exposures.xlsx.partial")

    assert [path.name for path in candidates(inbox, glob="*")] == ["exposures.xlsx"]


def test_candidates_ignore_files_the_glob_does_not_match(inbox: Path) -> None:
    _drop(inbox, "exposures.xlsx")
    _drop(inbox, "readme.txt")

    assert [path.name for path in candidates(inbox)] == ["exposures.xlsx"]


def test_a_missing_watched_folder_names_the_setting_to_correct(tmp_path: Path) -> None:
    """The likeliest cause is a typo in kedge.toml, so the message says which key."""
    with pytest.raises(WatchError, match=r"ingest\.watch_dir"):
        candidates(tmp_path / "not-there")


# ── stability ────────────────────────────────────────────────────────────────────────────────


def test_a_file_that_has_stopped_changing_settles(inbox: Path) -> None:
    path = _drop(inbox, "exposures.xlsx")

    assert wait_until_stable(path, settle_seconds=0.0, poll_seconds=0.01) is True


def test_a_file_that_vanished_mid_wait_is_not_stable(inbox: Path) -> None:
    """A file deleted between the event and the hash is not an error; it is simply not there."""
    assert wait_until_stable(inbox / "gone.xlsx", settle_seconds=0.0, poll_seconds=0.01) is False


def test_waiting_for_stability_gives_up_rather_than_blocking_for_ever(inbox: Path) -> None:
    """A copy that never finishes must not hold a scheduled run open indefinitely."""
    path = _drop(inbox, "exposures.xlsx")

    settled = wait_until_stable(path, settle_seconds=60.0, poll_seconds=0.01, timeout_seconds=0.05)

    assert settled is False


def test_a_file_that_has_not_settled_is_never_hashed_into_the_store(
    inbox: Path, store_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reproducibility claim rests on this: a partial file's digest describes nothing.

    A 200MB extract copied across a share is readable and wrong for as long as the copy takes,
    so an unsettled candidate is left where it is and picked up by the next sweep.
    """
    _drop(inbox, "exposures.xlsx")
    monkeypatch.setattr(WATCH_MODULE, "wait_until_stable", lambda *_a, **_k: False)

    assert scan_once(inbox, store_dir=store_dir) == []
    assert not store_dir.exists()


# ── sweeping ─────────────────────────────────────────────────────────────────────────────────


def test_a_swept_file_becomes_a_watched_hand_in_in_the_managed_store(
    inbox: Path, store_dir: Path
) -> None:
    source = _drop(inbox, "exposures.xlsx")

    received = scan_once(inbox, store_dir=store_dir, settle_seconds=0.0)

    assert len(received) == 1
    handin = received[0]
    assert handin.source == "watched"
    assert handin.original_name == "exposures.xlsx"
    assert handin.sha256 == store.hash_file(source)
    assert handin.path.is_relative_to(store_dir), "the record must point at the managed copy"
    assert handin.path.read_bytes() == source.read_bytes()
    assert source.exists(), "the sending team's file is copied, never moved"


def test_sweeping_the_same_folder_twice_receives_nothing_the_second_time(
    inbox: Path, store_dir: Path
) -> None:
    """Idempotence is what makes an hourly scheduled sweep safe."""
    _drop(inbox, "exposures.xlsx")

    first = scan_once(inbox, store_dir=store_dir, settle_seconds=0.0)
    second = scan_once(inbox, store_dir=store_dir, settle_seconds=0.0)

    assert len(first) == 1
    assert second == []


def test_dedupe_can_be_turned_off_so_every_sweep_reports_the_file(
    inbox: Path, store_dir: Path
) -> None:
    """``ingest.dedupe_by_hash = false`` means "tell me every time", and must reach this far."""
    _drop(inbox, "exposures.xlsx")

    scan_once(inbox, store_dir=store_dir, settle_seconds=0.0, dedupe=False)
    again = scan_once(inbox, store_dir=store_dir, settle_seconds=0.0, dedupe=False)

    assert len(again) == 1


def test_an_empty_folder_sweeps_to_nothing(inbox: Path, store_dir: Path) -> None:
    assert scan_once(inbox, store_dir=store_dir, settle_seconds=0.0) == []


def test_a_sweep_only_takes_what_the_glob_matches(inbox: Path, store_dir: Path) -> None:
    _drop(inbox, "exposures.csv")
    _drop(inbox, "notes.txt")

    received = scan_once(inbox, store_dir=store_dir, glob="*.csv", settle_seconds=0.0)

    assert [handin.original_name for handin in received] == ["exposures.csv"]


def test_sweeping_a_folder_that_does_not_exist_says_so(tmp_path: Path, store_dir: Path) -> None:
    with pytest.raises(WatchError, match="does not exist"):
        scan_once(tmp_path / "not-there", store_dir=store_dir)


# ── the watcher ──────────────────────────────────────────────────────────────────────────────


def test_starting_a_watcher_sweeps_first_so_waiting_files_are_not_missed(
    inbox: Path, store_dir: Path
) -> None:
    """A file that landed before kedge started is still a hand-in; no event will ever fire."""
    _drop(inbox, "exposures.xlsx")
    seen: list[str] = []

    folder = WatchedFolder(inbox, store_dir=store_dir, settle_seconds=0.0)
    try:
        folder.start(lambda handin: seen.append(handin.original_name))
    finally:
        folder.stop()

    assert seen == ["exposures.xlsx"]


def test_watching_the_same_folder_twice_is_refused(inbox: Path, store_dir: Path) -> None:
    folder = WatchedFolder(inbox, store_dir=store_dir, settle_seconds=0.0)
    try:
        folder.start(lambda _handin: None)
        with pytest.raises(WatchError, match="already watching"):
            folder.start(lambda _handin: None)
    finally:
        folder.stop()


def test_stopping_a_watcher_is_safe_before_it_starts_and_twice_after(
    inbox: Path, store_dir: Path
) -> None:
    """Ctrl-C can arrive at any point in the command, including before the observer exists."""
    folder = WatchedFolder(inbox, store_dir=store_dir, settle_seconds=0.0)

    folder.stop()
    folder.start(lambda _handin: None)
    folder.stop()
    folder.stop()


def test_the_context_manager_stops_the_watcher(inbox: Path, store_dir: Path) -> None:
    with WatchedFolder(inbox, store_dir=store_dir, settle_seconds=0.0) as folder:
        folder.start(lambda _handin: None)

    assert folder.wait(timeout_seconds=0.0) is True


def test_waiting_returns_as_soon_as_the_watcher_is_stopped(inbox: Path, store_dir: Path) -> None:
    """``kedge watch`` parks here, so a stop from another thread must not go unnoticed."""
    folder = WatchedFolder(inbox, store_dir=store_dir, settle_seconds=0.0)
    folder.start(lambda _handin: None)
    threading.Timer(0.05, folder.stop).start()

    started = time.monotonic()
    stopped = folder.wait(timeout_seconds=10.0)

    assert stopped is True
    assert time.monotonic() - started < 5.0, "waiting must not sit out the whole timeout"


def test_waiting_times_out_when_nothing_stops_the_watcher(inbox: Path, store_dir: Path) -> None:
    folder = WatchedFolder(inbox, store_dir=store_dir, settle_seconds=0.0)
    folder.start(lambda _handin: None)
    try:
        assert folder.wait(timeout_seconds=0.05) is False
    finally:
        folder.stop()


def test_watch_hands_the_store_settings_to_the_folder_it_starts(
    inbox: Path, store_dir: Path
) -> None:
    """``ingest.copy_on_select`` and ``ingest.dedupe_by_hash`` are configurable, so they have to
    survive the convenience wrapper rather than being silently defaulted."""
    folder = watch(
        inbox,
        lambda _handin: None,
        store_dir=store_dir,
        glob="*.csv",
        contract="exposures",
        copy_on_select=False,
        dedupe=False,
        settle_seconds=0.0,
    )
    try:
        assert (folder.glob, folder.contract) == ("*.csv", "exposures")
        assert folder.copy_on_select is False
        assert folder.dedupe is False
    finally:
        folder.stop()
