"""The hand-in contract, and the report validating against one produces.

A contract is a small YAML file stating what a hand-in is supposed to look like: its
columns and their dtypes, which of them may be null, which of them are keys, how many rows
to expect, and optionally the exact checksum of a known-good file. It is the artifact that
turns "a notebook" into "a controlled process" -- and it is the first thing that runs after
ingestion, so a hand-in that is wrong is rejected before a single number is computed
(PLAN M5).

Two different serialisation choices here, and the split is deliberate.

:class:`Contract` and :class:`ColumnContract` are **pydantic**, because a contract is a
hand-edited file. ``extra="forbid"`` turns a typo into an error naming the key rather than a
setting that silently does nothing, and the validation messages name the file. That is
precisely the case CONVENTIONS reserves pydantic for.

:class:`ValidationReport` and :class:`CheckResult` are **frozen dataclasses**. A report is an
internal structure that gets rendered, not a schema that gets parsed. It carries only plain
data -- strings, ints, booleans -- so that the same object renders as a ``mo.ui.table`` in
the notebook, as a rich table on the CLI, and as JSON in an audit bundle, with no widget
library anywhere near this module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from kedge.errors import ContractError

logger = logging.getLogger(__name__)

__all__ = [
    "CONTRACT_SCHEMA_VERSION",
    "CheckResult",
    "CheckStatus",
    "ChecksumMode",
    "ColumnContract",
    "Contract",
    "ContractFileError",
    "ContractViolationError",
    "ValidationReport",
    "load_contract",
    "save_contract",
]

CONTRACT_SCHEMA_VERSION = "1.0"


class ContractFileError(ContractError):
    """A contract file could not be read, parsed, or validated."""


class ContractViolationError(ContractError):
    """A hand-in failed validation against its contract.

    Carries the full :class:`ValidationReport`, so a caller that catches this can render the
    same detail the notebook panel shows rather than only the message.
    """

    def __init__(self, report: ValidationReport) -> None:
        super().__init__(report.render_text())
        self.report = report


# =============================================================================
# THE CONTRACT
# =============================================================================


class ChecksumMode(StrEnum):
    """What to do about the recorded checksum of the file a contract was drafted from."""

    IGNORE = "ignore"
    WARN = "warn"
    REQUIRE = "require"


class _ContractModel(BaseModel):
    """Base for the contract models: frozen, and strict about unrecognised keys."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ColumnContract(_ContractModel):
    """What one column of a hand-in is required to look like.

    Example:
        >>> ColumnContract(name="EAD", dtype="Float64", nullable=False, min_value=0.0)
    """

    name: str
    dtype: str = Field(
        description="polars dtype name, e.g. 'Int64', 'Float64', 'String', 'Date', 'Datetime'."
    )
    required: bool = Field(default=True, description="Whether the column must be present.")
    nullable: bool = Field(default=True, description="Whether null values are permitted.")
    unique: bool = Field(default=False, description="Whether values must be distinct.")
    min_value: float | None = Field(default=None, description="Inclusive lower bound, numerics.")
    max_value: float | None = Field(default=None, description="Inclusive upper bound, numerics.")
    allowed_values: list[str] | None = Field(
        default=None, description="Closed set of permitted values, compared as text."
    )
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            msg = "a column contract needs a name"
            raise ValueError(msg)
        return value


class Contract(_ContractModel):
    """What a hand-in is required to look like, in full.

    Example:
        >>> contract = load_contract(Path("contracts/exposures.yaml"))
        >>> report = validate(handin, contract)
        >>> report.ok
        True
    """

    schema_version: str = CONTRACT_SCHEMA_VERSION
    name: str = Field(description="Short identifier, referenced by HandIn.contract.")
    description: str | None = None
    generated_from: str | None = Field(
        default=None, description="Name of the hand-in this draft was inferred from."
    )
    generated_at: datetime | None = None

    sheet: str | None = Field(default=None, description="Worksheet name; None reads the first.")
    header_row: int | None = Field(
        default=None,
        description="Zero-based header row. None lets kedge detect it, which also absorbs a "
        "preamble row appearing or disappearing.",
    )

    columns: list[ColumnContract] = Field(default_factory=list)
    allow_extra_columns: bool = Field(
        default=True,
        description="Whether columns absent from the contract are permitted. True by default: "
        "a new column is usually harmless, whereas a missing one is not.",
    )
    require_column_order: bool = Field(
        default=False,
        description="Whether contract order must match file order. Off by default, because "
        "generated code selects by name.",
    )

    min_rows: int | None = Field(default=None, ge=0)
    max_rows: int | None = Field(default=None, ge=0)
    unique_key: list[str] = Field(
        default_factory=list,
        description="Columns whose combination identifies a row. Checked before any join, "
        "because a duplicated key silently multiplies rows (PLAN 2.6).",
    )

    checksum: str | None = Field(
        default=None, description="SHA-256 of the hand-in this contract was drafted from."
    )
    checksum_mode: ChecksumMode = ChecksumMode.WARN

    @field_validator("name")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            msg = "a contract needs a name"
            raise ValueError(msg)
        return value

    @property
    def column_names(self) -> list[str]:
        """The contracted column names, in contract order."""
        return [c.name for c in self.columns]

    def column(self, name: str) -> ColumnContract | None:
        """Return the contract for a named column, or ``None``."""
        return next((c for c in self.columns if c.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        """Return the YAML-serialisable form, with unset optional fields omitted."""
        return self.model_dump(mode="json", exclude_none=True)


def load_contract(path: Path) -> Contract:
    """Read and validate a contract from a YAML file.

    Args:
        path: The contract file.

    Returns:
        The validated :class:`Contract`.

    Raises:
        ContractFileError: If the file is absent, is not valid YAML, or has a key or value
            the schema rejects. The message names the file and the offending key.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = (
            f"could not read the contract {path}: {exc}. "
            f"Generate a first draft with `kedge contract infer <handin>`."
        )
        raise ContractFileError(msg) from exc

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        msg = f"{path}: not valid YAML: {exc}"
        raise ContractFileError(msg) from exc

    if not isinstance(raw, dict):
        msg = f"{path}: expected a YAML mapping at the top level, found {type(raw).__name__}"
        raise ContractFileError(msg)

    try:
        return Contract.model_validate(raw)
    except ValidationError as exc:
        problems = "\n".join(
            f"  {'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        msg = f"{path}: the contract is not valid:\n{problems}"
        raise ContractFileError(msg) from exc


def save_contract(contract: Contract, path: Path) -> Path:
    """Write a contract to a YAML file, without comments.

    Use :func:`kedge.contracts.infer.to_yaml` for a first draft instead: it emits the same
    content with the guidance a human needs in order to tighten it.

    Raises:
        ContractFileError: If the file cannot be written.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(contract.to_dict(), sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    except OSError as exc:
        msg = f"could not write the contract {path}: {exc}"
        raise ContractFileError(msg) from exc
    return path


# =============================================================================
# THE REPORT
# =============================================================================


class CheckStatus(StrEnum):
    """Outcome of one check. ``SKIP`` is a result, not an absence of one."""

    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """The outcome of one contract check, as plain data.

    Every field is a string, an int, or ``None``, so this renders unchanged in a
    ``mo.ui.table``, a rich table, or a JSON audit bundle.
    """

    check: str
    status: CheckStatus
    message: str
    column: str | None = None
    expected: str | None = None
    actual: str | None = None
    detail: str | None = None

    @property
    def failed(self) -> bool:
        """Whether this check rejects the hand-in."""
        return self.status is CheckStatus.FAIL

    def to_dict(self) -> dict[str, Any]:
        """Return the plain-data form."""
        return {
            "check": self.check,
            "status": self.status.value,
            "column": self.column,
            "expected": self.expected,
            "actual": self.actual,
            "message": self.message,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """The result of validating one hand-in against one contract.

    Structured rather than a bare boolean, because the useful question is never "did it
    pass" but "which column, and by how much". Rendering is left to the caller: use
    :meth:`to_rows` for a table widget, :meth:`render_text` for a terminal, and
    :meth:`to_dict` for the audit bundle.

    Example:
        >>> report = validate(handin, contract)
        >>> report.raise_for_status()
    """

    contract_name: str
    handin_name: str
    handin_sha256: str
    validated_at: datetime
    checks: tuple[CheckResult, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether every check passed or was skipped."""
        return not self.failures

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        """The checks that reject the hand-in."""
        return tuple(c for c in self.checks if c.status is CheckStatus.FAIL)

    @property
    def warnings(self) -> tuple[CheckResult, ...]:
        """The checks that passed but are worth reading."""
        return tuple(c for c in self.checks if c.status is CheckStatus.WARN)

    @property
    def skipped(self) -> tuple[CheckResult, ...]:
        """The checks that could not run, usually because a column was absent."""
        return tuple(c for c in self.checks if c.status is CheckStatus.SKIP)

    def to_rows(self) -> list[dict[str, Any]]:
        """Return one plain dict per check, ready for ``mo.ui.table`` or a rich table."""
        return [c.to_dict() for c in self.checks]

    def to_dict(self) -> dict[str, Any]:
        """Return the whole report as plain data, for the audit bundle."""
        return {
            "contract": self.contract_name,
            "handin": self.handin_name,
            "sha256": self.handin_sha256,
            "validated_at": self.validated_at.isoformat(),
            "ok": self.ok,
            "passed": len(self.checks) - len(self.failures) - len(self.skipped),
            "failed": len(self.failures),
            "warned": len(self.warnings),
            "skipped": len(self.skipped),
            "checks": self.to_rows(),
        }

    def summary_line(self) -> str:
        """Return a single sentence stating the outcome."""
        verdict = "passed" if self.ok else "FAILED"
        return (
            f"contract '{self.contract_name}' {verdict} for {self.handin_name} "
            f"(sha256:{self.handin_sha256[:12]}): {len(self.checks)} checks, "
            f"{len(self.failures)} failed, {len(self.warnings)} warnings, "
            f"{len(self.skipped)} skipped"
        )

    def render_text(self) -> str:
        """Return a plain-text rendering for CLI output and notebook markdown."""
        lines = [self.summary_line()]
        for check in self.checks:
            if check.status is CheckStatus.PASS:
                continue
            where = f" [{check.column}]" if check.column else ""
            lines.append(f"  {check.status.value.upper():<5}{where} {check.check}: {check.message}")
            if check.expected is not None or check.actual is not None:
                lines.append(f"        expected {check.expected}, found {check.actual}")
        return "\n".join(lines)

    def raise_for_status(self) -> ValidationReport:
        """Return this report, or raise :class:`ContractViolationError` if it failed.

        The loud half of "fails loudly with a structured report". A notebook cell that calls
        this stops the dataflow graph dead when the hand-in is wrong, which is the entire
        point of putting the contract cell first.
        """
        if not self.ok:
            raise ContractViolationError(self)
        return self
