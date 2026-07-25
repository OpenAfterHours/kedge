"""Legacy external data connections: ``xl/connections.xml`` to SQL plus a redacted string.

openpyxl does not parse this part at all (PLAN 1.5), so the workbook is read as a zip and the
XML is parsed here. What comes back is the thing a reviewer actually wants from a fifteen-tab
workbook: the SQL that feeds it, and which database it came from.

**Connection strings routinely carry credentials.** The contract has
``connection_string_redacted`` and no raw field, deliberately — the raw string exists only as
a local in :func:`extract_connections` and is never returned, logged, or stored.
:func:`redact_connection_string` is the only thing standing between a workbook someone saved
with "remember my password" ticked and an analysis artifact on disk, so it works two ways:
structurally, by splitting the string into ``key=value`` pairs and masking sensitive keys, and
then by sweeping the result with a regular expression to catch credentials nested inside
quoted sub-strings and URL query parameters.

References:
- PLAN.md 1.5, M1. ECMA-376 Part 1 18.13 (``connections``, ``dbPr``, ``webPr``, ``textPr``).
"""

from __future__ import annotations

import logging
import re
import zipfile
from typing import Literal
from xml.etree import ElementTree as ET

from kedge.analysis.model import (
    Connection,
    ExtractionStatus,
    Finding,
    FindingKind,
    Severity,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CONNECTIONS_PART",
    "REDACTED",
    "extract_connections",
    "redact_connection_string",
]

CONNECTIONS_PART = "xl/connections.xml"
REDACTED = "***REDACTED***"
"""What a masked credential is replaced with. Deliberately conspicuous."""

_MAX_PART_BYTES = 32 * 1024 * 1024
"""Refuse to parse an absurdly large connections part rather than expanding it into memory."""

ConnectionKind = Literal["odbc", "oledb", "web", "text", "worksheet", "other"]

# ECMA-376 18.18.16 (ST_ConnectionType-equivalent codes on `connection@type`). Anything not
# listed maps to `other` — unknown kinds are reported, never dropped.
_TYPE_CODES: dict[str, ConnectionKind] = {
    "1": "odbc",
    "4": "web",
    "5": "oledb",
    "6": "text",
}

# ECMA-376 18.13.4 (`dbPr@commandType`). Unrecognised codes are passed through verbatim.
_COMMAND_TYPES = {
    "1": "cube",
    "2": "sql",
    "3": "table",
    "4": "default",
    "5": "list",
}

_PROVIDER_RE = re.compile(r"(?i)\bprovider\s*=\s*([^;]+)")
_DRIVER_RE = re.compile(r"(?i)\bdriver\s*=\s*(\{[^}]*\}|[^;]+)")

# Normalised (lower-cased, punctuation-stripped) connection-string keys whose values are
# credentials. `_is_sensitive_key` also treats any key *ending* in one of the suffixes below
# as sensitive, which is what catches `Jet OLEDB:Database Password` and `ClientSecret`.
_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "pwd",
        "passwd",
        "pass",
        "userid",
        "uid",
        "user",
        "username",
        "accountkey",
        "sharedaccesssignature",
        "sig",
        "sastoken",
        "sas",
        "accesstoken",
        "refreshtoken",
        "token",
        "apikey",
        "key",
        "secret",
        "credential",
        "credentials",
        "authorization",
        "auth",
        "certificate",
        "privatekey",
        "publickey",
        "passphrase",
    }
)
_SENSITIVE_SUFFIXES = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "apikey",
    "accesskey",
    "accountkey",
    "privatekey",
    "passphrase",
    "credential",
    "signature",
)
_KEY_PUNCTUATION = re.compile(r"[\s_\-.:]+")

# Second pass, for credentials that never appear as a top-level `key=value` pair: inside a
# quoted `Extended Properties="..."`, in a URL query string, or after a bearer scheme.
_SWEEP_RE = re.compile(
    r"(?i)(?<![\w-])("
    r"shared[_\- ]?access[_\- ]?signature|client[_\- ]?secret|access[_\- ]?token|"
    r"refresh[_\- ]?token|private[_\- ]?key|account[_\- ]?key|api[_\- ]?key|"
    r"user[_\- ]?id|user[_\- ]?name|passphrase|password|passwd|credential|"
    r"authorization|secret|token|apikey|pwd|uid|sig|sas|user"
    r")\s*=\s*([^;&\s\"'}\]]+)"
)
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+([A-Za-z0-9\-._~+/=]{8,})")
_URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/\s@]+)@")


# ── redaction ────────────────────────────────────────────────────────────────────────────────


def _normalise_key(key: str) -> str:
    return _KEY_PUNCTUATION.sub("", key).strip().lower()


def _is_sensitive_key(key: str) -> bool:
    normalised = _normalise_key(key)
    if not normalised:
        return False
    return normalised in _SENSITIVE_KEYS or normalised.endswith(_SENSITIVE_SUFFIXES)


def _split_pairs(raw: str) -> list[str]:
    """Split a connection string on `;`, respecting ODBC braces and quoted values.

    A value opened with ``{``, ``'`` or ``"`` immediately after its ``=`` runs to its closing
    delimiter, so ``Driver={SQL Server; Native}`` stays in one piece.
    """
    parts: list[str] = []
    buffer: list[str] = []
    closer: str | None = None
    previous_significant = ""

    for char in raw:
        if closer is not None:
            buffer.append(char)
            if char == closer:
                closer = None
            continue
        if char in "{'\"" and previous_significant == "=":
            closer = "}" if char == "{" else char
            buffer.append(char)
            continue
        if char == ";":
            parts.append("".join(buffer))
            buffer = []
            previous_significant = ""
            continue
        buffer.append(char)
        if not char.isspace():
            previous_significant = char

    parts.append("".join(buffer))
    return parts


def redact_connection_string(raw: str | None) -> str | None:
    """Mask every credential-bearing element of a connection string.

    Handles the shapes that turn up in real workbooks: OLE DB ``Password=``, ODBC ``PWD=``
    and ``UID=``, Access ``Jet OLEDB:Database Password=``, Azure ``AccountKey=`` and
    ``SharedAccessSignature=``, tokens in a URL query string, and ``user:pass@host`` userinfo.
    User identities are masked as well as secrets: a service account name is an access-control
    fact, and the analysis artifact is not the place for it.

    Args:
        raw: The verbatim connection string, or ``None``.

    Returns:
        The string with every sensitive value replaced by :data:`REDACTED`, or ``None``.
    """
    if raw is None:
        return None
    if not raw.strip():
        return raw

    masked_parts: list[str] = []
    for part in _split_pairs(raw):
        key, separator, _value = part.partition("=")
        if separator and _is_sensitive_key(key):
            masked_parts.append(f"{key}={REDACTED}")
            continue
        masked_parts.append(part)

    masked = ";".join(masked_parts)
    masked = _SWEEP_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", masked)
    masked = _BEARER_RE.sub(lambda m: f"{m.group(1)} {REDACTED}", masked)
    return _URL_USERINFO_RE.sub(lambda m: f"{m.group(1)}{REDACTED}@", masked)


# ── XML helpers ──────────────────────────────────────────────────────────────────────────────


def _local(tag: str) -> str:
    """Return an XML tag's local name, discarding any namespace."""
    return tag.rsplit("}", maxsplit=1)[-1]


def _attr(element: ET.Element, name: str) -> str | None:
    """Read an attribute by name, ignoring namespace and case."""
    value = element.get(name)
    if value is not None:
        return value
    wanted = name.lower()
    for key, candidate in element.attrib.items():
        if _local(key).lower() == wanted:
            return candidate
    return None


def _child(element: ET.Element, name: str) -> ET.Element | None:
    return next((child for child in element if _local(child.tag) == name), None)


def _is_true(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "on", "yes"}


def _find_part(zf: zipfile.ZipFile, wanted: str) -> str | None:
    """Return the archive entry matching ``wanted``, case-insensitively."""
    lowered = wanted.lower()
    try:
        names = zf.namelist()
    except (OSError, RuntimeError, zipfile.BadZipFile):
        logger.warning("could not list the workbook archive while looking for %s", wanted)
        return None
    return next((name for name in names if name.lower() == lowered), None)


# ── parsing ──────────────────────────────────────────────────────────────────────────────────


def _kind(
    type_code: str | None, element: ET.Element, connection_string: str | None
) -> ConnectionKind:
    """Classify a connection, defaulting to `other` rather than dropping the unrecognised."""
    if type_code is not None:
        mapped = _TYPE_CODES.get(type_code.strip())
        if mapped is not None:
            return mapped

    if _child(element, "webPr") is not None:
        return "web"
    if _child(element, "textPr") is not None:
        return "text"

    text = (connection_string or "").strip().upper()
    if text.startswith("WORKSHEET"):
        return "worksheet"
    if text.startswith("ODBC") or "DRIVER=" in text or "DSN=" in text:
        return "odbc"
    if text.startswith("OLEDB") or "PROVIDER=" in text:
        return "oledb"
    return "other"


def _provider(connection_string: str | None) -> str | None:
    """Pull the OLE DB provider or the ODBC driver name out of a connection string."""
    if not connection_string:
        return None
    match = _PROVIDER_RE.search(connection_string)
    if match:
        return match.group(1).strip() or None
    match = _DRIVER_RE.search(connection_string)
    if match:
        return match.group(1).strip().strip("{}") or None
    return None


def _command_type(raw: str | None) -> str | None:
    if raw is None:
        return None
    stripped = raw.strip()
    return _COMMAND_TYPES.get(stripped, stripped) or None


def _parse_connection(element: ET.Element, ordinal: int) -> Connection:
    """Build one :class:`Connection`. The raw connection string never leaves this function."""
    db = _child(element, "dbPr")
    web = _child(element, "webPr")
    text = _child(element, "textPr")

    raw_string: str | None = None
    command: str | None = None
    command_type: str | None = None

    if db is not None:
        raw_string = _attr(db, "connection")
        command = _attr(db, "command")
        command_type = _command_type(_attr(db, "commandType"))
    if web is not None:
        raw_string = raw_string or _attr(web, "url")
        command = command or _attr(web, "post")
        command_type = command_type or "web_query"
    if text is not None:
        raw_string = raw_string or _attr(text, "sourceFile")
        command_type = command_type or "text_file"

    name = _attr(element, "name") or _attr(element, "id") or f"connection{ordinal}"
    return Connection(
        name=name,
        kind=_kind(_attr(element, "type"), element, raw_string),
        command=(command or None),
        command_type=command_type,
        provider=_provider(raw_string),
        connection_string_redacted=redact_connection_string(raw_string),
        refresh_on_load=_is_true(_attr(element, "refreshOnLoad")),
        description=_attr(element, "description"),
    )


def _unparseable(detail: str, remediation: str) -> Finding:
    return Finding(
        kind=FindingKind.UNPARSEABLE_PART,
        severity=Severity.WARNING,
        message=f"could not read {CONNECTIONS_PART}: {detail}",
        location=CONNECTIONS_PART,
        remediation=remediation,
    )


def extract_connections(
    zf: zipfile.ZipFile,
) -> tuple[list[Connection], ExtractionStatus, list[Finding]]:
    """Extract every legacy external data connection from an open workbook archive.

    A workbook with no ``xl/connections.xml`` is entirely normal: the result is an empty list,
    :attr:`ExtractionStatus.ABSENT`, and no findings. A part that exists but will not parse is
    :attr:`ExtractionStatus.UNPARSEABLE` plus one finding. Neither raises — a single malformed
    connection element costs that connection and produces a finding, and the rest still come
    back (CONVENTIONS non-negotiable 4).

    Args:
        zf: The open ``.xlsx`` archive.

    Returns:
        The connections found, the extractor's status, and any findings.
    """
    part = _find_part(zf, CONNECTIONS_PART)
    if part is None:
        logger.debug("no %s in this workbook", CONNECTIONS_PART)
        return [], ExtractionStatus.ABSENT, []

    try:
        raw = zf.read(part)
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
        logger.warning("could not read %s: %s", part, exc)
        return (
            [],
            ExtractionStatus.UNPARSEABLE,
            [
                _unparseable(
                    str(exc), "The workbook archive may be damaged; try re-saving it in Excel."
                )
            ],
        )

    if len(raw) > _MAX_PART_BYTES:
        detail = f"part is {len(raw)} bytes, above the {_MAX_PART_BYTES} byte ceiling"
        logger.warning("refusing to parse %s: %s", part, detail)
        return (
            [],
            ExtractionStatus.UNPARSEABLE,
            [_unparseable(detail, "Report this workbook as unusual.")],
        )

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        logger.warning("could not parse %s as XML: %s", part, exc)
        return (
            [],
            ExtractionStatus.UNPARSEABLE,
            [
                _unparseable(
                    f"not well-formed XML ({exc})",
                    "Open the workbook in Excel and re-save it to rewrite the part.",
                )
            ],
        )

    connections: list[Connection] = []
    findings: list[Finding] = []
    elements = [node for node in root.iter() if _local(node.tag) == "connection"]

    for ordinal, element in enumerate(elements, start=1):
        try:
            connections.append(_parse_connection(element, ordinal))
        except Exception as exc:  # a single bad element must not cost the rest
            logger.warning("skipping malformed connection %d in %s: %s", ordinal, part, exc)
            findings.append(
                Finding(
                    kind=FindingKind.UNPARSEABLE_PART,
                    severity=Severity.WARNING,
                    message=f"connection {ordinal} in {CONNECTIONS_PART} could not be read: {exc}",
                    location=CONNECTIONS_PART,
                    remediation="Inspect the connection in Excel under Data > Queries and Connections.",
                )
            )

    logger.info("extracted %d external connection(s) from %s", len(connections), part)
    return connections, ExtractionStatus.OK, findings
