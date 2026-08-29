"""Layered configuration: ``~/.kedge/config.toml`` overridden by ``./kedge.toml``.

Two files, three layers. Built-in defaults sit underneath a machine-wide user file, which is in
turn overridden by a per-project file next to the workbook. Machine-shaped settings (model
endpoint, keyring entry) belong in the user file; process-shaped settings (tolerances, redaction
rules, sampling caps) belong in the project file, because they are properties of the process
being converted rather than of the machine converting it (PLAN 2.9).

Loading records where every value came from, so ``kedge config`` can show the resolved value
*and* the file responsible for it. Validation failures name the offending file and key rather
than emitting a raw pydantic traceback.

The model endpoint's API key is never held here. Config carries ``api_key_ref``, the name of an
entry in the OS keyring (Windows Credential Manager); :func:`get_api_key` fetches it on demand
and it is never stored on, logged by, or reachable through a :class:`Config` instance.

Loading is the common case, but the hub can also *write* the machine-wide file, which is how a
user configures the model endpoint without a terminal. :func:`update_user_config` merges a section
back into ``~/.kedge/config.toml``; :func:`set_api_key` puts the key in the keyring, where the
writer physically cannot put it. Only the user file is ever written — the per-project
``kedge.toml`` belongs to the process being converted and is the user's to edit.
"""

from __future__ import annotations

import difflib
import logging
import os
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import keyring
import keyring.errors
import tomli_w
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from kedge.errors import ConfigError

logger = logging.getLogger(__name__)

__all__ = [
    "KEDGE_HOME_ENV",
    "KEYRING_SERVICE",
    "PROJECT_CONFIG_FILENAME",
    "USER_CONFIG_FILENAME",
    "AgentConfig",
    "ApiKeyStoreError",
    "Config",
    "ConfigFileError",
    "ConfigValidationError",
    "ContextConfig",
    "IngestConfig",
    "LoadedConfig",
    "MarimoConfig",
    "MissingApiKeyError",
    "ModelConfig",
    "PolicyConfig",
    "ReconciliationConfig",
    "RedactionConfig",
    "SamplingConfig",
    "api_key_status",
    "delete_api_key",
    "get_api_key",
    "keyring_set_command",
    "load_config",
    "set_api_key",
    "update_user_config",
    "user_config_path",
    "user_dir",
]

KEDGE_HOME_ENV = "KEDGE_HOME"
"""Environment variable that relocates ``~/.kedge``. Set by tests; useful for locked-down machines."""

USER_CONFIG_FILENAME = "config.toml"
PROJECT_CONFIG_FILENAME = "kedge.toml"
KEYRING_SERVICE = "kedge"
"""Keyring service name. The entry's username is the configured ``api_key_ref``."""

_DEFAULT_LABEL = "default"


# ── errors ───────────────────────────────────────────────────────────────────────────────────


class ConfigFileError(ConfigError):
    """A config file exists but could not be read or parsed as TOML."""


class ConfigValidationError(ConfigError):
    """A config file parsed, but a value or key in it is not valid."""


class MissingApiKeyError(ConfigError):
    """The configured keyring entry does not exist, or the keyring is unreachable."""


class ApiKeyStoreError(ConfigError):
    """The OS keyring refused to store or remove a key."""


# ── models ───────────────────────────────────────────────────────────────────────────────────


class _Section(BaseModel):
    """Base for config sections: frozen, and strict about unrecognised keys.

    ``extra="forbid"`` is what turns a typo in a hand-edited TOML file into a named error rather
    than a setting that silently does nothing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", protected_namespaces=())


class ModelConfig(_Section):
    """The OpenAI-compatible endpoint the agent talks to."""

    base_url: str = "https://api.openai.com/v1"
    model: str = Field(default="gpt-4o", min_length=1)
    api_key_ref: str = Field(default="default", min_length=1)
    """Name of the keyring entry holding the key. Never the key itself."""
    timeout_seconds: float = Field(default=120.0, gt=0)
    max_retries: int = Field(default=2, ge=0)

    ca_bundle: Path | None = None
    """A PEM to verify the endpoint against, instead of the operating system's trust store.

    Only needed where a TLS-inspecting proxy re-signs the connection and its root has not been
    pushed to the OS store. Unset by default, because :mod:`kedge.tls` already reads that store
    and the corporate root is normally in it. There is deliberately no setting that turns
    verification off; supplying the proxy's PEM here is the supported answer, and unlike
    ``verify=False`` it stays visible to whoever reads this file next (``SECURITY.md``).
    """

    @field_validator("ca_bundle")
    @classmethod
    def _expand_ca_bundle(cls, value: Path | None) -> Path | None:
        return None if value is None else Path(value).expanduser()

    api: Literal["auto", "responses", "chat_completions"] = "auto"
    """Which wire format to speak. ``auto`` tries responses and falls back on its own.

    The responses API is the only one that carries reasoning across a tool call, which is what
    kedge's turns are made of. It is also implemented by far fewer of the OpenAI-compatible
    servers people actually point kedge at, so ``auto`` is the default and the fallback is
    automatic: an endpoint with no ``/responses`` route is discovered on the first call and the
    client speaks chat completions from then on. Pin it only to stop the probing.
    """

    reasoning_effort: Literal["none", "minimal", "low", "medium", "high"] | None = None
    """How hard a reasoning model should think, or ``None`` to say nothing about it.

    ``None`` rather than a value by default, because the parameter is meaningless to a
    non-reasoning model and rejected outright by some endpoints. kedge never lets it be fatal:
    a request refused over reasoning is retried without it and the endpoint is not asked again
    (:class:`kedge.agent.loop.OpenAIClient`), so raising this on a model that cannot honour it
    costs one extra round trip on the first turn and nothing after.
    """

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            msg = f"must start with http:// or https:// (got {value!r})"
            raise ValueError(msg)
        return value.rstrip("/")


class AgentConfig(_Section):
    """How long one turn runs before the loop checks back in with the user."""

    max_steps: int = Field(default=50, ge=1)
    """Model round trips one turn takes before pausing to ask whether to carry on.

    A check-in, not a wall: the paused turn's tool traffic is held and the next message resumes it
    (``kedge.agent.loop.DEFAULT_MAX_STEPS``, which this default tracks). Raising it buys longer
    unattended runs at the cost of a larger bill between check-ins, since every step re-sends the
    context window.
    """


class PolicyConfig(_Section):
    """What the validation gate permits generated code to reach for (PLAN M4).

    Both lists are empty by default, which is the whole policy on a machine with no ``[policy]``
    section: no network, no database. They are read once into
    :class:`kedge.agent.validate.Policy`, and that gate is a quality gate rather than a sandbox —
    it matches names in an AST, so what it can permit is bounded by what it can recognise.

    Two lists rather than one, because a hostname and a connection target are not the same kind of
    name. ``network_allowlist`` is matched against the host in an ``http(s)://`` literal;
    ``database_allowlist`` is matched against whatever a connection actually names, which is
    frequently an ODBC DSN entry or a Snowflake account locator with no host in it anywhere. A
    single list would have to pretend those are interchangeable, and permitting a warehouse write
    is not the same decision as permitting an HTTPS read.
    """

    network_allowlist: tuple[str, ...] = ()
    """Hostnames a generated cell may fetch over HTTP. Matched exactly or as a parent domain, so
    ``internal.bank`` also permits ``rates.internal.bank``."""

    database_allowlist: tuple[str, ...] = ()
    """What a generated cell may connect to over a database driver.

    Any of: the target kedge can read out of the connection string — a hostname, an ODBC ``DSN=``
    name, a Snowflake account; the driver module itself (``pyodbc``, ``duckdb``) where the
    connection is assembled at run time and there is nothing in the cell to read; or the entry
    point (``read_database``, ``read_database_uri``, ``write_database``, ``create_engine``) where
    marimo's single-definition rule has put the engine in one cell and the read in the next, so
    the reading cell names no host and imports no driver. The last two forms are the blunt ones
    and say so by being a module or function name rather than a place.

    Neither list is unioned across layers. ``kedge.toml`` beside the workbook replaces whatever
    ``~/.kedge/config.toml`` set, so a project file that travels with a workbook can widen *or
    narrow* both — see SECURITY.md.
    """

    @field_validator("network_allowlist", "database_allowlist")
    @classmethod
    def _normalise_allowlist(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        entries: list[str] = []
        for raw in value:
            entry = raw.strip().lower()
            if not entry:
                msg = "an allowlist entry cannot be empty"
                raise ValueError(msg)
            if "://" in entry or "/" in entry:
                msg = (
                    f"{raw!r} is a URL; this list holds bare hostnames, DSN names, driver names "
                    f"and entry points (for example 'rates.internal.bank')"
                )
                raise ValueError(msg)
            offending = next(
                (
                    "whitespace" if character.isspace() else repr(character)
                    for character in entry
                    if character.isspace() or character in "=;@,'\""
                ),
                None,
            )
            if offending is not None:
                # Deliberately not echoing the value: the entry this exists to catch is a whole
                # DSN, and `SERVER=warehouse;UID=etl;PWD=hunter2` in an error message is the
                # password printed to a console and a log by the module that forbids exactly that.
                msg = (
                    f"an entry in this list contains {offending}, so it is a connection string "
                    f"rather than a name. A connection string in a config file may carry a "
                    f"password, and kedge keeps credentials in the OS keyring; this list holds "
                    f"bare hostnames, DSN names, driver names and entry points (for example "
                    f"'warehouse.internal', 'riskwarehouse', 'pyodbc')"
                )
                raise ValueError(msg)
            entries.append(entry)
        return tuple(entries)


class ContextConfig(_Section):
    """Context window budget and the eviction knobs from PLAN M4."""

    max_context_tokens: int = Field(default=128_000, gt=0)
    reserve_output_tokens: int = Field(default=4_096, ge=0)
    """Headroom kept clear for the model's own reply; the usable budget is the difference."""
    max_tool_result_tokens: int = Field(default=4_000, gt=0)
    """Per-tool-result ceiling. Anything larger is truncated with an explicit omission marker."""
    evict_tool_results_after_turns: int = Field(default=6, ge=1)
    """Tool results older than this many turns are dropped first; sampled data is the most
    disposable thing in the window and re-fetching costs one call."""
    compaction_threshold: float = Field(default=0.85, gt=0, le=1.0)
    """Fraction of the usable budget at which the conversation is compacted to a digest."""
    tokeniser: str = "cl100k_base"
    """tiktoken encoding used for counting."""


class ReconciliationConfig(_Section):
    """Tolerances for comparing generated output against the workbook's cached values.

    Absolute and relative are separate and both apply: a row matches when it is within *either*.
    Floating point and Excel's own rounding will not agree exactly, and 1e-9 alone produces
    noise on large magnitudes (PLAN 4.5).
    """

    absolute_tolerance: float = Field(default=1e-6, ge=0)
    # Zero by default so the absolute tolerance governs. A row matches within *either*
    # tolerance, so a non-zero relative one widens what passes at large magnitudes: at 1e-9 a
    # one-penny break was forgiven on anything above about ten million. Raise it only when
    # reconciling ratios or rates, where proportional agreement is the actual claim.
    relative_tolerance: float = Field(default=0.0, ge=0)
    max_mismatch_rows: int = Field(default=20, ge=1)
    """How many mismatching rows to report side by side before truncating."""


class RedactionConfig(_Section):
    """Optional column masking. Off by default (PLAN 2.3)."""

    enabled: bool = False
    column_patterns: tuple[str, ...] = ()
    """Case-insensitive regular expressions matched against column names. A matching column
    still reports dtype and null count, but its values are hashed."""
    hash_prefix_length: int = Field(default=12, ge=4, le=64)


class SamplingConfig(_Section):
    """Caps on every value-returning tool. A context-budget control first, a data-handling
    control second, but it serves both (PLAN 2.3)."""

    max_rows: int = Field(default=100, ge=1)
    max_payload_bytes: int = Field(default=32_768, ge=1_024)
    head_rows: int = Field(default=5, ge=0)
    tail_rows: int = Field(default=5, ge=0)
    random_rows: int = Field(default=5, ge=0)
    top_k: int = Field(default=10, ge=1)


class IngestConfig(_Section):
    """The managed hand-in store, and the optional watched folder source (PLAN 2.8)."""

    store_dir: Path | None = None
    """Managed store location. ``None`` means ``<project_dir>/handins``."""
    copy_on_select: bool = True
    """Copy a browser-selected file into the managed store rather than referencing it in place.
    On by default: a shared-drive path is not a stable artifact."""
    watch_dir: Path | None = None
    watch_glob: str = "*.xlsx"
    dedupe_by_hash: bool = True
    contract: Path | None = None
    """The hand-in contract the notebook enforces. ``None`` means ``<project_dir>/contract.yaml``.
    A relative path is relative to the project directory, as ``store_dir`` is."""
    share_handins: bool = False
    """Whether the managed store travels when the converted process is handed to the team
    (:mod:`kedge.handover`).

    Off, and it is the one default in this section that is a data-handling decision rather than a
    convenience. Everything else in a project directory -- the plan, the contract, the acceptance
    record, the run history -- *describes* the process; the store holds the production extracts
    themselves, and a shared team repository is a wider audience than the person who ran the
    extract. Turn it on per project where the hand-ins are the point of sharing (a worked
    example, a fixture, a reference month with nothing sensitive in it).

    It lives here rather than in a section of its own because whether that directory travels is a
    property of the store, and splitting it from ``store_dir`` would put one fact in two places.
    Note it is a statement of intent, not a control: a store *inside* the project directory can
    still be uploaded by a command that pushes everything, which is why
    :func:`kedge.handover.plan_handover` names the paths and why ``store_dir`` pointing outside
    the project directory is the stronger answer."""

    @field_validator("store_dir", "watch_dir", "contract")
    @classmethod
    def _expand_user(cls, value: Path | None) -> Path | None:
        return None if value is None else Path(value).expanduser()


class MarimoConfig(_Section):
    """How the marimo subprocess is launched and monitored."""

    host: str = "127.0.0.1"
    """Loopback only. There is no deployment story here and there should not be (PLAN 2.9)."""
    port: int = Field(default=0, ge=0, le=65_535)
    """0 means pick a free port at launch. A fixed port is for debugging only."""
    timeout_minutes: float | None = Field(default=30.0, gt=0)
    """Passed to ``marimo edit --timeout``. The server shuts itself down after this long with
    no connection, which is what makes orphans self-clearing on Windows. ``None`` disables it,
    which is not recommended."""
    session_ttl_seconds: int | None = Field(default=None, ge=0)
    """Passed to ``marimo edit --session-ttl``: how long a session survives after its last
    transport disconnects. Unset by default, and that is deliberate. kedge bootstraps its kernel
    session with a short-lived request and then drives the kernel through that session id; a TTL
    would close the session that long after the browser tab went away, and every subsequent
    kernel call would fail with a stale session id. Set it only if you want sessions to expire."""
    watch: bool = True
    """Passed as ``--watch``: reload when the notebook file changes on disk."""
    health_timeout_seconds: float = Field(default=30.0, gt=0)
    """How long to wait for ``GET /health`` to answer before declaring the launch failed."""
    health_poll_interval_seconds: float = Field(default=0.25, gt=0)
    shutdown_grace_seconds: float = Field(default=10.0, gt=0)
    """How long a graceful shutdown gets before escalating to killing the process tree."""


class Config(_Section):
    """The resolved configuration. Frozen; carries no secrets."""

    model: ModelConfig = Field(default_factory=ModelConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    reconciliation: ReconciliationConfig = Field(default_factory=ReconciliationConfig)
    redaction: RedactionConfig = Field(default_factory=RedactionConfig)
    sampling: SamplingConfig = Field(default_factory=SamplingConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    marimo: MarimoConfig = Field(default_factory=MarimoConfig)


@dataclass(frozen=True)
class LoadedConfig:
    """A resolved :class:`Config` plus where each value came from.

    Internal-only and never serialised, so a frozen dataclass rather than a pydantic model.
    """

    config: Config
    provenance: dict[str, str] = field(default_factory=dict)
    """Dotted key (``"sampling.max_rows"``) to origin: a file path, or ``"default"``."""
    files: tuple[Path, ...] = ()
    """The config files that were found and read, in increasing order of precedence."""

    def origin(self, dotted_key: str) -> str:
        """Return where the value at ``dotted_key`` came from, or ``"default"``."""
        return self.provenance.get(dotted_key, _DEFAULT_LABEL)


# ── paths ────────────────────────────────────────────────────────────────────────────────────


def user_dir() -> Path:
    """Return the machine-wide kedge directory, honouring ``KEDGE_HOME``.

    Defaults to ``~/.kedge``. Tests set ``KEDGE_HOME`` so they never touch the real one.
    """
    override = os.environ.get(KEDGE_HOME_ENV)
    if override and override.strip():
        return Path(override).expanduser()
    return Path.home() / ".kedge"


def user_config_path() -> Path:
    """Return the path of the machine-wide config file, whether or not it exists."""
    return user_dir() / USER_CONFIG_FILENAME


# ── loading ──────────────────────────────────────────────────────────────────────────────────


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        msg = f"{path}: not valid TOML: {exc}"
        raise ConfigFileError(msg) from exc
    except OSError as exc:
        msg = f"{path}: could not be read: {exc}"
        raise ConfigFileError(msg) from exc


def _merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively overlay one raw config mapping onto another."""
    merged = dict(base)
    for key, value in overlay.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _merge(existing, value)
        else:
            merged[key] = value
    return merged


def _flatten(raw: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten a nested mapping to dotted keys, keeping leaf values."""
    flat: dict[str, Any] = {}
    for key, value in raw.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, prefix=f"{dotted}."))
        else:
            flat[dotted] = value
    return flat


def _reject_inline_secrets(raw: dict[str, Any], source: Path) -> None:
    """Refuse a config file that carries an API key inline rather than a keyring reference."""
    for dotted in _flatten(raw):
        leaf = dotted.rsplit(".", maxsplit=1)[-1]
        if leaf in {"api_key", "apikey", "api-key", "secret_key", "token"}:
            msg = (
                f"{source}: '{dotted}' looks like a secret written into a config file. "
                f"kedge never reads keys from config. Store the key in the OS keyring with "
                f"`{keyring_set_command('<ref>')}` and set model.api_key_ref = '<ref>' instead."
            )
            raise ConfigValidationError(msg)


def _valid_keys_at(loc: tuple[Any, ...]) -> list[str]:
    """Return the field names valid at the given pydantic error location, for suggestions."""
    model: type[BaseModel] = Config
    for part in loc[:-1]:
        field_info = model.model_fields.get(str(part))
        annotation = None if field_info is None else field_info.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            model = annotation
        else:
            return []
    return list(model.model_fields)


def _blame(dotted: str, layers: list[tuple[Path, dict[str, Any]]]) -> Path | None:
    """Return the highest-precedence file that mentions ``dotted``, if any.

    Matches a whole subtree as well as a leaf, so an unknown *section* is blamed on the file
    that introduced it rather than falling through to "resolved config".
    """
    prefix = f"{dotted}."
    for path, raw in reversed(layers):
        keys = _flatten(raw)
        if dotted in keys or any(key.startswith(prefix) for key in keys):
            return path
    return None


def _describe_validation_error(
    exc: ValidationError, layers: list[tuple[Path, dict[str, Any]]]
) -> str:
    lines: list[str] = []
    for error in exc.errors():
        loc = error["loc"]
        dotted = ".".join(str(part) for part in loc)
        blamed = _blame(dotted, layers) or _blame(dotted.rsplit(".", maxsplit=1)[0], layers)
        where = str(blamed) if blamed is not None else "resolved config"
        if error["type"] == "extra_forbidden":
            suggestions = difflib.get_close_matches(str(loc[-1]), _valid_keys_at(loc), n=1)
            # ASCII only: these messages are printed to Windows consoles that are often cp1252.
            hint = f" - did you mean '{suggestions[0]}'?" if suggestions else ""
            lines.append(f"{where}: unknown key '{dotted}'{hint}")
        else:
            lines.append(f"{where}: invalid value for '{dotted}': {error['msg']}")
    return "\n".join(lines)


def load_config(
    *,
    project_dir: Path | None = None,
    user_path: Path | None = None,
    project_path: Path | None = None,
) -> LoadedConfig:
    """Load and validate the layered configuration.

    Reads ``~/.kedge/config.toml`` then ``<project_dir>/kedge.toml``, with the latter winning
    key by key. Missing files are normal and are simply skipped, so a fresh machine gets the
    defaults. Pass ``user_path`` or ``project_path`` to override either location outright.

    Raises :class:`ConfigFileError` if a file exists but will not parse, and
    :class:`ConfigValidationError` if a value or key in it is not valid; both name the file.
    """
    resolved_user = user_config_path() if user_path is None else user_path
    if project_path is not None:
        resolved_project = project_path
    elif project_dir is not None:
        resolved_project = project_dir / PROJECT_CONFIG_FILENAME
    else:
        resolved_project = Path.cwd() / PROJECT_CONFIG_FILENAME

    layers: list[tuple[Path, dict[str, Any]]] = []
    for path in (resolved_user, resolved_project):
        if not path.is_file():
            logger.debug("no config file at %s", path)
            continue
        raw = _read_toml(path)
        _reject_inline_secrets(raw, path)
        layers.append((path, raw))
        logger.debug("loaded config layer %s", path)

    merged: dict[str, Any] = {}
    provenance: dict[str, str] = {}
    for path, raw in layers:
        merged = _merge(merged, raw)
        for dotted in _flatten(raw):
            provenance[dotted] = str(path)

    try:
        config = Config.model_validate(merged)
    except ValidationError as exc:
        raise ConfigValidationError(_describe_validation_error(exc, layers)) from exc

    return LoadedConfig(
        config=config,
        provenance=provenance,
        files=tuple(path for path, _ in layers),
    )


# ── secrets ──────────────────────────────────────────────────────────────────────────────────


def keyring_set_command(api_key_ref: str) -> str:
    """Return the exact command a user should run to store their API key."""
    return f"uv run keyring set {KEYRING_SERVICE} {api_key_ref}"


def get_api_key(config: Config) -> str:
    """Fetch the model endpoint's API key from the OS keyring.

    The key is returned, never cached on the config object and never logged. On Windows the
    backing store is Credential Manager.

    Raises :class:`MissingApiKeyError` naming the entry and the exact command to create it.
    """
    ref = config.model.api_key_ref
    try:
        secret = keyring.get_password(KEYRING_SERVICE, ref)
    except keyring.errors.KeyringError as exc:
        msg = (
            f"could not read the API key from the OS keyring (service '{KEYRING_SERVICE}', "
            f"entry '{ref}'): {exc}. Check that a keyring backend is available, then store the "
            f"key with `{keyring_set_command(ref)}`."
        )
        raise MissingApiKeyError(msg) from exc

    if not secret:
        msg = (
            f"no API key found in the OS keyring for service '{KEYRING_SERVICE}', entry "
            f"'{ref}'. Store it with:\n    {keyring_set_command(ref)}\n"
            f"kedge never reads API keys from config files or environment variables."
        )
        raise MissingApiKeyError(msg)
    return secret


def api_key_status(config: Config) -> Literal["present", "missing", "unavailable"]:
    """Report whether the configured keyring entry resolves, without returning the key.

    Used by ``kedge config`` and ``kedge doctor``, which must never print the key itself.
    """
    try:
        secret = keyring.get_password(KEYRING_SERVICE, config.model.api_key_ref)
    except keyring.errors.KeyringError:
        logger.debug("keyring backend unavailable while checking entry")
        return "unavailable"
    return "present" if secret else "missing"


def set_api_key(api_key_ref: str, secret: str) -> None:
    """Store the model endpoint's API key in the OS keyring under ``api_key_ref``.

    The counterpart to :func:`get_api_key`, and the only supported way for kedge itself to put a
    key anywhere. The secret is never logged and never written to a config file; on Windows it
    lands in Credential Manager.

    Raises:
        ApiKeyStoreError: The entry name is empty, or the keyring refused the write.
    """
    ref = api_key_ref.strip()
    if not ref:
        msg = "the keyring entry name cannot be empty"
        raise ApiKeyStoreError(msg)
    if not secret:
        msg = f"refusing to store an empty API key in '{KEYRING_SERVICE}/{ref}'"
        raise ApiKeyStoreError(msg)
    try:
        keyring.set_password(KEYRING_SERVICE, ref, secret)
    except keyring.errors.KeyringError as exc:
        msg = (
            f"could not store the API key in the OS keyring (service '{KEYRING_SERVICE}', entry "
            f"'{ref}'): {exc}. Check that a keyring backend is available, or store it yourself "
            f"with `{keyring_set_command(ref)}`."
        )
        raise ApiKeyStoreError(msg) from exc
    # The entry name, never the secret and never its length.
    logger.info("stored an API key in the OS keyring entry %s/%s", KEYRING_SERVICE, ref)


def delete_api_key(api_key_ref: str) -> bool:
    """Remove the keyring entry named by ``api_key_ref``.

    Returns:
        Whether an entry was there to remove. Deleting one that does not exist is not an error;
        the caller asked for it to be gone and it is.

    Raises:
        ApiKeyStoreError: The keyring is unreachable, so nothing can be said about the entry.
    """
    ref = api_key_ref.strip()
    try:
        keyring.delete_password(KEYRING_SERVICE, ref)
    except keyring.errors.PasswordDeleteError:
        logger.debug("no keyring entry %s/%s to delete", KEYRING_SERVICE, ref)
        return False
    except keyring.errors.KeyringError as exc:
        msg = (
            f"could not remove the API key from the OS keyring (service '{KEYRING_SERVICE}', "
            f"entry '{ref}'): {exc}."
        )
        raise ApiKeyStoreError(msg) from exc
    logger.info("removed the OS keyring entry %s/%s", KEYRING_SERVICE, ref)
    return True


# ── writing ──────────────────────────────────────────────────────────────────────────────────

_USER_CONFIG_BANNER = (
    "# kedge machine-wide configuration.\n"
    "#\n"
    "# Rewritten whenever settings are saved from the hub, so comments added by hand below do\n"
    "# not survive. Process-shaped settings — tolerances, redaction, sampling — belong in a\n"
    "# kedge.toml beside the workbook, which kedge never writes.\n"
    "#\n"
    "# The model endpoint's API key is deliberately not here. It lives in the OS keyring under\n"
    "# the entry named by model.api_key_ref, and a key written into this file is refused.\n"
)


def update_user_config(
    section: str,
    values: Mapping[str, Any],
    *,
    path: Path | None = None,
) -> Path:
    """Merge ``values`` into ``[section]`` of the machine-wide config file and write it back.

    Only ``~/.kedge/config.toml`` is ever written. Machine-shaped settings — the model endpoint,
    the keyring entry — are exactly what belongs there, and the per-project ``kedge.toml`` is the
    user's to edit (PLAN 2.9). A project file that overrides one of these keys keeps winning after
    the write, which is why callers should report the resolved value rather than what they sent.

    A value of ``None`` removes that key, so the built-in default applies again.

    The merged file is validated as a whole before anything is written, and written atomically
    via a temporary file in the same directory, so a rejected value or a full disk leaves the
    previous configuration intact rather than truncated.

    Args:
        section: Top-level table to merge into, such as ``"model"``.
        values: Keys to set within it. ``None`` removes a key.
        path: Overrides the file written. For tests.

    Returns:
        The path written.

    Raises:
        ConfigValidationError: ``section`` or a key is unknown, a value is invalid, or ``values``
            carries something that looks like a secret.
        ConfigFileError: The existing file will not parse, or the write failed.
    """
    target = user_config_path() if path is None else path

    raw = _read_toml(target) if target.is_file() else {}
    _reject_inline_secrets({section: dict(values)}, target)

    existing = raw.get(section, {})
    if not isinstance(existing, dict):
        msg = f"{target}: '{section}' is not a table, so kedge will not write into it"
        raise ConfigValidationError(msg)

    table = dict(existing)
    for key, value in values.items():
        if value is None:
            table.pop(key, None)
        else:
            table[key] = value

    merged = dict(raw)
    if table:
        merged[section] = table
    else:
        merged.pop(section, None)

    try:
        Config.model_validate(merged)
    except ValidationError as exc:
        raise ConfigValidationError(_describe_validation_error(exc, [(target, merged)])) from exc

    _write_toml(target, merged)
    logger.info("wrote [%s] to %s", section, target)
    return target


def _write_toml(path: Path, raw: Mapping[str, Any]) -> None:
    """Write ``raw`` to ``path`` atomically, replacing whatever was there.

    Temporary file in the same directory then :meth:`Path.replace`, matching the workbook registry
    and the marker files: a crash mid-write must not leave a half-written config that then fails
    to parse on the next launch.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = _USER_CONFIG_BANNER + "\n" + tomli_w.dumps(dict(raw))
        # mkstemp rather than a fixed name, so two writes racing cannot land on the same file.
        handle, temporary_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(body)
            temporary.replace(path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    except OSError as exc:
        msg = f"{path}: could not be written: {exc}"
        raise ConfigFileError(msg) from exc
