"""What this eval sends a model when it asks for one cell body: the product's prompt, exactly.

This module used to assemble a prompt. It no longer does, and the reason is the sentence it has
carried in its own docstring since it was written:

    *An eval whose prompt differs from what the product sends measures a prompt that ships
    nowhere: it would go green on a change that broke every real conversion, and red on a change
    that fixed one.*

That became literally true. There was no cell-generation prompt to reuse when this was written, so
the assembly was done here; then :mod:`kedge.agent.fillprompt` shipped with the same assembly in
it, and the two texts drifted by six bytes -- "You are not in the chat pane" against "This is not
the chat pane", "the file the product sends" against "the file the chat sends". Six bytes is not
the problem. A copy that can drift by six bytes without anybody noticing is the problem, and the
number the whole exercise exists to produce -- what a model scores writing a whole conversion --
was being produced by a copy of the driver rather than by ``kedge convert``.

So the assembly is the product's and this module is a name map: the eval's vocabulary on the left,
:mod:`kedge.agent.fillprompt` on the right. There is no second implementation to keep in step, and
``tests/unit/test_evals_convert.py`` asserts that what the eval sends is byte-identical to what the
product sends -- which is now true by construction rather than by discipline.

The argument for the shape of that prompt has moved with it and is worth reading where it lives:
the product's parts minus ``tools.md``, subtracted by name; the ``## Validation`` section quoted
back out of the file that was dropped, because the gate stayed; and one task block appended last,
saying there are no tools, since ``role.md`` names three in passing and no subtraction removes
that.
"""

from __future__ import annotations

import logging

from kedge.agent.fillprompt import (
    FILL_PROMPT_PARTS,
    FILL_TASK,
    POLICY_SOURCE,
    PromptAssemblyError,
    cell_messages,
    policy_rules,
    system_prompt,
)
from kedge.notebook.scaffold import TODO_MARKER

logger = logging.getLogger(__name__)

__all__ = [
    "CELL_PROMPT_PARTS",
    "CELL_TASK",
    "POLICY_SOURCE",
    "TODO_MARKER",
    "PromptAssemblyError",
    "cell_messages",
    "policy_rules",
    "system_prompt",
]

CELL_PROMPT_PARTS = FILL_PROMPT_PARTS
"""The eval's name for :data:`kedge.agent.fillprompt.FILL_PROMPT_PARTS`. The same tuple object."""

CELL_TASK = FILL_TASK
"""The eval's name for :data:`kedge.agent.fillprompt.FILL_TASK`. The same string object."""
