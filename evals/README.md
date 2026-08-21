# Evals

An eval here is a workbook plus a **rubric**: an input, and a statement of what a good
conversion of it looks like. It is not a test.

The distinction matters enough to be worth a paragraph, because the two look alike and the
repository already has the other one. `tests/fixtures/` is a *corpus*: eight workbooks, each
built around one failure mode, asserted cell by cell against a hand-written `manifest.py` that
declares what the analyser should **find**. Those assertions are deterministic, they run on
every commit, and they go red on a bug. An eval declares what kedge should **do** — plan,
scaffold, generate, reconcile — and most of that involves a model, so most of it cannot be a
pass/fail assertion without becoming a test of one model's phrasing on one day.

So they are kept apart. Adding a ninth workbook to `tests/fixtures/` would fail
`test_exactly_one_fixture_offers_a_reconciliation_baseline` on the first run, and that test is
right: the corpus is *supposed* to have exactly one positive reconciliation path.

## What is here

| Path | What it is |
|---|---|
| `harness/` | The runner: drives a runbook headlessly, grades it, renders the report. Case-independent. |
| `harness/live.py` | What a model call cost and, when it failed, why. The one place a failure is attributed. |
| `harness/sweep.py` | Phase 1: propose a plan per model, grade the structural tier, tabulate cost. |
| `harness/cellprompt.py`, `cellgen.py`, `render.py`, `align.py`, `findings.py`, `convert.py` | Phase 2: scaffold a plan, have a model fill every hole, render, drive, grade. |
| `run.py` | The CLI. |
| `adjustment_signoff/` | A four-tab manual process — extract, adjust, re-extract, sign off — and whether kedge turns it back into the runbook it came from. |
| `fee_billing_run/` | The same five beats over a ten-tab monthly billing run at 1.9 times the complexity. A diagnostic: it changes the difficulty of the spreadsheet and nothing else. |
| `proposals/` | The case for an eval, argued before it is built -- and, once it is, the record of what building it measured. |

```bash
uv sync --group evals                                 # duckdb, for executing generated SQL
uv run python evals/run.py adjustment_signoff         # grade the reference conversion
uv run python evals/run.py fee_billing_run            # the other one
uv run python evals/run.py adjustment_signoff \
    --notebook path/to/converted.py --plan path/to/plan.yaml
uv run python evals/run.py adjustment_signoff --json  # for a CI step

uv run python evals/run.py adjustment_signoff --model MODEL --repeats 3   # plan, per model
uv run python evals/run.py adjustment_signoff --convert MODEL             # notebook, per model
uv run python evals/run.py adjustment_signoff --model MODEL --dry-run     # resolve, spend nothing
```

Exit code is 0 when nothing gradeable failed — and non-zero when *nothing was graded at all*,
because a sweep whose every leg skipped has not passed, it has not run.

The first four call no model. The last three do, and they are the only things here that spend
money or touch a network.

## `adjustment_signoff`

```
adjustment_signoff/
  build_workbook.py             generates the input; byte-deterministic
  q2_accrual_adjustment.xlsx    the committed input, 25KB
  expected.yaml                 the rubric
  build_notebook.py             generates the reference conversion
  notebook.py                   the reference conversion — the gold answer
  build_plan.py                 generates the reference plan
  plan.yaml                     the reference plan
  case.py                       the graders, keyed to rubric ids
```

```bash
uv run python evals/adjustment_signoff/build_workbook.py
uv run python evals/adjustment_signoff/build_workbook.py --verify-with-excel  # Windows + Excel
uv run python evals/adjustment_signoff/build_notebook.py
uv run python evals/adjustment_signoff/build_plan.py
uv run pytest tests/unit/test_evals_adjustment_signoff.py tests/unit/test_evals_harness.py
```

The workbook records a process, not a calculation:

1. **`Pre-Adjustment`** — the extract query (rows 4–16, and again in `xl/connections.xml` as a
   real ODBC connection), then the 120 rows it returned.
2. **`Adjustment`** — the parameters, the working, the set-based `UPDATE`, and a column of
   `="UPDATE ... "&F17&"..."` — one statement per row, built by concatenation, exactly as the
   original process built it.
3. **`Post-Adjustment`** — the re-extract, to prove the update took.
4. **`Sign-off`** — the memo somebody signed.

The conversion kedge should produce is a runbook: hand over the extract query, take the grid
back, compute the uplift where it can be checked, gate it behind a checkpoint, generate the
`UPDATE` from the approved frame, take the re-extract back, and verify row by row before
reporting anything. Two of those steps needed capability that did not exist when this eval was
written — `StageKind.HANDOFF` and per-stage hand-ins in the scaffolder,
`kedge.reconcile.verify`, `kedge.sql`, and paste ingestion — which is the point. **This eval
specifies target behaviour.** It was written to lead the implementation, not to measure what
was already there.

### What it discriminates

Nine things, and the workbook is built so that each fails independently.

| # | Discrimination | Where |
|---|---|---|
| 1 | Reads the extract SQL as a step to hand over, not as prose | `Pre-Adjustment` rows 4–16 |
| 2 | Recognises a concatenated SQL column as generated SQL | `Adjustment!G` |
| 3 | **Does not drop that column** | it is reported as `dead_region` |
| 4 | Notices the memo contradicts the statement | "three" entities vs four |
| 5 | Does not trust the memo's impact figures | they are stale |
| 6 | Excel's null-as-zero against SQL's `NULL` | `ACC-00001` |
| 7 | Rounds the way Excel rounds | four uplifts need the 15-digit rule |
| 8 | Escapes a literal correctly | `O'Brien & Partners`, `ACC-00041` |
| 9 | Verifies row by row rather than on a total | one break, and only one |

Three of those are worth expanding, because they are the ones that make this workbook different
from the corpus.

**Number 3 is the sharpest.** The analyser reports `Adjustment!G` as a `dead_region`, and it is
correct: nothing in the workbook reads it. Its consumer is a person with a clipboard, which no
static analysis can see. Dropping it is a plausible, well-reasoned, catastrophic edit — it
deletes the step that changes the data — and a plan that lists it under `dropped` has failed the
eval however good the rest of it is.

**Number 8 is the one place matching Excel is wrong.** The workbook's own generated statement
for `ACC-00041` is not valid SQL: `adjustment_note = 'Q2 uplift for O'Brien & Partners'` has an
unescaped apostrophe, because `&` does not escape anything. Reproducing it faithfully reproduces
a bug. `kedge.sql.literal` is the answer, and the rot guard asserts the workbook's version is
broken so that a conversion which fixes it can be told apart from one that never looked.

**Number 9 is the argument for the whole project, in one row.** `Post-Adjustment` matches the
prediction on all 76 adjusted rows except `ACC-00001`, where Excel predicted `0.00` from a blank
cell and the warehouse kept the `NULL`. A notebook that compares totals agrees with the memo and
signs off. A notebook that compares rows finds it. The mismatch is not a contrivance planted to
be found — it is the genuine downstream consequence of discrimination 6.

### Scoring

`expected.yaml` has three tiers, and the weights lean hard on the first.

- **deterministic** — no model in the loop. The harness drives the runbook, supplies the
  pre-adjustment grid as hand-in 1, approves the checkpoint, reads the generated SQL straight out
  of the notebook's definitions, supplies the post-adjustment grid as hand-in 2, and checks what
  the verification concluded. Generated SQL is graded by *executing* it — a DuckDB table seeded
  from the prediction, the statements run against it, the result compared — rather than by string
  comparison, which would have to know what correct escaping looks like.
- **structural** — properties of the plan that survive the model saying it differently on a
  second run, in the style of `tests/llm/test_plan_judgement.py`.
- **judgement** — recorded to be read, not scored.

Money is compared at half a penny, never with `==`. polars' vectorised execution of the rounding
chain lands a few parts in 1e11 from the scalar path — far inside the penny that matters and far
outside float equality — so an `==` grader would fail a correct notebook, and the obvious fix
(loosening the expected figure) would be the wrong one.

### Adding a variant

One workbook is one data point and will be overfit. `build_workbook.py` is parameterised enough
to emit variants — a stale memo versus an accurate one, a derivable rule versus a hardcoded id
list, cached values versus none — and a variant is worth more than another discrimination
inside this one. Keep the rot guard in step: `expected.yaml` quotes figures, and
`tests/unit/test_evals_adjustment_signoff.py` checks every one of them against what the
generator actually produces. That test has already caught the rubric going stale once.

## `fee_billing_run`

```
fee_billing_run/
  build_workbook.py             generates the input; byte-deterministic
  build_pivot.py                finishes the Summary tab by driving Excel over COM
  m11_management_fee_run.xlsx   the committed input, 70KB
  expected.yaml                 the rubric -- 24 items, 70 points
  build_notebook.py             generates the reference conversion
  notebook.py                   the reference conversion
  case.py                       the graders, keyed to rubric ids
```

```bash
uv run python evals/fee_billing_run/build_workbook.py                        # rebuild (refuses, see below)
uv run python evals/fee_billing_run/build_workbook.py --calibrate            # measure; writes nothing
uv run --with pywin32 python evals/fee_billing_run/build_workbook.py --with-pivot     # Windows + Excel
uv run python evals/fee_billing_run/build_workbook.py --verify-with-excel             # Windows + Excel
uv run python evals/fee_billing_run/build_notebook.py
uv run pytest tests/unit/test_evals_fee_billing_run.py
```

**Building and measuring are separate operations.** `--calibrate` and `--verify-with-excel` read the
workbook that is on disk and never rebuild it: the committed file is the artifact the eval grades, it
carries a `Summary` pivot no pure-Python build can author -- openpyxl reads a pivot table and cannot
write one -- and a rebuilt workbook is a different input. Both flags used to rebuild first, which
destroyed discrimination 7 and then reported on the wrong file. For the same reason a plain rebuild
over a workbook that already carries a pivot is *refused*: pass `--with-pivot` to put it back, or
`--force` to accept losing it. `build_pivot.py` carries the warning that matters more: Excel
recalculates on open, which silently repairs the deliberately stale `Allocation` figures
discrimination 9 is made of, so it sets manual calculation on the application *before* the workbook is
opened and re-reads every cached value afterwards. `--with-pivot` also restores `xl/connections.xml`
afterwards, because Excel rewrites that part on save: the query's newlines come back as `_x000a_`
rather than as the `&#10;` the fixtures are authored with, and `commandType="2"` is dropped because
`2` is its schema default. Neither survived the reader until this case found it (see below), so the
restore began as a workaround; now that both forms read back it is byte-stability against `build()`.

The workbook is a monthly management-fee billing run -- work out what each client owes, get it
approved, post it to the ledger, sign it off. Ten tabs, in the order somebody added them over four
years, which is deliberately not dependency order:

1. **`Positions`** -- the warehouse extract. The SQL in rows 3-18 *and* in `xl/connections.xml` as a
   real ODBC connection, so "the extract is a step to hand over" is a claim the analyser can check
   twice; then ~900 rows, one per client per day-end, over two months because the prior one carries
   the opening balances.
2. **`Fee Schedule`** -- pasted from the onboarding team's file. Two preamble rows, tiered bps by
   AUM band, effective-dated. The numbers arrive as text and the client codes carry leading zeros.
3. **`Entity Map`** -- client to legal entity to cost centre. Added in year three, which is why it
   is the eighth tab and the fourth depends on it.
4. **`Working`** -- the wide one. Some 28 heterogeneous columns: average AUM, the tier lookup,
   floors, caps, pro-rating, the composite key, and four abandoned columns from a method changed in
   2024 sitting between the live ones.
5. **`Overrides`** -- three clients whose computed fee the billing manager typed over, each with a
   written reason and a date.
6. **`Allocation`** -- fees at invoice grain, allocated to cost centres, with a subtotal row per
   legal entity embedded inside the grid. Last saved on manual calculation, so its cached values are
   real and stale.
7. **`Summary`** -- a pivot table over `Allocation`. The tab the manager actually reads.
8. **`Post`** -- one `INSERT` per invoice, built by `&`. The manual carry. Nothing reads it, and one
   of the eighty-four names it posts is `O'Hanlon & Reid Nominees`, so the workbook's own statement
   for client `00041` is not valid SQL.
9. **`Recon`** -- last month's figures beside this month's, a variance column, a hand-typed
   commentary.
10. **`Sign-off`** -- purpose, background, known issues, who signs. Feeds `Briefing` through the
    analyser's notes.

Measured: 50 logical operations, 15 distinct `ExcelPattern`s, 10 sheets, 60 dependency edges, 19
dead regions, complexity **0.699**, triage `proceed_with_care`. That is 1.9 times
`q2_accrual_adjustment.xlsx`'s 0.368, and the most complex thing anything in this repository has
been asked to convert. `--calibrate` measures that band and
`tests/unit/test_evals_fee_billing_run.py` pins it in both directions -- because complexity here
comes from *variety* rather than size, and a generator refactor that quietly collapses 28 distinct
column shapes back into one filled-down grid would leave a workbook that passes every assertion and
measures nothing. Verified by injecting exactly that collapse: chained, the workbook falls to 13
operations at 0.503 and both assertions fire.

### Why it is a different instrument from `adjustment_signoff`

`adjustment_signoff` is a **specification**. It was written to lead the implementation, and two of
its steps needed capability that did not exist when it was written.

This one is a **diagnostic**. It needed no new `src/` capability to be scored at all: build the
workbook, build the rubric, run today's `analyse` to `triage` to `propose` to `scaffold` and write
down what happens. It holds the process shape constant on purpose -- the same five beats, extract,
compute, hand a statement over, take the evidence back, sign off -- and changes exactly one
variable, the difficulty of the spreadsheet, so that a result is attributable. A case that also
varied the shape could not distinguish "kedge struggles with complex spreadsheets" from "kedge has
never seen a second input".

Diagnostic does not mean tolerant. Where it shows the pipeline cannot cope with a construct, the
pipeline changes: `evals/proposals/fee_billing_run.md` §7.1 specifies the pivot extractor rather
than recording the limitation, because pivot tables are ubiquitous in finance and the analyser
cannot see one.

### What it discriminates

Ten, of which eight carry points.

| # | Discrimination | Where |
|---|---|---|
| 1 | An approximate-match `VLOOKUP` is a banded join, not a lookup | `Working` tier column |
| 2 | A prior-row reference needs an explicit sort | `Working` opening balance |
| 3 | Pro-rating is date arithmetic with an Excel serial boundary | `Working` part-period columns |
| 4 | A manual override is a decision to re-ask, not a number to bake in | `Overrides` |
| 5 | Eighteen dead regions are dead; the nineteenth posts the fees | `Working` abandoned columns, and `Post` |
| 6 | An embedded subtotal row is not data | `Allocation` |
| 7 | A pivot table is a derived aggregation, never a data source | `Summary` |
| 8 | Text-formatted numbers, and a leading zero that must survive | `Fee Schedule` |
| 9 | Stale cached values on one sheet must not reconcile as passed | `Allocation`, saved on manual calculation |
| 10 | Tab order is not dependency order | `Entity Map` is the eighth tab and feeds the fourth |

**Number 5 is `adjustment_signoff`'s sharpest discrimination re-run at realistic scale.** There, one
dead region, and the eval asks "did you keep it?" -- a one-in-one choice a coin could win. Here,
nineteen, eighteen of which genuinely should go, and the one that must stay is an `info`
`dead_region` like the other eighteen, sits at the head of an undifferentiated nineteen-strong
zero-fan-out tail in the planner's context, and can be dismissed along with the rest by a single
`acknowledge_all_drops`.

**Number 7 is a wrong answer rather than a missing one.** `Summary` is a real pivot over
`Allocation`, and the analyser classifies its rendered grid as `role=data` -- an *input* -- at
confidence 0.853, with no operations on the sheet and no dependency edge to the tab it aggregates.
A plan that reads the summary as a source is believing well-evidenced analysis. `ExcelPattern.PIVOT`
is assigned nowhere in `src/`, so `pivot_is_derived_not_read` is an unconditional skip today and
that skip is the baseline the extractor will be measured against.

**Number 8 grew a second half, and it is about a non-negotiable.** The leading zero is one hazard in
a posted field; the other is client `00041`, `O'Hanlon & Reid Nominees`, whose name `Post!A`
concatenates straight into the statement. `&` quotes nothing and escapes nothing, so the workbook's
own line carries seven single quotes where a valid one has six -- a syntax error at the moment
somebody pastes it into a production client. That is what makes `generated_sql_is_valid`'s failure
text (*"render them through `kedge.sql`"*) name something it can detect: until the name was planted,
naive concatenation produced valid SQL here and non-negotiable 3 was untested by this case. Two rot
guards hold it, as `adjustment_signoff` holds `O'Brien & Partners`: one asserting the workbook's
version is genuinely broken, one asserting `kedge.sql` renders it correctly.

Number 7 carries no points until the extractor exists, and number 10 has no scored item at all --
it appears only in the unscored judgement list -- which is why the ten discriminations are eight for
scoring purposes. Nor do the eight fail perfectly independently: one measured coupling remains, and
`evals/proposals/fee_billing_run.md` §4 records what it is and why it is correct rather than a
defect.

### Scoring

The same three tiers, weighted the same way: **deterministic** 17 items over 49 points,
**structural** 7 items over 21, **judgement** recorded to be read. Money is compared at half a penny
here too.

The reference conversion scores **47/47**, with eight honest skips. Six items want a plan and no
reference plan is committed for this case yet -- five structural, plus
`dead_regions_are_individually_reasoned`, which sits in the deterministic tier and reads
`plan.dropped`; `pivot_is_derived_not_read` waits on the extractor; `consults_the_knowledge_pack`
waits on a pack describing `fin.fee_invoice`. So the structural tier is currently `0/0` -- which the
report renders as exactly that, rather than folding it into a total.

What that run reconciles is worth quoting, because "declared with a reason" must not become a way to
empty a denominator: **34 of the 45 regions are compared, over 2,609 rows**, and the other eleven are
declared. Ten are `not_reproduced` -- the four 2024 columns the plan drops, the stale `Allocation`
tab, the `Post` hand-off kedge renders through `kedge.sql` so there is no cached text to match, and
the four `Recon` ranges that read the stale tab. One is `no_usable_baseline`, which is the different
and more accurate claim: `Working!V` is the override flag, the notebook computes it, and eighty-one
of its cells cache as calculated empty strings that read back as nothing at all, so it is the
*workbook* that cannot check it. `reconciliation_map_resolves` counts both as resolved and enforces a
floor (`case.RECONCILED_SHARE`) on how much was genuinely compared.

The negative controls have a file of their own: `tests/unit/test_evals_fee_billing_run_controls.py`,
46 scenarios grown out of the adversarial review that preceded the reference conversion. It differs
from `test_evals_harness.py` in two ways, and both come from the workbook. Most scenarios are a
**stub context** -- a hand-built frame, statement block or plan handed straight to one grader --
rather than a textual mutation of the reference notebook, because at 1.9 times the complexity a
mutation big enough to change what a grader sees is usually big enough to stop the run, and "the
notebook stopped" is the one outcome that proves nothing. And every grader is controlled in **both
directions**, enforced rather than trusted: a suite that only checks the failing direction is
satisfied by a grader that fails everything, which has stopped measuring the conversion.

### What building it found

Running today's pipeline over the workbook -- offline, no model called, before a grader existed --
found three defects in committed `src/`. A fourth arrived later, when the workbook grew a real ODBC
connection. All four are fixed:

| Defect | Consequence |
|---|---|
| `operation_reference` reconstructed a contiguous rectangle from an anchor and a cell count, ignoring `operation.ranges` | `Allocation`'s fee column is discontiguous *because* the subtotal rows sit inside it, so the baseline read two `SUBTOTAL` figures as client fees, lost two real clients off the end, and reported a **correct** notebook as differing on 56 of its 84 rows. Silently: `truncated=False`, no finding |
| Any bare `&` concatenation classified as `arithmetic`, hint `col("a") * col("b")` | `classify_pattern` keyed off the outermost *function* and a bare `&` has none. The column that builds the INSERTs -- the one region that must never be dropped -- was described to the model as multiplication, with `functions: []` |
| The approximate-`VLOOKUP` description named the sheet holding the formula as the one to sort | *"assumes the table is sorted on Working"*, where the table to sort is on `Fee Schedule`. `_describe` appended `" on {sheet}"` to every headline, and this is the only headline ending in a word that binds it as a prepositional object -- so the most load-bearing sentence in the digest, on the construct the case calls its highest-value translation risk, actively misdirected |
| `analysis.connections` could not read a connection part **Excel itself** wrote | Excel escapes a newline inside `dbPr@command` as `_x000a_` -- `ST_Xstring`, not the `&#10;` this repository authors -- and drops `commandType` because `2` is its default. The reader decoded neither, so one round trip through Excel turned the extract query into a single line littered with `_x000a_`, with `command_type` `None`. `build_proposal_context` hands `connection.command` to the planner as the query to hand over, so that is what the model would have been shown. Neither form is malformed, so nothing complained. `_decode_xstring` now runs in `_attr`, once, for every `ST_Xstring` the module reads |

That last one is why this case's connection part earns its keep twice. It is the first in the
repository to make a round trip through Excel: `tests/fixtures/legacy_sql.xlsx` has one, but it is
hand-authored and has never been re-saved, so its escaping was only ever read back by the convention
that wrote it.

A fifth is pinned rather than fixed. openpyxl's `data_only` view discards `t="str"`, so a
calculated empty string reads as a cell nobody calculated: the 81 rows where the override flag
returns `""` take cached-value coverage to 3,133 of 3,214, and the blocker tells the user to
recalculate and re-save in Excel -- which cannot help, since Excel is what wrote the file.
`test_an_empty_string_result_reads_as_an_uncalculated_cell` asserts the current behaviour exactly,
so it inverts on the day it is fixed rather than quietly passing.

Two more came out of driving Excel, which is the only oracle for cached values that are a parallel
implementation of the sheet rather than a copy of it: an approximate `VLOOKUP` over a
text-formatted column returns *text*, and Excel's `&` renders an integral number without its
trailing `.0`. `--verify-with-excel` is now a mode rather than a one-off -- 5,309 cells compared, 11
moved, all 11 on the two tabs that are stale on purpose -- and it fails both ways, because nothing
moving would mean the deliberate staleness had gone.

The first of those two propagated further than the tab it was planted on, and cost a `src/` change
of its own. A conversion *must* type the tier column -- otherwise the arithmetic below it fails four
operations later inside a query plan -- so the cached `'20.0'` meets a computed `20.0`, and reading
that pair as a type difference makes the region unreconcilable by construction: doing the right
thing is what breaks the check. `reconcile.model.as_numeric_pair` now compares them as the number
both sides mean, and draws the line at **information loss rather than at type**
(`kedge.xl.unambiguous_number`, non-negotiable 3), so `'00417'` against `417.0` stays the difference
it is -- a client code is not the number 417, and blessing that agreement would pass a join key that
has already broken every join it takes part in.

## The harness

### Driving a runbook headlessly

A runbook cannot be run by `App.run()`. Its `mo.ui` elements come up empty and its `mo.stop`
guards halt immediately — by design, since nothing downstream of "waiting for a hand-in" should
compute anything — and marimo offers no way to inject values.

So `harness/drive.py` does what `tests/unit/test_scaffold.py` already does for the notebook head,
generalised to a whole file: read the `@app.cell` blocks with
`kedge.notebook.codegen.read_notebook`, then execute their bodies in order against a namespace
holding a stand-in `mo`. No kernel, no server, no private API.

Three details make it faithful rather than merely convenient:

- **The stand-in is installed as the `marimo` module for the run.** Otherwise the notebook's own
  `import marimo as mo` undoes the whole thing on the first cell. It is scoped and restored.
- **Values bind *after* the cell that defines the widget.** marimo's single-definition rule means
  a widget is defined in one cell and read in a later one; between them is exactly where the
  user's interaction sits. So the script reads `{"pre_adjustment_pick": (path,)}` — the same name
  the notebook uses.
- **Anything outside the stand-in's surface raises.** A harness that quietly absorbed an unknown
  `mo.something` would grade a notebook that never really ran.

It is not marimo: cells run in file order, nothing re-runs reactively, nothing renders. For a
notebook whose cells were emitted in dependency order that is enough, and the alternative is
grading a reimplementation of marimo rather than the notebook.

### The reference conversion, and why it is committed

`notebook.py` is the gold answer — a full, correct conversion of the workbook. It exists for two
reasons that pull the same way. It is the **worked example** of what a runbook looks like; and it
is the only thing that demonstrates the graders work at all. A harness whose checks have never
seen a passing notebook could have every one of them inverted, mis-keyed or reading the wrong
column, and the first real conversion it scored would take the blame.

`plan.yaml` is the same argument for the structural tier, built through the real model and the
real `approve()` so the file on disk is provably a plan kedge would accept.

Neither is the *only* right answer. The graders assert on figures and behaviour, never on
phrasing or stage decomposition, and `expected.yaml` records the alternatives it accepts.

### Negative controls

Full marks on the reference proves the graders can pass. `tests/unit/test_evals_harness.py`
proves they can **fail**, by injecting each defect into a copy of the reference notebook, one at
a time, and asserting the right item goes red and nothing else falls over:

| Mutation | Should fail |
|---|---|
| `.xl.round(2)` → `.round(2)` | `totals_to_the_penny` |
| drop `.xl.empty_as_zero()` | `null_is_not_zero` |
| drop the `CANCELLED` predicate | `cancelled_rows_excluded` |
| build the SQL by concatenation | `generated_sql_is_valid` |
| verify a frame against itself | `verification_finds_exactly_one_break` |
| carry the memo's stale movement | `does_not_trust_the_impact_summary` |
| rename a widget so the run stops | `ran_to_completion` (the rest **skip**, not fail) |
| add `Adjustment!G` to `dropped` | `does_not_drop_the_sql_column` |

The last two rows are the ones worth keeping. A notebook that stops has one problem, and
reporting it eight times buries it — so items about cells that never ran are skips. And a plan
that drops the SQL column is the sharpest failure the eval has: plausible, well-reasoned, and it
deletes the step that changes the data.

### The denominator

Three outcomes per item, not two. `SKIP` exists because an item can be ungradeable through
nobody's fault — duckdb not installed, no plan supplied, the notebook stopped before the cell
this item is about ever ran. Folding those into `FAIL` makes a missing dependency look like a
broken conversion; folding them into `PASS` is the reconciliation sin.

So a report always carries **two numbers**: the score and what it was taken over. `20/20` over a
rubric of sixteen items is not a pass, and the headline will not render it as one.

## Measuring a model

Everything above grades committed artifacts -- `adjustment_signoff` at 63/63, `fee_billing_run` at
47/47 -- with no model anywhere in the loop. That proves the graders work. It says nothing about
whether the model a user has configured can do the job — which matters, because
`q2_accrual_adjustment.xlsx` is one of the *simplest* processes a user will bring. Two modes answer
that, at two different seams and two very different prices. Both are case-independent, and
`fee_billing_run` is registered for them.

**`--model` grades the planning seam.** One call per repeat: propose a plan, grade it against the
structural tier, record tokens, seconds and — when it fails — why. Repeat with `--repeats`; one
sample is noise, and the table prints the spread rather than a mean that hides it.

**`--convert` grades the code-generation seam.** Scaffold an approved plan, ask the model for
every `TODO(kedge)` body, gate each through `kedge.agent.validate`, render, drive, grade. Many
calls, so it costs materially more. It sends the product's own prompt parts — computed by
subtraction from `SYSTEM_PARTS`, in the product's own message roles, carrying `tools.md`'s
validation rules quoted rather than paraphrased — because an eval whose prompt differs from what
ships measures something no user experiences.

They are deliberately separate, and `run.py` refuses to run both at once. Handing the committed
reference notebook to a model-written plan would print a near-perfect total made mostly of points
a human earned, under a model's name: the exact false confidence this whole apparatus exists to
remove, and almost invisible in a tidy table.

### Attribution is the point

A model that scores nothing may have refused the JSON schema, refused an explicit temperature,
timed out, been rate-limited, been unreachable, had no key in the keyring, been declined by
kedge's own triage, returned a 404 because that id is not enabled for the account, or produced
prose no repair round could turn into a plan. **Only the last of those, and a plan that validates
but is poor, are the model's judgement failing.** Everything else is a fact about the integration,
the account or the sweep's pacing, and `Failure.about_the_model` is where that distinction is
encoded rather than assumed — so those legs skip, loudly, under a heading that says they were
asked and did not answer.

A timeout skips for a reason worth repeating: `[model] timeout_seconds` is httpx's gap *between
reads*, not a budget for the whole answer, so an endpoint that goes quiet while a reasoning model
thinks trips it while working perfectly. Scoring that against the model replaces a good model over
a setting. This project has already had one live run where misattribution produced 24 failed
requests and the wrong diagnosis (`tests/llm/README.md`).

Cost is reported in tokens and seconds. Currency is opt-in via `--prices`, because a price table
committed here would be wrong within a month and wrong invisibly. An endpoint that volunteers no
`usage` block reports "not reported", never `0` — a model nobody measured must not come top of a
cost column.

### What generating a conversion found

Grading a *generated* notebook rather than the committed one surfaced five defects, all of which
the reference conversion had been hand-written around and none of which running the offline eval
could ever have shown:

| Defect | Consequence |
|---|---|
| Reconciliation mapped a hand-off's operation to the hand-off's cell | The panel became a dataflow descendant of the checkpoint, so the evidence for a decision would not render until after it |
| The same mapping bound a *statement* to a region of cached values | The region reported `failed` rather than unchecked -- permanently amber, unclearable |
| The region map carried no `TODO(kedge)` | Nothing that fills the scaffolder's holes was ever asked to finish it |
| The fixed head hand-in was emitted even when every stage declared its own | Dead cells, a contract gate with no consumers, and `check_translation` cited the wrong digest |
| The head hand-in never called `previous_handin` | Reopening a runbook stopped in the third cell |
| `mutates: false` on an `UPDATE ... SET` | No confirmation, so no token, so the re-extract box rendered from the moment the notebook opened |

All are fixed. `EXPECTED_DEFECTS` in `tests/unit/test_evals_convert.py` is now an exact **empty**
set, and each defect has a test pinning the *mechanism* rather than the absence of a string, so one
returning by another route is still caught. The reference bodies replayed through the pipeline score
**60/63**, up from 47/63 -- and 13 of those points came from fixing kedge, not from touching a
grader.

One item stays red on purpose: `progressive_disclosure` wants `extract_query` on screen at once,
and a scaffolded notebook blocks it with *"Step 1 of 8 ... fill in the inputs above"* because it has
no period end yet. The reference conversion passes by defaulting the date picker, and defaulting it
is the part worth arguing about -- a runbook that opens with a query already scoped to a date nobody
chose is how somebody extracts the wrong period. Recorded as a difference until someone decides
which a runbook should do.

That is the argument for this mode in one paragraph. The reference conversion is a worked example of
the destination; it was never evidence that kedge can get there.

## What the evals do not cover

- **Nothing about the chat loop.** This is workbook in, notebook out. Whether the agent
  *converses* well about the conversion is a different eval.
- **Widget names are aliased, but only where the alias is provable.** A candidate must be assigned
  from `mo.ui.*` and match the stage cell's own name exactly; anything looser bound strings and
  reported the run as driven while the scripted action went nowhere. What still cannot be played
  is reported as `NOT DRIVEN`, which is its own category and never a skip.
- **No knowledge-pack item, in either case.** `consults_the_knowledge_pack` skips both times:
  `context/databases/example.yaml` describes a different schema. Packs for `fin.accruals` and
  `fin.fee_invoice` would make them gradeable.
- **Two workbooks, one process shape.** A monthly adjustment with a sign-off, and a monthly billing
  run with a sign-off -- the same five beats on purpose, so that `fee_billing_run` varies the
  difficulty of the spreadsheet and nothing else. What is still untested is a second *shape*: two
  sources joined, an outbound payment file, a Power Query. That is a third case and it would not be
  attributable if it were folded into either of these.
- **`kedge.sql` is now exercised by both workbooks, and by one shape of literal.** `adjustment_signoff`
  has `O'Brien & Partners`, `fee_billing_run` has `O'Hanlon & Reid Nominees`, and both post a
  workbook statement that is genuinely broken, so non-negotiable 3 is tested twice rather than
  once -- `fee_billing_run` used to post nothing that needed escaping and its
  `generated_sql_is_valid` could not tell `kedge.sql` from concatenation. What neither covers is the
  rest of the class the module exists for: a `NULL`, a date, a money value at the edge of exponent
  notation. An apostrophe in a counterparty name is the ordinary case, not the whole of it.
- **A pivot table, still.** `pivot_is_derived_not_read` is an unconditional skip until
  `analysis/pivots.py` exists. The eval records the misclassification; it cannot yet grade the fix.
