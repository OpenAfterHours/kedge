"""The validation gate, driven with code a model would plausibly write.

Every offending sample here is a real drift rather than a synthetic trigger: a second
``import polars as pl`` because the model did not read the registry, a `.round(2)` at the end of a
haircut calculation, a `requests.get` for an FX rate, an `open(...)` writing the output somewhere
the user will never find it. If the gate does not catch these it does not catch anything that
matters, and a fabricated `os.system("rm -rf /")` would prove nothing about that.

Stage order is asserted directly, because it is the design: a cell that is both syntactically
broken and full of pandas must report the syntax error, since that is the only message the model
can act on.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from kedge.agent.context import CellFacts, NameRegistry
from kedge.agent.validate import (
    MAX_VALIDATION_ATTEMPTS,
    Policy,
    RoundingContext,
    ValidationStage,
    extract_names,
    validate_cell,
    violations_from_conflicts,
    violations_from_kernel_error,
)
from kedge.notebook.driver import MultiplyDefinedError, parse_definition_conflicts
from kedge.notebook.model import DefinitionConflict


@pytest.fixture
def registry() -> NameRegistry:
    """A three-cell notebook part-way through a conversion."""
    return NameRegistry(
        [
            CellFacts(id="AAaa", name="imports", defs=("mo", "pl", "xl")),
            CellFacts(
                id="MJUe",
                name="load_handin",
                defs=("load_handin", "exposure"),
                refs=("pl", "handin"),
            ),
            CellFacts(
                id="Kp2r",
                name="reference_haircuts",
                defs=("reference_haircuts",),
                refs=("pl",),
            ),
        ]
    )


# ── stage 1: syntax ──────────────────────────────────────────────────────────────────────────


def test_a_cell_that_does_not_parse_is_rejected_at_the_syntax_stage(registry: NameRegistry) -> None:
    report = validate_cell(
        'apply_haircuts = load_handin.join(\n    reference_haircuts, left_on="asset_class"\n',
        registry=registry,
    )
    assert not report.ok
    assert report.stage is ValidationStage.SYNTAX
    assert "does not parse" in report.messages[0]


def test_syntax_short_circuits_the_later_stages() -> None:
    report = validate_cell("import pandas as pd\nframe = pd.read_excel(")
    assert report.stage is ValidationStage.SYNTAX
    assert len(report.violations) == 1


# ── stage 2: the marimo contract ─────────────────────────────────────────────────────────────


def test_redefining_a_name_another_cell_owns_is_rejected(registry: NameRegistry) -> None:
    report = validate_cell(
        "import polars as pl\n\n"
        'apply_haircuts = load_handin.join(reference_haircuts, on="asset_class", how="left")\n',
        registry=registry,
    )
    assert not report.ok
    assert report.stage is ValidationStage.MARIMO
    message = "\n".join(report.messages)
    assert "'pl'" in message
    assert "imports" in message and "AAaa" in message
    assert "_pl" in message  # the escape hatch is spelled out, not merely alluded to


def test_an_edit_that_keeps_its_own_names_is_not_a_collision(registry: NameRegistry) -> None:
    report = validate_cell(
        'load_handin = pl.scan_parquet("handin.parquet")\nexposure = 1\n',
        registry=registry,
        cell="load_handin",
    )
    assert report.ok


def test_underscore_names_are_exempt_from_the_single_definition_rule(
    registry: NameRegistry,
) -> None:
    report = validate_cell("_pl = 1\n_scratch = _pl + 1\n", registry=registry)
    assert report.ok


def test_a_wildcard_import_is_rejected(registry: NameRegistry) -> None:
    report = validate_cell("from polars import *\n", registry=registry)
    assert not report.ok
    assert report.stage is ValidationStage.MARIMO
    assert "wildcard import" in report.messages[0]


def test_a_cell_that_would_close_a_cycle_is_rejected() -> None:
    registry = NameRegistry(
        [
            CellFacts(id="A", name="stage_one", defs=("totals",), refs=("adjusted",)),
            CellFacts(id="B", name="stage_two", defs=("adjusted",), refs=("raw",)),
        ]
    )
    report = validate_cell("raw = totals * 2\n", registry=registry)
    assert not report.ok
    assert report.stage is ValidationStage.MARIMO
    assert "cycle" in report.messages[0]


def test_without_a_registry_only_the_wildcard_check_runs() -> None:
    report = validate_cell("import polars as pl\nexposure = 1\n")
    assert report.ok


# ── stage 3: policy ──────────────────────────────────────────────────────────────────────────


def test_shelling_out_is_rejected(registry: NameRegistry) -> None:
    report = validate_cell(
        'import subprocess\n\nrefreshed = subprocess.run(["refresh.bat"], check=True)\n',
        registry=registry,
    )
    assert not report.ok
    assert report.stage is ValidationStage.POLICY
    assert any("subprocess.run" in message for message in report.messages)


def test_a_network_call_outside_the_allowlist_is_rejected(registry: NameRegistry) -> None:
    code = (
        "import requests\n\n"
        'fx_rates = requests.get("https://api.exchange.example.com/latest").json()\n'
    )
    report = validate_cell(code, registry=registry)
    assert not report.ok
    assert report.stage is ValidationStage.POLICY
    assert "api.exchange.example.com" in report.messages[0]


def test_a_network_call_inside_the_allowlist_is_permitted(registry: NameRegistry) -> None:
    code = 'import httpx\n\nfx_rates = httpx.get("https://rates.internal.bank/latest").json()\n'
    report = validate_cell(
        code,
        registry=registry,
        policy=Policy(network_allowlist=frozenset({"internal.bank"})),
    )
    assert report.ok


def test_writing_outside_the_working_directory_is_rejected(
    registry: NameRegistry, tmp_path: Path
) -> None:
    # Absolute, and outside `working_dir`, on whichever platform this runs. Spelling it with a
    # drive letter would only be absolute on Windows: on POSIX `C:\Users\...` is a legal
    # *relative* filename, so the gate rightly permits it and the assertion would fail for a
    # reason that has nothing to do with the policy under test.
    outside = tmp_path.parent / "elsewhere" / "rwa_monthly.csv"
    report = validate_cell(
        f"monthly_output.write_csv(r{str(outside)!r})\n",
        registry=registry,
        policy=Policy(working_dir=tmp_path),
    )
    assert not report.ok
    assert report.stage is ValidationStage.POLICY
    assert "outside" in report.messages[0]


@pytest.mark.skipif(os.name != "nt", reason="a drive letter is only an absolute path on Windows")
def test_writing_to_another_drive_letter_is_rejected(
    registry: NameRegistry, tmp_path: Path
) -> None:
    """The Windows spelling of the same policy, kept because it is the one users hit."""
    report = validate_cell(
        'monthly_output.write_csv(r"C:\\\\Users\\\\Public\\\\rwa_monthly.csv")\n',
        registry=registry,
        policy=Policy(working_dir=tmp_path),
    )
    assert not report.ok
    assert report.stage is ValidationStage.POLICY
    assert "outside" in report.messages[0]


def test_a_relative_write_is_permitted(registry: NameRegistry, tmp_path: Path) -> None:
    report = validate_cell(
        'monthly_output.write_csv("outputs/rwa_monthly.csv")\n',
        registry=registry,
        policy=Policy(working_dir=tmp_path),
    )
    assert report.ok


def test_a_credential_in_a_literal_is_rejected(registry: NameRegistry) -> None:
    report = validate_cell(
        'warehouse = "postgresql://etl_user:hunter2hunter2@warehouse.internal:5432/risk"\n',
        registry=registry,
    )
    assert not report.ok
    assert report.stage is ValidationStage.POLICY
    assert "keyring" in report.messages[0]


def test_a_credential_shaped_assignment_is_rejected(registry: NameRegistry) -> None:
    report = validate_cell('api_key = "abcd1234efgh5678"\n', registry=registry)
    assert not report.ok
    assert report.stage is ValidationStage.POLICY


# ── stage 4: output style ────────────────────────────────────────────────────────────────────


def test_importing_pandas_is_rejected(registry: NameRegistry) -> None:
    code = (
        "import pandas as pd\n\n"
        'handin_frame = pd.read_excel("handin.xlsx", sheet_name="Exposures")\n'
    )
    report = validate_cell(code, registry=registry)
    assert not report.ok
    assert report.stage is ValidationStage.STYLE
    assert "polars, never pandas" in report.messages[0]


def test_from_pandas_import_is_rejected_too(registry: NameRegistry) -> None:
    report = validate_cell("from pandas import read_excel\n", registry=registry)
    assert not report.ok
    assert report.stage is ValidationStage.STYLE


def test_pandas_is_permitted_only_through_the_explicit_escape_hatch(
    registry: NameRegistry,
) -> None:
    report = validate_cell(
        "import pandas as pd\nlegacy = pd.DataFrame()\n",
        registry=registry,
        policy=Policy(allow_pandas=True),
    )
    assert report.ok


def test_a_bare_round_on_a_value_from_an_excel_round_region_is_rejected(
    registry: NameRegistry,
) -> None:
    code = (
        "apply_haircuts = load_handin.with_columns(\n"
        '    (pl.col("exposure") * pl.col("haircut")).round(2).alias("haircut_exposure")\n'
        ")\n"
    )
    report = validate_cell(
        code, registry=registry, rounding=RoundingContext(columns=frozenset({"haircut"}))
    )
    assert not report.ok
    assert report.stage is ValidationStage.STYLE
    message = report.messages[0]
    assert "xl.round" in message
    assert "15 significant decimal digits" in message
    assert "4491760.58" in message


def test_xl_round_is_accepted(registry: NameRegistry) -> None:
    code = (
        "apply_haircuts = load_handin.with_columns(\n"
        '    pl.col("exposure").xl.mul(pl.col("haircut")).xl.round(2).alias("haircut_exposure")\n'
        ")\n"
    )
    report = validate_cell(code, registry=registry, rounding=RoundingContext(strict=True))
    assert report.ok


def test_the_builtin_round_is_rejected_in_a_rounding_workbook(registry: NameRegistry) -> None:
    report = validate_cell(
        "total_charge = round(gross_charge, 2)\n",
        registry=registry,
        rounding=RoundingContext(strict=True),
    )
    assert not report.ok
    assert report.stage is ValidationStage.STYLE


def test_rounding_is_not_flagged_where_the_workbook_never_rounds(registry: NameRegistry) -> None:
    report = validate_cell("scaled = raw_value.round(2)\n", registry=registry)
    assert report.ok


def test_a_row_wise_loop_over_a_frame_is_flagged(registry: NameRegistry) -> None:
    code = (
        "haircut_values = []\n"
        "for row in load_handin.iter_rows(named=True):\n"
        '    haircut_values.append(row["exposure"] * 0.9)\n'
    )
    report = validate_cell(code, registry=registry)
    assert not report.ok
    assert report.stage is ValidationStage.STYLE
    assert "column operation by construction" in report.messages[0]


def test_a_loop_over_a_frame_named_in_the_registry_is_flagged() -> None:
    registry = NameRegistry([CellFacts(id="A", name="load", defs=("exposures_data",))])
    report = validate_cell(
        "totals = []\nfor exposures_data_row in [1]:\n    totals.append(exposures_data_row)\n",
        registry=registry,
        frame_names=frozenset({"exposures_data"}),
    )
    assert report.ok  # the loop is over a list literal, not over the frame

    flagged = validate_cell(
        "totals = []\nfor _row in exposures_data:\n    totals.append(_row)\n",
        registry=registry,
        frame_names=frozenset({"exposures_data"}),
    )
    assert not flagged.ok
    assert flagged.stage is ValidationStage.STYLE


# ── a clean cell ─────────────────────────────────────────────────────────────────────────────


def test_a_realistic_clean_cell_passes_every_stage(registry: NameRegistry, tmp_path: Path) -> None:
    code = (
        "apply_haircuts = (\n"
        "    load_handin\n"
        '    .join(reference_haircuts, left_on="asset_class", right_on="asset_class", how="left")\n'
        "    .with_columns(\n"
        '        pl.col("exposure")\n'
        '        .xl.mul(pl.col("haircut"))\n'
        "        .xl.round(2)\n"
        '        .alias("haircut_exposure")\n'
        "    )\n"
        ")\n"
    )
    report = validate_cell(
        code,
        registry=registry,
        policy=Policy(working_dir=tmp_path),
        rounding=RoundingContext(strict=True),
    )
    assert report.ok, report.messages
    assert report.names.defs == ("apply_haircuts",)
    assert "load_handin" in report.names.refs
    assert "reference_haircuts" in report.names.refs


# ── name extraction ──────────────────────────────────────────────────────────────────────────


def test_extract_names_collects_module_scope_bindings_only() -> None:
    code = (
        "import polars as pl\n"
        "from kedge.xl import round_half_away\n"
        "\n"
        "def _helper(frame):\n"
        "    local_only = frame.height\n"
        "    return local_only\n"
        "\n"
        "with open('a.txt') as handle:\n"
        "    first_line = handle.readline()\n"
        "\n"
        "for _item in source_rows:\n"
        "    pass\n"
    )
    names = extract_names(ast.parse(code))
    assert set(names.defs) >= {"pl", "round_half_away", "_helper", "handle", "first_line", "_item"}
    assert "local_only" not in names.defs
    assert names.public_defs == ("first_line", "handle", "pl", "round_half_away")
    assert "source_rows" in names.refs


# ── kernel-side rejections ───────────────────────────────────────────────────────────────────


def test_kernel_conflicts_are_reused_rather_than_reparsed_by_hand() -> None:
    message = (
        "Multiply-defined names:\n"
        "  - 'exposure' is already defined in cell 'MJUe' (load_handin)\n"
        "  - 'pl' is already defined in cell 'AAaa' (imports)\n"
    )
    conflicts = parse_definition_conflicts(message)
    assert [conflict.name for conflict in conflicts] == ["exposure", "pl"]

    rendered = violations_from_kernel_error(MultiplyDefinedError(message, conflicts))
    assert len(rendered) == 2
    assert "'exposure'" in rendered[0]
    assert "load_handin" in rendered[0]
    assert "_exposure" in rendered[0]


def test_a_kernel_error_with_no_conflicts_still_produces_a_violation() -> None:
    rendered = violations_from_kernel_error(RuntimeError("the kernel restarted mid-flush"))
    assert rendered == ("the kernel rejected this cell: the kernel restarted mid-flush",)


def test_violations_from_conflicts_names_the_owner() -> None:
    rendered = violations_from_conflicts(
        [DefinitionConflict(name="haircut", defined_in=("Kp2r", "reference_haircuts"))]
    )
    assert "reference_haircuts" in rendered[0]


def test_the_retry_cap_is_three() -> None:
    assert MAX_VALIDATION_ATTEMPTS == 3
