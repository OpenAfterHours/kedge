"""The eval harness grades correctly, and can actually fail.

Two halves, and the second is the one that matters.

The first is the positive control: the reference conversion and the reference plan score full
marks. That proves the harness runs, that the graders read the right names, and that a good
notebook is recognised as one.

The second is a set of **negative controls**. A grader that has only ever been watched pass is a
grader whose polarity nobody has checked -- invert the comparison, key it to the wrong column,
compare a frame with itself, and it still reports green on the reference. So each defect this
eval exists to catch is injected into a copy of the reference notebook, one at a time, and the
test asserts that *that* item fails and that the injection did not knock over anything else.
The mutations are textual and deliberately small, because a mutation big enough to break the run
would prove nothing beyond "the notebook stopped".

The rubric is checked against the graders in both directions. An id in ``expected.yaml`` with no
grader is allowed at runtime -- a rubric may describe something nobody has automated -- but a
*grader* with no rubric entry is not, because that is a check whose reason was never written
down.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

EVAL_ROOT = Path(__file__).resolve().parents[2] / "evals"
if str(EVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(EVAL_ROOT))

from adjustment_signoff import case as adjustment_case  # noqa: E402
from harness.grade import grade, load_plan  # noqa: E402
from harness.model import Outcome  # noqa: E402

PLAN_PATH = adjustment_case.CASE_DIR / "plan.yaml"


@pytest.fixture(scope="module")
def rubric() -> dict[str, Any]:
    return yaml.safe_load(adjustment_case.RUBRIC.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def reference_report() -> Any:
    """One full run of the reference conversion, shared: it drives a whole notebook."""
    return grade(
        adjustment_case,
        notebook=adjustment_case.REFERENCE_NOTEBOOK,
        plan=load_plan(PLAN_PATH),
    )


def outcome_of(report: Any, item_id: str) -> Outcome:
    for tier in report.tiers:
        for item in tier.items:
            if item.id == item_id:
                return item.outcome
    msg = f"no item {item_id!r} in the report"
    raise AssertionError(msg)


def detail_of(report: Any, item_id: str) -> str:
    for tier in report.tiers:
        for item in tier.items:
            if item.id == item_id:
                return item.detail
    return ""


def mutated(tmp_path: Path, *replacements: tuple[str, str]) -> Path:
    """A copy of the reference notebook with one defect injected."""
    target = tmp_path / "mutated_notebook.py"
    source = adjustment_case.REFERENCE_NOTEBOOK.read_text(encoding="utf-8")
    for old, new in replacements:
        assert old in source, f"mutation anchor not found: {old!r}"
        source = source.replace(old, new)
    target.write_text(source, encoding="utf-8")
    shutil.copy(adjustment_case.WORKBOOK, tmp_path / adjustment_case.WORKBOOK.name)
    return target


def grade_mutant(notebook: Path) -> Any:
    return grade(adjustment_case, notebook=notebook, plan=load_plan(PLAN_PATH))


# ── the rubric and the graders agree ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("tier", "graders"), [("deterministic", "DETERMINISTIC"), ("structural", "STRUCTURAL")]
)
def test_every_grader_has_a_rubric_entry(rubric: dict[str, Any], tier: str, graders: str) -> None:
    """The direction that is not allowed to be loose: a check nobody wrote the reason for."""
    declared = {entry["id"] for entry in rubric[tier]}
    implemented = set(getattr(adjustment_case, graders))

    assert implemented <= declared, f"graders with no rubric entry: {implemented - declared}"


@pytest.mark.parametrize(
    ("tier", "graders"), [("deterministic", "DETERMINISTIC"), ("structural", "STRUCTURAL")]
)
def test_every_rubric_item_has_a_grader(rubric: dict[str, Any], tier: str, graders: str) -> None:
    declared = {entry["id"] for entry in rubric[tier]}
    implemented = set(getattr(adjustment_case, graders))

    assert declared <= implemented, f"rubric items with no grader: {declared - implemented}"


# ── positive control ─────────────────────────────────────────────────────────


def test_the_reference_conversion_scores_full_marks(reference_report: Any) -> None:
    """If this ever goes red, read the failures before touching a grader.

    It is the only assertion here that says the harness works at all -- everything else says a
    particular way of being wrong is caught.
    """
    assert reference_report.ok, "\n".join(
        f"{item.id}: {item.detail}" for item in reference_report.failures
    )
    assert reference_report.earned == reference_report.available
    assert reference_report.available > 30


def test_only_the_knowledge_pack_item_skips_on_the_reference(reference_report: Any) -> None:
    """Every other skip means an item silently stopped being measured."""
    assert [item.id for item in reference_report.skipped] == ["consults_the_knowledge_pack"]


def test_the_headline_shows_the_denominator(reference_report: Any) -> None:
    """A bare percentage over a partly-skipped rubric is the number this must not print."""
    headline = reference_report.headline()

    assert f"{reference_report.earned}/{reference_report.available}" in headline
    assert "1 skipped" in headline


# ── negative controls, one defect at a time ──────────────────────────────────


def test_a_bare_round_fails_the_penny_check(tmp_path: Path) -> None:
    """The four rows that need Excel's 15-significant-digit collapse, and nothing else."""
    report = grade_mutant(mutated(tmp_path, (".xl.round(2)", ".round(2)")))

    assert outcome_of(report, "totals_to_the_penny") is Outcome.FAIL
    assert "15-significant-digit" in detail_of(report, "totals_to_the_penny")
    assert outcome_of(report, "ran_to_completion") is Outcome.PASS


def test_letting_the_null_propagate_fails_the_null_check(tmp_path: Path) -> None:
    """polars' answer is null; Excel's is zero, and the workbook's cache holds zero."""
    report = grade_mutant(
        mutated(tmp_path, ('pl.col("accrual_gbp").xl.empty_as_zero()', 'pl.col("accrual_gbp")'))
    )

    assert outcome_of(report, "null_is_not_zero") is Outcome.FAIL
    assert "empty_as_zero" in detail_of(report, "null_is_not_zero")


def test_forgetting_the_cancelled_filter_fails_the_row_count(tmp_path: Path) -> None:
    # Anchored on `pre_adjustment.filter(` so only the `adjust` cell moves. The `verification`
    # cell restates the same predicate, and mutating both would move the goalposts as well as
    # the shot -- 80 rows predicted and 80 compared still verifies.
    scoped = (
        "pre_adjustment.filter(\n"
        '            pl.col("entity_id").is_in(ENTITIES_IN_SCOPE)'
        ' & (pl.col("status") != "CANCELLED")\n        )'
    )
    report = grade_mutant(
        mutated(
            tmp_path,
            (
                scoped,
                'pre_adjustment.filter(\n            pl.col("entity_id").is_in(ENTITIES_IN_SCOPE)'
                "\n        )",
            ),
        )
    )

    assert outcome_of(report, "cancelled_rows_excluded") is Outcome.FAIL
    assert "80" in detail_of(report, "cancelled_rows_excluded")


def test_concatenating_the_sql_fails_the_execution_check(tmp_path: Path) -> None:
    """The workbook's own approach, reproduced faithfully, and it does not parse.

    ``O'Brien & Partners`` is the row. This is the one place the eval rewards departing from
    Excel, so it is the one place a mutation has to prove the grader is not simply agreeing with
    whatever it is given.
    """
    naive = """update_statement = "\\n".join(
        "UPDATE fin.accruals SET accrual_gbp = %.2f, adjustment_note = '%s' WHERE trade_id = '%s';"
        % (row["accrual_gbp_after"], row["adjustment_note"], row["trade_id"])
        for row in _rows.iter_rows(named=True)
    )"""
    original_start = "    update_statement = kedge.sql.script("
    source = adjustment_case.REFERENCE_NOTEBOOK.read_text(encoding="utf-8")
    start = source.index(original_start)
    end = source.index("    mo.vstack(", start)
    target = tmp_path / "mutated_notebook.py"
    target.write_text(
        source[:start] + "    " + naive + "\n" + source[end:],
        encoding="utf-8",
    )

    report = grade_mutant(target)

    assert outcome_of(report, "generated_sql_is_valid") is Outcome.FAIL
    assert adjustment_case.APOSTROPHE_TRADE in detail_of(report, "generated_sql_is_valid")


def test_verifying_a_frame_against_itself_fails_the_verification_check(tmp_path: Path) -> None:
    """The dangerous direction: a green panel claiming production holds what the notebook says.

    Comparing the prediction with itself is the shape of every accidental self-verification, and
    it is exactly what a grader keyed to "did it pass" rather than "what did it find" would wave
    through.
    """
    report = grade_mutant(
        mutated(
            tmp_path,
            (
                "kedge.reconcile.verify(_predicted, _actual",
                "kedge.reconcile.verify(_predicted, _predicted",
            ),
        )
    )

    assert outcome_of(report, "verification_finds_exactly_one_break") is Outcome.FAIL
    assert "signed claim" in detail_of(report, "verification_finds_exactly_one_break")


def test_carrying_the_memos_stale_figures_forward_fails(tmp_path: Path) -> None:
    """The impact summary was signed and is wrong. Reproducing it reproduces that."""
    stale = yaml.safe_load(adjustment_case.RUBRIC.read_text(encoding="utf-8"))["facts"]
    report = grade_mutant(
        mutated(
            tmp_path,
            (
                '"movement": float(adjust_totals["uplift"][0]),',
                f'"movement": {stale["stale_movement"]},',
            ),
        )
    )

    assert outcome_of(report, "does_not_trust_the_impact_summary") is Outcome.FAIL
    assert "stale" in detail_of(report, "does_not_trust_the_impact_summary")


def test_a_notebook_that_stops_fails_completion_and_skips_the_rest(tmp_path: Path) -> None:
    """One problem reported once. The items about cells that never ran are skips, not failures."""
    target = tmp_path / "stops.py"
    source = adjustment_case.REFERENCE_NOTEBOOK.read_text(encoding="utf-8")
    target.write_text(
        source.replace(
            "pre_adjustment_pick = mo.ui.file_browser(", "unwatched_pick = mo.ui.file_browser("
        ),
        encoding="utf-8",
    )

    report = grade_mutant(target)

    assert outcome_of(report, "ran_to_completion") is Outcome.FAIL
    assert outcome_of(report, "totals_to_the_penny") is Outcome.SKIP
    assert outcome_of(report, "verification_finds_exactly_one_break") is Outcome.SKIP
    # The structural tier grades the plan, which is unaffected by the notebook stopping.
    assert outcome_of(report, "hands_over_rather_than_pretends") is Outcome.PASS


# ── negative controls on the plan ────────────────────────────────────────────


def test_dropping_the_sql_column_fails_the_sharpest_structural_item() -> None:
    """A plausible, well-reasoned, catastrophic edit: the analyser calls that region dead."""
    from kedge.plan.model import DroppedRange

    plan = load_plan(PLAN_PATH)
    dropped = plan.model_copy(
        update={
            "dropped": [
                *plan.dropped,
                DroppedRange(
                    range="Adjustment!G17:G92",
                    reason="dead region: nothing in the workbook reads it",
                    acknowledged=True,
                    accepted=True,
                ),
            ]
        }
    )

    report = grade(adjustment_case, notebook=adjustment_case.REFERENCE_NOTEBOOK, plan=dropped)

    assert outcome_of(report, "does_not_drop_the_sql_column") is Outcome.FAIL
    assert "clipboard" in detail_of(report, "does_not_drop_the_sql_column")


def test_a_plan_with_no_handoffs_fails_the_handover_item() -> None:
    plan = load_plan(PLAN_PATH)
    stripped = plan.model_copy(
        update={"stages": [stage for stage in plan.stages if not stage.is_handoff]}
    )

    report = grade(adjustment_case, notebook=adjustment_case.REFERENCE_NOTEBOOK, plan=stripped)

    assert outcome_of(report, "hands_over_rather_than_pretends") is Outcome.FAIL


def test_no_plan_skips_the_structural_tier_rather_than_failing_it() -> None:
    """A tier nobody supplied an input for is unmeasured, not failed."""
    report = grade(adjustment_case, notebook=adjustment_case.REFERENCE_NOTEBOOK, plan=None)

    structural = next(tier for tier in report.tiers if tier.name == "structural")
    assert all(item.outcome is not Outcome.FAIL for item in structural.items), (
        "a missing plan must not be reported as a failed conversion"
    )
    assert report.ok
