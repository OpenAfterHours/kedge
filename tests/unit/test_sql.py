"""Rendering statements a person will run against production.

kedge issues no SQL, so nothing here is about execution. It is about the statement being
*correct* when somebody pastes it into their own client -- and the ways a concatenated statement
goes wrong are not exotic. A name with an apostrophe, a null, a date, a money value large enough
to reach exponent notation: every one of those is an ordinary value in an ordinary finance
extract, and every one produces a statement that will not parse or, worse, one that parses and
means something else.

The eval workbook carries a real instance of the first: ``Adjustment!G`` builds its ``UPDATE``
with ``&`` and produces invalid SQL for ``O'Brien & Partners``. These tests pin the behaviour
that fixes it.
"""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal

import pytest

from kedge.sql import (
    MAX_STATEMENTS,
    SqlRenderError,
    literal,
    placeholders,
    render,
    render_all,
    script,
)

# ── literals ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "NULL"),
        (True, "TRUE"),
        (False, "FALSE"),
        (42, "42"),
        (-7, "-7"),
        (1.5, "1.5"),
        (Decimal("1.50"), "1.50"),
        ("plain", "'plain'"),
        (dt.date(2026, 6, 30), "DATE '2026-06-30'"),
        (dt.time(9, 30), "TIME '09:30:00'"),
        (dt.datetime(2026, 6, 30, 9, 30), "TIMESTAMP '2026-06-30 09:30:00'"),
    ],
)
def test_each_type_has_its_literal_form(value: object, expected: str) -> None:
    assert literal(value) == expected


def test_an_apostrophe_is_doubled_rather_than_escaped_with_a_backslash() -> None:
    """The ANSI form, and the one the eval workbook's own SQL gets wrong."""
    assert literal("O'Brien & Partners") == "'O''Brien & Partners'"


def test_a_value_that_is_nothing_but_quotes_still_balances() -> None:
    assert literal("'''").count("'") % 2 == 0


def test_a_bool_is_not_rendered_as_a_number() -> None:
    """`bool` subclasses `int`, so the order of the isinstance checks is load-bearing."""
    assert literal(True) == "TRUE"


def test_a_large_float_does_not_reach_exponent_notation() -> None:
    """`repr(1.5e16)` is '1.5e+16', which several engines reject inside a literal.

    The threshold sits inside the range of a plausible notional, so this is not hypothetical.
    """
    rendered = literal(1.5e16)

    assert "e" not in rendered.lower()
    assert rendered == "15000000000000000"


def test_a_decimal_keeps_the_scale_that_says_pence() -> None:
    """`Decimal('1.50')` is a statement about precision; float formatting discards it."""
    assert literal(Decimal("1.50")) == "1.50"
    assert literal(Decimal("0.001")) == "0.001"


@pytest.mark.parametrize("value", [float("nan"), math.inf, -math.inf])
def test_a_non_finite_number_is_refused_rather_than_guessed_at(value: float) -> None:
    with pytest.raises(SqlRenderError, match="non-finite"):
        literal(value)


def test_an_integer_beyond_bigint_is_refused() -> None:
    with pytest.raises(SqlRenderError, match="64-bit"):
        literal(2**64)


def test_a_nested_value_is_refused_with_the_remedy_named() -> None:
    with pytest.raises(SqlRenderError, match="Flatten or serialise"):
        literal([1, 2, 3])


# ── templates ────────────────────────────────────────────────────────────────


def test_placeholders_are_returned_in_first_appearance_order() -> None:
    template = "UPDATE t SET a = {amount}, b = {note} WHERE id = {id} AND x = {amount}"

    assert placeholders(template) == ("amount", "note", "id")


def test_format_spec_syntax_is_not_honoured() -> None:
    """`str.format` would evaluate `{0}`, `{a.b}`, `{x!r}` and `{y:>10}`. This must not.

    Each of those is a way for a template to reach something that is not a column value, so the
    narrow form is matched and anything else is left alone rather than interpreted.
    """
    assert placeholders("SET x = {0} AND y = {a.b} AND z = {c!r} AND w = {d:>10}") == ()


def test_rendering_substitutes_literals_not_raw_text() -> None:
    rendered = render(
        "UPDATE t SET amount = {amount}, note = {note} WHERE id = {id}",
        {"amount": 1.5, "note": "O'Brien", "id": None},
    )

    assert rendered == "UPDATE t SET amount = 1.5, note = 'O''Brien' WHERE id = NULL"


def test_an_unknown_placeholder_names_the_columns_that_do_exist() -> None:
    with pytest.raises(SqlRenderError, match="Available: amount, id"):
        render("SET x = {typo}", {"amount": 1, "id": "A"})


def test_render_all_produces_one_statement_per_row() -> None:
    rows = [{"id": "A", "x": 1}, {"id": "B", "x": 2}]

    assert render_all(rows, "UPDATE t SET x = {x} WHERE id = {id}") == [
        "UPDATE t SET x = 1 WHERE id = 'A'",
        "UPDATE t SET x = 2 WHERE id = 'B'",
    ]


def test_render_all_refuses_more_statements_than_anyone_will_read() -> None:
    """The cap is about reviewability, and the message says so rather than just naming a number."""
    rows = [{"id": index} for index in range(5)]

    with pytest.raises(SqlRenderError, match="review the SQL before running it"):
        render_all(rows, "DELETE FROM t WHERE id = {id}", limit=3)


def test_the_default_cap_is_the_documented_one() -> None:
    assert MAX_STATEMENTS == 5_000


# ── scripts ──────────────────────────────────────────────────────────────────


def test_a_script_terminates_every_statement_once() -> None:
    assert script(["SELECT 1", "SELECT 2;"]) == "SELECT 1;\nSELECT 2;"


def test_a_header_becomes_comment_lines() -> None:
    rendered = script(["SELECT 1"], header="Run against RiskWarehouse\n40 rows")

    assert rendered.splitlines()[:2] == ["-- Run against RiskWarehouse", "-- 40 rows"]


def test_no_statements_yields_nothing_rather_than_a_lone_header() -> None:
    """A header over an empty script reads as "here is your SQL" when there is none."""
    assert script([], header="Run against RiskWarehouse") == ""
