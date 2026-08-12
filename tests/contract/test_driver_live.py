"""Every :class:`~kedge.notebook.driver.NotebookDriver` method, against a real marimo kernel.

``marimo._code_mode`` is private and says so: *"No versioning guarantees. May change or be removed
without notice."* PLAN 6.1 lists five mitigations and this file is the third -- the one that makes
a marimo bump a decision rather than a gamble. ``tests/unit/test_driver_codegen.py`` proves the
programs kedge generates *say* the right thing; nothing there runs any of them. These tests submit
them to a live kernel and assert on what came back, so a marimo release that renames a parameter
or rewords a message fails here rather than in a user's conversation.

Read ``tests/contract/conftest.py`` for how the servers are spawned and torn down. What matters
when adding a test here:

* the notebook is shared for the module, so every test names its cells after itself, gives them
  variable names nobody else defines, and reads state back instead of assuming it;
* ``list_cells(with_code=True)`` reads every cell's code, which disarms marimo's read-before-write
  guard for the whole notebook. Nothing on the shared server calls it -- the guard test gets a
  server of its own, and everything else uses ``with_code=False`` plus ``get_cell`` for source;
* non-ASCII is written as escapes so this file stays ASCII, matching the hostile-string table in
  the unit tests.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

import pytest

from conftest import GUARD_FORCED_CODE, wait_for_notebook
from kedge.notebook.driver import (
    MARIMO_PIN,
    RESULT_BEGIN,
    RESULT_END,
    CellNameError,
    CellNotFoundError,
    CellSyntaxError,
    MultiplyDefinedError,
    StaleCellError,
    check_bridge,
    py_literal,
    verify_bridge,
)
from kedge.notebook.model import UNNAMED_CELL

if TYPE_CHECKING:
    from collections.abc import Sequence

    from conftest import LiveMarimo
    from kedge.notebook.driver import NotebookDriver
    from kedge.notebook.model import CellInfo

pytestmark = pytest.mark.contract


# ── helpers ──────────────────────────────────────────────────────────────────────────────────


def names_of(cells: Sequence[CellInfo]) -> list[str]:
    """The cell names, in notebook order."""
    return [cell.name for cell in cells]


def decorator_above(source: str, cell_name: str) -> str:
    """Return the decorator marimo wrote above ``def <cell_name>(`` in a saved notebook."""
    lines = source.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(f"def {cell_name}(") and index:
            return lines[index - 1].strip()
    message = f"no cell named {cell_name!r} in the notebook:\n{source}"
    raise AssertionError(message)


# ── the private surface ──────────────────────────────────────────────────────────────────────


def test_the_bridge_matches_the_pinned_marimo() -> None:
    """PLAN 6.1 mitigation 5: the startup assertion passes against the version kedge pins.

    This is what fails loudly on a bump. It needs no server -- ``check_bridge`` introspects the
    installed package -- but it belongs here, because a green ``check_bridge`` only means anything
    if the rest of this file passes too.
    """
    report = verify_bridge()

    assert report.ok
    assert report.problems == ()
    assert report.version == MARIMO_PIN
    assert report.version_matches_pin
    assert report.message() == f"the marimo bridge matches marimo {MARIMO_PIN}"
    assert check_bridge() is report  # cached, so a conversation pays for it once


CODE_MODE_METHODS = {
    "create_cell": ["after", "before", "code", "column", "disabled", "hide_code", "name"],
    "edit_cell": ["code", "column", "disabled", "hide_code", "name", "target"],
    "delete_cell": ["target"],
    "move_cell": ["after", "before", "target"],
    "run_cell": ["target"],
    "set_ui_value": ["element", "value"],
    "broadcast_raw_notification": ["notification"],
}
"""The ``_code_mode`` surface the driver's generated programs call, restated independently.

Deliberately not imported from ``driver._EXPECTED_CONTEXT_METHODS``: a contract test that reads
its expectations out of the thing under test cannot fail when that thing is wrong.
"""

_INTROSPECTION = """
import inspect
import marimo._code_mode as _cm

_surface = {
    "exports": sorted(_cm.__all__),
    "get_context": sorted(inspect.signature(_cm.get_context).parameters),
    "properties": sorted(
        name
        for name in ("cells", "globals", "graph", "packages")
        if isinstance(getattr(_cm.AsyncCodeModeContext, name, None), property)
    ),
    "methods": {
        name: sorted(
            parameter
            for parameter in inspect.signature(
                getattr(_cm.AsyncCodeModeContext, name)
            ).parameters
            if parameter != "self"
        )
        for name in __NAMES__
        if hasattr(_cm.AsyncCodeModeContext, name)
    },
}
_surface
"""


async def test_the_kernel_exposes_the_code_mode_surface_the_driver_writes_against(
    driver: NotebookDriver,
) -> None:
    """The same check as :func:`check_bridge`, made *inside the kernel* and over the wire.

    ``check_bridge`` introspects the marimo importable from kedge's own interpreter. The programs
    the driver generates run somewhere else -- in the notebook's kernel -- and it is that
    interpreter's ``_code_mode`` whose signatures have to match. Two processes, one pin; this
    asserts the second one.
    """
    program = _INTROSPECTION.replace("__NAMES__", repr(sorted(CODE_MODE_METHODS)))

    probe = await driver.probe(program)

    assert probe.ok, probe.error
    assert probe.value_repr is not None
    assert not probe.truncated
    surface = ast.literal_eval(probe.value_repr)

    assert surface["exports"] == [
        "AsyncCodeModeContext",
        "CellStatusType",
        "NotebookCell",
        "StaleCellError",
        "get_context",
    ]
    assert surface["get_context"] == ["skip_staleness_check", "skip_validation"]
    assert surface["properties"] == ["cells", "globals", "graph", "packages"]
    for name, parameters in CODE_MODE_METHODS.items():
        assert name in surface["methods"], f"AsyncCodeModeContext.{name}() is gone"
        missing = set(parameters) - set(surface["methods"][name])
        assert not missing, f"AsyncCodeModeContext.{name}() no longer accepts {sorted(missing)}"


async def test_the_driver_is_bound_to_the_session_the_lifecycle_established(
    driver: NotebookDriver, live_marimo: LiveMarimo
) -> None:
    """There is no auto-created session: the driver only works against one kedge asserted."""
    assert driver.session_id == live_marimo.session_id
    assert driver.session_id == live_marimo.workspace.session_id
    assert driver.base_url == live_marimo.base_url

    probe = await driver.probe("1 + 1")

    assert probe.ok
    assert probe.value_repr == "2"


# ── the premise: the user can read what the model wrote ──────────────────────────────────────


async def test_a_created_cell_reaches_the_file_on_disk_with_its_code_visible(
    driver: NotebookDriver, live_marimo: LiveMarimo
) -> None:
    """``hide_code=False`` is the whole "reviewable notebook" premise, so prove it on disk.

    marimo's own ``create_cell`` defaults to ``hide_code=True``; kedge inverts that on every call.
    Until now that was asserted only against generated source. Here the cell goes through the
    kernel, the server persists the document, and the decorator in the ``.py`` is read back --
    which is the file the user actually opens. A cell created with ``hide_code=True`` is created
    alongside it so the assertion is known to discriminate rather than merely to pass.
    """
    await driver.create_cell("visible_total = 3 + 4", name="visible_maker")
    await driver.create_cell("hidden_total = 5 + 6", name="hidden_maker", hide_code=True)

    source = wait_for_notebook(live_marimo.notebook_path, "def hidden_maker(")

    assert decorator_above(source, "visible_maker") == "@app.cell"
    assert decorator_above(source, "hidden_maker") == "@app.cell(hide_code=True)"
    assert "visible_total = 3 + 4" in source
    ast.parse(source)  # the file the user opens is still a notebook


async def test_a_named_cell_stays_addressable_by_its_name_across_calls(
    driver: NotebookDriver,
) -> None:
    """Cross-turn editing rests entirely on this: ids are opaque tokens, names are the handle.

    One name, addressed through five separate submissions -- create, read, edit, run, graph -- each
    entering a fresh ``_code_mode`` context inside the kernel.
    """
    created = await driver.create_cell("addressable_total = 1", name="addressable_cell")
    assert created.cell is not None
    assert created.cell.name == "addressable_cell"
    assert created.cell.target == "addressable_cell"

    fetched = await driver.get_cell("addressable_cell")
    assert fetched.id == created.cell.id
    assert fetched.code == "addressable_total = 1"
    assert fetched.is_named

    edited = await driver.edit_cell("addressable_cell", "addressable_total = 2")
    assert edited.cell is not None
    assert edited.cell.id == created.cell.id

    ran = await driver.run_cell("addressable_cell")
    assert ran.cell is not None
    assert ran.cell.id == created.cell.id
    assert ran.ran

    node = (await driver.read_graph()).node("addressable_cell")
    assert node is not None
    assert node.id == created.cell.id
    assert node.defs == ("addressable_total",)

    assert (await driver.probe("addressable_total")).value_repr == "2"


async def test_renaming_a_cell_keeps_it_reachable_under_the_new_name(
    driver: NotebookDriver,
) -> None:
    """A rename may mint a new cell id, so the driver reads the cell back by its new name."""
    created = await driver.create_cell("renamed_total = 5", name="rename_before")
    assert created.cell is not None

    renamed = await driver.edit_cell("rename_before", name="rename_after")

    assert renamed.cell is not None
    assert renamed.cell.name == "rename_after"
    assert (await driver.get_cell("rename_after")).code == "renamed_total = 5"

    names = names_of(await driver.list_cells(with_code=False))
    assert "rename_after" in names
    assert "rename_before" not in names


async def test_a_cell_can_also_be_addressed_by_index_including_from_the_end(
    driver: NotebookDriver,
) -> None:
    """``ctx.cells`` takes an index as well as an id or a name, and negative indexes count back.

    Indexes are the one address kedge should not persist -- they move whenever a cell is created
    or deleted -- but they are in the signature, so they are exercised.
    """
    cells = await driver.list_cells(with_code=False)

    assert (await driver.get_cell(0)).id == cells[0].id
    assert (await driver.get_cell(-1)).id == cells[-1].id


async def test_an_index_past_the_end_of_the_notebook_raises_cell_not_found(
    driver: NotebookDriver,
) -> None:
    """Every way of failing to address a cell should arrive as the same typed error."""
    with pytest.raises(CellNotFoundError):
        await driver.get_cell(9999)


async def test_edit_cell_changes_configuration_without_touching_the_code(
    driver: NotebookDriver,
) -> None:
    """A config-only edit passes ``code=None``, so it never trips the read-before-write guard."""
    await driver.create_cell("config_total = 1", name="config_cell")

    await driver.edit_cell("config_cell", hide_code=True, run=False)
    hidden = await driver.get_cell("config_cell")

    assert hidden.hide_code is True
    assert hidden.disabled is False
    assert hidden.code == "config_total = 1"

    await driver.edit_cell("config_cell", disabled=True, run=False)
    disabled = await driver.get_cell("config_cell")

    assert disabled.disabled is True
    assert disabled.hide_code is True  # the earlier setting is preserved, not reset


# ── mutations that actually compute ──────────────────────────────────────────────────────────


async def test_creating_a_cell_with_run_computes_its_value_in_the_kernel(
    driver: NotebookDriver,
) -> None:
    """create + run is one atomic flush, so the value exists by the time the call returns."""
    result = await driver.create_cell(
        "computed_rows = [1, 2, 3, 4]\ncomputed_total = sum(computed_rows) * 10",
        name="computed_cell",
    )

    assert result.ok
    assert result.ran
    assert result.status == "idle"
    assert result.errors == ()

    probe = await driver.probe("computed_total")

    assert probe.ok
    assert probe.value_repr == "100"
    assert probe.value_type == "int"


async def test_a_cell_created_without_running_stays_stale_until_run_cell(
    driver: NotebookDriver,
) -> None:
    """``run=False`` really does defer execution: the binding does not exist until ``run_cell``."""
    created = await driver.create_cell("deferred_total = 21 * 2", name="deferred_cell", run=False)

    assert created.ran is False
    assert created.status == "stale"
    assert (await driver.probe("deferred_total")).ok is False

    ran = await driver.run_cell("deferred_cell")

    assert ran.ran
    assert ran.status == "idle"
    assert (await driver.probe("deferred_total")).value_repr == "42"


async def test_a_cell_that_raises_comes_back_as_a_failed_mutation(
    driver: NotebookDriver,
) -> None:
    """A failing cell must surface as a failure, not as a silent success with a tidy result."""
    result = await driver.create_cell(
        "raise ValueError('kedge contract explosion')", name="exploding_cell"
    )

    assert result.cell is not None
    assert result.ran
    assert result.status == "exception"
    assert result.ok is False
    assert result.errors
    reported = " ".join(error.message for error in result.errors)
    assert "ValueError" in reported
    assert "kedge contract explosion" in reported

    fetched = await driver.get_cell("exploding_cell")
    assert fetched.failed
    assert fetched.status == "exception"


async def test_deleting_and_moving_cells_takes_effect_in_the_notebook(
    driver: NotebookDriver,
) -> None:
    """Structural edits, verified by reading the notebook's order and membership back."""
    await driver.create_cell("shuffle_first_value = 1", name="shuffle_first")
    await driver.create_cell("shuffle_second_value = 2", name="shuffle_second")
    await driver.create_cell("shuffle_third_value = shuffle_first_value + 1", name="shuffle_third")

    order = names_of(await driver.list_cells(with_code=False))
    assert order.index("shuffle_first") < order.index("shuffle_second")
    assert order.index("shuffle_second") < order.index("shuffle_third")

    moved = await driver.move_cell("shuffle_third", before="shuffle_first")

    assert moved.cell is not None
    assert moved.cell.name == "shuffle_third"
    order = names_of(await driver.list_cells(with_code=False))
    assert order.index("shuffle_third") < order.index("shuffle_first")

    # shuffle_third refers to shuffle_first, so deleting the definition has to be reported as
    # breaking a dependent cell rather than passing quietly (PLAN 6.2).
    deleted = await driver.delete_cell("shuffle_first")

    assert deleted.cell is not None
    assert deleted.cell.name == "shuffle_first"
    assert deleted.descendants
    assert "shuffle_first" not in names_of(await driver.list_cells(with_code=False))

    third = (await driver.read_graph()).node("shuffle_third")
    assert third is not None
    assert third.refs == ("shuffle_first_value",)


# ── validation, enforced kernel-side ─────────────────────────────────────────────────────────


async def test_a_multiply_defined_name_arrives_as_typed_conflicts(
    driver: NotebookDriver,
) -> None:
    """marimo enforces the single-definition rule at flush, as prose. kedge parses it.

    docs/marimo-api.md 4.2 records the message as
    ``- 'seed_value' is already defined in cell 'MJUe' (_)``. The parser takes both the quoted id
    and the parenthesised name, because either may be the one a reader recognises -- and until now
    it had never seen the real thing. If marimo rewords this, the conflict list empties and this
    test says so before a user meets a raw traceback.
    """
    original = await driver.create_cell("clash_seed_value = 1", name="clash_owner")
    assert original.cell is not None

    with pytest.raises(MultiplyDefinedError) as caught:
        await driver.create_cell("clash_seed_value = 2", name="clash_intruder")

    error = caught.value
    assert error.conflicts, f"the conflict list did not parse from: {error}"
    conflict = error.conflicts[0]
    assert conflict.name == "clash_seed_value"
    assert original.cell.id in conflict.defined_in
    assert "clash_owner" in conflict.defined_in
    assert "clash_seed_value" in str(conflict)
    assert "single-definition rule" in str(error)

    # Validation runs before the batch, so the intruder never existed and the owner is untouched.
    assert "clash_intruder" not in names_of(await driver.list_cells(with_code=False))
    assert (await driver.probe("clash_seed_value")).value_repr == "1"


async def test_code_that_does_not_compile_is_refused_before_anything_changes(
    driver: NotebookDriver,
) -> None:
    """The dry-run compile happens kernel-side in the same flush, so nothing partial lands."""
    with pytest.raises(CellSyntaxError):
        await driver.create_cell("def (:", name="uncompilable_cell")

    assert "uncompilable_cell" not in names_of(await driver.list_cells(with_code=False))


async def test_a_name_another_cell_already_uses_is_refused(driver: NotebookDriver) -> None:
    """The collision check runs inside the same context as the create, so it cannot race."""
    await driver.create_cell("taken_value = 1", name="taken_name_cell")

    with pytest.raises(CellNameError, match="already used by cell"):
        await driver.create_cell("taken_other_value = 2", name="taken_name_cell")

    assert names_of(await driver.list_cells(with_code=False)).count("taken_name_cell") == 1


async def test_an_unknown_cell_name_raises_cell_not_found(driver: NotebookDriver) -> None:
    """Addressing a cell that is not there is a typed error, not a kernel traceback."""
    with pytest.raises(CellNotFoundError):
        await driver.get_cell("no_cell_is_called_this")


# ── reads that do not pollute ────────────────────────────────────────────────────────────────


async def test_probe_returns_real_values_and_leaves_no_trace_in_the_notebook(
    driver: NotebookDriver,
) -> None:
    """The scratchpad is the cheap read: it sees notebook state and discards its own bindings."""
    await driver.create_cell(
        "pollution_rows = [10, 20, 30]\npollution_label = 'kedge'", name="pollution_source"
    )
    before = await driver.list_cells(with_code=False)

    total = await driver.probe("sum(pollution_rows)")
    label = await driver.probe("pollution_label")
    typed = await driver.probe("[len(pollution_rows), pollution_label]")

    assert total.value_repr == "60"
    assert total.value_type == "int"
    assert label.value_repr == "'kedge'"
    assert typed.value_repr == "[3, 'kedge']"
    assert typed.value_type == "list"

    # A trailing assignment reports what it bound -- and must not leave it behind for the next one.
    assert (await driver.probe("scratch_only = 99")).value_repr == "99"
    assert (await driver.probe("'scratch_only' in dir()")).value_repr == "False"

    # A block binds nothing there is one obvious value for, so it reports none.
    assert (await driver.probe("for _kedge_i in range(2):\n    _kedge_i")).value_repr is None

    after = await driver.list_cells(with_code=False)
    assert names_of(after) == names_of(before)


async def test_a_probe_that_raises_is_a_result_rather_than_an_exception(
    driver: NotebookDriver,
) -> None:
    """An exploratory query failing is information; it is not a bridge failure."""
    probe = await driver.probe("1 / 0")

    assert probe.ok is False
    assert probe.error is not None
    assert "ZeroDivisionError" in probe.error


async def test_list_cells_and_read_graph_describe_the_real_notebook(
    driver: NotebookDriver,
) -> None:
    """The structural reads: names, statuses, definitions, references, and reachability."""
    await driver.create_cell("graph_root_value = 6", name="graph_root")
    await driver.create_cell("graph_leaf_value = graph_root_value * 7", name="graph_leaf")

    cells = await driver.list_cells(with_code=False)
    by_name = {cell.name: cell for cell in cells}

    assert by_name["graph_root"].code is None  # with_code=False leaves the guard armed
    assert by_name["graph_root"].hide_code is False
    assert by_name["graph_root"].status == "idle"
    assert by_name["graph_root"].is_named
    # A cell loaded from disk as ``def _():`` reports "_", not "" (docs/marimo-api.md 7.8).
    assert UNNAMED_CELL in by_name
    assert by_name[UNNAMED_CELL].is_named is False
    assert all(cell.name for cell in cells)

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


# ── UI, notifications, packages ──────────────────────────────────────────────────────────────


async def test_set_ui_value_changes_the_element_the_notebook_holds(
    driver: NotebookDriver,
) -> None:
    """``set_ui_value`` addresses the element by the notebook variable bound to it."""
    await driver.create_cell(
        "import marimo as mo\nui_slider = mo.ui.slider(0, 10, value=1)", name="ui_cell"
    )
    assert (await driver.probe("ui_slider.value")).value_repr == "1"

    result = await driver.set_ui_value("ui_slider", 7)

    assert result.operation == "set_ui_value"
    assert (await driver.probe("ui_slider.value")).value_repr == "7"


async def test_notify_is_accepted_by_the_kernel_in_both_shapes(driver: NotebookDriver) -> None:
    """Notifications are fire-and-forget, and there is no browser here to receive one.

    What this proves is that the payload classes still exist with the fields the driver passes and
    that ``broadcast_raw_notification`` accepts them -- a signature change raises. Whether a banner
    is *rendered* cannot be asserted without a frontend, and is not claimed.
    """
    assert await driver.notify("Contract test", "a banner") is None
    assert await driver.notify("Contract test", "an alert", kind="alert", variant="danger") is None

    # The session survived both, which is the other half of "the kernel accepted them".
    assert (await driver.probe("2 + 2")).value_repr == "4"


async def test_add_packages_reports_the_environment_it_found(driver: NotebookDriver) -> None:
    """Package operations flush before cell operations, and report the before-state.

    ``polars`` is asked for precisely because it is already installed: the point is the round trip
    and the reported environment, not making the contract suite depend on a package index.
    """
    result = await driver.add_packages("polars")

    assert result.added == ("polars",)
    assert result.removed == ()
    assert "polars" in result.installed_before
    assert "marimo" in result.installed_before
    assert result.needs_restart_hint


# ── the result protocol ──────────────────────────────────────────────────────────────────────


async def test_the_payload_survives_marimo_and_cell_chatter_on_the_same_stdout(
    driver: NotebookDriver,
) -> None:
    """stdout is shared, so the delimited payload is the only thing kedge is allowed to read.

    ``_code_mode`` prints its own flush summary there, and the cell prints whatever it likes --
    here, lines engineered to look like a marimo summary and like a JSON result. Nothing in the
    typed result may depend on that text, and the raw console must carry it through unclassified.
    """
    body = "\n".join(
        [
            "print('chatter line one')",
            "print(\"created and ran cell 'ZZZZ' (a_convincing_fake)\")",
            'print(\'{"op": "create_cell", "ok": false}\')',
            "chatter_total = 8",
        ]
    )

    result = await driver.create_cell(body, name="chatter_cell")

    assert result.ok
    assert result.ran
    assert result.status == "idle"
    assert result.cell is not None
    assert result.cell.name == "chatter_cell"
    assert (await driver.probe("chatter_total")).value_repr == "8"

    assert "chatter line one" in result.console
    assert "a_convincing_fake" in result.console
    assert f"created and ran cell '{result.cell.id}' (chatter_cell)" in result.console
    assert RESULT_BEGIN not in result.console
    assert RESULT_END not in result.console


HOSTILE_CELL_VALUES = [
    pytest.param("both ' and \" together", id="both-quotes"),
    pytest.param("back\\slash and \\\\ double", id="backslash"),
    pytest.param("trailing backslash \\", id="trailing-backslash"),
    pytest.param('"""\nblock\n"""', id="triple-quoted-block"),
    pytest.param("a ''' triple single quote", id="triple-single"),
    pytest.param("line one\nline two\r\nline three\rline four", id="newlines"),
    pytest.param("tab\there\x0bvertical\x0cform", id="control-characters"),
    # Written as escapes so this file stays ASCII and carries no emoji (CONVENTIONS).
    pytest.param("caf\u00e9 na\u00efve \u65e5\u672c\u8a9e \U0001f600", id="non-ascii"),
    # U+2028 and U+2029 end a line for str.splitlines() but not for a Python tokeniser, so an
    # embedder that splits on them corrupts the cell body.
    pytest.param("line\u2028separator\u2029paragraph", id="unicode-line-separators"),
    pytest.param("null\x00byte", id="null-byte"),
    pytest.param("f'{oops}' and {braces} and %s and %(name)s", id="format-syntax"),
    pytest.param("", id="empty"),
]
"""The nastiest rows of ``tests/unit/test_driver_codegen.py::HOSTILE_STRINGS``, minus the sentinel
impostor -- that one has a test of its own, below, because reading it back does not work."""


@pytest.mark.parametrize("value", HOSTILE_CELL_VALUES)
async def test_hostile_strings_round_trip_through_a_real_kernel(
    driver: NotebookDriver, value: str, request: pytest.FixtureRequest
) -> None:
    """Embedding is the likeliest place in kedge for a silent, catastrophic bug.

    The unit tests prove ``py_literal`` round-trips these through ``ast.literal_eval`` and that the
    generated source parses. Neither can prove the *kernel* ends up with the same characters: for
    that the string has to cross HTTP as JSON, be compiled by the kernel, be stored in the notebook
    document, and come back. So each case is asserted twice -- the source marimo now holds, and the
    value the kernel actually bound.
    """
    name = f"hostile_{request.node.callspec.id.replace('-', '_')}"
    body = f"{name}_value = {py_literal(value)}"

    created = await driver.create_cell(body, name=name)

    assert created.ok
    assert created.status == "idle"
    assert (await driver.get_cell(name)).code == body
    assert (await driver.probe(f"{name}_value")).value_repr == repr(value)


async def test_the_notebook_file_survives_hostile_code(
    driver: NotebookDriver, live_marimo: LiveMarimo
) -> None:
    """Whatever the model writes, the ``.py`` marimo saves has to still parse and still round-trip.

    The on-disk file is the deliverable and the fallback path (PLAN 6.1 mitigation 4). A cell body
    that corrupts it would not show up in any of the assertions above.
    """
    values = [case.values[0] for case in HOSTILE_CELL_VALUES]
    body = "nasty_values = " + py_literal(values)

    await driver.create_cell(body, name="nasty_cell")
    source = wait_for_notebook(live_marimo.notebook_path, "def nasty_cell(")

    ast.parse(source)
    assert body in source
    assert (await driver.probe("nasty_values")).value_repr == repr(values)


async def test_a_cell_whose_code_contains_the_result_sentinels_can_be_read_back(
    driver: NotebookDriver,
) -> None:
    """The sentinel impostor from the hostile-string table, run for real.

    Writing it is fine -- ``create_cell`` never echoes the code back, and the impostor a cell
    *prints* lands before kedge's own payload, so it is harmless. Reading it is not: the source
    comes home inside the JSON payload, carrying kedge's delimiters with it. The cell is deleted
    afterwards whatever happens, because while it exists no ``list_cells(with_code=True)`` on this
    notebook can succeed.
    """
    impostor = f"{RESULT_BEGIN} impostor {RESULT_END}"
    body = f"sentinel_value = {py_literal(impostor)}"

    created = await driver.create_cell(body, name="sentinel_cell")
    try:
        assert created.ok  # writing it is not the problem
        assert (await driver.probe("sentinel_value")).value_repr == repr(impostor)
        assert (await driver.get_cell("sentinel_cell")).code == body
    finally:
        await driver.delete_cell("sentinel_cell")


# ── the read-before-write guard ──────────────────────────────────────────────────────────────


async def test_the_staleness_guard_fires_and_can_be_satisfied_two_ways(
    guard_driver: NotebookDriver, guard_marimo: LiveMarimo
) -> None:
    """PLAN 6.2's "user edited a cell between turns" protection, end to end.

    Deliberately one test in four phases rather than four tests. The guard is armed per cell and
    disarmed by reading, so the phases share one piece of kernel state and only mean anything in
    order; splitting them would buy independence in name and lose it in fact. It has a server to
    itself (see ``conftest.py``) so nothing else can disarm what it is measuring, and so a failure
    here cannot cascade into the rest of this file.

    The three cells were loaded from disk. kedge has never written them and never read them, which
    is exactly the state a notebook is in when the user has been typing in it between turns.
    """
    ids = {cell.name: cell.id for cell in await guard_driver.list_cells(with_code=False)}
    assert {"guard_unread", "guard_forced", "guard_listed"} <= set(ids)

    # 1. Unread cell, plain edit: refused, and named precisely enough to retry.
    with pytest.raises(StaleCellError) as caught:
        await guard_driver.edit_cell("guard_unread", "guard_unread_value = 101")

    error = caught.value
    assert error.retryable is True
    assert error.cell_id == ids["guard_unread"]
    assert ids["guard_unread"] in error.stale_cells
    assert "re-read the cell" in str(error)

    # 2. overwrite_unread reads the cell inside the call, so the guard is satisfied honestly and
    #    the user's version comes back rather than being lost. Only meaningful while unread.
    forced = await guard_driver.edit_cell(
        "guard_forced", "guard_forced_value = 202", overwrite_unread=True
    )
    assert forced.ok
    assert forced.previous_code == GUARD_FORCED_CODE

    # 3. A read recorded by get_cell in an earlier submission still counts in a later one: the
    #    read tracker lives on the kernel, not on the context object.
    fetched = await guard_driver.get_cell("guard_unread")
    assert fetched.code == "guard_unread_value = 1"  # phase 1 really did change nothing

    edited = await guard_driver.edit_cell("guard_unread", "guard_unread_value = 101")
    assert edited.ok
    assert edited.previous_code is None  # no read was needed inside this call
    assert (await guard_driver.get_cell("guard_unread")).code == "guard_unread_value = 101"

    # 4. list_cells(with_code=True) reads every cell's code, disarming the guard notebook-wide.
    #    That is why the driver offers with_code=False, and why nothing else here passes True.
    listed = await guard_driver.list_cells(with_code=True)
    assert all(cell.code is not None for cell in listed)

    after_listing = await guard_driver.edit_cell("guard_listed", "guard_listed_value = 303")
    assert after_listing.ok
    assert (await guard_driver.probe("guard_listed_value")).value_repr == "303"

    # And all of it reached the file the user opens.
    source = wait_for_notebook(guard_marimo.notebook_path, "guard_listed_value = 303")
    assert "guard_unread_value = 101" in source
    assert "guard_forced_value = 202" in source
