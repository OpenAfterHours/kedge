"""Build the management-fee billing-run eval workbook.

**Calibration spike.** This is the first cut of the generator proposed in
``evals/proposals/fee_billing_run.md``, written to answer one question before the full
generator is: does the tab design in section 3 actually land in the structural band the
proposal commits to -- 45-60 logical operations, 12-15 distinct patterns, complexity
0.68-0.75, and a triage verdict that is not ``stop``?

That band is narrow on purpose and it is easy to miss in either direction. A workbook that
scores 0.55 has not stressed anything the existing corpus does not already stress; one that
triages ``stop`` cannot be converted, so it cannot be graded on the quality of its conversion.
Section 1.1 of the proposal is the reason it is not obvious which way a design will fall: R1C1
normalisation collapses a uniform grid to a handful of operations however large it is, so
complexity comes from the *variety* of the formulas and not from the size of the sheets.

What is deliberately missing, and must arrive before this is the real generator:

* **Cached values.** openpyxl writes formulas with no cached result, so every region reports
  ``cached_values_present=False`` and nothing is reconcilable. ``tests/fixtures/generate.py``
  owns ``inject_cached_values`` and this module must import it rather than grow a second copy
  -- a second copy is a second chance to get Excel's rounding wrong.
* **The pivot on ``Summary``.** openpyxl cannot author a pivot table. It has to be built by
  driving Excel over COM, as ``evals/proposals`` measured it, which makes the committed
  workbook a Windows-generated artifact like the rest of the corpus.
* **The narrative.** Sign-off prose, the Recon commentary and the three override reasons are
  the parts a reader of the converted notebook depends on, and they are written rather than
  generated.
Determinism is already wired: ``build`` routes the saved file through the corpus generator's
``normalise``, so two runs produce identical bytes. That is not decoration -- without it the
workbook is only reproducible within a single wall-clock second, and the rot guard's determinism
assertion passes or fails depending on how fast the machine is.

Run it::

    uv run python evals/fee_billing_run/build_workbook.py
    uv run python evals/fee_billing_run/build_workbook.py --calibrate
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import sys
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
if str(FIXTURE_DIR) not in sys.path:
    sys.path.insert(0, str(FIXTURE_DIR))

# Imported rather than copied, for the reason CLAUDE.md gives about `kedge.xl`: a second copy of
# a normalisation routine is a second chance to get it subtly wrong. `normalise` also has to run
# *after* the save -- openpyxl assigns `properties.modified = datetime.now()` unconditionally
# inside `save_workbook`, so a timestamp set on the workbook object is discarded and the file is
# only reproducible within one wall-clock second.
from generate import normalise  # noqa: E402

WORKBOOK_NAME = "m11_management_fee_run.xlsx"

# The structural band the proposal commits to. `--calibrate` measures against these and the
# eventual rot guard asserts them, because a generator refactor that quietly collapses the
# workbook back to a handful of operations would otherwise pass every other check while
# measuring nothing at all.
TARGET_OPERATIONS = (45, 60)
TARGET_PATTERNS = 12
TARGET_COMPLEXITY = (0.68, 0.75)
FORBIDDEN_VERDICT = "stop"

CLIENTS = 84
"""Enough rows that an aggregation is not eyeballable, few enough that the file stays small."""

FIRST_DATA_ROW = 19
"""`Working` keeps its parameters and its preamble above the grid, as the original did."""

EXTRACT_SQL = """SELECT
       p.client_code,
       p.client_name,
       p.as_of_date,
       p.period_month,
       p.mandate,
       p.market_value_gbp
  FROM fin.positions p
  JOIN fin.mandates m
    ON m.client_code = p.client_code
 WHERE p.period_month IN ('2026-10', '2026-11')
   AND p.status <> 'CLOSED'
 ORDER BY p.client_code, p.as_of_date"""

POST_SQL_TEMPLATE = (
    '="INSERT INTO fin.fee_invoice (client_code, period_month, fee_gbp) VALUES (\'"'
    '&$A{row}&"\', \'"&$D{row}&"\', "&$S{row}&");"'
)


# =============================================================================
# THE FORMULA SHAPES
# =============================================================================
#
# One entry per column of `Working`, and they are deliberately all different. This list *is*
# the complexity of the workbook (proposal section 1.1): thirty columns sharing one R1C1 string
# compress to one logical operation, thirty distinct ones stay thirty. Each carries the header
# the workbook shows a person, because `reconcile.infer_regions` keys a region's column off
# that header -- so the headers are what make the reconciliation map hard, and they have to be
# written the way a person would write them rather than as identifiers.

WORKING_COLUMNS: list[tuple[str, str]] = [
    # Not a formula. A person pastes the client codes in, and it matters that this is data:
    # `=Positions!$A{n}` with a per-row absolute target normalises to a *different* R1C1 string
    # on every row, so it does not compress and one column becomes eighty-four operations.
    ("Client", ""),
    ("Client name", "=INDEX('Entity Map'!$B:$B,MATCH($A{row},'Entity Map'!$A:$A,0))"),
    ("Period start", "=DATE(2026,11,1)"),
    ("Period", '=TEXT($C{row},"yyyy-mm")'),
    ("Avg AUM (GBP)", "=AVERAGEIFS(Positions!$F:$F,Positions!$A:$A,$A{row})"),
    ("Prior close", "=E{prev}"),
    ("Opening balance", "=IF($A{row}=$A{prev},F{prev},E{row})"),
    ("Tier band", "=VLOOKUP(E{row},'Fee Schedule'!$C$5:$E$11,3,TRUE)"),
    ("Tier bps", "=VLOOKUP($A{row},'Fee Schedule'!$A$5:$E$92,5,FALSE)"),
    ("Legal entity", "=INDEX('Entity Map'!$C:$C,MATCH($A{row},'Entity Map'!$A:$A,0))"),
    ("Cost centre", "=INDEX('Entity Map'!$D:$D,MATCH($A{row},'Entity Map'!$A:$A,0))"),
    ("Gross fee (GBP)", "=ROUND(E{row}*I{row}/10000,2)"),
    ("Days in month", "=DAY(EOMONTH($C{row},0))"),
    ("Days billed", '=DATEDIF($C{row},EOMONTH($C{row},0),"d")+1'),
    ("Pro-rated fee", "=ROUND(L{row}*N{row}/M{row},2)"),
    ("Floor applied", "=MAX(O{row},'Fee Schedule'!$H$3)"),
    ("Cap applied", "=MIN(P{row},'Fee Schedule'!$H$4)"),
    ("Discount", "=IFERROR(Q{row}*'Fee Schedule'!$H$5,0)"),
    ("Net fee (GBP)", "=ROUND(Q{row}-R{row},2)"),
    ("Invoice key", '=LEFT($A{row},3)&"-"&$D{row}'),
    ("Positions seen", "=COUNTIFS(Positions!$A:$A,$A{row})"),
    ("Running total", "=SUM($S$19:S{row})"),
    ("Weighted check", "=SUMPRODUCT(Positions!$F$4:$F$903,--(Positions!$A$4:$A$903=$A{row}))"),
    ("Fee band", '=IF(S{row}>40000,"A",IF(S{row}>8000,"B","C"))'),
    ("Mandate", "=UPPER(TRIM(INDEX('Entity Map'!$E:$E,MATCH($A{row},'Entity Map'!$A:$A,0))))"),
    ("Billing year", "=YEAR($C{row})"),
    # The override the billing manager agreed, falling back to the calculation. This is the
    # column discrimination 4 turns on: the workbook resolves it silently and a runbook must
    # re-ask it.
    ("Agreed fee", "=IFERROR(VLOOKUP($A{row},Overrides!$A:$C,3,FALSE),S{row})"),
    ("Override applied", '=IF(AB{row}=S{row},"","OVERRIDE")'),
    ("Mandate AUM", "=SUMIFS(Positions!$F:$F,Positions!$E:$E,$Y{row})"),
    ("Share of mandate", "=IFERROR(E{row}/AD{row},0)"),
    ("Accounts on file", "=COUNTIF('Entity Map'!$A:$A,$A{row})"),
    ("Latest as-of", "=MAX(Positions!$C:$C)"),
    # The abandoned 2024 method: real formulas, filled down like the rest, read by nothing.
    # These are the dead regions discrimination 5 asks a conversion to tell apart from `Post`.
    ("Old method fee", "=ROUND(E{row}*$B$14/10000,2)"),
    ("Old method delta", "=AH{row}-S{row}"),
    ("2024 adj", "=AI{row}*$B$15"),
    ("2024 note", '=CONCATENATE($A{row}," / ",TEXT(AJ{row},"0.00"))'),
]
"""Thirty columns, thirty shapes.

The last four are the abandoned 2024 method the proposal's discrimination 5 is built on: real
formulas, filled down like the rest, and read by nothing. They become ``dead_region`` findings
and they are what the eighteenth one -- the ``Post`` column -- has to be told apart from.
"""


def column_letter_for(index: int) -> str:
    """1-based position in ``WORKING_COLUMNS`` to its column letter on the sheet."""
    return get_column_letter(index)


# =============================================================================
# SHEETS
# =============================================================================


def write_positions(sheet: Any, rng: random.Random) -> None:
    """The warehouse extract: the query that produced it, then the grid it returned.

    The SQL sits in the cells exactly as the original process kept it, because discrimination 1
    of the first eval -- read the query as a step to hand over rather than as prose -- is reused
    here unchanged and needs somewhere real to read it from.
    """
    sheet["A1"] = "Positions extract -- run this against the warehouse, paste below"
    sheet["A1"].font = Font(bold=True)
    for offset, line in enumerate(EXTRACT_SQL.splitlines()):
        cell = sheet.cell(3 + offset, 1, line)
        cell.font = Font(name="Consolas", size=9)
        cell.alignment = Alignment(horizontal="left")

    header_row = 3 + len(EXTRACT_SQL.splitlines()) + 1
    headers = [
        "client_code",
        "client_name",
        "as_of_date",
        "period_month",
        "mandate",
        "market_value_gbp",
    ]
    for index, header in enumerate(headers, start=1):
        sheet.cell(header_row, index, header).font = Font(bold=True)

    row = header_row + 1
    for client in range(CLIENTS):
        code = f"{client + 1:05d}"
        for month in ("2026-10", "2026-11"):
            sheet.cell(row, 1, code)
            sheet.cell(row, 2, f"Client {client + 1:03d} Holdings")
            sheet.cell(row, 3, dt.date(2026, 10 if month == "2026-10" else 11, 28))
            sheet.cell(row, 4, month)
            sheet.cell(row, 5, ("Balanced", "Equity", "Fixed Income")[client % 3])
            sheet.cell(row, 6, round(rng.uniform(250_000, 90_000_000), 2))
            row += 1


def write_fee_schedule(sheet: Any) -> None:
    """The file the onboarding team emailed, pasted in.

    Two preamble rows and a header on row 4, which is the shape a pasted extract actually has
    and which fires ``preamble_rows``. The bps column is written as **text** on purpose: Excel
    copies what a cell looks like, not what it holds, and CLAUDE.md records a whole class of
    failure that starts there. The client codes keep their leading zeros for the same reason --
    ``00417`` becoming ``417`` breaks every join it takes part in.
    """
    sheet["A1"] = "Fee schedule -- from Client Onboarding, effective 2026-11-01"
    sheet["A1"].font = Font(bold=True)
    sheet["A2"] = "Do not edit. Superseded schedules are kept on the shared drive."

    for index, header in enumerate(
        ["client_code", "effective_from", "band_floor", "band_ceiling", "bps"], start=1
    ):
        sheet.cell(4, index, header).font = Font(bold=True)

    bands = [
        (0, 5_000_000, "35.0"),
        (5_000_000, 20_000_000, "27.5"),
        (20_000_000, 50_000_000, "22.0"),
        (50_000_000, 100_000_000, "18.5"),
    ]
    for offset, (floor, ceiling, bps) in enumerate(bands):
        row = 5 + offset
        sheet.cell(row, 3, floor)
        sheet.cell(row, 4, ceiling)
        sheet.cell(row, 5, bps)

    # Per-client overrides to the banded rate, one row each, codes as text.
    for client in range(CLIENTS):
        row = 9 + client
        sheet.cell(row, 1, f"{client + 1:05d}")
        sheet.cell(row, 2, dt.date(2026, 11, 1))
        sheet.cell(row, 5, f"{20 + (client % 9) * 1.5:.1f}")

    sheet["G3"] = "Minimum fee"
    sheet["H3"] = 750.0
    sheet["G4"] = "Maximum fee"
    sheet["H4"] = 250_000.0
    sheet["G5"] = "Standard discount"
    sheet["H5"] = 0.025


def write_entity_map(sheet: Any) -> None:
    """Client to legal entity to cost centre.

    The eighth tab in the file and a dependency of the fourth, which is discrimination 10: tab
    order is the order somebody added things, never dependency order.
    """
    for index, header in enumerate(
        ["client_code", "client_name", "legal_entity", "cost_centre", "mandate"], start=1
    ):
        sheet.cell(1, index, header).font = Font(bold=True)
    entities = ["LuxCo", "UKCo", "DubCo"]
    for client in range(CLIENTS):
        row = 2 + client
        sheet.cell(row, 1, f"{client + 1:05d}")
        sheet.cell(row, 2, f"Client {client + 1:03d} Holdings")
        sheet.cell(row, 3, entities[client % 3])
        sheet.cell(row, 4, f"CC-{100 + (client % 6) * 10}")
        sheet.cell(row, 5, ("  balanced ", "equity", " fixed income")[client % 3])


def write_working(sheet: Any) -> None:
    """The wide one: parameters, then thirty distinct formula columns filled down.

    This is where the workbook's complexity actually lives. See ``WORKING_COLUMNS``.
    """
    sheet["A1"] = "Management fee working -- November 2026"
    sheet["A1"].font = Font(bold=True, size=12)
    sheet["A3"] = "Period"
    sheet["B3"] = "2026-11"
    sheet["A4"] = "Prepared by"
    sheet["B4"] = "Billing Operations"
    sheet["A14"] = "2024 flat rate (bps)"
    sheet["B14"] = 26.0
    sheet["A15"] = "2024 adjustment factor"
    sheet["B15"] = 0.97

    header_row = FIRST_DATA_ROW - 1
    for index, (header, _) in enumerate(WORKING_COLUMNS, start=1):
        sheet.cell(header_row, index, header).font = Font(bold=True)

    for offset in range(CLIENTS):
        row = FIRST_DATA_ROW + offset
        for index, (_, template) in enumerate(WORKING_COLUMNS, start=1):
            if not template:
                sheet.cell(row, index, f"{offset + 1:05d}")
                continue
            formula = template.format(row=row, prev=max(FIRST_DATA_ROW, row - 1))
            sheet.cell(row, index, formula)

    total_row = FIRST_DATA_ROW + CLIENTS + 1
    sheet.cell(total_row, 1, "TOTAL").font = Font(bold=True)
    for index in (12, 15, 19):
        letter = column_letter_for(index)
        sheet.cell(
            total_row,
            index,
            f"=SUM({letter}{FIRST_DATA_ROW}:{letter}{FIRST_DATA_ROW + CLIENTS - 1})",
        )


def write_overrides(sheet: Any) -> None:
    """Three computed fees the billing manager typed over, each with a reason.

    Discrimination 4, and the one the proposal argues is the sharpest judgement call: an
    override is a decision somebody made once, so a runbook has to re-ask it rather than bake
    it in. The reasons are prose because that is what makes them re-askable.
    """
    for index, header in enumerate(
        ["client_code", "computed_fee", "agreed_fee", "reason", "agreed_on", "agreed_by"], start=1
    ):
        sheet.cell(1, index, header).font = Font(bold=True)
    overrides = [
        (
            "00007",
            "Fee waived for Q4 pending the mandate transfer agreed with the client in September.",
            dt.date(2026, 9, 18),
        ),
        (
            "00041",
            "Capped at the 2025 level; the uplift was not communicated before the billing run.",
            dt.date(2026, 10, 2),
        ),
        (
            "00062",
            "Manual correction: two accounts were double-counted in the position extract.",
            dt.date(2026, 11, 3),
        ),
    ]
    for offset, (code, reason, agreed) in enumerate(overrides):
        row = 2 + offset
        sheet.cell(row, 1, code)
        sheet.cell(row, 2, f"=INDEX(Working!$S:$S,MATCH($A{row},Working!$A:$A,0))")
        sheet.cell(row, 3, [0.0, 18_500.0, 4_220.75][offset])
        sheet.cell(row, 4, reason)
        sheet.cell(row, 5, agreed)
        sheet.cell(row, 6, "Billing Operations")


def write_allocation(sheet: Any) -> None:
    """Fees aggregated to invoice grain, with a subtotal row per legal entity *inside* the grid.

    Discrimination 6. An embedded subtotal is not data, and a conversion that reads the grid
    flat double-counts every entity. ``SUBTOTAL`` rather than ``SUM`` because that is what a
    person reaching for the toolbar button gets, and because it excludes itself.
    """
    for index, header in enumerate(
        ["client_code", "legal_entity", "cost_centre", "period", "fee_gbp"], start=1
    ):
        sheet.cell(1, index, header).font = Font(bold=True)

    row = 2
    for entity_index, entity in enumerate(["LuxCo", "UKCo", "DubCo"]):
        block_start = row
        for client in range(entity_index, CLIENTS, 3):
            sheet.cell(row, 1, f"{client + 1:05d}")
            sheet.cell(row, 2, entity)
            sheet.cell(row, 3, f"CC-{100 + (client % 6) * 10}")
            sheet.cell(row, 4, "2026-11")
            # Aggregated by key rather than by position. `=Working!$S{n}` would point at a
            # different row from each Allocation row, so like the Working client column above
            # it would refuse to compress -- and a lookup is what an allocation tab does anyway.
            sheet.cell(row, 5, f"=SUMIFS(Working!$S:$S,Working!$A:$A,$A{row})")
            row += 1
        sheet.cell(row, 2, f"{entity} subtotal").font = Font(bold=True)
        sheet.cell(row, 5, f"=SUBTOTAL(9,E{block_start}:E{row - 1})")
        row += 1


def write_post(sheet: Any) -> None:
    """One INSERT per invoice, built by concatenation. The manual carry.

    Nothing in the workbook reads this column, so it is a ``dead_region`` with fan-out zero --
    which is exactly what sorts it last in the planner's context (proposal section 2.1). It is
    also the step that posts the fees, so dropping it deletes the point of the process.
    """
    sheet["A1"] = "Run these against the ledger once the fee run is approved."
    sheet["A1"].font = Font(bold=True)
    sheet["A3"] = "statement"
    sheet["A3"].font = Font(bold=True)
    for offset in range(CLIENTS):
        working_row = FIRST_DATA_ROW + offset
        sheet.cell(4 + offset, 1, POST_SQL_TEMPLATE.format(row=working_row))


def write_recon(sheet: Any) -> None:
    """Last month beside this month, with a variance column and typed commentary."""
    for index, header in enumerate(
        ["legal_entity", "october_gbp", "november_gbp", "variance_gbp", "commentary"], start=1
    ):
        sheet.cell(1, index, header).font = Font(bold=True)
    priors = {"LuxCo": 412_880.44, "UKCo": 388_100.02, "DubCo": 205_990.18}
    for offset, (entity, prior) in enumerate(priors.items()):
        row = 2 + offset
        sheet.cell(row, 1, entity)
        sheet.cell(row, 2, prior)
        sheet.cell(row, 3, f"=SUMIF(Allocation!$B:$B,$A{row},Allocation!$E:$E)")
        sheet.cell(row, 4, f"=C{row}-B{row}")
    for offset in range(len(priors)):
        row = 2 + offset
        sheet.cell(row, 6, f"=IFERROR(D{row}/B{row},0)")
        sheet.cell(row, 7, f'=IF(ABS(F{row})>0.05,"REVIEW","")')
    sheet.cell(1, 6, "variance_pct").font = Font(bold=True)
    sheet.cell(1, 7, "flag").font = Font(bold=True)
    sheet["E2"] = "Transfer in from the Geneva book; expected."
    sheet["E3"] = "Two mandates closed in October."
    sheet["E4"] = "Unexplained. Raised with the desk 2026-11-04."


def write_signoff(sheet: Any) -> None:
    """Purpose, background, known issues. The part nobody can reconstruct.

    ``WorkbookAnalysis.notes`` picks this up and the plan's ``Briefing`` is where it goes,
    prose citing its sources. Written rather than generated, because invented background in a
    finance notebook is confident, plausible and unattributable.
    """
    sheet["A1"] = "Management fee billing run -- sign-off"
    sheet["A1"].font = Font(bold=True, size=12)

    entries = [
        (
            "Purpose",
            "Calculate the management fee owed by each client for the month, apply the agreed "
            "floors, caps and discounts, allocate the result to the legal entity and cost "
            "centre that will carry it, and post the invoices to the ledger.",
        ),
        (
            "Background",
            "The tiered schedule replaced a flat 26bps rate in January 2024. The old columns "
            "are still in the Working tab because the 2024 comparatives were needed for the "
            "audit; nothing reads them now.",
        ),
        (
            "Known issues",
            "The Allocation tab is left on manual calculation because recalculating it on a "
            "full extract takes several minutes. Check the totals against Recon before "
            "signing. Three clients have agreed fees that override the calculation -- see the "
            "Overrides tab, and confirm each one still applies before posting.",
        ),
        (
            "Cadence",
            "Monthly, on the third working day, after the position extract has been signed off "
            "by Middle Office.",
        ),
    ]
    row = 3
    for heading, text in entries:
        sheet.cell(row, 1, heading).font = Font(bold=True)
        cell = sheet.cell(row + 1, 1, text)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        row += 3


def write_summary_placeholder(sheet: Any) -> None:
    """Where the pivot goes.

    openpyxl cannot author a pivot table, so the committed workbook has to be finished by
    driving Excel over COM -- the same route ``tests/fixtures/generate.py`` already uses for
    ground truth. Until that step exists this tab is empty and the eval's discrimination 7 is
    not yet reachable.
    """
    sheet["A1"] = "TODO(kedge): pivot over Allocation, built by Excel. See build_pivot.py."


# =============================================================================
# BUILD
# =============================================================================


def build(path: Path) -> None:
    """Write the workbook. Deterministic: the seed is fixed and nothing reads the clock."""
    rng = random.Random(20261103)
    wb = Workbook()

    # Tab order is the order somebody added things over four years, which is discrimination 10.
    positions = wb.active
    positions.title = "Positions"
    write_positions(positions, rng)
    write_fee_schedule(wb.create_sheet("Fee Schedule"))
    write_working(wb.create_sheet("Working"))
    write_overrides(wb.create_sheet("Overrides"))
    write_allocation(wb.create_sheet("Allocation"))
    write_summary_placeholder(wb.create_sheet("Summary"))
    write_post(wb.create_sheet("Post"))
    write_entity_map(wb.create_sheet("Entity Map"))
    write_recon(wb.create_sheet("Recon"))
    write_signoff(wb.create_sheet("Sign-off"))

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    normalise(path)


def calibrate(path: Path) -> int:
    """Measure the built workbook against the structural band, and say which way it missed.

    This is the whole point of the spike. Returns a process exit code so it can gate.
    """
    from kedge.analysis.analyse import analyse
    from kedge.plan.propose import build_proposal_context
    from kedge.plan.triage import complexity, triage

    analysis = analyse(path)
    result = triage(analysis)
    score = complexity(analysis)
    patterns = sorted({op.excel_pattern.value for op in analysis.operations})
    cross_sheet = sum(1 for op in analysis.operations if any(ref.sheet for ref in op.references))
    dead = [f for f in analysis.findings if f.kind.value == "dead_region"]
    context = build_proposal_context(analysis, result)
    ranked = sorted(analysis.operations, key=lambda op: (-op.downstream_ref_count, op.id))
    carry = next((op for op in analysis.operations if op.sheet == "Post"), None)

    print(f"workbook            {path.name}  ({path.stat().st_size:,} bytes)")
    print(f"operations          {len(analysis.operations):>6}   target {TARGET_OPERATIONS}")
    print(f"distinct patterns   {len(patterns):>6}   target >= {TARGET_PATTERNS}")
    print(f"complexity          {score:>6.3f}   target {TARGET_COMPLEXITY}")
    print(f"verdict             {result.verdict.value:>6}   must not be {FORBIDDEN_VERDICT!r}")
    print(f"convertible         {result.convertible:>6.2f}")
    print(f"sheets              {len(analysis.sheets):>6}")
    print(f"cross-sheet ops     {cross_sheet:>6}")
    print(f"findings            {len(analysis.findings):>6}")
    print(f"dead regions        {len(dead):>6}")
    print(f"dependency edges    {len(analysis.graph.edges):>6}")
    print(f"column profiles     {len(analysis.profiles):>6}")
    print(f"patterns            {patterns}")
    if carry is not None:
        position = [op.id for op in ranked].index(carry.id) + 1
        print(
            f"manual carry        ranks {position} of {len(ranked)}  "
            f"(fan-out {carry.downstream_ref_count}, pattern {carry.excel_pattern.value})"
        )
    else:
        print("manual carry        NOT FOUND -- the Post column did not become an operation")
    print(f"digest truncation   {[k for k in context if k.endswith('_omitted')] or 'none'}")
    for blocker in result.blockers:
        print(f"  blocker: {blocker.render()}")

    problems: list[str] = []
    if not TARGET_OPERATIONS[0] <= len(analysis.operations) <= TARGET_OPERATIONS[1]:
        problems.append(
            f"operations {len(analysis.operations)} outside {TARGET_OPERATIONS} -- "
            f"{'add' if len(analysis.operations) < TARGET_OPERATIONS[0] else 'remove'} "
            f"distinct formula shapes in WORKING_COLUMNS"
        )
    if len(patterns) < TARGET_PATTERNS:
        problems.append(f"only {len(patterns)} distinct patterns, want >= {TARGET_PATTERNS}")
    if not TARGET_COMPLEXITY[0] <= score <= TARGET_COMPLEXITY[1]:
        problems.append(f"complexity {score:.3f} outside {TARGET_COMPLEXITY}")
    if result.verdict.value == FORBIDDEN_VERDICT:
        problems.append("triage says stop, so the workbook cannot be converted or graded")

    print()
    if problems:
        for problem in problems:
            print(f"MISS  {problem}")
        return 1
    print("IN BAND")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="Measure the built workbook against the structural band.",
    )
    args = parser.parse_args()

    path = Path(__file__).resolve().parent / WORKBOOK_NAME
    build(path)
    print(f"wrote {path}")
    if args.calibrate:
        print()
        return calibrate(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
