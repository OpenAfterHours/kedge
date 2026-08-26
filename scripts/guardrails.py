"""Enforce the import and boundary invariants `CONVENTIONS.md` calls non-negotiable.

Both are written down as greps, and both are wrong as greps. ``import pandas`` appears in
`agent/validate.py` docstrings, in the `scaffold.py` message raised when generated code tries it,
and across `tests/unit/test_agent_validate.py`, whose whole subject is that the string is
rejected. ``marimo._code_mode`` is named in a dozen module docstrings that exist to explain why
that module does *not* import it.

Parsing each file and looking at its import statements is the check the prose actually means: a
docstring is not an import, and neither is a string literal handed to another interpreter.

Note that `notebook/driver.py` does import ``marimo._code_mode`` for real, twice, inside
:func:`check_bridge` and :func:`_check_packages` -- the preflight that proves the private API
still has the shape kedge assumes (PLAN 6.1 mitigation 5). Introspecting an API requires importing
it. The enforceable rule is the one CONVENTIONS.md leads with: **exactly one module**, and that
module is `driver.py`.

The third rule is the same shape one layer out. ``kedge.marimo_http`` is the single point of
contact for marimo's HTTP API, so that a marimo release moving an endpoint costs one file rather
than a hunt; ``notebook/kernel.py`` is the one deliberate exception, because streaming
``POST /api/kernel/execute`` asynchronously is a different shape from the rest, and even it
imports the path and the headers from `marimo_http`. Enforced by looking for the ``/api/kernel/``
prefix in live string literals -- docstrings excluded, since half a dozen of them name the
endpoints in order to explain the boundary. It is deliberately narrower than the prose invariant:
``/health`` and ``/sse`` are too generic to ban (kedge's own server serves ``/api/health``), so a
new module could still health-poll marimo without tripping this. The two authenticated POSTs are
the ones that matter, and they are covered.

The fourth rule is not in CONVENTIONS.md's numbered list, because it is not about a library
boundary -- it is the layering `CLAUDE.md` states in one line: ``analysis/ -> plan/ -> notebook/
-> agent/ -> server/``. A layer may import what is below it and nothing above it. The agent reads
and writes the notebook; the notebook knows nothing about the agent. A conversion driver was once
built under `notebook/` and had to import ``kedge.agent.context``, ``kedge.agent.prompts`` and
``kedge.agent.validate`` to work, and there was no runtime cycle only because
`notebook/__init__.py` did not import it. Adding that one line reproduced ``ImportError: cannot
import name 'CellFacts' from partially initialized module 'kedge.agent.context'`` -- and the blast
radius was the whole `agent` package rather than the one module, because `agent/__init__.py`
eagerly aggregates `context`, so *every* import starting inside `kedge.agent` failed. An inversion
that costs nothing until the day somebody adds an import is exactly the kind that comes back, so it
is checked here rather than remembered.

For a while only that one edge was checked, because it was the only one that held. All four are
checked now. The last to be cleared was `agent/ -> server/`, where the loop imported the server's
event vocabulary at module scope: the deferred ``kedge.server.app`` imports below it were there to
keep FastAPI out of ``import kedge.agent``, and did not, because ``from kedge.server.events import
...`` executes ``kedge/server/__init__.py`` and that imports `app.py` anyway. The vocabulary moved
to ``kedge.turn``, below both layers; the wiring moved to ``kedge.serve``, above both. A top-level
module is not itself walked -- `kedge/turn.py`, `kedge/sql.py` and their neighbours sit below the
ladder and are reachable from every rung, which is the point of putting a shared vocabulary there.
`kedge/serve.py` is the exception that proves it: it sits *above* the ladder, importing an agent
and a server in the same breath, so no rung may import it and :data:`ABOVE_LADDER` says so.

Run directly, or via CI::

    uv run python scripts/guardrails.py
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# CONVENTIONS.md 1: polars, never pandas -- in src/, tests, fixtures, or generated code.
BANNED_ANYWHERE = "pandas"
PANDAS_ROOTS = ("src", "tests", "utils")

# CONVENTIONS.md 2: marimo._code_mode is imported in exactly one module. Everywhere else reaches
# the kernel over HTTP, so that a marimo release withdrawing the private API costs kedge a swap
# of one file rather than a rewrite (PLAN 6.1).
BANNED_MODULE = "marimo._code_mode"
CODE_MODE_ROOTS = ("src",)
CODE_MODE_HOME = "src/kedge/notebook/driver.py"

# CONVENTIONS.md 6: marimo's HTTP API is spoken by one module, with one named exception.
KERNEL_API_PREFIX = "/api/kernel/"
KERNEL_API_ROOTS = ("src",)
KERNEL_API_HOMES = ("src/kedge/marimo_http.py", "src/kedge/notebook/kernel.py")

# CONVENTIONS.md 7: certificate trust for the model endpoint is decided in kedge/tls.py alone.
# Two shapes, because there are two ways to end up on certifi by accident. Building a raw httpx
# client is the first: httpx defaults to certifi, which never contains the root a TLS-inspecting
# proxy re-signs with, so a new module doing its own outbound call would reintroduce the failure
# tls.py exists to fix. The loopback homes are exempt -- they speak plain HTTP to 127.0.0.1 and
# no certificate is involved. The second is constructing an openai client without handing it a
# client we built: the SDK makes its own otherwise, and it too defaults to certifi.
HTTPX_CLIENT_NAMES = ("Client", "AsyncClient")
HTTPX_ROOTS = ("src",)
HTTPX_HOMES = (
    "src/kedge/tls.py",
    "src/kedge/marimo_http.py",
    "src/kedge/notebook/kernel.py",
)
OPENAI_CLIENT_NAMES = ("OpenAI", "AsyncOpenAI")
OPENAI_ROOTS = ("src",)

# CLAUDE.md's layering, bottom first: analysis/ -> plan/ -> notebook/ -> agent/ -> server/. Every
# upward edge is refused, not just the one that was once inverted. A layer may import anything
# below it and nothing above it. A top-level module that is *below* the ladder -- `kedge/sql.py`,
# `kedge/errors.py`, `kedge/turn.py` -- is not on it and is reachable from every rung, which is the
# whole point of putting a shared vocabulary there.
LAYERS: tuple[str, ...] = ("analysis", "plan", "notebook", "agent", "server")
LAYER_PARENT = "src/kedge"
LAYER_PACKAGE = "kedge"

# One top-level module is *above* the ladder rather than below it, and that is a different thing
# entirely. `kedge/serve.py` composes an agent with a server, so it holds both ends of one arrow at
# once; a layer importing it would inherit an edge to every layer with nothing to report it, since
# the rungs above only bans modules under `src/kedge/<layer>/`. That is not hypothetical: with the
# helpers in `agent/loop.py` a `notebook/` module reaching them had to write `from kedge.agent
# import serve`, which this check caught. Moving them to a top-level module moved them out of its
# reach, so the module is ranked here and every rung is refused it -- `server/` included, because
# the server is a layer and the composition of it is not.
ABOVE_LADDER: tuple[str, ...] = (f"{LAYER_PACKAGE}.serve",)


@dataclass(frozen=True, slots=True)
class Breach:
    """One import that a non-negotiable forbids."""

    path: Path
    line: int
    statement: str
    rule: str

    def render(self) -> str:
        """A single line, formatted so an editor can jump to it."""
        relative = self.path.relative_to(REPO_ROOT).as_posix()
        return f"{relative}:{self.line}: {self.statement} -- {self.rule}"


def _imported_names(tree: ast.AST) -> list[tuple[int, str, str]]:
    """Every module named by an import statement, as (line, module, rendered statement)."""
    found: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((node.lineno, alias.name, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            # `from . import x` has no module; a relative import cannot reach pandas or marimo.
            if node.level or node.module is None:
                continue
            names = ", ".join(alias.name for alias in node.names)
            found.append((node.lineno, node.module, f"from {node.module} import {names}"))
    return found


def _is_or_is_under(module: str, root: str) -> bool:
    """True when `module` is `root` itself or a submodule of it."""
    return module == root or module.startswith(f"{root}.")


def _python_files(roots: tuple[str, ...]) -> list[Path]:
    """Every `.py` file under the given repo-relative roots, `__pycache__` excluded."""
    files: list[Path] = []
    for root in roots:
        directory = REPO_ROOT / root
        if not directory.is_dir():
            continue
        files.extend(
            path for path in sorted(directory.rglob("*.py")) if "__pycache__" not in path.parts
        )
    return files


def _check(
    roots: tuple[str, ...], banned: str, rule: str, *, home: str | None = None
) -> list[Breach]:
    """Collect every import of `banned` (or a submodule of it) beneath `roots`.

    Args:
        roots: Repo-relative directories to walk.
        banned: The module that may not be imported.
        rule: The convention to quote when reporting a breach.
        home: The one repo-relative file permitted to import it, if any.

    Returns:
        Every offending import, in file then line order.
    """
    breaches: list[Breach] = []
    for path in _python_files(roots):
        if home is not None and path.relative_to(REPO_ROOT).as_posix() == home:
            continue
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            # A file that will not parse is a failure in its own right, and silently skipping it
            # would let a breach hide behind a typo.
            breaches.append(
                Breach(path=path, line=exc.lineno or 0, statement="could not parse", rule=str(exc))
            )
            continue
        breaches.extend(
            Breach(path=path, line=line, statement=statement, rule=rule)
            for line, module, statement in _imported_names(tree)
            if _is_or_is_under(module, banned)
        )
    return breaches


def _docstring_ids(tree: ast.AST) -> set[int]:
    """The identities of every string constant that is a docstring rather than a value.

    This codebase leans hard on the attribute-docstring idiom -- a bare string literal after an
    assignment -- as well as ordinary module, class and function docstrings, and all of them
    discuss marimo's endpoints freely. Prose about a boundary is not a crossing of it.
    """
    found: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        # `ast.IfExp` and friends carry a single expression under `body`, not a statement list.
        if not isinstance(body, list):
            continue
        for statement in body:
            if (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            ):
                found.add(id(statement.value))
    return found


def _check_kernel_api() -> list[Breach]:
    """Collect every live string literal naming a marimo kernel endpoint outside its two homes."""
    rule = (
        f"marimo's HTTP API is spoken by {KERNEL_API_HOMES[0]} alone, with "
        f"{KERNEL_API_HOMES[1]} as the one deliberate exception "
        "(CONVENTIONS.md non-negotiable 6)"
    )
    breaches: list[Breach] = []
    for path in _python_files(KERNEL_API_ROOTS):
        if path.relative_to(REPO_ROOT).as_posix() in KERNEL_API_HOMES:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue  # already reported by the import checks, which parse the same files
        docstrings = _docstring_ids(tree)
        breaches.extend(
            Breach(
                path=path,
                line=node.lineno,
                statement=f"string literal contains {KERNEL_API_PREFIX!r}",
                rule=rule,
            )
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and KERNEL_API_PREFIX in node.value
            and id(node) not in docstrings
        )
    return breaches


def _package_of(path: Path) -> str:
    """The dotted package a module lives in, so a relative import can be resolved against it."""
    relative = path.relative_to(REPO_ROOT / "src")
    # The last part is the module file either way: `kedge/notebook/scaffold.py` is in package
    # `kedge.notebook`, and so is `kedge/notebook/__init__.py`, which *is* that package.
    return ".".join(relative.parts[:-1])


def _absolute_module(node: ast.ImportFrom, package: str) -> str | None:
    """What ``from ... import x`` actually names, relative imports resolved.

    The other checks skip relative imports on the stated ground that one cannot reach pandas or
    marimo. A relative import *can* reach `kedge.agent` from `kedge.notebook` -- ``from ..agent
    .validate import validate_cell`` is two dots away -- so this rule resolves them rather than
    trusting the house style to keep using absolute ones.
    """
    if not node.level:
        return node.module
    parts = package.split(".")
    climbed = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
    if not climbed:
        return None
    return ".".join([*climbed, node.module]) if node.module else ".".join(climbed)


def _import_targets(node: ast.AST, package: str) -> tuple[str, ...]:
    """Every dotted path one import statement could be naming, in full.

    Two shapes reach `kedge.agent` from `kedge.notebook` without the string ``kedge.agent`` ever
    appearing as an import's *module*, and both are ordinary Python somebody writes without
    thinking:

    * ``from kedge import agent``. The module is ``kedge``; the dotted path is only complete once
      the alias is appended. Reading the module alone -- which `_imported_names` does, correctly,
      for a rule about `pandas` -- lets this straight through, and the guardrail then prints its
      guarantee over a tree where the guarantee is false.
    * ``from .. import agent``, the same statement spelled relatively.

    So every alias contributes ``module.alias`` as well as the module itself. That means
    ``from kedge.agent.context import CellFacts`` also offers ``kedge.agent.context.CellFacts``,
    which is a class rather than a module -- harmless, because a candidate that is not under the
    banned root never matches, and this one was already caught by its module.

    Args:
        node: Any AST node. Anything that is not an import contributes nothing.
        package: The dotted package the file lives in, for resolving a relative import.

    Returns:
        Every candidate, the module first.
    """
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)
    if not isinstance(node, ast.ImportFrom):
        return ()
    module = _absolute_module(node, package)
    if module is None:
        return ()
    return (module, *(f"{module}.{alias.name}" for alias in node.names))


def _render_import(node: ast.AST, package: str) -> str:
    """One import statement as a line an editor can jump to, the relative form resolved."""
    if isinstance(node, ast.Import):
        return "import " + ", ".join(alias.name for alias in node.names)
    if not isinstance(node, ast.ImportFrom):
        return "import"
    names = ", ".join(alias.name for alias in node.names)
    return f"from {_absolute_module(node, package)} import {names}"


def layers_above(layer: str) -> tuple[str, ...]:
    """The dotted modules `layer` may not import, highest last.

    Args:
        layer: One of :data:`LAYERS`.

    Returns:
        Every layer above it plus everything in :data:`ABOVE_LADDER`, as importable module names
        -- ``("kedge.agent", "kedge.server", "kedge.serve")`` for ``notebook``. The top rung still
        gets ``ABOVE_LADDER``: `server/` is a layer, and the module that composes a server with an
        agent sits above it.
    """
    rank = LAYERS.index(layer)
    return (*(f"{LAYER_PACKAGE}.{name}" for name in LAYERS[rank + 1 :]), *ABOVE_LADDER)


def _layering_rule(layer: str, banned: str) -> str:
    """The sentence quoted when `layer` is caught importing `banned`."""
    if banned in ABOVE_LADDER:
        return (
            f"{LAYER_PARENT}/{layer}/ may not import {banned}: it composes one layer with another "
            "and so sits above the whole ladder, and a rung reaching it inherits an edge to every "
            "layer that no rung-to-rung check can see (CLAUDE.md, 'Architecture in one paragraph')"
        )
    return (
        f"{LAYER_PARENT}/{layer}/ may not import {banned}: the layering is "
        + " -> ".join(f"{name}/" for name in LAYERS)
        + ", and a layer may import what is below it and nothing above it "
        "(CLAUDE.md, 'Architecture in one paragraph')"
    )


def _check_layering() -> list[Breach]:
    """Collect every import that reaches back up the layering, at every rung of it.

    The ladder was once enforced one edge at a time -- ``notebook/`` may not import
    ``kedge.agent`` -- because that was the edge somebody had actually inverted and the rest of
    the ladder did not hold. It holds now, so all of it is checked: an invariant enforced where it
    has already been broken catches the breach that has happened rather than the one that is
    coming.

    **There is no ``TYPE_CHECKING`` exemption, and the seam that would have wanted one no longer
    does.** ``kedge.agent.loop`` implements :class:`kedge.server.agent_seam.AgentLoop`, a Protocol
    declared one layer above it, and that is a legitimate inversion: an interface belongs to the
    caller that needs it, and nothing under `agent/` imports it, because the loop satisfies it
    structurally. What the loop *did* import from up there was the seam's data -- the turn request
    and the cancellation token -- which are arguments rather than an interface, and they moved down
    to ``kedge.turn`` where the events a turn yields back already sit. So the rule stays absolute:
    an import guarded by ``if TYPE_CHECKING:`` cannot create a runtime cycle and is reported all
    the same. The inversion this check exists for began as type-only references and grew runtime
    ones, and a rule with an exemption is a rule people learn to phrase their way past.

    **A composition module is refused from every rung, including the top one.** :data:`ABOVE_LADDER`
    is checked alongside the rungs because a rule that only compares one layer against another has
    a blind spot the width of a top-level module: `kedge/serve.py` imports an agent *and* a server,
    so a rung importing it reaches both, and every rung-to-rung comparison reports clean. Planted
    in a scratch tree, ``from kedge import serve`` inside `notebook/` reproduced exactly the
    partial-initialisation ImportError quoted at the top of this file while both this check and a
    test written to back it up said nothing.

    What the check cannot see, stated rather than assumed: it reads ``import`` statements, so
    ``importlib.import_module("kedge.server.app")``, ``__import__``, a name reached through
    ``exec``, and a ``.pyi`` stub are all invisible to it. Nothing in the tree does any of those,
    and a rule that catches every import somebody writes without thinking is the one worth having;
    a rule that also catches the ones somebody wrote *on purpose* to get past it is a different
    project.
    """
    breaches: list[Breach] = []
    for layer in LAYERS:
        banned_here = layers_above(layer)
        for path in _python_files((f"{LAYER_PARENT}/{layer}",)):
            package = _package_of(path)
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except SyntaxError:
                continue  # already reported by the import checks, which parse the same files
            for node in ast.walk(tree):
                if not isinstance(node, ast.Import | ast.ImportFrom):
                    continue
                targets = _import_targets(node, package)
                breaches.extend(
                    Breach(
                        path=path,
                        line=node.lineno,
                        statement=_render_import(node, package),
                        rule=_layering_rule(layer, banned),
                    )
                    for banned in banned_here
                    if any(_is_or_is_under(target, banned) for target in targets)
                )
    return sorted(breaches, key=lambda breach: (breach.path, breach.line))


def _outbound_trust() -> list[Breach]:
    """Collect every outbound client built somewhere that cannot have decided what it trusts.

    Both halves are about the same accident: a default-constructed client verifies against
    ``certifi``, which is a fixed list of public roots, so on a machine behind a TLS-inspecting
    proxy it fails with an error that says nothing about proxies. ``kedge.tls`` reads the
    operating system's store instead, and this keeps everything pointed through it.
    """
    httpx_rule = (
        f"outbound HTTP clients are built in {HTTPX_HOMES[0]}, so they verify against the "
        "operating system trust store rather than certifi (CONVENTIONS.md non-negotiable 7)"
    )
    openai_rule = (
        "an openai client must be given http_client=kedge.tls.client(...) or "
        "kedge.tls.async_client(...); without it the SDK builds its own against certifi "
        "(CONVENTIONS.md non-negotiable 7)"
    )
    breaches: list[Breach] = []
    for path in _python_files(HTTPX_ROOTS):
        relative = path.relative_to(REPO_ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue  # already reported by the import checks, which parse the same files
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            called = node.func
            name = called.attr if isinstance(called, ast.Attribute) else None
            if isinstance(called, ast.Name):
                name = called.id
            if name is None:
                continue
            if (
                name in HTTPX_CLIENT_NAMES
                and isinstance(called, ast.Attribute)
                and isinstance(called.value, ast.Name)
                and called.value.id == "httpx"
                and relative not in HTTPX_HOMES
            ):
                breaches.append(
                    Breach(
                        path=path,
                        line=node.lineno,
                        statement=f"httpx.{name}(...) built outside {HTTPX_HOMES[0]}",
                        rule=httpx_rule,
                    )
                )
            if name in OPENAI_CLIENT_NAMES and not any(
                keyword.arg == "http_client" for keyword in node.keywords
            ):
                breaches.append(
                    Breach(
                        path=path,
                        line=node.lineno,
                        statement=f"{name}(...) constructed with no http_client",
                        rule=openai_rule,
                    )
                )
    return sorted(breaches, key=lambda breach: (breach.path, breach.line))


def main() -> int:
    """Report every breach and return a process exit code."""
    breaches = [
        *_check(
            PANDAS_ROOTS,
            BANNED_ANYWHERE,
            "polars, never pandas (CONVENTIONS.md non-negotiable 1)",
        ),
        *_check(
            CODE_MODE_ROOTS,
            BANNED_MODULE,
            f"marimo._code_mode is imported in {CODE_MODE_HOME} and nowhere else "
            "(CONVENTIONS.md non-negotiable 2)",
            home=CODE_MODE_HOME,
        ),
        *_check_kernel_api(),
        *_outbound_trust(),
        *_check_layering(),
    ]

    if breaches:
        print(f"guardrails: {len(breaches)} breach(es) of a stated non-negotiable\n")
        for breach in breaches:
            print(f"  {breach.render()}")
        print("\nSee CONVENTIONS.md. These are not style preferences; they are load-bearing.")
        return 1

    print(
        f"guardrails: no pandas import anywhere; marimo._code_mode confined to {CODE_MODE_HOME}; "
        f"{KERNEL_API_PREFIX} confined to {', '.join(KERNEL_API_HOMES)}; "
        f"outbound TLS trust decided in {HTTPX_HOMES[0]}; "
        "no upward import anywhere in "
        + " -> ".join(f"{name}/" for name in LAYERS)
        + f", nor any import of {', '.join(ABOVE_LADDER)} from a rung of it."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
