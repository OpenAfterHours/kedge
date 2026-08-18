"""Run a runbook notebook headlessly, with the human steps supplied from a script.

A converted workbook is a *runbook*: it hands the user a query, waits, takes the result back,
computes, asks for a decision, hands over a second statement, waits again, and only then says
whether the change did what it said it would. Grading one means playing the human part -- and
playing it the same way every time.

**Why not ``App.run()``.** :func:`kedge.reconcile.compare.notebook_definitions` executes a
notebook through marimo's own public entry point, and for an ordinary notebook that is the right
tool. It cannot drive this one. A runbook's ``mo.ui`` elements come up empty on a fresh run and
its ``mo.stop`` guards halt immediately, by design: nothing downstream of "waiting for a hand-in"
should compute anything. ``App.run()`` offers no way in.

**What this does instead** is what ``tests/unit/test_scaffold.py`` already does for the notebook
head, generalised to a whole file: read the ``@app.cell`` blocks with
:func:`kedge.notebook.codegen.read_notebook`, then execute their bodies in order against a
namespace holding a stand-in ``mo``. No kernel, no server, no private API, and no marimo import
at all.

Two things make it faithful rather than merely convenient:

* **Values are bound after the cell that defines the widget, never before.** marimo's
  single-definition rule means a cell cannot both create a UI element and read it, so a widget is
  always defined in one cell and read in a later one. Binding between the two is exactly where
  the user's interaction happens, and doing it by variable name means the script says
  ``{"handin_pick": (path,)}`` -- the same name the notebook uses.
* **A ``mo.stop`` ends the run and is reported.** In marimo it halts the cell and its
  descendants; here execution is linear, so anything downstream would fail on a missing name
  anyway. A run that stopped is a graded result with a reason attached, not an error.

**What it is not.** It is not marimo. Cells run top to bottom in file order rather than in
dataflow order, nothing re-runs on a value changing, and no output is rendered. For grading a
notebook whose cells were emitted in dependency order that is enough, and the alternative --
reimplementing reactivity -- would be grading a copy of marimo rather than the notebook.
"""

from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kedge.notebook.codegen import read_notebook

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

logger = logging.getLogger(__name__)

__all__ = [
    "FakeMarimo",
    "NotebookRun",
    "Stopped",
    "UnsupportedMarimoCallError",
    "Widget",
    "run_notebook",
    "visible_cells",
]


class Stopped(Exception):  # noqa: N818 - it is a control signal, not a failure
    """A cell called ``mo.stop`` with a truthy condition.

    Not an error. It is the notebook saying it is waiting for something, which for a runbook is
    the normal state of every cell below the step the user has reached.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UnsupportedMarimoCallError(Exception):
    """The notebook used part of marimo's surface the stand-in does not implement.

    Raised rather than silently returning a no-op. A harness that quietly absorbed an unknown
    ``mo.something`` would grade a notebook that never really ran, and report a score for it.
    """


@dataclass
class Widget:
    """A stand-in for one ``mo.ui`` element. The only thing any cell touches is ``.value``."""

    kind: str
    value: Any = None
    label: str = ""
    options: Any = None

    def __repr__(self) -> str:
        return f"Widget({self.kind}, value={self.value!r})"


class _Panels:
    """Everything the notebook rendered, in order, as plain text.

    Grading reads these: a runbook's output is prose as much as it is numbers, and "did the
    verification panel say it failed" is a question about what the user was shown.
    """

    def __init__(self) -> None:
        self.rendered: list[str] = []

    def record(self, text: str) -> str:
        self.rendered.append(text)
        return text


class _FakeUi:
    """``mo.ui``. Every constructor returns a :class:`Widget` the script can fill in."""

    def __init__(self, panels: _Panels) -> None:
        self._panels = panels

    def file(self, **kwargs: Any) -> Widget:
        return Widget("file", value=(), label=str(kwargs.get("label", "")))

    def file_browser(self, **kwargs: Any) -> Widget:
        return Widget("file_browser", value=(), label=str(kwargs.get("label", "")))

    def text_area(self, **kwargs: Any) -> Widget:
        return Widget("text_area", value="", label=str(kwargs.get("label", "")))

    def text(self, **kwargs: Any) -> Widget:
        return Widget(
            "text", value=str(kwargs.get("value", "")), label=str(kwargs.get("label", ""))
        )

    def number(self, **kwargs: Any) -> Widget:
        return Widget("number", value=kwargs.get("value"), label=str(kwargs.get("label", "")))

    def checkbox(self, **kwargs: Any) -> Widget:
        return Widget("checkbox", value=bool(kwargs.get("value", False)))

    def dropdown(self, **kwargs: Any) -> Widget:
        return Widget(
            "dropdown",
            value=kwargs.get("value"),
            label=str(kwargs.get("label", "")),
            options=kwargs.get("options"),
        )

    def date(self, **kwargs: Any) -> Widget:
        return Widget("date", value=kwargs.get("value"), label=str(kwargs.get("label", "")))

    def tabs(self, tabs: Any = None, **_: Any) -> Widget:
        return Widget("tabs", value=tabs)

    def table(self, data: Any = None, **_: Any) -> Widget:
        return Widget("table", value=data)

    def __getattr__(self, name: str) -> Any:
        msg = (
            f"the notebook called mo.ui.{name}(), which this harness does not stand in for. "
            f"Add it to evals/harness/drive.py:_FakeUi rather than letting the run continue "
            f"against a widget that does nothing."
        )
        raise UnsupportedMarimoCallError(msg)


class FakeMarimo:
    """``mo``, reduced to what a scaffolded notebook actually calls.

    Anything outside that set raises :class:`UnsupportedMarimoCallError` naming itself, because the
    failure mode to avoid is a run that looks complete and skipped half the notebook.
    """

    #: `import marimo as mo` in the setup cell looks this up in `sys.modules`, so the stand-in
    #: is installed there for the run -- see `_marimo_stand_in`. Python does not require the
    #: object it finds to be a module, but anything introspecting one expects these.
    __name__ = "marimo"
    __spec__ = None

    def __init__(self) -> None:
        self._panels = _Panels()
        self.ui = _FakeUi(self._panels)

    @property
    def rendered(self) -> list[str]:
        return self._panels.rendered

    def md(self, text: str = "") -> str:
        return self._panels.record(str(text))

    def plain(self, value: Any = None) -> Any:
        return value

    def vstack(self, items: Any = (), **_: Any) -> Any:
        return self._stack(items)

    def hstack(self, items: Any = (), **_: Any) -> Any:
        return self._stack(items)

    def callout(self, value: Any = None, **_: Any) -> Any:
        return value

    def accordion(self, items: Any = None, **_: Any) -> Any:
        return items

    def stop(self, predicate: Any, output: Any = None) -> None:
        """Halt the run when the condition holds, carrying whatever the cell was going to show."""
        if predicate:
            raise Stopped(str(output) if output is not None else "")

    def _stack(self, items: Any) -> Any:
        # The items have usually been rendered already -- mo.md returns its own text and records
        # it. Stacking is a layout instruction, so there is nothing further to capture.
        return list(items) if isinstance(items, list | tuple) else items

    def __getattr__(self, name: str) -> Any:
        msg = (
            f"the notebook called mo.{name}(), which this harness does not stand in for. Add it "
            f"to evals/harness/drive.py:FakeMarimo rather than letting the run continue."
        )
        raise UnsupportedMarimoCallError(msg)


@contextmanager
def _marimo_stand_in(fake: FakeMarimo) -> Iterator[None]:
    """Make ``import marimo`` resolve to the stand-in for the duration of the run.

    Without this the notebook's own setup cell undoes the whole harness: ``import marimo as mo``
    rebinds ``mo`` to the real package, whose ``mo.stop`` raises ``MarimoStopError`` and whose
    ``mo.ui`` elements have values no script can set. The first hand-in cell then halts and the
    run reports the notebook as waiting when it was never given a chance to be driven.

    Scoped and restored, because the real marimo is a legitimate import elsewhere in the process
    -- ``kedge.reconcile.cell`` reaches for it -- and leaving a stand-in in ``sys.modules`` would
    turn a graded run into a booby trap for whatever ran next.
    """
    saved = sys.modules.get("marimo")
    sys.modules["marimo"] = fake  # type: ignore[assignment]
    try:
        yield
    finally:
        if saved is None:
            sys.modules.pop("marimo", None)
        else:
            sys.modules["marimo"] = saved


@dataclass(frozen=True)
class NotebookRun:
    """What happened when the notebook was driven to the end, or to where it stopped."""

    path: Path
    definitions: dict[str, Any] = field(default_factory=dict)
    panels: tuple[str, ...] = ()
    cells_run: tuple[str, ...] = ()
    stopped_at: str | None = None
    stopped_because: str | None = None
    failed_at: str | None = None
    error: BaseException | None = None

    @property
    def completed(self) -> bool:
        """Whether every cell ran without stopping or raising."""
        return self.stopped_at is None and self.failed_at is None

    def summary_line(self) -> str:
        if self.failed_at is not None:
            return f"failed in cell {self.failed_at!r}: {type(self.error).__name__}: {self.error}"
        if self.stopped_at is not None:
            return f"stopped in cell {self.stopped_at!r}: {self.stopped_because}"
        return f"ran {len(self.cells_run)} cell(s) to completion"

    def panel_containing(self, fragment: str) -> str | None:
        """The first rendered panel holding this text, or ``None``."""
        return next((panel for panel in self.panels if fragment in panel), None)


def run_notebook(
    path: Path,
    *,
    inputs: Mapping[str, Any] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> NotebookRun:
    """Execute a notebook's cells in file order, playing the human part from a script.

    Args:
        path: The notebook. Either a marimo file (``@app.cell`` blocks) or a plain module, which
            is executed as a single cell.
        inputs: Values to bind onto ``mo.ui`` widgets, keyed by the variable each is assigned to.
            Applied after every cell, so a widget defined in one cell is filled before the cell
            that reads it runs -- which is where the user's interaction sits.
        overrides: Plain names to replace after every cell. This is how a harness points a
            notebook's ``HANDIN_DIR`` at a temporary store rather than the user's, and it is
            deliberately blunt: what it does is stated in the report rather than hidden in a
            conditional inside the notebook.

    Returns:
        The :class:`NotebookRun`. A stop or an exception is recorded rather than raised -- both
        are results a grader has something to say about.
    """
    document = read_notebook(path)
    cells = [(cell.name or f"cell_{index}", cell.code) for index, cell in enumerate(document.cells)]
    if not cells:
        # No @app.cell blocks: a plain module, which is what a hand-written translation looks
        # like. Run it whole. `notebook_definitions` makes the same allowance.
        cells = [("<module>", path.read_text(encoding="utf-8"))]

    marimo = FakeMarimo()
    namespace: dict[str, Any] = {
        "__name__": "__kedge_eval__",
        "__file__": str(path),
        "mo": marimo,
    }
    _apply(namespace, inputs or {}, overrides or {})

    ran: list[str] = []
    for name, code in cells:
        try:
            with _marimo_stand_in(marimo):
                exec(compile(code, f"<{name}>", "exec"), namespace)
        except Stopped as stop:
            logger.info("notebook %s stopped in %s: %s", path.name, name, stop.message)
            return NotebookRun(
                path=path,
                definitions=_public(namespace),
                panels=tuple(marimo.rendered),
                cells_run=tuple(ran),
                stopped_at=name,
                stopped_because=stop.message,
            )
        except BaseException as error:
            logger.info("notebook %s failed in %s: %r", path.name, name, error)
            return NotebookRun(
                path=path,
                definitions=_public(namespace),
                panels=tuple(marimo.rendered),
                cells_run=tuple(ran),
                failed_at=name,
                error=error,
            )
        ran.append(name)
        _apply(namespace, inputs or {}, overrides or {})

    return NotebookRun(
        path=path,
        definitions=_public(namespace),
        panels=tuple(marimo.rendered),
        cells_run=tuple(ran),
    )


def visible_cells(
    path: Path,
    *,
    inputs: Mapping[str, Any] | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Which cells would render, given these inputs -- and, by omission, which would not.

    The question :func:`run_notebook` cannot answer. It executes linearly and aborts at the first
    stop, so it can say *where* a runbook halted but not *what a user would see*. That blind spot
    let a real defect through the whole eval: the post-adjustment selector constructed widgets and
    read nothing, so marimo had no edge to gate it on and it rendered from the moment the notebook
    opened -- inviting a re-extract taken before the update had been run. Every grader passed.

    This models what marimo does instead of aborting: a cell that stops is skipped, a cell whose
    references are not defined is skipped, and everything else carries on. Skipping propagates
    exactly as dataflow does, because a cell downstream of a skipped one raises ``NameError`` on
    the name that was never bound.

    It is an approximation -- cells run in file order rather than topological order -- and for a
    notebook whose cells were emitted in dependency order the two agree. What it is not is a
    guess: a cell listed here really did execute against these inputs.

    Returns:
        The names of the cells that ran to completion, in file order.
    """
    document = read_notebook(path)
    cells = [(cell.name or f"cell_{index}", cell.code) for index, cell in enumerate(document.cells)]
    marimo = FakeMarimo()
    namespace: dict[str, Any] = {
        "__name__": "__kedge_eval__",
        "__file__": str(path),
        "mo": marimo,
    }
    _apply(namespace, inputs or {}, overrides or {})

    rendered: list[str] = []
    for name, code in cells:
        try:
            with _marimo_stand_in(marimo):
                exec(compile(code, f"<{name}>", "exec"), namespace)
        except (Stopped, NameError):
            # Waiting, or waiting on something that is waiting. Both are "not shown".
            continue
        except BaseException:
            logger.debug("cell %s raised while probing visibility", name)
            continue
        rendered.append(name)
        _apply(namespace, inputs or {}, overrides or {})
    return tuple(rendered)


def _apply(
    namespace: dict[str, Any], inputs: Mapping[str, Any], overrides: Mapping[str, Any]
) -> None:
    """Fill in widget values and replace overridden names, for whatever exists so far.

    Names not yet defined are skipped rather than created. A script naming a widget the notebook
    does not have is a mistake worth catching, and :func:`unused_inputs` is what catches it --
    creating the name here would hide it.
    """
    for name, value in inputs.items():
        widget = namespace.get(name)
        if isinstance(widget, Widget):
            widget.value = value
    for name, value in overrides.items():
        if name in namespace:
            namespace[name] = value


def _public(namespace: dict[str, Any]) -> dict[str, Any]:
    """The notebook's own definitions: no dunders, no harness plumbing."""
    return {
        name: value
        for name, value in namespace.items()
        if not name.startswith("_") and name not in ("mo",)
    }


def unused_inputs(run: NotebookRun, inputs: Mapping[str, Any]) -> tuple[str, ...]:
    """Scripted inputs that named nothing in the notebook.

    A silent no-op here is the worst kind of harness bug: the notebook waits, the run stops, and
    the report blames the notebook for a typo in the script.
    """
    return tuple(sorted(name for name in inputs if name not in run.definitions))
