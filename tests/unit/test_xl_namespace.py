"""Tests for the `.xl` expression namespace.

The namespace is a thin façade: the Excel ground truth lives in the tests for the plain
functions, and these tests only prove two things -- that the namespace registers, and that
every method delegates to the function it claims to. If a method ever grows logic of its
own, the delegation test stops being a tautology and starts being a real assertion, which
is the point.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any

import polars as pl
import pytest

import kedge.xl as xl
from kedge.xl.namespace import ExcelExpr

# ── (column, namespace method, args, plain function) ──────────────────────────────────
# Every public method on ExcelExpr must appear here; the coverage test below enforces it.
DELEGATIONS: list[tuple[str, str, tuple[Any, ...], Callable[..., pl.Expr]]] = [
    ("amount", "round", (2,), xl.round_half_away),
    ("amount", "round", (0,), xl.round_half_away),
    ("amount", "round", (-1,), xl.round_half_away),
    ("amount", "roundup", (1,), xl.round_up),
    ("amount", "rounddown", (1,), xl.round_down),
    ("amount", "empty_as_zero", (), xl.empty_as_zero),
    ("amount", "add", (pl.col("other"),), xl.add),
    ("amount", "add", (5,), xl.add),
    ("amount", "sub", (pl.col("other"),), xl.sub),
    ("amount", "mul", (pl.col("other"),), xl.mul),
    ("amount", "div", (pl.col("other"),), xl.div),
    ("amount", "div", (0,), xl.div),
    ("text", "to_number", (), xl.to_number),
    ("serial", "serial_to_date", (), xl.serial_to_date),
    ("serial", "serial_to_datetime", (), xl.serial_to_datetime),
    ("date", "date_to_serial", (), xl.date_to_serial),
    ("amount", "is_error", (), xl.is_error),
    ("amount", "iferror", (0.0,), xl.iferror),
    ("amount", "iferror", ("missing",), xl.iferror),
]


@pytest.fixture
def frame() -> pl.LazyFrame:
    """One lazy frame carrying a column of each shape the namespace operates on."""
    return pl.LazyFrame(
        {
            "amount": [2.675, -2.5, None, 0.0],
            "other": [3.0, None, None, 0.0],
            "text": ["1,234", "$5.00", None, "abc"],
            "serial": [45000.5, 60.0, None, 1.0],
            "date": [dt.date(2023, 3, 15), dt.date(1900, 2, 28), None, dt.date(1900, 3, 1)],
        },
        schema={
            "amount": pl.Float64,
            "other": pl.Float64,
            "text": pl.String,
            "serial": pl.Float64,
            "date": pl.Date,
        },
    )


def test_importing_kedge_xl_should_register_the_namespace() -> None:
    """`import kedge.xl` should make `.xl` reachable on any expression, as a side effect."""
    assert hasattr(pl.col("anything"), "xl")
    assert isinstance(pl.col("anything").xl, ExcelExpr)


def test_namespace_should_be_registered_by_the_namespace_module() -> None:
    """The registration should come from `kedge.xl.namespace`, the module that can be deleted."""
    import sys

    assert "kedge.xl.namespace" in sys.modules
    assert pl.col("x").xl.__class__ is ExcelExpr


@pytest.mark.parametrize(
    ("column", "method", "args", "plain_function"),
    DELEGATIONS,
    ids=[f"{method}{args}" for _, method, args, _ in DELEGATIONS],
)
def test_namespace_method_should_delegate_to_the_plain_function(
    frame: pl.LazyFrame,
    column: str,
    method: str,
    args: tuple[Any, ...],
    plain_function: Callable[..., pl.Expr],
) -> None:
    """Each `.xl` method should produce exactly what its plain function produces."""
    through_namespace = getattr(pl.col(column).xl, method)(*args)
    directly = plain_function(pl.col(column), *args)

    result = frame.select(
        namespace=through_namespace.alias("namespace"),
        plain=directly.alias("plain"),
    ).collect()
    assert result["namespace"].to_list() == result["plain"].to_list()


def test_every_public_namespace_method_should_be_covered_by_a_delegation_test() -> None:
    """A method added without a delegation case should fail here rather than go untested."""
    public_methods = {
        name
        for name in dir(ExcelExpr)
        if not name.startswith("_") and callable(getattr(ExcelExpr, name))
    }
    covered = {method for _, method, _, _ in DELEGATIONS}
    assert public_methods == covered


def test_namespace_should_hold_no_state_beyond_the_expression() -> None:
    """The class must stay a pure wrapper so that deleting it is a removal, not a rewrite."""
    assert ExcelExpr.__slots__ == ("_expr",)
    instance = pl.col("x").xl
    assert not hasattr(instance, "__dict__")


def test_namespace_should_compose_inside_a_lazy_pipeline(frame: pl.LazyFrame) -> None:
    """The end-to-end shape generated notebook code will actually use."""
    result = (
        frame.with_columns(
            rounded=pl.col("amount").xl.round(2),
            total=pl.col("amount").xl.add(pl.col("other")),
            ratio=pl.col("amount").xl.div(pl.col("other")),
            parsed=pl.col("text").xl.to_number(),
            as_date=pl.col("serial").xl.serial_to_date(),
            as_serial=pl.col("date").xl.date_to_serial(),
        )
        .collect()
        .to_dicts()
    )
    assert result[0]["rounded"] == pytest.approx(2.68)
    assert result[0]["total"] == pytest.approx(5.675)
    assert result[0]["parsed"] == pytest.approx(1234.0)
    assert result[0]["as_date"] == dt.date(2023, 3, 15)
    assert result[0]["as_serial"] == 45000
    assert result[1]["as_date"] is None  # serial 60, the phantom leap day
    assert result[1]["ratio"] is None  # null denominator, Excel's #DIV/0!
    assert result[2]["rounded"] is None  # null in, null out
    assert result[3]["ratio"] is None  # zero denominator


def test_module_level_helpers_should_not_require_the_namespace() -> None:
    """`sum_horizontal` is row-wise across columns, so it is a function, not a method."""
    frame = pl.LazyFrame({"a": [1.0, None], "b": [2.0, None]})
    assert frame.select(xl.sum_horizontal("a", "b")).collect().to_series().to_list() == [3.0, 0.0]


def test_public_api_should_be_importable_from_the_package_root() -> None:
    """Everything in `__all__` should actually exist, so the documented surface is real."""
    missing = [name for name in xl.__all__ if not hasattr(xl, name)]
    assert missing == []
