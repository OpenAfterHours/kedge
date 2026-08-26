"""The agent: the tool surface, the validation gate, the context strategy, and the loop.

This package is PLAN M4. It is the only part of kedge that talks to a model endpoint, and the only
part that decides what a model is allowed to do to a notebook.

The chat loop is four modules, four jobs:

* :mod:`kedge.agent.tools` — the sixteen tools, and the row and payload caps every value-returning
  one of them passes through. The caps are structural: a tool cannot return a payload except via
  :meth:`~kedge.agent.tools.ToolRegistry.dispatch`, which caps it and audits it.
* :mod:`kedge.agent.validate` — the five-stage gate that runs before any generated code reaches the
  kernel: syntax, the marimo single-definition contract checked against the live graph, policy,
  output style, then hand the violations back and let the model retry, three times.
* :mod:`kedge.agent.context` — the name registry, the live notebook state, the token budget and the
  eviction order. Notebook state is rebuilt from the kernel every turn and never taken from history.
* :mod:`kedge.agent.loop` — :class:`~kedge.agent.loop.KedgeAgent`, which satisfies
  :class:`kedge.server.agent_seam.AgentLoop` and is a drop-in replacement for the server's
  ``ScriptedAgent``. It satisfies it structurally: nothing here imports ``kedge.server``, and the
  vocabulary a turn is conducted in lives in :mod:`kedge.turn`, below both packages.

There are two model-driving seams here, not one. The chat loop above is the interactive one; the
other is headless and runs to a termination condition. It is four modules of its own, cut by
altitude — the run, one attempt within it, what is said, and what came of it:

* :mod:`kedge.agent.fill` — the conversion driver behind ``kedge convert``: which of the
  ``TODO(kedge)`` holes the scaffolder left are asked about, in the scaffolder's own order, what a
  dead endpoint costs the rest of the run, and how an accepted body reaches a file. No kernel, no
  tool surface, no chat.
* :mod:`kedge.agent.fillhole` — one hole: what counts as one, what came back, and the two checks
  the driver makes that the gate does not — a name the cell was supposed to define, and a marker
  that survived the answer.
* :mod:`kedge.agent.fillprompt` — every word that seam sends: the shipped system prompt minus
  ``tools.md``, subtracted by name, with the gate's own rules quoted back out of the file that was
  dropped, plus the rejection block and the empty-reply nudge. ``evals/harness`` calls it rather
  than copying it, so the eval cannot measure a prompt that ships nowhere.
* :mod:`kedge.agent.fillreport` — what happened: the outcome vocabulary and its rendering. Seven
  outcomes rather than two, because a transport failure and a model writing bad code are different
  facts about different things, and every one of them stays in the denominator.

Plus :mod:`kedge.agent.audit`, which records what left the machine — tool, sheet, columns, row
count, byte count — and structurally cannot record the values themselves.

Wiring this package into the server is composition, and lives above both: see
:mod:`kedge.serve`. To run the server against the real loop rather than the scripted stand-in::

    uv run python -c "from kedge.serve import serve; serve('book.xlsx', port=8731)"
"""

from __future__ import annotations

from kedge.agent.audit import OutboundLog, OutboundRecord, outbound_log_for
from kedge.agent.context import (
    CellFacts,
    ConversationWindow,
    NameRegistry,
    NotebookState,
    TokenCounter,
    build_analysis_block,
    build_plan_block,
)
from kedge.agent.fill import (
    FillAttempt,
    FilledCell,
    FillOutcome,
    FillReport,
    convert_notebook,
    fill_holes,
)
from kedge.agent.fillprompt import PromptAssemblyError
from kedge.agent.loop import (
    AgentError,
    ChatDelta,
    KedgeAgent,
    ModelClient,
    OpenAIClient,
)
from kedge.agent.prompts import build_system_prompt
from kedge.agent.tools import (
    MAX_PAYLOAD_BYTES,
    MAX_ROWS,
    OMISSION_TEMPLATE,
    TOOL_SPECS,
    Caps,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    tool_names,
    tool_schemas,
)
from kedge.agent.validate import (
    MAX_VALIDATION_ATTEMPTS,
    MISSING_NAME_STAGE,
    Policy,
    RoundingContext,
    ValidationReport,
    ValidationStage,
    Violation,
    undefined_name,
    validate_cell,
)

__all__ = [
    "MAX_PAYLOAD_BYTES",
    "MAX_ROWS",
    "MAX_VALIDATION_ATTEMPTS",
    "MISSING_NAME_STAGE",
    "OMISSION_TEMPLATE",
    "TOOL_SPECS",
    "AgentError",
    "Caps",
    "CellFacts",
    "ChatDelta",
    "ConversationWindow",
    "FillAttempt",
    "FillOutcome",
    "FillReport",
    "FilledCell",
    "KedgeAgent",
    "ModelClient",
    "NameRegistry",
    "NotebookState",
    "OpenAIClient",
    "OutboundLog",
    "OutboundRecord",
    "Policy",
    "PromptAssemblyError",
    "RoundingContext",
    "TokenCounter",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "ValidationReport",
    "ValidationStage",
    "Violation",
    "build_analysis_block",
    "build_plan_block",
    "build_system_prompt",
    "convert_notebook",
    "fill_holes",
    "outbound_log_for",
    "tool_names",
    "tool_schemas",
    "undefined_name",
    "validate_cell",
]
