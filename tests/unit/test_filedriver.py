"""The file-writing fallback bridge, exercised against real notebooks on a real filesystem.

`kedge.notebook.filedriver` is PLAN 6.1 mitigation 4: the bridge kedge falls back to when
`marimo._code_mode` changes or disappears. Its value is entirely in being *boring* -- it has to
behave like `NotebookDriver` for everything a document edit can express, and refuse loudly for
everything else, and it has to do that without depending on anything marimo can withdraw.

So three things are asserted here and nowhere else:

* **the same typed errors for the same conditions.** The error classes are imported from
  `driver.py` rather than redeclared, and every rejection path is checked against them, because
  the agent loop's validation gate and retry logic are written against those types.
* **nothing private is reached.** `test_the_fallback_reaches_no_private_marimo_api` walks both
  modules for any `marimo._*` import or attribute. A fallback that depends on a private API is
  not a fallback.
* **the file is never corrupted.** Hostile cell bodies round-trip through the document, and the
  replacement file is proven complete before it is moved into place -- `marimo edit --watch` is
  reading this file on a timer, and a half-written notebook is a broken notebook.

`tests/contract/test_filedriver_live.py` is the other half: it drives a real
`marimo edit --watch` and asserts marimo picks the changes up.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest
from test_driver_codegen import HOSTILE_STRINGS

from kedge.notebook import codegen
from kedge.notebook.driver import (
    CellNameError,
    CellNotFoundError,
    CellSyntaxError,
    CycleDetectedError,
    MultiplyDefinedError,
    StaleCellError,
    py_literal,
)
from kedge.notebook.filedriver import (
    NOT_RUN_NOTE,
    FileNotebookDriver,
    NoKernelError,
)
from kedge.notebook.model import UNNAMED_CELL, NotebookBridge

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"

# The shape marimo 0.23.15 writes, down to the blank lines: two seeded cells, one of them
# unnamed so the "_" placeholder is exercised, and neither of them ever read by kedge -- which is
# the state the read-before-write guard exists to protect.
NOTEBOOK = """import marimo

__generated_with = "0.23.15"
app = marimo.App()


@app.cell
def seeded():
    seeded_value = 1
    return


@app.cell
def _():
    unnamed_value = 2
    return


if __name__ == "__main__":
    app.run()
"""


@pytest.fixture
def notebook(tmp_path: Path) -> Path:
    """A notebook file on disk, in the exact shape marimo saves one."""
    path = tmp_path / "notebook.py"
    path.write_text(NOTEBOOK, encoding="utf-8")
    return path


@pytest.fixture
def driver(notebook: Path) -> FileNotebookDriver:
    """A driver bound to that notebook."""
    return FileNotebookDriver(notebook)


def names_of(cells) -> list[str]:
    """The cell names, in notebook order."""
    return [cell.name for cell in cells]


# ── the interface, and the promise about what it depends on ──────────────────────────────────


def test_the_file_driver_satisfies_the_notebook_bridge_protocol(notebook: Path) -> None:
    """It has to be swappable behind the same interface, or it is not a fallback (PLAN 6.1)."""
    assert isinstance(FileNotebookDriver(notebook), NotebookBridge)


def _private_marimo_references(path: Path) -> list[str]:
    """Any import of, or attribute access into, a private marimo module in this file."""
    found: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names if alias.name.startswith("marimo._")]
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").startswith("marimo._"):
                found.append(node.module or "")
        elif (
            isinstance(node, ast.Attribute)
            and node.attr.startswith("_")
            and isinstance(node.value, ast.Name)
            and node.value.id == "marimo"
        ):
            found.append(f"marimo.{node.attr}")
    return found


@pytest.mark.parametrize("module", ["codegen.py", "filedriver.py"])
def test_the_fallback_reaches_no_private_marimo_api(module: str) -> None:
    """The whole point: it survives marimo withdrawing the private surface driver.py uses.

    marimo 0.23.15 ships no public notebook serialiser -- `parse_notebook`,
    `generate_filecontents` and `MarimoConvert` all live under `marimo._ast` / `marimo._convert`.
    Reaching for them would buy a fallback with the same failure mode as the thing it is a
    fallback for, so these two modules use the standard library plus `marimo.App` and
    `marimo.Cell`, both of which are in marimo's top-level `__all__`.
    """
    path = SRC_ROOT / "kedge" / "notebook" / module

    assert _private_marimo_references(path) == []


def test_the_analysis_program_only_names_the_public_marimo_surface() -> None:
    """The source `analyse_cells` execs must not reach into marimo either."""
    source = codegen._ANALYSIS_PREAMBLE

    assert "marimo._" not in source
    assert "import marimo" in source
    assert "marimo.App()" in source


# ── the document: what is read, and what is carried across untouched ─────────────────────────


def test_a_marimo_notebook_round_trips_through_parse_and_render_unchanged() -> None:
    """Reading and writing back with no edit must produce the identical file, byte for byte."""
    document = codegen.parse_notebook(NOTEBOOK)
    analyses, toplevel = codegen.analyse_document(document)

    assert codegen.render_notebook(document, analyses, toplevel) == NOTEBOOK


def test_the_cell_bodies_read_back_are_what_the_kernel_would_report() -> None:
    """A body is the decorated function's statements, dedented, with the return dropped."""
    document = codegen.parse_notebook(NOTEBOOK)

    assert [cell.name for cell in document.cells] == ["seeded", UNNAMED_CELL]
    assert [cell.code for cell in document.cells] == ["seeded_value = 1", "unnamed_value = 2"]


def test_everything_outside_a_cell_block_survives_a_write(tmp_path: Path) -> None:
    """kedge owns the `@app.cell` blocks and nothing else. Everything else is carried, not parsed.

    A real notebook holds constructs this module deliberately does not model -- inline script
    metadata, app configuration, a setup block, a promoted top-level function, comments between
    cells. Losing any of them silently would be worse than refusing to write at all.
    """
    source = """# /// script
# dependencies = ["polars"]
# ///

import marimo

__generated_with = "0.23.15"
app = marimo.App(width="medium", app_title="Monthly RWA")

with app.setup:
    SHARED_CONSTANT = 7


@app.function
def helper(value):
    return value * 2


# a comment the user wrote between two cells
@app.cell
def first():
    first_value = SHARED_CONSTANT
    return


if __name__ == "__main__":
    app.run()
"""
    path = tmp_path / "rich.py"
    path.write_text(source, encoding="utf-8")

    document = codegen.parse_notebook(source)
    analyses, toplevel = codegen.analyse_document(document)
    rendered = codegen.render_notebook(document, analyses, toplevel)

    assert rendered == source
    assert names_of(document.cells) == ["first"]  # only @app.cell blocks are cells
    assert "helper" in toplevel  # but a top-level def still owns its name
    assert "SHARED_CONSTANT" in toplevel  # and so does a setup block's definition


def test_cell_configuration_round_trips_through_the_decorator(tmp_path: Path) -> None:
    """`hide_code`, `disabled` and `column` live in the decorator and must survive a rewrite."""
    path = tmp_path / "configured.py"
    path.write_text(
        NOTEBOOK.replace("@app.cell\ndef seeded", "@app.cell(column=1, disabled=True)\ndef seeded"),
        encoding="utf-8",
    )
    document = codegen.parse_notebook(path.read_text(encoding="utf-8"))
    analyses, toplevel = codegen.analyse_document(document)

    assert document.cells[0].column == 1
    assert document.cells[0].disabled is True
    assert document.cells[0].hide_code is False
    assert "@app.cell(column=1, disabled=True)" in codegen.render_notebook(
        document, analyses, toplevel
    )


def test_a_file_that_is_not_a_marimo_notebook_is_refused(tmp_path: Path) -> None:
    """No `marimo.App(...)` means this is somebody else's Python file. Do not rewrite it."""
    path = tmp_path / "plain.py"
    path.write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(codegen.NotebookFormatError, match="does not build a marimo app"):
        codegen.read_notebook(path)


def test_a_file_that_does_not_parse_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "broken.py"
    path.write_text("import marimo\napp = marimo.App()\ndef (:\n", encoding="utf-8")

    with pytest.raises(codegen.NotebookFormatError, match="does not parse"):
        codegen.read_notebook(path)


def test_a_missing_notebook_is_refused_with_the_path_in_the_message(tmp_path: Path) -> None:
    with pytest.raises(codegen.NotebookFormatError, match="cannot read the notebook"):
        codegen.read_notebook(tmp_path / "absent.py")


# ── reads ────────────────────────────────────────────────────────────────────────────────────


async def test_list_cells_reports_names_positions_and_order(driver: FileNotebookDriver) -> None:
    cells = await driver.list_cells()

    assert names_of(cells) == ["seeded", UNNAMED_CELL]
    assert [cell.id for cell in cells] == ["0", "1"]
    assert cells[0].is_named
    assert cells[1].is_named is False
    assert cells[0].code == "seeded_value = 1"
    # A file cannot say whether a cell ran or what it produced, so it does not pretend to.
    assert cells[0].status is None
    assert cells[0].errors == ()


async def test_list_cells_without_code_omits_the_source(driver: FileNotebookDriver) -> None:
    cells = await driver.list_cells(with_code=False)

    assert all(cell.code is None for cell in cells)
    assert names_of(cells) == ["seeded", UNNAMED_CELL]


async def test_a_cell_can_be_addressed_by_name_by_id_and_by_index(
    driver: FileNotebookDriver,
) -> None:
    """`ctx.cells[key]` takes all three, so the fallback takes all three."""
    by_name = await driver.get_cell("seeded")
    by_id = await driver.get_cell("0")
    by_index = await driver.get_cell(0)
    from_the_end = await driver.get_cell(-1)

    assert by_name.id == by_id.id == by_index.id == "0"
    assert from_the_end.name == UNNAMED_CELL


@pytest.mark.parametrize("target", ["no_cell_is_called_this", 9999, -9999, "42"])
async def test_every_way_of_failing_to_address_a_cell_raises_cell_not_found(
    driver: FileNotebookDriver, target: str | int
) -> None:
    with pytest.raises(CellNotFoundError):
        await driver.get_cell(target)


async def test_read_graph_describes_the_real_notebook(driver: FileNotebookDriver) -> None:
    """Definitions and references come from marimo's own `Cell`, so the graph is marimo's."""
    await driver.create_cell("graph_root_value = 6", name="graph_root")
    await driver.create_cell("graph_leaf_value = graph_root_value * 7", name="graph_leaf")

    graph = await driver.read_graph()
    root = graph.node("graph_root")
    leaf = graph.node("graph_leaf")

    assert root is not None
    assert leaf is not None
    assert root.defs == ("graph_root_value",)
    assert leaf.refs == ("graph_root_value",)
    assert root.id in leaf.ancestors
    assert leaf.id in root.descendants
    assert graph.cycles == ()
    assert graph.defining("graph_root_value") == (root,)
    assert "graph_root_value" not in graph.multiply_defined
    assert root.status is None  # no kernel, no status


async def test_read_graph_reports_a_notebook_that_is_already_broken(
    driver: FileNotebookDriver, notebook: Path
) -> None:
    """Reading is diagnostic, not a gate: it describes conflicts rather than refusing to look."""
    notebook.write_text(NOTEBOOK.replace("unnamed_value = 2", "seeded_value = 2"), encoding="utf-8")

    graph = await driver.read_graph()

    assert graph.multiply_defined == ("seeded_value",)
    assert len(graph.defining("seeded_value")) == 2


# ── creating ─────────────────────────────────────────────────────────────────────────────────


async def test_create_cell_appends_a_block_marimo_would_have_written(
    driver: FileNotebookDriver, notebook: Path
) -> None:
    result = await driver.create_cell("made_value = seeded_value + 1", name="maker")

    assert result.operation == "create_cell"
    assert result.cell is not None
    assert result.cell.name == "maker"
    assert result.cell.target == "maker"
    assert result.ok

    source = notebook.read_text(encoding="utf-8")
    ast.parse(source)
    assert (
        "@app.cell\ndef maker(seeded_value):\n    made_value = seeded_value + 1\n    return\n"
        in (source)
    )
    # The producing cell's return tuple is regenerated too, exactly as marimo does on save.
    assert "def seeded():\n    seeded_value = 1\n    return (seeded_value,)" in source
    assert source.endswith('if __name__ == "__main__":\n    app.run()\n')


async def test_create_cell_never_claims_to_have_run_anything(driver: FileNotebookDriver) -> None:
    """`run=True` is the default and cannot be honoured, so the result says so rather than lies."""
    result = await driver.create_cell("ran_value = 1", name="runner", run=True)

    assert result.ran is False
    assert result.status is None
    assert NOT_RUN_NOTE in result.console

    quiet = await driver.create_cell("quiet_value = 1", name="quiet", run=False)
    assert quiet.console == ""


async def test_create_cell_places_a_cell_before_or_after_an_anchor(
    driver: FileNotebookDriver,
) -> None:
    await driver.create_cell("head_value = 1", name="head", before="seeded")
    await driver.create_cell("middle_value = 1", name="middle", after="seeded")

    assert names_of(await driver.list_cells(with_code=False)) == [
        "head",
        "seeded",
        "middle",
        UNNAMED_CELL,
    ]


async def test_create_cell_refuses_both_anchors_at_once(driver: FileNotebookDriver) -> None:
    with pytest.raises(CellNameError, match="at most one"):
        await driver.create_cell("x = 1", name="anchored", before="seeded", after="seeded")


@pytest.mark.parametrize(
    "name", ["", "not an identifier", "class", "__dunder__", "_private", "a" * 65]
)
async def test_create_cell_validates_the_name(driver: FileNotebookDriver, name: str) -> None:
    """The same validator the live driver uses, so a bad name fails identically on both."""
    with pytest.raises(CellNameError):
        await driver.create_cell("x = 1", name=name)


async def test_a_name_another_cell_already_uses_is_refused(
    driver: FileNotebookDriver, notebook: Path
) -> None:
    before = notebook.read_text(encoding="utf-8")

    with pytest.raises(CellNameError, match="already used by cell"):
        await driver.create_cell("other_value = 1", name="seeded")

    assert notebook.read_text(encoding="utf-8") == before


async def test_code_that_does_not_compile_is_refused_before_anything_changes(
    driver: FileNotebookDriver, notebook: Path
) -> None:
    with pytest.raises(CellSyntaxError, match="did not compile"):
        await driver.create_cell("def (:", name="uncompilable_cell")

    assert notebook.read_text(encoding="utf-8") == NOTEBOOK


async def test_a_multiply_defined_name_arrives_as_typed_conflicts(
    driver: FileNotebookDriver, notebook: Path
) -> None:
    """The live bridge parses this out of marimo's prose; here it is computed directly.

    Either way the caller receives `MultiplyDefinedError` carrying `DefinitionConflict` objects,
    which is what the validation gate renders. Nothing was written.
    """
    with pytest.raises(MultiplyDefinedError) as caught:
        await driver.create_cell("seeded_value = 2", name="intruder")

    error = caught.value
    assert error.conflicts
    conflict = error.conflicts[0]
    assert conflict.name == "seeded_value"
    assert "seeded" in conflict.defined_in
    assert "0" in conflict.defined_in
    assert "single-definition rule" in str(error)
    assert notebook.read_text(encoding="utf-8") == NOTEBOOK


async def test_a_cycle_is_refused_and_nothing_is_written(
    driver: FileNotebookDriver, notebook: Path
) -> None:
    await driver.create_cell("loop_a = loop_b + 1", name="loop_first")
    before = notebook.read_text(encoding="utf-8")

    with pytest.raises(CycleDetectedError, match="cycle"):
        await driver.create_cell("loop_b = loop_a + 1", name="loop_second")

    assert notebook.read_text(encoding="utf-8") == before


# ── editing, deleting, moving ────────────────────────────────────────────────────────────────


async def test_edit_cell_replaces_the_body(driver: FileNotebookDriver, notebook: Path) -> None:
    await driver.get_cell("seeded")

    result = await driver.edit_cell("seeded", "seeded_value = 99")

    assert result.operation == "edit_cell"
    assert result.ran is False
    assert result.previous_code is None  # only overwrite_unread reports it
    assert (await driver.get_cell("seeded")).code == "seeded_value = 99"
    assert "seeded_value = 99" in notebook.read_text(encoding="utf-8")


async def test_edit_cell_changes_configuration_without_touching_the_code(
    driver: FileNotebookDriver,
) -> None:
    """A config-only edit passes `code=None`, so it never trips the read-before-write guard."""
    await driver.edit_cell("seeded", hide_code=True, run=False)
    hidden = await driver.get_cell("seeded")

    assert hidden.hide_code is True
    assert hidden.disabled is False
    assert hidden.code == "seeded_value = 1"

    await driver.edit_cell("seeded", disabled=True, run=False)
    disabled = await driver.get_cell("seeded")

    assert disabled.disabled is True
    assert disabled.hide_code is True  # the earlier setting is preserved, not reset


async def test_edit_cell_renames_and_the_cell_stays_reachable(driver: FileNotebookDriver) -> None:
    await driver.create_cell("renamed_total = 5", name="rename_before")

    renamed = await driver.edit_cell("rename_before", name="rename_after")

    assert renamed.cell is not None
    assert renamed.cell.name == "rename_after"
    assert (await driver.get_cell("rename_after")).code == "renamed_total = 5"
    names = names_of(await driver.list_cells(with_code=False))
    assert "rename_after" in names
    assert "rename_before" not in names


async def test_edit_cell_refuses_a_rename_onto_a_taken_name(driver: FileNotebookDriver) -> None:
    await driver.create_cell("collide_value = 1", name="collider")

    with pytest.raises(CellNameError, match="already used by cell"):
        await driver.edit_cell("collider", name="seeded")


async def test_edit_cell_needs_something_to_change(driver: FileNotebookDriver) -> None:
    with pytest.raises(CellNameError, match="needs something to change"):
        await driver.edit_cell("seeded")


async def test_deleting_a_cell_reports_what_depended_on_it(
    driver: FileNotebookDriver, notebook: Path
) -> None:
    """PLAN 6.2: deleting a definition other cells use must be visible, not discovered later."""
    await driver.create_cell("dependent_value = seeded_value + 1", name="dependent")

    result = await driver.delete_cell("seeded")

    assert result.cell is not None
    assert result.cell.name == "seeded"
    assert result.descendants
    assert "seeded" not in names_of(await driver.list_cells(with_code=False))
    ast.parse(notebook.read_text(encoding="utf-8"))


async def test_deleting_the_only_cell_leaves_a_notebook_that_still_parses(
    tmp_path: Path,
) -> None:
    path = tmp_path / "single.py"
    path.write_text(
        "import marimo\n\napp = marimo.App()\n\n\n@app.cell\ndef only():\n    only_value = 1\n"
        '    return\n\n\nif __name__ == "__main__":\n    app.run()\n',
        encoding="utf-8",
    )
    driver = FileNotebookDriver(path)

    await driver.delete_cell("only")

    source = path.read_text(encoding="utf-8")
    ast.parse(source)
    assert await driver.list_cells() == ()
    assert 'if __name__ == "__main__":' in source

    # ...and a cell can be put back into the emptied notebook.
    await driver.create_cell("restored_value = 1", name="restored")
    ast.parse(path.read_text(encoding="utf-8"))
    assert names_of(await driver.list_cells(with_code=False)) == ["restored"]


async def test_moving_a_cell_reorders_the_file(driver: FileNotebookDriver, notebook: Path) -> None:
    await driver.create_cell("third_value = 3", name="third")

    moved = await driver.move_cell("third", before="seeded")

    assert moved.cell is not None
    assert moved.cell.name == "third"
    assert names_of(await driver.list_cells(with_code=False)) == [
        "third",
        "seeded",
        UNNAMED_CELL,
    ]

    await driver.move_cell("third", after=UNNAMED_CELL)
    assert names_of(await driver.list_cells(with_code=False))[-1] == "third"
    ast.parse(notebook.read_text(encoding="utf-8"))


@pytest.mark.parametrize("anchors", [{}, {"before": "seeded", "after": "seeded"}])
async def test_move_cell_needs_exactly_one_anchor(
    driver: FileNotebookDriver, anchors: dict[str, str]
) -> None:
    with pytest.raises(CellNameError, match="exactly one"):
        await driver.move_cell(UNNAMED_CELL, **anchors)


async def test_a_cell_cannot_be_moved_relative_to_itself(driver: FileNotebookDriver) -> None:
    with pytest.raises(CellNameError, match="relative to itself"):
        await driver.move_cell("seeded", before="seeded")


# ── the read-before-write guard ──────────────────────────────────────────────────────────────


async def test_editing_a_cell_kedge_has_not_read_is_refused(driver: FileNotebookDriver) -> None:
    """The seeded cells were loaded from disk and never read: exactly the guarded state."""
    with pytest.raises(StaleCellError) as caught:
        await driver.edit_cell("seeded", "seeded_value = 101")

    error = caught.value
    assert error.retryable is True
    assert error.cell_id == "0"
    assert "0" in error.stale_cells
    assert "re-read the cell" in str(error)


async def test_reading_a_cell_first_satisfies_the_guard(
    driver: FileNotebookDriver, notebook: Path
) -> None:
    """A read recorded in one call still counts in a later one, as it does on the kernel."""
    await driver.get_cell("seeded")

    edited = await driver.edit_cell("seeded", "seeded_value = 101")

    assert edited.previous_code is None
    assert "seeded_value = 101" in notebook.read_text(encoding="utf-8")


async def test_overwrite_unread_reads_the_cell_inside_the_call_and_returns_it(
    driver: FileNotebookDriver,
) -> None:
    """The honest override: the user's version comes back rather than being silently lost."""
    forced = await driver.edit_cell("seeded", "seeded_value = 202", overwrite_unread=True)

    assert forced.ok
    assert forced.previous_code == "seeded_value = 1"
    assert (await driver.get_cell("seeded")).code == "seeded_value = 202"


async def test_the_guard_never_fires_for_a_cell_kedge_itself_wrote(
    driver: FileNotebookDriver,
) -> None:
    """Matching marimo, which records a read for every cell the agent wrote in that batch."""
    await driver.create_cell("own_value = 1", name="own")

    first = await driver.edit_cell("own", "own_value = 2")
    second = await driver.edit_cell("own", "own_value = 3")

    assert first.ok
    assert second.ok


async def test_list_cells_with_code_disarms_the_guard_for_every_cell(
    driver: FileNotebookDriver,
) -> None:
    """Reading every body is a read of every body -- the same trade-off the live driver has."""
    await driver.list_cells(with_code=True)

    assert (await driver.edit_cell("seeded", "seeded_value = 303")).ok


async def test_list_cells_without_code_leaves_the_guard_armed(
    driver: FileNotebookDriver,
) -> None:
    await driver.list_cells(with_code=False)

    with pytest.raises(StaleCellError):
        await driver.edit_cell("seeded", "seeded_value = 404")


async def test_read_graph_does_not_count_as_reading_any_cell(driver: FileNotebookDriver) -> None:
    """Reading the graph never reads a body on the live bridge either. Keep it cheap and safe."""
    await driver.read_graph()

    with pytest.raises(StaleCellError):
        await driver.edit_cell("seeded", "seeded_value = 505")


async def test_a_change_made_on_disk_between_turns_re_arms_the_guard(
    driver: FileNotebookDriver, notebook: Path
) -> None:
    """The whole reason the guard exists: the user typed in the editor while kedge was thinking."""
    await driver.get_cell("seeded")
    notebook.write_text(
        NOTEBOOK.replace("seeded_value = 1", "seeded_value = 1  # the user's own change"),
        encoding="utf-8",
    )

    with pytest.raises(StaleCellError):
        await driver.edit_cell("seeded", "seeded_value = 606")

    assert "the user's own change" in notebook.read_text(encoding="utf-8")


async def test_an_empty_cell_body_is_exempt_from_the_guard(tmp_path: Path) -> None:
    """marimo skips the check when the target is empty, because there is nothing to lose."""
    path = tmp_path / "empty.py"
    path.write_text(
        "import marimo\n\napp = marimo.App()\n\n\n@app.cell\ndef blank():\n    return\n\n\n"
        'if __name__ == "__main__":\n    app.run()\n',
        encoding="utf-8",
    )
    driver = FileNotebookDriver(path)

    assert (await driver.edit_cell("blank", "blank_value = 1")).ok


# ── what a file simply cannot do ─────────────────────────────────────────────────────────────


async def test_run_cell_refuses_rather_than_pretending_a_watch_reload_is_execution(
    driver: FileNotebookDriver,
) -> None:
    with pytest.raises(NoKernelError, match="no kernel"):
        await driver.run_cell("seeded")


async def test_probe_refuses_because_a_file_does_not_hold_values(
    driver: FileNotebookDriver,
) -> None:
    with pytest.raises(NoKernelError, match="probe"):
        await driver.probe("seeded_value")


async def test_set_ui_value_refuses_because_the_element_lives_in_the_kernel(
    driver: FileNotebookDriver,
) -> None:
    with pytest.raises(NoKernelError, match="ui"):
        await driver.set_ui_value("ui_slider", 7)


async def test_notify_refuses_because_there_is_no_session_to_broadcast_on(
    driver: FileNotebookDriver,
) -> None:
    with pytest.raises(NoKernelError, match="notification"):
        await driver.notify("Title", "Description")


def test_the_no_kernel_error_is_not_retryable() -> None:
    """Retrying will not conjure a kernel, and the agent loop keys off this attribute."""
    assert NoKernelError.retryable is False
    assert StaleCellError.retryable is True


async def test_a_refused_operation_changes_nothing_on_disk(
    driver: FileNotebookDriver, notebook: Path
) -> None:
    for call in (
        driver.run_cell("seeded"),
        driver.probe("1 + 1"),
        driver.set_ui_value("slider", 1),
        driver.notify("a", "b"),
    ):
        with pytest.raises(NoKernelError):
            await call

    assert notebook.read_text(encoding="utf-8") == NOTEBOOK


# ── packages: a declaration, not an installation ─────────────────────────────────────────────


async def test_add_packages_records_a_pep_723_dependency_and_says_it_installed_nothing(
    driver: FileNotebookDriver, notebook: Path
) -> None:
    result = await driver.add_packages("polars==1.43.0")

    source = notebook.read_text(encoding="utf-8")
    ast.parse(source)
    assert source.startswith("# /// script\n")
    assert '"polars==1.43.0"' in source
    assert result.added == ("polars==1.43.0",)
    assert result.installed_before == ()  # no kernel means no environment to report
    assert "nothing was installed" in result.console
    assert result.needs_restart_hint
    # The notebook itself is untouched below the metadata block.
    assert "def seeded():" in source


async def test_add_packages_replaces_a_declaration_of_the_same_distribution(
    driver: FileNotebookDriver, notebook: Path
) -> None:
    await driver.add_packages("polars==1.0.0", "httpx")
    await driver.add_packages("polars==1.43.0")

    metadata = _script_metadata(notebook)

    assert metadata["dependencies"] == ["httpx", "polars==1.43.0"]


async def test_add_packages_removes_by_distribution_name(
    driver: FileNotebookDriver, notebook: Path
) -> None:
    await driver.add_packages("polars==1.43.0", "httpx>=0.27")

    result = await driver.add_packages(remove=("Polars",))

    assert result.removed == ("polars==1.43.0",)
    assert _script_metadata(notebook)["dependencies"] == ["httpx>=0.27"]


async def test_add_packages_preserves_a_block_the_user_already_wrote(tmp_path: Path) -> None:
    path = tmp_path / "metadata.py"
    path.write_text(
        '# /// script\n# requires-python = ">=3.12"\n# dependencies = ["openpyxl"]\n# ///\n\n'
        + NOTEBOOK,
        encoding="utf-8",
    )
    driver = FileNotebookDriver(path)

    await driver.add_packages("polars")

    metadata = _script_metadata(path)
    assert metadata["requires-python"] == ">=3.12"
    assert metadata["dependencies"] == ["openpyxl", "polars"]
    assert path.read_text(encoding="utf-8").count("# /// script") == 1


async def test_add_packages_needs_something_to_do(driver: FileNotebookDriver) -> None:
    with pytest.raises(CellNameError, match="at least one package"):
        await driver.add_packages()


def _script_metadata(path: Path) -> dict:
    """Read the notebook's PEP 723 block back with the standard library, not with kedge's parser."""
    lines: list[str] = []
    collecting = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line == "# /// script":
            collecting = True
            continue
        if collecting and line == "# ///":
            break
        if collecting:
            lines.append(line[2:] if line.startswith("# ") else line[1:])
    return tomllib.loads("\n".join(lines))


# ── the file must never be corrupted ─────────────────────────────────────────────────────────


async def test_the_replacement_file_is_complete_before_it_is_moved_into_place(
    driver: FileNotebookDriver, notebook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`marimo edit --watch` reads this file on a timer. A partial write is a broken notebook.

    The content goes to a temporary file in the same directory and is moved over the target in
    one step, so a reader either sees the old notebook or the new one and never a half of each.
    """
    seen: list[tuple[str, str]] = []
    original = Path.replace

    def recording(self: Path, target) -> Path:
        seen.append((self.name, self.read_text(encoding="utf-8")))
        return original(self, target)

    monkeypatch.setattr(Path, "replace", recording)

    await driver.create_cell("atomic_value = 1", name="atomic")

    assert len(seen) == 1
    name, staged = seen[0]
    assert name != notebook.name  # written elsewhere, then moved over
    assert staged == notebook.read_text(encoding="utf-8")
    assert "atomic_value = 1" in staged
    assert list(notebook.parent.glob("*.kedge-tmp")) == []


async def test_a_write_that_cannot_replace_the_file_leaves_it_untouched(
    driver: FileNotebookDriver, notebook: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows refuses to replace a file another process holds open; say so and change nothing."""

    def refuse(self: Path, target) -> Path:
        raise PermissionError("the file is open in another process")

    monkeypatch.setattr(Path, "replace", refuse)
    monkeypatch.setattr(codegen, "_WRITE_RETRY_DELAY", 0.0)

    with pytest.raises(codegen.NotebookFormatError, match="holding it open"):
        await driver.create_cell("doomed_value = 1", name="doomed")

    assert notebook.read_text(encoding="utf-8") == NOTEBOOK
    assert list(notebook.parent.glob("*.kedge-tmp")) == []


async def test_a_temporary_file_is_never_left_behind_on_success(
    driver: FileNotebookDriver, notebook: Path
) -> None:
    await driver.create_cell("tidy_value = 1", name="tidy")

    assert sorted(item.name for item in notebook.parent.iterdir()) == [notebook.name]


# ── hostile cell bodies ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", HOSTILE_STRINGS)
async def test_a_hostile_value_round_trips_through_the_notebook_file(
    driver: FileNotebookDriver, value: str, request: pytest.FixtureRequest, notebook: Path
) -> None:
    """Embedding is the likeliest place in kedge for a silent, catastrophic bug.

    The same table `tests/unit/test_driver_codegen.py` uses against the live bridge's generated
    source, run against the document instead: quotes, backslashes, triple-quotes, control
    characters, astral emoji, the sentinel impostor. The body has to come back byte for byte and
    the file it went through has to still be a notebook.
    """
    name = f"hostile_{request.node.callspec.id.replace('-', '_')}"
    body = f"{name}_value = {py_literal(value)}"

    await driver.create_cell(body, name=name)

    source = notebook.read_text(encoding="utf-8")
    ast.parse(source)
    assert (await driver.get_cell(name)).code == body
    assert body in source


async def test_a_multiline_string_at_column_zero_survives_the_round_trip(
    driver: FileNotebookDriver,
) -> None:
    """The dedent trap: a body is indented by exactly one level, so it must be un-indented by one.

    Removing the *common* prefix instead would leave a body whose triple-quoted interior sits at
    column zero still indented, and the next write would indent it again -- corruption that
    compiles and produces the wrong value, which is the worst kind.
    """
    body = 'block = """\nno indent here\n    and some here\n"""\nlength = len(block)'

    await driver.create_cell(body, name="multiline")

    assert (await driver.get_cell("multiline")).code == body
    # A second write must not shift it again.
    await driver.edit_cell("multiline", body, overwrite_unread=True)
    assert (await driver.get_cell("multiline")).code == body


async def test_the_notebook_survives_every_hostile_value_at_once(
    driver: FileNotebookDriver, notebook: Path
) -> None:
    values = [case.values[0] for case in HOSTILE_STRINGS]
    body = "nasty_values = " + py_literal(values)

    await driver.create_cell(body, name="nasty_cell")

    source = notebook.read_text(encoding="utf-8")
    ast.parse(source)
    assert body in source
    assert (await driver.get_cell("nasty_cell")).code == body


async def test_a_cell_body_containing_a_comment_and_a_trailing_comment_is_preserved(
    driver: FileNotebookDriver,
) -> None:
    """Comments are not in the AST, so an extractor keyed on statements would silently drop them."""
    body = "# a leading comment\ncommented_value = 1\n# a trailing comment"

    await driver.create_cell(body, name="commented")

    assert (await driver.get_cell("commented")).code == body


async def test_a_cell_that_awaits_at_its_top_level_is_written_as_an_async_cell(
    driver: FileNotebookDriver, notebook: Path
) -> None:
    """marimo allows top-level await in a cell; the file expresses it as `async def`."""
    await driver.create_cell("import asyncio\nawait asyncio.sleep(0)", name="waiter")

    source = notebook.read_text(encoding="utf-8")
    ast.parse(source)
    assert "async def waiter(" in source
    assert (await driver.get_cell("waiter")).code == "import asyncio\nawait asyncio.sleep(0)"


# ── the scaffolder's sync, against a real driver and a real file ─────────────────────────────
#
# `tests/unit/test_scaffold.py` proves what `sync_notebook` decides, against a dict. This proves
# it against a driver that really parses and rewrites a notebook file: the emission survives the
# round trip, and a second sync is a no-op down to the byte. That last one is the property the
# open sequence depends on -- every open runs this step, and only the first finds an empty
# notebook.


async def test_syncing_a_plan_twice_leaves_the_notebook_file_untouched(notebook: Path) -> None:
    from conftest import make_plan
    from kedge.notebook.scaffold import sync_notebook
    from kedge.plan.model import Approval, ApprovalState

    plan = make_plan().model_copy(update={"approval": Approval(state=ApprovalState.APPROVED)})

    first = await sync_notebook(plan, FileNotebookDriver(notebook))
    written = notebook.read_text(encoding="utf-8")
    second = await sync_notebook(plan, FileNotebookDriver(notebook))

    assert first.named("created")
    assert not second.named("created")
    assert not second.named("updated")
    assert set(second.named("unchanged")) == set(first.named("created"))
    assert notebook.read_text(encoding="utf-8") == written
    ast.parse(written)


async def test_a_synced_notebook_reads_its_cells_back_verbatim(notebook: Path) -> None:
    """A cell that does not survive the file round trip would report as diverged for ever."""
    from conftest import make_plan
    from kedge.notebook.scaffold import build_cells, sync_notebook
    from kedge.plan.model import Approval, ApprovalState

    plan = make_plan().model_copy(update={"approval": Approval(state=ApprovalState.APPROVED)})
    driver = FileNotebookDriver(notebook)

    await sync_notebook(plan, driver)

    read_back = {cell.name: cell.code for cell in await driver.list_cells(with_code=True)}
    assert all(read_back[cell.name] == cell.code for cell in build_cells(plan))


# ── odds and ends ────────────────────────────────────────────────────────────────────────────


def test_repr_names_the_file_it_edits(notebook: Path) -> None:
    assert repr(str(notebook)) in repr(FileNotebookDriver(notebook))


async def test_the_driver_is_usable_as_an_async_context_manager(notebook: Path) -> None:
    async with FileNotebookDriver(notebook) as driver:
        assert names_of(await driver.list_cells(with_code=False)) == ["seeded", UNNAMED_CELL]
