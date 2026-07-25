"""The outbound log records the shape of what left the machine, and never its contents.

The load-bearing test here is the sentinel one. A distinctive value is put into a workbook, pulled
back out through a value-returning tool, and then the whole log file is searched for it — not the
fields we expect it in, the whole file. That is the only form of this test that would still fail if
somebody added a well-meaning ``detail`` or ``preview`` field to :class:`OutboundRecord` later, and
the failure mode it guards against is one nobody notices until the tool is pointed at real customer
data.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from openpyxl import Workbook as OpenpyxlWorkbook

from kedge.agent.audit import MAX_LOGGED_COLUMNS, OutboundLog, OutboundRecord, outbound_log_for
from kedge.agent.tools import Caps, ToolContext, ToolRegistry
from kedge.notebook.model import ProbeResult
from kedge.workspace import Workspace

SENTINEL = "MERIDIAN-88213-ACCT-KESTREL"


class SentinelDriver:
    """A kernel whose every answer contains the sentinel."""

    async def probe(self, code: str) -> ProbeResult:
        return ProbeResult(ok=True, value_repr=f"['{SENTINEL}', '{SENTINEL}']", value_type="list")


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    monkeypatch.setenv("KEDGE_HOME", str(tmp_path / "home"))
    workbook = tmp_path / "customers.xlsx"
    book = OpenpyxlWorkbook()
    sheet = book.active
    sheet.title = "Ref"
    sheet.append(["account_ref", "exposure"])
    sheet.append([SENTINEL, 1234.56])
    sheet.append([f"{SENTINEL}-2", 99.0])
    book.save(workbook)
    built = Workspace.for_workbook(workbook)
    built.ensure_dirs()
    return built


# ── the sentinel ─────────────────────────────────────────────────────────────────────────────


async def test_a_value_returned_by_a_tool_never_reaches_the_log(workspace: Workspace) -> None:
    log = outbound_log_for(workspace, "session-a")
    tools = ToolRegistry(ToolContext(workspace=workspace), log=log)
    try:
        result = await tools.dispatch(
            "read_range", {"sheet": "Ref", "range": "A1:B3"}, turn_id="t1"
        )
    finally:
        await tools.aclose()

    assert SENTINEL in result.text, "the tool must genuinely have returned the value"

    raw = log.path.read_text(encoding="utf-8")
    assert SENTINEL not in raw
    assert "1234.56" not in raw

    entry = json.loads(raw.strip())
    assert entry["tool"] == "read_range"
    assert entry["sheet"] == "Ref"
    assert entry["row_count"] == 3
    assert entry["byte_count"] == result.byte_count
    assert entry["turn_id"] == "t1"


async def test_a_probe_value_never_reaches_the_log(workspace: Workspace) -> None:
    log = outbound_log_for(workspace, "session-b")
    tools = ToolRegistry(ToolContext(workspace=workspace, driver=SentinelDriver()), log=log)
    result = await tools.dispatch("probe", {"code": "handin.head()"})
    assert SENTINEL in result.text
    assert SENTINEL not in log.path.read_text(encoding="utf-8")


async def test_a_sample_carries_column_names_but_no_values(workspace: Workspace) -> None:
    log = outbound_log_for(workspace, "session-c")
    tools = ToolRegistry(ToolContext(workspace=workspace), log=log)
    try:
        await tools.dispatch("sample_data", {"sheet": "Ref", "rows": 5})
    finally:
        await tools.aclose()
    entry = next(iter(log.entries()))
    # Column names are metadata and travel deliberately; a log that cannot say which columns went
    # out answers none of the questions it exists to answer.
    assert "account_ref" in entry["columns"]
    assert SENTINEL not in json.dumps(entry)


async def test_every_dispatch_writes_exactly_one_line(workspace: Workspace) -> None:
    log = outbound_log_for(workspace, "session-d")
    tools = ToolRegistry(ToolContext(workspace=workspace), log=log)
    try:
        await tools.dispatch("read_range", {"sheet": "Ref", "range": "A1:B2"})
        await tools.dispatch("read_range", {"sheet": "Ref", "range": "A2:B3"})
        await tools.dispatch("get_knowledge", {})
        await tools.dispatch("no_such_tool", {})
    finally:
        await tools.aclose()
    assert len(list(log.entries())) == 4


# ── the record itself ────────────────────────────────────────────────────────────────────────


def test_the_record_has_no_field_a_value_could_travel_in() -> None:
    fields = set(
        OutboundRecord(
            timestamp="2026-07-25T09:00:00+00:00",
            session="s",
            turn_id=None,
            tool="read_range",
            ok=True,
            sheet="Ref",
            columns=("a",),
            row_count=1,
            byte_count=1,
        )
        .to_dict()
        .keys()
    )
    assert fields == {
        "timestamp",
        "session",
        "turn_id",
        "tool",
        "ok",
        "sheet",
        "columns",
        "column_count",
        "row_count",
        "byte_count",
        "truncated",
        "omitted_rows",
        "redacted_columns",
    }


def test_a_very_wide_result_logs_a_count_rather_than_every_column(tmp_path: Path) -> None:
    log = OutboundLog(tmp_path / "outbound-wide.jsonl", session="s")
    log.record(
        tool="sample_data",
        columns=tuple(f"column_{index}" for index in range(500)),
        row_count=1,
        byte_count=10,
    )
    entry = next(iter(log.entries()))
    assert len(entry["columns"]) == MAX_LOGGED_COLUMNS
    assert entry["column_count"] == 500


def test_a_log_that_cannot_be_written_degrades_rather_than_failing_the_turn(
    tmp_path: Path,
) -> None:
    # A file where the directory should be: the append must be swallowed, not raised.
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    log = OutboundLog(blocker / "outbound-s.jsonl", session="s")
    record = log.record(tool="read_range", row_count=1, byte_count=1)
    assert record.tool == "read_range"
    assert list(log.entries()) == []


def test_records_accumulate_in_order(tmp_path: Path) -> None:
    log = OutboundLog(tmp_path / "outbound-s.jsonl", session="s")
    for index in range(3):
        log.record(tool=f"tool_{index}", row_count=index, byte_count=index)
    assert [entry["tool"] for entry in log.entries()] == ["tool_0", "tool_1", "tool_2"]


def test_the_log_path_is_per_session(workspace: Workspace) -> None:
    first = outbound_log_for(workspace, "alpha")
    second = outbound_log_for(workspace, "beta")
    assert first.path != second.path
    assert first.path.name == "outbound-alpha.jsonl"


async def test_capped_results_record_what_they_dropped(workspace: Workspace) -> None:
    log = outbound_log_for(workspace, "session-e")
    tools = ToolRegistry(ToolContext(workspace=workspace, caps=Caps(max_rows=1)), log=log)
    try:
        await tools.dispatch("read_range", {"sheet": "Ref", "range": "A1:B3"})
    finally:
        await tools.aclose()
    entry = next(iter(log.entries()))
    assert entry["truncated"] is True
    assert entry["omitted_rows"] == 2
