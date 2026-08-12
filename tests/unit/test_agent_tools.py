"""The tool surface: the caps, the choke point, and what every tool does when its collaborator
is missing.

The cap tests are the important ones. PLAN 2.3's rule is not "each tool should remember to
truncate" — it is that a payload cannot reach the model uncapped — so the tests here go at the
choke point rather than at the individual tools: a deliberately leaky handler is registered and
:meth:`ToolRegistry.dispatch` is asserted to cap it anyway. That is the property that survives
somebody adding a sixteenth tool.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook as OpenpyxlWorkbook

from kedge.agent.audit import OutboundLog
from kedge.agent.context import CellFacts, NotebookState
from kedge.agent.tools import (
    MAX_PAYLOAD_BYTES,
    MAX_ROWS,
    Caps,
    ToolContext,
    ToolRegistry,
    ToolResult,
    tool_names,
    tool_schemas,
)
from kedge.notebook.driver import MultiplyDefinedError, StaleCellError
from kedge.notebook.model import (
    CellInfo,
    CellRef,
    GraphNode,
    GraphView,
    MutationResult,
    ProbeResult,
)
from kedge.workspace import Workspace

SENTINEL = "ZEPHYR-QX-9931-COUNTERPARTY"


# ── fakes ────────────────────────────────────────────────────────────────────────────────────


class FakeDriver:
    """Enough of :class:`~kedge.notebook.model.NotebookBridge` to exercise the tools."""

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.created: list[tuple[str, str]] = []
        self.edited: list[tuple[str, str]] = []
        self.probes: list[str] = []
        self.deleted: list[str] = []
        self.probe_result: ProbeResult | None = None
        self._fail = fail

    async def read_graph(self) -> GraphView:
        return GraphView(
            nodes=(
                GraphNode(id="AAaa", name="imports", defs=("pl",)),
                GraphNode(id="MJUe", name="load_handin", defs=("load_handin",), refs=("pl",)),
            )
        )

    async def list_cells(self, *, with_code: bool = True) -> tuple[CellInfo, ...]:
        return (
            CellInfo(id="AAaa", name="imports", code="import polars as pl" if with_code else None),
            CellInfo(
                id="MJUe",
                name="load_handin",
                code='load_handin = pl.scan_parquet("h.parquet")' if with_code else None,
            ),
        )

    async def get_cell(self, target: str | int) -> CellInfo:
        return CellInfo(id="MJUe", name="load_handin", code="load_handin = 1", status="idle")

    async def create_cell(self, code: str, *, name: str, **_kw: Any) -> MutationResult:
        if self._fail is not None:
            raise self._fail
        self.created.append((name, code))
        return MutationResult(
            operation="create_cell", cell=CellRef(id="U1", name=name), ran=True, status="idle"
        )

    async def edit_cell(
        self, target: str | int, code: str | None = None, **_kw: Any
    ) -> MutationResult:
        if self._fail is not None:
            raise self._fail
        self.edited.append((str(target), code or ""))
        return MutationResult(
            operation="edit_cell",
            cell=CellRef(id="MJUe", name=str(target)),
            ran=True,
            status="idle",
        )

    async def run_cell(self, target: str | int) -> MutationResult:
        return MutationResult(
            operation="run_cell", cell=CellRef(id="MJUe", name=str(target)), ran=True, status="idle"
        )

    async def delete_cell(self, target: str | int) -> MutationResult:
        self.deleted.append(str(target))
        return MutationResult(operation="delete_cell", cell=CellRef(id="MJUe", name=str(target)))

    async def probe(self, code: str) -> ProbeResult:
        self.probes.append(code)
        if self.probe_result is not None:
            return self.probe_result
        return ProbeResult(ok=True, value_repr=f"['{SENTINEL}']", value_type="list")


@pytest.fixture
def state() -> NotebookState:
    return NotebookState(
        cells=(
            CellFacts(id="AAaa", name="imports", defs=("pl",)),
            CellFacts(id="MJUe", name="load_handin", defs=("load_handin",), refs=("pl",)),
        )
    )


@pytest.fixture
def registry(state: NotebookState) -> ToolRegistry:
    tools = ToolRegistry(ToolContext(driver=FakeDriver()))
    tools.refresh(state)
    return tools


# ── schemas ──────────────────────────────────────────────────────────────────────────────────


def test_every_tool_plan_m4_lists_is_offered() -> None:
    assert set(tool_names()) == {
        "list_cells",
        "propose_cell",
        "edit_cell",
        "run_cell",
        "delete_cell",
        "inspect_workbook",
        "sample_data",
        "profile_column",
        "read_range",
        "probe",
        "get_plan",
        "amend_plan",
        "reconcile",
        "list_utils",
        "get_knowledge",
    }


def test_schemas_are_valid_openai_function_definitions() -> None:
    for schema in tool_schemas():
        assert schema["type"] == "function"
        function = schema["function"]
        assert function["name"] and function["description"]
        parameters = function["parameters"]
        assert parameters["type"] == "object"
        assert parameters["additionalProperties"] is False
        assert set(parameters["required"]) <= set(parameters["properties"])


def test_the_defaults_are_the_ones_plan_2_3_states() -> None:
    assert MAX_ROWS == 100
    assert MAX_PAYLOAD_BYTES == 32_768


def test_caps_come_from_config() -> None:
    workspace = Workspace.for_workbook("book.xlsx")
    caps = Caps.from_config(workspace.config)
    assert caps.max_rows == workspace.config.sampling.max_rows
    assert caps.max_payload_bytes == workspace.config.sampling.max_payload_bytes


# ── the caps ─────────────────────────────────────────────────────────────────────────────────


def test_the_row_cap_truncates_with_the_marker_plan_m4_requires() -> None:
    rows = [[index, index * 2] for index in range(500)]
    result = ToolResult.from_rows(rows, columns=["a", "b"], caps=Caps(max_rows=100))
    assert result.row_count == 100
    assert result.omitted_rows == 400
    assert result.truncated
    assert result.text.endswith("[… 400 more rows omitted]")


def test_the_marker_counts_rows_that_were_never_read_at_all() -> None:
    rows = [[index] for index in range(10)]
    result = ToolResult.from_rows(rows, columns=["a"], caps=Caps(), total=49_999)
    assert result.omitted_rows == 49_989
    assert "[… 49989 more rows omitted]" in result.text


def test_the_payload_cap_sheds_rows_rather_than_cutting_mid_value() -> None:
    rows = [[f"counterparty-{index:05d}", index * 1.5] for index in range(400)]
    result = ToolResult.from_rows(
        rows, columns=["name", "exposure"], caps=Caps(max_rows=400, max_payload_bytes=2_000)
    )
    assert result.byte_count <= 2_000
    assert result.row_count < 400
    assert result.truncated
    assert f"[… {result.omitted_rows} more rows omitted]" in result.text
    # Every rendered row is still complete JSON, which is the point of shedding rather than cutting.
    for line in result.text.splitlines():
        if line.startswith("[") and "omitted" not in line:
            json.loads(line)


def test_an_untruncated_result_carries_no_marker() -> None:
    result = ToolResult.from_rows([[1], [2]], columns=["a"], caps=Caps())
    assert not result.truncated
    assert "omitted" not in result.text


class _LeakyRegistry(ToolRegistry):
    """A handler that ignores the caps entirely, to prove the choke point does not."""

    async def _tool_get_knowledge(self, args: Any) -> ToolResult:
        return ToolResult(text="x" * 200_000, row_count=200_000)


async def test_dispatch_caps_a_handler_that_did_not_cap_itself() -> None:
    tools = _LeakyRegistry(ToolContext(caps=Caps(max_payload_bytes=4_096)))
    result = await tools.dispatch("get_knowledge", {})
    assert result.byte_count <= 4_096
    assert result.truncated
    assert "truncated at the payload cap" in result.text


async def test_dispatch_audits_the_capped_payload_not_the_original(tmp_path: Path) -> None:
    log = OutboundLog(tmp_path / "outbound-s1.jsonl", session="s1")
    tools = _LeakyRegistry(ToolContext(caps=Caps(max_payload_bytes=4_096)), log=log)
    result = await tools.dispatch("get_knowledge", {}, turn_id="t1")
    entries = list(log.entries())
    assert len(entries) == 1
    assert entries[0]["byte_count"] == result.byte_count <= 4_096
    assert entries[0]["truncated"] is True
    assert entries[0]["turn_id"] == "t1"


# ── dispatch ─────────────────────────────────────────────────────────────────────────────────


async def test_an_unknown_tool_is_a_result_rather_than_an_exception(registry: ToolRegistry) -> None:
    result = await registry.dispatch("delete_everything", {})
    assert not result.ok
    assert "no tool called" in result.text
    assert "propose_cell" in result.text


async def test_malformed_arguments_are_a_result_rather_than_an_exception(
    registry: ToolRegistry,
) -> None:
    result = await registry.dispatch("read_range", "{not json")
    assert not result.ok
    assert "not valid JSON" in result.text


async def test_missing_required_arguments_are_reported(registry: ToolRegistry) -> None:
    result = await registry.dispatch("read_range", {"sheet": "Calc"})
    assert not result.ok
    assert "range" in result.text


async def test_a_cell_body_with_raw_newlines_is_still_accepted(registry: ToolRegistry) -> None:
    raw = '{"name": "apply_haircuts", "code": "apply_haircuts = 1\n"}'
    result = await registry.dispatch("propose_cell", raw)
    assert result.ok, result.text


# ── notebook tools ───────────────────────────────────────────────────────────────────────────


async def test_propose_cell_goes_through_the_validation_gate(state: NotebookState) -> None:
    driver = FakeDriver()
    tools = ToolRegistry(ToolContext(driver=driver))
    tools.refresh(state)
    result = await tools.dispatch(
        "propose_cell",
        {"name": "loader", "code": "import pandas as pd\nframe = pd.DataFrame()\n"},
    )
    assert not result.ok
    assert result.validated is False
    assert any("polars, never pandas" in violation for violation in result.violations)
    assert driver.created == []  # nothing reached the kernel


async def test_propose_cell_reports_a_kernel_side_rejection_as_violations(
    state: NotebookState,
) -> None:
    message = "Multiply-defined names:\n  - 'pl' is already defined in cell 'AAaa' (imports)\n"
    driver = FakeDriver(fail=MultiplyDefinedError(message))
    tools = ToolRegistry(ToolContext(driver=driver))
    tools.refresh(state)
    result = await tools.dispatch("propose_cell", {"name": "second", "code": "value = 1\n"})
    assert not result.ok
    assert result.validated is False
    assert "'pl'" in result.violations[0]


async def test_edit_cell_surfaces_staleness_as_a_retryable_result(state: NotebookState) -> None:
    driver = FakeDriver(fail=StaleCellError("cell 'MJUe' changed since it was read"))
    tools = ToolRegistry(ToolContext(driver=driver))
    tools.refresh(state)
    result = await tools.dispatch("edit_cell", {"cell": "load_handin", "code": "value = 1\n"})
    assert not result.ok
    assert "list_cells" in result.text
    assert "resubmitting the same body" in result.text


async def test_delete_cell_never_deletes(registry: ToolRegistry) -> None:
    driver = registry.context.driver
    result = await registry.dispatch(
        "delete_cell", {"cell": "load_handin", "reason": "superseded by apply_haircuts"}
    )
    assert not result.ok
    assert "NOT happened" in result.text
    assert driver.deleted == []
    assert registry.pending_deletions[0].cell == "load_handin"
    assert registry.pending_deletions[0].reason == "superseded by apply_haircuts"


async def test_delete_cell_names_the_cells_that_would_break(registry: ToolRegistry) -> None:
    result = await registry.dispatch("delete_cell", {"cell": "imports", "reason": "tidy up"})
    assert "load_handin" in result.text


async def test_list_cells_returns_the_bodies_and_the_graph(registry: ToolRegistry) -> None:
    result = await registry.dispatch("list_cells", {})
    assert result.ok
    assert "import polars as pl" in result.text
    assert "defines: pl" in result.text


async def test_probe_returns_the_kernel_value(registry: ToolRegistry) -> None:
    result = await registry.dispatch("probe", {"code": "load_handin.height"})
    assert result.ok
    assert SENTINEL in result.text
    assert registry.context.driver.probes == ["load_handin.height"]


async def test_an_over_long_probe_is_refused(registry: ToolRegistry) -> None:
    result = await registry.dispatch("probe", {"code": "x" * 5_000})
    assert not result.ok
    assert "wants to be a cell" in result.text


async def test_a_probe_with_no_value_says_how_to_ask_again(registry: ToolRegistry) -> None:
    # "nothing" is not an answer the model can act on; it has to be told what to change.
    registry.context.driver.probe_result = ProbeResult(ok=True)
    result = await registry.dispatch("probe", {"code": "import polars as pl"})
    assert not result.ok
    assert "bare expression" in result.text
    assert result.summary == "probe returned nothing"


async def test_a_printing_probe_leads_with_what_it_printed(registry: ToolRegistry) -> None:
    # print(...) binds None. The printed text is the answer, so it must not sit under a header
    # that reads as an empty result.
    registry.context.driver.probe_result = ProbeResult(
        ok=True, value_repr="None", value_type="NoneType", stdout=f"shape: (1, 1)\n{SENTINEL}\n"
    )
    result = await registry.dispatch("probe", {"code": "print(load_handin)"})
    assert result.ok
    assert result.text.startswith("stdout:")
    assert SENTINEL in result.text
    assert result.summary == "probe printed output"


# ── degradation ──────────────────────────────────────────────────────────────────────────────


async def test_notebook_tools_say_so_when_no_kernel_is_attached() -> None:
    tools = ToolRegistry(ToolContext())
    result = await tools.dispatch("list_cells", {})
    assert not result.ok
    assert "no live marimo notebook" in result.text


async def test_inspect_workbook_says_so_when_no_analysis_is_loaded() -> None:
    tools = ToolRegistry(ToolContext())
    result = await tools.dispatch("inspect_workbook", {"section": "sheets"})
    assert not result.ok
    assert "kedge inspect" in result.text


async def test_get_plan_says_so_when_no_plan_exists(tmp_path: Path) -> None:
    from kedge.plan.store import PlanStore

    tools = ToolRegistry(ToolContext(plans=PlanStore(tmp_path / "plans")))
    result = await tools.dispatch("get_plan", {})
    assert not result.ok
    assert "kedge plan" in result.text


async def test_list_utils_degrades_when_the_package_is_absent() -> None:
    tools = ToolRegistry(ToolContext(utils_dir=None))
    result = await tools.dispatch("list_utils", {})
    assert result.ok
    assert "no utils package" in result.text


async def test_list_utils_degrades_when_the_package_is_empty(tmp_path: Path) -> None:
    empty = tmp_path / "utils"
    empty.mkdir()
    tools = ToolRegistry(ToolContext(utils_dir=empty))
    result = await tools.dispatch("list_utils", {})
    assert result.ok
    assert "empty" in result.text


async def test_get_knowledge_degrades_when_there_is_no_pack() -> None:
    tools = ToolRegistry(ToolContext(knowledge_dir=None))
    result = await tools.dispatch("get_knowledge", {})
    assert result.ok
    assert "no knowledge pack" in result.text


async def test_reconcile_without_a_workspace_degrades_to_not_reconciled() -> None:
    tools = ToolRegistry(ToolContext(driver=FakeDriver()))
    result = await tools.dispatch(
        "reconcile", {"variable": "apply_haircuts", "reference": "Calc!H2:H50000"}
    )
    assert not result.ok
    assert "NOT RECONCILED" in result.text
    assert "has not passed" in result.text


async def test_reconcile_refuses_an_unqualified_reference() -> None:
    tools = ToolRegistry(ToolContext(driver=FakeDriver()))
    result = await tools.dispatch("reconcile", {"variable": "x", "reference": "H2:H500"})
    assert not result.ok
    assert "sheet-qualified" in result.text


async def test_amend_plan_records_rather_than_applies() -> None:
    tools = ToolRegistry(ToolContext())
    result = await tools.dispatch(
        "amend_plan",
        {
            "rationale": "Calc!AF is a second haircut the plan does not mention",
            "change": "add a stage between apply_haircuts and reconcile",
            "stage": "apply_haircuts",
        },
    )
    assert result.ok
    assert "NOT in force" in result.text
    assert tools.pending_amendments[0].stage == "apply_haircuts"


# ── reading a real workbook ──────────────────────────────────────────────────────────────────


def _write_workbook(path: Path) -> None:
    book = OpenpyxlWorkbook()
    sheet = book.active
    sheet.title = "Ref"
    sheet.append(["asset_class", "counterparty", "haircut"])
    sheet.append(["gilts", SENTINEL, 0.02])
    for index in range(2, 60):
        sheet.append([f"class-{index}", f"cp-{index}", index / 100])
    book.save(path)


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    monkeypatch.setenv("KEDGE_HOME", str(tmp_path / "home"))
    workbook = tmp_path / "rwa_monthly.xlsx"
    _write_workbook(workbook)
    built = Workspace.for_workbook(workbook)
    built.ensure_dirs()
    return built


async def test_read_range_returns_verbatim_values(workspace: Workspace) -> None:
    tools = ToolRegistry(ToolContext(workspace=workspace))
    try:
        result = await tools.dispatch("read_range", {"sheet": "Ref", "range": "A1:C3"})
        assert result.ok
        assert SENTINEL in result.text
        assert result.sheet == "Ref"
        assert result.columns == ("A", "B", "C")
    finally:
        await tools.aclose()


async def test_read_range_reports_what_it_could_not_return(workspace: Workspace) -> None:
    tools = ToolRegistry(ToolContext(workspace=workspace, caps=Caps(max_rows=5)))
    try:
        result = await tools.dispatch("read_range", {"sheet": "Ref", "range": "A1:C60"})
        assert result.row_count == 5
        assert "[… 55 more rows omitted]" in result.text
    finally:
        await tools.aclose()


async def test_sample_data_resolves_the_header_row(workspace: Workspace) -> None:
    tools = ToolRegistry(ToolContext(workspace=workspace))
    try:
        result = await tools.dispatch("sample_data", {"sheet": "Ref", "rows": 3})
        assert result.ok
        assert "asset_class" in result.text
        assert "haircut" in result.text
        assert SENTINEL in result.text
    finally:
        await tools.aclose()


async def test_sample_data_hashes_a_redacted_column(workspace: Workspace) -> None:
    tools = ToolRegistry(ToolContext(workspace=workspace, redaction_patterns=("counterparty",)))
    try:
        result = await tools.dispatch("sample_data", {"sheet": "Ref", "rows": 3})
        assert SENTINEL not in result.text
        assert "sha256:" in result.text
        assert result.redacted_columns == 1
    finally:
        await tools.aclose()


async def test_an_unknown_sheet_is_reported_rather_than_raised(workspace: Workspace) -> None:
    tools = ToolRegistry(ToolContext(workspace=workspace))
    try:
        result = await tools.dispatch("read_range", {"sheet": "Nope", "range": "A1:B2"})
        assert "does not exist" in result.text
    finally:
        await tools.aclose()


# ── reconciliation against a real workbook ───────────────────────────────────────────────────


def _write_calc_workbook(path: Path, values: list[float]) -> None:
    book = OpenpyxlWorkbook()
    sheet = book.active
    sheet.title = "Calc"
    sheet["H1"] = "haircut_exposure"
    for offset, value in enumerate(values, start=2):
        sheet[f"H{offset}"] = value
    book.save(path)


def _write_notebook(workspace: Workspace, produced: list[float]) -> None:
    workspace.notebook_path.parent.mkdir(parents=True, exist_ok=True)
    workspace.notebook_path.write_text(f"apply_haircuts = {produced!r}\n", encoding="utf-8")


@pytest.fixture
def reconcilable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    monkeypatch.setenv("KEDGE_HOME", str(tmp_path / "home"))
    workbook = tmp_path / "rwa_monthly.xlsx"
    _write_calc_workbook(workbook, [1.0, 2.0, 3.0])
    built = Workspace.for_workbook(workbook)
    built.ensure_dirs()
    return built


async def test_reconcile_passes_when_the_notebook_reproduces_the_workbook(
    reconcilable: Workspace,
) -> None:
    _write_notebook(reconcilable, [1.0, 2.0, 3.0])
    tools = ToolRegistry(ToolContext(workspace=reconcilable))
    result = await tools.dispatch(
        "reconcile",
        {"variable": "apply_haircuts", "reference": "Calc!H2:H4", "region_id": "haircuts"},
    )
    assert result.ok, result.text
    assert "PASSED" in result.text
    assert "3" in result.text


async def test_reconcile_reports_the_mismatching_rows_side_by_side(
    reconcilable: Workspace,
) -> None:
    _write_notebook(reconcilable, [1.0, 2.0, 3.5])
    tools = ToolRegistry(ToolContext(workspace=reconcilable))
    result = await tools.dispatch(
        "reconcile", {"variable": "apply_haircuts", "reference": "Calc!H2:H4"}
    )
    assert not result.ok
    assert "FAILED" in result.text
    assert "3.5" in result.text
    assert "expected" in result.text and "actual" in result.text


async def test_reconcile_says_so_when_the_notebook_does_not_define_the_variable(
    reconcilable: Workspace,
) -> None:
    _write_notebook(reconcilable, [1.0])
    tools = ToolRegistry(ToolContext(workspace=reconcilable))
    result = await tools.dispatch(
        "reconcile", {"variable": "manual_overrides", "reference": "Calc!H2:H4"}
    )
    assert not result.ok
    assert "does not define 'manual_overrides'" in result.text
    assert "apply_haircuts" in result.text


async def test_reconcile_says_so_when_the_notebook_will_not_run(
    reconcilable: Workspace,
) -> None:
    reconcilable.notebook_path.parent.mkdir(parents=True, exist_ok=True)
    reconcilable.notebook_path.write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    tools = ToolRegistry(ToolContext(workspace=reconcilable))
    result = await tools.dispatch(
        "reconcile", {"variable": "apply_haircuts", "reference": "Calc!H2:H4"}
    )
    assert not result.ok
    assert "NOT RECONCILED" in result.text
    assert "could not be run" in result.text
