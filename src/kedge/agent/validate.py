"""The pre-commit validation gate: five stages, in order, before anything reaches the kernel.

PLAN M4 fixes both the stages and their order, and the order is the design. Syntax first, because
nothing downstream can read a tree that does not exist. The marimo contract second, checked
against the *live* graph, because a single-definition collision is the failure mode this project
will hit most often and catching it here saves a round trip and a confused model. Policy third,
because a cell that shells out is a different kind of problem from one that is merely wrong.
Output style last, because it is the only stage where the code would have worked — it would just
have been pandas, or silently rounded the wrong way.

Each stage short-circuits. There is no value in telling a model that its pandas import is against
the house style when the cell does not parse; the first message it can act on is the only one
worth sending.

Nothing here raises on a violation. The gate returns a :class:`ValidationReport`, the loop turns
it into a :class:`~kedge.turn.ValidationEvent` and a tool result, and the model gets to
try again — three times, then kedge stops and tells the user (:data:`MAX_VALIDATION_ATTEMPTS`).
An exception would be the wrong shape entirely: a rejected cell is a normal event in a
conversation, not an error in kedge.

The name analysis is deliberately approximate in the safe direction. marimo does proper scope
resolution kernel-side; this module over-collects bindings and under-collects references, so the
pre-flight check produces false *passes* rather than false rejections. A false pass costs one
round trip and marimo reports it precisely; a false rejection would make the gate something the
model learns to fight.

One check sits beside the five rather than among them. :func:`undefined_name` asks whether a body
defines the name the cells below it read, which the kernel would otherwise say -- three cells
later, blaming a cell that was written correctly. ``kedge convert`` has no kernel, so it asks
here. It is not a :class:`ValidationStage` and :func:`validate_cell` does not run it; the caller
that needs it composes it, and it lives here so the next caller that needs it finds it rather than
writing a third copy.
"""

from __future__ import annotations

import ast
import builtins
import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from kedge.notebook.driver import parse_definition_conflicts

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence

    from kedge.agent.context import NameRegistry
    from kedge.analysis.model import WorkbookAnalysis
    from kedge.notebook.model import DefinitionConflict

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_VALIDATION_ATTEMPTS",
    "MISSING_NAME_STAGE",
    "CellNames",
    "Policy",
    "RoundingContext",
    "ValidationReport",
    "ValidationStage",
    "Violation",
    "extract_names",
    "undefined_name",
    "validate_cell",
    "violations_from_conflicts",
    "violations_from_kernel_error",
]

MAX_VALIDATION_ATTEMPTS = 3
"""How many times one cell may be resubmitted before kedge stops and asks the user (PLAN M4)."""

_BUILTINS = frozenset(dir(builtins))

_ROUND_FUNCTIONS = frozenset({"ROUND", "ROUNDUP", "ROUNDDOWN", "MROUND"})

_SHELL_CALLS = frozenset(
    {
        "os.system",
        "os.popen",
        "os.execv",
        "os.execvp",
        "os.spawnl",
        "os.spawnv",
        "subprocess.run",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.Popen",
        "subprocess.getoutput",
        "subprocess.getstatusoutput",
        "pty.spawn",
        "commands.getoutput",
    }
)

_SHELL_MODULES = frozenset({"subprocess", "pty", "commands"})

_NETWORK_MODULES = frozenset(
    {
        "aiohttp",
        "ftplib",
        "http",
        "httpx",
        "paramiko",
        "requests",
        "smtplib",
        "socket",
        "telnetlib",
        "urllib",
        "urllib3",
        "websockets",
    }
)

_DATABASE_MODULES = frozenset(
    {
        "MySQLdb",
        "aioodbc",
        "asyncpg",
        "clickhouse_connect",
        "connectorx",
        "cx_Oracle",
        "ibm_db",
        "ibm_db_dbi",
        "mariadb",
        "mysql",
        "oracledb",
        "psycopg",
        "psycopg2",
        "pyhive",
        "pymssql",
        "pymysql",
        "pyodbc",
        "redshift_connector",
        "snowflake",
        "teradatasql",
        "turbodbc",
        "vertica_python",
    }
)
"""Drivers whose only purpose is to open a connection to a database server.

Importing one is the whole signal: there is nothing else to do with ``pyodbc``. These are absent
from :data:`_NETWORK_MODULES` on purpose — the refusal a database connection needs says something
different from the one an HTTP fetch needs, because a DSN is not a URL.

The list will never be complete, which is why :func:`_check_database` also treats a literal that
*is* a connection string as a signal in its own right: an unlisted driver handed a
``postgresql://warehouse.internal/risk`` is still a connection to a warehouse."""

_DATABASE_MODULE_PREFIXES = ("adbc_driver_",)
"""ADBC ships one distribution per backend — ``adbc_driver_postgresql``,
``adbc_driver_snowflake`` — so the family is matched by prefix rather than enumerated and left to
go stale as backends are added."""

_DUAL_USE_DATABASE_MODULES = frozenset({"duckdb", "sqlalchemy"})
"""Modules that reach a server only sometimes.

``duckdb`` over a local parquet file is not network access at all, and ``sqlalchemy`` is imported
by plenty of code that never connects, so importing these is not enough: they count only where a
*call* on them shows a connection — a server-shaped DSN in its own arguments, or a database entry
point. The trade is deliberate and it buys a false pass — a MotherDuck ``md:`` connection names no
recognised scheme and goes through, as does ``duckdb`` reading an ``https://`` parquet file."""

_POLARS_DATABASE_CALLS = frozenset({"read_database", "read_database_uri"})
"""polars' own database readers. Module functions, so the receiver is ``pl`` or ``polars``."""

_FRAME_DATABASE_CALLS = frozenset({"write_database"})
"""``DataFrame.write_database``: a *method*, so there is no module receiver to require — the
receiver is whatever the frame is called. Matched on the tail alone for that reason, and it is the
one entry point where that trade is clearly worth it: a write to a warehouse is the failure in
this module a reviewer cannot undo."""

_ENGINE_CALLS = frozenset({"create_engine"})
"""``sqlalchemy.create_engine``, recognised on ``sqlalchemy.create_engine(...)`` or on a bare
``create_engine(...)`` this cell imported from sqlalchemy itself."""

_DATABASE_CALLS = _POLARS_DATABASE_CALLS | _FRAME_DATABASE_CALLS | _ENGINE_CALLS
"""Database entry points that import nothing suspicious, so the call is the only thing to match.

``pl.read_database_uri(...)`` hands the URI to connectorx *inside* polars, and a cell that reads
or writes a warehouse through it imports only ``polars``. ``create_engine`` is here from the other
direction: importing ``sqlalchemy`` proves nothing, but calling it is unambiguous.

Each is matched with its receiver root rather than on the tail alone, which is what keeps
``handins.read_database("rates_2024")`` and ``create_engine("mpl")`` out of the gate's way. The
receiver is not resolved through this cell's alias map — under marimo's single-definition rule the
``import polars as pl`` lives in another cell — so ``pl`` and ``polars`` are both accepted as
written. The cost is a false pass on ``sa.create_engine(...)`` where the alias is bound elsewhere,
and a false pass is the direction this module errs in.

Every name here is also a valid ``[policy] database_allowlist`` entry: the two-cell shape marimo
forces — the engine in one cell, the read in the next — leaves the reading cell with nothing but
the entry point to name."""

_POLARS_ROOTS = frozenset({"pl", "polars"})

_WRITE_CALLS = frozenset(
    {
        "open",
        "write_text",
        "write_bytes",
        "write_csv",
        "write_parquet",
        "write_ipc",
        "write_json",
        "write_ndjson",
        "write_excel",
        "write_delta",
        "write_avro",
        "sink_csv",
        "sink_parquet",
        "sink_ipc",
        "sink_ndjson",
        "mkdir",
        "touch",
        "unlink",
        "rmdir",
        "to_csv",
    }
)
"""Calls that write. ``rename`` and ``replace`` are deliberately absent: ``str.replace`` and
``DataFrame.rename`` share those names, and a gate that rejects a legitimate cell teaches the model
to fight it. Missing a rare ``Path.rename`` costs less than that."""

_DESTRUCTIVE_CALLS = frozenset(
    {"shutil.rmtree", "os.remove", "os.unlink", "os.rmdir", "os.removedirs"}
)

_ROW_ITERATORS = frozenset(
    {"iter_rows", "rows", "to_dicts", "iterrows", "itertuples", "iter_slices", "to_dict"}
)

_CREDENTIAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "an API key"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "an AWS access key id"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "a private key"),
    (
        re.compile(
            r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|bearer)\b"
            r"\s*[=:]\s*['\"]?[^\s'\"]{6,}"
        ),
        "a credential",
    ),
    (
        # Any `scheme://user:password@host`, and the scheme may carry a SQLAlchemy driver suffix.
        # Naming the schemes and anchoring on `\b` was the earlier spelling and it had a hole
        # exactly where it mattered: there is no word boundary inside "pyodbc", so
        # `mssql+pyodbc://user:pass@host` — the tidiest way to write the worst literal in this
        # file — matched nothing, while the same URI assigned to `connection_string` was caught by
        # _CREDENTIAL_NAMES. The scheme is not the interesting part; the `:secret@` is.
        re.compile(
            r"(?i)(?<![A-Za-z0-9])[a-z][a-z0-9]*(?:\+[a-z0-9_]+)?://[^\s'\"/@]*:[^\s'\"/@]+@"
        ),
        "a connection string with an embedded password",
    ),
)

_CREDENTIAL_NAMES = re.compile(
    r"(?i)^_*(?:api_?key|apikey|secret|password|passwd|pwd|token|access_?token|"
    r"client_?secret|private_?key|conn(?:ection)?_?string)$"
)

_DSN_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:jdbc|odbc|postgres(?:ql)?|mysql|mariadb|mssql|sqlserver|oracle|"
    r"snowflake|databricks|clickhouse|redshift|trino|presto|hive|mongodb|bigquery|athena|"
    r"teradata|db2|vertica|cockroachdb)(?:\+[a-z0-9_]+)?://(?P<authority>[^\s'\"/?#]*)"
)
"""A connection string that names a *server*, with its ``user:password@host:port`` authority.

``sqlite://`` and ``duckdb://`` are deliberately absent: they name a local file, and treating them
as connections is what would make the gate reject a duckdb cell reading a parquet file off the
disk beside the notebook."""

_ODBC_DSN_PATTERN = re.compile(r"(?i)(?:\A|;)\s*(?:server|dsn|host|account)\s*=\s*([^;\s]+)")
"""The other spelling of a connection target: ``DRIVER={...};SERVER=warehouse.internal;...``.

Also matches a libpq keyword string (``host=warehouse dbname=risk``), since the key is at the
start there. A ``DSN=`` name is not a hostname and never will be, which is the whole reason
:attr:`Policy.database_allowlist` is a separate list from :attr:`Policy.network_allowlist`.

The captured value is filtered by :func:`_odbc_target` rather than trusted, because this pattern
also fires inside SQL: ``select 1 where c = 'a;server=prod'`` reaches ``;server=prod'``, and a
refusal telling the user to allowlist ``"prod'"`` is worse than no refusal at all."""

_HOST_SHAPED = re.compile(r"\A[A-Za-z0-9_][A-Za-z0-9._\-]*\Z")
"""What an ODBC value has to look like before kedge will call it a connection target: a hostname,
a DSN entry name or an account locator, and nothing that has come out of a SQL string by
accident."""

_CONNECTION_STRING_HEAD = re.compile(
    r"(?i)\A\s*(?:[a-z][a-z0-9]*(?:\+[a-z0-9_]+)?://|[a-z_]\w*\s*=)"
)
"""Whether a literal *is* a connection string rather than prose that mentions one.

Only literals that begin with a scheme or a keyword pair count when there is no recognised call to
attribute them to (:func:`_unattributed_connection_strings`). A sentence that names the system
being replaced — ``"mirrors the query that ran against postgresql://warehouse.internal/risk"`` —
is what good conversion work looks like, and refusing it teaches the model to stop writing it."""


# ── vocabulary ───────────────────────────────────────────────────────────────────────────────


class ValidationStage(StrEnum):
    """Which stage rejected the cell. Stages run in this order and short-circuit."""

    SYNTAX = "syntax"
    MARIMO = "marimo"
    POLICY = "policy"
    STYLE = "style"


@dataclass(frozen=True, slots=True)
class Violation:
    """One reason a cell was rejected, phrased as something the model can act on.

    Example:
        >>> Violation(stage=ValidationStage.STYLE, message="import pandas is not permitted").render()
        'style: import pandas is not permitted'
    """

    stage: ValidationStage
    message: str
    line: int | None = None

    def render(self) -> str:
        """The one-line form shown to the model and streamed as a validation event."""
        where = f" (line {self.line})" if self.line else ""
        return f"{self.stage.value}: {self.message}{where}"


@dataclass(frozen=True, slots=True)
class CellNames:
    """What a cell defines and what it reads, as marimo will see it."""

    defs: tuple[str, ...] = ()
    refs: tuple[str, ...] = ()

    @property
    def public_defs(self) -> tuple[str, ...]:
        """Definitions subject to the single-definition rule — underscore names are exempt."""
        return tuple(name for name in self.defs if not name.startswith("_"))


def _allowlisted(target: str, allowed: frozenset[str]) -> bool:
    """Whether ``target`` is on ``allowed``, exactly or as a child of an allowed parent."""
    needle = target.lower().strip()
    return any(needle == entry.lower() or needle.endswith("." + entry.lower()) for entry in allowed)


@dataclass(frozen=True, slots=True)
class Policy:
    """What generated code is allowed to reach for.

    ``working_dir`` bounds writes. The two allowlists are empty by default, which means no network
    and no database at all — a conversion notebook reads a workbook and a hand-in from disk, and
    anything else is a question for the user rather than a default. Both are populated from
    ``[policy]`` in config (:class:`kedge.config.PolicyConfig`); until that section exists on the
    machine, the default *is* the whole policy.

    They are separate lists because they answer different questions. ``network_allowlist`` holds
    hostnames and is matched against what :func:`urllib.parse.urlparse` finds in an ``http(s)://``
    literal. ``database_allowlist`` is matched against whatever a connection names, which is often
    not a hostname at all: an ODBC ``DSN=RiskWarehouse`` entry, a Snowflake account locator, or —
    where the DSN is assembled at run time and there is nothing in the cell to read — the driver
    module itself. One list matching both would have to pretend those are the same kind of name.

    Example:
        >>> Policy(network_allowlist=frozenset({"internal.example"})).allows_host("internal.example")
        True
        >>> Policy(database_allowlist=frozenset({"pyodbc"})).permits_database("pyodbc")
        True
    """

    working_dir: Path | None = None
    network_allowlist: frozenset[str] = frozenset()
    database_allowlist: frozenset[str] = frozenset()
    allow_pandas: bool = False
    """The explicit escape hatch PLAN 2.5 asks for. Off, and expected to stay off."""

    def allows_host(self, host: str) -> bool:
        """Whether ``host`` is on the allowlist, matching subdomains of an allowed domain."""
        return _allowlisted(host, self.network_allowlist)

    def permits_database(self, target: str) -> bool:
        """Whether a connection target, driver module or entry point is allowlisted.

        Matched the same way a hostname is, so an entry of ``internal.bank`` also permits
        ``warehouse.internal.bank``. A DSN name has no parent domain, so for one of those this is
        exact-match with the case folded.

        Four kinds of name reach here, and all four are things a user writes in
        ``database_allowlist``: a host (``warehouse.internal``), a DSN entry or account locator
        (``riskwarehouse``), a driver module (``pyodbc``) and an entry point
        (``read_database``, ``write_database``, ``create_engine``). The last exists because
        marimo's single-definition rule splits the ordinary workflow across two cells — the engine
        in one, the read in the next — and the reading cell contains no host, no driver import and
        nothing else a user could name.
        """
        return _allowlisted(target, self.database_allowlist)

    def permits_write(self, target: str) -> bool:
        """Whether a literal write path is inside the working directory.

        A relative path is always permitted: it resolves against the kernel's working directory,
        which *is* the working directory. An absolute path is permitted only when it sits under
        ``working_dir``, and when no working directory is configured every absolute path is
        refused — "somewhere on this machine" is not a place a generated notebook should write to.
        """
        path = Path(target)
        if not path.is_absolute() and not target.startswith("\\\\"):
            return ".." not in path.parts
        if self.working_dir is None:
            return False
        try:
            return path.resolve().is_relative_to(self.working_dir.resolve())
        except OSError:
            return False


@dataclass(frozen=True, slots=True)
class RoundingContext:
    """Where a bare ``.round()`` means the translation is wrong.

    PLAN 2.6 asks for a bare ``.round()`` to be rejected *on a value derived from an Excel ROUND
    region*, which needs a definition of "derived". Three answers are accepted, cheapest first: the
    receiver chain roots at a notebook name known to carry rounded values; a column name from a
    rounded region appears anywhere in the expression; or ``strict`` is set.

    ``strict`` is what :meth:`from_analysis` turns on when the workbook contains any rounding
    function at all. At that point every rounding in the notebook is suspect — the workbook has
    established that its numbers are rounded Excel's way, and polars' banker's rounding will
    disagree with it somewhere. Where the workbook contains no ROUND at all, nothing is flagged.

    Example:
        >>> RoundingContext(columns=frozenset({"haircut"})).columns
        frozenset({'haircut'})
    """

    names: frozenset[str] = frozenset()
    columns: frozenset[str] = frozenset()
    strict: bool = False

    @classmethod
    def from_analysis(cls, analysis: WorkbookAnalysis | None) -> RoundingContext:
        """Derive the rounding context from a workbook analysis."""
        if analysis is None:
            return cls()
        rounded = [
            operation
            for operation in analysis.operations
            if _ROUND_FUNCTIONS & {str(name).upper() for name in operation.functions}
        ]
        if not rounded:
            return cls()
        anchors = {(operation.sheet, _column_of(operation.anchor)) for operation in rounded}
        columns = {
            profile.header.strip().lower()
            for profile in analysis.profiles
            if profile.header and (profile.sheet, profile.column) in anchors
        }
        logger.debug("rounding context: %d rounded regions, %d columns", len(rounded), len(columns))
        return cls(columns=frozenset(columns), strict=True)


def _column_of(anchor: str) -> str:
    return "".join(character for character in anchor if character.isalpha()).upper()


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """The gate's verdict on one cell.

    Example:
        >>> ValidationReport().ok
        True
    """

    violations: tuple[Violation, ...] = ()
    names: CellNames = field(default_factory=CellNames)
    stage: ValidationStage | None = None
    """The stage that rejected the cell, or ``None`` when nothing did."""

    @property
    def ok(self) -> bool:
        """Whether the cell may be submitted."""
        return not self.violations

    @property
    def messages(self) -> tuple[str, ...]:
        """The violations rendered one per line, for the event stream and the tool result."""
        return tuple(violation.render() for violation in self.violations)

    def render(self) -> str:
        """The block returned to the model when the gate rejects a cell."""
        if self.ok:
            return "The validation gate accepted this cell."
        lines = [
            f"The validation gate rejected this cell at the {self.stage} stage. "
            f"Fix the cause and resubmit; you have a limited number of attempts.",
            "",
        ]
        lines += [f"  - {message}" for message in self.messages]
        return "\n".join(lines)


# ── name extraction ──────────────────────────────────────────────────────────────────────────


def _target_names(node: ast.expr) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Tuple | ast.List):
        return [name for element in node.elts for name in _target_names(element)]
    if isinstance(node, ast.Starred):
        return _target_names(node.value)
    return []


_NEVER_MODULE_SCOPE = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _module_statements(body: Sequence[ast.stmt]) -> Iterator[ast.stmt]:
    """Yield every statement that executes in module scope, function bodies excluded."""
    for node in body:
        yield node
        if isinstance(node, _NEVER_MODULE_SCOPE):
            continue
        for attribute in ("body", "orelse", "finalbody"):
            inner = getattr(node, attribute, None)
            if isinstance(inner, list):
                yield from _module_statements(inner)
        for handler in getattr(node, "handlers", None) or ():
            yield from _module_statements(handler.body)
        for case in getattr(node, "cases", None) or ():
            yield from _module_statements(case.body)


def _module_defs(tree: ast.Module) -> list[str]:
    found: list[str] = []
    for node in _module_statements(tree.body):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                found += _target_names(target)
        elif isinstance(node, ast.AnnAssign | ast.AugAssign):
            found += _target_names(node.target)
        elif isinstance(node, _NEVER_MODULE_SCOPE):
            found.append(node.name)
        elif isinstance(node, ast.Import):
            found += [alias.asname or alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            found += [alias.asname or alias.name for alias in node.names if alias.name != "*"]
        elif isinstance(node, ast.For | ast.AsyncFor):
            found += _target_names(node.target)
        elif isinstance(node, ast.With | ast.AsyncWith):
            for item in node.items:
                if item.optional_vars is not None:
                    found += _target_names(item.optional_vars)
        if not isinstance(node, _NEVER_MODULE_SCOPE):
            for inner in ast.walk(node):
                if isinstance(inner, ast.NamedExpr):
                    found += _target_names(inner.target)
    return found


def _all_bound(tree: ast.Module) -> set[str]:
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del):
            bound.add(node.id)
        elif isinstance(node, _NEVER_MODULE_SCOPE):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.Import):
            bound.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            bound.update(alias.asname or alias.name for alias in node.names if alias.name != "*")
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.Global | ast.Nonlocal):
            bound.update(node.names)
    return bound


def extract_names(tree: ast.Module) -> CellNames:
    """Return the public names a cell defines and the names it reads from elsewhere.

    Definitions are module-scope bindings only, which is what marimo's single-definition rule
    applies to. References are every name loaded anywhere in the cell that the cell does not bind
    itself and that is not a builtin.

    Args:
        tree: A parsed cell body.

    Returns:
        The cell's definitions and references, each sorted and de-duplicated.
    """
    defs = sorted(set(_module_defs(tree)))
    bound = _all_bound(tree)
    refs = sorted(
        {
            node.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
        }
        - bound
        - _BUILTINS
    )
    return CellNames(defs=tuple(defs), refs=tuple(refs))


# ── helpers shared by the stages ─────────────────────────────────────────────────────────────


def _dotted(node: ast.expr) -> str:
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Call):
        current = current.func
        while isinstance(current, ast.Attribute):
            current = current.value
    if not isinstance(current, ast.Name):
        return ".".join(reversed(parts))
    parts.append(current.id)
    return ".".join(reversed(parts))


def _receiver_chain(node: ast.expr) -> str:
    """The dotted name a call is made *on*, or ``""`` when a call sits in the receiver chain.

    :func:`_dotted` looks through a chained call on purpose, so ``Path("x").write_text`` still
    reads as a write. For deciding what a call *is*, that is wrong in both directions:
    ``requests.get(url).json()`` is a call on a response object rather than on ``requests``, and
    ``pyodbc.connect(dsn).cursor()`` is one connection, not two.
    """
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return ""
    parts.append(current.id)
    return ".".join(reversed(parts))


def _call_name(node: ast.Call, aliases: dict[str, str]) -> tuple[str, str, str]:
    """A call's receiver-resolved name, split into ``(dotted, root, tail)``.

    Empty strings where the receiver is not a plain name chain, which reads as "not a call on
    anything this module recognises".
    """
    chain = _receiver_chain(node.func)
    if not chain:
        return "", "", ""
    head, _, rest = chain.partition(".")
    resolved = f"{aliases.get(head, head)}.{rest}" if rest else aliases.get(head, head)
    return resolved, resolved.partition(".")[0], resolved.rpartition(".")[2]


def _root_name(node: ast.expr) -> str:
    dotted = _dotted(node)
    return dotted.split(".", maxsplit=1)[0] if dotted else ""


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    """Map local aliases back to the module they name, so ``sp.run`` resolves to ``subprocess.run``."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{root}.{alias.name}"
    return aliases


def _resolved(node: ast.expr, aliases: dict[str, str]) -> str:
    dotted = _dotted(node)
    if not dotted:
        return ""
    head, _, tail = dotted.partition(".")
    resolved = aliases.get(head, head)
    return f"{resolved}.{tail}" if tail else resolved


def _imported_modules(tree: ast.Module) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return modules


_DOCSTRING_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _docstring_ids(tree: ast.AST) -> frozenset[int]:
    """``id()`` of every constant that is a module, class or function docstring."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, _DOCSTRING_OWNERS) or ast.get_docstring(node) is None:
            continue
        first = node.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            found.add(id(first.value))
    return frozenset(found)


def _string_constants(tree: ast.AST, *, prose: bool = True) -> Iterator[tuple[str, int]]:
    """Every string literal in ``tree``, optionally without the docstrings.

    ``prose=False`` drops module, class and function docstrings. The checks that infer *intent*
    from a literal want that: a cell whose docstring says which warehouse it replaces is the
    documentation this project asks for, not a connection to one. The credential check keeps
    them — a password in a docstring is still a password in source.
    """
    skip = frozenset() if prose else _docstring_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip:
            yield node.value, getattr(node, "lineno", 0)


# ── stage 2: the marimo contract ─────────────────────────────────────────────────────────────


def _check_marimo(
    tree: ast.Module, names: CellNames, registry: NameRegistry | None, cell: str | None
) -> list[Violation]:
    found: list[Violation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
            found.append(
                Violation(
                    stage=ValidationStage.MARIMO,
                    message=(
                        f"wildcard import `from {node.module or '.'} import *` is not permitted: "
                        f"marimo cannot tell which names the cell defines, so the cell can neither "
                        f"be placed in the dataflow graph nor checked against the name registry. "
                        f"Import the names you use explicitly."
                    ),
                    line=node.lineno,
                )
            )
    if registry is None:
        return found

    for name, owner in registry.conflicts(names.public_defs, exclude=cell):
        found.append(
            Violation(
                stage=ValidationStage.MARIMO,
                message=(
                    f"this cell defines '{name}', which is already defined by cell "
                    f"'{owner.label}' ({owner.id}). marimo requires exactly one owning cell per "
                    f"public name, imports included. Either edit '{owner.label}' instead of "
                    f"adding a cell, choose a different name, or make it cell-local by renaming "
                    f"it to '_{name}'."
                ),
            )
        )
    cycle = registry.would_cycle(names.public_defs, names.refs, exclude=cell)
    if cycle:
        found.append(
            Violation(
                stage=ValidationStage.MARIMO,
                message=(
                    f"this cell would create a dependency cycle with: {', '.join(cycle)}. It reads "
                    f"a name those cells define while defining a name they read. marimo derives "
                    f"the graph from names, not from cell order, so moving the cell will not help."
                ),
            )
        )
    return found


# ── stage 3: policy ──────────────────────────────────────────────────────────────────────────


def _check_policy(tree: ast.Module, policy: Policy) -> list[Violation]:
    aliases = _import_aliases(tree)
    found: list[Violation] = []
    found += _check_shell(tree, aliases)
    # Credentials before the two allowlist checks: a password in the source is the one thing here
    # the model must fix whatever the user has permitted, so it should be the first line it reads
    # when a DSN trips both.
    found += _check_credentials(tree)
    found += _check_network(tree, aliases, policy)
    found += _check_database(tree, aliases, policy)
    found += _check_writes(tree, aliases, policy)
    return found


def _check_shell(tree: ast.Module, aliases: dict[str, str]) -> list[Violation]:
    found: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        resolved = _resolved(node.func, aliases)
        if resolved in _SHELL_CALLS:
            found.append(
                Violation(
                    stage=ValidationStage.POLICY,
                    message=(
                        f"`{resolved}(...)` executes a shell command. A conversion notebook reads "
                        f"a workbook and writes a frame; it does not run programs. If you believe "
                        f"this process genuinely needs to, say so in prose and let the user decide."
                    ),
                    line=node.lineno,
                )
            )
        elif resolved in _DESTRUCTIVE_CALLS:
            found.append(
                Violation(
                    stage=ValidationStage.POLICY,
                    message=(
                        f"`{resolved}(...)` deletes files. Generated cells never remove anything "
                        f"from disk."
                    ),
                    line=node.lineno,
                )
            )
    for module in _imported_modules(tree) & _SHELL_MODULES:
        found.append(
            Violation(
                stage=ValidationStage.POLICY,
                message=(
                    f"importing `{module}` is not permitted in a generated cell: it exists to run "
                    f"other programs, and nothing in a workbook conversion needs to."
                ),
            )
        )
    return found


def _has_string_argument(node: ast.Call) -> bool:
    """Whether any argument of ``node`` contains a string literal anywhere inside it."""
    return any(
        isinstance(inner, ast.Constant) and isinstance(inner.value, str)
        for argument in (*node.args, *(keyword.value for keyword in node.keywords))
        for inner in ast.walk(argument)
    )


def _opaque_network_calls(tree: ast.Module, aliases: dict[str, str]) -> list[str]:
    """Calls on a network module that carry no literal at all, so name no destination.

    ``httpx.get(build_url())`` is the case. Without this the hosts below are scraped cell-wide,
    and one allowlisted URL literal anywhere in the cell would permit every other request in it —
    the check would be disarmed by exactly the configuration that exists to widen it.

    This is deliberately weaker than the per-call attribution :func:`_check_database` does. A URL
    is routinely composed — ``httpx.get(f"{BASE}/latest")``, ``requests.get(BASE + path)`` — and
    demanding that the *destination* be readable per call would reject that, while a DSN is handed
    to a connect call whole. So the rule here is only that a call naming nothing at all is not
    covered by a literal somewhere else; a call carrying a fragment still falls back to them.
    """
    opaque: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        resolved, root, _ = _call_name(node, aliases)
        if root in _NETWORK_MODULES and not _has_string_argument(node):
            opaque.append(f"{resolved}(...)")
    return sorted(set(opaque))


def _check_network(tree: ast.Module, aliases: dict[str, str], policy: Policy) -> list[Violation]:
    used = _imported_modules(tree) & _NETWORK_MODULES
    if not used:
        return []
    hosts = {
        parsed.hostname
        for text, _ in _string_constants(tree, prose=False)
        if (parsed := urlparse(text)).scheme in ("http", "https") and parsed.hostname
    }
    opaque = _opaque_network_calls(tree, aliases)
    if hosts and not opaque and all(policy.allows_host(host) for host in hosts):
        logger.debug("network use permitted for hosts %s", sorted(hosts))
        return []
    allowed = ", ".join(sorted(policy.network_allowlist)) or "nothing (the allowlist is empty)"
    if opaque:
        refused = (
            f"to an address kedge cannot read from the source — {', '.join(opaque)} names no "
            f"literal, and a URL allowlisted elsewhere in the cell does not cover it"
        )
    elif hosts:
        refused = f"to {', '.join(sorted(hosts))}"
    else:
        refused = "to an address kedge could not read statically"
    return [
        Violation(
            stage=ValidationStage.POLICY,
            message=(
                f"this cell reaches the network via `{', '.join(sorted(used))}`, {refused}. The "
                f"allowlist permits: {allowed}. A reproducible notebook takes its inputs from the "
                f"managed hand-in store, not from a live endpoint whose contents change between "
                f"runs. Load the data through the hand-in instead."
            ),
        )
    ]


@dataclass(frozen=True, slots=True)
class _Connection:
    """One place in a cell that opens, or names, a connection to a database server.

    Attribution is per site rather than per cell, and that is the whole design. Scraping targets
    cell-wide meant one permitted target disarmed the check for every other connection beside it:
    with ``database_allowlist = ["riskwarehouse"]``, a cell holding ``"SERVER=RiskWarehouse"``
    passed while calling ``pyodbc.connect(build_dsn())`` two lines later.
    """

    subject: str
    """What the refusal names, already back-quoted: ```pyodbc.connect(...)``` or ```pyodbc```."""

    permits: frozenset[str]
    """Allowlist entries that permit this site by *name* — the driver module, the entry point, or
    both. Empty for a bare connection string, where the target is the only thing to name."""

    targets: frozenset[str]
    """Connection targets read from this site's own literals. Empty means unreadable, not absent:
    a DSN assembled at run time leaves nothing in the source to match."""

    line: int | None = None

    def permitted(self, policy: Policy) -> bool:
        """Whether the allowlist covers this site, by target or by name.

        Two ways, because the two-cell shape marimo forces leaves some sites with no readable
        target at all. Naming the driver or the entry point is the blunter permission and reads
        that way in config; naming the target is the precise one.
        """
        if self.targets and all(policy.permits_database(target) for target in self.targets):
            return True
        return any(policy.permits_database(name) for name in self.permits)


def _odbc_target(raw: str) -> str | None:
    """The host-shaped value of an ODBC or libpq keyword, or ``None`` when it is not one.

    ``SERVER=tcp:warehouse.internal,1433`` is the SQL Server spelling of one host, and braces are
    ODBC's quoting. Everything else is filtered: a value carrying a quote came out of a SQL string
    (``'a;server=prod'``), and a value that is only digits is a column predicate
    (``"account=4100"``), not a place.
    """
    value = raw.strip().strip("{}")
    if value.lower().startswith("tcp:"):
        value = value[4:]
    value = value.split(",", maxsplit=1)[0]
    if not value or value.isdigit() or not _HOST_SHAPED.match(value):
        return None
    return value.lower()


def _connection_targets(text: str) -> set[str]:
    """Connection targets readable from one string literal.

    A hostname from a ``scheme://user:pass@host:port/db`` DSN, or the value of ``SERVER=``,
    ``DSN=``, ``HOST=`` or ``ACCOUNT=`` in a keyword-style connection string.
    """
    targets: set[str] = set()
    for match in _DSN_PATTERN.finditer(text):
        authority = match.group("authority").rpartition("@")[2]
        host = authority.split(":", maxsplit=1)[0].strip("[]")
        if host:
            targets.add(host.lower())
    for match in _ODBC_DSN_PATTERN.finditer(text):
        if (value := _odbc_target(match.group(1))) is not None:
            targets.add(value)
    return targets


def _string_bindings(tree: ast.Module) -> dict[str, str]:
    """Module-level ``name = "literal"`` assignments, so a DSN held in a variable is still read.

    ``_dsn = "postgresql://warehouse.internal/risk"`` followed by ``asyncpg.connect(_dsn)`` is the
    ordinary spelling, and without this the call's target is unreadable — which would refuse the
    cell with a remedy naming the driver while the host sat two lines above, allowlistable and
    ignored.
    """
    bindings: dict[str, str] = {}
    for node in _module_statements(tree.body):
        if isinstance(node, ast.Assign):
            targets: Sequence[ast.expr] = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = (node.target,)
        else:
            continue
        value = node.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        for target in targets:
            for name in _target_names(target):
                bindings[name] = value.value
    return bindings


def _call_targets(node: ast.Call, bindings: dict[str, str]) -> set[str]:
    """Connection targets readable from one call's own arguments.

    A call with no literal argument — ``pyodbc.connect(build_dsn())`` — has an unknown target and
    says so by returning nothing, which is what sends it to the by-name branch of the refusal.
    """
    targets: set[str] = set()
    for argument in (*node.args, *(keyword.value for keyword in node.keywords)):
        for inner in ast.walk(argument):
            if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                targets |= _connection_targets(inner.value)
            elif isinstance(inner, ast.Name) and (text := bindings.get(inner.id)):
                targets |= _connection_targets(text)
    return targets


def _is_database_module(name: str) -> bool:
    """Whether ``name`` is a driver whose only purpose is to connect."""
    return name in _DATABASE_MODULES or name.startswith(_DATABASE_MODULE_PREFIXES)


def _is_database_entry_point(root: str, tail: str) -> bool:
    """Whether ``root.tail(...)`` is one of the entry points that connects without importing.

    The receiver root is required, which is the difference between ``pl.read_database(...)`` and
    ``handins.read_database("rates_2024")``. ``write_database`` is the exception and is matched on
    the tail alone: it is a frame method, so there is no module receiver to insist on.
    """
    if tail in _FRAME_DATABASE_CALLS:
        return True
    if tail in _POLARS_DATABASE_CALLS:
        return root in _POLARS_ROOTS
    return tail in _ENGINE_CALLS and root == "sqlalchemy"


def _database_call_sites(tree: ast.Module, aliases: dict[str, str]) -> list[_Connection]:
    """Every call in the cell that opens a database connection, with its own targets.

    Receivers are matched by module *name* as well as through this cell's imports, because
    marimo's single-definition rule puts ``import pyodbc`` in one cell and ``pyodbc.connect(...)``
    in another. Dual-use modules join only where the call shows a connection: ``duckdb.sql`` over a
    parquet file beside the notebook is local work (:data:`_DUAL_USE_DATABASE_MODULES`).
    """
    bindings = _string_bindings(tree)
    sites: list[_Connection] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        resolved, root, tail = _call_name(node, aliases)
        if not resolved:
            continue
        targets = _call_targets(node, bindings)
        permits: set[str] = set()
        if _is_database_module(root) or (
            root in _DUAL_USE_DATABASE_MODULES and (targets or tail in _DATABASE_CALLS)
        ):
            permits.add(root)
        if _is_database_entry_point(root, tail):
            permits.add(tail)
        if permits:
            sites.append(
                _Connection(
                    subject=f"`{resolved}(...)`",
                    permits=frozenset(permits),
                    targets=frozenset(targets),
                    line=node.lineno,
                )
            )
    return sites


def _unattributed_connection_strings(tree: ast.Module, attributed: set[str]) -> list[_Connection]:
    """Literals that *are* connection strings without a recognised call to attribute them to.

    This is what closes the unlisted-driver class. ``_DATABASE_MODULES`` will never be complete —
    a cell importing ``asyncpg`` and holding a full ``postgresql://warehouse.internal/risk`` used
    to pass on both counts — so a literal that is itself a connection string naming a server is a
    signal on its own, and one the refusal can name precisely enough to be lifted.

    Prose is excluded twice over: docstrings never reach here, and a literal that merely *mentions*
    a DSN mid-sentence does not begin with one (:data:`_CONNECTION_STRING_HEAD`).
    """
    found: list[_Connection] = []
    seen: set[str] = set(attributed)
    for text, line in _string_constants(tree, prose=False):
        if not _CONNECTION_STRING_HEAD.match(text):
            continue
        targets = _connection_targets(text) - seen
        if not targets:
            continue
        seen |= targets
        found.append(
            _Connection(
                subject="a connection string literal",
                permits=frozenset(),
                targets=frozenset(targets),
                line=line,
            )
        )
    return found


def _check_database(tree: ast.Module, aliases: dict[str, str], policy: Policy) -> list[Violation]:
    """Refuse a database connection that is not allowlisted.

    Policy rather than style, and beside :func:`_check_network` rather than after it, because it
    is the same question — data leaving or arriving over a wire that a reviewer re-running the
    notebook cannot reproduce — asked in a vocabulary the network check cannot see. That check is
    gated on HTTP-shaped imports, and ``pl.read_database_uri(...)`` imports nothing but polars.

    Each connection is judged on its own targets, and a driver imported but never called is a
    connection with no readable target rather than no connection at all. The refusal is
    deliberately not the network one: telling a user to allowlist a *hostname* would be nonsense
    for ``DSN=RiskWarehouse``, and telling them nothing at all was the state this replaced.
    """
    sites = _database_call_sites(tree, aliases)
    called = {name for site in sites for name in site.permits}
    loose = _unattributed_connection_strings(tree, {t for site in sites for t in site.targets})
    imported = [
        module
        for module in sorted(_imported_modules(tree))
        if _is_database_module(module) and module not in called
    ]
    if imported:
        # A driver imported here and a connection string sitting beside it are one connection, not
        # two. Kept together, either the host or the driver lifts it; split apart, the user has to
        # discover that permitting the host they can see is not enough.
        adopted = frozenset(target for connection in loose for target in connection.targets)
        sites += [
            _Connection(subject=f"`{module}`", permits=frozenset({module}), targets=adopted)
            for module in imported
        ]
        if adopted:
            loose = []
    sites += loose

    refused = [site for site in sites if not site.permitted(policy)]
    if not refused:
        if sites:
            logger.debug("database access permitted for %d site(s)", len(sites))
        return []
    return [
        _database_violation(site, policy)
        for site in sorted(refused, key=lambda site: (site.line or 0, site.subject))
    ]


def _database_violation(site: _Connection, policy: Policy) -> Violation:
    """The refusal for one connection: what it found, and the one line of config that lifts it."""
    allowed = ", ".join(sorted(policy.database_allowlist)) or "nothing (the allowlist is empty)"
    unpermitted = sorted(target for target in site.targets if not policy.permits_database(target))
    if unpermitted:
        found = f"to {', '.join(unpermitted)}"
        remedy = (
            f"If the user has decided to permit this connection, it goes in "
            f"`[policy] database_allowlist` in kedge.toml as "
            f"{', '.join(repr(target) for target in unpermitted)}."
        )
    else:
        found = (
            "to a target kedge cannot read from the source — a DSN name, an environment variable "
            "or a string built at run time"
        )
        remedy = (
            f"There is no target in this cell to name, so permitting it means allowlisting what "
            f"opens the connection: {', '.join(repr(name) for name in sorted(site.permits))} in "
            f"`[policy] database_allowlist` in kedge.toml."
        )
    return Violation(
        stage=ValidationStage.POLICY,
        message=(
            f"this cell opens a database connection via {site.subject}, {found}. The database "
            f"allowlist permits: {allowed}. A reproducible notebook takes its inputs from the "
            f"managed hand-in store, not from a live connection whose contents change between "
            f"runs — and a write to one is not something a reviewer can undo. Load the data "
            f"through the hand-in instead. {remedy}"
        ),
        line=site.line,
    )


def _write_target(node: ast.Call, tail: str) -> str | None:
    """The literal path a write call names, or ``None`` when it is not statically knowable."""
    if tail == "open":
        mode = ""
        if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
            mode = str(node.args[1].value)
        mode = mode or next(
            (
                str(kw.value.value)
                for kw in node.keywords
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant)
            ),
            "r",
        )
        if not any(flag in mode for flag in ("w", "a", "x", "+")):
            return None
        receiver_literal = _literal_path(node.func)
        if receiver_literal is not None:
            return receiver_literal
    for candidate in (*node.args, *(kw.value for kw in node.keywords)):
        if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str):
            return candidate.value
    return _literal_path(node.func)


def _literal_path(node: ast.expr) -> str | None:
    """Recover ``Path("C:/out.csv")`` from the receiver of a chained call."""
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        current = current.value
    if isinstance(current, ast.Call):
        for argument in current.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                return argument.value
    return None


def _check_writes(tree: ast.Module, aliases: dict[str, str], policy: Policy) -> list[Violation]:
    found: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        resolved = _resolved(node.func, aliases)
        tail = resolved.rsplit(".", maxsplit=1)[-1]
        if tail not in _WRITE_CALLS:
            continue
        target = _write_target(node, tail)
        if target is None or policy.permits_write(target):
            continue
        where = policy.working_dir or "the working directory"
        found.append(
            Violation(
                stage=ValidationStage.POLICY,
                message=(
                    f"`{resolved}(...)` writes to {target!r}, which is outside {where}. Everything "
                    f"a notebook produces belongs beside the notebook so it travels with the "
                    f"artifacts it describes. Use a relative path."
                ),
                line=node.lineno,
            )
        )
    return found


def _check_credentials(tree: ast.Module) -> list[Violation]:
    found: list[Violation] = []
    for text, line in _string_constants(tree):
        for pattern, description in _CREDENTIAL_PATTERNS:
            if pattern.search(text):
                found.append(
                    Violation(
                        stage=ValidationStage.POLICY,
                        message=(
                            f"a string literal in this cell looks like {description}. Credentials "
                            f"never go in notebook source: it is reviewed, diffed and shared. "
                            f"kedge keeps secrets in the OS keyring."
                        ),
                        line=line,
                    )
                )
                break
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, str) or len(node.value.value) < 6:
            continue
        for target in node.targets:
            for name in _target_names(target):
                if _CREDENTIAL_NAMES.match(name):
                    found.append(
                        Violation(
                            stage=ValidationStage.POLICY,
                            message=(
                                f"`{name}` is assigned a string literal. If that is a credential it "
                                f"must come from the OS keyring, not from notebook source; if it is "
                                f"not, give it a name that does not read like one."
                            ),
                            line=node.lineno,
                        )
                    )
                    break
    return found


# ── stage 4: output style ────────────────────────────────────────────────────────────────────


def _check_style(
    tree: ast.Module, policy: Policy, rounding: RoundingContext, frame_names: frozenset[str]
) -> list[Violation]:
    found: list[Violation] = []
    if not policy.allow_pandas:
        found += _check_pandas(tree)
    found += _check_rounding(tree, rounding)
    found += _check_row_loops(tree, frame_names)
    return found


def _check_pandas(tree: ast.Module) -> list[Violation]:
    message = (
        "generated code is polars, never pandas (PLAN 2.5, CONVENTIONS non-negotiable 1). Rewrite "
        "this cell with `pl` — the lazy API, expressions rather than row access — so the notebook "
        "shares one dataframe library end to end and the query plan stays aligned with the cell "
        "graph."
    )
    found: list[Violation] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Import)
            and any(
                alias.name == "pandas" or alias.name.startswith("pandas.") for alias in node.names
            )
        ) or (isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "pandas"):
            found.append(Violation(stage=ValidationStage.STYLE, message=message, line=node.lineno))
    return found


def _rounding_applies(node: ast.Call, rounding: RoundingContext) -> bool:
    if rounding.strict:
        return True
    root = _root_name(node.func)
    if root and root in rounding.names:
        return True
    return any(text.strip().lower() in rounding.columns for text, _ in _string_constants(node))


def _check_rounding(tree: ast.Module, rounding: RoundingContext) -> list[Violation]:
    if not (rounding.strict or rounding.names or rounding.columns):
        return []
    message = (
        "this rounds a value derived from an Excel ROUND region with plain rounding. Excel rounds "
        "half away from zero *and* collapses the operand to 15 significant decimal digits first — "
        "ROUND(4491760.574999999, 2) is 4491760.58, not .57 — while polars and Python round half "
        "to even. Use `.xl.round(...)` from `kedge.xl`, which implements both rules and makes the "
        "Excel-compatibility assumption greppable for a reviewer."
    )
    found: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr == "round":
            receiver = node.func.value
            if isinstance(receiver, ast.Attribute) and receiver.attr == "xl":
                continue
            if _rounding_applies(node, rounding):
                found.append(
                    Violation(stage=ValidationStage.STYLE, message=message, line=node.lineno)
                )
        elif (
            isinstance(node.func, ast.Name)
            and node.func.id == "round"
            and _rounding_applies(node, rounding)
        ):
            found.append(Violation(stage=ValidationStage.STYLE, message=message, line=node.lineno))
    return found


def _check_row_loops(tree: ast.Module, frame_names: frozenset[str]) -> list[Violation]:
    found: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.For | ast.AsyncFor):
            continue
        iterable = node.iter
        subject = ""
        if isinstance(iterable, ast.Call):
            tail = _dotted(iterable.func).rsplit(".", maxsplit=1)[-1]
            if tail in _ROW_ITERATORS:
                subject = f"`.{tail}()`"
        elif isinstance(iterable, ast.Name) and iterable.id in frame_names:
            subject = f"`{iterable.id}`"
        if not subject:
            continue
        found.append(
            Violation(
                stage=ValidationStage.STYLE,
                message=(
                    f"this loops row by row over {subject}. An Excel formula region is a column "
                    f"operation by construction — the R1C1 compression in the analysis has already "
                    f"established that — so a row-wise loop means the translation of that region "
                    f"is wrong, not merely slow. Express it with `pl.when(...).then(...)`, a join, "
                    f"`group_by().agg()`, `shift` or `cum_sum`."
                ),
                line=node.lineno,
            )
        )
    return found


# ── the gate ─────────────────────────────────────────────────────────────────────────────────


def validate_cell(
    code: str,
    *,
    registry: NameRegistry | None = None,
    cell: str | None = None,
    policy: Policy | None = None,
    rounding: RoundingContext | None = None,
    frame_names: frozenset[str] | None = None,
) -> ValidationReport:
    """Run the five-stage gate over one proposed cell body.

    Stages run in PLAN M4's order and short-circuit: syntax, the marimo contract checked against
    the live graph, policy, then output style. The fifth stage — return the violations and let the
    model retry, capped at :data:`MAX_VALIDATION_ATTEMPTS` — belongs to the loop, because it is
    about the conversation rather than about the code.

    Args:
        code: The full cell body, exactly as it would be submitted.
        registry: The live name registry. Without one, the single-definition and cycle checks are
            skipped and only the wildcard-import check runs — say so rather than implying the cell
            was checked against a graph.
        cell: The id or name of the cell being replaced, so an ``edit_cell`` that keeps its own
            names is not treated as colliding with itself.
        policy: What the code is allowed to reach for. Defaults to a policy with no network and no
            writes outside the working directory.
        rounding: Where a bare ``.round()`` is a mistranslation. Build it with
            :meth:`RoundingContext.from_analysis`.
        frame_names: Notebook names that plausibly hold a frame, for the row-wise-loop check.
            Normally ``registry.frame_names()``.

    Returns:
        The verdict. ``report.ok`` is the only thing a caller has to check.
    """
    resolved_policy = policy or Policy()
    resolved_rounding = rounding or RoundingContext()
    if frame_names is None:
        frame_names = registry.frame_names() if registry is not None else frozenset()

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        detail = exc.msg or "invalid syntax"
        return ValidationReport(
            violations=(
                Violation(
                    stage=ValidationStage.SYNTAX,
                    message=f"the cell does not parse: {detail}",
                    line=exc.lineno,
                ),
            ),
            stage=ValidationStage.SYNTAX,
        )

    names = extract_names(tree)

    for stage, violations in (
        (ValidationStage.MARIMO, _check_marimo(tree, names, registry, cell)),
        (ValidationStage.POLICY, _check_policy(tree, resolved_policy)),
        (
            ValidationStage.STYLE,
            _check_style(tree, resolved_policy, resolved_rounding, frame_names),
        ),
    ):
        if violations:
            logger.info("validation gate rejected a cell at the %s stage", stage.value)
            return ValidationReport(violations=tuple(violations), names=names, stage=stage)

    return ValidationReport(names=names)


# ── the check a kernel would otherwise have made ─────────────────────────────────────────────


MISSING_NAME_STAGE = "definition"
"""The stage name a caller records for a :func:`undefined_name` violation.

Not a :class:`ValidationStage`: this is not one of PLAN M4's five stages and adding a sixth member
would say it is. It is a stand-in for the kernel, so it is named alongside the stages rather than
among them.
"""


def undefined_name(report: ValidationReport, name: str) -> tuple[str, ...]:
    """Refuse a body that does not define the name the cells below it read.

    The one thing worth checking that :func:`validate_cell` does not, and it is a stand-in rather
    than an addition. In the chat the kernel catches this: the cell is accepted, flushed, and the
    cells downstream of it fail on a name that was never bound, which marimo reports precisely.
    ``kedge convert`` runs no kernel at all -- it writes through
    :class:`~kedge.notebook.filedriver.FileNotebookDriver` -- so without this check the failure
    surfaces the next time somebody opens the notebook, three cells below the one that caused it,
    and the blame lands on a cell that was written correctly.

    It lives here rather than in the driver that calls it because it is an extension of the gate,
    and a check kept beside its caller is a check the next caller copies.

    Args:
        report: The gate's verdict, whose ``names`` hold what the body defines.
        name: The name the body has to bind -- the cell's own.

    Returns:
        One violation naming the cell that failed to define the name, or nothing when the body
        defines it.
    """
    if name in report.names.public_defs:
        return ()
    defined = ", ".join(report.names.public_defs) or "nothing"
    return (
        f"definition: this cell must define '{name}' -- the cells below read it by that "
        f"name. It defines {defined}.",
    )


# ── kernel-side rejections ───────────────────────────────────────────────────────────────────


def violations_from_conflicts(conflicts: Iterable[DefinitionConflict]) -> tuple[str, ...]:
    """Render marimo's own definition conflicts as violation lines.

    The kernel is the authority on the single-definition rule and catches anything the pre-flight
    check missed. ``NotebookDriver`` already parses its message into typed conflicts, so the same
    vocabulary reaches the model whether the rejection happened here or there.
    """
    rendered: list[str] = []
    for conflict in conflicts:
        owners = ", ".join(conflict.defined_in) or "another cell"
        rendered.append(
            f"marimo: '{conflict.name}' is already defined in {owners}. Edit that cell rather "
            f"than redefining the name, or rename yours to '_{conflict.name}' to make it "
            f"cell-local."
        )
    return tuple(rendered)


def violations_from_kernel_error(exc: BaseException) -> tuple[str, ...]:
    """Render a kernel-side rejection as violation lines, parsing conflicts where present.

    Reuses :func:`kedge.notebook.driver.parse_definition_conflicts` for the multiply-defined case,
    both directly and via the conflicts a
    :class:`~kedge.notebook.driver.MultiplyDefinedError` already carries.
    """
    conflicts: tuple[DefinitionConflict, ...] = tuple(getattr(exc, "conflicts", ()) or ())
    if not conflicts:
        conflicts = parse_definition_conflicts(str(exc))
    if conflicts:
        return violations_from_conflicts(conflicts)
    return (f"the kernel rejected this cell: {exc}",)
