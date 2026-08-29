"""The hub: the landing page, the registry over HTTP, the file browser, and the open stream."""

from __future__ import annotations

import json
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kedge import purge
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


def test_the_upload_request_the_browser_actually_sends_is_diagnosable(
    client: TestClient, workbook: Path
) -> None:
    """The shape every dropped workbook arrived in, and the shape of the refusal.

    `hub.js` announced ``application/json`` over its ``FormData`` body, which is what stops the
    browser generating the multipart boundary -- so the file field never arrived and FastAPI
    rejected the request before the route ran. The refusal's ``detail`` is a *list*, and
    ``new Error`` on a list stringifies to "[object Object]", which is what the user actually saw.

    Both halves are asserted here because the client has to survive both: the header fix stops
    this request being sent, and the flattening stops any future 422 reading as nothing at all.
    """
    body = (
        b'--X\r\nContent-Disposition: form-data; name="file"; filename="dropped.xlsx"\r\n\r\n'
        + workbook.read_bytes()
        + b"\r\n--X--\r\n"
    )

    response = client.post(
        "/api/hub/upload", content=body, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


def test_the_hub_client_never_announces_json_over_a_body_that_frames_itself(
    client: TestClient,
) -> None:
    """The regression guard for the drop bug, asserted on the asset the browser is served.

    There is no JS runner in this repo, and the project already asserts on served asset text for
    exactly this class of drift. A `FormData` body must reach `fetch` with no content type of its
    own, or the boundary is never generated and every upload 422s.
    """
    for path in ("/static/hub.js", "/static/app.js"):
        script = client.get(path).text
        assert "instanceof FormData" in script, f"{path} does not guard the content type"
        assert "Array.isArray(detail)" in script, f"{path} would render a 422 as [object Object]"


def test_forgetting_a_workbook_deletes_it_and_everything_derived_from_it(
    client: TestClient, workbook: Path, home: Path
) -> None:
    """Forget means delete. The row was never the thing that made a workbook come back.

    Everything kedge writes is addressed from the workbook's resolved path, so removing the row
    alone left the notebook, the plans and the run records exactly where they were and re-adding
    the same file restored the lot.
    """
    key = client.post("/api/hub/workbooks", json={"path": str(workbook)}).json()["workbook"]["key"]
    workspace = Workspace.for_workbook(workbook, user_directory=home)
    workspace.ensure_dirs()
    workspace.notebook_path.write_text("# notebook", encoding="utf-8")
    (workspace.plans_dir / "plan-v001.yaml").write_text("stages: []", encoding="utf-8")

    response = client.delete(f"/api/hub/workbooks/{key}")

    assert response.status_code == 200
    assert client.get("/api/hub/state").json()["workbooks"] == []
    assert not workspace.project_dir.exists()
    assert not workbook.exists()
    assert client.delete(f"/api/hub/workbooks/{key}").status_code == 404


def test_the_deletion_preview_names_what_will_go_and_removes_none_of_it(
    client: TestClient, workbook: Path, home: Path
) -> None:
    """The confirmation is the whole safety mechanism, so its counts must be read, not written."""
    key = client.post("/api/hub/workbooks", json={"path": str(workbook)}).json()["workbook"]["key"]
    workspace = Workspace.for_workbook(workbook, user_directory=home)
    workspace.ensure_dirs()
    workspace.runs_dir.mkdir(parents=True, exist_ok=True)
    (workspace.runs_dir / "20260829T000000Z.json").write_text("{}", encoding="utf-8")

    data = client.get(f"/api/hub/workbooks/{key}/deletion").json()

    assert data["workbook_exists"] is True
    assert data["workbook"] == str(workbook.resolve())
    assert any("project directory" in line for line in data["items"])
    assert workbook.is_file(), "asking what would go must not make any of it go"
    assert workspace.project_dir.is_dir()


def test_a_workbook_that_is_open_on_this_server_cannot_be_forgotten(
    client: TestClient, workbook: Path, home: Path
) -> None:
    """Deleting a notebook a running marimo holds leaves the kernel on a file that is not there."""
    key = client.post("/api/hub/workbooks", json={"path": str(workbook)}).json()["workbook"]["key"]
    _state(client).workspace = Workspace.for_workbook(workbook, user_directory=home)

    response = client.delete(f"/api/hub/workbooks/{key}")

    assert response.status_code == 409
    assert "Close it first" in response.json()["detail"]
    assert workbook.is_file()
    assert client.get(f"/api/hub/workbooks/{key}/deletion").json()["open"] is True


def test_forgetting_a_workbook_takes_its_chat_sessions_with_it(
    client: TestClient, workbook: Path, home: Path
) -> None:
    """Sessions key off the notebook path, which is why the conversation used to come back."""
    key = client.post("/api/hub/workbooks", json={"path": str(workbook)}).json()["workbook"]["key"]
    workspace = Workspace.for_workbook(workbook, user_directory=home)
    store = _state(client).store
    session = store.create_session(
        workbook_path=str(workbook), notebook_path=str(workspace.notebook_path)
    )
    store.append_message(session.id, role="user", content="convert this")

    data = client.delete(f"/api/hub/workbooks/{key}").json()

    assert data["sessions"] == 1
    assert store.get_session(session.id) is None


def test_the_registry_row_survives_a_purge_that_could_not_finish(
    client: TestClient, workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-deleted workspace must stay visible, because the hub is where it can be retried."""
    key = client.post("/api/hub/workbooks", json={"path": str(workbook)}).json()["workbook"]["key"]

    def refuse(plan: object, **_: object) -> purge.PurgeResult:
        return purge.PurgeResult(removed=(), failures=((workbook, "being used by Excel"),))

    monkeypatch.setattr(purge, "execute", refuse)

    response = client.delete(f"/api/hub/workbooks/{key}")

    assert response.status_code == 409
    assert "being used by Excel" in response.json()["detail"]
    assert [item["key"] for item in client.get("/api/hub/state").json()["workbooks"]] == [key]


# ── releasing ────────────────────────────────────────────────────────────────────────────────
#
# Releasing is the successful end of a conversion, not a gentler forget: the notebook has become
# the monthly process and the spreadsheet is what has stopped being true. The tests below are
# about the two halves that are easy to get backwards -- that everything except the workbook
# survives, and that the registry never records a release the filesystem did not carry out.


def _converted(workbook: Path, home: Path) -> Workspace:
    """A workspace with a notebook in it, which is the precondition a release enforces.

    Releasing records that the notebook has become the process, so the route refuses a workbook
    kedge has built nothing from. Every test that expects a release to actually happen has to set
    that up rather than lean on a route that used to take anything.
    """
    workspace = Workspace.for_workbook(workbook, user_directory=home)
    workspace.ensure_dirs()
    workspace.notebook_path.write_text("# notebook", encoding="utf-8")
    return workspace


def test_the_release_preview_names_what_survives_and_removes_none_of_it(
    client: TestClient, workbook: Path, home: Path
) -> None:
    """The deletion preview makes a user hesitate; this one has to make a user confident.

    Deleting the spreadsheet a whole process was built on is only a reasonable click if the user
    is told exactly what is still there afterwards, with counts read off the disk rather than
    written into a sentence once. So the list has to be real -- and asking must cost nothing,
    which is the half that a preview quietly doing the work would break.
    """
    key = client.post("/api/hub/workbooks", json={"path": str(workbook)}).json()["workbook"]["key"]
    workspace = _converted(workbook, home)
    workspace.runs_dir.mkdir(parents=True, exist_ok=True)
    (workspace.runs_dir / "20260829T000000Z.json").write_text("{}", encoding="utf-8")

    data = client.get(f"/api/hub/workbooks/{key}/release").json()

    assert data["workbook_exists"] is True
    assert data["notebook_exists"] is True
    assert data["released"] is False
    assert any("project directory" in line for line in data["kept"])
    assert workbook.is_file(), "asking what a release keeps must not release anything"
    assert workspace.notebook_path.is_file()


def test_releasing_deletes_the_workbook_and_keeps_everything_else(
    client: TestClient, workbook: Path, home: Path
) -> None:
    """The whole feature in one assertion pair: the spreadsheet goes, the process stays.

    A release is derived from the purge enumeration with every item moved into ``kept``, so this
    also guards the direction that fails safely -- the next artifact added to that enumeration is
    kept by a release without anybody remembering to say so.
    """
    key = client.post("/api/hub/workbooks", json={"path": str(workbook)}).json()["workbook"]["key"]
    workspace = _converted(workbook, home)
    (workspace.plans_dir / "plan-v001.yaml").write_text("stages: []", encoding="utf-8")

    response = client.post(f"/api/hub/workbooks/{key}/release")

    assert response.status_code == 200
    assert response.json()["removed"] == 1, "only the workbook is removed"
    assert not workbook.exists()
    assert workspace.project_dir.is_dir()
    assert workspace.notebook_path.is_file()
    assert (workspace.plans_dir / "plan-v001.yaml").is_file()


def test_the_registry_reports_a_released_workbook_as_released_rather_than_missing(
    client: TestClient, workbook: Path, home: Path
) -> None:
    """The row stays, and its absence is reported as a decision rather than as breakage.

    This is the state the hub renders differently, and reading ``exists`` alone is how the
    successful end of a conversion came to be drawn as a file somebody had lost. ``exists`` is
    still false and still reported, because the two facts are kept apart on purpose.
    """
    key = client.post("/api/hub/workbooks", json={"path": str(workbook)}).json()["workbook"]["key"]
    _converted(workbook, home)

    client.post(f"/api/hub/workbooks/{key}/release")

    listed = client.get("/api/hub/state").json()["workbooks"]
    assert [item["key"] for item in listed] == [key]
    assert listed[0]["source_state"] == "released"
    assert listed[0]["exists"] is False
    assert listed[0]["released_at"]


def test_a_workbook_that_is_open_on_this_server_cannot_be_released(
    client: TestClient, workbook: Path, home: Path
) -> None:
    """Deleting the source under a live kernel is the same hazard a deletion is refused for.

    The scaffolded reconciliation cell reads the workbook, so a release taken mid-session leaves a
    running notebook failing on a file that is no longer there. The refusal has to say *that*,
    though, and not the deletion's sentence about the notebook -- a message describing the wrong
    hazard sends the user looking for a problem they do not have.
    """
    key = client.post("/api/hub/workbooks", json={"path": str(workbook)}).json()["workbook"]["key"]
    _state(client).workspace = Workspace.for_workbook(workbook, user_directory=home)

    response = client.post(f"/api/hub/workbooks/{key}/release")

    assert response.status_code == 409
    assert "Close it first, then release it." in response.json()["detail"]
    assert "reconciliation cell" in response.json()["detail"]
    assert workbook.is_file()
    assert client.get(f"/api/hub/workbooks/{key}/release").json()["open"] is True


def test_releasing_keeps_the_chat_sessions_that_record_how_it_was_converted(
    client: TestClient, workbook: Path, home: Path
) -> None:
    """Forgetting takes the conversation; releasing must not, and must say so.

    The sessions are the record of how the conversion was arrived at, and the process is
    continuing rather than ending. They are passed to the release plan only so the confirmation
    can name them among what survives -- naming them and then deleting them would be the worst of
    both, so both halves are asserted here.
    """
    key = client.post("/api/hub/workbooks", json={"path": str(workbook)}).json()["workbook"]["key"]
    workspace = _converted(workbook, home)
    store = _state(client).store
    session = store.create_session(
        workbook_path=str(workbook), notebook_path=str(workspace.notebook_path)
    )
    store.append_message(session.id, role="user", content="convert this")

    preview = client.get(f"/api/hub/workbooks/{key}/release").json()
    data = client.post(f"/api/hub/workbooks/{key}/release").json()

    assert preview["sessions"] == 1
    assert any("chat session" in line for line in preview["kept"])
    assert data["sessions"] == 1
    assert store.get_session(session.id) is not None


def test_a_release_that_could_not_delete_the_workbook_is_not_recorded_as_one(
    client: TestClient, workbook: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordering decision, asserted from the side that matters.

    ``released_at`` is a claim that the spreadsheet is gone, so it is stamped last and only on a
    clean result. Stamping first would leave the hub saying "released, the notebook is the
    process" over a workbook still sitting on the share -- which is precisely the lie this whole
    feature exists to stop telling, arrived at from the other direction.
    """
    key = client.post("/api/hub/workbooks", json={"path": str(workbook)}).json()["workbook"]["key"]
    _converted(workbook, home)

    def refuse(plan: object, **_: object) -> purge.PurgeResult:
        return purge.PurgeResult(removed=(), failures=((workbook, "being used by Excel"),))

    monkeypatch.setattr(purge, "execute", refuse)

    response = client.post(f"/api/hub/workbooks/{key}/release")

    assert response.status_code == 409
    assert "being used by Excel" in response.json()["detail"]
    listed = client.get("/api/hub/state").json()["workbooks"]
    assert listed[0]["released_at"] is None
    assert listed[0]["source_state"] == "linked", "nothing was deleted, so nothing is released"


def test_releasing_a_workbook_that_has_already_gone_records_the_decision(
    client: TestClient, workbook: Path, home: Path
) -> None:
    """The retry that recovers the one failure stamping last can leave behind.

    A delete that succeeds and a registry write that does not shows as ``missing``, which is the
    wrong framing for a deliberate act. It is recoverable rather than terminal precisely because
    removing an absent file is a success, so a second Release finishes the job -- and that is the
    same call a user makes on a workbook they deleted in Explorer themselves.
    """
    key = client.post("/api/hub/workbooks", json={"path": str(workbook)}).json()["workbook"]["key"]
    _converted(workbook, home)
    workbook.unlink()

    data = client.post(f"/api/hub/workbooks/{key}/release").json()

    assert data["removed"] == 0, "there was nothing left to delete"
    assert client.get("/api/hub/state").json()["workbooks"][0]["source_state"] == "released"


def test_a_released_workbook_can_still_be_forgotten(
    client: TestClient, workbook: Path, home: Path
) -> None:
    """Release keeps the artifacts; forget is still how a user is rid of them.

    And the deletion preview has to stay honest about a workbook that has already gone, because
    the confirmation is built from it: promising to delete a file that is not there is how a
    dialogue teaches a user to stop reading it.
    """
    key = client.post("/api/hub/workbooks", json={"path": str(workbook)}).json()["workbook"]["key"]
    workspace = _converted(workbook, home)
    client.post(f"/api/hub/workbooks/{key}/release")

    preview = client.get(f"/api/hub/workbooks/{key}/deletion").json()
    response = client.delete(f"/api/hub/workbooks/{key}")

    assert preview["workbook_exists"] is False
    assert response.status_code == 200
    assert client.get("/api/hub/state").json()["workbooks"] == []
    assert not workspace.project_dir.exists()


def test_the_release_preview_says_whether_there_is_an_acceptance_left_to_cite(
    client: TestClient, workbook: Path, home: Path
) -> None:
    """The one fact in this dialogue that a release destroys rather than deletes.

    Everything on the kept list survives the workbook. Whether the translation was ever accepted
    does not: the spreadsheet is the only thing the notebook's arithmetic could be measured
    against, so releasing without a record ends that question for the life of the notebook.
    """
    key = client.post("/api/hub/workbooks", json={"path": str(workbook)}).json()["workbook"]["key"]
    workspace = Workspace.for_workbook(workbook, user_directory=home)
    workspace.ensure_dirs()

    before = client.get(f"/api/hub/workbooks/{key}/release").json()
    (workspace.project_dir / "reconciliation.json").write_text(
        json.dumps({"status": "PASSED", "generated_at": "2026-08-29T00:00:00Z"}), encoding="utf-8"
    )
    after = client.get(f"/api/hub/workbooks/{key}/release").json()

    assert before["acceptance"] == "none"
    assert before["acceptance_status"] is None
    assert after["acceptance"] == "recorded"
    assert after["acceptance_status"] == "PASSED"
    assert after["accepted_at"] == "2026-08-29T00:00:00Z"


def test_an_acceptance_that_did_not_pass_still_counts_as_something_to_cite(
    client: TestClient, workbook: Path, home: Path
) -> None:
    """The question is whether a record survives, not whether it says what the user hoped.

    An acceptance recorded as a failure outlives the spreadsheet and is visible for ever, which is
    the opposite of the silence a release makes permanent. Grading it here would put the verdict in
    the route; it is passed through so the dialogue quotes it instead.
    """
    key = client.post("/api/hub/workbooks", json={"path": str(workbook)}).json()["workbook"]["key"]
    workspace = Workspace.for_workbook(workbook, user_directory=home)
    workspace.ensure_dirs()
    (workspace.project_dir / "reconciliation.json").write_text(
        json.dumps({"status": "NOT RECONCILED", "generated_at": "2026-08-29T00:00:00Z"}),
        encoding="utf-8",
    )

    data = client.get(f"/api/hub/workbooks/{key}/release").json()

    assert data["acceptance"] == "recorded"
    assert data["acceptance_status"] == "NOT RECONCILED"


def test_a_conversion_that_was_never_reconciled_is_still_allowed_to_be_released(
    client: TestClient, workbook: Path, home: Path
) -> None:
    """Warned about, never refused, and the refusal was the tempting mistake.

    A conversion that deliberately improves on the workbook reproduces nothing and can never be
    reconciled -- ``not_reproduced`` exists for exactly that -- so a gate here would make release
    unreachable for the conversions most likely to deserve it. It would also be the first place in
    kedge where an unreconciled state blocks an action rather than being reported as a decision
    with a reason, and it would be walked around by Forget or by one Explorer window. The friction
    belongs in the dialogue's typing box, not in a route that says no.
    """
    key = client.post("/api/hub/workbooks", json={"path": str(workbook)}).json()["workbook"]["key"]
    _converted(workbook, home)

    assert client.get(f"/api/hub/workbooks/{key}/release").json()["acceptance"] == "none"
    response = client.post(f"/api/hub/workbooks/{key}/release")

    assert response.status_code == 200
    assert not workbook.exists()


def test_a_workbook_kedge_has_built_no_notebook_from_cannot_be_released(
    client: TestClient, workbook: Path, home: Path
) -> None:
    """The hub hides the button here; the route has to refuse it, and they are not the same thing.

    A button is a convenience and a route is the contract, so a script, a stale page or a verb
    added later would otherwise get the purge of the wrong half: the spreadsheet deleted and an
    empty project directory kept. Worse than the deletion is the record -- a release files a claim
    the hub then renders as "the spreadsheet is gone and this notebook is the process", and over a
    workspace with no notebook that sentence is false. Forget is the verb for this, and the
    refusal names it, instruction first.
    """
    key = client.post("/api/hub/workbooks", json={"path": str(workbook)}).json()["workbook"]["key"]
    Workspace.for_workbook(workbook, user_directory=home).ensure_dirs()

    response = client.post(f"/api/hub/workbooks/{key}/release")

    assert response.status_code == 409
    assert response.json()["detail"].startswith("Convert rwa_monthly.xlsx first, or forget it")
    assert workbook.is_file()
    assert client.get("/api/hub/state").json()["workbooks"][0]["source_state"] == "linked"
    assert client.get(f"/api/hub/workbooks/{key}/release").json()["notebook_exists"] is False


def _marked(workspace: Workspace, *, port: int = 2718) -> None:
    """Give this workspace a marimo marker and token file, as a launch would leave behind."""
    workspace.attach_marimo(host="127.0.0.1", port=port, token="tok-release", pid=999_999)
    workspace.write_token_file("tok-release")
    workspace.write_marker(kedge_version="0.1.0")


def test_a_release_leaves_the_marker_alone_while_something_is_still_serving_it(
    client: TestClient, workbook: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The marker is the only record of a live marimo's port and token.

    Removing it orphans the very process ``cleanup_orphan`` exists to find, so a release keeps it
    for as long as the reason to keep it holds. This is also why the release route does not simply
    call ``cleanup_orphan``: that answers the same liveness question by *stopping* the server, and
    the notebook that server is serving is the process this release is graduating.
    """
    key = client.post("/api/hub/workbooks", json={"path": str(workbook)}).json()["workbook"]["key"]
    workspace = _converted(workbook, home)
    _marked(workspace)
    monkeypatch.setattr("kedge.lifecycle.health_check", lambda *_a, **_k: True)

    data = client.post(f"/api/hub/workbooks/{key}/release").json()

    assert data["marker"] == "kept"
    assert "still serving" in data["marker_detail"]
    assert workspace.marker_path.is_file()
    assert workspace.token_file_path.is_file()
    assert not workbook.exists(), "the release itself still happened"


def test_a_release_takes_a_stale_marker_and_its_token_with_the_workbook(
    client: TestClient, workbook: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A marker for a server that is gone is an inert credential, and inert is not fine.

    It sits in plaintext under ``~/.kedge`` until somebody opens the workspace again, and a
    released workspace is precisely the one nobody opens for a month. The workbook is gone by then
    anyway, so there is nothing left for the marker to be the trail to.
    """
    key = client.post("/api/hub/workbooks", json={"path": str(workbook)}).json()["workbook"]["key"]
    workspace = _converted(workbook, home)
    _marked(workspace)
    monkeypatch.setattr("kedge.lifecycle.health_check", lambda *_a, **_k: False)

    data = client.post(f"/api/hub/workbooks/{key}/release").json()

    assert data["marker"] == "cleared"
    assert not workspace.marker_path.exists()
    assert not workspace.token_file_path.exists()


def test_the_release_preview_never_lists_the_marker_among_what_survives(
    client: TestClient, workbook: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kept list is a list of guarantees, and the marker's fate is not one of them.

    ``plan_release`` keeps the marker and token, correctly -- it does no HTTP and cannot tell a
    running server from a dead one. Striking them only when the probe says *stale right now* was
    the obvious fix and it left a race with a consequence out of proportion to its size: a marker
    live at the preview and dead at the click was promised as kept and then swept, so the one list
    whose entire job is to be a list of guarantees was the thing that turned out not to be one.

    They are therefore struck whatever the probe says. Why a live marker survives is prose in the
    dialogue, where it can be conditional, and what actually happened comes back on the POST.
    """
    key = client.post("/api/hub/workbooks", json={"path": str(workbook)}).json()["workbook"]["key"]
    workspace = _converted(workbook, home)
    _marked(workspace)

    monkeypatch.setattr("kedge.lifecycle.health_check", lambda *_a, **_k: True)
    live = client.get(f"/api/hub/workbooks/{key}/release").json()
    monkeypatch.setattr("kedge.lifecycle.health_check", lambda *_a, **_k: False)
    stale = client.get(f"/api/hub/workbooks/{key}/release").json()

    assert live["marker"] == "live"
    assert stale["marker"] == "stale"
    for shown in (live, stale):
        assert not any("marimo marker" in line for line in shown["kept"])
        assert not any("marimo token" in line for line in shown["kept"])
    assert any("project directory" in line for line in live["kept"]), "the rest is still listed"
    assert workspace.marker_path.is_file(), "asking must still not sweep anything"


def test_the_count_of_what_a_release_kept_excludes_what_it_swept(
    client: TestClient, workbook: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counted after the sweep, because ``kept_present`` is read off the disk.

    Taken before it, the number includes the marker and token the same call is about to delete.
    Nothing renders it today, which is exactly the reason to get it right now: a wrong number
    nobody looks at is a wrong number that gets shipped the day somebody does.
    """
    key = client.post("/api/hub/workbooks", json={"path": str(workbook)}).json()["workbook"]["key"]
    workspace = _converted(workbook, home)
    _marked(workspace)
    monkeypatch.setattr("kedge.lifecycle.health_check", lambda *_a, **_k: False)

    data = client.post(f"/api/hub/workbooks/{key}/release").json()

    assert data["marker"] == "cleared"
    assert data["kept"] == 1, "the project directory, and not the two files just deleted"


def test_a_release_says_plainly_when_there_was_no_marker_at_all(
    client: TestClient, workbook: Path, home: Path
) -> None:
    """The ordinary case, and it must not be reported as though something was tidied away."""
    key = client.post("/api/hub/workbooks", json={"path": str(workbook)}).json()["workbook"]["key"]
    _converted(workbook, home)

    data = client.post(f"/api/hub/workbooks/{key}/release").json()

    assert data["marker"] == "absent"


async def test_opening_a_released_workbook_reads_the_analysis_kedge_kept(
    tmp_path: Path, home: Path
) -> None:
    """The hub offers Open on a released row, so the open sequence has to survive one.

    ``analyse`` raises on a file that is not there, which made that button a sequence that always
    died on the analysis step -- the notebook is the process now, and the process could not be
    started. The recorded ``analysis.json`` sits inside the project directory a release keeps, so
    there is nothing to regenerate and nothing to guess.
    """
    from kedge.analysis.model import WorkbookAnalysis
    from kedge.analysis.workbook import read_identity
    from kedge.server.hub import _step_analyse

    workbook = _make_workbook(tmp_path / "processes" / "rwa_monthly.xlsx")
    workspace = Workspace.for_workbook(workbook, user_directory=home)
    workspace.ensure_dirs()
    recorded = WorkbookAnalysis(
        kedge_version="0.0.0-test",
        generated_at="2026-08-29T00:00:00Z",
        workbook=read_identity(workbook),
    )
    workspace.analysis_path.write_text(recorded.model_dump_json(indent=2), encoding="utf-8")
    workbook.unlink()
    job = OpenJob(job_id="released", workbook=str(workbook))

    analysis = await _step_analyse(workspace, job)

    assert analysis is not None
    assert [frame.state for frame in job.frames] == ["ok"]
    assert "released" in job.frames[0].detail


async def test_a_released_workbook_with_no_recorded_analysis_says_the_file_is_missing(
    tmp_path: Path, home: Path
) -> None:
    """The fallback is narrow on purpose: an absent workbook, and an analysis that was kept.

    An empty analysis invented for a workbook nobody ever analysed would let the open sequence run
    all the way to a chat pane whose every answer is about a spreadsheet kedge has never read.
    Naming the file that is not there is the honest answer, and it is the one the analyser already
    gives.
    """
    from kedge.errors import KedgeError
    from kedge.server.hub import _step_analyse

    workbook = _make_workbook(tmp_path / "processes" / "rwa_monthly.xlsx")
    workspace = Workspace.for_workbook(workbook, user_directory=home)
    workspace.ensure_dirs()
    workbook.unlink()
    job = OpenJob(job_id="never-analysed", workbook=str(workbook))

    with pytest.raises(KedgeError, match="does not exist"):
        await _step_analyse(workspace, job)


# ── marimo's own AI assistant ────────────────────────────────────────────────────────────────
#
# kedge writes a `.marimo.toml` at launch to switch marimo's built-in assistant off, and that
# control fails open on purpose: a write that fails leaves the assistant live and the launch goes
# ahead, because refusing to start would not un-write a credential and would take away the
# settings panel that clears it. Failing open into a log line is the part that is not acceptable,
# and these are the two places the user is told instead.


def _marimo_toml(workspace: Workspace, body: str) -> Path:
    """Write a `.marimo.toml` into the project directory, as marimo's own editor would."""
    workspace.ensure_dirs()
    path = workspace.project_dir / ".marimo.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_a_credential_left_in_marimo_toml_is_reported_by_name_and_never_by_value(
    client: TestClient, workbook: Path, home: Path
) -> None:
    """The exposure is a key in plaintext in the project directory, and naming it is the warning.

    Naming it is also the whole of what may be said: the value is a live credential and there is
    no reading of "show the user their exposure" that involves putting it in an HTTP response a
    browser will cache. The API hands back dotted names for that reason, and this asserts the
    route did not go looking for anything else.
    """
    client.post("/api/hub/workbooks", json={"path": str(workbook)})
    workspace = Workspace.for_workbook(workbook, user_directory=home)
    _marimo_toml(
        workspace,
        '[ai]\nenabled = false\n\n[ai.open_ai]\napi_key = "sk-not-a-real-key"\n\n'
        "[completion]\ncopilot = false\n",
    )

    body = client.get("/api/hub/state")

    assert body.json()["workbooks"][0]["assistant_keys"] == ["ai.open_ai.api_key"]
    assert "sk-not-a-real-key" not in body.text, "a credential must never reach the browser"


def test_the_hub_reports_whether_marimos_own_assistant_is_disabled(
    client: TestClient, workbook: Path, home: Path
) -> None:
    """Read on every sweep, not captured at open, and that is the point of the API.

    marimo rewrites that file whenever a setting changes in its own editor, so the assistant can
    be switched back on an hour after a launch that reported the notebook clean. The hub polls, so
    what a card shows has to be what the file says now.
    """
    client.post("/api/hub/workbooks", json={"path": str(workbook)})
    workspace = Workspace.for_workbook(workbook, user_directory=home)
    _marimo_toml(workspace, "[ai]\nenabled = false\n\n[completion]\ncopilot = false\n")

    locked = client.get("/api/hub/state").json()["workbooks"][0]
    _marimo_toml(workspace, "[ai]\nenabled = true\n\n[completion]\ncopilot = false\n")
    live = client.get("/api/hub/state").json()["workbooks"][0]

    assert locked["assistant_enforced"] is True
    assert live["assistant_enforced"] is False
    assert live["assistant_keys"] == []


def test_the_launching_step_says_when_marimos_assistant_is_still_live(
    tmp_path: Path, home: Path
) -> None:
    """The sentence that replaces the log line nobody reads.

    A `.marimo.toml` kedge could not write leaves marimo's assistant live, which puts everything
    it sends outside kedge's tool surface and outside the outbound payload log. The launch is
    deliberately not refused over it -- declining to start un-writes nothing and removes the only
    route to the panel that would fix it -- so the whole of the control is that the user is told.
    """
    from kedge.server.hub import _assistant_note

    workbook = _make_workbook(tmp_path / "processes" / "rwa_monthly.xlsx")
    workspace = Workspace.for_workbook(workbook, user_directory=home)
    _marimo_toml(workspace, '[ai]\nenabled = true\n\n[ai.anthropic]\napi_key = "sk-ant-nope"\n')

    note = _assistant_note(workspace)

    assert "built-in AI assistant is" in note
    assert "outbound log" in note
    assert "ai.anthropic.api_key" in note
    assert "sk-ant-nope" not in note, "the name is the warning; the value is the exposure"


def test_the_launching_step_stays_quiet_when_the_assistant_is_off_and_nothing_is_stored(
    tmp_path: Path, home: Path
) -> None:
    """A warning on every successful open is a warning nobody reads by the third one."""
    from kedge.server.hub import _assistant_note

    workbook = _make_workbook(tmp_path / "processes" / "rwa_monthly.xlsx")
    workspace = Workspace.for_workbook(workbook, user_directory=home)
    _marimo_toml(workspace, "[ai]\nenabled = false\n\n[completion]\ncopilot = false\n")

    assert _assistant_note(workspace) == ""


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


class _MemoryDriver:
    """A notebook held in a dict: enough of the driver for a real `sync_notebook` to run against.

    `_StubDriver` above is deliberately smaller — it exists so the plan gate is the only thing
    under test, and it never reaches a sync. This one has to be a notebook, because the number the
    scaffold step now reports is a fact about what is in one.
    """

    def __init__(self, cells: dict[str, str] | None = None) -> None:
        self.cells: dict[str, str] = dict(cells or {})
        self.listings = 0

    async def list_cells(self, *, with_code: bool = True):
        from kedge.notebook.model import CellInfo

        self.listings += 1
        return tuple(
            CellInfo(id=f"C{index}", name=name, code=code if with_code else None)
            for index, (name, code) in enumerate(self.cells.items())
        )

    async def create_cell(self, code: str, *, name: str, **_kw: object):
        from kedge.notebook.model import CellRef, MutationResult

        self.cells[name] = code
        return MutationResult(
            operation="create_cell", cell=CellRef(id=name, name=name), ran=True, status="idle"
        )

    async def edit_cell(self, target: str, code: str, **_kw: object):
        from kedge.notebook.model import CellRef, MutationResult

        self.cells[target] = code
        return MutationResult(
            operation="edit_cell", cell=CellRef(id=target, name=target), ran=True, status="idle"
        )


def _scaffold_of(plan: object, workspace: Workspace) -> dict[str, str]:
    """The notebook a first open would leave behind, keyed by cell name.

    Built with the same three paths `_step_scaffold` passes, so a reopen against it reports its
    cells as unchanged rather than as seventeen divergences.
    """
    from kedge.notebook.scaffold import build_cells

    return {
        cell.name: cell.code
        for cell in build_cells(
            plan,
            handins_dir=workspace.handins_dir,
            workbook_path=workspace.workbook_path,
            contract_path=workspace.contract_path,
        )
    }


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


async def test_the_scaffold_step_says_how_many_cells_are_still_to_write(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scaffolded notebook runs, so nothing on screen said the conversion was unfinished.

    The stage bodies are passthroughs on purpose — the hand-in machinery is meant to be
    exercisable the moment a plan is approved — so the page opens, renders and steps through
    exactly as a finished conversion does. "17 written" was true and told the user nothing.
    """
    from conftest import make_approved_plan
    from kedge.server.hub import _step_scaffold

    workspace = _bare_workspace(tmp_path, home)
    driver = _MemoryDriver()
    monkeypatch.setattr(
        "kedge.notebook.driver.NotebookDriver.for_workspace", lambda _workspace: driver
    )
    plan = make_approved_plan()
    job = OpenJob(job_id="fresh", workbook=str(workspace.workbook_path))

    await _step_scaffold(workspace, plan, job)

    detail = job.frames[-1].detail
    assert job.frames[-1].state == "ok"
    assert "17 cells scaffolded, 4 still to write" in detail
    assert "TODO(kedge)" in detail, "the user needs the marker to search the notebook for"


async def test_the_cells_still_to_write_are_counted_off_the_notebook_not_the_scaffold(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This step runs on every open, and only the first one finds an empty notebook.

    Counted off a fresh `build_cells` the number would be four for ever, including on the open
    after the last hole was filled — a progress report that never moves is one nobody reads.
    """
    from conftest import make_approved_plan
    from kedge.server.hub import _step_scaffold

    workspace = _bare_workspace(tmp_path, home)
    plan = make_approved_plan()
    written = _scaffold_of(plan, workspace)
    written["apply_haircuts"] = "apply_haircuts = load_handin.with_columns(haircut=0.1)\n"
    driver = _MemoryDriver(written)
    monkeypatch.setattr(
        "kedge.notebook.driver.NotebookDriver.for_workspace", lambda _workspace: driver
    )
    job = OpenJob(job_id="reopen", workbook=str(workspace.workbook_path))

    await _step_scaffold(workspace, plan, job)

    detail = job.frames[-1].detail
    assert "17 cells scaffolded, 3 still to write" in detail
    assert "4 still to write" not in detail
    assert driver.cells["apply_haircuts"].startswith("apply_haircuts = load_handin"), (
        "the translated cell must still be the user's, not the scaffold's"
    )


async def test_a_conversion_with_no_holes_left_is_said_so_rather_than_left_silent(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero is the one number worth saying out loud: it is the end of the conversion."""
    from conftest import make_approved_plan
    from kedge.notebook.scaffold import is_unwritten
    from kedge.server.hub import _step_scaffold

    workspace = _bare_workspace(tmp_path, home)
    plan = make_approved_plan()
    written = {
        name: (f"{name} = 1\n" if is_unwritten(code) else code)
        for name, code in _scaffold_of(plan, workspace).items()
    }
    monkeypatch.setattr(
        "kedge.notebook.driver.NotebookDriver.for_workspace",
        lambda _workspace: _MemoryDriver(written),
    )
    job = OpenJob(job_id="finished", workbook=str(workspace.workbook_path))

    await _step_scaffold(workspace, plan, job)

    assert "All 17 scaffolded cells have been written." in job.frames[-1].detail
    assert "still to write" not in job.frames[-1].detail


async def test_a_notebook_that_will_not_list_costs_the_count_rather_than_the_step(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wrong count is worse than no count on a step whose job is to say what is unfinished.

    The sync itself succeeded, so the step is `ok` and says what it wrote. It just declines to
    guess at the half it could not read.
    """
    from conftest import make_approved_plan
    from kedge.errors import NotebookError
    from kedge.server.hub import _step_scaffold

    class _ListsOnce(_MemoryDriver):
        async def list_cells(self, *, with_code: bool = True):
            if self.listings:
                msg = "the kernel stopped answering"
                raise NotebookError(msg)
            return await super().list_cells(with_code=with_code)

    driver = _ListsOnce()
    monkeypatch.setattr(
        "kedge.notebook.driver.NotebookDriver.for_workspace", lambda _workspace: driver
    )
    workspace = _bare_workspace(tmp_path, home)
    job = OpenJob(job_id="halfblind", workbook=str(workspace.workbook_path))

    await _step_scaffold(workspace, make_approved_plan(), job)

    assert job.frames[-1].state == "ok"
    assert "17 written" in job.frames[-1].detail
    assert "still to write" not in job.frames[-1].detail
    assert "scaffolded cells have been written" not in job.frames[-1].detail


async def test_the_placeholder_notebook_leaves_the_scaffold_a_name_of_its_own(
    tmp_path: Path, home: Path
) -> None:
    """The placeholder used to define ``mo``, and ``kedge_setup`` defines ``mo``.

    So on every fresh workbook the notebook's whole preamble -- ``pl``, ``kedge.xl``,
    ``kedge.sql``, ``kedge.runs``, ``WORKBOOK``, ``HANDIN_DIR``, ``CONTRACT_PATH``, ``RUNS_DIR``,
    ``ACCEPTANCE_PATH`` -- was refused for a duplicate definition, and the sync went on to write
    every stage beneath it against names that were never bound. In app mode that is a page that
    just ends.

    Asserted through ``multiply_defined`` over the file the two steps actually produce, not
    against the placeholder's text: a test that checked the constant has no ``@app.cell`` in it
    would go green again the moment somebody adds a placeholder that clashes on a different name.
    """
    from conftest import make_approved_plan
    from kedge.notebook.codegen import analyse_document, multiply_defined, read_notebook
    from kedge.notebook.filedriver import FileNotebookDriver
    from kedge.notebook.scaffold import sync_notebook
    from kedge.server.hub import _step_notebook

    workspace = _bare_workspace(tmp_path, home)
    job = OpenJob(job_id="placeholder", workbook=str(workspace.workbook_path))
    await _step_notebook(workspace, job)

    plan = make_approved_plan()
    async with FileNotebookDriver.for_workspace(workspace) as driver:
        result = await sync_notebook(
            plan,
            driver,
            handins_dir=workspace.handins_dir,
            workbook_path=workspace.workbook_path,
            contract_path=workspace.contract_path,
        )

    assert result.named("refused") == (), "the placeholder must not cost the scaffold a cell"
    assert "kedge_setup" in result.named("created")
    analyses, _ = analyse_document(read_notebook(workspace.notebook_path))
    assert multiply_defined(analyses) == {}, "two cells define the same name"


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
