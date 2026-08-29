# kedge — Implementation Plan

**Purpose:** Turn manual Excel processes into reviewable, reproducible marimo notebooks, with an AI copilot that operates the notebook through a controlled tool surface.

**Status:** Greenfield. This document is the brief for Claude Code. Read §1 before writing any code — several findings here contradict the obvious design.

---

## 1. Verified technical findings

Everything in this section was checked against marimo 0.23.15 and openpyxl 3.1.5, not recalled. Re-verify on version bumps.

### 1.1 The marimo bridge exists and is usable — but it is a private API

`marimo._code_mode` is real. Its own docstring says:

> **Internal, agent-only API.** Not part of marimo's public API. No versioning guarantees. May change or be removed without notice.

Confirmed surface on `AsyncCodeModeContext` (0.23.15):

| Member | Signature (abbreviated) |
|---|---|
| `cells` | property — read-only ordered view; index by position, id, or name |
| `graph` | property — dataflow graph; `.cells[cid].defs` / `.refs`, `.ancestors()`, `.descendants()` |
| `globals` | property — kernel global namespace |
| `packages` | property — `.add()` / `.remove()` |
| `create_cell` | `(code, *, before, after, hide_code=True, disabled, column, name) -> CellId_t` |
| `edit_cell` | `(target, code=None, *, hide_code, disabled, column, name)` |
| `delete_cell` | `(target)` |
| `move_cell` | `(target, *, before, after)` |
| `run_cell` | `(target)` |
| `set_ui_value` | `(element, value)` |
| `broadcast_raw_notification` | `(notification)` — push a message into the notebook UI |
| `screenshot` | `(target, ...)` — needs Playwright |

Semantics that matter:

- `async with cm.get_context() as ctx:` is **mandatory**. Without it, operations silently no-op.
- Methods inside the block are **synchronous** — they queue. Do not `await` them. The context manager flushes on clean exit; if the block raises, the queue is discarded.
- `create_cell` / `edit_cell` are **structural only**. Nothing executes until `run_cell`.
- `create_cell` defaults to `hide_code=True`. Kedge must pass `hide_code=False` — the whole point is that the user can read and edit what the AI wrote.
- The **`name` parameter is the most important thing here.** Named cells give stable addressing (`ctx.cells["load_handin"]`) across turns. Cell IDs are opaque and churn. Kedge should name every cell it creates, from a controlled vocabulary. This is the difference between an agent that can reliably edit its own work and one that can't.

### 1.2 How to reach the kernel from outside

marimo exposes an HTTP API. Kedge's backend talks to it directly — no marimo import needed on the server side.

- **Server registry:** `${XDG_STATE_HOME:-~/.local/state}/marimo/servers/*.json` (Windows: `~/.marimo/servers`). Fields: `pid`, `host`, `port`, `base_url`, `server_id`.
- `GET {base}/health` — liveness (use this on Windows; PID checks are unreliable there).
- `GET {base}/api/sessions` — dict keyed by session id, values include `filename`.
- `POST {base}/api/kernel/execute` — headers `Marimo-Session-Id: <id>`, `Authorization: Bearer <token>`; body `{"code": "..."}`. Returns **SSE**: `stdout` / `stderr` events carrying `{"data": "..."}`, then a terminal `done` event `{"success": bool, "error": {"msg": ...}, "output": {"data": ...}}`.

Code posted here runs in a **scratchpad**: a shallow copy of kernel globals. Notebook variables are readable by name; new top-level bindings are discarded after the call. This is why persistence must go through `cm`. It's also a useful property — Kedge can inspect state cheaply without polluting the notebook.

Reference implementation to crib from: `github.com/marimo-team/marimo-pair`, `skills/marimo-pair/scripts/execute-code.sh`.

### 1.3 The iframe works, with one trap

- marimo sets `X-Frame-Options: DENY` **only on the login endpoint** (`_server/api/endpoints/login.py`). Notebook pages set no frame-busting headers, so embedding is fine.
- The trap: if the iframe loads unauthenticated, it lands on the login page, which refuses to frame. Fix: `access_token` is accepted as a **query parameter** (`TOKEN_QUERY_PARAM = "access_token"` in `_server/api/auth.py`). Launch with `--token-password <generated>` and frame `http://127.0.0.1:<port>/?file=<nb>.py&access_token=<token>`. The login page is never reached.
- Relevant `marimo edit` flags: `--headless`, `--port`, `--token-password`, `--base-url`, `--proxy`, `--allow-origins`, `--watch`, `--session-ttl`, `--sandbox`.

### 1.4 `create_asgi_app` will not work here

It's tempting to mount marimo inside the Kedge FastAPI app for a single-origin deployment. Its docstring: *"This only works for application that are in Run mode."* Run mode hides code and disables editing. Kedge needs edit mode. **Use a separate `marimo edit` process.** If single-origin is wanted later, reverse-proxy it (HTTP + WebSocket) behind `--base-url /notebook --proxy ...`.

### 1.5 Excel: what's extractable and how

- **Formulas:** `openpyxl.formula.tokenizer.Tokenizer` is built in and sufficient. Tokens come back typed — `OPERAND/RANGE` gives every cell reference including cross-sheet (`Data!B2:B100`), `FUNC/OPEN` gives the function inventory. No third-party formula library needed for analysis. (`formulas`, `pycel`, `xlcalculator` are *evaluators*; Kedge doesn't need to evaluate, it needs to translate.)
- **Cached calculated values:** `load_workbook(path, data_only=True)` returns the values Excel last computed and cached, rather than the formula strings. **You cannot get both from one load** — Kedge must load the workbook twice and zip the two views together. Verified caveat: openpyxl never calculates anything itself, so if a workbook was written by a tool rather than saved by Excel, `data_only` returns `None` for every formula cell. Detect this (all-`None` formula cells) and report it loudly; silently treating those as zeros would poison the reconciliation in §4.5.
- **SQL from legacy connections:** `xl/connections.xml`, `<dbPr command="SELECT ..."/>` plus the connection string. **openpyxl does not parse this** — read the `.xlsx` as a zip and parse the XML directly.
- **Power Query M code:** stored as a zip-inside-a-zip. Do *not* hardcode `customXml/item1.xml`. Iterate `customXml/item*.xml`, find the part whose schema is `http://schemas.microsoft.com/DataMashup`, base64-decode the `<DataMashup>` element, and unzip it; the M source for every query is concatenated in `Formulas/Section1.m`. Format is documented as MS-QDEFF.
- **Word process notes:** `python-docx`. Also handle `.doc` by failing clearly with a conversion hint rather than silently.

---

## 2. Architecture

Three processes, one machine.

```
┌────────────────────────────────────────────────────────────┐
│  Browser  —  http://127.0.0.1:8000                         │
│  ┌──────────────────────┐  ┌─────────────────────────────┐ │
│  │  Chat pane           │  │  <iframe>                   │ │
│  │  (SSE event stream)  │  │  marimo edit, token in URL  │ │
│  └──────────────────────┘  └─────────────────────────────┘ │
└───────────┬──────────────────────────────┬─────────────────┘
            │                              │ (direct ws/http)
┌───────────▼──────────────┐   ┌───────────▼─────────────────┐
│  kedge server (FastAPI)  │   │  marimo edit --headless     │
│  • analyser              │   │  port 2718                  │
│  • agent loop + tools    │──▶│  POST /api/kernel/execute   │
│  • session store         │   │    → import marimo._code_mode│
│  • context assembly      │   │                             │
└───────────┬──────────────┘   └─────────────────────────────┘
            │
   OpenAI-compatible endpoint (user-supplied base_url + key)
```

The kedge server owns the marimo subprocess: spawns it, holds the token, monitors health, restarts on crash.

### 2.1 The central design decision: deterministic facts, AI-decided plan

There is no canonical workbook. Process shape, decomposition, sequencing and intent vary enough that a fixed scaffold would produce a *transliteration* — 47 cells mirroring 47 columns — which is not an improvement on a spreadsheet, just a spreadsheet written in Python. The AI has to look at what's actually there and decide the approach.

But "the AI decides" should not extend to the primitives. `VLOOKUP(..., FALSE)` is a left join in every workbook that has ever existed; `ROUND` is half-away-from-zero always; R1C1 region detection is mechanical. Letting the model re-derive those per workbook doesn't buy flexibility, it buys variance — and variance in the primitives is precisely what breaks reconciliation. Three layers, with the boundary drawn at *judgement*:

| Layer | Who decides | Varies per workbook? |
|---|---|---|
| **Facts** — regions, DAG, dtypes, cached values, SQL, M code, findings | Analyser, deterministic | No. Same extraction every time |
| **Plan** — stages, decomposition, sequencing, what to drop, what needs a human | **AI, reviewed by the user** | Yes. This is where all the flexibility lives |
| **Code** — the polars expressions implementing each planned stage | AI, from a fixed vocabulary (§2.7), validated | Vocabulary fixed; composition free |

The analyser gets *more* important under this model, not less. The better the facts, the better the plan — and a model reasoning over a clean structural summary makes far better decisions than one squinting at raw cells.

**The R1C1 compression trick underpins the facts layer.** Normalise every formula to R1C1 relative form. A contiguous block sharing an identical R1C1 string is *one logical operation*, regardless of whether it spans 5 rows or 200,000. A 40MB workbook with half a million formula cells typically collapses to 30–60 distinct operations. That collapsed set is what the plan is written against.

### 2.2 The process plan is a reviewable artifact

Before any code is written, the AI produces a **plan** — and the user approves or edits it. This is the single most important addition that flexibility demands, because if the model is deciding the approach, the user needs to see the approach at the point where correcting it is cheap. Reviewing a twelve-line plan is minutes; reviewing forty generated cells to work out that the decomposition was wrong is an afternoon.

```yaml
workbook: rwa_monthly_v14.xlsx
assessment:
  convertible: 0.8              # proportion of logic the model believes it can translate
  blockers: [ "Sheet 'Adjustments' has 14 manually-typed overrides with no formula" ]
stages:
  - id: load_handin
    intent: "Read counterparty exposures from the hand-in"
    sources: [handin]
    confidence: high
    assumptions: ["header on row 1", "one row per counterparty"]
  - id: apply_haircuts
    intent: "Collateral haircut lookup by asset class"
    sources: [ "Calc!H2:H50000", "Ref!A1:D50" ]
    excel_pattern: VLOOKUP_exact
    confidence: high
  - id: manual_overrides
    intent: "Overrides typed by the analyst each month"
    kind: checkpoint            # NOT automated — see below
    confidence: n/a
  - id: reconcile
    intent: "Check output against workbook cached values"
open_questions:
  - "Column AF is computed but never referenced. Dead, or read manually?"
dropped:
  - { range: "Calc!AK:AP", reason: "no downstream refs, all zero since 2023" }
```

Properties that matter:

- **Stages are proposed, not fixed.** A workbook that wants four stages gets four; one that wants fifteen gets fifteen. No template.
- **`open_questions` is a first-class field.** The model must be able to say "I don't know what this is for" rather than inventing a purpose. Silent guessing is the failure mode that makes a converted process untrustworthy.
- **`dropped` is explicit and requires acknowledgement.** Workbooks accumulate dead columns; removing them is a genuine improvement, but only if it's declared. Silent removal is indistinguishable from a bug.
- **The plan is saved, versioned, and diffable** alongside the notebook. When the process changes next quarter, the diff of the plan is the change record — and it re-seeds the model on a similar workbook rather than starting cold.

**Checkpoint stages are the flexibility escape hatch.** A real process contains steps that are not automatable: a judgement call, an override agreed with another team, a "check this looks sensible before continuing". Forcing these into code either fabricates logic that was never there or silently drops a control. Instead, `kind: checkpoint` scaffolds an explicit `mo.ui` approval cell — the user records a decision and a note, and downstream cells stay blocked until they do. That's *better* than the Excel original, where the same step was someone typing a number with no record of why.

**Triage before planning.** Not every workbook should be converted. Heavy VBA, genuine circularity, or a process that is mostly human judgement with a spreadsheet attached are all legitimate "don't". An honest `convertible: 0.4` with a list of blockers is far more useful than a notebook that looks complete and isn't.

### 2.3 The model sees data — but through a sampling layer, never in bulk

The model has full access to cell values. This is a deliberate choice: in Excel the logic/data boundary genuinely collapses (a hardcoded rate table on a Parameters sheet *is* business logic), and a model that can see values can verify its own translation instead of guessing at it. §4.5 is only possible because of this.

"Sees the data" cannot mean "the data goes in the prompt", though — a 200,000-row sheet doesn't fit and wouldn't help if it did. It means the model gets a **profile by default and values on request**:

*Always in context, per data region:*
- Column name, inferred dtype, null count, distinct count
- Numeric: min / max / mean / sum
- Categorical: top-k values with frequencies
- Head and tail rows (default 5 each), plus a random sample of 5

*Available on request, via tools:*
- `sample_data(sheet, rows, columns, where=None)` — bounded slice, hard row cap
- `profile_column(sheet, column)` — full distribution, outliers, format anomalies
- `read_range(sheet, "A1:D50")` — verbatim values, for parameter and lookup tables
- `probe(code)` — scratchpad execution against the live kernel, returning real values

`probe` is the important one. Once the data is loaded in the notebook, the model can run `df.filter(...).height`, check whether a join key is unique, or total a column — and get a real answer rather than a plausible one. Most of the practical benefit of data visibility arrives through this tool, not through the prompt.

**Bulk transfer stays blocked.** Every value-returning tool enforces a row cap (default 100) and a payload cap (default 32KB), truncating with an explicit `[… N more rows omitted]` marker. The model can request many small reads; it cannot request the sheet. This is a context-budget control first and a data-handling control second, but it serves both.

**Optional column redaction, off by default.** A column can be marked sensitive — by name pattern in config, or by tag in a knowledge pack — in which case tools return dtype and null count but hash the values. Off by default; when a workbook contains customer identifiers it's one config line rather than a rebuild.

**Log every outbound payload.** `~/.kedge/logs/outbound-<session>.jsonl`: timestamp, tool, sheet, columns, row count, byte count. Not the values themselves. Cheap to build now, and the first thing anyone asks for when the tool gets pointed at something real.

### 2.4 Sheet role classification

Heuristic first, LLM-confirmed:

| Role | Signal |
|---|---|
| `data` | high row count, no formulas, uniform dtypes, header row |
| `calculation` | dense formula regions, references other sheets |
| `parameters` | small, named ranges, mostly literals referenced widely |
| `documentation` | mostly text cells, few/no formulas, long strings |
| `output` | formulas referencing calculation sheets, formatting-heavy, low fan-out |

### 2.5 Target: polars

Generated code is **polars**, not pandas. Not negotiable, and it should be enforced rather than merely requested — pandas is heavily over-represented in training data, so a model told "prefer polars" will still drift back to it under pressure. Add a check to the validation gate (§M4): reject any cell importing pandas unless an explicit escape hatch is set.

House rules for generated code, to be stated in the system prompt *and* enforced where checkable:

- **`LazyFrame` throughout; `.collect()` only at boundaries** — the reconciliation cell, the final output write, and anywhere a `mo.ui` element needs a materialised frame. Intermediate cells pass lazy frames between each other. This keeps the notebook's cell graph and polars' query plan aligned, which is what makes the whole thing scale past the point where Excel gave up.
- **Expressions over loops.** An Excel formula region is a column operation by construction — the R1C1 compression in §2.1 has already established that. If the model emits `for row in ...`, the translation of that region is wrong, not merely slow.
- **`pl.read_excel` defaults to the `calamine` engine** (verified on polars 1.43) — fast, and no Excel install needed. Use it for hand-in loading.
- **Name every intermediate frame after the business step**, not `df2` / `tmp`. This is the readability half of the pitch; a notebook nobody can review is not an improvement on a spreadsheet nobody can review.

### 2.6 Excel semantics that polars does not share

This is the reconciliation-breaker list. Every item below was verified against polars 1.43.0, and every one of them produces silently wrong numbers rather than an error. Ship this as a checklist in the system prompt, and reach for it first whenever §4.5 reports a small unexplained delta.

| Behaviour | Excel | polars | Consequence |
|---|---|---|---|
| Rounding at `.5` | half away from zero: `0.5→1`, `2.5→3` | banker's, half-to-even: `0.5→0`, `2.5→2` | **The single most likely source of pennies-out reconciliation failures.** Never translate `ROUND()` to `.round()` |
| Empty cell in arithmetic | treated as `0`: `1 + <empty> = 1` | null propagates: `1 + null = null` | Rows silently become null and vanish from downstream sums |
| Row-wise addition | `=A1+B1+C1` ignores empties | `pl.col("a")+pl.col("b")` propagates null | Use `pl.sum_horizontal(...)`, which matches Excel — verified |
| Column aggregate | `SUM()` ignores empties | `.sum()` ignores nulls | Matches. Safe |
| Divide by zero | `#DIV/0!` error, propagates visibly | `inf` / `-inf` / `nan` | `inf` poisons downstream aggregates quietly. Wrap every division |
| `VLOOKUP(..., FALSE)` | exact match, first hit | — | `join(..., how="left")`; check key uniqueness first or rows multiply |
| `VLOOKUP(..., TRUE)` | approximate, assumes sorted, takes largest ≤ key | — | `join_asof(strategy="backward")` — verified default |
| Text-formatted numbers | coerced on the fly | stays `String` | Silent type mismatch on join keys. Profile dtypes at load |
| Dates | 1900 serial system, with the 1900 leap-year bug | proper temporal types | Off-by-one on pre-1901 dates; rare but real |

**Deliverable: a `kedge.xl` expression namespace.** Register a polars namespace so generated code states its Excel-compatibility intent inline rather than open-coding the workaround each time:

```python
col("amount").xl.round(2)  # half away from zero, matching Excel ROUND
col("a").xl.add(col("b"))  # nulls as zero
col("n").xl.div(col("d"))  # returns null on zero denominator, not inf
col("x").xl.serial_to_date()  # 1900 system, leap bug included
```

This follows the same registered-namespace pattern as `rwa-calc`'s `col.rwa.*` / `col.irb.*`, so it will read as familiar. It also makes the semantic choice **greppable** — a reviewer can see exactly where Excel compatibility was assumed, which is far better than the alternative where it's implicit in a `.round()` that happens to be wrong. Build this in M1 alongside the analyser; the agent's output quality depends on it existing before the agent does.

### 2.7 Formula → polars translation reference

These mappings are deterministic and belong in the codebase as a **vocabulary the plan is written in — not a template the plan must fit**. The AI decides which patterns apply, in what order, and how they compose into stages; it should never be re-deriving that an exact `VLOOKUP` is a left join. Where a workbook does something this table doesn't cover, that's a signal for an `open_question` in the plan (§2.2), not an invitation to improvise silently.

| Excel pattern | polars |
|---|---|
| `=A2*B2`, arithmetic | `col("a") * col("b")` — via `xl.*` where empties are possible |
| `=IF(c, x, y)` | `pl.when(c).then(x).otherwise(y)` |
| nested `IF` chains | chained `.when().then()`, one per branch — flattens deep nesting nicely |
| `=IFERROR(x, y)` | `pl.when(<error cond>).then(y).otherwise(x)` — make the condition explicit; do not swallow |
| `=SUMIF(r, c, s)` | `.filter(c).select(col(s).sum())`, or `group_by().agg()` if the criterion is a key |
| `=SUMIFS` / `COUNTIFS` | `group_by([...]).agg([...])` then join back |
| `=VLOOKUP(k, t, n, FALSE)` | `.join(t, left_on=k, right_on=..., how="left")` |
| `=VLOOKUP(k, t, n, TRUE)` | `.join_asof(t, on=k, strategy="backward")` |
| `=INDEX(r, MATCH(k, m, 0))` | same as exact `VLOOKUP` — a join |
| `=SUMPRODUCT(a, b)` | `(col("a") * col("b")).sum()` |
| running total down a column | `col("x").cum_sum()` |
| `=A2` referencing the row above | `col("x").shift(1)` — window, not a loop |
| absolute ref `$B$1` to a parameter | a Python constant, or a value from the parameters frame |
| whole-column formula fill | one expression on the frame; this is the R1C1 region |
| pivot table | `.group_by(...).agg(...)` or `.pivot(...)` |
| `SUBTOTAL` with filters applied | `.filter(...)` then aggregate — check which rows were actually visible |

Two patterns that should **stop the scaffold and ask**, rather than be guessed: iterative/circular calculation (Excel's iterative-calc setting is on for a reason and there is no clean polars equivalent), and anything reading from an unresolvable external workbook link.

### 2.8 Hand-in ingestion

Two entry points, both native to marimo, verified on 0.23.15 — and they return **different things**, which is the whole design problem:

| Component | Returns | Implication |
|---|---|---|
| `mo.ui.file(kind="area")` | `FileUploadResults(name: str, contents: bytes)` | Drag-and-drop zone. The entire file is held **in kernel memory**. No path. `max_size` defaults to 100MB |
| `mo.ui.file_browser(...)` | `FileBrowserFileInfo(id, path: Path, name, is_directory)` | Server-side picker. **Path only, no bytes.** No size ceiling. `restrict_navigation=True` confines the user to a directory |

**An uploaded file is not reproducible.** Re-open the notebook tomorrow and the bytes are gone — the cell yields nothing and the whole downstream graph is dead. A path-based hand-in re-runs. This is not a small distinction for a tool whose pitch is "controlled, repeatable process", so drop and select must converge before anything downstream sees them.

**Normalise both into a managed store.** `kedge.ingest.receive()` accepts either payload and returns one `HandIn` record:

```python
HandIn(
    path:          Path,     # always the managed copy, never the transient upload
    original_name: str,
    sha256:        str,
    size_bytes:    int,
    received_at:   datetime,
    source:        Literal["dropped", "selected", "watched"],
    contract:      str | None,
)
```

On drop, write the bytes to `handins/<yyyy-mm-dd>/<sha256[:12]>-<name>` and hand back the managed path. On select, hash in place and either reference or copy depending on config — copy by default, since a shared-drive path is not a stable artifact. Downstream cells only ever see a `HandIn`, so notebook code is identical regardless of how the file arrived. The hash is what makes the audit line defensible: *this run consumed this file*.

**Worth adding a third source: a watched location.** The stated flow is a hand-in from another team, which in practice usually means a file landing in a shared folder on a schedule. `source="watched"` with a directory and a glob is the production path; drop and select are the interactive ones. Same `HandIn` record, so it costs very little once the store exists. Flagging as a suggestion, not scope.

**The selector cell sits at the top of the notebook, and swapping it re-runs everything.** This is the moment the conversion pays off — change the hand-in, the contract re-validates, every downstream cell recalculates in dataflow order, and the reconciliation panel goes green or red. That is precisely what the Excel process cannot do, and it's the clearest demonstration of why the notebook is better. Build the selector as a first-class part of the scaffold, not an afterthought.

Suggested cells at the notebook head:

```
handin_source   mo.ui.tabs({"Drop": mo.ui.file(kind="area"), "Select": mo.ui.file_browser(...)})
handin          kedge.ingest.receive(handin_source.value)      # → HandIn
handin_check    kedge.contracts.validate(handin, contract)     # → pass/fail panel
handin_frame    pl.scan_* / pl.read_excel against handin.path  # → LazyFrame
```

**Profile on ingest**, using the same machinery as M1 (§2.3). Two reasons: the user sees what actually arrived before any processing happens, and the agent needs the schema in context to write code against it. A hand-in whose schema the model hasn't seen gets code written against a guessed one.

**Detect shape drift.** Hand-ins from another team change without warning — a column renamed, an extra preamble row, a totals row appended at the bottom, a date column that arrives as text this month. Compare each hand-in's profile against the last accepted one and report differences *before* the contract check, because "column `EAD` became `EAD_GBP`" is a far more useful message than a schema validation traceback. This is a large part of the practical value over the Excel version, where the same drift just produces a quietly wrong number.

### 2.9 Operating model: single-user, local

One person, one machine. Treat this as licence to *not build things*, rather than as a temporary state to engineer around.

**Genuinely deferred — do not build:** user accounts, login, RBAC, multi-tenancy, session isolation, server deployment, scaling, concurrent-edit conflict resolution, shared knowledge-pack governance. All expensive, all speculative, all addable later behind the seams below.

**Cheap now, expensive later — do build:**

- **A `Workspace` object owning paths, config, and the marimo session**, rather than module-level globals. Multi-user later becomes "more workspaces"; global state becomes a rewrite. The one structural concession worth making, and it costs an afternoon.
- **Layered config** — `~/.kedge/config.toml` overridden by a per-project `kedge.toml`. Needed almost immediately regardless, since tolerances and redaction rules are per-process, not per-machine.
- **The outbound audit log** (§2.3). Already in scope; keep it.

**Auth collapses to almost nothing.** Bind the Kedge server to `127.0.0.1` only. Generate a random marimo token per launch, pass via `--token-password`. No accounts, no cookies, no CORS configuration. The model endpoint's API key lives in the OS keyring — Windows Credential Manager here — never in the config file.

**Never auto-discover the marimo server.** The registry scan in `marimo-pair` (§1.2) exists so an agent can attach to whatever notebook a user already has open. Kedge must not do that: anyone running this will plausibly have other marimo notebooks open for unrelated work, and the discovery script's own failure mode is to refuse when it finds more than one server or session. Instead, Kedge **owns** its marimo process — pick a free port at launch, generate the token, record both in the `Workspace`, and always target by explicit `--url` and `--session`. Read the registry only to spot a stale Kedge-owned process from a crashed run, identified by a workspace marker file rather than by guessing.

**Lifecycle.** `kedge open process.xlsx` should be the entire interface: analyse (M1, no network) → scaffold (M2, no LLM) → spawn `marimo edit --headless --port N --token-password ... --watch` → poll `/health`, then `/api/sessions` for the session id → start the Kedge server on loopback → open the browser.

Teardown is what will misbehave. **Windows has no `SIGTERM` semantics**, so a force-quit or crashed Kedge orphans the marimo subprocess and holds the port. Three cheap defences: launch marimo with **`--timeout <minutes>`** (verified flag — shuts the server down after that long with no connection, making orphans self-clearing); write a marker file with PID, port, and token, checked and cleaned on next launch; `atexit` plus signal handlers for the normal paths.

**Distribution:** `uv tool install kedge`, then `kedge open <workbook>`. Matches the existing stack and gives a persistent isolated environment. The `.pyz` zipapp route is worth remembering for locked-down machines, but shouldn't drive initial packaging — get `uv tool install` working and revisit only if the environment forces it.

---

## 3. Repository layout

```
kedge/
├── pyproject.toml              # uv, ruff, pytest; Apache-2.0; OpenAfterHours
├── src/kedge/
│   ├── analysis/               # ── Milestone 1: pure, offline, no LLM
│   │   ├── workbook.py         # zip + openpyxl load
│   │   ├── formulas.py         # Tokenizer → refs, funcs, R1C1 normalisation
│   │   ├── regions.py          # R1C1 grouping → logical operations
│   │   ├── values.py           # dual load (formulas + data_only), cached results
│   │   ├── profile.py          # column profiles, sampling, top-k
│   │   ├── redact.py           # optional column masking (off by default)
│   │   ├── graph.py            # cell-level DAG, cycle + fan-out analysis
│   │   ├── connections.py      # xl/connections.xml → SQL + conn strings
│   │   ├── powerquery.py       # customXml → DataMashup → Section1.m
│   │   ├── docs.py             # python-docx + in-sheet process notes
│   │   ├── classify.py         # sheet role heuristics
│   │   └── model.py            # WorkbookAnalysis (pydantic) — THE CONTRACT
│   ├── xl/                     # ── Milestone 1: Excel-semantics polars namespace
│   │   ├── namespace.py        # registers col(...).xl.*
│   │   ├── rounding.py         # half-away-from-zero
│   │   ├── nulls.py            # empty-as-zero arithmetic
│   │   └── dates.py            # 1900 serial system
│   ├── plan/                   # ── Milestone 2: AI-decided process plan
│   │   ├── model.py            # ProcessPlan schema (stages, questions, dropped)
│   │   ├── propose.py          # analysis → plan, via LLM
│   │   ├── triage.py           # convertibility assessment + blockers
│   │   ├── review.py           # user edit/approve, diff between versions
│   │   └── store.py            # versioned plans alongside the notebook
│   ├── notebook/               # ── Milestone 2: marimo bridge
│   │   ├── discovery.py        # server registry, health, session id
│   │   ├── driver.py           # ONLY module that emits _code_mode code
│   │   ├── supervisor.py       # spawn/monitor marimo edit subprocess
│   │   └── scaffold.py         # plan → notebook cells; shape follows the plan
│   ├── agent/                  # ── Milestone 4
│   │   ├── loop.py             # tool-calling loop, streaming
│   │   ├── tools.py            # tool schemas + dispatch
│   │   ├── validate.py         # pre-commit gate (AST + marimo contract)
│   │   ├── context.py          # budget, assembly, compaction
│   │   ├── reconcile.py        # Python output vs cached Excel values
│   │   ├── audit.py            # outbound payload log
│   │   └── prompts/
│   ├── server/                 # ── Milestone 3
│   │   ├── app.py              # FastAPI
│   │   ├── events.py           # SSE typed event stream
│   │   └── static/             # chat UI + iframe shell
│   ├── ingest/                 # ── Milestone 5: hand-in intake
│   │   ├── receive.py          # upload | browser | watch → HandIn
│   │   ├── store.py            # managed handins/ dir, hashing, dedupe
│   │   ├── drift.py            # profile diff vs last accepted hand-in
│   │   └── watch.py            # optional watched-folder source
│   ├── contracts/              # ── Milestone 5: hand-in validation
│   ├── knowledge/              # ── Milestone 5: context packs loader
│   ├── config.py               # layered ~/.kedge/config.toml + kedge.toml; keyring
│   ├── workspace.py            # owns paths, config, marimo session, marker file
│   ├── lifecycle.py            # spawn/health-poll/teardown, orphan cleanup
│   └── cli.py
├── utils/                      # user-editable, importable from notebooks
├── context/                    # user-editable knowledge packs
└── tests/
    ├── fixtures/               # committed .xlsx covering each pattern
    └── contract/               # live-kernel tests — see §6.1
```

---

## 4. Milestones

### M0 — Skeleton
`uv` project, ruff, pytest, CLI stub, layered config loader (§2.9), `Workspace` object, API key via OS keyring (`keyring` package) — **not plaintext**. Config carries `base_url`, `model`, `api_key_ref`, `max_context_tokens`, reconciliation tolerances, redaction rules.

No auth, no accounts, loopback binding only. Single-user local (§2.9) means M0 is genuinely small — resist the urge to build a user model "for later".

### M1 — The analyser (build this first, ship it standalone)

`kedge inspect process.xlsx --out analysis.json --report report.html`

Independently valuable even if the AI half never ships: it's an Excel archaeology tool. Anyone who has inherited a 15-tab workbook wants this. It also de-risks everything downstream, because M4's quality is bounded by M1's quality.

Deliverables: `WorkbookAnalysis` populated with sheets + roles, logical operations (R1C1-compressed, with source ranges and a plain-English description of the formula shape), the dependency DAG, external connections with SQL, Power Query M, named ranges, extracted process notes, and a **findings list** — circular references, volatile functions, hardcoded constants inside formulas, `IFERROR` swallowing, inconsistent formulas within a region (the classic "someone typed over row 47" bug), external workbook links, hidden sheets/columns.

Plus, from the dual load (§1.5): **column profiles and cached calculated values** for every region. Profiles go in the analysis JSON; raw values do not — they stay accessible through the workbook handle so tools can read them on demand without a multi-hundred-megabyte artifact. Cached values for `output` and `calculation` regions are retained specifically as the reconciliation baseline for §4.5. Flag any workbook where the cache is absent, because it disables reconciliation entirely.

That findings list is the "extra control" pitch made concrete. Lead with it.

M1 also ships **`kedge.xl`**, the Excel-semantics polars namespace from §2.6. It's small, it's pure, it's unit-testable against a table of known Excel outputs, and everything downstream depends on it being right. Build it here rather than discovering the need for it halfway through M4.5 when reconciliation is failing by a penny and nobody knows why.

**Testing strategy: a corpus, not a reference workbook.** Since no single file is representative, M1's fixtures should be 5–8 genuinely *different* real workbooks chosen to span the axes that matter — one heavily Power Query driven, one with legacy ODBC SQL, one with dense cross-sheet formula chains, one that's mostly manual typing with a thin calculation layer, one with process notes in a tab, one with a separate Word procedure, one deliberately messy (merged cells, preamble rows, a totals row mid-sheet).

The point of breadth is not coverage for its own sake — it's that **the analyser must degrade gracefully on the unfamiliar rather than assume a shape**. Every extractor should return "absent" or "unparseable" as a first-class result and keep going. A workbook with no `connections.xml` is normal; a workbook whose DataMashup won't decode should produce a finding, not an exception. Assert this explicitly in tests: run the analyser over a deliberately hostile file and require a complete `WorkbookAnalysis` with populated findings.

### M2 — Notebook driver and the planning step

`discovery.py` + `driver.py` + `supervisor.py`. Every `_code_mode` interaction is generated in `driver.py` and nowhere else — see §6.1.

Then the planning loop (§2.2), which is the part that makes this work across workbooks that don't resemble each other:

1. **Triage** — `convertible` score plus blockers. Stop here and say so if the answer is "don't".
2. **Propose** — analysis in, `ProcessPlan` out. One LLM call over the structural facts, no code generation yet.
3. **Review** — render the plan for the user to edit, reorder, split, merge, answer open questions, and confirm or reject drops. **Nothing is written to the notebook before this is approved.**
4. **Scaffold** — the approved plan becomes cells. Stage order is cell order; each stage gets a named cell; checkpoint stages get `mo.ui` approval cells; the reconciliation cell goes last.

`scaffold.py` is now *plan*-driven rather than analysis-driven, so notebook shape follows the process rather than the spreadsheet's layout. The fixed parts stay fixed: the hand-in selector at the head (§2.8), the contract cell, the reconciliation cell at the tail. Everything between is whatever the plan says it is.

**Steps 1, 3 and 4 need no LLM.** Only `propose` does. That matters for iteration speed and for cost, and it means a hand-written or previously-saved plan can be scaffolded instantly with no model call at all — which is exactly what you want the second time the same monthly process comes round.

### M3 — Server and UI

FastAPI, static chat UI (plain HTML/JS — no build step; consistent with the rest of the OpenAfterHours tooling), SSE event stream, iframe with token-in-URL.

Typed SSE events, because "user is not sat there wondering what is happening" is a stated requirement and an afterthought spinner won't satisfy it:

```
status        {phase: "analysing"|"thinking"|"editing"|"running"}
token         {text}                       # streamed assistant prose
tool_call     {name, args_summary}         # summary, never raw args
tool_result   {name, ok, summary}
cell_created  {cell_id, name, preview}
cell_running  {cell_id}
cell_result   {cell_id, ok, error?}
validation    {ok, violations[]}
done          {turn_id, tokens_used}
error         {message, recoverable}
```

Mirror the important ones into the notebook itself via `ctx.broadcast_raw_notification` so the user gets feedback in whichever pane they're looking at.

### M4 — Agent loop

OpenAI SDK, chat completions, `base_url` + `api_key` from config, streaming with tool calls.

**Tool surface** (the model never writes to the notebook directly):

| Tool | Purpose |
|---|---|
| `list_cells` | current notebook state: id, name, code, defs, refs |
| `propose_cell` | create — goes through the validation gate |
| `edit_cell` | replace a named cell's full body |
| `run_cell` | execute |
| `delete_cell` | requires explicit user confirmation in the UI |
| `inspect_workbook` | progressive disclosure into the analysis (see below) |
| `sample_data` | bounded row slice from a sheet — row and byte capped |
| `profile_column` | full distribution for one column |
| `read_range` | verbatim values from a range, for parameter and lookup tables |
| `probe` | scratchpad execution against the live kernel — real values, results to model, not notebook |
| `get_plan` | the approved `ProcessPlan` — the model's standing instructions |
| `amend_plan` | propose a plan change mid-session; requires user approval, same as the initial plan |
| `reconcile` | compare a notebook variable against cached Excel values (§4.5) |
| `list_utils` | catalogue of reusable functions |
| `get_knowledge` | fetch a context pack by key |

**Validation gate** (`validate.py`) runs before anything reaches `cm`:

1. `ast.parse` — syntax.
2. Marimo contract — no wildcard imports; no public name already owned by another cell; no cycle introduced. Check against `ctx.graph` *before* submitting; a rejected batch is a wasted round trip and a confused model.
3. Policy — no `os.system`/`subprocess`, no network calls outside an allowlist, no writes outside the working directory, no credentials in literals.
4. Output style — reject `import pandas` (see §2.5); reject bare `.round()` on a value derived from an Excel `ROUND` region, pointing the model at `xl.round` (see §2.6); flag row-wise `for` loops over frames. These are the three drifts that will otherwise recur every session.
5. On failure, return violations to the model as a tool result and let it retry. Cap retries at 3, then surface to the user.

**The single-definition rule will be the dominant failure mode.** marimo requires each public name to have exactly one owning cell — including imports (two cells doing `import polars as pl` is a hard error). Mitigation: inject a compact **name registry** (`name → owning cell`) into every turn's context, and state the rule in the system prompt with the `_private` escape hatch spelled out. Without this, most turns will fail validation.

**Context strategy:**

- Rebuild notebook state fresh from `ctx` every turn. Never trust cell code carried in message history — the user edits the notebook directly, so history goes stale immediately. This is the single most important context rule.
- Ship the analysis **summary plus column profiles** by default; full detail and raw values come through tools. Progressive disclosure, not a dump — profiles are cheap, sheets are not.
- Truncate every value-returning tool result with an explicit `[… N more rows omitted]` marker so the model knows it's seeing a slice and doesn't reason as though it saw everything. A model that silently believes it read the whole sheet will assert things about the tail that aren't true.
- Evict old tool results aggressively. Sampled data is the largest and most disposable thing in the window — a `sample_data` result from six turns ago is almost never load-bearing, and re-fetching costs one call.
- Count tokens with `tiktoken`; enforce a hard budget with a fixed eviction order: oldest tool results → oldest turns (summarised) → never evict the system prompt, name registry, or live notebook state.
- Compaction on `/new` or on budget breach: summarise the conversation to a decisions-and-open-questions digest, keep it, drop the rest.

Sessions in SQLite. Each row a message; each session references a notebook path and a snapshot marker so it can be resumed coherently.

### M4.5 — Reconciliation against the workbook

This is the milestone data access buys, and it is probably the most valuable thing in the project.

Because `data_only=True` yields the numbers Excel last calculated, Kedge holds a complete expected-output baseline for every formula cell in the workbook. So after the agent translates a region into Python, it can **check itself**:

1. Run the generated cell.
2. Read the resulting column/frame from the kernel via `probe`.
3. Compare against the cached Excel values for the corresponding range, with a configurable tolerance (absolute and relative — floating-point and Excel's own rounding will not agree exactly, and `1e-9` will produce noise).
4. Report: rows matched, rows differing, worst absolute and relative delta, and the first N mismatching rows with both values side by side.

Then feed the mismatches back to the model as a tool result and let it fix its own translation. This is a closed loop that terminates on evidence rather than on the model declaring itself finished.

Surface it as a **reconciliation cell in the notebook**, not just an agent-internal check. The user sees a green/amber/red panel per translated region, and it re-runs reactively whenever an upstream cell changes. That artifact — "this Python reproduces the workbook's numbers to within 1e-6, here's the proof, and it re-checks itself on every run" — is the difference between a notebook someone wrote and a controlled process someone can sign off.

Design notes:
- Reconcile **regions**, not whole sheets. A per-region pass/fail localises the failure.
- Mismatch counts and deltas go to the model; full mismatch rows are capped like any other value-returning tool.
- Where the cache is absent (§1.5), degrade explicitly to "not reconciled" — never to "passed".
- Keep the tolerance in config, and record the tolerance used in the audit bundle. A reconciliation that passed at 1e-2 is a different claim from one that passed at 1e-9.
- **Triage order for a failure:** check §2.6 before anything else. In practice the overwhelming majority of small unexplained deltas are rounding mode or null-vs-empty, and both have a mechanical fix. Consider encoding that as an automatic diagnostic — if every mismatch is within half a unit of the last retained decimal place, say "this looks like ROUND semantics" rather than making the user work it out.

### M5 — Contracts, utils, knowledge packs

**Hand-in ingestion** — the `HandIn` record, managed store, and both entry points from §2.8. The selector cell goes into the M2 scaffold so it exists from the first notebook, even before contracts are wired up.

**Hand-in contracts** — the audit story. A YAML contract per hand-in: expected columns and dtypes, nullability, row-count bounds, key uniqueness, checksum. `kedge.contracts.validate(handin, contract)` fails loudly with a structured report, and the contract cell is the first thing that runs after ingestion. This is what turns "a notebook" into "a controlled process".

Bootstrap contracts rather than making people write YAML from nothing: `kedge contract infer <file>` generates a first draft from an actual hand-in, which the user then tightens. Nobody writes a good contract on a blank page, but everybody can correct a generated one.

**Utils** — a real importable package, not a loose folder. Generate a catalogue via `ast` (name, signature, first docstring line) and expose it through `list_utils`. Compact enough to always include; the model reuses instead of reinventing, which is the whole maintainability argument.

**Knowledge packs** — `context/` with `databases/*.yaml` (tables, columns, grain, join keys, gotchas), `conventions.md`, `glossary.md`. Start by including everything; only add retrieval when it stops fitting. If it does, reuse the client-side BM25 approach from mooring rather than adding an embedding dependency.

### M6 — Polish
Model picker (fetch `/v1/models` where supported, manual override where not), new-chat, session history, export notebook + analysis + transcript as an audit bundle, diff view of AI-proposed changes before commit.

---

## 5. Two decisions to make before M4

**~~Does the model see cell values?~~ — Decided: yes.**

Kept here because the consequences shape three other modules. Data visibility is not a flag, it's an architecture:

- The sampling layer (§2.3) is now on the critical path, not an optimisation. Without row and byte caps, the first real workbook blows the context window on turn one.
- Reconciliation (§4.5) becomes possible and should be treated as a headline feature rather than a nicety.
- The redaction switch and the outbound log should be built in M1/M4 even though they're off by default. They are days of work now and a rewrite later, and the first time this points at anything with customer identifiers in it, someone will ask what left the machine.

This is a real divergence from mooring, which guarantees the copilot never sees values. Worth being deliberate that the two projects now make different promises — if they ever share documentation or a landing page, the distinction needs stating plainly rather than being inferred.

**~~Is the bespoke chat UI worth building?~~ — Decided: yes, build it.**

*Author's decision (2026-07-24): build the full plan, all milestones. marimo's built-in assistant is explicitly not to be used — the kedge AI window is how the user interacts with the AI.* M3 and M4 are therefore in scope as specified, not deferred.

That decision is now mechanically enforced rather than only stated. `kedge.marimo_config` writes a `.marimo.toml` into the directory kedge launches marimo from, forcing `ai.enabled = false` and `completion.copilot = false`; marimo's config search reads the launch directory before the user's home, so those win over whatever the user has configured personally. Without it the assistant runs from that personal config, inside the iframe kedge serves, against an endpoint outside the tool surface and outside the §2.3 outbound log — the second channel that would make the log's account of what leaves the machine untrue.

**How partially it is closed matters and is written down in `SECURITY.md`, not here.** Three limits in brief: `ai.enabled` is a front-end gate and marimo's `/api/ai/*` endpoints stay reachable to anything holding the marimo token; a `[tool.marimo]` section in the nearest `pyproject.toml` is merged over the file kedge writes and can re-enable the assistant; and because marimo writes its own settings back to whichever config its search found, that file is a plaintext location for a model API key, beside the workbook. kedge detects and reports all three rather than claiming a control it does not have — `kedge.lifecycle.assistant_status` is what a caller reads.

---

## 6. Risks

### 6.1 `_code_mode` is private and will break

This is the top risk. Mitigations, all of them:

1. **One adapter module.** Every string of `_code_mode` code is generated in `notebook/driver.py`. Nothing else imports or references it. When the API shifts, one file changes.
2. **Pin marimo exactly** (`marimo==0.23.15`), not a range.
3. **Contract tests in CI.** Spin up a real `marimo edit --headless --no-token`, open a session, exercise every driver method, assert the results. These must run on every marimo bump, and the bump does not merge until they pass.
4. **A fallback path.** Kedge can also write the notebook `.py` file directly and run `marimo edit --watch`. Slower and it loses live-kernel state, but it's a public interface that will not vanish. Design `driver.py` so this can be swapped in behind the same interface.
5. **Assert on startup.** Check that expected methods exist with expected signatures; fail with a clear message naming the marimo version rather than a `TypeError` mid-conversation.

Also note the skill's own warning: never import `_code_mode` from notebook cells or library code that a user runs — scratchpad only.

### 6.2 Other risks

| Risk | Mitigation |
|---|---|
| Live session required — no open notebook, no session id | Supervisor spawns marimo and waits for the session; clear UI state while waiting |
| User edits a cell between turns | Always re-read via `ctx.cells` before `edit_cell`; submit full replacement bodies |
| Delete+create with same variable names fails validation | Prefer `edit_cell`; where delete is genuinely needed, check `ctx.graph.descendants` first |
| Package installs don't refresh cached module availability | Use `ctx.packages.add()`; warn that a kernel restart may be needed |
| `.xlsb` / heavily macro-driven workbooks | Detect and refuse clearly in M1; VBA is a separate problem — don't half-solve it |
| Model over-fits the plan to one workbook's idiom | Judge `propose` across the whole corpus, not one file; keep the plan schema free-form in stage count and naming |
| Plan looks plausible but decomposes the process wrongly | User approval gate before any code; `open_questions` and `dropped` forced to be explicit |
| Model silently invents purpose for logic it doesn't understand | `open_questions` is a required field — an empty one on a complex workbook is itself suspicious |
| Model drifts back to pandas | Enforce in the validation gate, don't just ask in the prompt — pandas dominates training data |
| Excel/polars semantic gaps produce silently wrong numbers | `kedge.xl` namespace + §2.6 checklist in the prompt + reconciliation as the backstop. Three layers, because one won't hold |
| polars changes rounding behaviour between versions | Pin polars; the `kedge.xl` unit tests are the tripwire |
| Uploaded hand-in makes the notebook unreproducible | `mo.ui.file` returns bytes with no path — always persist to the managed store and reference the managed path (§2.8) |
| Hand-in larger than the 100MB upload ceiling | Route large files through `file_browser` / watched folder; detect and say so rather than failing opaquely |
| Hand-in schema drifts between deliveries | Profile diff against last accepted, reported before contract validation |
| Attaching to the user's *other* marimo notebooks | Never auto-discover — own the process, target by explicit `--url` and `--session` (§2.9) |
| Orphaned marimo process holds the port after a crash | `--timeout` self-shutdown, workspace marker file, `atexit` + signal handlers. Windows has no `SIGTERM` |
| Windows-first environment | Registry path is `~/.marimo/servers`; PID liveness checks are unreliable, use `/health` |
| Model generalises from a 5-row sample to the whole sheet | Every truncated result carries an explicit omission marker; profiles carry distinct counts and min/max so the model can see the range it hasn't sampled |
| Sampled data floods the context window | Row + byte caps on every value-returning tool; sampled results evicted first |
| No cached values in the workbook → reconciliation silently vacuous | Detect all-`None` formula cells at load; report "not reconciled", never "passed" |
| Sensitive values leave the machine unnoticed | Outbound payload log from day one; column redaction available in config |

---

## 7. Suggested order of work

1. **`kedge.xl` first.** A day or two, no dependencies on anything else, unit-tested against known Excel outputs. Everything downstream is wrong without it, and it's the cheapest thing here.
2. M1 to a genuinely useful `kedge inspect`, tested against 3–4 real workbooks with committed fixtures. Everything downstream is bounded by this.
3. M2 driver + contract tests. Prove the bridge before building anything on it.
4. **The planning loop, and judge it on plans alone.** Run triage + propose across the whole M1 corpus and read the plans. This is the cheapest possible test of whether the idea works: if the plans are sensible on five dissimilar workbooks, the code generation is a solvable problem; if they aren't, no amount of downstream engineering fixes it. Do this *before* wiring plan → cells.
5. **M4.5 reconciliation, standalone and before any agent work.** `kedge reconcile notebook.py process.xlsx` needs no LLM — it's a diff between a notebook's outputs and the workbook's cached values. Useful on its own, and it means that when the agent does arrive, its correctness check already exists and is trusted.
6. M5, then M3/M4.

---

## 8. Open questions for the author

- **Which 5–8 workbooks form the corpus?** *Resolved for now (2026-07-24): generate a synthetic corpus spanning the M1 axes as the committed regression suite. Real workbooks to be pointed at later.*
- Should `kedge.xl` ship inside kedge, or as its own small package? It's independently useful to anyone porting Excel to polars, and it's the sort of thing that would sit well alongside `curfew` in the OpenAfterHours set.
- Is there a shared location the hand-in reliably lands in? If so the watched source is worth building, and it's the path that eventually lets the whole process run unattended.
- Is one reference workbook enough to prove it, or does the tool need to handle two or three quite different processes before it's worth the effort?
