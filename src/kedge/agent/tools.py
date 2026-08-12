"""The tool surface: fifteen tools, one choke point, and a cap the model cannot walk around.

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

Adding a sixteenth tool therefore inherits the cap by construction. Forgetting it would require
deleting :meth:`dispatch`.

**The model never touches the notebook directly.** Structural changes go through the validation
gate first (:mod:`kedge.agent.validate`), and ``delete_cell`` does not delete: it records a
request and returns it, because a destructive change to the user's notebook is the user's decision
and a model that can delete a cell on its own reasoning will eventually delete the wrong one.

Every collaborator is optional and every tool degrades to a sentence explaining what is missing.
A workspace with no marimo attached, no analysis, no plan, an empty ``utils/`` and no knowledge
pack is a normal early state, not a broken one — and "there is no plan yet" is a far more useful
tool result than a traceback.
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
    from kedge.plan.store import PlanStore
    from kedge.workspace import Workspace

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_PAYLOAD_BYTES",
    "MAX_ROWS",
    "OMISSION_TEMPLATE",
    "TOOL_SPECS",
    "Caps",
    "PendingAmendment",
    "PendingDeletion",
    "ToolContext",
    "ToolKind",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "tool_names",
    "tool_schemas",
]

MAX_ROWS = 100
"""Default row cap for every value-returning tool (PLAN 2.3). Overridden by ``[sampling]``."""

MAX_PAYLOAD_BYTES = 32_768
"""Default payload cap in bytes (PLAN 2.3). Overridden by ``[sampling]``."""

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


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One tool as the model sees it, plus how kedge treats it."""

    name: str
    description: str
    properties: dict[str, Any]
    required: tuple[str, ...] = ()
    kind: ToolKind = ToolKind.VALUE

    def schema(self) -> dict[str, Any]:
        """The OpenAI chat-completions tool definition."""
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
            "Create a new named cell. The body is checked by the validation gate — syntax, the "
            "marimo single-definition rule against the live graph, policy, and output style — "
            "before it reaches the kernel. Violations come back to you to fix."
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
            "Replace a cell's full body. Partial edits do not exist. Refused unless the cell was "
            "read at its current version, which is how the user's own edits are protected; if it "
            "comes back stale, list_cells that cell and look at what they wrote before deciding."
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
    ),
    ToolSpec(
        name="get_knowledge",
        description=(
            "Fetch a context pack: documented warehouse tables with their grain, join keys and "
            "gotchas, plus the project's conventions and glossary. Omit the key for everything."
        ),
        properties={"key": _string("A document key or a table name. Omitted, the whole pack.")},
        kind=ToolKind.CONTEXT,
    ),
)


def tool_schemas() -> list[dict[str, Any]]:
    """Return every tool definition in the OpenAI chat-completions format."""
    return [spec.schema() for spec in TOOL_SPECS]


def tool_names() -> tuple[str, ...]:
    """Return every tool name, in the order they are offered to the model."""
    return tuple(spec.name for spec in TOOL_SPECS)


_SPECS_BY_NAME = {spec.name: spec for spec in TOOL_SPECS}


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
        """Build a context from a workspace, reading the caps and redaction rules from config."""
        from kedge.plan.store import PlanStore as _PlanStore

        config = workspace.config
        redaction = config.redaction
        return cls(
            workspace=workspace,
            analysis=analysis,
            driver=driver,
            plans=plans or _PlanStore.for_workspace(workspace),
            caps=Caps.from_config(config),
            policy=Policy(working_dir=workspace.project_dir),
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
        self._handle_failed = False
        self.pending_deletions: list[PendingDeletion] = []
        self.pending_amendments: list[PendingAmendment] = []

    # ── per-turn state ───────────────────────────────────────────────────────────────────

    @property
    def context(self) -> ToolContext:
        """The collaborators these tools work through."""
        return self._context

    @property
    def names(self) -> NameRegistry | None:
        """The live name registry, or ``None`` before the first refresh."""
        return self._registry

    def refresh(self, state: NotebookState) -> None:
        """Adopt the notebook state read from the kernel at the start of a turn."""
        self._state = state
        self._registry = state.registry

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

        handler = getattr(self, f"_tool_{name}")
        try:
            result = await handler(parsed)
        except KedgeError as exc:
            logger.info("tool %s failed: %s", name, exc)
            result = ToolResult.note(f"{name} failed: {exc}", ok=False)
        except Exception as exc:
            logger.exception("tool %s raised", name)
            result = ToolResult.note(f"{name} could not run: {type(exc).__name__}: {exc}", ok=False)
        return self._finalise(name, result, turn_id)

    def _finalise(self, name: str, result: ToolResult, turn_id: str | None) -> ToolResult:
        """Apply the payload cap unconditionally, then record the payload's shape.

        The re-cap is the belt to :meth:`ToolResult.from_rows`'s braces. A handler that builds a
        result by hand cannot emit an uncapped payload, because it does not control this step.
        """
        text, clipped = _cap_text(result.text, self._context.caps)
        capped = (
            result
            if not clipped
            else replace(
                result, text=text, truncated=True, summary=result.summary or _first_line(text)
            )
        )
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

    async def aclose(self) -> None:
        """Release the workbook handle, if one was opened."""
        if self._handle is not None:
            await asyncio.to_thread(self._handle.close)
            self._handle = None

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

    async def _workbook(self) -> Any:
        """Open the workbook lazily, once, and keep it for the session."""
        if self._handle is not None:
            return self._handle
        if self._handle_failed:
            msg = "the workbook could not be opened earlier in this session; see the server log."
            raise KedgeError(msg)
        workspace = self._context.workspace
        if workspace is None:
            msg = "no workspace is attached, so the workbook cannot be read."
            raise KedgeError(msg)
        from kedge.analysis.workbook import open_workbook

        try:
            self._handle = await asyncio.to_thread(open_workbook, workspace.workbook_path)
        except Exception:
            self._handle_failed = True
            raise
        return self._handle

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

    async def _tool_propose_cell(self, args: Mapping[str, Any]) -> ToolResult:
        driver = self._require_driver()
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
                "no process plan exists for this workbook yet. Nothing should be written to the "
                "notebook until one has been proposed and approved — run `kedge plan` first. "
                "Improvising a decomposition here is exactly what the plan exists to prevent.",
                ok=False,
                summary="no plan",
            )
        from kedge.agent.context import build_plan_block

        return ToolResult.note(
            f"This plan is {state}.\n\n{build_plan_block(plan)}",
            summary=f"plan v{plan.version} ({state})",
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
    if not isinstance(decoded, dict):
        msg = f"the tool arguments must be a JSON object, not {type(decoded).__name__}."
        raise ValueError(msg)
    return decoded


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

    analysis = context.analysis
    if analysis is not None and not analysis.cached_values.coverage:
        return refuse(
            "this workbook carries no cached Excel values, so there is no baseline at all. "
            "openpyxl never calculates, so a workbook written by a tool rather than saved by "
            "Excel has nothing cached. Ask the user to open it in Excel, let it calculate, and "
            "save it.",
            "not reconciled: no cached values",
        )

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
