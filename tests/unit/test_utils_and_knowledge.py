"""The user-editable halves: `utils/` helpers and the `context/` knowledge pack.

These are shipped as worked examples rather than placeholders, so they are tested like real code.
The catalogue and pack tests also pin the shape the agent's `list_utils` and `get_knowledge`
tools depend on.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from kedge.knowledge import build_catalogue, get_knowledge, load_pack, search_tables
from utils.frames import (
    DuplicateKeyError,
    assert_unique_key,
    drop_all_null_columns,
    normalise_headers,
    strip_totals_row,
)
from utils.money import allocate_residual, to_thousands

REPO_ROOT = Path(__file__).parent.parent.parent
CONTEXT_DIR = REPO_ROOT / "context"
UTILS_DIR = REPO_ROOT / "utils"


# ── frames ───────────────────────────────────────────────────────────────────────────────────


def test_normalise_headers_should_snake_case_and_deduplicate() -> None:
    frame = pl.LazyFrame({"  Asset Class ": ["a"], "Amount": [1.0], "Amount ": [2.0]})
    assert normalise_headers(frame).collect_schema().names() == [
        "asset_class",
        "amount",
        "amount_2",  # Not silently shadowing the first.
    ]


def test_normalise_headers_should_survive_a_header_of_only_punctuation() -> None:
    frame = pl.LazyFrame({"???": [1], "!!!": [2]})
    assert normalise_headers(frame).collect_schema().names() == ["column", "column_2"]


def test_drop_all_null_columns_should_keep_columns_with_any_value() -> None:
    frame = pl.LazyFrame({"keep": [None, 1], "drop": [None, None]})
    assert drop_all_null_columns(frame).collect_schema().names() == ["keep"]


def test_strip_totals_row_should_remove_totals_anywhere_not_just_the_bottom() -> None:
    frame = pl.LazyFrame(
        {"asset_class": ["a", "Total", "b", None, "c"], "amount": [1, 99, 2, 88, 3]}
    )
    result = strip_totals_row(frame, key="asset_class").collect()
    assert result["asset_class"].to_list() == ["a", "b", "c"]
    assert result["amount"].sum() == 6  # The mid-sheet total is not double counted.


def test_strip_totals_row_should_accept_extra_markers() -> None:
    frame = pl.LazyFrame({"k": ["a", "Subtotal", "b"], "v": [1, 9, 2]})
    result = strip_totals_row(frame, key="k", markers=("subtotal",)).collect()
    assert result["k"].to_list() == ["a", "b"]


def test_assert_unique_key_should_return_the_frame_when_the_key_is_unique() -> None:
    frame = pl.LazyFrame({"k": [1, 2, 3], "v": ["a", "b", "c"]})
    assert assert_unique_key(frame, "k").collect().height == 3


def test_assert_unique_key_should_raise_naming_the_offending_value() -> None:
    frame = pl.LazyFrame({"k": [1, 1, 1, 2], "v": ["a", "b", "c", "d"]})
    with pytest.raises(DuplicateKeyError, match="not unique"):
        assert_unique_key(frame, "k", label="haircuts")


def test_assert_unique_key_should_handle_a_composite_key() -> None:
    frame = pl.LazyFrame({"a": [1, 1], "b": ["x", "y"]})
    assert_unique_key(frame, ["a", "b"])  # Unique together, though "a" alone is not.
    with pytest.raises(DuplicateKeyError):
        assert_unique_key(frame, "a")


# ── money ────────────────────────────────────────────────────────────────────────────────────


def test_to_thousands_should_round_the_way_excel_does_not_the_way_polars_does() -> None:
    frame = pl.DataFrame({"x": [1500.0, 2500.0]})
    # Excel ROUND is half away from zero: 1.5 -> 2 and 2.5 -> 3.
    # polars' native .round() is banker's, which would give 2 and 2.
    assert frame.with_columns(to_thousands("x").alias("k"))["k"].to_list() == [2.0, 3.0]


def test_allocate_residual_should_make_the_parts_sum_to_the_whole() -> None:
    frame = pl.LazyFrame({"charge": [33.333, 33.333, 33.334]})
    result = allocate_residual(frame, column="charge", total=100.0).collect()
    values = result["charge"].to_list()
    assert round(sum(values), 2) == 100.00
    assert values == [33.33, 33.33, 33.34]


def test_allocate_residual_should_give_the_extra_unit_to_the_largest_remainder() -> None:
    # Floors are 3.33 / 3.33 / 3.33 summing to 9.99; the penny goes to the biggest fraction.
    frame = pl.LazyFrame({"v": [3.331, 3.339, 3.330]})
    values = allocate_residual(frame, column="v", total=10.0).collect()["v"].to_list()
    assert round(sum(values), 2) == 10.00
    assert values[1] == max(values)


def test_allocate_residual_should_write_into_a_new_column_when_asked() -> None:
    frame = pl.LazyFrame({"raw": [1.005, 1.005]})
    result = allocate_residual(frame, column="raw", total=2.01, into="rounded").collect()
    assert "raw" in result.columns
    assert round(result["rounded"].sum(), 2) == 2.01


# ── knowledge pack ───────────────────────────────────────────────────────────────────────────


def test_the_shipped_context_pack_loads() -> None:
    pack = load_pack(CONTEXT_DIR)
    assert {d.key for d in pack.documents} >= {"conventions", "glossary"}
    assert len(pack.tables) >= 2


def test_a_missing_pack_is_empty_rather_than_an_error(tmp_path: Path) -> None:
    # A project that has not written a knowledge pack is not broken.
    pack = load_pack(tmp_path / "nothing_here")
    assert pack.documents == ()
    assert pack.tables == ()


def test_search_ranks_a_name_match_above_a_description_match() -> None:
    pack = load_pack(CONTEXT_DIR)
    hits = search_tables(pack.tables, "haircut")
    assert hits, "the example pack documents a collateral_haircut table"
    assert "haircut" in hits[0].name


def test_the_example_pack_records_grain_and_gotchas() -> None:
    # These are the fields that are nowhere in a schema and that stop a join multiplying rows.
    pack = load_pack(CONTEXT_DIR)
    exposure = next(t for t in pack.tables if t.name == "counterparty_exposure")
    assert exposure.grain
    assert exposure.gotchas


def test_get_knowledge_returns_a_named_document() -> None:
    pack = load_pack(CONTEXT_DIR)
    assert "Glossary" in get_knowledge(pack, "glossary")


# ── utils catalogue ──────────────────────────────────────────────────────────────────────────


def test_the_utils_catalogue_finds_every_shipped_helper() -> None:
    catalogue = build_catalogue(UTILS_DIR, package="utils")
    names = {f.name for f in catalogue.functions}
    assert names >= {
        "normalise_headers",
        "drop_all_null_columns",
        "strip_totals_row",
        "assert_unique_key",
        "to_thousands",
        "allocate_residual",
    }


def test_every_catalogued_function_has_a_summary_line() -> None:
    # The summary is literally what the model sees, so an empty one is a bug in the helper.
    catalogue = build_catalogue(UTILS_DIR, package="utils")
    assert [f.name for f in catalogue.functions if not f.summary] == []
