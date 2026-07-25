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
it into a :class:`~kedge.server.events.ValidationEvent` and a tool result, and the model gets to
try again — three times, then kedge stops and tells the user (:data:`MAX_VALIDATION_ATTEMPTS`).
An exception would be the wrong shape entirely: a rejected cell is a normal event in a
conversation, not an error in kedge.

The name analysis is deliberately approximate in the safe direction. marimo does proper scope
resolution kernel-side; this module over-collects bindings and under-collects references, so the
pre-flight check produces false *passes* rather than false rejections. A false pass costs one
round trip and marimo reports it precisely; a false rejection would make the gate something the
model learns to fight.
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
    "CellNames",
    "Policy",
    "RoundingContext",
    "ValidationReport",
    "ValidationStage",
    "Violation",
    "extract_names",
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
        re.compile(
            r"(?i)\b(?:jdbc|odbc|mongodb|postgres(?:ql)?|mysql|redis)://[^\s'\"]*:[^\s'\"@]+@"
        ),
        "a connection string with an embedded password",
    ),
)

_CREDENTIAL_NAMES = re.compile(
    r"(?i)^_*(?:api_?key|apikey|secret|password|passwd|pwd|token|access_?token|"
    r"client_?secret|private_?key|conn(?:ection)?_?string)$"
)


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


@dataclass(frozen=True, slots=True)
class Policy:
    """What generated code is allowed to reach for.

    ``working_dir`` bounds writes. ``network_allowlist`` holds hostnames; it is empty by default,
    which means no network at all — a conversion notebook reads a workbook and a hand-in from
    disk, and anything else is a question for the user rather than a default.

    Example:
        >>> Policy(network_allowlist=frozenset({"internal.example"})).allows_host("internal.example")
        True
    """

    working_dir: Path | None = None
    network_allowlist: frozenset[str] = frozenset()
    allow_pandas: bool = False
    """The explicit escape hatch PLAN 2.5 asks for. Off, and expected to stay off."""

    def allows_host(self, host: str) -> bool:
        """Whether ``host`` is on the allowlist, matching subdomains of an allowed domain."""
        target = host.lower().strip()
        return any(
            target == allowed.lower() or target.endswith("." + allowed.lower())
            for allowed in self.network_allowlist
        )

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


def _string_constants(tree: ast.AST) -> Iterator[tuple[str, int]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
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
    found += _check_network(tree, policy)
    found += _check_writes(tree, aliases, policy)
    found += _check_credentials(tree)
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


def _check_network(tree: ast.Module, policy: Policy) -> list[Violation]:
    used = _imported_modules(tree) & _NETWORK_MODULES
    if not used:
        return []
    hosts = {
        parsed.hostname
        for text, _ in _string_constants(tree)
        if (parsed := urlparse(text)).scheme in ("http", "https") and parsed.hostname
    }
    if hosts and all(policy.allows_host(host) for host in hosts):
        logger.debug("network use permitted for hosts %s", sorted(hosts))
        return []
    allowed = ", ".join(sorted(policy.network_allowlist)) or "nothing (the allowlist is empty)"
    refused = ", ".join(sorted(hosts)) if hosts else "an address kedge could not read statically"
    return [
        Violation(
            stage=ValidationStage.POLICY,
            message=(
                f"this cell reaches the network via `{', '.join(sorted(used))}`, to {refused}. The "
                f"allowlist permits: {allowed}. A reproducible notebook takes its inputs from the "
                f"managed hand-in store, not from a live endpoint whose contents change between "
                f"runs. Load the data through the hand-in instead."
            ),
        )
    ]


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
