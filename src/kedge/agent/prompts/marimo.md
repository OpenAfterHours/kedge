# marimo's single-definition rule — read this before writing any cell

**A public name may be defined by exactly one cell in the notebook. Imports included.**

Two cells doing `import polars as pl` is not a style problem; it is a hard error. marimo refuses
the whole batch with `RuntimeError: Multiply-defined names` and nothing is applied. The same is
true of two cells assigning `exposures`, or one cell assigning `total` while another does
`total = ...` inside an `if`. marimo builds its dataflow graph from these definitions, so the rule
is the graph, not a lint.

The **name registry** is injected into every turn. It is the authoritative `name -> owning cell`
table, read from the live kernel at the start of this turn. Before you write a cell:

1. Check every name your cell will define against the registry.
2. If a name is already owned, do **not** redefine it. Either
   - `edit_cell` the owning cell, replacing its body, or
   - choose a different name for your new value.
3. Never delete a cell to free up its names and then recreate them. Delete-then-create with the
   same names trips the check partway through the batch. `edit_cell` keeps the cell's identity and
   its place in the graph, and is almost always what you actually meant.

## The escape hatch

**Names beginning with an underscore are cell-local.** They are exempt from the rule and may
repeat freely across cells:

```python
_scaled = load_handin.select(pl.col("exposure") * 100)  # private: safe to repeat
exposures_scaled = _scaled.collect()  # public: must be unique
```

Use `_` for scratch values, loop variables and intermediate expressions that nothing downstream
needs. Use a public name only when another cell genuinely has to read it. This alone removes most
collisions.

## Cycles

A cell that reads a name defined by a cell downstream of it forms a cycle, and marimo refuses
that too. The dataflow graph is derived from names, not from cell order — moving a cell does not
fix a cycle. Restructure so the dependency runs one way.

## Before you submit

kedge checks your code against the live graph *before* it reaches the kernel, and returns any
collisions or cycles to you as violations. That is cheaper than a round trip, but it is not a
substitute for reading the registry: a rejected proposal is a wasted turn either way.


## A cell that builds UI and reads nothing cannot be hidden

marimo decides what to show from the dataflow graph, and the graph is built from *references*. A
cell that only constructs `mo.ui` elements references nothing, so it has no incoming edge and
renders the moment the notebook opens — whatever is supposed to happen first.

In a runbook that is not cosmetic. A cell offering "paste the re-extract here", visible before
the statement it is meant to follow has even been generated, invites somebody to paste an
extract taken *before* the update ran. Nothing downstream can tell the difference, and neither
can anyone afterwards.

So a cell that should wait its turn must read the token of the step before it, even if it does
nothing with the value:

```python
_after_post_adjustment = update_statement_confirmed  # the edge is the gate
post_adjustment_drop = mo.ui.file(kind="area", label="Drop the re-extract here")
```

The other half of the same rule: `mo.stop` halts the cell **and everything downstream**, and in
app mode a stopped cell's message is the only thing left on the page. Every one of them names
its step and says what to do — `**Step 3 of 8: the extract.** Run the query above, then paste
what it returned.` A blocked page that explains nothing is indistinguishable from a broken one.
