"""The Excel side: reading a range, and being honest about what came back.

`read_baseline` is exercised against a stub handle rather than a workbook. The stub answers
exactly what `WorkbookHandle` answers -- a list of rows of values, `None` where Excel cached
nothing -- so the degradation paths (unknown sheet, unparseable range, all-None range) are
reachable without eight fixture files, and the real handle is exercised in the corpus tests.
"""

from __future__ import annotations

from typing import Any

import pytest

from conftest import make_analysis, make_operation
from kedge.reconcile.baseline import (
    BaselineVector,
    infer_regions,
    operation_reference,
    read_baseline,
    read_baselines,
    specs_from_mapping,
    split_reference,
)
from kedge.reconcile.model import NotReconciledReason, RegionSpec


class _StubHandle:
    """A workbook handle with a fixed answer, matching the real read_range contract."""

    def __init__(self, sheets: dict[str, list[list[Any]]]) -> None:
        self._sheets = sheets
        self.calls: list[tuple[str, str]] = []

    @property
    def sheet_names(self) -> list[str]:
        return list(self._sheets)

    def read_range(self, sheet: str, a1: str, **_: Any) -> list[list[Any]]:
        self.calls.append((sheet, a1))
        return self._sheets.get(sheet, [])


# ── references ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("reference", "sheet", "a1"),
    [
        ("Calc!G2:G501", "Calc", "G2:G501"),
        ("=Calc!G2", "Calc", "G2"),
        ("'Trade Data'!A1:A9", "Trade Data", "A1:A9"),
        ("'O''Neill'!B2:B3", "O'Neill", "B2:B3"),
        ("G2:G501", None, "G2:G501"),
    ],
)
def test_a_reference_splits_into_a_sheet_and_a_range(
    reference: str, sheet: str | None, a1: str
) -> None:
    assert split_reference(reference) == (sheet, a1)


@pytest.mark.parametrize(
    ("orientation", "count", "expected"),
    [
        ("column", 500, "Calc!G2:G501"),
        ("row", 4, "Calc!G2:J2"),
        ("single", 1, "Calc!G2"),
        ("block", 12, None),
    ],
)
def test_a_regions_target_range_is_reconstructed_from_its_anchor_and_extent(
    orientation: str, count: int, expected: str | None
) -> None:
    """The analyser records where a region starts and how big it is, not the rectangle."""
    operation = make_operation(sheet="Calc", anchor="G2", cell_count=count, orientation=orientation)

    assert operation_reference(operation) == expected


def test_a_sheet_name_that_needs_quoting_gets_it() -> None:
    operation = make_operation(sheet="Trade Data", anchor="B2", cell_count=3, orientation="column")

    assert operation_reference(operation) == "'Trade Data'!B2:B4"


# ── the baseline vector ─────────────────────────────────────────────────────


def test_a_range_of_cached_values_is_a_usable_baseline() -> None:
    handle = _StubHandle({"Calc": [[1.0], [2.0], [3.0]]})

    vector = read_baseline(handle, RegionSpec(id="rwa", reference="Calc!G2:G4"))

    assert vector.values == (1.0, 2.0, 3.0)
    assert vector.cells == ("G2", "G3", "G4")
    assert vector.cell_at(1) == "Calc!G3"
    assert vector.status == "present"
    assert vector.reason is None


def test_a_range_where_excel_cached_nothing_is_absent_not_empty_and_not_zero() -> None:
    """The tool-written-workbook case: every formula cell reads None under data_only."""
    handle = _StubHandle({"Calc": [[None], [None], [None]]})

    vector = read_baseline(handle, RegionSpec(id="vat", reference="Calc!C2:C4"))

    assert vector.status == "absent"
    assert vector.present_count == 0
    assert vector.missing_count == 3
    assert vector.reason is NotReconciledReason.NO_CACHED_VALUES


def test_a_range_with_holes_is_partial() -> None:
    handle = _StubHandle({"Calc": [[1.0], [None], [3.0]]})

    vector = read_baseline(handle, RegionSpec(id="rwa", reference="Calc!G2:G4"))

    assert vector.status == "partial"
    assert vector.reason is NotReconciledReason.PARTIAL_CACHED_VALUES


def test_an_unknown_sheet_is_unreadable_rather_than_an_exception() -> None:
    handle = _StubHandle({"Calc": [[1.0]]})

    vector = read_baseline(handle, RegionSpec(id="x", reference="Missing!A1:A9"))

    assert vector.unreadable
    assert vector.reason is NotReconciledReason.BASELINE_RANGE_UNREADABLE


def test_a_reference_with_no_sheet_is_unreadable() -> None:
    handle = _StubHandle({"Calc": [[1.0]]})

    assert read_baseline(handle, RegionSpec(id="x", reference="G2:G4")).unreadable


def test_an_unparseable_range_is_unreadable() -> None:
    handle = _StubHandle({"Calc": [[1.0]]})

    assert read_baseline(handle, RegionSpec(id="x", reference="Calc!not-a-range")).unreadable


def test_a_range_that_reads_back_empty_is_empty() -> None:
    handle = _StubHandle({"Calc": []})

    vector = read_baseline(handle, RegionSpec(id="x", reference="Calc!G2:G4"))

    assert vector.status == "empty"
    assert vector.reason is NotReconciledReason.BASELINE_RANGE_EMPTY


def test_a_read_that_raises_degrades_to_unreadable() -> None:
    class _Exploding(_StubHandle):
        def read_range(self, sheet: str, a1: str, **_: Any) -> list[list[Any]]:
            raise OSError("the archive went away")

    vector = read_baseline(_Exploding({"Calc": []}), RegionSpec(id="x", reference="Calc!G2:G4"))

    assert vector.unreadable


def test_a_block_is_flattened_row_major_with_its_cell_references() -> None:
    handle = _StubHandle({"Calc": [[1.0, 2.0], [3.0, 4.0]]})

    vector = read_baseline(handle, RegionSpec(id="x", reference="Calc!B2:C3"))

    assert vector.values == (1.0, 2.0, 3.0, 4.0)
    assert vector.cells == ("B2", "C2", "B3", "C3")


def test_error_literals_in_the_range_are_counted() -> None:
    vector = BaselineVector(
        spec_id="x", reference="Calc!A1:A3", sheet="Calc", values=(1.0, "#DIV/0!", "#N/A")
    )

    assert vector.error_count == 2


def test_every_region_is_read_and_keyed_by_its_id() -> None:
    handle = _StubHandle({"Calc": [[1.0]], "Output": [[2.0]]})
    specs = [
        RegionSpec(id="a", reference="Calc!A1"),
        RegionSpec(id="b", reference="Output!B2"),
    ]

    baselines = read_baselines(handle, specs)

    assert set(baselines) == {"a", "b"}
    assert baselines["b"].values == (2.0,)


# ── inferring regions from an analysis ──────────────────────────────────────


def test_regions_are_inferred_from_cached_formula_columns_and_named_by_their_header() -> None:
    handle = _StubHandle({"Calc": [["rwa"]]})
    analysis = make_analysis(
        operations=[
            make_operation(
                "calc_g2_g501",
                sheet="Calc",
                anchor="G2",
                cell_count=500,
                orientation="column",
                cached_values_present=True,
            )
        ]
    )

    specs = infer_regions(handle, analysis)

    assert len(specs) == 1
    assert specs[0].reference == "Calc!G2:G501"
    assert specs[0].column == "rwa"
    assert specs[0].label == "rwa"


def test_a_header_is_normalised_into_a_column_name() -> None:
    handle = _StubHandle({"Calc": [["Adjusted Notional (GBP)"]]})
    analysis = make_analysis(
        operations=[
            make_operation(
                "calc_e2_e9", sheet="Calc", anchor="E2", cell_count=8, cached_values_present=True
            )
        ]
    )

    assert infer_regions(handle, analysis)[0].column == "adjusted_notional_gbp"


def test_a_region_with_no_cached_values_is_never_proposed_as_a_baseline() -> None:
    handle = _StubHandle({"Calc": [["rwa"]]})
    analysis = make_analysis(
        operations=[
            make_operation("calc_g2_g501", sheet="Calc", anchor="G2", cached_values_present=False)
        ]
    )

    assert infer_regions(handle, analysis) == []


def test_single_cell_regions_are_left_out_of_inference() -> None:
    """A one-cell metric has no notebook column to match; declare it if you want it."""
    handle = _StubHandle({"Output": [["total"]]})
    analysis = make_analysis(
        operations=[
            make_operation(
                "output_b2",
                sheet="Output",
                anchor="B2",
                cell_count=1,
                orientation="single",
                cached_values_present=True,
            )
        ]
    )

    assert infer_regions(handle, analysis) == []


# ── declared specs ──────────────────────────────────────────────────────────


def test_dicts_become_specs_and_take_their_id_from_the_column_when_absent() -> None:
    specs = specs_from_mapping([{"reference": "Calc!G2:G9", "column": "rwa"}])

    assert specs[0].id == "rwa"


def test_a_dict_with_neither_id_nor_column_still_gets_a_stable_id() -> None:
    specs = specs_from_mapping([{"reference": "Calc!G2:G9"}])

    assert specs[0].id == "region_1"
