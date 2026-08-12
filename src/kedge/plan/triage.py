"""Deterministic convertibility assessment. No LLM, no network, no judgement calls.

Not every workbook should be converted. Heavy VBA, genuine circularity, or a process that is
mostly human judgement with a spreadsheet attached are all legitimate "don't". An honest
``convertible: 0.4`` with a list of blockers is far more useful than a notebook that looks
complete and is not (PLAN 2.2).

Two design points do the real work here:

- **"Stop" is a verdict, not a low number.** A score nobody notices is a score that gets
  overridden by momentum. :class:`TriageVerdict` is a first-class outcome and
  :func:`kedge.plan.propose.propose_plan` refuses to spend a model call on a ``STOP`` unless the
  caller explicitly forces it.
- **Conversion blockers and verification blockers are different things.** An absent value cache
  does not make a workbook harder to translate; it makes the translation impossible to *prove*,
  which is a separate and arguably worse problem. Mixing the two into one score produces a
  number that means nothing. :attr:`Blocker.blocks` keeps them apart, and only conversion
  blockers move the ``convertible`` figure.

Scoring is a fixed penalty table applied to the analysis. It is deliberately boring: the point
of a deterministic triage is that the same workbook yields the same answer every time, so a
change in the number means a change in the workbook.

References:
- PLAN.md 2.2 (triage before planning), 1.5 and 4.5 (cached values), 6.2 (xlsb/VBA refusal).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from kedge.analysis.model import (
    ExcelPattern,
    ExtractionStatus,
    Finding,
    FindingKind,
    Severity,
    SheetRole,
    WorkbookAnalysis,
)

logger = logging.getLogger(__name__)

__all__ = [
    "Blocker",
    "BlockerCode",
    "TriageResult",
    "TriageVerdict",
    "complexity",
    "triage",
]

STOP_THRESHOLD = 0.35
"""Below this, the verdict is STOP regardless of which penalties produced it."""

CARE_THRESHOLD = 0.75
"""Below this, the verdict is PROCEED_WITH_CARE."""

_MANUAL_RATIO_THRESHOLD = 0.75
"""Share of non-formula cells on calculation sheets above which the process is mostly typing."""

_UNKNOWN_PATTERN_THRESHOLD = 0.4
"""Share of logical operations the analyser could not classify before it becomes a blocker."""


class TriageVerdict(StrEnum):
    """What to do with this workbook.

    ``STOP`` is the whole reason this module exists. It is not "the score was low"; it is a
    refusal with reasons attached, which the CLI prints and the pipeline honours.
    """

    PROCEED = "proceed"
    PROCEED_WITH_CARE = "proceed_with_care"
    STOP = "stop"


class BlockerCode(StrEnum):
    """The catalogue of reasons a workbook resists conversion or verification."""

    VBA_PRESENT = "vba_present"
    UNSUPPORTED_FORMAT = "unsupported_format"
    CIRCULAR_REFERENCE = "circular_reference"
    ITERATIVE_CALCULATION = "iterative_calculation"
    UNRESOLVABLE_EXTERNAL_LINK = "unresolvable_external_link"
    MOSTLY_MANUAL = "mostly_manual"
    NO_LOGIC = "no_logic"
    UNPARSEABLE_POWER_QUERY = "unparseable_power_query"
    UNCLASSIFIED_FORMULAS = "unclassified_formulas"
    ANALYSIS_ERRORS = "analysis_errors"
    NO_CACHED_VALUES = "no_cached_values"
    PARTIAL_CACHED_VALUES = "partial_cached_values"


@dataclass(frozen=True, slots=True)
class Blocker:
    """One reason the answer is not an unqualified yes.

    ``blocks`` is the field that matters. A ``verification`` blocker leaves ``convertible``
    untouched: the code can still be written, it just cannot be proved right against the
    workbook's own numbers.
    """

    code: BlockerCode
    message: str
    blocks: str = "conversion"
    """Either ``"conversion"`` or ``"verification"``."""
    severity: Severity = Severity.WARNING
    penalty: float = 0.0
    """How much this took off the convertible score. Zero for verification blockers."""
    fatal: bool = False
    """Whether this alone forces a STOP verdict, whatever the arithmetic says."""
    remediation: str | None = None

    def render(self) -> str:
        """One line, suitable for a CLI list or a plan's ``assessment.blockers``."""
        suffix = f" ({self.remediation})" if self.remediation else ""
        return f"{self.message}{suffix}"


@dataclass(frozen=True, slots=True)
class TriageResult:
    """The structured answer: a verdict, a score, and the reasons for both.

    Example:
        >>> result = TriageResult(verdict=TriageVerdict.STOP, convertible=0.1, complexity=0.5)
        >>> result.should_stop
        True
    """

    verdict: TriageVerdict
    convertible: float
    complexity: float
    blockers: tuple[Blocker, ...] = ()
    reconcilable: bool = True
    workbook: str = ""
    notes: tuple[str, ...] = ()

    @property
    def should_stop(self) -> bool:
        """Whether the pipeline must halt and say so rather than spending a model call."""
        return self.verdict is TriageVerdict.STOP

    @property
    def conversion_blockers(self) -> list[Blocker]:
        """Blockers that make the translation itself harder or impossible."""
        return [blocker for blocker in self.blockers if blocker.blocks == "conversion"]

    @property
    def verification_blockers(self) -> list[Blocker]:
        """Blockers that leave the translation possible but unprovable (PLAN 4.5)."""
        return [blocker for blocker in self.blockers if blocker.blocks == "verification"]

    @property
    def fatal_blockers(self) -> list[Blocker]:
        """Blockers that forced the STOP on their own."""
        return [blocker for blocker in self.blockers if blocker.fatal]

    def blocker_lines(self) -> list[str]:
        """Every blocker rendered as one line each, conversion first."""
        return [
            blocker.render() for blocker in (*self.conversion_blockers, *self.verification_blockers)
        ]

    def explain(self) -> str:
        """A complete, plain-English explanation, including what the user can do about it.

        This is what the CLI prints when the verdict is STOP, and it is the whole value of a
        refusal: a workbook kedge declines to convert should leave the user better informed
        than when they started.
        """
        headline = {
            TriageVerdict.PROCEED: "Convertible.",
            TriageVerdict.PROCEED_WITH_CARE: "Convertible, with reservations.",
            TriageVerdict.STOP: "kedge recommends not converting this workbook.",
        }[self.verdict]

        lines = [
            f"{headline}",
            f"  convertible: {self.convertible:.2f}    complexity: {self.complexity:.2f}",
        ]
        conversion = self.conversion_blockers
        if conversion:
            lines.append("  blockers to conversion:")
            lines.extend(f"    - {blocker.render()}" for blocker in conversion)
        verification = self.verification_blockers
        if verification:
            lines.append(
                "  blockers to verification (the code can be written; it cannot be proved):"
            )
            lines.extend(f"    - {blocker.render()}" for blocker in verification)
        if not conversion and not verification:
            lines.append("  no blockers found.")
        lines.extend(f"  note: {note}" for note in self.notes)
        if self.should_stop:
            lines.append(
                "  Proceeding anyway is possible with --force, but the result will be a notebook "
                "that looks more complete than it is."
            )
        return "\n".join(lines)


# =============================================================================
# COMPLEXITY
# =============================================================================


def complexity(analysis: WorkbookAnalysis) -> float:
    """Score how much process there is to understand here, 0-1.

    Used for two things: deciding how much of the analysis to spend context on, and deciding
    whether an empty ``open_questions`` list on the resulting plan is suspicious
    (:meth:`kedge.plan.model.ProcessPlan.open_questions_warning`).

    The inputs are all structural — operation count, sheet count, cross-sheet reach, pattern
    variety, findings — and each is squashed on a log scale so that a 500-operation workbook is
    "very complex" rather than "fifty times complex".

    Args:
        analysis: The workbook analysis to score.

    Returns:
        A float in 0-1. Roughly: under 0.35 is a simple workbook, over 0.7 is a big one.
    """
    operations = len(analysis.operations)
    sheets = len([sheet for sheet in analysis.sheets if sheet.formula_cell_count or sheet.max_row])
    cross_sheet = sum(
        1
        for operation in analysis.operations
        if any(reference.sheet for reference in operation.references)
    )
    patterns = {operation.excel_pattern for operation in analysis.operations}
    findings = len(analysis.findings)

    components = (
        _squash(operations, 60),
        _squash(sheets, 12),
        _squash(cross_sheet, 30),
        _squash(len(patterns), 10),
        _squash(findings, 25),
    )
    weights = (0.35, 0.2, 0.2, 0.1, 0.15)
    score = sum(component * weight for component, weight in zip(components, weights, strict=True))
    return round(min(1.0, max(0.0, score)), 3)


def _squash(value: int, midpoint: int) -> float:
    """Map a count onto 0-1 on a log scale, reaching ~0.5 at ``midpoint``."""
    if value <= 0:
        return 0.0
    return min(1.0, math.log1p(value) / math.log1p(midpoint * 4))


# =============================================================================
# TRIAGE
# =============================================================================


def triage(analysis: WorkbookAnalysis) -> TriageResult:
    """Assess whether this workbook should be converted at all.

    Deterministic and offline: the same analysis always produces the same verdict, so a change
    in the number means a change in the workbook rather than a change of mood.

    Args:
        analysis: The complete workbook analysis from :mod:`kedge.analysis`.

    Returns:
        A :class:`TriageResult` whose ``verdict`` is the outcome the pipeline acts on and whose
        ``blockers`` are what the plan's ``assessment.blockers`` is seeded from.
    """
    blockers: list[Blocker] = []
    notes: list[str] = []

    _check_format(analysis, blockers, notes)
    _check_vba(analysis, blockers)
    _check_circularity(analysis, blockers)
    _check_external_links(analysis, blockers)
    _check_manual_ratio(analysis, blockers, notes)
    _check_power_query(analysis, blockers)
    _check_unclassified(analysis, blockers)
    _check_analysis_errors(analysis, blockers)
    _check_cached_values(analysis, blockers)

    penalty = sum(blocker.penalty for blocker in blockers)
    convertible = round(min(1.0, max(0.0, 1.0 - penalty)), 3)

    fatal = any(blocker.fatal for blocker in blockers)
    if fatal or convertible < STOP_THRESHOLD:
        verdict = TriageVerdict.STOP
    elif convertible < CARE_THRESHOLD or blockers:
        verdict = TriageVerdict.PROCEED_WITH_CARE
    else:
        verdict = TriageVerdict.PROCEED

    result = TriageResult(
        verdict=verdict,
        convertible=convertible,
        complexity=complexity(analysis),
        blockers=tuple(blockers),
        reconcilable=analysis.is_reconcilable,
        workbook=analysis.workbook.filename,
        notes=tuple(notes),
    )
    logger.info(
        "triage of %s: %s (convertible %.2f, %d blockers)",
        analysis.workbook.filename,
        result.verdict.value,
        result.convertible,
        len(result.blockers),
    )
    return result


# ── individual checks ────────────────────────────────────────────────────────


def _check_format(analysis: WorkbookAnalysis, blockers: list[Blocker], notes: list[str]) -> None:
    """`.xlsb` and `.xls` are refused outright: openpyxl cannot read either.

    A companion file kedge cannot read is a different matter, and is recorded as a note.
    """
    file_format = analysis.workbook.file_format
    if file_format in ("xlsb", "xls"):
        blockers.append(
            Blocker(
                code=BlockerCode.UNSUPPORTED_FORMAT,
                message=(
                    f"{analysis.workbook.filename} is a .{file_format} workbook, which kedge "
                    f"cannot read: formulas, cached values and structure are all unavailable"
                ),
                severity=Severity.ERROR,
                penalty=1.0,
                fatal=True,
                remediation="re-save as .xlsx in Excel and analyse that",
            )
        )
        return
    # UNSUPPORTED_FORMAT is raised for companion files too -- a legacy .doc procedure sitting
    # beside the workbook earns one -- and reading that as "the workbook is unreadable" is a
    # category error with a fatal penalty attached: documented.xlsx triaged STOP at 0.00
    # convertible on the strength of a Word document nobody needed. Only a finding that names
    # the workbook itself, or names nothing, is about the workbook.
    unreadable = [
        finding
        for finding in analysis.findings_of(FindingKind.UNSUPPORTED_FORMAT)
        if _is_about_the_workbook(finding, analysis)
    ]
    if unreadable:
        blockers.append(
            Blocker(
                code=BlockerCode.UNSUPPORTED_FORMAT,
                message="the analyser reported the workbook format as unsupported",
                severity=Severity.ERROR,
                penalty=1.0,
                fatal=True,
                remediation="re-save as .xlsx in Excel and analyse that",
            )
        )
        return

    named = sorted(
        {
            Path(finding.location).name
            for finding in analysis.findings_of(FindingKind.UNSUPPORTED_FORMAT)
            if finding.location and not _is_about_the_workbook(finding, analysis)
        }
    )
    if named:
        # A note rather than a blocker: the workbook converts fine, so neither the score nor
        # the verdict should move. What is lost is the documentation of *why* it does what it
        # does, which is a real gap when the plan comes to explain a stage -- so it is said out
        # loud rather than silently dropped (PLAN 1.5).
        notes.append(
            f"could not read {', '.join(named)} beside the workbook, so any process notes in "
            f"it are missing from the analysis"
        )


def _is_about_the_workbook(finding: Finding, analysis: WorkbookAnalysis) -> bool:
    """Whether a finding points at the workbook itself rather than a file beside it."""
    if not finding.location:
        return True
    return Path(finding.location).name == analysis.workbook.filename


def _check_vba(analysis: WorkbookAnalysis, blockers: list[Blocker]) -> None:
    """VBA is a separate problem and kedge does not half-solve it (PLAN 6.2)."""
    macro_findings = analysis.findings_of(FindingKind.MACRO_PRESENT)
    if not analysis.workbook.has_vba and not macro_findings:
        return
    detail = macro_findings[0].message if macro_findings else "the workbook contains a VBA project"
    blockers.append(
        Blocker(
            code=BlockerCode.VBA_PRESENT,
            message=(
                f"{detail}. Macro logic is invisible to the analyser, so any plan written from "
                f"the formulas alone is describing part of the process"
            ),
            severity=Severity.ERROR,
            penalty=0.4,
            remediation="document what the macros do, or convert them by hand first",
        )
    )


def _check_circularity(analysis: WorkbookAnalysis, blockers: list[Blocker]) -> None:
    """Circular references and iterative calculation have no clean polars equivalent."""
    cycles = analysis.graph.cycles
    iterative = analysis.workbook.iterative_calculation or bool(
        analysis.findings_of(FindingKind.ITERATIVE_CALCULATION)
    )
    if cycles:
        sample = " -> ".join(cycles[0][:4])
        blockers.append(
            Blocker(
                code=BlockerCode.CIRCULAR_REFERENCE,
                message=(
                    f"{len(cycles)} circular reference chain(s) in the calculation graph, "
                    f"starting {sample}. There is no clean polars equivalent of an iterative "
                    f"solve, and a plan that pretends otherwise will not reconcile"
                ),
                severity=Severity.ERROR,
                penalty=0.35,
                fatal=iterative,
                remediation="break the cycle in Excel, or plan the loop as a checkpoint stage",
            )
        )
    if iterative:
        blockers.append(
            Blocker(
                code=BlockerCode.ITERATIVE_CALCULATION,
                message=(
                    "Excel's iterative calculation setting is enabled, which is done for a "
                    "reason: some part of this workbook converges rather than computes"
                ),
                severity=Severity.ERROR,
                penalty=0.25,
                remediation="identify the converging region before planning; it needs a loop, "
                "not an expression",
            )
        )


def _check_external_links(analysis: WorkbookAnalysis, blockers: list[Blocker]) -> None:
    """An unresolvable external link is a value nobody can reproduce."""
    unresolvable = analysis.findings_of(FindingKind.UNRESOLVABLE_EXTERNAL_LINK)
    if not unresolvable:
        return
    blockers.append(
        Blocker(
            code=BlockerCode.UNRESOLVABLE_EXTERNAL_LINK,
            message=(
                f"{len(unresolvable)} reference(s) to external workbooks that could not be "
                f"resolved; the values behind them cannot be reproduced from this file alone"
            ),
            severity=Severity.ERROR,
            penalty=min(0.3, 0.1 * len(unresolvable)),
            remediation="supply the linked workbooks, or plan those inputs as hand-ins",
        )
    )


def _check_manual_ratio(
    analysis: WorkbookAnalysis, blockers: list[Blocker], notes: list[str]
) -> None:
    """A process that is mostly typing with a spreadsheet attached is a legitimate "don't"."""
    calculation_sheets = [
        sheet
        for sheet in analysis.sheets
        if sheet.role in (SheetRole.CALCULATION, SheetRole.OUTPUT, SheetRole.UNKNOWN)
    ]
    formula_cells = sum(sheet.formula_cell_count for sheet in calculation_sheets)
    value_cells = sum(sheet.value_cell_count for sheet in calculation_sheets)
    populated = formula_cells + value_cells

    if not analysis.operations and formula_cells == 0:
        blockers.append(
            Blocker(
                code=BlockerCode.NO_LOGIC,
                message=(
                    "no formulas anywhere in the workbook: there is no calculation to translate, "
                    "only data and typing"
                ),
                severity=Severity.ERROR,
                penalty=1.0,
                fatal=True,
                remediation="if the process really is manual, a notebook adds ceremony without "
                "adding control; consider a contract-checked hand-in instead",
            )
        )
        return

    if populated < 50:
        notes.append("very few populated cells on the calculation sheets; triage is approximate")
        return

    manual_ratio = value_cells / populated
    if manual_ratio < _MANUAL_RATIO_THRESHOLD:
        return

    overrides = analysis.findings_of(FindingKind.MANUAL_OVERRIDE_BLOCK)
    detail = f", including {len(overrides)} block(s) of manual overrides" if overrides else ""
    blockers.append(
        Blocker(
            code=BlockerCode.MOSTLY_MANUAL,
            message=(
                f"{manual_ratio:.0%} of populated cells on the calculation sheets are typed "
                f"values rather than formulas{detail}: this is mostly human judgement with a "
                f"spreadsheet attached"
            ),
            severity=Severity.WARNING,
            penalty=0.3 if manual_ratio < 0.9 else 0.45,
            remediation="expect most stages to be checkpoints rather than code",
        )
    )


def _check_power_query(analysis: WorkbookAnalysis, blockers: list[Blocker]) -> None:
    """A DataMashup that will not decode hides the whole ingestion half of the process."""
    if analysis.power_query.status is not ExtractionStatus.UNPARSEABLE:
        return
    blockers.append(
        Blocker(
            code=BlockerCode.UNPARSEABLE_POWER_QUERY,
            message=(
                "the workbook carries a Power Query DataMashup part that could not be decoded, "
                "so the M code behind its loaded tables is invisible to the plan"
            ),
            severity=Severity.ERROR,
            penalty=0.2,
            remediation="export the queries from Excel's Power Query editor and supply them as "
            "process notes",
        )
    )


def _check_unclassified(analysis: WorkbookAnalysis, blockers: list[Blocker]) -> None:
    """Formulas the analyser could not classify are formulas the plan will guess at."""
    if not analysis.operations:
        return
    unknown = [
        operation
        for operation in analysis.operations
        if operation.excel_pattern is ExcelPattern.UNKNOWN
    ]
    ratio = len(unknown) / len(analysis.operations)
    if ratio < _UNKNOWN_PATTERN_THRESHOLD:
        return
    blockers.append(
        Blocker(
            code=BlockerCode.UNCLASSIFIED_FORMULAS,
            message=(
                f"{ratio:.0%} of logical operations ({len(unknown)} of "
                f"{len(analysis.operations)}) match no known Excel pattern, so the plan is "
                f"working from descriptions rather than from a translation vocabulary"
            ),
            severity=Severity.WARNING,
            penalty=min(0.25, 0.3 * ratio),
            remediation="expect open questions on those regions rather than confident stages",
        )
    )


def _check_analysis_errors(analysis: WorkbookAnalysis, blockers: list[Blocker]) -> None:
    """Error-severity findings the other checks did not already account for."""
    accounted = {
        FindingKind.UNSUPPORTED_FORMAT,
        FindingKind.MACRO_PRESENT,
        FindingKind.CIRCULAR_REFERENCE,
        FindingKind.ITERATIVE_CALCULATION,
        FindingKind.UNRESOLVABLE_EXTERNAL_LINK,
        FindingKind.MISSING_CACHED_VALUES,
        FindingKind.PARTIAL_CACHED_VALUES,
    }
    remaining = [finding for finding in analysis.errors if finding.kind not in accounted]
    if not remaining:
        return
    kinds = sorted({finding.kind.value for finding in remaining})
    blockers.append(
        Blocker(
            code=BlockerCode.ANALYSIS_ERRORS,
            message=(
                f"{len(remaining)} error-severity finding(s) from the analyser: {', '.join(kinds)}"
            ),
            severity=Severity.ERROR,
            penalty=min(0.2, 0.05 * len(remaining)),
            remediation="read the findings list in the analysis report before planning",
        )
    )


def _check_cached_values(analysis: WorkbookAnalysis, blockers: list[Blocker]) -> None:
    """Absent cached values block *verification*, not conversion. Keep the two apart.

    openpyxl never calculates anything, so a workbook written by a tool rather than saved by
    Excel returns None for every formula cell. The translation can still be written; it simply
    cannot be checked against the workbook's own numbers, and reconciliation must report "not
    reconciled" rather than "passed" (PLAN 1.5, 4.5).
    """
    coverage = analysis.cached_values
    if coverage.status == "not_applicable":
        return
    if coverage.status == "absent" or not coverage.reconcilable:
        blockers.append(
            Blocker(
                code=BlockerCode.NO_CACHED_VALUES,
                message=(
                    "the workbook carries no cached calculated values, so there is no baseline "
                    "to reconcile the translation against; every region will report 'not "
                    "reconciled' rather than 'passed'"
                ),
                blocks="verification",
                severity=Severity.ERROR,
                penalty=0.0,
                remediation="open the workbook in Excel, recalculate, and save it again",
            )
        )
        return
    if coverage.status == "partial" or coverage.coverage < 0.95:
        blockers.append(
            Blocker(
                code=BlockerCode.PARTIAL_CACHED_VALUES,
                message=(
                    f"cached values cover only {coverage.coverage:.0%} of formula cells; the "
                    f"regions without them cannot be reconciled"
                ),
                blocks="verification",
                severity=Severity.WARNING,
                penalty=0.0,
                remediation="recalculate and re-save in Excel to complete the baseline",
            )
        )
