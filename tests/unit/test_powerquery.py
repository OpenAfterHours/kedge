"""DataMashup discovery, MS-QDEFF decoding, and M section splitting.

The load-bearing test here is
:func:`test_the_mashup_part_is_found_by_namespace_not_by_path`: the workbook it builds puts
the queries at ``customXml/item3.xml`` behind two decoys, which is exactly what a hardcoded
``item1.xml`` would miss (PLAN 1.5).
"""

from __future__ import annotations

import base64
import io
import struct
import zipfile
from pathlib import Path

import pytest

from kedge.analysis.model import ExtractionStatus, FindingKind, Severity
from kedge.analysis.powerquery import (
    SECTION_PART,
    decode_mashup,
    extract_power_query,
    split_m_section,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

SECTION = """section Section1;

shared Counterparties = let
    Source = Sql.Database("sql-prod-01", "RiskDW"),
    // a comment containing a semicolon ; which must not split the query
    Filtered = Table.SelectRows(Source, each [Active] = true)
in
    Filtered;

[Description = "collateral haircuts by asset class"]
shared #"Haircut Table" = let
    Note = "a text literal with ; and ""escaped quotes"" inside",
    Joined = Table.NestedJoin(Counterparties, {"id"}, Source, {"id"}, "c")
in
    Joined;

Helper = (x) => x + 1;
"""

_DECOY_PROPS = (
    '<ds:datastoreItem xmlns:ds="http://schemas.openxmlformats.org/officeDocument/2006/'
    'customXml" ds:itemID="{9F1C0D3A-0000-0000-0000-000000000001}"/>'
)
_DECOY_ITEM = '<properties xmlns="urn:example:decoy"><title>not a mashup</title></properties>'


# ── builders ─────────────────────────────────────────────────────────────────────────────────


def _package(parts: dict[str, bytes | str] | None = None) -> bytes:
    """An OPC package of the shape Power Query stores inside the DataMashup blob."""
    payload = {"Config/Package.xml": "<Package/>", SECTION_PART: SECTION}
    if parts is not None:
        payload = parts
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as writer:
        for name, content in payload.items():
            writer.writestr(name, content)
    return buffer.getvalue()


def _qdeff(package: bytes, *, trailing_bytes: int = 100_000) -> bytes:
    """Wrap a package in the MS-QDEFF frame: version, length, package, then trailing sections.

    ``trailing_bytes`` defaults to more than the 64KB ``zipfile`` scans back through, so a
    decoder that ignores the header and hands the whole blob to ``zipfile`` fails this.
    """
    permissions = b"<Permissions/>"
    metadata = b"<Metadata/>" + b"\x00" * trailing_bytes
    return (
        struct.pack("<II", 0, len(package))
        + package
        + struct.pack("<I", len(permissions))
        + permissions
        + struct.pack("<I", len(metadata))
        + metadata
        + struct.pack("<I", 0)
    )


def _mashup_part(blob: bytes) -> str:
    encoded = base64.b64encode(blob).decode("ascii")
    return f'<DataMashup xmlns="http://schemas.microsoft.com/DataMashup">{encoded}</DataMashup>'


def _workbook(mashup_xml: str | None, *, part: str = "customXml/item3.xml") -> zipfile.ZipFile:
    """A workbook archive with two decoy custom XML parts before the real one."""
    parts: dict[str, bytes | str] = {
        "xl/workbook.xml": "<workbook/>",
        "customXml/item1.xml": _DECOY_ITEM,
        "customXml/itemProps1.xml": _DECOY_PROPS,
        "customXml/item2.xml": _DECOY_ITEM,
        "customXml/itemProps2.xml": _DECOY_PROPS,
    }
    if mashup_xml is not None:
        parts[part] = mashup_xml
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as writer:
        for name, content in parts.items():
            writer.writestr(name, content)
    buffer.seek(0)
    return zipfile.ZipFile(buffer)


# ── discovery ────────────────────────────────────────────────────────────────────────────────


def test_the_mashup_part_is_found_by_namespace_not_by_path() -> None:
    with _workbook(_mashup_part(_qdeff(_package()))) as archive:
        result, findings = extract_power_query(archive)

    assert result.status is ExtractionStatus.OK
    assert result.part_path == "customXml/item3.xml", "the part must be found by namespace"
    assert findings == []
    assert [query.name for query in result.queries] == [
        "Counterparties",
        "Haircut Table",
        "Helper",
    ]
    assert result.section_m_bytes == len(SECTION.encode("utf-8"))


def test_a_workbook_with_no_mashup_part_is_absent_and_silent() -> None:
    with _workbook(None) as archive:
        result, findings = extract_power_query(archive)

    assert result.status is ExtractionStatus.ABSENT
    assert result.queries == []
    assert findings == []


def test_a_bare_zip_payload_still_decodes() -> None:
    """Some writers emit the package with no QDEFF header at all."""
    with _workbook(_mashup_part(_package())) as archive:
        result, _ = extract_power_query(archive)

    assert result.status is ExtractionStatus.OK
    assert len(result.queries) == 3


def test_qdeff_framing_is_parsed_rather_than_left_to_zipfile() -> None:
    package = _package()
    blob = _qdeff(package, trailing_bytes=200_000)

    assert decode_mashup(blob) == package
    with pytest.raises(zipfile.BadZipFile):
        zipfile.ZipFile(io.BytesIO(blob))  # the naive reading, for contrast


# ── corrupt input ────────────────────────────────────────────────────────────────────────────


def _assert_degraded(archive: zipfile.ZipFile, *, fragment: str) -> None:
    result, findings = extract_power_query(archive)

    assert result.status is ExtractionStatus.UNPARSEABLE
    assert result.queries == []
    assert len(findings) == 1
    assert findings[0].kind is FindingKind.UNPARSEABLE_PART
    assert findings[0].severity is Severity.WARNING
    assert findings[0].remediation
    assert fragment in findings[0].message, findings[0].message


def test_truncated_base64_degrades_with_a_finding() -> None:
    encoded = base64.b64encode(_qdeff(_package())).decode("ascii")
    broken = (
        f'<DataMashup xmlns="http://schemas.microsoft.com/DataMashup">{encoded[:-3]}A</DataMashup>'
    )
    with _workbook(broken) as archive:
        _assert_degraded(archive, fragment="customXml/item3.xml")


def test_invalid_base64_degrades_with_a_finding() -> None:
    payload = '<DataMashup xmlns="http://schemas.microsoft.com/DataMashup">abcde</DataMashup>'
    with _workbook(payload) as archive:
        _assert_degraded(archive, fragment="not valid base64")


def test_a_mashup_containing_non_zip_bytes_degrades_with_a_finding() -> None:
    with _workbook(_mashup_part(b"this is definitely not a zip archive")) as archive:
        _assert_degraded(archive, fragment="not a zip archive")


def test_a_package_missing_section1_degrades_with_a_finding() -> None:
    package = _package({"Config/Package.xml": "<Package/>"})
    with _workbook(_mashup_part(_qdeff(package))) as archive:
        _assert_degraded(archive, fragment=f"contains no {SECTION_PART}")


def test_an_empty_mashup_element_degrades_with_a_finding() -> None:
    payload = '<DataMashup xmlns="http://schemas.microsoft.com/DataMashup"></DataMashup>'
    with _workbook(payload) as archive:
        _assert_degraded(archive, fragment="empty")


def test_malformed_xml_in_a_mashup_part_degrades_with_a_finding() -> None:
    with _workbook('<DataMashup xmlns="http://schemas.microsoft.com/DataMashup">oops') as archive:
        _assert_degraded(archive, fragment="not well-formed XML")


def test_a_malformed_decoy_part_is_ignored_rather_than_reported() -> None:
    """A broken customXml part that never claimed to be a mashup is not our problem."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as writer:
        writer.writestr("customXml/item1.xml", "<unclosed")
        writer.writestr("customXml/item2.xml", _mashup_part(_qdeff(_package())))
    buffer.seek(0)

    with zipfile.ZipFile(buffer) as archive:
        result, findings = extract_power_query(archive)

    assert result.status is ExtractionStatus.OK
    assert findings == []


def test_a_truncated_inner_package_degrades_with_a_finding() -> None:
    package = _package()[: len(_package()) // 2]
    with _workbook(_mashup_part(package)) as archive:
        result, findings = extract_power_query(archive)

    assert result.status is ExtractionStatus.UNPARSEABLE
    assert findings[0].kind is FindingKind.UNPARSEABLE_PART


# ── M splitting ──────────────────────────────────────────────────────────────────────────────


def test_queries_are_split_on_top_level_semicolons_only() -> None:
    queries = split_m_section(SECTION)

    assert [query.name for query in queries] == ["Counterparties", "Haircut Table", "Helper"]
    assert queries[0].m_source.startswith("shared Counterparties =")
    assert queries[0].m_source.endswith(";")
    assert "a comment containing a semicolon" in queries[0].m_source


def test_a_quoted_query_name_is_unquoted() -> None:
    queries = split_m_section('section Section1;\nshared #"Sales by Region" = 1;')

    assert [query.name for query in queries] == ["Sales by Region"]


def test_referenced_queries_are_resolved_between_members() -> None:
    queries = {query.name: query for query in split_m_section(SECTION)}

    assert queries["Haircut Table"].referenced_queries == ["Counterparties"]
    assert queries["Counterparties"].referenced_queries == []


def test_a_reference_by_quoted_name_is_resolved() -> None:
    section = 'section Section1;\nshared #"A B" = 1;\nshared C = #"A B" + 1;'

    assert split_m_section(section)[1].referenced_queries == ["A B"]


def test_the_section_header_is_not_mistaken_for_a_query() -> None:
    assert split_m_section("section Section1;") == []


def test_an_empty_section_yields_no_queries() -> None:
    assert split_m_section("") == []
    assert split_m_section("   \n  ") == []


def test_an_unterminated_literal_degrades_rather_than_raising() -> None:
    queries = split_m_section('section Section1;\nshared A = "unterminated')

    assert [query.name for query in queries] == ["A"]


def test_a_member_without_a_trailing_semicolon_is_still_recovered() -> None:
    queries = split_m_section("section Section1;\nshared Last = 1 + 1")

    assert [query.name for query in queries] == ["Last"]


# ── corpus fixture, once it lands ────────────────────────────────────────────────────────────


@pytest.mark.corpus
def test_power_query_fixture_decodes() -> None:
    fixture = FIXTURES / "powerquery.xlsx"
    if not fixture.is_file():
        pytest.skip("tests/fixtures/powerquery.xlsx has not landed yet")

    with zipfile.ZipFile(fixture) as archive:
        result, findings = extract_power_query(archive)

    assert result.status is ExtractionStatus.OK, result.detail
    assert result.queries, "the fixture is meant to carry at least one query"
    assert findings == []
    assert result.part_path is not None
    assert result.part_path != "customXml/item1.xml", (
        "the fixture deliberately places the mashup elsewhere; finding it at item1 means "
        "the path was hardcoded"
    )
