"""The hub: the landing page, the registry over HTTP, the file browser, and the open stream."""

from __future__ import annotations

import json
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kedge.server.app import ServerError, ServerState, create_hub_app
from kedge.server.events import OPEN_STEPS
from kedge.server.hub import OpenJob
from kedge.server.sessions import SessionStore
from kedge.workspace import Workspace

# ── fixtures ─────────────────────────────────────────────────────────────────────────────────


def _make_workbook(path: Path, *, note: str = "kedge") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", f"<Types>{note}</Types>")
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
def client(home: Path, tmp_path: Path) -> Iterator[TestClient]:
    app = create_hub_app(
        store=SessionStore(tmp_path / "sessions.sqlite"),
        user_directory=home,
    )
    with TestClient(app) as opened:
        yield opened


def _state(client: TestClient) -> ServerState:
    return client.app.state.kedge


# ── the page ─────────────────────────────────────────────────────────────────────────────────


def test_the_root_serves_the_hub_when_no_workbook_is_open(client: TestClient) -> None:
    """`kedge hub` and `kedge open` differ in exactly this."""
    response = client.get("/")

    assert response.status_code == 200
    assert 'id="hub-list"' in response.text
    assert "<iframe" not in response.text


def test_the_hub_is_reachable_by_name_as_well(client: TestClient) -> None:
    assert 'id="hub-list"' in client.get("/hub").text


def test_the_root_stops_being_the_hub_the_moment_a_workbook_is_open(
    client: TestClient, workbook: Path, home: Path
) -> None:
    """The two bodies of ``/``, and the header that stops a browser conflating them.

    Opening a workbook changes what ``/`` means. A browser that had cached the hub against that URL
    went on serving it after the open, so the "Go to the notebook" button on the open dialogue --
    which is a plain link to ``/`` -- redrew the hub instead. Nothing about that is visible from
    the server, which answers correctly every time it is asked; the fix is to make sure it is asked.
    """
    from kedge.server.agent_seam import ScriptedAgent

    before = client.get("/")
    assert 'id="hub-list"' in before.text
    assert before.headers["cache-control"] == "no-store"

    workspace = Workspace.for_workbook(workbook, user_directory=home)
    workspace.ensure_dirs()
    _state(client).attach(workspace, agent=ScriptedAgent(delay=0.0), demo=True)

    after = client.get("/")
    assert 'id="notebook-frame"' in after.text
    assert 'id="hub-list"' not in after.text
    assert after.headers["cache-control"] == "no-store"


def test_the_hub_renders_with_no_workbooks_at_all(client: TestClient) -> None:
    payload = client.get("/api/hub/state").json()

    assert payload["workbooks"] == []
    assert payload["attached"] is False
    assert payload["open_workbook"] is None
    assert payload["steps"] == list(OPEN_STEPS)


def test_context_says_nothing_is_attached_rather_than_erroring(client: TestClient) -> None:
    """The shell asks for context before it knows which mode it is in."""
    payload = client.get("/api/context").json()

    assert payload["attached"] is False
    assert payload["hub_url"] == "/hub"


def test_health_answers_in_hub_mode_and_says_why_there_is_no_kernel(client: TestClient) -> None:
    payload = client.get("/api/health").json()

    assert payload["server"] == "ok"
    assert payload["marimo"]["state"] == "absent"
    assert "hub" in payload["marimo"]["detail"]


def test_routes_that_need_a_workbook_answer_409_and_point_at_the_hub(client: TestClient) -> None:
    for path in ("/api/sessions", "/api/models"):
        response = client.get(path)
        assert response.status_code == 409, path
        assert "hub" in response.json()["detail"].lower(), path


# ── adding ───────────────────────────────────────────────────────────────────────────────────


def test_adding_a_workbook_by_path_registers_it(client: TestClient, workbook: Path) -> None:
    response = client.post("/api/hub/workbooks", json={"path": str(workbook)})

    assert response.status_code == 201
    assert response.json()["workbook"]["name"] == "rwa_monthly.xlsx"
    assert [item["name"] for item in client.get("/api/hub/state").json()["workbooks"]] == [
        "rwa_monthly.xlsx"
    ]


def test_a_non_workbook_is_refused_with_a_reason(client: TestClient, tmp_path: Path) -> None:
    imposter = tmp_path / "actually_a_csv.xlsx"
    imposter.write_text("a,b\n1,2\n", encoding="utf-8")

    response = client.post("/api/hub/workbooks", json={"path": str(imposter)})

    assert response.status_code == 400
    assert "not a zip archive" in response.json()["detail"]
    assert client.get("/api/hub/state").json()["workbooks"] == []


def test_a_workbook_that_is_not_there_is_refused(client: TestClient, tmp_path: Path) -> None:
    response = client.post("/api/hub/workbooks", json={"path": str(tmp_path / "ghost.xlsx")})

    assert response.status_code == 400
    assert "no such file" in response.json()["detail"]


def test_a_dropped_workbook_is_saved_under_the_user_directory_and_registered(
    client: TestClient, workbook: Path, home: Path
) -> None:
    response = client.post(
        "/api/hub/upload",
        files={"file": ("dropped.xlsx", workbook.read_bytes(), "application/octet-stream")},
    )

    assert response.status_code == 201
    saved = Path(response.json()["saved_to"])
    assert saved.parent == home / "dropped"
    assert saved.is_file()


def test_a_dropped_file_with_the_wrong_extension_is_refused_before_it_is_written(
    client: TestClient, home: Path
) -> None:
    response = client.post("/api/hub/upload", files={"file": ("notes.txt", b"hello", "text/plain")})

    assert response.status_code == 400
    assert not (home / "dropped").exists()


def test_a_dropped_file_that_is_not_really_a_workbook_leaves_nothing_behind(
    client: TestClient, home: Path
) -> None:
    response = client.post(
        "/api/hub/upload",
        files={"file": ("liar.xlsx", b"a,b\n1,2\n", "application/octet-stream")},
    )

    assert response.status_code == 400
    assert list((home / "dropped").iterdir()) == []


def test_forgetting_a_workbook_removes_the_row_and_not_the_file(
    client: TestClient, workbook: Path
) -> None:
    key = client.post("/api/hub/workbooks", json={"path": str(workbook)}).json()["workbook"]["key"]

    assert client.delete(f"/api/hub/workbooks/{key}").status_code == 200
    assert client.get("/api/hub/state").json()["workbooks"] == []
    assert workbook.is_file()
    assert client.delete(f"/api/hub/workbooks/{key}").status_code == 404


# ── derived state over HTTP ──────────────────────────────────────────────────────────────────


def test_a_workbook_whose_file_has_gone_is_listed_as_missing_not_dropped(
    client: TestClient, workbook: Path
) -> None:
    client.post("/api/hub/workbooks", json={"path": str(workbook)})
    workbook.unlink()

    listed = client.get("/api/hub/state").json()["workbooks"]

    assert len(listed) == 1
    assert listed[0]["exists"] is False
    assert listed[0]["name"] == "rwa_monthly.xlsx"


def test_findings_and_convertibility_are_reported_where_the_analysis_and_plan_exist(
    client: TestClient, workbook: Path, home: Path
) -> None:
    client.post("/api/hub/workbooks", json={"path": str(workbook)})
    workspace = Workspace.for_workbook(workbook, user_directory=home)
    workspace.ensure_dirs()
    workspace.analysis_path.write_text(
        json.dumps({"findings": [{"severity": "error"}], "operations": [{"id": "a"}]}),
        encoding="utf-8",
    )
    (workspace.plans_dir / "plan-v002.yaml").write_text(
        "version: 2\napproval:\n  state: approved\nassessment:\n  convertible: 0.4\n",
        encoding="utf-8",
    )

    listed = client.get("/api/hub/state").json()["workbooks"][0]

    assert listed["findings"] == {"total": 1, "error": 1, "warning": 0, "info": 0}
    assert listed["convertible"] == pytest.approx(0.4)
    assert listed["approved_version"] == 2


def test_the_report_link_404s_with_advice_until_one_has_been_generated(
    client: TestClient, workbook: Path, home: Path
) -> None:
    key = client.post("/api/hub/workbooks", json={"path": str(workbook)}).json()["workbook"]["key"]

    response = client.get(f"/api/hub/report/{key}")
    assert response.status_code == 404
    assert "kedge inspect" in response.json()["detail"]

    workspace = Workspace.for_workbook(workbook, user_directory=home)
    workspace.ensure_dirs()
    (workspace.project_dir / "report.html").write_text("<h1>report</h1>", encoding="utf-8")

    assert client.get("/api/hub/state").json()["workbooks"][0]["report_available"] is True
    assert "<h1>report</h1>" in client.get(f"/api/hub/report/{key}").text


# ── the file browser ─────────────────────────────────────────────────────────────────────────


def test_the_browser_lists_directories_and_workbooks_and_counts_the_rest(
    client: TestClient, tmp_path: Path
) -> None:
    root = tmp_path / "browse"
    (root / "sub").mkdir(parents=True)
    _make_workbook(root / "a.xlsx")
    (root / "notes.txt").write_text("hello", encoding="utf-8")

    payload = client.get("/api/hub/browse", params={"path": str(root)}).json()

    assert [item["name"] for item in payload["directories"]] == ["sub"]
    assert [item["name"] for item in payload["workbooks"]] == ["a.xlsx"]
    assert payload["other_file_count"] == 1
    assert payload["parent"] == str(root.parent)


def test_the_browser_hides_excels_lock_files(client: TestClient, tmp_path: Path) -> None:
    """`~$book.xlsx` is Excel's own lock file and is never something to open."""
    root = tmp_path / "browse"
    root.mkdir()
    _make_workbook(root / "book.xlsx")
    (root / "~$book.xlsx").write_bytes(b"lock")

    payload = client.get("/api/hub/browse", params={"path": str(root)}).json()

    assert [item["name"] for item in payload["workbooks"]] == ["book.xlsx"]


def test_browsing_something_that_is_not_a_directory_404s(
    client: TestClient, workbook: Path
) -> None:
    response = client.get("/api/hub/browse", params={"path": str(workbook)})

    assert response.status_code == 404


# ── the open sequence ────────────────────────────────────────────────────────────────────────


def test_opening_an_unknown_key_is_a_404(client: TestClient) -> None:
    response = client.post("/api/hub/open", json={"key": "nope"})

    assert response.status_code == 404


def test_streaming_an_unknown_job_is_a_404(client: TestClient) -> None:
    assert client.get("/api/hub/open/nosuchjob").status_code == 404


@pytest.fixture
def no_marimo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop the open sequence at the launch step without spawning a real marimo.

    A unit test must not put a subprocess on a port. The steps before this one — cleanup,
    analysis, the report, the plan lookup, the notebook file — are all offline and are exactly
    what is being asserted; the live-kernel half belongs to the contract tests.
    """
    from kedge.errors import NotebookError

    def _refuse(*_args: object, **_kwargs: object) -> None:
        msg = "marimo is not being started in a unit test"
        raise NotebookError(msg)

    monkeypatch.setattr("kedge.lifecycle.launch_marimo", _refuse)
    monkeypatch.setattr("kedge.lifecycle.register_teardown", lambda _workspace: None)


def test_the_open_sequence_streams_a_step_at_a_time_and_ends_with_a_verdict(
    client: TestClient, workbook: Path, no_marimo: None, home: Path
) -> None:
    """The point of PLAN M3: not a spinner, a running account of what is happening."""
    key = client.post("/api/hub/workbooks", json={"path": str(workbook)}).json()["workbook"]["key"]

    job_id = client.post("/api/hub/open", json={"key": key}).json()["job_id"]
    frames = _frames(client.get(f"/api/hub/open/{job_id}").text)

    steps = [frame for frame in frames if frame["type"] == "open_progress"]
    reached = [frame["step"] for frame in steps]
    assert reached[0] == "cleanup"
    assert "analysing" in reached
    assert "planning" in reached
    assert "notebook" in reached
    assert "launching" in reached

    analysed = [
        frame for frame in steps if frame["step"] == "analysing" and frame["state"] != "running"
    ]
    assert analysed, "the analysis step must resolve, not just start"
    assert "finding(s)" in analysed[-1]["detail"], (
        "the analysis step reports what it found; a malformed workbook is a finding, "
        "never a traceback"
    )

    planning = [
        frame for frame in steps if frame["step"] == "planning" and frame["state"] != "running"
    ]
    assert planning[-1]["state"] == "skipped", "no plan is a skip, not a failure"
    assert "no process plan yet" in planning[-1]["detail"]

    assert frames[-1]["type"] == "error", "a launch that cannot happen must end the stream"
    assert "marimo is not being started" in frames[-1]["message"]
    assert frames[-1]["recoverable"] is False


def test_a_failed_open_leaves_the_workbook_unattached_and_the_hub_usable(
    client: TestClient, workbook: Path, no_marimo: None
) -> None:
    key = client.post("/api/hub/workbooks", json={"path": str(workbook)}).json()["workbook"]["key"]
    job_id = client.post("/api/hub/open", json={"key": key}).json()["job_id"]
    _frames(client.get(f"/api/hub/open/{job_id}").text)

    assert _state(client).attached is False
    assert client.get("/api/hub/state").json()["attached"] is False


def test_the_analysis_step_writes_the_analysis_and_the_report_beside_the_workbook(
    client: TestClient, workbook: Path, no_marimo: None, home: Path
) -> None:
    """The hub's Report link has to be real the first time, not a button that generates one."""
    key = client.post("/api/hub/workbooks", json={"path": str(workbook)}).json()["workbook"]["key"]
    job_id = client.post("/api/hub/open", json={"key": key}).json()["job_id"]
    _frames(client.get(f"/api/hub/open/{job_id}").text)

    workspace = Workspace.for_workbook(workbook, user_directory=home)
    assert workspace.analysis_path.is_file()
    assert (workspace.project_dir / "report.html").is_file()
    assert workspace.notebook_path.is_file(), "marimo needs a file to open"
    assert client.get(f"/api/hub/report/{key}").status_code == 200


def test_the_stream_replays_everything_a_finished_job_said(client: TestClient) -> None:
    """A reloaded tab must be caught up, not join a sequence it cannot infer the start of."""
    state = _state(client)
    job = OpenJob(job_id="abc123", workbook="C:/a.xlsx")
    state.opens[job.job_id] = job
    job.step("cleanup", "ok", "nothing to clean up")
    job.step("analysing", "failed", "it exploded")
    job.close()

    frames = _frames(client.get("/api/hub/open/abc123").text)

    assert [(frame["step"], frame["state"]) for frame in frames] == [
        ("cleanup", "ok"),
        ("analysing", "failed"),
    ]


def _frames(text: str) -> list[dict]:
    events = []
    for block in text.split("\n\n"):
        payload = "\n".join(
            line[5:].strip() for line in block.split("\n") if line.startswith("data:")
        )
        if payload:
            events.append(json.loads(payload))
    return events


# ── attaching ────────────────────────────────────────────────────────────────────────────────


def test_attaching_a_workspace_turns_the_root_into_the_chat_shell(
    client: TestClient, workbook: Path, home: Path
) -> None:
    from kedge.server.agent_seam import ScriptedAgent

    state = _state(client)
    assert state.attached is False

    workspace = Workspace.for_workbook(workbook, user_directory=home)
    workspace.ensure_dirs()
    state.attach(workspace, agent=ScriptedAgent(delay=0.0), demo=True)

    assert state.attached is True
    assert 'id="notebook-frame"' in client.get("/").text
    assert client.get("/api/context").json()["attached"] is True
    assert client.get("/hub").status_code == 200, "the hub stays reachable from the chat view"


def test_attaching_a_second_different_workbook_is_refused(
    client: TestClient, workbook: Path, tmp_path: Path, home: Path
) -> None:
    """One server owns one workbook and one marimo process (PLAN 2.9)."""
    from kedge.server.agent_seam import ScriptedAgent

    state = _state(client)
    first = Workspace.for_workbook(workbook, user_directory=home)
    first.ensure_dirs()
    state.attach(first, agent=ScriptedAgent(delay=0.0))

    other = Workspace.for_workbook(_make_workbook(tmp_path / "other.xlsx"), user_directory=home)
    with pytest.raises(ServerError, match="already has"):
        state.attach(other, agent=ScriptedAgent(delay=0.0))


def test_opening_a_different_workbook_on_an_attached_server_is_a_409(
    client: TestClient, workbook: Path, tmp_path: Path, home: Path
) -> None:
    from kedge.server.agent_seam import ScriptedAgent

    other = _make_workbook(tmp_path / "other.xlsx")
    key = client.post("/api/hub/workbooks", json={"path": str(other)}).json()["workbook"]["key"]

    workspace = Workspace.for_workbook(workbook, user_directory=home)
    workspace.ensure_dirs()
    _state(client).attach(workspace, agent=ScriptedAgent(delay=0.0))

    response = client.post("/api/hub/open", json={"key": key})

    assert response.status_code == 409
    assert "one marimo process" in response.json()["detail"]


def test_a_refused_open_by_path_does_not_leave_a_row_behind(
    client: TestClient, workbook: Path, tmp_path: Path, home: Path
) -> None:
    """The conflict is decided before anything is written."""
    from kedge.server.agent_seam import ScriptedAgent

    workspace = Workspace.for_workbook(workbook, user_directory=home)
    workspace.ensure_dirs()
    _state(client).attach(workspace, agent=ScriptedAgent(delay=0.0))
    other = _make_workbook(tmp_path / "elsewhere.xlsx")

    response = client.post("/api/hub/open", json={"path": str(other)})

    assert response.status_code == 409
    assert client.get("/api/hub/state").json()["workbooks"] == []


def test_reopening_the_same_workbook_on_an_attached_server_is_allowed(
    client: TestClient, workbook: Path, home: Path, no_marimo: None
) -> None:
    """Re-opening the one already open is how a reattach works, so it must not be refused."""
    from kedge.server.agent_seam import ScriptedAgent

    key = client.post("/api/hub/workbooks", json={"path": str(workbook)}).json()["workbook"]["key"]
    workspace = Workspace.for_workbook(workbook, user_directory=home)
    workspace.ensure_dirs()
    _state(client).attach(workspace, agent=ScriptedAgent(delay=0.0))

    assert client.post("/api/hub/open", json={"key": key}).status_code == 202
