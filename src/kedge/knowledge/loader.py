"""Load the user-editable knowledge packs in ``context/``.

A knowledge pack is what the agent knows about *this* organisation rather than about Excel:
which warehouse tables exist, what grain they are at, which columns join to which, and the
gotchas that everyone who has used the table before already knows. Plus prose --
``conventions.md`` and ``glossary.md`` -- for the things a schema cannot express.

::

    context/
      conventions.md          # how converted processes are expected to be written
      glossary.md             # what the abbreviations mean
      databases/
        example.yaml          # tables, columns, grain, join keys, gotchas

**Everything is included by default.** A pack that fits in the context window needs no
retrieval, and retrieval that is not needed is a source of the worst failure mode available:
the right fact existing and not being retrieved. :func:`kedge.knowledge.search` is there for
when a pack outgrows the budget, and not before.

**Nothing here raises past its own boundary.** A malformed YAML file is logged and skipped;
the pack loads without it. A knowledge pack is hand-edited by definition, and a stray tab
character in one file must not stop the agent knowing about the other eleven.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

__all__ = [
    "DATABASES_DIRNAME",
    "Column",
    "Document",
    "KnowledgePack",
    "Table",
    "get_knowledge",
    "load_pack",
]

DATABASES_DIRNAME = "databases"

_YAML_SUFFIXES = (".yaml", ".yml")


# =============================================================================
# RECORDS
# =============================================================================


@dataclass(frozen=True, slots=True)
class Column:
    """One column of a documented table."""

    name: str
    dtype: str | None = None
    description: str | None = None
    is_key: bool = False

    def render(self) -> str:
        """Return a compact one-line rendering for the model's context."""
        parts = [self.name]
        if self.dtype:
            parts.append(f"({self.dtype})")
        if self.is_key:
            parts.append("[key]")
        if self.description:
            parts.append(f"- {self.description}")
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class Table:
    """A documented warehouse table.

    ``grain`` and ``gotchas`` earn their place in a way a column list does not. Knowing that
    a table is one row per counterparty *per reporting date* is what stops a join producing
    twelve times the rows anybody expected, and that fact is nowhere in the schema.
    """

    name: str
    database: str = ""
    schema: str | None = None
    description: str | None = None
    grain: str | None = None
    join_keys: tuple[str, ...] = ()
    gotchas: tuple[str, ...] = ()
    columns: tuple[Column, ...] = ()

    @property
    def qualified(self) -> str:
        """The fully qualified name, e.g. ``'risk_warehouse.dbo.counterparty_exposure'``."""
        return ".".join(p for p in (self.database, self.schema, self.name) if p)

    def column(self, name: str) -> Column | None:
        """Return the named column, or ``None``."""
        return next((c for c in self.columns if c.name == name), None)

    def render(self) -> str:
        """Return the table as text for the model's context."""
        lines = [f"## {self.qualified}"]
        if self.description:
            lines.append(self.description)
        if self.grain:
            lines.append(f"Grain: {self.grain}")
        if self.join_keys:
            lines.append(f"Join keys: {', '.join(self.join_keys)}")
        if self.columns:
            lines.append("Columns:")
            lines += [f"  - {c.render()}" for c in self.columns]
        if self.gotchas:
            lines.append("Gotchas:")
            lines += [f"  - {g}" for g in self.gotchas]
        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class Document:
    """One prose knowledge file, loaded whole."""

    key: str
    title: str
    path: Path
    text: str

    def render(self) -> str:
        """Return the document's text, unchanged."""
        return self.text


@dataclass(frozen=True, slots=True)
class KnowledgePack:
    """Everything in ``context/``, loaded.

    Example:
        >>> pack = load_pack(Path("context"))
        >>> pack.keys()
        ['conventions', 'glossary']
        >>> [t.qualified for t in pack.search("exposure")]
        ['risk_warehouse.dbo.counterparty_exposure']
    """

    root: Path
    documents: tuple[Document, ...] = ()
    tables: tuple[Table, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        """Whether the pack holds nothing at all."""
        return not self.documents and not self.tables

    def keys(self) -> list[str]:
        """Return every document key, for the agent's ``get_knowledge`` tool."""
        return [d.key for d in self.documents]

    def document(self, key: str) -> Document | None:
        """Return a document by key, or ``None``."""
        return next((d for d in self.documents if d.key == key), None)

    def get(self, key: str) -> str | None:
        """Return the text of one document, or ``None`` if there is no such key."""
        document = self.document(key)
        return None if document is None else document.text

    def table(self, name: str) -> Table | None:
        """Return a table by bare or qualified name, or ``None``."""
        return next((t for t in self.tables if name in (t.name, t.qualified)), None)

    def search(self, query: str, limit: int = 8) -> list[Table]:
        """Return the tables most relevant to a query. See :mod:`kedge.knowledge.search`."""
        from kedge.knowledge.search import search_tables

        return search_tables(self.tables, query, limit=limit)

    def summary(self) -> dict[str, Any]:
        """Return a compact digest: what is in the pack, without its contents."""
        return {
            "root": str(self.root),
            "documents": self.keys(),
            "table_count": len(self.tables),
            "tables": [t.qualified for t in self.tables],
        }

    def render(self) -> str:
        """Return the whole pack as one block of text.

        Everything, deliberately. Retrieval is only worth its risk once a pack no longer
        fits, and this is what "start by including everything" means in practice.
        """
        blocks = [d.text.strip() for d in self.documents if d.text.strip()]
        if self.tables:
            blocks.append("# Database reference\n\n" + "\n\n".join(t.render() for t in self.tables))
        return "\n\n---\n\n".join(blocks)


# =============================================================================
# LOADING
# =============================================================================


def _load_document(path: Path) -> Document | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("ignoring unreadable knowledge document %s: %s", path, exc)
        return None
    heading = next(
        (line[2:].strip() for line in text.splitlines() if line.startswith("# ")), path.stem
    )
    return Document(key=path.stem, title=heading, path=path, text=text)


def _as_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value)
    return ()


def _parse_column(raw: Any) -> Column | None:
    if isinstance(raw, str):
        return Column(name=raw)
    if not isinstance(raw, dict) or not raw.get("name"):
        return None
    return Column(
        name=str(raw["name"]),
        dtype=_as_str(raw.get("dtype")),
        description=_as_str(raw.get("description")),
        is_key=bool(raw.get("key", raw.get("is_key", False))),
    )


def _parse_table(raw: Any, *, database: str, source: Path) -> Table | None:
    if not isinstance(raw, dict) or not raw.get("name"):
        logger.warning("ignoring a table entry with no name in %s", source)
        return None
    columns = [c for c in (_parse_column(item) for item in raw.get("columns") or []) if c]
    return Table(
        name=str(raw["name"]),
        database=str(raw.get("database", database) or ""),
        schema=_as_str(raw.get("schema")),
        description=_as_str(raw.get("description")),
        grain=_as_str(raw.get("grain")),
        join_keys=_as_tuple(raw.get("join_keys")),
        gotchas=_as_tuple(raw.get("gotchas")),
        columns=tuple(columns),
    )


def _load_database_file(path: Path) -> list[Table]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        # A hand-edited pack must degrade rather than break: one bad file, not no pack.
        logger.warning("ignoring unreadable database pack %s: %s", path, exc)
        return []
    if not isinstance(raw, dict):
        logger.warning("ignoring database pack %s: expected a YAML mapping", path)
        return []
    database = str(raw.get("database", path.stem))
    entries = raw.get("tables") or []
    if not isinstance(entries, list):
        logger.warning("ignoring database pack %s: 'tables' is not a list", path)
        return []
    return [t for t in (_parse_table(e, database=database, source=path) for e in entries) if t]


def load_pack(root: Path) -> KnowledgePack:
    """Load every knowledge file under ``root``.

    Args:
        root: The pack directory, normally ``context/``. A directory that does not exist
            yields an empty pack rather than an error -- knowledge packs are optional, and a
            project that has not written one yet is not broken.

    Returns:
        The loaded :class:`KnowledgePack`. Documents are ordered by key and tables by
        qualified name, so the rendered context is byte-identical between runs and does not
        churn a prompt cache.
    """
    if not root.is_dir():
        logger.debug("no knowledge pack at %s", root)
        return KnowledgePack(root=root)

    documents = [d for d in (_load_document(p) for p in sorted(root.glob("*.md"))) if d]

    tables: list[Table] = []
    databases_dir = root / DATABASES_DIRNAME
    if databases_dir.is_dir():
        for suffix in _YAML_SUFFIXES:
            for path in sorted(databases_dir.glob(f"*{suffix}")):
                tables.extend(_load_database_file(path))

    logger.info(
        "loaded knowledge pack %s: %d document(s), %d table(s)", root, len(documents), len(tables)
    )
    return KnowledgePack(
        root=root,
        documents=tuple(sorted(documents, key=lambda d: d.key)),
        tables=tuple(sorted(tables, key=lambda t: t.qualified)),
    )


def get_knowledge(pack: KnowledgePack, key: str | None = None) -> str:
    """Return knowledge for the agent's ``get_knowledge`` tool.

    Args:
        pack: The loaded pack.
        key: A document key, a table name, or ``None`` for everything.

    Returns:
        The requested text. An unknown key returns a message naming what *is* available,
        rather than an empty string the model would read as "there is nothing to know".
    """
    if key is None:
        return pack.render()
    document = pack.document(key)
    if document is not None:
        return document.text
    table = pack.table(key)
    if table is not None:
        return table.render()
    available = ", ".join([*pack.keys(), *(t.qualified for t in pack.tables)]) or "nothing"
    return f"no knowledge is filed under '{key}'. Available keys: {available}."
