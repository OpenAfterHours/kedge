"""Write the ``.marimo.toml`` that keeps marimo's own AI assistant out of the conversation.

marimo ships an assistant of its own: a chat panel, cell-generation actions, and inline
completion. It is configured from the user's personal ``~/.marimo.toml``, not from anything kedge
owns, and left alone it sits inside the ``<iframe>`` kedge serves -- one click from the workbook
data, sending sample values to whatever endpoint that personal config names. That endpoint is
outside kedge's controlled tool surface and outside the outbound payload log (PLAN 2.3), which is
the whole of kedge's account of what leaves this machine. In a tool pointed at finance data, an
unlogged second channel is not a rough edge: it is the central claim failing quietly.

The mechanism is marimo's own config search, ``marimo._config.utils.get_user_config_path``: it
starts at the process's working directory, walks up through the parents, and returns the first
``.marimo.toml`` it finds, only then falling back to the home directory and XDG. kedge launches
marimo with ``cwd`` set to the workspace's project directory, so a file written there is found
before the user's. ``tests/unit/test_lifecycle.py`` drives that resolver directly rather than
trusting this paragraph, because it is the half of the arrangement kedge does not own.

**Merged, never clobbered.** The file is not ours alone even though we write it. marimo's
``UserConfigManager.save_config`` writes the *whole* merged config back to whichever path the
search found, so the moment a user changes anything from inside the editor, their settings land
in this file -- and since ``_load_config`` returns ``merge_default_config(...)``, what lands is
the entire schema with every default filled in, not just what they changed. Rewriting it from
scratch on the next launch would silently discard that. Everything found is preserved;
:data:`FORCED` alone is overwritten.

That heading has a sharp edge, and it is the difference between a file that is *malformed* and
one that is merely *unreadable*. A file whose content will not parse holds nothing worth keeping
and is replaced. A file that could not be opened -- a Windows lock, a folder mid-sync, a scanner
holding it -- holds everything and kedge simply cannot see it, so writing would mean discarding
the user's whole configuration to assert two keys. That one is left untouched and the launch is
reported as unenforced, which is the only answer that keeps both halves of the heading true.

Two consequences of that follow, and the second is why :func:`disable_marimo_assistant` returns
more than a boolean. First, this file *shadows* the user's own: ``_load_config`` reads only the
path the search returned, so inside a kedge-launched editor none of their personal config applies
-- not their theme, not their keymap. That is a cost of the arrangement, not a bug in it, and it
is not worth solving for a notebook opened to convert one workbook. Second, and less benign:
whatever they change in that editor is written *into the project directory*, beside the workbook.
marimo's schema has ``api_key`` on five provider configs plus ``codeium_api_key`` and the two
Bedrock credentials, so a model key typed into that settings panel lands in plaintext in a folder
that is frequently synced and, once a conversion is handed over, shared. ``ai.enabled = false``
hides the panels that ask for one, which makes it unlikely rather than impossible. So the file is
scanned for secret-shaped keys and they are *reported by name* on
:attr:`AssistantLockdown.secret_keys` -- never by value, and never removed. Deleting a key
somebody deliberately entered is its own surprise; saying it is there is the honest act.

**One config source still outranks it, and it is reported rather than lost.** ``.marimo.toml`` is
the *user* config, and marimo merges ``[tool.marimo]`` from the nearest ``pyproject.toml`` on top
of it (``ProjectConfigManager``). So a workbook sitting inside a Python project whose pyproject
enables the assistant wins, and there is nothing kedge can write to win it back short of putting
a ``pyproject.toml`` of its own into the project directory, which would change what that directory
*is* to every other tool that looks at it. :func:`disable_marimo_assistant` therefore looks for
that file and, where it finds one that would switch the assistant back on, returns
``enforced=False`` naming it. The notebook's own PEP 723 header cannot do the same thing:
``ALLOWED_SCRIPT_CONFIG_TOP_KEYS`` does not include ``ai`` or ``completion``, so a model-authored
header has no route back in. All three were checked against marimo 0.23.15 by running it, not by
reading it.

**Failure policy: report, never refuse.** Neither an unreadable existing file nor a failed write
stops a launch. The case against that is the conventional one for a containment control -- fail
closed, because a control that fails open and only says so in a log line is the permanently amber
signal nobody reads. It loses here on what the file actually buys. ``ai.enabled`` is a *frontend*
gate in marimo 0.23.15 -- it hides the AI actions and panels, while the ``/api/ai/*`` endpoints
stay reachable to anything holding the server token -- so the honest description is "the
affordance is not on screen", not "the endpoint is sealed", and reaching it at all needs a
provider the user configured themselves. Set against that, refusing to launch costs the user the
entire conversion over a file they can fix in seconds, on the one platform where a read-only file
is easiest to acquire by accident. So: no exception escapes this module, the failure is logged at
ERROR naming the path and what is now live, and :class:`AssistantLockdown` carries the same
sentence back to the caller for anything with a user in front of it. What is *not* acceptable is
proceeding silently, which is why there is a return value at all.

This module writes a file for marimo to read. It never imports marimo -- the filename and the two
keys are copied here deliberately, so that a marimo release renaming either costs one file. It is
also not to be confused with :class:`kedge.config.MarimoConfig`, which is kedge's own settings for
the subprocess (host, port, timeouts); this is marimo's settings for itself.
"""

from __future__ import annotations

import logging
import os
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import tomli_w

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "CONFIG_FILENAME",
    "FORCED",
    "AssistantLockdown",
    "disable_marimo_assistant",
    "inspect_marimo_assistant",
]

CONFIG_FILENAME = ".marimo.toml"
"""marimo's config filename, copied from ``marimo._config.utils.CONFIG_FILENAME``."""

FORCED: tuple[tuple[str, str, bool], ...] = (
    ("ai", "enabled", False),
    ("completion", "copilot", False),
)
"""The keys kedge overwrites, as ``(table, key, value)``.

``ai.enabled`` withdraws the chat panel and the generate-with-AI actions. ``completion.copilot``
is separate and has to be named separately: it is the inline completion provider, typed as
``bool | "github" | "codeium" | "custom"``, and ``ai.enabled`` does not cover it.
"""

_SECRET_KEY_NAMES = frozenset({"api_key", "aws_access_key_id", "aws_secret_access_key"})
"""Key names kedge reports as secret-shaped if it finds one holding a value.

``api_key`` covers the OpenAI, Anthropic, Google, GitHub and completion configs, and the suffix
rule below covers ``codeium_api_key``. The two AWS names are here because marimo's own
``_config.secrets.mask_secrets`` lists them as secrets and a name rule alone would miss them --
neither ends in ``_api_key``, and a guard that quietly skipped two credentials would be worse
than no guard.
"""

_SECRET_KEY_SUFFIX = "_api_key"
_SECRET_PLACEHOLDER = "********"
"""``marimo._config.secrets.SECRET_PLACEHOLDER``, which is what a *masked* key looks like.

``remove_secret_placeholders`` should keep it off disk, but reporting one as a credential would
be a false alarm, and this guard is only worth having while it never cries wolf.
"""

_BANNER = (
    "# Written by kedge before each marimo launch, and rewritten before the next one.\n"
    "#\n"
    "# marimo finds this file ahead of ~/.marimo.toml because kedge launches marimo from this\n"
    "# directory, so the two settings below win. They are forced rather than defaulted: marimo's\n"
    "# own AI assistant would send workbook values to an endpoint kedge neither controls nor\n"
    "# logs, and the log of what leaves this machine is the point of the tool.\n"
    "#\n"
    "# Every other key here is yours. kedge preserves what it finds and overwrites only\n"
    "# ai.enabled and completion.copilot.\n"
)


@dataclass(frozen=True, slots=True)
class AssistantLockdown:
    """Whether marimo's assistant was disabled for the launch about to happen.

    ``enforced`` false means the assistant is live for this session and the user should be told.
    ``secret_keys`` is a separate question with a separate answer -- credentials sitting in the
    project directory are worth reporting whether or not the assistant was disabled -- so it does
    not touch ``enforced``, and it holds dotted key *names*, never values. Nothing here is ever
    raised -- see the module docstring for why -- so a caller with a user in front of it is the
    only thing that can pass ``detail`` on.

    Example:
        >>> outcome = disable_marimo_assistant(project_dir)
        >>> outcome.enforced
        True
        >>> outcome.secret_keys
        ('ai.open_ai.api_key',)
    """

    path: Path
    enforced: bool
    detail: str
    secret_keys: tuple[str, ...] = ()


def disable_marimo_assistant(project_dir: Path) -> AssistantLockdown:
    """Force marimo's AI assistant off for a server launched from ``project_dir``.

    Reads any ``.marimo.toml`` already there, overwrites :data:`FORCED` and nothing else, and
    writes the result back atomically. Never raises: a malformed existing file is replaced, a
    write that fails is reported on the returned :class:`AssistantLockdown`, and so is a
    ``pyproject.toml`` above the directory that would outrank the file just written. The file is
    also scanned for credentials a previous session left in it, which are reported by name and
    left exactly where they are.

    Args:
        project_dir: The directory marimo will be launched from, which is the workspace's project
            directory. It is created if it is not there.

    Returns:
        What happened, including a sentence fit to put in front of a user when it went wrong.

    References:
        ``marimo._config.utils.get_user_config_path`` -- the search this relies on.
        ``marimo._config.manager.ProjectConfigManager`` -- the one that outranks it.
        ``marimo._config.manager.UserConfigManager.save_config`` -- how a key gets in here.
    """
    path = project_dir / CONFIG_FILENAME
    found = _read_existing(path)
    if found is None:
        return _unreadable(path)

    merged = _with_assistant_off(found)
    secret_keys = _secret_shaped_keys(merged)
    if secret_keys:
        # Names only. redacted_argv in kedge.lifecycle is the local precedent: kedge logs that a
        # credential exists and where, never what it is.
        logger.warning(
            "%s holds %d secret-shaped key(s) in plaintext: %s",
            path,
            len(secret_keys),
            ", ".join(secret_keys),
        )

    try:
        _write_atomically(path, merged)
    except OSError as exc:
        detail = (
            f"could not write {path}, so marimo's built-in AI assistant is still enabled for "
            f"this session and anything it sends will not appear in kedge's outbound log ({exc})"
        )
        logger.error("marimo's AI assistant could not be disabled: %s", detail)
        return _lockdown(path, enforced=False, detail=detail, secret_keys=secret_keys)

    logger.debug("wrote %s disabling marimo's AI assistant", path)
    return _assess(project_dir, path, secret_keys=secret_keys)


def inspect_marimo_assistant(project_dir: Path) -> AssistantLockdown:
    """Report the same thing as :func:`disable_marimo_assistant`, writing nothing.

    This is what a caller renders. It exists because the launch-time result cannot be handed to
    anything that wants it -- ``launch_marimo`` returns a ``Popen`` and its callers are spread
    across the CLI and the server -- and because caching that result would be wrong even if it
    could be. The file is live: marimo rewrites it whenever a setting changes in the editor, so
    a credential can appear in it, or the assistant be switched back on, an hour after the launch
    that reported it clean. Reading the file each time is the only answer that stays true.

    The one thing it cannot recover is *why* a write failed -- that sentence, with the operating
    system's own words, is in the log at ERROR. What it reports instead is the state that failure
    left behind, which is what the user has to act on either way.

    Args:
        project_dir: The directory marimo was launched from.

    Returns:
        The current state of the control, with a sentence fit to put in front of a user.
    """
    path = project_dir / CONFIG_FILENAME
    found = _read_existing(path)
    if found is None:
        return _unreadable(path)
    secret_keys = _secret_shaped_keys(found)

    if not path.is_file():
        detail = (
            f"there is no {CONFIG_FILENAME} in {project_dir}, so nothing is disabling marimo's "
            f"built-in AI assistant and what it sends will not appear in kedge's outbound log"
        )
        return _lockdown(path, enforced=False, detail=detail, secret_keys=secret_keys)

    unset = tuple(
        f"{table}.{key}"
        for table, key, value in FORCED
        if not (isinstance(found.get(table), dict) and found[table].get(key) == value)
    )
    if unset:
        detail = (
            f"{path} does not set {' or '.join(unset)}, so marimo's built-in AI assistant is "
            f"live for this notebook and what it sends will not appear in kedge's outbound log"
        )
        return _lockdown(path, enforced=False, detail=detail, secret_keys=secret_keys)

    return _assess(project_dir, path, secret_keys=secret_keys)


def _unreadable(path: Path) -> AssistantLockdown:
    """The outcome for a config that is there but could not be opened.

    Not enforced, and deliberately so: the file is left exactly as it is. Note that
    ``secret_keys`` is empty here because nothing was *scanned*, not because nothing was found --
    the detail says so, since an empty tuple would otherwise read as a clean bill of health.
    """
    detail = (
        f"{path} is there but could not be read, so kedge has left it alone rather than replace "
        f"a file it cannot merge -- discarding your marimo settings to assert two of them would "
        f"be the worse trade, and it could not be scanned for stored credentials either. marimo "
        f"cannot read it any better, so it falls back to its defaults: its built-in AI assistant "
        f"is live for this session and what it sends will not appear in kedge's outbound log. "
        f"Close whatever is holding that file, or delete it so the next launch writes a fresh one"
    )
    logger.error("marimo's AI assistant could not be disabled: %s", detail)
    return AssistantLockdown(path=path, enforced=False, detail=detail)


def _assess(project_dir: Path, path: Path, *, secret_keys: tuple[str, ...]) -> AssistantLockdown:
    """Judge a ``.marimo.toml`` that does say the right thing, against what still outranks it."""
    outranking = _overriding_pyproject(project_dir)
    if outranking is not None:
        pyproject, keys = outranking
        detail = (
            f"{path} disables marimo's built-in AI assistant, but {pyproject} sets "
            f"{' and '.join(keys)} under [tool.marimo], which marimo merges on top of it. The "
            f"assistant is live for this session and what it sends will not appear in kedge's "
            f"outbound log. Remove those keys from that file, or convert the workbook from a "
            f"directory outside that project"
        )
        logger.error("marimo's AI assistant is re-enabled by a pyproject.toml: %s", detail)
        return _lockdown(path, enforced=False, detail=detail, secret_keys=secret_keys)

    return _lockdown(
        path,
        enforced=True,
        detail=f"marimo's built-in AI assistant is disabled by {path}",
        secret_keys=secret_keys,
    )


def _lockdown(
    path: Path, *, enforced: bool, detail: str, secret_keys: tuple[str, ...]
) -> AssistantLockdown:
    """Assemble the result, folding any secret-shaped keys into the sentence a caller renders."""
    if secret_keys:
        named = ", ".join(secret_keys)
        plural = "them" if len(secret_keys) > 1 else "it"
        detail = (
            f"{detail}. Separately, {path} holds {named} in plaintext, beside the workbook and "
            f"inside whatever that directory is synced or shared with. Move {plural} out of that "
            f"file if that is not where the credential should live -- kedge has not altered "
            f"{plural}, because a key entered deliberately is not kedge's to delete"
        )
    return AssistantLockdown(path=path, enforced=enforced, detail=detail, secret_keys=secret_keys)


def _secret_shaped_keys(config: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the dotted paths of every key in ``config`` that looks like a stored credential.

    Matched on the key *name* rather than on a list of known paths, because the paths that hold
    one are marimo's to change: ``ai.custom_providers`` is a free-form mapping of provider name to
    config, and ``completion.api_key`` is a deprecated field marimo's own mask list no longer
    covers. Only non-empty strings count, so the defaults ``save_config`` writes back -- and the
    masking placeholder, on the off chance one reaches disk -- raise nothing.
    """
    found: list[str] = []

    def walk(node: Any, trail: str) -> None:
        if isinstance(node, dict):
            for raw_key, value in node.items():
                key = str(raw_key)
                where = f"{trail}.{key}" if trail else key
                if _is_secret_shaped(key) and _holds_a_value(value):
                    found.append(where)
                    continue
                walk(value, where)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{trail}[{index}]")

    walk(dict(config), "")
    return tuple(sorted(found))


def _is_secret_shaped(key: str) -> bool:
    return key in _SECRET_KEY_NAMES or key.endswith(_SECRET_KEY_SUFFIX)


def _holds_a_value(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value != _SECRET_PLACEHOLDER


def _read_existing(path: Path) -> dict[str, Any] | None:
    """Return the config at ``path``, ``{}`` if there is none to keep, or ``None`` if unreadable.

    The last of those three is the load-bearing one, and collapsing it into the second is a bug
    this function used to have. **Malformed and unreadable are different.** A file whose content
    will not parse has nothing in it worth keeping, and replacing it is strictly an improvement:
    marimo cannot read it either, so it falls back to defaults that have the assistant enabled.
    A file that could not be *opened* -- a Windows lock, a file being synced, an antivirus scanner
    holding it -- has content, and kedge simply cannot see it. Merging is impossible, so writing
    would mean discarding the user's whole marimo configuration in order to assert two keys, and
    "merged, never clobbered" would be a heading rather than a behaviour.

    ``None`` therefore means *do not write*, and the caller reports the launch as unenforced.
    """
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return {}
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        logger.warning(
            "the marimo config at %s will not parse (%s); replacing it with one that disables "
            "the assistant, which is what marimo would fall back from anyway",
            path,
            exc,
        )
        return {}
    except OSError as exc:
        logger.error(
            "the marimo config at %s could not be read (%s); leaving it untouched rather than "
            "overwriting a file that cannot be merged",
            path,
            exc,
        )
        return None


def _with_assistant_off(existing: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``existing`` with :data:`FORCED` overwritten and every other key left alone."""
    merged: dict[str, Any] = dict(existing)

    for table, key, value in FORCED:
        found = merged.get(table)
        if found is not None and not isinstance(found, dict):
            logger.warning(
                "the existing marimo config has [%s] as %s rather than a table; replacing it",
                table,
                type(found).__name__,
            )
            found = None
        section: dict[str, Any] = {} if found is None else dict(found)
        if section.get(key) not in (None, value):
            logger.info(
                "marimo's %s.%s was %r in the config beside the workbook; forcing it to %r",
                table,
                key,
                section[key],
                value,
            )
        section[key] = value
        merged[table] = section

    return merged


def _overriding_pyproject(project_dir: Path) -> tuple[Path, tuple[str, ...]] | None:
    """Return the nearest ``pyproject.toml`` that would switch the assistant back on, and how.

    The walk mirrors ``marimo._config.reader.find_nearest_pyproject_toml``: upwards from the
    launch directory, first file wins, no allowlist applied to what it then reads. A file that
    cannot be read is not evidence of an override -- marimo's own reader catches the same failure
    and contributes nothing -- so it is reported as no override rather than as a maybe.
    """
    directory = project_dir
    while True:
        candidate = directory / "pyproject.toml"
        if candidate.is_file():
            break
        if directory.parent == directory:
            return None
        directory = directory.parent

    try:
        with candidate.open("rb") as handle:
            parsed = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        logger.debug("could not read %s (%s); marimo will not read it either", candidate, exc)
        return None

    tool = parsed.get("tool")
    section = tool.get("marimo") if isinstance(tool, dict) else None
    if not isinstance(section, dict):
        return None

    keys = tuple(
        f"{table}.{key}"
        for table, key, value in FORCED
        if isinstance(section.get(table), dict) and section[table].get(key, value) != value
    )
    return (candidate, keys) if keys else None


def _write_atomically(path: Path, config: Mapping[str, Any]) -> None:
    """Write ``config`` to ``path``, replacing whatever was there.

    Temporary file in the same directory then :meth:`Path.replace`, matching ``kedge.config`` and
    the marker files. Here the reason is sharper than usual: marimo falls back to its *defaults*
    when the config it finds will not parse, and its default has the assistant enabled, so a
    half-written file is not a degraded control but an absent one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    body = _BANNER + "\n" + tomli_w.dumps(dict(config))
    # mkstemp rather than a fixed name, so two launches racing cannot land on the same file.
    handle, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(body)
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
