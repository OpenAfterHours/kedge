"""Process notes from documentation sheets, cell comments and sibling Word documents."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import pytest
from docx import Document as NewDocument
from openpyxl import Workbook
from openpyxl.comments import Comment

from kedge.analysis.docs import extract_notes, sidecar_documents
from kedge.analysis.model import FindingKind, Severity
from kedge.analysis.workbook import open_workbook

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

_PROSE = (
    "Each month the exposures hand-in arrives from the credit risk team as a CSV "
    "and is pasted into the Data tab without modification."
)
_SECOND_PROSE = (
    "Collateral haircuts are applied from the reference table. Any counterparty missing "
    "a haircut is escalated to the desk before the run is signed off."
)


def _workbook(tmp_path: Path, *, name: str = "process.xlsx") -> Path:
    """A workbook with a documentation tab, a calculation tab, and one cell comment."""
    book = Workbook()

    notes = book.active
    notes.title = "Process Notes"
    notes["A1"] = "Monthly RWA process"
    notes["A2"] = _PROSE
    notes["A4"] = "Haircuts"
    notes["A5"] = _SECOND_PROSE

    data = book.create_sheet("Calc")
    data["A1"] = "ead"
    data["A2"] = 100
    data["B2"] = "=A2*2"
    data["B2"].comment = Comment("Doubled at the desk's request, agreed 2024-03.", "A. Analyst")

    path = tmp_path / name
    book.save(path)
    return path


def _write_docx(path: Path) -> None:
    document = NewDocument()
    document.add_heading("Purpose", level=1)
    document.add_paragraph("Restate the monthly capital position for the trading book.")
    document.add_heading("Steps", level=2)
    document.add_paragraph("Load the hand-in.")
    document.add_paragraph("Apply the haircut table.")
    document.save(path)


# ── documentation sheets ─────────────────────────────────────────────────────────────────────


def test_prose_is_stitched_into_blocks_rather_than_one_note_per_cell(tmp_path: Path) -> None:
    with open_workbook(_workbook(tmp_path)) as handle:
        notes, findings = extract_notes(handle)

    sheet_notes = [note for note in notes if note.source == "sheet"]
    assert findings == []
    assert len(sheet_notes) == 2, "two blank-row-separated blocks, not five cells"
    assert sheet_notes[0].origin == "Process Notes"
    assert sheet_notes[0].heading == "Monthly RWA process"
    assert sheet_notes[0].text == _PROSE
    assert sheet_notes[1].heading == "Haircuts"
    assert sheet_notes[1].location is not None


def test_calculation_sheets_contribute_no_prose(tmp_path: Path) -> None:
    with open_workbook(_workbook(tmp_path)) as handle:
        notes, _ = extract_notes(handle)

    assert not [note for note in notes if note.source == "sheet" and note.origin == "Calc"]


# ── cell comments ────────────────────────────────────────────────────────────────────────────


def test_cell_comments_become_located_notes(tmp_path: Path) -> None:
    with open_workbook(_workbook(tmp_path)) as handle:
        notes, _ = extract_notes(handle)

    comments = [note for note in notes if note.source == "cell_comment"]
    assert len(comments) == 1
    assert comments[0].origin == "Calc"
    assert comments[0].location == "B2"
    assert "agreed 2024-03" in comments[0].text
    assert comments[0].heading == "A. Analyst"


def test_a_workbook_without_comments_costs_nothing_and_says_nothing(tmp_path: Path) -> None:
    book = Workbook()
    book.active["A1"] = 1
    path = tmp_path / "plain.xlsx"
    book.save(path)

    with open_workbook(path) as handle:
        notes, findings = extract_notes(handle)

    assert [note for note in notes if note.source == "cell_comment"] == []
    assert findings == []


# ── sibling Word documents ───────────────────────────────────────────────────────────────────


def test_a_sibling_docx_contributes_notes_with_its_own_headings(tmp_path: Path) -> None:
    path = _workbook(tmp_path)
    _write_docx(tmp_path / "process - procedure.docx")

    with open_workbook(path) as handle:
        notes, findings = extract_notes(handle)

    docx_notes = [note for note in notes if note.source == "docx"]
    assert findings == []
    assert [note.heading for note in docx_notes] == ["Purpose", "Steps"]
    assert "Restate the monthly capital position" in docx_notes[0].text
    assert "Apply the haircut table." in docx_notes[1].text
    assert docx_notes[0].origin.endswith("process - procedure.docx")


def test_a_legacy_doc_fails_loudly_with_a_conversion_hint(tmp_path: Path) -> None:
    path = _workbook(tmp_path)
    legacy = tmp_path / "process - procedure.doc"
    legacy.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00\xff" * 64)

    with open_workbook(path) as handle:
        notes, findings = extract_notes(handle)

    stubs = [note for note in notes if note.source == "doc_stub"]
    assert len(stubs) == 1, "a .doc must never be skipped silently"
    assert "convert" in stubs[0].text.casefold()

    assert len(findings) == 1
    assert findings[0].kind is FindingKind.UNSUPPORTED_FORMAT
    assert findings[0].severity is Severity.WARNING
    assert "procedure.doc" in (findings[0].location or "")
    assert ".docx" in (findings[0].remediation or "")


def test_a_doc_that_is_really_a_docx_is_read_anyway(tmp_path: Path) -> None:
    path = _workbook(tmp_path)
    renamed = tmp_path / "process - procedure.doc"
    _write_docx(renamed)

    with open_workbook(path) as handle:
        notes, findings = extract_notes(handle)

    assert [note for note in notes if note.source == "doc_stub"] == []
    assert [note for note in notes if note.source == "docx"]
    assert findings == []


def test_a_corrupt_docx_produces_a_finding_rather_than_a_crash(tmp_path: Path) -> None:
    path = _workbook(tmp_path)
    (tmp_path / "process - procedure.docx").write_bytes(b"PK\x03\x04 and then nothing valid")

    with open_workbook(path) as handle:
        notes, findings = extract_notes(handle)

    assert [note for note in notes if note.source == "docx"] == []
    assert len(findings) == 1
    assert findings[0].kind is FindingKind.UNPARSEABLE_PART
    assert "process - procedure.docx" in findings[0].message


def test_an_empty_docx_is_read_without_notes_or_findings(tmp_path: Path) -> None:
    path = _workbook(tmp_path)
    NewDocument().save(tmp_path / "process - procedure.docx")

    with open_workbook(path) as handle:
        notes, findings = extract_notes(handle)

    assert [note for note in notes if note.source == "docx"] == []
    assert findings == []


# ── sidecar selection ────────────────────────────────────────────────────────────────────────


def test_sidecar_selection_matches_by_stem_or_convention(tmp_path: Path) -> None:
    workbook = tmp_path / "rwa_monthly.xlsx"
    workbook.touch()
    for name in (
        "rwa_monthly.docx",
        "rwa_monthly - procedure.docx",
        "Process Notes.docx",
        "somebody elses report.docx",
        "~$rwa_monthly.docx",
    ):
        (tmp_path / name).touch()

    found = {path.name for path in sidecar_documents(workbook)}

    assert found == {"rwa_monthly.docx", "rwa_monthly - procedure.docx", "Process Notes.docx"}
    assert "somebody elses report.docx" not in found, "unrelated documents must not be attached"
    assert "~$rwa_monthly.docx" not in found, "Word lock files are not documents"


def test_sidecar_selection_on_a_missing_directory_is_empty() -> None:
    assert sidecar_documents(Path("does") / "not" / "exist" / "book.xlsx") == []


# ── degradation ──────────────────────────────────────────────────────────────────────────────


class _BrokenHandle:
    """A handle that fails at every turn. Every extractor must survive it."""

    path = Path("nowhere.xlsx")

    def __getattr__(self, name: str) -> Any:
        message = f"deliberately broken handle: {name}"
        raise RuntimeError(message)


def test_a_handle_that_raises_on_everything_yields_empty_results() -> None:
    notes, findings = extract_notes(_BrokenHandle())

    assert notes == []
    assert findings == []


def test_a_handle_with_no_views_yields_empty_results() -> None:
    class _Bare:
        path = Path("nowhere.xlsx")

    notes, findings = extract_notes(_Bare())

    assert (notes, findings) == ([], [])


# ── corpus fixture ───────────────────────────────────────────────────────────────────────────


@pytest.mark.corpus
def test_documented_fixture_yields_prose_a_comment_and_the_word_procedure() -> None:
    fixture = FIXTURES / "documented.xlsx"
    if not fixture.is_file():
        pytest.skip("tests/fixtures/documented.xlsx has not landed yet")

    with open_workbook(fixture) as handle:
        notes, findings = extract_notes(handle)

    sources = {note.source for note in notes}
    assert "sheet" in sources, "the documented fixture is meant to carry a notes tab"
    if (FIXTURES / "documented_procedure.docx").is_file():
        assert "docx" in sources
    if (FIXTURES / "procedure_legacy.doc").is_file():
        assert "doc_stub" in sources
        assert any(finding.kind is FindingKind.UNSUPPORTED_FORMAT for finding in findings)


@pytest.mark.corpus
def test_the_hostile_fixture_never_raises() -> None:
    fixture = FIXTURES / "hostile.xlsx"
    if not fixture.is_file():
        pytest.skip("tests/fixtures/hostile.xlsx has not landed yet")

    with zipfile.ZipFile(fixture):
        pass  # it is at least a readable archive
    with open_workbook(fixture) as handle:
        notes, findings = extract_notes(handle)

    assert isinstance(notes, list)
    assert isinstance(findings, list)
