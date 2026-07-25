"""The exception hierarchy for the whole of kedge.

Every exception kedge raises inherits from :class:`KedgeError`, so a caller that wants to
catch "anything kedge did wrong" has exactly one name to catch. The hierarchy is deliberately
flat: one subclass per subsystem, each named after the subsystem it belongs to. Modules define
their own narrower exceptions locally (per CONVENTIONS "define exceptions in the module that
raises them") and inherit from the appropriate subsystem error here.

Error messages are expected to state what was being attempted, what was found, and what the
user can do about it.
"""

from __future__ import annotations

__all__ = [
    "AnalysisError",
    "ConfigError",
    "ContractError",
    "IngestError",
    "KedgeError",
    "KernelError",
    "NotebookError",
    "ReconciliationError",
    "ValidationError",
    "WorkspaceError",
]


class KedgeError(Exception):
    """Base class for every error kedge raises deliberately."""


class ConfigError(KedgeError):
    """Configuration could not be loaded, validated, or resolved.

    Covers malformed TOML, unknown or mistyped keys in a config file, and a missing or
    unreadable API key in the OS keyring.
    """


class WorkspaceError(KedgeError):
    """A workspace path, directory, or marker file could not be established or read."""


class AnalysisError(KedgeError):
    """The workbook analyser failed outright.

    Note that most analyser problems are *not* this: a sub-extractor that cannot make sense of
    part of a workbook records a finding and keeps going (CONVENTIONS non-negotiable 4). This
    is for the cases where there is nothing to analyse at all, such as a file that is not a
    readable workbook.
    """


class NotebookError(KedgeError):
    """The marimo notebook process or the notebook file itself could not be managed.

    Spawning, health-polling, session resolution, scaffolding, and teardown all live here.
    """


class KernelError(KedgeError):
    """Code submitted to the live marimo kernel failed, or the kernel became unreachable."""


class ValidationError(KedgeError):
    """Generated code was rejected by the pre-commit validation gate.

    Raised for syntax errors, marimo single-definition violations, policy breaches, and output
    style breaches. Distinct from :class:`ContractError`, which is about data, not code.
    """


class ContractError(KedgeError):
    """A hand-in failed validation against its declared contract."""


class ReconciliationError(KedgeError):
    """Reconciliation against the workbook's cached values could not be performed.

    A reconciliation that *runs* and reports differences is a result, not an error. This is for
    the cases that prevent the comparison happening at all, such as an absent value cache.
    """


class IngestError(KedgeError):
    """A hand-in could not be received into the managed store."""
