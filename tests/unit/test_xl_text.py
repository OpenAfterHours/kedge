"""The scalar half of Excel's text-to-number coercion, and the line it must not cross.

Two things are asserted here and the second matters more than the first. One: the rule itself
-- what text reads as a number and what text is an identifier that merely looks like one. Two:
that :func:`kedge.xl.unambiguous_number` and :func:`kedge.xl.to_number` never disagree about
the value, because two spellings of "what is a number" that drift apart is the exact defect
:mod:`kedge.xl` exists to prevent, and the drift would show up as a reconciliation that passes
in the notebook and fails in the reader, or the reverse.
"""

from __future__ import annotations

import math

import polars as pl
import pytest

from kedge.xl import (
    MAX_SIGNIFICANT_DIGITS,
    loses_information_as_a_number,
    to_number,
    unambiguous_number,
)

# Every text Excel would coerce at the point of use, and the number it coerces to. The
# grammar is `to_number`'s; the values are what Excel's `=VALUE(...)` and `="x"+0` give.
COERCIBLE: list[tuple[str, float]] = [
    ("20.0", 20.0),  # the eval's tier rate, cached as text by VLOOKUP
    ("417", 417.0),
    ("0", 0.0),
    ("0.5", 0.5),
    ("-0.25", -0.25),
    ("+5", 5.0),
    ("  1,234.56  ", 1234.56),
    ("\xa01,234.56", 1234.56),  # a non-breaking space survives a copy out of a web report
    ("£1,000", 1000.0),
    ("$250,000.00", 250000.0),
    ("(1,234.50)", -1234.5),  # an accounting export's negative
    ("12%", 0.12),
    ("1e3", 1000.0),
    (".5", 0.5),
    ("5.", 5.0),
]

# Digits that spell an identifier rather than a quantity. `to_number` reads every one of
# these -- and is right to, because a translation asking for it has said so explicitly.
# Deciding it *unasked* is what `loses_information_as_a_number` refuses.
LOSES_INFORMATION: list[tuple[str, str]] = [
    ("00417", "a client code: the leading zeros are the identity"),
    ("00123", "a cost centre"),
    ("0123.5", "a leading zero before a significant digit, decimal point or not"),
    ("-0417", "the sign does not make it a quantity"),
    ("1" * (MAX_SIGNIFICANT_DIGITS + 1), "a 16-digit account number does not survive Float64"),
]

# Text that is not a number in either spelling: the grammar refuses it, unasked or not.
NOT_A_NUMBER_AT_ALL: list[tuple[str, str]] = [
    ("n/a", "somebody wrote a note in the column"),
    ("", "an empty cell says nothing"),
    ("   ", "and neither does a cell of spaces"),
    ("2026-11-30", "a date is not a number here"),
    ("(6", "an unmatched parenthesis"),
    ("6)", "and the other one"),
    ("0x10", "not a decimal literal"),
    ("1_000", "Python's float reads this and polars does not; polars wins"),
    ("١٢", "Python's float reads Arabic-Indic digits; nothing else does"),
    ("inf", "no workbook cell holds an infinity"),
    ("Infinity", "nor spelled out"),
    ("nan", "and nothing can be reconciled against a NaN"),
    ("#DIV/0!", "an Excel error is a fact about the workbook, not a number"),
]


@pytest.mark.parametrize(("text", "number"), COERCIBLE)
def test_text_excel_would_coerce_reads_as_the_number_it_spells(text: str, number: float) -> None:
    assert unambiguous_number(text) == pytest.approx(number)


@pytest.mark.parametrize(("text", "why"), LOSES_INFORMATION + NOT_A_NUMBER_AT_ALL)
def test_text_that_would_lose_something_is_not_read_as_a_number(text: str, why: str) -> None:
    assert unambiguous_number(text) is None, why


@pytest.mark.parametrize(("text", "why"), LOSES_INFORMATION)
def test_digits_that_spell_an_identifier_are_named_as_such(text: str, why: str) -> None:
    assert loses_information_as_a_number(text), why


@pytest.mark.parametrize(("text", "_why"), NOT_A_NUMBER_AT_ALL)
def test_text_the_grammar_refuses_is_not_accused_of_being_an_identifier(
    text: str, _why: str
) -> None:
    """The two refusals are separate. ``n/a`` is not a number; it is not a cost centre either."""
    assert not loses_information_as_a_number(text.strip())


def test_a_client_code_is_not_the_number_it_looks_like() -> None:
    """The distinction the whole rule exists for, stated on its own.

    ``00417`` and ``417`` are the same digits and different values. A rule that coerced the
    first would report agreement on a join key that has already broken every join it is in.
    """
    assert unambiguous_number("00417") is None
    assert unambiguous_number("417") == 417.0
    assert loses_information_as_a_number("00417")
    assert not loses_information_as_a_number("417")


def test_nothing_but_a_string_can_spell_a_number() -> None:
    """Asked of an arbitrary cell, so every other type has to answer None rather than raise."""
    for value in (417, 417.0, True, False, None, [417], object()):
        assert unambiguous_number(value) is None


@pytest.mark.parametrize(("text", "number"), COERCIBLE)
def test_the_scalar_rule_and_the_column_expression_agree(text: str, number: float) -> None:
    """The tripwire. One rule, two spellings, and they must never drift apart.

    ``to_number`` is the polars expression a translation calls on a whole column;
    ``unambiguous_number`` is what the comparison engine and the hand-in reader's guard call on
    one cell. A value either of them reads must be the same value.
    """
    column = pl.DataFrame({"v": [text]}).select(to_number(pl.col("v")))["v"].to_list()[0]

    assert column == pytest.approx(number)
    assert unambiguous_number(text) == pytest.approx(column)


@pytest.mark.parametrize("text", [text for text, _why in NOT_A_NUMBER_AT_ALL])
def test_where_the_scalar_refuses_on_grammar_the_expression_produces_nothing_usable(
    text: str,
) -> None:
    """The refusals that are about the *grammar* are refusals in both spellings.

    ``to_number`` answers null for all but the three non-finite spellings, which it parses as
    polars does; ``unambiguous_number`` refuses those too, because no workbook cell holds an
    infinity. Either way nothing usable comes out, which is the property that matters.
    """
    column = pl.DataFrame({"v": [text]}).select(to_number(pl.col("v")))["v"].to_list()[0]

    assert column is None or not math.isfinite(column)
    assert unambiguous_number(text) is None


@pytest.mark.parametrize(("text", "_why"), LOSES_INFORMATION)
def test_the_information_loss_rule_is_the_one_place_the_two_deliberately_differ(
    text: str, _why: str
) -> None:
    """``to_number`` reads ``00417`` as ``417.0``, and is right to.

    A translation that calls it has said explicitly that the column is numeric. The scalar rule
    is the one deciding *unasked*, so it is the one that has to be conservative -- and that
    asymmetry is deliberate, not drift, which is why it is pinned rather than merely excluded
    from the agreement test above.
    """
    column = pl.DataFrame({"v": [text]}).select(to_number(pl.col("v")))["v"].to_list()[0]

    assert column is not None and math.isfinite(column)
    assert unambiguous_number(text) is None
