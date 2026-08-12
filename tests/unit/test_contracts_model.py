"""The contract file, and the report validating against one produces.

The split of serialisation strategies is the thing worth pinning. A contract is
hand-edited, so it is pydantic with ``extra="forbid"``: a typo has to become an error naming
the key, not a setting that silently does nothing. A report is rendered rather than parsed,
so it is a frozen dataclass carrying only plain data -- strings, ints, booleans -- and these
tests assert that literally, because the same object has to render as a `mo.ui.table`, as a
rich table, and as JSON in an audit bundle with no widget library anywhere near it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from kedge.contracts.model import (
    CONTRACT_SCHEMA_VERSION,
    CheckResult,
    CheckStatus,
    ChecksumMode,
    ColumnContract,
    Contract,
    ContractFileError,
    ContractViolationError,
    ValidationReport,
    load_contract,
    save_contract,
)
from kedge.errors import ContractError

VALIDATED_AT = datetime(2026, 7, 25, 8, 14, 22, tzinfo=UTC)


def _contract(**overrides: object) -> Contract:
    fields: dict[str, object] = {
        "name": "exposures",
        "columns": [
            ColumnContract(name="counterparty", dtype="String", nullable=False, unique=True),
            ColumnContract(name="EAD", dtype="Float64", min_value=0.0),
        ],
        "min_rows": 1,
        "max_rows": 1_000,
        "unique_key": ["counterparty"],
    }
    fields.update(overrides)
    return Contract(**fields)  # type: ignore[arg-type]


def _report(*checks: CheckResult) -> ValidationReport:
    return ValidationReport(
        contract_name="exposures",
        handin_name="exposures.xlsx",
        handin_sha256="4f2b91c8ad03" + "0" * 52,
        validated_at=VALIDATED_AT,
        checks=checks,
    )


def _check(status: CheckStatus, name: str = "dtype", **overrides: object) -> CheckResult:
    fields: dict[str, object] = {
        "check": name,
        "status": status,
        "message": f"{name} was {status.value}",
    }
    fields.update(overrides)
    return CheckResult(**fields)  # type: ignore[arg-type]


# ── the contract model ──────────────────────────────────────────────────────


def test_a_contract_defaults_to_the_current_schema_version() -> None:
    assert Contract(name="exposures").schema_version == CONTRACT_SCHEMA_VERSION


def test_a_contract_is_frozen_so_a_validated_one_cannot_drift_under_a_caller() -> None:
    contract = _contract()

    with pytest.raises(ValueError, match="frozen"):
        contract.name = "something else"  # type: ignore[misc]


def test_the_permissive_defaults_are_the_stated_ones() -> None:
    """A column the sending team adds is usually harmless; one they remove is not."""
    contract = Contract(name="exposures")

    assert contract.allow_extra_columns
    assert not contract.require_column_order
    assert contract.checksum_mode is ChecksumMode.WARN


def test_a_column_contract_is_required_and_nullable_unless_told_otherwise() -> None:
    column = ColumnContract(name="EAD", dtype="Float64")

    assert column.required
    assert column.nullable
    assert not column.unique


def test_a_contract_needs_a_name() -> None:
    with pytest.raises(ValueError, match="a contract needs a name"):
        Contract(name="   ")


def test_a_column_contract_needs_a_name() -> None:
    with pytest.raises(ValueError, match="a column contract needs a name"):
        ColumnContract(name="", dtype="Float64")


def test_columns_can_be_listed_and_looked_up_by_name() -> None:
    contract = _contract()

    assert contract.column_names == ["counterparty", "EAD"]
    assert contract.column("EAD") is not None
    assert contract.column("EAD_GBP") is None


def test_the_serialisable_form_omits_the_optional_fields_nobody_set() -> None:
    payload = Contract(name="exposures").to_dict()

    assert payload["name"] == "exposures"
    assert "description" not in payload
    assert "checksum" not in payload


# ── loading a contract from a file ──────────────────────────────────────────


def test_a_contract_round_trips_through_save_and_load(tmp_path: Path) -> None:
    contract = _contract()
    path = tmp_path / "contracts" / "exposures.yaml"

    assert save_contract(contract, path) == path
    assert load_contract(path) == contract


def test_a_contract_file_that_is_not_there_points_at_the_command_that_makes_one(
    tmp_path: Path,
) -> None:
    with pytest.raises(ContractFileError, match="kedge contract infer"):
        load_contract(tmp_path / "absent.yaml")


def test_a_contract_file_that_is_not_valid_yaml_names_the_file(tmp_path: Path) -> None:
    path = tmp_path / "exposures.yaml"
    path.write_text("name: exposures\n  columns: [", encoding="utf-8")

    with pytest.raises(ContractFileError, match="not valid YAML"):
        load_contract(path)


def test_a_contract_file_that_is_not_a_mapping_says_what_it_found_instead(tmp_path: Path) -> None:
    path = tmp_path / "exposures.yaml"
    path.write_text("- counterparty\n- EAD\n", encoding="utf-8")

    with pytest.raises(
        ContractFileError, match="expected a YAML mapping at the top level, found list"
    ):
        load_contract(path)


def test_an_empty_contract_file_is_rejected_rather_than_read_as_an_empty_contract(
    tmp_path: Path,
) -> None:
    path = tmp_path / "exposures.yaml"
    path.write_text("", encoding="utf-8")

    with pytest.raises(ContractFileError, match="expected a YAML mapping"):
        load_contract(path)


def test_a_mistyped_key_is_an_error_naming_it_rather_than_a_setting_that_does_nothing(
    tmp_path: Path,
) -> None:
    """This is precisely the case CONVENTIONS reserves pydantic for."""
    path = tmp_path / "exposures.yaml"
    path.write_text("name: exposures\nallow_extra_column: false\n", encoding="utf-8")

    with pytest.raises(ContractFileError) as excinfo:
        load_contract(path)

    assert "allow_extra_column" in str(excinfo.value)
    assert "Extra inputs are not permitted" in str(excinfo.value)


def test_a_mistyped_key_inside_a_column_names_the_column_it_is_in(tmp_path: Path) -> None:
    path = tmp_path / "exposures.yaml"
    path.write_text(
        "name: exposures\ncolumns:\n  - name: EAD\n    dtype: Float64\n    nulable: true\n",
        encoding="utf-8",
    )

    with pytest.raises(ContractFileError) as excinfo:
        load_contract(path)

    assert "columns.0.nulable" in str(excinfo.value)


def test_a_negative_row_bound_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "exposures.yaml"
    path.write_text("name: exposures\nmin_rows: -1\n", encoding="utf-8")

    with pytest.raises(ContractFileError, match="min_rows"):
        load_contract(path)


def test_a_contract_that_cannot_be_written_is_a_contract_file_error(tmp_path: Path) -> None:
    blocked = tmp_path / "exposures.yaml"
    blocked.mkdir()

    with pytest.raises(ContractFileError, match="could not write the contract"):
        save_contract(_contract(), blocked)


def test_the_saved_file_is_yaml_a_human_can_edit(tmp_path: Path) -> None:
    path = tmp_path / "exposures.yaml"
    save_contract(_contract(), path)

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert raw["name"] == "exposures"
    assert [c["name"] for c in raw["columns"]] == ["counterparty", "EAD"]


def test_a_contract_file_error_is_a_contract_error() -> None:
    assert issubclass(ContractFileError, ContractError)
    assert issubclass(ContractViolationError, ContractError)


# ── one check ───────────────────────────────────────────────────────────────


def test_only_a_fail_rejects_the_hand_in() -> None:
    assert _check(CheckStatus.FAIL).failed
    assert not _check(CheckStatus.PASS).failed
    assert not _check(CheckStatus.WARN).failed
    assert not _check(CheckStatus.SKIP).failed


def test_a_check_renders_as_plain_data_only() -> None:
    payload = _check(
        CheckStatus.FAIL, "dtype", column="EAD", expected="Float64", actual="String"
    ).to_dict()

    assert payload["status"] == "fail"
    assert payload["column"] == "EAD"
    assert all(value is None or isinstance(value, str) for value in payload.values())


# ── the report ──────────────────────────────────────────────────────────────


def test_a_report_of_passes_and_skips_is_ok() -> None:
    """A check that could not run is a result, not a rejection."""
    report = _report(_check(CheckStatus.PASS), _check(CheckStatus.SKIP))

    assert report.ok
    assert report.skipped and not report.failures


def test_a_warning_does_not_reject_the_hand_in() -> None:
    """A monthly hand-in is a different file every month; a checksum warning is expected."""
    report = _report(_check(CheckStatus.WARN, "checksum"))

    assert report.ok
    assert [c.check for c in report.warnings] == ["checksum"]


def test_one_failure_rejects_the_hand_in() -> None:
    report = _report(_check(CheckStatus.PASS), _check(CheckStatus.FAIL, "not_null"))

    assert not report.ok
    assert [c.check for c in report.failures] == ["not_null"]


def test_the_report_counts_every_outcome_separately_in_its_plain_data_form() -> None:
    report = _report(
        _check(CheckStatus.PASS),
        _check(CheckStatus.PASS, "not_null"),
        _check(CheckStatus.FAIL, "unique"),
        _check(CheckStatus.WARN, "checksum"),
        _check(CheckStatus.SKIP, "value_range"),
    )

    payload = report.to_dict()

    assert payload["ok"] is False
    assert (payload["passed"], payload["failed"], payload["warned"], payload["skipped"]) == (
        3,
        1,
        1,
        1,
    )
    assert payload["checks"] == report.to_rows()
    assert payload["validated_at"] == VALIDATED_AT.isoformat()


def test_the_summary_line_states_the_verdict_and_names_the_file() -> None:
    report = _report(_check(CheckStatus.FAIL, "not_null"))

    line = report.summary_line()

    assert "contract 'exposures' FAILED for exposures.xlsx" in line
    assert "sha256:4f2b91c8ad03" in line
    assert "1 failed" in line


def test_a_passing_report_says_passed_rather_than_saying_nothing() -> None:
    assert "passed" in _report(_check(CheckStatus.PASS)).summary_line()


def test_the_text_rendering_shows_only_what_needs_reading() -> None:
    """A hundred passing checks above the one failure is not a report anybody reads."""
    report = _report(
        _check(CheckStatus.PASS, "columns_present"),
        _check(CheckStatus.FAIL, "dtype", column="EAD", expected="Float64", actual="String"),
    )

    lines = report.render_text().splitlines()

    assert len(lines) == 3
    assert "columns_present" not in report.render_text()
    assert "[EAD]" in lines[1]
    assert lines[2].strip() == "expected Float64, found String"


# ── failing loudly ──────────────────────────────────────────────────────────


def test_raise_for_status_returns_the_report_when_the_hand_in_is_good() -> None:
    report = _report(_check(CheckStatus.PASS))

    assert report.raise_for_status() is report


def test_raise_for_status_stops_the_dataflow_graph_when_the_hand_in_is_wrong() -> None:
    """The loud half of "fails loudly with a structured report" (PLAN M5)."""
    report = _report(_check(CheckStatus.FAIL, "not_null", column="EAD"))

    with pytest.raises(ContractViolationError) as excinfo:
        report.raise_for_status()

    assert excinfo.value.report is report
    assert "not_null" in str(excinfo.value)


def test_the_violation_carries_the_whole_report_not_just_a_message() -> None:
    """A caller that catches this renders the same detail the notebook panel shows."""
    report = _report(
        _check(CheckStatus.FAIL, "dtype", column="EAD", expected="Float64", actual="String"),
        _check(CheckStatus.FAIL, "unique", column="counterparty"),
    )

    with pytest.raises(ContractViolationError) as excinfo:
        report.raise_for_status()

    assert len(excinfo.value.report.failures) == 2
    assert excinfo.value.report.to_dict()["failed"] == 2
