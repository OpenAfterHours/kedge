# Proposal: the hub does not finish a conversion, and nothing in the tree says so

Status: proposal. Nothing here is built.

`evals/README.md` ends its scope section with one line that turns out to have been the whole
answer: **"Nothing about the chat loop. This is workbook in, notebook out."** The eval measures
two seams -- planning, and filling a scaffold's holes -- and it measures each one in isolation,
each starting from a hand-authored artifact. The hub composes them, starting from nothing, and
that composition is measured nowhere.

This is not a hypothesis. The run in question left its artifacts on disk and they are still
there, so every claim below is a fact about one real conversion rather than a guess about a
class of them.

| Artifact | Path |
|---|---|
| The plan the model proposed and the user approved | `q2_accrual_adjustment.kedge/plans/plan-v001.yaml` |
| The notebook the session produced | `q2_accrual_adjustment.kedge/q2_accrual_adjustment.py` |
| Every tool call the agent made | `~/.kedge/logs/outbound-84550c0d7b3e43ee.jsonl` |
| The analysis all of it was written against | `q2_accrual_adjustment.kedge/analysis.json` |

(The project directory is gitignored -- `*.kedge/` -- so it is a live artifact, not a fixture.)

---

## 1. What actually happened

Four turns, twenty-five tool calls, just under eight minutes.

| Turn | Time (UTC) | Calls | Outcome |
|---|---|---|---|
| `ab7f794c` | 08:33:45 - 08:34:00 | `list_cells`, `inspect_workbook` x6, `sample_data` x4, `read_range` x3, `propose_plan` | a plan |
| `18975f20` | 08:36:27 - 08:36:33 | `get_plan` (**failed**), `propose_plan` | the plan that was approved, 08:36:57 |
| `1e3df754` | 08:39:52 - 08:40:01 | `list_cells` (**32,768 B, truncated**), `list_cells`, `edit_cell` | **one** cell written |
| `1b6637a7` | 08:41:19 - 08:41:30 | `list_cells` x3, `get_plan`, `edit_cell` | one 217-byte edit |

The reading discipline in turn 1 is exactly what `role.md` asks for, and the one stage cell the
agent did write -- `document_extract` -- follows the "show the query behind a paste" rule to the
letter, raw literal and all. **When the agent writes a cell, the prompt works.** It wrote one.

Scaffolding the approved plan and diffing every cell body against what is on disk gives the
number that matters:

```
23 cells scaffolded.  22 byte-identical to the scaffold.  6 TODO(kedge) markers remaining.
```

`load_pre_adjustment`, `select_adjustment_scope`, `apply_uplift`, `render_update_script`,
`reconciliation_values` and `prepare_sign_off` are all still `= <upstream>  # passthrough until
translated`. The arithmetic of the process -- the uplift, the scope, the rounding, the
verification -- was never written at all.

And a scaffolded notebook **runs**. The passthroughs are passthroughs on purpose, so the
hand-in machinery can be exercised from the moment it is scaffolded. A user looking at the page
sees a runbook that opens, renders and moves between steps. Nothing on screen counts the holes.

---

## 2. The plan is where the conversion was lost

Before a single cell body: the approved plan and the reference plan describe different
processes.

| | Reference `plan.yaml` | The run's `plan-v001.yaml` |
|---|---|---|
| stages | 8 | 7 |
| `handoff` stages | **2** -- `extract_query`, `update_statement` | **0** |
| `checkpoint` | 1, **before** the update | 1, **after** it |
| `briefing` | present, 6 citations | **absent** |
| `open_questions` | 2 | 0 |
| acknowledged `dropped` ranges | 1 | 0 |

Graded against `expected.yaml`'s structural tier, that plan loses three of the eight items --
`hands_over_rather_than_pretends`, `generates_the_update_from_the_frame` and
`has_a_checkpoint_before_the_update` -- eight of the nineteen structural points, before the
deterministic tier is reached at all.

**The model never used `StageKind.HANDOFF`.** `render_update_script` is `kind: output`, and its
own intent reads *"render one safe SQL UPDATE per scoped trade for operator review and require
confirmation that it was run externally"*. That is a hand-off described in prose and typed as
something else. With no `Handoff` object there is no `statement`, no `template`, no
`built_from`, no `mutates` and no `instruction` -- so no confirmation cell, so no token, so
nothing downstream can be gated on the update having been carried out. The mechanism CLAUDE.md
describes at length (*"`mutates` is a claim; the statement is the fact"*) never engages, because
there is no statement to inspect.

### One wrong `kind`, three consequences, no warning

`verify_post_adjustment` was typed `kind: checkpoint` while carrying `origin: handin` -- the
re-extract. `_handin_cells` never fires for a checkpoint stage, so:

1. **The second hand-in vanished.** No `post_adjustment_input`, no `_handin`, no `_frame`. The
   re-extract the whole process exists to verify against has nowhere to arrive.
2. **`prepare_sign_off` lost its upstream.** Its only dependency is that checkpoint, and
   `_upstream_name` falls through to `return "handin_frame"` (`scaffold.py:2019`) -- so the
   sign-off builds on the notebook's fixed head hand-in rather than on the verified result.
3. **That fall-through switched the head hand-in back on.** `head_handin_is_read` returns True
   the moment any stage resolves to `handin_frame`, so six cells and 11,175 bytes of hand-in
   machinery were emitted for an input no step of the process names -- 35% of the notebook's
   code -- and its `mo.stop` blocks the page until the user supplies a file they have no use
   for. The reference plan, where every load stage declares its own hand-in, emits none of it.

Every one of those is the documented, correct behaviour of the code it passes through. The
defect is that a single mistyped enum produces them silently.

### The review gate said almost nothing

Running `review_warnings(plan, analysis)` over the approved plan returns **one** warning, and it
is about open questions. Zero hand-offs on a process with a recorded ODBC connection *and* a
column of generated `UPDATE`s; a production statement with no approval upstream of it; a
sign-off wired to an orphan input; no briefing on a workbook carrying eight cited notes. The
approval card renders warnings faithfully (`routes.py:718`, `app.js:943`). There was nothing to
render.

### The briefing is worse than missing

`analysis.json` carries eight notes, each with the sheet and cells it came from:

```
Sign-off!A3:A4    Purpose        "To record the Q2 2026 uplift applied to statutory accruals ..."
Sign-off!A6:A7    Background     "The June reforecast moved the accrual basis ... a flat 4.5% uplift ..."
Sign-off!A12:A13  Scope          "Entities E-04, E-07, E-09 and E-12. Statutory ledger only ..."
Sign-off!A15:A16  Known issues   "One trade carries no accrual value ... shown at nil ..."
```

`propose_system.md` makes filling `briefing` **instruction number one**. The plan came back with
`briefing: null`, and the notebook now tells its reader:

> *"The workbook this was converted from carried no description of what the process is for, so
> none is offered here."*

That is false, and it is confidently false in the one register the project exists to protect. The
`Briefing` validator refuses prose without citations -- rightly -- but nothing anywhere notices
prose that never arrived. The asymmetry is the bug: **an invented briefing is caught, an absent
one is not.**

---

## 3. Seven gaps, each verifiable in the tree today

| # | Gap | Evidence |
|---|---|---|
| 1 | **There is no conversion driver.** The loop that enumerates `TODO(kedge)` holes and asks for each one exists only in `evals/harness/cellgen.py`. | `grep -rn "TODO" src/kedge/agent/ src/kedge/server/` returns nothing. The string appears in `scaffold.py` and `plan/model.py` only -- the code that *writes* the markers. Nothing that reads them ships. |
| 2 | **The agent is never told the markers mean anything.** | `TODO` appears in no file under `src/kedge/agent/prompts/`. `role.md` and `tools.md` describe a copilot that translates regions on request; neither describes a scaffold with holes in it, nor says that filling them is the job. |
| 3 | **`list_cells` cannot show the notebook it is asked to complete.** | 23 cells, 31,664 bytes of raw code, wrapped in JSON with id/name/defs/refs -- over the 32,768 cap. The log records `byte_count: 32768, truncated: true` on the first call of the conversion turn. `tools.md` then instructs: *"you have seen a slice ... do not state a total"*. Every hole is in the truncated tail. |
| 4 | **More than half that budget is machinery the model never edits.** | The fixed head is 10 cells and 17,447 bytes of the 31,664. `list_cells` offers `cell` and `with_code` and nothing else -- no filter by role, no "unwritten only", no pagination. |
| 5 | **`review_warnings` does not check the things the scaffolder consumes.** | No warning for zero hand-offs, for a mutating step with no checkpoint upstream, for `briefing is None` beside a non-empty `analysis.notes`, for a `handin` source on a stage kind that ignores it, or for a stage whose upstream silently resolves to `handin_frame`. |
| 6 | **The structural tier grades presence, not shape.** | Eight items, nineteen of sixty-three points, and it never grades the briefing, the checkpoint's *position* relative to the mutating step, or the `mutates` flag. Sharper still: `takes_two_handins` asks only that "at least one stage declares a hand-in with a `ref`", and **this plan passes it** -- both its load stages carry one -- while the scaffolder emits hand-in cells for exactly one of them, because the other sits on a `checkpoint`. An item can be green on a plan whose second hand-in has nowhere to arrive. |
| 7 | **No eval covers the composed path.** | `--model` grades plan-from-model against the structural tier. `--convert` grades cells-from-model against a **hand-authored** `plan.yaml`. `run.py` refuses both at once. Model plan -> scaffold -> model cells -- the only path a hub user takes -- is measured nowhere. |

Gap 7 is why none of the other six were visible. The eval's `--convert` mode starts from the
gold plan, so it has never once seen a scaffold with the wrong shape, an absent briefing, or a
head hand-in emitted by accident.

---

## 4. What to build

Six changes. The first three are most of the distance and none of them is large.

### A. A conversion driver, in the product

`harness/cellgen.convert()` is a missing product feature that was written as test scaffolding.
It already does the right thing: enumerate holes in dependency order, send one focused prompt
per hole carrying the analysis, the plan and the registry of names filled *above* it, gate each
answer through `kedge.agent.validate`, retry on the gate's own violations, and report per-hole
outcomes that distinguish "never asked" from "never passed".

Move that shape into `src/kedge/notebook/fill.py` and expose it twice:

- **`kedge convert <workbook>`** -- headless, fills every hole, reports what it could not.
  There is no `convert` verb in the CLI today (`open`, `hub`, `inspect`, `plan *`, `reconcile`,
  `watch`, `contract *`, `doctor`).
- **A tool for the chat agent.** The minimum viable version is one tool -- `unwritten_cells`,
  returning the ordered holes with their comment headers and nothing else -- plus a paragraph in
  `role.md`. That converts an open-ended request into a work list with a termination condition,
  which is the actual difference between the two seams. A turn is allowed fifty completions
  (`[agent] max_steps`, default 50); the two conversion turns here spent three tool calls and
  five. Nothing capped them. Nothing told them they were not finished either.

The per-hole prompt is already written and already provably faithful:
`cellprompt.CELL_PROMPT_PARTS` is `SYSTEM_PARTS` minus `tools.md`, subtracted by name. In the
product the subtraction is unnecessary -- the tools are real -- so the product version is
simpler than the eval's, not harder.

### B. Make the notebook legible to the agent

- **Say which cells are unwritten in `NotebookState.render`.** That block is pinned into every
  turn and already lists all 23 cells with defs and refs. One word per cell -- `[unwritten]` --
  puts the work list in front of the model on every step for a handful of tokens, in the part of
  the prompt a cache keeps. It needs the scaffolder's marker to reach `CellFacts`, which is a
  substring test on a body the driver already has.
- **Give `list_cells` a filter.** `role=` or `unwritten=true`. Do **not** raise the 32KB cap --
  the cap is right and the reasoning in `tools.md` is right. What is wrong is spending it on the
  fixed head.

### C. Sharpen `review_warnings`

Warnings never block, which makes this the cheapest safe change in the list -- and it lands on
the card the user reads before clicking Approve. Six, each a few lines:

1. No `handoff` stage while the analysis carries a connection `command`, a Power Query
   `m_source`, or a region the analyser typed as generated SQL. Name the connection.
2. A hand-off whose statement writes (`kedge.sql.changes_data`), or an `output` stage whose
   intent mentions running something externally, with no `checkpoint` upstream in `depends_on`.
3. `briefing is None` -- or `briefing.is_empty` -- while `analysis.notes` is non-empty. Name the
   sheets and cells that were extracted and dropped.
4. A `handin` source on a stage whose kind the scaffolder ignores it for. Say which cells will
   not be emitted.
5. A non-checkpoint stage whose `_upstream_name` resolves to `handin_frame` while its
   `depends_on` names a stage. Say that the head hand-in will be emitted and why.

Numbers 3, 4 and 5 would each, on their own, have caught this run.

### D. Grade the plan on what the scaffolder consumes

Add structural items for the briefing, for the checkpoint's position relative to the mutating
step, and for `mutates`. Every one is a property of the plan that survives rephrasing, and every
one is a defect actually observed rather than an invented case.

### E. Measure the composed path

`run.py` refuses `--model` with `--convert`, and the reasoning is sound for the composition it
was written about: a model plan plus the *committed* notebook prints points a human earned under
a model's name. The reverse composition is the product's own path and carries no such confound.
Narrow the refusal to "gold notebook plus model plan" and add `--convert MODEL --plan-from
MODEL`, which scaffolds the model's own plan and grades the whole thing. Expect the number to be
materially below the current `--convert` figure. That gap is the thing this proposal is about,
and until it has a number it will keep being rediscovered by hand.

### F. Give the hub a conversion affordance

- The open job reports "scaffolded 23 cells". It should report **"23 cells scaffolded, 6 still
  to write"** -- the number that says the conversion is not finished.
- The four `SUGGESTIONS` in `app.js` are generic and one of them names `Calc!H2:H50000`, a range
  from a different fixture entirely. Replace them with the real workflow: propose a plan; write
  the stages that are still passthroughs; reconcile against the workbook.

---

## 5. What I would not do

- **Do not make a missing briefing or a missing hand-off block approval.**
  `approval_blockers` is for plans that cannot be scaffolded. These are judgement, and a blocking
  gate on judgement is a gate that gets clicked through -- the amber-signal argument CLAUDE.md
  already makes about `NOT RECONCILED`.
- **Do not have the driver approve its own plan.** Everything above sits on the user side of
  the gate. `_step_plan`'s refusal to propose silently during a page load is right.
- **Do not raise the payload cap.** Make the listing smaller.
- **Do not paraphrase the cell prompt into a second copy.** `cellprompt.py`'s whole argument is
  that a second copy of a rule rots; the product version should assemble from `SYSTEM_PARTS` the
  same way.
- **Do not reach for `excel_pattern`.** It looks like a lever -- the run's plan set `unknown` on
  all seven stages, which renders `# Excel pattern: unknown -> ` with an empty hint. But the
  reference plan sets it to `None` on all eight and scores 63/63, so the hint mechanism carries
  no weight in either direction here. Worth tidying; not worth counting.

---

## 6. How we would know it worked

Three numbers, in order of how much they prove.

1. **`--convert MODEL --plan-from MODEL` on `adjustment_signoff`**, against the 60/63 the
   reference bodies score when replayed through the same pipeline on the gold plan. The gap
   between the two is this proposal's subject, and closing it is the goal. Note that the second
   number is itself a ceiling measured with a human's plan and a human's cell bodies -- so a
   model's figure on the composed path is the first honest reading of what a user gets.
2. **Holes filled per conversion**, reported by the driver. The run above scored 0/6 and said
   nothing about it.
3. **Warnings raised on a plan later found defective.** A cheap regression: replay
   `plan-v001.yaml` through `review_warnings` in a unit test and assert the new warnings fire.
   The file is a real model output with four independent defects in it, which makes it a better
   fixture than anything anyone would think to write.
