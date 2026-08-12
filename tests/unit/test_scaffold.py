"""The scaffolder: an approved plan in, the notebook the user actually gets out.

This module is the last thing between a reviewed plan and forty cells somebody has to live
with, so the assertions here are mostly about properties of the *emitted text* rather than
about return values:

- every cell body parses, because a cell that does not is a notebook that will not open;
- no public name is defined by two cells, because marimo rejects the batch if one is;
- the head calls `kedge.ingest` and `kedge.contracts` and the tail calls `kedge.reconcile`,
  rather than re-implementing any of them;
- and the reconciliation cell says "not reconciled" wherever it cannot say anything else.

That last one is not a style point. A reconciliation that reports a pass it did not earn is
the single most dangerous thing kedge could emit (PLAN 6.2), so the tail cell is executed
here -- against a missing workbook and against a real one with no cached values -- and the
resulting panel is asserted to be falsy and to say so in words.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING, Any

import polars as pl
import pytest
from openpyxl import Workbook

import kedge
import kedge.contracts
import kedge.ingest
import kedge.ingest.drift
import kedge.reconcile
from conftest import make_draft, make_plan
from kedge.notebook import scaffold
from kedge.notebook.scaffold import (
    HEAD_CELL_NAMES,
    TAIL_CELL_NAMES,
    PlanNotApprovedError,
    ScaffoldCell,
    ScaffoldError,
    build_cells,
    cell_name_for,
    scaffold_notebook,
)
from kedge.plan.model import (
    Approval,
    ApprovalState,
    Checkpoint,
    ProcessPlan,
    Stage,
    StageKind,
)
from kedge.reconcile.model import ReconciliationStatus

if TYPE_CHECKING:
    from collections.abc import Iterable


# ── helpers ──────────────────────────────────────────────────────────────────


def approved(**overrides: Any) -> ProcessPlan:
    """The shared fixture plan, with the review gate satisfied."""
    plan = make_plan(**overrides)
    return plan.model_copy(update={"approval": Approval(state=ApprovalState.APPROVED)})


def cells_for(plan: ProcessPlan | None = None, **kwargs: Any) -> list[ScaffoldCell]:
    return build_cells(plan if plan is not None else approved(), **kwargs)


def named(cells: Iterable[ScaffoldCell], name: str) -> ScaffoldCell:
    """The one cell with this name; a KeyError here is itself a useful failure."""
    for cell in cells:
        if cell.name == name:
            return cell
    raise KeyError(name)


def public_names(code: str) -> set[str]:
    """Every module-level name a cell body binds, excluding marimo's `_` escape hatch.

    Deliberately generous -- it walks the whole tree rather than only the top level -- because
    a name bound inside a `try` or an `if` is just as much a definition as far as marimo's
    static analysis is concerned.
    """
    tree = ast.parse(code)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            found.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                found.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.add(node.name)
    return {name for name in found if not name.startswith("_")}


class RecordingDriver:
    """A `CellCreator` that records rather than writing. No kernel, no marimo."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []

    async def create_cell(self, code: str, *, name: str, hide_code: bool = False) -> str:
        self.calls.append((name, code, hide_code))
        return f"id-{len(self.calls)}"


def plain_workbook(path: Path) -> Path:
    """A workbook of literal values and not one formula: the no-baseline case."""
    book = Workbook()
    sheet = book.active
    sheet.title = "Data"
    sheet.append(["id", "amount"])
    for row in range(1, 6):
        sheet.append([row, row * 1.5])
    book.save(path)
    return path


class StoppedError(Exception):
    """Stands in for marimo's `MarimoStopError`, which needs a kernel to raise for real."""


class FakeMarimo:
    """Just enough of `mo` for the head cells to run outside a notebook.

    `mo.stop` halting the cell *and its descendants* is the mechanism the contract gate and the
    empty-selection guard both rely on, so it is modelled as an exception rather than stubbed
    out: a test that could not tell a block from a pass would be worse than no test.

    Everything passed to `mo.md` is kept, so a test can assert on the panel the user reads and
    not merely on the values the cell happened to bind.
    """

    class ui:  # noqa: N801 - mirrors marimo's own lowercase namespace
        pass

    def __init__(self) -> None:
        self.rendered: list[str] = []

    def md(self, text: str) -> str:
        self.rendered.append(text)
        return text

    def vstack(self, items: list[Any]) -> list[Any]:
        return items

    @staticmethod
    def stop(predicate: object, output: Any = None) -> None:
        if predicate:
            raise StoppedError(output)

    def panel(self, heading: str) -> str:
        """The one rendered panel starting with this markdown heading."""
        for text in self.rendered:
            if text.startswith(heading):
                return text
        raise AssertionError(f"no panel headed {heading!r} in {self.rendered}")


class Selection:
    """A stand-in for a marimo file input: the `.value` is all the cells touch."""

    def __init__(self, value: tuple[Any, ...] = ()) -> None:
        self.value = value


def run_head(
    cells: list[ScaffoldCell],
    *,
    store: Path,
    picked: Path | None = None,
    contract: Path | None = None,
) -> dict[str, Any]:
    """Execute the five head cells below the selector against the real ingest machinery.

    The setup and selector cells are supplied rather than run: one imports marimo for real and
    the other builds UI elements that need a kernel. Everything below them is ordinary Python
    and is exactly what this is here to exercise.
    """
    import datetime as datetime_module
    import pathlib

    namespace: dict[str, Any] = {
        "mo": FakeMarimo(),
        "pl": pl,
        "kedge": kedge,
        "datetime": datetime_module,
        "pathlib": pathlib,
        "HANDIN_DIR": store,
        "CONTRACT_PATH": contract if contract is not None else store / "absent.yaml",
        "WORKBOOK": store / "absent.xlsx",
        "handin_drop": Selection(),
        "handin_pick": Selection((picked,) if picked is not None else ()),
    }
    for name in HEAD_CELL_NAMES[2:]:
        exec(compile(named(cells, name).code, f"<{name}>", "exec"), namespace)
    return namespace


def run_reconciliation(cell: ScaffoldCell, workbook: Path, **frames: Any) -> Any:
    """Execute the reconciliation cell body and hand back the panel it produced.

    The cell reads `WORKBOOK`, `kedge` and the stage cells' frames out of the notebook's
    globals, so supplying those is the whole of the harness. `mo` is deliberately absent: the
    tail must not need marimo to state that nothing was checked.
    """
    namespace: dict[str, Any] = {"WORKBOOK": workbook, "kedge": kedge, **frames}
    exec(compile(cell.code, "<reconciliation>", "exec"), namespace)
    return namespace["reconciliation"]


# ── the review gate ──────────────────────────────────────────────────────────


def test_build_cells_refuses_an_unapproved_plan() -> None:
    """The structural half of the review gate (PLAN 2.2, M2 step 3)."""
    with pytest.raises(PlanNotApprovedError) as caught:
        build_cells(make_plan())
    assert "'draft', not 'approved'" in str(caught.value)


def test_the_refusal_names_the_blockers_a_reviewer_has_to_clear() -> None:
    with pytest.raises(PlanNotApprovedError) as caught:
        build_cells(make_plan())

    assert "Calc!AK:AP" in str(caught.value)
    assert "has not been acknowledged" in str(caught.value)


def test_an_unapproved_plan_can_be_previewed_but_not_written() -> None:
    """`build_cells` has an escape hatch because it writes nothing; scaffolding has none."""
    preview = build_cells(make_plan(), allow_unapproved=True)
    assert [cell.name for cell in preview[: len(HEAD_CELL_NAMES)]] == list(HEAD_CELL_NAMES)


def test_scaffold_notebook_has_no_way_to_write_an_unapproved_plan() -> None:
    import inspect

    parameters = inspect.signature(scaffold_notebook).parameters
    assert "allow_unapproved" not in parameters


@pytest.mark.parametrize(
    "state",
    [ApprovalState.DRAFT, ApprovalState.CHANGES_REQUESTED, ApprovalState.REJECTED],
)
async def test_scaffold_notebook_writes_nothing_without_approval(state: ApprovalState) -> None:
    plan = make_plan().model_copy(update={"approval": Approval(state=state)})
    driver = RecordingDriver()

    with pytest.raises(PlanNotApprovedError):
        await scaffold_notebook(plan, driver)

    assert driver.calls == []


async def test_scaffold_notebook_creates_every_cell_visible() -> None:
    """`create_cell` hides code by default, and kedge always overrides that (PLAN 1.1)."""
    driver = RecordingDriver()

    created = await scaffold_notebook(approved(), driver)

    # Names, not driver-assigned ids: both real drivers return a MutationResult, so an id is not
    # available here and pretending otherwise is what made `ty` flag the hub's call site.
    assert created == [cell.name for cell in cells_for()]
    assert [name for name, _, _ in driver.calls] == [cell.name for cell in cells_for()]
    assert all(hide_code is False for _, _, hide_code in driver.calls)


async def test_scaffold_notebook_passes_the_paths_through_to_the_setup_cell(
    tmp_path: Path,
) -> None:
    driver = RecordingDriver()

    await scaffold_notebook(
        approved(),
        driver,
        handins_dir=tmp_path / "handins",
        workbook_path=tmp_path / "source.xlsx",
        contract_path=tmp_path / "agreed.yaml",
    )

    setup = driver.calls[0][1]
    assert repr(str(tmp_path / "source.xlsx")) in setup
    assert repr(str(tmp_path / "agreed.yaml")) in setup


# ── every cell is real Python ────────────────────────────────────────────────


@pytest.mark.parametrize("cell", cells_for(), ids=lambda cell: cell.name)
def test_every_generated_cell_body_parses(cell: ScaffoldCell) -> None:
    ast.parse(cell.code)


@pytest.mark.parametrize(
    "plan",
    [
        approved(),
        approved(draft=make_draft(dropped=[])),
        approved(
            draft=make_draft(
                stages=[Stage(id="only", intent="One stage and nothing else")],
                open_questions=[],
                dropped=[],
            )
        ),
        approved(
            draft=make_draft(
                stages=[
                    Stage(
                        id="sign_off",
                        intent="Someone has to look at this",
                        kind=StageKind.CHECKPOINT,
                        checkpoint=Checkpoint(question="Fine?", require_note=False),
                    )
                ],
                open_questions=[],
                dropped=[],
            )
        ),
    ],
    ids=["standard", "no-drops", "single-stage", "checkpoint-only"],
)
def test_every_cell_parses_for_every_plan_shape(plan: ProcessPlan) -> None:
    for cell in build_cells(plan):
        ast.parse(cell.code)


def test_a_cell_that_would_not_parse_is_refused_rather_than_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scaffolder marks its own homework; a broken cell must not reach the kernel."""
    monkeypatch.setattr(scaffold, "_SOURCE_CELL", "handin_source = mo.ui.tabs(")

    with pytest.raises(ScaffoldError, match="would not parse"):
        build_cells(approved())


def test_a_cell_that_would_import_pandas_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-negotiable 1, checked on the emitted text rather than trusted (PLAN 2.5)."""
    plan = approved(
        draft=make_draft(
            stages=[Stage(id="load", intent="Read it", notes="import pandas as pd first")],
            open_questions=[],
            dropped=[],
        )
    )

    with pytest.raises(ScaffoldError, match="pandas"):
        build_cells(plan)


def test_no_cell_mentions_pandas_or_the_private_marimo_api() -> None:
    """Non-negotiables 1 and 2, on the text a user will run."""
    for cell in cells_for():
        assert "pandas" not in cell.code
        assert "_code_mode" not in cell.code


# ── names, ordering and the single-definition rule ───────────────────────────


def test_the_head_the_stages_and_the_tail_are_emitted_in_that_order() -> None:
    cells = cells_for()
    names = [cell.name for cell in cells]

    assert names[: len(HEAD_CELL_NAMES)] == list(HEAD_CELL_NAMES)
    assert names[-len(TAIL_CELL_NAMES) :] == list(TAIL_CELL_NAMES)
    assert names[len(HEAD_CELL_NAMES) : -len(TAIL_CELL_NAMES)] == [
        "load_handin",
        "apply_haircuts",
        "manual_overrides_ui",
        "manual_overrides",
        "write_output",
    ]


def test_the_head_reports_drift_before_the_contract_check() -> None:
    """PLAN 2.8: a renamed column is a better message than a schema-validation traceback."""
    names = list(HEAD_CELL_NAMES)
    assert names.index("handin_drift") < names.index("handin_check")
    assert names.index("handin_check") < names.index("handin_frame")


def test_the_contract_is_loaded_before_anything_that_has_to_read_the_same_rows() -> None:
    """Loading is not checking: the sheet is needed by the profile, the check and the frame."""
    names = list(HEAD_CELL_NAMES)
    assert names.index("handin") < names.index("handin_contract")
    assert names.index("handin_contract") < names.index("handin_drift")

    loader = named(cells_for(), "handin_contract")
    assert "kedge.contracts.load(CONTRACT_PATH)" in loader.code
    assert "kedge.contracts.validate" not in loader.code  # loading is not checking


def test_stage_cells_carry_their_stage_id_and_the_fixed_cells_do_not() -> None:
    cells = cells_for()
    assert named(cells, "apply_haircuts").stage_id == "apply_haircuts"
    assert named(cells, "manual_overrides_ui").stage_id == "manual_overrides"
    assert named(cells, "kedge_setup").stage_id is None
    assert named(cells, "reconciliation").stage_id is None


def test_every_cell_has_a_role_from_the_vocabulary() -> None:
    roles = {cell.role for cell in cells_for()}
    assert roles == {"setup", "handin", "stage", "checkpoint", "reconcile"}


def test_no_public_name_is_defined_by_two_cells() -> None:
    """marimo's single-definition rule; `_`-prefixed names are the escape hatch."""
    owner: dict[str, str] = {}
    for cell in cells_for():
        for name in public_names(cell.code):
            assert name not in owner, f"{name!r} defined by {owner.get(name)!r} and {cell.name!r}"
            owner[name] = cell.name


def test_a_checkpoints_ui_names_cannot_collide_with_another_stage() -> None:
    """A checkpoint owns three more public names than its own, and all four must be unique."""
    plan = approved(
        draft=make_draft(
            stages=[
                Stage(id="review_decision", intent="A stage that wants the checkpoint's name"),
                Stage(
                    id="review",
                    intent="The checkpoint itself",
                    kind=StageKind.CHECKPOINT,
                    checkpoint=Checkpoint(question="Approved?"),
                ),
            ],
            open_questions=[],
            dropped=[],
        )
    )

    owner: dict[str, str] = {}
    for cell in build_cells(plan):
        for name in public_names(cell.code):
            assert name not in owner, f"{name!r} defined by {owner.get(name)!r} and {cell.name!r}"
            owner[name] = cell.name


@pytest.mark.parametrize(
    ("stage_id", "expected"),
    [
        ("apply_haircuts", "apply_haircuts"),
        ("Reconcile-EOD", "reconcile_eod"),
        ("step 1", "step_1"),
        ("1_load", "stage_1_load"),
        ("class", "class_stage"),
        ("match", "match_stage"),
        ("!!!x!!!", "x"),
    ],
)
def test_cell_name_for_produces_a_usable_identifier(stage_id: str, expected: str) -> None:
    name = cell_name_for(stage_id)
    assert name == expected
    assert name.isidentifier()


@pytest.mark.parametrize("reserved", ["handin", "handin_frame", "reconciliation", "mo", "kedge"])
def test_cell_name_for_never_collides_with_a_fixed_cell_or_an_import(reserved: str) -> None:
    assert cell_name_for(reserved) != reserved


def test_cell_name_for_suffixes_rather_than_overwriting() -> None:
    assert cell_name_for("load", taken=["load"]) == "load_2"
    assert cell_name_for("load", taken=["load", "load_2"]) == "load_3"


def test_cell_name_for_gives_up_loudly_rather_than_returning_a_duplicate() -> None:
    """A silent duplicate would be a notebook marimo refuses to open."""
    taken = ["load", *(f"load_{index}" for index in range(2, 1000))]

    with pytest.raises(ScaffoldError, match="unique cell name"):
        cell_name_for("load", taken=taken)


# ── the fixed head calls the real machinery ──────────────────────────────────


def test_the_head_carries_no_unfinished_markers() -> None:
    """The stage cells legitimately carry TODOs; the plumbing around them does not."""
    fixed = [cell for cell in cells_for() if cell.role in ("setup", "handin", "reconcile")]
    for cell in fixed:
        assert "TODO" not in cell.code, cell.name


def test_the_handin_cell_calls_ingest_rather_than_hashing_by_hand() -> None:
    code = named(cells_for(), "handin").code
    assert "kedge.ingest.receive(" in code
    assert "store_dir=HANDIN_DIR" in code
    assert "hashlib" not in code


def test_nothing_treats_the_handin_as_a_dict() -> None:
    """`receive` returns a HandIn record; subscripting it would be an AttributeError."""
    for cell in cells_for():
        assert 'handin["' not in cell.code
        assert "handin['" not in cell.code


def test_the_handin_cell_waits_rather_than_erroring_on_an_empty_selection() -> None:
    code = named(cells_for(), "handin").code
    stop = code.index("mo.stop(")
    assert stop < code.index("kedge.ingest.receive(")
    assert "Waiting for a hand-in" in code


def test_the_drift_cell_calls_check_drift_and_degrades_to_a_message() -> None:
    code = named(cells_for(), "handin_drift").code
    assert "kedge.ingest.check_drift(" in code
    assert "except kedge.IngestError" in code
    assert "Could not profile the hand-in" in code


def test_the_check_cell_validates_against_a_contract_when_there_is_one() -> None:
    code = named(cells_for(), "handin_check").code
    assert "kedge.contracts.validate(handin, handin_contract)" in code


def test_the_contract_cell_scaffolds_and_runs_with_no_contract_at_all() -> None:
    """A new notebook has no contract, and that is a state, not an error."""
    code = named(cells_for(), "handin_contract").code
    assert "CONTRACT_PATH.is_file()" in code
    assert "nothing below is enforced" in code
    assert "kedge contract infer" in code


def test_a_failed_contract_stops_the_frame_and_everything_below_it() -> None:
    """mo.stop halts the cell and its descendants, so the check is a gate (PLAN 2.8)."""
    code = named(cells_for(), "handin_frame").code
    stop = code.index("mo.stop(")
    assert stop < code.index("handin_frame = ")
    assert "handin_check is not None and not handin_check.ok" in code


def test_the_frame_goes_through_the_same_reader_the_contract_check_does() -> None:
    """Not an equivalent-looking read: the same function, with the contract's own arguments.

    `kedge.contracts.validate` does `read_data(handin.path, sheet=contract.sheet,
    header_row=contract.header_row)`. Anything else here and the frame the notebook computes on
    is not the frame that was validated. The behaviour is pinned by the executed tests below;
    this pins the mechanism, because a mirrored four-line equivalent is one refactor from
    diverging again.
    """
    code = named(cells_for(), "handin_frame").code
    assert "kedge.ingest.read_data(" in code
    assert "sheet=handin_contract.sheet if handin_contract is not None else None" in code
    assert "header_row=handin_contract.header_row if handin_contract is not None else None" in code
    assert "handin_frame = _data.lazy()" in code
    assert "detect_layout" not in code  # the reader does that, and does it once


def test_the_setup_cell_owns_the_three_paths_and_nothing_computes_them() -> None:
    code = named(cells_for(), "kedge_setup").code
    assert "HANDIN_DIR = pathlib.Path(" in code
    assert "WORKBOOK = pathlib.Path(" in code
    assert "CONTRACT_PATH = pathlib.Path(" in code


def test_the_paths_default_to_the_standard_workspace_layout() -> None:
    """`<workbook>.kedge/handins` beside the workbook is where kedge puts things."""
    code = named(cells_for(handins_dir=Path("C:/p/rwa.kedge/handins")), "kedge_setup").code
    assert repr(str(Path("C:/p/rwa_monthly_v14.xlsx"))) in code
    assert repr(str(Path("C:/p/rwa.kedge/contract.yaml"))) in code


def test_an_explicit_workbook_path_beats_the_derived_one(tmp_path: Path) -> None:
    elsewhere = tmp_path / "archive" / "2026-06.xlsx"
    code = named(cells_for(handins_dir=tmp_path / "h", workbook_path=elsewhere), "kedge_setup").code
    assert repr(str(elsewhere)) in code


# ── the head, executed against the real ingest and contracts packages ────────


def handin_file(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "exposures.csv"
    path.write_text("id,amount\n1,10.5\n2,20.25\n3,30.0\n", encoding="utf-8")
    return path


def test_the_head_runs_end_to_end_with_no_contract(tmp_path: Path) -> None:
    """The state a notebook is in on its very first run: a file, no contract, nothing enforced."""
    picked = handin_file(tmp_path / "incoming")

    namespace = run_head(cells_for(), store=tmp_path / "store", picked=picked)

    handin = namespace["handin"]
    assert handin.original_name == "exposures.csv"
    assert handin.path.parent.is_relative_to(tmp_path / "store")  # the managed copy, not the drop
    assert handin.sha256
    assert namespace["handin_drift"].is_first_hand_in
    assert namespace["handin_contract"] is None
    assert namespace["handin_check"] is None
    assert namespace["handin_frame"].collect().height == 3


def test_the_head_waits_rather_than_erroring_when_nothing_is_selected(tmp_path: Path) -> None:
    with pytest.raises(StoppedError, match="Waiting for a hand-in"):
        run_head(cells_for(), store=tmp_path / "store")


def test_a_hand_in_that_fails_its_contract_never_reaches_the_frame(tmp_path: Path) -> None:
    """The contract gate, executed: mo.stop fires before handin_frame is defined (PLAN 2.8)."""
    from kedge.contracts import ColumnContract, Contract, save

    picked = handin_file(tmp_path / "incoming")
    contract = tmp_path / "contract.yaml"
    save(
        Contract(
            name="exposures",
            columns=[ColumnContract(name="counterparty_id", dtype="Int64")],
        ),
        contract,
    )

    with pytest.raises(StoppedError, match="does not satisfy its contract"):
        run_head(cells_for(), store=tmp_path / "store", picked=picked, contract=contract)


def test_a_hand_in_that_satisfies_its_contract_passes_through(tmp_path: Path) -> None:
    from kedge.contracts import ColumnContract, Contract, save

    picked = handin_file(tmp_path / "incoming")
    contract = tmp_path / "contract.yaml"
    save(
        Contract(
            name="exposures",
            columns=[
                ColumnContract(name="id", dtype="Int64"),
                ColumnContract(name="amount", dtype="Float64"),
            ],
        ),
        contract,
    )

    namespace = run_head(cells_for(), store=tmp_path / "store", picked=picked, contract=contract)

    assert namespace["handin_check"].ok
    assert namespace["handin_frame"].collect().height == 3


def test_a_contract_that_will_not_load_says_so_without_blocking(tmp_path: Path) -> None:
    """A YAML typo must not put the data out of reach, but it must not look controlled either."""
    picked = handin_file(tmp_path / "incoming")
    contract = tmp_path / "contract.yaml"
    contract.write_text("name: [unclosed\n", encoding="utf-8")

    namespace = run_head(cells_for(), store=tmp_path / "store", picked=picked, contract=contract)

    assert namespace["handin_check"] is None
    assert namespace["handin_contract"] is None
    assert namespace["handin_frame"].collect().height == 3


def test_the_frame_skips_a_preamble_the_way_the_contract_check_does(tmp_path: Path) -> None:
    """Reading it any other way means the notebook computes on a different frame than was
    validated, and the difference is a row."""
    incoming = tmp_path / "incoming"
    incoming.mkdir(parents=True)
    picked = incoming / "exposures.csv"
    picked.write_text("Monthly extract,,\nid,amount\n1,10.5\n2,20.25\n", encoding="utf-8")

    namespace = run_head(cells_for(), store=tmp_path / "store", picked=picked)

    frame = namespace["handin_frame"].collect()
    assert frame.columns[:2] == ["id", "amount"]
    assert frame.height == 2
    # And it says what it skipped, so a row count that does not match the file is explicable.
    assert "_Layout: skipped 1 preamble row(s) above the header._" in namespace["mo"].rendered


# ── the frame is the frame that was validated ────────────────────────────────
#
# The dangerous shape of this bug is not a crash. It is a green contract panel over rows
# nobody computed on: `validate` reads `contract.sheet` and `contract.header_row`, so anything
# in the frame cell that does not is a check of one thing and a calculation on another.


def two_sheet_workbook(directory: Path) -> Path:
    """A cover page first, the data on a named sheet second. The layout that exposed this."""
    directory.mkdir(parents=True, exist_ok=True)
    book = Workbook()
    cover = book.active
    cover.title = "Cover"
    cover.append(["report", "generated"])
    cover.append(["exposures", "2026-06-30"])
    data = book.create_sheet("Data")
    data.append(["id", "amount"])
    for row in range(1, 6):
        data.append([row, row * 1.5])
    path = directory / "exposures.xlsx"
    book.save(path)
    return path


def contract_at(path: Path, **fields: Any) -> Path:
    from kedge.contracts import ColumnContract, Contract, save

    save(
        Contract(
            name="exposures",
            columns=[
                ColumnContract(name="id", dtype="Int64"),
                ColumnContract(name="amount", dtype="Float64"),
            ],
            **fields,
        ),
        path,
    )
    return path


def test_a_contract_naming_a_sheet_is_checked_and_computed_on_the_same_sheet(
    tmp_path: Path,
) -> None:
    """The reviewed defect: the check passed on 'Data' while the frame held the cover page."""
    picked = two_sheet_workbook(tmp_path / "incoming")
    contract = contract_at(tmp_path / "contract.yaml", sheet="Data")

    namespace = run_head(cells_for(), store=tmp_path / "store", picked=picked, contract=contract)

    validated, _layout = kedge.ingest.drift.read_data(
        namespace["handin"].path, sheet="Data", header_row=None
    )
    frame = namespace["handin_frame"].collect()
    assert namespace["handin_check"].ok
    assert frame.columns == validated.columns == ["id", "amount"]
    assert frame.height == validated.height == 5


def test_a_contract_pinning_a_header_row_is_checked_and_computed_on_the_same_rows(
    tmp_path: Path,
) -> None:
    """A header the detector would not have chosen, so honouring the contract is observable."""
    incoming = tmp_path / "incoming"
    incoming.mkdir(parents=True)
    picked = incoming / "exposures.csv"
    picked.write_text("region,currency\nid,amount\n1,10.5\n2,20.25\n", encoding="utf-8")
    contract = contract_at(tmp_path / "contract.yaml", header_row=1)

    # What the frame cell would produce if it let kedge detect the header, as it used to.
    detected, _layout = kedge.ingest.drift.read_data(picked)
    assert detected.columns == ["region", "currency"]

    namespace = run_head(cells_for(), store=tmp_path / "store", picked=picked, contract=contract)

    frame = namespace["handin_frame"].collect()
    assert namespace["handin_check"].ok
    assert frame.columns == ["id", "amount"]
    assert frame.height == 2


def test_the_shape_profile_is_taken_from_the_sheet_the_contract_names(tmp_path: Path) -> None:
    """Drift off the wrong sheet reports every column as renamed, twice a year, wrongly."""
    picked = two_sheet_workbook(tmp_path / "incoming")
    contract = contract_at(tmp_path / "contract.yaml", sheet="Data")

    namespace = run_head(cells_for(), store=tmp_path / "store", picked=picked, contract=contract)

    profile = namespace["handin_profile"]
    assert profile.sheet == "Data"
    assert profile.column_names == ("id", "amount")
    assert profile.row_count == 5


def test_the_shape_panel_says_what_actually_arrived(tmp_path: Path) -> None:
    """PLAN 2.8: the user sees what arrived before any processing happens."""
    picked = handin_file(tmp_path / "incoming")

    namespace = run_head(cells_for(), store=tmp_path / "store", picked=picked)

    panel = namespace["mo"].panel("### Hand-in shape")
    assert "3 rows, 2 columns" in panel
    assert "`id` Int64" in panel
    assert "`amount` Float64" in panel


def test_a_very_wide_hand_in_is_summarised_rather_than_rendered_whole(tmp_path: Path) -> None:
    """A 200-column extract as one markdown line is a wall, not information."""
    incoming = tmp_path / "incoming"
    incoming.mkdir(parents=True)
    picked = incoming / "wide.csv"
    headers = [f"c{index}" for index in range(30)]
    picked.write_text(
        ",".join(headers) + "\n" + ",".join(str(index) for index in range(30)) + "\n",
        encoding="utf-8",
    )

    namespace = run_head(cells_for(), store=tmp_path / "store", picked=picked)

    panel = namespace["mo"].panel("### Hand-in shape")
    assert "`c23` Int64" in panel
    assert "`c24`" not in panel
    assert "and 6 more -- see `handin_profile`" in panel


def test_the_contract_panel_names_the_rows_everything_below_will_read(tmp_path: Path) -> None:
    picked = two_sheet_workbook(tmp_path / "incoming")
    contract = contract_at(tmp_path / "contract.yaml", sheet="Data", header_row=0)

    namespace = run_head(cells_for(), store=tmp_path / "store", picked=picked, contract=contract)

    panel = namespace["mo"].panel("### Contract")
    assert "sheet `Data`" in panel
    assert "header row 0" in panel


def test_the_contract_panel_says_so_when_there_is_no_contract(tmp_path: Path) -> None:
    picked = handin_file(tmp_path / "incoming")

    namespace = run_head(cells_for(), store=tmp_path / "store", picked=picked)

    assert "nothing below is enforced" in namespace["mo"].panel("### Contract")
    assert "nothing was checked" in namespace["mo"].panel("### Contract check")


# ── checkpoints ──────────────────────────────────────────────────────────────


def test_a_checkpoint_scaffolds_a_ui_cell_and_a_blocking_gate() -> None:
    cells = cells_for()
    ui = named(cells, "manual_overrides_ui")
    gate = named(cells, "manual_overrides")

    assert ui.role == "checkpoint" and gate.role == "checkpoint"
    assert "mo.ui.dropdown(" in ui.code
    assert "mo.ui.text_area(" in ui.code
    assert "mo.stop(" not in ui.code
    assert gate.code.count("mo.stop(") == 2


def test_the_gate_blocks_on_anything_but_the_first_option() -> None:
    """The first option is the one that unblocks; everything else stops the graph."""
    gate = named(cells_for(), "manual_overrides").code
    assert "manual_overrides_decision.value != 'approve'" in gate


def test_a_checkpoint_that_does_not_require_a_note_gets_one_gate_not_two() -> None:
    plan = approved(
        draft=make_draft(
            stages=[
                Stage(
                    id="sign_off",
                    intent="Someone has to look at this",
                    kind=StageKind.CHECKPOINT,
                    checkpoint=Checkpoint(question="Fine?", require_note=False),
                )
            ],
            open_questions=[],
            dropped=[],
        )
    )

    gate = named(build_cells(plan), "sign_off").code
    assert gate.count("mo.stop(") == 1
    assert "A reason is required" not in gate


def test_a_downstream_stage_reads_the_checkpoint_so_the_block_reaches_it() -> None:
    """A gated stage has to reference the checkpoint, or mo.stop never touches it."""
    code = named(cells_for(), "write_output").code
    assert '_gate_write_output = manual_overrides["decision"]' in code


def test_a_checkpoint_renders_the_guidance_a_reviewer_needs() -> None:
    plan = approved(
        draft=make_draft(
            stages=[
                Stage(
                    id="sign_off",
                    intent="Someone has to look at this",
                    kind=StageKind.CHECKPOINT,
                    checkpoint=Checkpoint(
                        question="Fine?",
                        guidance="Compare the override sheet against last month's sign-off.",
                    ),
                )
            ],
            open_questions=[],
            dropped=[],
        )
    )

    ui = named(build_cells(plan), "sign_off_ui").code
    assert "# Guidance: Compare the override sheet" in ui


def test_a_checkpoint_never_becomes_the_frame_a_stage_builds_on() -> None:
    """Its output is a decision record, not a frame."""
    code = named(cells_for(), "write_output").code
    assert "write_output = apply_haircuts.collect()" in code


def test_a_stage_listing_its_checkpoint_first_still_builds_on_a_frame() -> None:
    """The checkpoint gates the stage; the frame it transforms is the next dependency along."""
    plan = approved(
        draft=make_draft(
            stages=[
                Stage(id="load_it", intent="Read the hand-in", kind=StageKind.LOAD),
                Stage(
                    id="sign_off",
                    intent="Someone has to look at this",
                    kind=StageKind.CHECKPOINT,
                    depends_on=["load_it"],
                    checkpoint=Checkpoint(question="Fine?"),
                ),
                Stage(
                    id="summarise",
                    intent="Total it up",
                    depends_on=["sign_off", "load_it"],
                ),
            ],
            open_questions=[],
            dropped=[],
        )
    )

    code = named(build_cells(plan), "summarise").code
    assert "summarise = load_it" in code
    assert '_gate_summarise = sign_off["decision"]' in code


# ── the reconciliation tail ──────────────────────────────────────────────────


def test_the_tail_calls_reconcile_rather_than_asserting_an_outcome() -> None:
    code = named(cells_for(), "reconciliation").code
    assert "kedge.reconcile.reconcile_panel(WORKBOOK, reconciliation_values)" in code


def test_the_tail_maps_analysis_operation_ids_to_the_cells_that_reproduce_them() -> None:
    """`Stage.operations` is the link back to the facts, and this is what consumes it."""
    code = named(cells_for(), "reconciliation").code
    assert "'calc_h2_h500': apply_haircuts," in code


def test_an_operation_claimed_by_two_stages_is_mapped_once() -> None:
    """A duplicate key in the literal would silently keep whichever stage came last."""
    plan = approved(
        draft=make_draft(
            stages=[
                Stage(id="first", intent="Half of it", operations=["calc_h2_h500"]),
                Stage(
                    id="second",
                    intent="The other half",
                    depends_on=["first"],
                    operations=["calc_h2_h500"],
                ),
            ],
            open_questions=[],
            dropped=[],
        )
    )

    code = named(build_cells(plan), "reconciliation").code
    assert code.count("'calc_h2_h500':") == 1
    assert "'calc_h2_h500': first," in code


def test_a_stage_naming_no_operation_contributes_no_region() -> None:
    """There is no honest guess about which workbook range an unlinked stage reproduces."""
    plan = approved(
        draft=make_draft(
            stages=[Stage(id="only", intent="No operations named")],
            open_questions=[],
            dropped=[],
        )
    )

    code = named(build_cells(plan), "reconciliation").code
    assert "reconciliation_values = {}" in code
    assert "never as" in code and "passed" in code


def test_the_tail_never_uses_the_word_passed_as_a_verdict() -> None:
    """The only route to a pass is through kedge.reconcile, which refuses an unearned one."""
    code = named(cells_for(), "reconciliation").code
    assert "PASSED" not in code
    assert 'reconciliation = "' not in code


def test_a_missing_workbook_reconciles_to_not_reconciled(tmp_path: Path) -> None:
    """Non-negotiable 6, executed rather than asserted on the text."""
    cell = named(cells_for(), "reconciliation")

    panel = run_reconciliation(
        cell,
        tmp_path / "never-existed.xlsx",
        apply_haircuts=pl.DataFrame({"haircut": [0.1, 0.2]}),
    )

    assert panel.status is ReconciliationStatus.NOT_RECONCILED
    assert not panel
    assert "NOT RECONCILED" in panel.report.headline()
    assert "not a pass" in " ".join(panel.report.notes)


def test_a_workbook_with_no_cached_values_reconciles_to_not_reconciled(tmp_path: Path) -> None:
    """The dangerous case: a real workbook, a real run, and nothing to compare against."""
    workbook = plain_workbook(tmp_path / "no-formulas.xlsx")
    cell = named(cells_for(), "reconciliation")

    panel = run_reconciliation(cell, workbook, apply_haircuts=pl.DataFrame({"haircut": [0.1, 0.2]}))

    assert panel.status is ReconciliationStatus.NOT_RECONCILED
    assert not panel
    assert "PASSED" not in kedge.reconcile.panel_html(panel.report)
    assert "not a pass" in " ".join(panel.report.notes).lower()


def test_the_tail_survives_a_reconciliation_that_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure to run is still not a pass, and it must not reach the user as a traceback."""

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise kedge.ReconciliationError("the workbook is a zip bomb")

    monkeypatch.setattr(kedge.reconcile, "reconcile_panel", explode)
    workbook = plain_workbook(tmp_path / "unreadable.xlsx")

    panel = run_reconciliation(
        named(cells_for(), "reconciliation"),
        workbook,
        apply_haircuts=pl.DataFrame({"haircut": [0.1]}),
    )

    assert panel.status is ReconciliationStatus.NOT_RECONCILED
    assert "zip bomb" in " ".join(panel.report.notes)


def test_the_tail_scaffolds_for_a_plan_that_is_nothing_but_checkpoints() -> None:
    plan = approved(
        draft=make_draft(
            stages=[
                Stage(
                    id="sign_off",
                    intent="Someone has to look at this",
                    kind=StageKind.CHECKPOINT,
                    checkpoint=Checkpoint(question="Fine?"),
                )
            ],
            open_questions=[],
            dropped=[],
        )
    )

    code = named(build_cells(plan), "reconciliation").code
    ast.parse(code)
    assert "reconciliation_values = {}" in code
