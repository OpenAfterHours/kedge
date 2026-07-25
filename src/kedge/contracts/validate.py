"""Validate a hand-in against its contract, and report the result structurally.

The contract cell is the first thing that runs after ingestion, so this is the gate every
downstream number passes through. Three principles shape how it behaves.

**Every check runs.** A missing column does not stop the row-count check, and a failed dtype
does not stop the uniqueness check. Somebody correcting a hand-in wants the whole list, not
the first problem in file order followed by four more rounds of the same conversation.

**A check that cannot run says so.** When a contracted column is absent, its dtype and
nullability checks report ``SKIP`` rather than ``FAIL``. Two reports of the same underlying
problem is noise, and a skipped check makes it obvious that the column itself is the issue.

**Nothing is a bare boolean.** Each result names the check, the column, what was expected and
what was found, so the report renders identically as a notebook panel, as CLI output, and as
JSON in an audit bundle.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import polars as pl

from kedge.contracts.model import (
    CheckResult,
    CheckStatus,
    ChecksumMode,
    ColumnContract,
    Contract,
    ValidationReport,
)
from kedge.ingest.drift import Layout, dtype_name, read_data
from kedge.ingest.model import HandIn, utcnow
from kedge.ingest.store import hash_file

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["validate", "validate_frame", "validate_path"]

_INTEGER_DTYPES = frozenset(
    {
        "Int8",
        "Int16",
        "Int32",
        "Int64",
        "Int128",
        "UInt8",
        "UInt16",
        "UInt32",
        "UInt64",
    }
)
_FLOAT_DTYPES = frozenset({"Float32", "Float64", "Decimal"})
_MAX_LISTED_VALUES = 5


# =============================================================================
# DTYPE COMPATIBILITY
# =============================================================================


def _dtype_satisfies(actual: str, expected: str) -> bool:
    """Whether an observed dtype meets a contracted one.

    Exact by default. Two widenings are permitted, both of which are safe and both of which
    would otherwise produce a failure nobody wants to read: an integer column satisfies a
    ``Float64`` expectation, because a whole-numbered month is a real and harmless
    occurrence; and any dtype satisfies the wildcard ``Any``.

    The widening that is emphatically *not* permitted is text where a number was expected --
    that is the silent join failure in PLAN 2.6 and it must fail.
    """
    if expected in ("Any", "any", "*"):
        return True
    if actual == expected:
        return True
    return actual in _INTEGER_DTYPES and expected in _FLOAT_DTYPES


# =============================================================================
# INDIVIDUAL CHECKS
# =============================================================================


def _checksum_check(contract: Contract, handin_sha256: str) -> CheckResult | None:
    if contract.checksum is None or contract.checksum_mode is ChecksumMode.IGNORE:
        return None
    matched = contract.checksum == handin_sha256
    if matched:
        return CheckResult(
            check="checksum",
            status=CheckStatus.PASS,
            message="the hand-in is byte-for-byte the file this contract records.",
            expected=contract.checksum[:12],
            actual=handin_sha256[:12],
        )
    require = contract.checksum_mode is ChecksumMode.REQUIRE
    return CheckResult(
        check="checksum",
        status=CheckStatus.FAIL if require else CheckStatus.WARN,
        message=(
            "this is not the file the contract was drafted from."
            if not require
            else "checksum_mode is 'require' and this is a different file."
        ),
        expected=contract.checksum[:12],
        actual=handin_sha256[:12],
        detail=(
            None
            if require
            else "Expected for a monthly hand-in. Set checksum_mode: require only for a "
            "fixed reference file."
        ),
    )


def _presence_checks(contract: Contract, present: list[str]) -> list[CheckResult]:
    results: list[CheckResult] = []
    available = set(present)
    missing = [c.name for c in contract.columns if c.required and c.name not in available]
    results.append(
        CheckResult(
            check="columns_present",
            status=CheckStatus.FAIL if missing else CheckStatus.PASS,
            message=(
                f"required column(s) absent: {', '.join(missing)}."
                if missing
                else "every required column is present."
            ),
            expected=", ".join(c.name for c in contract.columns if c.required) or None,
            actual=", ".join(present) or None,
            detail=(
                "Check the drift report above -- a column reported as missing here has "
                "usually been renamed, not removed."
                if missing
                else None
            ),
        )
    )

    contracted = set(contract.column_names)
    extra = [name for name in present if name not in contracted]
    if extra:
        results.append(
            CheckResult(
                check="no_extra_columns",
                status=CheckStatus.PASS if contract.allow_extra_columns else CheckStatus.FAIL,
                message=(
                    f"column(s) not in the contract: {', '.join(extra)}."
                    + ("" if contract.allow_extra_columns else " allow_extra_columns is false.")
                ),
                actual=", ".join(extra),
            )
        )

    if contract.require_column_order:
        expected_order = [c.name for c in contract.columns if c.name in set(present)]
        actual_order = [name for name in present if name in contracted]
        results.append(
            CheckResult(
                check="column_order",
                status=(CheckStatus.PASS if expected_order == actual_order else CheckStatus.FAIL),
                message=(
                    "columns are in the contracted order."
                    if expected_order == actual_order
                    else "columns are not in the contracted order."
                ),
                expected=", ".join(expected_order),
                actual=", ".join(actual_order),
            )
        )
    return results


def _row_count_check(contract: Contract, height: int) -> CheckResult | None:
    if contract.min_rows is None and contract.max_rows is None:
        return None
    low = contract.min_rows
    high = contract.max_rows
    within = (low is None or height >= low) and (high is None or height <= high)
    bounds = f"{'-' if low is None else low} to {'-' if high is None else high}"
    return CheckResult(
        check="row_count",
        status=CheckStatus.PASS if within else CheckStatus.FAIL,
        message=(
            f"{height:,} rows, within the contracted bounds."
            if within
            else f"{height:,} rows is outside the contracted bounds of {bounds}."
        ),
        expected=bounds,
        actual=f"{height:,}",
        detail=(
            None
            if within
            else "An unexpected row count usually means the extract ran for the wrong period."
        ),
    )


def _skipped(column: ColumnContract, check: str) -> CheckResult:
    return CheckResult(
        check=check,
        status=CheckStatus.SKIP,
        column=column.name,
        message=f"column '{column.name}' is not present, so this could not be checked.",
    )


def _dtype_check(column: ColumnContract, actual: str) -> CheckResult:
    ok = _dtype_satisfies(actual, column.dtype)
    text_for_number = actual == "String" and column.dtype in (_FLOAT_DTYPES | _INTEGER_DTYPES)
    return CheckResult(
        check="dtype",
        status=CheckStatus.PASS if ok else CheckStatus.FAIL,
        column=column.name,
        expected=column.dtype,
        actual=actual,
        message=(
            f"column '{column.name}' is {actual}."
            if ok
            else f"column '{column.name}' should be {column.dtype} but arrived as {actual}."
        ),
        detail=(
            "A number arriving as text is the silent join-failure cause in PLAN 2.6: the key "
            "matches nothing, rows drop out, and the total is simply smaller. Cast before "
            "joining."
            if text_for_number
            else None
        ),
    )


def _nullability_check(column: ColumnContract, series: pl.Series) -> CheckResult | None:
    if column.nullable:
        return None
    nulls = int(series.null_count())
    return CheckResult(
        check="not_null",
        status=CheckStatus.PASS if nulls == 0 else CheckStatus.FAIL,
        column=column.name,
        expected="0 nulls",
        actual=f"{nulls:,} nulls",
        message=(
            f"column '{column.name}' has no nulls."
            if nulls == 0
            else f"column '{column.name}' is declared not-null but has {nulls:,} null(s)."
        ),
    )


def _uniqueness_check(column: ColumnContract, series: pl.Series) -> CheckResult | None:
    if not column.unique:
        return None
    values = series.drop_nulls()
    duplicates = values.len() - values.n_unique()
    return CheckResult(
        check="unique",
        status=CheckStatus.PASS if duplicates == 0 else CheckStatus.FAIL,
        column=column.name,
        expected="all values distinct",
        actual=f"{duplicates:,} duplicate value(s)",
        message=(
            f"column '{column.name}' is unique."
            if duplicates == 0
            else f"column '{column.name}' is declared unique but has {duplicates:,} duplicate(s)."
        ),
        detail=(
            None
            if duplicates == 0
            else "A duplicated key multiplies rows on the join it feeds, which inflates every "
            "total downstream without erroring."
        ),
    )


def _range_check(column: ColumnContract, series: pl.Series) -> CheckResult | None:
    if column.min_value is None and column.max_value is None:
        return None
    numeric = series.cast(pl.Float64, strict=False).drop_nulls()
    if numeric.len() == 0:
        return CheckResult(
            check="value_range",
            status=CheckStatus.SKIP,
            column=column.name,
            message=f"column '{column.name}' has no numeric values to range-check.",
        )
    below = 0 if column.min_value is None else int((numeric < column.min_value).sum())
    above = 0 if column.max_value is None else int((numeric > column.max_value).sum())
    breaches = below + above
    bounds = (
        f"{'-' if column.min_value is None else column.min_value} to "
        f"{'-' if column.max_value is None else column.max_value}"
    )
    return CheckResult(
        check="value_range",
        status=CheckStatus.PASS if breaches == 0 else CheckStatus.FAIL,
        column=column.name,
        expected=bounds,
        actual=f"min {numeric.min()}, max {numeric.max()}",
        message=(
            f"column '{column.name}' is within {bounds}."
            if breaches == 0
            else f"column '{column.name}' has {below:,} value(s) below and {above:,} above "
            f"the contracted range."
        ),
    )


def _allowed_values_check(column: ColumnContract, series: pl.Series) -> CheckResult | None:
    if not column.allowed_values:
        return None
    permitted = set(column.allowed_values)
    seen = {str(v) for v in series.drop_nulls().unique().to_list()}
    unexpected = sorted(seen - permitted)
    listed = ", ".join(unexpected[:_MAX_LISTED_VALUES])
    if len(unexpected) > _MAX_LISTED_VALUES:
        listed += f", ... and {len(unexpected) - _MAX_LISTED_VALUES} more"
    return CheckResult(
        check="allowed_values",
        status=CheckStatus.PASS if not unexpected else CheckStatus.FAIL,
        column=column.name,
        expected=", ".join(sorted(permitted)),
        actual=listed or None,
        message=(
            f"column '{column.name}' contains only permitted values."
            if not unexpected
            else f"column '{column.name}' contains {len(unexpected)} value(s) the contract "
            f"does not permit: {listed}."
        ),
    )


def _unique_key_check(contract: Contract, frame: pl.DataFrame) -> CheckResult | None:
    if not contract.unique_key:
        return None
    missing = [name for name in contract.unique_key if name not in frame.columns]
    if missing:
        return CheckResult(
            check="unique_key",
            status=CheckStatus.SKIP,
            message=(
                f"the key {', '.join(contract.unique_key)} could not be checked: "
                f"{', '.join(missing)} absent."
            ),
        )
    duplicates = frame.height - frame.select(contract.unique_key).n_unique()
    key = " + ".join(contract.unique_key)
    return CheckResult(
        check="unique_key",
        status=CheckStatus.PASS if duplicates == 0 else CheckStatus.FAIL,
        expected=f"{key} identifies a row",
        actual=f"{duplicates:,} duplicate key(s)",
        message=(
            f"the key {key} is unique."
            if duplicates == 0
            else f"the key {key} is duplicated on {duplicates:,} row(s)."
        ),
        detail=(
            None
            if duplicates == 0
            else "Check key uniqueness before any join: an exact VLOOKUP translated to a left "
            "join multiplies rows when the right-hand key repeats (PLAN 2.6)."
        ),
    )


# =============================================================================
# THE VALIDATOR
# =============================================================================


def _layout_check(layout: Layout) -> CheckResult | None:
    """Report anything the reader had to skip, so nothing is dropped silently."""
    notes = layout.notes()
    if not notes:
        return None
    return CheckResult(
        check="layout",
        status=CheckStatus.PASS,
        message=f"before checking, kedge {' and '.join(notes)}.",
        actual=f"header on row {layout.header_row}",
        detail=(
            "A totals row is layout, not data: validating it would fail the not-null and "
            "uniqueness checks on every identifier column."
            if layout.totals_row_excluded
            else None
        ),
    )


def validate_frame(
    frame: pl.DataFrame,
    contract: Contract,
    *,
    handin_name: str,
    handin_sha256: str = "",
    layout: Layout | None = None,
) -> ValidationReport:
    """Validate an already-loaded frame against a contract.

    Split out from :func:`validate` so a notebook that has the frame in hand can re-check it
    without re-reading the file, and so the checks are unit-testable without a filesystem.

    Args:
        frame: The hand-in's data, headers applied and any totals row already removed.
        contract: The contract to check against.
        handin_name: Name to record in the report.
        handin_sha256: Digest to record in the report, and to compare when the contract
            carries a checksum.
        layout: What the reader skipped to reach the data, reported as its own check.

    Returns:
        The :class:`~kedge.contracts.model.ValidationReport`.
    """
    checks: list[CheckResult] = []

    checksum = _checksum_check(contract, handin_sha256)
    if checksum is not None:
        checks.append(checksum)

    if layout is not None:
        layout_note = _layout_check(layout)
        if layout_note is not None:
            checks.append(layout_note)

    checks.extend(_presence_checks(contract, list(frame.columns)))

    row_count = _row_count_check(contract, frame.height)
    if row_count is not None:
        checks.append(row_count)

    schema = frame.schema
    for column in contract.columns:
        if column.name not in frame.columns:
            if column.required:
                checks.append(_skipped(column, "dtype"))
            continue
        series = frame[column.name]
        checks.append(_dtype_check(column, dtype_name(schema[column.name])))
        for result in (
            _nullability_check(column, series),
            _uniqueness_check(column, series),
            _range_check(column, series),
            _allowed_values_check(column, series),
        ):
            if result is not None:
                checks.append(result)

    key_check = _unique_key_check(contract, frame)
    if key_check is not None:
        checks.append(key_check)

    report = ValidationReport(
        contract_name=contract.name,
        handin_name=handin_name,
        handin_sha256=handin_sha256,
        validated_at=utcnow(),
        checks=tuple(checks),
    )
    if report.ok:
        logger.info("contract '%s' passed for %s", contract.name, handin_name)
    else:
        logger.warning(
            "contract '%s' failed for %s: %d check(s) failed",
            contract.name,
            handin_name,
            len(report.failures),
        )
    return report


def validate(handin: HandIn, contract: Contract) -> ValidationReport:
    """Validate a received hand-in against its contract.

    The call the notebook's contract cell makes. It returns a structured report rather than
    raising, so the cell can render pass *and* fail states; call
    :meth:`~kedge.contracts.model.ValidationReport.raise_for_status` on the result to stop
    the dataflow graph when the hand-in is wrong.

    Args:
        handin: The received hand-in. Its managed path is what gets read.
        contract: The contract to check against.

    Returns:
        The :class:`~kedge.contracts.model.ValidationReport`.

    Raises:
        kedge.ingest.drift.HandInReadError: If the hand-in cannot be read at all. That is a
            failure to run the checks rather than a check result, so it raises.

    Example:
        >>> report = validate(handin, contract)
        >>> mo.ui.table(report.to_rows())
    """
    frame, layout = read_data(handin.path, sheet=contract.sheet, header_row=contract.header_row)
    return validate_frame(
        frame,
        contract,
        handin_name=handin.original_name,
        handin_sha256=handin.sha256,
        layout=layout,
    )


def validate_path(path: Path, contract: Contract) -> ValidationReport:
    """Validate a file that has not been through ingestion, for ``kedge contract check``.

    Behaves exactly as :func:`validate` does, including the checksum check, but the file is
    hashed here rather than at receipt. There is no receipt, so there is no audit trail
    proving which bytes were read -- use this for checking a draft contract, not as a
    substitute for ingesting the hand-in.
    """
    frame, layout = read_data(path, sheet=contract.sheet, header_row=contract.header_row)
    return validate_frame(
        frame,
        contract,
        handin_name=path.name,
        handin_sha256=hash_file(path),
        layout=layout,
    )
