"""Hand-in contracts: the thing that turns a notebook into a controlled process.

A contract is a small YAML file per hand-in stating what the file is supposed to look like --
expected columns and dtypes, nullability, row-count bounds, key uniqueness, checksum. The
contract cell is the first thing that runs after ingestion, so a wrong hand-in is rejected
before a single number is computed (PLAN M5).

:func:`validate` returns a **structured report** rather than a boolean, because the useful
question is never "did it pass" but "which column, and by how much". The report carries only
plain data, so it renders unchanged as a ``mo.ui.table`` in the notebook, as a rich table on
the CLI, and as JSON in an audit bundle. Call
:meth:`~kedge.contracts.model.ValidationReport.raise_for_status` to make it fail loudly and
stop the dataflow graph.

:func:`infer` bootstraps the whole thing. Nobody writes a good contract on a blank page, but
everybody can correct a generated one -- so a draft is generated from a real hand-in, and
:func:`to_yaml` renders it with a comment against every guess saying what it was derived from
and what would make it better.

:func:`sketch` bootstraps the bootstrap. A process fed by a pasted query result has no hand-in
until the user produces one, and nothing tells them what the notebook expects until the check
fails -- so a contract is sketched from the pasted sheet's own column profiles instead, which
is last month's version of the file they are about to export. It says so in the file, and it is
deliberately looser than an inferred draft.

The intended notebook sequence::

    contract = kedge.contracts.load(Path("contracts/exposures.yaml"))
    report   = kedge.contracts.validate(handin, contract)
    report.raise_for_status()
"""

from __future__ import annotations

from kedge.contracts.infer import Commentary, infer, infer_with_notes, to_yaml, write_yaml
from kedge.contracts.model import (
    CheckResult,
    CheckStatus,
    ChecksumMode,
    ColumnContract,
    Contract,
    ContractFileError,
    ContractViolationError,
    ValidationReport,
    load_contract,
    save_contract,
)
from kedge.contracts.sketch import SheetSketch, sketch, write_sketch
from kedge.contracts.validate import validate, validate_frame, validate_path

load = load_contract
"""Alias, so a notebook cell reads ``kedge.contracts.load(path)``."""

save = save_contract
"""Alias, so a notebook cell reads ``kedge.contracts.save(contract, path)``."""

__all__ = [
    "CheckResult",
    "CheckStatus",
    "ChecksumMode",
    "ColumnContract",
    "Commentary",
    "Contract",
    "ContractFileError",
    "ContractViolationError",
    "SheetSketch",
    "ValidationReport",
    "infer",
    "infer_with_notes",
    "load",
    "load_contract",
    "save",
    "save_contract",
    "sketch",
    "to_yaml",
    "validate",
    "validate_frame",
    "validate_path",
    "write_sketch",
    "write_yaml",
]
