"""One real conversion's plan, and the facts it was written against.

On 2026-08-23 a model was asked, through the hub, to convert
``evals/adjustment_signoff/q2_accrual_adjustment.xlsx``. It proposed the plan below, a user
approved it, and the notebook it scaffolded opened, ran, and was wrong in four independent ways:
it hands nothing over on a workbook with a recorded ODBC connection and a column of generated
``UPDATE``s; its only checkpoint sits *after* the update rather than before it; it carries no
briefing on a workbook whose Sign-off tab explains itself in eight cited sections; and it declares
the re-extract on the checkpoint that gates it, where the scaffolder never looks for a hand-in.
:func:`kedge.plan.review.review_warnings` raised one warning on it, about open questions, and the
plan was approved.

**This is a fixture because the artifact is not.** The project directory it came from
(``evals/adjustment_signoff/q2_accrual_adjustment.kedge/``) is gitignored -- ``*.kedge/`` -- so it
is a live working directory that any re-run overwrites and any clean checkout lacks. A regression
test cannot depend on it. It is copied here instead of invented because a plan a model actually
produced has defects nobody would think to write: the mistyped ``kind`` on the re-extract is not a
mistake a test author makes, and it is the one that cost three cells and a silent input.

Two halves, and they are copied to different standards:

- :data:`OBSERVED_PLAN_YAML` is **verbatim**, byte for byte, including the curly apostrophes the
  model wrote. It is the thing under test and paraphrasing it would defeat the point.
- :func:`observed_analysis` is **transcribed and trimmed**, which is a weaker claim and worth
  stating plainly. The full ``analysis.json`` is 50KB, most of it column profiles no warning
  reads. What is kept is everything the review pass touches -- the connection and its command,
  the eight process notes with their sheets and cells, all seven formula regions including the
  one that concatenates SQL -- plus enough of the sheets and findings that
  :func:`kedge.plan.triage.complexity` scores it what it scored the whole file. ``role_signals``,
  ``profiles`` and the findings' ``detail`` prose are dropped.

  A test asserts that complexity score, and it is worth knowing exactly how much that proves:
  complexity reads five numbers -- operation count, sheet count, cross-sheet reach, pattern
  variety, finding count -- so it catches a section going missing and catches nothing inside one.
  A transcription slip in a field it does not read is invisible to it, and one got through: this
  file claimed VLOOKUP column 4 for ``adjustment_d17_d92`` where the artifact says 5, and every
  test stayed green. The values that carry a warning -- the notes' sheets and locations, the
  connection command, the ``G17:G92`` formula -- are the ones to check against
  ``analysis.json`` if the live artifact is ever regenerated.

:func:`corrected_plan` is the other side: a plan for the same workbook with the same four
defects put right. It exists so "does this fire on a correct plan?" can be asked without a unit
test reading a file out of ``evals/``, which would be I/O outside ``tests/fixtures`` and a
coupling from ``tests/unit`` to a directory it has no business knowing about. It is written to
the shape of ``evals/adjustment_signoff/plan.yaml`` rather than copied from it -- the shape is
what is under test, and a second copy of that file would rot against the first.
"""

from __future__ import annotations

from datetime import UTC, datetime

from kedge.analysis.model import (
    CachedValueCoverage,
    Connection,
    DependencyGraph,
    ExcelPattern,
    Finding,
    FindingKind,
    LogicalOperation,
    NamedRange,
    ProcessNote,
    Reference,
    Severity,
    SheetInfo,
    SheetRole,
    WorkbookAnalysis,
    WorkbookIdentity,
)
from kedge.plan.model import (
    Assessment,
    Briefing,
    Confidence,
    Handoff,
    OpenQuestion,
    PlanDraft,
    ProcessPlan,
    SourceOrigin,
    Stage,
    StageKind,
    StageSource,
)
from kedge.plan.store import plan_from_yaml

WORKBOOK_SHA256 = "8f3cee46fd4774314e2220026106eaa5a72e4bfc5e4fe00e2bc9ab52014eeccc"

OBSERVED_COMPLEXITY = 0.368
"""What :func:`kedge.plan.triage.complexity` scored the untrimmed ``analysis.json``, transcribed
here so the trim has something to be wrong against: complexity reads operation count, sheet
count, cross-sheet reach, pattern variety and finding count, and every one of those had to
survive. The approval card and the plan's own rationale round it to 0.37."""

OBSERVED_PLAN_YAML = """\
# kedge process plan. Edit freely: this file is a review artifact, not generated code.
# Nothing reaches the notebook until approval.state is 'approved' (PLAN 2.2).
plan_schema_version: '1.1'
version: 1
created_at: '2026-08-23T08:36:33.516739Z'
workbook: q2_accrual_adjustment.xlsx
workbook_sha256: 8f3cee46fd4774314e2220026106eaa5a72e4bfc5e4fe00e2bc9ab52014eeccc
analysis_schema_version: '1.0'
kedge_version: 0.0.11
generated_by: llm
llm_model: gpt-5.6-terra
assessment:
  convertible: 1.0
  blockers: []
  rationale: 'kedge triage: proceed, complexity 0.37. Scored deterministically from the analysis, not
    estimated.'
stages:
- id: document_extract
  intent: Show the recorded FinanceWarehouse extraction query and the expected seven-column pre-/post-adjustment
    export contract.
  kind: load
  sources:
  - origin: query
    ref: AccrualExtract connection command
  - origin: range
    ref: Pre-Adjustment!A18:G138 and Post-Adjustment!A18:G138
  depends_on: []
  confidence: high
  assumptions:
  - The recorded ODBC query is the query the operator must run outside the notebook; the notebook will
    not connect to the warehouse.
  excel_pattern: unknown
  operations: []
- id: load_pre_adjustment
  intent: Load the operator-provided pre-adjustment export and enforce the expected columns and types.
  kind: load
  sources:
  - origin: handin
    ref: Pre-adjustment accrual extract
  depends_on:
  - document_extract
  confidence: high
  assumptions:
  - The export has one row per trade_id and is taken for 30 June 2026, statutory ledger.
  excel_pattern: unknown
  operations: []
- id: select_adjustment_scope
  intent: Restrict the extract to the workbook's explicit 76-trade adjustment roster for entities E-04,
    E-07, E-09 and E-12, retaining the unresolved null accrual as nil for calculation.
  kind: transform
  sources:
  - origin: range
    ref: Adjustment!A17:B92
  - origin: stage
    ref: load_pre_adjustment
  depends_on:
  - load_pre_adjustment
  confidence: high
  assumptions:
  - The explicit trade roster, rather than entity/status criteria alone, is the authoritative scope; this
    preserves the workbook's exclusion of cancelled trades.
  excel_pattern: unknown
  operations:
  - adjustment_c17_c92
  - adjustment_d17_d92
- id: apply_uplift
  intent: Calculate per-trade uplift and post-adjustment accrual using the named 4.5% rate with Excel-compatible
    rounding, and produce review totals.
  kind: transform
  sources:
  - origin: range
    ref: Adjustment!B4 and D94:F95
  - origin: stage
    ref: select_adjustment_scope
  depends_on:
  - select_adjustment_scope
  confidence: high
  assumptions:
  - A missing source accrual is treated as zero, as evidenced by ACC-00001 in the workbook.
  excel_pattern: unknown
  operations:
  - adjustment_e17_e92
  - adjustment_f17_f92
  - adjustment_d94_f94
  - adjustment_d95
- id: render_update_script
  intent: Render one safe SQL UPDATE per scoped trade for operator review and require confirmation that
    it was run externally.
  kind: output
  sources:
  - origin: stage
    ref: apply_uplift
  depends_on:
  - apply_uplift
  confidence: high
  assumptions:
  - Per-trade statements are the intended operational hand-off even though the workbook also contains
    a broader set-based update text.
  excel_pattern: unknown
  operations:
  - adjustment_g17_g92
- id: verify_post_adjustment
  intent: Accept the post-adjustment re-extract only after update confirmation and compare predicted versus
    returned accruals by trade_id, distinguishing missing, unexpected and mismatched rows.
  kind: checkpoint
  sources:
  - origin: handin
    ref: Post-adjustment accrual extract
  - origin: stage
    ref: render_update_script
  depends_on:
  - render_update_script
  confidence: high
  assumptions:
  - The re-extract is run using the same recorded query after the update.
  excel_pattern: unknown
  operations: []
- id: prepare_sign_off
  intent: Present reconciled impact totals and an auditable sign-off record derived from the completed
    run, replacing the workbook’s incorrect after-total with the verified post-adjustment result.
  kind: output
  sources:
  - origin: stage
    ref: verify_post_adjustment
  - origin: range
    ref: Sign-off!A19:D28
  depends_on:
  - verify_post_adjustment
  confidence: high
  assumptions:
  - 'The approved scope is four entities: E-04, E-07, E-09 and E-12. The Sign-off sheet’s after-total
    is incorrect and will not be used as a reconciliation baseline; the verified post-adjustment re-extract
    is authoritative.'
  excel_pattern: unknown
  operations: []
open_questions: []
dropped: []
summary: 'Translate the Q2 statutory accrual uplift into a staged marimo runbook: document the warehouse
  extract, ingest the pre-adjustment hand-in, derive the 4.5% uplift for the explicit four-entity trade
  roster, render safe update statements, and verify the re-extract row by row before producing a corrected
  sign-off summary.'
approval:
  state: approved
  by: user
  at: '2026-08-23T08:36:57.083174Z'
"""  # noqa: RUF001 -- the model wrote curly apostrophes; verbatim means verbatim

_GENERATED_UPDATE_R1C1 = (
    '="UPDATE fin.accruals SET accrual_gbp = "&TEXT(RC[-1],"0.00")'
    '&", adjustment_note = \'Q2 uplift for "&RC[-4]&"\'"&" WHERE trade_id = \'"&RC[-6]&"\';"'
)
"""``Adjustment!G17:G92``. The step that changes the data, typed ``text_manipulation`` because
that is what it is, reported as a dead region because nothing in the workbook reads it, and read
by a person with a clipboard."""

_GENERATED_UPDATE_A1 = (
    '="UPDATE fin.accruals SET accrual_gbp = "&TEXT(F17,"0.00")'
    '&", adjustment_note = \'Q2 uplift for "&C17&"\'"&" WHERE trade_id = \'"&A17&"\';"'
)

_EXTRACT_QUERY = (
    "SELECT\n"
    "    a.trade_id,\n"
    "    a.entity_id,\n"
    "    e.entity_name,\n"
    "    a.cost_centre,\n"
    "    a.accrual_gbp,\n"
    "    a.period_end,\n"
    "    a.status\n"
    "FROM fin.accruals AS a\n"
    "INNER JOIN fin.entities AS e\n"
    "    ON e.entity_id = a.entity_id\n"
    "WHERE a.period_end = '2026-06-30'\n"
    "  AND a.ledger = 'STATUTORY'\n"
    "ORDER BY a.entity_id, a.trade_id"
)


def _lookup(
    op_id: str, anchor: str, column: str, offset: int, taken: int, fan_out: int
) -> LogicalOperation:
    """One of the two VLOOKUPs into the pre-adjustment tab, which is what makes this cross-sheet.

    ``offset`` is how far left the key sits; ``taken`` is the column of the lookup table returned.
    They are separate arguments because in the real workbook they are separate numbers, and
    deriving one from the other is how this fixture came to claim column 4 where the artifact
    says 5.
    """
    return LogicalOperation(
        id=op_id,
        sheet="Adjustment",
        anchor=anchor,
        ranges=[f"Adjustment!{column}17:{column}92"],
        cell_count=76,
        r1c1=f"=VLOOKUP(RC[-{offset}],'Pre-Adjustment'!R19C1:R138C7,{taken},FALSE)",
        sample_a1=f"=VLOOKUP(A17,'Pre-Adjustment'!$A$19:$G$138,{taken},FALSE)",
        functions=["VLOOKUP"],
        references=[
            Reference(raw="A17", a1="A17"),
            Reference(
                raw="'Pre-Adjustment'!$A$19:$G$138",
                sheet="Pre-Adjustment",
                a1="$A$19:$G$138",
                is_range=True,
            ),
        ],
        excel_pattern=ExcelPattern.VLOOKUP_EXACT,
        description=(
            "An exact-match lookup into a reference table on Adjustment, filled down 76 rows, "
            "reading from Pre-Adjustment."
        ),
        cached_values_present=True,
        downstream_ref_count=fan_out,
    )


def _rounded(op_id: str, anchor: str, column: str, r1c1: str, fan_out: int) -> LogicalOperation:
    """The uplift and the post-adjustment accrual: ROUND over the looked-up value."""
    return LogicalOperation(
        id=op_id,
        sheet="Adjustment",
        anchor=anchor,
        ranges=[f"Adjustment!{column}17:{column}92"],
        cell_count=76,
        r1c1=r1c1,
        sample_a1=r1c1.replace("RC[-1]", "D17").replace("RC[-2]", "D17"),
        functions=["ROUND"],
        references=[Reference(raw="D17", a1="D17")],
        excel_pattern=ExcelPattern.ARITHMETIC,
        description="Arithmetic over neighbouring cells on Adjustment, filled down 76 rows.",
        cached_values_present=True,
        downstream_ref_count=fan_out,
    )


def _aggregate(
    op_id: str, anchor: str, ranges: list[str], cells: int, r1c1: str, a1: str
) -> LogicalOperation:
    """The two summary cells under the adjustment table."""
    return LogicalOperation(
        id=op_id,
        sheet="Adjustment",
        anchor=anchor,
        ranges=ranges,
        cell_count=cells,
        r1c1=r1c1,
        sample_a1=a1,
        functions=[a1[1 : a1.index("(")]],
        references=[Reference(raw="D17:D92", a1="D17:D92", is_range=True)],
        excel_pattern=ExcelPattern.AGGREGATE,
        description=f"An aggregate ({a1[1 : a1.index('(')]}) over a range on Adjustment.",
        cached_values_present=True,
    )


_OPERATION_IDS = [
    "adjustment_c17_c92",
    "adjustment_d17_d92",
    "adjustment_e17_e92",
    "adjustment_f17_f92",
    "adjustment_g17_g92",
    "adjustment_d94_f94",
    "adjustment_d95",
]


def corrected_plan() -> ProcessPlan:
    """The same process, planned correctly. Nothing added for the observed run fires on it.

    Written to the shape of the reference conversion: the extract query and the UPDATE are
    hand-offs rather than ``output`` stages, the checkpoint sits *above* the write, each hand-in
    is declared on the stage that reads it, and the briefing carries the Sign-off tab's own words
    with citations. Every one of the five checks has something here it could fire on and must not.
    """
    return ProcessPlan.from_draft(
        PlanDraft(
            assessment=Assessment(convertible=0.85),
            stages=[
                Stage(
                    id="extract_query",
                    intent="Hand the user the query that produces the pre-adjustment position",
                    kind=StageKind.HANDOFF,
                    sources=[StageSource(origin=SourceOrigin.QUERY, ref="AccrualExtract")],
                    handoff=Handoff(
                        instruction="Run this against FinanceWarehouse and bring the grid back.",
                        statement=_EXTRACT_QUERY,
                        connection="FinanceWarehouse",
                    ),
                ),
                Stage(
                    id="pre_adjustment",
                    intent="The statutory accrual position before any adjustment",
                    kind=StageKind.LOAD,
                    sources=[StageSource(origin=SourceOrigin.HANDIN, ref="pre-adjustment extract")],
                    depends_on=["extract_query"],
                    confidence=Confidence.HIGH,
                ),
                Stage(
                    id="adjust",
                    intent="Apply the agreed 4.5% Q2 uplift to in-scope statutory accruals",
                    sources=[StageSource(origin=SourceOrigin.STAGE, ref="pre_adjustment")],
                    depends_on=["pre_adjustment"],
                    confidence=Confidence.HIGH,
                    operations=[
                        "adjustment_c17_c92",
                        "adjustment_d17_d92",
                        "adjustment_e17_e92",
                        "adjustment_f17_f92",
                        "adjustment_d94_f94",
                        "adjustment_d95",
                    ],
                ),
                Stage(
                    id="approve_adjustment",
                    intent="Record the decision to apply the adjustment before it is applied",
                    kind=StageKind.CHECKPOINT,
                    depends_on=["adjust"],
                ),
                Stage(
                    id="update_statement",
                    intent="Hand the user the UPDATE that applies the approved adjustment",
                    kind=StageKind.HANDOFF,
                    depends_on=["approve_adjustment", "adjust"],
                    operations=["adjustment_g17_g92"],
                    handoff=Handoff(
                        instruction="Run this in one transaction, then re-extract.",
                        built_from="adjust",
                        template=(
                            "UPDATE fin.accruals SET accrual_gbp = {accrual_gbp_after} "
                            "WHERE trade_id = {trade_id}"
                        ),
                        connection="FinanceWarehouse",
                        mutates=True,
                    ),
                ),
                Stage(
                    id="post_adjustment",
                    intent="The re-extract, as evidence the update did what was intended",
                    kind=StageKind.LOAD,
                    sources=[
                        StageSource(origin=SourceOrigin.HANDIN, ref="post-adjustment extract")
                    ],
                    depends_on=["update_statement"],
                    confidence=Confidence.HIGH,
                ),
                Stage(
                    id="verification",
                    intent="Compare the re-extract against what the notebook predicted, row by row",
                    sources=[
                        StageSource(origin=SourceOrigin.STAGE, ref="adjust"),
                        StageSource(origin=SourceOrigin.STAGE, ref="post_adjustment"),
                    ],
                    depends_on=["adjust", "post_adjustment"],
                    confidence=Confidence.HIGH,
                ),
                Stage(
                    id="signoff",
                    intent="The impact statement, recomputed, saying what was verified",
                    kind=StageKind.OUTPUT,
                    sources=[StageSource(origin=SourceOrigin.STAGE, ref="verification")],
                    depends_on=["verification"],
                    confidence=Confidence.HIGH,
                ),
            ],
            open_questions=[
                OpenQuestion(
                    question="The Sign-off tab says three entities; the UPDATE names four. Which?",
                    answer="Four. The statement is what ran.",
                )
            ],
            briefing=Briefing(
                purpose="Records the quarterly uplift applied to statutory accruals.",
                background="The June reforecast moved the accrual basis to the 2026 rate card.",
                cadence="Quarterly, once the reforecast has been agreed.",
                watch_for=["One trade carries no accrual value and is shown at nil."],
                sources=[
                    "Sign-off!A3:A4 (Purpose)",
                    "Sign-off!A6:A7 (Background)",
                    "Sign-off!A15:A16 (Known issues)",
                ],
            ),
        ),
        workbook="q2_accrual_adjustment.xlsx",
        workbook_sha256=WORKBOOK_SHA256,
        created_at=datetime(2026, 8, 23, 9, 0, tzinfo=UTC),
        generated_by="human",
    )


def observed_plan() -> ProcessPlan:
    """The plan the model proposed and the user approved, loaded through the real store."""
    return plan_from_yaml(OBSERVED_PLAN_YAML)


def observed_analysis() -> WorkbookAnalysis:
    """The facts that plan was written against, trimmed to what the review pass reads."""
    return WorkbookAnalysis(
        kedge_version="0.0.11",
        generated_at=datetime(2026, 8, 23, 8, 33, 7, tzinfo=UTC),
        workbook=WorkbookIdentity(
            path="evals/adjustment_signoff/q2_accrual_adjustment.xlsx",
            filename="q2_accrual_adjustment.xlsx",
            sha256=WORKBOOK_SHA256,
            size_bytes=25_318,
            file_format="xlsx",
            created_by="Microsoft Excel Compatible / Openpyxl 3.1.5",
        ),
        sheets=[
            SheetInfo(
                name="Sign-off",
                index=0,
                role=SheetRole.DATA,
                role_confidence=0.535,
                max_row=28,
                max_column=4,
                used_range="A1:D28",
                value_cell_count=31,
                text_cell_count=26,
                header_row=19,
                preamble_rows=18,
            ),
            SheetInfo(
                name="Pre-Adjustment",
                index=1,
                role=SheetRole.DATA,
                role_confidence=0.831,
                max_row=138,
                max_column=7,
                used_range="A1:G138",
                value_cell_count=863,
                text_cell_count=624,
                header_row=18,
                preamble_rows=17,
            ),
            SheetInfo(
                name="Adjustment",
                index=2,
                role=SheetRole.CALCULATION,
                role_confidence=0.679,
                max_row=95,
                max_column=7,
                used_range="A1:G95",
                formula_cell_count=384,
                value_cell_count=178,
                text_cell_count=176,
                header_row=16,
                preamble_rows=15,
                operation_ids=list(_OPERATION_IDS),
            ),
            SheetInfo(
                name="Post-Adjustment",
                index=3,
                role=SheetRole.DATA,
                role_confidence=0.831,
                max_row=138,
                max_column=7,
                used_range="A1:G138",
                value_cell_count=863,
                text_cell_count=624,
                header_row=18,
                preamble_rows=17,
            ),
        ],
        operations=[
            _lookup("adjustment_c17_c92", "C17", "C", offset=2, taken=3, fan_out=1),
            _lookup("adjustment_d17_d92", "D17", "D", offset=3, taken=5, fan_out=4),
            _rounded("adjustment_e17_e92", "E17", "E", "=ROUND(RC[-1]*uplift_rate,2)", 1),
            _rounded("adjustment_f17_f92", "F17", "F", "=ROUND(RC[-2]*(1+uplift_rate),2)", 2),
            LogicalOperation(
                id="adjustment_g17_g92",
                sheet="Adjustment",
                anchor="G17",
                ranges=["Adjustment!G17:G92"],
                cell_count=76,
                r1c1=_GENERATED_UPDATE_R1C1,
                sample_a1=_GENERATED_UPDATE_A1,
                functions=["TEXT"],
                references=[
                    Reference(raw="F17", a1="F17"),
                    Reference(raw="C17", a1="C17"),
                    Reference(raw="A17", a1="A17"),
                ],
                excel_pattern=ExcelPattern.TEXT_MANIPULATION,
                description="Text manipulation on Adjustment, filled down 76 rows.",
                translation_hint='col("x").str.* expressions',
                cached_values_present=True,
                downstream_ref_count=0,
            ),
            _aggregate(
                "adjustment_d94_f94",
                "D94",
                ["Adjustment!D94:F94"],
                3,
                "=SUM(R[-77]C:R[-2]C)",
                "=SUM(D17:D92)",
            ),
            _aggregate(
                "adjustment_d95",
                "D95",
                ["Adjustment!D95"],
                1,
                "=COUNT(R[-78]C:R[-3]C)",
                "=COUNT(D17:D92)",
            ),
        ],
        graph=DependencyGraph(
            nodes=list(_OPERATION_IDS),
            edges=[
                ("adjustment_d17_d92", "adjustment_e17_e92"),
                ("adjustment_d17_d92", "adjustment_f17_f92"),
                ("adjustment_f17_f92", "adjustment_g17_g92"),
                ("adjustment_c17_c92", "adjustment_g17_g92"),
                ("adjustment_d17_d92", "adjustment_d94_f94"),
                ("adjustment_e17_e92", "adjustment_d94_f94"),
                ("adjustment_f17_f92", "adjustment_d94_f94"),
                ("adjustment_d17_d92", "adjustment_d95"),
            ],
            roots=["adjustment_c17_c92", "adjustment_d17_d92"],
            leaves=["adjustment_g17_g92", "adjustment_d94_f94", "adjustment_d95"],
        ),
        connections=[
            Connection(
                name="AccrualExtract",
                kind="odbc",
                command=_EXTRACT_QUERY,
                command_type="sql",
                connection_string_redacted=(
                    "ODBC;DSN=FinanceWarehouse;Description=Finance Warehouse (PROD);"
                    "UID=***REDACTED***;Trusted_Connection=Yes;DATABASE=FinanceWarehouse"
                ),
                description="Statutory accrual position at period end",
            )
        ],
        connections_status="ok",
        named_ranges=[
            NamedRange(name="uplift_rate", refers_to="Adjustment!$B$4", reference_count=152)
        ],
        notes=[
            ProcessNote(
                source="sheet",
                origin="Sign-off",
                location="A1",
                text="Q2 2026 statutory accrual adjustment - sign-off",
            ),
            ProcessNote(
                source="sheet",
                origin="Sign-off",
                location="A3:A4",
                heading="Purpose",
                text=(
                    "To record the Q2 2026 uplift applied to statutory accruals following the "
                    "reforecast agreed at the June finance committee, and to evidence that the "
                    "adjustment was applied as approved."
                ),
            ),
            ProcessNote(
                source="sheet",
                origin="Sign-off",
                location="A6:A7",
                heading="Background",
                text=(
                    "The June reforecast moved the accrual basis for the affected entities from "
                    "the 2025 rate card to the 2026 one. Finance agreed a flat 4.5% uplift as the "
                    "practical equivalent, to be applied to statutory-ledger accruals at 30 June "
                    "2026 only. Management accounts are unaffected."
                ),
            ),
            ProcessNote(
                source="sheet",
                origin="Sign-off",
                location="A9:A10",
                heading="What was done",
                text=(
                    "The pre-adjustment position was extracted from the finance warehouse (see "
                    "the Pre-Adjustment tab, which carries the query). The uplift was calculated "
                    "per trade on the Adjustment tab and applied to the three affected entities "
                    "by the update statement recorded there. The position was then re-extracted "
                    "using the same query to confirm the update had taken effect."
                ),
            ),
            ProcessNote(
                source="sheet",
                origin="Sign-off",
                location="A12:A13",
                heading="Scope",
                text=(
                    "Entities E-04, E-07, E-09 and E-12. Statutory ledger only. Period end 30 "
                    "June 2026. Cancelled trades are excluded."
                ),
            ),
            ProcessNote(
                source="sheet",
                origin="Sign-off",
                location="A15:A16",
                heading="Known issues",
                text=(
                    "One trade carries no accrual value in the source system pending a "
                    "cost-centre reallocation. It is shown at nil and will be picked up in Q3."
                ),
            ),
            ProcessNote(
                source="sheet",
                origin="Sign-off",
                location="A18:D20",
                heading="Impact summary",
                text="Measure Before After Movement\nIn-scope accruals (GBP)",
            ),
            ProcessNote(
                source="sheet",
                origin="Sign-off",
                location="A24:B28",
                heading="Sign-off",
                text=(
                    "Prepared by R. Advani, Financial Control\nReviewed by M. Okafor, Head of "
                    "Statutory Reporting\nDate\nControl reference FC-Q2-2026-014"
                ),
            ),
        ],
        cached_values=CachedValueCoverage(
            formula_cell_count=384,
            cached_present_count=384,
            coverage=1.0,
            status="present",
        ),
        findings=[
            Finding(
                kind=FindingKind.PREAMBLE_ROWS,
                severity=Severity.WARNING,
                message="Sheet 'Sign-off' has 18 row(s) above its header at row 19.",
                sheet="Sign-off",
                location="A1:A18",
                affected_cells=18,
            ),
            Finding(
                kind=FindingKind.PREAMBLE_ROWS,
                severity=Severity.WARNING,
                message="Sheet 'Pre-Adjustment' has 17 row(s) above its header at row 18.",
                sheet="Pre-Adjustment",
                location="A1:A17",
                affected_cells=17,
            ),
            Finding(
                kind=FindingKind.PREAMBLE_ROWS,
                severity=Severity.WARNING,
                message="Sheet 'Post-Adjustment' has 17 row(s) above its header at row 18.",
                sheet="Post-Adjustment",
                location="A1:A17",
                affected_cells=17,
            ),
            Finding(
                kind=FindingKind.DEAD_REGION,
                severity=Severity.INFO,
                message=(
                    "The region at Adjustment!G17 (76 cells) is not read by anything else in the "
                    "workbook."
                ),
                sheet="Adjustment",
                location="G17",
                affected_cells=76,
            ),
            Finding(
                kind=FindingKind.DEAD_REGION,
                severity=Severity.INFO,
                message=(
                    "The region at Adjustment!D94 (3 cells) is not read by anything else in the "
                    "workbook."
                ),
                sheet="Adjustment",
                location="D94",
                affected_cells=3,
            ),
            Finding(
                kind=FindingKind.DEAD_REGION,
                severity=Severity.INFO,
                message=(
                    "The region at Adjustment!D95 (1 cells) is not read by anything else in the "
                    "workbook."
                ),
                sheet="Adjustment",
                location="D95",
                affected_cells=1,
            ),
        ],
    )
