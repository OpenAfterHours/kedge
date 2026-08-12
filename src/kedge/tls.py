"""Certificate trust for the one connection kedge makes over TLS.

kedge talks to exactly one thing over TLS: the OpenAI-compatible model endpoint. The marimo
subprocess is loopback HTTP, the analyser never leaves the machine, and the report is
self-contained. So the whole outbound trust question is "do we trust the certificate the model
endpoint presents", and it is settled here and nowhere else.

**Why this module exists.** kedge's stated audience runs it inside financial institutions
(PLAN 2.9 makes the deployment single-user and local, but says nothing about the network in
between), and those machines sit behind a TLS-inspecting proxy. The proxy terminates the
connection and re-signs it with a corporate root that IT pushes into the operating system's
trust store. Python does not look there: ``httpx`` and the ``openai`` SDK both default to the
``certifi`` bundle, which is a fixed list of public roots and will never contain it. The result
is ``SSLCertVerificationError: unable to get local issuer certificate`` on the first model call,
with nothing in the message to suggest that the machine already trusts the certificate and only
Python disagrees.

**How it is settled.** Two sources, in order:

1. ``[model] ca_bundle`` in config, when set -- an explicit PEM, usually handed out by IT.
2. Otherwise the operating system's trust store, via ``truststore``, which asks SChannel on
   Windows, Security.framework on macOS and OpenSSL's default paths on Linux.

The system store is also supplemented with ``certifi``. That matters on Linux, where
``truststore`` delegates to OpenSSL's default verify paths: on a desktop those are populated by
the distribution, but in a stripped container they can be empty, and falling back to no roots at
all would be a worse failure than the one this module fixes. Supplementing is safe in the other
direction too -- adding public roots to a store that already has them changes nothing.

**There is no way to turn verification off, and there should not be.** The tempting fix for a
certificate error is ``verify=False``, and it is the wrong one: it is invisible in a config file
six months later, it applies to every future connection rather than the one that was failing,
and it discards the interception proxy's own guarantees along with everyone else's. A site that
genuinely cannot get its root into the OS store points ``ca_bundle`` at a PEM instead, which is
explicit, greppable, and reviewable. See ``SECURITY.md``.

Nothing in kedge ever connects without verifying, including the diagnostics: ``kedge doctor``
explains a certificate failure and hands the user :func:`inspect_command` to identify the signer
themselves, rather than completing an unverified handshake to read it for them.
"""

from __future__ import annotations

import logging
import ssl
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

import certifi
import httpx
import truststore

from kedge.errors import ConfigError

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "Trust",
    "TrustSource",
    "async_client",
    "certificate_error",
    "client",
    "describe",
    "inspect_command",
    "ssl_context",
]

TrustSource = Literal["ca_bundle", "system"]
"""Where the roots kedge verifies against came from."""


@dataclass(frozen=True, slots=True)
class Trust:
    """What :func:`ssl_context` would trust, in a shape ``kedge doctor`` can print."""

    source: TrustSource
    detail: str
    """Human-readable: the bundle path, or the platform store being consulted."""
    ca_count: int | None
    """Roots kedge can count. ``None`` where the store will not enumerate -- a ``truststore``
    context raises ``NotImplementedError`` from ``get_ca_certs()``, because on Windows and macOS
    the verification happens inside the platform and there is no list to hand back."""


def ssl_context(ca_bundle: Path | None = None) -> ssl.SSLContext:
    """Build the context every outbound model call verifies against.

    Args:
        ca_bundle: An explicit PEM to trust instead of the system store, or ``None``.

    Returns:
        A client-side context with hostname checking and verification both on.

    Raises:
        ConfigError: ``ca_bundle`` is set but missing, unreadable, or not a valid PEM. Failing
            here is deliberate: silently falling back to the system store would mean a typo in a
            path quietly restores the very error the setting was added to fix.
    """
    if ca_bundle is not None:
        context = ssl.create_default_context(cafile=str(_readable_bundle(ca_bundle)))
        logger.debug("model endpoint trust: ca_bundle %s", ca_bundle)
        return context

    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # Supplement rather than replace. See the module docstring: this is the Linux case, where
    # truststore defers to OpenSSL's default paths and those can legitimately be empty.
    context.load_verify_locations(certifi.where())
    logger.debug("model endpoint trust: operating system store, supplemented with certifi")
    return context


def describe(ca_bundle: Path | None = None) -> Trust:
    """Describe the trust :func:`ssl_context` would apply, without opening a connection.

    Raises:
        ConfigError: As :func:`ssl_context`.
    """
    if ca_bundle is not None:
        resolved = _readable_bundle(ca_bundle)
        counted = ssl.create_default_context(cafile=str(resolved))
        return Trust(source="ca_bundle", detail=str(resolved), ca_count=len(counted.get_ca_certs()))

    detail = "operating system trust store, supplemented with certifi"
    count: int | None = None
    if hasattr(ssl, "enum_certificates"):
        # Windows only, and only an indication: SChannel decides, not this number. Worth showing
        # anyway, because "70 roots" versus "0 roots" is the whole diagnosis on a locked-down box.
        try:
            count = len(ssl.enum_certificates("ROOT"))
        except OSError as exc:  # pragma: no cover - a store that will not enumerate
            logger.debug("could not enumerate the Windows ROOT store: %s", exc)
    return Trust(source="system", detail=detail, ca_count=count)


def client(*, ca_bundle: Path | None = None, timeout: float | None = None) -> httpx.Client:
    """A synchronous client that verifies the model endpoint correctly.

    Raises:
        ConfigError: As :func:`ssl_context`.
    """
    return httpx.Client(verify=ssl_context(ca_bundle), timeout=timeout)


def async_client(
    *, ca_bundle: Path | None = None, timeout: float | None = None
) -> httpx.AsyncClient:
    """An asynchronous client that verifies the model endpoint correctly.

    Raises:
        ConfigError: As :func:`ssl_context`.
    """
    return httpx.AsyncClient(verify=ssl_context(ca_bundle), timeout=timeout)


def certificate_error(exc: BaseException) -> ssl.SSLCertVerificationError | None:
    """Return the certificate failure inside ``exc``, or ``None`` if it is a different problem.

    httpx wraps the original ``ssl`` exception in a ``ConnectError``, so the interesting type is
    one or more ``__cause__`` links down. Walking the chain rather than matching on the message
    keeps this working when the wording changes.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, ssl.SSLCertVerificationError):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


def inspect_command(base_url: str) -> str:
    """Return a command the user can run to see who signed the certificate they are getting.

    Naming the issuer is the fastest way to end the confusion -- it is normally the user's own
    proxy vendor, at which point nothing is mysterious any more. kedge deliberately does not go
    and read it: doing so means completing a handshake with verification switched off, and no
    stdlib API on 3.12 or 3.13 will then decode the issuer out of the DER without pulling in a
    certificate-parsing dependency that is absent on Windows, which is where this problem is
    most common. Trading kedge's "nothing here ever connects unverified" property for a string
    we can tell the user how to obtain in one line is a bad trade, so this is that line.
    """
    split = urlsplit(base_url)
    host = split.hostname or base_url
    port = split.port or 443
    return f"openssl s_client -showcerts -connect {host}:{port} </dev/null 2>&1 | openssl x509 -noout -issuer"


def _readable_bundle(ca_bundle: Path) -> Path:
    """Resolve and validate an explicit PEM.

    Raises:
        ConfigError: The path does not resolve to a readable, parseable certificate bundle.
    """
    resolved = ca_bundle.expanduser()
    if not resolved.is_file():
        msg = (
            f"model.ca_bundle points at {resolved}, which is not a file. It should be a PEM "
            f"holding the certificate authority that signs your endpoint's certificate -- the "
            f"one your IT department issues for the TLS-inspecting proxy."
        )
        raise ConfigError(msg)
    try:
        ssl.create_default_context(cafile=str(resolved))
    except (OSError, ssl.SSLError) as exc:
        msg = (
            f"model.ca_bundle at {resolved} could not be read as a certificate bundle: {exc}. "
            f"It must be PEM, not DER -- a DER file converts with "
            f"`openssl x509 -inform der -in <file> -out <file>.pem`."
        )
        raise ConfigError(msg) from exc
    return resolved
