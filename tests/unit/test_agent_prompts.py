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
from kedge.agent.tools import tool_names
from kedge.plan.propose import load_prompt as plan_prompt

_EMOJI = re.compile("[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f000-\U0001f0ff⬀-⯿]")
_SEPARATOR = re.compile(r"^:?-{3,}:?$")


def _tables(text: str) -> list[list[list[str]]]:
    """Split a markdown document into its tables, each a list of rows of cells."""
    tables: list[list[list[str]]] = []
    rows: list[list[str]] = []
    for line in text.splitlines():
        if line.startswith("|"):
            rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
            continue
        if rows:
            tables.append(rows)
            rows = []
    if rows:
        tables.append(rows)
    return tables


def test_every_declared_part_exists_on_disk() -> None:
    for name in SYSTEM_PARTS:
        assert (PROMPTS_DIR / name).is_file(), f"{name} is declared in SYSTEM_PARTS but missing"


def test_a_missing_part_degrades_to_empty_rather_than_raising() -> None:
    assert load_prompt("no-such-part.md") == ""
    assert build_system_prompt(parts=("no-such-part.md",)) == ""


def test_role_prompt_tells_the_model_to_show_the_query_behind_a_paste() -> None:
    """A pasted query result is the other non-formula input, and this one is recoverable.

    No scaffolder can place this cell: nothing in the code associates a connection with the range
    it fills, so the instruction is the entire mechanism. Three parts make it worth a cell — the
    query verbatim, the profile that says whether an export matches, and the command that drafts a
    contract from the pasted sheet *before* the first export — and the fourth assertion is the
    emission idiom, because a query interpolated through an f-string is either rejected or
    silently rewritten.

    `sketch` rather than `infer` is the whole point of naming a command here: `infer` needs a
    hand-in, and the hand-in is the thing the user is trying to produce.
    """
    role = load_prompt("role.md")
    assert 'inspect_workbook(section="connections")' in role
    assert "m_source" in role
    assert "kedge contract sketch" in role
    assert 'r"""' in role and "f-string" in role
    # And none of it may become licence to write a query the workbook does not record.
    assert "never compose the query" in role


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


def test_every_tool_the_model_is_offered_is_described_in_the_prompt() -> None:
    """A tool the schemas offer and the prompt never mentions is one nobody chose to add."""
    tools = load_prompt("tools.md")
    for name in tool_names():
        assert f"`{name}`" in tools, f"{name} is on the tool surface but absent from tools.md"


def test_the_tool_tables_are_tables_rather_than_something_that_looks_like_one() -> None:
    """Markdown fails silently, and a broken table is read by the model as a broken table.

    Inserting a paragraph between two rows ends the table there and starts a second, headerless
    one — so the rows below it lose the column meanings entirely. That is invisible in the source,
    costs nothing to assert, and happened the first time `propose_plan` was documented.
    """
    tables = _tables(load_prompt("tools.md"))
    assert tables, "tools.md has no tables at all"
    for table in tables:
        assert len(table) >= 3, f"{table[0]} begins a run of rows too short to be a table"
        header, separator, *body = table
        assert all(_SEPARATOR.match(cell) for cell in separator), (
            f"the table headed {header} has no separator row, so it is not a table"
        )
        assert body, f"the table headed {header} has a header and no rows"
        assert all(len(row) == len(header) for row in table), (
            f"the table headed {header} has rows of differing widths"
        )

    documented = {row[0].strip("`") for table in tables for row in table[2:]}
    assert set(tool_names()) <= documented, (
        f"not described in a tools.md table: {sorted(set(tool_names()) - documented)}"
    )


def test_prompts_contain_no_emoji() -> None:
    for name in SYSTEM_PARTS:
        text = load_prompt(name)
        assert not _EMOJI.search(text), f"{name} contains an emoji"


def test_extra_blocks_are_appended_after_the_files() -> None:
    prompt = build_system_prompt(parts=("role.md",), extra=("## This workspace\n\nOne sheet.",))
    assert prompt.endswith("One sheet.")
    assert "kedge's conversion copilot" in prompt


# ── the vocabularies the model is given must be the vocabularies the code has ────────────────
#
# Every one of these tables is hand-written prose duplicating an enum, and prose does not go red
# when an enum gains a member. `StageKind.HANDOFF` was added and `propose_system.md` went on
# listing four kinds for the whole of one release, which meant the model could not propose a
# hand-off because it had never been told the word existed -- and no test noticed. These are the
# same idea as `test_every_tool_the_model_is_offered_is_described_in_the_prompt`, applied to the
# enums instead of the tool list.


def test_every_stage_kind_is_offered_to_the_planner() -> None:
    from kedge.plan.model import StageKind

    prompt = plan_prompt("propose_system.md")

    missing = [kind.value for kind in StageKind if f"`{kind.value}`" not in prompt]
    assert not missing, (
        f"stage kinds the model is never told about: {missing}. It cannot propose what it has "
        f"not been given a word for."
    )


def test_every_source_origin_is_offered_to_the_planner() -> None:
    from kedge.plan.model import SourceOrigin

    prompt = plan_prompt("propose_system.md")

    missing = [origin.value for origin in SourceOrigin if f"`{origin.value}`" not in prompt]
    assert not missing, f"source origins the model is never told about: {missing}"


def test_every_excel_pattern_is_in_the_translation_vocabulary() -> None:
    from kedge.analysis.model import ExcelPattern

    prompt = plan_prompt("propose_vocabulary.md")

    missing = [pattern.value for pattern in ExcelPattern if f"`{pattern.value}`" not in prompt]
    assert not missing, (
        f"Excel patterns with no entry in the vocabulary: {missing}. A pattern the model cannot "
        f"name is one it will improvise around instead of raising an open question."
    )


def test_the_planner_is_told_a_mutating_handoff_needs_confirming() -> None:
    """The field that decides whether a re-extract box can appear before the update ran."""
    prompt = plan_prompt("propose_system.md")

    assert "mutates" in prompt
    assert "UPDATE" in prompt


def test_the_prompts_do_not_still_claim_text_columns_are_handled_at_load() -> None:
    """The line that made the typing bug invisible for a whole release.

    Both vocabularies said text-formatted numbers were dealt with because "dtypes are profiled at
    load". Profiling is not converting, and a grid pasted out of Excel arrives as text every
    time. The reader now converts and reports; the prompts have to say what actually happens.
    """
    for name, read in (("propose_vocabulary.md", plan_prompt), ("excel.md", load_prompt)):
        prompt = read(name)
        assert "profiled at load" not in prompt.lower(), name
        assert "read_data" in prompt, name


def test_the_agent_is_told_to_render_sql_rather_than_concatenate_it() -> None:
    prompt = load_prompt("role.md")

    assert "kedge.sql" in prompt
    assert "kedge.reconcile.verify" in prompt
    assert "kedge.runs" in prompt


def test_the_planner_is_told_to_capture_the_workbook_s_own_documentation() -> None:
    """And told not to invent it, which is the half that matters in a finance notebook."""
    prompt = plan_prompt("propose_system.md")

    assert "briefing" in prompt
    assert "sources" in prompt
    assert "leave the fields empty" in prompt
