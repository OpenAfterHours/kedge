"""Reconciliation against the committed workbook corpus: the positive path and the vacuous one.

Two fixtures carry the whole argument of PLAN 4.5.

`clean_pipeline.xlsx` was saved with its calculated values intact -- 4,006 formula cells, all
cached, coverage 1.0, independently verified against real Excel over COM. Rebuilding its Calc
sheet in polars and reconciling against those values is the closest thing this project has to
a proof that the translation layer works.

`no_cached_values.xlsx` is the same shape written by a tool rather than by Excel, so every
formula cell reads `None`. Reconciling against it must report NOT RECONCILED, must not report
PASSED, and must say so in words a user cannot misread. PLAN 6.2 lists "reconciliation
silently vacuous" as a risk and CLAUDE.md calls a false pass the most dangerous failure mode
in the project; this file is where that is actually checked.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from kedge.analysis.analyse import analyse
from kedge.analysis.workbook import open_workbook
from kedge.reconcile import (
    ReconciliationStatus,
    RegionSpec,
    infer_regions,
    reconcile_notebook,
    reconcile_panel,
    reconcile_workbook,
)
from kedge.reconcile.model import DiagnosticCause, NotReconciledReason
from kedge.xl import round_half_away

pytestmark = pytest.mark.corpus

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CLEAN = FIXTURES / "clean_pipeline.xlsx"
NO_CACHE = FIXTURES / "no_cached_values.xlsx"

#: The risk weights the workbook's nested IF chain encodes, as a lookup.
RISK_WEIGHTS = {"AAA": 0.2, "AA": 0.3, "A": 0.5, "BBB": 1.0, "BB": 1.5, "B": 2.0}
CAPITAL_RATIO = 0.105

#: The Calc sheet's formula columns, as regions. Written out rather than inferred so the test
#: asserts the numbers rather than the inference.
CLEAN_REGIONS = [
    RegionSpec(id="adjusted", reference="Calc!E2:E501", column="adjusted_notional"),
    RegionSpec(id="risk_weight", reference="Calc!F2:F501", column="risk_weight"),
    RegionSpec(id="rwa", reference="Calc!G2:G501", column="rwa"),
    RegionSpec(id="capital_charge", reference="Calc!H2:H501", column="capital_charge"),
]


@pytest.fixture(scope="module")
def calc() -> pl.DataFrame:
    """The Calc sheet rebuilt in polars, the way a translated notebook would have it.

    `VLOOKUP(..., FALSE)` is a left join, the nested IF chain is a lookup, and both `ROUND`
    calls go through `kedge.xl` -- which is the entire point of that module existing.
    """
    data = pl.read_excel(CLEAN, sheet_name="Data")
    haircuts = pl.read_excel(CLEAN, sheet_name="Ref").select("asset_class", "haircut")
    return (
        data.join(haircuts, on="asset_class", how="left")
        .with_columns(adjusted_notional=pl.col("notional") * (1 - pl.col("haircut")))
        .with_columns(
            risk_weight=pl.col("rating").replace_strict(RISK_WEIGHTS, return_dtype=pl.Float64)
        )
        .with_columns(rwa=round_half_away(pl.col("adjusted_notional") * pl.col("risk_weight"), 2))
        .with_columns(capital_charge=round_half_away(pl.col("rwa") * CAPITAL_RATIO, 2))
    )


# ── the positive path ───────────────────────────────────────────────────────


def test_a_correct_translation_reconciles_against_the_cached_values(calc: pl.DataFrame) -> None:
    """2,000 rows of real Excel output, matched at 1e-6 absolute."""
    report = reconcile_workbook(
        CLEAN, dict.fromkeys((spec.id for spec in CLEAN_REGIONS), calc), specs=CLEAN_REGIONS
    )

    assert report.status is ReconciliationStatus.PASSED
    assert bool(report.status)
    assert report.colour == "green"
    assert report.rows_compared == 2000
    assert [region.status for region in report.regions] == [ReconciliationStatus.PASSED] * 4
    assert all(region.rows_matched == 500 for region in report.regions)
    assert all(region.rows_differing == 0 for region in report.regions)


def test_the_passing_report_records_the_tolerance_it_passed_at(calc: pl.DataFrame) -> None:
    """PLAN 4.5: passing at 1e-2 is a different claim from passing at 1e-9."""
    report = reconcile_workbook(CLEAN, {"rwa": calc}, specs=[CLEAN_REGIONS[2]])

    assert report.tolerance.absolute == 1e-6
    assert report.tolerance.relative == 0.0
    assert "absolute 1e-06" in report.headline()
    assert report.to_dict()["tolerance"] == {"absolute": 1e-6, "relative": 0.0}


def test_the_passing_report_reports_a_worst_delta_small_enough_to_believe(
    calc: pl.DataFrame,
) -> None:
    """A pass with a worst delta at the tolerance boundary is a pass worth looking at twice."""
    report = reconcile_workbook(CLEAN, {"rwa": calc}, specs=[CLEAN_REGIONS[2]])
    region = report.region("rwa")

    assert region is not None
    assert region.worst_absolute_delta is not None
    assert region.worst_absolute_delta < 1e-6
    assert region.worst_at is not None and region.worst_at.startswith("Calc!G")


def test_the_report_carries_the_workbook_hash_and_its_cached_value_coverage(
    calc: pl.DataFrame,
) -> None:
    analysis = analyse(CLEAN)
    report = reconcile_workbook(CLEAN, {"rwa": calc}, specs=[CLEAN_REGIONS[2]], analysis=analysis)

    assert report.workbook_sha256 == analysis.workbook.sha256
    assert report.cached_value_status == "present"
    assert report.cached_value_coverage == 1.0
    assert report.formula_cell_count == 4006


# ── the vacuous path: the one that must never say "passed" ──────────────────


def test_a_workbook_with_no_cached_values_is_not_reconciled_and_never_passed() -> None:
    """The single most important assertion in this milestone."""
    spec = RegionSpec(id="vat", reference="Calc!C2:C201", column="vat")

    report = reconcile_workbook(NO_CACHE, {"vat": [1.0] * 200}, specs=[spec])

    assert report.status is not ReconciliationStatus.PASSED
    assert report.status is ReconciliationStatus.NOT_RECONCILED
    assert not report.status, "an absent baseline must not be truthy"
    assert report.passed == []


def test_the_vacuous_report_says_unmistakably_that_nothing_was_checked() -> None:
    """It has to be readable by someone who is not looking for the catch."""
    spec = RegionSpec(id="vat", reference="Calc!C2:C201", column="vat")

    report = reconcile_workbook(NO_CACHE, {"vat": [1.0] * 200}, specs=[spec])
    region = report.regions[0]
    text = str(report)

    assert region.reason is NotReconciledReason.NO_CACHED_VALUES
    assert region.rows_compared == 0
    assert region.rows_missing_baseline == 200
    assert "NOT been checked" in region.detail
    assert "NOT a pass" in region.detail
    assert "recalculate" in region.detail
    assert "NOT RECONCILED" in text
    assert "PASSED" not in text
    assert report.cached_value_status == "absent"


def test_the_analyser_and_the_reconciler_agree_that_there_is_no_baseline() -> None:
    """The loud finding in the analysis and the amber panel are the same fact."""
    analysis = analyse(NO_CACHE)

    assert analysis.cached_values.status == "absent"
    assert not analysis.is_reconcilable
    assert infer_regions_for(NO_CACHE) == []


def infer_regions_for(workbook: Path) -> list[RegionSpec]:
    """Every region the workbook offers as a reconciliation baseline."""
    analysis = analyse(workbook)
    with open_workbook(workbook) as handle:
        return infer_regions(handle, analysis)


def test_a_notebook_run_against_a_workbook_with_no_cache_reports_nothing_verified(
    tmp_path: Path,
) -> None:
    """End to end, the way `kedge reconcile notebook.py process.xlsx` runs it."""
    notebook = tmp_path / "process.py"
    notebook.write_text(
        "import marimo\n\n"
        "app = marimo.App()\n\n\n"
        "@app.cell\n"
        "def _():\n"
        "    calc = {'vat': [1.0] * 200}\n"
        "    KEDGE_RECONCILE = [\n"
        "        {'id': 'vat', 'reference': 'Calc!C2:C201', 'variable': 'calc', 'column': 'vat'}\n"
        "    ]\n"
        "    return calc, KEDGE_RECONCILE\n",
        encoding="utf-8",
    )

    report = reconcile_notebook(notebook, NO_CACHE)

    assert report.status is ReconciliationStatus.NOT_RECONCILED
    assert not report.status
    assert report.regions[0].reason is NotReconciledReason.NO_CACHED_VALUES


# ── a wrong translation, and the diagnosis that names the fix ───────────────


def test_a_translation_that_forgot_to_round_fails_and_is_diagnosed(calc: pl.DataFrame) -> None:
    """The workbook rounds RWA to 2dp; this one does not, and the report says which fix to apply."""
    unrounded = calc.with_columns(rwa=pl.col("adjusted_notional") * pl.col("risk_weight"))

    report = reconcile_workbook(CLEAN, {"rwa": unrounded}, specs=[CLEAN_REGIONS[2]])
    region = report.regions[0]

    assert report.status is ReconciliationStatus.FAILED
    assert region.rows_differing > 200
    # Half a unit in the last place, plus the float noise of a 500-row product: the deltas
    # are bounded by the rounding step itself, which is the signature the rule keys on.
    assert region.worst_absolute_delta is not None
    assert region.worst_absolute_delta == pytest.approx(0.005, abs=1e-6)
    assert region.top_diagnosis is not None
    assert region.top_diagnosis.cause is DiagnosticCause.ROUNDING_NOT_APPLIED
    assert region.top_diagnosis.remedy_symbol == "kedge.xl.round_half_away"
    assert "rwa" in region.top_diagnosis.remedy


def test_a_translation_that_dropped_rows_fails_on_the_row_count(calc: pl.DataFrame) -> None:
    report = reconcile_workbook(CLEAN, {"rwa": calc.head(400)}, specs=[CLEAN_REGIONS[2]])
    region = report.regions[0]

    assert report.status is ReconciliationStatus.FAILED
    assert region.rows_actual == 400
    assert region.rows_expected == 500
    assert "400 rows against 500" in region.detail


def test_a_join_that_multiplied_rows_is_diagnosed_as_a_duplicated_key(calc: pl.DataFrame) -> None:
    duplicated = pl.concat([calc, calc.head(3)])

    report = reconcile_workbook(CLEAN, {"rwa": duplicated}, specs=[CLEAN_REGIONS[2]])
    region = report.regions[0]

    assert report.status is ReconciliationStatus.FAILED
    assert any(d.cause is DiagnosticCause.JOIN_KEY_NOT_UNIQUE for d in region.diagnoses)


# ── inference, so the CLI needs no configuration to be useful ───────────────


def test_the_reconcilable_regions_of_the_clean_workbook_are_inferred_from_its_headers() -> None:
    specs = infer_regions_for(CLEAN)

    by_column = {spec.column: spec.reference for spec in specs}

    assert by_column["rwa"] == "Calc!G2:G501"
    assert by_column["capital_charge"] == "Calc!H2:H501"
    assert by_column["adjusted_notional"] == "Calc!E2:E501"
    assert all(spec.reference.startswith("Calc!") for spec in specs)


def test_a_notebook_that_declares_nothing_is_still_reconciled_by_name(tmp_path: Path) -> None:
    """The zero-configuration path: match the workbook's headers to the notebook's columns."""
    notebook = tmp_path / "inferred.py"
    notebook.write_text(
        "import marimo\n\n"
        "app = marimo.App()\n\n\n"
        "@app.cell\n"
        "def _():\n"
        "    import polars as pl\n"
        "    from kedge.xl import round_half_away\n"
        f"    workbook = r'{CLEAN}'\n"
        "    weights = {'AAA': 0.2, 'AA': 0.3, 'A': 0.5, 'BBB': 1.0, 'BB': 1.5, 'B': 2.0}\n"
        "    data = pl.read_excel(workbook, sheet_name='Data')\n"
        "    haircuts = pl.read_excel(workbook, sheet_name='Ref').select('asset_class', 'haircut')\n"
        "    calc = (\n"
        "        data.join(haircuts, on='asset_class', how='left')\n"
        "        .with_columns(adjusted_notional=pl.col('notional') * (1 - pl.col('haircut')))\n"
        "        .with_columns(risk_weight=pl.col('rating').replace_strict(weights, return_dtype=pl.Float64))\n"
        "        .with_columns(rwa=round_half_away(pl.col('adjusted_notional') * pl.col('risk_weight'), 2))\n"
        "        .with_columns(capital_charge=round_half_away(pl.col('rwa') * 0.105, 2))\n"
        "    )\n"
        "    return (calc,)\n",
        encoding="utf-8",
    )

    report = reconcile_notebook(notebook, CLEAN)

    assert report.spec_source == "inferred"
    assert report.status is ReconciliationStatus.PASSED, str(report)
    assert {region.column for region in report.regions} >= {"rwa", "capital_charge"}
    assert any("declare" in note.lower() for note in report.notes)


def test_a_declared_notebook_reconciles_end_to_end_the_way_the_cli_runs_it(tmp_path: Path) -> None:
    notebook = tmp_path / "declared.py"
    notebook.write_text(
        "import marimo\n\n"
        "app = marimo.App()\n\n\n"
        "@app.cell\n"
        "def _():\n"
        "    import polars as pl\n"
        "    from kedge.xl import round_half_away\n"
        f"    workbook = r'{CLEAN}'\n"
        "    weights = {'AAA': 0.2, 'AA': 0.3, 'A': 0.5, 'BBB': 1.0, 'BB': 1.5, 'B': 2.0}\n"
        "    data = pl.read_excel(workbook, sheet_name='Data')\n"
        "    haircuts = pl.read_excel(workbook, sheet_name='Ref').select('asset_class', 'haircut')\n"
        "    calc = (\n"
        "        data.join(haircuts, on='asset_class', how='left')\n"
        "        .with_columns(adjusted_notional=pl.col('notional') * (1 - pl.col('haircut')))\n"
        "        .with_columns(risk_weight=pl.col('rating').replace_strict(weights, return_dtype=pl.Float64))\n"
        "        .with_columns(rwa=round_half_away(pl.col('adjusted_notional') * pl.col('risk_weight'), 2))\n"
        "    )\n"
        "    KEDGE_RECONCILE = [\n"
        "        {'id': 'rwa', 'reference': 'Calc!G2:G501', 'variable': 'calc', 'column': 'rwa'}\n"
        "    ]\n"
        "    return calc, KEDGE_RECONCILE\n",
        encoding="utf-8",
    )

    report = reconcile_notebook(notebook, CLEAN)

    assert report.spec_source == "declared"
    assert report.status is ReconciliationStatus.PASSED
    assert report.tolerance.absolute == 1e-6, "the tolerance asked for is the tolerance recorded"
    assert report.notebook == str(notebook)

    # The same notebook, the same numbers, a tolerance a thousand times tighter than the
    # float noise in a 500-row product: the claim is genuinely different and so is the answer.
    strict = reconcile_notebook(notebook, CLEAN, absolute_tolerance=1e-12, relative_tolerance=0.0)

    assert strict.status is ReconciliationStatus.FAILED
    assert strict.tolerance.absolute == 1e-12


# ── the notebook panel, which is what the user actually sees ────────────────


def test_the_notebook_panel_goes_green_on_a_correct_translation(calc: pl.DataFrame) -> None:
    """PLAN 4.5's artifact: the cell a reviewer looks at before signing the process off."""
    panel = reconcile_panel(
        CLEAN,
        {"rwa": calc, "capital_charge": calc},
        regions=[
            {"id": "rwa", "reference": "Calc!G2:G501", "column": "rwa"},
            {"id": "capital_charge", "reference": "Calc!H2:H501", "column": "capital_charge"},
        ],
    )
    html = panel._repr_html_()

    assert bool(panel) is True
    assert panel.colour == "green"
    assert "#1a7f37" in html
    assert "1000 rows compared" in html
    assert "absolute 1e-06" in html


def test_the_notebook_panel_goes_amber_when_there_is_no_baseline() -> None:
    """Amber, not green, and the words say nothing was checked."""
    panel = reconcile_panel(
        NO_CACHE,
        {"vat": [1.0] * 200},
        regions=[{"id": "vat", "reference": "Calc!C2:C201", "column": "vat"}],
    )
    html = panel._repr_html_()

    assert bool(panel) is False
    assert panel.colour == "amber"
    assert panel.status is ReconciliationStatus.NOT_RECONCILED
    assert "#9a6700" in html
    assert "NOT a pass" in html


def test_the_notebook_panel_goes_red_and_shows_the_fix(calc: pl.DataFrame) -> None:
    unrounded = calc.with_columns(rwa=pl.col("adjusted_notional") * pl.col("risk_weight"))

    panel = reconcile_panel(
        CLEAN,
        {"rwa": unrounded},
        regions=[{"id": "rwa", "reference": "Calc!G2:G501", "column": "rwa"}],
    )
    html = panel._repr_html_()

    assert bool(panel) is False
    assert panel.colour == "red"
    assert "kedge.xl.round_half_away" in html
    assert "Likely cause" in html


# ── every fixture, no exceptions ────────────────────────────────────────────


@pytest.mark.parametrize("workbook", sorted(FIXTURES.glob("*.xlsx")), ids=lambda p: p.stem)
def test_every_fixture_reconciles_without_raising_and_without_claiming_a_pass(
    workbook: Path,
) -> None:
    """A hostile workbook produces a report, never a traceback -- and never a false pass.

    Nothing is supplied for the notebook side, so no region can be checked. Every one of them
    must come back NOT RECONCILED with a reason, which is also the strongest possible check
    that "no values" cannot drift into "passed".
    """
    report = reconcile_workbook(workbook, {})

    assert report.status is not ReconciliationStatus.PASSED
    assert not report.status
    assert all(region.reason is not None for region in report.not_reconciled)
    assert report.failed == []
