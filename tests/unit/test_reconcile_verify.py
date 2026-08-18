"""Did the change do what the notebook said it would?

The invariant under test throughout is the one `kedge.reconcile` exists to hold, restated for
a comparison against a re-extract rather than against Excel's cache: **nothing to compare is
never a pass**. A green verification is a claim somebody acts on -- it says production now holds
what this notebook says it holds -- so every route to "cannot tell" has to land on
`NOT_RECONCILED` with a reason, and never on `PASSED`.

The other half is that the four ways of being wrong stay apart. A row that is present and
different, a row that never got updated, a row that got updated and should not have been, and a
key that is not a key are four different problems with four different fixes, and a report that
folded them into one number would be a worse tool than the two tabs side by side that it
replaces.
"""

from __future__ import annotations

import polars as pl
import pytest

from kedge.reconcile import ReconciliationStatus, Tolerance, verify

BEFORE = pl.DataFrame(
    {
        "trade_id": ["ACC-1", "ACC-2", "ACC-3"],
        "accrual_gbp": [100.00, 200.00, 300.00],
        "entity_id": ["E-04", "E-04", "E-07"],
    }
)


def frame(**columns: list) -> pl.DataFrame:
    return pl.DataFrame(columns)


# ── the happy path ───────────────────────────────────────────────────────────


def test_an_exact_match_verifies() -> None:
    report = verify(BEFORE, BEFORE, keys=["trade_id"])

    assert report.status is ReconciliationStatus.PASSED
    assert report.ok
    assert report.matched == 3
    assert "All 3 rows match" in report.summary_line()


def test_a_lazyframe_is_accepted_on_either_side() -> None:
    """Stage cells hand LazyFrames around; requiring a collect at the call site would leak."""
    assert verify(BEFORE.lazy(), BEFORE.lazy(), keys=["trade_id"]).ok


def test_only_the_shared_non_key_columns_are_compared_by_default() -> None:
    """A re-extract carrying an extra audit column must not fail every row."""
    actual = BEFORE.with_columns(pl.lit("svc_fin").alias("adjusted_by"))

    report = verify(BEFORE, actual, keys=["trade_id"])

    assert report.ok
    assert "adjusted_by" not in report.compared_columns


def test_the_columns_to_compare_can_be_named() -> None:
    actual = BEFORE.with_columns(pl.lit("E-99").alias("entity_id"))

    assert verify(BEFORE, actual, keys=["trade_id"], columns=["accrual_gbp"]).ok


# ── the four ways of being wrong ─────────────────────────────────────────────


def test_a_changed_value_is_a_break_naming_the_row_the_column_and_the_delta() -> None:
    actual = BEFORE.with_columns(
        pl.when(pl.col("trade_id") == "ACC-2")
        .then(205.00)
        .otherwise(pl.col("accrual_gbp"))
        .alias("accrual_gbp")
    )

    report = verify(BEFORE, actual, keys=["trade_id"])

    assert report.status is ReconciliationStatus.FAILED
    assert report.break_count == 1
    assert report.breaks[0].key == ("ACC-2",)
    assert report.breaks[0].column == "accrual_gbp"
    assert report.breaks[0].delta == pytest.approx(5.0)
    assert "+5" in report.breaks[0].render()


def test_a_row_the_update_never_reached_is_missing_not_a_break() -> None:
    """A narrower WHERE clause than the adjustment. Different problem, different remedy."""
    report = verify(BEFORE, BEFORE.filter(pl.col("trade_id") != "ACC-3"), keys=["trade_id"])

    assert report.missing == (("ACC-3",),)
    assert report.missing_count == 1
    assert report.break_count == 0


def test_a_row_nobody_predicted_is_unexpected() -> None:
    """The dangerous direction: a WHERE clause wider than the adjustment."""
    actual = pl.concat([BEFORE, frame(trade_id=["ACC-9"], accrual_gbp=[50.0], entity_id=["E-99"])])

    report = verify(BEFORE, actual, keys=["trade_id"])

    assert report.unexpected == (("ACC-9",),)
    assert not report.ok


def test_a_duplicate_key_is_not_reconciled_rather_than_compared_arbitrarily() -> None:
    duplicated = pl.concat([BEFORE, BEFORE.head(1)])

    report = verify(duplicated, BEFORE, keys=["trade_id"])

    assert report.status is ReconciliationStatus.NOT_RECONCILED
    assert "duplicate key" in report.reason


# ── nothing to compare is never a pass ───────────────────────────────────────


@pytest.mark.parametrize(
    ("predicted", "actual", "fragment"),
    [
        (BEFORE, BEFORE.head(0), "re-extract is empty"),
        (BEFORE.head(0), BEFORE, "prediction is empty"),
    ],
)
def test_an_empty_side_is_not_reconciled(
    predicted: pl.DataFrame, actual: pl.DataFrame, fragment: str
) -> None:
    report = verify(predicted, actual, keys=["trade_id"])

    assert report.status is ReconciliationStatus.NOT_RECONCILED
    assert fragment in report.reason
    assert not report.ok


def test_no_keys_at_all_is_not_reconciled() -> None:
    report = verify(BEFORE, BEFORE, keys=[])

    assert report.status is ReconciliationStatus.NOT_RECONCILED
    assert "no key columns" in report.reason


def test_a_key_the_re_extract_does_not_have_names_what_it_does_have() -> None:
    report = verify(BEFORE, BEFORE.drop("trade_id"), keys=["trade_id"])

    assert report.status is ReconciliationStatus.NOT_RECONCILED
    assert "accrual_gbp, entity_id" in report.reason


def test_two_frames_sharing_only_their_keys_are_not_reconciled() -> None:
    """Zero compared columns compares nothing, and reporting a pass for it would be a lie."""
    keys_only = BEFORE.select("trade_id")

    report = verify(keys_only, keys_only, keys=["trade_id"])

    assert report.status is ReconciliationStatus.NOT_RECONCILED
    assert "share no value columns" in report.reason


def test_a_not_reconciled_report_is_falsy() -> None:
    """`if report.status:` must not treat an unverifiable comparison as success (PLAN 6.2)."""
    assert not bool(verify(BEFORE, BEFORE.head(0), keys=["trade_id"]).status)


# ── nulls, tolerance and the eval's own case ─────────────────────────────────


def test_two_nulls_agree_and_a_null_against_a_value_does_not() -> None:
    """A re-extract that nulled a column is a break, however tempting "missing equals" is."""
    predicted = frame(trade_id=["A", "B"], amount=[None, 1.0])
    actual = frame(trade_id=["A", "B"], amount=[None, None])

    report = verify(predicted, actual, keys=["trade_id"])

    assert report.break_count == 1
    assert report.breaks[0].key == ("B",)
    assert report.breaks[0].delta is None


def test_the_evals_own_divergence_is_one_break_and_not_a_pass() -> None:
    """Excel predicted 0.00 from a blank cell; the warehouse kept the NULL.

    This is the single row the adjustment eval turns on, and the reason it is worth a test of
    its own is the failure mode: a notebook comparing totals agrees with the memo and signs off.
    """
    predicted = frame(trade_id=["ACC-00001", "ACC-00002"], accrual_gbp=[0.00, 215143.45])
    reextract = frame(trade_id=["ACC-00001", "ACC-00002"], accrual_gbp=[None, 215143.45])

    report = verify(predicted, reextract, keys=["trade_id"])

    assert report.status is ReconciliationStatus.FAILED
    assert report.break_count == 1
    assert report.breaks[0].key == ("ACC-00001",)


def test_a_penny_is_caught_at_the_default_tolerance() -> None:
    """The default is absolute, so a penny does not get forgiven at large magnitudes."""
    predicted = frame(trade_id=["A"], amount=[50_000_000.00])
    actual = frame(trade_id=["A"], amount=[50_000_000.01])

    assert not verify(predicted, actual, keys=["trade_id"]).ok


def test_a_widened_tolerance_is_carried_in_the_report() -> None:
    """The tolerance is part of the claim: passing at 1e-2 is a different statement from 1e-9."""
    predicted = frame(trade_id=["A"], amount=[100.00])
    actual = frame(trade_id=["A"], amount=[100.004])

    report = verify(predicted, actual, keys=["trade_id"], tolerance=Tolerance(absolute=0.01))

    assert report.ok
    assert report.tolerance.absolute == 0.01
    assert "0.01" in report.summary_line()


def test_a_compound_key_is_compared_as_a_whole() -> None:
    predicted = frame(entity=["E-04", "E-04"], trade=["A", "B"], amount=[1.0, 2.0])
    actual = frame(entity=["E-04", "E-04"], trade=["A", "B"], amount=[1.0, 9.0])

    report = verify(predicted, actual, keys=["entity", "trade"])

    assert report.breaks[0].key == ("E-04", "B")


def test_the_rendered_report_carries_all_three_kinds_of_disagreement() -> None:
    predicted = frame(trade_id=["A", "B", "C"], amount=[1.0, 2.0, 3.0])
    actual = frame(trade_id=["A", "B", "D"], amount=[1.0, 9.0, 4.0])

    rendered = verify(predicted, actual, keys=["trade_id"]).render()

    assert "Rows that differ" in rendered
    assert "Missing from the re-extract" in rendered
    assert "Present but not predicted" in rendered


def test_the_itemised_lists_are_capped_but_the_counts_are_not() -> None:
    """A report holding forty thousand break records gets truncated by something that says so."""
    size = 120
    predicted = frame(trade_id=[f"T{index}" for index in range(size)], amount=[1.0] * size)
    actual = frame(trade_id=[f"T{index}" for index in range(size)], amount=[2.0] * size)

    report = verify(predicted, actual, keys=["trade_id"])

    assert report.break_count == size
    assert len(report.breaks) == 50
    assert "and 70 more" in report.render()
