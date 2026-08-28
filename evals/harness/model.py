"""What an eval run produces: a score per rubric item, and a report somebody reads.

Four outcomes per item, not two, and the fourth is the one with a story. ``PASS`` and ``FAIL``
are the obvious pair. ``SKIP`` is for an item that is ungradeable *for reasons that are nobody's
fault* -- the optional SQL engine is not installed, no plan was supplied, the case has no
knowledge pack to consult. Folding those into ``FAIL`` would make a missing dependency look like
a broken conversion, and folding them into ``PASS`` would be the reconciliation sin: reporting a
result that was never measured.

``BLOCKED`` is for an item nobody could grade **because the conversion under test broke first**:
the notebook stopped in an earlier cell, or the plan omitted the hand-off this item asks about.
It exists because those two were once the same outcome, and treating them alike had the eval
flattering exactly the conversions it was built to catch.

**Why the distinction is arithmetic and not presentation.** :attr:`ItemResult.available` drops a
``SKIP`` from the denominator, which is right -- an item the case cannot pose is not one the
conversion failed. Apply that to a stopped notebook and the denominator shrinks in proportion to
how early the thing broke, so the *worse* the conversion, the better it scores. Measured on a real
hub conversion of ``adjustment_signoff``: 6/33 (18%) as reported, against 6/69 (9%) once the items
its own stop had removed were counted. Repairing only the cell that stopped it, and nothing else,
would have *lowered* the printed figure from 18% to 9%. A ``BLOCKED`` item therefore stays in the
denominator and earns nothing.

What it does **not** do is diagnose. Fifteen red lines that are all one bug bury the bug, which is
the argument :meth:`Context.need` makes and it is a good one; blocked items are counted in the
score, named in one line, and explained once by the ``FAIL`` that caused them.

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
        >>> bool(Outcome.PASS), bool(Outcome.SKIP), bool(Outcome.BLOCKED)
        (True, False, False)
    """

    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"
    BLOCKED = "blocked"

    def __bool__(self) -> bool:
        """True only for :attr:`PASS`, so a skipped item can never read as a success."""
        return self is Outcome.PASS

    @property
    def counts_against(self) -> bool:
        """Whether this outcome leaves the item in the denominator.

        The whole distinction between :attr:`SKIP` and :attr:`BLOCKED` in one property.
        """
        return self is not Outcome.SKIP

    @property
    def marker(self) -> str:
        return {
            Outcome.PASS: "PASS",
            Outcome.FAIL: "FAIL",
            Outcome.SKIP: "SKIP",
            Outcome.BLOCKED: "BLOCKED",
        }[self]


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
        """Zero for a skip, full weight for everything else -- ``BLOCKED`` included.

        An item the *case* could not pose is not part of the denominator. An item the
        *conversion* prevented is: it is a thing the conversion was asked for and did not
        deliver, and dropping it would pay the conversion for breaking early.
        """
        return self.weight if self.outcome.counts_against else 0

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

    @property
    def blocked(self) -> tuple[ItemResult, ...]:
        return tuple(item for item in self.items if item.outcome is Outcome.BLOCKED)

    def render(self) -> str:
        if not self.items:
            return f"{self.name}: nothing graded"
        lines = [
            f"{self.name}: {self.earned}/{self.available}"
            + _blocked_note(self.blocked)
            + _skip_note(self.skipped)
        ]
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
    def blocked(self) -> tuple[ItemResult, ...]:
        return tuple(item for tier in self.tiers for item in tier.blocked)

    @property
    def failures(self) -> tuple[ItemResult, ...]:
        return tuple(
            item for tier in self.tiers for item in tier.items if item.outcome is Outcome.FAIL
        )

    @property
    def ok(self) -> bool:
        """Whether everything the conversion was responsible for came out right.

        Deliberately does *not* require a full denominator -- a ``SKIP`` is the case's limit, not
        the conversion's, and whether a partial run is acceptable is the caller's decision, which
        :attr:`skipped` is there to make.

        A ``BLOCKED`` item is different in kind and is counted here. It says the conversion broke
        before this could be measured, and "nothing I could measure was wrong" must not come back
        true for a notebook that stopped on its second cell -- which is precisely what it used to
        do whenever the stop itself happened to be the only ``FAIL``.
        """
        return not self.failures and not self.blocked

    def headline(self) -> str:
        """The one line worth putting at the top, with the denominator visible.

        The percentage is over what was *gradeable*, so it is stated alongside the count rather
        than instead of it. A bare "100%" over half a rubric is the number this avoids printing,
        and a shrinking denominator is the subtler version of the same lie -- hence blocked items
        counting, and being named on this line so a reader can see the score is mostly a
        consequence of one break rather than of thirty separate ones.
        """
        if not self.available:
            return f"{self.case}: nothing could be graded ({len(self.skipped)} item(s) skipped)"
        share = 100.0 * self.earned / self.available
        line = f"{self.case}: {self.earned}/{self.available} ({share:.0f}%)"
        return line + _blocked_note(self.blocked, names=False) + _skip_note(self.skipped)

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


def _blocked_note(blocked: Sequence[ItemResult], *, names: bool = True) -> str:
    """Blocked items, counted in points rather than in items.

    Points, because points are what the denominator is in and what the reader is being asked to
    compare. "16 skipped" beside "6/33" gives no sense that the missing items are worth six times
    the ones that were graded.

    ``names=False`` on the headline. Fourteen ids is not a headline, and every one of them is
    named on the tier line below it and rendered in full further down; the number is the part
    that has to survive being quoted on its own.
    """
    if not blocked:
        return ""
    weight = sum(item.weight for item in blocked)
    note = f"  [{weight} point(s) across {len(blocked)} item(s) blocked by the conversion"
    return f"{note}: {', '.join(item.id for item in blocked)}]" if names else f"{note}]"


def tier(name: str, items: Iterable[ItemResult]) -> TierResult:
    """Build a :class:`TierResult`, keeping rubric order."""
    return TierResult(name=name, items=tuple(items))
