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
| `run.py` | The CLI. |
| `adjustment_signoff/` | A four-tab manual process — extract, adjust, re-extract, sign off — and whether kedge turns it back into the runbook it came from. |

```bash
uv sync --group evals                                 # duckdb, for executing generated SQL
uv run python evals/run.py adjustment_signoff         # grade the reference conversion
uv run python evals/run.py adjustment_signoff \
    --notebook path/to/converted.py --plan path/to/plan.yaml
uv run python evals/run.py adjustment_signoff --json  # for a CI step
```

Exit code is 0 when nothing gradeable failed.

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

## What this eval does not cover

- **Nothing about the chat loop.** This is workbook in, notebook out. Whether the agent
  *converses* well about the conversion is a different eval.
- **The harness cannot drive a notebook that names its widgets differently.** The script is keyed
  by variable name, so a conversion that calls its selector something else is reported as unused
  inputs. Honest, but a real limitation of grading a runbook without a kernel.
- **No knowledge-pack item.** `consults_the_knowledge_pack` skips: `context/databases/example.yaml`
  describes a different schema. Adding a pack for `fin.accruals` would make it gradeable.
- **One workbook, one process shape.** A monthly adjustment with a sign-off. Nothing here says
  anything about reconciliations, allocations, or anything with a Power Query in it.
