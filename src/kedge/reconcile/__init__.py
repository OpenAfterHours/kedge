"""Reconciliation: proving the Python reproduces the workbook's own numbers (PLAN 4.5).

Because ``data_only=True`` yields the values Excel last calculated, kedge holds an expected
output for every formula cell in the workbook. Reconciliation is the loop that uses it: read
a column out of the notebook, compare it against the cached values for the corresponding
range within an absolute *and* relative tolerance, and report what matched, what did not, and
what the difference most likely is.

Five modules, and the split is the design:

* :mod:`kedge.reconcile.model` -- the report schema. Plain serialisable data, no polars, no
  marimo, no openpyxl. This is also where the safety invariant lives: a status that is falsy
  unless it is ``PASSED``, and a region that refuses to be constructed as a pass without
  compared rows.
* :mod:`kedge.reconcile.baseline` -- the Excel side. Reads the cached values for a range and
  is scrupulous about the difference between "absent", "partial" and "empty".
* :mod:`kedge.reconcile.compare` -- the engine and the entry points, including
  :func:`~kedge.reconcile.compare.reconcile_notebook`, which is what ``kedge reconcile``
  runs. Takes vectors, so it is testable with plain lists.
* :mod:`kedge.reconcile.diagnose` -- the triage. Pattern-matches mismatches against the
  PLAN 2.6 semantics table and returns ranked causes with concrete remedies.
* :mod:`kedge.reconcile.cell` -- the green/amber/red notebook panel. marimo is imported in
  one function and nowhere else.

**The invariant that outranks everything else here.** Where the workbook holds no cached
values, reconciliation degrades to ``NOT_RECONCILED`` and says so in as many words. It never
degrades to ``PASSED``. PLAN 6.2 names "reconciliation silently vacuous" as a risk, and a
false pass is the most dangerous thing this project could ship: it is a signed-off claim that
nothing checked.
"""

from __future__ import annotations

from kedge.reconcile.acceptance import (
    Acceptance,
    AcceptanceStore,
    TranslationCheck,
    check_translation,
)
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
from kedge.reconcile.cell import ReconciliationPanel, panel_html, reconcile_panel, render
from kedge.reconcile.compare import (
    DECLARED_REGIONS_NAME,
    Comparison,
    NotebookRunError,
    compare_vectors,
    declared_regions,
    notebook_definitions,
    reconcile_notebook,
    reconcile_region,
    reconcile_values,
    reconcile_workbook,
    resolve_actual,
    to_vector,
)
from kedge.reconcile.diagnose import diagnose
from kedge.reconcile.model import (
    SCHEMA_VERSION,
    Diagnosis,
    DiagnosticCause,
    Mismatch,
    MismatchKind,
    NotReconciledReason,
    ReconciliationReport,
    ReconciliationStatus,
    RegionResult,
    RegionSpec,
    Tolerance,
    as_numeric_pair,
    signed_delta,
    worst_status,
)
from kedge.reconcile.verify import (
    MAX_REPORTED,
    RowBreak,
    VerificationReport,
    verify,
)

__all__ = [
    "DECLARED_REGIONS_NAME",
    "MAX_REPORTED",
    "SCHEMA_VERSION",
    "Acceptance",
    "AcceptanceStore",
    "BaselineVector",
    "Comparison",
    "Diagnosis",
    "DiagnosticCause",
    "Mismatch",
    "MismatchKind",
    "NotReconciledReason",
    "NotebookRunError",
    "ReconciliationPanel",
    "ReconciliationReport",
    "ReconciliationStatus",
    "RegionResult",
    "RegionSpec",
    "RowBreak",
    "Tolerance",
    "TranslationCheck",
    "VerificationReport",
    "as_numeric_pair",
    "check_translation",
    "compare_vectors",
    "declared_regions",
    "diagnose",
    "infer_regions",
    "notebook_definitions",
    "operation_ranges",
    "operation_reference",
    "panel_html",
    "read_baseline",
    "read_baselines",
    "reconcile_notebook",
    "reconcile_panel",
    "reconcile_region",
    "reconcile_values",
    "reconcile_workbook",
    "region_cell_count",
    "render",
    "resolve_actual",
    "signed_delta",
    "specs_from_mapping",
    "split_reference",
    "to_vector",
    "verify",
    "worst_status",
]
