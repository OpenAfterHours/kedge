"""The fallback bridge against a real ``marimo edit --watch``, and beside the live one.

`tests/unit/test_filedriver.py` proves `FileNotebookDriver` writes the notebook it says it
writes. Nothing there proves marimo agrees -- that the file it produces is one marimo can load,
that marimo notices the write at all, or that a marimo re-save leaves it alone. Those are the
claims that make PLAN 6.1 mitigation 4 a real fallback rather than a plausible-looking module,
and they can only be settled by a running server.

Three things are established here:

* **the loop closes.** A cell written to the file by the fallback appears in the running kernel's
  own view of the notebook, because `--watch` reloaded it.
* **the honest limit is real.** That reload does not run the cell. `run=True` genuinely cannot be
  honoured, and this asserts the shape of the gap rather than describing it.
* **the two bridges agree.** The same conditions raise the same error types on both, and the file
  each writes is the file the other would have written -- proven by making marimo itself re-save
  a notebook the fallback wrote and diffing the block.

The server is module-scoped and shared, as in `test_driver_live.py`. Nothing here calls
`list_cells(with_code=True)` on the live driver: it reads every cell's code and would disarm the
read-before-write guard that the parity test is measuring.
"""

from __future__ import annotations

import ast
import time
from typing import TYPE_CHECKING

import pytest

from conftest import serving, wait_for_notebook
from kedge.notebook.codegen import analyse_document, parse_notebook, render_notebook
from kedge.notebook.driver import (
    CellNameError,
    CellNotFoundError,
    CellSyntaxError,
    MultiplyDefinedError,
    StaleCellError,
)
from kedge.notebook.filedriver import FileNotebookDriver, NoKernelError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from conftest import LiveMarimo
    from kedge.notebook.driver import NotebookDriver

pytestmark = pytest.mark.contract

# Two cells kedge has never written and never read, so the read-before-write guard is armed on
# both -- one for each bridge to be caught by. Everything else this module needs, it creates.
WATCHED_NOTEBOOK = """import marimo

app = marimo.App()


@app.cell
def guard_for_live():
    guard_for_live_value = 1
    return


@app.cell
def guard_for_file():
    guard_for_file_value = 2
    return


if __name__ == "__main__":
    app.run()
"""

_RELOAD_TIMEOUT = 45.0
"""marimo's polling watcher ticks once a second, and a reload has to reach the kernel after that."""


@pytest.fixture(scope="module")
def watched_marimo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[LiveMarimo]:
    """A real ``marimo edit --watch`` on a notebook both bridges take turns editing."""
    with serving(tmp_path_factory.mktemp("kedge-contract-file"), WATCHED_NOTEBOOK) as server:
        yield server


@pytest.fixture
def file_driver(watched_marimo: LiveMarimo) -> FileNotebookDriver:
    """The fallback bridge, pointed at the file that server has open."""
    return FileNotebookDriver(watched_marimo.notebook_path)


async def wait_for_kernel(
    driver: NotebookDriver, name: str, *, timeout: float = _RELOAD_TIMEOUT
) -> tuple[str, ...]:
    """Return the kernel's cell names once ``name`` is among them.

    ``with_code=False`` throughout: reading every cell's code would satisfy the staleness guard
    for the whole notebook, which is the property the parity test measures.
    """
    deadline = time.monotonic() + timeout
    names: tuple[str, ...] = ()
    while time.monotonic() < deadline:
        names = tuple(cell.name for cell in await driver.list_cells(with_code=False))
        if name in names:
            return names
        time.sleep(0.5)
    message = (
        f"marimo never reloaded {name!r} from disk within {timeout:g}s; the kernel holds {names}. "
        f"Either --watch is off or the file the fallback wrote is one marimo refused to load."
    )
    raise AssertionError(message)


def block_for(source: str, name: str) -> str:
    """The whole ``@app.cell`` block for ``name``, decorator line included."""
    lines = source.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if not line.startswith(f"def {name}(") and not line.startswith(f"async def {name}("):
            continue
        start = index - 1
        end = index
        while end < len(lines) and (lines[end].strip() or end == index):
            end += 1
        return "".join(lines[start:end])
    message = f"no cell named {name!r} in:\n{source}"
    raise AssertionError(message)


# ── the loop closes ──────────────────────────────────────────────────────────────────────────


async def test_a_cell_written_to_the_file_reaches_the_running_kernel(
    watched_marimo: LiveMarimo, file_driver: FileNotebookDriver
) -> None:
    """The whole premise of the fallback: edit the file, and marimo picks the change up.

    kedge already launches ``marimo edit --watch`` (``lifecycle.build_marimo_argv``), so no
    special setup is needed -- the fallback writes the notebook and the running server notices.
    Asserted through the *kernel's* view of the notebook rather than by reading the file back,
    because the file is what kedge wrote and proves nothing on its own.
    """
    result = await file_driver.create_cell("watched_value = 41 + 1", name="watched_cell")

    assert result.cell is not None
    assert result.cell.name == "watched_cell"

    async with watched_marimo.driver() as live:
        names = await wait_for_kernel(live, "watched_cell")
        assert "watched_cell" in names
        assert (await live.get_cell("watched_cell")).code == "watched_value = 41 + 1"


async def test_the_reload_does_not_run_the_cell_the_fallback_wrote(
    watched_marimo: LiveMarimo, file_driver: FileNotebookDriver
) -> None:
    """The gap, asserted rather than merely documented.

    marimo's ``runtime.watcher_on_save`` defaults to ``"lazy"``: a reloaded cell is marked stale
    and waits for the user. So ``run=True`` cannot be honoured by a file write, the fallback
    reports ``ran=False``, and the binding does not exist in the kernel until somebody runs it.
    If a future marimo changes that default, this test says so.
    """
    result = await file_driver.create_cell("unrun_value = 7", name="unrun_cell", run=True)

    assert result.ran is False
    assert result.status is None
    assert "no kernel" in result.console

    async with watched_marimo.driver() as live:
        await wait_for_kernel(live, "unrun_cell")
        probe = await live.probe("unrun_value")

        assert probe.ok is False, (
            "marimo executed a cell the file bridge only wrote to disk; watcher_on_save is no "
            "longer 'lazy' and FileNotebookDriver.run_cell's docstring needs revisiting"
        )

        # ...and running it through the live bridge is what makes the value exist.
        ran = await live.run_cell("unrun_cell")
        assert ran.ran
        assert (await live.probe("unrun_value")).value_repr == "7"


async def test_the_fallback_reads_back_what_the_live_bridge_wrote(
    watched_marimo: LiveMarimo, file_driver: FileNotebookDriver
) -> None:
    """Both bridges have to describe the same notebook, or swapping one for the other loses state.

    The server persists a code-mode mutation to disk on a worker thread
    (docs/marimo-api.md 4.3), which is what makes the file an accurate record of the kernel and
    therefore what makes the fallback usable mid-conversation.
    """
    body = "handover_rows = [1, 2, 3]\nhandover_total = sum(handover_rows)"
    async with watched_marimo.driver() as live:
        await live.create_cell(body, name="handover_cell", hide_code=True)

    wait_for_notebook(watched_marimo.notebook_path, "def handover_cell(")

    cell = await file_driver.get_cell("handover_cell")

    assert cell.code == body
    assert cell.hide_code is True
    assert cell.name == "handover_cell"
    assert "handover_cell" in [item.name for item in await file_driver.list_cells(with_code=False)]


# ── the two serialisers agree ────────────────────────────────────────────────────────────────


async def test_marimo_re_saves_a_cell_the_fallback_wrote_without_changing_a_byte(
    watched_marimo: LiveMarimo, file_driver: FileNotebookDriver
) -> None:
    """The fidelity claim, settled by marimo rather than by inspection.

    marimo ships no public serialiser, so `kedge.notebook.codegen` writes the notebook format
    itself -- the ``def`` line's parameters, the trailing ``return`` tuple, the decorator
    keywords, the blank lines. If any of that were wrong, marimo's own save would rewrite it.
    So: the fallback writes a cell, marimo reloads it, a live mutation makes marimo re-save the
    whole document, and the block comes back identical.
    """
    body = "fidelity_source = 3\nfidelity_other = 'kept'"
    await file_driver.create_cell(body, name="fidelity_maker", hide_code=True, column=0)
    await file_driver.create_cell("fidelity_used = fidelity_source + 1", name="fidelity_user")
    written = block_for(watched_marimo.notebook_path.read_text(encoding="utf-8"), "fidelity_maker")

    async with watched_marimo.driver() as live:
        await wait_for_kernel(live, "fidelity_user")
        # Any code-mode mutation makes the server rewrite the entire notebook from its own model.
        await live.create_cell("fidelity_trigger = 1", name="fidelity_trigger")

    source = wait_for_notebook(watched_marimo.notebook_path, "def fidelity_trigger(")

    assert block_for(source, "fidelity_maker") == written
    assert "@app.cell(column=0, hide_code=True)" in written
    assert "return (fidelity_source,)" in written  # only the definition another cell uses
    assert "def fidelity_user(fidelity_source):" in source


async def test_a_notebook_marimo_wrote_survives_a_read_and_write_with_no_edit(
    watched_marimo: LiveMarimo,
) -> None:
    """Parsing and rendering a real marimo-written file must be the identity function.

    Every operation the fallback performs rewrites the whole file, so any drift between marimo's
    formatting and kedge's would churn the user's notebook on every turn.
    """
    source = watched_marimo.notebook_path.read_text(encoding="utf-8")
    document = parse_notebook(source)
    analyses, toplevel = analyse_document(document)

    assert render_notebook(document, analyses, toplevel) == source
    ast.parse(source)


# ── behavioural parity ───────────────────────────────────────────────────────────────────────


async def test_both_bridges_refuse_an_unknown_cell_with_the_same_error(
    watched_marimo: LiveMarimo, file_driver: FileNotebookDriver
) -> None:
    async with watched_marimo.driver() as live:
        with pytest.raises(CellNotFoundError):
            await live.get_cell("no_cell_is_called_this")
        with pytest.raises(CellNotFoundError):
            await file_driver.get_cell("no_cell_is_called_this")

        with pytest.raises(CellNotFoundError):
            await live.get_cell(9999)
        with pytest.raises(CellNotFoundError):
            await file_driver.get_cell(9999)


async def test_both_bridges_refuse_a_name_another_cell_already_uses(
    watched_marimo: LiveMarimo, file_driver: FileNotebookDriver
) -> None:
    async with watched_marimo.driver() as live:
        await live.create_cell("parity_taken_value = 1", name="parity_taken")
        wait_for_notebook(watched_marimo.notebook_path, "def parity_taken(")

        with pytest.raises(CellNameError, match="already used by cell"):
            await live.create_cell("parity_other_value = 2", name="parity_taken")
        with pytest.raises(CellNameError, match="already used by cell"):
            await file_driver.create_cell("parity_other_value = 2", name="parity_taken")


async def test_both_bridges_refuse_code_that_does_not_compile(
    watched_marimo: LiveMarimo, file_driver: FileNotebookDriver
) -> None:
    async with watched_marimo.driver() as live:
        with pytest.raises(CellSyntaxError):
            await live.create_cell("def (:", name="parity_uncompilable_live")
        with pytest.raises(CellSyntaxError):
            await file_driver.create_cell("def (:", name="parity_uncompilable_file")


async def test_both_bridges_report_a_multiply_defined_name_as_typed_conflicts(
    watched_marimo: LiveMarimo, file_driver: FileNotebookDriver
) -> None:
    """The live bridge parses this out of marimo's prose; the fallback computes it directly.

    The validation gate renders `DefinitionConflict` objects, so both have to produce them --
    naming the same variable, and pointing at the same owning cell by a name the reader knows.
    """
    async with watched_marimo.driver() as live:
        owner = await live.create_cell("parity_clash_value = 1", name="parity_clash_owner")
        assert owner.cell is not None
        wait_for_notebook(watched_marimo.notebook_path, "def parity_clash_owner(")

        with pytest.raises(MultiplyDefinedError) as from_live:
            await live.create_cell("parity_clash_value = 2", name="parity_clash_live")
        with pytest.raises(MultiplyDefinedError) as from_file:
            await file_driver.create_cell("parity_clash_value = 2", name="parity_clash_file")

    for caught in (from_live, from_file):
        assert caught.value.conflicts, f"the conflict list did not parse from: {caught.value}"
        conflict = caught.value.conflicts[0]
        assert conflict.name == "parity_clash_value"
        assert "parity_clash_owner" in conflict.defined_in
        assert "single-definition rule" in str(caught.value)


async def test_both_bridges_refuse_to_overwrite_a_cell_they_have_not_read(
    watched_marimo: LiveMarimo, file_driver: FileNotebookDriver
) -> None:
    """PLAN 6.2's "user edited a cell between turns" protection, on both bridges.

    marimo's guard lives on the kernel and is armed by touching a cell's code; the fallback's is
    armed by hashing a body when it is read. The two cells were seeded on disk and neither bridge
    has read either, which is the state a notebook is in when the user has been typing in it.
    Each bridge gets its own cell, because reading one to satisfy a guard disarms it.
    """
    async with watched_marimo.driver() as live:
        with pytest.raises(StaleCellError) as from_live:
            await live.edit_cell("guard_for_live", "guard_for_live_value = 101")
        with pytest.raises(StaleCellError) as from_file:
            await file_driver.edit_cell("guard_for_file", "guard_for_file_value = 202")

        for caught in (from_live, from_file):
            assert caught.value.retryable is True
            assert caught.value.cell_id is not None
            assert caught.value.stale_cells
            assert "re-read the cell" in str(caught.value)

        # Both are satisfied the same two ways: read first, or force the read inside the call.
        await live.get_cell("guard_for_live")
        assert (await live.edit_cell("guard_for_live", "guard_for_live_value = 101")).ok

        forced = await file_driver.edit_cell(
            "guard_for_file", "guard_for_file_value = 202", overwrite_unread=True
        )
        assert forced.ok
        assert forced.previous_code == "guard_for_file_value = 2"


async def test_the_fallback_refuses_everything_that_needs_a_kernel(
    file_driver: FileNotebookDriver,
) -> None:
    """The other half of parity: what the fallback cannot do, it refuses loudly and typed.

    Run beside a live server on purpose. The server exists, the notebook is open, the kernel is
    healthy -- and the fallback still cannot reach any of it, because it only has the file.
    """
    with pytest.raises(NoKernelError):
        await file_driver.run_cell("guard_for_file")
    with pytest.raises(NoKernelError):
        await file_driver.probe("1 + 1")
    with pytest.raises(NoKernelError):
        await file_driver.set_ui_value("anything", 1)
    with pytest.raises(NoKernelError):
        await file_driver.notify("Title", "Description")
