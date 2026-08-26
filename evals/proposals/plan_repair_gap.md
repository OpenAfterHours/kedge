# Proposal: kedge diagnoses the plan correctly, and tells only the person who cannot act on it

Status: **proposed**. Every number below was measured on 26 August 2026 against artifacts still on
disk, with no model in the loop — `evals/run.py` grades a notebook and a plan handed to it, so all
of this is reproducible from the tree.

The previous proposal (`hub_conversion_gap.md`) asked why the hub scaffolded a notebook and left six
holes in it. That is fixed, and this document should start by saying so: the run measured here
filled **every** hole, and the cell bodies it wrote use `kedge.sql.render_all`, `kedge.sql.script`,
`kedge.reconcile.verify`, `kedge.runs.record_handin` and `kedge.ingest.read_data` correctly and
unprompted. The conversion driver works. The cell-writing seam is not the problem any more.

The conversion is still worth 6 of the rubric's 72 points, and the reason is entirely upstream of
the notebook. **The plan is wrong in three specific ways; `review_warnings` detects all three, in
imperative sentences that name the field and the range; and the only thing that consumes those
sentences is a card shown to a human who would have to hand-edit YAML to act on them.** The model
that wrote the plan never sees them. The repair loop that would feed them back already exists, is
already wired to a prompt, and fires only on pydantic schema errors.

---

## 1. What was measured

Three gradings, all deterministic, all repeatable:

```bash
uv run python evals/run.py adjustment_signoff \
    --notebook evals/adjustment_signoff/notebook.py --plan evals/adjustment_signoff/plan.yaml
uv run python evals/run.py adjustment_signoff --project <project>.kedge
```

| Conversion | Deterministic | Structural | Total | Warnings on its plan |
|---|---|---|---|---|
| Reference (`notebook.py` + `plan.yaml`) | 45/45 | 26/26 | **71/71** | 2 |
| Hub, 26 Aug, holes filled by the chat agent | 1/43 | 5/26 | **6/69** | 5 |
| Hub, 26 Aug, fresh scaffold under current code | 3/45 | 2/26 | **5/71** | 6 |

The hub conversion **earned 6**, lost 27 on items that were graded, and lost the remaining 36 on
**14 items that could not be graded at all**, because the notebook stops on its second cell.

**These are not the figures the eval printed when this was first measured, and the difference is
itself a finding.** It reported 6/33 (18%) and 5/35 (14%). An item nobody could grade was skipped,
and a skipped item left the denominator -- so a notebook that stopped was scored out of only the
part of the rubric it survived long enough to be asked about, and the denominator shrank in
proportion to how early it broke. Repairing the one cell that stopped this notebook, and nothing
else, would have *lowered* its printed score from 18% to 9%.

`harness.model.Outcome.BLOCKED` is the fix: an item the *case* cannot pose still leaves the
denominator, an item the *conversion* prevented does not. The rows above are what the eval reports
now, and the third one is scored out of the same 71 as the reference -- which is the point. Two
numbers that were never comparable now are.

Two independent hub runs six days apart, on the same workbook, produced plans with the same three
defects. This is not one bad sample.

| | Reference `plan.yaml` | Hub, 23 Aug | Hub, 26 Aug |
|---|---|---|---|
| `kind: handoff` stages | **2** | 0 | 0 |
| stages declaring `origin: handin` | 2 | 2 (one on a `checkpoint`, where the scaffolder ignores it) | **0** |
| `briefing` | present, 6 citations | `null` | `null` |
| notes the analyser recovered and offered | 8 | 8 | 8 |
| structural tier | 26/26 | 5/19 | 5/19 |

---

## 2. What actually happened, with timestamps

The 26 August session (`~/.kedge/logs/outbound-de1a7a10e8ba46e5.jsonl`, `sessions.sqlite`):

| Time (UTC) | Event |
|---|---|
| 18:18:46 | twelve reads — `inspect_workbook` x6, `sample_data` x3, `read_range` x3 |
| 18:19:15 | `propose_plan` returns an eight-stage plan |
| 18:19:15 | `review_warnings` raises **six warnings**, three of them naming a defect and its fix |
| 18:20:09, 18:20:38 | the user acknowledges both dropped ranges |
| 18:20:43 | the user approves. **88 seconds after the plan was proposed** |
| 18:21:28 | *"where is the sql to run to get the starting data?"* |
| 18:22:19 | *"can this be a markdown cell so the user can pick this up"* |
| 18:23:04 | *"i can't see it"* |

Those last three lines are not a usability aside. They are the head hand-in defect being reported by
a user in real time, and section 4.2 is what they were looking at.

The three structural warnings, verbatim from `review_warnings(plan, analysis)` replayed over the
saved plan:

```
- Type the step that writes as `kind: handoff`, naming the statement it hands over:
  Adjustment!G17:G92, a formula column building 'UPDATE fin.accruals SET accrual_gbp = 0.00 ,
  adjustment_note...' - nothing in the plan claims it. Without a `Handoff` there is no statement
  for the notebook to show, no confirmation that it was run, and no token for the step after it
  to be gated on
- Fill `briefing` from the workbook's own words, citing them: the plan carries no briefing while
  the analyser recovered 8 note(s) from its sheets and comments - Sign-off!A1, Sign-off!A3:A4
  (Purpose), Sign-off!A6:A7 (Background) and 5 more. Left empty, the notebook tells whoever opens
  it that this workbook carried no description of what the process is for
- Name the stage whose result 'produce_signoff' reads in its `sources` and `depends_on`: it
  currently names 'verify_post_adjustment', which is a checkpoint, and a checkpoint records a
  decision rather than producing a frame - so the scaffolder has nothing to build this stage on
  and falls back to the notebook's own hand-in
```

Every one is correct. Every one names the field to change and the range to name. Every one is
already written in the imperative. **They are ready to be a repair prompt with no rewriting at
all**, and instead they were rendered as prose beside an Approve button.

---

## 3. The root cause is one missing edge

`src/kedge/plan/__init__.py`:

```python
240:    plan = propose_plan(                     # <- the retry/repair loop lives inside here
...
250:    warnings = review_warnings(plan, facts, triage_result=assessment)
```

Ten lines apart. `propose_plan` runs up to `max_attempts`, and on a failure feeds `propose_repair.md`
an `{{errors}}` block and asks for a correction. That prompt opens:

> *Your previous response could not be loaded as a process plan.*

Which is the whole story. The repair loop answers **schema invalidity**. The warnings describe
**semantic invalidity** — a plan that loads perfectly and scaffolds into a notebook that cannot run —
and they are computed after the function that could repair them has already returned.

`approval_blockers` does not close the gap either: it is structural-only by design, and blocks on a
rejected plan and unacknowledged or rejected drops. Nothing else.

**And the prompt is not the missing piece.** `propose_system.md` already instructs all three, in its
numbered list, near the top:

- line 35, item 1: *"Fill in `briefing` from what the workbook says about itself, and cite every line of it."*
- line 74, item 7: *"Use `kind: handoff` for a statement the user has to run somewhere kedge cannot reach."*
- line 95, item 8: *"A process with more than one input needs more than one hand-in."*

Two runs, three instructions at the top of the prompt, ignored all three times, twice. A fourth
sentence of prompt will not fix this. A deterministic check that already fires, wired to a loop that
already exists, will.

---

## 4. Three defects, and what each one costs

### 4.1 No hand-off, so the step that changes the data is not a step

`render_update_statement` is `kind: output`, and its own intent says *"render the approved
statutory-ledger update ... and require confirmation that it was run outside the notebook"*. That is
a hand-off described in prose and typed as something else. With no `Handoff` there is no
`statement`, so `Handoff.needs_confirmation` never engages, so there is no confirmation cell, so no
token, so nothing downstream can be gated on the update having been carried out.

Costs `hands_over_rather_than_pretends` (3), `generates_the_update_from_the_frame` (3) and
`has_a_checkpoint_before_the_update` (3), and makes `the_re_extract_waits_for_the_update` (3) and
`mutates_agrees_with_the_statement` (2) ungradeable. **Fourteen points on one enum value.**

### 4.2 No per-stage hand-in, so the notebook asks for a file before it says which file

No stage declares `origin: handin`, so `_upstream_name` falls through to `handin_frame` for three of
them — `load_pre_adjustment`, `calculate_uplift` and `produce_signoff` — and the notebook's fixed
head hand-in is switched on. In the notebook the current scaffolder produced today:

```
lines 170-424   handin_source, handin, handin_contract, handin_drift, handin_check, handin_frame
line  233         mo.md("**Waiting for a hand-in.** Drop a file above, select one, or paste a grid.")
line  426       pre_adjustment_query        <- the markdown cell holding the SQL that produces that file
```

Six cells and some 255 lines of hand-in machinery for an input no stage names, blocking the page
**above the cell that tells the user what to go and fetch**. That is exactly what the user hit at
18:21 and 18:23: they asked where the SQL was, were told it would be added as a markdown cell, and
then reported *"i can't see it"* — because it was below a hard stop.

The blocking message also breaks the project's own rule. CLAUDE.md: *"Every `mo.stop` message
therefore names its step (`Step 3 of 8`) and what to do — at that moment it is the entire user
interface."* The head hand-in is the one stop in the scaffold that names no step, because it is not
a step.

Costs `ran_to_completion` (2), `progressive_disclosure` (3), `a_blocked_step_says_which_step_it_is`
(2) and `a_paste_out_of_excel_works` (3), and blocks **11 further deterministic items worth 29
points** — the rounding, the null-as-zero break, the row-level verification, the SQL escaping.
Every discriminating thing this eval was built to test is downstream of a cell that stops.

Meanwhile the second hand-in, the re-extract the whole process exists to check against, has nowhere
to arrive: `load_post_adjustment` resolves to `render_update_statement`.

### 4.3 No briefing, so the notebook opens by contradicting the workbook

`briefing: null` against an analysis carrying eight notes with the sheet and cells each came from —
Purpose, Background, Scope, Known issues, all in the Sign-off tab. The notebook therefore opens by
telling its reader the workbook carried no description of what the process is for, which is false.

Costs `the_notebook_says_why_this_process_exists` (3) and `the_briefing_survives_the_workbook` (2).
The asymmetry named in the previous proposal is still exactly the bug: an invented briefing is
refused by the validator, an absent one is not.

### 4.4 And then the agent papers over it

This is the part worth dwelling on, because it is evidence about where the remaining effort should
*not* go. Handed that scaffold, the chat agent filled every hole and hand-rolled the machinery the
plan had failed to ask for — three `kedge.runs.record_handin` sites, three `previous_handin`, six
`ingest.receive` — duplicating, in cell bodies, what the scaffolder emits correctly and testably when
a stage declares `origin: handin`. It could not remove the head hand-in above it, so the page still
stops on cell two.

The model did good work inside every hole it was given. It cannot repair the shape of the notebook
it was given, and it should not be asked to.

---

## 5. What to build

Ordered by rubric points per line of code.

### A. Feed the warnings back to the proposer, before the card is drawn

The single change this document is about.

Split `review_warnings` into two classes and act on one of them:

- **Repairable** — the scaffolder will silently mis-handle this plan: no hand-off against a recorded
  statement that writes, `briefing is None` beside non-empty `analysis.notes`, a hand-in declared on
  a stage kind that ignores it, a stage whose upstream falls through to `handin_frame`, a stage
  reading a checkpoint as though it produced a frame.
- **Advisory** — judgement for the human, and no business being repaired by a model: open questions
  outstanding (which is a *good* sign), an empty `open_questions` on a complex workbook, operations
  claimed by no stage.

Then run **one** repair attempt on a non-empty repairable set, through the loop and the prompt that
already exist. `propose_repair.md` needs one more opening line to distinguish "this did not load"
from "this loaded and will scaffold badly"; the warning strings need no rewriting whatever.

Cost: one extra model call on a plan that needs it, none on a plan that does not. Expected return:
the hand-off, both hand-ins and the briefing, which is 14 of the 21 structural points now being
lost, and — because 4.2 is what stops the page — most of the 36 points currently blocked.

Do it inside `propose_plan` rather than at the `plan/__init__.py` call site, so `kedge plan propose`,
the hub and the eval sweep all get it without three copies of the decision.

### B. Stop the head hand-in being a silent default

Three separate things, all small:

1. **Make the fall-through explicit.** `_upstream_name` returning `handin_frame` because nothing else
   matched emits six cells and 255 lines. Where no stage declares `origin: handin`, either refuse to
   emit the head machinery or say in the scaffold report that it was emitted and why. The open job
   now counts unwritten cells, which was the previous proposal's fix; it still says nothing about a
   hand-in nobody asked for now gating the page.
2. **Order it after the cell that explains it.** Even where the head hand-in is right, a stage's
   query or markdown cell belongs above the box asking for that query's output. A runbook that asks
   for a file before naming it has the two halves of one step in the wrong order.
3. **Give its `mo.stop` a step number**, or move it inside the stage that reads it. It is the only
   stop in the scaffold that violates the rule the rest of the file follows.

### C. Say what approving costs, on the card

The approval card renders warnings faithfully, as a flat list of prose, next to an Approve button.
Six of them, three structural, 88 seconds. The card should separate the two classes A introduces and
lead the repairable ones with their consequence for the notebook — *"this plan hands nothing over, so
the notebook will have no confirmation step and the re-extract will not wait for the update"* — and
offer **"ask the model to fix these"** as a button, which is A exposed rather than new machinery.
Still not blocking; the amber-signal argument holds.

### D. Let the pane start the driver

`convert_notebook` is reachable from `kedge convert` alone — no agent tool, no button. The pane's
`approved` suggestions ask the *chat agent* to write the stages conversationally, which is the slower
path with the weaker gate, and is what the 26 August run took. One tool or one button, and the
approved-state suggestion becomes the driver it was written for.

### E. Make the eval able to confirm any of this — **built**

Everything above was measured by hand, with two long paths, on an eval that had three ways to
score the same conversion and printed the most flattering one by default. That is its own defect
and it is fixed:

1. **`Outcome.BLOCKED`.** A skip leaves the denominator; an item the conversion prevented no longer
   does. 6/33 becomes 6/69, and the fresh scaffold is scored out of the same 71 as the reference.
   Section 1 has the arithmetic and `evals/README.md` has the reasoning.
2. **`--project <dir>.kedge`** resolves the notebook and the latest *approved* plan out of a hub
   project. The two-flag form was the step nobody took, which is most of why the composed path had
   never been graded once.
3. **`--plan` without `--notebook` is refused.** It silently graded a model's plan alongside the
   committed reference conversion — 49/66, 74%, of which 45 points are a human's cell bodies. The
   same confound `--plan-from` is already refused for, reached by accident instead of on purpose.
4. **The reason survives.** A sweep leg's blocked items are listed under "Why", because the points
   now come off the score and the sentence explaining them must not disappear with the old
   short-leg line.

What is still not built is the regression that would have caught the *plan* defect on its own:
replay `plan-v001.yaml` through `review_warnings` and assert the repairable set is non-empty.
`tests/unit/observed_conversion.py` already carries the plan.

### F. Put a number on the composed path and keep it

`--convert MODEL --plan-from MODEL` was built by the previous proposal and, as far as the tree
records, has never been run. The figure in section 1 is that measurement taken by hand, once, on one
workbook. Run it, and print it in `evals/README.md` beside the 71/71 so the gap is a standing number
rather than something rediscovered by hand every few weeks.

Add the cheap regression alongside it: both archived plans are in the tree, both are real model
output with the same three defects, and `tests/unit/observed_conversion.py` already exists for
exactly this. Assert the repairable set is non-empty on each, and that a repaired plan carries at
least one `handoff`, a second hand-in and a briefing.

---

## 6. What I would not do

- **Do not make the warnings blocking.** `approval_blockers` is for plans that cannot be scaffolded.
  A blocking gate on judgement is a gate that gets clicked through — and a plan can be defective in a
  way this list does not name, so a green card must not read as a guarantee.
- **Do not add a fourth instruction to `propose_system.md`.** Items 1, 7 and 8 already say all three
  things, near the top, in bold. The evidence that prose is not the lever is two runs and six days.
- **Do not touch the cell-writing seam.** It filled every hole and used the right APIs. Effort spent
  there is effort spent below the defect.
- **Do not delete the head hand-in.** A single-input workbook is the common case and the machinery is
  right for it. What is wrong is that it arrives by fall-through, unannounced, above the cell that
  explains it.
- **Do not let the repair loop approve anything.** Everything above still lands on the user's side of
  the gate; A changes what the card shows, not who clicks it.

---

## 7. How we would know it worked

1. **The composed path**, `--convert M --plan-from M` on `adjustment_signoff`, against 71. This
   document's 6 is the pre-change reading.
2. **Structural tier on a repaired plan**: 5/26 today. A repair that lands the hand-off, the second
   hand-in and the briefing should put it above 20/26.
3. **Points blocked**: 36 today, 0 on the reference. This is the number that says the notebook
   runs, and it is the one to watch first — while it is non-zero the deterministic tier is not
   really being measured at all.

---

## 8. What this measurement does not cover

Said plainly, because the previous document's most useful pages were its corrections.

- **One workbook, one model, two runs.** `adjustment_signoff` is the only eval in the tree, and both
  plans came from `gpt-5.6-terra`. That the same three defects appear twice makes a fluke unlikely;
  it does not make it a property of every model.
- **The graded notebook was scaffolded on 23 August under older code**, then filled on 26 August. So
  the 6/33 mixes an old scaffold with a current agent. The third row of the table is the control for
  that: a notebook scaffolded today, by current code, from today's plan, stops in the same `handin`
  cell with the same unnumbered message, above the same `pre_adjustment_query`. The defect is live.
- **The repair loop's yield is an expectation, not a measurement.** It is a strong one — the model is
  being handed a specific field, a specific range and a specific consequence, which is a much easier
  request than the original — but until A is built and F is run, 14 points is arithmetic on the
  rubric rather than an observation.
- **`consults_the_knowledge_pack` was skipped in all three runs.** No knowledge pack describes
  `fin.accruals`. One point, and it is unmeasured on the reference too.
