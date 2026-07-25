"""The marimo bridge: everything kedge does to a live notebook.

Three modules, and the split between them is the whole design (PLAN 6.1):

* :mod:`kedge.notebook.driver` generates Python source that calls ``marimo._code_mode``, and
  parses what comes back. It is the only module in the repository allowed to mention that API,
  and because generating source is a pure function it is testable without a kernel.
* :mod:`kedge.notebook.kernel` carries that source over HTTP and reassembles the SSE reply. It
  has no opinion about what the source says.
* :mod:`kedge.notebook.discovery` reads marimo's local server registry for diagnostics, and for
  nothing else. kedge never attaches to a server it did not start.

Two more implement the fallback PLAN 6.1 mitigation 4 asks for, so that a marimo release which
withdraws ``_code_mode`` costs kedge a swap rather than a rewrite:

* :mod:`kedge.notebook.codegen` reads and writes the notebook ``.py`` as a document, using the
  standard library and marimo's *public* ``App`` and ``Cell`` and nothing else.
* :mod:`kedge.notebook.filedriver` is the bridge built on it. It satisfies the same
  :class:`~kedge.notebook.model.NotebookBridge` interface and raises the same typed errors; what
  it cannot do -- run, probe, set a UI value, notify -- it refuses loudly rather than faking.

Process lifecycle — spawning, health polling, session bootstrap, teardown — belongs to
:mod:`kedge.lifecycle`, not here. A driver is handed a url, a token and a session id that already
work.
"""

from __future__ import annotations

from kedge.notebook.codegen import (
    CellAnalysis,
    FileCell,
    NotebookDocument,
    NotebookFormatError,
)
from kedge.notebook.discovery import RegisteredServer, registered_servers, registry_dir
from kedge.notebook.driver import (
    MARIMO_PIN,
    BridgeReport,
    BridgeVersionError,
    CellNameError,
    CellNotFoundError,
    CellSyntaxError,
    CycleDetectedError,
    KernelExecutionError,
    MultiplyDefinedError,
    NotebookDriver,
    StaleCellError,
    check_bridge,
    verify_bridge,
)
from kedge.notebook.filedriver import FileNotebookDriver, NoKernelError
from kedge.notebook.kernel import KernelClient, KernelTransportError
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

__all__ = [
    "MARIMO_PIN",
    "BridgeReport",
    "BridgeVersionError",
    "CellAnalysis",
    "CellErrorInfo",
    "CellInfo",
    "CellNameError",
    "CellNotFoundError",
    "CellRef",
    "CellSyntaxError",
    "CycleDetectedError",
    "DefinitionConflict",
    "ExecResult",
    "FileCell",
    "FileNotebookDriver",
    "GraphNode",
    "GraphView",
    "KernelClient",
    "KernelExecutionError",
    "KernelTransportError",
    "MultiplyDefinedError",
    "MutationResult",
    "NoKernelError",
    "NotebookBridge",
    "NotebookDocument",
    "NotebookDriver",
    "NotebookFormatError",
    "PackageResult",
    "ProbeResult",
    "RegisteredServer",
    "StaleCellError",
    "check_bridge",
    "registered_servers",
    "registry_dir",
    "verify_bridge",
]
