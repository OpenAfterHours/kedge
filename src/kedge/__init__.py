"""kedge — turn manual Excel processes into reviewable, reproducible marimo notebooks.

The names re-exported here are the stable, public surface of the package: the exception
hierarchy every subsystem raises through, the layered configuration, and the :class:`Workspace`
that owns paths, config, and the marimo session for one workbook.

Everything else is internal and should be imported from its own module. In particular
``kedge.lifecycle`` (process supervision), ``kedge.analysis`` (the workbook analyser),
``kedge.xl`` (Excel-semantics polars namespace) and ``kedge.notebook`` (the marimo bridge) are
subsystem modules with their own conventions, not part of this top-level surface.
"""

from __future__ import annotations

from kedge.config import Config, LoadedConfig, get_api_key, load_config
from kedge.errors import (
    AnalysisError,
    ConfigError,
    ContractError,
    IngestError,
    KedgeError,
    KernelError,
    NotebookError,
    ReconciliationError,
    ValidationError,
    WorkspaceError,
)
from kedge.workspace import MarimoMarker, MarimoSession, Workspace

__version__ = "0.0.4"

__all__ = [
    "AnalysisError",
    "Config",
    "ConfigError",
    "ContractError",
    "IngestError",
    "KedgeError",
    "KernelError",
    "LoadedConfig",
    "MarimoMarker",
    "MarimoSession",
    "NotebookError",
    "ReconciliationError",
    "ValidationError",
    "Workspace",
    "WorkspaceError",
    "__version__",
    "get_api_key",
    "load_config",
]
