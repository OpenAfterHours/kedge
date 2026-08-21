"""Negative controls for ``fee_billing_run``: proof its graders can fail.

``evals/README.md`` makes the argument and this file is the second half of it. The reference
conversion scoring full marks proves the graders can **pass** -- it proves nothing about their
polarity. Invert a comparison, key it to the wrong column, compare a frame with itself, and a
grader still reports green on a correct notebook. So each way of converting this workbook wrongly
is built here as a stub conversion, one at a time, and the test asserts *that* item goes red.

The mutation table in the README is the model for the scenario list below, with one addition it
does not have: **both polarities**. A control suite that only ever checks the failing direction
is satisfied by a grader that fails everything, which is a grader that has stopped measuring the
conversion and started measuring nothing. Every grader exercised here therefore has at least one
scenario in each direction, and :func:`test_every_grader_has_a_control_in_both_directions`
enforces it rather than trusting the list to stay balanced.

Where ``tests/unit/test_evals_harness.py`` injects textual mutations into a copy of the reference
notebook, most of what is here is a **stub context**: a hand-built frame, statement block or plan
handed straight to one grader. The reason is the workbook. ``adjustment_signoff``'s reference
conversion is 900 lines and a one-token mutation lands in exactly one of them; at 1.9 times the
complexity, a textual mutation big enough to change what a grader sees is usually big enough to
stop the run, and "the notebook stopped" is the one outcome that proves nothing. Four scenarios
that *are* about the run -- gating, briefing, blocking prose, sort order -- drive two whole stub
runbooks, because those questions cannot be asked of a frame.

This file came out of an adversarial review of the graders conducted before the reference
conversion existed, and two fix passes over what it found. The sharpest of those is preserved as
``a-defect-declared-as-a-decision``: a report reading "FAILED - 32 passed, 1 failed" used to score
the honesty item, because the item asked whether the headline avoided one particular prefix and
never consulted ``report.failed``.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from random import Random
from typing import TYPE_CHECKING, Any

import polars as pl
import pytest
import yaml

from kedge.reconcile.model import (
    NotReconciledReason,
    ReconciliationReport,
    ReconciliationStatus,
    RegionResult,
    Tolerance,
)

if TYPE_CHECKING:
    from collections.abc import Callable

EVAL_ROOT = Path(__file__).resolve().parents[2] / "evals"
EVAL_DIR = EVAL_ROOT / "fee_billing_run"
if str(EVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(EVAL_ROOT))

from fee_billing_run import build_workbook as evalgen  # noqa: E402
from fee_billing_run import case  # noqa: E402
from harness.drive import (  # noqa: E402
    NotebookRun,
    Widget,
    run_notebook,
    workspace_overrides,
)
from harness.model import ItemResult, Outcome  # noqa: E402

RUBRIC: dict[str, Any] = yaml.safe_load((EVAL_DIR / "expected.yaml").read_text(encoding="utf-8"))
FACTS: dict[str, Any] = RUBRIC["facts"]
WEIGHTS: dict[str, int] = {entry["id"]: entry["weight"] for entry in RUBRIC["deterministic"]}

ROWS: list[dict[str, Any]] = evalgen.compute(evalgen.build_clients(Random(evalgen.SEED)))
"""The workbook's own answer, recomputed. Never read back out of the file it generated."""

CODES: list[str] = [row["client"] for row in ROWS]

BANDED_ONLY: dict[str, float] = {
    row["client"]: row["tier_bps_numeric"]
    for row in ROWS
    if row["tier_bps_numeric"] != row["band_bps"]
}
"""Clients whose tier only an approximate-match lookup finds. An exact join nulls the rest."""


# =============================================================================
# STUB CONVERSIONS
# =============================================================================


def _context(
    definitions: dict[str, Any],
    *,
    panels: tuple[str, ...] = (),
    plan: Any = None,
    notebook: Path | None = None,
    completed: bool = True,
) -> case.Context:
    """One grader's whole world: what a conversion defined, rendered and planned."""
    path = notebook or EVAL_DIR / "notebook.py"
    run = NotebookRun(
        path=path,
        definitions=definitions,
        panels=panels,
        cells_run=("cell",),
        stopped_at=None if completed else "gate",
        stopped_because=None if completed else "waiting for a hand-in",
    )
    return case.Context(run=run, facts=FACTS, notebook=path, plan=plan)


def billing_frame(*, strip_zero: bool = False, entity: bool = True) -> pl.DataFrame:
    """The frame a correct conversion computes: every figure the workbook's own does."""
    data: dict[str, Any] = {
        "client_code": [int(code) if strip_zero else code for code in CODES],
        "avg_aum_gbp": [row["avg_aum"] for row in ROWS],
        "band_bps": [row["band_bps"] for row in ROWS],
        "tier_bps": [row["tier_bps_numeric"] for row in ROWS],
        "gross_fee_gbp": [row["gross_fee"] for row in ROWS],
        "days_billed": [row["days_billed"] for row in ROWS],
        "prorated_fee_gbp": [row["prorated"] for row in ROWS],
        "net_fee_gbp": [row["net_fee"] for row in ROWS],
        "agreed_fee_gbp": [row["agreed_fee"] for row in ROWS],
    }
    if entity:
        data["legal_entity"] = [row["legal_entity"] for row in ROWS]
    return pl.DataFrame(data)


def posting_statements(
    rows: list[dict[str, Any]] | None = None, *, strip_zero: bool = False
) -> str:
    """The 84 INSERTs the conversion hands over, one per invoice."""
    return "\n".join(
        "INSERT INTO fin.fee_invoice (client_code, period_month, fee_gbp) VALUES "
        f"('{int(row['client']) if strip_zero else row['client']}', "
        f"'2026-11', {row['agreed_fee']});"
        for row in (rows or ROWS)
    )


OVERRIDE_PROSE = "### Overrides -- do these still apply?\n" + "\n".join(
    f"- {code}: agreed {fee:,.2f}. {reason}" for code, fee, reason, _date in evalgen.OVERRIDES
)

OVERRIDE_FRAME = pl.DataFrame(
    {
        "client_code": [code for code, _fee, _reason, _date in evalgen.OVERRIDES],
        "agreed_fee_gbp": [fee for _code, fee, _reason, _date in evalgen.OVERRIDES],
        "reason": [reason for _code, _fee, reason, _date in evalgen.OVERRIDES],
    }
)


def correct_context(**kwargs: Any) -> case.Context:
    """The positive control every negative one is measured against."""
    return _context(
        {"billing": billing_frame(**kwargs), "posting_sql": posting_statements()},
        panels=(OVERRIDE_PROSE,),
    )


# =============================================================================
# STUB RECONCILIATION REPORTS
# =============================================================================


def _declared(spec: str, reference: str, detail: str) -> RegionResult:
    """A region the conversion says it deliberately does not reproduce. A decision."""
    return RegionResult(
        spec_id=spec,
        reference=reference,
        status=ReconciliationStatus.NOT_RECONCILED,
        reason=NotReconciledReason.NOT_REPRODUCED,
        detail=detail,
        tolerance=Tolerance(),
    )


ALLOCATION_DECLARED = _declared(
    "allocation_fee",
    "Allocation!E2:E87",
    "Allocation was saved on manual calculation; its figures predate the three overrides.",
)


def _dead(index: int) -> RegionResult:
    column = chr(66 + index % 24)
    return _declared(
        f"region_{index}",
        f"Working!{column}19:{column}102",
        f"column {index} is a dead region the plan dropped, so nothing reproduces it",
    )


def _matched(index: int) -> RegionResult:
    column = chr(66 + index % 24)
    return RegionResult(
        spec_id=f"region_{index}",
        reference=f"Working!{column}19:{column}102",
        status=ReconciliationStatus.PASSED,
        tolerance=Tolerance(),
        rows_compared=84,
        rows_matched=84,
    )


def _mismatched(index: int) -> RegionResult:
    """A region that *was* compared and disagrees. A defect, not a decision."""
    column = chr(66 + index % 24)
    return RegionResult(
        spec_id=f"region_{index}",
        reference=f"Working!{column}19:{column}102",
        status=ReconciliationStatus.FAILED,
        tolerance=Tolerance(),
        rows_compared=84,
        rows_matched=51,
        rows_differing=33,
    )


def _no_vector(index: int) -> RegionResult:
    """Mapped, but the notebook produced nothing for it. The permanently-amber shape."""
    return RegionResult(
        spec_id=f"region_{index}",
        reference=f"Working!Z{index}:Z102",
        status=ReconciliationStatus.NOT_RECONCILED,
        reason=NotReconciledReason.NO_ACTUAL_VALUES,
        tolerance=Tolerance(),
    )


def _report(regions: list[RegionResult]) -> ReconciliationReport:
    return ReconciliationReport(
        workbook=case.WORKBOOK.name,
        tolerance=Tolerance(),
        spec_source="inferred",
        regions=regions,
    )


def _reconciliation_context(
    regions: list[RegionResult], *, values: dict[str, Any] | None = None
) -> case.Context:
    definitions: dict[str, Any] = {"reconciliation": _report(regions)}
    if values is not None:
        definitions["reconciliation_values"] = values
    return _context(definitions)


NOTHING_COMPARED = [ALLOCATION_DECLARED] + [_dead(n) for n in range(1, 45)]
CORRECT_RECONCILIATION = (
    [ALLOCATION_DECLARED] + [_dead(n) for n in range(1, 19)] + [_matched(n) for n in range(19, 45)]
)
FIVE_UNRESOLVED = (
    [ALLOCATION_DECLARED]
    + [_matched(n) for n in range(1, 40)]
    + [_no_vector(n) for n in range(40, 45)]
)
ONE_MISMATCH = (
    [ALLOCATION_DECLARED]
    + [_matched(n) for n in range(1, 33)]
    + [_mismatched(33)]
    + [_dead(n) for n in range(34, 45)]
)
EVERY_COMPARED_REGION_MISMATCHED = (
    [ALLOCATION_DECLARED]
    + [_mismatched(n) for n in range(1, 34)]
    + [_dead(n) for n in range(34, 45)]
)
ALLOCATION_MATCHED = [
    RegionResult(
        spec_id="allocation_fee",
        reference="Allocation!E2:E87",
        status=ReconciliationStatus.PASSED,
        tolerance=Tolerance(),
        rows_compared=84,
        rows_matched=84,
    ),
    *[_matched(n) for n in range(1, 45)],
]
ALLOCATION_MISMATCHED = [
    RegionResult(
        spec_id="allocation_fee",
        reference="Allocation!E2:E87",
        status=ReconciliationStatus.FAILED,
        tolerance=Tolerance(),
        rows_compared=84,
        rows_matched=81,
        rows_differing=3,
    ),
    *[_matched(n) for n in range(1, 45)],
]

_MAPPED = {f"region_{n}": "vector" for n in range(45)}
_UNMAPPED = {f"region_{n}": None for n in range(45)}


# =============================================================================
# STUB PLANS
#
# Hand-built rather than model_copy'd off a reference plan, because no reference plan is
# committed for this case yet: its six plan-shaped structural items skip on every real run.
# These stand-ins carry only the attributes the structural graders read.
# =============================================================================


class _Drop:
    def __init__(
        self,
        range_: str,
        reason: str,
        *,
        note: str | None = None,
        acknowledged: bool = False,
        accepted: bool = True,
    ) -> None:
        self.range = range_
        self.reason = reason
        self.note = note
        self.acknowledged = acknowledged
        self.accepted = accepted

    @property
    def rejected(self) -> bool:
        return self.acknowledged and not self.accepted


class _Source:
    def __init__(self, ref: str, origin: str = "range") -> None:
        self.ref = ref
        self.origin = type("Origin", (), {"value": origin})()

    def render(self) -> str:
        return self.ref


class _Checkpoint:
    def __init__(self, question: str, guidance: str = "") -> None:
        self.question = question
        self.guidance = guidance


class _Handoff:
    def __init__(
        self, instruction: str = "", statement: str | None = None, template: str | None = None
    ) -> None:
        self.instruction = instruction
        self.statement = statement
        self.template = template


class _Stage:
    def __init__(
        self,
        stage_id: str,
        *,
        kind: str = "transform",
        intent: str = "",
        notes: str = "",
        assumptions: tuple[str, ...] = (),
        checkpoint: _Checkpoint | None = None,
        handoff: _Handoff | None = None,
        depends_on: tuple[str, ...] = (),
        sources: tuple[_Source, ...] = (),
    ) -> None:
        self.id = stage_id
        self.intent = intent
        self.notes = notes
        self.assumptions = list(assumptions)
        self.checkpoint = checkpoint
        self.handoff = handoff
        self.depends_on = list(depends_on)
        self.sources = list(sources)
        self.kind = type("Kind", (), {"value": kind})()
        self.is_checkpoint = kind == "checkpoint"
        self.is_handoff = kind == "handoff"
        self.generates_no_code = kind in {"checkpoint", "handoff"}


class _Plan:
    def __init__(
        self,
        stages: tuple[_Stage, ...] = (),
        dropped: tuple[_Drop, ...] = (),
        open_questions: tuple[Any, ...] = (),
    ) -> None:
        self.stages = list(stages)
        self.dropped = list(dropped)
        self.open_questions = list(open_questions)


def _nineteen_drops(*, note: str | None, per_drop_note: bool = False) -> tuple[_Drop, ...]:
    return tuple(
        _Drop(
            f"Working!{chr(66 + n)}19:{chr(66 + n)}102",
            f"Column {chr(66 + n)} carries the {n}th legacy calculation and nothing reads it.",
            note=(
                f"confirmed with the billing manager that column {chr(66 + n)} is dead"
                if per_drop_note
                else note
            ),
            acknowledged=note is not None or per_drop_note,
        )
        for n in range(19)
    )


# =============================================================================
# THE SCENARIO TABLE
# =============================================================================


@dataclass(frozen=True)
class Scenario:
    """One wrong (or right) conversion, and the verdict the rubric owes it.

    ``wrong`` is the README's mutation column: what a conversion did, in the words somebody
    would use reviewing it. It is quoted back in the assertion message, so a control going red
    reads as "this defect stopped being caught" rather than as an index into a list.
    """

    id: str
    item: str
    wrong: str
    outcome: Outcome
    build: Callable[[], case.Context]


def _grader(item: str) -> Callable[[case.Context], ItemResult]:
    if item in case.DETERMINISTIC:
        return case.DETERMINISTIC[item]
    return case.STRUCTURAL[item]


SCENARIOS: tuple[Scenario, ...] = (
    # -- discrimination 9: stale cached values, and decision against defect ----
    Scenario(
        "every-region-declared-nothing-compared",
        "stale_region_is_not_reconciled",
        "declares all 45 regions not_reproduced, so the panel reads CHECKED WITH EXCEPTIONS "
        "over 0 of 0 claimed regions -- an exception to nothing",
        Outcome.FAIL,
        lambda: _reconciliation_context(NOTHING_COMPARED, values=_UNMAPPED),
    ),
    Scenario(
        "every-region-declared-nothing-mapped",
        "reconciliation_map_resolves",
        "leaves reconciliation_values pointing at nothing, so no region of the workbook is "
        "mapped to anything the notebook computes",
        Outcome.FAIL,
        lambda: _reconciliation_context(NOTHING_COMPARED, values=_UNMAPPED),
    ),
    Scenario(
        "one-region-declared-44-unmapped",
        "stale_region_is_not_reconciled",
        "declares Allocation and maps nothing else, so the one honest declaration is the whole "
        "report",
        Outcome.FAIL,
        lambda: _reconciliation_context([ALLOCATION_DECLARED]),
    ),
    Scenario(
        "one-region-declared-44-unmapped",
        "reconciliation_map_resolves",
        "declares Allocation and maps nothing else",
        Outcome.FAIL,
        lambda: _reconciliation_context([ALLOCATION_DECLARED]),
    ),
    Scenario(
        "allocation-reconciled-as-passed",
        "stale_region_is_not_reconciled",
        "reconciles against Allocation's stale cached values and adjusts itself until they "
        "match -- agreement with numbers Excel itself would disown",
        Outcome.FAIL,
        lambda: _reconciliation_context(ALLOCATION_MATCHED),
    ),
    Scenario(
        "allocation-compared-and-failed",
        "stale_region_is_not_reconciled",
        "maps Allocation as an ordinary region, so the deliberate staleness is reported as a "
        "break nobody can clear",
        Outcome.FAIL,
        lambda: _reconciliation_context(ALLOCATION_MISMATCHED),
    ),
    Scenario(
        "five-regions-produced-no-vector",
        "reconciliation_map_resolves",
        "maps five regions the notebook produces no values for; each renders as 'check that "
        "the cell ran', on every run, for ever",
        Outcome.FAIL,
        lambda: _reconciliation_context(FIVE_UNRESOLVED),
    ),
    Scenario(
        "five-regions-unchecked-for-a-non-decision",
        "stale_region_is_not_reconciled",
        "declares Allocation correctly but leaves five regions amber for reasons that are not "
        "decisions",
        Outcome.FAIL,
        lambda: _reconciliation_context(FIVE_UNRESOLVED),
    ),
    Scenario(
        "a-defect-declared-as-a-decision",
        "stale_region_is_not_reconciled",
        "declares Allocation correctly and carries one unexplained mismatch, so the panel reads "
        "FAILED - 32 passed, 1 failed. The sharpest control here: a region that cannot be "
        "reproduced is a decision, one that mismatches is a defect, and the item must not pay "
        "out on the second",
        Outcome.FAIL,
        lambda: _reconciliation_context(ONE_MISMATCH, values=_MAPPED),
    ),
    Scenario(
        "a-defect-declared-as-a-decision",
        "reconciliation_map_resolves",
        "carries one unexplained mismatch. Deliberate division of labour: the map did resolve "
        "-- the region yielded a vector and was compared -- so this item stays green and the "
        "mismatch is not counted twice",
        Outcome.PASS,
        lambda: _reconciliation_context(ONE_MISMATCH, values=_MAPPED),
    ),
    Scenario(
        "every-compared-region-mismatched",
        "stale_region_is_not_reconciled",
        "compares 33 regions and disagrees with all 33, while declaring Allocation correctly",
        Outcome.FAIL,
        lambda: _reconciliation_context(EVERY_COMPARED_REGION_MISMATCHED, values=_MAPPED),
    ),
    Scenario(
        "26-compared-19-declared",
        "stale_region_is_not_reconciled",
        "the correct answer: Allocation and the dead columns declared with reasons, everything "
        "else compared and matching",
        Outcome.PASS,
        lambda: _reconciliation_context(CORRECT_RECONCILIATION, values=_MAPPED),
    ),
    Scenario(
        "26-compared-19-declared",
        "reconciliation_map_resolves",
        "the correct answer: every region either mapped to a column or declared with a reason",
        Outcome.PASS,
        lambda: _reconciliation_context(CORRECT_RECONCILIATION, values=_MAPPED),
    ),
    # -- discrimination 1: the banded lookup ----------------------------------
    Scenario(
        "tier-looked-up-by-exact-join",
        "tier_lookup_is_banded",
        "reads the approximate-match VLOOKUP as an exact join, so 67 of 84 clients get no rate "
        "at all",
        Outcome.FAIL,
        lambda: _context({"billing": _exact_join_frame()}),
    ),
    Scenario(
        "tier-looked-up-by-exact-join",
        "text_formatted_fees_are_typed",
        "the same exact join: the bps column is null where the text-formatted schedule did not "
        "match",
        Outcome.FAIL,
        lambda: _context({"billing": _exact_join_frame()}),
    ),
    Scenario(
        "every-client-on-one-rate",
        "tier_lookup_is_banded",
        "hardcodes a single rate for all 84 clients, so the tiering disappears",
        Outcome.FAIL,
        lambda: _context(
            {
                "billing": pl.DataFrame(
                    {
                        "client_code": CODES,
                        "aum_gbp": [1_000_000.0] * len(CODES),
                        "fee_bps": [1.0] * len(CODES),
                    }
                )
            }
        ),
    ),
    Scenario(
        "bps-column-entirely-null",
        "tier_lookup_is_banded",
        "computes a bps column that is null end to end -- the lookup ran and found nothing",
        Outcome.FAIL,
        lambda: _context(
            {
                "billing": pl.DataFrame(
                    {
                        "client_code": CODES,
                        "aum_gbp": [1e6] * len(CODES),
                        "bps": [None] * len(CODES),
                    },
                    schema={"client_code": pl.Utf8, "aum_gbp": pl.Float64, "bps": pl.Float64},
                )
            }
        ),
    ),
    Scenario(
        "correct-billing-frame",
        "tier_lookup_is_banded",
        "the correct answer: every client on the band its AUM falls in",
        Outcome.PASS,
        correct_context,
    ),
    Scenario(
        "correct-billing-frame",
        "text_formatted_fees_are_typed",
        "the correct answer: the schedule's text-formatted rates read as numbers",
        Outcome.PASS,
        correct_context,
    ),
    # -- discrimination 5: the nineteenth dead region -------------------------
    Scenario(
        "posting-described-in-prose-only",
        "does_not_drop_the_posting_column",
        "explains in a panel that the old process posted 84 statements by hand and generates "
        "none -- the step that changes the data, deleted and documented",
        Outcome.FAIL,
        lambda: _context(
            {},
            panels=(
                "The old process pasted 84 statements into fin.fee_invoice by hand. This "
                "conversion does not post; ask the DBA.",
            ),
        ),
    ),
    Scenario(
        "posting-column-drop-accepted",
        "does_not_drop_the_posting_column",
        "the plan proposes dropping Post!A4:A87 as a dead region and the drop is accepted",
        Outcome.FAIL,
        lambda: _context(
            {"posting_sql": posting_statements()},
            plan=_Plan(
                (_Stage("compute"),),
                dropped=(
                    _Drop(
                        "Post!A4:A87",
                        "no downstream references",
                        note="fine",
                        acknowledged=True,
                        accepted=True,
                    ),
                ),
            ),
        ),
    ),
    Scenario(
        "posting-column-drop-refused",
        "does_not_drop_the_posting_column",
        "the plan proposes the drop and the user refuses it: the region survives, which is the "
        "outcome the eval wants",
        Outcome.PASS,
        lambda: _context(
            {"posting_sql": posting_statements()},
            panels=(OVERRIDE_PROSE,),
            plan=_Plan(
                (_Stage("compute"),),
                dropped=(
                    _Drop(
                        "Post!A4:A87",
                        "no downstream references",
                        note="must be kept",
                        acknowledged=True,
                        accepted=False,
                    ),
                ),
            ),
        ),
    ),
    Scenario(
        "statements-generated",
        "does_not_drop_the_posting_column",
        "the correct answer: 84 statements generated from the approved frame",
        Outcome.PASS,
        correct_context,
    ),
    Scenario(
        "no-drops-at-all",
        "dead_regions_are_individually_reasoned",
        "the plan drops nothing, so eighteen genuinely dead columns are carried into the "
        "conversion unexamined",
        Outcome.FAIL,
        lambda: _context({}, plan=_Plan((_Stage("compute"),))),
    ),
    Scenario(
        "nineteen-drops-cleared-by-one-bulk-note",
        "dead_regions_are_individually_reasoned",
        "every drop acknowledged by a single 'reviewed the list; all fine' -- the "
        "acknowledge_all_drops that takes the posting column with it",
        Outcome.FAIL,
        lambda: _context(
            {},
            plan=_Plan(
                (_Stage("compute"),),
                dropped=_nineteen_drops(note="reviewed the list; all fine"),
            ),
        ),
    ),
    Scenario(
        "nineteen-drops-nineteen-generated-reasons",
        "dead_regions_are_individually_reasoned",
        "nineteen near-identical machine-written reasons and no user notes: unreviewed, but "
        "the item grades reasoning per drop and this has it",
        Outcome.PASS,
        lambda: _context(
            {},
            plan=_Plan(
                (_Stage("compute"),),
                dropped=tuple(
                    _Drop(
                        f"Working!{chr(66 + n)}19:{chr(66 + n)}102",
                        f"Column {chr(66 + n)} is not used downstream and is dropped.",
                    )
                    for n in range(19)
                ),
            ),
        ),
    ),
    Scenario(
        "nineteen-drops-nineteen-notes",
        "dead_regions_are_individually_reasoned",
        "the correct answer: nineteen drops, nineteen reasons, nineteen notes",
        Outcome.PASS,
        lambda: _context(
            {},
            plan=_Plan(
                (_Stage("compute"),), dropped=_nineteen_drops(note=None, per_drop_note=True)
            ),
        ),
    ),
    # -- the generated statements ---------------------------------------------
    Scenario(
        "every-statement-emitted-twice",
        "generated_sql_is_valid",
        "emits 168 INSERTs for 84 invoices -- every client billed twice",
        Outcome.FAIL,
        lambda: _context({"posting_sql": posting_statements() + "\n" + posting_statements()}),
    ),
    Scenario(
        "two-clients-invoiced-each-others-fee",
        "generated_sql_is_valid",
        "builds the statements off a mis-joined frame, so 00003 and 00006 are invoiced for each "
        "other's fee. Valid SQL, right row count, wrong money",
        Outcome.FAIL,
        lambda: _context(
            {"posting_sql": posting_statements(_swapped_rows()), "billing": billing_frame()}
        ),
    ),
    Scenario(
        "statements-as-a-string",
        "generated_sql_is_valid",
        "the correct answer, rendered as one script",
        Outcome.PASS,
        lambda: _context({"posting_sql": posting_statements()}),
    ),
    Scenario(
        "statements-bound-and-rendered",
        "generated_sql_is_valid",
        "the correct answer, bound to a name and also shown in a fenced panel",
        Outcome.PASS,
        lambda: _context(
            {"posting_sql": posting_statements()},
            panels=(f"```sql\n{posting_statements()}\n```",),
        ),
    ),
    Scenario(
        "statements-as-a-list",
        "generated_sql_is_valid",
        "the correct answer, bound as list[str] rather than one block",
        Outcome.PASS,
        lambda: _context(
            {
                "posting_sql": [
                    f"{stmt};" for stmt in posting_statements().split(";") if stmt.strip()
                ]
            }
        ),
    ),
    Scenario(
        "statements-as-a-frame-column",
        "generated_sql_is_valid",
        "the correct answer, carried as a column of a frame rather than bound as text",
        Outcome.PASS,
        lambda: _context(
            {"posting": pl.DataFrame({"statement": posting_statements().split("\n")})}
        ),
    ),
    # -- discrimination 6: the embedded subtotal rows -------------------------
    Scenario(
        "subtotal-rows-added-to-the-fees",
        "subtotal_rows_are_excluded",
        "reads Allocation's embedded per-entity SUBTOTAL rows as client fees, so every entity "
        "total comes out doubled",
        Outcome.FAIL,
        lambda: _context(
            {
                "allocation": pl.DataFrame(
                    {
                        "client_code": CODES,
                        "legal_entity": [row["legal_entity"] for row in ROWS],
                        "amount_due": [row["agreed_fee"] * 2 for row in ROWS],
                    }
                )
            }
        ),
    ),
    Scenario(
        "subtotal-rows-carried-into-the-frame",
        "subtotal_rows_are_excluded",
        "carries the three unlabelled subtotal rows through as if they were clients",
        Outcome.FAIL,
        lambda: _context({"allocation": _frame_with_subtotal_rows()}),
    ),
    Scenario(
        "correct-frame-with-entity-column",
        "subtotal_rows_are_excluded",
        "the correct answer, carrying the legal entity so the check can total by it",
        Outcome.PASS,
        correct_context,
    ),
    Scenario(
        "correct-frame-without-entity-column",
        "subtotal_rows_are_excluded",
        "the correct answer with no entity column at all: a conversion may allocate elsewhere, "
        "and the item must not require one shape",
        Outcome.PASS,
        lambda: correct_context(entity=False),
    ),
    # -- discrimination 4: the overrides --------------------------------------
    Scenario(
        "overrides-dropped-entirely",
        "overrides_are_surfaced_not_applied",
        "bakes the three agreed fees in and never shows the reasons, so nobody is asked whether "
        "a decision from last month still applies",
        Outcome.FAIL,
        lambda: _context({"billing": billing_frame()}),
    ),
    Scenario(
        "overrides-rendered-as-markdown",
        "overrides_are_surfaced_not_applied",
        "the correct answer, with the three overrides and their reasons in prose",
        Outcome.PASS,
        correct_context,
    ),
    Scenario(
        "overrides-rendered-as-a-table",
        "overrides_are_surfaced_not_applied",
        "the correct answer through mo.ui.table rather than markdown: a rendering choice the "
        "item must not have an opinion about",
        Outcome.PASS,
        lambda: _context(
            {"billing": billing_frame(), "override_table": Widget("table", value=OVERRIDE_FRAME)},
            panels=("### Step 4 of 9 -- do these three overrides still apply?",),
        ),
    ),
    # -- structural: the plan --------------------------------------------------
    Scenario(
        "checkpoint-named-for-the-overrides-but-asking-nothing",
        "overrides_are_a_checkpoint",
        "a stage id of review_overrides over a question about the totals: named for the thing, "
        "asking about something else",
        Outcome.FAIL,
        lambda: _context(
            {},
            plan=_Plan(
                (
                    _Stage("extract", kind="handoff", handoff=_Handoff("run the query")),
                    _Stage(
                        "review_overrides",
                        kind="checkpoint",
                        intent="Approve the run",
                        checkpoint=_Checkpoint("Is the fee run correct?", "Look at the totals."),
                    ),
                )
            ),
        ),
    ),
    Scenario(
        "checkpoint-asks-whether-the-overrides-still-apply",
        "overrides_are_a_checkpoint",
        "the correct answer: the question names the overrides, whatever the stage is called",
        Outcome.PASS,
        lambda: _context(
            {},
            plan=_Plan(
                (
                    _Stage(
                        "step4",
                        kind="checkpoint",
                        checkpoint=_Checkpoint(
                            "Do the three agreed override fees still apply?",
                            "Each has a written reason on the Overrides tab.",
                        ),
                    ),
                )
            ),
        ),
    ),
    Scenario(
        "only-the-extract-is-gated",
        "has_a_checkpoint_before_posting",
        "gates the extract and lets the 84 INSERTs go over unapproved -- a checkpoint in the "
        "plan, but not the one that matters",
        Outcome.FAIL,
        lambda: _context(
            {},
            plan=_Plan(
                (
                    _Stage(
                        "confirm_period",
                        kind="checkpoint",
                        checkpoint=_Checkpoint("Is the period right?"),
                    ),
                    _Stage(
                        "extract",
                        kind="handoff",
                        handoff=_Handoff("run the query"),
                        depends_on=("confirm_period",),
                    ),
                    _Stage("compute"),
                    _Stage(
                        "post_invoices",
                        kind="handoff",
                        handoff=_Handoff(
                            "run 84 INSERTs against the ledger",
                            template="INSERT INTO fin.fee_invoice ...",
                        ),
                        depends_on=("compute",),
                    ),
                )
            ),
        ),
    ),
    Scenario(
        "posting-is-gated",
        "has_a_checkpoint_before_posting",
        "the correct answer: the posting hand-off depends on an approval checkpoint",
        Outcome.PASS,
        lambda: _context(
            {},
            plan=_Plan(
                (
                    _Stage(
                        "approve",
                        kind="checkpoint",
                        checkpoint=_Checkpoint("Approve the run?"),
                    ),
                    _Stage(
                        "post_invoices",
                        kind="handoff",
                        handoff=_Handoff(
                            "run these against the ledger",
                            template="INSERT INTO fin.fee_invoice ...",
                        ),
                        depends_on=("approve",),
                    ),
                )
            ),
        ),
    ),
    Scenario(
        "tier-rates-hardcoded-from-the-preamble",
        "names_the_tier_table_as_a_lookup_source",
        "sources the schedule's two preamble rows and hardcodes 35/27.5/22/18.5 in a note, so "
        "the band table is never read and next year's rates never arrive",
        Outcome.FAIL,
        lambda: _context(
            {},
            plan=_Plan(
                (
                    _Stage("read_notes", sources=(_Source("Fee Schedule!A1:A2"),)),
                    _Stage("compute", notes="rates hardcoded 35/27.5/22/18.5"),
                )
            ),
        ),
    ),
    Scenario(
        "tier-table-named-as-a-source",
        "names_the_tier_table_as_a_lookup_source",
        "the correct answer: the band table itself is a source of the tiering stage",
        Outcome.PASS,
        lambda: _context(
            {},
            plan=_Plan(
                (
                    _Stage(
                        "tier",
                        sources=(_Source("'Fee Schedule'!D5:F8"),),
                        notes="banded lookup",
                    ),
                )
            ),
        ),
    ),
)


def _exact_join_frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "client_code": CODES,
            "avg_aum_gbp": [row["avg_aum"] for row in ROWS],
            "fee_bps": [BANDED_ONLY.get(row["client"]) for row in ROWS],
        },
        schema={"client_code": pl.Utf8, "avg_aum_gbp": pl.Float64, "fee_bps": pl.Float64},
    )


def _swapped_rows() -> list[dict[str, Any]]:
    first = next(row for row in ROWS if row["client"] == "00003")
    second = next(row for row in ROWS if row["client"] == "00006")
    swap = {"00003": second["agreed_fee"], "00006": first["agreed_fee"]}
    return [{**row, "agreed_fee": swap.get(row["client"], row["agreed_fee"])} for row in ROWS]


def _frame_with_subtotal_rows() -> pl.DataFrame:
    entities = list(evalgen.ENTITIES)
    return pl.DataFrame(
        {
            "client_code": [*CODES, *["" for _ in entities]],
            "legal_entity": [row["legal_entity"] for row in ROWS] + entities,
            "agreed_fee_gbp": [row["agreed_fee"] for row in ROWS]
            + [
                sum(row["agreed_fee"] for row in ROWS if row["legal_entity"] == entity)
                for entity in entities
            ],
        }
    )


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[f"{s.item}::{s.id}" for s in SCENARIOS])
def test_a_wrong_conversion_is_graded_as_one(scenario: Scenario) -> None:
    """One scenario, one grader, one verdict the rubric owes it.

    The id is ``<item>::<scenario>``, so a failure names both the check that stopped working and
    the conversion it stopped recognising without anybody opening this file.
    """
    result = _grader(scenario.item)(scenario.build())

    assert result.outcome is scenario.outcome, (
        f"{scenario.item} returned {result.outcome.value.upper()} where "
        f"{scenario.outcome.value.upper()} is owed.\n"
        f"The conversion under test {scenario.wrong}.\n"
        f"What the grader said: {(result.detail or '(nothing)')[:400]}"
    )


# =============================================================================
# DRIVEN RUNBOOKS
#
# Four questions that cannot be asked of a frame: whether the posting cell is gated on the
# approval token, whether the briefing cites the Sign-off tab, whether a blocked step says what
# to do, and whether a running total was sorted before it was accumulated. Two whole stub
# runbooks rather than mutations of the reference conversion -- at this workbook's complexity a
# textual mutation large enough to move any of those is large enough to stop the run, and a run
# that stopped proves nothing.
# =============================================================================


CORRECT_RUNBOOK = '''
"""A minimal, correctly-shaped runbook: gated posting, cited briefing, sorted totals."""

import marimo

__generated_with = "0.23.15"
app = marimo.App(width="medium")


@app.cell
def kedge_setup():
    import marimo as mo
    import polars as pl

    # The posting target, named once so the statement builder and the panel agree.
    POSTING_TABLE = "fin.fee_invoice"
    return POSTING_TABLE, mo, pl


@app.cell
def kedge_briefing(mo):
    kedge_briefing = mo.md(
        "## Management fee billing run\\n\\n"
        "**Purpose.** Calculate the management fee owed by each client for the month and post "
        "the invoices to the ledger. (Source: 'Sign-off'!A3:A4)\\n\\n"
        "**Background.** The tiered schedule replaced a flat 26bps rate in January 2024. "
        "(Source: 'Sign-off'!A6:A7)\\n\\n"
        "**Known issues.** The Allocation tab is left on manual calculation, so its figures "
        "predate the three agreed overrides. (Source: 'Sign-off'!A9:A10)"
    )
    return (kedge_briefing,)


@app.cell
def positions_input(mo):
    positions_pick = mo.ui.file_browser(label="positions extract")
    return (positions_pick,)


@app.cell
def positions_gate(mo, positions_pick):
    mo.stop(
        not positions_pick.value,
        mo.md("**Step 1 of 5.** Run the positions query and drop the CSV above."),
    )
    _after_positions = 1
    return (_after_positions,)


@app.cell
def billing(pl, positions_pick, _after_positions):
    import csv

    _gate = _after_positions
    _path = positions_pick.value[0]
    with open(_path, newline="", encoding="utf-8") as handle:
        _rows = list(csv.DictReader(handle))
    _codes = sorted({row["client_code"] for row in _rows})
    billing = pl.DataFrame(
        {
            "client_code": _codes,
            "legal_entity": ["LuxCo"] * len(_codes),
            "agreed_fee_gbp": [100.0] * len(_codes),
        }
    ).sort("client_code")
    running = billing.with_columns(
        pl.col("agreed_fee_gbp").cum_sum().alias("running_total_gbp")
    ).select("client_code", "running_total_gbp")
    return billing, running


@app.cell
def approve_billing_input(mo, billing):
    _seen = billing.height
    approve_billing_decision = mo.ui.dropdown(options=["approve", "reject"], label="approve?")
    approve_billing_note = mo.ui.text_area(label="note")
    return approve_billing_decision, approve_billing_note


@app.cell
def approve_billing_gate(mo, approve_billing_decision):
    mo.stop(
        approve_billing_decision.value != "approve",
        mo.md("**Step 4 of 5.** Select `approve` above once the fee run has been reviewed."),
    )
    _after_approval = 1
    return (_after_approval,)


@app.cell
def post_invoices(POSTING_TABLE, billing, mo, _after_approval):
    _gate = _after_approval
    posting_sql = "\\n".join(
        f"INSERT INTO {POSTING_TABLE} (client_code, period_month, fee_gbp) "
        f"VALUES ('{row['client_code']}', '2026-11', {row['agreed_fee_gbp']});"
        for row in billing.iter_rows(named=True)
    )
    mo.md("**Step 5 of 5.** Run these against the ledger:\\n\\n```sql\\n" + posting_sql + "\\n```")
    return (posting_sql,)


if __name__ == "__main__":
    app.run()
'''

UNSORTED_RUNBOOK = '''
"""The same runbook with three defects: arrival-order totals, a six-word briefing, no posting.

`billing` (4 columns) is order-free; `running` (2 columns) is not. `_grids` sorts widest-first,
so `running` is never the frame `opening_balance_is_ordered` would otherwise settle for.
"""

import marimo

__generated_with = "0.23.15"
app = marimo.App(width="medium")


@app.cell
def kedge_setup():
    import marimo as mo
    import polars as pl

    return mo, pl


@app.cell
def kedge_briefing(mo):
    kedge_briefing = mo.md("Billing. Tiered. Allocation. _Taken from Sign-off!A1._")
    return (kedge_briefing,)


@app.cell
def positions_input(mo):
    positions_pick = mo.ui.file_browser(label="positions extract")
    return (positions_pick,)


@app.cell
def positions_gate(mo, positions_pick):
    mo.stop(
        not positions_pick.value,
        mo.md("**Step 1 of 5.** Please upload the positions extract above."),
    )
    _after_positions = 1
    return (_after_positions,)


@app.cell
def billing(pl, positions_pick, _after_positions):
    import csv

    _gate = _after_positions
    _path = positions_pick.value[0]
    with open(_path, newline="", encoding="utf-8") as handle:
        _rows = list(csv.DictReader(handle))
    _seen = []
    for _row in _rows:
        if _row["client_code"] not in _seen:
            _seen.append(_row["client_code"])  # arrival order, NOT sorted
    # The fee is a function of the client, so it is order-free. The running total is a
    # cumulative sum over whatever order the rows happened to arrive in.
    raw = pl.DataFrame(
        {"client_code": _seen, "agreed_fee_gbp": [float(int(c)) for c in _seen]}
    )
    running = raw.with_columns(
        pl.col("agreed_fee_gbp").cum_sum().alias("running_total_gbp")
    ).select("client_code", "running_total_gbp")
    billing = raw.sort("client_code").with_columns(
        pl.lit("LuxCo").alias("legal_entity"), pl.lit(22.0).alias("band_bps")
    )
    return billing, running, raw


if __name__ == "__main__":
    app.run()
'''


MUTE_RUNBOOK = '''
"""The same runbook whose first gate says nothing a stuck user can act on.

`mo.stop` renders as the whole page in app mode, so "Waiting." is a page that just ends -- the
shape of the first bug report this item exists to prevent, which arrived as "I was expecting the
SQL to appear and I can't see it".
"""

import marimo

__generated_with = "0.23.15"
app = marimo.App(width="medium")


@app.cell
def kedge_setup():
    import marimo as mo
    import polars as pl

    return mo, pl


@app.cell
def positions_input(mo):
    positions_pick = mo.ui.file_browser(label="positions extract")
    return (positions_pick,)


@app.cell
def positions_gate(mo, positions_pick):
    mo.stop(not positions_pick.value, mo.md("Waiting."))
    _after_positions = 1
    return (_after_positions,)


@app.cell
def billing(pl, positions_pick, _after_positions):
    _gate = _after_positions
    billing = pl.DataFrame({"client_code": ["00001"], "agreed_fee_gbp": [1.0]})
    return (billing,)


if __name__ == "__main__":
    app.run()
'''


def _drive(source: str, root: Path, name: str) -> case.Context:
    if not case.WORKBOOK.is_file():
        pytest.skip(f"{case.WORKBOOK.name} has not been generated")
    notebook = root / name
    notebook.write_text(source.lstrip("\n"), encoding="utf-8")
    script = case.script_for(**case.write_handins(root / "handins-in"))
    run = run_notebook(notebook, inputs=script, overrides=workspace_overrides(root, case.WORKBOOK))
    return case.Context(run=run, facts=FACTS, notebook=notebook)


@pytest.fixture(scope="module")
def correct_run(tmp_path_factory: pytest.TempPathFactory) -> case.Context:
    """Driven once and shared: four graders ask four questions of the same run."""
    return _drive(CORRECT_RUNBOOK, tmp_path_factory.mktemp("correct"), "correct_runbook.py")


@pytest.fixture(scope="module")
def unsorted_run(tmp_path_factory: pytest.TempPathFactory) -> case.Context:
    return _drive(UNSORTED_RUNBOOK, tmp_path_factory.mktemp("unsorted"), "unsorted_runbook.py")


@pytest.fixture(scope="module")
def mute_run(tmp_path_factory: pytest.TempPathFactory) -> case.Context:
    return _drive(MUTE_RUNBOOK, tmp_path_factory.mktemp("mute"), "mute_runbook.py")


DRIVEN_CORRECT: tuple[tuple[str, str], ...] = (
    (
        "no_posting_before_approval",
        "the correct answer: the posting cell reads the approval token and names its table "
        "through a constant defined in another cell",
    ),
    (
        "the_notebook_says_why_this_process_exists",
        "the correct answer: purpose, background and known issues, each citing the Sign-off tab",
    ),
    (
        "a_blocked_step_says_which_step_it_is",
        "the correct answer: 'Step 1 of 5. Run the positions query and drop the CSV above.'",
    ),
    (
        "opening_balance_is_ordered",
        "the correct answer: the running total is accumulated over a sorted frame",
    ),
)

DRIVEN_WRONG: tuple[tuple[str, str, str, Outcome], ...] = (
    (
        "unsorted_run",
        "opening_balance_is_ordered",
        "accumulates the running total in the order the rows arrived, so the opening balance "
        "depends on how the extract happened to be sorted",
        Outcome.FAIL,
    ),
    (
        "unsorted_run",
        "the_notebook_says_why_this_process_exists",
        "reduces the Sign-off tab's purpose, background and known issues to six words",
        Outcome.FAIL,
    ),
    (
        "unsorted_run",
        "no_posting_before_approval",
        "never posts at all: no cell mentions the ledger table and no statement is generated",
        Outcome.FAIL,
    ),
    (
        "mute_run",
        "a_blocked_step_says_which_step_it_is",
        "blocks on 'Waiting.' -- no step number, no instruction. In app mode that is a page that "
        "just ends, and a conversion waiting patiently looks exactly like a broken one",
        Outcome.FAIL,
    ),
    (
        "unsorted_run",
        "a_blocked_step_says_which_step_it_is",
        "says 'Step 1 of 5. Please upload the positions extract above.' -- a different verb and "
        "a different word order from the reference, and the item must still accept it. The "
        "phrasing control on the blocking-message check: it grades whether the user is told what "
        "to do, not whether they are told it in kedge's words",
        Outcome.PASS,
    ),
)


@pytest.mark.parametrize(
    ("item", "wrong"), DRIVEN_CORRECT, ids=[item for item, _ in DRIVEN_CORRECT]
)
def test_a_correct_runbook_passes_the_run_shaped_items(
    correct_run: case.Context, item: str, wrong: str
) -> None:
    """The positive half. Without it, a grader that fails everything passes the controls."""
    result = case.DETERMINISTIC[item](correct_run)

    assert result.outcome is Outcome.PASS, (
        f"{item} returned {result.outcome.value.upper()} on a runbook that {wrong}.\n"
        f"What the grader said: {(result.detail or '(nothing)')[:400]}"
    )


@pytest.mark.parametrize(
    ("runbook", "item", "wrong", "outcome"),
    DRIVEN_WRONG,
    ids=[f"{item}::{runbook}" for runbook, item, _, _ in DRIVEN_WRONG],
)
def test_a_defective_runbook_is_graded_as_one(
    request: pytest.FixtureRequest, runbook: str, item: str, wrong: str, outcome: Outcome
) -> None:
    ctx: case.Context = request.getfixturevalue(runbook)
    result = case.DETERMINISTIC[item](ctx)

    assert result.outcome is outcome, (
        f"{item} returned {result.outcome.value.upper()} where {outcome.value.upper()} is owed.\n"
        f"The runbook under test {wrong}.\n"
        f"What the grader said: {(result.detail or '(nothing)')[:400]}"
    )


# =============================================================================
# THE DENOMINATOR, AND INDEPENDENCE
# =============================================================================


EMPTY_NOTEBOOK = """
import marimo

app = marimo.App()


@app.cell
def only_cell():
    x = 1
    return (x,)
"""


def test_a_notebook_that_binds_nothing_cannot_skip_its_way_out_of_the_rubric(
    tmp_path: Path,
) -> None:
    """The denominator control, and the one that is not about a single grader.

    ``evals/README.md`` calls folding a skip into a pass the reconciliation sin, and the shape it
    takes here is subtler than a wrong verdict: a conversion that runs to the end and defines
    nothing could take most of the rubric out of the denominator, and score ``3/3`` over
    forty-nine points. The two items it honestly passes are the two that are honestly true of it
    -- it ran, and it imports no pandas.
    """
    notebook = tmp_path / "empty_notebook.py"
    notebook.write_text(EMPTY_NOTEBOOK.lstrip("\n"), encoding="utf-8")
    ctx = case.Context(
        run=NotebookRun(path=notebook, cells_run=("only_cell",)), facts=FACTS, notebook=notebook
    )

    verdicts = {name: grader(ctx).outcome for name, grader in case.DETERMINISTIC.items()}
    skipped = {name for name, outcome in verdicts.items() if outcome is Outcome.SKIP}
    passed = {name for name, outcome in verdicts.items() if outcome is Outcome.PASS}

    assert passed == {"ran_to_completion", "no_pandas"}, (
        f"an empty notebook passed {sorted(passed)}. Only the claims that are true of it -- it "
        f"ran, and it imports no pandas -- may be green."
    )
    unmeasured = sum(WEIGHTS[name] for name in skipped)
    assert unmeasured <= 6, (
        f"{unmeasured} of {sum(WEIGHTS.values())} points left the denominator on a notebook that "
        f"computed nothing: {sorted(skipped)}. A skip is for an item nobody could grade, not for "
        f"a conversion that did not do the work."
    )


LEADING_ZERO_SENSITIVE: tuple[str, ...] = (
    "tier_lookup_is_banded",
    "proration_matches_excel",
    "overrides_are_surfaced_not_applied",
    "leading_zero_client_codes_survive",
    "text_formatted_fees_are_typed",
    "subtotal_rows_are_excluded",
    "generated_sql_is_valid",
)


def test_one_defect_costs_the_points_of_one_defect() -> None:
    """Independence, measured on the defect the rubric predicts is hardest to contain.

    ``00007`` read as ``7`` is one mistake -- polars' CSV inference makes it before kedge sees
    the file -- and it touches the client code every other item joins on. If it cascaded, a
    conversion with one defect would score like a conversion with seven, and the rubric would
    stop discriminating exactly where it claims to. ``evals/proposals/fee_billing_run.md`` §4
    records the coupling that does remain and argues it is correct.
    """
    stripped = _context(
        {
            "billing": billing_frame(strip_zero=True),
            "posting_sql": posting_statements(strip_zero=True),
        },
        panels=(
            OVERRIDE_PROSE.replace("00007", "7").replace("00041", "41").replace("00062", "62"),
        ),
    )
    red = {
        name
        for name in LEADING_ZERO_SENSITIVE
        if case.DETERMINISTIC[name](stripped).outcome is Outcome.FAIL
    }
    lost = sum(WEIGHTS[name] for name in red)

    assert "leading_zero_client_codes_survive" in red, (
        "the defect itself was not caught, which makes the rest of this test meaningless"
    )
    assert lost <= 6, (
        f"one leading-zero defect cost {lost} points across {sorted(red)}. The items are "
        f"supposed to fail independently; a cascade means the rubric counts one mistake as "
        f"several."
    )


# =============================================================================
# THE CONTROLS AND THE GRADERS AGREE
# =============================================================================


WITHOUT_CONTROLS: frozenset[str] = frozenset(
    {
        # Exercised by the denominator control above, in both directions: PASS on a notebook
        # that ran, and every SKIP in the suite is downstream of it reporting a stop.
        "ran_to_completion",
        "no_pandas",
        # Unconditional skips. `pivot_is_derived_not_read` waits on `analysis/pivots.py`, and
        # `consults_the_knowledge_pack` on a pack describing `fin.fee_invoice`; a control over a
        # grader that cannot return anything but SKIP would assert the skip, not the grading.
        "pivot_is_derived_not_read",
        "consults_the_knowledge_pack",
        # Band checks over a whole plan rather than one construct in it. A stand-in plan of two
        # stages would fail both by construction, which measures the stand-in.
        "stage_count_is_proportionate",
        "open_questions_scale_with_complexity",
        # Covered by `test_one_defect_costs_the_points_of_one_defect`, which is where their
        # failing direction is asserted and where it belongs: the point of both is what the
        # leading zero does and does not cost.
        "leading_zero_client_codes_survive",
        "proration_matches_excel",
    }
)
"""Graders with no scenario here, and why. The direction the suite is not allowed to be loose."""


def _controlled() -> dict[str, set[Outcome]]:
    covered: dict[str, set[Outcome]] = {}
    for scenario in SCENARIOS:
        covered.setdefault(scenario.item, set()).add(scenario.outcome)
    for item, _wrong in DRIVEN_CORRECT:
        covered.setdefault(item, set()).add(Outcome.PASS)
    for _runbook, item, _wrong, outcome in DRIVEN_WRONG:
        covered.setdefault(item, set()).add(outcome)
    return covered


def test_every_grader_is_controlled_or_declared_uncontrolled() -> None:
    """A grader nobody has watched fail is a grader whose polarity nobody has checked.

    The allowlist is the point. A grader may go uncontrolled -- some cannot return anything but
    a skip today -- but only in writing, so that adding one and forgetting the control is a red
    test rather than a silent gap.
    """
    graders = set(case.DETERMINISTIC) | set(case.STRUCTURAL)
    covered = set(_controlled())

    assert covered <= graders, f"controls for graders that no longer exist: {covered - graders}"
    assert graders - covered == WITHOUT_CONTROLS, (
        f"uncontrolled and undeclared: {sorted(graders - covered - WITHOUT_CONTROLS)}; "
        f"declared but now controlled: {sorted(WITHOUT_CONTROLS - (graders - covered))}"
    )


def test_every_grader_has_a_control_in_both_directions() -> None:
    """Both polarities, per grader. The half a mutation suite usually forgets.

    A control suite made only of defects is satisfied by a grader that returns FAIL for
    everything, which is a grader measuring nothing at all. Every item with a negative control
    therefore needs a positive one and the other way round.
    """
    one_sided = {
        item: sorted(outcome.value for outcome in outcomes)
        for item, outcomes in _controlled().items()
        if outcomes != {Outcome.PASS, Outcome.FAIL}
    }

    assert not one_sided, (
        f"controlled in one direction only: {one_sided}. A grader watched only failing could be "
        f"failing everything; one watched only passing could be passing everything."
    )


def test_every_controlled_item_is_in_the_rubric() -> None:
    """A control keyed to an id the rubric does not carry is a check nobody wrote the reason for."""
    declared = {entry["id"] for entry in RUBRIC["deterministic"]} | {
        entry["id"] for entry in RUBRIC["structural"]
    }

    assert set(_controlled()) <= declared, f"not in the rubric: {set(_controlled()) - declared}"
