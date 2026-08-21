"""Pattern recognition and description text for one formula at a time.

`tests/corpus/` asserts the analyser over whole fixture workbooks; this file asserts the two
judgements in `kedge.analysis.regions` that are easiest to get wrong on a single formula and
that no fixture currently demonstrates.

Both were found by analysing `evals/fee_billing_run/m11_management_fee_run.xlsx`:

- The `Post` sheet's manual carry -- one `INSERT` statement per row, built with `&` -- came
  back as `arithmetic` with the hint `col("a") * col("b")`, because the fallthrough for a
  formula with no function call looked at the references and never at the operators. The
  equivalent column in `evals/adjustment_signoff` classifies correctly only because it happens
  to use `CONCATENATE`, so the operator form had no coverage at all.
- The `Working!band_bps` column's description advised sorting the lookup table "on Working".
  The table is on `Fee Schedule`; `Working` is merely the sheet holding the formula. An
  approximate-match `VLOOKUP` translates to a `join_asof`, where sorting the wrong frame is
  wrong *silently*, so that sentence was misleading on the highest-value translation risk in
  the eval.
"""

from __future__ import annotations

import pytest

from kedge.analysis.formulas import parse_formula
from kedge.analysis.model import ExcelPattern
from kedge.analysis.regions import TRANSLATION_HINTS, _describe, classify_pattern

KNOWN_SHEETS = frozenset({"Working", "Fee Schedule", "Post"})

# The manual carry, verbatim from evals/fee_billing_run/m11_management_fee_run.xlsx!Post!A4.
POST_STATEMENT = (
    '="INSERT INTO fin.fee_invoice (client_code, period_month, fee_gbp) VALUES (\'"'
    '&Working!$A19&"\', \'"&Working!$E19&"\', "&Working!$U19&");"'
)

# The banded fee lookup, verbatim from the same workbook at Working!I19.
BANDED_LOOKUP = "=VLOOKUP(F19,'Fee Schedule'!$D$5:$F$8,3,TRUE)"


def _classify(formula: str, *, row: int = 19, col: int = 1, names: frozenset[str] = frozenset()):
    parsed = parse_formula(
        formula, row, col, sheet="Working", names=names, known_sheets=KNOWN_SHEETS
    )
    return classify_pattern(parsed, col=col)


def _describe_formula(
    formula: str,
    *,
    row: int = 19,
    col: int = 9,
    sheet: str = "Working",
    names: frozenset[str] = frozenset(),
) -> str:
    parsed = parse_formula(formula, row, col, sheet=sheet, names=names, known_sheets=KNOWN_SHEETS)
    pattern = classify_pattern(parsed, col=col)
    return _describe(parsed, pattern, "column", 84, sheet)


# ══════════════════════════════════════════════════════════════════════════════════════
# A BARE `&` IS TEXT, NOT ARITHMETIC
# ══════════════════════════════════════════════════════════════════════════════════════


def test_the_generated_sql_column_is_text_manipulation_not_arithmetic() -> None:
    """The sharpest thing this eval can lose is that a column builds a statement per row."""
    assert _classify(POST_STATEMENT) is ExcelPattern.TEXT_MANIPULATION


def test_the_generated_sql_column_is_not_handed_a_multiplication_to_translate() -> None:
    """`arithmetic` came with `col("a") * col("b")`, which is not a thing to do to a string."""
    hint = TRANSLATION_HINTS[_classify(POST_STATEMENT)]

    assert hint != TRANSLATION_HINTS[ExcelPattern.ARITHMETIC]
    assert 'col("a") * col("b")' not in hint
    assert ".str." in hint


@pytest.mark.parametrize(
    "formula",
    [
        "=A19&B19",
        '=A19&" - "&B19',
        '="x"&A19',
        "=$A$1&$B$1",  # all-absolute operands: a parameter reference builds a string too
    ],
)
def test_a_top_level_ampersand_classifies_as_text(formula: str) -> None:
    assert _classify(formula) is ExcelPattern.TEXT_MANIPULATION


def test_brackets_round_the_whole_formula_do_not_hide_the_operator() -> None:
    """`=(A19&B19)` has the same top-level operator as `=A19&B19`; the brackets are noise."""
    assert _classify("=(A19&B19)") is ExcelPattern.TEXT_MANIPULATION


@pytest.mark.parametrize(
    "formula",
    [
        '="Total: "&A19*B19',  # `&` binds looser than `*`, so the result is a string
        '=A19&B19&" "&C19*D19',
    ],
)
def test_mixing_concatenation_with_arithmetic_is_decided_by_precedence(formula: str) -> None:
    """The documented decision on the mixed case. `&` at the top level wins, always.

    Excel's `&` binds looser than every arithmetic operator, so a formula carrying one at the
    top level evaluates to text however much arithmetic sits underneath it.
    """
    assert _classify(formula) is ExcelPattern.TEXT_MANIPULATION


@pytest.mark.parametrize(
    ("formula", "expected"),
    [
        ("=A19*B19", ExcelPattern.ARITHMETIC),
        ("=A19+B19-C19", ExcelPattern.ARITHMETIC),
        ('=A19&B19="x"', ExcelPattern.ARITHMETIC),  # a comparison binds looser still: boolean
        ("=(A19&B19)*C19", ExcelPattern.ARITHMETIC),  # grouped, so `*` is the top-level operator
        ('="a"&"b"', ExcelPattern.LITERAL),  # no references at all
        ("=SUM(A19&B19)", ExcelPattern.AGGREGATE),  # the `&` is an argument, not the top level
    ],
)
def test_what_the_ampersand_rule_deliberately_does_not_claim(
    formula: str, expected: ExcelPattern
) -> None:
    assert _classify(formula) is expected


def test_the_prior_row_geometry_still_wins_over_the_operator() -> None:
    """Which row a formula reads is geometry nothing else recovers, so it is tested first."""
    assert _classify("=A18&B19", row=19) is ExcelPattern.PRIOR_ROW


def test_the_description_of_the_generated_sql_column_no_longer_says_arithmetic() -> None:
    description = _describe_formula(POST_STATEMENT, col=1, sheet="Post")

    assert description.startswith("Text manipulation on Post,")
    assert "Arithmetic" not in description


# ══════════════════════════════════════════════════════════════════════════════════════
# THE SORTED-TABLE ADVICE NAMES THE TABLE, NOT THE FORMULA'S SHEET
# ══════════════════════════════════════════════════════════════════════════════════════


def test_the_sorted_table_is_named_by_the_sheet_holding_it() -> None:
    """The regression. `Working` holds the formula; `Fee Schedule` holds the table."""
    description = _describe_formula(BANDED_LOOKUP)

    assert "sorted ascending by its first column" in description
    assert "the lookup table on Fee Schedule is sorted" in description
    assert "sorted on Working" not in description


def test_the_sorting_advice_is_its_own_sentence() -> None:
    """It hung off the headline, directly in front of the generic `on {sheet}` suffix.

    That adjacency is what produced "sorted on Working" in the first place, so the headline is
    a plain noun phrase now and the caveat follows the full stop.
    """
    description = _describe_formula(BANDED_LOOKUP)
    headline, _, caveat = description.partition(". ")

    assert headline == (
        "An approximate-match lookup into a reference table on Working, filled down 84 rows, "
        "reading from Fee Schedule"
    )
    assert caveat.startswith("Approximate matching assumes")


def test_a_lookup_table_on_the_formulas_own_sheet_names_that_sheet() -> None:
    """An unqualified range genuinely is local, and saying so is correct rather than lucky."""
    description = _describe_formula("=VLOOKUP(F19,$D$5:$F$8,3,TRUE)")

    assert "the lookup table on Working is sorted" in description


@pytest.mark.parametrize(
    ("formula", "names", "why"),
    [
        ("=VLOOKUP(F19,Bands,3,TRUE)", frozenset({"BANDS"}), "a defined name"),
        ("=VLOOKUP(F19,'Gone'!$D$5:$F$8,3,TRUE)", frozenset(), "a sheet the workbook lacks"),
        ("=VLOOKUP(F19,'[1]Other'!$D$5:$F$8,3,TRUE)", frozenset(), "another workbook"),
    ],
)
def test_an_unresolvable_lookup_range_names_no_sheet_at_all(
    formula: str, names: frozenset[str], why: str
) -> None:
    """Naming the wrong sheet is worse than naming none, which is the whole defect."""
    description = _describe_formula(formula, names=names)

    assert "Approximate matching assumes the lookup table is sorted ascending" in description
    assert "the lookup table on" not in description, why


def test_an_hlookup_is_sorted_by_its_first_row() -> None:
    """`HLOOKUP` shares the pattern with `VLOOKUP` and transposes the advice with it."""
    description = _describe_formula("=HLOOKUP(F19,'Fee Schedule'!$D$5:$F$8,3,TRUE)")

    assert "on Fee Schedule is sorted ascending by its first row" in description


def test_a_three_argument_vlookup_still_gets_the_advice() -> None:
    """A missing fourth argument means approximate matching, which is the trap, not an excuse."""
    description = _describe_formula("=VLOOKUP(F19,'Fee Schedule'!$D$5:$F$8,3)")

    assert "the lookup table on Fee Schedule is sorted" in description


def test_an_exact_match_lookup_carries_no_sorting_advice() -> None:
    """`FALSE` needs no sorted table, and a caveat that is always there is one nobody reads."""
    description = _describe_formula("=VLOOKUP(F19,'Fee Schedule'!$D$5:$F$8,3,FALSE)")

    assert "sorted" not in description
    assert description.startswith("An exact-match lookup into a reference table on Working,")
