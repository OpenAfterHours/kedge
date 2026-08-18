"""Grading: drive a case's notebook once, then run every rubric item over the result.

Split from ``run.py`` so tests can import it. ``run.py`` is argparse and printing; everything
that decides a score is here, and is exercised on every commit by
``tests/unit/test_evals_harness.py`` -- which runs the reference conversion for a full pass and
then mutates it, one defect at a time, to prove each grader can actually fail. A grader nobody
has watched go red is a grader nobody has checked the polarity of.
"""

from __future__ import annotations

import importlib
import json
import logging
import tempfile
from pathlib import Path
from typing import Any

import yaml

from harness.drive import run_notebook, unused_inputs, workspace_overrides
from harness.model import EvalReport, ItemResult, Outcome, TierResult

logger = logging.getLogger(__name__)

CASES = ("adjustment_signoff",)

__all__ = ["CASES", "as_json", "grade", "load_case", "load_plan"]


def load_case(name: str) -> Any:
    """Import a case's ``case`` module, or say which cases exist."""
    if name not in CASES:
        msg = f"unknown eval {name!r}. Known: {', '.join(CASES)}"
        raise SystemExit(msg)

    return importlib.import_module(f"{name}.case")


def load_plan(path: Path | None) -> Any:
    """Read a saved process plan, or ``None``."""
    if path is None:
        return None
    from kedge.plan.store import plan_from_yaml

    return plan_from_yaml(path.read_text(encoding="utf-8"))


def grade(
    case: Any,
    *,
    notebook: Path,
    plan: Any = None,
) -> EvalReport:
    """Drive the notebook once, then run every grader over the result."""
    rubric = yaml.safe_load(case.RUBRIC.read_text(encoding="utf-8"))
    facts = rubric["facts"]

    with tempfile.TemporaryDirectory(prefix="kedge-eval-") as workspace:
        root = Path(workspace)
        pre, post = case.write_handins(root / "handins-in")
        script = case.script_for(pre, post)
        run = run_notebook(
            notebook,
            inputs=script,
            overrides=workspace_overrides(root, case.WORKBOOK),
        )
        context = case.Context(
            run=run,
            facts=facts,
            notebook=notebook,
            unused_inputs=unused_inputs(run, script) if run.completed else (),
            plan=plan,
        )
        tiers = (
            _tier("deterministic", rubric, case.DETERMINISTIC, context),
            _tier("structural", rubric, case.STRUCTURAL, context),
        )

    notes = [
        f"notebook: {notebook}",
        f"workbook: {case.WORKBOOK.name}",
        f"run: {run.summary_line()}",
    ]
    if plan is None:
        notes.append("no plan supplied, so the structural tier was skipped in full")
    return EvalReport(case=rubric["workbook"], tiers=tiers, notes=tuple(notes))


def _tier(name: str, rubric: dict[str, Any], graders: dict[str, Any], context: Any) -> TierResult:
    """Grade one tier, in rubric order, carrying each item's declared weight.

    An id in the rubric with no grader is a ``SKIP`` naming itself rather than a crash: a rubric
    is allowed to describe something nobody has automated yet, and saying so is more useful than
    refusing to run. The reverse -- a grader with no rubric entry -- is caught by the tests,
    because that one is a check nobody wrote down the reason for.
    """
    items: list[ItemResult] = []
    for entry in rubric.get(name, []):
        item_id, weight = entry["id"], int(entry.get("weight", 1))
        grader = graders.get(item_id)
        if grader is None:
            items.append(
                ItemResult(
                    id=item_id,
                    outcome=Outcome.SKIP,
                    weight=weight,
                    detail="described in expected.yaml, but no grader implements it",
                )
            )
            continue
        try:
            result = grader(context)
        except Exception as error:
            logger.exception("grader %s raised", item_id)
            items.append(
                ItemResult(
                    id=item_id,
                    outcome=Outcome.SKIP,
                    weight=weight,
                    detail=f"the grader itself raised {type(error).__name__}: {error}",
                )
            )
            continue
        items.append(
            ItemResult(id=item_id, outcome=result.outcome, weight=weight, detail=result.detail)
        )
    return TierResult(name=name, items=tuple(items))


def as_json(report: EvalReport) -> str:
    return json.dumps(
        {
            "case": report.case,
            "earned": report.earned,
            "available": report.available,
            "ok": report.ok,
            "tiers": [
                {
                    "name": tier.name,
                    "items": [
                        {
                            "id": item.id,
                            "outcome": item.outcome.value,
                            "weight": item.weight,
                            "detail": item.detail,
                        }
                        for item in tier.items
                    ],
                }
                for tier in report.tiers
            ],
            "notes": list(report.notes),
        },
        indent=2,
    )
