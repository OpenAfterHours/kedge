"""The workbook registry: what kedge has seen, and what it derives about each one."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from kedge.registry import (
    REGISTRY_SCHEMA_VERSION,
    RegistryEntry,
    RegistryError,
    SourceState,
    WorkbookRegistry,
    describe,
    validate_workbook,
)
from kedge.workspace import Workspace

# ── fixtures ─────────────────────────────────────────────────────────────────────────────────


def _make_workbook(path: Path, *, content: bytes = b"<Types/>") -> Path:
    """Write the smallest thing that passes the OOXML sniff: a zip with a content-types part."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", content.decode())
        archive.writestr("xl/workbook.xml", "<workbook/>")
    return path


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "home"
    directory.mkdir()
    monkeypatch.setenv("KEDGE_HOME", str(directory))
    return directory


@pytest.fixture
def workbook(tmp_path: Path) -> Path:
    return _make_workbook(tmp_path / "processes" / "rwa_monthly.xlsx")


@pytest.fixture
def registry(home: Path) -> WorkbookRegistry:
    return WorkbookRegistry.for_user(home)


# ── validation ───────────────────────────────────────────────────────────────────────────────


def test_a_real_workbook_validates(workbook: Path) -> None:
    assert validate_workbook(workbook) == workbook.resolve()


def test_a_csv_renamed_to_xlsx_is_rejected_by_content_not_by_name(tmp_path: Path) -> None:
    """The common mistake is a rename, not an attack, and the message has to say so."""
    imposter = tmp_path / "not_really.xlsx"
    imposter.write_text("a,b\n1,2\n", encoding="utf-8")

    with pytest.raises(RegistryError, match="not a zip archive"):
        validate_workbook(imposter)


def test_a_zip_without_content_types_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "plain.xlsx"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("hello.txt", "hi")

    with pytest.raises(RegistryError, match="Office Open XML"):
        validate_workbook(archive)


def test_the_legacy_binary_formats_are_refused_with_a_way_forward(tmp_path: Path) -> None:
    legacy = tmp_path / "old.xls"
    legacy.write_bytes(b"\xd0\xcf\x11\xe0")

    with pytest.raises(RegistryError, match=r"re-save as \.xlsx"):
        validate_workbook(legacy)


def test_a_missing_file_is_refused_by_name(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="no such file"):
        validate_workbook(tmp_path / "absent.xlsx")


def test_a_directory_is_not_a_workbook(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="is a directory"):
        validate_workbook(tmp_path)


# ── persistence ──────────────────────────────────────────────────────────────────────────────


def test_an_added_workbook_round_trips_through_disk(
    registry: WorkbookRegistry, workbook: Path, home: Path
) -> None:
    entry = registry.add(workbook)

    reopened = WorkbookRegistry.for_user(home)
    stored = reopened.get(entry.key)

    assert stored is not None
    assert stored.path == str(workbook.resolve())
    assert stored.name == "rwa_monthly.xlsx"
    assert len(stored.sha256) == 64
    assert stored.size_bytes == workbook.stat().st_size
    assert stored.open_count == 0


def test_the_key_is_the_workspace_key_so_the_two_agree(
    registry: WorkbookRegistry, workbook: Path, home: Path
) -> None:
    """The registry and the workspace must name the same workbook the same way, or the marker
    files and the registry rows drift apart."""
    entry = registry.add(workbook)
    assert entry.key == Workspace.for_workbook(workbook, user_directory=home).key


def test_adding_the_same_workbook_twice_refreshes_rather_than_duplicating(
    registry: WorkbookRegistry, workbook: Path
) -> None:
    first = registry.add(workbook)
    _make_workbook(workbook, content=b"<Types>changed</Types>")
    second = registry.add(workbook)

    assert [entry.key for entry in registry.entries()] == [first.key]
    assert second.sha256 != first.sha256
    assert second.added_at == first.added_at, "the original add time is history, not a refresh"


def test_recording_an_open_stamps_the_time_and_counts_it(
    registry: WorkbookRegistry, workbook: Path
) -> None:
    entry = registry.add(workbook)
    assert entry.last_opened_at is None

    updated = registry.record_open(entry.key)

    assert updated is not None
    assert updated.last_opened_at is not None
    assert updated.open_count == 1


def test_entries_come_back_most_recently_used_first(
    registry: WorkbookRegistry, tmp_path: Path
) -> None:
    first = registry.add(_make_workbook(tmp_path / "one.xlsx"))
    registry.add(_make_workbook(tmp_path / "two.xlsx"))
    registry.record_open(first.key)

    assert [entry.name for entry in registry.entries()] == ["one.xlsx", "two.xlsx"]


def test_forgetting_removes_the_row_and_touches_nothing_on_disk(
    registry: WorkbookRegistry, workbook: Path
) -> None:
    entry = registry.add(workbook)

    assert registry.forget(entry.key) is True
    assert registry.entries() == []
    assert workbook.is_file(), "forgetting is a list operation, never a delete"
    assert registry.forget(entry.key) is False


def test_an_unreadable_registry_yields_an_empty_list_rather_than_raising(home: Path) -> None:
    """The landing page of a tool must open."""
    (home / "registry.json").write_text("{ this is not json", encoding="utf-8")

    assert WorkbookRegistry.for_user(home).entries() == []


def test_one_corrupt_row_costs_that_row_and_not_the_registry(
    registry: WorkbookRegistry, workbook: Path, home: Path
) -> None:
    registry.add(workbook)
    path = home / "registry.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["workbooks"].append({"name": "no path here"})
    path.write_text(json.dumps(raw), encoding="utf-8")

    assert [entry.name for entry in WorkbookRegistry.for_user(home).entries()] == [
        "rwa_monthly.xlsx"
    ]


# ── derivation ───────────────────────────────────────────────────────────────────────────────


def test_a_registered_workbook_whose_file_has_gone_renders_as_missing(
    registry: WorkbookRegistry, workbook: Path, home: Path
) -> None:
    """The entry survives — the user chose to add it — and nothing raises."""
    entry = registry.add(workbook)
    workbook.unlink()

    statuses = registry.statuses(probe=lambda _url: False)

    assert len(statuses) == 1
    status = statuses[0]
    assert status.entry.key == entry.key
    assert status.exists is False
    assert status.notebook_exists is False
    assert status.to_dict()["exists"] is False


def test_a_workbook_edited_since_kedge_saw_it_is_flagged(
    registry: WorkbookRegistry, workbook: Path, home: Path
) -> None:
    entry = registry.add(workbook)
    _make_workbook(workbook, content=b"<Types>much longer content than before</Types>")

    status = describe(entry, user_directory=home)

    assert status.exists is True
    assert status.changed_on_disk is True


def test_state_is_derived_from_the_analysis_and_the_plans_on_disk(
    registry: WorkbookRegistry, workbook: Path, home: Path
) -> None:
    entry = registry.add(workbook)
    workspace = Workspace.for_workbook(workbook, user_directory=home)
    workspace.ensure_dirs()
    workspace.notebook_path.write_text("import marimo\n", encoding="utf-8")
    workspace.analysis_path.write_text(
        json.dumps(
            {
                "findings": [
                    {"severity": "error", "message": "a"},
                    {"severity": "warning", "message": "b"},
                ],
                "operations": [{"id": "one"}, {"id": "two"}],
                "sheets": [{"name": "Calc"}],
                "cached_values": {"status": "present"},
            }
        ),
        encoding="utf-8",
    )
    (workspace.plans_dir / "plan-v001.yaml").write_text(
        "version: 1\napproval:\n  state: approved\nassessment:\n"
        "  convertible: 0.75\n  blockers: [manual overrides]\n",
        encoding="utf-8",
    )

    status = describe(entry, user_directory=home)

    assert status.notebook_exists is True
    assert status.findings == {"total": 2, "error": 1, "warning": 1, "info": 0}
    assert status.operation_count == 2
    assert status.cached_values == "present"
    assert status.plan_state == "approved"
    assert status.approved_version == 1
    assert status.convertible == pytest.approx(0.75)
    assert status.blockers == ("manual overrides",)


def test_an_unparseable_plan_costs_that_plan_and_not_the_page(
    registry: WorkbookRegistry, workbook: Path, home: Path
) -> None:
    entry = registry.add(workbook)
    workspace = Workspace.for_workbook(workbook, user_directory=home)
    workspace.ensure_dirs()
    (workspace.plans_dir / "plan-v001.yaml").write_text(
        "version: [1,\n  unclosed", encoding="utf-8"
    )

    status = describe(entry, user_directory=home)

    assert status.plan_state == "none"
    assert status.convertible is None


def test_a_live_marimo_is_reported_only_when_our_own_marker_answers(
    registry: WorkbookRegistry, workbook: Path, home: Path
) -> None:
    """Never auto-discovered: the marker is ours, and the server has to answer to count."""
    entry = registry.add(workbook)
    workspace = Workspace.for_workbook(workbook, user_directory=home)
    workspace.ensure_dirs()
    workspace.attach_marimo(host="127.0.0.1", port=2718, token="t", pid=4242)
    workspace.write_marker(kedge_version="0.1.0")

    silent = registry.statuses(probe=lambda _url: False)[0]
    assert silent.marimo_live is False

    answering = registry.statuses(probe=lambda _url: True)[0]
    assert answering.marimo_live is True
    assert answering.marimo_port == 2718
    assert answering.entry.key == entry.key


def test_a_probe_that_raises_does_not_break_the_listing(
    registry: WorkbookRegistry, workbook: Path, home: Path
) -> None:
    registry.add(workbook)
    workspace = Workspace.for_workbook(workbook, user_directory=home)
    workspace.ensure_dirs()
    workspace.attach_marimo(host="127.0.0.1", port=2718, token="t", pid=1)
    workspace.write_marker(kedge_version="0.1.0")

    def _explode(_url: str) -> bool:
        raise RuntimeError("the network is on fire")

    assert registry.statuses(probe=_explode)[0].marimo_live is False


def test_an_entry_dict_survives_a_round_trip() -> None:
    entry = RegistryEntry(key="k", path="C:/a.xlsx", name="a.xlsx", sha256="0" * 64, open_count=3)

    assert RegistryEntry.from_dict(entry.to_dict()) == entry


def test_a_row_with_no_path_is_dropped() -> None:
    assert RegistryEntry.from_dict({"name": "orphan.xlsx"}) is None


# ── releasing ────────────────────────────────────────────────────────────────────────────────


def test_a_workbook_that_is_still_there_is_linked(
    registry: WorkbookRegistry, workbook: Path, home: Path
) -> None:
    entry = registry.add(workbook)

    assert describe(entry, user_directory=home).source_state is SourceState.LINKED


def test_a_workbook_that_has_gone_with_nobody_saying_so_is_missing(
    registry: WorkbookRegistry, workbook: Path, home: Path
) -> None:
    """Moved, renamed, or on a drive that is not mounted -- the one reading that is a fault."""
    entry = registry.add(workbook)
    workbook.unlink()

    status = describe(entry, user_directory=home)

    assert status.source_state is SourceState.MISSING
    assert status.exists is False


def test_a_released_workbook_is_not_reported_as_breakage(
    registry: WorkbookRegistry, workbook: Path, home: Path
) -> None:
    """kedge's happy path terminates here, and it used to render as a file somebody had lost."""
    entry = registry.add(workbook)
    released = registry.release(entry.key)
    workbook.unlink()

    assert released is not None
    status = describe(released, user_directory=home)
    assert status.source_state is SourceState.RELEASED
    assert status.exists is False
    assert status.to_dict()["source_state"] == "released"


def test_a_released_entry_survives_a_registry_round_trip(
    registry: WorkbookRegistry, workbook: Path, home: Path
) -> None:
    """A release the registry forgets on the next page load is a release that did not happen."""
    entry = registry.add(workbook)
    registry.release(entry.key)

    stored = WorkbookRegistry.for_user(home).get(entry.key)

    assert stored is not None
    assert stored.released is True
    assert stored.released_at is not None
    assert stored.sha256 == entry.sha256, "the digest is evidence, and outlives the file"


def test_releasing_twice_keeps_the_first_answer(registry: WorkbookRegistry, workbook: Path) -> None:
    """When a process graduated is history, not something a second click gets to rewrite."""
    entry = registry.add(workbook)
    first = registry.release(entry.key)
    second = registry.release(entry.key)

    assert first is not None
    assert second is not None
    assert second.released_at == first.released_at


def test_releasing_a_key_nobody_registered_says_so(registry: WorkbookRegistry) -> None:
    assert registry.release("rwa_monthly-000000000000") is None


def test_releasing_marks_the_row_and_deletes_nothing(
    registry: WorkbookRegistry, workbook: Path
) -> None:
    """The file is kedge.purge's to remove and the decision is the registry's to record. Keeping
    them apart is what lets a failed delete leave a row that does not claim the file is gone."""
    entry = registry.add(workbook)
    registry.release(entry.key)

    assert workbook.is_file()


def test_putting_the_workbook_back_and_re_adding_it_undoes_the_release(
    registry: WorkbookRegistry, workbook: Path, home: Path
) -> None:
    """A readable, validated file at that path is the opposite of 'retired on purpose', so there
    is no separate un-release verb for a caller to forget."""
    entry = registry.add(workbook)
    registry.release(entry.key)

    relinked = registry.add(workbook)

    assert relinked.released_at is None
    assert describe(relinked, user_directory=home).source_state is SourceState.LINKED


def test_the_key_of_a_released_workbook_is_the_key_it_always_had(
    registry: WorkbookRegistry, workbook: Path, home: Path
) -> None:
    """Identity is a hash of the path *string*, which does not need a file to be stable, so the
    notebook and the project directory a released process keeps are still addressable."""
    entry = registry.add(workbook)
    registry.release(entry.key)
    workbook.unlink()

    stored = registry.get(entry.key)

    assert stored is not None
    assert describe(stored, user_directory=home).notebook_path == str(
        Workspace.for_workbook(workbook, user_directory=home).notebook_path
    )


# ── schema compatibility ─────────────────────────────────────────────────────────────────────


def test_a_row_written_before_the_release_field_existed_loads_as_linked(
    home: Path, workbook: Path
) -> None:
    """An older kedge wrote no released_at, and its absence has to mean 'still linked' rather
    than costing the user the row."""
    (home / "registry.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workbooks": [{"key": "k", "path": str(workbook), "name": workbook.name}],
            }
        ),
        encoding="utf-8",
    )

    entry = WorkbookRegistry.for_user(home).get("k")

    assert entry is not None
    assert entry.released_at is None
    assert entry.released is False
    assert describe(entry, user_directory=home).source_state is SourceState.LINKED


def test_a_registry_from_a_newer_kedge_is_read_rather_than_refused(
    home: Path, workbook: Path
) -> None:
    """Nothing gates on schema_version and nothing should: every change to this file has been
    additive, so refusing a number you do not recognise would lose rows for no reason."""
    (home / "registry.json").write_text(
        json.dumps(
            {
                "schema_version": REGISTRY_SCHEMA_VERSION + 7,
                "workbooks": [
                    {
                        "key": "k",
                        "path": str(workbook),
                        "name": workbook.name,
                        "something_a_later_kedge_added": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert [entry.key for entry in WorkbookRegistry.for_user(home).entries()] == ["k"]


def test_a_released_entry_dict_survives_a_round_trip() -> None:
    entry = RegistryEntry(key="k", path="C:/a.xlsx", name="a.xlsx", released_at="2026-08-29T00:00Z")

    assert RegistryEntry.from_dict(entry.to_dict()) == entry


# ── re-adopting a released process ───────────────────────────────────────────────────────────


def test_a_released_process_can_be_registered_with_no_workbook_to_validate(
    registry: WorkbookRegistry, tmp_path: Path, home: Path
) -> None:
    """add() requires a readable .xlsx, which is right for a new conversion and wrong for a
    process whose spreadsheet was retired on purpose."""
    retired = tmp_path / "processes" / "rwa_monthly.xlsx"
    retired.parent.mkdir(parents=True, exist_ok=True)

    entry = registry.register_released(retired)

    assert entry.released is True
    assert entry.key == Workspace.for_workbook(retired, user_directory=home).key
    assert describe(entry, user_directory=home).source_state is SourceState.RELEASED


def test_re_registering_a_released_process_keeps_its_history(
    registry: WorkbookRegistry, workbook: Path
) -> None:
    """The digest and the add time are the evidence the acceptance record cites, and they are
    the half of a released workbook that can never be recovered from disk."""
    entry = registry.add(workbook)
    released = registry.release(entry.key)
    assert released is not None
    workbook.unlink()

    again = registry.register_released(entry.path)

    assert again.key == entry.key
    assert again.added_at == entry.added_at
    assert again.sha256 == entry.sha256
    assert again.released_at == released.released_at
    assert len(registry.entries()) == 1


def test_registering_a_notebook_as_a_released_workbook_is_refused(
    registry: WorkbookRegistry, tmp_path: Path
) -> None:
    """The key, the project directory and the notebook name all come off this path, so a
    notebook handed in here would address a workspace nothing else in kedge agrees with."""
    with pytest.raises(RegistryError, match="not a workbook kedge can read"):
        registry.register_released(tmp_path / "rwa_monthly.kedge" / "rwa_monthly.py")


def test_registering_a_directory_as_a_released_workbook_is_refused(
    registry: WorkbookRegistry, tmp_path: Path
) -> None:
    with pytest.raises(RegistryError, match="is a directory"):
        registry.register_released(tmp_path)
