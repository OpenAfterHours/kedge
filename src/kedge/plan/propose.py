"""Analysis in, :class:`~kedge.plan.model.ProcessPlan` out. The only part of M2 that needs an LLM.

One call over the structural facts. **No code generation happens here** — the model decides the
decomposition and nothing else, because the point of the plan is that the decomposition gets
reviewed before any code exists (PLAN 2.2, M2 step 2).

Three things in this module carry more weight than their line count suggests:

- **The endpoint is user-supplied and OpenAI-*compatible*, which is not the same as OpenAI.**
  Structured output cannot be assumed. :class:`OpenAICompleter` tries strict JSON-schema mode,
  falls back to JSON-object mode, then to plain text, and remembers which worked so the fallback
  is paid for once per session rather than once per attempt.
- **Validation failures are repair instructions, not errors.** A response that will not load
  goes back to the model with the pydantic messages attached, capped at
  :data:`DEFAULT_MAX_ATTEMPTS` attempts. ``extra="forbid"`` on the plan models means a
  hallucinated field produces a precise correction rather than being silently dropped.
- **A module that can only be tested against a live model will not get tested.** The LLM sits
  behind the :class:`Completer` protocol; :class:`ScriptedCompleter` replays canned responses
  from memory or from disk, so the whole path — context building, prompt assembly, parsing,
  retry, validation, assembly — runs offline in unit tests and in the corpus sweep.

Prompts live in ``prompts/*.md`` and are loaded from disk rather than embedded as string
literals, because they will be iterated on heavily and a prompt buried in a triple-quoted string
is a prompt nobody edits.

References:
- PLAN.md 2.2 (the plan artifact), 2.3 (profiles, not values), 2.6, 2.7 (the vocabulary), M2.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import ValidationError

from kedge.analysis.model import ColumnProfile, LogicalOperation, WorkbookAnalysis
from kedge.plan.model import (
    Assessment,
    DroppedRange,
    OpenQuestion,
    PlanDraft,
    PlanError,
    ProcessPlan,
)
from kedge.plan.review import repairable_warnings
from kedge.plan.triage import TriageResult, triage

if TYPE_CHECKING:
    from collections.abc import Iterable

    from kedge.config import Config

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "Completer",
    "CompletionRequest",
    "CompletionUsage",
    "OpenAICompleter",
    "ProposalError",
    "ProposalRefusedError",
    "ScriptedCompleter",
    "build_messages",
    "build_proposal_context",
    "completer_from_config",
    "describe_errors",
    "load_prompt",
    "parse_draft",
    "plan_json_schema",
    "propose_plan",
    "record_responses",
    "scripted_from_plan",
]

DEFAULT_MAX_ATTEMPTS = 3
"""How many times a malformed response is sent back with its validation errors."""

PROMPTS_DIR = Path(__file__).parent / "prompts"

_MAX_OPERATIONS = 80
_MAX_PROFILES = 100
_MAX_FINDINGS = 40
_MAX_NOTE_CHARS = 1_500
_MAX_M_CHARS = 2_000
_MAX_SQL_CHARS = 1_000
_MAX_TOP_K = 5

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


class ProposalError(PlanError):
    """The model endpoint could not be reached, or would not produce a usable plan."""


class ProposalRefusedError(PlanError):
    """Triage said stop, and the caller did not force it.

    A distinct exception rather than a return value, because "kedge declined to plan this
    workbook" is a different outcome from "here is a poor plan" and the two must not be confused
    at the call site.
    """

    def __init__(self, result: TriageResult) -> None:
        super().__init__(result.explain())
        self.result = result


# =============================================================================
# THE LLM SEAM
# =============================================================================


@dataclass(frozen=True, slots=True)
class CompletionRequest:
    """One chat completion, described independently of any SDK.

    Keeping the request as plain data is what lets :class:`ScriptedCompleter` stand in for a real
    endpoint without any mocking of the OpenAI client.
    """

    messages: list[dict[str, str]]
    model: str
    json_schema: dict[str, Any] | None = None
    schema_name: str = "process_plan"
    temperature: float = 0.2


class Completer(Protocol):
    """Anything that can turn a :class:`CompletionRequest` into response text.

    The whole LLM dependency of the planning loop is this one method.
    """

    def complete(self, request: CompletionRequest) -> str:
        """Return the assistant's message content for this request."""
        ...


def _rejects_temperature(exc: Exception) -> bool:
    """Whether a 400 is about ``temperature`` rather than about structured output.

    The reasoning models answer an explicit temperature with *"Unsupported value: 'temperature'
    does not support 0.2 with this model"* and name the parameter in ``error.param``. That field
    is checked first because it is unambiguous; the message is a fallback for the proxies and
    local servers that return a bare string.
    """
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("param") == "temperature":
            return True
    return "temperature" in str(exc).lower()


@dataclass(frozen=True, slots=True)
class CompletionUsage:
    """What one logical :meth:`Completer.complete` call cost, as the endpoint counted it.

    One ``complete()`` is **not** one HTTP request. The temperature negotiation and the
    ``json_schema -> json_object -> text`` ladder both retry inside the same call, so a single
    logical completion can be three round trips against three different payloads -- and a model
    that quietly burned two of them degrading a response format is a materially different result
    from one that answered first time. Both figures are therefore kept: :attr:`requests` counts
    what went over the wire, and the caller counts the completions.

    ``reported`` is the field that keeps an honest sweep honest. Plenty of OpenAI-compatible
    servers -- llama.cpp, a thin proxy, an internal gateway -- answer perfectly and volunteer no
    ``usage`` block at all, and a zero token count from one of those is *not* a cheap model. So
    "the endpoint reported nothing" (``reported == 0``) is expressed distinctly from "the endpoint
    reported zero", and a report that renders the two the same way will get a model credited with
    a cost nobody measured. It counts blocks that yielded a **number**, not blocks that merely
    existed: a proxy answering with ``usage`` as a bare ``dict``, or with ``prompt_tokens: null``,
    has told nobody anything, and counting it as a report is how the least informative endpoint in
    a sweep becomes the cheapest model in it.

    ``answered`` is the denominator ``reported`` belongs over, and it is not ``requests``. A 400
    that starts a negotiation is a request that *cannot* carry a usage block, so measuring
    completeness against the request count declares every recovered negotiation a flaky endpoint.

    Deliberately a plain frozen dataclass rather than a reuse of
    :class:`kedge.agent.loop.Usage`: ``plan/`` sits below ``agent/`` in the layering, and a
    planning module importing the chat loop to borrow five integers would invert it. The
    vocabulary is shared on purpose even though the type is not -- with one deliberate exception,
    :attr:`any_reported`, which is spelled differently from :attr:`kedge.agent.loop._Meter.measured`
    because it means something different: ``_Meter.measured`` is "every step reported", this is
    "at least one request did". Both were called ``measured`` once, and
    ``if usage.measured: print(usage.total)`` printed a partial total as though it were the bill.

    Example:
        >>> CompletionUsage(prompt=8_000, completion=120, requests=1, reported=1).total
        8120
        >>> CompletionUsage(requests=3).any_reported
        False
    """

    prompt: int = 0
    completion: int = 0
    cached: int = 0
    """Prompt tokens the endpoint served from cache. Part of ``prompt``, not additional to it."""
    requests: int = 0
    """HTTP requests issued, including the ones a negotiation threw away."""
    answered: int = 0
    """How many of those requests came back with a response at all, rather than raising."""
    reported: int = 0
    """How many answered requests carried usable numbers in the endpoint's own usage block."""

    @property
    def total(self) -> int:
        """Prompt plus completion, cache or no cache."""
        return self.prompt + self.completion

    @property
    def any_reported(self) -> bool:
        """Whether any of it is the endpoint's own arithmetic rather than an assumed zero."""
        return self.reported > 0

    @property
    def fully_reported(self) -> bool:
        """Whether every request that could have carried numbers did.

        Measured against :attr:`answered` rather than :attr:`requests`, because a request refused
        with a 400 never had a usage block to volunteer.
        """
        return self.reported > 0 and self.reported >= self.answered


def _read(source: Any, name: str) -> Any:
    """One field off a usage block, whichever shape the endpoint chose to send it in.

    The SDK hands back a pydantic object; a proxy that assembles its own JSON hands back a plain
    ``dict``, and ``getattr`` finds nothing at all on one of those -- which is how a complete
    usage block comes to read as "this endpoint reported nothing". Both shapes are read here, and
    anything that raises on access is treated as absent: metering is bookkeeping, and bookkeeping
    must never be the thing that turns a working plan into a traceback.
    """
    if source is None:
        return None
    try:
        if isinstance(source, Mapping):
            return source.get(name)
        return getattr(source, name, None)
    except Exception:
        logger.debug("a usage block raised while reading %s; treating it as absent", name)
        return None


def _count(source: Any, name: str) -> int | None:
    """One token count off a usage block, or ``None`` when the endpoint did not really give one.

    ``None`` and ``0`` are different answers and the difference is the whole honesty of the cost
    column: ``None`` is "nobody counted", ``0`` is "the endpoint counted zero". So a null, a
    missing key, a nested ``dict`` where a number belongs and a string that will not parse --
    ``prompt_tokens="8,000"``, which an ``int()`` would raise on -- all come back as ``None``
    rather than as a zero somebody will later average, or as a ``ValueError`` in the middle of a
    successful plan. ``bool`` is excluded on purpose: ``True`` is an ``int`` in Python and one
    prompt token is not what an endpoint sending it meant.
    """
    value = _read(source, name)
    try:
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            return None
        return int(value)
    except (TypeError, ValueError, OverflowError):
        logger.debug("usage field %s was not a number; treating it as unreported", name)
        return None


class OpenAICompleter:
    """A :class:`Completer` backed by the OpenAI SDK against the configured endpoint.

    Structured output is attempted in descending order of strictness and the working mode is
    remembered, because the endpoint is whatever the user configured: a hosted API, a local
    llama.cpp server, a proxy. Assuming ``response_format`` support and failing hard would make
    kedge unusable against half of them.

    ``temperature`` is negotiated the same way and for the same reason. The reasoning models
    accept only their default and reject any explicit value outright, so a rejection naming that
    parameter drops it for the rest of the session rather than being misread as one more piece of
    evidence that structured output is unsupported.

    Both negotiations leave a trace worth reading, and so does what they cost: :attr:`mode`,
    :attr:`omit_temperature` and :attr:`usage` are the three pieces of observable state a caller
    needs to tell "answered first time" from "answered on the third attempt after giving up on
    structured output", which is a real difference between two models that both returned a plan.

    Example:
        >>> completer = OpenAICompleter(base_url="https://api.example/v1", api_key="k", model="m")
        >>> completer.mode
        'json_schema'
        >>> completer.usage.any_reported
        False
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 120.0,
        max_retries: int = 2,
        ca_bundle: Path | None = None,
    ) -> None:
        from openai import OpenAI

        from kedge import tls

        # Verified against the OS trust store rather than certifi, so a TLS-inspecting proxy
        # does not turn the first plan into an unreadable SSL error (kedge.tls).
        self._client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            max_retries=max_retries,
            http_client=tls.client(ca_bundle=ca_bundle, timeout=timeout),
        )
        self._model = model
        self.mode = "json_schema"
        """Current structured-output mode: ``json_schema``, ``json_object`` or ``text``."""
        self.omit_temperature = False
        """Whether the endpoint refused an explicit ``temperature`` and it is no longer sent."""
        self.usage = CompletionUsage()
        """What the **most recent** :meth:`complete` cost, accumulated across its retries.

        Public for the same reason :attr:`mode` and :attr:`omit_temperature` are: it is observable
        state about the negotiation that just happened, and a caller that wants to know what a
        plan cost has nowhere else to read it. A decorator around :class:`Completer` can time a
        call and count calls, but the protocol returns a bare string, so tokens are structurally
        invisible from outside -- an eval sweep comparing models on cost would have to guess.

        Reset at the top of every call, so it describes one completion rather than a session, and
        written *as the requests happen* rather than at the end -- a call that raises still burned
        whatever it burned, and a failed leg of a sweep that reports zero tokens is a lie about
        the bill.

        That reset is also why **one completer must not be called from two threads at once**: the
        second call's reset lands between the first call's requests and the caller's read of them,
        so both callers read one call's arithmetic and the sweep bills a model for tokens nobody
        spent. A parallel sweep gives each worker its own completer.
        """

    def complete(self, request: CompletionRequest) -> str:
        """Send the request, degrading the structured-output mode on rejection."""
        from openai import BadRequestError, OpenAIError

        self.usage = CompletionUsage()
        while True:
            payload: dict[str, Any] = {
                "model": request.model or self._model,
                "messages": request.messages,
                "response_format": self._response_format(request),
            }
            if not self.omit_temperature:
                payload["temperature"] = request.temperature
            try:
                response = self._client.chat.completions.create(**payload)
            except BadRequestError as exc:
                self._meter(None)
                # Tested before the structured-output ladder, because it is the same exception
                # type: without this, an endpoint that only accepts its default temperature burns
                # three requests degrading a `response_format` it was perfectly happy with, and
                # then reports the wrong cause.
                if _rejects_temperature(exc) and not self.omit_temperature:
                    self.omit_temperature = True
                    logger.warning(
                        "endpoint rejected an explicit temperature; sending its default instead"
                    )
                    continue
                degraded = self._degrade()
                if degraded is None:
                    msg = (
                        f"the model endpoint rejected the request with no fallback left "
                        f"(no response format"
                        f"{', no temperature' if self.omit_temperature else ''}): {exc}"
                    )
                    raise ProposalError(msg) from exc
                logger.warning(
                    "endpoint rejected structured output mode; falling back to %s", degraded
                )
                continue
            except OpenAIError as exc:
                self._meter(None)
                msg = f"the model endpoint could not be reached or refused the request: {exc}"
                raise ProposalError(msg) from exc
            except Exception:
                # Not every failure arrives translated. The SDK maps transport errors around the
                # request, but a raw `httpx.ReadTimeout` -- or the bare `TimeoutError` httpx maps
                # one from -- has already escaped it on this project once (CLAUDE.md). Metered and
                # re-raised untouched: the request was still made and still billed, and a leg that
                # reports zero requests for it is a lie about the bill in the one direction that
                # flatters the endpoint.
                self._meter(None)
                raise

            self._meter(response)
            content = response.choices[0].message.content if response.choices else None
            if not content:
                msg = "the model endpoint returned an empty response"
                raise ProposalError(msg)
            return content

    def _meter(self, response: Any | None) -> None:
        """Fold one HTTP request into :attr:`usage`, whether or not it returned anything.

        Called on every outcome of ``chat.completions.create`` -- the answer, the 400 that starts
        a negotiation, the transport failure that ends the call -- because the request count has
        to include the ones that were thrown away. That is the whole point of keeping it apart
        from the completion count.

        Every field is optional and every read is defended (:func:`_count`), because the endpoint
        is whatever the user configured. A partial usage block is commonplace on
        OpenAI-compatible servers and is still worth more than nothing; a missing or malformed one
        must never turn a working plan into a traceback. ``reported`` advances only when a block
        yielded an actual number, so an endpoint that answers with an empty ``usage`` dict stays
        visibly unmeasured rather than being credited with zero tokens it never claimed.
        """
        current = self.usage
        if response is None:
            self.usage = replace(current, requests=current.requests + 1)
            return
        block = _read(response, "usage")
        prompt = _count(block, "prompt_tokens")
        completion = _count(block, "completion_tokens")
        cached = _count(_read(block, "prompt_tokens_details"), "cached_tokens")
        self.usage = CompletionUsage(
            prompt=current.prompt + (prompt or 0),
            completion=current.completion + (completion or 0),
            cached=current.cached + (cached or 0),
            requests=current.requests + 1,
            answered=current.answered + 1,
            # A block is a report when it carried a token count, not when it was present. The two
            # are different for exactly the servers this negotiation exists for.
            reported=current.reported + (1 if prompt is not None or completion is not None else 0),
        )

    def _response_format(self, request: CompletionRequest) -> dict[str, Any] | None:
        if self.mode == "json_schema" and request.json_schema is not None:
            return {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "strict": True,
                    "schema": request.json_schema,
                },
            }
        if self.mode in ("json_schema", "json_object"):
            return {"type": "json_object"}
        return None

    def _degrade(self) -> str | None:
        order = {"json_schema": "json_object", "json_object": "text"}
        nxt = order.get(self.mode)
        if nxt is None:
            return None
        self.mode = nxt
        return nxt


@dataclass(slots=True)
class ScriptedCompleter:
    """A :class:`Completer` that replays canned responses in order. No network.

    This is what makes ``propose`` testable offline, and it is deliberately in the library rather
    than in the test suite: the corpus sweep uses it to exercise the whole path against recorded
    responses, and a fake that only exists in ``tests/`` cannot be reused that way.

    Example:
        >>> completer = ScriptedCompleter(['{"assessment": {"convertible": 1.0}}'])
        >>> completer.complete(CompletionRequest(messages=[], model="m"))
        '{"assessment": {"convertible": 1.0}}'
    """

    responses: list[str]
    requests: list[CompletionRequest] = field(default_factory=list)
    _index: int = 0

    @classmethod
    def from_paths(cls, *paths: Path) -> ScriptedCompleter:
        """Replay responses recorded on disk, one file per attempt."""
        return cls([path.read_text(encoding="utf-8") for path in paths])

    def complete(self, request: CompletionRequest) -> str:
        """Return the next scripted response, recording the request for inspection."""
        self.requests.append(request)
        if self._index >= len(self.responses):
            msg = (
                f"scripted completer exhausted after {len(self.responses)} response(s); the "
                f"retry loop asked for another"
            )
            raise ProposalError(msg)
        response = self.responses[self._index]
        self._index += 1
        return response


def completer_from_config(config: Config) -> OpenAICompleter:
    """Build a completer from the resolved config and the API key in the OS keyring."""
    from kedge.config import get_api_key

    return OpenAICompleter(
        base_url=config.model.base_url,
        api_key=get_api_key(config),
        model=config.model.model,
        timeout=config.model.timeout_seconds,
        max_retries=config.model.max_retries,
        ca_bundle=config.model.ca_bundle,
    )


# =============================================================================
# PROMPTS
# =============================================================================


@lru_cache(maxsize=8)
def load_prompt(name: str) -> str:
    """Read a prompt file from ``kedge/plan/prompts``.

    Cached, because the retry loop reads the same files repeatedly and they never change within
    a run.

    Raises:
        ProposalError: naming the file, when a prompt is missing from the installation.
    """
    path = PROMPTS_DIR / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"could not read the prompt file {path}: {exc}"
        raise ProposalError(msg) from exc


def _fill(template: str, values: dict[str, str]) -> str:
    """Substitute ``{{placeholder}}`` markers.

    Deliberately not ``str.format``: the templates contain JSON, and every brace in it would
    have to be doubled.
    """
    filled = template
    for key, value in values.items():
        filled = filled.replace("{{" + key + "}}", value)
    return filled


# =============================================================================
# CONTEXT
# =============================================================================


def build_proposal_context(
    analysis: WorkbookAnalysis,
    triage_result: TriageResult,
    *,
    max_operations: int = _MAX_OPERATIONS,
    max_profiles: int = _MAX_PROFILES,
    include_sample_values: bool = False,
) -> dict[str, Any]:
    """Assemble the compact digest of the analysis that the model reasons over.

    Summary plus column profiles plus the logical operations with their R1C1, pattern,
    description and ranges — which is exactly what ``WorkbookAnalysis.summary()`` was designed
    for. Raw cell values are excluded: a 200,000-row sheet does not fit and would not help if it
    did, and the agent's capped tools exist for the cases where a real value is needed
    (PLAN 2.3).

    Everything that is truncated says so explicitly, so the model never reasons as though it saw
    the whole workbook.

    Args:
        analysis: The complete workbook analysis.
        triage_result: The deterministic assessment, included as evidence.
        max_operations: Cap on logical operations, highest fan-out first.
        max_profiles: Cap on column profiles.
        include_sample_values: Include the bounded head/tail/sample rows from each profile. Off
            by default; turn it on only for a workbook whose parameter tables matter more than
            the context budget.

    Returns:
        A JSON-serialisable mapping.
    """
    context: dict[str, Any] = {
        "summary": analysis.summary(),
        "operations": _operation_digest(analysis.operations, max_operations),
        "column_profiles": _profile_digest(
            analysis.profiles, max_profiles, include_sample_values=include_sample_values
        ),
        "findings": _finding_digest(analysis),
        "named_ranges": [
            {"name": item.name, "refers_to": item.refers_to, "scope": item.scope}
            for item in analysis.named_ranges[:40]
        ],
        "connections": [
            {
                "name": item.name,
                "kind": item.kind,
                "sql": _clip(item.command, _MAX_SQL_CHARS),
                "provider": item.provider,
            }
            for item in analysis.connections
        ],
        "power_query": {
            "status": analysis.power_query.status.value,
            "queries": [
                {"name": query.name, "m": _clip(query.m_source, _MAX_M_CHARS)}
                for query in analysis.power_query.queries
            ],
        },
        # `location` is not decoration. The plan's `briefing` refuses to carry prose about why
        # a process exists without citing where that came from, and a model cannot cite a cell
        # reference it was never shown -- it would have to invent one, which is precisely what
        # the citation requirement exists to prevent.
        "process_notes": [
            {
                "source": note.source,
                "origin": note.origin,
                "location": note.location,
                "heading": note.heading,
                "text": _clip(note.text, _MAX_NOTE_CHARS),
            }
            for note in analysis.notes[:20]
        ],
        "dependency_graph": {
            "edges": analysis.graph.edges[:200],
            "cycles": analysis.graph.cycles[:10],
            "roots": analysis.graph.roots[:40],
            "leaves": analysis.graph.leaves[:40],
        },
        "triage": {
            "verdict": triage_result.verdict.value,
            "convertible": triage_result.convertible,
            "complexity": triage_result.complexity,
            "reconcilable": triage_result.reconcilable,
            "conversion_blockers": [b.render() for b in triage_result.conversion_blockers],
            "verification_blockers": [b.render() for b in triage_result.verification_blockers],
        },
    }
    if len(analysis.operations) > max_operations:
        context["operations_omitted"] = len(analysis.operations) - max_operations
    if len(analysis.profiles) > max_profiles:
        context["column_profiles_omitted"] = len(analysis.profiles) - max_profiles
    return context


def _operation_digest(operations: list[LogicalOperation], limit: int) -> list[dict[str, Any]]:
    """The operations the plan is written against, highest fan-out first.

    Fan-out is the ranking because a region twenty other regions depend on is structurally more
    important than one nothing reads, and if anything has to be dropped it should be the latter.
    """
    ranked = sorted(operations, key=lambda op: (-op.downstream_ref_count, op.id))
    return [
        {
            "id": operation.id,
            "sheet": operation.sheet,
            "ranges": operation.ranges,
            "cells": operation.cell_count,
            "orientation": operation.orientation,
            "pattern": operation.excel_pattern.value,
            "description": operation.description,
            "r1c1": operation.r1c1,
            "sample_a1": operation.sample_a1,
            "functions": operation.functions,
            "translation_hint": operation.translation_hint,
            "fan_out": operation.downstream_ref_count,
            "reconcilable": operation.cached_values_present,
            "volatile": operation.is_volatile,
            "external_refs": operation.has_external_refs,
            "inconsistencies": [
                {"cell": item.cell, "expected": item.expected_r1c1, "actual": item.actual_r1c1}
                for item in operation.inconsistencies[:5]
            ],
        }
        for operation in ranked[:limit]
    ]


def _profile_digest(
    profiles: list[ColumnProfile], limit: int, *, include_sample_values: bool
) -> list[dict[str, Any]]:
    """Column shape and distribution. Values only as capped top-k frequencies."""
    digest: list[dict[str, Any]] = []
    for profile in profiles[:limit]:
        entry: dict[str, Any] = {
            "sheet": profile.sheet,
            "column": profile.column,
            "header": profile.header,
            "dtype": profile.dtype,
            "rows": profile.row_count,
            "nulls": profile.null_count,
            "distinct": profile.distinct_count,
            "redacted": profile.redacted,
        }
        if profile.numeric is not None:
            entry["numeric"] = {
                "min": profile.numeric.min,
                "max": profile.numeric.max,
                "mean": profile.numeric.mean,
                "sum": profile.numeric.sum,
                "zeros": profile.numeric.zero_count,
                "negatives": profile.numeric.negative_count,
            }
        if profile.top_k and not profile.redacted:
            entry["top_values"] = [
                [_scalar(value), count] for value, count in profile.top_k[:_MAX_TOP_K]
            ]
        if profile.format_anomalies:
            entry["format_anomalies"] = profile.format_anomalies
        if include_sample_values and not profile.redacted:
            entry["head"] = [_scalar(value) for value in profile.head]
            entry["tail"] = [_scalar(value) for value in profile.tail]
        digest.append(entry)
    return digest


def _finding_digest(analysis: WorkbookAnalysis) -> list[dict[str, Any]]:
    """Findings, errors first — the reader most needs the things that break translations."""
    ranked = sorted(
        analysis.findings, key=lambda f: {"error": 0, "warning": 1, "info": 2}[f.severity.value]
    )
    return [
        {
            "kind": finding.kind.value,
            "severity": finding.severity.value,
            "message": finding.message,
            "sheet": finding.sheet,
            "location": finding.location,
            "remediation": finding.remediation,
        }
        for finding in ranked[:_MAX_FINDINGS]
    ]


def _scalar(value: Any) -> Any:
    """Render a cell value as something JSON can carry, without pulling in bulk data."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return _clip(value, 120) if isinstance(value, str) else value
    return _clip(str(value), 120)


def _clip(text: str | None, limit: int) -> str | None:
    """Truncate with an explicit marker, so nothing looks complete when it is not."""
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [{len(text) - limit} more characters omitted]"


# =============================================================================
# MESSAGES AND SCHEMA
# =============================================================================


def build_messages(
    analysis: WorkbookAnalysis,
    triage_result: TriageResult,
    *,
    seed_plan: ProcessPlan | None = None,
    context: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Assemble the system and user messages for one proposal.

    The system message carries the role and the hard rules; the vocabulary and the Excel
    semantics checklist follow it as a second system message, framed as a vocabulary the plan is
    written in rather than a template the plan must fit (PLAN 2.7).

    Args:
        analysis: The workbook analysis.
        triage_result: The deterministic assessment.
        seed_plan: A previously approved plan for this or a similar workbook, offered as a
            worked example rather than starting the model cold (PLAN 2.2).
        context: Pre-built digest; built from the analysis when omitted.
    """
    digest = context if context is not None else build_proposal_context(analysis, triage_result)
    seed_block = ""
    if seed_plan is not None:
        seed_block = (
            "# A previous plan for this process\n\n"
            "This was approved for an earlier version of this workbook. Where the process has "
            "not changed, keep the same stage ids so the plans diff cleanly. Where it has "
            "changed, change them and say what changed in `summary`.\n\n"
            f"```json\n{seed_plan.to_draft().model_dump_json(indent=2, exclude_none=True)}\n```"
        )

    user = _fill(
        load_prompt("propose_user.md"),
        {
            "workbook": _workbook_header(analysis),
            "triage": triage_result.explain(),
            "analysis": json.dumps(digest, indent=2, default=str),
            "seed": seed_block,
        },
    )
    return [
        {"role": "system", "content": load_prompt("propose_system.md")},
        {"role": "system", "content": load_prompt("propose_vocabulary.md")},
        {"role": "user", "content": user},
    ]


def _workbook_header(analysis: WorkbookAnalysis) -> str:
    identity = analysis.workbook
    return (
        f"- file: {identity.filename} ({identity.file_format}, "
        f"{identity.size_bytes / 1_048_576:.1f} MB)\n"
        f"- sha256: {identity.sha256}\n"
        f"- sheets: {len(analysis.sheets)}    logical operations: {len(analysis.operations)}\n"
        f"- reconcilable against cached values: {analysis.is_reconcilable}"
    )


@lru_cache(maxsize=1)
def plan_json_schema() -> dict[str, Any]:
    """Return the JSON schema for a :class:`~kedge.plan.model.PlanDraft`, in strict form.

    Strict structured output requires every object to forbid additional properties and to list
    every property as required, which pydantic's export does not do on its own. Defaults and
    format annotations are stripped for the same reason: endpoints reject keywords they do not
    implement, and a schema that is refused costs a fallback round trip.
    """
    return _strictify(PlanDraft.model_json_schema())


def _strictify(schema: dict[str, Any]) -> dict[str, Any]:
    """Recursively make a pydantic JSON schema acceptable to strict structured output."""
    node = {
        key: value
        for key, value in schema.items()
        if key not in {"default", "format", "examples", "$comment"}
    }
    for key in ("$defs", "properties"):
        if isinstance(node.get(key), dict):
            node[key] = {name: _strictify(sub) for name, sub in node[key].items()}
    for key in ("items", "additionalItems", "not"):
        if isinstance(node.get(key), dict):
            node[key] = _strictify(node[key])
    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        if isinstance(node.get(key), list):
            node[key] = [_strictify(sub) for sub in node[key] if isinstance(sub, dict)]
    if node.get("type") == "object" or "properties" in node:
        node["additionalProperties"] = False
        node["required"] = list(node.get("properties", {}))
    return node


# =============================================================================
# PARSING
# =============================================================================


def parse_draft(text: str, *, assessment: Assessment | None = None) -> PlanDraft:
    """Load a model response into a :class:`PlanDraft`, stripping what is not the model's to say.

    Tolerant of the two things models do to JSON regardless of instructions: wrapping it in a
    markdown fence, and adding a sentence before it. Anything beyond that is a validation error
    and becomes a repair instruction.

    This is also the one seam where kedge takes fields back off the model. ``assessment`` was the
    first — a plan that says ``0.9`` because the model felt confident is precisely the notebook
    that looks more complete than it is — and :func:`_strip_human_decisions` is the rest:
    a drop's acknowledgement and an open question's answer both record what a *reviewer* decided,
    and ``unacknowledged_drops`` is the only structural blocker a plan has. A model that could set
    it could sign off its own deletions and land them as ``[confirmed]`` on the review card. Both
    the CLI proposal path and the chat ``propose_plan`` tool come through here, so one strip
    covers both.

    Args:
        text: The raw assistant message.
        assessment: An assessment computed elsewhere. When given it *replaces* whatever the
            response carried, so a caller that scores convertibility deterministically — see
            :meth:`~kedge.plan.triage.TriageResult.as_assessment` — need not ask the model for a
            figure at all, and is not tripped up by one it volunteered anyway.

    Returns:
        The draft, with every drop reduced to its range and reason and every open question to its
        question and context.

    Raises:
        ValidationError: from pydantic, carrying the messages the retry loop feeds back.
        ProposalError: when there is no JSON object in the response at all.
    """
    candidate = _FENCE.sub("", text).strip()
    if not candidate.startswith("{"):
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end <= start:
            msg = f"the model returned no JSON object: {candidate[:200]!r}"
            raise ProposalError(msg)
        candidate = candidate[start : end + 1]
    if assessment is None:
        try:
            return _strip_human_decisions(PlanDraft.model_validate_json(candidate))
        except ValidationError:
            raise
        except ValueError as exc:  # malformed JSON that pydantic could not even tokenise
            msg = f"the model returned text that is not valid JSON: {exc}"
            raise ProposalError(msg) from exc
    try:
        payload = json.loads(candidate)
    except ValueError as exc:
        msg = f"the model returned text that is not valid JSON: {exc}"
        raise ProposalError(msg) from exc
    if not isinstance(payload, dict):
        msg = f"the model returned a {type(payload).__name__} where a JSON object was expected"
        raise ProposalError(msg)
    return _strip_human_decisions(PlanDraft.model_validate({**payload, "assessment": assessment}))


def _strip_human_decisions(draft: PlanDraft) -> PlanDraft:
    """Rebuild the fields a reviewer owns from their defaults, keeping what the model proposed.

    Applied after validation rather than to the raw JSON, so a response that is malformed still
    fails the way the retry loop expects and the model is still told about a field it invented —
    ``extra="forbid"`` is a repair instruction, and silently dropping keys here would cost it.

    A drop keeps its ``range`` and ``reason``; an open question keeps its ``question`` and
    ``context``. Everything else on both — ``acknowledged``, ``accepted``, ``note``,
    ``acknowledged_at``, ``answer``, ``answered_at`` — is a record of a decision only
    :mod:`kedge.plan.review` may write, on an explicit user action.
    """
    return draft.model_copy(
        update={
            "dropped": [
                DroppedRange(range=drop.range, reason=drop.reason) for drop in draft.dropped
            ],
            "open_questions": [
                OpenQuestion(question=question.question, context=question.context)
                for question in draft.open_questions
            ],
        }
    )


def describe_errors(exc: ValidationError) -> str:
    """Render a pydantic failure as the repair instruction a model can act on.

    One line per error, capped at twenty: past that the response is wrong in kind rather than in
    detail, and a longer list teaches the model nothing the first twenty did not.
    """
    lines = []
    for error in exc.errors()[:20]:
        location = ".".join(str(part) for part in error["loc"]) or "(root)"
        lines.append(f"- {location}: {error['msg']}")
    return "\n".join(lines)


# =============================================================================
# THE CALL
# =============================================================================


def propose_plan(
    analysis: WorkbookAnalysis,
    *,
    completer: Completer,
    triage_result: TriageResult | None = None,
    model: str = "",
    seed_plan: ProcessPlan | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    force: bool = False,
    temperature: float = 0.2,
    version: int = 1,
) -> ProcessPlan:
    """Propose a process plan for an analysed workbook. One LLM call, plus repairs.

    The convertibility figure on the plan that comes back is triage's, never the model's own:
    whatever the response claimed is replaced on the way through :func:`parse_draft`, exactly as
    the chat ``propose_plan`` tool does it. Both paths therefore write the same kind of number,
    which is the whole point of that field — :meth:`~kedge.plan.triage.TriageResult.as_assessment`
    explains why a model has nothing to score its own decomposition against.

    Args:
        analysis: The complete workbook analysis. Its summary, column profiles and logical
            operations become the model's context; raw values do not.
        completer: The LLM seam. Use :func:`completer_from_config` in production and
            :class:`ScriptedCompleter` in tests.
        triage_result: A pre-computed assessment; triage runs here when omitted.
        model: Model id to record on the plan and send in the request.
        seed_plan: A previous plan to re-seed from.
        max_attempts: Total attempts including repairs.
        force: Propose even when triage says stop. The refusal exists for a reason; overriding
            it should be a deliberate act at the call site.
        temperature: Sampling temperature.
        version: Plan version number to stamp.

    Returns:
        A validated, unapproved :class:`ProcessPlan`. Nothing reaches the notebook until a human
        approves it.

    Raises:
        ProposalRefusedError: when triage's verdict is ``STOP`` and ``force`` is not set.
        ProposalError: when the endpoint fails, or no attempt produced a valid plan.
    """
    assessment = triage_result if triage_result is not None else triage(analysis)
    if assessment.should_stop and not force:
        raise ProposalRefusedError(assessment)
    if assessment.should_stop:
        logger.warning(
            "proposing a plan for %s despite a STOP verdict, because force was set",
            analysis.workbook.filename,
        )

    messages = build_messages(analysis, assessment, seed_plan=seed_plan)
    schema = plan_json_schema()
    # Computed once, outside the retry loop, and handed to every attempt: the figure is a property
    # of the workbook, so it cannot depend on which attempt happened to parse.
    scored = assessment.as_assessment()
    failures: list[str] = []
    # The best plan seen so far, and how many things the scaffolder would trip over in it. A plan
    # that validates is never thrown away again: an amendment that comes back malformed, or comes
    # back worse, loses to the plan already in hand rather than costing the caller everything.
    best: ProcessPlan | None = None
    best_findings: list[str] = []
    amended = False

    for attempt in range(1, max(1, max_attempts) + 1):
        logger.info(
            "proposing a plan for %s (attempt %d of %d)",
            analysis.workbook.filename,
            attempt,
            max_attempts,
        )
        text = completer.complete(
            CompletionRequest(
                messages=list(messages),
                model=model,
                json_schema=schema,
                temperature=temperature,
            )
        )
        try:
            draft = parse_draft(text, assessment=scored)
        except (ValidationError, ProposalError) as exc:
            detail = describe_errors(exc) if isinstance(exc, ValidationError) else str(exc)
            failures.append(f"attempt {attempt}: {detail}")
            logger.warning("proposal attempt %d did not validate: %s", attempt, detail)
            if best is not None:
                # This was the amendment, and it came back malformed. There is already a valid
                # plan in hand, so there is nothing left to repair towards: spending the
                # remaining attempts re-asking would cost whole proposals to improve a plan the
                # caller is going to get either way.
                logger.warning(
                    "the amendment for %s did not validate; keeping the plan already proposed",
                    analysis.workbook.filename,
                )
                break
            if attempt >= max_attempts:
                break
            messages = [
                *messages,
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": _fill(load_prompt("propose_repair.md"), {"errors": detail}),
                },
            ]
            continue

        plan = ProcessPlan.from_analysis_draft(
            draft,
            analysis,
            llm_model=model or None,
            version=version,
            based_on_version=seed_plan.version if seed_plan is not None else None,
        )
        logger.info(
            "proposed %d stage(s), %d open question(s), %d drop(s) for %s",
            len(plan.stages),
            len(plan.open_questions),
            len(plan.dropped),
            analysis.workbook.filename,
        )

        findings = repairable_warnings(plan, analysis)
        if best is None or len(findings) < len(best_findings):
            best, best_findings = plan, findings
        if not findings or amended or attempt >= max_attempts:
            break

        # One amendment, never more. These findings are deterministic, so a model that did not
        # act on them the first time will not act on them the fifth, and every further round is
        # a whole proposal's tokens spent to re-read the same sentences. The user's review is
        # what catches a model that ignored them -- the card still renders every warning.
        amended = True
        logger.info(
            "amending the plan for %s: %d finding(s) the scaffolder would trip over",
            analysis.workbook.filename,
            len(findings),
        )
        messages = [
            *messages,
            {"role": "assistant", "content": text},
            {
                "role": "user",
                "content": _fill(
                    load_prompt("propose_amend.md"), {"warnings": _numbered(findings)}
                ),
            },
        ]

    if best is not None:
        if best_findings:
            logger.warning(
                "the plan for %s still carries %d finding(s) after amendment: %s",
                analysis.workbook.filename,
                len(best_findings),
                "; ".join(best_findings),
            )
        return best

    joined = "\n".join(failures)
    msg = (
        f"the model did not produce a valid process plan in {max_attempts} attempt(s) against "
        f"{analysis.workbook.filename}:\n{joined}"
    )
    raise ProposalError(msg)


def _numbered(findings: Sequence[str]) -> str:
    """The findings as a numbered list, which is how the amend prompt refers to them."""
    return "\n\n".join(f"{index}. {finding}" for index, finding in enumerate(findings, start=1))


def record_responses(
    responses: Iterable[str], directory: Path, *, prefix: str = "response"
) -> list[Path]:
    """Write model responses to disk so a live run can be replayed offline.

    The other half of :meth:`ScriptedCompleter.from_paths`: capture once against a real endpoint,
    replay in CI forever.

    Args:
        responses: Raw assistant message contents, in the order they arrived.
        directory: Where to write them. Created if absent.
        prefix: Filename stem.

    Returns:
        The paths written, in order.
    """
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for index, response in enumerate(responses, start=1):
        path = directory / f"{prefix}-{index:02d}.json"
        path.write_text(response, encoding="utf-8")
        written.append(path)
    return written


def scripted_from_plan(plan: ProcessPlan | PlanDraft) -> ScriptedCompleter:
    """Build a completer that returns exactly this plan. The offline test fixture in one line."""
    draft = plan.to_draft() if isinstance(plan, ProcessPlan) else plan
    return ScriptedCompleter([draft.model_dump_json()])
