"""Read marimo's local server registry — for diagnostics only, never to pick a server to drive.

marimo writes a small JSON file per running server so external tools can find one. kedge does not
want to find one. CONVENTIONS non-negotiable 5 and PLAN 2.9 are explicit: kedge owns its own
marimo process, targets it by url and session id recorded in its own marker file, and never
attaches to a server it did not start. Anyone running kedge plausibly has other marimo notebooks
open for unrelated work, and quietly editing one of those would be the worst bug this project
could ship.

Two facts make that boundary structural rather than a matter of discipline.

**A kedge server is never in the registry.** Verified against 0.23.15: the registry is written
only for servers started **without** an auth token. kedge always launches with
``--token-password``, so its own process never appears here. Anything this module lists is by
definition somebody else's.

**A registry entry can never be driven.** The registry deliberately contains no auth token, and
:class:`RegisteredServer` deliberately exposes no ``base_url`` — the on-disk record does carry
one (docs/marimo-api.md 7.4), and it is dropped on the way in rather than merely left unused.
Every kedge server requires a bearer token that only kedge holds, so there is no path from an
entry read here to a request the kernel would accept. This module contains no HTTP client and
imports none: :mod:`kedge.marimo_http` is the only module that talks to a marimo server, and
nothing here can hand it an address.

What it is genuinely for: ``kedge doctor`` answering "something is already listening on the port
I wanted" and "here is what else is running", with a pid the user can act on themselves.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

__all__ = [
    "REGISTRY_DIR_NAME",
    "RegisteredServer",
    "registered_servers",
    "registry_dir",
    "server_on_port",
]

REGISTRY_DIR_NAME = "servers"


@dataclass(frozen=True, slots=True)
class RegisteredServer:
    """One entry from marimo's server registry.

    Diagnostic only. There is deliberately no ``base_url`` property and no method that would
    build a request: an entry describes a process the user might want to know about, not an
    endpoint kedge may talk to.

    Example:
        >>> RegisteredServer(server_id="127.0.0.1:2718", pid=42, host="127.0.0.1", port=2718).describe()
        'marimo 127.0.0.1:2718 on port 2718 (pid 42)'
    """

    server_id: str
    pid: int
    host: str
    port: int
    started_at: str = ""
    version: str = ""
    path: Path | None = None

    def describe(self) -> str:
        """A one-line description for ``kedge doctor`` output."""
        version = f"marimo {self.version} " if self.version else "marimo "
        return f"{version}{self.server_id} on port {self.port} (pid {self.pid})"

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, path: Path | None = None) -> RegisteredServer | None:
        """Build an entry from its on-disk form, or return ``None`` if it is unusable.

        Never raises. A registry written by a different marimo version is somebody else's
        artifact, and a malformed one must not be able to break ``kedge doctor``.
        """
        try:
            return cls(
                server_id=str(raw["server_id"]),
                pid=int(raw["pid"]),
                host=str(raw["host"]),
                port=int(raw["port"]),
                started_at=str(raw.get("started_at", "")),
                version=str(raw.get("version", "")),
                path=path,
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug("ignoring a registry entry that does not parse: %s", exc)
            return None


def registry_dir(home: Path | None = None) -> Path:
    """Return the directory marimo writes its server registry to.

    marimo follows the XDG state convention on POSIX and puts the directory straight under the
    home directory on Windows, which is kedge's primary platform.

    Args:
        home: Override the home directory. For tests.

    Returns:
        ``~/.marimo/servers`` on Windows, ``${XDG_STATE_HOME:-~/.local/state}/marimo/servers``
        elsewhere. The directory need not exist.
    """
    base = Path.home() if home is None else home
    if os.name == "posix":
        configured = os.environ.get("XDG_STATE_HOME", "").strip()
        state = Path(configured) if configured else base / ".local" / "state"
        return state / "marimo" / REGISTRY_DIR_NAME
    return base / ".marimo" / REGISTRY_DIR_NAME


def _iter_registered_servers(home: Path | None = None) -> Iterator[RegisteredServer]:
    directory = registry_dir(home)
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("ignoring unreadable registry file %s: %s", path, exc)
            continue
        if not isinstance(raw, dict):
            logger.debug("ignoring registry file %s: expected a JSON object", path)
            continue
        entry = RegisteredServer.from_dict(raw, path=path)
        if entry is not None:
            yield entry


def registered_servers(home: Path | None = None) -> tuple[RegisteredServer, ...]:
    """Return every marimo server in the local registry.

    Remember what this list is and is not. It is every ``--no-token`` marimo on this machine,
    which is to say every marimo kedge did **not** start. It is not a list of candidates to
    attach to, and there is no supported way to turn an entry into a driveable session.
    """
    servers = tuple(_iter_registered_servers(home))
    logger.debug("marimo server registry lists %d server(s)", len(servers))
    return servers


def server_on_port(port: int, home: Path | None = None) -> RegisteredServer | None:
    """Return the registered server holding ``port``, if the registry knows of one.

    Answers "who has the port I wanted" when a launch fails to bind. A ``None`` result means only
    that the registry has no entry — a token-protected marimo, or any other program, holds ports
    without appearing here.
    """
    for server in _iter_registered_servers(home):
        if server.port == port:
            return server
    return None
