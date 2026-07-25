"""The notebook panel: green, amber, red, and no marimo anywhere it does not belong.

Two things are asserted here that are easy to lose in a refactor: the panel is falsy unless
the whole report passed, so `assert panel` is a valid checkpoint in a notebook; and marimo is
imported inside one function rather than at module scope, so the comparison engine stays
testable and usable without it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from kedge.reconcile import cell as cell_module
from kedge.reconcile.cell import ReconciliationPanel, panel_html, render
from kedge.reconcile.model import (
    Diagnosis,
    DiagnosticCause,
    Mismatch,
    NotReconciledReason,
    ReconciliationReport,
    ReconciliationStatus,
    RegionResult,
    Tolerance,
)

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "kedge" / "reconcile"


def _report(*regions: RegionResult, **kwargs) -> ReconciliationReport:
    return ReconciliationReport(
        workbook="process.xlsx", tolerance=Tolerance(), regions=list(regions), **kwargs
    )


def _passed(spec_id: str = "rwa") -> RegionResult:
    return RegionResult(
        spec_id=spec_id,
        reference="Calc!G2:G501",
        label="rwa",
        status=ReconciliationStatus.PASSED,
        tolerance=Tolerance(),
        rows_compared=500,
        rows_matched=500,
        worst_absolute_delta=1.86e-09,
        worst_at="Calc!G189",
        detail="500 of 500 rows matched.",
    )


def _failed() -> RegionResult:
    return RegionResult(
        spec_id="charge",
        reference="Calc!H2:H501",
        status=ReconciliationStatus.FAILED,
        tolerance=Tolerance(),
        rows_compared=500,
        rows_matched=488,
        rows_differing=12,
        detail="12 of 500 compared rows differ.",
        mismatches=[
            Mismatch(row=4, cell="Calc!H5", expected=2.68, actual=2.67, absolute_delta=0.01)
        ],
        mismatches_truncated=True,
        diagnoses=[
            Diagnosis(
                cause=DiagnosticCause.ROUNDING_MODE,
                confidence=0.9,
                headline="Wrong rounding mode.",
                evidence=["Every difference is one unit in the last place."],
                remedy="Use round_half_away.",
                remedy_symbol="kedge.xl.round_half_away",
            )
        ],
    )


def _not_reconciled() -> RegionResult:
    return RegionResult(
        spec_id="vat",
        reference="Calc!C2:C201",
        status=ReconciliationStatus.NOT_RECONCILED,
        reason=NotReconciledReason.NO_CACHED_VALUES,
        detail=NotReconciledReason.NO_CACHED_VALUES.explanation,
        tolerance=Tolerance(),
    )


# ── the panel object ────────────────────────────────────────────────────────


def test_a_panel_is_falsy_unless_every_region_passed() -> None:
    assert bool(ReconciliationPanel(_report(_passed()))) is True
    assert bool(ReconciliationPanel(_report(_passed(), _not_reconciled()))) is False
    assert bool(ReconciliationPanel(_report(_failed()))) is False
    assert bool(ReconciliationPanel(_report())) is False


def test_a_panel_keeps_the_report_so_a_test_can_assert_on_it() -> None:
    panel = ReconciliationPanel(_report(_passed()))

    assert panel.status is ReconciliationStatus.PASSED
    assert panel.colour == "green"
    assert panel.report.rows_compared == 500
    assert "PASSED" in repr(panel)


def test_a_panel_renders_through_marimos_duck_typed_display_protocol() -> None:
    """Returning one from a cell displays it without this module importing marimo."""
    panel = ReconciliationPanel(_report(_passed()))

    mime, payload = panel._mime_()

    assert mime == "text/html"
    assert payload == panel._repr_html_()
    assert "PASSED" in payload


# ── the html ────────────────────────────────────────────────────────────────


def test_the_panel_states_the_tolerance_and_the_cached_value_position() -> None:
    """Both qualify the claim, so neither may be buried."""
    html = panel_html(_report(_passed(), cached_value_status="present", cached_value_coverage=1.0))

    assert "absolute 1e-06" in html
    assert "relative" not in html, "a relative tolerance of 0 does nothing; naming it misleads"
    assert "cached values: present" in html
    assert "100.0% of formula cells" in html


@pytest.mark.parametrize(
    ("region", "colour"),
    [(_passed(), "#1a7f37"), (_not_reconciled(), "#9a6700"), (_failed(), "#cf222e")],
)
def test_each_region_gets_its_own_traffic_light(region: RegionResult, colour: str) -> None:
    assert colour in panel_html(_report(region))


def test_a_failing_region_shows_both_values_side_by_side_and_its_remedy() -> None:
    html = panel_html(_report(_failed()))

    assert "2.68" in html and "2.67" in html
    assert "Wrong rounding mode." in html
    assert "kedge.xl.round_half_away" in html
    assert "further mismatching rows omitted" in html


def test_a_report_with_no_regions_says_nothing_was_verified() -> None:
    html = panel_html(_report())

    assert "No regions were reconciled" in html
    assert "not a pass" in html


def test_values_are_escaped_rather_than_injected() -> None:
    """Cell values are arbitrary workbook content and end up inside the page."""
    region = RegionResult(
        spec_id="x",
        status=ReconciliationStatus.FAILED,
        tolerance=Tolerance(),
        rows_compared=1,
        rows_differing=1,
        mismatches=[Mismatch(row=1, expected="<script>alert(1)</script>", actual="ok")],
    )

    html = panel_html(_report(region))

    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_render_returns_a_marimo_element() -> None:
    element = render(_report(_passed()))

    assert type(element).__name__ == "Html"
    assert "PASSED" in element.text


# ── the import rules ────────────────────────────────────────────────────────


def test_marimo_is_imported_inside_the_rendering_function_and_nowhere_else() -> None:
    """CONVENTIONS: the comparison engine must be usable without marimo."""
    tree = ast.parse(Path(cell_module.__file__).read_text(encoding="utf-8"))

    module_level = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    imported_names = {
        alias.name for node in module_level if isinstance(node, ast.Import) for alias in node.names
    }
    assert not any(name.startswith("marimo") for name in imported_names)

    functions_importing_marimo = {
        function.name
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef)
        for node in ast.walk(function)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name.startswith("marimo")
    }
    assert functions_importing_marimo == {"render"}


def test_the_private_code_mode_api_is_not_reachable_from_reconciliation() -> None:
    """PLAN 6.1: `_code_mode` lives in notebook/driver.py and must not leak into library code.

    Reconciliation is library code a user runs, and it runs in a notebook. The repo-wide guard
    in `test_driver_codegen.py` covers `src/`; this one keeps the rule visible where it would
    be most tempting to break, since the notebook panel is the one module here that is
    marimo-adjacent.
    """

    def references_code_mode(node: ast.AST) -> bool:
        if isinstance(node, ast.Import):
            return any("_code_mode" in alias.name for alias in node.names)
        if isinstance(node, ast.ImportFrom):
            return "_code_mode" in (node.module or "")
        return isinstance(node, ast.Attribute) and node.attr == "_code_mode"

    offenders = sorted(
        path.name
        for path in PACKAGE.glob("*.py")
        if any(
            references_code_mode(node)
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        )
    )

    assert offenders == []
