"""What this eval sends a model when it asks for one cell body: the product's prompt, near enough.

There was no cell-generation prompt to reuse. ``src/kedge/agent/prompts/`` holds the *chat* system
prompt, assembled by :func:`~kedge.agent.prompts.build_system_prompt`, and there were three
options here of which only one measures anything a user will meet.

*Writing a prompt here* would have been easiest and is the trap. An eval whose prompt differs
from what the product sends measures a prompt that ships nowhere: it would go green on a change
that broke every real conversion, and red on a change that fixed one. The number would be about
this file.

*Sending the whole system prompt* is wrong for one specific reason. ``tools.md`` describes six
notebook tools, five workbook tools and a validation flow reached through ``propose_cell`` -- and
this seam offers none of them. A model told to call ``list_cells`` before editing, with no
``list_cells`` to call, answers with a tool call in prose; the harness sees no cell body and
records ``EMPTY``, and a *harness* mismatch is attributed to the *model*.

So :data:`CELL_PROMPT_PARTS` is :data:`~kedge.agent.prompts.SYSTEM_PARTS` **minus** ``tools.md``,
subtracted by name rather than listed out, so a part added to the product prompt tomorrow is in
this eval tomorrow without anybody remembering. Everything that makes a *cell* good -- the role,
marimo's single-definition rule, the polars house style, the Excel semantics that go silently
wrong, the translation vocabulary -- is sent verbatim, byte for byte, from the files the product
ships.

**But dropping the file dropped the rules with it.** ``tools.md``'s ``## Validation`` section is
where the product tells a model what :class:`~kedge.agent.validate.Policy` refuses -- shell,
network, database connections, writes outside the working directory, credentials in literals --
and this seam still runs the real ``Policy``. A model writing ``duckdb.connect(...)`` is warned
in the product and ambushed here, which *punishes models the product would pass*. So that one
section is carried across by :func:`policy_rules`, quoted out of the shipped file at load time
rather than paraphrased: a paraphrase is a second copy of a rule, and a second copy rots. It
raises rather than returning nothing if the heading ever moves, because silently sending no rules
is the exact failure it exists to prevent.

What replaces the rest of ``tools.md`` is :data:`CELL_TASK`, passed through
``build_system_prompt``'s own ``extra=`` hook, which exists for exactly this: stating something
about the caller's situation without editing a shipped file. It describes the seam and nothing
else -- one cell, the body only, the gate and its cap -- and it deliberately restates no rule
that a shipped part already carries.

## The message shape is the product's, roles included

:func:`cell_messages` sends **one system message** holding the prompt and the pinned blocks
together, then a user message holding the cell. That is not a stylistic choice: it is what
:meth:`kedge.agent.context.ConversationWindow._render` does, joining ``_head()`` -- the system
prompt and every pinned block -- into a single ``{"role": "system"}`` entry before any
conversation message. Splitting them here would put some eight thousand tokens in a different
role from production, and model families weight the instruction hierarchy differently, so the
split would inject a per-model-family confound into precisely the number this eval exists to
produce. ``tests/unit/test_evals_convert.py`` asserts the placement against a real
``ConversationWindow`` rather than against a literal, so the two cannot drift apart quietly.

The pinned blocks are in ``KedgeAgent._window_for``'s own least-volatile-first order --
:func:`~kedge.agent.context.build_analysis_block`, then
:func:`~kedge.agent.context.build_plan_block`, then
:meth:`~kedge.agent.context.NameRegistry.render`. The one pinned block left out is
:meth:`~kedge.agent.context.NotebookState.render`: it instructs the reader to call ``list_cells``
and it exists to describe a live kernel's staleness, which is not a thing that can happen here.

Three honest differences remain. A cell body reaches ``propose_cell`` as a tool argument, so the
product never has to strip a Markdown fence off one; here it arrives as prose and
``cellgen._body_of`` strips it. The real loop's retry rides on a tool result rather than a user
turn. And ``role.md`` names ``propose_cell``, ``probe`` and ``inspect_workbook`` in passing, which
no subtraction short of editing a copy of a shipped file removes -- so :data:`CELL_TASK` says
there are no tools in as many words, and is appended *after* every shipped part, where a model
reading in order meets it last.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kedge.agent.context import (
    build_analysis_block,
    build_plan_block,
)
from kedge.agent.prompts import SYSTEM_PARTS, build_system_prompt, load_prompt

if TYPE_CHECKING:
    from collections.abc import Sequence

    from kedge.agent.context import NameRegistry
    from kedge.analysis.model import WorkbookAnalysis
    from kedge.notebook.scaffold import ScaffoldCell
    from kedge.plan.model import ProcessPlan

logger = logging.getLogger(__name__)

__all__ = [
    "CELL_PROMPT_PARTS",
    "CELL_TASK",
    "POLICY_SOURCE",
    "TODO_MARKER",
    "cell_messages",
    "policy_rules",
    "system_prompt",
]

TODO_MARKER = "TODO(kedge)"
"""What :func:`kedge.notebook.scaffold._stage_cell` writes above every body it left unwritten."""

CELL_PROMPT_PARTS: tuple[str, ...] = tuple(part for part in SYSTEM_PARTS if part != "tools.md")
"""The product's system prompt parts, less the one describing tools this seam does not offer.

Subtracted rather than enumerated on purpose. A new part added to
:data:`~kedge.agent.prompts.SYSTEM_PARTS` -- another file of Excel semantics, say -- reaches this
eval the moment it ships, with nobody having to remember that a second list exists. Listing the
five wanted files here would have made this the place model quality silently stopped tracking
the product.
"""

POLICY_SOURCE = ("tools.md", "## Validation")
"""Where the policy rules are quoted from: the shipped file, and the heading inside it."""


def policy_rules(source: tuple[str, str] = POLICY_SOURCE) -> str:
    """The shipped ``## Validation`` section, quoted verbatim out of ``tools.md``.

    Dropping ``tools.md`` from the prompt dropped the only statement of what
    :class:`~kedge.agent.validate.Policy` refuses, while the gate itself stayed. A model that
    reaches for ``duckdb.connect(...)`` is warned in the product and ambushed here, so this eval
    would mark down models the product would pass -- and the fix must not be a paraphrase, because
    a paraphrase is a second copy of a rule that drifts out of step with the first and nobody
    notices until a model is blamed for it.

    Args:
        source: The prompt file, and the Markdown heading whose section to quote.

    Returns:
        The section, heading line included, exactly as the shipped file holds it.

    Raises:
        LookupError: when the heading is not in the file. A prompt assembled without the rules
            would run, score, and punish the wrong thing; refusing to build one is the whole
            point of checking.
    """
    name, heading = source
    text = load_prompt(name)
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip() == heading), None)
    if start is None:
        msg = (
            f"the prompt part {name!r} no longer has a {heading!r} section, so the cell-generation "
            f"eval cannot tell a model what the validation gate refuses. Point POLICY_SOURCE at "
            f"wherever those rules live now -- do not restate them here, or this eval will start "
            f"marking down models the product would have warned."
        )
        raise LookupError(msg)
    end = next(
        (
            index
            for index in range(start + 1, len(lines))
            if lines[index].startswith("## ") or lines[index].startswith("# ")
        ),
        len(lines),
    )
    return "\n".join(lines[start:end]).rstrip()


CELL_TASK = """
# This turn: one cell body, and nothing else

You are not in the chat pane and you have no tools. There is no `list_cells`, no `probe`, no
`propose_cell`; there is one cell, and your reply *is* its body. Everything you are given about
the workbook and the plan is below -- there is no way to ask for more, so work from it and say
what you assumed rather than guessing quietly.

The notebook has already been scaffolded from the approved plan. Its structure is settled: the
cells exist, they are named, and the ones above this one have been written. What is left is the
translation inside this cell, marked `TODO(kedge)`.

Reply with **Python only**: the lines that replace the placeholder body, at zero indentation, no
Markdown fence, no explanation before or after. The comment header above the `TODO(kedge)` line
is kept for you -- do not repeat it. The cell must define the name the placeholder defines,
because the cells below read it; give anything else you need a leading underscore, which marimo
treats as cell-local.

Your reply goes through kedge's validation gate before it is accepted. If it is rejected you get
the violations back and a limited number of further attempts -- read them and fix the cause,
because a cell that never passes is a hole in the notebook rather than a cell that came out
badly. The gate's rules are below, quoted from the file the product sends: the tool names in
them do not apply here, but every rule does, because it is the same gate.
""".strip()
"""The one block this eval adds, appended through ``build_system_prompt(extra=...)``.

It states the mechanics of the seam and deliberately nothing else. Every rule about *how to
translate* -- polars, LazyFrames, ``kedge.xl``, ``kedge.sql``, the single-definition rule -- is
in the shipped parts above it, and restating one here would create a second copy to keep in step
with the first. It ends up out of step; they always do. The rules of the gate itself are not
restated either: :func:`policy_rules` quotes them.
"""


def system_prompt(*, parts: Sequence[str] = CELL_PROMPT_PARTS, extra: Sequence[str] = ()) -> str:
    """The system prompt for the cell-generation seam.

    Args:
        parts: Which shipped prompt files to send. Defaults to :data:`CELL_PROMPT_PARTS`.
        extra: Blocks appended after :data:`CELL_TASK` and the quoted policy rules, for a caller
            with something further to say.

    Returns:
        The assembled prompt, built by the product's own
        :func:`~kedge.agent.prompts.build_system_prompt`.

    Raises:
        LookupError: when the policy rules can no longer be quoted. See :func:`policy_rules`.
    """
    return build_system_prompt(parts=parts, extra=(CELL_TASK, policy_rules(), *extra))


def cell_messages(
    *,
    cell: ScaffoldCell,
    plan: ProcessPlan,
    analysis: WorkbookAnalysis | None,
    registry: NameRegistry,
    history: Sequence[tuple[str, str]] = (),
) -> list[dict[str, str]]:
    """The messages for one hole, in the roles the product puts them in.

    One system message carrying the prompt and the pinned blocks together, then a user message
    carrying the cell, then any retry traffic. See the module docstring: this mirrors
    :meth:`kedge.agent.context.ConversationWindow._render`, and sending the pinned blocks as a
    user message instead would move eight thousand tokens into a role production never puts them
    in -- a confound in the one number this eval exists to produce.

    Args:
        cell: The scaffolded cell, comment header and placeholder body together. Sent whole: the
            intent, the sources, the assumptions and the Excel-pattern hint are the brief.
        plan: The approved plan, rendered as the model's standing instructions.
        analysis: The workbook analysis. ``None`` renders the block that says so, which is what
            the product does too.
        registry: Every public name the notebook already owns, including the holes filled above
            this one.
        history: Prior ``(role, content)`` pairs for this cell -- the rejected body and the
            gate's verdict on it.

    Returns:
        The message list, ready for a :class:`~kedge.plan.propose.CompletionRequest`.
    """
    head = "\n\n".join(
        [
            system_prompt(),
            build_analysis_block(analysis),
            build_plan_block(plan),
            registry.render(),
        ]
    )
    task = (
        f"## The cell to write: `{cell.name}`\n\n"
        f"This is the scaffolded cell as it stands. Everything above the "
        f"`{TODO_MARKER}` line is kept as it is; reply with what replaces the "
        f"placeholder below it, Python only.\n\n"
        f"```python\n{cell.code}\n```"
    )
    messages = [
        {"role": "system", "content": head},
        {"role": "user", "content": task},
    ]
    messages.extend({"role": role, "content": content} for role, content in history)
    return messages
