"""Process notes: documentation sheets, cell comments, and a sibling Word document.

The prose is worth as much as the formulas. A workbook that has been run monthly for six years
usually carries its real specification in three places — a tab called "Notes" that nobody has
opened since 2019, a scattering of cell comments explaining the exceptions, and a Word
procedure sitting next to the file on the shared drive — and none of it survives if the
analyser only reads cells.

Three sources, three shapes:

- **Documentation sheets** are stitched, not enumerated. One :class:`ProcessNote` per coherent
  block of prose, with a heading where the sheet has one; emitting a note per cell would turn
  a readable procedure into forty fragments and make the planner's job harder, not easier.
- **Cell comments** come through openpyxl, one note each, located by cell reference.
- **A sibling ``.docx``** comes through python-docx, split on its own headings so
  ``ProcessNote.heading`` carries the author's structure rather than a guess at it.

A sibling ``.doc`` -- the old binary format -- **fails loudly**: a ``doc_stub`` note and a
finding naming the file and the exact conversion command. Silently skipping it would leave the
user believing kedge had read a procedure it never opened (PLAN 1.5).

References:
- PLAN.md 1.5, 2.4 (documentation sheets), M1.
"""

from __future__ import annotations

import logging
import posixpath
import re
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any
from xml.etree import ElementTree

from docx import Document
from openpyxl.utils import get_column_letter

from kedge.analysis.model import Finding, FindingKind, ProcessNote, Severity
from kedge.analysis.profile import workbook_view

if TYPE_CHECKING:
    from kedge.analysis.workbook import WorkbookHandle

logger = logging.getLogger(__name__)

__all__ = ["extract_notes", "sidecar_documents"]

MAX_NOTE_CHARS = 4_000
"""Per-note ceiling. A note longer than this is truncated with an explicit marker."""

_MAX_SHEETS = 100
_MAX_DOC_ROWS = 500
_MAX_DOC_COLS = 20
_MAX_NOTES_PER_SHEET = 50
_MAX_COMMENTS = 500
_MAX_SIDECARS = 10
_MAX_DOCX_NOTES = 200
_MAX_DOCX_TABLES = 20

_LONG_TEXT_CHARS = 40
"""A cell holding this much text is prose, not a label. The signal PLAN 2.4 classifies on."""

_MIN_BLOCK_CHARS = 25
_MAX_HEADING_CHARS = 80
_TRUNCATION_MARKER = " [... truncated]"

# `\b` is no use here: an underscore is a word character, so `\bprocedure\b` misses
# `procedure_legacy.doc` -- which is exactly how people name these files.
_DOC_SHEET_NAME_RE = re.compile(
    r"(?i)(?<![a-z])(read ?me|notes?|doc|docs|documentation|instructions?|process|procedure|"
    r"method|methodology|guide|about|overview|assumptions?|change ?log|history|control|"
    r"glossary)(?![a-z])"
)
_DOC_FILENAME_RE = re.compile(
    r"(?i)(?<![a-z])(process|procedure|method|notes?|readme|instructions?|guide|sop|runbook|"
    r"documentation|handover|spec|specification)(?![a-z])"
)

_PATH_ATTRS = ("path", "workbook_path", "source_path", "filename", "file")
_ZIP_ATTRS = ("zf", "zip", "archive", "zip_file", "zipfile")
_COMMENTS_PART_RE = re.compile(r"(?i)^xl/comments.*\.xml$")
"""Excel writes ``xl/comments1.xml``; openpyxl writes ``xl/comments/comment1.xml``. Both count.
Only a pre-check -- the authoritative mapping comes from each sheet's relationships."""

_OLE2_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_SIGNATURE = b"PK\x03\x04"

_DOC_REMEDIATION = (
    "Convert it to .docx (Word: File > Save As > Word Document, or "
    "`soffice --headless --convert-to docx <file>`) and re-run kedge inspect."
)


# ── handle access ────────────────────────────────────────────────────────────────────────────


def _handle_path(handle: WorkbookHandle) -> Path | None:
    """The workbook's own path, which is what sibling documents are found relative to."""
    for attribute in _PATH_ATTRS:
        value = getattr(handle, attribute, None)
        if isinstance(value, Path):
            return value
        if isinstance(value, str) and value:
            return Path(value)
    return None


def _handle_zip(handle: WorkbookHandle) -> zipfile.ZipFile | None:
    for attribute in _ZIP_ATTRS:
        value = getattr(handle, attribute, None)
        if isinstance(value, zipfile.ZipFile):
            return value
    return None


def _worksheets(handle: WorkbookHandle) -> list[tuple[str, Any]]:
    """Named worksheets from the formula view, so a formula is distinguishable from prose."""
    book = workbook_view(handle, data_only=False) or workbook_view(handle, data_only=True)
    if book is None:
        logger.warning("no openpyxl view on the workbook handle; skipping in-sheet notes")
        return []

    named: list[tuple[str, Any]] = []
    for name in list(book.sheetnames)[:_MAX_SHEETS]:
        try:
            named.append((str(name), book[name]))
        except (KeyError, TypeError):
            logger.debug("sheet %r could not be opened for note extraction", name)
    return named


def _truncate(text: str) -> str:
    if len(text) <= MAX_NOTE_CHARS:
        return text
    return text[: MAX_NOTE_CHARS - len(_TRUNCATION_MARKER)] + _TRUNCATION_MARKER


# ── documentation sheets ─────────────────────────────────────────────────────────────────────


def _is_bold(cell: Any) -> bool:
    try:
        return bool(cell.font is not None and cell.font.bold)
    except Exception:  # a style openpyxl cannot resolve is not worth a traceback
        return False


class _Line:
    """One row of a documentation sheet, flattened to a single line of prose."""

    __slots__ = ("bold", "columns", "row", "text")

    def __init__(self, row: int, columns: list[int], text: str, *, bold: bool) -> None:
        self.row = row
        self.columns = columns
        self.text = text
        self.bold = bold


def _read_lines(worksheet: Any) -> tuple[list[_Line], int, int, int]:
    """Flatten a sheet into prose lines, and count what kind of sheet it looks like.

    Row and column numbers come from the iteration, never from the cell: in read-only mode a
    gap in a row is an ``EmptyCell``, which has no idea where it is.
    """
    max_row = min(int(getattr(worksheet, "max_row", 0) or 0), _MAX_DOC_ROWS)
    max_column = min(int(getattr(worksheet, "max_column", 0) or 0), _MAX_DOC_COLS)
    if max_row <= 0 or max_column <= 0:
        return [], 0, 0, 0

    lines: list[_Line] = []
    formula_cells = 0
    text_cells = 0
    long_text_cells = 0

    rows = worksheet.iter_rows(min_row=1, max_row=max_row, min_col=1, max_col=max_column)
    for row_number, row in enumerate(rows, start=1):
        pieces: list[str] = []
        columns: list[int] = []
        bold = False
        for column_number, cell in enumerate(row, start=1):
            value = cell.value
            if not isinstance(value, str):
                continue
            text = value.strip()
            if not text:
                continue
            if text.startswith("="):
                formula_cells += 1
                continue
            text_cells += 1
            if len(text) >= _LONG_TEXT_CHARS:
                long_text_cells += 1
            pieces.append(text)
            columns.append(column_number)
            bold = bold or _is_bold(cell)
        if pieces:
            lines.append(_Line(row_number, columns, " ".join(pieces), bold=bold))

    return lines, formula_cells, text_cells, long_text_cells


def _looks_documentary(
    title: str, formula_cells: int, text_cells: int, long_text_cells: int
) -> bool:
    """PLAN 2.4's documentation signal: mostly text, few or no formulas, long strings."""
    if text_cells == 0:
        return False
    if _DOC_SHEET_NAME_RE.search(title):
        return True
    return long_text_cells >= 3 and formula_cells * 4 <= text_cells


def _blocks(lines: list[_Line]) -> list[list[_Line]]:
    """Group lines into blocks, split wherever the sheet leaves a blank row."""
    grouped: list[list[_Line]] = []
    current: list[_Line] = []
    previous_row = None
    for line in lines:
        if previous_row is not None and line.row > previous_row + 1 and current:
            grouped.append(current)
            current = []
        current.append(line)
        previous_row = line.row
    if current:
        grouped.append(current)
    return grouped


def _looks_like_heading(line: _Line, block: list[_Line]) -> bool:
    if len(block) < 2:
        return False
    if len(line.text) > _MAX_HEADING_CHARS:
        return False
    return line.bold or not line.text.endswith((".", ":", ";", ","))


def _block_note(sheet_name: str, block: list[_Line]) -> ProcessNote | None:
    heading: str | None = None
    body = block
    if _looks_like_heading(block[0], block):
        heading = block[0].text
        body = block[1:]

    text = "\n".join(line.text for line in body).strip()
    if heading is None and len(text) < _MIN_BLOCK_CHARS:
        return None  # a stray label, not a note

    columns = [column for line in block for column in line.columns] or [1]
    first, last = block[0].row, block[-1].row
    start = f"{get_column_letter(min(columns))}{first}"
    end = f"{get_column_letter(max(columns))}{last}"
    return ProcessNote(
        source="sheet",
        origin=sheet_name,
        location=start if start == end else f"{start}:{end}",
        text=_truncate(text),
        heading=heading,
    )


def _sheet_notes(handle: WorkbookHandle) -> list[ProcessNote]:
    notes: list[ProcessNote] = []
    for title, worksheet in _worksheets(handle):
        try:
            lines, formula_cells, text_cells, long_text_cells = _read_lines(worksheet)
        except Exception:
            logger.warning("could not read sheet %r for process notes", title, exc_info=True)
            continue
        if not _looks_documentary(title, formula_cells, text_cells, long_text_cells):
            continue

        found = [
            note for block in _blocks(lines) if (note := _block_note(title, block)) is not None
        ]
        if found:
            logger.debug("stitched %d note(s) from documentation sheet %r", len(found), title)
        notes.extend(found[:_MAX_NOTES_PER_SHEET])
    return notes


# ── cell comments ────────────────────────────────────────────────────────────────────────────


def _part_names(handle: WorkbookHandle) -> frozenset[str]:
    names = getattr(handle, "part_names", None)
    if isinstance(names, (frozenset, set, list, tuple)):
        return frozenset(str(name) for name in names)
    archive = _handle_zip(handle)
    if archive is None:
        return frozenset()
    try:
        return frozenset(archive.namelist())
    except (OSError, RuntimeError, zipfile.BadZipFile):
        return frozenset()


def _read_part(handle: WorkbookHandle, name: str) -> bytes | None:
    reader = getattr(handle, "read_part", None)
    if callable(reader):
        try:
            return reader(name)
        except Exception:
            logger.debug("could not read part %s", name, exc_info=True)
            return None
    archive = _handle_zip(handle)
    if archive is None:
        return None
    try:
        return archive.read(name)
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile):
        return None


def _resolve_target(base_part: str, target: str) -> str:
    """Resolve a relationship target against the part that declared it."""
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(base_part), target))


def _comment_parts(handle: WorkbookHandle) -> list[tuple[str, str]]:
    """Map each sheet to its comments part, via that sheet's relationships.

    Read-only openpyxl drops cell comments entirely, so they are recovered from the archive.
    The sheet-to-part mapping comes from :meth:`WorkbookHandle.structure`, which already owns
    it -- deriving it a second time here would be two things to keep in step.
    """
    names = _part_names(handle)
    if not any(_COMMENTS_PART_RE.match(name) for name in names):
        logger.debug("workbook carries no comment parts")
        return []

    pairs: list[tuple[str, str]] = []
    for sheet in list(getattr(handle, "sheet_names", []))[:_MAX_SHEETS]:
        structure = handle.structure(sheet)
        part = getattr(structure, "part", None)
        if not part:
            continue
        rels_path = f"{posixpath.dirname(part)}/_rels/{posixpath.basename(part)}.rels"
        data = _read_part(handle, rels_path)
        if not data:
            continue
        try:
            root = ElementTree.fromstring(data)
        except ElementTree.ParseError:
            logger.debug("relationships for sheet %r are not well-formed", sheet)
            continue
        for relationship in root:
            rel_type = relationship.get("Type", "")
            target = relationship.get("Target", "")
            if not target or rel_type.rsplit("/", maxsplit=1)[-1] != "comments":
                continue
            pairs.append((sheet, _resolve_target(part, target)))
    return pairs


def _strip_threaded_preamble(text: str) -> str:
    """Drop the compatibility preamble Excel writes into a threaded comment's legacy mirror."""
    if not text.lstrip().startswith("[Threaded comment]"):
        return text
    _, marker, body = text.partition("Comment:")
    return body.strip() if marker else text


def _comments_from_part(data: bytes, sheet: str) -> list[ProcessNote]:
    root = ElementTree.fromstring(data)
    authors = [
        (element.text or "").strip()
        for element in root.iter()
        if element.tag.rsplit("}", maxsplit=1)[-1] == "author"
    ]

    notes: list[ProcessNote] = []
    for element in root.iter():
        if element.tag.rsplit("}", maxsplit=1)[-1] != "comment":
            continue
        runs = [
            node.text or ""
            for node in element.iter()
            if node.tag.rsplit("}", maxsplit=1)[-1] == "t"
        ]
        text = _strip_threaded_preamble("".join(runs).strip())
        if not text:
            continue
        author_index = element.get("authorId", "")
        author = (
            authors[int(author_index)]
            if author_index.isdigit() and int(author_index) < len(authors)
            else None
        )
        notes.append(
            ProcessNote(
                source="cell_comment",
                origin=sheet,
                location=element.get("ref") or None,
                text=_truncate(text),
                heading=author or None,
            )
        )
    return notes


def _comment_notes(handle: WorkbookHandle) -> list[ProcessNote]:
    notes: list[ProcessNote] = []
    for sheet, part in _comment_parts(handle):
        data = _read_part(handle, part)
        if not data:
            continue
        try:
            found = _comments_from_part(data, sheet)
        except ElementTree.ParseError as exc:
            logger.warning("could not parse %s: %s", part, exc)
            continue
        notes.extend(found)
        if len(notes) >= _MAX_COMMENTS:
            logger.warning("stopping at %d cell comments", _MAX_COMMENTS)
            return notes[:_MAX_COMMENTS]
    return notes


# ── sibling Word documents ───────────────────────────────────────────────────────────────────


def sidecar_documents(workbook_path: Path) -> list[Path]:
    """Find Word documents beside the workbook that plausibly document it.

    Matched two ways: a file whose stem relates to the workbook's own (``rwa_monthly.xlsx``
    and ``rwa_monthly - procedure.docx``), or one named conventionally (``Process Notes``,
    ``SOP``, ``Runbook``). Deliberately not "every ``.docx`` in the folder" — on a shared drive
    that attaches somebody else's procedure to this workbook's analysis.

    Args:
        workbook_path: Path of the workbook being analysed.

    Returns:
        Matching ``.docx`` and ``.doc`` paths, sorted, capped at ten. Word lock files
        (``~$...``) are excluded.
    """
    directory = workbook_path.parent
    if not directory.is_dir():
        return []

    stem = workbook_path.stem.casefold()
    matches: list[Path] = []
    try:
        entries = sorted(directory.iterdir())
    except OSError as exc:
        logger.warning("could not list %s for sibling documents: %s", directory, exc)
        return []

    for entry in entries:
        if entry.suffix.casefold() not in (".docx", ".doc") or entry.name.startswith("~$"):
            continue
        candidate = entry.stem.casefold()
        related = candidate.startswith(stem) or stem.startswith(candidate)
        if related or _DOC_FILENAME_RE.search(entry.stem):
            matches.append(entry)
    return matches[:_MAX_SIDECARS]


def _docx_notes(path: Path) -> list[ProcessNote]:
    """Read a .docx into notes, one per heading section plus one per table."""
    document = Document(str(path))
    notes: list[ProcessNote] = []
    origin = str(path)
    heading: str | None = None
    buffer: list[str] = []
    block_start = 1

    def flush(end: int) -> None:
        text = "\n".join(buffer).strip()
        if not text:
            return
        location = (
            f"paragraph {block_start}" if block_start == end else f"paragraphs {block_start}-{end}"
        )
        notes.append(
            ProcessNote(
                source="docx",
                origin=origin,
                location=location,
                text=_truncate(text),
                heading=heading,
            )
        )

    for number, paragraph in enumerate(document.paragraphs, start=1):
        text = paragraph.text.strip()
        if not text:
            continue
        style = ""
        try:
            style = str(paragraph.style.name or "") if paragraph.style is not None else ""
        except Exception:  # a document with a broken style table still has readable text
            logger.debug("could not read a paragraph style in %s", path, exc_info=True)
        if style.startswith("Heading") or style in ("Title", "Subtitle"):
            flush(number - 1)
            buffer = []
            heading = text
            block_start = number
            continue
        if not buffer:
            block_start = number
        buffer.append(text)
        if len(notes) >= _MAX_DOCX_NOTES:
            break
    flush(len(document.paragraphs))

    for index, table in enumerate(document.tables[:_MAX_DOCX_TABLES], start=1):
        rows = [
            " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
            for row in table.rows
        ]
        text = "\n".join(line for line in rows if line).strip()
        if text:
            notes.append(
                ProcessNote(
                    source="docx",
                    origin=origin,
                    location=f"table {index}",
                    text=_truncate(text),
                    heading=heading,
                )
            )
    return notes


def _doc_stub(path: Path) -> tuple[ProcessNote, Finding]:
    """A legacy .doc: say so, name the file, and give the conversion command."""
    message = (
        f"'{path.name}' sits beside the workbook but is in the legacy .doc binary format, "
        f"which kedge cannot read. Any process notes in it are missing from this analysis."
    )
    note = ProcessNote(
        source="doc_stub",
        origin=str(path),
        location=None,
        text=f"{message} {_DOC_REMEDIATION}",
        heading=None,
    )
    finding = Finding(
        kind=FindingKind.UNSUPPORTED_FORMAT,
        severity=Severity.WARNING,
        message=message,
        location=str(path),
        remediation=_DOC_REMEDIATION,
    )
    return note, finding


def _sidecar_notes(workbook_path: Path) -> tuple[list[ProcessNote], list[Finding]]:
    notes: list[ProcessNote] = []
    findings: list[Finding] = []

    for path in sidecar_documents(workbook_path):
        try:
            with path.open("rb") as handle:
                header = handle.read(8)
        except OSError as exc:
            logger.warning("could not open %s: %s", path, exc)
            continue

        if path.suffix.casefold() == ".doc" and not header.startswith(_ZIP_SIGNATURE):
            if not header.startswith(_OLE2_SIGNATURE):
                logger.debug("%s is neither OLE2 nor a zip; reporting it as unreadable", path)
            note, finding = _doc_stub(path)
            notes.append(note)
            findings.append(finding)
            continue

        try:
            found = _docx_notes(path)
        except Exception as exc:  # python-docx raises a wide family on a damaged package
            logger.warning("could not read %s: %s", path, exc)
            findings.append(
                Finding(
                    kind=FindingKind.UNPARSEABLE_PART,
                    severity=Severity.WARNING,
                    message=f"could not read the Word document '{path.name}': {exc}",
                    location=str(path),
                    remediation=(
                        "Open it in Word and re-save it. If it will not open there either, "
                        "the file is damaged and its process notes are lost."
                    ),
                )
            )
            continue

        logger.info("read %d process note(s) from %s", len(found), path.name)
        notes.extend(found)

    return notes, findings


# ── entry point ──────────────────────────────────────────────────────────────────────────────


def extract_notes(handle: WorkbookHandle) -> tuple[list[ProcessNote], list[Finding]]:
    """Collect every piece of process prose attached to a workbook.

    Draws on documentation sheets, cell comments, and sibling Word documents, in that order.
    Each source is isolated: an unreadable sheet, a workbook openpyxl will not walk, or a
    damaged ``.docx`` costs that source alone and produces a warning or a finding, never an
    exception (CONVENTIONS non-negotiable 4).

    Args:
        handle: The open workbook handle. Its ``path`` is what sibling documents are found
            relative to; without one, only in-workbook sources are read.

    Returns:
        The notes found, in source order, and any findings raised while reading them. A
        workbook with no prose anywhere returns two empty lists and that is a normal result.
    """
    notes: list[ProcessNote] = []
    findings: list[Finding] = []

    for source, reader in (("sheet", _sheet_notes), ("cell_comment", _comment_notes)):
        try:
            notes.extend(reader(handle))
        except Exception:
            logger.warning("the %s note extractor failed; continuing", source, exc_info=True)

    workbook_path = _handle_path(handle)
    if workbook_path is None:
        logger.debug("workbook handle exposes no path; skipping sibling Word documents")
    else:
        try:
            sidecar_notes, sidecar_findings = _sidecar_notes(workbook_path)
        except Exception:
            logger.warning("could not scan for sibling Word documents", exc_info=True)
        else:
            notes.extend(sidecar_notes)
            findings.extend(sidecar_findings)

    logger.info("extracted %d process note(s)", len(notes))
    return notes, findings
