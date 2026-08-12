# `kedge inspect`, worked through

PLAN M1 pitches the analyser as the half of kedge that stands on its own:

> Independently valuable even if the AI half never ships: it's an Excel archaeology tool. Anyone
> who has inherited a 15-tab workbook wants this. [...] That findings list is the "extra control"
> pitch made concrete. Lead with it.

This document leads with it. Two committed fixtures, the real command, real output. No model, no
network, nothing written anywhere near the workbook unless you ask for it.

Everything here was produced by running the two commands in this document — with an absolute
temporary path in place of `$tmp` — on Windows 11 / Python 3.13 with kedge at commit `6138b53`,
and reproduced identically against the working tree afterwards. Filesystem paths and two
over-long formulas are shortened with `...`, and the log timestamps are trimmed off. Where a block
condenses several JSON fields into a line, it says so; nothing here was invented, and every figure
came out of a run.

## Running it

```bash
tmp="${TMPDIR:-/tmp}"

uv run kedge inspect tests/fixtures/clean_pipeline.xlsx \
  --out    "$tmp/clean_pipeline.json" \
  --report "$tmp/clean_pipeline.html"
```

On PowerShell, `$tmp = $env:TEMP` and join the lines — the continuation character is a backtick,
not a backslash.

`--out` takes the analysis JSON, `--report` an HTML rendering of the same thing. Give it neither
and the JSON lands in the workbook's own `<name>.kedge/` directory. `--json` prints the analysis to
stdout *as well as* writing it, not instead of it — a bare `kedge inspect x.xlsx --json` still
leaves `x.kedge/analysis.json` beside the workbook — so pass `--out` whenever you care where the
file goes. Send it somewhere temporary while you are reading: generated analysis output does not
belong in a repository, which is why `.gitignore` already excludes `*.kedge/` and `scratch/`.

---

## Part 1 — a workbook that behaves

`clean_pipeline.xlsx` is the corpus's well-behaved case: five sheets, a 500-row data table, a
lookup, a parameter sheet, a calculation sheet and six output metrics.

```
INFO    kedge.analysis.workbook: opened workbook clean_pipeline.xlsx (5 sheets)
INFO    kedge.analysis.analyse: analysing clean_pipeline.xlsx: 5 sheets
INFO    kedge.analysis.graph: dependency graph: 13 regions, 13 edges, 0 cycles
INFO    kedge.analysis.findings: collected 9 findings
INFO    kedge.analysis.docs: read 5 process note(s) from documented_procedure.docx
INFO    kedge.analysis.docs: extracted 6 process note(s)
INFO    kedge.analysis.profile: profiled 7 column(s) of 'Data' over 500 row(s)
INFO    kedge.analysis.profile: profiled 3 column(s) of 'Ref' over 6 row(s)
INFO    kedge.analysis.profile: profiled 2 column(s) of 'Params' over 4 row(s)
INFO    kedge.analysis.profile: profiled 8 column(s) of 'Calc' over 500 row(s)
INFO    kedge.analysis.profile: profiled 2 column(s) of 'Output' over 6 row(s)
INFO    kedge.analysis.analyse: analysed clean_pipeline.xlsx in 0.49s: 13 operations, 10 findings
```

The two finding counts are not a contradiction: `findings.py` collects nine from the workbook
itself, and `analyse` adds the tenth — the companion `.doc` it could not read — after the fact.

### The number that matters: 4,006 formula cells, 13 operations

This is PLAN 2.1 made visible. A workbook does not contain four thousand decisions; it contains
thirteen, filled down. The analyser groups cells by their **R1C1** form — the formula with
references written relative to the cell holding it — so a whole column of `=B2*C2`, `=B3*C3`,
`=B4*C4` is one thing that happened, not five hundred:

| Operation | Cells | R1C1 | Pattern | Range |
|---|---|---|---|---|
| `calc_a2_a501` | 500 | `=Data!RC` | arithmetic | `Calc!A2:A501` |
| `calc_b2_c501` | 1000 | `=Data!RC[1]` | arithmetic | `Calc!B2:C501` |
| `calc_d2_d501` | 500 | `=VLOOKUP(RC[-2],Ref!R2C1:R7C3,2,FALSE)` | `vlookup_exact` | `Calc!D2:D501` |
| `calc_e2_e501` | 500 | `=RC[-2]*(1-RC[-1])` | arithmetic | `Calc!E2:E501` |
| `calc_f2_f501` | 500 | `=IF(Data!RC[1]="AAA",0.2,IF(...` | `if_nested` | `Calc!F2:F501` |
| `calc_g2_g501` | 500 | `=ROUND(RC[-2]*RC[-1],2)` | arithmetic | `Calc!G2:G501` |
| `calc_h2_h501` | 500 | `=ROUND(RC[-1]*capital_ratio,2)` | arithmetic | `Calc!H2:H501` |
| `output_b2` | 1 | `=ROUND(SUM(Calc!RC[1]:R[499]C[1]),2)` | unknown | `Output!B2` |
| `output_b3` | 1 | `=ROUND(SUM(Calc!R[-1]C[5]:R[498]C[5]),2)` | unknown | `Output!B3` |
| `output_b4` | 1 | `=ROUND(SUM(Calc!R[-2]C[6]:R[497]C[6]),2)` | unknown | `Output!B4` |
| `output_b5` | 1 | `=COUNT(Calc!R[-3]C[1]:R[496]C[1])` | aggregate | `Output!B5` |
| `output_b6` | 1 | `=ROUND(SUMIF(Calc!R[-4]C:R[495]C,"Equity",...` | unknown | `Output!B6` |
| `output_b7` | 1 | `=ROUND(AVERAGE(Calc!R[-5]C[2]:R[494]C[2]),4)` | unknown | `Output!B7` |

`calc_b2_c501` is worth a second look: two adjacent columns, a thousand cells, one region. B and C
have the *same* R1C1 (`=Data!RC[1]` — "the cell one to the right, on Data"), so they are one
operation that happens to be two columns wide, and the analyser says so rather than reporting two.

Each operation carries more than the table shows:

```json
{
  "id": "calc_a2_a501",
  "sheet": "Calc",
  "anchor": "A2",
  "ranges": ["Calc!A2:A501"],
  "cell_count": 500,
  "orientation": "column",
  "r1c1": "=Data!RC",
  "sample_a1": "=Data!A2",
  "functions": [],
  "references": [
    {"raw": "Data!A2", "sheet": "Data", "a1": "A2", "is_range": false,
     "absolute_row": false, "absolute_col": false, "is_external": false,
     "external_workbook": null, "resolves": true}
  ],
  "excel_pattern": "arithmetic",
  "description": "Arithmetic over neighbouring cells on Calc, filled down 500 rows, reading from Data.",
  "translation_hint": "col(\"a\") * col(\"b\"), through kedge.xl where empty cells are possible",
  "inconsistencies": [],
  "is_volatile": false,
  "has_external_refs": false,
  "cached_values_present": true,
  "downstream_ref_count": 0
}
```

`description` is the plain-English shape, `translation_hint` is the polars idiom PLAN 2.7 maps the
pattern to, and `references` is what the dependency graph is built from. `sample_a1` keeps one real
formula so you can check the R1C1 against something a human wrote.

### It can be reconciled against

```json
"cached_values": {
  "formula_cell_count": 4006,
  "cached_present_count": 4006,
  "coverage": 1.0,
  "status": "present"
}
```

`status` is one of `present`, `partial`, `absent` or `not_applicable`, and only the first two carry
a baseline worth checking against — `CachedValueCoverage.reconcilable` is exactly
`status in ("present", "partial") and cached_present_count > 0`. Here every one of the 4,006
formula cells holds the value Excel last calculated, so a translation can be checked against
evidence rather than declared finished. Hold that thought for Part 2.

### The findings

Ten, and none of them alarming — which is the point of running this on a workbook you already
trust, because it calibrates what the list looks like when nothing is wrong. Condensed from the
`findings` array to severity, kind, sheet, location and the gist of the message:

```
WARNING  embedded_totals_row   Output   row 2, row 3, row 4
INFO     hardcoded_constant    Calc     F2      embeds the constant(s) 0.2, 0.3, 0.5, 1.5
INFO     dead_region           Calc     A2      (500 cells) is not read by anything else
INFO     dead_region           Output   B2..B7  (1 cell each)
WARNING  unsupported_format    -        procedure_legacy.doc
```

Read them as questions rather than defects:

- **`hardcoded_constant` at `Calc!F2`** is the five-deep rating `IF` chain with its haircuts typed
  into the formula. That is business logic living somewhere nobody can find it, and it is exactly
  the kind of thing that should reach the conversion plan as a parameter.
- **`dead_region`** on the six `Output` metrics is expected — outputs are terminal by definition,
  which is what "nothing reads this" means for them. On `Calc!A2` it is more interesting: a
  500-cell passthrough column nothing downstream uses.
- **`unsupported_format`** is the analyser telling you it looked beside the workbook, found
  `procedure_legacy.doc`, and could not read it. Not a failure — a stated gap, with the
  `soffice --headless --convert-to docx` command in its `remediation` field.

---

## Part 2 — the same command on a workbook that does not behave

`hostile.xlsx` is the fixture built to break things: merged cells, preamble rows, a totals row in
the middle of the data, duplicate headers, numbers stored as text, a genuine circular reference, a
`#REF!`, an unresolvable external workbook link, a defined name over a deleted range, and a hidden
sheet.

```bash
uv run kedge inspect tests/fixtures/hostile.xlsx \
  --out "$tmp/hostile.json" --report "$tmp/hostile.html"
```

```
INFO    kedge.analysis.workbook: opened workbook hostile.xlsx (5 sheets)
INFO    kedge.analysis.analyse: analysing hostile.xlsx: 5 sheets
INFO    kedge.analysis.graph: dependency graph: 21 regions, 9 edges, 1 cycles
INFO    kedge.analysis.findings: collected 40 findings
INFO    kedge.analysis.docs: read 5 process note(s) from documented_procedure.docx
INFO    kedge.analysis.docs: extracted 6 process note(s)
INFO    kedge.analysis.profile: profiled 10 column(s) of 'Messy' over 78 row(s)
INFO    kedge.analysis.profile: profiled 2 column(s) of 'Volatile' over 8 row(s)
INFO    kedge.analysis.profile: profiled 2 column(s) of 'Circular' over 2 row(s)
INFO    kedge.analysis.profile: profiled 1 column(s) of 'Broken' over 4 row(s)
INFO    kedge.analysis.profile: profiled 2 column(s) of '_Archive' over 12 row(s)
INFO    kedge.analysis.analyse: analysed hostile.xlsx in 0.13s: 21 operations, 41 findings
```

**Note what did not happen.** No traceback, no partial file, no extractor that gave up and took the
run with it. A complete `WorkbookAnalysis` with every section populated, in 0.13 seconds. That is
CONVENTIONS rule 4 — absent and unparseable are first-class results — and it is the single property
that makes the analyser usable on a workbook nobody understands.

### The banner you want to see

```json
"cached_values": {
  "formula_cell_count": 94,
  "cached_present_count": 0,
  "coverage": 0.0,
  "status": "absent"
}
```

and, as an `error`-severity finding:

> **missing cached values** — None of the 94 formula cells carry a cached value, so the workbook
> cannot be reconciled against.
>
> *detail:* openpyxl never calculates anything: it reports the values Excel last saved. An empty
> cache means this file was written by a tool rather than saved by Excel.
>
> *remediation:* Open the workbook in Excel, allow it to calculate, and save it. Until then any
> reconciliation must report 'not reconciled', never 'passed'.

The HTML report says the same thing above the fold, before any of the other 40 findings:

> **Not reconcilable.** This workbook carries no cached calculated values, which means it was
> written by a tool rather than saved by Excel. There is no baseline to check a translation
> against. Open it in Excel and save it to populate the cache before relying on reconciliation.

This is the most dangerous failure mode in the project (PLAN 6.2, CLAUDE.md non-negotiable 6): a
reconciliation with no baseline that reports "passed" is worse than no reconciliation at all,
because someone will believe it. The analyser's job is to make the absence loud at the point where
it is first knowable, which is here.

### 41 findings, sorted by how much they should worry you

```
4 error   ·  19 warning  ·  18 info
```

The four errors:

| Kind | Where | What it says |
|---|---|---|
| `missing_cached_values` | 94 cells | Nothing to reconcile against, as above |
| `circular_reference` | `Circular!C2` | Circular reference across 3 cell(s): `C2 -> C4 -> C3` |
| `error_value` | `Messy!K9` | 1 cell evaluating to an Excel error (`K9=#REF!`) |
| `unresolvable_external_link` | `xl/externalLinks/_rels/externalLink1.xml.rels` | `file:///Z:/retired/vendor_rates_2019.xlsx` cannot be found from here |

Each carries a `remediation` written for the person holding the workbook rather than for the
analyser's author. The circular one, for instance:

> Excel resolves this by iterating to convergence. There is no polars equivalent, so the
> translation has to state the intent explicitly. → Stop and ask the process owner what this loop
> is meant to compute (PLAN §2.7).

And the external link:

> Formulas reading from it fall back on values cached the last time the link resolved, which may be
> arbitrarily stale. → Obtain the linked workbook and treat it as a hand-in, or replace the link
> with the values it supplies.

The nineteen warnings cover `inconsistent_formula` (2), `iferror_swallow` (2), `volatile_function`
(7), `external_link` (2), `merged_cells`, `duplicate_header`, `embedded_totals_row`,
`text_formatted_number`, `broken_named_range` and `unsupported_format`. The eighteen `info`
findings are fifteen `dead_region`s, a `hardcoded_constant`, a `hidden_column` and a
`hidden_sheet` — the archaeology, not the alarm.

### The one that pays for the whole tool

`Messy!D5:D80` is a column of `=B*C`, filled down. Someone, once, typed over row 47. The operation
as the analyser records it, with the fields already shown in Part 1 left out:

```json
{
  "id": "messy_d5_d80",
  "anchor": "D5",
  "ranges": ["Messy!D5:D29", "Messy!D31:D44", "Messy!D46", "Messy!D48:D59", "Messy!D61:D80"],
  "cell_count": 72,
  "r1c1": "=RC[-2]*RC[-1]",
  "sample_a1": "=B5*C5",
  "inconsistencies": [
    {"cell": "D30", "expected_r1c1": "=RC[-2]*RC[-1]",
     "actual_r1c1": "=SUM(R[-25]C:R[-1]C)",
     "note": "a different formula sits inside an otherwise uniform region"},
    {"cell": "D47", "expected_r1c1": "=RC[-2]*RC[-1]",
     "actual_r1c1": "=RC[-2]*RC[-1]*1.1",
     "note": "a different formula sits inside an otherwise uniform region"}
  ]
}
```

Read the `ranges` list: the region is 76 rows with four holes in it. Two are the blank rows at 45
and 60. The other two are `D30` — a `SUM` of the 25 rows above it, a totals row sitting *inside*
the data — and `D47`, which is the same formula as its neighbours with a `*1.1` on the end.

Nobody scrolling this sheet would see either. `D47` is the classic "someone typed over row 47" bug,
and its neighbours `D46` and `D48` are untouched so the column looks perfectly uniform. The
finding's remediation puts the decision where it belongs:

> Decide which formula is correct: one of the two is almost certainly a mistake, and translating
> the region will silently pick one.

That is the "extra control" pitch, concretely: the analyser found in 130 milliseconds a
discrepancy that survives every visual inspection of the sheet.

### Profiles as corroboration

The `profiles` array carries one entry per column, and `format_anomalies` is where the data-quality
traps surface independently of the formulas:

```json
{"sheet": "Messy", "column": "E", "header": "Notes", "dtype": "string",
 "row_count": 78, "null_count": 5, "distinct_count": 73,
 "format_anomalies": ["numbers stored as text (73 cells, 100% of the column) -- a silent join and arithmetic hazard"]}

{"sheet": "Messy", "column": "G", "header": "Booked", "dtype": "string",
 "row_count": 78, "null_count": 5, "distinct_count": 5,
 "format_anomalies": ["mixed date formats (3 text date layouts detected)"]}

{"sheet": "Messy", "column": "K", "header": null, "dtype": "string",
 "row_count": 78, "null_count": 76, "distinct_count": 2,
 "format_anomalies": ["Excel error values (1 cells)"]}
```

Column E is a header that says "Notes" over a column that is 100% numbers-stored-as-text. Excel
coerces those on the fly and polars does not, which is how a sum silently comes out wrong — hence
the `text_formatted_number` finding pointing at the same column from the other direction.

### Absent is a result, not a failure

```json
"connections_status": "absent",
"power_query": {"status": "absent", "part_path": null, "queries": [],
                "section_m_bytes": null, "detail": null}
```

`hostile.xlsx` has no external connections and no Power Query, and the analyser says so with a
status rather than an empty list that could equally mean "did not look" or "could not read". Every
extractor reports one of `ok` / `absent` / `unparseable` / `skipped`, and the third is the one that
matters: a `DataMashup` that will not decode produces `unparseable` plus a `Finding`, never an
exception. Compare `named_ranges`, where a broken name is reported *as* a name:

```json
[{"name": "obsolete_rate_table", "refers_to": "#REF!#REF!", "is_broken": true,
  "is_hidden": false, "reference_count": 1},
 {"name": "messy_amounts", "refers_to": "Messy!$B$5:$B$80", "is_broken": false,
  "is_hidden": false, "reference_count": 0}]
```

---

## What is in the analysis JSON

Sixteen top-level keys, stable under `schema_version`.

| Key | What it holds |
|---|---|
| `schema_version` | `"1.0"` — the contract in `kedge.analysis.model`, not the kedge version |
| `kedge_version`, `generated_at`, `analysis_duration_seconds` | Provenance for the run |
| `workbook` | Path, filename, `sha256`, size, format, mtime, `created_by`, `has_vba`, `iterative_calculation` |
| `sheets` | Per sheet: role and confidence, the signals behind the role, used range, formula/value/blank/text cell counts, header row, preamble rows, hidden state, hidden columns, merged ranges, embedded totals, and the ids of the operations on it |
| `operations` | The R1C1-compressed logical operations, as above |
| `graph` | `nodes`, `edges`, `cycles`, `fan_out`, `topological_order` (`null` when there is a cycle), `roots`, `leaves` |
| `connections` / `connections_status` | External data connections with their SQL, and whether the extractor found anything |
| `power_query` | The `DataMashup` status, the part it was found in, and the M queries |
| `named_ranges` | Defined names, what they refer to, whether they resolve, how often they are used |
| `notes` | Process notes recovered from documentation sheets, cell comments and companion Word files |
| `profiles` | Per column: dtype, row/null/distinct counts, numerics, top-k, head/tail/sample, format anomalies |
| `cached_values` | The reconciliation baseline: formula cell count, how many carry a value, coverage, status |
| `findings` | The list. `kind`, `severity`, `message`, `sheet`, `location`, `detail`, `affected_cells`, `remediation` |

The `sha256` in `workbook` is what makes an analysis citable: it identifies the exact bytes the
findings were derived from, so a later run against a changed workbook is visibly a different run.

## The HTML report

`--report` renders the same analysis as a single self-contained file — about 39KB for
`hostile.xlsx`, no assets, opens from disk. It leads with the identity line and the counts:

```
hostile.xlsx
xlsx · 12,532 bytes · sha256 d317bb195f1d332a… · analysed 2026-08-11 23:12 · kedge 0.0.1

5 sheets · 21 operations · 94 formula cells · 0 connections · 0 power queries · 41 findings
```

then the reconcilability banner, then Findings, Sheets, Logical operations, External data, Named
ranges and Process notes. Findings come before everything else deliberately: it is the section
somebody who has just inherited the workbook actually needs.

## What this does not tell you

Said plainly, so nobody over-reads it:

- **These fixtures are synthetic.** They are built to span the axes M1 has to handle, not sampled
  from real work. `tests/fixtures/README.md` is explicit about the limits, including that no
  fixture is genuinely Excel-authored and none has a corrupt part, so the `unparseable` path is
  exercised only on structurally valid input.
- **Findings are heuristics, not verdicts.** `dead_region` on a terminal output is not a defect.
  Severity ranks how much a human should look, not how wrong the workbook is.
- **Sheet roles are confidence-scored guesses**, and the corpus manifest allows alternatives for
  genuinely ambiguous sheets — a six-row lookup table is defensibly `parameters` or `data`. Read
  `role_signals` rather than `role` when it matters.
- **`kedge_version` comes from the installed distribution metadata**
  (`importlib.metadata.version("kedge")`), so in a source checkout it can lag `kedge.__version__`
  until the editable install is refreshed. The runs above reported `0.0.1` from a tree whose
  `kedge --version` said `0.0.3`.
- **The analysis is not the data.** Profiles carry a handful of sampled values and a top-k by
  design (PLAN 2.3), but the bulk cached values stay in the workbook and are read on demand
  through the workbook handle. That is why a 4,006-formula-cell workbook produces a 56KB artifact
  rather than a megabyte of numbers. It is not *free* of data, though — head, tail and sample rows
  are real cell values — so treat the JSON as carrying a little of the workbook rather than none
  of it before attaching it to anything.
