"""Chat sessions and messages, in SQLite.

PLAN M4 asks for "sessions in SQLite. Each row a message; each session references a notebook path
and a snapshot marker so it can be resumed coherently." That is what this is, and no more: plain
``sqlite3`` from the standard library, no ORM, no migration framework beyond ``PRAGMA
user_version``. A single-user local tool storing a few thousand chat messages does not need one,
and an ORM here would be the largest dependency in the project for the smallest reason.

The snapshot marker is what makes resumption honest. A session is a conversation *about a
particular notebook at a particular state*, and the user edits that notebook directly between
turns — so reopening a week-old session and carrying on as though nothing moved would be a
fiction. Each session records a digest of the notebook file at the time it was created;
:func:`notebook_snapshot` recomputes it on resume, and a mismatch is reported rather than
smoothed over.

Turn events are stored alongside the assistant message that produced them. Without that, a
reopened session shows the prose but loses the activity trail, and the trail is a good part of
what makes the conversation reviewable — "which cells did this turn create, and did validation
pass" is not answerable from the prose alone.

Access is guarded by a lock and the connection is opened with ``check_same_thread=False``,
because Starlette runs synchronous endpoints on a threadpool and every one of them may touch the
store.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

__all__ = [
    "SCHEMA_VERSION",
    "ChatMessage",
    "ChatSession",
    "MessageRole",
    "SessionStore",
    "notebook_snapshot",
]

SCHEMA_VERSION = 1
MessageRole = Literal["user", "assistant", "system", "tool"]

_TITLE_CHARS = 70
_ABSENT_SNAPSHOT = "absent"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT    PRIMARY KEY,
    title         TEXT    NOT NULL,
    workbook_path TEXT    NOT NULL,
    notebook_path TEXT    NOT NULL,
    snapshot      TEXT,
    model         TEXT,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT    NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    turn_id     TEXT,
    role        TEXT    NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
    content     TEXT    NOT NULL,
    events      TEXT,
    tokens_used INTEGER,
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS messages_by_session ON messages (session_id, id);
CREATE INDEX IF NOT EXISTS sessions_by_notebook ON sessions (notebook_path, updated_at DESC);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def notebook_snapshot(notebook_path: Path) -> str:
    """Return a marker for the current state of ``notebook_path``.

    A content digest rather than an mtime: ``--watch`` rewrites the file whenever the kernel
    changes a cell, so mtime moves for reasons that are not user edits, and comparing digests
    means an edit-and-undo correctly reads as "unchanged".

    Args:
        notebook_path: The notebook the session is about.

    Returns:
        ``sha256:<12 hex chars>``, or ``"absent"`` if the file does not exist or cannot be read.
    """
    try:
        data = notebook_path.read_bytes()
    except OSError:
        return _ABSENT_SNAPSHOT
    return f"sha256:{hashlib.sha256(data).hexdigest()[:12]}"


def _derive_title(text: str) -> str:
    """Return a session title taken from the first thing the user said."""
    collapsed = " ".join(text.split())
    if not collapsed:
        return "New chat"
    if len(collapsed) <= _TITLE_CHARS:
        return collapsed
    return collapsed[: _TITLE_CHARS - 1].rstrip() + "…"


@dataclass(frozen=True, slots=True)
class ChatSession:
    """One conversation, about one notebook, at one recorded state of that notebook."""

    id: str
    title: str
    workbook_path: str
    notebook_path: str
    snapshot: str | None
    model: str | None
    created_at: str
    updated_at: str
    message_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable form the UI consumes."""
        return {
            "id": self.id,
            "title": self.title,
            "workbook_path": self.workbook_path,
            "notebook_path": self.notebook_path,
            "snapshot": self.snapshot,
            "model": self.model,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message_count": self.message_count,
        }


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One message. Assistant messages also carry the turn's activity trail."""

    id: int
    session_id: str
    turn_id: str | None
    role: MessageRole
    content: str
    events: tuple[dict[str, Any], ...]
    tokens_used: int | None
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable form the UI consumes."""
        return {
            "id": self.id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "role": self.role,
            "content": self.content,
            "events": list(self.events),
            "tokens_used": self.tokens_used,
            "created_at": self.created_at,
        }


class SessionStore:
    """The SQLite-backed store of sessions and messages.

    Example:
        >>> store = SessionStore(Path(":memory:"))
        >>> session = store.create_session(workbook_path="a.xlsx", notebook_path="a.py")
        >>> _ = store.append_message(session.id, role="user", content="hello")
        >>> [message.content for message in store.messages(session.id)]
        ['hello']
        >>> store.close()
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        if str(path) != ":memory:":
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(str(path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._prepare()
        logger.debug("session store ready at %s", path)

    def _prepare(self) -> None:
        with self._lock, self._connection:
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.executescript(_SCHEMA)
            version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
            if version < SCHEMA_VERSION:
                self._connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        """Close the underlying connection. Safe to call more than once."""
        with self._lock:
            self._connection.close()

    # ── sessions ─────────────────────────────────────────────────────────────────────────

    def create_session(
        self,
        *,
        workbook_path: str,
        notebook_path: str,
        title: str | None = None,
        model: str | None = None,
        snapshot: str | None = None,
    ) -> ChatSession:
        """Create and return a new session."""
        now = _now()
        session = ChatSession(
            id=uuid.uuid4().hex[:16],
            title=title or "New chat",
            workbook_path=workbook_path,
            notebook_path=notebook_path,
            snapshot=snapshot,
            model=model,
            created_at=now,
            updated_at=now,
        )
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO sessions (id, title, workbook_path, notebook_path, snapshot, "
                "model, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session.id,
                    session.title,
                    session.workbook_path,
                    session.notebook_path,
                    session.snapshot,
                    session.model,
                    session.created_at,
                    session.updated_at,
                ),
            )
        logger.info("created chat session %s", session.id)
        return session

    def get_session(self, session_id: str) -> ChatSession | None:
        """Return one session, or ``None`` if there is no such id."""
        with self._lock:
            row = self._connection.execute(
                "SELECT s.*, (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) "
                "AS message_count FROM sessions s WHERE s.id = ?",
                (session_id,),
            ).fetchone()
        return None if row is None else _session_from_row(row)

    def list_sessions(
        self, *, notebook_path: str | None = None, limit: int = 50
    ) -> list[ChatSession]:
        """Return sessions newest first, optionally restricted to one notebook."""
        query = (
            "SELECT s.*, (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) "
            "AS message_count FROM sessions s"
        )
        parameters: list[Any] = []
        if notebook_path is not None:
            query += " WHERE s.notebook_path = ?"
            parameters.append(notebook_path)
        query += " ORDER BY s.updated_at DESC, s.rowid DESC LIMIT ?"
        parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [_session_from_row(row) for row in rows]

    def update_session(
        self,
        session_id: str,
        *,
        title: str | None = None,
        model: str | None = None,
        snapshot: str | None = None,
    ) -> ChatSession | None:
        """Apply whichever of ``title``, ``model`` and ``snapshot`` were supplied."""
        assignments: list[str] = []
        parameters: list[Any] = []
        for column, value in (("title", title), ("model", model), ("snapshot", snapshot)):
            if value is not None:
                assignments.append(f"{column} = ?")
                parameters.append(value)
        if not assignments:
            return self.get_session(session_id)
        assignments.append("updated_at = ?")
        parameters.extend([_now(), session_id])
        with self._lock, self._connection:
            self._connection.execute(
                f"UPDATE sessions SET {', '.join(assignments)} WHERE id = ?", parameters
            )
        return self.get_session(session_id)

    def delete_session(self, session_id: str) -> bool:
        """Delete a session and its messages. Returns whether there was one to delete."""
        with self._lock, self._connection:
            # Deleted explicitly rather than by cascade: foreign keys are a per-connection
            # pragma, and a store opened by something that forgot it would silently orphan rows.
            self._connection.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            cursor = self._connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        deleted = cursor.rowcount > 0
        if deleted:
            logger.info("deleted chat session %s", session_id)
        return deleted

    # ── messages ─────────────────────────────────────────────────────────────────────────

    def append_message(
        self,
        session_id: str,
        *,
        role: MessageRole,
        content: str,
        turn_id: str | None = None,
        events: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
        tokens_used: int | None = None,
    ) -> ChatMessage:
        """Append a message to a session, retitling it if this is the first thing said."""
        now = _now()
        payload = json.dumps(list(events)) if events else None
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "INSERT INTO messages (session_id, turn_id, role, content, events, tokens_used, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, turn_id, role, content, payload, tokens_used, now),
            )
            self._connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id)
            )
            if role == "user":
                self._connection.execute(
                    "UPDATE sessions SET title = ? WHERE id = ? AND title = 'New chat'",
                    (_derive_title(content), session_id),
                )
        return ChatMessage(
            id=int(cursor.lastrowid or 0),
            session_id=session_id,
            turn_id=turn_id,
            role=role,
            content=content,
            events=tuple(events),
            tokens_used=tokens_used,
            created_at=now,
        )

    def messages(self, session_id: str, *, limit: int | None = None) -> list[ChatMessage]:
        """Return a session's messages oldest first."""
        query = "SELECT * FROM messages WHERE session_id = ? ORDER BY id"
        parameters: list[Any] = [session_id]
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [_message_from_row(row) for row in rows]


def _session_from_row(row: sqlite3.Row) -> ChatSession:
    return ChatSession(
        id=row["id"],
        title=row["title"],
        workbook_path=row["workbook_path"],
        notebook_path=row["notebook_path"],
        snapshot=row["snapshot"],
        model=row["model"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        message_count=int(row["message_count"]) if "message_count" in row.keys() else 0,  # noqa: SIM118
    )


def _message_from_row(row: sqlite3.Row) -> ChatMessage:
    raw = row["events"]
    events: tuple[dict[str, Any], ...] = ()
    if raw:
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "message %s has an unreadable event trail; showing prose only", row["id"]
            )
        else:
            if isinstance(decoded, list):
                events = tuple(item for item in decoded if isinstance(item, dict))
    return ChatMessage(
        id=int(row["id"]),
        session_id=row["session_id"],
        turn_id=row["turn_id"],
        role=row["role"],
        content=row["content"],
        events=events,
        tokens_used=row["tokens_used"],
        created_at=row["created_at"],
    )
