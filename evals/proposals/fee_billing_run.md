# Proposal: `fee_billing_run` -- the same process, a much harder spreadsheet

Status: proposal. **Phase 1 step 1 is built** -- `evals/fee_billing_run/build_workbook.py` and
`tests/unit/test_evals_fee_billing_run.py`, a calibration spike and its rot guard, measured in
section 3. Everything else here is still a proposal.

Every figure in section 1 and section 2 was measured against the tree at `305fe62`, by running the
real analyser and the real `build_proposal_context` over the committed fixtures and over three probe
workbooks built for this proposal -- one of them a genuine pivot table constructed by driving Excel
over COM, per CLAUDE.md's rule that ground truth about Excel is generated rather than reasoned about.
Where a claim is a prediction rather than a measurement it says so.

---

## 0. What this eval is for, and why it is not `rebate_payfile`

`adjustment_signoff` and the `rebate_payfile` proposal beside this one strike the same bargain, and
`adjustment_signoff`'s README states it outright: **"This eval specifies target behaviour. It was
written to lead the implementation, not to measure what was already there."** Both are specifications
wearing an eval's clothes. Both require substantial new `src/` before they can be scored at all --
`rebate_payfile` estimates 1,200-1,800 lines.

This one is the opposite instrument, and the difference is the whole point of proposing it.

**It is a diagnostic.** It changes exactly one variable -- the difficulty of the spreadsheet -- and
asks whether the pipeline that demonstrably works on a simple workbook still produces the desired
output on a realistic one. Phase 1 requires **no new `src/` capability whatsoever**: build the
workbook, build the rubric, run today's `analyse` -> `triage` -> `propose` -> `scaffold` ->
`--convert` and write down what happens. Everything it finds is a defect that exists right now, in
committed code, that nothing in the repository is currently able to see.

**It deliberately keeps the process shape constant.** Extract, compute, hand a statement over, take
the evidence back, sign off -- the same five beats as `adjustment_signoff`, for the same reason a
controlled experiment changes one thing at a time. `rebate_payfile` varies the shape (two sources,
one join, an outbound file) and therefore cannot distinguish "kedge struggles with complex
spreadsheets" from "kedge has never seen a second input". Holding the shape fixed makes the result
attributable. The two proposals are complementary and neither substitutes for the other; if only one
is built, this is the cheaper one and it is the one that tells you where you actually are.

**Diagnostic does not mean tolerant.** Measuring first is a sequencing choice, not a scope one: the
standing rule is that really hard spreadsheets are the target, so where this eval shows the pipeline
cannot cope with a construct, the pipeline changes. Section 2.4 is already an example -- pivot tables
are ubiquitous in finance and the analyser cannot see one, so section 7.1 specifies the extractor
rather than recording the limitation. Nothing this eval finds should end up in "out of scope"; the
only question is the order things are built in.

---

## 1. How simple the current corpus actually is

Every workbook in the repository, analysed:

| Workbook | Operations | Sheets | Findings | Edges | Complexity | Verdict |
|---|---:|---:|---:|---:|---:|---|
| `powerquery.xlsx` | 2 | 3 | 6 | 1 | 0.234 | proceed_with_care |
| `legacy_sql.xlsx` | 2 | 2 | 8 | 0 | 0.273 | proceed_with_care |
| `documented.xlsx` | 4 | 4 | 6 | 2 | 0.336 | proceed_with_care |
| **`q2_accrual_adjustment.xlsx`** (the eval) | **7** | **4** | **6** | **8** | **0.368** | **proceed** |
| `mostly_manual.xlsx` | 8 | 4 | 11 | 10 | 0.408 | proceed_with_care |
| `no_cached_values.xlsx` | 9 | 3 | 12 | 8 | 0.432 | proceed_with_care |
| `clean_pipeline.xlsx` | 13 | 5 | 13 | 13 | 0.495 | proceed |
| `cross_sheet_chain.xlsx` | 16 | 5 | 16 | 19 | 0.525 | proceed_with_care |
| `hostile.xlsx` | 21 | 5 | 44 | 9 | 0.527 | stop |

`kedge.plan.triage.complexity`'s own docstring reads: *"Roughly: under 0.35 is a simple workbook,
over 0.7 is a big one."* The workbook that defines kedge's target behaviour scores **0.368**. The
most complex artifact anywhere in the tree scores 0.527, is called `hostile`, and is a
robustness fixture that triages to `stop` -- so it has never been converted and never will be.

**Nothing in this repository has ever been converted at a complexity above 0.368.** Every claim
kedge makes about handling real spreadsheets rests on one workbook that its own scorer calls
one notch above simple.

### 1.1 Size is not complexity, and this determines how the workbook must be built

The obvious way to write a harder generator -- more sheets, more columns, more rows -- produces a
workbook that is not harder at all. R1C1 normalisation is doing its job: a formula filled down a
column *and* across thirty columns still normalises to one string.

Measured, on a workbook built for this proposal:

> 5 sheets, 3 of them 30 columns wide by 58 rows of chained arithmetic, 5,220 formula cells.
> **7 logical operations. Complexity 0.369.** Indistinguishable from `q2_accrual_adjustment.xlsx`.

Replacing those uniform grids with **heterogeneous** columns -- 25 different formula shapes, then 20
more reading the first sheet -- on a workbook of comparable size:

> **49 logical operations, 14 distinct `ExcelPattern`s, 73 dependency edges, 55 column profiles,
> 28 findings. Complexity 0.665.**

That is the target. A `fee_billing_run` generator must emit *variety*, not volume: every column a
different calculation, which is what a spreadsheet grown over four years by three people actually
looks like. A generator that emits a big grid will produce a simple workbook and the eval will
measure nothing.

---

## 2. Six things that are true today, and only a bigger workbook can show

Each is verifiable in the tree as it stands. Three are measurements from the probe above; two are
present-tense defects reachable on a **committed fixture** right now. The sixth was found by
*building* the workbook rather than by reading the code, which is the case for this eval in
miniature -- it surfaced before a single grader existed.

### 2.1 The one operation that must never be dropped sorts dead last in the planner's context

`_operation_digest` ranks by fan-out, and the comment says why: *"a region twenty other regions
depend on is structurally more important than one nothing reads, and if anything has to be dropped
it should be the latter."* Sound reasoning, and on a simple workbook harmless.

But a **manual carry** -- a column of `="UPDATE ... "&F17&"..."` whose consumer is a person with a
clipboard -- has fan-out zero *by definition*. Nothing in the workbook reads it. That is precisely
what makes it a `dead_region`, and `adjustment_signoff`'s README calls dropping it **the sharpest
failure the eval has**: *"plausible, well-reasoned, and it deletes the step that changes the data."*

On the 49-operation probe, with one such column present:

> `manual carry ranks 49 of 49`
> `all 18 dead regions sit in the final 18 positions`

`_MAX_OPERATIONS` is 80. At 7 operations the ranking is invisible. At 49 it is a latent hazard. Past
80 the manual carry is simply **not in the context the model reasons over**, and the model is told
only `operations_omitted: N` -- a count, with no way to know that the omitted tail is where every
manually-carried step in the workbook lives, because zero fan-out is exactly what a manual carry and
an abandoned column have in common.

The fix is probably not "raise the cap". It is that fan-out is the wrong sole key: a region carrying
generated SQL, or one the analyser flagged `dead_region`, is *more* interesting for having no
consumer, not less. But the eval has to exist before that argument can be settled with evidence.

### 2.2 Eighteen decisions, one button

`plan.dropped` requires per-drop acknowledgement, and `DroppedRange` insists a reason is given --
*"'Unused' alone is not a reason."* At one dead region that is a genuine decision, and
`adjustment_signoff` grades it as one.

The probe produced **18 dead regions out of 49 operations**. `kedge.plan.review.acknowledge_all_drops`
clears all of them in one call with one shared note, and its docstring is careful and correct about
what it does: *"It still records an explicit acknowledgement per drop, so the audit trail is
identical to acknowledging them one at a time."* The audit trail is identical. The **decision** is
not: eighteen ranges dismissed with one sentence that cannot be true of all of them, and the manual
carry is one of the eighteen.

This is the same shape as CLAUDE.md's own note that *"a signal that is permanently amber is one
people stop reading"*, arriving from the other direction: a signal that can be cleared in bulk is one
people clear in bulk.

### 2.3 The reconciliation map is the hole that scales worst

`_values_cell` emits `reconciliation_values` with a `TODO(kedge)` marker and one entry per operation,
defaulting each to the stage's whole frame. Its docstring is honest that the default is *"a starting
point rather than an answer"*: it resolves only where the workbook's own header for the region names
a column of that frame, because `infer_regions` sets `column=_normalise_name(header)` and
`compare.to_vector` needs that exact name.

Today: 6 operations across 8 stages, and the reference conversion fills 5 entries by hand.

Predicted for `fee_billing_run`: **40-plus entries**, of which the default resolves for almost none,
because a conversion that is doing its job renames `"Fee @ tier bps"` to `fee_gbp` and because a
totals row has no column to name at all. Every unresolved entry renders as *"the notebook produced no
values for this region, check that the cell ran and that the variable names match"* -- the exact
message the module's own docstring says *"sends a reader hunting a bug that is not there, on every
run, for ever."*

So the largest single translation burden in a complex conversion is a hole nothing has ever measured
at scale, whose failure mode is a wall of text telling the reader to go and look for bugs that do not
exist. That is worth knowing before a user finds it.

### 2.4 A pivot table reads as a data sheet, and everything needed to translate it is already on disk

This is the sharpest of the five, and it is not a gap. It is a **silent wrong answer**.

Grep the whole tree first. `ExcelPattern.PIVOT` appears in exactly three places -- two
translation-hint tables in `analysis/regions.py` and one in `notebook/scaffold.py` -- and in **no**
assignment. `classify_pattern` cannot return it; nothing else sets `excel_pattern`. Nothing anywhere
in `src/` reads a `pivotCache` or a `pivotTable` part. Because
`tests/unit/test_agent_prompts.py` asserts every `ExcelPattern` member appears in the prompt that
offers it, the model is told about a vocabulary word the analyser can never hand it -- the mirror
image of the `StageKind.HANDOFF` bug already in CLAUDE.md, where the enum grew and the prose did not.

What that costs is worse than silence. A pivot table writes its **rendered result** into the sheet as
ordinary cached cells, so the tab does not come back empty -- it comes back looking like data. Driving
Excel to build a real pivot (row fields `entity` and `cost_centre`, column field `month`, page field
`region`, `Sum of fee_gbp`) over a 198-row source and re-reading it:

```
sheets: ['Allocation', 'Summary']
  Allocation: role=data formula_cells=0 max_row=199
  Summary:    role=data formula_cells=0 max_row=11     <- the pivot
operations: 0        -> on Summary: 0
findings on Summary: ['preamble_rows']
```

`Summary` is classified **`role=data`**. Not `output`, not `calculation`, not unknown -- an *input*.
A conversion reading that plan has been told the aggregated summary is a source table, when it is a
derived view of the tab beside it. Reading a derived aggregate as a source is how a total gets
double-counted, and nothing in the analysis says otherwise.

And the translation is not a research problem. openpyxl already parses the whole thing; the same
workbook, read back:

```
pivots on Summary: 1
  name='FeeSummary' location='A3:E11' cacheId=6
  source: sheet='Allocation' ref='A1:F199'
  cache fields: ['client','entity','cost_centre','month','region','fee_gbp']
  rowFields:  ['entity', 'cost_centre']
  colFields:  ['month']
  pageFields: ['region']
  dataField:  field='fee_gbp' name='Sum of fee_gbp' aggregation='sum'
```

That is a complete, machine-readable specification of

```python
alloc.filter(pl.col("region") == region).group_by(["entity", "cost_centre", "month"])
     .agg(pl.col("fee_gbp").sum()).collect().pivot(on="month", ...)
```

with no guessing anywhere in it -- source range, grouping keys, the pivoted axis, the filter field
and the aggregation function all read straight off the part. It is exactly the kind of deterministic
extraction `analysis/` exists to do, it needs no new dependency, and the enum member, the prompt entry
and the translation hint it would populate **all already exist**. Only the extractor is missing.

Pivot tables are heavily used in finance processes, so this is not an edge case to be declared out of
scope; it is a capability the analyser has to grow. Section 7.1 specifies it.

### 2.5 Truncation is already silent, and it is already happening

`build_proposal_context`'s docstring promises: *"Everything that is truncated says so explicitly, so
the model never reasons as though it saw the whole workbook."*

It sets `operations_omitted` and `column_profiles_omitted`. It sets nothing for **findings** (capped
at 40), **process notes** (20), **named ranges** (40), **dependency edges** (200), **cycles** (10) or
**roots/leaves** (40 each). Those six truncate in silence.

This is not hypothetical and does not need a new workbook to demonstrate. On `tests/fixtures/hostile.xlsx`,
today:

```
analysis findings: 44 -> digest findings: 40
notes: 21, cap 20
keys announcing truncation: []
```

Four findings and one process note are dropped from the planner's view with no announcement. The
findings are ranked errors-first, so what goes is the `info` tail -- and three of the four dropped
are `document_attached_by_filename`, the one member of `FindingKind` whose entire purpose the model
docstring spells out: *"a sidecar document was attached because its filename looked like a process
document... the user is the only one who can tell whether the guess was right, and a note silently
attributed to the wrong workbook is worse than no note."*

So the finding that exists to say *"I guessed at this attribution, please check me"* is `info`, sorts
last, and is silently discarded -- while the notes it is warning about are in the context, unlabelled,
feeding a `Briefing` that is required to cite its sources. The citation requirement holds; what it
cites may be attributed to the wrong workbook, and the warning that said so did not survive the trip.

To be precise about the blast radius: this affects the **planner's digest** only. The analysis JSON
`kedge inspect` writes holds all 44, and `report.py` renders all 44 uncapped, so a human who looks
sees everything. The defect is that the *model* reasons over a truncated view while the context it
was handed says it is complete -- and a complex workbook turns each of those six caps from a
theoretical limit into a live one.

### 2.6 An empty-string formula result is indistinguishable from a cell nobody calculated

Found while wiring the cached values into the generator, not by reading the code.

`Working` has an override flag: `=IF(agreed=net,"","OVERRIDE")`. On the eighty-one rows with no
override it returns an empty string. Excel stores that as -- verified by driving Excel over COM
rather than assumed:

```xml
<c r="B1" t="str"><f>IF(A1=A2,"","DIFF")</f><v/></c>
<c r="B2" t="str"><f>IF(A1=A2,"SAME","DIFF")</f><v>SAME</v></c>
```

openpyxl's `data_only` view hands back `None` for the first of those -- confirmed against the file
Excel itself wrote, not against a generated one -- and `values.add_formula` counts `cached is None`
as *no cached value*. So a workbook that Excel has fully calculated reports:

```
formula cells 3214, cached present 3133, coverage 0.975, status partial
blocker: cached values cover only 97% of formula cells; the regions without them cannot be
         reconciled (recalculate and re-save in Excel to complete the baseline)
```

Two things are wrong here and the second is worse than the first. The coverage figure is untrue:
every one of those cells *was* calculated. And **the remediation is unfollowable** -- it tells the
user to recalculate and re-save in Excel, which cannot help, because Excel is what wrote the file.
A user who follows the instruction exactly gets the identical blocker back, which is the
"permanently amber" failure CLAUDE.md already names, with an added instruction that wastes their
time before it fails.

The fix is small and the signal is already in the file: `t="str"` with an empty `<v/>` is a
calculated empty string, where an uncalculated numeric cell carries no `t` at all. What is missing
is that openpyxl's `data_only` view discards the attribute before kedge sees it, so the fix has to
read it from the sheet XML the handle already holds.

`tests/unit/test_evals_fee_billing_run.py::test_an_empty_string_result_reads_as_an_uncalculated_cell`
pins the current behaviour exactly -- 81 missing, matching the 81 empty-string rows -- so the
assertion inverts the day it is fixed rather than quietly passing.

The eval keeps the column rather than working around it. `IF(...,"",...)` is what people write, the
blocker is a *verification* blocker so `convertible` stays at 1.00 and all 50 operations remain
reconcilable, and a workaround would hide the one defect this exercise has already paid for.

---

## 3. The process the workbook records

**`m11_management_fee_run.xlsx`.** The monthly management-fee billing run: work out what each client
owes, get it approved, post it to the ledger, sign it off. Ten tabs, laid out in the order somebody
built them over four years -- which is deliberately **not** dependency order.

| Tab | What it is |
|---|---|
| `Positions` | The warehouse extract. SQL in rows 3-18, the same query in `xl/connections.xml` as a real ODBC connection, then ~900 rows: one per client per day-end, two months (the prior month is needed for opening balances). |
| `Fee Schedule` | Pasted from the onboarding team's file. Two preamble rows, header on row 4, tiered bps by AUM band, effective-dated. Numbers arrive as text because Excel copied what the cells *looked like*. Client codes carry leading zeros. |
| `Entity Map` | Client to legal entity to cost centre. Added in year three, which is why it is the eighth tab and everything above it depends on it. |
| `Working` | The wide one. ~28 heterogeneous columns: average AUM, tier lookup, floors and caps, pro-rating, the composite key. Four abandoned columns from a method changed in 2024 sit between the live ones. |
| `Overrides` | Three clients whose computed fee was typed over by the billing manager, each with a reason and a date. |
| `Allocation` | Fees aggregated to invoice grain and allocated to cost centres, with an embedded subtotal row per legal entity inside the grid. |
| `Summary` | A pivot table over `Allocation`. The tab the manager actually reads. |
| `Post` | One `INSERT ... ` per invoice, built by `&`. The manual carry. Nothing reads it. |
| `Recon` | Last month's figures beside this month's, with a variance column and a hand-typed commentary. |
| `Sign-off` | Purpose, background, known issues, who signs. Feeds `Briefing` through the analyser's notes. |

Target shape was **45-60 logical operations, 12-15 distinct patterns, complexity 0.68-0.75**, and
that band is no longer an estimate. A calibration spike -- `evals/fee_billing_run/build_workbook.py`,
the first cut of this generator -- was built to answer whether the tab design above actually lands in
it, because a design that scores 0.55 stresses nothing new and one that triages `stop` cannot be
graded at all. Measured:

```
operations              50     target (45, 60)
distinct patterns       15     target >= 12
complexity           0.698     target (0.68, 0.75)
verdict     proceed_with_care  must not be 'stop'
convertible           1.00
reconcilable ops        50     of 50
inferred regions        45
sheets                  10
cross-sheet ops         21
findings                40
dead regions            19
dependency edges        60
column profiles         77
manual carry        ranks 32 of 50  (fan-out 0)
digest truncation     none
```

**In band, and 1.9 times the complexity of anything kedge has ever converted.** Three things the
spike settled that the design could not:

- **Two columns had to be rewritten before the workbook was hard at all.** A reference whose target
  moves per row -- `=Positions!$A{n}` filled down, and `=Working!$S{n}` on `Allocation` -- normalises
  to a *different* R1C1 string on every row, so one column became eighty-four operations and the
  first measurement came back at **208 operations, complexity 0.855**. Both are now written the way
  the process would really do it (a pasted code column, and a `SUMIFS` by key). Section 1.1 warned
  that uniformity hides complexity; the opposite error is just as easy and it inflates rather than
  collapses.
- **19 dead regions, above the 15-plus predicted** -- so P2's haystack is real and discrimination 5
  is sharper than the first eval's one-in-one choice by an order of magnitude.
- **The workbook sits deliberately under `_MAX_OPERATIONS`.** At 49 the planner sees every
  operation, so the eval measures conversion quality rather than truncation. Truncation is a
  different question and the cheap way to ask it is a *variant* of this workbook past 80, not a
  second eval.

Note what is *not* in that list: no macros, no external links, no circular references, no `.xlsb`. Those
are `hostile.xlsx`'s job and they triage to `stop`. This workbook must triage to `proceed` or
`proceed_with_care`, because an eval about conversion quality that refuses to convert measures nothing.

---

## 4. What it discriminates

Ten, each planted so it fails independently, and each carrying process meaning rather than being a
parsing quirk. The README's own warning applies and is respected: *"a case that fails on ten parsing
quirks measures the reader, not the conversion."*

| # | Discrimination | Where |
|---|---|---|
| 1 | An approximate-match `VLOOKUP` is a banded join, not a lookup | `Working` tier column |
| 2 | A prior-row reference needs an explicit sort | `Working` opening balance |
| 3 | Pro-rating is date arithmetic with an Excel serial boundary | `Working` part-period columns |
| 4 | A manual override is a decision to re-ask, not a number to bake in | `Overrides` |
| 5 | Seventeen dead regions are dead; the eighteenth posts the fees | `Working` abandoned columns, and `Post` |
| 6 | An embedded subtotal row is not data | `Allocation` |
| 7 | A pivot table is a derived aggregation, never a data source | `Summary` |
| 8 | Text-formatted numbers, and a leading zero that must survive | `Fee Schedule` |
| 9 | Stale cached values on one sheet must not reconcile as passed | `Allocation`, saved on manual calculation |
| 10 | Tab order is not dependency order | `Entity Map` is the eighth tab and feeds the fourth |

Six are worth expanding.

**Number 1 is the highest-value translation risk in the whole proposal.** `=VLOOKUP(AUM, tiers, 3, TRUE)`
over a banded rate table is the single most common construct in finance spreadsheets and the one
whose polars translation is least forgiving. The analyser already classifies it (`vlookup_approx`)
and already hints the answer (`.join_asof(lookup, on=..., strategy="backward")`). So this tests
something sharper than recognition: **is the hint enough?** `join_asof` requires both frames sorted
on the join key and silently produces wrong answers if they are not; the band boundary is `>=` on the
floor and an off-by-one moves an entire tier of clients onto the wrong rate. Note also that
non-negotiable 3 says Excel semantics belong in `kedge.xl` -- and approximate-match lookup *is*
Excel semantics that `kedge.xl` does not have. Whether that is a gap or correctly out of scope is a
question this eval is built to force.

**Number 4 is the sharpest judgement discrimination, and it is new.** Three clients' fees were typed
over by a person, each with a written reason. There are three wrong answers and they fail in
different directions. Reproducing the typed numbers bakes last quarter's judgement into every future
month. Dropping them silently loses three real decisions. Reconciling against them reports a pass for
a figure nobody calculated. The desired output is a **checkpoint** that surfaces all three with their
reasons and asks whether they still apply *this* month -- which is the runbook argument in miniature,
and which no existing eval asks for. `adjustment_signoff` has one checkpoint gating one approval;
this asks whether the planner can recognise that a block of typed-over cells *is* a checkpoint.

**Number 5 is `adjustment_signoff`'s sharpest discrimination, re-run at realistic scale.** There, one
dead region, and the eval asks "did you keep it?" -- a one-in-one choice a coin could win. Here,
eighteen, seventeen of which genuinely should go, and the one that must stay sorts last in the
planner's context (section 2.1) and can be dismissed along with the rest by a single
`acknowledge_all_drops` (section 2.2). Same question, and the first time it has been asked in a way
that can distinguish understanding from luck.

**Number 7 is a wrong answer rather than a missing one, which is why it is graded hard.** The
`Summary` tab is a pivot over `Allocation`, and today it classifies as `role=data` (section 2.4) --
so a plan can legitimately conclude the aggregated figures are an input. The desired output is that
the summary is *derived*: one stage grouping `Allocation` by entity and cost centre, reproducing the
pivot's own cached grid as its reconciliation region. Once the extractor of section 7.1 exists this
is gradeable in both directions -- the conversion must recompute the summary and must **not** read it
as a source -- and it needs no `visible_cells` trickery, because a sheet consumed without being
declared is visible in the plan alone.

**Number 9 is non-negotiable 6 under pressure.** `Allocation` was last saved with calculation set to
manual, so its cached values are real but stale -- `PARTIAL_CACHED_VALUES` fires. A conversion that
reconciles against them reports a pass against numbers Excel itself would disown. The correct
outcome is that region declared not reconciled, with a reason, and the headline reading `CHECKED WITH
EXCEPTIONS` -- the mechanism CLAUDE.md records as `not_reproduced`, exercised for the second time and
for a completely different cause than the first.

**Number 10 is where the harness's own limits show, and that is worth grading too.**
`harness/drive.py` executes cells in file order; marimo does not. A plan that follows tab order emits
a notebook whose `Working` cells reference `Entity Map` names defined below them -- which marimo
resolves by dataflow and the harness reports as a `NameError`. The scaffolder emits in topological
order regardless (`ordering_warnings` is explicitly *"not an error"*), so this should pass. If it
does not, the eval has found either a scaffolder bug or a harness limit, and the two are told apart
by opening the file. Either is worth a day.

---

## 5. Predictions

A diagnostic that predicts nothing is just a bigger test. These are falsifiable and should be
recorded before the first run, so the eval can be **wrong**.

| # | Prediction | Confidence | Basis |
|---|---|---|---|
| P1 | Triage returns `proceed_with_care`, complexity 0.68-0.75 | High | Measured on the probe: 49 ops gave 0.665 |
| P2 | The plan's `dropped` list arrives with 15+ entries and is cleared in one action | High | `acknowledge_all_drops` exists and is the path of least resistance |
| P3 | The `Post` column is dropped, or kept with a reason that does not mention that a person runs it | Medium-high | It ranks last, it is an `info` `dead_region`, and there are seventeen like it |
| P4 | `Summary` is planned as a **data source** rather than as a derived aggregate | Very high | Measured: a real pivot classifies `role=data`, 0 operations, 0 findings |
| P5 | The plan carries 1-3 open questions, satisfying `open_questions_warning` | High | The guard is `if self.open_questions: return None` -- binary, on a continuous quantity |
| P6 | `reconciliation_values` comes back with under half its entries resolvable | Medium-high | The default is the whole frame; the key is the workbook's own header |
| P7 | The tier lookup is translated as an exact join or a `when/then` ladder rather than `join_asof` | Medium | The hint is one line in a digest of 50 operations |
| P8 | `--convert` cost rises 3-5x against `adjustment_signoff` | High | ~14 `TODO(kedge)` holes against 6, over a larger context |

P5 deserves its own line, because it is a small fix with a real consequence. `open_questions_warning`
returns `None` the moment the list is non-empty, whatever the complexity. One question on a
0.37 workbook is proportionate; one question on a 0.75 workbook is a model that has understood a
tenth of it and said so once. The quantity is continuous and the guard is a boolean.

If P1, P4 and P5 hold and everything else fails to reproduce, the eval has still paid for itself:
those three are defects in committed code that no existing test can reach.

---

## 6. The rubric, in outline

Ids are the contract between `expected.yaml` and `case.py`; `tests/unit/test_evals_harness.py`
asserts the two sets match in both directions. Three tiers, weighted as `adjustment_signoff` weights
them.

### Deterministic (~42 points)

| id | w | What it checks |
|---|---|---|
| `ran_to_completion` | 2 | every scripted input lands, nothing raises, nothing is still waiting |
| `tier_lookup_is_banded` | 4 | the band boundary is right at every tier edge, including the lowest and highest bands |
| `opening_balance_is_ordered` | 3 | the prior-row column survives an input frame in a shuffled order |
| `proration_matches_excel` | 3 | part-period day counts, on the month boundary and on the serial boundary |
| `overrides_are_surfaced_not_applied` | 4 | all three appear with their reasons; none is silently reproduced |
| `does_not_drop_the_posting_column` | 4 | the eighteenth dead region survives; the other seventeen may go |
| `dead_regions_are_individually_reasoned` | 2 | no two drops share a reason verbatim |
| `subtotal_rows_are_excluded` | 3 | the embedded subtotals do not reach the aggregation |
| `leading_zero_client_codes_survive` | 3 | `00417` is still `00417` after ingestion and after the join |
| `text_formatted_fees_are_typed` | 2 | the pasted bps column is numeric before arithmetic touches it |
| `stale_region_is_not_reconciled` | 4 | `Allocation` declared `not_reproduced`; headline reads `CHECKED WITH EXCEPTIONS` |
| `reconciliation_map_resolves` | 3 | every entry in `reconciliation_values` yields a vector, or says why not |
| `generated_sql_is_valid` | 3 | executed against DuckDB, as `adjustment_signoff` does it |
| `no_posting_before_approval` | 3 | via `visible_cells`: the posting panel does not render until the checkpoint is recorded |
| `the_notebook_says_why_this_process_exists` | 3 | briefing present, prose cites the `Sign-off` tab |
| `a_blocked_step_says_which_step_it_is` | 2 | reused verbatim |
| `no_pandas` | 1 | reused verbatim |

### Structural (~20 points)

`pivot_is_derived_not_read` (4) -- `Summary` appears in the plan as a stage computed from
`Allocation`, with the pivot's grouping keys and aggregation named, and **not** as a `StageSource`.
Graded in both directions, because reading it as a source is the wrong answer and omitting it
entirely is merely an incomplete one. Expected to fail before section 7.1 lands; that failure is the
baseline the extractor is measured against.

`overrides_are_a_checkpoint` (4) -- `names_the_tier_table_as_a_lookup_source` (3) --
`has_a_checkpoint_before_posting` (2) -- `stage_count_is_proportionate` (2), 8-16, because a
50-operation workbook in four stages has not been decomposed and in forty has not been understood --
`open_questions_scale_with_complexity` (3), at least three on a 0.7 workbook --
`consults_the_knowledge_pack` (3), gradeable at last if a pack for the billing schema is added.

### Judgement (not scored, recorded to be read)

Would somebody who has never seen the workbook know, from the notebook alone, why three clients' fees
differ from the formula? Does the conversion say what it did about the pivot tab, or only omit it? Is
the tier table presented as a thing that changes, or baked in as constants?

---

## 7. What has to change

Two lists, and they are different in kind. The **harness** changes are what phase 1 needs to run at
all. The **product** changes are what the workbook shows kedge cannot yet do, of which one -- the
pivot extractor -- is already established as needed rather than merely predicted, so it is specified
here in full.

### 7.1 A pivot extractor: `analysis/pivots.py`

Established by measurement in section 2.4, not waiting on the eval. Pivot tables are ubiquitous in
finance processes, openpyxl already parses every part of one, and today a pivot's rendered grid
misclassifies as `role=data` -- so the cost of the gap is a wrong answer rather than a missing one.

`PowerQueryExtraction` is the exact precedent and the shape to copy: an extractor that is normally
absent, degrades to a status plus a `Finding` rather than an exception, and hangs off
`WorkbookAnalysis` beside its peers.

```python
class PivotTable(_Frozen):
    name: str                       # 'FeeSummary'
    sheet: str                      # where the rendered grid sits
    location: str                   # 'A3:E11' -- the cached result, in A1
    source_sheet: str | None        # 'Allocation'
    source_ref: str | None          # 'A1:F199'
    source_name: str | None         # a defined name or table, when that is the source
    row_fields: list[str]
    column_fields: list[str]
    page_fields: list[str]          # filters
    data_fields: list[PivotDataField]   # (field, caption, aggregation)
    refreshed_on: datetime | None
    stale: bool                     # source has more rows than the cache recorded

class PivotExtraction(_Frozen):
    status: ExtractionStatus = ExtractionStatus.ABSENT
    tables: list[PivotTable] = Field(default_factory=list)
    detail: str | None = None
```

Five consequences, each small and each in an existing seam:

1. **A `LogicalOperation` per pivot**, with `excel_pattern=ExcelPattern.PIVOT` -- which finally
   assigns the enum member that already exists, already appears in the prompt, and already carries
   the translation hint `.group_by(...).agg(...) or .pivot(...)`. `ranges` is the rendered grid, so
   the pivot reconciles against its own cached output like any other region, and `references` points
   at the source range so it lands in the dependency graph as a **descendant** of the sheet it
   aggregates. That single edge is what stops a plan reading it as an input.
2. **`classify.py` learns the role.** A sheet whose cells are wholly inside a pivot's `location` is
   `output`, never `data`. This is the fix for the misclassification, and it is a shape heuristic of
   exactly the kind that module already contains.
3. **Two findings.** `PIVOT_SOURCE_UNRESOLVABLE` when the cache source is an external connection or
   an OLAP cube rather than a worksheet range -- genuinely unconvertible without the warehouse, and
   an honest blocker. `PIVOT_CACHE_STALE` when the recorded cache is narrower than the source range
   now is, because the grid on screen is then answering a question about last month's data.
4. **`triage` counts them.** A pivot is real process, so it belongs in `complexity`; an unresolvable
   source belongs in `verification_blockers`, since the code can be written and the numbers cannot be
   proved.
5. **`SCHEMA_VERSION` goes to `1.1`.** Additive with a default, so a `.kedge/analysis.json` written
   by an older build still validates.

Deliberately not in scope for 7.1: calculated fields and calculated items (a formula stored inside
the pivot, which is a small expression language of its own), and `GETPIVOTDATA` references, which are
formulas the analyser already tokenises and which should resolve to the pivot's operation id once
there is one to resolve to. Both are follow-ups, and both should be findings rather than silence in
the meantime.

### 7.2 Harness changes for phase 1

Three, all limits `adjustment_signoff` happened not to hit:

1. **The two-hand-in tuple.** `harness/grade.py:61` and `harness/convert.py:377` both do
   `pre, post = case.write_handins(...)`. This workbook has three inbound artifacts (positions
   extract, fee schedule paste, prior-month positions). A mapping, not a tuple. The
   `rebate_payfile` proposal identifies the same line, which is a good sign that it is genuinely the
   seam and not a quirk of either case.
2. **`drive.py`'s stand-in `mo`.** It raises on anything outside its surface, by design and rightly.
   A wider workbook plausibly wants `mo.ui.multiselect` for the override review; add it when the
   scaffolder actually emits it, not before.
3. **A rot guard proportionate to the workbook.** `tests/unit/test_evals_adjustment_signoff.py`
   checks every figure `expected.yaml` quotes against what the generator produces, and the README
   notes it has already caught the rubric going stale once. With ten discriminations and forty
   graded figures that test is not optional, and it should assert the *structural* targets too --
   operation count, pattern count, complexity band -- because a generator refactor that quietly
   collapses the workbook back to seven operations (see 1.1) would otherwise pass everything while
   measuring nothing.

### 7.3 The product changes the diagnosis already points at

Four, beyond 7.1. Each is small, each has its evidence in section 2, and the reason they are listed
separately from 7.1 is sequencing rather than doubt -- the eval should measure the current behaviour
once before it is changed, so the fix has a before and an after.

| Change | Evidence | Shape |
|---|---|---|
| A second ranking key in `_operation_digest` | 2.1 | Rank a flagged `dead_region` or a generated-SQL region above the zero-fan-out tail, so a manual carry cannot be truncated away |
| `*_omitted` keys for the six silent caps | 2.5 | Make the docstring's promise true; `hostile.xlsx` is the regression test and it already fails |
| Complexity-scaled `open_questions_warning` | P5 | Expect questions in proportion to the workbook rather than at least one |
| A `kedge.xl` banded-lookup helper | 4, disc. 1 | Approximate-match `VLOOKUP` is Excel semantics, and non-negotiable 3 says Excel semantics live in `kedge.xl` -- a sorted `join_asof` with the boundary condition fixed in one place and tested against Excel |
| Read `t="str"` when counting cached values | 2.6 | An empty-string result is a calculated value; today it reports as a gap, with a remediation the user cannot follow. The attribute is in the sheet XML the handle already holds |

`acknowledge_all_drops` (2.2) is deliberately absent from that table. The right answer there is a
design decision rather than a defect -- possibly a cap on how many drops one note may cover, possibly
nothing at all -- and it should be settled after seeing a real eighteen-drop plan, not before.

---

## 8. Phasing

The order matters, and it is not the order `adjustment_signoff` was built in. That case
hand-wrote its gold answer first and, in its own words, *"the reference conversion is a worked example
of the destination; it was never evidence that kedge can get there"* -- five real defects hid behind
it until `--convert` was built.

1. ~~**Generator and rot guard only.**~~ **Done.** `m11_management_fee_run.xlsx` lands at 49
   operations and complexity 0.699, and `tests/unit/test_evals_fee_billing_run.py` asserts the band
   in both directions -- verified by injecting the collapse it exists to catch (chain the columns and
   the workbook falls to 13 operations at 0.503; both assertions fire). Still to finish before the
   spike is the real generator: cached values via the corpus's `inject_cached_values`, the `Summary`
   pivot built by Excel over COM, and the written narrative.
2. **Run today's pipeline and write down what happened.** `kedge inspect`, `triage`, a real plan
   proposal, `scaffold`. Score the eight predictions in section 5. **This is where the value is**, and
   it arrives before a single grader is written.
3. **`expected.yaml` and `case.py`**, informed by step 2 rather than by guesswork about what will be
   hard.
4. **The pivot extractor (7.1)**, with its own unit tests over an Excel-built fixture. It sits here
   rather than first only so that step 2 records the `role=data` misclassification once, as the
   before; it does not depend on anything in steps 1-3 and could equally land ahead of them.
5. **`--convert` a model through it**, from the beginning and not at the end.
6. **The three remaining product changes (7.3)**, each measured against the run from step 2.
7. **The reference conversion last, not first** -- deliberately inverting the first eval's order. Its
   only jobs are to prove the graders can pass and to serve as the worked example; writing it first
   is what let the graders be shaped around a hand-written answer that hid the defects.

Sizing: the generator is the bulk of it, perhaps 900-1,200 lines because heterogeneity cannot be
looped. The rubric and graders scale with the ten discriminations, call it 1,200. The pivot extractor
is small -- openpyxl does the parsing, so it is a mapping layer, a role heuristic, two findings and a
schema bump, call it 300 lines plus tests. Steps 1 and 2 together are maybe a quarter of the work and
return most of the diagnostic value, which is the same argument `rebate_payfile` makes for its own
phase 1 and is the right way round for both.

---

## 9. Deliberately out of scope

- **Not a robustness fixture.** No macros, no `.xlsb`, no circular references, no unresolvable
  external links. `hostile.xlsx` owns those and triages to `stop`; a workbook that refuses to convert
  cannot be graded on the quality of its conversion.
- **Not a second process shape.** One input arriving in two forms plus a schedule, one hand-off, one
  sign-off. Two sources joined and an outbound file are `rebate_payfile`'s subject and the two
  proposals should stay separable, or neither result is attributable.
- **Not a parsing gauntlet.** Two format nasties only -- text-formatted numbers and the leading-zero
  code -- and both carry process meaning.
- **Still nothing about the chat loop.** Workbook in, notebook out.
- **Not a second specification.** A discrimination here must be gradeable against a workbook and a
  conversion, not against machinery that has to be designed first. That is what keeps this eval a
  measurement. It is *not* a licence to declare a hard construct out of scope: section 7.1 exists
  precisely because pivot tables are common enough that the answer is to build the extractor. The
  test is "can this be graded?", never "can kedge already do this?"

---

## 10. Open questions for the author

1. **Is the manual override block a checkpoint or a hand-in?** It is a person's judgement recorded in
   cells, which is `checkpoint`. But next month's overrides are different cells, so arguably it is an
   input that should arrive as a hand-in each run. `StageKind`'s membership test -- *does the
   scaffolder emit something different for it?* -- says checkpoint. The workbook's own habit says
   hand-in. I think checkpoint and I am not confident.
2. **Where does an unresolvable pivot source stop the run?** Settled: a worksheet-sourced pivot is
   extracted and translated (section 7.1). What is still open is the pivot whose cache comes off an
   external connection or an OLAP cube -- no `worksheetSource`, nothing in the workbook to recompute
   from. That is genuinely unconvertible without the warehouse, so it is a `verification_blocker` at
   least; whether it should also be a *conversion* blocker depends on whether the rest of the
   workbook stands without it, which the analyser can see and the triage scorer currently cannot
   express.
3. **How hard is too hard?** If this workbook triages to `stop`, the eval is unrunnable and has to be
   softened. If it triages to `proceed`, it may not be stressing triage at all. `proceed_with_care`
   is the target and it is a narrow band to hit deliberately -- which is itself worth knowing about
   the triage scorer.
4. **Should `expected.yaml` pin the predictions in section 5?** Recording them as gradeable items
   makes the eval self-documenting about what it expected to fail and turns each fix into a visible
   score movement -- the same thing `EXPECTED_DEFECTS` does in `tests/unit/test_evals_convert.py`,
   which went from five entries to an exact empty set. The risk is a rubric that scores kedge against
   yesterday's opinion of itself.
5. **Does a second eval mean a second reference conversion is affordable?** `adjustment_signoff`'s is
   817 lines and is the single most expensive artifact in `evals/`. Phasing it last (section 8) is
   partly an argument that it might not be needed at all until the graders have been proven some
   other way.
