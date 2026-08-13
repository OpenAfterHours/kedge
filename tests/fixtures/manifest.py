"""Machine-readable ground truth for the .xlsx fixture corpus.

This module declares what the analyser is expected to find in each committed
fixture. It is deliberately hand-written and independent of ``generate.py``: if
the expectations were derived from the generator, asserting the analyser against
them would only prove the generator agrees with itself.

Two families of claim live here, and they are checked in different places:

* **Directly checkable** — sheet names, cached-value presence, connection count,
  Power Query query names, companion files. ``tests/unit/test_fixtures_generate.py``
  asserts these against the actual files, so the manifest cannot drift away from
  the corpus without a test going red.
* **Analyser-dependent** — sheet roles, logical operations, the dependency DAG,
  connections, notes and findings. ``tests/corpus/test_analysis_corpus.py``
  asserts these by running ``kedge.analysis.analyse.analyse`` over every fixture.

Region counts are expressed as inclusive ``(minimum, maximum)`` bounds rather
than exact figures. The measured value from a straightforward R1C1 grouping is
recorded in ``region_note`` for each fixture; the bounds leave room for
reasonable differences in how the analyser treats single-cell regions and
whether it merges across blank rows. The same convention applies per sheet in
``SheetExpectation.operation_bounds``, where the width of the range is usually
"does a compressor merge two adjacent columns that happen to share one R1C1
string?" — both answers are defensible and the fixture proves neither.

Individual operations, by contrast, are pinned exactly in
``FixtureExpectation.operations``: an anchor, a cell count and the A1 ranges the
region occupies are facts about the workbook, not judgements about the analyser.
``pattern`` is asserted only where the fixture was built to demonstrate that
shape (an empty ``pattern`` means "not asserted") because the boundary between,
say, ``aggregate`` and ``unknown`` for ``=ROUND(SUM(...),2)`` is a matter of
taste rather than of fact.

The dependency DAG is claimed from both sides, because a present-only claim can
never see an edge the analyser invents, and a false *upstream* edge inverts
dependency order in a generated scaffold. ``dependency_edges`` names edges that
must be there; ``dependency_edges_are_complete`` withdraws the subset licence
where the whole set is derivable by hand. It is derivable wherever the region
set itself is unambiguous. Where it is not -- ``clean_pipeline.xlsx`` and
``cross_sheet_chain.xlsx``, whose adjacent columns may or may not merge on a
shared R1C1 string -- stating the complete edge set would pin exactly the
judgement the region bounds refuse to pin, so those fixtures declare
``forbidden_edges`` instead: individual pairs hand-derived to be false.

Findings are claimed from both sides too, and for the same reason. ``findings``
names the traps a fixture sets and how many times each must fire;
``finding_kinds`` names *every* kind the workbook produces, so that a kind the
fixture does not produce is a claim the analyser can fail. Without it a detector
that fired on everything would satisfy every positive claim in this file, and
the findings list is the one place where a false positive costs the reader most
(PLAN section M1 leads with it). Each ``finding_kinds`` entry is derived from the
formulas and cells ``generate.py`` writes, never from what the analyser returns;
where the analyser and the fixture disagree the corpus tests carry a strict
xfail naming the defect, rather than the set being widened to accommodate it.

Finding kinds are lower-case snake_case strings rather than an enum because
``kedge.analysis.model`` owns the real vocabulary and did not exist when this was
written. Treat the names here as *proposed*; if the analyser settles on different
ones, map them in the corpus tests and update ``FINDING_KINDS`` below.
``tests/corpus/test_analysis_corpus.py`` holds that mapping and asserts it is
total in both directions, so neither a proposed name the analyser has no
equivalent for nor an analyser kind no fixture reaches can go unnoticed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent

__all__ = [
    "FINDING_KINDS",
    "FIXTURES",
    "FIXTURE_DIR",
    "PATTERN_NAMES",
    "SHEET_ROLES",
    "ConnectionExpectation",
    "FindingExpectation",
    "FixtureExpectation",
    "OperationExpectation",
    "SheetExpectation",
    "fixture_path",
    "fixtures_with_cached_values",
    "iter_fixtures",
]

# The five roles from PLAN section 2.4.
SHEET_ROLES = ("data", "calculation", "parameters", "documentation", "output")

# Proposed finding vocabulary. See the module docstring. Two names arrived late,
# when `finding_kinds` made each fixture state its complete set and these turned
# out to be produced by fixtures that had never needed to name them:
# `dead_region` (a formula region nothing else reads) and `unsupported_format`
# (the .doc companion, which attaches to every workbook in this directory).
# `document_attached_by_filename` arrived the same way and for the same reason:
# the companions attach on their names, and so does this directory's README.md.
FINDING_KINDS = (
    "blank_columns",
    "blank_rows",
    "circular_reference",
    "dates_stored_as_text",
    "dead_region",
    "document_attached_by_filename",
    "duplicate_headers",
    "error_cell",
    "external_workbook_link",
    "hardcoded_constant",
    "hidden_columns",
    "hidden_sheet",
    "iferror_swallow",
    "inconsistent_formula_in_region",
    "manual_override_block",
    "merged_cells",
    "no_cached_values",
    "numbers_stored_as_text",
    "preamble_rows",
    "totals_row_in_data",
    "unresolved_defined_name",
    "unresolvable_external_link",
    "unsupported_format",
    "volatile_function",
)

# The formula shapes from the PLAN section 2.7 translation vocabulary that this
# corpus actually exercises. Proposed names, on the same footing as
# FINDING_KINDS: `kedge.analysis.model.ExcelPattern` owns the real vocabulary.
PATTERN_NAMES = (
    "aggregate",
    "arithmetic",
    "countifs",
    "date_arithmetic",
    "if_nested",
    "iferror",
    "index_match",
    "parameter_ref",
    "prior_row",
    "running_total",
    "sumif",
    "sumifs",
    "sumproduct",
    "unknown",
    "vlookup_exact",
)


@dataclass(frozen=True, slots=True)
class SheetExpectation:
    """What the analyser should conclude about one sheet."""

    name: str
    role: str
    """The role the heuristic is expected to land on (PLAN section 2.4)."""

    role_alternatives: tuple[str, ...] = ()
    """Roles that would also be defensible. Corpus tests should accept any of
    ``(role, *role_alternatives)`` rather than pinning an ambiguous sheet."""

    has_formulas: bool = False
    hidden: bool = False

    operation_bounds: tuple[int, int] | None = None
    """Inclusive (min, max) logical operations on this sheet. None means the
    sheet's operation count is not claimed. Ranges rather than exact figures for
    the reason given in the module docstring."""

    header_row: int | None = None
    preamble_rows: int = 0
    """Structural facts the classifier keys on, asserted only where the fixture
    deliberately sets them: everything else leaves them at the defaults."""


@dataclass(frozen=True, slots=True)
class FindingExpectation:
    """A finding the analyser must raise, and where."""

    kind: str
    minimum: int = 1
    """Fewest occurrences that count as a pass."""

    locations: tuple[str, ...] = ()
    """Specific ``Sheet!Cell`` or ``Sheet!Range`` coordinates that must appear
    among the finding's locations, where the fixture sets the trap at a known
    spot. Empty means "somewhere on the workbook"."""

    note: str = ""


@dataclass(frozen=True, slots=True)
class OperationExpectation:
    """One R1C1-compressed logical operation the analyser must recover.

    The anchor, the cell count and the ranges are facts about the workbook: they
    say which cells share one relative formula and where those cells are. They
    are pinned exactly. ``pattern`` is a judgement about the translation
    vocabulary and is asserted only where the fixture exists to demonstrate it.
    """

    anchor: str
    """Sheet-qualified top-left cell of the region, e.g. ``Calc!D2``."""

    cell_count: int
    ranges: tuple[str, ...] = ()
    """Sheet-qualified A1 ranges the region occupies, in reading order. A region
    broken by blank or overwritten rows occupies several."""

    pattern: str = ""
    """Expected :class:`ExcelPattern` value, or empty for "not asserted"."""

    pattern_alternatives: tuple[str, ...] = ()
    reads: tuple[str, ...] = ()
    """Verbatim reference tokens the region's formula reads, as they appear in
    the anchor's A1 formula. Complete where declared, not a subset: what a
    formula refers to is readable off the formula, so a reference the analyser
    reports and this list does not name is one the analyser invented. Defined
    names are not references and do not appear here — ``capital_ratio`` in
    ``=ROUND(G2*capital_ratio,2)`` is counted against the name, not the region.
    Empty means "not asserted"."""

    is_volatile: bool = False
    reads_external_workbook: bool = False
    note: str = ""


@dataclass(frozen=True, slots=True)
class ConnectionExpectation:
    """One legacy external data connection and the statement it carries."""

    name: str
    kind: str
    """``odbc``, ``oledb``, ``web``, ``text``, ``worksheet`` or ``other``."""

    command_newlines: int
    """The statement is multi-line, and how many lines is the whole trap: see the
    ``legacy_sql.xlsx`` notes on XML attribute-value normalisation."""

    command_starts_with: str = ""
    command_contains: tuple[str, ...] = ()
    provider: str | None = None
    description: str | None = None
    secrets: tuple[str, ...] = ()
    """Fragments of the connection string that must NOT survive redaction."""

    note: str = ""


@dataclass(frozen=True, slots=True)
class FixtureExpectation:
    """Ground truth for one fixture workbook."""

    filename: str
    axis: str
    """The single M1 axis this fixture exists to cover."""

    sheets: tuple[SheetExpectation, ...]
    region_bounds: tuple[int, int]
    """Inclusive (min, max) logical R1C1 regions across the whole workbook."""

    region_note: str = ""
    operations: tuple[OperationExpectation, ...] = ()
    """The individual regions worth pinning. Not exhaustive: a fixture declares
    the operations it exists to demonstrate, and the rest are covered by
    ``region_bounds`` and by the cell-count reconciliation in the corpus tests."""

    findings: tuple[FindingExpectation, ...] = ()
    finding_kinds: tuple[str, ...] = ()
    """Every finding kind this workbook produces, as a set rather than a subset.

    ``findings`` says "this trap must fire"; this says "and nothing else fires".
    The two are declared separately because they answer different questions and
    fail for different reasons, and the corpus tests check that every kind named
    in ``findings`` also appears here so they cannot drift apart. Derived from
    what ``generate.py`` puts in the workbook, so a kind the analyser raises and
    this list does not name is over-reporting rather than a stale manifest."""

    finding_kinds_are_complete: bool = True
    """False withdraws the negative half of the ``finding_kinds`` claim for one
    fixture, for a workbook whose full set genuinely cannot be derived by hand.
    An honest partial claim beats a fabricated complete one — but it is not free:
    ``test_every_fixture_declares_the_complete_set_of_findings_it_produces``
    fails until somebody records there why this fixture is the exception."""

    cached_values_present: bool = False
    """True only where ``load_workbook(data_only=True)`` returns real numbers
    for formula cells. Reconciliation is possible only for these."""

    cached_value_coverage: float = 0.0
    """Fraction of formula cells carrying a cached value. Exactly 0.0 or 1.0
    across this corpus: no fixture is partially cached."""

    dependency_edges: tuple[tuple[str, str], ...] = ()
    """(upstream anchor, downstream anchor) pairs the DAG must contain, both
    sheet-qualified. A subset by default, chosen for what each fixture exists to
    prove — the analyser is free to find more — unless
    ``dependency_edges_are_complete`` says otherwise."""

    dependency_edges_are_complete: bool = False
    """True where ``dependency_edges`` is every edge in the workbook and not
    merely the interesting ones. Set only where the region set is unambiguous,
    so that the claim is about the formulas rather than about how the compressor
    chose to group them. An edge outside the declared set is then a defect."""

    forbidden_edges: tuple[tuple[str, str], ...] = ()
    """(upstream anchor, downstream anchor) pairs the DAG must NOT contain,
    hand-derived from the formulas. For the fixtures that cannot state a
    complete edge set, this is the only thing standing between the corpus and
    silent edge over-reporting."""

    cycles: tuple[tuple[str, ...], ...] = ()
    """Cell-level circular references, each an unordered set of sheet-qualified
    A1 refs. A workbook with a cycle has no topological order."""

    connection_count: int = 0
    connections: tuple[ConnectionExpectation, ...] = ()
    power_query_names: tuple[str, ...] = ()
    power_query_part: str | None = None
    """Where the DataMashup actually lives. The whole point of ``powerquery.xlsx``
    is that this is not ``customXml/item1.xml``."""

    power_query_references: tuple[tuple[str, tuple[str, ...]], ...] = ()
    """(query name, queries it reads) for queries that read another query."""

    named_ranges: tuple[str, ...] = ()
    broken_named_ranges: tuple[str, ...] = ()
    """Defined names whose target no longer exists. A subset of ``named_ranges``."""

    external_links: tuple[str, ...] = ()
    unparseable_parts: tuple[str, ...] = ()
    """Parts the analyser is expected to report as unparseable rather than
    raising on. Empty for every fixture at present — see README.md."""

    note_sources: tuple[str, ...] = ()
    """``ProcessNote.source`` values this workbook's own content must produce.
    Sidecar Word files match on filename, so they attach to every workbook in
    the fixture directory and are deliberately excluded here."""

    commented_cells: tuple[str, ...] = ()
    """Sheet-qualified cells carrying a comment whose text exists nowhere else."""

    documentation_headings: tuple[str, ...] = ()
    """Headings of the prose blocks on the documentation sheet. Every one must
    appear in the recovered notes, however the extractor chooses to block them."""

    companion_files: tuple[str, ...] = ()
    formula_cell_count: int = 0
    """Total formula cells, as a sanity check on the extraction before any
    compression is applied."""

    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def path(self) -> Path:
        """Absolute path to the committed workbook."""
        return FIXTURE_DIR / self.filename

    @property
    def sheet_names(self) -> tuple[str, ...]:
        """Sheet names in workbook order."""
        return tuple(sheet.name for sheet in self.sheets)

    def finding(self, kind: str) -> FindingExpectation | None:
        """Return the expectation for ``kind``, or None if not expected."""
        for expectation in self.findings:
            if expectation.kind == kind:
                return expectation
        return None

    def sheet(self, name: str) -> SheetExpectation | None:
        """Return the expectation for one sheet, or None if it is not declared."""
        for expectation in self.sheets:
            if expectation.name == name:
                return expectation
        return None

    def operation(self, anchor: str) -> OperationExpectation | None:
        """Return the expectation for the region anchored at ``Sheet!Cell``."""
        for expectation in self.operations:
            if expectation.anchor == anchor:
                return expectation
        return None


# =============================================================================
# The corpus
# =============================================================================

CLEAN_PIPELINE = FixtureExpectation(
    filename="clean_pipeline.xlsx",
    axis="The well-behaved case, and the R1C1 compression showcase.",
    sheets=(
        SheetExpectation("Data", "data", header_row=1),
        SheetExpectation("Ref", "parameters", ("data",), header_row=1),
        SheetExpectation("Params", "parameters", header_row=1),
        SheetExpectation("Calc", "calculation", has_formulas=True, operation_bounds=(7, 8)),
        SheetExpectation("Output", "output", has_formulas=True, operation_bounds=(6, 6)),
    ),
    region_bounds=(12, 18),
    region_note=(
        "Eight whole-column fills on Calc (4000 formula cells) plus six "
        "single-cell metrics on Output. The Calc figure is the headline claim — "
        "4000 cells must compress to eight logical operations, or to seven: "
        "Calc!B is =Data!C2 and Calc!C is =Data!D2, which are the same relative "
        "formula, so a compressor that merges adjacent columns sharing one R1C1 "
        "string sees one block of 1000 cells where a per-column one sees two. "
        "Both are correct; hence (7, 8) on the sheet and (12, 18) overall."
    ),
    operations=(
        OperationExpectation(
            anchor="Calc!A2",
            cell_count=500,
            ranges=("Calc!A2:A501",),
            reads=("Data!A2",),
            note="A bare cross-sheet passthrough, and the only Calc column nothing reads.",
        ),
        OperationExpectation(
            anchor="Calc!D2",
            cell_count=500,
            ranges=("Calc!D2:D501",),
            pattern="vlookup_exact",
            reads=("B2", "Ref!$A$2:$C$7"),
            note="=VLOOKUP(B2,Ref!$A$2:$C$7,2,FALSE) — the exact lookup that becomes a left join.",
        ),
        OperationExpectation(
            anchor="Calc!E2",
            cell_count=500,
            ranges=("Calc!E2:E501",),
            pattern="arithmetic",
            reads=("C2", "D2"),
        ),
        OperationExpectation(
            anchor="Calc!F2",
            cell_count=500,
            ranges=("Calc!F2:F501",),
            pattern="if_nested",
            reads=("Data!G2",),
            note="A five-deep IF chain on rating, all five arms reading the same cell.",
        ),
        OperationExpectation(
            anchor="Calc!G2",
            cell_count=500,
            ranges=("Calc!G2:G501",),
            pattern="arithmetic",
            reads=("E2", "F2"),
            note="=ROUND(E2*F2,2) — the reconciliation baseline column.",
        ),
        OperationExpectation(
            anchor="Calc!H2",
            cell_count=500,
            ranges=("Calc!H2:H501",),
            pattern="arithmetic",
            pattern_alternatives=("parameter_ref",),
            reads=("G2",),
            note="=ROUND(G2*capital_ratio,2) — a named range used as a parameter.",
        ),
        OperationExpectation(
            anchor="Output!B5",
            cell_count=1,
            ranges=("Output!B5",),
            pattern="aggregate",
            reads=("Calc!C2:C501",),
            note="=COUNT(Calc!C2:C501), the trade count control.",
        ),
    ),
    # Three kinds, and the well-behaved fixture is the one where that claim bites
    # hardest: anything else raised here is the analyser finding fault with a
    # workbook that has none. `hardcoded_constant` is Calc!F, the five-deep IF
    # chain over the literal risk weights 0.2, 0.3, 0.5, 1, 1.5 and 2 — the only
    # formula in the workbook whose numbers are not ROUND digit counts.
    # `dead_region` is Calc!A, the trade-id passthrough the operation note above
    # already says nothing reads. `unsupported_format` is procedure_legacy.doc,
    # which attaches to every workbook in this directory exactly as the two Word
    # note sources do. No cached-value finding: this is the fixture that has one.
    finding_kinds=(
        "dead_region",
        "document_attached_by_filename",
        "hardcoded_constant",
        "unsupported_format",
    ),
    cached_values_present=True,
    cached_value_coverage=1.0,
    dependency_edges=(
        ("Calc!D2", "Calc!E2"),
        ("Calc!E2", "Calc!G2"),
        ("Calc!F2", "Calc!G2"),
        ("Calc!G2", "Calc!H2"),
        ("Calc!G2", "Output!B3"),
        ("Calc!H2", "Output!B4"),
    ),
    # Not a complete set: whether Calc!B and Calc!C are one region or two decides
    # how the edges out of them are named. These three are false whichever way
    # that goes, and each is a column-precision claim — the whole risk in
    # sweeping a fill's references analytically is that the sweep spills into the
    # column next door.
    forbidden_edges=(
        # Calc!A is the trade-id passthrough, and nothing in the workbook reads
        # it. Output!B2 is =ROUND(SUM(Calc!C2:C501),2), two columns away.
        ("Calc!A2", "Output!B2"),
        # Output!B3 is =ROUND(SUM(Calc!G2:G501),2). F and H are the columns
        # immediately either side of G, and neither is in it.
        ("Calc!F2", "Output!B3"),
        ("Calc!H2", "Output!B3"),
    ),
    named_ranges=("capital_ratio", "fx_rate_usd", "reporting_date", "tolerance"),
    formula_cell_count=4006,
    notes=(
        "The only fixture with cached values, so the only positive path for "
        "reconciliation (PLAN section 4.5).",
        "Cached values were verified cell by cell against a real Excel "
        "recalculation; all 2506 agree exactly.",
        "Calc!D is an exact VLOOKUP, Calc!F a five-deep IF chain, Calc!G and "
        "Calc!H are ROUND — between them they cover most of PLAN section 2.7.",
    ),
)

POWERQUERY = FixtureExpectation(
    filename="powerquery.xlsx",
    axis="A real Power Query DataMashup, hidden behind decoy customXml parts.",
    sheets=(
        SheetExpectation(
            "Report",
            "output",
            ("calculation", "data"),
            has_formulas=True,
            header_row=1,
            operation_bounds=(2, 2),
        ),
        SheetExpectation("Staging", "data", header_row=1),
        SheetExpectation("HaircutTable", "parameters", ("data",), header_row=1),
    ),
    region_bounds=(2, 4),
    region_note="A share column and a total on Report, which is two.",
    operations=(
        OperationExpectation(
            anchor="Report!C2",
            cell_count=6,
            ranges=("Report!C2:C7",),
            reads=("B2", "$B$9"),
            note="=ROUND(B2/$B$9,4) — each row's share of the total two rows below it.",
        ),
        OperationExpectation(
            anchor="Report!B9",
            cell_count=1,
            ranges=("Report!B9",),
            pattern="aggregate",
            reads=("B2:B7",),
        ),
    ),
    # Report!C2:C7 is the share column and nothing reads it, which is the dead
    # region; B9 is read by C2, so it is not. The dead-region finding is
    # suppressed on sheets classified `output`, and Report is not one — it reads
    # no other sheet, and every OUTPUT signal in the classifier needs that — so
    # this entry stands or falls with the role. It is the only thing holding it
    # up here, which is worth knowing on the day the role scoring changes.
    # =ROUND(B2/$B$9,4) and =SUM(B2:B7)
    # between them hold one numeric literal, the ROUND digit count. The "total"
    # label on A9 sits on the last populated row — a totals row at the bottom,
    # which is normal and not a finding. The mashup lives in the archive rather
    # than in a cell, so it produces no finding of its own.
    finding_kinds=(
        "dead_region",
        "document_attached_by_filename",
        "no_cached_values",
        "unsupported_format",
    ),
    dependency_edges=(("Report!B9", "Report!C2"),),
    # Two operations and one edge between them. C2:C7 is =ROUND(B2/$B$9,4), so it
    # reads the total below it; B9 is =SUM(B2:B7) over typed values, so it reads
    # nothing the workbook computes. There is no third possibility to leave out.
    dependency_edges_are_complete=True,
    power_query_names=("Exposures", "CollateralHaircuts", "NetExposure"),
    power_query_part="customXml/item3.xml",
    power_query_references=(("NetExposure", ("Exposures", "CollateralHaircuts")),),
    formula_cell_count=7,
    notes=(
        "The DataMashup is at customXml/item3.xml, NOT item1.xml. Two decoy "
        "customXml parts with unrelated schemas sit in front of it, so the "
        "extractor must iterate and match on the DataMashup namespace "
        "(PLAN section 1.5 warns about exactly this).",
        "The base64 payload is a real MS-QDEFF envelope, not a bare zip: the "
        "inner OPC package starts at byte offset 8, behind a version field and "
        "a length field, and is followed by permissions and metadata blocks.",
        "Measured caveat, stated because it limits what this fixture proves: "
        "Python's zipfile opens the envelope anyway. It finds the central "
        "directory by scanning backwards, so the eight-byte prefix and the "
        "trailing blocks do not stop it. A naive base64-decode-then-unzip "
        "therefore passes against this fixture. It is still wrong — it discards "
        "the permissions and metadata and relies on that tolerance — but the "
        "corpus cannot catch it. Verified in "
        "test_naive_unzip_of_the_envelope_still_works_in_python.",
        "NetExposure references the other two queries, so query-level dependency "
        "extraction has something to find.",
    ),
)

LEGACY_SQL = FixtureExpectation(
    filename="legacy_sql.xlsx",
    axis="Legacy ODBC and OLEDB connections carrying embedded SQL.",
    sheets=(
        SheetExpectation("Extract", "data", header_row=1),
        SheetExpectation(
            "Summary",
            "output",
            ("calculation",),
            has_formulas=True,
            header_row=1,
            operation_bounds=(2, 2),
        ),
    ),
    region_bounds=(2, 4),
    region_note="SUMIF and COUNTIF columns on Summary, which is two.",
    operations=(
        OperationExpectation(
            anchor="Summary!B2",
            cell_count=3,
            ranges=("Summary!B2:B4",),
            pattern="sumif",
            reads=("Extract!$D$2:$D$121", "A2", "Extract!$C$2:$C$121"),
        ),
        OperationExpectation(
            anchor="Summary!C2",
            cell_count=3,
            ranges=("Summary!C2:C4",),
            pattern="countifs",
            reads=("Extract!$D$2:$D$121", "A2"),
            note="COUNTIF, which the vocabulary folds into the COUNTIFS shape.",
        ),
    ),
    # Both Summary columns are terminal aggregates over a typed sheet, so both
    # are dead regions — on the same footing as powerquery.xlsx's share column,
    # and with the same caveat: the finding is suppressed for a sheet classified
    # `output`, and Summary loses that role to `calculation` for the reason the
    # corpus tests give under clean_pipeline!Output.
    # SUMIF and COUNTIF carry no numeric literals at all. The
    # connections extractor raises a finding only for a connection it cannot
    # read, and the whole point of this fixture is that both of these parse.
    finding_kinds=(
        "dead_region",
        "document_attached_by_filename",
        "no_cached_values",
        "unsupported_format",
    ),
    # Deliberately empty, and complete: both Summary columns aggregate the typed
    # Extract sheet and neither reads the other, so this workbook has two
    # operations and no dependencies at all. An edge here would be invented.
    dependency_edges=(),
    dependency_edges_are_complete=True,
    connection_count=2,
    connections=(
        ConnectionExpectation(
            name="RiskWarehouse",
            kind="odbc",
            command_newlines=17,
            command_starts_with="SELECT",
            command_contains=(
                "FROM risk.trades AS t",
                "INNER JOIN risk.counterparties AS c",
                "ORDER BY t.trade_date DESC",
            ),
            description="Trade extract from the risk warehouse (nightly)",
            secrets=("svc_riskread",),
            note="Eighteen lines, so seventeen newlines, all stored as &#10;.",
        ),
        ConnectionExpectation(
            name="FinanceCube",
            kind="oledb",
            command_newlines=6,
            command_starts_with="SELECT",
            command_contains=(
                "SUM(rwa_amount) AS total_rwa",
                "GROUP BY period_end, legal_entity",
            ),
            provider="MSOLAP.8",
            description="Finance RWA summary cube",
        ),
    ),
    formula_cell_count=6,
    notes=(
        "openpyxl does not parse xl/connections.xml at all; it must be read from "
        "the zip directly (PLAN section 1.5).",
        "Connection 1 is ODBC (type=1) with an 18-line SELECT and a DSN "
        "connection string. Connection 2 is OLEDB (type=5) against an OLAP cube.",
        "Newlines inside the command attribute are encoded as &#10;. A parser "
        "that reads the attribute without decoding character references will get "
        "one long line back; a parser that lets XML attribute-value "
        "normalisation apply will get spaces instead of newlines.",
    ),
)

CROSS_SHEET_CHAIN = FixtureExpectation(
    filename="cross_sheet_chain.xlsx",
    axis="Deep cross-sheet dependency chains, for the DAG and fan-out analysis.",
    sheets=(
        SheetExpectation("Params", "parameters"),
        SheetExpectation("A_Source", "data", header_row=1),
        SheetExpectation(
            "B_Enrich",
            "calculation",
            has_formulas=True,
            header_row=1,
            operation_bounds=(5, 6),
        ),
        SheetExpectation(
            "C_Aggregate",
            "calculation",
            has_formulas=True,
            header_row=1,
            operation_bounds=(3, 3),
        ),
        SheetExpectation(
            "D_Report", "output", has_formulas=True, header_row=1, operation_bounds=(8, 8)
        ),
    ),
    region_bounds=(14, 22),
    region_note=(
        "Six on B_Enrich (the running total splits at its seed row), three on "
        "C_Aggregate, eight single cells on D_Report — or five on B_Enrich, "
        "because columns A and B are both =A_Source!<same column>, one relative "
        "formula between them. Sixteen or seventeen overall."
    ),
    operations=(
        OperationExpectation(
            anchor="B_Enrich!C2",
            cell_count=120,
            ranges=("B_Enrich!C2:C121",),
            pattern="arithmetic",
            reads=("A_Source!C2", "Params!$B$1"),
            note="The absolute parameter reference PLAN section 2.7 maps to a constant.",
        ),
        OperationExpectation(
            anchor="B_Enrich!D2",
            cell_count=120,
            ranges=("B_Enrich!D2:D121",),
            pattern="iferror",
            reads=("C2", "A_Source!D2", "Params!$B$2"),
        ),
        OperationExpectation(
            anchor="B_Enrich!E2",
            cell_count=1,
            ranges=("B_Enrich!E2",),
            reads=("C2",),
            note="The running total's seed row: =C2, with no row above to read.",
        ),
        OperationExpectation(
            anchor="B_Enrich!E3",
            cell_count=119,
            ranges=("B_Enrich!E3:E121",),
            pattern="running_total",
            pattern_alternatives=("prior_row",),
            reads=("E2", "C3"),
            note="=E2+C3 filled down: 119 cells, one operation, not 119.",
        ),
        OperationExpectation(
            anchor="C_Aggregate!C2",
            cell_count=9,
            ranges=("C_Aggregate!C2:C10",),
            pattern="sumifs",
        ),
        OperationExpectation(
            anchor="C_Aggregate!D2",
            cell_count=9,
            ranges=("C_Aggregate!D2:D10",),
            pattern="sumproduct",
        ),
        OperationExpectation(
            anchor="C_Aggregate!E2",
            cell_count=9,
            ranges=("C_Aggregate!E2:E10",),
            pattern="index_match",
            pattern_alternatives=("iferror",),
            note="INDEX/MATCH inside an IFERROR: either half is a defensible label.",
        ),
        OperationExpectation(
            anchor="D_Report!B9",
            cell_count=1,
            ranges=("D_Report!B9",),
            reads=("B_Enrich!E121",),
            note="The layer-skipping probe: D_Report reaches past C_Aggregate into B_Enrich.",
        ),
    ),
    # Four IFERROR wrappers — B_Enrich!D, C_Aggregate!E and D_Report!B7 and B8 —
    # and the terminal regions: C_Aggregate!E, because nothing reads first_price,
    # and the D_Report metrics that feed no other cell. Every numeric literal in
    # the workbook is either a ROUND digit count or an IFERROR fallback of 0, so
    # none of them is the kind of embedded rate the constant finding is for.
    finding_kinds=(
        "dead_region",
        "document_attached_by_filename",
        "iferror_swallow",
        "no_cached_values",
        "unsupported_format",
    ),
    dependency_edges=(
        ("B_Enrich!C2", "C_Aggregate!C2"),
        ("B_Enrich!C2", "C_Aggregate!D2"),
        ("B_Enrich!D2", "C_Aggregate!E2"),
        ("B_Enrich!E2", "B_Enrich!E3"),
        ("B_Enrich!E3", "D_Report!B9"),
        ("C_Aggregate!C2", "D_Report!B2"),
        ("C_Aggregate!D2", "D_Report!B3"),
        ("D_Report!B2", "D_Report!B4"),
        ("D_Report!B3", "D_Report!B4"),
    ),
    # Not a complete set, for the same reason as clean_pipeline: B_Enrich!A and
    # !B are both =A_Source!<same column> and may compress to one region.
    forbidden_edges=(
        # C_Aggregate reads B_Enrich columns A, B, C and D. It never reads E, the
        # running total — the only thing that reads E is D_Report!B9, which is
        # what makes B9 the layer-skipping probe.
        ("B_Enrich!E3", "C_Aggregate!C2"),
        # D_Report!B2 is =SUM(C_Aggregate!C2:C10) and B3 is =SUM(C_Aggregate!
        # D2:D10). One column each, and they are adjacent columns.
        ("C_Aggregate!D2", "D_Report!B2"),
        ("C_Aggregate!C2", "D_Report!B3"),
    ),
    named_ranges=("uplift_factor",),
    formula_cell_count=635,
    notes=(
        "The chain is four deep: A_Source to B_Enrich to C_Aggregate to D_Report. "
        "Params fans out into B_Enrich and D_Report.",
        "B_Enrich!E is a running total — E2 seeds from C2, then E(n) = E(n-1) + "
        "C(n). That is a cum_sum and a row-above reference in one column, and it "
        "should compress to two regions, not 120.",
        "C_Aggregate covers SUMIFS, SUMPRODUCT and INDEX/MATCH; D_Report!B9 "
        "reaches back into B_Enrich, so the DAG is not a clean layer cake.",
        "Params!$B$1 and $B$2 are absolute parameter references, which PLAN "
        "section 2.7 maps to Python constants rather than columns.",
    ),
)

MOSTLY_MANUAL = FixtureExpectation(
    filename="mostly_manual.xlsx",
    axis="Hand-typed values with a thin calculation layer over the top.",
    sheets=(
        SheetExpectation("Input", "data", header_row=1),
        SheetExpectation("Overrides", "data", ("parameters",), header_row=3, preamble_rows=2),
        SheetExpectation(
            "Calc", "calculation", has_formulas=True, header_row=1, operation_bounds=(4, 4)
        ),
        SheetExpectation(
            "Output", "output", has_formulas=True, header_row=1, operation_bounds=(4, 4)
        ),
    ),
    region_bounds=(6, 12),
    region_note="Four columns on Calc, four single cells on Output, which is eight.",
    operations=(
        OperationExpectation(
            anchor="Calc!B2",
            cell_count=5,
            ranges=("Calc!B2:B6",),
            pattern="sumif",
            reads=("Input!$A$2:$A$61", "A2", "Input!$C$2:$C$61"),
        ),
        OperationExpectation(
            anchor="Calc!C2",
            cell_count=5,
            ranges=("Calc!C2:C6",),
            reads=("B2",),
            note="=ROUND(B2*0.2,2) — the VAT rate, hardcoded.",
        ),
        OperationExpectation(
            anchor="Calc!D2",
            cell_count=5,
            ranges=("Calc!D2:D6",),
            reads=("B2",),
            note="=ROUND(B2*0.08,2) — the contingency rate, hardcoded.",
        ),
        OperationExpectation(
            anchor="Calc!E2",
            cell_count=5,
            ranges=("Calc!E2:E6",),
            reads=("A2", "B2", "C2", "D2", "Overrides!$A$4:$A$17", "Overrides!$B$4:$B$17"),
            note=(
                "The only formula that reads the manual override block. A2 is the "
                "SUMIF criterion, and is as much a reference as the two ranges it "
                "sits between."
            ),
        ),
        OperationExpectation(
            anchor="Output!B5",
            cell_count=1,
            ranges=("Output!B5",),
            reads=("B3",),
            note="=ROUND(B3/47,2) — a headcount divisor with no provenance at all.",
        ),
    ),
    # Eight operations, none of them ambiguous, so the whole DAG is derivable:
    # C and D are =ROUND(B*rate,2); E is =B+C+D+SUMIF(Overrides!...); Output!B2
    # and B3 total Calc!B and Calc!E; B4 is =ROUND(B3-B2,2) and B5 is
    # =ROUND(B3/47,2). Calc!A and the Overrides block carry no formula, so
    # nothing flows out of them.
    dependency_edges=(
        ("Calc!B2", "Calc!C2"),
        ("Calc!B2", "Calc!D2"),
        ("Calc!B2", "Calc!E2"),
        ("Calc!B2", "Output!B2"),
        ("Calc!C2", "Calc!E2"),
        ("Calc!D2", "Calc!E2"),
        ("Calc!E2", "Output!B3"),
        ("Output!B2", "Output!B4"),
        ("Output!B3", "Output!B4"),
        ("Output!B3", "Output!B5"),
    ),
    dependency_edges_are_complete=True,
    findings=(
        FindingExpectation(
            kind="manual_override_block",
            minimum=1,
            locations=("Overrides!A4:D17",),
            note=(
                "Fourteen typed rows with no formula and no provenance. This is "
                "the checkpoint-stage trigger from PLAN section 2.2 — it must not "
                "be translated into generated code."
            ),
        ),
        FindingExpectation(
            kind="hardcoded_constant",
            minimum=3,
            locations=("Calc!C2", "Calc!D2", "Output!B5"),
            note=(
                "0.2 (VAT) and 0.08 (contingency) are embedded in Calc formulas "
                "where a Params reference belonged; Output!B5 divides by a "
                "hardcoded headcount of 47."
            ),
        ),
    ),
    # The two traps above, plus three the fixture sets without meaning to make a
    # point of them: the Overrides title row and its blank spacer put that
    # sheet's header on row 3, which is the preamble finding; Output!B4 and B5
    # are terminal, which is the dead region; and the .doc companion attaches
    # here as it does everywhere. manual_override_block is listed even though
    # the analyser does not raise it, because this set is the fixture's ground
    # truth rather than a transcript of the analyser — the corpus test carries
    # the strict xfail that says so.
    finding_kinds=(
        "dead_region",
        "document_attached_by_filename",
        "hardcoded_constant",
        "manual_override_block",
        "no_cached_values",
        "preamble_rows",
        "unsupported_format",
    ),
    formula_cell_count=24,
    notes=(
        "The ratio matters as much as the traps: roughly 60 typed input rows and "
        "14 typed overrides against 24 formula cells. Triage should score this "
        "workbook as only partly convertible (PLAN section 2.2).",
    ),
)

DOCUMENTED = FixtureExpectation(
    filename="documented.xlsx",
    axis="Process documentation, in a sheet and in a companion Word file.",
    sheets=(
        SheetExpectation("Process Notes", "documentation"),
        SheetExpectation("Data", "data", header_row=1),
        SheetExpectation(
            "Calc", "calculation", has_formulas=True, header_row=1, operation_bounds=(4, 4)
        ),
        SheetExpectation("Ref", "parameters", ("data",), header_row=1),
    ),
    region_bounds=(3, 6),
    region_note=(
        "Four whole-column fills on Calc over 80 rows. Nothing merges here: A is "
        "=Data!A2 and B is =Data!D2, three columns apart in relative terms."
    ),
    operations=(
        OperationExpectation(
            anchor="Calc!A2", cell_count=80, ranges=("Calc!A2:A81",), reads=("Data!A2",)
        ),
        OperationExpectation(
            anchor="Calc!B2", cell_count=80, ranges=("Calc!B2:B81",), reads=("Data!D2",)
        ),
        OperationExpectation(
            anchor="Calc!C2",
            cell_count=80,
            ranges=("Calc!C2:C81",),
            pattern="vlookup_exact",
            reads=("Data!C2", "Ref!$A$2:$B$7"),
            note="The lookup the Calc!C1 comment says must NOT be wrapped in IFERROR.",
        ),
        OperationExpectation(
            anchor="Calc!D2",
            cell_count=80,
            ranges=("Calc!D2:D81",),
            pattern="arithmetic",
            reads=("B2", "C2"),
        ),
    ),
    # The quietest fixture in the corpus, and deliberately so: the only region
    # nothing reads is Calc!A, the trade-id passthrough, and every literal in
    # the four Calc columns is a ROUND digit count or the 1 in (1-C2). The .doc
    # companion is this fixture's own, which is why it is the one that asserts
    # the conversion hint. The three cell comments and the Process Notes prose
    # are notes, not findings — nothing about documenting a workbook is a fault.
    finding_kinds=(
        "dead_region",
        "document_attached_by_filename",
        "no_cached_values",
        "unsupported_format",
    ),
    # Four operations on one sheet and nothing merges, so this is the whole DAG.
    # Only Calc!D reads another formula column; Calc!A, B and C all read the
    # typed Data and Ref sheets, and nothing reads Calc!A at all.
    dependency_edges=(("Calc!B2", "Calc!D2"), ("Calc!C2", "Calc!D2")),
    dependency_edges_are_complete=True,
    note_sources=("sheet", "cell_comment"),
    commented_cells=("Data!A1", "Data!D1", "Calc!C1"),
    documentation_headings=(
        "Purpose",
        "Inputs",
        "Step 1",
        "Step 2",
        "Step 3",
        "Known issues",
        "Sign-off",
    ),
    companion_files=("documented_procedure.docx", "procedure_legacy.doc"),
    formula_cell_count=320,
    notes=(
        "Process Notes holds seven long prose blocks (200 to 400 characters "
        "each) under bold headings — the documentation role heuristic in PLAN "
        "section 2.4 keys on exactly this shape.",
        "Three cell comments carry operational detail that is not in any cell "
        "value: Data!A1, Data!D1 and Calc!C1. The comment on Calc!C1 explicitly "
        "says not to wrap the VLOOKUP in IFERROR, which is the kind of intent "
        "that should reach the plan.",
        "documented_procedure.docx is a real .docx for python-docx to read. "
        "procedure_legacy.doc is deliberately NOT an OLE2 compound file, so the "
        "'fail clearly with a conversion hint' path has a target.",
    ),
)

HOSTILE = FixtureExpectation(
    filename="hostile.xlsx",
    axis="Deliberately awful. A complete analysis with findings, never an exception.",
    sheets=(
        SheetExpectation(
            "Messy",
            "calculation",
            ("data",),
            has_formulas=True,
            header_row=4,
            preamble_rows=3,
            operation_bounds=(7, 9),
        ),
        SheetExpectation("Volatile", "calculation", has_formulas=True, operation_bounds=(7, 7)),
        SheetExpectation("Circular", "calculation", has_formulas=True, operation_bounds=(4, 4)),
        SheetExpectation("Broken", "calculation", has_formulas=True, operation_bounds=(3, 3)),
        SheetExpectation("_Archive", "data", hidden=True, header_row=1),
    ),
    region_bounds=(20, 34),
    region_note=(
        "This is the fixture where the total is least meaningful: blank rows and "
        "the row-47 break fragment the Messy columns on purpose. Assert the "
        "bounds loosely and the specific traps precisely. Messy is (7, 9) "
        "because the D column's fill, the typed-over cell, the two totals rows "
        "and the three K-column one-offs are seven if the paired B/D totals "
        "cells merge on their shared R1C1 and nine if they do not — the bound is "
        "the width of that one judgement and nothing wider."
    ),
    operations=(
        OperationExpectation(
            anchor="Messy!D5",
            cell_count=72,
            ranges=(
                "Messy!D5:D29",
                "Messy!D31:D44",
                "Messy!D46",
                "Messy!D48:D59",
                "Messy!D61:D80",
            ),
            pattern="arithmetic",
            reads=("B5", "C5"),
            note=(
                "=B*C over rows 5 to 80, minus the subtotal at 30, the blank rows "
                "at 45 and 60, and the typed-over row at 47. Seventy-six rows "
                "less four is seventy-two cells, in five contiguous runs, and it "
                "is still ONE logical operation."
            ),
        ),
        OperationExpectation(
            anchor="Messy!D47",
            cell_count=1,
            ranges=("Messy!D47",),
            pattern="arithmetic",
            reads=("B47", "C47"),
            note="=B47*C47*1.1, the cell somebody typed over. Its own region, and a finding.",
        ),
        OperationExpectation(
            anchor="Messy!K6",
            cell_count=1,
            ranges=("Messy!K6",),
            pattern="iferror",
            reads=("B5", "L5"),
            note="A real division by zero: L5 is 0, and IFERROR turns the error into 0.",
        ),
        OperationExpectation(
            anchor="Messy!K7",
            cell_count=1,
            ranges=("Messy!K7",),
            pattern="iferror",
            reads=("A5:B80",),
            note="A VLOOKUP miss swallowed into an empty string.",
        ),
        OperationExpectation(
            anchor="Volatile!B2", cell_count=1, ranges=("Volatile!B2",), is_volatile=True
        ),
        OperationExpectation(
            anchor="Volatile!B3", cell_count=1, ranges=("Volatile!B3",), is_volatile=True
        ),
        OperationExpectation(
            anchor="Volatile!B4", cell_count=1, ranges=("Volatile!B4",), is_volatile=True
        ),
        OperationExpectation(
            anchor="Volatile!B5", cell_count=1, ranges=("Volatile!B5",), is_volatile=True
        ),
        OperationExpectation(
            anchor="Volatile!B6", cell_count=1, ranges=("Volatile!B6",), is_volatile=True
        ),
        OperationExpectation(
            anchor="Volatile!B7", cell_count=1, ranges=("Volatile!B7",), is_volatile=True
        ),
        OperationExpectation(
            anchor="Volatile!B8",
            cell_count=1,
            ranges=("Volatile!B8",),
            is_volatile=True,
            note=(
                "INDIRECT over a cell reference: the target sheet name is not "
                "statically knowable, so the extractor must degrade rather than fail."
            ),
        ),
        OperationExpectation(anchor="Circular!C2", cell_count=1, ranges=("Circular!C2",)),
        OperationExpectation(anchor="Circular!C3", cell_count=1, ranges=("Circular!C3",)),
        OperationExpectation(anchor="Circular!C4", cell_count=1, ranges=("Circular!C4",)),
        OperationExpectation(
            anchor="Circular!E3",
            cell_count=1,
            ranges=("Circular!E3",),
            reads=("E3",),
            note="=E3+1. A cell that reads itself.",
        ),
        OperationExpectation(
            anchor="Broken!A2",
            cell_count=1,
            ranges=("Broken!A2",),
            reads_external_workbook=True,
            note="=[1]Rates!$B$2 — the index form, resolvable only via the external link part.",
        ),
        OperationExpectation(
            anchor="Broken!A3",
            cell_count=1,
            ranges=("Broken!A3",),
            reads_external_workbook=True,
        ),
        OperationExpectation(
            anchor="Broken!A5",
            cell_count=1,
            ranges=("Broken!A5",),
            note="=SUM(obsolete_rate_table), over a defined name pointing at #REF!#REF!.",
        ),
    ),
    cycles=(("Circular!C2", "Circular!C3", "Circular!C4"), ("Circular!E3",)),
    findings=(
        FindingExpectation(
            kind="inconsistent_formula_in_region",
            locations=("Messy!D47",),
            note=(
                "The classic 'someone typed over row 47' bug. D5:D80 is "
                "=B*C throughout except D47, which is =B47*C47*1.1. Its "
                "neighbours D46 and D48 are untouched."
            ),
        ),
        FindingExpectation(
            kind="circular_reference",
            minimum=2,
            locations=("Circular!C2", "Circular!E3"),
            note=(
                "A genuine three-cell cycle C2 -> C4 -> C3 -> C2, plus a direct "
                "self-reference at E3. PLAN section 2.7 says circularity should "
                "stop the scaffold and ask, not be guessed at."
            ),
        ),
        FindingExpectation(
            kind="volatile_function",
            minimum=5,
            locations=("Volatile!B2", "Volatile!B3", "Volatile!B4", "Volatile!B6", "Volatile!B7"),
            note="NOW, TODAY, RAND, RANDBETWEEN, OFFSET and INDIRECT.",
        ),
        FindingExpectation(
            kind="iferror_swallow",
            minimum=2,
            locations=("Messy!K6", "Messy!K7"),
            note=(
                "K6 wraps a real division by zero (L5 is 0); K7 swallows a "
                "VLOOKUP miss into an empty string. Both hide a genuine error."
            ),
        ),
        FindingExpectation(
            kind="error_cell",
            minimum=1,
            locations=("Messy!K9",),
            note="A hard #REF! error cell, stored with cell type 'e'.",
        ),
        FindingExpectation(
            kind="external_workbook_link",
            minimum=1,
            locations=("Broken!A2", "Broken!A3"),
            note=(
                "A real xl/externalLinks/externalLink1.xml part pointing at "
                "file:///Z:/retired/vendor_rates_2019.xlsx, which cannot resolve. "
                "The formulas use the [1] index form, so resolving them requires "
                "reading the external link part."
            ),
        ),
        FindingExpectation(
            kind="unresolved_defined_name",
            minimum=1,
            locations=("obsolete_rate_table",),
            note="Points at #REF!#REF! — the range it named was deleted.",
        ),
        FindingExpectation(kind="hidden_sheet", locations=("_Archive",)),
        FindingExpectation(kind="hidden_columns", locations=("Messy!H", "Messy!I")),
        FindingExpectation(
            kind="merged_cells",
            minimum=2,
            locations=("Messy!A1:F1", "Messy!A61:B61"),
            note="One in the title preamble, one buried inside the data region.",
        ),
        FindingExpectation(
            kind="preamble_rows",
            locations=("Messy!1:3",),
            note="Three rows of title and metadata before the header on row 4.",
        ),
        FindingExpectation(
            kind="duplicate_headers",
            locations=("Messy!B4", "Messy!D4"),
            note="Both are named 'Amount'.",
        ),
        FindingExpectation(
            kind="totals_row_in_data",
            minimum=1,
            locations=("Messy!30",),
            note=(
                "A subtotal row sitting in the MIDDLE of the data, rows 5 to 80. "
                "There is a second totals row at row 82, which is merely at the "
                "bottom and therefore much easier to spot."
            ),
        ),
        FindingExpectation(
            kind="numbers_stored_as_text",
            locations=("Messy!E",),
            note="Column E holds numeric strings such as '742.19'.",
        ),
        FindingExpectation(
            kind="dates_stored_as_text",
            locations=("Messy!G",),
            note=(
                "Five formats mixed in one column: 01/03/2026, 2026-03-04, "
                "7 Mar 2026, Mar-26 and the ambiguous US-ordered 03/15/2026."
            ),
        ),
        FindingExpectation(kind="blank_rows", minimum=2, locations=("Messy!45", "Messy!60")),
        FindingExpectation(kind="blank_columns", minimum=1, locations=("Messy!F",)),
        FindingExpectation(
            kind="unresolvable_external_link",
            minimum=1,
            locations=("xl/externalLinks/_rels/externalLink1.xml.rels",),
            note=(
                "Distinct from external_workbook_link: that one says a formula "
                "reads another workbook, this one says the workbook it reads is "
                "not there. Z:\\retired\\ has not existed for years."
            ),
        ),
    ),
    # Every trap above that the model has a kind for, and three the fixture
    # produces without being built to:
    #   `hardcoded_constant` — the typed-over cell is =B47*C47*1.1, and the 1.1
    #     is a magic number with no provenance whichever way you read it. The
    #     over-typing is the point; the constant is a true observation about it.
    #   `dead_region` — the Volatile sheet is seven one-off formulas nothing
    #     reads, Broken is three more, and the two totals rows are terminal.
    #   `unsupported_format` — the .doc companion, as everywhere else.
    # `manual_override_block` is the one kind this workbook does NOT produce:
    # that finding needs three or more typed-in literals sitting inside a
    # formula region, and column D's only intruders are a subtotal and a
    # different formula. mostly_manual.xlsx is where that trap lives.
    # blank_rows and blank_columns are here because the workbook does contain
    # them and the traps above say where; they are proposed names the model has
    # no kind for, so the corpus tests skip them the way they skip them in
    # `findings`, and this set will already be right on the day it gains one.
    finding_kinds=(
        "blank_columns",
        "blank_rows",
        "circular_reference",
        "dates_stored_as_text",
        "dead_region",
        "document_attached_by_filename",
        "duplicate_headers",
        "error_cell",
        "external_workbook_link",
        "hardcoded_constant",
        "hidden_columns",
        "hidden_sheet",
        "iferror_swallow",
        "inconsistent_formula_in_region",
        "merged_cells",
        "no_cached_values",
        "numbers_stored_as_text",
        "preamble_rows",
        "totals_row_in_data",
        "unresolvable_external_link",
        "unresolved_defined_name",
        "unsupported_format",
        "volatile_function",
    ),
    dependency_edges=(("Messy!D5", "Messy!B30"), ("Messy!D5", "Messy!B82")),
    forbidden_edges=(
        # The one that matters. D5:D80 is =B{n}*C{n} on every row it occupies,
        # and it does not occupy row 30 — the subtotal sits there instead. So no
        # cell of the fill reads B30 or D30, and the fill is strictly UPSTREAM of
        # the subtotal, never downstream of it. The reverse edge is declared
        # above and is correct: D30 is =SUM(D5:D29), which reads the fill.
        ("Messy!B30", "Messy!D5"),
        # The typed-over cell is =B47*C47*1.1. It reads the same two typed
        # columns the fill does and neither reads the other, in either
        # direction, even though row 47 sits in the middle of the fill's rows.
        ("Messy!D47", "Messy!D5"),
        ("Messy!D5", "Messy!D47"),
        # K7's VLOOKUP range is A5:B80 — two columns wide, and column D is not
        # one of them.
        ("Messy!D5", "Messy!K7"),
    ),
    external_links=("file:///Z:/retired/vendor_rates_2019.xlsx",),
    named_ranges=("obsolete_rate_table", "messy_amounts"),
    broken_named_ranges=("obsolete_rate_table",),
    formula_cell_count=94,
    notes=(
        "This is the most important fixture in the corpus. The requirement is "
        "not that every finding above is raised, but that the analyser returns a "
        "COMPLETE WorkbookAnalysis with findings populated and never raises "
        "(PLAN section M1, CONVENTIONS rule 4).",
        "Volatile!B8 nests INDIRECT over a cell reference, so the referenced "
        "sheet name is not statically knowable. An extractor that tries to "
        "resolve it must degrade rather than fail.",
    ),
)

NO_CACHED_VALUES = FixtureExpectation(
    filename="no_cached_values.xlsx",
    axis="A tool-written workbook with no cached values: reconciliation must be disabled.",
    sheets=(
        SheetExpectation("Data", "data", header_row=1),
        SheetExpectation(
            "Calc", "calculation", has_formulas=True, header_row=1, operation_bounds=(5, 5)
        ),
        SheetExpectation(
            "Output", "output", has_formulas=True, header_row=1, operation_bounds=(4, 4)
        ),
    ),
    region_bounds=(7, 12),
    region_note="Five columns on Calc, four single cells on Output, which is nine.",
    operations=(
        OperationExpectation(
            anchor="Calc!A2", cell_count=200, ranges=("Calc!A2:A201",), reads=("Data!A2",)
        ),
        OperationExpectation(
            anchor="Calc!B2", cell_count=200, ranges=("Calc!B2:B201",), reads=("Data!C2",)
        ),
        OperationExpectation(
            anchor="Calc!C2",
            cell_count=200,
            ranges=("Calc!C2:C201",),
            pattern="arithmetic",
            reads=("B2", "Data!D2"),
        ),
        OperationExpectation(
            anchor="Calc!D2",
            cell_count=200,
            ranges=("Calc!D2:D201",),
            pattern="arithmetic",
            reads=("B2", "C2"),
        ),
        OperationExpectation(
            anchor="Calc!E2",
            cell_count=200,
            ranges=("Calc!E2:E201",),
            pattern="if_nested",
            reads=("D2",),
        ),
        OperationExpectation(
            anchor="Output!B5",
            cell_count=1,
            ranges=("Output!B5",),
            pattern="countifs",
            reads=("Calc!E2:E201",),
        ),
    ),
    # Nine unambiguous operations, so the whole DAG is derivable: C is
    # =ROUND(B*Data!D,2), D is =B+C, E bands D, and each Output cell totals one
    # Calc column. Calc!A is =Data!A and nothing reads it.
    dependency_edges=(
        ("Calc!B2", "Calc!C2"),
        ("Calc!B2", "Calc!D2"),
        ("Calc!B2", "Output!B2"),
        ("Calc!C2", "Calc!D2"),
        ("Calc!C2", "Output!B3"),
        ("Calc!D2", "Calc!E2"),
        ("Calc!D2", "Output!B4"),
        ("Calc!E2", "Output!B5"),
    ),
    dependency_edges_are_complete=True,
    findings=(
        FindingExpectation(
            kind="no_cached_values",
            minimum=1,
            note=(
                "All 1004 formula cells return None under data_only=True. PLAN "
                "section 1.5 and the risk table are explicit: report this loudly "
                "and degrade to 'not reconciled', never to 'passed' and never to "
                "zeros."
            ),
        ),
    ),
    # "Structurally an ordinary, sane workbook" is not quite true of the
    # findings, and the exception is worth stating rather than glossing:
    # =IF(D2>10000,"large",IF(D2>1000,"medium","small")) writes both banding
    # thresholds straight into the formula, and both clear the bar for a
    # constant worth reporting. Calc!A is =Data!A and nothing reads it, which
    # with the terminal Output metrics is the dead region.
    finding_kinds=(
        "dead_region",
        "document_attached_by_filename",
        "hardcoded_constant",
        "no_cached_values",
        "unsupported_format",
    ),
    cached_values_present=False,
    formula_cell_count=1004,
    notes=(
        "Structurally this is an ordinary, sane workbook. The only thing wrong "
        "with it is the missing cache, which is what makes it a clean test of "
        "that one behaviour.",
        "Contrast with clean_pipeline.xlsx, which has the same shape and does have cached values.",
    ),
)

FIXTURES: dict[str, FixtureExpectation] = {
    expectation.filename: expectation
    for expectation in (
        CLEAN_PIPELINE,
        POWERQUERY,
        LEGACY_SQL,
        CROSS_SHEET_CHAIN,
        MOSTLY_MANUAL,
        DOCUMENTED,
        HOSTILE,
        NO_CACHED_VALUES,
    )
}


def fixture_path(filename: str) -> Path:
    """Absolute path to a committed fixture."""
    return FIXTURE_DIR / filename


def iter_fixtures() -> tuple[FixtureExpectation, ...]:
    """Every fixture expectation, in corpus order."""
    return tuple(FIXTURES.values())


def fixtures_with_cached_values() -> tuple[FixtureExpectation, ...]:
    """Fixtures for which reconciliation has a positive path."""
    return tuple(f for f in FIXTURES.values() if f.cached_values_present)
