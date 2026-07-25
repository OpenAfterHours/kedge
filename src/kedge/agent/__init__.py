"""The agent: the tool surface, the validation gate, the context strategy, and the loop.

This package is PLAN M4. It is the only part of kedge that talks to a model endpoint, and the only
part that decides what a model is allowed to do to a notebook.

Four modules, four jobs:

* :mod:`kedge.agent.tools` — the fifteen tools, and the row and payload caps every value-returning
  one of them passes through. The caps are structural: a tool cannot return a payload except via
  :meth:`~kedge.agent.tools.ToolRegistry.dispatch`, which caps it and audits it.
* :mod:`kedge.agent.validate` — the five-stage gate that runs before any generated code reaches the
  kernel: syntax, the marimo single-definition contract checked against the live graph, policy,
  output style, then hand the violations back and let the model retry, three times.
* :mod:`kedge.agent.context` — the name registry, the live notebook state, the token budget and the
  eviction order. Notebook state is rebuilt from the kernel every turn and never taken from history.
* :mod:`kedge.agent.loop` — :class:`~kedge.agent.loop.KedgeAgent`, which satisfies
  :class:`kedge.server.agent_seam.AgentLoop` and is a drop-in replacement for the server's
  ``ScriptedAgent``.

Plus :mod:`kedge.agent.audit`, which records what left the machine — tool, sheet, columns, row
count, byte count — and structurally cannot record the values themselves.

To run the server against the real loop rather than the scripted stand-in::

    uv run python -c "from kedge.agent import serve; serve('book.xlsx', port=8731)"
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
from kedge.agent.loop import (
    AgentError,
    ChatDelta,
    KedgeAgent,
    ModelClient,
    OpenAIClient,
    build_agent_app,
    serve,
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
    Policy,
    RoundingContext,
    ValidationReport,
    ValidationStage,
    Violation,
    validate_cell,
)

__all__ = [
    "MAX_PAYLOAD_BYTES",
    "MAX_ROWS",
    "MAX_VALIDATION_ATTEMPTS",
    "OMISSION_TEMPLATE",
    "TOOL_SPECS",
    "AgentError",
    "Caps",
    "CellFacts",
    "ChatDelta",
    "ConversationWindow",
    "KedgeAgent",
    "ModelClient",
    "NameRegistry",
    "NotebookState",
    "OpenAIClient",
    "OutboundLog",
    "OutboundRecord",
    "Policy",
    "RoundingContext",
    "TokenCounter",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "ValidationReport",
    "ValidationStage",
    "Violation",
    "build_agent_app",
    "build_analysis_block",
    "build_plan_block",
    "build_system_prompt",
    "outbound_log_for",
    "serve",
    "tool_names",
    "tool_schemas",
    "validate_cell",
]
