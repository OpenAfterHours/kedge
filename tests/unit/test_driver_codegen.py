"""The code generator is a pure function, so it is tested without a kernel anywhere near it.

Every test here builds source text and inspects it. Nothing spawns marimo, nothing opens a
socket. That is the point of the split described in `kedge.notebook.driver`: the risky, private
part of the bridge is a string builder, and a string builder can be pinned down exactly.

The escaping tests parse the generated program back with `ast` and recover the embedded literal,
rather than matching on substrings. A substring assertion would pass on source that is subtly
wrong; recovering the value proves the round trip.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from kedge.notebook import driver
from kedge.notebook.driver import (
    CellNameError,
    CellSyntaxError,
    EmbeddingError,
    MultiplyDefinedError,
    NotebookDriver,
    StaleCellError,
    build_program,
    extract_payload,
    parse_definition_conflicts,
    py_literal,
    validate_cell_name,
)
from kedge.notebook.model import ExecResult, NotebookBridge

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"

HOSTILE_STRINGS = [
    pytest.param("it's a single quote", id="single-quote"),
    pytest.param('he said "hello"', id="double-quote"),
    pytest.param("both ' and \" together", id="both-quotes"),
    pytest.param("back\\slash and \\\\ double", id="backslash"),
    pytest.param("trailing backslash \\", id="trailing-backslash"),
    pytest.param('a """ triple double quote', id="triple-double"),
    pytest.param("a ''' triple single quote", id="triple-single"),
    pytest.param('"""\nblock\n"""', id="triple-quoted-block"),
    pytest.param("line one\nline two\r\nline three\rline four", id="newlines"),
    pytest.param("tab\there\x0bvertical\x0cform", id="control-characters"),
    pytest.param("café naïve résumé", id="latin-1-accents"),
    pytest.param("日本語のコメント", id="cjk"),
    pytest.param("emoji 😀 outside the BMP", id="astral-emoji"),
    # Written as escapes so this source file stays ASCII. U+2028 and U+2029 are line
    # terminators to str.splitlines() but not to a Python tokeniser, so an embedder that
    # splits on them corrupts the cell body -- which is exactly what this case catches.
    pytest.param("line separator paragraph", id="unicode-line-separators"),
    pytest.param("null\x00byte", id="null-byte"),
    pytest.param("<<<KEDGE-RESULT-BEGIN>>> impostor <<<KEDGE-RESULT-END>>>", id="sentinels"),
    pytest.param("f'{oops}' and {braces} and %s and %(name)s", id="format-syntax"),
    pytest.param(
        "print('x'); import os  # \\N{GREEK SMALL LETTER ALPHA}", id="escape-sequence-name"
    ),
    pytest.param("", id="empty"),
]


def embedded_call_arg(program: str, function: str, index: int = 0) -> str:
    """Recover a positional argument of a call to ``function`` in generated source.

    The index matters because the mutators do not share an argument order: ``create_cell``
    takes the code first, while ``edit_cell`` takes the target first and the code second.

    Args:
        program: The generated Python source.
        function: The attribute name of the call to find, e.g. ``"edit_cell"``.
        index: Which positional argument to recover.

    Returns:
        The literal value of that argument.
    """
    tree = ast.parse(program)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == function
            and len(node.args) > index
        ):
            return ast.literal_eval(node.args[index])
    msg = f"no call to {function}() with a positional argument {index} was generated"
    raise AssertionError(msg)


# ── embedding and escaping ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", HOSTILE_STRINGS)
def test_py_literal_round_trips_hostile_strings(value: str) -> None:
    assert ast.literal_eval(py_literal(value)) == value


@pytest.mark.parametrize("value", HOSTILE_STRINGS)
def test_embedded_cell_code_survives_generation_unchanged(value: str) -> None:
    program = driver.generate_create_cell(value, name="hostile_cell")
    ast.parse(program)
    assert embedded_call_arg(program, "create_cell") == value


@pytest.mark.parametrize("value", HOSTILE_STRINGS)
def test_embedded_edit_code_survives_generation_unchanged(value: str) -> None:
    program = driver.generate_edit_cell("target", value)
    ast.parse(program)
    # edit_cell(target, code) — the code is the second positional argument.
    assert embedded_call_arg(program, "edit_cell", 1) == value


def test_embedded_literal_is_always_one_physical_line() -> None:
    # Bodies are assembled by indenting text, so an embedded literal that spanned lines would
    # have its continuation lines re-indented into the wrong block.
    program = driver.generate_create_cell("a\nb\nc\n d", name="multiline_cell")
    creating = [line for line in program.splitlines() if ".create_cell(" in line]
    assert len(creating) == 1


def test_py_literal_refuses_a_value_with_no_literal_form() -> None:
    with pytest.raises(EmbeddingError, match="cannot embed"):
        py_literal(object())


def test_py_literal_refuses_a_float_that_cannot_round_trip() -> None:
    with pytest.raises(EmbeddingError, match="cannot embed"):
        py_literal(float("nan"))


def test_py_literal_handles_nested_containers() -> None:
    value = {"a": [1, 2.5, None, True], "b": ("x", "y'z")}
    assert ast.literal_eval(py_literal(value)) == value


# ── cell names ───────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["load_handin", "stage1", "reconcile", "Apply_Haircuts"])
def test_valid_cell_names_are_accepted(name: str) -> None:
    assert validate_cell_name(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "has space",
        "1leading_digit",
        "has-hyphen",
        "class",
        "__init__",
        "_private",
        "_",
        "x" * 65,
    ],
)
def test_invalid_cell_names_are_rejected(name: str) -> None:
    with pytest.raises(CellNameError):
        validate_cell_name(name)


def test_create_cell_requires_a_valid_name() -> None:
    with pytest.raises(CellNameError):
        driver.generate_create_cell("x = 1", name="not a name")


# ── the rules the generated code must obey ───────────────────────────────────────────────────


def test_create_cell_always_passes_hide_code_false() -> None:
    program = driver.generate_create_cell("x = 1", name="visible_cell")
    assert "hide_code=False" in program
    assert "hide_code=True" not in program


def test_create_cell_always_passes_a_name() -> None:
    program = driver.generate_create_cell("x = 1", name="named_cell")
    assert "name='named_cell'" in program


def test_create_cell_checks_the_name_is_free_kernel_side() -> None:
    program = driver.generate_create_cell("x = 1", name="maybe_taken")
    assert "_kedge_taken" in program
    assert "CellNameInUse" in program


def test_edit_cell_reads_the_code_first_only_when_overwriting_unread() -> None:
    reading = driver.generate_edit_cell("cell_a", "x = 2", overwrite_unread=True)
    assert "_kedge_ctx.cells[_kedge_target].code" in reading

    guarded = driver.generate_edit_cell("cell_a", "x = 2")
    # The shared preamble defines a describe() helper that can return code, so a blanket search
    # for ".code" matches boilerplate. What matters is that the default performs no read of the
    # target's code, and asks describe() not to either.
    assert "_kedge_ctx.cells[_kedge_target].code" not in guarded, (
        "the default must leave marimo's staleness guard armed"
    )
    assert "_kedge_describe(_kedge_ctx.cells[_kedge_target], False)" in guarded


def test_no_generated_program_ever_disables_the_staleness_guard() -> None:
    programs = [
        driver.generate_create_cell("x = 1", name="a_cell"),
        driver.generate_edit_cell("a_cell", "x = 2"),
        driver.generate_edit_cell("a_cell", "x = 2", overwrite_unread=True),
        driver.generate_delete_cell("a_cell"),
        driver.generate_move_cell("a_cell", before="b_cell"),
        driver.generate_run_cell("a_cell"),
        driver.generate_read_graph(),
        driver.generate_list_cells(with_code=True),
        driver.generate_get_cell("a_cell"),
    ]
    for program in programs:
        assert "skip_staleness_check" not in program
        assert "skip_validation" not in program


def test_no_generated_program_ever_reprs_the_cells_view() -> None:
    # _CellsView.__repr__ reads every cell's code, which would silently disarm the staleness
    # guard for the whole notebook (docs/marimo-api.md 4.1).
    programs = [
        driver.generate_list_cells(with_code=False),
        driver.generate_read_graph(),
        driver.generate_delete_cell("a_cell"),
    ]
    for program in programs:
        assert "repr(_kedge_ctx.cells" not in program
        assert "str(_kedge_ctx.cells)" not in program


def test_listing_without_code_never_touches_cell_source() -> None:
    program = driver.generate_list_cells(with_code=False)
    assert "_kedge_describe(_kedge_cell, False)" in program


def test_listing_with_code_asks_for_it_explicitly() -> None:
    program = driver.generate_list_cells(with_code=True)
    assert "_kedge_describe(_kedge_cell, True)" in program


@pytest.mark.parametrize(
    "generate",
    [
        lambda: driver.generate_create_cell("x = 1", name="a_cell"),
        lambda: driver.generate_edit_cell("a_cell", "x = 1"),
        lambda: driver.generate_delete_cell("a_cell"),
        lambda: driver.generate_move_cell("a_cell", after="b_cell"),
        lambda: driver.generate_run_cell("a_cell"),
        lambda: driver.generate_list_cells(with_code=True),
        lambda: driver.generate_get_cell(0),
        lambda: driver.generate_read_graph(),
        lambda: driver.generate_set_ui_value("slider", 5),
        lambda: driver.generate_notify("title", "body"),
        lambda: driver.generate_add_packages(["polars"]),
        lambda: driver.generate_probe("1 + 1"),
    ],
)
def test_every_generated_program_is_valid_python(generate) -> None:
    ast.parse(generate())


@pytest.mark.parametrize(
    "generate",
    [
        lambda: driver.generate_create_cell("x = 1", name="a_cell"),
        lambda: driver.generate_edit_cell("a_cell", "x = 1"),
        lambda: driver.generate_delete_cell("a_cell"),
        lambda: driver.generate_run_cell("a_cell"),
        lambda: driver.generate_read_graph(),
    ],
)
def test_every_code_mode_program_emits_a_delimited_payload(generate) -> None:
    program = generate()
    assert driver.RESULT_BEGIN in program
    assert driver.RESULT_END in program
    assert program.rstrip().endswith("_kedge_sys.stdout.flush()")


def test_create_cell_rejects_two_anchors() -> None:
    with pytest.raises(CellNameError):
        driver.generate_create_cell("x = 1", name="a_cell", before="b", after="c")


def test_move_cell_requires_exactly_one_anchor() -> None:
    with pytest.raises(CellNameError):
        driver.generate_move_cell("a_cell")
    with pytest.raises(CellNameError):
        driver.generate_move_cell("a_cell", before="b", after="c")


def test_edit_cell_requires_something_to_change() -> None:
    with pytest.raises(CellNameError):
        driver.generate_edit_cell("a_cell")


def test_edit_cell_resolves_indexes_through_the_cells_view() -> None:
    # edit_cell() itself accepts an id or a name but not an index, so the index has to be
    # resolved before the call.
    program = driver.generate_edit_cell(-1, "x = 1")
    assert "_kedge_ctx.cells[-1].id" in program


def test_delete_cell_reports_dependants() -> None:
    program = driver.generate_delete_cell("a_cell")
    assert "descendants" in program


def test_set_ui_value_addresses_the_element_through_context_globals() -> None:
    program = driver.generate_set_ui_value("threshold", 0.5)
    assert "_kedge_ctx.globals['threshold']" in program
    with pytest.raises(CellNameError):
        driver.generate_set_ui_value("not an identifier", 1)


def test_notify_builds_the_requested_notification_class() -> None:
    assert "BannerNotification(" in driver.generate_notify("t", "d")
    assert "AlertNotification(" in driver.generate_notify("t", "d", kind="alert")
    with pytest.raises(CellNameError):
        driver.generate_notify("t", "d", kind="toast")
    with pytest.raises(CellNameError):
        driver.generate_notify("t", "d", variant="lilac")


def test_add_packages_lists_before_mutating() -> None:
    program = driver.generate_add_packages(["polars"], ["pandas"])
    assert program.index("packages.list()") < program.index("packages.add(")
    with pytest.raises(CellNameError):
        driver.generate_add_packages([])


def test_generated_programs_are_deterministic() -> None:
    first = driver.generate_create_cell("x = 1", name="stable_cell")
    second = driver.generate_create_cell("x = 1", name="stable_cell")
    assert first == second


# ── probes ───────────────────────────────────────────────────────────────────────────────────


def test_probe_binds_a_trailing_expression_so_its_value_comes_back() -> None:
    program = driver.generate_probe("frame.height")
    assert "_kedge_value = frame.height" in program


def test_probe_leaves_statement_only_code_without_a_value() -> None:
    # An assignment is genuinely statement-only. Note `print('hello')` would NOT be: it is an
    # expression statement, so its value (None) is correctly bound and reported.
    program = driver.generate_probe("x = 1")
    assert "_kedge_text = repr(_kedge_value) if False else None" in program


def test_probe_never_indents_the_submitted_source() -> None:
    # Indenting arbitrary source would rewrite the contents of multi-line string literals.
    program = driver.generate_probe('note = """\nkeep this exact\n"""\nnote')
    assert "\nnote = " in program
    assert ast.literal_eval(ast.parse(program).body[3].value) == "\nkeep this exact\n"  # type: ignore[attr-defined]


def test_probe_supports_top_level_await() -> None:
    ast.parse(
        driver.generate_probe("await something()")
        .replace("await ", "")
        .replace("_kedge_value = something()", "_kedge_value = something()")
    )


def test_probe_rejects_source_that_does_not_parse() -> None:
    with pytest.raises(CellSyntaxError, match="does not parse"):
        driver.generate_probe("def (")


def test_probe_does_not_import_code_mode() -> None:
    assert "_code_mode" not in driver.generate_probe("1 + 1")


# ── reading the reply ────────────────────────────────────────────────────────────────────────


def payload_text(body: str) -> str:
    return f"{driver.RESULT_BEGIN}\n{body}\n{driver.RESULT_END}\n"


def test_payload_is_extracted_from_surrounding_chatter() -> None:
    stdout = "created and ran cell 'UMIT' (kedge_cell)\n" + payload_text('{"ok": true}')
    assert extract_payload(stdout) == {"ok": True}


def test_the_last_payload_wins_when_user_output_mimics_the_sentinels() -> None:
    stdout = payload_text('{"ok": false, "impostor": true}') + payload_text('{"ok": true}')
    assert extract_payload(stdout) == {"ok": True}


def test_a_malformed_payload_falls_back_to_an_earlier_complete_one() -> None:
    stdout = payload_text('{"ok": true}') + payload_text("not json at all")
    assert extract_payload(stdout) == {"ok": True}


def test_no_payload_returns_none() -> None:
    assert extract_payload("created and ran cell 'UMIT'\n") is None
    assert extract_payload("") is None


def test_a_truncated_payload_returns_none() -> None:
    assert extract_payload(driver.RESULT_BEGIN + '\n{"ok": true}\n') is None


def test_a_payload_containing_the_sentinels_is_still_read() -> None:
    """Regression, found by the live-kernel contract suite.

    The payload carries cell source and rendered output, so a cell whose body merely *mentions*
    the marker text puts the marker inside the payload. Scanning back to the nearest opening
    marker finds that embedded one, fails to decode, and — if the closing pointer is moved
    instead of the opening one — never retries the real payload. The effect was that
    `get_cell` and `list_cells(with_code=True)` failed for the *whole notebook*, blaming an
    interrupted kernel, because one cell contained a string.
    """
    code = f"x = '{driver.RESULT_BEGIN} impostor {driver.RESULT_END}'"
    stdout = (
        f"\n{driver.RESULT_BEGIN}\n"
        + json.dumps({"ok": True, "data": {"code": code}})
        + f"\n{driver.RESULT_END}\n"
    )

    payload = extract_payload(stdout)

    assert payload is not None, "a cell mentioning the sentinel must not break reading the reply"
    assert payload["data"]["code"] == code


def test_multiply_defined_message_parses_into_conflicts() -> None:
    message = (
        "Multiply-defined names:\n"
        "  - 'seed_value' is already defined in cell 'MJUe' (_)\n"
        "  - 'other_value' is already defined in cell 'Ab12' (loader), 'Cd34'\n"
        "  - 'bare_value'\n\n"
        "To skip validation, use: async with cm.get_context(skip_validation=True) as ctx"
    )
    conflicts = parse_definition_conflicts(message)
    assert [c.name for c in conflicts] == ["seed_value", "other_value", "bare_value"]
    assert conflicts[0].defined_in == ("MJUe", "_")
    assert conflicts[1].defined_in == ("Ab12", "loader", "Cd34")
    assert conflicts[2].defined_in == ()


def failing(kind: str, message: str) -> tuple[dict, ExecResult]:
    payload = {"op": "create_cell", "ok": False, "error": {"type": kind, "message": message}}
    return payload, ExecResult(success=True, stdout="", stderr="")


def test_a_stale_cell_error_becomes_a_typed_retryable_failure() -> None:
    payload = {
        "op": "edit_cell",
        "ok": False,
        "error": {
            "type": "StaleCellError",
            "message": "Cell 'Hbol' was modified since the agent last read it.",
            "cell_id": "Hbol",
            "stale_cells": ["Hbol", "Abcd"],
        },
    }
    with pytest.raises(StaleCellError) as caught:
        driver._raise_for_payload(payload, ExecResult(success=True, stdout="", stderr=""))
    assert caught.value.cell_id == "Hbol"
    assert caught.value.stale_cells == ("Hbol", "Abcd")
    assert caught.value.retryable is True


def test_a_multiply_defined_runtime_error_becomes_a_typed_violation() -> None:
    payload, result = failing(
        "RuntimeError",
        "Multiply-defined names:\n  - 'seed_value' is already defined in cell 'MJUe' (_)",
    )
    with pytest.raises(MultiplyDefinedError) as caught:
        driver._raise_for_payload(payload, result)
    assert [c.name for c in caught.value.conflicts] == ["seed_value"]
    assert "single-definition rule" in str(caught.value)


def test_a_cycle_becomes_a_typed_validation_failure() -> None:
    payload, result = failing("RuntimeError", "Cycles detected: {('a', 'b')}")
    with pytest.raises(driver.CycleDetectedError):
        driver._raise_for_payload(payload, result)


def test_a_syntax_error_becomes_a_typed_validation_failure() -> None:
    payload, result = failing("SyntaxError", "invalid syntax (<unknown>, line 1)")
    with pytest.raises(CellSyntaxError):
        driver._raise_for_payload(payload, result)


def test_a_missing_cell_becomes_a_typed_lookup_failure() -> None:
    payload, result = failing("KeyError", "\"Cell 'nope' not found in notebook or pending adds\"")
    with pytest.raises(driver.CellNotFoundError):
        driver._raise_for_payload(payload, result)


def test_an_unclassified_kernel_failure_keeps_its_type_and_traceback() -> None:
    payload = {
        "ok": False,
        "error": {"type": "ZeroDivisionError", "message": "division by zero", "traceback": "..."},
    }
    with pytest.raises(driver.KernelExecutionError) as caught:
        driver._raise_for_payload(payload, ExecResult(success=True, stdout="", stderr=""))
    assert caught.value.error_type == "ZeroDivisionError"


def test_a_run_with_no_payload_is_reported_as_an_interrupted_program() -> None:
    result = ExecResult(success=False, stdout="", stderr="KeyboardInterrupt\n")
    with pytest.raises(driver.KernelExecutionError, match="no result payload"):
        driver._require_payload(result, "create_cell")


def test_console_output_excludes_the_payload() -> None:
    result = ExecResult(
        success=True,
        stdout="hello from the cell\ncreated and ran cell 'UMIT'\n" + payload_text('{"ok": true}'),
        stderr="",
    )
    console = driver._console_of(result)
    assert "hello from the cell" in console
    assert driver.RESULT_BEGIN not in console
    assert '{"ok": true}' not in console


# ── the interface and the startup assertion ──────────────────────────────────────────────────


def test_the_driver_satisfies_the_shared_bridge_protocol() -> None:
    built = NotebookDriver(
        base_url="http://127.0.0.1:2718",
        token="secret",
        session_id="kedge-test",
        verify=False,
    )
    assert isinstance(built, NotebookBridge)


def test_the_driver_never_puts_the_token_in_its_repr() -> None:
    built = NotebookDriver(
        base_url="http://127.0.0.1:2718",
        token="super-secret-token",
        session_id="kedge-test",
        verify=False,
    )
    assert "super-secret-token" not in repr(built)


def test_the_bridge_check_passes_against_the_pinned_marimo() -> None:
    report = driver.check_bridge()
    assert report.ok, report.message()
    assert report.version == driver.MARIMO_PIN
    assert driver.verify_bridge() is report


def test_the_bridge_report_message_names_the_installed_version() -> None:
    report = driver.BridgeReport(
        version="9.9.9", pinned="0.23.15", problems=("create_cell is gone",)
    )
    message = report.message()
    assert "9.9.9" in message
    assert "0.23.15" in message
    assert "create_cell is gone" in message
    assert not report.ok


def test_build_program_reports_the_operation_it_was_built_for() -> None:
    program = build_program("some_operation", "pass")
    assert "\"op\": 'some_operation'" in program


# ── the non-negotiable ───────────────────────────────────────────────────────────────────────


def _code_references(path: Path) -> bool:
    """Whether this module imports or otherwise *executes against* ``marimo._code_mode``.

    Deliberately blind to string constants. Prose is allowed anywhere and is not a violation:
    several modules carry a docstring saying they specifically do NOT touch ``_code_mode``, which
    is exactly the right thing for them to say, and ``cli.py`` names it in user-facing help.
    A comment cannot execute anything. What the convention actually forbids is *reaching* it.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any("_code_mode" in alias.name for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if "_code_mode" in (node.module or ""):
                return True
        elif isinstance(node, ast.Attribute) and node.attr == "_code_mode":
            return True
    return False


def test_code_mode_is_reached_from_driver_and_nowhere_else_in_src() -> None:
    # CONVENTIONS non-negotiable 2. driver.py's single import lives inside verify_bridge(), which
    # introspects the real signatures so a marimo bump fails with a clear message naming the
    # version rather than a TypeError mid-conversation (PLAN 6.1 mitigation 5). It never calls
    # get_context() in kedge's process -- that only works inside a kernel, and the driver reaches
    # it by generating source that the kernel executes.
    offenders = sorted(
        path.relative_to(SRC_ROOT).as_posix()
        for path in SRC_ROOT.rglob("*.py")
        if _code_references(path)
    )
    assert offenders == ["kedge/notebook/driver.py"], (
        "marimo._code_mode must be confined to driver.py (CONVENTIONS non-negotiable 2)"
    )


def test_driver_never_calls_get_context_in_our_own_process() -> None:
    # The import is for introspection only. An actual call would raise, because get_context()
    # requires a running kernel driven through the execute endpoint.
    tree = ast.parse((SRC_ROOT / "kedge" / "notebook" / "driver.py").read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get_context"
    ]
    assert calls == [], "get_context() must only appear inside generated source, never as code"
