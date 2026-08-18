"""Write ``plan.yaml`` -- the reference process plan for the adjustment workbook.

The structural tier grades a :class:`~kedge.plan.model.ProcessPlan`, so it needs one that passes
before it can be trusted to fail anything. Same argument as the reference notebook: a grader
that has never seen a good input is a grader nobody has checked the polarity of.

Written through the real model and the real review functions rather than as hand-authored YAML,
so the file on disk is provably a plan kedge would accept -- schema, stage graph, drop
acknowledgement, approval and all. A plan that only *looks* valid would make every structural
item skip with a load error, and the tier would read as "not implemented" rather than "broken".

Run it::

    uv run python evals/adjustment_signoff/build_plan.py
"""

from __future__ import annotations

import argparse
import hashlib
from datetime import UTC, datetime
from pathlib import Path

from kedge.plan.model import (
    Assessment,
    Briefing,
    Checkpoint,
    Confidence,
    DroppedRange,
    Handoff,
    OpenQuestion,
    PlanDraft,
    ProcessPlan,
    SourceOrigin,
    Stage,
    StageKind,
    StageSource,
)
from kedge.plan.review import acknowledge_all_drops, approve
from kedge.plan.store import plan_to_yaml

PLAN_NAME = "plan.yaml"
WORKBOOK = Path(__file__).resolve().parent / "q2_accrual_adjustment.xlsx"

EXTRACT_SQL = """SELECT
    a.trade_id,
    a.entity_id,
    e.entity_name,
    a.cost_centre,
    a.accrual_gbp,
    a.period_end,
    a.status
FROM fin.accruals AS a
INNER JOIN fin.entities AS e
    ON e.entity_id = a.entity_id
WHERE a.period_end = {period_end}
  AND a.ledger = {ledger}
ORDER BY a.entity_id, a.trade_id"""

UPDATE_TEMPLATE = (
    "UPDATE fin.accruals SET accrual_gbp = {accrual_gbp_after}, "
    "adjustment_note = {adjustment_note} WHERE trade_id = {trade_id}"
)


BRIEFING = Briefing(
    purpose=(
        "Records the quarterly uplift applied to statutory accruals following the reforecast, "
        "and evidences that the adjustment was applied as approved. The output is the sign-off "
        "that Financial Control keeps as the control record for the period."
    ),
    background=(
        "The June reforecast moved the accrual basis for the affected entities from the 2025 "
        "rate card to the 2026 one. Finance agreed a flat 4.5% uplift as the practical "
        "equivalent, to be applied to statutory-ledger accruals at the period end only. "
        "Management accounts are unaffected."
    ),
    cadence="Quarterly, once the reforecast has been agreed at the finance committee.",
    audience=(
        "Prepared by Financial Control, reviewed by the Head of Statutory Reporting. Control "
        "reference FC-Q2-2026-014."
    ),
    watch_for=[
        "One trade carries no accrual value in the source system pending a cost-centre "
        "reallocation. Excel read the blank as zero and the workbook predicted 0.00 for it; the "
        "warehouse left it NULL, so the verification step reports it as a break every run until "
        "the reallocation happens.",
        "Cancelled trades are excluded by the statement, so they must be excluded here too. "
        "Four of them sit inside the adjusted entities.",
        "The workbook's own generated SQL was invalid for the entity whose name contains an "
        "apostrophe. This notebook renders statements through kedge.sql instead, so its output "
        "deliberately differs from the spreadsheet's.",
    ],
    sources=[
        "Sign-off!A3:A4 (Purpose)",
        "Sign-off!A6:A7 (Background)",
        "Sign-off!A12:A13 (Scope)",
        "Sign-off!A15:A16 (Known issues)",
        "Sign-off!A24:B28 (Sign-off block)",
        "Adjustment!G17:G92, read during conversion",
    ],
)

STAGES = [
    Stage(
        id="extract_query",
        intent="Hand the user the query that produces the pre-adjustment position",
        kind=StageKind.HANDOFF,
        sources=[StageSource(origin=SourceOrigin.QUERY, ref="AccrualExtract")],
        handoff=Handoff(
            instruction=(
                "Run this against FinanceWarehouse and bring the whole grid back, header row "
                "included."
            ),
            statement=EXTRACT_SQL,
            connection="FinanceWarehouse",
            parameters=["period_end", "ledger"],
        ),
        notes=(
            "Recovered from xl/connections.xml and reproduced on the Pre-Adjustment tab above "
            "the data it returned."
        ),
    ),
    Stage(
        id="pre_adjustment",
        intent="The statutory accrual position as it stands before any adjustment",
        kind=StageKind.LOAD,
        depends_on=["extract_query"],
        sources=[StageSource(origin=SourceOrigin.HANDIN, ref="pre-adjustment extract")],
        confidence=Confidence.HIGH,
        assumptions=["One row per trade per period end; trade_id is unique within the extract."],
    ),
    Stage(
        id="adjust",
        intent="Apply the agreed 4.5% Q2 uplift to in-scope statutory accruals",
        kind=StageKind.TRANSFORM,
        depends_on=["pre_adjustment"],
        sources=[StageSource(origin=SourceOrigin.STAGE, ref="pre_adjustment")],
        confidence=Confidence.HIGH,
        operations=[
            "adjustment_c17_c92",
            "adjustment_d17_d92",
            "adjustment_e17_e92",
            "adjustment_f17_f92",
            "adjustment_d94_f94",
        ],
        assumptions=[
            "Scope is the UPDATE's own WHERE clause: four entities (E-04, E-07, E-09, E-12), "
            "statutory ledger, period end 2026-06-30, excluding CANCELLED trades.",
            "A blank accrual is zero, because Excel's ROUND reads the empty cell as zero and "
            "the workbook's cached value for that row is 0.00. Whether that is right for the "
            "process is a separate question; the translation has to reproduce it.",
            "ROUND is Excel's: half away from zero over the 15-significant-digit value. Four "
            "of the 76 rows are a penny out without it.",
        ],
        notes=(
            "The Sign-off tab says 'the three affected entities' where the scope row and the "
            "UPDATE both name four. Four is what ran, so four is what this reproduces -- see "
            "the open question."
        ),
    ),
    Stage(
        id="approve_adjustment",
        intent="Record the decision to apply the adjustment before it is applied",
        kind=StageKind.CHECKPOINT,
        depends_on=["adjust"],
        checkpoint=Checkpoint(
            question="Approve this adjustment and generate the UPDATE?",
            guidance=(
                "Check the row count and the movement against what the finance committee "
                "agreed. Note that the Sign-off tab's own figures are stale."
            ),
        ),
    ),
    Stage(
        id="update_statement",
        intent="Hand the user the UPDATE that applies the approved adjustment",
        kind=StageKind.HANDOFF,
        depends_on=["approve_adjustment"],
        handoff=Handoff(
            instruction=(
                "Run this against FinanceWarehouse in one transaction, then run the extract "
                "query again and bring the new grid back."
            ),
            template=UPDATE_TEMPLATE,
            built_from="adjust",
            connection="FinanceWarehouse",
        ),
        operations=["adjustment_g17_g92"],
        notes=(
            "Adjustment!G is a column of ='UPDATE ... '&F17&'...' filled down 76 rows. The "
            "analyser reports it as a dead region because nothing in the workbook reads it -- "
            "its consumer is a person with a clipboard. Rendered through kedge.sql rather than "
            "reproduced: the workbook's own statement is invalid SQL for the entity whose name "
            "contains an apostrophe."
        ),
    ),
    Stage(
        id="post_adjustment",
        intent="The re-extract, as evidence the update did what was intended",
        kind=StageKind.LOAD,
        depends_on=["update_statement"],
        sources=[StageSource(origin=SourceOrigin.HANDIN, ref="post-adjustment extract")],
        confidence=Confidence.HIGH,
    ),
    Stage(
        id="verification",
        intent="Compare the re-extract against what the notebook predicted, row by row",
        kind=StageKind.TRANSFORM,
        depends_on=["adjust", "post_adjustment"],
        sources=[
            StageSource(origin=SourceOrigin.STAGE, ref="adjust"),
            StageSource(origin=SourceOrigin.STAGE, ref="post_adjustment"),
        ],
        confidence=Confidence.HIGH,
        assumptions=[
            "Compared per row on trade_id, not on a total. A total agrees whether the update "
            "hit 76 rows or 74."
        ],
    ),
    Stage(
        id="signoff",
        intent="The impact statement, recomputed, saying what was verified as well as computed",
        kind=StageKind.OUTPUT,
        depends_on=["verification"],
        sources=[StageSource(origin=SourceOrigin.STAGE, ref="verification")],
        confidence=Confidence.HIGH,
        notes=(
            "The Sign-off tab's own impact figures are not inputs. They are stale -- they "
            "predate two trades joining the scope -- and were signed anyway."
        ),
    ),
]

QUESTIONS = [
    OpenQuestion(
        question=(
            "The Sign-off tab says the uplift was applied to 'the three affected entities', "
            "but the scope row and the UPDATE statement both name four: E-04, E-07, E-09 and "
            "E-12. The statement is what ran. Which is right?"
        ),
        context=(
            "If the memo is right, one entity was adjusted that should not have been, and the "
            "movement is overstated. If the statement is right, the memo understates what was "
            "changed and was signed on that basis."
        ),
    ),
    OpenQuestion(
        question=(
            "One in-scope trade has no accrual value. Excel read the blank as zero and the "
            "workbook predicts 0.00 for it; the warehouse left it NULL. Should the adjustment "
            "skip rows with no accrual rather than writing zero to them?"
        ),
        context=(
            "The re-extract disagrees with the workbook on exactly this row, so whatever the "
            "answer, the workbook's own sign-off claimed a position it did not have."
        ),
    ),
]


def build(path: Path) -> None:
    """Construct, review and approve the plan, then write it."""
    draft = PlanDraft(
        stages=STAGES,
        open_questions=QUESTIONS,
        dropped=[
            DroppedRange(
                range="Sign-off!B20:D20",
                reason=(
                    "The typed impact summary. Recomputed downstream rather than read: the "
                    "figures are stale, and carrying them forward would carry the staleness."
                ),
            )
        ],
        briefing=BRIEFING,
        assessment=Assessment(
            convertible=0.85,
            rationale=(
                "Every calculation is a fill-down of two ROUNDs over a lookup, which "
                "translates cleanly. What is not code is the three steps a person carries "
                "out elsewhere, and those become hand-offs rather than blockers."
            ),
        ),
        summary=(
            "A quarterly statutory accrual uplift: extract, compute, approve, apply by "
            "statement, re-extract, verify, sign off. Four of the seven steps are a person "
            "doing something kedge cannot do, so the notebook is a runbook rather than a "
            "pipeline."
        ),
    )
    plan = ProcessPlan.from_draft(
        draft,
        workbook=WORKBOOK.name,
        workbook_sha256=hashlib.sha256(WORKBOOK.read_bytes()).hexdigest(),
        created_at=datetime(2026, 8, 18, 9, 0, tzinfo=UTC),
    )
    approved = approve(
        acknowledge_all_drops(plan, note="Impact summary confirmed stale; recomputing."),
        by="evals/adjustment_signoff/build_plan.py",
    )
    path.write_text(plan_to_yaml(approved), encoding="utf-8")
    print(f"wrote {path.name}: {len(approved.stages)} stages, v{approved.version}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / PLAN_NAME)
    build(parser.parse_args().out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
