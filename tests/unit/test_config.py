"""Tests for the layered config loader and keyring-backed API key access."""

from __future__ import annotations

import json
from pathlib import Path

import keyring.errors
import pytest

from kedge import config as config_module
from kedge.config import (
    KEYRING_SERVICE,
    Config,
    ConfigFileError,
    ConfigValidationError,
    MissingApiKeyError,
    api_key_status,
    get_api_key,
    keyring_set_command,
    load_config,
    user_config_path,
    user_dir,
)

SECRET = "sk-do-not-leak-this-value-0123456789"


@pytest.fixture
def kedge_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point KEDGE_HOME at a temporary directory so the real ~/.kedge is never touched."""
    home = tmp_path / "kedge-home"
    home.mkdir()
    monkeypatch.setenv("KEDGE_HOME", str(home))
    return home


@pytest.fixture
def project(tmp_path: Path) -> Path:
    directory = tmp_path / "project"
    directory.mkdir()
    return directory


class _FakeKeyring:
    """Stands in for the OS keyring. The real Windows Credential Manager is never touched."""

    def __init__(
        self, entries: dict[tuple[str, str], str] | None = None, error: Exception | None = None
    ) -> None:
        self._entries = entries or {}
        self._error = error

    def get_password(self, service: str, username: str) -> str | None:
        if self._error is not None:
            raise self._error
        return self._entries.get((service, username))


def _install_keyring(monkeypatch: pytest.MonkeyPatch, fake: _FakeKeyring) -> None:
    monkeypatch.setattr(config_module.keyring, "get_password", fake.get_password)


# ── layering ─────────────────────────────────────────────────────────────────────────────────


def test_defaults_load_when_no_config_files_exist(kedge_home: Path, project: Path) -> None:
    loaded = load_config(project_dir=project)

    assert loaded.files == ()
    assert loaded.config.sampling.max_rows == 100
    assert loaded.config.sampling.max_payload_bytes == 32_768
    assert loaded.config.ingest.copy_on_select is True
    assert loaded.config.redaction.enabled is False
    assert loaded.origin("sampling.max_rows") == "default"


def test_user_config_overrides_defaults(kedge_home: Path, project: Path) -> None:
    user_file = kedge_home / "config.toml"
    user_file.write_text(
        '[model]\nmodel = "gpt-5"\nbase_url = "https://llm.internal/v1"\n', encoding="utf-8"
    )

    loaded = load_config(project_dir=project)

    assert loaded.config.model.model == "gpt-5"
    assert loaded.config.model.base_url == "https://llm.internal/v1"
    assert loaded.origin("model.model") == str(user_file)
    assert loaded.origin("model.timeout_seconds") == "default"


def test_project_config_overrides_user_config_key_by_key(kedge_home: Path, project: Path) -> None:
    user_file = kedge_home / "config.toml"
    user_file.write_text(
        '[model]\nmodel = "gpt-5"\n\n[reconciliation]\nabsolute_tolerance = 0.01\n',
        encoding="utf-8",
    )
    project_file = project / "kedge.toml"
    project_file.write_text("[reconciliation]\nabsolute_tolerance = 0.5\n", encoding="utf-8")

    loaded = load_config(project_dir=project)

    # The project file wins where it speaks, and is silent everywhere else.
    assert loaded.config.reconciliation.absolute_tolerance == 0.5
    assert loaded.config.reconciliation.relative_tolerance == 0.0
    assert loaded.config.model.model == "gpt-5"
    assert loaded.files == (user_file, project_file)


def test_provenance_names_the_file_each_value_came_from(kedge_home: Path, project: Path) -> None:
    user_file = kedge_home / "config.toml"
    user_file.write_text("[sampling]\nmax_rows = 50\ntop_k = 3\n", encoding="utf-8")
    project_file = project / "kedge.toml"
    project_file.write_text("[sampling]\nmax_rows = 25\n", encoding="utf-8")

    loaded = load_config(project_dir=project)

    assert loaded.origin("sampling.max_rows") == str(project_file)
    assert loaded.origin("sampling.top_k") == str(user_file)
    assert loaded.origin("sampling.head_rows") == "default"


def test_explicit_paths_override_the_discovered_locations(tmp_path: Path, kedge_home: Path) -> None:
    elsewhere = tmp_path / "elsewhere.toml"
    elsewhere.write_text("[marimo]\nport = 2718\n", encoding="utf-8")

    loaded = load_config(user_path=elsewhere, project_path=tmp_path / "absent.toml")

    assert loaded.config.marimo.port == 2718
    assert loaded.files == (elsewhere,)


# ── error messages ───────────────────────────────────────────────────────────────────────────


def test_malformed_toml_names_the_file(kedge_home: Path, project: Path) -> None:
    bad = project / "kedge.toml"
    bad.write_text("[sampling\nmax_rows = 10\n", encoding="utf-8")

    with pytest.raises(ConfigFileError) as excinfo:
        load_config(project_dir=project)

    assert str(bad) in str(excinfo.value)
    assert "not valid TOML" in str(excinfo.value)


def test_unknown_key_names_the_file_and_the_key(kedge_home: Path, project: Path) -> None:
    bad = project / "kedge.toml"
    bad.write_text("[sampling]\nmax_row = 10\n", encoding="utf-8")

    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(project_dir=project)

    message = str(excinfo.value)
    assert str(bad) in message
    assert "sampling.max_row" in message
    assert "did you mean 'max_rows'" in message


def test_unknown_section_is_reported(kedge_home: Path, project: Path) -> None:
    bad = project / "kedge.toml"
    bad.write_text("[samplin]\nmax_rows = 10\n", encoding="utf-8")

    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(project_dir=project)

    assert "samplin" in str(excinfo.value)
    assert str(bad) in str(excinfo.value)


def test_invalid_value_names_the_file_and_the_key(kedge_home: Path, project: Path) -> None:
    bad = project / "kedge.toml"
    bad.write_text('[sampling]\nmax_rows = "lots"\n', encoding="utf-8")

    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(project_dir=project)

    message = str(excinfo.value)
    assert str(bad) in message
    assert "sampling.max_rows" in message


def test_out_of_range_value_is_rejected(kedge_home: Path, project: Path) -> None:
    bad = project / "kedge.toml"
    bad.write_text("[sampling]\nmax_rows = 0\n", encoding="utf-8")

    with pytest.raises(ConfigValidationError):
        load_config(project_dir=project)


def test_base_url_without_a_scheme_is_rejected(kedge_home: Path, project: Path) -> None:
    bad = project / "kedge.toml"
    bad.write_text('[model]\nbase_url = "llm.internal/v1"\n', encoding="utf-8")

    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(project_dir=project)

    assert "http" in str(excinfo.value)


def test_trailing_slash_is_stripped_from_base_url(kedge_home: Path, project: Path) -> None:
    (project / "kedge.toml").write_text(
        '[model]\nbase_url = "https://llm.internal/v1/"\n', encoding="utf-8"
    )

    loaded = load_config(project_dir=project)

    assert loaded.config.model.base_url == "https://llm.internal/v1"


def test_an_inline_api_key_is_refused_with_keyring_instructions(
    kedge_home: Path, project: Path
) -> None:
    bad = project / "kedge.toml"
    bad.write_text(f'[model]\napi_key = "{SECRET}"\n', encoding="utf-8")

    with pytest.raises(ConfigValidationError) as excinfo:
        load_config(project_dir=project)

    message = str(excinfo.value)
    assert "keyring" in message
    assert "uv run keyring set kedge" in message
    assert SECRET not in message, "the refusal must not echo the secret back"


# ── paths ────────────────────────────────────────────────────────────────────────────────────


def test_kedge_home_environment_variable_relocates_the_user_directory(kedge_home: Path) -> None:
    assert user_dir() == kedge_home
    assert user_config_path() == kedge_home / "config.toml"


def test_user_directory_defaults_to_dot_kedge_in_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("KEDGE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    assert user_dir() == tmp_path / ".kedge"


# ── the API key ──────────────────────────────────────────────────────────────────────────────


def test_get_api_key_reads_the_configured_keyring_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config.model_validate({"model": {"api_key_ref": "work-endpoint"}})
    _install_keyring(monkeypatch, _FakeKeyring({(KEYRING_SERVICE, "work-endpoint"): SECRET}))

    assert get_api_key(config) == SECRET


def test_missing_keyring_entry_names_the_exact_command_to_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = Config.model_validate({"model": {"api_key_ref": "work-endpoint"}})
    _install_keyring(monkeypatch, _FakeKeyring({}))

    with pytest.raises(MissingApiKeyError) as excinfo:
        get_api_key(config)

    message = str(excinfo.value)
    assert "uv run keyring set kedge work-endpoint" in message
    assert keyring_set_command("work-endpoint") in message


def test_unreachable_keyring_backend_is_reported_distinctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = Config()
    _install_keyring(monkeypatch, _FakeKeyring(error=keyring.errors.NoKeyringError("no backend")))

    with pytest.raises(MissingApiKeyError) as excinfo:
        get_api_key(config)

    assert "keyring" in str(excinfo.value)
    assert api_key_status(config) == "unavailable"


def test_api_key_status_reports_without_returning_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    config = Config()
    _install_keyring(monkeypatch, _FakeKeyring({(KEYRING_SERVICE, "default"): SECRET}))
    assert api_key_status(config) == "present"

    _install_keyring(monkeypatch, _FakeKeyring({}))
    assert api_key_status(config) == "missing"


def test_the_api_key_never_appears_in_the_config_object(
    monkeypatch: pytest.MonkeyPatch, kedge_home: Path, project: Path
) -> None:
    """The key is fetched on demand and never held on the config, so it cannot leak through it."""
    (project / "kedge.toml").write_text(
        '[model]\napi_key_ref = "work-endpoint"\n', encoding="utf-8"
    )
    _install_keyring(monkeypatch, _FakeKeyring({(KEYRING_SERVICE, "work-endpoint"): SECRET}))

    loaded = load_config(project_dir=project)
    config = loaded.config
    assert get_api_key(config) == SECRET

    assert SECRET not in repr(config)
    assert SECRET not in str(config)
    assert SECRET not in config.model_dump_json()
    assert SECRET not in json.dumps(config.model_dump(mode="json"))
    assert SECRET not in repr(loaded)
    assert "work-endpoint" in repr(config), (
        "the reference itself is not a secret and should be visible"
    )


def test_config_is_frozen() -> None:
    config = Config()

    with pytest.raises(Exception, match=r"frozen|immutable"):
        config.model.model = "gpt-5"  # ty: ignore[invalid-assignment]
