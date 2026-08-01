"""The settings surface: the model endpoint, its API key, and which model to use.

Until this existed the only way to point kedge at a model was to hand-edit ``~/.kedge/config.toml``
and run ``keyring set kedge default`` in a terminal. That is a poor first run: the open sequence
falls back to demo mode when no key resolves (:mod:`kedge.server.hub`), so a new user reaches a
working notebook and a scripted agent, with the fix living somewhere they were never shown.

Four endpoints, and a hard split between the two things being stored:

* **Config** — ``base_url``, ``model``, ``api_key_ref`` — is merged into the *user* config file by
  :func:`kedge.config.update_user_config`. Machine-shaped settings belong in the machine-wide file
  (PLAN 2.9); the per-project ``kedge.toml`` is hand-written and kedge does not touch it.
* **The key** goes to the OS keyring and nowhere else. It is accepted on the way in, never
  returned, never logged, and never written to a file. The config writer refuses it independently,
  so a mistake here is caught twice.

Because a project ``kedge.toml`` still wins over the file being written, :func:`model_settings`
reports which keys are being overridden and by what. Saving a base URL and seeing nothing change
is otherwise a genuinely baffling five minutes.

:func:`probe` is what makes the model list work before anything is saved: the browser sends the
endpoint the user has just typed, kedge asks it for ``/models``, and the answer populates the
picker. PLAN M6 asks for exactly this — ``/v1/models`` where supported, manual override where not —
so a failure is reported as prose beside a free-text box rather than as an error.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, ValidationError

from kedge.config import (
    KEYRING_SERVICE,
    PROJECT_CONFIG_FILENAME,
    Config,
    ConfigError,
    LoadedConfig,
    ModelConfig,
    api_key_status,
    delete_api_key,
    get_api_key,
    keyring_set_command,
    load_config,
    set_api_key,
    update_user_config,
    user_config_path,
)

if TYPE_CHECKING:
    from kedge.server.app import ServerState

logger = logging.getLogger(__name__)

__all__ = ["fetch_model_names", "router"]

router = APIRouter()

_PROBE_TIMEOUT = 10.0

_MANAGED_KEYS = ("base_url", "model", "api_key_ref")
"""The ``[model]`` keys the settings panel writes. Timeouts and retries stay hand-edited: they
are tuning, not setup, and every control on the panel is one more thing to read past."""


def _state(request: Request) -> ServerState:
    return request.app.state.kedge


def _project_dir(state: ServerState) -> Path:
    """Return the directory whose ``kedge.toml`` applies.

    With a workbook open that is the workbook's own directory, so the panel reports the settings
    the agent is actually using. On the hub, before anything is open, there is no project, and the
    working directory is the same thing :func:`kedge.config.load_config` would assume.
    """
    if state.workspace is not None:
        return state.workspace.workbook_path.parent
    return Path.cwd()


def _loaded(state: ServerState) -> LoadedConfig:
    """Load the layered config for the current context, or answer 500 naming the bad file."""
    try:
        return load_config(project_dir=_project_dir(state))
    except ConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ── request bodies ───────────────────────────────────────────────────────────────────────────


class ModelSettingsBody(BaseModel):
    """Body for saving the model endpoint.

    Every field is optional and ``None`` means "leave this alone", so the panel can save one
    field without restating the rest. Removing a stored key is a ``DELETE``, not an empty string,
    because an empty string in a password box is what a browser sends when nobody touched it.
    """

    base_url: str | None = None
    model: str | None = None
    api_key_ref: str | None = None
    api_key: str | None = Field(default=None, repr=False)
    """Stored in the OS keyring, never in config. Excluded from ``repr`` so it cannot reach a log
    line through an exception rendering the model."""


class ProbeBody(BaseModel):
    """Body for testing an endpoint that has not been saved yet."""

    base_url: str | None = None
    api_key: str | None = Field(default=None, repr=False)
    api_key_ref: str | None = None


# ── the model list ───────────────────────────────────────────────────────────────────────────


async def fetch_model_names(base_url: str, api_key: str) -> list[str]:
    """Return the model ids ``base_url`` offers, sorted.

    Raises:
        httpx.HTTPError: The endpoint could not be reached or answered an error status.
        ValueError: The endpoint answered something that was not JSON.
    """
    async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as http:
        response = await http.get(
            f"{base_url}/models", headers={"Authorization": f"Bearer {api_key}"}
        )
    response.raise_for_status()
    payload = response.json()
    entries = payload.get("data") if isinstance(payload, dict) else None
    return sorted(
        {str(item["id"]) for item in entries or [] if isinstance(item, dict) and item.get("id")}
    )


# ── reading ──────────────────────────────────────────────────────────────────────────────────


def _overrides(loaded: LoadedConfig) -> dict[str, str]:
    """Return the managed keys a project ``kedge.toml`` is winning on, and the file responsible.

    The panel writes the user file; a project file layered on top still wins (PLAN 2.9). Saying so
    is the difference between "kedge ignored me" and "that value is pinned by this file".
    """
    pinned: dict[str, str] = {}
    for key in _MANAGED_KEYS:
        origin = loaded.origin(f"model.{key}")
        if origin != "default" and Path(origin).name == PROJECT_CONFIG_FILENAME:
            pinned[key] = origin
    return pinned


def _describe(state: ServerState, loaded: LoadedConfig) -> dict[str, Any]:
    """Render the current model settings for the browser. Never includes the key itself."""
    model = loaded.config.model
    entry = model.api_key_ref
    status = api_key_status(loaded.config)
    return {
        "base_url": model.base_url,
        "model": model.model,
        "api_key_ref": entry,
        "timeout_seconds": model.timeout_seconds,
        "max_retries": model.max_retries,
        "api_key": {
            "status": status,
            "service": KEYRING_SERVICE,
            "entry": entry,
            "set_command": keyring_set_command(entry),
        },
        "config_path": str(user_config_path()),
        "provenance": {key: loaded.origin(f"model.{key}") for key in _MANAGED_KEYS},
        "overridden_by_project": _overrides(loaded),
        "defaults": {
            "base_url": ModelConfig.model_fields["base_url"].default,
            "model": ModelConfig.model_fields["model"].default,
            "api_key_ref": ModelConfig.model_fields["api_key_ref"].default,
        },
        "attached": state.attached,
        "demo": state.demo,
    }


@router.get("/api/settings/model")
async def model_settings(request: Request) -> dict[str, Any]:
    """Report the model endpoint as it currently resolves.

    Works with or without a workbook open, because the first thing a new user needs to do is set
    this up and the hub is where they are standing.
    """
    state = _state(request)
    loaded = _loaded(state)
    return await run_in_threadpool(_describe, state, loaded)


# ── writing ──────────────────────────────────────────────────────────────────────────────────


def _validated(current: Config, body: ModelSettingsBody) -> dict[str, str]:
    """Return the managed keys ``body`` changes, normalised, or raise 400 naming the field.

    Validation goes through :class:`~kedge.config.ModelConfig` rather than being restated here, so
    the panel and a hand-edited file accept exactly the same values — and the stored ``base_url``
    is the normalised one, without its trailing slash.
    """
    supplied = {
        key: value.strip()
        for key, value in (
            ("base_url", body.base_url),
            ("model", body.model),
            ("api_key_ref", body.api_key_ref),
        )
        if value is not None
    }
    if not supplied:
        return {}
    try:
        candidate = current.model.model_copy(update=supplied)
        checked = ModelConfig.model_validate(candidate.model_dump())
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first["loc"]) or "model"
        raise HTTPException(status_code=400, detail=f"{field}: {first['msg']}") from exc
    return {key: getattr(checked, key) for key in supplied}


@router.put("/api/settings/model")
async def save_model_settings(body: ModelSettingsBody, request: Request) -> dict[str, Any]:
    """Save the model endpoint, and pick it up in this process.

    The key goes to the keyring first. If that fails there is nothing useful to write, and a
    config file naming an entry that was never created is worse than no change at all.

    With a workbook open, the workspace re-reads its config and the agent loop is rebuilt, so a
    server that opened in demo mode for want of a key starts answering for real. Without one there
    is nothing to rebuild: the next open reads the file fresh.
    """
    state = _state(request)
    loaded = _loaded(state)
    changes = _validated(loaded.config, body)

    entry = changes.get("api_key_ref", loaded.config.model.api_key_ref)
    if body.api_key is not None and body.api_key.strip():
        try:
            await run_in_threadpool(set_api_key, entry, body.api_key.strip())
        except ConfigError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    if changes:
        try:
            await run_in_threadpool(update_user_config, "model", changes)
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return await _reload(state)


@router.delete("/api/settings/model/key")
async def forget_api_key(request: Request) -> dict[str, Any]:
    """Remove the stored API key, leaving the endpoint and model as they are.

    Deleting an entry that is not there is not an error. The user asked for the key to be gone.
    """
    state = _state(request)
    loaded = _loaded(state)
    try:
        await run_in_threadpool(delete_api_key, loaded.config.model.api_key_ref)
    except ConfigError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return await _reload(state)


async def _reload(state: ServerState) -> dict[str, Any]:
    """Adopt the saved settings in this process and report the result."""
    settings = await run_in_threadpool(_describe, state, _loaded(state))
    workspace = state.workspace
    if workspace is None:
        settings["applied"] = "next_open"
        return settings

    try:
        await run_in_threadpool(workspace.reload_config)
    except ConfigError as exc:  # pragma: no cover - the write validated moments ago
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    live = await run_in_threadpool(state.rebuild_agent)
    settings["demo"] = state.demo
    settings["applied"] = "now" if live else "unusable"
    return settings


# ── probing ──────────────────────────────────────────────────────────────────────────────────


@router.post("/api/settings/model/probe")
async def probe(body: ProbeBody, request: Request) -> dict[str, Any]:
    """Test an endpoint and list its models, without saving anything.

    This is how the model picker fills in for an endpoint the user has typed but not yet committed
    to. It answers 200 on every path, with ``ok`` and a sentence, because "that endpoint refused
    the key" is a normal thing to learn from a settings panel and not an HTTP error.
    """
    state = _state(request)
    loaded = _loaded(state)
    configured = loaded.config.model

    base_url = (body.base_url or configured.base_url).strip()
    try:
        checked = ModelConfig.model_validate(
            configured.model_copy(update={"base_url": base_url}).model_dump()
        )
    except ValidationError as exc:
        return {"ok": False, "source": "invalid", "models": [], "detail": exc.errors()[0]["msg"]}
    base_url = checked.base_url

    if state.demo:
        return {
            "ok": True,
            "source": "demo",
            "models": [],
            "detail": "Demo mode: no endpoint was contacted.",
        }

    key = (body.api_key or "").strip()
    if not key:
        # No key typed, so fall back to the stored one — under the entry name being probed, which
        # may itself be unsaved.
        entry = (body.api_key_ref or "").strip() or configured.api_key_ref
        stored_under = configured.model_copy(update={"api_key_ref": entry})
        probing = loaded.config.model_copy(update={"model": stored_under})
        try:
            key = await run_in_threadpool(get_api_key, probing)
        except ConfigError as exc:
            return {"ok": False, "source": "no_key", "models": [], "detail": str(exc)}

    try:
        names = await fetch_model_names(base_url, key)
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("probe of %s failed: %s", base_url, exc)
        return {
            "ok": False,
            "source": "unreachable",
            "models": [],
            "detail": f"{base_url}/models did not answer usefully ({exc}). "
            f"Plenty of endpoints do not implement it; you can still type a model name.",
        }

    if not names:
        return {
            "ok": True,
            "source": "empty",
            "models": [],
            "detail": f"{base_url} answered, but listed no models. Type a model name.",
        }
    return {
        "ok": True,
        "source": "endpoint",
        "models": names,
        "detail": f"{base_url} answered with {len(names)} model(s).",
    }
