"""Ask a model for the cell bodies a scaffold left unwritten -- by running the product's driver.

`harness/convert.py` owns the pipeline; this module owns the half of it a model can be blamed
for. It used to *implement* that half: five hundred lines enumerating the ``TODO(kedge)`` holes
:func:`kedge.notebook.scaffold.build_cells` leaves, asking for each one in scaffold order, and
gating every reply through :func:`kedge.agent.validate.validate_cell`. That loop now ships --
:mod:`kedge.agent.fill` is what ``kedge convert`` runs -- so this module is a name map onto it and
nothing else.

The copy was not a stylistic problem. A reviewer compared the two modules' syntax trees with
docstrings stripped and found ``_body_of``, ``_without_echoed_header``, ``_undefined_name``,
``_missing_name_verdict``, ``_registry_for`` and ``cell_messages`` byte-identical, ``_fill_one``
different by two renamed types and one log string -- and the two *prompts* already six bytes
apart. Which means the number this eval exists to produce, what a model scores writing a whole
conversion, was being produced by a copy of the driver rather than by the driver. An eval that
measures a near-identical reimplementation measures the reimplementation.

## The eval's vocabulary, and why it is kept

``CellOutcome``, ``GeneratedCell``, ``ConversionResult`` are the names `harness/convert.py`,
`harness/findings.py` and `harness/render.py` read better for: a *generated* conversion is what
this eval is about, and ``FillReport`` is the product's word for what a *conversion* produced.
They are aliases, not wrappers -- the same objects under a second name -- so there is nothing to
keep in step.

## One semantic difference, and the parameter that turns out to be the answer

``convert`` had no ``stop_on_error`` and ``CellOutcome`` had no ``SKIPPED``, so a dead endpoint
came back here as *n* ``ERROR`` and in the product as one ``ERROR`` and *n-1* ``SKIPPED``. That
looked like a choice between two readings of the same event. It is not: it is one implementation
with a parameter on it, and the two callers want it set differently for reasons that are about
what each is *for*.

**The product stops.** A user is waiting at a terminal, one dead endpoint is one fact, and putting
it five more questions costs five timeouts to write that fact down five times.

**The eval carries on** -- :func:`convert` binds ``stop_on_error=False``. A run here is a
measurement of a model over six holes, taken unattended, against a real endpoint that has been
paid for. One transient 429 on the first hole would otherwise abandon the other five: outcome
``NO_MODEL``, nothing graded, a whole leg of a sweep spent on one call. Per-hole attribution is
the thing this eval exists for, and the machinery to report a partial run honestly is already
here -- :attr:`~harness.convert.ConversionOutcome.INTERRUPTED` and
:class:`~harness.findings.Coverage` say exactly how much of the rubric a transport failure took
away. Five extra requests to a genuinely dead endpoint are cheap by comparison; they fail fast and
nobody is watching.

``SKIPPED`` therefore does not arise on this path today. It is in
:class:`CellOutcome` regardless, because the enum is the product's and a caller passing
``stop_on_error=True`` gets it.

:attr:`~harness.convert.ConversionOutcome.NO_MODEL` is reachable under either setting, because it
no longer counts errors: it asks :attr:`kedge.agent.fill.FillReport.unmeasured`, which is true
when no request to the model was answered at all.
"""

from __future__ import annotations

import logging
from functools import partial

from kedge.agent.fill import (
    FillAttempt,
    FilledCell,
    FillOutcome,
    FillReport,
    fill_holes,
)
from kedge.notebook.scaffold import TODO_MARKER

logger = logging.getLogger(__name__)

__all__ = [
    "TODO_MARKER",
    "CellAttempt",
    "CellOutcome",
    "ConversionResult",
    "GeneratedCell",
    "convert",
]
# `holes_in` is deliberately **not** re-exported. It is `scaffold.py`'s predicate and it answers a
# different question from the one this module's denominator answers -- it counts what a notebook
# still owes, including holes nothing can be asked to fill. A reader who reaches for it through
# here gets the wrong one; `kedge.notebook.scaffold` is where it lives and where it is right.

CellOutcome = FillOutcome
"""How one hole came out: :class:`kedge.agent.fill.FillOutcome`, under the eval's name.

Six members now rather than four, and the two new ones are worth knowing. ``SKIPPED`` is what a
run abandoned after a transport failure records for the holes it never reached -- not reachable
from :func:`convert`, which does not abandon; see the module docstring. ``UNFILLABLE`` is a hole
nothing can be asked to fill, and it is the one that used to be **missing from the denominator**:
a stage whose hand-off declares no statement scaffolds to a cell with no placeholder, was dropped
where it was found, and the model that wrote that plan was then printed as having filled every
hole it was given.
"""

CellAttempt = FillAttempt
"""One request-and-verdict: :class:`kedge.agent.fill.FillAttempt`, under the eval's name."""

GeneratedCell = FilledCell
"""One hole and everything that happened to it: :class:`kedge.agent.fill.FilledCell`."""

ConversionResult = FillReport
"""A scaffold with its holes filled, or not: :class:`kedge.agent.fill.FillReport`.

It carries ``plan``, ``names``, ``codes``, ``scaffolded`` and the per-hole record, which is
everything `harness/findings.py` and `harness/render.py` read off it -- and one thing the eval's
own class never had: ``refused``, the cells a *notebook* would not accept. That is always empty
here, because this seam renders a file rather than syncing one into a live notebook.

Two report lines read differently from the eval's old class, deliberately: ``summary_line()``
adds "after retries" and ``render()`` leads ``conversion:`` where this once led ``generation:``.
Same numbers. What a reader of a conversion report sees is now the line ``kedge convert`` prints,
which is the whole argument of this module applied to its own output.
"""

convert = partial(fill_holes, stop_on_error=False)
"""Scaffold the plan and have the model fill every hole: :func:`kedge.agent.fill.fill_holes`.

The shipped function with exactly one keyword bound, so the *only* difference between what the
eval runs and what ``kedge convert`` runs is a single deliberate argument that a reader can see
here. See the module docstring for why an eval presses on where the product stops; a caller can
pass ``stop_on_error=True`` and get the product's behaviour back.
"""
