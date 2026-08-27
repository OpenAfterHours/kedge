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
| `harness/render.py`, `align.py`, `findings.py`, `convert.py` | Phase 2: scaffold a plan, have a model fill every hole, render, drive, grade. |
| `harness/cellprompt.py`, `cellgen.py` | Name maps onto `kedge.agent.fillprompt` and `kedge.agent.fill`. **The product fills the holes; this eval calls it.** They were copies once, and the two prompts had already drifted apart by six bytes -- which is the thing `cellprompt.py` was written to forbid. Aliases rather than wrappers, so there is nothing to keep in step. |
| `run.py` | The CLI. Also where the two phases are *composed*: `--plan-from` proposes the plan with a model, approves it, and hands it to Phase 2. |
| `adjustment_signoff/` | A four-tab manual process — extract, adjust, re-extract, sign off — and whether kedge turns it back into the runbook it came from. |

```bash
uv sync --group evals                                 # duckdb, for executing generated SQL
uv run python evals/run.py adjustment_signoff         # grade the reference conversion
uv run python evals/run.py adjustment_signoff \
    --notebook path/to/converted.py --plan path/to/plan.yaml
uv run python evals/run.py adjustment_signoff --project path/to/workbook.kedge  # both, resolved
uv run python evals/run.py adjustment_signoff --json  # for a CI step

uv run python evals/run.py adjustment_signoff --model MODEL --repeats 3   # plan, per model
uv run python evals/run.py adjustment_signoff --convert MODEL             # notebook, per model
uv run python evals/run.py adjustment_signoff --convert MODEL --plan-from MODEL  # the whole thing
uv run python evals/run.py adjustment_signoff --model MODEL --dry-run     # resolve, spend nothing
```

Exit code is 0 when nothing gradeable failed — and non-zero when *nothing was graded at all*,
because a sweep whose every leg skipped has not passed, it has not run.

The first three call no model. The last three do, and they are the only things here that spend
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
  second run, in the style of `tests/llm/test_plan_judgement.py`. Eleven items and 27 of the 72
  points the rubric declares — one of which, `consults_the_knowledge_pack`, always skips, so a
  full run scores out of 71 and the deterministic tier is 45 of them, or **62.5%** of what is
  declared. Still the lopsidedness this list exists to declare: a plan that reads well and
  produces the wrong pennies has failed. The share is asserted in
  `tests/unit/test_evals_adjustment_signoff.py`, because a bound of the form "deterministic is
  bigger" lets it drift a tier at a time without anything going red.
- **judgement** — recorded to be read, not scored.

**The structural tier grades shape, not presence.** It used to grade presence, and a real
model-written plan passed four of the seven items it could be graded on — 10 of 18 — while
scaffolding into a notebook with no briefing, no hand-off, a checkpoint below the update rather
than above it, and one place to put a grid in a process that brings back two. Five items were
rewritten or added around what the scaffolder actually consumes:

| Item | Was | Is |
|---|---|---|
| `takes_two_handins` | at least one stage declares a hand-in with a `ref` | the hand-in cells `build_cells` *emits*. A re-extract declared on a `checkpoint` stage is read by the schema, shown on the approval card, and emitted as nothing |
| `has_a_checkpoint_before_the_update` | some hand-off names a checkpoint in `depends_on` | a checkpoint reachable through the whole `depends_on` closure of the hand-off whose statement **writes** |
| `mutates_agrees_with_the_statement` | — | `changes_data` over the text, whatever `medium` and `mutates` claim; and a `handoff` stage must carry a statement at all |
| `the_briefing_survives_the_workbook` | — | a non-empty `briefing`, with a purpose, a background, and a citation that names a sheet the workbook has |
| `the_re_extract_waits_for_the_update` | — | **where**, not whether: a hand-in emitted below the writing hand-off, reading its confirmation token |

Two of those are worth the extra sentence.

`Briefing` refuses prose with no sources, so an *invented* briefing cannot reach a plan at all —
and nothing anywhere noticed one that never arrived. The asymmetry was the bug, and the notebook
it produced opened by telling its reader the workbook carried no description of what the process
was for, which four cited notes on the `Sign-off` tab flatly contradict. The item asks that a
citation point somewhere, too: `sources: ["nowhere in particular"]` satisfies the schema, and an
unfollowable citation is the appearance of attribution rather than attribution.

`the_re_extract_waits_for_the_update` is the only item here that grades **where** rather than
whether, and it exists because every other item can pass on a notebook that invites the one
mistake nobody can detect afterwards. Point the re-extract stage at the extract instead of at the
update — one edge — and `build_cells` emits the re-extract selector seven cells *above* the
UPDATE with no gate token in it, so marimo has no dataflow edge to hide it on and the box is on
screen from the moment the notebook opens. That plan scored full structural marks, and the same
total as the correct one when the reference cell bodies were replayed through it -- 65/68, at the
rubric state before this item existed. A re-extract
taken before the statement ran looks exactly like one taken after; the verification passes either
way. It is the defect `Handoff.needs_confirmation` was changed to prevent, and nothing measured
whether the prevention worked.

That model-written plan is committed verbatim as `tests/unit/observed_conversion.py`, and
`test_the_plan_that_got_past_the_loose_tier_does_not_get_past_this_one` grades it item by item.
**5 of 19** on the plan-only measurement — the one a sweep takes, and the only honest one for a
plan, since `does_not_trust_the_impact_summary` is graded against a *notebook* the plan had no
hand in. The loose tier gave it 10 of 18. It is a better input than any mutation of the reference
— a mistyped `kind` on a re-extract is not a mistake a test author makes.

Note what two items do on it rather than failing: `mutates_agrees_with_the_statement` and
`the_re_extract_waits_for_the_update` are both **blocked**. A plan that hands nothing over has no
statement for a flag to contradict and no update for a re-extract to wait for, and reporting
either as a failure would name the wrong defect. `hands_over_rather_than_pretends` is the item
about that, and it is red. They are blocked rather than skipped, so their five points stay in the
denominator: the plan scored 5/19 while the very defect that cost it those points also removed
them from what it was scored out of. It now scores 5/24.

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
| rename a widget so the run stops | `ran_to_completion` (the rest **block**, not fail — and stay in the denominator) |
| add `Adjustment!G` to `dropped` | `does_not_drop_the_sql_column` |
| clear the plan's `briefing` | `the_briefing_survives_the_workbook` |
| a briefing with cadence and audience and no `sources` | `the_briefing_survives_the_workbook` |
| `mutates: false` over the `UPDATE` | `mutates_agrees_with_the_statement` (and *not* the checkpoint item, which reads the statement) |
| move the checkpoint below the update | `has_a_checkpoint_before_the_update` |
| gate only the read-only extract | `has_a_checkpoint_before_the_update` |
| a checkpoint two stages upstream | **nothing** — reachability, not one edge |
| `mutates: true` on the read-only extract | **nothing** — over-declaring is the safe direction |
| retype the re-extract's stage as `checkpoint` | `takes_two_handins` |
| both hand-ins declare the same `ref` | `takes_two_handins` — one grid asked for twice |
| the `UPDATE` retyped `medium: text`, `mutates: false` | `mutates_agrees_with_the_statement` |
| strip the `handoff` block off the update stage | `mutates_agrees_with_the_statement` |
| `sources: ["nowhere in particular"]` | `the_briefing_survives_the_workbook` |
| point the re-extract at the extract, not the update | `the_re_extract_waits_for_the_update` |

The last two rows of the first block are the ones worth keeping. A notebook that stops has one
problem, and reporting it eight times buries it — so items about cells that never ran are not
failures. And a plan that drops the SQL column is the sharpest failure the eval has: plausible,
well-reasoned, and it deletes the step that changes the data.

### The denominator

Four outcomes per item, and the fourth is the one with a story.

`SKIP` is for an item nobody could grade **through nobody's fault** — duckdb not installed, no
plan supplied, no knowledge pack describing `fin.accruals`. It leaves the denominator, because an
item the case cannot pose is not one the conversion failed.

`BLOCKED` is for an item nobody could grade **because the conversion under test broke first** —
the notebook stopped in an earlier cell, or the plan omitted the hand-off this item asks about. It
stays in the denominator and earns nothing.

Those two were the same outcome once, and the conflation ran the wrong way. A stopped notebook was
scored out of only the part of the rubric it survived long enough to be asked about, so the
denominator shrank in proportion to how early the thing broke. Measured on a real hub conversion:
**6/33 (18%) as reported, 6/69 (9%) once the items its own stop had removed were counted back in.**
Repairing only the cell that stopped it, and nothing else, would have *lowered* the printed figure
from 18% to 9%. A rubric that pays a conversion for breaking early is not a rubric.

What `BLOCKED` does not do is diagnose. Fifteen red lines that are all one bug bury the bug, which
is why these are not `FAIL`: they are counted in the score, named in one line, and explained once
by the `FAIL` that caused them.

So a report always carries **two numbers**: the score and what it was taken over. `20/20` over a
rubric of sixteen items is not a pass, and the headline will not render it as one.

### Grading what the hub actually produced

```bash
uv run python evals/run.py adjustment_signoff --project path/to/q2_accrual_adjustment.kedge
```

`--project` reads a hub project directory as the hub leaves it and resolves both halves: the
notebook, and the **latest approved** plan — approved, because an unapproved plan was never
scaffolded, so grading a notebook against it reports a shape the notebook was never asked to have.

It exists because the two-flag form was the step nobody took. The composed path had never been
graded once, and the first time it was, by hand, it came back at 6 of the rubric's 72 points.

### Driving a conversion that names its steps differently

Every widget and every stage cell in a converted notebook is named after a **stage id**, and a
stage id is the model's free choice. The reference plan calls the checkpoint before the update
`approve_adjustment`; one real hub plan called it `select_adjustment_population`. Same step, two
names, and no spelling rule connects them — `harness/align.py` bridges a difference in *prefix*,
which is a different problem.

The consequence was not a partial drive but a total one. **Not one of the eight scripted actions
named a widget the hub's notebook had**, so the run stopped at the first thing waiting for a
human, and the whole deterministic tier was reported as blocked. A conversion that could not be
driven was indistinguishable from one that did not work.

`harness/roles.py` resolves both sides through the plan instead:

- **Driving**, by `Role` — the first hand-in, every checkpoint's decision, every confirmation that
  a statement was run, the extract's parameters by their own names. `case.role_script` says what
  the human does; the roles are read off `build_cells`, so the naming convention stays the
  scaffolder's and is never copied into the harness.
- **Reading results**, by `frame_aliases` — the graders ask for `adjust`, `verification`,
  `signoff`, which are the reference plan's stage ids because that is the vocabulary the rubric is
  written in. Both plans are put through the same role lookup and the difference becomes an alias.
  Over the reference against itself it is the **identity**, which is the assertion that keeps it
  honest: a map that renamed anything there would be scoring the gold conversion through a
  translation layer.

The roles are plan and workbook properties, never guesses about wording. "The stage that computes
the uplift" is found by the **analysis operation ids** it claims — `adjustment_e17_e92` is a range
in the workbook, so that one cannot be moved by renaming anything.

A role the other plan does not fill is **absent, not guessed at**. A plan that types its `UPDATE`
as `output` hands nothing over, so there is no `update_statement` to find, and the grader reports
that rather than being pointed at whatever stage happened to be nearby.

This also closes the denominator hole from the other side. Before it, a name the rubric asked for
and the notebook did not define was a `SKIP` — "possibly just a rename" — and a skip leaves the
denominator, so a conversion's score *rose* as its naming diverged. With the role map in place an
absent name is an absent step, so it is `BLOCKED` and keeps its points. Measured on one hub
scaffold: 5/71, then 9/53 with driving alone and the denominator quietly falling away, then
**9/71** once the results were read by role too — the same denominator as the reference, and the
first two numbers in this repo that are directly comparable.

**`--plan` on its own is refused.** `--notebook` defaults to the committed reference conversion, so
`--plan a-model-plan.yaml` alone grades a model's plan alongside a human's cell bodies: measured on
a real hub plan, **49/66 (74%)**, of which 45 points are the reference notebook's deterministic
tier. That is the same confound `--plan-from` is refused for, reached offline and by accident
rather than by asking for it. Name the notebook that plan produced, or point `--project` at the
directory holding both.

## Measuring a model

Everything above grades committed artifacts, and scores 71/71 with no model anywhere in the loop.
That proves the graders work. It says nothing about whether the model a user has configured can
do the job — which matters, because this workbook is one of the *simplest* processes a user will
bring. Three modes answer that, at three different seams and three very different prices.

**`--model` grades the planning seam.** One call per repeat: propose a plan, grade it against the
structural tier, record tokens, seconds and — when it fails — why. Repeat with `--repeats`; one
sample is noise, and the table prints the spread rather than a mean that hides it.

**`--convert` grades the code-generation seam.** Scaffold an approved plan, ask the model for
every `TODO(kedge)` body, gate each through `kedge.agent.validate`, render, drive, grade. Many
calls, so it costs materially more. It sends the product's own prompt parts — computed by
subtraction from `SYSTEM_PARTS`, in the product's own message roles, carrying `tools.md`'s
validation rules quoted rather than paraphrased — because an eval whose prompt differs from what
ships measures something no user experiences.

**`--convert MODEL --plan-from MODEL` grades the composed path.** The model proposes the plan,
`run.py` approves it and acknowledges its drops the way a reviewer clicking through the card
would, kedge scaffolds *that* plan, and the model fills its holes. Both tiers are earned by the
model: the structural one over the plan it wrote, the deterministic one over the cells it wrote
into the notebook that plan produced. This is the only path a hub user actually takes, and until
it had a number the gap between it and a `--convert` figure kept being rediscovered by hand.

### What the refusals are actually about

One composition is confounded, and it is worth naming precisely rather than approximately: **a
model's plan graded alongside a notebook the model did not write.** That prints a near-perfect
total made mostly of points a human earned, under a model's name — the exact false confidence
this apparatus exists to remove, and almost invisible in a tidy table. `--plan-from` without
`--convert` is that composition exactly, since the notebook graded would be the committed
reference conversion and 45 of the rubric's 71 points are its deterministic tier. It is refused
by name.

`--model` with `--convert` is refused too, for a duller reason: a sweep tabulates several models
over the planning seam and a conversion grades one notebook, and neither is a section of the
other. The refusal used to stand in for the confound as well, which is how it came to block the
*reverse* composition — the model's own plan, filled by the same model — where every point on the
board is the model's and there is nothing to confound.

**Both convert modes say whose plan they used, above the number rather than below it.** Not only
the composed one — a plain `--convert` figure is *not* "the cell bodies alone", which is what that
line used to claim. The structural tier is graded against the plan whoever wrote it, so 26 of a
`--convert` run's 71 points are a human's:

```
adjustment_signoff: graded, N/N hole(s) filled ...
PLAN NOT THE MODEL'S -- read from evals/adjustment_signoff/plan.yaml. The model wrote the cell
bodies and nothing else; 26 of 71 points are structural, so this total is not a whole
conversion's.
q2_accrual_adjustment.xlsx: N/71 (N%)

adjustment_signoff: graded, N/N hole(s) filled ...
COMPOSED PATH -- plan proposed by MODEL, and the cell bodies are the same model's. Every point on
the board is the model's; 26 of 71 points are structural. Not comparable with a plain --convert
figure, whose structural tier is a human's plan.
q2_accrual_adjustment.xlsx: N/71 (N%)
```

(No figure is quoted here, deliberately: nobody has run either against a live model yet, and a
number written into a README before it was measured is the kind of thing people cite.) Two totals
over one rubric, measuring different things. A reader who takes one for the other has been handed
exactly what this file exists to prevent, and that line is the only thing between them.

**Flags a mode would ignore are refused rather than accepted.** `--repeats`, `--notebook` and
`--json` all belong to other modes, and `--convert m --plan-from m --repeats 3` used to announce
"2 model(s) x 3 repeat(s)" before doing one proposal and one pass. A cost estimate a reader would
act on, and never true.

A composed run has one more way to end than a `--convert` run: `ConversionOutcome.NO_PLAN`. Two
ways in, and both mean the same thing — no plan the pipeline could use. Either none arrived, or
one arrived that `build_cells` refuses, which used to be a traceback out of `main()` with no
report, no outcome and no statement of what the proposal had already cost. (The cost is printed
from a `finally` now, so every path out of a mode that spends money says what it spent.) It is a
member of its own rather than an `INCOMPLETE` — whose docstring reads *"the gaps are the model's"*
— because a run that never scaffolded a cell has no gaps, and filing an unreachable endpoint as a
model that writes bad code is the misattribution the next
section is about.

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

The composed path inherits all of it one level up. A `--plan-from` run that gets no plan reports
`NO_PLAN`, and the sentence beside it quotes `Failure.about_the_model` rather than re-deciding it,
in **three** buckets rather than two: "the model's own output never validated as a plan"; "a fact
about the integration, the account or the endpoint"; and `triage_refused`, which is neither —
kedge read the workbook, decided it was not convertible, and never asked. Filing that under the
endpoint sends a reader to a proxy that answered nothing, and filing it under the model blames one
that never saw the question.

A plan that arrives but that kedge will not scaffold — a blocker `acknowledge_all_drops` does not
clear, or anything `build_cells` refuses — is `NO_PLAN` as well, with the reason quoted, because a
plan the product would refuse is a result about whoever wrote it and not a run to fudge past.

**A leg the sweep could not grade in full does not print full marks.** A plan the scaffolder
refuses used to make items skip that every other leg was graded on, and the leg then rendered
`18/18 PASS` directly beneath a header saying legs are scored out of a larger number — with the
skip that caused it named nowhere. Three changes: the score cell renders against the *sweep's*
denominator and says how many points went unmeasured; `SweepReport.ungradeable` is taken from the
best-measured leg rather than the first one, so the preamble cannot announce one plan's skip as
everybody's; and a short leg lists its skips under "Why". A skip is a measurement nobody took, and
the reader has to be told which one.

That particular route is closed at the source now: a plan the scaffolder refuses is the *plan's*
defect, so its items are `BLOCKED` and keep their weight. The leg is scored out of the whole tier
like everybody else and simply earns less of it, and the reason is listed under "Why" beside the
failures. The short-leg machinery above still stands for the case that genuinely shortens a
denominator — a real `SKIP` one leg hits and the others do not.

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
**71/71** — 68/71, 65/68 and 60/63 at three earlier states, and 47/63 before the scaffolder was
fixed, so 21 of those points came from fixing kedge rather than from touching a grader. That figure
is the ceiling for the composed path, and it is measured with a human's plan and a human's cell
bodies: a model's number on `--convert MODEL --plan-from MODEL` is the first honest reading of what
a user gets.

The last item to go green was `progressive_disclosure`, which stood red for a while as a difference
rather than a defect: it wants `extract_query` on screen at once, and a scaffolded notebook blocked
it with *"Step 1 of 8 ... fill in the inputs above"* because it had no period end yet. The argument
for leaving it was that the reference conversion only passes by *defaulting* its date picker, and a
runbook that opens with a query already scoped to a date nobody chose is how somebody extracts the
wrong period.

Defaulting was never the alternative on offer. What a hand-off withholds until its parameters are
supplied is the **statement**; what it was also withholding was the **step** — the heading, the
instruction, and the fact that a query exists at all — while the box asking for that query's output
sat on screen above it, ungated, because a selector builds `mo.ui` elements and reads nothing. A
real user met that as "where is the sql to run to get the starting data?", then "i can't see it".
Both halves are fixed in `scaffold.py`: the step renders either way and names the input it is still
waiting for, and a stage's hand-in selector reads the hand-off's token, so it can never precede it.
A *mutating* hand-off's token is still its confirmation, so the re-extract box stays behind the
UPDATE.

That is the argument for this mode in one paragraph. The reference conversion is a worked example of
the destination; it was never evidence that kedge can get there.

## What this eval does not cover

- **Nothing about the chat loop.** This is workbook in, notebook out — including on the composed
  path, which calls `propose_plan` and the cell-filling loop directly rather than through the
  agent's tool surface. Whether the agent *converses* well about the conversion, and whether it
  ever finds the holes it is meant to fill, is a different eval. That line used to say the eval
  measured two seams in isolation; it now measures their composition too, and what is left
  uncovered is the loop that would drive it in the product.
- **Widget names are aliased, but only where the alias is provable.** A candidate must be assigned
  from `mo.ui.*` and match the stage cell's own name exactly; anything looser bound strings and
  reported the run as driven while the scripted action went nowhere. What still cannot be played
  is reported as `NOT DRIVEN`, which is its own category and never a skip.
- **No knowledge-pack item.** `consults_the_knowledge_pack` skips: `context/databases/example.yaml`
  describes a different schema. Adding a pack for `fin.accruals` would make it gradeable.
- **One workbook, one process shape.** A monthly adjustment with a sign-off. Nothing here says
  anything about reconciliations, allocations, or anything with a Power Query in it.
