"""The kedge server: the chat pane, the notebook iframe, and the event stream between them.

This is Milestone 3. The author's decision is that the kedge chat window — not marimo's built-in
assistant — is how the user interacts with the AI, so the chat pane is the primary surface rather
than a thin wrapper around something else.

The pieces:

* :mod:`kedge.server.events` — the bus that fans a turn's events out to the SSE response, the
  notebook mirror and any monitor, the SSE framing, and the hub's own open-progress vocabulary.
  The *turn* vocabulary it re-exports is :mod:`kedge.turn`, which sits below both this package and
  the agent: the agent emits those events and the server serialises them, so neither owns them and
  a module above either would put one layer above the other.
* :mod:`kedge.server.sessions` — chat sessions and messages in SQLite, each session tied to a
  notebook path and a snapshot marker so it can be resumed honestly.
* :mod:`kedge.server.agent_seam` — the :class:`~kedge.server.agent_seam.AgentLoop` protocol the
  agent loop builds to, and a scripted stand-in that satisfies it.
* :mod:`kedge.server.routes` and :mod:`kedge.server.app` — the HTTP surface and the application.
* :mod:`kedge.server.hub` — the landing page: every workbook kedge has seen, a file browser for
  adding another, and the open sequence with its progress streamed. ``create_hub_app`` builds the
  same application with no workbook attached, which is what makes the browser a way *in* rather
  than something the CLI has to open on your behalf.

The static UI in ``static/`` is plain HTML, CSS and JavaScript with no build step, no framework
and no CDN, in keeping with the rest of the OpenAfterHours tooling and with local-first: nothing
on the page reaches off the machine.
"""

from __future__ import annotations

from kedge.server.agent_seam import (
    AgentLoop,
    AgentUnavailableError,
    CancelToken,
    ScriptedAgent,
    TurnMessage,
    TurnRegistry,
    TurnRequest,
)
from kedge.server.app import (
    ServerError,
    ServerState,
    WorkspaceNotAttachedError,
    create_app,
    create_demo_app,
    create_hub_app,
    run_server,
)
from kedge.server.events import (
    AnyEvent,
    CellCreatedEvent,
    CellResultEvent,
    CellRunningEvent,
    DoneEvent,
    ErrorEvent,
    Event,
    EventBus,
    HubEvent,
    NotebookNotification,
    NotebookNotifier,
    OpenProgressEvent,
    OpenReadyEvent,
    PausedEvent,
    StatusEvent,
    TokenEvent,
    ToolCallEvent,
    ToolResultEvent,
    ValidationEvent,
    encode_sse,
    parse_event,
)
from kedge.server.sessions import ChatMessage, ChatSession, SessionStore, notebook_snapshot

__all__ = [
    "AgentLoop",
    "AgentUnavailableError",
    "AnyEvent",
    "CancelToken",
    "CellCreatedEvent",
    "CellResultEvent",
    "CellRunningEvent",
    "ChatMessage",
    "ChatSession",
    "DoneEvent",
    "ErrorEvent",
    "Event",
    "EventBus",
    "HubEvent",
    "NotebookNotification",
    "NotebookNotifier",
    "OpenProgressEvent",
    "OpenReadyEvent",
    "PausedEvent",
    "ScriptedAgent",
    "ServerError",
    "ServerState",
    "SessionStore",
    "StatusEvent",
    "TokenEvent",
    "ToolCallEvent",
    "ToolResultEvent",
    "TurnMessage",
    "TurnRegistry",
    "TurnRequest",
    "ValidationEvent",
    "WorkspaceNotAttachedError",
    "create_app",
    "create_demo_app",
    "create_hub_app",
    "encode_sse",
    "notebook_snapshot",
    "parse_event",
    "run_server",
]
