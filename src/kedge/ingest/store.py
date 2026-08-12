"""The managed hand-in store: where a received file lands, and what proves it landed.

Layout under the store root (by default ``<project>.kedge/handins``)::

    handins/
      2026-07-25/
        4f2b91c8ad03-exposures.xlsx      # <sha256[:12]>-<original name>
      index.jsonl                        # one line per receipt, append-only
      profiles/
        4f2b91c8ad03.json                # the accepted shape of that file
        last-accepted.json               # pointer to the shape drift compares against

Three properties are load-bearing.

**The date partition** keeps a directory listing readable after a year of monthly hand-ins,
and makes "which file did the March run consume" answerable from the filesystem alone. The
date is UTC, matching every other timestamp kedge records.

**The hash in the filename** means two files that differ land in two places even when the
sending team reuses the same name every month, which they always do. It is also the dedupe
key: the same bytes are never written twice, however many times they are handed in.

**The index is append-only.** Each line is a *receipt*, not a file: re-receiving bytes that
are already stored writes a new line pointing at the existing managed copy. That is exactly
the audit claim worth making -- *this run consumed this file* -- and it survives the store
being pruned, because the line records the hash rather than depending on the file.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from kedge.errors import IngestError
from kedge.ingest.model import HandIn, HandInProfile

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime

logger = logging.getLogger(__name__)

__all__ = [
    "INDEX_FILENAME",
    "LAST_ACCEPTED_FILENAME",
    "PROFILES_DIRNAME",
    "StoreError",
    "accept_profile",
    "hash_bytes",
    "hash_file",
    "history",
    "index_path",
    "last_accepted_profile",
    "load_profile",
    "managed_filename",
    "receipt_for_hash",
    "record_receipt",
    "store_bytes",
    "store_copy",
    "store_profile",
]

INDEX_FILENAME = "index.jsonl"
PROFILES_DIRNAME = "profiles"
LAST_ACCEPTED_FILENAME = "last-accepted.json"

_HASH_CHUNK_BYTES = 1024 * 1024
_SHORT_HASH_LENGTH = 12
_MAX_NAME_LENGTH = 96
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class StoreError(IngestError):
    """A hand-in could not be written to, or read from, the managed store."""


# =============================================================================
# HASHING
# =============================================================================


def hash_bytes(data: bytes) -> str:
    """Return the SHA-256 hex digest of an in-memory payload."""
    return hashlib.sha256(data).hexdigest()


def hash_file(path: Path) -> str:
    """Return the SHA-256 hex digest of a file, read in chunks.

    Chunked deliberately: a selected hand-in is hashed *in place* before anything is copied,
    and a hand-in on a shared drive can be far larger than the drag-and-drop ceiling that
    forced the user to the file browser in the first place.

    Args:
        path: The file to hash. It is not modified.

    Returns:
        The lowercase hex digest.

    Raises:
        StoreError: If the file cannot be read.
    """
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_HASH_CHUNK_BYTES):
                digest.update(chunk)
    except OSError as exc:
        msg = f"could not read {path} to hash it: {exc}"
        raise StoreError(msg) from exc
    return digest.hexdigest()


# =============================================================================
# PATHS
# =============================================================================


def _safe_name(original_name: str) -> str:
    """Reduce a supplied filename to something safe to write on any filesystem.

    Directory components are discarded outright. A hand-in's name arrives from another
    team's export and is never trusted to be a bare filename.
    """
    bare = Path(original_name.replace("\\", "/")).name
    cleaned = _UNSAFE_NAME.sub("-", bare).strip("-")
    if not cleaned or set(cleaned) <= {".", "-"}:
        return "handin"
    if len(cleaned) <= _MAX_NAME_LENGTH:
        return cleaned
    stem, _, suffix = cleaned.rpartition(".")
    if not stem:
        return cleaned[:_MAX_NAME_LENGTH]
    keep = max(1, _MAX_NAME_LENGTH - len(suffix) - 1)
    return f"{stem[:keep]}.{suffix}"


def managed_filename(sha256: str, original_name: str) -> str:
    """Return the filename a hand-in takes inside the store: ``<sha256[:12]>-<name>``."""
    return f"{sha256[:_SHORT_HASH_LENGTH]}-{_safe_name(original_name)}"


def _dated_dir(root: Path, received_at: datetime) -> Path:
    return root / received_at.strftime("%Y-%m-%d")


def index_path(root: Path) -> Path:
    """Return the path of the append-only receipt index."""
    return root / INDEX_FILENAME


def _profiles_dir(root: Path) -> Path:
    return root / PROFILES_DIRNAME


def _ensure_dir(directory: Path) -> None:
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        msg = f"could not create the hand-in store directory {directory}: {exc}"
        raise StoreError(msg) from exc


# =============================================================================
# WRITING FILES INTO THE STORE
# =============================================================================


def store_bytes(root: Path, *, data: bytes, original_name: str, received_at: datetime) -> Path:
    """Write a dropped payload into the store and return its managed path.

    This is the step that makes an upload reproducible. Until the bytes are on disk they
    exist only in kernel memory, and the notebook that reads them will yield nothing when it
    is reopened tomorrow (PLAN 2.8).

    Args:
        root: The store root.
        data: The file's bytes, exactly as uploaded.
        original_name: The name the sender used, for the human-readable half of the filename.
        received_at: The receipt timestamp, which selects the date partition.

    Returns:
        The managed path. An existing file with the same name is left untouched: the name
        contains the content hash, so it already holds these bytes.

    Raises:
        StoreError: If the file cannot be written.
    """
    directory = _dated_dir(root, received_at)
    _ensure_dir(directory)
    destination = directory / managed_filename(hash_bytes(data), original_name)
    if destination.exists():
        logger.debug("managed copy already present, not rewriting: %s", destination)
        return destination
    temporary = destination.with_name(f"{destination.name}.partial")
    try:
        temporary.write_bytes(data)
        temporary.replace(destination)
    except OSError as exc:
        msg = f"could not write the hand-in to {destination}: {exc}"
        raise StoreError(msg) from exc
    logger.info("stored dropped hand-in %s (%d bytes)", destination.name, len(data))
    return destination


def store_copy(root: Path, *, source: Path, sha256: str, received_at: datetime) -> Path:
    """Copy a selected or watched file into the store and return its managed path.

    Copying is the default because a shared-drive path is not a stable artifact: the sending
    team overwrites it next month, and a notebook that references it in place silently starts
    reading different numbers.

    Args:
        root: The store root.
        source: The file the user selected, or that landed in the watched folder.
        sha256: The digest already computed from ``source``, so it is not read twice.
        received_at: The receipt timestamp, which selects the date partition.

    Returns:
        The managed path.

    Raises:
        StoreError: If the file cannot be copied.
    """
    directory = _dated_dir(root, received_at)
    _ensure_dir(directory)
    destination = directory / managed_filename(sha256, source.name)
    if destination.exists():
        logger.debug("managed copy already present, not recopying: %s", destination)
        return destination
    temporary = destination.with_name(f"{destination.name}.partial")
    try:
        shutil.copy2(source, temporary)
        temporary.replace(destination)
    except OSError as exc:
        msg = f"could not copy the hand-in {source} into the store at {destination}: {exc}"
        raise StoreError(msg) from exc
    # Deliberately does not say "selected": the same copy serves a browser selection and a file
    # that landed in the watched folder, and `kedge watch` printing "selected" beside an audit
    # line that says "watched" is a small contradiction with no upside. Which source it was is
    # recorded on the HandIn and shown in that audit line.
    logger.info("copied hand-in %s into the store", destination.name)
    return destination


# =============================================================================
# THE RECEIPT INDEX
# =============================================================================


def record_receipt(root: Path, handin: HandIn) -> None:
    """Append one receipt to the store index.

    Args:
        root: The store root.
        handin: The record to append.

    Raises:
        StoreError: If the index cannot be written.
    """
    _ensure_dir(root)
    line = json.dumps(handin.to_dict(), sort_keys=True)
    try:
        with index_path(root).open("a", encoding="utf-8") as handle:
            handle.write(f"{line}\n")
    except OSError as exc:
        msg = f"could not append to the hand-in index {index_path(root)}: {exc}"
        raise StoreError(msg) from exc


def _iter_index(root: Path) -> Iterator[HandIn]:
    path = index_path(root)
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError as well as OSError: kedge writes this file itself, as ASCII, so a
        # byte that will not decode means the index has been corrupted from outside -- and a
        # corrupt index must never stop a hand-in being received. Degrading to no history
        # costs a duplicate copy in the store, which is the cheap end of the trade.
        logger.warning("could not read the hand-in index %s: %s", path, exc)
        return
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            yield HandIn.from_dict(json.loads(stripped))
        except (json.JSONDecodeError, ValueError) as exc:
            # A corrupt line must never stop a hand-in being received.
            logger.warning("ignoring unusable line %d of %s: %s", number, path, exc)


def history(root: Path) -> list[HandIn]:
    """Return every readable receipt, oldest first."""
    return list(_iter_index(root))


def receipt_for_hash(root: Path, sha256: str) -> HandIn | None:
    """Return the most recent receipt for these exact bytes whose managed copy still exists.

    This is the dedupe lookup. It matches on the full digest rather than the twelve
    characters that appear in the filename, so a truncated-prefix collision cannot cause one
    hand-in to be mistaken for another.

    Args:
        root: The store root.
        sha256: The full hex digest to look for.

    Returns:
        The newest matching receipt, or ``None`` if these bytes are new here.
    """
    for handin in reversed(history(root)):
        if handin.sha256 == sha256 and handin.exists():
            return handin
    return None


# =============================================================================
# ACCEPTED SHAPES
# =============================================================================


def store_profile(root: Path, profile: HandInProfile) -> Path:
    """Write a hand-in's profile into the store and return its path.

    Storing is not accepting: a profile is written for every hand-in that is examined, but
    only :func:`accept_profile` moves the pointer that drift compares against.

    Raises:
        StoreError: If the profile cannot be written.
    """
    directory = _profiles_dir(root)
    _ensure_dir(directory)
    destination = directory / f"{profile.sha256[:_SHORT_HASH_LENGTH]}.json"
    try:
        destination.write_text(
            json.dumps(profile.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )
    except OSError as exc:
        msg = f"could not write the hand-in profile {destination}: {exc}"
        raise StoreError(msg) from exc
    return destination


def accept_profile(root: Path, profile: HandInProfile) -> Path:
    """Record a profile as the accepted baseline that the next hand-in is compared against.

    Persisting this is what makes "the last accepted hand-in" mean anything across runs.
    Without it, drift detection can only compare two files handed in during the same
    session, which is not the situation it exists for.

    Args:
        root: The store root.
        profile: The shape to accept.

    Returns:
        The path of the pointer file.

    Raises:
        StoreError: If the pointer cannot be written.
    """
    store_profile(root, profile)
    destination = _profiles_dir(root) / LAST_ACCEPTED_FILENAME
    try:
        destination.write_text(
            json.dumps(profile.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
        )
    except OSError as exc:
        msg = f"could not record the accepted hand-in shape at {destination}: {exc}"
        raise StoreError(msg) from exc
    logger.info("accepted hand-in shape sha256:%s as the drift baseline", profile.sha256[:12])
    return destination


def _read_profile(path: Path) -> HandInProfile | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("ignoring unreadable hand-in profile %s: %s", path, exc)
        return None
    try:
        return HandInProfile.from_dict(raw)
    except ValueError as exc:
        logger.warning("ignoring malformed hand-in profile %s: %s", path, exc)
        return None


def load_profile(root: Path, sha256: str) -> HandInProfile | None:
    """Return the stored profile for these bytes, or ``None`` if there is not one."""
    return _read_profile(_profiles_dir(root) / f"{sha256[:_SHORT_HASH_LENGTH]}.json")


def last_accepted_profile(root: Path) -> HandInProfile | None:
    """Return the shape drift is measured against, or ``None`` on the first hand-in."""
    return _read_profile(_profiles_dir(root) / LAST_ACCEPTED_FILENAME)
