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
from kedge.analysis.model import LogicalOperation
from kedge.analysis.workbook import parse_a1_range
from kedge.reconcile.baseline import (
    BaselineVector,
    infer_regions,
    operation_ranges,
    operation_reference,
    read_baseline,
    read_baselines,
    region_cell_count,
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


class _GridHandle:
    """A handle that answers the range it was asked for, the way the real one does.

    `_StubHandle` returns the whole sheet whatever it is asked for, which is enough for a
    region that is one rectangle and actively misleading for one that is several: every
    piece would come back holding the same values, and a baseline that read the wrong cells
    would still look right. This one holds cells by reference and slices them, clamping to
    the sheet's last row and to `max_rows` exactly as `WorkbookHandle.read_range` does.
    """

    def __init__(self, sheets: dict[str, dict[str, Any]], *, last_row: int | None = None) -> None:
        self._cells: dict[str, dict[tuple[int, int], Any]] = {}
        for sheet, cells in sheets.items():
            grid: dict[tuple[int, int], Any] = {}
            for reference, value in cells.items():
                bounds = parse_a1_range(reference)
                assert bounds is not None, reference
                grid[(bounds[0], bounds[1])] = value
            self._cells[sheet] = grid
        self._last_row = last_row
        self.calls: list[tuple[str, str]] = []

    @property
    def sheet_names(self) -> list[str]:
        return list(self._cells)

    def read_range(self, sheet: str, a1: str, *, max_rows: int = 500, **_: Any) -> list[list[Any]]:
        self.calls.append((sheet, a1))
        grid = self._cells[sheet]
        bounds = parse_a1_range(a1)
        assert bounds is not None, a1
        min_row, min_col, max_row, max_col = bounds
        last = self._last_row if self._last_row is not None else max(row for row, _ in grid)
        max_row = min(max_row, last, min_row + max_rows - 1)
        if max_row < min_row:
            return []
        return [
            [grid.get((row, col)) for col in range(min_col, max_col + 1)]
            for row in range(min_row, max_row + 1)
        ]


#: A fee column broken by a subtotal row: E2:E5 and E7:E9 are client fees, E6 is their total.
#: The shape the bug was found on, in eight cells rather than eighty-four.
_BROKEN_COLUMN = {
    "Calc": {
        "E1": "fee",
        "E2": 10.0,
        "E3": 20.0,
        "E4": 30.0,
        "E5": 40.0,
        "E6": 100.0,  # the SUBTOTAL: in the rectangle, not in the operation
        "E7": 50.0,
        "E8": 60.0,
        "E9": 70.0,
    }
}


def _broken_operation(**overrides: Any) -> LogicalOperation:
    """The discontiguous fee column as the analyser records it."""
    fields: dict[str, Any] = {
        "sheet": "Calc",
        "anchor": "E2",
        "ranges": ["Calc!E2:E5", "Calc!E7:E9"],
        "cell_count": 7,
        "orientation": "column",
        "cached_values_present": True,
    }
    fields.update(overrides)
    return make_operation("calc_e2_e9", **fields)


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


# ── regions that are not one rectangle ──────────────────────────────────────


def test_a_contiguous_column_reads_its_own_cells_and_nothing_else() -> None:
    """The unchanged case, asserted against a handle that answers the range it was given."""
    handle = _GridHandle(_BROKEN_COLUMN)

    vector = read_baseline(handle, RegionSpec(id="fee", reference="Calc!E2:E5", cell_count=4))

    assert vector.cells == ("E2", "E3", "E4", "E5")
    assert vector.values == (10.0, 20.0, 30.0, 40.0)
    assert vector.status == "present"


def test_a_discontiguous_operation_is_labelled_by_the_rectangle_that_encloses_it() -> None:
    """There is no single range that is the operation and nothing else, and it says so."""
    assert operation_reference(_broken_operation()) == "Calc!E2:E9"
    assert operation_ranges(_broken_operation()) == ["Calc!E2:E5", "Calc!E7:E9"]


def test_a_contiguous_operation_keeps_its_reconstructed_rectangle_and_declares_no_ranges() -> None:
    operation = make_operation(
        "calc_g2_g501", sheet="Calc", anchor="G2", cell_count=500, orientation="column"
    )

    assert operation_reference(operation) == "Calc!G2:G501"
    assert operation_ranges(operation) == []


def test_a_discontiguous_region_reads_its_own_cells_and_not_the_rows_between_them() -> None:
    """The bug: the enclosing rectangle holds the subtotal and stops two rows short.

    Reading `Calc!E2:E8` would put the subtotal at the fifth position and shunt every later
    fee up one, so a correct conversion fails and an incorrect one can pass. Every value
    below is asserted, because a vector of the right length is exactly what the bug produced.
    """
    handle = _GridHandle(_BROKEN_COLUMN)
    spec = RegionSpec(
        id="fee",
        reference="Calc!E2:E9",
        ranges=["Calc!E2:E5", "Calc!E7:E9"],
        cell_count=7,
    )

    vector = read_baseline(handle, spec)

    assert vector.cells == ("E2", "E3", "E4", "E5", "E7", "E8", "E9")
    assert vector.values == (10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0)
    assert 100.0 not in vector.values  # the SUBTOTAL is not a client fee
    assert vector.status == "present"
    assert vector.cell_at(4) == "Calc!E7"


def test_an_inferred_region_carries_the_operations_ranges_and_its_cell_count() -> None:
    handle = _GridHandle(_BROKEN_COLUMN)
    analysis = make_analysis(operations=[_broken_operation()])

    specs = infer_regions(handle, analysis)

    assert len(specs) == 1
    assert specs[0].reference == "Calc!E2:E9"
    assert specs[0].ranges == ["Calc!E2:E5", "Calc!E7:E9"]
    assert specs[0].cell_count == 7
    assert specs[0].column == "fee"
    assert read_baseline(handle, specs[0]).values == (10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0)


def test_a_region_read_over_several_ranges_counts_its_cells_not_its_rectangle() -> None:
    spec = RegionSpec(
        id="fee", reference="Calc!E2:E9", ranges=["Calc!E2:E5", "Calc!E7:E9"], cell_count=7
    )

    assert region_cell_count(spec) == 7  # not the 8 cells of E2:E9
    assert region_cell_count(RegionSpec(id="x", reference="Calc!B2:C3")) == 4
    assert region_cell_count(RegionSpec(id="x", reference="Calc!not-a-range")) is None


# ── where no baseline can be built at all ───────────────────────────────────


def test_ranges_that_do_not_account_for_every_cell_degrade_rather_than_shorten() -> None:
    """The analyser renders at most twenty ranges; a region with more loses the rest.

    A short vector is not a smaller version of the right answer. It would be compared row
    against row from the top, so everything after the first missing piece lines up with the
    wrong cell -- which is the failure this whole module exists to refuse.
    """
    handle = _GridHandle(_BROKEN_COLUMN)
    spec = RegionSpec(
        id="fee",
        reference="Calc!E2:E9",
        ranges=["Calc!E2:E5"],  # the second piece never made it into the list
        cell_count=7,
    )

    vector = read_baseline(handle, spec)

    assert vector.status == "incomplete"
    assert vector.reason is NotReconciledReason.BASELINE_RANGE_INCOMPLETE
    assert vector.values == ()  # nothing is read, so nothing can be compared by accident
    assert "NOT a pass" in NotReconciledReason.BASELINE_RANGE_INCOMPLETE.explanation


def test_an_operation_with_more_pieces_than_the_analyser_records_is_never_read_as_a_rectangle() -> (
    None
):
    """End to end from the analysis: the ranges are short of the count, so the region stops."""
    handle = _GridHandle(_BROKEN_COLUMN)
    analysis = make_analysis(operations=[_broken_operation(cell_count=9)])

    spec = infer_regions(handle, analysis)[0]

    assert read_baseline(handle, spec).reason is NotReconciledReason.BASELINE_RANGE_INCOMPLETE


def test_ranges_that_overlap_are_unreadable_because_their_cells_have_no_order() -> None:
    handle = _GridHandle(_BROKEN_COLUMN)
    spec = RegionSpec(id="fee", reference="Calc!E2:E9", ranges=["Calc!E2:E5", "Calc!E4:E9"])

    assert read_baseline(handle, spec).reason is NotReconciledReason.BASELINE_RANGE_UNREADABLE


def test_a_region_spread_over_two_sheets_is_unreadable_because_a_baseline_is_one_vector() -> None:
    handle = _GridHandle({**_BROKEN_COLUMN, "Other": {"E2": 1.0}})
    spec = RegionSpec(id="fee", reference="Calc!E2:E9", ranges=["Calc!E2:E5", "Other!E2"])

    assert read_baseline(handle, spec).unreadable


def test_a_piece_that_reads_back_short_is_padded_so_the_pieces_after_it_stay_in_place() -> None:
    """A hole is a missing baseline, never a reason to slide the next piece up one row."""

    class _ShortHandle(_GridHandle):
        def read_range(self, sheet: str, a1: str, **kwargs: Any) -> list[list[Any]]:
            rows = super().read_range(sheet, a1, **kwargs)
            return rows[:-1] if a1 == "E2:E5" else rows

    handle = _ShortHandle(_BROKEN_COLUMN)
    spec = RegionSpec(
        id="fee", reference="Calc!E2:E9", ranges=["Calc!E2:E5", "Calc!E7:E9"], cell_count=7
    )

    vector = read_baseline(handle, spec)

    assert vector.cells == ("E2", "E3", "E4", "E5", "E7", "E8", "E9")
    assert vector.values == (10.0, 20.0, 30.0, None, 50.0, 60.0, 70.0)
    assert vector.status == "partial"
    assert vector.reason is NotReconciledReason.PARTIAL_CACHED_VALUES


def test_the_read_ceiling_bounds_the_region_rather_than_each_of_its_ranges() -> None:
    handle = _GridHandle(_BROKEN_COLUMN)
    spec = RegionSpec(
        id="fee", reference="Calc!E2:E9", ranges=["Calc!E2:E5", "Calc!E7:E9"], cell_count=7
    )

    vector = read_baseline(handle, spec, max_rows=5)

    assert vector.truncated
    assert vector.values == (10.0, 20.0, 30.0, 40.0, 50.0)
    assert vector.cells == ("E2", "E3", "E4", "E5", "E7")


# ── declared specs ──────────────────────────────────────────────────────────


def test_dicts_become_specs_and_take_their_id_from_the_column_when_absent() -> None:
    specs = specs_from_mapping([{"reference": "Calc!G2:G9", "column": "rwa"}])

    assert specs[0].id == "rwa"


def test_a_dict_with_neither_id_nor_column_still_gets_a_stable_id() -> None:
    specs = specs_from_mapping([{"reference": "Calc!G2:G9"}])

    assert specs[0].id == "region_1"
