"""Metering and failure attribution for a live model, exercised entirely offline.

Two halves, and the second is the one that matters.

The first is the ordinary case: what a completion cost is captured, accumulates across the
requests one logical call makes, and survives an endpoint that volunteers no ``usage`` block --
distinctly, as "nothing was reported", never as zero.

The second is a set of **negative controls** over :func:`harness.live.classify_failure`. A
classifier that has only ever seen a success is a classifier whose polarity nobody has checked,
and this one exists precisely because a live sweep once misattributed every failure it saw: an
endpoint refusing an explicit ``temperature`` on all twenty-four requests, read as "structured
output unsupported", producing no plans at all and pointing the diagnosis at the wrong thing
(``tests/llm/README.md``). So each failure this module can name is provoked through the *real*
code path in ``kedge.plan.propose`` -- a stubbed SDK client raising what a real endpoint raises,
or ``propose_plan`` given responses it cannot repair -- rather than by hand-writing a
``ProposalError`` with a convenient message. A test that constructs the exception it expects to
classify proves only that a string was matched.

The distinction that whole history turns on is asserted directly: a *recovered* temperature
refusal is a negotiation that succeeded, and must classify as a success carrying the
renegotiation, not as a failure.

No network, no keyring, no endpoint. The keyring is stubbed where it is reached at all, so this
file passes on a machine that has none of the three.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    InternalServerError,
    NotFoundError,
    RateLimitError,
    UnprocessableEntityError,
)

from kedge.config import MissingApiKeyError
from kedge.plan.propose import (
    CompletionRequest,
    CompletionUsage,
    OpenAICompleter,
    ProposalError,
    ProposalRefusedError,
    ScriptedCompleter,
    propose_plan,
    scripted_from_plan,
)

EVAL_ROOT = Path(__file__).resolve().parents[2] / "evals"
if str(EVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(EVAL_ROOT))

from harness.live import (  # noqa: E402
    Failure,
    MeteredCompleter,
    MeteredUsage,
    ModelSpec,
    ResolvedModel,
    classify_failure,
    resolve_model,
)
from harness.model import Outcome  # noqa: E402

from conftest import make_analysis, make_draft  # noqa: E402

_URL = "https://example.invalid/v1"


# ── helpers ─────────────────────────────────────────────────────────────────


def _usage(prompt: int = 0, completion: int = 0, cached: int | None = None) -> SimpleNamespace:
    """A chat-completions usage block, of the shape the SDK hands back."""
    details = None if cached is None else SimpleNamespace(cached_tokens=cached)
    return SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=completion, prompt_tokens_details=details
    )


def _answer(content: str, usage: Any = None) -> SimpleNamespace:
    """One completion envelope. ``usage=None`` is the endpoint that reports nothing.

    ``usage`` is deliberately untyped: half the point of these tests is the block that is not the
    shape the SDK promises -- a plain ``dict`` from a proxy, an empty namespace, a string where a
    number belongs.
    """
    envelope = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage
    )
    if usage is None:
        del envelope.usage
    return envelope


def _stub_endpoint(*outcomes: Any) -> SimpleNamespace:
    """Stand in for `OpenAI().chat`, replaying envelopes and raising exceptions in order."""
    calls: list[dict[str, Any]] = []
    remaining = list(outcomes)

    def create(**kwargs: Any) -> Any:
        calls.append(kwargs)
        outcome = remaining.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            return SimpleNamespace(choices=[])
        return outcome

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _completer(*outcomes: Any) -> OpenAICompleter:
    completer = OpenAICompleter(base_url=_URL, api_key="k", model="m")
    # Reaching past the constructor is the point: the SDK client is the seam being faked.
    completer._client = _stub_endpoint(*outcomes)
    return completer


def _request() -> CompletionRequest:
    return CompletionRequest(messages=[], model="m", json_schema={"type": "object"})


def _status(kind: type, message: str, code: int, body: Any = None) -> Any:
    """A status error of the SDK's own class, carrying the status code that class means.

    The code is required rather than defaulted, because it used to be 429 for every one of these.
    Every status test then passed against a classifier reading the response code instead of the
    exception type -- an authentication test built on a 429 proves nothing about either. The two
    must agree here for the tests to discriminate at all.
    """
    response = httpx.Response(code, request=httpx.Request("POST", _URL))
    return kind(message, response=response, body=body)


def _schema_refused() -> BadRequestError:
    """The rejection an endpoint that does not implement `response_format` sends back."""
    response = httpx.Response(400, request=httpx.Request("POST", _URL))
    return BadRequestError("response_format is not supported", response=response, body=None)


def _temperature_refused() -> BadRequestError:
    """What a reasoning model answers an explicit `temperature` with, verbatim.

    Transcribed from a real 400 off this project's own configured endpoint, and the exact
    rejection that once cost a whole sweep by being read as something else.
    """
    response = httpx.Response(400, request=httpx.Request("POST", _URL))
    message = (
        "Unsupported value: 'temperature' does not support 0.2 with this model. "
        "Only the default (1) value is supported."
    )
    body = {"error": {"message": message, "param": "temperature", "code": "unsupported_value"}}
    return BadRequestError(message, response=response, body=body)


def _raised_by(work: Any) -> BaseException:
    """Run ``work`` and hand back what it raised, for the classifier to attribute."""
    with pytest.raises(Exception) as caught:
        work()
    return caught.value


# =============================================================================
# WHAT A COMPLETION COST (the src/ hook)
# =============================================================================


def test_the_endpoints_own_numbers_are_captured_rather_than_discarded() -> None:
    """`Completer` returns a bare string, so a decorator cannot see tokens at all."""
    completer = _completer(_answer("{}", _usage(prompt=8_000, completion=120, cached=7_800)))

    completer.complete(_request())

    assert completer.usage == CompletionUsage(
        prompt=8_000, completion=120, cached=7_800, requests=1, answered=1, reported=1
    )
    assert completer.usage.total == 8_120
    assert completer.usage.any_reported


def test_requests_accumulate_across_the_retries_one_logical_completion_makes() -> None:
    """One `complete()` is not one request, and a table that says it is hides a real difference.

    An endpoint that refuses strict JSON schema and then JSON object costs three round trips to
    answer once. The model answered -- but it answered in plain text on the third attempt, which
    is a materially different result from answering first time.
    """
    completer = _completer(
        _schema_refused(), _schema_refused(), _answer("{}", _usage(prompt=100, completion=10))
    )

    completer.complete(_request())

    assert completer.usage.requests == 3
    assert completer.usage.reported == 1
    assert completer.usage.prompt == 100
    assert completer.mode == "text"


def test_an_endpoint_that_reports_no_usage_says_so_rather_than_reporting_zero() -> None:
    """llama.cpp, a thin proxy, an internal gateway: none of them owe you a usage block.

    Folding that into `0 tokens` would crown whichever model happened to be behind the least
    complete server as the cheapest in the sweep.
    """
    completer = _completer(_answer("{}"))

    completer.complete(_request())

    assert completer.usage.requests == 1
    assert completer.usage.reported == 0
    assert not completer.usage.any_reported
    assert completer.usage.total == 0


def test_a_partial_usage_block_is_taken_for_what_it_has() -> None:
    """A server that reports prompt tokens and no cache detail is still worth more than nothing."""
    completer = _completer(_answer("{}", _usage(prompt=42)))

    completer.complete(_request())

    assert completer.usage.prompt == 42
    assert completer.usage.cached == 0
    assert completer.usage.any_reported


def test_a_failed_call_still_reports_what_it_burned() -> None:
    """A leg of a sweep that died having spent three requests did not cost nothing."""
    completer = _completer(_schema_refused(), _schema_refused(), _schema_refused())

    with pytest.raises(ProposalError):
        completer.complete(_request())

    assert completer.usage.requests == 3
    assert not completer.usage.any_reported


def test_usage_describes_one_completion_rather_than_the_whole_session() -> None:
    """Reset per call: otherwise the second proposal in a run is billed for the first as well."""
    completer = _completer(
        _answer("{}", _usage(prompt=100)),
        _answer("{}", _usage(prompt=7)),
    )

    completer.complete(_request())
    completer.complete(_request())

    assert completer.usage.prompt == 7
    assert completer.usage.requests == 1


def test_a_usage_block_carrying_no_numbers_is_not_counted_as_a_report() -> None:
    """ "A usage attribute existed" and "usable numbers arrived" are different facts.

    An endpoint answering with an empty ``usage`` object has told nobody anything, and counting it
    as a report produced `reported=1, prompt=0, total=0, measured=True` -- which is to say, the
    cheapest model in the sweep. That is precisely the claim this module's docstring exists to
    stop anybody making.
    """
    completer = _completer(_answer("{}", SimpleNamespace()))

    completer.complete(_request())

    assert completer.usage.answered == 1, "the request was answered"
    assert completer.usage.reported == 0, "and the answer counted nothing"
    assert not completer.usage.any_reported


def test_a_null_token_count_is_absent_rather_than_zero() -> None:
    """``prompt_tokens: null`` is a proxy declining to say, not a proxy saying nought."""
    completer = _completer(
        _answer("{}", SimpleNamespace(prompt_tokens=None, completion_tokens=None))
    )

    completer.complete(_request())

    assert completer.usage.reported == 0
    assert not completer.usage.any_reported


def test_a_dict_shaped_usage_block_is_read_rather_than_missed() -> None:
    """Not every endpoint is the SDK.

    A proxy that assembles its own JSON hands back plain dictionaries, and ``getattr`` finds
    nothing at all on one of those -- so a complete usage block read as an endpoint that reports
    nothing. The same lie as counting an empty block, in the opposite direction.
    """
    block = {
        "prompt_tokens": 8_000,
        "completion_tokens": 120,
        "prompt_tokens_details": {"cached_tokens": 7_800},
    }
    completer = _completer(_answer("{}", block))

    completer.complete(_request())

    assert completer.usage.prompt == 8_000
    assert completer.usage.completion == 120
    assert completer.usage.cached == 7_800
    assert completer.usage.any_reported


def test_a_token_count_that_is_not_a_number_does_not_raise_mid_plan() -> None:
    """``getattr`` guards absence; ``int()`` does not guard badness.

    ``prompt_tokens="8,000"`` raises ``ValueError``, and bookkeeping that turns a completed plan
    into a traceback is a worse bug than the miscount it was preventing. The unreadable field is
    dropped; the readable one beside it is still counted.
    """
    block = SimpleNamespace(
        prompt_tokens="8,000", completion_tokens=120, prompt_tokens_details=None
    )
    completer = _completer(_answer("{}", block))

    assert completer.complete(_request()) == "{}"

    assert completer.usage.prompt == 0
    assert completer.usage.completion == 120
    assert completer.usage.any_reported


def test_a_usage_property_that_raises_is_treated_as_an_absent_field() -> None:
    """A property that raises propagates, and this one would propagate out of a working plan."""

    class _Hostile:
        completion_tokens = 5
        prompt_tokens_details = None

        @property
        def prompt_tokens(self) -> int:
            msg = "this gateway computes usage lazily, and badly"
            raise RuntimeError(msg)

    completer = _completer(_answer("{}", _Hostile()))

    assert completer.complete(_request()) == "{}"

    assert completer.usage.completion == 5
    assert completer.usage.prompt == 0


def test_an_untranslated_transport_error_still_counts_the_request_it_made() -> None:
    """The SDK translates transport errors around the request, and does not always manage it.

    A raw ``httpx.ReadTimeout`` reaching kedge is recorded on this project (CLAUDE.md). It used to
    escape the metering entirely -- ``requests == 0`` for a request that was made and billed --
    because only ``BadRequestError`` and ``OpenAIError`` were metered. It is still re-raised
    untouched: the caller decides what a failed proposal means.
    """
    completer = _completer(httpx.ReadTimeout("timed out reading the response"))

    with pytest.raises(httpx.ReadTimeout):
        completer.complete(_request())

    assert completer.usage.requests == 1
    assert completer.usage.answered == 0


# =============================================================================
# METERING ONE CALL AND ONE PROPOSAL
# =============================================================================


def test_a_metered_scripted_completer_reports_no_usage_rather_than_zero() -> None:
    """The offline case and the no-usage-block case are the same branch, on purpose."""
    metered = MeteredCompleter(ScriptedCompleter(["{}"]))

    assert metered.complete(_request()) == "{}"
    assert metered.usage.completions == 1
    assert not metered.usage.any_reported
    assert "no usage reported" in metered.usage.describe()
    assert metered.last is not None
    assert metered.last.mode is None


def test_metering_sums_the_repair_rounds_of_one_proposal(analysis) -> None:
    """A proposal is one completion plus one per repair, and a row wants the total."""
    valid = make_draft().model_dump_json()
    completer = _completer(
        _answer("I would rather not.", _usage(prompt=1_000, completion=20)),
        _answer(valid, _usage(prompt=1_400, completion=300, cached=900)),
    )
    metered = MeteredCompleter(completer)

    propose_plan(analysis, completer=metered)

    assert metered.usage.completions == 2
    assert metered.usage.requests == 2
    assert metered.usage.prompt == 2_400
    assert metered.usage.completion == 320
    assert metered.usage.cached == 900
    assert metered.usage.any_reported
    assert metered.usage.wasted_requests == 0


def test_wall_clock_seconds_come_from_the_injected_clock() -> None:
    ticks = iter([10.0, 12.5, 100.0, 100.25])
    metered = MeteredCompleter(ScriptedCompleter(["{}", "{}"]), clock=lambda: next(ticks))

    metered.complete(_request())
    metered.complete(_request())

    assert [call.seconds for call in metered.calls] == [2.5, 0.25]
    assert metered.seconds == pytest.approx(2.75)


def test_the_mode_recorded_is_the_one_that_actually_worked() -> None:
    metered = MeteredCompleter(_completer(_schema_refused(), _answer("{}")))

    metered.complete(_request())

    call = metered.last
    assert call is not None
    assert call.mode == "json_object"
    assert call.degraded
    assert call.negotiated
    assert call.ok


def test_a_recovered_temperature_refusal_is_a_success_that_records_the_negotiation() -> None:
    """The single most consequential distinction in this module.

    The endpoint said no to an explicit temperature, kedge dropped it, the model answered. That
    is a negotiation that succeeded. Scoring it as a failure is how a sweep once concluded that
    a perfectly capable model could not produce a plan; not recording it at all is how the same
    mistake goes unnoticed the second time.
    """
    metered = MeteredCompleter(_completer(_temperature_refused(), _answer("{}")))

    metered.complete(_request())

    call = metered.last
    assert call is not None
    assert call.ok
    assert call.failure is Failure.NONE
    assert call.failure.as_outcome() is Outcome.PASS
    assert call.omit_temperature
    assert call.negotiated
    assert not call.degraded
    assert call.usage.requests == 2
    assert call.usage.wasted_requests == 1


def test_a_raising_call_is_recorded_and_re_raised_untouched() -> None:
    metered = MeteredCompleter(_completer(_schema_refused(), _schema_refused(), _schema_refused()))

    with pytest.raises(ProposalError):
        metered.complete(_request())

    call = metered.last
    assert call is not None
    assert call.failure is Failure.SCHEMA_REFUSED
    assert call.usage.requests == 3
    assert call.detail


def test_summarise_takes_the_failure_the_caller_saw_not_the_last_completion(analysis) -> None:
    """Repairs exhausted is raised *after* the last successful completion, and is the real story."""
    metered = MeteredCompleter(ScriptedCompleter(["nope", "nope", "nope"]))
    exc = _raised_by(lambda: propose_plan(analysis, completer=metered))

    summary = metered.summarise(exc)

    assert summary.failure is Failure.REPAIRS_EXHAUSTED
    assert summary.usage.completions == 3
    assert not summary.ok


def test_summarise_of_a_clean_run_is_a_pass(analysis) -> None:
    metered = MeteredCompleter(scripted_from_plan(make_draft()))
    propose_plan(analysis, completer=metered)

    summary = metered.summarise(None)

    assert summary.ok
    assert not summary.negotiated
    assert summary.failure.as_outcome() is Outcome.PASS


def test_summarise_keeps_the_failure_when_the_caller_swallowed_the_exception() -> None:
    """``complete()`` raised, the caller handled it, and then asked for the row.

    The branch has always been there and has never been exercised. A row reporting a pass here is
    a row that lies about a live endpoint: the negotiation failed, and somebody merely caught it.
    """
    metered = MeteredCompleter(_completer(_schema_refused(), _schema_refused(), _schema_refused()))
    with pytest.raises(ProposalError):
        metered.complete(_request())

    summary = metered.summarise(None)

    assert summary.failure is Failure.SCHEMA_REFUSED
    assert not summary.ok
    assert summary.detail, "a classification with no evidence is unactionable"


def test_a_completer_that_issues_no_http_reports_no_waste_rather_than_a_deficit() -> None:
    """A scripted completer serves real completions and issues no requests: 0 - 1 = -1.

    The clamp returned the right number by arithmetic accident. This is the decision written down,
    and it is the offline path every test in this file runs through.
    """
    metered = MeteredCompleter(ScriptedCompleter(["{}"]))

    metered.complete(_request())

    assert metered.usage.requests == 0
    assert metered.usage.completions == 1
    assert metered.usage.wasted_requests == 0


def test_a_recovered_negotiation_does_not_read_as_an_endpoint_losing_its_own_numbers() -> None:
    """A 400 cannot carry a usage block, so it must not be in the denominator that judges one.

    Counting reports against *requests* printed ", 1 of them reported" on every recovered
    temperature refusal -- a leg that negotiated correctly and answered, rendered as a flaky
    endpoint. The denominator is the requests that were answered.
    """
    metered = MeteredCompleter(
        _completer(_temperature_refused(), _answer("{}", _usage(prompt=100, completion=10)))
    )

    metered.complete(_request())

    assert metered.usage.requests == 2
    assert metered.usage.answered == 1
    assert metered.usage.fully_reported
    assert "of them reported" not in metered.usage.describe()


def test_an_endpoint_that_reports_only_sometimes_says_so() -> None:
    """The other half of the same rule: a genuinely partial report is still called partial."""
    metered = MeteredCompleter(
        _completer(_answer("{}", _usage(prompt=100, completion=10)), _answer("{}"))
    )

    metered.complete(_request())
    metered.complete(_request())

    assert metered.usage.any_reported
    assert not metered.usage.fully_reported
    assert ", 1 of them reported" in metered.usage.describe()


def test_repeats_share_one_negotiated_session_unless_the_caller_asks_otherwise() -> None:
    """``metered()`` wraps the same completer every time, and its negotiation persists by design.

    Repeat two therefore reports ``degraded`` and ``negotiated`` having paid for neither -- true
    of the session, misleading about the repeat, and the only thing telling them apart is that the
    first repeat's requests carry the ladder and the rest do not. That reasoning lived in
    ``harness.sweep.run_leg``, which is the consumer rather than the API, so the next consumer
    would have had to rediscover it. ``renegotiate=`` is there for a caller who wants each repeat
    to measure a cold session instead.
    """
    completer = _completer(
        _temperature_refused(),
        _schema_refused(),
        _answer("{}"),
        _answer("{}"),
        _answer("{}"),
    )
    resolved = ResolvedModel(spec=ModelSpec(model="m"), completer=completer)

    first = resolved.metered()
    assert first is not None
    first.complete(_request())

    second = resolved.metered()
    assert second is not None
    second.complete(_request())

    assert first.usage.requests == 3, "the first repeat negotiated both parameters"
    assert second.usage.requests == 1, "the second inherited them and paid nothing"
    assert second.usage.wasted_requests == 0
    assert second.last is not None
    assert second.last.negotiated, "true of the session; it says nothing about this repeat"

    third = resolved.metered(renegotiate=True)

    assert third is not None
    assert completer.mode == "json_schema", "asked for a cold session before anything was sent"
    assert completer.omit_temperature is False

    third.complete(_request())

    assert third.last is not None
    assert not third.last.negotiated, "this repeat really did answer a full-strength request"


def test_one_metered_completer_is_not_safe_to_share_between_threads() -> None:
    """The constraint the module docstring states, asserted so it cannot change quietly.

    ``OpenAICompleter.usage`` is reset at the top of ``complete()`` and read once it returns, so
    two calls in flight through one meter read each other's arithmetic. A sweep is the obvious
    thing to parallelise and this is what it would report: two calls costing 100 and 250 billed as
    500. No lock is taken, because a lock here would serialise the model calls that are the whole
    of the elapsed time -- legs get a completer each instead.

    If somebody does make this safe, invert the assertion and rewrite the docstrings it cites.
    """

    class _Shared:
        """The shape of ``OpenAICompleter``: ``usage`` describes the call that just finished."""

        def __init__(self) -> None:
            self.usage = CompletionUsage()
            self.lock = threading.Lock()
            self.turn = 0
            self.first_inside = threading.Event()
            self.second_done = threading.Event()

        def complete(self, request: CompletionRequest) -> str:
            with self.lock:
                first = self.turn == 0
                self.turn += 1
            if first:
                self.usage = CompletionUsage(prompt=100, requests=1, answered=1, reported=1)
                self.first_inside.set()
                self.second_done.wait(timeout=5)
            else:
                self.first_inside.wait(timeout=5)
                self.usage = CompletionUsage(prompt=250, requests=1, answered=1, reported=1)
                self.second_done.set()
            return "{}"

    metered = MeteredCompleter(_Shared())
    threads = [threading.Thread(target=lambda: metered.complete(_request())) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert metered.usage.prompt == 500, "both records read the second call's numbers"
    assert metered.usage.prompt != 350, "which is not what the two calls cost"


def test_usage_records_add_so_repeats_can_be_aggregated() -> None:
    """`n > 1` is the point: one sample per model is noise."""
    one = MeteredUsage(prompt=10, completion=2, cached=1, requests=1, completions=1, reported=1)
    total = sum([one, one, one], MeteredUsage())

    assert total == MeteredUsage(
        prompt=30, completion=6, cached=3, requests=3, completions=3, reported=3
    )
    assert total.describe().startswith("30 prompt + 6 completion = 36 tokens, 3 cached")


# =============================================================================
# NEGATIVE CONTROLS: every failure fires on the exception that should produce it
# =============================================================================


def test_nothing_raised_is_not_a_failure() -> None:
    assert classify_failure(None) is Failure.NONE
    assert not Failure.NONE


def test_a_triage_refusal_is_attributed_to_kedge_and_not_to_the_model() -> None:
    """The model was never asked. Blaming it for a workbook kedge declined to plan is a lie."""
    stopped = make_analysis(workbook_fields={"file_format": "xlsb"})
    exc = _raised_by(
        lambda: propose_plan(stopped, completer=ScriptedCompleter([make_draft().model_dump_json()]))
    )

    assert isinstance(exc, ProposalRefusedError)
    assert classify_failure(exc) is Failure.TRIAGE_REFUSED


def test_repairs_exhausted_is_the_one_failure_that_is_about_the_model(analysis) -> None:
    exc = _raised_by(
        lambda: propose_plan(analysis, completer=ScriptedCompleter(["nope", "nope", "nope"]))
    )

    assert classify_failure(exc) is Failure.REPAIRS_EXHAUSTED


def test_an_empty_completion_is_not_an_invalid_plan() -> None:
    """A model that said nothing and a model that said the wrong thing need different fixes."""
    completer = _completer(None)
    exc = _raised_by(lambda: completer.complete(_request()))

    assert classify_failure(exc) is Failure.EMPTY_RESPONSE


def test_an_endpoint_that_refuses_every_structured_output_mode_is_named_as_such() -> None:
    completer = _completer(_schema_refused(), _schema_refused(), _schema_refused())
    exc = _raised_by(lambda: completer.complete(_request()))

    assert classify_failure(exc) is Failure.SCHEMA_REFUSED


def test_a_temperature_refused_on_every_request_is_not_reported_as_a_schema_refusal() -> None:
    """The exact live-run misdiagnosis recorded in `tests/llm/README.md`, as a regression test.

    Every request comes back refusing the explicit temperature. kedge drops it on the first, then
    -- since the refusals keep coming -- walks the structured-output ladder to the bottom and
    gives up. A classifier reading only "no fallback left" reports that as an endpoint with no
    structured output support, which is what sent the diagnosis in the wrong direction and
    produced no plans against any workbook. The 400 underneath says ``temperature``, and it is
    the 400 that must be believed.
    """
    completer = _completer(
        _temperature_refused(),
        _temperature_refused(),
        _temperature_refused(),
        _temperature_refused(),
    )
    exc = _raised_by(lambda: completer.complete(_request()))

    assert completer.omit_temperature
    assert classify_failure(exc) is Failure.TEMPERATURE_REFUSED
    assert classify_failure(exc) is not Failure.SCHEMA_REFUSED


def test_a_recovered_temperature_refusal_does_not_claim_a_later_schema_refusal() -> None:
    """The mirror image of the test above, and the reason the classifier reads `__cause__` only.

    Here the temperature refusal was recovered from -- kedge dropped the parameter and carried on
    -- and what actually ended the call was an endpoint with no structured-output support at all.
    Threading the implicit exception context would find the temperature 400 buried under the
    schema ones and blame the negotiation that worked.
    """
    completer = _completer(
        _temperature_refused(), _schema_refused(), _schema_refused(), _schema_refused()
    )
    exc = _raised_by(lambda: completer.complete(_request()))

    assert completer.omit_temperature
    assert classify_failure(exc) is Failure.SCHEMA_REFUSED


def test_a_timeout_is_not_reported_as_a_transport_error() -> None:
    """`APITimeoutError` subclasses `APIConnectionError`, so the order of those two checks is
    load-bearing: reversed, every timeout sends somebody to look at their proxy instead of at
    `timeout_seconds`."""
    completer = _completer(APITimeoutError(request=httpx.Request("POST", _URL)))
    exc = _raised_by(lambda: completer.complete(_request()))

    assert classify_failure(exc) is Failure.TIMEOUT
    assert classify_failure(exc) is not Failure.TRANSPORT


def test_a_dropped_connection_is_a_transport_failure() -> None:
    completer = _completer(APIConnectionError(request=httpx.Request("POST", _URL)))
    exc = _raised_by(lambda: completer.complete(_request()))

    assert classify_failure(exc) is Failure.TRANSPORT


def test_a_429_is_about_the_sweeps_pacing_and_not_the_models_ability() -> None:
    completer = _completer(_status(RateLimitError, "rate limit reached", 429))
    exc = _raised_by(lambda: completer.complete(_request()))

    assert classify_failure(exc) is Failure.RATE_LIMITED


def test_a_rejected_key_is_authentication_rather_than_a_missing_configuration() -> None:
    completer = _completer(_status(AuthenticationError, "invalid api key", 401))
    exc = _raised_by(lambda: completer.complete(_request()))

    assert classify_failure(exc) is Failure.AUTHENTICATION


def test_a_404_is_a_fact_about_the_account_and_not_about_the_model() -> None:
    """The first thing that happens on the first live run of a six-model sweep.

    One model id in the list is not enabled for the key, the endpoint says so, and the leg used to
    land in UNKNOWN -- which mapped to FAIL. A model that does not exist here cannot have failed
    at anything, and the fix is one line of the sweep's own configuration.
    """
    message = "The model `gpt-5-turbo` does not exist or you do not have access to it."
    completer = _completer(_status(NotFoundError, message, 404))
    exc = _raised_by(lambda: completer.complete(_request()))

    assert classify_failure(exc) is Failure.MODEL_NOT_FOUND
    assert not classify_failure(exc).about_the_model
    assert classify_failure(exc).as_outcome() is Outcome.SKIP


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_status(InternalServerError, "internal server error", 500), Failure.ENDPOINT_ERROR),
        (_status(InternalServerError, "service unavailable", 503), Failure.ENDPOINT_ERROR),
        (_status(UnprocessableEntityError, "cannot process that", 422), Failure.REQUEST_REJECTED),
        (_status(ConflictError, "another request is in flight", 409), Failure.REQUEST_REJECTED),
    ],
)
def test_a_status_error_the_classifier_had_no_case_for_is_no_longer_scored_as_a_model_failure(
    error: Any, expected: Failure
) -> None:
    """Four rejections that fell through to UNKNOWN, and UNKNOWN used to mean FAIL.

    None of them says anything about a model's ability to decompose a workbook: two are the
    endpoint breaking and two are a proxy refusing the shape of a request kedge never got to
    renegotiate.
    """
    completer = _completer(error)
    exc = _raised_by(lambda: completer.complete(_request()))

    assert classify_failure(exc) is expected
    assert classify_failure(exc) is not Failure.UNKNOWN
    assert classify_failure(exc).as_outcome() is Outcome.SKIP


def test_a_temperature_refusal_delivered_as_a_422_is_still_named_as_one() -> None:
    """``OpenAICompleter`` degrades on ``BadRequestError`` and on nothing else.

    A proxy that answers an unwelcome ``temperature`` with a 422 rather than a 400 is never
    offered the fallback: the call dies where a 400 would have been renegotiated and answered.
    Naming the parameter is the difference between "drop the temperature for this endpoint" and
    "this model cannot plan a workbook" -- the same misdiagnosis ``tests/llm/README.md`` records,
    arriving under a different status code.
    """
    message = "Unsupported value: 'temperature' does not support 0.2 with this model."
    body = {"error": {"message": message, "param": "temperature"}}
    completer = _completer(_status(UnprocessableEntityError, message, 422, body))
    exc = _raised_by(lambda: completer.complete(_request()))

    assert classify_failure(exc) is Failure.TEMPERATURE_REFUSED
    assert classify_failure(exc) is not Failure.REQUEST_REJECTED


def test_a_raw_httpx_timeout_is_a_timeout_rather_than_an_unrecognised_failure() -> None:
    """The SDK does not translate everything, and this exception reaches kedge on this project."""
    completer = _completer(httpx.ReadTimeout("timed out reading the response"))
    exc = _raised_by(lambda: completer.complete(_request()))

    assert classify_failure(exc) is Failure.TIMEOUT
    assert classify_failure(exc) is not Failure.UNKNOWN


def test_a_raw_httpx_transport_error_is_a_transport_failure() -> None:
    completer = _completer(httpx.ConnectError("could not resolve the host"))
    exc = _raised_by(lambda: completer.complete(_request()))

    assert classify_failure(exc) is Failure.TRANSPORT


def test_a_model_talking_about_empty_responses_cannot_shed_the_exhausted_repairs(analysis) -> None:
    """The one failure this module calls genuinely about the model, and the model got a vote.

    kedge's exhausted-repairs message quotes up to 200 characters of the response back per
    attempt (``parse_draft``), so a reply that happens to contain the words "returned an empty
    response" matched the empty-response pattern from inside kedge's own message -- and the model
    talked its way out of the only attribution that counts against it. The patterns are anchored
    to the openings of kedge's messages now, which the model does not write.
    """
    reply = "Sorry, the upstream tool returned an empty response so I cannot plan."
    exc = _raised_by(lambda: propose_plan(analysis, completer=ScriptedCompleter([reply] * 3)))

    assert "returned an empty response" in str(exc), "the model's words really are in the message"
    assert classify_failure(exc) is Failure.REPAIRS_EXHAUSTED
    assert classify_failure(exc) is not Failure.EMPTY_RESPONSE
    assert classify_failure(exc).about_the_model


def test_the_message_that_ended_the_call_wins_over_one_further_down_the_chain() -> None:
    """Ordering, made load-bearing and asserted in both directions.

    The classifier walks ``__cause__`` outermost first, so whichever kedge message ended the call
    is the one believed. Reversing that walk reports the first thing that went wrong rather than
    the thing that stopped the run, which for a repair loop is the opposite of the truth.
    """
    empty = ProposalError("the model endpoint returned an empty response")
    exhausted = ProposalError(
        "the model did not produce a valid process plan in 3 attempt(s) against a.xlsx:\n"
        "attempt 1: the model returned no JSON object: 'nope'"
    )

    exhausted.__cause__ = empty
    assert classify_failure(exhausted) is Failure.REPAIRS_EXHAUSTED

    exhausted.__cause__ = None
    empty.__cause__ = exhausted
    assert classify_failure(empty) is Failure.EMPTY_RESPONSE


def test_a_message_that_merely_quotes_kedges_wording_is_not_classified_by_it() -> None:
    """Anchored to the start of kedge's own message, not matched anywhere inside a string.

    The chain's text includes whatever the endpoint and the model said, and both of them can say
    anything at all.
    """
    quoted = ProposalError(
        "the model endpoint could not be reached or refused the request: upstream said "
        "'the model endpoint returned an empty response'"
    )

    assert classify_failure(quoted) is Failure.UNKNOWN


def test_the_exception_class_is_believed_over_the_code_on_the_response() -> None:
    """Types first; codes only where the SDK has no class, and this is why ``_status`` insists.

    Building every status error on one response code -- a 429, as this file used to -- makes a
    type-reading classifier and a code-reading one indistinguishable, so the authentication test
    proved nothing about either. Here the two disagree on purpose. The SDK's mapping from status
    to exception class is the authority; the code is a fallback for a status this version has no
    class for, and reading it first would misclassify anything constructed by hand.
    """
    completer = _completer(_status(AuthenticationError, "invalid api key", 429))
    exc = _raised_by(lambda: completer.complete(_request()))

    assert classify_failure(exc) is Failure.AUTHENTICATION
    assert classify_failure(exc) is not Failure.RATE_LIMITED


def test_a_status_error_with_no_class_of_its_own_is_classified_by_its_code() -> None:
    """The fallback, for the 4xx a future SDK grows a class for and this one has not."""
    from openai import APIStatusError

    completer = _completer(_status(APIStatusError, "gateway is having a moment", 503))
    exc = _raised_by(lambda: completer.complete(_request()))

    assert classify_failure(exc) is Failure.ENDPOINT_ERROR


def test_a_missing_keyring_entry_is_never_attributed_to_the_model() -> None:
    exc = MissingApiKeyError("no API key found in the OS keyring for service 'kedge'")

    assert classify_failure(exc) is Failure.NOT_CONFIGURED
    assert classify_failure(exc).as_outcome() is Outcome.SKIP


def test_an_unrecognised_failure_is_visible_as_unrecognised() -> None:
    """Bucketing an unknown cause as a known one is worse than admitting it is unknown."""
    assert classify_failure(ProposalError("the flux capacitor is at an angle")) is Failure.UNKNOWN


_ABOUT_THE_MODEL = {
    Failure.SCHEMA_REFUSED,
    Failure.TEMPERATURE_REFUSED,
    Failure.EMPTY_RESPONSE,
    Failure.REPAIRS_EXHAUSTED,
}
"""Written out here independently of the implementation, so this is a second opinion rather than
a restatement. A member added to `Failure` without a decision about which half it belongs in
fails the test below, which is the point: the decision must be made in a review."""


def test_every_failure_declares_whether_it_is_about_the_model_and_the_outcome_follows() -> None:
    """SKIP is not FAIL. The repo's cardinal rule, over the whole enum, exhaustively.

    `as_outcome` used to map everything except `NOT_CONFIGURED` to FAIL, which contradicted three
    members' own docstrings: a 429 "says nothing about the model's ability", a triage refusal means
    "the model was never asked", and `AUTHENTICATION` blamed the model for a rejected key while
    `NOT_CONFIGURED` -- an absent one -- did not. `UNKNOWN` was the worst of it: "we could not
    attribute this, therefore the model failed" is not an attribution, and that bucket is where an
    unrecognised 404 or 5xx lands.
    """
    assert set(Failure) - {Failure.NONE} - _ABOUT_THE_MODEL, (
        "sanity: some failures are not the model's"
    )
    for failure in Failure:
        assert bool(failure) is (failure is not Failure.NONE)
        if failure is Failure.NONE:
            assert failure.as_outcome() is Outcome.PASS
            assert not failure.about_the_model
            continue
        assert failure.about_the_model is (failure in _ABOUT_THE_MODEL), failure
        expected = Outcome.FAIL if failure in _ABOUT_THE_MODEL else Outcome.SKIP
        assert failure.as_outcome() is expected, failure


def test_a_timeout_does_not_blame_the_model_for_kedges_read_gap() -> None:
    """The counter-intuitive one, called out on its own because it will look wrong to a reader.

    `[model] timeout_seconds` is httpx's gap *between reads*, not a budget for the whole answer
    (CLAUDE.md), so an endpoint that goes quiet while a reasoning model thinks trips it while
    working perfectly. Scoring that as a model failure grades the model on kedge's own setting,
    and the model that thinks hardest loses.
    """
    assert not Failure.TIMEOUT.about_the_model
    assert Failure.TIMEOUT.as_outcome() is Outcome.SKIP


def test_a_failure_read_back_out_of_json_must_be_re_entered_through_the_enum() -> None:
    """`bool(Failure.NONE)` is False; `bool("none")` is True, and JSON leaves the string behind.

    A consumer of `harness.sweep.as_json` writing `if document["failure"]:` reads a clean run as
    a failure -- and every other member as one too, so the bug is invisible until the day it
    matters. Documented on the enum; asserted here so the trap is written down somewhere that
    runs.
    """
    assert bool(Failure.NONE) is False
    assert bool(Failure.NONE.value) is True, "the trap: the value is a non-empty string"
    assert Failure(Failure.NONE.value) is Failure.NONE
    assert not Failure(Failure.NONE.value)


def test_a_cause_buried_under_a_proposal_error_is_still_found() -> None:
    """Everything the planning loop raises wraps the thing that actually went wrong."""
    inner = APITimeoutError(request=httpx.Request("POST", _URL))
    outer = ProposalError("the model endpoint could not be reached")
    outer.__cause__ = inner

    assert classify_failure(outer) is Failure.TIMEOUT


def test_a_cyclic_cause_chain_does_not_hang_the_classifier() -> None:
    first = ProposalError("first")
    second = ProposalError("second")
    first.__cause__ = second
    second.__cause__ = first

    assert classify_failure(first) is Failure.UNKNOWN


# =============================================================================
# RESOLVING A SWEEP LEG
# =============================================================================


@pytest.fixture
def kedge_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A kedge directory with an endpoint configured, and a cwd with no project config in it."""
    home = tmp_path / "kedge-home"
    home.mkdir()
    (home / "config.toml").write_text(
        '[model]\nbase_url = "https://endpoint.invalid/v1"\nmodel = "configured-default"\n',
        encoding="utf-8",
    )
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    return home


def _keyring(monkeypatch: pytest.MonkeyPatch, secret: str | None) -> None:
    """Stub the OS keyring, so this file passes on a machine that has no backend at all."""
    monkeypatch.setattr("kedge.config.keyring.get_password", lambda service, ref: secret)


def test_a_resolved_leg_carries_the_specs_overrides_over_the_users_config(
    kedge_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _keyring(monkeypatch, "secret")
    spec = ModelSpec(model="challenger", base_url="https://other.invalid/v2", label="leg-b")

    resolved = resolve_model(spec, kedge_home=kedge_home)

    assert resolved.ready
    assert resolved.failure is Failure.NONE
    assert resolved.outcome is Outcome.PASS
    assert resolved.completer is not None
    assert resolved.completer._model == "challenger"
    assert resolved.spec.name == "leg-b"
    assert isinstance(resolved.metered(), MeteredCompleter)


def test_an_unnamed_endpoint_falls_back_to_the_users_own(
    kedge_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _keyring(monkeypatch, "secret")

    resolved = resolve_model(ModelSpec(model="challenger"), kedge_home=kedge_home)

    assert resolved.ready
    assert str(resolved.completer._client.base_url).startswith("https://endpoint.invalid/v1")


def test_a_leg_with_no_key_in_the_keyring_is_a_skip_and_still_produces_a_row(
    kedge_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sweep across six models where two are unconfigured must still print six rows."""
    _keyring(monkeypatch, None)

    resolved = resolve_model(ModelSpec(model="unconfigured"), kedge_home=kedge_home)

    assert not resolved.ready
    assert resolved.failure is Failure.NOT_CONFIGURED
    assert resolved.outcome is Outcome.SKIP
    assert resolved.metered() is None
    assert "keyring" in resolved.detail
    assert "keyring set kedge" in resolved.detail


def test_a_config_file_that_will_not_parse_is_reported_rather_than_raised(
    kedge_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _keyring(monkeypatch, "secret")
    (kedge_home / "config.toml").write_text("[model\nbroken", encoding="utf-8")

    resolved = resolve_model(ModelSpec(model="m"), kedge_home=kedge_home)

    assert resolved.failure is Failure.NOT_CONFIGURED
    assert "not valid TOML" in resolved.detail


def test_a_base_url_with_no_scheme_is_refused_as_configuration_not_as_a_model_failure(
    kedge_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _keyring(monkeypatch, "secret")
    spec = ModelSpec(model="m", base_url="endpoint.invalid/v1")

    resolved = resolve_model(spec, kedge_home=kedge_home)

    assert resolved.failure is Failure.NOT_CONFIGURED
    assert resolved.outcome is Outcome.SKIP


def test_a_kedge_toml_in_the_working_directory_cannot_capture_a_pinned_sweep(
    kedge_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running a sweep from inside a kedge project is the ordinary case, not a corner one.

    kedge's config is the user layer overridden by ``./kedge.toml``, and the project layer
    resolves from the working directory -- so pinning ``kedge_home`` used to be silently
    overridden by whatever project the sweep happened to be launched from. Every leg went to that
    project's ``base_url``, under that project's keyring entry, and six models measured one
    endpoint while the table said six.

    The fixture's ``chdir`` into an empty directory was the workaround. This test removes it.
    """
    (Path.cwd() / "kedge.toml").write_text(
        '[model]\nbase_url = "https://decoy.invalid/v9"\napi_key_ref = "decoy"\n',
        encoding="utf-8",
    )
    asked: list[str] = []

    def get_password(service: str, ref: str) -> str:
        asked.append(ref)
        return "secret"

    monkeypatch.setattr("kedge.config.keyring.get_password", get_password)

    resolved = resolve_model(ModelSpec(model="challenger"), kedge_home=kedge_home)

    assert resolved.ready
    assert resolved.completer is not None
    assert str(resolved.completer._client.base_url).startswith("https://endpoint.invalid/v1")
    assert "decoy" not in asked, "the leg used the decoy project's keyring entry"


def test_a_project_layer_is_still_read_when_the_caller_names_one(
    kedge_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pinned, not abolished. A caller sweeping a particular project can still say which."""
    _keyring(monkeypatch, "secret")
    project = tmp_path / "a-real-project"
    project.mkdir()
    (project / "kedge.toml").write_text(
        '[model]\nbase_url = "https://project.invalid/v3"\n', encoding="utf-8"
    )

    resolved = resolve_model(
        ModelSpec(model="challenger"), kedge_home=kedge_home, project_dir=project
    )

    assert resolved.completer is not None
    assert str(resolved.completer._client.base_url).startswith("https://project.invalid/v3")


def test_a_mixed_sweep_produces_a_row_for_every_model_it_was_given(
    kedge_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole reason `resolve_model` returns a value instead of raising."""
    keys = {"present": "secret"}
    monkeypatch.setattr("kedge.config.keyring.get_password", lambda service, ref: keys.get(ref))
    specs = [
        ModelSpec(model="a", api_key_ref="present"),
        ModelSpec(model="b", api_key_ref="absent"),
        ModelSpec(model="c", api_key_ref="present"),
    ]

    resolved = [resolve_model(spec, kedge_home=kedge_home) for spec in specs]

    assert [item.outcome for item in resolved] == [Outcome.PASS, Outcome.SKIP, Outcome.PASS]
    assert [item.spec.name for item in resolved] == ["a", "b", "c"]
