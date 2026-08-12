"""The machinery behind the corpus sweep: what a run produces, and where it is written.

Split out of ``conftest.py`` for one reason that is not tidiness. pytest imports every
``conftest.py`` under the *same* module name, so a test module doing ``from conftest import X``
gets whichever directory's conftest reached ``sys.modules`` first — and mixing test directories on
one command line then breaks collection in whichever direction loses the race. Everything the
judgement module needs to import by name therefore lives here, under a name nothing else in the
repository uses, and ``conftest.py`` keeps only the fixtures pytest resolves positionally.

Nothing in this module talks to a model. The completer is injected; see ``conftest.py``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from manifest import FixtureExpectation, iter_fixtures

from kedge.plan.propose import record_responses
from kedge.plan.review import render_plan
from kedge.plan.store import plan_to_yaml

if TYPE_CHECKING:
    from kedge.analysis.model import WorkbookAnalysis
    from kedge.plan.model import ProcessPlan
    from kedge.plan.propose import Completer, CompletionRequest
    from kedge.plan.triage import TriageResult

ARTIFACT_DIR_ENV = "KEDGE_LLM_ARTIFACTS"
"""Root for the plans a human is meant to read. A directory under the system temp when unset."""

FIXTURE_SELECTION_ENV = "KEDGE_LLM_FIXTURES"
"""Comma-separated fixture filenames, for sweeping a subset while iterating on prompts."""

DEFAULT_ARTIFACT_ROOT = "kedge-plan-judgement"
"""Subdirectory of the system temp directory used when ``KEDGE_LLM_ARTIFACTS`` is unset."""


# =============================================================================
# THE SWEEP RESULT
# =============================================================================


@dataclass(frozen=True, slots=True)
class PlanOutcome:
    """What the planning loop produced for one corpus workbook.

    ``plan`` is None exactly when the proposal failed, and ``error`` says why. A failure is
    recorded rather than raised so that one bad workbook does not hide the other seven — the
    point of a corpus sweep is to see all of it at once.
    """

    fixture: FixtureExpectation
    analysis: WorkbookAnalysis
    triage: TriageResult
    plan: ProcessPlan | None = None
    forced: bool = False
    """Whether triage said STOP and the sweep proposed anyway (PLAN 7.4 wants the plan read)."""
    warnings: tuple[str, ...] = ()
    responses: tuple[str, ...] = ()
    """Every raw model response, in order. More than one means the plan needed repairing."""
    error: str | None = None

    @property
    def filename(self) -> str:
        """The fixture workbook's filename, as the manifest names it."""
        return self.fixture.filename

    @property
    def attempts(self) -> int:
        """How many times the model was asked. Above one, the first answer did not validate."""
        return len(self.responses)


@dataclass(slots=True)
class RecordingCompleter:
    """Wraps a :class:`~kedge.plan.propose.Completer`, keeping every raw response.

    The captured text is written beside the plan, which is what makes a live run replayable
    offline through :meth:`~kedge.plan.propose.ScriptedCompleter.from_paths` and what lets a
    human see what the model actually said when a plan looks wrong.
    """

    inner: Completer
    responses: list[str] = field(default_factory=list)

    def complete(self, request: CompletionRequest) -> str:
        """Delegate, recording the response."""
        text = self.inner.complete(request)
        self.responses.append(text)
        return text


# =============================================================================
# THE CORPUS
# =============================================================================


def selected_fixtures() -> tuple[FixtureExpectation, ...]:
    """The fixtures to sweep, honouring ``KEDGE_LLM_FIXTURES``."""
    everything = iter_fixtures()
    raw = os.environ.get(FIXTURE_SELECTION_ENV, "").strip()
    if not raw:
        return everything
    wanted = {name.strip() for name in raw.split(",") if name.strip()}
    chosen = tuple(item for item in everything if item.filename in wanted)
    if not chosen:
        known = ", ".join(item.filename for item in everything)
        pytest.skip(f"{FIXTURE_SELECTION_ENV}={raw!r} matched no fixture; the corpus is: {known}")
    return chosen


# =============================================================================
# ARTIFACTS: THE THING PLAN 7.4 ACTUALLY ASKS A HUMAN TO READ
# =============================================================================


def run_directory(now: datetime | None = None) -> Path:
    """A fresh, stamped directory for this run's artifacts.

    Stamped rather than reused because ``KEDGE_LLM_ARTIFACTS`` is usually a stable path and two
    runs sharing one directory is how a reader ends up looking at half of each: a workbook that
    planned last time and failed this time would keep its old ``plan.txt`` sitting beside the new
    ``FAILED.txt``, and a shorter run would leave the previous run's extra ``response-NN.json``
    behind. Old runs are kept — comparing two sweeps after a prompt change is the point.
    """
    configured = os.environ.get(ARTIFACT_DIR_ENV, "").strip()
    root = (
        Path(configured).expanduser()
        if configured
        else Path(tempfile.gettempdir()) / DEFAULT_ARTIFACT_ROOT
    )
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    directory = root / f"run-{stamp}"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def write_artifacts(root: Path, outcome: PlanOutcome) -> None:
    """Write one workbook's triage, plan and raw model output.

    The per-workbook directory is emptied first. Belt and braces alongside the run stamp: a
    second sweep within the same second, or a hand-picked path reused deliberately, must not
    leave a stale plan beside a fresh failure.
    """
    directory = root / Path(outcome.filename).stem
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "triage.txt").write_text(outcome.triage.explain(), encoding="utf-8")

    if outcome.plan is None:
        (directory / "FAILED.txt").write_text(outcome.error or "unknown", encoding="utf-8")
    else:
        (directory / "plan.txt").write_text(
            render_plan(outcome.plan, analysis=outcome.analysis, triage_result=outcome.triage),
            encoding="utf-8",
        )
        (directory / "plan.yaml").write_text(plan_to_yaml(outcome.plan), encoding="utf-8")

    if outcome.responses:
        record_responses(list(outcome.responses), directory / "responses")


def write_index(root: Path, outcomes: dict[str, PlanOutcome]) -> None:
    """A one-screen summary, because the first question is always "did the shapes differ?"."""
    lines = [
        "# Plan judgement sweep (PLAN 7.4)",
        "",
        "Triage and propose over the committed fixture corpus. Read `plan.txt` in each",
        "directory; the numbers below are only there to tell you which one to read first.",
        "",
        "| workbook | verdict | triage convertible | stages | checkpoints | questions | "
        "drops | plan convertible | attempts |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name, outcome in outcomes.items():
        plan = outcome.plan
        if plan is None:
            lines.append(
                f"| {name} | {outcome.triage.verdict.value} | "
                f"{outcome.triage.convertible:.2f} | FAILED | - | - | - | - | "
                f"{outcome.attempts} |"
            )
            continue
        verdict = outcome.triage.verdict.value + (" (forced)" if outcome.forced else "")
        lines.append(
            f"| {name} | {verdict} | {outcome.triage.convertible:.2f} | "
            f"{len(plan.stages)} | {len(plan.checkpoints)} | {len(plan.open_questions)} | "
            f"{len(plan.dropped)} | {plan.assessment.convertible:.2f} | {outcome.attempts} |"
        )

    lines.extend(
        [
            "",
            "`attempts` above one means the model's first answer did not validate and was sent "
            "back for repair;",
            "`responses/response-01.json` is what it said before the correction.",
            "",
            "## Review warnings",
            "",
        ]
    )
    for name, outcome in outcomes.items():
        if outcome.plan is None:
            body = [f"- proposal failed: {outcome.error}"]
        else:
            body = [f"- {warning}" for warning in outcome.warnings] or ["- none"]
        lines.extend([f"### {name}", "", *body, ""])
    root.joinpath("index.md").write_text("\n".join(lines), encoding="utf-8")
