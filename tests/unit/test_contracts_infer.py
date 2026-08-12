"""Drafting a contract from a hand-in that actually exists.

Two promises the module makes, and both are load-bearing enough to be asserted directly.

**The draft passes against the file it came from.** The module docstring says there is a
test that asserts exactly that; this is it. Without it the starting point is red, and the
user cannot tell a real change in the data from an artefact of generation.

**The commented YAML is still YAML.** The comments are the whole point of hand-writing the
emitter -- an inferred bound with no note beside it is indistinguishable from a considered
one -- but a draft that will not load back is worse than no draft, so the round trip through
`load_contract` has to land on an equal contract.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest
import yaml

from kedge.contracts.infer import ROW_COUNT_SLACK, infer, infer_with_notes, to_yaml, write_yaml
from kedge.contracts.model import ChecksumMode, ColumnContract, Contract, load_contract
from kedge.contracts.validate import validate, validate_path
from kedge.errors import ContractError
from kedge.ingest.drift import HandInReadError
from kedge.ingest.model import HandIn
from kedge.ingest.store import hash_file

STAMP = datetime(2026, 7, 25, 8, 14, 22, tzinfo=UTC)

RATINGS = ["A", "BBB", "BB"]


def _exposures(path: Path, *, rows: int = 40) -> Path:
    """A hand-in with a key, a numeric measure, a low-cardinality category and a nullable."""
    lines = ["counterparty,EAD,rating,comment"]
    for index in range(rows):
        comment = "" if index % 2 else "reviewed"
        lines.append(f"cp{index:03d},{(index + 1) * 1000}.5,{RATINGS[index % 3]},{comment}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _handin(path: Path) -> HandIn:
    return HandIn(
        path=path,
        original_name=path.name,
        sha256=hash_file(path),
        size_bytes=path.stat().st_size,
        received_at=STAMP,
        source="selected",
    )


# ── the draft ───────────────────────────────────────────────────────────────


def test_a_draft_describes_the_columns_that_are_actually_there(tmp_path: Path) -> None:
    contract = infer(_exposures(tmp_path / "exposures.csv"))

    assert contract.column_names == ["counterparty", "EAD", "rating", "comment"]
    assert contract.column("EAD").dtype == "Float64"  # type: ignore[union-attr]
    assert contract.column("counterparty").dtype == "String"  # type: ignore[union-attr]


def test_a_draft_names_itself_after_the_file_unless_told_otherwise(tmp_path: Path) -> None:
    path = _exposures(tmp_path / "2026 Exposures (final).csv")

    assert infer(path).name == "2026_exposures__final"
    assert infer(path, name="exposures").name == "exposures"


def test_a_draft_records_what_it_was_drafted_from(tmp_path: Path) -> None:
    path = _exposures(tmp_path / "exposures.csv")

    contract = infer(path)

    assert contract.generated_from == "exposures.csv"
    assert contract.generated_at is not None
    assert contract.checksum == hash_file(path)
    assert contract.checksum_mode is ChecksumMode.WARN
    assert "Review every guess" in (contract.description or "")


def test_drafting_from_a_hand_in_records_the_receipts_digest(tmp_path: Path) -> None:
    handin = _handin(_exposures(tmp_path / "exposures.csv"))

    contract = infer(handin)

    assert contract.checksum == handin.sha256
    assert contract.generated_from == "exposures.csv"


def test_a_column_with_no_nulls_is_drafted_not_null_and_one_with_nulls_is_not(
    tmp_path: Path,
) -> None:
    contract = infer(_exposures(tmp_path / "exposures.csv"))

    assert contract.column("counterparty").nullable is False  # type: ignore[union-attr]
    assert contract.column("comment").nullable is True  # type: ignore[union-attr]


def test_a_distinct_column_is_drafted_unique_and_becomes_the_key(tmp_path: Path) -> None:
    contract = infer(_exposures(tmp_path / "exposures.csv"))

    assert contract.column("counterparty").unique is True  # type: ignore[union-attr]
    assert contract.column("rating").unique is False  # type: ignore[union-attr]
    assert contract.unique_key == ["counterparty"]


def test_a_low_cardinality_text_column_gets_a_suggested_permitted_set(tmp_path: Path) -> None:
    contract = infer(_exposures(tmp_path / "exposures.csv"))

    assert contract.column("rating").allowed_values == sorted(RATINGS)  # type: ignore[union-attr]


def test_below_twenty_rows_distinct_values_are_a_coincidence_not_a_category(
    tmp_path: Path,
) -> None:
    contract = infer(_exposures(tmp_path / "exposures.csv", rows=10))

    assert contract.column("rating").allowed_values is None  # type: ignore[union-attr]


def test_the_row_bounds_sit_either_side_of_what_was_observed(tmp_path: Path) -> None:
    contract = infer(_exposures(tmp_path / "exposures.csv", rows=40))

    assert contract.min_rows == int(40 * (1 - ROW_COUNT_SLACK))
    assert contract.max_rows == int(40 * (1 + ROW_COUNT_SLACK)) + 1


def test_no_numeric_bounds_are_drafted_because_one_month_is_not_a_business_rule(
    tmp_path: Path,
) -> None:
    contract = infer(_exposures(tmp_path / "exposures.csv"))

    assert contract.column("EAD").min_value is None  # type: ignore[union-attr]
    assert contract.column("EAD").max_value is None  # type: ignore[union-attr]


def test_a_draft_permits_extra_columns_and_does_not_pin_their_order(tmp_path: Path) -> None:
    contract = infer(_exposures(tmp_path / "exposures.csv"))

    assert contract.allow_extra_columns
    assert not contract.require_column_order


def test_the_header_row_is_left_to_detection_so_a_new_preamble_row_is_absorbed(
    tmp_path: Path,
) -> None:
    contract = infer(_exposures(tmp_path / "exposures.csv"))

    assert contract.header_row is None


def test_a_draft_reads_through_the_same_data_view_validation_uses(tmp_path: Path) -> None:
    """If inference profiled 400 rows and validation checked 401, the draft would fail
    against the very file it was generated from."""
    path = tmp_path / "exposures.csv"
    path.write_text(
        "Monthly exposures extract\n"
        "counterparty,EAD\n"
        + "".join(f"cp{i:03d},{(i + 1) * 1000}.0\n" for i in range(40))
        + "Total,820000.0\n",
        encoding="utf-8",
    )

    contract = infer(path)

    assert contract.column_names == ["counterparty", "EAD"]
    assert contract.min_rows == 20
    assert validate_path(path, contract).ok


# ── the commentary ──────────────────────────────────────────────────────────


def test_the_notes_say_what_each_guess_was_derived_from(tmp_path: Path) -> None:
    _contract, notes = infer_with_notes(_exposures(tmp_path / "exposures.csv"))

    assert any("no nulls in 40 rows" in note for note in notes["counterparty"])
    assert any("every value distinct" in note for note in notes["counterparty"])
    assert any("only 3 distinct values seen" in note for note in notes["rating"])
    # A guess that is very likely wrong still says so rather than being emitted bare.
    assert any("delete allowed_values" in note for note in notes["comment"])


def test_a_text_column_full_of_numbers_is_called_out_as_the_join_failure_it_causes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "exposures.csv"
    path.write_text(
        "counterparty,account\n" + "".join(f'cp{i:03d},"1,00{i}"\n' for i in range(5)),
        encoding="utf-8",
    )

    _contract, notes = infer_with_notes(path)

    assert any("numbers stored as text" in note for note in notes["account"])
    assert any("PLAN 2.6" in note for note in notes["account"])


# ── the round trip that matters most ────────────────────────────────────────


def test_a_draft_validates_the_hand_in_it_was_drafted_from(tmp_path: Path) -> None:
    """The starting point is always green, so every later failure is a real change."""
    handin = _handin(_exposures(tmp_path / "exposures.csv"))

    report = validate(handin, infer(handin))

    assert report.ok, report.render_text()


def test_a_draft_of_an_awkward_hand_in_still_validates_it(tmp_path: Path) -> None:
    path = tmp_path / "awkward.csv"
    path.write_text(
        "Extract run 2026-07-25\n"
        "id,value,note\n"
        "1,1.0,\n2,,alpha\n3,3.0,beta\n4,4.0,\n5,5.0,gamma\n",
        encoding="utf-8",
    )

    assert validate_path(path, infer(path)).ok


def test_the_commented_draft_reloads_to_an_equal_contract(tmp_path: Path) -> None:
    contract = infer(_exposures(tmp_path / "exposures.csv"))
    path = tmp_path / "contracts" / "exposures.yaml"

    assert write_yaml(contract, path, notes={}) == path
    assert load_contract(path) == contract


def test_the_reloaded_draft_still_validates_the_hand_in(tmp_path: Path) -> None:
    handin = _handin(_exposures(tmp_path / "exposures.csv"))
    contract, notes = infer_with_notes(handin)
    path = write_yaml(contract, tmp_path / "exposures.yaml", notes=notes)

    assert validate(handin, load_contract(path)).ok


def test_the_notes_survive_into_the_file_as_comments(tmp_path: Path) -> None:
    contract, notes = infer_with_notes(_exposures(tmp_path / "exposures.csv"))

    text = to_yaml(contract, notes=notes)

    assert "# GUESS: drawn from one sample of rows" in text
    assert "    # no nulls in 40 rows, so drafted as not-null" in text
    assert "# No bounds drafted: one month's minimum is not a business rule." in text


def test_a_contract_with_no_key_says_so_rather_than_omitting_the_field(tmp_path: Path) -> None:
    path = tmp_path / "repeats.csv"
    path.write_text("rating\nA\nA\nBBB\n", encoding="utf-8")
    contract = infer(path)

    text = to_yaml(contract)

    assert contract.unique_key == []
    assert "unique_key: []" in text
    assert "composite key" in text
    assert yaml.safe_load(text)["unique_key"] == []


def test_a_contract_with_no_columns_still_emits_yaml_that_reloads(tmp_path: Path) -> None:
    """A bare `columns:` key parses back as null rather than as an empty list."""
    path = write_yaml(Contract(name="rows-only", min_rows=1), tmp_path / "rows-only.yaml")

    assert load_contract(path) == Contract(name="rows-only", min_rows=1)


def test_a_name_carrying_a_quote_survives_the_emitter(tmp_path: Path) -> None:
    contract = Contract(
        name='the "monthly" extract',
        columns=[ColumnContract(name="EAD \\ GBP", dtype="Float64")],
    )
    path = write_yaml(contract, tmp_path / "quoted.yaml")

    assert load_contract(path) == contract


def test_the_emitted_yaml_is_parseable_by_a_plain_yaml_loader(tmp_path: Path) -> None:
    contract, notes = infer_with_notes(_exposures(tmp_path / "exposures.csv"))

    raw = yaml.safe_load(to_yaml(contract, notes=notes))

    assert raw["name"] == "exposures"
    assert raw["checksum_mode"] == "warn"
    assert [c["name"] for c in raw["columns"]] == contract.column_names


# ── degrading gracefully ────────────────────────────────────────────────────


def test_a_file_with_no_columns_says_no_contract_can_be_drafted_from_it(tmp_path: Path) -> None:
    path = tmp_path / "exposures.parquet"
    pl.DataFrame().write_parquet(path)

    with pytest.raises(ContractError, match="no columns to describe"):
        infer(path)


def test_a_file_that_is_not_a_spreadsheet_raises_the_readers_error(tmp_path: Path) -> None:
    path = tmp_path / "handover.pdf"
    path.write_bytes(b"%PDF-1.4")

    with pytest.raises(HandInReadError):
        infer(path)


def test_an_empty_file_raises_the_readers_error(tmp_path: Path) -> None:
    path = tmp_path / "exposures.csv"
    path.write_bytes(b"")

    with pytest.raises(HandInReadError):
        infer(path)


def test_a_draft_that_cannot_be_written_is_a_contract_error(tmp_path: Path) -> None:
    blocked = tmp_path / "exposures.yaml"
    blocked.mkdir()

    with pytest.raises(ContractError, match="could not write the contract draft"):
        write_yaml(Contract(name="exposures"), blocked)


def test_writing_a_draft_creates_the_directory_it_needs(tmp_path: Path) -> None:
    path = write_yaml(Contract(name="exposures"), tmp_path / "a" / "b" / "exposures.yaml")

    assert path.is_file()
