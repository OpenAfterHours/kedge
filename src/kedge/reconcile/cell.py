"""The reconciliation cell: a green/amber/red panel, per region, that re-runs reactively.

PLAN 4.5 argues that the reconciliation belongs *in the notebook*, not only inside the agent:
"the user sees a green/amber/red panel per translated region, and it re-runs reactively
whenever an upstream cell changes. That artifact -- this Python reproduces the workbook's
numbers to within 1e-6, here's the proof, and it re-checks itself on every run -- is the
difference between a notebook someone wrote and a controlled process someone can sign off."

Reactivity needs no machinery here. A marimo cell re-runs when the variables it reads change,
so a cell that passes its frames to :func:`reconcile_panel` re-runs whenever those frames do.
Nothing is cached and nothing is memoised: a stale green panel would be worse than no panel.

**marimo is confined to :func:`render`.** The comparison engine, the report and the HTML are
all plain Python, so the whole panel is testable without marimo and works unchanged in a
plain script. :class:`ReconciliationPanel` implements marimo's duck-typed ``_mime_``
protocol, which is why returning one from a cell displays it without this module importing
marimo at all. ``marimo._code_mode`` is not imported here and must not be: it is reachable
only from ``notebook/driver.py`` (CONVENTIONS non-negotiable 2), and it must never be
imported by library code a user runs.

References:
- PLAN.md 4.5 (the reconciliation cell), 6.1 (why the private API stays in one file).
"""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING, Any

from kedge.reconcile.baseline import specs_from_mapping
from kedge.reconcile.compare import reconcile_workbook
from kedge.reconcile.model import (
    ReconciliationReport,
    ReconciliationStatus,
    RegionResult,
    Tolerance,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["ReconciliationPanel", "panel_html", "reconcile_panel", "render"]

_COLOURS: dict[str, str] = {
    "green": "#1a7f37",
    "amber": "#9a6700",
    "red": "#cf222e",
}

_SURFACES: dict[str, str] = {
    "green": "rgba(26, 127, 55, 0.08)",
    "amber": "rgba(154, 103, 0, 0.10)",
    "red": "rgba(207, 34, 46, 0.08)",
}


class ReconciliationPanel:
    """A report plus its rendering, displayable in a notebook and readable in a test.

    Holding the report rather than only the HTML matters: the cell that shows the panel is
    also the cell an agent or a test asserts against, and ``panel.report.status`` is a much
    better thing to assert on than a string of markup.

    Example:
        >>> panel = ReconciliationPanel(ReconciliationReport(workbook="p.xlsx", tolerance=Tolerance()))
        >>> panel.status.label
        'NOT RECONCILED'
        >>> bool(panel)
        False
    """

    __slots__ = ("report",)

    def __init__(self, report: ReconciliationReport) -> None:
        self.report = report

    @property
    def status(self) -> ReconciliationStatus:
        """The report's overall outcome."""
        return self.report.status

    @property
    def colour(self) -> str:
        """The traffic-light colour for the panel as a whole."""
        return self.report.colour

    def __bool__(self) -> bool:
        """True only when the whole report passed. An unreconciled panel is falsy."""
        return bool(self.report.status)

    def _mime_(self) -> tuple[str, str]:
        """marimo's display protocol: return the mime type and the payload."""
        return "text/html", panel_html(self.report)

    def _repr_html_(self) -> str:
        """The same HTML for any other notebook front end."""
        return panel_html(self.report)

    def __repr__(self) -> str:
        return f"<ReconciliationPanel {self.report.headline()}>"

    def __str__(self) -> str:
        return str(self.report)


# =============================================================================
# RENDERING
# =============================================================================


def _badge(status: ReconciliationStatus) -> str:
    """The status pill at the top of the panel."""
    colour = _COLOURS[status.colour]
    return (
        f'<span style="display:inline-block;padding:2px 10px;border-radius:10px;'
        f"background:{colour};color:#fff;font-weight:600;font-size:0.8rem;"
        f'letter-spacing:0.04em">{html.escape(status.label)}</span>'
    )


def _mismatch_table(region: RegionResult) -> str:
    """The first N mismatching rows, both values side by side (PLAN 4.5 step 4)."""
    if not region.mismatches:
        return ""
    header = (
        '<tr style="text-align:left;font-size:0.75rem;opacity:0.7">'
        "<th>row</th><th>cell</th><th>Excel</th><th>Python</th><th>delta</th></tr>"
    )
    rows = []
    for mismatch in region.mismatches:
        delta = f"{mismatch.absolute_delta:g}" if mismatch.absolute_delta is not None else "-"
        rows.append(
            '<tr style="font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:0.78rem">'
            f"<td>{mismatch.row}</td>"
            f"<td>{html.escape(mismatch.cell or '')}</td>"
            f"<td>{html.escape(repr(mismatch.expected))}</td>"
            f"<td>{html.escape(repr(mismatch.actual))}</td>"
            f"<td>{html.escape(delta)}</td>"
            "</tr>"
        )
    more = (
        '<div style="font-size:0.75rem;opacity:0.7;margin-top:2px">... further mismatching '
        "rows omitted</div>"
        if region.mismatches_truncated
        else ""
    )
    return (
        '<table style="border-collapse:collapse;margin-top:6px;width:100%">'
        f"{header}{''.join(rows)}</table>{more}"
    )


def _diagnoses(region: RegionResult) -> str:
    """The ranked likely causes and their remedies."""
    if not region.diagnoses:
        return ""
    blocks = []
    for diagnosis in region.diagnoses:
        evidence = "".join(f"<li>{html.escape(item)}</li>" for item in diagnosis.evidence)
        symbol = (
            f" <code>{html.escape(diagnosis.remedy_symbol)}</code>"
            if diagnosis.remedy_symbol
            else ""
        )
        blocks.append(
            '<div style="margin-top:6px;font-size:0.82rem">'
            f"<div><strong>Likely cause ({diagnosis.confidence:.0%}):</strong> "
            f"{html.escape(diagnosis.headline)}</div>"
            f'<ul style="margin:4px 0 4px 18px;padding:0;opacity:0.85">{evidence}</ul>'
            f"<div><strong>Fix:</strong> {html.escape(diagnosis.remedy)}{symbol}</div>"
            f'<div style="opacity:0.6;font-size:0.75rem">{html.escape(diagnosis.reference)}</div>'
            "</div>"
        )
    return "".join(blocks)


def _region_block(region: RegionResult) -> str:
    """One region's row in the panel."""
    colour = _COLOURS[region.colour]
    surface = _SURFACES[region.colour]
    counts = (
        f"{region.rows_matched} matched, {region.rows_differing} differing, "
        f"{region.rows_compared} compared"
    )
    if region.rows_missing_baseline:
        counts += f", {region.rows_missing_baseline} with no cached value"
    worst = ""
    if region.worst_absolute_delta is not None:
        worst = f" | worst absolute {region.worst_absolute_delta:g}"
        if region.worst_relative_delta is not None:
            worst += f", relative {region.worst_relative_delta:g}"
        if region.worst_at:
            worst += f" at {html.escape(region.worst_at)}"

    return (
        f'<div style="border-left:4px solid {colour};background:{surface};padding:8px 12px;'
        'margin:6px 0;border-radius:0 4px 4px 0">'
        f'<div style="font-weight:600">{html.escape(region.name)} '
        f'<span style="font-weight:400;opacity:0.75;font-size:0.85rem">'
        f"{html.escape(region.reference or '')}</span></div>"
        f'<div style="font-size:0.8rem;color:{colour};font-weight:600">'
        f"{html.escape(region.status.label)}</div>"
        f'<div style="font-size:0.8rem;opacity:0.85">{html.escape(counts)}{worst}</div>'
        + (
            f'<div style="font-size:0.82rem;margin-top:4px">{html.escape(region.detail)}</div>'
            if region.detail
            else ""
        )
        + _diagnoses(region)
        + _mismatch_table(region)
        + "</div>"
    )


def panel_html(report: ReconciliationReport) -> str:
    """Render a report as a self-contained block of HTML.

    No marimo, no external stylesheet, no script. The tolerance and the cached-value coverage
    are in the header rather than buried, because they are what qualify the claim: a
    reconciliation that passed at ``1e-2`` says something different from one that passed at
    ``1e-9``.

    Args:
        report: The report to render.

    Returns:
        A single ``<div>`` with everything inline.
    """
    coverage = report.cached_value_status
    if report.cached_value_coverage is not None:
        coverage += f" ({report.cached_value_coverage:.1%} of formula cells)"

    regions = (
        "".join(_region_block(region) for region in report.regions)
        or '<div style="opacity:0.8;font-size:0.85rem">No regions were reconciled. Nothing '
        "about this notebook has been verified against the workbook, and this is not a "
        "pass.</div>"
    )
    notes = "".join(
        f'<li style="font-size:0.78rem;opacity:0.8">{html.escape(note)}</li>'
        for note in report.notes
    )

    return (
        '<div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;'
        'border:1px solid rgba(128,128,128,0.35);border-radius:6px;padding:12px 14px">'
        f'<div style="display:flex;gap:10px;align-items:center">{_badge(report.status)}'
        f'<span style="font-weight:600">Reconciliation against '
        f"{html.escape(report.workbook)}</span></div>"
        f'<div style="margin-top:6px;font-size:0.88rem">{html.escape(report.headline())}</div>'
        f'<div style="margin-top:4px;font-size:0.78rem;opacity:0.75">'
        f"tolerance: {html.escape(report.tolerance.describe())} | cached values: "
        f"{html.escape(coverage)}</div>"
        f"{regions}"
        + (f'<ul style="margin:6px 0 0 18px;padding:0">{notes}</ul>' if notes else "")
        + "</div>"
    )


def render(report: ReconciliationReport) -> Any:
    """Render a report as a marimo ``Html`` element.

    The only function in :mod:`kedge.reconcile` that imports marimo, and it imports it
    lazily, so every other entry point works in a plain script, a test or a CLI process where
    marimo is not wanted.

    Args:
        report: The report to render.

    Returns:
        ``marimo.Html``, or a :class:`ReconciliationPanel` when marimo is not installed —
        which still displays, via the same duck-typed protocol marimo itself uses.
    """
    try:
        import marimo as mo
    except ImportError:  # pragma: no cover - marimo is a hard dependency of kedge
        logger.warning("marimo is not importable; returning the panel object instead")
        return ReconciliationPanel(report)
    return mo.Html(panel_html(report))


# =============================================================================
# THE CELL ITSELF
# =============================================================================


def reconcile_panel(
    workbook: Path | str,
    values: Mapping[str, Any],
    *,
    regions: Sequence[Any] | None = None,
    absolute_tolerance: float | None = None,
    relative_tolerance: float | None = None,
    max_mismatch_rows: int = 20,
) -> ReconciliationPanel:
    """Reconcile a notebook's frames against the workbook and return the panel.

    The body of a reconciliation cell::

        panel = reconcile_panel(
            workbook_path,
            {"rwa": calc, "capital_charge": calc},
            regions=[
                {"id": "rwa", "reference": "Calc!G2:G501", "column": "rwa"},
                {"id": "capital_charge", "reference": "Calc!H2:H501", "column": "capital_charge"},
            ],
        )
        panel

    Because the cell reads ``calc``, marimo re-runs it whenever ``calc`` changes, and the
    panel is recomputed from the workbook every time. Nothing is cached.

    Args:
        workbook: The workbook holding the cached expected values.
        values: Region id to the notebook's frame, series or list for that region.
        regions: The region specs, as :class:`~kedge.reconcile.model.RegionSpec` objects or
            plain dicts. Inferred from the workbook when None, which is slower and is a
            guess — declare them once the columns are settled.
        absolute_tolerance: Absolute tolerance; the default when None.
        relative_tolerance: Relative tolerance; the default when None.
        max_mismatch_rows: How many mismatching rows to show per region.

    Returns:
        A :class:`ReconciliationPanel`. Returning it from a marimo cell displays it; it is
        falsy unless every region passed, so ``assert panel`` is a valid checkpoint.
    """
    defaults = Tolerance()
    tolerance = Tolerance(
        absolute=absolute_tolerance if absolute_tolerance is not None else defaults.absolute,
        relative=relative_tolerance if relative_tolerance is not None else defaults.relative,
    )
    specs = specs_from_mapping(list(regions)) if regions is not None else None
    report = reconcile_workbook(
        workbook,
        values,
        specs=specs,
        tolerance=tolerance,
        max_mismatch_rows=max_mismatch_rows,
    )
    return ReconciliationPanel(report)
