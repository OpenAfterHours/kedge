"""Render a :class:`~kedge.analysis.model.WorkbookAnalysis` as a self-contained HTML report.

This is the human-facing half of ``kedge inspect``, and PLAN §M1 is clear about what it is for:
the analyser is "independently valuable even if the AI half never ships — it's an Excel
archaeology tool. Anyone who has inherited a 15-tab workbook wants this." So the report leads
with the findings list rather than burying it under structure.

The output is a single file with no external requests of any kind — no CDN, no webfont, no
analytics. kedge is a local-first tool that may be pointed at a bank's workbook, and a report
that phones home would be indefensible. Styling and the small amount of interaction are inlined.

References:
- PLAN.md §M1 (deliverables and the findings list), §2.4 (sheet roles).
"""

from __future__ import annotations

import html
import logging
from collections.abc import Iterable
from pathlib import Path

from kedge.analysis.model import (
    Finding,
    Severity,
    WorkbookAnalysis,
)

logger = logging.getLogger(__name__)

__all__ = ["render_html", "write_report"]

_SEVERITY_ORDER = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}

_STYLE = """
:root {
  color-scheme: light dark;
  --bg: #ffffff; --fg: #1a1a1a; --muted: #666; --line: #e2e2e2;
  --panel: #f7f7f7; --error: #b3261e; --warning: #8a6100; --info: #33618d;
  --ok: #1e6b3a;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16181c; --fg: #e6e6e6; --muted: #9aa0a6; --line: #2c2f36;
    --panel: #1e2128; --error: #f2b8b5; --warning: #e8c46a; --info: #9ecbff;
    --ok: #7ee2a8;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2rem 1.5rem 5rem; background: var(--bg); color: var(--fg);
  font: 15px/1.55 ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif;
}
main { max-width: 68rem; margin: 0 auto; }
h1 { font-size: 1.6rem; margin: 0 0 .25rem; }
h2 { font-size: 1.15rem; margin: 2.5rem 0 .75rem; padding-bottom: .3rem;
     border-bottom: 1px solid var(--line); }
h3 { font-size: .95rem; margin: 1.5rem 0 .5rem; color: var(--muted);
     text-transform: uppercase; letter-spacing: .06em; }
.sub { color: var(--muted); margin: 0 0 2rem; font-size: .9rem; }
code, .mono { font-family: ui-monospace, "Cascadia Code", Consolas, monospace; font-size: .86em; }
table { border-collapse: collapse; width: 100%; margin: .5rem 0 1rem; font-size: .9rem; }
th, td { text-align: left; padding: .45rem .6rem; border-bottom: 1px solid var(--line);
         vertical-align: top; }
th { font-weight: 600; color: var(--muted); font-size: .8rem; text-transform: uppercase;
     letter-spacing: .04em; }
tbody tr:hover { background: var(--panel); }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr)); gap: .75rem;
         margin: 1.5rem 0; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 8px;
        padding: .8rem .9rem; }
.card .n { font-size: 1.5rem; font-weight: 600; display: block; line-height: 1.2; }
.card .l { color: var(--muted); font-size: .78rem; text-transform: uppercase;
           letter-spacing: .05em; }
.finding { border-left: 3px solid var(--line); padding: .6rem .9rem; margin: .5rem 0;
           background: var(--panel); border-radius: 0 6px 6px 0; }
.finding.error { border-left-color: var(--error); }
.finding.warning { border-left-color: var(--warning); }
.finding.info { border-left-color: var(--info); }
.finding .head { display: flex; gap: .6rem; align-items: baseline; flex-wrap: wrap; }
.finding .kind { font-weight: 600; }
.finding .where { color: var(--muted); font-size: .85rem; }
.finding .detail, .finding .fix { margin: .35rem 0 0; font-size: .88rem; color: var(--muted); }
.finding .fix { color: var(--fg); }
.tag { display: inline-block; padding: .1rem .45rem; border-radius: 4px; font-size: .75rem;
       background: var(--line); color: var(--fg); }
.sev-error { color: var(--error); font-weight: 600; }
.sev-warning { color: var(--warning); font-weight: 600; }
.sev-info { color: var(--info); }
.banner { padding: .8rem 1rem; border-radius: 6px; margin: 1rem 0; font-size: .92rem;
          border: 1px solid var(--line); background: var(--panel); }
.banner.bad { border-left: 3px solid var(--error); }
.banner.good { border-left: 3px solid var(--ok); }
.empty { color: var(--muted); font-style: italic; padding: .5rem 0; }
details { margin: .4rem 0; }
summary { cursor: pointer; padding: .3rem 0; font-size: .9rem; }
pre { background: var(--panel); border: 1px solid var(--line); border-radius: 6px;
      padding: .7rem .8rem; overflow-x: auto; margin: .4rem 0; }
.right { text-align: right; font-variant-numeric: tabular-nums; }
footer { margin-top: 4rem; padding-top: 1rem; border-top: 1px solid var(--line);
         color: var(--muted); font-size: .82rem; }
"""


def _esc(value: object) -> str:
    """HTML-escape any value for safe interpolation."""
    return html.escape(str(value), quote=True)


def _card(number: object, label: str) -> str:
    return (
        f'<div class="card"><span class="n">{_esc(number)}</span>'
        f'<span class="l">{_esc(label)}</span></div>'
    )


def _finding_html(finding: Finding) -> str:
    where = " · ".join(_esc(part) for part in (finding.sheet, finding.location) if part)
    parts = [
        f'<div class="finding {finding.severity.value}">',
        '<div class="head">',
        f'<span class="kind">{_esc(finding.kind.value.replace("_", " "))}</span>',
        f'<span class="sev-{finding.severity.value}">{_esc(finding.severity.value)}</span>',
    ]
    if where:
        parts.append(f'<span class="where mono">{where}</span>')
    if finding.affected_cells:
        parts.append(f'<span class="tag">{finding.affected_cells:,} cells</span>')
    parts.append("</div>")
    parts.append(f"<div>{_esc(finding.message)}</div>")
    if finding.detail:
        parts.append(f'<p class="detail">{_esc(finding.detail)}</p>')
    if finding.remediation:
        parts.append(f'<p class="fix">→ {_esc(finding.remediation)}</p>')
    parts.append("</div>")
    return "".join(parts)


def _table(headers: Iterable[str], rows: Iterable[Iterable[str]], empty: str) -> str:
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    if not body:
        return f'<p class="empty">{_esc(empty)}</p>'
    head = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _reconciliation_banner(analysis: WorkbookAnalysis) -> str:
    """State plainly whether this workbook can be verified at all.

    PLAN §1.5 and §4.5 both insist this is reported loudly: a workbook written by a tool rather
    than saved by Excel has no cached values, which disables reconciliation entirely. Silently
    treating that as "fine" is the most dangerous failure mode in the project.
    """
    coverage = analysis.cached_values
    if coverage.status == "not_applicable":
        return (
            '<div class="banner">No formula cells, so there is nothing to reconcile against.</div>'
        )
    if not coverage.reconcilable:
        return (
            '<div class="banner bad"><strong>Not reconcilable.</strong> This workbook carries no '
            "cached calculated values, which means it was written by a tool rather than saved by "
            "Excel. There is no baseline to check a translation against. Open it in Excel and "
            "save it to populate the cache before relying on reconciliation.</div>"
        )
    pct = coverage.coverage * 100
    if coverage.status == "partial":
        return (
            f'<div class="banner">Partially reconcilable — cached values present for '
            f"{coverage.cached_present_count:,} of {coverage.formula_cell_count:,} formula cells "
            f"({pct:.1f}%). Regions without a cache cannot be verified.</div>"
        )
    return (
        f'<div class="banner good">Reconcilable — cached values present for '
        f"{coverage.cached_present_count:,} of {coverage.formula_cell_count:,} formula cells "
        f"({pct:.1f}%). A translation of this workbook can be checked against evidence.</div>"
    )


def render_html(analysis: WorkbookAnalysis) -> str:
    """Render the analysis as one self-contained HTML document.

    Args:
        analysis: The completed workbook analysis.

    Returns:
        A complete HTML document with no external resource references.
    """
    wb = analysis.workbook
    findings = sorted(analysis.findings, key=lambda f: (_SEVERITY_ORDER[f.severity], f.kind.value))
    counts = {s: sum(1 for f in findings if f.severity is s) for s in Severity}

    sheet_rows = [
        (
            _esc(s.name)
            + (' <span class="tag">hidden</span>' if s.is_hidden or s.is_very_hidden else ""),
            _esc(s.role.value),
            f'<span class="right">{s.max_row:,}</span>',
            f'<span class="right">{s.max_column:,}</span>',
            f'<span class="right">{s.formula_cell_count:,}</span>',
            f'<span class="right">{len(s.operation_ids)}</span>',
            _esc(", ".join(s.role_signals[:3])),
        )
        for s in analysis.sheets
    ]

    op_rows = [
        (
            f"<code>{_esc(o.id)}</code>",
            _esc(o.sheet),
            f"<code>{_esc(', '.join(o.ranges[:2]))}</code>",
            f'<span class="right">{o.cell_count:,}</span>',
            _esc(o.excel_pattern.value),
            _esc(o.description),
            (
                f'<span class="sev-error">{len(o.inconsistencies)}</span>'
                if o.inconsistencies
                else '<span class="right">—</span>'
            ),
        )
        for o in analysis.operations
    ]

    conn_rows = [
        (
            _esc(c.name),
            _esc(c.kind),
            f"<pre>{_esc(c.command)}</pre>" if c.command else '<span class="empty">none</span>',
            _esc(c.connection_string_redacted or ""),
        )
        for c in analysis.connections
    ]

    pq_rows = [
        (
            _esc(q.name),
            f"<details><summary>M source</summary><pre>{_esc(q.m_source)}</pre></details>",
            _esc(", ".join(q.referenced_queries)),
        )
        for q in analysis.power_query.queries
    ]

    name_rows = [
        (
            _esc(n.name),
            f"<code>{_esc(n.refers_to)}</code>",
            _esc(n.scope or "workbook"),
            '<span class="sev-error">broken</span>' if n.is_broken else "ok",
        )
        for n in analysis.named_ranges
    ]

    note_rows = [
        (_esc(n.source), _esc(n.origin), _esc(n.heading or ""), _esc(n.text[:400]))
        for n in analysis.notes
    ]

    compression = ""
    total_formula_cells = sum(s.formula_cell_count for s in analysis.sheets)
    if analysis.operations and total_formula_cells:
        ratio = total_formula_cells / len(analysis.operations)
        compression = (
            f"<p class='sub'>{total_formula_cells:,} formula cells compress to "
            f"{len(analysis.operations)} logical operations — a factor of {ratio:.0f}. "
            f"Those operations, not the cells, are what a conversion has to reproduce.</p>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>kedge inspect — {_esc(wb.filename)}</title>
<style>{_STYLE}</style>
</head>
<body>
<main>
  <h1>{_esc(wb.filename)}</h1>
  <p class="sub">
    {_esc(wb.file_format)} · {wb.size_bytes:,} bytes · sha256 <code>{_esc(wb.sha256[:16])}…</code>
    · analysed {_esc(analysis.generated_at.strftime("%Y-%m-%d %H:%M"))}
    · kedge {_esc(analysis.kedge_version)}
  </p>

  <div class="cards">
    {_card(len(analysis.sheets), "sheets")}
    {_card(len(analysis.operations), "operations")}
    {_card(f"{total_formula_cells:,}", "formula cells")}
    {_card(len(analysis.connections), "connections")}
    {_card(len(analysis.power_query.queries), "power queries")}
    {_card(len(findings), "findings")}
  </div>

  {_reconciliation_banner(analysis)}

  <h2>Findings</h2>
  <p class="sub">
    <span class="sev-error">{counts[Severity.ERROR]} error</span> ·
    <span class="sev-warning">{counts[Severity.WARNING]} warning</span> ·
    <span class="sev-info">{counts[Severity.INFO]} info</span>
  </p>
  {
        "".join(_finding_html(f) for f in findings)
        or '<p class="empty">Nothing flagged. That is unusual for a workbook of any age — check the analyser ran fully.</p>'
    }

  <h2>Sheets</h2>
  {
        _table(
            ["Sheet", "Role", "Rows", "Cols", "Formula cells", "Operations", "Why this role"],
            sheet_rows,
            "No sheets found.",
        )
    }

  <h2>Logical operations</h2>
  {compression}
  {
        _table(
            ["Id", "Sheet", "Ranges", "Cells", "Pattern", "What it does", "Inconsistent"],
            op_rows,
            "No formula regions found.",
        )
    }

  <h2>External data</h2>
  <h3>Connections</h3>
  {_table(["Name", "Kind", "Command", "Connection"], conn_rows, "No external connections.")}
  <h3>Power Query</h3>
  {
        _table(
            ["Query", "M source", "References"],
            pq_rows,
            f"No Power Query found (status: {_esc(analysis.power_query.status.value)}).",
        )
    }

  <h2>Named ranges</h2>
  {_table(["Name", "Refers to", "Scope", "State"], name_rows, "No defined names.")}

  <h2>Process notes</h2>
  {
        _table(
            ["Source", "Origin", "Heading", "Text"],
            note_rows,
            "No process documentation found in the workbook or alongside it.",
        )
    }

  <footer>
    Generated by kedge. This report is self-contained and makes no external requests.
    Raw cell values are deliberately not included.
  </footer>
</main>
</body>
</html>
"""


def write_report(analysis: WorkbookAnalysis, path: Path) -> Path:
    """Write the HTML report to `path`, creating parent directories as needed.

    Args:
        analysis: The completed workbook analysis.
        path: Destination `.html` file.

    Returns:
        The path written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(analysis), encoding="utf-8")
    logger.info("wrote report to %s", path)
    return path
