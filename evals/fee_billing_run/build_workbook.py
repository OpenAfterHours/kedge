"""Build the management-fee billing-run eval workbook.

The workbook this produces records a monthly process: work out what each client owes, apply the
floors, caps and discounts that were agreed, allocate the result to the entity that carries it,
and post the invoices. Ten tabs, laid out in the order somebody added them over four years --
which is deliberately not dependency order. See ``evals/proposals/fee_billing_run.md``.

What makes it an eval rather than a fixture is that the difficulty is *placed*. Ten
discriminations are planted, each so it can fail on its own; the proposal names them and this
module is where each one physically lives.

Two structural rules govern the whole file, and breaking either quietly destroys the eval.

**Complexity comes from variety, never from size.** R1C1 normalisation collapses a formula
filled down a column and across thirty columns to one logical operation, so a big uniform grid
is a *simple* workbook however many cells it has. ``WORKING_COLUMNS`` is therefore a list of
distinct shapes, and it is the complexity of the workbook almost by itself.

**The opposite error is just as easy.** A reference whose target moves per row -- a fill-down of
``=Positions!$A4``, ``=Positions!$A6``, ``=Positions!$A8`` -- normalises to a *different* R1C1
string on every row, so it never compresses and one column becomes eighty-four operations. The
first build of this workbook came in at 208 operations and complexity 0.855 for exactly that
reason. Both offenders are now written the way the process would really do it, and
``--calibrate`` is what caught it.

Columns are addressed **by name**, never by letter. Every formula template interpolates
``{some_key}`` and gets that column's letter, so inserting a column cannot silently repoint an
arithmetic chain at its neighbour. The same keys drive the Python model in ``compute``, which is
what makes the cached values a parallel implementation of the sheet rather than a copy of it.

Run it::

    uv run python evals/fee_billing_run/build_workbook.py
    uv run python evals/fee_billing_run/build_workbook.py --calibrate

Still to come: the ``Summary`` pivot, which openpyxl cannot author and which has to be built by
driving Excel over COM.
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
if str(FIXTURE_DIR) not in sys.path:
    sys.path.insert(0, str(FIXTURE_DIR))

# Imported rather than copied, for the reason CLAUDE.md gives about `kedge.xl`: a second copy of
# a rounding or normalisation routine is a second chance to get it subtly wrong. `excel_round`
# carries the 15-significant-digit collapse, and `normalise` has to run *after* the save --
# openpyxl assigns `properties.modified = datetime.now()` inside `save_workbook`, so a timestamp
# set on the workbook object is discarded and the file is reproducible only within one second.
from generate import (  # noqa: E402
    excel_round,
    inject_cached_values,
    normalise,
    read_parts,
    set_full_calc_on_load,
    write_parts,
)

WORKBOOK_NAME = "m11_management_fee_run.xlsx"

# The structural band the proposal commits to, asserted by `--calibrate` and by
# `tests/unit/test_evals_fee_billing_run.py`. A band rather than a floor because this eval can
# rot in both directions: too simple and it stresses nothing the corpus does not already stress,
# too complex and it triages to `stop`, at which point it cannot be converted and so cannot be
# graded on the quality of its conversion.
TARGET_OPERATIONS = (45, 60)
TARGET_PATTERNS = 12
TARGET_COMPLEXITY = (0.68, 0.75)
FORBIDDEN_VERDICT = "stop"

CLIENTS = 84
ENTITIES = ("LuxCo", "UKCo", "DubCo")
MANDATES = ("  balanced ", "equity", " fixed income")

PERIOD_START = dt.date(2026, 11, 1)
PERIOD_END = dt.date(2026, 11, 30)
EXCEL_EPOCH = dt.date(1899, 12, 30)
"""Excel's day zero. 1900 is treated as a leap year, which is why this is the 30th."""

MIN_FEE = 750.0
MAX_FEE = 250_000.0
DISCOUNT = 0.025
OLD_FLAT_BPS = 26.0
OLD_ADJUSTMENT = 0.97

BANDS: tuple[tuple[float, float, float], ...] = (
    (0.0, 5_000_000.0, 35.0),
    (5_000_000.0, 20_000_000.0, 27.5),
    (20_000_000.0, 50_000_000.0, 22.0),
    (50_000_000.0, 100_000_000.0, 18.5),
)
"""Tiered rate card, matched with an approximate ``VLOOKUP`` on the band floor.

Discrimination 1, and the highest-value translation risk in the eval: the polars equivalent is
``join_asof(strategy="backward")`` on a sorted frame, the boundary is ``>=`` on the floor, and
an off-by-one moves an entire tier of clients onto the wrong rate.
"""

NEGOTIATED_STEP = 5
"""Every fifth client has a negotiated rate that overrides the band.

Not every client, deliberately: if all of them had one the banded lookup would be dead code and
discrimination 1 would evaporate.
"""

SMALL_ACCOUNTS: dict[int, float] = {3: 120_000.0, 27: 185_000.0, 71: 96_500.0}
"""Client index to AUM, for the handful of accounts small enough that the minimum fee binds.

Without them the floor column is dead: an average AUM of tens of millions never produces a fee
anywhere near 750, so ``MAX(prorated, minimum)`` is the identity on every row and a conversion
that drops it entirely still reconciles. ``require_planted_discriminations`` refuses to build a
workbook where that is true, and it caught this on the first run.
"""

ONBOARDED_MID_PERIOD: dict[int, int] = {12: 12, 33: 20, 58: 6}
"""Client index to day of November they were onboarded. Everyone else bills a full month.

Discrimination 3. Without these the pro-rating columns compute ``fee * 30 / 30`` for every row
and a conversion that drops them entirely still reconciles.
"""

OVERRIDES: tuple[tuple[str, float, str, dt.date], ...] = (
    (
        "00007",
        0.0,
        "Fee waived for Q4 pending the mandate transfer agreed with the client in September.",
        dt.date(2026, 9, 18),
    ),
    (
        "00041",
        18_500.0,
        "Capped at the 2025 level; the uplift was not communicated before the billing run.",
        dt.date(2026, 10, 2),
    ),
    (
        "00062",
        4_220.75,
        "Manual correction: two accounts were double-counted in the position extract.",
        dt.date(2026, 11, 3),
    ),
)
"""Three computed fees the billing manager typed over, each with a written reason.

Discrimination 4, and the sharpest judgement call in the eval. Three answers are wrong in
different directions: reproducing the numbers bakes last quarter's decision into every future
month, dropping them loses three real decisions, and reconciling against them reports a pass for
a figure nobody calculated. The reasons are prose because that is what makes them re-askable.
"""

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

SQL_FIRST_ROW = 3
POSITIONS_HEADER_ROW = SQL_FIRST_ROW + len(EXTRACT_SQL.splitlines()) + 1
POSITIONS_FIRST_ROW = POSITIONS_HEADER_ROW + 1
POSITIONS_LAST_ROW = POSITIONS_FIRST_ROW + CLIENTS * 2 - 1

FEE_HEADER_ROW = 4
FEE_BAND_FIRST_ROW = 5
FEE_BAND_LAST_ROW = FEE_BAND_FIRST_ROW + len(BANDS) - 1
FEE_NEGOTIATED_FIRST_ROW = 5
FEE_NEGOTIATED_LAST_ROW = FEE_NEGOTIATED_FIRST_ROW + (CLIENTS - 1) // NEGOTIATED_STEP

WORKING_HEADER_ROW = 18
WORKING_FIRST_ROW = WORKING_HEADER_ROW + 1
WORKING_LAST_ROW = WORKING_FIRST_ROW + CLIENTS - 1
WORKING_TOTAL_ROW = WORKING_LAST_ROW + 2
OLD_RATE_CELL = "B14"
OLD_FACTOR_CELL = "B15"
OLD_RATE_REF = "$B$14"
OLD_FACTOR_REF = "$B$15"
"""Fully absolute, and that is not a style preference.

``$B14`` is column-absolute but *row-relative*, so filled down it normalises to ``R[-5]C2``,
``R[-6]C2``, ``R[-7]C2`` -- a different R1C1 string on every row. Two columns written that way
added 168 single-cell operations and pushed the workbook to 216. A parameter reference is
absolute in both axes, which is also what makes it classify as ``parameter_ref`` rather than as
arithmetic."""


def serial(value: dt.date) -> int:
    """A date as Excel stores it: days since 1899-12-30."""
    return (value - EXCEL_EPOCH).days


# =============================================================================
# THE FORMULA SHAPES
# =============================================================================


@dataclass(frozen=True, slots=True)
class Column:
    """One column of ``Working``: what it is called, and how it is calculated.

    ``formula`` is a template. ``{row}`` and ``{prev}`` are the current and previous row
    numbers; every other placeholder is a *column key* and resolves to that column's letter, so
    a chain of arithmetic says what it reads rather than encoding a letter that a later
    insertion would silently invalidate. An empty template means the column holds data.
    """

    key: str
    header: str
    formula: str = ""


BAND_RANGE = f"'Fee Schedule'!$D${FEE_BAND_FIRST_ROW}:$F${FEE_BAND_LAST_ROW}"
NEGOTIATED_RANGE = f"'Fee Schedule'!$A${FEE_NEGOTIATED_FIRST_ROW}:$B${FEE_NEGOTIATED_LAST_ROW}"
POSITIONS_VALUE_RANGE = f"Positions!$F${POSITIONS_FIRST_ROW}:$F${POSITIONS_LAST_ROW}"
POSITIONS_CODE_RANGE = f"Positions!$A${POSITIONS_FIRST_ROW}:$A${POSITIONS_LAST_ROW}"
"""Ranges the formula templates splice in.

Precomputed rather than interpolated inline: a template's ``{key}`` placeholders have to survive
untouched until :func:`formula_for` resolves them, so an f-string would need every one of them
doubled. Concatenating a ready-made range reads better and cannot get that wrong.
"""


WORKING_COLUMNS: tuple[Column, ...] = (
    # Data, not a formula, and that is load-bearing twice over. A person pastes the codes in --
    # and `=Positions!$A{n}` filled down would normalise to a different R1C1 string per row and
    # turn one column into eighty-four operations.
    Column("client", "Client"),
    Column(
        "client_name",
        "Client name",
        "=INDEX('Entity Map'!$B:$B,MATCH({client}{row},'Entity Map'!$A:$A,0))",
    ),
    Column(
        "onboarded",
        "Onboarded",
        "=INDEX('Entity Map'!$F:$F,MATCH({client}{row},'Entity Map'!$A:$A,0))",
    ),
    Column("period_start", "Period start", "=MAX(DATE(2026,11,1),{onboarded}{row})"),
    Column("period", "Period", '=TEXT({period_start}{row},"yyyy-mm")'),
    Column(
        "avg_aum",
        "Avg AUM (GBP)",
        "=AVERAGEIFS(Positions!$F:$F,Positions!$A:$A,{client}{row})",
    ),
    Column("prior_close", "Prior close", "={avg_aum}{prev}"),
    Column(
        "opening",
        "Opening balance",
        "=IF({client}{row}={client}{prev},{prior_close}{row},{avg_aum}{row})",
    ),
    # Discrimination 1. Approximate match: the band whose floor is the largest not exceeding the
    # AUM. `join_asof`, not `join`.
    Column(
        "band_bps",
        "Band bps",
        "=VLOOKUP({avg_aum}{row}," + BAND_RANGE + ",3,TRUE)",
    ),
    # Discrimination 8 is here rather than decorative: the negotiated rate arrives as *text*,
    # Excel coerces it in the arithmetic below, and polars will not.
    Column(
        "tier_bps",
        "Tier bps",
        "=IFERROR(VLOOKUP({client}{row}," + NEGOTIATED_RANGE + ",2,FALSE),{band_bps}{row})",
    ),
    Column(
        "legal_entity",
        "Legal entity",
        "=INDEX('Entity Map'!$C:$C,MATCH({client}{row},'Entity Map'!$A:$A,0))",
    ),
    Column(
        "cost_centre",
        "Cost centre",
        "=INDEX('Entity Map'!$D:$D,MATCH({client}{row},'Entity Map'!$A:$A,0))",
    ),
    Column("gross_fee", "Gross fee (GBP)", "=ROUND({avg_aum}{row}*{tier_bps}{row}/10000,2)"),
    Column("days_in_month", "Days in month", "=DAY(EOMONTH({period_start}{row},0))"),
    Column(
        "days_billed",
        "Days billed",
        '=DATEDIF({period_start}{row},EOMONTH({period_start}{row},0),"d")+1',
    ),
    Column(
        "prorated",
        "Pro-rated fee",
        "=ROUND({gross_fee}{row}*{days_billed}{row}/{days_in_month}{row},2)",
    ),
    Column("floored", "Floor applied", "=MAX({prorated}{row},'Fee Schedule'!$I$3)"),
    Column("capped", "Cap applied", "=MIN({floored}{row},'Fee Schedule'!$I$4)"),
    Column("discount", "Discount", "=IFERROR({capped}{row}*'Fee Schedule'!$I$5,0)"),
    Column("net_fee", "Net fee (GBP)", "=ROUND({capped}{row}-{discount}{row},2)"),
    # Discrimination 4: the workbook resolves the override silently, and a runbook must re-ask it.
    Column(
        "agreed_fee",
        "Agreed fee",
        "=IFERROR(VLOOKUP({client}{row},Overrides!$A:$C,3,FALSE),{net_fee}{row})",
    ),
    Column(
        "override_flag",
        "Override applied",
        '=IF({agreed_fee}{row}={net_fee}{row},"","OVERRIDE")',
    ),
    Column("invoice_key", "Invoice key", '=LEFT({client}{row},3)&"-"&{period}{row}'),
    Column("positions_seen", "Positions seen", "=COUNTIFS(Positions!$A:$A,{client}{row})"),
    Column("running_total", "Running total", "=SUM(${agreed_fee}${first}:{agreed_fee}{row})"),
    Column(
        "mandate",
        "Mandate",
        "=UPPER(TRIM(INDEX('Entity Map'!$E:$E,MATCH({client}{row},'Entity Map'!$A:$A,0))))",
    ),
    Column(
        "mandate_aum",
        "Mandate AUM",
        "=SUMIFS(Positions!$F:$F,Positions!$E:$E,{mandate}{row})",
    ),
    Column("share", "Share of mandate", "=IFERROR({avg_aum}{row}/{mandate_aum}{row},0)"),
    Column(
        "weighted_check",
        "Weighted check",
        "=SUMPRODUCT(" + POSITIONS_VALUE_RANGE + ",--(" + POSITIONS_CODE_RANGE + "={client}{row}))",
    ),
    Column(
        "fee_band",
        "Fee band",
        '=IF({agreed_fee}{row}>40000,"A",IF({agreed_fee}{row}>8000,"B","C"))',
    ),
    Column("accounts", "Accounts on file", "=COUNTIF('Entity Map'!$A:$A,{client}{row})"),
    Column("latest_asof", "Latest as-of", "=MAX(Positions!$C:$C)"),
    Column("billing_year", "Billing year", "=YEAR({period_start}{row})"),
    # The abandoned 2024 method. Real formulas, filled down like everything else, read by
    # nothing -- so they become `dead_region` findings, and they are the haystack that
    # discrimination 5 asks a conversion to find the `Post` column in.
    Column("old_fee", "Old method fee", "=ROUND({avg_aum}{row}*" + OLD_RATE_REF + "/10000,2)"),
    Column("old_delta", "Old method delta", "={old_fee}{row}-{net_fee}{row}"),
    Column("old_adj", "2024 adj", "={old_delta}{row}*" + OLD_FACTOR_REF),
    Column(
        "old_note",
        "2024 note",
        '=CONCATENATE({client}{row}," / ",TEXT({old_adj}{row},"0.00"))',
    ),
)

LETTERS: dict[str, str] = {
    column.key: get_column_letter(index) for index, column in enumerate(WORKING_COLUMNS, start=1)
}

TOTALLED = ("gross_fee", "prorated", "net_fee", "agreed_fee")
"""The columns the workbook sums under the grid. A totals row is its own logical operation and
has no column name for `infer_regions` to match, which is why the reconciliation map cannot be
left at the scaffolder's default for it."""


def formula_for(column: Column, row: int) -> str:
    """Render one cell's formula, resolving column keys to letters."""
    return column.formula.format(
        row=row, prev=max(WORKING_FIRST_ROW, row - 1), first=WORKING_FIRST_ROW, **LETTERS
    )


# =============================================================================
# THE PYTHON MODEL
# =============================================================================
#
# A parallel implementation of the sheet, not a transcription of it. These are the values Excel
# would leave in the cells, and they are what gets injected as the cached values that make the
# workbook reconcilable at all. Written against the same column keys as the formulas above so
# the two can be read side by side.


@dataclass(frozen=True, slots=True)
class Client:
    """One client's inputs: what the extract returned and what the reference data says."""

    index: int
    code: str
    name: str
    entity: str
    cost_centre: str
    mandate_raw: str
    onboarded: dt.date
    october_aum: float
    november_aum: float

    @property
    def negotiated_bps(self) -> str | None:
        """The rate as it arrives from the onboarding team: text, because Excel copies what a
        cell *looks like* rather than what it holds."""
        if self.index % NEGOTIATED_STEP:
            return None
        return f"{20 + (self.index % 9) * 1.5:.1f}"


def build_clients(rng: random.Random) -> list[Client]:
    """The client list, seeded and deterministic."""
    clients: list[Client] = []
    for index in range(CLIENTS):
        day = ONBOARDED_MID_PERIOD.get(index)
        onboarded = dt.date(2026, 11, day) if day is not None else dt.date(2024, 3, 1 + index % 27)
        small = SMALL_ACCOUNTS.get(index)
        october = small if small is not None else round(rng.uniform(250_000, 90_000_000), 2)
        november = (
            round(small * 1.04, 2)
            if small is not None
            else round(rng.uniform(250_000, 90_000_000), 2)
        )
        clients.append(
            Client(
                index=index,
                code=f"{index + 1:05d}",
                name=f"Client {index + 1:03d} Holdings",
                entity=ENTITIES[index % 3],
                cost_centre=f"CC-{100 + (index % 6) * 10}",
                mandate_raw=MANDATES[index % 3],
                onboarded=onboarded,
                october_aum=october,
                november_aum=november,
            )
        )
    return clients


def band_bps_for(aum: float) -> float:
    """Excel's approximate ``VLOOKUP``: the last band whose floor does not exceed the value."""
    chosen = BANDS[0][2]
    for floor, _ceiling, bps in BANDS:
        if aum >= floor:
            chosen = bps
    return chosen


def compute(clients: list[Client]) -> list[dict[str, Any]]:
    """Every cell of ``Working``, in the order Excel would evaluate it.

    Money goes through :func:`generate.excel_round` rather than :func:`round`, because Excel
    collapses an operand to 15 significant digits before rounding half away from zero and the
    difference is a penny that then propagates into every total below it.
    """
    override_by_code = {code: agreed for code, agreed, _reason, _on in OVERRIDES}
    mandate_totals: dict[str, float] = {}
    for client in clients:
        key = client.mandate_raw.strip().upper()
        mandate_totals[key] = (
            mandate_totals.get(key, 0.0) + client.october_aum + client.november_aum
        )

    rows: list[dict[str, Any]] = []
    running = 0.0
    for position, client in enumerate(clients):
        avg_aum = (client.october_aum + client.november_aum) / 2
        prior_close = avg_aum if position == 0 else rows[position - 1]["avg_aum"]
        period_start = max(PERIOD_START, client.onboarded)
        band_bps = band_bps_for(avg_aum)
        negotiated = client.negotiated_bps
        tier_bps = float(negotiated) if negotiated is not None else band_bps

        gross_fee = excel_round(avg_aum * tier_bps / 10000, 2)
        days_in_month = PERIOD_END.day
        days_billed = (PERIOD_END - period_start).days + 1
        prorated = excel_round(gross_fee * days_billed / days_in_month, 2)
        floored = max(prorated, MIN_FEE)
        capped = min(floored, MAX_FEE)
        discount = capped * DISCOUNT
        net_fee = excel_round(capped - discount, 2)
        agreed_fee = override_by_code.get(client.code, net_fee)
        running += agreed_fee

        mandate = client.mandate_raw.strip().upper()
        mandate_aum = mandate_totals[mandate]
        old_fee = excel_round(avg_aum * OLD_FLAT_BPS / 10000, 2)
        old_delta = old_fee - net_fee
        old_adj = old_delta * OLD_ADJUSTMENT

        rows.append(
            {
                "client": client.code,
                "client_name": client.name,
                "onboarded": serial(client.onboarded),
                "period_start": serial(period_start),
                "period": f"{period_start:%Y-%m}",
                "avg_aum": avg_aum,
                "prior_close": prior_close,
                "opening": prior_close if position == 0 else avg_aum,
                "band_bps": band_bps,
                "tier_bps": tier_bps,
                "legal_entity": client.entity,
                "cost_centre": client.cost_centre,
                "gross_fee": gross_fee,
                "days_in_month": days_in_month,
                "days_billed": days_billed,
                "prorated": prorated,
                "floored": floored,
                "capped": capped,
                "discount": discount,
                "net_fee": net_fee,
                "agreed_fee": agreed_fee,
                "override_flag": "" if agreed_fee == net_fee else "OVERRIDE",
                "invoice_key": f"{client.code[:3]}-{period_start:%Y-%m}",
                "positions_seen": 2,
                "running_total": excel_round(running, 2),
                "mandate": mandate,
                "mandate_aum": mandate_aum,
                "share": avg_aum / mandate_aum,
                "weighted_check": client.october_aum + client.november_aum,
                "fee_band": "A" if agreed_fee > 40000 else ("B" if agreed_fee > 8000 else "C"),
                "accounts": 1,
                "latest_asof": serial(dt.date(2026, 11, 28)),
                "billing_year": period_start.year,
                "old_fee": old_fee,
                "old_delta": old_delta,
                "old_adj": old_adj,
                # TEXT(x,"0.00") rounds the way Excel rounds, not the way format() does, so the
                # rounding goes through `excel_round` before it is formatted.
                "old_note": f"{client.code} / {excel_round(old_adj, 2):.2f}",
            }
        )
    return rows


def require_planted_discriminations(rows: list[dict[str, Any]]) -> None:
    """Refuse to build a workbook whose discriminations have gone.

    A seed change or a threshold tweak can remove one silently, leaving a file that opens fine
    and grades fine against everything except the thing it was built to measure.
    """
    if not any(row["days_billed"] < row["days_in_month"] for row in rows):
        msg = "no client is billed for a part month, so the pro-rating columns are a no-op"
        raise RuntimeError(msg)
    if len({row["band_bps"] for row in rows}) < 3:
        msg = "the banded lookup resolves to fewer than three bands, so it is barely a band"
        raise RuntimeError(msg)
    if not any(row["override_flag"] for row in rows):
        msg = "no override survived into the computed rows"
        raise RuntimeError(msg)
    if not any(row["floored"] > row["prorated"] for row in rows):
        msg = "the minimum fee never binds, so the floor column is dead"
        raise RuntimeError(msg)
    negotiated = sum(1 for row in rows if row["tier_bps"] != row["band_bps"])
    if not 5 <= negotiated <= len(rows) - 5:
        msg = f"{negotiated} negotiated rates: the band lookup and the override must both matter"
        raise RuntimeError(msg)


# =============================================================================
# SHEETS
# =============================================================================


def write_positions(sheet: Any, clients: list[Client]) -> None:
    """The warehouse extract: the query that produced it, then the grid it returned.

    The SQL sits in cells exactly as the original process kept it, so a conversion has something
    real to read when it decides this is a step to hand over rather than prose to summarise.
    """
    sheet["A1"] = "Positions extract -- run this against the warehouse, paste below"
    sheet["A1"].font = Font(bold=True)
    for offset, line in enumerate(EXTRACT_SQL.splitlines()):
        cell = sheet.cell(SQL_FIRST_ROW + offset, 1, line)
        cell.font = Font(name="Consolas", size=9)
        cell.alignment = Alignment(horizontal="left")

    headers = (
        "client_code",
        "client_name",
        "as_of_date",
        "period_month",
        "mandate",
        "market_value_gbp",
    )
    for index, header in enumerate(headers, start=1):
        sheet.cell(POSITIONS_HEADER_ROW, index, header).font = Font(bold=True)

    row = POSITIONS_FIRST_ROW
    for client in clients:
        for month, day, value in (
            ("2026-10", dt.date(2026, 10, 28), client.october_aum),
            ("2026-11", dt.date(2026, 11, 28), client.november_aum),
        ):
            sheet.cell(row, 1, client.code)
            sheet.cell(row, 2, client.name)
            sheet.cell(row, 3, day)
            sheet.cell(row, 4, month)
            sheet.cell(row, 5, client.mandate_raw.strip())
            sheet.cell(row, 6, value)
            row += 1


def write_fee_schedule(sheet: Any, clients: list[Client]) -> None:
    """The file the onboarding team emailed, pasted in.

    Two preamble rows and a header on row 4 -- the shape a pasted extract actually has. Two
    tables side by side: the banded rate card that most clients are on, and the negotiated rates
    that override it for a minority. Both have to be there, or one of them is dead code.

    The negotiated rate is written as **text**, which is discrimination 8 and not decoration:
    Excel copies what a cell looks like rather than what it holds, coerces it back to a number
    in the arithmetic downstream, and polars does not.
    """
    sheet["A1"] = "Fee schedule -- from Client Onboarding, effective 2026-11-01"
    sheet["A1"].font = Font(bold=True)
    sheet["A2"] = "Do not edit. Superseded schedules are kept on the shared drive."

    for column, header in (
        (1, "client_code"),
        (2, "negotiated_bps"),
        (4, "band_floor"),
        (5, "band_ceiling"),
        (6, "band_bps"),
    ):
        sheet.cell(FEE_HEADER_ROW, column, header).font = Font(bold=True)

    for offset, (floor, ceiling, bps) in enumerate(BANDS):
        row = FEE_BAND_FIRST_ROW + offset
        sheet.cell(row, 4, floor)
        sheet.cell(row, 5, ceiling)
        sheet.cell(row, 6, bps)

    row = FEE_NEGOTIATED_FIRST_ROW
    for client in clients:
        negotiated = client.negotiated_bps
        if negotiated is None:
            continue
        sheet.cell(row, 1, client.code)
        sheet.cell(row, 2, negotiated)
        row += 1

    for label_row, label, value in (
        (3, "Minimum fee", MIN_FEE),
        (4, "Maximum fee", MAX_FEE),
        (5, "Standard discount", DISCOUNT),
    ):
        sheet.cell(label_row, 8, label)
        sheet.cell(label_row, 9, value)


def write_entity_map(sheet: Any, clients: list[Client]) -> None:
    """Client to legal entity to cost centre, plus when each client came on.

    The eighth tab in the file and a dependency of the fourth, which is discrimination 10: tab
    order is the order somebody added things, never dependency order.
    """
    headers = ("client_code", "client_name", "legal_entity", "cost_centre", "mandate", "onboarded")
    for index, header in enumerate(headers, start=1):
        sheet.cell(1, index, header).font = Font(bold=True)
    for offset, client in enumerate(clients):
        row = 2 + offset
        sheet.cell(row, 1, client.code)
        sheet.cell(row, 2, client.name)
        sheet.cell(row, 3, client.entity)
        sheet.cell(row, 4, client.cost_centre)
        sheet.cell(row, 5, client.mandate_raw)
        sheet.cell(row, 6, client.onboarded)


def write_working(sheet: Any, clients: list[Client]) -> None:
    """The wide one: parameters, then a column per formula shape, filled down.

    This is where the workbook's complexity lives. See ``WORKING_COLUMNS``.
    """
    sheet["A1"] = "Management fee working -- November 2026"
    sheet["A1"].font = Font(bold=True, size=12)
    sheet["A3"] = "Period"
    sheet["B3"] = "2026-11"
    sheet["A4"] = "Prepared by"
    sheet["B4"] = "Billing Operations"
    sheet["A14"] = "2024 flat rate (bps)"
    sheet[OLD_RATE_CELL] = OLD_FLAT_BPS
    sheet["A15"] = "2024 adjustment factor"
    sheet[OLD_FACTOR_CELL] = OLD_ADJUSTMENT

    for index, column in enumerate(WORKING_COLUMNS, start=1):
        sheet.cell(WORKING_HEADER_ROW, index, column.header).font = Font(bold=True)

    for offset, client in enumerate(clients):
        row = WORKING_FIRST_ROW + offset
        for index, column in enumerate(WORKING_COLUMNS, start=1):
            if not column.formula:
                sheet.cell(row, index, client.code)
                continue
            sheet.cell(row, index, formula_for(column, row))

    sheet.cell(WORKING_TOTAL_ROW, 1, "TOTAL").font = Font(bold=True)
    for key in TOTALLED:
        letter = LETTERS[key]
        sheet.cell(
            WORKING_TOTAL_ROW,
            get_column_letter_index(letter),
            f"=SUM({letter}{WORKING_FIRST_ROW}:{letter}{WORKING_LAST_ROW})",
        )


def get_column_letter_index(letter: str) -> int:
    """Column letter to 1-based index. openpyxl has the inverse; this direction is trivial."""
    index = 0
    for character in letter:
        index = index * 26 + (ord(character) - ord("A") + 1)
    return index


def write_overrides(sheet: Any) -> None:
    """Three computed fees the billing manager typed over, each with a reason."""
    headers = ("client_code", "computed_fee", "agreed_fee", "reason", "agreed_on", "agreed_by")
    for index, header in enumerate(headers, start=1):
        sheet.cell(1, index, header).font = Font(bold=True)
    net = LETTERS["net_fee"]
    client = LETTERS["client"]
    for offset, (code, agreed, reason, agreed_on) in enumerate(OVERRIDES):
        row = 2 + offset
        sheet.cell(row, 1, code)
        sheet.cell(
            row,
            2,
            f"=INDEX(Working!${net}:${net},MATCH($A{row},Working!${client}:${client},0))",
        )
        sheet.cell(row, 3, agreed)
        sheet.cell(row, 4, reason)
        sheet.cell(row, 5, agreed_on)
        sheet.cell(row, 6, "Billing Operations")


def allocation_rows(clients: list[Client]) -> list[tuple[int, Client | None, str]]:
    """The Allocation grid's layout: client rows in entity blocks, each block subtotalled.

    Returns ``(row, client, entity)`` with ``client`` None on a subtotal row. Shared by the
    writer and the cached-value model so the two cannot drift.
    """
    layout: list[tuple[int, Client | None, str]] = []
    row = 2
    for entity in ENTITIES:
        for client in clients:
            if client.entity != entity:
                continue
            layout.append((row, client, entity))
            row += 1
        layout.append((row, None, entity))
        row += 1
    return layout


def write_allocation(sheet: Any, clients: list[Client]) -> None:
    """Fees aggregated to invoice grain, with a subtotal row per entity *inside* the grid.

    Discrimination 6. An embedded subtotal is not data, and a conversion that reads the grid flat
    double-counts every entity. ``SUBTOTAL`` rather than ``SUM`` because that is what the toolbar
    button produces, and because it excludes itself.
    """
    headers = ("client_code", "legal_entity", "cost_centre", "period", "fee_gbp")
    for index, header in enumerate(headers, start=1):
        sheet.cell(1, index, header).font = Font(bold=True)

    agreed = LETTERS["agreed_fee"]
    client_letter = LETTERS["client"]
    block_start = 2
    for row, client, entity in allocation_rows(clients):
        if client is None:
            sheet.cell(row, 2, f"{entity} subtotal").font = Font(bold=True)
            sheet.cell(row, 5, f"=SUBTOTAL(9,E{block_start}:E{row - 1})")
            block_start = row + 1
            continue
        sheet.cell(row, 1, client.code)
        sheet.cell(row, 2, entity)
        sheet.cell(row, 3, client.cost_centre)
        sheet.cell(row, 4, "2026-11")
        # Aggregated by key rather than by position: `=Working!$S{n}` would point at a different
        # row from each Allocation row and so refuse to compress, and a lookup is what an
        # allocation tab does anyway.
        sheet.cell(
            row,
            5,
            f"=SUMIFS(Working!${agreed}:${agreed},"
            f"Working!${client_letter}:${client_letter},$A{row})",
        )


def write_post(sheet: Any, clients: list[Client]) -> None:
    """One INSERT per invoice, built by concatenation. The manual carry.

    Nothing in the workbook reads this column, so it is a ``dead_region`` with fan-out zero --
    which is exactly what sorts it into the tail of the planner's context. It is also the step
    that posts the fees, so dropping it deletes the point of the process.
    """
    sheet["A1"] = "Run these against the ledger once the fee run is approved."
    sheet["A1"].font = Font(bold=True)
    sheet["A3"] = "statement"
    sheet["A3"].font = Font(bold=True)
    client = LETTERS["client"]
    period = LETTERS["period"]
    agreed = LETTERS["agreed_fee"]
    for offset in range(len(clients)):
        working_row = WORKING_FIRST_ROW + offset
        sheet.cell(
            4 + offset,
            1,
            '="INSERT INTO fin.fee_invoice (client_code, period_month, fee_gbp) VALUES ('
            f"'\"&Working!${client}{working_row}&\"', '\"&Working!${period}{working_row}"
            f'&"\', "&Working!${agreed}{working_row}&");"',
        )


def write_recon(sheet: Any) -> None:
    """Last month beside this month, with a variance column and typed commentary."""
    headers = (
        "legal_entity",
        "october_gbp",
        "november_gbp",
        "variance_gbp",
        "commentary",
        "variance_pct",
        "flag",
    )
    for index, header in enumerate(headers, start=1):
        sheet.cell(1, index, header).font = Font(bold=True)
    priors = (412_880.44, 388_100.02, 205_990.18)
    for offset, (entity, prior) in enumerate(zip(ENTITIES, priors, strict=True)):
        row = 2 + offset
        sheet.cell(row, 1, entity)
        sheet.cell(row, 2, prior)
        sheet.cell(row, 3, f"=SUMIF(Allocation!$B:$B,$A{row},Allocation!$E:$E)")
        sheet.cell(row, 4, f"=C{row}-B{row}")
        sheet.cell(row, 6, f"=IFERROR(D{row}/B{row},0)")
        sheet.cell(row, 7, f'=IF(ABS(F{row})>0.05,"REVIEW","")')
    sheet["E2"] = "Transfer in from the Geneva book; expected."
    sheet["E3"] = "Two mandates closed in October."
    sheet["E4"] = "Unexplained. Raised with the desk 2026-11-04."


def write_signoff(sheet: Any) -> None:
    """Purpose, background, known issues. The part nobody can reconstruct.

    ``WorkbookAnalysis.notes`` picks this up and the plan's ``Briefing`` is where it goes, prose
    citing its sources. Written rather than generated, because invented background in a finance
    notebook is confident, plausible and unattributable.

    The "known issues" entry is load-bearing for discrimination 9: the staleness of Allocation is
    recorded *here and nowhere else*, because no analyser can detect it from the cells.
    """
    sheet["A1"] = "Management fee billing run -- sign-off"
    sheet["A1"].font = Font(bold=True, size=12)

    entries = (
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
            "The Allocation tab is left on manual calculation because recalculating it over a "
            "full extract takes several minutes, so its figures are from before the overrides "
            "were agreed and do not match the Working tab. Recalculate before relying on them. "
            "Three clients have agreed fees that override the calculation -- see the Overrides "
            "tab, and confirm each one still applies before posting.",
        ),
        (
            "Cadence",
            "Monthly, on the third working day, after the position extract has been signed off "
            "by Middle Office.",
        ),
    )
    row = 3
    for heading, text in entries:
        sheet.cell(row, 1, heading).font = Font(bold=True)
        cell = sheet.cell(row + 1, 1, text)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        row += 3


def write_summary_placeholder(sheet: Any) -> None:
    """Where the pivot goes.

    openpyxl cannot author a pivot table, so the committed workbook has to be finished by driving
    Excel over COM. Until then this tab is empty and discrimination 7 is not yet reachable.
    """
    sheet["A1"] = "TODO(kedge): pivot over Allocation, built by Excel. See the proposal, 7.1."


# =============================================================================
# CACHED VALUES
# =============================================================================


def cached_values(clients: list[Client], rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """What Excel would have left in every formula cell.

    Without these the workbook has no reconciliation baseline at all: triage reports a blocker,
    ``infer_regions`` proposes nothing, and every region degrades to "not reconciled" -- which
    makes the whole reconciliation half of the rubric ungradeable.

    ``Allocation`` is the deliberate exception, and it is discrimination 9. Its values are real
    but **stale**: they are what the tab held before the three overrides were agreed, because the
    sheet is left on manual calculation. A conversion that reconciles against them and "fixes"
    itself until they match has adopted numbers Excel itself would disown; the correct answer is
    to declare the region not reproduced, with the reason, and report CHECKED WITH EXCEPTIONS.
    """
    working: dict[str, Any] = {}
    for offset, row in enumerate(rows):
        excel_row = WORKING_FIRST_ROW + offset
        for column in WORKING_COLUMNS:
            if not column.formula:
                continue
            working[f"{LETTERS[column.key]}{excel_row}"] = row[column.key]
    for key in TOTALLED:
        working[f"{LETTERS[key]}{WORKING_TOTAL_ROW}"] = excel_round(
            sum(row[key] for row in rows), 2
        )

    by_code = {row["client"]: row for row in rows}
    overrides = {
        f"B{2 + offset}": by_code[code]["net_fee"]
        for offset, (code, _agreed, _reason, _on) in enumerate(OVERRIDES)
    }

    # The stale allocation: pre-override fees, which is what "left on manual calculation" means.
    allocation: dict[str, Any] = {}
    block_total = 0.0
    for row_number, client, _entity in allocation_rows(clients):
        if client is None:
            allocation[f"E{row_number}"] = excel_round(block_total, 2)
            block_total = 0.0
            continue
        stale = by_code[client.code]["net_fee"]
        allocation[f"E{row_number}"] = stale
        block_total += stale

    recon: dict[str, Any] = {}
    priors = (412_880.44, 388_100.02, 205_990.18)
    for offset, (entity, prior) in enumerate(zip(ENTITIES, priors, strict=True)):
        row_number = 2 + offset
        november = excel_round(
            sum(
                by_code[client.code]["net_fee"]
                for _r, client, ent in allocation_rows(clients)
                if client is not None and ent == entity
            ),
            2,
        )
        variance = november - prior
        recon[f"C{row_number}"] = november
        recon[f"D{row_number}"] = variance
        recon[f"F{row_number}"] = variance / prior
        recon[f"G{row_number}"] = "REVIEW" if abs(variance / prior) > 0.05 else ""

    post = {
        f"A{4 + offset}": (
            "INSERT INTO fin.fee_invoice (client_code, period_month, fee_gbp) VALUES "
            f"('{row['client']}', '{row['period']}', {row['agreed_fee']});"
        )
        for offset, row in enumerate(rows)
    }

    return {
        "Working": working,
        "Overrides": overrides,
        "Allocation": allocation,
        "Recon": recon,
        "Post": post,
    }


# =============================================================================
# BUILD
# =============================================================================


def build(path: Path) -> None:
    """Write the workbook, cached values included. Deterministic: nothing reads the clock."""
    rng = random.Random(20261103)
    clients = build_clients(rng)
    rows = compute(clients)
    require_planted_discriminations(rows)

    wb = Workbook()
    # Tab order is the order somebody added things over four years, which is discrimination 10.
    positions = wb.active
    positions.title = "Positions"
    write_positions(positions, clients)
    write_fee_schedule(wb.create_sheet("Fee Schedule"), clients)
    write_working(wb.create_sheet("Working"), clients)
    write_overrides(wb.create_sheet("Overrides"))
    write_allocation(wb.create_sheet("Allocation"), clients)
    write_summary_placeholder(wb.create_sheet("Summary"))
    write_post(wb.create_sheet("Post"), clients)
    write_entity_map(wb.create_sheet("Entity Map"), clients)
    write_recon(wb.create_sheet("Recon"))
    write_signoff(wb.create_sheet("Sign-off"))

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)

    parts = read_parts(path)
    populated = inject_cached_values(parts, cached_values(clients, rows))
    # False, so Excel trusts what is cached rather than rebuilding it on open. That is what makes
    # the stale Allocation figures survive to be found.
    set_full_calc_on_load(parts, False)
    write_parts(path, parts)
    normalise(path)

    total = sum(populated.values())
    if total < 3_000:
        msg = f"only {total} cached values landed; the workbook will not reconcile"
        raise RuntimeError(msg)


def calibrate(path: Path) -> int:
    """Measure the built workbook against the structural band, and say which way it missed."""
    from kedge.analysis.analyse import analyse
    from kedge.analysis.workbook import open_workbook
    from kedge.plan.propose import build_proposal_context
    from kedge.plan.triage import complexity, triage
    from kedge.reconcile.baseline import infer_regions

    analysis = analyse(path)
    result = triage(analysis)
    score = complexity(analysis)
    patterns = sorted({op.excel_pattern.value for op in analysis.operations})
    cross_sheet = sum(1 for op in analysis.operations if any(r.sheet for r in op.references))
    dead = [f for f in analysis.findings if f.kind.value == "dead_region"]
    reconcilable = [op for op in analysis.operations if op.cached_values_present]
    context = build_proposal_context(analysis, result)
    ranked = sorted(analysis.operations, key=lambda op: (-op.downstream_ref_count, op.id))
    carry = next((op for op in analysis.operations if op.sheet == "Post"), None)
    with open_workbook(path) as handle:
        regions = infer_regions(handle, analysis)

    print(f"workbook            {path.name}  ({path.stat().st_size:,} bytes)")
    print(f"operations          {len(analysis.operations):>6}   target {TARGET_OPERATIONS}")
    print(f"distinct patterns   {len(patterns):>6}   target >= {TARGET_PATTERNS}")
    print(f"complexity          {score:>6.3f}   target {TARGET_COMPLEXITY}")
    print(f"verdict             {result.verdict.value:>6}   must not be {FORBIDDEN_VERDICT!r}")
    print(f"convertible         {result.convertible:>6.2f}")
    print(f"reconcilable ops    {len(reconcilable):>6}   of {len(analysis.operations)}")
    print(f"inferred regions    {len(regions):>6}")
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
            f"manual carry        ranks {position} of {len(ranked)}  (fan-out {carry.downstream_ref_count})"
        )
    else:
        print("manual carry        NOT FOUND -- the Post column did not become an operation")
    print(f"digest truncation   {[k for k in context if k.endswith('_omitted')] or 'none'}")
    for blocker in result.blockers:
        print(f"  blocker: {blocker.render()}")

    problems: list[str] = []
    if not TARGET_OPERATIONS[0] <= len(analysis.operations) <= TARGET_OPERATIONS[1]:
        problems.append(f"operations {len(analysis.operations)} outside {TARGET_OPERATIONS}")
    if len(patterns) < TARGET_PATTERNS:
        problems.append(f"only {len(patterns)} distinct patterns, want >= {TARGET_PATTERNS}")
    if not TARGET_COMPLEXITY[0] <= score <= TARGET_COMPLEXITY[1]:
        problems.append(f"complexity {score:.3f} outside {TARGET_COMPLEXITY}")
    if result.verdict.value == FORBIDDEN_VERDICT:
        problems.append("triage says stop, so the workbook cannot be converted or graded")
    if not regions:
        problems.append("no reconcilable regions, so the reconciliation rubric is ungradeable")

    print()
    if problems:
        for problem in problems:
            print(f"MISS  {problem}")
        return 1
    print("IN BAND")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the fee-billing-run eval workbook.")
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
