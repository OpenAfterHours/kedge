"""Tests for the command line: argument surface, output, and the milestone shims."""

from __future__ import annotations

import importlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kedge import cli
from kedge import config as config_module
from kedge.cli import app
from kedge.notebook import BridgeReport
from kedge.reconcile import ReconciliationStatus

SECRET = "sk-must-never-be-printed-0123456789"

runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

WATCH_MODULE = importlib.import_module("kedge.ingest.watch")
"""The watched-folder module, fetched the long way round.

``kedge.ingest`` re-exports the ``watch`` *function*, which shadows the submodule of the same
name -- so ``import kedge.ingest.watch as m``, and monkeypatch's dotted string form, both bind
the function instead of the module. Patching a name inside the module needs the module itself.
"""


def unstyled(output: str) -> str:
    """Rendered output with the escape sequences taken out.

    Anything asserting on a *token* in rich-rendered output has to go through this. rich splits
    a styled run wherever its highlighter says to, and for an option it emits
    ``\\x1b[1;36m-\\x1b[0m\\x1b[1;36m--out\\x1b[0m`` -- so ``"--out" in result.output`` is false
    while the text on screen plainly reads ``--out``. Whether that happens depends on whether
    rich thinks colour is available, which is why this passes on a developer's machine and fails
    under CI, where ``FORCE_COLOR`` is set.
    """
    return _ANSI.sub("", output)


def flattened(output: str) -> str:
    """Unstyled output with every run of whitespace collapsed to one space.

    For asserting on a *phrase*. rich word-wraps at the console width, so a sentence that reads
    ``the watched folder ... does not exist`` on screen may carry a newline in the middle of it,
    and the length of a `tmp_path` decides where. Collapsing the whitespace makes the assertion
    about the message rather than about the terminal.
    """
    return " ".join(unstyled(output).split())


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every CLI test runs against a temporary ~/.kedge and a temporary working directory."""
    home = tmp_path / "kedge-home"
    home.mkdir()
    monkeypatch.setenv("KEDGE_HOME", str(home))
    working = tmp_path / "cwd"
    working.mkdir()
    monkeypatch.chdir(working)
    return home


@pytest.fixture(autouse=True)
def no_real_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real Windows Credential Manager is never consulted by a test."""
    monkeypatch.setattr(config_module.keyring, "get_password", lambda service, username: None)


@pytest.fixture
def workbook(tmp_path: Path) -> Path:
    path = tmp_path / "cwd" / "process.xlsx"
    path.write_bytes(b"")
    return path


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    """The folder another team drops hand-ins into."""
    directory = tmp_path / "inbox"
    directory.mkdir()
    return directory


def moved_bridge() -> BridgeReport:
    """A report from a marimo whose private API has shifted under kedge."""
    return BridgeReport(
        version="0.24.0",
        pinned=cli.MARIMO_PIN,
        problems=("AsyncCodeModeContext.edit_cell() no longer accepts hide_code",),
    )


def pretend_bridge(monkeypatch: pytest.MonkeyPatch, report: BridgeReport) -> None:
    """Point every route to the preflight at one fixed report.

    ``kedge.notebook`` re-exports ``check_bridge``, and ``verify_bridge`` calls the copy in
    ``kedge.notebook.driver``. Patching one and not the other proves nothing: `doctor` would
    read the fake report and `open` the real one.
    """
    monkeypatch.setattr("kedge.notebook.check_bridge", lambda: report)
    monkeypatch.setattr("kedge.notebook.driver.check_bridge", lambda: report)


# ── help and version ─────────────────────────────────────────────────────────────────────────


def test_help_lists_every_command() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in ("open", "hub", "inspect", "reconcile", "watch", "contract", "config", "doctor"):
        assert command in result.output


def test_no_arguments_shows_help_rather_than_failing_silently() -> None:
    result = runner.invoke(app, [])

    assert "Usage" in result.output


def test_version_reports_the_package_and_the_marimo_pin() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert "kedge" in result.output
    assert "0.23.15" in result.output


@pytest.mark.parametrize(
    "command", ["open", "hub", "inspect", "reconcile", "watch", "config", "doctor"]
)
def test_each_command_has_help(command: str) -> None:
    result = runner.invoke(app, [command, "--help"])

    assert result.exit_code == 0


def test_contract_infer_is_reachable() -> None:
    result = runner.invoke(app, ["contract", "infer", "--help"])

    assert result.exit_code == 0
    assert "--out" in unstyled(result.output)


# ── config ───────────────────────────────────────────────────────────────────────────────────


def test_config_shows_defaults_when_no_files_exist() -> None:
    result = runner.invoke(app, ["config"])

    assert result.exit_code == 0
    assert "sampling.max_rows" in result.output
    assert "reconciliation.absolute_tolerance" in result.output


def test_config_names_the_file_each_value_came_from(isolated_home: Path, tmp_path: Path) -> None:
    (isolated_home / "config.toml").write_text("[sampling]\ntop_k = 3\n", encoding="utf-8")
    (tmp_path / "cwd" / "kedge.toml").write_text("[sampling]\nmax_rows = 25\n", encoding="utf-8")

    result = runner.invoke(app, ["config", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["values"]["sampling.max_rows"]["value"] == 25
    assert payload["values"]["sampling.max_rows"]["source"].endswith("kedge.toml")
    assert payload["values"]["sampling.top_k"]["source"].endswith("config.toml")
    assert payload["values"]["sampling.head_rows"]["source"] == "default"


def test_config_never_prints_the_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module.keyring, "get_password", lambda service, username: SECRET)

    plain = runner.invoke(app, ["config"])
    machine = runner.invoke(app, ["config", "--json"])

    # Through unstyled(): a secret broken across two style spans is still a leaked secret, and
    # asserting on the raw output would let exactly that through.
    assert SECRET not in unstyled(plain.output)
    assert SECRET not in unstyled(machine.output)
    assert json.loads(machine.output)["api_key"]["status"] == "present"


def test_config_tells_you_how_to_set_a_missing_key() -> None:
    result = runner.invoke(app, ["config"])

    assert "uv run keyring set kedge" in result.output


def test_config_reports_a_broken_config_file_clearly(tmp_path: Path) -> None:
    (tmp_path / "cwd" / "kedge.toml").write_text(
        "[sampling]\nmax_rows = 'lots'\n", encoding="utf-8"
    )

    result = runner.invoke(app, ["config"])

    assert result.exit_code == 1
    assert "sampling.max_rows" in result.output
    assert "kedge.toml" in result.output


# ── doctor ───────────────────────────────────────────────────────────────────────────────────


def test_doctor_runs_and_reports_each_check() -> None:
    result = runner.invoke(app, ["doctor", "--no-network"])

    assert result.exit_code == 0
    for check in ("python", "marimo", "marimo bridge", "config", "keyring", "marker files"):
        assert check in result.output


def test_doctor_json_is_machine_readable() -> None:
    result = runner.invoke(app, ["doctor", "--no-network", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    names = {check["check"] for check in payload["checks"]}
    assert {
        "python",
        "marimo",
        "marimo bridge",
        "config",
        "keyring",
        "user directory",
        "marker files",
    } <= names


def test_doctor_warns_about_a_missing_keyring_entry() -> None:
    result = runner.invoke(app, ["doctor", "--no-network", "--json"])

    keyring_check = next(c for c in json.loads(result.output)["checks"] if c["check"] == "keyring")
    assert keyring_check["status"] == "warn"
    assert "uv run keyring set kedge" in keyring_check["detail"]


def test_doctor_notices_a_stale_marker(isolated_home: Path, workbook: Path) -> None:
    """A marker whose port answers nothing is a leftover from a crashed run."""
    from kedge.config import Config, LoadedConfig
    from kedge.workspace import Workspace

    workspace = Workspace.for_workbook(workbook, loaded_config=LoadedConfig(config=Config()))
    workspace.ensure_dirs()
    workspace.attach_marimo(host="127.0.0.1", port=1, token="t", pid=999_999)
    workspace.write_marker(kedge_version="0.1.0")

    result = runner.invoke(app, ["doctor", "--no-network", "--json"])

    markers = next(c for c in json.loads(result.output)["checks"] if c["check"] == "marker files")
    assert markers["status"] == "warn"
    assert "stale" in markers["detail"]


def test_doctor_fails_when_the_marimo_pin_does_not_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "installed_marimo_version", lambda: "0.23.9")

    result = runner.invoke(app, ["doctor", "--no-network", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    marimo_check = next(c for c in payload["checks"] if c["check"] == "marimo")
    assert marimo_check["status"] == "fail"
    assert "0.23.15" in marimo_check["detail"]


def test_doctor_introspects_the_private_marimo_api_the_bridge_depends_on() -> None:
    """PLAN 6.1 mitigation 5, run rather than merely built.

    The `marimo` check above compares two version strings. This one imports ``_code_mode`` and
    looks at it, which is the only thing that catches a surface that moved without the version
    kedge pins moving with it.
    """
    result = runner.invoke(app, ["doctor", "--no-network", "--json"])

    bridge = next(c for c in json.loads(result.output)["checks"] if c["check"] == "marimo bridge")
    assert bridge["status"] == "ok"
    assert cli.MARIMO_PIN in bridge["detail"]


def test_doctor_fails_and_names_the_marimo_version_when_the_bridge_has_moved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "marimo changed" is not actionable; "edit_cell() lost hide_code in 0.24.0" is."""
    pretend_bridge(monkeypatch, moved_bridge())

    result = runner.invoke(app, ["doctor", "--no-network", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False
    bridge = next(c for c in payload["checks"] if c["check"] == "marimo bridge")
    assert bridge["status"] == "fail"
    assert "0.24.0" in bridge["detail"]
    assert "edit_cell" in bridge["detail"]


def test_doctor_only_warns_when_an_unpinned_marimo_still_has_the_shape_kedge_drives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A version kedge has not verified against is worth saying; it is not a broken install."""
    pretend_bridge(monkeypatch, BridgeReport(version="0.24.0", pinned=cli.MARIMO_PIN))

    result = runner.invoke(app, ["doctor", "--no-network", "--json"])

    assert result.exit_code == 0
    bridge = next(c for c in json.loads(result.output)["checks"] if c["check"] == "marimo bridge")
    assert bridge["status"] == "warn"
    assert "0.24.0" in bridge["detail"]


def test_doctor_reports_a_workspace_for_a_named_workbook(workbook: Path) -> None:
    result = runner.invoke(app, ["doctor", "--no-network", "--json", "--workbook", str(workbook)])

    workspace_check = next(
        c for c in json.loads(result.output)["checks"] if c["check"] == "workspace"
    )
    assert workspace_check["status"] == "ok"
    assert "process.kedge" in workspace_check["detail"]
    assert re.search(r"session id s_[\da-z]{6}", workspace_check["detail"])


# ── argument validation ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "argv",
    [
        ["open", "absent.xlsx"],
        ["inspect", "absent.xlsx"],
        ["watch", "absent.xlsx"],
        ["contract", "infer", "absent.csv"],
    ],
)
def test_a_missing_input_file_is_reported_before_anything_else_happens(argv: list[str]) -> None:
    result = runner.invoke(app, argv)

    assert result.exit_code == 1
    assert "no such" in result.output


def test_reconcile_checks_both_of_its_inputs(workbook: Path) -> None:
    result = runner.invoke(app, ["reconcile", "absent.py", str(workbook)])

    assert result.exit_code == 1
    assert "no such file" in result.output


# ── milestone shims ──────────────────────────────────────────────────────────────────────────


def test_inspect_actually_analyses_now_that_m1_has_landed(workbook: Path, tmp_path: Path) -> None:
    """`inspect` is wired all the way through: no shim, a real analysis on disk."""
    destination = tmp_path / "analysis.json"
    result = runner.invoke(app, ["inspect", str(workbook), "--out", str(destination)])

    assert result.exception is None, result.output
    assert destination.is_file()
    analysis = json.loads(destination.read_text(encoding="utf-8"))
    assert analysis["workbook"]["filename"] == workbook.name
    assert analysis["schema_version"]


def test_inspect_writes_a_self_contained_report(workbook: Path, tmp_path: Path) -> None:
    report = tmp_path / "report.html"
    result = runner.invoke(app, ["inspect", str(workbook), "--report", str(report)])

    assert result.exception is None, result.output
    html = report.read_text(encoding="utf-8")
    # A report on someone's workbook must not fetch anything when opened.
    assert "https://" not in html
    assert "<title>" in html


def test_a_command_whose_milestone_has_not_landed_names_the_owing_module() -> None:
    """Nothing silently no-ops; an unimplemented command says whose it is.

    Asserted against `_resolve` itself rather than against whichever command still has a shim.
    Every command this test used to reach through has since been implemented -- which is the
    point of the shim, and also how the test kept being rewritten. The mechanism is the durable
    subject: it must name the module, the milestone and the file that owes the work.
    """
    with pytest.raises(NotImplementedError) as caught:
        cli._resolve("kedge.contracts.infer", "infer_the_whole_thing", "M5 (contracts)")

    message = str(caught.value)
    assert "kedge.contracts.infer.infer_the_whole_thing" in message
    assert "M5 (contracts)" in message
    assert "src/kedge/contracts/infer.py" in message


def test_a_shim_for_a_module_that_does_not_exist_says_so_the_same_way() -> None:
    """The other half: an absent module, not merely an absent attribute in a present one."""
    with pytest.raises(NotImplementedError, match="M9 \\(not a milestone\\)"):
        cli._resolve("kedge.does_not_exist", "anything", "M9 (not a milestone)")


def test_reconcile_never_reports_passed_when_it_could_not_run(
    workbook: Path, tmp_path: Path
) -> None:
    """The most dangerous failure mode in the project, pinned at the CLI boundary.

    An empty notebook produces no values to compare. That is "not reconciled", and it must exit
    non-zero so the command is usable as a gate -- a vacuous pass would be worse than a crash
    (PLAN 4.5).
    """
    notebook = tmp_path / "cwd" / "process.py"
    notebook.parent.mkdir(parents=True, exist_ok=True)
    notebook.write_text("", encoding="utf-8")

    result = runner.invoke(app, ["reconcile", str(notebook), str(workbook), "--json"])

    assert result.exit_code != 0, "a reconciliation that could not run must not exit 0"
    report = json.loads(result.stdout)
    statuses = {region["status"] for region in report["regions"]}
    assert "passed" not in statuses


def test_contract_infer_writes_a_commented_draft_that_validates_its_own_source(
    tmp_path: Path,
) -> None:
    """The whole point of a draft: it passes against the file it was drafted from.

    Regression, and a sharp one. The command resolved `kedge.contracts.infer.infer_contract`,
    which has never existed -- the module exports `infer`, `infer_with_notes` and `write_yaml`
    -- so every invocation died with `NotImplementedError`. The scaffolded notebook tells the
    user to run this exact command when it finds no contract, so a broken command made that
    guidance a dead end.
    """
    handin = tmp_path / "cwd" / "handin.csv"
    handin.parent.mkdir(parents=True, exist_ok=True)
    handin.write_text("id,amount\n1,10.5\n2,20.25\n", encoding="utf-8")
    destination = tmp_path / "contract.yaml"

    result = runner.invoke(app, ["contract", "infer", str(handin), "--out", str(destination)])

    assert result.exit_code == 0, result.output
    assert destination.is_file()
    body = destination.read_text(encoding="utf-8")
    assert "# Contract:" in body  # the commentary, not just the model
    assert "id" in body and "amount" in body

    from kedge.contracts import load
    from kedge.contracts.validate import validate_frame
    from kedge.ingest import read_data

    contract = load(destination)
    frame, layout = read_data(handin, sheet=contract.sheet, header_row=contract.header_row)
    report = validate_frame(frame, contract, handin_name=handin.name, layout=layout)
    assert report.ok, report.summary_line()


def test_contract_infer_defaults_its_output_beside_the_hand_in(tmp_path: Path) -> None:
    handin = tmp_path / "cwd" / "handin.csv"
    handin.parent.mkdir(parents=True, exist_ok=True)
    handin.write_text("id,amount\n1,10.5\n", encoding="utf-8")

    result = runner.invoke(app, ["contract", "infer", str(handin)])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "cwd" / "handin.contract.yaml").is_file()


def test_contract_infer_reports_a_file_it_cannot_describe_rather_than_tracebacking(
    tmp_path: Path,
) -> None:
    """A file with no columns is a `_fail`, not a stack trace: it names what went wrong."""
    handin = tmp_path / "cwd" / "empty.csv"
    handin.parent.mkdir(parents=True, exist_ok=True)
    handin.write_text("", encoding="utf-8")

    result = runner.invoke(app, ["contract", "infer", str(handin)])

    assert result.exit_code == 1
    assert "error" in flattened(result.output)
    assert not (tmp_path / "cwd" / "empty.contract.yaml").exists()


def test_open_runs_the_same_sequence_the_hub_runs(
    workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`open` and the hub must not keep separate copies of the open sequence.

    Ordering in that sequence is not cosmetic: scaffolding needs a live driver, so it cannot
    precede the marimo launch. `open` used to have its own copy with the steps in the wrong order
    and raised a TypeError before it ever launched marimo. It now delegates.
    """
    called: dict[str, object] = {}

    async def _capture(state, path, **kwargs):
        called["path"] = Path(path)
        called["reattach"] = kwargs.get("reattach")
        called["workspace"] = kwargs.get("workspace")
        raise SystemExit(0)

    monkeypatch.setattr("kedge.server.hub.open_workbook", _capture)
    runner.invoke(app, ["open", str(workbook), "--no-browser"])

    assert called["path"] == workbook
    assert called["reattach"] is True, "a crashed previous run is the common case on the CLI"


def test_the_port_option_actually_pins_the_marimo_port(
    workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An accepted flag that is only printed is worse than no flag at all."""
    seen: dict[str, int] = {}

    async def _capture(_state, _path, **kwargs):
        workspace = kwargs["workspace"]
        seen["port"] = workspace.config.marimo.port
        raise SystemExit(0)

    monkeypatch.setattr("kedge.server.hub.open_workbook", _capture)

    runner.invoke(app, ["open", str(workbook), "--port", "2718", "--no-browser"])

    assert seen["port"] == 2718, "the pinned port must reach the workspace marimo is launched from"


# ── hub ──────────────────────────────────────────────────────────────────────────────────────


def test_hub_starts_the_server_with_no_workbook_and_opens_the_browser_at_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`kedge hub` differs from `kedge open` in exactly one thing: nothing is attached."""
    served: dict[str, object] = {}

    def _capture(app_: object, **kwargs: object) -> None:
        served["state"] = app_.state.kedge
        served["port"] = kwargs.get("port")

    monkeypatch.setattr("kedge.server.app.run_server", _capture)
    monkeypatch.setattr("webbrowser.open", lambda url: served.setdefault("url", url))

    result = runner.invoke(app, ["hub", "--port", "8731"])

    assert result.exit_code == 0
    assert served["port"] == 8731
    assert served["url"] == "http://127.0.0.1:8731"
    assert served["state"].workspace is None
    assert served["state"].agent is None


def test_hub_can_be_told_not_to_open_a_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr("kedge.server.app.run_server", lambda *_a, **_k: None)
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))

    result = runner.invoke(app, ["hub", "--no-browser"])

    assert result.exit_code == 0
    assert opened == []


def test_hub_reports_how_many_workbooks_it_knows_about(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kedge.server.app.run_server", lambda *_a, **_k: None)

    result = runner.invoke(app, ["hub", "--no-browser"])

    assert "no workbooks registered yet" in result.output


def test_opening_a_workbook_registers_it_so_the_hub_and_the_terminal_agree(
    isolated_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import zipfile

    from kedge.registry import WorkbookRegistry

    path = tmp_path / "cwd" / "real.xlsx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")

    async def _stop(*_args, **_kwargs):
        raise SystemExit(0)

    monkeypatch.setattr("kedge.server.hub._run_open", _stop)
    runner.invoke(app, ["open", str(path), "--no-browser"])

    assert [entry.name for entry in WorkbookRegistry.for_user().entries()] == ["real.xlsx"]


def test_a_workbook_that_will_not_register_does_not_stop_open(
    isolated_home: Path, workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `workbook` fixture is an empty file, which is not a real workbook. `open` still runs:
    the user has already said which file they mean."""
    from kedge.registry import WorkbookRegistry

    reached: list[str] = []

    async def _capture(*_args, **_kwargs):
        reached.append("sequence started")
        raise SystemExit(0)

    monkeypatch.setattr("kedge.server.hub._run_open", _capture)
    runner.invoke(app, ["open", str(workbook), "--no-browser"])

    assert reached == ["sequence started"], "registration failing must not abort the open"
    assert WorkbookRegistry.for_user().entries() == []


def test_a_project_config_beside_the_workbook_is_picked_up(workbook: Path) -> None:
    """kedge.toml is hand-written, so it lives beside the workbook, not in the generated dir."""
    (workbook.parent / "kedge.toml").write_text("[sampling]\nmax_rows = 7\n", encoding="utf-8")

    result = runner.invoke(app, ["config", "--json", "--workbook", str(workbook)])

    payload = json.loads(result.output)
    assert payload["values"]["sampling.max_rows"]["value"] == 7


# ── the bridge preflight ─────────────────────────────────────────────────────────────────────


def test_open_refuses_before_it_spawns_anything_when_the_bridge_has_moved(
    workbook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PLAN 6.1 mitigation 5, at the entry point it was written for.

    Without this the same mismatch surfaces as a TypeError from inside a tool call, after the
    analysis, the scaffold, a spawned marimo and several minutes of conversation.
    """
    pretend_bridge(monkeypatch, moved_bridge())
    reached: list[str] = []

    async def _capture(*_args, **_kwargs):
        reached.append("open sequence started")
        raise SystemExit(0)

    monkeypatch.setattr("kedge.server.hub.open_workbook", _capture)

    result = runner.invoke(app, ["open", str(workbook), "--no-browser"])

    assert result.exit_code == 1
    assert reached == [], "nothing may be spawned once the preflight has failed"
    output = flattened(result.output)
    assert "0.24.0" in output, "the error has to name the marimo that is actually installed"
    assert cli.MARIMO_PIN in output


def test_hub_refuses_before_it_starts_the_server_when_the_bridge_has_moved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hub spawns marimo too -- from the page rather than the terminal, but just as far in."""
    pretend_bridge(monkeypatch, moved_bridge())
    served: list[str] = []
    monkeypatch.setattr("kedge.server.app.run_server", lambda *_a, **_k: served.append("served"))

    result = runner.invoke(app, ["hub", "--no-browser"])

    assert result.exit_code == 1
    assert served == []
    assert "0.24.0" in flattened(result.output)


# ── watch ────────────────────────────────────────────────────────────────────────────────────


class _StubFolder:
    """A started watcher that never sees a file, and optionally takes a Ctrl-C while waiting."""

    def __init__(self, *, interrupt: bool = False) -> None:
        self._interrupt = interrupt
        self.stopped = False

    def wait(self, *, timeout_seconds: float | None = None) -> bool:
        del timeout_seconds
        if self._interrupt:
            raise KeyboardInterrupt
        return True

    def stop(self) -> None:
        self.stopped = True


def test_watch_says_which_setting_to_fill_in_when_no_folder_is_configured(workbook: Path) -> None:
    """``ingest.watch_dir`` is optional, so an unset one is a message rather than a traceback."""
    result = runner.invoke(app, ["watch", str(workbook)])

    assert result.exit_code == 1
    output = flattened(result.output)
    assert "ingest.watch_dir" in output
    assert "--dir" in output


def test_watch_names_the_folder_that_is_not_there(workbook: Path, tmp_path: Path) -> None:
    argv = ["watch", str(workbook), "--dir", str(tmp_path / "not-there"), "--once"]

    result = runner.invoke(app, argv)

    assert result.exit_code == 1
    assert "does not exist" in flattened(result.output)


def test_watch_once_receives_what_is_already_sitting_in_the_folder(
    workbook: Path, inbox: Path
) -> None:
    """The sweep with no filesystem watcher at all: what a scheduled task runs (PLAN 2.8)."""
    (inbox / "exposures.xlsx").write_bytes(b"a,b\n1,2\n")

    result = runner.invoke(
        app, ["watch", str(workbook), "--dir", str(inbox), "--once", "--settle", "0"]
    )

    assert result.exit_code == 0, result.output
    output = flattened(result.output)
    assert "1 hand-in(s) received" in output
    assert "watched" in output and "exposures.xlsx" in output, "the audit line is the point"
    copies = list(workbook.parent.rglob("*exposures.xlsx"))
    assert len(copies) == 1, "the hand-in must be copied into the managed store"
    assert "handins" in copies[0].parts


def test_watch_once_twice_over_does_not_receive_the_same_hand_in_again(
    workbook: Path, inbox: Path
) -> None:
    """Idempotence is what makes an hourly scheduled sweep safe to leave running."""
    (inbox / "exposures.xlsx").write_bytes(b"a,b\n1,2\n")
    argv = ["watch", str(workbook), "--dir", str(inbox), "--once", "--settle", "0"]

    first = runner.invoke(app, argv)
    second = runner.invoke(app, argv)

    assert "1 hand-in(s) received" in flattened(first.output)
    assert "0 hand-in(s) received" in flattened(second.output)


def test_watch_takes_the_folder_and_the_pattern_from_the_project_config(
    workbook: Path, inbox: Path
) -> None:
    """A watched folder is a property of the process, so it belongs in kedge.toml, not a flag."""
    (workbook.parent / "kedge.toml").write_text(
        f'[ingest]\nwatch_dir = "{inbox.as_posix()}"\nwatch_glob = "*.csv"\n', encoding="utf-8"
    )
    (inbox / "exposures.csv").write_bytes(b"a,b\n1,2\n")
    (inbox / "ignored.xlsx").write_bytes(b"a,b\n3,4\n")

    result = runner.invoke(app, ["watch", str(workbook), "--once", "--settle", "0"])

    assert result.exit_code == 0, result.output
    output = flattened(result.output)
    assert "exposures.csv" in output
    assert "ignored.xlsx" not in output


def test_a_relative_watch_dir_is_relative_to_the_workbook_not_to_the_shell(
    workbook: Path, inbox: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scheduled sweep starts in whatever directory the scheduler chose, and must still work.

    ``ingest.watch_dir = "inbox"`` is exactly what the "set it in a kedge.toml beside the
    workbook" message invites, and resolving it against the process working directory made the
    setting work from the project folder and quietly find nothing from anywhere else.
    """
    beside_the_workbook = workbook.parent / "inbox"
    beside_the_workbook.mkdir()
    (beside_the_workbook / "exposures.xlsx").write_bytes(b"a,b\n1,2\n")
    (workbook.parent / "kedge.toml").write_text('[ingest]\nwatch_dir = "inbox"\n', encoding="utf-8")
    # `inbox` is a decoy of the same name in the directory the command is run from, and empty.
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["watch", str(workbook), "--once", "--settle", "0"])

    assert result.exit_code == 0, result.output
    assert "1 hand-in(s) received" in flattened(result.output)
    assert not list(inbox.iterdir()), "the folder beside the shell must not have been touched"


def test_a_relative_dir_flag_is_relative_to_the_shell(
    workbook: Path, inbox: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the rule: a flag means what it means where it was typed."""
    (inbox / "exposures.xlsx").write_bytes(b"a,b\n1,2\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app, ["watch", str(workbook), "--dir", "inbox", "--once", "--settle", "0"]
    )

    assert result.exit_code == 0, result.output
    assert "1 hand-in(s) received" in flattened(result.output)


def test_watch_hands_the_configured_store_and_pattern_to_the_watcher(
    workbook: Path, inbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The event-driven path is a thin wrapper, and it has to be given the same settings.

    Including the two booleans: with both defaulting to True in ``watch()`` itself, dropping
    them here would leave the config quietly ignored on this path and every test still green.
    """
    (workbook.parent / "kedge.toml").write_text(
        "[ingest]\ncopy_on_select = false\ndedupe_by_hash = false\n", encoding="utf-8"
    )
    captured: dict[str, object] = {}

    def _start(directory: Path, _on_handin: object, **kwargs: object) -> object:
        captured["directory"] = directory
        captured.update(kwargs)
        return _StubFolder()

    monkeypatch.setattr(WATCH_MODULE, "watch", _start)
    runner.invoke(app, ["watch", str(workbook), "--dir", str(inbox), "--contract", "exposures"])

    assert captured["directory"] == inbox
    assert captured["glob"] == "*.xlsx"
    assert captured["contract"] == "exposures"
    assert Path(str(captured["store_dir"])).name == "handins"
    assert captured["copy_on_select"] is False
    assert captured["dedupe"] is False


def test_watch_does_not_announce_a_folder_it_could_not_start_watching(
    workbook: Path, tmp_path: Path
) -> None:
    """The reassuring line comes after the watcher is up, so it is never contradicted below it."""
    result = runner.invoke(app, ["watch", str(workbook), "--dir", str(tmp_path / "not-there")])

    assert result.exit_code == 1
    output = flattened(result.output)
    assert "does not exist" in output
    assert "watching" not in output, "it never started, so it must not say that it did"


def test_watch_stops_the_watcher_when_the_user_interrupts(
    workbook: Path, inbox: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C is how a watch is meant to end, so it exits 0 and leaves no observer thread."""
    folder = _StubFolder(interrupt=True)
    monkeypatch.setattr(WATCH_MODULE, "watch", lambda *_a, **_k: folder)

    result = runner.invoke(app, ["watch", str(workbook), "--dir", str(inbox)])

    assert result.exit_code == 0
    assert folder.stopped, "the observer thread must not outlive the command"
    assert "0 hand-in(s) received" in flattened(result.output)


# ── the real command line ────────────────────────────────────────────────────────────────────
#
# Everything above goes through CliRunner, which hands click a list of arguments. The console
# script does not: click reads `sys.argv` itself, and on Windows it rewrites what it finds there
# before any kedge code sees it. That is a whole class of bug no CliRunner test can reach, so
# these three drive the real argv instead.


def test_a_glob_pattern_reaches_the_command_exactly_as_it_was_typed(
    workbook: Path, inbox: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: click globbed `--glob` against the current directory and swallowed the result.

    ``kedge watch book.xlsx --glob "*.xlsx"`` is normally run from the folder the workbook is in
    -- which by definition contains a match -- so click's Windows argv expansion turned the
    pattern into ``process.xlsx``, the sweep found no file of that name in the inbox, and the
    command reported nothing received and exited 0.
    """
    (inbox / "exposures.xlsx").write_bytes(b"a,b\n1,2\n")
    assert list(Path.cwd().glob("*.xlsx")) == [workbook], "the cwd must hold the tempting match"
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)  # typer replaces it; put it back after
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "kedge",
            "watch",
            str(workbook),
            "--dir",
            str(inbox),
            "--once",
            "--settle",
            "0",
            "--glob",
            "*.xlsx",
        ],
    )

    with pytest.raises(SystemExit) as exit_info:
        app()

    assert exit_info.value.code == 0
    assert "1 hand-in(s) received" in flattened(capsys.readouterr().out)
    stored = [path for path in workbook.parent.rglob("*exposures.xlsx") if "handins" in path.parts]
    assert len(stored) == 1, "the hand-in the pattern matched must be in the managed store"


def test_the_command_line_expands_a_home_directory_but_not_a_wildcard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turning click's expansion off must not cost the one part of it that was worth having.

    No Windows shell expands ``~`` for a native program, so kedge does it; both shells expand
    their own variables and neither expands wildcards, so kedge does neither.
    """
    monkeypatch.setattr(
        sys, "argv", ["kedge", "watch", "~/book.xlsx", "--glob", "*.xlsx", "--contract", "~$a.xlsx"]
    )

    expanded = cli._console_args()

    assert expanded[1] == str(Path.home() / "book.xlsx")
    assert expanded[3] == "*.xlsx"
    assert expanded[5] == "~$a.xlsx", "an Excel lock-file pattern is not a home directory"


def test_the_entry_point_uses_that_expansion_and_not_clicks(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """And the app really does read its argv through it, or `~` would stop working on Windows.

    Nothing in the home directory is touched: the file does not exist, and the point is the
    path kedge says it could not find.
    """
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(sys, "argv", ["kedge", "inspect", "~/no-such-workbook.xlsx"])

    with pytest.raises(SystemExit) as exit_info:
        app()

    assert exit_info.value.code == 1
    reported = flattened(capsys.readouterr().err)
    assert "no such workbook" in reported
    assert "~" not in reported, "the home directory was not expanded on the way in"


def test_reconcile_exit_code_folds_on_status_not_on_the_passed_list() -> None:
    """Regression: `report.passed` is the list of passing regions, not a verdict.

    A report with one passing and one failing region has a truthy `.passed`, so branching on it
    exited 0 on a failed reconciliation -- a silent green light on exactly the case the command
    exists to catch. `.status` folds the regions and is falsy for anything but a clean pass.
    """

    @dataclass
    class _Report:
        status: ReconciliationStatus
        passed: list[str]

    mixed = _Report(ReconciliationStatus.FAILED, ["one region did pass"])
    clean = _Report(ReconciliationStatus.PASSED, ["everything passed"])
    vacuous = _Report(ReconciliationStatus.NOT_RECONCILED, [])

    assert bool(mixed.passed), "the bug only bites when .passed is truthy but the report failed"
    assert cli._reconcile_exit_code(mixed) == 1
    assert cli._reconcile_exit_code(clean) == 0
    assert cli._reconcile_exit_code(vacuous) == 1
