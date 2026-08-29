"""Tests for the mooring handover description.

Two things are being defended here. The first is that "does this repo declare kedge?" is
answered by parsing rather than by a substring match -- the notebook's import cell fails
invisibly in marimo's app mode, so a wrong *yes* costs a colleague a blank page with nothing to
read. The second is that hand-ins do not travel unless somebody said so, and that the produced
steps could actually be run in the order they are given.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kedge.config import Config, IngestConfig
from kedge.handover import (
    DependencyState,
    Handover,
    ShareItem,
    plan_handover,
    read_dependency,
    requirement_name,
)

DECLARING = """
[project]
name = "team-notebooks"
dependencies = ["marimo==0.23.15", "kedge>=0.2", "polars"]
"""

NOT_DECLARING = """
[project]
name = "team-notebooks"
dependencies = ["marimo==0.23.15", "polars"]
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A mooring workspace root with a locked environment that already declares kedge."""
    directory = tmp_path / "workspace"
    directory.mkdir()
    (directory / "pyproject.toml").write_text(DECLARING, encoding="utf-8")
    return directory


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A converted workbook's project directory, with everything a real one holds."""
    directory = tmp_path / "q2.kedge"
    (directory / "plans").mkdir(parents=True)
    (directory / "plans" / "v1.json").write_text("{}", encoding="utf-8")
    (directory / "runs").mkdir()
    (directory / "runs" / "20260818T104812Z.json").write_text("{}", encoding="utf-8")
    (directory / "handins").mkdir()
    (directory / "handins" / "extract.csv").write_text("id,gbp\n1,2\n", encoding="utf-8")
    (directory / "q2.py").write_text("import marimo\n", encoding="utf-8")
    (directory / "contract.yaml").write_text("columns: []\n", encoding="utf-8")
    (directory / "reconciliation.json").write_text("{}", encoding="utf-8")
    (directory / "analysis.json").write_text("{}", encoding="utf-8")
    (directory / "report.html").write_text("<p>x</p>", encoding="utf-8")
    return directory


def _sharing_handins() -> Config:
    return Config(ingest=IngestConfig(share_handins=True))


def _item(plan: Handover, name: str) -> ShareItem:
    return next(item for item in plan.items if item.name == name)


# ── PEP 508 ──────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "requirement",
    [
        "kedge",
        "KEDGE",
        "Kedge",
        "kedge>=0.1",
        "kedge==0.2.0",
        "kedge[extra]==1.2",
        "kedge (>=0.1)",
        "kedge ; python_version >= '3.12'",
        "kedge @ https://example.internal/kedge-0.2.0-py3-none-any.whl",
        "  kedge  >= 0.1  ",
        "kedge~=0.2",
        "kedge!=0.3",
    ],
)
def test_every_ordinary_way_of_writing_a_requirement_still_names_kedge(requirement: str) -> None:
    """A repo declaring kedge with a pin, an extra or a marker has still declared it.

    Each of these is what somebody actually types, and reading any of them as "not declared"
    would have kedge tell a user to add a dependency the repo already has.
    """
    assert requirement_name(requirement) == "kedge"


@pytest.mark.parametrize(
    "requirement",
    ["kedge-extras", "kedge_tools", "kedge.plugins", "mooring-kedge", "kedgeworks"],
)
def test_a_package_merely_named_after_kedge_is_not_kedge(requirement: str) -> None:
    """The trap a substring match falls into, and it fails in the dangerous direction.

    Reporting `kedge-extras` as kedge means the handover says the notebook will run, the user
    pushes it, and the import cell dies where marimo's app mode renders nothing at all.
    """
    assert requirement_name(requirement) != "kedge"


def test_a_name_is_compared_with_runs_of_punctuation_collapsed() -> None:
    """PEP 503 makes `my_pkg.name`, `my-pkg-name` and `MY__PKG.NAME` one distribution.

    Comparing raw strings would report the same package as absent depending on how the repo
    happened to spell it.
    """
    assert requirement_name("MY__PKG.NAME") == requirement_name("my-pkg-name") == "my-pkg-name"


@pytest.mark.parametrize("requirement", ["", "   ", ">=1.0", "!kedge", "-kedge", "[extra]"])
def test_something_that_is_not_a_requirement_parses_to_nothing(requirement: str) -> None:
    """A malformed entry in a dependencies list must not be guessed at or raise.

    It is one line of somebody's pyproject.toml, and the answer kedge needs from it is only ever
    "this is not the package I am looking for".
    """
    assert requirement_name(requirement) is None


# ── reading the repo ─────────────────────────────────────────────────────────────────────────


def test_a_repo_that_declares_kedge_is_reported_as_declared_with_the_line_that_says_so(
    repo: Path,
) -> None:
    """The requirement string is quoted back so a user can see what was matched."""
    report = read_dependency(repo)

    assert report.state is DependencyState.DECLARED
    assert report.satisfied
    assert report.requirement == "kedge>=0.2"


def test_a_repo_that_parses_and_omits_kedge_is_absent_rather_than_cannot_tell(
    tmp_path: Path,
) -> None:
    """Absent is the only state that justifies telling somebody to run a command.

    Collapsing it into "cannot tell" would either nag every user or nag none of them, and the
    distinction is exactly what the handover's dependency step turns on.
    """
    (tmp_path / "pyproject.toml").write_text(NOT_DECLARING, encoding="utf-8")

    assert read_dependency(tmp_path).state is DependencyState.ABSENT


def test_a_missing_pyproject_is_cannot_tell_rather_than_absent(tmp_path: Path) -> None:
    """A repo kedge cannot read is not a repo that has said no.

    Reporting absent would put a `mooring deps add` in front of a user whose repo may already be
    correct, which is the sort of instruction people learn to ignore.
    """
    report = read_dependency(tmp_path / "nowhere")

    assert report.state is DependencyState.UNKNOWN
    assert "pyproject.toml" in report.detail


def test_a_pyproject_that_will_not_parse_degrades_to_cannot_tell(tmp_path: Path) -> None:
    """A malformed file is somebody else's repo mid-edit, not a kedge bug worth a traceback."""
    (tmp_path / "pyproject.toml").write_text('[project\nname = "broken"', encoding="utf-8")

    report = read_dependency(tmp_path)

    assert report.state is DependencyState.UNKNOWN
    assert str(tmp_path / "pyproject.toml") in report.detail


def test_a_pyproject_with_no_project_table_degrades_to_cannot_tell(tmp_path: Path) -> None:
    """Valid TOML that declares nothing kedge understands is still "cannot tell"."""
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n", encoding="utf-8")

    assert read_dependency(tmp_path).state is DependencyState.UNKNOWN


def test_a_dependencies_key_that_is_not_a_list_degrades_to_cannot_tell(tmp_path: Path) -> None:
    """Iterating a string would read `kedge` out of the letters of any word containing it."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = "kedge"\n', encoding="utf-8"
    )

    assert read_dependency(tmp_path).state is DependencyState.UNKNOWN


def test_kedge_in_a_dependency_group_only_is_optional_rather_than_declared(
    tmp_path: Path,
) -> None:
    """Whether a group is installed depends on how the environment was synced.

    Calling that declared would let the handover promise a locked environment it has no evidence
    for -- and the failure it hides is the invisible one.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["marimo"]\n'
        '\n[dependency-groups]\ndev = ["pytest", "kedge>=0.2"]\n',
        encoding="utf-8",
    )

    report = read_dependency(tmp_path)

    assert report.state is DependencyState.OPTIONAL
    assert not report.satisfied


def test_a_dependency_group_holding_a_table_rather_than_a_string_is_stepped_over(
    tmp_path: Path,
) -> None:
    """`{include-group = "dev"}` is legal in a dependency group and is not a distribution name."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["kedge"]\n'
        '\n[dependency-groups]\nall = [{include-group = "dev"}]\ndev = ["pytest"]\n',
        encoding="utf-8",
    )

    assert read_dependency(tmp_path).state is DependencyState.DECLARED


@pytest.mark.parametrize(
    "marker",
    ["python_version < '3.0'", "sys_platform == 'win32'", "extra == 'dev'"],
)
def test_a_requirement_behind_an_environment_marker_is_not_a_plain_yes(
    tmp_path: Path, marker: str
) -> None:
    """`kedge; python_version < '3.0'` installs nowhere, and reads as declared to a naive check.

    kedge does not evaluate markers -- that needs `packaging`, which it does not declare -- so it
    reports the marker rather than judging it. A marker that is true on the machine running this
    check still says nothing about mooring's runner or a colleague's laptop, so erring towards
    asking is the honest answer here as well as the safe one.
    """
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "x"\ndependencies = ["kedge; {marker}"]\n', encoding="utf-8"
    )

    report = read_dependency(tmp_path)

    assert report.state is DependencyState.CONDITIONAL
    assert not report.satisfied
    assert marker in report.detail


def test_a_marker_free_requirement_is_still_a_plain_yes(repo: Path) -> None:
    """The other side of the marker check: it must not downgrade an ordinary declaration."""
    assert read_dependency(repo).satisfied


@pytest.mark.parametrize(
    "source",
    ['{path = "../kedge"}', '{path = "../kedge", editable = true}', "{workspace = true}"],
)
def test_a_uv_source_pointing_somewhere_local_is_not_a_declaration_a_colleague_can_use(
    tmp_path: Path, source: str
) -> None:
    """The most dangerous shape of all, and the one a kedge developer's own repo has.

    The declaration is perfect and the lock is local, so the check says yes, the notebook is
    pushed, and it dies at its first cell on every machine but the one it was tested on -- which
    is the exact failure this module exists to prevent, reached through the check meant to
    prevent it.
    """
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "x"\ndependencies = ["kedge"]\n\n[tool.uv.sources]\nkedge = {source}\n',
        encoding="utf-8",
    )

    report = read_dependency(tmp_path)

    assert report.state is DependencyState.LOCAL_SOURCE
    assert not report.satisfied


@pytest.mark.parametrize(
    "source",
    ['{git = "https://github.com/OpenAfterHours/kedge"}', '{index = "internal"}'],
)
def test_a_uv_source_everybody_can_reach_leaves_the_declaration_alone(
    tmp_path: Path, source: str
) -> None:
    """Only `path` and `workspace` are machine-local. Flagging a git or index override would
    nag about a repo that is set up correctly, and a warning nobody needs is one they learn to
    skip past on the day it matters."""
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "x"\ndependencies = ["kedge"]\n\n[tool.uv.sources]\nkedge = {source}\n',
        encoding="utf-8",
    )

    assert read_dependency(tmp_path).state is DependencyState.DECLARED


def test_a_local_source_for_a_different_package_does_not_touch_the_kedge_verdict(
    tmp_path: Path,
) -> None:
    """A repo pinning its own library to a path says nothing about how kedge resolves."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["kedge", "houseutils"]\n'
        '\n[tool.uv.sources]\nhouseutils = {path = "../houseutils"}\n',
        encoding="utf-8",
    )

    assert read_dependency(tmp_path).state is DependencyState.DECLARED


def test_the_local_source_step_says_to_remove_the_entry_rather_than_add_the_package(
    project: Path, repo: Path
) -> None:
    """`mooring deps add kedge` is the wrong instrument, and would look like it worked.

    The package is already declared; adding it again leaves the source override standing and the
    lock exactly as local as it was, so the user runs a command, sees no error, and pushes a
    notebook that still cannot start.
    """
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["kedge"]\n'
        '\n[tool.uv.sources]\nkedge = {path = "../kedge"}\n',
        encoding="utf-8",
    )

    plan = plan_handover(project, repo)

    assert "mooring deps lock" in plan.commands
    assert "mooring deps add kedge" not in plan.commands
    assert any("tool.uv.sources" in step.instruction for step in plan.steps)


def test_a_pyproject_with_only_dependency_groups_reports_optional_not_cannot_tell(
    tmp_path: Path,
) -> None:
    """The groups were gathered after the [project] guard had already returned.

    So a file with groups and no [project] table never reached them, and `OPTIONAL`'s own
    docstring described a detection that could not happen. Prose does not go red; this does.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[dependency-groups]\nnotebooks = ["kedge>=0.2"]\n', encoding="utf-8"
    )

    report = read_dependency(tmp_path)

    assert report.state is DependencyState.OPTIONAL
    assert report.requirement == "kedge>=0.2"


def test_dependencies_computed_by_the_build_backend_do_not_read_as_declared(
    tmp_path: Path,
) -> None:
    """`dynamic = ["dependencies"]` means the list is not in the file at all.

    kedge cannot see what the backend will produce, and the safe answer is the one that still
    names a command rather than one that promises the notebook will run.
    """
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndynamic = ["dependencies"]\n', encoding="utf-8"
    )

    assert not read_dependency(tmp_path).satisfied


# ── what travels ─────────────────────────────────────────────────────────────────────────────


def test_hand_ins_are_withheld_and_everything_describing_the_process_travels(
    project: Path, repo: Path
) -> None:
    """The whole policy in one assertion, and the default is the safe one.

    The plan, the contract, the acceptance record and the run history describe the process; the
    hand-in store holds the production figures themselves.
    """
    plan = plan_handover(project, repo)

    assert {item.name for item in plan.shared} == {
        "q2.py",
        "plans",
        "runs",
        "contract.yaml",
        "reconciliation.json",
        "analysis.json",
        "report.html",
    }
    assert [item.name for item in plan.withheld] == ["handins"]
    assert plan.handins_shared is False


def test_the_run_history_travels_because_an_audit_trail_on_one_laptop_is_not_one(
    project: Path, repo: Path
) -> None:
    """Pinned on its own because it is the decision most likely to be quietly reversed.

    A run record carries hand-in digests and typed reasons, never hand-in contents, and it is the
    thing that answers "was this month's process carried out, and who signed it off".
    """
    plan = plan_handover(project, repo)

    assert _item(plan, "runs").shared is True


def test_hand_ins_travel_when_the_project_opts_in(project: Path, repo: Path) -> None:
    """A worked example or a reference month is a legitimate thing to share.

    The opt-in has to actually reach the classification, or the config key is decoration.
    """
    plan = plan_handover(project, repo, config=_sharing_handins())

    assert _item(plan, "handins").shared is True
    assert plan.handins_shared is True
    assert any("share_handins is on" in warning for warning in plan.warnings)


def test_a_hand_in_store_configured_outside_the_project_directory_never_appears(
    project: Path, repo: Path, tmp_path: Path
) -> None:
    """The strongest form of withholding is having nothing there to push.

    `store_dir` outside the project directory means no command that uploads a folder can carry
    the extracts, so there is nothing to list either way.
    """
    elsewhere = tmp_path / "extracts"
    elsewhere.mkdir()
    (elsewhere / "extract.csv").write_text("id\n1\n", encoding="utf-8")
    (project / "handins" / "extract.csv").unlink()
    (project / "handins").rmdir()

    plan = plan_handover(project, repo, handins_dir=elsewhere)

    assert plan.withheld == ()
    assert plan.handins_dir == elsewhere.resolve()
    assert not any("no paths" in warning for warning in plan.warnings)


def _nested_store(project: Path, relative: str) -> Path:
    """Move the fixture's hand-ins to ``relative`` under the project, as `store_dir` would."""
    store = project / relative
    store.mkdir(parents=True)
    (store / "extract.csv").write_text("id,gbp\n1,2\n", encoding="utf-8")
    (project / "handins" / "extract.csv").unlink()
    (project / "handins").rmdir()
    return store


def _push(plan: Handover) -> str:
    return next(command for command in plan.commands if command.startswith("mooring push"))


def test_a_store_nested_below_a_top_level_entry_withholds_the_entry_that_holds_it(
    project: Path, repo: Path
) -> None:
    """`store_dir = "data/handins"` puts the extracts under a top-level `data/`.

    Matching the store by name or by equality against each top-level entry sees neither, so `data/`
    fell through to "unrecognised" and was shared -- while the report went on saying hand-ins were
    withheld. Being told you are protected while the extracts go up is worse than no policy at all.
    """
    _nested_store(project, "data/handins")

    plan = plan_handover(project, repo, handins_dir=project / "data" / "handins")

    assert [item.name for item in plan.withheld] == ["data"]
    assert "data" not in {item.name for item in plan.shared}


def test_a_nested_store_is_never_named_in_the_push_command(project: Path, repo: Path) -> None:
    """The command is the thing that actually uploads, so it is the thing worth asserting on.

    A classification that says "withheld" and a command line that names the path are not in
    disagreement about a detail; the command wins, every time.
    """
    _nested_store(project, "data/handins")

    plan = plan_handover(project, repo, handins_dir=project / "data" / "handins")

    assert "data" not in _push(plan)
    assert "handins" not in _push(plan)


def test_a_store_nested_two_levels_down_is_caught_by_the_same_rule(
    project: Path, repo: Path
) -> None:
    """Depth is not part of the rule, and a test at one level would not prove that."""
    _nested_store(project, "data/extracts/handins")

    plan = plan_handover(project, repo, handins_dir=project / "data" / "extracts" / "handins")

    assert [item.name for item in plan.withheld] == ["data"]
    assert "data" not in _push(plan)


def test_withholding_a_container_says_so_rather_than_doing_it_silently(
    project: Path, repo: Path
) -> None:
    """Withholding `data/` also withholds everything else in `data/`.

    A user who is not told that will wonder why the team never got the other files in there, and
    the fix -- move the store, or restructure -- is theirs to make rather than kedge's to guess.
    """
    _nested_store(project, "data/handins")
    (project / "data" / "lookup.csv").write_text("code,name\n1,a\n", encoding="utf-8")

    plan = plan_handover(project, repo, handins_dir=project / "data" / "handins")

    notice = next(
        warning for warning in plan.warnings if "store_dir" in warning and "data/" in warning
    )
    assert "withheld" in notice
    assert "data/handins" in notice
    assert "withheld whole" in _item(plan, "data").reason


def test_a_nested_store_travels_with_its_container_when_the_project_opts_in(
    project: Path, repo: Path
) -> None:
    """The opt-in has to reach the container case too, or it silently does nothing there."""
    _nested_store(project, "data/handins")

    plan = plan_handover(
        project, repo, config=_sharing_handins(), handins_dir=project / "data" / "handins"
    )

    assert _item(plan, "data").shared is True
    assert "notebooks/q2.kedge/data" in _push(plan)


MODEL_KEY = "sk-this-value-must-never-be-printed-0123456789"


def test_a_model_credential_in_the_project_config_blocks_the_handover(
    project: Path, repo: Path
) -> None:
    """A handover is the moment the directory is copied somewhere shared.

    The launch path deliberately does not refuse over the same file -- the key is already on disk
    and refusing removes the route to the panel that clears it -- so the hard stop belongs at the
    copy. It is a blocker rather than a warning because it is the only thing on this path that
    cannot be taken back once it is pushed, and a warning would sit among the others.
    """
    (project / ".marimo.toml").write_text(
        f'[ai.open_ai]\napi_key = "{MODEL_KEY}"\n', encoding="utf-8"
    )

    plan = plan_handover(project, repo)

    assert not plan.ready
    assert any("ai.open_ai.api_key" in blocker for blocker in plan.blockers)


def test_the_credential_block_names_the_key_and_never_the_value(project: Path, repo: Path) -> None:
    """A tool that reports a leaked secret by printing it has leaked it again, to a terminal
    and to whatever scrapes that terminal. Names only, and the whole handover is searched --
    a value copied into a reason or a step would be just as published."""
    (project / ".marimo.toml").write_text(
        f'[ai.open_ai]\napi_key = "{MODEL_KEY}"\n', encoding="utf-8"
    )

    plan = plan_handover(project, repo)
    everything = " ".join(
        [
            *plan.blockers,
            *plan.warnings,
            *(item.reason for item in plan.items),
            *(step.instruction for step in plan.steps),
            *(step.command or "" for step in plan.steps),
        ]
    )

    assert MODEL_KEY not in everything


def test_the_credential_block_says_the_risk_survives_the_careful_push(
    project: Path, repo: Path
) -> None:
    """The push command never names the file, so a user could reasonably think it stays behind.

    It does not: it travels with the folder. Saying only "there is a key in this file" would
    leave somebody looking at a push list that plainly excludes it and concluding they are fine.
    """
    (project / ".marimo.toml").write_text(
        f'[ai.open_ai]\napi_key = "{MODEL_KEY}"\n', encoding="utf-8"
    )

    blocker = next(b for b in plan_handover(project, repo).blockers if "api_key" in b)

    assert blocker.startswith("Clear ")
    assert "travels with the folder" in blocker
    assert "git add ." in blocker


def test_a_project_with_no_marimo_config_says_nothing_about_credentials(
    project: Path, repo: Path
) -> None:
    """The common case must be silent, or this becomes the amber signal nobody reads."""
    plan = plan_handover(project, repo)

    assert plan.ready
    assert not any("credential" in text for text in (*plan.blockers, *plan.warnings))


def test_a_marimo_config_holding_no_credential_says_nothing(project: Path, repo: Path) -> None:
    """kedge writes one of these into every project directory it opens, so a file being there
    is the norm rather than the signal. Only a key with a value in it is."""
    (project / ".marimo.toml").write_text(
        '[ai]\nenabled = false\n\n[display]\ntheme = "dark"\n', encoding="utf-8"
    )

    plan = plan_handover(project, repo)

    assert plan.ready
    assert plan.blockers == ()


def test_a_store_configured_above_the_project_directory_is_a_blocker(
    project: Path, repo: Path
) -> None:
    """A store that contains the project makes every entry a hand-in and the push empty.

    Classifying that correctly produces a wall of withheld paths and no command, which is a true
    answer nobody can act on. Saying it once, as the reason there is nothing to do, is the
    difference between a report and a puzzle.
    """
    plan = plan_handover(project, repo, handins_dir=project.parent)

    assert not plan.ready
    assert any("not one above it" in blocker for blocker in plan.blockers)


def test_a_store_left_behind_by_a_project_that_moved_its_hand_ins_is_still_withheld(
    project: Path, repo: Path, tmp_path: Path
) -> None:
    """Matching only the configured path would let a stale `handins/` fall through to "unknown".

    Unknown entries are shared, so the one directory of production extracts on disk would be the
    one thing the policy failed to catch -- and it would fail silently.
    """
    elsewhere = tmp_path / "extracts"
    elsewhere.mkdir()

    plan = plan_handover(project, repo, handins_dir=elsewhere)

    assert [item.name for item in plan.withheld] == ["handins"]


def test_hand_ins_inside_the_shared_directory_warn_that_a_bare_push_would_take_them(
    project: Path, repo: Path
) -> None:
    """Withholding is a claim about a command, and the wrong command defeats it.

    The user needs to know before they push, not after, so the warning names the config key that
    removes the hazard rather than only the one that describes it.
    """
    plan = plan_handover(project, repo)

    hazard = next(warning for warning in plan.warnings if "no paths" in warning)
    assert "store_dir" in hazard


def test_a_file_kedge_did_not_write_is_shared_and_named_in_a_warning(
    project: Path, repo: Path
) -> None:
    """Silently withholding an unknown file breaks the notebook where nobody can see why.

    Sharing it and saying so puts the judgement with the person who put it there.
    """
    (project / "notes.txt").write_text("read me\n", encoding="utf-8")

    plan = plan_handover(project, repo)

    assert _item(plan, "notes.txt").shared is True
    assert any("notes.txt" in warning for warning in plan.warnings)


def test_a_helper_module_beside_the_notebook_is_not_mistaken_for_the_notebook(
    project: Path, repo: Path
) -> None:
    """Taking the first `.py` file would point `mooring verify` at a helper, alphabetically.

    The notebook is named after the workbook and the project directory after the same stem, so
    the stems are what identify it -- and the helper still travels, as an unrecognised entry.
    """
    (project / "helpers.py").write_text("x = 1\n", encoding="utf-8")

    plan = plan_handover(project, repo)

    assert plan.notebook == "q2.py"
    assert _item(plan, "helpers.py").shared is True


def test_build_artefacts_and_dot_entries_are_listed_neither_way(project: Path, repo: Path) -> None:
    """A __pycache__ in the shared list is noise that trains people to skim the list."""
    (project / "__pycache__").mkdir()
    (project / ".DS_Store").write_text("", encoding="utf-8")

    plan = plan_handover(project, repo)

    assert {"__pycache__", ".DS_Store"}.isdisjoint({item.name for item in plan.items})


# ── the steps ────────────────────────────────────────────────────────────────────────────────


def test_the_steps_run_in_an_order_that_would_actually_work(project: Path, repo: Path) -> None:
    """The move, then verification, then the push, and no registration step at all.

    Landing under a folder mooring already syncs is what removes the registration: `mooring new`
    would scaffold a template over the real notebook, and `mooring adopt` discovers its candidates
    from the remote, so it cannot reach a directory that has never been pushed. Verification runs
    on the machine that made the notebook, before a colleague can open it.
    """
    plan = plan_handover(project, repo)
    verbs = [step.command.split()[1] if step.command else "move" for step in plan.steps]

    assert verbs == ["move", "verify", "push"]
    assert not any(
        command.startswith(("mooring new", "mooring adopt")) for command in plan.commands
    )


def test_a_project_directory_already_under_notebooks_needs_no_move(
    repo: Path, tmp_path: Path
) -> None:
    """The common case once somebody keeps their workbooks in the mooring workspace.

    Its workspace path is where it already is, and the steps are commands from the first one.
    """
    inside = repo / "notebooks" / "q2.kedge"
    inside.mkdir(parents=True)
    (inside / "q2.py").write_text("import marimo\n", encoding="utf-8")

    plan = plan_handover(inside, repo)

    assert plan.workspace_path == "notebooks/q2.kedge"
    assert all(step.command is not None for step in plan.steps)


def test_a_directory_inside_the_repo_but_outside_the_synced_folders_still_moves(
    repo: Path,
) -> None:
    """Being in the workspace is not the same as being synced.

    Mooring syncs a nested path only under `notebooks`, `data` or `reports`, and the only verb
    that registers anything else needs the files to be in the remote already. So a project in a
    folder mooring ignores is moved like one that was never in the repo, and every later command
    names the destination rather than where it sits today.
    """
    inside = repo / "finance" / "q2.kedge"
    inside.mkdir(parents=True)
    (inside / "q2.py").write_text("import marimo\n", encoding="utf-8")

    plan = plan_handover(inside, repo)

    assert plan.workspace_path == "notebooks/q2.kedge"
    assert plan.steps[0].command is None
    assert "mooring verify notebooks/q2.kedge/q2.py" in plan.commands


def test_the_dependency_step_appears_only_when_the_repo_might_not_have_kedge(
    project: Path, repo: Path
) -> None:
    """A command offered on every handover is one people stop reading.

    It is also the only step that prevents the failure this module exists for, so it must appear
    whenever the answer is anything other than a confirmed yes.
    """
    declared = plan_handover(project, repo)
    (repo / "pyproject.toml").write_text(NOT_DECLARING, encoding="utf-8")
    absent = plan_handover(project, repo)

    assert "mooring deps add kedge" not in declared.commands
    assert "mooring deps add kedge" in absent.commands


def test_a_repo_that_cannot_be_read_still_offers_the_dependency_step_and_says_why(
    project: Path, tmp_path: Path
) -> None:
    """Cannot tell is not a pass, and the user is the one who can go and look."""
    empty_repo = tmp_path / "workspace"
    empty_repo.mkdir()

    plan = plan_handover(project, empty_repo)

    assert plan.dependency.state is DependencyState.UNKNOWN
    assert "mooring deps add kedge" in plan.commands
    assert any("Check it by hand" in warning for warning in plan.warnings)


def test_the_push_step_names_every_shared_path_and_never_the_hand_in_store(
    project: Path, repo: Path
) -> None:
    """The push is where the hand-in policy is either enforced or lost.

    A bare `mooring push` uploads every changed file, so the withholding only holds if the
    command kedge hands the user lists the paths itself.
    """
    plan = plan_handover(project, repo)

    push = next(command for command in plan.commands if command.startswith("mooring push"))
    assert "notebooks/q2.kedge/q2.py" in push
    assert "notebooks/q2.kedge/runs" in push
    assert "handins" not in push


def test_the_verify_step_names_the_notebook_mooring_would_open(project: Path, repo: Path) -> None:
    """`mooring verify` takes a workspace-relative path, not a local one.

    Handing over the local path produces a command that fails for a reason unrelated to the
    notebook, on the one step whose whole job is to prove the notebook runs.
    """
    plan = plan_handover(project, repo)

    assert "mooring verify notebooks/q2.kedge/q2.py" in plan.commands


def test_a_workspace_path_holding_a_space_is_quoted(repo: Path) -> None:
    """A workbook named `Month End.xlsx` gives a project directory with a space in it.

    Unquoted it splits into two arguments, and `mooring verify` fails on a path that is not the
    notebook's, on the one step whose job is to prove the notebook runs.
    """
    inside = repo / "notebooks" / "month end.kedge"
    inside.mkdir(parents=True)
    (inside / "month end.py").write_text("import marimo\n", encoding="utf-8")

    plan = plan_handover(inside, repo)

    assert '"notebooks/month end.kedge/month end.py"' in " ".join(plan.commands)


# ── blockers ─────────────────────────────────────────────────────────────────────────────────


def test_a_project_directory_that_does_not_exist_is_a_blocker_not_a_traceback(
    repo: Path, tmp_path: Path
) -> None:
    """The caller is a CLI command or a route, and it has to say something either way."""
    plan = plan_handover(tmp_path / "never-converted.kedge", repo)

    assert not plan.ready
    assert plan.items == ()
    assert any("Convert the workbook first" in blocker for blocker in plan.blockers)


def test_a_project_directory_with_no_notebook_is_a_blocker(repo: Path, tmp_path: Path) -> None:
    """An analysis with no notebook is a conversion somebody abandoned half way.

    There is nothing for a colleague to open, so saying so beats producing a step list that
    verifies a file which is not there.
    """
    directory = tmp_path / "q2.kedge"
    directory.mkdir()
    (directory / "analysis.json").write_text("{}", encoding="utf-8")

    plan = plan_handover(directory, repo)

    assert not plan.ready
    assert plan.notebook is None
    assert "mooring verify" not in " ".join(plan.commands)


def test_a_repo_that_is_not_a_directory_warns_rather_than_blocks(
    project: Path, tmp_path: Path
) -> None:
    """kedge cannot know whether a path is a mooring workspace, only whether it is there.

    A warning lets somebody plan the handover before the workspace exists; a blocker would make
    the description useless in exactly that case.
    """
    plan = plan_handover(project, tmp_path / "not-yet")

    assert plan.ready
    assert any("mooring init" in warning for warning in plan.warnings)
