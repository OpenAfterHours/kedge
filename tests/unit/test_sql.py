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
    changes_data,
    literal,
    placeholders,
    reads_as_sql,
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


# ── is this a statement, or a sentence with a SQL verb in it? ─────────────────
#
# `changes_data` answers what running a statement would do. `reads_as_sql` answers whether it is
# a statement at all, and it exists because one caller does not have one: `kedge.plan.review`
# reads the text an Excel formula column concatenates, looking for the `="UPDATE ... "&F17&"..."`
# that is the step of a manual process that changes the data. Most of what a `text_manipulation`
# column builds is labels and instructions to a colleague, and `changes_data` alone put a warning
# on an approval card for one of those.
#
# The three formulas below are the fixtures that matter: two are real prose cases confirmed to
# have been misread as statements, and the third is the eval workbook's own `Adjustment!G`, which
# has to keep reading as SQL. They are transcribed as the literal text `parse_formula` recovers
# from each formula -- the cell references are gone, which is why the spacing is odd.

WORKBOOK_UPDATE = "UPDATE fin.accruals SET accrual_gbp =   WHERE trade_id = ' '"
"""``Adjustment!G17:G92`` of the adjustment eval workbook, as its string literals concatenate."""

TRACKER_INSERT = "Insert values from   into the tracker table"
"""``="Insert values from "&A1&" into the tracker table"``. Four SQL words, no SQL."""

TRACKER_DELETE = "Delete row   from the tracker"
"""``="Delete row "&A1&" from the tracker"``. Opens with a writing verb; is a sentence."""


def test_the_workbooks_own_generated_update_reads_as_a_statement() -> None:
    """If this ever reads as prose, the check that finds an unhanded-over write goes silent."""
    assert reads_as_sql(WORKBOOK_UPDATE)
    assert changes_data(WORKBOOK_UPDATE)


@pytest.mark.parametrize("prose", [TRACKER_INSERT, TRACKER_DELETE])
def test_a_sentence_containing_sql_words_does_not_read_as_a_statement(prose: str) -> None:
    """Both were confirmed firing a review warning on a correct plan.

    Note the second half of each assertion: `changes_data` still says these write, because it is
    asked what the verb means and the verb is `insert` / `delete`. That is not a bug in it -- it
    is why the question "is this a statement" had to be asked separately rather than by making
    `changes_data` cleverer, which would change the answer for every caller that already has one.
    """
    assert not reads_as_sql(prose)
    assert changes_data(prose), "the verb is real; only the grammar around it is not"


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE fin.accruals SET accrual_gbp = {after} WHERE trade_id = {trade_id}",
        "UPDATE t AS a SET x = 1",
        "INSERT INTO fin.log (trade_id) VALUES ('ACC-1')",
        "DELETE FROM fin.staging WHERE period_end = '2026-06-30'",
        "MERGE INTO target USING source ON target.id = source.id WHEN MATCHED THEN UPDATE SET a=1",
        "TRUNCATE TABLE fin.staging",
        "DROP TABLE fin.staging",
        "CREATE TEMP TABLE scratch AS SELECT * FROM t",
        "CREATE OR REPLACE VIEW v AS SELECT 1 FROM t",
        "SELECT trade_id, last_update FROM fin.accruals FOR UPDATE",
        "WITH scoped AS (SELECT id FROM t) DELETE FROM u WHERE id IN (SELECT id FROM scoped)",
    ],
)
def test_real_statements_read_as_statements(statement: str) -> None:
    assert reads_as_sql(statement)


@pytest.mark.parametrize(
    "prose",
    [
        "Merge the two tabs before using this",
        "Ask the DBA to run the Q2 migration",
        "Q2 uplift for E-04",
        "ACC-00001 / E-04 / 2026-06-30",
        "",
        "   ",
        "-- update the accruals first",
    ],
)
def test_prose_does_not_read_as_a_statement(prose: str) -> None:
    assert not reads_as_sql(prose)


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT fin.log VALUES ('ACC-1')",
        "DELETE fin.staging",
        "SELECT 1",
        "EXEC sp_rebuild_accruals",
        "GRANT SELECT ON fin.accruals TO reporting",
    ],
)
def test_shapes_with_no_rule_read_as_prose_rather_than_being_guessed_at(statement: str) -> None:
    """T-SQL's keyword-less forms, a `SELECT` with no `FROM`, and openers this module has no
    grammar for. Every one is real SQL and every one reads as prose.

    That is the direction to be wrong in and the docstring says so: reading a statement as prose
    costs a warning nobody sees, while reading prose as a statement puts amber on the approval
    card of a correct plan.
    """
    assert not reads_as_sql(statement)


def test_a_statement_that_reads_as_sql_still_has_to_be_asked_whether_it_writes() -> None:
    """The two questions are independent, and the caller asks both."""
    select = "SELECT trade_id FROM fin.accruals"
    assert reads_as_sql(select)
    assert not changes_data(select)


def test_every_top_level_statement_has_to_read_as_one() -> None:
    """A script is only SQL if all of it is. One sentence in the middle means it is not a script."""
    assert reads_as_sql("DELETE FROM t WHERE id = 1; UPDATE u SET x = 2")
    assert not reads_as_sql("DELETE FROM t WHERE id = 1; then tell Ops it is done")


# ── changes_data, pinned ─────────────────────────────────────────────────────
#
# `reads_as_sql` was added beside `changes_data` and shares its scanner, so it is worth having
# something that fails if the older answer moves. `Handoff.statement_writes` and the eval graders
# both read it, and `Handoff.needs_confirmation` turns it into whether a notebook demands a
# confirmation before showing a re-extract box -- so a change here is a change to what a runbook
# makes a user do.


@pytest.mark.parametrize(
    ("statement", "writes"),
    [
        ("UPDATE fin.accruals SET x = {y} WHERE id = {id}", True),
        ("SELECT trade_id, last_update FROM fin.accruals FOR UPDATE", False),
        ("SELECT * FROM t WHERE action = 'update'", False),
        ("-- update the accruals first\nSELECT 1", False),
        ("WITH scoped AS (SELECT id FROM t) DELETE FROM u WHERE id IN (1)", True),
        ("CREATE TEMP TABLE scratch AS SELECT * FROM t", False),
        ("SELECT (SELECT max(id) FROM audit) FROM t", False),
        ("INSERT INTO fin.log (id) VALUES (1)", True),
        ("", False),
    ],
)
def test_changes_data_answers_what_it_always_answered(statement: str, writes: bool) -> None:
    assert changes_data(statement) is writes
