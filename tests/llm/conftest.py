"""The corpus sweep behind ``pytest -m llm``: triage + propose over every committed fixture.

PLAN 7.4 asks for exactly one thing before plan-to-cells is trusted: *"Run triage + propose
across the whole M1 corpus and read the plans. This is the cheapest possible test of whether the
idea works: if the plans are sensible on five dissimilar workbooks, the code generation is a
solvable problem; if they aren't, no amount of downstream engineering fixes it."*

This module holds the fixtures for that. It calls the configured model once per fixture, writes
every plan somewhere a human can actually read it, and hands the results to
``test_plan_judgement.py`` to assert the structural and relational properties that survive model
nondeterminism. Nothing here asserts on phrasing: the same workbook planned twice will not
produce the same words, and a test that demands it would be measuring the wrong thing. The result
types and the artifact writing live in ``_sweep.py``, whose docstring says why.

Three decisions worth knowing about before reading the code:

- **The endpoint comes from :mod:`kedge.config` and nowhere else.** The base URL is the resolved
  ``model.base_url`` and the key is whatever :func:`kedge.config.get_api_key` finds in the OS
  keyring under ``model.api_key_ref``. There is no second mechanism, no environment variable and
  no file — and when the keyring entry is absent every test here skips with the command that
  would create it.
- **Each fixture is analysed in a directory of its own.** ``tests/fixtures/`` is a shared bucket:
  ``documented.xlsx``'s companion ``procedure_legacy.doc`` sits beside all eight workbooks, so
  analysing in place makes every workbook inherit every other workbook's companion files. A real
  user's directory does not look like that. Each workbook is therefore copied to a temporary
  directory with its own declared companions and analysed there.
- **A ``STOP`` verdict is forced rather than skipped.** PLAN 7.4 wants the plans read for the
  whole corpus, including the workbooks kedge would decline. The refusal itself is asserted
  separately, so forcing here does not hide it.

Cost control: set ``KEDGE_LLM_FIXTURES`` to a comma-separated list of fixture filenames to sweep
a subset while iterating on prompts.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from _sweep import (
    PlanOutcome,
    RecordingCompleter,
    run_directory,
    selected_fixtures,
    write_artifacts,
    write_index,
)

from kedge.analysis.analyse import analyse
from kedge.config import (
    KEYRING_SERVICE,
    Config,
    api_key_status,
    keyring_set_command,
    load_config,
    user_config_path,
)
from kedge.plan.propose import (
    ProposalError,
    ProposalRefusedError,
    completer_from_config,
    propose_plan,
)
from kedge.plan.review import review_warnings
from kedge.plan.triage import triage

if TYPE_CHECKING:
    from kedge.analysis.model import WorkbookAnalysis
    from kedge.plan.propose import Completer

_ARTIFACTS = pytest.StashKey[Path]()


# =============================================================================
# THE ENDPOINT
# =============================================================================


@pytest.fixture(scope="session")
def model_config() -> Config:
    """The resolved config, or a skip naming the command that would configure it.

    Deliberately a fixture rather than a module-level check: collection must succeed on a machine
    with no endpoint at all, so that ``pytest -m llm --collect-only`` still reports what this
    harness would run.
    """
    config = load_config().config
    status = api_key_status(config)
    if status != "present":
        pytest.skip(
            f"no model endpoint configured: the OS keyring has no entry '{KEYRING_SERVICE}/"
            f"{config.model.api_key_ref}' ({status}). Set the endpoint in {user_config_path()} "
            f"and store the key with `{keyring_set_command(config.model.api_key_ref)}`."
        )
    return config


@pytest.fixture(scope="session")
def completer(model_config: Config) -> Completer:
    """One completer for the whole sweep, so a degraded structured-output mode is learned once."""
    return completer_from_config(model_config)


# =============================================================================
# THE CORPUS
# =============================================================================


@pytest.fixture(scope="session")
def corpus_analyses(tmp_path_factory: pytest.TempPathFactory) -> dict[str, WorkbookAnalysis]:
    """Analyse every selected fixture, each in a directory containing only its own files.

    See the module docstring: analysing in ``tests/fixtures/`` in place makes every workbook
    inherit every other workbook's companion files, which no real project directory does.
    """
    root = tmp_path_factory.mktemp("corpus")
    analyses: dict[str, WorkbookAnalysis] = {}
    for expectation in selected_fixtures():
        directory = root / Path(expectation.filename).stem
        directory.mkdir()
        shutil.copy2(expectation.path, directory / expectation.filename)
        for companion in expectation.companion_files:
            shutil.copy2(expectation.path.parent / companion, directory / companion)
        analyses[expectation.filename] = analyse(directory / expectation.filename)
    return analyses


@pytest.fixture(scope="session")
def artifact_dir(request: pytest.FixtureRequest) -> Path:
    """Where the plans are written for a human to read, per PLAN 7.4."""
    directory = run_directory()
    request.session.config.stash[_ARTIFACTS] = directory
    return directory


@pytest.fixture(scope="session")
def corpus_plans(
    model_config: Config,
    completer: Completer,
    corpus_analyses: dict[str, WorkbookAnalysis],
    artifact_dir: Path,
) -> dict[str, PlanOutcome]:
    """Triage and propose across the whole corpus. One model call per workbook, plus repairs."""
    outcomes: dict[str, PlanOutcome] = {}
    for expectation in selected_fixtures():
        analysis = corpus_analyses[expectation.filename]
        assessment = triage(analysis)
        recorder = RecordingCompleter(completer)
        try:
            plan = propose_plan(
                analysis,
                completer=recorder,
                triage_result=assessment,
                model=model_config.model.model,
                force=assessment.should_stop,
            )
        except (ProposalError, ProposalRefusedError) as exc:
            outcomes[expectation.filename] = PlanOutcome(
                fixture=expectation,
                analysis=analysis,
                triage=assessment,
                responses=tuple(recorder.responses),
                error=str(exc),
            )
        else:
            outcomes[expectation.filename] = PlanOutcome(
                fixture=expectation,
                analysis=analysis,
                triage=assessment,
                plan=plan,
                forced=assessment.should_stop,
                warnings=tuple(review_warnings(plan, analysis, triage_result=assessment)),
                responses=tuple(recorder.responses),
            )
        write_artifacts(artifact_dir, outcomes[expectation.filename])

    write_index(artifact_dir, outcomes)
    return outcomes


def pytest_terminal_summary(terminalreporter: Any, exitstatus: int, config: pytest.Config) -> None:
    """Say where the plans were written. Reading them is the whole point of PLAN 7.4."""
    directory = config.stash.get(_ARTIFACTS, None)
    if directory is None:
        return
    terminalreporter.write_sep("-", "plan judgement artifacts")
    terminalreporter.write_line(f"plans written to: {directory}")
    terminalreporter.write_line(f"start with:       {directory / 'index.md'}")
