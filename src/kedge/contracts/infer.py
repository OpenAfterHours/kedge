"""Draft a contract from a hand-in that actually exists.

Nobody writes a good contract on a blank page, but everybody can correct a generated one.
So ``kedge contract infer <file>`` reads a real hand-in and produces a first draft, and the
job of this module is to make that draft *easy to tighten* rather than to be right on its
own -- which it cannot be, having seen exactly one month of data.

That is why the YAML is written by hand here rather than through ``yaml.safe_dump``. Every
inferred constraint carries a comment saying what it was derived from and what would make it
better: a row-count bound says it is a guess from one sample, a uniqueness constraint says it
held on this file and might not hold generally, and the columns the sample never showed a
null in are marked so the reader can decide whether that is a rule or an accident. A dump of
bare YAML would be the same constraints with none of that, and the user would either accept
them uncritically or delete them wholesale.

The draft is guaranteed to pass against the file it came from -- there is a test that asserts
exactly that -- so the starting point is always green, and every subsequent failure is a real
change in the data rather than an artefact of generation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import polars as pl

from kedge.contracts.model import (
    CONTRACT_SCHEMA_VERSION,
    ChecksumMode,
    ColumnContract,
    Contract,
)
from kedge.errors import ContractError
from kedge.ingest.drift import dtype_name, read_data
from kedge.ingest.model import HandIn, utcnow
from kedge.ingest.store import hash_file

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["Commentary", "infer", "infer_with_notes", "to_yaml", "write_yaml"]

ROW_COUNT_SLACK = 0.5
"""How far either side of the observed row count the drafted bounds sit."""

_MAX_CATEGORY_CARDINALITY = 12
"""A text column with no more distinct values than this gets a suggested allowed-values set."""

_MIN_ROWS_FOR_CATEGORY = 20
"""Below this many rows, distinct values are a coincidence rather than a category."""

_NUMERIC_DTYPES = frozenset(
    {
        "Int8",
        "Int16",
        "Int32",
        "Int64",
        "Int128",
        "UInt8",
        "UInt16",
        "UInt32",
        "UInt64",
        "Float32",
        "Float64",
        "Decimal",
    }
)


# =============================================================================
# INFERENCE
# =============================================================================


def _column_draft(frame: pl.DataFrame, name: str) -> tuple[ColumnContract, list[str]]:
    """Draft one column's contract, and the notes explaining what was guessed."""
    series = frame[name]
    dtype = dtype_name(frame.schema[name])
    nulls = int(series.null_count())
    values = series.drop_nulls()
    distinct = int(values.n_unique())
    notes: list[str] = []

    nullable = nulls > 0
    if not nullable:
        notes.append(f"no nulls in {frame.height:,} rows, so drafted as not-null")

    unique = values.len() > 0 and distinct == values.len() and frame.height > 1
    if unique:
        notes.append(
            f"every value distinct in this sample of {frame.height:,} rows - a real key, "
            f"or a coincidence? set unique: false if it is not guaranteed"
        )

    allowed: list[str] | None = None
    if (
        dtype == "String"
        and frame.height >= _MIN_ROWS_FOR_CATEGORY
        and 0 < distinct <= _MAX_CATEGORY_CARDINALITY
    ):
        allowed = sorted(str(v) for v in values.unique().to_list())
        notes.append(
            f"only {distinct} distinct values seen; a value outside this set will fail, "
            f"so delete allowed_values unless it is genuinely closed"
        )

    if dtype == "String" and values.len() and _mostly_numeric(values):
        notes.append(
            "WARNING: this text column looks like numbers stored as text. If it should be "
            "numeric, fix it at source or cast on read - a text join key matches nothing "
            "(PLAN 2.6)"
        )

    return (
        ColumnContract(
            name=name,
            dtype=dtype,
            required=True,
            nullable=nullable,
            unique=unique,
            allowed_values=allowed,
        ),
        notes,
    )


def _mostly_numeric(values: pl.Series) -> bool:
    sample = values.head(500).cast(pl.String, strict=False)
    parsed = sample.str.replace_all(r"[,\s]", "").cast(pl.Float64, strict=False)
    return sample.len() > 0 and parsed.null_count() <= 0.2 * sample.len()


def _draft(
    frame: pl.DataFrame,
    *,
    name: str,
    sheet: str | None,
    source_name: str,
    checksum: str | None,
) -> tuple[Contract, dict[str, list[str]]]:
    columns: list[ColumnContract] = []
    notes: dict[str, list[str]] = {}
    for column_name in frame.columns:
        draft, column_notes = _column_draft(frame, column_name)
        columns.append(draft)
        if column_notes:
            notes[column_name] = column_notes

    keys = [c.name for c in columns if c.unique and not c.nullable]
    height = frame.height
    contract = Contract(
        schema_version=CONTRACT_SCHEMA_VERSION,
        name=name,
        description=f"Drafted from {source_name}. Review every guess before relying on it.",
        generated_from=source_name,
        generated_at=utcnow(),
        sheet=sheet,
        header_row=None,
        columns=columns,
        allow_extra_columns=True,
        require_column_order=False,
        min_rows=max(0, int(height * (1 - ROW_COUNT_SLACK))),
        max_rows=int(height * (1 + ROW_COUNT_SLACK)) + 1,
        unique_key=keys[:1],
        checksum=checksum,
        checksum_mode=ChecksumMode.WARN,
    )
    return contract, notes


def infer_with_notes(
    source: HandIn | Path,
    *,
    name: str | None = None,
    sheet: str | None = None,
) -> tuple[Contract, dict[str, list[str]]]:
    """Draft a contract and return the per-column commentary alongside it.

    The full result of inference. :func:`infer` is this with the notes dropped, for callers
    that only want the model; ``kedge contract infer`` uses this one, because :func:`to_yaml`
    needs the notes to explain what it guessed.

    Reads through the same data view validation uses -- preamble skipped, totals row excluded
    -- so the draft describes the data rather than the file's decorations, and passes when
    validated back against the file it came from.

    Args:
        source: A received hand-in, or a path to a file that has not been ingested.
        name: Contract name; defaults to the file's stem.
        sheet: Worksheet name for spreadsheet formats.

    Returns:
        ``(contract, notes)`` -- the draft, and commentary keyed by column name.

    Raises:
        ContractError: If the file has no columns to describe.
        kedge.ingest.drift.HandInReadError: If the file cannot be read.
    """
    if isinstance(source, HandIn):
        path, source_name, checksum = source.path, source.original_name, source.sha256
    else:
        path, source_name, checksum = source, source.name, hash_file(source)

    frame, layout = read_data(path, sheet=sheet)
    _require_columns(frame, source_name)
    contract, notes = _draft(
        frame,
        name=name or _default_name(source_name),
        sheet=sheet,
        source_name=source_name,
        checksum=checksum,
    )

    # A column the reader typed is described here as numeric, which is what the process will
    # actually receive -- every reader goes through `read_data`, so the contract, the validation
    # and the notebook cannot disagree about it. But the file itself holds text, and a contract
    # that said `float` with no explanation would be a surprise to anyone who opened the source.
    # So the conversion is stated rather than assumed, per column, with a sample.
    for coercion in layout.coercions:
        notes.setdefault(coercion.column, []).append(
            f"arrived as text and was read as a number ({coercion.rows:,} values; "
            f"e.g. {', '.join(repr(value) for value in coercion.samples)}). Numbers stored as "
            f"text are the usual cause -- a grid pasted out of Excel carries its displayed "
            f"formatting. Declare the column `string` in this contract if it is genuinely an "
            f"identifier and the conversion is wrong."
        )
    logger.info(
        "drafted contract '%s' from %s: %d columns, %d rows",
        contract.name,
        source_name,
        len(contract.columns),
        frame.height,
    )
    return contract, notes


def infer(
    source: HandIn | Path,
    *,
    name: str | None = None,
    sheet: str | None = None,
) -> Contract:
    """Draft a contract from a hand-in, or from any readable file.

    Args:
        source: A received hand-in, or a path to a file that has not been ingested.
        name: Contract name; defaults to the file's stem.
        sheet: Worksheet name for spreadsheet formats.

    Returns:
        A :class:`~kedge.contracts.model.Contract` draft. Render it with :func:`to_yaml`,
        which adds the commentary a human needs in order to tighten it.

    Raises:
        ContractError: If the file has no columns to describe.
        kedge.ingest.drift.HandInReadError: If the file cannot be read.

    Example:
        >>> contract = infer(handin)
        >>> report = validate(handin, contract)
        >>> report.ok
        True
    """
    contract, _notes = infer_with_notes(source, name=name, sheet=sheet)
    return contract


def _require_columns(frame: pl.DataFrame, source_name: str) -> None:
    if frame.width:
        return
    msg = (
        f"{source_name} has no columns to describe, so no contract can be drafted from it. "
        f"Check the sheet name and whether the file has a header row."
    )
    raise ContractError(msg)


def _default_name(filename: str) -> str:
    stem = filename.rsplit(".", maxsplit=1)[0]
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in stem).strip("_").lower()
    return cleaned or "handin"


# =============================================================================
# COMMENTED YAML
# =============================================================================


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _scalar(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    return _quote(str(value))


_DEFAULT_PREAMBLE: tuple[str, ...] = (
    "Generated by `kedge contract infer` from one real hand-in. It describes what that",
    "file contained, which is not the same as what every future hand-in must contain.",
    "Read every line marked 'guess' before this becomes the thing that stops a run.",
    "",
    "This draft passes against the file it came from. Every later failure is therefore a",
    "real change in the data rather than an artefact of how it was generated.",
)

_DEFAULT_ROW_BOUNDS: tuple[str, ...] = (
    f"GUESS: drawn from one sample of rows, plus or minus {ROW_COUNT_SLACK:.0%}. Replace with",
    "what the process actually expects; an unexpected row count is usually an extract",
    "that ran for the wrong period.",
)

_DEFAULT_UNIQUE_KEY: tuple[str, ...] = (
    "GUESS: the first column found to be distinct and non-null in the sample.",
    "Check key uniqueness before any join - a duplicated key multiplies rows and",
    "inflates every total downstream without erroring.",
)


@dataclass(frozen=True, slots=True)
class Commentary:
    """Replacement comment blocks for a contract drafted from something other than a hand-in.

    The prose :func:`to_yaml` writes by default makes a specific evidence claim -- these
    numbers were counted off one real file, and here is what that file was. A contract drafted
    from a workbook sheet instead (:mod:`kedge.contracts.sketch`) would carry a header that is
    simply untrue, and the reader has no way to tell the two apart from the inside. So each
    block that claims evidence can be replaced, one field per block.

    An empty tuple keeps that block's default, so a caller overrides only what it must. Every
    string is one comment line; the leading ``#`` is added by the renderer, and an empty string
    is a bare ``#`` separator.

    Example:
        >>> to_yaml(contract, commentary=Commentary(preamble=("Sketched, not inferred.",)))
    """

    preamble: tuple[str, ...] = ()
    row_bounds: tuple[str, ...] = ()
    unique_key: tuple[str, ...] = ()
    trailer: tuple[str, ...] = ()


def _comments(lines: Sequence[str]) -> list[str]:
    return [f"# {line}" if line else "#" for line in lines]


def to_yaml(
    contract: Contract,
    *,
    notes: dict[str, list[str]] | None = None,
    commentary: Commentary | None = None,
) -> str:
    """Render a contract as YAML a human can tighten.

    Emitted by hand rather than dumped, because the comments are the point: an inferred
    bound with no note beside it is indistinguishable from a considered one, and the reader
    has no way to tell which numbers were chosen and which were counted.

    Args:
        contract: The contract to render.
        notes: Per-column commentary keyed by column name. Inference supplies these; a
            hand-written contract has none.
        commentary: Replacement comment blocks, for a draft whose evidence is not a hand-in.
            The defaults describe :func:`infer`'s.

    Returns:
        YAML text, ready to write to a file and edit.
    """
    notes = notes or {}
    commentary = commentary or Commentary()
    lines: list[str] = [
        f"# Contract: {contract.name}",
        "#",
        *_comments(commentary.preamble or _DEFAULT_PREAMBLE),
        "",
        f"schema_version: {_quote(contract.schema_version)}",
        f"name: {_quote(contract.name)}",
    ]
    if contract.description:
        lines.append(f"description: {_quote(contract.description)}")
    if contract.generated_from:
        lines.append(f"generated_from: {_quote(contract.generated_from)}")
    if contract.generated_at:
        lines.append(f"generated_at: {_quote(contract.generated_at.isoformat())}")

    lines += [
        "",
        "# Worksheet to read. null takes the first sheet.",
        f"sheet: {_scalar(contract.sheet)}",
        "# Zero-based header row. Leave null: kedge detects it, which absorbs a preamble row",
        "# appearing or disappearing without anyone being told.",
        f"header_row: {_scalar(contract.header_row)}",
        "",
        "# A column the sending team adds is usually harmless; one they remove is not.",
        f"allow_extra_columns: {_scalar(contract.allow_extra_columns)}",
        "# Off by default - generated code selects columns by name, so order does not matter.",
        f"require_column_order: {_scalar(contract.require_column_order)}",
        "",
        *_comments(commentary.row_bounds or _DEFAULT_ROW_BOUNDS),
        f"min_rows: {_scalar(contract.min_rows)}",
        f"max_rows: {_scalar(contract.max_rows)}",
    ]

    lines.append("")
    if contract.unique_key:
        lines += [
            *_comments(commentary.unique_key or _DEFAULT_UNIQUE_KEY),
            "unique_key:",
            *[f"  - {_quote(key)}" for key in contract.unique_key],
        ]
    else:
        lines += [
            "# No column was distinct across the sample, so no key was drafted. If the hand-in",
            "# has a composite key, list its columns here.",
            "unique_key: []",
        ]

    lines += [
        "",
        "# SHA-256 of the hand-in this was drafted from.",
        "#   ignore  - do not check",
        "#   warn    - note when a different file arrives (the default; a monthly hand-in",
        "#             is a different file every month)",
        "#   require - fail unless the bytes match, for a fixed reference file",
        f"checksum: {_scalar(contract.checksum)}",
        f"checksum_mode: {_quote(contract.checksum_mode.value)}",
        "",
        # An empty list is written out explicitly. A bare `columns:` key parses back as null
        # rather than as [], and the file would not survive a round trip through
        # load_contract -- which is the one thing a generated draft has to do.
        "columns:" if contract.columns else "columns: []",
    ]

    for column in contract.columns:
        lines.append(f"  - name: {_quote(column.name)}")
        lines.append(f"    dtype: {_quote(column.dtype)}")
        lines.append(f"    required: {_scalar(column.required)}")
        lines.append(f"    nullable: {_scalar(column.nullable)}")
        lines.append(f"    unique: {_scalar(column.unique)}")
        if column.allowed_values is not None:
            lines.append("    allowed_values:")
            lines += [f"      - {_quote(value)}" for value in column.allowed_values]
        if column.dtype in _NUMERIC_DTYPES:
            lines.append("    # No bounds drafted: one month's minimum is not a business rule.")
            lines.append("    # min_value: 0.0")
            lines.append("    # max_value: null")
        for note in notes.get(column.name, []):
            lines.append(f"    # {note}")
        lines.append("")

    if commentary.trailer:
        lines += ["", *_comments(commentary.trailer)]

    return "\n".join(lines).rstrip() + "\n"


def write_yaml(
    contract: Contract, path: Path, *, notes: dict[str, list[str]] | None = None
) -> Path:
    """Write a commented contract draft to a file and return its path.

    Raises:
        ContractError: If the file cannot be written.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(to_yaml(contract, notes=notes), encoding="utf-8")
    except OSError as exc:
        msg = f"could not write the contract draft {path}: {exc}"
        raise ContractError(msg) from exc
    logger.info("wrote contract draft %s", path)
    return path
