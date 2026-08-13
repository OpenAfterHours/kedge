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
    # The bridge preflight comes first, before a marimo is spawned: PLAN 6.1 mitigation 5 wants
    # a private-API mismatch to be one clear message up front, not a TypeError from inside a
    # tool call after the user has waited eight seconds for a notebook.
    assert reached[0] == "bridge"
    assert reached.index("bridge") < reached.index("launching")
    assert "cleanup" in reached
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


def test_a_bridge_that_no_longer_matches_stops_the_open_before_anything_is_spawned(
    client: TestClient, workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PLAN 6.1 mitigation 5, on the path that actually spawns a process.

    `marimo._code_mode` is private, so an upgrade can withdraw it. Until this step existed the
    only check on this path lived inside `NotebookDriver.for_workspace` at the scaffolding step
    -- after marimo had been started and a kernel session asserted -- and the `except KedgeError`
    there downgraded it to a stepped-over warning. The user got a notebook they could not drive
    and a chat pane whose every tool call would fail.
    """
    from kedge.notebook.driver import BridgeVersionError

    spawned: list[object] = []

    def _explode() -> None:
        msg = "kedge's marimo bridge does not match the installed marimo 9.9.9"
        raise BridgeVersionError(msg)

    monkeypatch.setattr("kedge.notebook.driver.verify_bridge", _explode)
    monkeypatch.setattr("kedge.lifecycle.launch_marimo", lambda *a, **k: spawned.append(a))
    monkeypatch.setattr("kedge.lifecycle.register_teardown", lambda _workspace: None)

    key = client.post("/api/hub/workbooks", json={"path": str(workbook)}).json()["workbook"]["key"]
    job_id = client.post("/api/hub/open", json={"key": key}).json()["job_id"]
    frames = _frames(client.get(f"/api/hub/open/{job_id}").text)

    steps = [frame for frame in frames if frame["type"] == "open_progress"]
    failed = [frame for frame in steps if frame["state"] == "failed"]
    assert [frame["step"] for frame in failed] == ["bridge"]
    assert "9.9.9" in failed[0]["detail"]

    assert not spawned, "no marimo may be started once the bridge is known not to match"
    assert "analysing" not in [frame["step"] for frame in steps]
    assert frames[-1]["type"] == "error"
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


# ── the plan the notebook is scaffolded from ─────────────────────────────────────────────────
#
# `kedge open --plan <file>` names the plan to scaffold from. Everything here is about the step
# that reads it: it must prefer the named file over the store, it must not scaffold from a file
# nobody approved, and neither a missing file nor a mangled one may reach the user as a traceback.


STORE_SUMMARY = "the decomposition already saved in the store"
"""What the seeded store's approved plan says, so a test can tell it from the one it names."""


class _StubDriver:
    """Just enough notebook bridge for the plan gate to be the only thing under test."""

    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []

    async def create_cell(self, code: str, *, name: str, **_kw: object):
        from kedge.notebook.model import CellRef, MutationResult

        self.created.append((name, code))
        return MutationResult(
            operation="create_cell", cell=CellRef(id="U1", name=name), ran=True, status="idle"
        )


def _bare_workspace(tmp_path: Path, home: Path) -> Workspace:
    """A workspace for a real workbook, with no plan of any kind saved for it."""
    workbook = _make_workbook(tmp_path / "processes" / "rwa_monthly.xlsx")
    workspace = Workspace.for_workbook(workbook, user_directory=home)
    workspace.ensure_dirs()
    return workspace


def _planned_workspace(tmp_path: Path, home: Path) -> Workspace:
    """A workspace whose plan store already holds an approved plan of its own.

    The store's plan is deliberately not the one the tests name with the flag. A test where both
    plans are the same proves nothing about which one was read.
    """
    from conftest import approved_plan_store, make_draft

    workspace = _bare_workspace(tmp_path, home)
    approved_plan_store(
        workspace.plans_dir,
        draft=make_draft(summary=STORE_SUMMARY),
        workbook="the-store-s-own.xlsx",
    )
    return workspace


def _plan_for(workspace: Workspace, *, summary: str = "named with the flag") -> object:
    """An approved plan written for this exact workbook, digest and all.

    The identity warning fires on a plan whose workbook does not match, which is right and is
    tested for -- but it would otherwise ride along on the detail line of every other assertion
    here and make them read as though a mismatch were normal. The summary is what tells two
    otherwise identical plans apart.
    """
    from conftest import make_approved_plan, make_draft
    from kedge.analysis.workbook import read_identity

    identity = read_identity(workspace.workbook_path)
    return make_approved_plan(
        draft=make_draft(summary=summary),
        workbook=workspace.workbook_path.name,
        workbook_sha256=identity.sha256,
    )


def _write_plan(path: Path, plan: object) -> Path:
    from kedge.plan.store import plan_to_yaml

    path.write_text(plan_to_yaml(plan), encoding="utf-8")
    return path


def _store(workspace: Workspace) -> object:
    from kedge.plan.store import PlanStore

    return PlanStore.for_workspace(workspace)


async def test_an_explicitly_named_plan_is_read_instead_of_the_stores_latest_approved(
    tmp_path: Path, home: Path
) -> None:
    """--plan means *this* plan. Both are approved here, so only provenance can decide."""
    from kedge.server.hub import _step_plan

    workspace = _planned_workspace(tmp_path, home)
    in_store = _store(workspace).latest_approved()
    named = _write_plan(
        tmp_path / "agreed.yaml", _plan_for(workspace, summary="the one named with the flag")
    )
    job = OpenJob(job_id="explicit", workbook=str(workspace.workbook_path))

    plan = await _step_plan(workspace, job, plan_path=named)

    assert in_store.summary == STORE_SUMMARY, "the two plans have to genuinely differ"
    assert plan is not None
    assert plan.summary == "the one named with the flag", "the store's plan must not win instead"
    detail = job.frames[-1].detail
    assert job.frames[-1].state == "ok"
    assert "--plan" in detail, "the terminal and the dialog both have to say where the plan is from"
    assert str(named) in detail


async def test_the_named_plan_is_recorded_so_the_rest_of_kedge_agrees_with_the_notebook(
    tmp_path: Path, home: Path
) -> None:
    """The scaffolded plan must not be invisible to everything that reads the store.

    `latest_approved()` is what "a plan is in force" means to the agent's write gate, `get_plan`,
    `propose_plan`'s already-approved refusal and the pinned plan block. A notebook scaffolded from
    a file the store has never heard of is a workspace at war with itself: cells implementing a
    decomposition the chat says does not exist, and a `propose_plan` offering to write a first one
    over the top of it.
    """
    from kedge.server.hub import _step_plan

    workspace = _bare_workspace(tmp_path, home)
    named = _write_plan(tmp_path / "agreed.yaml", _plan_for(workspace, summary="adopted by flag"))
    job = OpenJob(job_id="record", workbook=str(workspace.workbook_path))

    adopted = await _step_plan(workspace, job, plan_path=named)

    in_force = _store(workspace).latest_approved()
    assert in_force is not None, "nothing else in kedge can see a plan that was never recorded"
    assert in_force.summary == "adopted by flag"
    assert adopted.version == in_force.version, "the notebook is scaffolded from what was recorded"
    assert "recorded as plan v1" in job.frames[-1].detail


async def test_adopting_the_same_plan_again_records_nothing_new(tmp_path: Path, home: Path) -> None:
    """Re-opening a workbook is routine. `kedge plan approve` already refuses to record a
    decision that changes nothing, and a history of identical versions says nothing to the person
    reading it next quarter."""
    from kedge.server.hub import _step_plan

    workspace = _bare_workspace(tmp_path, home)
    named = _write_plan(tmp_path / "agreed.yaml", _plan_for(workspace))
    first = OpenJob(job_id="once", workbook=str(workspace.workbook_path))
    second = OpenJob(job_id="twice", workbook=str(workspace.workbook_path))

    await _step_plan(workspace, first, plan_path=named)
    await _step_plan(workspace, second, plan_path=named)

    assert _store(workspace).versions() == [1]
    assert "already the approved plan in force" in second.frames[-1].detail


async def test_the_agents_write_gate_agrees_once_a_named_plan_is_adopted(
    tmp_path: Path, home: Path
) -> None:
    """The assertion that would have caught the flag scaffolding a plan nothing else could see.

    Same store, same predicate the tools use. Before the adoption `propose_cell` is refused, which
    is what makes the second half worth asserting: it is the adoption that moves the gate, not the
    fixture.
    """
    from kedge.agent.context import NotebookState
    from kedge.agent.tools import ToolContext, ToolRegistry
    from kedge.server.hub import _step_plan

    workspace = _bare_workspace(tmp_path, home)
    named = _write_plan(tmp_path / "agreed.yaml", _plan_for(workspace))
    driver = _StubDriver()
    tools = ToolRegistry(ToolContext(driver=driver, plans=_store(workspace)))
    tools.refresh(NotebookState(cells=()))
    cell = {"name": "apply_haircuts", "code": "apply_haircuts = 1\n"}

    before = await tools.dispatch("propose_cell", cell)
    await _step_plan(workspace, OpenJob(job_id="gate", workbook=str(workspace.workbook_path)))
    refused_by_the_store = await tools.dispatch("propose_cell", cell)
    await _step_plan(
        workspace, OpenJob(job_id="gate", workbook=str(workspace.workbook_path)), plan_path=named
    )
    after = await tools.dispatch("propose_cell", cell)

    assert not before.ok and "no approved plan" in before.summary
    assert not refused_by_the_store.ok, "the store alone still has nothing approved in it"
    assert after.ok, "the tools must not refuse to write the notebook kedge just scaffolded"
    assert [name for name, _code in driver.created] == ["apply_haircuts"]


async def test_a_named_plan_superseding_an_approved_one_says_which_one(
    tmp_path: Path, home: Path
) -> None:
    """A decomposition never displaces another one quietly (see `_confirm_replacing_the_plan_in_force`).

    On this path there is nobody to ask -- the sequence is already running -- so the frame carries
    the fact instead, and it is the one the user needs: the plan they reviewed last month is no
    longer the one this notebook implements.
    """
    from kedge.server.hub import _step_plan

    workspace = _planned_workspace(tmp_path, home)
    in_force = _store(workspace).latest_approved()
    named = _write_plan(tmp_path / "agreed.yaml", _plan_for(workspace, summary="this month's"))
    job = OpenJob(job_id="supersede", workbook=str(workspace.workbook_path))

    await _step_plan(workspace, job, plan_path=named)

    detail = job.frames[-1].detail
    assert f"superseding v{in_force.version}" in detail
    assert f"recorded as plan v{in_force.version + 1}" in detail
    assert _store(workspace).latest_approved().summary == "this month's"


async def test_a_file_claiming_an_approval_the_review_gate_would_refuse_is_not_adopted(
    tmp_path: Path, home: Path
) -> None:
    """`approval.state: approved` typed by hand is not the same thing as an approved plan.

    Every plan that went through `kedge.plan.review.approve` is approvable by construction, so
    this refuses only a file that could not have come from the real path -- here one whose dropped
    range nobody ever acknowledged, which is the blocker the whole acknowledge verb exists for.
    Ten scaffolded cells behind a drop nobody agreed to is exactly the silent removal PLAN 2.2
    treats as indistinguishable from a bug.
    """
    from kedge.plan.model import Approval, ApprovalState
    from kedge.server.hub import _step_plan

    workspace = _bare_workspace(tmp_path, home)
    from conftest import make_plan

    claimed = make_plan(workbook=workspace.workbook_path.name).model_copy(
        update={"approval": Approval(state=ApprovalState.APPROVED, by="typed it in myself")}
    )
    named = _write_plan(tmp_path / "hand-approved.yaml", claimed)
    job = OpenJob(job_id="unapprovable", workbook=str(workspace.workbook_path))

    plan = await _step_plan(workspace, job, plan_path=named)

    assert plan is None
    assert job.frames[-1].state == "failed"
    detail = job.frames[-1].detail
    assert "has not been acknowledged" in detail, "the blockers are named, not just counted"
    assert _store(workspace).versions() == [], "nothing may be recorded from a file like this"


async def test_a_plan_approved_by_nobody_named_is_adopted_and_says_so(
    tmp_path: Path, home: Path
) -> None:
    """Who approved it is a record, not a gate.

    A hand-written file can leave `approval.by` out, and there is no honest way to fill it in
    afterwards. Refusing would block a legitimate plan over a missing name; recording the gap where
    the user can see it is what the rest of kedge does (`kedge plan approve` prints "nobody named").
    """
    from kedge.plan.model import Approval, ApprovalState
    from kedge.server.hub import _step_plan

    workspace = _bare_workspace(tmp_path, home)
    anonymous = _plan_for(workspace).model_copy(
        update={"approval": Approval(state=ApprovalState.APPROVED)}
    )
    named = _write_plan(tmp_path / "anonymous.yaml", anonymous)
    job = OpenJob(job_id="anonymous", workbook=str(workspace.workbook_path))

    plan = await _step_plan(workspace, job, plan_path=named)

    assert plan is not None, "a missing reviewer name is not a reason to refuse a plan"
    assert "approved by nobody named" in job.frames[-1].detail


async def test_the_step_refuses_a_foreign_plan_itself_and_not_only_the_cli_before_it(
    tmp_path: Path, home: Path
) -> None:
    """The guard on a durable artifact belongs beside the write, not only in front of one caller.

    `cli._require_usable_plan` refuses this before a marimo is spawned, and today that is the only
    way in -- `OpenWorkbookBody` has no plan field. The day it gains one, a refusal that lived only
    in the CLI would be gone with no test failing, which inverts the pattern the project states for
    its own gate: `sync_notebook` refuses an unapproved plan structurally, with no parameter
    that talks it out of it. So this drives the step directly, with no pre-flight in front of it.
    """
    from conftest import make_approved_plan
    from kedge.server.hub import _plan_from_file

    workspace = _bare_workspace(tmp_path, home)
    foreign = _write_plan(
        tmp_path / "foreign.yaml", make_approved_plan(workbook="quite_other.xlsx")
    )
    job = OpenJob(job_id="foreign", workbook=str(workspace.workbook_path))

    plan = await _plan_from_file(workspace, job, foreign)

    assert plan is None, "nothing may be scaffolded from another workbook's decomposition"
    assert job.frames[-1].state == "failed"
    assert _store(workspace).versions() == [], "and nothing may be written to this plan history"
    detail = job.frames[-1].detail
    assert "written for quite_other.xlsx, not rwa_monthly.xlsx" in detail
    assert "`workbook:` line" in detail, "the way through is the edit, and it has to be named"


async def test_a_workbook_that_has_merely_changed_is_adopted_with_a_warning(
    tmp_path: Path, home: Path
) -> None:
    """Same filename, different digest: what a monthly process looks like, so it is not refused.

    This is the carve-out that makes the refusal above safe to be a refusal -- and it is the state
    a user lands in after taking the advice in that refusal and editing the `workbook:` line.
    """
    from conftest import make_approved_plan
    from kedge.server.hub import _step_plan

    workspace = _bare_workspace(tmp_path, home)
    moved_on = _write_plan(
        tmp_path / "moved.yaml",
        make_approved_plan(workbook=workspace.workbook_path.name, workbook_sha256="b" * 64),
    )
    job = OpenJob(job_id="moved", workbook=str(workspace.workbook_path))

    assert await _step_plan(workspace, job, plan_path=moved_on) is not None
    assert "has changed since this plan was written" in job.frames[-1].detail


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("workbook", "", "does not record which workbook it was written for"),
        ("workbook_sha256", "", "records no sha256"),
    ],
)
async def test_a_plan_that_says_nothing_about_its_workbook_is_not_told_it_has_changed(
    tmp_path: Path, home: Path, field: str, value: str, expected: str
) -> None:
    """Saying the wrong thing about identity is worse than saying nothing.

    Both of these used to fall through to the digest branch and report that the workbook "has
    changed since this plan was written for it" -- a claim about a comparison that never happened.
    An empty `workbook` skipped the foreign-workbook check on the way past, too, so the file was
    adopted and then described inaccurately.
    """
    from kedge.server.hub import _step_plan

    workspace = _bare_workspace(tmp_path, home)
    named = _write_plan(
        tmp_path / "vague.yaml", _plan_for(workspace).model_copy(update={field: value})
    )
    job = OpenJob(job_id="vague", workbook=str(workspace.workbook_path))

    plan = await _step_plan(workspace, job, plan_path=named)

    assert plan is not None, "an unidentified plan claims nothing about another workbook"
    detail = job.frames[-1].detail
    assert expected in detail
    assert "has changed since" not in detail


async def test_an_unapproved_plan_file_is_stepped_over_rather_than_scaffolded(
    tmp_path: Path, home: Path
) -> None:
    """The review gate does not move because the user typed a path.

    `sync_notebook` refuses an unapproved plan structurally and has no override, so the only
    question is how that refusal reaches the user: as a legible step, with the workbook open and
    the notebook empty, rather than as an exception out of the open sequence. Falling back to the
    store's approved plan would be worse still -- it would scaffold a decomposition nobody asked
    for -- so this asserts nothing at all comes back.
    """
    from conftest import make_plan
    from kedge.server.hub import _step_plan

    workspace = _planned_workspace(tmp_path, home)
    named = _write_plan(tmp_path / "draft.yaml", make_plan(workbook="named-with-the-flag.xlsx"))
    job = OpenJob(job_id="unapproved", workbook=str(workspace.workbook_path))

    plan = await _step_plan(workspace, job, plan_path=named)

    assert plan is None, "an unapproved file must not silently fall back to the store's plan"
    assert job.frames[-1].state == "skipped", "a held gate is not a crash"
    detail = job.frames[-1].detail
    assert "'draft', not approved" in detail
    assert str(named) in detail
    assert "nothing is scaffolded unreviewed" in detail


async def test_a_plan_file_that_will_not_parse_is_one_message_naming_the_file(
    tmp_path: Path, home: Path
) -> None:
    """A hand-edited plan is invited (PLAN 2.2), so a mangled one is a normal thing to hit."""
    from kedge.server.hub import _step_plan

    workspace = _planned_workspace(tmp_path, home)
    named = tmp_path / "mangled.yaml"
    named.write_text("stages: [oh dear\n", encoding="utf-8")
    job = OpenJob(job_id="mangled", workbook=str(workspace.workbook_path))

    plan = await _step_plan(workspace, job, plan_path=named)

    assert plan is None
    assert job.frames[-1].state == "failed"
    detail = job.frames[-1].detail
    assert str(named) in detail, "the message has to name the file the user typed"
    assert "not valid YAML" in detail


async def test_a_plan_file_that_is_not_there_is_reported_not_raised(
    tmp_path: Path, home: Path
) -> None:
    """The CLI checks first, but the step is what the hub would call and it must not raise."""
    from kedge.server.hub import _step_plan

    workspace = _planned_workspace(tmp_path, home)
    missing = tmp_path / "never-written.yaml"
    job = OpenJob(job_id="missing", workbook=str(workspace.workbook_path))

    plan = await _step_plan(workspace, job, plan_path=missing)

    assert plan is None
    assert job.frames[-1].state == "failed"
    assert str(missing) in job.frames[-1].detail


async def test_the_scaffold_step_does_not_claim_no_plan_was_named_when_one_was(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "No approved plan" is the wrong account of a run where the user named one and it was refused.

    Two steps report the same silence and only one of them knows why. A reader told the wrong
    reason here goes looking in the plan store for something that was never the problem.
    """
    from kedge.server.hub import _step_scaffold

    monkeypatch.setattr(
        "kedge.notebook.driver.NotebookDriver.for_workspace", lambda _workspace: _StubDriver()
    )
    workspace = _bare_workspace(tmp_path, home)
    named = tmp_path / "refused.yaml"
    with_flag = OpenJob(job_id="named", workbook=str(workspace.workbook_path))
    without = OpenJob(job_id="unnamed", workbook=str(workspace.workbook_path))

    await _step_scaffold(workspace, None, with_flag, plan_path=named)
    await _step_scaffold(workspace, None, without)

    assert with_flag.frames[-1].state == "skipped"
    assert "was not adopted" in with_flag.frames[-1].detail
    assert str(named) in with_flag.frames[-1].detail
    assert without.frames[-1].detail == "no approved plan, so there is nothing to scaffold"


async def test_the_named_plan_survives_the_whole_open_sequence(
    client: TestClient,
    workbook: Path,
    no_marimo: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defect was the wiring, not the step: the flag was validated and then dropped.

    Asserted through `open_workbook` because that is the seam `kedge open` and the hub's Open
    button share -- a parameter that stops anywhere along `_run_open` is the same lie in `--help`
    as no parameter at all.
    """
    from kedge.server.hub import open_workbook

    seen: list[Path | None] = []

    async def _capture(_workspace, _job, *, plan_path=None):
        seen.append(plan_path)
        return None

    monkeypatch.setattr("kedge.server.hub._step_plan", _capture)
    named = tmp_path / "agreed.yaml"

    await open_workbook(_state(client), workbook, reattach=False, plan_path=named)

    assert seen == [named]


async def test_the_hubs_own_open_still_takes_the_stores_latest_approved(
    tmp_path: Path, home: Path
) -> None:
    """Nothing about the hub's route changed: no file named, no behaviour moved."""
    from kedge.server.hub import _step_plan

    workspace = _planned_workspace(tmp_path, home)
    job = OpenJob(job_id="store", workbook=str(workspace.workbook_path))

    plan = await _step_plan(workspace, job)

    assert plan is not None
    assert plan.workbook == "the-store-s-own.xlsx"
    assert "--plan" not in job.frames[-1].detail


# ── the contract the analysis step sketches ──────────────────────────────────────────────────


def _pasted_workbook(tmp_path: Path) -> Path:
    """The committed fixture: an `Extract` sheet holding the pasted result of a stored query."""
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "legacy_sql.xlsx"
    if not fixture.is_file():  # pragma: no cover - the fixture is committed
        pytest.skip("tests/fixtures/legacy_sql.xlsx has not landed yet")
    destination = tmp_path / "processes" / "legacy_sql.xlsx"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(fixture.read_bytes())
    return destination


def test_opening_a_workbook_leaves_a_contract_the_notebook_can_enforce(
    client: TestClient, tmp_path: Path, no_marimo: None, home: Path
) -> None:
    """The bootstrapping hole: no hand-in until there is a contract, no contract until a hand-in.

    A process fed by a pasted query result has neither on the first run, and the scaffolded
    check cell says plainly that it is enforcing nothing. The pasted sheet is last month's
    version of the file the user is about to export, so opening the workbook leaves a contract
    describing it -- and says in the step, and in the file, that it is a paste rather than an
    export anybody has checked.
    """
    workbook = _pasted_workbook(tmp_path)
    key = client.post("/api/hub/workbooks", json={"path": str(workbook)}).json()["workbook"]["key"]

    job_id = client.post("/api/hub/open", json={"key": key}).json()["job_id"]
    frames = _frames(client.get(f"/api/hub/open/{job_id}").text)

    workspace = Workspace.for_workbook(workbook, user_directory=home)
    assert workspace.contract_path.is_file()
    body = workspace.contract_path.read_text(encoding="utf-8")
    assert "SKETCHED" in body
    assert "trade_id" in body

    analysed = [
        frame
        for frame in frames
        if frame["type"] == "open_progress"
        and frame["step"] == "analysing"
        and frame["state"] == "ok"
    ]
    assert "Sketched contract.yaml from the 'Extract' sheet" in analysed[-1]["detail"]
    assert "read it" in analysed[-1]["detail"]


def test_a_contract_that_is_already_there_is_never_replaced_by_a_sketch(
    tmp_path: Path, home: Path
) -> None:
    """It may have been tightened by somebody who knows what the process receives."""
    from kedge.analysis.analyse import analyse
    from kedge.server.hub import _sketch_contract

    workbook = _pasted_workbook(tmp_path)
    workspace = Workspace.for_workbook(workbook, user_directory=home)
    workspace.ensure_dirs()
    workspace.contract_path.write_text("name: tightened-by-hand\n", encoding="utf-8")

    detail = _sketch_contract(workspace, analyse(workbook))

    assert workspace.contract_path.read_text(encoding="utf-8") == "name: tightened-by-hand\n"
    assert "left as it is" in detail


def test_a_workbook_with_no_pasted_sheet_opens_anyway_and_says_why(
    tmp_path: Path, home: Path
) -> None:
    """No contract is the state every workbook was in before this existed. It is never fatal."""
    from kedge.analysis.analyse import analyse
    from kedge.server.hub import _sketch_contract

    workbook = _make_workbook(tmp_path / "processes" / "rwa_monthly.xlsx")
    workspace = Workspace.for_workbook(workbook, user_directory=home)
    workspace.ensure_dirs()

    detail = _sketch_contract(workspace, analyse(workbook))

    assert not workspace.contract_path.exists()
    assert detail.startswith("No contract was sketched:")


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


def test_a_refused_open_names_the_workbook_holding_the_server(
    client: TestClient, workbook: Path, tmp_path: Path, home: Path
) -> None:
    """The refusal has to be answerable, so it says which workbook is in the way."""
    from kedge.server.agent_seam import ScriptedAgent

    other = _make_workbook(tmp_path / "other.xlsx")
    key = client.post("/api/hub/workbooks", json={"path": str(other)}).json()["workbook"]["key"]
    workspace = Workspace.for_workbook(workbook, user_directory=home)
    workspace.ensure_dirs()
    _state(client).attach(workspace, agent=ScriptedAgent(delay=0.0))

    response = client.post("/api/hub/open", json={"key": key})

    assert response.status_code == 409
    assert response.headers["X-Kedge-Open-Workbook"] == workspace.key
    assert "close it" in response.json()["detail"].lower()


# ── closing ──────────────────────────────────────────────────────────────────────────────────


def test_closing_releases_the_workbook_so_another_can_be_opened(
    client: TestClient, workbook: Path, tmp_path: Path, home: Path, no_marimo: None
) -> None:
    """The whole point: picking the wrong file costs a click, not a restart of the server."""
    from kedge.server.agent_seam import ScriptedAgent

    other = _make_workbook(tmp_path / "other.xlsx")
    key = client.post("/api/hub/workbooks", json={"path": str(other)}).json()["workbook"]["key"]
    workspace = Workspace.for_workbook(workbook, user_directory=home)
    workspace.ensure_dirs()
    _state(client).attach(workspace, agent=ScriptedAgent(delay=0.0), demo=True)

    assert client.post("/api/hub/open", json={"key": key}).status_code == 409

    closed = client.post("/api/hub/close")
    assert closed.status_code == 200
    assert closed.json()["name"] == workbook.name

    state = _state(client)
    assert state.attached is False
    assert state.agent is None
    assert state.demo is False, "demo mode belonged to the workbook that has just been let go"
    assert client.get("/api/context").json()["attached"] is False
    assert client.post("/api/hub/open", json={"key": key}).status_code == 202


def test_closing_is_refused_while_a_turn_is_in_flight(
    client: TestClient, workbook: Path, home: Path
) -> None:
    """The loop holds a driver pointed at the marimo this would stop."""
    from kedge.server.agent_seam import ScriptedAgent

    state = _state(client)
    workspace = Workspace.for_workbook(workbook, user_directory=home)
    workspace.ensure_dirs()
    state.attach(workspace, agent=ScriptedAgent(delay=0.0))
    state.turns.start("turn-1")

    response = client.post("/api/hub/close")

    assert response.status_code == 409
    assert "a turn is still running" in response.json()["detail"]
    assert state.attached is True

    state.turns.finish("turn-1")
    assert client.post("/api/hub/close").status_code == 200


def test_closing_nothing_is_not_an_error(client: TestClient) -> None:
    """A hub with no workbook open is the normal state, not a failed request."""
    response = client.post("/api/hub/close")

    assert response.status_code == 200
    assert response.json()["closed"] is None


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
