"""The dependency graph: region-to-region edges, cell-level cycles, and fan-out.

Edges are region-to-region because that is the granularity the process plan is written at.
Cycles are hunted at cell level because that is where Excel actually forms them — a column
of running totals references its own region on every row without being circular at all, so
a region-level self-loop proves nothing and only the cells can settle it.

The work is kept bounded in two ways. A region's references are swept analytically rather
than per cell: parsing the formula at the region's anchor and again at its far corner gives
the two extremes of every relative reference, and their bounding box covers everything the
fill touches. Cell-level expansion then runs only over the regions that the region-level
graph says might be cyclic, which on a healthy workbook is none of them.

References:
- PLAN.md §2.1 (regions), §M1 (circular references, dead regions).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kedge.analysis.formulas import RefExtent, parse_formula, to_a1
from kedge.analysis.model import DependencyGraph
from kedge.analysis.values import cell_ref

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from kedge.analysis.regions import DetectedRegion
    from kedge.analysis.values import SheetScan

logger = logging.getLogger(__name__)

__all__ = ["build_graph"]

_MAX_CYCLE_REPORTS = 10
_MAX_CYCLE_CELLS = 20_000
_MAX_EXTENT_CELLS = 4_096
_CYCLE_WORK_BUDGET = 2_000_000


# =============================================================================
# EXTENT SWEEP
# =============================================================================


def _swept_extents(
    region: DetectedRegion,
    *,
    names: frozenset[str],
    known_sheets: frozenset[str],
) -> list[RefExtent]:
    """Every cell the region's references touch, across the whole fill.

    Relative offsets move linearly with the cell holding them, so the union of the
    references resolved at the region's two extreme corners bounds the lot.
    """
    base = list(region.parsed.extents)
    if region.operation.cell_count == 1:
        return base

    far = parse_formula(
        to_a1(region.operation.r1c1, region.max_row, region.max_col),
        region.max_row,
        region.max_col,
        sheet=region.operation.sheet,
        names=names,
        known_sheets=known_sheets,
    ).extents
    if len(far) != len(base):
        logger.debug("region %s: reference count differs across the fill", region.operation.id)
        return base

    swept: list[RefExtent] = []
    for first, second in zip(base, far, strict=True):
        swept.append(
            RefExtent(
                sheet=first.sheet,
                min_row=min(first.min_row, second.min_row),
                min_col=min(first.min_col, second.min_col),
                max_row=max(first.max_row, second.max_row),
                max_col=max(first.max_col, second.max_col),
                is_external=first.is_external,
            )
        )
    return swept


# =============================================================================
# REGION GRAPH
# =============================================================================


def _edges(
    regions: Sequence[DetectedRegion],
    *,
    names: frozenset[str],
    known_sheets: frozenset[str],
) -> list[tuple[str, str]]:
    """Build (upstream, downstream) region pairs from the swept references."""
    by_sheet: dict[str, list[DetectedRegion]] = {}
    for region in regions:
        by_sheet.setdefault(region.operation.sheet, []).append(region)

    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []
    for region in regions:
        downstream = region.operation.id
        for extent in _swept_extents(region, names=names, known_sheets=known_sheets):
            if extent.is_external or extent.sheet is None:
                continue
            for candidate in by_sheet.get(extent.sheet, ()):
                if candidate.operation.id == downstream:
                    continue
                if not extent.overlaps(
                    candidate.min_row, candidate.min_col, candidate.max_row, candidate.max_col
                ):
                    continue
                edge = (candidate.operation.id, downstream)
                if edge not in seen:
                    seen.add(edge)
                    ordered.append(edge)
    return ordered


def _cyclic_core(nodes: list[str], edges: list[tuple[str, str]]) -> set[str]:
    """The nodes left once sources and sinks are peeled away — everything on a cycle."""
    remaining = set(nodes)
    outgoing: dict[str, set[str]] = {node: set() for node in nodes}
    incoming: dict[str, set[str]] = {node: set() for node in nodes}
    for upstream, downstream in edges:
        outgoing[upstream].add(downstream)
        incoming[downstream].add(upstream)

    changed = True
    while changed:
        changed = False
        for node in list(remaining):
            live_in = incoming[node] & remaining
            live_out = outgoing[node] & remaining
            if not live_in or not live_out:
                remaining.discard(node)
                changed = True
    return remaining


def _topological_order(nodes: list[str], edges: list[tuple[str, str]]) -> list[str] | None:
    """Kahn's algorithm. None when the graph has a cycle."""
    in_degree = dict.fromkeys(nodes, 0)
    outgoing: dict[str, list[str]] = {node: [] for node in nodes}
    for upstream, downstream in edges:
        outgoing[upstream].append(downstream)
        in_degree[downstream] += 1

    ready = [node for node in nodes if in_degree[node] == 0]
    order: list[str] = []
    while ready:
        node = ready.pop()
        order.append(node)
        for downstream in outgoing[node]:
            in_degree[downstream] -= 1
            if in_degree[downstream] == 0:
                ready.append(downstream)
    return order if len(order) == len(nodes) else None


# =============================================================================
# CELL-LEVEL CYCLES
# =============================================================================


def _cell_dependencies(
    candidates: dict[tuple[str, int, int], str],
    scans: Mapping[str, SheetScan],
    *,
    names: frozenset[str],
    known_sheets: frozenset[str],
) -> dict[tuple[str, int, int], list[tuple[str, int, int]]]:
    """Expand a bounded set of cells into a cell-level dependency map."""
    dependencies: dict[tuple[str, int, int], list[tuple[str, int, int]]] = {}
    by_sheet: dict[str, list[tuple[str, int, int]]] = {}
    for key in candidates:
        by_sheet.setdefault(key[0], []).append(key)

    budget = _CYCLE_WORK_BUDGET
    for key, r1c1 in candidates.items():
        sheet, row, col = key
        parsed = parse_formula(
            to_a1(r1c1, row, col), row, col, sheet=sheet, names=names, known_sheets=known_sheets
        )
        upstream: list[tuple[str, int, int]] = []
        for extent in parsed.extents:
            if extent.is_external or extent.sheet is None or extent.sheet not in scans:
                continue
            if extent.cell_count <= _MAX_EXTENT_CELLS:
                for target_row in range(extent.min_row, extent.max_row + 1):
                    for target_col in range(extent.min_col, extent.max_col + 1):
                        budget -= 1
                        target = (extent.sheet, target_row, target_col)
                        if target in candidates:
                            upstream.append(target)
            else:
                for target in by_sheet.get(extent.sheet, ()):
                    budget -= 1
                    if extent.overlaps(target[1], target[2], target[1], target[2]):
                        upstream.append(target)
            if budget <= 0:
                logger.warning(
                    "cell-level cycle expansion exhausted its budget; results may be partial"
                )
                dependencies[key] = upstream
                return dependencies
        dependencies[key] = upstream
    return dependencies


def _find_cycles(
    dependencies: dict[tuple[str, int, int], list[tuple[str, int, int]]],
) -> list[list[str]]:
    """Depth-first search for cycles, reported as lists of sheet-qualified A1 refs."""
    white, grey, black = 0, 1, 2
    colour = dict.fromkeys(dependencies, white)
    cycles: list[list[str]] = []
    seen: set[frozenset[tuple[str, int, int]]] = set()

    for start in dependencies:
        if colour[start] != white:
            continue
        stack: list[tuple[tuple[str, int, int], int]] = [(start, 0)]
        path: list[tuple[str, int, int]] = [start]
        colour[start] = grey
        while stack:
            node, index = stack[-1]
            neighbours = dependencies.get(node, ())
            if index >= len(neighbours):
                stack.pop()
                colour[node] = black
                path.pop()
                continue
            stack[-1] = (node, index + 1)
            neighbour = neighbours[index]
            if neighbour not in colour:
                continue
            if colour[neighbour] == grey:
                position = path.index(neighbour)
                cycle = path[position:]
                signature = frozenset(cycle)
                if signature not in seen:
                    seen.add(signature)
                    cycles.append([f"{sheet}!{cell_ref(row, col)}" for sheet, row, col in cycle])
                    if len(cycles) >= _MAX_CYCLE_REPORTS:
                        return cycles
                continue
            if colour[neighbour] == white:
                colour[neighbour] = grey
                path.append(neighbour)
                stack.append((neighbour, 0))
    return cycles


def _cycles(
    regions: Sequence[DetectedRegion],
    scans: Mapping[str, SheetScan],
    core: set[str],
    *,
    names: frozenset[str],
    known_sheets: frozenset[str],
) -> list[list[str]]:
    """Confirm or dismiss the region-level cyclic core at cell level."""
    if not core:
        return []

    candidates: dict[tuple[str, int, int], str] = {}
    for region in regions:
        if region.operation.id not in core:
            continue
        sheet = region.operation.sheet
        scan = scans.get(sheet)
        if scan is None:
            continue
        for row, col in region.coordinates:
            candidates[(sheet, row, col)] = region.operation.r1c1
            if len(candidates) >= _MAX_CYCLE_CELLS:
                logger.warning(
                    "cycle detection capped at %d cells; a cycle outside that set may be missed",
                    _MAX_CYCLE_CELLS,
                )
                break
        if len(candidates) >= _MAX_CYCLE_CELLS:
            break

    dependencies = _cell_dependencies(candidates, scans, names=names, known_sheets=known_sheets)
    return _find_cycles(dependencies)


# =============================================================================
# ENTRY POINT
# =============================================================================


def build_graph(
    regions: Sequence[DetectedRegion],
    scans: Mapping[str, SheetScan],
    *,
    names: frozenset[str] = frozenset(),
    known_sheets: frozenset[str] = frozenset(),
) -> DependencyGraph:
    """Build the region dependency graph, with cell-level cycles and fan-out.

    Args:
        regions: Every region in the workbook.
        scans: Sheet name to its scan, used when expanding cycles to cell level.
        names: Defined names in scope, upper-cased.
        known_sheets: Sheet names in the workbook.

    Returns:
        A :class:`~kedge.analysis.model.DependencyGraph`. ``topological_order`` is None when
        the region graph contains a cycle, whether or not it was confirmed at cell level.
    """
    nodes = [region.operation.id for region in regions]
    edges = _edges(regions, names=names, known_sheets=known_sheets)

    fan_out = dict.fromkeys(nodes, 0)
    fan_in = dict.fromkeys(nodes, 0)
    for upstream, downstream in edges:
        fan_out[upstream] += 1
        fan_in[downstream] += 1

    core = _cyclic_core(nodes, edges)
    cycles = _cycles(regions, scans, core, names=names, known_sheets=known_sheets)
    if core and not cycles:
        logger.debug(
            "region-level cycle among %d regions was not confirmed at cell level", len(core)
        )

    graph = DependencyGraph(
        nodes=nodes,
        edges=edges,
        cycles=cycles,
        fan_out=fan_out,
        topological_order=_topological_order(nodes, edges),
        roots=[node for node in nodes if fan_in[node] == 0],
        leaves=[node for node in nodes if fan_out[node] == 0],
    )
    logger.info(
        "dependency graph: %d regions, %d edges, %d cycles", len(nodes), len(edges), len(cycles)
    )
    return graph
