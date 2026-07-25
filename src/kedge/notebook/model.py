"""Typed results for the marimo bridge, and the interface the driver satisfies.

Nothing here imports marimo. These are plain frozen dataclasses describing what came back from
the kernel, so the rest of kedge can hold notebook state without ever touching a private marimo
API, and so a test can build a result by hand.

:class:`NotebookBridge` is the structural interface every driver implementation satisfies. It
exists because ``_code_mode`` is private and may vanish (PLAN 6.1 mitigation 4): the fallback is
to write the notebook ``.py`` on disk and let ``marimo edit --watch`` reload it, and that only
stays a swap rather than a rewrite if the interface is defined up front. It is a
:class:`~typing.Protocol` rather than an abstract base class because the house style prefers
structural typing — an implementation opts in by having the right methods, not by inheriting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "UNNAMED_CELL",
    "CellErrorInfo",
    "CellInfo",
    "CellRef",
    "DefinitionConflict",
    "ExecResult",
    "GraphNode",
    "GraphView",
    "MutationResult",
    "NotebookBridge",
    "PackageResult",
    "ProbeResult",
]

UNNAMED_CELL = "_"
"""What marimo reports as the name of a cell loaded from disk as ``def _():``.

Verified against 0.23.15: it is ``"_"``, not the empty string the docstring implies
(docs/marimo-api.md 7.8). Anything comparing names must know this.
"""


# ── transport ────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ExecResult:
    """One ``POST /api/kernel/execute`` round trip, reassembled from its SSE stream.

    ``stdout`` and ``stderr`` are the concatenated payloads of every ``stdout`` / ``stderr``
    event, in arrival order. They are *not* only kedge's output: ``_code_mode`` prints its own
    flush summary to stdout, and any cell that runs during the call streams its console output
    through the same channel. Structured results are carried in a delimited payload inside
    ``stdout`` instead, and extracted by the driver.

    Example:
        >>> ExecResult(success=True, stdout="created cell 'UMIT'\\n", stderr="").ok
        True
    """

    success: bool
    stdout: str
    stderr: str
    output_mimetype: str | None = None
    output_data: str | None = None
    saw_done: bool = True

    @property
    def ok(self) -> bool:
        """Whether the kernel reported the submission as successful."""
        return self.success

    def stderr_tail(self, limit: int = 2000) -> str:
        """Return the last ``limit`` characters of stderr, for error messages."""
        text = self.stderr.strip()
        if len(text) <= limit:
            return text
        return "..." + text[-limit:]


# ── cells ────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CellErrorInfo:
    """One error attached to a cell, flattened out of marimo's tagged error union."""

    kind: str
    message: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.message}"


@dataclass(frozen=True, slots=True)
class CellRef:
    """The two ways to address a cell: its opaque id, and the name kedge gave it.

    Ids are short opaque tokens (``Hbol``, ``UMIT``) and churn — a rename can mint a new one — so
    the name is the stable handle across turns and the id is for one-shot follow-ups.
    """

    id: str
    name: str

    @property
    def target(self) -> str:
        """The preferred address for a follow-up operation: the name if it has one."""
        return self.name if self.name and self.name != UNNAMED_CELL else self.id

    def __str__(self) -> str:
        return f"{self.id} ({self.name})" if self.name else self.id


@dataclass(frozen=True, slots=True)
class CellInfo:
    """A cell as the kernel currently sees it.

    ``code`` is ``None`` when the cell was listed without its source. That distinction is
    load-bearing rather than cosmetic: reading a cell's code is what marks it read for marimo's
    staleness guard, so a listing that omits code deliberately leaves the guard armed.

    Example:
        >>> CellInfo(id="UMIT", name="load_handin", code="x = 1", status="idle").ref.target
        'load_handin'
    """

    id: str
    name: str
    code: str | None = None
    status: str | None = None
    hide_code: bool = False
    disabled: bool = False
    column: int | None = None
    errors: tuple[CellErrorInfo, ...] = ()
    output_mimetype: str | None = None
    output_preview: str | None = None

    @property
    def ref(self) -> CellRef:
        """This cell's addressing pair."""
        return CellRef(id=self.id, name=self.name)

    @property
    def is_named(self) -> bool:
        """Whether the cell carries a real name rather than marimo's unnamed placeholder."""
        return bool(self.name) and self.name != UNNAMED_CELL

    @property
    def failed(self) -> bool:
        """Whether the cell is currently carrying errors."""
        return bool(self.errors)


@dataclass(frozen=True, slots=True)
class MutationResult:
    """The outcome of one structural change to the notebook.

    ``console`` is everything the kernel printed outside kedge's own delimited payload: the
    ``_code_mode`` flush summary, plus whatever the cell printed if it ran. It is deliberately
    kept raw and unclassified — pattern-matching marimo's summary wording would break silently on
    the next release, and the structured fields already carry everything kedge needs.
    """

    operation: str
    cell: CellRef | None = None
    ran: bool = False
    status: str | None = None
    errors: tuple[CellErrorInfo, ...] = ()
    previous_code: str | None = None
    descendants: tuple[str, ...] = ()
    output_mimetype: str | None = None
    output_preview: str | None = None
    console: str = ""

    @property
    def ok(self) -> bool:
        """Whether the change applied and the cell is not carrying errors."""
        return not self.errors


# ── graph ────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class GraphNode:
    """One cell's place in the dataflow graph."""

    id: str
    name: str
    defs: tuple[str, ...] = ()
    refs: tuple[str, ...] = ()
    ancestors: tuple[str, ...] = ()
    descendants: tuple[str, ...] = ()
    status: str | None = None


@dataclass(frozen=True, slots=True)
class GraphView:
    """The notebook's dataflow graph, flattened.

    Reading the graph never reads any cell's code, so it is the cheap way to answer "what would
    break if this went away" without disarming the staleness guard.
    """

    nodes: tuple[GraphNode, ...] = ()
    cycles: tuple[tuple[str, ...], ...] = ()
    multiply_defined: tuple[str, ...] = ()

    def node(self, target: str) -> GraphNode | None:
        """Return the node with this cell id or name, or ``None``."""
        for node in self.nodes:
            if target in (node.id, node.name):
                return node
        return None

    def defining(self, name: str) -> tuple[GraphNode, ...]:
        """Return every node that defines the variable ``name``."""
        return tuple(node for node in self.nodes if name in node.defs)


@dataclass(frozen=True, slots=True)
class DefinitionConflict:
    """One entry from marimo's ``Multiply-defined names`` rejection.

    marimo enforces the single-definition rule kernel-side, at flush time, as a ``RuntimeError``
    with a formatted list. Parsing it into this shape is what lets kedge's validation gate
    (PLAN M4) show the model a clean violation instead of a traceback.
    """

    name: str
    defined_in: tuple[str, ...] = ()

    def __str__(self) -> str:
        if not self.defined_in:
            return f"{self.name!r} is defined more than once"
        return f"{self.name!r} is already defined in cell {', '.join(self.defined_in)}"


# ── probes and packages ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """The result of running throwaway code in the kernel's scratchpad.

    A probe cannot change the notebook: the scratchpad is a shallow copy of kernel globals and
    every new top-level binding is discarded when the call returns (PLAN 1.2). That is what makes
    it the cheap way to look at notebook state without leaving a cell behind.
    """

    ok: bool
    value_repr: str | None = None
    value_type: str | None = None
    truncated: bool = False
    stdout: str = ""
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PackageResult:
    """The outcome of a package add/remove batch.

    A newly installed package is not necessarily importable in already-running cells: Python
    caches module lookups, so a kernel restart may be needed (PLAN 6.2). ``needs_restart_hint``
    exists so the caller can say so rather than leaving the user with a puzzling ImportError.
    """

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    installed_before: tuple[str, ...] = ()
    console: str = ""

    @property
    def needs_restart_hint(self) -> bool:
        """Whether the caller should warn that a kernel restart may be needed."""
        return bool(self.added)


# ── the interface ────────────────────────────────────────────────────────────────────────────


@runtime_checkable
class NotebookBridge(Protocol):
    """Everything kedge asks of a notebook, independent of how it is delivered.

    The live implementation is :class:`kedge.notebook.driver.NotebookDriver`, which drives
    marimo's private ``_code_mode`` API over the kernel's HTTP endpoint. A file-writing
    implementation that edits the notebook ``.py`` and relies on ``marimo edit --watch`` could
    satisfy the structural half of this interface — everything that is a document edit — but not
    :meth:`probe`, :meth:`set_ui_value` or :meth:`run_cell`, which need a live kernel and have no
    file-based equivalent at all.

    Example:
        >>> def cells_of(bridge: NotebookBridge) -> object:
        ...     return bridge.list_cells()
    """

    async def list_cells(self, *, with_code: bool = True) -> tuple[CellInfo, ...]: ...

    async def get_cell(self, target: str | int) -> CellInfo: ...

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
    ) -> MutationResult: ...

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
    ) -> MutationResult: ...

    async def delete_cell(self, target: str | int) -> MutationResult: ...

    async def move_cell(
        self,
        target: str | int,
        *,
        before: str | None = None,
        after: str | None = None,
    ) -> MutationResult: ...

    async def run_cell(self, target: str | int) -> MutationResult: ...

    async def read_graph(self) -> GraphView: ...

    async def probe(self, code: str) -> ProbeResult: ...

    async def set_ui_value(self, element: str, value: Any) -> MutationResult: ...

    async def notify(
        self,
        title: str,
        description: str,
        *,
        variant: str | None = None,
        kind: str = "banner",
    ) -> None: ...

    async def add_packages(
        self,
        *packages: str,
        remove: tuple[str, ...] = (),
    ) -> PackageResult: ...
