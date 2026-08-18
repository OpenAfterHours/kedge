"""Measuring a live model: what it cost, how long it took, and -- if it failed -- why.

The rest of ``harness/`` grades artifacts that are already committed. This module is the seam
where a real model gets involved, and it exists because the two questions a per-model sweep has
to answer are both invisible from the outside of :class:`~kedge.plan.propose.Completer`.

**What did it cost.** The protocol returns a bare string, so a decorator can time a call and
count calls and nothing else. :class:`~kedge.plan.propose.OpenAICompleter` now keeps what the
endpoint reported on ``.usage``; :class:`MeteredCompleter` reads it off whatever it wraps, and
falls back to "nothing was reported" -- distinctly, never to zero -- for a delegate that does not
keep one. That fallback is not a corner case. A :class:`~kedge.plan.propose.ScriptedCompleter`
reports nothing because there was no endpoint, and half the OpenAI-compatible servers people
point kedge at report nothing because they never implemented ``usage``. A table that renders
either as ``0 tokens`` has just declared the cheapest model in the sweep.

**Why did it score zero.** This is the requirement everything else here serves. A model that
returned no plan may have refused the JSON schema, refused an explicit ``temperature``, timed
out, dropped the connection, returned prose that no repair round could parse, or never been
asked because there is no key in the keyring for it. Those are six different findings and one of
them is not even about the model. Rendering them identically is how a negotiation bug gets a
model blamed -- which has already happened once on this project, recorded in
``tests/llm/README.md``: twenty-four requests refused over ``temperature``, misread as
"structured output unsupported", and no plans produced against any workbook. :class:`Failure`
is a closed enum over exactly that space, and :func:`classify_failure` maps an exception raised
by :func:`~kedge.plan.propose.propose_plan` into it.

Two consequences of that history are built in on purpose:

- **A recovered temperature refusal is not a failure.** The endpoint said no, kedge dropped the
  parameter, the model answered. That is a negotiation that succeeded, and it lands as
  :attr:`Failure.NONE` with :attr:`MeteredCall.negotiated` set -- because the fact is worth
  reporting and the verdict is not "this model failed".
- **Four failures are about the model; the rest are about the plumbing.**
  :attr:`Failure.about_the_model` is where each member answers that question for itself, and
  :meth:`Failure.as_outcome` is derived from it rather than from a list of exceptions to a
  blanket FAIL. An unconfigured leg is the obvious SKIP, and so -- for the same reason -- are a
  401, a 404 for a model this key is not enabled for, a 429, a 5xx, a dropped connection and the
  unattributed remainder. "We could not attribute it, therefore the model failed" is not an
  attribution; it is the reconciliation sin with a different noun. A sweep across six models
  where two have no keyring entry must still produce a table, and those two rows must not read as
  a model that answered badly.
- **A timeout is a SKIP too, and that is the counter-intuitive one.** ``[model]
  timeout_seconds`` is httpx's gap *between reads* rather than a budget for the whole answer
  (CLAUDE.md), so an endpoint that goes quiet while a reasoning model thinks trips it while
  working perfectly. Scoring that as a model failure blames the model for kedge's read-gap
  setting. It is a SKIP that has to be **loud**: a leg that was asked and did not answer is not
  an untested one, and ``harness/sweep.py`` says so above the table rather than leaving the row
  to read like an absent keyring entry.

**Nothing here is thread-safe, and a sweep is the obvious thing to parallelise.** One
:class:`MeteredCompleter` driven from two threads double-counts, because
:attr:`~kedge.plan.propose.OpenAICompleter.usage` is reset at the top of every ``complete()`` and
read after it returns, so two calls in flight read each other's arithmetic -- measured at 300
prompt tokens over 3 requests where the truth was 200 over 2. No lock is taken: a lock here would
serialise the model calls it was added to protect, which is the whole of the elapsed time, and the
constraint it would buy is one a caller can satisfy for free. **Parallelise across legs, never
within one**: :func:`~harness.sweep.run_leg` resolves a completer per leg, so separate legs share
no mutable state, while the repeats inside a leg deliberately share one negotiated session.

Nothing here calls a network by itself. Everything is exercised offline in
``tests/unit/test_evals_live.py``, including every branch of the classifier -- a classifier that
has only ever seen a good run has never had its polarity checked.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from harness.model import Outcome

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from kedge.plan.propose import Completer, CompletionRequest, OpenAICompleter

logger = logging.getLogger(__name__)

__all__ = [
    "Failure",
    "MeteredCall",
    "MeteredCompleter",
    "MeteredUsage",
    "ModelSpec",
    "ResolvedModel",
    "classify_failure",
    "resolve_model",
]


# =============================================================================
# COST
# =============================================================================


@dataclass(frozen=True, slots=True)
class MeteredUsage:
    """Tokens, requests and completions for one call or for a whole sweep leg.

    Three counts that are routinely confused and must not be:

    - :attr:`completions` is how many times somebody called ``complete()``. One proposal is one
      completion plus one per repair round.
    - :attr:`requests` is how many HTTP requests those completions issued. It is larger whenever
      a negotiation happened, because the temperature retry and the
      ``json_schema -> json_object -> text`` ladder both retry inside a single ``complete()``. A
      model that burned three requests to answer once is not the same result as one that answered
      first time, and the only way to see that in a table is to carry both numbers.
    - :attr:`reported` is how many of those requests came back with usable numbers in the
      endpoint's own ``usage`` block. It is the honesty field: ``reported == 0`` means the token
      counts below are not measurements of anything, and :attr:`any_reported` is what a renderer
      should ask before printing them. "The endpoint said nothing" and "the endpoint said zero"
      are different facts and a cost comparison that folds them together is worse than one that
      omits the model.
    - :attr:`answered` is how many requests came back at all. It is the denominator
      :attr:`reported` belongs over, and it is not :attr:`requests`: the 400 that starts a
      negotiation could never have carried a usage block, so measuring completeness against the
      request count reports every recovered negotiation as a flaky endpoint.

    :attr:`any_reported` is spelled that way rather than ``measured`` because
    :attr:`kedge.agent.loop._Meter.measured` -- kedge's other token accountant -- means *every*
    step reported, where this means *any* request did. Both were called ``measured`` once, and
    ``if usage.measured: print(usage.total)`` printed a partial total as though it were the bill.
    :attr:`fully_reported` is the one that means what ``_Meter.measured`` means.

    Addable, so ``sum(..., MeteredUsage())`` aggregates the repeats of an ``n > 1`` sweep without
    anybody re-deriving the arithmetic.

    Example:
        >>> MeteredUsage(prompt=8_000, completion=120, requests=1, reported=1, completions=1).total
        8120
        >>> MeteredUsage(requests=3, completions=1).any_reported
        False
    """

    prompt: int = 0
    completion: int = 0
    cached: int = 0
    """Prompt tokens the endpoint served from cache. Part of :attr:`prompt`, not extra to it."""
    requests: int = 0
    """HTTP requests issued, including the ones a negotiation threw away."""
    completions: int = 0
    """Logical ``complete()`` calls."""
    answered: int = 0
    """How many requests came back with a response rather than raising."""
    reported: int = 0
    """How many answered requests carried usable numbers in the endpoint's usage block."""

    @property
    def total(self) -> int:
        """Prompt plus completion, cache or no cache."""
        return self.prompt + self.completion

    @property
    def any_reported(self) -> bool:
        """Whether any token count here is the endpoint's arithmetic rather than an assumed zero.

        The guard a renderer owes its reader. False means print "not reported", never "0".
        """
        return self.reported > 0

    @property
    def fully_reported(self) -> bool:
        """Whether every request that could have carried numbers did.

        Over :attr:`answered`, never over :attr:`requests` -- see the class docstring. This is the
        property that means what :attr:`kedge.agent.loop._Meter.measured` means.

        ``>=`` rather than ``==`` so a record assembled by hand, or by a delegate that keeps no
        answered count of its own, is not reported as partial on the strength of a field it never
        filled in.
        """
        return self.reported > 0 and self.reported >= self.answered

    @property
    def wasted_requests(self) -> int:
        """Requests beyond one per completion -- what the negotiations and repairs cost.

        A delegate that issues no HTTP at all -- a scripted completer, whose completions are real
        and whose requests are zero -- has no waste rather than a negative one. The clamp below it
        would report the same number, but by arithmetic accident rather than by decision, and the
        decision is the part worth writing down.
        """
        if not self.requests:
            return 0
        return max(0, self.requests - self.completions)

    def __add__(self, other: MeteredUsage) -> MeteredUsage:
        """Field-wise sum, so repeats and legs aggregate without losing the honesty field."""
        if not isinstance(other, MeteredUsage):  # pragma: no cover - guarded for symmetry
            return NotImplemented
        return MeteredUsage(
            prompt=self.prompt + other.prompt,
            completion=self.completion + other.completion,
            cached=self.cached + other.cached,
            requests=self.requests + other.requests,
            completions=self.completions + other.completions,
            answered=self.answered + other.answered,
            reported=self.reported + other.reported,
        )

    def __radd__(self, other: Any) -> MeteredUsage:
        """Support ``sum()``, whose start value is ``0``."""
        if other == 0:
            return self
        return NotImplemented

    def describe(self) -> str:
        """One cell of a table: the counts, or an explicit statement that there are none.

        The "of them reported" clause counts against :attr:`answered`, not against
        :attr:`requests`. Against requests it fired on every recovered negotiation -- a 400 has no
        usage block to volunteer, so a leg that dropped the temperature and then answered
        perfectly printed "1 of them reported" and read as an endpoint losing its own numbers.
        """
        shape = f"{self.completions} completion(s), {self.requests} request(s)"
        if not self.any_reported:
            return f"no usage reported ({shape})"
        cache = f", {self.cached} cached" if self.cached else ""
        partial = "" if self.fully_reported else f", {self.reported} of them reported"
        return (
            f"{self.prompt} prompt + {self.completion} completion = {self.total} tokens"
            f"{cache} ({shape}{partial})"
        )


def _usage_of(delegate: Any) -> MeteredUsage:
    """Read a completer's own usage record, or say that it keeps none.

    Duck-typed rather than isinstance-checked against
    :class:`~kedge.plan.propose.CompletionUsage`, because the point of the ``Completer`` protocol
    is that anything satisfying it can be metered -- a scripted completer, a recording proxy, a
    fake in a test. A delegate with no ``usage`` attribute is the ordinary case, not an error,
    and it yields ``reported=0``: no tokens were measured, and nothing here will pretend they
    were zero.
    """
    reported = getattr(delegate, "usage", None)
    if reported is None:
        return MeteredUsage(completions=1)
    return MeteredUsage(
        prompt=_int(reported, "prompt"),
        completion=_int(reported, "completion"),
        cached=_int(reported, "cached"),
        requests=_int(reported, "requests"),
        completions=1,
        answered=_int(reported, "answered"),
        reported=_int(reported, "reported"),
    )


def _int(source: Any, name: str) -> int:
    """One count off a delegate's usage record, defended the same way the record itself is.

    ``int()`` is not a guard: ``int(None)`` and ``int("8,000")`` both raise, and a delegate is
    whatever satisfies the protocol. Metering must not be the thing that turns a completed plan
    into a traceback, so anything unreadable counts as zero here -- which is safe only because
    :attr:`MeteredUsage.reported` is read the same way, so an unreadable record reports nothing
    rather than reporting zero tokens as a measurement.
    """
    try:
        value = getattr(source, name, 0)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return 0
        return int(value)
    except Exception:
        logger.debug("a usage record was unreadable at %s; counting it as zero", name)
        return 0


# =============================================================================
# FAILURE
# =============================================================================


class Failure(StrEnum):
    """Why a model produced no plan -- a closed set, so a zero can always be attributed.

    Closed on purpose. An open string field would let every new endpoint invent its own wording
    and a cross-model table would end up with one column per proxy. :attr:`UNKNOWN` is the escape
    hatch and it carries the exception text alongside it, so an unrecognised failure is visible
    as unrecognised rather than silently bucketed as something it is not.

    A :class:`~enum.StrEnum`, so it serialises to its own value and back -- but only if a consumer
    re-enters through the constructor. ``bool(Failure.NONE)`` is ``False`` and ``bool("none")``,
    which is what a JSON round trip leaves behind, is ``True``. Anything reading this back out of
    :func:`harness.sweep.as_json` must do ``Failure(document["failure"])`` before testing it;
    ``if document["failure"]:`` reads a clean run as a failure and every other member as one too.

    Example:
        >>> bool(Failure.NONE), bool(Failure.TIMEOUT)
        (False, True)
        >>> Failure("none") is Failure.NONE
        True
        >>> Failure.NOT_CONFIGURED.as_outcome() is Outcome.SKIP
        True
    """

    NONE = "none"
    """The call succeeded. Possibly after a negotiation -- see :attr:`MeteredCall.negotiated`."""

    TRIAGE_REFUSED = "triage_refused"
    """kedge itself declined to plan the workbook. The model was never asked."""

    NOT_CONFIGURED = "not_configured"
    """No keyring entry, no readable config, no endpoint. Not the model's fault; a SKIP."""

    SCHEMA_REFUSED = "schema_refused"
    """The endpoint rejected every structured-output mode, down to plain text."""

    TEMPERATURE_REFUSED = "temperature_refused"
    """The endpoint rejected an explicit temperature and kedge could not recover from it.

    A *recovered* refusal is not this. It is :attr:`NONE`, with the renegotiation recorded --
    the distinction ``tests/llm/README.md`` says cost a whole sweep.
    """

    TIMEOUT = "timeout"
    """The endpoint accepted the request and did not answer in time.

    A SKIP, and the one member where that will look wrong. ``[model] timeout_seconds`` is httpx's
    gap *between reads*, not a budget for the whole answer, so an endpoint that goes quiet while a
    reasoning model thinks trips it while working perfectly (CLAUDE.md). Scoring this against the
    model grades it on kedge's own setting, and the model that thinks hardest loses.
    """

    TRANSPORT = "transport"
    """The connection failed: DNS, TLS, a proxy, a reset. The request may never have arrived."""

    RATE_LIMITED = "rate_limited"
    """A 429. Says nothing about the model's ability and everything about the sweep's pacing."""

    AUTHENTICATION = "authentication"
    """A 401 or 403. The key exists but the endpoint will not take it for this model.

    A SKIP, like :attr:`NOT_CONFIGURED`: a key that is absent and a key that is refused are the
    same fact about the sweep's credentials, and letting the *reason* a leg was unrunnable decide
    whether the model gets blamed for it is not a distinction anybody can act on.
    """

    MODEL_NOT_FOUND = "model_not_found"
    """A 404: this endpoint has no such model, or this key is not entitled to it.

    The first thing that happens on the first live run of a six-model sweep, and a fact about the
    account rather than about the model -- there is no model here to have failed. It has its own
    member because it is the one failure whose fix is a single line of the sweep's own config.
    """

    ENDPOINT_ERROR = "endpoint_error"
    """A 5xx. The endpoint broke; nothing was learned about the model."""

    REQUEST_REJECTED = "request_rejected"
    """A 4xx that is not any of the above -- a 422 or a 409 from a proxy with its own opinions.

    Distinct from :attr:`SCHEMA_REFUSED` because that one is the *negotiation* running out, which
    kedge drove deliberately and can report on. This is a rejection kedge never got to negotiate:
    ``OpenAICompleter`` degrades on ``BadRequestError`` alone, so a proxy that answers an
    unwelcome ``temperature`` with a 422 rather than a 400 is never offered the fallback. Where
    the body names the parameter the classifier says so; where it does not, it says this rather
    than inventing an attribution.
    """

    EMPTY_RESPONSE = "empty_response"
    """The endpoint returned a completion with no content at all."""

    REPAIRS_EXHAUSTED = "repairs_exhausted"
    """Every attempt parsed as something, and none of it validated as a plan.

    The one failure that is genuinely about the model's competence rather than the plumbing.
    """

    UNKNOWN = "unknown"
    """Something else. The exception text travels alongside it, on ``detail``, so the next person
    to meet it can add a case for it rather than guess."""

    def __bool__(self) -> bool:
        """Truthy only when there *is* a failure, so ``if call.failure:`` reads correctly."""
        return self is not Failure.NONE

    @property
    def about_the_model(self) -> bool:
        """Whether this is a result about the model, or a fact about everything around it.

        The question the whole module exists to answer, asked once and answered by the member
        itself rather than by whoever happens to be rendering it. Four failures are results: the
        endpoint refused structured output down to plain text, it refused a temperature and
        nothing recovered it, it returned a completion with nothing in it, and the model's own
        output exhausted the repair budget. Those describe this model against this endpoint, and
        a sweep exists to compare them.

        Everything else is the plumbing, the account or the sweep's own pacing -- and
        :attr:`UNKNOWN` is emphatically in that half. "We could not attribute this, therefore the
        model failed" is not an attribution, and the bucket holds 404s and 5xx from proxies
        nobody has met yet.

        A member added without a decision here is a SKIP, deliberately: the cost of failing to
        blame a model is a leg somebody re-runs, and the cost of blaming one wrongly is a model
        replaced over a proxy bug. ``tests/unit/test_evals_live.py`` pins the whole mapping, so
        the decision is made in a review rather than by omission.
        """
        return self in _ABOUT_THE_MODEL

    def as_outcome(self) -> Outcome:
        """Map onto the harness's three-valued outcome.

        Derived from :attr:`about_the_model`, not from a list of exceptions to a blanket FAIL.
        The repo's cardinal rule in one method: a leg nobody could run, or nobody could attribute,
        must never read as a leg that ran badly -- and it must never read as one that passed
        either. A SKIP is not free, though: it is only honest if whatever renders it says out loud
        that the leg was asked and did not answer, which is why ``harness/sweep.py`` reports those
        separately from the legs that were never asked at all.
        """
        if self is Failure.NONE:
            return Outcome.PASS
        return Outcome.FAIL if self.about_the_model else Outcome.SKIP


_ABOUT_THE_MODEL = frozenset(
    {
        Failure.SCHEMA_REFUSED,
        Failure.TEMPERATURE_REFUSED,
        Failure.EMPTY_RESPONSE,
        Failure.REPAIRS_EXHAUSTED,
    }
)
"""The failures :attr:`Failure.about_the_model` answers yes to. See that property for why.

A set of the four rather than a set of the rest, so a member added later is a SKIP until somebody
decides otherwise -- the safe direction, and the direction the test suite makes visible.
"""


# The *openings* of the messages `kedge.plan.propose` raises. Matched rather than typed, because
# all three are the same exception class and two of them have no `__cause__` at all: an empty
# response and an exhausted repair budget are both a bare `ProposalError` with nothing underneath.
#
# Anchored to the start of the message, and tested one exception at a time, because an exhausted
# repair budget quotes the model back at itself -- up to 200 characters of it per attempt, from
# `parse_draft`. A model that replies "Sorry, the upstream tool returned an empty response so I
# cannot plan" was matching the empty-response phrase from inside kedge's own exhausted-repairs
# message, and the one failure this module calls genuinely about the model's competence was
# reported as an endpoint that said nothing. A prefix cannot be talked into by the model, because
# the model does not write the first characters of kedge's exception.
#
# The coupling to these strings is real, and it is held by tests that provoke the actual code
# paths rather than hand-writing the exception -- so a reworded message fails a test here instead
# of quietly collapsing into UNKNOWN.
_EMPTY = "the model endpoint returned an empty response"
_EXHAUSTED = "the model did not produce a valid process plan"
_NO_FALLBACK = "the model endpoint rejected the request with no fallback left"

_MAX_CHAIN = 8
"""How far down ``__cause__`` to look. Bounded so a self-referential chain cannot hang a sweep."""


def _chain(exc: BaseException) -> list[BaseException]:
    """The exception and its explicit causes, outermost first, bounded and cycle-safe.

    ``__cause__`` only, never ``__context__``. Everything ``kedge.plan.propose`` raises uses
    ``raise ... from``, so the deliberate chain is complete -- while the implicit one threads
    through every 400 the negotiation already recovered from, which would make a call that
    survived a temperature refusal and then died on the structured-output ladder report the
    temperature as its cause. The last refusal is the one that ended the call, and it is the one
    to believe.
    """
    seen: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and len(seen) < _MAX_CHAIN:
        if any(current is item for item in seen):
            break
        seen.append(current)
        current = current.__cause__
    return seen


def classify_failure(exc: BaseException | None) -> Failure:
    """Attribute a failed proposal to one cause.

    Reads the whole ``__cause__`` chain, because everything the planning loop raises is a
    :class:`~kedge.plan.propose.ProposalError` wrapping the thing that actually went wrong --
    the SDK exception is one link down, and classifying on the outer type alone would put every
    distinct failure in this module into a single bucket.

    Ordering matters and is deliberate. Configuration is checked first: a missing keyring entry
    is not a model result at all and must never be attributed to the model. Transport and status
    codes come next, since those are unambiguous types. Only then are the messages consulted, and
    only for the failures that share one exception class with nothing else to tell them apart --
    outermost cause first, because the exception that ended the call is the one to believe.

    Args:
        exc: The exception :func:`~kedge.plan.propose.propose_plan` raised, or ``None`` for a
            call that succeeded.

    Returns:
        The single :class:`Failure` that best attributes it. :attr:`Failure.NONE` for ``None``.
    """
    if exc is None:
        return Failure.NONE

    from kedge.errors import ConfigError
    from kedge.plan.propose import ProposalRefusedError

    chain = _chain(exc)

    # A workbook kedge declined to plan, and a machine with no key, are both outcomes about the
    # sweep rather than about the model. They are looked for first so nothing below can claim
    # them.
    if any(isinstance(item, ProposalRefusedError) for item in chain):
        return Failure.TRIAGE_REFUSED
    if any(isinstance(item, ConfigError) for item in chain):
        return Failure.NOT_CONFIGURED

    for item in chain:
        typed = _by_type(item)
        if typed is not None:
            return typed

    for item in chain:
        named = _by_message(item, chain)
        if named is not None:
            return named

    text = "; ".join(str(item) for item in chain)
    logger.debug("unclassified proposal failure: %s", text)
    return Failure.UNKNOWN


def _by_message(exc: BaseException, chain: Iterable[BaseException]) -> Failure | None:
    """Classify one exception on the opening of its own message.

    Exhausted repairs is tested before an empty response for the reason recorded above the
    anchors: kedge's exhausted-repairs message quotes the model's output, and the model can write
    anything at all in it -- including the empty-response wording, which is how it used to talk
    its way out of the one attribution that counts against it.

    Be clear about which half of that fix is load-bearing. The **anchoring** is: two different
    prefixes cannot both match one message, so no ordering of prefix tests can misattribute
    anything. The **ordering** is belt and braces, kept for whoever loosens ``startswith`` to
    ``in`` some day in a hurry, and it costs nothing. A mutation that only swaps these two lines
    therefore breaks no test, and should not: the test that matters
    (``test_a_model_talking_about_empty_responses_cannot_shed_the_exhausted_repairs``) fails the
    moment the match stops being anchored.
    """
    text = str(exc).strip()
    if text.startswith(_EXHAUSTED):
        return Failure.REPAIRS_EXHAUSTED
    if text.startswith(_EMPTY):
        return Failure.EMPTY_RESPONSE
    if text.startswith(_NO_FALLBACK):
        return _ladder_ran_out(chain)
    return None


def _ladder_ran_out(chain: Iterable[BaseException]) -> Failure:
    """Say which negotiation the endpoint refused, once every fallback has been spent.

    A proxy that will not take a ``response_format`` is a different report from a reasoning model
    that will not take a ``temperature``, and the 400 underneath is what says which.

    ``_rejects_temperature`` is applied to the status errors alone, never to the wrapper. Its
    fallback branch is a substring test for "temperature", and the wrapper's own message says
    "(no response format, no temperature)" whenever the temperature was already dropped -- so
    handing it the ``ProposalError`` makes every exhausted ladder look like a temperature refusal,
    including the ones where the temperature negotiation had already succeeded.
    """
    from openai import APIStatusError

    # `_rejects_temperature` is private and imported anyway. It is the exact predicate
    # `OpenAICompleter` negotiates on, and a second copy of it here would drift from the one that
    # matters -- which is precisely the bug this classifier exists to make visible.
    from kedge.plan.propose import _rejects_temperature

    if any(_rejects_temperature(item) for item in chain if isinstance(item, APIStatusError)):
        return Failure.TEMPERATURE_REFUSED
    return Failure.SCHEMA_REFUSED


def _by_type(item: BaseException) -> Failure | None:
    """Classify one exception on its type, which is unambiguous where it applies.

    ``APITimeoutError`` subclasses ``APIConnectionError``, so it is tested first; reversing those
    two lines would report every timeout as a transport error and send somebody looking at their
    proxy instead of at their ``timeout_seconds``. The same ordering applies to the ``httpx``
    exceptions below, where ``TimeoutException`` subclasses ``TransportError``.

    ``httpx`` is tested at all because the SDK does not translate everything. It maps transport
    errors around the *request*, and a ``ReadTimeout`` escaping it untranslated is recorded on
    this project (CLAUDE.md) -- classified here as the timeout it is, rather than as UNKNOWN.

    ``BadRequestError`` is deliberately absent: a 400 is how both negotiations begin, and the
    exhausted ladder is told apart from an ordinary refusal by :func:`_by_message` reading the
    wrapper kedge raised around it. Classifying 400s here would collapse the schema/temperature
    distinction this module was written for.
    """
    import httpx
    from openai import (
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        AuthenticationError,
        ConflictError,
        InternalServerError,
        NotFoundError,
        PermissionDeniedError,
        RateLimitError,
        UnprocessableEntityError,
    )

    if isinstance(item, APITimeoutError | TimeoutError | httpx.TimeoutException):
        return Failure.TIMEOUT
    if isinstance(item, RateLimitError):
        return Failure.RATE_LIMITED
    if isinstance(item, AuthenticationError | PermissionDeniedError):
        return Failure.AUTHENTICATION
    if isinstance(item, NotFoundError):
        return Failure.MODEL_NOT_FOUND
    if isinstance(item, InternalServerError):
        return Failure.ENDPOINT_ERROR
    if isinstance(item, UnprocessableEntityError | ConflictError):
        return _rejected_outright(item)
    if isinstance(item, APIConnectionError | httpx.TransportError):
        return Failure.TRANSPORT
    if isinstance(item, APIStatusError):
        return _by_status(item)
    return None


def _rejected_outright(item: BaseException) -> Failure:
    """A 422 or a 409: a rejection kedge was never given the chance to negotiate.

    ``OpenAICompleter`` degrades its structured-output mode on ``BadRequestError`` and on nothing
    else, so a proxy that answers an unwelcome ``temperature`` with a 422 rather than a 400 gets
    no fallback and no retry -- the call simply dies. Where the body names the parameter that is
    still a temperature refusal and is reported as one; where it does not, the honest answer is
    that the endpoint rejected the request, not a guess about which part of it.
    """
    from kedge.plan.propose import _rejects_temperature

    if isinstance(item, Exception) and _rejects_temperature(item):
        return Failure.TEMPERATURE_REFUSED
    return Failure.REQUEST_REJECTED


def _by_status(item: Any) -> Failure | None:
    """The last resort for a status error the SDK did not give a class of its own.

    Types are believed before codes: the SDK's mapping from status to exception class is the
    authority, and reading the code first would misclassify anything a caller constructed by hand.
    This exists for the gaps -- a future 4xx, an SDK version with a class this one does not import
    -- and it returns ``None`` for a 400 on purpose, so the structured-output ladder stays the
    business of :func:`_by_message`.
    """
    status = getattr(item, "status_code", None)
    if not isinstance(status, int) or status == 400:
        return None
    if status in (401, 403):
        return Failure.AUTHENTICATION
    if status == 404:
        return Failure.MODEL_NOT_FOUND
    if status == 408:
        return Failure.TIMEOUT
    if status == 429:
        return Failure.RATE_LIMITED
    if status >= 500:
        return Failure.ENDPOINT_ERROR
    if status >= 400:
        return Failure.REQUEST_REJECTED
    return None


# =============================================================================
# THE METERED COMPLETER
# =============================================================================


@dataclass(frozen=True, slots=True)
class MeteredCall:
    """One logical ``complete()``: what it cost, how long it took, and how it was negotiated.

    :attr:`mode` and :attr:`omit_temperature` are read off the delegate *after* the call, so they
    describe what actually worked rather than what was attempted. ``mode`` is ``None`` for a
    delegate that does not negotiate structured output at all -- a scripted completer has no
    opinion about ``response_format``, and recording ``"json_schema"`` for it would be a fact
    about nothing.

    Example:
        >>> MeteredCall(usage=MeteredUsage(), seconds=1.5).ok
        True
    """

    usage: MeteredUsage = field(default_factory=MeteredUsage)
    seconds: float = 0.0
    mode: str | None = None
    """The structured-output mode in force when the call ended, or ``None`` if not applicable."""
    omit_temperature: bool = False
    """Whether the endpoint had refused an explicit temperature and it was no longer sent."""
    failure: Failure = Failure.NONE
    detail: str = ""
    """The exception text, kept verbatim. A classification with no evidence is unactionable."""

    @property
    def ok(self) -> bool:
        """Whether the call produced an answer. Says nothing about whether it was a good one."""
        return self.failure is Failure.NONE

    @property
    def degraded(self) -> bool:
        """Whether structured output ended below strict JSON schema."""
        return self.mode is not None and self.mode != "json_schema"

    @property
    def negotiated(self) -> bool:
        """Whether anything about the request had to be given up to get an answer.

        This, with :attr:`ok`, is what says "succeeded, but only after dropping the temperature".
        Reporting that as a failure is the mistake ``tests/llm/README.md`` records; not reporting
        it at all is how the same mistake goes unnoticed the second time.
        """
        return self.omit_temperature or self.degraded


@dataclass(slots=True)
class MeteredCompleter:
    """A :class:`~kedge.plan.propose.Completer` that records what every call cost.

    Delegation rather than a subclass, so the same wrapper measures a real
    :class:`~kedge.plan.propose.OpenAICompleter` and a
    :class:`~kedge.plan.propose.ScriptedCompleter` -- which is what lets the whole of an eval's
    metering logic be tested with no endpoint, no key and no network. The scripted case is not
    only a test convenience: it is the "endpoint reported nothing" path, which is also what a
    llama.cpp server or a thin proxy produces in production, so exercising it offline exercises a
    real branch rather than a mock of one.

    Mutable by necessity -- it accumulates -- and therefore the one non-frozen dataclass here.
    ``clock`` is injectable so a test can assert on elapsed seconds without sleeping.

    **Not thread-safe, and not fixable from here.** The delegate's ``usage`` is reset at the top of
    every ``complete()`` and read once it returns, so two calls in flight through one of these read
    each other's arithmetic: two 100-token calls have been measured as 300 tokens over 3 requests.
    A lock would serialise the model calls themselves, which is the entire elapsed time of a sweep
    and the only thing worth parallelising, so the constraint is documented rather than defended.
    Give each thread its own :class:`ResolvedModel` and its own meter; see the module docstring.

    Example:
        >>> from kedge.plan.propose import CompletionRequest, ScriptedCompleter
        >>> metered = MeteredCompleter(ScriptedCompleter(["{}"]))
        >>> metered.complete(CompletionRequest(messages=[], model="m"))
        '{}'
        >>> metered.usage.completions, metered.usage.any_reported
        (1, False)
    """

    delegate: Completer
    calls: list[MeteredCall] = field(default_factory=list)
    clock: Callable[[], float] = time.perf_counter

    def complete(self, request: CompletionRequest) -> str:
        """Delegate the call, recording its cost, its duration and its outcome either way.

        A raising call is recorded and then re-raised untouched. Recording it matters twice over:
        the requests it burned were still billed, and the classification of *why* it raised is
        the thing a cross-model table is for. Swallowing the exception would be worse -- the
        caller decides whether a failed proposal ends the leg.
        """
        started = self.clock()
        try:
            text = self.delegate.complete(request)
        except Exception as exc:
            self.calls.append(self._record(started, exc))
            raise
        self.calls.append(self._record(started, None))
        return text

    def _record(self, started: float, exc: BaseException | None) -> MeteredCall:
        return MeteredCall(
            usage=_usage_of(self.delegate),
            seconds=max(0.0, self.clock() - started),
            mode=getattr(self.delegate, "mode", None),
            omit_temperature=bool(getattr(self.delegate, "omit_temperature", False)),
            failure=classify_failure(exc),
            detail="" if exc is None else str(exc),
        )

    @property
    def usage(self) -> MeteredUsage:
        """Every call's usage, summed."""
        return sum((call.usage for call in self.calls), MeteredUsage())

    @property
    def seconds(self) -> float:
        """Wall-clock seconds spent inside the delegate, summed across calls."""
        return sum((call.seconds for call in self.calls), 0.0)

    @property
    def last(self) -> MeteredCall | None:
        """The most recent call, or ``None`` if nothing has been asked of it yet."""
        return self.calls[-1] if self.calls else None

    def summarise(self, exc: BaseException | None = None) -> MeteredCall:
        """Fold the whole sequence of calls into one record for a table row.

        A proposal is one completion plus one per repair round, and a row in a per-model table
        wants the total. ``exc`` is the exception the *caller* saw -- from
        :func:`~kedge.plan.propose.propose_plan`, not from ``complete()`` -- because the failure
        that matters is often raised after the last successful completion: an exhausted repair
        budget is exactly that shape, every call returned a string and none of them was a plan.

        Args:
            exc: What the surrounding operation raised, or ``None`` if it succeeded.

        Returns:
            One :class:`MeteredCall` over the whole sequence. Negotiation state comes from the
            last call, since it is cumulative on the delegate; seconds and usage are summed.
        """
        last = self.last
        failure = classify_failure(exc)
        if failure is Failure.NONE and last is not None and last.failure:
            # `complete()` raised and the caller handled it. The negotiation still failed, and a
            # row that says otherwise is a row that reports a lie about a live endpoint.
            failure = last.failure
        return MeteredCall(
            usage=self.usage,
            seconds=self.seconds,
            mode=last.mode if last is not None else None,
            omit_temperature=last.omit_temperature if last is not None else False,
            failure=failure,
            detail=str(exc) if exc is not None else (last.detail if last is not None else ""),
        )


# =============================================================================
# RESOLVING A MODEL FOR A SWEEP LEG
# =============================================================================


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """One leg of a sweep: which model, optionally where, optionally under which key.

    Everything but :attr:`model` is optional and falls back to the resolved kedge config, so the
    common case -- several models behind one endpoint and one key -- is a list of bare model ids.
    :attr:`api_key_ref` names a *keyring entry*, never a key: kedge reads keys from the OS keyring
    and from nowhere else, and a sweep across providers is exactly the situation where somebody
    would otherwise be tempted to put one in a file.

    Example:
        >>> ModelSpec(model="gpt-4o").name
        'gpt-4o'
        >>> ModelSpec(model="gpt-4o", label="baseline").name
        'baseline'
    """

    model: str
    base_url: str | None = None
    api_key_ref: str | None = None
    label: str | None = None
    """What the table calls this leg. Defaults to the model id; useful when one id is run twice
    against different endpoints, where two rows called ``gpt-4o`` would be unreadable."""
    timeout_seconds: float | None = None
    max_retries: int | None = None

    @property
    def name(self) -> str:
        """The label if there is one, otherwise the model id."""
        return self.label or self.model


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    """A leg that is either ready to run or explains, in the report's own vocabulary, why not.

    The whole reason this returns a value instead of raising: a sweep across six models where two
    have no keyring entry must still print a table with six rows. Two of them say
    ``not_configured`` and carry the exact command that would fix it, and four of them carry
    results. A traceback at leg two throws away the four.

    Example:
        >>> ResolvedModel(spec=ModelSpec(model="m"), failure=Failure.NOT_CONFIGURED).ready
        False
    """

    spec: ModelSpec
    completer: OpenAICompleter | None = None
    failure: Failure = Failure.NONE
    detail: str = ""

    @property
    def ready(self) -> bool:
        """Whether there is a completer to call."""
        return self.completer is not None

    @property
    def outcome(self) -> Outcome:
        """SKIP for an unconfigured leg, FAIL for a broken one, PASS for a usable one."""
        return self.failure.as_outcome()

    def metered(self, *, renegotiate: bool = False) -> MeteredCompleter | None:
        """Wrap the completer for measurement, or ``None`` if this leg cannot run.

        **Every call wraps the same completer, and that completer keeps its negotiation.**
        :attr:`~kedge.plan.propose.OpenAICompleter.mode` and ``omit_temperature`` persist by
        design -- a real session pays for the structured-output ladder once, not once a proposal
        -- so a second repeat of a leg reports ``degraded`` and ``negotiated`` with no negotiation
        cost against them. Those flags are true statements about the session and misleading
        statements about the repeat, and the repeat's ``wasted_requests`` is what tells them
        apart: the first repeat carries the ladder, the rest carry none. The reasoning used to
        live only in :func:`harness.sweep.run_leg`, which is the consumer rather than the API, so
        the next consumer would have had to rediscover it.

        Args:
            renegotiate: Reset the delegate's negotiated state first, so this repeat pays for the
                ladder again and measures what a *cold* session costs. Off by default because the
                default is what a user actually pays; on, a sweep measures the negotiation ``n``
                times and every repeat's flags describe only itself.

        Returns:
            A meter over this leg's completer, or ``None`` when there is nothing to meter.
        """
        if self.completer is None:
            return None
        if renegotiate:
            # Reset rather than rebuilt: the completer owns an SDK client, a TLS context and the
            # API key, and none of the three is reachable from here without the config that built
            # them. These two attributes are the whole of what a fresh completer would differ by,
            # and resetting them does not put the key into a second place in the process.
            #
            # Guarded because a leg's completer is whatever satisfies the protocol -- the scripted
            # stand-ins the tests resolve to have no opinion about `response_format` and no slot
            # to hold one, so assigning blind would raise on the fake and never on the real thing.
            for name, value in (("mode", "json_schema"), ("omit_temperature", False)):
                if hasattr(self.completer, name):
                    setattr(self.completer, name, value)
        return MeteredCompleter(self.completer)


def resolve_model(
    spec: ModelSpec, *, kedge_home: Path | None = None, project_dir: Path | None = None
) -> ResolvedModel:
    """Build a completer for one sweep leg, or classify why there cannot be one.

    Reads the ordinary kedge configuration and overlays the spec on top of it, so a leg inherits
    the user's endpoint, CA bundle and timeouts and overrides only what it names. ``kedge_home``
    points at a different ``~/.kedge`` without touching ``KEDGE_HOME`` in the environment --
    process-wide mutable state is the one thing a sweep running several legs must not introduce,
    and a leg that changed an environment variable would be read by every other leg.

    **Pinning ``kedge_home`` pins both layers.** kedge's config is ``~/.kedge/config.toml``
    overridden by ``./kedge.toml``, and the project layer resolves from the current working
    directory -- so a sweep pointed at one kedge home, run from inside somebody's kedge project,
    quietly took that project's ``base_url`` and ``api_key_ref`` for every leg. Running a sweep
    from inside a project is the ordinary case, not a corner one, and the symptom is six legs
    silently measuring one endpoint. When ``kedge_home`` is given the project layer is pinned
    alongside it, so the caller gets exactly the configuration it named; when it is not, the
    ordinary layering applies, because a caller that pinned nothing is asking for the endpoint
    they are standing in.

    Nothing here raises. Every foreseeable failure comes back as a :class:`ResolvedModel` -- no
    config file, a config file that will not parse, no keyring entry, no keyring backend at all --
    and anything unforeseen is classified too, as :attr:`Failure.UNKNOWN` carrying its text. A
    sweep is a batch job, and the alternative to one bad row is no table.

    Args:
        spec: Which model to resolve, and what to override on the way.
        kedge_home: A kedge directory to read ``config.toml`` from instead of the default.
            Honours ``KEDGE_HOME`` when omitted, exactly as the rest of kedge does.
        project_dir: The directory whose ``kedge.toml`` forms the project layer. Defaults to
            ``kedge_home`` when that is given -- which is to say, to no project layer at all
            unless the sweep's own directory holds one -- and to the working directory otherwise.

    Returns:
        A resolved leg. Check :attr:`ResolvedModel.ready` before calling it.
    """
    from pydantic import ValidationError

    from kedge.config import (
        PROJECT_CONFIG_FILENAME,
        USER_CONFIG_FILENAME,
        ModelConfig,
        get_api_key,
        load_config,
    )
    from kedge.errors import ConfigError
    from kedge.plan.propose import OpenAICompleter

    user_path = None if kedge_home is None else Path(kedge_home) / USER_CONFIG_FILENAME
    project_root = project_dir if project_dir is not None else kedge_home
    project_path = None if project_root is None else Path(project_root) / PROJECT_CONFIG_FILENAME
    overrides: dict[str, Any] = {"model": spec.model}
    if spec.base_url is not None:
        overrides["base_url"] = spec.base_url
    if spec.api_key_ref is not None:
        overrides["api_key_ref"] = spec.api_key_ref
    if spec.timeout_seconds is not None:
        overrides["timeout_seconds"] = spec.timeout_seconds
    if spec.max_retries is not None:
        overrides["max_retries"] = spec.max_retries

    try:
        loaded = load_config(user_path=user_path, project_path=project_path)
        # Re-validated rather than `model_copy`d, so a mistyped base_url on the command line is
        # refused here, with the name of the offending key, instead of surfacing forty seconds
        # later as an SDK error about a URL nobody typed.
        model_config = ModelConfig.model_validate({**loaded.config.model.model_dump(), **overrides})
        # The key never lands on the config object and is never logged: it is fetched here,
        # handed straight to the SDK client, and forgotten.
        api_key = get_api_key(loaded.config.model_copy(update={"model": model_config}))
        completer = OpenAICompleter(
            base_url=model_config.base_url,
            api_key=api_key,
            model=model_config.model,
            timeout=model_config.timeout_seconds,
            max_retries=model_config.max_retries,
            ca_bundle=model_config.ca_bundle,
        )
    except (ConfigError, ValidationError) as exc:
        logger.warning("model %s is not runnable: %s", spec.name, exc)
        return ResolvedModel(spec=spec, failure=Failure.NOT_CONFIGURED, detail=str(exc))
    except Exception as exc:
        logger.warning("model %s could not be built: %s", spec.name, exc)
        return ResolvedModel(spec=spec, failure=classify_failure(exc), detail=str(exc))

    logger.info("model %s resolved against %s", spec.name, model_config.base_url)
    return ResolvedModel(spec=spec, completer=completer)
