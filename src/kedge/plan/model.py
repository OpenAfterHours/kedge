"""The :class:`ProcessPlan` — the reviewable artifact between the facts and the code.

The analyser decides what a workbook *contains*; this module holds what the model proposes to
*do about it*, in a form a human can read in a couple of minutes and correct while correcting is
still cheap. Reviewing a twelve-line plan is minutes; reviewing forty generated cells to work
out that the decomposition was wrong is an afternoon (PLAN 2.2).

Three properties of the schema are load-bearing and are the reason it looks looser than a schema
usually would:

- **Stages are free-form in count and naming.** There is no template and no enum of allowed
  stage ids. A workbook that wants four stages gets four; one that wants fifteen gets fifteen.
  The only controlled vocabulary is :class:`StageKind`, and it exists solely because the
  scaffolder has to branch on it (PLAN 2.2, 6.2).
- **``open_questions`` is structurally required.** Not optional-with-default: the model must
  actively decide the list is empty rather than omitting the field and never considering it. An
  empty one on a complex workbook is itself suspicious, which
  :meth:`ProcessPlan.open_questions_warning` exists to say out loud.
- **``dropped`` requires acknowledgement.** Removing dead columns is a genuine improvement, but
  only if it is declared and someone signs it off. Acknowledgement is state on the plan, not an
  out-of-band flag, so a plan loaded from disk carries its own approvability with it.

Plans are frozen. Every edit in :mod:`kedge.plan.review` produces a new version rather than
mutating in place, which is what makes "the diff of the plan is the change record" true.

References:
- PLAN.md 2.1 (facts vs plan vs code), 2.2 (the plan artifact), 2.7 (translation vocabulary),
  6.2 (over-fitting risk), M2.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kedge.analysis.model import SCHEMA_VERSION as ANALYSIS_SCHEMA_VERSION
from kedge.analysis.model import ExcelPattern, WorkbookAnalysis
from kedge.errors import KedgeError

PLAN_SCHEMA_VERSION = "1.0"

__all__ = [
    "ANALYSIS_SCHEMA_VERSION",
    "PLAN_SCHEMA_VERSION",
    "Approval",
    "ApprovalState",
    "Assessment",
    "Checkpoint",
    "Confidence",
    "DroppedRange",
    "OpenQuestion",
    "PlanDraft",
    "PlanError",
    "ProcessPlan",
    "Stage",
    "StageKind",
    "topological_stages",
]


class PlanError(KedgeError):
    """The base error for the planning loop: triage, proposal, review, storage."""


# =============================================================================
# ENUMS
# =============================================================================


class Confidence(StrEnum):
    """How sure the plan is about a stage.

    ``NOT_APPLICABLE`` and ``UNKNOWN`` are deliberately different answers. ``n/a`` is a
    positive statement — a checkpoint is a human judgement and confidence is meaningless for it.
    ``unknown`` is the absence of a statement, and the scaffolder treats it exactly like ``low``:
    the cell gets a review marker rather than silently looking as finished as the rest.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NOT_APPLICABLE = "n/a"
    UNKNOWN = "unknown"


class StageKind(StrEnum):
    """The only controlled vocabulary in the stage schema.

    Kept to four members because each one changes what the scaffolder emits, and nothing else
    does. ``checkpoint`` is the flexibility escape hatch: a stage that is deliberately *not*
    automated — a judgement call, an override agreed with another team, a sanity check — which
    scaffolds to an ``mo.ui`` approval cell that blocks everything downstream until a decision
    and a note are recorded (PLAN 2.2).
    """

    LOAD = "load"
    TRANSFORM = "transform"
    OUTPUT = "output"
    CHECKPOINT = "checkpoint"


class ApprovalState(StrEnum):
    """Where the plan sits in review. Nothing reaches the notebook before ``APPROVED``."""

    DRAFT = "draft"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED = "approved"
    REJECTED = "rejected"


# =============================================================================
# BASE
# =============================================================================


class _PlanModel(BaseModel):
    """Base for every model here: immutable, and rejects unknown keys on load.

    ``extra="forbid"`` earns its place twice over: it turns a typo in a hand-edited YAML plan
    into a named error, and it turns a hallucinated field in an LLM response into a validation
    message that :mod:`kedge.plan.propose` can feed straight back as a repair instruction.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


# =============================================================================
# STAGE
# =============================================================================


class Checkpoint(_PlanModel):
    """What a checkpoint stage asks the human, and what answers it accepts.

    Example:
        >>> Checkpoint(question="Have this month's overrides been agreed with Risk?").options
        ['approve', 'reject']
    """

    question: str = Field(description="The decision the user is being asked to record.")
    options: list[str] = Field(
        default_factory=lambda: ["approve", "reject"],
        description="Accepted decisions. The first is the one that unblocks downstream cells.",
    )
    guidance: str | None = Field(
        default=None, description="What the reviewer should look at before deciding."
    )
    require_note: bool = Field(
        default=True,
        description="Whether a free-text reason is mandatory. On by default: the note is the "
        "whole improvement over someone typing a number into Excel with no record of why.",
    )

    @field_validator("options")
    @classmethod
    def _at_least_one_option(cls, value: list[str]) -> list[str]:
        cleaned = [option.strip() for option in value if option.strip()]
        if not cleaned:
            msg = "a checkpoint needs at least one decision option"
            raise ValueError(msg)
        return cleaned


class Stage(_PlanModel):
    """One step of the process, as the plan proposes it.

    A stage is a unit of *intent*, not a unit of code. It may collapse forty formula columns
    into one join, or split one sprawling region into three readable steps; that judgement is
    exactly what the plan exists to expose for review before any code is written.

    Example:
        >>> Stage(id="apply_haircuts", intent="Collateral haircut lookup by asset class").kind
        <StageKind.TRANSFORM: 'transform'>
    """

    id: str = Field(
        description="Free-form slug, unique within the plan. Becomes the notebook cell name."
    )
    intent: str = Field(description="What this step is for, in the business's own terms.")
    kind: StageKind = StageKind.TRANSFORM

    sources: list[str] = Field(
        default_factory=list,
        description="Where the inputs come from: sheet-qualified ranges, 'handin', or the id "
        "of an upstream stage.",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="Ids of stages that must run first. Stage order alone cannot express a DAG "
        "and the scaffolder needs one.",
    )

    confidence: Confidence = Confidence.UNKNOWN
    assumptions: list[str] = Field(
        default_factory=list,
        description="What the translation takes for granted: 'header on row 1', 'one row per "
        "counterparty'. These are what a reviewer checks first.",
    )
    excel_pattern: ExcelPattern | None = Field(
        default=None,
        description="The PLAN 2.7 pattern this stage translates, where one applies. "
        "`unknown` is a legitimate answer and should come with an open question.",
    )
    operations: list[str] = Field(
        default_factory=list,
        description="Ids of the analysis LogicalOperations this stage implements. The link back "
        "to the facts, and what reconciliation later compares against.",
    )
    checkpoint: Checkpoint | None = Field(
        default=None,
        description="Required in spirit for `kind: checkpoint`; synthesised if absent.",
    )
    notes: str | None = None

    # ── normalisation ────────────────────────────────────────────────────

    @model_validator(mode="before")
    @classmethod
    def _default_checkpoint_confidence(cls, value: Any) -> Any:
        """A checkpoint with no stated confidence means 'n/a', not 'unknown'."""
        checkpoint = isinstance(value, dict) and value.get("kind") == StageKind.CHECKPOINT.value
        if checkpoint and not value.get("confidence"):
            return {**value, "confidence": Confidence.NOT_APPLICABLE.value}
        return value

    @field_validator("id")
    @classmethod
    def _usable_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            msg = "stage id must not be blank"
            raise ValueError(msg)
        if not any(character.isalnum() for character in cleaned):
            msg = f"stage id {value!r} has no alphanumeric characters to build a cell name from"
            raise ValueError(msg)
        if len(cleaned) > 64:
            msg = f"stage id {value!r} is longer than 64 characters"
            raise ValueError(msg)
        return cleaned

    @field_validator("intent")
    @classmethod
    def _non_empty_intent(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            msg = "every stage needs an intent; 'I do not know what this does' belongs in open_questions"
            raise ValueError(msg)
        return cleaned

    @field_validator("excel_pattern", mode="before")
    @classmethod
    def _coerce_pattern(cls, value: Any) -> Any:
        """Accept the vocabulary case-insensitively; map anything unrecognised to `unknown`.

        The PLAN's own worked example writes ``VLOOKUP_exact``. Failing validation on a case
        difference would burn a retry for nothing, whereas an unrecognised pattern name is
        genuinely information: it becomes ``unknown``, which is the signal to raise an open
        question rather than improvise.
        """
        if value is None or isinstance(value, ExcelPattern):
            return value
        if not isinstance(value, str):
            return value
        normalised = value.strip().lower().replace("-", "_").replace(" ", "_")
        if normalised in {"", "none", "null", "n/a", "na"}:
            return None
        try:
            return ExcelPattern(normalised)
        except ValueError:
            return ExcelPattern.UNKNOWN

    # ── accessors ────────────────────────────────────────────────────────

    @property
    def is_checkpoint(self) -> bool:
        """Whether this stage is a deliberate human decision rather than code."""
        return self.kind is StageKind.CHECKPOINT

    @property
    def needs_review_marker(self) -> bool:
        """Whether the scaffolded cell should carry an explicit low-confidence marker.

        ``unknown`` counts. A stage whose confidence was never stated must not scaffold looking
        as finished as one the model was sure about.
        """
        if self.is_checkpoint:
            return False
        return self.confidence in (Confidence.LOW, Confidence.UNKNOWN)

    def effective_checkpoint(self) -> Checkpoint:
        """Return this stage's checkpoint spec, synthesising one from the intent if absent."""
        if self.checkpoint is not None:
            return self.checkpoint
        return Checkpoint(question=f"Approve: {self.intent}")


# =============================================================================
# QUESTIONS, DROPS, ASSESSMENT, APPROVAL
# =============================================================================


class OpenQuestion(_PlanModel):
    """Something the model does not know, said out loud instead of guessed at.

    Modelled as an object rather than a bare string because review has to *answer* these, and an
    answer that lives next to its question survives a round trip through YAML. A plain string is
    still accepted on load, since that is what the PLAN's worked example and most model output
    will produce.

    Example:
        >>> OpenQuestion.model_validate("Column AF is computed but never referenced.").answered
        False
    """

    question: str
    context: str | None = Field(default=None, description="Where in the workbook it came from.")
    answer: str | None = None
    answered_at: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def _accept_bare_string(cls, value: Any) -> Any:
        return {"question": value} if isinstance(value, str) else value

    @field_validator("question")
    @classmethod
    def _non_empty_question(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            msg = "an open question must not be blank"
            raise ValueError(msg)
        return cleaned

    @property
    def answered(self) -> bool:
        """Whether a human has answered this."""
        return bool(self.answer and self.answer.strip())


class DroppedRange(_PlanModel):
    """A range the plan proposes not to translate, and the user's decision about it.

    Silent removal is indistinguishable from a bug, so a drop carries a reason and stays
    unacknowledged until someone says otherwise. ``accepted`` separates the two ways of
    acknowledging: confirming the drop, or rejecting it and keeping the range.

    Example:
        >>> DroppedRange(range="Calc!AK:AP", reason="no downstream refs").acknowledged
        False
    """

    range: str = Field(description="Sheet-qualified range, e.g. 'Calc!AK:AP'.")
    reason: str = Field(description="Why it is safe to drop. 'Unused' alone is not a reason.")
    acknowledged: bool = Field(
        default=False, description="Set only by an explicit user decision in review."
    )
    accepted: bool = Field(
        default=True,
        description="True when the user confirmed the drop, False when they rejected it and the "
        "range must be kept. Only meaningful once acknowledged.",
    )
    note: str | None = None
    acknowledged_at: datetime | None = None

    @field_validator("range", "reason")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            msg = "a dropped range needs both a range and a reason"
            raise ValueError(msg)
        return cleaned

    @property
    def rejected(self) -> bool:
        """Whether the user acknowledged the drop by refusing it."""
        return self.acknowledged and not self.accepted


class Assessment(_PlanModel):
    """How much of the workbook the plan believes it can translate, and what stands in the way.

    An honest ``convertible: 0.4`` with a list of blockers is far more useful than a notebook
    that looks complete and is not (PLAN 2.2).
    """

    convertible: float = Field(
        ge=0.0,
        le=1.0,
        description="Proportion of the logic the plan believes it can translate faithfully.",
    )
    blockers: list[str] = Field(
        default_factory=list, description="Plain-English reasons the figure is not 1.0."
    )
    rationale: str | None = None


class Approval(_PlanModel):
    """The review gate. The scaffolder checks this and refuses without it."""

    state: ApprovalState = ApprovalState.DRAFT
    by: str | None = None
    at: datetime | None = None
    note: str | None = None

    @property
    def approved(self) -> bool:
        """Whether this plan may be written to a notebook."""
        return self.state is ApprovalState.APPROVED


# =============================================================================
# STAGE GRAPH
# =============================================================================


def _check_stage_graph(stages: list[Stage]) -> None:
    """Validate stage ids, dependency references, and acyclicity.

    Raises:
        ValueError: with a message naming the offending stage, so a failed LLM proposal can be
            retried with the error text as the repair instruction.
    """
    if not stages:
        msg = "a plan needs at least one stage"
        raise ValueError(msg)

    seen: set[str] = set()
    for stage in stages:
        if stage.id in seen:
            msg = f"duplicate stage id {stage.id!r}; stage ids must be unique within a plan"
            raise ValueError(msg)
        seen.add(stage.id)

    for stage in stages:
        for dependency in stage.depends_on:
            if dependency == stage.id:
                msg = f"stage {stage.id!r} depends on itself"
                raise ValueError(msg)
            if dependency not in seen:
                msg = f"stage {stage.id!r} depends on unknown stage {dependency!r}"
                raise ValueError(msg)

    ordered = topological_stages(stages, strict=False)
    if len(ordered) != len(stages):
        remaining = sorted({stage.id for stage in stages} - {stage.id for stage in ordered})
        msg = f"depends_on forms a cycle involving: {', '.join(remaining)}"
        raise ValueError(msg)


def topological_stages(stages: list[Stage], *, strict: bool = True) -> list[Stage]:
    """Return the stages in dependency order, keeping plan order as the tie-break.

    Stage order is cell order, and the plan's own order is respected wherever the dependencies
    permit it. Where a plan lists a stage before something it depends on, this quietly fixes the
    emission order rather than rejecting the plan — a forward reference is a sequencing slip, not
    a decomposition error, and :meth:`ProcessPlan.ordering_warnings` reports it for review.

    Args:
        stages: The plan's stages, in plan order.
        strict: Raise on a cycle rather than returning a short list.

    Returns:
        The stages in a stable topological order. On a cycle with ``strict=False``, only the
        stages that could be ordered.
    """
    position = {stage.id: index for index, stage in enumerate(stages)}
    remaining = {stage.id: set(stage.depends_on) & set(position) for stage in stages}
    by_id = {stage.id: stage for stage in stages}

    ordered: list[Stage] = []
    while remaining:
        ready = sorted(
            (stage_id for stage_id, deps in remaining.items() if not deps),
            key=lambda stage_id: position[stage_id],
        )
        if not ready:
            if strict:
                msg = f"depends_on forms a cycle involving: {', '.join(sorted(remaining))}"
                raise ValueError(msg)
            break
        for stage_id in ready:
            ordered.append(by_id[stage_id])
            del remaining[stage_id]
        for deps in remaining.values():
            deps.difference_update(ready)
    return ordered


# =============================================================================
# THE PLAN
# =============================================================================


class PlanDraft(_PlanModel):
    """Exactly the part of a plan a model is asked to produce.

    Provenance — versions, hashes, timestamps, approval — is assembled by
    :meth:`ProcessPlan.from_draft` from facts kedge already holds. Keeping it out of the model's
    schema removes a whole class of hallucination and shrinks the structured-output contract to
    the four things that actually require judgement.
    """

    assessment: Assessment
    stages: list[Stage]
    open_questions: list[OpenQuestion]
    """Required, with no default. The model must actively decide this is empty (PLAN 6.2)."""
    dropped: list[DroppedRange] = Field(default_factory=list)
    summary: str | None = Field(
        default=None, description="One or two sentences on the shape of the process overall."
    )

    @model_validator(mode="after")
    def _validate_graph(self) -> PlanDraft:
        _check_stage_graph(self.stages)
        return self


class ProcessPlan(_PlanModel):
    """A complete, versioned, reviewable process plan.

    Frozen: every edit produces a new version, which is what makes the plan history a change
    record rather than a mutable document. Approval is state on the plan, so a plan loaded from
    disk carries its own approvability with it and the scaffolder can refuse without consulting
    anything else.

    Example:
        >>> plan = ProcessPlan.from_draft(
        ...     PlanDraft(
        ...         assessment=Assessment(convertible=0.8),
        ...         stages=[Stage(id="load_handin", intent="Read the hand-in")],
        ...         open_questions=[],
        ...     ),
        ...     workbook="rwa_monthly_v14.xlsx",
        ...     workbook_sha256="0" * 64,
        ... )
        >>> plan.version, plan.approval.state
        (1, <ApprovalState.DRAFT: 'draft'>)
    """

    # ── provenance ───────────────────────────────────────────────────────
    plan_schema_version: str = PLAN_SCHEMA_VERSION
    version: int = Field(default=1, ge=1, description="Monotonic within one workbook's history.")
    created_at: datetime
    workbook: str = Field(description="The source workbook's filename.")
    workbook_sha256: str = Field(description="Ties this plan to the exact file it was written for.")
    analysis_schema_version: str = ANALYSIS_SCHEMA_VERSION
    kedge_version: str = ""
    generated_by: Literal["llm", "human", "seed"] = "llm"
    llm_model: str | None = Field(default=None, description="Model id, when one produced it.")
    based_on_version: int | None = Field(
        default=None, description="The version this was derived from, for the history chain."
    )

    # ── content ──────────────────────────────────────────────────────────
    assessment: Assessment
    stages: list[Stage]
    open_questions: list[OpenQuestion]
    dropped: list[DroppedRange] = Field(default_factory=list)
    summary: str | None = None

    # ── review ───────────────────────────────────────────────────────────
    approval: Approval = Field(default_factory=Approval)

    @model_validator(mode="after")
    def _validate_graph(self) -> ProcessPlan:
        _check_stage_graph(self.stages)
        return self

    # ── construction ─────────────────────────────────────────────────────

    @classmethod
    def from_draft(
        cls,
        draft: PlanDraft,
        *,
        workbook: str,
        workbook_sha256: str,
        analysis_schema_version: str = ANALYSIS_SCHEMA_VERSION,
        kedge_version: str = "",
        generated_by: Literal["llm", "human", "seed"] = "llm",
        llm_model: str | None = None,
        version: int = 1,
        based_on_version: int | None = None,
        created_at: datetime | None = None,
    ) -> ProcessPlan:
        """Assemble a full plan from a model-produced draft plus known provenance."""
        return cls(
            version=version,
            created_at=created_at or datetime.now(UTC),
            workbook=workbook,
            workbook_sha256=workbook_sha256,
            analysis_schema_version=analysis_schema_version,
            kedge_version=kedge_version,
            generated_by=generated_by,
            llm_model=llm_model,
            based_on_version=based_on_version,
            assessment=draft.assessment,
            stages=draft.stages,
            open_questions=draft.open_questions,
            dropped=draft.dropped,
            summary=draft.summary,
        )

    @classmethod
    def from_analysis_draft(
        cls,
        draft: PlanDraft,
        analysis: WorkbookAnalysis,
        **kwargs: Any,
    ) -> ProcessPlan:
        """Assemble a full plan, taking provenance from the analysis it was written against."""
        return cls.from_draft(
            draft,
            workbook=analysis.workbook.filename,
            workbook_sha256=analysis.workbook.sha256,
            analysis_schema_version=analysis.schema_version,
            kedge_version=kwargs.pop("kedge_version", analysis.kedge_version),
            **kwargs,
        )

    def to_draft(self) -> PlanDraft:
        """Strip provenance and approval, leaving the part a model produces.

        This is how a previous quarter's plan re-seeds the next one, and how an approved plan
        for a similar workbook is offered to the model as a worked example rather than starting
        it cold (PLAN 2.2).
        """
        return PlanDraft(
            assessment=self.assessment,
            stages=self.stages,
            open_questions=self.open_questions,
            dropped=self.dropped,
            summary=self.summary,
        )

    # ── accessors ────────────────────────────────────────────────────────

    @property
    def stage_ids(self) -> list[str]:
        """Stage ids in plan order."""
        return [stage.id for stage in self.stages]

    def stage(self, stage_id: str) -> Stage | None:
        """Return the stage with this id, or None."""
        return next((stage for stage in self.stages if stage.id == stage_id), None)

    @property
    def checkpoints(self) -> list[Stage]:
        """The stages that are deliberately not automated."""
        return [stage for stage in self.stages if stage.is_checkpoint]

    @property
    def low_confidence_stages(self) -> list[Stage]:
        """Stages whose scaffolded cell must carry a review marker."""
        return [stage for stage in self.stages if stage.needs_review_marker]

    @property
    def unacknowledged_drops(self) -> list[DroppedRange]:
        """Drops nobody has signed off yet. These block approval."""
        return [drop for drop in self.dropped if not drop.acknowledged]

    @property
    def unanswered_questions(self) -> list[OpenQuestion]:
        """Open questions still without an answer. These warn, but do not block."""
        return [question for question in self.open_questions if not question.answered]

    def ordered_stages(self) -> list[Stage]:
        """Stages in the order the scaffolder will emit them: dependencies first."""
        return topological_stages(self.stages)

    # ── review support ───────────────────────────────────────────────────

    def approval_blockers(self) -> list[str]:
        """Return the reasons this plan cannot be approved yet, empty when it can.

        Structural only — no analysis needed — so the check is identical in the CLI, the web UI,
        and the scaffolder.
        """
        blockers: list[str] = []
        if self.approval.state is ApprovalState.REJECTED:
            blockers.append("the plan has been rejected; propose a new one rather than approving")
        for drop in self.unacknowledged_drops:
            blockers.append(
                f"dropped range {drop.range!r} has not been acknowledged "
                f"(reason given: {drop.reason})"
            )
        rejected = [drop for drop in self.dropped if drop.rejected]
        for drop in rejected:
            if not any(drop.range in stage.sources for stage in self.stages):
                blockers.append(
                    f"dropped range {drop.range!r} was rejected — it must be kept — but no stage "
                    f"lists it as a source"
                )
        return blockers

    @property
    def is_approvable(self) -> bool:
        """Whether :func:`kedge.plan.review.approve` would accept this plan."""
        return not self.approval_blockers()

    def open_questions_warning(self, *, complexity: float) -> str | None:
        """Flag an empty ``open_questions`` on a complex workbook as a review warning.

        A model that asks nothing about a fifteen-sheet workbook has almost certainly invented a
        purpose for something rather than understood it, and silent guessing is the failure mode
        that makes a converted process untrustworthy (PLAN 6.2). Pass the complexity score from
        :func:`kedge.plan.triage.complexity`.

        Args:
            complexity: 0-1 structural complexity of the analysed workbook.

        Returns:
            A warning line, or None when there is nothing to say.
        """
        if self.open_questions:
            return None
        if complexity < 0.35:
            return None
        return (
            f"no open questions on a workbook with complexity {complexity:.2f} — an empty "
            f"open_questions list here is itself suspicious; check that nothing has been "
            f"silently guessed at"
        )

    def ordering_warnings(self) -> list[str]:
        """Report stages listed before something they depend on.

        Not an error: the scaffolder emits in topological order regardless. Worth surfacing in
        review because the plan reads out of sequence to a human.
        """
        position = {stage.id: index for index, stage in enumerate(self.stages)}
        warnings: list[str] = []
        for stage in self.stages:
            for dependency in stage.depends_on:
                if position.get(dependency, -1) > position[stage.id]:
                    warnings.append(
                        f"stage {stage.id!r} is listed before {dependency!r}, which it depends on"
                    )
        return warnings
