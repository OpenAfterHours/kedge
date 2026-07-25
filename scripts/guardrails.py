"""Enforce the two import invariants `CONVENTIONS.md` calls non-negotiable.

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
    ]

    if breaches:
        print(f"guardrails: {len(breaches)} breach(es) of a stated non-negotiable\n")
        for breach in breaches:
            print(f"  {breach.render()}")
        print("\nSee CONVENTIONS.md. These are not style preferences; they are load-bearing.")
        return 1

    print(f"guardrails: no pandas import anywhere; marimo._code_mode confined to {CODE_MODE_HOME}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
