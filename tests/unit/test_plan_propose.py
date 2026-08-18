"""The one step of the planning loop that needs a model, exercised entirely offline.

`propose_plan` is a context builder, a prompt assembler, a parser and a retry loop wrapped round
a single LLM call, and the call itself is a `Completer` protocol. Every one of those parts is
tested here through `ScriptedCompleter` or a stub endpoint, because a module that can only be
tested against a live model is a module that does not get tested (PLAN M2, 7.4).

The live-model half — whether the plans are any *good* — lives in `tests/llm/`.
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import BadRequestError, OpenAIError
from pydantic import ValidationError

from conftest import make_analysis, make_draft, make_finding, make_operation, make_profile
from kedge.analysis.model import (
    CachedValueCoverage,
    Connection,
    DependencyGraph,
    ExcelPattern,
    Finding,
    FindingKind,
    Inconsistency,
    NamedRange,
    ProcessNote,
    Severity,
)
from kedge.config import Config, ModelConfig
from kedge.plan import PlanRun, iter_plan_history, load_analysis, run_plan
from kedge.plan.model import (
    Assessment,
    PlanDraft,
    PlanError,
    ProcessPlan,
    SourceOrigin,
    Stage,
    StageSource,
)
from kedge.plan.propose import (
    CompletionRequest,
    OpenAICompleter,
    ProposalError,
    ProposalRefusedError,
    ScriptedCompleter,
    build_messages,
    build_proposal_context,
    completer_from_config,
    load_prompt,
    parse_draft,
    plan_json_schema,
    propose_plan,
    record_responses,
    scripted_from_plan,
)
from kedge.plan.triage import TriageResult, TriageVerdict, triage
from kedge.workspace import Workspace

# ── helpers ─────────────────────────────────────────────────────────────────


def _stub_endpoint(*outcomes: Any) -> SimpleNamespace:
    """Stand in for `OpenAI().chat`, replaying strings and raising exceptions in order."""
    calls: list[dict[str, Any]] = []
    remaining = list(outcomes)

    def create(**kwargs: Any) -> Any:
        calls.append(kwargs)
        outcome = remaining.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            return SimpleNamespace(choices=[])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=outcome))])

    endpoint = SimpleNamespace(completions=SimpleNamespace(create=create))
    return SimpleNamespace(chat=endpoint, calls=calls)


def _bad_request() -> BadRequestError:
    """The rejection an endpoint that does not implement `response_format` sends back."""
    response = httpx.Response(400, request=httpx.Request("POST", "https://example.invalid/v1"))
    return BadRequestError("response_format is not supported", response=response, body=None)


def _temperature_rejected(*, param: bool = True) -> BadRequestError:
    """What a reasoning model answers an explicit `temperature` with, verbatim.

    Transcribed from a real 400 off this project's own configured endpoint. `param=False` is the
    same refusal from a proxy that returns a bare message with no structured error body.
    """
    response = httpx.Response(400, request=httpx.Request("POST", "https://example.invalid/v1"))
    message = (
        "Unsupported value: 'temperature' does not support 0.2 with this model. "
        "Only the default (1) value is supported."
    )
    body = (
        {"error": {"message": message, "param": "temperature", "code": "unsupported_value"}}
        if param
        else None
    )
    return BadRequestError(message, response=response, body=body)


def _completer_over(*outcomes: Any) -> tuple[OpenAICompleter, SimpleNamespace]:
    completer = OpenAICompleter(base_url="https://example.invalid/v1", api_key="k", model="m")
    client = _stub_endpoint(*outcomes)
    # Reaching past the constructor is the point: the SDK client is the seam being faked.
    completer._client = client
    return completer, client


# =============================================================================
# CONTEXT: profiles, not values
# =============================================================================


def test_the_context_carries_structure_and_never_raw_cell_values(analysis) -> None:
    """PLAN 2.3: the model reasons over shape, not over 200,000 rows of data."""
    context = build_proposal_context(analysis, triage(analysis))

    assert set(context) >= {
        "summary",
        "operations",
        "column_profiles",
        "findings",
        "named_ranges",
        "connections",
        "power_query",
        "process_notes",
        "dependency_graph",
        "triage",
    }
    profile = context["column_profiles"][0]
    assert "head" not in profile
    assert "tail" not in profile
    assert profile["top_values"] == [[0.1, 200], [0.15, 120]]


def test_sample_values_are_included_only_when_explicitly_asked_for(analysis) -> None:
    context = build_proposal_context(analysis, triage(analysis), include_sample_values=True)
    assert context["column_profiles"][0]["head"] == [0.1, 0.15, 0.1]


def test_a_redacted_column_contributes_shape_but_no_values() -> None:
    """Redaction is a data-handling control that must survive the digest (PLAN 2.3)."""
    analysis = make_analysis(profiles=[make_profile(redacted=True)])
    profile = build_proposal_context(analysis, triage(analysis), include_sample_values=True)[
        "column_profiles"
    ][0]
    assert profile["redacted"] is True
    assert "top_values" not in profile
    assert "head" not in profile


def test_operations_are_ranked_by_fan_out_so_truncation_drops_the_least_read() -> None:
    """A region twenty others depend on matters more than one nothing reads."""
    analysis = make_analysis(
        operations=[
            make_operation("quiet", downstream_ref_count=0),
            make_operation("loud", downstream_ref_count=40),
            make_operation("middling", downstream_ref_count=7),
        ]
    )
    context = build_proposal_context(analysis, triage(analysis), max_operations=2)
    assert [operation["id"] for operation in context["operations"]] == ["loud", "middling"]
    assert context["operations_omitted"] == 1


def test_truncated_profiles_say_so_rather_than_looking_complete() -> None:
    analysis = make_analysis(profiles=[make_profile(f"C{index}") for index in range(5)])
    context = build_proposal_context(analysis, triage(analysis), max_profiles=2)
    assert len(context["column_profiles"]) == 2
    assert context["column_profiles_omitted"] == 3


def test_nothing_is_truncated_silently_within_an_operation() -> None:
    """A clipped SQL body carries an explicit marker, so the model never assumes it saw it all."""
    analysis = make_analysis(
        connections=[Connection(name="legacy", kind="odbc", command="SELECT 1, " + "x" * 4_000)]
    )
    sql = build_proposal_context(analysis, triage(analysis))["connections"][0]["sql"]
    assert "more characters omitted" in sql


def test_findings_are_ordered_errors_first() -> None:
    analysis = make_analysis(
        findings=[
            make_finding(FindingKind.PREAMBLE_ROWS, Severity.INFO),
            make_finding(FindingKind.MERGED_CELLS, Severity.WARNING),
            make_finding(FindingKind.CIRCULAR_REFERENCE, Severity.ERROR),
        ]
    )
    severities = [
        finding["severity"]
        for finding in build_proposal_context(analysis, triage(analysis))["findings"]
    ]
    assert severities == ["error", "warning", "info"]


def test_the_deterministic_triage_travels_with_the_context_as_evidence() -> None:
    analysis = make_analysis(workbook_fields={"has_vba": True})
    result = triage(analysis)
    context = build_proposal_context(analysis, result)
    assert context["triage"]["verdict"] == result.verdict.value
    assert context["triage"]["convertible"] == result.convertible
    assert any("Macro logic" in line for line in context["triage"]["conversion_blockers"])


def test_named_ranges_notes_and_the_graph_all_reach_the_model() -> None:
    analysis = make_analysis(
        named_ranges=[NamedRange(name="capital_ratio", refers_to="Params!$B$1")],
        notes=[ProcessNote(source="sheet", origin="Process Notes", text="Run after month end.")],
        graph=DependencyGraph(nodes=["a", "b"], edges=[("a", "b")], roots=["a"], leaves=["b"]),
    )
    context = build_proposal_context(analysis, triage(analysis))
    assert context["named_ranges"][0]["name"] == "capital_ratio"
    assert context["process_notes"][0]["text"] == "Run after month end."
    assert context["dependency_graph"]["edges"] == [("a", "b")]


def test_the_context_is_json_serialisable_because_that_is_what_the_prompt_carries(
    analysis,
) -> None:
    context = build_proposal_context(analysis, triage(analysis))
    assert json.loads(json.dumps(context, default=str))["summary"]["workbook"] == (
        analysis.workbook.filename
    )


# =============================================================================
# PROMPTS AND MESSAGES
# =============================================================================


def test_a_proposal_is_two_system_messages_and_one_user_message(analysis) -> None:
    """Role and hard rules first, then the vocabulary, then the workbook (PLAN 2.7)."""
    messages = build_messages(analysis, triage(analysis))
    assert [message["role"] for message in messages] == ["system", "system", "user"]
    assert "You are the planning step of kedge" in messages[0]["content"]
    assert "translation vocabulary" in messages[1]["content"]


def test_every_placeholder_in_the_user_prompt_is_filled(analysis) -> None:
    """A `{{workbook}}` that survives into the prompt is a silently degraded proposal."""
    user = build_messages(analysis, triage(analysis))[2]["content"]
    assert "{{" not in user
    assert analysis.workbook.filename in user
    assert analysis.workbook.sha256 in user
    assert "Convertible." in user


def test_a_seed_plan_is_offered_as_a_worked_example_rather_than_starting_the_model_cold(
    analysis, plan
) -> None:
    """PLAN 2.2: last quarter's approved plan re-seeds this quarter's proposal."""
    user = build_messages(analysis, triage(analysis), seed_plan=plan)[2]["content"]
    assert "A previous plan for this process" in user
    assert "apply_haircuts" in user
    assert "keep the same stage ids so the plans diff cleanly" in user


def test_without_a_seed_the_prompt_carries_no_empty_seed_block(analysis) -> None:
    user = build_messages(analysis, triage(analysis))[2]["content"]
    assert "A previous plan for this process" not in user


def test_a_prebuilt_context_is_used_rather_than_rebuilt(analysis) -> None:
    """The corpus sweep builds one digest and reuses it; rebuilding would be silent waste."""
    user = build_messages(analysis, triage(analysis), context={"marker": "prebuilt-digest"})[2]
    assert "prebuilt-digest" in user["content"]
    assert "column_profiles" not in user["content"]


def test_a_missing_prompt_file_names_the_file_it_could_not_read() -> None:
    load_prompt.cache_clear()
    with pytest.raises(ProposalError, match=r"propose_nonexistent\.md"):
        load_prompt("propose_nonexistent.md")


def test_prompts_are_cached_because_the_retry_loop_reads_them_repeatedly() -> None:
    load_prompt.cache_clear()
    load_prompt("propose_system.md")
    load_prompt("propose_system.md")
    assert load_prompt.cache_info().hits >= 1


# =============================================================================
# THE STRICT JSON SCHEMA
# =============================================================================


def _walk(node: Any) -> list[dict[str, Any]]:
    """Every object node in a JSON schema, including those under `$defs` and `anyOf`."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if node.get("type") == "object" or "properties" in node:
            found.append(node)
        for value in node.values():
            found.extend(_walk(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk(item))
    return found


def test_every_object_in_the_schema_forbids_additional_properties() -> None:
    """Strict structured output requires it, and it is what turns a hallucinated field into a
    named repair instruction rather than a silent drop."""
    for node in _walk(plan_json_schema()):
        assert node["additionalProperties"] is False


def test_every_property_is_required_because_strict_mode_demands_it() -> None:
    for node in _walk(plan_json_schema()):
        assert sorted(node["required"]) == sorted(node.get("properties", {}))


def test_keywords_endpoints_reject_are_stripped() -> None:
    """A schema refused for carrying `default` costs a whole fallback round trip."""
    rendered = json.dumps(plan_json_schema())
    assert '"default"' not in rendered
    assert '"format"' not in rendered


def test_the_schema_describes_a_draft_not_a_whole_plan() -> None:
    """Provenance is kedge's to fill in; asking the model for it invites hallucination."""
    schema = plan_json_schema()
    assert sorted(schema["required"]) == [
        "assessment",
        "briefing",
        "dropped",
        "open_questions",
        "stages",
        "summary",
    ]
    assert "workbook_sha256" not in schema["properties"]
    assert "approval" not in schema["properties"]


def test_the_schema_asks_for_a_structured_source_rather_than_a_string() -> None:
    """A free-form string is an answer nothing can validate and nothing can reason about."""
    source = plan_json_schema()["$defs"]["StageSource"]
    assert sorted(source["properties"]) == ["origin", "ref"]
    assert source["additionalProperties"] is False

    origins = plan_json_schema()["$defs"]["SourceOrigin"]["enum"]
    assert sorted(origins) == sorted(origin.value for origin in SourceOrigin)


def test_the_system_prompt_explains_every_origin_the_schema_will_accept() -> None:
    """A schema the prompt does not explain produces a model that guesses at it."""
    prompt = load_prompt("propose_system.md")
    for origin in SourceOrigin:
        assert f"`{origin.value}`" in prompt, f"{origin.value} is in the schema, not in the prompt"


# =============================================================================
# PARSING WHAT MODELS ACTUALLY RETURN
# =============================================================================


def test_a_bare_json_object_parses() -> None:
    assert len(parse_draft(make_draft().model_dump_json()).stages) == 4


def test_a_markdown_fence_is_tolerated_because_models_add_one_regardless() -> None:
    body = make_draft().model_dump_json()
    assert parse_draft(f"```json\n{body}\n```").stages


def test_a_sentence_before_the_json_is_tolerated() -> None:
    body = make_draft().model_dump_json()
    assert parse_draft(f"Here is the plan you asked for:\n{body}").stages


def test_prose_wrapped_round_a_fence_is_tolerated() -> None:
    body = make_draft().model_dump_json()
    assert parse_draft(f"Sure!\n```json\n{body}\n```\nHope that helps.").stages


@pytest.mark.parametrize("text", ["", "   ", "I am unable to plan this workbook."])
def test_a_response_with_no_json_object_at_all_is_a_proposal_error(text: str) -> None:
    with pytest.raises(ProposalError, match="no JSON object"):
        parse_draft(text)


def test_truncated_json_becomes_a_validation_error_the_retry_loop_can_feed_back() -> None:
    """A cut-off response is the commonest malformed output there is; it must repair, not crash."""
    body = make_draft().model_dump_json()
    with pytest.raises(ValidationError, match="Invalid JSON"):
        parse_draft(body[: len(body) // 2])


def test_a_plan_that_fails_schema_validation_raises_with_the_offending_field_named() -> None:
    raw = json.loads(make_draft().model_dump_json())
    raw["stages"][0]["confidence_score"] = 0.9
    with pytest.raises(ValidationError, match="confidence_score"):
        parse_draft(json.dumps(raw))


def test_a_plan_whose_stage_graph_is_broken_is_rejected_here_not_in_the_scaffolder() -> None:
    raw = json.loads(make_draft().model_dump_json())
    raw["stages"][1]["depends_on"] = ["a_stage_that_does_not_exist"]
    with pytest.raises(ValidationError, match="unknown stage"):
        parse_draft(json.dumps(raw))


def test_a_stage_reading_a_stage_that_does_not_exist_is_a_repair_instruction() -> None:
    """`sources` is validated now, so the retry loop can correct a mistyped upstream id."""
    raw = json.loads(make_draft().model_dump_json())
    raw["stages"][1]["sources"] = [{"origin": "stage", "ref": "laod_handin"}]
    with pytest.raises(ValidationError, match="reads unknown stage"):
        parse_draft(json.dumps(raw))


def test_structured_sources_arrive_with_their_origins_intact() -> None:
    raw = json.loads(make_draft().model_dump_json())
    raw["stages"][0]["sources"] = [
        {"origin": "query", "ref": "MonthlyExposures"},
        {"origin": "manual", "ref": "Adjustments!B2:B15"},
    ]

    draft = parse_draft(json.dumps(raw))

    assert draft.stages[0].sources == [
        StageSource(origin=SourceOrigin.QUERY, ref="MonthlyExposures"),
        StageSource(origin=SourceOrigin.MANUAL, ref="Adjustments!B2:B15"),
    ]


def test_a_model_that_sends_the_old_bare_strings_is_read_rather_than_repaired() -> None:
    """It will: the seed plan it is shown may be a 1.0 plan, and models copy their examples."""
    raw = json.loads(make_draft().model_dump_json())
    raw["stages"][1]["sources"] = ["Calc!H2:H500", "load_handin", "handin"]

    draft = parse_draft(json.dumps(raw))

    assert [source.render() for source in draft.stages[1].sources] == [
        "range Calc!H2:H500",
        "stage load_handin",
        "handin",
    ]


# =============================================================================
# FIELDS THAT ARE A HUMAN'S DECISION, NOT THE MODEL'S
# =============================================================================
#
# `assessment` was the first of these and `parse_draft(..., assessment=)` is the seam that
# overrides it. The same reasoning applies to every field that records something a *reviewer*
# decided: a drop's acknowledgement, and an open question's answer. `unacknowledged_drops` is the
# only structural blocker on a plan — the thing standing between "kedge silently deleted six
# columns" and a bug report — so a model that can set it can sign off its own deletions.


def _draft_with_signed_off_drop(**extra: Any) -> str:
    raw = json.loads(make_draft().model_dump_json())
    raw["dropped"] = [
        {
            "range": "Calc!AK:AP",
            "reason": "unused",
            "acknowledged": True,
            "accepted": True,
            "note": "signed off by the analyst",
            "acknowledged_at": "2026-08-12T09:00:00Z",
        }
    ]
    raw.update(extra)
    return json.dumps(raw)


@pytest.mark.parametrize("assessment", [None, Assessment(convertible=0.7)])
def test_a_model_cannot_acknowledge_its_own_dropped_ranges(assessment) -> None:
    """Parametrised over both paths: the CLI proposal and the chat tool go through one seam."""
    draft = parse_draft(_draft_with_signed_off_drop(), assessment=assessment)

    drop = draft.dropped[0]
    assert drop.range == "Calc!AK:AP", "the proposal itself survives"
    assert drop.reason == "unused"
    assert drop.acknowledged is False, "a signature no human made is not a signature"
    assert drop.note is None
    assert drop.acknowledged_at is None


def test_a_drop_the_model_signed_off_still_blocks_approval() -> None:
    """The end of the chain: the forged acknowledgement must not clear `approval_blockers`."""
    draft = parse_draft(_draft_with_signed_off_drop())
    plan = ProcessPlan.from_draft(draft, workbook="w.xlsx", workbook_sha256="0" * 64)

    assert plan.unacknowledged_drops
    assert any("Calc!AK:AP" in blocker for blocker in plan.approval_blockers())


@pytest.mark.parametrize("assessment", [None, Assessment(convertible=0.7)])
def test_a_model_cannot_answer_its_own_open_questions(assessment) -> None:
    """An answered question is a question a human closed; it warns until they do."""
    raw = json.loads(make_draft().model_dump_json())
    raw["open_questions"] = [
        {
            "question": "Column AF is computed but never referenced. Dead, or read manually?",
            "context": "Calc!AF2:AF500",
            "answer": "Dead since the 2023 migration.",
            "answered_at": "2026-08-12T09:00:00Z",
        }
    ]

    draft = parse_draft(json.dumps(raw), assessment=assessment)

    question = draft.open_questions[0]
    assert question.context == "Calc!AF2:AF500", "where it came from is the model's to state"
    assert question.answered is False
    assert question.answer is None
    assert question.answered_at is None


def test_a_source_origin_is_a_claim_about_the_workbook_and_survives_the_strip() -> None:
    """The other side of the same seam, and the reason `sources` is not stripped with the rest.

    Every member of the vocabulary states something about where an input comes from — a hand-in,
    a query, a person typing — which is a reading of the workbook a reviewer can see in
    `render_plan` and correct with `edit_stage`, exactly like `intent` or `excel_pattern`. None of
    them records a decision a *reviewer* took, which is what `acknowledged` and `answer` do and
    why those two are rebuilt from their defaults. `handin` is the one worth stating out loud: it
    proposes that an input should arrive as a managed hand-in, and proposing is all it does —
    nothing downstream treats the word as evidence that anyone has supplied one, and the
    scaffolder emits the same hand-in head either way.
    """
    raw = json.loads(make_draft().model_dump_json())
    raw["stages"][0]["sources"] = [{"origin": "handin", "ref": None}]

    draft = parse_draft(json.dumps(raw))

    assert draft.stages[0].sources == [StageSource(origin=SourceOrigin.HANDIN)]


# =============================================================================
# THE SCRIPTED SEAM
# =============================================================================


def test_the_scripted_completer_records_what_it_was_asked() -> None:
    completer = ScriptedCompleter(["{}"])
    request = CompletionRequest(messages=[{"role": "user", "content": "hi"}], model="m")
    assert completer.complete(request) == "{}"
    assert completer.requests == [request]


def test_an_exhausted_script_says_so_rather_than_looping_forever() -> None:
    completer = ScriptedCompleter(["{}"])
    completer.complete(CompletionRequest(messages=[], model="m"))
    with pytest.raises(ProposalError, match="exhausted after 1 response"):
        completer.complete(CompletionRequest(messages=[], model="m"))


def test_responses_recorded_to_disk_replay_offline(tmp_path: Path) -> None:
    """Capture once against a real endpoint, replay in CI forever."""
    written = record_responses(["first", "second"], tmp_path)
    assert [path.name for path in written] == ["response-01.json", "response-02.json"]

    replayed = ScriptedCompleter.from_paths(*written)
    assert replayed.complete(CompletionRequest(messages=[], model="m")) == "first"
    assert replayed.complete(CompletionRequest(messages=[], model="m")) == "second"


def test_recording_creates_the_directory_it_was_pointed_at(tmp_path: Path) -> None:
    written = record_responses(["only"], tmp_path / "captured" / "run-1", prefix="attempt")
    assert written[0].name == "attempt-01.json"
    assert written[0].is_file()


# =============================================================================
# THE OPENAI-COMPATIBLE ENDPOINT
# =============================================================================


def test_structured_output_starts_at_the_strictest_mode() -> None:
    completer, client = _completer_over('{"ok": true}')
    completer.complete(CompletionRequest(messages=[], model="m", json_schema={"type": "object"}))
    assert client.calls[0]["response_format"]["type"] == "json_schema"
    assert client.calls[0]["response_format"]["json_schema"]["strict"] is True


def test_a_rejected_schema_degrades_to_json_object_and_the_mode_is_remembered() -> None:
    """The endpoint is whatever the user configured; the fallback is paid for once per session."""
    completer, client = _completer_over(_bad_request(), '{"ok": true}', '{"ok": true}')
    schema = {"type": "object"}

    completer.complete(CompletionRequest(messages=[], model="m", json_schema=schema))
    assert completer.mode == "json_object"
    assert client.calls[1]["response_format"] == {"type": "json_object"}

    completer.complete(CompletionRequest(messages=[], model="m", json_schema=schema))
    assert client.calls[2]["response_format"] == {"type": "json_object"}


def test_an_endpoint_that_rejects_every_mode_degrades_all_the_way_to_plain_text() -> None:
    completer, client = _completer_over(_bad_request(), _bad_request(), '{"ok": true}')
    completer.complete(CompletionRequest(messages=[], model="m", json_schema={"type": "object"}))
    assert completer.mode == "text"
    assert client.calls[2]["response_format"] is None


def test_a_rejection_with_no_fallback_left_is_reported_not_retried() -> None:
    completer, _ = _completer_over(_bad_request(), _bad_request(), _bad_request())
    with pytest.raises(ProposalError, match="no fallback left"):
        completer.complete(CompletionRequest(messages=[], model="m", json_schema={}))


def test_an_endpoint_that_refuses_an_explicit_temperature_sends_its_default_instead() -> None:
    """Found by pointing `tests/llm` at a real endpoint: the reasoning models accept only their
    default temperature and 400 on any other, which is a different failure from "this endpoint
    does not do structured output" and must not be treated as one."""
    completer, client = _completer_over(_temperature_rejected(), '{"ok": true}')

    completer.complete(CompletionRequest(messages=[], model="m", json_schema={"type": "object"}))

    assert completer.omit_temperature
    assert "temperature" in client.calls[0]
    assert "temperature" not in client.calls[1]
    assert completer.mode == "json_schema", (
        "a temperature rejection degraded structured output, which it says nothing about"
    )


def test_the_temperature_refusal_is_remembered_for_the_rest_of_the_session() -> None:
    """One rejection per session, not one per request: the sweep in `tests/llm` makes eight."""
    completer, client = _completer_over(_temperature_rejected(), '{"ok": true}', '{"ok": true}')
    completer.complete(CompletionRequest(messages=[], model="m"))
    completer.complete(CompletionRequest(messages=[], model="m"))
    assert [("temperature" in call) for call in client.calls] == [True, False, False]


def test_a_temperature_refusal_carrying_no_error_body_is_still_recognised() -> None:
    """A proxy or a local server answers with a bare message and no structured error object."""
    completer, client = _completer_over(_temperature_rejected(param=False), '{"ok": true}')
    completer.complete(CompletionRequest(messages=[], model="m"))
    assert completer.omit_temperature
    assert "temperature" not in client.calls[1]


def test_a_structured_rejection_about_another_parameter_degrades_normally() -> None:
    """`error.param` is the unambiguous signal, so a 400 naming a different parameter must fall
    through to the structured-output ladder instead of quietly dropping the temperature."""
    response = httpx.Response(400, request=httpx.Request("POST", "https://example.invalid/v1"))
    rejection = BadRequestError(
        "Unsupported value: 'response_format'",
        response=response,
        body={"error": {"message": "unsupported", "param": "response_format"}},
    )
    completer, client = _completer_over(rejection, '{"ok": true}')

    completer.complete(CompletionRequest(messages=[], model="m", json_schema={"type": "object"}))

    assert not completer.omit_temperature
    assert completer.mode == "json_object"
    assert "temperature" in client.calls[1]


def test_an_endpoint_that_refuses_the_request_even_without_a_temperature_gives_up() -> None:
    """The fallback fires once. A second identical rejection is a different problem and must not
    loop, so it falls through to the structured-output ladder and out."""
    completer, client = _completer_over(*[_temperature_rejected() for _ in range(5)])
    with pytest.raises(ProposalError, match="no temperature"):
        completer.complete(CompletionRequest(messages=[], model="m", json_schema={}))
    assert len(client.calls) == 4


def test_a_request_with_no_schema_still_asks_for_a_json_object() -> None:
    completer, client = _completer_over('{"ok": true}')
    completer.complete(CompletionRequest(messages=[], model="m"))
    assert client.calls[0]["response_format"] == {"type": "json_object"}


def test_an_unreachable_endpoint_says_so_in_words_a_user_can_act_on() -> None:
    completer, _ = _completer_over(OpenAIError("connection refused"))
    with pytest.raises(ProposalError, match="could not be reached"):
        completer.complete(CompletionRequest(messages=[], model="m"))


def test_an_empty_response_is_an_error_rather_than_an_empty_plan() -> None:
    completer, _ = _completer_over(None)
    with pytest.raises(ProposalError, match="empty response"):
        completer.complete(CompletionRequest(messages=[], model="m"))


def test_a_completer_is_built_from_config_and_the_key_comes_from_the_keyring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CONVENTIONS: never from a file, never from an environment variable.

    The key itself is asserted, not merely faked. The OpenAI SDK falls back to `OPENAI_API_KEY`
    when it is handed none, so the environment is loaded here with a value that must lose: only
    reading the key back off the client distinguishes "the keyring was consulted" from "a key
    arrived from somewhere". Without that assertion this test passes against an implementation
    that hardcodes the key and never calls `get_api_key` at all.
    """
    import kedge.config

    monkeypatch.setenv("OPENAI_API_KEY", "from-the-environment")
    monkeypatch.setattr(kedge.config, "get_api_key", lambda config: "from-the-keyring")
    config = Config(model=ModelConfig(base_url="https://example.invalid/v1", model="local-7b"))

    completer = completer_from_config(config)

    assert completer._client.api_key == "from-the-keyring"
    assert completer.mode == "json_schema"
    assert completer._client.base_url.host == "example.invalid"
    assert completer._model == "local-7b"


def test_the_key_is_never_read_from_the_environment_when_the_keyring_has_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`get_api_key` raises rather than returning; the SDK's environment fallback must not rescue
    it, because a key kedge never stored is a key kedge cannot rotate or revoke."""
    import kedge.config
    from kedge.config import MissingApiKeyError

    def refuse(config: Config) -> str:
        raise MissingApiKeyError("no keyring entry 'kedge/default'")

    monkeypatch.setenv("OPENAI_API_KEY", "from-the-environment")
    monkeypatch.setattr(kedge.config, "get_api_key", refuse)

    with pytest.raises(MissingApiKeyError):
        completer_from_config(Config(model=ModelConfig(base_url="https://example.invalid/v1")))


# =============================================================================
# PROPOSE: THE CALL, THE REFUSAL, THE REPAIR
# =============================================================================


def test_a_valid_response_becomes_a_plan_stamped_with_the_analysis_provenance(analysis) -> None:
    draft = make_draft()
    plan = propose_plan(analysis, completer=scripted_from_plan(draft), model="local-7b", version=4)
    assert plan.stage_ids == [stage.id for stage in draft.stages]
    assert plan.workbook == analysis.workbook.filename
    assert plan.workbook_sha256 == analysis.workbook.sha256
    assert plan.llm_model == "local-7b"
    assert plan.version == 4


def test_a_proposed_plan_arrives_unapproved(analysis) -> None:
    """Nothing reaches the notebook before a human approves it (PLAN 2.2)."""
    plan = propose_plan(analysis, completer=scripted_from_plan(make_draft()))
    assert not plan.approval.approved


def test_a_seed_plan_records_the_version_it_was_derived_from(analysis, plan) -> None:
    proposed = propose_plan(
        analysis, completer=scripted_from_plan(make_draft()), seed_plan=plan, version=2
    )
    assert proposed.based_on_version == plan.version


def test_triage_runs_here_when_the_caller_did_not_run_it(analysis) -> None:
    completer = scripted_from_plan(make_draft())
    propose_plan(analysis, completer=completer)
    assert "Convertible." in completer.requests[0].messages[2]["content"]


def test_a_stop_verdict_refuses_to_spend_a_model_call() -> None:
    """A refusal with reasons is a different outcome from a poor plan, and must not be confused
    with one at the call site."""
    analysis = make_analysis(workbook_fields={"file_format": "xlsb"})
    completer = ScriptedCompleter([make_draft().model_dump_json()])
    with pytest.raises(ProposalRefusedError) as caught:
        propose_plan(analysis, completer=completer)
    assert caught.value.result.should_stop
    assert "re-save as .xlsx" in str(caught.value)
    assert not completer.requests


def test_force_overrides_the_refusal_because_it_should_be_a_deliberate_act() -> None:
    analysis = make_analysis(workbook_fields={"file_format": "xlsb"})
    plan = propose_plan(analysis, completer=scripted_from_plan(make_draft()), force=True)
    assert plan.stages


def test_convertibility_is_kedges_triage_and_not_the_models_own_score(analysis) -> None:
    """The CLI path scores this the way the chat `propose_plan` tool does, and for the reason
    :meth:`TriageResult.as_assessment` gives: a model scoring its own decomposition has nothing to
    score it against, and `render_plan` prints the figure as though it had been computed.
    """
    raw = json.loads(make_draft().model_dump_json())
    raw["assessment"] = {
        "convertible": 0.05,
        "blockers": ["I am not sure about any of this"],
        "rationale": "a feeling about the workbook",
    }

    plan = propose_plan(analysis, completer=ScriptedCompleter([json.dumps(raw)]))

    scored = triage(analysis)
    assert plan.assessment.convertible == scored.convertible
    assert plan.assessment.convertible != 0.05
    assert plan.assessment.blockers == scored.blocker_lines()
    assert "triage" in (plan.assessment.rationale or "")


def test_a_precomputed_triage_is_honoured_rather_than_recomputed(analysis) -> None:
    stop = TriageResult(verdict=TriageVerdict.STOP, convertible=0.0, complexity=0.9)
    with pytest.raises(ProposalRefusedError):
        propose_plan(analysis, completer=ScriptedCompleter([]), triage_result=stop)


def test_a_malformed_response_is_repaired_rather_than_abandoned(analysis) -> None:
    """Validation failures are repair instructions: the pydantic messages go back to the model."""
    valid = make_draft().model_dump_json()
    completer = ScriptedCompleter(["I would rather not.", valid])

    plan = propose_plan(analysis, completer=completer)

    assert plan.stages
    assert len(completer.requests) == 2
    repair = completer.requests[1].messages
    assert repair[-2]["role"] == "assistant"
    assert repair[-2]["content"] == "I would rather not."
    assert "could not be loaded as a process plan" in repair[-1]["content"]
    assert "no JSON object" in repair[-1]["content"]


def test_the_repair_instruction_names_the_fields_that_failed(analysis) -> None:
    # The offending field is one the model still owns. `assessment.convertible` used to serve
    # here and no longer can: triage's figure replaces it before pydantic sees the response, so a
    # bad number in the JSON never reaches validation at all — which is the point of it.
    raw = json.loads(make_draft().model_dump_json())
    raw["stages"][0]["confidence"] = "fairly sure"
    completer = ScriptedCompleter([json.dumps(raw), make_draft().model_dump_json()])

    propose_plan(analysis, completer=completer)

    repair = completer.requests[1].messages[-1]["content"]
    assert "stages.0.confidence" in repair
    assert "'high', 'medium'" in repair


def test_each_repair_appends_to_the_prompt_without_rewriting_or_mutating_it(
    analysis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repair is an append of exactly two messages — the assistant's bad reply, then the
    instruction — onto an opening prompt that is never rebuilt.

    The list `build_messages` handed back is spied on and asserted still to hold three entries,
    because request lengths alone cannot tell `messages = [*messages, a, b]` from
    `messages.append(a); messages.append(b)`: every request is snapshotted with `list(messages)`
    on the way out, so both produce 3, 5, 7. Only the original list knows which happened.
    """
    import kedge.plan.propose as propose_module

    captured: list[list[dict[str, str]]] = []
    build = propose_module.build_messages

    def spy(*args: Any, **kwargs: Any) -> list[dict[str, str]]:
        captured.append(build(*args, **kwargs))
        return captured[-1]

    monkeypatch.setattr(propose_module, "build_messages", spy)
    completer = ScriptedCompleter(["nope", "still nope", make_draft().model_dump_json()])

    propose_plan(analysis, completer=completer)

    assert len(captured) == 1, "the opening prompt was rebuilt for a repair rather than extended"
    assert len(captured[0]) == 3, "the retry loop mutated the message list build_messages returned"
    assert [len(request.messages) for request in completer.requests] == [3, 5, 7]
    for request in completer.requests:
        assert request.messages[:3] == captured[0], "a repair rewrote the opening prompt"
    assert [message["role"] for message in completer.requests[-1].messages] == [
        "system",
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]


def test_running_out_of_attempts_reports_every_failure(analysis) -> None:
    completer = ScriptedCompleter(["one", "two", "three"])
    with pytest.raises(ProposalError) as caught:
        propose_plan(analysis, completer=completer, max_attempts=3)
    message = str(caught.value)
    assert "did not produce a valid process plan in 3 attempt(s)" in message
    assert analysis.workbook.filename in message
    assert message.count("attempt ") == 3


def test_a_single_attempt_does_not_ask_for_a_repair_it_will_never_send(analysis) -> None:
    completer = ScriptedCompleter(["nope"])
    with pytest.raises(ProposalError):
        propose_plan(analysis, completer=completer, max_attempts=1)
    assert len(completer.requests) == 1


def test_max_attempts_below_one_still_makes_one_attempt(analysis) -> None:
    completer = ScriptedCompleter([make_draft().model_dump_json()])
    assert propose_plan(analysis, completer=completer, max_attempts=0).stages
    assert len(completer.requests) == 1


def test_the_schema_and_temperature_reach_the_request(analysis) -> None:
    completer = ScriptedCompleter([make_draft().model_dump_json()])
    propose_plan(analysis, completer=completer, temperature=0.7)
    request = completer.requests[0]
    assert request.temperature == 0.7
    assert request.json_schema == plan_json_schema()
    assert request.schema_name == "process_plan"


def test_scripted_from_plan_accepts_a_plan_or_a_draft(plan) -> None:
    """The offline fixture in one line, from either end of the round trip."""
    assert (
        parse_draft(scripted_from_plan(plan).complete(CompletionRequest(messages=[], model="m")))
        == plan.to_draft()
    )
    draft = make_draft()
    assert (
        parse_draft(scripted_from_plan(draft).complete(CompletionRequest(messages=[], model="m")))
        == draft
    )


# =============================================================================
# STEPS 1-2 BEHIND ONE CALL: `run_plan`
# =============================================================================


@pytest.fixture
def workbook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A workbook path in an isolated project directory, with `~/.kedge` redirected."""
    monkeypatch.setenv("KEDGE_HOME", str(tmp_path / "home"))
    path = tmp_path / "rwa_monthly_v14.xlsx"
    path.write_bytes(b"not a real workbook; run_plan is given the analysis directly")
    return path


def test_run_plan_triages_then_proposes_and_saves_a_version(workbook: Path, analysis) -> None:
    run = run_plan(workbook, analysis=analysis, completer=scripted_from_plan(make_draft()))
    assert isinstance(run, PlanRun)
    assert not run.stopped
    assert run.plan is not None
    assert run.saved_to is not None
    assert run.saved_to.name == "plan-v001.yaml"
    assert run.saved_to.is_file()


def test_a_dry_run_writes_nothing_which_is_what_makes_judging_the_corpus_cheap(
    workbook: Path, analysis
) -> None:
    """PLAN 7.4: run it over dissimilar workbooks and read the plans."""
    run = run_plan(
        workbook, analysis=analysis, completer=scripted_from_plan(make_draft()), dry_run=True
    )
    assert run.saved_to is None
    assert not (workbook.parent / "rwa_monthly_v14.kedge" / "plans").exists()


def test_a_refusal_is_a_result_rather_than_an_exception(workbook: Path) -> None:
    """An honest refusal with reasons is more useful than a notebook that looks complete."""
    refused = make_analysis(workbook_fields={"file_format": "xlsb"})
    run = run_plan(workbook, analysis=refused, completer=ScriptedCompleter([]))
    assert run.stopped
    assert run.plan is None
    assert run.saved_to is None
    assert "recommends not converting" in run.render()


def test_review_warnings_travel_with_the_run(workbook: Path) -> None:
    analysis = make_analysis(
        operations=[make_operation("calc_h2_h500"), make_operation("orphan_op")]
    )
    run = run_plan(
        workbook, analysis=analysis, completer=scripted_from_plan(make_draft()), dry_run=True
    )
    assert any("claimed by no stage" in warning for warning in run.warnings)


def test_the_second_run_reseeds_from_the_saved_plan(workbook: Path, analysis) -> None:
    """The whole point of retaining the history: next quarter starts from last quarter."""
    run_plan(workbook, analysis=analysis, completer=scripted_from_plan(make_draft()))
    completer = ScriptedCompleter([make_draft().model_dump_json()])

    second = run_plan(workbook, analysis=analysis, completer=completer)

    assert "A previous plan for this process" in completer.requests[0].messages[2]["content"]
    assert second.plan is not None
    assert second.plan.version == 2


def test_reseeding_can_be_turned_off(workbook: Path, analysis) -> None:
    run_plan(workbook, analysis=analysis, completer=scripted_from_plan(make_draft()))
    completer = ScriptedCompleter([make_draft().model_dump_json()])
    run_plan(workbook, analysis=analysis, completer=completer, reseed=False)
    assert "A previous plan for this process" not in completer.requests[0].messages[2]["content"]


def test_the_render_of_a_successful_run_carries_both_the_triage_and_the_plan(
    workbook: Path, analysis
) -> None:
    run = run_plan(
        workbook, analysis=analysis, completer=scripted_from_plan(make_draft()), dry_run=True
    )
    rendered = run.render()
    assert "convertible:" in rendered
    assert "STAGES (4)" in rendered


# ── load_analysis ───────────────────────────────────────────────────────────


def test_a_saved_analysis_is_preferred_over_re_analysing(tmp_path: Path, analysis) -> None:
    saved = tmp_path / "analysis.json"
    saved.write_text(analysis.model_dump_json(), encoding="utf-8")
    assert load_analysis(tmp_path / "wb.xlsx", analysis_path=saved) == analysis


def test_a_corrupt_analysis_file_names_itself(tmp_path: Path) -> None:
    saved = tmp_path / "analysis.json"
    saved.write_text("{not json", encoding="utf-8")
    with pytest.raises(PlanError, match="could not load the analysis"):
        load_analysis(tmp_path / "wb.xlsx", analysis_path=saved)


def test_falling_back_to_the_analyser_calls_the_function_not_the_module_of_the_same_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The submodule and the function share a name, and only the call site tells them apart.

    Regression: `kedge.analysis.__init__` used to re-export the contract types but not
    `analyse`, so `from kedge.analysis import analyse` bound the *module*, the import
    succeeded, and `analyse(path)` raised `'module' object is not callable` for every workbook
    that had not already been inspected. The package now re-exports the function, which is
    asserted here too -- both halves have to hold, because the ambiguity comes back the moment
    the re-export is dropped.
    """
    import kedge.analysis
    from kedge.analysis.analyse import analyse as analyse_function

    assert kedge.analysis.analyse is analyse_function
    assert callable(kedge.analysis.analyse)

    monkeypatch.setenv("KEDGE_HOME", str(tmp_path / "home"))
    workbook = tmp_path / "clean_pipeline.xlsx"
    workbook.write_bytes(b"not a workbook")

    seen: list[Path] = []

    def fake_analyse(path: Path) -> Any:
        seen.append(Path(path))
        return make_analysis()

    # Patched on the module object, not through the dotted string: the re-export above means
    # `kedge.analysis.analyse` reached by attribute is now the function, so monkeypatch's own
    # attribute traversal would stop one level short and raise.
    monkeypatch.setattr(sys.modules["kedge.analysis.analyse"], "analyse", fake_analyse)

    assert load_analysis(workbook).workbook.filename == "rwa_monthly_v14.xlsx"
    assert seen == [workbook]


# ── the plan the tests are written against is itself well formed ────────────


def test_the_shared_draft_fixture_is_a_valid_plan_draft() -> None:
    """If this ever fails, every offline test above is asserting against a fiction."""
    draft = make_draft()
    assert isinstance(draft, PlanDraft)
    assert ProcessPlan.from_draft(draft, workbook="w.xlsx", workbook_sha256="0" * 64).stages


def test_a_minimal_draft_is_enough_to_propose_from(analysis) -> None:
    """A one-stage plan with no questions and no drops is legal; the schema does not template."""
    minimal = PlanDraft(
        assessment=Assessment(convertible=1.0),
        stages=[Stage(id="only", intent="Do the one thing this workbook does")],
        open_questions=[],
    )
    plan = propose_plan(analysis, completer=scripted_from_plan(minimal))
    assert plan.stage_ids == ["only"]
    assert plan.open_questions == []


def test_an_analysis_with_nothing_in_it_still_builds_a_context() -> None:
    """Every extractor degrades gracefully, so the plan layer must accept the degraded result."""
    empty = make_analysis(
        sheets=[],
        operations=[],
        profiles=[],
        graph=DependencyGraph(),
        cached_values=CachedValueCoverage(status="absent"),
        findings=[
            Finding(
                kind=FindingKind.MISSING_CACHED_VALUES,
                severity=Severity.ERROR,
                message="no cached values",
            )
        ],
    )
    context = build_proposal_context(empty, triage(empty))
    assert context["operations"] == []
    assert context["column_profiles"] == []


def test_an_operation_with_inconsistencies_carries_them_into_the_context() -> None:
    """The row-47 bug is exactly the kind of thing the plan must be told about."""
    analysis = make_analysis(
        operations=[
            make_operation(
                excel_pattern=ExcelPattern.ARITHMETIC,
                inconsistencies=[
                    Inconsistency(
                        cell="Messy!D47", expected_r1c1="=RC[-2]*RC[-1]", actual_r1c1="=RC[-2]*1.1"
                    )
                ],
            )
        ]
    )
    entry = build_proposal_context(analysis, triage(analysis))["operations"][0]
    assert entry["inconsistencies"][0]["cell"] == "Messy!D47"


def test_a_non_numeric_column_carries_no_numeric_block_and_keeps_its_anomalies() -> None:
    """A text column with five date formats in it is the reason `format_anomalies` exists."""
    analysis = make_analysis(
        profiles=[
            make_profile(
                "G",
                dtype="str",
                numeric=None,
                top_k=[],
                format_anomalies=["five date formats mixed in one column"],
            )
        ]
    )
    entry = build_proposal_context(analysis, triage(analysis))["column_profiles"][0]
    assert "numeric" not in entry
    assert entry["format_anomalies"] == ["five date formats mixed in one column"]


def test_a_cell_value_that_json_cannot_carry_is_rendered_as_clipped_text() -> None:
    analysis = make_analysis(
        profiles=[make_profile("D", head=[date(2026, 3, 1), None, "x" * 200], top_k=[])]
    )
    head = build_proposal_context(analysis, triage(analysis), include_sample_values=True)[
        "column_profiles"
    ][0]["head"]
    assert head[0] == "2026-03-01"
    assert head[1] is None
    assert head[2].endswith("more characters omitted]")


def test_an_absent_sql_body_stays_absent_rather_than_becoming_the_string_none() -> None:
    analysis = make_analysis(connections=[Connection(name="cube", kind="oledb", command=None)])
    assert build_proposal_context(analysis, triage(analysis))["connections"][0]["sql"] is None


def test_the_analyser_being_absent_names_the_command_that_would_produce_an_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEDGE_HOME", str(tmp_path / "home"))
    monkeypatch.setitem(sys.modules, "kedge.analysis.analyse", None)
    with pytest.raises(PlanError, match=r"kedge inspect rwa_monthly_v14\.xlsx"):
        load_analysis(tmp_path / "rwa_monthly_v14.xlsx")


def test_the_plan_history_is_iterable_straight_off_a_workspace(workbook: Path, analysis) -> None:
    space = Workspace.for_workbook(workbook)
    run_plan(workbook, analysis=analysis, completer=scripted_from_plan(make_draft()))
    run_plan(workbook, analysis=analysis, completer=scripted_from_plan(make_draft()))
    assert [plan.version for plan in iter_plan_history(space)] == [1, 2]


def test_the_payload_gives_the_model_the_cell_references_it_is_asked_to_cite(
    analysis_factory: Any,
) -> None:
    """`briefing.sources` demands citations, so the notes must arrive with their locations.

    They did not, for one release. The schema refused prose without a citation and the payload
    handed over the sheet and the heading but never the cells -- so the only way to satisfy the
    validator was to invent a range, which is precisely the failure the requirement exists to
    prevent. A rule the model cannot obey honestly is worse than no rule.
    """
    from kedge.analysis.model import ProcessNote

    analysis = analysis_factory(
        notes=[
            ProcessNote(
                source="sheet",
                origin="Sign-off",
                location="A3:A4",
                heading="Purpose",
                text="To record the quarterly uplift.",
            )
        ]
    )

    note = build_proposal_context(analysis, triage(analysis))["process_notes"][0]

    assert note["location"] == "A3:A4"
    assert note["origin"] == "Sign-off"
    assert note["heading"] == "Purpose"


def test_the_planner_is_asked_about_hand_offs_and_the_briefing() -> None:
    """The silent checklist is what actually shapes the answer, so it has to be current."""
    prompt = load_prompt("propose_user.md")

    assert "briefing" in prompt
    assert "handoff" in prompt
    assert "mutates" in prompt
    assert "`ref`" in prompt
    # The dead-region trap: a column of generated SQL has a person as its consumer. Asserted
    # on a fragment that does not straddle the prompt's line wrapping.
    assert "have a person as its consumer" in prompt
