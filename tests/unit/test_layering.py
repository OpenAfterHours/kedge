"""The whole ladder, not one rung of it: ``analysis/ -> plan/ -> notebook/ -> agent/ -> server/``.

``scripts/guardrails.py`` used to check a single edge -- ``notebook/`` may not import
``kedge.agent`` -- because that was the edge somebody had actually inverted and, on the tree of the
day, the only one that held. Enforcing an invariant only where it has already been broken catches
the breach that has happened rather than the one that is coming, so the check now walks every rung.
This module is where each rung is argued.

Every test here calls the guardrail's **own** functions rather than walking the tree again. A
second copy of a rule is a rule with two versions, and the first draft of the original layering
test proved the point by inheriting the same blind spot as the guardrail it was meant to back up.

The plants are hermetic: :data:`scripts.guardrails.REPO_ROOT` is redirected at a temporary tree, so
nothing is ever written into `src/`. That matters more than tidiness -- a test that plants a broken
import in the working tree and reverts it is a test that leaves a broken import behind the moment
it fails.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from scripts import guardrails
from scripts.guardrails import (
    LAYERS,
    _check_layering,
    _import_targets,
    _is_or_is_under,
    layers_above,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

REPO = Path(__file__).resolve().parents[2]


# ── the ladder is a hand-written copy of a documented one ────────────────────────────────────


def test_the_guardrails_ladder_is_the_one_claude_md_states() -> None:
    """Prose does not go red, so the copy is checked against the original.

    `CLAUDE.md` names the layering in one sentence and `scripts/guardrails.py` enforces a tuple.
    Nothing but this makes the second follow the first, and a guardrail enforcing a ladder the
    architecture no longer has is worse than no guardrail: it passes, loudly, over the wrong tree.
    """
    text = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
    line = next(line for line in text.splitlines() if line.startswith("The layering that matters:"))

    assert tuple(re.findall(r"`(\w+)/`", line)) == LAYERS


def test_every_layer_named_in_the_ladder_is_a_real_package() -> None:
    """A typo in the tuple would silently check nothing at all: `_python_files` skips what is
    not a directory, so `LAYERS = ("analisys", ...)` would report a clean ladder for ever."""
    for layer in LAYERS:
        assert (REPO / "src" / "kedge" / layer / "__init__.py").is_file(), layer


def test_layers_above_is_everything_higher_plus_what_sits_above_the_whole_ladder() -> None:
    assert layers_above("analysis") == (
        "kedge.plan",
        "kedge.notebook",
        "kedge.agent",
        "kedge.server",
        "kedge.serve",
    )
    assert layers_above("agent") == ("kedge.server", "kedge.serve")
    # The top rung is still not allowed the composition module: `server/` is a layer, and the
    # thing that wires a server to an agent is above it.
    assert layers_above("server") == ("kedge.serve",)


def test_kedge_serve_is_not_confused_with_kedge_server() -> None:
    """Six shared letters and a dotted-segment match is the only thing keeping them apart.

    A prefix match would make every ``from kedge.server...`` in the tree a breach of the
    composition rule, and the guardrail would be unrunnable rather than wrong -- which is the good
    failure. The bad one is the reverse, so both directions are asserted.
    """
    assert not _is_or_is_under("kedge.server", "kedge.serve")
    assert not _is_or_is_under("kedge.server.app", "kedge.serve")
    assert _is_or_is_under("kedge.serve", "kedge.serve")


def test_the_off_ladder_packages_are_an_omission_somebody_chose() -> None:
    """Five of the ten packages under ``src/kedge`` are on the ladder. Naming the other five here
    is the difference between a decision and an oversight.

    ``reconcile/`` is named in the same `CLAUDE.md` sentence as the ladder, and named as sitting
    *below* the CLI, the notebook and the agent -- so it is deliberately not a rung, and neither
    are the other four. What this test buys is that a new subpackage cannot land silently: it will
    be in neither list, and this fails until somebody decides which.
    """
    off_ladder = {"reconcile", "ingest", "contracts", "knowledge", "xl"}
    packages = {
        path.parent.name
        for path in (REPO / "src" / "kedge").glob("*/__init__.py")
        if "__pycache__" not in path.parts
    }

    assert packages == set(LAYERS) | off_ladder


# ── planting one upward import at each rung ──────────────────────────────────────────────────


def _plant(root: Path, files: Iterable[tuple[str, str]]) -> None:
    """Write a minimal ``src/kedge`` tree holding the given (repo-relative path, source) pairs."""
    for relative, source in files:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")


UPWARD = [
    ("analysis", "from kedge.plan.model import ProcessPlan", "kedge.plan"),
    ("plan", "import kedge.notebook.scaffold", "kedge.notebook"),
    ("notebook", "from kedge import agent", "kedge.agent"),
    ("agent", "from kedge.server.app import create_app", "kedge.server"),
]


@pytest.mark.parametrize(("layer", "statement", "banned"), UPWARD)
def test_an_upward_import_is_caught_at_every_rung(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    layer: str,
    statement: str,
    banned: str,
) -> None:
    """One plant per rung, each naming the file, the line and the rule it broke.

    ``notebook/`` is the rung that has actually been inverted and it was the only one checked; the
    other three were unenforced on a tree that happened to satisfy them. Each statement here is a
    different spelling on purpose -- absolute ``from``, plain ``import``, the module-plus-alias
    form that an earlier version of `_import_targets` let straight through, and a submodule import
    -- so the generalisation cannot have quietly dropped one of the shapes the single-edge check
    already knew.
    """
    _plant(tmp_path, [(f"src/kedge/{layer}/planted.py", f"{statement}\n")])
    monkeypatch.setattr(guardrails, "REPO_ROOT", tmp_path)

    breaches = _check_layering()

    assert len(breaches) == 1, [breach.render() for breach in breaches]
    rendered = breaches[0].render()
    assert rendered.startswith(f"src/kedge/{layer}/planted.py:1: ")
    assert banned in rendered
    assert f"src/kedge/{layer}/ may not import {banned}" in breaches[0].rule


def test_all_four_rungs_break_at_once_and_all_four_are_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check reports every breach rather than the first, so one run fixes one tree."""
    _plant(
        tmp_path,
        [(f"src/kedge/{layer}/planted.py", f"{statement}\n") for layer, statement, _ in UPWARD],
    )
    monkeypatch.setattr(guardrails, "REPO_ROOT", tmp_path)

    breaches = _check_layering()

    assert [breach.path.parent.name for breach in breaches] == sorted(
        layer for layer, _, _ in UPWARD
    )


@pytest.mark.parametrize(
    ("layer", "statement"),
    [
        ("server", "from kedge.agent.loop import KedgeAgent"),
        ("agent", "from kedge.notebook.codegen import read_notebook"),
        ("notebook", "from kedge.plan.model import ProcessPlan"),
        ("plan", "from kedge.analysis.model import WorkbookAnalysis"),
        ("agent", "from kedge.turn import DoneEvent"),
        ("server", "from kedge.turn import DoneEvent"),
        ("notebook", "from kedge.sql import literal"),
    ],
)
def test_a_downward_or_off_ladder_import_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layer: str, statement: str
) -> None:
    """The ladder constrains direction, not traffic.

    The last three matter most for this refactor: ``kedge.turn`` and ``kedge.sql`` are top-level
    modules, so they are not on the ladder at all, and both an agent and a server may reach them.
    That is the whole reason the turn vocabulary went there rather than under either package.
    """
    _plant(tmp_path, [(f"src/kedge/{layer}/planted.py", f"{statement}\n")])
    monkeypatch.setattr(guardrails, "REPO_ROOT", tmp_path)

    assert [breach.render() for breach in _check_layering()] == []


# ── the exemption that was considered and refused ────────────────────────────────────────────


def test_a_type_checking_import_upward_is_still_a_breach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``TYPE_CHECKING`` escape hatch, and the seam that would have wanted one no longer does.

    ``kedge.agent.loop`` implements :class:`kedge.server.agent_seam.AgentLoop`, a Protocol one
    layer above it. That inversion is legitimate -- an interface belongs to the caller that needs
    it -- and it costs nothing, because the loop satisfies the Protocol structurally and never
    imports it. What the loop did import from up there was the seam's *data*: the turn request and
    the cancellation token, which are arguments rather than an interface. Those moved down to
    ``kedge.turn``, so the exemption had no remaining customer and was not granted.

    Which leaves the rule absolute, deliberately. A type-only import cannot make a runtime cycle,
    but the inversion this check exists for began as exactly that and grew runtime edges
    afterwards.
    """
    _plant(
        tmp_path,
        [
            (
                "src/kedge/agent/planted.py",
                "from typing import TYPE_CHECKING\n\n"
                "if TYPE_CHECKING:\n"
                "    from kedge.server.agent_seam import AgentLoop\n",
            )
        ],
    )
    monkeypatch.setattr(guardrails, "REPO_ROOT", tmp_path)

    breaches = _check_layering()

    assert len(breaches) == 1
    assert breaches[0].render().startswith("src/kedge/agent/planted.py:4: ")


def test_a_relative_import_upward_is_resolved_rather_than_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``from ..server import events`` is two dots away and names no banned module until it is
    resolved against the file's own package. The other guardrail checks skip relative imports on
    the stated ground that one cannot reach pandas that way; one can reach the layer above."""
    _plant(
        tmp_path,
        [("src/kedge/agent/deep/planted.py", "from ...server import events\n")],
    )
    monkeypatch.setattr(guardrails, "REPO_ROOT", tmp_path)

    breaches = _check_layering()

    assert len(breaches) == 1
    assert "from kedge.server import events" in breaches[0].render()


# ── the composition module sits above the ladder, and nothing imports it ─────────────────────


COMPOSITION_SPELLINGS = [
    "import kedge.serve",
    "from kedge.serve import serve",
    "from kedge import serve",
    "from .. import serve",
    "from ..serve import build_agent_app",
]


@pytest.mark.parametrize("statement", COMPOSITION_SPELLINGS)
def test_every_spelling_of_reaching_the_composition_module_is_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, statement: str
) -> None:
    """``kedge.serve`` holds both ends of one arrow, so a rung importing it reaches every layer.

    This was found the hard way and is the reason the check moved into the guardrail rather than
    living beside it as a test. The first version of this guard was a regex --
    ``^\\s*(from kedge\\.serve\\b|import kedge\\.serve\\b)`` -- and ``from kedge import serve``
    does not match it: the module named is ``kedge`` and the dotted path is only complete once the
    alias is appended. That is the *same* blind spot ``_import_targets`` carries a nine-line
    docstring about, written in the same change. Planted in a scratch tree,
    ``src/kedge/notebook/planted.py`` containing ``from kedge import serve`` produced no breach,
    no offender, and ``ImportError: cannot import name 'ContextMessage' from partially initialized
    module 'kedge.agent.context'`` -- the exact failure class the guardrail's own module docstring
    quotes as its reason for existing -- with FastAPI loaded on top.

    Ranking ``kedge.serve`` above the ladder puts every spelling through
    :func:`~scripts.guardrails._import_targets`, which already knows all of them.
    """
    _plant(tmp_path, [("src/kedge/notebook/planted.py", f"{statement}\n")])
    monkeypatch.setattr(guardrails, "REPO_ROOT", tmp_path)

    breaches = _check_layering()

    assert len(breaches) == 1, [breach.render() for breach in breaches]
    assert "may not import kedge.serve" in breaches[0].rule
    assert "sits above the whole ladder" in breaches[0].rule


def test_even_the_top_rung_may_not_import_the_composition_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``server/`` is a layer; the module that composes a server with an agent is not one.

    Left out, ``server/app.py`` could import ``kedge.serve`` and pull ``KedgeAgent`` into the
    process by the back door -- which is exactly what ``server/agent_seam.py`` exists to prevent,
    and what nothing else here would report, because the server may legally import the agent.
    """
    _plant(tmp_path, [("src/kedge/server/planted.py", "from kedge import serve\n")])
    monkeypatch.setattr(guardrails, "REPO_ROOT", tmp_path)

    assert [breach.render() for breach in _check_layering()] != []


def test_the_composition_module_is_imported_by_nothing_in_the_package_at_all() -> None:
    """The rungs are the guardrail's business; the top-level modules beside it are this test's.

    ``kedge/cli.py``, ``kedge/registry.py`` and their neighbours are not on the ladder, so
    `_check_layering` never walks them, and one of them importing ``kedge.serve`` would load a web
    framework and a model client into every ``kedge inspect``. Asserted by AST rather than by
    regex, and through the guardrail's own resolver, so that ``from kedge import serve`` cannot
    walk past it a second time.
    """
    offenders: list[str] = []
    for path in sorted((REPO / "src" / "kedge").rglob("*.py")):
        relative = path.relative_to(REPO).as_posix()
        if "__pycache__" in path.parts or relative == "src/kedge/serve.py":
            continue
        package = "kedge." + ".".join(path.relative_to(REPO / "src" / "kedge").parts[:-1])
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        offenders += [
            f"{relative}:{node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Import | ast.ImportFrom)
            and any(
                _is_or_is_under(target, banned)
                for target in _import_targets(node, package.rstrip("."))
                for banned in guardrails.ABOVE_LADDER
            )
        ]

    assert offenders == []


# ── what the ladder buys at runtime ──────────────────────────────────────────────────────────


def _modules_after(program: str) -> set[str]:
    """The interesting entries of ``sys.modules`` after running `program` in a fresh interpreter.

    Its own interpreter because an import is idempotent: a module another test already loaded
    would make this pass over a tree where it should not.
    """
    finished = subprocess.run(
        [
            sys.executable,
            "-c",
            f"{program}\nimport sys, json\nprint(json.dumps(sorted(sys.modules)))",
        ],
        check=False,
        capture_output=True,
        text=True,
        # A child that hangs must fail this test rather than the whole suite: what is being
        # imported here is precisely the code most likely to open a socket by accident.
        timeout=120,
    )
    assert finished.returncode == 0, f"{program}\n{finished.stderr}"
    return set(json.loads(finished.stdout))


def test_importing_the_agent_no_longer_drags_the_whole_server_package_in() -> None:
    """The cost the inversion was charging, and the reason the deferred imports did not save it.

    ``agent/loop.py`` imported ``kedge.server.events`` at module scope. Importing a submodule runs
    its package's ``__init__``, and ``kedge/server/__init__.py`` imports ``kedge.server.app``,
    which imports FastAPI -- so ``import kedge.agent`` loaded the entire server package, while two
    function-local imports lower down the same file were carrying a comment about keeping FastAPI
    out of the way of exactly that. The vocabulary moved to ``kedge.turn``; the wiring moved to
    ``kedge.serve``; the CLI stopped paying for a web framework it does not use.
    """
    loaded = _modules_after("import kedge.agent")

    assert "kedge.server" not in loaded
    assert "fastapi" not in loaded


def test_importing_the_server_does_not_drag_the_agent_in_either() -> None:
    """The guarantee ``server/agent_seam.py`` exists for, and the reason the turn vocabulary is
    not under ``agent/``.

    The server ships a scripted stand-in so the whole UI can be built, exercised and judged with
    no model endpoint -- and ``--demo`` runs it. Putting the shared vocabulary under ``agent/``
    would have been *legal*, since the server is above the agent and may import it, and it would
    have quietly ended that: ``kedge/agent/__init__.py`` eagerly aggregates the package, so
    ``from kedge.agent.events import ...`` in `server/events.py` would construct
    ``KedgeAgent``'s module and everything under it on the way to a pydantic model. Below both is
    the placement that costs neither side anything, and `CLAUDE.md` says as much about
    ``reconcile/`` in the same paragraph as the ladder.
    """
    loaded = _modules_after("import kedge.server")

    assert "kedge.agent" not in loaded
    assert "kedge.agent.loop" not in loaded


def test_the_composition_module_is_the_one_place_that_holds_both() -> None:
    """And it works: importing it loads an agent and a server, which is what it is for."""
    loaded = _modules_after("import kedge.serve")

    assert {"kedge.agent.loop", "kedge.server.app"} <= loaded
