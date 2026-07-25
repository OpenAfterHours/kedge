"""The comparison engine: vectors in, a defensible verdict out.

The engine is deliberately free of polars, marimo and openpyxl, so everything below runs on
plain lists. The parts that do touch a frame or a notebook are exercised through their real
types, because duck-typing that has never met a `DataFrame` is not duck-typing.
"""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from kedge.reconcile.baseline import BaselineVector
from kedge.reconcile.compare import (
    DECLARED_REGIONS_NAME,
    NotebookRunError,
    compare_vectors,
    declared_regions,
    find_actual,
    notebook_definitions,
    reconcile_notebook,
    reconcile_region,
    resolve_actual,
    to_vector,
)
from kedge.reconcile.model import (
    MismatchKind,
    NotReconciledReason,
    ReconciliationStatus,
    RegionSpec,
    Tolerance,
)

TOLERANCE = Tolerance(absolute=1e-6, relative=1e-9)
SPEC = RegionSpec(id="rwa", reference="Calc!G2:G6", column="rwa")


def _baseline(values: tuple, *, sheet: str = "Calc", first_row: int = 2) -> BaselineVector:
    """A baseline vector as `read_baseline` would have built it."""
    return BaselineVector(
        spec_id=SPEC.id,
        reference=SPEC.reference,
        sheet=sheet,
        values=values,
        cells=tuple(f"G{first_row + index}" for index in range(len(values))),
    )


# ── the arithmetic ──────────────────────────────────────────────────────────


def test_identical_vectors_match_every_row() -> None:
    comparison = compare_vectors([1.0, 2.0, 3.0], [1.0, 2.0, 3.0], tolerance=TOLERANCE)

    assert (comparison.rows_compared, comparison.rows_matched, comparison.rows_differing) == (
        3,
        3,
        0,
    )
    assert comparison.mismatches == []
    assert comparison.worst_absolute_delta == 0.0


def test_a_row_within_either_tolerance_matches_and_still_reports_its_delta() -> None:
    """The worst delta in a passing region is how a reader judges whether the tolerance worked."""
    comparison = compare_vectors([1_000_000.0], [1_000_000.0000004], tolerance=TOLERANCE)

    assert comparison.rows_matched == 1
    assert comparison.worst_absolute_delta == pytest.approx(4e-7, rel=1e-4)
    assert comparison.worst_relative_delta == pytest.approx(4e-13, rel=1e-4)


def test_the_worst_delta_names_the_cell_it_came_from() -> None:
    comparison = compare_vectors(
        [1.0, 2.0, 3.0],
        [1.0, 2.5, 3.0],
        tolerance=TOLERANCE,
        cells=["Calc!G2", "Calc!G3", "Calc!G4"],
    )

    assert comparison.worst_absolute_delta == pytest.approx(0.5)
    assert comparison.worst_at == "Calc!G3"
    assert comparison.mismatches[0].cell == "Calc!G3"


def test_a_tighter_tolerance_turns_a_match_into_a_mismatch() -> None:
    """The tolerance is the claim, so it has to be able to change the answer."""
    loose = compare_vectors([1.0], [1.004], tolerance=Tolerance(absolute=1e-2, relative=0.0))
    tight = compare_vectors([1.0], [1.004], tolerance=Tolerance(absolute=1e-9, relative=0.0))

    assert loose.rows_differing == 0
    assert tight.rows_differing == 1


# ── the missing baseline ────────────────────────────────────────────────────


def test_a_none_expected_value_is_a_missing_baseline_and_never_a_zero() -> None:
    """openpyxl cannot tell "never cached" from "cached a blank", so neither may we."""
    comparison = compare_vectors([1.0, None, 3.0], [1.0, 0.0, 3.0], tolerance=TOLERANCE)

    assert comparison.rows_missing_baseline == 1
    assert comparison.rows_compared == 2
    assert comparison.rows_differing == 0
    assert comparison.mismatches == []


def test_rows_the_two_sides_do_not_share_count_as_differing() -> None:
    """A region whose row count is wrong has not reconciled, whatever the shared rows say."""
    extra = compare_vectors([1.0, 2.0], [1.0, 2.0, 3.0], tolerance=TOLERANCE)
    missing = compare_vectors([1.0, 2.0, 3.0], [1.0, 2.0], tolerance=TOLERANCE)

    assert extra.rows_differing == 1
    assert extra.mismatches[0].kind is MismatchKind.EXTRA_ROW
    assert missing.rows_differing == 1
    assert missing.mismatches[0].kind is MismatchKind.MISSING_ROW


def test_a_trailing_row_with_no_cached_value_is_missing_baseline_not_a_missing_row() -> None:
    comparison = compare_vectors([1.0, None], [1.0], tolerance=TOLERANCE)

    assert comparison.rows_missing_baseline == 1
    assert comparison.rows_differing == 0


# ── mismatch kinds ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("expected", "actual", "kind"),
    [
        (1.0, 2.0, MismatchKind.VALUE_DIFFERS),
        (1.0, None, MismatchKind.ACTUAL_NULL),
        ("#DIV/0!", 0.0, MismatchKind.EXCEL_ERROR),
        (1.0, float("inf"), MismatchKind.NON_FINITE),
        (1.0, float("nan"), MismatchKind.NON_FINITE),
        ("1234.5", 1234.5, MismatchKind.TYPE_DIFFERS),
        (date(2026, 1, 1), date(2026, 1, 2), MismatchKind.VALUE_DIFFERS),
        (46203.0, date(2026, 7, 25), MismatchKind.TYPE_DIFFERS),
        ("Equity", "Cash", MismatchKind.VALUE_DIFFERS),
        (True, False, MismatchKind.VALUE_DIFFERS),
        (True, 1.0, MismatchKind.TYPE_DIFFERS),
    ],
)
def test_disagreements_are_classified_finely_enough_to_diagnose(
    expected: object, actual: object, kind: MismatchKind
) -> None:
    comparison = compare_vectors([expected], [actual], tolerance=TOLERANCE)

    assert comparison.mismatches[0].kind is kind


def test_matching_dates_and_strings_and_booleans_are_matches() -> None:
    comparison = compare_vectors(
        [date(2026, 1, 1), "Equity", True], [date(2026, 1, 1), "Equity", True], tolerance=TOLERANCE
    )

    assert comparison.rows_matched == 3


def test_a_date_difference_is_measured_in_days() -> None:
    comparison = compare_vectors([date(2026, 1, 1)], [date(2026, 1, 4)], tolerance=TOLERANCE)

    assert comparison.mismatches[0].absolute_delta == pytest.approx(3.0)


def test_an_excel_error_is_never_quietly_accepted() -> None:
    """A #DIV/0! in the baseline is a fact about the workbook, not something to smooth over."""
    comparison = compare_vectors(["#DIV/0!"], [None], tolerance=TOLERANCE)

    assert comparison.rows_differing == 1
    assert comparison.mismatches[0].kind is MismatchKind.EXCEL_ERROR


def test_the_mismatch_sample_is_capped_and_says_so() -> None:
    comparison = compare_vectors(
        [float(i) for i in range(100)],
        [float(i) + 1 for i in range(100)],
        tolerance=TOLERANCE,
        sample_limit=5,
    )

    assert comparison.rows_differing == 100
    assert len(comparison.mismatches) == 5


# ── region outcomes ─────────────────────────────────────────────────────────


def test_a_region_where_everything_matches_passes_and_records_the_tolerance() -> None:
    result = reconcile_region(SPEC, _baseline((1.0, 2.0)), [1.0, 2.0], tolerance=TOLERANCE)

    assert result.status is ReconciliationStatus.PASSED
    assert bool(result.status)
    assert result.colour == "green"
    assert result.tolerance == TOLERANCE
    assert "absolute 1e-06 or relative 1e-09" in result.detail


def test_a_region_with_no_cached_values_is_not_reconciled_and_not_passed() -> None:
    """The whole point of the milestone: no baseline is never a pass."""
    result = reconcile_region(SPEC, _baseline((None, None)), [1.0, 2.0], tolerance=TOLERANCE)

    assert result.status is ReconciliationStatus.NOT_RECONCILED
    assert result.status is not ReconciliationStatus.PASSED
    assert not result.status
    assert result.reason is NotReconciledReason.NO_CACHED_VALUES
    assert "NOT a pass" in result.detail
    assert result.colour == "amber"


def test_a_partially_cached_region_is_not_signed_off_even_when_every_compared_row_matches() -> None:
    result = reconcile_region(
        SPEC, _baseline((1.0, None, 3.0)), [1.0, 2.0, 3.0], tolerance=TOLERANCE
    )

    assert result.status is ReconciliationStatus.NOT_RECONCILED
    assert result.reason is NotReconciledReason.PARTIAL_CACHED_VALUES
    assert result.rows_matched == 2
    assert result.rows_missing_baseline == 1
    assert "NOT signed off" in result.detail


def test_an_unreadable_range_is_not_reconciled_rather_than_an_exception() -> None:
    unreadable = BaselineVector(
        spec_id="rwa", reference="Nope!A1:A9", sheet="Nope", unreadable=True
    )

    result = reconcile_region(SPEC, unreadable, [1.0], tolerance=TOLERANCE)

    assert result.status is ReconciliationStatus.NOT_RECONCILED
    assert result.reason is NotReconciledReason.BASELINE_RANGE_UNREADABLE


def test_a_region_the_notebook_produced_nothing_for_is_not_reconciled() -> None:
    result = reconcile_region(SPEC, _baseline((1.0, 2.0)), None, tolerance=TOLERANCE)

    assert result.status is ReconciliationStatus.NOT_RECONCILED
    assert result.reason is NotReconciledReason.NO_ACTUAL_VALUES


def test_a_failing_region_reports_counts_worst_deltas_and_a_capped_sample() -> None:
    expected = tuple(float(i) for i in range(50))
    actual = [float(i) + (0.5 if i % 2 else 0.0) for i in range(50)]

    result = reconcile_region(
        SPEC, _baseline(expected), actual, tolerance=TOLERANCE, max_mismatch_rows=3
    )

    assert result.status is ReconciliationStatus.FAILED
    assert result.colour == "red"
    assert result.rows_compared == 50
    assert result.rows_differing == 25
    assert result.worst_absolute_delta == pytest.approx(0.5)
    assert len(result.mismatches) == 3
    assert result.mismatches_truncated
    assert "25 of 50" in result.detail


def test_a_failing_region_carries_its_diagnoses() -> None:
    expected = (2.63, 0.13, 5.63)
    actual = [2.62, 0.12, 5.62]

    result = reconcile_region(SPEC, _baseline(expected), actual, tolerance=TOLERANCE)

    assert result.top_diagnosis is not None
    assert result.top_diagnosis.remedy_symbol == "kedge.xl.round_half_away"


def test_the_region_headline_reads_as_a_sentence() -> None:
    passed = reconcile_region(SPEC, _baseline((1.0,)), [1.0], tolerance=TOLERANCE)
    absent = reconcile_region(SPEC, _baseline((None,)), [1.0], tolerance=TOLERANCE)

    assert passed.headline().startswith("rwa (Calc!G2:G6): PASSED")
    assert "NOT RECONCILED" in absent.headline()


# ── reading values out of whatever the notebook produced ────────────────────


def test_a_vector_can_be_read_from_every_shape_a_notebook_produces() -> None:
    frame = pl.DataFrame({"rwa": [1.0, 2.0], "other": [9.0, 9.0]})

    assert to_vector([1.0, 2.0]) == [1.0, 2.0]
    assert to_vector(pl.Series("rwa", [1.0, 2.0])) == [1.0, 2.0]
    assert to_vector(frame, column="rwa") == [1.0, 2.0]
    assert to_vector(frame.lazy(), column="rwa") == [1.0, 2.0]
    assert to_vector(frame.select("rwa")) == [1.0, 2.0]
    assert to_vector(42.5) == [42.5]


def test_an_ambiguous_frame_is_refused_rather_than_guessed_at() -> None:
    frame = pl.DataFrame({"a": [1.0], "b": [2.0]})

    assert to_vector(frame) is None
    assert to_vector(None) is None


def test_a_region_is_matched_to_a_named_variable_and_column() -> None:
    definitions = {"calc": pl.DataFrame({"rwa": [1.0, 2.0]}), "other": 7}

    values = resolve_actual(
        definitions, RegionSpec(id="r", reference="Calc!G2:G3", variable="calc", column="rwa")
    )

    assert values == [1.0, 2.0]


def test_a_region_with_no_variable_is_matched_by_column_name() -> None:
    definitions = {
        "unrelated": pl.DataFrame({"x": [0.0]}),
        "calc": pl.DataFrame({"rwa": [1.0, 2.0]}),
    }

    values = resolve_actual(definitions, RegionSpec(id="r", reference="Calc!G2:G3", column="rwa"))

    assert values == [1.0, 2.0]


def test_a_column_name_shared_by_two_frames_is_resolved_by_the_regions_extent() -> None:
    """A lookup table and the calculation both have a `haircut` column. Only one is 500 rows.

    Found by running the inferred path over `clean_pipeline.xlsx`: matching on name alone
    picked the six-row `Ref` frame for `Calc!D2:D501` and reported a confident, wrong failure.
    """
    definitions = {
        "haircuts": pl.DataFrame({"asset_class": ["a", "b"], "haircut": [0.1, 0.2]}),
        "calc": pl.DataFrame({"haircut": [0.1, 0.2, 0.3, 0.1, 0.2]}),
    }
    spec = RegionSpec(id="haircut", reference="Calc!D2:D6", column="haircut")

    name, values = find_actual(definitions, spec)

    assert name == "calc"
    assert values == [0.1, 0.2, 0.3, 0.1, 0.2]


def test_where_no_frame_has_the_right_extent_the_first_match_is_used_and_the_row_count_shows() -> (
    None
):
    """Falling back is fine as long as nothing is hidden: the report says the counts differ."""
    definitions = {"lookup": pl.DataFrame({"haircut": [0.1, 0.2]})}
    spec = RegionSpec(id="haircut", reference="Calc!D2:D501", column="haircut")

    name, values = find_actual(definitions, spec)

    assert name == "lookup"
    assert values == [0.1, 0.2]


def test_a_named_variable_always_wins_over_the_search() -> None:
    definitions = {
        "calc": pl.DataFrame({"haircut": [0.1] * 5}),
        "lookup": pl.DataFrame({"haircut": [0.9, 0.8]}),
    }
    spec = RegionSpec(id="h", reference="Calc!D2:D3", column="haircut", variable="calc")

    assert find_actual(definitions, spec) == ("calc", [0.1] * 5)


def test_a_region_that_matches_nothing_returns_none_rather_than_an_empty_vector() -> None:
    """An empty vector would compare as "every row missing"; None is "the notebook has no such column"."""
    assert resolve_actual({}, RegionSpec(id="r", reference="Calc!G2:G3", column="rwa")) is None
    assert (
        resolve_actual(
            {"calc": pl.DataFrame({"x": [1.0]})},
            RegionSpec(id="r", reference="C!A1", variable="missing"),
        )
        is None
    )


# ── declared regions ────────────────────────────────────────────────────────


def test_a_notebook_declares_its_regions_with_plain_dicts() -> None:
    definitions = {
        DECLARED_REGIONS_NAME: [
            {"id": "rwa", "reference": "Calc!G2:G501", "variable": "calc", "column": "rwa"},
            {"reference": "Calc!H2:H501", "column": "capital_charge"},
        ]
    }

    specs = declared_regions(definitions)

    assert [spec.id for spec in specs] == ["rwa", "capital_charge"]
    assert specs[0].variable == "calc"


def test_declared_regions_accept_the_spec_objects_too() -> None:
    spec = RegionSpec(id="rwa", reference="Calc!G2:G9")

    assert declared_regions({DECLARED_REGIONS_NAME: [spec]}) == [spec]
    assert declared_regions({DECLARED_REGIONS_NAME: spec}) == [spec]


def test_a_notebook_that_declares_nothing_declares_nothing() -> None:
    assert declared_regions({}) == []
    assert declared_regions({DECLARED_REGIONS_NAME: "Calc!G2:G9"}) == []


def test_a_malformed_declared_region_is_skipped_rather_than_fatal() -> None:
    specs = declared_regions(
        {DECLARED_REGIONS_NAME: [{"id": "ok", "reference": "Calc!G2:G9"}, {"id": "bad"}, 42]}
    )

    assert [spec.id for spec in specs] == ["ok"]


# ── running the notebook ────────────────────────────────────────────────────

_NOTEBOOK = """
import marimo

app = marimo.App()


@app.cell
def _():
    calc = {"rwa": [1.0, 2.0]}
    KEDGE_RECONCILE = [{"id": "rwa", "reference": "Calc!G2:G3", "variable": "calc", "column": "rwa"}]
    return calc, KEDGE_RECONCILE
"""

_BROKEN_NOTEBOOK = """
import marimo

app = marimo.App()


@app.cell
def _():
    raise RuntimeError("the hand-in is not where the notebook expects it")
"""


def test_a_notebook_is_executed_through_marimos_public_app_run(tmp_path: Path) -> None:
    """No kernel, no session, no `_code_mode`: `App.run()` is public API."""
    notebook = tmp_path / "process.py"
    notebook.write_text(_NOTEBOOK, encoding="utf-8")

    definitions = notebook_definitions(notebook)

    assert definitions["calc"] == {"rwa": [1.0, 2.0]}
    assert declared_regions(definitions)[0].id == "rwa"


def test_a_plain_script_falls_back_to_its_module_globals(tmp_path: Path) -> None:
    script = tmp_path / "plain.py"
    script.write_text("total = 41 + 1\n", encoding="utf-8")

    assert notebook_definitions(script)["total"] == 42


def test_a_notebook_that_raises_reports_the_error_rather_than_a_traceback(tmp_path: Path) -> None:
    notebook = tmp_path / "broken.py"
    notebook.write_text(_BROKEN_NOTEBOOK, encoding="utf-8")

    with pytest.raises(NotebookRunError, match="not where the notebook expects"):
        notebook_definitions(notebook)


def test_a_missing_notebook_says_so(tmp_path: Path) -> None:
    with pytest.raises(NotebookRunError, match="does not exist"):
        notebook_definitions(tmp_path / "absent.py")


def test_a_notebook_that_will_not_run_is_not_reconciled_never_passed(tmp_path: Path) -> None:
    notebook = tmp_path / "broken.py"
    notebook.write_text(_BROKEN_NOTEBOOK, encoding="utf-8")
    workbook = Path("tests/fixtures/clean_pipeline.xlsx")

    report = reconcile_notebook(notebook, workbook)

    assert report.status is ReconciliationStatus.NOT_RECONCILED
    assert not report.status
    assert report.regions[0].reason is NotReconciledReason.NOTEBOOK_FAILED
    assert "not run to completion" in report.regions[0].detail


def test_the_notebook_run_does_not_leave_the_module_behind(tmp_path: Path) -> None:
    """Executing a notebook twice must not hit a cached module from the first run."""
    import sys

    notebook = tmp_path / "process.py"
    notebook.write_text(_NOTEBOOK, encoding="utf-8")
    before = set(sys.modules)

    notebook_definitions(notebook)
    notebook_definitions(notebook)

    assert {
        name for name in set(sys.modules) - before if name.startswith("_kedge_reconcile")
    } == set()


def test_infinity_is_reported_as_a_non_finite_mismatch_not_lost(tmp_path: Path) -> None:
    """`inf` must survive into the report, since it is the divide-by-zero evidence."""
    result = reconcile_region(SPEC, _baseline((1.0,)), [math.inf], tolerance=TOLERANCE)

    assert result.status is ReconciliationStatus.FAILED
    assert result.mismatches[0].actual == math.inf
