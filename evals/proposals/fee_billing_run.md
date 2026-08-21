# Proposal: `fee_billing_run` -- the same process, a much harder spreadsheet

Status: **built, and mostly no longer a proposal.** The workbook, the pivot, the rot guard, the
rubric, the graders and a reference conversion all exist, and
`uv run python evals/run.py fee_billing_run` drives that conversion end to end and scores it
**47/47 on the deterministic tier, with eight honest skips** -- six items waiting on a reference
plan (five structural ones, plus `dead_regions_are_individually_reasoned`, which sits in the
deterministic tier and reads `plan.dropped`), `pivot_is_derived_not_read` waiting on the extractor
of 7.1, and `consults_the_knowledge_pack` waiting on a pack for `fin.fee_invoice`. Phase 1 step 2 ran offline
and is scored in section 5.1; the defects it paid for are in 5.2 and their `src/` fixes are in the
tree. Step 3's graders were then reviewed adversarially and largely rewritten, which is section 5.3
and the most important measurement in this document.

Still genuinely proposed: the pivot extractor of 7.1, the remaining product changes of 7.3, a
reference plan and therefore the whole structural tier, and `--convert`. **No model endpoint has
been called against this workbook at any point**, so nothing here is evidence about a model.

Every figure in sections 1 and 2.1-2.6 was measured against the tree at `305fe62`, by running the
real analyser and the real `build_proposal_context` over the committed fixtures and over three probe
workbooks built for this proposal -- one of them a genuine pivot table constructed by driving Excel
over COM, per CLAUDE.md's rule that ground truth about Excel is generated rather than reasoned about.
Sections 2.7, 3, 5.1 and 5.2 were measured later against the real workbook, and say which state of
it where the two differ. Section 5.3's figures come from the review itself and from re-running the
graders after it. Sections 2.7, 3 and 4 were re-measured against the **committed** workbook in the
final pass, once the two gaps section 3 used to record were closed; where an older figure is kept
because it belongs to a run that happened, it is dated. Where a claim is a prediction rather than a
measurement it says so.

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

**Nothing in this repository had ever been converted at a complexity above 0.368.** Every claim
kedge made about handling real spreadsheets rested on one workbook that its own scorer calls
one notch above simple. That is the state this proposal argues against, and the table above is the
one it was written from; `m11_management_fee_run.xlsx` now sits at 0.699 with a reference conversion
scoring full marks over it, which is the whole of the answer to this section.

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

## 2. Seven things that are true today, and only a bigger workbook can show

Each is verifiable in the tree as it stands. Three are measurements from the probe above; two are
present-tense defects reachable on a **committed fixture** right now. The sixth was found by
*building* the workbook rather than by reading the code, which is the case for this eval in
miniature -- it surfaced before a single grader existed. The seventh (2.7) was added afterwards,
when 2.5's silent truncation stopped being a statement about a robustness fixture and started
firing on this eval's own workbook.

### 2.1 The one operation that must never be dropped is buried in the planner's context

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

**On the workbook itself it does not rank last.** This heading read *"sorts dead last"* until step 2
measured it; section 3 had already contradicted it and nothing noticed. `post_a4_a87` ranks **32 of
50**: the tiebreak inside fan-out 0 is the operation id, and `post_a4_a87` sorts ahead of every
`working_*` id, so the manual carry *heads* a nineteen-strong zero-fan-out tail rather than trailing
it. That leaves the argument intact and changes its shape. The hazard is being undifferentiated, not
being last, and a ranking fix has to lift the region out of a block of nineteen rather than off the
bottom.

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

**Measured, the prediction in that paragraph is wrong, and the reason indicts the generator rather
than the mechanism.** 81% of the map resolves on this workbook, because the headers it was built
with normalise straight onto the names a conversion would choose anyway. See P6 in section 5.1:
until the generator emits headers a conversion would genuinely rename, this section is a claim the
eval does not test.

### 2.4 A pivot table reads as a data sheet, and everything needed to translate it is already on disk

This is the sharpest of the seven, and it is not a gap. It is a **silent wrong answer**.

Grep the whole tree first. `ExcelPattern.PIVOT` appears in exactly three places -- a translation-hint
table and a description table in `analysis/regions.py`, and a second hint table in
`notebook/scaffold.py` -- and in **no** assignment. `classify_pattern` cannot return it; nothing
else sets `excel_pattern`. Nothing anywhere in `src/` reads a `pivotCache` or a `pivotTable` part.
Because
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

### 2.7 That silent truncation is not a fixture's problem; it is firing on this workbook now

Section 2.5 demonstrates the defect on `tests/fixtures/hostile.xlsx` -- a workbook that triages to
`stop` and will therefore never be converted, which makes the whole thing easy to read as a limit
that only bites the fixture built to bite. It is not. `analyse` over
`m11_management_fee_run.xlsx` as the pivot first landed it, alone in a directory of its own:

```
analysis findings              41
digest findings                40
keys announcing truncation     []
finding discarded              [info] dead_region  Recon!G2
```

The pivot is what tipped it. `Summary`'s rendered grid earns a `preamble_rows` finding, which took
the workbook from 40 findings to 41 and put it one over `_MAX_FINDINGS`; the planner is handed
forty of them and told nothing, on the very workbook this eval exists to measure conversion quality
against.

Where the `Post` column's own flag sits in that ranking is what makes this more than arithmetic.
`_finding_digest` sorts on severity alone and `sorted` is stable, so the `info` block keeps
analysis order:

```
   39. [info] dead_region  Working!M104
   40. [info] dead_region  Post!A4
   41. [info] dead_region  Recon!G2      <-- discarded by the cap
```

`Post!A4` is at **40 of 41**: the last surviving position, with no headroom at all. One more finding
anywhere in the workbook and the flag on the one region that must never be dropped leaves the
planner's context with no announcement.

**That took under an hour, and it has already been spent once.** While this section was first being
written another change to the generator landed and the analysis went to 42 findings, one of them a
`duplicate_header` **warning** -- and a warning sorts above the whole `info` block, so it pushed the
tail down by one and `Post!A4` was discarded alongside `Recon!G2`. The flag on the manual carry was
not one finding away from being dropped; it was being dropped, and the change that did it was the
fix for P6, headers a real spreadsheet would have, which is exactly what section 5.1 argues for.

Later work on the generator took the count back to 41, and the readout above is the one the
**committed** workbook gives today: 41 analysed, 40 digested, nothing announcing the cut, `Recon!G2`
discarded, `Post!A4` back at the last surviving position. That is not a reprieve. It is the same
reading as before with one finding of slack in it, the workbook has been over the cap continuously
since the pivot landed, and the slack is whatever the generator does next -- once already it went
without anyone intending it or noticing. Two things follow. The cap is not a distant limit
that a hostile fixture reaches; it is a live constraint that ordinary work on this eval crosses, and
it does not need an `info` finding to do it. And this proposal treats 2.1 and
2.5 as independent hazards, where on this workbook they are one hazard converging on the same
eighty-four cells: the region is undifferentiated in the operation digest *and* one finding away
from being absent from the finding digest, and nothing in either says so. `*_omitted` keys (7.3)
stop being tidiness and start being the thing that makes the eval measurable.

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
| `Post` | One `INSERT ... ` per invoice, built by `&`. The manual carry. Nothing reads it, and one of the names it posts is `O'Hanlon & Reid Nominees`, so the workbook's own statement for `00041` is invalid SQL. |
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
complexity           0.699     target (0.68, 0.75)
verdict     proceed_with_care  must not be 'stop'
convertible           1.00
reconcilable ops        50     of 50
inferred regions        45
sheets                  10
cross-sheet ops         21
findings                41
dead regions            19
dependency edges        60
column profiles         80
manual carry        ranks 32 of 50  (fan-out 0)
digest truncation     none
```

**One correction to that readout, and one that used to be needed and no longer is.**

The one that has gone: `--calibrate` used to *rebuild* the workbook before measuring it, and the
pure-Python build has no `Summary` pivot -- openpyxl reads a pivot table and cannot author one, so
the committed file is finished by driving Excel (`build_pivot.py`). Asking the calibration question
therefore deleted discrimination 7 and then answered about a file that no longer matched the
committed one, and the readout had to be corrected by hand from 0.698 / 40 findings / 77 profiles to
the pivot-bearing figures. Building and measuring are now separate operations: `--calibrate` and
`--verify-with-excel` read the file on disk and write nothing, and a plain rebuild over a
pivot-bearing workbook is *refused* unless `--with-pivot` will put the pivot back or `--force`
accepts losing it. The block above is what the committed workbook prints today, uncorrected. (0.698
and 40 findings are still what a pure-Python build scores, which is why section 8 quotes both; the
rendered grid is worth a `preamble_rows` finding, three column profiles and a thousandth of
complexity.)

The correction that stands: `digest truncation none` is true of operations and **false of
findings**. The line prints
`[k for k in context if k.endswith("_omitted")]`, and `build_proposal_context` sets an `_omitted`
key for operations and for column profiles and for nothing else -- which is the defect of section
2.5, so the readout is reporting through the very blind spot it would need to see past. At 41
findings against `_MAX_FINDINGS` of 40 the digest is already truncating, silently, and what it
discards is a `dead_region`. Read the line as `operations none; findings 41 -> 40, unannounced`,
and section 2.7 for the measurement.

**Two known gaps in the workbook itself, recorded here when they were open. Both are now closed,
and what closing the second cost is the more interesting half.**

The first was a promise the tab table made and the built file did not keep. `Positions` held its
extract SQL in cells, but there was no `xl/connections.xml` in the zip, so `connections` analysed as
0 -- `adjustment_signoff` had a real ODBC connection part and this one did not, which made the one
thing this eval was built to vary, difficulty, *lower* here on that axis than on the simple
workbook. The generator now writes one: an ODBC `dbPr` carrying the DSN, the description and the
query, and `analyse(...).connections` is **1**, `kind='odbc'`, `command_type='sql'`, the credential
redacted out of `connection_string_redacted` and the stored query's newlines matching
`EXTRACT_SQL`'s line for line. That is what makes "the extract is a step to hand over" a claim the
analyser can check twice rather than once -- from the SQL a person left in the cells, and from the
query Excel itself stored. `test_the_extract_query_is_also_a_real_connection` pins all of it. It
also uncovered a defect in the reader, which is 5.2's sixth entry.

The second was sharper, and it was about a non-negotiable: **no literal anywhere in this workbook
needed SQL escaping.** `Post` inserted a five-digit client code, a month string and a number and the
client *name* never reached the statement, so naive `&` concatenation produced valid SQL, and
`generated_sql_is_valid` could not tell a statement rendered through `kedge.sql` from one built by
concatenation -- while the grader's own failure text said *"render them through `kedge.sql` rather
than by concatenation"*. Client `00041` is now **`O'Hanlon & Reid Nominees`**, `Post!A` carries the
name into the statement, and the workbook's own line reads

```sql
INSERT INTO fin.fee_invoice (client_code, client_name, period_month, fee_gbp)
VALUES ('00041', 'O'Hanlon & Reid Nominees', '2026-11', 18500);
```

That is seven single quotes where a valid single-row statement has six, and a syntax error at the
moment somebody pastes it into a production client. Non-negotiable 3 is tested here now the way
`adjustment_signoff` tests it with `O'Brien & Partners`, and by the same pair of rot guards:
`test_the_workbooks_own_generated_sql_is_broken_for_the_apostrophe_client` asserts the workbook's
version is genuinely broken, so a conversion that fixes it can be told from one that never looked,
and `test_kedge_sql_renders_that_same_statement_correctly` pins the fix so its absence is a failing
assertion rather than a quiet one.

**What closing that cost is worth recording, because the natural formula is the wrong one.** The
client name sits one lookup away in `Working!B`, which is where a person writing this column would
reach for it. Nothing in the workbook reads `Working!B` -- it is a display column -- so it is one of
the nineteen `dead_region` findings that discrimination 5 hides the manual carry among, and pointing
`Post` at it *revives* the region. Measured, by swapping the eighty-four references in the committed
file's `Post` sheet part and re-analysing:

```
committed  ('Entity Map'!$B{n}) : 19 dead regions, 41 findings, complexity 0.699, 60 edges
variant    (Working!$B{n})      : 18 dead regions, 40 findings, complexity 0.698, 61 edges
```

Two figures the rubric quotes -- `facts.dead_regions` and `facts.complexity` -- move because of a
formula that reads better, and the haystack discrimination 5 is built on gets one straw smaller. The
name therefore comes off `'Entity Map'!$B{n}` by position, which `Working!B` already reads, so the
edge was there anyway and the measured structure is untouched; the offset is constant, so the column
still normalises to one R1C1 string and stays one logical operation.
`test_the_posting_column_reads_nothing_that_was_dead` guards the choice, because it is the kind of
thing a tidy-up reverses without knowing what it is paying.

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
- **The workbook sits deliberately under `_MAX_OPERATIONS`.** At 50 the planner sees every
  operation, so the eval measures conversion quality rather than truncation. Truncation is a
  different question and the cheap way to ask it is a *variant* of this workbook past 80, not a
  second eval.

Note what is *not* in that list: no macros, no external links, no circular references, no `.xlsb`. Those
are `hostile.xlsx`'s job and they triage to `stop`. This workbook must triage to `proceed` or
`proceed_with_care`, because an eval about conversion quality that refuses to convert measures nothing.

---

## 4. What it discriminates

Ten, each carrying process meaning rather than being a parsing quirk. The README's own warning
applies and is respected: *"a case that fails on ten parsing quirks measures the reader, not the
conversion."*

**This heading used to say "each planted so it fails independently". Measured, that was false**, and
what it says instead is below the table: eight scored discriminations, and one honest coupling.

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

### Eight scored discriminations, and one honest coupling

Two of the ten carry no points at all. Discrimination 7 is an unconditional `SKIP` until the pivot
extractor of 7.1 exists -- by design, and argued below. Discrimination 10 has no item in either
scored tier; it appears only in the unscored judgement list. So the ten are **eight** for scoring
purposes, and the headline figure was overstating coverage by two.

Independence was worse, and it was worse in the direction that matters.
`expected.yaml` marks `leading_zero_client_codes_survive` as expected to fail today, because polars'
own CSV inference turns `00013` into `13` before kedge sees it. Feeding the graders an
otherwise-perfect conversion whose only defect was `client_code` arriving as `Int64` cost **19
points across 6 items**: `tier_lookup_is_banded` (4), `proration_matches_excel` (3),
`overrides_are_surfaced_not_applied` (4), `text_formatted_fees_are_typed` (2) and
`generated_sql_is_valid` (3) all went red alongside the item that is actually about the codes. A
conversion that did everything right except the one thing the rubric predicts will fail scored 30 of
49, and five of its six red items pointed at things it had done correctly. That is not a rubric
measuring ten discriminations. It is a rubric measuring one, six times, and reporting it as a
verdict on the whole conversion.

The fix is `_norm_code` in `case.py`: every grader that matches a client by code pads the code back
to its full width before matching, so each item measures its own discrimination. Padding is not
forgiveness -- `leading_zero_client_codes_survive` reads the column raw and still fails. Measured
after the change, the same defect costs **6 points across 2 items**.

**The coupling that is left is real, and keeping it is the correct answer.** The two that still fail
together are `leading_zero_client_codes_survive` (3) and `generated_sql_is_valid` (3). They fail
together because a client code posted as `'7'` into a ledger keyed by client code posts against a
client that does not exist. The statement is valid. DuckDB executes it. The invoice lands on nobody,
or on somebody else. Normalising the code inside the SQL grader would hide precisely the consequence
that makes the defect worth three points elsewhere: this is not a formatting preference, it is a
wrong row in a finance table. So that grader compares what the notebook actually posted against the
eighty-four codes the workbook bills, unpadded, and reports the miss in those words. One defect, two
red items, and the second is the one that says what the defect *costs*.

**`generated_sql_is_valid` now carries a second discrimination, and it is not a new number in the
table.** Client `00041` is `O'Hanlon & Reid Nominees` and `Post!A` puts the name in the statement
(section 3), so the workbook's own concatenated line is invalid SQL and a conversion that reproduces
its text reproduces a syntax error. That is a property of the same item rather than an eleventh
discrimination -- the grader executes what the notebook posted, and an unescaped apostrophe stops
being valid before any of it is compared -- but it is what makes the failure message honest:
*"render them through `kedge.sql` rather than by concatenation"* now names something the grader can
actually detect.

Six of the ten are worth expanding.

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
nineteen, eighteen of which genuinely should go, and the one that must stay heads an
undifferentiated nineteen-strong zero-fan-out tail in the planner's context (section 2.1) and can be
dismissed along with the rest by a single `acknowledge_all_drops` (section 2.2). Same question, and
the first time it has been asked in a way that can distinguish understanding from luck.

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
| P3 | The `Post` column is dropped, or kept with a reason that does not mention that a person runs it | Medium-high | It ranks 32 of 50, at the head of a nineteen-strong zero-fan-out tail; it is an `info` `dead_region` and eighteen others are too |
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

(P3's basis above is a correction. It originally read *"it ranks last"*, which section 3 had
already contradicted two pages earlier and 5.1 measures again.)

### 5.1 What the first run found

Phase 1 step 2, scored. **Measured offline: no model endpoint was called and no network request
was made.** Everything below is a deterministic run of `src/` over the workbook as the pivot first
landed it, isolated in a directory of its own -- `analyse()` attaches sidecar documents from the
workbook's own folder, so measuring a copy sitting beside `.py` files adds a spurious
`document_attached_by_filename` finding and, at this finding count, tips the digest into the
truncation of 2.7. Anyone reproducing these numbers should isolate the file first.

Two of the eight -- P3 and P7 -- are wholly predictions about what a *model* produces and could not
be run at all; they are recorded as untested rather than as anything else. **A prediction that
could not be tested has not passed**, and nothing below leans on one.

| # | Prediction | Verdict | What was actually measured |
|---|---|---|---|
| P1 | triage `proceed_with_care`, complexity 0.68-0.75 | **CONFIRMED** | `proceed_with_care`, complexity **0.699**, `convertible` 1.00, one verification blocker (`partial_cached_values`, coverage 3,133 / 3,214) |
| P2 | `dropped` arrives with 15+ entries and is cleared in one action | **PARTIALLY CONFIRMED** | The bulk clear is exact: 19 `DroppedRange`s, one `acknowledge_all_drops`, 19 unacknowledged to 0, one shared note, `is_approvable` False to True. 19 dead regions are available. What a model lists was not measured. |
| P3 | The `Post` column is dropped, or kept without mentioning that a person runs it | **NOT TESTABLE OFFLINE** | Needs a plan. Substrate only: rank 32 of 50, fan-out 0, digest 50 of 50 with nothing omitted. |
| P4 | `Summary` is planned as a data source rather than a derived aggregate | **PARTIALLY CONFIRMED** | The analyser half is confirmed on the eval's own workbook: `role=data` at confidence **0.853**, 0 operations, 0 dependency edges. What a plan does with it needs a model. |
| P5 | The plan carries 1-3 open questions, satisfying `open_questions_warning` | **CONFIRMED** (mechanism) | 0 questions warns; 1 and 5 return the identical `None`. The count a model writes was not measured. |
| P6 | `reconciliation_values` comes back with under half its entries resolvable | **REFUTED** | 26 of 32 -- **81%** -- resolve on an exact name match. |
| P7 | The tier lookup is translated as an exact join or a `when/then` ladder | **NOT TESTABLE OFFLINE** | Needs `--convert`. The recognition works: `vlookup_approx`, hint `.join_asof(...)`, rank 27 of 50. |
| P8 | `--convert` cost rises 3-5x against `adjustment_signoff` | **PARTIALLY CONFIRMED** | Context 70,547 characters against 17,493 -- **4.0x**, and every step of a turn re-sends it. Holes 12 against 6 -- **2.0x**, not the ~14 the basis assumed. The cost itself needs a model. |

P2, P5 and P8 are half-and-half in the same way: a testable mechanism wrapped around an untestable
model judgement, and only the mechanism is scored above. Only P1 and P6 are wholly deterministic,
and one of those two came back wrong -- which is the return on writing predictions down.

Three are worth expanding.

**P6 is refuted, and that is the most actionable thing the run produced.** The prediction's basis
was that *"a conversion that is doing its job renames `'Fee @ tier bps'` to `fee_gbp`"*. On this
workbook the premise does not hold, because the generator wrote headers that were already tidy:
`'Avg AUM (GBP)'` normalises to `avg_aum_gbp`, `'Gross fee (GBP)'` to `gross_fee_gbp`, `'Net fee
(GBP)'` to `net_fee_gbp`, `'Days in month'` to `days_in_month`. Those are the names a conversion
would pick anyway, so `_normalise_name` and the translator agree by accident. Measured on that
workbook, **41 of the 45 inferred regions come back with a column name already**, and
simulating resolution against a frame named exactly as the regions expect resolves 26 of 32
entries. Six cannot resolve whatever a conversion calls its columns: four are single-cell
operations below `infer_regions`' `min_cells=2`, and two are the `column=None` halves of the
seeded-first-row shape described in 5.2.

Say it plainly: **section 2.3 is not currently being tested by this workbook.** The mechanism it
describes is real and the failure mode it names is real, but a rubric written against 81% would be
grading a coincidence. The fix is not to the mechanism; it is headers a real spreadsheet would
have -- `'Fee @ tier bps'`, `'AUM £m (avg)'`, `'Fee - net of disc.'`, a duplicate header, an
untitled column -- which is a change to `build_workbook.py` and belongs ahead of the
`reconciliation_map_resolves` grader. Note also that resolution is an exact string match with no
fallback (`if column and column in names`), so each of the 26 is one rename away from failing;
81% is a measurement of the generator, not a margin of safety.

That change is already being made -- a `duplicate_header` warning is in the working tree's analysis
as this is written -- and it is what tipped the finding digest over the cap in 2.7. Worth noticing
that the two are connected: making the workbook realistic enough to test 2.3 is what made 2.5 fire
on the region 2.1 is about.

**P5 is confirmed exactly as its own paragraph argues.** `open_questions_warning` on this
workbook's complexity:

```
0 open question(s) at complexity 0.699 -> 'no open questions on a workbook with complexity 0.70
                                           - an empty open_questions list here is itself
                                           suspicious; check that nothing has been silently
                                           guessed at'
1 open question(s) at complexity 0.699 -> None
5 open question(s) at complexity 0.699 -> None
```

One question silences the warning completely; the difference between one and five is nothing at
all. And the message is identical in substance at 0.37 and at 0.70 -- complexity is interpolated
into the prose and gates nothing but the `< 0.35` early return, so the warning is structurally
unable to say *"one question is not enough for this workbook"*. A plan that asked three questions
about a fifty-operation workbook is, as far as review is concerned, indistinguishable from one
that understood it.

**P4's predicate now reproduces on the eval's own workbook rather than on a probe.** Section 2.4
was measured on a purpose-built two-sheet probe; the pivot is in `m11_management_fee_run.xlsx`
now, five pivot parts in the zip, and it reads:

```
Summary: role=data  confidence=0.853  rows=20  cols=4  formula_cells=0
  role_signals: ['20 rows', 'no formulas at all', 'header row at row 4',
                 '3 preamble row(s) above the header', 'mostly numeric values',
                 'small sheet (20 rows) of mostly literals']
  operations on Summary:  []
  findings on Summary:    ['preamble_rows']
  edges naming Summary:   []
  profiles: [('A', 'Row Labels'), ('B', '2026-11'), ('C', '(blank)'), ('D', 'Grand Total')]
pivot-classified operations anywhere in the workbook: []
```

The confidence is the sharp part. At **0.853** the analyser is not hedging: it is making a
well-evidenced claim that a derived aggregate is an input, and a plan that believes it is
believing the evidence it was given. `'Row Labels'` and `'Grand Total'` give it away to a human
and to nothing in `src/`. Section 7.1 is what closes it, and the structural grader
`pivot_is_derived_not_read` now has a real before to be measured against.

### 5.2 Defects found since this was written

Six, in the order they would change somebody's day. Four are `src/` defects, all fixed in the
working tree and not yet committed; one is a pair of generator bugs, fixed, with an Excel oracle
added so the class cannot recur unseen; and one is a hazard in how the eval is *maintained* rather
than anything in `src/` at all. None of them was reachable by reading the code, which is the
argument for step 2 in one paragraph. The last of the six arrived with the workbook's final pass
rather than with step 2, and it is filed here because it is the same kind of thing found the same
way: by making the workbook more like a real one and looking at what came back.

**1. `operation_reference` collapses a discontiguous operation into one rectangle, so a correct
conversion fails reconciliation.** The worst of the five. `infer_regions` built a region's
reference by reconstructing a rectangle from the anchor, the cell count and the orientation,
ignoring `operation.ranges` entirely -- and the `Allocation` fee column is discontiguous precisely
because the subtotal rows of discrimination 6 are embedded in it:

```
allocation_e2_e87
  operation.ranges : ['Allocation!E2:E29', 'Allocation!E31:E58', 'Allocation!E60:E87']
  reconstructed    : Allocation!E2:E85          <-- wrong, and silent

  baseline read from it (truncated=False, no finding, no warning):
    positions differing from the truth: 56 of 84
    subtotal values that leaked in:     [2449100.34, 2843188.09]
    real client rows lost:              [70958.19, 53791.68]
```

Two `SUBTOTAL` figures -- each about twenty-eight times a normal row -- read as client fees, two
real clients fall outside the rectangle and are never compared, and every row after the first
break is compared against its neighbour. The same bug on the totals row read `Working!M104:N104`
where the region is `M104` and `P104`, so the second value was an empty cell. This lands on
discrimination 6 and on `reconciliation_map_resolves`, and its consequence is non-negotiable 6
inverted and no less corrosive: a **correct** notebook reported as disagreeing with the workbook on
56 of 84 rows, on the panel whose whole job is to be believed.

Being fixed concurrently, and the fix is in the working tree, uncommitted: `RegionSpec` now
carries the ranges themselves, `read_baseline` reads them in order and concatenates, a region
whose ranges cannot be enumerated exactly degrades to a new `incomplete` status and
`NotReconciledReason.BASELINE_RANGE_INCOMPLETE` rather than falling back to the rectangle, and
`operation_reference` is documented as the *enclosing* rectangle instead of pretending to be the
region. Measured against the working tree as it stands, `allocation_e2_e87` reads 84 values with
no subtotal above a million among them and `70958.19, 53791.68` present at the end, and
`working_m104_p104` reads `(8230033.34, 8123322.62)`. It should be committed before any grader is
written against the old behaviour.

**2. Opening the workbook in Excel destroys the eval, and nothing announced it.** Measured, not
reasoned about: opened with Excel's default automatic calculation, a pivot added and the file
saved, **34 of 5,253 cached values move** -- and five of them are the five the eval is built
around. `Allocation` is planted stale, holding the fees as they stood before the three overrides
were agreed, which *is* discrimination 9; Excel recalculates it into agreement with `Working`,
`Recon` follows, and the discrimination is gone from a file that still opens, still analyses and
still passes every assertion that does not look at those five cells. `fullCalcOnLoad="0"` does not
prevent it -- Excel compares the stored `calcId` against its own engine version and recalculates
anyway. Setting `Application.Calculation = xlCalculationManual` **before** the workbook is opened
takes it to **0 of 5,253**; it has to be before, because a workbook opened while the application
is on automatic has already recalculated by the time the property is settable, and the property is
application-level, which Excel refuses to set with no workbook open at all. `build_pivot.py` opens
an empty scratch workbook first for exactly that reason, and re-reads every cached value
afterwards so the guard cannot rot in silence.

**3. Two generator bugs that only Excel could have found.** The cached values are a parallel
implementation of the sheet, so nothing but Excel can say whether they are right, and asking it
found two things no amount of reading the code would have. `VLOOKUP` over a text-formatted column
returns **text**, so seventeen `tier_bps` cells held a float where Excel holds `'20.0'` -- the
`Fee Schedule` tab's numbers-as-text is discrimination 8, and it propagates further than the tab
it is planted on. And Excel's `&` renders an integral number without its trailing `.0`, so every
generated statement whose fee happened to be a round number disagreed with the workbook's own
text. Both are fixed, and the oracle is now a mode rather than a one-off:
`build_workbook.py --verify-with-excel` recalculates a **copy** and reports every cached value
that moves. Run on the committed workbook: **5,309 cells compared, 11 moved, all 11 on
`Allocation` and `Recon`, 0 unexplained** -- 5,309 rather than the 5,253 above because the pivot's
rendered grid is itself cached cells. It fails both ways: an unexplained move means the Python
model has drifted from Excel, and *nothing* moving means the deliberate staleness has gone.

**4. A bare `&` concatenation classified as `arithmetic`, with the hint `col("a") * col("b")`.**
The manual carry itself -- the one region section 2.1 is about. `classify_pattern` keyed off
`parsed.outermost_function`, and a bare `&` has none, so it fell through to `ARITHMETIC if
parsed.references else UNKNOWN`. Every structured field pointed away from the truth at once:
pattern `arithmetic`, description *"Arithmetic over neighbouring cells on Post"*, hint to
multiply, `functions: []`. For contrast `=LEFT(A19,3)&"-"&E19` classified correctly as
`text_manipulation`, purely because `LEFT` was outermost -- so the column building a three-character
key was described accurately and the column building SQL was not. Fixed in the working tree by
classifying a function-less formula on its top-level operator, with the mixed case decided by
precedence rather than by counting: `&` binds looser than every arithmetic operator and tighter
than every comparison one, so `="Total "&A2*B2` is a string with a product inside it. The digest
now reads `pattern: 'text_manipulation'`, `translation_hint: 'col("x").str.* expressions'`.

**5. The approximate-`VLOOKUP` description named the sheet holding the formula as the one to
sort.** *"An approximate-match lookup, which assumes the table is sorted on Working"* -- where the
table that must be sorted is the band table on `Fee Schedule` and `Working` is the frame being
looked up *from*. `_describe` appended `" on {sheet}"` to every headline, and `VLOOKUP_APPROX` was
the only headline in the table ending in a word (`"sorted"`) that binds it as a prepositional
object. So the single most load-bearing sentence in the digest, on the construct section 4 calls
the highest-value translation risk in the proposal, actively misdirected -- and a `join_asof` with
the wrong frame sorted is silently wrong rather than loudly wrong. Fixed in the working tree: the
caveat is a sentence of its own, it resolves the lookup table's own sheet from the call's second
argument, and where it cannot resolve one all-or-nothing it names none rather than guessing. It
now reads *"Approximate matching assumes the lookup table on Fee Schedule is sorted ascending by
its first column -- that table, not the sheet holding the formula."*

**6. `kedge.analysis.connections` could not read a connection part Excel itself wrote.** Found by
closing section 3's first gap. The generator authors `xl/connections.xml` with the query's newlines
as `&#10;` character references, which is what the
reader is built for -- XML attribute-value normalisation turns a literal newline inside an attribute
into a space, so multi-line SQL has to be escaped somehow. Excel rewrites the part on every save and
escapes it *differently*: the newlines come back as `_x000a_`, the `ST_Xstring` convention of
ECMA-376 22.9.2.19, and `commandType="2"` is dropped outright because `2` is the attribute's schema
default. `_parse_connection` decoded neither. So after one round trip through Excel the extract query
read back as a single unbroken line with `_x000a_` littered through it, and `command_type` came back
`None` where the authored part gives `sql`. Neither is malformed, so nothing complained.

That is not cosmetic. `build_proposal_context` puts `connection.command` in front of the planner as
the query to hand over, so a workbook a user had opened once -- which is every workbook -- would
show the model a corrupted extract, on the construct `adjustment_signoff`'s discrimination 1 is
built on.

**Why nothing found it before.** This eval's connection part is the first in the repository to make
a round trip through Excel. `tests/fixtures/legacy_sql.xlsx` has one, but it is hand-authored and has
never been re-saved, so its `&#10;` escaping was read back by the same convention that wrote it --
the shape of tautology `tests/corpus`'s hand-written manifest exists to avoid, arriving through a
part rather than through an assertion. It is the mirror of entry 3 above: the cached values needed
Excel as an oracle, and so did the connection part.

**Fixed in the working tree, uncommitted, alongside the other three.** `_decode_xstring` undoes
`_xHHHH_` in `_attr`, so every `ST_Xstring` attribute the module reads is decoded in one place rather
than at each call site, and `_command_type` supplies the schema default when a `dbPr` carries a
command with no `commandType` -- but only then, since naming the command type of a `dbPr` that has no
command would be an invented claim rather than a recovered one. Two details in the decoder are worth
the reading: it never rescans its own output, because `_x005f_x000a_` is a column *called* `_x000a_`
and a search-and-replace gets that backwards; and an escaped surrogate pair is round-tripped through
UTF-16 rather than left as a lone surrogate, which would be a `UnicodeEncodeError` in whatever
serialises the analysis -- non-negotiable 4, a malformed workbook costing a finding rather than a
traceback. `tests/unit/test_connections.py` pins the form Excel actually writes, and both edge cases.

`build_workbook.restore_connection` still puts the authored part back after `--with-pivot` drives
Excel, and now that the reader handles both forms it is byte-stability rather than a workaround: the
committed file matches what `build()` writes, so a rebuild is comparable. It was a workaround when it
was added, and it is worth remembering which it was.

Four smaller findings from the same run, recorded because they change what section 6 can grade
rather than what the pipeline does:

- **`DroppedRange` does not enforce distinct reasons.** Nineteen identical ones validate;
  *"'Unused' alone is not a reason"* is field-description prose with no validator behind it. The
  `dead_regions_are_individually_reasoned` grader therefore has **no machinery to build on** and is
  not free.
- **A column with a seeded first row is unreconcilable at both ends.** `working_g19` -- the
  opening-balance carry, discrimination 2's own column -- is one cell, below `min_cells=2`, so it
  gets no region at all; `working_g20_g102` gets `column=None` because `_header_above` reads the
  cell directly above the anchor, which is the *other half of the same column*. The same happens
  to `working_h19` / `working_h20_h102`. That shape is ordinary in finance spreadsheets.
- **`to_vector` falls back to the only column of a single-column frame whatever was asked for**,
  including when the region's `column` is `None` -- the case it is least entitled to guess at.
- **A `dead_region` finding cannot be joined to its operation.** `Finding` has no `operation_id`
  and its `location` is the anchor cell (`'A4'`), not the range (`'Post!A4:A87'`); 17 of 19 fail a
  naive string match. 7.3's ranking fix needs an anchor join, which is a cost it does not yet
  price in.

### 5.3 The adversarial review, and what it says about the graders

**This is the section to read if you read one.** Everything else in this document argues that the
workbook is hard. This is the only evidence that the *rubric* measures rather than flatters, and
when it was written it did not.

The graders were reviewed adversarially before any reference conversion existed. Method: read-only
over `build_workbook.py`, `expected.yaml`, `case.py` and the harness, with every finding below
produced by building a stub `Context` -- or a real driven notebook -- and calling the grader, rather
than by reading it. Nothing under `src/`, `evals/` or `tests/` was modified to produce them.

What the review found *not* to be wrong is worth stating first, because it is the part the rest of
this document rests on: the workbook's difficulty is real and every planted discrimination the
reviewer could check is present -- 50 operations, 19 dead regions, complexity 0.699, 45 reconcilable
regions, a real pivot on `Summary`, `vlookup_approx` present, `subtotal` on `Allocation`, `Post` at
fan-out 0, triage `proceed_with_care` -- and all 42 figures in the rubric's `facts` recompute
exactly from `build_workbook.compute()`.

The graders were another matter. **Ten confirmed defects**, each demonstrated rather than argued:

| # | Defect | At risk |
|---|---|---:|
| 1 | **A conversion that reconciled *nothing* scored 7 of 7.** Declare all 45 regions `not_reproduced` and the report renders "CHECKED WITH EXCEPTIONS - 0 of 0 claimed regions passed", which does not begin `NOT RECONCILED`, so `stale_region_is_not_reconciled` passed; `reconciliation_map_resolves` counts only regions that are unreconciled *and* not declared, so declaring everything emptied the set it grades. Non-negotiable 6 failing inside the eval written to test non-negotiable 6 | 7 |
| 2 | **33 of the 49 deterministic points could leave the denominator through `SKIP`.** A completed run missing `reconciliation` was read as "a naming difference rather than a stop" -- but that is a name the scaffolder *fixes*, so a completed notebook without it has not named it differently, it has not reconciled. `_no_grid` did the same for a conversion keying on a column outside the recognised list: twelve more points | 33 |
| 3 | **The exact-join failure passed the two items that exist to catch it.** `tier_lookup_is_banded` skipped null rows in its per-row loop and appended to `checked` anyway, so a join matching nothing was graded as agreeing; with an unrecognised AUM column it checked nothing at all and said so in the detail of a `PASS` | 6 |
| 4 | **`does_not_drop_the_posting_column` had its polarity inverted on the one path that matters.** It failed a plan that reviewed the proposed drop and *refused* it -- `accepted=False` means the range is kept, and the grader never read the field -- while passing any prose that merely names the table, a briefing sentence included | 4 |
| 5 | **`generated_sql_is_valid`, three ways.** `dict.fromkeys` deduplicated the statements, so a notebook posting every invoice twice graded as 84; no per-client amount was checked for 81 of the 84, so two clients invoiced each other's fee passed; and statements bound as a `list[str]` or as a frame column -- the natural shape -- were invisible to it, failing a correct conversion | 3 |
| 6 | **`no_posting_before_approval` read source text, not behaviour.** A correctly gated notebook failed because a constants cell held `POSTING_TABLE = "fin.fee_invoice"`, and the remedy the message offered was unfollowable; a notebook assembling the name from parts skipped instead, removing the points | 3 |
| 7 | **The frame-finding helpers latched onto the widest frame.** `opening_balance_is_ordered` compared one frame, so a genuinely order-dependent column went ungraded when a wider order-free frame existed; the same shape let a correct intermediate mask a wrong final | 3 |
| 8 | **Four structural items passed on the wrong evidence** -- a checkpoint matched by its *id*, any hand-off gated by any checkpoint, a stage sourcing the schedule's two preamble rows. Worst, `dead_regions_are_individually_reasoned` read `DroppedRange.reason`, which the proposing model writes, while `acknowledge_all_drops` -- the bulk dismissal it exists to detect -- writes `note`. It was blind to its own subject by construction, and passed a plan that dropped nothing at all | 11 |
| 9 | **Two graders printed a `PASS` whose own detail described work they had not done.** `subtotal_rows_are_excluded` passed when the fee column was named something it did not recognise, one line below a comment saying that would be "exactly the vacuous pass this rubric is trying not to award"; the briefing item passed on `mo.md("Billing. Tiered. Allocation.")` | 6 |
| 10 | **Correct conversions failed on style.** "Upload" was not in the instruction-verb list, though it is the verb for `mo.ui.file`; three overrides rendered as a table rather than as markdown were "nowhere in what the notebook shows"; and the briefing citation `'Sign-off'!A3:A4` -- the form Excel itself writes, because the sheet name has a hyphen -- failed a literal `"Sign-off!"` match | 9 |

**An eleventh was found later, by writing the reference conversion, and no stub could have found
it.** `stale_region_is_not_reconciled` guarded the headline with a single negative,
`not startswith("NOT RECONCILED")`, over a precondition (`if not report.passed`) that asks only
whether *any* region passed. Neither half consulted `report.failed`. So a report reading
`FAILED - 32 passed, 1 failed` satisfied both and paid out four points for handling reconciliation
honestly while carrying an unexplained mismatch -- on the item whose entire job is to tell a
decision from a break. Both claims are now made positively: no region may be in `report.failed`,
and the headline must actually read `CHECKED WITH EXCEPTIONS` rather than merely avoid one prefix.

Three rules came out of the fixes, and they generalise past this case:

- **`_pass()` must never be reachable with nothing checked.** Three graders printed a pass over an
  empty `checked` list. A grader that cannot measure its subject fails or skips; it does not
  congratulate.
- **An omission is not an ambiguity.** `SKIP` is reserved for a notebook that stopped before the
  cell this item is about, a missing optional dependency, a plan that was not supplied, or a thing
  kedge cannot do yet. A completed run missing a name the scaffolder fixes is a failure.
- **Require positive evidence, not the absence of a bad word.** The reconciliation items now assert
  `rows_compared > 0` and a floor on how much of the workbook was actually compared
  (`RECONCILED_SHARE`, half of the 45 reconcilable regions), because "declared with a reason" must
  not be a way to empty the denominator a headline is computed over.

Measured after the rewrite, on the same artifacts:

| Conversion | Before | After |
|---|---|---|
| A stub computing a flat fee per client code -- tiers nothing, prorates nothing, surfaces no override, reconciles nothing, posts nothing | **10/37**, eleven items skipped | **6/47**, one item skipped |
| A notebook that runs and binds nothing at all | 33 of 49 points removed from the denominator | **2** removed -- and that 2 is the plan-dependent item, skipped because no plan was supplied. It scores 1/47 |
| The reference conversion | -- | **47/47** |

The one skip left in the deterministic tier is honest and it names what would clear it. That is the
difference between a denominator and a rounding-up.

Two defects the reviewer confirmed early were fixed by concurrent work before the review ended, and
are recorded rather than claimed: the rot guard now recomputes `facts` from
`build_workbook.compute()`, and the rubric/grader parity tests now exist for this case -- previously
they were bound to `adjustment_signoff` alone, so `case.py`'s claim that "the contract tests assert
the two sets are identical in both directions" was, for this case, false.

**What this says about the phasing.** Section 8 argues for writing the reference conversion last,
inverting `adjustment_signoff`'s order, on the grounds that a hand-written gold answer lets graders
be shaped around it. That is now measured rather than asserted: ten defects were found by probing
graders that had never seen a passing notebook, and the eleventh was found by writing one. Neither
order finds both. What matters is that the graders were attacked before anything was claimed on
their behalf.

---

## 6. The rubric

Written as an outline and now built: 24 items over 70 points, and the tables below are what
`expected.yaml` holds rather than what it was hoped it would hold. Ids are the contract between
`expected.yaml` and `case.py`, and `tests/unit/test_evals_fee_billing_run.py` asserts the two sets
match in both directions -- in this file, not in `test_evals_harness.py`, whose parity tests are
bound to `adjustment_signoff` and once left this case's identical claim untested. Three tiers,
weighted as `adjustment_signoff` weights them.

### Deterministic (17 items, 49 points -- the outline said ~42)

| id | w | What it checks |
|---|---|---|
| `ran_to_completion` | 2 | every scripted input lands, nothing raises, nothing is still waiting |
| `tier_lookup_is_banded` | 4 | the band boundary is right at every tier edge, including the lowest and highest bands |
| `opening_balance_is_ordered` | 3 | the prior-row column survives an input frame in a shuffled order |
| `proration_matches_excel` | 3 | part-period day counts, on the month boundary and on the serial boundary |
| `overrides_are_surfaced_not_applied` | 4 | all three appear with their reasons; none is silently reproduced |
| `does_not_drop_the_posting_column` | 4 | the nineteenth dead region survives; the other eighteen may go |
| `dead_regions_are_individually_reasoned` | 2 | no two drops share a reason verbatim |
| `subtotal_rows_are_excluded` | 3 | the embedded subtotals do not reach the aggregation |
| `leading_zero_client_codes_survive` | 3 | `00007` is still `00007` after ingestion and after the join |
| `text_formatted_fees_are_typed` | 2 | the pasted bps column is numeric before arithmetic touches it |
| `stale_region_is_not_reconciled` | 4 | `Allocation` declared `not_reproduced`, no region in `report.failed`, headline reads `CHECKED WITH EXCEPTIONS` |
| `reconciliation_map_resolves` | 3 | every entry in `reconciliation_values` yields a vector or says why not -- **and** at least `RECONCILED_SHARE` of the 45 regions were actually compared |
| `generated_sql_is_valid` | 3 | executed against DuckDB, as `adjustment_signoff` does it |
| `no_posting_before_approval` | 3 | via `visible_cells`: the posting panel does not render until the checkpoint is recorded |
| `the_notebook_says_why_this_process_exists` | 3 | briefing present, prose cites the `Sign-off` tab |
| `a_blocked_step_says_which_step_it_is` | 2 | reused verbatim |
| `no_pandas` | 1 | reused verbatim |

### Structural (7 items, 21 points -- the outline said ~20)

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

**What is actually scored today.** No reference plan is committed for this case, so the six
plan-dependent items skip -- five structural ones plus `dead_regions_are_individually_reasoned`,
which lives in the deterministic tier but reads `plan.dropped`. `pivot_is_derived_not_read` and
`consults_the_knowledge_pack` skip for want of machinery rather than for want of a plan. The live
denominator is therefore **47 of 70**, the reference conversion scores 47/47 over it, and the report
prints `structural: 0/0` rather than folding an unscored tier into a flattering total. Every one of
those eight skips names what would make it gradeable.

**What that run actually reconciles**, since 5.3's first defect was a report that reconciled nothing
and scored full marks for it. `uv run python evals/run.py fee_billing_run`, on the tree as this pass
was written:

```
reconciliation_map_resolves    34 of 45 region(s) compared over 2,609 rows, 11 declared with reasons
stale_region_is_not_reconciled CHECKED WITH EXCEPTIONS - 34 of 34 claimed regions passed
```

Eleven declarations, not one, and they do not all say the same thing. Ten are `not_reproduced` --
the four 2024 columns the plan drops, stale `Allocation`, the `Post` hand-off kedge renders through
`kedge.sql` so there is no cached text to match, and the `Recon` cells that read the stale tab -- and
one is `no_usable_baseline`, which is a different claim and the more accurate of the two on the
region it is made about. `Working!V` is the override flag: the notebook computes it, and the
*workbook* has nothing to check it against, because the eighty-one non-override rows cache as empty
strings that openpyxl hands back as `None` (section 2.6). Saying "this conversion does not reproduce
it" would be a claim about the conversion where the fact is about the spreadsheet.
`reconciliation_map_resolves` counts both as resolved, and the comment in `case.py` says why:
counting the accurate declaration as unresolved would punish a conversion for declaring precisely.

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
    name: str  # 'FeeSummary'
    sheet: str  # where the rendered grid sits
    location: str  # 'A3:E11' -- the cached result, in A1
    source_sheet: str | None  # 'Allocation'
    source_ref: str | None  # 'A1:F199'
    source_name: str | None  # a defined name or table, when that is the source
    row_fields: list[str]
    column_fields: list[str]
    page_fields: list[str]  # filters
    data_fields: list[PivotDataField]  # (field, caption, aggregation)
    refreshed_on: datetime | None
    stale: bool  # source has more rows than the cache recorded


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

Three, all limits `adjustment_signoff` happened not to hit. Two are now done:

1. ~~**The two-hand-in tuple.**~~ **Done.** `harness/grade.py` and `harness/convert.py` both did
   `pre, post = case.write_handins(...)`, an assumption about every case there will ever be. This
   workbook has three inbound artifacts (positions extract, fee schedule paste, prior-month
   positions), so `write_handins` returns a **mapping** here and the harness accepts either shape.
   The `rebate_payfile` proposal identifies the same line, which was a good sign that it was
   genuinely the seam and not a quirk of either case.
2. **`drive.py`'s stand-in `mo`.** It raises on anything outside its surface, by design and rightly.
   A wider workbook plausibly wants `mo.ui.multiselect` for the override review; add it when the
   scaffolder actually emits it, not before. Nothing has needed it yet.
3. ~~**A rot guard proportionate to the workbook.**~~ **Done.**
   `tests/unit/test_evals_fee_billing_run.py` recomputes every figure
   `expected.yaml` quotes from `build_workbook.compute()`, and asserts the *structural* targets too
   -- operation count, pattern count, complexity band -- because a generator refactor that quietly
   collapses the workbook back to seven operations (see 1.1) would otherwise pass everything while
   measuring nothing. It also guards things no figure would catch: that the posting column reads
   nothing that was dead (section 3), that the workbook's own SQL is still broken for the apostrophe
   client, and that measuring never rebuilds.

### 7.3 The product changes the diagnosis already points at

Five, beyond 7.1. Each is small, each has its evidence in section 2, and the reason they are listed
separately from 7.1 is sequencing rather than doubt -- the eval should measure the current behaviour
once before it is changed, so the fix has a before and an after. Step 2 has now measured all five
(5.1), and it moved two of them: the ranking key has to lift a region out of a nineteen-strong tail
rather than off the bottom, and the `*_omitted` keys are no longer a claim about a fixture.

| Change | Evidence | Shape |
|---|---|---|
| A second ranking key in `_operation_digest` | 2.1, 2.7 | Rank a flagged `dead_region` or a generated-SQL region above the zero-fan-out tail, so a manual carry cannot be truncated away. Note the analyser has no notion of a region carrying generated SQL, so that half needs a detector first -- `kedge.sql.changes_data` already returns `True` on the rendered statement, and nothing reads it |
| `*_omitted` keys for the six silent caps | 2.5, 2.7 | Make the docstring's promise true. `hostile.xlsx` is one regression test and it already fails; this eval's own workbook is the other, at 41 findings against a cap of 40 |
| Complexity-scaled `open_questions_warning` | P5 | Expect questions in proportion to the workbook rather than at least one |
| A `kedge.xl` banded-lookup helper | 4, disc. 1 | Approximate-match `VLOOKUP` is Excel semantics, and non-negotiable 3 says Excel semantics live in `kedge.xl` -- a sorted `join_asof` with the boundary condition fixed in one place and tested against Excel |
| Read `t="str"` when counting cached values | 2.6 | An empty-string result is a calculated value; today it reports as a gap, with a remediation the user cannot follow. The attribute is in the sheet XML the handle already holds |

`acknowledge_all_drops` (2.2) is deliberately absent from that table. The right answer there is a
design decision rather than a defect -- possibly a cap on how many drops one note may cover, possibly
nothing at all -- and it should be settled after seeing a real nineteen-drop plan, not before.

---

## 8. Phasing

The order matters, and it is not the order `adjustment_signoff` was built in. That case
hand-wrote its gold answer first and, in its own words, *"the reference conversion is a worked example
of the destination; it was never evidence that kedge can get there"* -- five real defects hid behind
it until `--convert` was built.

1. ~~**Generator and rot guard only.**~~ **Done.** `m11_management_fee_run.xlsx` lands at 50
   operations and complexity 0.698 -- 0.699 as committed, with the pivot -- and
   `tests/unit/test_evals_fee_billing_run.py` asserts the band in both directions, verified by
   injecting the collapse it exists to catch (chain the columns and the workbook falls to 13
   operations at 0.503; both assertions fire). The three things that were outstanding are in: cached
   values via the corpus's `inject_cached_values` (3,133 of 3,214, the 81-cell gap of 2.6), the
   `Summary` pivot built by Excel over COM (`build_pivot.py`), and the written narrative (six
   process notes off `Overrides` and `Sign-off`).
2. ~~**Run today's pipeline and write down what happened.**~~ **Done, offline: section 5.1.** All
   eight predictions scored against committed code, one refuted, three recorded as untestable
   without a model. It paid for itself before a grader existed -- five defects (5.2), of which the
   discontiguous-region one would have had the graders shaped around a kedge bug.
3. ~~**`expected.yaml` and `case.py`**~~ **Done, and then attacked: section 5.3.** 24 items over 70
   points, informed by step 2 rather than by guesswork about what would be hard. Both things step 2
   changed about it held: the `reconciliation_map_resolves` grader waited for the generator's
   headers (P6), and `dead_regions_are_individually_reasoned` had to be written from scratch
   because nothing enforces distinctness -- it is the one deterministic item that still skips,
   because it reads `plan.dropped` and there is no reference plan. The adversarial review of these
   graders found ten defects and is the most valuable thing in this document.
4. **The pivot extractor (7.1)**, with its own unit tests over an Excel-built fixture. It sits here
   rather than first only so that step 2 records the `role=data` misclassification once, as the
   before; it does not depend on anything in steps 1-3 and could equally land ahead of them.
5. **`--convert` a model through it**, from the beginning and not at the end.
6. **The three remaining product changes (7.3)**, each measured against the run from step 2.
7. ~~**The reference conversion last, not first**~~ **Done, and the inversion paid.** Its only jobs
   are to prove the graders can pass and to serve as the worked example; writing it first is what
   let the first eval's graders be shaped around a hand-written answer that hid the defects. It
   scores 47/47 over the live denominator, and writing it found the eleventh grader defect (5.3) --
   the one no stub could reach, because it needed a report that had genuinely reconciled most of
   the workbook and failed on part of it.

   The mutation suite that was outstanding behind it is now in the tree:
   `tests/unit/test_evals_fee_billing_run_controls.py`, 46 scenarios, mostly stub contexts rather
   than textual mutations of the reference notebook -- at this complexity a mutation big enough to
   change what a grader sees is usually big enough to stop the run, and a stopped run proves
   nothing. It goes further than `adjustment_signoff`'s in one way that 5.3 argues for directly:
   every grader is controlled in **both** directions, and a test enforces that rather than trusting
   the list, because a suite that only checks the failing direction is satisfied by a grader that
   fails everything.

   One thing is still outstanding: a reference **plan**, which is what unblocks the whole structural
   tier and the last deterministic skip.

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
