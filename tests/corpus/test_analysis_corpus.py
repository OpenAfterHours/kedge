"""The analyser over the committed workbook corpus, asserted against the manifest.

PLAN 7.2 says everything downstream is bounded by M1's quality, and M1's quality is exactly
what this file measures. Eight synthetic workbooks span the axes the analyser has to survive --
a clean pipeline, a Power Query mashup, legacy SQL connections, a four-deep cross-sheet chain,
a mostly-typed workbook, a documented one, a deliberately hostile one and one with no cached
values -- and `tests/fixtures/manifest.py` declares, by hand, what is in each of them.

Two invariants are load-bearing and get their own sections at the bottom of this file:

* **CONVENTIONS non-negotiable 4** -- `hostile.xlsx` must produce a COMPLETE `WorkbookAnalysis`
  with findings populated and never a traceback. Every extractor is also driven on its own, so
  a failure localises to the extractor rather than to "analysis broke".
* **CONVENTIONS non-negotiable 6's precondition** -- `no_cached_values.xlsx` must be detected as
  having no baseline at analysis time and must say so loudly. The reconciliation half of that
  argument lives in `test_reconcile_corpus.py`; this file covers the half that happens first.

The manifest is hand-written and independent of `generate.py`, and this file keeps it that way.
Nothing here was copied from what `analyse()` returns: every expectation was derived from the
fixture -- from the formulas the generator writes, the rows it skips, the parts it hand-builds --
and where a value could not be derived with confidence it was left out rather than pasted in.
A manifest populated from analyser output would assert only that the analyser agrees with
itself, which is worse than no test because it looks like one.

The dependency graph is claimed from both sides, because a suite that only ever asks "is this
edge present?" is satisfied by an analyser that reports every pair. Five fixtures declare their
complete edge set and the other three declare individual edges that must be absent; the
manifest says which and why. An invented upstream edge inverts dependency order in a generated
scaffold, so it is at least as damaging as a missing one.

The findings are claimed from both sides for the same reason, and it matters more here than
anywhere: PLAN M1 makes the findings list the "extra control" pitch and says to lead with it,
so a false positive in it costs the reader more than a false positive anywhere else in the
analyser. Every fixture declares the complete set of kinds it produces (`finding_kinds`), and
this file turns that into one assertion per declared kind -- it must be raised -- and one per
kind the fixture does not produce -- it must not be. Decomposed rather than a single
set-equality per fixture, because one known defect would otherwise put a whole fixture's set
claim behind one xfail and the other nineteen kinds would stop being tested. `forbidden_edges`
is decomposed for the same reason.

**Where the analyser and the manifest genuinely disagree**, the assertion is marked
`xfail(strict=True)` with a reason naming the defect (see `KNOWN_*_GAPS` below). Strict, so that
fixing the analyser turns the xfail into a failure and forces the marker to be deleted: a
silently-absorbed fix leaves a test that no longer tests anything. If one of these goes red
after a change to `src/kedge/analysis/`, the change is probably the fix and the marker is the
thing to remove.

References:
- PLAN.md M1 (the corpus and graceful degradation), 1.5, 2.1, 2.4, 2.7, 4.5, 7.2.
- CONVENTIONS.md non-negotiables 4 and 6.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import manifest
import pytest
from manifest import ConnectionExpectation, FixtureExpectation, OperationExpectation

from kedge.analysis.analyse import analyse
from kedge.analysis.classify import classify_sheets
from kedge.analysis.connections import extract_connections
from kedge.analysis.docs import extract_notes
from kedge.analysis.findings import collect_findings, external_link_targets
from kedge.analysis.graph import build_graph
from kedge.analysis.model import (
    ExcelPattern,
    ExtractionStatus,
    Finding,
    FindingKind,
    LogicalOperation,
    Severity,
    SheetRole,
    WorkbookAnalysis,
)
from kedge.analysis.powerquery import extract_power_query
from kedge.analysis.profile import profile_sheet
from kedge.analysis.regions import detect_regions
from kedge.analysis.values import cached_value_coverage, scan_sheet
from kedge.analysis.workbook import open_workbook, read_identity

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.corpus

HOSTILE = manifest.HOSTILE
NO_CACHE = manifest.NO_CACHED_VALUES
CLEAN = manifest.CLEAN_PIPELINE
DOCUMENTED = manifest.DOCUMENTED


# =============================================================================
# The manifest's proposed vocabulary, mapped onto the analyser's
# =============================================================================

#: `manifest.FINDING_KINDS` predates `kedge.analysis.model` and says so; this is the promised
#: mapping. Two names on the manifest side mean something the model splits differently:
#: `external_workbook_link` is "a formula reads another workbook" (EXTERNAL_LINK), which is a
#: different claim from "the workbook it reads is not there"
#: (UNRESOLVABLE_EXTERNAL_LINK) -- hostile.xlsx sets both traps and the manifest declares both.
FINDING_KIND_BY_PROPOSED_NAME: dict[str, FindingKind] = {
    "circular_reference": FindingKind.CIRCULAR_REFERENCE,
    "dates_stored_as_text": FindingKind.MIXED_DATE_FORMAT,
    "dead_region": FindingKind.DEAD_REGION,
    "document_attached_by_filename": FindingKind.DOCUMENT_ATTACHED_BY_FILENAME,
    "duplicate_headers": FindingKind.DUPLICATE_HEADER,
    "error_cell": FindingKind.ERROR_VALUE,
    "external_workbook_link": FindingKind.EXTERNAL_LINK,
    "hardcoded_constant": FindingKind.HARDCODED_CONSTANT,
    "hidden_columns": FindingKind.HIDDEN_COLUMN,
    "hidden_sheet": FindingKind.HIDDEN_SHEET,
    "iferror_swallow": FindingKind.IFERROR_SWALLOW,
    "inconsistent_formula_in_region": FindingKind.INCONSISTENT_FORMULA,
    "manual_override_block": FindingKind.MANUAL_OVERRIDE_BLOCK,
    "merged_cells": FindingKind.MERGED_CELLS,
    "no_cached_values": FindingKind.MISSING_CACHED_VALUES,
    "numbers_stored_as_text": FindingKind.TEXT_FORMATTED_NUMBER,
    "preamble_rows": FindingKind.PREAMBLE_ROWS,
    "totals_row_in_data": FindingKind.EMBEDDED_TOTALS_ROW,
    "unresolvable_external_link": FindingKind.UNRESOLVABLE_EXTERNAL_LINK,
    "unresolved_defined_name": FindingKind.BROKEN_NAMED_RANGE,
    "unsupported_format": FindingKind.UNSUPPORTED_FORMAT,
    "volatile_function": FindingKind.VOLATILE_FUNCTION,
}

#: The other direction, which is the one that goes stale silently. A `FindingKind` no fixture
#: can reach is a detector with no corpus behind it, and the only way to see that is to make
#: every member of the enum account for itself: mapped above, or listed here with why.
FINDING_KINDS_NOT_IN_THE_CORPUS: dict[FindingKind, str] = {
    FindingKind.MACRO_PRESENT: (
        "no fixture carries a VBA project -- test_every_fixture_produces_a_complete_analysis "
        "asserts has_vba is False for all eight -- so nothing reaches the detector. A "
        "macro-driven workbook is a hole in the corpus rather than in the analyser (PLAN 6.2)."
    ),
    FindingKind.ITERATIVE_CALCULATION: (
        "no fixture enables iterative calculation; generate.py never writes the calcPr "
        "attribute and openpyxl does not default it on."
    ),
    FindingKind.PARTIAL_CACHED_VALUES: (
        "every fixture is all cached or none. Deliberate: 'partial' is the dangerous middle, "
        "so test_no_fixture_reports_a_partial_cache_it_does_not_have asserts its ABSENCE "
        "workbook by workbook, which is the claim worth having until a fixture exists for it."
    ),
    FindingKind.UNPARSEABLE_PART: (
        "nothing in the corpus fails to parse, and the strongest form of that claim is "
        "test_the_hostile_workbook_produces_a_complete_analysis_and_never_raises asserting "
        "this kind is absent even from hostile.xlsx."
    ),
}

#: Proposed names the model has no equivalent for. Declared rather than dropped, so the gap is
#: a line of code somebody has to delete rather than an omission nobody notices.
UNMAPPED_FINDING_KINDS = frozenset(
    {
        # hostile.xlsx blanks rows 45 and 60 and leaves column F empty. Neither is in
        # `FindingKind`. A blank row inside a data region breaks a naive read the same way a
        # totals row does, so this is a gap in the vocabulary rather than in the detectors.
        "blank_rows",
        "blank_columns",
    }
)


# =============================================================================
# Where the analyser and the corpus genuinely disagree
# =============================================================================

_SELF_REFERENCE = (
    "graph._edges drops self-edges (`if candidate.operation.id == downstream: continue`), so a "
    "one-cell region that reads itself never joins the cyclic core and _cycles never looks at "
    "it. Circular!E3 is `=E3+1`: the simplest circular reference there is, and it is invisible."
)

_SWEPT_BOUNDING_BOX = (
    "graph._edges compares bounding rectangles at both ends and neither end knows about the "
    "holes in a region. _swept_extents returns the box that bounds a fill's references rather "
    "than the references its occupied cells actually make, and the candidate is tested as "
    "`candidate.min_row..max_row x min_col..max_col` rather than as its own occupied cells. "
    "Messy!D5 occupies rows 5-80 except 30, 45, 47 and 60, so its sweep of RC[-2] comes back "
    "B5:B80 -- which crosses the B30/D30 subtotal region sitting in the row-30 hole, and an "
    "upstream edge is invented from the subtotal into the fill that produced it."
)

#: (fixture, upstream, downstream) -> why an edge the fixture rules out is reported anyway.
KNOWN_EDGE_GAPS: dict[tuple[str, str, str], str] = {
    ("hostile.xlsx", "Messy!B30", "Messy!D5"): _SWEPT_BOUNDING_BOX,
}

_PREAMBLE_SUPPRESSED = (
    "findings._sheet_findings raises PREAMBLE_ROWS only for sheets classified data, output or "
    "unknown, and Messy classifies as calculation. The fact itself is extracted -- see "
    "test_the_header_and_preamble_rows_are_found_where_the_fixture_put_them, which passes -- so "
    "this is the finding being suppressed rather than the preamble being missed."
)
_NO_TEXT_DATE_DETECTOR = (
    "There is no detector for a column of dates held as text. The nearest kind, "
    "MIXED_DATE_FORMAT, means something else -- findings._mixed_type_columns needs at least two "
    "real dates AND two non-dates in one column -- and Messy!G is 100% text, so it fires for "
    "neither reason."
)
_OVERRIDE_BLOCK_NEEDS_A_REGION = (
    "findings._inconsistency_findings only sees typed-in cells that sit INSIDE a formula region. "
    "Overrides!A4:D17 has no formula anywhere on the sheet, so there is no region for it to be "
    "inside and the block is invisible. This is the PLAN 2.2 checkpoint trigger, so it matters "
    "more than most."
)
_INTEGER_CONSTANTS_IGNORED = (
    "findings._region_findings only counts a numeric literal as interesting when it is "
    "non-integral or at least 1,000. Output!B5 is `=ROUND(B3/47,2)`: an integer headcount with "
    "no provenance, and exactly the kind of magic number the finding exists for."
)

#: (fixture, proposed finding kind) -> why too few of that finding are raised. Applies to the
#: count assertion only: a kind can be raised in the right place and still be short of the
#: fixture's `minimum`, which is what the two entries at the bottom are.
KNOWN_FINDING_GAPS: dict[tuple[str, str], str] = {
    ("hostile.xlsx", "preamble_rows"): _PREAMBLE_SUPPRESSED,
    ("hostile.xlsx", "dates_stored_as_text"): _NO_TEXT_DATE_DETECTOR,
    ("mostly_manual.xlsx", "manual_override_block"): _OVERRIDE_BLOCK_NEEDS_A_REGION,
    ("hostile.xlsx", "circular_reference"): _SELF_REFERENCE,
    ("mostly_manual.xlsx", "hardcoded_constant"): _INTEGER_CONSTANTS_IGNORED,
}

#: (fixture, proposed finding kind, declared location) -> why THAT location is missed. Kept
#: separate from the count gaps above and deliberately not derived from them: two thirds of the
#: hardcoded constants in mostly_manual.xlsx are found, and pretending otherwise would stop the
#: two that work from being tested.
KNOWN_LOCATION_GAPS: dict[tuple[str, str, str], str] = {
    ("hostile.xlsx", "preamble_rows", "Messy!1:3"): _PREAMBLE_SUPPRESSED,
    ("hostile.xlsx", "dates_stored_as_text", "Messy!G"): _NO_TEXT_DATE_DETECTOR,
    ("hostile.xlsx", "circular_reference", "Circular!E3"): _SELF_REFERENCE,
    ("mostly_manual.xlsx", "manual_override_block", "Overrides!A4:D17"): (
        _OVERRIDE_BLOCK_NEEDS_A_REGION
    ),
    ("mostly_manual.xlsx", "hardcoded_constant", "Output!B5"): _INTEGER_CONSTANTS_IGNORED,
}

#: (fixture, proposed finding kind) -> why a kind the fixture declares is not raised at all.
#: Deliberately separate from KNOWN_FINDING_GAPS, which is about counts: two of the three
#: entries there name a kind that IS raised and merely short of its minimum, and marking the
#: presence assertion with those would turn a passing test into a strict-xfail failure.
KNOWN_ABSENT_KIND_GAPS: dict[tuple[str, str], str] = {
    ("hostile.xlsx", "preamble_rows"): _PREAMBLE_SUPPRESSED,
    ("hostile.xlsx", "dates_stored_as_text"): _NO_TEXT_DATE_DETECTOR,
    ("mostly_manual.xlsx", "manual_override_block"): _OVERRIDE_BLOCK_NEEDS_A_REGION,
}

_TOTALS_LABEL_IS_A_METRIC_NAME = (
    "classify.detect_embedded_totals calls any label matching '(grand |sub-)?total' in the "
    "first three columns a totals row, and treats every one above the last populated row as "
    "mid-sheet. A `metric | value` sheet is nothing but labels: Output!A2 reads 'total_net', "
    "so row 2 of a five-row metrics table is reported as a totals row inside the data, with "
    "the remediation 'filter the totals rows out on load, and recompute the totals' -- on a "
    "sheet where every row is a metric and there is nothing to filter. Nothing in the "
    "detector asks whether the row sits inside a data region, or whether the sheet has one."
)
_TOTALS_LABEL_IS_A_HEADER = (
    "the same detector as _TOTALS_LABEL_IS_A_METRIC_NAME, one row higher: the header row is "
    "a row of labels like any other, so legacy_sql!Summary's 'total_notional' header and "
    "C_Aggregate's 'total' header are each reported as a mid-sheet totals row at row 1. "
    "Neither workbook has a totals row anywhere. "
    "test_the_header_row_is_never_reported_as_an_embedded_totals_row makes the narrower "
    "version of this claim; this one says the kind should not be raised on these fixtures "
    "at all."
)

#: (fixture, proposed finding kind) -> why a kind the fixture does NOT produce is raised
#: anyway. This is the register the corpus had no room for before `finding_kinds`: an
#: over-report is a defect exactly as a missed finding is, and it is the more expensive of
#: the two, because a reader who finds one invented entry in the findings list stops trusting
#: the rest of it (PLAN M1).
KNOWN_SPURIOUS_KIND_GAPS: dict[tuple[str, str], str] = {
    ("clean_pipeline.xlsx", "totals_row_in_data"): _TOTALS_LABEL_IS_A_METRIC_NAME,
    ("mostly_manual.xlsx", "totals_row_in_data"): _TOTALS_LABEL_IS_A_METRIC_NAME,
    ("no_cached_values.xlsx", "totals_row_in_data"): _TOTALS_LABEL_IS_A_METRIC_NAME,
    ("legacy_sql.xlsx", "totals_row_in_data"): _TOTALS_LABEL_IS_A_HEADER,
    ("cross_sheet_chain.xlsx", "totals_row_in_data"): _TOTALS_LABEL_IS_A_HEADER,
}

_HEADER_NEEDS_A_CACHED_VALUE_BELOW_IT = (
    "classify.detect_header_row reads `SheetScan.head_rows`, which comes from the cached-value "
    "view, and requires the row below the candidate header to be populated. On an all-formula "
    "sheet in a workbook with no cached values every row below the header reads None, so the "
    "header is never confirmed and comes back as None. clean_pipeline!Calc has the same shape "
    "and IS found, because that workbook has a cache -- which is the tell. A missed header row "
    "means the profiler and any generated read_excel treat the labels as data."
)

#: (fixture, sheet) -> why the header row the generator wrote is not found.
KNOWN_HEADER_GAPS: dict[tuple[str, str], str] = dict.fromkeys(
    (
        ("cross_sheet_chain.xlsx", "B_Enrich"),
        ("documented.xlsx", "Calc"),
        ("no_cached_values.xlsx", "Calc"),
    ),
    _HEADER_NEEDS_A_CACHED_VALUE_BELOW_IT,
)

#: (fixture, sheet) -> why the classifier lands somewhere the corpus does not accept.
KNOWN_ROLE_GAPS: dict[tuple[str, str], str] = {
    ("clean_pipeline.xlsx", "Output"): (
        "SheetRole.OUTPUT cannot win against CALCULATION for any sheet that clears "
        "CALCULATION's formula gate, which is every terminal sheet in this corpus. "
        "classify_sheet's first OUTPUT signal requires `signals.references` -- the sheet must "
        "read another sheet -- but that same condition awards CALCULATION 2.0, on top of 3.0 "
        "for `formula_count >= 20 or formula_share > 0.25` and 1.0 for 3+ regions. OUTPUT caps "
        "at 4.0. A small terminal sheet under both formula thresholds would still win the role, "
        "so this is unwinnable here rather than unreachable everywhere -- do not rewrite the "
        "scoring table more aggressively than that."
    ),
    ("clean_pipeline.xlsx", "Params"): (
        "Four named ranges point at Params and nothing else does, but PARAMETERS scores 4.0 "
        "(2.0 small-and-literal, 2.0 named ranges) against DATA's 4.0 (3.0 no formulas, 1.0 "
        "header row) and loses the tie on SheetRole declaration order. Confidence comes back "
        "0.45, the floor, which is the classifier telling the truth about the coin toss."
    ),
}
KNOWN_ROLE_GAPS.update(
    dict.fromkeys(
        (
            ("cross_sheet_chain.xlsx", "D_Report"),
            ("mostly_manual.xlsx", "Output"),
            ("no_cached_values.xlsx", "Output"),
        ),
        KNOWN_ROLE_GAPS[("clean_pipeline.xlsx", "Output")],
    )
)


def _xfail(reason: str | None) -> tuple[pytest.MarkDecorator, ...]:
    """Mark a parameter xfail when a known defect covers it, otherwise leave it alone."""
    return () if reason is None else (pytest.mark.xfail(reason=reason, strict=True),)


# =============================================================================
# Fixtures: analyse each workbook once
# =============================================================================


@pytest.fixture(scope="module")
def analyses() -> dict[str, WorkbookAnalysis]:
    """Every fixture analysed once. Eight workbooks and 6,000-odd formula cells is not free."""
    return {name: analyse(expectation.path) for name, expectation in manifest.FIXTURES.items()}


@pytest.fixture(scope="module")
def hostile(analyses: dict[str, WorkbookAnalysis]) -> WorkbookAnalysis:
    """The fixture that exists to prove the analyser cannot be made to raise."""
    return analyses[HOSTILE.filename]


def _fixture_params() -> list[Any]:
    """One parameter per fixture, identified by filename."""
    return [
        pytest.param(expectation, id=expectation.filename)
        for expectation in manifest.iter_fixtures()
    ]


# =============================================================================
# Helpers
# =============================================================================

_ROW_LOCATION_RE = re.compile(r"^row (\d+)$")
_CELL_RE = re.compile(r"^\$?[A-Z]{1,3}\$?(\d+)$")


def _by_anchor(analysis: WorkbookAnalysis) -> dict[str, LogicalOperation]:
    """Operations keyed by sheet-qualified anchor, the way the manifest names them."""
    return {f"{operation.sheet}!{operation.anchor}": operation for operation in analysis.operations}


def _places(finding: Finding) -> set[str]:
    """Every place a finding can reasonably be said to point at, sheet-qualified.

    `Finding.location` is not one shape: region detectors put a bare anchor there and carry the
    sheet separately, sheet detectors put a comma-joined sample of ranges or `row 4`, and the
    external-link detector puts a part path. This flattens all of them so the manifest can go
    on declaring plain `Sheet!Cell` coordinates.
    """
    places: set[str] = set()
    sheet = finding.sheet
    if sheet:
        places.add(sheet)
    for piece in (part.strip() for part in (finding.location or "").split(",")):
        if not piece:
            continue
        places.add(piece)
        if sheet and "!" not in piece:
            places.add(f"{sheet}!{piece}")
        row = _ROW_LOCATION_RE.match(piece)
        if row and sheet:
            places.add(f"{sheet}!{row.group(1)}")
    return places


def _points_at(finding: Finding, expected: str) -> bool:
    """Whether a finding points at a place the fixture set a trap.

    A finding that names a row rather than a cell counts for any cell on that row: the duplicate
    header at `Messy!B4` is reported as `row 4`, which is the honest granularity for a finding
    about the header row as a whole. Falling back to the message covers findings whose subject
    is not a coordinate at all, such as a defined name.
    """
    places = _places(finding)
    if expected in places:
        return True
    sheet, _, cell = expected.partition("!")
    cell_match = _CELL_RE.match(cell)
    if cell_match and f"{sheet}!{cell_match.group(1)}" in places:
        return True
    return expected in (finding.message or "")


def _finding_params() -> list[Any]:
    """One parameter per (fixture, declared finding kind), skipping unmapped kinds."""
    params: list[Any] = []
    for expectation in manifest.iter_fixtures():
        for declared in expectation.findings:
            if declared.kind in UNMAPPED_FINDING_KINDS:
                continue
            reason = KNOWN_FINDING_GAPS.get((expectation.filename, declared.kind))
            params.append(
                pytest.param(
                    expectation,
                    declared,
                    marks=_xfail(reason),
                    id=f"{expectation.filename}-{declared.kind}",
                )
            )
    return params


def _location_params() -> list[Any]:
    """One parameter per (fixture, declared finding kind, declared location)."""
    params: list[Any] = []
    for expectation in manifest.iter_fixtures():
        for declared in expectation.findings:
            if declared.kind in UNMAPPED_FINDING_KINDS:
                continue
            for location in declared.locations:
                reason = KNOWN_LOCATION_GAPS.get((expectation.filename, declared.kind, location))
                params.append(
                    pytest.param(
                        expectation,
                        declared.kind,
                        location,
                        marks=_xfail(reason),
                        id=f"{expectation.filename}-{declared.kind}-{location}",
                    )
                )
    return params


def _declared_kind_params() -> list[Any]:
    """One parameter per (fixture, declared finding kind), skipping unmapped kinds."""
    return [
        pytest.param(
            expectation,
            kind,
            marks=_xfail(KNOWN_ABSENT_KIND_GAPS.get((expectation.filename, kind))),
            id=f"{expectation.filename}-{kind}",
        )
        for expectation in manifest.iter_fixtures()
        for kind in expectation.finding_kinds
        if kind not in UNMAPPED_FINDING_KINDS
    ]


def _undeclared_kind_params() -> list[Any]:
    """One parameter per (fixture, kind the fixture does not produce).

    The complement of `finding_kinds` over the mapped vocabulary, which is the findings
    equivalent of `dependency_edges_are_complete`: small enough to enumerate exhaustively,
    where the edge set is quadratic and has to be sampled by hand into `forbidden_edges`.
    """
    return [
        pytest.param(
            expectation,
            kind,
            marks=_xfail(KNOWN_SPURIOUS_KIND_GAPS.get((expectation.filename, kind))),
            id=f"{expectation.filename}-{kind}",
        )
        for expectation in manifest.iter_fixtures()
        if expectation.finding_kinds_are_complete
        for kind in sorted(set(FINDING_KIND_BY_PROPOSED_NAME) - set(expectation.finding_kinds))
    ]


def _sheet_params(
    keep: Callable[[manifest.SheetExpectation], bool] | None = None,
    gaps: dict[tuple[str, str], str] | None = None,
) -> list[Any]:
    """One parameter per declared sheet, optionally filtered and optionally marked.

    Each sheet-level assertion takes its own known-gap registry rather than a shared one: a
    sheet whose role is wrong still has the right header row and the right operation count,
    and those assertions have to stay live.
    """
    return [
        pytest.param(
            expectation,
            sheet,
            marks=_xfail((gaps or {}).get((expectation.filename, sheet.name))),
            id=f"{expectation.filename}-{sheet.name}",
        )
        for expectation in manifest.iter_fixtures()
        for sheet in expectation.sheets
        if keep is None or keep(sheet)
    ]


def _operation_params(keep: Callable[[OperationExpectation], bool] | None = None) -> list[Any]:
    """One parameter per declared logical operation, optionally filtered."""
    return [
        pytest.param(expectation, operation, id=f"{expectation.filename}-{operation.anchor}")
        for expectation in manifest.iter_fixtures()
        for operation in expectation.operations
        if keep is None or keep(operation)
    ]


def _edge_params() -> list[Any]:
    """One parameter per declared dependency edge."""
    return [
        pytest.param(
            expectation, upstream, downstream, id=f"{expectation.filename}-{upstream}>{downstream}"
        )
        for expectation in manifest.iter_fixtures()
        for upstream, downstream in expectation.dependency_edges
    ]


def _forbidden_edge_params() -> list[Any]:
    """One parameter per edge a fixture rules out, marked where the analyser reports it."""
    return [
        pytest.param(
            expectation,
            upstream,
            downstream,
            marks=_xfail(KNOWN_EDGE_GAPS.get((expectation.filename, upstream, downstream))),
            id=f"{expectation.filename}-{upstream}>{downstream}",
        )
        for expectation in manifest.iter_fixtures()
        for upstream, downstream in expectation.forbidden_edges
    ]


# =============================================================================
# A complete analysis, for every fixture
# =============================================================================


@pytest.mark.parametrize("expectation", _fixture_params())
def test_every_fixture_produces_a_complete_analysis(
    expectation: FixtureExpectation, analyses: dict[str, WorkbookAnalysis]
) -> None:
    """PLAN M1: one workbook in, one complete WorkbookAnalysis out, always."""
    analysis = analyses[expectation.filename]

    assert analysis.workbook.filename == expectation.filename
    assert analysis.workbook.file_format == "xlsx"
    assert re.fullmatch(r"[0-9a-f]{64}", analysis.workbook.sha256)
    assert analysis.workbook.size_bytes == expectation.path.stat().st_size
    assert analysis.workbook.has_vba is False
    assert analysis.schema_version
    assert analysis.kedge_version
    assert analysis.generated_at.tzinfo is not None
    assert analysis.analysis_duration_seconds is not None


@pytest.mark.parametrize("expectation", _fixture_params())
def test_the_analysis_survives_the_round_trip_to_json_and_back(
    expectation: FixtureExpectation, analyses: dict[str, WorkbookAnalysis]
) -> None:
    """The analysis is an artifact that travels: to the plan, to the agent, to disk.

    Every structural section must survive intact. Full object equality is deliberately not
    claimed: `ColumnProfile.head`, `tail` and `sample` are `list[Any]`, so a sampled date
    leaves as a datetime and comes back as an ISO string. That is JSON's limitation rather
    than the analyser's, and the second pass being byte-identical is the property that
    actually matters -- an artifact written and re-read does not drift further on each trip.
    """
    analysis = analyses[expectation.filename]

    encoded = analysis.model_dump_json()
    assert json.loads(encoded)["workbook"]["filename"] == expectation.filename

    restored = WorkbookAnalysis.model_validate_json(encoded)
    assert restored.model_dump_json() == encoded
    assert restored.workbook == analysis.workbook
    assert restored.sheets == analysis.sheets
    assert restored.operations == analysis.operations
    assert restored.graph == analysis.graph
    assert restored.findings == analysis.findings
    assert restored.connections == analysis.connections
    assert restored.power_query == analysis.power_query
    assert restored.named_ranges == analysis.named_ranges
    assert restored.notes == analysis.notes
    assert restored.cached_values == analysis.cached_values


@pytest.mark.parametrize("expectation", _fixture_params())
def test_the_summary_digest_agrees_with_the_analysis_it_summarises(
    expectation: FixtureExpectation, analyses: dict[str, WorkbookAnalysis]
) -> None:
    """PLAN M4's progressive disclosure: the digest is smaller, not different."""
    analysis = analyses[expectation.filename]
    summary = analysis.summary()

    assert summary["workbook"] == expectation.filename
    assert [sheet["name"] for sheet in summary["sheets"]] == list(expectation.sheet_names)
    assert summary["operation_count"] == len(analysis.operations)
    assert summary["reconcilable"] is expectation.cached_values_present
    assert sum(summary["findings_by_kind"].values()) == len(analysis.findings)


# =============================================================================
# Sheets, and the roles the classifier lands on
# =============================================================================


@pytest.mark.parametrize("expectation", _fixture_params())
def test_the_sheets_come_back_in_workbook_order(
    expectation: FixtureExpectation, analyses: dict[str, WorkbookAnalysis]
) -> None:
    analysis = analyses[expectation.filename]

    assert [sheet.name for sheet in analysis.sheets] == list(expectation.sheet_names)
    assert [sheet.index for sheet in analysis.sheets] == list(range(len(expectation.sheets)))


@pytest.mark.parametrize(("expectation", "declared"), _sheet_params(gaps=KNOWN_ROLE_GAPS))
def test_each_sheet_is_classified_with_a_role_the_corpus_accepts(
    expectation: FixtureExpectation,
    declared: manifest.SheetExpectation,
    analyses: dict[str, WorkbookAnalysis],
) -> None:
    """PLAN 2.4. `role_alternatives` exists because some sheets are genuinely ambiguous."""
    sheet = analyses[expectation.filename].sheet(declared.name)

    assert sheet is not None
    acceptable = {declared.role, *declared.role_alternatives}
    assert sheet.role.value in acceptable, (
        f"{declared.name} classified {sheet.role.value}, not one of {sorted(acceptable)}; "
        f"signals were {sheet.role_signals}"
    )
    assert 0.0 < sheet.role_confidence <= 1.0
    assert sheet.role_signals, "a classification with no stated reason is not reviewable"


@pytest.mark.parametrize("expectation", _fixture_params())
def test_the_hidden_sheets_are_exactly_the_ones_the_fixture_hides(
    expectation: FixtureExpectation, analyses: dict[str, WorkbookAnalysis]
) -> None:
    analysis = analyses[expectation.filename]

    hidden = {sheet.name for sheet in analysis.sheets if sheet.is_hidden or sheet.is_very_hidden}
    assert hidden == {sheet.name for sheet in expectation.sheets if sheet.hidden}


@pytest.mark.parametrize(
    ("expectation", "declared"),
    _sheet_params(keep=lambda sheet: sheet.header_row is not None, gaps=KNOWN_HEADER_GAPS),
)
def test_the_header_and_preamble_rows_are_found_where_the_fixture_put_them(
    expectation: FixtureExpectation,
    declared: manifest.SheetExpectation,
    analyses: dict[str, WorkbookAnalysis],
) -> None:
    """The structural facts a default `read_excel` would get wrong."""
    sheet = analyses[expectation.filename].sheet(declared.name)

    assert sheet is not None
    assert sheet.header_row == declared.header_row
    assert sheet.preamble_rows == declared.preamble_rows


@pytest.mark.parametrize("expectation", _fixture_params())
def test_the_formula_cells_are_attributed_to_the_sheets_that_hold_them(
    expectation: FixtureExpectation, analyses: dict[str, WorkbookAnalysis]
) -> None:
    """Per-sheet counts must add up to the workbook total the manifest declares."""
    analysis = analyses[expectation.filename]

    with_formulas = {sheet.name for sheet in analysis.sheets if sheet.formula_cell_count}
    assert with_formulas == {sheet.name for sheet in expectation.sheets if sheet.has_formulas}
    assert sum(sheet.formula_cell_count for sheet in analysis.sheets) == (
        expectation.formula_cell_count
    )


# =============================================================================
# R1C1 compression -- PLAN 2.1, the claim the corpus exists to prove
# =============================================================================


@pytest.mark.parametrize("expectation", _fixture_params())
def test_the_workbook_compresses_to_a_plausible_number_of_operations(
    expectation: FixtureExpectation, analyses: dict[str, WorkbookAnalysis]
) -> None:
    """Bounds, not an exact count: see the manifest docstring for why."""
    analysis = analyses[expectation.filename]
    low, high = expectation.region_bounds

    assert low <= len(analysis.operations) <= high, expectation.region_note


def test_every_sheet_that_carries_formulas_declares_what_they_compress_to() -> None:
    """A sheet with formulas and no declared bounds is a hole in the corpus, not a pass."""
    for expectation in manifest.iter_fixtures():
        for sheet in expectation.sheets:
            assert sheet.has_formulas == (sheet.operation_bounds is not None), (
                f"{expectation.filename}/{sheet.name}"
            )


@pytest.mark.parametrize(
    ("expectation", "declared"),
    _sheet_params(keep=lambda sheet: sheet.operation_bounds is not None),
)
def test_each_sheet_compresses_to_a_plausible_number_of_operations(
    expectation: FixtureExpectation,
    declared: manifest.SheetExpectation,
    analyses: dict[str, WorkbookAnalysis],
) -> None:
    sheet = analyses[expectation.filename].sheet(declared.name)

    assert sheet is not None
    assert declared.operation_bounds is not None
    low, high = declared.operation_bounds
    assert low <= len(sheet.operation_ids) <= high, expectation.region_note


@pytest.mark.parametrize("expectation", _fixture_params())
def test_every_formula_cell_belongs_to_exactly_one_operation(
    expectation: FixtureExpectation, analyses: dict[str, WorkbookAnalysis]
) -> None:
    """Compression must lose nothing. A cell in no region is a cell nobody will translate."""
    analysis = analyses[expectation.filename]

    assert sum(operation.cell_count for operation in analysis.operations) == (
        expectation.formula_cell_count
    )


@pytest.mark.parametrize("expectation", _fixture_params())
def test_the_operations_are_uniquely_identified_and_owned_by_their_sheets(
    expectation: FixtureExpectation, analyses: dict[str, WorkbookAnalysis]
) -> None:
    """Operation ids are how everything downstream refers to a region, so they must be keys."""
    analysis = analyses[expectation.filename]

    ids = [operation.id for operation in analysis.operations]
    assert len(ids) == len(set(ids))

    owned = [op_id for sheet in analysis.sheets for op_id in sheet.operation_ids]
    assert sorted(owned) == sorted(ids)
    for sheet in analysis.sheets:
        for op_id in sheet.operation_ids:
            operation = analysis.operation(op_id)
            assert operation is not None
            assert operation.sheet == sheet.name


def test_four_thousand_formula_cells_become_eight_logical_operations_at_most(
    analyses: dict[str, WorkbookAnalysis],
) -> None:
    """The headline claim of PLAN 2.1, on the fixture built to make it.

    Eight whole-column fills over 500 rows. Seven or eight operations, not 4,000 -- seven
    because Calc!B is `=Data!C2` and Calc!C is `=Data!D2`, which are one relative formula
    between them, so an adjacent-column merge is as correct an answer as a per-column one.
    """
    calc = analyses[CLEAN.filename].sheet("Calc")

    assert calc is not None
    assert calc.formula_cell_count == 4000
    assert len(calc.operation_ids) <= 8

    operations = [analyses[CLEAN.filename].operation(op_id) for op_id in calc.operation_ids]
    assert all(operation is not None for operation in operations)
    assert sum(operation.cell_count for operation in operations if operation) == 4000


@pytest.mark.parametrize(("expectation", "declared"), _operation_params())
def test_the_declared_operation_is_recovered_with_its_source_ranges(
    expectation: FixtureExpectation,
    declared: OperationExpectation,
    analyses: dict[str, WorkbookAnalysis],
) -> None:
    """Anchor, cell count and occupied ranges: facts about the workbook, pinned exactly."""
    operation = _by_anchor(analyses[expectation.filename]).get(declared.anchor)

    assert operation is not None, (
        f"no operation anchored at {declared.anchor}; found "
        f"{sorted(_by_anchor(analyses[expectation.filename]))}"
    )
    assert operation.cell_count == declared.cell_count, declared.note
    if declared.ranges:
        assert sorted(operation.ranges) == sorted(declared.ranges), declared.note
    assert operation.r1c1.startswith("="), "the compressed formula is what defines the region"
    assert operation.sample_a1.startswith("="), "and a human needs the A1 form to read it"
    assert operation.description, "every operation carries a plain-English description"


@pytest.mark.parametrize(
    ("expectation", "declared"), _operation_params(keep=lambda operation: bool(operation.reads))
)
def test_the_declared_operation_reads_what_the_formula_says_it_reads(
    expectation: FixtureExpectation,
    declared: OperationExpectation,
    analyses: dict[str, WorkbookAnalysis],
) -> None:
    """A region that has lost a reference has lost an edge, and the DAG is built from edges.

    Exact, not a subset, for the reason `forbidden_edges` exists one level up: a reference
    the analyser reports and the formula does not make is an edge waiting to be invented, and
    a subset claim cannot see one. It is affordable here because what a formula reads is
    readable off the formula -- every declared `reads` in the manifest is the complete list
    of reference tokens in the anchor's A1 form, with defined names excluded because the
    analyser counts those against the name rather than the region.
    """
    operation = _by_anchor(analyses[expectation.filename])[declared.anchor]

    raw = {reference.raw for reference in operation.references}
    assert raw == set(declared.reads), (
        f"{declared.anchor} reads {sorted(raw)}; the fixture writes {operation.sample_a1!r} "
        f"and the manifest declares {sorted(declared.reads)}"
    )


def test_the_manifests_proposed_pattern_vocabulary_maps_onto_the_analysers() -> None:
    """`PATTERN_NAMES` is proposed on the same footing as `FINDING_KINDS`. Keep it honest."""
    real = {pattern.value for pattern in ExcelPattern}

    assert set(manifest.PATTERN_NAMES) <= real, sorted(set(manifest.PATTERN_NAMES) - real)
    for expectation in manifest.iter_fixtures():
        for operation in expectation.operations:
            for name in (operation.pattern, *operation.pattern_alternatives):
                assert not name or name in manifest.PATTERN_NAMES, (
                    f"{expectation.filename}/{operation.anchor}: unknown pattern {name!r}"
                )


@pytest.mark.parametrize(
    ("expectation", "declared"), _operation_params(keep=lambda operation: bool(operation.pattern))
)
def test_the_declared_operation_is_recognised_as_the_shape_it_is(
    expectation: FixtureExpectation,
    declared: OperationExpectation,
    analyses: dict[str, WorkbookAnalysis],
) -> None:
    """PLAN 2.7's vocabulary, asserted only where the fixture exists to demonstrate a shape."""
    operation = _by_anchor(analyses[expectation.filename])[declared.anchor]

    acceptable = {declared.pattern, *declared.pattern_alternatives}
    assert operation.excel_pattern.value in acceptable, (
        f"{declared.anchor} came back {operation.excel_pattern.value} for "
        f"{operation.sample_a1!r}; the fixture allows {sorted(acceptable)}"
    )
    assert operation.excel_pattern is not ExcelPattern.UNKNOWN


@pytest.mark.parametrize(("expectation", "declared"), _operation_params())
def test_the_volatile_and_external_operations_are_flagged_as_such(
    expectation: FixtureExpectation,
    declared: OperationExpectation,
    analyses: dict[str, WorkbookAnalysis],
) -> None:
    """Volatility and external reads both make a region non-reproducible, in different ways."""
    operation = _by_anchor(analyses[expectation.filename])[declared.anchor]

    assert operation.is_volatile is declared.is_volatile, declared.note
    assert operation.has_external_refs is declared.reads_external_workbook, declared.note


@pytest.mark.parametrize("expectation", _fixture_params())
def test_a_region_can_serve_as_a_baseline_only_where_the_workbook_has_one(
    expectation: FixtureExpectation, analyses: dict[str, WorkbookAnalysis]
) -> None:
    """`cached_values_present` on an operation is a reconciliation claim: guard it."""
    analysis = analyses[expectation.filename]

    baselines = [op for op in analysis.operations if op.cached_values_present]
    if not expectation.cached_values_present:
        assert baselines == [], "a region cannot offer a baseline the workbook does not carry"
        return
    assert len(baselines) == len(analysis.operations)


# =============================================================================
# The dependency graph -- PLAN 2.1, and the reason cross_sheet_chain exists
# =============================================================================


@pytest.mark.parametrize("expectation", _fixture_params())
def test_the_graph_nodes_are_exactly_the_operations(
    expectation: FixtureExpectation, analyses: dict[str, WorkbookAnalysis]
) -> None:
    analysis = analyses[expectation.filename]

    assert sorted(analysis.graph.nodes) == sorted(op.id for op in analysis.operations)
    assert set(analysis.graph.fan_out) == set(analysis.graph.nodes)
    for upstream, downstream in analysis.graph.edges:
        assert upstream in analysis.graph.nodes
        assert downstream in analysis.graph.nodes


@pytest.mark.parametrize(("expectation", "upstream", "downstream"), _edge_params())
def test_the_dependency_edge_the_fixture_was_built_for_is_present(
    expectation: FixtureExpectation,
    upstream: str,
    downstream: str,
    analyses: dict[str, WorkbookAnalysis],
) -> None:
    analysis = analyses[expectation.filename]
    anchors = _by_anchor(analysis)

    assert upstream in anchors, f"no operation anchored at {upstream}"
    assert downstream in anchors, f"no operation anchored at {downstream}"
    edge = (anchors[upstream].id, anchors[downstream].id)
    assert edge in analysis.graph.edges, (
        f"{upstream} -> {downstream} is missing; {downstream} reads "
        f"{anchors[downstream].sample_a1!r}"
    )


@pytest.mark.parametrize(("expectation", "upstream", "downstream"), _forbidden_edge_params())
def test_the_edge_the_fixture_rules_out_is_not_reported(
    expectation: FixtureExpectation,
    upstream: str,
    downstream: str,
    analyses: dict[str, WorkbookAnalysis],
) -> None:
    """An edge that is not in the workbook must not be in the graph either.

    Every other DAG assertion here asks whether a declared edge is present, which is a claim
    the analyser can satisfy by reporting everything. This is the other half: a false upstream
    edge inverts dependency order in a generated scaffold and makes a region look like it
    depends on something it feeds, and nothing else in the corpus can see one.
    """
    analysis = analyses[expectation.filename]
    anchors = _by_anchor(analysis)

    assert upstream in anchors, f"no operation anchored at {upstream}"
    assert downstream in anchors, f"no operation anchored at {downstream}"
    edge = (anchors[upstream].id, anchors[downstream].id)
    assert edge not in analysis.graph.edges, (
        f"{upstream} -> {downstream} is reported but does not exist: {downstream} reads "
        f"{anchors[downstream].sample_a1!r}, which touches nothing in {upstream}"
    )


@pytest.mark.parametrize(
    "expectation",
    [
        pytest.param(expectation, id=expectation.filename)
        for expectation in manifest.iter_fixtures()
        if expectation.dependency_edges_are_complete
    ],
)
def test_the_declared_edges_are_all_the_edges_there_are(
    expectation: FixtureExpectation, analyses: dict[str, WorkbookAnalysis]
) -> None:
    """Where the whole DAG is derivable by hand, the analyser may not add to it.

    Five of the eight fixtures have an unambiguous region set, so their edge list is a fact
    about the formulas rather than about how the compressor grouped them. The other three
    keep the subset licence and rely on `forbidden_edges` instead -- see the manifest.
    """
    analysis = analyses[expectation.filename]
    anchors = _by_anchor(analysis)
    by_id = {operation.id: anchor for anchor, operation in anchors.items()}

    reported = {
        (by_id[upstream], by_id[downstream]) for upstream, downstream in analysis.graph.edges
    }
    declared = set(expectation.dependency_edges)
    assert reported == declared, (
        f"invented {sorted(reported - declared)}, missed {sorted(declared - reported)}"
    )


def test_every_fixture_makes_a_negative_claim_about_its_dependency_graph() -> None:
    """A fixture with no negative claim is a fixture over-reporting cannot be seen on.

    The same shape as `test_every_sheet_that_carries_formulas_declares_what_they_compress_to`:
    it guards the corpus rather than the analyser, so that deleting the last `forbidden_edges`
    entry from a fixture is a failure rather than a quiet loss of cover.
    """
    for expectation in manifest.iter_fixtures():
        assert expectation.dependency_edges_are_complete or expectation.forbidden_edges, (
            f"{expectation.filename} claims only that edges are present"
        )
        assert not (expectation.dependency_edges_are_complete and expectation.forbidden_edges), (
            f"{expectation.filename}: a complete edge set already forbids everything else"
        )
        overlap = set(expectation.dependency_edges) & set(expectation.forbidden_edges)
        assert not overlap, f"{expectation.filename} both requires and forbids {sorted(overlap)}"


@pytest.mark.parametrize("expectation", _fixture_params())
def test_a_workbook_without_a_cycle_has_a_topological_order(
    expectation: FixtureExpectation, analyses: dict[str, WorkbookAnalysis]
) -> None:
    """A scaffold is written in dependency order, so the order has to exist."""
    analysis = analyses[expectation.filename]

    if expectation.cycles:
        assert analysis.graph.cycles, "a workbook with a cycle must say so"
        assert analysis.graph.topological_order is None
        return

    assert analysis.graph.cycles == []
    assert analysis.graph.topological_order is not None
    position = {node: index for index, node in enumerate(analysis.graph.topological_order)}
    assert set(position) == set(analysis.graph.nodes)
    for upstream, downstream in analysis.graph.edges:
        assert position[upstream] < position[downstream]


@pytest.mark.parametrize("expectation", _fixture_params())
def test_fan_out_counts_the_edges_leaving_each_operation(
    expectation: FixtureExpectation, analyses: dict[str, WorkbookAnalysis]
) -> None:
    """Fan-out is what tells the planner which region is load-bearing."""
    analysis = analyses[expectation.filename]

    counted: dict[str, int] = dict.fromkeys(analysis.graph.nodes, 0)
    for upstream, _ in analysis.graph.edges:
        counted[upstream] += 1
    assert analysis.graph.fan_out == counted
    for operation in analysis.operations:
        assert operation.downstream_ref_count == counted[operation.id]


def test_the_chain_fixture_is_four_deep_and_not_a_clean_layer_cake(
    analyses: dict[str, WorkbookAnalysis],
) -> None:
    """A_Source to B_Enrich to C_Aggregate to D_Report, with D_Report!B9 skipping a layer."""
    analysis = analyses["cross_sheet_chain.xlsx"]
    anchors = _by_anchor(analysis)
    order = analysis.graph.topological_order

    assert order is not None
    position = {node: index for index, node in enumerate(order)}
    chain = ["B_Enrich!C2", "C_Aggregate!C2", "D_Report!B2", "D_Report!B4"]
    depths = [position[anchors[step].id] for step in chain]
    assert depths == sorted(depths), "the four-deep chain must come out in order"

    probe = anchors["D_Report!B9"]
    assert (anchors["B_Enrich!E3"].id, probe.id) in analysis.graph.edges
    assert probe.id not in {
        downstream
        for upstream, downstream in analysis.graph.edges
        if upstream.startswith("c_aggregate")
    }, "B9 reaches back into B_Enrich directly, which is the point of it"


def test_the_running_total_compresses_to_two_operations_not_a_hundred_and_twenty(
    analyses: dict[str, WorkbookAnalysis],
) -> None:
    """B_Enrich!E seeds at =C2 then runs =E(n-1)+C(n): a cum_sum and a shift in one column."""
    analysis = analyses["cross_sheet_chain.xlsx"]
    anchors = _by_anchor(analysis)

    seed, run = anchors["B_Enrich!E2"], anchors["B_Enrich!E3"]
    assert seed.cell_count + run.cell_count == 120
    assert (seed.id, run.id) in analysis.graph.edges
    assert run.translation_hint is not None
    # `shift` and `cum_sum` are polars expressions, not prose: they are the two idioms the
    # two defensible readings of this column map to (`prior_row` and `running_total`, the
    # pattern and its alternative in the manifest), and the hint's whole job is to name the
    # one it landed on. A hint that says "running total" and nothing else leaves the
    # generator to guess at the API, which is what the hint exists to stop.
    assert "shift" in run.translation_hint or "cum_sum" in run.translation_hint


# =============================================================================
# Findings
# =============================================================================


def test_the_manifests_proposed_finding_vocabulary_maps_onto_the_analysers() -> None:
    """Every proposed name is either mapped or declared unmapped. No third option."""
    declared = set(manifest.FINDING_KINDS)

    assert set(FINDING_KIND_BY_PROPOSED_NAME) | UNMAPPED_FINDING_KINDS == declared
    assert not set(FINDING_KIND_BY_PROPOSED_NAME) & UNMAPPED_FINDING_KINDS
    assert len(set(FINDING_KIND_BY_PROPOSED_NAME.values())) == len(FINDING_KIND_BY_PROPOSED_NAME)

    used = {
        finding.kind for expectation in manifest.iter_fixtures() for finding in expectation.findings
    }
    assert used <= declared


def test_every_finding_kind_the_analyser_can_raise_is_accounted_for() -> None:
    """The reverse mapping, which is the direction that rots quietly.

    A `FindingKind` no fixture reaches is a detector this corpus says nothing about, and the
    test above cannot see one: it reads manifest to model only, so a member added to the enum
    tomorrow would leave every assertion in this file passing and untouched. Every member has
    to account for itself instead — mapped and declared by some fixture, or listed with why
    not. Which is also how `dead_region` was found: it fires forty times across the corpus
    and nothing here mentioned it.
    """
    every = set(FindingKind)
    mapped = set(FINDING_KIND_BY_PROPOSED_NAME.values())
    excused = set(FINDING_KINDS_NOT_IN_THE_CORPUS)

    assert mapped | excused == every, sorted(every - mapped - excused)
    assert not mapped & excused, sorted(mapped & excused)
    assert all(reason.strip() for reason in FINDING_KINDS_NOT_IN_THE_CORPUS.values())

    reachable = {
        FINDING_KIND_BY_PROPOSED_NAME[kind]
        for expectation in manifest.iter_fixtures()
        for kind in expectation.finding_kinds
        if kind not in UNMAPPED_FINDING_KINDS
    }
    assert reachable == mapped, (
        f"mapped but declared by no fixture: {sorted(kind.value for kind in mapped - reachable)}"
    )


def test_every_fixture_declares_the_complete_set_of_findings_it_produces() -> None:
    """A fixture with no negative claim about its findings is one over-reporting hides on.

    The findings counterpart of
    `test_every_fixture_makes_a_negative_claim_about_its_dependency_graph`, and it guards the
    corpus rather than the analyser. All eight sets are derivable from what `generate.py`
    writes, so all eight are complete; a fixture that genuinely could not state its full set
    would turn `finding_kinds_are_complete` off, and this is where somebody would have to say
    which one and why. Turning it off to quieten a failing negative claim fails here instead.
    """
    for expectation in manifest.iter_fixtures():
        declared = set(expectation.finding_kinds)
        assert expectation.finding_kinds, f"{expectation.filename} declares no finding kinds"
        assert expectation.finding_kinds_are_complete, (
            f"{expectation.filename} claims only that findings are present"
        )
        assert list(expectation.finding_kinds) == sorted(expectation.finding_kinds), (
            f"{expectation.filename}: keep finding_kinds sorted so the diff is readable"
        )
        assert len(declared) == len(expectation.finding_kinds)
        assert declared <= set(manifest.FINDING_KINDS), (
            f"{expectation.filename} names {sorted(declared - set(manifest.FINDING_KINDS))}, "
            f"which is in no vocabulary"
        )
        trapped = {finding.kind for finding in expectation.findings}
        assert trapped <= declared, (
            f"{expectation.filename} sets a trap for {sorted(trapped - declared)} but does not "
            f"list it among the kinds it produces"
        )


@pytest.mark.parametrize(("expectation", "kind"), _declared_kind_params())
def test_the_finding_kind_the_fixture_produces_is_raised(
    expectation: FixtureExpectation, kind: str, analyses: dict[str, WorkbookAnalysis]
) -> None:
    """The positive half of the set claim, for kinds no `FindingExpectation` covers.

    Most declared kinds are also declared as traps with a location and a minimum count, and
    those are asserted harder below. This catches the rest -- the dead region, the absent
    value cache, the unreadable Word companion -- which are as much a part of what each
    workbook produces and were previously claimed nowhere.
    """
    raised = analyses[expectation.filename].findings_of(FINDING_KIND_BY_PROPOSED_NAME[kind])

    assert raised, (
        f"{expectation.filename} produces no {kind} finding; it raised "
        f"{sorted({finding.kind.value for finding in analyses[expectation.filename].findings})}"
    )


@pytest.mark.parametrize(("expectation", "kind"), _undeclared_kind_params())
def test_the_finding_kind_the_fixture_does_not_produce_is_not_raised(
    expectation: FixtureExpectation, kind: str, analyses: dict[str, WorkbookAnalysis]
) -> None:
    """The half the corpus was missing, and the one the findings list most needs.

    Every other findings assertion here asks whether something was found, which an analyser
    that reported everything would satisfy. This asks the opposite, kind by kind, over the
    whole mapped vocabulary. It is a claim about kinds and not about instances: a fixture
    that legitimately produces a kind cannot use this to rule out a wrong instance of it, so
    a false `dead_region` on a workbook that has a real one still needs its own test.
    """
    analysis = analyses[expectation.filename]
    raised = analysis.findings_of(FINDING_KIND_BY_PROPOSED_NAME[kind])

    assert raised == [], (
        f"{expectation.filename} does not produce {kind}, but the analyser raised "
        f"{len(raised)}: {[(finding.sheet, finding.location) for finding in raised]}"
    )


@pytest.mark.parametrize(("expectation", "declared"), _finding_params())
def test_the_finding_the_fixture_sets_a_trap_for_is_raised(
    expectation: FixtureExpectation,
    declared: manifest.FindingExpectation,
    analyses: dict[str, WorkbookAnalysis],
) -> None:
    """Occurrences, counted as findings or as distinct declared locations, whichever is more.

    One `MERGED_CELLS` finding can legitimately cover two merged ranges, so counting Finding
    objects alone would under-count a detector that summarises per sheet.
    """
    analysis = analyses[expectation.filename]
    kind = FINDING_KIND_BY_PROPOSED_NAME[declared.kind]
    raised = analysis.findings_of(kind)
    matched = {
        location
        for location in declared.locations
        if any(_points_at(finding, location) for finding in raised)
    }

    assert max(len(raised), len(matched)) >= declared.minimum, (
        f"{expectation.filename} expected at least {declared.minimum} {declared.kind}; "
        f"got {len(raised)} finding(s) covering {sorted(matched)}. {declared.note}"
    )


@pytest.mark.parametrize(("expectation", "kind", "location"), _location_params())
def test_the_finding_points_at_the_place_the_fixture_set_the_trap(
    expectation: FixtureExpectation,
    kind: str,
    location: str,
    analyses: dict[str, WorkbookAnalysis],
) -> None:
    """A finding that cannot say where is a finding the user cannot act on."""
    raised = analyses[expectation.filename].findings_of(FINDING_KIND_BY_PROPOSED_NAME[kind])

    assert any(_points_at(finding, location) for finding in raised), (
        f"no {kind} finding points at {location}; they point at "
        f"{[(f.sheet, f.location) for f in raised]}"
    )


@pytest.mark.parametrize("expectation", _fixture_params())
def test_every_finding_carries_a_message_a_real_sheet_and_a_way_out(
    expectation: FixtureExpectation, analyses: dict[str, WorkbookAnalysis]
) -> None:
    """CONVENTIONS: what the user can do about it is part of the report, not an extra.

    "What was attempted, what was found, what to do" is prose and not mechanically checkable;
    the third clause is, and it is the one a user acts on. Required at WARNING and ERROR,
    where something has to happen -- an INFO finding may reasonably be an observation.
    """
    for finding in analyses[expectation.filename].findings:
        assert finding.message.strip(), f"{finding.kind} has no message"
        assert finding.message == finding.message.strip()
        assert finding.sheet is None or finding.sheet in expectation.sheet_names
        if finding.severity is not Severity.INFO:
            assert finding.remediation and finding.remediation.strip(), (
                f"{finding.kind} at {finding.severity} says what is wrong but not what to do"
            )


def test_the_typed_over_cell_breaks_its_region_and_becomes_a_finding(
    hostile: WorkbookAnalysis,
) -> None:
    """The classic "someone typed over row 47" bug, and the single most valuable find."""
    fill = _by_anchor(hostile)["Messy!D5"]

    broken = {item.cell for item in fill.inconsistencies}
    assert "D47" in broken, "D5:D80 is =B*C throughout except D47, which is =B47*C47*1.1"
    inconsistency = next(item for item in fill.inconsistencies if item.cell == "D47")
    assert inconsistency.expected_r1c1 == fill.r1c1
    assert inconsistency.actual_r1c1 is not None
    assert inconsistency.actual_r1c1 != fill.r1c1
    assert "1.1" in inconsistency.actual_r1c1

    assert any(
        finding.location == "D47"
        for finding in hostile.findings_of(FindingKind.INCONSISTENT_FORMULA)
    )


def test_the_repeated_header_label_is_named_and_not_merely_counted(
    hostile: WorkbookAnalysis,
) -> None:
    """Messy!B4 and Messy!D4 are both "Amount", two columns apart with "Rate" between them.

    The finding reports "row 4", which is the honest granularity -- it is a fact about the
    header row -- but a row number alone leaves the reader to hunt for the collision. The
    label has to be in the message, or the two declared locations are indistinguishable.
    """
    findings = hostile.findings_of(FindingKind.DUPLICATE_HEADER)

    assert len(findings) == 1, "one header row, one finding about it"
    assert findings[0].sheet == "Messy"
    assert "amount" in findings[0].message.lower(), (
        f"the repeated label is not named: {findings[0].message!r}"
    )


def test_the_neighbours_of_the_typed_over_cell_are_left_alone(hostile: WorkbookAnalysis) -> None:
    """D46 and D48 are untouched, so the region is otherwise uniform and D47 is the anomaly."""
    fill = _by_anchor(hostile)["Messy!D5"]

    broken = {item.cell for item in fill.inconsistencies}
    assert {"D46", "D48"} & broken == set()
    assert "Messy!D46" in fill.ranges
    assert "Messy!D48:D59" in fill.ranges


_SUBTOTAL_REPORTED_AS_A_TYPED_OVER_CELL = (
    "regions puts Messy!D30 in the D5 fill's `inconsistencies` even though the same analysis "
    "has already assigned D30 to the subtotal operation anchored at B30 -- Messy!D30 is in "
    "that operation's `ranges` and its cell is counted against it -- and has drawn the edge "
    "Messy!D5 -> Messy!B30 to say the subtotal reads the fill. findings._inconsistency_"
    "findings then reports every entry in that list, so the deliberate subtotal comes back as "
    "'Messy!D30 holds a different formula from the region filled from D5': word for word the "
    "message the genuine typed-over cell at D47 gets. The two claims cannot both be "
    "actionable, and INCONSISTENT_FORMULA is the finding the whole list is sold on (PLAN M1), "
    "so an invented one is the most expensive false positive the analyser can produce."
)


@pytest.mark.xfail(reason=_SUBTOTAL_REPORTED_AS_A_TYPED_OVER_CELL, strict=True)
def test_the_subtotal_in_the_middle_of_the_fill_is_not_reported_as_a_typed_over_cell(
    hostile: WorkbookAnalysis,
) -> None:
    """Messy!D30 is =SUM(D5:D29). It is a totals row, and the corpus says so twice already.

    `totals_row_in_data` reports it, and the DAG has the fill flowing into it. Reporting it a
    third time as a broken cell inside the fill contradicts both.
    """
    fill = _by_anchor(hostile)["Messy!D5"]
    subtotal = _by_anchor(hostile)["Messy!B30"]

    assert "Messy!D30" in subtotal.ranges, "D30 belongs to the subtotal operation"
    assert "D30" not in {item.cell for item in fill.inconsistencies}
    assert not any(
        finding.location == "D30"
        for finding in hostile.findings_of(FindingKind.INCONSISTENT_FORMULA)
    )


# =============================================================================
# External connections, and the SQL they carry
# =============================================================================


@pytest.mark.parametrize("expectation", _fixture_params())
def test_a_workbook_reports_the_connections_it_has_and_no_others(
    expectation: FixtureExpectation, analyses: dict[str, WorkbookAnalysis]
) -> None:
    """openpyxl does not parse `xl/connections.xml` at all: absent is a result, not a failure."""
    analysis = analyses[expectation.filename]

    assert len(analysis.connections) == expectation.connection_count
    expected_status = (
        ExtractionStatus.OK if expectation.connection_count else (ExtractionStatus.ABSENT)
    )
    assert analysis.connections_status is expected_status


@pytest.mark.parametrize(
    ("filename", "declared"),
    [
        pytest.param(
            expectation.filename, connection, id=f"{expectation.filename}-{connection.name}"
        )
        for expectation in manifest.iter_fixtures()
        for connection in expectation.connections
    ],
)
def test_the_legacy_connection_keeps_its_statement_line_for_line(
    filename: str, declared: ConnectionExpectation, analyses: dict[str, WorkbookAnalysis]
) -> None:
    """The trap is newline handling: `&#10;` in, real newlines out, never spaces."""
    found = {connection.name: connection for connection in analyses[filename].connections}

    assert declared.name in found
    connection = found[declared.name]
    assert connection.kind == declared.kind
    assert connection.provider == declared.provider
    assert connection.description == declared.description

    assert connection.command is not None
    assert connection.command.startswith(declared.command_starts_with)
    assert connection.command.count("\n") == declared.command_newlines, declared.note
    for fragment in declared.command_contains:
        assert fragment in connection.command


@pytest.mark.parametrize(
    ("filename", "declared"),
    [
        pytest.param(
            expectation.filename, connection, id=f"{expectation.filename}-{connection.name}"
        )
        for expectation in manifest.iter_fixtures()
        for connection in expectation.connections
    ],
)
def test_the_connection_string_is_redacted_before_it_reaches_the_analysis(
    filename: str, declared: ConnectionExpectation, analyses: dict[str, WorkbookAnalysis]
) -> None:
    """Connection strings routinely carry credentials; the raw string is never retained."""
    connection = next(c for c in analyses[filename].connections if c.name == declared.name)
    rendered = json.dumps(analyses[filename].model_dump(mode="json"))

    assert connection.connection_string_redacted is not None
    for secret in declared.secrets:
        assert secret not in connection.connection_string_redacted
        assert secret not in rendered, "and it must not survive anywhere else in the artifact"


# =============================================================================
# Power Query
# =============================================================================


@pytest.mark.parametrize("expectation", _fixture_params())
def test_the_data_mashup_is_found_behind_the_decoys_or_reported_absent(
    expectation: FixtureExpectation, analyses: dict[str, WorkbookAnalysis]
) -> None:
    """PLAN 1.5 warns against hardcoding `customXml/item1.xml`, so the fixture puts it third."""
    extraction = analyses[expectation.filename].power_query

    if not expectation.power_query_names:
        assert extraction.status is ExtractionStatus.ABSENT
        assert extraction.queries == []
        return

    assert extraction.status is ExtractionStatus.OK
    assert extraction.part_path == expectation.power_query_part
    assert [query.name for query in extraction.queries] == list(expectation.power_query_names)
    assert extraction.section_m_bytes


@pytest.mark.parametrize(
    ("filename", "query", "reads"),
    [
        pytest.param(expectation.filename, query, reads, id=f"{expectation.filename}-{query}")
        for expectation in manifest.iter_fixtures()
        for query, reads in expectation.power_query_references
    ],
)
def test_a_query_that_reads_another_query_says_so(
    filename: str, query: str, reads: tuple[str, ...], analyses: dict[str, WorkbookAnalysis]
) -> None:
    """Query-level dependencies are the M-language half of the dependency graph."""
    found = {q.name: q for q in analyses[filename].power_query.queries}

    assert query in found
    assert sorted(found[query].referenced_queries) == sorted(reads)
    assert found[query].m_source.strip()
    for name in reads:
        assert found[name].referenced_queries == [], f"{name} is a source, it reads nothing"


# =============================================================================
# Named ranges
# =============================================================================


@pytest.mark.parametrize("expectation", _fixture_params())
def test_the_defined_names_are_recovered_and_the_broken_one_is_marked(
    expectation: FixtureExpectation, analyses: dict[str, WorkbookAnalysis]
) -> None:
    analysis = analyses[expectation.filename]

    assert {named.name for named in analysis.named_ranges} == set(expectation.named_ranges)
    broken = {named.name for named in analysis.named_ranges if named.is_broken}
    assert broken == set(expectation.broken_named_ranges)


def test_a_named_range_a_formula_uses_is_counted_against_it(
    analyses: dict[str, WorkbookAnalysis],
) -> None:
    """`capital_ratio` is read by every one of Calc!H's 500 cells, and by nothing else."""
    named = {n.name: n for n in analyses[CLEAN.filename].named_ranges}

    assert named["capital_ratio"].refers_to == "Params!$B$2"
    assert named["capital_ratio"].reference_count == 500
    assert named["fx_rate_usd"].reference_count == 0, (
        "declared but never used, which is worth knowing"
    )


def test_the_defined_name_over_a_deleted_range_does_not_resolve(
    hostile: WorkbookAnalysis,
) -> None:
    named = {n.name: n for n in hostile.named_ranges}

    assert named["obsolete_rate_table"].is_broken is True
    assert "#REF!" in named["obsolete_rate_table"].refers_to
    assert named["messy_amounts"].is_broken is False
    assert named["messy_amounts"].refers_to == "Messy!$B$5:$B$80"


# =============================================================================
# Process notes -- PLAN 1.5's "the intent lives in the prose"
# =============================================================================


#: `docs.sidecar_documents` matches documents by conventional filename, so
#: `documented_procedure.docx` and `procedure_legacy.doc` attach to every workbook in
#: tests/fixtures. Documented behaviour rather than a defect, but it has to be subtracted
#: before a fixture's own note sources can be claimed exactly.
_COMPANION_NOTE_SOURCES = frozenset({"docx", "doc_stub"})

#: The same rule reaches `tests/fixtures/README.md`, because `readme` is in the filename
#: vocabulary and a README is the one markdown file almost every directory has. It is
#: subtracted for the same reason as the Word companions, but deliberately not *required*:
#: that README is repository housekeeping, and a corpus assertion that depends on it would
#: fail the day somebody renames it. `document_attached_by_filename` is where that guess is
#: claimed instead, fixture by fixture, in the manifest.
_SIDECAR_NOTE_SOURCES = _COMPANION_NOTE_SOURCES | frozenset({"markdown", "plain_text"})


@pytest.mark.parametrize("expectation", _fixture_params())
def test_the_note_sources_are_exactly_the_ones_the_workbook_itself_carries(
    expectation: FixtureExpectation, analyses: dict[str, WorkbookAnalysis]
) -> None:
    """Exact, not subset: seven of these eight workbooks contain no prose of their own.

    A subset assertion would pass vacuously on those seven, so the sidecar sources are
    subtracted and the remainder pinned. `documented.xlsx` must have both a sheet and cell
    comments; nothing else may invent either.
    """
    sources = {note.source for note in analyses[expectation.filename].notes}

    assert sources - _SIDECAR_NOTE_SOURCES == set(expectation.note_sources)
    assert sources >= _COMPANION_NOTE_SOURCES, "the two Word companions attach to every fixture"


def test_the_documentation_sheet_prose_is_recovered_whole(
    analyses: dict[str, WorkbookAnalysis],
) -> None:
    """Seven prose blocks under bold headings. However they are grouped, none may be lost."""
    notes = [note for note in analyses[DOCUMENTED.filename].notes if note.source == "sheet"]

    assert notes, "the Process Notes sheet is the whole point of this fixture"
    assert all(note.origin == "Process Notes" for note in notes)
    recovered = "\n".join(note.text for note in notes)
    for heading in DOCUMENTED.documentation_headings:
        assert heading in recovered, f"the {heading!r} block did not survive extraction"


def test_the_cell_comments_carrying_intent_are_recovered(
    analyses: dict[str, WorkbookAnalysis],
) -> None:
    """Three comments say things that appear in no cell value anywhere in the workbook."""
    notes = [note for note in analyses[DOCUMENTED.filename].notes if note.source == "cell_comment"]

    found = {f"{note.origin}!{note.location}" for note in notes}
    assert found == set(DOCUMENTED.commented_cells)
    assert all(note.text.strip() for note in notes)


def test_the_comment_forbidding_iferror_reaches_the_analysis(
    analyses: dict[str, WorkbookAnalysis],
) -> None:
    """The kind of intent that must reach the plan rather than be optimised away."""
    analysis = analyses[DOCUMENTED.filename]

    comment = next(
        note
        for note in analysis.notes
        if note.source == "cell_comment" and note.location == "C1" and note.origin == "Calc"
    )
    assert "IFERROR" in comment.text
    assert "#N/A" in comment.text

    prose = "\n".join(note.text for note in analysis.notes if note.source == "sheet")
    assert "IFERROR" in prose, "the Known issues block says the same thing at greater length"


def test_the_legacy_doc_stub_fails_clearly_with_a_conversion_hint(
    analyses: dict[str, WorkbookAnalysis],
) -> None:
    """`procedure_legacy.doc` is deliberately not an OLE2 file. A clear refusal, not a stack."""
    analysis = analyses[DOCUMENTED.filename]

    unsupported = analysis.findings_of(FindingKind.UNSUPPORTED_FORMAT)
    assert unsupported, "an unreadable sidecar must be reported, not skipped in silence"
    assert any("procedure_legacy.doc" in (f.location or "") for f in unsupported)
    assert any(".docx" in (f.remediation or "") for f in unsupported)
    assert any(note.source == "doc_stub" for note in analysis.notes)


# =============================================================================
# Cached values -- the precondition for CONVENTIONS non-negotiable 6
# =============================================================================


@pytest.mark.parametrize("expectation", _fixture_params())
def test_only_the_declared_fixture_offers_a_reconciliation_baseline(
    expectation: FixtureExpectation, analyses: dict[str, WorkbookAnalysis]
) -> None:
    """The whole of non-negotiable 6 rests on this being right for every workbook."""
    coverage = analyses[expectation.filename].cached_values

    assert coverage.formula_cell_count == expectation.formula_cell_count
    assert coverage.coverage == pytest.approx(expectation.cached_value_coverage)
    assert coverage.reconcilable is expectation.cached_values_present
    assert analyses[expectation.filename].is_reconcilable is expectation.cached_values_present
    assert coverage.status == ("present" if expectation.cached_values_present else "absent")


def test_the_clean_pipeline_carries_a_complete_baseline(
    analyses: dict[str, WorkbookAnalysis],
) -> None:
    """4,006 formula cells, every one cached, independently verified against real Excel."""
    coverage = analyses[CLEAN.filename].cached_values

    assert coverage.cached_present_count == coverage.formula_cell_count == 4006
    assert coverage.coverage == 1.0
    assert coverage.status == "present"
    assert analyses[CLEAN.filename].findings_of(FindingKind.MISSING_CACHED_VALUES) == []
    assert analyses[CLEAN.filename].findings_of(FindingKind.PARTIAL_CACHED_VALUES) == []


def test_the_workbook_with_no_cache_says_so_loudly_at_analysis_time(
    analyses: dict[str, WorkbookAnalysis],
) -> None:
    """Non-negotiable 6 starts here: the analyser must see the missing baseline first.

    `test_reconcile_corpus.py` covers what reconciliation then does with it. This is the half
    that happens before any reconciliation is attempted, and if it is wrong the other half
    never gets the chance to be right.
    """
    analysis = analyses[NO_CACHE.filename]
    coverage = analysis.cached_values

    assert coverage.status == "absent"
    assert coverage.cached_present_count == 0
    assert coverage.coverage == 0.0
    assert coverage.reconcilable is False
    assert analysis.is_reconcilable is False
    assert analysis.summary()["reconcilable"] is False

    findings = analysis.findings_of(FindingKind.MISSING_CACHED_VALUES)
    assert len(findings) == 1, "once, at the workbook level, not once per sheet"
    finding = findings[0]
    assert finding in analysis.errors, "an absent baseline is an ERROR, not a note in passing"
    assert finding.affected_cells == 1004
    # These four pin wording, deliberately and by exception. Everywhere else in this file
    # wording is the analyser's business, but non-negotiable 6 is a promise about what the
    # user is told: "cannot be reconciled", and a remediation that says the answer degrades
    # to "not reconciled" and never to "passed". Those phrases are the invariant. A finding
    # that carried the same facts while saying something softer would satisfy every
    # structural assertion above and break the promise.
    assert "1,004" in finding.message
    assert "cannot be reconciled" in finding.message
    assert finding.remediation is not None
    assert "not reconciled" in finding.remediation
    assert "passed" in finding.remediation


def test_no_fixture_reports_a_partial_cache_it_does_not_have(
    analyses: dict[str, WorkbookAnalysis],
) -> None:
    """ "Partial" is the dangerous middle: it must be claimed only where it is true, and it
    is true nowhere in this corpus. Every fixture is all cached or none."""
    for filename, analysis in analyses.items():
        coverage = analysis.cached_values
        assert coverage.status != "partial", filename
        assert coverage.coverage in (0.0, 1.0), filename
        assert analysis.findings_of(FindingKind.PARTIAL_CACHED_VALUES) == [], filename


# =============================================================================
# hostile.xlsx -- CONVENTIONS non-negotiable 4, by name
# =============================================================================


def test_the_hostile_workbook_produces_a_complete_analysis_and_never_raises(
    hostile: WorkbookAnalysis,
) -> None:
    """CONVENTIONS non-negotiable 4, stated as one assertion per clause.

    "Every analyser extractor degrades gracefully. Absent and unparseable are first-class
    results. A malformed workbook produces a Finding, never a traceback. analyse() always
    returns a complete WorkbookAnalysis."
    """
    assert isinstance(hostile, WorkbookAnalysis)

    # Complete: every section of the contract is populated, not merely present.
    assert len(hostile.sheets) == len(HOSTILE.sheets)
    assert hostile.operations, "a workbook with 94 formula cells has operations"
    assert hostile.graph.nodes
    assert hostile.profiles, "the profiler ran over the sheets it could read"
    assert hostile.named_ranges
    assert hostile.workbook.sha256
    assert hostile.analysis_duration_seconds is not None

    # Findings populated, and severe enough to stop somebody converting this by accident.
    # Twenty-five is the fixture's own traps counted once each at the granularity the
    # detectors work at, and not a figure read off a run: seven volatile regions on
    # Volatile!B2:B8, two IFERROR regions at Messy!K6 and K7, two external-link regions at
    # Broken!A2 and A3, the 1.1 in the typed-over cell, that typed-over cell itself, then one
    # apiece for the hidden sheet, the hidden columns, the merged ranges, the duplicate
    # header, the mid-sheet subtotal, the text-formatted column and the #REF! cell, and one
    # apiece for the absent value cache, the broken defined name, the dead external link, the
    # cycle and the .doc companion. Fifteen dead regions on top of that are not counted here,
    # so the floor holds even if the dead-region detector is retuned.
    assert len(hostile.findings) >= 25
    assert hostile.errors, "a #REF!, a cycle and a dead external link are not INFO"

    # Absent is a result: this workbook has neither connections nor a mashup, and says so.
    assert hostile.connections_status is ExtractionStatus.ABSENT
    assert hostile.power_query.status is ExtractionStatus.ABSENT

    # And nothing anywhere reported itself as having fallen over.
    unparseable = hostile.findings_of(FindingKind.UNPARSEABLE_PART)
    assert unparseable == [], f"an extractor gave up: {[f.message for f in unparseable]}"


def test_the_hostile_analysis_is_deterministic() -> None:
    """Two runs over the same bytes must agree, or nothing downstream can be reviewed."""
    first, second = analyse(HOSTILE.path), analyse(HOSTILE.path)

    assert [op.id for op in first.operations] == [op.id for op in second.operations]
    assert [(f.kind, f.sheet, f.location) for f in first.findings] == [
        (f.kind, f.sheet, f.location) for f in second.findings
    ]
    assert first.graph.edges == second.graph.edges
    assert first.graph.cycles == second.graph.cycles


def _drive_scan(path: Path) -> object:
    """values.scan_sheet over every sheet, on its own."""
    with open_workbook(path) as handle:
        scans = {name: scan_sheet(handle, name) for name in handle.sheet_names}
    assert sum(scan.formula_count for scan in scans.values()) == HOSTILE.formula_cell_count
    return scans


def _drive_structure(path: Path) -> object:
    """workbook.WorkbookHandle.structure over every sheet, on its own."""
    with open_workbook(path) as handle:
        structures = {name: handle.structure(name) for name in handle.sheet_names}
    assert structures["Messy"].hidden_columns == ("H", "I")
    assert structures["_Archive"].is_hidden is True
    return structures


def _drive_regions(path: Path) -> object:
    """regions.detect_regions over every sheet, on its own."""
    with open_workbook(path) as handle:
        used: set[str] = set()
        regions = [
            region
            for name in handle.sheet_names
            for region in detect_regions(
                scan_sheet(handle, name),
                names=handle.defined_name_keys,
                known_sheets=frozenset(handle.sheet_names),
                used_ids=used,
            )
        ]
    assert sum(r.operation.cell_count for r in regions) == HOSTILE.formula_cell_count
    return regions


def _drive_classify(path: Path) -> object:
    """classify.classify_sheets, on its own."""
    with open_workbook(path) as handle:
        scans = {name: scan_sheet(handle, name) for name in handle.sheet_names}
        structures = {name: handle.structure(name) for name in handle.sheet_names}
        classifications = classify_sheets(scans, structures, [], handle.defined_names())
    assert set(classifications) == set(HOSTILE.sheet_names)
    assert all(c.role is not SheetRole.UNKNOWN for c in classifications.values())
    return classifications


def _drive_graph(path: Path) -> object:
    """graph.build_graph, on its own. INDIRECT and a dead external link both live here."""
    with open_workbook(path) as handle:
        scans = {name: scan_sheet(handle, name) for name in handle.sheet_names}
        used: set[str] = set()
        regions = [
            region
            for name in handle.sheet_names
            for region in detect_regions(
                scans[name],
                names=handle.defined_name_keys,
                known_sheets=frozenset(handle.sheet_names),
                used_ids=used,
            )
        ]
        graph = build_graph(
            regions,
            scans,
            names=handle.defined_name_keys,
            known_sheets=frozenset(handle.sheet_names),
        )
    assert graph.cycles, "the three-cell cycle must survive being looked for on its own"
    return graph


def _drive_findings(path: Path) -> object:
    """findings.collect_findings, on its own."""
    identity = read_identity(path)
    with open_workbook(path) as handle:
        scans = {name: scan_sheet(handle, name) for name in handle.sheet_names}
        structures = {name: handle.structure(name) for name in handle.sheet_names}
        used: set[str] = set()
        regions = [
            region
            for name in handle.sheet_names
            for region in detect_regions(
                scans[name],
                names=handle.defined_name_keys,
                known_sheets=frozenset(handle.sheet_names),
                used_ids=used,
            )
        ]
        named = handle.defined_names()
        findings = collect_findings(
            identity=identity,
            handle=handle,
            scans=scans,
            structures=structures,
            regions=regions,
            graph=build_graph(regions, scans, known_sheets=frozenset(handle.sheet_names)),
            classifications=classify_sheets(scans, structures, regions, named),
            named_ranges=named,
            coverage=cached_value_coverage(scans.values()),
        )
    assert findings
    return findings


def _drive_connections(path: Path) -> object:
    """connections.extract_connections, on its own. There is no connections part here."""
    with open_workbook(path) as handle:
        connections, status, findings = extract_connections(handle.zf)
    assert connections == []
    assert status is ExtractionStatus.ABSENT
    assert findings == []
    return connections


def _drive_power_query(path: Path) -> object:
    """powerquery.extract_power_query, on its own. There is no mashup here either."""
    with open_workbook(path) as handle:
        extraction, findings = extract_power_query(handle.zf)
    assert extraction.status is ExtractionStatus.ABSENT
    assert findings == []
    return extraction


def _drive_docs(path: Path) -> object:
    """docs.extract_notes, on its own.

    hostile.xlsx carries no prose of its own, but it does not have to: the two Word
    companions in tests/fixtures attach to every workbook in the directory, so the extractor
    has something to find here and an empty list is a failure rather than a pass.

    Neither companion shares hostile.xlsx's stem, so both are attached on their filenames and
    both say so. That is the second kind here, and the reason this is not a single-kind claim.
    """
    with open_workbook(path) as handle:
        notes, findings = extract_notes(handle)
    assert any(note.source == "docx" for note in notes), "documented_procedure.docx read as none"
    assert any(note.source == "doc_stub" for note in notes), (
        "the .doc must fail loudly, not quietly"
    )
    assert findings, "and the unreadable companion must be reported"
    assert any(finding.kind is FindingKind.UNSUPPORTED_FORMAT for finding in findings)
    assert all(
        finding.kind in (FindingKind.UNSUPPORTED_FORMAT, FindingKind.DOCUMENT_ATTACHED_BY_FILENAME)
        for finding in findings
    )
    return notes


def _drive_external_links(path: Path) -> object:
    """findings.external_link_targets, on its own: the link that cannot resolve."""
    with open_workbook(path) as handle:
        targets = external_link_targets(handle)
    assert [target for _, target, _ in targets] == list(HOSTILE.external_links)
    assert all(resolves is False for _, _, resolves in targets)
    return targets


def _drive_profiles(path: Path) -> object:
    """profile.profile_sheet over every sheet, on its own.

    Messy is profiled column by column and the count is pinned, because "returned a list" is
    satisfied by returning nothing. Its header row names nine columns with F deliberately
    blank, so eight of A to I carry data; K and L hold the IFERROR block and its zero
    divisor; J is never touched. Ten, and the two holes are as much the claim as the eight.
    """
    analysis = analyse(path)
    with open_workbook(path) as handle:
        profiles = {sheet.name: profile_sheet(handle, sheet) for sheet in analysis.sheets}
    assert [column.column for column in profiles["Messy"]] == list("ABCDEGHIKL"), (
        "the sheet with every trap on it still profiles, column for column"
    )
    assert [column.column for column in profiles["_Archive"]] == ["A", "B"], (
        "and a hidden sheet is profiled like any other"
    )
    return profiles


@pytest.mark.parametrize(
    "drive",
    [
        pytest.param(_drive_scan, id="values.scan_sheet"),
        pytest.param(_drive_structure, id="workbook.structure"),
        pytest.param(_drive_regions, id="regions.detect_regions"),
        pytest.param(_drive_classify, id="classify.classify_sheets"),
        pytest.param(_drive_graph, id="graph.build_graph"),
        pytest.param(_drive_findings, id="findings.collect_findings"),
        pytest.param(_drive_connections, id="connections.extract_connections"),
        pytest.param(_drive_power_query, id="powerquery.extract_power_query"),
        pytest.param(_drive_docs, id="docs.extract_notes"),
        pytest.param(_drive_external_links, id="findings.external_link_targets"),
        pytest.param(_drive_profiles, id="profile.profile_sheet"),
    ],
)
def test_every_extractor_survives_the_hostile_workbook_on_its_own(
    drive: Callable[[Path], object],
) -> None:
    """Non-negotiable 4, extractor by extractor, so a failure localises.

    Driving each one directly rather than only through `analyse()` matters because `analyse()`
    catches everything: an extractor that raises would come back as an UNPARSEABLE_PART finding
    and the analysis would still look complete. Here the exception is the failure.
    """
    assert drive(HOSTILE.path) is not None


def test_the_indirect_over_a_cell_reference_degrades_rather_than_failing(
    hostile: WorkbookAnalysis,
) -> None:
    """Volatile!B8's target sheet name is not statically knowable. It must still be described."""
    operation = _by_anchor(hostile)["Volatile!B8"]

    assert operation.is_volatile is True
    assert "INDIRECT" in operation.functions
    assert operation.excel_pattern is ExcelPattern.UNKNOWN, (
        "an unresolvable reference is exactly the case UNKNOWN exists for (PLAN 2.7)"
    )
    assert operation.description
    assert operation.id in hostile.graph.nodes


def test_the_error_cell_is_read_as_an_error_rather_than_as_text(
    hostile: WorkbookAnalysis,
) -> None:
    """Messy!K9 is stored with cell type 'e'. A '#REF!' string would be a silent corruption."""
    findings = hostile.findings_of(FindingKind.ERROR_VALUE)

    assert len(findings) == 1
    assert findings[0].sheet == "Messy"
    assert findings[0].location == "K9"
    assert findings[0] in hostile.errors, "a #REF! is an error, not a warning"


def test_the_cycle_is_reported_at_cell_level_and_costs_the_topological_order(
    hostile: WorkbookAnalysis,
) -> None:
    """PLAN 2.7: circularity stops the scaffold and asks. It cannot be guessed at."""
    assert hostile.graph.topological_order is None
    assert hostile.graph.cycles

    found = {frozenset(cycle) for cycle in hostile.graph.cycles}
    assert frozenset({"Circular!C2", "Circular!C3", "Circular!C4"}) in found

    findings = hostile.findings_of(FindingKind.CIRCULAR_REFERENCE)
    assert findings
    assert all(finding in hostile.errors for finding in findings)


@pytest.mark.xfail(reason=_SELF_REFERENCE, strict=True)
def test_the_direct_self_reference_is_reported_as_a_cycle(hostile: WorkbookAnalysis) -> None:
    """Circular!E3 is `=E3+1`. Excel refuses to accept it; kedge does not notice it."""
    found = {frozenset(cycle) for cycle in hostile.graph.cycles}

    assert frozenset({"Circular!E3"}) in found


@pytest.mark.xfail(
    reason=(
        "classify.detect_embedded_totals matches any label starting 'total' in the first three "
        "columns, including the header row itself. legacy_sql!Summary has a `total_notional` "
        "header and C_Aggregate has a `total` header, and both are reported as a totals row "
        "mid-sheet at row 1 -- which is the header row the same analysis already identified."
    ),
    strict=True,
)
@pytest.mark.parametrize("filename", ["legacy_sql.xlsx", "cross_sheet_chain.xlsx"])
def test_the_header_row_is_never_reported_as_an_embedded_totals_row(
    filename: str, analyses: dict[str, WorkbookAnalysis]
) -> None:
    analysis = analyses[filename]
    header_rows = {sheet.name: sheet.header_row for sheet in analysis.sheets}

    for finding in analysis.findings_of(FindingKind.EMBEDDED_TOTALS_ROW):
        rows = {int(match) for match in re.findall(r"row (\d+)", finding.location or "")}
        assert header_rows.get(finding.sheet or "") not in rows, finding.message
