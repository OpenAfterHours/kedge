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
from kedge.plan.model import (
    PLAN_SCHEMA_VERSION,
    Approval,
    ApprovalState,
    Confidence,
    ProcessPlan,
    SourceOrigin,
    Stage,
    StageSource,
)
from kedge.plan.review import approve, edit_stage
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


def test_a_plan_that_fails_validation_is_summarised_rather_than_dumped_verbatim() -> None:
    """A hand-editing user gets a list of what is wrong, not pydantic's five-part dump with a
    documentation URL under each one."""
    raw = yaml.safe_load(plan_to_yaml(make_plan()))
    raw["version"] = 0
    raw["stages"][0]["confidance"] = "high"
    del raw["workbook"]

    with pytest.raises(PlanStoreError) as caught:
        plan_from_yaml(yaml.safe_dump(raw))

    message = str(caught.value)
    assert "errors.pydantic.dev" not in message, "the links are noise to somebody editing YAML"
    assert "3 problems" in message
    assert "stages.0.confidance" in message
    assert "workbook" in message


def test_only_the_first_few_validation_problems_are_listed() -> None:
    """A file edited into the wrong shape entirely should not print a screenful."""
    raw = yaml.safe_load(plan_to_yaml(make_plan()))
    for index in range(9):
        raw[f"invented_{index}"] = "x"

    with pytest.raises(PlanStoreError) as caught:
        plan_from_yaml(yaml.safe_dump(raw))

    message = str(caught.value)
    assert "9 problems" in message
    assert "(+4 more)" in message


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


def test_a_plan_file_that_is_not_utf_8_says_so_rather_than_raising_a_traceback(
    store: PlanStore,
) -> None:
    """Realistic rather than theoretical: the header invites hand-editing, the file is written
    with `allow_unicode`, and a Windows editor saving cp1252 with a pound sign in a note
    produces exactly this. `UnicodeDecodeError` is a `ValueError`, so `except OSError` misses it.
    """
    store.save(_at(1))
    store.path_for(1).write_bytes(plan_to_yaml(_at(1)).encode("cp1252").replace(b"lookup", b"\xf8"))

    with pytest.raises(PlanStoreError) as caught:
        store.load(1)

    message = str(caught.value)
    assert "plan-v001.yaml" in message
    assert "UTF-8" in message
    assert "0xf8" in message, "which byte, so the user can find it"


def test_saving_over_a_plan_file_that_is_not_utf_8_names_the_file_too(store: PlanStore) -> None:
    """`save` reads the file at that version to decide whether anything changed."""
    store.save(_at(1))
    store.path_for(1).write_bytes(b"# a plan saved as cp1252: \xa3\n")

    with pytest.raises(PlanStoreError, match="UTF-8"):
        store.save(_at(1, draft=make_draft(summary="a different summary")))


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


def test_an_approval_cannot_be_written_over_the_version_it_approves(store: PlanStore) -> None:
    """The decision record is the versions, so a decision may not land on top of another.

    `save` used to take `overwrite=True` for exactly this, on the reading that an approval is a
    decision about a version rather than a new one. That erased whatever decision was already
    recorded there: approve, request changes, approve again left one line on disk and no trace of
    the two before it -- including the one that un-approved a plan a notebook may already have
    been scaffolded from.
    """
    store.save(_at(1))
    with pytest.raises(PlanStoreError, match="a version's decision record is the versions around"):
        store.save(_at(1, approved=True))
    assert store.load(1).approval.approved is False, "the draft on disk is untouched"


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


# ── the schema stamp on a file written today ────────────────────────────────


def test_a_new_file_derived_from_a_1_0_plan_is_stamped_at_todays_schema(store: PlanStore) -> None:
    """The stamp dates a file. Propagated, a v7 written this afternoon still claims 1.0.

    `_revise` rebuilds from `model_dump`, which carries the old stamp across every content edit,
    so without a stamp at the point a new file is created the claim survives indefinitely and any
    future migration keyed on it mis-handles a file it has never seen the shape of.
    """
    store.directory.mkdir(parents=True)
    store.path_for(1).write_text(_PLAN_V1_0_YAML, encoding="utf-8")
    loaded = store.load(1)
    assert loaded.plan_schema_version == "1.0"

    edited = edit_stage(loaded, "apply_haircuts", intent="Collateral haircuts, by asset class")
    stamped, path = store.save_next(edited)

    assert path.name == "plan-v002.yaml"
    assert stamped.plan_schema_version == PLAN_SCHEMA_VERSION == "1.1"
    assert "plan_schema_version: '1.1'" in path.read_text(encoding="utf-8")
    assert store.load(1).plan_schema_version == "1.0"


def test_the_approval_path_stamps_the_schema_too(store: PlanStore) -> None:
    """`approve` uses `model_copy` and never goes through `_revise`, so fixing `_revise` alone
    would leave the whole approval route stamping 1.0 on a file written today."""
    store.directory.mkdir(parents=True)
    store.path_for(1).write_text(_PLAN_V1_0_YAML, encoding="utf-8")

    approved = approve(store.load(1), by="phil")
    stamped, _ = store.save_next(approved)

    assert stamped.approval.approved
    assert stamped.plan_schema_version == PLAN_SCHEMA_VERSION


def test_saving_a_1_0_plan_at_its_own_version_leaves_its_stamp_alone(store: PlanStore) -> None:
    """`save` writes a plan *at* its version; only `save_next` creates a new file.

    Stamping here would rewrite history — and break the idempotent re-save `save` promises, since
    the bytes on disk would no longer match the plan that produced them.
    """
    plan = _at(1).model_copy(update={"plan_schema_version": "1.0"})

    path = store.save(plan)

    assert store.load(1).plan_schema_version == "1.0"
    assert store.save(plan) == path  # and the re-save is still a no-op, not a refusal


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


# =============================================================================
# SOURCES, ACROSS THE SCHEMA VERSIONS THEY WERE WRITTEN IN
# =============================================================================


_PLAN_V1_0_YAML = """\
# kedge process plan. Edit freely: this file is a review artifact, not generated code.
plan_schema_version: '1.0'
version: 1
created_at: '2026-06-30T09:00:00Z'
workbook: rwa_monthly_v13.xlsx
workbook_sha256: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
analysis_schema_version: '1.0'
generated_by: llm
assessment:
  convertible: 0.8
  blockers: []
stages:
- id: load_handin
  intent: Read counterparty exposures from the hand-in
  kind: load
  sources:
  - handin
  confidence: high
- id: apply_haircuts
  intent: Collateral haircut lookup by asset class
  kind: transform
  sources:
  - Calc!H2:H500
  - Ref!A1:D50
  - load_handin
  depends_on:
  - load_handin
  confidence: high
open_questions:
- question: Column AF is computed but never referenced. Dead, or read manually?
dropped: []
approval:
  state: approved
  by: phil
  at: '2026-06-30T11:15:00Z'
"""
"""Last quarter's plan, as schema 1.0 wrote it. Not derived from a dump: the point of the test is
a file somebody committed months ago, so it is spelled out the way that file is."""


def test_a_plan_file_written_before_sources_had_origins_still_opens(store: PlanStore) -> None:
    """The failure this guards against is quiet and total: a history that will not load."""
    store.directory.mkdir(parents=True)
    store.path_for(1).write_text(_PLAN_V1_0_YAML, encoding="utf-8")

    plan = store.load(1)

    assert plan.plan_schema_version == "1.0"
    assert plan.approval.approved
    assert [source.render() for source in plan.stages[0].sources] == ["handin"]
    assert [source.render() for source in plan.stages[1].sources] == [
        "range Calc!H2:H500",
        "range Ref!A1:D50",
        "stage load_handin",
    ]
    assert store.latest_approved() == plan
    assert store.seed() == plan


def test_an_old_plan_reseeds_the_next_one_with_its_sources_carried_across(
    store: PlanStore,
) -> None:
    """`to_draft()` is how last quarter's plan is offered to the model; origins go with it."""
    store.directory.mkdir(parents=True)
    store.path_for(1).write_text(_PLAN_V1_0_YAML, encoding="utf-8")

    draft = store.load(1).to_draft()

    assert draft.stages[1].upstream_stage_ids == ["load_handin"]


def test_a_source_origin_survives_the_trip_to_disk_and_back(store: PlanStore) -> None:
    plan = make_plan(
        draft=make_draft(
            stages=[
                Stage(
                    id="load_exposures",
                    intent="Pull exposures from the warehouse",
                    sources=[
                        StageSource(origin=SourceOrigin.QUERY, ref="MonthlyExposures"),
                        StageSource(origin=SourceOrigin.POWER_QUERY, ref="Ratings"),
                        StageSource(origin=SourceOrigin.MANUAL, ref="Adjustments!B2:B15"),
                        StageSource(origin=SourceOrigin.HANDIN),
                    ],
                    confidence=Confidence.HIGH,
                )
            ],
            dropped=[],
        )
    )
    store.save(plan)

    reloaded = store.load(1)
    assert reloaded == plan
    assert [source.origin for source in reloaded.stages[0].sources] == [
        SourceOrigin.QUERY,
        SourceOrigin.POWER_QUERY,
        SourceOrigin.MANUAL,
        SourceOrigin.HANDIN,
    ]


def test_an_origin_that_names_nothing_writes_no_null_line_for_a_reviewer_to_read_past() -> None:
    plan = make_plan(
        draft=make_draft(
            stages=[
                Stage(
                    id="load_handin",
                    intent="Read the hand-in",
                    sources=[StageSource(origin=SourceOrigin.HANDIN)],
                    confidence=Confidence.HIGH,
                )
            ],
            dropped=[],
        )
    )
    body = plan_to_yaml(plan)
    # The load-bearing half. The other half only asserts the origin survived at all — spelled
    # without PyYAML's indentation, which is its choice to change and not what this test is about.
    assert "ref: null" not in body
    assert "origin: handin" in body
