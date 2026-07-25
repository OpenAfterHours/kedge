# House rules for generated code

**polars, never pandas.** This is enforced, not requested: a cell that imports pandas is rejected
by the validation gate before it reaches the kernel. pandas is heavily over-represented in
training data, so the drift back to it is real — do not spend a turn on it.

- **`LazyFrame` throughout; `.collect()` only at boundaries.** The boundaries are the
  reconciliation cell, the final output write, and anywhere a `mo.ui` element needs a materialised
  frame. Intermediate cells pass lazy frames to each other. This keeps the notebook's cell graph
  and polars' query plan aligned, which is what makes the notebook scale past the point where
  Excel gave up.
- **Expressions, not loops.** An Excel formula region is a column operation by construction — that
  is what the R1C1 compression in the analysis has already established. If you find yourself
  writing `for row in ...` over a frame, the translation of that region is *wrong*, not merely
  slow. Reach for `pl.when(...).then(...)`, `group_by().agg()`, `join`, `shift`, `cum_sum`.
  Row-wise loops over frames are flagged by the validation gate.
- **`pl.read_excel` defaults to the `calamine` engine** (verified on polars 1.43): fast, and needs
  no Excel install. Use it for hand-in loading.
- **Name every intermediate frame after the business step**, never `df2` or `tmp`.
- **Prefer editing an existing cell to adding a near-duplicate one.** A notebook of eleven clear
  stages beats one of forty micro-steps.
- Keep each cell to one logical operation from the plan. A cell that does three things cannot be
  reconciled against one workbook region.

## Imports

Put the imports in **one** cell and reference them everywhere else. Because of the single-
definition rule, a second `import polars as pl` anywhere in the notebook is a hard error. If an
import cell already exists — check the name registry — use it rather than adding another.

```python
import marimo as mo
import polars as pl

import kedge.xl  # registers the .xl expression namespace
```

## Excel compatibility is explicit, never implicit

Every place you rely on Excel's semantics rather than polars', say so in the code by going through
the `.xl` namespace. This is what makes the assumption greppable for a reviewer, which is far
better than an assumption hidden inside a `.round()` that happens to be wrong. The next section is
the list of places where that matters.
