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
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from harness.drive import run_notebook, unused_inputs, workspace_overrides
from harness.model import EvalReport, ItemResult, Outcome, TierResult

logger = logging.getLogger(__name__)

CASES = ("adjustment_signoff", "fee_billing_run")

__all__ = [
    "CASES",
    "HandIns",
    "as_json",
    "grade",
    "handins_for",
    "human_script",
    "load_case",
    "load_plan",
    "placeholder_handins",
]


def load_case(name: str) -> Any:
    """Import a case's ``case`` module, or say which cases exist.

    A registered case whose module is not committed yet is reported as that, rather than as an
    ``ImportError`` traceback: :data:`CASES` is what ``run.py`` builds its ``--help`` from, so a
    case is listed as offered from the moment it is being built, and a half-built one has to say
    which half is missing. Only the case's own module going missing is translated -- a
    ``ModuleNotFoundError`` raised from *inside* it names a dependency the user has not installed,
    and swallowing that into "write your case" would send them the wrong way entirely.
    """
    if name not in CASES:
        msg = f"unknown eval {name!r}. Known: {', '.join(CASES)}"
        raise SystemExit(msg)

    module = f"{name}.case"
    try:
        return importlib.import_module(module)
    except ModuleNotFoundError as error:
        if error.name not in (module, name):
            raise
        msg = (
            f"eval {name!r} is registered but has no case module: expected "
            f"evals/{name}/case.py, defining WORKBOOK, RUBRIC, REFERENCE_NOTEBOOK, Context, "
            f"write_handins, script_for, DETERMINISTIC and STRUCTURAL."
        )
        raise SystemExit(msg) from error


def load_plan(path: Path | None) -> Any:
    """Read a saved process plan, or ``None``."""
    if path is None:
        return None
    from kedge.plan.store import plan_from_yaml

    return plan_from_yaml(path.read_text(encoding="utf-8"))


type HandIns = Mapping[str, Path] | tuple[Path, ...]
"""What a case's ``write_handins`` gives back: named inbound artifacts, or a bare sequence.

``adjustment_signoff`` returns two paths positionally, because its process has exactly two grids
and naming them would have been ceremony. That is not a shape case-independent code can hold,
though. ``pre, post = case.write_handins(...)`` is an assumption about every case there will ever
be, written into the one module that is supposed to know nothing about any of them, and a workbook
with three inbound artifacts cannot say itself in it. A mapping is what a new case should return:
it names its artifacts in its own ``script_for`` signature, and the harness never learns how many
there are. The sequence stays supported because it is still the right answer for two, and because
rewriting a working case to prove a point is how a green suite goes red for nothing.
"""


def handins_for(case: Any, directory: Path) -> HandIns:
    """Write a case's inbound artifacts, in whichever of the two shapes it declares them.

    The directory does not exist yet; making it is the case's job, because the case is what knows
    whether it is writing one file or six.
    """
    handins = case.write_handins(directory)
    return handins if isinstance(handins, Mapping) else tuple(handins)


def human_script(case: Any, handins: HandIns) -> dict[str, Any]:
    """The human's part of the runbook, fed its hand-ins however the case names them.

    Named artifacts are spread by keyword, so the case's ``script_for`` signature *is* the
    declaration of what the process asks a person to bring, and no caller has to count them. A
    bare sequence is spread positionally, which is what a two-argument ``script_for`` wants.

    Not called ``script_for``, though it is the obvious name: it takes the case whose
    ``script_for`` it calls, and one of the things it is called with is
    ``harness.convert._DrivenCase``, which has a ``script_for`` of its own. Two spellings of one
    word at that call site would read as a mistake even when it is not one.
    """
    if isinstance(handins, Mapping):
        return case.script_for(**handins)
    return case.script_for(*handins)


def placeholder_handins(case: Any) -> HandIns:
    """Hand-ins naming nothing on disk, for a caller that wants only the script's *keys*.

    ``harness.convert`` re-keys a script onto a generated notebook's widget names, which is a
    question about keys alone. ``script_for``'s keys never depend on its values, so placeholders
    answer it without a temporary directory, a workbook read and a pile of CSVs written purely to
    learn the keys of a dict.

    A case declares its artifact names in ``HANDINS`` rather than having them read off
    ``script_for``'s signature, because this is also handed the stand-in wrappers that
    ``harness.convert`` and the convert tests put in front of a case: a wrapper forwards an
    attribute faithfully, while its own ``(*args, **kwargs)`` signature says nothing at all. The
    two-path fallback is for ``adjustment_signoff``, which predates the declaration -- only the
    count is load-bearing there, and the count is two.
    """
    names = tuple(getattr(case, "HANDINS", ()))
    if names:
        return {name: Path(f"{name}.csv") for name in names}
    return (Path("pre.csv"), Path("post.csv"))


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
        script = human_script(case, handins_for(case, root / "handins-in"))
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
