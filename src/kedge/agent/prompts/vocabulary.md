# Formula to polars — a vocabulary, not a template

These mappings are deterministic and are given to you so that you never spend reasoning
re-deriving that an exact `VLOOKUP` is a left join. **They are a vocabulary the plan is written
in, not a template the plan must fit.** You decide which patterns apply, in what order, and how
they compose into stages.

| Excel pattern | polars |
|---|---|
| `=A2*B2`, arithmetic | `col("a") * col("b")` — via `xl.*` wherever empty cells are possible |
| `=IF(c, x, y)` | `pl.when(c).then(x).otherwise(y)` |
| nested `IF` chains | chained `.when().then()`, one per branch — this flattens deep nesting nicely |
| `=IFERROR(x, y)` | `pl.when(<error condition>).then(y).otherwise(x)` — make the condition explicit; do not swallow it |
| `=SUMIF(r, c, s)` | `.filter(c).select(col(s).sum())`, or `group_by().agg()` when the criterion is a key |
| `=SUMIFS` / `=COUNTIFS` | `group_by([...]).agg([...])`, then join back |
| `=VLOOKUP(k, t, n, FALSE)` | `.join(t, left_on=k, right_on=..., how="left")` |
| `=VLOOKUP(k, t, n, TRUE)` | `.join_asof(t, on=k, strategy="backward")` |
| `=INDEX(r, MATCH(k, m, 0))` | the same as an exact `VLOOKUP` — a join |
| `=SUMPRODUCT(a, b)` | `(col("a") * col("b")).sum()` |
| running total down a column | `col("x").cum_sum()` |
| `=A2` referencing the row above | `col("x").shift(1)` — a window, never a loop |
| absolute ref `$B$1` to a parameter | a Python constant, or a value read from the parameters frame |
| whole-column formula fill | one expression over the frame; this *is* the R1C1 region |
| pivot table | `.group_by(...).agg(...)` or `.pivot(...)` |
| `SUBTOTAL` with filters applied | `.filter(...)` then aggregate — check which rows were actually visible |

## When the workbook does something this table does not cover

That is a signal for an **open question**, not an invitation to improvise silently. Say what the
formula does, say what you are unsure about, and ask. A translation you flagged is a decision the
user made; a translation you guessed is a defect nobody knows about yet.

## Two patterns that stop the work

- **Iterative or circular calculation.** Excel's iterative-calc setting is on for a reason and
  there is no clean polars equivalent. Raise it; do not attempt a fixed-point loop.
- **An unresolvable external workbook link.** The values are cached from a file you cannot see.
  Raise it; do not treat the cached numbers as inputs.
