"""The :class:`Workspace`: one workbook's paths, config, and marimo session.

This is the seam that keeps kedge free of module-level global state (PLAN 2.9). Anything
process-wide hangs off a workspace instance, so "multi-user later" becomes "more workspaces"
rather than a rewrite, and tests can stand up a complete workspace in a ``tmp_path`` without
touching the real ``~/.kedge``.

A workspace owns two kinds of location. Project locations sit next to the workbook and hold
artifacts the user reviews and version-controls — the notebook, the plan history, the hand-in
store. Machine locations sit under ``~/.kedge`` and hold runtime state that is nobody's
deliverable — logs, the sessions database, and the marker file.

The marker file records a launched marimo server so the *next* launch can spot one left behind
by a crashed run. It is deliberately machine-scoped rather than project-scoped, for two reasons:
it carries the server's auth token, which has no business sitting in a directory that might be a
git repository or a shared drive; and a single scannable directory is what lets ``kedge doctor``
find every stale marker on the machine, which a per-project location would make impossible.

Staleness is never decided from the recorded PID. PID liveness checks are unreliable on Windows
(PLAN 6.2) and PIDs are recycled, so a recorded PID that is "alive" proves nothing. The marker
records everything needed to ask the server itself — port, token, and the notebook it was opened
on — and :mod:`kedge.lifecycle` decides liveness over HTTP.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from kedge.config import LoadedConfig, load_config, user_dir
from kedge.errors import WorkspaceError

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Iterator

    from kedge.config import Config

logger = logging.getLogger(__name__)

__all__ = [
    "MARKER_SUFFIX",
    "MarimoMarker",
    "MarimoSession",
    "Workspace",
    "iter_markers",
    "markers_dir",
]

MARKER_SUFFIX = ".marker.json"
MARKER_SCHEMA_VERSION = 1
PROJECT_DIR_SUFFIX = ".kedge"

_SLUG_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(text: str) -> str:
    cleaned = _SLUG_UNSAFE.sub("-", text).strip("-.")
    return cleaned[:48] or "workbook"


@dataclass(frozen=True, slots=True)
class MarimoSession:
    """Connection details for the marimo server this workspace owns.

    Populated in two steps, because the session id does not exist until the server is up:
    :meth:`Workspace.attach_marimo` records everything known at spawn time, and
    :meth:`Workspace.set_session_id` fills in the rest once ``/api/sessions`` has been resolved.

    ``process`` is the handle for the process we spawned, and is ``None`` when the session was
    reconstructed from a marker file rather than launched by this run.
    """

    host: str
    port: int
    token: str
    pid: int
    notebook_path: Path
    session_id: str | None = None
    process: subprocess.Popen[bytes] | None = None

    @property
    def base_url(self) -> str:
        """The server's origin, with no trailing slash."""
        return f"http://{self.host}:{self.port}"

    def __repr__(self) -> str:
        # The token is an auth credential; keep it out of tracebacks and log lines.
        return (
            f"MarimoSession(host={self.host!r}, port={self.port}, pid={self.pid}, "
            f"session_id={self.session_id!r}, token=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class MarimoMarker:
    """The on-disk record of a marimo server kedge launched.

    Internal runtime state rather than a published artifact, so a frozen dataclass with explicit
    serialisation rather than a pydantic model. It is written on launch and removed on clean
    teardown; one found at launch means the previous run did not exit cleanly.
    """

    schema_version: int
    pid: int
    host: str
    port: int
    token: str
    workbook: str
    notebook: str
    created_at: str
    kedge_version: str

    @property
    def base_url(self) -> str:
        """The origin the recorded server was listening on."""
        return f"http://{self.host}:{self.port}"

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable form written to disk."""
        return {
            "schema_version": self.schema_version,
            "pid": self.pid,
            "host": self.host,
            "port": self.port,
            "token": self.token,
            "workbook": self.workbook,
            "notebook": self.notebook,
            "created_at": self.created_at,
            "kedge_version": self.kedge_version,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> MarimoMarker:
        """Rebuild a marker from its on-disk form.

        Raises :class:`WorkspaceError` when a required field is absent or the wrong type; the
        caller is expected to treat that as "unusable marker" rather than as fatal.
        """
        try:
            return cls(
                schema_version=int(raw["schema_version"]),
                pid=int(raw["pid"]),
                host=str(raw["host"]),
                port=int(raw["port"]),
                token=str(raw["token"]),
                workbook=str(raw["workbook"]),
                notebook=str(raw["notebook"]),
                created_at=str(raw["created_at"]),
                kedge_version=str(raw["kedge_version"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            msg = f"marker file is missing or has a malformed field: {exc}"
            raise WorkspaceError(msg) from exc

    def __repr__(self) -> str:
        return (
            f"MarimoMarker(pid={self.pid}, host={self.host!r}, port={self.port}, "
            f"notebook={self.notebook!r}, created_at={self.created_at!r}, token=<redacted>)"
        )


def markers_dir(user_directory: Path | None = None) -> Path:
    """Return the machine-wide directory holding marimo marker files."""
    return (user_directory or user_dir()) / "markers"


def iter_markers(user_directory: Path | None = None) -> Iterator[tuple[Path, MarimoMarker]]:
    """Yield every readable marker file on this machine, with its path.

    Unreadable or malformed markers are logged and skipped rather than raised: a corrupt marker
    must never be able to prevent a launch or break ``kedge doctor``.
    """
    directory = markers_dir(user_directory)
    if not directory.is_dir():
        return
    for path in sorted(directory.glob(f"*{MARKER_SUFFIX}")):
        marker = _read_marker_file(path)
        if marker is not None:
            yield path, marker


def _read_marker_file(path: Path) -> MarimoMarker | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("ignoring unreadable marker file %s: %s", path, exc)
        return None
    if not isinstance(raw, dict):
        logger.warning("ignoring marker file %s: expected a JSON object", path)
        return None
    try:
        return MarimoMarker.from_dict(raw)
    except WorkspaceError as exc:
        logger.warning("ignoring marker file %s: %s", path, exc)
        return None


class Workspace:
    """Owns the paths, configuration, and marimo session for one workbook.

    Construct with :meth:`for_workbook`, which resolves the workbook path and loads the layered
    config for the project directory it derives. Nothing is created on disk until
    :meth:`ensure_dirs` is called, so building a workspace is cheap and side-effect free.
    """

    def __init__(
        self,
        workbook_path: Path,
        *,
        loaded_config: LoadedConfig,
        user_directory: Path,
    ) -> None:
        self._workbook_path = workbook_path
        self._loaded_config = loaded_config
        self._user_directory = user_directory
        self._marimo: MarimoSession | None = None

    # ── construction ─────────────────────────────────────────────────────────────────────

    @classmethod
    def for_workbook(
        cls,
        workbook: Path | str,
        *,
        loaded_config: LoadedConfig | None = None,
        user_directory: Path | None = None,
    ) -> Workspace:
        """Build a workspace for ``workbook``, loading config if none is supplied.

        The per-project ``kedge.toml`` is looked for beside the workbook rather than inside the
        generated project directory: it is hand-written and belongs where the user put the
        workbook, not among the artifacts kedge generates.

        The workbook is resolved to an absolute path but is not required to exist yet; callers
        that need it to exist should say so themselves, with a message about that specific file.
        """
        path = Path(workbook).expanduser()
        try:
            resolved = path.resolve()
        except OSError as exc:
            msg = f"could not resolve workbook path {path}: {exc}"
            raise WorkspaceError(msg) from exc
        if resolved.is_dir():
            msg = f"{resolved} is a directory, not a workbook file"
            raise WorkspaceError(msg)

        directory = (user_directory or user_dir()).expanduser()
        config = loaded_config or load_config(project_dir=resolved.parent)
        return cls(resolved, loaded_config=config, user_directory=directory)

    @staticmethod
    def _project_dir_for(workbook: Path) -> Path:
        return workbook.parent / f"{_slug(workbook.stem)}{PROJECT_DIR_SUFFIX}"

    # ── configuration ────────────────────────────────────────────────────────────────────

    @property
    def config(self) -> Config:
        """The resolved, validated configuration."""
        return self._loaded_config.config

    @property
    def loaded_config(self) -> LoadedConfig:
        """The configuration together with the provenance of each value."""
        return self._loaded_config

    def reload_config(self) -> LoadedConfig:
        """Re-read the layered config files and adopt the result.

        Config is loaded once at construction, which is right for a process that is handed its
        settings and gets on with it. The hub's settings panel breaks that assumption: the user
        changes the model endpoint while the server is running, and every later read of
        :attr:`config` should see it without a restart.

        Only the *configuration* is replaced. The marimo session, the paths and the workbook are
        untouched, because none of them can change without a different workspace. Anything already
        constructed from config — an ``OpenAIClient``, an agent loop — holds the old values and
        must be rebuilt by whoever built it.

        Raises:
            ConfigFileError: A config file exists but will not parse.
            ConfigValidationError: A value or key in one of them is not valid.
        """
        self._loaded_config = load_config(project_dir=self.workbook_path.parent)
        logger.debug("reloaded config for %s", self.workbook_path)
        return self._loaded_config

    # ── project locations ────────────────────────────────────────────────────────────────

    @property
    def workbook_path(self) -> Path:
        """The Excel workbook being converted."""
        return self._workbook_path

    @property
    def project_dir(self) -> Path:
        """The directory holding everything derived from this workbook.

        Sits next to the workbook and is named after it, so the association is obvious in a file
        listing and the artifacts travel with the workbook when it is copied.
        """
        return self._project_dir_for(self._workbook_path)

    @property
    def notebook_path(self) -> Path:
        """The marimo notebook generated from this workbook."""
        return self.project_dir / f"{_slug(self._workbook_path.stem)}.py"

    @property
    def plans_dir(self) -> Path:
        """Versioned process plans, diffable alongside the notebook (PLAN 2.2)."""
        return self.project_dir / "plans"

    @property
    def handins_dir(self) -> Path:
        """The managed hand-in store, overridable by ``ingest.store_dir``."""
        configured = self.config.ingest.store_dir
        if configured is not None:
            return configured if configured.is_absolute() else self.project_dir / configured
        return self.project_dir / "handins"

    @property
    def analysis_path(self) -> Path:
        """Where ``kedge inspect`` writes this workbook's analysis by default."""
        return self.project_dir / "analysis.json"

    # ── machine locations ────────────────────────────────────────────────────────────────

    @property
    def user_dir(self) -> Path:
        """The machine-wide kedge directory, normally ``~/.kedge``."""
        return self._user_directory

    @property
    def logs_dir(self) -> Path:
        """``~/.kedge/logs`` — including the outbound payload log (PLAN 2.3)."""
        return self._user_directory / "logs"

    @property
    def sessions_db_path(self) -> Path:
        """The SQLite database holding chat sessions and messages (PLAN M4)."""
        return self._user_directory / "sessions.sqlite"

    @property
    def key(self) -> str:
        """A stable identifier for this workspace, derived from the workbook's resolved path.

        Derived rather than generated, so it is identical across runs, and includes a digest so
        two workbooks with the same name in different folders cannot collide.
        """
        digest = hashlib.sha256(str(self._workbook_path).encode("utf-8")).hexdigest()[:12]
        return f"{_slug(self._workbook_path.stem)}-{digest}"

    @property
    def session_id(self) -> str:
        """The marimo kernel session id kedge asserts for this workspace.

        kedge chooses its own session id and connects a transport with it, rather than
        discovering one — a marimo server creates no session until a client connects, and the id
        is the caller's to pick (docs/marimo-api.md 5.3). That is a better fit for "never
        auto-discover" than matching on filenames.

        It must be *stable*, not fresh per launch: edit mode holds at most one session, so
        bootstrapping a second id evicts the first and the old one starts failing. Deriving it
        from the workbook path makes reconnecting idempotent.

        **The shape is not cosmetic.** marimo's own frontend will adopt a session id handed to it
        in the query string, but only one matching ``s_[0-9a-z]{6}`` — anything else it silently
        discards and mints its own. The iframe is not an observer here: on connecting it resumes
        the orphaned session kedge bootstrapped and *renames* it to whatever id it is using, so an
        id the frontend will not adopt is an id kedge loses the moment the notebook pane loads.
        Hence ``s_`` plus six hex digits of the same digest :attr:`key` uses. Uniqueness costs
        nothing here — the server serves exactly one notebook — so only stability matters.
        """
        digest = hashlib.sha256(str(self._workbook_path).encode("utf-8")).hexdigest()[:6]
        return f"s_{digest}"

    @property
    def marker_path(self) -> Path:
        """This workspace's marimo marker file."""
        return markers_dir(self._user_directory) / f"{self.key}{MARKER_SUFFIX}"

    @property
    def token_file_path(self) -> Path:
        """The file holding this run's marimo token, passed as ``--token-password-file``.

        A token on the command line is readable by any process running as this user, because
        Windows exposes other processes' command lines. A file under the user profile is not a
        strong boundary either, but it is a better one, and it keeps the credential out of the
        process table and out of anything that logs command lines.
        """
        return self._user_directory / "run" / f"{self.key}.token"

    def outbound_log_path(self, session: str) -> Path:
        """The outbound payload log for one chat session (PLAN 2.3)."""
        return self.logs_dir / f"outbound-{_slug(session)}.jsonl"

    # ── directories ──────────────────────────────────────────────────────────────────────

    def ensure_dirs(self) -> None:
        """Create every directory this workspace needs. Idempotent."""
        for directory in (
            self.project_dir,
            self.plans_dir,
            self.handins_dir,
            self._user_directory,
            self.logs_dir,
            markers_dir(self._user_directory),
            self.token_file_path.parent,
        ):
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                msg = f"could not create workspace directory {directory}: {exc}"
                raise WorkspaceError(msg) from exc
        logger.debug("workspace directories ready under %s", self.project_dir)

    # ── marimo session ───────────────────────────────────────────────────────────────────

    @property
    def marimo(self) -> MarimoSession | None:
        """The marimo server this workspace owns, or ``None`` if none is attached."""
        return self._marimo

    @property
    def has_marimo(self) -> bool:
        """Whether a marimo server is attached to this workspace."""
        return self._marimo is not None

    def require_marimo(self) -> MarimoSession:
        """Return the attached session, raising if the supervisor has not spawned one yet."""
        if self._marimo is None:
            msg = (
                "no marimo server is attached to this workspace; the supervisor must spawn one "
                "before the notebook can be driven"
            )
            raise WorkspaceError(msg)
        return self._marimo

    def attach_marimo(
        self,
        *,
        host: str,
        port: int,
        token: str,
        pid: int,
        process: subprocess.Popen[bytes] | None = None,
        session_id: str | None = None,
    ) -> MarimoSession:
        """Record the marimo server this workspace owns, and return the session.

        Called once by the supervisor after spawning the process. Attaching a second server
        without detaching the first is a bug: kedge owns exactly one marimo per workspace.
        """
        if self._marimo is not None:
            msg = (
                f"a marimo server is already attached on port {self._marimo.port}; "
                f"detach it before attaching another"
            )
            raise WorkspaceError(msg)
        self._marimo = MarimoSession(
            host=host,
            port=port,
            token=token,
            pid=pid,
            notebook_path=self.notebook_path,
            session_id=session_id,
            process=process,
        )
        logger.info("attached marimo server on %s (pid %d)", self._marimo.base_url, pid)
        return self._marimo

    def set_session_id(self, session_id: str) -> MarimoSession:
        """Record the session id resolved from ``/api/sessions``, and return the session."""
        session = self.require_marimo()
        self._marimo = replace(session, session_id=session_id)
        logger.debug("resolved marimo session id %s", session_id)
        return self._marimo

    def detach_marimo(self) -> None:
        """Forget the attached session. Does not stop the process; that is teardown's job."""
        self._marimo = None

    def notebook_url(self, *, with_token: bool = True) -> str:
        """Return the URL that opens this workspace's notebook in the browser.

        The access token goes in the query string deliberately. An iframe that loads
        unauthenticated lands on marimo's login page, which is the one endpoint that sets
        ``X-Frame-Options: DENY``, so the frame breaks. Passing ``access_token`` means the login
        page is never reached (PLAN 1.3).

        ``session_id`` is there for a sharper reason: without it the frontend mints its own id,
        and marimo's edit-mode resume then hands it kedge's session under that new name. kedge
        keeps addressing the old id and every ``/api/kernel/execute`` answers 500 "Invalid session
        id" — the notebook looks perfectly healthy in the frame while every tool call fails.
        Naming the session in the URL makes both sides agree on one id from the start
        (docs/marimo-api.md 5.3).
        """
        session = self.require_marimo()
        # The established id, not the derived one, so the frame is pointed at the session kedge
        # actually holds rather than the one it would have asked for.
        established = session.session_id or self.session_id
        query = f"file={quote(str(session.notebook_path))}&session_id={quote(established)}"
        if with_token:
            query += f"&access_token={quote(session.token)}"
        return f"{session.base_url}/?{query}"

    # ── marker file ──────────────────────────────────────────────────────────────────────

    def write_marker(self, *, kedge_version: str) -> MarimoMarker:
        """Write the marker file recording the attached marimo server.

        Written immediately after spawn, so that a run killed a moment later still leaves a
        usable trail. The write is atomic to avoid a half-written marker if the process dies
        mid-write.
        """
        session = self.require_marimo()
        marker = MarimoMarker(
            schema_version=MARKER_SCHEMA_VERSION,
            pid=session.pid,
            host=session.host,
            port=session.port,
            token=session.token,
            workbook=str(self._workbook_path),
            notebook=str(session.notebook_path),
            created_at=datetime.now(UTC).isoformat(),
            kedge_version=kedge_version,
        )
        path = self.marker_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(marker.to_dict(), indent=2), encoding="utf-8")
            temporary.replace(path)
        except OSError as exc:
            msg = f"could not write the marimo marker file {path}: {exc}"
            raise WorkspaceError(msg) from exc
        logger.debug("wrote marker file %s", path)
        return marker

    def read_marker(self) -> MarimoMarker | None:
        """Return this workspace's marker, or ``None`` if absent or unusable."""
        path = self.marker_path
        if not path.is_file():
            return None
        return _read_marker_file(path)

    # ── token file ───────────────────────────────────────────────────────────────────────

    def write_token_file(self, token: str) -> Path:
        """Write this run's marimo token to disk and return the path.

        The file is created with owner-only permissions where the platform honours them. On
        Windows ``chmod`` is close to a no-op, so the real protection is that the file lives
        under the user profile, whose default ACL excludes other users. It is removed on
        teardown.
        """
        path = self.token_file_path
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(token, encoding="utf-8")
            path.chmod(0o600)
        except OSError as exc:
            msg = f"could not write the marimo token file {path}: {exc}"
            raise WorkspaceError(msg) from exc
        return path

    def clear_token_file(self) -> bool:
        """Delete this workspace's token file. Returns whether one was there to delete."""
        try:
            self.token_file_path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            logger.warning(
                "could not remove the marimo token file %s: %s", self.token_file_path, exc
            )
            return False
        return True

    def clear_marker(self) -> bool:
        """Delete this workspace's marker file. Returns whether one was there to delete."""
        path = self.marker_path
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            msg = f"could not remove the stale marker file {path}: {exc}"
            raise WorkspaceError(msg) from exc
        logger.debug("cleared marker file %s", path)
        return True

    def __repr__(self) -> str:
        return f"Workspace(workbook={self._workbook_path!s}, marimo={self._marimo!r})"
