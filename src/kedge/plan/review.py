"""Render a plan for a human, accept their edits, and diff two versions. No LLM.

This module is the approval gate. **Nothing is written to the notebook before the plan is
approved**, and that is enforced structurally rather than by convention: approval is state on the
plan, every edit here resets it to ``DRAFT`` — except a rejection, which is carried forward
because it is terminal — and :mod:`kedge.notebook.scaffold` refuses a plan whose
``approval.state`` is not ``APPROVED``. There is no separate flag to forget to check.

There are two chokepoints and everything goes through one of them. Content edits go through
:func:`_revise`, which bumps the version and resets approval; review decisions go through
:func:`_decide`, which does neither and refuses the two transitions that would defeat the gate —
out of ``REJECTED`` at all, and out of ``APPROVED`` without the withdrawal being asked for by
name. A verb that writes ``approval`` itself is a verb that can forget one of those, which is how
:func:`request_changes` came to be a route round a rejection.

Every edit returns a **new plan at the next version**. Plans are frozen, so an edit cannot
happen by accident, and the version history is what makes "when the process changes next
quarter, the diff of the plan is the change record" true (PLAN 2.2).

Rendering is plain text with no ``rich`` dependency, because this module is library code that the
web UI will import later; the CLI is welcome to wrap the same models in panels.

References:
- PLAN.md 2.2 (review, drops, open questions), M2 step 3.
"""

from __future__ import annotations

import logging
import textwrap
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from kedge.plan.model import (
    Approval,
    ApprovalState,
    OpenQuestion,
    PlanError,
    ProcessPlan,
    SourceOrigin,
    Stage,
    StageSource,
)
from kedge.plan.triage import complexity

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from kedge.analysis.model import WorkbookAnalysis
    from kedge.plan.triage import TriageResult

logger = logging.getLogger(__name__)

__all__ = [
    "PlanDiff",
    "PlanNotApprovableError",
    "StageChange",
    "acknowledge_all_drops",
    "acknowledge_drop",
    "add_question",
    "add_stage",
    "answer_question",
    "approve",
    "diff_plans",
    "edit_stage",
    "merge_stages",
    "reject",
    "remove_stage",
    "render_diff",
    "render_plan",
    "reorder_stages",
    "request_changes",
    "review_warnings",
    "split_stage",
]


class PlanNotApprovableError(PlanError):
    """Approval was attempted while something still blocks it.

    The message lists every blocker, because a user told only the first one will fix it, retry,
    and be told the second.
    """


# =============================================================================
# REVISION
# =============================================================================


def _revise(plan: ProcessPlan, **changes: Any) -> ProcessPlan:
    """Return a new plan at the next version with ``changes`` applied.

    Any content edit resets approval to ``DRAFT``. That is the point: an approval granted
    against one decomposition must not survive a change to it, or the gate protects nothing.
    Revalidation runs in full, so an edit that breaks the stage graph fails here rather than in
    the scaffolder.

    **A rejection is carried forward instead.** ``REJECTED`` is the one terminal state — "a
    rejected plan can never be approved; propose a new one" (:func:`reject`) — and resetting it to
    ``DRAFT`` here disarmed the guard in
    :meth:`~kedge.plan.model.ProcessPlan.approval_blockers` that says so. A one-sentence edit to a
    decomposition the user turned down would then be approvable, and approving that one sentence
    would put the whole of the rejected plan into force. The rejector and their reason travel with
    it, because "why was this turned down" outlives the version it was turned down at. The way
    forward from a rejection is a *new* plan: :meth:`~kedge.plan.model.ProcessPlan.from_draft`
    starts at ``DRAFT``, so re-seeding from ``to_draft()`` is unaffected.
    """
    raw = plan.model_dump(mode="python")
    raw.update(changes)
    raw["version"] = plan.version + 1
    raw["based_on_version"] = plan.version
    raw["created_at"] = datetime.now(UTC)
    raw["generated_by"] = "human"
    if plan.approval.state is not ApprovalState.REJECTED:
        raw["approval"] = Approval().model_dump(mode="python")
    return ProcessPlan.model_validate(raw)


# =============================================================================
# STAGE EDITS
# =============================================================================


def reorder_stages(plan: ProcessPlan, order: Sequence[str]) -> ProcessPlan:
    """Reorder the stages. ``order`` must name every stage exactly once.

    Args:
        plan: The plan to revise.
        order: Stage ids in the desired order.

    Returns:
        A new plan at the next version.

    Raises:
        PlanError: when the ids do not match the plan's stages exactly.
    """
    wanted = list(order)
    if sorted(wanted) != sorted(plan.stage_ids):
        missing = sorted(set(plan.stage_ids) - set(wanted))
        unknown = sorted(set(wanted) - set(plan.stage_ids))
        msg = (
            f"reordering must list every stage exactly once; missing: {missing or 'none'}, "
            f"unknown: {unknown or 'none'}"
        )
        raise PlanError(msg)
    by_id = {stage.id: stage for stage in plan.stages}
    return _revise(plan, stages=[by_id[stage_id].model_dump(mode="python") for stage_id in wanted])


def edit_stage(plan: ProcessPlan, stage_id: str, **changes: Any) -> ProcessPlan:
    """Replace named fields on one stage.

    Sources are classified with the rest of the plan in hand, through
    :meth:`~kedge.plan.model.Stage.validate_in_plan`. ``Stage.model_validate`` on its own cannot
    tell an upstream stage id from a named range, so an edit passing ``sources=["load_handin"]``
    would land as ``unknown`` where the identical text in the plan's YAML lands as ``stage`` — the
    edit verb quietly weakening a plan it is holding open.

    Args:
        plan: The plan to revise.
        stage_id: The stage to edit.
        **changes: Field names and values, validated against :class:`Stage`.
    """
    stage = _require_stage(plan, stage_id)
    raw = stage.model_dump(mode="python")
    raw.update(changes)
    updated = Stage.validate_in_plan(raw, plan.stage_ids)
    stages = [updated if item.id == stage_id else item for item in plan.stages]
    renamed = updated.id != stage_id
    if renamed:
        stages = [_repoint(item, {stage_id: updated.id}) for item in stages]
    return _revise(plan, stages=[item.model_dump(mode="python") for item in stages])


def add_stage(plan: ProcessPlan, stage: Stage, *, after: str | None = None) -> ProcessPlan:
    """Insert a stage, optionally after a named one. Appended when ``after`` is None."""
    if plan.stage(stage.id) is not None:
        msg = f"stage {stage.id!r} already exists in this plan"
        raise PlanError(msg)
    stages = list(plan.stages)
    if after is None:
        stages.append(stage)
    else:
        _require_stage(plan, after)
        index = plan.stage_ids.index(after) + 1
        stages.insert(index, stage)
    return _revise(plan, stages=[item.model_dump(mode="python") for item in stages])


def remove_stage(plan: ProcessPlan, stage_id: str) -> ProcessPlan:
    """Remove a stage, detaching any dependency on it and any source that read it.

    Dependencies are dropped rather than rewired: guessing which of the removed stage's own
    dependencies should take its place is exactly the sort of silent decision this whole module
    exists to prevent. A source naming it goes the same way, for the same reason — and because a
    source pointing at a stage that is no longer in the plan will not validate.
    """
    _require_stage(plan, stage_id)
    remaining = [stage for stage in plan.stages if stage.id != stage_id]
    if not remaining:
        msg = "a plan needs at least one stage; removing the last one is not a review action"
        raise PlanError(msg)
    detached = [
        stage.model_copy(
            update={
                "depends_on": [d for d in stage.depends_on if d != stage_id],
                "sources": [
                    source
                    for source in stage.sources
                    if not (source.origin is SourceOrigin.STAGE and source.ref == stage_id)
                ],
            }
        )
        for stage in remaining
    ]
    return _revise(plan, stages=[stage.model_dump(mode="python") for stage in detached])


def split_stage(plan: ProcessPlan, stage_id: str, parts: Sequence[Stage]) -> ProcessPlan:
    """Replace one stage with several, chained in the order given.

    The first part inherits the original's dependencies; each subsequent part depends on the one
    before it; anything that depended on the original now depends on the last part. That is the
    only wiring that preserves the plan's meaning without guessing.

    Args:
        plan: The plan to revise.
        stage_id: The stage being split.
        parts: Two or more replacement stages, in order.
    """
    original = _require_stage(plan, stage_id)
    if len(parts) < 2:
        msg = "splitting a stage needs at least two parts"
        raise PlanError(msg)

    chained: list[Stage] = []
    for index, part in enumerate(parts):
        depends = list(original.depends_on) if index == 0 else [parts[index - 1].id]
        chained.append(part.model_copy(update={"depends_on": depends}))

    last = chained[-1].id
    stages: list[Stage] = []
    for stage in plan.stages:
        if stage.id == stage_id:
            stages.extend(chained)
            continue
        stages.append(_repoint(stage, {stage_id: last}))
    return _revise(plan, stages=[stage.model_dump(mode="python") for stage in stages])


def merge_stages(
    plan: ProcessPlan,
    stage_ids: Sequence[str],
    *,
    into_id: str,
    intent: str | None = None,
) -> ProcessPlan:
    """Merge several stages into one, at the position of the first.

    Sources, assumptions and operation ids are unioned in order; dependencies are unioned minus
    the merged stages themselves; anything depending on any of them now depends on the merged
    stage. Confidence takes the lowest of the merged stages, because a merged step is only as
    trustworthy as its weakest part.

    A source naming one of the merged stages drops out of the union along with the dependency on
    it: an edge between two stages that are now one stage is not an input any more.
    """
    if len(stage_ids) < 2:
        msg = "merging needs at least two stages"
        raise PlanError(msg)
    merged = [_require_stage(plan, stage_id) for stage_id in stage_ids]
    if any(stage.is_checkpoint for stage in merged) and not all(
        stage.is_checkpoint for stage in merged
    ):
        msg = "refusing to merge a checkpoint with automated stages; the control would disappear"
        raise PlanError(msg)

    ids = set(stage_ids)
    combined = Stage(
        id=into_id,
        intent=intent or "; ".join(stage.intent for stage in merged),
        kind=merged[0].kind,
        sources=_unique(
            source
            for stage in merged
            for source in stage.sources
            if not (source.origin is SourceOrigin.STAGE and source.ref in ids)
        ),
        depends_on=_unique(
            dependency
            for stage in merged
            for dependency in stage.depends_on
            if dependency not in ids
        ),
        confidence=min(merged, key=lambda stage: _CONFIDENCE_RANK[stage.confidence]).confidence,
        assumptions=_unique(item for stage in merged for item in stage.assumptions),
        excel_pattern=merged[0].excel_pattern,
        operations=_unique(item for stage in merged for item in stage.operations),
        checkpoint=next((stage.checkpoint for stage in merged if stage.checkpoint), None),
        notes="; ".join(stage.notes for stage in merged if stage.notes) or None,
    )

    stages: list[Stage] = []
    inserted = False
    for stage in plan.stages:
        if stage.id in ids:
            if not inserted:
                stages.append(combined)
                inserted = True
            continue
        stages.append(_repoint(stage, dict.fromkeys(ids, into_id)))
    return _revise(plan, stages=[stage.model_dump(mode="python") for stage in stages])


_CONFIDENCE_RANK = {
    "unknown": 0,
    "low": 1,
    "n/a": 2,
    "medium": 3,
    "high": 4,
}


def _repoint(stage: Stage, mapping: dict[str, str]) -> Stage:
    """Rewrite a stage's dependencies *and* its stage sources through an id mapping.

    Sources travel with dependencies because both name a stage and, since schema 1.1, both are
    validated. A rename or a merge that rewrote one and left the other would point a source at an
    id that no longer exists, and the revalidation :func:`_revise` runs would then reject the
    *edit* — a reviewer renaming a stage would be told their plan reads an unknown one.

    Self-references are dropped rather than kept: after a merge, a source naming one of the merged
    stages is describing the stage it is now part of.

    Only sources the mapping *moved* are de-duplicated. A merge legitimately collapses two edges
    onto one stage and the second is then noise, but a stage that already listed the same range
    twice said so deliberately — under 1.0 a source was a bare string and repeating one was how
    you said "twice" — and a rename somewhere else in the plan is no reason to edit it.
    """
    repointed = _unique(
        mapping.get(dependency, dependency)
        for dependency in stage.depends_on
        if mapping.get(dependency, dependency) != stage.id
    )
    sources: list[StageSource] = []
    for original in stage.sources:
        source = _moved(original, mapping)
        if source.origin is SourceOrigin.STAGE and source.ref == stage.id:
            continue
        if source is not original and source in sources:
            continue
        sources.append(source)
    if repointed == stage.depends_on and sources == stage.sources:
        return stage
    return stage.model_copy(update={"depends_on": repointed, "sources": sources})


def _moved(source: StageSource, mapping: dict[str, str]) -> StageSource:
    """A stage source pointing at wherever the stage it names has gone."""
    if source.origin is not SourceOrigin.STAGE or source.ref not in mapping:
        return source
    return source.model_copy(update={"ref": mapping[source.ref]})


def _unique[T](values: Iterable[T]) -> list[T]:
    """Order-preserving de-duplication.

    Generic because it de-duplicates two different things now: dependency ids, and the frozen
    :class:`~kedge.plan.model.StageSource` objects a merge unions. Both are hashable; a frozen
    pydantic model is.
    """
    return list(dict.fromkeys(values))


def _require_stage(plan: ProcessPlan, stage_id: str) -> Stage:
    stage = plan.stage(stage_id)
    if stage is None:
        msg = f"no stage {stage_id!r} in this plan; it has: {', '.join(plan.stage_ids)}"
        raise PlanError(msg)
    return stage


# =============================================================================
# QUESTIONS AND DROPS
# =============================================================================


def answer_question(plan: ProcessPlan, question: str | int, answer: str) -> ProcessPlan:
    """Record an answer to an open question, by index or by matching text."""
    index = _question_index(plan, question)
    answered = plan.open_questions[index].model_copy(
        update={"answer": answer, "answered_at": datetime.now(UTC)}
    )
    questions = list(plan.open_questions)
    questions[index] = answered
    return _revise(plan, open_questions=[item.model_dump(mode="python") for item in questions])


def add_question(plan: ProcessPlan, question: str, *, context: str | None = None) -> ProcessPlan:
    """Add an open question the reviewer noticed and the model did not."""
    questions = [*plan.open_questions, OpenQuestion(question=question, context=context)]
    return _revise(plan, open_questions=[item.model_dump(mode="python") for item in questions])


def _question_index(plan: ProcessPlan, question: str | int) -> int:
    if isinstance(question, int):
        if not 0 <= question < len(plan.open_questions):
            msg = f"no open question at index {question}; the plan has {len(plan.open_questions)}"
            raise PlanError(msg)
        return question
    for index, item in enumerate(plan.open_questions):
        if item.question == question:
            return index
    msg = f"no open question matching {question!r}"
    raise PlanError(msg)


def _drop_refusal_question(range_: str) -> str:
    """The question a refused drop raises, composed in one place so it can be taken back.

    Withdrawing it when the refusal is overturned means finding it again, and the only handle an
    :class:`~kedge.plan.model.OpenQuestion` offers is its text. Composed here, the sentence a
    withdrawal looks for is the sentence the refusal wrote — equality is identity by construction,
    with nothing to drift and no prefix rule to be over-eager.
    """
    return (
        f"{range_} was proposed for dropping and the drop was rejected, so it must "
        f"be kept. Which stage consumes it, and what does it feed?"
    )


def acknowledge_drop(
    plan: ProcessPlan,
    range_: str,
    *,
    accepted: bool = True,
    note: str | None = None,
) -> ProcessPlan:
    """Confirm or reject one proposed drop.

    Rejecting a drop means the range must be kept, which leaves the plan with no stage that
    consumes it — so an open question is raised automatically and approval stays blocked until
    somebody says which stage should. Silently accepting a rejection would be exactly the kind
    of quiet hole this gate exists to prevent.

    **Overturning a refusal takes that question back.** Confirming a drop that was refused answers
    it by withdrawing its premise: nothing has to consume a range that is going. Left behind, the
    plan asserted both things at once — the drop confirmed, and a question insisting the range be
    kept — and the contradiction outlived the review, because no verb here removes a question.

    Three things narrow that withdrawal, and it is the only place in this module that deletes
    anything, so each is load-bearing. The sentence must be the exact one
    :func:`_drop_refusal_question` composed for *this* range — a question a model wrote that merely
    opens by naming the range is somebody's real question. It must be unanswered — an answer is a
    decision. And **the drop must actually have been refused**, which is the one that reads as
    redundant and is not: :meth:`~kedge.plan.store.PlanStore.seed` offers the previous plan to the
    model when a new one is proposed, ``propose`` strips the human decisions but carries question
    text across verbatim, and a re-proposing model commonly repeats the questions it was shown. So
    a fresh proposal can arrive holding last quarter's refusal sentence against a drop nobody has
    refused this time, and confirming that drop would delete an inherited question unread.

    Args:
        plan: The plan to revise.
        range_: The dropped range, as written in the plan.
        accepted: True to confirm the drop, False to keep the range.
        note: The reviewer's reason, recorded either way.
    """
    matches = [drop for drop in plan.dropped if drop.range == range_]
    if not matches:
        known = ", ".join(drop.range for drop in plan.dropped) or "none"
        msg = f"no dropped range {range_!r} in this plan; it proposes dropping: {known}"
        raise PlanError(msg)

    updated = [
        drop.model_copy(
            update={
                "acknowledged": True,
                "accepted": accepted,
                "note": note,
                "acknowledged_at": datetime.now(UTC),
            }
        )
        if drop.range == range_
        else drop
        for drop in plan.dropped
    ]
    raised = _drop_refusal_question(range_)
    questions = list(plan.open_questions)
    if not accepted:
        questions.append(OpenQuestion(question=raised, context=note))
    elif any(drop.rejected for drop in matches):
        questions = [
            question for question in questions if question.answered or question.question != raised
        ]
    return _revise(
        plan,
        dropped=[drop.model_dump(mode="python") for drop in updated],
        open_questions=[item.model_dump(mode="python") for item in questions],
    )


def acknowledge_all_drops(plan: ProcessPlan, *, note: str | None = None) -> ProcessPlan:
    """Confirm every outstanding drop in one action.

    A convenience for the common case where the reviewer has read the list and agrees with all of
    it. It still records an explicit acknowledgement per drop, so the audit trail is identical to
    acknowledging them one at a time.
    """
    if not plan.unacknowledged_drops:
        return plan
    now = datetime.now(UTC)
    updated = [
        drop
        if drop.acknowledged
        else drop.model_copy(
            update={"acknowledged": True, "accepted": True, "note": note, "acknowledged_at": now}
        )
        for drop in plan.dropped
    ]
    return _revise(plan, dropped=[drop.model_dump(mode="python") for drop in updated])


# =============================================================================
# APPROVAL
# =============================================================================


def _decide(
    plan: ProcessPlan,
    *,
    state: ApprovalState,
    by: str,
    note: str | None,
    doing: str,
    withdraw_approval: bool = False,
) -> ProcessPlan:
    """Return the plan carrying a new review decision. Content and version are untouched.

    This is the approval-side twin of :func:`_revise`, and the only place a review verb writes
    ``approval``. A verb that builds its own :class:`Approval` is a verb that can forget a guard,
    which is exactly how :func:`request_changes` came to be able to take a plan *out* of
    ``REJECTED``.

    Two transitions are refused.

    **Out of ``REJECTED``.** It is the one terminal state — "a rejected plan can never be
    approved; propose a new one" (:func:`reject`) — and any route out of it launders a rejection
    into an approval, because ``CHANGES_REQUESTED`` approves cleanly. Sending a rejected plan back
    for changes and approving what comes back would put the whole of the turned-down
    decomposition into force with the rejector and their reason erased. :func:`_revise` carries a
    rejection forward across a content edit for the same reason.

    **Out of ``APPROVED``, unless ``withdraw_approval`` is set.** A notebook may already have been
    scaffolded from the approved plan, and a decision does not bump the version, so the withdrawal
    replaces the approval on the only version there is. Quietly is therefore the one way it must
    not happen: asked for by name it is allowed, it is logged, and the approval it overturns is
    written into the note, so the plan itself still says an approval was given and taken away
    however the caller chooses to store it.
    """
    current = plan.approval
    if current.state is ApprovalState.REJECTED and state is not ApprovalState.REJECTED:
        because = f" (reason: {current.note})" if current.note else ""
        msg = (
            f"cannot {doing}: it was rejected {_attribution(current)}{because}. A rejection is "
            f"terminal — a rejected plan can never be approved — so propose a new one rather "
            f"than moving this one back into review."
        )
        raise PlanNotApprovableError(msg)

    withdrawing = current.state is ApprovalState.APPROVED and state is not ApprovalState.APPROVED
    if withdrawing and not withdraw_approval:
        msg = (
            f"cannot {doing}: it is approved {_attribution(current)}, and a notebook may already "
            f"have been scaffolded from it. Pass withdraw_approval=True to withdraw that approval "
            f"deliberately; the approval it overturns is recorded on the plan when you do."
        )
        raise PlanNotApprovableError(msg)
    if withdrawing:
        record = f"withdraws the approval given {_attribution(current)}"
        note = f"{note}; {record}" if note else record
        logger.warning(
            "plan v%d for %s: approval by %s withdrawn by %s",
            plan.version,
            plan.workbook,
            current.by,
            by,
        )

    return plan.model_copy(
        update={"approval": Approval(state=state, by=by, at=datetime.now(UTC), note=note)}
    )


def _attribution(approval: Approval) -> str:
    """Who took a decision and when, in the form ``by phil at 2026-07-24 10:00``.

    Degrades to whatever of that was actually recorded: a plan hand-edited on disk can carry a
    state with no name against it.
    """
    who = f"by {approval.by}" if approval.by else "by an unnamed reviewer"
    return f"{who} at {approval.at:%Y-%m-%d %H:%M}" if approval.at is not None else who


def approve(plan: ProcessPlan, *, by: str, note: str | None = None) -> ProcessPlan:
    """Approve the plan, unlocking the scaffolder.

    The version is *not* bumped: approval is a decision about this version, not a new one.

    Raises:
        PlanNotApprovableError: listing every outstanding blocker, chiefly unacknowledged drops.
            A rejected plan is blocked here too, and says to propose a new one.
    """
    blockers = plan.approval_blockers()
    if blockers:
        joined = "\n".join(f"  - {blocker}" for blocker in blockers)
        msg = f"this plan cannot be approved yet:\n{joined}"
        raise PlanNotApprovableError(msg)
    approved = _decide(
        plan, state=ApprovalState.APPROVED, by=by, note=note, doing="approve this plan"
    )
    logger.info("plan v%d for %s approved by %s", plan.version, plan.workbook, by)
    return approved


def request_changes(
    plan: ProcessPlan, *, by: str, note: str, withdraw_approval: bool = False
) -> ProcessPlan:
    """Mark the plan as needing changes, without editing it.

    Unlike a rejection this is not terminal — the plan can be revised and approved — which is
    precisely why it may not be used on a *rejected* plan: ``CHANGES_REQUESTED`` approves cleanly,
    so that route would launder a rejection into an approval.

    Args:
        plan: The plan to send back.
        by: Who is asking for changes.
        note: What needs to change. Required: "changes requested" with nothing said is not a
            review.
        withdraw_approval: Required to send an *approved* plan back, because doing so takes an
            approval away from a plan a notebook may already have been scaffolded from.

    Raises:
        PlanNotApprovableError: when the plan was rejected, or is approved and the withdrawal was
            not asked for.
    """
    return _decide(
        plan,
        state=ApprovalState.CHANGES_REQUESTED,
        by=by,
        note=note,
        doing="send this plan back for changes",
        withdraw_approval=withdraw_approval,
    )


def reject(
    plan: ProcessPlan, *, by: str, reason: str, withdraw_approval: bool = False
) -> ProcessPlan:
    """Reject the plan outright. A rejected plan can never be approved; propose a new one.

    Args:
        plan: The plan being turned down.
        by: Who is rejecting it.
        reason: Why. It travels with the plan, because "why was this turned down" outlives the
            version it was turned down at.
        withdraw_approval: Required to reject an *approved* plan. Rejecting one silently would
            take the approval away from a plan a notebook may already have been scaffolded from,
            and leave nothing anywhere saying it had ever been given.

    Raises:
        PlanNotApprovableError: when the plan is approved and the withdrawal was not asked for.
            Rejecting an already-rejected plan is allowed; it stays rejected.
    """
    rejected = _decide(
        plan,
        state=ApprovalState.REJECTED,
        by=by,
        note=reason,
        doing="reject this plan",
        withdraw_approval=withdraw_approval,
    )
    logger.info("plan v%d for %s rejected by %s: %s", plan.version, plan.workbook, by, reason)
    return rejected


# =============================================================================
# WARNINGS
# =============================================================================


def review_warnings(
    plan: ProcessPlan,
    analysis: WorkbookAnalysis | None = None,
    *,
    triage_result: TriageResult | None = None,
) -> list[str]:
    """Everything a reviewer should look at before approving.

    Warnings never block; :meth:`ProcessPlan.approval_blockers` does that. These are the things
    that are legal but suspicious, and the most important of them is an empty ``open_questions``
    on a workbook complex enough that the model should have had a question (PLAN 6.2).

    Args:
        plan: The plan under review.
        analysis: The analysis it was written against, used for the complexity check.
        triage_result: A pre-computed triage, saving a second complexity pass.
    """
    warnings: list[str] = []

    score = None
    if triage_result is not None:
        score = triage_result.complexity
    elif analysis is not None:
        score = complexity(analysis)
    if score is not None:
        empty_questions = plan.open_questions_warning(complexity=score)
        if empty_questions:
            warnings.append(empty_questions)

    warnings.extend(plan.ordering_warnings())

    for stage in plan.stages:
        unlisted = [
            upstream
            for upstream in stage.upstream_stage_ids
            if upstream not in stage.depends_on and upstream != stage.id
        ]
        if unlisted:
            warnings.append(
                f"stage {stage.id!r} reads {', '.join(unlisted)} but does not depend on "
                f"{'them' if len(unlisted) > 1 else 'it'} — the scaffolder emits in depends_on "
                f"order, so that frame may not exist yet when this stage runs"
            )

    low = plan.low_confidence_stages
    if low:
        warnings.append(
            f"{len(low)} stage(s) at low or unstated confidence: "
            f"{', '.join(stage.id for stage in low)} — these scaffold with a review marker"
        )
    unanswered = plan.unanswered_questions
    if unanswered:
        warnings.append(f"{len(unanswered)} open question(s) still unanswered")
    if plan.assessment.convertible < 0.6:
        warnings.append(
            f"triage scores only {plan.assessment.convertible:.0%} of the logic as convertible; "
            f"read assessment.blockers before approving"
        )
    if not plan.checkpoints and plan.assessment.convertible < 0.9:
        warnings.append(
            "no checkpoint stages on a plan that is not scored as fully convertible — is a "
            "judgement call being translated into code that was never really code?"
        )
    if analysis is not None:
        planned = {op for stage in plan.stages for op in stage.operations}
        if planned:
            known = {operation.id for operation in analysis.operations}
            unplanned = known - planned
            if unplanned:
                warnings.append(
                    f"{len(unplanned)} analysed operation(s) are claimed by no stage and appear "
                    f"in no drop"
                )
            invented = planned - known
            if invented:
                warnings.append(
                    f"stages reference {len(invented)} operation id(s) that are not in the "
                    f"analysis: {', '.join(sorted(invented)[:5])}"
                )
    return warnings


# =============================================================================
# RENDERING
# =============================================================================


def render_plan(
    plan: ProcessPlan,
    *,
    analysis: WorkbookAnalysis | None = None,
    triage_result: TriageResult | None = None,
    show_warnings: bool = True,
) -> str:
    """Render a plan as plain text for review.

    The layout follows the PLAN's own worked example: assessment, then stages in order, then
    open questions, then drops. Anything that blocks approval is stated at the bottom where a
    reader ends up, not buried at the top where they started.
    """
    lines: list[str] = [
        f"Process plan v{plan.version} for {plan.workbook}",
        # Stamped UTC, so say UTC. This is read months later beside an approval time, and an
        # unmarked timestamp is one the reader has to guess the zone of.
        f"  created {plan.created_at.astimezone(UTC):%Y-%m-%d %H:%M} UTC by {plan.generated_by}"
        + (f" ({plan.llm_model})" if plan.llm_model else "")
        + f"    approval: {plan.approval.state.value}",
    ]
    if plan.summary:
        lines.extend(["", _wrap(plan.summary, indent="  ")])

    # The briefing is rendered here, before anything technical, because approving a plan means
    # approving what the notebook will *tell people this process is for* -- and that text goes
    # into the notebook and outlives everyone. A reviewer who never saw it has signed off prose
    # they cannot vouch for, which is exactly the failure the `sources` requirement exists to
    # make visible: the citations are here so they can be checked against the workbook.
    briefing = plan.briefing
    if briefing is not None and not briefing.is_empty:
        lines.extend(["", "BRIEFING", "  what the notebook will say this process is for"])
        for label, value in (
            ("purpose", briefing.purpose),
            ("background", briefing.background),
            ("cadence", briefing.cadence),
            ("audience", briefing.audience),
        ):
            if value:
                lines.extend(["", f"  {label}:", _wrap(value, indent="    ")])
        if briefing.watch_for:
            lines.extend(["", "  watch for:"])
            lines.extend(_wrap(f"- {item}", indent="    ") for item in briefing.watch_for)
        lines.extend(["", "  sources:"])
        lines.extend(f"    {source}" for source in briefing.sources)
        lines.append(
            _wrap(
                "Check these against the workbook. Prose here that no source supports is "
                "invented, and it will read as authoritative for as long as the notebook lives.",
                indent="  ",
            )
        )

    lines.extend(
        [
            "",
            "ASSESSMENT",
            f"  convertible: {plan.assessment.convertible:.2f}",
        ]
    )
    if plan.assessment.rationale:
        lines.append(_wrap(plan.assessment.rationale, indent="  "))
    for blocker in plan.assessment.blockers:
        lines.append(_wrap(f"- {blocker}", indent="  "))
    if not plan.assessment.blockers:
        lines.append("  no blockers stated")

    lines.extend(["", f"STAGES ({len(plan.stages)})"])
    for index, stage in enumerate(plan.stages, start=1):
        lines.extend(_render_stage(index, stage))

    lines.extend(["", f"OPEN QUESTIONS ({len(plan.open_questions)})"])
    if not plan.open_questions:
        lines.append("  none — on a complex workbook that is itself worth questioning")
    for question in plan.open_questions:
        marker = "answered" if question.answered else "OPEN"
        lines.append(_wrap(f"[{marker}] {question.question}", indent="  "))
        if question.context:
            lines.append(_wrap(f"context: {question.context}", indent="      "))
        if question.answer:
            lines.append(_wrap(f"answer: {question.answer}", indent="      "))

    lines.extend(["", f"DROPPED ({len(plan.dropped)})"])
    if not plan.dropped:
        lines.append("  nothing dropped")
    for drop in plan.dropped:
        if not drop.acknowledged:
            state = "NOT ACKNOWLEDGED"
        elif drop.accepted:
            state = "confirmed"
        else:
            state = "REJECTED - must be kept"
        lines.append(_wrap(f"[{state}] {drop.range}: {drop.reason}", indent="  "))
        if drop.note:
            lines.append(_wrap(f"note: {drop.note}", indent="      "))

    if show_warnings:
        warnings = review_warnings(plan, analysis, triage_result=triage_result)
        if warnings:
            lines.extend(["", "REVIEW WARNINGS"])
            lines.extend(_wrap(f"- {warning}", indent="  ") for warning in warnings)

    blockers = plan.approval_blockers()
    lines.extend(["", "APPROVAL"])
    if plan.approval.approved:
        # Through :func:`_attribution` rather than formatted here, so one place decides how an
        # attribution reads. Unguarded, an approval carrying no timestamp took `kedge plan show`
        # and `kedge plan propose` down with a `TypeError` out of `__format__`, and one naming
        # nobody printed "approved by None". Neither is exotic: `kedge open --plan` adopts a plan
        # file the user wrote, and a hand-written approval block records whatever it records.
        lines.append(f"  approved {_attribution(plan.approval)}")
    elif blockers:
        lines.append("  cannot be approved yet:")
        lines.extend(_wrap(f"- {blocker}", indent="    ") for blocker in blockers)
    else:
        lines.append("  ready for approval; nothing is written to the notebook until it is given")
    return "\n".join(lines)


def _render_stage(index: int, stage: Stage) -> list[str]:
    head = f"  {index:>2}. {stage.id}  [{stage.kind.value}, confidence {stage.confidence.value}]"
    lines = [head, _wrap(stage.intent, indent="      ")]
    if stage.depends_on:
        lines.append(f"      after: {', '.join(stage.depends_on)}")
    if stage.sources:
        lines.append(_render_sources(stage))
    if stage.excel_pattern is not None:
        lines.append(f"      pattern: {stage.excel_pattern.value}")
    if stage.operations:
        shown = ", ".join(stage.operations[:6])
        more = f" (+{len(stage.operations) - 6} more)" if len(stage.operations) > 6 else ""
        lines.append(_wrap(f"operations: {shown}{more}", indent="      "))
    for assumption in stage.assumptions:
        lines.append(_wrap(f"assumes: {assumption}", indent="      "))
    if stage.is_checkpoint:
        checkpoint = stage.effective_checkpoint()
        lines.append(_wrap(f"asks: {checkpoint.question}", indent="      "))
        lines.append(f"      options: {', '.join(checkpoint.options)}")
    if stage.notes:
        lines.append(_wrap(f"note: {stage.notes}", indent="      "))
    return lines


def _render_sources(stage: Stage, *, indent: str = "      ") -> str:
    """The ``sources`` line, or one line per source where they will not fit on one.

    ``_wrap`` breaks prose at any space, and since 1.1 a rendered source *has* a space in it —
    ``power_query Ratings``. A wrapped list could therefore end a line at ``power_query`` and put
    the ref on the next, which reads as a bare origin: the reader sees a Power Query table with no
    name where the plan named one. One source per line costs a few rows and cannot say that.
    """
    rendered = [source.render() for source in stage.sources]
    one_line = _wrap(f"sources: {', '.join(rendered)}", indent=indent)
    if "\n" not in one_line:
        return one_line
    continuation = indent + " " * len("sources: ")
    return "\n".join(
        [f"{indent}sources: {rendered[0]}", *(continuation + item for item in rendered[1:])]
    )


def _wrap(text: str, *, indent: str, width: int = 96) -> str:
    """Wrap prose to a readable width at a fixed indent."""
    return textwrap.fill(text, width=width, initial_indent=indent, subsequent_indent=indent + "  ")


# =============================================================================
# DIFF
# =============================================================================


@dataclass(frozen=True, slots=True)
class StageChange:
    """One stage that differs between two plan versions."""

    stage_id: str
    fields: tuple[str, ...]
    before: dict[str, Any]
    after: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PlanDiff:
    """What changed between two versions of a plan.

    This is the change record for a process. When the workbook changes next quarter, this is
    what gets reviewed rather than the whole plan again (PLAN 2.2).
    """

    before_version: int
    after_version: int
    added_stages: tuple[str, ...] = ()
    removed_stages: tuple[str, ...] = ()
    reordered: bool = False
    changed_stages: tuple[StageChange, ...] = ()
    added_questions: tuple[str, ...] = ()
    answered_questions: tuple[str, ...] = ()
    removed_questions: tuple[str, ...] = ()
    added_drops: tuple[str, ...] = ()
    removed_drops: tuple[str, ...] = ()
    acknowledged_drops: tuple[str, ...] = ()
    convertible_delta: float = 0.0
    approval_changed: bool = False

    @property
    def is_empty(self) -> bool:
        """Whether the two versions are materially identical."""
        return not (
            self.added_stages
            or self.removed_stages
            or self.reordered
            or self.changed_stages
            or self.added_questions
            or self.answered_questions
            or self.removed_questions
            or self.added_drops
            or self.removed_drops
            or self.acknowledged_drops
            or abs(self.convertible_delta) > 1e-9
        )


_DIFFED_STAGE_FIELDS = (
    "intent",
    "kind",
    "sources",
    "depends_on",
    "confidence",
    "assumptions",
    "excel_pattern",
    "operations",
    "checkpoint",
    "notes",
)


def _diffable(stage: Stage) -> dict[str, Any]:
    """A stage as flat JSON, with its sources rendered rather than dumped.

    A diff is read, not parsed. ``sources: ['range Calc!H2:H500'] -> [...]`` is the line somebody
    reviewing a change wants; the mapping each source dumps to is the same information spelled at
    three times the width. Rendering loses nothing a diff needs, because two sources render the
    same only when their origin and ref are the same.
    """
    raw = stage.model_dump(mode="json")
    raw["sources"] = [source.render() for source in stage.sources]
    return raw


def diff_plans(before: ProcessPlan, after: ProcessPlan) -> PlanDiff:
    """Compare two plan versions field by field.

    Args:
        before: The earlier version.
        after: The later version.

    Returns:
        A structured diff. Render it with :func:`render_diff`.
    """
    before_ids = before.stage_ids
    after_ids = after.stage_ids
    added = [stage_id for stage_id in after_ids if stage_id not in before_ids]
    removed = [stage_id for stage_id in before_ids if stage_id not in after_ids]
    common_before = [stage_id for stage_id in before_ids if stage_id in after_ids]
    common_after = [stage_id for stage_id in after_ids if stage_id in before_ids]

    changes: list[StageChange] = []
    for stage_id in common_after:
        old = before.stage(stage_id)
        new = after.stage(stage_id)
        if old is None or new is None:
            continue
        old_raw = _diffable(old)
        new_raw = _diffable(new)
        differing = tuple(
            name for name in _DIFFED_STAGE_FIELDS if old_raw.get(name) != new_raw.get(name)
        )
        if differing:
            changes.append(
                StageChange(
                    stage_id=stage_id,
                    fields=differing,
                    before={name: old_raw.get(name) for name in differing},
                    after={name: new_raw.get(name) for name in differing},
                )
            )

    before_questions = {question.question: question for question in before.open_questions}
    after_questions = {question.question: question for question in after.open_questions}
    answered = [
        text
        for text, question in after_questions.items()
        if question.answered and not (before_questions.get(text) or question).answered
    ]

    before_drops = {drop.range: drop for drop in before.dropped}
    after_drops = {drop.range: drop for drop in after.dropped}
    acknowledged = [
        key
        for key, drop in after_drops.items()
        if drop.acknowledged and not (before_drops.get(key) or drop).acknowledged
    ]

    return PlanDiff(
        before_version=before.version,
        after_version=after.version,
        added_stages=tuple(added),
        removed_stages=tuple(removed),
        reordered=common_before != common_after,
        changed_stages=tuple(changes),
        added_questions=tuple(text for text in after_questions if text not in before_questions),
        answered_questions=tuple(answered),
        removed_questions=tuple(text for text in before_questions if text not in after_questions),
        added_drops=tuple(key for key in after_drops if key not in before_drops),
        removed_drops=tuple(key for key in before_drops if key not in after_drops),
        acknowledged_drops=tuple(acknowledged),
        convertible_delta=round(after.assessment.convertible - before.assessment.convertible, 4),
        approval_changed=before.approval.state is not after.approval.state,
    )


def render_diff(diff: PlanDiff) -> str:
    """Render a :class:`PlanDiff` as plain text."""
    lines = [f"Plan v{diff.before_version} -> v{diff.after_version}"]
    if diff.is_empty and not diff.approval_changed:
        lines.append("  no material changes")
        return "\n".join(lines)
    # `is_empty` deliberately ignores approval — the two versions really are materially
    # identical — but an approval is the single most important event in a plan's history, so
    # returning "no material changes" and saying nothing else would hide the only thing that
    # happened.

    for stage_id in diff.added_stages:
        lines.append(f"  + stage {stage_id}")
    for stage_id in diff.removed_stages:
        lines.append(f"  - stage {stage_id}")
    if diff.reordered:
        lines.append("  ~ stages reordered")
    for change in diff.changed_stages:
        lines.append(f"  ~ stage {change.stage_id}: {', '.join(change.fields)}")
        for name in change.fields:
            lines.append(f"      {name}: {change.before[name]!r} -> {change.after[name]!r}")
    for question in diff.added_questions:
        lines.append(_wrap(f"+ question: {question}", indent="  "))
    for question in diff.answered_questions:
        lines.append(_wrap(f"* answered: {question}", indent="  "))
    for question in diff.removed_questions:
        lines.append(_wrap(f"- question: {question}", indent="  "))
    for drop in diff.added_drops:
        lines.append(f"  + drop {drop}")
    for drop in diff.removed_drops:
        lines.append(f"  - drop {drop}")
    for drop in diff.acknowledged_drops:
        lines.append(f"  * acknowledged drop {drop}")
    if abs(diff.convertible_delta) > 1e-9:
        lines.append(f"  ~ convertible {diff.convertible_delta:+.2f}")
    if diff.approval_changed:
        lines.append("  ~ approval state changed")
    return "\n".join(lines)
