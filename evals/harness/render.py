"""Decide the order a generated conversion's cells are written in, and render the file.

Split from ``convert.py`` because it answers one question the rest of the pipeline only consumes:
*where does each cell go*. That question turned out to matter far more than it looks. marimo
decides **when** a cell runs from the dataflow graph and **where** it appears from the file, so a
notebook whose cells are emitted in the wrong order is wrong for the reader even though it is
correct for the kernel -- and this harness, which executes in file order with no kernel, cannot
run one at all.

So the order is rewritten, and every rewrite is reported. See :func:`plan_layout`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib import metadata
from typing import TYPE_CHECKING

from kedge.notebook.codegen import (
    FileCell,
    NotebookDocument,
    analyse_cells,
    render_notebook,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from harness.cellgen import ConversionResult

logger = logging.getLogger(__name__)

__all__ = [
    "ForwardReference",
    "Layout",
    "plan_layout",
    "write_notebook",
]


@dataclass(frozen=True, slots=True)
class ForwardReference:
    """A cell that reads a name the scaffolder defines further down the file."""

    cell: str
    name: str
    defined_in: str

    def render(self) -> str:
        return f"{self.cell} reads {self.name!r}, which {self.defined_in} defines below it"


@dataclass(frozen=True, slots=True)
class Layout:
    """The order a conversion's cells are written in, and what it cost to get there.

    ``order`` indexes into the scaffolder's own order. ``moved`` names only the cells that
    genuinely crossed another -- comparing each position against its original index would report
    every cell after the first move as having moved, which turns one displaced panel into a note
    listing half the notebook.
    """

    order: tuple[int, ...]
    moved: tuple[str, ...] = ()
    forward_references: tuple[ForwardReference, ...] = ()

    @property
    def rewritten(self) -> bool:
        return bool(self.moved)


def plan_layout(names: Sequence[str], codes: Sequence[str]) -> Layout:
    """Decide the file order, and record every forward reference that forced a change.

    marimo runs a notebook as a *graph*, so the scaffolder is entitled to emit a cell above one
    it reads from -- and it does, deliberately: ``_with_reconciliation`` puts the reconciliation
    panel immediately after the last stage it can report on, which for the reference plan is
    above the hand-off whose statements it also cites.

    **That is not free, and the docstring this function used to carry said it was.** marimo lays
    a notebook out in file order: it is the graph that decides *when* a cell runs and the file
    that decides *where* it appears. ``scaffold._with_reconciliation`` is itself an argument
    about placement ("putting it before the checkpoint makes it an argument"),
    ``drive.visible_cells`` walks the file top to bottom, and the ``progressive_disclosure``
    grader asks what a user sees on opening. So a cell emitted above the name it reads is a
    defect in the notebook a user gets, not a quirk of grading one without a kernel -- in
    ``harness/drive.py`` it is additionally a ``NameError`` that reports the whole notebook as
    broken.

    The order is still rewritten, because a run that stops at the first forward reference grades
    nothing at all. What changed is that the rewrite is reported: every forward reference comes
    back on the :class:`Layout` and :func:`convert_and_grade` turns it into a :class:`Defect`
    naming the cells, so the report says "kedge emitted this in an order it cannot render in"
    rather than "the harness tidied the file up".

    A cycle, which marimo would reject anyway, degrades to the original order rather than
    looping.

    Returns:
        The :class:`Layout`: the original positions in the order they should be written, the
        cells that had to move, and the forward references that made them move.
    """
    analyses = analyse_cells(list(codes))
    defines = [set(analysis.defs) for analysis in analyses]
    reads = [set(analysis.refs) for analysis in analyses]

    forward: list[ForwardReference] = []
    for index, wanted in enumerate(reads):
        for name in sorted(wanted - defines[index]):
            later = next(
                (other for other in range(index + 1, len(names)) if name in defines[other]), None
            )
            if later is not None:
                forward.append(
                    ForwardReference(cell=names[index], name=name, defined_in=names[later])
                )

    remaining = list(range(len(names)))
    order: list[int] = []
    while remaining:
        for position, index in enumerate(remaining):
            pending: set[str] = set()
            for other in remaining:
                if other != index:
                    pending |= defines[other]
            if not (reads[index] & pending - defines[index]):
                order.append(index)
                remaining.pop(position)
                break
        else:
            stuck = remaining.pop(0)
            logger.warning("cell %s sits in a dependency cycle; left where it was", names[stuck])
            order.append(stuck)

    moved: list[str] = []
    highest = -1
    for index in order:
        if index < highest:
            moved.append(names[index])
        highest = max(highest, index)

    return Layout(
        order=tuple(order),
        moved=tuple(sorted(moved)),
        forward_references=tuple(forward),
    )


def _marimo_version() -> str:
    """The marimo that is actually installed, for the ``__generated_with`` line.

    Not a copy of the version string in ``adjustment_signoff/build_notebook.py``. There were
    three hardcoded ``"0.23.15"`` in this repository and a fourth would have been one more place
    to forget on an upgrade -- while the value this file wants is a fact about the environment
    that rendered it, which is exactly what the metadata says.
    """
    try:
        return metadata.version("marimo")
    except metadata.PackageNotFoundError:  # pragma: no cover - marimo is a hard dependency
        logger.warning("marimo is not installed; the notebook header will say so")
        return "unknown"


def _notebook_prefix() -> str:
    """The header of a generated conversion, which says what it is and what wrote it."""
    return (
        '"""A conversion generated by kedge\'s code-generation eval.\n\n'
        "The structure is the scaffolder's, from an approved plan. The cell bodies under each\n"
        "stage were written by a model and accepted by kedge.agent.validate.validate_cell. It is\n"
        "kept so a score can be argued with: read the cells, not the number.\n"
        '"""\n\n'
        "import marimo\n\n"
        f'__generated_with = "{_marimo_version()}"\n'
        'app = marimo.App(width="medium")\n\n\n'
    )


_NOTEBOOK_SUFFIX = '\n\nif __name__ == "__main__":\n    app.run()\n'


def write_notebook(result: ConversionResult, path: Path, layout: Layout | None = None) -> Path:
    """Render the converted cells to a marimo notebook file.

    The same route ``adjustment_signoff/build_notebook.py`` takes -- marimo's static analysis
    regenerates every ``def`` line and ``return`` tuple, so the signatures describe the dataflow
    rather than somebody's memory of it. It cannot *call* ``build_notebook.build``: that function
    renders one case's own committed cells behind its own docstring and takes no cells as an
    argument, and ``harness/`` is case-independent by design (``evals/README.md``), so importing
    from a case directory would couple the runner to one workbook. What was worth removing was
    the duplicated marimo version, and :func:`_marimo_version` removes it at the root.

    Args:
        result: The generated conversion.
        path: Where to write it.
        layout: The order to write the cells in. Computed with :func:`plan_layout` when not
            supplied -- pass one in to report on it as well as use it.

    Raises:
        ValueError: naming the cells whose bodies do not compile. That can only happen where a
            hole was left unfilled by a model whose reply never parsed *and* the scaffolder's
            placeholder was replaced anyway -- so it is a bug here, not a bad conversion, and it
            says so rather than writing a notebook nothing can open.
    """
    resolved = layout or plan_layout(result.names, result.codes)
    names = [result.names[index] for index in resolved.order]
    codes = [result.codes[index].strip("\n") for index in resolved.order]
    analyses = analyse_cells(codes)
    unparsed = [name for name, analysis in zip(names, analyses, strict=True) if not analysis.parsed]
    if unparsed:
        msg = f"these cell bodies do not parse, so no notebook was written: {', '.join(unparsed)}"
        raise ValueError(msg)

    document = NotebookDocument(
        prefix=_notebook_prefix(),
        cells=tuple(
            FileCell(name=name, code=code) for name, code in zip(names, codes, strict=True)
        ),
        separators=("\n\n",) * (len(codes) - 1),
        suffix=_NOTEBOOK_SUFFIX,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_notebook(document, analyses, toplevel=frozenset()), encoding="utf-8")
    logger.info("wrote %s: %d cell(s)", path.name, len(codes))
    return path
