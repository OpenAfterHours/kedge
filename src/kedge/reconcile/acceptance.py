"""Reconciliation is a claim about the *translation*, not about this month's run.

The distinction is easy to lose and expensive to lose. :mod:`kedge.reconcile.compare` answers
"does this Python reproduce the workbook's own numbers?" — and that is a question about the
**conversion**, asked once, against the data the workbook itself holds. It is an acceptance test.

A converted workbook is then run every month against *new* data. Nothing in the workbook is a
baseline for that: the spreadsheet was Q2, the run is Q3, and the numbers are supposed to differ.
Leaving the live comparison in the notebook means it fails on every run after the first — a red
panel saying the figures do not match, on a run where nothing is wrong. That is worse than
useless. It trains the user to ignore the one control in the notebook that would matter if it
ever went red for a real reason, and after a few months it is pointing at a spreadsheet that no
longer describes the process at all.

So the outcome is **recorded once and cited afterwards**. The record says what was reconciled,
against which workbook, at what version of it, on what date, and how it came out. A citation is a
historical fact and does not rot: it stays true when the workbook is superseded, moved, or
deleted, which is exactly what happens to the source spreadsheet a few months after a conversion.

**A workbook that is gone is cited against, never compared against.** That sentence used to be
true of the record and false of the code: :func:`check_translation` reached the live comparison in
two of its three branches without asking whether the spreadsheet was still there, so a released
notebook -- one whose workbook was deliberately retired because the notebook *is* the process now
-- reported ``not_reconciled`` for ever, with a note telling the reader to go and check the path
of a file they had chosen to delete. Worse, on a run whose hand-in digest matched the acceptance,
a comparison that could not run shadowed a perfectly good record and the panel accused somebody of
having edited the notebook. An absent workbook is not evidence of a regression; it is the absence
of evidence, and the record is what stands in its place. What it can never do is manufacture an
acceptance that was never measured: a workbook that is gone with nothing recorded is still
NOT RECONCILED, and says so in terms that do not blame the reader for it.

**This function is the generated notebook's only runtime read of the workbook, and that is load
bearing.** Swept when the above was fixed: ``WORKBOOK`` appears in a scaffolded notebook exactly
twice -- ``KEDGE_RUNS.start(workbook=WORKBOOK.name)``, which takes a name and touches no
filesystem, and the :func:`check_translation` call. The hand-ins, the contract, the drift profile,
the analysis and the run records all live in the project directory, which a release keeps. So a
released notebook is genuinely independent of its spreadsheet rather than merely intended to be --
and that is an invariant of the whole notebook resting on one line of this module. It is also the
kind that stops being true in silence: one convenient ``pl.read_excel(WORKBOOK)`` added to a stage
cell two releases from now puts the traceback back into a notebook whose workbook was deleted on
purpose, and nothing fails until somebody releases one. Anything reaching for ``WORKBOOK`` outside
these two places should be assumed wrong until it has explained itself.

**When the live check does re-run.** Only when this run is working on the same data the
acceptance ran on, matched by hand-in digest. That is the case where a failure means something —
somebody edited the notebook and broke the translation — and it is the only case where the
comparison is meaningful at all. Everything else cites.

What checks a *run* is elsewhere and always was: the hand-in contract, the drift report, the
checkpoints, and — where the process changes data — the verification of the re-extract against
what the notebook predicted. This module deliberately says so in its own output, because a user
looking at a citation needs to know what is watching today's numbers.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kedge.errors import KedgeError, ReconciliationError
from kedge.reconcile.compare import reconcile_workbook
from kedge.reconcile.model import ReconciliationReport, ReconciliationStatus, Tolerance

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "SCHEMA_VERSION",
    "Acceptance",
    "AcceptanceStore",
    "TranslationCheck",
    "check_translation",
]

SCHEMA_VERSION = "1.0"


@dataclass(frozen=True, slots=True)
class Acceptance:
    """The recorded outcome of reconciling a translation against the workbook it came from.

    ``handin_sha256`` is what makes the record usable rather than merely archival: it says which
    data the claim was measured on, so a later run can tell whether it is looking at the same
    thing. Without it the record could only ever be quoted, never re-checked.
    """

    workbook: str
    workbook_sha256: str | None
    handin_sha256: str | None
    reconciled_at: datetime
    status: str
    regions_passed: int = 0
    regions_claimed: int = 0
    regions_declared: int = 0
    rows_compared: int = 0
    tolerance: str = ""
    headline: str = ""
    notebook: str | None = None
    schema_version: str = SCHEMA_VERSION

    @property
    def passed(self) -> bool:
        return self.status == ReconciliationStatus.PASSED.value or self.status == "checked"

    def cite(self) -> str:
        """One sentence quoting the claim, with everything a reader needs to weigh it.

        The date and the workbook are not decoration. A translation reconciled against a
        spreadsheet eight months ago, before the process changed twice, is a weaker claim than
        one reconciled last week, and the reader is the one entitled to decide that.
        """
        when = self.reconciled_at.strftime("%d %b %Y")
        what = (
            f"{self.regions_passed} of {self.regions_claimed} claimed region(s) matched"
            if self.regions_claimed
            else "nothing was compared"
        )
        declared = (
            f", {self.regions_declared} declared not reproduced" if self.regions_declared else ""
        )
        return (
            f"This translation was reconciled against `{self.workbook}` on {when}: "
            f"{what}{declared}, within {self.tolerance or 'the configured tolerance'}."
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "workbook": self.workbook,
            "workbook_sha256": self.workbook_sha256,
            "handin_sha256": self.handin_sha256,
            "reconciled_at": self.reconciled_at.isoformat(),
            "status": self.status,
            "regions_passed": self.regions_passed,
            "regions_claimed": self.regions_claimed,
            "regions_declared": self.regions_declared,
            "rows_compared": self.rows_compared,
            "tolerance": self.tolerance,
            "headline": self.headline,
            "notebook": self.notebook,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Acceptance:
        try:
            stamp = datetime.fromisoformat(str(data["reconciled_at"]))
            return cls(
                workbook=str(data["workbook"]),
                workbook_sha256=data.get("workbook_sha256"),
                handin_sha256=data.get("handin_sha256"),
                reconciled_at=stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC),
                status=str(data["status"]),
                regions_passed=int(data.get("regions_passed", 0)),
                regions_claimed=int(data.get("regions_claimed", 0)),
                regions_declared=int(data.get("regions_declared", 0)),
                rows_compared=int(data.get("rows_compared", 0)),
                tolerance=str(data.get("tolerance", "")),
                headline=str(data.get("headline", "")),
                notebook=data.get("notebook"),
                schema_version=str(data.get("schema_version", SCHEMA_VERSION)),
            )
        except (KeyError, TypeError, ValueError) as error:
            msg = f"the acceptance record is missing or has a malformed field: {error}"
            raise ReconciliationError(msg) from error


class AcceptanceStore:
    """The one acceptance record for one converted workbook, on disk.

    One file, not a history. A translation has one current answer to "was this checked against
    the spreadsheet?", and re-checking after an amendment replaces it — the old answer described
    code that no longer exists. Runs are the thing there is a history of, and
    :mod:`kedge.runs` keeps that.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> Acceptance | None:
        """The recorded acceptance, or ``None`` if the translation has never been checked.

        A file that will not parse is logged and treated as absent. The alternative is a
        notebook that refuses to open because of a corrupt sidecar, and "never checked" is both
        the safe reading and the true one when the record is unreadable.
        """
        if not self._path.is_file():
            return None
        try:
            return Acceptance.from_json(json.loads(self._path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, ReconciliationError):
            logger.warning("the acceptance record at %s could not be read", self._path)
            return None

    def record(
        self,
        report: ReconciliationReport,
        *,
        handin_sha256: str | None = None,
        notebook: str | None = None,
        now: datetime | None = None,
    ) -> Acceptance:
        """Write the outcome of a live reconciliation as this translation's acceptance."""
        claimed = [
            region for region in report.regions if region not in report.declared_not_reproduced
        ]
        acceptance = Acceptance(
            workbook=Path(report.workbook).name,
            workbook_sha256=report.workbook_sha256,
            handin_sha256=handin_sha256,
            reconciled_at=now or datetime.now(UTC),
            status="checked" if not report.failed and report.passed else report.status.value,
            regions_passed=len(report.passed),
            regions_claimed=len(claimed),
            regions_declared=len(report.declared_not_reproduced),
            rows_compared=report.rows_compared,
            tolerance=report.tolerance.describe(),
            headline=report.headline(),
            notebook=notebook,
        )
        # Non-fatal. The record is how a *later* run avoids comparing against a spreadsheet that
        # does not describe its data; it is not what makes this reconciliation true. A read-only
        # directory should cost the note, never the check.
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            scratch = self._path.with_suffix(".json.tmp")
            scratch.write_text(json.dumps(acceptance.as_json(), indent=2) + "\n", encoding="utf-8")
            scratch.replace(self._path)
        except OSError as error:
            logger.warning(
                "could not record the translation acceptance at %s: %s", self._path, error
            )
        else:
            logger.info("recorded translation acceptance: %s", acceptance.cite())
        return acceptance


def _reconcile_or_explain(
    workbook_path: Path | str,
    actuals: Mapping[str, Any],
    *,
    tolerance: Tolerance | None,
    notebook: str | None,
    not_reproduced: Mapping[str, str] | None,
) -> ReconciliationReport:
    """Reconcile, or return a report saying why nothing was compared.

    :func:`reconcile_workbook` degrades gracefully for a workbook it can *open* and finds
    wanting, but raises for one that is not there at all -- and a converted notebook outlives
    the spreadsheet it came from far more often than not. A traceback out of this cell would
    take the rest of the notebook with it; a NOT_RECONCILED report saying the workbook has moved
    is the honest answer and the one a user can act on.
    """
    try:
        return reconcile_workbook(
            workbook_path,
            actuals,
            tolerance=tolerance,
            notebook=notebook,
            not_reproduced=not_reproduced,
        )
    except KedgeError as error:
        return ReconciliationReport(
            workbook=str(workbook_path),
            notebook=notebook,
            tolerance=tolerance or Tolerance(),
            notes=[
                f"Nothing was compared against the workbook: {error}",
                "This is not a pass. If the workbook has been superseded or archived, that is "
                "expected -- a translation is reconciled once, at conversion, and cited "
                "afterwards.",
            ],
        )


def _compared_anything(report: ReconciliationReport) -> bool:
    """Whether a report is evidence about the translation at all.

    A report with no verdict either way is not a live check that *failed*; it is a live check
    that could not run -- an unreadable workbook, an absent one, or every region declared not
    reproduced. The distinction is the same one :mod:`kedge.reconcile.verify` draws between a
    row that is missing and a row that is different, and getting it wrong has the same shape of
    consequence: a recorded acceptance overruled by a comparison that never happened.
    """
    return bool(report.passed or report.failed)


def _first_check_reason(*, recorded: bool) -> str:
    """Why the first live comparison ran, phrased for what it actually left behind.

    Only a reconciliation that stands up becomes an acceptance, so the cheerful "the result is
    recorded" is a lie on precisely the runs where the user most needs to know it is not.
    """
    if recorded:
        return (
            "First reconciliation of this translation against the workbook it came from. "
            "The result is recorded, and later runs on other periods will cite it rather "
            "than compare against a spreadsheet that does not describe their data."
        )
    return (
        "First reconciliation of this translation against the workbook it came from. Nothing "
        "has been recorded, because only a reconciliation that stands up becomes an acceptance; "
        "the next run will try again."
    )


def _same_data_reason(report: ReconciliationReport) -> str:
    """Why the live comparison ran on a digest match, phrased for what it established.

    "A failure here means the notebook has been edited into disagreeing with the workbook" is
    true of a comparison that *happened*. Said over one that could not run, it accuses the
    reader of tampering on the strength of a file kedge could not open.
    """
    if _compared_anything(report):
        return (
            "This run is working on the same data the translation was accepted against, so "
            "the comparison is live. A failure here means the notebook has been edited into "
            "disagreeing with the workbook."
        )
    return (
        "This run is working on the same data the translation was accepted against, so the "
        "comparison was attempted -- but nothing could be compared, so it establishes nothing "
        "either way and the acceptance above stands undisturbed."
    )


def _workbook_is_absent(workbook_path: Path | str) -> bool:
    """Whether the spreadsheet is simply not there any more.

    Deliberately narrower than "the comparison failed". A workbook that is present but corrupt,
    or present with no cached values, is an anomaly worth surfacing live exactly as it always
    was. One that is *gone* is the ordinary end of a conversion, and is the only case this
    module answers from the record instead.
    """
    try:
        return not Path(workbook_path).is_file()
    except (OSError, ValueError):  # pragma: no cover - an unusable path is an absent workbook
        return True


@dataclass(frozen=True)
class TranslationCheck:
    """What the notebook shows about reconciliation, whichever of the two cases it is in.

    One type rather than two so a generated cell does not have to branch. ``live`` is the report
    when the comparison was actually made; ``acceptance`` is the record being cited; ``applies``
    says which of those the reader is looking at.
    """

    applies: bool
    live: ReconciliationReport | None = None
    acceptance: Acceptance | None = None
    reason: str = ""
    watching_this_run: tuple[str, ...] = field(default_factory=tuple)

    @property
    def status(self) -> ReconciliationStatus:
        """This run's reconciliation status, and it is falsy unless something was compared.

        A citation is ``NOT_RECONCILED``, deliberately and always. The translation may be
        perfectly well accepted, but nothing was compared *on this run*, and
        :class:`~kedge.reconcile.model.ReconciliationStatus` is the type whose whole job is to
        be falsy when nothing was checked. Answering "was the translation accepted?" through it
        would put a truthy value in front of every caller who wrote ``if
        reconciliation.status:`` meaning "were today's numbers verified against something?".
        That question is answered by :attr:`translation_accepted`, which is named so it cannot
        be mistaken for this one.
        """
        if self.live is not None:
            return self.live.status
        return ReconciliationStatus.NOT_RECONCILED

    def __bool__(self) -> bool:
        """Falsy unless something was compared and matched, exactly like the status it wraps.

        The whole family holds this property so that ``if reconciliation:`` in a notebook, a
        panel or a CLI cannot read a citation -- or an absent workbook -- as a pass. A caller
        asking the *other* question wants :attr:`translation_accepted`, which is spelled
        differently on purpose.
        """
        return bool(self.status)

    @property
    def report(self) -> ReconciliationReport | None:
        """The live report, when one was produced. ``None`` for a citation.

        Named for what every existing caller already calls it, so a panel renderer or a test
        that reaches past the wrapper keeps working.
        """
        return self.live

    @property
    def colour(self) -> str:
        """green / amber / red, for a panel. A citation is amber: nothing was compared today."""
        if self.live is not None:
            return self.live.colour
        return "amber"

    @property
    def translation_accepted(self) -> bool:
        """Whether the *translation* is believed to reproduce the workbook it came from.

        A different question from :attr:`status`, and the reason for the long name. For a live
        check it is this run's own result; for a citation it is what was recorded, on a date the
        reader can see and weigh.

        A live report that compared *nothing* is neither. It carries no information about the
        translation, so it does not get to overrule the record -- which is the whole reason the
        record is written down. Letting it through is how a released notebook came to report a
        good acceptance as a failure, and it would do the same for any workbook that would not
        open.
        """
        if self.live is not None and _compared_anything(self.live):
            return not self.live.failed and bool(self.live.passed)
        return self.acceptance is not None and self.acceptance.passed

    def summary_line(self) -> str:
        if self.live is not None:
            return self.live.headline()
        if self.acceptance is not None:
            return self.acceptance.cite()
        return (
            "This translation has never been reconciled against the workbook it came from, so "
            "nothing is claimed about whether it reproduces the original's numbers."
        )

    def render(self) -> str:
        """The panel. A citation is short on purpose: it is not this run's business."""
        lines = ["### Translation check", self.summary_line()]
        if self.reason:
            lines.extend(["", f"_{self.reason}_"])
        if self.watching_this_run:
            lines.extend(["", "**What is checking *this* run:**", ""])
            lines.extend(f"- {item}" for item in self.watching_this_run)
        if self.live is not None:
            # `__str__` is the report's own full rendering; it has no `render`.
            lines.extend(["", str(self.live)])
        return "\n".join(lines)


def _without_the_workbook(
    recorded: Acceptance | None,
    workbook_path: Path | str,
    watching_this_run: Sequence[str],
) -> TranslationCheck:
    """The answer when the spreadsheet is gone: cite the record, or say plainly there is none.

    Two outcomes, and keeping them apart is the entire point. A recorded acceptance survives the
    workbook by design -- see this module's docstring, and :attr:`Workspace.acceptance_path` --
    so it is cited and :attr:`TranslationCheck.translation_accepted` stays true on the strength
    of it. With no record there is nothing to cite and nothing ever can be, because an acceptance
    can only be measured against the workbook it came from; that stays NOT RECONCILED, because
    non-negotiable 6 is absolute and no amount of sympathy for the reader's position makes an
    unmeasured translation an accepted one.

    Neither message tells the reader to check a path. Whether the workbook was retired on purpose
    or lost is not knowable from here, and "check the path" is wrong and slightly insulting in the
    case that this feature exists to create.

    Args:
        recorded: The acceptance on file, if there is one.
        workbook_path: Where the workbook was.
        watching_this_run: What does check today's numbers.

    Returns:
        A citation, or an honest blank.
    """
    if recorded is not None:
        return TranslationCheck(
            applies=False,
            acceptance=recorded,
            reason=(
                f"`{Path(workbook_path).name}` is no longer on disk, so nothing was compared "
                f"against it today. That is the ordinary end of a conversion rather than a "
                f"fault -- the notebook is the process now -- and the acceptance above was "
                f"written down at conversion time precisely so that it would outlive the "
                f"spreadsheet it names."
            ),
            watching_this_run=tuple(watching_this_run),
        )
    return TranslationCheck(
        applies=False,
        reason=(
            f"To record an acceptance, restore the workbook at `{workbook_path}` and run this "
            f"notebook once: the outcome is written down and cited from then on. It is not "
            f"there now, so nothing could be compared and nothing is claimed. If the "
            f"spreadsheet was retired deliberately, this translation will simply never carry "
            f"an acceptance -- one can only ever be measured against the workbook it came from."
        ),
        watching_this_run=tuple(watching_this_run),
    )


def check_translation(
    store: AcceptanceStore,
    workbook_path: Path | str,
    actuals: Mapping[str, Any],
    *,
    handin_sha256: str | None = None,
    not_reproduced: Mapping[str, str] | None = None,
    tolerance: Tolerance | None = None,
    notebook: str | None = None,
    watching_this_run: Sequence[str] = (),
) -> TranslationCheck:
    """Reconcile against the workbook, or cite the recorded acceptance, whichever is honest.

    The decision is made on **identity, not outcome**. Comparing anyway and forgiving the failure
    because "we passed once before" would mask exactly the regression this is for: somebody edits
    a translated cell and the numbers quietly stop matching the spreadsheet.

    Args:
        store: Where this translation's acceptance record lives.
        workbook_path: The workbook the notebook was converted from.
        actuals: Region id to the notebook's values, as :func:`reconcile_workbook` takes them.
        handin_sha256: The digest of the data this run is working on. When it matches the
            acceptance's, the live comparison is meaningful and is re-run; when it does not,
            this run is a different period and the workbook is not its baseline.
        not_reproduced: Regions the notebook deliberately does not reproduce, and why.
        tolerance: Tolerances for the live comparison.
        notebook: Path of the notebook, recorded with a new acceptance.
        watching_this_run: What *does* check today's numbers — the contract, the drift report,
            the verification. Named in the panel so a citation does not read as "nothing is
            being checked".

    Returns:
        A :class:`TranslationCheck`. Never raises for the ordinary cases. A workbook that is
        present but cannot be opened produces a NOT_RECONCILED live report, as it always did; a
        workbook that is *gone* is cited against rather than compared against, by
        :func:`_without_the_workbook`.
    """
    recorded = store.load()

    # Before anything else, because both branches below read the workbook off disk and a
    # released notebook -- the successful end of a conversion -- has no workbook to read.
    if _workbook_is_absent(workbook_path):
        return _without_the_workbook(recorded, workbook_path, watching_this_run)

    # Never checked: do it now, and record it if it stands up. This is the conversion-time
    # acceptance test, and the first run of a freshly converted notebook is when it happens.
    if recorded is None:
        report = _reconcile_or_explain(
            workbook_path,
            actuals,
            tolerance=tolerance,
            notebook=notebook,
            not_reproduced=not_reproduced,
        )
        keeps = bool(report.passed) and not report.failed
        if keeps:
            store.record(report, handin_sha256=handin_sha256, notebook=notebook)
        return TranslationCheck(
            applies=True,
            live=report,
            reason=_first_check_reason(recorded=keeps),
            watching_this_run=tuple(watching_this_run),
        )

    # Same data the acceptance ran on: the comparison still means something, and a failure here
    # is a real regression rather than a different month.
    if handin_sha256 is not None and handin_sha256 == recorded.handin_sha256:
        report = _reconcile_or_explain(
            workbook_path,
            actuals,
            tolerance=tolerance,
            notebook=notebook,
            not_reproduced=not_reproduced,
        )
        if report.passed and not report.failed:
            store.record(report, handin_sha256=handin_sha256, notebook=notebook)
        return TranslationCheck(
            applies=True,
            live=report,
            acceptance=recorded,
            reason=_same_data_reason(report),
            watching_this_run=tuple(watching_this_run),
        )

    return TranslationCheck(
        applies=False,
        acceptance=recorded,
        reason=(
            "This run is on different data, so the workbook is not its baseline and nothing was "
            "compared against it. That is not a gap: reconciliation asks whether the *translation* "
            "is faithful, which is a question about the conversion and was answered once, above. "
            "Whether *this* run is right is a different question, and these are what answer it."
        ),
        watching_this_run=tuple(watching_this_run),
    )
