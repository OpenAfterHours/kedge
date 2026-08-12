"""The one module in kedge that speaks ``marimo._code_mode`` — and it does so by generating text.

``marimo._code_mode`` is a private, explicitly unversioned API. PLAN 6.1 makes it the project's
top risk and CONVENTIONS non-negotiable 2 confines it here, so that a marimo change is one file's
problem. This module is that file.

**The split.** ``_code_mode`` only works inside a marimo kernel: outside one, ``get_context()``
raises ``RuntimeError("code mode requires a running kernel context")``. So kedge does not call it
— kedge *writes Python that calls it* and posts that source to the kernel's execute endpoint.
:mod:`kedge.notebook.kernel` carries the source over HTTP and hands back what came out. This
module builds the source and parses the reply. Code generation is therefore a pure function of
its arguments, which is why ``tests/unit/test_driver_codegen.py`` asserts on generated strings
with no server involved.

Consequently, kedge's own process never imports ``_code_mode`` to *use* it. The single import is
inside :func:`check_bridge`, is function-local, and exists only so :func:`inspect.signature` can
confirm the surface still looks the way kedge expects before a conversation starts rather than
after (PLAN 6.1 mitigation 5).

**Getting results back.** Scratchpad execution discards new top-level bindings, so anything kedge
wants must be printed. But stdout is shared: ``_code_mode``'s flush prints its own summary there
(``created and ran cell 'UMIT' (kedge_cell)``), and any cell that runs during the call streams its
console output through the same channel. Generated programs therefore emit a delimited JSON
payload — :data:`RESULT_BEGIN`, one line of ``json.dumps``, :data:`RESULT_END` — and
:func:`extract_payload` reads only between the markers. Nothing is inferred from the shape or
wording of the surrounding text, because that wording is marimo's to change.

**Embedding.** Generated source carries user and model code as string literals inside a string.
Every embedded value goes through :func:`py_literal`, which uses :func:`repr` (whose contract is
that ``eval(repr(x)) == x``) and then re-reads its own output with :func:`ast.literal_eval` to
prove the round trip before the source is allowed anywhere near the kernel. No manual escaping,
anywhere, ever: it is the likeliest place in kedge for a silent, catastrophic bug.

**Staleness.** ``edit_cell`` refuses to overwrite a cell the agent has not read at its current
version. marimo's read tracker lives on the kernel, not on the context object, so a read recorded
by :meth:`NotebookDriver.get_cell` in one turn still counts in the next — which is exactly the
"user edited a cell between turns" protection PLAN 6.2 asks for. ``skip_staleness_check`` is never
passed anywhere in this module; grep for it.
"""

from __future__ import annotations

import ast
import inspect
import json
import keyword
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from kedge.errors import KedgeError, KernelError, NotebookError, ValidationError
from kedge.notebook.kernel import DEFAULT_TIMEOUT_SECONDS, KernelClient
from kedge.notebook.model import (
    CellErrorInfo,
    CellInfo,
    CellRef,
    DefinitionConflict,
    ExecResult,
    GraphNode,
    GraphView,
    MutationResult,
    NotebookBridge,
    PackageResult,
    ProbeResult,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from kedge.workspace import Workspace

logger = logging.getLogger(__name__)

__all__ = [
    "MARIMO_PIN",
    "RESULT_BEGIN",
    "RESULT_END",
    "BridgeReport",
    "BridgeVersionError",
    "CellNameError",
    "CellNotFoundError",
    "CellSyntaxError",
    "CycleDetectedError",
    "EmbeddingError",
    "KernelExecutionError",
    "MultiplyDefinedError",
    "NotebookDriver",
    "StaleCellError",
    "check_bridge",
    "extract_payload",
    "py_literal",
    "validate_cell_name",
    "verify_bridge",
]

MARIMO_PIN = "0.23.15"
"""The marimo version this bridge was verified against (docs/marimo-api.md)."""

_NOT_INSTALLED = "not installed"
"""Stands in for a version string when marimo is absent, so the report is still a report."""

RESULT_BEGIN = "<<<KEDGE-RESULT-BEGIN>>>"
RESULT_END = "<<<KEDGE-RESULT-END>>>"

MAX_CELL_NAME_LENGTH = 64
OUTPUT_PREVIEW_LIMIT = 2000
PROBE_VALUE_LIMIT = 4000


# ── errors ───────────────────────────────────────────────────────────────────────────────────


class EmbeddingError(KedgeError):
    """A value could not be embedded in generated source as a Python literal."""


class BridgeVersionError(NotebookError):
    """The installed marimo does not expose the surface this bridge was written against."""


class CellNameError(KernelError):
    """A cell name is unusable: malformed, reserved, or already taken by another cell."""


class CellNotFoundError(KernelError):
    """No cell in the notebook matches the given id, name, or index."""


class StaleCellError(KernelError):
    """The target cell changed since kedge last read it, so the edit was refused.

    Retryable, and worth retrying: re-read the cell, show the user's version to the model, and
    resubmit. Suppressing it would silently discard whatever the user typed between turns.
    """

    retryable = True

    def __init__(
        self, message: str, *, cell_id: str | None = None, stale: Sequence[str] = ()
    ) -> None:
        super().__init__(message)
        self.cell_id = cell_id
        self.stale_cells: tuple[str, ...] = tuple(stale)


class MultiplyDefinedError(ValidationError):
    """Generated code broke marimo's single-definition rule.

    marimo enforces this kernel-side at flush time and reports it as a ``RuntimeError`` with a
    formatted list. :attr:`conflicts` is that list parsed, so the validation gate can present
    violations rather than a traceback.
    """

    def __init__(self, message: str, conflicts: Sequence[DefinitionConflict] = ()) -> None:
        super().__init__(message)
        self.conflicts: tuple[DefinitionConflict, ...] = tuple(conflicts)


class CycleDetectedError(ValidationError):
    """Generated code would introduce a cycle in the notebook's dataflow graph."""


class CellSyntaxError(ValidationError):
    """Generated code did not compile during marimo's dry-run check, so nothing was applied."""


class KernelExecutionError(KernelError):
    """Code reached the kernel and failed there in a way with no more specific type."""

    def __init__(self, message: str, *, error_type: str = "", traceback_text: str = "") -> None:
        super().__init__(message)
        self.error_type = error_type
        self.traceback_text = traceback_text


# ── embedding ────────────────────────────────────────────────────────────────────────────────


def py_literal(value: object) -> str:
    """Render ``value`` as Python source that evaluates back to it.

    Uses :func:`repr`, whose documented contract for the types kedge embeds is that the result is
    a valid literal, and then verifies that claim with :func:`ast.literal_eval` before returning.
    The verification is the point: a value that cannot be embedded faithfully must fail loudly
    here, in kedge's process, rather than producing source that compiles into something subtly
    different inside the kernel.

    Args:
        value: A string, number, bool, ``None``, or a list/tuple/dict/set of those.

    Returns:
        Source text for the value.

    Raises:
        EmbeddingError: The value has no literal form, or the round trip did not match.
    """
    text = repr(value)
    try:
        restored = ast.literal_eval(text)
    except (ValueError, SyntaxError, MemoryError, RecursionError) as exc:
        msg = (
            f"cannot embed a {type(value).__name__} in generated code: its repr() is not a Python "
            f"literal. Only strings, numbers, bools, None and containers of those can be sent to "
            f"the kernel."
        )
        raise EmbeddingError(msg) from exc
    if type(restored) is not type(value) or restored != value:
        msg = (
            f"refusing to embed a {type(value).__name__} in generated code: it did not survive a "
            f"repr/literal_eval round trip, so the kernel would receive a different value."
        )
        raise EmbeddingError(msg)
    return text


def _kwarg(name: str, value: object) -> str:
    return f"{name}={py_literal(value)}"


# ── cell names ───────────────────────────────────────────────────────────────────────────────


def validate_cell_name(name: str) -> str:
    """Check that ``name`` is usable as a marimo cell name, returning it unchanged.

    Named cells are the only stable way to address a cell across turns — ids are short opaque
    tokens and a rename can mint a new one (PLAN 1.1). The name also becomes a function name in
    the notebook file on disk, which is why it must be a plain identifier.

    Args:
        name: The proposed cell name.

    Returns:
        The name, unchanged, when it is valid.

    Raises:
        CellNameError: The name is empty, not an identifier, a keyword, a dunder, private, or
            too long.
    """
    if not name:
        msg = "a cell name is required: unnamed cells cannot be addressed reliably across turns"
        raise CellNameError(msg)
    if not name.isidentifier():
        msg = (
            f"cell name {name!r} is not a valid Python identifier. Cell names become function "
            f"names in the notebook file, so they must be plain identifiers."
        )
        raise CellNameError(msg)
    if keyword.iskeyword(name):
        msg = f"cell name {name!r} is a Python keyword and cannot be used as a function name"
        raise CellNameError(msg)
    if name.startswith("__") and name.endswith("__"):
        msg = f"cell name {name!r} is a dunder; those are reserved and must not be used for cells"
        raise CellNameError(msg)
    if name.startswith("_"):
        msg = (
            f"cell name {name!r} starts with an underscore. marimo reports unnamed cells as '_' "
            f"and treats leading-underscore names as private, so such a name is not addressable."
        )
        raise CellNameError(msg)
    if len(name) > MAX_CELL_NAME_LENGTH:
        msg = f"cell name {name!r} is longer than {MAX_CELL_NAME_LENGTH} characters"
        raise CellNameError(msg)
    return name


# ── code generation ──────────────────────────────────────────────────────────────────────────

_HELPERS = """

def _kedge_status(value):
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _kedge_errors(cell):
    found = []
    for err in cell.errors:
        describe = getattr(err, "describe", None)
        found.append(
            {
                "kind": type(err).__name__,
                "message": describe() if callable(describe) else str(err),
            }
        )
    return found


def _kedge_describe(cell, include_code):
    config = cell.config
    info = {
        "id": str(cell.id),
        "name": cell.name,
        "status": _kedge_status(cell.status),
        "hide_code": bool(getattr(config, "hide_code", False)),
        "disabled": bool(getattr(config, "disabled", False)),
        "column": getattr(config, "column", None),
        "errors": _kedge_errors(cell),
        "code": None,
        "output_mimetype": None,
        "output_preview": None,
    }
    output = cell.output
    if output is not None:
        info["output_mimetype"] = _kedge_status(getattr(output, "mimetype", None))
        info["output_preview"] = str(getattr(output, "data", ""))[:<<PREVIEW>>]
    if include_code:
        info["code"] = cell.code
    return info

""".replace("<<PREVIEW>>", str(OUTPUT_PREVIEW_LIMIT))


def build_program(
    operation: str,
    body: str,
    *,
    extra_imports: Sequence[str] = (),
    helpers: bool = True,
) -> str:
    """Wrap an operation body in the standard preamble, guard, and result protocol.

    The shape is fixed for every ``_code_mode`` operation: build a payload dict, run the body
    inside ``try``, convert any exception into a structured error rather than a traceback, and
    print the payload between the sentinels. The final statement is a ``flush()`` returning
    ``None`` so the kernel's rendered value for the scratch cell stays empty rather than showing
    the character count of the last write.

    Args:
        operation: Name recorded in the payload, echoed back for sanity checking.
        body: Source for the operation, at indent zero. Placed inside the ``try``.
        extra_imports: Additional import lines for the preamble.
        helpers: Whether to include the cell-description helpers.

    Returns:
        A complete Python program ready to POST to the kernel.
    """
    op = py_literal(operation)
    lines = [
        "import json as _kedge_json",
        "import sys as _kedge_sys",
        "import traceback as _kedge_traceback",
        "",
        "import marimo._code_mode as _kedge_cm",
        *extra_imports,
    ]
    if helpers:
        lines.append(_HELPERS)
    lines += [
        "",
        f'_kedge_payload = {{"op": {op}, "ok": True, "data": {{}}}}',
        "try:",
        *_indent(body),
        "except Exception as _kedge_exc:",
        "    _kedge_payload = {",
        f'        "op": {op},',
        '        "ok": False,',
        '        "error": {',
        '            "type": type(_kedge_exc).__name__,',
        '            "module": type(_kedge_exc).__module__,',
        '            "message": str(_kedge_exc),',
        '            "traceback": _kedge_traceback.format_exc(),',
        '            "cell_id": getattr(_kedge_exc, "cell_id", None),',
        '            "stale_cells": sorted(getattr(_kedge_exc, "stale_cells", None) or ()),',
        "        },",
        "    }",
        *_emit_payload(),
    ]
    return "\n".join(lines) + "\n"


def _emit_payload() -> list[str]:
    return [
        f'_kedge_sys.stdout.write("\\n" + {py_literal(RESULT_BEGIN)} + "\\n")',
        "_kedge_sys.stdout.write(_kedge_json.dumps(_kedge_payload, default=repr))",
        f'_kedge_sys.stdout.write("\\n" + {py_literal(RESULT_END)} + "\\n")',
        "_kedge_sys.stdout.flush()",
    ]


def _indent(body: str, prefix: str = "    ") -> list[str]:
    return [prefix + line if line.strip() else line for line in body.rstrip("\n").split("\n")]


def _readback(lookup: str, *, include_code: bool = False) -> list[str]:
    """Source that re-enters a fresh context to read a cell's post-run state.

    A cell's status, errors and output are a frozen snapshot taken when the context was entered,
    so they are stale by the time ``run_cell`` has finished inside that same context. Re-entering
    is marimo's documented way to see the new values.
    """
    return [
        'if _kedge_payload["ok"]:',
        "    async with _kedge_cm.get_context() as _kedge_ctx:",
        "        try:",
        f'            _kedge_payload["data"]["cell"] = _kedge_describe('
        f"_kedge_ctx.cells[{lookup}], {py_literal(include_code)})",
        "        except KeyError:",
        '            _kedge_payload["data"]["cell"] = None',
    ]


def generate_list_cells(*, with_code: bool) -> str:
    """Generate the program that enumerates every cell.

    ``with_code`` is not a convenience flag. Touching a cell's ``code`` is what records a read
    for marimo's staleness guard, so listing without code deliberately leaves the guard armed for
    every cell. Enumeration is explicit for the same reason ``repr(ctx.cells)`` is banned: that
    repr reads every cell's code and would disarm the guard notebook-wide.
    """
    body = "\n".join(
        [
            "async with _kedge_cm.get_context() as _kedge_ctx:",
            "    _kedge_found = []",
            "    for _kedge_cell in _kedge_ctx.cells:",
            f"        _kedge_found.append(_kedge_describe(_kedge_cell, {py_literal(with_code)}))",
            '    _kedge_payload["data"]["cells"] = _kedge_found',
        ]
    )
    return build_program("list_cells", body)


def generate_get_cell(target: str | int) -> str:
    """Generate the program that reads one cell, including its code."""
    body = "\n".join(
        [
            "async with _kedge_cm.get_context() as _kedge_ctx:",
            f'    _kedge_payload["data"]["cell"] = _kedge_describe('
            f"_kedge_ctx.cells[{py_literal(target)}], True)",
        ]
    )
    return build_program("get_cell", body)


def generate_create_cell(
    code: str,
    *,
    name: str,
    before: str | None = None,
    after: str | None = None,
    hide_code: bool = False,
    disabled: bool = False,
    column: int | None = None,
    run: bool = True,
) -> str:
    """Generate the program that creates a named cell.

    ``hide_code`` is passed explicitly on every call and defaults to ``False`` here, inverting
    marimo's own default of ``True``. Hiding the code would hide the model's work from the user,
    which is the one thing kedge exists to prevent.

    The name collision check runs kernel-side, inside the same context as the create, so the
    check and the write cannot be separated by someone else's edit. A collision reports a
    structured error and leaves the queue empty rather than raising, so no partial change is
    applied.
    """
    validate_cell_name(name)
    if before is not None and after is not None:
        msg = "give at most one of before= and after= when creating a cell"
        raise CellNameError(msg)

    arguments = [py_literal(code), _kwarg("name", name), _kwarg("hide_code", hide_code)]
    if disabled:
        arguments.append(_kwarg("disabled", disabled))
    if column is not None:
        arguments.append(_kwarg("column", column))
    if before is not None:
        arguments.append(_kwarg("before", before))
    if after is not None:
        arguments.append(_kwarg("after", after))

    body = [
        "async with _kedge_cm.get_context() as _kedge_ctx:",
        "    _kedge_taken = {}",
        "    for _kedge_cell in _kedge_ctx.cells:",
        "        if _kedge_cell.name:",
        "            _kedge_taken[_kedge_cell.name] = str(_kedge_cell.id)",
        f"    if {py_literal(name)} in _kedge_taken:",
        '        _kedge_payload["ok"] = False',
        '        _kedge_payload["error"] = {',
        '            "type": "CellNameInUse",',
        f'            "message": "cell name " + {py_literal(name)} '
        f'+ " is already used by cell " + _kedge_taken[{py_literal(name)}],',
        "        }",
        "    else:",
        f"        _kedge_cid = _kedge_ctx.create_cell({', '.join(arguments)})",
        '        _kedge_payload["data"]["cell_id"] = str(_kedge_cid)',
        f'        _kedge_payload["data"]["name"] = {py_literal(name)}',
        f'        _kedge_payload["data"]["ran"] = {py_literal(run)}',
    ]
    if run:
        body.append("        _kedge_ctx.run_cell(_kedge_cid)")
    body += _readback("_kedge_cid")
    return build_program("create_cell", "\n".join(body))


def generate_edit_cell(
    target: str | int,
    code: str | None = None,
    *,
    name: str | None = None,
    hide_code: bool | None = None,
    disabled: bool | None = None,
    column: int | None = None,
    run: bool = True,
    overwrite_unread: bool = False,
) -> str:
    """Generate the program that edits an existing cell.

    ``overwrite_unread`` decides whether the generated code touches ``ctx.cells[target].code``
    before editing. That read is what satisfies marimo's staleness guard, so:

    * ``False`` (the default) — no read happens in this call, and the edit therefore relies on a
      read recorded by an earlier ``get_cell`` or ``list_cells``. If the user changed the cell in
      between, marimo refuses and kedge raises :class:`StaleCellError`. This is the setting that
      makes the guard mean anything, which is why it is the default.
    * ``True`` — the code is read first and returned as ``previous_code``, which satisfies the
      guard honestly and lets the edit through. Use it when the model has just been shown the
      cell and is deliberately replacing it.

    ``skip_staleness_check`` is never used in either path: turning the guard off wholesale would
    disarm it for the rest of the session, not just this edit.
    """
    if name is not None:
        validate_cell_name(name)
    if code is None and name is None and hide_code is None and disabled is None and column is None:
        msg = "edit_cell needs something to change: pass code, name, hide_code, disabled or column"
        raise CellNameError(msg)

    arguments = ["_kedge_target"]
    if code is not None:
        arguments.append(py_literal(code))
    for key, value in (
        ("name", name),
        ("hide_code", hide_code),
        ("disabled", disabled),
        ("column", column),
    ):
        if value is not None:
            arguments.append(_kwarg(key, value))

    body = [
        "async with _kedge_cm.get_context() as _kedge_ctx:",
        # edit_cell resolves ids and names but not indexes, so normalise through the cells view.
        f"    _kedge_target = str(_kedge_ctx.cells[{py_literal(target)}].id)",
        '    _kedge_payload["data"]["cell_id"] = _kedge_target',
    ]
    if overwrite_unread and code is not None:
        body.append(
            '    _kedge_payload["data"]["previous_code"] = _kedge_ctx.cells[_kedge_target].code'
        )
    body.append(f"    _kedge_ctx.edit_cell({', '.join(arguments)})")
    body.append(f'    _kedge_payload["data"]["ran"] = {py_literal(run)}')
    if run:
        body.append("    _kedge_ctx.run_cell(_kedge_target)")

    # Renaming a cell can mint a new id, so read back by the new name when there is one.
    lookup = py_literal(name) if name is not None else "_kedge_target"
    body += _readback(lookup)
    return build_program("edit_cell", "\n".join(body))


def generate_delete_cell(target: str | int) -> str:
    """Generate the program that deletes a cell.

    The cell's graph descendants are reported alongside the deletion. Deleting a cell other cells
    depend on leaves them referring to a name nothing defines, and PLAN 6.2 asks for that to be
    visible rather than discovered by the user afterwards.
    """
    body = "\n".join(
        [
            "async with _kedge_cm.get_context() as _kedge_ctx:",
            f"    _kedge_cell = _kedge_ctx.cells[{py_literal(target)}]",
            "    _kedge_cid = str(_kedge_cell.id)",
            '    _kedge_payload["data"]["cell_id"] = _kedge_cid',
            '    _kedge_payload["data"]["name"] = _kedge_cell.name',
            '    _kedge_payload["data"]["descendants"] = sorted('
            "str(_kedge_d) for _kedge_d in _kedge_ctx.graph.descendants(_kedge_cell.id))",
            "    _kedge_ctx.delete_cell(_kedge_cid)",
        ]
    )
    return build_program("delete_cell", body)


def generate_move_cell(
    target: str | int, *, before: str | None = None, after: str | None = None
) -> str:
    """Generate the program that moves a cell before or after another."""
    if (before is None) == (after is None):
        msg = "give exactly one of before= and after= when moving a cell"
        raise CellNameError(msg)
    anchor = _kwarg("before", before) if before is not None else _kwarg("after", after)
    body = "\n".join(
        [
            "async with _kedge_cm.get_context() as _kedge_ctx:",
            f"    _kedge_target = str(_kedge_ctx.cells[{py_literal(target)}].id)",
            '    _kedge_payload["data"]["cell_id"] = _kedge_target',
            f"    _kedge_ctx.move_cell(_kedge_target, {anchor})",
            *_readback("_kedge_target"),
        ]
    )
    return build_program("move_cell", body)


def generate_run_cell(target: str | int) -> str:
    """Generate the program that re-runs an existing cell."""
    body = "\n".join(
        [
            "async with _kedge_cm.get_context() as _kedge_ctx:",
            f"    _kedge_target = str(_kedge_ctx.cells[{py_literal(target)}].id)",
            '    _kedge_payload["data"]["cell_id"] = _kedge_target',
            '    _kedge_payload["data"]["ran"] = True',
            "    _kedge_ctx.run_cell(_kedge_target)",
            *_readback("_kedge_target"),
        ]
    )
    return build_program("run_cell", body)


def generate_read_graph() -> str:
    """Generate the program that snapshots the dataflow graph.

    Nothing here touches any cell's ``code``, so reading the graph — unlike reading a cell — has
    no effect on the staleness guard. That makes it the cheap call for "what depends on what".
    """
    body = "\n".join(
        [
            "async with _kedge_cm.get_context() as _kedge_ctx:",
            "    _kedge_graph = _kedge_ctx.graph",
            "    _kedge_nodes = []",
            "    for _kedge_cell in _kedge_ctx.cells:",
            "        _kedge_impl = _kedge_graph.cells.get(_kedge_cell.id)",
            "        _kedge_nodes.append(",
            "            {",
            '                "id": str(_kedge_cell.id),',
            '                "name": _kedge_cell.name,',
            '                "status": _kedge_status(_kedge_cell.status),',
            '                "defs": sorted(_kedge_impl.defs) if _kedge_impl is not None else [],',
            '                "refs": sorted(_kedge_impl.refs) if _kedge_impl is not None else [],',
            '                "ancestors": sorted(str(_kedge_a) for _kedge_a in '
            "_kedge_graph.ancestors(_kedge_cell.id)) if _kedge_impl is not None else [],",
            '                "descendants": sorted(str(_kedge_d) for _kedge_d in '
            "_kedge_graph.descendants(_kedge_cell.id)) if _kedge_impl is not None else [],",
            "            }",
            "        )",
            '    _kedge_payload["data"]["nodes"] = _kedge_nodes',
            '    _kedge_payload["data"]["multiply_defined"] = sorted('
            "_kedge_graph.get_multiply_defined())",
            '    _kedge_payload["data"]["cycles"] = [',
            "        sorted({str(_kedge_c) for _kedge_edge in _kedge_cycle for _kedge_c in _kedge_edge})",
            "        for _kedge_cycle in _kedge_graph.cycles",
            "    ]",
        ]
    )
    return build_program("read_graph", body)


def generate_set_ui_value(element: str, value: Any) -> str:
    """Generate the program that sets a UI element's value.

    ``element`` is the *name of the notebook variable* holding the element, resolved through
    ``ctx.globals``. The scratchpad's copy of globals is not the kernel's, so a bare name would
    read a stale object; ``ctx.globals`` is marimo's documented way through.
    """
    if not element.isidentifier():
        msg = f"{element!r} is not a variable name; set_ui_value addresses elements by the name the notebook binds them to"
        raise CellNameError(msg)
    body = "\n".join(
        [
            "async with _kedge_cm.get_context() as _kedge_ctx:",
            f"    _kedge_element = _kedge_ctx.globals[{py_literal(element)}]",
            f"    _kedge_ctx.set_ui_value(_kedge_element, {py_literal(value)})",
            f'    _kedge_payload["data"]["element"] = {py_literal(element)}',
        ]
    )
    return build_program("set_ui_value", body)


def generate_notify(
    title: str, description: str, *, variant: str | None = None, kind: str = "banner"
) -> str:
    """Generate the program that pushes a notification into the notebook UI.

    Unlike every other mutator, ``broadcast_raw_notification`` fires immediately rather than
    being queued for the flush. It therefore still happens if the surrounding block later raises,
    which is exactly why it is worth saying out loud: a notification is not rolled back with the
    rest of the batch.
    """
    if kind not in ("banner", "alert"):
        msg = f"notification kind must be 'banner' or 'alert', not {kind!r}"
        raise CellNameError(msg)
    if variant is not None and variant != "danger":
        msg = f"notification variant must be 'danger' or None, not {variant!r}"
        raise CellNameError(msg)
    cls = "BannerNotification" if kind == "banner" else "AlertNotification"
    body = "\n".join(
        [
            "async with _kedge_cm.get_context() as _kedge_ctx:",
            f"    _kedge_ctx.broadcast_raw_notification({cls}(",
            f"        title={py_literal(title)},",
            f"        description={py_literal(description)},",
            f"        variant={py_literal(variant)},",
            "    ))",
            f'    _kedge_payload["data"]["kind"] = {py_literal(kind)}',
        ]
    )
    return build_program(
        "notify",
        body,
        extra_imports=[
            "from marimo._messaging.notification import AlertNotification, BannerNotification"
        ],
        helpers=False,
    )


def generate_add_packages(packages: Sequence[str], remove: Sequence[str] = ()) -> str:
    """Generate the program that installs or removes packages in the kernel's environment.

    ``packages.list()`` has to run before any add or remove in the same batch, so the
    before-state is captured first. A freshly installed package may not be importable by cells
    that already ran, because Python caches module lookups; the caller is expected to say so
    (PLAN 6.2).
    """
    if not packages and not remove:
        msg = "add_packages needs at least one package to add or remove"
        raise CellNameError(msg)
    body = [
        "async with _kedge_cm.get_context() as _kedge_ctx:",
        '    _kedge_payload["data"]["installed_before"] = sorted(',
        '        str(getattr(_kedge_p, "name", _kedge_p)) for _kedge_p in _kedge_ctx.packages.list()',
        "    )",
    ]
    if packages:
        body.append(f"    _kedge_ctx.packages.add({', '.join(py_literal(p) for p in packages)})")
    if remove:
        body.append(f"    _kedge_ctx.packages.remove({', '.join(py_literal(p) for p in remove)})")
    body.append(f'    _kedge_payload["data"]["added"] = {py_literal(list(packages))}')
    body.append(f'    _kedge_payload["data"]["removed"] = {py_literal(list(remove))}')
    return build_program("add_packages", "\n".join(body), helpers=False)


def generate_probe(code: str) -> str:
    """Generate a scratchpad program that evaluates ``code`` and returns its last value.

    The probe's own source is placed at module level, never indented into a ``try``. Indenting
    arbitrary source would silently rewrite the contents of any multi-line string literal in it —
    a corruption that produces valid syntax and wrong values, which is the worst kind. The cost
    is that a probe that raises emits no payload; the driver reads the failure off the SSE
    stream's ``stderr`` instead.

    If the last statement is an expression it is bound to a temporary so its value can be
    reported, which is what makes ``probe("df.height")`` behave the way a REPL would. The source
    is normalised through :func:`ast.unparse`, so it is re-emitted from a parsed tree rather than
    spliced as text.

    Args:
        code: Python source to evaluate against the notebook's globals.

    Returns:
        A complete program.

    Raises:
        CellSyntaxError: ``code`` does not parse.
    """
    try:
        tree = compile(
            code,
            "<kedge-probe>",
            "exec",
            flags=ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
            dont_inherit=True,
        )
    except SyntaxError as exc:
        msg = f"the probe does not parse: {exc}"
        raise CellSyntaxError(msg) from exc
    if not isinstance(tree, ast.Module):  # pragma: no cover - compile() always returns a Module
        msg = "the probe did not compile to a module"
        raise CellSyntaxError(msg)

    last = tree.body[-1] if tree.body else None
    has_value = isinstance(last, ast.Expr)
    if isinstance(last, ast.Expr):
        tree.body[-1] = ast.Assign(
            targets=[ast.Name(id="_kedge_value", ctx=ast.Store())],
            value=last.value,
        )
        ast.fix_missing_locations(tree)

    lines = [
        "import json as _kedge_json",
        "import sys as _kedge_sys",
        "",
        "_kedge_value = None",
        ast.unparse(tree),
        "",
        f"_kedge_text = repr(_kedge_value) if {py_literal(has_value)} else None",
        "_kedge_payload = {",
        '    "op": "probe",',
        '    "ok": True,',
        '    "data": {',
        f'        "value": None if _kedge_text is None else _kedge_text[:{PROBE_VALUE_LIMIT}],',
        f'        "truncated": bool(_kedge_text is not None and len(_kedge_text) > {PROBE_VALUE_LIMIT}),',
        f'        "type": type(_kedge_value).__name__ if {py_literal(has_value)} else None,',
        "    },",
        "}",
        *_emit_payload(),
    ]
    return "\n".join(lines) + "\n"


# ── reading the reply ────────────────────────────────────────────────────────────────────────


def extract_payload(stdout: str) -> dict[str, Any] | None:
    """Return the delimited JSON payload from a run's stdout, or ``None`` if there is none.

    The last complete pair of markers wins. Everything outside them — marimo's flush summary,
    anything a cell printed — is ignored rather than interpreted, so a change to marimo's summary
    wording cannot break result parsing.

    The markers can legitimately appear *inside* the payload, because the payload carries cell
    source and rendered output: a cell whose body or result contains the marker text puts it
    there. So a pair that fails to decode is retried against successively earlier opening
    markers before the closing marker is abandoned. Narrowing to the nearest opening marker and
    giving up would break reads of the whole notebook whenever one cell mentioned the sentinel.

    Args:
        stdout: Concatenated stdout from one execute call.

    Returns:
        The decoded payload, or ``None`` when no complete, well-formed payload is present.
    """
    end = stdout.rfind(RESULT_END)
    while end != -1:
        start = stdout.rfind(RESULT_BEGIN, 0, end)
        while start != -1:
            raw = stdout[start + len(RESULT_BEGIN) : end].strip()
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                logger.debug("discarding a malformed kedge payload of %d characters", len(raw))
                start = stdout.rfind(RESULT_BEGIN, 0, start)
                continue
            return decoded if isinstance(decoded, dict) else None
        end = stdout.rfind(RESULT_END, 0, end)
    return None


_CONFLICT_LINE = re.compile(r"^\s*-\s*'(?P<name>[^']+)'(?P<rest>.*)$")
_QUOTED = re.compile(r"'([^']+)'")
# Owning cells appear as a quoted id optionally followed by a parenthesised cell name, e.g.
# "in cell 'MJUe' (_)" or "'Ab12' (loader), 'Cd34'". Both forms identify a cell, so both are
# captured, in the order marimo wrote them.
_CELL_TOKEN = re.compile(r"'([^']+)'|\(([^)]*)\)")


def parse_definition_conflicts(message: str) -> tuple[DefinitionConflict, ...]:
    """Parse marimo's ``Multiply-defined names`` message into structured conflicts.

    The message lists one indented ``- 'name' is already defined in cell 'id' (name)`` line per
    violation. Only the quoted variable name is required; the rest is best-effort, so a change to
    the surrounding prose degrades the detail rather than the diagnosis.

    Cells are identified by both the quoted id and the parenthesised cell name, because marimo
    reports both and either may be the one a reader recognises — ids are opaque and churn, names
    are what kedge addresses cells by.
    """
    conflicts: list[DefinitionConflict] = []
    for line in message.splitlines():
        match = _CONFLICT_LINE.match(line)
        if match is None:
            continue
        cells = tuple(
            quoted or parenthesised
            for quoted, parenthesised in _CELL_TOKEN.findall(match.group("rest"))
        )
        conflicts.append(DefinitionConflict(name=match.group("name"), defined_in=cells))
    return tuple(conflicts)


def _raise_for_payload(payload: dict[str, Any], result: ExecResult) -> None:
    error = payload.get("error") or {}
    kind = str(error.get("type", ""))
    message = (
        str(error.get("message", "")).strip() or "the kernel reported a failure with no message"
    )
    traceback_text = str(error.get("traceback", ""))

    if kind == "StaleCellError":
        raise StaleCellError(
            f"{message}\nkedge will not force the write: re-read the cell so the user's version "
            f"is seen before it is replaced.",
            cell_id=error.get("cell_id"),
            stale=[str(c) for c in error.get("stale_cells") or ()],
        )
    if kind == "CellNameInUse":
        raise CellNameError(message)
    if message.startswith("Multiply-defined names:"):
        conflicts = parse_definition_conflicts(message)
        detail = "\n".join(f"  - {conflict}" for conflict in conflicts) or f"  {message}"
        raise MultiplyDefinedError(
            "the submitted code breaks marimo's single-definition rule, so nothing was applied:\n"
            + detail,
            conflicts,
        )
    if message.startswith("Cycles detected:"):
        raise CycleDetectedError(
            f"the submitted code would create a cycle in the notebook's dataflow graph, so "
            f"nothing was applied: {message}"
        )
    if kind == "SyntaxError":
        raise CellSyntaxError(
            f"the submitted code did not compile, so nothing was applied: {message}"
        )
    if kind == "KeyError" and "not found" in message:
        raise CellNotFoundError(message.strip('"'))
    if kind == "IndexError":
        # Addressing a cell by position is as legitimate as addressing it by name, so an index
        # past the end is the same class of mistake as an unknown name -- not a kernel fault.
        raise CellNotFoundError(f"no cell at that position: {message}")

    raise KernelExecutionError(
        f"{kind or 'error'} in the marimo kernel: {message}",
        error_type=kind,
        traceback_text=traceback_text or result.stderr_tail(),
    )


def _require_payload(result: ExecResult, operation: str) -> dict[str, Any]:
    payload = extract_payload(result.stdout)
    if payload is None:
        msg = (
            f"the kernel returned no result payload for {operation}. The program did not reach "
            f"its final print, which normally means the kernel was interrupted or restarted "
            f"mid-run.{_stderr_note(result)}"
        )
        raise KernelExecutionError(msg, traceback_text=result.stderr_tail())
    if not payload.get("ok", False):
        _raise_for_payload(payload, result)
    return payload


def _stderr_note(result: ExecResult) -> str:
    tail = result.stderr_tail()
    return f"\nThe kernel wrote to stderr:\n{tail}" if tail else ""


def _console_of(result: ExecResult) -> str:
    """Everything the kernel printed outside kedge's delimited payload."""
    start = result.stdout.rfind(RESULT_BEGIN)
    if start == -1:
        return result.stdout
    end = result.stdout.find(RESULT_END, start)
    tail = result.stdout[end + len(RESULT_END) :] if end != -1 else ""
    return (result.stdout[:start] + tail).strip("\n")


def _cell_from_dict(raw: dict[str, Any] | None) -> CellInfo | None:
    if not raw:
        return None
    return CellInfo(
        id=str(raw.get("id", "")),
        name=str(raw.get("name", "")),
        code=raw.get("code"),
        status=raw.get("status"),
        hide_code=bool(raw.get("hide_code", False)),
        disabled=bool(raw.get("disabled", False)),
        column=raw.get("column"),
        errors=tuple(
            CellErrorInfo(kind=str(e.get("kind", "")), message=str(e.get("message", "")))
            for e in raw.get("errors") or ()
        ),
        output_mimetype=raw.get("output_mimetype"),
        output_preview=raw.get("output_preview"),
    )


def _mutation_from_payload(
    operation: str, payload: dict[str, Any], result: ExecResult
) -> MutationResult:
    data = payload.get("data") or {}
    cell = _cell_from_dict(data.get("cell"))
    ref = cell.ref if cell is not None else None
    if ref is None and data.get("cell_id"):
        ref = CellRef(id=str(data["cell_id"]), name=str(data.get("name") or ""))
    return MutationResult(
        operation=operation,
        cell=ref,
        ran=bool(data.get("ran", False)),
        status=cell.status if cell is not None else None,
        errors=cell.errors if cell is not None else (),
        previous_code=data.get("previous_code"),
        descendants=tuple(str(d) for d in data.get("descendants") or ()),
        output_mimetype=cell.output_mimetype if cell is not None else None,
        output_preview=cell.output_preview if cell is not None else None,
        console=_console_of(result),
    )


# ── startup assertion ────────────────────────────────────────────────────────────────────────

_EXPECTED_MODULE_NAMES = ("AsyncCodeModeContext", "NotebookCell", "StaleCellError", "get_context")
_EXPECTED_GET_CONTEXT_PARAMS = ("skip_validation", "skip_staleness_check")
_EXPECTED_CONTEXT_PROPERTIES = ("cells", "globals", "graph", "packages")
_EXPECTED_CONTEXT_METHODS: dict[str, tuple[str, ...]] = {
    "create_cell": ("code", "before", "after", "hide_code", "disabled", "column", "name"),
    "edit_cell": ("target", "code", "hide_code", "disabled", "column", "name"),
    "delete_cell": ("target",),
    "move_cell": ("target", "before", "after"),
    "run_cell": ("target",),
    "set_ui_value": ("element", "value"),
    "broadcast_raw_notification": ("notification",),
}
_EXPECTED_CELL_PROPERTIES = ("id", "code", "name", "config", "status", "errors", "output")
_EXPECTED_PACKAGE_METHODS = ("add", "remove", "list")
_EXPECTED_NOTIFICATION_FIELDS = ("title", "description", "variant")


@dataclass(frozen=True, slots=True)
class BridgeReport:
    """What :func:`check_bridge` found, in a shape ``kedge doctor`` can print."""

    version: str
    pinned: str
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether the installed marimo exposes everything the bridge needs."""
        return not self.problems

    @property
    def version_matches_pin(self) -> bool:
        """Whether the installed marimo is the version the bridge was verified against."""
        return self.version == self.pinned

    @property
    def installed(self) -> bool:
        """Whether marimo is installed at all, as opposed to installed and mismatched."""
        return self.version != _NOT_INSTALLED

    def message(self) -> str:
        """A single message naming the version and listing every problem found."""
        if self.ok:
            return f"the marimo bridge matches marimo {self.version}"
        if not self.installed:
            # Worth its own sentence: this message is user-facing from `kedge open` and
            # `kedge hub`, and the general form below would read "does not match the installed
            # marimo not installed", which tells a user with a broken environment nothing.
            return (
                f"marimo is not installed, so kedge cannot open a notebook. "
                f"Install it with `uv pip install marimo=={self.pinned}` -- that is the version "
                f"kedge's bridge was verified against (docs/marimo-api.md)."
            )
        listed = "\n".join(f"  - {problem}" for problem in self.problems)
        return (
            f"kedge's marimo bridge does not match the installed marimo {self.version} "
            f"(verified against {self.pinned}). marimo._code_mode is a private API with no "
            f"versioning guarantees, so a version change can remove or rename anything.\n"
            f"{listed}\n"
            f"Pin marimo to {self.pinned}, or re-verify the bridge against {self.version} and "
            f"update src/kedge/notebook/driver.py and docs/marimo-api.md."
        )


@lru_cache(maxsize=1)
def check_bridge() -> BridgeReport:
    """Introspect the installed marimo and report anything the bridge depends on that is missing.

    This is the only place kedge's own process imports ``marimo._code_mode``, the import is
    function-local, and nothing here calls ``get_context()`` — outside a kernel that raises by
    design. The import exists so the surface can be checked with :func:`inspect.signature`
    up front, rather than surfacing as a ``TypeError`` halfway through a conversation
    (PLAN 6.1 mitigation 5).

    Returns:
        A report naming the installed version and listing every mismatch found.

    Example:
        >>> report = check_bridge()
        >>> report.ok or bool(report.problems)
        True
    """
    from importlib import metadata

    problems: list[str] = []
    try:
        version = metadata.version("marimo")
    except metadata.PackageNotFoundError:
        return BridgeReport(
            version=_NOT_INSTALLED, pinned=MARIMO_PIN, problems=("marimo is not installed",)
        )

    try:
        import marimo._code_mode as code_mode
    except ImportError as exc:
        return BridgeReport(
            version=version,
            pinned=MARIMO_PIN,
            problems=(f"marimo._code_mode cannot be imported: {exc}",),
        )

    for name in _EXPECTED_MODULE_NAMES:
        if not hasattr(code_mode, name):
            problems.append(f"marimo._code_mode.{name} is gone")

    get_context = getattr(code_mode, "get_context", None)
    if get_context is not None:
        parameters = inspect.signature(get_context).parameters
        problems += [
            f"get_context() no longer accepts {param}"
            for param in _EXPECTED_GET_CONTEXT_PARAMS
            if param not in parameters
        ]

    context = getattr(code_mode, "AsyncCodeModeContext", None)
    if context is not None:
        problems += _check_context(context)

    cell = getattr(code_mode, "NotebookCell", None)
    if cell is not None:
        problems += [
            f"NotebookCell.{name} is gone"
            for name in _EXPECTED_CELL_PROPERTIES
            if not hasattr(cell, name)
        ]

    problems += _check_packages()
    problems += _check_notifications()

    if version != MARIMO_PIN and not problems:
        logger.warning(
            "marimo %s is installed but the bridge was verified against %s; the surface still "
            "matches, but re-run the contract tests",
            version,
            MARIMO_PIN,
        )
    return BridgeReport(version=version, pinned=MARIMO_PIN, problems=tuple(problems))


def _check_context(context: type) -> list[str]:
    problems = [
        f"AsyncCodeModeContext.{name} is gone"
        for name in _EXPECTED_CONTEXT_PROPERTIES
        if not isinstance(getattr(context, name, None), property)
    ]
    for name in ("__aenter__", "__aexit__"):
        if not hasattr(context, name):
            problems.append(f"AsyncCodeModeContext.{name} is gone; 'async with' would not work")
    for name, expected in _EXPECTED_CONTEXT_METHODS.items():
        method = getattr(context, name, None)
        if method is None:
            problems.append(f"AsyncCodeModeContext.{name}() is gone")
            continue
        parameters = inspect.signature(method).parameters
        problems += [
            f"AsyncCodeModeContext.{name}() no longer accepts {param}"
            for param in expected
            if param not in parameters
        ]
    return problems


def _check_packages() -> list[str]:
    try:
        from marimo._code_mode._packages import Packages
    except ImportError as exc:
        return [f"marimo._code_mode._packages.Packages cannot be imported: {exc}"]
    return [
        f"Packages.{name}() is gone"
        for name in _EXPECTED_PACKAGE_METHODS
        if not hasattr(Packages, name)
    ]


def _check_notifications() -> list[str]:
    try:
        from marimo._messaging.notification import AlertNotification, BannerNotification
    except ImportError as exc:
        return [f"marimo._messaging.notification is unusable: {exc}"]
    problems: list[str] = []
    for cls in (BannerNotification, AlertNotification):
        fields = set(getattr(cls, "__struct_fields__", ()) or ())
        problems += [
            f"{cls.__name__}.{field} is gone"
            for field in _EXPECTED_NOTIFICATION_FIELDS
            if field not in fields
        ]
    return problems


def verify_bridge() -> BridgeReport:
    """Return the bridge report, raising :class:`BridgeVersionError` if anything is missing."""
    report = check_bridge()
    if not report.ok:
        raise BridgeVersionError(report.message())
    return report


# ── the driver ───────────────────────────────────────────────────────────────────────────────


class NotebookDriver:
    """Drives one marimo notebook through the kernel's execute endpoint.

    Every method builds a Python program, posts it, and reads the delimited payload back. The
    driver holds no notebook state of its own: the kernel is the single source of truth, so two
    drivers pointed at the same session cannot disagree about what the notebook contains.

    Example:
        >>> driver = NotebookDriver(
        ...     base_url="http://127.0.0.1:2718",
        ...     token="secret",
        ...     session_id="kedge-1",
        ...     check_bridge=False,
        ... )
        >>> driver.session_id
        'kedge-1'
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        session_id: str,
        client: KernelClient | None = None,
        check_bridge: bool = True,
        timeout: float | None = None,
    ) -> None:
        """Build a driver.

        Args:
            base_url: The marimo server kedge owns, always loopback.
            token: The per-launch token that server was started with.
            session_id: The session every submission runs in.
            client: A transport to use instead of building one. The test seam.
            check_bridge: Run :func:`verify_bridge` first, so a marimo whose private API has
                moved fails here, naming the version, rather than mid-conversation (PLAN 6.1
                mitigation 5). Nothing to do with TLS -- this connection is loopback HTTP and
                presents no certificate. Certificate trust lives in :mod:`kedge.tls`, which is
                why this is not called ``verify``: one codebase cannot afford two meanings of
                that word.
            timeout: Seconds to allow a submission, or ``None`` for the default.
        """
        if check_bridge:
            verify_bridge()
        self._kernel = client or KernelClient(
            base_url,
            token=token,
            session_id=session_id,
            timeout=DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout,
        )

    @classmethod
    def for_workspace(cls, workspace: Workspace, *, check_bridge: bool = True) -> NotebookDriver:
        """Build a driver for the marimo server ``workspace`` owns.

        Args:
            workspace: The workspace holding the marimo session to drive.
            check_bridge: As :meth:`__init__`. The private-API preflight, not TLS.

        Raises:
            WorkspaceError: No server is attached to the workspace.
            NotebookError: A server is attached but its session id has not been resolved yet.
        """
        session = workspace.require_marimo()
        if session.session_id is None:
            msg = (
                "the marimo server for this workspace has no session yet. A session is created "
                "only when a transport connects with a chosen id; bootstrap one before driving "
                "the notebook."
            )
            raise NotebookError(msg)
        return cls(
            base_url=session.base_url,
            token=session.token,
            session_id=session.session_id,
            check_bridge=check_bridge,
        )

    @property
    def session_id(self) -> str:
        """The marimo session every submission runs in."""
        return self._kernel.session_id

    @property
    def base_url(self) -> str:
        """The marimo server this driver talks to."""
        return self._kernel.base_url

    async def aclose(self) -> None:
        """Release the underlying HTTP client."""
        await self._kernel.aclose()

    async def __aenter__(self) -> NotebookDriver:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # ── reads ────────────────────────────────────────────────────────────────────────────

    async def list_cells(self, *, with_code: bool = True) -> tuple[CellInfo, ...]:
        """Return every cell in notebook order.

        Args:
            with_code: Whether to include each cell's source. Including it records a read against
                every cell, which satisfies marimo's staleness guard for all of them until the
                user next edits one. Pass ``False`` for a structural listing that leaves the
                guard armed.

        Returns:
            The cells, in the order they appear in the notebook.
        """
        result = await self._run(generate_list_cells(with_code=with_code), "list_cells")
        payload = _require_payload(result, "list_cells")
        raw = payload.get("data", {}).get("cells") or ()
        cells = tuple(cell for cell in (_cell_from_dict(item) for item in raw) if cell is not None)
        logger.debug("listed %d cells (with_code=%s)", len(cells), with_code)
        return cells

    async def get_cell(self, target: str | int) -> CellInfo:
        """Return one cell, including its source.

        Reading the source records a read at the cell's current version. That is what a later
        :meth:`edit_cell` relies on, and what makes the edit fail if the user changes the cell in
        the meantime.

        Args:
            target: A cell id, a cell name, or an integer index (negative allowed).

        Returns:
            The cell.

        Raises:
            CellNotFoundError: Nothing in the notebook matches ``target``.
        """
        result = await self._run(generate_get_cell(target), "get_cell")
        payload = _require_payload(result, "get_cell")
        cell = _cell_from_dict(payload.get("data", {}).get("cell"))
        if cell is None:
            msg = f"the kernel returned no cell for {target!r}"
            raise CellNotFoundError(msg)
        return cell

    async def read_graph(self) -> GraphView:
        """Return the notebook's dataflow graph.

        Reads no cell source, so it does not affect the staleness guard.
        """
        result = await self._run(generate_read_graph(), "read_graph")
        data = _require_payload(result, "read_graph").get("data", {})
        nodes = tuple(
            GraphNode(
                id=str(node.get("id", "")),
                name=str(node.get("name", "")),
                defs=tuple(node.get("defs") or ()),
                refs=tuple(node.get("refs") or ()),
                ancestors=tuple(node.get("ancestors") or ()),
                descendants=tuple(node.get("descendants") or ()),
                status=node.get("status"),
            )
            for node in data.get("nodes") or ()
        )
        return GraphView(
            nodes=nodes,
            cycles=tuple(tuple(cycle) for cycle in data.get("cycles") or ()),
            multiply_defined=tuple(data.get("multiply_defined") or ()),
        )

    # ── writes ───────────────────────────────────────────────────────────────────────────

    async def create_cell(
        self,
        code: str,
        *,
        name: str,
        before: str | None = None,
        after: str | None = None,
        hide_code: bool = False,
        disabled: bool = False,
        column: int | None = None,
        run: bool = True,
    ) -> MutationResult:
        """Create a named cell, and run it unless told otherwise.

        Args:
            code: The cell's source.
            name: A stable identifier for the cell. Required, validated, and unique.
            before: Insert before this cell id or name.
            after: Insert after this cell id or name.
            hide_code: Left at ``False`` deliberately — marimo's default is ``True``, which would
                hide the model's work from the user.
            disabled: Create the cell disabled.
            column: Which notebook column to place it in.
            run: Whether to run the cell in the same batch as the create. Doing both in one
                context makes validation, creation and execution a single atomic flush.

        Returns:
            The new cell and its post-run state.

        Raises:
            CellNameError: The name is malformed or already taken.
            MultiplyDefinedError: The code redefines a name another cell defines.
            CellSyntaxError: The code does not compile.
        """
        program = generate_create_cell(
            code,
            name=name,
            before=before,
            after=after,
            hide_code=hide_code,
            disabled=disabled,
            column=column,
            run=run,
        )
        result = await self._run(program, "create_cell")
        payload = _require_payload(result, "create_cell")
        mutation = _mutation_from_payload("create_cell", payload, result)
        logger.info("created cell %s", mutation.cell)
        return mutation

    async def edit_cell(
        self,
        target: str | int,
        code: str | None = None,
        *,
        name: str | None = None,
        hide_code: bool | None = None,
        disabled: bool | None = None,
        column: int | None = None,
        run: bool = True,
        overwrite_unread: bool = False,
    ) -> MutationResult:
        """Replace a cell's source, its configuration, or both.

        Args:
            target: A cell id, name, or index.
            code: The full replacement body. Partial edits are not a thing marimo offers, and
                submitting whole bodies is what PLAN 6.2 asks for anyway.
            name: Rename the cell. Renaming can mint a new cell id.
            hide_code: Change the code-visibility flag.
            disabled: Change the disabled flag.
            column: Move the cell to another notebook column.
            run: Whether to run the cell after editing it.
            overwrite_unread: Read the cell inside this call before overwriting it, which
                satisfies marimo's staleness guard from within. Leave it ``False`` to require
                that some earlier call read the cell, so an edit made by the user in between is
                caught rather than clobbered.

        Returns:
            The edited cell and its post-run state. ``previous_code`` is populated only when
            ``overwrite_unread`` is set.

        Raises:
            StaleCellError: The cell changed since kedge last read it. Retryable: re-read it.
            CellNotFoundError: Nothing matches ``target``.
        """
        program = generate_edit_cell(
            target,
            code,
            name=name,
            hide_code=hide_code,
            disabled=disabled,
            column=column,
            run=run,
            overwrite_unread=overwrite_unread,
        )
        result = await self._run(program, "edit_cell")
        payload = _require_payload(result, "edit_cell")
        mutation = _mutation_from_payload("edit_cell", payload, result)
        logger.info("edited cell %s", mutation.cell)
        return mutation

    async def delete_cell(self, target: str | int) -> MutationResult:
        """Delete a cell, reporting the cells that depended on it.

        Prefer :meth:`edit_cell`: replacing a body keeps the cell's identity and its place in the
        graph, whereas delete-then-create with the same variable names trips marimo's
        single-definition check partway through (PLAN 6.2).
        """
        result = await self._run(generate_delete_cell(target), "delete_cell")
        payload = _require_payload(result, "delete_cell")
        mutation = _mutation_from_payload("delete_cell", payload, result)
        if mutation.descendants:
            logger.warning(
                "deleted cell %s which %d other cell(s) depended on",
                mutation.cell,
                len(mutation.descendants),
            )
        return mutation

    async def move_cell(
        self,
        target: str | int,
        *,
        before: str | None = None,
        after: str | None = None,
    ) -> MutationResult:
        """Move a cell before or after another. Exactly one anchor is required."""
        result = await self._run(
            generate_move_cell(target, before=before, after=after), "move_cell"
        )
        payload = _require_payload(result, "move_cell")
        return _mutation_from_payload("move_cell", payload, result)

    async def run_cell(self, target: str | int) -> MutationResult:
        """Re-run an existing cell and report its resulting status and errors."""
        result = await self._run(generate_run_cell(target), "run_cell")
        payload = _require_payload(result, "run_cell")
        return _mutation_from_payload("run_cell", payload, result)

    async def set_ui_value(self, element: str, value: Any) -> MutationResult:
        """Set a ``mo.ui`` element's value.

        Args:
            element: The name of the notebook variable bound to the element.
            value: The new value. Must have a Python literal form.
        """
        result = await self._run(generate_set_ui_value(element, value), "set_ui_value")
        payload = _require_payload(result, "set_ui_value")
        return _mutation_from_payload("set_ui_value", payload, result)

    async def notify(
        self,
        title: str,
        description: str,
        *,
        variant: str | None = None,
        kind: str = "banner",
    ) -> None:
        """Push a banner or alert into the notebook UI.

        This one is immediate rather than queued: it has already been delivered by the time the
        surrounding batch would have flushed, so it is not undone if the batch later fails.
        """
        result = await self._run(
            generate_notify(title, description, variant=variant, kind=kind), "notify"
        )
        _require_payload(result, "notify")

    async def add_packages(self, *packages: str, remove: tuple[str, ...] = ()) -> PackageResult:
        """Install or remove packages in the kernel's environment.

        Package operations flush before cell operations, so a cell created in the same batch can
        import what was just installed. Cells that already ran may still not see it without a
        kernel restart.
        """
        result = await self._run(generate_add_packages(packages, remove), "add_packages")
        data = _require_payload(result, "add_packages").get("data", {})
        return PackageResult(
            added=tuple(data.get("added") or ()),
            removed=tuple(data.get("removed") or ()),
            installed_before=tuple(data.get("installed_before") or ()),
            console=_console_of(result),
        )

    async def probe(self, code: str) -> ProbeResult:
        """Evaluate throwaway code against the notebook's state without changing it.

        The scratchpad discards new top-level bindings, so a probe cannot leave anything behind;
        it is the cheap way to inspect a dataframe's shape or a variable's value without adding a
        cell the user then has to read past.

        Args:
            code: Python source. If it ends in an expression, that value is reported.

        Returns:
            The probe's outcome. A probe that raises comes back with ``ok=False`` and the
            traceback in ``error``, rather than raising here: an exploratory query failing is a
            result, not a bridge failure.
        """
        program = generate_probe(code)
        result = await self._run(program, "probe")
        payload = extract_payload(result.stdout)
        if payload is None or not payload.get("ok", False):
            return ProbeResult(
                ok=False,
                stdout=_console_of(result),
                error=result.stderr_tail() or "the probe produced no result and no error",
            )
        data = payload.get("data", {})
        return ProbeResult(
            ok=True,
            value_repr=data.get("value"),
            value_type=data.get("type"),
            truncated=bool(data.get("truncated", False)),
            stdout=_console_of(result),
        )

    # ── plumbing ─────────────────────────────────────────────────────────────────────────

    async def _run(self, program: str, operation: str) -> ExecResult:
        logger.debug("running %s against %s", operation, self._kernel.base_url)
        return await self._kernel.execute(program)

    def __repr__(self) -> str:
        return f"NotebookDriver(base_url={self.base_url!r}, session_id={self.session_id!r})"


if TYPE_CHECKING:  # pragma: no cover - a compile-time assertion, not runtime behaviour

    def _driver_satisfies_bridge(driver: NotebookDriver) -> NotebookBridge:
        """Fail type checking if :class:`NotebookDriver` drifts from the shared interface."""
        return driver
