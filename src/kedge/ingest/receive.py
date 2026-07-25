"""Normalise every way a hand-in can arrive into a single :class:`~kedge.ingest.model.HandIn`.

marimo gives two native entry points and they return different things, which is the whole
design problem this module solves (PLAN 2.8, verified against marimo 0.23.15):

============================  ==========================================  ==================
Component                     ``.value`` element                          What is missing
============================  ==========================================  ==================
``mo.ui.file(kind="area")``   ``FileUploadResults(name: str,              no path; bytes sit
                              contents: bytes)``                          in kernel memory
``mo.ui.file_browser(...)``   ``FileBrowserFileInfo(id, path: Path,       no bytes
                              name, is_directory)``
============================  ==========================================  ==================

An uploaded file is not reproducible. Reopen the notebook tomorrow and the bytes are gone,
the cell yields nothing, and the whole downstream graph is dead. A path-based hand-in
re-runs. So :func:`receive` takes either payload -- plus a plain path, which is how the
watched folder feeds in -- and returns one record whose ``path`` is always the managed copy.

Nothing here imports marimo. The two payload shapes are matched **structurally**, on the
attributes they carry, so this module is testable without a kernel, adds no import cost to a
notebook that never uses the drop zone, and does not break when marimo moves a class between
private modules. :class:`~kedge.ingest.model.Upload` is kedge's own copy of the drop-zone
shape for callers that want to construct one directly.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kedge.errors import IngestError
from kedge.ingest import store
from kedge.ingest.model import HandIn, HandInSource, Upload, utcnow

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

logger = logging.getLogger(__name__)

__all__ = [
    "UPLOAD_SIZE_LIMIT_BYTES",
    "EmptySelectionError",
    "UnsupportedPayloadError",
    "UploadTooLargeError",
    "exceeds_upload_limit",
    "receive",
    "receive_many",
    "receive_paths",
    "size_guidance",
]

UPLOAD_SIZE_LIMIT_BYTES = 100_000_000
"""marimo's ``mo.ui.file`` ``max_size`` default, verified on 0.23.15.

It is 100,000,000 bytes -- decimal megabytes, not 100 MiB. A file above this never reaches
the kernel through the drop zone at all, so the useful behaviour is to say so and name the
two entry points that have no ceiling, rather than to fail opaquely (PLAN 6.2).
"""


class UnsupportedPayloadError(IngestError):
    """The object handed to :func:`receive` is not a recognised hand-in payload."""


class EmptySelectionError(IngestError):
    """No file has been chosen yet.

    Normal on the first run of a notebook: the selector cell evaluates before the user has
    picked anything. Guard the cell with ``mo.stop(not selector.value, ...)`` rather than
    letting this reach the user as a traceback.
    """


class UploadTooLargeError(IngestError):
    """A dropped payload is larger than the drag-and-drop ceiling can carry."""


# =============================================================================
# SIZE ROUTING
# =============================================================================


def exceeds_upload_limit(size_bytes: int, *, limit: int = UPLOAD_SIZE_LIMIT_BYTES) -> bool:
    """Whether a file of this size is too big for the drag-and-drop zone."""
    return size_bytes > limit


def size_guidance(size_bytes: int, *, limit: int = UPLOAD_SIZE_LIMIT_BYTES) -> str | None:
    """Return a message routing an over-large hand-in away from the drop zone, or ``None``.

    Call this from the selector cell against a candidate file's size to warn *before* the
    user drags a 400MB extract into a zone that will refuse it without explanation.

    Args:
        size_bytes: The file's size.
        limit: The drop-zone ceiling; defaults to marimo's own.

    Returns:
        A sentence naming the two entry points with no ceiling, or ``None`` if the file
        would upload fine.
    """
    if not exceeds_upload_limit(size_bytes, limit=limit):
        return None
    return (
        f"this hand-in is {size_bytes / 1_000_000:,.1f}MB, above the "
        f"{limit / 1_000_000:,.0f}MB drag-and-drop ceiling. Use the Select tab "
        f"(mo.ui.file_browser), which passes a path rather than bytes and has no size "
        f"limit, or drop the file into the watched folder."
    )


# =============================================================================
# PAYLOAD NORMALISATION
# =============================================================================


class _Incoming:
    """One nominated file, reduced to either bytes or a path plus its intended source."""

    __slots__ = ("contents", "name", "path", "source")

    def __init__(
        self,
        *,
        name: str,
        source: HandInSource,
        contents: bytes | None = None,
        path: Path | None = None,
    ) -> None:
        self.name = name
        self.source = source
        self.contents = contents
        self.path = path


def _is_upload_shaped(payload: Any) -> bool:
    """Match ``mo.ui.file``'s ``FileUploadResults`` structurally: a name and some bytes."""
    return isinstance(getattr(payload, "contents", None), bytes) and isinstance(
        getattr(payload, "name", None), str
    )


def _is_browser_shaped(payload: Any) -> bool:
    """Match ``mo.ui.file_browser``'s ``FileBrowserFileInfo`` structurally: a path and a name."""
    return isinstance(getattr(payload, "path", None), Path) and isinstance(
        getattr(payload, "name", None), str
    )


def _normalise_one(payload: Any, *, source: HandInSource | None) -> _Incoming | None:
    if isinstance(payload, Upload):
        return _Incoming(name=payload.name, source=source or "dropped", contents=payload.contents)

    if _is_upload_shaped(payload):
        return _Incoming(
            name=str(payload.name), source=source or "dropped", contents=bytes(payload.contents)
        )

    if _is_browser_shaped(payload):
        if getattr(payload, "is_directory", False):
            logger.debug("ignoring directory selection %s", payload.path)
            return None
        return _Incoming(
            name=str(payload.name), source=source or "selected", path=Path(payload.path)
        )

    if isinstance(payload, Path | str):
        path = Path(payload).expanduser()
        return _Incoming(name=path.name, source=source or "selected", path=path)

    return _unsupported(payload)


def _unsupported(payload: Any) -> _Incoming:
    msg = (
        f"cannot receive a hand-in from {type(payload).__name__}. Pass the value of "
        f"mo.ui.file (FileUploadResults: name + contents), the value of mo.ui.file_browser "
        f"(FileBrowserFileInfo: path + name), a kedge.ingest.Upload, or a filesystem path."
    )
    raise UnsupportedPayloadError(msg)


def _normalise(payload: Any, *, source: HandInSource | None) -> list[_Incoming]:
    """Flatten any accepted payload -- single or sequence -- into nominated files."""
    if payload is None:
        return []
    if isinstance(payload, Path | str) or not _is_sequence(payload):
        one = _normalise_one(payload, source=source)
        return [] if one is None else [one]
    incoming: list[_Incoming] = []
    for item in payload:
        one = _normalise_one(item, source=source)
        if one is not None:
            incoming.append(one)
    return incoming


def _is_sequence(payload: Any) -> bool:
    """Whether the payload is a container of nominations rather than one nomination.

    ``mo.ui.file`` and ``mo.ui.file_browser`` both return a ``tuple`` from ``.value``, even
    with ``multiple=False``, so this path is the normal one rather than the exception.
    """
    if _is_upload_shaped(payload) or _is_browser_shaped(payload):
        return False
    return isinstance(payload, list | tuple | set | frozenset)


# =============================================================================
# RECEIVING
# =============================================================================


def _receive_one(
    incoming: _Incoming,
    *,
    root: Path,
    contract: str | None,
    copy_on_select: bool,
    dedupe: bool,
    upload_limit: int,
    received_at: datetime,
) -> HandIn:
    if incoming.contents is not None:
        size = len(incoming.contents)
        if exceeds_upload_limit(size, limit=upload_limit):
            raise UploadTooLargeError(str(size_guidance(size, limit=upload_limit)))
        sha256 = store.hash_bytes(incoming.contents)
        managed = _dedupe_path(root, sha256, dedupe=dedupe)
        if managed is None:
            managed = store.store_bytes(
                root,
                data=incoming.contents,
                original_name=incoming.name,
                received_at=received_at,
            )
    else:
        source_path = _require_file(incoming.path)
        size = source_path.stat().st_size
        sha256 = store.hash_file(source_path)
        managed = _dedupe_path(root, sha256, dedupe=dedupe)
        if managed is None:
            managed = (
                store.store_copy(root, source=source_path, sha256=sha256, received_at=received_at)
                if copy_on_select
                else source_path
            )
            if not copy_on_select:
                logger.warning(
                    "referencing %s in place; a shared-drive path is not a stable artifact "
                    "and this hand-in may not be reproducible",
                    source_path,
                )

    handin = HandIn(
        path=managed,
        original_name=incoming.name,
        sha256=sha256,
        size_bytes=size,
        received_at=received_at,
        source=incoming.source,
        contract=contract,
    )
    store.record_receipt(root, handin)
    logger.info("received hand-in: %s", handin.audit_line())
    return handin


def _dedupe_path(root: Path, sha256: str, *, dedupe: bool) -> Path | None:
    if not dedupe:
        return None
    existing = store.receipt_for_hash(root, sha256)
    if existing is None:
        return None
    logger.debug("hand-in sha256:%s is already stored at %s", sha256[:12], existing.path)
    return existing.path


def _require_file(path: Path | None) -> Path:
    if path is None:  # pragma: no cover - guarded by _normalise_one
        msg = "a selected hand-in has no path"
        raise UnsupportedPayloadError(msg)
    if not path.exists():
        msg = f"the selected hand-in {path} does not exist"
        raise IngestError(msg)
    if not path.is_file():
        msg = f"the selected hand-in {path} is a directory, not a file"
        raise IngestError(msg)
    return path


def receive_many(
    payload: Any,
    *,
    store_dir: Path,
    source: HandInSource | None = None,
    contract: str | None = None,
    copy_on_select: bool = True,
    dedupe: bool = True,
    upload_limit: int = UPLOAD_SIZE_LIMIT_BYTES,
    received_at: datetime | None = None,
) -> list[HandIn]:
    """Receive every file in a payload into the managed store.

    Accepts anything :func:`receive` accepts, and returns one record per file. An empty
    selection yields an empty list rather than raising, because a multi-file selector that
    has not been touched yet is not an error.

    Args:
        payload: One nomination or a sequence of them.
        store_dir: The managed store root, normally ``Workspace.handins_dir``.
        source: Override the label recorded on the resulting records. The watched folder
            passes ``"watched"``; interactive callers leave this alone and let the payload
            shape decide.
        contract: Name of the contract these hand-ins are expected to satisfy.
        copy_on_select: Copy a nominated path into the store rather than referencing it in
            place. On by default; ``kedge.toml``'s ``ingest.copy_on_select`` sets it.
        dedupe: Reuse the managed copy when these exact bytes are already stored.
        upload_limit: The drag-and-drop ceiling applied to byte payloads.
        received_at: Receipt timestamp; defaults to now, in UTC.

    Returns:
        One :class:`~kedge.ingest.model.HandIn` per file, in payload order.

    Raises:
        UnsupportedPayloadError: The payload is not a recognised shape.
        UploadTooLargeError: A dropped payload exceeds the drag-and-drop ceiling.
        IngestError: A nominated path does not exist or is not a file.
    """
    incoming = _normalise(payload, source=source)
    if not incoming:
        return []
    stamp = received_at or utcnow()
    return [
        _receive_one(
            item,
            root=store_dir,
            contract=contract,
            copy_on_select=copy_on_select,
            dedupe=dedupe,
            upload_limit=upload_limit,
            received_at=stamp,
        )
        for item in incoming
    ]


def receive(
    payload: Any,
    *,
    store_dir: Path,
    source: HandInSource | None = None,
    contract: str | None = None,
    copy_on_select: bool = True,
    dedupe: bool = True,
    upload_limit: int = UPLOAD_SIZE_LIMIT_BYTES,
    received_at: datetime | None = None,
) -> HandIn:
    """Receive one hand-in into the managed store and return its record.

    The single call the notebook's ingest cell makes. It accepts the value of ``mo.ui.file``
    (bytes, no path), the value of ``mo.ui.file_browser`` (a path, no bytes), a
    :class:`~kedge.ingest.model.Upload`, a plain path, or a one-element sequence of any of
    those -- and returns the same record in every case, pointing at the managed copy.

    Args:
        payload: The nominated file. See :func:`receive_many` for the accepted shapes.
        store_dir: The managed store root, normally ``Workspace.handins_dir``.
        source: Override the recorded source label.
        contract: Name of the contract this hand-in is expected to satisfy.
        copy_on_select: Copy a nominated path into the store rather than referencing it.
        dedupe: Reuse the managed copy when these exact bytes are already stored.
        upload_limit: The drag-and-drop ceiling applied to byte payloads.
        received_at: Receipt timestamp; defaults to now, in UTC.

    Returns:
        The :class:`~kedge.ingest.model.HandIn` for the nominated file.

    Raises:
        EmptySelectionError: Nothing has been selected yet.
        UnsupportedPayloadError: The payload is not a recognised shape.
        UploadTooLargeError: A dropped payload exceeds the drag-and-drop ceiling.
        IngestError: More than one file was nominated, or a nominated path is unusable.

    Example:
        >>> handin = receive(selector.value, store_dir=workspace.handins_dir)
        >>> frame = pl.read_excel(handin.path)
    """
    received = receive_many(
        payload,
        store_dir=store_dir,
        source=source,
        contract=contract,
        copy_on_select=copy_on_select,
        dedupe=dedupe,
        upload_limit=upload_limit,
        received_at=received_at,
    )
    if not received:
        msg = (
            "no hand-in has been selected yet. Guard the cell with "
            "`mo.stop(not selector.value, mo.md('Drop or select a hand-in to begin'))` "
            "so the notebook waits rather than erroring."
        )
        raise EmptySelectionError(msg)
    if len(received) > 1:
        names = ", ".join(h.original_name for h in received)
        msg = (
            f"{len(received)} files were nominated ({names}) but receive() returns one "
            f"hand-in. Use receive_many() for a multi-file selector."
        )
        raise IngestError(msg)
    return received[0]


def receive_paths(
    paths: Sequence[Path],
    *,
    store_dir: Path,
    source: HandInSource = "watched",
    contract: str | None = None,
    copy_on_select: bool = True,
    dedupe: bool = True,
) -> list[HandIn]:
    """Receive a list of paths, labelled with a given source. Used by the watched folder."""
    return receive_many(
        list(paths),
        store_dir=store_dir,
        source=source,
        contract=contract,
        copy_on_select=copy_on_select,
        dedupe=dedupe,
    )
