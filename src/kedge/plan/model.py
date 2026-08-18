"""The :class:`ProcessPlan` — the reviewable artifact between the facts and the code.

The analyser decides what a workbook *contains*; this module holds what the model proposes to
*do about it*, in a form a human can read in a couple of minutes and correct while correcting is
still cheap. Reviewing a twelve-line plan is minutes; reviewing forty generated cells to work
out that the decomposition was wrong is an afternoon (PLAN 2.2).

Three properties of the schema are load-bearing and are the reason it looks looser than a schema
usually would:

- **Stages are free-form in count and naming.** There is no template and no enum of allowed
  stage ids. A workbook that wants four stages gets four; one that wants fifteen gets fifteen.
  There are exactly two controlled vocabularies: :class:`StageKind`, which exists because the
  scaffolder has to branch on it, and :class:`SourceOrigin`, which exists because "where does
  this input come from" has a small, known set of answers and a free-form string is an answer
  nothing can check and nothing can reason about (PLAN 2.2, 6.2).
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

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kedge.analysis.model import SCHEMA_VERSION as ANALYSIS_SCHEMA_VERSION
from kedge.analysis.model import ExcelPattern, WorkbookAnalysis
from kedge.errors import KedgeError

if TYPE_CHECKING:
    from collections.abc import Collection

PLAN_SCHEMA_VERSION = "1.1"
"""The schema plans are written in.

``1.1`` structured ``Stage.sources``: a list of bare strings became a list of
:class:`StageSource`, each carrying a :class:`SourceOrigin`. The change is backward compatible by
construction — a ``1.0`` plan is a list of strings, every one of which
:func:`_classify_bare_source` reads into an origin and a ref without ever failing — so there is no
migration step to run and no version gate anywhere. The number is here to date a file, not to
admit it.
"""

__all__ = [
    "ANALYSIS_SCHEMA_VERSION",
    "PLAN_SCHEMA_VERSION",
    "Approval",
    "ApprovalState",
    "Assessment",
    "Briefing",
    "Checkpoint",
    "Confidence",
    "DroppedRange",
    "Handoff",
    "HandoffMedium",
    "OpenQuestion",
    "PlanDraft",
    "PlanError",
    "ProcessPlan",
    "SourceOrigin",
    "Stage",
    "StageKind",
    "StageSource",
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
    """What a stage *is*, in the only terms the scaffolder branches on.

    Membership is decided by one test and no other: **does the scaffolder emit something
    different for it?** ``checkpoint`` is the flexibility escape hatch -- a stage that is
    deliberately *not* automated, a judgement call, an override agreed with another team, a
    sanity check -- which scaffolds to an ``mo.ui`` approval cell that blocks everything
    downstream until a decision and a note are recorded (PLAN 2.2). ``handoff`` scaffolds to a
    panel holding a statement for the user to take away and run somewhere kedge cannot reach,
    and no assignment at all: it is the one stage kind that computes nothing.

    A member for "this stage reads from outside the workbook" was the obvious place to put an
    input's provenance and would have been wrong twice over. It would have earned its place only
    if the scaffolder emitted something different for it, and it cannot: a stage may read a
    hand-in *and* a query *and* an upstream frame, so provenance is a property of each input
    rather than of the stage. That is :class:`SourceOrigin`, on :class:`StageSource`. Note the
    contrast with ``handoff``, which passes the same test the other way: a hand-off is not an
    input at all, and there is nothing on :class:`StageSource` that could carry an *outbound*
    artifact.
    """

    LOAD = "load"
    TRANSFORM = "transform"
    OUTPUT = "output"
    CHECKPOINT = "checkpoint"
    HANDOFF = "handoff"


class SourceOrigin(StrEnum):
    """Where one of a stage's inputs comes from.

    ``prompts/propose_user.md`` has always asked the model "what arrives from outside — a hand-in,
    a query, a Power Query table — and what is computed?". Until this vocabulary existed there was
    nowhere to put the answer: ``sources`` was a list of bare strings that nothing validated and
    nothing could reason about, so a query could not be associated with the stage it feeds and a
    workbook wholly dependent on a manual paste looked exactly like one that reads its own cells.

    The members are not a taxonomy for its own sake — each is something kedge already
    distinguishes elsewhere. ``RANGE`` and ``STAGE`` are the two the schema can check on its own:
    one is a location in the workbook, the other is an edge in the stage graph, and
    :func:`_check_stage_graph` now validates it exactly as it validates ``depends_on``. ``QUERY``
    and ``POWER_QUERY`` are the analyser's own two ingestion extractors
    (:attr:`~kedge.analysis.model.WorkbookAnalysis.connections` and ``power_query``). ``HANDIN``
    and ``EXTERNAL`` are the two inputs the system prompt already tells the model to plan as
    inputs rather than as calculations. ``MANUAL`` is a person typing, which is the one origin
    nobody can reproduce and the one a reviewer most needs to see named. ``UNKNOWN`` is the
    honest answer, and like an unknown :class:`~kedge.analysis.model.ExcelPattern` it should come
    with an open question rather than a guess.
    """

    RANGE = "range"
    STAGE = "stage"
    HANDIN = "handin"
    QUERY = "query"
    POWER_QUERY = "power_query"
    EXTERNAL = "external"
    MANUAL = "manual"
    UNKNOWN = "unknown"


class HandoffMedium(StrEnum):
    """What kind of artifact a hand-off puts in front of the user.

    ``sql`` renders in a SQL code block and is the case the vocabulary exists for: a statement
    to run in a database client. ``text`` is anything else somebody has to take elsewhere -- a
    filename to request, a ticket to raise, an instruction to a colleague -- and renders as
    prose. The distinction is presentational and nothing branches on it beyond the fence
    language, which is the point: a hand-off is text and a person, and kedge is not in the
    middle of it.
    """

    SQL = "sql"
    TEXT = "text"


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


class Briefing(_PlanModel):
    """Why this process exists, for whoever opens the notebook in eight months.

    A converted notebook outlives the person who converted it and, usually, the person who ran
    it last. What survives in the workbook is a Sign-off tab holding Purpose, Background and
    Known issues — prose somebody wrote precisely so the next person would not have to guess —
    and the conversion used to throw all of it away, leaving a technically perfect notebook that
    said nothing about what it was for. The analyser already recovers it
    (:attr:`~kedge.analysis.model.WorkbookAnalysis.notes`, with the sheet and cells each note
    came from); this is where it goes.

    **``sources`` is required wherever there is prose, and that is the whole design.** A
    fabricated business rationale in a finance notebook is worse than none: it is confident,
    plausible and unattributable, and the next person has no way to tell it from the real thing.
    Every sentence here has to be traceable to something in the workbook — a documentation
    sheet, a cell comment, a companion procedure — or to a person who said it. Where the
    workbook explains nothing, the honest briefing says so and the field stays empty.

    Example:
        >>> Briefing(purpose="Quarterly accrual uplift", sources=["Sign-off!A3:A4"]).purpose
        'Quarterly accrual uplift'
    """

    purpose: str | None = Field(
        default=None,
        description="What this process is for, in the business's own terms. One or two "
        "sentences: what it produces and what that is used for.",
    )
    background: str | None = Field(
        default=None,
        description="Why it exists and why it is done this way — the decision, the policy, the "
        "history. This is the part nobody can reconstruct from the code.",
    )
    cadence: str | None = Field(
        default=None,
        description="How often it runs and what triggers it: 'quarterly, after the reforecast "
        "is agreed'. A reader's first question after 'what is this'.",
    )
    audience: str | None = Field(
        default=None,
        description="Who relies on the output, and who signs it off.",
    )
    watch_for: list[str] = Field(
        default_factory=list,
        description="Known issues, gotchas, and things that have gone wrong before. Taken from "
        "the workbook where it says; this is the section people actually reread.",
    )
    sources: list[str] = Field(
        default_factory=list,
        description="Where each part of this was learned: 'Sign-off!A3:A4 (Purpose)', 'cell "
        "comment on Calc!C1', 'procedure.docx'. Required wherever there is prose.",
    )

    @model_validator(mode="after")
    def _prose_must_be_attributable(self) -> Briefing:
        """Refuse a briefing that explains a process without saying how it knows.

        The one rule this type exists to enforce. Everything else here is a text field.
        """
        if (self.purpose or self.background or self.watch_for) and not self.sources:
            msg = (
                "a briefing that says what a process is for must say where that came from: a "
                "documentation sheet, a cell comment, a companion procedure, or the person who "
                "told you. Invented background is worse than none -- it is plausible, "
                "confident, and the next reader cannot tell it from the real thing. If the "
                "workbook explains nothing, leave the fields empty and say so in `sources`."
            )
            raise ValueError(msg)
        return self

    @property
    def is_empty(self) -> bool:
        """Whether this says nothing at all, which is a legitimate answer for some workbooks."""
        return not (
            self.purpose or self.background or self.cadence or self.audience or self.watch_for
        )


class Handoff(_PlanModel):
    """A statement the user takes away, runs elsewhere, and comes back from.

    This is the half of a manual process that a data pipeline has no way to express. The
    workbook it comes from says: run this extract, look at what came back, work out the
    adjustment, run *this* update, then re-extract to check it took. kedge cannot run any of it
    -- it holds no connection and issues no statement (see :mod:`kedge.sql`) -- and it should
    not want to. What it can do is hold the state between the steps, do the arithmetic in the
    middle where a formula column used to, generate the second statement from the first
    statement's results, and refuse to go on until the evidence comes back.

    Two shapes, and the difference is where the text comes from:

    - **Static.** ``statement`` carries it verbatim -- normally an extract query the analyser
      recovered from ``xl/connections.xml``. ``parameters`` names the placeholders the notebook
      should fill from its own inputs, so a period end typed once reaches the query.
    - **Generated.** ``built_from`` names an upstream stage and ``template`` is rendered once
      per row of that stage's frame, through :func:`kedge.sql.render_all`. This is the
      ``="UPDATE ... "&F2&"..."`` column that real workbooks are full of, translated into
      something reviewable: the adjustment is computed in polars where it can be checked, and
      the SQL is a rendering of it rather than the place it lives.

    ``instruction`` is not decoration. The user is about to run something against a production
    warehouse, and the sentence telling them where, in what order, and what to bring back is the
    part that makes the notebook a runbook rather than a printout.

    Example:
        >>> Handoff(instruction="Run against RiskWarehouse", statement="SELECT 1").medium
        <HandoffMedium.SQL: 'sql'>
    """

    instruction: str = Field(
        description="What the user should do with this: where to run it, and what to bring "
        "back. Written to somebody who has the client open and the workbook closed."
    )
    medium: HandoffMedium = HandoffMedium.SQL
    statement: str | None = Field(
        default=None,
        description="The statement verbatim, where it is fixed. Mutually exclusive with "
        "`template`.",
    )
    built_from: str | None = Field(
        default=None,
        description="Id of the stage whose frame the statement is rendered from, one statement "
        "per row. Required with `template`.",
    )
    template: str | None = Field(
        default=None,
        description="Statement template rendered once per row of `built_from`, with "
        "`{column_name}` where values belong. Mutually exclusive with `statement`.",
    )
    connection: str | None = Field(
        default=None,
        description="The connection or database this is meant to be run against, named as the "
        "workbook names it. Carried so the notebook can say it rather than the user recalling "
        "it.",
    )
    parameters: list[str] = Field(
        default_factory=list,
        description="Names of `{placeholders}` in a static `statement` that the notebook should "
        "fill from its own inputs -- a period end, an entity list -- rather than hardcode.",
    )
    mutates: bool = Field(
        default=False,
        description="Whether running this changes data. An UPDATE, INSERT, DELETE or MERGE "
        "does; a SELECT does not. True makes the notebook require an explicit confirmation "
        "that the statement was run before anything downstream of it appears.",
    )

    @property
    def needs_confirmation(self) -> bool:
        """Whether the notebook must record that this was carried out before going on.

        Tied to :attr:`mutates` rather than being a separate switch, because the two questions
        have one answer. A read-only query needs no confirmation: the hand-in that follows *is*
        the evidence it was run, and asking as well is friction with nothing behind it. A
        statement that changes data is different in both directions -- nothing downstream is
        evidence it ran, and pasting a re-extract taken *before* it ran is a mistake a user can
        make in one click and nobody can detect afterwards.

        So a mutating hand-off gates what follows on somebody saying they ran it, and that
        assertion goes in the run record with a time against it.
        """
        return self.mutates

    @model_validator(mode="after")
    def _one_source_of_text(self) -> Handoff:
        """A hand-off has exactly one body, and a generated one says what it is generated from.

        Both failures are the same class of plan error -- a proposal that named the shape but
        not the substance -- and both scaffold into a panel that shows the user nothing. Better
        to reject the plan at review, where there is somebody to ask.
        """
        if self.statement and self.template:
            msg = (
                "a hand-off carries either a fixed `statement` or a `template` rendered per "
                "row, not both. Which is it?"
            )
            raise ValueError(msg)
        if not self.statement and not self.template:
            msg = (
                "a hand-off must carry the text the user is being handed: a fixed `statement`, "
                "or a `template` plus the `built_from` stage to render it against"
            )
            raise ValueError(msg)
        if self.template and not self.built_from:
            msg = (
                "a hand-off with a `template` must name the stage it is rendered from in "
                "`built_from`: one statement per row of that stage's frame"
            )
            raise ValueError(msg)
        return self

    @property
    def is_generated(self) -> bool:
        """Whether the text is rendered from an upstream frame rather than fixed."""
        return self.template is not None

    def render(self) -> str:
        """One line for a review pane or an approval card."""
        where = f" against {self.connection}" if self.connection else ""
        changes = ", changes data" if self.mutates else ""
        if self.is_generated:
            return f"{self.medium.value} generated from {self.built_from}{where}{changes}"
        return f"{self.medium.value} statement{where}{changes}"


_ORIGIN_ALIASES: dict[str, SourceOrigin] = {
    "upload": SourceOrigin.HANDIN,
    "upstream": SourceOrigin.STAGE,
    "upstream_stage": SourceOrigin.STAGE,
    "sheet": SourceOrigin.RANGE,
    "worksheet": SourceOrigin.RANGE,
    "cells": SourceOrigin.RANGE,
    "named_range": SourceOrigin.RANGE,
    "sql": SourceOrigin.QUERY,
    "database": SourceOrigin.QUERY,
    "connection": SourceOrigin.QUERY,
    "powerquery": SourceOrigin.POWER_QUERY,
    "external_workbook": SourceOrigin.EXTERNAL,
    "external_link": SourceOrigin.EXTERNAL,
    "typed": SourceOrigin.MANUAL,
    "pasted": SourceOrigin.MANUAL,
    "override": SourceOrigin.MANUAL,
}
"""Words a model reaches for that mean an origin kedge already has. Cheaper than a repair."""

_HANDIN_WORDS = frozenset({"handin", "hand_in", "the_hand_in", "handin_file"})

_A1_RANGE = re.compile(
    r"^\$?[A-Za-z]{1,3}\$?\d+(:\$?[A-Za-z]{1,3}\$?\d+)?$|^\$?[A-Za-z]{1,3}:\$?[A-Za-z]{1,3}$"
)
"""``H2``, ``H2:H500``, ``$A$1:$D$50``, ``AK:AP`` — an unqualified range, with no sheet on it."""

_REF_REQUIRED = (SourceOrigin.RANGE, SourceOrigin.STAGE)

_NO_CODE_KINDS: frozenset[str] = frozenset({StageKind.CHECKPOINT.value, StageKind.HANDOFF.value})
"""Kinds that translate to no code, and so have nothing for a confidence to be about.

Module level rather than a class attribute because pydantic claims any name a model class
declares with a leading underscore as a private attribute, and reading it back through ``cls``
then yields the descriptor rather than the set.
"""


def _normalise(text: str) -> str:
    """Fold a vocabulary word to its canonical form: lower case, underscores for gaps."""
    return text.strip().lower().replace("-", "_").replace(" ", "_")


def _classify_bare_source(text: str, stage_ids: Collection[str] = ()) -> dict[str, Any]:
    """Read a pre-1.1 bare source string as an origin and a ref. Never fails.

    Every plan written before schema 1.1 holds a list of these, documented as "sheet-qualified
    ranges, 'handin', or the id of an upstream stage", so all three have to survive the trip.

    The order of the tests is the order of how sure each one is, and *evidence* is what makes one
    test surer than another, not how memorable the vocabulary word is:

    1. A ``!`` says sheet-qualified, and nothing else in the vocabulary carries one. Unambiguous.
    2. An exact match against another stage id of *this* plan. The plan is the strongest evidence
       available — stronger than a shape and stronger than a keyword, because it is a fact about
       the file being read rather than a guess about what a string looks like.
    3. An A1 *shape*. Only a shape: ``q1``, ``fx1``, ``vat2`` and ``s1`` are all perfectly good
       stage ids that happen to look like cells, which is exactly why 2 is tested first.
    4. The literal ``handin``, which was in the vocabulary and in the PLAN's own worked example —
       but ``load_handin`` is that example's stage name, so a plan whose stage is called ``handin``
       is entirely plausible, and 2 has to win there. This is the one lossy branch (the ref is
       dropped, because the origin is the whole answer), so it is also the one that must go last.

    Anything left is ``unknown`` with the text kept as the ref, which is the whole reason this
    cannot fail: an old plan naming something kedge cannot classify — a named range, a stage that
    was later renamed, a note somebody typed into the list — still loads, still renders, and
    still says what it said. Guessing ``stage`` instead would turn that plan into a validation
    error months after anyone could fix it.

    Args:
        text: The bare string from a 1.0 plan, or from a model that sent one anyway.
        stage_ids: The *other* stages in the plan being loaded, where the caller knows them. A
            stage's own id is excluded by :func:`_stage_with_read_sources`: under 1.0 a stage was
            often named after the region it reads and naming itself was ordinary, so reading that
            as a stage edge would refuse a committed plan under a rule that did not exist when it
            was written.

    Returns:
        Keyword arguments for a :class:`StageSource`.
    """
    cleaned = text.strip()
    if "!" in cleaned:
        return {"origin": SourceOrigin.RANGE.value, "ref": cleaned}
    if cleaned in stage_ids:
        return {"origin": SourceOrigin.STAGE.value, "ref": cleaned}
    if _A1_RANGE.match(cleaned):
        return {"origin": SourceOrigin.RANGE.value, "ref": cleaned}
    if _normalise(cleaned) in _HANDIN_WORDS:
        return {"origin": SourceOrigin.HANDIN.value, "ref": None}
    return {"origin": SourceOrigin.UNKNOWN.value, "ref": cleaned}


class StageSource(_PlanModel):
    """One input to a stage, and where it comes from.

    A bare string is accepted on load and classified by :func:`_classify_bare_source`, because
    every plan written before schema 1.1 is a list of them and those plans are on disk, in git,
    and reviewed. That acceptance is a load-time convenience only: the JSON schema the model is
    given describes the object, so a proposal is asked for the structured form.

    Example:
        >>> StageSource.model_validate("Calc!H2:H500").origin
        <SourceOrigin.RANGE: 'range'>
        >>> StageSource(origin=SourceOrigin.QUERY, ref="MonthlyExposures").render()
        'query MonthlyExposures'
    """

    origin: SourceOrigin = Field(description="What kind of input this is.")
    ref: str | None = Field(
        default=None,
        description="What it names: the sheet-qualified range, the id of the upstream stage, the "
        "connection or query name, the linked workbook, or where a manual entry lands. Required "
        "for `range` and `stage`; elsewhere the origin alone can be the whole answer.",
    )

    # ── normalisation ────────────────────────────────────────────────────

    @model_validator(mode="before")
    @classmethod
    def _accept_bare_string(cls, value: Any) -> Any:
        """Take a bare 1.0 string or the 1.1 object, and name both when it is neither.

        Pydantic's own message for ``sources: [42]`` is "Input should be a valid dictionary or
        instance of StageSource", which is true and misleading: a bare string is accepted too, and
        somebody hand-editing a plan reads that message as "strings are not allowed" and rewrites
        a file that was fine.
        """
        if isinstance(value, str):
            return _classify_bare_source(value)
        if isinstance(value, Mapping | StageSource):
            return value
        msg = (
            f"a source must be an object like {{origin: range, ref: 'Calc!H2:H500'}}, or the bare "
            f"string schema 1.0 wrote — 'Calc!H2:H500', 'handin', or the id of an upstream stage. "
            f"Got {type(value).__name__}: {value!r}"
        )
        raise ValueError(msg)

    @field_validator("origin", mode="before")
    @classmethod
    def _coerce_origin(cls, value: Any) -> Any:
        """Accept the vocabulary case-insensitively; map anything unrecognised to ``unknown``.

        The same bargain :meth:`Stage._coerce_pattern` strikes, for the same reason. A model that
        writes ``Hand-In`` or ``SQL`` has understood the question and spelled the answer its own
        way, and burning a repair round trip on that buys nothing; a word the vocabulary genuinely
        does not have becomes ``unknown``, which is information rather than a silent invention.
        """
        if value is None or isinstance(value, SourceOrigin) or not isinstance(value, str):
            return value
        normalised = _normalise(value)
        if normalised in _HANDIN_WORDS:
            return SourceOrigin.HANDIN
        if normalised in _ORIGIN_ALIASES:
            return _ORIGIN_ALIASES[normalised]
        try:
            return SourceOrigin(normalised)
        except ValueError:
            return SourceOrigin.UNKNOWN

    @field_validator("ref")
    @classmethod
    def _blank_ref_is_no_ref(cls, value: str | None) -> str | None:
        cleaned = (value or "").strip()
        return cleaned or None

    @model_validator(mode="after")
    def _ref_where_the_origin_needs_one(self) -> StageSource:
        if self.origin in _REF_REQUIRED and not self.ref:
            msg = (
                f"a {self.origin.value!r} source must say what it reads: a sheet-qualified range "
                f"like 'Calc!H2:H500', or the id of an upstream stage"
            )
            raise ValueError(msg)
        return self

    # ── rendering ────────────────────────────────────────────────────────

    def render(self) -> str:
        """One line for a review pane, a notebook comment or the approval card.

        ``range Calc!H2:H500``, ``stage load_handin``, ``handin``. The origin leads because it is
        the part a reader is scanning for; a bare ``Calc!AK:AP`` never says whether anyone knew
        where it came from.
        """
        return f"{self.origin.value} {self.ref}" if self.ref else self.origin.value

    def __str__(self) -> str:
        return self.render()


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

    sources: list[StageSource] = Field(
        default_factory=list,
        description="Where this stage's inputs come from, one entry each: a range in the "
        "workbook, an upstream stage, a hand-in, a query, a Power Query table, a linked "
        "workbook, or a person typing.",
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
    handoff: Handoff | None = Field(
        default=None,
        description="A statement the user takes away and runs elsewhere. Required in spirit "
        "for `kind: handoff`. Also valid on a `load` stage, where it means 'here is the query, "
        "and here is where you paste what it returns' -- one step of the process, two cells.",
    )
    notes: str | None = None

    # ── normalisation ────────────────────────────────────────────────────

    @model_validator(mode="before")
    @classmethod
    def _default_checkpoint_confidence(cls, value: Any) -> Any:
        """A stage that generates no code and has no stated confidence means 'n/a', not 'unknown'.

        Both kinds it covers are human steps. Leaving them ``unknown`` would put a review marker
        on a cell with nothing in it to review, and would drag the plan's own confidence summary
        down by counting decisions nobody was ever asked to be confident about.
        """
        no_code = isinstance(value, dict) and value.get("kind") in _NO_CODE_KINDS
        if no_code and not value.get("confidence"):
            return {**value, "confidence": Confidence.NOT_APPLICABLE.value}
        return value

    @model_validator(mode="after")
    def _a_generated_handoff_runs_after_what_it_renders(self) -> Stage:
        """Add ``built_from`` to ``depends_on`` where the plan left it out.

        Ordering comes from ``depends_on`` alone (:func:`topological_stages`), so a hand-off
        rendered from a frame that has not been computed yet scaffolds above the cell that
        defines it. In marimo that is not a subtle bug -- it is a cell that cannot run at all.
        A forward reference is a sequencing slip rather than a decomposition error, so this
        fixes it in place, exactly as :func:`topological_stages` fixes plan order.
        """
        if self.handoff is None or not self.handoff.built_from:
            return self
        built_from = self.handoff.built_from
        if built_from != self.id and built_from not in self.depends_on:
            object.__setattr__(self, "depends_on", [*self.depends_on, built_from])
        return self

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

    # ── construction ─────────────────────────────────────────────────────

    @classmethod
    def validate_in_plan(cls, value: Any, stage_ids: Collection[str]) -> Stage:
        """Validate one stage's raw mapping with the rest of the plan's stage ids in hand.

        :meth:`model_validate` has no plan context, so a bare ``"load_handin"`` in ``sources``
        classifies as ``unknown`` there while the identical text inside a plan mapping classifies
        as ``stage``. That asymmetry is invisible until somebody hits it:
        :func:`kedge.plan.review.edit_stage` validates a single stage and would silently downgrade
        an edge the same edit expressed in YAML would keep. Anything editing one stage of a plan
        it already holds should come through here instead.

        Args:
            value: The raw stage mapping. Anything else is passed through to ``model_validate``.
            stage_ids: The plan's stage ids. The stage's own id is excluded, exactly as it is on
                load — see :func:`_stage_with_read_sources`.
        """
        return cls.model_validate(_stage_with_read_sources(value, set(stage_ids)))

    # ── accessors ────────────────────────────────────────────────────────

    @property
    def is_checkpoint(self) -> bool:
        """Whether this stage is a deliberate human decision rather than code."""
        return self.kind is StageKind.CHECKPOINT

    @property
    def is_handoff(self) -> bool:
        """Whether this stage hands the user something to run elsewhere rather than computing."""
        return self.kind is StageKind.HANDOFF

    @property
    def generates_no_code(self) -> bool:
        """Whether this stage scaffolds to a panel rather than to a translation.

        The two human kinds, asked as one question, because every caller that cares about one
        cares about the other: neither gets a review marker, neither gets a passthrough
        assignment, and neither belongs in a count of how much of the workbook became code.
        """
        return self.kind in (StageKind.CHECKPOINT, StageKind.HANDOFF)

    @property
    def upstream_stage_ids(self) -> list[str]:
        """The ids of the stages this one reads, from its sources and its hand-off.

        Distinct from ``depends_on``, which is ordering: a stage can be gated by a checkpoint it
        reads nothing from, and — until somebody lists it — can read a frame it forgot to depend
        on. :func:`kedge.plan.review.review_warnings` reports the second.

        A generated hand-off's ``built_from`` counts as a read, because it is one: the statement
        is rendered row by row out of that stage's frame.
        """
        ids = [
            source.ref
            for source in self.sources
            if source.origin is SourceOrigin.STAGE and source.ref is not None
        ]
        if self.handoff is not None and self.handoff.built_from:
            ids.append(self.handoff.built_from)
        return ids

    @property
    def needs_review_marker(self) -> bool:
        """Whether the scaffolded cell should carry an explicit low-confidence marker.

        ``unknown`` counts. A stage whose confidence was never stated must not scaffold looking
        as finished as one the model was sure about.
        """
        if self.generates_no_code:
            return False
        return self.confidence in (Confidence.LOW, Confidence.UNKNOWN)

    def effective_checkpoint(self) -> Checkpoint:
        """Return this stage's checkpoint spec, synthesising one from the intent if absent."""
        if self.checkpoint is not None:
            return self.checkpoint
        return Checkpoint(question=f"Approve: {self.intent}")

    def effective_handoff(self) -> Handoff:
        """Return this stage's hand-off spec, synthesising a placeholder one if absent.

        Unlike a checkpoint, a hand-off cannot be meaningfully synthesised: a checkpoint's whole
        content is a question, and the intent *is* the question, but nobody can guess a
        statement. So the synthesised one carries the intent as its instruction and a statement
        saying in as many words that the plan did not supply it. That scaffolds to a panel the
        user can see is unfinished, which is the honest outcome -- an exception here would put
        one malformed stage between the user and a notebook that is otherwise fine.
        """
        if self.handoff is not None:
            return self.handoff
        return Handoff(
            instruction=self.intent,
            statement=(
                "-- TODO(kedge): the plan marked this stage a hand-off but supplied no "
                "statement.\n-- Paste the query or command this step runs, then re-approve."
            ),
        )


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
    """Validate stage ids, dependency references, source references, and acyclicity.

    A ``stage`` source is checked exactly as ``depends_on`` is, and for the same reason: both
    name a stage, and a name that resolves to nothing is a plan describing a step that reads
    something which does not exist. It was silent while ``sources`` was free-form, because
    ``"laod_handin"`` and ``"Ref!A1:D50"`` are the same kind of thing to a list of strings.

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
        for upstream in stage.upstream_stage_ids:
            if upstream == stage.id:
                msg = f"stage {stage.id!r} lists itself as one of its own sources"
                raise ValueError(msg)
            if upstream not in seen:
                msg = f"stage {stage.id!r} reads unknown stage {upstream!r} in its sources"
                raise ValueError(msg)
        if stage.handoff is not None and stage.handoff.built_from == stage.id:
            msg = (
                f"stage {stage.id!r} renders its hand-off from itself. A generated statement "
                f"is rendered from an upstream stage's frame."
            )
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
# LOADING A PLAN WRITTEN BEFORE SCHEMA 1.1
# =============================================================================


def _read_bare_sources(value: Any) -> Any:
    """Classify a plan's bare 1.0 source strings before its stages are validated.

    :class:`StageSource` classifies a bare string on its own, and on its own it cannot tell an
    upstream stage id from a named range: both are one word. A plan can, because it holds every
    stage id — so this runs first, on the raw mapping, and hands those ids to
    :func:`_classify_bare_source`.

    Only exact matches against *other* stages are upgraded, which is what makes this safe on a
    plan written months ago: a string that matches no stage id stays whatever
    :class:`StageSource` makes of it on its own, so a stage renamed since is a source that reads
    ``unknown``, never a plan that will not load. The exclusion of a stage's own id lives in
    :func:`_stage_with_read_sources`, which explains why.

    Raw mappings only — the YAML and JSON paths, which is where every pre-1.1 plan arrives. A
    :class:`Stage` built in Python has already classified its sources, and an ``unknown`` there
    is as likely to be a deliberate "I could not tell" as an unresolved id. Rewriting it would be
    inventing a fact; code that means an upstream stage should say
    ``StageSource(origin=SourceOrigin.STAGE, ref=...)``.
    """
    if not isinstance(value, dict):
        return value
    stages = value.get("stages")
    if not isinstance(stages, list):
        return value
    stage_ids = {
        stage["id"].strip()
        for stage in stages
        if isinstance(stage, dict) and isinstance(stage.get("id"), str)
    }
    if not stage_ids:
        return value
    return {**value, "stages": [_stage_with_read_sources(item, stage_ids) for item in stages]}


def _stage_with_read_sources(stage: Any, stage_ids: set[str]) -> Any:
    """Classify one stage's bare sources against every stage id *but its own*.

    A 1.0 stage naming itself was ordinary, because a stage was often named after the region it
    reads: ``id: adjustments`` with ``sources: [adjustments]`` meant "the adjustments region",
    written when nothing in the schema could have meant anything else. Reading that as a stage
    edge makes :func:`_check_stage_graph` refuse a file somebody committed months ago, under a
    rule that did not exist when they wrote it — the worst thing a schema change can do. Excluded
    from the candidates, it falls through to ``unknown``, keeps its text, and loads.

    ``_check_stage_graph`` is deliberately left alone: a self-source *written* as
    ``origin: stage`` in a 1.1 plan is a genuine error and still says so.
    """
    if not isinstance(stage, dict):
        return stage
    sources = stage.get("sources")
    if not isinstance(sources, list) or not any(isinstance(item, str) for item in sources):
        return stage
    own = stage.get("id")
    others = stage_ids - {own.strip()} if isinstance(own, str) else stage_ids
    return {
        **stage,
        "sources": [
            _classify_bare_source(item, others) if isinstance(item, str) else item
            for item in sources
        ],
    }


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
    briefing: Briefing | None = Field(
        default=None,
        description="Why this process exists, drawn from the workbook's own documentation. "
        "Distinct from `summary`, which describes the shape of the conversion: this describes "
        "the *business* process, and it is what the next person to open the notebook in eight "
        "months needs. Every claim must cite where it came from.",
    )

    @model_validator(mode="before")
    @classmethod
    def _classify_bare_sources(cls, value: Any) -> Any:
        return _read_bare_sources(value)

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
    briefing: Briefing | None = None

    # ── review ───────────────────────────────────────────────────────────
    approval: Approval = Field(default_factory=Approval)

    @model_validator(mode="before")
    @classmethod
    def _classify_bare_sources(cls, value: Any) -> Any:
        return _read_bare_sources(value)

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
            briefing=draft.briefing,
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
            briefing=self.briefing,
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
            # The same test it always was — is this exact range named by some stage — moved from
            # the string to the ref it became. A reviewer who clears this blocker by typing the
            # range into a stage's `sources`, which is what the CLI tells them to do, writes a
            # bare string that loads as a `range` source with that ref, so the remedy still works.
            claimed = any(
                source.ref == drop.range for stage in self.stages for source in stage.sources
            )
            if not claimed:
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
