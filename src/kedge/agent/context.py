"""Context assembly, the name registry, the token budget, and compaction.

PLAN M4 states one rule above all the others: **notebook state is rebuilt from the kernel every
turn**. Cell code carried in message history is stale the moment the user edits a cell, which
they do, in the pane next to the chat. So nothing here trusts history for facts about the
notebook — :class:`NotebookState` and :class:`NameRegistry` are constructed fresh from a live
:class:`~kedge.notebook.model.GraphView` at the top of each turn and pinned into the window.

The **name registry** is the mitigation for what PLAN calls the dominant failure mode: marimo
requires each public name to have exactly one owning cell, imports included, so two cells doing
``import polars as pl`` is a hard error at flush. A model that cannot see who owns what will
collide constantly. The registry is a compact ``name -> owning cell`` table, injected every turn
and never evicted, and it is also what :mod:`kedge.agent.validate` checks proposed code against
before anything is submitted.

What is pinned and what is disposable is a fixed order, not a heuristic:

* never evicted — the system prompt, the name registry, live notebook state;
* evicted first — tool results, oldest first, because sampled data is the largest and most
  disposable thing in the window and re-fetching it costs one call;
* evicted next — whole turns, oldest first, replaced by a line in the digest.

Two windows can hold the same turn. A turn that ends without an answer — the step budget ran out,
the user pressed Stop — is lifted out with :meth:`ConversationWindow.suspend` and seeded back into
the next turn's window by :meth:`ConversationWindow.resume`. The tool traffic that made the turn
expensive is precisely what the server does not persist, so without that hand-off the model comes
back having forgotten everything it just went and looked up.

Counting is done with ``tiktoken`` where it is available. It is not always: the encodings are
downloaded on first use, and a machine with no network — or a locked-down one — would otherwise
turn a context budget into a hard failure. :class:`TokenCounter` degrades to a character
heuristic and says so, which is the right trade for a budget that only has to be approximately
right.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from kedge.analysis.model import WorkbookAnalysis
    from kedge.notebook.model import CellInfo, GraphView
    from kedge.plan.model import ProcessPlan

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_REGISTRY_NAMES",
    "CellFacts",
    "ContextMessage",
    "ConversationWindow",
    "EvictionReport",
    "NameRegistry",
    "NotebookState",
    "TokenCounter",
    "build_analysis_block",
    "build_plan_block",
    "summarise_messages",
]

MAX_REGISTRY_NAMES = 400
"""Ceiling on names rendered in the registry. Beyond this a workbook has other problems."""

MAX_PROFILE_ROWS = 60
"""Column profiles shipped by default. Full detail comes through ``inspect_workbook``."""

_CHARS_PER_TOKEN = 4
"""Fallback ratio when tiktoken is unavailable. Deliberately conservative for English prose."""

_EVICTED_TOOL_RESULT = (
    "[tool result evicted to stay within the context budget — call the tool again if you need it]"
)

_UNANSWERED_TOOL_CALL = "[no result — the turn stopped before this call completed]"


# ── token counting ───────────────────────────────────────────────────────────────────────────


class TokenCounter:
    """Counts tokens with ``tiktoken``, falling back to a character heuristic.

    ``exact`` reports which of the two is in play, so a caller can say "approximately" when it
    matters and a test can assert the fallback path without a network.

    Example:
        >>> counter = TokenCounter(encoding="cl100k_base", allow_download=False)
        >>> counter.count("") == 0
        True
    """

    def __init__(self, *, encoding: str = "cl100k_base", allow_download: bool = True) -> None:
        self._name = encoding
        self._encoding: Any | None = None
        if allow_download:
            self._encoding = _load_encoding(encoding)
        self.exact = self._encoding is not None

    @property
    def encoding_name(self) -> str:
        """The tiktoken encoding requested."""
        return self._name

    def count(self, text: str) -> int:
        """Return the token count of ``text``."""
        if not text:
            return 0
        if self._encoding is not None:
            try:
                return len(self._encoding.encode(text, disallowed_special=()))
            except Exception:
                logger.debug("tiktoken failed on a payload; falling back", exc_info=True)
                self._encoding = None
                self.exact = False
        return max(1, len(text) // _CHARS_PER_TOKEN)

    def count_message(self, message: dict[str, Any]) -> int:
        """Return the approximate cost of one chat message, framing included."""
        total = 4
        for key, value in message.items():
            if isinstance(value, str):
                total += self.count(value)
            elif value is not None and key != "role":
                total += self.count(json.dumps(value, default=str))
        return total


def _load_encoding(name: str) -> Any | None:
    try:
        import tiktoken
    except ImportError:
        logger.warning("tiktoken is not installed; token counts will be approximate")
        return None
    try:
        return tiktoken.get_encoding(name)
    except Exception as exc:
        logger.warning(
            "could not load the %r tiktoken encoding (%s); token counts will be approximate",
            name,
            exc,
        )
        return None


# ── the name registry ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CellFacts:
    """One cell's identity and its place in the dataflow graph. Never its code.

    Code is deliberately absent. Reading a cell's code is what marks it read for marimo's
    staleness guard (docs/marimo-api.md 4.1), so a per-turn rebuild that pulled every body
    would silently disarm the "the user edited this behind your back" protection for the whole
    notebook. Bodies come back through ``list_cells`` when the model actually needs one.
    """

    id: str
    name: str
    defs: tuple[str, ...] = ()
    refs: tuple[str, ...] = ()
    status: str | None = None

    @property
    def label(self) -> str:
        """How this cell is named in prose: its name where it has one, else its id."""
        return self.name if self.name and self.name != "_" else self.id


class NameRegistry:
    """``name -> owning cell`` for every public name in the notebook.

    marimo requires exactly one owning cell per public name, including imports. Two cells doing
    ``import polars as pl`` is a hard error at flush, reported as
    ``RuntimeError: Multiply-defined names``. This is the table that stops the model walking
    into that, and the table the validation gate checks against so a collision is caught before
    a round trip rather than after one.

    Names beginning with an underscore are marimo's escape hatch — they are cell-local and may
    repeat — so they are excluded from ownership entirely.

    Example:
        >>> registry = NameRegistry([CellFacts(id="A", name="imports", defs=("pl",))])
        >>> registry.owner_of("pl").label
        'imports'
    """

    def __init__(self, cells: Iterable[CellFacts]) -> None:
        self._cells = tuple(cells)
        self._owners: dict[str, CellFacts] = {}
        for cell in self._cells:
            for name in cell.defs:
                if not name.startswith("_"):
                    self._owners.setdefault(name, cell)

    @classmethod
    def from_graph(cls, graph: GraphView) -> NameRegistry:
        """Build a registry from a live dataflow graph."""
        return cls(
            CellFacts(
                id=node.id,
                name=node.name,
                defs=tuple(node.defs),
                refs=tuple(node.refs),
                status=node.status,
            )
            for node in graph.nodes
        )

    @property
    def cells(self) -> tuple[CellFacts, ...]:
        """Every cell known to the registry, in notebook order."""
        return self._cells

    @property
    def names(self) -> tuple[str, ...]:
        """Every owned public name, sorted."""
        return tuple(sorted(self._owners))

    def owner_of(self, name: str) -> CellFacts | None:
        """Return the cell owning ``name``, or ``None`` if nothing owns it."""
        return self._owners.get(name)

    def conflicts(
        self, defs: Iterable[str], *, exclude: str | None = None
    ) -> tuple[tuple[str, CellFacts], ...]:
        """Return the ``(name, owner)`` pairs that a cell defining ``defs`` would collide with.

        ``exclude`` is the cell being replaced — an ``edit_cell`` that keeps its own names is
        not a collision with itself.
        """
        found: list[tuple[str, CellFacts]] = []
        for name in defs:
            if name.startswith("_"):
                continue
            owner = self._owners.get(name)
            if owner is None:
                continue
            if exclude is not None and exclude in (owner.id, owner.name):
                continue
            found.append((name, owner))
        return tuple(found)

    def would_cycle(
        self, defs: Iterable[str], refs: Iterable[str], *, exclude: str | None = None
    ) -> tuple[str, ...]:
        """Return the cells forming a cycle if a cell with these defs and refs were added.

        A cycle exists when a cell this one would read from also, transitively, reads something
        this one defines. Reported as the labels of the cells involved, which is what a useful
        violation message needs.
        """
        defined = {name for name in defs if not name.startswith("_")}
        wanted = set(refs)
        if not defined or not wanted:
            return ()

        upstream = {
            owner.id
            for name, owner in self._owners.items()
            if name in wanted and not _is(owner, exclude)
        }
        if not upstream:
            return ()

        downstream: set[str] = set()
        frontier = {
            cell.id for cell in self._cells if not _is(cell, exclude) and defined & set(cell.refs)
        }
        while frontier:
            downstream |= frontier
            produced = {name for cell in self._cells if cell.id in frontier for name in cell.defs}
            frontier = {
                cell.id
                for cell in self._cells
                if cell.id not in downstream
                and not _is(cell, exclude)
                and produced & set(cell.refs)
            }

        overlap = upstream & downstream
        by_id = {cell.id: cell for cell in self._cells}
        return tuple(sorted(by_id[cell_id].label for cell_id in overlap))

    def frame_names(self) -> frozenset[str]:
        """Public names that plausibly hold a frame, for the row-wise-loop style check.

        A heuristic, and knowingly so: the registry holds names, not types. It is used only to
        raise the confidence of a warning that is already suspicious on its own.
        """
        return frozenset(
            name
            for name in self._owners
            if any(hint in name.lower() for hint in ("frame", "_df", "df_", "data", "rows"))
            or name.lower().endswith("df")
        )

    def render(self) -> str:
        """Render the registry as the block injected into every turn."""
        lines = [
            "## Name registry — one owning cell per public name",
            "",
            "marimo requires exactly one owning cell per public name, imports included. Defining",
            "a name that already appears below is rejected before it reaches the kernel. To reuse",
            "a name, edit its owning cell; to keep something cell-local, prefix it with an",
            "underscore — `_private` names are exempt and may repeat.",
            "",
        ]
        if not self._owners:
            lines.append("(the notebook defines nothing yet)")
            return "\n".join(lines)

        width = min(32, max(len(name) for name in self._owners))
        for name in sorted(self._owners)[:MAX_REGISTRY_NAMES]:
            owner = self._owners[name]
            lines.append(f"{name.ljust(width)}  {owner.label} ({owner.id})")
        omitted = len(self._owners) - MAX_REGISTRY_NAMES
        if omitted > 0:
            lines.append(f"[… {omitted} more names omitted]")
        return "\n".join(lines)

    def __len__(self) -> int:
        return len(self._owners)

    def __repr__(self) -> str:
        return f"NameRegistry(cells={len(self._cells)}, names={len(self._owners)})"


def _is(cell: CellFacts, target: str | None) -> bool:
    return target is not None and target in (cell.id, cell.name)


# ── live notebook state ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class NotebookState:
    """The notebook as the kernel currently sees it, rebuilt at the top of every turn."""

    cells: tuple[CellFacts, ...] = ()
    cycles: tuple[tuple[str, ...], ...] = ()
    multiply_defined: tuple[str, ...] = ()

    @classmethod
    def from_graph(cls, graph: GraphView) -> NotebookState:
        """Build state from a live dataflow graph."""
        return cls(
            cells=tuple(
                CellFacts(
                    id=node.id,
                    name=node.name,
                    defs=tuple(node.defs),
                    refs=tuple(node.refs),
                    status=node.status,
                )
                for node in graph.nodes
            ),
            cycles=tuple(tuple(cycle) for cycle in graph.cycles),
            multiply_defined=tuple(graph.multiply_defined),
        )

    @classmethod
    def from_cells(cls, cells: Iterable[CellInfo]) -> NotebookState:
        """Build state from a cell listing, where no graph is available."""
        return cls(
            cells=tuple(CellFacts(id=cell.id, name=cell.name, status=cell.status) for cell in cells)
        )

    @property
    def registry(self) -> NameRegistry:
        """The name registry for this state."""
        return NameRegistry(self.cells)

    def render(self) -> str:
        """Render the block injected into every turn."""
        lines = [
            "## Live notebook state (read from the kernel at the start of this turn)",
            "",
            "Cell bodies are not shown. The user edits cells directly, so anything you remember",
            "from earlier in the conversation is stale — call `list_cells` when you need a body,",
            "and always read a cell before editing it.",
            "",
        ]
        if not self.cells:
            lines.append("(the notebook has no cells yet)")
        for position, cell in enumerate(self.cells, start=1):
            status = f" [{cell.status}]" if cell.status else ""
            defines = ", ".join(cell.defs) or "-"
            refs = ", ".join(cell.refs) or "-"
            lines.append(
                f"{position}. {cell.label} ({cell.id}){status} defines: {defines} | reads: {refs}"
            )
        if self.multiply_defined:
            lines.append("")
            lines.append(
                "WARNING — names already multiply defined: " + ", ".join(self.multiply_defined)
            )
        for cycle in self.cycles:
            lines.append("WARNING — dependency cycle: " + " -> ".join(cycle))
        return "\n".join(lines)


# ── analysis and plan blocks ─────────────────────────────────────────────────────────────────


def build_analysis_block(
    analysis: WorkbookAnalysis | None, *, max_profiles: int = MAX_PROFILE_ROWS
) -> str:
    """Render the workbook analysis as summary plus column profiles.

    Progressive disclosure, not a dump (PLAN 2.3, M4). ``WorkbookAnalysis.summary()`` exists for
    exactly this, and profiles are cheap where sheets are not. Formula bodies, references, cached
    values and sample rows all stay behind ``inspect_workbook`` and the value-returning tools.
    """
    if analysis is None:
        return (
            "## Workbook analysis\n\nNo analysis is loaded for this workspace. Run "
            "`kedge inspect` before asking for translation work — without it you are guessing at "
            "the workbook's structure."
        )

    lines = [
        "## Workbook analysis — summary",
        "",
        "```json",
        json.dumps(analysis.summary(), indent=2, default=str),
        "```",
        "",
        "Full detail — formula bodies, references, findings, connections, M code — is available "
        "through `inspect_workbook`. Raw cell values come through `sample_data`, `read_range` "
        "and `profile_column`.",
    ]

    profiles = analysis.profiles[:max_profiles]
    if profiles:
        lines += ["", "## Column profiles", ""]
        for profile in profiles:
            head = ", ".join(_short(value) for value in profile.head[:3])
            numeric = ""
            if profile.numeric is not None:
                numeric = (
                    f" min={_short(profile.numeric.min)} max={_short(profile.numeric.max)}"
                    f" sum={_short(profile.numeric.sum)}"
                )
            anomalies = (
                f" anomalies={'; '.join(profile.format_anomalies)}"
                if profile.format_anomalies
                else ""
            )
            redacted = " [redacted]" if profile.redacted else ""
            lines.append(
                f"{profile.sheet}!{profile.column} {profile.header or '(no header)'}: "
                f"{profile.dtype}, rows={profile.row_count}, nulls={profile.null_count}, "
                f"distinct={profile.distinct_count}{numeric}{anomalies}{redacted} "
                f"head=[{head}]"
            )
        omitted = len(analysis.profiles) - len(profiles)
        if omitted > 0:
            lines.append(f"[… {omitted} more column profiles omitted — use `profile_column`]")
    return "\n".join(lines)


def build_plan_block(plan: ProcessPlan | None) -> str:
    """Render the approved plan as the model's standing instructions."""
    if plan is None:
        return (
            "## Process plan\n\nNo approved plan is available. Nothing should be written to the "
            "notebook until one exists — propose one through the planning step rather than "
            "improvising a decomposition here."
        )

    lines = [
        f"## Process plan v{plan.version} ({plan.approval.state.value})",
        "",
        "These are your standing instructions. Work the stages in order; where the workbook "
        "disagrees with the plan, say so and use `amend_plan` rather than quietly diverging.",
        "",
        f"Convertible: {plan.assessment.convertible:.2f}",
    ]
    for blocker in plan.assessment.blockers:
        lines.append(f"  blocker: {blocker}")
    lines.append("")
    for stage in plan.ordered_stages():
        marker = " [CHECKPOINT — not automated]" if stage.is_checkpoint else ""
        review = " [low confidence — flag your assumptions]" if stage.needs_review_marker else ""
        lines.append(f"- {stage.id} ({stage.kind.value}, {stage.confidence.value}){marker}{review}")
        lines.append(f"    intent: {stage.intent}")
        if stage.sources:
            lines.append(f"    sources: {', '.join(stage.sources)}")
        if stage.depends_on:
            lines.append(f"    depends on: {', '.join(stage.depends_on)}")
        for assumption in stage.assumptions:
            lines.append(f"    assumption: {assumption}")
    if plan.unanswered_questions:
        lines += ["", "Open questions the user has not answered yet:"]
        lines += [f"  - {question.question}" for question in plan.unanswered_questions]
    return "\n".join(lines)


def _short(value: Any, limit: int = 24) -> str:
    text = "null" if value is None else str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ── the conversation window ──────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class ContextMessage:
    """One message in the window, with the bookkeeping eviction needs."""

    role: Literal["user", "assistant", "tool"]
    content: str
    turn: int = 0
    kind: Literal["user", "assistant", "tool_result"] = "user"
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_calls: tuple[dict[str, Any], ...] = ()
    evicted: bool = False

    def to_openai(self) -> dict[str, Any]:
        """Render as the chat-completions message shape."""
        if self.role == "tool":
            return {
                "role": "tool",
                "tool_call_id": self.tool_call_id or "",
                "content": _EVICTED_TOOL_RESULT if self.evicted else self.content,
            }
        message: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = list(self.tool_calls)
        return message


@dataclass(frozen=True, slots=True)
class EvictionReport:
    """What eviction had to do to fit the window."""

    tool_results_evicted: int = 0
    turns_summarised: int = 0
    tokens_before: int = 0
    tokens_after: int = 0

    @property
    def acted(self) -> bool:
        """Whether anything was dropped."""
        return bool(self.tool_results_evicted or self.turns_summarised)


class ConversationWindow:
    """Holds one conversation and fits it to the token budget.

    The pinned blocks — system prompt, name registry, live notebook state, analysis summary,
    plan — are never evicted; they are what the model needs to be *correct*, and dropping them
    to make room for a six-turn-old sample of a spreadsheet would be exactly backwards.

    Example:
        >>> window = ConversationWindow(system="be useful", budget=100_000)
        >>> window.add_user("convert the haircut lookup")
        >>> [message["role"] for message in window.assemble()]
        ['system', 'user']
    """

    def __init__(
        self,
        *,
        system: str,
        budget: int,
        counter: TokenCounter | None = None,
        evict_tool_results_after_turns: int = 6,
    ) -> None:
        self._system = system
        self._budget = max(budget, 1)
        self._counter = counter or TokenCounter()
        self._evict_after = max(1, evict_tool_results_after_turns)
        self._pinned: tuple[str, ...] = ()
        self._digest: str = ""
        self._messages: list[ContextMessage] = []
        self._turn = 0
        self._resumed_at: int | None = None

    # ── population ───────────────────────────────────────────────────────────────────────

    @property
    def counter(self) -> TokenCounter:
        """The token counter in use."""
        return self._counter

    @property
    def budget(self) -> int:
        """The usable token budget, output reserve already deducted."""
        return self._budget

    @property
    def messages(self) -> tuple[ContextMessage, ...]:
        """Every message currently held, evicted ones included."""
        return tuple(self._messages)

    @property
    def turn(self) -> int:
        """The index of the turn currently being built."""
        return self._turn

    def set_pinned(self, blocks: Sequence[str]) -> None:
        """Replace the pinned context blocks. Called once per turn, before assembly."""
        self._pinned = tuple(block for block in blocks if block.strip())

    def set_digest(self, digest: str) -> None:
        """Replace the compaction digest kept in place of dropped turns."""
        self._digest = digest

    @property
    def digest(self) -> str:
        """The decisions-and-open-questions digest, empty when nothing has been compacted."""
        return self._digest

    def begin_turn(self) -> int:
        """Start a new turn and return its index."""
        self._turn += 1
        return self._turn

    def add_user(self, content: str) -> None:
        """Append a user message."""
        self._messages.append(
            ContextMessage(role="user", content=content, turn=self._turn, kind="user")
        )

    def add_assistant(self, content: str, *, tool_calls: Sequence[dict[str, Any]] = ()) -> None:
        """Append an assistant message, with its tool calls where it made any."""
        self._messages.append(
            ContextMessage(
                role="assistant",
                content=content,
                turn=self._turn,
                kind="assistant",
                tool_calls=tuple(tool_calls),
            )
        )

    def add_tool_result(self, *, tool_call_id: str, name: str, content: str) -> None:
        """Append a tool result, the first thing eviction will reach for."""
        self._messages.append(
            ContextMessage(
                role="tool",
                content=content,
                turn=self._turn,
                kind="tool_result",
                tool_call_id=tool_call_id,
                tool_name=name,
            )
        )

    def load_history(self, history: Iterable[tuple[str, str]]) -> None:
        """Seed the window from the server's persisted history of prior turns.

        Only prose survives a restart: the server stores user and assistant messages, not the
        tool traffic between them, which is the right thing to persist and also the right thing
        to forget. Each prior exchange is given its own turn index so eviction can age it.
        """
        for role, content in history:
            if role == "user":
                self._turn += 1
                self.add_user(content)
            elif role == "assistant":
                self.add_assistant(content)

    # ── carrying a turn across a pause ───────────────────────────────────────────────────

    def suspend(self) -> tuple[ContextMessage, ...]:
        """Lift out the turn being built, so a later window can carry on from where it stopped.

        A turn that ends before the model has answered — the step budget ran out, the user pressed
        Stop — has usually spent most of its cost on tool calls, and :meth:`load_history` cannot
        bring them back: the server persists prose, which is the right thing to persist and the
        wrong thing to resume from. Handing these messages to the next window keeps the expensive
        half of the turn instead of making the model re-read the workbook to rediscover what it
        already knew.

        Every tool call leaves here answered. A chat completion is rejected outright when an
        assistant message carrying ``tool_calls`` is not followed by a result for each of them,
        and a turn abandoned between dispatching a call and recording its result is exactly how
        that happens, so anything unanswered is filled in with a note saying so.

        A window that was itself resumed carries the whole span back out, not just its newest
        turn. Work that survived one pause has to survive the second: a conversation that stops
        three times is one piece of work, and a hand-off that only ever remembered the most recent
        leg would quietly drop the first two. What limits the span is the budget rather than the
        count — :meth:`compact` runs first and takes with it anything the window could no longer
        afford.

        Example:
            >>> window = ConversationWindow(system="be useful", budget=100_000)
            >>> window.begin_turn()
            1
            >>> window.add_user("convert the haircut lookup")
            >>> window.add_assistant("", tool_calls=[{"id": "c1", "function": {"name": "probe"}}])
            >>> [message.role for message in window.suspend()]
            ['user', 'assistant', 'tool']
        """
        floor = self._turn if self._resumed_at is None else min(self._resumed_at, self._turn)
        current = [message for message in self._messages if message.turn >= floor]
        answered = {message.tool_call_id for message in current if message.kind == "tool_result"}
        carried: list[ContextMessage] = []
        for message in current:
            carried.append(message)
            if message.kind != "assistant":
                continue
            for call in message.tool_calls:
                call_id = str(call.get("id") or "")
                if not call_id or call_id in answered:
                    continue
                answered.add(call_id)
                function = call.get("function") or {}
                carried.append(
                    ContextMessage(
                        role="tool",
                        content=_UNANSWERED_TOOL_CALL,
                        turn=message.turn,
                        kind="tool_result",
                        tool_call_id=call_id,
                        tool_name=str(function.get("name") or ""),
                    )
                )
        return tuple(carried)

    def resume(self, messages: Sequence[ContextMessage]) -> int:
        """Seed the window with a suspended turn, tool traffic and all, and return its index.

        The messages are re-dated into the turn they are resumed as rather than kept at the index
        they were suspended from. A message inherits its age from its turn, and those tool results
        are the reason the turn was carried at all, so leaving them at their original index would
        have :meth:`_age_out_tool_results` throw away exactly what was preserved.

        The index is remembered, so a window that pauses again hands back the whole resumed span
        rather than only its last leg (:meth:`suspend`).
        """
        turn = self.begin_turn()
        self._messages.extend(replace(message, turn=turn) for message in messages)
        self._resumed_at = turn
        return turn

    # ── assembly ─────────────────────────────────────────────────────────────────────────

    def assemble(self) -> list[dict[str, Any]]:
        """Return the messages to send, evicting first if the window is over budget."""
        self._age_out_tool_results()
        self.fit()
        return self._render()

    def _render(self) -> list[dict[str, Any]]:
        head = "\n\n".join((self._system, *self._pinned))
        messages: list[dict[str, Any]] = [{"role": "system", "content": head}]
        if self._digest:
            messages.append(
                {
                    "role": "system",
                    "content": "## Earlier in this conversation (compacted)\n\n" + self._digest,
                }
            )
        messages.extend(message.to_openai() for message in self._messages)
        return messages

    def token_total(self) -> int:
        """The token cost of the window as it currently stands."""
        return sum(self._counter.count_message(message) for message in self._render())

    # ── eviction ─────────────────────────────────────────────────────────────────────────

    def _age_out_tool_results(self) -> None:
        """Evict tool results older than the configured number of turns, budget or no budget.

        A ``sample_data`` result from six turns ago is almost never load-bearing and re-fetching
        it costs one call, so this happens on age alone rather than waiting for pressure.
        """
        cutoff = self._turn - self._evict_after
        for message in self._messages:
            if message.kind == "tool_result" and not message.evicted and message.turn <= cutoff:
                message.evicted = True

    def fit(self) -> EvictionReport:
        """Evict until the window fits the budget, in the fixed order from PLAN M4."""
        before = self.token_total()
        if before <= self._budget:
            return EvictionReport(tokens_before=before, tokens_after=before)

        evicted = 0
        summarised = 0
        for message in self._messages:
            if self.token_total() <= self._budget:
                break
            if message.kind == "tool_result" and not message.evicted:
                message.evicted = True
                evicted += 1

        while self.token_total() > self._budget and self._can_drop_a_turn():
            summarised += self._drop_oldest_turn()

        after = self.token_total()
        report = EvictionReport(
            tool_results_evicted=evicted,
            turns_summarised=summarised,
            tokens_before=before,
            tokens_after=after,
        )
        if report.acted:
            logger.info(
                "context eviction: %d tool results, %d turns summarised, %d -> %d tokens",
                evicted,
                summarised,
                before,
                after,
            )
        if after > self._budget:
            logger.warning(
                "context is still %d tokens over budget after eviction; the pinned blocks alone "
                "exceed it",
                after - self._budget,
            )
        return report

    def _can_drop_a_turn(self) -> bool:
        return any(message.turn < self._turn for message in self._messages)

    def _drop_oldest_turn(self) -> int:
        """Drop every message of the oldest turn, folding it into the digest.

        Whole turns, never individual messages: a chat completion is rejected outright if an
        assistant message carrying ``tool_calls`` loses the tool results that answer them.
        """
        oldest = min(message.turn for message in self._messages)
        if oldest >= self._turn:
            return 0
        dropped = [message for message in self._messages if message.turn == oldest]
        self._messages = [message for message in self._messages if message.turn != oldest]
        line = summarise_messages(dropped)
        if line:
            self._digest = f"{self._digest}\n{line}".strip()
        return 1

    def compact(self) -> str:
        """Summarise everything but the current turn to a digest and drop the rest.

        PLAN M4 asks for this on ``/new`` and on a budget breach. It is done deterministically
        rather than with a model call: a compaction that needs the endpoint cannot run when the
        endpoint is the thing that is failing, and "decisions and open questions" is a shape that
        can be lifted out of the transcript without judgement.
        """
        keep = [message for message in self._messages if message.turn >= self._turn]
        dropped = [message for message in self._messages if message.turn < self._turn]
        if dropped:
            line = summarise_messages(dropped)
            if line:
                self._digest = f"{self._digest}\n{line}".strip()
        self._messages = keep
        return self._digest

    def reset(self) -> None:
        """Forget the conversation entirely, digest included. The ``/new`` path."""
        self._messages = []
        self._digest = ""
        self._turn = 0
        self._resumed_at = None

    def __repr__(self) -> str:
        return (
            f"ConversationWindow(messages={len(self._messages)}, turn={self._turn}, "
            f"budget={self._budget})"
        )


def summarise_messages(messages: Sequence[ContextMessage]) -> str:
    """Return a decisions-and-open-questions digest line for a run of messages.

    Deliberately mechanical. What survives compaction is what the next turn cannot rediscover
    cheaply: what the user asked for, which cells were touched, what was rejected, and anything
    left hanging as a question.
    """
    asked = [message.content.strip() for message in messages if message.kind == "user"]
    tools = [
        message.tool_name
        for message in messages
        if message.kind == "tool_result" and message.tool_name
    ]
    questions = [
        line.strip()
        for message in messages
        if message.kind == "assistant"
        for line in message.content.splitlines()
        if line.strip().endswith("?")
    ]
    parts: list[str] = []
    if asked:
        parts.append("asked: " + "; ".join(_short(item, 120) for item in asked))
    if tools:
        counts: dict[str, int] = {}
        for name in tools:
            counts[name] = counts.get(name, 0) + 1
        parts.append(
            "tools: " + ", ".join(f"{name}x{count}" for name, count in sorted(counts.items()))
        )
    if questions:
        parts.append("open: " + "; ".join(_short(item, 120) for item in questions[:3]))
    if not parts:
        return ""
    turn = messages[0].turn if messages else 0
    return f"- turn {turn}: " + " | ".join(parts)


_ = field  # re-exported dataclass helper kept importable for callers building ContextMessages
