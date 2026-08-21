"""Excel's text-to-number coercion, one cell at a time, and the line it must not cross.

:func:`kedge.xl.to_number` answers this question for a whole column, as a polars expression.
Two callers need the same answer for a single value, and neither of them has a frame:

* :mod:`kedge.reconcile.compare`, holding one cached workbook cell and one notebook value.
  ``VLOOKUP`` returns the cell it found *verbatim*, so a rate card pasted out of a database
  client leaves ``'20.0'`` cached in a column of numbers -- and Excel goes on multiplying by it
  perfectly happily. A conversion that types the column, which it must, then produces ``20.0``.
  Reading that pair as a type difference makes the region unreconcilable by construction.
* :mod:`kedge.ingest.coerce`, deciding whether a text column of a hand-in is really numeric.

**The line is information loss, not type.** ``to_number`` is deliberately liberal, because when
a translation asks for it the intent is explicit. Deciding *unasked* is a different question,
and :func:`loses_information_as_a_number` is where the answer is written down once: a leading
zero and more than fifteen significant digits both mean the digits are an identifier -- a cost
centre, an account, a sort code -- and ``00417`` is not the number 417.

This module holds no polars, so :mod:`kedge.reconcile.model` can use it and stay the
dependency-free schema its own docstring claims to be. It owns the character class
:func:`kedge.xl.to_number` strips as well, so the expression and the scalar cannot drift apart
in the one place it would be easiest for them to.
"""

from __future__ import annotations

import math
import re
from typing import Final

__all__ = [
    "MAX_SIGNIFICANT_DIGITS",
    "STRIP_PATTERN",
    "loses_information_as_a_number",
    "unambiguous_number",
]

STRIP_PATTERN: Final[str] = r"[\s,$£€¥]"
"""Characters stripped before parsing a text-formatted number.

Whitespace (including the non-breaking and narrow non-breaking spaces that survive a copy-paste
out of a web report), the comma group separator, and the currency symbols Excel accepts inline.
Shared with :func:`kedge.xl.to_number`, which compiles it as a polars regex.
"""

MAX_SIGNIFICANT_DIGITS: Final[int] = 15
"""Beyond this a value does not survive ``Float64``, so reading it as a number loses digits.

The same limit Excel works to, which is why long identifiers are text in the source system as
well. See :func:`kedge.xl.round_half_away` for the other place this number governs.
"""

_STRIP_CHARS: Final[re.Pattern[str]] = re.compile(STRIP_PATTERN)

_LEADING_ZERO: Final[re.Pattern[str]] = re.compile(r"^[+-]?0\d")
"""``00123``, ``0123`` -- an identifier. ``0``, ``0.5`` and ``-0.25`` are numbers and are fine."""

_DIGITS: Final[re.Pattern[str]] = re.compile(r"\d")


def loses_information_as_a_number(text: str) -> bool:
    """Whether reading this text as a number would throw away something that mattered.

    Two rules, and each is here because the liberal answer destroys information:

    * **A leading zero before a significant digit.** ``00123`` is a cost centre, an account, a
      sort code -- an identifier that happens to be spelled in digits. Reading it as ``123.0``
      loses the zeros and silently breaks every join it takes part in, and would make a client
      code ``00417`` compare equal to the number ``417``.
    * **More than 15 significant digits.** A 16-digit account number does not survive a round
      trip through ``Float64``. Excel has the same limit, which is why such things are text in
      the source system too.

    Args:
        text: The cell's text, already stripped of surrounding whitespace.

    Returns:
        True when the digits are an identifier rather than a quantity.

    Example:
        >>> loses_information_as_a_number("00417"), loses_information_as_a_number("417")
        (True, False)
    """
    return bool(_LEADING_ZERO.match(text)) or len(_DIGITS.findall(text)) > MAX_SIGNIFICANT_DIGITS


def unambiguous_number(value: object) -> float | None:
    """The number a text cell spells, when it spells one and nothing is lost by saying so.

    The scalar twin of :func:`kedge.xl.to_number`, with
    :func:`loses_information_as_a_number` in front of it. A second spelling of "what is a
    number" is exactly the drift :mod:`kedge.xl` exists to prevent, so the unit tests assert
    that this and ``to_number`` return the same value for every input either of them reads.

    Three deliberate divergences, all of them stricter:

    * ``inf`` and ``nan`` are rejected. ``to_number`` returns polars' parse of them, but no
      workbook cell holds an infinity and nothing can be reconciled against one.
    * Underscores are rejected. Python's ``float`` reads ``1_000`` as a thousand; polars does
      not, and the polars answer is the one that has to win.
    * Non-ASCII digits are rejected, for the same reason: Python's ``float`` reads them.

    Args:
        value: Anything. Only a ``str`` can spell a number; everything else returns None,
            ``bool`` included, so a caller may ask this of an arbitrary cell.

    Returns:
        The finite number the text unambiguously spells, or None.

    Example:
        >>> unambiguous_number("20.0"), unambiguous_number("1,234.56")
        (20.0, 1234.56)
        >>> unambiguous_number("00417") is None  # a client code, not the number 417
        True
        >>> unambiguous_number("n/a"), unambiguous_number(417)
        (None, None)
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or loses_information_as_a_number(text):
        return None
    number = _parse_number(text)
    return number if number is not None and math.isfinite(number) else None


def _parse_number(text: str) -> float | None:
    """Parse one stripped cell exactly as :func:`kedge.xl.to_number` parses a column.

    The order is load-bearing and mirrors the expression step for step: match a *pair* of
    parentheses first, then a trailing percent inside them, then strip the group separators and
    currency symbols, then parse. ``"(12%)"`` is ``-0.12`` and ``"(6"`` is not a number at all.
    """
    parenthesised = text.startswith("(") and text.endswith(")")
    core = text[1:-1] if parenthesised else text

    percentage = core.endswith("%")
    if percentage:
        core = core[:-1]

    core = _STRIP_CHARS.sub("", core)
    if not core.isascii() or "_" in core:
        return None
    try:
        value = float(core)
    except ValueError:
        return None

    if percentage:
        value /= 100.0
    return -value if parenthesised else value
