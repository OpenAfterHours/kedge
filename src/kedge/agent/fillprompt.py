"""What kedge sends a model when it asks for one cell body: the chat prompt, minus the tools.

The chat seam and the conversion seam ask the same model to do the same job under different
mechanics. ``src/kedge/agent/prompts/`` holds the *chat* system prompt, assembled by
:func:`~kedge.agent.prompts.build_system_prompt`, and there were three ways to give the conversion
driver a prompt of its own, of which only one measures anything a user will meet.

*Writing a prompt here* would have been easiest and is the trap. A second statement of a rule
drifts out of step with the first and nobody notices until a cell is blamed for it. It also breaks
the eval, which sends what this module assembles: an eval whose prompt differs from the product's
measures a prompt that ships nowhere, going green on a change that breaks every real conversion
and red on one that fixes one.

*Sending the whole system prompt* is wrong for one specific reason. ``tools.md`` describes six
notebook tools, five workbook tools and a validation flow reached through ``propose_cell`` -- and
this seam offers none of them. ``kedge convert`` is headless: it writes through
:class:`~kedge.notebook.filedriver.FileNotebookDriver`, there is no kernel and there is no tool
call to make. A model told to call ``list_cells`` before editing, with no ``list_cells`` to call,
answers with a tool call in prose; the reply holds no cell body, and a *driver* mismatch is
recorded as a model failure.

So :data:`FILL_PROMPT_PARTS` is :data:`~kedge.agent.prompts.SYSTEM_PARTS` **minus** ``tools.md``,
subtracted by name rather than listed out, so a part added to the chat prompt tomorrow is in this
seam tomorrow without anybody remembering that a second list exists. Everything that makes a
*cell* good -- the role, marimo's single-definition rule, the polars house style, the Excel
semantics that go silently wrong, the translation vocabulary -- is sent verbatim, byte for byte,
from the files that ship.

**But dropping the file drops the rules with it.** ``tools.md``'s ``## Validation`` section is
where kedge states what :class:`~kedge.agent.validate.Policy` refuses -- shell, network, database
connections, writes outside the working directory, credentials in literals -- and this seam still
runs the real ``Policy``. A model writing ``duckdb.connect(...)`` is warned in the chat and
ambushed here, so a conversion would refuse cells the chat would have talked the model out of
writing. That one section is carried across by :func:`policy_rules`, quoted out of the shipped
file at load time rather than paraphrased. It raises rather than returning nothing if the heading
ever moves, because silently sending no rules is the exact failure it exists to prevent.

What replaces the rest of ``tools.md`` is :data:`FILL_TASK`, passed through
``build_system_prompt``'s own ``extra=`` hook, which exists for exactly this: stating something
about the caller's situation without editing a shipped file. It describes the seam and nothing
else -- one cell, the body only, the gate and its cap -- and it deliberately restates no rule that
a shipped part already carries.

## The message shape is the chat loop's, roles included

:func:`cell_messages` sends **one system message** holding the prompt and the pinned blocks
together, then a user message holding the cell. That is not a stylistic choice: it is what
:meth:`kedge.agent.context.ConversationWindow._render` does, joining the system prompt and every
pinned block into a single ``{"role": "system"}`` entry before any conversation message. Splitting
them here would put some eight thousand tokens in a different role from the chat, and model
families weight the instruction hierarchy differently -- so the split would inject a
per-model-family confound into the one seam whose whole argument is that it is the same prompt.

The pinned blocks are in :meth:`kedge.agent.loop.KedgeAgent._window_for`'s own
least-volatile-first order -- :func:`~kedge.agent.context.build_analysis_block`, then
:func:`~kedge.agent.context.build_plan_block`, then
:meth:`~kedge.agent.context.NameRegistry.render`. A prompt cache keys on the prefix, so anything
ahead of a block that changes stays cached; the registry is the one that changes between holes,
and it goes last. The one pinned block left out is
:meth:`~kedge.agent.context.NotebookState.render`: it instructs the reader to call ``list_cells``
and it exists to describe a live kernel's staleness, neither of which is a thing that can happen
here.

Three honest differences from the chat remain. A cell body reaches ``propose_cell`` as a tool
argument, so the chat never has to strip a Markdown fence off one; here it arrives as prose and
:func:`kedge.agent.fill._body_of` strips it. The chat's retry rides on a tool result rather than a
user turn. And ``role.md`` names ``propose_cell``, ``probe`` and ``inspect_workbook`` in passing,
which no subtraction short of editing a copy of a shipped file removes -- so :data:`FILL_TASK`
says there are no tools in as many words, and is appended *after* every shipped part, where a
model reading in order meets it last.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kedge.agent.context import build_analysis_block, build_plan_block
from kedge.agent.prompts import SYSTEM_PARTS, build_system_prompt, load_prompt
from kedge.errors import NotebookError
from kedge.notebook.scaffold import TODO_MARKER

if TYPE_CHECKING:
    from collections.abc import Sequence

    from kedge.agent.context import NameRegistry
    from kedge.analysis.model import WorkbookAnalysis
    from kedge.notebook.scaffold import ScaffoldCell
    from kedge.plan.model import ProcessPlan

logger = logging.getLogger(__name__)

__all__ = [
    "FILL_PROMPT_PARTS",
    "FILL_TASK",
    "POLICY_SOURCE",
    "TODO_MARKER",
    "PromptAssemblyError",
    "cell_messages",
    "policy_rules",
    "system_prompt",
]


class PromptAssemblyError(NotebookError, LookupError):
    """The prompt cannot be assembled from the shipped parts, so nothing is sent.

    Both bases earn their place. :class:`~kedge.errors.NotebookError` is what CONVENTIONS asks
    for and what makes this reach the command line as a message rather than a traceback --
    ``kedge convert`` catches ``KedgeError`` and nothing else, and what this seam is ultimately
    failing to produce is a notebook. ``LookupError`` is what it actually is: a heading that is no
    longer in the file. Callers written against either one keep working.
    """


FILL_PROMPT_PARTS: tuple[str, ...] = tuple(part for part in SYSTEM_PARTS if part != "tools.md")
"""The chat's system prompt parts, less the one describing tools this seam does not offer.

Subtracted rather than enumerated. A new part added to
:data:`~kedge.agent.prompts.SYSTEM_PARTS` -- another file of Excel semantics, say -- reaches this
seam the moment it ships. Listing the five wanted files here would have made this the place a
conversion silently stopped tracking the chat prompt.
"""

POLICY_SOURCE = ("tools.md", "## Validation")
"""Where the policy rules are quoted from: the shipped file, and the heading inside it."""


def policy_rules(source: tuple[str, str] = POLICY_SOURCE) -> str:
    """The shipped ``## Validation`` section, quoted verbatim out of ``tools.md``.

    Dropping ``tools.md`` from the prompt drops the only statement of what
    :class:`~kedge.agent.validate.Policy` refuses, while the gate itself stays. A model that
    reaches for ``duckdb.connect(...)`` is warned in the chat and ambushed here, so a conversion
    would refuse cells the chat would have talked the model out of writing -- and the fix must not
    be a paraphrase, because a paraphrase is a second copy of a rule that drifts out of step with
    the first and nobody notices until a cell is blamed for it.

    Args:
        source: The prompt file, and the Markdown heading whose section to quote.

    Returns:
        The section, heading line included, exactly as the shipped file holds it.

    Raises:
        PromptAssemblyError: when the heading is not in the file. A prompt assembled without the
            rules would run and would refuse cells it never warned about; refusing to build one is
            the whole point of checking.
    """
    name, heading = source
    text = load_prompt(name)
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip() == heading), None)
    if start is None:
        msg = (
            f"the prompt part {name!r} no longer has a {heading!r} section, so a conversion cannot "
            f"tell a model what the validation gate refuses. Point POLICY_SOURCE at wherever those "
            f"rules live now -- do not restate them here, or `kedge convert` will start rejecting "
            f"cells it never warned about."
        )
        raise PromptAssemblyError(msg)
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].startswith(("## ", "# "))),
        len(lines),
    )
    return "\n".join(lines[start:end]).rstrip()


FILL_TASK = """
# This turn: one cell body, and nothing else

This is not the chat pane and you have no tools. There is no `list_cells`, no `probe`, no
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
badly. The gate's rules are below, quoted from the file the chat sends: the tool names in them do
not apply here, but every rule does, because it is the same gate.
""".strip()
"""The one block this seam adds, appended through ``build_system_prompt(extra=...)``.

It states the mechanics of the seam and deliberately nothing else. Every rule about *how to
translate* -- polars, LazyFrames, ``kedge.xl``, ``kedge.sql``, the single-definition rule -- is in
the shipped parts above it, and restating one here would create a second copy to keep in step with
the first. It ends up out of step; they always do. The rules of the gate itself are not restated
either: :func:`policy_rules` quotes them.
"""


def system_prompt(*, parts: Sequence[str] = FILL_PROMPT_PARTS, extra: Sequence[str] = ()) -> str:
    """The system prompt for the cell-filling seam.

    Args:
        parts: Which shipped prompt files to send. Defaults to :data:`FILL_PROMPT_PARTS`.
        extra: Blocks appended after :data:`FILL_TASK` and the quoted policy rules, for a caller
            with something further to say.

    Returns:
        The assembled prompt, built by kedge's own
        :func:`~kedge.agent.prompts.build_system_prompt`.

    Raises:
        PromptAssemblyError: when the policy rules can no longer be quoted. See
            :func:`policy_rules`.
    """
    return build_system_prompt(parts=parts, extra=(FILL_TASK, policy_rules(), *extra))


def cell_messages(
    *,
    cell: ScaffoldCell,
    plan: ProcessPlan,
    analysis: WorkbookAnalysis | None,
    registry: NameRegistry,
    history: Sequence[tuple[str, str]] = (),
) -> list[dict[str, str]]:
    """The messages for one hole, in the roles the chat loop puts them in.

    One system message carrying the prompt and the pinned blocks together, then a user message
    carrying the cell, then any retry traffic. See the module docstring: this mirrors
    :meth:`kedge.agent.context.ConversationWindow._render`, and splitting them here would put some
    eight thousand tokens in a role the chat never puts them in.

    Args:
        cell: The scaffolded cell, comment header and placeholder body together. Sent whole: the
            intent, the sources, the assumptions and the Excel-pattern hint are the brief.
        plan: The approved plan, rendered as the model's standing instructions.
        analysis: The workbook analysis. ``None`` renders the block that says so, which is what
            the chat does too.
        registry: Every public name the notebook already owns, including the holes filled above
            this one.
        history: Prior ``(role, content)`` pairs for this cell -- the rejected body and the gate's
            verdict on it.

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
