"""Weighted substring search over a knowledge pack.

**Only needed once a pack stops fitting.** The default is to include everything (see
:mod:`kedge.knowledge.loader`), because retrieval that is not needed introduces the worst
failure mode available: the right fact existing and not being retrieved. This module is what
to reach for when a pack outgrows the context budget, and not before.

When that day comes, this is the scorer to use rather than an embedding index. It is the
same weighted substring scorer the sibling projects use -- pure Python, no dependency, no
model, no index to build or invalidate, and entirely predictable, which matters because a
user who cannot find a table they know they documented needs to be able to work out why.

The shape is deliberately preserved from those projects, and each property is load-bearing:

- **Integer field weights**, name 3 / child-name 2 / description 1. A query that matches a
  table's name outranks one that matches a sentence in its description, which is what a
  reader expects and what a float-weighted scheme makes harder to reason about.
- **``elif`` between a child's name and its description**, so one column contributes at most
  once. Without it, a column whose name also appears in its own description scores twice and
  a verbose entry beats a relevant one.
- **Zero-score records are dropped**, never returned as weak matches.
- **A deterministic tie-break on a stable key**, so equal scores come back in the same order
  every time and the result does not churn between runs.
- **Case-insensitive, no stemming, no state, no dependencies.**

It is a substring matcher, not a search engine. "exposure" finds
``counterparty_exposure``; "exposures" does not. That is the trade for having no index and
no surprises.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from kedge.knowledge.loader import Document, Table

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_LIMIT", "search_documents", "search_tables"]

DEFAULT_LIMIT = 8
"""How many results a search returns unless asked otherwise."""

_NAME_WEIGHT = 3
_CHILD_WEIGHT = 2
_DESCRIPTION_WEIGHT = 1


def search_tables(tables: Sequence[Table], query: str, limit: int = DEFAULT_LIMIT) -> list[Table]:
    """Return the tables matching a query, most relevant first.

    Substring match over table and column names and descriptions. An empty or whitespace-only
    query returns nothing rather than everything: "no query" is not a request for the whole
    catalogue.

    Args:
        tables: The tables to search.
        query: The search text; matched case-insensitively as a substring.
        limit: Maximum results.

    Returns:
        The matching tables, ordered by descending score then by qualified name.
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    scored: list[tuple[int, Table]] = []
    for table in tables:
        score = 0
        if q in table.name.lower() or q in table.qualified.lower():
            score += _NAME_WEIGHT
        if q in (table.description or "").lower():
            score += _DESCRIPTION_WEIGHT
        for column in table.columns:
            if q in column.name.lower():
                score += _CHILD_WEIGHT
            elif q in (column.description or "").lower():
                score += _DESCRIPTION_WEIGHT
        if score:
            scored.append((score, table))
    scored.sort(key=lambda s: (-s[0], s[1].qualified))
    return [table for _, table in scored[:limit]]


def search_documents(
    documents: Sequence[Document], query: str, limit: int = DEFAULT_LIMIT
) -> list[Document]:
    """Return the prose documents matching a query, most relevant first.

    The same scorer over the other half of a pack: title in place of table name, headings in
    place of columns, body text in place of descriptions.

    Args:
        documents: The documents to search.
        query: The search text; matched case-insensitively as a substring.
        limit: Maximum results.

    Returns:
        The matching documents, ordered by descending score then by key.
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    scored: list[tuple[int, Document]] = []
    for document in documents:
        score = 0
        if q in document.key.lower() or q in document.title.lower():
            score += _NAME_WEIGHT
        for line in document.text.splitlines():
            if not line.startswith("#"):
                continue
            if q in line.lower():
                score += _CHILD_WEIGHT
                break
        if q in document.text.lower():
            score += _DESCRIPTION_WEIGHT
        if score:
            scored.append((score, document))
    scored.sort(key=lambda s: (-s[0], s[1].key))
    return [document for _, document in scored[:limit]]
