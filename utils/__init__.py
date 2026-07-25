"""Reusable helpers for converted processes — yours to edit.

This package is importable from notebook cells, and the copilot is given a catalogue of what is
here (name, signature, and the first line of each docstring) on every turn. So a function added
here gets reused instead of reinvented, and that reuse is the whole maintainability argument for
converting a workbook in the first place.

Two rules worth keeping:

* Everything here takes and returns ``pl.LazyFrame`` or ``pl.Expr``, so helpers compose inside a
  query plan rather than forcing a collect.
* Give every function a one-line docstring that says what it does in business terms. That line is
  literally what the model sees.
"""

from __future__ import annotations

from utils.frames import (
    assert_unique_key,
    drop_all_null_columns,
    normalise_headers,
    strip_totals_row,
)
from utils.money import allocate_residual, to_thousands

__all__ = [
    "allocate_residual",
    "assert_unique_key",
    "drop_all_null_columns",
    "normalise_headers",
    "strip_totals_row",
    "to_thousands",
]
