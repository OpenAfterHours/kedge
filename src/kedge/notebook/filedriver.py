"""The fallback bridge: drive the notebook by editing its ``.py`` and letting marimo reload it.

PLAN 6.1 calls ``marimo._code_mode`` the project's top risk — a private API whose own docstring
says it "may change or be removed without notice" — and lists five mitigations. Four of them
harden the live bridge. This module is the fifth: somewhere else to go when the live bridge stops
working, built on interfaces that cannot be withdrawn.

**What it gives up.**

* *The kernel.* Nothing here executes anything. :meth:`FileNotebookDriver.run_cell`,
  :meth:`~FileNotebookDriver.probe`, :meth:`~FileNotebookDriver.set_ui_value` and
  :meth:`~FileNotebookDriver.notify` raise :class:`NoKernelError` rather than pretending. A cell
  written with ``run=True`` comes back with ``ran=False``, because that is what happened.
* *Results.* No status, no output, no cell errors — a :class:`~kedge.notebook.model.CellInfo`
  from this bridge carries ``status=None`` and ``errors=()``, since a file cannot report whether
  a cell ran or what it produced. A caller that treats "no errors" as "it worked" is wrong here,
  and would be wrong for the same reason about any notebook that has not been run.
* *Installation.* :meth:`~FileNotebookDriver.add_packages` edits the notebook's PEP 723 inline
  script metadata. It records a dependency; it does not install one.
* *Speed.* Every operation reads the whole file, analyses every cell, and rewrites the file.
  That is milliseconds on a notebook of the size kedge writes, and it means two drivers pointed
  at the same file cannot disagree about what it contains — the file is the single source of
  truth, exactly as the kernel is for the live driver.

**What it gains.** It depends on the notebook file format and on ``marimo.App`` / ``marimo.Cell``,
and on nothing else — see :mod:`kedge.notebook.codegen` for why that distinction is the whole
point. It also works with no server running at all, which makes it the bridge for editing a
notebook offline, in CI, or while marimo is broken.

**How the change reaches marimo.** kedge already launches ``marimo edit --watch``
(``lifecycle.build_marimo_argv``). marimo polls the file, notices a write it did not make, and
reloads the notebook into the running session. With ``runtime.watcher_on_save`` at its default of
``"lazy"`` the reloaded cells are marked stale rather than run, which is why ``run=True`` cannot
be honoured: the user runs them, or the live driver does.

**Everything else behaves identically.** The structural operations raise the same errors, with
the same types, for the same conditions as :class:`~kedge.notebook.driver.NotebookDriver` — those
types are imported from it rather than redeclared, so the agent loop's validation gate and retry
logic cannot tell the two bridges apart. That includes the read-before-write guard: marimo's
lives on the kernel and is armed by touching a cell's code, and the one here is armed the same
way, by hashing each cell body as it is read and refusing to overwrite a body that has changed
since. Nothing in this module imports ``marimo._code_mode``; nothing in it needs to.
"""

from __future__ import annotations

import ast
import logging
import re
import tomllib
from hashlib import blake2s
from pathlib import Path
from typing import TYPE_CHECKING, Any

import tomli_w

from kedge.errors import KernelError
from kedge.notebook.codegen import (
    CellAnalysis,
    FileCell,
    NotebookDocument,
    analyse_document,
    cycles_of,
    dependency_map,
    multiply_defined,
    parse_notebook,
    read_notebook,
    render_notebook,
    transitive,
    write_atomically,
)
from kedge.notebook.driver import (
    CellNameError,
    CellNotFoundError,
    CellSyntaxError,
    CycleDetectedError,
    MultiplyDefinedError,
    StaleCellError,
    validate_cell_name,
)
from kedge.notebook.model import (
    CellInfo,
    CellRef,
    DefinitionConflict,
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
    "NOT_RUN_NOTE",
    "PACKAGES_NOTE",
    "FileNotebookDriver",
    "NoKernelError",
]

NOT_RUN_NOTE = (
    "the file bridge has no kernel, so nothing was executed. marimo reloads the notebook when "
    "the file changes and leaves the changed cells stale until they are run."
)

PACKAGES_NOTE = (
    "the file bridge has no kernel, so nothing was installed. The packages were recorded in the "
    "notebook's PEP 723 inline script metadata. Rebuild the environment before any cell imports "
    "them -- and note that kedge launches marimo with --no-sandbox, which ignores this metadata."
)

_SCRIPT_BLOCK = re.compile(r"(?m)^# /// script$\s(?P<content>(^#(| .*)$\s)+)^# ///$")
_REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9._-]+")


class NoKernelError(KernelError):
    """The operation needs a live marimo kernel, and the file bridge does not have one.

    Not retryable and not a bug: the fallback bridge is a document editor. The remedy is to run
    the operation through :class:`~kedge.notebook.driver.NotebookDriver` against a live session,
    or to ask the user to run the notebook in the editor.
    """

    retryable = False


# ── the driver ───────────────────────────────────────────────────────────────────────────────


class FileNotebookDriver:
    """Drives one marimo notebook by rewriting its ``.py`` on disk.

    Satisfies :class:`~kedge.notebook.model.NotebookBridge` for everything that is a document
    edit, and raises :class:`NoKernelError` for everything that is not.

    Example:
        >>> driver = FileNotebookDriver("notebook.py")
        >>> driver.path.name
        'notebook.py'
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._read: set[str] = set()

    @classmethod
    def for_workspace(cls, workspace: Workspace) -> FileNotebookDriver:
        """Build a driver for the notebook a workspace owns. No server required."""
        return cls(workspace.notebook_path)

    @property
    def path(self) -> Path:
        """The notebook file this driver edits."""
        return self._path

    async def aclose(self) -> None:
        """Release resources. There are none; the method exists so callers can be uniform."""

    async def __aenter__(self) -> FileNotebookDriver:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        return f"FileNotebookDriver(path={str(self._path)!r})"

    # ── reads ────────────────────────────────────────────────────────────────────────────

    async def list_cells(self, *, with_code: bool = True) -> tuple[CellInfo, ...]:
        """Return every cell in notebook order.

        Args:
            with_code: Whether to include each cell's source. Including it records a read for
                every cell, which satisfies the staleness guard for all of them until one changes
                on disk again. Pass ``False`` for a structural listing that leaves the guard
                armed -- the same trade-off, and the same default, as the live driver.

        Returns:
            The cells, in the order they appear in the file.
        """
        document = self._load()
        if with_code:
            for cell in document.cells:
                self._record(cell)
        return tuple(
            self._info(document, index, with_code=with_code) for index in range(len(document.cells))
        )

    async def get_cell(self, target: str | int) -> CellInfo:
        """Return one cell, including its source, and record the read.

        Args:
            target: A cell name, a cell id, or an integer index (negative allowed).

        Returns:
            The cell.

        Raises:
            CellNotFoundError: Nothing in the notebook matches ``target``.
        """
        document = self._load()
        index = self._resolve(document, target)
        self._record(document.cells[index])
        return self._info(document, index, with_code=True)

    async def read_graph(self) -> GraphView:
        """Return the notebook's dataflow graph, computed from the source.

        Definitions and references come from ``marimo.Cell``, so the graph is marimo's own
        analysis rather than an approximation of it. Statuses are ``None``: a file has no idea
        which cells have run.

        Reading the graph does not record a read for any cell, matching the live driver -- so it
        stays the cheap way to ask "what depends on what" without disarming the guard.
        """
        document = self._load()
        analyses, _ = analyse_document(document)
        children = dependency_map(analyses)
        parents: dict[int, set[int]] = {index: set() for index in range(len(analyses))}
        for parent, kids in children.items():
            for kid in kids:
                parents[kid].add(parent)

        nodes = tuple(
            GraphNode(
                id=str(index),
                name=document.cells[index].name,
                defs=analyses[index].defs,
                refs=analyses[index].refs,
                ancestors=_ids(transitive(parents, index)),
                descendants=_ids(transitive(children, index)),
            )
            for index in range(len(document.cells))
        )
        return GraphView(
            nodes=nodes,
            cycles=tuple(_ids(cycle) for cycle in cycles_of(analyses)),
            multiply_defined=tuple(sorted(multiply_defined(analyses))),
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
        """Write a new named cell into the file.

        Args:
            code: The cell's source.
            name: A stable identifier for the cell. Required, validated, and unique.
            before: Insert before this cell id or name.
            after: Insert after this cell id or name.
            hide_code: Left at ``False`` deliberately, inverting marimo's own default.
            disabled: Create the cell disabled.
            column: Which notebook column to place it in.
            run: Accepted for interface parity and *not honoured* -- there is no kernel. The
                result reports ``ran=False`` and says so in ``console``.

        Returns:
            The new cell. ``ran`` is always ``False``.

        Raises:
            CellNameError: The name is malformed, already taken, or both anchors were given.
            CellSyntaxError: The code does not compile.
            MultiplyDefinedError: The code redefines a name another cell defines.
            CycleDetectedError: The code would close a loop in the dataflow graph.
        """
        validate_cell_name(name)
        if before is not None and after is not None:
            msg = "give at most one of before= and after= when creating a cell"
            raise CellNameError(msg)
        _check_syntax(code)

        document = self._load()
        self._refuse_taken_name(document, name)
        index = self._insertion_point(document, before=before, after=after)
        cell = FileCell(name=name, code=code, hide_code=hide_code, disabled=disabled, column=column)
        updated = document.inserted(cell, index)
        self._write(updated, changed=index, operation="create_cell")
        self._record(cell)
        logger.info("wrote cell %s to %s", name, self._path)
        return MutationResult(
            operation="create_cell",
            cell=CellRef(id=str(index), name=name),
            console=NOT_RUN_NOTE if run else "",
        )

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
            code: The full replacement body.
            name: Rename the cell.
            hide_code: Change the code-visibility flag.
            disabled: Change the disabled flag.
            column: Move the cell to another notebook column.
            run: Accepted and not honoured; see :meth:`create_cell`.
            overwrite_unread: Read the cell inside this call before overwriting it, satisfying
                the guard honestly and returning the user's version as ``previous_code``. Leave
                it ``False`` to require that some earlier call read the cell, so an edit the user
                made in between is caught rather than clobbered.

        Returns:
            The edited cell. ``previous_code`` is populated only when ``overwrite_unread`` is set.

        Raises:
            StaleCellError: The cell changed on disk since kedge last read it. Retryable.
            CellNotFoundError: Nothing matches ``target``.
            CellNameError: Nothing was passed to change, or the new name is unusable.
        """
        if (
            code is None
            and name is None
            and hide_code is None
            and disabled is None
            and column is None
        ):
            msg = "edit_cell needs something to change: pass code, name, hide_code, disabled or column"
            raise CellNameError(msg)
        if name is not None:
            validate_cell_name(name)
        if code is not None:
            _check_syntax(code)

        document = self._load()
        index = self._resolve(document, target)
        existing = document.cells[index]
        if name is not None and name != existing.name:
            self._refuse_taken_name(document, name)

        previous_code: str | None = None
        if code is not None:
            if overwrite_unread:
                previous_code = existing.code
                self._record(existing)
            self._require_read(document, index)

        cell = FileCell(
            name=existing.name if name is None else name,
            code=existing.code if code is None else code,
            hide_code=existing.hide_code if hide_code is None else hide_code,
            disabled=existing.disabled if disabled is None else disabled,
            column=existing.column if column is None else column,
        )
        self._write(document.with_cell(index, cell), changed=index, operation="edit_cell")
        self._record(cell)
        logger.info("edited cell %s in %s", cell.name, self._path)
        return MutationResult(
            operation="edit_cell",
            cell=CellRef(id=str(index), name=cell.name),
            previous_code=previous_code,
            console=NOT_RUN_NOTE if run else "",
        )

    async def delete_cell(self, target: str | int) -> MutationResult:
        """Remove a cell from the file, reporting the cells that depended on it.

        Prefer :meth:`edit_cell`: replacing a body keeps the cell's place in the graph, whereas
        delete-then-create with the same variable names trips the single-definition check
        partway through (PLAN 6.2).
        """
        document = self._load()
        index = self._resolve(document, target)
        cell = document.cells[index]
        analyses, _ = analyse_document(document)
        descendants = _ids(transitive(dependency_map(analyses), index))

        self._write(document.without(index), changed=None, operation="delete_cell")
        if descendants:
            logger.warning(
                "deleted cell %s which %d other cell(s) depended on", cell.name, len(descendants)
            )
        return MutationResult(
            operation="delete_cell",
            cell=CellRef(id=str(index), name=cell.name),
            descendants=descendants,
        )

    async def move_cell(
        self,
        target: str | int,
        *,
        before: str | None = None,
        after: str | None = None,
    ) -> MutationResult:
        """Move a cell before or after another. Exactly one anchor is required."""
        if (before is None) == (after is None):
            msg = "give exactly one of before= and after= when moving a cell"
            raise CellNameError(msg)

        document = self._load()
        index = self._resolve(document, target)
        anchor = self._resolve(document, before or after or "")
        if anchor == index:
            msg = f"cell {target!r} cannot be moved relative to itself"
            raise CellNameError(msg)

        destination = anchor - 1 if anchor > index else anchor
        if after is not None:
            destination += 1
        cell = document.cells[index]
        # A move cannot introduce a conflict or a cycle: marimo's graph is built from
        # definitions and references, neither of which depends on notebook order.
        self._write(document.moved(index, destination), changed=None, operation="move_cell")
        return MutationResult(
            operation="move_cell", cell=CellRef(id=str(destination), name=cell.name)
        )

    async def add_packages(self, *packages: str, remove: tuple[str, ...] = ()) -> PackageResult:
        """Record dependencies in the notebook's PEP 723 inline script metadata.

        This is a *declaration*, not an installation: there is no environment to install into.
        The block it writes is the one PEP 723 standardises and marimo already understands, so a
        later ``marimo edit --sandbox`` or ``uv run`` picks it up -- but kedge's own launcher
        passes ``--no-sandbox`` on purpose (``lifecycle.build_marimo_argv``), so the metadata has
        no effect on a kedge-managed kernel until the environment is rebuilt by hand.

        Args:
            *packages: Requirement specifiers to add, e.g. ``"polars==1.43.0"``.
            remove: Requirement names to drop. Matched by distribution name, so ``"polars"``
                removes ``"polars==1.43.0"``.

        Returns:
            What was added and removed. ``installed_before`` is empty, because this bridge cannot
            see an environment and will not guess at one.

        Raises:
            CellNameError: Nothing was passed to add or remove.
        """
        if not packages and not remove:
            msg = "add_packages needs at least one package to add or remove"
            raise CellNameError(msg)

        document = self._load()
        metadata = _read_script_metadata(document.prefix)
        declared = [str(item) for item in metadata.get("dependencies", [])]

        drop = {_requirement_name(item) for item in remove}
        kept = [item for item in declared if _requirement_name(item) not in drop]
        removed = tuple(item for item in declared if _requirement_name(item) in drop)
        for specifier in packages:
            name = _requirement_name(specifier)
            kept = [item for item in kept if _requirement_name(item) != name]
            kept.append(specifier)

        metadata["dependencies"] = sorted(kept)
        updated = NotebookDocument(
            prefix=_write_script_metadata(document.prefix, metadata),
            cells=document.cells,
            separators=document.separators,
            suffix=document.suffix,
            toplevel=document.toplevel,
            setup_code=document.setup_code,
        )
        self._write(updated, changed=None, operation="add_packages")
        logger.info("recorded %d package declaration(s) in %s", len(packages), self._path)
        return PackageResult(
            added=tuple(packages), removed=removed, installed_before=(), console=PACKAGES_NOTE
        )

    # ── the operations a file cannot perform ─────────────────────────────────────────────

    async def run_cell(self, target: str | int) -> MutationResult:
        """Always raises: running a cell needs a kernel.

        ``marimo edit --watch`` reloading the file is not execution. In marimo's default
        ``"lazy"`` watcher mode a reloaded cell is marked stale and waits for the user; even in
        ``"autorun"`` mode the run happens in marimo's process, after this call has returned,
        with no result kedge could report. Reporting either as a run would be a lie, so this
        refuses instead.
        """
        raise NoKernelError(
            f"cannot run cell {target!r}: the file bridge edits {self._path} and has no kernel to "
            f"run anything in. Reloading the file marks the cell stale; running it is the user's "
            f"or the live driver's job."
        )

    async def probe(self, code: str) -> ProbeResult:
        """Always raises: a probe reads live kernel state, and there is none."""
        raise NoKernelError(
            f"cannot probe the notebook: a probe evaluates code against the kernel's globals, and "
            f"the file bridge only has {self._path} on disk. Nothing in a file says what a "
            f"variable currently holds."
        )

    async def set_ui_value(self, element: str, value: Any) -> MutationResult:
        """Always raises: a ``mo.ui`` element exists only in a running kernel."""
        raise NoKernelError(
            f"cannot set {element!r}: a mo.ui element and its value live in the kernel, not in "
            f"the notebook file. The file bridge cannot reach one."
        )

    async def notify(
        self,
        title: str,
        description: str,
        *,
        variant: str | None = None,
        kind: str = "banner",
    ) -> None:
        """Always raises: a notification is pushed to a connected frontend over the session."""
        raise NoKernelError(
            f"cannot show the {kind} {title!r}: notifications are broadcast to the notebook's "
            f"frontend over a live session, and the file bridge has no session. Put the message "
            f"in the chat pane instead."
        )

    # ── plumbing ─────────────────────────────────────────────────────────────────────────

    def _load(self) -> NotebookDocument:
        return read_notebook(self._path)

    def _write(self, document: NotebookDocument, *, changed: int | None, operation: str) -> None:
        """Validate the resulting notebook, then replace the file in one step."""
        analyses, toplevel = analyse_document(document)
        if changed is not None:
            _refuse_conflicts(document, analyses, changed)
            _refuse_cycles(document, analyses, changed)
        source = render_notebook(document, analyses, toplevel)
        _refuse_unparsable(source, operation)
        write_atomically(self._path, source)

    def _resolve(self, document: NotebookDocument, target: str | int) -> int:
        index = document.index_of(target)
        if index is None:
            names = ", ".join(cell.name for cell in document.cells) or "none"
            msg = (
                f"no cell {target!r} in {self._path}. The notebook holds: {names}. Cells are "
                f"addressed by name, by position, or by the index the file bridge reports as an id."
            )
            raise CellNotFoundError(msg)
        return index

    def _insertion_point(
        self, document: NotebookDocument, *, before: str | None, after: str | None
    ) -> int:
        if before is not None:
            return self._resolve(document, before)
        if after is not None:
            return self._resolve(document, after) + 1
        return len(document.cells)

    def _refuse_taken_name(self, document: NotebookDocument, name: str) -> None:
        for index, cell in enumerate(document.cells):
            if cell.name == name:
                msg = f"cell name {name} is already used by cell {index}"
                raise CellNameError(msg)
        if name in document.toplevel:
            msg = (
                f"cell name {name} is already used by a top-level definition in the notebook, "
                f"which would shadow it"
            )
            raise CellNameError(msg)

    def _info(self, document: NotebookDocument, index: int, *, with_code: bool) -> CellInfo:
        cell = document.cells[index]
        return CellInfo(
            id=str(index),
            name=cell.name,
            code=cell.code if with_code else None,
            hide_code=cell.hide_code,
            disabled=cell.disabled,
            column=cell.column,
        )

    # ── the read-before-write guard ──────────────────────────────────────────────────────

    def _record(self, cell: FileCell) -> None:
        """Note that this exact cell body has been seen, arming an edit of it."""
        self._read.add(_digest(cell))

    def _require_read(self, document: NotebookDocument, index: int) -> None:
        """Refuse to overwrite a body kedge has not read at its current version.

        The live guard lives on marimo's kernel and is armed by touching a cell's code; this one
        is armed by hashing the body when it is read. Both protect the same thing and neither
        protects more than it: a cell kedge itself wrote this session is recorded as read, so the
        guard fires only for a body kedge has not seen -- one already on disk, or one the user
        changed in the editor between turns (docs/marimo-api.md 4.1).

        A cell whose current body is empty is exempt, matching marimo: there is nothing there to
        lose.
        """
        cell = document.cells[index]
        if not cell.code.strip() or _digest(cell) in self._read:
            return
        stale = tuple(
            str(position)
            for position, other in enumerate(document.cells)
            if _digest(other) not in self._read
        )
        others = ", ".join(item for item in stale if item != str(index))
        msg = (
            f"cell {str(index)!r} ({cell.name}) was modified on disk since kedge last read it.\n"
            f"kedge will not force the write: re-read the cell so the user's version is seen "
            f"before it is replaced."
        )
        if others:
            msg += f"\nOther stale cells: {others}."
        raise StaleCellError(msg, cell_id=str(index), stale=stale)


# ── validation ───────────────────────────────────────────────────────────────────────────────


def _check_syntax(code: str) -> None:
    """Reject code that does not compile, before anything is written.

    The live bridge gets this from marimo's dry-run compile at flush time. Here it happens in
    kedge's own process, with the same flags marimo compiles a cell with -- top-level ``await``
    is legal in a cell and must not be reported as a syntax error.
    """

    try:
        compile(
            code,
            "<kedge-cell>",
            "exec",
            flags=ast.PyCF_ONLY_AST | ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
            dont_inherit=True,
        )
    except (SyntaxError, ValueError) as exc:
        msg = f"the submitted code did not compile, so nothing was applied: {exc}"
        raise CellSyntaxError(msg) from exc


def _refuse_conflicts(
    document: NotebookDocument, analyses: Sequence[CellAnalysis], changed: int
) -> None:
    """Refuse a write that would make the changed cell redefine another cell's name.

    marimo enforces the single-definition rule kernel-side and reports it as prose that the live
    driver parses back into :class:`~kedge.notebook.model.DefinitionConflict`. Here the conflict
    is computed directly, so the same structured list reaches the validation gate without a
    round trip through a sentence. Only conflicts the changed cell is party to are reported: a
    notebook that was already broken elsewhere stays editable.
    """
    conflicts = [
        DefinitionConflict(
            name=name,
            defined_in=tuple(
                item
                for position in where
                if position != changed
                for item in (str(position), document.cells[position].name)
            ),
        )
        for name, where in sorted(multiply_defined(analyses).items())
        if changed in where
    ]
    if not conflicts:
        return
    detail = "\n".join(f"  - {conflict}" for conflict in conflicts)
    raise MultiplyDefinedError(
        "the submitted code breaks marimo's single-definition rule, so nothing was applied:\n"
        + detail,
        conflicts,
    )


def _refuse_cycles(
    document: NotebookDocument, analyses: Sequence[CellAnalysis], changed: int
) -> None:
    """Refuse a write that would put the changed cell in a dependency loop."""
    involved = [cycle for cycle in cycles_of(analyses) if changed in cycle]
    if not involved:
        return
    described = "; ".join(
        ", ".join(f"{position} ({document.cells[position].name})" for position in cycle)
        for cycle in involved
    )
    raise CycleDetectedError(
        f"the submitted code would create a cycle in the notebook's dataflow graph, so nothing "
        f"was applied: {described}"
    )


def _refuse_unparsable(source: str, operation: str) -> None:
    """The last gate before the file is replaced: it must still be a notebook."""
    try:
        parse_notebook(source)
    except Exception as exc:
        msg = (
            f"refusing to write the notebook after {operation}: the rendered file does not parse "
            f"({exc}). Nothing was written, so the notebook on disk is unchanged."
        )
        raise CellSyntaxError(msg) from exc


def _digest(cell: FileCell) -> str:
    """A stable token for one cell body, used as the read-tracker key.

    Keyed on content rather than position, so reordering the notebook does not spuriously disarm
    or re-arm the guard, and re-reading a body kedge has already seen is recognised as the same
    body it saw.
    """
    payload = f"{cell.name}\x00{cell.code}".encode()
    return blake2s(payload, digest_size=16).hexdigest()


def _ids(positions: Sequence[int] | set[int]) -> tuple[str, ...]:
    return tuple(str(position) for position in sorted(positions))


# ── PEP 723 inline script metadata ───────────────────────────────────────────────────────────


def _read_script_metadata(prefix: str) -> dict[str, Any]:
    """Return the notebook's ``# /// script`` block as a dict, or an empty one."""
    match = _SCRIPT_BLOCK.search(prefix)
    if match is None:
        return {}
    content = "".join(
        line[2:] if line.startswith("# ") else line[1:]
        for line in match.group("content").splitlines(keepends=True)
    )
    try:
        return dict(tomllib.loads(content))
    except tomllib.TOMLDecodeError:
        logger.warning("the notebook's PEP 723 script block is not valid TOML; replacing it")
        return {}


def _write_script_metadata(prefix: str, metadata: dict[str, Any]) -> str:
    """Return ``prefix`` with its ``# /// script`` block replaced or inserted at the top."""
    body = tomli_w.dumps(metadata).rstrip("\n")
    commented = "\n".join(f"# {line}" if line else "#" for line in body.splitlines())
    block = f"# /// script\n{commented}\n# ///"
    match = _SCRIPT_BLOCK.search(prefix)
    if match is None:
        return f"{block}\n\n{prefix.lstrip()}"
    return prefix[: match.start()] + block + prefix[match.end() :]


def _requirement_name(specifier: str) -> str:
    """The distribution name from a PEP 508 requirement, normalised for comparison."""
    match = _REQUIREMENT_NAME.match(specifier.strip())
    return match.group(0).lower().replace("_", "-") if match else specifier.strip().lower()


if TYPE_CHECKING:  # pragma: no cover - a compile-time assertion, not runtime behaviour

    def _filedriver_satisfies_bridge(driver: FileNotebookDriver) -> NotebookBridge:
        """Fail type checking if :class:`FileNotebookDriver` drifts from the shared interface."""
        return driver
