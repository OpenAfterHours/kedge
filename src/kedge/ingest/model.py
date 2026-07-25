"""The records that describe a hand-in, its shape, and how that shape has moved.

Three families of record live here, and the first of them is the reason the whole package
exists.

:class:`HandIn` is the convergence point. marimo offers two native ways to nominate a file
and they return different things: ``mo.ui.file(kind="area")`` yields
``FileUploadResults(name, contents)`` -- bytes held in kernel memory with no path at all --
while ``mo.ui.file_browser(...)`` yields ``FileBrowserFileInfo(id, path, name,
is_directory)`` -- a path with no bytes. An uploaded file is **not reproducible**: reopen
the notebook tomorrow and the bytes are gone, the cell yields nothing, and every downstream
cell in the dataflow graph dies with it. So both entry points, and the watched folder that
makes the process run unattended, normalise into one ``HandIn`` whose ``path`` is always the
managed copy and never the transient upload. Downstream notebook code is then identical
regardless of how the file arrived, and the recorded hash is what makes the audit line
defensible: *this run consumed this file* (PLAN 2.8).

:class:`HandInProfile` is the shape of one hand-in, expressed in the same
:class:`~kedge.analysis.model.ColumnProfile` the analyser uses, so the agent sees hand-in
columns and workbook columns in one vocabulary.

:class:`DriftReport` is the difference between two profiles. It is reported *before* the
contract check, because "column ``EAD`` became ``EAD_GBP``" is a far more useful message
than a schema-validation traceback.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from kedge.analysis.model import ColumnProfile, Severity

logger = logging.getLogger(__name__)

__all__ = [
    "DriftItem",
    "DriftKind",
    "DriftReport",
    "HandIn",
    "HandInProfile",
    "HandInSource",
    "Upload",
    "sort_drift",
    "utcnow",
]

HandInSource = Literal["dropped", "selected", "watched"]
"""How a hand-in reached the managed store.

``dropped`` came through ``mo.ui.file`` as bytes, ``selected`` through
``mo.ui.file_browser`` as a path, ``watched`` from a folder kedge is monitoring. The record
that results is the same in all three cases; only this label differs.
"""

_SEVERITY_RANK: dict[Severity, int] = {
    Severity.ERROR: 0,
    Severity.WARNING: 1,
    Severity.INFO: 2,
}


# =============================================================================
# INPUT SHAPES
# =============================================================================


@dataclass(frozen=True, slots=True)
class Upload:
    """A dropped file's name and bytes.

    Structurally identical to marimo's ``FileUploadResults``, and accepted by
    :func:`kedge.ingest.receive` in exactly the same way. It exists so that callers and
    tests can express a drag-and-drop payload without importing marimo, and so the shape
    kedge depends on is written down in kedge rather than inferred from a private detail of
    somebody else's widget.

    Example:
        >>> handin = receive(Upload(name="exposures.csv", contents=b"a,b\\n1,2\\n"), store=root)
        >>> handin.source
        'dropped'
    """

    name: str
    contents: bytes

    def __repr__(self) -> str:
        # Contents can be a hundred megabytes; never render them into a traceback.
        return f"Upload(name={self.name!r}, contents=<{len(self.contents)} bytes>)"


# =============================================================================
# THE HAND-IN RECORD
# =============================================================================


@dataclass(frozen=True, slots=True)
class HandIn:
    """One file accepted into the managed store.

    ``path`` is always inside the managed store. That is the invariant the record exists to
    hold: a dropped file's bytes have no path of their own, and a selected file's path is
    typically on a shared drive that is not a stable artifact, so neither is safe to hand
    downstream. Notebook cells read ``handin.path`` and are indifferent to which entry point
    produced it.

    Example:
        >>> handin.audit_line()
        'selected 2026-07-25T08:14:22+00:00 sha256:4f2b91c8ad03 exposures.xlsx (18,204 bytes)'
    """

    path: Path
    original_name: str
    sha256: str
    size_bytes: int
    received_at: datetime
    source: HandInSource
    contract: str | None = None

    @property
    def short_hash(self) -> str:
        """The first twelve hex characters of the digest, as used in managed filenames."""
        return self.sha256[:12]

    @property
    def suffix(self) -> str:
        """The lowercased file extension, e.g. ``'.xlsx'``."""
        return Path(self.original_name).suffix.lower()

    def exists(self) -> bool:
        """Whether the managed copy is still on disk."""
        return self.path.is_file()

    def with_contract(self, contract: str | None) -> HandIn:
        """Return a copy of this record bound to a contract name."""
        return replace(self, contract=contract)

    def audit_line(self) -> str:
        """Return a one-line, human-readable statement of what this run consumed."""
        return (
            f"{self.source} {self.received_at.isoformat()} sha256:{self.short_hash} "
            f"{self.original_name} ({self.size_bytes:,} bytes)"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable form written to the store index."""
        return {
            "path": str(self.path),
            "original_name": self.original_name,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "received_at": self.received_at.isoformat(),
            "source": self.source,
            "contract": self.contract,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> HandIn:
        """Rebuild a record from its stored form.

        Raises:
            ValueError: If a required field is absent or will not convert. Callers treat
                that as "unusable index line" rather than as fatal -- a corrupt index must
                never be able to stop a hand-in being received.
        """
        try:
            return cls(
                path=Path(str(raw["path"])),
                original_name=str(raw["original_name"]),
                sha256=str(raw["sha256"]),
                size_bytes=int(raw["size_bytes"]),
                received_at=datetime.fromisoformat(str(raw["received_at"])),
                source=_coerce_source(raw.get("source")),
                contract=None if raw.get("contract") is None else str(raw["contract"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            msg = f"hand-in record is missing or has a malformed field: {exc}"
            raise ValueError(msg) from exc


def _coerce_source(value: object) -> HandInSource:
    text = str(value)
    if text not in ("dropped", "selected", "watched"):
        msg = f"unknown hand-in source {value!r}"
        raise ValueError(msg)
    return text  # type: ignore[return-value]


# =============================================================================
# SHAPE
# =============================================================================


@dataclass(frozen=True, slots=True)
class HandInProfile:
    """The shape of one hand-in: its columns, its row count, and its layout quirks.

    Columns are :class:`~kedge.analysis.model.ColumnProfile` values, deliberately the same
    type the workbook analyser produces, so the agent reasons about hand-in columns and
    workbook columns in one vocabulary (PLAN 2.8, "profile on ingest").

    ``row_count`` excludes a detected totals row. A totals row is a layout artifact rather
    than data, and leaving it in would poison every sum and maximum in the profile -- which
    would then show up next month as several columns' worth of statistical drift instead of
    the one true finding, ``totals_row_appended``.
    """

    sha256: str
    original_name: str
    profiled_at: datetime
    sheet: str
    row_count: int
    column_count: int
    header_row: int
    preamble_rows: int
    has_totals_row: bool
    columns: tuple[ColumnProfile, ...] = ()

    @property
    def column_names(self) -> tuple[str, ...]:
        """Header names in file order, falling back to the column letter where absent."""
        return tuple(c.header or c.column for c in self.columns)

    def column(self, name: str) -> ColumnProfile | None:
        """Return the profile for a named column, or ``None``."""
        return next((c for c in self.columns if (c.header or c.column) == name), None)

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable form persisted as the last accepted shape."""
        return {
            "sha256": self.sha256,
            "original_name": self.original_name,
            "profiled_at": self.profiled_at.isoformat(),
            "sheet": self.sheet,
            "row_count": self.row_count,
            "column_count": self.column_count,
            "header_row": self.header_row,
            "preamble_rows": self.preamble_rows,
            "has_totals_row": self.has_totals_row,
            "columns": [c.model_dump(mode="json") for c in self.columns],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> HandInProfile:
        """Rebuild a profile from its stored form.

        Raises:
            ValueError: If a field is absent or will not convert.
        """
        try:
            return cls(
                sha256=str(raw["sha256"]),
                original_name=str(raw["original_name"]),
                profiled_at=datetime.fromisoformat(str(raw["profiled_at"])),
                sheet=str(raw["sheet"]),
                row_count=int(raw["row_count"]),
                column_count=int(raw["column_count"]),
                header_row=int(raw["header_row"]),
                preamble_rows=int(raw["preamble_rows"]),
                has_totals_row=bool(raw["has_totals_row"]),
                columns=tuple(ColumnProfile.model_validate(c) for c in raw.get("columns", [])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            msg = f"stored hand-in profile is malformed: {exc}"
            raise ValueError(msg) from exc


# =============================================================================
# DRIFT
# =============================================================================


class DriftKind(StrEnum):
    """The ways a hand-in's shape moves between deliveries.

    Every member here was chosen because it is a thing another team does without telling
    anyone, and because the Excel process it replaces would have absorbed it silently and
    produced a quietly wrong number.
    """

    COLUMN_ADDED = "column_added"
    COLUMN_REMOVED = "column_removed"
    COLUMN_RENAMED = "column_renamed"
    COLUMN_REORDERED = "column_reordered"
    DTYPE_CHANGED = "dtype_changed"
    NUMERIC_TO_TEXT = "numeric_to_text"
    ROW_COUNT_SHIFT = "row_count_shift"
    NULL_RATE_SHIFT = "null_rate_shift"
    PREAMBLE_ROWS_CHANGED = "preamble_rows_changed"
    TOTALS_ROW_APPENDED = "totals_row_appended"
    TOTALS_ROW_REMOVED = "totals_row_removed"
    SHEET_CHANGED = "sheet_changed"


@dataclass(frozen=True, slots=True)
class DriftItem:
    """One difference between the last accepted hand-in and this one."""

    kind: DriftKind
    severity: Severity
    message: str
    column: str | None = None
    before: str | None = None
    after: str | None = None
    remediation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the plain-data form, suitable for a table widget or JSON."""
        return {
            "kind": self.kind.value,
            "severity": self.severity.value,
            "column": self.column,
            "before": self.before,
            "after": self.after,
            "message": self.message,
            "remediation": self.remediation,
        }


@dataclass(frozen=True, slots=True)
class DriftReport:
    """What changed between the last accepted hand-in and this one.

    Rendered before contract validation runs, so the user reads "column ``EAD`` became
    ``EAD_GBP``" rather than a dtype mismatch three checks deep in a schema report.

    Example:
        >>> report = compare(baseline, current)
        >>> report.ok
        False
        >>> print(report.render_text())
    """

    current_sha256: str
    baseline_sha256: str | None = None
    items: tuple[DriftItem, ...] = ()

    @property
    def ok(self) -> bool:
        """True when nothing changed at all."""
        return not self.items

    @property
    def is_first_hand_in(self) -> bool:
        """True when there was no accepted hand-in to compare against."""
        return self.baseline_sha256 is None

    @property
    def errors(self) -> tuple[DriftItem, ...]:
        """Drift severe enough that downstream code is probably already wrong."""
        return tuple(i for i in self.items if i.severity is Severity.ERROR)

    @property
    def warnings(self) -> tuple[DriftItem, ...]:
        """Drift worth a look before the numbers are trusted."""
        return tuple(i for i in self.items if i.severity is Severity.WARNING)

    def of_kind(self, *kinds: DriftKind) -> tuple[DriftItem, ...]:
        """Return the items matching any of the given kinds."""
        wanted = set(kinds)
        return tuple(i for i in self.items if i.kind in wanted)

    def to_rows(self) -> list[dict[str, Any]]:
        """Return one plain dict per item, ready for ``mo.ui.table`` or a rich table."""
        return [i.to_dict() for i in self.items]

    def to_dict(self) -> dict[str, Any]:
        """Return the whole report as plain data."""
        return {
            "current_sha256": self.current_sha256,
            "baseline_sha256": self.baseline_sha256,
            "ok": self.ok,
            "items": self.to_rows(),
        }

    def summary_line(self) -> str:
        """Return a single sentence stating the outcome."""
        if self.is_first_hand_in:
            return "no previously accepted hand-in to compare against - nothing to drift from"
        if self.ok:
            return "no shape drift against the last accepted hand-in"
        return (
            f"{len(self.items)} shape difference(s) against the last accepted hand-in: "
            f"{len(self.errors)} error, {len(self.warnings)} warning"
        )

    def render_text(self) -> str:
        """Return a plain-text rendering for CLI output and notebook markdown."""
        lines = [self.summary_line()]
        for item in self.items:
            where = f" [{item.column}]" if item.column else ""
            lines.append(f"  {item.severity.value.upper():<7}{where} {item.message}")
            if item.remediation:
                lines.append(f"          -> {item.remediation}")
        return "\n".join(lines)


def sort_drift(items: list[DriftItem]) -> tuple[DriftItem, ...]:
    """Order drift items so the dangerous ones are read first.

    Severity leads, then kind, then column name. A numeric column arriving as text is the
    cause of the silent join failure in PLAN 2.6, so it is an error and sorts to the top.

    Args:
        items: The unordered findings.

    Returns:
        The same items as an ordered tuple.
    """
    return tuple(
        sorted(items, key=lambda i: (_SEVERITY_RANK[i.severity], i.kind.value, i.column or ""))
    )


def utcnow() -> datetime:
    """Return the current time in UTC.

    Every timestamp kedge records is UTC, including the date used to partition the managed
    store, so an audit trail reads the same wherever it is opened.
    """
    return datetime.now(UTC)
