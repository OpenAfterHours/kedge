"""A notebook with no cells in it, served by a real marimo, and the scaffold landing on it.

`kedge.notebook.codegen.EMPTY_NOTEBOOK` is what the open sequence writes when a workbook has no
notebook yet. It used to carry one placeholder cell doing ``import marimo as mo``, and the
docstring above it said it was "the smallest notebook marimo will open cleanly" -- which reads as
a claim that the cell is load-bearing. It is not, and the cell was expensive: ``kedge_setup``, the
first cell the scaffolder writes, imports ``mo`` too, so on every fresh workbook the preamble
carrying ``pl``, ``kedge.xl``, ``kedge.sql``, ``kedge.runs``, ``WORKBOOK``, ``HANDIN_DIR``,
``CONTRACT_PATH``, ``RUNS_DIR`` and ``ACCEPTANCE_PATH`` collided on ``mo``.

The two bridges failed differently, which is why this file exists beside the unit test rather than
instead of it. `FileNotebookDriver` **refuses** ``kedge_setup`` outright and writes a notebook with
a hole where its preamble should be; the live kernel **accepts** both cells, and the notebook on
disk ends up defining ``mo`` twice, so marimo marks both cells in error and every name in the
preamble is unbound at run time. `tests/unit/test_hub_routes.py` catches the first. Only a real
server catches the second, and only a real server can settle whether marimo serves a cell-free
file at all -- so the claim in that old docstring is tested here rather than believed.

One module-scoped server, and one test, because the interesting assertion ("no cells") is only
true before the scaffold runs and the interesting assertion ("nothing defined twice") is only
available after it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from conftest import serving, wait_for_notebook
from kedge.notebook.codegen import (
    EMPTY_NOTEBOOK,
    analyse_cells,
    analyse_document,
    multiply_defined,
    parse_notebook,
)
from kedge.notebook.scaffold import sync_notebook
from kedge.plan.model import Assessment, Confidence, PlanDraft, ProcessPlan, Stage, StageKind
from kedge.plan.review import approve

if TYPE_CHECKING:
    from collections.abc import Iterator

    from conftest import LiveMarimo

pytestmark = pytest.mark.contract


@pytest.fixture(scope="module")
def empty_marimo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LiveMarimo]:
    """A real ``marimo edit`` started on a notebook that has no cells at all.

    `serving` health-checks the server and bootstraps a kernel session, so this fixture yielding
    at all is already most of the claim: marimo loaded the file and answered on it.
    """
    with serving(tmp_path_factory.mktemp("kedge-contract-empty"), EMPTY_NOTEBOOK) as server:
        yield server


def _approved_plan() -> ProcessPlan:
    """The smallest approved plan that still emits a full head and a stage of its own.

    Built here rather than imported from `tests/unit/conftest.py`, which is not on this
    directory's path. Two stages are enough: the collision is with ``kedge_setup``, and every plan
    that scaffolds at all emits one.
    """
    draft = PlanDraft(
        assessment=Assessment(convertible=0.9, blockers=[]),
        stages=[
            Stage(
                id="load_handin",
                intent="Read the month's exposures from the hand-in",
                kind=StageKind.LOAD,
                sources=["handin"],
                confidence=Confidence.HIGH,
            ),
            Stage(
                id="apply_uplift",
                intent="Apply the agreed uplift to the accrual",
                depends_on=["load_handin"],
                confidence=Confidence.HIGH,
            ),
        ],
        open_questions=[],
        dropped=[],
        summary="One load and one transform, which is all the head cells need to exist.",
    )
    plan = ProcessPlan.from_draft(
        draft,
        workbook="process.xlsx",
        workbook_sha256="a" * 64,
        created_at=datetime(2026, 8, 23, tzinfo=UTC),
    )
    return approve(plan, by="contract tests")


async def test_marimo_serves_a_cell_free_notebook_and_the_scaffold_owns_every_name(
    empty_marimo: LiveMarimo,
) -> None:
    """Three claims, in the order the open sequence makes them.

    That marimo opens a file with no cells and brings no names of its own to it; that the
    scaffold's first cell is then accepted rather than refused for redefining a placeholder's
    name; and that the notebook the kernel persists defines nothing twice. The last two are the
    mechanism rather than the string: a placeholder clashing on ``pl`` instead of ``mo`` fails
    this test in exactly the same place.
    """
    workspace = empty_marimo.workspace

    async with empty_marimo.driver() as driver:
        # marimo loads a cell-free file happily and then puts one *empty* unnamed cell into the
        # kernel, so the editor has somewhere to type. Asserting "no cells" would therefore be
        # asserting the wrong thing, and would fail a fix that is entirely correct. What has to
        # hold is that nothing the notebook arrives with defines a name, because every name in it
        # is then the scaffold's to define.
        before = await driver.list_cells(with_code=True)
        arrived_with = {
            name
            for analysis in analyse_cells([cell.code or "" for cell in before])
            for name in analysis.defs
        }
        assert arrived_with == set(), (
            f"the notebook kedge writes before any plan exists already defines {arrived_with}, "
            f"and the scaffold has to be free to define every one of those itself"
        )

        result = await sync_notebook(
            _approved_plan(),
            driver,
            handins_dir=workspace.handins_dir,
            workbook_path=workspace.workbook_path,
            contract_path=workspace.contract_path,
        )

    refused = [(cell.name, cell.detail) for cell in result.cells if cell.outcome == "refused"]
    assert not refused, f"the kernel refused {refused}"
    assert "kedge_setup" in result.named("created"), (
        "the notebook's whole preamble -- pl, kedge.xl, kedge.sql, WORKBOOK, HANDIN_DIR -- comes "
        "from this one cell, and a conversion missing it still reports success"
    )

    source = wait_for_notebook(workspace.notebook_path, "apply_uplift")
    analyses, _ = analyse_document(parse_notebook(source))
    duplicates = multiply_defined(analyses)
    assert duplicates == {}, f"cells define the same name twice: {duplicates}"
