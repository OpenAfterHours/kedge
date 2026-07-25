"""Tests for the Workspace: derived paths, marimo session state, and the marker file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kedge.config import Config, LoadedConfig
from kedge.errors import WorkspaceError
from kedge.workspace import MarimoMarker, MarimoSession, Workspace, iter_markers, markers_dir

TOKEN = "token-that-must-not-be-logged-abcdef"


@pytest.fixture
def kedge_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "kedge-home"
    monkeypatch.setenv("KEDGE_HOME", str(home))
    return home


@pytest.fixture
def workbook(tmp_path: Path) -> Path:
    path = tmp_path / "processes" / "rwa monthly v14.xlsx"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not really a workbook")
    return path


def _workspace(workbook: Path, config: Config | None = None) -> Workspace:
    return Workspace.for_workbook(workbook, loaded_config=LoadedConfig(config=config or Config()))


# ── derived paths ────────────────────────────────────────────────────────────────────────────


def test_project_paths_are_derived_from_the_workbook(kedge_home: Path, workbook: Path) -> None:
    workspace = _workspace(workbook)

    assert workspace.workbook_path == workbook.resolve()
    assert workspace.project_dir == workbook.parent / "rwa-monthly-v14.kedge"
    assert workspace.notebook_path == workspace.project_dir / "rwa-monthly-v14.py"
    assert workspace.plans_dir == workspace.project_dir / "plans"
    assert workspace.handins_dir == workspace.project_dir / "handins"
    assert workspace.analysis_path == workspace.project_dir / "analysis.json"


def test_machine_paths_live_under_the_kedge_home(kedge_home: Path, workbook: Path) -> None:
    workspace = _workspace(workbook)

    assert workspace.user_dir == kedge_home
    assert workspace.logs_dir == kedge_home / "logs"
    assert workspace.sessions_db_path == kedge_home / "sessions.sqlite"
    assert workspace.marker_path.parent == kedge_home / "markers"
    assert workspace.token_file_path.parent == kedge_home / "run"
    assert workspace.outbound_log_path("abc123") == kedge_home / "logs" / "outbound-abc123.jsonl"


def test_the_handin_store_can_be_relocated_by_config(
    kedge_home: Path, workbook: Path, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "shared" / "handins"
    config = Config.model_validate({"ingest": {"store_dir": str(elsewhere)}})

    assert _workspace(workbook, config).handins_dir == elsewhere


def test_two_workbooks_with_the_same_name_get_different_markers(
    kedge_home: Path, tmp_path: Path
) -> None:
    first = tmp_path / "jan" / "process.xlsx"
    second = tmp_path / "feb" / "process.xlsx"
    for path in (first, second):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"")

    assert _workspace(first).marker_path != _workspace(second).marker_path
    assert _workspace(first).session_id != _workspace(second).session_id


def test_the_session_id_is_stable_across_workspaces_for_the_same_workbook(
    kedge_home: Path, workbook: Path
) -> None:
    """Edit mode holds one session, so a fresh id per launch would evict the previous one."""
    first = _workspace(workbook).session_id
    second = _workspace(workbook).session_id

    assert first == second
    assert first.startswith("kedge-")


def test_a_directory_is_not_a_workbook(kedge_home: Path, tmp_path: Path) -> None:
    with pytest.raises(WorkspaceError, match="directory"):
        Workspace.for_workbook(tmp_path)


def test_ensure_dirs_is_idempotent(kedge_home: Path, workbook: Path) -> None:
    workspace = _workspace(workbook)
    workspace.ensure_dirs()
    workspace.ensure_dirs()

    for directory in (
        workspace.project_dir,
        workspace.plans_dir,
        workspace.handins_dir,
        workspace.logs_dir,
        markers_dir(kedge_home),
        workspace.token_file_path.parent,
    ):
        assert directory.is_dir()


# ── marimo session state ─────────────────────────────────────────────────────────────────────


def test_marimo_session_starts_unattached(kedge_home: Path, workbook: Path) -> None:
    workspace = _workspace(workbook)

    assert workspace.marimo is None
    assert workspace.has_marimo is False
    with pytest.raises(WorkspaceError, match="no marimo server"):
        workspace.require_marimo()


def test_attaching_then_resolving_the_session_id(kedge_home: Path, workbook: Path) -> None:
    workspace = _workspace(workbook)

    session = workspace.attach_marimo(host="127.0.0.1", port=2718, token=TOKEN, pid=4242)
    assert isinstance(session, MarimoSession)
    assert session.session_id is None
    assert session.base_url == "http://127.0.0.1:2718"

    updated = workspace.set_session_id("kedge-abc")
    assert updated.session_id == "kedge-abc"
    assert workspace.require_marimo().session_id == "kedge-abc"
    assert updated.token == TOKEN, "replacing the session id must not disturb the rest"


def test_attaching_twice_is_refused(kedge_home: Path, workbook: Path) -> None:
    workspace = _workspace(workbook)
    workspace.attach_marimo(host="127.0.0.1", port=2718, token=TOKEN, pid=1)

    with pytest.raises(WorkspaceError, match="already attached"):
        workspace.attach_marimo(host="127.0.0.1", port=2719, token=TOKEN, pid=2)

    workspace.detach_marimo()
    workspace.attach_marimo(host="127.0.0.1", port=2719, token=TOKEN, pid=2)
    assert workspace.require_marimo().port == 2719


def test_notebook_url_carries_the_token_in_the_query_string(
    kedge_home: Path, workbook: Path
) -> None:
    """An iframe that loads unauthenticated hits the login page, the one page that refuses framing."""
    workspace = _workspace(workbook)
    workspace.attach_marimo(host="127.0.0.1", port=2718, token=TOKEN, pid=1)

    url = workspace.notebook_url()

    assert url.startswith("http://127.0.0.1:2718/?file=")
    assert f"access_token={TOKEN}" in url
    assert "access_token" not in workspace.notebook_url(with_token=False)


def test_the_token_is_not_in_the_session_repr(kedge_home: Path, workbook: Path) -> None:
    workspace = _workspace(workbook)
    workspace.attach_marimo(host="127.0.0.1", port=2718, token=TOKEN, pid=1)

    assert TOKEN not in repr(workspace.require_marimo())
    assert TOKEN not in repr(workspace)
    assert "<redacted>" in repr(workspace.require_marimo())


# ── marker file ──────────────────────────────────────────────────────────────────────────────


def test_marker_roundtrips_through_disk(kedge_home: Path, workbook: Path) -> None:
    workspace = _workspace(workbook)
    workspace.ensure_dirs()
    workspace.attach_marimo(host="127.0.0.1", port=2718, token=TOKEN, pid=4242)

    written = workspace.write_marker(kedge_version="0.1.0")
    read_back = workspace.read_marker()

    assert read_back == written
    assert read_back is not None
    assert read_back.pid == 4242
    assert read_back.port == 2718
    assert read_back.token == TOKEN
    assert read_back.workbook == str(workbook.resolve())
    assert read_back.notebook == str(workspace.notebook_path)
    assert read_back.base_url == "http://127.0.0.1:2718"
    assert read_back.created_at


def test_marker_records_everything_needed_to_probe_the_server_over_http(
    kedge_home: Path, workbook: Path
) -> None:
    """Staleness is decided by asking the server, not by checking the PID (PLAN 6.2)."""
    workspace = _workspace(workbook)
    workspace.ensure_dirs()
    workspace.attach_marimo(host="127.0.0.1", port=2718, token=TOKEN, pid=4242)
    workspace.write_marker(kedge_version="0.1.0")

    raw = json.loads(workspace.marker_path.read_text(encoding="utf-8"))

    assert {"host", "port", "token"} <= set(raw), "an HTTP identity check needs all three"
    assert raw["pid"] == 4242, "the pid is recorded for termination, not for liveness"


def test_a_missing_marker_reads_as_none(kedge_home: Path, workbook: Path) -> None:
    workspace = _workspace(workbook)

    assert workspace.read_marker() is None
    assert workspace.clear_marker() is False


def test_a_corrupt_marker_is_ignored_rather_than_fatal(kedge_home: Path, workbook: Path) -> None:
    workspace = _workspace(workbook)
    workspace.ensure_dirs()
    workspace.marker_path.write_text("{not json at all", encoding="utf-8")

    assert workspace.read_marker() is None
    assert list(iter_markers(kedge_home)) == []


def test_a_marker_missing_a_field_is_ignored(kedge_home: Path, workbook: Path) -> None:
    workspace = _workspace(workbook)
    workspace.ensure_dirs()
    workspace.marker_path.write_text(json.dumps({"pid": 1, "port": 2718}), encoding="utf-8")

    assert workspace.read_marker() is None


def test_clearing_a_marker_removes_it(kedge_home: Path, workbook: Path) -> None:
    workspace = _workspace(workbook)
    workspace.ensure_dirs()
    workspace.attach_marimo(host="127.0.0.1", port=2718, token=TOKEN, pid=1)
    workspace.write_marker(kedge_version="0.1.0")

    assert workspace.clear_marker() is True
    assert not workspace.marker_path.exists()
    assert workspace.read_marker() is None


def test_iter_markers_finds_every_marker_on_the_machine(kedge_home: Path, tmp_path: Path) -> None:
    for name in ("alpha.xlsx", "beta.xlsx"):
        path = tmp_path / name
        path.write_bytes(b"")
        workspace = _workspace(path)
        workspace.ensure_dirs()
        workspace.attach_marimo(host="127.0.0.1", port=2718, token=TOKEN, pid=1)
        workspace.write_marker(kedge_version="0.1.0")

    found = list(iter_markers(kedge_home))

    assert len(found) == 2
    assert all(isinstance(marker, MarimoMarker) for _, marker in found)


def test_the_token_is_not_in_the_marker_repr(kedge_home: Path, workbook: Path) -> None:
    workspace = _workspace(workbook)
    workspace.ensure_dirs()
    workspace.attach_marimo(host="127.0.0.1", port=2718, token=TOKEN, pid=1)

    marker = workspace.write_marker(kedge_version="0.1.0")

    assert TOKEN not in repr(marker)


def test_writing_a_marker_without_a_session_is_refused(kedge_home: Path, workbook: Path) -> None:
    with pytest.raises(WorkspaceError, match="no marimo server"):
        _workspace(workbook).write_marker(kedge_version="0.1.0")


# ── token file ───────────────────────────────────────────────────────────────────────────────


def test_the_token_file_lives_under_the_user_directory_not_the_project(
    kedge_home: Path, workbook: Path
) -> None:
    """The project directory may be a git repo or a shared drive; the token belongs in neither."""
    workspace = _workspace(workbook)
    workspace.ensure_dirs()

    path = workspace.write_token_file(TOKEN)

    assert path.read_text(encoding="utf-8") == TOKEN
    assert kedge_home in path.parents
    assert workspace.project_dir not in path.parents

    assert workspace.clear_token_file() is True
    assert not path.exists()
    assert workspace.clear_token_file() is False
