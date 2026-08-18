"""The planning loop: triage, propose, review, store. The heart of Milestone 2.

The process plan is the single most important addition that flexibility demands (PLAN 2.2).
There is no canonical workbook, so a fixed scaffold would produce a transliteration; the model
therefore decides the *approach*, and the plan is where that decision is written down in a form
the user can correct while correcting is still cheap.

Four steps, and only one of them needs a model:

1. :func:`kedge.plan.triage.triage` — deterministic convertibility assessment. Stop here and say
   so if the answer is "don't".
2. :func:`kedge.plan.propose.propose_plan` — analysis in, plan out. One LLM call over the
   structural facts. No code generation.
3. :mod:`kedge.plan.review` — the user edits, reorders, splits, merges, answers open questions,
   and confirms or rejects drops. **Nothing is written to the notebook before approval.**
4. :func:`kedge.notebook.scaffold.sync_notebook` — the approved plan becomes cells.

Because steps 1, 3 and 4 need no LLM, a hand-written or previously-saved plan scaffolds instantly
with no model call at all — which is exactly what is wanted the second time the same monthly
process comes round.

:func:`run_plan` is the whole of steps 1-2 behind one call, and is what ``kedge plan propose``
invokes. Step 3 has a command surface of its own — ``kedge plan show``, ``acknowledge``,
``approve``, ``reject``, ``request-changes``, ``history`` — every one of which is a thin wrapper
over :mod:`kedge.plan.review` and none of which needs a model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from kedge.analysis.model import WorkbookAnalysis
from kedge.plan.model import (
    PLAN_SCHEMA_VERSION,
    Approval,
    ApprovalState,
    Assessment,
    Checkpoint,
    Confidence,
    DroppedRange,
    Handoff,
    HandoffMedium,
    OpenQuestion,
    PlanDraft,
    PlanError,
    ProcessPlan,
    Stage,
    StageKind,
)
from kedge.plan.propose import (
    Completer,
    ProposalError,
    ProposalRefusedError,
    ScriptedCompleter,
    completer_from_config,
    propose_plan,
)
from kedge.plan.review import render_plan, review_warnings
from kedge.plan.store import PlanStore, PlanStoreError, plan_from_yaml, plan_to_yaml
from kedge.plan.triage import Blocker, TriageResult, TriageVerdict, complexity, triage
from kedge.workspace import Workspace

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

__all__ = [
    "PLAN_SCHEMA_VERSION",
    "Approval",
    "ApprovalState",
    "Assessment",
    "Blocker",
    "Checkpoint",
    "Completer",
    "Confidence",
    "DroppedRange",
    "Handoff",
    "HandoffMedium",
    "OpenQuestion",
    "PlanDraft",
    "PlanError",
    "PlanRun",
    "PlanStore",
    "PlanStoreError",
    "ProcessPlan",
    "ProposalError",
    "ProposalRefusedError",
    "ScriptedCompleter",
    "Stage",
    "StageKind",
    "TriageResult",
    "TriageVerdict",
    "completer_from_config",
    "complexity",
    "load_analysis",
    "plan_from_yaml",
    "plan_to_yaml",
    "propose_plan",
    "render_plan",
    "review_warnings",
    "run_plan",
    "triage",
]


@dataclass(frozen=True, slots=True)
class PlanRun:
    """The outcome of one ``kedge plan propose`` invocation.

    ``plan`` is None exactly when triage refused, which is a legitimate result rather than a
    failure: an honest refusal with reasons is more useful than a notebook that looks complete
    and is not (PLAN 2.2).
    """

    workbook: Path
    triage: TriageResult
    plan: ProcessPlan | None = None
    saved_to: Path | None = None
    stopped: bool = False
    warnings: tuple[str, ...] = ()

    def render(self) -> str:
        """The full text a CLI should print: the triage, then the plan or the refusal."""
        if self.plan is None:
            return self.triage.explain()
        return f"{self.triage.explain()}\n\n{render_plan(self.plan, triage_result=self.triage)}"


def load_analysis(
    workbook: Path | str,
    *,
    analysis_path: Path | None = None,
    workspace: Workspace | None = None,
) -> WorkbookAnalysis:
    """Load the analysis for a workbook, preferring a saved artifact over re-analysing.

    Args:
        workbook: The workbook the analysis describes.
        analysis_path: An explicit ``analysis.json``. Defaults to the workspace's.
        workspace: An existing workspace, built from the workbook when omitted.

    Returns:
        The complete :class:`~kedge.analysis.model.WorkbookAnalysis`.

    Raises:
        PlanError: when no analysis exists and the analyser cannot produce one, naming the
            command that would.
    """
    space = workspace or Workspace.for_workbook(workbook)
    path = analysis_path if analysis_path is not None else space.analysis_path
    if path.is_file():
        logger.info("loading analysis from %s", path)
        try:
            return WorkbookAnalysis.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            msg = f"could not load the analysis at {path}: {exc}"
            raise PlanError(msg) from exc

    # The function, not the module of the same name. `kedge.analysis.__init__` now re-exports
    # `analyse` precisely so this is unambiguous, but the submodule path is kept here because it
    # cannot become ambiguous again: before the re-export existed, `from kedge.analysis import
    # analyse` bound the *module*, and that import succeeded and then failed at the call with
    # "module object is not callable" -- a confusing way to discover a missing analyser.
    try:
        from kedge.analysis.analyse import analyse
    except ImportError as exc:
        msg = (
            f"no analysis found at {path}, and the analyser is not available in this "
            f"installation. Run `kedge inspect {Path(workbook).name}` first, or pass an "
            f"analysis file explicitly."
        )
        raise PlanError(msg) from exc
    logger.info("no saved analysis at %s; analysing %s", path, workbook)
    return analyse(Path(workbook))


def run_plan(
    workbook: Path | str,
    *,
    analysis: WorkbookAnalysis | None = None,
    analysis_path: Path | None = None,
    workspace: Workspace | None = None,
    completer: Completer | None = None,
    dry_run: bool = False,
    force: bool = False,
    reseed: bool = True,
    max_attempts: int = 3,
) -> PlanRun:
    """Triage a workbook and, unless triage refuses, propose a process plan for it.

    This is the batch route to a plan, and ``kedge plan propose`` is a thin wrapper over it.
    There are two routes on purpose. In the chat the model authors a plan with ``propose_plan``
    and the user approves it in the pending panel, which is the path somebody converting a
    workbook interactively will take. This one takes an analysis and returns a plan with no
    conversation anywhere near it, which is what the corpus judging in PLAN §7 step 4 needs —
    ``dry_run`` over five dissimilar workbooks, reading the plans — and what somebody scripting a
    re-plan after a workbook changes wants. Both end at the same review gate: what this saves is
    a **draft**, and only :func:`kedge.plan.review.approve` moves it past that.

    It writes nothing to a notebook: a plan reaches the notebook only after a human approves it
    and :func:`kedge.notebook.scaffold.sync_notebook` is called with the approved version.

    Args:
        workbook: The workbook to plan.
        analysis: A pre-loaded analysis. Loaded from disk, or produced, when omitted.
        analysis_path: An explicit ``analysis.json`` to load.
        workspace: An existing workspace; one is built from the workbook when omitted.
        completer: The LLM seam. Built from config when omitted; pass a
            :class:`~kedge.plan.propose.ScriptedCompleter` to run the whole path offline.
        dry_run: Print the plan without saving it. This is the ``kedge plan propose --dry-run``
            path, and it is what makes judging ``propose`` across the whole corpus cheap
            (PLAN §7 step 4): run it over five dissimilar workbooks and read the plans.
        force: Propose even when triage says stop.
        reseed: Offer the most recent saved plan to the model as a worked example.
        max_attempts: Attempts, including validation repairs.

    Returns:
        A :class:`PlanRun`. ``stopped`` is True and ``plan`` is None when triage refused.

    Raises:
        PlanError: subclasses, when the analysis cannot be loaded or the model will not produce
            a valid plan.
    """
    path = Path(workbook).expanduser()
    space = workspace or Workspace.for_workbook(path)
    facts = analysis or load_analysis(path, analysis_path=analysis_path, workspace=space)

    assessment = triage(facts)
    if assessment.should_stop and not force:
        logger.warning("triage refused %s: %s", path.name, assessment.verdict.value)
        return PlanRun(workbook=path, triage=assessment, stopped=True)

    store = PlanStore.for_workspace(space)
    seed = store.seed() if reseed else None

    plan = propose_plan(
        facts,
        completer=completer or completer_from_config(space.config),
        triage_result=assessment,
        model=space.config.model.model,
        seed_plan=seed,
        max_attempts=max_attempts,
        force=force,
        version=store.next_version(),
    )
    warnings = review_warnings(plan, facts, triage_result=assessment)

    saved: Path | None = None
    if not dry_run:
        space.ensure_dirs()
        plan, saved = store.save_next(plan)

    return PlanRun(
        workbook=path,
        triage=assessment,
        plan=plan,
        saved_to=saved,
        stopped=False,
        warnings=tuple(warnings),
    )


def iter_plan_history(workspace: Workspace) -> Iterator[ProcessPlan]:
    """Yield every saved plan version for a workspace, oldest first."""
    yield from PlanStore.for_workspace(workspace).history()
