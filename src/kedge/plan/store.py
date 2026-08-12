"""Versioned process plans on disk, alongside the notebook. No LLM.

YAML, one file per version, never overwritten. Three reasons the format and the retention rule
are what they are:

- **Human-editable.** A plan is a review artifact. A user who wants to fix a stage's intent in
  their editor should be able to, and YAML is the format that does not punish them for it.
- **Diff-friendly.** ``sort_keys=False`` and block style mean a one-word change to a stage's
  intent is a one-line diff in git, not a reflowed document.
- **History is retained rather than overwritten.** "When the process changes next quarter, the
  diff of the plan is the change record" is only true if last quarter's plan is still there
  (PLAN 2.2). :meth:`PlanStore.save` refuses to overwrite a version whose content differs.

Files are named ``plan-v001.yaml`` so that a directory listing sorts into version order, which
matters more than it sounds when the history is a dozen versions long.

References:
- PLAN.md 2.2 (saved, versioned, diffable, re-seedable), M2.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from kedge.plan.model import PlanError, ProcessPlan

logger = logging.getLogger(__name__)

__all__ = [
    "PLAN_FILENAME_PATTERN",
    "PlanStore",
    "PlanStoreError",
    "plan_from_yaml",
    "plan_to_yaml",
]

PLAN_FILENAME_PATTERN = re.compile(r"^plan-v(\d+)\.ya?ml$")

_HEADER = (
    "# kedge process plan. Edit freely: this file is a review artifact, not generated code.\n"
    "# Nothing reaches the notebook until approval.state is 'approved' (PLAN 2.2).\n"
)


class PlanStoreError(PlanError):
    """A plan could not be written to, or read from, the plan store."""


# =============================================================================
# SERIALISATION
# =============================================================================


def plan_to_yaml(plan: ProcessPlan) -> str:
    """Render a plan as the YAML written to disk.

    ``exclude_none`` keeps the file readable: a stage with no notes, no pattern and no checkpoint
    should not carry three ``null`` lines that a reviewer has to skip past.
    """
    payload = plan.model_dump(mode="json", exclude_none=True)
    body = yaml.safe_dump(
        payload,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=100,
    )
    return _HEADER + body


def plan_from_yaml(text: str) -> ProcessPlan:
    """Load a plan from YAML, validating it in full.

    Raises:
        PlanStoreError: when the text is not YAML, or is not a valid plan. A hand-edited file
            with a typo must produce a message naming the field, not a traceback.
    """
    try:
        raw: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        msg = f"plan file is not valid YAML: {exc}"
        raise PlanStoreError(msg) from exc
    if not isinstance(raw, dict):
        msg = f"plan file should contain a mapping at the top level, found {type(raw).__name__}"
        raise PlanStoreError(msg)
    try:
        return ProcessPlan.model_validate(raw)
    except ValidationError as exc:
        msg = f"plan file is not a valid process plan; {_summarise(exc)}"
        raise PlanStoreError(msg) from exc
    except ValueError as exc:
        msg = f"plan file is not a valid process plan: {exc}"
        raise PlanStoreError(msg) from exc


_SHOWN_PROBLEMS = 5


def _summarise(exc: ValidationError) -> str:
    """One line per problem: where it is, and what is wrong with it.

    pydantic's own rendering is five stanzas and a documentation URL per error, which is the right
    thing for a developer and the wrong thing for somebody who has just hand-edited a YAML file
    and wants to know which key to fix. The location is kept because that is what names the
    field — ``stages.0.confidance`` — and the count is kept because a user told only the first
    problem will fix it and be told the second.
    """
    problems = exc.errors(include_url=False)
    lines = [
        f"  - {'.'.join(str(part) for part in problem['loc']) or '(top level)'}: {problem['msg']}"
        for problem in problems[:_SHOWN_PROBLEMS]
    ]
    if len(problems) > _SHOWN_PROBLEMS:
        lines.append(f"  (+{len(problems) - _SHOWN_PROBLEMS} more)")
    plural = "problem" if len(problems) == 1 else "problems"
    return "\n".join([f"{len(problems)} {plural}:", *lines])


def _read(path: Path) -> str:
    """Read a plan file as UTF-8, or say why it could not be read.

    Both failure modes have to be named here, because they are not related by type.
    ``UnicodeDecodeError`` is a ``ValueError``, not an ``OSError``, so an ``except OSError`` around
    :meth:`~pathlib.Path.read_text` lets it straight through — and it is a realistic failure, not a
    theoretical one: this file invites hand-editing, it is written with ``allow_unicode`` so
    non-ASCII in a stage note is normal, and an editor saving it back as cp1252 with a pound sign
    or an o-slash in that note produces exactly this.
    """
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        msg = (
            f"could not read the plan file {path}: it is not valid UTF-8 (byte "
            f"0x{exc.object[exc.start]:02x} at position {exc.start}). Plan files are written as "
            f"UTF-8 and must be saved back as UTF-8 — an editor that saved this one as cp1252 or "
            f"latin-1 would do this. Re-save it as UTF-8, or restore it from version control."
        )
        raise PlanStoreError(msg) from exc
    except OSError as exc:
        msg = f"could not read the plan file {path}: {exc}"
        raise PlanStoreError(msg) from exc


# =============================================================================
# THE STORE
# =============================================================================


@dataclass(frozen=True, slots=True)
class PlanStore:
    """The versioned plan history for one workbook.

    Lives at ``<project>.kedge/plans`` next to the notebook, so the plan travels with the
    artifacts it describes.

    Example:
        >>> store = PlanStore(Path("rwa_monthly.kedge/plans"))
        >>> store.path_for(3).name
        'plan-v003.yaml'
    """

    directory: Path

    @classmethod
    def for_workspace(cls, workspace: Any) -> PlanStore:
        """Build a store from a :class:`kedge.workspace.Workspace`'s ``plans_dir``.

        Typed loosely on purpose: this module has no reason to import the workspace, and the
        only thing it needs from it is one path.
        """
        return cls(Path(workspace.plans_dir))

    # ── paths ────────────────────────────────────────────────────────────

    def path_for(self, version: int) -> Path:
        """The file a given version is written to, whether or not it exists."""
        return self.directory / f"plan-v{version:03d}.yaml"

    def versions(self) -> list[int]:
        """Every version present on disk, ascending."""
        if not self.directory.is_dir():
            return []
        found: list[int] = []
        for path in self.directory.iterdir():
            match = PLAN_FILENAME_PATTERN.match(path.name)
            if match:
                found.append(int(match.group(1)))
        return sorted(found)

    def next_version(self) -> int:
        """The version a newly proposed plan should carry."""
        existing = self.versions()
        return (existing[-1] + 1) if existing else 1

    # ── reading ──────────────────────────────────────────────────────────

    def load(self, version: int) -> ProcessPlan:
        """Load one version.

        Raises:
            PlanStoreError: when that version is not present, is not readable as UTF-8, or will
                not parse.
        """
        path = self.path_for(version)
        if not path.is_file():
            available = ", ".join(str(item) for item in self.versions()) or "none"
            msg = f"no plan version {version} in {self.directory} (available: {available})"
            raise PlanStoreError(msg)
        text = _read(path)
        try:
            return plan_from_yaml(text)
        except PlanStoreError as exc:
            msg = f"{path}: {exc}"
            raise PlanStoreError(msg) from exc

    def latest(self) -> ProcessPlan | None:
        """The highest version on disk, or None when the history is empty."""
        existing = self.versions()
        return self.load(existing[-1]) if existing else None

    def latest_approved(self) -> ProcessPlan | None:
        """The most recent approved version, or None.

        This is what the scaffolder is pointed at, and what a second run of the same monthly
        process re-uses with no model call at all (PLAN M2).
        """
        for version in reversed(self.versions()):
            plan = self.load(version)
            if plan.approval.approved:
                return plan
        return None

    def history(self) -> list[ProcessPlan]:
        """Every version, ascending. The change record."""
        return [self.load(version) for version in self.versions()]

    # ── writing ──────────────────────────────────────────────────────────

    def save(self, plan: ProcessPlan) -> Path:
        """Write a plan at its own version number.

        History is retained, not overwritten: saving over a version whose content differs is
        refused, with no way to ask otherwise. Re-saving byte-identical content is a no-op, so an
        idempotent pipeline does not need to check first.

        There was an ``overwrite`` escape hatch here, for recording an approval against a version
        already on disk. It looked reasonable — an approval is a decision *about* a version rather
        than a new one — and it destroyed the change record: approve, request changes, approve
        again left one line on disk and no trace that the first two decisions happened, including
        the one that silently un-approved a plan a notebook may already have been scaffolded from.
        A single approval slot cannot hold a history. Every decision goes to :meth:`save_next` now,
        and :func:`kedge.plan.review.render_diff` already renders two versions differing only in
        approval state.

        Args:
            plan: The plan to write, at ``plan.version``.

        Returns:
            The path written.

        Raises:
            PlanStoreError: when the version exists with different content, when the file already
                at that version cannot be read back to compare against, or when the write fails.
        """
        path = self.path_for(plan.version)
        text = plan_to_yaml(plan)
        if path.is_file():
            existing = _read(path)
            if existing == text:
                return path
            msg = (
                f"plan version {plan.version} already exists at {path} with different content; "
                f"plan history is retained rather than overwritten. Save at version "
                f"{self.next_version()} instead — including when what changed is only an "
                f"approval, because a version's decision record is the versions around it."
            )
            raise PlanStoreError(msg)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(path)
        except OSError as exc:
            msg = f"could not write the plan file {path}: {exc}"
            raise PlanStoreError(msg) from exc
        logger.info("saved plan v%d for %s to %s", plan.version, plan.workbook, path)
        return path

    def save_next(self, plan: ProcessPlan) -> tuple[ProcessPlan, Path]:
        """Write a plan at the next free version, renumbering it if necessary.

        The version a plan carries and the version it lands at can drift — a plan proposed
        against an empty store is v1, but by the time it is saved the user may have hand-written
        one. This resolves that without silently clobbering anything.

        Returns:
            The plan as written, and its path.
        """
        target = self.next_version()
        stamped = (
            plan
            if plan.version == target
            else plan.model_copy(update={"version": target, "based_on_version": plan.version})
        )
        return stamped, self.save(stamped)

    def seed(self) -> ProcessPlan | None:
        """The plan to offer the model as a worked example on the next proposal.

        The most recent approved version, falling back to the most recent of any state. Feeding
        a rejected draft back would re-seed the model with the decomposition the user turned
        down, so approval is preferred wherever one exists.
        """
        return self.latest_approved() or self.latest()
