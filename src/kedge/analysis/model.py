"""The `WorkbookAnalysis` contract — the single structural description of a workbook.

Everything downstream binds to this module: the planner reasons over it, the scaffolder
walks it, the agent's `inspect_workbook` tool discloses it progressively, and the
reconciler uses its cached-value coverage to decide whether reconciliation is even
possible. Changing a field here is a cross-cutting change.

Two design rules run through the whole module:

- **Absent is a value, not an exception.** Every extractor reports an
  :class:`ExtractionStatus`. A workbook with no ``connections.xml`` is normal; a workbook
  whose DataMashup will not decode is a :class:`Finding`, not a traceback. The analyser
  must always return a complete ``WorkbookAnalysis``.
- **Profiles travel, raw data does not.** Column profiles and a handful of bounded sample
  rows are serialised here. Bulk cell values stay behind the workbook handle and are read
  on demand through capped tools, so the artifact never becomes hundreds of megabytes.

References:
- PLAN.md §1.5 (what is extractable), §2.1 (R1C1 compression), §2.3 (profiles vs values),
  §2.4 (sheet roles), §2.7 (formula patterns), §M1 (deliverables), §4.5 (reconciliation).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0"

__all__ = [
    "SCHEMA_VERSION",
    "CachedValueCoverage",
    "ColumnProfile",
    "Connection",
    "DependencyGraph",
    "ExcelPattern",
    "ExtractionStatus",
    "Finding",
    "FindingKind",
    "Inconsistency",
    "LogicalOperation",
    "NamedRange",
    "NumericStats",
    "Orientation",
    "PowerQuery",
    "PowerQueryExtraction",
    "ProcessNote",
    "Reference",
    "Severity",
    "SheetInfo",
    "SheetRole",
    "WorkbookAnalysis",
    "WorkbookIdentity",
]


class _Frozen(BaseModel):
    """Base for every model here: immutable, and rejects unknown keys on load."""

    model_config = ConfigDict(frozen=True, extra="forbid")


# =============================================================================
# ENUMS
# =============================================================================


class ExtractionStatus(StrEnum):
    """Outcome of one extractor. `absent` is a normal, expected result."""

    OK = "ok"
    ABSENT = "absent"
    UNPARSEABLE = "unparseable"
    SKIPPED = "skipped"


class Severity(StrEnum):
    """How much a finding should worry the reader."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class SheetRole(StrEnum):
    """Sheet classification. See PLAN §2.4 for the heuristic signals."""

    DATA = "data"
    CALCULATION = "calculation"
    PARAMETERS = "parameters"
    DOCUMENTATION = "documentation"
    OUTPUT = "output"
    UNKNOWN = "unknown"


class FindingKind(StrEnum):
    """The catalogue of things worth telling the user about.

    This list is the "extra control" pitch made concrete (PLAN §M1) and is expected to
    grow. Adding a member is additive; renaming one is a contract change.

    Every member but one describes something found *in* the workbook.
    `DOCUMENT_ATTACHED_BY_FILENAME` describes a decision kedge made about it: a sidecar
    document was attached because its filename looked like a process document, not because
    it shares the workbook's stem, so the association is a guess. It belongs here for the
    same reason as the rest — the user is the only one who can tell whether the guess was
    right, and a note silently attributed to the wrong workbook is worse than no note.
    """

    CIRCULAR_REFERENCE = "circular_reference"
    VOLATILE_FUNCTION = "volatile_function"
    HARDCODED_CONSTANT = "hardcoded_constant"
    IFERROR_SWALLOW = "iferror_swallow"
    INCONSISTENT_FORMULA = "inconsistent_formula"
    EXTERNAL_LINK = "external_link"
    UNRESOLVABLE_EXTERNAL_LINK = "unresolvable_external_link"
    HIDDEN_SHEET = "hidden_sheet"
    HIDDEN_COLUMN = "hidden_column"
    ERROR_VALUE = "error_value"
    BROKEN_NAMED_RANGE = "broken_named_range"
    MISSING_CACHED_VALUES = "missing_cached_values"
    PARTIAL_CACHED_VALUES = "partial_cached_values"
    UNPARSEABLE_PART = "unparseable_part"
    MERGED_CELLS = "merged_cells"
    DUPLICATE_HEADER = "duplicate_header"
    TEXT_FORMATTED_NUMBER = "text_formatted_number"
    MIXED_DATE_FORMAT = "mixed_date_format"
    EMBEDDED_TOTALS_ROW = "embedded_totals_row"
    PREAMBLE_ROWS = "preamble_rows"
    MANUAL_OVERRIDE_BLOCK = "manual_override_block"
    MACRO_PRESENT = "macro_present"
    UNSUPPORTED_FORMAT = "unsupported_format"
    ITERATIVE_CALCULATION = "iterative_calculation"
    DEAD_REGION = "dead_region"
    DOCUMENT_ATTACHED_BY_FILENAME = "document_attached_by_filename"


class ExcelPattern(StrEnum):
    """Recognised formula shapes from the PLAN §2.7 translation vocabulary.

    `UNKNOWN` is a legitimate and important answer — it is the signal that the planner
    should raise an `open_question` rather than improvise a translation.
    """

    ARITHMETIC = "arithmetic"
    IF_SIMPLE = "if_simple"
    IF_NESTED = "if_nested"
    IFERROR = "iferror"
    SUMIF = "sumif"
    SUMIFS = "sumifs"
    COUNTIFS = "countifs"
    VLOOKUP_EXACT = "vlookup_exact"
    VLOOKUP_APPROX = "vlookup_approx"
    INDEX_MATCH = "index_match"
    SUMPRODUCT = "sumproduct"
    RUNNING_TOTAL = "running_total"
    PRIOR_ROW = "prior_row"
    PARAMETER_REF = "parameter_ref"
    AGGREGATE = "aggregate"
    TEXT_MANIPULATION = "text_manipulation"
    DATE_ARITHMETIC = "date_arithmetic"
    PIVOT = "pivot"
    SUBTOTAL = "subtotal"
    LITERAL = "literal"
    UNKNOWN = "unknown"


# =============================================================================
# FINDINGS
# =============================================================================


class Finding(_Frozen):
    """One thing the analyser noticed that a reviewer should know about."""

    kind: FindingKind
    severity: Severity
    message: str = Field(description="Plain-English, states what was found and where.")
    sheet: str | None = None
    location: str | None = Field(default=None, description="A1 range or part path.")
    detail: str | None = None
    affected_cells: int | None = None
    remediation: str | None = Field(
        default=None, description="What the user or the planner should do about it."
    )


# =============================================================================
# FORMULAS AND LOGICAL OPERATIONS
# =============================================================================


class Reference(_Frozen):
    """A single cell or range reference pulled out of a formula."""

    raw: str = Field(description="Verbatim token, e.g. 'Data!$B$2:B100'.")
    sheet: str | None = Field(default=None, description="None means same-sheet.")
    a1: str = Field(description="The reference with any sheet qualifier stripped.")
    is_range: bool = False
    absolute_row: bool = False
    absolute_col: bool = False
    is_external: bool = False
    external_workbook: str | None = None
    resolves: bool = True


class Inconsistency(_Frozen):
    """A cell that breaks the R1C1 uniformity of its region.

    This is the classic "someone typed over row 47" bug and is usually the single most
    valuable thing the analyser finds.
    """

    cell: str
    expected_r1c1: str
    actual_r1c1: str | None = Field(default=None, description="None if the cell is a literal.")
    actual_value: Any | None = None
    note: str | None = None


Orientation = Literal["column", "row", "block", "single"]
"""The shape of a compressed formula region.

An alias rather than the literal spelled out at the field, because it is spelled out in two
places -- here and in :func:`kedge.analysis.regions._orientation`, which produces it -- and two
copies of a closed vocabulary drift. They had already: the producer was annotated `str`, which
is wider than the field accepts, and the mypy suppression at the call site was never doing
anything (see docs/ty-diagnostics.md).
"""


class LogicalOperation(_Frozen):
    """One R1C1-compressed formula region: a single logical operation.

    A contiguous block sharing an identical R1C1 string is one operation regardless of
    whether it spans 5 rows or 200,000. This collapsed set is what the process plan is
    written against (PLAN §2.1).
    """

    id: str = Field(description="Stable slug, e.g. 'calc_h2_h50000'.")
    sheet: str
    anchor: str = Field(description="Top-left cell of the region, e.g. 'H2'.")
    ranges: list[str] = Field(description="Source ranges, sheet-qualified.")
    cell_count: int
    orientation: Orientation = "column"

    r1c1: str = Field(description="The normalised relative formula that defines the region.")
    sample_a1: str = Field(description="The A1 formula at the anchor, for humans to read.")
    functions: list[str] = Field(default_factory=list, description="Function inventory.")
    references: list[Reference] = Field(default_factory=list)

    excel_pattern: ExcelPattern = ExcelPattern.UNKNOWN
    description: str = Field(description="Plain-English description of the formula shape.")
    translation_hint: str | None = Field(
        default=None, description="Suggested polars shape from the PLAN §2.7 vocabulary."
    )

    inconsistencies: list[Inconsistency] = Field(default_factory=list)
    is_volatile: bool = False
    has_external_refs: bool = False
    cached_values_present: bool = Field(
        default=False, description="Whether this region can serve as a reconciliation baseline."
    )
    downstream_ref_count: int = Field(
        default=0, description="Fan-out. Zero on an output sheet is fine; zero elsewhere is dead."
    )


# =============================================================================
# DEPENDENCY GRAPH
# =============================================================================


class DependencyGraph(_Frozen):
    """Dependencies between logical operations, plus cell-level cycle detection.

    Edges are region-to-region because that is the granularity the plan is written at.
    Cycles are reported at cell level because that is where Excel actually forms them.
    """

    nodes: list[str] = Field(default_factory=list, description="LogicalOperation ids.")
    edges: list[tuple[str, str]] = Field(
        default_factory=list, description="(upstream_id, downstream_id) pairs."
    )
    cycles: list[list[str]] = Field(
        default_factory=list, description="Cell-level cycles, each a list of A1 refs."
    )
    fan_out: dict[str, int] = Field(default_factory=dict)
    topological_order: list[str] | None = Field(
        default=None, description="None when the graph contains a cycle."
    )
    roots: list[str] = Field(default_factory=list)
    leaves: list[str] = Field(default_factory=list)


# =============================================================================
# EXTERNAL DATA
# =============================================================================


class Connection(_Frozen):
    """A legacy external data connection from `xl/connections.xml`.

    Connection strings routinely carry credentials, so the raw string is never retained;
    only a redacted rendering and the parsed provider survive.
    """

    name: str
    kind: Literal["odbc", "oledb", "web", "text", "worksheet", "other"] = "other"
    command: str | None = Field(default=None, description="The SQL or query text.")
    command_type: str | None = None
    provider: str | None = None
    connection_string_redacted: str | None = None
    refresh_on_load: bool = False
    description: str | None = None


class PowerQuery(_Frozen):
    """One M query recovered from the DataMashup part."""

    name: str
    m_source: str
    referenced_queries: list[str] = Field(default_factory=list)
    load_target: str | None = None


class PowerQueryExtraction(_Frozen):
    """The result of hunting for a DataMashup part.

    `ABSENT` is the common case and carries no findings. `UNPARSEABLE` means the part was
    located but would not decode, which is a `Finding` (PLAN §M1).
    """

    status: ExtractionStatus = ExtractionStatus.ABSENT
    part_path: str | None = Field(default=None, description="e.g. 'customXml/item3.xml'.")
    queries: list[PowerQuery] = Field(default_factory=list)
    section_m_bytes: int | None = None
    detail: str | None = None


class NamedRange(_Frozen):
    """A defined name, workbook- or sheet-scoped."""

    name: str
    refers_to: str
    scope: str | None = Field(default=None, description="Sheet name, or None for workbook scope.")
    is_broken: bool = False
    is_hidden: bool = False
    reference_count: int = 0


class ProcessNote(_Frozen):
    """Prose describing the process, recovered from a sheet, a comment, or a sibling document.

    `source` names what the prose had to be read out of, and it is user-facing: the report
    renders it into the notes table and the planner sends it to the model. So `markdown` and
    `plain_text` are two members rather than one. They differ in exactly the way the rest of
    this field distinguishes its members — by what kedge could recover — since a markdown file
    carries headings that can fill `heading`, and a `.txt` carries none, which makes an absent
    `heading` mean "the file had none to give" in one case and "this part had none" in the
    other. The distinction also warns the model that `#` and `-` in a markdown note are
    structure rather than prose.
    """

    source: Literal["sheet", "cell_comment", "docx", "doc_stub", "markdown", "plain_text"]
    origin: str = Field(description="Sheet name or file path.")
    location: str | None = None
    text: str
    heading: str | None = None


# =============================================================================
# PROFILES AND CACHED VALUES
# =============================================================================


class NumericStats(_Frozen):
    """Summary statistics for a numeric column."""

    min: float | None = None
    max: float | None = None
    mean: float | None = None
    sum: float | None = None
    zero_count: int = 0
    negative_count: int = 0


class ColumnProfile(_Frozen):
    """What the model sees about a column by default (PLAN §2.3).

    Bounded sample values are included deliberately: a model that can see a handful of
    real values writes better code than one guessing at them. Bulk reads go through the
    capped tools instead. When redaction is enabled for this column, the sample lists are
    empty and `redacted` is true — dtype and null count still travel.
    """

    sheet: str
    column: str = Field(description="Column letter, e.g. 'H'.")
    index: int
    header: str | None = None
    dtype: str
    row_count: int
    null_count: int
    distinct_count: int | None = None
    numeric: NumericStats | None = None
    top_k: list[tuple[Any, int]] = Field(
        default_factory=list, description="(value, frequency), most frequent first."
    )
    head: list[Any] = Field(default_factory=list)
    tail: list[Any] = Field(default_factory=list)
    sample: list[Any] = Field(default_factory=list, description="Random rows, seeded.")
    redacted: bool = False
    format_anomalies: list[str] = Field(
        default_factory=list, description="e.g. 'numbers stored as text', 'mixed date formats'."
    )


class CachedValueCoverage(_Frozen):
    """Whether the workbook carries the values Excel last calculated.

    openpyxl never calculates anything, so a workbook written by a tool rather than saved
    by Excel returns `None` for every formula cell. That disables reconciliation entirely
    and must be reported loudly — never silently treated as zeros (PLAN §1.5, §4.5).
    """

    formula_cell_count: int = 0
    cached_present_count: int = 0
    coverage: float = Field(default=0.0, ge=0.0, le=1.0)
    status: Literal["present", "partial", "absent", "not_applicable"] = "absent"

    @property
    def reconcilable(self) -> bool:
        """True when there is enough of a baseline to reconcile against."""
        return self.status in ("present", "partial") and self.cached_present_count > 0


# =============================================================================
# SHEETS AND WORKBOOK
# =============================================================================


class SheetInfo(_Frozen):
    """Structural facts about one worksheet, plus its classified role."""

    name: str
    index: int
    role: SheetRole = SheetRole.UNKNOWN
    role_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    role_signals: list[str] = Field(
        default_factory=list, description="Why the classifier chose that role."
    )

    max_row: int = 0
    max_column: int = 0
    used_range: str | None = None
    formula_cell_count: int = 0
    value_cell_count: int = 0
    blank_cell_count: int = 0
    text_cell_count: int = 0

    is_hidden: bool = False
    is_very_hidden: bool = False
    hidden_columns: list[str] = Field(default_factory=list)
    merged_ranges: list[str] = Field(default_factory=list)

    header_row: int | None = Field(default=None, description="None when no header was found.")
    preamble_rows: int = 0
    has_embedded_totals: bool = False
    table_ranges: list[str] = Field(default_factory=list)
    operation_ids: list[str] = Field(default_factory=list)


class WorkbookIdentity(_Frozen):
    """Which file this analysis describes, and enough to prove it later."""

    path: str
    filename: str
    sha256: str
    size_bytes: int
    file_format: Literal["xlsx", "xlsm", "xlsb", "xls", "unknown"] = "xlsx"
    modified_at: datetime | None = None
    created_by: str | None = Field(default=None, description="The application that wrote it.")
    has_vba: bool = False
    iterative_calculation: bool = False


class WorkbookAnalysis(_Frozen):
    """The complete structural description of one workbook.

    This is always returned complete. An extractor that fails contributes an
    `ExtractionStatus` and a `Finding`; it never prevents the analysis from being
    produced (PLAN §M1).
    """

    schema_version: str = SCHEMA_VERSION
    kedge_version: str
    generated_at: datetime
    workbook: WorkbookIdentity

    sheets: list[SheetInfo] = Field(default_factory=list)
    operations: list[LogicalOperation] = Field(default_factory=list)
    graph: DependencyGraph = Field(default_factory=DependencyGraph)

    connections: list[Connection] = Field(default_factory=list)
    connections_status: ExtractionStatus = ExtractionStatus.ABSENT
    power_query: PowerQueryExtraction = Field(default_factory=PowerQueryExtraction)
    named_ranges: list[NamedRange] = Field(default_factory=list)
    notes: list[ProcessNote] = Field(default_factory=list)

    profiles: list[ColumnProfile] = Field(default_factory=list)
    cached_values: CachedValueCoverage = Field(default_factory=CachedValueCoverage)

    findings: list[Finding] = Field(default_factory=list)
    analysis_duration_seconds: float | None = None

    # ── convenience accessors ────────────────────────────────────────────

    def sheet(self, name: str) -> SheetInfo | None:
        """Return the sheet with this name, or None."""
        return next((s for s in self.sheets if s.name == name), None)

    def operation(self, op_id: str) -> LogicalOperation | None:
        """Return the logical operation with this id, or None."""
        return next((o for o in self.operations if o.id == op_id), None)

    def findings_of(self, *kinds: FindingKind) -> list[Finding]:
        """Return findings matching any of the given kinds."""
        wanted = set(kinds)
        return [f for f in self.findings if f.kind in wanted]

    @property
    def errors(self) -> list[Finding]:
        """Findings severe enough to block or seriously qualify a conversion."""
        return [f for f in self.findings if f.severity is Severity.ERROR]

    @property
    def is_reconcilable(self) -> bool:
        """Whether the workbook carries a usable reconciliation baseline."""
        return self.cached_values.reconcilable

    def summary(self) -> dict[str, Any]:
        """A compact digest for the model's default context (PLAN §M4).

        Deliberately excludes formula bodies, references and sample values — those come
        back through `inspect_workbook` on demand. Progressive disclosure, not a dump.

        Every string here is at full fidelity, the workbook-authored ones — the filename, the
        sheet names — included, and bounding them is the business of whatever renders this rather
        than of this method. Two consumers, two different needs: `kedge.plan.propose` seeds a plan
        from this dict and that plan is written to disk, so a name clipped at the source would be
        clipped in the artifact; `kedge.agent.context.build_analysis_block` pins it into a system
        message and bounds its own copy there. Clipping here would corrupt the first to serve the
        second.
        """
        return {
            "workbook": self.workbook.filename,
            "format": self.workbook.file_format,
            "has_vba": self.workbook.has_vba,
            "sheets": [
                {
                    "name": s.name,
                    "role": s.role.value,
                    "rows": s.max_row,
                    "columns": s.max_column,
                    "formula_cells": s.formula_cell_count,
                    "hidden": s.is_hidden or s.is_very_hidden,
                    "operations": len(s.operation_ids),
                }
                for s in self.sheets
            ],
            "operation_count": len(self.operations),
            "operations_by_pattern": _count_by([o.excel_pattern.value for o in self.operations]),
            "has_cycles": bool(self.graph.cycles),
            "connection_count": len(self.connections),
            "power_query_status": self.power_query.status.value,
            "power_query_count": len(self.power_query.queries),
            "named_range_count": len(self.named_ranges),
            "note_count": len(self.notes),
            "profiled_columns": len(self.profiles),
            "reconcilable": self.is_reconcilable,
            "cached_value_coverage": round(self.cached_values.coverage, 3),
            "findings_by_severity": _count_by([f.severity.value for f in self.findings]),
            "findings_by_kind": _count_by([f.kind.value for f in self.findings]),
        }


def _count_by(values: list[str]) -> dict[str, int]:
    """Tally a list of strings into a count map, ordered by descending count."""
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
