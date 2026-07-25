"""The notebook ``.py`` as a document: read it, edit it, write it back, with no kernel.

:mod:`kedge.notebook.driver` reaches the notebook through ``marimo._code_mode``, a private API
whose docstring promises nothing. This module is the other half of PLAN 6.1 mitigation 4 — the
half that survives that API being renamed or deleted — and :mod:`kedge.notebook.filedriver` is
the bridge built on top of it.

**What it is allowed to depend on.** marimo 0.23.15 ships *no* public notebook serialiser. The
whole of ``parse_notebook``, ``generate_filecontents``, ``MarimoConvert`` and the
``NotebookSerialization`` IR lives under ``marimo._ast`` and ``marimo._convert`` — every one of
them behind a leading underscore, and therefore exactly as unguaranteed as ``_code_mode`` itself.
Using them would buy a fallback with the same failure mode as the thing it is a fallback for, so
this module does not use them. It depends on two things instead:

* **the notebook file format**, which is not an API at all — it is the artifact the user opens,
  edits and commits to version control, and marimo cannot change it without breaking every
  notebook in existence. It is read here with the standard library's :mod:`ast` and
  :mod:`tokenize`, and written back in the same shape marimo writes it;
* **``marimo.App`` and ``marimo.Cell``**, which *are* public — both are named in marimo's
  top-level ``__all__``. :func:`analyse_cells` registers each cell body on a throwaway ``App``
  and reads ``Cell.defs`` and ``Cell.refs`` back, so the definitions and references kedge reasons
  about are computed by marimo's own analyser rather than by a reimplementation of it. No kernel
  is involved: the values are available from a static parse.

**Surgery, not regeneration.** A write splices cell blocks into the original text and leaves
everything else — the header comments and any PEP 723 block, the ``marimo.App(...)`` constructor,
``with app.setup:``, ``@app.function``, ``app._unparsable_cell(...)``, the ``__main__`` footer,
and any comment between cells — byte-for-byte alone. kedge owns the ``@app.cell`` blocks and
nothing else, because a notebook contains constructs this module does not model and destroying
them silently would be worse than not writing at all.

Within a cell block, the ``def`` line and the trailing ``return`` are regenerated on every write
from the computed references and definitions, which is what marimo does on every save; the body
is carried across verbatim, dedented by exactly one indent level on the way in and indented by
exactly one on the way out, so the round trip is lossless for any body marimo wrote.

**Not modelled.** Only ``@app.cell`` blocks are cells here. A ``@app.function`` or
``@app.class_definition`` contributes its name to the notebook's definitions, and a
``with app.setup:`` block contributes whatever it defines, but neither is listable or editable
through this module. They are preserved, not managed.
"""

from __future__ import annotations

import ast
import builtins
import io
import linecache
import logging
import os
import tempfile
import textwrap
import time
import tokenize
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kedge.errors import NotebookError
from kedge.notebook.model import UNNAMED_CELL

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "BUILTIN_NAMES",
    "CELL_INDENT",
    "CELL_SEPARATOR",
    "MAX_LINE_LENGTH",
    "CellAnalysis",
    "FileCell",
    "NotebookDocument",
    "NotebookFormatError",
    "analyse_cells",
    "analyse_document",
    "cycles_of",
    "dependency_map",
    "multiply_defined",
    "parse_notebook",
    "read_notebook",
    "render_notebook",
    "transitive",
    "write_atomically",
]

CELL_INDENT = "    "
"""One level of indentation inside a cell block. marimo's ``INDENT``, restated."""

CELL_SEPARATOR = "\n\n"
"""What sits between two cell blocks: two blank lines, as marimo writes them."""

MAX_LINE_LENGTH = 80
"""Where marimo wraps a generated signature or return tuple. Its ``MAX_LINE_LENGTH``."""

BUILTIN_NAMES = frozenset(vars(builtins)) | {
    "__builtin__",
    "__builtins__",
    "__class__",
    "__file__",
}
"""Names a cell may reference without taking them as parameters.

marimo keeps the same set in ``marimo._ast.variables.BUILTINS`` — the interpreter's builtins plus
the four module-level dunders that resolve without being defined anywhere. Restated from the
standard library rather than imported, because the point of this module is to have nothing
private to lose.
"""

_WRITE_RETRIES = 6
_WRITE_RETRY_DELAY = 0.05


class NotebookFormatError(NotebookError):
    """The file is not a marimo notebook this module can read or safely rewrite."""


# ── the document ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FileCell:
    """One ``@app.cell`` block, as a document record rather than a kernel object.

    ``code`` is the cell body with one indent level removed and the trailing ``return`` dropped,
    which is the same string ``marimo._code_mode`` reports as ``NotebookCell.code``. That
    equivalence is the whole point: a caller cannot tell which bridge produced it.

    Example:
        >>> FileCell(name="loader", code="frame = 1").is_named
        True
    """

    name: str
    code: str
    hide_code: bool = False
    disabled: bool = False
    column: int | None = None

    @property
    def is_named(self) -> bool:
        """Whether the cell carries a real name rather than marimo's unnamed placeholder."""
        return bool(self.name) and self.name != UNNAMED_CELL


@dataclass(frozen=True, slots=True)
class CellAnalysis:
    """What marimo's own static analysis says one cell body defines and references.

    ``parsed`` is ``False`` for a body that does not compile at all. Such a cell keeps its place
    in the document and its text is preserved, but it contributes nothing to the graph — the
    honest answer, rather than a guess.
    """

    defs: tuple[str, ...] = ()
    refs: tuple[str, ...] = ()
    coroutine: bool = False
    parsed: bool = True


@dataclass(frozen=True, slots=True)
class NotebookDocument:
    """A notebook file split into the part kedge owns and the parts it only carries.

    ``prefix``, ``separators`` and ``suffix`` are raw slices of the original file and are never
    interpreted. Rendering is ``prefix + cell + separator + cell + ... + suffix``, so anything
    this module does not understand survives a write unexamined.

    Example:
        >>> document = parse_notebook("import marimo\\napp = marimo.App()\\n")
        >>> document.cells
        ()
    """

    prefix: str
    cells: tuple[FileCell, ...] = ()
    separators: tuple[str, ...] = ()
    suffix: str = ""
    toplevel: tuple[str, ...] = ()
    setup_code: str | None = None

    def index_of(self, target: str | int) -> int | None:
        """Resolve a cell id, name, or index to a position, or ``None`` if nothing matches.

        Names win over ids, exactly as ``ctx.cells[key]`` resolves them. A file-bridge cell id is
        its position rendered as a string, which can never collide with a name because a cell
        name has to be a Python identifier.
        """
        if isinstance(target, int):
            return self._at(target)
        for index, cell in enumerate(self.cells):
            if cell.name == target and cell.is_named:
                return index
        if target.lstrip("-").isdigit():
            return self._at(int(target))
        # Last: the unnamed placeholder. Every cell marimo loaded as ``def _():`` answers to it,
        # so the first one wins -- which is what ``ctx.cells["_"]`` does too.
        return next((index for index, cell in enumerate(self.cells) if cell.name == target), None)

    def _at(self, position: int) -> int | None:
        resolved = position + len(self.cells) if position < 0 else position
        return resolved if 0 <= resolved < len(self.cells) else None

    def with_cell(self, index: int, cell: FileCell) -> NotebookDocument:
        """Return a copy with the cell at ``index`` replaced."""
        cells = list(self.cells)
        cells[index] = cell
        return replace(self, cells=tuple(cells))

    def inserted(self, cell: FileCell, index: int) -> NotebookDocument:
        """Return a copy with ``cell`` placed at position ``index``."""
        if not self.cells:
            prefix = self.prefix.rstrip("\n") + "\n" + CELL_SEPARATOR
            suffix = CELL_SEPARATOR + self.suffix.lstrip("\n") if self.suffix.strip() else ""
            return replace(self, prefix=prefix, cells=(cell,), separators=(), suffix=suffix)
        separators = list(self.separators)
        separators.insert(min(index, len(separators)), CELL_SEPARATOR)
        cells = list(self.cells)
        cells.insert(index, cell)
        return replace(self, cells=tuple(cells), separators=tuple(separators))

    def without(self, index: int) -> NotebookDocument:
        """Return a copy with the cell at ``index`` removed, and one separator with it."""
        if len(self.cells) == 1:
            tail = self.suffix.lstrip("\n")
            prefix = self.prefix.rstrip("\n") + "\n"
            return replace(
                self,
                prefix=prefix if not tail else prefix + CELL_SEPARATOR,
                cells=(),
                separators=(),
                suffix=tail,
            )
        cells = list(self.cells)
        del cells[index]
        separators = list(self.separators)
        del separators[min(index, len(separators) - 1)]
        return replace(self, cells=tuple(cells), separators=tuple(separators))

    def moved(self, index: int, destination: int) -> NotebookDocument:
        """Return a copy with the cell at ``index`` reinserted at ``destination``."""
        cell = self.cells[index]
        return self.without(index).inserted(cell, destination)


# ── reading ──────────────────────────────────────────────────────────────────────────────────


def read_notebook(path: Path) -> NotebookDocument:
    """Read and parse the notebook at ``path``.

    Args:
        path: The notebook ``.py``.

    Returns:
        The parsed document.

    Raises:
        NotebookFormatError: The file is missing, unreadable, or not a marimo notebook.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = (
            f"cannot read the notebook at {path}: {exc}. The file bridge edits the notebook on "
            f"disk, so the file has to exist and be readable before any operation."
        )
        raise NotebookFormatError(msg) from exc
    return parse_notebook(source)


def parse_notebook(source: str) -> NotebookDocument:
    """Split notebook source into its cells and the text between them.

    Args:
        source: The contents of a marimo notebook ``.py``.

    Returns:
        The document.

    Raises:
        NotebookFormatError: The source does not parse, or does not construct a ``marimo.App``.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        msg = (
            f"the notebook does not parse as Python, so it cannot be edited as a file: {exc}. "
            f"Fix the file in the editor, or restore it from version control."
        )
        raise NotebookFormatError(msg) from exc

    if not _constructs_an_app(tree):
        msg = (
            "this file does not build a marimo app: no 'app = marimo.App(...)' statement was "
            "found at the top level. The file bridge only edits marimo notebooks."
        )
        raise NotebookFormatError(msg)

    lines = source.splitlines(keepends=True)
    cells: list[FileCell] = []
    spans: list[tuple[int, int]] = []
    toplevel: list[str] = []
    setup_code: str | None = None

    for node in tree.body:
        if isinstance(node, ast.With | ast.AsyncWith) and _is_setup_block(node):
            setup_code = _body_source(lines, node)
            continue
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        decorator = _app_decorator(node)
        if decorator is None:
            continue
        if decorator != "cell":
            toplevel.append(node.name)
            continue
        if isinstance(node, ast.ClassDef):  # pragma: no cover - marimo never writes this shape
            continue
        start = node.decorator_list[0].lineno - 1
        stop = _block_stop(lines, node)
        hide_code, disabled, column = _decorator_options(node)
        cells.append(
            FileCell(
                name=node.name,
                code=_body_source(lines, node, drop_return=True),
                hide_code=hide_code,
                disabled=disabled,
                column=column,
            )
        )
        spans.append((start, stop))

    return _document_from_spans(lines, cells, spans, tuple(toplevel), setup_code)


def _document_from_spans(
    lines: list[str],
    cells: list[FileCell],
    spans: list[tuple[int, int]],
    toplevel: tuple[str, ...],
    setup_code: str | None,
) -> NotebookDocument:
    if not cells:
        anchor = _main_guard_line(lines)
        return NotebookDocument(
            prefix="".join(lines[:anchor]),
            suffix="".join(lines[anchor:]),
            toplevel=toplevel,
            setup_code=setup_code,
        )
    separators = [
        "".join(lines[spans[index][1] : spans[index + 1][0]]) for index in range(len(spans) - 1)
    ]
    return NotebookDocument(
        prefix="".join(lines[: spans[0][0]]),
        cells=tuple(cells),
        separators=tuple(separators),
        suffix="".join(lines[spans[-1][1] :]),
        toplevel=toplevel,
        setup_code=setup_code,
    )


def _constructs_an_app(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        function = node.value.func
        if isinstance(function, ast.Attribute) and function.attr == "App":
            return True
    return False


def _main_guard_line(lines: list[str]) -> int:
    for index, line in enumerate(lines):
        if line.startswith('if __name__ == "__main__":') or line.startswith(
            "if __name__ == '__main__':"
        ):
            return index
    return len(lines)


def _app_decorator(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str | None:
    """Which ``@app.<kind>`` decorator this definition carries, if any."""
    for decorator in node.decorator_list:
        expression = decorator.func if isinstance(decorator, ast.Call) else decorator
        if (
            isinstance(expression, ast.Attribute)
            and isinstance(expression.value, ast.Name)
            and expression.value.id == "app"
        ):
            return expression.attr
    return None


def _is_setup_block(node: ast.With | ast.AsyncWith) -> bool:
    for item in node.items:
        expression = (
            item.context_expr.func if isinstance(item.context_expr, ast.Call) else item.context_expr
        )
        if (
            isinstance(expression, ast.Attribute)
            and isinstance(expression.value, ast.Name)
            and expression.value.id == "app"
            and expression.attr == "setup"
        ):
            return True
    return False


def _decorator_options(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[bool, bool, int | None]:
    """``hide_code``, ``disabled`` and ``column``, read off the ``@app.cell(...)`` decorator.

    Anything else in the decorator is ignored rather than guessed at: a keyword a later marimo
    adds is preserved only in the sense that this module rewrites the decorator from what it
    understood, so an unknown keyword is dropped. That is the one place cell configuration is
    lossy, and it is why the parser is conservative about which keywords it recognises.
    """
    hide_code = False
    disabled = False
    column: int | None = None
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        for keyword in decorator.keywords:
            if not isinstance(keyword.value, ast.Constant):
                continue
            value = keyword.value.value
            if keyword.arg == "hide_code":
                hide_code = bool(value)
            elif keyword.arg == "disabled":
                disabled = bool(value)
            elif keyword.arg == "column" and isinstance(value, int) and not isinstance(value, bool):
                column = value
    return hide_code, disabled, column


def _header_end(lines: list[str], node: ast.stmt) -> int:
    """0-based index of the line carrying the ``:`` that closes a block header.

    Tokenised rather than guessed at, because a signature can wrap across lines and an annotation
    or a lambda default puts a colon inside the parentheses. The first colon at bracket depth zero
    is the one that ends the header.
    """
    fragment = "".join(lines[node.lineno - 1 : node.end_lineno])
    depth = 0
    try:
        for token in tokenize.generate_tokens(io.StringIO(fragment).readline):
            if token.type != tokenize.OP:
                continue
            if token.string in "([{":
                depth += 1
            elif token.string in ")]}":
                depth -= 1
            elif token.string == ":" and depth == 0:
                return node.lineno - 1 + token.end[0] - 1
    except (tokenize.TokenError, IndentationError):  # pragma: no cover - defensive
        logger.debug("could not tokenise a block header at line %d", node.lineno)
    body = getattr(node, "body", None)
    return (body[0].lineno - 2) if body else node.lineno - 1


def _block_stop(lines: list[str], node: ast.stmt) -> int:
    """0-based exclusive end of a block, including trailing comments at body indentation.

    The AST stops at the last statement, so a comment written after it belongs to nobody. marimo
    keeps such comments with the cell; so does this, by walking on through blank and indented
    lines and then back to the last line with content.
    """
    stop = node.end_lineno or node.lineno
    limit = stop
    for index in range(stop, len(lines)):
        line = lines[index]
        if line.strip() and not line.startswith((" ", "\t")):
            break
        if line.strip():
            limit = index + 1
    return max(stop, limit)


def _body_source(lines: list[str], node: ast.stmt, *, drop_return: bool = False) -> str:
    """The text of a block's body, dedented by exactly one indent level.

    Exactly one level, not the common prefix: marimo writes a cell with
    ``textwrap.indent(code, "    ")``, which prefixes every line that has content — including the
    interior lines of a triple-quoted string. Removing the common prefix would leave such a body
    indented and the next write would indent it again, so the inverse of what marimo did is the
    only lossless rule.
    """
    start = _header_end(lines, node) + 1
    stop = _block_stop(lines, node)
    body = getattr(node, "body", ())
    if drop_return and body and isinstance(body[-1], ast.Return):
        stop = min(stop, body[-1].lineno - 1)
    text = "".join(_strip_one_indent(line) for line in lines[start:stop])
    return text.rstrip("\n")


def _strip_one_indent(line: str) -> str:
    removed = 0
    while removed < len(CELL_INDENT) and removed < len(line) and line[removed] in " \t":
        removed += 1
    return line[removed:]


# ── static analysis, through marimo's public Cell ────────────────────────────────────────────

_ANALYSIS_PREAMBLE = "import marimo\napp = marimo.App()\n"
_ANALYSIS_PREFIX = "_kedge_cell_"


def analyse_cells(codes: Sequence[str]) -> tuple[CellAnalysis, ...]:
    """Return what each cell body defines and references, per marimo's own analyser.

    Each body is wrapped in a function, decorated with ``@app.cell`` on a throwaway
    ``marimo.App``, and the resulting ``marimo.Cell`` is asked for ``defs`` and ``refs``. Both
    names are in marimo's top-level ``__all__``, and neither needs a kernel: the values come from
    the static parse marimo does at decoration time.

    The synthetic module is registered in :mod:`linecache` under a one-shot name rather than
    written to disk, because ``@app.cell`` retrieves the decorated function's source through
    :mod:`inspect` and a function compiled from a string it cannot find has no source at all. The
    entry is removed again before returning.

    Args:
        codes: Cell bodies, in notebook order.

    Returns:
        One analysis per body, in the same order. A body that does not compile comes back with
        ``parsed=False`` and no definitions rather than a guess.
    """
    if not codes:
        return ()

    flavours = [_cell_flavour(code) for code in codes]
    parts = [_ANALYSIS_PREAMBLE]
    for index, (code, flavour) in enumerate(zip(codes, flavours, strict=True)):
        if flavour is None:
            continue
        parts.append(
            f"\n@app.cell\n{flavour} {_ANALYSIS_PREFIX}{index}():\n"
            f"{textwrap.indent(code, CELL_INDENT)}\n{CELL_INDENT}pass\n"
        )
    source = "".join(parts)
    namespace = _run_analysis_module(source)
    if namespace is None:
        return tuple(CellAnalysis(parsed=False) for _ in codes)

    results: list[CellAnalysis] = []
    for index, flavour in enumerate(flavours):
        cell = namespace.get(f"{_ANALYSIS_PREFIX}{index}")
        if flavour is None or cell is None:
            results.append(CellAnalysis(parsed=False))
            continue
        results.append(
            CellAnalysis(
                defs=tuple(sorted(cell.defs)),
                refs=tuple(sorted(cell.refs)),
                coroutine=flavour.startswith("async"),
            )
        )
    return tuple(results)


def _cell_flavour(code: str) -> str | None:
    """``def``, ``async def``, or ``None`` when the body does not compile at all.

    A marimo cell may ``await`` at its top level, which is legal in the notebook file only
    because the enclosing function is declared ``async``. Trying the plain form first keeps every
    ordinary cell on the ordinary path.

    Compiled all the way to bytecode rather than only to an AST, and deliberately so:
    ``PyCF_ONLY_AST`` skips the symbol-table pass, and "await outside async function" is raised by
    that pass. A full compile still executes nothing.
    """
    body = f"{textwrap.indent(code, CELL_INDENT)}\n{CELL_INDENT}pass\n"
    for flavour in ("def", "async def"):
        try:
            compile(f"{flavour} _probe():\n{body}", "<kedge-cell>", "exec", dont_inherit=True)
        except (SyntaxError, ValueError):
            continue
        return flavour
    return None


def _run_analysis_module(source: str) -> dict[str, Any] | None:
    name = f"<kedge-notebook-{uuid.uuid4().hex}>"
    linecache.cache[name] = (len(source), None, source.splitlines(keepends=True), name)
    namespace: dict[str, Any] = {"__name__": "kedge_notebook_analysis"}
    try:
        exec(compile(source, name, "exec"), namespace)
    except Exception as exc:
        logger.warning(
            "marimo could not analyse the notebook's cells, so its graph will be empty: %s", exc
        )
        return None
    finally:
        linecache.cache.pop(name, None)
    return namespace


def analyse_document(document: NotebookDocument) -> tuple[tuple[CellAnalysis, ...], frozenset[str]]:
    """Analyse every cell, plus whatever a ``with app.setup:`` block defines.

    Returns:
        The per-cell analyses and the set of names already bound outside the cells — the setup
        block's definitions and any ``@app.function`` or ``@app.class_definition``. Those names
        are in scope everywhere, so a cell that references one does not take it as a parameter.
    """
    codes = [cell.code for cell in document.cells]
    extra = [document.setup_code] if document.setup_code is not None else []
    results = analyse_cells([*codes, *extra])
    toplevel = set(document.toplevel)
    if extra:
        toplevel |= set(results[-1].defs)
    return tuple(results[: len(codes)]), frozenset(toplevel)


# ── the dataflow graph, computed from the source ─────────────────────────────────────────────


def dependency_map(analyses: Sequence[CellAnalysis]) -> dict[int, set[int]]:
    """Map each cell's position to the positions of the cells that depend on it.

    A cell is a child of every cell defining a name it references, which is marimo's edge rule
    stated over the same definitions and references marimo computed.
    """
    definers: dict[str, set[int]] = {}
    for index, analysis in enumerate(analyses):
        for name in analysis.defs:
            definers.setdefault(name, set()).add(index)
    children: dict[int, set[int]] = {index: set() for index in range(len(analyses))}
    for index, analysis in enumerate(analyses):
        for name in analysis.refs:
            for parent in definers.get(name, ()):
                if parent != index:
                    children[parent].add(index)
    return children


def transitive(edges: dict[int, set[int]], start: int) -> set[int]:
    """Every node reachable from ``start``, excluding ``start`` itself unless it is in a cycle."""
    seen: set[int] = set()
    pending = list(edges.get(start, ()))
    while pending:
        node = pending.pop()
        if node in seen:
            continue
        seen.add(node)
        pending.extend(edges.get(node, ()))
    return seen


def cycles_of(analyses: Sequence[CellAnalysis]) -> tuple[tuple[int, ...], ...]:
    """Every group of cells that transitively depend on one another."""
    children = dependency_map(analyses)
    found: list[tuple[int, ...]] = []
    grouped: set[int] = set()
    for index in range(len(analyses)):
        if index in grouped:
            continue
        reachable = transitive(children, index)
        if index not in reachable:
            continue
        cycle = tuple(
            sorted({index} | {node for node in reachable if index in transitive(children, node)})
        )
        grouped.update(cycle)
        found.append(cycle)
    return tuple(found)


def multiply_defined(analyses: Sequence[CellAnalysis]) -> dict[str, tuple[int, ...]]:
    """Names defined by more than one cell, mapped to the positions defining them."""
    definers: dict[str, list[int]] = {}
    for index, analysis in enumerate(analyses):
        for name in analysis.defs:
            definers.setdefault(name, []).append(index)
    return {name: tuple(where) for name, where in definers.items() if len(where) > 1}


# ── writing ──────────────────────────────────────────────────────────────────────────────────


def render_notebook(
    document: NotebookDocument,
    analyses: Sequence[CellAnalysis],
    toplevel: frozenset[str],
) -> str:
    """Render the document back to notebook source.

    Every cell's ``def`` line and trailing ``return`` are regenerated from ``analyses``, matching
    what marimo writes on its own saves: parameters are the references the cell does not already
    have in scope, and the return tuple is the definitions some other cell actually uses. Bodies
    and everything outside a cell block are copied through untouched.
    """
    all_defs = {name for analysis in analyses for name in analysis.defs}
    used = frozenset(name for analysis in analyses for name in analysis.refs)
    base = frozenset(toplevel | (BUILTIN_NAMES - all_defs))

    parts = [document.prefix]
    for index, (cell, analysis) in enumerate(zip(document.cells, analyses, strict=True)):
        if index:
            parts.append(document.separators[index - 1])
        parts.append(
            _render_cell(cell, analysis, allowed=base | frozenset(analysis.defs), used=used)
        )
    parts.append(document.suffix)
    return "".join(parts)


def _render_cell(
    cell: FileCell, analysis: CellAnalysis, *, allowed: frozenset[str], used: frozenset[str]
) -> str:
    parameters = tuple(
        name for name in analysis.refs if name not in allowed and name.isidentifier()
    )
    returns = tuple(name for name in analysis.defs if name in used)
    prefix = "async " if analysis.coroutine else ""
    block = [
        _decorator(cell),
        _format_elements(f"{prefix}def {cell.name}(...):", parameters),
    ]
    body = textwrap.indent(cell.code, CELL_INDENT)
    if body:
        block.append(body)
    if _needs_trailing_blank_line(cell.code):
        block.append("")
    block.append(_format_elements("return (...)", returns, indent=True, naked=True))
    return "\n".join(block) + "\n"


def _decorator(cell: FileCell) -> str:
    options: list[str] = []
    if cell.column is not None:
        options.append(f"column={cell.column}")
    if cell.disabled:
        options.append("disabled=True")
    if cell.hide_code:
        options.append("hide_code=True")
    if not options:
        return "@app.cell"
    return _format_elements("@app.cell(...)", tuple(options))


def _format_elements(
    template: str, elements: Sequence[str], *, indent: bool = False, naked: bool = False
) -> str:
    """Fill ``(...)`` in ``template`` with ``elements``, wrapping the way marimo wraps.

    A faithful restatement of marimo's ``format_tuple_elements``: a bare tuple where one is
    allowed, a trailing comma on a single element, and one element per line once the result would
    reach :data:`MAX_LINE_LENGTH`. Matching it is what keeps a file kedge wrote and a file marimo
    wrote from differing over formatting alone.
    """
    shift = (lambda text: textwrap.indent(text, CELL_INDENT)) if indent else (lambda text: text)
    if not elements:
        if naked:
            return shift(template.replace("(...)", "").rstrip())
        return shift(template.replace("(...)", "()"))

    items = tuple(elements)
    if naked and len(items) == 1:
        naked = False
        items = (f"{items[0]},",)

    joined = ", ".join(items)
    attempt = (
        template.replace("(...)", joined).rstrip()
        if naked
        else template.replace("(...)", f"({joined})")
    )
    attempt = shift(attempt)
    if len(attempt) < MAX_LINE_LENGTH:
        return attempt

    if len(items) == 1:
        items = (items[0].strip(","),)
    multiline = "\n".join(["(", textwrap.indent(",\n".join(items), CELL_INDENT) + ",", ")"])
    return shift(template.replace("(...)", multiline))


def _needs_trailing_blank_line(code: str) -> bool:
    """Whether marimo would put a blank line before the ``return``.

    It does so when the body ends in an import, a function, or a class, so that the file the user
    opens is formatted the way a formatter would leave it.
    """
    try:
        module = ast.parse(code)
    except SyntaxError:
        return False
    if not module.body:
        return False
    last = module.body[-1]
    if not isinstance(
        last, ast.Import | ast.ImportFrom | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    ):
        return False
    return last.end_lineno == code.rstrip().count("\n") + 1


def write_atomically(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` in one step, never leaving a partial file behind.

    ``marimo edit --watch`` is reading this file on a timer, and a notebook caught halfway
    through a write is a notebook that does not parse. The content goes to a temporary file in
    the same directory, is flushed and fsynced, and is then moved over the target — an atomic
    rename on every filesystem kedge runs on.

    The move is retried briefly because Windows refuses to replace a file another process has
    open, and the watcher opens this one about once a second.

    Args:
        path: The notebook ``.py``.
        text: Its complete new contents.

    Raises:
        NotebookFormatError: The notebook could not be written.
    """
    handle, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".kedge-tmp", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        _replace_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _replace_with_retry(temporary: Path, path: Path) -> None:
    for attempt in range(_WRITE_RETRIES):
        try:
            temporary.replace(path)
        except PermissionError:
            if attempt == _WRITE_RETRIES - 1:
                msg = (
                    f"could not replace {path}: another process is holding it open. marimo's "
                    f"file watcher reads the notebook about once a second, so retry the "
                    f"operation; if it keeps failing, close anything else editing the file."
                )
                raise NotebookFormatError(msg) from None
            time.sleep(_WRITE_RETRY_DELAY)
        except OSError as exc:
            msg = f"could not write the notebook to {path}: {exc}"
            raise NotebookFormatError(msg) from exc
        else:
            return
