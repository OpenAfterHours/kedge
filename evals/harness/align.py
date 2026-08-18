"""Map a case's script of human actions onto the widgets a scaffolded notebook actually has.

``harness/drive.py`` binds a scripted value by variable name, and ``evals/README.md`` already
records the limitation that falls out of it: a conversion that names its widgets differently
cannot be driven. A scaffolded notebook always does. The scaffolder derives every widget name
from the plan's stage ids -- ``extract_query_period_end``, not ``period_end`` -- so a script
written against a hand-authored reference names nothing at all.

Resolving that is legitimate: the names are *kedge's*, not the model's, so a model measured
through them is measured fairly. Resolving it loosely is not, and a review found this module's
predecessor doing exactly that. It matched any defined name ending ``_<key>``, which bound
``query`` to ``extract_query`` -- a string. ``drive._apply`` binds only onto a
:class:`~harness.drive.Widget`, so the value went nowhere; ``unused_inputs`` had nothing to
report because the name existed; and the run was recorded as faithfully driven with the scripted
action discarded. A rule that guesses in the direction of flattery is worse than no rule.

Two guards, and both are exact rather than approximate. A candidate must be assigned from
``mo.ui.*`` -- checked on the AST of the cell that defines it, not by spelling -- and an alias
must be the name the scaffolder would itself have derived, ``f"{stem}_{key}"`` for a stem of that
cell. Anything else is reported unplayed, which is a loud wrong answer instead of a quiet one.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kedge.notebook.codegen import analyse_cells
from kedge.notebook.scaffold import HEAD_CELL_NAMES

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "Alignment",
    "HeadFeed",
    "Widgets",
    "align_inputs",
]

_HANDIN_SUFFIXES = ("drop", "pick", "paste")
"""The three ways a hand-in arrives. Named here because the fixed head has one of each and so
does every stage that declares a hand-in of its own -- which is how the duplicate is spotted."""

_WIDGET_CELL_SUFFIXES = ("_inputs", "_input", "_source", "_ui")
"""What the scaffolder appends to a stage's cell name when it emits that stage's widgets.

The alias rule in :meth:`Widgets.resolve` is exact, and these are the only stems it will accept
beyond the defining cell's own name. A scaffolder that invents a fourth suffix therefore stops
resolving aliases and the scripted actions are reported as unplayed -- loudly wrong rather than
quietly bound to whatever happened to end in the right letters.
"""


def _is_widget_call(node: ast.expr | None) -> bool:
    """Whether an expression is a call to ``mo.ui.<something>``."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "ui"
        and isinstance(func.value.value, ast.Name)
        and func.value.value.id == "mo"
    )


def _widget_names(code: str) -> tuple[str, ...]:
    """The names one cell binds to an ``mo.ui`` element, by AST rather than by spelling."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ()
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_widget_call(node.value):
            found.extend(target.id for target in node.targets if isinstance(target, ast.Name))
        elif (
            isinstance(node, ast.AnnAssign)
            and _is_widget_call(node.value)
            and isinstance(node.target, ast.Name)
        ):
            found.append(node.target.id)
    return tuple(found)


def _stems_for(cell_name: str) -> tuple[str, ...]:
    """The names an alias in this cell may be derived from: the cell, and its stage."""
    for suffix in _WIDGET_CELL_SUFFIXES:
        if cell_name.endswith(suffix) and len(cell_name) > len(suffix):
            return (cell_name, cell_name[: -len(suffix)])
    return (cell_name,)


@dataclass(frozen=True, slots=True)
class Widgets:
    """The ``mo.ui`` elements a rendered notebook defines, and what each may be aliased from.

    Built by AST rather than by name, and this is the whole discrimination. ``harness/drive.py``
    binds a scripted value only onto a :class:`~harness.drive.Widget`; anything else is a silent
    no-op. So an alias rule that matched any defined name ending ``_<key>`` bound ``query`` to
    ``extract_query`` -- a *string* -- and the run then reported as faithfully driven with the
    scripted action discarded, because the name existed and
    :func:`~harness.drive.unused_inputs` had nothing to complain about. It did the same for
    ``statement -> update_statement``, ``check -> handin_check`` and
    ``values -> reconciliation_values``. Every one of those is a finding turned into a no-op.

    Example:
        >>> widgets = Widgets(stems=(("extract_query_period_end", ("extract_query",)),))
        >>> widgets.resolve("period_end")[0]
        'extract_query_period_end'
    """

    stems: tuple[tuple[str, tuple[str, ...]], ...] = ()
    """Each widget, with the stems an alias for it may be built from. A tuple of pairs rather
    than a mapping so the dataclass can be frozen with slots and hold nothing mutable."""

    defined: frozenset[str] = frozenset()
    """Every public name the notebook defines, widget or not. Only used to tell "there is no
    such name" from "there is, and binding to it would do nothing"."""

    order: tuple[str, ...] = ()
    """Widget-defining cell names in file order, for choosing the head hand-in's donor."""

    @classmethod
    def read(cls, notebook: Path) -> Widgets:
        """Read a rendered notebook and find every widget it defines."""
        from kedge.notebook.codegen import read_notebook

        document = read_notebook(notebook)
        codes = [cell.code for cell in document.cells]
        analyses = analyse_cells(codes)
        stems: list[tuple[str, tuple[str, ...]]] = []
        order: list[str] = []
        for cell, code in zip(document.cells, codes, strict=True):
            names = _widget_names(code)
            if not names:
                continue
            order.append(cell.name or "")
            stems.extend((name, _stems_for(cell.name or "")) for name in names)
        return cls(
            stems=tuple(stems),
            defined=frozenset(name for analysis in analyses for name in analysis.defs),
            order=tuple(order),
        )

    @property
    def names(self) -> frozenset[str]:
        return frozenset(name for name, _ in self.stems)

    def resolve(self, key: str) -> tuple[str | None, str]:
        """The widget a scripted action should bind to, or nothing and why not.

        Three rules, in order, and none of them guesses:

        1. **Exact match.** The notebook has a widget of that name.
        2. **The scaffolder's own derivation.** Exactly one widget is named
           ``f"{stem}_{key}"`` for a stem of the cell that defines it -- so ``period_end``
           resolves to ``extract_query_period_end`` because the cell ``extract_query_inputs``
           belongs to the stage ``extract_query``, and to nothing else. Two candidates is
           ambiguous and is left unmatched rather than picked.
        3. **Nothing.** The reason says which kind of nothing, because the case that stings is
           the one where the notebook *does* have a name of about the right shape and it is not
           a widget: binding to that is a scripted action discarded without a word.

        Returns:
            ``(widget name, "")`` or ``(None, why not)``.
        """
        widgets = self.names
        if key in widgets:
            return key, ""
        candidates = sorted(
            {name for name, stems in self.stems if any(f"{stem}_{key}" == name for stem in stems)}
        )
        if len(candidates) == 1:
            return candidates[0], ""
        if len(candidates) > 1:
            return None, f"{len(candidates)} widgets could be meant: {', '.join(candidates)}"
        near = sorted(
            name
            for name in self.defined
            if name not in widgets and (name == key or name.endswith(f"_{key}"))
        )
        if near:
            return (
                None,
                f"the notebook defines {', '.join(near)}, and not as an mo.ui element -- "
                f"binding to one would be discarded without a word",
            )
        return None, "the notebook has no widget for it"


@dataclass(frozen=True, slots=True)
class HeadFeed:
    """The fixed head hand-in, and the stage hand-in the harness fed it from.

    Every scaffolded notebook opens with the hand-in cells of
    :data:`~kedge.notebook.scaffold.HEAD_CELL_NAMES`, whose ``mo.stop`` halts the whole run until
    something is supplied -- *even when every hand-in in the plan is declared on a stage*, which
    for a runbook it is. So the head asks for a file no step of the process names, and nothing
    below it runs until somebody supplies one.

    That is a defect in the scaffolder, and it is reported as one (see
    :func:`_scaffold_defects`). It is also fed, from the process's own first hand-in, because the
    alternative is a run that stops in the third cell and an eval that measures nothing about any
    model ever again. The donor is the *first stage hand-in in file order*, never "whichever key
    happened to come first in the script", which is what made this dependent on the insertion
    order of a literal dict in ``case.py``.
    """

    donor: str
    """The stage whose hand-in was copied into the head."""

    pairs: tuple[tuple[str, str], ...] = ()
    """``(head widget, donor widget)`` for each of drop, select and paste."""


@dataclass(frozen=True, slots=True)
class Alignment:
    """How a case's script of human actions maps onto the widgets a scaffolded notebook has.

    ``harness/drive.py`` binds values by variable name, and ``evals/README.md`` already records
    the limitation that falls out of it: a conversion that names its widgets differently cannot
    be driven. A scaffolded notebook always does. The scaffolder derives every widget name from
    the plan's stage ids -- ``extract_query_period_end``, not ``period_end`` -- so a script
    written against a hand-authored reference names nothing.

    That is a naming difference, not a defect, and it is *the scaffolder's* naming rather than
    the model's, so resolving it measures the model fairly. Resolving it silently would not:
    every alias used, every scripted action that found no widget, and the head hand-in the
    harness had to feed are all carried here, printed in the report's notes, and -- where they
    are findings about kedge rather than about naming -- raised as a :class:`Defect`.
    """

    widgets: Widgets
    exact: tuple[str, ...] = ()
    """Script keys the notebook already uses that name for."""

    aliases: tuple[tuple[str, str], ...] = ()
    """``(script key, widget)`` for the keys that had to be resolved to a different name."""

    unmatched: tuple[tuple[str, str], ...] = ()
    """``(script key, why not)``. A step the notebook does not have, which is a finding."""

    head: HeadFeed | None = None

    @property
    def mapping(self) -> dict[str, str]:
        """Script key to notebook name, for the keys that resolved."""
        return {key: key for key in self.exact} | dict(self.aliases)

    @property
    def unplayed(self) -> tuple[str, ...]:
        """Just the keys, for a caller that only wants to know what did not happen."""
        return tuple(key for key, _ in self.unmatched)

    def bind(self, script: Mapping[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
        """Re-key a script of human actions onto this notebook's names.

        Resolves every key against the notebook rather than against :attr:`mapping`, because a
        case's graders build scripts of their own -- a paste instead of a file, a later period, a
        request to start a new run -- and those keys were never in the set :func:`align_inputs`
        was asked about. Aligning only what was declared is how five of this rubric's items came
        to be driven against a notebook that had stopped in its third cell.

        Two keys resolving to one widget bind nothing at all. Whichever the dict happened to
        yield last would otherwise win, silently, and one step of the process would be driven
        with another step's value -- so both are reported unplayed instead, which is a loud
        wrong answer rather than a quiet one.

        Returns:
            ``(inputs, keys that were not played)``.
        """
        claimed: dict[str, list[str]] = {}
        unplayed: list[str] = []
        for key in script:
            name, _why = self.widgets.resolve(key)
            if name is None:
                unplayed.append(key)
                continue
            claimed.setdefault(name, []).append(key)

        inputs: dict[str, Any] = {}
        for name, keys in claimed.items():
            if len(keys) > 1:
                logger.warning("scripted actions %s all resolve to %s; none was played", keys, name)
                unplayed.extend(keys)
                continue
            inputs[name] = script[keys[0]]
        if self.head is not None:
            for widget, donor in self.head.pairs:
                if donor in inputs:
                    inputs[widget] = inputs[donor]
        return inputs, tuple(sorted(unplayed))

    def apply(self, script: Mapping[str, Any]) -> dict[str, Any]:
        """:meth:`bind`, for a caller that only wants the inputs."""
        return self.bind(script)[0]

    def notes(self) -> tuple[str, ...]:
        """What a reader of the report has to be told about how the notebook was driven."""
        lines: list[str] = []
        if self.aliases:
            rendered = ", ".join(f"{key} -> {name}" for key, name in self.aliases)
            lines.append(f"scripted actions re-keyed to the scaffolder's widget names: {rendered}")
        if self.head is not None:
            lines.append(
                f"the fixed head hand-in was fed from the {self.head.donor!r} stage's own "
                f"hand-in: "
                + ", ".join(f"{widget} <- {donor}" for widget, donor in self.head.pairs)
            )
        if self.unmatched:
            lines.append(
                "scripted actions the notebook has no step for, so they were not played: "
                + ", ".join(f"{key} ({why})" for key, why in self.unmatched)
            )
        return tuple(lines)


def align_inputs(notebook: Path, keys: Iterable[str]) -> Alignment:
    """Work out which of a notebook's widgets each scripted action should bind to.

    Args:
        notebook: The rendered notebook.
        keys: The script's keys -- the names a case uses for the human's actions.

    Returns:
        The :class:`Alignment`. Nothing is bound that the notebook does not define as a widget,
        so a scripted action with no step is reported rather than discarded.
    """
    widgets = Widgets.read(notebook)

    exact: list[str] = []
    aliases: list[tuple[str, str]] = []
    unmatched: list[tuple[str, str]] = []
    for key in keys:
        name, why = widgets.resolve(key)
        if name is None:
            unmatched.append((key, why))
        elif name == key:
            exact.append(key)
        else:
            aliases.append((key, name))

    return Alignment(
        widgets=widgets,
        exact=tuple(exact),
        aliases=tuple(aliases),
        unmatched=tuple(unmatched),
        head=_head_feed(widgets),
    )


def _head_feed(widgets: Widgets) -> HeadFeed | None:
    """Which stage hand-in to copy into the fixed head's, or ``None`` if there is nothing to copy.

    The donor is the first hand-in *cell* in file order that is not part of the fixed head, which
    makes this a property of the notebook rather than of the order somebody wrote a dict literal
    in. It matters: the old rule took the first script key ending ``_pick``, so reordering
    ``script_for``'s literal would have fed the post-adjustment grid into the head hand-in while
    the report's notes went on saying it was the pre-adjustment one.
    """
    names = widgets.names
    head_widgets = tuple(
        f"handin_{suffix}" for suffix in _HANDIN_SUFFIXES if f"handin_{suffix}" in names
    )
    if not head_widgets:
        return None
    donor = next(
        (
            cell
            for cell in widgets.order
            if cell not in HEAD_CELL_NAMES
            and any(f"{stem}_pick" in names for stem in _stems_for(cell))
        ),
        None,
    )
    if donor is None:
        return None
    stem = _stems_for(donor)[-1]
    pairs = tuple(
        (f"handin_{suffix}", f"{stem}_{suffix}")
        for suffix in _HANDIN_SUFFIXES
        if f"handin_{suffix}" in names and f"{stem}_{suffix}" in names
    )
    if not pairs:
        return None
    return HeadFeed(donor=stem, pairs=pairs)
