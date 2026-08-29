"""The workbook registry: what kedge has seen, and what state each of them is in.

The hub needs to answer "which workbooks do I know about, and how far has each one got?" before
any :class:`~kedge.workspace.Workspace` exists. That is why this sits beside ``workspace.py``
rather than inside ``server/``: it is a machine-wide fact about the user, not a fact about one
server process, and ``kedge doctor`` has as much business reading it as the browser does.

Plain JSON under ``~/.kedge``, written atomically, exactly like the marker files it sits next to.
SQLite is already in the tree for chat sessions, where the relational shape and the concurrent
writes earn it; a list of a dozen file paths does not.

**The registry records identity, never status.** It holds the path, the name, the hash and when
the workbook was last opened — facts about a decision the user made. Everything a user is shown
about a workbook's *state* is derived from disk on every read: whether the file is still there,
whether a notebook was generated, which plan versions exist and whether one is approved, what the
analysis found, and whether a marimo server of ours is live for it. A registry that remembered
status would be wrong the first time somebody deleted a notebook in Explorer, and the failure mode
of a stale cache here is a user told their work is safe when it is not.

**A workbook that is gone is not necessarily a fault.** The successful end of a conversion is
that the notebook becomes the process and the spreadsheet is retired, so an absent file has
two readings and the registry has to be able to tell them apart. It does that the only way
consistent with the paragraph above: by recording the *decision*. ``released_at`` is a fact
about something the user did, exactly like ``added_at``, and :class:`SourceState` is derived
from it and from disk on every read — ``released`` where a release was recorded, ``linked``
where the file is there, ``missing`` where it is not and nobody said so. An entry is dropped
silently in none of the three; the user chose to add it, "that workbook has moved" is
information, and describing one never raises.

Identity is untouched by any of this. The key is ``slug + sha256(str(path))[:12]`` — a hash
of a *string*, which does not have to resolve to a file to be stable — so a released workbook
keeps the key, the project directory and the notebook it has always had.
"""

from __future__ import annotations

import hashlib
import json
import logging
import zipfile
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from kedge.config import user_dir
from kedge.errors import KedgeError
from kedge.workspace import Workspace, iter_markers

logger = logging.getLogger(__name__)

__all__ = [
    "RECONCILIATION_FILENAME",
    "REGISTRY_FILENAME",
    "REGISTRY_SCHEMA_VERSION",
    "REPORT_FILENAME",
    "WORKBOOK_SUFFIXES",
    "RegistryEntry",
    "RegistryError",
    "SourceState",
    "WorkbookRegistry",
    "WorkbookStatus",
    "describe",
    "live_marimo_servers",
    "reconciliation_path_for",
    "registry_path",
    "report_path_for",
    "validate_workbook",
]

REGISTRY_FILENAME = "registry.json"

REGISTRY_SCHEMA_VERSION = 2
"""2 adds ``released_at``. Every change to this file so far has been additive and both
directions degrade correctly — an older kedge ignores the key it does not know and reads a
released workbook as one that has moved, a newer kedge reads a row without it as linked — so
**a reader must not refuse a version it does not recognise**, and none does. The number moves
anyway, because it is the only record that the row shape changed, and the first time somebody
needs it to mean something is far too late to make it honest retrospectively."""

WORKBOOK_SUFFIXES = frozenset({".xlsx", ".xlsm"})
"""What the hub will accept. ``.xls`` and ``.xlsb`` are not OOXML and the analyser cannot read
them, so accepting one would only defer the failure to somewhere less explicable."""

_ZIP_MAGIC = b"PK\x03\x04"
_CONTENT_TYPES = "[Content_Types].xml"
_HASH_CHUNK = 1 << 20


class RegistryError(KedgeError):
    """The workbook registry could not be read, written, or added to."""


class SourceState(StrEnum):
    """What has become of the workbook an entry names.

    Derived on every read by :func:`describe`, never stored — see the module docstring for why
    the registry holds the decision (``released_at``) and not the state.

    Example:
        >>> SourceState.RELEASED.value
        'released'
    """

    LINKED = "linked"
    """The workbook is where kedge last saw it. The ordinary case, and the only one during a
    conversion."""

    RELEASED = "released"
    """The workbook was retired on purpose: the notebook is the process now. Everything kedge
    derived — the notebook, the plans, the contract, the run records, the acceptance — is
    still there, which is the whole point."""

    MISSING = "missing"
    """The workbook is not there and nobody said it should not be. Moved, renamed, or on a
    drive that is not mounted. This is the one the hub should treat as a fault."""


def registry_path(user_directory: Path | None = None) -> Path:
    """Return the registry file's location, whether or not it exists."""
    return (user_directory or user_dir()) / REGISTRY_FILENAME


# ── validation ───────────────────────────────────────────────────────────────────────────────


def _resolve_workbook_path(path: Path) -> Path:
    """Return ``path`` resolved and shaped like a workbook, without requiring it to exist.

    The half of :func:`validate_workbook` that is about the *name*. Split out because a released
    process has no file left to open, and every check below this one would be a lie about it,
    while these two still hold: the key, the project directory and the notebook name are all
    derived from this path, so a directory or a notebook handed in here would address a workspace
    nothing else agrees with.

    Args:
        path: The candidate workbook path.

    Returns:
        The resolved absolute path.

    Raises:
        RegistryError: The path will not resolve, is a directory, or is not named like a workbook
            kedge can read.
    """
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError as exc:
        msg = f"could not resolve {path}: {exc}"
        raise RegistryError(msg) from exc

    if resolved.is_dir():
        msg = f"{resolved} is a directory, not a workbook file"
        raise RegistryError(msg)

    suffix = resolved.suffix.lower()
    if suffix not in WORKBOOK_SUFFIXES:
        accepted = ", ".join(sorted(WORKBOOK_SUFFIXES))
        extra = (
            " The legacy binary formats .xls and .xlsb are not OOXML; re-save as .xlsx in Excel."
            if suffix in (".xls", ".xlsb")
            else ""
        )
        msg = f"{resolved.name} is not a workbook kedge can read. Accepted: {accepted}.{extra}"
        raise RegistryError(msg)
    return resolved


def validate_workbook(path: Path) -> Path:
    """Return ``path`` resolved, having established that it really is a readable workbook.

    Browsing the real filesystem is the right behaviour for a single-user local tool, so nothing
    here restricts *where* a workbook may live. What it does refuse is a file that is not one:
    the suffix is checked, then the file is actually opened as a zip and asked for its OOXML
    content-types part, because a ``.xlsx`` that is really a CSV someone renamed is a far more
    common mistake than a malicious upload.

    Args:
        path: The candidate workbook.

    Returns:
        The resolved absolute path.

    Raises:
        RegistryError: Naming what was wrong with the file and what would be acceptable.
    """
    resolved = _resolve_workbook_path(path)
    if not resolved.is_file():
        msg = f"no such file: {resolved}"
        raise RegistryError(msg)

    try:
        with resolved.open("rb") as handle:
            magic = handle.read(4)
    except OSError as exc:
        msg = f"could not read {resolved}: {exc}"
        raise RegistryError(msg) from exc
    if magic != _ZIP_MAGIC:
        msg = (
            f"{resolved.name} has a workbook extension but is not a zip archive, so it is not a "
            f"real .xlsx/.xlsm. It may have been renamed from another format."
        )
        raise RegistryError(msg)

    try:
        with zipfile.ZipFile(resolved) as archive:
            names = set(archive.namelist())
    except (OSError, zipfile.BadZipFile) as exc:
        msg = f"{resolved.name} is not a readable zip archive: {exc}"
        raise RegistryError(msg) from exc
    if _CONTENT_TYPES not in names:
        msg = (
            f"{resolved.name} is a zip archive but carries no {_CONTENT_TYPES}, so it is not an "
            f"Office Open XML workbook."
        )
        raise RegistryError(msg)
    return resolved


def _sha256(path: Path) -> str:
    """Hash a file, returning an empty string if it cannot be read."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_HASH_CHUNK):
                digest.update(chunk)
    except OSError as exc:
        logger.warning("could not hash %s: %s", path, exc)
        return ""
    return digest.hexdigest()


# ── the recorded entry ───────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    """One workbook the user has pointed kedge at.

    Only identity and history live here. See the module docstring for why status does not.

    Example:
        >>> RegistryEntry(key="k", path="C:/a.xlsx", name="a.xlsx").open_count
        0
    """

    key: str
    path: str
    name: str
    sha256: str = ""
    size_bytes: int = 0
    mtime_ns: int = 0
    added_at: str = ""
    last_opened_at: str | None = None
    open_count: int = 0
    released_at: str | None = None
    """When the user retired the workbook, if they did.

    A decision, not a status — which is what makes it the one thing about an absent file the
    registry is entitled to remember. Everything downstream (:class:`SourceState`,
    :attr:`WorkbookStatus.exists`) is still derived from disk on every read."""

    @property
    def released(self) -> bool:
        """Whether this workbook was retired on purpose."""
        return self.released_at is not None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable form written to disk."""
        return {
            "key": self.key,
            "path": self.path,
            "name": self.name,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
            "added_at": self.added_at,
            "last_opened_at": self.last_opened_at,
            "open_count": self.open_count,
            "released_at": self.released_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RegistryEntry | None:
        """Rebuild an entry, returning ``None`` if the row is unusable.

        A corrupt row must cost the user that row, never the whole registry and never a
        traceback on the landing page.
        """
        try:
            path = str(raw["path"])
            return cls(
                key=str(raw.get("key") or ""),
                path=path,
                name=str(raw.get("name") or Path(path).name),
                sha256=str(raw.get("sha256") or ""),
                size_bytes=int(raw.get("size_bytes") or 0),
                mtime_ns=int(raw.get("mtime_ns") or 0),
                added_at=str(raw.get("added_at") or ""),
                last_opened_at=(str(raw["last_opened_at"]) if raw.get("last_opened_at") else None),
                open_count=int(raw.get("open_count") or 0),
                # Absent in a registry written before schema 2, which is exactly the linked case.
                released_at=(str(raw["released_at"]) if raw.get("released_at") else None),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("ignoring an unusable registry row: %s", exc)
            return None


# ── the derived view ─────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class WorkbookStatus:
    """A registry entry plus everything derivable about it, read fresh from disk.

    Built by :func:`describe`. Nothing in here is cached between calls, because every one of
    these facts can be changed by the user in Explorer between two loads of the hub page.
    """

    entry: RegistryEntry
    exists: bool = False
    changed_on_disk: bool = False
    source_state: SourceState = SourceState.MISSING
    """Which reading of the workbook applies. Defaults to ``MISSING`` for the same reason
    ``exists`` defaults to ``False``: a status built with nothing known has found nothing."""
    project_dir: str = ""
    notebook_path: str = ""
    notebook_exists: bool = False
    analysis_present: bool = False
    findings: dict[str, int] = field(default_factory=dict)
    cached_values: str = "unknown"
    operation_count: int = 0
    sheet_count: int = 0
    plan_state: str = "none"
    plan_version: int | None = None
    approved_version: int | None = None
    convertible: float | None = None
    blockers: tuple[str, ...] = ()
    reconciliation: str | None = None
    reconciled_at: str | None = None
    report_available: bool = False
    marimo_live: bool = False
    marimo_base_url: str | None = None
    marimo_port: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the shape the hub page consumes."""
        return {
            **self.entry.to_dict(),
            "exists": self.exists,
            "changed_on_disk": self.changed_on_disk,
            "source_state": self.source_state.value,
            "project_dir": self.project_dir,
            "notebook_path": self.notebook_path,
            "notebook_exists": self.notebook_exists,
            "analysis_present": self.analysis_present,
            "findings": dict(self.findings),
            "cached_values": self.cached_values,
            "operation_count": self.operation_count,
            "sheet_count": self.sheet_count,
            "plan_state": self.plan_state,
            "plan_version": self.plan_version,
            "approved_version": self.approved_version,
            "convertible": self.convertible,
            "blockers": list(self.blockers),
            "reconciliation": self.reconciliation,
            "reconciled_at": self.reconciled_at,
            "report_available": self.report_available,
            "marimo": {
                "live": self.marimo_live,
                "base_url": self.marimo_base_url,
                "port": self.marimo_port,
            },
        }


def describe(
    entry: RegistryEntry,
    *,
    user_directory: Path | None = None,
    live_markers: dict[str, tuple[str, int]] | None = None,
) -> WorkbookStatus:
    """Derive everything the hub shows about one workbook, from disk.

    Never raises. A workbook that has been deleted, a project directory somebody removed, a
    half-written ``analysis.json`` and an unparseable plan all degrade to an absent field.

    A deleted workbook is reported twice over, and deliberately: :attr:`WorkbookStatus.exists`
    says whether the file is there, :attr:`WorkbookStatus.source_state` says whether its absence
    was asked for. Reading the first alone is how "the notebook is the process now" came to be
    rendered as breakage.

    Args:
        entry: The recorded entry.
        user_directory: Overrides ``~/.kedge``, for tests.
        live_markers: Pre-computed live marimo servers keyed by normalised workbook path, from
            :func:`live_marimo_servers`. Passed in so listing N workbooks makes one health
            sweep rather than N.

    Returns:
        The entry with its derived state.
    """
    path = Path(entry.path)
    exists = path.is_file()
    changed = False
    if exists:
        try:
            stat = path.stat()
            changed = bool(entry.size_bytes) and (
                stat.st_size != entry.size_bytes or stat.st_mtime_ns != entry.mtime_ns
            )
        except OSError:  # pragma: no cover - raced with a delete
            exists = False

    try:
        workspace = Workspace.for_workbook(path, user_directory=user_directory)
    except KedgeError as exc:
        logger.warning("could not build a workspace for %s: %s", entry.path, exc)
        return WorkbookStatus(
            entry=entry,
            exists=exists,
            changed_on_disk=changed,
            source_state=_source_state(entry, exists=exists),
        )

    analysis = _read_analysis(workspace.analysis_path)
    plan_state, plan_version, approved_version, convertible, blockers = _read_plans(
        workspace.plans_dir
    )
    reconciliation, reconciled_at = _read_reconciliation(workspace.project_dir)
    marimo = (live_markers or {}).get(_normalise(path))

    return WorkbookStatus(
        entry=entry,
        exists=exists,
        changed_on_disk=changed,
        source_state=_source_state(entry, exists=exists),
        project_dir=str(workspace.project_dir),
        notebook_path=str(workspace.notebook_path),
        notebook_exists=workspace.notebook_path.is_file(),
        analysis_present=analysis is not None,
        findings=_count_findings(analysis),
        cached_values=_cached_value_status(analysis),
        operation_count=len(analysis.get("operations") or []) if analysis else 0,
        sheet_count=len(analysis.get("sheets") or []) if analysis else 0,
        plan_state=plan_state,
        plan_version=plan_version,
        approved_version=approved_version,
        convertible=convertible,
        blockers=blockers,
        reconciliation=reconciliation,
        reconciled_at=reconciled_at,
        report_available=report_path_for(workspace).is_file(),
        marimo_live=marimo is not None,
        marimo_base_url=None if marimo is None else marimo[0],
        marimo_port=None if marimo is None else marimo[1],
    )


def _source_state(entry: RegistryEntry, *, exists: bool) -> SourceState:
    """Which of the three readings of the workbook applies.

    A recorded release outranks the filesystem. A spreadsheet back at that path is a restore, a
    different file, or a release whose delete did not finish — none of which is "kedge never
    retired this", and all of which read better as a release with the file still present than as
    a live link. ``exists`` sits beside this on the status so the hub can say both.
    """
    if entry.released:
        return SourceState.RELEASED
    return SourceState.LINKED if exists else SourceState.MISSING


REPORT_FILENAME = "report.html"
RECONCILIATION_FILENAME = "reconciliation.json"


def report_path_for(workspace: Workspace) -> Path:
    """Where the hub looks for, and writes, this workbook's HTML analysis report."""
    return workspace.project_dir / REPORT_FILENAME


def reconciliation_path_for(workspace: Workspace) -> Path:
    """Where the last reconciliation report is cached for the hub to read."""
    return workspace.project_dir / RECONCILIATION_FILENAME


def _normalise(path: Path) -> str:
    """A comparable form of a path, case-folded because Windows paths are case-insensitive."""
    try:
        resolved = path.expanduser().resolve()
    except OSError:  # pragma: no cover - unresolvable path
        resolved = path
    return str(resolved).casefold()


def live_marimo_servers(
    user_directory: Path | None = None,
    *,
    probe: Any = None,
) -> dict[str, tuple[str, int]]:
    """Return the kedge-owned marimo servers that are actually answering, keyed by workbook.

    Only marker files kedge itself wrote are consulted, and each is confirmed over HTTP before
    being reported (PLAN 2.9: kedge owns its own process and never auto-discovers someone
    else's). A marker whose server is silent is simply absent from the result — clearing it is
    ``cleanup_orphan``'s job at the next open, not the hub's on a page load.

    Args:
        user_directory: Overrides ``~/.kedge``, for tests.
        probe: The liveness check, defaulting to :func:`kedge.lifecycle.health_check`.

    Returns:
        Normalised workbook path -> (base url, port).
    """
    if probe is None:
        from kedge.lifecycle import health_check

        probe = health_check

    live: dict[str, tuple[str, int]] = {}
    for _path, marker in iter_markers(user_directory):
        try:
            if not probe(marker.base_url):
                continue
        except Exception:  # pragma: no cover - a probe must never break the hub
            logger.exception("health probe failed for %s", marker.base_url)
            continue
        live[_normalise(Path(marker.workbook))] = (marker.base_url, marker.port)
    return live


def _read_analysis(path: Path) -> dict[str, Any] | None:
    """Read ``analysis.json`` as plain JSON.

    Deliberately not validated through :class:`~kedge.analysis.model.WorkbookAnalysis`. The hub
    wants four numbers off it, and an analysis written by an older schema should still yield a
    findings count rather than a validation error on the landing page.
    """
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("ignoring unreadable analysis %s: %s", path, exc)
        return None
    return raw if isinstance(raw, dict) else None


def _count_findings(analysis: dict[str, Any] | None) -> dict[str, int]:
    counts = {"total": 0, "error": 0, "warning": 0, "info": 0}
    if analysis is None:
        return counts
    findings = analysis.get("findings")
    if not isinstance(findings, list):
        return counts
    counts["total"] = len(findings)
    for finding in findings:
        severity = finding.get("severity") if isinstance(finding, dict) else None
        if severity in counts:
            counts[str(severity)] += 1
    return counts


def _cached_value_status(analysis: dict[str, Any] | None) -> str:
    if analysis is None:
        return "unknown"
    cached = analysis.get("cached_values")
    if isinstance(cached, dict) and cached.get("status"):
        return str(cached["status"])
    return "unknown"


def _read_plans(
    plans_dir: Path,
) -> tuple[str, int | None, int | None, float | None, tuple[str, ...]]:
    """Return (state, latest version, approved version, convertibility, blockers).

    Read straight off the YAML rather than through :class:`~kedge.plan.store.PlanStore`, for the
    same reason the analysis is: a hand-edited plan with a typo should show as "unreadable" in
    one row of the hub, not take the page down.
    """
    if not plans_dir.is_dir():
        return "none", None, None, None, ()

    versions: list[tuple[int, dict[str, Any]]] = []
    for path in sorted(plans_dir.glob("plan-v*.y*ml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("ignoring unreadable plan %s: %s", path, exc)
            continue
        if not isinstance(raw, dict):
            continue
        try:
            versions.append((int(raw.get("version") or 0), raw))
        except (TypeError, ValueError):
            continue

    if not versions:
        return "none", None, None, None, ()

    versions.sort(key=lambda item: item[0])
    latest_version, latest = versions[-1]
    approved = [version for version, raw in versions if _approval_state(raw) == "approved"]
    assessment = latest.get("assessment")
    convertible = None
    blockers: tuple[str, ...] = ()
    if isinstance(assessment, dict):
        try:
            convertible = float(assessment["convertible"])
        except (KeyError, TypeError, ValueError):
            convertible = None
        raw_blockers = assessment.get("blockers")
        if isinstance(raw_blockers, list):
            blockers = tuple(str(item) for item in raw_blockers)
    return (
        _approval_state(latest),
        latest_version,
        approved[-1] if approved else None,
        convertible,
        blockers,
    )


def _approval_state(raw: dict[str, Any]) -> str:
    approval = raw.get("approval")
    if isinstance(approval, dict) and approval.get("state"):
        return str(approval["state"])
    return "draft"


def _read_reconciliation(project_dir: Path) -> tuple[str | None, str | None]:
    """Return the last cached reconciliation verdict and when it was taken."""
    path = project_dir / RECONCILIATION_FILENAME
    if not path.is_file():
        return None, None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("ignoring unreadable reconciliation %s: %s", path, exc)
        return None, None
    if not isinstance(raw, dict):
        return None, None
    return raw.get("status"), raw.get("generated_at")


# ── the registry ─────────────────────────────────────────────────────────────────────────────


class WorkbookRegistry:
    """The list of workbooks kedge has been pointed at, newest use first.

    Example:
        >>> import tempfile
        >>> with tempfile.TemporaryDirectory() as home:
        ...     registry = WorkbookRegistry(Path(home) / "registry.json")
        ...     registry.entries()
        []
    """

    def __init__(self, path: Path, *, user_directory: Path | None = None) -> None:
        self._path = path
        self._user_directory = user_directory or path.parent

    @classmethod
    def for_user(cls, user_directory: Path | None = None) -> WorkbookRegistry:
        """Build the registry for ``~/.kedge`` (or an override, for tests)."""
        directory = (user_directory or user_dir()).expanduser()
        return cls(directory / REGISTRY_FILENAME, user_directory=directory)

    @property
    def path(self) -> Path:
        """The file this registry is persisted to."""
        return self._path

    # ── reading ──────────────────────────────────────────────────────────────────────────

    def entries(self) -> list[RegistryEntry]:
        """Every recorded workbook, most recently used first.

        An absent, unreadable or malformed file yields an empty list rather than raising: the
        landing page of a tool must open.
        """
        if not self._path.is_file():
            return []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("ignoring unreadable registry %s: %s", self._path, exc)
            return []
        if not isinstance(raw, dict):
            logger.warning("ignoring registry %s: expected a JSON object", self._path)
            return []
        rows = raw.get("workbooks")
        if not isinstance(rows, list):
            return []
        entries = [
            entry
            for entry in (RegistryEntry.from_dict(row) for row in rows if isinstance(row, dict))
            if entry is not None
        ]
        return sorted(entries, key=_recency, reverse=True)

    def get(self, key: str) -> RegistryEntry | None:
        """Return the entry with this key, or ``None``."""
        return next((entry for entry in self.entries() if entry.key == key), None)

    def statuses(self, *, probe: Any = None) -> list[WorkbookStatus]:
        """Every entry with its derived state, most recently used first."""
        live = live_marimo_servers(self._user_directory, probe=probe)
        return [
            describe(entry, user_directory=self._user_directory, live_markers=live)
            for entry in self.entries()
        ]

    def status(self, key: str, *, probe: Any = None) -> WorkbookStatus | None:
        """The derived state of one entry, or ``None`` if it is not registered."""
        entry = self.get(key)
        if entry is None:
            return None
        live = live_marimo_servers(self._user_directory, probe=probe)
        return describe(entry, user_directory=self._user_directory, live_markers=live)

    # ── writing ──────────────────────────────────────────────────────────────────────────

    def add(self, workbook: Path | str) -> RegistryEntry:
        """Register a workbook after validating that it really is one.

        Idempotent by resolved path: adding a workbook already registered refreshes its hash and
        size rather than creating a second row, so a user who re-adds a file after editing it in
        Excel sees one entry with current facts.

        This is also how a release is undone. Adding a workbook means a readable file was found
        and validated at that path, which is the opposite of "retired on purpose" whatever the
        row said a moment ago, so ``released_at`` is cleared. There is no separate un-release
        verb, because there is nothing to un-release without the spreadsheet back.

        Args:
            workbook: The candidate workbook.

        Returns:
            The stored entry.

        Raises:
            RegistryError: The file is not a readable ``.xlsx``/``.xlsm``, or the registry could
                not be written.
        """
        resolved = validate_workbook(Path(workbook))
        key = Workspace.for_workbook(resolved, user_directory=self._user_directory).key
        stat = resolved.stat()
        now = _now()

        entries = self.entries()
        existing = next((entry for entry in entries if entry.key == key), None)
        entry = RegistryEntry(
            key=key,
            path=str(resolved),
            name=resolved.name,
            sha256=_sha256(resolved),
            size_bytes=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            added_at=existing.added_at if existing else now,
            last_opened_at=existing.last_opened_at if existing else None,
            open_count=existing.open_count if existing else 0,
            released_at=None,
        )
        self._write([entry, *(item for item in entries if item.key != key)])
        logger.info("registered workbook %s", resolved)
        if existing is not None and existing.released:
            logger.info(
                "workbook %s is back on disk, so it is linked again rather than released", resolved
            )
        return entry

    def release(self, key: str) -> RegistryEntry | None:
        """Record that this workbook has been retired on purpose, and return the entry.

        A release is the successful end of a conversion, not a failure: the notebook has become
        the process and the spreadsheet is obsolete. Recording it is what lets the hub tell a
        deliberate retirement from a file somebody moved by accident, which are otherwise the
        same absence and were, until this existed, both reported as breakage.

        Nothing on disk is touched. Removing the workbook is
        :func:`kedge.purge.plan_release` and :func:`kedge.purge.execute`, and the caller runs
        those *first* and only marks the row on a clean result — the same ordering the delete
        path uses, and for the same reason. A row marked released whose spreadsheet is still
        there is a half-finished release the hub can show as one; a deleted workbook with no row
        marked is indistinguishable from an accident.

        Idempotent. Releasing an entry that is already released keeps the original timestamp,
        for the same reason :meth:`add` keeps the original ``added_at``: it is history.

        Args:
            key: The workspace key of the entry to release.

        Returns:
            The stored entry, or ``None`` if no entry has that key.
        """
        entries = self.entries()
        existing = next((entry for entry in entries if entry.key == key), None)
        if existing is None:
            return None
        if existing.released:
            return existing
        updated = replace(existing, released_at=_now())
        self._write([updated, *(item for item in entries if item.key != key)])
        logger.info("released workbook %s; the notebook is the process now", existing.path)
        return updated

    def register_released(self, workbook: Path | str) -> RegistryEntry:
        """Register, or keep, an entry whose workbook is deliberately gone.

        :meth:`add` validates that the path really is a readable OOXML workbook, which is right
        for a new conversion and wrong here: the subject of a released process is the notebook,
        and the spreadsheet it came from was retired on purpose. A separate verb rather than a
        flag on :meth:`add`, because registering a file that is not there is a different act with
        a different failure mode, and a boolean at a call site does not say which of the two was
        meant.

        The *shape* of the path is still checked, by :func:`_resolve_workbook_path`. The key, the
        project directory and the notebook name are all derived from it, so this has to be the
        path the workbook had — hand in the notebook and you address a workspace nothing else
        agrees with. Sharp edge worth knowing: on Windows a resolved path picks up the on-disk
        casing of the components that still exist, so the reliable caller passes the ``path`` off
        an existing row rather than retyping it.

        History is kept where a row is already there. The last digest and size of the spreadsheet
        are evidence — the acceptance record cites a digest — and outlive the file itself.

        Args:
            workbook: Where the workbook was, whether or not anything is there now.

        Returns:
            The stored entry, marked released.

        Raises:
            RegistryError: The path is not shaped like a workbook, or the registry could not be
                written.
        """
        resolved = _resolve_workbook_path(Path(workbook))
        key = Workspace.for_workbook(resolved, user_directory=self._user_directory).key
        now = _now()

        entries = self.entries()
        existing = next((entry for entry in entries if entry.key == key), None)
        entry = RegistryEntry(
            key=key,
            path=str(resolved),
            name=resolved.name,
            sha256=existing.sha256 if existing else "",
            size_bytes=existing.size_bytes if existing else 0,
            mtime_ns=existing.mtime_ns if existing else 0,
            added_at=existing.added_at if existing else now,
            last_opened_at=existing.last_opened_at if existing else None,
            open_count=existing.open_count if existing else 0,
            released_at=existing.released_at if existing and existing.released else now,
        )
        self._write([entry, *(item for item in entries if item.key != key)])
        logger.info("registered released workbook %s", resolved)
        return entry

    def record_open(self, key: str) -> RegistryEntry | None:
        """Stamp an entry as opened now, returning it. ``None`` if it is not registered."""
        entries = self.entries()
        existing = next((entry for entry in entries if entry.key == key), None)
        if existing is None:
            return None
        updated = replace(existing, last_opened_at=_now(), open_count=existing.open_count + 1)
        self._write([updated, *(item for item in entries if item.key != key)])
        return updated

    def forget(self, key: str) -> bool:
        """Remove an entry. Returns whether one was there to remove.

        Removes the row only -- and do not read that as the product's promise, because it stopped
        being one. ``server/hub.py``'s ``forget_workbook`` runs :func:`kedge.purge.execute`
        first and calls this last, so Forget on the hub takes the workbook and everything kedge
        derived from it as well. Two docstrings giving opposite accounts of the same button is
        worse than either being wrong on its own, and this is the one that was stale.

        The split is deliberate rather than left over. The row is the registry's business and the
        files are :mod:`kedge.purge`'s, and keeping them apart is exactly what lets a deletion
        that fails halfway leave the card in the list, where the user can see it and try again.
        A version of this method that deleted as well would have nowhere to put that failure.
        """
        entries = self.entries()
        remaining = [entry for entry in entries if entry.key != key]
        if len(remaining) == len(entries):
            return False
        self._write(remaining)
        logger.info("forgot workbook %s", key)
        return True

    def _write(self, entries: list[RegistryEntry]) -> None:
        payload = {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "workbooks": [entry.to_dict() for entry in entries],
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            temporary.replace(self._path)
        except OSError as exc:
            msg = f"could not write the workbook registry {self._path}: {exc}"
            raise RegistryError(msg) from exc


def _recency(entry: RegistryEntry) -> str:
    """Sort key: last opened, falling back to when it was added."""
    return entry.last_opened_at or entry.added_at or ""


def _now() -> str:
    return datetime.now(UTC).isoformat()
