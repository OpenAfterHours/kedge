"""The settings surface: the model endpoint, its API key, and which model to use.

The point of this panel is that a first run can be fixed from the browser rather than from a
terminal, so the tests care most about two things: that the key goes to the keyring and nowhere
near a config file, and that saving takes effect in the running process rather than only on the
next launch.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import keyring.errors
import pytest
from fastapi.testclient import TestClient

from kedge import config as config_module
from kedge.config import KEYRING_SERVICE, user_config_path
from kedge.server import settings as settings_module
from kedge.server.agent_seam import ScriptedAgent
from kedge.server.app import ServerState, create_hub_app
from kedge.server.sessions import SessionStore
from kedge.workspace import Workspace

SECRET = "sk-do-not-leak-this-value-0123456789"

# ── fixtures ─────────────────────────────────────────────────────────────────────────────────


class _FakeKeyring:
    """Stands in for the OS keyring. The real Windows Credential Manager is never touched."""

    def __init__(
        self, entries: dict[tuple[str, str], str] | None = None, error: Exception | None = None
    ) -> None:
        self.entries = entries or {}
        self._error = error

    def get_password(self, service: str, username: str) -> str | None:
        if self._error is not None:
            raise self._error
        return self.entries.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        if self._error is not None:
            raise self._error
        self.entries[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if self._error is not None:
            raise self._error
        if (service, username) not in self.entries:
            raise keyring.errors.PasswordDeleteError(username)
        del self.entries[(service, username)]


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "home"
    directory.mkdir()
    monkeypatch.setenv("KEDGE_HOME", str(directory))
    return directory


@pytest.fixture
def kr(monkeypatch: pytest.MonkeyPatch) -> _FakeKeyring:
    fake = _FakeKeyring()
    monkeypatch.setattr(config_module.keyring, "get_password", fake.get_password)
    monkeypatch.setattr(config_module.keyring, "set_password", fake.set_password)
    monkeypatch.setattr(config_module.keyring, "delete_password", fake.delete_password)
    return fake


@pytest.fixture
def client(home: Path, tmp_path: Path, kr: _FakeKeyring) -> Iterator[TestClient]:
    app = create_hub_app(
        store=SessionStore(tmp_path / "sessions.sqlite"),
        user_directory=home,
    )
    with TestClient(app) as opened:
        yield opened


def _state(client: TestClient) -> ServerState:
    return client.app.state.kedge


# ── reading ──────────────────────────────────────────────────────────────────────────────────


def test_settings_are_readable_on_the_hub_with_no_workbook_open(client: TestClient) -> None:
    """The first thing a new user needs is this panel, and the hub is where they are standing."""
    response = client.get("/api/settings/model")

    assert response.status_code == 200
    body = response.json()
    assert body["attached"] is False
    assert body["base_url"] == "https://api.openai.com/v1"
    assert body["model"] == "gpt-4o"
    assert body["api_key"]["status"] == "missing"
    assert body["config_path"] == str(user_config_path())


def test_reading_settings_never_returns_the_key(client: TestClient, kr: _FakeKeyring) -> None:
    kr.entries[(KEYRING_SERVICE, "default")] = SECRET

    response = client.get("/api/settings/model")

    assert response.json()["api_key"]["status"] == "present"
    assert SECRET not in response.text


def test_settings_report_the_defaults_so_the_panel_can_show_them(client: TestClient) -> None:
    body = client.get("/api/settings/model").json()

    assert body["defaults"]["base_url"] == "https://api.openai.com/v1"
    assert body["defaults"]["model"] == "gpt-4o"


def test_settings_name_the_project_file_that_overrides_them(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kedge.toml still wins over the file this panel writes, and saying so saves five minutes."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "kedge.toml").write_text('[model]\nmodel = "pinned"\n', encoding="utf-8")
    monkeypatch.chdir(project)

    body = client.get("/api/settings/model").json()

    assert body["model"] == "pinned"
    assert body["overridden_by_project"]["model"].endswith("kedge.toml")
    assert "base_url" not in body["overridden_by_project"]


# ── writing ──────────────────────────────────────────────────────────────────────────────────


def test_saving_writes_config_and_puts_the_key_in_the_keyring(
    client: TestClient, kr: _FakeKeyring
) -> None:
    response = client.put(
        "/api/settings/model",
        json={
            "base_url": "http://localhost:11434/v1",
            "model": "qwen2.5-coder:32b",
            "api_key": SECRET,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["base_url"] == "http://localhost:11434/v1"
    assert body["model"] == "qwen2.5-coder:32b"
    assert body["api_key"]["status"] == "present"

    assert kr.entries == {(KEYRING_SERVICE, "default"): SECRET}
    written = user_config_path().read_text(encoding="utf-8")
    assert "http://localhost:11434/v1" in written
    assert SECRET not in written, "the key must never reach a config file"
    assert SECRET not in response.text


def test_saving_normalises_the_base_url(client: TestClient) -> None:
    body = client.put("/api/settings/model", json={"base_url": "https://x.test/v1/"}).json()

    assert body["base_url"] == "https://x.test/v1"


def test_saving_one_field_leaves_the_others_alone(client: TestClient) -> None:
    client.put("/api/settings/model", json={"base_url": "https://x.test/v1", "model": "a-model"})

    body = client.put("/api/settings/model", json={"model": "b-model"}).json()

    assert body["model"] == "b-model"
    assert body["base_url"] == "https://x.test/v1"


def test_the_reasoning_effort_round_trips(client: TestClient) -> None:
    """A property of the model rather than of kedge, so it changes whenever the model does."""
    assert client.get("/api/settings/model").json()["reasoning_effort"] is None

    body = client.put("/api/settings/model", json={"reasoning_effort": "high"}).json()

    assert body["reasoning_effort"] == "high"
    assert "high" in body["reasoning_efforts"]
    assert client.get("/api/settings/model").json()["reasoning_effort"] == "high"


def test_clearing_the_reasoning_effort_is_not_the_same_as_setting_it_to_none(
    client: TestClient,
) -> None:
    """Absent means kedge never mentions reasoning; "none" means it asks for none of it.

    The distinction is the whole of the fallback: an endpoint that has never heard of the
    parameter needs it left out, not set to a value.
    """
    client.put("/api/settings/model", json={"reasoning_effort": "none"})
    assert client.get("/api/settings/model").json()["reasoning_effort"] == "none"

    body = client.put("/api/settings/model", json={"reasoning_effort": ""}).json()

    assert body["reasoning_effort"] is None


def test_an_unknown_reasoning_effort_is_refused_by_name(client: TestClient) -> None:
    response = client.put("/api/settings/model", json={"reasoning_effort": "extreme"})

    assert response.status_code == 400
    assert "reasoning_effort" in response.json()["detail"]


def test_an_empty_key_leaves_the_stored_one_alone(client: TestClient, kr: _FakeKeyring) -> None:
    """An empty password box is what a browser sends when nobody touched it."""
    kr.entries[(KEYRING_SERVICE, "default")] = SECRET

    body = client.put("/api/settings/model", json={"model": "a-model", "api_key": ""}).json()

    assert body["api_key"]["status"] == "present"
    assert kr.entries[(KEYRING_SERVICE, "default")] == SECRET


def test_a_bad_base_url_is_rejected_by_the_same_validator_as_a_hand_edited_file(
    client: TestClient,
) -> None:
    response = client.put("/api/settings/model", json={"base_url": "ftp://nope"})

    assert response.status_code == 400
    assert "must start with http" in response.json()["detail"]
    assert not user_config_path().exists()


def test_an_empty_model_name_is_rejected(client: TestClient) -> None:
    response = client.put("/api/settings/model", json={"model": "   "})

    assert response.status_code == 400
    assert "model" in response.json()["detail"]


def test_the_key_goes_under_a_renamed_entry(client: TestClient, kr: _FakeKeyring) -> None:
    body = client.put("/api/settings/model", json={"api_key_ref": "work", "api_key": SECRET}).json()

    assert body["api_key_ref"] == "work"
    assert body["api_key"]["status"] == "present"
    assert kr.entries == {(KEYRING_SERVICE, "work"): SECRET}


def test_an_unwritable_keyring_stops_the_save_rather_than_writing_a_dangling_reference(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Config naming an entry that was never created is worse than no change at all."""
    broken = _FakeKeyring(error=keyring.errors.KeyringError("no backend"))
    monkeypatch.setattr(config_module.keyring, "set_password", broken.set_password)

    response = client.put(
        "/api/settings/model", json={"base_url": "https://x.test/v1", "api_key": SECRET}
    )

    assert response.status_code == 502
    assert not user_config_path().exists()


def test_forgetting_the_key_removes_it_and_leaves_the_endpoint(
    client: TestClient, kr: _FakeKeyring
) -> None:
    client.put("/api/settings/model", json={"base_url": "https://x.test/v1", "api_key": SECRET})

    body = client.delete("/api/settings/model/key").json()

    assert body["api_key"]["status"] == "missing"
    assert body["base_url"] == "https://x.test/v1"
    assert kr.entries == {}


def test_forgetting_a_key_that_is_not_there_is_not_an_error(client: TestClient) -> None:
    assert client.delete("/api/settings/model/key").status_code == 200


# ── taking effect ────────────────────────────────────────────────────────────────────────────


def _attach(client: TestClient, tmp_path: Path, *, factory=None) -> Workspace:
    workbook = tmp_path / "processes" / "rwa.xlsx"
    workbook.parent.mkdir(parents=True, exist_ok=True)
    workbook.write_bytes(b"not really a workbook")
    workspace = Workspace.for_workbook(workbook)
    _state(client).attach(
        workspace, agent=ScriptedAgent(delay=0.0), demo=True, agent_factory=factory
    )
    return workspace


def test_saving_with_nothing_open_says_it_applies_on_the_next_open(client: TestClient) -> None:
    assert client.put("/api/settings/model", json={"model": "a"}).json()["applied"] == "next_open"


def test_saving_rebuilds_the_agent_so_demo_mode_can_be_climbed_out_of(
    client: TestClient, tmp_path: Path
) -> None:
    real = ScriptedAgent(delay=0.0)
    _attach(client, tmp_path, factory=lambda: real)
    assert _state(client).demo is True

    body = client.put("/api/settings/model", json={"model": "a-model", "api_key": SECRET}).json()

    assert body["applied"] == "now"
    assert body["demo"] is False
    assert _state(client).agent is real


def test_a_rebuild_that_fails_leaves_the_previous_agent_and_says_so(
    client: TestClient, tmp_path: Path
) -> None:
    """A server with a stale agent still answers; one with no agent at all does not."""
    from kedge.errors import KedgeError

    def explode() -> ScriptedAgent:
        raise KedgeError("no key")

    _attach(client, tmp_path, factory=explode)
    before = _state(client).agent

    body = client.put("/api/settings/model", json={"model": "a-model"}).json()

    assert body["applied"] == "unusable"
    assert body["demo"] is True
    assert _state(client).agent is before


def test_saving_makes_the_attached_workspace_re_read_its_config(
    client: TestClient, tmp_path: Path
) -> None:
    workspace = _attach(client, tmp_path)
    assert workspace.config.model.model == "gpt-4o"

    client.put("/api/settings/model", json={"model": "qwen2.5-coder:32b"})

    assert workspace.config.model.model == "qwen2.5-coder:32b"


# ── probing ──────────────────────────────────────────────────────────────────────────────────


def _fake_models(monkeypatch: pytest.MonkeyPatch, result: object) -> list[tuple[str, str]]:
    """Replace the outbound call, recording what it was asked for.

    ``ca_bundle`` is accepted rather than ignored because the probe is one of the four places
    that must verify against the configured trust (:mod:`kedge.tls`); a fake that swallowed
    ``**kwargs`` would let the route quietly stop passing it.
    """
    calls: list[tuple[str, str]] = []

    async def fake(base_url: str, api_key: str, *, ca_bundle: Path | None = None) -> list[str]:
        assert ca_bundle is None or isinstance(ca_bundle, Path)
        calls.append((base_url, api_key))
        if isinstance(result, Exception):
            raise result
        return list(result)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(settings_module, "fetch_model_names", fake)
    return calls


def test_probing_lists_the_models_an_endpoint_offers(
    client: TestClient, kr: _FakeKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    kr.entries[(KEYRING_SERVICE, "default")] = SECRET
    calls = _fake_models(monkeypatch, ["a", "b"])

    body = client.post(
        "/api/settings/model/probe", json={"base_url": "https://typed.test/v1"}
    ).json()

    assert body["ok"] is True
    assert body["models"] == ["a", "b"]
    assert calls == [("https://typed.test/v1", SECRET)], (
        "the endpoint the user has typed is probed, not the one already saved"
    )


def test_probing_uses_a_typed_key_that_has_not_been_saved(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_models(monkeypatch, ["a"])

    client.post("/api/settings/model/probe", json={"api_key": "sk-typed"})

    assert calls == [("https://api.openai.com/v1", "sk-typed")]


def test_probing_without_a_key_explains_rather_than_failing(client: TestClient) -> None:
    body = client.post("/api/settings/model/probe", json={}).json()

    assert body["ok"] is False
    assert body["source"] == "no_key"
    assert body["models"] == []


def test_an_endpoint_with_no_model_list_is_reported_not_raised(
    client: TestClient, kr: _FakeKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plenty of OpenAI-compatible servers do not implement /models (PLAN M6)."""
    kr.entries[(KEYRING_SERVICE, "default")] = SECRET
    _fake_models(monkeypatch, httpx.ConnectError("refused"))

    response = client.post("/api/settings/model/probe", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["source"] == "unreachable"
    assert "type a model name" in body["detail"].lower()


def test_probing_rejects_a_url_it_could_never_reach(client: TestClient) -> None:
    body = client.post("/api/settings/model/probe", json={"base_url": "ftp://nope"}).json()

    assert body["ok"] is False
    assert body["source"] == "invalid"


def test_probing_never_echoes_the_key(
    client: TestClient, kr: _FakeKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    kr.entries[(KEYRING_SERVICE, "default")] = SECRET
    _fake_models(monkeypatch, ["a"])

    response = client.post("/api/settings/model/probe", json={"api_key": SECRET})

    assert SECRET not in response.text
