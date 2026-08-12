"""Certificate trust for the model endpoint.

The interesting tests here mint a throwaway certificate authority with ``trustme`` and serve a
real TLS socket from it, because the thing worth proving is not that :mod:`kedge.tls` calls the
functions we expect -- it is that a chain kedge has not been told about is *rejected*, and that
the same chain is *accepted* once ``ca_bundle`` names its authority. A mock of ``ssl`` cannot
fail that way round, which is exactly why it would be the wrong instrument: the failure this
module exists to fix is a verification outcome, so the test has to produce one.

The server side is a thread running a single blocking accept. It is deliberately dumb -- no
HTTP, no framing -- because every assertion is about whether the handshake completes.
"""

from __future__ import annotations

import socket
import ssl
import threading
from typing import TYPE_CHECKING

import httpx
import pytest
import trustme

from kedge import tls
from kedge.errors import ConfigError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytestmark = pytest.mark.filterwarnings("ignore::ResourceWarning")


# ── a real, throwaway TLS server ─────────────────────────────────────────────────────────────


class _Server:
    """A TLS listener that completes one handshake per connection and says nothing."""

    def __init__(self, authority: trustme.CA) -> None:
        self.host = "127.0.0.1"
        self._certificate = authority.issue_cert("localhost")
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((self.host, 0))
        self._listener.listen(8)
        self.port: int = self._listener.getsockname()[1]
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._certificate.configure_cert(self._context)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        """The endpoint a client should dial. ``localhost`` because that is the name on the cert."""
        return f"https://localhost:{self.port}"

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                raw, _ = self._listener.accept()
            except OSError:
                return
            try:
                with self._context.wrap_socket(raw, server_side=True) as secured:
                    secured.recv(1024)
                    secured.sendall(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
            except (OSError, ssl.SSLError):
                # A client that hung up mid-handshake is the point of half these tests.
                pass

    def close(self) -> None:
        self._stop.set()
        self._listener.close()
        self._thread.join(timeout=5)


@pytest.fixture
def authority() -> trustme.CA:
    return trustme.CA()


@pytest.fixture
def server(authority: trustme.CA) -> Iterator[_Server]:
    running = _Server(authority)
    yield running
    running.close()


@pytest.fixture
def ca_pem(authority: trustme.CA, tmp_path: Path) -> Path:
    path = tmp_path / "corporate-root.pem"
    authority.cert_pem.write_to_path(str(path))
    return path


# ── the mechanism, proved end to end ─────────────────────────────────────────────────────────


def test_an_unknown_authority_is_rejected(server: _Server) -> None:
    """The failure users report, reproduced. The throwaway CA stands in for the proxy's root:
    it is not in the OS store and not in certifi, so verification must refuse it."""
    with tls.client(timeout=10.0) as client, pytest.raises(httpx.ConnectError) as caught:
        client.get(server.url)

    assert tls.certificate_error(caught.value) is not None


def test_the_same_endpoint_is_accepted_once_ca_bundle_names_its_authority(
    server: _Server, ca_pem: Path
) -> None:
    """And the fix, proved on the same socket that just refused: nothing about the server
    changed, only what kedge was told to trust."""
    with tls.client(ca_bundle=ca_pem, timeout=10.0) as client:
        response = client.get(server.url)

    assert response.status_code == 204


async def test_the_async_client_verifies_the_same_way(server: _Server, ca_pem: Path) -> None:
    """The agent loop is async and the plan proposal is not; both must trust the same things."""
    async with tls.async_client(timeout=10.0) as strict:
        with pytest.raises(httpx.ConnectError) as caught:
            await strict.get(server.url)
    assert tls.certificate_error(caught.value) is not None

    async with tls.async_client(ca_bundle=ca_pem, timeout=10.0) as trusting:
        response = await trusting.get(server.url)
    assert response.status_code == 204


def test_a_bundle_does_not_also_admit_a_different_authority(
    server: _Server, tmp_path: Path
) -> None:
    """``ca_bundle`` replaces the trust anchors rather than widening them to anything nearby.
    A second CA's PEM must not verify the first CA's server, or the setting would be a way to
    accidentally trust everything that happens to be lying around."""
    other = trustme.CA()
    wrong = tmp_path / "unrelated-root.pem"
    other.cert_pem.write_to_path(str(wrong))

    with tls.client(ca_bundle=wrong, timeout=10.0) as client, pytest.raises(httpx.ConnectError):
        client.get(server.url)


def test_hostname_checking_stays_on(authority: trustme.CA, tmp_path: Path) -> None:
    """Trusting the authority is not the same as trusting any name it signs. A certificate
    issued for another host must still be refused when dialled as localhost."""
    elsewhere = _Server.__new__(_Server)
    elsewhere.host = "127.0.0.1"
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind((elsewhere.host, 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    authority.issue_cert("not-the-host-you-asked-for.example").configure_cert(context)

    def serve() -> None:
        try:
            raw, _ = listener.accept()
            with context.wrap_socket(raw, server_side=True):
                pass
        except (OSError, ssl.SSLError):
            pass

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    pem = tmp_path / "root.pem"
    authority.cert_pem.write_to_path(str(pem))

    try:
        with tls.client(ca_bundle=pem, timeout=10.0) as client, pytest.raises(httpx.ConnectError):
            client.get(f"https://localhost:{port}")
    finally:
        listener.close()
        thread.join(timeout=5)


# ── context construction ─────────────────────────────────────────────────────────────────────


def test_the_default_context_verifies_and_checks_hostnames() -> None:
    context = tls.ssl_context()

    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_the_default_context_reads_the_operating_system_store() -> None:
    """The whole point: not certifi alone. ``truststore`` is what consults SChannel on Windows,
    Security.framework on macOS and OpenSSL's paths on Linux."""
    import truststore

    assert isinstance(tls.ssl_context(), truststore.SSLContext)


def test_a_ca_bundle_context_also_verifies_and_checks_hostnames(ca_pem: Path) -> None:
    context = tls.ssl_context(ca_pem)

    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True


# ── configuration errors are loud ────────────────────────────────────────────────────────────


def test_a_missing_bundle_is_an_error_rather_than_a_silent_fallback(tmp_path: Path) -> None:
    """Falling back to the system store on a typo would restore the very error the setting was
    added to fix, and would do it invisibly."""
    with pytest.raises(ConfigError) as caught:
        tls.ssl_context(tmp_path / "typo.pem")

    assert "not a file" in str(caught.value)
    assert "ca_bundle" in str(caught.value)


def test_a_bundle_that_is_not_a_certificate_names_the_likely_cause(tmp_path: Path) -> None:
    """DER rather than PEM is the common mistake, so the message carries the conversion."""
    not_a_pem = tmp_path / "bundle.pem"
    not_a_pem.write_bytes(b"\x30\x82\x01\x0a this is DER, or nothing at all")

    with pytest.raises(ConfigError) as caught:
        tls.ssl_context(not_a_pem)

    assert "openssl x509 -inform der" in str(caught.value)


def test_a_bundle_path_with_a_tilde_is_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """IT hands out a path in the user's profile more often than not."""
    from pathlib import Path as RealPath

    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    authority = trustme.CA()
    authority.cert_pem.write_to_path(str(tmp_path / "root.pem"))

    trust = tls.describe(RealPath("~/root.pem"))

    assert trust.source == "ca_bundle"
    assert trust.detail == str(tmp_path / "root.pem")


# ── what doctor prints ───────────────────────────────────────────────────────────────────────


def test_describe_reports_the_system_store_by_default() -> None:
    trust = tls.describe(None)

    assert trust.source == "system"
    assert "operating system" in trust.detail


def test_describe_counts_the_roots_in_an_explicit_bundle(ca_pem: Path) -> None:
    trust = tls.describe(ca_pem)

    assert trust.source == "ca_bundle"
    assert trust.detail == str(ca_pem)
    assert trust.ca_count == 1


def test_describe_does_not_raise_on_the_store_that_will_not_enumerate() -> None:
    """A ``truststore`` context raises ``NotImplementedError`` from ``get_ca_certs()``, so the
    count has to come from somewhere else or not at all. ``None`` is a legitimate answer and
    doctor renders it by saying nothing, which is why this must not throw."""
    trust = tls.describe(None)

    assert trust.ca_count is None or trust.ca_count >= 0


# ── unwrapping the error httpx actually raises ───────────────────────────────────────────────


def test_a_certificate_failure_is_found_through_the_wrapping(server: _Server) -> None:
    """httpx buries the ssl exception under ConnectError; doctor needs it back out to know it is
    looking at interception rather than a dead host."""
    with tls.client(timeout=10.0) as client, pytest.raises(httpx.ConnectError) as caught:
        client.get(server.url)

    found = tls.certificate_error(caught.value)

    assert isinstance(found, ssl.SSLCertVerificationError)
    assert found.verify_message


def test_an_ordinary_connection_failure_is_not_mistaken_for_a_certificate_one() -> None:
    """Nothing listening is a different diagnosis and must not be given the proxy explanation.

    The port is bound and released to get one nothing is on, rather than picked: a low reserved
    port refuses on Linux but silently hangs on Windows, which turns the same intent into a
    ``ConnectTimeout`` on one platform and a ``ConnectError`` on the other. ``TransportError``
    covers both, and either way the claim is the same -- no certificate was involved.
    """
    spare = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    spare.bind(("127.0.0.1", 0))
    port = spare.getsockname()[1]
    spare.close()

    with tls.client(timeout=5.0) as client, pytest.raises(httpx.TransportError) as caught:
        client.get(f"https://127.0.0.1:{port}")

    assert tls.certificate_error(caught.value) is None


def test_the_unwrapper_terminates_on_a_self_referencing_cause() -> None:
    """Exception chains can be cyclic once they have been re-raised through each other, and a
    diagnostic that hangs is worse than one that says nothing."""
    first = ValueError("first")
    second = ValueError("second")
    first.__cause__ = second
    second.__cause__ = first

    assert tls.certificate_error(first) is None


# ── the command doctor hands the user ────────────────────────────────────────────────────────


def test_the_inspect_command_names_the_host_and_port() -> None:
    command = tls.inspect_command("https://models.internal.example:8443/v1")

    assert "models.internal.example:8443" in command
    assert "openssl" in command


def test_the_inspect_command_defaults_to_the_https_port() -> None:
    assert "api.openai.com:443" in tls.inspect_command("https://api.openai.com/v1")
