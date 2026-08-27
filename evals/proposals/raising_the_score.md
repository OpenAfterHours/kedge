# Plan: getting the composed path from 11 to something worth quoting

Status: **proposed**. Every figure marked *measured* comes from `evals/run.py` on artifacts in the
tree; everything else is marked *expected* and says what would confirm it.

## Where we are

| Conversion | Score | What it is |
|---|---|---|
| Reference (`notebook.py` + `plan.yaml`) | **71/71** | the gold answer, measured |
| The hub conversion of 26 Aug | **11/69** | a real run, measured |
| That plan, freshly scaffolded, holes unfilled | 9/71 | the floor, measured |

**The number to judge 11 against is not 69.** With the plan that conversion was built from, at
least 27 points are unreachable *however well the cells are written* -- you cannot pass
"generates the update from the frame" when the plan never declares an update -- and because the
re-extract has nowhere to arrive the notebook stops at Step 6 of 7, putting roughly 18 more out
of reach. **That plan caps the conversion at about 24/69.**

So there are two different jobs here, and only one of them is about models:

- **Raise the ceiling.** Make kedge build a working notebook from an imperfect plan. Deterministic,
  no model in the loop, and it cannot regress the reference.
- **Raise the plan.** Get the model to declare the hand-off, the second hand-in and the briefing.

The first is worth more than it looks and is not model-dependent, so it goes first.

---

## Phase 0 -- find out whether the amendment pass works (no code)

`repairable_warnings` and the amendment pass are built and unit-tested, and their *yield* has
never been measured: the tests prove the findings reach the model and that a worse plan loses,
not that a real model acts on them.

```bash
uv run python evals/run.py adjustment_signoff --model MODEL --repeats 3   # the planning seam
uv run python evals/run.py adjustment_signoff --convert MODEL --plan-from MODEL
```

The first is one call per repeat and grades the structural tier -- the cheapest possible test of
whether the amendment lands a `handoff`, a second `handin` and a `briefing`. The second is the
composed path and the number this whole document is about.

Do this first because it decides how much of Phase 2 is needed. **Expected:** structural 5/26
becomes 20+/26. If it does not, the finding is that a model told exactly what to fix still will
not, and that is worth knowing before any more prompt work.

---

## Phase 1 -- make the scaffolder tolerant of a reasonable plan

Both of these raise the ceiling for *every* plan, need no model, and are verified by re-grading
artifacts already on disk.

### 1.1 A checkpoint that declares a hand-in should get one -- **~21 points**

`build_cells` dispatches on kind:

```python
for index, stage in enumerate(ordered, start=1):
    if stage.is_checkpoint:
        cells.extend(_checkpoint_cells(...))
        continue                     # <- never reaches _named_handin
    label = _named_handin(stage)
    if label is not None:
        cells.extend(_handin_cells(...))
```

A plan that says "this step takes the re-extract and then records a decision about it" is not
unreasonable -- it is what the process does. kedge reads the hand-in, shows it on the approval
card, and then drops it on the floor, and the file has nowhere to arrive.

Move the `_named_handin` check above the branch so a checkpoint gets its receiver cells and then
its decision cells, in that order, which is also the order the user works in. `review_warnings`
keeps its message but it stops being load-bearing: the notebook works either way.

Unblocks `takes_two_handins` (3) and `ran_to_completion` (2), and with the run reaching the end,
`verification_finds_exactly_one_break` (3), `totals_to_the_penny` (3),
`reconciliation_does_not_rot_on_a_later_period` (3), `the_run_resumes_after_the_kernel_dies` (3),
`starting_fresh_keeps_the_old_run` (2) and `does_not_trust_the_impact_summary` (2) become
gradeable rather than blocked.

**Verify:** re-grade `q2_accrual_adjustment.kedge` and the archived plan. The run should pass Step
6 of 7. The reference must stay at 71/71 -- its plan declares hand-ins on `load` stages, so this
path does not touch it.

### 1.2 Put the query above the box that asks for its output -- **3 points**

Item 2 of the previous proposal, still open. A runbook that asks for a file before naming the
query that produces it has the two halves of one step in the wrong order, and it is exactly what
a user hit: *"where is the sql to run to get the starting data?"*, then *"i can't see it"*.

**Verify:** `progressive_disclosure`. Note it now renames through the role map, so it grades a
conversion whatever its stages are called.

---

## Phase 2 -- the planning seam

Scope decided by Phase 0. If the amendment pass lands the three declarations, this phase is done
and the ceiling is roughly the whole rubric. If it does not:

**Offer the hand-off rather than requiring it to be invented.** The analyser already types
`Adjustment!G` as generated SQL and holds the connection's `command`; `review_warnings` already
quotes both. The step kedge is missing is turning that into a *proposed* `Handoff` on the approval
card -- a concrete amendment the user accepts or rejects -- rather than a sentence asking the model
to write one.

This stays on the user's side of the gate, which is the line the previous proposal drew and it
still holds: kedge may **draft**, the user approves, and nothing is written silently. What it must
not become is kedge writing the plan and the approval turning into a formality.

---

## Phase 3 -- the remaining thirteen

Four items that are not explained by the three plan defects, and each needs its own diagnosis
before anything is written:

| Item | Weight |
|---|---|
| `reconciles_against_the_workbook` | 3 |
| `a_paste_out_of_excel_works` | 3 |
| `a_declared_exception_does_not_read_as_a_defect` | 2 |
| `progressive_disclosure` (also 1.2) | 3 |

These are cell-writing and scaffolder quality rather than structure. Do them last: with the run
stopping at Step 6, several are being reported against a notebook that never reached them.

---

## What would count as success

1. **The composed path**, `--convert M --plan-from M`, against 71. There is no figure for it yet;
   Phase 0 produces the first one.
2. **The ceiling**, which is the more honest measure while the plan is the problem: how many points
   are reachable at all given the plan, versus how many were earned. 11 against a ceiling of 24 is
   a different report from 11 against 69, and only the first says where to work.
3. **Points blocked**: 26 today. While that is large the deterministic tier is not really being
   measured, and Phase 1 is aimed squarely at it.

## What I would not do

- **Do not tune anything to the rubric.** Every item here is a defect a user met -- a page that
  stops, a file with nowhere to go, a notebook that says the workbook explained nothing. If a fix
  raises a score without fixing one of those, it is the wrong fix.
- **Do not let kedge write the plan.** Phase 2's drafted hand-off is offered, never applied. The
  approval gate is the product.
- **Do not chase Phase 3 first.** It is the smallest pile and several of its items cannot be
  measured honestly until the run reaches the end.
