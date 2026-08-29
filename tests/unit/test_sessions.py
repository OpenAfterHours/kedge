"""The SQLite session and message store."""

from __future__ import annotations

from pathlib import Path

import pytest

from kedge.server.sessions import SessionStore, notebook_snapshot


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    opened = SessionStore(tmp_path / "sessions.sqlite")
    yield opened
    opened.close()


def _make(store: SessionStore, **kwargs: object) -> object:
    defaults = {"workbook_path": "C:/work/rwa.xlsx", "notebook_path": "C:/work/rwa.kedge/rwa.py"}
    return store.create_session(**{**defaults, **kwargs})


# ── sessions ─────────────────────────────────────────────────────────────────────────────────


def test_a_session_records_the_notebook_it_is_about(store: SessionStore) -> None:
    session = _make(store, snapshot="sha256:abc123def456")
    fetched = store.get_session(session.id)
    assert fetched is not None
    assert fetched.notebook_path == "C:/work/rwa.kedge/rwa.py"
    assert fetched.workbook_path == "C:/work/rwa.xlsx"
    assert fetched.snapshot == "sha256:abc123def456"


def test_unknown_session_is_none_rather_than_an_error(store: SessionStore) -> None:
    assert store.get_session("no-such-session") is None


def test_sessions_are_listed_newest_first(store: SessionStore) -> None:
    first = _make(store, title="oldest")
    second = _make(store, title="newest")
    store.append_message(second.id, role="user", content="later")
    listed = store.list_sessions()
    assert [session.id for session in listed] == [second.id, first.id]


def test_listing_can_be_restricted_to_one_notebook(store: SessionStore) -> None:
    mine = _make(store)
    _make(store, notebook_path="C:/other/other.py")
    listed = store.list_sessions(notebook_path="C:/work/rwa.kedge/rwa.py")
    assert [session.id for session in listed] == [mine.id]


def test_deleting_a_session_removes_its_messages(store: SessionStore) -> None:
    session = _make(store)
    store.append_message(session.id, role="user", content="hello")
    assert store.delete_session(session.id) is True
    assert store.get_session(session.id) is None
    assert store.messages(session.id) == []
    assert store.delete_session(session.id) is False


def test_deleting_a_notebook_s_sessions_takes_every_one_of_them(store: SessionStore) -> None:
    """Forgetting a workbook has to take the whole conversation, not the first page of it.

    ``list_sessions`` caps at ``limit``, so a caller that listed and then deleted would leave
    everything past the fiftieth row behind -- waiting to reappear the moment the same workbook
    was added again, which is the exact bug this is part of fixing.
    """
    kept = _make(store, notebook_path="C:/other/other.py")
    mine = [_make(store) for _ in range(60)]
    store.append_message(mine[0].id, role="user", content="hello")

    assert store.delete_sessions_for_notebook("C:/work/rwa.kedge/rwa.py") == 60
    assert store.list_sessions(notebook_path="C:/work/rwa.kedge/rwa.py") == []
    assert store.messages(mine[0].id) == []
    assert store.get_session(kept.id) is not None


def test_deleting_the_sessions_of_a_notebook_with_none_is_not_an_error(
    store: SessionStore,
) -> None:
    assert store.delete_sessions_for_notebook("C:/nowhere/none.py") == 0


def test_session_ids_for_a_notebook_are_not_capped(store: SessionStore) -> None:
    """The id list is what the purge is planned from, so a truncated one under-deletes."""
    for _ in range(60):
        _make(store)

    assert len(store.session_ids_for_notebook("C:/work/rwa.kedge/rwa.py")) == 60


def test_updating_only_touches_the_fields_supplied(store: SessionStore) -> None:
    session = _make(store, model="gpt-4o", snapshot="sha256:aaa")
    updated = store.update_session(session.id, title="Haircuts")
    assert updated is not None
    assert updated.title == "Haircuts"
    assert updated.model == "gpt-4o"
    assert updated.snapshot == "sha256:aaa"


# ── messages ─────────────────────────────────────────────────────────────────────────────────


def test_messages_come_back_oldest_first(store: SessionStore) -> None:
    session = _make(store)
    store.append_message(session.id, role="user", content="one")
    store.append_message(session.id, role="assistant", content="two")
    store.append_message(session.id, role="user", content="three")
    assert [message.content for message in store.messages(session.id)] == ["one", "two", "three"]


def test_an_assistant_message_keeps_the_turn_activity_trail(store: SessionStore) -> None:
    session = _make(store)
    trail = [
        {"type": "token", "text": "I will add a cell. "},
        {"type": "tool_call", "name": "propose_cell", "args_summary": "name=apply_haircuts"},
        {"type": "validation", "ok": True, "violations": []},
    ]
    store.append_message(
        session.id, role="assistant", content="I will add a cell. ", turn_id="t1", events=trail
    )
    stored = store.messages(session.id)[0]
    assert stored.turn_id == "t1"
    assert [item["type"] for item in stored.events] == ["token", "tool_call", "validation"]


def test_a_message_with_no_trail_reads_back_as_an_empty_trail(store: SessionStore) -> None:
    session = _make(store)
    store.append_message(session.id, role="user", content="hello")
    assert store.messages(session.id)[0].events == ()


def test_the_first_user_message_names_the_session(store: SessionStore) -> None:
    session = _make(store)
    assert session.title == "New chat"
    store.append_message(session.id, role="user", content="  Translate\nthe haircut lookup  ")
    renamed = store.get_session(session.id)
    assert renamed is not None
    assert renamed.title == "Translate the haircut lookup"


def test_a_long_first_message_is_truncated_into_a_title(store: SessionStore) -> None:
    session = _make(store)
    store.append_message(session.id, role="user", content="word " * 60)
    renamed = store.get_session(session.id)
    assert renamed is not None
    assert len(renamed.title) <= 70
    assert renamed.title.endswith("…")


def test_a_deliberate_title_is_not_overwritten_by_the_first_message(store: SessionStore) -> None:
    session = _make(store, title="Monthly RWA")
    store.append_message(session.id, role="user", content="anything at all")
    renamed = store.get_session(session.id)
    assert renamed is not None
    assert renamed.title == "Monthly RWA"


def test_message_count_and_updated_at_follow_the_messages(store: SessionStore) -> None:
    session = _make(store)
    store.append_message(session.id, role="user", content="one")
    store.append_message(session.id, role="assistant", content="two")
    fetched = store.get_session(session.id)
    assert fetched is not None
    assert fetched.message_count == 2


def test_role_is_constrained_by_the_schema(store: SessionStore) -> None:
    session = _make(store)
    with pytest.raises(Exception, match="CHECK constraint"):
        store.append_message(session.id, role="villain", content="x")  # type: ignore[arg-type]


# ── the snapshot marker ──────────────────────────────────────────────────────────────────────


def test_snapshot_changes_when_the_notebook_changes(tmp_path: Path) -> None:
    notebook = tmp_path / "process.py"
    notebook.write_text("import marimo\n", encoding="utf-8")
    before = notebook_snapshot(notebook)
    notebook.write_text("import marimo\n\n@app.cell\ndef _(): ...\n", encoding="utf-8")
    assert notebook_snapshot(notebook) != before


def test_snapshot_is_stable_for_an_unchanged_notebook(tmp_path: Path) -> None:
    notebook = tmp_path / "process.py"
    notebook.write_text("import marimo\n", encoding="utf-8")
    assert notebook_snapshot(notebook) == notebook_snapshot(notebook)


def test_a_missing_notebook_snapshots_as_absent_rather_than_raising(tmp_path: Path) -> None:
    assert notebook_snapshot(tmp_path / "nothing.py") == "absent"


# ── persistence ──────────────────────────────────────────────────────────────────────────────


def test_the_store_survives_being_closed_and_reopened(tmp_path: Path) -> None:
    path = tmp_path / "sessions.sqlite"
    first = SessionStore(path)
    session = first.create_session(workbook_path="a.xlsx", notebook_path="a.py")
    first.append_message(session.id, role="user", content="remembered")
    first.close()

    second = SessionStore(path)
    try:
        assert [message.content for message in second.messages(session.id)] == ["remembered"]
    finally:
        second.close()


def test_the_store_creates_its_parent_directory(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "deep" / "nested" / "sessions.sqlite")
    try:
        assert (tmp_path / "deep" / "nested").is_dir()
    finally:
        store.close()
