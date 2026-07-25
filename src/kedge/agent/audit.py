"""The outbound payload log: what left the machine, never what it said.

PLAN 2.3 asks for one line per value-returning tool call in
``~/.kedge/logs/outbound-<session>.jsonl``, carrying timestamp, tool, sheet, columns, row count
and byte count — and explicitly *not* the values themselves. It is cheap to build now and it is
the first thing anyone asks for the moment this gets pointed at something real.

"Not the values" is enforced structurally rather than by remembering. :class:`OutboundRecord` has
a fixed set of scalar fields, every one of which is a *name* or a *count*; there is no field a
cell value could travel in, and :meth:`OutboundRecord.to_dict` enumerates them explicitly rather
than dumping ``__dict__``. A tool that wanted to log a value would have to change this module,
which is exactly the review that should be required.

Column names are the one judgement call. They are metadata rather than data, and a log that
cannot say *which* columns went out answers none of the questions it exists to answer — so they
travel, capped in count and length.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_LOGGED_COLUMNS",
    "OutboundLog",
    "OutboundRecord",
    "outbound_log_for",
]

MAX_LOGGED_COLUMNS = 64
"""Ceiling on the column names recorded per line. A wider result logs a count instead."""

MAX_COLUMN_NAME_CHARS = 64
"""Longest column name recorded verbatim. Longer names are truncated, never expanded."""


@dataclass(frozen=True, slots=True)
class OutboundRecord:
    """One value-returning tool call, described by shape rather than by content.

    Example:
        >>> record = OutboundRecord(
        ...     timestamp="2026-07-25T09:00:00+00:00",
        ...     session="s1",
        ...     turn_id="t1",
        ...     tool="sample_data",
        ...     ok=True,
        ...     sheet="Calc",
        ...     columns=("exposure", "haircut"),
        ...     row_count=100,
        ...     byte_count=4096,
        ... )
        >>> record.to_dict()["tool"]
        'sample_data'
    """

    timestamp: str
    session: str
    turn_id: str | None
    tool: str
    ok: bool
    sheet: str | None
    columns: tuple[str, ...]
    row_count: int
    byte_count: int
    truncated: bool = False
    omitted_rows: int = 0
    redacted_columns: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON object written to the log.

        Every key is listed here by hand. That is the point: adding a field is a deliberate
        edit to this module, not something that happens by accident when a caller passes an
        extra keyword.
        """
        columns = [_clip(name) for name in self.columns[:MAX_LOGGED_COLUMNS]]
        return {
            "timestamp": self.timestamp,
            "session": self.session,
            "turn_id": self.turn_id,
            "tool": self.tool,
            "ok": self.ok,
            "sheet": self.sheet,
            "columns": columns,
            "column_count": len(self.columns),
            "row_count": self.row_count,
            "byte_count": self.byte_count,
            "truncated": self.truncated,
            "omitted_rows": self.omitted_rows,
            "redacted_columns": self.redacted_columns,
        }


def _clip(name: str) -> str:
    text = " ".join(str(name).split())
    if len(text) <= MAX_COLUMN_NAME_CHARS:
        return text
    return text[: MAX_COLUMN_NAME_CHARS - 1] + "…"


class OutboundLog:
    """Append-only JSONL record of every payload a tool returned to the model.

    One instance per chat session, holding one path. Writing is guarded by a lock because the
    server may have more than one turn in flight against the same session, and a half-written
    line in an audit log is worse than no line at all.

    A failed write is logged and swallowed. The audit trail is important, but a full disk must
    degrade the trail rather than take down the conversation that was producing it.

    Example:
        >>> import tempfile
        >>> from pathlib import Path
        >>> with tempfile.TemporaryDirectory() as directory:
        ...     log = OutboundLog(Path(directory) / "outbound-s1.jsonl", session="s1")
        ...     _ = log.record(tool="read_range", sheet="Ref", columns=("A",), row_count=3,
        ...                    byte_count=64)
        ...     len(list(log.entries()))
        1
    """

    def __init__(self, path: Path, *, session: str) -> None:
        self._path = Path(path)
        self._session = session
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        """Where the log is written."""
        return self._path

    @property
    def session(self) -> str:
        """The chat session this log belongs to."""
        return self._session

    def record(
        self,
        *,
        tool: str,
        row_count: int,
        byte_count: int,
        columns: tuple[str, ...] = (),
        sheet: str | None = None,
        ok: bool = True,
        truncated: bool = False,
        omitted_rows: int = 0,
        redacted_columns: int = 0,
        turn_id: str | None = None,
    ) -> OutboundRecord:
        """Append one line describing a payload, and return the record written."""
        record = OutboundRecord(
            timestamp=datetime.now(UTC).isoformat(),
            session=self._session,
            turn_id=turn_id,
            tool=tool,
            ok=ok,
            sheet=sheet,
            columns=tuple(columns),
            row_count=row_count,
            byte_count=byte_count,
            truncated=truncated,
            omitted_rows=omitted_rows,
            redacted_columns=redacted_columns,
        )
        self._append(record)
        return record

    def _append(self, record: OutboundRecord) -> None:
        line = json.dumps(record.to_dict(), ensure_ascii=False, separators=(",", ":"))
        try:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except OSError as exc:
            logger.warning("could not write the outbound audit log %s: %s", self._path, exc)

    def entries(self) -> Iterator[dict[str, Any]]:
        """Yield every record written so far, for ``kedge doctor`` and for tests."""
        if not self._path.is_file():
            return
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("could not read the outbound audit log %s: %s", self._path, exc)
            return
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                logger.warning("ignoring a malformed line in %s", self._path)

    def __repr__(self) -> str:
        return f"OutboundLog(path={self._path!s}, session={self._session!r})"


def outbound_log_for(workspace: Any, session: str) -> OutboundLog:
    """Build the log for one session from a :class:`~kedge.workspace.Workspace`.

    Typed loosely on purpose: the only thing this module needs from a workspace is one path,
    and importing the workspace for that would be a dependency nobody benefits from.
    """
    return OutboundLog(Path(workspace.outbound_log_path(session)), session=session)
