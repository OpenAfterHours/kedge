"""Hand-in intake: three entry points, one reproducible record.

The problem this package exists for is stated in PLAN 2.8 and is worth restating, because it
is the reason the module is not simply "open the file the user picked".

marimo's two native file inputs return different things. ``mo.ui.file(kind="area")`` gives
``FileUploadResults(name, contents)`` -- the whole file held in kernel memory, with no path
and a 100MB ceiling. ``mo.ui.file_browser(...)`` gives ``FileBrowserFileInfo(id, path, name,
is_directory)`` -- a path, no bytes, no ceiling. **An uploaded file is not reproducible.**
Reopen the notebook tomorrow and the bytes are gone: the cell yields nothing and the whole
downstream graph is dead. A path-based hand-in re-runs. For a tool whose pitch is "a
controlled, repeatable process", that is not a small distinction.

So both converge, along with a watched folder, into :func:`receive`, which returns a
:class:`HandIn` whose ``path`` is always the managed copy and never the transient upload.
Notebook code downstream is identical regardless of how the file arrived, and the recorded
SHA-256 is what makes the audit line defensible: *this run consumed this file*.

The intended notebook sequence::

    handin  = kedge.ingest.receive(selector.value, store_dir=ws.handins_dir)
    profile, drift = kedge.ingest.check_drift(handin, store_dir=ws.handins_dir)
    report  = kedge.contracts.validate(handin, contract)
    frame, layout = kedge.ingest.read_data(handin.path, sheet=contract.sheet)

Drift is reported before the contract check on purpose: "column ``EAD`` became ``EAD_GBP``"
is a far more useful message than a schema-validation traceback.

The last line is :func:`~kedge.ingest.drift.read_data` rather than a bare ``pl.read_excel``
because it is the one reader profiling, validation and inference all go through, and reading
the file any other way means the frame downstream is not the frame that was validated. Pass
the contract's ``sheet`` and ``header_row`` to all three calls, or none of them.
"""

from __future__ import annotations

from kedge.ingest.drift import (
    HandInReadError,
    check_drift,
    compare,
    detect_layout,
    detect_totals_row,
    dtype_name,
    profile_frame,
    profile_handin,
    read_data,
    read_frame,
)
from kedge.ingest.model import (
    DriftItem,
    DriftKind,
    DriftReport,
    HandIn,
    HandInProfile,
    HandInSource,
    Upload,
)
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
from kedge.ingest.store import (
    StoreError,
    accept_profile,
    hash_bytes,
    hash_file,
    history,
    last_accepted_profile,
    load_profile,
    receipt_for_hash,
)
from kedge.ingest.watch import WatchedFolder, WatchError, scan_once, watch

__all__ = [
    "UPLOAD_SIZE_LIMIT_BYTES",
    "DriftItem",
    "DriftKind",
    "DriftReport",
    "EmptySelectionError",
    "HandIn",
    "HandInProfile",
    "HandInReadError",
    "HandInSource",
    "StoreError",
    "UnsupportedPayloadError",
    "Upload",
    "UploadTooLargeError",
    "WatchError",
    "WatchedFolder",
    "accept_profile",
    "check_drift",
    "compare",
    "detect_layout",
    "detect_totals_row",
    "dtype_name",
    "exceeds_upload_limit",
    "hash_bytes",
    "hash_file",
    "history",
    "last_accepted_profile",
    "load_profile",
    "profile_frame",
    "profile_handin",
    "read_data",
    "read_frame",
    "receipt_for_hash",
    "receive",
    "receive_many",
    "receive_paths",
    "scan_once",
    "size_guidance",
    "watch",
]
