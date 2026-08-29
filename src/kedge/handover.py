"""Handing a converted process to the team, and what must not go with it.

After a conversion the notebook *is* the monthly process and the workbook is obsolete. The team
then wants to run it and share it, and the destination is **mooring** -- a sibling project that
shares marimo notebooks over the GitHub REST API into one private team repo.

kedge does not depend on mooring, shell out to it, or require it to be installed. What this
module produces is a *description*: what would be shared, what would be withheld and why,
whether the target repo declares ``kedge``, and the steps to take in order. Nothing here writes,
copies, uploads or deletes anything. A caller renders it; a person runs it.

**The blocker this module exists for.** A generated kedge notebook is not standalone. Its import
cell reaches for ``kedge``, ``kedge.contracts``, ``kedge.ingest``, ``kedge.reconcile``,
``kedge.runs``, ``kedge.sql`` and ``kedge.xl`` (:mod:`kedge.notebook.scaffold`), and mooring runs
notebooks in the repo's locked environment -- a ``pyproject.toml`` and ``uv.lock`` at the
workspace root. Unless that file declares ``kedge``, the notebook dies at its first cell, and it
dies **invisibly**: in marimo's app mode an error renders as nothing at all, so a colleague opens
the link and sees a page that just ends. Reading the repo's ``pyproject.toml`` is therefore not a
nicety, and neither is naming ``mooring deps add kedge`` when it is missing. Mooring has the same
convention for its own Excel writer -- the run completes and the command says what to add -- and
this follows it: a repo that cannot be read is reported as *cannot tell*, never as a failure.

Reading that file is where the care goes, because **a `pyproject.toml` can name kedge and still
not install it**, and every such route ends at the same blank page. A requirement carrying an
environment marker installs only where the marker holds; ``[tool.uv.sources]`` can redirect a
perfectly ordinary ``dependencies = ["kedge"]`` to a path only this machine has -- which is what
a kedge developer's own mooring repo looks like, so it is the shape most likely to be tested by
the one person who would never see it fail. :class:`DependencyState` keeps those apart from a
plain yes, and only :attr:`DependencyState.DECLARED` is one.

**Where it goes, and why there.** The project directory lands whole, at
``notebooks/<workbook>.kedge/``. ``notebooks`` is one of the three folders mooring syncs by
default, and a path is in scope when it *equals or is nested under* one of them -- at any depth,
notebooks and everything else alike. So the directory travels as it stands, the plan, the
contract, the acceptance record and the run history stay beside the notebook they describe, and
there is no registration step to run at all. That adjacency is the point: it is what makes a
converted process auditable a year later, and splitting it into ``notebooks/`` and ``reports/``
reads better to a mooring native and breaks it.

Neither of mooring's two ways of registering a folder *outside* those three fits, and it is the
same obstacle twice. ``mooring adopt`` discovers its candidates from the remote branch, so it can
only adopt a folder whose files are already in the repo -- which is the thing being asked for.
``mooring new`` does register the folder it is handed, but as a side effect of scaffolding a
notebook from a template at that path, which is the wrong instrument to point at a directory that
already holds a real one. A folder mooring already syncs needs neither, so that is where this
puts it. Note :data:`SYNCED_FOLDERS` is mooring's *default* and a team can widen it; a project
already sitting in a folder the team added is told to move one it did not need to, which is
wrong in the harmless direction and cheaper than reading another tool's config.

**One thing that lands in the repo without being listed here, and the one hard stop.** kedge
writes a ``.marimo.toml`` into the project directory to force marimo's own AI assistant off
(:mod:`kedge.marimo_config`), so a project under ``notebooks/`` carries a nested one. Nothing
needs reconciling with mooring's root file -- marimo searches upwards from the working directory,
so each tool's own launch finds its own first. It is skipped here with the other dot-entries and
is absent from the push this module spells out.

Being absent from *that* command is not the same as staying behind, and this is where the
distinction bites. marimo rewrites that file with the **whole merged user config** whenever
somebody changes a setting from inside the editor, so it is not reliably ours alone -- a personal
``ai.open_ai.api_key`` can end up in it. The file then travels with the folder whatever this
module's push names, and a later bare ``mooring push`` or ``git add .`` sends it to everyone with
access. So a credential found there is a :attr:`Handover.blockers` entry, not a warning: it is the
only thing on this path that cannot be undone once it is pushed, and a warning would sit among
others and be scrolled past. The launch path deliberately does *not* refuse over the same file --
the key is already on disk there and refusing removes the route to the panel that clears it --
which is exactly why the stop belongs at the copy instead. Reported by key **name** only; the
value is never read, rendered or logged.

Its one blind spot is worth stating: a ``.marimo.toml`` that will not parse reports no keys,
because nothing was scanned rather than because nothing was found. Blocking on "kedge could not
read a file" would be amber on every odd file forever, so it does not.

**What travels, and what does not.**

*Hand-ins do not.* ``<project>/handins/`` holds production finance extracts -- the actual figures,
not a description of them. Nothing else in the project directory does. The default is to withhold
them and :attr:`kedge.config.IngestConfig.share_handins` is the per-project opt-in. Two things
this gets wrong if it is done casually. Finding the store is containment in both directions
rather than an equality test, because ``store_dir`` can put it at any depth and a leftover
directory can outlive a move -- see :func:`_store_overlap`, which has been walked into from both
sides. And withholding is only as good as the *push*: ``mooring push`` with no paths uploads
every changed file, so the step list names the shared paths explicitly and must stay verbose,
while a store inside the shared directory raises a warning pointing at ``[ingest] store_dir``.
A store outside the project directory cannot be pushed by accident at all, which is why that is
the fix the warnings recommend.

*Run records do.* ``<project>/runs/`` is the audit trail: which hand-in each step consumed, by
digest; what was approved, with the reason typed at the time; and when. That is precisely the
question a private team repo exists to answer -- was this month's process carried out, and who
signed it off -- and an audit trail that lives on one laptop is not an audit trail. It is also
what lets somebody else resume a run that was left half-finished, which is the reason
:mod:`kedge.runs` writes to disk rather than to widget state. The argument against is real and
worth stating: a run record carries times, free-text decision notes and
:attr:`kedge.runs.RunState.parameters`, which is whatever the notebook chose to record, so a
figure somebody typed can end up in it. It carries hand-in *digests*, though, never hand-in
*contents* -- so the exposure is a description of the process, which is what the rest of the
directory is. If a project genuinely cannot share it, that wants the same opt-out shape hand-ins
have rather than a flipped default.

Entries the analyser did not write are shared and *named in a warning*, because the alternative
-- silently withholding something -- is a notebook that fails at run time on a colleague's
machine for a reason nobody can see. ``__pycache__`` and dot-entries are ignored outright.

Example:
    >>> from pathlib import Path
    >>> plan = plan_handover(Path("q2.kedge"), Path("workspace"))
    >>> plan.workspace_path
    'notebooks/q2.kedge'
"""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from kedge.config import Config
from kedge.marimo_config import inspect_marimo_assistant
from kedge.workspace import PROJECT_DIR_SUFFIX

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_WORKSPACE_PARENT",
    "HANDINS_DIR_NAME",
    "HANDINS_REASON",
    "PACKAGE_NAME",
    "PYPROJECT_FILENAME",
    "SYNCED_FOLDERS",
    "DependencyReport",
    "DependencyState",
    "Handover",
    "HandoverStep",
    "ShareItem",
    "plan_handover",
    "read_dependency",
    "requirement_marker",
    "requirement_name",
]

PACKAGE_NAME = "kedge"
"""The distribution the generated notebook's import cell needs in the repo's locked environment."""

PYPROJECT_FILENAME = "pyproject.toml"

SYNCED_FOLDERS = ("notebooks", "data", "reports")
"""The workspace folders mooring syncs unless a team says otherwise.

A path is in scope when it equals one of these or is nested under it, at any depth, whatever its
extension -- which is what lets a whole ``<workbook>.kedge/`` directory travel with no
registration step. Mooring's default rather than a fact: a team that widens its own list will see
this module propose a move that was not needed.
"""

DEFAULT_WORKSPACE_PARENT = SYNCED_FOLDERS[0]
"""Where a project directory goes when it is not already somewhere mooring syncs."""

HANDINS_REASON = (
    "hand-ins are production extracts: the figures themselves, not a description of them"
)

HANDINS_DIR_NAME = "handins"
"""The default store name, withheld wherever it appears.

Matched by name *as well as* by containment against the resolved ``[ingest] store_dir``, so a
directory left behind by a project that has since moved its store elsewhere does not quietly
become an unrecognised entry and travel. :func:`_store_overlap` is the whole rule."""

_COUNT_LIMIT = 1_000
"""Where counting hand-in files gives up. See :func:`_count_files`."""

_IGNORED_NAMES = frozenset({"__pycache__", "__marimo__"})
"""Never listed either way. Build artefacts and editor state are nobody's deliverable."""

_SHARED_REASONS: dict[str, str] = {
    "plans": "the approved plan history -- what the conversion was agreed to do, version by version",
    "runs": "the audit trail: which hand-in each step consumed, what was approved, and when",
    "contract.yaml": "the shape every hand-in must arrive in, which the notebook enforces on each run",
    "reconciliation.json": "whether this translation was ever accepted against the workbook it came from",
    "analysis.json": "what the analyser found in the workbook, which the plan was built from",
    "report.html": "the readable conversion report",
}


# ── the shape of a requirement ───────────────────────────────────────────────────────────────

# PEP 508: a name, then optionally extras, a version specifier, a URL or an environment marker.
# The name character class already stops at every one of those, so the lookahead is only there to
# reject a requirement that is not one -- `kedge$$` matches the name and then nothing legal.
_REQUIREMENT_NAME = re.compile(
    r"\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*(?=[\[(<>=!~;@]|$)"
)

# PEP 503: runs of `-`, `_` and `.` are one separator, and comparison is case-insensitive.
_NAME_SEPARATORS = re.compile(r"[-_.]+")


def _normalise(name: str) -> str:
    return _NAME_SEPARATORS.sub("-", name).lower()


def requirement_name(requirement: str) -> str | None:
    """The distribution a PEP 508 requirement string names, normalised per PEP 503.

    A substring match is the wrong tool here and fails in the direction that matters: it reports
    ``kedge-extras`` as ``kedge`` and tells the user the notebook will run when it will not.

    Args:
        requirement: One entry from a ``dependencies`` list -- ``"kedge"``, ``"kedge>=0.1"``,
            ``"kedge[extra]==1.2"``, ``"kedge @ https://..."``, or one carrying an environment
            marker.

    Returns:
        The lower-cased name with runs of ``-_.`` collapsed to a single hyphen, or ``None`` if
        the string does not begin with a distribution name.
    """
    match = _REQUIREMENT_NAME.match(requirement)
    return None if match is None else _normalise(match.group(1))


def requirement_marker(requirement: str) -> str | None:
    """The environment marker a PEP 508 requirement carries, or ``None`` if it carries none.

    Reported, never evaluated. Deciding whether ``python_version < '3.0'`` can ever be true is
    what ``packaging.markers`` is for, and kedge does not declare ``packaging`` -- it is present
    only transitively, and building a load-bearing check on a dependency nobody declared is how a
    resolver change becomes an ImportError at the point somebody is trying to share a notebook.
    Reimplementing marker evaluation here would be worse: the failure mode is a wrong answer
    rather than a missing one, on the question this whole module exists to answer.

    So a marker downgrades the verdict to :attr:`DependencyState.CONDITIONAL` and is quoted back
    for a person to judge. That is the safe direction, and it is the honest one for a check whose
    subject is not this machine anyway: a marker true here says nothing about mooring's runner or
    a colleague's laptop. Declaring ``packaging`` would let this narrow to "can never be true",
    and is the obvious upgrade if the nagging turns out to be noise.

    Args:
        requirement: A PEP 508 requirement string.

    Returns:
        The marker text with surrounding whitespace removed, or ``None``. The split is on the
        first ``;``, which is the separator PEP 508 defines; a URL containing an unescaped ``;``
        would fool it, and no index-installable requirement has one.
    """
    _, separator, marker = requirement.partition(";")
    return marker.strip() if separator and marker.strip() else None


class DependencyState(StrEnum):
    """Whether the target repo's locked environment will have ``kedge`` in it.

    Only :attr:`DECLARED` is a yes. The other five are the ways a ``pyproject.toml`` can *mention*
    kedge while a colleague still opens a blank page, and they are kept apart because each one
    wants a different sentence and, in one case, a different command.
    """

    DECLARED = "declared"
    """Named unconditionally in ``[project] dependencies``, resolving from an index. The notebook's
    import cell will resolve."""

    CONDITIONAL = "conditional"
    """Named in ``[project] dependencies``, but carrying an environment marker.

    ``kedge; python_version < '3.0'`` installs nowhere; ``kedge; sys_platform == 'win32'`` installs
    for some of the team and not the rest. Deciding which needs a marker evaluator, and the one
    worth using lives in ``packaging``, which kedge does not declare -- so this reports the marker
    rather than judging it, and errs towards asking. See :func:`requirement_marker`."""

    LOCAL_SOURCE = "local_source"
    """Named in ``[project] dependencies``, but ``[tool.uv.sources]`` resolves it from a path.

    The state a kedge developer's own mooring repo lands in, and the most dangerous of the six:
    the declaration is perfect, the lock is local, and the notebook dies on every machine but
    one. The fix is not ``deps add`` -- adding it again changes nothing while the source override
    stands."""

    OPTIONAL = "optional"
    """Named only in an optional extra or a dependency group, so whether it is installed depends
    on how the environment was synced. Reported separately rather than as declared, because a
    ``uv sync`` without the group produces exactly the blank page this module exists to prevent.
    Found even in a file with no ``[project]`` table at all."""

    ABSENT = "absent"
    """The file parsed and does not name it. The one state that justifies ``mooring deps add``."""

    UNKNOWN = "unknown"
    """Cannot tell: no ``pyproject.toml``, or one that will not parse. Never a failure."""


@dataclass(frozen=True, slots=True)
class DependencyReport:
    """What the target repo's ``pyproject.toml`` says about one package.

    Example:
        >>> report = DependencyReport(DependencyState.ABSENT, Path("pyproject.toml"), "no")
        >>> report.satisfied
        False
    """

    state: DependencyState
    pyproject_path: Path
    detail: str
    """One sentence, always populated, naming the file and what was found in it."""
    requirement: str | None = None
    """The requirement string that declared it, verbatim, when one did."""

    @property
    def satisfied(self) -> bool:
        """True only for :attr:`DependencyState.DECLARED`.

        An optional extra and an unreadable file are both "add it and find out", so neither is a
        pass. The safe direction here is the one that names a command.
        """
        return self.state is DependencyState.DECLARED


def _iter_requirements(table: object) -> Iterator[str]:
    """Every string in a mapping of name to requirement list, skipping anything else.

    ``[dependency-groups]`` entries may be tables (``{include-group = "dev"}``), and a malformed
    file may hold anything at all. Neither is a distribution name.
    """
    if not isinstance(table, dict):
        return
    for entries in table.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, str):
                yield entry


def _named(requirements: Iterable[str], wanted: str) -> str | None:
    """The first requirement string naming ``wanted``, verbatim, or ``None``."""
    return next((entry for entry in requirements if requirement_name(entry) == wanted), None)


def _local_source(data: dict[str, Any], wanted: str) -> str | None:
    """Where ``[tool.uv.sources]`` redirects ``wanted`` to, when that is somewhere local.

    uv lets a repository override where a declared dependency resolves from, and two of those
    overrides are machine-local: ``path`` names a directory on this disk, and ``workspace = true``
    names a sibling member of this repository. Either one makes ``dependencies = ["kedge"]`` a
    declaration that installs here and nowhere else -- which is exactly what a kedge developer's
    own mooring repo looks like, so it is the shape most likely to be tested by the one person who
    would never see it fail.

    ``git``, ``url`` and ``index`` overrides are left alone: they name somewhere a colleague's
    machine can also reach, so they do not weaken the declaration.

    Args:
        data: The parsed ``pyproject.toml``.
        wanted: The PEP 503 normalised distribution name.

    Returns:
        A phrase naming the local location, for an error message, or ``None`` when nothing
        redirects ``wanted`` anywhere local.
    """
    tool = data.get("tool")
    uv = tool.get("uv") if isinstance(tool, dict) else None
    sources = uv.get("sources") if isinstance(uv, dict) else None
    if not isinstance(sources, dict):
        return None
    for name, source in sources.items():
        if not isinstance(name, str) or _normalise(name) != wanted:
            continue
        # uv accepts a list of sources, each with its own marker, as well as a single table.
        for candidate in source if isinstance(source, list) else [source]:
            if not isinstance(candidate, dict):
                continue
            path = candidate.get("path")
            if isinstance(path, str):
                return f"the local path {path!r}"
            if candidate.get("workspace") is True:
                return "a workspace member of this repository"
    return None


def read_dependency(repo_dir: Path, *, package: str = PACKAGE_NAME) -> DependencyReport:
    """Ask a repository's ``pyproject.toml`` whether it declares ``package``.

    Degrades in every direction. A missing file, a file that will not decode, a file with no
    ``[project]`` table and a ``dependencies`` key that is not a list all come back as
    :attr:`DependencyState.UNKNOWN` with a sentence saying so. Nothing raises.

    Args:
        repo_dir: The mooring workspace root -- the directory holding ``pyproject.toml``
            and ``uv.lock``.
        package: The distribution to look for. Defaults to ``kedge``.

    Returns:
        A :class:`DependencyReport`. Its ``detail`` is safe to show a user as-is.
    """
    path = repo_dir / PYPROJECT_FILENAME
    wanted = _normalise(package)

    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return DependencyReport(
            DependencyState.UNKNOWN,
            path,
            f"there is no {PYPROJECT_FILENAME} at {path}, so whether the repo declares "
            f"{package} cannot be read from here",
        )
    except OSError as exc:
        return DependencyReport(DependencyState.UNKNOWN, path, f"could not read {path}: {exc}")

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        logger.info("could not parse %s: %s", path, exc)
        return DependencyReport(
            DependencyState.UNKNOWN, path, f"{path} will not parse as TOML: {exc}"
        )

    def optional_report(entry: str) -> DependencyReport:
        return DependencyReport(
            DependencyState.OPTIONAL,
            path,
            f"{path} names {package} as {entry!r}, but only in an optional extra or a "
            f"dependency group, so whether it is installed depends on how the environment "
            f"was synced",
            requirement=entry,
        )

    # Gathered before the [project] guard, not after it. Reaching them only on the far side of
    # that guard meant a file with dependency groups and no [project] table returned "cannot
    # tell" while OPTIONAL's own docstring claimed such a file was detected.
    groups = list(_iter_requirements(data.get("dependency-groups")))

    project = data.get("project")
    if not isinstance(project, dict):
        entry = _named(groups, wanted)
        if entry is not None:
            return optional_report(entry)
        return DependencyReport(
            DependencyState.UNKNOWN,
            path,
            f"{path} has no [project] table, so it declares no dependencies kedge can read",
        )

    declared = project.get("dependencies", [])
    if not isinstance(declared, list):
        return DependencyReport(
            DependencyState.UNKNOWN,
            path,
            f"[project] dependencies in {path} is not a list, so it cannot be read as "
            f"requirement strings",
        )

    entry = _named((item for item in declared if isinstance(item, str)), wanted)
    if entry is not None:
        # Named is not the same as installable, twice over. Both checks below turn a declaration
        # that reads perfectly into one a colleague's machine will not honour.
        local = _local_source(data, wanted)
        if local is not None:
            return DependencyReport(
                DependencyState.LOCAL_SOURCE,
                path,
                f"{path} declares {package} as {entry!r}, but [tool.uv.sources] resolves it from "
                f"{local} -- somewhere this machine has and a colleague's checkout and mooring's "
                f"runner do not",
                requirement=entry,
            )
        marker = requirement_marker(entry)
        if marker is not None:
            return DependencyReport(
                DependencyState.CONDITIONAL,
                path,
                f"{path} declares {package} as {entry!r}, so it installs only where {marker!r} "
                f"holds, which kedge does not evaluate",
                requirement=entry,
            )
        return DependencyReport(
            DependencyState.DECLARED,
            path,
            f"{path} declares {package} as {entry!r}",
            requirement=entry,
        )

    entry = _named([*_iter_requirements(project.get("optional-dependencies")), *groups], wanted)
    if entry is not None:
        return optional_report(entry)

    return DependencyReport(
        DependencyState.ABSENT,
        path,
        f"{path} does not declare {package}",
    )


# ── the handover ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ShareItem:
    """One top-level entry of the project directory, and whether it travels.

    Example:
        >>> item = ShareItem(Path("q2.kedge/plans"), "notebooks/q2.kedge/plans", True, "why", True)
        >>> item.name
        'plans'
    """

    path: Path
    workspace_path: str
    """Where it would sit in the mooring workspace, as a forward-slash relative path."""
    shared: bool
    reason: str
    """Why it travels, or why it does not. Shown to the user verbatim."""
    is_dir: bool

    @property
    def name(self) -> str:
        """The entry's own name, without any directories above it."""
        return self.path.name


@dataclass(frozen=True, slots=True)
class HandoverStep:
    """One thing to do, in order.

    ``command`` is ``None`` for the one step that is not a command -- placing the project
    directory inside the workspace, which is a file move whose spelling depends on the shell.

    Example:
        >>> HandoverStep("Share it.", "mooring push notebooks/q2.kedge").command
        'mooring push notebooks/q2.kedge'
    """

    instruction: str
    command: str | None = None


@dataclass(frozen=True, slots=True)
class Handover:
    """Everything a caller needs to hand one converted process to the team.

    Always constructible: a project directory that does not exist, or holds no notebook, comes
    back with the fact in :attr:`blockers` rather than as an exception, because the caller is a
    CLI command or a route that has to say something either way.

    Example:
        >>> plan = plan_handover(Path("q2.kedge"), Path("workspace"))
        >>> plan.ready
        False
    """

    project_dir: Path
    repo_dir: Path
    workspace_path: str
    """Where the project directory sits, or would sit, relative to the workspace root."""
    notebook: str | None
    """The generated notebook's filename, or ``None`` if the directory holds no ``.py`` file."""
    handins_dir: Path
    handins_shared: bool
    """``[ingest] share_handins``. False is the default and the safe one."""
    items: tuple[ShareItem, ...]
    dependency: DependencyReport
    steps: tuple[HandoverStep, ...]
    warnings: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    """Reasons this handover cannot be carried out at all, as opposed to carried out carefully."""

    @property
    def shared(self) -> tuple[ShareItem, ...]:
        """The entries that travel to the team repo."""
        return tuple(item for item in self.items if item.shared)

    @property
    def withheld(self) -> tuple[ShareItem, ...]:
        """The entries that stay on this machine, each carrying the reason."""
        return tuple(item for item in self.items if not item.shared)

    @property
    def commands(self) -> tuple[str, ...]:
        """Just the command lines from :attr:`steps`, in order, for a copyable block."""
        return tuple(step.command for step in self.steps if step.command is not None)

    @property
    def ready(self) -> bool:
        """Whether there is anything to hand over. Warnings do not make it False."""
        return not self.blockers


def _handins_dir(project_dir: Path, config: Config) -> Path:
    """Resolve ``[ingest] store_dir`` the way :class:`kedge.workspace.Workspace` does.

    Repeated here rather than imported so the module stays a pure description over two paths, and
    so a caller that already holds a workspace can hand its ``handins_dir`` in instead.
    """
    configured = config.ingest.store_dir
    if configured is not None:
        return configured if configured.is_absolute() else project_dir / configured
    return project_dir / HANDINS_DIR_NAME


def _count_files(directory: Path) -> int:
    """How many files are under ``directory``, stopping at :data:`_COUNT_LIMIT`.

    The number is only there to make a warning concrete, and a hand-in store can hold years of
    extracts; walking all of them to say "a lot" is work nobody asked for. Zero if unreadable.
    """
    total = 0
    try:
        for entry in directory.rglob("*"):
            if entry.is_file():
                total += 1
                if total >= _COUNT_LIMIT:
                    break
    except OSError as exc:
        logger.info("could not count files under %s: %s", directory, exc)
    return total


def _count_text(total: int) -> str:
    """``"3"``, or ``"1000 or more"`` once the walk gave up counting."""
    return f"{total} or more" if total >= _COUNT_LIMIT else str(total)


def _quote(path: str) -> str:
    return f'"{path}"' if " " in path else path


def _resolve(path: Path) -> Path:
    """``Path.resolve`` that never raises: an unresolvable path is used as given."""
    try:
        return path.expanduser().resolve()
    except OSError as exc:
        logger.info("could not resolve %s: %s", path, exc)
        return path.expanduser()


def _notebook_name(entries: list[Path], project_dir: Path) -> str | None:
    """Which ``.py`` file is the generated notebook.

    Never "the first one": a project directory is allowed to hold a helper module the user wrote,
    and picking that would point ``mooring verify`` at a file that is not the process. kedge names
    the notebook after the workbook, and the project directory after the same stem
    (:class:`kedge.workspace.Workspace`), so the stems agree. Falling back to a lone ``.py`` file
    covers a directory that has been renamed by hand; two candidates and no match is ambiguous,
    and an ambiguous answer here is worse than none.
    """
    scripts = [entry for entry in entries if entry.suffix == ".py" and not entry.is_dir()]
    expected = project_dir.name.removesuffix(PROJECT_DIR_SUFFIX)
    named = next((entry.name for entry in scripts if entry.stem == expected), None)
    if named is not None:
        return named
    return scripts[0].name if len(scripts) == 1 else None


def _store_overlap(entry: Path, handins: Path) -> Literal["store", "container", "inside"] | None:
    """How a top-level entry of the project directory relates to the hand-in store.

    **Containment in both directions, never equality.** Equality is the trap this function exists
    to close, and it has now been walked into from both sides. Matching only the resolved store
    path let a stale ``handins/`` -- left behind by a project that moved its store -- fall through
    to "unrecognised", which is shared. Matching the resolved path *and* the name still missed a
    store one level down: ``[ingest] store_dir = "data/handins"`` puts the extracts under a
    top-level ``data/`` that is neither, so ``data/`` was shared and named in the push command
    while the report said hand-ins were withheld. Being told you are protected while the command
    uploads the figures is worse than having no policy.

    So: the entry is the store, or the store is somewhere beneath it, or -- when ``store_dir``
    names an ancestor of the project -- the entry is beneath the store. Depth is irrelevant in
    both directions.

    Args:
        entry: One top-level entry of the project directory.
        handins: The resolved hand-in store.

    Returns:
        ``"store"`` when the entry is the store itself, ``"container"`` when the store sits
        somewhere inside it, ``"inside"`` when the entry sits inside the store, or ``None`` when
        the two do not overlap.
    """
    if entry.name == HANDINS_DIR_NAME:
        return "store"
    resolved = _resolve(entry)
    if resolved == handins:
        return "store"
    if handins.is_relative_to(resolved):
        return "container"
    if resolved.is_relative_to(handins):
        return "inside"
    return None


def _store_reason(overlap: Literal["store", "container", "inside"], share_handins: bool) -> str:
    """Why an entry overlapping the hand-in store travels, or does not.

    The container case is spelled out rather than folded in with the rest: withholding a whole
    directory because something inside it is sensitive also withholds everything else in there,
    and a user who is not told that will wonder why the team never got it.
    """
    if share_handins:
        if overlap == "container":
            return (
                "[ingest] share_handins is on, so this travels with the hand-in store inside it; "
                f"{HANDINS_REASON}"
            )
        return f"[ingest] share_handins is on, so the hand-in store travels; {HANDINS_REASON}"
    if overlap == "container":
        return (
            f"withheld whole because the hand-in store sits inside it: {HANDINS_REASON}. Anything "
            f"else in this directory is withheld with it."
        )
    if overlap == "inside":
        return f"inside the hand-in store: {HANDINS_REASON}"
    return f"withheld by default: {HANDINS_REASON}"


def _classify(
    project_dir: Path,
    workspace_path: str,
    *,
    handins: Path,
    share_handins: bool,
) -> tuple[tuple[ShareItem, ...], str | None, tuple[str, ...]]:
    """Every top-level entry of the project directory, sorted, with the unknown ones named."""
    try:
        entries = sorted(project_dir.iterdir(), key=lambda entry: entry.name.lower())
    except OSError as exc:
        logger.info("could not list %s: %s", project_dir, exc)
        return (), None, (f"could not list {project_dir}: {exc}",)

    entries = [
        entry
        for entry in entries
        if entry.name not in _IGNORED_NAMES and not entry.name.startswith(".")
    ]
    notebook = _notebook_name(entries, project_dir)

    store_path = (
        handins.relative_to(project_dir).as_posix()
        if handins.is_relative_to(project_dir)
        else str(handins)
    )
    items: list[ShareItem] = []
    unknown: list[str] = []
    containers: list[str] = []
    for entry in entries:
        is_dir = entry.is_dir()
        relative = f"{workspace_path}/{entry.name}"
        overlap = _store_overlap(entry, handins)
        if overlap is not None:
            if overlap == "container":
                containers.append(entry.name)
            items.append(
                ShareItem(
                    entry, relative, share_handins, _store_reason(overlap, share_handins), is_dir
                )
            )
            continue
        reason = _SHARED_REASONS.get(entry.name)
        if reason is None and entry.name == notebook:
            reason = "the generated notebook -- from here on, this is the process"
        if reason is None:
            unknown.append(entry.name)
            reason = (
                "not written by kedge, and shared anyway: withholding it silently would break "
                "the notebook on somebody else's machine for a reason nobody could see"
            )
        items.append(ShareItem(entry, relative, True, reason, is_dir))

    warnings: list[str] = []
    if unknown:
        warnings.append(
            f"{project_dir} holds {len(unknown)} entry(s) kedge did not write "
            f"({', '.join(sorted(unknown))}). They will be shared. Check none of them holds data "
            f"before pushing."
        )
    if containers and not share_handins:
        listed = ", ".join(f"{name}/" for name in sorted(containers))
        warnings.append(
            f"[ingest] store_dir puts the hand-in store at {store_path}, inside {listed} -- so "
            f"the whole of {listed} is withheld, including anything in there the team would "
            f"otherwise get. Move the store out of it, or outside {project_dir} altogether, if "
            f"that directory holds something they need."
        )
    return tuple(items), notebook, tuple(warnings)


def _steps(
    *,
    workspace_path: str,
    notebook: str | None,
    needs_move: bool,
    project_dir: Path,
    repo_dir: Path,
    dependency: DependencyReport,
    shared: tuple[ShareItem, ...],
) -> tuple[HandoverStep, ...]:
    """The order the commands actually run in.

    There is no registration step: the move puts the directory somewhere mooring already syncs,
    which is the whole reason for choosing ``notebooks/`` over a folder that would need one. Then
    the dependency, because it is the one thing that decides whether the notebook runs at all.
    Then verify, then push -- that way a broken import cell is caught on the machine that made it,
    not on a colleague's blank page.
    """
    steps: list[HandoverStep] = []

    if needs_move:
        steps.append(
            HandoverStep(
                f"Move {project_dir} to {repo_dir / workspace_path}, directory intact. Everything "
                f"nested under {workspace_path.split('/')[0]}/ syncs, so the plan, the contract, "
                f"the acceptance record and the run history travel beside the notebook they "
                f"describe -- and that adjacency is what makes this auditable later."
            )
        )

    if not dependency.satisfied:
        consequence = (
            "The notebook's import cell needs it, and in marimo's app mode a failed import "
            "renders as nothing at all -- a page that just ends."
        )
        if dependency.state is DependencyState.LOCAL_SOURCE:
            # `deps add` is the wrong instrument here: it would re-add a package that is already
            # declared, and leave the source override that is the actual problem in place.
            step = HandoverStep(
                f"{dependency.detail}. {consequence} Delete that [tool.uv.sources] entry so "
                f"{PACKAGE_NAME} resolves from an index the whole team can reach, then re-lock "
                f"and push the lock.",
                "mooring deps lock",
            )
        else:
            step = HandoverStep(
                f"{dependency.detail}. {consequence} Add it, then push the lock change so the "
                f"team gets it too.",
                f"mooring deps add {PACKAGE_NAME}",
            )
        steps.append(step)

    if notebook:
        steps.append(
            HandoverStep(
                "Smoke-run the notebook once in the repo's locked environment. This is the step "
                "that catches a missing dependency, and it exits non-zero if any cell failed.",
                f"mooring verify {_quote(f'{workspace_path}/{notebook}')}",
            )
        )

    if shared:
        paths = " ".join(_quote(item.workspace_path) for item in shared)
        steps.append(
            HandoverStep(
                "Share it, naming the paths. A bare `mooring push` uploads every changed file, "
                "which is how a withheld hand-in ends up in the team repo anyway.",
                f"mooring push {paths}",
            )
        )
    return tuple(steps)


def plan_handover(
    project_dir: Path,
    repo_dir: Path,
    *,
    config: Config | None = None,
    handins_dir: Path | None = None,
    workspace_path: str | None = None,
) -> Handover:
    """Describe handing one converted process to a mooring workspace.

    Reads two directories and one ``pyproject.toml``. Writes nothing, runs nothing, and never
    raises: everything that could go wrong arrives as a :attr:`Handover.warnings` or
    :attr:`Handover.blockers` entry with a sentence naming the fix.

    Args:
        project_dir: A ``<workbook>.kedge/`` directory -- :attr:`kedge.workspace.Workspace
            .project_dir`.
        repo_dir: The root of the mooring workspace, holding ``pyproject.toml`` and ``uv.lock``.
        config: Supplies ``[ingest] share_handins`` and, when ``handins_dir`` is not given,
            ``[ingest] store_dir``. Defaults to a stock :class:`kedge.config.Config`, which
            withholds hand-ins.
        handins_dir: The resolved hand-in store, for a caller that already holds a
            :class:`kedge.workspace.Workspace`. Wins over ``config``.
        workspace_path: Where the project directory will sit relative to the workspace root --
            the destination, not necessarily where it is now. Derived when omitted: its current
            location if that is already under one of :data:`SYNCED_FOLDERS`, otherwise
            ``notebooks/<name>``, and the steps then open with the move.

    Returns:
        A :class:`Handover`. Check :attr:`Handover.ready` before rendering the steps.
    """
    settings = config or Config()
    project = _resolve(project_dir)
    repo = _resolve(repo_dir)
    handins = _resolve(handins_dir if handins_dir is not None else _handins_dir(project, settings))
    share_handins = settings.ingest.share_handins

    # Where it is now, if that is anywhere inside the workspace at all, and whether mooring would
    # sync it there. Being inside the repo is not enough: a folder outside SYNCED_FOLDERS needs a
    # registration mooring offers no way to perform, so it is moved like anything else.
    current = (
        project.relative_to(repo).as_posix()
        if project != repo and project.is_relative_to(repo)
        else None
    )
    if workspace_path is None:
        if current is not None and current.split("/")[0] in SYNCED_FOLDERS:
            workspace_path = current
        else:
            workspace_path = f"{DEFAULT_WORKSPACE_PARENT}/{project.name}"
    workspace_path = workspace_path.strip("/")
    needs_move = current != workspace_path

    dependency = read_dependency(repo)
    blockers: list[str] = []
    warnings: list[str] = []
    items: tuple[ShareItem, ...] = ()
    notebook: str | None = None

    if project.is_dir():
        # A handover is the moment this directory is copied somewhere shared, which is the one
        # point on its whole life where a credential inside it stops being a local problem. The
        # launch path deliberately does not refuse over this -- the key is already on disk, and
        # refusing there only removes the user's route to the panel that clears it -- so the hard
        # stop belongs here instead. Key *names* only; the value is never read, rendered or logged.
        lockdown = inspect_marimo_assistant(project)
        if lockdown.secret_keys:
            named = ", ".join(lockdown.secret_keys)
            blockers.append(
                f"Clear {named} from {lockdown.path}, then run this again. That file carries a "
                f"model credential into the team repository. kedge never names it in the push "
                f"below -- it is skipped with the other dot-entries -- but it sits inside the "
                f"directory being handed over, so it travels with the folder, and a later bare "
                f"`mooring push` or `git add .` sends it to everyone with access. A key in a "
                f"shared repository cannot be taken back."
            )

    if project.is_relative_to(handins):
        # The store is the project directory or an ancestor of it, so every entry is inside the
        # store and nothing can be shared. Left to the classification that is a correct answer and
        # a useless one -- an empty push and a wall of withheld paths -- so it is said once, here.
        blockers.append(
            f"[ingest] store_dir puts the hand-in store at {handins}, which contains {project} "
            f"itself, so nothing in the project directory can be shared. Point it at a directory "
            f"inside or beside the project, not one above it."
        )

    if not project.is_dir():
        blockers.append(
            f"{project} is not a directory. Convert the workbook first -- there is no notebook, "
            f"plan or contract to hand over yet."
        )
    else:
        items, notebook, listing_warnings = _classify(
            project, workspace_path, handins=handins, share_handins=share_handins
        )
        warnings.extend(listing_warnings)

    if notebook is None and not blockers:
        blockers.append(
            f"{project} holds no notebook kedge recognises: it looks for "
            f"{project.name.removesuffix(PROJECT_DIR_SUFFIX)}.py, or for a single .py file. "
            f"Convert the workbook first -- there is nothing for a colleague to open."
        )

    if not repo.is_dir():
        warnings.append(
            f"{repo} is not a directory. The steps below assume it is a mooring workspace; run "
            f"`mooring init` there first, or point this at the right one."
        )

    inside = handins.is_relative_to(project)
    handin_count = _count_files(handins) if handins.is_dir() else 0
    if share_handins and inside and handin_count:
        warnings.append(
            f"[ingest] share_handins is on, so {_count_text(handin_count)} hand-in file(s) under "
            f"{handins} will be pushed to the team repo. {HANDINS_REASON.capitalize()}, and every "
            f"member of the repo can read them."
        )
    elif share_handins and not inside:
        warnings.append(
            f"[ingest] share_handins is on, but the store is at {handins}, outside the directory "
            f"being shared, so the opt-in changes nothing. Move it under {project} if the "
            f"hand-ins really are meant to travel."
        )

    # Driven by what is actually withheld rather than by the configured store, so a directory a
    # project left behind when it moved its hand-ins elsewhere raises the same warning.
    withheld = [item.workspace_path for item in items if not item.shared]
    if withheld:
        warnings.append(
            f"{', '.join(withheld)} is withheld, but it sits inside the directory being shared. "
            f"`mooring push` with no paths uploads every changed file -- so push the paths this "
            f"handover names, or set [ingest] store_dir to somewhere outside {project}, where "
            f"nothing can push it by accident."
        )

    if dependency.state is DependencyState.UNKNOWN:
        warnings.append(f"{dependency.detail}. Check it by hand before telling anyone it is there.")

    steps = _steps(
        workspace_path=workspace_path,
        notebook=notebook,
        needs_move=needs_move,
        project_dir=project,
        repo_dir=repo,
        dependency=dependency,
        shared=tuple(item for item in items if item.shared),
    )

    logger.debug(
        "handover for %s into %s: %d shared, %d withheld, dependency %s",
        project,
        repo,
        sum(1 for item in items if item.shared),
        sum(1 for item in items if not item.shared),
        dependency.state,
    )
    return Handover(
        project_dir=project,
        repo_dir=repo,
        workspace_path=workspace_path,
        notebook=notebook,
        handins_dir=handins,
        handins_shared=share_handins,
        items=items,
        dependency=dependency,
        steps=steps,
        warnings=tuple(warnings),
        blockers=tuple(blockers),
    )
