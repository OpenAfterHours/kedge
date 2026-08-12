"""The event vocabulary, its wire format, and the bus that fans it out."""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from kedge.server.events import (
    MAX_ARGS_SUMMARY_CHARS,
    CellCreatedEvent,
    CellResultEvent,
    CellRunningEvent,
    DoneEvent,
    ErrorEvent,
    EventBus,
    NotebookNotification,
    PausedEvent,
    StatusEvent,
    TokenEvent,
    ToolCallEvent,
    ToolResultEvent,
    ValidationEvent,
    encode_sse,
    notebook_mirror,
    notification_for,
    parse_event,
    sse_comment,
    summarise_args,
)

# ── the M3 event list ────────────────────────────────────────────────────────────────────────


def test_every_event_in_the_plan_has_a_model_with_the_named_fields() -> None:
    # PLAN M3 lists these ten with these fields; the list is a specification, not a suggestion.
    expected = {
        "status": (StatusEvent, {"phase"}),
        "token": (TokenEvent, {"text"}),
        "tool_call": (ToolCallEvent, {"name", "args_summary"}),
        "tool_result": (ToolResultEvent, {"name", "ok", "summary"}),
        "cell_created": (CellCreatedEvent, {"cell_id", "name", "preview"}),
        "cell_running": (CellRunningEvent, {"cell_id"}),
        "cell_result": (CellResultEvent, {"cell_id", "ok", "error"}),
        "validation": (ValidationEvent, {"ok", "violations"}),
        "done": (DoneEvent, {"turn_id", "tokens_used"}),
        "error": (ErrorEvent, {"message", "recoverable"}),
    }
    for name, (model, fields) in expected.items():
        assert set(model.model_fields) == fields | {"type"}, name
        assert model.model_fields["type"].default == name


def test_events_are_frozen_so_a_published_event_cannot_be_edited_downstream() -> None:
    event = StatusEvent(phase="analysing")
    with pytest.raises(ValidationError):
        event.phase = "running"  # type: ignore[misc]


def test_status_phase_is_restricted_to_the_four_named_phases() -> None:
    with pytest.raises(ValidationError):
        StatusEvent(phase="pondering")  # type: ignore[arg-type]


# ── tool_call carries a summary, never raw args ──────────────────────────────────────────────


def test_tool_call_rejects_a_raw_arguments_field_outright() -> None:
    # The structural half: there is no field for raw arguments, and extra="forbid" means passing
    # one is an error rather than something quietly ignored.
    with pytest.raises(ValidationError):
        ToolCallEvent(name="propose_cell", args={"code": "x = 1"})  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        ToolCallEvent(name="propose_cell", arguments={"code": "x = 1"})  # type: ignore[call-arg]


def test_tool_call_rejects_a_summary_that_is_a_serialised_argument_dump() -> None:
    dump = json.dumps({"name": "load_handin", "code": "x = 1"})
    with pytest.raises(ValidationError, match="serialised arguments"):
        ToolCallEvent(name="propose_cell", args_summary=dump)


def test_tool_call_rejects_a_multi_line_summary() -> None:
    with pytest.raises(ValidationError, match="single line"):
        ToolCallEvent(name="propose_cell", args_summary="code=\nimport polars as pl\n")


def test_tool_call_rejects_an_over_long_summary() -> None:
    with pytest.raises(ValidationError, match="at most"):
        ToolCallEvent(name="probe", args_summary="x" * (MAX_ARGS_SUMMARY_CHARS + 1))


def test_summarising_keeps_a_cell_body_out_of_the_event() -> None:
    body = "import polars as pl\nload_handin = pl.scan_parquet('secret.parquet')\n" * 6
    event = ToolCallEvent.summarising(
        "propose_cell", {"name": "load_handin", "after": "handin", "code": body}
    )
    assert "polars" not in event.args_summary
    assert "secret" not in event.args_summary
    assert f"<{len(body)} chars>" in event.args_summary
    # The short, useful arguments do survive: the trail is meant to be specific.
    assert "name=load_handin" in event.args_summary
    assert len(event.args_summary) <= MAX_ARGS_SUMMARY_CHARS


def test_summarising_describes_structures_by_shape_not_by_content() -> None:
    summary = summarise_args(
        {
            "rows": [{"counterparty": "ACME", "ead": 12.5}] * 40,
            "filters": {"sheet": "Calc", "column": "H"},
            "limit": 100,
            "verbose": True,
            "where": None,
        }
    )
    assert "ACME" not in summary
    assert "rows=<40 items>" in summary
    assert "filters=<2 keys>" in summary
    assert "limit=100" in summary
    assert "verbose=true" in summary
    assert "where=none" in summary


def test_summarising_truncates_rather_than_producing_an_invalid_event() -> None:
    event = ToolCallEvent.summarising("probe", {f"key_{i}": f"value_{i}" for i in range(60)})
    assert len(event.args_summary) <= MAX_ARGS_SUMMARY_CHARS


# ── the wire format ──────────────────────────────────────────────────────────────────────────


def test_encode_sse_names_the_event_and_frames_the_payload() -> None:
    frame = encode_sse(ToolResultEvent(name="reconcile", ok=True, summary="0 differing"))
    assert frame.startswith("event: tool_result\ndata: {")
    assert frame.endswith("\n\n")
    payload = json.loads(frame.split("data: ", 1)[1])
    assert payload == {
        "type": "tool_result",
        "name": "reconcile",
        "ok": True,
        "summary": "0 differing",
    }


def test_encode_sse_keeps_multi_line_text_on_one_data_line() -> None:
    # A raw newline in the payload would split the frame and corrupt every following event.
    frame = encode_sse(TokenEvent(text="line one\nline two\n"))
    assert len(frame.rstrip("\n").split("\n")) == 2


def test_sse_comment_is_a_valid_ignorable_frame() -> None:
    assert sse_comment("keep-alive") == ": keep-alive\n\n"


def test_parse_event_round_trips_every_variant() -> None:
    events = [
        StatusEvent(phase="editing"),
        TokenEvent(text="hello"),
        ToolCallEvent.summarising("list_cells", {}),
        ToolResultEvent(name="list_cells", ok=True, summary="6 cells"),
        CellCreatedEvent(cell_id="UMIT", name="apply_haircuts", preview="x = 1"),
        CellResultEvent(cell_id="UMIT", ok=False, error="NameError"),
        ValidationEvent(ok=False, violations=("multiply-defined name",)),
        DoneEvent(turn_id="t1", tokens_used=42),
        ErrorEvent(message="nope", recoverable=True),
    ]
    for event in events:
        assert parse_event(event.model_dump_json()) == event
        assert parse_event(event.model_dump()) == event


def test_parse_event_dispatches_on_the_discriminator() -> None:
    parsed = parse_event({"type": "cell_running", "cell_id": "Hbol"})
    assert parsed.type == "cell_running"
    assert parsed.cell_id == "Hbol"


# ── the bus ──────────────────────────────────────────────────────────────────────────────────


async def test_bus_delivers_to_every_subscriber() -> None:
    bus = EventBus()
    async with bus.subscribe() as first, bus.subscribe() as second:
        assert bus.subscriber_count == 2
        await bus.publish(StatusEvent(phase="running"))
        assert await anext(first) == StatusEvent(phase="running")
        assert await anext(second) == StatusEvent(phase="running")
    assert bus.subscriber_count == 0


async def test_bus_drops_events_for_a_subscriber_that_is_not_draining() -> None:
    # A suspended browser tab must not be able to stall the turn that is producing events.
    bus = EventBus(queue_size=2)
    async with bus.subscribe():
        for _ in range(5):
            await bus.publish(TokenEvent(text="x"))
    assert bus.dropped == 3


async def test_bus_observer_that_raises_does_not_break_the_turn() -> None:
    bus = EventBus()
    seen: list[str] = []

    def explode(_event: object) -> None:
        msg = "the notebook has gone away"
        raise RuntimeError(msg)

    bus.add_observer(explode)
    bus.add_observer(lambda event: seen.append(event.type))
    await bus.publish(DoneEvent(turn_id="t1"))
    assert seen == ["done"]


async def test_bus_observer_may_be_asynchronous_and_can_be_removed() -> None:
    bus = EventBus()
    seen: list[str] = []

    async def observe(event: object) -> None:
        await asyncio.sleep(0)
        seen.append(event.type)

    remove = bus.add_observer(observe)
    await bus.publish(TokenEvent(text="a"))
    remove()
    await bus.publish(TokenEvent(text="b"))
    assert seen == ["token"]


# ── the notebook mirror ──────────────────────────────────────────────────────────────────────


def test_only_notebook_relevant_events_are_mirrored() -> None:
    mirrored = {
        CellCreatedEvent(cell_id="a", name="load_handin"),
        CellResultEvent(cell_id="a", ok=False, error="boom"),
        ValidationEvent(ok=False, violations=("multiply-defined name 'ead'",)),
        ErrorEvent(message="the kernel went away", recoverable=False),
        PausedEvent(message="Say continue and I will pick it up.", steps=50),
        DoneEvent(turn_id="t1"),
    }
    ignored = {
        StatusEvent(phase="thinking"),
        TokenEvent(text="prose"),
        ToolCallEvent.summarising("probe", {"code": "df.height"}),
        ToolResultEvent(name="probe", ok=True, summary="42"),
        CellResultEvent(cell_id="a", ok=True),
    }
    for event in mirrored:
        assert notification_for(event) is not None, event.type
    for event in ignored:
        assert notification_for(event) is None, event.type


def test_a_pause_is_mirrored_as_a_question_rather_than_a_failure() -> None:
    """A turn waiting for a word has not gone wrong, and must not be dressed up as if it had."""
    notification = notification_for(PausedEvent(message="Say continue.", steps=50))
    assert notification is not None
    assert notification.variant is None
    assert "Say continue." in notification.description


def test_a_paused_event_survives_the_wire() -> None:
    event = PausedEvent(message="I have taken 50 steps.", steps=50)
    restored = parse_event(json.loads(encode_sse(event).split("data: ", 1)[1]))
    assert restored == event


def test_failures_are_mirrored_as_danger() -> None:
    notification = notification_for(ValidationEvent(ok=False, violations=("no pandas",)))
    assert notification is not None
    assert notification.variant == "danger"
    assert "no pandas" in notification.description


async def test_notebook_mirror_forwards_only_the_important_events() -> None:
    received: list[NotebookNotification] = []

    class Driver:
        def notify(self, notification: NotebookNotification) -> None:
            received.append(notification)

    bus = EventBus()
    bus.add_observer(notebook_mirror(Driver()))
    await bus.publish(TokenEvent(text="ignored"))
    await bus.publish(CellCreatedEvent(cell_id="UMIT", name="apply_haircuts"))
    assert len(received) == 1
    assert "apply_haircuts" in received[0].description
