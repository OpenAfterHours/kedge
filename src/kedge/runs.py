"""Run state: what happened, in what order, and what evidence it was based on.

A converted workbook is a runbook, and a runbook takes hours or days. The user runs the extract,
goes to a meeting, comes back, computes the adjustment, waits for an approval, runs the update
the next morning, re-extracts. **marimo's state does not survive any of that.** Widget values
live in the kernel; close the tab and the paste box is empty, the dropdown is unset, and a
process somebody is three steps into looks exactly like one nobody has started.

The hand-ins already survive -- that is what the managed store, the hashes and the receipts are
for. What was missing is everything else: which step this run has reached, which hand-in it
consumed at that step, what was approved and why, and who said the statement had been run. This
module holds that, as one JSON file per run.

**One file per run, and a run is never edited into a different run.** Starting fresh writes a new
file beside the old one rather than clearing anything. That is not filing tidiness: the record of
a Q2 adjustment is evidence, and a tool that lets "start again" quietly erase what was signed off
last month is one nobody should trust with a control. :meth:`RunStore.start` therefore always
creates, and the previous run stays on disk exactly as it was left.

**Every record carries a hash, not a path.** A path is a claim about where a file was; a SHA-256
is a claim about what it contained. Resuming a run means re-reading the hand-in the run actually
consumed, and being able to say so if the file at that path is no longer that file.

Nothing here validates the process. It does not know what a step means, what order steps come in,
or whether an approval was a good idea. It records what a person did, so the notebook can pick up
where they left off and so the audit line at the end is derived from something rather than
asserted.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kedge.errors import KedgeError

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

    from kedge.ingest.model import HandIn

logger = logging.getLogger(__name__)

__all__ = [
    "SCHEMA_VERSION",
    "ConsumedHandIn",
    "Decision",
    "RunError",
    "RunState",
    "RunStore",
    "previous_handin",
    "record_decision",
    "record_handin",
    "record_parameters",
    "utcnow",
]

SCHEMA_VERSION = "1.0"
"""The shape of a run file. Written into every one so an old run can be read by a new kedge."""

_RUN_ID = re.compile(r"^\d{8}T\d{6}Z(?:-\d+)?$")
"""``20260818T104812Z``. Sortable as text, unambiguous across time zones, and safe as a filename."""

_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"


class RunError(KedgeError):
    """A run could not be read, written, or resumed."""


def utcnow() -> datetime:
    """Now, in UTC. One definition, so every timestamp in a run file is comparable."""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ConsumedHandIn:
    """One hand-in this run took in, at one step.

    ``sha256`` is the record, not ``path``. Resuming re-reads the file, and if what is there now
    hashes differently the run says so rather than quietly computing on something else --
    which is the whole reason the managed store hashes on the way in.
    """

    step: str
    sha256: str
    path: str
    original_name: str
    received_at: datetime
    source: str = "selected"

    def audit_line(self) -> str:
        return (
            f"{self.step}: {self.original_name} ({self.source}) "
            f"sha256:{self.sha256[:12]} at {self.received_at.isoformat()}"
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "sha256": self.sha256,
            "path": self.path,
            "original_name": self.original_name,
            "received_at": self.received_at.isoformat(),
            "source": self.source,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> ConsumedHandIn:
        return cls(
            step=str(data["step"]),
            sha256=str(data["sha256"]),
            path=str(data["path"]),
            original_name=str(data["original_name"]),
            received_at=_read_stamp(data["received_at"]),
            source=str(data.get("source", "selected")),
        )


@dataclass(frozen=True, slots=True)
class Decision:
    """One thing a person decided, and why.

    Covers both shapes the runbook asks for: an approval at a checkpoint, and a confirmation
    that a hand-off was carried out. They are the same record -- somebody asserting something,
    with a reason and a time -- and splitting them into two types would mean two audit trails to
    read and reconcile.
    """

    step: str
    decision: str
    note: str = ""
    decided_at: datetime = field(default_factory=utcnow)

    def audit_line(self) -> str:
        reason = f" -- {self.note}" if self.note else ""
        return f"{self.step}: {self.decision} at {self.decided_at.isoformat()}{reason}"

    def as_json(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "decision": self.decision,
            "note": self.note,
            "decided_at": self.decided_at.isoformat(),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Decision:
        return cls(
            step=str(data["step"]),
            decision=str(data["decision"]),
            note=str(data.get("note", "")),
            decided_at=_read_stamp(data["decided_at"]),
        )


@dataclass(frozen=True, slots=True)
class RunState:
    """One pass through a runbook, as far as it has got.

    Frozen, and every recording method returns a new state. The store writes on each change, so
    "the file on disk is the run" holds without anybody remembering to save -- and a crash
    between two steps loses at most the step in progress.

    Example:
        >>> state = RunState(run_id="20260818T104812Z", workbook="q2.xlsx")
        >>> state.with_decision("approve_adjustment", "approve", "agreed").decision_for(
        ...     "approve_adjustment"
        ... ).decision
        'approve'
    """

    run_id: str
    workbook: str
    started_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
    parameters: dict[str, Any] = field(default_factory=dict)
    handins: tuple[ConsumedHandIn, ...] = ()
    decisions: tuple[Decision, ...] = ()
    schema_version: str = SCHEMA_VERSION

    # ── reading ──────────────────────────────────────────────────────────────

    def handin_for(self, step: str) -> ConsumedHandIn | None:
        """The hand-in this run consumed at ``step``, latest wins.

        Latest rather than first because re-supplying a hand-in is a legitimate thing to do --
        the extract was wrong, the user runs it again -- and the run should follow the correction
        rather than the mistake. Both stay in the record.
        """
        return next(
            (item for item in reversed(self.handins) if item.step == step),
            None,
        )

    def decision_for(self, step: str) -> Decision | None:
        """The decision recorded at ``step``, latest wins."""
        return next((item for item in reversed(self.decisions) if item.step == step), None)

    def has_decided(self, step: str, *, expecting: str | None = None) -> bool:
        """Whether ``step`` has a decision, optionally a particular one.

        This is what a notebook gate reads. ``expecting`` exists so the common case -- "has the
        adjustment been approved" rather than "has anything been recorded" -- does not have to be
        spelled out at every call site and get spelled differently at one of them.
        """
        decision = self.decision_for(step)
        if decision is None:
            return False
        return expecting is None or decision.decision == expecting

    @property
    def steps_reached(self) -> tuple[str, ...]:
        """Every step this run has recorded anything against, in the order it first did."""
        seen: dict[str, None] = {}
        for item in (*self.handins, *self.decisions):
            seen.setdefault(item.step, None)
        return tuple(seen)

    def audit_lines(self) -> list[str]:
        """The whole run as ordered prose, for the sign-off panel and for a reviewer."""
        events: list[tuple[datetime, str]] = [
            (item.received_at, item.audit_line()) for item in self.handins
        ]
        events.extend((item.decided_at, item.audit_line()) for item in self.decisions)
        return [line for _, line in sorted(events, key=lambda pair: pair[0])]

    # ── writing ──────────────────────────────────────────────────────────────

    def with_handin(self, step: str, handin: HandIn) -> RunState:
        """Record that ``step`` consumed this hand-in."""
        consumed = ConsumedHandIn(
            step=step,
            sha256=handin.sha256,
            path=str(handin.path),
            original_name=handin.original_name,
            received_at=handin.received_at,
            source=str(handin.source),
        )
        return replace(self, handins=(*self.handins, consumed), updated_at=utcnow())

    def with_decision(self, step: str, decision: str, note: str = "") -> RunState:
        """Record an approval, a rejection, or a confirmation that something was run."""
        return replace(
            self,
            decisions=(*self.decisions, Decision(step=step, decision=decision, note=note)),
            updated_at=utcnow(),
        )

    def with_parameters(self, **values: Any) -> RunState:
        """Record the inputs this run was given -- a period end, an entity list, a rate.

        Kept because a statement handed to a user was rendered with them, and a run that cannot
        say which period it was for is not a record of anything.
        """
        return replace(self, parameters={**self.parameters, **values}, updated_at=utcnow())

    # ── serialisation ────────────────────────────────────────────────────────

    def as_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "workbook": self.workbook,
            "started_at": self.started_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "parameters": _plain(self.parameters),
            "handins": [item.as_json() for item in self.handins],
            "decisions": [item.as_json() for item in self.decisions],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> RunState:
        try:
            return cls(
                run_id=str(data["run_id"]),
                workbook=str(data["workbook"]),
                started_at=_read_stamp(data["started_at"]),
                updated_at=_read_stamp(data["updated_at"]),
                parameters=dict(data.get("parameters") or {}),
                handins=tuple(ConsumedHandIn.from_json(item) for item in data.get("handins") or ()),
                decisions=tuple(Decision.from_json(item) for item in data.get("decisions") or ()),
                schema_version=str(data.get("schema_version", SCHEMA_VERSION)),
            )
        except (KeyError, TypeError, ValueError) as error:
            msg = f"run file is missing or has a malformed field: {error}"
            raise RunError(msg) from error


class RunStore:
    """The runs for one converted workbook, on disk.

    One JSON file per run, named by run id, so the directory listing is the history in order.
    Nothing is ever overwritten by :meth:`start`.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = Path(directory)

    @property
    def directory(self) -> Path:
        return self._directory

    def path_for(self, run_id: str) -> Path:
        if not _RUN_ID.match(run_id):
            msg = (
                f"{run_id!r} is not a run id. They look like '20260818T104812Z' and are "
                f"generated by RunStore.start()."
            )
            raise RunError(msg)
        return self._directory / f"{run_id}.json"

    def start(self, *, workbook: str, now: datetime | None = None) -> RunState:
        """Begin a new run. Never touches an existing one.

        A second run started inside the same second gets a numeric suffix rather than colliding.
        That is not hypothetical -- clicking "start fresh" twice is exactly the sort of thing a
        confused user does, and the second click silently overwriting the first would be the
        worst possible response to it.
        """
        stamp = now or utcnow()
        base = stamp.strftime(_STAMP_FORMAT)
        run_id, suffix = base, 2
        while self.path_for(run_id).exists():
            run_id = f"{base}-{suffix}"
            suffix += 1
        state = RunState(run_id=run_id, workbook=workbook, started_at=stamp, updated_at=stamp)
        self.save(state)
        logger.info("started run %s for %s", run_id, workbook)
        return state

    def save(self, state: RunState) -> Path:
        """Write a run, atomically enough that a crash cannot leave half a file.

        Written to a temporary name in the same directory and renamed over the target. A run file
        is the record of what somebody approved; a truncated one is worse than a missing one,
        because it looks readable.
        """
        self._directory.mkdir(parents=True, exist_ok=True)
        target = self.path_for(state.run_id)
        scratch = target.with_suffix(".json.tmp")
        try:
            scratch.write_text(
                json.dumps(state.as_json(), indent=2, sort_keys=False) + "\n", encoding="utf-8"
            )
            scratch.replace(target)
        except OSError as error:
            msg = f"could not write the run to {target}: {error}"
            raise RunError(msg) from error
        return target

    def load(self, run_id: str) -> RunState:
        path = self.path_for(run_id)
        try:
            return RunState.from_json(json.loads(path.read_text(encoding="utf-8")))
        except OSError as error:
            msg = f"could not read the run at {path}: {error}"
            raise RunError(msg) from error
        except json.JSONDecodeError as error:
            msg = f"the run at {path} is not valid JSON: {error}"
            raise RunError(msg) from error

    def run_ids(self) -> list[str]:
        """Every run, oldest first. The id format sorts chronologically as text."""
        if not self._directory.is_dir():
            return []
        return sorted(path.stem for path in self._directory.glob("*.json"))

    def history(self) -> Iterator[RunState]:
        """Every readable run, oldest first.

        A run that will not parse is logged and skipped rather than raising: one corrupt file
        from an interrupted write must not make the rest of the history unreadable.
        """
        for run_id in self.run_ids():
            try:
                yield self.load(run_id)
            except RunError:
                logger.warning("skipping unreadable run %s", run_id)

    def latest(self) -> RunState | None:
        """The most recently started run, or ``None`` if there are none."""
        ids = self.run_ids()
        if not ids:
            return None
        try:
            return self.load(ids[-1])
        except RunError:
            logger.warning("the latest run %s will not load", ids[-1])
            return None

    def resume_or_start(self, *, workbook: str) -> RunState:
        """The run to continue, starting one only if there is nothing to continue.

        This is the single call a notebook's head makes. Resuming is the default because it is
        what the user meant by reopening the notebook; starting fresh is a decision, and it has
        its own method so it cannot happen by accident.
        """
        existing = self.latest()
        if existing is not None:
            logger.info(
                "resuming run %s (%d hand-in(s), %d decision(s))",
                existing.run_id,
                len(existing.handins),
                len(existing.decisions),
            )
            return existing
        return self.start(workbook=workbook)


# =============================================================================
# WHAT A NOTEBOOK CELL CALLS
# =============================================================================
#
# marimo's single-definition rule means a cell cannot rebind `run` -- so a notebook cannot thread
# an immutable RunState from cell to cell the way ordinary Python would. Each of these is
# therefore load-modify-save against the run id: the file is the state, and every cell that
# records something reads the current file rather than a stale copy captured when the notebook
# opened. That also makes a half-finished run correct after a kernel restart, which is the whole
# point of the module.


def record_handin(store: RunStore, run_id: str, step: str, handin: HandIn) -> ConsumedHandIn:
    """Record that ``step`` consumed ``handin``, and return what was written."""
    state = store.load(run_id).with_handin(step, handin)
    store.save(state)
    consumed = state.handin_for(step)
    if consumed is None:  # pragma: no cover - with_handin just appended it
        msg = f"recording the hand-in for {step!r} did not take"
        raise RunError(msg)
    logger.info("run %s: %s", run_id, consumed.audit_line())
    return consumed


def record_decision(
    store: RunStore, run_id: str, step: str, decision: str, note: str = ""
) -> Decision:
    """Record an approval, a rejection, or a confirmation that a statement was run."""
    state = store.load(run_id).with_decision(step, decision, note)
    store.save(state)
    recorded = state.decision_for(step)
    if recorded is None:  # pragma: no cover - with_decision just appended it
        msg = f"recording the decision for {step!r} did not take"
        raise RunError(msg)
    logger.info("run %s: %s", run_id, recorded.audit_line())
    return recorded


def record_parameters(store: RunStore, run_id: str, **values: Any) -> dict[str, Any]:
    """Record the inputs this run was given, and return them as stored."""
    state = store.load(run_id).with_parameters(**values)
    store.save(state)
    return dict(state.parameters)


def previous_handin(store: RunStore, run_id: str, step: str) -> Path | None:
    """The file this run already consumed at ``step``, if it is still there and unchanged.

    What makes resuming real. The user closed the notebook after pasting the extract; on reopen
    the paste box is empty, but the managed copy is on disk and the run says which one it was.
    Returning its path lets the cell carry on without asking again.

    ``None`` when there is no record, when the file has gone, or when what is at that path no
    longer hashes the same -- the last being the case worth having: a run must not silently
    resume onto different data.
    """
    from kedge.ingest.store import hash_file

    try:
        consumed = store.load(run_id).handin_for(step)
    except RunError:
        return None
    if consumed is None:
        return None
    path = Path(consumed.path)
    if not path.is_file():
        logger.warning(
            "run %s consumed %s at step %s, but that file is no longer there",
            run_id,
            path,
            step,
        )
        return None
    if hash_file(path) != consumed.sha256:
        logger.warning(
            "run %s consumed sha256:%s at step %s, but %s now holds different bytes",
            run_id,
            consumed.sha256[:12],
            step,
            path,
        )
        return None
    return path


def _plain(values: Mapping[str, Any]) -> dict[str, Any]:
    """Render parameter values as JSON can hold them, without losing what they were.

    Dates and datetimes go to ISO strings; everything else is passed through and will raise at
    serialisation if it genuinely cannot be written, which is better than silently storing a
    ``repr``.
    """
    out: dict[str, Any] = {}
    for key, value in values.items():
        if isinstance(value, datetime) or hasattr(value, "isoformat"):
            out[key] = value.isoformat()
        elif isinstance(value, tuple | set | frozenset):
            out[key] = list(value)
        else:
            out[key] = value
    return out


def _read_stamp(value: Any) -> datetime:
    """Parse an ISO timestamp, insisting it carry a time zone.

    A naive timestamp in an audit record is one nobody can compare against anything, and the
    files this reads are written by :func:`utcnow`, which always sets one.
    """
    stamp = datetime.fromisoformat(str(value))
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


def steps_completed(state: RunState, steps: Sequence[str]) -> list[bool]:
    """Which of ``steps`` this run has reached, in the order given.

    The progress panel's data. Kept here rather than in the scaffolder because "has this step
    happened" is a question about the run, and the notebook should not be re-deriving it from
    two lists every time it renders.
    """
    reached = set(state.steps_reached)
    return [step in reached for step in steps]
