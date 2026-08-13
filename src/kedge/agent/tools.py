"""The tool surface: sixteen tools, one choke point, and a cap the model cannot walk around.

PLAN M4 lists the tools and PLAN 2.3 states the rule that shapes this module: *every*
value-returning tool caps its result at 100 rows and 32KB, truncating with an explicit
``[… N more rows omitted]`` marker. The reasoning is not about bandwidth. A model that silently
believes it read the whole sheet will assert things about the tail that are not true, and it will
do so with the same confidence it uses for the rows it actually saw.

**The cap is structural, not a convention.** Three things enforce it, and none of them is "the
tool remembers":

1. A tool cannot return a string. It returns a :class:`ToolResult`, and the only constructors
   that build one from data — :meth:`ToolResult.from_rows` and :meth:`ToolResult.note` — apply the
   row cap and the byte cap on the way through.
2. :meth:`ToolRegistry.dispatch` is the only path from a tool to the model, and it re-applies the
   byte cap to whatever came back. A handler that hand-builds a ``ToolResult`` around a megabyte
   of text still emits a capped payload.
3. The same choke point writes the audit line. There is no way to return a payload to the model
   without it being counted, and no way to count it without it having been capped first.

Adding a seventeenth tool therefore inherits the cap by construction. Forgetting it would require
deleting :meth:`dispatch`.

**The model never touches the notebook directly.** Structural changes go through the validation
gate first (:mod:`kedge.agent.validate`), and ``delete_cell`` does not delete: it records a
request and returns it, because a destructive change to the user's notebook is the user's decision
and a model that can delete a cell on its own reasoning will eventually delete the wrong one.

**Nor does it touch the plan.** ``propose_plan`` and ``amend_plan`` are the same shape: both
record a proposal, say plainly that nothing has happened, and leave the writing to the user's
half of the decision in :mod:`kedge.server.routes`. They divide the ground between them, and the
division is the control PLAN 2.2 puts around a plan change — ``propose_plan`` authors a first
plan and refuses once one has been approved, so a whole replacement decomposition cannot be
walked past the review that ``amend_plan`` puts a single change through. It refuses three more
things, for the same reason it refuses that one: it is the tool that turns a conversation into a
large, structured, confident-looking artifact, so what must not happen is refused rather than
discouraged. A workbook :func:`kedge.plan.triage.triage` returned ``STOP`` for is not planned at
all, which is the refusal :func:`kedge.plan.propose.propose_plan` makes on the CLI path and which
must mean the same thing reached through the chat. A session that has read nothing about the
workbook cannot propose a plan about it (:data:`WORKBOOK_READING_TOOLS`). And no plan may be
larger than a notebook someone will actually review (:data:`MAX_PROPOSED_STAGES`).

Every collaborator is optional and every tool degrades to a sentence explaining what is missing.
A workspace with no marimo attached, no analysis, no plan, an empty ``utils/`` and no knowledge
pack is a normal early state, not a broken one — and "there is no plan yet" is a far more useful
tool result than a traceback.

**A result is remembered only when it cannot go stale.** :class:`ToolKind` says what a tool does;
:class:`Volatility` says how long its answer stays true, and only the session-stable tools — the
ones that are pure functions of the deterministic analysis and of an ``.xlsx`` kedge never writes
— are held for the session. The cache is keyed on a fingerprint of those files, so a user who
saves the workbook from Excel mid-session invalidates it without knowing it exists, and a hit
still goes back through :meth:`ToolRegistry._finalise`, so the audit log gains no holes. The open
workbook handle is keyed on that same fingerprint (:meth:`ToolRegistry._workbook`), because a
cache that invalidates over a handle that does not would re-run the handler against the file as
it was and charge the model for the same stale answer twice.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kedge.agent.validate import (
    Policy,
    RoundingContext,
    ValidationReport,
    validate_cell,
    violations_from_kernel_error,
)
from kedge.errors import KedgeError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from kedge.agent.audit import OutboundLog
    from kedge.agent.context import NameRegistry, NotebookState
    from kedge.analysis.model import WorkbookAnalysis
    from kedge.notebook.model import NotebookBridge
    from kedge.plan.model import ProcessPlan
    from kedge.plan.store import PlanStore
    from kedge.workspace import Workspace

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_CACHED_BYTES",
    "MAX_CACHED_RESULTS",
    "MAX_PAYLOAD_BYTES",
    "MAX_PROPOSED_STAGES",
    "MAX_ROWS",
    "OMISSION_TEMPLATE",
    "TOOL_SPECS",
    "WORKBOOK_READING_TOOLS",
    "Caps",
    "PendingAmendment",
    "PendingDeletion",
    "PendingProposal",
    "ToolContext",
    "ToolKind",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "Volatility",
    "tool_names",
    "tool_schemas",
    "volatility_of",
]

MAX_ROWS = 100
"""Default row cap for every value-returning tool (PLAN 2.3). Overridden by ``[sampling]``."""

MAX_PAYLOAD_BYTES = 32_768
"""Default payload cap in bytes (PLAN 2.3). Overridden by ``[sampling]``."""

MAX_CACHED_RESULTS = 32
"""How many session-stable results one registry holds before the oldest is dropped."""

MAX_CACHED_BYTES = 1_048_576
"""How many bytes of cached payload one registry holds.

Roughly ``MAX_CACHED_RESULTS`` results at the payload cap, so neither bound is decorative: a
session that profiles fifty columns is held by the count, and one that reads fifty wide ranges by
the bytes.
"""

MAX_PROPOSED_STAGES = 50
"""Ceiling on the stages one proposed plan may carry.

One stage becomes one cell, so this is the size of the notebook the plan describes. Fifty is the
point past which two other things have already broken: a reviewer cannot hold that many stages in
their head, which is the whole pitch (PLAN 2.6), and :data:`~kedge.agent.loop.DEFAULT_MAX_STEPS`
is fifty completions, so a longer plan could not be scaffolded inside one turn even if it were
right. A workbook that genuinely needs more stages than this is two processes, and the useful
answer is to say so rather than to write one plan that nobody will read.
"""

WORKBOOK_READING_TOOLS: frozenset[str] = frozenset(
    {"inspect_workbook", "sample_data", "profile_column", "read_range"}
)
"""The tools whose successful use means the model has actually looked at this workbook.

The four that return facts about the workbook — the deterministic analysis beyond the pinned
summary, and the ``.xlsx`` itself. ``probe`` is deliberately not among them: it runs against the
live kernel and describes the hand-in loaded into the notebook rather than the process the
workbook performs, which is what a plan decomposes. It could not be a route round this anyway,
since probing needs cells and cells need an approved plan.

Used by ``propose_plan`` to refuse a plan written from nothing. Membership is what the registry
counts, and only an ``ok`` result counts: a ``sample_data`` call against a sheet that does not
exist has read no more than not calling it would have.
"""

OMISSION_TEMPLATE = "[… {count} more rows omitted]"
"""The marker PLAN M4 requires on every truncated result. Its presence is the model's signal."""

_BYTES_MARKER = (
    "[… truncated at the payload cap: the tail of this result is missing. Ask for a narrower "
    "slice, or use `probe` for an aggregate.]"
)

_MAX_SAMPLE_COLUMNS = 40
_PROBE_CODE_LIMIT = 4_000
_DEFAULT_HEADER_ROW = 1

_PROBE_NO_VALUE = (
    "no value: the probe ended in a block, a loop or an import, so there was nothing to report. "
    "End it with the value you want back — either a bare expression such as `frame.height` or an "
    "assignment to a single name, whose value is reported for you."
)
_PROBE_PRINTED = (
    "(the probe's last expression evaluated to None, which is what `print(...)` returns. The "
    "printed text above is the whole answer; end with a bare expression to get a value too.)"
)


# ── caps ─────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Caps:
    """The row and payload ceilings every value-returning tool passes through.

    Example:
        >>> Caps().max_rows
        100
    """

    max_rows: int = MAX_ROWS
    max_payload_bytes: int = MAX_PAYLOAD_BYTES

    @classmethod
    def from_config(cls, config: Any) -> Caps:
        """Read the caps from a :class:`~kedge.config.Config`'s ``[sampling]`` section."""
        sampling = getattr(config, "sampling", None)
        if sampling is None:
            return cls()
        return cls(
            max_rows=int(sampling.max_rows),
            max_payload_bytes=int(sampling.max_payload_bytes),
        )


def _cap_text(text: str, caps: Caps) -> tuple[str, bool]:
    """Clip ``text`` to the payload cap, appending a marker when anything was dropped."""
    encoded = text.encode("utf-8")
    if len(encoded) <= caps.max_payload_bytes:
        return text, False
    budget = max(0, caps.max_payload_bytes - len(_BYTES_MARKER.encode("utf-8")) - 1)
    clipped = encoded[:budget].decode("utf-8", errors="ignore").rstrip()
    return f"{clipped}\n{_BYTES_MARKER}", True


def _render_rows(header: str, columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    body = "\n".join(json.dumps(list(row), default=str, ensure_ascii=False) for row in rows)
    lines = [header] if header else []
    if columns:
        lines.append("columns: " + json.dumps(list(columns), ensure_ascii=False))
    if body:
        lines.append(body)
    return "\n".join(lines)


def _fit_rows(
    header: str, columns: Sequence[str], rows: Sequence[Sequence[Any]], caps: Caps, omitted: int
) -> tuple[str, int, int]:
    """Render as many rows as fit both caps, returning the text, the count kept, and the count lost.

    Rows are dropped from the tail and counted rather than the text being cut mid-value: a payload
    that stops halfway through a number is worse than one that stops between rows, because only the
    second can carry an honest ``[… N more rows omitted]``.
    """
    kept = list(rows)
    dropped = omitted
    while True:
        marker = OMISSION_TEMPLATE.format(count=dropped) if dropped else ""
        text = _render_rows(header, columns, kept)
        if marker:
            text = f"{text}\n{marker}" if text else marker
        if len(text.encode("utf-8")) <= caps.max_payload_bytes or not kept:
            return text, len(kept), dropped
        shed = max(1, len(kept) // 5)
        kept = kept[:-shed]
        dropped += shed


# ── results ──────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What a tool returns to the model, plus what the audit log records about it.

    Never constructed with raw data by a handler: :meth:`from_rows` and :meth:`note` are the way
    in, and both cap. The notebook fields exist so the loop can raise the right
    :mod:`kedge.server.events` without tools knowing about the event vocabulary.

    Example:
        >>> ToolResult.note("6 cells", summary="6 cells").text
        '6 cells'
    """

    text: str
    ok: bool = True
    summary: str = ""
    sheet: str | None = None
    columns: tuple[str, ...] = ()
    row_count: int = 0
    omitted_rows: int = 0
    truncated: bool = False
    redacted_columns: int = 0

    violations: tuple[str, ...] = ()
    validated: bool | None = None
    """``True``/``False`` when the validation gate ran, ``None`` when the tool does not use it."""

    draft_rejected: bool = False
    """The model's own draft came back wrong and the loop should count the attempt.

    Set only where calling again *with a corrected draft* is the right response — a plan whose
    fields did not validate, a plan too large to be a plan. A refusal no retry can change (no plan
    store attached, a plan already approved) leaves this ``False``, because capping those would be
    counting the wrong thing and the message at the cap would be advice about the wrong problem.
    :meth:`kedge.agent.loop.KedgeAgent._invoke` is what reads it, and it stops the turn at
    :data:`~kedge.agent.loop.MAX_DRAFT_ATTEMPTS` exactly as the validation gate stops a cell.
    """

    cell_id: str | None = None
    cell_name: str | None = None
    cell_preview: str = ""
    cell_ran: bool = False
    cell_ok: bool | None = None
    cell_error: str | None = None

    @property
    def byte_count(self) -> int:
        """The payload's size in bytes, as recorded in the audit log."""
        return len(self.text.encode("utf-8"))

    @classmethod
    def note(
        cls,
        text: str,
        *,
        ok: bool = True,
        summary: str = "",
        caps: Caps | None = None,
        **extra: Any,
    ) -> ToolResult:
        """A prose result. Capped like everything else — prose can be long too."""
        capped, truncated = _cap_text(text, caps or Caps())
        return cls(
            text=capped,
            ok=ok,
            summary=summary or _first_line(capped),
            truncated=truncated,
            **extra,
        )

    @classmethod
    def from_rows(
        cls,
        rows: Sequence[Sequence[Any]],
        *,
        columns: Sequence[str],
        caps: Caps,
        header: str = "",
        sheet: str | None = None,
        total: int | None = None,
        summary: str = "",
        redacted_columns: int = 0,
    ) -> ToolResult:
        """Render tabular data under both caps, with an explicit omission marker.

        Args:
            rows: The rows to render, already filtered to the requested columns.
            columns: Column labels, in the same order as each row.
            caps: The ceilings to enforce.
            header: A one-line description placed above the data.
            sheet: The sheet the data came from, for the audit log.
            total: How many rows exist in total, where that is known. The difference between this
                and what is rendered is what the marker reports.
            summary: The activity-trail summary; derived when not given.
            redacted_columns: How many columns had their values hashed.

        Returns:
            The capped result.
        """
        available = len(rows)
        kept = list(rows[: caps.max_rows])
        omitted = max(0, (total if total is not None else available) - len(kept))
        text, count, omitted = _fit_rows(header, columns, kept, caps, omitted)
        return cls(
            text=text,
            summary=summary or f"{count} row(s)" + (f", {omitted} omitted" if omitted else ""),
            sheet=sheet,
            columns=tuple(columns),
            row_count=count,
            omitted_rows=omitted,
            truncated=bool(omitted),
            redacted_columns=redacted_columns,
        )


def _first_line(text: str, limit: int = 160) -> str:
    line = " ".join(text.strip().splitlines()[0].split()) if text.strip() else ""
    return line if len(line) <= limit else line[: limit - 1] + "…"


# ── specs ────────────────────────────────────────────────────────────────────────────────────


class ToolKind(StrEnum):
    """What a tool does, which decides whether it is audited and whether it can mutate."""

    VALUE = "value"
    """Returns workbook or kernel data to the model. Capped and audited."""
    NOTEBOOK = "notebook"
    """Changes the notebook. Goes through the validation gate."""
    CONTEXT = "context"
    """Returns kedge's own artifacts: the plan, the catalogue, a knowledge pack."""


class Volatility(StrEnum):
    """How long a tool's result stays true, which is what decides whether it may be remembered.

    Orthogonal to :class:`ToolKind`. That axis is about what a tool *does* — return data, change
    the notebook, read kedge's own artifacts — and on it ``inspect_workbook``, which reads a file
    the analyser wrote once, sits beside ``probe``, which runs arbitrary code against a live
    kernel. This axis asks the other question: *called again with the same arguments an hour
    later, could the answer have changed?*

    Answer it for a new tool by naming what would have to change for its result to become wrong.
    Nothing kedge or the user can do within a session — :attr:`SESSION_STABLE`. A file in the
    project directory — :attr:`ARTIFACT_TIED`. The kernel, the notebook, or the call's own side
    effects — :attr:`VOLATILE`, which is also the default, so a tool added without a thought
    spent here is simply never remembered.
    """

    SESSION_STABLE = "session_stable"
    """A pure function of the deterministic analysis and of the ``.xlsx``, neither of which kedge
    writes. Cached for the session, keyed on a fingerprint of those files."""
    ARTIFACT_TIED = "artifact_tied"
    """True until one of kedge's own artifacts changes — the plan, ``utils/``, ``context/``. Not
    cached today: these are cheap to read, and the plan is pinned into every turn regardless."""
    VOLATILE = "volatile"
    """True only at the moment it was read. A live kernel, a mutation, or a decision put to the
    user. Never cached, and never carried forward as though it still described anything."""


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One tool as the model sees it, plus how kedge treats it."""

    name: str
    description: str
    properties: dict[str, Any]
    required: tuple[str, ...] = ()
    kind: ToolKind = ToolKind.VALUE

    volatility: Volatility = Volatility.VOLATILE
    """How long this tool's result stays true (:class:`Volatility`).

    The default is deliberately the pessimistic one. A tool added later must opt in to being
    remembered, because inheriting a cache by omission is how a payload that stopped being true
    reaches the model with nobody having decided that it should.
    """

    def schema(self) -> dict[str, Any]:
        """The OpenAI chat-completions tool definition.

        Assembled key by key rather than dumped from the dataclass, and deliberately so: these
        schemas are the head of the system prompt, which is the prompt cache's prefix, so a field
        added here for kedge's own use would be a silent cost regression on every turn of every
        session. ``kind`` and ``volatility`` are kedge's business and stay out of it.
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.properties,
                    "required": list(self.required),
                    "additionalProperties": False,
                },
            },
        }


def _string(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": "string", "description": description, **extra}


def _integer(description: str, **extra: Any) -> dict[str, Any]:
    return {"type": "integer", "description": description, **extra}


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="list_cells",
        description=(
            "The notebook as the kernel currently sees it: id, name, code, defs and refs. Call it "
            "before editing anything — the user edits cells directly, so what you remember is "
            "stale. Reading a cell's code is also what allows a later edit_cell to replace it."
        ),
        properties={
            "cell": _string("One cell id or name. Omitted, every cell is listed."),
            "with_code": {
                "type": "boolean",
                "description": (
                    "Include each cell's source. Default true. False gives a structural listing "
                    "and leaves marimo's staleness guard armed for every cell."
                ),
            },
        },
        kind=ToolKind.NOTEBOOK,
    ),
    ToolSpec(
        name="propose_cell",
        description=(
            "Create a new named cell. Refused until the user has approved a plan. The body is "
            "checked by the validation gate — syntax, the marimo single-definition rule against "
            "the live graph, policy, and output style — before it reaches the kernel. Violations "
            "come back to you to fix."
        ),
        properties={
            "name": _string("Cell name: a plain identifier, named after the business step."),
            "code": _string("The complete cell body."),
            "after": _string("Insert after this cell id or name."),
            "before": _string("Insert before this cell id or name."),
            "run": {"type": "boolean", "description": "Run the cell on creation. Default true."},
        },
        required=("name", "code"),
        kind=ToolKind.NOTEBOOK,
    ),
    ToolSpec(
        name="edit_cell",
        description=(
            "Replace a cell's full body. Partial edits do not exist. Refused until the user has "
            "approved a plan, and refused unless the cell was read at its current version, which "
            "is how the user's own edits are protected; if it comes back stale, list_cells that "
            "cell and look at what they wrote before deciding."
        ),
        properties={
            "cell": _string("The cell id or name to replace."),
            "code": _string("The complete replacement body."),
            "run": {"type": "boolean", "description": "Run the cell after editing. Default true."},
        },
        required=("cell", "code"),
        kind=ToolKind.NOTEBOOK,
    ),
    ToolSpec(
        name="run_cell",
        description="Re-run an existing cell and report its status and any errors.",
        properties={"cell": _string("The cell id or name to run.")},
        required=("cell",),
        kind=ToolKind.NOTEBOOK,
    ),
    ToolSpec(
        name="delete_cell",
        description=(
            "Request that a cell be deleted. This never deletes on your say-so: it records the "
            "request, reports which cells depend on the target, and asks the user to confirm. "
            "Prefer edit_cell — replacing a body keeps the cell's identity and its place in the "
            "graph, where delete-then-create trips the single-definition check partway through."
        ),
        properties={
            "cell": _string("The cell id or name to delete."),
            "reason": _string("Why it should go. The user reads this before confirming."),
        },
        required=("cell", "reason"),
        kind=ToolKind.NOTEBOOK,
    ),
    ToolSpec(
        name="inspect_workbook",
        description=(
            "Progressive disclosure into the deterministic analysis: formula bodies, references, "
            "findings, named ranges, connections, Power Query, process notes and the dependency "
            "graph. The summary and column profiles are already in your context; this is the "
            "detail behind them."
        ),
        properties={
            "section": _string(
                "What to read.",
                enum=[
                    "sheets",
                    "operations",
                    "operation",
                    "findings",
                    "named_ranges",
                    "connections",
                    "power_query",
                    "notes",
                    "graph",
                    "cached_values",
                ],
            ),
            "sheet": _string("Restrict to one sheet, where the section supports it."),
            "operation": _string("A logical operation id, required by the 'operation' section."),
        },
        required=("section",),
        volatility=Volatility.SESSION_STABLE,
    ),
    ToolSpec(
        name="sample_data",
        description=(
            "A bounded row slice from a sheet, with the header row resolved from the analysis. "
            "Row and byte capped: a truncated result ends with an explicit omission marker and "
            "you must not reason about the rows you did not see."
        ),
        properties={
            "sheet": _string("Sheet name."),
            "rows": _integer("How many rows to return. Capped.", minimum=1),
            "start_row": _integer("1-based first data row. Defaults to just after the header."),
            "columns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Column headers or letters to keep. Omitted, all columns.",
            },
        },
        required=("sheet",),
        volatility=Volatility.SESSION_STABLE,
    ),
    ToolSpec(
        name="profile_column",
        description=(
            "The full profile for one column: dtype, nulls, cardinality, numeric statistics, "
            "top-k values, head and tail, and format anomalies such as numbers stored as text."
        ),
        properties={
            "sheet": _string("Sheet name."),
            "column": _string("Column letter, e.g. 'H', or the header text."),
        },
        required=("sheet", "column"),
        volatility=Volatility.SESSION_STABLE,
    ),
    ToolSpec(
        name="read_range",
        description=(
            "Verbatim cell values from an A1 range — the tool for parameter tables and lookup "
            "tables, where the values are the logic. Row and byte capped."
        ),
        properties={
            "sheet": _string("Sheet name."),
            "range": _string("An A1 range such as 'A1:D50', or a single cell."),
            "view": _string(
                "'values' for what Excel last calculated, 'formulas' for formula text.",
                enum=["values", "formulas"],
            ),
        },
        required=("sheet", "range"),
        volatility=Volatility.SESSION_STABLE,
    ),
    ToolSpec(
        name="probe",
        description=(
            "Run throwaway Python against the live kernel and get the real value back. Nothing is "
            "left behind: the scratchpad discards new bindings, so a probe cannot create a cell. "
            "Use it to check join-key uniqueness, row counts and totals before you write the "
            "cell that depends on them — and to get an aggregate that is never truncated."
        ),
        properties={
            "code": _string(
                "Python source. End it with the value you want back: a bare expression such as "
                "`frame.height`, or an assignment to a single name, whose value is reported for "
                "you. A trailing `print(...)` returns the printed text but no value."
            )
        },
        required=("code",),
    ),
    ToolSpec(
        name="get_plan",
        description=(
            "The approved process plan: stages, order, assumptions, checkpoints and open "
            "questions. These are your standing instructions."
        ),
        properties={},
        kind=ToolKind.CONTEXT,
        volatility=Volatility.ARTIFACT_TIED,
    ),
    ToolSpec(
        name="propose_plan",
        description=(
            "Author the process plan: the stages in order, what each is for, where its inputs "
            "come from, what it assumes, and what you still do not know. Read the workbook "
            "first — a plan is a reading of it, not a guess at one, and this is refused until you "
            "have used inspect_workbook, sample_data, profile_column or read_range at least once. "
            "An account left in the chat is compacted away as the conversation grows; a plan is "
            "on disk, versioned and in front of you on every later turn. Requires the user's "
            "approval like the original planning step: it records the proposal and surfaces it, "
            "writing nothing and changing no instructions. Refused once a plan is approved, and "
            "for a workbook kedge's triage recommended against converting."
        ),
        properties={
            "plan": _string(
                'The plan as a JSON object: {"summary": one or two sentences on the shape of the '
                'process, "stages": [{"id": short slug, becomes the cell name, "intent": what the '
                'step is for in the business\'s terms, "kind": load|transform|output|checkpoint, '
                '"sources": [{"origin": range|stage|handin|query|power_query|external|manual|'
                'unknown, "ref": what it names}], "depends_on": stage ids that must run first, '
                '"confidence": high|medium|low, "assumptions": what the translation takes for '
                'granted, "operations": ids of the analysis operations it implements, '
                '"excel_pattern": the pattern it translates}], "open_questions": what you could '
                "not work out — required, and an empty list on a complex workbook is itself "
                'suspicious, "dropped": [{"range": ..., "reason": ...}]}. Only id and intent are '
                "required. Send no assessment: convertibility is scored by kedge's triage."
            )
        },
        required=("plan",),
        kind=ToolKind.CONTEXT,
    ),
    ToolSpec(
        name="amend_plan",
        description=(
            "Propose a change to the approved plan when the workbook disagrees with it. Requires "
            "the user's approval, exactly like the original plan did — this records the proposal "
            "and surfaces it; it does not change the plan or your instructions by itself."
        ),
        properties={
            "rationale": _string("What the workbook showed that the plan did not anticipate."),
            "change": _string("The amendment you are proposing, stated concretely."),
            "stage": _string("The stage id it affects, where it affects one."),
        },
        required=("rationale", "change"),
        kind=ToolKind.CONTEXT,
    ),
    ToolSpec(
        name="reconcile",
        description=(
            "Compare a notebook variable against the values Excel last calculated for a workbook "
            "range, at the configured tolerance. Reports rows matched, rows differing, worst "
            "absolute and relative delta, and the first mismatching rows side by side. Where the "
            "workbook carries no cached values this reports 'not reconciled' — never 'passed'."
        ),
        properties={
            "variable": _string("The notebook variable holding the frame or series."),
            "reference": _string("The workbook range to compare against, e.g. 'Calc!H2:H50000'."),
            "column": _string("The column within the frame, where the variable is a frame."),
            "region_id": _string("A stable slug for this region, for the reconciliation panel."),
        },
        required=("variable", "reference"),
    ),
    ToolSpec(
        name="list_utils",
        description=(
            "The catalogue of reusable functions in the project's utils package: name, signature "
            "and one line of description. Reuse one rather than reinventing it."
        ),
        properties={"query": _string("Filter by substring against name and description.")},
        kind=ToolKind.CONTEXT,
        volatility=Volatility.ARTIFACT_TIED,
    ),
    ToolSpec(
        name="get_knowledge",
        description=(
            "Fetch a context pack: documented warehouse tables with their grain, join keys and "
            "gotchas, plus the project's conventions and glossary. Omit the key for everything."
        ),
        properties={"key": _string("A document key or a table name. Omitted, the whole pack.")},
        kind=ToolKind.CONTEXT,
        volatility=Volatility.ARTIFACT_TIED,
    ),
)


def tool_schemas() -> list[dict[str, Any]]:
    """Return every tool definition in the OpenAI chat-completions format."""
    return [spec.schema() for spec in TOOL_SPECS]


def tool_names() -> tuple[str, ...]:
    """Return every tool name, in the order they are offered to the model."""
    return tuple(spec.name for spec in TOOL_SPECS)


_SPECS_BY_NAME = {spec.name: spec for spec in TOOL_SPECS}


def volatility_of(name: str) -> Volatility:
    """Return how long ``name``'s results stay true.

    The lookup exists so a caller deciding what is still worth carrying forward — the agent loop,
    holding a finished turn's tool traffic — can ask the question without reaching into the specs
    table. An unknown name answers :attr:`Volatility.VOLATILE`, because the only safe assumption
    about a result whose provenance kedge cannot identify is that it has already expired.

    Args:
        name: The tool name, as the model asked for it.

    Returns:
        The tool's volatility, or :attr:`Volatility.VOLATILE` for a name this build does not offer.
    """
    spec = _SPECS_BY_NAME.get(name)
    return spec.volatility if spec is not None else Volatility.VOLATILE


# ── pending user decisions ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PendingDeletion:
    """A deletion the model asked for and the user has not confirmed."""

    cell: str
    reason: str
    descendants: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PendingAmendment:
    """A plan amendment the model proposed and the user has not approved."""

    rationale: str
    change: str
    stage: str | None = None


@dataclass(frozen=True, slots=True)
class PendingProposal:
    """A whole process plan the model authored and the user has not approved.

    The plan is assembled in full here — provenance and all — rather than at approval time,
    because the analysis it is a reading of is at hand in the tool and may not be by the time the
    user clicks. Assembling it is not writing it: nothing reaches
    :class:`~kedge.plan.store.PlanStore` until :mod:`kedge.server.routes` is asked to, and until
    then this is a proposal in memory that dies with the session if it is never approved.
    """

    plan: ProcessPlan


# ── context ──────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ToolContext:
    """The collaborators the tools work through. Every one of them is optional.

    Example:
        >>> ToolContext().caps.max_rows
        100
    """

    workspace: Workspace | None = None
    analysis: WorkbookAnalysis | None = None
    driver: NotebookBridge | None = None
    plans: PlanStore | None = None
    caps: Caps = field(default_factory=Caps)
    policy: Policy = field(default_factory=Policy)
    rounding: RoundingContext = field(default_factory=RoundingContext)
    knowledge_dir: Path | None = None
    utils_dir: Path | None = None
    redaction_patterns: tuple[str, ...] = ()
    hash_prefix_length: int = 12

    @classmethod
    def for_workspace(
        cls,
        workspace: Workspace,
        *,
        analysis: WorkbookAnalysis | None = None,
        driver: NotebookBridge | None = None,
        plans: PlanStore | None = None,
    ) -> ToolContext:
        """Build a context from a workspace, reading caps, redaction and ``[policy]`` from config.

        The allowlists are the only route by which anything ever reaches
        :class:`~kedge.agent.validate.Policy`, so a machine with no ``[policy]`` section gets the
        documented default: no network, no database.
        """
        from kedge.plan.store import PlanStore as _PlanStore

        config = workspace.config
        redaction = config.redaction
        return cls(
            workspace=workspace,
            analysis=analysis,
            driver=driver,
            plans=plans or _PlanStore.for_workspace(workspace),
            caps=Caps.from_config(config),
            policy=Policy(
                working_dir=workspace.project_dir,
                network_allowlist=frozenset(config.policy.network_allowlist),
                database_allowlist=frozenset(config.policy.database_allowlist),
            ),
            rounding=RoundingContext.from_analysis(analysis),
            knowledge_dir=_first_existing(workspace, "context"),
            utils_dir=_first_existing(workspace, "utils"),
            redaction_patterns=tuple(redaction.column_patterns) if redaction.enabled else (),
            hash_prefix_length=redaction.hash_prefix_length,
        )


def _first_existing(workspace: Workspace, name: str) -> Path | None:
    """Find ``context/`` or ``utils/`` beside the project, then in the current directory.

    Both are project-level conventions from PLAN M5 rather than workspace paths, so they are
    searched rather than derived. ``None`` means "not present", which every consumer treats as
    empty rather than as an error.
    """
    for candidate in (
        workspace.project_dir / name,
        workspace.workbook_path.parent / name,
        Path.cwd() / name,
    ):
        if candidate.is_dir():
            return candidate
    return None


# ── the registry ─────────────────────────────────────────────────────────────────────────────


class ToolRegistry:
    """Dispatches tool calls, enforces the caps, and writes the audit line.

    One instance per chat session. :meth:`refresh` is called at the top of every turn with the
    notebook state rebuilt from the kernel, so the validation gate and the tools are checking
    against what is actually there rather than against what the conversation remembers.

    The session cache hangs off the same instance and has the same lifetime, which is the whole
    reason it is safe: it holds only :attr:`Volatility.SESSION_STABLE` results, keyed on a
    fingerprint of the files those results were read from, so a repeat question is cheap rather
    than forbidden and a workbook saved from Excel mid-session answers freshly. The open workbook
    handle is held on the same terms and released the moment that fingerprint moves
    (:meth:`_workbook`), or the miss would only buy a second read of the file as it was.

    Example:
        >>> import asyncio
        >>> registry = ToolRegistry(ToolContext())
        >>> asyncio.run(registry.dispatch("get_plan", {})).ok
        False
    """

    def __init__(self, context: ToolContext, *, log: OutboundLog | None = None) -> None:
        self._context = context
        self._log = log
        self._state: NotebookState | None = None
        self._registry: NameRegistry | None = None
        self._handle: Any = None
        self._handle_fingerprint: str | None = None
        self._handle_failed = False
        self._model: str | None = None
        self._read: set[str] = set()
        self._cache: dict[str, ToolResult] = {}
        self._cache_bytes = 0
        self.pending_deletions: list[PendingDeletion] = []
        self.pending_amendments: list[PendingAmendment] = []
        self.pending_proposals: list[PendingProposal] = []

    # ── per-turn state ───────────────────────────────────────────────────────────────────

    @property
    def context(self) -> ToolContext:
        """The collaborators these tools work through."""
        return self._context

    @property
    def names(self) -> NameRegistry | None:
        """The live name registry, or ``None`` before the first refresh."""
        return self._registry

    @property
    def model(self) -> str | None:
        """The model id driving the turn, or ``None`` where nobody has said."""
        return self._model

    @property
    def has_read_workbook(self) -> bool:
        """Whether any :data:`WORKBOOK_READING_TOOLS` call has succeeded this session."""
        return bool(self._read)

    def refresh(self, state: NotebookState, *, model: str | None = None) -> None:
        """Adopt the per-turn facts, read fresh at the top of every turn.

        Both of these have exactly the lifetime of a turn, which is why they arrive together and
        why this is where they arrive. The notebook state was rebuilt from the kernel because the
        user edits cells between turns. The model id is the one the turn will actually run, which
        is not necessarily the one in config: it can be overridden per session and per request,
        and an artifact stamped with the configured id instead would be confidently wrong about
        the one forensic question a plan's provenance exists to answer.

        Args:
            state: The notebook as the kernel has it now.
            model: The model driving this turn. ``None`` leaves it unstamped, which is the honest
                answer — a guess here is worse than an absence, because it reads as a fact.
        """
        self._state = state
        self._registry = state.registry
        if model is not None:
            self._model = model

    def set_context(self, context: ToolContext) -> None:
        """Replace the collaborators, e.g. once marimo has come up mid-session."""
        self._context = context

    # ── dispatch ─────────────────────────────────────────────────────────────────────────

    async def dispatch(
        self,
        name: str,
        arguments: str | Mapping[str, Any],
        *,
        turn_id: str | None = None,
    ) -> ToolResult:
        """Run one tool call and return its capped, audited result.

        This is the only path from a tool to the model. It parses the arguments the model sent,
        dispatches, re-applies the payload cap to whatever came back, and writes one audit line
        describing the payload's shape — never its values.

        A session-stable tool asked the same question twice is answered from the session cache,
        which changes what the call costs and nothing else: the text is byte-identical and the
        audit line is written either way.

        Args:
            name: The tool name the model asked for.
            arguments: The JSON string the model produced, or an already-decoded mapping.
            turn_id: Recorded in the audit line so a payload can be traced to a turn.

        Returns:
            The result to hand back to the model. A failure is a result with ``ok=False``, not an
            exception: a tool that could not answer is something the model should read and react
            to, not something that should end the turn.
        """
        spec = _SPECS_BY_NAME.get(name)
        if spec is None:
            offered = ", ".join(tool_names())
            return self._finalise(
                name,
                ToolResult.note(
                    f"there is no tool called '{name}'. Available tools: {offered}.", ok=False
                ),
                turn_id,
            )

        try:
            parsed = _parse_arguments(arguments)
        except ValueError as exc:
            return self._finalise(name, ToolResult.note(str(exc), ok=False), turn_id)

        missing = [key for key in spec.required if parsed.get(key) in (None, "")]
        if missing:
            return self._finalise(
                name,
                ToolResult.note(
                    f"'{name}' needs {', '.join(missing)}, which the call did not provide.",
                    ok=False,
                ),
                turn_id,
            )

        cache_key = self._cache_key(spec, parsed)
        if cache_key is not None:
            remembered = self._cache.get(cache_key)
            if remembered is not None:
                logger.debug("tool %s answered from the session cache", name)
                # Back through _finalise rather than returned straight from here. Every payload
                # handed to the model is one line in the outbound log, and a log that grew holes
                # wherever a result came from memory would be a worse defect than the second read
                # it saved (SECURITY.md). The text is byte-identical, so the line is too.
                return self._finalise(name, remembered, turn_id)

        handler = getattr(self, f"_tool_{name}")
        try:
            result = await handler(parsed)
        except KedgeError as exc:
            logger.info("tool %s failed: %s", name, exc)
            result = ToolResult.note(f"{name} failed: {exc}", ok=False)
        except Exception as exc:
            logger.exception("tool %s raised", name)
            result = ToolResult.note(f"{name} could not run: {type(exc).__name__}: {exc}", ok=False)
        finalised = self._finalise(name, result, turn_id)
        if cache_key is not None and finalised.ok:
            # Only a success. A transient failure that pinned itself for the session would be a
            # tool that stays broken until the user restarts kedge.
            self._remember(cache_key, finalised)
        return finalised

    def _finalise(self, name: str, result: ToolResult, turn_id: str | None) -> ToolResult:
        """Apply the payload cap unconditionally, then record the payload's shape.

        The re-cap is the belt to :meth:`ToolResult.from_rows`'s braces. A handler that builds a
        result by hand cannot emit an uncapped payload, because it does not control this step.

        It is also where the session's reading is noted. The same argument that puts the audit
        line here puts this here: a payload cannot reach the model without passing through, so
        "has this session read the workbook?" is answered from the same evidence the outbound log
        is written from, including on the path where a result came back from the cache.
        """
        text, clipped = _cap_text(result.text, self._context.caps)
        capped = (
            result
            if not clipped
            else replace(
                result, text=text, truncated=True, summary=result.summary or _first_line(text)
            )
        )
        if capped.ok and name in WORKBOOK_READING_TOOLS:
            self._read.add(name)
        if self._log is not None:
            self._log.record(
                tool=name,
                ok=capped.ok,
                sheet=capped.sheet,
                columns=capped.columns,
                row_count=capped.row_count,
                byte_count=capped.byte_count,
                truncated=capped.truncated,
                omitted_rows=capped.omitted_rows,
                redacted_columns=capped.redacted_columns,
                turn_id=turn_id,
            )
        return capped

    # ── the session cache ────────────────────────────────────────────────────────────────

    def _cache_key(self, spec: ToolSpec, args: Mapping[str, Any]) -> str | None:
        """Build this call's cache key, or ``None`` when the call must not be cached at all.

        Four things go into it: the tool; its arguments rendered with their keys in a fixed order,
        because the model emits them in whatever order it likes and the same question asked twice
        has to land on the same key; the fingerprint of the files the answer is a function of; and
        the settings the payload was rendered under. That last part is short but not optional — a
        result read before a redaction pattern was added was rendered without it, and reissuing it
        afterwards would be kedge quietly unredacting a column.
        """
        if spec.volatility is not Volatility.SESSION_STABLE:
            return None
        fingerprint = self._fingerprint()
        if fingerprint is None:
            return None
        try:
            rendered = json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
        except TypeError:  # pragma: no cover - a caller-supplied mapping, never model JSON
            return None
        context = self._context
        settings = (
            f"{context.caps.max_rows}:{context.caps.max_payload_bytes}:"
            f"{context.hash_prefix_length}:{'|'.join(context.redaction_patterns)}"
        )
        return f"{spec.name}\x1f{fingerprint}\x1f{settings}\x1f{rendered}"

    def _fingerprint(self) -> str | None:
        """Identify the workbook and the analysis a session-stable result was read from.

        ``st_mtime_ns`` and ``st_size`` of every file such a result is a function of. A user who
        opens the workbook in Excel, changes a rate and saves it invalidates every cached row
        without knowing the cache exists — which is the only reason it is safe to hold rows read
        from a file kedge does not own.

        Returns ``None`` when nothing could be stat'd, and the caller must then not cache. A
        payload that cannot be shown to still be current is worth less than the read it saves.
        """
        analysis = self._context.analysis
        workspace = self._context.workspace
        sources: list[Path] = []
        if workspace is not None:
            sources += [workspace.workbook_path, workspace.analysis_path]
        if analysis is not None:
            sources.append(Path(analysis.workbook.path))

        parts: list[str] = []
        seen: set[str] = set()
        for source in sources:
            key = str(source)
            if key in seen:
                continue
            seen.add(key)
            try:
                stat = source.stat()
            except OSError:
                continue
            parts.append(f"{key}:{stat.st_mtime_ns}:{stat.st_size}")
        if analysis is not None:
            # The loaded analysis can be replaced in memory without analysis.json moving — a
            # re-run is a new answer to the same question, and the results quoting it are stale.
            parts.append(f"{analysis.workbook.sha256}:{analysis.generated_at.isoformat()}")
        return "|".join(parts) or None

    def _remember(self, key: str, result: ToolResult) -> None:
        """Hold a result for the session, evicting oldest-first to stay inside both bounds."""
        previous = self._cache.pop(key, None)
        if previous is not None:
            self._cache_bytes -= previous.byte_count
        self._cache[key] = result
        self._cache_bytes += result.byte_count
        while self._cache and (
            len(self._cache) > MAX_CACHED_RESULTS or self._cache_bytes > MAX_CACHED_BYTES
        ):
            oldest = next(iter(self._cache))
            self._cache_bytes -= self._cache.pop(oldest).byte_count

    async def aclose(self) -> None:
        """Release the workbook handle, if one was opened."""
        if self._handle is not None:
            await asyncio.to_thread(self._handle.close)
            self._handle = None
            self._handle_fingerprint = None

    # ── collaborators ────────────────────────────────────────────────────────────────────

    def _require_driver(self) -> NotebookBridge:
        driver = self._context.driver
        if driver is None:
            msg = (
                "there is no live marimo notebook attached to this workspace, so the notebook "
                "tools cannot run. Start kedge against the workbook so a kernel exists, then try "
                "again."
            )
            raise KedgeError(msg)
        return driver

    def _require_analysis(self) -> WorkbookAnalysis:
        analysis = self._context.analysis
        if analysis is None:
            msg = (
                "no workbook analysis is loaded for this workspace. Run `kedge inspect` on the "
                "workbook first; without it there are no facts to disclose and any translation "
                "would be a guess."
            )
            raise KedgeError(msg)
        return analysis

    def _require_workspace(self) -> Workspace:
        workspace = self._context.workspace
        if workspace is None:
            msg = "no workspace is attached, so the workbook cannot be read."
            raise KedgeError(msg)
        return workspace

    async def _workbook(self) -> Any:
        """Open the workbook lazily, and keep it only while it is still the same file.

        A :class:`~kedge.analysis.workbook.WorkbookHandle` is a zip archive and two openpyxl
        views read at open time, so a handle held across a save answers from the workbook as it
        was. That would make the session cache's invalidation theatre: the cache would miss on
        the new fingerprint, the handler would re-read the same already-open handle, and the
        model would pay to be told the identical stale thing twice.

        Which is not hypothetical, because it is the workflow kedge itself asks for. "Open the
        workbook in Excel, allow it to calculate, and save it" is the remedy
        :mod:`kedge.analysis.findings` prints, and the one ``reconcile`` returns for a workbook
        with no cached values — so the user who follows it and says "try again" is exactly the
        user who would be told the same failure about the file they have just fixed.

        The change is detected with :meth:`_fingerprint`, the same one the cache keys on, so
        there is one notion of "has the workbook changed" rather than two that can drift. No
        fingerprint means no reopen: an answer that cannot be compared is not evidence of a
        change, and reopening on every call would be the expensive reading of a silence.
        """
        fingerprint = self._fingerprint()
        if self._handle is not None:
            if fingerprint is None or fingerprint == self._handle_fingerprint:
                return self._handle
            return await self._reopen(fingerprint)
        if self._handle_failed:
            msg = "the workbook could not be opened earlier in this session; see the server log."
            raise KedgeError(msg)
        workspace = self._require_workspace()
        from kedge.analysis.workbook import open_workbook

        try:
            self._handle = await asyncio.to_thread(open_workbook, workspace.workbook_path)
        except Exception:
            self._handle_failed = True
            raise
        self._handle_fingerprint = fingerprint
        return self._handle

    async def _reopen(self, fingerprint: str) -> Any:
        """Replace a handle whose file has moved underneath it.

        The old handle is **closed**, not dropped.
        :meth:`~kedge.analysis.workbook.WorkbookHandle.close` releases three OS handles, and
        leaving them to the collector leaves them on a file the user is actively editing in
        Excel — on Windows that is worse than a leak. Closed off the event loop, for the same
        reason the open it mirrors is.

        It is closed *before* the replacement is opened, and the attribute is cleared first, so
        there is no path out of here that leaves a stale handle reachable. Serving the old file
        quietly is the one outcome that must not be possible: it is what this whole method
        exists to prevent.

        A reopen that fails does not set ``_handle_failed``. The file has just been written, so
        the likeliest cause is a write still in flight — Excel replaces the workbook rather than
        editing it in place, and there is a moment in the middle where it cannot be opened.
        Pinning the session on that would strand the user at the moment they had done what kedge
        asked. The next call finds no handle and goes through the ordinary open path above,
        which pins if it fails too; that is the existing mechanism doing its job one attempt
        later, rather than a second one invented here.

        Args:
            fingerprint: The current fingerprint, stamped on the new handle so the next call
                compares against what was actually opened.

        Returns:
            The fresh handle.

        Raises:
            KedgeError: The workbook changed and the new version could not be opened. Carries
                what the model needs to know, which is mostly that its earlier reads describe
                the previous version and must not be quoted as current.
        """
        stale = self._handle
        self._handle = None
        self._handle_fingerprint = None
        logger.info("the workbook changed on disk; reopening it")
        await asyncio.to_thread(stale.close)

        workspace = self._require_workspace()
        from kedge.analysis.workbook import open_workbook

        try:
            handle = await asyncio.to_thread(open_workbook, workspace.workbook_path)
        except Exception as exc:
            logger.warning("the workbook changed on disk and would not reopen: %s", exc)
            msg = (
                f"the workbook changed on disk during this session and the new version could "
                f"not be opened ({exc}). The copy kedge had been reading was released rather "
                f"than reused, so nothing is being answered from the old file — but everything "
                f"you read from this workbook earlier describes the version before that change, "
                f"and must not be quoted as current. If the user is part-way through saving "
                f"{workspace.workbook_path.name} from Excel, wait for that to finish and read "
                f"again; otherwise tell them kedge can no longer open it, and say what you were "
                f"trying to read."
            )
            raise KedgeError(msg) from exc
        self._handle = handle
        self._handle_fingerprint = fingerprint
        return handle

    # ── notebook tools ───────────────────────────────────────────────────────────────────

    async def _tool_list_cells(self, args: Mapping[str, Any]) -> ToolResult:
        driver = self._require_driver()
        target = args.get("cell")
        with_code = bool(args.get("with_code", True))
        if target:
            cell = await driver.get_cell(str(target))
            body = cell.code or "(no source was returned)"
            text = (
                f"{cell.name or cell.id} ({cell.id}) status={cell.status or 'unknown'}\n\n"
                f"```python\n{body}\n```"
            )
            return ToolResult.note(text, summary=f"read cell {cell.name or cell.id}")

        cells = await driver.list_cells(with_code=with_code)
        known = {cell.id: cell for cell in (self._state.cells if self._state else ())}
        lines: list[str] = []
        for position, cell in enumerate(cells, start=1):
            node = known.get(cell.id)
            defines = ", ".join(node.defs) if node and node.defs else "-"
            refs = ", ".join(node.refs) if node and node.refs else "-"
            lines.append(
                f"{position}. {cell.name or cell.id} ({cell.id}) "
                f"[{cell.status or 'unknown'}] defines: {defines} | reads: {refs}"
            )
            if with_code and cell.code:
                lines.append(f"```python\n{cell.code}\n```")
        text = "\n".join(lines) or "the notebook has no cells yet."
        return ToolResult.note(text, summary=f"{len(cells)} cell(s)", caps=self._context.caps)

    async def _refuse_without_an_approved_plan(self) -> ToolResult | None:
        """Refuse a write to the notebook that no approved plan authorises, or return None.

        The tools' half of the review gate. :func:`kedge.notebook.scaffold.scaffold_notebook`
        refuses an unapproved plan structurally and with no override, but it is not the only way a
        cell is written: ``propose_cell`` and ``edit_cell`` reach the same kernel, and until this
        existed the only thing between a plan the user had declined and forty cells implementing it
        was prose in the system prompt. A control that matters is refused rather than discouraged
        here, the same way the row caps, the validation gate and ``propose_plan``'s own refusals
        are (PLAN 2.2).

        ``latest_approved() is None`` is the predicate because that is what "a plan is in force"
        means everywhere else in kedge -- the scaffolder, ``get_plan``, ``propose_plan``'s
        already-approved refusal and the server's plan panel all read exactly this. No store at all
        is the same answer for a stronger reason: nothing can have been approved.

        A store that will not read raises :class:`~kedge.errors.KedgeError` out of here and
        :meth:`dispatch` turns it into a failed result. That is the same outcome by a different
        route, which is the point: there is no path through this that ends in a cell being written.

        Returns:
            The refusal to hand back, or ``None`` when a plan is in force and the caller may
            proceed.
        """
        store = self._context.plans
        approved = None if store is None else await asyncio.to_thread(store.latest_approved)
        if approved is not None:
            return None
        opening = (
            "no plan store is attached to this workspace, so nothing can have been approved"
            if store is None
            else "no plan has been approved for this workbook"
        )
        return ToolResult.note(
            f"refused: {opening}, and nothing is written to the notebook before the plan is "
            f"approved. That review is what the plan is for: it is where the decomposition is "
            f"corrected while correcting it is still minutes rather than an afternoon of reading "
            f"cells backwards. Work out what this workbook does, stage by stage, and send that "
            f"account through `propose_plan` for the user to approve; if you have already proposed "
            f"one, it is with them and the answer is to say so and wait, not to propose it again. "
            f"Nothing about resending this cell will change this — it is a decision the user has "
            f"yet to make, not a failure to retry.",
            ok=False,
            summary="refused: no approved plan is in force",
        )

    async def _tool_propose_cell(self, args: Mapping[str, Any]) -> ToolResult:
        driver = self._require_driver()
        # Ordered behind the driver and ahead of the validation gate. The driver is whether kedge
        # can write a cell at all, which no amount of planning changes and which the model cannot
        # fix; the plan is whether it may; validation is whether this particular body is fit to.
        # Most fundamental first, and the same order `propose_plan` refuses in: the collaborator,
        # then the standing, then the contents.
        refusal = await self._refuse_without_an_approved_plan()
        if refusal is not None:
            return refusal
        name = str(args["name"])
        code = str(args["code"])
        report = self._validate(code, cell=None)
        if not report.ok:
            return _rejected(report)

        from kedge.notebook.driver import MultiplyDefinedError

        try:
            mutation = await driver.create_cell(
                code,
                name=name,
                before=_optional_str(args.get("before")),
                after=_optional_str(args.get("after")),
                run=bool(args.get("run", True)),
            )
        except MultiplyDefinedError as exc:
            return _kernel_rejected(exc)
        return _mutation_result(mutation, code=code, verb="created", validated=True)

    async def _tool_edit_cell(self, args: Mapping[str, Any]) -> ToolResult:
        driver = self._require_driver()
        # Gated on the same terms as `propose_cell`: replacing a cell's whole body is authoring
        # logic, and a rewrite is exactly how a declined decomposition would arrive one cell at a
        # time. `run_cell` and `delete_cell` are not gated -- re-running existing code writes no
        # new logic, and a deletion already stops and asks the user.
        refusal = await self._refuse_without_an_approved_plan()
        if refusal is not None:
            return refusal
        target = str(args["cell"])
        code = str(args["code"])
        report = self._validate(code, cell=target)
        if not report.ok:
            return _rejected(report)

        from kedge.notebook.driver import MultiplyDefinedError, StaleCellError

        try:
            mutation = await driver.edit_cell(
                target, code, run=bool(args.get("run", True)), overwrite_unread=False
            )
        except StaleCellError as exc:
            return ToolResult.note(
                f"{exc}\n\nThe user changed this cell since it was last read. Call `list_cells` "
                f"with cell='{target}' to see what they wrote, and decide again with that in "
                f"front of you rather than resubmitting the same body.",
                ok=False,
                summary="refused: the cell is stale",
            )
        except MultiplyDefinedError as exc:
            return _kernel_rejected(exc)
        return _mutation_result(mutation, code=code, verb="edited", validated=True)

    async def _tool_run_cell(self, args: Mapping[str, Any]) -> ToolResult:
        driver = self._require_driver()
        mutation = await driver.run_cell(str(args["cell"]))
        return _mutation_result(mutation, code=None, verb="ran")

    async def _tool_delete_cell(self, args: Mapping[str, Any]) -> ToolResult:
        target = str(args["cell"])
        reason = str(args["reason"])
        descendants: tuple[str, ...] = ()
        if self._state is not None:
            owner = next(
                (cell for cell in self._state.cells if target in (cell.id, cell.name)), None
            )
            if owner is not None:
                defined = set(owner.defs)
                descendants = tuple(
                    cell.label
                    for cell in self._state.cells
                    if cell.id != owner.id and defined & set(cell.refs)
                )
        self.pending_deletions.append(
            PendingDeletion(cell=target, reason=reason, descendants=descendants)
        )
        depends = (
            f" {len(descendants)} cell(s) read names it defines: {', '.join(descendants)}."
            if descendants
            else " Nothing else reads what it defines."
        )
        return ToolResult.note(
            f"Deletion of '{target}' has been put to the user for confirmation and has NOT "
            f"happened.{depends} Tell them plainly what would be lost and why you want it gone, "
            f"and carry on with the rest of the work. If the cell only needs different contents, "
            f"use `edit_cell` instead — that keeps its identity and its place in the graph.",
            ok=False,
            summary=f"deletion of '{target}' awaits the user's confirmation",
        )

    def _validate(self, code: str, *, cell: str | None) -> ValidationReport:
        return validate_cell(
            code,
            registry=self._registry,
            cell=cell,
            policy=self._context.policy,
            rounding=self._context.rounding,
        )

    # ── workbook tools ───────────────────────────────────────────────────────────────────

    async def _tool_inspect_workbook(self, args: Mapping[str, Any]) -> ToolResult:
        analysis = self._require_analysis()
        section = str(args["section"])
        sheet = _optional_str(args.get("sheet"))
        renderer = _SECTIONS.get(section)
        if renderer is None:
            return ToolResult.note(
                f"'{section}' is not a section of the analysis. Choose one of: "
                f"{', '.join(sorted(_SECTIONS))}.",
                ok=False,
            )
        text = renderer(analysis, sheet, _optional_str(args.get("operation")))
        return ToolResult.note(text, summary=f"analysis: {section}", caps=self._context.caps)

    async def _tool_sample_data(self, args: Mapping[str, Any]) -> ToolResult:
        analysis = self._context.analysis
        sheet = str(args["sheet"])
        handle = await self._workbook()
        info = analysis.sheet(sheet) if analysis is not None else None
        header_row = info.header_row if info and info.header_row else _DEFAULT_HEADER_ROW
        width = min(
            info.max_column if info and info.max_column else _MAX_SAMPLE_COLUMNS,
            _MAX_SAMPLE_COLUMNS,
        )
        total_rows = info.max_row if info and info.max_row else None

        wanted = int(args.get("rows") or self._context.caps.max_rows)
        wanted = max(1, min(wanted, self._context.caps.max_rows))
        start = int(args.get("start_row") or header_row + 1)
        last_column = _column_letters(max(1, width))

        headers = await asyncio.to_thread(
            handle.read_range, sheet, f"A{header_row}:{last_column}{header_row}"
        )
        labels = _header_labels(headers[0] if headers else [], width)
        rows = await asyncio.to_thread(
            handle.read_range, sheet, f"A{start}:{last_column}{start + wanted - 1}"
        )
        if not rows:
            return ToolResult.note(
                f"no rows at {sheet}!A{start} — the sheet has "
                f"{total_rows if total_rows is not None else 'an unknown number of'} rows.",
                sheet=sheet,
            )

        keep = _selected_columns(labels, args.get("columns"))
        labels, rows, redacted = self._project(labels, rows, keep)
        remaining = None if total_rows is None else max(0, total_rows - (start - 1))
        return ToolResult.from_rows(
            rows,
            columns=labels,
            caps=self._context.caps,
            header=f"{sheet}: rows {start}..{start + len(rows) - 1}, header on row {header_row}",
            sheet=sheet,
            total=remaining,
            redacted_columns=redacted,
        )

    async def _tool_read_range(self, args: Mapping[str, Any]) -> ToolResult:
        sheet = str(args["sheet"])
        a1 = str(args["range"])
        view = str(args.get("view") or "values")
        if view not in ("values", "formulas"):
            return ToolResult.note("view must be 'values' or 'formulas'.", ok=False)
        handle = await self._workbook()
        rows = await asyncio.to_thread(
            handle.read_range,
            sheet,
            a1,
            view=view,
            max_rows=self._context.caps.max_rows,
        )
        if not rows:
            return ToolResult.note(
                f"{sheet}!{a1} is empty, or the sheet or range does not exist. Check the sheet "
                f"names in the analysis summary.",
                sheet=sheet,
            )
        first_column, requested = _range_shape(a1)
        labels = [_column_letters(first_column + index) for index in range(len(rows[0]))]
        return ToolResult.from_rows(
            rows,
            columns=labels,
            caps=self._context.caps,
            header=f"{sheet}!{a1} ({view})",
            sheet=sheet,
            total=requested,
        )

    async def _tool_profile_column(self, args: Mapping[str, Any]) -> ToolResult:
        analysis = self._require_analysis()
        sheet = str(args["sheet"])
        column = str(args["column"]).strip()
        wanted = column.upper()
        profile = next(
            (
                item
                for item in analysis.profiles
                if item.sheet == sheet
                and (
                    item.column.upper() == wanted
                    or (item.header or "").strip().lower() == column.lower()
                )
            ),
            None,
        )
        if profile is None:
            known = ", ".join(
                f"{item.column}={item.header or '(no header)'}"
                for item in analysis.profiles
                if item.sheet == sheet
            )
            return ToolResult.note(
                f"no profile for {sheet}!{column}. Profiled columns on that sheet: "
                f"{known or 'none'}. Use `read_range` for a column the analyser did not profile.",
                ok=False,
                sheet=sheet,
            )
        return ToolResult.note(
            _render_profile(profile),
            summary=f"profile of {sheet}!{profile.column}",
            sheet=sheet,
            columns=(profile.header or profile.column,),
            row_count=profile.row_count,
            redacted_columns=1 if profile.redacted else 0,
            caps=self._context.caps,
        )

    async def _tool_probe(self, args: Mapping[str, Any]) -> ToolResult:
        driver = self._require_driver()
        code = str(args["code"])
        if len(code) > _PROBE_CODE_LIMIT:
            return ToolResult.note(
                f"the probe is {len(code)} characters; keep it under {_PROBE_CODE_LIMIT}. A probe "
                f"asks one question — if it needs to be this long it wants to be a cell.",
                ok=False,
            )
        result = await driver.probe(code)
        if not result.ok:
            return ToolResult.note(
                f"the probe raised:\n{result.error or 'no error text was returned'}",
                ok=False,
                summary="probe failed",
            )
        console = result.stdout.strip()
        block = f"stdout:\n{console}" if console else ""
        if result.value_repr is None:
            # Nothing was bound, so whatever the code printed is the only result there is. Say how
            # to ask again rather than reporting an empty answer the model cannot act on.
            return ToolResult.note(
                f"{_PROBE_NO_VALUE}\n\n{block}" if console else _PROBE_NO_VALUE,
                ok=bool(console),
                summary="probe printed output but returned no value"
                if console
                else "probe returned nothing",
                row_count=1 if console else 0,
                caps=self._context.caps,
            )
        if result.value_type == "NoneType" and console:
            # A probe ending in print(...) binds None. The printed text is the real answer, so it
            # leads; burying it under a "type: NoneType" header reads as an empty result.
            return ToolResult.note(
                f"{block}\n\n{_PROBE_PRINTED}",
                summary="probe printed output",
                row_count=1,
                caps=self._context.caps,
            )
        marker = "  [value repr was truncated by the kernel]" if result.truncated else ""
        tail = f"\n\n{block}" if console else ""
        return ToolResult.note(
            f"type: {result.value_type}{marker}\n{result.value_repr}{tail}",
            summary=f"probe returned {result.value_type}",
            row_count=1,
            caps=self._context.caps,
        )

    # ── context tools ────────────────────────────────────────────────────────────────────

    async def _tool_get_plan(self, args: Mapping[str, Any]) -> ToolResult:
        del args
        store = self._context.plans
        if store is None:
            return ToolResult.note(
                "no plan store is attached to this workspace.", ok=False, summary="no plan store"
            )
        plan = await asyncio.to_thread(store.latest_approved)
        state = "approved"
        if plan is None:
            plan = await asyncio.to_thread(store.latest)
            state = "proposed but NOT approved"
        if plan is None:
            return ToolResult.note(
                "no process plan exists for this workbook yet, so you have no standing "
                "instructions and nothing should be written to the notebook. The planning step is "
                "yours: work out what this workbook does, stage by stage, then send that account "
                "through `propose_plan` for the user to approve. That is the only way a plan gets "
                "written, so do not tell the user to run a command instead. Improvising a "
                "decomposition cell by cell is what the plan exists to prevent.",
                ok=False,
                summary="no plan",
            )
        from kedge.agent.context import build_plan_block

        return ToolResult.note(
            f"This plan is {state}.\n\n{build_plan_block(plan)}",
            summary=f"plan v{plan.version} ({state})",
            caps=self._context.caps,
        )

    async def _tool_propose_plan(self, args: Mapping[str, Any]) -> ToolResult:
        """Record a whole proposed plan for the user to approve. Volatile, and deliberately so.

        Nothing about this may be remembered: it has a side effect, its refusal depends on what is
        in the plan store at the moment it is called, and a second proposal answered from a cache
        would be a proposal that never reached the user.

        Four refusals guard it, and only the last is about the plan's contents. It is the one
        tool on this surface that turns a conversation into a large, structured, confident-looking
        artifact, and it is offered on every one of up to ``max_steps`` completions — so the
        things that must not happen are refused rather than discouraged, which is how the row
        caps, the validation gate and the scaffolder's approval check are all built. A plan may
        not replace one already approved (that is ``amend_plan``'s review, walked round); it may
        not be written for a workbook triage returned ``STOP`` for, which is what
        :func:`kedge.plan.propose.propose_plan` refuses on the CLI path; it may not be written by
        a session that has read nothing (:data:`WORKBOOK_READING_TOOLS`); and it may not be larger
        than a notebook anyone will read (:data:`MAX_PROPOSED_STAGES`).
        """
        from pydantic import ValidationError

        from kedge.plan.model import PlanError, ProcessPlan
        from kedge.plan.propose import describe_errors, parse_draft
        from kedge.plan.triage import triage

        store = self._context.plans
        if store is None:
            return ToolResult.note(
                "no plan store is attached to this workspace, so there is nowhere for a proposed "
                "plan to go.",
                ok=False,
                summary="no plan store",
            )

        # The gate, and the reason this tool is not a way around amendment review. `amend_plan`
        # exists so that a change to an approved plan is reviewed as a change; a whole replacement
        # proposed here would be reviewed as a fresh plan, which is a quieter decision about a
        # louder edit. The condition is "any version has been approved", because that is exactly
        # when a plan is in force — `latest_approved` is what the scaffolder and `get_plan` read.
        approved = await asyncio.to_thread(store.latest_approved)
        if approved is not None:
            return ToolResult.note(
                f"refused: plan v{approved.version} is already approved for this workbook, and "
                f"`propose_plan` only authors a first plan. Proposing a replacement would put a "
                f"whole new decomposition past the review that every change to an approved plan "
                f"goes through. Use `amend_plan` for what you want changed, one change at a time, "
                f"with the rationale the user needs to judge it.",
                ok=False,
                summary=f"refused: plan v{approved.version} is already approved",
            )

        # Ahead of the read gate, because without an analysis there is nothing to read: telling
        # the model to go and read the workbook when kedge has not looked at it either would be
        # a dead end, and "run `kedge inspect`" is the answer to both.
        analysis = self._require_analysis()

        # The most fundamental of the refusals, and the only one about the workbook rather than
        # about this session. `propose.propose_plan` raises `ProposalRefusedError` on a STOP
        # rather than spend a model call on it, and the verdict has to mean the same thing when it
        # is reached through the chat: without this, a workbook kedge would decline to convert is
        # planned here, approved in the web UI, and the word "stop" never reaches the user.
        # Ahead of the read gate, because reading cannot change the verdict — sending the model
        # off through four tools and refusing it when it came back would spend the turn and imply
        # the answer was ever going to be different. Behind the approved-plan refusal, which needs
        # no analysis and points at `amend_plan`: a workbook already being converted is past the
        # question this one asks.
        triage_result = triage(analysis)
        if triage_result.should_stop:
            return ToolResult.note(
                f"refused: kedge's triage recommends against converting this workbook, so no plan "
                f"was authored. Triage is deterministic, so reading more of the workbook will not "
                f"change it.\n\n"
                f"{triage_result.explain()}\n\n"
                f"Do not write the plan out in prose instead. Say plainly to the user why this "
                f"workbook is not a candidate, quoting the blockers above, and leave the decision "
                f"to override with them.",
                ok=False,
                summary=f"refused: triage says stop ({triage_result.convertible:.2f} convertible)",
            )

        if not self._read:
            # Structural, not a reminder. Every other control in this project is — the row caps,
            # the validation gate, the scaffolder's approval check — and exhortation is the
            # weakest possible guard on the one tool that can manufacture a confident-looking
            # decomposition out of nothing.
            return ToolResult.note(
                "refused: you have read nothing about this workbook yet, and a plan written from "
                "the analysis summary alone is a guess with a confident tone. The summary carries "
                "the order and purpose of the stages and nothing else — not a value, a dtype, a "
                "count, or whether a key is unique, and each of those goes silently wrong in its "
                "own way. Read the workbook first with `inspect_workbook`, `sample_data`, "
                "`profile_column` or `read_range`, then propose the plan you can defend.",
                ok=False,
                summary="refused: nothing has been read about this workbook",
            )

        assessment = triage_result.as_assessment()
        try:
            draft = parse_draft(_as_json_text(args["plan"]), assessment=assessment)
        except ValidationError as exc:
            return ToolResult.note(
                f"the plan did not validate, so nothing was recorded. Fix these and call "
                f"`propose_plan` again:\n{describe_errors(exc)}",
                ok=False,
                summary="proposed plan did not validate",
                draft_rejected=True,
                caps=self._context.caps,
            )
        except PlanError as exc:
            return ToolResult.note(
                f"the plan could not be read, so nothing was recorded: {exc}. The `plan` argument "
                f"is one JSON object with `stages` and `open_questions`, and nothing else around "
                f"it.",
                ok=False,
                summary="proposed plan could not be read",
                draft_rejected=True,
                caps=self._context.caps,
            )

        if len(draft.stages) > MAX_PROPOSED_STAGES:
            return ToolResult.note(
                f"the plan has {len(draft.stages)} stages and the ceiling is "
                f"{MAX_PROPOSED_STAGES}, so nothing was recorded. One stage becomes one cell, and "
                f"a notebook nobody can hold in their head is no improvement on a spreadsheet "
                f"nobody can hold in their head. Group the mechanical steps into the business "
                f"stages a reviewer would name — a fill-down is one operation, not one per row. "
                f"If this workbook genuinely runs more than {MAX_PROPOSED_STAGES} distinct "
                f"stages, say so in prose: it is two processes and wants two notebooks.",
                ok=False,
                summary=f"refused: {len(draft.stages)} stages exceeds {MAX_PROPOSED_STAGES}",
                draft_rejected=True,
                caps=self._context.caps,
            )

        existing = await asyncio.to_thread(store.latest)
        plan = ProcessPlan.from_analysis_draft(
            draft,
            analysis,
            generated_by="llm",
            # The model the turn is actually running, stamped by `refresh`, not the one in config
            # — those differ the moment a session or a request overrides it, and a plan is read
            # months later by someone asking which model wrote it and whether it is still trusted.
            # An absent id sends them looking; a wrong one stops them looking.
            llm_model=self._model,
            version=await asyncio.to_thread(store.next_version),
            based_on_version=existing.version if existing is not None else None,
        )
        self.pending_proposals.append(PendingProposal(plan=plan))
        supersedes = (
            f" It would supersede the unapproved v{existing.version} on disk." if existing else ""
        )
        return ToolResult.note(
            f"The plan has been recorded and put to the user for approval: {len(plan.stages)} "
            f"stage(s), {len(plan.open_questions)} open question(s), {len(plan.dropped)} dropped "
            f"range(s).{supersedes} It is NOT in force — nothing has been written to the plan "
            f"store and you have no standing instructions until they approve it, so write no "
            f"cells against it yet. Set out in your reply what the process does stage by stage "
            f"and what you are unsure of, so they are reviewing your reading of the workbook "
            f"rather than a list of slugs. kedge's own triage scored this workbook "
            f"{assessment.convertible:.2f} convertible; that figure is not yours to set.",
            summary=f"plan proposed: {len(plan.stages)} stage(s), awaiting approval",
            caps=self._context.caps,
        )

    async def _tool_amend_plan(self, args: Mapping[str, Any]) -> ToolResult:
        amendment = PendingAmendment(
            rationale=str(args["rationale"]),
            change=str(args["change"]),
            stage=_optional_str(args.get("stage")),
        )
        self.pending_amendments.append(amendment)
        where = f" to stage '{amendment.stage}'" if amendment.stage else ""
        return ToolResult.note(
            f"The amendment{where} has been recorded and put to the user for approval. It is NOT "
            f"in force: the approved plan is unchanged and remains your standing instructions "
            f"until they approve it, the same way the original plan was approved. Say plainly in "
            f"your reply what you found and what you are proposing, and work to the current plan "
            f"meanwhile.",
            summary=f"amendment proposed{where}",
        )

    async def _tool_reconcile(self, args: Mapping[str, Any]) -> ToolResult:
        return await _reconcile(self._context, args)

    async def _tool_list_utils(self, args: Mapping[str, Any]) -> ToolResult:
        directory = self._context.utils_dir
        if directory is None:
            return ToolResult.note(
                "this project has no utils package yet, so there is nothing to reuse. Write the "
                "logic in the cell; if it turns out to be worth sharing, say so and the user can "
                "lift it into utils/.",
                summary="no utils package",
            )
        from kedge.knowledge import build_catalogue

        catalogue = await asyncio.to_thread(build_catalogue, directory)
        if catalogue.is_empty:
            return ToolResult.note(
                f"the utils package at {directory} is empty. Write the logic in the cell.",
                summary="utils catalogue is empty",
            )
        query = _optional_str(args.get("query"))
        text = catalogue.render()
        if query:
            needle = query.lower()
            matched = [
                function.render()
                for function in catalogue.functions
                if needle in function.qualified.lower() or needle in function.summary.lower()
            ]
            text = "\n".join(matched) or f"no util matches '{query}'.\n\n{text}"
        return ToolResult.note(
            text, summary=f"{len(catalogue.functions)} util(s)", caps=self._context.caps
        )

    async def _tool_get_knowledge(self, args: Mapping[str, Any]) -> ToolResult:
        directory = self._context.knowledge_dir
        if directory is None:
            return ToolResult.note(
                "this project has no knowledge pack, so there is no documented table grain, join "
                "key or convention to draw on. Work from the workbook analysis, and where the "
                "grain of something matters, ask the user rather than assuming it.",
                summary="no knowledge pack",
            )
        from kedge.knowledge import get_knowledge, load_pack

        pack = await asyncio.to_thread(load_pack, directory)
        if pack.is_empty:
            return ToolResult.note(
                f"the knowledge pack at {directory} is empty.", summary="knowledge pack is empty"
            )
        text = get_knowledge(pack, _optional_str(args.get("key")))
        return ToolResult.note(text, summary="knowledge pack", caps=self._context.caps)

    # ── redaction ────────────────────────────────────────────────────────────────────────

    def _project(
        self,
        labels: Sequence[str],
        rows: Sequence[Sequence[Any]],
        keep: Sequence[int],
    ) -> tuple[list[str], list[list[Any]], int]:
        """Select columns and hash the values of any that redaction covers."""
        from kedge.analysis.redact import hash_value, should_redact

        patterns = self._context.redaction_patterns
        chosen = [labels[index] for index in keep]
        masked = [bool(patterns) and should_redact(label, patterns) for label in chosen]
        length = self._context.hash_prefix_length
        projected: list[list[Any]] = []
        for row in rows:
            built: list[Any] = []
            for position, index in enumerate(keep):
                value = row[index] if index < len(row) else None
                built.append(hash_value(value, prefix_length=length) if masked[position] else value)
            projected.append(built)
        return chosen, projected, sum(masked)


def _parse_arguments(arguments: str | Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, str):
        return dict(arguments)
    text = arguments.strip()
    if not text:
        return {}
    try:
        # strict=False permits raw newlines and tabs inside string values. Models emit cell
        # bodies that way often enough that refusing them would waste a round trip on a
        # difference the model cannot see and did not intend.
        decoded = json.loads(text, strict=False)
    except json.JSONDecodeError as exc:
        msg = f"the tool arguments were not valid JSON ({exc}). Send a JSON object."
        raise ValueError(msg) from exc
    except RecursionError as exc:
        # Deep nesting, not bad syntax. `RecursionError` is not a `JSONDecodeError`, so left
        # uncaught it goes straight past `dispatch`'s handler and out of the loop's catch-all,
        # where the user is told the turn failed fatally over one malformed tool call.
        msg = "the tool arguments were nested too deeply to decode. Send a flatter JSON object."
        raise ValueError(msg) from exc
    if not isinstance(decoded, dict):
        msg = f"the tool arguments must be a JSON object, not {type(decoded).__name__}."
        raise ValueError(msg)
    return decoded


def _as_json_text(value: Any) -> str:
    """Render a tool argument documented as "a JSON object" back into JSON text.

    Models emit an object where a description says "object" regardless of the schema declaring a
    string, and both readings of the instruction are defensible. ``str()`` on the decoded object
    would produce a Python repr — single quotes, ``None``, ``True`` — and the parser would then
    tell the model its JSON was invalid at the first property name, which is both false and
    unactionable: its JSON was fine and there is nothing in the message for it to fix.

    Args:
        value: Whatever arrived under the argument, decoded.

    Returns:
        ``value`` if it is already text, otherwise its JSON serialisation.
    """
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value)
    except (TypeError, ValueError):  # pragma: no cover - model arguments are always serialisable
        return str(value)


def _optional_str(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _column_letters(index: int) -> str:
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _range_shape(a1: str) -> tuple[int, int | None]:
    """Return the first column index and the row count an A1 range asked for.

    The row count is what makes the omission marker honest: ``read_range`` clamps a wide request
    kernel-side, so without knowing what was asked for kedge would report nothing omitted.
    """
    from kedge.analysis.workbook import parse_a1_range

    bounds = parse_a1_range(a1)
    if bounds is None:
        return 1, None
    min_row, min_col, max_row, _ = bounds
    return min_col, max(0, max_row - min_row + 1)


def _header_labels(row: Sequence[Any], width: int) -> list[str]:
    labels: list[str] = []
    for position in range(width):
        value = row[position] if position < len(row) else None
        text = " ".join(str(value).split()) if value is not None else ""
        labels.append(text or _column_letters(position + 1))
    return labels


def _selected_columns(labels: Sequence[str], wanted: Any) -> list[int]:
    if not wanted:
        return list(range(len(labels)))
    requested = [str(item).strip().lower() for item in wanted if str(item).strip()]
    chosen = [
        index
        for index, label in enumerate(labels)
        if label.lower() in requested or _column_letters(index + 1).lower() in requested
    ]
    return chosen or list(range(len(labels)))


def _rejected(report: ValidationReport) -> ToolResult:
    return ToolResult(
        text=report.render(),
        ok=False,
        summary=f"rejected by the validation gate ({len(report.violations)})",
        violations=report.messages,
        validated=False,
    )


def _kernel_rejected(exc: BaseException) -> ToolResult:
    violations = violations_from_kernel_error(exc)
    body = "\n".join(f"  - {line}" for line in violations)
    return ToolResult(
        text=f"marimo refused this cell, so nothing was applied:\n{body}",
        ok=False,
        summary=f"rejected by marimo ({len(violations)})",
        violations=violations,
        validated=False,
    )


def _mutation_result(
    mutation: Any, *, code: str | None, verb: str, validated: bool | None = None
) -> ToolResult:
    reference = mutation.cell
    label = (reference.name or reference.id) if reference is not None else "the cell"
    errors = "; ".join(error.message for error in mutation.errors)
    status = mutation.status or "unknown"
    text = f"{verb} {label} (status {status})."
    if errors:
        text += f"\nThe cell reported errors: {errors}"
    if mutation.output_preview:
        text += f"\noutput: {mutation.output_preview}"
    if mutation.descendants:
        text += f"\ncells downstream: {', '.join(mutation.descendants)}"
    return ToolResult(
        text=text,
        ok=mutation.ok,
        summary=f"{verb} {label}",
        validated=validated,
        cell_id=reference.id if reference is not None else None,
        cell_name=reference.name if reference is not None else None,
        cell_preview=_preview(code),
        cell_ran=bool(mutation.ran),
        cell_ok=mutation.ok,
        cell_error=errors or None,
    )


def _preview(code: str | None, lines: int = 4) -> str:
    if not code:
        return ""
    head = code.strip().splitlines()[:lines]
    return "\n".join(head)


# ── analysis rendering ───────────────────────────────────────────────────────────────────────


def _dump(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str, ensure_ascii=False)


def _sheets(analysis: WorkbookAnalysis, sheet: str | None, _operation: str | None) -> str:
    chosen = [item for item in analysis.sheets if sheet is None or item.name == sheet]
    return _dump([item.model_dump(mode="json") for item in chosen])


def _operations(analysis: WorkbookAnalysis, sheet: str | None, _operation: str | None) -> str:
    chosen = [item for item in analysis.operations if sheet is None or item.sheet == sheet]
    return _dump(
        [
            {
                "id": item.id,
                "sheet": item.sheet,
                "anchor": item.anchor,
                "ranges": item.ranges,
                "cell_count": item.cell_count,
                "r1c1": item.r1c1,
                "sample_a1": item.sample_a1,
                "functions": item.functions,
                "pattern": item.excel_pattern.value,
                "description": item.description,
                "translation_hint": item.translation_hint,
                "cached_values_present": item.cached_values_present,
                "inconsistencies": len(item.inconsistencies),
            }
            for item in chosen
        ]
    )


def _operation(analysis: WorkbookAnalysis, _sheet: str | None, operation: str | None) -> str:
    if operation is None:
        return "the 'operation' section needs an operation id. Use the 'operations' section first."
    found = analysis.operation(operation)
    if found is None:
        available = ", ".join(item.id for item in analysis.operations[:40]) or "none"
        return f"no operation '{operation}'. Known ids: {available}."
    return _dump(found.model_dump(mode="json"))


def _findings(analysis: WorkbookAnalysis, sheet: str | None, _operation: str | None) -> str:
    chosen = [item for item in analysis.findings if sheet is None or item.sheet == sheet]
    return _dump([item.model_dump(mode="json") for item in chosen])


def _named_ranges(analysis: WorkbookAnalysis, _sheet: str | None, _operation: str | None) -> str:
    return _dump([item.model_dump(mode="json") for item in analysis.named_ranges])


def _connections(analysis: WorkbookAnalysis, _sheet: str | None, _operation: str | None) -> str:
    return _dump(
        {
            "status": analysis.connections_status.value,
            "connections": [item.model_dump(mode="json") for item in analysis.connections],
        }
    )


def _power_query(analysis: WorkbookAnalysis, _sheet: str | None, _operation: str | None) -> str:
    return _dump(analysis.power_query.model_dump(mode="json"))


def _notes(analysis: WorkbookAnalysis, _sheet: str | None, _operation: str | None) -> str:
    return _dump([item.model_dump(mode="json") for item in analysis.notes])


def _graph(analysis: WorkbookAnalysis, _sheet: str | None, _operation: str | None) -> str:
    return _dump(analysis.graph.model_dump(mode="json"))


def _cached_values(analysis: WorkbookAnalysis, _sheet: str | None, _operation: str | None) -> str:
    return _dump(analysis.cached_values.model_dump(mode="json"))


_SECTIONS = {
    "sheets": _sheets,
    "operations": _operations,
    "operation": _operation,
    "findings": _findings,
    "named_ranges": _named_ranges,
    "connections": _connections,
    "power_query": _power_query,
    "notes": _notes,
    "graph": _graph,
    "cached_values": _cached_values,
}


def _render_profile(profile: Any) -> str:
    lines = [
        f"{profile.sheet}!{profile.column} — {profile.header or '(no header)'}",
        f"dtype={profile.dtype} rows={profile.row_count} nulls={profile.null_count} "
        f"distinct={profile.distinct_count}",
    ]
    if profile.numeric is not None:
        stats = profile.numeric
        lines.append(
            f"min={stats.min} max={stats.max} mean={stats.mean} sum={stats.sum} "
            f"zeros={stats.zero_count} negatives={stats.negative_count}"
        )
    if profile.format_anomalies:
        lines.append("anomalies: " + "; ".join(profile.format_anomalies))
    if profile.redacted:
        lines.append("values are redacted for this column; shape only.")
        return "\n".join(lines)
    if profile.top_k:
        lines.append(
            "top values: " + ", ".join(f"{value!r} x{count}" for value, count in profile.top_k)
        )
    for label, values in (
        ("head", profile.head),
        ("tail", profile.tail),
        ("sample", profile.sample),
    ):
        if values:
            lines.append(f"{label}: " + json.dumps(list(values), default=str, ensure_ascii=False))
    return "\n".join(lines)


# ── reconciliation ───────────────────────────────────────────────────────────────────────────

_REFERENCE = re.compile(r"^(?P<sheet>[^!]+)!(?P<range>.+)$")
_MAX_REPORTED_MISMATCHES = 20


async def _reconcile(context: ToolContext, args: Mapping[str, Any]) -> ToolResult:
    """Reconcile one notebook variable against the workbook's cached values, or say why not.

    The values come from the notebook *file*, through
    :func:`kedge.reconcile.notebook_definitions`, rather than from the kernel. That is not a
    workaround: the probe channel returns a truncated ``repr``, so a 49,999-row region cannot come
    back through it, and running the notebook is the same trust boundary as opening it in marimo.
    The consequence is stated in the tool's own result — a region is reconciled as the notebook was
    last written to disk, so an unsaved edit reconciles the previous version.

    Every failure path here reports NOT RECONCILED and says why. It is the one place in kedge where
    an optimistic default would be actively dangerous (CLAUDE.md non-negotiable 6): a false "passed"
    is a signed-off process that does not reproduce its own numbers.

    **There is deliberately no cached-value pre-check here.** One used to refuse outright when
    ``context.analysis`` reported no coverage, and it was wrong three times over. The analysis is
    loaded once when the loop is built and never regenerated, so it went on refusing after the user
    had done the very thing the refusal asked for — open the workbook in Excel, let it calculate,
    save it — which is the sequence kedge itself prints. It asked a workbook-wide question where
    the one being answered is per-range. And it was redundant: :func:`kedge.reconcile.read_baseline`
    reads the actual range from the file as it is now, and
    :func:`kedge.reconcile.compare.reconcile_region` turns an absent baseline into a
    ``NOT_RECONCILED`` region carrying :attr:`NotReconciledReason.NO_CACHED_VALUES`' own
    explanation, which says what the pre-check said. Nothing weakens by removing it: a region with
    no baseline cannot reach ``PASSED`` from any of the three checks that stand in its way -- the
    absent-baseline branch, the zero-compared-rows branch, and ``RegionResult``'s own refusal to be
    constructed as a pass without compared rows.
    """
    reference = str(args["reference"])
    variable = str(args["variable"])
    match = _REFERENCE.match(reference)
    if match is None:
        return ToolResult.note(
            f"'{reference}' is not a sheet-qualified range. Write it as 'Calc!H2:H50000'.",
            ok=False,
        )
    sheet = match.group("sheet")
    region_id = _optional_str(args.get("region_id")) or variable

    def refuse(detail: str, summary: str) -> ToolResult:
        return ToolResult.note(
            f"NOT RECONCILED — {detail}\n\nSay this plainly rather than implying the "
            f"translation was checked. A region that could not be compared has not passed.",
            ok=False,
            sheet=sheet,
            summary=summary,
            caps=context.caps,
        )

    try:
        from kedge.reconcile import (
            NotebookRunError,
            RegionSpec,
            Tolerance,
            notebook_definitions,
            reconcile_workbook,
        )
    except ImportError as exc:
        logger.info("kedge.reconcile is unavailable: %s", exc)
        return refuse(
            f"reconciliation is not available in this build ({exc}), so '{variable}' cannot be "
            f"compared against {reference}.",
            "not reconciled: reconciliation unavailable",
        )

    workspace = context.workspace
    if workspace is None:
        return refuse(
            "no workspace is attached, so there is no workbook to read a baseline from.",
            "not reconciled: no workspace",
        )

    # Read for the report header only, and never refused on: see the note in the docstring. It is
    # the analysis as it was when the loop was built, so it may describe the workbook before the
    # user recalculated and saved it; the baseline itself is read from the file as it is now.
    analysis = context.analysis

    try:
        definitions = await asyncio.to_thread(notebook_definitions, workspace.notebook_path)
    except NotebookRunError as exc:
        return refuse(
            f"the notebook could not be run, so it produced no values to compare: {exc}",
            "not reconciled: the notebook did not run",
        )

    if variable not in definitions:
        available = ", ".join(sorted(name for name in definitions if not name.startswith("_"))[:40])
        return refuse(
            f"the notebook does not define '{variable}'. It defines: {available or 'nothing'}. "
            f"Note that this reads the notebook as it was last written to disk, so a cell you "
            f"have only just created may not be saved yet — run it and try again.",
            "not reconciled: no such variable",
        )

    spec = RegionSpec(
        id=region_id,
        reference=reference,
        variable=variable,
        column=_optional_str(args.get("column")),
    )
    tolerances = workspace.config.reconciliation
    report = await asyncio.to_thread(
        reconcile_workbook,
        workspace.workbook_path,
        {spec.id: definitions[variable]},
        specs=[spec],
        tolerance=Tolerance.from_config(tolerances),
        max_mismatch_rows=tolerances.max_mismatch_rows,
        notebook=str(workspace.notebook_path),
        analysis=analysis,
    )
    return _render_reconciliation(report, sheet=sheet, caps=context.caps)


def _render_reconciliation(report: Any, *, sheet: str, caps: Caps) -> ToolResult:
    """Render a reconciliation report for the model, mismatch rows and all, under the caps."""
    lines = [report.headline()]
    rows: list[list[Any]] = []
    for region in report.regions:
        lines.append(region.headline())
        if region.detail:
            lines.append(f"  {region.detail}")
        if region.worst_absolute_delta is not None:
            lines.append(
                f"  worst absolute delta {region.worst_absolute_delta} at "
                f"{region.worst_at or 'an unrecorded row'}; worst relative "
                f"{region.worst_relative_delta}"
            )
        for diagnosis in region.diagnoses[:3]:
            # The remedy is the half the model acts on; the headline is the half the user reads.
            lines.append(
                f"  diagnosis ({diagnosis.confidence:.2f}): {diagnosis.headline}\n"
                f"    remedy: {diagnosis.remedy}"
            )
        for mismatch in region.mismatches[:_MAX_REPORTED_MISMATCHES]:
            rows.append(
                [
                    region.spec_id,
                    mismatch.row,
                    mismatch.cell,
                    mismatch.kind.value,
                    mismatch.expected,
                    mismatch.actual,
                    mismatch.absolute_delta,
                ]
            )
        if region.mismatches_truncated:
            lines.append("  (the mismatch list was truncated by the reconciler)")

    passed = report.status.value == "passed"
    header = "\n".join(lines)
    if not rows:
        return ToolResult.note(
            header, ok=passed, sheet=sheet, summary=report.headline()[:160], caps=caps
        )
    table = ToolResult.from_rows(
        rows,
        columns=["region", "row", "cell", "kind", "expected", "actual", "absolute_delta"],
        caps=caps,
        header=header,
        sheet=sheet,
        summary=report.headline()[:160],
    )
    return replace(table, ok=passed)
