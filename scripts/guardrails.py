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
        f"outbound TLS trust decided in {HTTPX_HOMES[0]}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
