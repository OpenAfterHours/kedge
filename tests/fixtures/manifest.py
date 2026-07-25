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
* **Analyser-dependent** — sheet roles, logical region counts, findings. Nothing
  can check these until ``kedge.analysis`` exists; they are the contract that
  ``tests/corpus/`` will assert once it does.

Region counts are expressed as inclusive ``(minimum, maximum)`` bounds rather
than exact figures. The measured value from a straightforward R1C1 grouping is
recorded in ``region_note`` for each fixture; the bounds leave room for
reasonable differences in how the analyser treats single-cell regions and
whether it merges across blank rows.

Finding kinds are lower-case snake_case strings rather than an enum because
``kedge.analysis.model`` owns the real vocabulary and did not exist when this was
written. Treat the names here as *proposed*; if the analyser settles on different
ones, map them in the corpus tests and update ``FINDING_KINDS`` below.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent

__all__ = [
    "FINDING_KINDS",
    "FIXTURES",
    "FIXTURE_DIR",
    "SHEET_ROLES",
    "FindingExpectation",
    "FixtureExpectation",
    "SheetExpectation",
    "fixture_path",
    "fixtures_with_cached_values",
    "iter_fixtures",
]

# The five roles from PLAN section 2.4.
SHEET_ROLES = ("data", "calculation", "parameters", "documentation", "output")

# Proposed finding vocabulary. See the module docstring.
FINDING_KINDS = (
    "blank_columns",
    "blank_rows",
    "circular_reference",
    "dates_stored_as_text",
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
    "volatile_function",
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
class FixtureExpectation:
    """Ground truth for one fixture workbook."""

    filename: str
    axis: str
    """The single M1 axis this fixture exists to cover."""

    sheets: tuple[SheetExpectation, ...]
    region_bounds: tuple[int, int]
    """Inclusive (min, max) logical R1C1 regions across the whole workbook."""

    region_note: str = ""
    findings: tuple[FindingExpectation, ...] = ()
    cached_values_present: bool = False
    """True only where ``load_workbook(data_only=True)`` returns real numbers
    for formula cells. Reconciliation is possible only for these."""

    connection_count: int = 0
    power_query_names: tuple[str, ...] = ()
    named_ranges: tuple[str, ...] = ()
    external_links: tuple[str, ...] = ()
    unparseable_parts: tuple[str, ...] = ()
    """Parts the analyser is expected to report as unparseable rather than
    raising on. Empty for every fixture at present — see README.md."""

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


# =============================================================================
# The corpus
# =============================================================================

CLEAN_PIPELINE = FixtureExpectation(
    filename="clean_pipeline.xlsx",
    axis="The well-behaved case, and the R1C1 compression showcase.",
    sheets=(
        SheetExpectation("Data", "data"),
        SheetExpectation("Ref", "parameters", ("data",)),
        SheetExpectation("Params", "parameters"),
        SheetExpectation("Calc", "calculation", has_formulas=True),
        SheetExpectation("Output", "output", has_formulas=True),
    ),
    region_bounds=(12, 18),
    region_note=(
        "Measured 14: eight whole-column fills on Calc (4000 formula cells) plus "
        "six single-cell metrics on Output. The Calc figure is the headline claim "
        "— 4000 cells must compress to 8 logical operations."
    ),
    cached_values_present=True,
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
        SheetExpectation("Report", "output", ("calculation", "data"), has_formulas=True),
        SheetExpectation("Staging", "data"),
        SheetExpectation("HaircutTable", "parameters", ("data",)),
    ),
    region_bounds=(2, 4),
    region_note="Measured 2: a share column and a total on Report.",
    power_query_names=("Exposures", "CollateralHaircuts", "NetExposure"),
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
        SheetExpectation("Extract", "data"),
        SheetExpectation("Summary", "output", ("calculation",), has_formulas=True),
    ),
    region_bounds=(2, 4),
    region_note="Measured 2: SUMIF and COUNTIF columns on Summary.",
    connection_count=2,
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
        SheetExpectation("A_Source", "data"),
        SheetExpectation("B_Enrich", "calculation", has_formulas=True),
        SheetExpectation("C_Aggregate", "calculation", has_formulas=True),
        SheetExpectation("D_Report", "output", has_formulas=True),
    ),
    region_bounds=(14, 22),
    region_note=(
        "Measured 17: six on B_Enrich (the running total splits at its seed row), "
        "three on C_Aggregate, eight single cells on D_Report."
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
        SheetExpectation("Input", "data"),
        SheetExpectation("Overrides", "data", ("parameters",)),
        SheetExpectation("Calc", "calculation", has_formulas=True),
        SheetExpectation("Output", "output", has_formulas=True),
    ),
    region_bounds=(6, 12),
    region_note="Measured 8: four columns on Calc, four single cells on Output.",
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
        SheetExpectation("Data", "data"),
        SheetExpectation("Calc", "calculation", has_formulas=True),
        SheetExpectation("Ref", "parameters", ("data",)),
    ),
    region_bounds=(3, 6),
    region_note="Measured 4: four whole-column fills on Calc over 80 rows.",
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
        SheetExpectation("Messy", "calculation", ("data",), has_formulas=True),
        SheetExpectation("Volatile", "calculation", has_formulas=True),
        SheetExpectation("Circular", "calculation", has_formulas=True),
        SheetExpectation("Broken", "calculation", has_formulas=True),
        SheetExpectation("_Archive", "data", hidden=True),
    ),
    region_bounds=(20, 34),
    region_note=(
        "Measured 27, but this is the fixture where the figure is least "
        "meaningful: blank rows and the row-47 break fragment the Messy columns "
        "on purpose. Assert the bounds loosely and the specific traps precisely."
    ),
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
    ),
    external_links=("file:///Z:/retired/vendor_rates_2019.xlsx",),
    named_ranges=("obsolete_rate_table", "messy_amounts"),
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
        SheetExpectation("Data", "data"),
        SheetExpectation("Calc", "calculation", has_formulas=True),
        SheetExpectation("Output", "output", has_formulas=True),
    ),
    region_bounds=(7, 12),
    region_note="Measured 9: five columns on Calc, four single cells on Output.",
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
