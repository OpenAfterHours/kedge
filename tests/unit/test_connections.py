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
