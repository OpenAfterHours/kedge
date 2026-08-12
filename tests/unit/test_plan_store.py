"""Versioned plans on disk: readable YAML, retained history, and a seed for the next proposal.

"When the process changes next quarter, the diff of the plan is the change record" (PLAN 2.2) is
only true if last quarter's plan is still there. The store's whole job is that retention rule and
the file naming that makes a directory listing sort into version order, so both are asserted
here from the outside rather than trusted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from conftest import make_draft, make_plan
from kedge.plan.model import Approval, ApprovalState, Confidence, ProcessPlan, Stage
from kedge.plan.store import (
    PLAN_FILENAME_PATTERN,
    PlanStore,
    PlanStoreError,
    plan_from_yaml,
    plan_to_yaml,
)

# ── helpers ─────────────────────────────────────────────────────────────────


def _at(version: int, *, approved: bool = False, **overrides: Any) -> ProcessPlan:
    """A plan stamped at a version, optionally already approved."""
    plan = make_plan(version=version, **overrides)
    if not approved:
        return plan
    acknowledged = [drop.model_copy(update={"acknowledged": True}) for drop in plan.dropped]
    return plan.model_copy(
        update={
            "dropped": acknowledged,
            "approval": Approval(state=ApprovalState.APPROVED, by="phil"),
        }
    )


@pytest.fixture
def store(tmp_path: Path) -> PlanStore:
    """An empty store, at the path a workspace would put it."""
    return PlanStore(tmp_path / "rwa_monthly_v14.kedge" / "plans")


# =============================================================================
# SERIALISATION
# =============================================================================


def test_a_plan_round_trips_through_yaml_with_nothing_lost() -> None:
    plan = make_plan()
    assert plan_from_yaml(plan_to_yaml(plan)) == plan


def test_the_file_says_what_it_is_and_that_approval_gates_the_notebook() -> None:
    """The header is the first thing a user hand-editing the file reads."""
    text = plan_to_yaml(make_plan())
    assert text.startswith("# kedge process plan.")
    assert "Nothing reaches the notebook until approval.state is 'approved'" in text


def test_null_fields_are_omitted_so_a_reviewer_is_not_reading_past_them() -> None:
    plan = make_plan(
        draft=make_draft(
            stages=[Stage(id="only", intent="the whole process", confidence=Confidence.HIGH)],
            dropped=[],
            summary=None,
        )
    )
    body = plan_to_yaml(plan)
    assert "notes:" not in body
    assert "checkpoint:" not in body
    assert "summary:" not in body


def test_the_yaml_is_block_style_and_keeps_field_order_so_a_one_word_edit_is_a_one_line_diff() -> (
    None
):
    body = plan_to_yaml(make_plan())
    assert "{" not in body
    keys = list(yaml.safe_load(body))
    assert keys[:4] == ["plan_schema_version", "version", "created_at", "workbook"]


def test_a_file_that_is_not_yaml_names_the_problem_rather_than_raising_a_traceback() -> None:
    with pytest.raises(PlanStoreError, match="not valid YAML"):
        plan_from_yaml("stages: [\n  - unterminated")


def test_a_yaml_file_that_is_not_a_mapping_says_what_it_found_instead() -> None:
    with pytest.raises(PlanStoreError, match="found list"):
        plan_from_yaml("- just\n- a\n- list\n")


def test_a_hand_edited_typo_produces_a_message_naming_the_field() -> None:
    raw = yaml.safe_load(plan_to_yaml(make_plan()))
    raw["stages"][0]["confidance"] = "high"
    with pytest.raises(PlanStoreError, match="confidance"):
        plan_from_yaml(yaml.safe_dump(raw))


def test_a_hand_edited_plan_with_a_broken_stage_graph_is_refused() -> None:
    raw = yaml.safe_load(plan_to_yaml(make_plan()))
    raw["stages"][1]["depends_on"] = ["a_stage_that_does_not_exist"]
    with pytest.raises(PlanStoreError, match="unknown stage"):
        plan_from_yaml(yaml.safe_dump(raw))


# =============================================================================
# PATHS AND VERSIONS
# =============================================================================


def test_filenames_are_zero_padded_so_a_directory_listing_sorts_into_version_order(
    store: PlanStore,
) -> None:
    names = [store.path_for(version).name for version in (1, 2, 10, 100)]
    assert names == ["plan-v001.yaml", "plan-v002.yaml", "plan-v010.yaml", "plan-v100.yaml"]
    assert sorted(names) == names


@pytest.mark.parametrize(
    ("name", "matches"),
    [
        ("plan-v001.yaml", True),
        ("plan-v7.yml", True),
        ("plan-v001.yaml.bak", False),
        ("plan.yaml", False),
        ("analysis.json", False),
        ("plan-vX.yaml", False),
    ],
)
def test_only_plan_files_are_recognised_as_versions(name: str, matches: bool) -> None:
    assert bool(PLAN_FILENAME_PATTERN.match(name)) is matches


def test_an_absent_directory_is_an_empty_history_rather_than_an_error(store: PlanStore) -> None:
    assert store.versions() == []
    assert store.next_version() == 1
    assert store.latest() is None
    assert store.latest_approved() is None
    assert store.history() == []
    assert store.seed() is None


def test_versions_are_returned_ascending_and_ignore_anything_else_in_the_directory(
    store: PlanStore,
) -> None:
    store.save(_at(1))
    store.save(_at(10))
    store.save(_at(2))
    store.directory.joinpath("notes.md").write_text("not a plan", encoding="utf-8")

    assert store.versions() == [1, 2, 10]
    assert store.next_version() == 11


# =============================================================================
# READING
# =============================================================================


def test_asking_for_a_version_that_is_not_there_lists_the_ones_that_are(
    store: PlanStore,
) -> None:
    store.save(_at(1))
    store.save(_at(2))
    with pytest.raises(PlanStoreError, match=r"available: 1, 2"):
        store.load(3)


def test_an_empty_store_says_so_rather_than_listing_nothing(store: PlanStore) -> None:
    store.directory.mkdir(parents=True)
    with pytest.raises(PlanStoreError, match="available: none"):
        store.load(1)


def test_an_unreadable_plan_file_names_the_path_it_came_from(store: PlanStore) -> None:
    store.directory.mkdir(parents=True)
    store.path_for(1).write_text("just a string", encoding="utf-8")
    with pytest.raises(PlanStoreError) as caught:
        store.load(1)
    assert "plan-v001.yaml" in str(caught.value)


def test_a_plan_file_that_cannot_be_read_at_all_names_the_path(
    store: PlanStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.save(_at(1))

    def refuse(self: Path, *args: Any, **kwargs: Any) -> str:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", refuse)
    with pytest.raises(PlanStoreError, match="could not read the plan file"):
        store.load(1)


def test_latest_is_the_highest_version_not_the_most_recently_written(store: PlanStore) -> None:
    store.save(_at(3))
    store.save(_at(1))
    latest = store.latest()
    assert latest is not None
    assert latest.version == 3


def test_latest_approved_skips_later_unapproved_drafts(store: PlanStore) -> None:
    """The scaffolder is pointed at this, so a later draft must not shadow the approved plan."""
    store.save(_at(1, approved=True))
    store.save(_at(2))
    approved = store.latest_approved()
    assert approved is not None
    assert approved.version == 1


def test_latest_approved_prefers_the_most_recent_of_several(store: PlanStore) -> None:
    store.save(_at(1, approved=True))
    store.save(_at(2, approved=True))
    store.save(_at(3))
    approved = store.latest_approved()
    assert approved is not None
    assert approved.version == 2


def test_the_history_is_every_version_oldest_first(store: PlanStore) -> None:
    for version in (2, 1, 3):
        store.save(_at(version))
    assert [plan.version for plan in store.history()] == [1, 2, 3]


# =============================================================================
# WRITING: HISTORY IS RETAINED, NOT OVERWRITTEN
# =============================================================================


def test_saving_creates_the_directory_and_returns_the_path(store: PlanStore) -> None:
    path = store.save(_at(1))
    assert path == store.path_for(1)
    assert path.is_file()
    assert store.directory.is_dir()


def test_saving_the_same_content_again_is_a_no_op(store: PlanStore) -> None:
    """An idempotent pipeline should not have to check first."""
    plan = _at(1)
    first = store.save(plan)
    written = first.stat().st_mtime_ns
    assert store.save(plan) == first
    assert first.stat().st_mtime_ns == written


def test_overwriting_a_version_with_different_content_is_refused_and_says_what_to_do_instead(
    store: PlanStore,
) -> None:
    store.save(_at(1))
    with pytest.raises(PlanStoreError) as caught:
        store.save(_at(1, draft=make_draft(summary="a different summary")))
    message = str(caught.value)
    assert "plan history is retained rather than overwritten" in message
    assert "Save at version 2 instead" in message


def test_overwrite_is_permitted_when_recording_an_approval_against_the_same_version(
    store: PlanStore,
) -> None:
    """Approval does not bump the version, so it lands on top of the file already there."""
    store.save(_at(1))
    store.save(_at(1, approved=True), overwrite=True)
    reloaded = store.load(1)
    assert reloaded.approval.approved


def test_no_temporary_file_is_left_behind(store: PlanStore) -> None:
    store.save(_at(1))
    assert [path.name for path in store.directory.iterdir()] == ["plan-v001.yaml"]


def test_a_write_that_fails_names_the_file(store: PlanStore, monkeypatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", refuse)
    with pytest.raises(PlanStoreError, match="could not write the plan file"):
        store.save(_at(1))


def test_save_next_renumbers_a_plan_that_would_have_clobbered_an_existing_version(
    store: PlanStore,
) -> None:
    """A plan proposed against an empty store is v1; by the time it is saved a hand-written one
    may be there."""
    store.save(_at(1))
    stamped, path = store.save_next(
        _at(1, draft=make_draft(summary="proposed while v1 already existed"))
    )
    assert stamped.version == 2
    assert stamped.based_on_version == 1
    assert path.name == "plan-v002.yaml"
    assert store.load(1).summary != stamped.summary


def test_save_next_leaves_a_correctly_numbered_plan_alone(store: PlanStore) -> None:
    plan = _at(1)
    stamped, path = store.save_next(plan)
    assert stamped is plan
    assert path.name == "plan-v001.yaml"


# =============================================================================
# RE-SEEDING THE NEXT PROPOSAL
# =============================================================================


def test_the_seed_is_the_most_recent_approved_plan(store: PlanStore) -> None:
    """Feeding a rejected draft back would re-seed the model with what the user turned down."""
    store.save(_at(1, approved=True))
    store.save(_at(2))
    seed = store.seed()
    assert seed is not None
    assert seed.version == 1


def test_the_seed_falls_back_to_the_latest_of_any_state_when_none_is_approved(
    store: PlanStore,
) -> None:
    store.save(_at(1))
    store.save(_at(2))
    seed = store.seed()
    assert seed is not None
    assert seed.version == 2


# =============================================================================
# WIRING
# =============================================================================


def test_a_store_is_built_from_a_workspace_without_importing_one(tmp_path: Path) -> None:
    """Typed loosely on purpose: the only thing the store needs from a workspace is one path."""

    class _JustAPath:
        plans_dir = tmp_path / "plans"

    assert PlanStore.for_workspace(_JustAPath()).directory == tmp_path / "plans"


def test_a_saved_plan_reloads_as_the_same_object_including_review_state(store: PlanStore) -> None:
    """The end-to-end claim: a plan loaded from disk carries its own approvability with it."""
    approved = _at(4, approved=True)
    store.save(approved)
    reloaded = store.load(4)
    assert reloaded == approved
    assert reloaded.is_approvable
    assert reloaded.approval.by == "phil"
