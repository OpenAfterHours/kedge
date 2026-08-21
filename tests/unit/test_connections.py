"""Legacy connection extraction, and the credential redaction that guards it."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from kedge.analysis.connections import (
    CONNECTIONS_PART,
    REDACTED,
    extract_connections,
    redact_connection_string,
)
from kedge.analysis.model import ExtractionStatus, FindingKind, Severity

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

_SQL_CONNECTION_STRING = (
    "OLEDB;Provider=SQLOLEDB.1;Password=Sup3rSecret!;Persist Security Info=True;"
    "User ID=risk_reader;Initial Catalog=RiskDW;Data Source=sql-prod-01"
)
_ODBC_CONNECTION_STRING = "ODBC;DSN=RiskDW;UID=svc_risk;PWD=hunter2;"

_CONNECTIONS_XML = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<connections xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <connection id="1" name="RiskDW" type="5" refreshOnLoad="1" description="Monthly exposures">
    <dbPr connection="{_SQL_CONNECTION_STRING}"
          command="SELECT counterparty, ead FROM dbo.exposures WHERE as_of = ?"
          commandType="2"/>
  </connection>
  <connection id="2" name="Haircuts" type="1">
    <dbPr connection="{_ODBC_CONNECTION_STRING}" command="dbo.haircuts" commandType="3"/>
  </connection>
</connections>
"""


def _archive(parts: dict[str, bytes | str]) -> zipfile.ZipFile:
    """Build an in-memory .xlsx-shaped archive holding exactly the given parts."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as writer:
        for name, payload in parts.items():
            writer.writestr(name, payload)
    buffer.seek(0)
    return zipfile.ZipFile(buffer)


# ── absent and unparseable ───────────────────────────────────────────────────────────────────


def test_workbook_without_a_connections_part_is_absent_and_silent() -> None:
    with _archive({"xl/workbook.xml": "<workbook/>"}) as archive:
        connections, status, findings = extract_connections(archive)

    assert connections == []
    assert status is ExtractionStatus.ABSENT
    assert findings == [], "a workbook with no connections is normal and must not raise a finding"


def test_malformed_xml_degrades_to_unparseable_with_a_finding() -> None:
    with _archive({CONNECTIONS_PART: "<connections><connection name='x'>"}) as archive:
        connections, status, findings = extract_connections(archive)

    assert connections == []
    assert status is ExtractionStatus.UNPARSEABLE
    assert [finding.kind for finding in findings] == [FindingKind.UNPARSEABLE_PART]
    assert findings[0].location == CONNECTIONS_PART
    assert findings[0].severity is Severity.WARNING
    assert findings[0].remediation


def test_empty_part_degrades_rather_than_raising() -> None:
    with _archive({CONNECTIONS_PART: b""}) as archive:
        connections, status, findings = extract_connections(archive)

    assert (connections, status) == ([], ExtractionStatus.UNPARSEABLE)
    assert len(findings) == 1


def test_binary_rubbish_in_the_part_degrades_rather_than_raising() -> None:
    with _archive({CONNECTIONS_PART: bytes(range(256))}) as archive:
        connections, status, findings = extract_connections(archive)

    assert (connections, status) == ([], ExtractionStatus.UNPARSEABLE)
    assert findings[0].kind is FindingKind.UNPARSEABLE_PART


def test_a_part_holding_no_connections_is_ok_and_empty() -> None:
    with _archive({CONNECTIONS_PART: "<connections/>"}) as archive:
        connections, status, findings = extract_connections(archive)

    assert (connections, status, findings) == ([], ExtractionStatus.OK, [])


# ── parsing ──────────────────────────────────────────────────────────────────────────────────


def test_sql_command_provider_and_kind_are_extracted() -> None:
    with _archive({CONNECTIONS_PART: _CONNECTIONS_XML}) as archive:
        connections, status, findings = extract_connections(archive)

    assert status is ExtractionStatus.OK
    assert findings == []
    assert [connection.name for connection in connections] == ["RiskDW", "Haircuts"]

    sql = connections[0]
    assert sql.kind == "oledb"
    assert sql.command == "SELECT counterparty, ead FROM dbo.exposures WHERE as_of = ?"
    assert sql.command_type == "sql"
    assert sql.provider == "SQLOLEDB.1"
    assert sql.refresh_on_load is True
    assert sql.description == "Monthly exposures"

    odbc = connections[1]
    assert odbc.kind == "odbc"
    assert odbc.command_type == "table"
    assert odbc.refresh_on_load is False


def test_unknown_connection_kind_maps_to_other_rather_than_being_dropped() -> None:
    xml = '<connections><connection name="Odd" type="99"/></connections>'
    with _archive({CONNECTIONS_PART: xml}) as archive:
        connections, status, _ = extract_connections(archive)

    assert status is ExtractionStatus.OK
    assert len(connections) == 1
    assert connections[0].kind == "other"


def test_web_and_text_connections_are_classified() -> None:
    xml = (
        "<connections>"
        '<connection name="Rates" type="4">'
        '<webPr url="https://example.invalid/rates?apikey=abcd1234"/>'
        "</connection>"
        '<connection name="Feed" type="6"><textPr sourceFile="C:\\feeds\\daily.csv"/></connection>'
        '<connection name="Local"><dbPr connection="WORKSHEET;Sheet1!A1:D9"/></connection>'
        "</connections>"
    )
    with _archive({CONNECTIONS_PART: xml}) as archive:
        connections, status, _ = extract_connections(archive)

    assert status is ExtractionStatus.OK
    assert [connection.kind for connection in connections] == ["web", "text", "worksheet"]
    assert connections[1].connection_string_redacted == "C:\\feeds\\daily.csv"


def test_an_unnamed_connection_gets_a_positional_name_rather_than_being_dropped() -> None:
    with _archive({CONNECTIONS_PART: "<connections><connection/></connections>"}) as archive:
        connections, _, _ = extract_connections(archive)

    assert [connection.name for connection in connections] == ["connection1"]


# ── the part as Excel itself writes it ───────────────────────────────────────────────────────


_EXCEL_WRITTEN_CONNECTIONS_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<connections xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" mc:Ignorable="xr16" '
    'xmlns:xr16="http://schemas.microsoft.com/office/spreadsheetml/2017/revision16">'
    '<connection id="1" xr16:uid="{00000000-0015-0000-FFFF-FFFF00000000}" name="PositionsExtract" '
    'description="Client positions at day-end, current month and prior" type="1" '
    'refreshedVersion="8" minRefreshableVersion="3" background="1" saveData="1">'
    '<dbPr connection="ODBC;DSN=FinanceWarehouse;UID=svc_finread;DATABASE=FinanceWarehouse" '
    'command="SELECT p.client_code,_x000a_       p.market_value_gbp_x000a_'
    "  FROM fin.positions p_x000a_ WHERE p.status &lt;&gt; 'CLOSED'\"/>"
    "</connection></connections>"
)
"""Verbatim from Excel, with the query shortened.

Produced by opening ``evals/fee_billing_run/m11_management_fee_run.xlsx`` in Excel and saving
it. Two things that workbook was authored with do not survive: the ``&#10;`` character
references become ``_x000a_``, and ``commandType="2"`` is gone. Neither is malformed, so a
reader that decodes neither reports success and hands the planner a corrupted query.
"""


def test_the_form_excel_actually_writes_reads_back_as_the_query_it_holds() -> None:
    with _archive({CONNECTIONS_PART: _EXCEL_WRITTEN_CONNECTIONS_XML}) as archive:
        connections, status, findings = extract_connections(archive)

    assert (status, findings) == (ExtractionStatus.OK, [])
    connection = connections[0]
    assert connection.command is not None
    assert connection.command.splitlines() == [
        "SELECT p.client_code,",
        "       p.market_value_gbp",
        "  FROM fin.positions p",
        " WHERE p.status <> 'CLOSED'",
    ]
    assert "_x000a_" not in connection.command
    assert connection.command_type == "sql", "Excel drops commandType because 2 is its default"
    assert connection.name == "PositionsExtract"
    assert connection.kind == "odbc"
    assert "svc_finread" not in (connection.connection_string_redacted or "")


def test_a_missing_command_type_beside_a_command_is_the_schema_default() -> None:
    xml = '<connections><connection name="X"><dbPr command="SELECT 1"/></connection></connections>'
    with _archive({CONNECTIONS_PART: xml}) as archive:
        connections, _, _ = extract_connections(archive)

    assert connections[0].command_type == "sql"


def test_a_dbpr_with_no_command_is_not_given_a_command_type() -> None:
    """The default describes a query. Where there is none, inventing one would be a claim."""
    xml = (
        "<connections>"
        '<connection name="Local"><dbPr connection="WORKSHEET;Sheet1!A1:D9"/></connection>'
        "</connections>"
    )
    with _archive({CONNECTIONS_PART: xml}) as archive:
        connections, _, _ = extract_connections(archive)

    assert connections[0].command_type is None
    assert connections[0].kind == "worksheet"


def test_an_explicit_command_type_still_wins_over_the_default() -> None:
    xml = (
        "<connections>"
        '<connection name="X"><dbPr command="dbo.haircuts" commandType="3"/></connection>'
        "</connections>"
    )
    with _archive({CONNECTIONS_PART: xml}) as archive:
        connections, _, _ = extract_connections(archive)

    assert connections[0].command_type == "table"


def test_an_escaped_underscore_leaves_a_literal_x000a_alone() -> None:
    """The case a search-and-replace for ``_x000a_`` gets wrong.

    ``_x005f_`` is how OOXML writes an underscore that would otherwise open an escape, so
    ``_x005f_x000a_`` is a column called ``_x000a_`` and not a line break. Decoding has to be
    one pass that never rescans what it produced, or a legitimate identifier becomes whitespace.
    """
    xml = (
        '<connections><connection name="X"><dbPr command='
        '"SELECT _x005f_x000a_ AS odd,_x000a_       n FROM t"/></connection></connections>'
    )
    with _archive({CONNECTIONS_PART: xml}) as archive:
        connections, _, _ = extract_connections(archive)

    assert connections[0].command == "SELECT _x000a_ AS odd,\n       n FROM t"


def test_the_escape_is_decoded_wherever_it_appears_not_only_in_the_command() -> None:
    xml = (
        '<connections><connection name="Two_x000a_Lines" description="A_x0009_tab">'
        '<dbPr connection="ODBC;DSN=X" command="SELECT 1"/></connection></connections>'
    )
    with _archive({CONNECTIONS_PART: xml}) as archive:
        connections, _, _ = extract_connections(archive)

    assert connections[0].name == "Two\nLines"
    assert connections[0].description == "A\ttab"


def test_uppercase_hex_digits_decode_as_readily_as_lowercase() -> None:
    xml = '<connections><connection name="A_x000D__x000A_B"/></connections>'
    with _archive({CONNECTIONS_PART: xml}) as archive:
        connections, _, _ = extract_connections(archive)

    assert connections[0].name == "A\r\nB"


def test_an_escaped_surrogate_pair_becomes_the_one_character_it_encodes() -> None:
    xml = '<connections><connection name="_xD83D__xDE00_ ok"/></connections>'
    with _archive({CONNECTIONS_PART: xml}) as archive:
        connections, _, _ = extract_connections(archive)

    assert connections[0].name == "\U0001f600 ok"


def test_a_lone_surrogate_degrades_to_something_that_can_be_serialised() -> None:
    """A half-escape must not put a byte sequence in the artifact that cannot be written out."""
    xml = '<connections><connection name="_xD83D_ alone"/></connections>'
    with _archive({CONNECTIONS_PART: xml}) as archive:
        connections, status, findings = extract_connections(archive)

    assert (status, findings) == (ExtractionStatus.OK, [])
    assert connections[0].name.encode("utf-8"), "a lone surrogate would raise here"
    assert connections[0].model_dump_json()


def test_text_carrying_no_escape_survives_untouched() -> None:
    xml = (
        "<connections>"
        '<connection name="X"><dbPr command="SELECT max_x FROM t WHERE a_x_b = 1"/></connection>'
        "</connections>"
    )
    with _archive({CONNECTIONS_PART: xml}) as archive:
        connections, _, _ = extract_connections(archive)

    assert connections[0].command == "SELECT max_x FROM t WHERE a_x_b = 1"


# ── credentials ──────────────────────────────────────────────────────────────────────────────


def test_a_password_never_survives_into_the_model() -> None:
    """The guarantee: the raw string is a local, and the artifact carries no credential."""
    with _archive({CONNECTIONS_PART: _CONNECTIONS_XML}) as archive:
        connections, _, _ = extract_connections(archive)

    serialised = "\n".join(connection.model_dump_json() for connection in connections)
    for secret in ("Sup3rSecret!", "hunter2", "risk_reader", "svc_risk"):
        assert secret not in serialised, f"{secret!r} leaked into the analysis artifact"

    assert REDACTED in (connections[0].connection_string_redacted or "")
    # Everything that is not a credential still survives, or the field is useless.
    assert "Data Source=sql-prod-01" in (connections[0].connection_string_redacted or "")


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ("Provider=SQLOLEDB;Password=p@ss;Data Source=s", "p@ss"),
        ("Provider=SQLOLEDB;PWD=p@ss;Data Source=s", "p@ss"),
        ("DSN=x;Uid=admin;Pwd=p@ss", "p@ss"),
        ("DSN=x;UID=admin;PWD=p@ss", "admin"),
        ("Driver={SQL Server};Server=s;User Id=admin;Password=p@ss", "p@ss"),
        ("Provider=ACE.OLEDB.12.0;Jet OLEDB:Database Password=p@ss", "p@ss"),
        ("AccountName=a;AccountKey=cGFzc3dvcmQ=;EndpointSuffix=core.windows.net", "cGFzc3dvcmQ="),
        (
            "BlobEndpoint=https://a.blob.core.windows.net;SharedAccessSignature=sv=2021&sig=p@ss",
            "p@ss",
        ),
        ("https://api.example.invalid/data?access_token=p@ss&format=json", "p@ss"),
        ("https://api.example.invalid/data?api-key=p@ss", "p@ss"),
        ("https://admin:p@ss@intranet.example.invalid/report", "intranet.example.invalid"),
        ("Server=s;Authorization=Bearer eyJhbGciOiJIUzI1NiJ9", "eyJhbGciOiJIUzI1NiJ9"),
        ("Server=s;ClientSecret=p@ss;ClientId=abc", "p@ss"),
        ('Server=s;Extended Properties="HDR=YES;Password=p@ss"', "p@ss"),
        ("Driver={SQL Server};Server=s;Pwd={p@ss;with;braces}", "p@ss"),
    ],
)
def test_credential_variants_are_all_masked(raw: str, secret: str) -> None:
    masked = redact_connection_string(raw)

    assert masked is not None
    if secret == "intranet.example.invalid":
        # The host is what must survive when the userinfo is stripped.
        assert secret in masked
        assert "p@ss" not in masked
        return
    assert secret not in masked
    assert REDACTED in masked


def test_redaction_leaves_a_credential_free_string_alone() -> None:
    raw = "Provider=SQLOLEDB.1;Data Source=sql-prod-01;Initial Catalog=RiskDW;Integrated Security=SSPI"

    assert redact_connection_string(raw) == raw


def test_redaction_passes_through_none_and_blank() -> None:
    assert redact_connection_string(None) is None
    assert redact_connection_string("   ") == "   "


# ── corpus fixture, once it lands ────────────────────────────────────────────────────────────


@pytest.mark.corpus
def test_legacy_sql_fixture_yields_two_connections_with_no_credentials() -> None:
    fixture = FIXTURES / "legacy_sql.xlsx"
    if not fixture.is_file():
        pytest.skip("tests/fixtures/legacy_sql.xlsx has not landed yet")

    with zipfile.ZipFile(fixture) as archive:
        connections, status, findings = extract_connections(archive)

    assert status is ExtractionStatus.OK
    assert len(connections) >= 2
    assert findings == []
    assert any(connection.command for connection in connections), "at least one should carry SQL"
    serialised = "\n".join(connection.model_dump_json() for connection in connections)
    assert "password=" not in serialised.lower()
    assert "pwd=" not in serialised.lower()
