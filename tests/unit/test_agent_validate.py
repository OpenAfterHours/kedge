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
    MISSING_NAME_STAGE,
    Policy,
    RoundingContext,
    ValidationStage,
    extract_names,
    undefined_name,
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


def test_a_network_call_naming_no_literal_is_not_excused_by_an_allowlisted_url(
    registry: NameRegistry,
) -> None:
    """The same disarm the database check had: an allowlisted literal anywhere in the cell used to
    permit every other request beside it."""
    report = validate_cell(
        "import httpx\n\n"
        '_documented = "https://rates.internal.bank/v1"\n'
        "fx_rates = httpx.get(build_url()).json()\n",
        registry=registry,
        policy=Policy(network_allowlist=frozenset({"internal.bank"})),
    )
    assert not report.ok
    assert report.stage is ValidationStage.POLICY
    assert "cannot read" in report.messages[0]


def test_a_url_composed_from_an_allowlisted_base_is_still_permitted(
    registry: NameRegistry,
) -> None:
    """The reason the network check is not attributed per call the way the database check is: a
    URL is routinely composed, and refusing this would be a false rejection."""
    report = validate_cell(
        'import httpx\n\nbase = "https://rates.internal.bank"\n'
        'fx_rates = httpx.get(f"{base}/latest").json()\n',
        registry=registry,
        policy=Policy(network_allowlist=frozenset({"internal.bank"})),
    )
    assert report.ok, report.messages


@pytest.mark.parametrize(
    "code",
    [
        "import pyodbc\n",
        "import psycopg\n",
        "import psycopg2\n",
        "import pymssql\n",
        "import pymysql\n",
        "import mysql.connector\n",
        "import oracledb\n",
        "import cx_Oracle\n",
        "import snowflake.connector\n",
        "import connectorx\n",
        "import clickhouse_connect\n",
        "import adbc_driver_postgresql.dbapi\n",
        "from adbc_driver_snowflake import dbapi\n",
    ],
)
def test_importing_a_database_driver_is_refused(code: str, registry: NameRegistry) -> None:
    """A driver import is the whole signal: there is nothing else to do with `pyodbc`."""
    report = validate_cell(code, registry=registry)
    assert not report.ok
    assert report.stage is ValidationStage.POLICY
    assert "database connection" in report.messages[0]
    assert "database_allowlist" in report.messages[0]


def test_reading_a_warehouse_through_polars_is_refused_although_it_imports_only_polars(
    registry: NameRegistry,
) -> None:
    """The import-gated network check cannot see this cell: `pl.read_database_uri` imports
    nothing, and connectorx is reached from inside polars."""
    code = (
        "exposures = pl.read_database_uri(\n"
        '    query="select * from risk.exposures",\n'
        '    uri="postgresql://warehouse.internal:5432/risk",\n'
        ")\n"
    )
    report = validate_cell(code, registry=registry)
    assert not report.ok
    assert report.stage is ValidationStage.POLICY
    assert "read_database_uri" in report.messages[0]
    assert "warehouse.internal" in report.messages[0]


def test_writing_to_a_warehouse_is_refused_as_firmly_as_reading_from_one(
    registry: NameRegistry,
) -> None:
    code = 'apply_haircuts.write_database("risk.exposures", connection=warehouse_engine)\n'
    report = validate_cell(code, registry=registry)
    assert not report.ok
    assert report.stage is ValidationStage.POLICY
    assert "write_database" in report.messages[0]

    # And the refusal is liftable, which is the half that matters: the table name is not a target
    # and the engine lives in another cell, so `write_database` itself is the only thing here a
    # user can name. A refusal no configuration answers costs three attempts and then the turn.
    permitted = validate_cell(
        code, registry=registry, policy=Policy(database_allowlist=frozenset({"write_database"}))
    )
    assert permitted.ok, permitted.messages


def test_the_two_cell_engine_shape_can_be_permitted_by_configuration(
    registry: NameRegistry,
) -> None:
    """marimo's single-definition rule forces the engine into its own cell, so the ordinary
    approved workflow is two cells — and the second one contains no host, no driver import and
    nothing else a `database_allowlist` entry could match except the entry point."""
    policy = Policy(database_allowlist=frozenset({"warehouse.internal", "read_database"}))
    engine = validate_cell(
        "from sqlalchemy import create_engine\n\n"
        'warehouse_engine = create_engine("postgresql://warehouse.internal/risk")\n',
        registry=registry,
        policy=policy,
    )
    assert engine.ok, engine.messages

    read = validate_cell(
        'exposures = pl.read_database(\n    "select * from risk.exposures", '
        "connection=warehouse_engine\n)\n",
        registry=registry,
        policy=policy,
    )
    assert read.ok, read.messages


def test_the_reading_cell_of_that_shape_is_refused_by_default_and_says_what_lifts_it(
    registry: NameRegistry,
) -> None:
    report = validate_cell(
        'exposures = pl.read_database("select * from risk.exposures", '
        "connection=warehouse_engine)\n",
        registry=registry,
    )
    assert not report.ok
    assert "read_database" in report.messages[0]
    assert "'read_database'" in report.messages[0]  # named as the entry it would go in as


def test_an_allowlisted_target_does_not_excuse_a_connection_kedge_cannot_read(
    registry: NameRegistry,
) -> None:
    """Targets belong to calls, not to cells.

    Scraped cell-wide, one permitted literal disarmed the whole check: the moment a user widened
    the list — the only reason it exists — every cell naming the permitted warehouse got a free
    pass for every other connection in it.
    """
    report = validate_cell(
        'import pyodbc\n\n_approved = "SERVER=RiskWarehouse"\nwarehouse = pyodbc.connect(build_dsn())\n',
        registry=registry,
        policy=Policy(database_allowlist=frozenset({"riskwarehouse"})),
    )
    assert not report.ok
    assert report.stage is ValidationStage.POLICY
    assert "cannot read" in report.messages[0]
    assert "'pyodbc'" in report.messages[0]


def test_a_second_connection_beside_a_permitted_one_is_still_checked(
    registry: NameRegistry,
) -> None:
    report = validate_cell(
        "import pyodbc\n\n"
        'approved = pyodbc.connect("SERVER=warehouse.internal;DATABASE=risk")\n'
        "other = pyodbc.connect(other_dsn)\n",
        registry=registry,
        policy=Policy(database_allowlist=frozenset({"warehouse.internal"})),
    )
    assert not report.ok
    assert len(report.violations) == 1  # the permitted one is silent
    assert report.violations[0].line == 4


def test_a_connection_string_passed_inline_is_caught_as_a_credential(
    registry: NameRegistry,
) -> None:
    """The tidy spelling and the careless one now agree.

    `connection_string = "mssql+pyodbc://..."` was caught by the credential *name*; the same URI
    handed straight to a call was not, because there is no word boundary inside "pyodbc".
    """
    inline = validate_cell(
        'exposures = pl.read_database_uri(\n    "select 1",\n'
        '    "mssql+pyodbc://etl_user:hunter2hunter2@warehouse.internal/risk",\n)\n',
        registry=registry,
    )
    assert not inline.ok
    assert inline.stage is ValidationStage.POLICY
    assert any("keyring" in message for message in inline.messages)

    named = validate_cell(
        'connection_string = "mssql+pyodbc://etl_user:hunter2hunter2@warehouse.internal/risk"\n',
        registry=registry,
    )
    assert not named.ok
    assert any("keyring" in message for message in named.messages)


def test_a_warehouse_host_is_accepted_once_it_is_allowlisted(registry: NameRegistry) -> None:
    code = (
        "exposures = pl.read_database_uri(\n"
        '    "select * from risk.exposures", "postgresql://warehouse.internal:5432/risk"\n'
        ")\n"
    )
    report = validate_cell(
        code,
        registry=registry,
        policy=Policy(database_allowlist=frozenset({"warehouse.internal"})),
    )
    assert report.ok, report.messages


def test_an_odbc_dsn_is_accepted_once_it_is_allowlisted(registry: NameRegistry) -> None:
    """A DSN name is not a hostname, which is why it needs its own list rather than the
    hostname allowlist the network check reads."""
    code = (
        'import pyodbc\n\nwarehouse = pyodbc.connect("DSN=RiskWarehouse;Trusted_Connection=yes")\n'
    )
    report = validate_cell(
        code,
        registry=registry,
        policy=Policy(database_allowlist=frozenset({"RiskWarehouse"})),
    )
    assert report.ok, report.messages


def test_a_driver_may_be_allowlisted_where_the_connection_is_built_at_run_time(
    registry: NameRegistry,
) -> None:
    code = "import pyodbc\n\nwarehouse = pyodbc.connect(warehouse_dsn)\n"
    refused = validate_cell(code, registry=registry)
    assert not refused.ok
    assert "cannot read" in refused.messages[0]
    assert "'pyodbc'" in refused.messages[0]

    permitted = validate_cell(
        code, registry=registry, policy=Policy(database_allowlist=frozenset({"pyodbc"}))
    )
    assert permitted.ok, permitted.messages


def test_duckdb_over_a_local_file_is_not_treated_as_a_connection(registry: NameRegistry) -> None:
    """The false-positive trade, asserted: duckdb reading a parquet file beside the notebook is
    local work, and a gate that refuses it is one the model learns to fight."""
    code = (
        "import duckdb\n\n"
        "local_totals = duckdb.sql(\n"
        "    \"select asset_class, sum(exposure) from 'handins/exposures.parquet' group by 1\"\n"
        ").pl()\n"
    )
    report = validate_cell(code, registry=registry)
    assert report.ok, report.messages


def test_duckdb_attaching_a_warehouse_is_refused(registry: NameRegistry) -> None:
    code = "import duckdb\n\nwarehouse = duckdb.sql(\"attach 'postgresql://warehouse.internal/risk'\")\n"
    report = validate_cell(code, registry=registry)
    assert not report.ok
    assert report.stage is ValidationStage.POLICY
    assert "warehouse.internal" in report.messages[0]


def test_importing_sqlalchemy_without_connecting_is_not_refused(registry: NameRegistry) -> None:
    report = validate_cell(
        "from sqlalchemy import text\n\nstatement = text('select 1')\n", registry=registry
    )
    assert report.ok, report.messages


def test_creating_a_sqlalchemy_engine_is_refused(registry: NameRegistry) -> None:
    report = validate_cell(
        "from sqlalchemy import create_engine\n\n"
        'warehouse_engine = create_engine("postgresql+psycopg://warehouse.internal/risk")\n',
        registry=registry,
    )
    assert not report.ok
    assert report.stage is ValidationStage.POLICY
    assert "create_engine" in report.messages[0]


def test_a_docstring_naming_the_system_being_replaced_is_not_a_connection(
    registry: NameRegistry,
) -> None:
    """A conversion cell whose docstring says which warehouse it replaces is exactly what good
    work looks like here, and the gate used to read it as the connection itself."""
    report = validate_cell(
        "import duckdb\n\n"
        '"""Mirrors the old extract that ran against postgresql://warehouse.internal/risk."""\n'
        "local_totals = duckdb.sql(\"select * from 'handins/exposures.parquet'\").pl()\n",
        registry=registry,
    )
    assert report.ok, report.messages


def test_prose_that_merely_mentions_a_dsn_is_not_a_connection(registry: NameRegistry) -> None:
    report = validate_cell(
        'provenance = "replaces the nightly extract from postgresql://warehouse.internal/risk"\n',
        registry=registry,
    )
    assert report.ok, report.messages


@pytest.mark.parametrize(
    "code",
    [
        'import duckdb\n\n_flt = "account=4100"\n_t = duckdb.sql("select 1 where a = 1").pl()\n',
        "import duckdb\n\n_t = duckdb.sql(\"select 1 where c = 'a;server=prod'\").pl()\n",
    ],
)
def test_sql_that_looks_like_a_keyword_dsn_is_not_a_connection_target(
    code: str, registry: NameRegistry
) -> None:
    """`SERVER=`/`ACCOUNT=` only names a target when the value is host-shaped. A column
    predicate and a quoted fragment of SQL are neither."""
    report = validate_cell(code, registry=registry)
    assert report.ok, report.messages


def test_a_refusal_never_asks_the_user_to_allowlist_a_quoted_fragment(
    registry: NameRegistry,
) -> None:
    report = validate_cell(
        'import pyodbc\n\nwarehouse = pyodbc.connect("server=prod\';DATABASE=risk")\n',
        registry=registry,
    )
    assert not report.ok
    assert "prod'" not in report.messages[0]
    assert "'pyodbc'" in report.messages[0]  # falls back to the driver, which is nameable


@pytest.mark.parametrize(
    "code",
    [
        'rates = handins.read_database("rates_2024")\n',
        'style = create_engine("mpl")\n',
        'catalogue = store.read_database_uri("rates_2024")\n',
    ],
)
def test_an_entry_point_name_on_an_unrelated_object_is_not_a_connection(
    code: str, registry: NameRegistry
) -> None:
    """Tail-matching a call is how `_WRITE_CALLS` deliberately does *not* work, and for the same
    reason: a gate that rejects a legitimate cell teaches the model to fight it."""
    report = validate_cell(code, registry=registry)
    assert report.ok, report.messages


def test_the_polars_entry_points_are_recognised_on_pl_and_on_polars(registry: NameRegistry) -> None:
    for receiver in ("pl", "polars"):
        report = validate_cell(
            f'exposures = {receiver}.read_database_uri("select 1", '
            '"postgresql://warehouse.internal/risk")\n',
            registry=registry,
        )
        assert not report.ok, receiver
        assert "warehouse.internal" in report.messages[0]


def test_a_full_dsn_beside_an_unlisted_driver_is_refused(registry: NameRegistry) -> None:
    """The driver list will never be complete — `asyncpg` was not on it — so a literal that *is*
    a connection string naming a server is a signal on its own."""
    code = (
        "import asyncpg\n\n"
        '_dsn = "postgresql://warehouse.internal/risk"\n'
        "exposures = fetch_all(_dsn)\n"
    )
    report = validate_cell(code, registry=registry)
    assert not report.ok
    assert report.stage is ValidationStage.POLICY
    assert "warehouse.internal" in report.messages[0]

    # One entry lifts it, because the import and the string beside it are one connection.
    permitted = validate_cell(
        code, registry=registry, policy=Policy(database_allowlist=frozenset({"warehouse.internal"}))
    )
    assert permitted.ok, permitted.messages


def test_a_dsn_held_in_a_variable_is_attributed_to_the_call_that_uses_it(
    registry: NameRegistry,
) -> None:
    code = (
        "import pyodbc\n\n"
        '_dsn = "DRIVER={ODBC Driver 18 for SQL Server};SERVER=tcp:warehouse.internal,1433"\n'
        "warehouse = pyodbc.connect(_dsn)\n"
    )
    report = validate_cell(code, registry=registry)
    assert not report.ok
    assert "warehouse.internal" in report.messages[0]

    permitted = validate_cell(
        code, registry=registry, policy=Policy(database_allowlist=frozenset({"warehouse.internal"}))
    )
    assert permitted.ok, permitted.messages


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


@pytest.mark.parametrize(
    "literal",
    [
        "https://rates.internal.bank:8443/v1/latest",  # a port is not a password
        "analyst@bank.example",  # an @ with no scheme is an address
        "https://cdn.example.com/@vite/client",  # an @ in a path
        "https://analyst@rates.internal.bank/v1",  # a user with no secret beside it
        r"\\fileserver\risk\exposures.xlsx",  # a UNC path
        "C:/handins/2024-05/exposures.xlsx",
        "select * from risk.exposures where booked_at > '2024-01-01 09:00:00'",
    ],
)
def test_the_credential_check_does_not_fire_on_ordinary_strings(
    literal: str, registry: NameRegistry
) -> None:
    """The negative table for the credential patterns.

    The URI pattern was widened to catch `mssql+pyodbc://user:pass@host`, which has no word
    boundary to anchor on; every one of these is a shape that widening could plausibly have
    swept up. `keyring` is the tell — it appears in the credential refusals and nowhere else.
    """
    report = validate_cell(f"_source = {literal!r}\n", registry=registry)
    assert not any("keyring" in message for message in report.messages), report.messages


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


# ── the check a kernel would otherwise have made ─────────────────────────────────────────────
#
# `undefined_name` is not one of the five stages and `validate_cell` does not run it. It lives
# beside the gate because it extends the gate: `kedge convert` has no kernel, so nobody else will
# say that a cell failed to bind the name the cells below it read. It was written twice -- once in
# the driver and once in the eval's copy of the driver -- which is what one check kept beside its
# caller reliably becomes.


def test_a_body_that_does_not_define_the_cells_name_is_named_at_the_point_it_happened(
    registry: NameRegistry,
) -> None:
    """Three cells later, blaming a cell that was written correctly, is the alternative."""
    report = validate_cell("_working = 1", registry=registry, cell="adjust")
    violations = undefined_name(report, "adjust")

    assert report.ok, "the gate itself has no objection; that is the whole point"
    assert len(violations) == 1
    assert "must define 'adjust'" in violations[0]
    assert violations[0].startswith(f"{MISSING_NAME_STAGE}:")


def test_a_body_that_defines_the_name_raises_no_violation(registry: NameRegistry) -> None:
    report = validate_cell("adjust = 1", registry=registry, cell="adjust")

    assert undefined_name(report, "adjust") == ()


def test_the_violation_says_what_the_body_did_define(registry: NameRegistry) -> None:
    """A rejection a model can act on names the alternative it offered instead."""
    report = validate_cell("uplift = 1", registry=registry, cell="adjust")

    assert "It defines uplift." in undefined_name(report, "adjust")[0]


def test_a_body_that_defines_nothing_public_says_nothing_rather_than_an_empty_list(
    registry: NameRegistry,
) -> None:
    """``_working`` is cell-local to marimo, so there is genuinely no public name to report."""
    report = validate_cell("_working = 1", registry=registry, cell="adjust")

    assert "It defines nothing." in undefined_name(report, "adjust")[0]


def test_the_definition_check_is_not_one_of_the_five_stages() -> None:
    """A sixth ``ValidationStage`` member would say the gate runs it. It does not."""
    assert MISSING_NAME_STAGE not in {stage.value for stage in ValidationStage}
