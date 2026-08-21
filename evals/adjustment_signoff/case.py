"""Grading the adjustment eval: what to feed the runbook, and what to check when it stops.

Every grader here is keyed to an id in ``expected.yaml``, and
``tests/unit/test_evals_harness.py`` asserts the two sets are identical in both directions. That
link is the whole reason a rubric written in prose is worth having: the prose says what is being
checked and why, the code says how, and neither can drift without the other going red.

**The hand-ins are derived from the workbook, never committed beside it.** ``Pre-Adjustment`` and
``Post-Adjustment`` are already the two grids a user would bring back, so the harness reads them
out and writes them to a temporary directory as CSV. Committing copies would mean two sources of
truth for the same 120 rows and one of them going stale.

**Deterministic graders compare at a tolerance, never with ``==``.** polars' vectorised execution
of the rounding chain lands a few parts in 1e11 away from the scalar path, which is far inside
the penny that matters and far outside float equality. A grader written with ``==`` would fail a
correct notebook, and the obvious fix -- loosening the expected value -- would be the wrong one.
:data:`PENNY` is the threshold, and it is half a penny because that is the smallest difference
anybody would act on.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harness.model import ItemResult, Outcome
from openpyxl import load_workbook

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from harness.drive import NotebookRun

logger = logging.getLogger(__name__)

CASE_DIR = Path(__file__).resolve().parent
WORKBOOK = CASE_DIR / "q2_accrual_adjustment.xlsx"
REFERENCE_NOTEBOOK = CASE_DIR / "notebook.py"
RUBRIC = CASE_DIR / "expected.yaml"

DATA_HEADER_ROW = 18
"""Where the extract tabs' header sits. Rows 3-16 hold the query, which a result grid would not."""

DATA_ROWS = 120

PENNY = 0.005
"""Half a penny: the tolerance every money comparison here uses. See the module docstring."""

BLANK_TRADE = "ACC-00001"
"""The trade with no accrual. Excel predicts 0.00 from the blank; the warehouse keeps NULL."""

APOSTROPHE_TRADE = "ACC-00041"
"""``O'Brien & Partners``. The workbook's own concatenated statement for this row is invalid."""


# =============================================================================
# WHAT THE RUNBOOK IS FED
# =============================================================================


def grid_csv(sheet_name: str) -> str:
    """One extract tab as the CSV a user would bring back from the query client.

    Only the data region: the query text above it is workbook furniture, not something a result
    grid carries. Dates are rendered ISO because that is what every client emits and what the
    paste path would receive.
    """
    workbook = load_workbook(WORKBOOK, data_only=True)
    sheet = workbook[sheet_name]
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    for row in sheet.iter_rows(
        min_row=DATA_HEADER_ROW, max_row=DATA_HEADER_ROW + DATA_ROWS, max_col=7
    ):
        writer.writerow([_cell_text(cell.value) for cell in row])
    return buffer.getvalue()


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value)


def excel_style_paste(sheet_name: str) -> str:
    """One extract tab as Excel puts it on the clipboard: displayed text, tab-delimited.

    The difference from :func:`grid_csv` is the whole point. A file written by a query tool
    carries values; a copy out of a spreadsheet carries what the cell *looked like*, so a column
    formatted ``#,##0.00`` arrives as ``364,422.95`` and a date as ``30/06/2026``. That is the
    paste a user actually makes, and it is the one that broke.
    """
    rows = list(csv.reader(io.StringIO(grid_csv(sheet_name))))
    lines: list[str] = []
    for index, row in enumerate(rows):
        if index == 0:
            lines.append("\t".join(row))
            continue
        rendered = list(row)
        if rendered[4]:
            rendered[4] = f"{float(rendered[4]):,.2f}"
        if rendered[5]:
            rendered[5] = dt.date.fromisoformat(rendered[5]).strftime("%d/%m/%Y")
        lines.append("\t".join(rendered))
    return "\n".join(lines)


def write_handins(directory: Path) -> tuple[Path, Path]:
    """Write the two grids the runbook will ask for, and return their paths."""
    directory.mkdir(parents=True, exist_ok=True)
    pre = directory / "pre_adjustment.csv"
    post = directory / "post_adjustment.csv"
    pre.write_text(grid_csv("Pre-Adjustment"), encoding="utf-8")
    post.write_text(grid_csv("Post-Adjustment"), encoding="utf-8")
    return pre, post


def script_for(pre: Path, post: Path) -> dict[str, Any]:
    """The human's part, played the same way every run.

    Keyed by the variable each widget is assigned to. A notebook that names its widgets
    differently will not be driven by this -- which is a real limitation of grading a runbook
    headlessly, and is reported as unused inputs rather than silently scored as a stop.
    """
    return {
        "period_end": dt.date(2026, 6, 30),
        "ledger": "STATUTORY",
        "pre_adjustment_pick": (pre,),
        "approve_adjustment_decision": "approve",
        "approve_adjustment_note": (
            "Uplift agreed at the June finance committee; scope checked against the statement."
        ),
        "update_statement_ran": True,
        "update_statement_ran_note": "76 rows affected, ticket FC-2291.",
        "post_adjustment_pick": (post,),
    }


# =============================================================================
# CONTEXT
# =============================================================================


@dataclass(frozen=True)
class Context:
    """Everything a grader is allowed to look at."""

    run: NotebookRun
    facts: Mapping[str, Any]
    notebook: Path
    unused_inputs: tuple[str, ...] = ()
    plan: Any = None
    notes: list[str] = field(default_factory=list)

    @property
    def defs(self) -> dict[str, Any]:
        return self.run.definitions

    def need(self, *names: str) -> ItemResult | None:
        """A skip naming what is missing, or ``None`` when everything is there.

        A notebook that stopped early has not *failed* the items about cells it never reached --
        it failed the item about stopping. Marking the rest as failures would report one problem
        five times and bury it.
        """
        absent = [name for name in names if name not in self.defs]
        if not absent:
            return None
        return _skip(
            f"the notebook defines no {', '.join(absent)}. "
            + (
                f"It {self.run.summary_line()}."
                if not self.run.completed
                else "It ran to completion, so this is a naming difference rather than a stop."
            )
        )


def _pass(detail: str = "") -> ItemResult:
    return ItemResult(id="", outcome=Outcome.PASS, detail=detail)


def _fail(detail: str) -> ItemResult:
    return ItemResult(id="", outcome=Outcome.FAIL, detail=detail)


def _skip(detail: str) -> ItemResult:
    return ItemResult(id="", outcome=Outcome.SKIP, detail=detail)


def _close(actual: float, expected: float) -> bool:
    return abs(actual - expected) < PENNY


# =============================================================================
# TIER 1 - DETERMINISTIC
# =============================================================================


def ran_to_completion(ctx: Context) -> ItemResult:
    """Nothing else is meaningful if the runbook never reached the end."""
    if ctx.unused_inputs:
        return _fail(
            f"the script named widgets the notebook does not have: "
            f"{', '.join(ctx.unused_inputs)}. Either the notebook names them differently or a "
            f"step is missing; nothing below was driven as intended."
        )
    if not ctx.run.completed:
        return _fail(ctx.run.summary_line())
    return _pass(f"{len(ctx.run.cells_run)} cells")


def totals_to_the_penny(ctx: Context) -> ItemResult:
    missing = ctx.need("adjust_totals")
    if missing:
        return missing
    totals = ctx.defs["adjust_totals"]
    try:
        got = {name: float(totals[name][0]) for name in ("before", "uplift", "after")}
    except (KeyError, IndexError, TypeError) as error:
        return _fail(f"adjust_totals is not a frame with before/uplift/after columns: {error}")

    expected = {
        "before": float(ctx.facts["accrual_total_before"]),
        "uplift": float(ctx.facts["uplift_total"]),
        "after": float(ctx.facts["accrual_total_after"]),
    }
    wrong = {
        name: (got[name], value) for name, value in expected.items() if not _close(got[name], value)
    }
    if wrong:
        lines = [
            f"{name}: expected {want:,.2f}, got {have:,.2f} (out by {have - want:+,.2f})"
            for name, (have, want) in wrong.items()
        ]
        return _fail(
            "\n".join(lines)
            + "\nFour of the 76 rows need Excel's 15-significant-digit collapse before "
            "rounding. A bare .round(2) is a penny out on each."
        )
    return _pass(f"before {got['before']:,.2f}, uplift {got['uplift']:,.2f}")


def reconciles_against_the_workbook(ctx: Context) -> ItemResult:
    """Every region the notebook claims must pass, and none may fail.

    Deliberately not "the overall status is PASSED". ``Adjustment!G`` holds the statements the
    workbook built by concatenation, one of which is invalid SQL; a good conversion does not
    reproduce it, so the report's overall status is honestly ``not_reconciled``. Requiring PASSED
    would fail the right answer, and quietly dropping the unmapped region from the denominator
    would be the false pass this project exists not to produce.
    """
    missing = ctx.need("reconciliation")
    if missing:
        return missing
    check = ctx.defs["reconciliation"]
    # The notebook's value is a TranslationCheck now: reconciliation is an acceptance test on
    # the conversion, not a live comparison on every run. `.report` is the live one when there
    # was one, which at conversion time -- which is what this grader drives -- there always is.
    report = getattr(check, "report", check)
    if report is None:
        return _fail(
            "nothing was compared against the workbook on the very first run of a freshly "
            "converted notebook. That run *is* the acceptance test; there is nothing to cite yet."
        )
    regions = list(getattr(report, "regions", []))
    if not regions:
        return _fail("the reconciliation report has no regions, so nothing was compared")

    failed = [region for region in regions if region.status.value == "failed"]
    passed = [region for region in regions if region.status.value == "passed"]
    if failed:
        lines = [f"{region.reference}: {region.status.value}" for region in failed]
        return _fail("regions disagreeing with the workbook's cached values:\n" + "\n".join(lines))
    if not passed:
        return _fail(
            "no region was compared at all. The workbook carries a full cache, so this means "
            "the notebook mapped nothing -- not that there was no baseline."
        )
    unreconciled = [region for region in regions if region.status.value == "not_reconciled"]
    detail = f"{len(passed)} region(s) passed, none failed"
    if unreconciled:
        detail += "; unmapped: " + ", ".join(region.reference for region in unreconciled)
    return _pass(detail)


def generated_sql_is_valid(ctx: Context) -> ItemResult:
    """Execute the statements the notebook generated and check they do what it predicted.

    Graded by running them, not by reading them. The row this catches is ``ACC-00041``, whose
    entity is ``O'Brien & Partners``: the workbook's own statement for it does not parse, and a
    conversion that reproduced the concatenation faithfully would reproduce that. A string
    comparison would have to know what correct escaping looks like; an engine already does.

    Compared against the notebook's own prediction rather than against ``Post-Adjustment``,
    because those are two different claims. This one is "the SQL says what the notebook worked
    out"; whether the warehouse then agreed is
    :func:`verification_finds_exactly_one_break`.
    """
    try:
        import duckdb
    except ImportError:
        return _skip(
            "duckdb is not installed, so the generated SQL was not executed. "
            "Install it with `uv sync --group evals`."
        )

    missing = ctx.need("update_statement", "adjust")
    if missing:
        return missing

    statements = str(ctx.defs["update_statement"])
    if not statements.strip():
        return _fail("the notebook generated no statements")
    predicted = ctx.defs["adjust"]
    predicted = predicted.collect() if hasattr(predicted, "collect") else predicted

    connection = duckdb.connect()
    try:
        connection.execute(
            "CREATE TABLE fin_accruals (trade_id VARCHAR, accrual_gbp DOUBLE, "
            "adjustment_note VARCHAR)"
        )
        rows = [
            (row["trade_id"], row["accrual_gbp_before"], None)
            for row in predicted.iter_rows(named=True)
        ]
        connection.executemany("INSERT INTO fin_accruals VALUES (?, ?, ?)", rows)
        # The notebook writes `fin.accruals`; DuckDB has no such schema here, and creating one
        # would be pretending the eval knows the warehouse's layout. Rewriting the qualifier is
        # the smallest possible change and touches nothing the grader is about.
        executed = 0
        for statement in statements.splitlines():
            text = statement.strip()
            if not text or text.startswith("--"):
                continue
            connection.execute(text.replace("fin.accruals", "fin_accruals"))
            executed += 1
        result = connection.execute(
            "SELECT trade_id, accrual_gbp, adjustment_note FROM fin_accruals ORDER BY trade_id"
        ).fetchall()
    except Exception as error:
        return _fail(
            f"the generated SQL did not execute: {type(error).__name__}: {error}\n"
            f"The row to look at is {APOSTROPHE_TRADE} -- its entity name contains an "
            f"apostrophe, and `&` in Excel escapes nothing."
        )
    finally:
        connection.close()

    applied = {trade: (value, note) for trade, value, note in result}
    wrong = [
        f"{row['trade_id']}: notebook computed {row['accrual_gbp_after']:,.2f}, "
        f"its own SQL produced {applied.get(row['trade_id'], (None, None))[0]}"
        for row in predicted.iter_rows(named=True)
        if not _close(
            applied.get(row["trade_id"], (float("nan"), None))[0] or 0.0, row["accrual_gbp_after"]
        )
    ]
    if wrong:
        return _fail(
            f"{len(wrong)} statement(s) did not apply the value the notebook computed:\n"
            + "\n".join(wrong[:5])
        )
    if APOSTROPHE_TRADE not in applied:
        return _fail(
            f"no statement reached {APOSTROPHE_TRADE}, the row whose entity name carries an "
            f"apostrophe. That is the row this check exists for."
        )
    note = applied[APOSTROPHE_TRADE][1] or ""
    if "O'Brien" not in note:
        return _fail(
            f"{APOSTROPHE_TRADE} was updated but its note came back as {note!r}, so the "
            f"apostrophe was mangled rather than escaped."
        )
    return _pass(f"{executed} statements executed, including {APOSTROPHE_TRADE}")


def null_is_not_zero(ctx: Context) -> ItemResult:
    """The workbook says 0.00 for the blank accrual; the notebook must say so too.

    Reproducing Excel is the whole job, and this is the row where doing so feels wrong. Excel
    read the empty cell as zero inside ROUND, so the workbook's cache holds 0.00 and any
    notebook that "improves" on it by predicting NULL stops reconciling. The place to notice the
    problem is the verification against the re-extract, not here.
    """
    missing = ctx.need("adjust")
    if missing:
        return missing
    frame = ctx.defs["adjust"]
    frame = frame.collect() if hasattr(frame, "collect") else frame
    row = next(
        (item for item in frame.iter_rows(named=True) if item["trade_id"] == BLANK_TRADE), None
    )
    if row is None:
        return _fail(
            f"{BLANK_TRADE} is not in the adjusted set. It has no accrual, but the UPDATE's "
            f"WHERE clause still reaches it -- dropping it silently loses a row the statement "
            f"touches."
        )
    after = row.get("accrual_gbp_after")
    if after is None:
        return _fail(
            f"{BLANK_TRADE} came out as null. Excel reads the blank cell as zero inside ROUND, "
            f"so the workbook's cached value is 0.00 and this will not reconcile. Route the "
            f"column through .xl.empty_as_zero() rather than letting polars propagate the null."
        )
    if not _close(float(after), 0.0):
        return _fail(f"{BLANK_TRADE} came out as {after}, where the workbook holds 0.00")
    return _pass(f"{BLANK_TRADE} predicted at 0.00, matching the workbook")


def verification_finds_exactly_one_break(ctx: Context) -> ItemResult:
    """The dangerous direction is a pass. This is the row a total-level check never finds."""
    missing = ctx.need("verification")
    if missing:
        return missing
    report = ctx.defs["verification"]
    if getattr(report, "ok", False):
        return _fail(
            "the verification passed. It should not: the warehouse left "
            f"{BLANK_TRADE} NULL where the notebook predicted 0.00. A pass here is a signed "
            "claim that production holds what the notebook says it holds."
        )
    if report.status.value == "not_reconciled":
        return _fail(
            f"nothing was verified: {report.reason}. Not verifying is not the same as "
            f"verifying and finding a break, and only one of them is the right answer here."
        )
    breaks = list(getattr(report, "breaks", ()))
    if report.break_count != 1:
        return _fail(
            f"expected exactly 1 break, got {report.break_count} "
            f"({report.missing_count} missing, {report.unexpected_count} unexpected). "
            f"First few: {[item.key for item in breaks[:3]]}"
        )
    if breaks and breaks[0].key != (BLANK_TRADE,):
        return _fail(f"the break is on {breaks[0].key}, expected ({BLANK_TRADE!r},)")
    return _pass(f"one break, on {BLANK_TRADE}, as designed")


def cancelled_rows_excluded(ctx: Context) -> ItemResult:
    missing = ctx.need("adjust")
    if missing:
        return missing
    frame = ctx.defs["adjust"]
    frame = frame.collect() if hasattr(frame, "collect") else frame
    expected = int(ctx.facts["rows_the_update_reaches"])
    if frame.height != expected:
        return _fail(
            f"{frame.height} rows adjusted, expected {expected}. Four trades inside the "
            f"in-scope entities are CANCELLED, and the UPDATE's WHERE clause excludes them."
        )
    return _pass(f"{expected} rows")


def no_pandas(ctx: Context) -> ItemResult:
    """The validation gate over every cell of the notebook as written."""
    from kedge.agent.validate import validate_cell
    from kedge.notebook.codegen import read_notebook

    document = read_notebook(ctx.notebook)
    offenders: list[str] = []
    for index, cell in enumerate(document.cells):
        report = validate_cell(cell.code, cell=cell.name or str(index))
        offenders.extend(
            f"{cell.name or index}: {violation.message}"
            for violation in report.violations
            if not violation.message.startswith("no name registry")
        )
    if offenders:
        return _fail("\n".join(offenders[:8]))
    return _pass(f"{len(document.cells)} cells")


# =============================================================================
# THE RUNBOOK AS A USER MEETS IT
# =============================================================================
#
# Everything above grades the *result* of a completed run. These grade the experience of
# working through one, and every one of them exists because a real user hit the problem in app
# mode while the deterministic tier was reporting 38/38. A conversion can be numerically perfect
# and still be a page that stalls with no explanation.

STEPS_BEFORE_ANY_INPUT = ("extract_query", "pre_adjustment_input")
"""What a user should see on opening a fresh notebook: the first statement, and where to reply."""

MUST_BE_HIDDEN_AT_THE_START = (
    "post_adjustment_input",
    "post_adjustment_paste",
    "update_statement",
    "update_statement_ran",
)
"""What must not be on screen before it is that step's turn.

``post_adjustment_input`` is the one this list was written for. A selector that constructs
widgets and reads nothing has no dataflow edge, so marimo cannot gate it -- it renders from the
moment the notebook opens, and invites a re-extract taken before the update has been run. There
is no way to detect that afterwards, which is what makes it worth a grader of its own.
"""


def _drive(ctx: Context, inputs: dict[str, Any], root: Path) -> Any:
    """Drive the notebook again, in a workspace of this grader's own."""
    from harness.drive import run_notebook, workspace_overrides

    return run_notebook(
        ctx.notebook,
        inputs=inputs,
        overrides=workspace_overrides(root, WORKBOOK),
    )


def progressive_disclosure(ctx: Context) -> ItemResult:
    """Nothing is on screen before its turn, and the first step is."""
    from harness.drive import visible_cells, workspace_overrides

    with tempfile.TemporaryDirectory(prefix="kedge-eval-visible-") as workspace:
        root = Path(workspace)
        seen = set(
            visible_cells(
                ctx.notebook,
                inputs={},
                overrides=workspace_overrides(root, WORKBOOK),
            )
        )

    early = [name for name in MUST_BE_HIDDEN_AT_THE_START if name in seen]
    if early:
        return _fail(
            f"visible on a fresh open, before their step: {', '.join(early)}.\n"
            f"A cell that only constructs widgets reads nothing, so marimo has no edge to hide "
            f"it on. Have it read the token of the step before it."
        )
    missing = [name for name in STEPS_BEFORE_ANY_INPUT if name not in seen]
    if missing:
        return _fail(
            f"not visible on a fresh open: {', '.join(missing)}. A runbook that shows the user "
            f"nothing to do has stalled, whatever it is waiting for."
        )
    return _pass(f"{len(seen)} cell(s) shown; the rest wait their turn")


def a_blocked_step_says_which_step_it_is(ctx: Context) -> ItemResult:
    """In app mode a stopped cell is the only thing left on the page. It has to explain itself.

    Everything below a stop disappears, with nothing to distinguish "waiting for you", "broken"
    and "finished". The stop message is the entire user interface at that moment, so it has to
    carry the position as well as the instruction.
    """
    with tempfile.TemporaryDirectory(prefix="kedge-eval-blocked-") as workspace:
        run = _drive(ctx, {"period_end": dt.date(2026, 6, 30)}, Path(workspace))

    if run.completed:
        return _fail("the notebook ran to completion with no hand-in supplied, so nothing blocked")
    message = run.stopped_because or ""
    if not message.strip():
        return _fail(
            f"the notebook stopped at {run.stopped_at!r} with an empty message. In app mode "
            f"that is a blank page."
        )
    if not re.search(r"step\s+\d+\s+of\s+\d+", message, re.IGNORECASE):
        return _fail(
            f"the blocking message does not say which step it is:\n  {message[:200]}\n"
            f"Without it the user cannot tell a page that is waiting from one that has died."
        )
    if not re.search(r"\b(run|drop|select|paste|type|choose|tick|enter|fill)\b", message, re.I):
        return _fail(
            f"the blocking message explains itself but never says what to do:\n"
            f"  {message[:200]}\n"
            f"A user reading that is stuck. Lead with the instruction; put the reason after it."
        )
    return _pass(message.split(".")[0])


def a_declared_exception_does_not_read_as_a_defect(ctx: Context) -> ItemResult:
    """A region the notebook deliberately does not reproduce must not send anyone bug-hunting.

    ``Adjustment!G`` is the workbook's own concatenated UPDATE statements, one of which is
    invalid SQL. A good conversion renders them properly instead, so there is nothing to
    reconcile -- and the default message for an unmapped region ("the notebook produced no
    values for this region, check that the cell ran and that the variable names match") is then
    actively misleading.

    It is still not a pass. Nothing here can make an unchecked region one, and this insists on
    that as firmly as it insists on the wording.

    And an exception is not a break. The headline check used to be the single negative
    ``startswith("NOT RECONCILED")``, which nothing else qualified -- so a report reading
    "FAILED - 32 passed, 1 failed" satisfied it and scored two points for handling
    reconciliation honestly while carrying an unexplained mismatch, and a report that compared
    *nothing* and declared its one region rendered "CHECKED WITH EXCEPTIONS - 0 of 0 claimed
    regions passed" and scored the same. Neither consulted
    :attr:`~kedge.reconcile.ReconciliationReport.failed`, and FAILED is the most severe status a
    report can hold. So the claims are now made positively: something must actually have been
    compared and matched, no region may be in ``report.failed``, and the headline must *read*
    CHECKED WITH EXCEPTIONS rather than merely avoid one particular prefix. A region that cannot
    be reproduced is a decision and is declared as one; a region that mismatches is a defect,
    and this is the item that has to tell them apart. The same defect was found and fixed in
    ``fee_billing_run``'s :func:`stale_region_is_not_reconciled`.
    """
    missing = ctx.need("reconciliation")
    if missing:
        return missing
    check = ctx.defs["reconciliation"]
    report = getattr(check, "report", check)
    if report is None:
        return _skip("this run cited a recorded acceptance, so there are no live regions")
    regions = list(getattr(report, "regions", []))
    region = next(
        (item for item in regions if "G17" in (item.reference or "")),
        None,
    )
    if region is None:
        return _skip("the report has no region for Adjustment!G, so there is nothing to judge")
    if region.status.value == "passed":
        return _fail(
            "Adjustment!G is reported as passed. Nothing was compared against it -- an "
            "unchecked region is never a pass, however deliberate the omission."
        )
    if getattr(region.reason, "value", None) != "not_reproduced":
        return _fail(
            f"Adjustment!G is reported as {getattr(region.reason, 'value', None)!r}, which "
            f"reads as a cell that failed to run. Declare it with `not_reproduced=` so the "
            f"panel says it was a decision, and why."
        )
    if "kedge.sql" not in region.detail and "invalid" not in region.detail.lower():
        return _fail("the region is declared, but the reason given explains nothing")

    broken = report.failed
    if broken:
        lines = [
            f"{item.reference or item.spec_id}: {item.rows_differing} of "
            f"{item.rows_compared} row(s) differ"
            for item in broken
        ]
        return _fail(
            f"Adjustment!G is declared, but {len(broken)} of {len(regions)} region(s) came out "
            f"FAILED:\n  "
            + "\n  ".join(lines[:6])
            + (f"\n  ... and {len(lines) - 6} more" if len(lines) > 6 else "")
            + f"\nThe panel therefore leads with {report.headline().split('.')[0]!r}, which is "
            f"the most severe thing a reconciliation report can say. A region that cannot be "
            f"reproduced is a decision, declared with a reason the way Adjustment!G is; a "
            f"region that mismatches is a defect, and declaring Adjustment!G correctly says "
            f"nothing about it. Either the arithmetic disagrees with the workbook and wants "
            f"fixing, or the workbook is the thing that is wrong -- in which case say so, with "
            f"`not_reproduced=` and the reason why, and it stops being a break."
        )
    if not report.passed or report.rows_compared <= 0:
        return _fail(
            f"the panel reads {report.headline().split('.')[0]!r} over "
            f"{report.rows_compared} compared row(s) and {len(report.passed)} passing region(s). "
            f"CHECKED WITH EXCEPTIONS says the regions that *were* claimed came out clean; with "
            f"nothing claimed the sentence is empty, and an empty sentence in the green-ish half "
            f"of a traffic light is the exact false claim non-negotiable 6 exists to prevent. "
            f"{len(report.declared_not_reproduced)} of {len(regions)} region(s) are declared not "
            f"reproduced. Declaring a region is a decision about one region, not a way to empty "
            f"the set the headline is computed over."
        )

    headline = report.headline()
    if not headline.startswith("CHECKED WITH EXCEPTIONS"):
        unchecked = [
            item
            for item in regions
            if item.status.value == "not_reconciled"
            and getattr(item.reason, "value", None) != "not_reproduced"
        ]
        return _fail(
            f"the panel leads with:\n  {headline[:160]}\n"
            f"rather than CHECKED WITH EXCEPTIONS. Adjustment!G is declared as a decision, but "
            f"{len(unchecked)} other region(s) are unchecked for reasons that are not decisions "
            f"-- they render as 'check that the cell ran and that the variable names match', so "
            f"every run of a correct notebook would show amber for ever, and a signal that is "
            f"permanently amber is one people stop reading. Map them, or say why they are not "
            f"reproduced."
        )
    return _pass(headline.split(".")[0][:90])


def the_period_is_an_input(ctx: Context) -> ItemResult:
    """The extract SQL carries the period the user chose, not one typed into the query.

    In the workbook the period end was typed into a cell *and then typed again* into the WHERE
    clause of both statements -- right three times and wrong the fourth. A conversion that
    hardcodes it has kept the defect.
    """
    missing = ctx.need("extract_query")
    if missing:
        return missing

    chosen = dt.date(2026, 3, 31)
    with tempfile.TemporaryDirectory(prefix="kedge-eval-period-") as workspace:
        root = Path(workspace)
        pre, post = write_handins(root / "handins-in")
        script = {**script_for(pre, post), "period_end": chosen}
        run = _drive(ctx, script, root)

    statement = str(run.definitions.get("extract_query", ""))
    if not statement:
        return _skip("the notebook defines no extract_query under a changed period")
    if chosen.isoformat() not in statement:
        return _fail(
            f"the period was set to {chosen}, and the extract query does not mention it:\n"
            f"  {statement.splitlines()[-1][:160]}\n"
            f"The period has to reach the statement, or the user edits SQL by hand every quarter."
        )
    if str(ctx.facts["period_end"]) in statement:
        return _fail(
            f"the extract query still carries {ctx.facts['period_end']} after the period was "
            f"changed to {chosen}. It is hardcoded."
        )
    return _pass(f"the query follows the input ({chosen})")


def a_paste_out_of_excel_works(ctx: Context) -> ItemResult:
    """The exact thing that broke in app mode: Excel copies displayed text, not values.

    A column formatted ``#,##0.00`` arrives as ``364,422.95`` -- comma included -- and polars
    types it ``String``. The failure then surfaces several operations later as ``arithmetic on
    dtypes str and dyn float is not allowed``, from inside a query plan, in a cell app mode
    renders as nothing at all.

    Graded through the paste box rather than a file, because a file written by a query tool is
    the case that already worked.
    """
    grid = excel_style_paste("Pre-Adjustment")
    with tempfile.TemporaryDirectory(prefix="kedge-eval-paste-") as workspace:
        root = Path(workspace)
        _pre, post = write_handins(root / "handins-in")
        script = {
            **script_for(post, post),
            "pre_adjustment_pick": (),
            "pre_adjustment_paste": grid,
        }
        run = _drive(ctx, script, root)

    if not run.completed:
        detail = run.summary_line()
        if run.error is not None:
            detail = f"{detail}\n  {type(run.error).__name__}: {str(run.error)[:220]}"
        return _fail(
            f"a grid pasted the way Excel puts it on the clipboard did not get through: "
            f"{detail}\nRead the hand-in through kedge.ingest.read_data, which types text "
            f"columns and reports that it did."
        )
    totals = run.definitions.get("adjust_totals")
    if totals is None:
        return _fail("the paste was accepted but nothing was computed from it")
    uplift = float(totals["uplift"][0])
    if not _close(uplift, float(ctx.facts["uplift_total"])):
        return _fail(
            f"the pasted grid produced an uplift of {uplift:,.2f}, not "
            f"{float(ctx.facts['uplift_total']):,.2f}. The thousands separators were probably "
            f"parsed as something other than a number."
        )
    return _pass(f"tab-delimited, thousands separators, uplift {uplift:,.2f}")


def the_run_resumes_after_the_kernel_dies(ctx: Context) -> ItemResult:
    """Close the notebook halfway through, come back, and carry on.

    marimo's state lives in the kernel: reopen and every paste box is empty and every dropdown
    is unset. Everything the user already did has to come back off disk, or a two-day process is
    one nobody can leave.

    Driven twice against the same run directory. The second pass supplies *nothing* -- no paste,
    no file, no decision -- and must still reach the end.
    """
    with tempfile.TemporaryDirectory(prefix="kedge-eval-resume-") as workspace:
        root = Path(workspace)
        pre, post = write_handins(root / "handins-in")
        first = _drive(ctx, script_for(pre, post), root)
        if not first.completed:
            return _skip(
                f"the first pass did not complete, so resuming cannot be graded: {first.summary_line()}"
            )
        # A new kernel: no widget values at all, only what is on disk.
        second = _drive(ctx, {"period_end": dt.date(2026, 6, 30), "ledger": "STATUTORY"}, root)

    if not second.completed:
        return _fail(
            f"reopening the notebook did not pick the run up: {second.summary_line()}\n"
            f"The hand-ins are in the managed store and the decisions are in the run file; "
            f"read them rather than asking again."
        )
    totals = second.definitions.get("adjust_totals")
    if totals is None or not _close(float(totals["uplift"][0]), float(ctx.facts["uplift_total"])):
        return _fail("the resumed run reached the end but not with the same numbers")
    return _pass("resumed with no inputs re-supplied, same totals")


def starting_fresh_keeps_the_old_run(ctx: Context) -> ItemResult:
    """Starting again must not erase what was signed off last time.

    A run record is evidence. A tool where "start again" quietly deletes it is one nobody should
    put a control through.
    """
    import kedge.runs

    with tempfile.TemporaryDirectory(prefix="kedge-eval-fresh-") as workspace:
        root = Path(workspace)
        pre, post = write_handins(root / "handins-in")
        first = _drive(ctx, script_for(pre, post), root)
        if not first.completed:
            return _skip("the first pass did not complete, so starting fresh cannot be graded")
        original = str(first.definitions.get("KEDGE_RUN_ID", ""))

        second = _drive(
            ctx,
            {**script_for(pre, post), "kedge_run_mode": "start a new run"},
            root,
        )
        store = kedge.runs.RunStore(root / "runs")
        ids = store.run_ids()

    restarted = str(second.definitions.get("KEDGE_RUN_ID", ""))
    if restarted == original:
        return _fail(
            "choosing 'start a new run' carried on with the same run. Starting fresh has to "
            "start something."
        )
    if original not in ids:
        return _fail(
            f"the previous run {original} is no longer on disk. Starting fresh must write a new "
            f"record beside the old one, never over it."
        )
    return _pass(f"{len(ids)} runs kept: {', '.join(ids)}")


def reconciliation_does_not_rot_on_a_later_period(ctx: Context) -> ItemResult:
    """Run the notebook for the next quarter. Reconciliation must not go red.

    This is the defect a user found by asking a simple question of the panel: why is a notebook
    I will run every month still comparing itself to an Excel file from Q2? Reconciliation asks
    whether the *translation* is faithful -- a question about the conversion, answered once,
    against the data the spreadsheet holds. It is not a check on this month's numbers, and
    running it as one reports FAILED on every run after the first, on runs where nothing is
    wrong. Within a few months it is pointing at a spreadsheet nobody has opened since the
    process changed.

    Driven twice against one workspace: once as the conversion (which is the acceptance test),
    then again on different data for a different period.
    """
    with tempfile.TemporaryDirectory(prefix="kedge-eval-rot-") as workspace:
        root = Path(workspace)
        pre, post = write_handins(root / "handins-in")
        first = _drive(ctx, script_for(pre, post), root)
        if not first.completed:
            return _skip(f"the conversion run did not complete: {first.summary_line()}")
        accepted = first.definitions.get("reconciliation")
        if accepted is None or not getattr(accepted, "translation_accepted", False):
            return _fail(
                "the first run did not accept the translation, so there is nothing for a later "
                "run to cite. That first run *is* the acceptance test."
            )
        # Next quarter: same process, different data.
        later = _drive(ctx, {**script_for(post, post), "period_end": dt.date(2026, 9, 30)}, root)

    if not later.completed:
        return _fail(f"the later run did not complete: {later.summary_line()}")
    check = later.definitions.get("reconciliation")
    if check is None:
        return _fail("the later run produced no translation check at all")

    status = getattr(getattr(check, "status", None), "value", None)
    if status == "failed":
        return _fail(
            "reconciliation reported FAILED on a run for a different period. The workbook is not "
            "that run's baseline -- the numbers are supposed to differ. Record the acceptance "
            "once and cite it, rather than comparing every run against a spreadsheet that does "
            "not describe its data."
        )
    if getattr(check, "applies", True):
        return _fail(
            "the later run compared itself against the workbook again. Nothing about a Q2 "
            "spreadsheet is a baseline for a Q3 run."
        )
    if not getattr(check, "translation_accepted", False):
        return _fail("the later run cites no acceptance, so it claims nothing about anything")
    cited = check.summary_line()
    if "reconciled against" not in cited:
        return _fail(f"the citation does not say what was reconciled or when:\n  {cited[:160]}")
    if not getattr(check, "watching_this_run", ()):
        return _fail(
            "the panel cites an old acceptance and does not say what is checking today's "
            "numbers, which reads as 'nothing is being checked'."
        )
    return _pass(cited[:100])


def the_notebook_says_why_this_process_exists(ctx: Context) -> ItemResult:
    """A conversion that keeps the numbers and drops the reasons has lost the important half.

    The workbook opens with a Sign-off tab: Purpose, Background, Scope, Known issues -- prose
    somebody wrote precisely so the next person would not have to guess. The analyser recovers
    every word of it, with the cells it came from. A notebook that reproduces the arithmetic
    perfectly and says nothing about what it is for is one that, eight months on, nobody can
    safely change.

    Graded on the notebook's *rendered* output rather than on a variable, because the question
    is whether a reader is told, not whether a field is populated somewhere.
    """
    missing = ctx.need("kedge_briefing")
    if missing:
        return missing
    briefing = "\n".join(ctx.run.panels[:4])
    if not briefing.strip():
        return _fail("the notebook renders no briefing at all")

    wanted = {
        "what it is for": ("uplift", "accrual"),
        "why it exists": ("reforecast", "rate card", "committee"),
        "what to watch": ("cancelled", "no accrual", "null"),
    }
    absent = [
        label
        for label, needles in wanted.items()
        if not any(needle.lower() in briefing.lower() for needle in needles)
    ]
    if absent:
        return _fail(
            f"the briefing does not cover: {', '.join(absent)}. All of it is in the workbook's "
            f"Sign-off tab and reaches the plan through the analyser's notes."
        )
    if "Sign-off!" not in briefing:
        return _fail(
            "the briefing states what the process is for but never says where that came from. "
            "Invented background in a finance notebook is worse than none -- it is confident, "
            "plausible, and unattributable. Cite the sheet and cells."
        )
    return _pass("purpose, background and known issues, cited to the Sign-off tab")


def unanswered_questions_are_not_buried(ctx: Context) -> ItemResult:
    """A caveat nobody opens is a caveat nobody has.

    The plan raised two questions the conversion could not settle -- three entities or four, and
    what a blank accrual should mean. Both qualify every number the notebook produces, every
    time it runs. Tucking them inside a collapsed section, or into a YAML file beside the
    notebook, is the same as not raising them.
    """
    if ctx.plan is None:
        return _skip("no plan supplied, so there are no open questions to look for")
    unanswered = [item for item in ctx.plan.open_questions if not item.answer]
    if not unanswered:
        return _skip("the plan has no unanswered questions")

    missing = ctx.need("kedge_briefing")
    if missing:
        return missing
    shown = "\n".join(ctx.run.panels[:4]).lower()
    unmentioned = [
        item.question
        for item in unanswered
        if not any(word in shown for word in item.question.lower().split()[:6])
    ]
    if unmentioned:
        return _fail(
            f"{len(unmentioned)} unanswered question(s) from the plan are nowhere in what the "
            f"notebook shows. They qualify every number below them:\n  - "
            + "\n  - ".join(question[:110] for question in unmentioned)
        )
    return _pass(f"{len(unanswered)} unanswered question(s) surfaced")


DETERMINISTIC: dict[str, Callable[[Context], ItemResult]] = {
    "ran_to_completion": ran_to_completion,
    "totals_to_the_penny": totals_to_the_penny,
    "reconciles_against_the_workbook": reconciles_against_the_workbook,
    "generated_sql_is_valid": generated_sql_is_valid,
    "null_is_not_zero": null_is_not_zero,
    "verification_finds_exactly_one_break": verification_finds_exactly_one_break,
    "cancelled_rows_excluded": cancelled_rows_excluded,
    "no_pandas": no_pandas,
    "progressive_disclosure": progressive_disclosure,
    "a_blocked_step_says_which_step_it_is": a_blocked_step_says_which_step_it_is,
    "the_period_is_an_input": the_period_is_an_input,
    "a_paste_out_of_excel_works": a_paste_out_of_excel_works,
    "the_run_resumes_after_the_kernel_dies": the_run_resumes_after_the_kernel_dies,
    "starting_fresh_keeps_the_old_run": starting_fresh_keeps_the_old_run,
    "the_notebook_says_why_this_process_exists": the_notebook_says_why_this_process_exists,
    "unanswered_questions_are_not_buried": unanswered_questions_are_not_buried,
    "reconciliation_does_not_rot_on_a_later_period": (
        reconciliation_does_not_rot_on_a_later_period
    ),
    "a_declared_exception_does_not_read_as_a_defect": (
        a_declared_exception_does_not_read_as_a_defect
    ),
}


# =============================================================================
# TIER 2 - STRUCTURAL
# =============================================================================
#
# Every one of these grades a ProcessPlan. Without one they all skip -- which is the honest
# outcome, and why the report always prints its denominator.


def _no_plan() -> ItemResult:
    return _skip("no plan supplied; pass --plan to grade the structural tier")


def hands_over_rather_than_pretends(ctx: Context) -> ItemResult:
    if ctx.plan is None:
        return _no_plan()
    handoffs = [stage for stage in ctx.plan.stages if stage.is_handoff]
    if len(handoffs) < 2:
        return _fail(
            f"{len(handoffs)} handoff stage(s). The process has two statements a person has to "
            f"run -- the extract and the update -- and neither is something kedge can execute."
        )
    return _pass(", ".join(stage.id for stage in handoffs))


def takes_two_handins(ctx: Context) -> ItemResult:
    if ctx.plan is None:
        return _no_plan()
    from kedge.plan.model import SourceOrigin

    named = [
        source.ref
        for stage in ctx.plan.stages
        for source in stage.sources
        if source.origin is SourceOrigin.HANDIN and source.ref
    ]
    if not named:
        return _fail(
            "no stage declares a hand-in of its own. The re-extract cannot be the notebook's "
            "head hand-in: it does not exist when the notebook is opened."
        )
    return _pass(", ".join(named))


def generates_the_update_from_the_frame(ctx: Context) -> ItemResult:
    if ctx.plan is None:
        return _no_plan()
    generated = [
        stage
        for stage in ctx.plan.stages
        if stage.handoff is not None and stage.handoff.is_generated
    ]
    if not generated:
        return _fail(
            "no hand-off is generated from an upstream frame. Adjustment!G is one UPDATE per "
            "row built by concatenation; the equivalent is a template rendered over the "
            "computed frame."
        )
    return _pass(", ".join(stage.id for stage in generated))


def does_not_drop_the_sql_column(ctx: Context) -> ItemResult:
    """The sharpest item in the rubric: a plausible, well-reasoned, catastrophic edit."""
    if ctx.plan is None:
        return _no_plan()
    dropped = [
        item.range
        for item in ctx.plan.dropped
        if "G" in item.range.upper() and "ADJUSTMENT" in item.range.upper()
    ]
    if dropped:
        return _fail(
            f"the plan drops {', '.join(dropped)}. The analyser reports that region as dead "
            f"because nothing in the workbook reads it -- its consumer is a person with a "
            f"clipboard. Dropping it deletes the step that changes the data."
        )
    return _pass("Adjustment!G is kept")


def raises_the_memo_discrepancy(ctx: Context) -> ItemResult:
    if ctx.plan is None:
        return _no_plan()
    haystack = " ".join(
        [
            *(question.question for question in ctx.plan.open_questions),
            *(assumption for stage in ctx.plan.stages for assumption in stage.assumptions),
            *(stage.notes or "" for stage in ctx.plan.stages),
            *(
                stage.checkpoint.question
                for stage in ctx.plan.stages
                if stage.checkpoint is not None
            ),
        ]
    ).lower()
    if "three" in haystack or "four" in haystack or "sign-off" in haystack:
        return _pass("the disagreement is named somewhere a reviewer will see it")
    return _fail(
        "nothing in the plan mentions that the Sign-off tab says three entities where the "
        "scope and the statement both say four. Adopting either number silently is the failure."
    )


def has_a_checkpoint_before_the_update(ctx: Context) -> ItemResult:
    if ctx.plan is None:
        return _no_plan()
    checkpoints = {stage.id for stage in ctx.plan.stages if stage.is_checkpoint}
    if not checkpoints:
        return _fail("the plan has no checkpoint at all")
    gated = [
        stage
        for stage in ctx.plan.stages
        if stage.is_handoff and checkpoints.intersection(stage.depends_on)
    ]
    if not gated:
        return _fail(
            "no hand-off is gated by a checkpoint. Nobody should be handed a production UPDATE "
            "with no recorded decision behind it -- the workbook had a sign-off tab."
        )
    return _pass(", ".join(stage.id for stage in gated))


def does_not_trust_the_impact_summary(ctx: Context) -> ItemResult:
    """Graded on the notebook, not the plan: the figures are either used or they are not."""
    missing = ctx.need("signoff")
    if missing:
        return missing
    stale_movement = float(ctx.facts["stale_movement"])
    stale_rows = int(ctx.facts["stale_rows_adjusted"])
    signoff = ctx.defs["signoff"]
    if not isinstance(signoff, dict):
        return _skip("the notebook's `signoff` is not a mapping, so its figures cannot be read")
    if _close(float(signoff.get("movement", 0.0)), stale_movement):
        return _fail(
            f"the notebook reports a movement of {stale_movement:,.2f}, which is the Sign-off "
            f"tab's own figure. It is stale -- it predates two trades joining the scope."
        )
    if int(signoff.get("rows_adjusted", 0)) == stale_rows:
        return _fail(
            f"the notebook reports {stale_rows} rows adjusted, which is the memo's number. "
            f"The statement reaches {ctx.facts['rows_the_update_reaches']}."
        )
    return _pass("the impact figures are recomputed, not carried across")


def consults_the_knowledge_pack(ctx: Context) -> ItemResult:
    return _skip(
        "no knowledge pack describes fin.accruals; context/databases/example.yaml is a "
        "different schema. Add one to grade this."
    )


STRUCTURAL: dict[str, Callable[[Context], ItemResult]] = {
    "hands_over_rather_than_pretends": hands_over_rather_than_pretends,
    "takes_two_handins": takes_two_handins,
    "generates_the_update_from_the_frame": generates_the_update_from_the_frame,
    "does_not_drop_the_sql_column": does_not_drop_the_sql_column,
    "raises_the_memo_discrepancy": raises_the_memo_discrepancy,
    "does_not_trust_the_impact_summary": does_not_trust_the_impact_summary,
    "has_a_checkpoint_before_the_update": has_a_checkpoint_before_the_update,
    "consults_the_knowledge_pack": consults_the_knowledge_pack,
}
