"""Removing a workbook and everything kedge derived from it (:mod:`kedge.purge`)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from kedge import purge
from kedge.workspace import Workspace


def _make_workbook(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types>kedge</Types>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    workbook = _make_workbook(tmp_path / "processes" / "rwa_monthly.xlsx")
    return Workspace.for_workbook(workbook, user_directory=tmp_path / "home")


def _populate(workspace: Workspace) -> None:
    """Give the workspace the artifacts a real conversion would have left."""
    workspace.ensure_dirs()
    workspace.notebook_path.write_text("# notebook", encoding="utf-8")
    workspace.analysis_path.write_text("{}", encoding="utf-8")
    (workspace.plans_dir / "plan-v001.yaml").write_text("stages: []", encoding="utf-8")
    workspace.runs_dir.mkdir(parents=True, exist_ok=True)
    (workspace.runs_dir / "20260829T000000Z.json").write_text("{}", encoding="utf-8")
    workspace.acceptance_path.write_text("{}", encoding="utf-8")
    workspace.marker_path.parent.mkdir(parents=True, exist_ok=True)
    workspace.marker_path.write_text("{}", encoding="utf-8")
    workspace.token_file_path.parent.mkdir(parents=True, exist_ok=True)
    workspace.token_file_path.write_text("token", encoding="utf-8")


# ── planning ─────────────────────────────────────────────────────────────────────────────────


def test_planning_a_purge_removes_nothing(workspace: Workspace) -> None:
    """The plan is what the confirmation is built from, so it has to be safe to build."""
    _populate(workspace)

    purge.plan_purge(workspace)

    assert workspace.notebook_path.is_file()
    assert workspace.project_dir.is_dir()
    assert workspace.workbook_path.is_file()


def test_the_project_directory_goes_as_a_tree_not_as_a_list_of_known_files(
    workspace: Workspace,
) -> None:
    """The agent writes files here under names it chooses, so nothing can enumerate them."""
    _populate(workspace)
    (workspace.project_dir / "something-the-model-named.md").write_text("x", encoding="utf-8")

    plan = purge.plan_purge(workspace)
    project = next(item for item in plan.owned if item.path == workspace.project_dir)

    assert project.is_tree
    assert project.contains is not None and project.contains >= 6

    purge.execute(plan, include_workbook=False)

    assert not workspace.project_dir.exists()


def test_the_outbound_logs_of_the_handed_in_sessions_are_included(workspace: Workspace) -> None:
    """They are named after the chat, not the workspace, so they cannot be found without it."""
    _populate(workspace)
    log = workspace.outbound_log_path("chat-abc")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("{}", encoding="utf-8")

    plan = purge.plan_purge(workspace, session_ids=["chat-abc"])
    purge.execute(plan, include_workbook=False)

    assert not log.exists()


def test_a_hand_in_store_inside_the_project_is_not_reported_as_external(
    workspace: Workspace,
) -> None:
    """It is already covered by the tree, and naming it twice would double-count the warning."""
    _populate(workspace)

    assert purge.plan_purge(workspace).external == ()


def test_a_hand_in_store_configured_outside_the_project_is_held_back(tmp_path: Path) -> None:
    """kedge did not create that directory and may be sharing it, so it is the caller's call."""
    workbook = _make_workbook(tmp_path / "processes" / "rwa_monthly.xlsx")
    elsewhere = tmp_path / "shared_handins"
    elsewhere.mkdir()
    (elsewhere / "january.csv").write_text("a,b\n", encoding="utf-8")
    (workbook.parent / "kedge.toml").write_text(
        f'[ingest]\nstore_dir = "{elsewhere.as_posix()}"\n', encoding="utf-8"
    )
    workspace = Workspace.for_workbook(workbook, user_directory=tmp_path / "home")
    _populate(workspace)

    plan = purge.plan_purge(workspace)

    assert [item.path for item in plan.external_present] == [elsewhere]

    purge.execute(plan, include_workbook=True)
    assert elsewhere.is_dir(), "external locations must not go unless the caller asks"

    purge.execute(plan, include_workbook=True, include_external=True)
    assert not elsewhere.exists()


# ── executing ────────────────────────────────────────────────────────────────────────────────


def test_executing_removes_the_derived_artifacts_and_the_machine_state(
    workspace: Workspace,
) -> None:
    _populate(workspace)

    result = purge.execute(purge.plan_purge(workspace), include_workbook=True)

    assert result.ok
    assert not workspace.project_dir.exists()
    assert not workspace.marker_path.exists()
    assert not workspace.token_file_path.exists()
    assert not workspace.workbook_path.exists()


def test_the_workbook_is_kept_when_it_is_not_asked_for(workspace: Workspace) -> None:
    """The flag exists so one call site cannot silently start deleting the source."""
    _populate(workspace)

    purge.execute(purge.plan_purge(workspace), include_workbook=False)

    assert workspace.workbook_path.is_file()
    assert not workspace.project_dir.exists()


def test_a_purge_of_a_workspace_that_was_never_opened_succeeds(workspace: Workspace) -> None:
    """Absent is success: the goal is that the thing is gone, not that it was there."""
    result = purge.execute(purge.plan_purge(workspace), include_workbook=True)

    assert result.ok
    assert not workspace.workbook_path.exists()


def test_a_failure_is_collected_rather_than_raised(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One locked file must not stop the other nine going, and must be named afterwards."""
    _populate(workspace)
    real_unlink = Path.unlink

    def refuse(self: Path, *args: object, **kwargs: object) -> None:
        if self == workspace.marker_path:
            msg = "being used by another process"
            raise OSError(msg)
        real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse)

    result = purge.execute(purge.plan_purge(workspace), include_workbook=True)

    assert not result.ok
    assert [path for path, _ in result.failures] == [workspace.marker_path]
    assert not workspace.project_dir.exists()
    assert not workspace.workbook_path.exists()


# ── describing ───────────────────────────────────────────────────────────────────────────────


def test_the_description_counts_what_is_really_there(workspace: Workspace) -> None:
    """The confirmation's whole job is telling the user that run records are in scope."""
    _populate(workspace)

    lines = purge.describe(purge.plan_purge(workspace), sessions=3)

    assert any("project directory" in line and "files" in line for line in lines)
    assert any("3 chat sessions" in line for line in lines)


def test_the_description_names_only_what_exists(workspace: Workspace) -> None:
    """A dialogue that lists a token file the user never had is a dialogue they stop reading."""
    lines = purge.describe(purge.plan_purge(workspace))

    assert not any("token" in line for line in lines)
