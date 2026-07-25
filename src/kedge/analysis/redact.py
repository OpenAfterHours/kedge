"""Optional column masking. Off by default (PLAN 2.3).

Redaction is a name-driven switch, not a content classifier: a column whose header matches
one of the configured patterns reports its dtype, row count and null count, and nothing else.
That is enough for the planner to write correct code against the column — it needs to know a
key is a string and how many blanks it has — without any value crossing the boundary.

:func:`redact_profile` is deliberately *total*. It does not mask fields one by one; it builds
a fresh :class:`~kedge.analysis.model.ColumnProfile` from a small allowlist of value-free
fields. Any field added to the contract later therefore arrives at its default rather than
being copied through, so a redacted column cannot start leaking because someone extended the
model. The sentinel test in ``tests/unit/test_redact.py`` is the guarantee; this docstring is
only the intent.

:func:`hash_value` exists for the row-level sampling tools (PLAN 2.3, M4), which hash rather
than elide so that repeated values stay visibly repeated. Profiles elide instead, because a
frequency table of hashes is still a frequency table.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Sequence
from typing import Any

from kedge.analysis.model import ColumnProfile

logger = logging.getLogger(__name__)

__all__ = [
    "HASH_PREFIX",
    "compile_patterns",
    "hash_value",
    "redact_profile",
    "should_redact",
]

HASH_PREFIX = "sha256:"
"""Marker on every hashed value, so a reader never mistakes one for a real value."""

_MIN_PREFIX_LENGTH = 4
_MAX_PREFIX_LENGTH = 64


def compile_patterns(patterns: Sequence[str] | None) -> list[re.Pattern[str]]:
    """Compile column-name patterns, tolerating ones that are not valid regular expressions.

    A hand-written config is the usual source of these, so a pattern that does not compile
    falls back to a literal (escaped) match and a warning rather than taking the analysis
    down with it.

    Args:
        patterns: Case-insensitive regular expressions, or ``None``.

    Returns:
        One compiled pattern per input, in the same order.
    """
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns or ():
        if not pattern:
            continue
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error as exc:
            logger.warning(
                "redaction pattern %r is not a valid regular expression (%s); "
                "matching it literally instead",
                pattern,
                exc,
            )
            compiled.append(re.compile(re.escape(pattern), re.IGNORECASE))
    return compiled


def should_redact(header: str | None, patterns: Sequence[str] | None) -> bool:
    """Report whether a column with this header should have its values withheld.

    Matching is a case-insensitive :func:`re.search`, so ``"account"`` matches
    ``"Account Number"``. A column with no header can never match, because there is nothing
    to match against — redaction is configured by name.

    Args:
        header: The column header, or ``None`` when the column is unheaded.
        patterns: Case-insensitive regular expressions from ``redaction.column_patterns``.

    Returns:
        True when at least one pattern matches.
    """
    if not header or not patterns:
        return False
    return any(pattern.search(header) for pattern in compile_patterns(patterns))


def redact_profile(profile: ColumnProfile) -> ColumnProfile:
    """Return a copy of ``profile`` carrying shape but no values.

    Rebuilt field by field from an allowlist rather than copied and masked: ``head``,
    ``tail``, ``sample`` and ``top_k`` are dropped, and so is ``numeric``, because a minimum
    and a maximum are values like any other. What survives is the column's identity, its
    dtype, its row and null counts, its cardinality, and its format anomalies — the last of
    which are drawn from a fixed value-free vocabulary (see
    :data:`kedge.analysis.profile.FORMAT_ANOMALY_PREFIXES`).

    Args:
        profile: The profile to redact. Redacting an already-redacted profile is a no-op.

    Returns:
        A new frozen :class:`~kedge.analysis.model.ColumnProfile` with ``redacted`` set.
    """
    return ColumnProfile(
        sheet=profile.sheet,
        column=profile.column,
        index=profile.index,
        header=profile.header,
        dtype=profile.dtype,
        row_count=profile.row_count,
        null_count=profile.null_count,
        distinct_count=profile.distinct_count,
        redacted=True,
        format_anomalies=list(profile.format_anomalies),
    )


def hash_value(value: Any, *, prefix_length: int = 12) -> str:
    """Return a stable, truncated SHA-256 rendering of a single cell value.

    Stable across runs and across machines — unlike :func:`hash`, which is salted per
    process — so equal values hash equal in every session and a reviewer can still see that
    two rows share a key. That is the point, and also the limit: this is consistent elision,
    not anonymisation. A low-cardinality column (a country code, a yes/no flag) is trivially
    recoverable by hashing the candidates, so redaction is a control on incidental exposure,
    not a claim about irreversibility.

    Args:
        value: Any cell value. ``None`` hashes to a fixed marker rather than to a digest.
        prefix_length: Hex characters of digest to keep, clamped to 4..64.

    Returns:
        ``"sha256:<hex>"``, or ``"sha256:null"`` for ``None``.
    """
    if value is None:
        return f"{HASH_PREFIX}null"
    length = max(_MIN_PREFIX_LENGTH, min(_MAX_PREFIX_LENGTH, prefix_length))
    digest = hashlib.sha256(str(value).encode("utf-8", errors="replace")).hexdigest()
    return f"{HASH_PREFIX}{digest[:length]}"
