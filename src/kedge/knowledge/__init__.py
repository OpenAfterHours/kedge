"""Knowledge packs and the reusable-utility catalogue — the context the model is given.

Two independent sources, both deliberately cheap to keep in every turn:

* A :class:`~kedge.knowledge.loader.KnowledgePack` loaded from ``context/`` — prose conventions,
  a glossary, and documented warehouse tables. The grain and gotchas recorded there are the
  things that are nowhere in a schema and that silently multiply a join.
* A :class:`~kedge.knowledge.utils_catalogue.UtilsCatalogue` built by walking ``utils/`` with
  ``ast`` — name, signature and first docstring line only. Compact enough to always include, so
  the model reuses a function instead of reinventing it.

Both degrade to empty rather than raising. A project that has not written a knowledge pack is not
broken, and neither is one whose ``utils/`` is still empty.

References:
- PLAN.md §M5.
"""

from __future__ import annotations

from kedge.knowledge.loader import (
    DATABASES_DIRNAME,
    Column,
    Document,
    KnowledgePack,
    Table,
    get_knowledge,
    load_pack,
)
from kedge.knowledge.search import DEFAULT_LIMIT, search_documents, search_tables
from kedge.knowledge.utils_catalogue import UtilFunction, UtilsCatalogue, build_catalogue

__all__ = [
    "DATABASES_DIRNAME",
    "DEFAULT_LIMIT",
    "Column",
    "Document",
    "KnowledgePack",
    "Table",
    "UtilFunction",
    "UtilsCatalogue",
    "build_catalogue",
    "get_knowledge",
    "load_pack",
    "search_documents",
    "search_tables",
]
