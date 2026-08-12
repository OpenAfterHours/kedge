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
  disposable thing in the window and re-fetching it costs one call. What is left in place names
  the call that produced it and the shape of what came back, so the model can tell a sample it
  no longer needs from a finding it does, instead of re-fetching everything on principle;
* evicted next — whole turns, oldest first, replaced by one digest entry per turn holding what
  the user asked, the model's own written account of what it found, and anything left open;
* evicted last — digest entries themselves, oldest first. The digest is the only part of the
  window that nothing else evicts, so left to itself it accumulates: every compaction appends to
  it, it is counted against the budget, and a larger digest trips the compaction threshold sooner,
  which appends to it again. That loop ends with a window that cannot fit however much it drops,
  on every turn for the rest of the session. It is bounded to a share of the budget
  (:data:`DIGEST_BUDGET_SHARE`) rather than to a fixed size, because the budget itself varies by
  an order of magnitude between a hosted model and a local one.

Pressure and age are not the only things that evict. A caller carrying a turn forward may evict a
result because it has stopped being *true* — a probe against a kernel that has since run, a cell
listing from before an edit — which leaves the same stub and reads the same way to the model. This
module cannot make that judgement itself: it does not know the tool surface and must not import it
(:meth:`ConversationWindow.add_tool_result`), so the decision belongs to
:mod:`kedge.agent.loop` and only the mechanism lives here.

Two windows can hold the same turn. Any turn may be lifted out with
:meth:`ConversationWindow.suspend` and seeded into the next turn's window by
:meth:`ConversationWindow.resume` — a turn that ended without an answer, so that the next message
continues it, and equally a turn that answered, so that what it went and looked up is still there
when the user asks the follow-up. The tool traffic is precisely what the server does not persist,
so without that hand-off the model comes back having forgotten everything it just read.

Carrying on every turn is what makes ageing delicate. :meth:`ConversationWindow.resume` re-dates a
carried message into the turn it is resumed as, because ordering demands it, so a message's turn
index says nothing about its age — :attr:`ContextMessage.carried_age` is what does, and
:meth:`ContextMessage.age_at` is the only honest way to ask. Without it a carried result is reborn
young on every carry and never ages out at all, which is a context leak rather than a rounding
error.

It is also what makes the *cost* of ageing visible. The span sits between the flattened history and
the current message, so a leg leaving it on every turn moves a boundary in the middle of the prompt
on every turn, and a prefix cache keeps nothing behind a boundary that moves. So the span is re-cut
on one turn in :data:`CARRY_BLOCK_TURNS` and only grows at its end in between
(:meth:`ConversationWindow._recut`), and how much of it may accumulate is bounded by characters as
well as by messages (:data:`CARRY_BUDGET_SHARE`), because a count of messages does not bound a
cost.

Counting is done with ``tiktoken`` where it is available. It is not always: the encodings are
downloaded on first use, and a machine with no network — or a locked-down one — would otherwise
turn a context budget into a hard failure. :class:`TokenCounter` degrades to a character
heuristic and says so, which is the right trade for a budget that only has to be approximately
right.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from kedge.analysis.model import WorkbookAnalysis
    from kedge.notebook.model import CellInfo, GraphView
    from kedge.plan.model import ProcessPlan

logger = logging.getLogger(__name__)

__all__ = [
    "CARRY_BLOCK_TURNS",
    "CARRY_BUDGET_SHARE",
    "DIGEST_BUDGET_SHARE",
    "MAX_CARRIED_MESSAGES",
    "MAX_DIGEST_PROSE_CHARS",
    "MAX_DIGEST_PROSE_LINES",
    "MAX_EVICTED_ARGUMENT_CHARS",
    "MAX_EVICTED_SHAPE_CHARS",
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

MAX_CARRIED_MESSAGES = 200
"""Ceiling on the messages one window hands to the next (:meth:`ConversationWindow.suspend`).

A backstop, and only once there are two legs to choose between. There is no unit smaller than a
turn to drop — an assistant message whose results were trimmed out from under it is rejected by the
endpoint — so a span holding one enormous turn passes *this* bound untouched: fifty steps making
three calls each is 201 messages in a single leg. That shape is :data:`CARRY_BUDGET_SHARE`'s to
answer, and it answers it by evicting inside the leg rather than by dropping it. What the count
catches is the shape neither of the other bounds sees: many small legs, none of them old and none
of them large.

The age bound (:meth:`ConversationWindow._recut`) does the ordinary work and
:data:`CARRY_BUDGET_SHARE` bounds the cost; this bounds the count."""

CARRY_BLOCK_TURNS = 2
"""Turns a carried span holds still before it is re-cut, so that a prompt cache can keep it.

A leg that leaves the span reappears in the flattened history the server persists — one assistant
message holding the answer, where the span held the interim prose, the tool calls and the results
— so the moment the boundary between the two moves, the new prompt stops matching the old one at
the first carried message and everything behind it is re-sent uncached, the legs that did not
change included. Stubbing one leg and dropping one leg per turn moves that boundary every turn.
Measured over fourteen turns against a 9k-token head at the default horizon, it cost 1,408 uncached
tokens a turn to carry payloads worth a few hundred.

So the stubbing and the dropping happen together, on one turn in this many: the legs stubbed last
time are dropped and the oldest live ones are stubbed to replace them, and in between the span only
gains a leg at its end, where a prefix cache does not care. The same measurement then alternates
1,292 and 254, a mean of 773 — the re-cut turn still pays for the whole span, and that part is
irreducible: a leg has to leave the span for history eventually, and it is a different shape on the
other side.

Two rather than more because the two costs pull opposite ways. The churn amortises as
``span / block``, while a leg loses up to ``block - 1`` turns of content: the re-cut stubs it
before its own horizon rather than after it, which is the safe direction to be wrong in — nothing
is ever kept past the horizon the user set — but is still a turn of detail the model does not get.
Two halves the churn at the cost of one turn off the oldest and least useful leg in the span.
Clamped below by the horizon in :meth:`ConversationWindow._recut`, so a session that keeps results
for one or two turns keeps the per-turn cadence it asked for instead of losing its whole carry to a
block."""

CARRY_BUDGET_SHARE = 0.25
"""Share of the token budget the carried span may occupy, counted in characters.

A count of messages does not bound a cost. One ``sample_data`` payload at the 32KB cap is some
fifteen thousand tokens, so :data:`MAX_CARRIED_MESSAGES` of those is twenty times a hosted model's
whole context and nearly two hundred times a small local one — and a span that large does not merely
cost too much, it delivers nothing: :meth:`ConversationWindow.fit` evicts every result in it before
the model reads one, having tokenised them all first and held every byte for the session.

A share rather than a constant for the reason :data:`DIGEST_BUDGET_SHARE` gives — the budget is the
user's to set and spans an order of magnitude — and a quarter of it because the span is the warm
start and not the conversation: the pinned head is already most of a small budget, and what is left
has to hold the history and the turn itself. Measured on the raw payloads rather than on what they
render as, so it bounds what a session holds as well as what it sends."""

MAX_PROFILE_ROWS = 60
"""Column profiles shipped by default. Full detail comes through ``inspect_workbook``."""

MAX_EVICTED_ARGUMENT_CHARS = 100
"""Ceiling on the arguments rendered into an eviction stub.

The stub exists to cost less than what it replaces, so a call with a 4KB argument gets the same
short note as one with none: enough to recognise the call, never enough to be worth keeping. This
caps one rung; :func:`_evicted_stub` is what holds the whole stub under what it replaced."""

MAX_EVICTED_SHAPE_CHARS = 60
"""Ceiling on the caller-supplied description of what an evicted result contained."""

MAX_DIGEST_PROSE_LINES = 12
"""Lines of assistant prose carried into the digest when a turn is compacted."""

MAX_DIGEST_PROSE_CHARS = 1200
"""Characters of that prose carried into the digest. Whichever cap bites first wins."""

DIGEST_BUDGET_SHARE = 0.05
"""Share of the token budget the compaction digest may occupy before its oldest entries go.

A share rather than a constant because ``[context] max_context_tokens`` is the user's to set and
spans an order of magnitude: the same fixed cap that is a rounding error against a hosted model's
128k is most of a 16k local one. Capping each *entry* cannot bound an accumulator, and the digest
accumulates — see :meth:`ConversationWindow._trim_digest` for the loop this closes.

Five per cent leaves the digest room for roughly a dozen compacted turns at the default budget
and two at the smallest one worth running, which is the right way round: a small budget is
compacted often, and what it most needs the room for is the conversation itself."""

_CHARS_PER_TOKEN = 4
"""Fallback ratio when tiktoken is unavailable. Deliberately conservative for English prose."""

_EVICTED_TOOL_RESULT = (
    "[tool result evicted to stay within the context budget — call the tool again if you need it]"
)
"""The stub for a result whose call was never recorded. All that can honestly be said about one,
and used where it costs no more than the result did (:func:`_evicted_stub`)."""

_EVICTED_TAIL = "evicted to stay within the context budget; call it again if you need the detail"

_EVICTED_SHORT_TAIL = "evicted; call it again if you need it"
"""The tail for a result too small to be worth a full stub. Names the call and says to repeat it."""

_EVICTED_BRIEF = "[evicted]"
"""The floor: what is left when even naming the call would cost more than the result did.

Nothing shorter can be said and still be true. A result this small — a ``probe`` returning a
single number — cannot be replaced by anything cheaper than itself, so the only question is how
much is overspent saying it has gone, and this is the least."""

_SUBSTANTIAL_PROSE_CHARS = 80
"""Below this, an assistant message is narration — "Reading the sheet." — not an account."""

_DIGEST_ELISION = "[… the rest of this answer was elided to fit the digest]"

_DIGEST_ENTRY_MARKER = "- "
"""What an entry starts with, and therefore how a rendered digest is split back into entries."""

_DIGEST_DROPPED = (
    "- [{count} earlier digest {noun} dropped to keep the digest inside its share of the budget]"
)

_DIGEST_DROPPED_PATTERN = re.compile(r"^- \[(\d+) earlier digest entr")
"""Recovers the count from a rendered marker, so a digest that round-trips through
:meth:`ConversationWindow.set_digest` does not forget how much it has already let go of."""

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
        """Render the block injected into every turn.

        Kept to the notebook, and short. This block is the most volatile of the pinned set and
        therefore sits last in them, where a prompt cache keys on the prefix and nothing behind a
        change survives it: every character here is re-sent on any turn that touches a cell. What
        the model may assume about the *workbook* is a standing rule and lives in the role prompt,
        which can condition it on the user having saved from Excel; a frozen record of
        :class:`CellFacts` cannot, and stating it here unconditionally made it false in exactly
        the case kedge asks the user to create.
        """
        lines = [
            "## Live notebook state (read from the kernel at the start of this turn)",
            "",
            "Cell bodies are not shown. The user edits cells directly, so a cell body you remember",
            "from earlier in the conversation is stale — call `list_cells` when you need one, and",
            "always read a cell before editing it. This block describes the notebook alone; that it",
            "is rebuilt every turn says nothing about anything else you have established.",
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

    The block dates itself. kedge never writes to the workbook, but the user is repeatedly asked
    to — reconciliation with no baseline is answered by opening it in Excel, recalculating and
    saving — and nothing regenerates the analysis when they do. This is the one place that can say
    so honestly, since it has :attr:`~kedge.analysis.model.WorkbookAnalysis.generated_at` in hand,
    and the cheapest place to say it: first in the pinned order, so it sits in the part of the
    prompt a cache keeps.
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
        f"This describes the workbook as it was read at {analysis.generated_at:%Y-%m-%d %H:%M}; "
        "if the user tells you they have saved it from Excel since, this summary and every cached "
        "value you have read from it are out of date.",
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
    """Render the approved plan as the model's standing instructions.

    With no plan the block is not a placeholder but the instruction for the turn: the planning
    step is the model's own, through ``propose_plan``, and saying "propose one through the
    planning step" while that step is right here read as though it were somewhere else.
    """
    if plan is None:
        return (
            "## Process plan\n\nNo approved plan is available, so you have no standing "
            "instructions and nothing should be written to the notebook yet. The planning step is "
            "yours and it happens here: work out what this workbook does, stage by stage, then "
            "send that account through `propose_plan` for the user to approve. An account left in "
            "the chat is compacted and evicted as the conversation grows; a plan is on disk, "
            "versioned, and in front of you on every later turn. Do not improvise a decomposition "
            "cell by cell instead."
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


def _argument_value(value: Any) -> str:
    try:
        text = json.dumps(value, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return _short(text, 40)


def _render_call(name: str | None, arguments: Mapping[str, Any] | None) -> str:
    """Render ``tool(arg="value")`` for an eviction stub, or the bare name where nothing was
    recorded, or nothing at all where not even the name was.

    Arguments are rendered in key order rather than in the order the endpoint happened to send
    them, so that the same call twice is the same stub twice.
    """
    if not name:
        return ""
    if not arguments:
        return name
    rendered = ", ".join(
        f"{key}={_argument_value(value)}" for key, value in sorted(arguments.items())
    )
    if len(rendered) > MAX_EVICTED_ARGUMENT_CHARS:
        rendered = rendered[: MAX_EVICTED_ARGUMENT_CHARS - 1].rstrip() + "…"
    return f"{name}({rendered})"


def _evicted_stub(
    name: str | None,
    arguments: Mapping[str, Any] | None,
    shape: str | None,
    *,
    replacing: int,
) -> str:
    """Render what stands in for an evicted tool result, for no more than the result cost.

    One constant for every eviction told the model that something had gone without telling it
    what, which leaves re-fetching everything as the only safe move. Naming the call and the
    shape of its answer lets the model decide: a stale sample of 200 rows is worth re-reading,
    a count it already wrote down is not.

    The catch is that a stub is not free, and results are not all large. A ``probe`` answering
    ``"12345"`` costs eight tokens; the full stub for it costs forty, so a turn of twenty small
    probes *grew* by evicting them — and eviction happens on age alone, with no budget pressure
    needed. So the rungs are tried richest first and the richest one that fits inside what it
    replaces wins. Where none fits, the cheapest is used: the content is gone either way, because
    a caller may have evicted it for having stopped being true rather than for costing too much
    (:mod:`kedge.agent.loop`), and rendering it again would be a lie rather than a saving.

    Args:
        name: The tool that produced the result, where it was recorded.
        arguments: What it was called with.
        shape: The caller's short description of what came back.
        replacing: Length in characters of the content being replaced.

    Returns:
        The most informative rendering costing no more than ``replacing`` characters, or the
        cheapest rendering there is when nothing costs that little.
    """
    call = _render_call(name, arguments)
    rungs: list[str] = []
    if call:
        if shape:
            rungs.append(f"[{call} — {_short(shape, MAX_EVICTED_SHAPE_CHARS)} — {_EVICTED_TAIL}]")
        rungs.append(f"[{call} — {_EVICTED_TAIL}]")
        rungs.append(f"[{name} — {_EVICTED_SHORT_TAIL}]")
        rungs.append(f"[{name} evicted]")
    else:
        rungs.append(_EVICTED_TOOL_RESULT)
    rungs.append(_EVICTED_BRIEF)
    return next((rung for rung in rungs if len(rung) <= replacing), rungs[-1])


@dataclass(slots=True)
class ContextMessage:
    """One message in the window, with the bookkeeping eviction needs."""

    role: Literal["user", "assistant", "tool"]
    content: str
    turn: int = 0
    kind: Literal["user", "assistant", "tool_result"] = "user"
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_arguments: Mapping[str, Any] | None = None
    result_shape: str | None = None
    tool_calls: tuple[dict[str, Any], ...] = ()
    evicted: bool = False

    carried_age: int = 0
    """Turns this message had already lived through before this window first saw it.

    ``turn`` is an index into *this* window and is what fixes ordering, so
    :meth:`ConversationWindow.resume` has to re-date a carried message forward: left at the index
    it was suspended from it would sort behind history that is older than it. That makes ``turn``
    useless as an age, and every turn now carries its tool traffic forward, so a message read as
    young on every carry would never age out at all. This is the part of the age that survives
    re-dating; :meth:`age_at` is the sum.
    """

    _tokens: tuple[bool, int] | None = field(default=None, init=False, compare=False, repr=False)
    """The cached ``(evicted, count)`` pair. Keyed on ``evicted`` because that is the only field
    that changes after a message is added, and it changes what ``to_openai`` renders — the
    eviction stub is a function of ``tool_name``, ``tool_arguments``, ``result_shape`` and the
    length of ``content``, every one of them fixed at construction, so it does not widen the key.
    Not an init field, so :func:`dataclasses.replace` starts a copy with an empty cache rather
    than an inherited one."""

    def age_at(self, turn: int) -> int:
        """How many turns old this message is once the window has reached ``turn``.

        The distance from the current turn plus whatever age it arrived carrying. Every question
        about age asks this rather than reading ``turn``, which measures position and not time.

        Args:
            turn: The index the window is currently building.

        Returns:
            The age in turns, never negative.
        """
        return max(0, turn - self.turn) + self.carried_age

    def tokens(self, counter: TokenCounter) -> int:
        """Return this message's cost, counting it only when the answer is not already known.

        Eviction walks the window repeatedly, and re-tokenising every message on every pass made
        fitting a full window quadratic in tiktoken calls rather than in additions.
        """
        cached = self._tokens
        if cached is not None and cached[0] == self.evicted:
            return cached[1]
        count = counter.count_message(self.to_openai())
        self._tokens = (self.evicted, count)
        return count

    def to_openai(self) -> dict[str, Any]:
        """Render as the chat-completions message shape."""
        if self.role == "tool":
            content = self.content
            if self.evicted:
                content = _evicted_stub(
                    self.tool_name,
                    self.tool_arguments,
                    self.result_shape,
                    replacing=len(self.content),
                )
            return {
                "role": "tool",
                "tool_call_id": self.tool_call_id or "",
                "content": content,
            }
        message: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = list(self.tool_calls)
        return message


@dataclass(frozen=True, slots=True)
class EvictionReport:
    """What eviction had to do to fit the window.

    ``tool_results_evicted`` counts results the model can no longer read, which is not quite the
    same as results that saved anything: a result already smaller than the shortest honest stub
    is replaced by one costing a few tokens more (:func:`_evicted_stub`). Counting the flips is
    the meaning the field is used for — how much of the transcript went — and ``tokens_before``
    against ``tokens_after`` is the honest measure of what it bought.
    """

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
        self._digest_entries: list[str] = []
        self._digest_dropped = 0
        self._messages: list[ContextMessage] = []
        self._turn = 0
        self._resumed_at: int | None = None
        self._head_tokens: tuple[str, int] | None = None
        self._digest_tokens: tuple[str, int] | None = None

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
        """Replace the compaction digest kept in place of dropped turns.

        Takes rendered text because that is what a caller holds between turns — the agent keeps
        one string per session — and splits it back into the entries it was rendered from, so
        that a digest arriving at its cap sheds its oldest entry rather than the whole of itself.
        Text that is not in that shape is kept whole as a single entry rather than discarded.
        """
        self._digest_dropped, self._digest_entries = _parse_digest(digest)
        self._trim_digest()

    @property
    def digest(self) -> str:
        """The decisions-and-open-questions digest, empty when nothing has been compacted."""
        if not self._digest_entries:
            return ""
        if not self._digest_dropped:
            return "\n".join(self._digest_entries)
        noun = "entry" if self._digest_dropped == 1 else "entries"
        marker = _DIGEST_DROPPED.format(count=self._digest_dropped, noun=noun)
        return "\n".join([marker, *self._digest_entries])

    @property
    def digest_cap(self) -> int:
        """Characters the digest may occupy: :data:`DIGEST_BUDGET_SHARE` of the budget.

        In characters because that is what is being trimmed and the trim runs on every compaction;
        converted at :data:`_CHARS_PER_TOKEN`, which is the same approximation the counter falls
        back to and is close enough for a bound whose job is to exist rather than to be exact.
        """
        return max(1, int(self._budget * DIGEST_BUDGET_SHARE * _CHARS_PER_TOKEN))

    @property
    def carry_cap(self) -> int:
        """Characters the carried span may hold: :data:`CARRY_BUDGET_SHARE` of the budget.

        In characters for the reason :attr:`digest_cap` gives: characters are what is being
        trimmed, the trim runs on every suspend, and converting at :data:`_CHARS_PER_TOKEN` is
        close enough for a bound whose job is to exist rather than to be exact.
        """
        return max(1, int(self._budget * CARRY_BUDGET_SHARE * _CHARS_PER_TOKEN))

    def _append_digest(self, entry: str) -> None:
        """Add one compacted turn to the digest and bring it back inside its cap."""
        if not entry:
            return
        self._digest_entries.append(entry)
        self._trim_digest()

    def _trim_digest(self) -> None:
        """Drop digest entries, oldest first, until the digest fits :attr:`digest_cap`.

        Nothing else in the window evicts these. The digest is counted in :meth:`_fixed_tokens`,
        so it raises :meth:`token_total`, so it trips the caller's compaction threshold sooner,
        so it is appended to sooner: an accumulator that accelerates itself. Left alone it ends a
        session outright — once head plus digest exceeds the budget, :meth:`fit` evicts every tool
        result, finds no turn left to drop, and warns, on that turn and on every turn after it,
        until ``/new``. Capping each entry cannot fix that; only capping the sum can.

        Oldest first, because the newest entries are the ones the next turn is most likely to
        want, and what goes leaves a marker rather than a silence: a model that can see it has
        lost the early conversation can ask, where one that cannot will confidently talk about a
        workbook it no longer remembers reading.

        The newest entry always survives, whatever the cap. One entry is itself bounded — see
        :data:`MAX_DIGEST_PROSE_CHARS` — so a bounded number of bounded entries is still bounded,
        and a budget too small to hold even one of them is better served by the last thing that
        happened than by a marker saying something did.
        """
        cap = self.digest_cap
        while len(self._digest_entries) > 1 and len(self.digest) > cap:
            self._digest_entries.pop(0)
            self._digest_dropped += 1

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

    def add_tool_result(
        self,
        *,
        tool_call_id: str,
        name: str,
        content: str,
        arguments: Mapping[str, Any] | None = None,
        shape: str | None = None,
    ) -> None:
        """Append a tool result, the first thing eviction will reach for.

        Args:
            tool_call_id: The id of the call this answers. The endpoint rejects a mismatch.
            name: The tool that produced it.
            content: The result as the model will read it.
            arguments: What the tool was called with, kept so eviction can name the call rather
                than leaving an anonymous hole. Rendered capped at
                :data:`MAX_EVICTED_ARGUMENT_CHARS` and never sent while the result is intact.
            shape: A short description of what came back — "37 operations, 4.2KB". Plain text
                supplied by the caller: this module does not know the tool surface and must not
                import it.
        """
        self._messages.append(
            ContextMessage(
                role="tool",
                content=content,
                turn=self._turn,
                kind="tool_result",
                tool_call_id=tool_call_id,
                tool_name=name,
                tool_arguments=dict(arguments) if arguments else None,
                result_shape=shape,
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

        A turn's cost is mostly its tool calls, and :meth:`load_history` cannot bring them back:
        the server persists prose, which is the right thing to persist and the wrong thing to
        resume from. Handing these messages to the next window keeps the expensive half of the
        turn instead of making the model re-read the workbook to rediscover what it already knew.
        That is as true of a turn that answered as of one that stopped early — the caller decides
        which, and how much of what it carries is still true (:meth:`ContextMessage.evicted`).

        Every tool call leaves here answered. A chat completion is rejected outright when an
        assistant message carrying ``tool_calls`` is not followed by a result for each of them,
        and a turn abandoned between dispatching a call and recording its result is exactly how
        that happens, so anything unanswered is filled in with a note saying so.

        A window that was itself resumed carries the whole span back out, not just its newest
        turn. Work that survived one pause has to survive the second: a conversation that stops
        three times is one piece of work, and a hand-off that only ever remembered the most recent
        leg would quietly drop the first two. The span is bounded rather than unbounded, though —
        see :meth:`_bounded`, which is what stops a session that carries on every turn from
        accumulating a transcript it never lets go of.

        What leaves is a copy, stamped with its age (:attr:`ContextMessage.carried_age`) so that
        re-dating on the way back in cannot make it young again.

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
                        carried_age=message.carried_age,
                    )
                )
        aged = [replace(message, carried_age=message.age_at(self._turn)) for message in carried]
        return tuple(self._bounded(aged))

    def _bounded(self, carried: Sequence[ContextMessage]) -> list[ContextMessage]:
        """Trim a span to what is worth handing on, in whole legs from the oldest.

        Three bounds asking three different questions. :meth:`_recut` asks how old a result is and
        is the one that runs in the ordinary case; :data:`MAX_CARRIED_MESSAGES` and
        :attr:`carry_cap` ask how many messages and how many characters have accumulated whatever
        their age, which is the shape a single very long turn has (:meth:`_capped`).
        """
        return self._capped(self._recut(list(carried)))

    def _recut(self, carried: list[ContextMessage]) -> list[ContextMessage]:
        """Stub and drop the oldest legs of a span, on one turn in :data:`CARRY_BLOCK_TURNS`.

        The horizons first, because the arithmetic is off by one and deliberately so.
        :meth:`resume` re-dates a carried message and :meth:`_age_out_tool_results` adds the turn
        back before judging, so a message stamped ``carried_age`` here is read at
        ``carried_age + 1``: content survives for ``evict_tool_results_after_turns`` turns, and the
        leg stamped one short of that arrives already expired and renders as the stub naming its
        call. That extra turn is signal rather than an accident — "this is gone, ask again if you
        still want it" is worth one turn of a few tokens, where a result that simply vanished
        leaves the model to guess whether it ever existed. So the cut is at
        ``stub_at = evict_after - 1`` and the stub is stamped here, by the window that knows the
        span's shape, rather than left for the receiving window to discover.

        The cadence is the other half, and it is what :data:`CARRY_BLOCK_TURNS` buys. Stubbing one
        leg and dropping one leg on every turn moves the boundary between the span and the
        flattened history on every turn, and a prefix cache keeps nothing behind a boundary that
        moves. So both happen together, once per block: the legs stubbed at the last re-cut are
        dropped, and the ``block`` oldest live legs are stubbed to take their place. Between
        re-cuts the span gains a leg at its end and changes nowhere else, so the whole of the
        previous turn's prompt is a prefix of this one's.

        Stubbing ``block`` legs where one is due is what a block costs. The horizon becomes a
        ceiling rather than an exact figure: content lasts ``evict_after`` turns for the leg that
        was due and as few as ``evict_after - block + 1`` for the youngest one caught with it.
        Early rather than late, so a result is never read after the horizon the user set, and it is
        always the oldest legs in the span that pay.

        A re-cut is due when the oldest leg still holding content has reached ``stub_at`` — one
        turn before the receiving window would stub it anyway, so nothing is ever kept past the
        horizon the user set — or, for a span with nothing live left in it, when its oldest leg has
        aged a whole block past that. Without the second clause a span of prose and stubs alone
        would never be cut and would grow for the rest of the session.
        """
        stub_at = self._evict_after - 1
        block = min(CARRY_BLOCK_TURNS, max(1, stub_at))
        oldest = max((message.carried_age for message in carried), default=-1)
        live = max(
            (
                message.carried_age
                for message in carried
                if message.kind == "tool_result" and not message.evicted
            ),
            default=-1,
        )
        if live < stub_at and oldest < stub_at + block:
            return carried
        kept = [message for message in carried if message.carried_age <= stub_at]
        for message in kept:
            if message.kind == "tool_result" and message.carried_age > stub_at - block:
                message.evicted = True
        return kept

    def _capped(self, kept: list[ContextMessage]) -> list[ContextMessage]:
        """Drop whole legs, oldest first, until the span fits both hard bounds.

        Whole legs, never part of one, for the reason :meth:`_drop_oldest_turn` gives: an assistant
        message whose results were trimmed out from under it is rejected by the endpoint. Re-dating
        flattens a resumed span onto one turn index, so the leg boundaries are recovered from the
        stamped age instead — every message of one leg carries the same one.

        Both bounds stop at the last leg standing, because there is nothing smaller to *drop*. What
        is left there is evicted instead: one fifty-step turn of results at the payload cap is a
        single leg of three quarters of a million tokens, comfortably inside the message count and
        far past any budget, and handing it on unchanged would tokenise every byte of it so that
        :meth:`fit` could throw the lot away at the far end. Evicting keeps the shape the endpoint
        requires — a result against every id — and the content is collapsed to the stub rather than
        merely hidden behind it, so the bound holds on what the session *holds* and not only on
        what it sends. A span left with nothing current in it is then dropped whole by its caller,
        which is the right end for a turn too large to carry.

        Dropping here moves the boundary between the span and the flattened history off the block
        cadence :meth:`_recut` keeps it on, so it costs the cached prefix a turn. That is the right
        trade under duress and the wrong one as a routine, which is why these are bounds and the
        cadence is the policy.
        """
        ages = sorted({message.carried_age for message in kept}, reverse=True)
        while len(ages) > 1 and (
            len(kept) > MAX_CARRIED_MESSAGES or _span_chars(kept) > self.carry_cap
        ):
            oldest = ages.pop(0)
            kept = [message for message in kept if message.carried_age != oldest]

        chars = _span_chars(kept)
        for message in kept:
            if chars <= self.carry_cap:
                break
            if message.kind != "tool_result":
                continue
            stub = _evicted_stub(
                message.tool_name,
                message.tool_arguments,
                message.result_shape,
                replacing=len(message.content),
            )
            # Including results this span has already stubbed: those still hold their payload, and
            # what is being bounded here is what the session holds. Idempotent — a stub rendered
            # against its own length comes back as itself.
            if len(stub) >= len(message.content):
                continue
            chars += len(stub) - len(message.content)
            message.content = stub
            message.evicted = True
        return kept

    def resume(self, messages: Sequence[ContextMessage]) -> int:
        """Seed the window with a suspended turn, tool traffic and all, and return its index.

        The messages are re-dated into the turn they are resumed as rather than kept at the index
        they were suspended from. That is about *order*: this window numbers its own turns from
        one, so a message left at the index it was suspended from would sort in front of history
        that is older than it, and the conversation would read out of sequence.

        Age is not taken from that index. It rides along in
        :attr:`ContextMessage.carried_age`, stamped by :meth:`suspend`, and every judgement about
        age goes through :meth:`ContextMessage.age_at`. A turn hands its tool traffic on whether or
        not it answered, so re-dating happens on every turn of a long session — and an age read off
        the re-dated index would restart at nothing each time, leaving
        :meth:`_age_out_tool_results` unable to evict anything it had ever preserved.

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
        messages: list[dict[str, Any]] = [{"role": "system", "content": self._head()}]
        if self._digest_entries:
            messages.append({"role": "system", "content": self._digest_message()})
        messages.extend(message.to_openai() for message in self._messages)
        return messages

    def _head(self) -> str:
        return "\n\n".join((self._system, *self._pinned))

    def _digest_message(self) -> str:
        return "## Earlier in this conversation (compacted)\n\n" + self.digest

    def _head_tokens_count(self) -> int:
        """The cost of the system head, counted only when the pinned blocks have changed.

        The head is the largest single message in the window and the one that changes least often
        -- once per turn, when the pinned blocks are rebuilt -- so counting it on every pass of
        eviction was most of the work for none of the information.
        """
        head = self._head()
        if self._head_tokens is None or self._head_tokens[0] != head:
            self._head_tokens = (
                head,
                self._counter.count_message({"role": "system", "content": head}),
            )
        return self._head_tokens[1]

    def _digest_tokens_count(self) -> int:
        """The cost of the digest message, counted only when the digest has changed."""
        if not self._digest_entries:
            return 0
        digest = self._digest_message()
        if self._digest_tokens is None or self._digest_tokens[0] != digest:
            self._digest_tokens = (
                digest,
                self._counter.count_message({"role": "system", "content": digest}),
            )
        return self._digest_tokens[1]

    def _fixed_tokens(self) -> int:
        """The cost of everything eviction cannot touch: the system head and the digest.

        Kept as two counts rather than one because when the window will not fit, which of them is
        responsible is the whole of the diagnosis.
        """
        return self._head_tokens_count() + self._digest_tokens_count()

    def token_total(self) -> int:
        """The token cost of the window as it currently stands."""
        return self._fixed_tokens() + sum(
            message.tokens(self._counter) for message in self._messages
        )

    # ── eviction ─────────────────────────────────────────────────────────────────────────

    def _age_out_tool_results(self) -> None:
        """Evict tool results older than the configured number of turns, budget or no budget.

        A ``sample_data`` result from six turns ago is almost never load-bearing and re-fetching
        it costs one call, so this happens on age alone rather than waiting for pressure.

        Age is :meth:`ContextMessage.age_at`, never ``message.turn``. A carried result is re-dated
        into the turn it was resumed as (:meth:`resume`), so reading its index would say it was
        born a turn ago however many turns it has actually survived — and since a turn hands its
        traffic on whether or not it answered, that would disarm this method for the whole of a
        long session.

        In practice this now finds little to do. :meth:`_recut` stamps the stub on the sending side
        so that the span it hands over holds still for a whole block, which means a carried result
        arrives either inside the horizon or already evicted. This stays the backstop: it is what
        makes the horizon true of any span, including one built by a window configured differently
        from this one, and it costs a walk of a list that is walked anyway.
        """
        for message in self._messages:
            if (
                message.kind == "tool_result"
                and not message.evicted
                and message.age_at(self._turn) >= self._evict_after
            ):
                message.evicted = True

    def fit(self) -> EvictionReport:
        """Evict until the window fits the budget, in the fixed order from PLAN M4."""
        before = self.token_total()
        if before <= self._budget:
            return EvictionReport(tokens_before=before, tokens_after=before)

        evicted = 0
        summarised = 0
        # Tracked incrementally rather than recomputed: evicting one message changes the total by
        # exactly that message's delta, and asking the whole window again for every candidate is
        # what made fitting a large context quadratic.
        running = before
        for message in self._messages:
            if running <= self._budget:
                break
            if message.kind == "tool_result" and not message.evicted:
                was = message.tokens(self._counter)
                message.evicted = True
                running += message.tokens(self._counter) - was
                evicted += 1

        # Dropping a turn folds it into the digest, so the fixed part moves too and the running
        # total has to be taken afresh. There are few of these; there are many of the above.
        while running > self._budget and self._can_drop_a_turn():
            summarised += self._drop_oldest_turn()
            running = self.token_total()

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
            # Naming the culprit rather than assuming it. This used to blame the pinned blocks
            # outright, which sent whoever read it to the wrong function whenever the digest was
            # the larger half -- and an unbounded digest was exactly how a session got here.
            head = self._head_tokens_count()
            digest = self._digest_tokens_count()
            logger.warning(
                "context is still %d tokens over a %d budget after eviction; %s is the larger "
                "fixed block (pinned blocks %d tokens, digest %d)",
                after - self._budget,
                self._budget,
                "the pinned blocks" if head >= digest else "the digest",
                head,
                digest,
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
        self._append_digest(summarise_messages(dropped))
        return 1

    def compact(self) -> str:
        """Summarise everything but the current turn to a digest and drop the rest.

        PLAN M4 asks for this on ``/new`` and on a budget breach. It is done deterministically
        rather than with a model call: a compaction that needs the endpoint cannot run when the
        endpoint is the thing that is failing, and what is worth keeping — what was asked, what
        the model wrote down about what it found, what is still open — can be lifted out of the
        transcript verbatim, without judgement.

        One entry per turn, as :meth:`_drop_oldest_turn` produces. Handing the whole span to
        :func:`summarise_messages` in one call cost a turn's findings each: that function retains
        one block of prose and labels its entry with the first turn it was given, so five turns
        came back as the fifth turn's answer attributed to the first, with the other four gone.
        This is the path that runs — the caller compacts at a fraction of the budget every turn,
        so :meth:`_drop_oldest_turn` rarely gets the chance — which made it the path where the
        loss was largest.
        """
        keep = [message for message in self._messages if message.turn >= self._turn]
        dropped = [message for message in self._messages if message.turn < self._turn]
        for turn in sorted({message.turn for message in dropped}):
            self._append_digest(
                summarise_messages([message for message in dropped if message.turn == turn])
            )
        self._messages = keep
        return self.digest

    def reset(self) -> None:
        """Forget the conversation entirely, digest included. The ``/new`` path."""
        self._messages = []
        self._digest_entries = []
        self._digest_dropped = 0
        self._turn = 0
        self._resumed_at = None

    def __repr__(self) -> str:
        return (
            f"ConversationWindow(messages={len(self._messages)}, turn={self._turn}, "
            f"budget={self._budget})"
        )


def _span_chars(messages: Sequence[ContextMessage]) -> int:
    """The raw size of a span in characters.

    Counted on ``content`` rather than on what the message renders as, so an evicted result is
    measured at what it still occupies in memory rather than at the handful of characters its stub
    sends. That is the honest number for :attr:`ConversationWindow.carry_cap`, which bounds what a
    session holds as much as what it costs to send.
    """
    return sum(len(message.content) for message in messages)


def summarise_messages(messages: Sequence[ContextMessage]) -> str:
    """Return a decisions-and-open-questions digest entry for a run of messages.

    Deliberately mechanical, and deterministic by design: compaction runs precisely when the
    window will not fit, which includes the case where the model endpoint is the thing that is
    failing, so it cannot itself depend on a model call. Everything below is lifted out of the
    transcript verbatim.

    What survives is what the next turn cannot rediscover cheaply: what the user asked for, the
    model's own account of what it found, and anything left hanging as a question. The tool tally
    comes last on purpose — it says where the work went, not what it produced, and a turn reduced
    to ``inspect_workbookx3`` is a turn the model has no choice but to redo.

    The retained prose is bounded by :data:`MAX_DIGEST_PROSE_LINES` and
    :data:`MAX_DIGEST_PROSE_CHARS`, and marked where it was cut. A digest entry that grew with
    the answer it summarises would be no use to a token budget. Bounding the entry is not the
    same as bounding the digest, though, and cannot be — how many entries there are is the
    window's business, not this function's, and it bounds them in
    :meth:`ConversationWindow._trim_digest`.
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
    if questions:
        parts.append("open: " + "; ".join(_short(item, 120) for item in questions[:3]))
    if tools:
        counts: dict[str, int] = {}
        for name in tools:
            counts[name] = counts.get(name, 0) + 1
        parts.append(
            "tools: " + ", ".join(f"{name}x{count}" for name, count in sorted(counts.items()))
        )
    prose = _retained_prose(messages)
    if not parts and not prose:
        return ""
    turn = messages[0].turn if messages else 0
    head = f"- turn {turn}: " + " | ".join(parts) if parts else f"- turn {turn}:"
    if not prose:
        return head
    lines = prose.splitlines()
    return "\n".join([head, "  said: " + lines[0], *(f"  {line}" for line in lines[1:])])


def _retained_prose(messages: Sequence[ContextMessage]) -> str:
    """Return the assistant's own account of a run, bounded and marked where it was cut.

    The model's terminal answer is picked structurally, not by size. A step that is going to call
    a tool emits its narration *with* the calls attached, and only the step that ends the turn
    emits prose with none — so the last assistant message carrying no ``tool_calls`` is the
    answer, whatever it is next to. Length was standing in for that, and it read backwards
    exactly where it mattered: "I am going to check the Calc sheet, then look at how Report!B4 is
    totalled" beat "Calc!H2:H400, keyed on the rating column", keeping the plan and discarding
    the finding.

    Length is still the fallback, for a run in which nothing has empty ``tool_calls`` — a turn cut
    short by a failing tool, say. Messages seeded by :meth:`ConversationWindow.load_history` and
    the pause message both have empty ``tool_calls`` too, and both are genuinely the last word of
    what they belong to, so the structural rule holds for them as it stands.

    Blank lines are dropped before the line cap is applied, which does mean the cap counts
    differently from what the model wrote. That is deliberate: the cap exists to bound cost, and
    spending a third of it on paragraph breaks in a digest entry nothing renders as Markdown buys
    nothing back.
    """
    said = [
        message for message in messages if message.kind == "assistant" and message.content.strip()
    ]
    if not said:
        return ""
    terminal = next(
        (message.content.strip() for message in reversed(said) if not message.tool_calls), None
    )
    if terminal is not None:
        chosen = terminal
    else:
        candidates = [message.content.strip() for message in said]
        chosen = next(
            (text for text in reversed(candidates) if len(text) >= _SUBSTANTIAL_PROSE_CHARS),
            max(candidates, key=len),
        )

    lines = [line.rstrip() for line in chosen.splitlines() if line.strip()]
    kept = lines[:MAX_DIGEST_PROSE_LINES]
    elided = len(kept) < len(lines)
    text = "\n".join(kept)
    if len(text) > MAX_DIGEST_PROSE_CHARS:
        text = text[:MAX_DIGEST_PROSE_CHARS].rstrip()
        elided = True
    return f"{text}\n{_DIGEST_ELISION}" if elided else text


def _parse_digest(text: str) -> tuple[int, list[str]]:
    """Split a rendered digest back into ``(entries already dropped, entries)``.

    The digest crosses a turn boundary as text — the agent holds one string per session — so a
    window that needs to drop the oldest of its entries has to be able to find them again. Every
    entry :func:`summarise_messages` produces starts at an unindented ``- `` and continues in
    lines indented by two spaces, which is enough to split on. Anything else is kept as one entry
    rather than dropped: a digest written by an older build is worth less than a parsed one and a
    great deal more than nothing.
    """
    dropped = 0
    entries: list[str] = []
    for line in text.splitlines():
        if line.startswith(_DIGEST_ENTRY_MARKER):
            marker = _DIGEST_DROPPED_PATTERN.match(line)
            if marker is not None:
                dropped += int(marker.group(1))
                continue
            entries.append(line)
        elif entries:
            entries[-1] += "\n" + line
        elif line.strip():
            entries.append(line)
    return dropped, entries


_ = field  # re-exported dataclass helper kept importable for callers building ContextMessages
