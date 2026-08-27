"""Drive a runbook by what each step *is*, rather than by what somebody called it.

``harness/align.py`` maps a case's script onto a notebook's widgets, and it is exact about it:
an alias must be the name the scaffolder would itself have derived, ``f"{stem}_{key}"``. That
bridges a difference in *prefix* -- ``period_end`` reaching ``extract_query_period_end`` -- and
it is the right rule for what it does.

It cannot bridge a difference in the *name itself*, and that is the case a hub conversion
actually presents. Every widget the scaffolder writes is named after a stage id, and a stage id
is the model's free choice. The reference plan calls the checkpoint before the update
``approve_adjustment``; one real hub plan called it ``select_adjustment_population``. Those are
two names for one role, no spelling rule connects them, and the consequence was not a
mis-drive but a total one: **not one of the eight scripted actions named a widget the hub's
notebook had**, so the run stopped at the first thing waiting for a human and the entire
deterministic tier was reported as blocked. A conversion that could not be driven looked exactly
like a conversion that did not work.

So the script is keyed by role here, and both sides are resolved through the plan. A role is a
property of the *process* -- the first hand-in, the checkpoint that gates the update, the
confirmation that a statement was run -- and a runbook has those whatever its stages are called.

**The names still come from the scaffolder, never from a rule written here.** The plan is
scaffolded and the widgets are read off the cells it produces, tagged by the role the scaffolder
already assigns each cell. A second copy of the naming convention in this file would be a copy
that rots, and the convention is not this module's to know.

Two escape hatches, both narrow and both reported:

- **The head hand-in.** A plan whose load stage declares no hand-in of its own falls through to
  the notebook's fixed head one, so the first hand-in role resolves to ``handin_pick``. That is
  a real notebook a real user drives; refusing to drive it would report a defect that is already
  reported by ``takes_two_handins``.
- **The bare parameter.** The committed reference conversion is hand-written and says
  ``period_end`` where a scaffolded one says ``extract_query_period_end``. Leading stems are
  stripped one at a time until something matches, which is :class:`~harness.align.Widgets`'
  rule read in the other direction.

Anything that resolves to nothing is returned as unresolved rather than dropped. A scripted
action that quietly names nothing is the harness bug that makes every grader below it lie.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from harness.align import Widgets

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "Role",
    "Slot",
    "bind_by_role",
    "frame_aliases",
    "rename",
    "slots_for",
    "stage_roles",
]

_WIDGET = re.compile(r"^\s*([a-z_][a-z0-9_]*)\s*=\s*mo\.ui\.(\w+)", re.MULTILINE)
"""``name = mo.ui.kind(`` at the start of a line, read off *scaffolded* cell bodies only.

Not used on the notebook under test -- :class:`~harness.align.Widgets` reads that by AST,
because there the question is "is this really a widget" and a regex answering it wrongly is the
silent no-op that module exists to prevent. Here the input is code this process just generated,
and what is wanted from it is the kind as well as the name."""


class Role(StrEnum):
    """What a widget is *for*, independent of what its stage is called."""

    HANDIN = "handin"
    """A hand-in selector. Ordered: the first is the process's first input, and so on."""

    CHECKPOINT_DECISION = "checkpoint_decision"
    CHECKPOINT_NOTE = "checkpoint_note"
    HANDOFF_RAN = "handoff_ran"
    """The tick-box confirming a statement was run somewhere kedge cannot reach."""

    HANDOFF_RAN_NOTE = "handoff_ran_note"
    PARAMETER = "parameter"
    """An input to a hand-off's statement, keyed by the parameter's own name rather than by
    position -- a parameter is named after a column or a period in the workbook, which is far
    more stable across plans than a stage id."""


@dataclass(frozen=True, slots=True)
class Slot:
    """One thing the human does, as the scaffolder would name it."""

    role: Role
    name: str
    """The widget a scaffolded notebook of this plan defines for it."""

    stage_id: str
    parameter: str = ""
    ordinal: int = 0
    """Position among the slots of this role, in notebook order. Only read for hand-ins."""

    def describe(self) -> str:
        detail = f" ({self.parameter})" if self.parameter else ""
        return f"{self.role.value}{detail} on {self.stage_id!r}, scaffolded as {self.name!r}"


def slots_for(plan: Any) -> tuple[Slot, ...]:
    """Every widget a scaffolded notebook of this plan would put in front of the user.

    Read off :func:`~kedge.notebook.scaffold.build_cells`, tagged by the ``role`` the scaffolder
    already puts on each cell. A plan the scaffolder refuses yields nothing and says so: that
    plan has a finding of its own and it is not this function's to report.
    """
    from kedge.notebook.scaffold import build_cells

    try:
        cells = build_cells(plan, allow_unapproved=True)
    except Exception as error:
        logger.warning("the plan would not scaffold, so no roles could be read: %s", error)
        return ()

    slots: list[Slot] = []
    handins = 0
    for cell in cells:
        stage_id = cell.stage_id or ""
        for name, kind in _WIDGET.findall(cell.code):
            role, parameter = _classify(cell.role, name, kind)
            if role is None:
                continue
            ordinal = handins if role is Role.HANDIN else 0
            if role is Role.HANDIN:
                handins += 1
            slots.append(
                Slot(
                    role=role,
                    name=name,
                    stage_id=stage_id,
                    parameter=parameter,
                    ordinal=ordinal,
                )
            )
    return tuple(slots)


def _classify(cell_role: str, name: str, kind: str) -> tuple[Role | None, str]:
    """Which role a widget plays, from the cell's role and the widget's kind and suffix.

    The suffixes are the scaffolder's, and they are checked *with* the kind rather than instead
    of it: ``_note`` is a text area on a checkpoint and also on a confirmation, and reading the
    first as the second would approve a step by writing a note into it.
    """
    if name == "kedge_run_mode":
        return None, ""
    if cell_role == "handin":
        return (Role.HANDIN, "") if kind == "file_browser" else (None, "")
    if cell_role == "checkpoint":
        if kind == "dropdown" and name.endswith("_decision"):
            return Role.CHECKPOINT_DECISION, ""
        if name.endswith("_note"):
            return Role.CHECKPOINT_NOTE, ""
        return None, ""
    if cell_role == "handoff":
        if kind == "checkbox" and name.endswith("_ran"):
            return Role.HANDOFF_RAN, ""
        if name.endswith("_ran_note"):
            return Role.HANDOFF_RAN_NOTE, ""
        # Everything else a hand-off cell offers is an input to its statement. The parameter's
        # own name is what a case keys on, and it is the widget name less the stage's stem.
        return Role.PARAMETER, name
    return None, ""


def bind_by_role(
    plan: Any,
    notebook: Path,
    values: Mapping[Any, Any],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Re-key a role-indexed script onto the widgets this notebook actually defines.

    Args:
        plan: The plan the notebook was scaffolded from. Roles are read from it.
        notebook: The notebook to drive.
        values: What the human does, keyed by :class:`Role` -- or by a parameter's name, as a
            plain string, for :attr:`Role.PARAMETER`. A :attr:`Role.HANDIN` entry is a sequence
            applied to the hand-in slots in order; every other role's value is applied to every
            slot of that role, because a runbook's checkpoints all have to be approved for the
            run to reach the end.

    Returns:
        ``(inputs keyed by this notebook's widget names, descriptions of what could not be
        resolved)``.
    """
    widgets = Widgets.read(notebook)
    defined = widgets.names
    slots = slots_for(plan)

    inputs: dict[str, Any] = {}
    unresolved: list[str] = []
    files = list(values.get(Role.HANDIN, ()) or ())
    head_taken = False

    for slot in slots:
        if slot.role is Role.HANDIN:
            if slot.ordinal >= len(files):
                continue
            value: Any = files[slot.ordinal]
        elif slot.role is Role.PARAMETER:
            key = _parameter_key(slot, values)
            if key is None:
                continue
            value = values[key]
        else:
            if slot.role not in values:
                continue
            value = values[slot.role]

        name, head_taken = _resolve(slot, defined, head_taken=head_taken)
        if name is None:
            unresolved.append(slot.describe())
            continue
        if name in inputs:
            # Two slots on one widget drives one step with another step's value. Both are
            # reported instead, which is `Alignment.bind`'s rule and it is right for the same
            # reason: a loud wrong answer beats a quiet one.
            unresolved.append(f"{slot.describe()} -- collides with a slot already bound there")
            continue
        inputs[name] = value

    return inputs, tuple(unresolved)


def _parameter_key(slot: Slot, values: Mapping[Any, Any]) -> str | None:
    """The key in ``values`` this parameter slot answers to, or ``None``.

    Matched on the parameter's own name with the stage's stem stripped, so a case says
    ``"period_end"`` and reaches ``extract_query_period_end`` on one plan and
    ``render_update_statement_period_end`` on another.
    """
    for candidate in _stems_stripped(slot.parameter):
        if candidate in values:
            return candidate
    return None


def _resolve(slot: Slot, defined: frozenset[str], *, head_taken: bool) -> tuple[str | None, bool]:
    """This slot's widget in the notebook under test, and whether the head hand-in is now used."""
    if slot.name in defined:
        return slot.name, head_taken
    for candidate in _stems_stripped(slot.name):
        if candidate in defined:
            return candidate, head_taken
    if slot.role is Role.HANDIN and not head_taken and "handin_pick" in defined:
        # The plan's load stage declared no hand-in of its own, so the scaffolder wired it to the
        # notebook's fixed head one. Only the first such stage gets it: there is exactly one head
        # hand-in, and a second input genuinely has nowhere to arrive.
        return "handin_pick", True
    return None, head_taken


def _stems_stripped(name: str) -> Sequence[str]:
    """``a_b_c`` -> ``b_c``, ``c``. The leading stems removed one at a time."""
    parts = name.split("_")
    return ["_".join(parts[index:]) for index in range(1, len(parts))]


# =============================================================================
# STAGES BY ROLE
# =============================================================================
#
# Driving a notebook by role got the run to the end. Reading its *results* is the other half of
# the same problem: a grader asks for `ctx.defs["adjust"]`, and `adjust` is the reference plan's
# stage id. A conversion that calls the same step `calculate_uplift` defines no such name, so the
# grader skipped -- and a skip leaves the denominator, so the score went *up* as the naming
# diverged. Measured: driving alone took a hub scaffold from 5/71 to 9/53, and the shrinking
# denominator was doing as much of that work as the driving.
#
# Every rule below is a property of the plan and the workbook, never a guess about wording. The
# operation ids a stage claims come from the analyser, not from the model, which is what makes
# "the stage that computes the uplift" a fact rather than an interpretation.


def stage_roles(plan: Any) -> dict[str, str]:
    """The stage id playing each role in this plan, for the roles that can be decided.

    Absent keys are the point as much as present ones: a plan with no hand-off has no
    ``writing_handoff``, so a grader asking about the update statement finds nothing and reports
    that -- rather than being quietly pointed at some other stage that happened to be nearby.
    """
    roles: dict[str, str] = {}
    ordered = list(plan.ordered_stages())
    if not ordered:
        return roles

    handoffs = [stage for stage in ordered if stage.is_handoff]
    if handoffs:
        roles["first_handoff"] = handoffs[0].id
    writing = [stage for stage in handoffs if _writes(stage)]
    if writing:
        roles["writing_handoff"] = writing[0].id

    # The stage that does the arithmetic, found by the analysis operations it claims. Those ids
    # are the analyser's -- `adjustment_e17_e92` is a range in the workbook -- so this is the one
    # role that cannot be moved by renaming anything.
    computing = max(
        (stage for stage in ordered if stage.operations),
        key=lambda stage: len(stage.operations),
        default=None,
    )
    if computing is not None:
        roles["computing"] = computing.id

    # The notebook's fixed head hand-in belongs to no stage, so its slot carries no stage id.
    # Left in, it made ``first_load`` an empty string and put ``pre_adjustment -> ''`` in the
    # alias map, which is a rename to nothing.
    loads = [
        slot.stage_id for slot in slots_for(plan) if slot.role is Role.HANDIN and slot.stage_id
    ]
    if loads:
        roles["first_load"] = loads[0]
    if len(loads) > 1:
        roles["second_load"] = loads[1]
        verifying = [
            stage
            for stage in ordered
            if loads[1] in stage.depends_on and stage.id != roles.get("final")
        ]
        if verifying:
            roles["verifying"] = verifying[0].id

    roles["final"] = ordered[-1].id
    return roles


def _writes(stage: Any) -> bool:
    """Whether this hand-off's statement changes data, judged on the statement itself.

    `kedge.sql.changes_data` rather than the plan's `mutates` flag, for the reason CLAUDE.md
    already gives: `mutates` is a claim and the statement is the fact.
    """
    from kedge.sql import changes_data

    handoff = stage.effective_handoff()
    text = handoff.statement or handoff.template or ""
    try:
        return bool(text) and changes_data(text)
    except Exception:
        return False


def frame_aliases(reference: Any, plan: Any) -> dict[str, str]:
    """Map the names a case's graders ask for onto the equivalents in ``plan``.

    Built by playing the same role lookup over both plans, so the case goes on naming what it
    wants in its own terms and the mapping is derived rather than declared. Over the reference
    plan against itself this is the identity, which is the assertion that keeps it honest --
    :func:`test_the_reference_plan_aliases_to_itself` -- because a mapping that quietly renamed
    something on the reference would be scoring the gold conversion through a translation layer.

    A role the *reference* does not fill contributes nothing, and neither does one the other plan
    does not: the grader then finds the name absent, which is the true answer.
    """
    mine, theirs = stage_roles(reference), stage_roles(plan)
    return {
        mine[role]: theirs[role]
        for role in mine
        if mine.get(role) and theirs.get(role) and mine[role] != theirs[role]
    }


def rename(name: str, aliases: Mapping[str, str]) -> str:
    """A name in the rubric's vocabulary, said in the plan's.

    Whole leading segments only. ``adjust -> calculate_uplift`` must turn ``adjust_totals`` into
    ``calculate_uplift_totals`` and leave ``adjustment_note`` completely alone -- a substring
    rule would make that ``calculate_upliftment_note``, which is a name nothing defines and a
    grader that skips for a reason nobody could work out.
    """
    if name in aliases:
        return aliases[name]
    for wanted, actual in aliases.items():
        if name.startswith(f"{wanted}_"):
            return f"{actual}_{name[len(wanted) + 1 :]}"
    return name
