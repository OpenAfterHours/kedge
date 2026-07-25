"""Tests for the command line: argument surface, output, and the milestone shims."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kedge import cli
from kedge import config as config_module
from kedge.cli import app
from kedge.reconcile import ReconciliationStatus

SECRET = "sk-must-never-be-printed-0123456789"

runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


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


# ── help and version ─────────────────────────────────────────────────────────────────────────


def test_help_lists_every_command() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in ("open", "hub", "inspect", "reconcile", "contract", "config", "doctor"):
        assert command in result.output


def test_no_arguments_shows_help_rather_than_failing_silently() -> None:
    result = runner.invoke(app, [])

    assert "Usage" in result.output


def test_version_reports_the_package_and_the_marimo_pin() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert "kedge" in result.output
    assert "0.23.15" in result.output


@pytest.mark.parametrize("command", ["open", "hub", "inspect", "reconcile", "config", "doctor"])
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
    for check in ("python", "marimo", "config", "keyring", "marker files"):
        assert check in result.output


def test_doctor_json_is_machine_readable() -> None:
    result = runner.invoke(app, ["doctor", "--no-network", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ok"] is True
    names = {check["check"] for check in payload["checks"]}
    assert {"python", "marimo", "config", "keyring", "user directory", "marker files"} <= names


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


def test_doctor_reports_a_workspace_for_a_named_workbook(workbook: Path) -> None:
    result = runner.invoke(app, ["doctor", "--no-network", "--json", "--workbook", str(workbook)])

    workspace_check = next(
        c for c in json.loads(result.output)["checks"] if c["check"] == "workspace"
    )
    assert workspace_check["status"] == "ok"
    assert "kedge-process-" in workspace_check["detail"]


# ── argument validation ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "argv",
    [
        ["open", "absent.xlsx"],
        ["inspect", "absent.xlsx"],
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


def test_a_command_whose_milestone_has_not_landed_names_the_owing_module(workbook: Path) -> None:
    """Nothing silently no-ops; an unimplemented command says whose it is."""
    result = runner.invoke(app, ["contract", "infer", str(workbook)])

    if isinstance(result.exception, NotImplementedError):
        message = str(result.exception)
        assert "kedge." in message
        assert "M5" in message
    else:  # The milestone landed; the shim is gone, which is the point of the shim.
        assert result.exception is None, result.output


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


def test_contract_infer_names_the_module_that_owes_the_implementation(tmp_path: Path) -> None:
    handin = tmp_path / "cwd" / "handin.csv"
    handin.write_text("a,b\n1,2\n", encoding="utf-8")

    result = runner.invoke(app, ["contract", "infer", str(handin)])

    assert isinstance(result.exception, NotImplementedError)
    assert "M5" in str(result.exception)


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
