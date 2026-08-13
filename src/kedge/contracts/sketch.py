"""Sketch a contract from a workbook sheet, for the hand-in that does not exist yet.

``kedge contract infer`` needs a hand-in, and the first hand-in is exactly the thing the user
does not have. The process's input arrives by somebody running a query and pasting the grid into
a sheet, so on the first run there is no contract, nothing says which columns the notebook
expects, and the way to find out is to produce an export and watch the check fail. That is the
wrong end of the loop.

The workbook already answers the question. **The pasted sheet is last month's version of that
file**, and the analyser has already described it: header, dtype, null count, distinct count,
numeric range, format anomalies (PLAN 2.3). This module turns that description into a contract,
so the user has something to read *before* their first export instead of after it.

What it must not do is claim to be what :func:`~kedge.contracts.infer.infer` is. The evidence
here is weaker in three ways -- one month, of a paste rather than of the file, seen through a
profiler that may have read only the first rows of it -- and a contract that fails a perfectly
good export gets deleted, after which nothing is checked at all. So the sketch is deliberately
looser than the inferred draft, in ways worth stating plainly:

- **Row bounds are a quarter to four times what was pasted**, and are dropped entirely when
  there was too little of it to bound anything (:data:`MIN_ROWS_FOR_BOUNDS`), or when the
  profiler truncated its scan and the true height is unknown.
- **Nothing is drafted not-null** except the proposed key. A column that was full last month is
  weak evidence that a blank is a defect; the count is put in a comment instead.
- **No permitted-value set and no checksum.** The distinct values of one paste are not a
  closed set, and the workbook's digest is not the hand-in's.
- **One uniqueness assertion at most**, and it is the column named in ``unique_key``.

Dtypes are widened for the same reason, against what an export actually reads back as rather
than against what Excel held (see :data:`_CONTRACT_DTYPES`), and a column the analyser could not
classify is left out of the contract entirely with its reason recorded in the file. An omitted
column is a gap the reader can fill; a wrong dtype is a failure they have to debug.
"""

from __future__ import annotations

import logging
import textwrap
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kedge.analysis.model import SheetRole
from kedge.contracts.infer import Commentary, to_yaml
from kedge.contracts.model import (
    CONTRACT_SCHEMA_VERSION,
    ChecksumMode,
    ColumnContract,
    Contract,
)
from kedge.errors import ContractError
from kedge.ingest.model import utcnow

if TYPE_CHECKING:
    from pathlib import Path

    from kedge.analysis.model import ColumnProfile, SheetInfo, WorkbookAnalysis

logger = logging.getLogger(__name__)

__all__ = [
    "MIN_ROWS_FOR_BOUNDS",
    "ROW_BOUND_FACTOR",
    "SheetSketch",
    "sketch",
    "write_sketch",
]

ROW_BOUND_FACTOR = 4
"""How far either side of the pasted row count the sketched bounds sit.

Four times, not the half either side :func:`~kedge.contracts.infer.infer` uses, because the
evidence is one paste rather than one file. A quiet month is a normal event and must not be an
error; an extract that ran for the wrong period is usually wrong by an order of magnitude, and
that is what is left worth catching.
"""

MIN_ROWS_FOR_BOUNDS = 20
"""Below this many pasted rows, no row bound is drafted at all.

Four times three rows is twelve, which would fail the first ordinary month. Too little evidence
to bound anything is a state to report, not to scale up.
"""

_TRUNCATED_PROFILE = "profile truncated"
"""Prefix of the anomaly the profiler adds when it stopped short of the end of a sheet.

One of :data:`kedge.analysis.profile.FORMAT_ANOMALY_PREFIXES`, matched as a prefix rather than
imported by index; there is a test asserting it is still in that tuple.
"""

_NUMBERS_AS_TEXT = "numbers stored as text"
"""Likewise: the anomaly that makes a text column's dtype not worth asserting."""

_MAX_LISTED_VALUES = 12
"""Above this many distinct values, the paste is not describing a closed set worth suggesting."""

_MAX_VALUE_CHARS = 40
"""Cell values reach a comment line, so they are clipped as well as stripped of newlines."""

_CONTRACT_DTYPES = {
    "integer": "Float64",
    "float": "Float64",
    "boolean": "Boolean",
    "string": "String",
    "date": "Any",
    "datetime": "Any",
    "time": "Any",
    "duration": "Any",
}
"""The analyser's dtype vocabulary mapped onto polars dtype names, widened where it must be.

The analyser names what Excel held; the contract is checked against what an export *reads back
as*, which is not the same thing, and two of the differences are load-bearing.

**A whole-numbered column is contracted ``Float64``, not ``Int64``.** Validation accepts an
integer column where a float was contracted (that widening is
:func:`kedge.contracts.validate._dtype_satisfies`) but not the reverse, so ``Float64`` passes
whether next month's amounts arrive whole or with decimals -- while still failing the one case
that matters, a number arriving as text.

**A date is contracted ``Any``.** ``pl.read_csv`` does not parse dates unless asked and kedge's
reader does not ask, so a CSV export of a real date column reads back as ``String``. A contract
saying ``Date`` would therefore fail a perfectly correct export on day one, which is precisely
the wolf-cry this module exists to avoid. The column is still checked for presence, and the note
in the file tells the reader what the workbook actually held.
"""

_UNCLASSIFIED_DTYPES = {
    "empty": "every cell in the pasted block was blank, so there is no evidence of a type at all",
    "mixed": "the pasted block holds more than one kind of value, so no single dtype is honest",
    "error": "the pasted block holds nothing but Excel error values",
    "other": "the analyser could not classify these values",
}
"""Why a column is left out. A contract that asserts a wrong dtype is worse than one that omits
the column: the omission is a gap somebody can fill, the assertion is a failure they must debug.
"""

_KEY_DTYPES = frozenset({"string", "integer"})
"""Dtypes whose distinctness is evidence of identity.

A float measure whose every value differs is arithmetic coincidence -- two trades of the same
notional are not a data-quality problem -- and ``unique_key`` is the field with teeth, so it is
proposed only where distinctness plausibly means "this is what identifies a row".
"""


_COMMENT_WIDTH = 92
"""Comment lines are wrapped to this, so a workbook's own long sheet name cannot run off the
edge of the file it lands in. Two characters of ``# `` short of the project's 100."""


def _paragraphs(*prose: str) -> tuple[str, ...]:
    """Wrap each paragraph to the comment width, blank-separated. Ready for ``Commentary``."""
    wrapped: list[str] = []
    for paragraph in prose:
        if wrapped:
            wrapped.append("")
        wrapped += textwrap.wrap(paragraph, width=_COMMENT_WIDTH) or [paragraph]
    return tuple(wrapped)


def _bullet(text: str) -> list[str]:
    return textwrap.wrap(
        text, width=_COMMENT_WIDTH, initial_indent="  - ", subsequent_indent="    "
    ) or [f"  - {text}"]


# =============================================================================
# THE RESULT
# =============================================================================


@dataclass(frozen=True, slots=True)
class SheetSketch:
    """A contract drafted from one workbook sheet, with everything needed to justify it.

    The justification travels with the contract rather than being printed and lost: the sheet
    it came from, why that sheet was chosen, the per-column commentary, and every column left
    out with its reason. :meth:`to_yaml` puts all of it in the file, which is where the person
    who has to trust it will be reading.

    Example:
        >>> drafted = sketch(analysis)
        >>> write_sketch(drafted, workspace.contract_path)
    """

    contract: Contract
    sheet: str
    workbook: str
    reason: str
    rows: int
    notes: dict[str, list[str]]
    omitted: tuple[str, ...] = ()
    totals_row_excluded: bool = False

    def to_yaml(self) -> str:
        """Render the sketch as commented YAML that says what it was drafted from."""
        return to_yaml(self.contract, notes=self.notes, commentary=self._commentary())

    def _commentary(self) -> Commentary:
        return Commentary(
            preamble=self._preamble(),
            row_bounds=self._row_bounds_note(),
            unique_key=self._unique_key_note(),
            trailer=self._trailer(),
        )

    def _preamble(self) -> tuple[str, ...]:
        return _paragraphs(
            f"SKETCHED by `kedge contract sketch` from the '{self.sheet}' sheet of "
            f"{self.workbook}, and NOT from a hand-in: nobody has given kedge a real export of "
            f"this file yet. That sheet holds the block somebody pasted into it, so what follows "
            f"describes the file you are about to produce, as it looked the last time it was "
            f"produced.",
            f"Why that sheet: {self.reason}.",
            "One paste is weak evidence, so every constraint here is deliberately loose: a "
            "contract that cries wolf gets deleted, and then nothing is checked at all. It "
            "asserts the column names, a widened dtype for each, and at most one key. It does "
            "not assert nullability, permitted values, or a checksum -- those are yours to add "
            "once you know them.",
            "Once you have one real export, `kedge contract infer <export> --out <this file>` "
            "replaces this with a draft made from the thing itself.",
            "`sheet` below is null on purpose. It names a worksheet inside the *hand-in*, which "
            f"has nothing to do with the '{self.sheet}' sheet this was read from.",
        )

    def _row_bounds_note(self) -> tuple[str, ...]:
        contract = self.contract
        if contract.min_rows is None and contract.max_rows is None:
            return _paragraphs(
                f"No bounds drafted: the pasted block held {self.rows:,} row(s), fewer than the "
                f"{MIN_ROWS_FOR_BOUNDS} it would take to say anything about next month's export. "
                f"Fill these in from what the process actually expects."
            )
        ceiling = (
            f"and up to {ROW_BOUND_FACTOR} times it"
            if contract.max_rows is not None
            else "with no ceiling, because the profiler stopped before the end of the sheet and "
            "the paste may have been taller than it read"
        )
        return _paragraphs(
            f"GUESS, and a deliberately loose one: {self.rows:,} row(s) were pasted into "
            f"'{self.sheet}', so this accepts a quarter of that {ceiling}. A quiet month must "
            f"not be an error; an extract that ran for the wrong period usually misses by more."
            + (
                " The sheet's last row looks like a totals row and is not counted, which is how "
                "a hand-in is read too."
                if self.totals_row_excluded
                else ""
            )
        )

    def _unique_key_note(self) -> tuple[str, ...]:
        if not self.contract.unique_key:
            return ()
        key = self.contract.unique_key[0]
        return _paragraphs(
            f"PROPOSED on evidence: every pasted value of '{key}' was different and none was "
            f"blank, which is what a row identifier looks like. Nothing here checked that it "
            f"holds in any other month.",
            "Check key uniqueness before any join - a duplicated key multiplies rows and "
            "inflates every total downstream without erroring (PLAN 2.6).",
        )

    def _trailer(self) -> tuple[str, ...]:
        if not self.omitted:
            return ()
        return (
            *_paragraphs(
                f"{len(self.omitted)} column(s) of '{self.sheet}' are NOT in this contract. Each "
                f"was left out rather than guessed at, because a wrong dtype fails an export that "
                f"is actually fine. Add any of them by hand once you know what it should be:"
            ),
            "",
            *(line for entry in self.omitted for line in _bullet(entry)),
        )


# =============================================================================
# SKETCHING
# =============================================================================


def sketch(
    analysis: WorkbookAnalysis,
    *,
    sheet: str | None = None,
    name: str | None = None,
) -> SheetSketch:
    """Draft a contract from a workbook sheet's column profiles.

    Args:
        analysis: The workbook analysis. Nothing is read from disk here -- the profiles the
            analyser already produced are the whole input.
        sheet: The worksheet to describe. ``None`` picks the workbook's only sheet classified
            as ``data`` and says so; more than one candidate is a question, not a guess.
        name: Contract name; defaults to the workbook and sheet it came from.

    Returns:
        A :class:`SheetSketch`. Render it with :meth:`SheetSketch.to_yaml`, or write it with
        :func:`write_sketch`, which refuses to replace a contract that already exists.

    Raises:
        ContractError: If no sheet can be chosen, the named sheet is not in the workbook, it
            has no header row, or nothing on it could be described. Every message names what
            was found and what the user can do about it.

    Example:
        >>> drafted = sketch(analyse(Path("process.xlsx")))
        >>> drafted.contract.column_names
        ['trade_id', 'counterparty_name', 'notional']
    """
    chosen, reason = _choose_sheet(analysis, sheet)
    profiles = _profiles_for(analysis, chosen)
    columns, notes, omitted, key = _columns_from(profiles)
    if not columns:
        detail = "; ".join(omitted) or "the analyser profiled no columns on it"
        msg = (
            f"no column of sheet '{chosen.name}' could be described, so no contract was drafted "
            f"from it: {detail}. Draft one from a real export instead with `kedge contract infer`."
        )
        raise ContractError(msg)

    rows = _pasted_rows(profiles, chosen)
    minimum, maximum = _row_bounds(rows, truncated=_was_truncated(profiles))
    workbook = analysis.workbook.filename
    contract = Contract(
        schema_version=CONTRACT_SCHEMA_VERSION,
        name=name or _default_name(workbook, chosen.name),
        description=(
            f"Sketched from the '{chosen.name}' sheet of {workbook} -- a paste of what this "
            f"process was last handed, not a hand-in kedge has seen. Loosened deliberately; "
            f"tighten it once one real export has arrived."
        ),
        generated_from=f"{workbook} sheet '{chosen.name}' (a pasted block, not a hand-in)",
        generated_at=utcnow(),
        # Deliberately not `chosen.name`: this field names a worksheet inside the hand-in, and
        # an export is not the workbook. Pointing it at 'Extract' would send the reader looking
        # for a sheet of that name in a CSV.
        sheet=None,
        header_row=None,
        columns=columns,
        allow_extra_columns=True,
        require_column_order=False,
        min_rows=minimum,
        max_rows=maximum,
        unique_key=[key] if key else [],
        # The workbook's digest is not the hand-in's, and there is no hand-in to record.
        checksum=None,
        checksum_mode=ChecksumMode.IGNORE,
    )
    logger.info(
        "sketched contract '%s' from %s sheet %r: %d column(s), %d omitted, %d pasted row(s)",
        contract.name,
        workbook,
        chosen.name,
        len(columns),
        len(omitted),
        rows,
    )
    return SheetSketch(
        contract=contract,
        sheet=chosen.name,
        workbook=workbook,
        reason=reason,
        rows=rows,
        notes=notes,
        omitted=omitted,
        totals_row_excluded=bool(chosen.has_embedded_totals and rows),
    )


def write_sketch(drafted: SheetSketch, path: Path, *, overwrite: bool = False) -> Path:
    """Write a sketch to a YAML file, refusing to replace one that is already there.

    A contract is a hand-edited file, and the one already at that path may have been tightened
    by somebody who knows what the process receives. Replacing it with a guess drawn off a
    workbook would be a silent downgrade, so it takes ``overwrite=True`` to say so out loud.

    Args:
        drafted: The sketch to write.
        path: Where to write it, usually ``Workspace.contract_path``.
        overwrite: Replace an existing file rather than refusing.

    Returns:
        The path written.

    Raises:
        ContractError: If a contract is already there and ``overwrite`` is false, or if the
            file cannot be written.
    """
    if path.exists() and not overwrite:
        msg = (
            f"there is already a contract at {path}, and nothing was written. A contract is a "
            f"hand-edited file: this one may have been tightened by somebody who knows what the "
            f"process receives, and a sketch drawn off the workbook is a weaker description than "
            f"whatever is in it. Write the sketch elsewhere, or delete that file first."
        )
        raise ContractError(msg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(drafted.to_yaml(), encoding="utf-8")
    except OSError as exc:
        msg = f"could not write the contract sketch {path}: {exc}"
        raise ContractError(msg) from exc
    logger.info("wrote contract sketch %s from sheet %r", path, drafted.sheet)
    return path


# =============================================================================
# CHOOSING THE SHEET
# =============================================================================


def _choose_sheet(analysis: WorkbookAnalysis, requested: str | None) -> tuple[SheetInfo, str]:
    """Return the sheet to describe and the sentence justifying it.

    Named sheets are taken as given, including one classified as something other than ``data``
    -- the user is looking at the workbook and kedge is not. Unnamed, the only ``data`` sheet is
    taken and said out loud; several is a question rather than a guess, because one of them may
    be a reference table somebody maintains by hand rather than a query result, and a contract
    drafted off the wrong sheet is a contract that describes the wrong file entirely.
    """
    if requested is not None:
        return _named_sheet(analysis, requested)

    candidates = [
        sheet
        for sheet in analysis.sheets
        if sheet.role is SheetRole.DATA and _profiles_of(analysis, sheet.name)
    ]
    if len(candidates) == 1:
        chosen = candidates[0]
        signals = ", ".join(chosen.role_signals[:3]) or "no formulas, and a header row"
        return chosen, (
            f"it is the only sheet in {analysis.workbook.filename} kedge classified as data "
            f"({signals}), which is what a pasted query result looks like"
        )
    if candidates:
        listed = ", ".join(f"'{sheet.name}'" for sheet in candidates)
        msg = (
            f"{analysis.workbook.filename} has {len(candidates)} sheets that look like pasted "
            f"data ({listed}), and one of them may be a reference table somebody maintains by "
            f"hand rather than a query result. Name the one this process is handed, with "
            f"--sheet."
        )
        raise ContractError(msg)

    described = ", ".join(f"'{sheet.name}' ({sheet.role.value})" for sheet in analysis.sheets)
    msg = (
        f"no sheet of {analysis.workbook.filename} looks like a block of pasted data, so there "
        f"is nothing to draft a contract from. kedge sees {described or 'no sheets at all'}. "
        f"Name a sheet with --sheet if one of them is the paste, or draft from a real export "
        f"with `kedge contract infer`."
    )
    raise ContractError(msg)


def _named_sheet(analysis: WorkbookAnalysis, requested: str) -> tuple[SheetInfo, str]:
    chosen = analysis.sheet(requested) or next(
        (s for s in analysis.sheets if s.name.casefold() == requested.casefold()), None
    )
    if chosen is None:
        listed = ", ".join(f"'{sheet.name}'" for sheet in analysis.sheets) or "no sheets at all"
        msg = f"{analysis.workbook.filename} has no sheet called '{requested}'. It has {listed}."
        raise ContractError(msg)
    if chosen.role is SheetRole.DATA:
        return chosen, "you named it, and kedge classified it as data too"
    return chosen, (
        f"you named it. kedge classified it as {chosen.role.value} rather than data, so read "
        f"the columns below carefully -- a sheet with formulas on it is not usually the paste"
    )


def _profiles_of(analysis: WorkbookAnalysis, sheet_name: str) -> list[ColumnProfile]:
    return [profile for profile in analysis.profiles if profile.sheet == sheet_name]


def _profiles_for(analysis: WorkbookAnalysis, chosen: SheetInfo) -> list[ColumnProfile]:
    profiles = sorted(_profiles_of(analysis, chosen.name), key=lambda profile: profile.index)
    if not profiles:
        msg = (
            f"sheet '{chosen.name}' of {analysis.workbook.filename} has no column profiles, so "
            f"there is nothing to describe. An empty sheet, or one the analyser could not read."
        )
        raise ContractError(msg)
    if chosen.header_row is None and not any(profile.header for profile in profiles):
        msg = (
            f"sheet '{chosen.name}' of {analysis.workbook.filename} has no header row that kedge "
            f"could find, so its columns have no names to contract on. Put a header row above "
            f"the data, or draft from a real export with `kedge contract infer`."
        )
        raise ContractError(msg)
    return profiles


# =============================================================================
# COLUMNS
# =============================================================================


def _columns_from(
    profiles: list[ColumnProfile],
) -> tuple[list[ColumnContract], dict[str, list[str]], tuple[str, ...], str | None]:
    """Draft one contract column per profile kedge can honestly describe.

    Returns the columns, the per-column commentary, one line per column left out, and the
    proposed key -- the first column whose values were all distinct and none blank.
    """
    columns: list[ColumnContract] = []
    notes: dict[str, list[str]] = {}
    omitted: list[str] = []
    key: str | None = None

    for profile in profiles:
        header = _header_of(profile)
        if header is None:
            omitted.append(f"column {profile.column}: its header cell is blank")
            continue
        if any(column.name == header for column in columns):
            omitted.append(
                f"column {profile.column} ('{header}'): a column to its left already has that "
                f"header, and two columns of one name is not a contract anybody can check"
            )
            continue
        dtype = _CONTRACT_DTYPES.get(profile.dtype)
        if dtype is None:
            reason = _UNCLASSIFIED_DTYPES.get(profile.dtype, "the analyser could not classify it")
            omitted.append(f"column {profile.column} ('{header}'): {reason}")
            continue

        is_key = key is None and _is_key_evidence(profile)
        if is_key:
            key = header
        column_notes = _notes_for(profile, header=header, dtype=dtype, nullable=not is_key)
        columns.append(
            ColumnContract(
                name=header,
                dtype=dtype,
                required=True,
                # Loose on purpose. A column that was full last month is not evidence that a
                # blank next month is a defect, and the count is in the notes for whoever wants
                # to tighten it. The proposed key is the exception: a blank key is a real fault.
                nullable=not is_key,
                unique=is_key,
                allowed_values=None,
            )
        )
        if column_notes:
            notes[header] = column_notes

    return columns, notes, tuple(omitted), key


def _header_of(profile: ColumnProfile) -> str | None:
    """The header as a single-line name, or ``None`` when the cell was blank.

    Whitespace is collapsed because an Excel header cell often holds a line break from Alt+Enter,
    and a newline inside a YAML double-quoted scalar does not survive the round trip as itself.
    """
    if not profile.header:
        return None
    collapsed = " ".join(profile.header.split())
    return collapsed or None


def _is_key_evidence(profile: ColumnProfile) -> bool:
    """Whether this column's values identified its rows in the paste.

    Every value different, none blank, and enough rows for that to mean anything. A profile
    whose distinct count overflowed the profiler's cap answers ``None``, which is not evidence
    either way and is therefore not evidence.
    """
    return (
        profile.dtype in _KEY_DTYPES
        and profile.row_count > 1
        and profile.null_count == 0
        and profile.distinct_count is not None
        and profile.distinct_count == profile.row_count
    )


def _notes_for(profile: ColumnProfile, *, header: str, dtype: str, nullable: bool) -> list[str]:
    """The commentary for one column: what was seen, and what the reader might tighten."""
    notes: list[str] = []
    if header != (profile.header or ""):
        notes.append(
            f"the header cell reads {_clip(profile.header or '')!r}; the contract uses the "
            f"whitespace-collapsed form"
        )

    distinct = (
        "an unknown number of" if profile.distinct_count is None else f"{profile.distinct_count:,}"
    )
    notes.append(
        f"{profile.sheet}!{profile.column}: {profile.row_count:,} pasted row(s), "
        f"{profile.null_count:,} blank, {distinct} distinct, "
        f"held as {profile.dtype} in the workbook"
    )

    if dtype == "Any":
        notes.append(
            f"contracted as Any, not {profile.dtype.capitalize()}: a CSV export writes a date as "
            f"text and kedge's reader does not parse it back, so a date type here would fail an "
            f"export that is perfectly correct. Tighten it once you have seen one"
        )
    elif profile.dtype == "integer":
        notes.append(
            "contracted as Float64 though the paste held whole numbers: an Int64 column satisfies "
            "Float64, so this passes whether next month arrives whole or with decimals, and still "
            "fails a number that arrives as text. Set Int64 if it must be whole"
        )

    if nullable and profile.null_count == 0 and profile.row_count:
        notes.append(
            f"no blanks in the {profile.row_count:,} pasted row(s), but left nullable: one month "
            f"is thin evidence that a blank is a fault. Set nullable: false if it is"
        )

    notes += _value_notes(profile, dtype=dtype)
    notes += [
        f"WARNING: {anomaly}"
        for anomaly in profile.format_anomalies
        if anomaly.startswith(_NUMBERS_AS_TEXT)
    ]
    return notes


def _value_notes(profile: ColumnProfile, *, dtype: str) -> list[str]:
    """Observed values and ranges, offered as comments rather than as constraints."""
    notes: list[str] = []
    if profile.numeric is not None and profile.numeric.min is not None:
        notes.append(
            f"observed in the paste: {_number(profile.numeric.min)} to "
            f"{_number(profile.numeric.max)}"
        )
    if (
        dtype == "String"
        and not profile.redacted
        and profile.distinct_count is not None
        and 1 < profile.distinct_count <= _MAX_LISTED_VALUES
        and len(profile.top_k) == profile.distinct_count
    ):
        values = ", ".join(_clip(str(value)) for value, _count in profile.top_k)
        notes.append(
            f"only {profile.distinct_count} distinct value(s) in the paste ({values}). Add "
            f"allowed_values to close the set - but anything outside it will then fail"
        )
    return notes


def _number(value: float | None) -> str:
    """A number a human reads at a glance: grouped, and never in scientific notation."""
    if value is None:  # pragma: no cover - guarded by the caller
        return "unknown"
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"


def _clip(value: str) -> str:
    """One line, short enough for a comment. Cell values reach the file; newlines must not."""
    collapsed = " ".join(str(value).split())
    if len(collapsed) <= _MAX_VALUE_CHARS:
        return collapsed
    return collapsed[: _MAX_VALUE_CHARS - 1] + "..."


# =============================================================================
# ROWS
# =============================================================================


def _pasted_rows(profiles: list[ColumnProfile], chosen: SheetInfo) -> int:
    """How many rows of data the paste held, as the contract should count them.

    The profiler counts every row below the header, a totals row included; validation reads a
    hand-in with that row already excluded (``kedge.ingest.drift.read_data``). Counting them
    differently would put the bounds out by one against the very thing they are checked on.
    """
    rows = max((profile.row_count for profile in profiles), default=0)
    if chosen.has_embedded_totals and rows:
        return rows - 1
    return rows


def _was_truncated(profiles: list[ColumnProfile]) -> bool:
    return any(
        anomaly.startswith(_TRUNCATED_PROFILE)
        for profile in profiles
        for anomaly in profile.format_anomalies
    )


def _row_bounds(rows: int, *, truncated: bool) -> tuple[int | None, int | None]:
    """Bounds wide enough to survive a quiet month, or none at all where nothing is known."""
    if rows < MIN_ROWS_FOR_BOUNDS:
        return None, None
    minimum = max(0, rows // ROW_BOUND_FACTOR)
    if truncated:
        # The profiler stopped before the end of the sheet, so the paste was at least this tall
        # and might have been far taller. A ceiling drawn from what was read would be a ceiling
        # last month's own data broke through.
        return minimum, None
    return minimum, rows * ROW_BOUND_FACTOR


def _default_name(workbook: str, sheet: str) -> str:
    stem = workbook.rsplit(".", maxsplit=1)[0]
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in f"{stem} {sheet}").strip("_").lower()
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned or "handin"
