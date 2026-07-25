"""Catalogue the user-editable ``utils/`` package for the agent's ``list_utils`` tool.

The maintainability argument for kedge rests on the model reusing what already exists rather
than reinventing it in every notebook. It can only do that if it knows what exists, which
means a catalogue that is **always** in context -- so this is name, signature, and the first
line of the docstring, and nothing else. A few hundred tokens for a package of a dozen
helpers, against several thousand for the source.

The catalogue is built by **parsing, never importing**. Three reasons, all of them practical:
``utils/`` is user-editable and a syntax error in it must not take down the agent; importing
would execute module-level code written by someone who did not expect the agent to run it;
and ``utils/`` sits outside the installed package, so it is frequently not importable from
wherever kedge happens to be running. ``ast`` has none of those problems.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["UtilFunction", "UtilsCatalogue", "build_catalogue"]

_PRIVATE_PREFIX = "_"
_DUNDER_INIT = "__init__.py"


@dataclass(frozen=True, slots=True)
class UtilFunction:
    """One public function in the utils package."""

    module: str
    name: str
    signature: str
    """Parameters and return annotation, parentheses included: ``'(frame: pl.DataFrame) -> str'``."""
    summary: str
    is_async: bool = False

    @property
    def qualified(self) -> str:
        """The importable name, e.g. ``'utils.frames.normalise_headers'``."""
        return f"{self.module}.{self.name}"

    def render(self) -> str:
        """Return the two-line catalogue entry: the call, then what it does."""
        prefix = "async " if self.is_async else ""
        return f"{prefix}{self.qualified}{self.signature}\n    {self.summary}"


@dataclass(frozen=True, slots=True)
class UtilsCatalogue:
    """Every public function in the utils package, with enough to decide whether to use one.

    Example:
        >>> catalogue = build_catalogue(Path("utils"))
        >>> print(catalogue.render())
        # utils - reusable helpers, importable from any notebook cell.
        utils.frames.normalise_headers(frame: pl.DataFrame) -> pl.DataFrame
            Return a frame with its column names normalised to snake_case.
    """

    package: str
    functions: tuple[UtilFunction, ...] = ()

    @property
    def is_empty(self) -> bool:
        """Whether the package holds no public functions."""
        return not self.functions

    def modules(self) -> list[str]:
        """Return the module names present, in catalogue order."""
        seen: list[str] = []
        for function in self.functions:
            if function.module not in seen:
                seen.append(function.module)
        return seen

    def function(self, name: str) -> UtilFunction | None:
        """Return a function by bare or qualified name, or ``None``."""
        return next((f for f in self.functions if name in (f.name, f.qualified)), None)

    def render(self) -> str:
        """Return the catalogue as text, compact enough to include in every turn."""
        if self.is_empty:
            return (
                f"# {self.package} - no reusable helpers yet. Anything worth using twice "
                f"belongs here."
            )
        lines = [
            f"# {self.package} - reusable helpers, importable from any notebook cell.",
            "# Prefer one of these to writing the same thing again.",
        ]
        for module in self.modules():
            lines.append("")
            for function in self.functions:
                if function.module == module:
                    lines.append(function.render())
        return "\n".join(lines)

    def to_rows(self) -> list[dict[str, str]]:
        """Return one plain dict per function, for a table widget or JSON."""
        return [
            {
                "name": f.qualified,
                "signature": f.signature,
                "summary": f.summary,
            }
            for f in self.functions
        ]


def _summary_of(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Return the first line of a function's docstring, or a placeholder."""
    docstring = ast.get_docstring(node, clean=True)
    if not docstring:
        return "(no docstring)"
    first = docstring.strip().splitlines()[0].strip()
    return first or "(no docstring)"


def _signature_of(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Return the parameter list and return annotation, exactly as written."""
    try:
        parameters = ast.unparse(node.args)
    except (AttributeError, ValueError):  # pragma: no cover - defensive
        parameters = "..."
    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return f"{parameters}){returns}"[:-1] + ")" + returns if False else f"{parameters}){returns}"


def _module_functions(path: Path, *, package: str) -> list[UtilFunction]:
    """Parse one module and return its public top-level functions."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("skipping unreadable utils module %s: %s", path, exc)
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        # utils/ is user-editable. A syntax error there is the user's problem to fix, not a
        # reason for the agent to lose its catalogue of everything else.
        logger.warning("skipping utils module %s: %s", path, exc)
        return []

    module = package if path.name == _DUNDER_INIT else f"{package}.{path.stem}"
    functions: list[UtilFunction] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.name.startswith(_PRIVATE_PREFIX):
            continue
        functions.append(
            UtilFunction(
                module=module,
                name=node.name,
                signature=_signature_of(node),
                summary=_summary_of(node),
                is_async=isinstance(node, ast.AsyncFunctionDef),
            )
        )
    return functions


def build_catalogue(package_dir: Path, *, package: str | None = None) -> UtilsCatalogue:
    """Build the catalogue by parsing every module in a package directory.

    Args:
        package_dir: The package root, normally ``utils/``. A directory that does not exist
            yields an empty catalogue rather than an error.
        package: Import name to render; defaults to the directory name.

    Returns:
        The :class:`UtilsCatalogue`, ordered by module then by source order within a module,
        so it renders identically between runs.
    """
    name = package or package_dir.name
    if not package_dir.is_dir():
        logger.debug("no utils package at %s", package_dir)
        return UtilsCatalogue(package=name)

    functions: list[UtilFunction] = []
    modules = sorted(package_dir.glob("*.py"), key=lambda p: (p.name != _DUNDER_INIT, p.name))
    for path in modules:
        functions.extend(_module_functions(path, package=name))

    logger.info("catalogued %d util function(s) from %s", len(functions), package_dir)
    return UtilsCatalogue(package=name, functions=tuple(functions))
