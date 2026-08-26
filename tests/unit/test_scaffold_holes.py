"""Tests for the hole seam in ``kedge.notebook.scaffold``.

Four functions -- ``is_unwritten``, ``holes_in``, ``split_hole`` and ``strip_marker`` -- are the
whole contract between the thing that *writes* a ``TODO(kedge)`` marker and everything that reads
one back: the conversion driver, anything counting what a notebook still owes, and the sync that
must not mistake a translated cell for an untouched one. They live in the module that emits the
marker so the two cannot drift apart, and these assert the parts of that contract a caller relies
on rather than the parts that happen to be true today.

Two of them carry a bug each in their docstrings, and both are asserted here. ``split_hole`` keeps
the ``_gate_...`` assignment in the *header*, because it is the line that makes a cell invisible
until its checkpoint is recorded and a body rewritten without it would silently un-gate the
notebook. ``strip_marker`` takes the marker out on the way back in, because ``holes_in`` finds
holes by it -- leave it above working code and the cell reads as unwritten for ever and every
count of what is left is wrong.
"""

from __future__ import annotations

import ast

from conftest import make_approved_plan, make_draft
from kedge.notebook.scaffold import (
    TODO_MARKER,
    ScaffoldCell,
    build_cells,
    holes_in,
    is_unwritten,
    split_hole,
    strip_marker,
)
from kedge.plan.model import SourceOrigin, Stage, StageKind, StageSource


def scaffolded() -> list[ScaffoldCell]:
    """A real scaffold, so these test the marker the scaffolder writes rather than a literal."""
    return build_cells(make_approved_plan())


def hole_named(name: str) -> ScaffoldCell:
    return next(cell for cell in holes_in(scaffolded()) if cell.name == name)


# ── is_unwritten ─────────────────────────────────────────────────────────────────────────────


def test_a_body_carrying_the_marker_is_unwritten() -> None:
    assert is_unwritten(f"# {TODO_MARKER}: translate this stage\nx = 1")


def test_a_body_with_no_marker_is_written() -> None:
    assert not is_unwritten("# Stage 1 of 4: load\nload = handin_frame")


def test_a_humans_note_below_a_finished_translation_is_not_a_hole() -> None:
    """The defect a substring test caused, and it cost a user their work.

    A finished translation carrying a reviewer's note read as a hole: everything above the note
    became the header, ``strip_marker`` deleted the note, and a model's answer was appended below
    a translation that was now dead code.
    """
    translated = (
        "# Stage 2 of 8: apply_uplift\n"
        "apply_uplift = scope.with_columns(pl.col('accrual').xl.mul(1.045))\n"
        f"# {TODO_MARKER}: E-12 still needs the statutory-only filter -- Phil, 2026-08\n"
        "apply_uplift"
    )

    assert not is_unwritten(translated)
    assert split_hole(translated) == ("", translated)


def test_a_trailing_comment_mentioning_the_marker_is_not_a_hole() -> None:
    assert not is_unwritten(
        f"alpha = 1  # {TODO_MARKER} asked for a LazyFrame; this one is eager on purpose"
    )


def test_the_marker_embedded_in_a_statement_is_still_a_hole_but_cannot_be_split() -> None:
    """The third emitter: a hand-off the plan gave no statement writes it into the literal.

    Still unwritten -- there is no statement to hand anybody -- but there is no placeholder a
    splice could replace, so ``split_hole`` refuses and nothing may overwrite it.
    """
    handoff = (
        "update_statement = "
        f"'-- {TODO_MARKER}: the plan marked this stage a hand-off but supplied no statement.'\n"
        "mo.vstack([])"
    )

    assert is_unwritten(handoff)
    assert split_hole(handoff) == ("", handoff)


# ── holes_in ─────────────────────────────────────────────────────────────────────────────────


def test_holes_in_returns_only_the_cells_still_carrying_the_marker() -> None:
    cells = scaffolded()
    holes = holes_in(cells)

    assert holes, "a scaffolded plan with automated stages must leave holes"
    assert all(is_unwritten(cell.code) for cell in holes)
    assert {cell.name for cell in holes} <= {cell.name for cell in cells}


def test_holes_in_keeps_the_scaffolders_own_order() -> None:
    """Order is the contract: a hole is filled against the names defined above it."""
    cells = scaffolded()
    holes = holes_in(cells)

    positions = [cells.index(cell) for cell in holes]
    assert positions == sorted(positions)


def test_holes_in_finds_more_than_stage_cells() -> None:
    """Three places emit the marker; the region map is the one a role filter would miss."""
    names = {cell.name for cell in holes_in(scaffolded())}

    assert "reconciliation_values" in names


def test_a_notebook_with_nothing_left_has_no_holes() -> None:
    filled = [ScaffoldCell(name="one", code="one = 1"), ScaffoldCell(name="two", code="two = 2")]

    assert holes_in(filled) == ()


# ── split_hole ───────────────────────────────────────────────────────────────────────────────


def test_split_hole_puts_the_documentation_in_the_header_and_the_placeholder_below() -> None:
    hole = hole_named("apply_haircuts")
    header, placeholder = split_hole(hole.code)

    assert header.startswith("# Stage 2 of 4: apply_haircuts")
    assert TODO_MARKER in header
    assert TODO_MARKER not in placeholder
    assert "apply_haircuts = " in placeholder
    assert f"{header}\n{placeholder}" == hole.code


def test_split_hole_keeps_the_checkpoint_gate_out_of_the_placeholder() -> None:
    """The gate is not translation. A body rewritten without it would un-gate the notebook."""
    hole = hole_named("write_output")
    header, placeholder = split_hole(hole.code)

    assert '_gate_write_output = manual_overrides["decision"]' in header
    assert "_gate_" not in placeholder


def test_split_hole_leaves_a_cell_that_is_not_a_hole_alone() -> None:
    """``("", code)`` rather than an empty header, so a caller cannot overwrite a finished cell."""
    code = "# Stage 1 of 4: load\nload = handin_frame"

    assert split_hole(code) == ("", code)


# ── strip_marker ─────────────────────────────────────────────────────────────────────────────


def test_strip_marker_removes_the_instruction_and_its_separator() -> None:
    header, _ = split_hole(hole_named("apply_haircuts").code)
    kept = strip_marker(header)

    assert TODO_MARKER not in kept
    assert "LazyFrame throughout" not in kept
    assert not kept.endswith("#")


def test_strip_marker_keeps_every_line_that_documents_the_stage() -> None:
    header, _ = split_hole(hole_named("write_output").code)
    kept = strip_marker(header)

    assert "# Intent: Produce the monthly RWA summary" in kept
    assert "# After: apply_haircuts, manual_overrides" in kept
    assert "REVIEW: the plan records confidence 'low' here." in kept
    assert '_gate_write_output = manual_overrides["decision"]' in kept


def test_strip_marker_leaves_a_header_that_never_had_one_unchanged() -> None:
    header = "# Stage 1 of 4: load\n# Intent: read the hand-in"

    assert strip_marker(header) == header


def test_a_filled_cell_stops_reading_as_a_hole() -> None:
    """The round trip the driver performs: split, strip, splice. The marker must not survive."""
    hole = hole_named("apply_haircuts")
    header, _ = split_hole(hole.code)
    spliced = f"{strip_marker(header)}\napply_haircuts = load_handin.with_columns()"

    assert not is_unwritten(spliced)
    assert holes_in([ScaffoldCell(name=hole.name, code=spliced)]) == ()


# ── plan text reaching generated code ────────────────────────────────────────────────────────


def test_a_handin_ref_with_a_quote_a_newline_and_a_backslash_still_scaffolds() -> None:
    """Plan text interpolated into a quoted literal is the same bug as SQL by concatenation.

    ``label="Drop {label} here"`` closed its own literal the moment somebody called a re-extract
    ``the "after" extract`` -- an entirely ordinary thing to write -- and the whole scaffold died
    with ``cell 'post_adjustment_input' would not parse``. Non-negotiable 3, with Python in the
    place of SQL.
    """
    hostile = 'the "after" extract\nfrom \\\\server\\share'
    plan = make_approved_plan(
        draft=make_draft(
            stages=[
                Stage(
                    id="load_handin",
                    intent="Read the first extract",
                    kind=StageKind.LOAD,
                    sources=[StageSource(origin=SourceOrigin.HANDIN)],
                ),
                Stage(
                    id="reextract",
                    intent="Read the extract taken after the update",
                    kind=StageKind.LOAD,
                    depends_on=["load_handin"],
                    sources=[StageSource(origin=SourceOrigin.HANDIN, ref=hostile)],
                ),
            ],
            dropped=[],
        )
    )

    cells = build_cells(plan)

    for cell in cells:
        ast.parse(cell.code)  # `build_cells` checks this too; assert it rather than trust it
    selector = next(cell for cell in cells if cell.name == "reextract_input")
    assert f"Drop {hostile} here" in string_literals(selector.code), (
        "the label has to survive intact, not merely fail to break the parse"
    )
    receipt = next(cell for cell in cells if cell.name == "reextract_handin")
    assert f"**{hostile}**" in string_literals(receipt.code)


def string_literals(code: str) -> set[str]:
    """Every string constant in a cell, read out of its AST rather than off its text."""
    return {
        node.value
        for node in ast.walk(ast.parse(code))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
