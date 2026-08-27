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

from kedge.analysis.formulas import parse_formula
from kedge.plan.model import (
    Approval,
    ApprovalState,
    Handoff,
    OpenQuestion,
    PlanError,
    ProcessPlan,
    SourceOrigin,
    Stage,
    StageSource,
)
from kedge.plan.triage import complexity
from kedge.sql import changes_data, reads_as_sql

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from kedge.analysis.model import LogicalOperation, ProcessNote, WorkbookAnalysis
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
    "repairable_warnings",
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
#
# The checks below the ordinary ones are about what :mod:`kedge.notebook.scaffold` will *do* with
# the plan, and three of those need a predicate the scaffolder owns: which hand-in a stage reads
# as its own, when a stage's upstream frame resolves to the notebook's head hand-in, and which
# stages define a token a later cell can be held behind.
#
# **They are reimplemented here rather than imported, deliberately.** CLAUDE.md's layering is
# ``analysis/ -> plan/ -> notebook/ -> agent/ -> server/``; the scaffolder imports this package,
# so importing it back inverts the dependency, and hiding that inside a function body to dodge
# the import cycle would make the inversion less visible rather than less real. It would also
# drag the whole scaffolder -- templates, house-rule checks, marimo code strings -- into a review
# pass that renders text. Note the contrast with :func:`kedge.analysis.formulas.parse_formula`
# below, which is public, sits *under* this package, and is therefore imported outright: the
# argument here is about direction, not about copying being preferable.
#
# The reimplementation is three lines and the risk is obvious: the scaffolder changes, these
# quietly start describing a notebook nobody builds any more. So the copies are not trusted to
# stay true -- ``tests/unit/test_plan_review.py`` asserts each one against the scaffolder's own
# function over a battery of stages that includes every kind, a hand-off dependency and a
# dependency the name map does not cover, and a change to either side fails there. That is the
# same mechanism ``test_agent_prompts.py`` uses to keep a hand-written prompt honest about
# :class:`~kedge.plan.model.StageKind`, and for the same reason: a copy of a rule is fine as long
# as something fails when it drifts.
#
# The other discipline these checks are held to is **silence**. Every one of them is rendered on
# the approval card as its own paragraph, so a check that fires on ordinary correct plans makes
# the card permanently amber and the whole list stops being read -- the argument CLAUDE.md makes
# about ``NOT RECONCILED``. Each is therefore run over the analyser's own output for every
# workbook in ``tests/fixtures`` with a plausible correct plan, and has to stay quiet.

_IN_THE_WORKBOOK: frozenset[str] = frozenset({"sheet", "cell_comment"})
"""The two :attr:`~kedge.analysis.model.ProcessNote.source` values that mean *this file*.

The other four -- ``docx``, ``doc_stub``, ``markdown``, ``plain_text`` -- are companion documents
the analyser found beside the workbook, and it attaches them by filename rather than by any
stated relationship: :attr:`~kedge.analysis.model.FindingKind.DOCUMENT_ATTACHED_BY_FILENAME`
exists precisely to say that the association is a guess. A README sitting in a shared drive is
attached to every workbook in it -- over ``tests/fixtures`` that is 21 notes on all eight
workbooks, including the seven with no documentation of their own -- so a warning built on those
notes fires on nearly every conversion. Only what the analyser read out of the workbook itself
decides whether a briefing was dropped.
"""

_MAX_NAMED = 3
"""How many things a warning names before it says "and N more".

Enough to act on, short enough that the card stays a list of sentences. The briefing warning is
what set it: eight cited notes rendered in full is a 900-character paragraph.
"""


def _statement_that_writes(operation: LogicalOperation) -> str | None:
    """The text a formula region concatenates, when that text reads as a statement that writes.

    The ``="UPDATE ... "&F17&"..."`` column that real workbooks are full of. The analyser cannot
    type it as anything better than ``text_manipulation`` -- it is string concatenation, and
    nothing in the workbook reads the result, so it is also reported as a dead region -- but its
    consumer is a person with a clipboard, and dropping it deletes the step that changes the data
    (``prompts/propose_vocabulary.md``).

    The literals come from :func:`kedge.analysis.formulas.parse_formula`, which tokenises with
    openpyxl rather than with a regular expression of this module's own; only ``string_literals``
    is read, so the holding cell's coordinates are irrelevant and passed as ``1, 1``.

    **Both questions about the text are asked in :mod:`kedge.sql` and neither is answered here**
    -- non-negotiable 3. :func:`~kedge.sql.reads_as_sql` says whether the concatenated prose is a
    statement at all, which matters because most of what a ``text_manipulation`` column builds is
    labels and instructions: ``="Delete row "&A1&" from the tracker"`` opens with a writing verb
    and is a sentence. :func:`~kedge.sql.changes_data` then says whether that statement writes,
    and the reason it owns that is a prefix match calling ``SELECT ... FOR UPDATE`` a write.

    ``reads_as_sql`` is a reading rather than a parse and says so; an English sentence opening
    ``Update`` and containing ``set`` still gets through. So the caller **quotes the text it
    found**: a reviewer reading ``"Update the tracker and set the flag"`` on the approval card
    sees what it is at a glance, where "a formula column that writes" would leave them hunting.

    Returns:
        The concatenated literal text, or ``None`` when there is none, it is not a statement, or
        it does not write.
    """
    formula = operation.sample_a1 or operation.r1c1 or ""
    if not formula:
        return None
    text = " ".join(parse_formula(formula, 1, 1).string_literals).strip()
    if not text or not reads_as_sql(text) or not changes_data(text):
        return None
    return text


def _stage_handin_label(stage: Stage) -> str | None:
    """The label of this stage's *own* hand-in, or ``None`` if it reads the notebook's.

    Mirrors ``scaffold._named_handin``: the ``ref`` is what separates "another file, arriving
    later" from "the file at the top of the notebook". Kept true by the tripwire test above.
    """
    for source in stage.sources:
        if source.origin is SourceOrigin.HANDIN and source.ref:
            return source.ref
    return None


def _reads_the_head_handin(stage: Stage) -> bool:
    """Whether this stage names the notebook's own hand-in as an input, on purpose."""
    return any(source.origin is SourceOrigin.HANDIN and not source.ref for source in stage.sources)


def _falls_through_to_the_head_handin(stage: Stage, frame_stage_ids: set[str]) -> bool:
    """Whether the scaffolder would build this stage on ``handin_frame`` by default.

    Mirrors ``scaffold._upstream_name``, which walks ``depends_on`` for the first dependency that
    is both in the name map and not a checkpoint -- a checkpoint's output is a decision record,
    not a frame -- and ``return "handin_frame"`` when it finds none.

    Args:
        stage: The stage whose upstream frame is in question.
        frame_stage_ids: Ids of the stages that produce a frame to build on, which is the
            scaffolder's ``dependency in names and dependency not in checkpoints`` expressed as a
            set. Passed in rather than derived from ``is_checkpoint`` here so the tripwire test
            can hand both sides a dependency the name map does not cover.
    """
    if _stage_handin_label(stage) is not None:
        return False
    return not any(dependency in frame_stage_ids for dependency in stage.depends_on)


def _frame_stage_ids(plan: ProcessPlan) -> set[str]:
    """The stages whose cell defines a frame something downstream can build on."""
    return {stage.id for stage in plan.stages if not stage.is_checkpoint}


def _transitive_depends_on(plan: ProcessPlan) -> dict[str, set[str]]:
    """Stage id to every stage that must run before it, following ``depends_on`` all the way.

    "Upstream" has to mean transitively or the check it feeds is trivially defeated: a checkpoint
    two steps above a production ``UPDATE`` is still an approval in front of it, and a plan that
    put one there would be told it had not.

    Iterative rather than recursive on purpose. A validated plan is acyclic and shallow, but
    :func:`kedge.server.routes._review_warnings` catches broadly and returns *no* warnings when
    anything here raises -- so a `RecursionError` on a pathological plan would not lose one
    warning, it would lose the whole card.
    """
    by_id = {stage.id: stage for stage in plan.stages}
    resolved: dict[str, set[str]] = {}
    for stage in plan.stages:
        upstream: set[str] = set()
        pending = list(stage.depends_on)
        while pending:
            current = pending.pop()
            if current in upstream:
                continue
            upstream.add(current)
            found = by_id.get(current)
            if found is not None:
                pending.extend(found.depends_on)
        upstream.discard(stage.id)
        resolved[stage.id] = upstream
    return resolved


def _plan_handoffs(plan: ProcessPlan) -> list[Handoff]:
    """Every hand-off the plan carries, whatever kind of stage it hangs off.

    ``kind: handoff`` is the ordinary case, but a ``load`` stage may carry one too -- "here is
    the query, and here is where you paste what it returns" is one step and two cells.
    """
    return [
        stage.effective_handoff()
        for stage in plan.stages
        if stage.is_handoff or stage.handoff is not None
    ]


def _confirming_handoffs(plan: ProcessPlan) -> list[Stage]:
    """The hand-off stages the notebook will ask somebody to confirm as having been run.

    **Whether a hand-off writes is asked of the statement**, through
    :attr:`~kedge.plan.model.Handoff.needs_confirmation` and so through
    :func:`kedge.sql.changes_data` -- non-negotiable 3, and CLAUDE.md's "``mutates`` is a claim;
    the statement is the fact". A hand-off that only *claims* to mutate is included as well,
    because the scaffolder emits a confirmation for that too and this list exists to describe
    what the scaffolder does.

    ``kind: handoff`` and nothing else, even though a ``load`` stage may carry a ``handoff``
    (:func:`_plan_handoffs`). ``scaffold.build_cells`` reaches ``_handoff_cells`` only on
    ``stage.is_handoff``, so a hand-off hanging off another kind scaffolds no confirmation cell
    and therefore defines no token -- and a warning telling somebody to depend on a token that
    is never defined is a warning whose repair changes nothing.
    """
    return [
        stage
        for stage in plan.stages
        if stage.is_handoff and stage.effective_handoff().needs_confirmation
    ]


def _gate_producing_stage_ids(plan: ProcessPlan) -> set[str]:
    """The stages whose cell defines a token a later cell can be held behind.

    Mirrors ``scaffold._gate_map``: a checkpoint's token is its decision record and a mutating
    hand-off's is its confirmation, and both mean the same thing to the cell that reads one --
    *a person has done something, and until they had, you were not to be shown*. Only the ids
    are wanted here; the names the tokens are built out of are the scaffolder's business.
    """
    return {stage.id for stage in plan.stages if stage.is_checkpoint} | {
        stage.id for stage in _confirming_handoffs(plan)
    }


def _gates_a_stage_waits_for(plan: ProcessPlan, producers: set[str]) -> dict[str, set[str]]:
    """Stage id to every gate its cells are held behind, following gate edges and nothing else.

    ``scaffold._gate_tokens`` is ``[gated[item] for item in stage.depends_on if item in gated]``
    -- the stage's **direct** dependencies -- and a gate cell in turn reads only its own. Chained,
    those edges are exactly what a hand-in selector waits for, because a selector reads nothing
    else: it constructs ``mo.ui`` elements, and a frame it never reads can gate nothing. So a
    second checkpoint or hand-off between an approval and a re-extract carries the protection
    across, and a plain transform in the same position does not.

    Iterative and in emission order, for the reason :func:`_transitive_depends_on` gives: a
    ``RecursionError`` here would cost the whole approval card rather than one warning.
    """
    reached: dict[str, set[str]] = {}
    for stage in plan.ordered_stages():
        found: set[str] = set()
        for dependency in stage.depends_on:
            if dependency in producers:
                found.add(dependency)
                found |= reached.get(dependency, set())
        reached[stage.id] = found
    return reached


def _transitive_dependents(
    plan: ProcessPlan, upstream: Mapping[str, set[str]]
) -> dict[str, set[str]]:
    """Stage id to every stage that runs after it: :func:`_transitive_depends_on` read backwards.

    Only ids the plan actually declares appear as keys, so a stage naming a dependency that does
    not exist contributes nothing here -- the same tolerance the forward walk shows.
    """
    dependents: dict[str, set[str]] = {stage.id: set() for stage in plan.stages}
    for stage in plan.stages:
        for ancestor in upstream[stage.id]:
            if ancestor in dependents:
                dependents[ancestor].add(stage.id)
    return dependents


def _shares_a_branch_with(
    stage_id: str,
    writer_id: str,
    upstream: Mapping[str, set[str]],
    dependents: Mapping[str, set[str]],
) -> bool:
    """Whether the plan relates this stage to that hand-off at all.

    The question :func:`_ungated_handin_warnings` could not answer without it, and the one that
    separates a re-extract from a file that has nothing to do with the statement. A re-extract is
    evidence **about** a statement, so a plan that means one says so somewhere: either the stage
    itself is built on something the hand-off is built on -- the reference plan's ``adjust``,
    which is both the arithmetic the ``UPDATE`` is rendered from and the prediction the re-extract
    is checked against -- or something downstream joins the two, which is what a comparison stage
    is. Either way the branches converge.

    Where they never converge the plan is describing two unrelated things, and the older rule --
    "no edge to the hand-off, therefore gate it on the hand-off" -- told it to invent one: a
    reference mapping file was told to wait for an ``UPDATE`` it is not evidence for, a runbook's
    *first* extract was told to hide behind an unrelated closing statement, and two independently
    correct update/re-extract pairs in one workbook were told to cross-wire, which
    :class:`~kedge.plan.model.PlanDraft` then refused as a cycle.

    Args:
        stage_id: The stage that takes a hand-in of its own.
        writer_id: The hand-off whose statement changes data.
        upstream: :func:`_transitive_depends_on` for this plan.
        dependents: :func:`_transitive_dependents` for this plan.
    """
    branch = upstream[writer_id] | {writer_id}
    return any(upstream[joining] & branch for joining in (stage_id, *dependents[stage_id]))


def _cite(note: ProcessNote) -> str:
    """One note, located: ``Sign-off!A3:A4 (Purpose)``, ``Calc!C1``.

    Only ever called for notes the analyser read out of the workbook, so ``origin`` is a sheet
    name and never a path. That matters: the companion-document origins are absolute paths on the
    machine the analysis ran on, and a warning is not the place for one.
    """
    where = f"{note.origin}!{note.location}" if note.location else note.origin
    return f"{where} ({note.heading})" if note.heading else where


def _excerpt(text: str, limit: int = 60) -> str:
    """One line of a statement, whitespace collapsed, for quoting inside a warning."""
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else f"{collapsed[:limit].rstrip()}..."


def _and_more(items: Sequence[str], limit: int = _MAX_NAMED) -> str:
    """Join up to ``limit`` names, then say how many were not named."""
    if len(items) <= limit:
        return ", ".join(items)
    return f"{', '.join(items[:limit])} and {len(items) - limit} more"


def _unhandled_write_warnings(plan: ProcessPlan, analysis: WorkbookAnalysis | None) -> list[str]:
    """The workbook records a statement that changes data, and no stage hands it over.

    A hand-off is the one stage kind that computes nothing: it holds a statement for the user to
    run somewhere kedge cannot reach. Where the statement *writes*, that stage is also the only
    thing that makes the rest of the runbook work --
    :attr:`~kedge.plan.model.Handoff.needs_confirmation` is answered from the statement, and the
    confirmation is what produces the token a re-extract is gated on. Typed ``output`` instead,
    the plan carries no statement, so there is no confirmation, no token, and a re-extract taken
    *before* the update looks exactly like one taken after.

    **Only writes count as evidence.** An earlier version fired on any connection with a
    ``command``, which is every workbook that reads a database: ``tests/fixtures/legacy_sql.xlsx``
    holds two ``SELECT``s and ``powerquery.xlsx`` three reference-data pulls, and a correct
    read-only conversion of either was told to add a hand-off. The justification was wrong there
    as well -- a ``SELECT`` hand-off has ``needs_confirmation`` false, so adding one would have
    changed nothing it promised.

    **And evidence is matched to hand-offs one item at a time**, not "does this plan have a
    hand-off anywhere". Asked the loose way, a plan that hands over the harmless extract query and
    silently drops the generated ``UPDATE`` column comes back clean -- which is one ``kind:``
    edit away from the plan this check exists to catch, and the natural repair to make after
    reading it.
    """
    if analysis is None:
        return []
    handoffs = _plan_handoffs(plan)
    hands_over_a_write = any(handoff.statement_writes for handoff in handoffs)
    named_connections = {
        handoff.connection.strip().casefold() for handoff in handoffs if handoff.connection
    }
    claimed = {
        operation_id
        for stage in plan.stages
        if stage.is_handoff or stage.handoff is not None
        for operation_id in stage.operations
    }

    unhandled = [
        f"connection {connection.name!r}, whose recorded command changes data "
        f"({_excerpt(connection.command or '')!r})"
        for connection in analysis.connections
        # A connection command is SQL because of where it was found -- `xl/connections.xml` holds
        # nothing else -- so it is asked only whether it writes. `reads_as_sql` is for the
        # formula regions below, where the text might be a sentence.
        if connection.command
        and changes_data(connection.command)
        and not hands_over_a_write
        and connection.name.strip().casefold() not in named_connections
    ]
    for operation in analysis.operations:
        if operation.id in claimed:
            continue
        statement = _statement_that_writes(operation)
        if statement is None:
            continue
        where = operation.ranges[0] if operation.ranges else operation.id
        unhandled.append(f"{where}, a formula column building {_excerpt(statement)!r}")

    if not unhandled:
        return []
    return [
        f"Type the step that writes as `kind: handoff`, naming the statement it hands over: "
        f"{_and_more(unhandled)} — nothing in the plan claims it. Without a `Handoff` there is no "
        f"statement for the notebook to show, no confirmation that it was run, and no token for "
        f"the step after it to be gated on"
    ]


def _unapproved_write_warnings(plan: ProcessPlan) -> list[str]:
    """A production write with nobody asked first.

    Whether the text writes is :func:`kedge.sql.changes_data`, reached through
    :attr:`~kedge.plan.model.Handoff.statement_writes` -- non-negotiable 3, and the reason it
    belongs there is that a prefix match calls ``SELECT ... FOR UPDATE`` a write. A declared
    ``mutates`` counts too: the claim is enough to want an approval in front of it even where the
    text does not parse as one.
    """
    upstream = _transitive_depends_on(plan)
    checkpoint_ids = {stage.id for stage in plan.stages if stage.is_checkpoint}
    warnings: list[str] = []
    for stage in plan.stages:
        if stage.handoff is None and not stage.is_handoff:
            continue
        if not stage.effective_handoff().needs_confirmation:
            continue
        if upstream[stage.id] & checkpoint_ids:
            continue
        elsewhere = [
            f"{other!r}" for other in sorted(checkpoint_ids - upstream[stage.id] - {stage.id})
        ]
        where = (
            f"the plan's checkpoint(s) — {_and_more(elsewhere)} — are downstream of it, and a "
            f"checkpoint below a write approves something already done"
            if elsewhere
            else "the plan has no checkpoint at all"
        )
        warnings.append(
            f"Add a `kind: checkpoint` stage above {stage.id!r}: it hands over a statement that "
            f"changes data and nothing upstream of it records a decision to go ahead — {where}"
        )
    return warnings


def _dropped_briefing_warning(plan: ProcessPlan, analysis: WorkbookAnalysis | None) -> list[str]:
    """The workbook explained itself and the plan threw it away.

    :class:`~kedge.plan.model.Briefing` refuses prose that cites nothing, because invented
    background in a finance notebook is confident, plausible and unattributable. Nothing refused
    a briefing that never arrived -- and the notebook then tells its reader, in as many words,
    that the workbook carried no description of what the process is for. On a workbook with a
    Purpose, a Background, a Scope and a Known issues section that is a confident falsehood in
    the one register the project exists to protect, which makes it the worse half of the
    asymmetry rather than the safer one.

    Two things keep it quiet where it should be. Only notes read out of the workbook itself count
    (:data:`_IN_THE_WORKBOOK`), because the companion documents are attached by a filename guess
    and their origins are absolute paths on somebody's machine. And a briefing carrying
    ``sources`` is never reported, empty or not: that is the honest blank
    ``_prose_must_be_attributable`` tells an author to write when the workbook explains nothing,
    and a check with no way to be satisfied is a check people learn to scroll past.
    """
    if analysis is None:
        return []
    recovered = [note for note in analysis.notes if note.source in _IN_THE_WORKBOOK]
    if not recovered:
        return []
    briefing = plan.briefing
    if briefing is not None and (not briefing.is_empty or briefing.sources):
        return []
    citations = [_cite(note) for note in recovered]
    said = "carries no briefing" if briefing is None else "carries a briefing that says nothing"
    return [
        f"Fill `briefing` from the workbook's own words, citing them: the plan {said} while the "
        f"analyser recovered {len(recovered)} note(s) from its sheets and comments — "
        f"{_and_more(citations)}. Left empty, the notebook tells whoever opens it that this "
        f"workbook carried no description of what the process is for"
    ]


def _stranded_handin_warnings(plan: ProcessPlan) -> list[str]:
    """A hand-in declared on a checkpoint: it arrives now, but nothing computes on it.

    This used to be the sharper finding of the two. ``build_cells`` ``continue``d past a checkpoint
    before it looked for a hand-in source, so the file had *nowhere to arrive* -- no selector, no
    receipt, no frame -- and a runbook stopped dead at the step meant to prove the update had
    worked. The scaffolder now emits the receiver cells for a checkpoint like any other stage, and
    ``test_the_scaffolder_emits_hand_in_cells_for_every_stage_kind`` is what keeps that true.

    **The wording follows the consequence down rather than keeping the louder claim.** What is
    left is real but smaller: a checkpoint records a decision and generates no code, and
    ``scaffold._upstream_name`` never treats a checkpoint as the frame a stage builds on, so
    nothing is scaffolded to *read* the file the checkpoint now receives -- the comparison a
    re-extract exists for has no cell of its own. Saying "that file has nowhere to arrive" about a
    notebook that visibly asks for it would be the worse error of the two: an approval card is
    read exactly as long as everything on it is true.
    """
    warnings: list[str] = []
    for stage in plan.stages:
        if not stage.is_checkpoint:
            continue
        label = _stage_handin_label(stage)
        if label is None:
            continue
        warnings.append(
            f"Move the hand-in ({label!r}) off {stage.id!r} and onto the stage that reads the "
            f"file: a `kind: checkpoint` stage does get its own selector, receipt and frame, so "
            f"{label!r} arrives -- but a checkpoint records a decision and computes nothing, and "
            f"no stage builds on a checkpoint's frame, so nothing is scaffolded to compare that "
            f"file against what this process predicted. Declared on the `kind: load` or "
            f"`kind: transform` stage that reads it, with {stage.id!r} left to record the "
            f"decision, the comparison has a cell to live in"
        )
    return warnings


def _checkpoint_as_a_frame_warnings(plan: ProcessPlan) -> list[str]:
    """A stage that says it reads a checkpoint. A checkpoint yields a decision, not a frame.

    ``_upstream_name`` walks ``depends_on`` for the first dependency that produces a frame and
    falls through to ``handin_frame`` when it finds none, so a stage whose only upstream is the
    checkpoint approving it is built on the notebook's own hand-in rather than on the result it
    names -- while its ``sources`` say, in the plan the user approved, that it reads the
    checkpoint.

    **This asks about the stage's own declaration and nothing wider.** An earlier version fired
    whenever the fall-through happened at all, which is a plan-level question asked per stage: a
    minimal ``load -> checkpoint -> compute`` plan reaches ``handin_frame`` because that genuinely
    is the frame it wants, and where a ``load`` stage already declares the hand-in, the head cells
    are emitted whatever this stage does. Both were told the head hand-in "blocks the page until a
    file no step of this process asks for is supplied", which was false in both. What survives is
    the part that is true wherever it fires: naming a checkpoint as a source claims a frame that
    stage cannot produce.

    A stage that reads something it does not depend on is left to ``ordering_warnings`` -- one
    defect should not cost the reviewer two paragraphs asking for the same edit.
    """
    checkpoint_ids = {stage.id for stage in plan.stages if stage.is_checkpoint}
    if not checkpoint_ids:
        return []
    frames = _frame_stage_ids(plan)
    warnings: list[str] = []
    for stage in plan.stages:
        if stage.generates_no_code or _reads_the_head_handin(stage):
            continue
        named = [
            source.ref
            for source in stage.sources
            if source.origin is SourceOrigin.STAGE and source.ref in checkpoint_ids
        ]
        if not named:
            continue
        if not _falls_through_to_the_head_handin(stage, frames):
            continue
        if any(read not in stage.depends_on for read in stage.upstream_stage_ids):
            continue
        cited = _and_more([f"{item!r}" for item in named])
        warnings.append(
            f"Name the stage whose result {stage.id!r} reads in its `sources` and `depends_on`: "
            f"it currently names {cited}, which is a checkpoint, and a checkpoint records a "
            f"decision rather than producing a frame — so the scaffolder has nothing to build "
            f"this stage on and falls back to the notebook's own hand-in"
        )
    return warnings


def _ungated_handin_warnings(plan: ProcessPlan) -> list[str]:
    """A re-extract with no dataflow edge to the statement it is evidence for.

    Observed on all three plans a live sweep proposed for the adjustment workbook, every one of
    them losing the same eval item for the same reason: the stage receiving the post-adjustment
    extract was emitted below the hand-off that runs the ``UPDATE`` and read none of its
    confirmation tokens. **The scaffolder is not at fault.** ``_gate_map`` produces a token for a
    mutating hand-off and ``_handin_cells`` gates the selector on it; ``_gate_tokens`` reads
    ``depends_on`` *directly*, and the plans declared no dependency there to read.

    What that costs is the failure CLAUDE.md describes. A cell that only constructs ``mo.ui``
    elements has no dataflow edges, so nothing can hide it, and the box for the re-extract is on
    screen from the moment the notebook opens. A grid pasted into it before the ``UPDATE`` ran
    looks exactly like one pasted after, and no later step can tell them apart -- which is why
    this is worth a paragraph on the card rather than a tidier plan.

    **Four things keep it quiet, in the order the loop asks them, and the last one is the whole
    difficulty.**

    A checkpoint's own hand-in belongs to :func:`_stranded_handin_warnings` alone. That warning's
    repair is to move the file *off* the stage and this one's is to add a dependency *to* it, and
    one plan defect must never produce two repair instructions pulling opposite ways -- least of
    all through :func:`repairable_warnings`, which puts them to a model told every finding is a
    defect it may not decline. The guard that used to say this was ``stage.kind in
    _HANDIN_KINDS_THE_SCAFFOLDER_IGNORES``, written one commit after that set was emptied, so it
    was never once true and the double report happened on every plan that could produce it.

    A stage already held behind **some** writing hand-off is left alone, whichever one it is.
    The harm named below is a file box on screen from the moment the notebook opens; a stage
    waiting on any confirmation at all does not have it, and which statement a re-extract is
    evidence for is a judgement the plan has made that this check is in no position to overrule.
    A chain of gates counts, because each gate cell reads the token before it and defines its own
    (:func:`_gates_a_stage_waits_for`).

    A hand-in the plan puts *before* the statement is not a re-extract: where the hand-off depends
    on the stage, however indirectly, the file demonstrably arrives first, which is the reference
    plan's ``pre_adjustment`` -- it feeds the arithmetic the ``UPDATE`` is rendered from.

    And the plan has to relate the two at all (:func:`_shares_a_branch_with`). Without that this
    fired on correct plans, because absent an edge the rule read "no relation stated, therefore
    gate it" -- the wrong default in a plan language where most pairs of stages have no relation
    to state. A rule cannot be both ordering-free and silent on unrelated files without asking
    what actually ties a re-extract to a statement, and that is what convergence answers.
    """
    writers = _confirming_handoffs(plan)
    if not writers:
        return []
    writer_ids = {writer.id for writer in writers}
    reached = _gates_a_stage_waits_for(plan, _gate_producing_stage_ids(plan))
    upstream = _transitive_depends_on(plan)
    dependents = _transitive_dependents(plan, upstream)
    warnings: list[str] = []
    for stage in plan.stages:
        if stage.is_checkpoint:
            continue
        label = _stage_handin_label(stage)
        if label is None:
            continue
        if reached[stage.id] & writer_ids:
            continue
        missed = [
            writer.id
            for writer in writers
            # A hand-off that receives a file of its own is excluded, because the remedy would be
            # a self-dependency and the plan would not validate with one.
            if writer.id != stage.id
            and stage.id not in upstream[writer.id]
            and _shares_a_branch_with(stage.id, writer.id, upstream, dependents)
        ]
        if not missed:
            continue
        named = _and_more([f"{item!r}" for item in missed])
        warnings.append(
            f"Add {named} to `depends_on` on {stage.id!r}: it takes a hand-in of its own "
            f"({label!r}) and waits on no statement confirmation at all, so its file box renders "
            f"from the moment the notebook opens. A hand-in selector builds `mo.ui` elements and "
            f"reads nothing else, so a confirmation token is the only thing that can hide it — "
            f"read where this stage names the hand-off in `depends_on`, or names a checkpoint or "
            f"hand-off that waits for it in turn. Ungated, a re-extract taken before the "
            f"statement ran arrives looking exactly like one taken after, and nothing afterwards "
            f"can tell them apart"
        )
    return warnings


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

    **Every warning names what it found.** This list is rendered on the approval card the user
    reads before clicking Approve, and a card that says "something may be wrong" is a card people
    stop reading -- the argument CLAUDE.md already makes about a permanently amber ``NOT
    RECONCILED``. So a warning quotes the connection, the range, the stage or the cells it is
    about, or it does not go in.

    The checks split in two. Some are about the plan on its own terms -- ordering, confidence,
    unanswered questions -- and some are about what the scaffolder will build from it: a plan can
    be entirely valid and still scaffold a notebook with an input that has nowhere to arrive, a
    production statement nobody was asked to approve, or a briefing that tells its reader the
    workbook explained nothing. Those are the expensive ones, because nothing downstream reports
    them: the plan validates, the notebook scaffolds, and it runs.

    Args:
        plan: The plan under review.
        analysis: The analysis it was written against. Everything that compares the plan against
            the workbook's own facts is skipped without it; nothing raises.
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
        if stage.is_handoff and stage.effective_handoff().contradicts_its_own_statement:
            # Nothing here blocks, and this one does not need to: `Handoff.needs_confirmation`
            # answers from the statement, so the notebook gates on a confirmation whatever the
            # flag says. What the flag still costs is truthfulness -- the approval card renders
            # it, and a reviewer told a production UPDATE changes nothing is being misled about
            # the one stage of the plan that touches live data.
            warnings.append(
                f"stage {stage.id!r} hands over a statement that writes but declares "
                f"`mutates: false` — the notebook will require a confirmation that it was run "
                f"regardless, but the plan says the opposite of what the statement does"
            )
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

    warnings.extend(repairable_warnings(plan, analysis))
    return warnings


def repairable_warnings(plan: ProcessPlan, analysis: WorkbookAnalysis | None = None) -> list[str]:
    """The subset a model can be asked to fix, and be expected to.

    Everything :func:`review_warnings` raises is worth a reviewer's attention. Only some of it is
    worth putting back to the model, and the line between the two is not severity -- it is
    whether the warning names a **defect with a fix in the plan's own fields**.

    These do. Each one names the field to change (``kind: handoff``, ``briefing``, a stage's
    ``sources`` or its ``depends_on``), the thing in the workbook it is about (a range, a
    connection, the sheets a note came from, the file a stage waits for), and what the notebook
    loses without it. They are already written in the
    imperative, so they need no rewriting to become a repair instruction. And every one describes
    a plan that *validates perfectly* and scaffolds a notebook that opens, runs, and is wrong --
    which is why nothing downstream catches them.

    The rest are deliberately excluded, because a model must not be asked to make them go away:

    - **Open questions outstanding.** A plan that asks the user four things is doing its job. Ask
      a model to clear that warning and it deletes the questions.
    - **An empty ``open_questions`` on a complex workbook.** The remedy is to have understood the
      workbook, not to invent a question to satisfy a check.
    - **Low confidence, operations claimed by no stage, convertibility.** Judgements and coverage
      facts. A model told to clear them raises its own confidence and claims the operations.

    Every one of those is a warning whose *only* honest audience is a person, and the failure mode
    of automating them is a plan that looks better and is not.

    Args:
        plan: The plan under review.
        analysis: The analysis it was written against. The two warnings that compare the plan
            against the workbook -- the unhandled write and the dropped briefing -- need it and
            are skipped without it.

    Returns:
        The repairable warnings, in the order :func:`review_warnings` renders them.
    """
    # Each of these was observed on one real conversion, which produced a notebook that opened,
    # ran, and was wrong.
    warnings: list[str] = []
    warnings.extend(_unhandled_write_warnings(plan, analysis))
    warnings.extend(_unapproved_write_warnings(plan))
    warnings.extend(_dropped_briefing_warning(plan, analysis))
    warnings.extend(_stranded_handin_warnings(plan))
    warnings.extend(_checkpoint_as_a_frame_warnings(plan))
    warnings.extend(_ungated_handin_warnings(plan))
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
