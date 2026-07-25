"""The system prompt must actually carry the things PLAN M4 says it carries.

A prompt is the one artifact in this project with no compiler behind it, so these tests are the
compiler. They assert on *content*, not on wording — that the 15-significant-digit rounding rule
is stated with its worked example, that the single-definition rule and its underscore escape hatch
are both present, that pandas is banned rather than merely discouraged, and that the §2.7 mappings
are framed as a vocabulary rather than a template. Each of those is a thing whose absence would
cost a real conversion turn and would otherwise be noticed by nobody.
"""

from __future__ import annotations

import re

from kedge.agent.prompts import PROMPTS_DIR, SYSTEM_PARTS, build_system_prompt, load_prompt

_EMOJI = re.compile("[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f000-\U0001f0ff⬀-⯿]")


def test_every_declared_part_exists_on_disk() -> None:
    for name in SYSTEM_PARTS:
        assert (PROMPTS_DIR / name).is_file(), f"{name} is declared in SYSTEM_PARTS but missing"


def test_a_missing_part_degrades_to_empty_rather_than_raising() -> None:
    assert load_prompt("no-such-part.md") == ""
    assert build_system_prompt(parts=("no-such-part.md",)) == ""


def test_system_prompt_states_the_marimo_single_definition_rule_and_the_escape_hatch() -> None:
    prompt = build_system_prompt().lower()
    assert "exactly one cell" in prompt or "exactly one owning cell" in prompt
    assert "import polars as pl" in prompt
    assert "underscore" in prompt
    assert "_scaled" in prompt or "cell-local" in prompt


def test_system_prompt_carries_the_excel_rounding_rule_including_15_significant_digits() -> None:
    excel = load_prompt("excel.md")
    assert "half away from zero" in excel
    assert "15 significant decimal digits" in excel
    # The worked example is the point: without it "15 significant digits" reads as trivia.
    assert "4491760.574999999" in excel
    assert "4491760.58" in excel
    assert "xl.round" in excel


def test_system_prompt_carries_the_whole_excel_semantics_checklist() -> None:
    excel = load_prompt("excel.md")
    for behaviour in (
        "Empty cell in arithmetic",
        "Row-wise addition",
        "Column aggregate",
        "Divide by zero",
        "VLOOKUP(..., FALSE)",
        "VLOOKUP(..., TRUE)",
        "Text-formatted numbers",
        "1900 serial system",
    ):
        assert behaviour in excel, f"the checklist is missing: {behaviour}"
    assert "sum_horizontal" in excel
    assert "join_asof" in excel


def test_system_prompt_states_the_polars_house_rules() -> None:
    polars = load_prompt("polars.md")
    assert "polars, never pandas" in polars
    assert "LazyFrame" in polars
    assert ".collect()" in polars
    assert "calamine" in polars
    assert "Expressions, not loops" in polars


def test_translation_reference_is_framed_as_a_vocabulary_not_a_template() -> None:
    vocabulary = load_prompt("vocabulary.md")
    assert "vocabulary" in vocabulary.lower()
    assert "not a template" in vocabulary.lower()
    for pattern in ("VLOOKUP", "SUMIFS", "INDEX", "SUMPRODUCT", "cum_sum", "shift(1)", "pivot"):
        assert pattern in vocabulary
    # The two patterns that must stop the work rather than be guessed.
    assert "circular" in vocabulary.lower()
    assert "external workbook link" in vocabulary.lower()


def test_tool_prompt_warns_that_results_are_a_slice() -> None:
    tools = load_prompt("tools.md")
    assert "[… N more rows omitted]" in tools
    assert "100 rows" in tools
    assert "32KB" in tools
    assert "delete" in tools.lower() and "confirm" in tools.lower()


def test_prompts_contain_no_emoji() -> None:
    for name in SYSTEM_PARTS:
        text = load_prompt(name)
        assert not _EMOJI.search(text), f"{name} contains an emoji"


def test_extra_blocks_are_appended_after_the_files() -> None:
    prompt = build_system_prompt(parts=("role.md",), extra=("## This workspace\n\nOne sheet.",))
    assert prompt.endswith("One sheet.")
    assert "kedge's conversion copilot" in prompt
