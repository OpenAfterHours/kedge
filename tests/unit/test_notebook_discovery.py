"""Tests for reading marimo's server registry, and for the boundary that makes it safe.

Two kinds of test here, and the second kind is the important one.

The first kind is ordinary parsing: the registry is somebody else's artifact, written by whatever
marimo version the user happens to be running, so every extractor degrades to "ignore this entry"
rather than raising. A malformed file must not be able to break ``kedge doctor``.

The second kind pins CONVENTIONS non-negotiable 5 -- **never auto-discover a marimo server** --
as a structural property rather than a matter of discipline. The registry is readable for
diagnostics only, and the tests below assert that there is no path from an entry read here to a
request kedge could send: the on-disk record's ``base_url`` is dropped on the way in, nothing can
be bolted onto the frozen result, this module imports no HTTP client, and the modules that do
speak HTTP cannot reach this one however the import is spelled -- including through the names
``kedge/notebook/__init__.py`` re-exports, which never mention ``discovery`` at all. A test that
only checked the happy path would let a future convenience property quietly open that door.
"""

from __future__ import annotations

import ast
import json
import os
from contextlib import suppress
from dataclasses import FrozenInstanceError
from importlib import import_module
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from kedge.notebook import discovery
from kedge.notebook.discovery import (
    REGISTRY_DIR_NAME,
    RegisteredServer,
    registered_servers,
    registry_dir,
    server_on_port,
)

# The full on-disk shape, docs/marimo-api.md 7.4. Note ``base_url``: marimo records one, and
# kedge's job is to not keep it.
ENTRY: dict[str, Any] = {
    "server_id": "127.0.0.1:2718",
    "pid": 4242,
    "host": "127.0.0.1",
    "port": 2718,
    "base_url": "http://127.0.0.1:2718",
    "started_at": "2026-01-01T09:00:00+00:00",
    "version": "0.23.15",
}


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty registry directory, wired up for whichever platform the test is running on."""
    home = tmp_path / "home"
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    directory = (
        home / ".marimo" / REGISTRY_DIR_NAME
        if os.name != "posix"
        else home / ".local" / "state" / "marimo" / REGISTRY_DIR_NAME
    )
    directory.mkdir(parents=True)
    monkeypatch.setattr(discovery.Path, "home", staticmethod(lambda: home))
    return directory


def _write(directory: Path, name: str, payload: Any) -> Path:
    path = directory / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _pretend_posix(monkeypatch: pytest.MonkeyPatch, **environ: str) -> None:
    """Make ``discovery`` believe it is on POSIX without telling the rest of the interpreter.

    Patching ``os.name`` globally would be simpler and quite wrong: ``pathlib`` reads it when it
    decides whether to build a ``WindowsPath`` or a ``PosixPath``, so a Windows run would start
    raising ``UnsupportedOperation`` from the very expression under test. Replacing the module's
    own reference keeps the lie local to the code that is meant to see it.
    """
    monkeypatch.setattr(discovery, "os", SimpleNamespace(name="posix", environ=environ))


# ── where the registry lives ─────────────────────────────────────────────────────────────────


def test_the_registry_is_under_the_home_directory_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discovery, "os", SimpleNamespace(name="nt", environ={}))

    assert registry_dir(tmp_path) == tmp_path / ".marimo" / "servers"


def test_the_registry_follows_xdg_state_home_on_posix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pretend_posix(monkeypatch, XDG_STATE_HOME=str(tmp_path / "state"))

    assert registry_dir(tmp_path) == tmp_path / "state" / "marimo" / "servers"


def test_an_unset_or_blank_xdg_state_home_falls_back_to_local_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = tmp_path / ".local" / "state" / "marimo" / "servers"

    _pretend_posix(monkeypatch)
    assert registry_dir(tmp_path) == expected

    _pretend_posix(monkeypatch, XDG_STATE_HOME="   ")
    assert registry_dir(tmp_path) == expected


def test_the_registry_directory_is_resolved_without_being_created(tmp_path: Path) -> None:
    """kedge reads this directory; creating it would be writing to marimo's private state."""
    directory = registry_dir(tmp_path)

    assert not directory.exists()


# ── reading entries ──────────────────────────────────────────────────────────────────────────


def test_a_missing_registry_lists_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discovery.Path, "home", staticmethod(lambda: tmp_path / "nowhere"))

    assert registered_servers() == ()


def test_an_empty_registry_lists_nothing(registry: Path) -> None:
    assert registered_servers() == ()


def test_an_entry_is_read_with_the_file_it_came_from(registry: Path) -> None:
    path = _write(registry, "127.0.0.1_2718.json", ENTRY)

    (server,) = registered_servers()

    assert server.server_id == "127.0.0.1:2718"
    assert server.pid == 4242
    assert server.host == "127.0.0.1"
    assert server.port == 2718
    assert server.started_at == "2026-01-01T09:00:00+00:00"
    assert server.version == "0.23.15"
    assert server.path == path


def test_the_optional_fields_default_to_empty(registry: Path) -> None:
    """A registry written by a different marimo need not carry everything this one does."""
    _write(registry, "a.json", {k: ENTRY[k] for k in ("server_id", "pid", "host", "port")})

    (server,) = registered_servers()

    assert server.started_at == ""
    assert server.version == ""


def test_entries_are_listed_in_a_stable_order(registry: Path) -> None:
    for port in (2720, 2718, 2719):
        _write(registry, f"127.0.0.1_{port}.json", {**ENTRY, "port": port})

    assert [server.port for server in registered_servers()] == [2718, 2719, 2720]


def test_a_home_override_is_honoured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    directory = registry_dir(tmp_path)
    directory.mkdir(parents=True)
    _write(directory, "a.json", ENTRY)

    assert len(registered_servers(tmp_path)) == 1


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("not-json.json", "{oh dear"),
        ("a-list.json", "[1, 2, 3]"),
        ("a-string.json", '"a string"'),
        ("null.json", "null"),
        ("empty.json", ""),
    ],
)
def test_a_file_that_is_not_an_object_is_ignored(registry: Path, name: str, content: str) -> None:
    (registry / name).write_text(content, encoding="utf-8")

    assert registered_servers() == ()


@pytest.mark.parametrize(
    "payload",
    [
        {"pid": 1, "host": "h", "port": 1},  # no server_id
        {"server_id": "s", "host": "h", "port": 1},  # no pid
        {"server_id": "s", "pid": "not a number", "host": "h", "port": 1},
        {"server_id": "s", "pid": 1, "host": "h", "port": "not a number"},
        {"server_id": "s", "pid": None, "host": "h", "port": 1},
    ],
    ids=["no-server-id", "no-pid", "pid-not-a-number", "port-not-a-number", "pid-null"],
)
def test_an_entry_that_does_not_parse_is_ignored(registry: Path, payload: Any) -> None:
    """Absent and unparseable are first-class results (CONVENTIONS non-negotiable 4)."""
    _write(registry, "broken.json", payload)

    assert registered_servers() == ()


def test_one_broken_entry_does_not_hide_the_good_ones(registry: Path) -> None:
    _write(registry, "a-broken.json", {"nothing": "useful"})
    _write(registry, "b-good.json", ENTRY)

    assert [server.port for server in registered_servers()] == [2718]


def test_from_dict_reports_failure_by_returning_none_not_by_raising() -> None:
    assert RegisteredServer.from_dict({}) is None
    assert RegisteredServer.from_dict({"server_id": "s", "pid": 1, "host": "h"}) is None


def test_a_directory_that_is_unreadable_is_skipped_rather_than_fatal(registry: Path) -> None:
    """``*.json`` can match a directory; reading one raises OSError, which is not our problem."""
    (registry / "a-directory.json").mkdir()
    _write(registry, "b-good.json", ENTRY)

    assert [server.port for server in registered_servers()] == [2718]


# ── finding the holder of a port ─────────────────────────────────────────────────────────────


def test_server_on_port_answers_who_has_the_port_we_wanted(registry: Path) -> None:
    _write(registry, "a.json", {**ENTRY, "port": 2718})
    _write(registry, "b.json", {**ENTRY, "port": 2719})

    found = server_on_port(2719)

    assert found is not None
    assert found.port == 2719


def test_server_on_port_is_none_when_the_registry_has_no_entry(registry: Path) -> None:
    """A token-protected marimo -- which is what kedge launches -- holds a port invisibly."""
    _write(registry, "a.json", ENTRY)

    assert server_on_port(9999) is None


def test_describe_is_one_line_naming_the_port_and_the_pid() -> None:
    server = RegisteredServer(server_id="127.0.0.1:2718", pid=42, host="127.0.0.1", port=2718)

    assert server.describe() == "marimo 127.0.0.1:2718 on port 2718 (pid 42)"


def test_describe_includes_the_version_when_the_entry_carries_one() -> None:
    server = RegisteredServer(
        server_id="127.0.0.1:2718", pid=42, host="127.0.0.1", port=2718, version="0.23.15"
    )

    assert server.describe() == "marimo 0.23.15 127.0.0.1:2718 on port 2718 (pid 42)"


# ── never auto-discover a marimo server ──────────────────────────────────────────────────────


def test_the_recorded_base_url_is_dropped_rather_than_merely_unused(registry: Path) -> None:
    """marimo writes a base_url into every entry. Keeping it would be an address to attach to."""
    _write(registry, "a.json", ENTRY)

    (server,) = registered_servers()

    assert not hasattr(server, "base_url")
    assert not hasattr(server, "url")
    assert ENTRY["base_url"] not in repr(server)


def test_an_entry_carries_nothing_that_could_authenticate_a_request(registry: Path) -> None:
    """The registry never contains a token; even if a future marimo added one, it is not read."""
    _write(registry, "a.json", {**ENTRY, "token": "hunter2", "access_token": "hunter2"})

    (server,) = registered_servers()

    assert "hunter2" not in repr(server)
    assert not hasattr(server, "token")
    assert not hasattr(server, "access_token")


def test_an_address_cannot_be_bolted_onto_an_entry_afterwards() -> None:
    """Frozen and slotted, so "just add a base_url at the call site" is not available.

    ``slots=True`` is what makes it structural: there is no instance ``__dict__`` for a new
    attribute to land in, so the refusal is not a matter of anyone remembering to be careful.
    """
    server = RegisteredServer(server_id="s", pid=1, host="127.0.0.1", port=2718)

    assert not hasattr(server, "__dict__")
    assert "base_url" not in RegisteredServer.__slots__

    with pytest.raises(FrozenInstanceError):
        server.port = 9999  # ty: ignore[invalid-assignment]

    # Setting a name that is not a field is refused too, though CPython raises TypeError rather
    # than FrozenInstanceError for it: `dataclass(frozen=True, slots=True)` rebuilds the class,
    # and the generated __setattr__ still closes over the original. Refused either way.
    with pytest.raises((TypeError, AttributeError)):
        server.base_url = "http://127.0.0.1:2718"  # ty: ignore[unresolved-attribute]


def test_discovery_imports_no_http_client() -> None:
    """kedge.marimo_http is the only module that talks to a marimo server (review finding 2)."""
    imported = _imported_modules(discovery)

    assert not {name for name in imported if name.split(".")[0] in {"httpx", "requests", "urllib"}}
    assert not _names_reaching(imported, "kedge.marimo_http")


@pytest.mark.parametrize(
    "module", ["kedge.lifecycle", "kedge.marimo_http", "kedge.notebook.kernel"]
)
def test_nothing_that_speaks_to_a_server_consults_the_registry(module: str) -> None:
    """The boundary runs both ways: a module that can reach marimo must not learn of one here."""
    reached = _names_reaching(_imported_modules(import_module(module)), "kedge.notebook.discovery")

    assert not reached, f"{module} can see the registry via {sorted(reached)}"


@pytest.mark.parametrize(
    "statement",
    [
        "import kedge.notebook.discovery",
        "import kedge.notebook.discovery as d",
        "from kedge.notebook.discovery import registered_servers",
        "from kedge.notebook import discovery",
        "from kedge.notebook import discovery as d",
        "from kedge.notebook import registered_servers",
        "from kedge.notebook import RegisteredServer",
        "from . import discovery",
        "from .discovery import registered_servers",
        "from ..notebook.discovery import server_on_port",
    ],
)
def test_the_guard_sees_every_way_of_reaching_the_registry(statement: str) -> None:
    """A guard is only worth what it catches, so the ways round it are enumerated here.

    Two doors, and neither is hypothetical. ``from kedge.notebook import discovery`` names the
    module in the alias rather than in the statement's ``module`` -- this very file imports it
    that way -- and ``from kedge.notebook import registered_servers`` never spells the submodule
    at all, because ``kedge/notebook/__init__.py`` re-exports discovery's three public names.
    An earlier version of this helper looked only at the left-hand side of an ``ImportFrom`` and
    waved every one of these through. The relative forms are the same doors from inside the
    package.
    """
    imported = _imports_in(statement + "\n", package="kedge.notebook")

    assert _names_reaching(imported, "kedge.notebook.discovery")


@pytest.mark.parametrize(
    "statement",
    ["import httpx", "import httpx as h", "from httpx import AsyncClient", "import urllib.request"],
)
def test_the_guard_sees_every_way_of_naming_an_http_client(statement: str) -> None:
    """The other half of the same helper, filtered the way its caller filters it."""
    found = _imports_in(statement + "\n", package="kedge.notebook")

    assert {name for name in found if name.split(".")[0] in {"httpx", "requests", "urllib"}}


def _imported_modules(module: ModuleType) -> set[str]:
    """Every module named by an import statement in ``module``'s source."""
    source = Path(str(module.__file__)).read_text(encoding="utf-8")
    return _imports_in(source, package=module.__package__ or "")


def _imports_in(source: str, *, package: str) -> set[str]:
    """Every dotted name a source file imports, by AST rather than by grep.

    A docstring that mentions ``httpx`` to explain why this module does not use one is not an
    import, and the same reasoning is why ``scripts/guardrails.py`` parses rather than greps.

    Each ``from X import y`` contributes ``X.y`` as well as ``X``, because a submodule is named
    in the alias and not in the statement's ``module``. Whether ``X.y`` is a submodule or a
    function is left to :func:`_names_reaching`. Relative imports are resolved against
    ``package``: inside ``kedge/notebook`` they open exactly the same doors.
    """
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _absolute_module(node, package)
            if not base:
                continue
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
    return names


def _absolute_module(node: ast.ImportFrom, package: str) -> str:
    """Resolve ``from .x import y``'s ``.x`` against the importing module's package."""
    if not node.level:
        return node.module or ""
    parts = package.split(".") if package else []
    anchor = ".".join(parts[: len(parts) - node.level + 1])
    return f"{anchor}.{node.module}" if node.module else anchor


def _names_reaching(imported: set[str], target: str) -> set[str]:
    """The subset of ``imported`` that leads to something ``target`` defines.

    Matching the dotted path is not enough. ``kedge/notebook/__init__.py`` re-exports
    ``registered_servers``, so ``from kedge.notebook import registered_servers`` reaches the
    registry without ever spelling ``discovery``, and a path check would pass it. Each name is
    therefore resolved against the live package: an object whose ``__module__`` is ``target``
    came from ``target``, however the import was spelled. Only names under ``kedge`` are
    resolved, so a guard never imports a third-party package to answer the question.
    """
    reaching = set()
    for name in imported:
        if name == target or name.startswith(f"{target}."):
            reaching.add(name)
            continue
        owner, _, attribute = name.rpartition(".")
        if not owner.startswith("kedge"):
            continue
        with suppress(ImportError):
            found = getattr(import_module(owner), attribute, None)
            if getattr(found, "__module__", None) == target:
                reaching.add(name)
    return reaching
