"""What an eval run produces: a score per rubric item, and a report somebody reads.

Three outcomes per item, not two. ``PASS`` and ``FAIL`` are the obvious pair; ``SKIP`` exists
because an item can be ungradeable for reasons that are nobody's fault -- the optional SQL engine
is not installed, no plan was supplied, the notebook stopped before the cell this item is about
ever ran. Folding those into ``FAIL`` would make a missing dependency look like a broken
conversion, and folding them into ``PASS`` would be the reconciliation sin: reporting a result
that was never measured.

So a report carries **two numbers, always**: the score, and how much of the rubric it was taken
over. A run that skipped half the items and passed the rest is not 100%, and
:meth:`EvalReport.headline` refuses to render it as though it were.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = [
    "EvalReport",
    "ItemResult",
    "Outcome",
    "TierResult",
]


class Outcome(StrEnum):
    """How one rubric item came out.

    Example:
        >>> bool(Outcome.PASS), bool(Outcome.SKIP)
        (True, False)
    """

    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"

    def __bool__(self) -> bool:
        """True only for :attr:`PASS`, so a skipped item can never read as a success."""
        return self is Outcome.PASS

    @property
    def marker(self) -> str:
        return {Outcome.PASS: "PASS", Outcome.FAIL: "FAIL", Outcome.SKIP: "SKIP"}[self]


@dataclass(frozen=True, slots=True)
class ItemResult:
    """One rubric item, graded.

    ``detail`` is the whole value of a failure. "totals_to_the_penny: FAIL" tells nobody
    anything; "expected 884207.62, got 884203.15 -- a 4.47 difference, about four rows' worth of
    a penny each" tells them where to look.
    """

    id: str
    outcome: Outcome
    weight: int = 1
    detail: str = ""

    @property
    def earned(self) -> int:
        return self.weight if self.outcome is Outcome.PASS else 0

    @property
    def available(self) -> int:
        """Zero for a skip: an item nobody could grade is not part of the denominator."""
        return 0 if self.outcome is Outcome.SKIP else self.weight

    def render(self) -> str:
        head = f"  [{self.outcome.marker}] {self.id} ({self.weight})"
        if not self.detail:
            return head
        indented = "\n".join(f"        {line}" for line in self.detail.splitlines())
        return f"{head}\n{indented}"


@dataclass(frozen=True, slots=True)
class TierResult:
    """One tier of the rubric -- deterministic, structural -- and everything in it."""

    name: str
    items: tuple[ItemResult, ...] = ()

    @property
    def earned(self) -> int:
        return sum(item.earned for item in self.items)

    @property
    def available(self) -> int:
        return sum(item.available for item in self.items)

    @property
    def skipped(self) -> tuple[ItemResult, ...]:
        return tuple(item for item in self.items if item.outcome is Outcome.SKIP)

    def render(self) -> str:
        if not self.items:
            return f"{self.name}: nothing graded"
        lines = [f"{self.name}: {self.earned}/{self.available}" + _skip_note(self.skipped)]
        lines.extend(item.render() for item in self.items)
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class EvalReport:
    """A whole eval run: every tier, plus what was graded and how it was driven."""

    case: str
    tiers: tuple[TierResult, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def earned(self) -> int:
        return sum(tier.earned for tier in self.tiers)

    @property
    def available(self) -> int:
        return sum(tier.available for tier in self.tiers)

    @property
    def skipped(self) -> tuple[ItemResult, ...]:
        return tuple(item for tier in self.tiers for item in tier.skipped)

    @property
    def failures(self) -> tuple[ItemResult, ...]:
        return tuple(
            item for tier in self.tiers for item in tier.items if item.outcome is Outcome.FAIL
        )

    @property
    def ok(self) -> bool:
        """Whether everything that could be graded passed.

        Deliberately does *not* require a full denominator. Whether a partial run is acceptable
        is the caller's decision, and :attr:`skipped` is there to make it -- but "nothing I could
        measure was wrong" is still a distinct and useful thing to be able to ask.
        """
        return not self.failures

    def headline(self) -> str:
        """The one line worth putting at the top, with the denominator visible.

        The percentage is over what was *gradeable*, so it is stated alongside the count rather
        than instead of it. A bare "100%" over half a rubric is the number this avoids printing.
        """
        if not self.available:
            return f"{self.case}: nothing could be graded ({len(self.skipped)} item(s) skipped)"
        share = 100.0 * self.earned / self.available
        line = f"{self.case}: {self.earned}/{self.available} ({share:.0f}%)"
        return line + _skip_note(self.skipped)

    def render(self) -> str:
        """The full text report."""
        blocks = [self.headline(), ""]
        blocks.extend(tier.render() + "\n" for tier in self.tiers)
        if self.notes:
            blocks.append("Notes")
            blocks.extend(f"  - {note}" for note in self.notes)
        return "\n".join(blocks).rstrip() + "\n"

    def exit_code(self) -> int:
        """0 when nothing gradeable failed, 1 otherwise. What a CI step wants."""
        return 0 if self.ok else 1


def _skip_note(skipped: Sequence[ItemResult]) -> str:
    if not skipped:
        return ""
    return f"  [{len(skipped)} skipped: {', '.join(item.id for item in skipped)}]"


def tier(name: str, items: Iterable[ItemResult]) -> TierResult:
    """Build a :class:`TierResult`, keeping rubric order."""
    return TierResult(name=name, items=tuple(items))
