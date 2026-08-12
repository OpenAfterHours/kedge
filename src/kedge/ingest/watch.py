"""The third hand-in source: a folder kedge watches.

Drop and select are the interactive entry points. This one is the production path. The
stated flow is a hand-in from another team, which in practice means a file landing in a
shared folder on a schedule -- and because a watched hand-in arrives as a *path*, it produces
exactly the same reproducible :class:`~kedge.ingest.model.HandIn` a browser selection does.
That is what eventually lets the whole process run unattended (PLAN 2.8).

Two things make this harder than "call ``receive`` when a file appears".

**A file is visible long before it is complete.** A 200MB extract copied across a network
share exists, and is readable, and is wrong, for as long as the copy takes. Hashing it then
records a digest of a partial file and the reproducibility claim quietly becomes false. So
every candidate is held until its size and modification time have stopped moving --
:func:`wait_until_stable`.

**Events are not the only way in.** ``scan_once`` sweeps the folder with no filesystem
watcher at all, which is what a scheduled run wants, is trivially testable, and is the
fallback when watchdog's platform backend misbehaves. The event-driven
:class:`WatchedFolder` is a convenience on top of the same code path, not an alternative to
it.

``kedge watch <workbook>`` is the entry point onto both: ``--once`` calls
:func:`scan_once`, and without it the command starts a :class:`WatchedFolder` and parks on
:meth:`WatchedFolder.wait` until Ctrl-C.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from kedge.errors import IngestError
from kedge.ingest import store
from kedge.ingest.receive import receive_many

if TYPE_CHECKING:
    from collections.abc import Callable

    from kedge.ingest.model import HandIn

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_SETTLE_SECONDS",
    "WatchError",
    "WatchedFolder",
    "candidates",
    "scan_once",
    "wait_until_stable",
    "watch",
]

DEFAULT_SETTLE_SECONDS = 2.0
"""How long a file's size and mtime must hold still before it counts as fully written."""

_DEFAULT_POLL_SECONDS = 0.25
_DEFAULT_STABLE_TIMEOUT = 300.0
_WAIT_TICK_SECONDS = 0.5


class WatchError(IngestError):
    """The watched folder could not be established or observed."""


# =============================================================================
# CANDIDATES AND STABILITY
# =============================================================================


def candidates(directory: Path, *, glob: str = "*.xlsx") -> list[Path]:
    """Return the files in a watched folder that match the glob, oldest first.

    Partial files written by kedge's own store, and the temporary files Excel leaves behind
    while a workbook is open (``~$name.xlsx``), are excluded.

    Args:
        directory: The folder being watched.
        glob: Pattern matched against filenames.

    Returns:
        Matching file paths, ordered by modification time.

    Raises:
        WatchError: If the directory does not exist.
    """
    if not directory.is_dir():
        msg = (
            f"the watched folder {directory} does not exist. Create it, or correct "
            f"ingest.watch_dir in kedge.toml."
        )
        raise WatchError(msg)
    found = [
        path
        for path in directory.glob(glob)
        if path.is_file() and not path.name.startswith("~$") and path.suffix != ".partial"
    ]
    return sorted(found, key=lambda p: (p.stat().st_mtime, p.name))


def wait_until_stable(
    path: Path,
    *,
    settle_seconds: float = DEFAULT_SETTLE_SECONDS,
    poll_seconds: float = _DEFAULT_POLL_SECONDS,
    timeout_seconds: float = _DEFAULT_STABLE_TIMEOUT,
) -> bool:
    """Block until a file has stopped changing, and say whether it did.

    A file that is still being copied into the watched folder is readable and incomplete at
    the same time. Hashing it produces a digest of something that never really existed, which
    would make the audit line -- *this run consumed this file* -- untrue in the one place it
    matters.

    Args:
        path: The candidate file.
        settle_seconds: How long size and mtime must hold still.
        poll_seconds: Interval between checks.
        timeout_seconds: Give up after this long and return False.

    Returns:
        True when the file settled, False if it vanished or was still changing at timeout.
    """
    deadline = time.monotonic() + timeout_seconds
    stable_since: float | None = None
    last: tuple[int, float] | None = None
    while time.monotonic() < deadline:
        try:
            info = path.stat()
        except OSError:
            logger.debug("candidate %s disappeared while waiting for it to settle", path.name)
            return False
        current = (info.st_size, info.st_mtime)
        now = time.monotonic()
        if current != last:
            last, stable_since = current, now
        elif now - (stable_since or now) >= settle_seconds:
            return True
        time.sleep(poll_seconds)
    logger.warning(
        "gave up waiting for %s to stop changing after %.0fs", path.name, timeout_seconds
    )
    return False


# =============================================================================
# SCANNING
# =============================================================================


def scan_once(
    directory: Path,
    *,
    store_dir: Path,
    glob: str = "*.xlsx",
    contract: str | None = None,
    copy_on_select: bool = True,
    dedupe: bool = True,
    settle_seconds: float = DEFAULT_SETTLE_SECONDS,
) -> list[HandIn]:
    """Receive every new file currently sitting in the watched folder.

    Idempotent when ``dedupe`` is on: files already in the store are skipped by hash, so a
    scheduled run can sweep the same folder every hour without producing duplicates.

    Args:
        directory: The folder to sweep.
        store_dir: The managed store root.
        glob: Pattern matched against filenames.
        contract: Name of the contract these hand-ins are expected to satisfy.
        copy_on_select: Copy into the store rather than referencing in place.
        dedupe: Skip files whose bytes are already stored.
        settle_seconds: How long each file must hold still before it is read.

    Returns:
        One record per newly received file, oldest first. Files already stored are omitted
        rather than re-reported.

    Raises:
        WatchError: If the directory does not exist.
    """
    fresh: list[Path] = []
    for path in candidates(directory, glob=glob):
        if not wait_until_stable(path, settle_seconds=settle_seconds):
            continue
        # Hashed here as well as inside receive(): a file already in the store must be
        # skipped rather than re-reported, or every sweep fires the callback again.
        if dedupe and store.receipt_for_hash(store_dir, store.hash_file(path)) is not None:
            logger.debug("watched file %s is already in the store", path.name)
            continue
        fresh.append(path)

    if not fresh:
        return []
    received = receive_many(
        fresh,
        store_dir=store_dir,
        source="watched",
        contract=contract,
        copy_on_select=copy_on_select,
        dedupe=dedupe,
    )
    logger.info("received %d hand-in(s) from the watched folder %s", len(received), directory)
    return received


# =============================================================================
# EVENT-DRIVEN WATCHING
# =============================================================================


class WatchedFolder:
    """Watch a folder and call back with each hand-in that lands in it.

    Thin by design: the filesystem event only triggers a :func:`scan_once` sweep, so the
    event-driven and scheduled paths cannot drift apart in behaviour, and a missed event
    costs nothing because the next sweep picks the file up anyway.

    Example:
        >>> folder = WatchedFolder(Path("//share/handins"), store_dir=ws.handins_dir)
        >>> folder.start(lambda handin: print(handin.audit_line()))
        >>> folder.stop()
    """

    def __init__(
        self,
        directory: Path,
        *,
        store_dir: Path,
        glob: str = "*.xlsx",
        contract: str | None = None,
        copy_on_select: bool = True,
        dedupe: bool = True,
        settle_seconds: float = DEFAULT_SETTLE_SECONDS,
    ) -> None:
        self.directory = directory
        self.store_dir = store_dir
        self.glob = glob
        self.contract = contract
        self.copy_on_select = copy_on_select
        self.dedupe = dedupe
        self.settle_seconds = settle_seconds
        self._observer: object | None = None
        self._lock = threading.Lock()
        self._stopped = threading.Event()

    def sweep(self) -> list[HandIn]:
        """Run one scan now, serialised against the watcher thread."""
        with self._lock:
            return scan_once(
                self.directory,
                store_dir=self.store_dir,
                glob=self.glob,
                contract=self.contract,
                copy_on_select=self.copy_on_select,
                dedupe=self.dedupe,
                settle_seconds=self.settle_seconds,
            )

    def start(self, on_handin: Callable[[HandIn], None]) -> None:
        """Begin watching, sweeping once immediately so pre-existing files are not missed.

        Args:
            on_handin: Called once per received hand-in, on the watcher thread.

        Raises:
            WatchError: If watchdog is unavailable or the folder cannot be observed.
        """
        if self._observer is not None:
            msg = f"already watching {self.directory}"
            raise WatchError(msg)
        self._stopped.clear()

        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError as exc:  # pragma: no cover - watchdog is a declared dependency
            msg = (
                "watchdog is not installed, so the watched folder cannot be observed. "
                "Call scan_once() on a schedule instead."
            )
            raise WatchError(msg) from exc

        for handin in self.sweep():
            on_handin(handin)

        watcher = self

        class _Handler(FileSystemEventHandler):  # type: ignore[misc]
            def on_any_event(self, event: object) -> None:
                if getattr(event, "is_directory", False):
                    return
                for received in watcher.sweep():
                    on_handin(received)

        observer = Observer()
        try:
            observer.schedule(_Handler(), str(self.directory), recursive=False)
            observer.start()
        except (OSError, RuntimeError) as exc:
            msg = f"could not watch the folder {self.directory}: {exc}"
            raise WatchError(msg) from exc
        self._observer = observer
        logger.info("watching %s for %s", self.directory, self.glob)

    def wait(self, *, timeout_seconds: float | None = None) -> bool:
        """Block until the watcher is stopped, and say whether it was.

        watchdog does its work on its own thread, so a caller with nothing else to do -- the
        ``kedge watch`` command is exactly that -- needs somewhere to park. The wait is taken
        in short slices rather than one long one because a console delivers Ctrl-C by
        interrupting the main thread, and a main thread parked in a single indefinite wait is
        the shape that has historically swallowed it on Windows.

        Args:
            timeout_seconds: Give up waiting after this long. ``None`` waits indefinitely,
                which is what the command line does; ``0`` asks the question without waiting
                at all.

        Returns:
            True once the watcher has been stopped, False if the timeout ran out first.
        """
        deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds
        while True:
            remaining = None if deadline is None else max(deadline - time.monotonic(), 0.0)
            if self._stopped.wait(
                _WAIT_TICK_SECONDS if remaining is None else min(_WAIT_TICK_SECONDS, remaining)
            ):
                return True
            if remaining is not None and remaining <= 0:
                return False

    def stop(self, *, timeout_seconds: float = 5.0) -> None:
        """Stop watching. Safe to call when not started, and safe to call twice."""
        observer = self._observer
        self._observer = None
        self._stopped.set()
        if observer is None:
            return
        observer.stop()  # type: ignore[attr-defined]
        observer.join(timeout_seconds)  # type: ignore[attr-defined]
        logger.info("stopped watching %s", self.directory)

    def __enter__(self) -> WatchedFolder:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def watch(
    directory: Path,
    on_handin: Callable[[HandIn], None],
    *,
    store_dir: Path,
    glob: str = "*.xlsx",
    contract: str | None = None,
    copy_on_select: bool = True,
    dedupe: bool = True,
    settle_seconds: float = DEFAULT_SETTLE_SECONDS,
) -> WatchedFolder:
    """Start watching a folder and return the handle that stops it again.

    Args:
        directory: The folder to watch.
        on_handin: Called once per received hand-in, on the watcher thread.
        store_dir: The managed store root.
        glob: Pattern matched against filenames.
        contract: Name of the contract these hand-ins are expected to satisfy.
        copy_on_select: Copy into the store rather than referencing in place.
        dedupe: Skip files whose bytes are already stored.
        settle_seconds: How long each file must hold still before it is read.

    Returns:
        A started :class:`WatchedFolder`. Call ``stop()``, or use it as a context manager.

    Raises:
        WatchError: If the folder does not exist or cannot be observed.
    """
    folder = WatchedFolder(
        directory,
        store_dir=store_dir,
        glob=glob,
        contract=contract,
        copy_on_select=copy_on_select,
        dedupe=dedupe,
        settle_seconds=settle_seconds,
    )
    folder.start(on_handin)
    return folder
