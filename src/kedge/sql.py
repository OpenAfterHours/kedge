"""Rendering SQL for a person to run somewhere kedge cannot reach.

kedge holds no database connection and issues no statement. What a converted workbook does
instead is hand the user a statement to run in their own client, against their own warehouse,
under their own credentials -- and then take the result back as a hand-in. This module is the
one place a statement is built, and it exists because the alternative is string concatenation
scattered through generated notebook cells.

The problem it actually solves is **correctness, not injection**. The values interpolated here
came out of the user's own extract, so nobody is smuggling anything; but a counterparty called
``O'Brien`` concatenated naively produces a statement that will not parse, a ``None`` becomes
the four characters ``None`` rather than ``NULL``, a date becomes ``datetime.date(2026, 6, 30)``,
and a float large enough to reach exponent notation becomes ``1.5e+07``, which several engines
reject. Each of those is a broken statement a user has to debug by hand at the point they were
promised the process was under control. :func:`literal` renders every one of them properly, and
the escaping it applies is the reason a value containing a quote is safe as well as correct.

Two questions about a piece of text are answered here as well, and they are here for the same
reason the rendering is: :func:`changes_data` says whether running it would change data, and
:func:`reads_as_sql` says whether it is a statement at all or a sentence that happens to open
with a SQL verb. Both are decided by one scanner over the text, and both belong in this module
rather than wherever the text was found -- a second, weaker copy of "is this SQL" is exactly the
drift non-negotiable 3 exists to prevent, and it is not machine-enforceable, so it holds only as
long as it is kept in one place.

**What this module does not do.** It does not connect, execute, transact, or validate that a
statement means what its author intended. It does not know your dialect: the literal forms here
(single-quoted strings with doubled internal quotes, ISO dates, unquoted numerics, ``NULL``) are
the ANSI ones every mainstream engine accepts, and anything more specific belongs in the
statement text the plan supplies rather than here. It emits text. A human reads it, decides,
and runs it.

Rendered statements are capped. :data:`MAX_STATEMENTS` is not a limit on what a process may
adjust -- it is the point past which a wall of generated SQL stops being something a reviewer
can meaningfully read before running, and a plan that reaches it should be handing over a
join against a staging table rather than forty thousand single-row updates. The cap reports
rather than truncates silently, for the same reason reconciliation refuses to report a pass it
did not measure.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from kedge.errors import KedgeError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_STATEMENTS",
    "SqlRenderError",
    "changes_data",
    "literal",
    "placeholders",
    "reads_as_sql",
    "render",
    "render_all",
    "script",
]

MAX_STATEMENTS = 5_000
"""How many rendered statements :func:`render_all` will return before refusing.

Chosen as the point where the output stops being reviewable rather than where it stops being
generatable. Somebody scrolling five thousand UPDATE statements is not checking them.
"""

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_ ]*)\}")
"""``{column_name}`` -- deliberately not :meth:`str.format`.

``str.format`` would also honour ``{0}``, ``{a.b}``, ``{x!r}`` and ``{y:>10}``, and every one of
those is a way for a template to reach something that is not a column value. Matching the narrow
form means an unrecognised brace is reported as an unknown column rather than quietly evaluated.
"""

_MAX_SAFE_INTEGER = 2**63 - 1


class SqlRenderError(KedgeError):
    """A statement could not be rendered.

    Always names the template and the column at fault: the fix is in the plan, and the person
    reading this has the plan open.
    """


def literal(value: Any) -> str:
    """Render one Python value as an ANSI SQL literal.

    Args:
        value: Anything a polars row yields -- ``None``, a string, a number, a ``Decimal``, a
            ``date``, a ``datetime``, a ``time``, or a ``bool``.

    Returns:
        The literal text, quoted and escaped where the type calls for it.

    Raises:
        SqlRenderError: The value is of a type with no unambiguous literal form -- a list, a
            dict, a struct. Rendering one would mean choosing a dialect's array syntax on the
            caller's behalf.

    Example:
        >>> literal("O'Brien")
        "'O''Brien'"
        >>> literal(None)
        'NULL'
        >>> literal(dt.date(2026, 6, 30))
        "DATE '2026-06-30'"
    """
    if value is None:
        return "NULL"
    # bool before int: bool is a subclass of int, and TRUE is not 1 in every engine's type check.
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE_INTEGER:
            # Beyond bigint. Quoting it would silently change the column's type on the way in;
            # emitting it bare would overflow. Neither is a decision to make quietly.
            msg = (
                f"cannot render {value} as a SQL integer literal: it is outside the range of a "
                f"64-bit integer, so the statement would overflow or be silently retyped. "
                f"Convert the column to a decimal or a string in the notebook first."
            )
            raise SqlRenderError(msg)
        return str(value)
    if isinstance(value, Decimal):
        # Decimal is the one numeric type that already knows its own scale. `str` preserves it,
        # including a trailing zero that says "pence", which float formatting would discard.
        return _plain_number(format(value, "f"))
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):  # NaN or infinity
            msg = (
                f"cannot render {value!r} as a SQL literal. A non-finite number has no ANSI "
                f"literal form; decide in the notebook what it should become -- NULL, zero, or "
                f"an excluded row -- before the statement is built."
            )
            raise SqlRenderError(msg)
        # repr() is the shortest round-tripping form, which is what a money value needs; it can
        # still reach exponent notation, which several engines will not parse in a literal.
        return _plain_number(repr(value))
    if isinstance(value, dt.datetime):
        return f"TIMESTAMP '{value.isoformat(sep=' ')}'"
    if isinstance(value, dt.date):
        return f"DATE '{value.isoformat()}'"
    if isinstance(value, dt.time):
        return f"TIME '{value.isoformat()}'"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"

    msg = (
        f"cannot render a {type(value).__name__} as a SQL literal. Nested values -- lists, "
        f"structs, dicts -- have no portable literal form. Flatten or serialise the column in "
        f"the notebook before the statement is built."
    )
    raise SqlRenderError(msg)


_WRITING_VERBS = frozenset(
    {"insert", "update", "delete", "merge", "upsert", "replace", "truncate", "drop", "alter"}
)
"""Statement openers that change what a later extract will return.

``create`` is deliberately absent. ``CREATE TEMP TABLE ... AS SELECT`` is an ordinary way to
write a read-only extract, and treating every one of them as a write would put a confirmation
step in front of a query that changes nothing -- which is how a control becomes a box people
tick without reading. The verbs here all change rows or objects that were already there.
"""

_MAIN_VERBS = frozenset({"select", "insert", "update", "delete", "merge"})
"""What may follow a ``WITH`` prefix. A CTE's *name* can never be one: they are reserved words."""


def _statement_words(text: str) -> list[list[str]]:
    """Bare words at bracket depth zero, grouped by top-level statement.

    The scanner exists so that :func:`changes_data` can look at the verb a statement *opens
    with* rather than at whether the word appears anywhere in it. Everything it skips is
    somewhere the word ``update`` means something other than an update: inside a string literal
    (``WHERE action = 'update'``), inside a quoted identifier, inside a comment, or inside
    brackets, where a sub-select's own verb belongs to the sub-select. ``last_update`` survives
    as one word rather than as ``update``, because words are taken whole.
    """
    statements: list[list[str]] = [[]]
    depth = 0
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if text.startswith("--", index):
            newline = text.find("\n", index)
            index = length if newline == -1 else newline + 1
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = length if end == -1 else end + 2
            continue
        if char in "'\"`":
            index = _skip_delimited(text, index, char)
            continue
        if char == "[":
            end = text.find("]", index + 1)
            index = length if end == -1 else end + 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == ";" and depth == 0:
            statements.append([])
        elif char.isalpha() or char == "_":
            start = index
            while index < length and (text[index].isalnum() or text[index] == "_"):
                index += 1
            if depth == 0:
                statements[-1].append(text[start:index].lower())
            continue
        index += 1
    return [words for words in statements if words]


def _skip_delimited(text: str, start: int, delimiter: str) -> int:
    """Index just past a quoted run, honouring the doubled-delimiter escape SQL uses."""
    index = start + 1
    while index < len(text):
        if text[index] != delimiter:
            index += 1
            continue
        if text.startswith(delimiter * 2, index):
            index += 2
            continue
        return index + 1
    return len(text)


def changes_data(statement: str) -> bool:
    """Whether running this text would change data, judged from the verb it opens with.

    kedge never runs a statement, so the only thing it can know about one is what it says. That
    matters in exactly one place and it matters a lot: a hand-off that writes has to be
    confirmed as having been run before anything downstream appears, because nothing downstream
    is evidence it ran -- a re-extract taken beforehand looks exactly like one taken after, and
    there is no way to tell afterwards. A plan that hands over an ``UPDATE`` and declares it
    read-only produces precisely that notebook.

    Judged on the **opening verb of each top-level statement**, never on whether a word appears.
    ``SELECT a.last_update FROM t FOR UPDATE`` is a locking read; ``WHERE action = 'update'`` is
    a string; ``-- update the accruals first`` is a comment. Every one of those would be a false
    positive, and a false positive here is a confirmation step in front of a query that changes
    nothing. A ``WITH`` prefix is followed through to the statement it introduces, because
    ``WITH scoped AS (...) DELETE FROM ...`` is a delete.

    Args:
        statement: The statement text, or a template with ``{placeholders}`` still in it --
            the verb is in the same place either way.

    Returns:
        Whether any top-level statement in the text writes.

    Example:
        >>> changes_data("UPDATE fin.accruals SET x = {y} WHERE id = {id}")
        True
        >>> changes_data("SELECT trade_id, last_update FROM fin.accruals FOR UPDATE")
        False
    """
    for words in _statement_words(statement):
        verb = words[0]
        if verb == "with":
            verb = next((word for word in words[1:] if word in _MAIN_VERBS), "")
        if verb in _WRITING_VERBS:
            return True
    return False


_OBJECT_KEYWORDS = frozenset(
    {
        "column",
        "constraint",
        "database",
        "function",
        "index",
        "procedure",
        "schema",
        "sequence",
        "table",
        "trigger",
        "type",
        "view",
    }
)
"""What a ``DROP``, ``ALTER`` or ``CREATE`` names before it names the thing."""

_MUST_FOLLOW: dict[str, frozenset[str]] = {
    "insert": frozenset({"into"}),
    "replace": frozenset({"into"}),
    "upsert": frozenset({"into"}),
    "delete": frozenset({"from"}),
    "truncate": frozenset({"table"}),
}
"""Verbs whose grammar fixes the **very next** word, which is what separates them from English.

``INSERT INTO`` and ``DELETE FROM`` are one token apart in SQL and arbitrarily far apart in a
sentence, so the position is the whole discrimination: ``Insert values from A1 into the tracker``
does contain ``into``, and puts ``values`` where SQL requires it.

Dialects that allow the keyword to be dropped -- T-SQL's ``INSERT tbl VALUES (...)``, ``DELETE
tbl`` -- read as prose here. That is the direction to err in; see :func:`reads_as_sql`.
"""

_MUST_NAME_SOON: dict[str, frozenset[str]] = {
    "drop": _OBJECT_KEYWORDS,
    "alter": _OBJECT_KEYWORDS,
    "create": _OBJECT_KEYWORDS,
}
"""Verbs that name what kind of object they act on, within a modifier or two of the verb.

``DROP TABLE`` is adjacent; ``CREATE TEMP TABLE`` and ``CREATE OR REPLACE VIEW`` are not, which
is why this is a short window rather than the next word alone.
"""

_MUST_NAME_SOON_WINDOW = 3
"""How many words after the verb :data:`_MUST_NAME_SOON` looks in. Wide enough for ``OR REPLACE``,
narrow enough that a sentence has to put a table keyword in its first four words."""

_MUST_CONTAIN: dict[str, frozenset[str]] = {
    "update": frozenset({"set"}),
    "merge": frozenset({"using", "on"}),
    "select": frozenset({"from"}),
}
"""Verbs whose mandatory keywords follow a target of unpredictable length. **All** must appear.

``UPDATE fin.accruals SET`` and ``UPDATE t AS a SET`` put ``SET`` in different places, so these
can only be checked by presence -- though never immediately after the verb, because ``UPDATE SET``
names nothing to update. ``MERGE`` carries two, and both are load-bearing: ``USING`` alone lets
``"Merge the two tabs before using this"`` through, where ``ON`` is a join condition no sentence
supplies.

``SELECT`` is here for completeness rather than for any caller. A bare ``SELECT 1`` is a legal
statement and reads as prose under this rule, which is the cost of asking one question of every
opener.

**This is the weak half of the predicate and it is worth naming.** A presence test cannot tell
``UPDATE fin.accruals SET x = 1`` from ``"Update the tracker and set the flag"``: both open with
the verb and both contain the keyword, and no rule over bare words separates them without
blocking real SQL -- an article test breaks on ``FROM fin.accruals AS a``, and a window on the
target breaks on ``UPDATE t AS a SET``. The two confirmed prose cases from the conversion this
predicate was written for are both :data:`_MUST_FOLLOW` verbs and are both rejected; an English
sentence opening ``Update`` and containing ``set`` still reads as SQL. Callers should quote the
text they judged rather than assert about it -- ``kedge.plan.review`` does.
"""


def reads_as_sql(text: str) -> bool:
    """Whether this text is a SQL statement, or a sentence that happens to contain a SQL verb.

    The question :func:`changes_data` does not ask, and must not: ``changes_data`` is given
    statements -- a hand-off's own text, a command out of ``xl/connections.xml`` -- and answers
    what running one would do. Point it at prose and it answers about the prose, correctly and
    uselessly: ``"Delete row 4 from the tracker"`` opens with ``delete``, so it "writes".

    That matters because one caller does not have a statement. A column of
    ``="UPDATE ... "&F17&"..."`` is the step of a manual process that changes the data, and
    finding it means reading the text an ordinary ``text_manipulation`` region concatenates --
    where most of what turns up is labels, keys and instructions to a colleague. Asking
    ``changes_data`` alone put a warning on the approval card for
    ``="Delete row "&A1&" from the tracker"``. Asking a keyword-count question first, in the
    module that happened to have the text, was worse: a second and weaker classifier for the
    thing this module exists to own (non-negotiable 3).

    So the judgement is here, on the same scanner :func:`changes_data` uses -- bare words at
    bracket depth zero, comments and quoted runs skipped, grouped by top-level statement -- and
    the test is **grammar, not vocabulary**. Every statement must open with a verb this module
    recognises and carry that verb's mandatory keyword where the grammar puts it: immediately
    after (:data:`_MUST_FOLLOW`) or somewhere past the target (:data:`_MUST_CONTAIN`). A ``WITH``
    prefix is followed through to the statement it introduces, exactly as ``changes_data``
    follows it.

    **Conservative on purpose, in one direction.** A dialect-specific shape that omits a keyword
    -- ``INSERT tbl VALUES``, ``DELETE tbl``, ``SELECT 1`` -- reads as prose, and an opener this
    module has no rule for (``EXEC``, ``CALL``, ``GRANT``) reads as prose too. The two errors are
    not equal: reading real SQL as prose costs a warning nobody sees, while reading prose as SQL
    puts amber on the approval card of a correct plan, and a card that is always amber stops
    being read.

    **It is a reading, not a parse, and it is beatable.** Where the mandatory keyword follows a
    target of unknown length the test can only be presence, so ``"Update the tracker and set the
    flag"`` still reads as SQL; :data:`_MUST_CONTAIN` records why no rule over bare words fixes
    that without rejecting real statements. Callers should quote the text they judged.

    Args:
        text: Statement text, a template with ``{placeholders}`` still in it, or arbitrary prose.

    Returns:
        Whether every top-level statement in ``text`` reads as SQL. Empty text is not a
        statement, and returns ``False``.

    Example:
        >>> reads_as_sql("UPDATE fin.accruals SET accrual_gbp = 1 WHERE trade_id = 'A'")
        True
        >>> reads_as_sql("Delete row 4 from the tracker")
        False
    """
    statements = _statement_words(text)
    if not statements:
        return False
    return all(_reads_as_one_statement(words) for words in statements)


def _reads_as_one_statement(words: list[str]) -> bool:
    """Whether one top-level statement's bare words open the way its verb requires."""
    start = 0
    if words[0] == "with":
        start = next(
            (index for index, word in enumerate(words) if index and word in _MAIN_VERBS), -1
        )
        if start < 0:
            return False
    verb = words[start]
    required = _MUST_FOLLOW.get(verb)
    if required is not None:
        return len(words) > start + 1 and words[start + 1] in required
    soon = _MUST_NAME_SOON.get(verb)
    if soon is not None:
        window = words[start + 1 : start + 1 + _MUST_NAME_SOON_WINDOW]
        return any(word in soon for word in window)
    contains = _MUST_CONTAIN.get(verb)
    if contains is not None:
        return contains <= set(words[start + 2 :])
    return False


def placeholders(template: str) -> tuple[str, ...]:
    """The column names a template refers to, in first-appearance order.

    Used to check a template against a frame's schema *before* rendering a thousand statements
    against it, so a misspelled column is one message rather than a thousand.

    Example:
        >>> placeholders("UPDATE t SET amount = {new_amount} WHERE id = {trade_id}")
        ('new_amount', 'trade_id')
    """
    seen: dict[str, None] = {}
    for match in _PLACEHOLDER.finditer(template):
        seen.setdefault(match.group(1), None)
    return tuple(seen)


def render(template: str, row: Mapping[str, Any]) -> str:
    """Render one statement, substituting ``{column}`` with that column's literal.

    Args:
        template: The statement, with ``{column_name}`` where values belong.
        row: One row, as ``polars.DataFrame.iter_rows(named=True)`` yields it.

    Returns:
        The statement, with every placeholder replaced.

    Raises:
        SqlRenderError: A placeholder names a column the row does not have, or a value has no
            literal form.

    Example:
        >>> render("UPDATE t SET x = {amount} WHERE id = {id}", {"amount": 1.5, "id": "A'B"})
        "UPDATE t SET x = 1.5 WHERE id = 'A''B'"
    """

    def substitute(match: re.Match[str]) -> str:
        column = match.group(1)
        if column not in row:
            available = ", ".join(sorted(row)) or "(none)"
            msg = (
                f"the statement template refers to {{{column}}}, which is not a column of the "
                f"frame it is being rendered against. Available: {available}."
            )
            raise SqlRenderError(msg)
        return literal(row[column])

    return _PLACEHOLDER.sub(substitute, template)


def render_all(
    rows: Iterable[Mapping[str, Any]],
    template: str,
    *,
    limit: int = MAX_STATEMENTS,
) -> list[str]:
    """Render one statement per row.

    Args:
        rows: The rows, normally ``frame.iter_rows(named=True)``.
        template: The statement template.
        limit: How many statements to render before refusing.

    Returns:
        One statement per row, in row order.

    Raises:
        SqlRenderError: A row is missing a placeholder's column, a value has no literal form,
            or there are more rows than ``limit``.
    """
    statements: list[str] = []
    for index, row in enumerate(rows):
        if index >= limit:
            msg = (
                f"this would render more than {limit:,} statements. That is past the point "
                f"where anyone can review the SQL before running it, which is the only reason "
                f"to generate it here at all. Hand over a staging table and a set-based UPDATE "
                f"instead, or raise the limit deliberately if the review burden is understood."
            )
            raise SqlRenderError(msg)
        statements.append(render(template, row))
    logger.debug("rendered %d statement(s)", len(statements))
    return statements


def script(
    statements: Sequence[str],
    *,
    header: str | None = None,
    terminator: str = ";",
) -> str:
    """Join rendered statements into one block for the user to copy.

    Args:
        statements: The rendered statements.
        header: Comment text placed at the top, one ``--`` line per line of it. This is where
            the run instruction and the provenance belong -- what it was generated from, and
            how many rows it touches.
        terminator: Appended to any statement not already ending in it.

    Returns:
        The script. Empty statements yield an empty string rather than a lone header, because a
        header over nothing reads as "here is your SQL" when there is none.

    Example:
        >>> script(["UPDATE t SET x = 1"], header="Run against RiskWarehouse")
        '-- Run against RiskWarehouse\\nUPDATE t SET x = 1;'
    """
    if not statements:
        return ""
    lines: list[str] = []
    if header:
        lines.extend(f"-- {line}" if line.strip() else "--" for line in header.splitlines())
    for statement in statements:
        text = statement.strip()
        lines.append(text if text.endswith(terminator) else f"{text}{terminator}")
    return "\n".join(lines)


def _plain_number(text: str) -> str:
    """Expand exponent notation, which several engines reject inside a literal.

    ``repr(15_000_000.0)`` is ``'15000000.0'`` but ``repr(1.5e16)`` is ``'1.5e+16'``, and the
    threshold sits inside the range of a plausible notional. Decimal does the expansion exactly;
    going through float here would reintroduce the binary representation this avoids.
    """
    if "e" not in text and "E" not in text:
        return text
    return format(Decimal(text), "f")
