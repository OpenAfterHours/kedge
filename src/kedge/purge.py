"""Removing a workbook and everything kedge derived from it.

Forgetting a workbook used to mean dropping one row out of ``registry.json``. Everything else
kedge writes is addressed *deterministically from the workbook's resolved path* -- the key is
``slug + sha256(path)[:12]`` and the project directory is ``<workbook parent>/<slug>.kedge`` --
so re-adding the same file did not restore a backup, it stopped hiding what had never been
removed. This module is the other half: what a workspace owns, and how to take it away.

Two rules shape it.

**Enumerate by directory, never by filename.** ``agent/tools.py`` writes files into the project
directory under names the model chooses, and every release adds another well-known artifact
beside them. A hand-maintained list of filenames would be correct on the day it was written and
quietly incomplete for ever after -- which is the same shape as the bug this module exists to
fix. The project directory therefore goes as a tree.

**A plan is built before anything is deleted.** The caller has to be able to show a user exactly
what is about to go, with counts, and get an answer, before a single file is unlinked. So
:func:`plan_purge` reads and :func:`execute` writes, and nothing does both.

Paths that a user's own configuration has pointed *outside* the project directory --
``ingest.store_dir`` and ``ingest.contract`` may both be absolute -- are held separately in
:attr:`PurgePlan.external`. kedge does not own those directories and may be sharing them with
another workbook, so whether they go is the caller's decision rather than this module's.

**Release is the same enumeration with the opposite disposition.** The successful end of a
conversion is that the notebook becomes the process and the spreadsheet is obsolete, so kedge has
to be able to take the workbook away and leave everything else standing. :func:`plan_release`
enumerates nothing of its own: it takes :func:`plan_purge`'s answer and moves every item out of
``owned`` and ``external`` into :attr:`PurgePlan.kept`, which leaves the workbook as the only
thing to remove. That is not tidiness. A second enumeration would be a hand-written list of what
a release spares -- correct on the day it was written, and then the next artifact added to
:func:`plan_purge` would start being *deleted* by a release nobody had touched. Deriving it this
way round means a new artifact is kept by default, which is the direction that fails safely.

It also settles what becomes of the machine-scoped state *in the plan*, and the answer is:
nothing. The marimo marker and the token file belong to a server that may still be serving the
notebook -- a release is not a teardown, and teardown is what clears them -- and the marker is the
only record of that server's port and token, so removing it orphans the very process
``cleanup_orphan`` exists to find. This module speaks no HTTP and therefore cannot tell a marker
for a running server from a marker for one that died months ago, and it is right not to guess:
both stay in ``kept``. A caller that *can* ask finishes the job. ``server/hub.py``'s
``_sweep_marker`` takes the pair once nothing answers on the recorded port, because what is left
by then is an inert credential in plaintext under a workspace nobody may open for a month. Read
the two together, because a reader of this file alone would get it wrong: a release *plan* keeps
the marker and the token, and a release usually does not.

The outbound payload logs are the record of what kedge sent to the model, which is evidence about
a process at the moment it becomes production; destroying it as a side effect of graduating would
be the unasked-for destruction kedge refuses everywhere else. Chat sessions are the same argument
and are not this module's to reach anyway -- ``server/sessions.py`` sits above it, so a caller
that wants them gone composes it.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from kedge.workspace import Workspace

logger = logging.getLogger(__name__)

__all__ = [
    "PurgeItem",
    "PurgePlan",
    "PurgeResult",
    "describe",
    "describe_kept",
    "execute",
    "plan_purge",
    "plan_release",
]


@dataclass(frozen=True)
class PurgeItem:
    """One thing that will be removed, described the way a user would recognise it."""

    path: Path
    """What will be unlinked or removed as a tree."""

    label: str
    """A human name for it. Goes straight into the confirmation, so it reads as prose."""

    kind: str
    """``workbook``, ``project``, or ``machine`` -- which of the three homes this sits in."""

    is_tree: bool = False
    """Whether removal means :func:`shutil.rmtree` rather than :meth:`Path.unlink`."""

    contains: int | None = None
    """How many files are inside, when that is the number worth showing (plans, runs)."""

    @property
    def exists(self) -> bool:
        """Whether there is anything here to remove right now."""
        return self.path.exists()


@dataclass(frozen=True)
class PurgePlan:
    """Everything :func:`execute` would do to a workspace, assembled without doing any of it."""

    key: str
    """The workspace key, so a caller can match this plan back to a registry row."""

    workbook: Path
    """The workbook itself. Named separately because deleting it is the irreversible part."""

    owned: tuple[PurgeItem, ...]
    """Artifacts kedge created and unambiguously owns."""

    external: tuple[PurgeItem, ...]
    """Configured locations outside the project directory. Removed only if the caller says so."""

    kept: tuple[PurgeItem, ...] = ()
    """Items this plan deliberately leaves alone.

    Empty for a purge, which spares nothing it enumerates. For a release it holds everything the
    enumeration found except the workbook, so the confirmation can *name* what survives instead
    of leaving the user to infer it from the absence of a warning.
    """

    @property
    def present(self) -> tuple[PurgeItem, ...]:
        """The owned items that are actually on disk, for a confirmation that does not lie."""
        return tuple(item for item in self.owned if item.exists)

    @property
    def external_present(self) -> tuple[PurgeItem, ...]:
        """The external items that are actually on disk."""
        return tuple(item for item in self.external if item.exists)

    @property
    def kept_present(self) -> tuple[PurgeItem, ...]:
        """The retained items that are actually on disk.

        Same reasoning as :attr:`present` from the other side: a release that promises to keep
        run records the user never had is as unreadable as a deletion that threatens a token file
        that was never written.
        """
        return tuple(item for item in self.kept if item.exists)

    @property
    def is_release(self) -> bool:
        """Whether this plan graduates the workspace rather than destroying it.

        A sound discriminator because :func:`plan_purge` emits the project directory, the marker
        and the token whether or not any of them is on disk, so a release always retains at least
        three items and a purge always retains none.
        """
        return bool(self.kept)


@dataclass(frozen=True)
class PurgeResult:
    """What :func:`execute` managed to remove, and what it could not.

    A purge that fails halfway is reported rather than raised. The caller removes the registry
    row *last* and only on a clean result, so a workspace that could not be fully deleted stays
    visible in the hub -- which is the only place the user can see it and try again.
    """

    removed: tuple[Path, ...]
    failures: tuple[tuple[Path, str], ...]

    @property
    def ok(self) -> bool:
        """Whether everything the plan named is gone."""
        return not self.failures


def plan_purge(
    workspace: Workspace,
    *,
    session_ids: Iterable[str] = (),
) -> PurgePlan:
    """Describe everything ``workspace`` owns, without touching any of it.

    Args:
        workspace: The workspace to describe.
        session_ids: Chat sessions belonging to this workbook. Their outbound payload logs are
            named after the session rather than the workspace, so they cannot be found from the
            workspace alone and have to be handed in.

    Returns:
        The plan. Building one is read-only and safe to do on every page render.
    """
    project_dir = workspace.project_dir
    owned: list[PurgeItem] = [
        PurgeItem(
            path=project_dir,
            label=f"the project directory {project_dir.name}",
            kind="project",
            is_tree=True,
            contains=_count_files(project_dir),
        ),
        PurgeItem(
            path=workspace.marker_path,
            label="the marimo marker",
            kind="machine",
        ),
        PurgeItem(
            path=workspace.token_file_path,
            label="the marimo token",
            kind="machine",
        ),
    ]
    owned.extend(
        PurgeItem(
            path=workspace.outbound_log_path(session),
            label=f"the outbound payload log for chat {session}",
            kind="machine",
        )
        for session in session_ids
    )

    external = [
        item
        for item in (
            _external(workspace.handins_dir, "the hand-in store", project_dir),
            _external(workspace.contract_path, "the hand-in contract", project_dir),
        )
        if item is not None
    ]

    return PurgePlan(
        key=workspace.key,
        workbook=workspace.workbook_path,
        owned=tuple(owned),
        external=tuple(external),
    )


def plan_release(
    workspace: Workspace,
    *,
    session_ids: Iterable[str] = (),
) -> PurgePlan:
    """Describe releasing ``workspace``: the workbook goes, everything else stays.

    Releasing is what a finished conversion looks like. The notebook is run monthly on new data
    and *is* the process; the spreadsheet it was translated from is obsolete, and kedge should be
    able to say so rather than modelling it as a file somebody lost.

    Derived from :func:`plan_purge` rather than enumerated separately, for the reason the module
    docstring gives: anything added to that enumeration later must be *kept* by a release without
    anyone remembering to say so.

    Args:
        workspace: The workspace to release.
        session_ids: Chat sessions belonging to this workbook. Passed through so the confirmation
            can name their payload logs among what is kept; nothing here removes them.

    Returns:
        A plan whose only removal is the workbook, with everything it spares in
        :attr:`PurgePlan.kept`. Building one is read-only.
    """
    plan = plan_purge(workspace, session_ids=session_ids)
    return replace(plan, owned=(), external=(), kept=plan.owned + plan.external)


def execute(
    plan: PurgePlan,
    *,
    include_workbook: bool,
    include_external: bool = False,
) -> PurgeResult:
    """Carry out ``plan``, collecting failures rather than stopping at the first one.

    The workbook goes **last**. It is the one file the user may still have a use for and the one
    thing kedge cannot regenerate, so if anything else fails the source is still there.

    A :func:`plan_release` plan is carried out by this same call and needs no mode of its own: it
    holds nothing owned and nothing external, so ``include_workbook=True`` removes the workbook
    and leaves the notebook, the plans, the contract, the run records and the acceptance exactly
    where they are. ``include_external`` cannot make a release destroy a shared hand-in store,
    because a release plan has no external items for it to reach.

    Args:
        plan: What to do, from :func:`plan_purge` or :func:`plan_release`.
        include_workbook: Whether to delete the workbook file itself.
        include_external: Whether to delete configured locations outside the project directory.

    Returns:
        What went and what did not.
    """
    removed: list[Path] = []
    failures: list[tuple[Path, str]] = []

    items = list(plan.owned)
    if include_external:
        items.extend(plan.external)
    for item in items:
        _remove(item.path, is_tree=item.is_tree, removed=removed, failures=failures)

    if include_workbook:
        _remove(plan.workbook, is_tree=False, removed=removed, failures=failures)

    logger.info(
        "%s workspace %s: %d removed, %d kept, %d failed",
        "released" if plan.is_release else "purged",
        plan.key,
        len(removed),
        len(plan.kept),
        len(failures),
    )
    return PurgeResult(removed=tuple(removed), failures=tuple(failures))


def _remove(
    path: Path,
    *,
    is_tree: bool,
    removed: list[Path],
    failures: list[tuple[Path, str]],
) -> None:
    """Remove one path, recording the outcome. Absent is success: the goal is that it is gone."""
    try:
        if is_tree:
            if not path.exists():
                return
            shutil.rmtree(path)
        elif not path.exists():
            return
        else:
            path.unlink()
    except OSError as exc:
        # Windows holds a lock on an open workbook and on a notebook a running marimo has
        # imported, and that is exactly when a user reaches for delete. Naming the file and the
        # reason is the difference between "close Excel and try again" and a shrug.
        logger.warning("could not remove %s: %s", path, exc)
        failures.append((path, str(exc)))
    else:
        removed.append(path)


def _external(path: Path, label: str, project_dir: Path) -> PurgeItem | None:
    """A configured path, but only if the user has pointed it outside the project directory."""
    if _is_within(path, project_dir):
        return None
    return PurgeItem(
        path=path,
        label=f"{label} at {path}",
        kind="external",
        is_tree=path.is_dir(),
        contains=_count_files(path) if path.is_dir() else None,
    )


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _count_files(directory: Path) -> int | None:
    """How many files are under ``directory``. ``None`` if it is not there or cannot be read."""
    if not directory.is_dir():
        return None
    try:
        return sum(1 for path in directory.rglob("*") if path.is_file())
    except OSError:
        return None


def describe(plan: PurgePlan, *, sessions: int = 0) -> Sequence[str]:
    """Phrases naming what a purge will remove, for a confirmation the user can act on.

    Counts come from the plan rather than from a guess, because the point of the confirmation is
    that signed-off run records are in scope and the user cannot otherwise know it.

    On a :func:`plan_release` plan this is empty, correctly: a release removes only the workbook,
    which the caller names itself. What that confirmation wants is :func:`describe_kept`, and
    ``sessions`` belongs there too, since a release keeps them.
    """
    lines = _phrases(plan.present)
    if sessions:
        lines.append(f"{sessions} chat session{'s' if sessions != 1 else ''}")
    lines.extend(item.label for item in plan.external_present)
    return lines


def describe_kept(plan: PurgePlan, *, sessions: int = 0) -> Sequence[str]:
    """Phrases naming what a release leaves standing, for a confirmation that reassures.

    The counterpart to :func:`describe`, and the load-bearing half of a release: the user is
    being asked to delete the spreadsheet a whole process was built on, and the only thing that
    makes that a reasonable click is being told, with counts read off the disk, exactly what is
    still there afterwards.

    Args:
        plan: A plan from :func:`plan_release`. A purge plan keeps nothing and yields nothing.
        sessions: How many chat sessions belong to this workbook. They survive a release, so they
            are named here rather than in :func:`describe`.

    Returns:
        One phrase per retained thing that is really on disk.
    """
    lines = _phrases(plan.kept_present)
    if sessions:
        lines.append(f"{sessions} chat session{'s' if sessions != 1 else ''}")
    return lines


def _phrases(items: Iterable[PurgeItem]) -> list[str]:
    """One phrase per item, carrying the file count where there is one worth showing."""
    return [
        f"{item.label} ({item.contains} files)" if item.contains else item.label for item in items
    ]
