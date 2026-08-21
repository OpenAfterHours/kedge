"""The fee-billing-run workbook is still hard, and still hard in the ways it was built to be.

This eval's whole premise is that the workbook is *complex* -- see
``evals/proposals/fee_billing_run.md``. That makes its rot guard different in kind from
``test_evals_adjustment_signoff.py``, which pins figures. Here the thing that can rot silently
is the **structure**, and it can rot in a way no ordinary test would notice.

The mechanism is R1C1 normalisation, and it is the reason this file leads with a structural
band rather than a list of cells. A formula filled down a column and across thirty columns
normalises to one string, so thirty *identical* columns are one logical operation and thirty
*different* ones are thirty. A refactor that tidies ``WORKING_COLUMNS`` into something
uniform -- exactly the sort of change a reviewer would wave through as a simplification --
collapses a 49-operation workbook to single figures while leaving a file that opens fine,
analyses fine and grades fine against every other assertion in the suite. The eval would go on
passing and would have stopped measuring anything.

Both directions matter, hence a band rather than a floor. Too simple and the workbook stresses
nothing the existing corpus does not already stress. Too complex and it triages to ``stop``, at
which point it cannot be converted, so it cannot be graded on the quality of its conversion.
The measured position is recorded in the proposal and reproduced here.

This is not a corpus test: it asserts what the workbook *is*, not what the analyser should find
in general. See ``evals/README.md`` for why those are kept apart.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from kedge.analysis.analyse import analyse
from kedge.analysis.model import WorkbookAnalysis
from kedge.plan.propose import build_proposal_context
from kedge.plan.triage import TriageVerdict, complexity, triage

EVAL_DIR = Path(__file__).resolve().parents[2] / "evals" / "fee_billing_run"


def _load_generator() -> ModuleType:
    """Import this case's ``build_workbook`` under a name nothing else can claim.

    Every eval directory has a module of that name, and ``test_evals_adjustment_signoff.py``
    puts the *other* one into ``sys.modules`` as the bare name ``build_workbook``. Two modules
    with one name means whichever import ran first wins, so a plain ``import build_workbook``
    here silently binds to whichever test file pytest happened to collect first -- passing
    alone, failing in a full run, and taking the other eval's suite down with it when the order
    goes the other way. The first version of this file did exactly that.

    ``tests/fixtures/generate.py`` was renamed for this reason and the comment recording it
    sits at the top of ``evals/adjustment_signoff/build_workbook.py``. Loading from an explicit
    path under a qualified name is the version that does not depend on anybody else's import
    order, and it leaves the bare name free for the case that already uses it.
    """
    path = EVAL_DIR / "build_workbook.py"
    spec = importlib.util.spec_from_file_location("evals.fee_billing_run.build_workbook", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


evalgen = _load_generator()
WORKBOOK = EVAL_DIR / evalgen.WORKBOOK_NAME


@pytest.fixture(scope="module")
def analysis() -> WorkbookAnalysis:
    if not WORKBOOK.is_file():
        pytest.skip(f"{WORKBOOK.name} has not been generated")
    return analyse(WORKBOOK)


# =============================================================================
# THE STRUCTURAL BAND
# =============================================================================


def test_the_workbook_is_as_complex_as_the_eval_needs(analysis: WorkbookAnalysis) -> None:
    """The measured position, and the thing every other assertion here depends on."""
    low, high = evalgen.TARGET_OPERATIONS
    assert low <= len(analysis.operations) <= high, (
        f"{len(analysis.operations)} logical operations. This is the R1C1 collapse: check "
        f"whether WORKING_COLUMNS has been made uniform, or whether a column's references now "
        f"vary per row and so refuse to compress."
    )

    score = complexity(analysis)
    low, high = evalgen.TARGET_COMPLEXITY
    assert low <= score <= high, f"complexity {score:.3f}, band {evalgen.TARGET_COMPLEXITY}"

    patterns = {op.excel_pattern for op in analysis.operations}
    assert len(patterns) >= evalgen.TARGET_PATTERNS, sorted(p.value for p in patterns)


def test_the_workbook_can_still_be_converted(analysis: WorkbookAnalysis) -> None:
    """A workbook that triages to `stop` cannot be graded on its conversion.

    That is the ceiling on how hard this eval is allowed to get, and it is a real one: the
    corpus's own `hostile.xlsx` is over it.
    """
    result = triage(analysis)
    assert result.verdict is not TriageVerdict.STOP, result.blocker_lines()


def test_it_is_harder_than_anything_the_repository_has_converted(
    analysis: WorkbookAnalysis,
) -> None:
    """The point of the exercise, stated as an assertion.

    ``q2_accrual_adjustment.xlsx`` scores 0.368 and is the most complex workbook kedge has ever
    been asked to convert. If this one ever drops near it, the eval has stopped being a second
    data point and has become a second copy of the first.
    """
    assert complexity(analysis) > 0.55


# =============================================================================
# THE PLANTED DISCRIMINATIONS
# =============================================================================


def test_the_posting_column_is_a_dead_region_among_many(analysis: WorkbookAnalysis) -> None:
    """Discrimination 5, and the reason it is sharper here than in the first eval.

    `Post` must be a real operation with no consumer -- that is what makes dropping it
    plausible -- and it must be one of *many*, so that keeping it is a judgement rather than
    the only available answer to "did you keep the one dead region?".
    """
    carry = [op for op in analysis.operations if op.sheet == "Post"]
    assert len(carry) == 1, "the Post column should compress to exactly one operation"
    assert carry[0].downstream_ref_count == 0, "a manual carry has no consumer, by definition"

    dead = [f for f in analysis.findings if f.kind.value == "dead_region"]
    assert len(dead) >= 10, f"only {len(dead)} dead regions; the needle needs a haystack"


def test_the_posting_column_sorts_into_the_truncated_tail(analysis: WorkbookAnalysis) -> None:
    """Proposal section 2.1, as a regression test rather than an argument.

    `_operation_digest` ranks by fan-out, so every zero-fan-out region sorts last -- and a
    manual carry has zero fan-out *because* it is carried by a person. If this assertion ever
    fails it is good news: it means the ranking has learned to treat a generated-SQL region as
    something other than an abandoned column.
    """
    ranked = sorted(analysis.operations, key=lambda op: (-op.downstream_ref_count, op.id))
    carry = next(op for op in analysis.operations if op.sheet == "Post")
    position = [op.id for op in ranked].index(carry.id)
    assert position >= len(ranked) // 2, (
        "the posting column no longer sorts into the tail of the planner's context"
    )


def test_the_planner_sees_every_operation(analysis: WorkbookAnalysis) -> None:
    """The workbook is hard, not pathological.

    Past ``_MAX_OPERATIONS`` the digest silently drops its tail, and the tail is where the
    posting column lives -- so a workbook over the cap tests truncation rather than conversion
    quality. Those are different questions and the proposal keeps them apart: truncation is
    what a *variant* of this workbook is for.
    """
    context = build_proposal_context(analysis, triage(analysis))
    assert "operations_omitted" not in context, context.get("operations_omitted")


def test_the_tier_lookup_is_an_approximate_match(analysis: WorkbookAnalysis) -> None:
    """Discrimination 1: a banded rate table, which is `join_asof` and not a join."""
    assert any(op.excel_pattern.value == "vlookup_approx" for op in analysis.operations), (
        "the banded tier lookup has gone; it is the highest-value translation risk here"
    )


def test_the_overrides_are_reachable_and_reasoned(analysis: WorkbookAnalysis) -> None:
    """Discrimination 4: three typed-over fees, each carrying a written reason.

    The reasons are the part that matters. An override with no reason is a number, and a
    runbook cannot re-ask a question nobody wrote down.
    """
    from openpyxl import load_workbook

    sheet = load_workbook(WORKBOOK)["Overrides"]
    reasons = [sheet.cell(row, 4).value for row in (2, 3, 4)]
    assert all(isinstance(r, str) and len(r) > 30 for r in reasons), reasons
    assert len(set(reasons)) == 3, "three overrides, three distinct reasons"


def test_the_subtotals_sit_inside_the_allocation_grid(analysis: WorkbookAnalysis) -> None:
    """Discrimination 6: an embedded subtotal is not data."""
    assert any(
        op.sheet == "Allocation" and op.excel_pattern.value == "subtotal"
        for op in analysis.operations
    )


def test_the_dependency_order_is_not_the_tab_order(analysis: WorkbookAnalysis) -> None:
    """Discrimination 10: `Entity Map` is the eighth tab and the fourth tab depends on it."""
    names = [sheet.name for sheet in analysis.sheets]
    assert names.index("Entity Map") > names.index("Working")


def test_the_process_notes_survive_into_the_analysis(analysis: WorkbookAnalysis) -> None:
    """The briefing has to come from somewhere, and it must cite it."""
    text = " ".join(note.text for note in analysis.notes)
    assert "manual calculation" in text or "Overrides" in text


# =============================================================================
# THE RECONCILIATION BASELINE
# =============================================================================


def test_the_workbook_carries_a_reconciliation_baseline(analysis: WorkbookAnalysis) -> None:
    """Without cached values the whole reconciliation half of the rubric is ungradeable.

    ``infer_regions`` proposes nothing, every region degrades to "not reconciled", and triage
    reports a blocker -- so items about whether a translation reproduces the workbook cannot be
    scored at all, in either direction.
    """
    from kedge.analysis.workbook import open_workbook
    from kedge.reconcile.baseline import infer_regions

    assert analysis.cached_values.cached_present_count > 3_000
    assert all(op.cached_values_present for op in analysis.operations)

    with open_workbook(WORKBOOK) as handle:
        regions = infer_regions(handle, analysis)
    assert len(regions) > 30, f"only {len(regions)} reconcilable regions"


def test_the_allocation_tab_is_stale_on_purpose(analysis: WorkbookAnalysis) -> None:
    """Discrimination 9, and the only place in the eval where the cached values are *wrong*.

    ``Allocation`` is left on manual calculation, so its figures predate the three agreed
    overrides. A conversion that reconciles against them and adjusts itself until they match has
    adopted numbers Excel itself would disown. The correct answer is to declare the region not
    reproduced, with the reason, and report CHECKED WITH EXCEPTIONS.

    The staleness is recorded in the Sign-off prose and nowhere else, because no analyser can
    see it in the cells -- which is exactly why the briefing has to survive the conversion.
    """
    from openpyxl import load_workbook

    values = load_workbook(WORKBOOK, data_only=True)
    working = values["Working"]
    allocation = values["Allocation"]

    agreed = evalgen.LETTERS["agreed_fee"]
    net = evalgen.LETTERS["net_fee"]
    codes = {code for code, _agreed, _reason, _on in evalgen.OVERRIDES}

    live = 0.0
    stale_expected = 0.0
    for offset in range(evalgen.CLIENTS):
        row = evalgen.WORKING_FIRST_ROW + offset
        live += working[f"{agreed}{row}"].value
        stale_expected += working[f"{net}{row}"].value

    # A client row carries a code in column A; a subtotal row does not. Reading the sheet rather
    # than recomputing the layout keeps this a check on the file and not on the generator.
    banked = sum(
        row[4].value
        for row in allocation.iter_rows(min_row=2, max_col=5)
        if row[0].value is not None
    )
    assert abs(banked - stale_expected) < 0.05, "Allocation should hold the pre-override figures"
    assert abs(banked - live) > 1.0, "Allocation must disagree with Working, or nothing is stale"
    assert codes, "the overrides are what make the two differ"


def test_an_empty_string_result_reads_as_an_uncalculated_cell(
    analysis: WorkbookAnalysis,
) -> None:
    """A defect in kedge, pinned here because this workbook is the first thing to reach it.

    ``=IF(agreed=net,"","OVERRIDE")`` returns an empty string on the 81 rows with no override.
    Excel stores that as ``<c t="str"><f>...</f><v/></c>`` -- verified by driving Excel, not
    assumed -- and openpyxl's ``data_only`` view hands back ``None`` for it, which
    ``values.add_formula`` counts as *no cached value*.

    So a workbook that is fully calculated reports partial coverage and a verification blocker,
    and the blocker's remediation is unfollowable: it says "recalculate and re-save in Excel",
    which cannot help, because Excel is what wrote the file. The ``t="str"`` attribute is the
    signal that would tell the two apart, and it is discarded before kedge sees it.

    When that is fixed this assertion inverts, and the eval will have paid for itself once.
    """
    coverage = analysis.cached_values
    missing = coverage.formula_cell_count - coverage.cached_present_count
    empty_string_rows = evalgen.CLIENTS - len(evalgen.OVERRIDES)
    assert missing == empty_string_rows, (
        f"{missing} formula cells report no cached value; expected exactly the "
        f"{empty_string_rows} rows whose override flag is an empty string"
    )


# =============================================================================
# DETERMINISM
# =============================================================================


def test_generating_twice_produces_identical_bytes(tmp_path: Path) -> None:
    """A workbook nobody can regenerate is a workbook nobody can change."""
    first, second = tmp_path / "a.xlsx", tmp_path / "b.xlsx"
    evalgen.build(first)
    evalgen.build(second)
    assert first.read_bytes() == second.read_bytes()
