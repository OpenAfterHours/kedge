"""Power Query M source: customXml to DataMashup to ``Formulas/Section1.m``.

The trickiest extraction in the project, and the one with a trap in it. The M source is a zip
inside a base64 blob inside an XML part inside the workbook's own zip, and **the part is not
reliably ``customXml/item1.xml``** — Excel numbers those parts by insertion order, so a
workbook that acquired a couple of other custom XML parts first will carry its queries at
``item3.xml`` or later. Hardcoding the path is the single easiest way to get this wrong, so
:func:`extract_power_query` iterates every ``customXml`` part and selects by namespace.

The decoded blob is MS-QDEFF, not a bare zip: a 4-byte version, a 4-byte package length, the
OPC package itself, and then length-prefixed permissions and metadata sections trailing behind
it. :func:`decode_mashup` reads that header and slices the package out, because relying on
``zipfile`` to scan back past the trailing sections only works while those sections stay under
64KB. It falls back to treating the blob as a bare zip, and then to hunting for the local file
header signature, so a hand-built or slightly unusual blob still decodes.

Everything degrades: a part that will not decode produces
:attr:`~kedge.analysis.model.ExtractionStatus.UNPARSEABLE` and an ``UNPARSEABLE_PART``
finding, never an exception (CONVENTIONS non-negotiable 4).

References:
- PLAN.md 1.5, M1. MS-QDEFF (Query Definition File Format).
"""

from __future__ import annotations

import base64
import binascii
import io
import logging
import re
import struct
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from kedge.analysis.model import (
    ExtractionStatus,
    Finding,
    FindingKind,
    PowerQuery,
    PowerQueryExtraction,
    Severity,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DATAMASHUP_NAMESPACES",
    "SECTION_PART",
    "decode_mashup",
    "extract_power_query",
    "split_m_section",
]

DATAMASHUP_NAMESPACES = (
    "http://schemas.microsoft.com/DataMashup",
    "http://schemas.microsoft.com/DataExplorer",
)
"""Namespaces that mark a customXml part as carrying queries. DataExplorer is the older one."""

SECTION_PART = "Formulas/Section1.m"
"""Where every query's M source is concatenated inside the mashup package."""

_CUSTOM_XML_RE = re.compile(r"(?i)^customxml/[^/]+\.xml$")
_ZIP_SIGNATURE = b"PK\x03\x04"
_MAX_PART_BYTES = 64 * 1024 * 1024
_MAX_SECTION_BYTES = 16 * 1024 * 1024
_QDEFF_HEADER_BYTES = 8

# One pass over M source, in precedence order: comments, strings, quoted identifiers, bare
# identifiers, then any single character. Anything the first four match is opaque to the
# structural scan, which is what keeps a `;` inside a comment or a string from splitting a
# query in half.
_LEX_RE = re.compile(
    r"""
      //[^\n]*                      # line comment
    | /\*.*?(?:\*/|\Z)              # block comment, tolerating an unterminated one
    | \#"[^"]*(?:""[^"]*)*"         # quoted identifier
    | "[^"]*(?:""[^"]*)*"           # text literal
    | [A-Za-z_][A-Za-z0-9_.]*       # bare identifier
    | .                             # anything else, one character at a time
    """,
    re.VERBOSE | re.DOTALL,
)
_OPENERS = frozenset("([{")
_CLOSERS = frozenset(")]}")


# ── M source splitting ───────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Token:
    start: int
    end: int
    text: str

    @property
    def is_opaque(self) -> bool:
        """True for comments and literals: present in the text, invisible to the parser."""
        return self.text.startswith(("//", "/*", '"'))

    @property
    def is_identifier(self) -> bool:
        return bool(self.text) and (self.text[0].isalpha() or self.text[0] == "_")

    @property
    def is_quoted_identifier(self) -> bool:
        return self.text.startswith('#"')


def _lex(source: str) -> list[_Token]:
    return [_Token(m.start(), m.end(), m.group(0)) for m in _LEX_RE.finditer(source)]


def _unquote_identifier(text: str) -> str:
    """Turn ``#"Sales by Region"`` into ``Sales by Region``."""
    if not text.startswith('#"'):
        return text
    return text[2:-1].replace('""', '"') if text.endswith('"') else text[2:]


def _statement_spans(source: str, tokens: list[_Token]) -> list[tuple[int, int]]:
    """Return (start, end) character spans for each top-level ``;``-terminated statement."""
    spans: list[tuple[int, int]] = []
    depth = 0
    start = 0
    for token in tokens:
        if token.is_opaque or token.is_quoted_identifier:
            continue
        if token.text in _OPENERS:
            depth += 1
        elif token.text in _CLOSERS:
            depth = max(0, depth - 1)
        elif token.text == ";" and depth == 0:
            spans.append((start, token.end))
            start = token.end
    if source[start:].strip():
        spans.append((start, len(source)))
    return spans


@dataclass(frozen=True, slots=True)
class _Member:
    name: str
    text: str
    body_tokens: tuple[_Token, ...]


def _parse_member(source: str, span: tuple[int, int]) -> _Member | None:
    """Parse one section member: optional attributes, optional ``shared``, name, ``=``, body."""
    start, end = span
    text = source[start:end]
    tokens = [token for token in _lex(text) if token.text.strip() or token.is_opaque]
    index = 0

    # Skip leading comments and any `[ ... ]` attribute record.
    while (
        index < len(tokens) and tokens[index].is_opaque and not tokens[index].text.startswith('"')
    ):
        index += 1
    if index < len(tokens) and tokens[index].text == "[":
        depth = 0
        while index < len(tokens):
            if tokens[index].text == "[":
                depth += 1
            elif tokens[index].text == "]":
                depth -= 1
                if depth == 0:
                    index += 1
                    break
            index += 1
    while (
        index < len(tokens) and tokens[index].is_opaque and not tokens[index].text.startswith('"')
    ):
        index += 1

    if index >= len(tokens):
        return None
    if tokens[index].text == "section":
        return None  # the `section Section1;` header, not a query
    if tokens[index].text == "shared":
        index += 1
    if index >= len(tokens):
        return None

    name_token = tokens[index]
    if not (name_token.is_identifier or name_token.is_quoted_identifier):
        return None
    name = _unquote_identifier(name_token.text).strip()
    if not name:
        return None

    body_start = index + 1
    if body_start < len(tokens) and tokens[body_start].text == "=":
        body_start += 1
    return _Member(name=name, text=text.strip(), body_tokens=tuple(tokens[body_start:]))


def _referenced(member: _Member, names: set[str]) -> list[str]:
    """Names of other queries this one mentions, in order of first appearance."""
    seen: list[str] = []
    for token in member.body_tokens:
        if token.is_opaque:
            continue
        if not (token.is_identifier or token.is_quoted_identifier):
            continue
        candidate = _unquote_identifier(token.text)
        if candidate in names and candidate != member.name and candidate not in seen:
            seen.append(candidate)
    return seen


def split_m_section(section: str) -> list[PowerQuery]:
    """Split a concatenated ``Section1.m`` into one :class:`PowerQuery` per named query.

    Section members look like ``shared Name = let ... in ...;``, with ``shared`` optional and
    the name optionally quoted as ``#"Name With Spaces"``. Splitting is done by scanning for
    top-level semicolons through a lexer that understands comments, text literals and quoted
    identifiers, so a ``;`` inside a comment or a string does not split a query in two.

    Known limitations, both deliberate rather than accidental: this is not a full M parser, so
    an unterminated literal degrades to a best-effort split; and ``referenced_queries`` is
    resolved by matching identifiers against the set of query names, so a ``let`` variable that
    happens to share a query's name produces a spurious edge. Both fail towards reporting more
    than exists rather than silently losing a query.

    Args:
        section: The decoded text of ``Formulas/Section1.m``.

    Returns:
        One query per member, in source order. An empty list if there are no members.
    """
    if not section.strip():
        return []

    spans = _statement_spans(section, _lex(section))
    members = [
        member for member in (_parse_member(section, span) for span in spans) if member is not None
    ]
    names = {member.name for member in members}
    return [
        PowerQuery(
            name=member.name,
            m_source=member.text,
            referenced_queries=_referenced(member, names),
        )
        for member in members
    ]


# ── MS-QDEFF decoding ────────────────────────────────────────────────────────────────────────


def decode_mashup(blob: bytes) -> bytes | None:
    """Extract the OPC package from a decoded DataMashup blob.

    Tries the MS-QDEFF header first (version, package length, package, then length-prefixed
    permissions and metadata), then a bare zip, then a scan for the first local file header.
    The fallbacks matter: hand-built fixtures and some third-party writers emit the package
    with no header at all.

    Args:
        blob: The base64-decoded contents of the ``<DataMashup>`` element.

    Returns:
        The package bytes, or ``None`` if nothing zip-shaped could be found.
    """
    if blob.startswith(_ZIP_SIGNATURE):
        return blob

    if len(blob) > _QDEFF_HEADER_BYTES:
        version, package_length = struct.unpack_from("<II", blob, 0)
        end = _QDEFF_HEADER_BYTES + package_length
        if (
            0 < package_length <= len(blob) - _QDEFF_HEADER_BYTES
            and blob[_QDEFF_HEADER_BYTES : _QDEFF_HEADER_BYTES + 4] == _ZIP_SIGNATURE
        ):
            logger.debug("DataMashup header: version %d, package %d bytes", version, package_length)
            return blob[_QDEFF_HEADER_BYTES:end]

    offset = blob.find(_ZIP_SIGNATURE)
    if offset >= 0:
        logger.debug("no usable DataMashup header; using the zip found at offset %d", offset)
        return blob[offset:]
    return None


def _read_section(package: bytes) -> tuple[str, int] | None:
    """Return the decoded text of ``Formulas/Section1.m`` and its byte length."""
    with zipfile.ZipFile(io.BytesIO(package)) as inner:
        wanted = SECTION_PART.lower()
        name = next((entry for entry in inner.namelist() if entry.lower() == wanted), None)
        if name is None:
            return None
        info = inner.getinfo(name)
        if info.file_size > _MAX_SECTION_BYTES:
            msg = f"{SECTION_PART} is {info.file_size} bytes, above the supported ceiling"
            raise ValueError(msg)
        raw = inner.read(name)

    for encoding in ("utf-8-sig", "utf-16", "utf-8"):
        try:
            return raw.decode(encoding), len(raw)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return raw.decode("latin-1", errors="replace"), len(raw)


# ── part discovery ───────────────────────────────────────────────────────────────────────────


def _custom_xml_parts(zf: zipfile.ZipFile) -> list[str]:
    try:
        names = zf.namelist()
    except (OSError, RuntimeError, zipfile.BadZipFile):
        logger.warning("could not list the workbook archive while looking for customXml parts")
        return []
    return sorted(name for name in names if _CUSTOM_XML_RE.match(name))


def _is_mashup_root(root: ET.Element) -> bool:
    tag = root.tag
    namespace = tag[1:].split("}", maxsplit=1)[0] if tag.startswith("{") else ""
    local = tag.rsplit("}", maxsplit=1)[-1]
    return namespace in DATAMASHUP_NAMESPACES or local == "DataMashup"


def _finding(part: str, detail: str, remediation: str) -> Finding:
    return Finding(
        kind=FindingKind.UNPARSEABLE_PART,
        severity=Severity.WARNING,
        message=f"could not decode DataMashup in {part}: {detail}",
        location=part,
        detail=detail,
        remediation=remediation,
    )


_REMEDIATION = (
    "Open the workbook in Excel, use Data > Queries and Connections to confirm the queries "
    "still load, and re-save. If they do not, the mashup part is damaged and the M source "
    "will have to be recovered from a previous version of the file."
)


def _candidate_parts(zf: zipfile.ZipFile) -> tuple[list[tuple[str, str]], list[Finding]]:
    """Find every customXml part carrying a DataMashup element, with its base64 payload."""
    candidates: list[tuple[str, str]] = []
    findings: list[Finding] = []

    for part in _custom_xml_parts(zf):
        try:
            raw = zf.read(part)
        except (KeyError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
            logger.warning("could not read %s: %s", part, exc)
            continue
        if len(raw) > _MAX_PART_BYTES:
            logger.warning("skipping oversized custom XML part %s (%d bytes)", part, len(raw))
            continue

        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            if b"DataMashup" in raw:
                findings.append(_finding(part, f"not well-formed XML ({exc})", _REMEDIATION))
            else:
                logger.debug("skipping custom XML part %s: not well-formed XML", part)
            continue

        if not _is_mashup_root(root):
            logger.debug("skipping custom XML part %s: not a DataMashup part", part)
            continue
        candidates.append((part, (root.text or "").strip()))

    return candidates, findings


def extract_power_query(zf: zipfile.ZipFile) -> tuple[PowerQueryExtraction, list[Finding]]:
    """Recover every Power Query M query from a workbook archive.

    Iterates every ``customXml`` part and selects the one whose root element is in a
    DataMashup namespace — never by path, because the part number varies between workbooks.
    A workbook with no such part is normal and produces no findings.

    Args:
        zf: The open ``.xlsx`` archive.

    Returns:
        The extraction result, and any findings raised while decoding.
    """
    candidates, findings = _candidate_parts(zf)
    if not candidates:
        if findings:
            return (
                PowerQueryExtraction(
                    status=ExtractionStatus.UNPARSEABLE,
                    detail=findings[0].message,
                ),
                findings,
            )
        logger.debug("no DataMashup part in this workbook")
        return PowerQueryExtraction(status=ExtractionStatus.ABSENT), []

    for part, payload in candidates:
        result, failure = _extract_one(part, payload)
        if result is not None:
            logger.info("recovered %d Power Query queries from %s", len(result.queries), part)
            return result, findings
        if failure is not None:
            findings.append(failure)

    detail = findings[-1].message if findings else "no DataMashup part could be decoded"
    return (
        PowerQueryExtraction(
            status=ExtractionStatus.UNPARSEABLE,
            part_path=candidates[0][0],
            detail=detail,
        ),
        findings,
    )


def _extract_one(part: str, payload: str) -> tuple[PowerQueryExtraction | None, Finding | None]:
    """Decode one DataMashup payload. Returns (result, None) or (None, finding)."""
    if not payload:
        return None, _finding(part, "the DataMashup element is empty", _REMEDIATION)

    try:
        blob = base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError) as exc:
        return None, _finding(part, f"the payload is not valid base64 ({exc})", _REMEDIATION)
    if not blob:
        return None, _finding(part, "the payload decoded to zero bytes", _REMEDIATION)

    package = decode_mashup(blob)
    if package is None:
        return None, _finding(part, "not a zip archive", _REMEDIATION)

    try:
        section = _read_section(package)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ValueError, EOFError) as exc:
        return None, _finding(part, f"the mashup package would not open ({exc})", _REMEDIATION)

    if section is None:
        return None, _finding(
            part,
            f"the mashup package contains no {SECTION_PART}",
            _REMEDIATION,
        )

    text, size = section
    try:
        queries = split_m_section(text)
    except Exception as exc:  # a lexer surprise must not cost the whole analysis
        logger.warning("could not split %s from %s: %s", SECTION_PART, part, exc)
        return None, _finding(
            part, f"{SECTION_PART} could not be split into queries ({exc})", _REMEDIATION
        )

    return (
        PowerQueryExtraction(
            status=ExtractionStatus.OK,
            part_path=part,
            queries=queries,
            section_m_bytes=size,
            detail=None if queries else f"{SECTION_PART} decoded but defines no queries",
        ),
        None,
    )
