# The translation vocabulary

**This is a vocabulary the plan is written in, not a template the plan must fit.**

The distinction matters. The mappings below are fixed and deterministic — you should never be
re-deriving that an exact `VLOOKUP` is a left join. But *which* patterns apply, in what order,
and how they compose into stages is entirely your decision, and it is the only thing that varies
per workbook. A plan that names one pattern per stage in table order has misunderstood the job.

Where a workbook does something this table does not cover, that is a signal for an
`open_question` — not an invitation to improvise silently.

| Excel pattern | vocabulary name | polars shape |
|---|---|---|
| `=A2*B2`, arithmetic | `arithmetic` | `col("a") * col("b")`, via `xl.*` where empties are possible |
| `=IF(c, x, y)` | `if_simple` | `pl.when(c).then(x).otherwise(y)` |
| nested `IF` chains | `if_nested` | chained `.when().then()`, one per branch |
| `=IFERROR(x, y)` | `iferror` | `pl.when(<error cond>).then(y).otherwise(x)` — condition made explicit, never swallowed |
| `=SUMIF(r, c, s)` | `sumif` | `.filter(c).select(col(s).sum())`, or `group_by().agg()` if the criterion is a key |
| `=SUMIFS` / `COUNTIFS` | `sumifs` / `countifs` | `group_by([...]).agg([...])` then join back |
| `=VLOOKUP(k, t, n, FALSE)` | `vlookup_exact` | `.join(t, left_on=k, right_on=..., how="left")` |
| `=VLOOKUP(k, t, n, TRUE)` | `vlookup_approx` | `.join_asof(t, on=k, strategy="backward")` |
| `=INDEX(r, MATCH(k, m, 0))` | `index_match` | same as exact `VLOOKUP` — a join |
| `=SUMPRODUCT(a, b)` | `sumproduct` | `(col("a") * col("b")).sum()` |
| running total down a column | `running_total` | `col("x").cum_sum()` |
| `=A2` referencing the row above | `prior_row` | `col("x").shift(1)` — a window, not a loop |
| absolute ref `$B$1` to a parameter | `parameter_ref` | a Python constant, or a value from the parameters frame |
| whole-column formula fill | (the region itself) | one expression on the frame |
| pivot table | `pivot` | `.group_by(...).agg(...)` or `.pivot(...)` |
| `SUBTOTAL` with filters applied | `subtotal` | `.filter(...)` then aggregate — check which rows were actually visible |
| text functions | `text_manipulation` | string expressions |
| date arithmetic | `date_arithmetic` | temporal expressions, via `xl.serial_to_date()` where serials appear |
| a literal block | `literal` | a parameters frame or constants |
| anything else | `unknown` | raise an open question |

# Excel semantics polars does not share

Every item below produces **silently wrong numbers** rather than an error. Flag in a stage's
`assumptions` or `notes` wherever one of these applies to the region it translates, so the
generated code reaches for `kedge.xl` rather than the naive equivalent.

| Behaviour | Excel | polars | Consequence |
|---|---|---|---|
| Rounding at `.5` | half away from zero: `0.5 -> 1`, `2.5 -> 3` | banker's, half-to-even: `0.5 -> 0`, `2.5 -> 2` | The single most likely source of pennies-out reconciliation failures. `ROUND()` never becomes `.round()` |
| Empty cell in arithmetic | treated as `0`: `1 + <empty> = 1` | null propagates: `1 + null = null` | Rows silently become null and vanish from downstream sums |
| Row-wise addition | `=A1+B1+C1` ignores empties | `col("a")+col("b")` propagates null | `pl.sum_horizontal(...)` matches Excel |
| Column aggregate | `SUM()` ignores empties | `.sum()` ignores nulls | Matches. Safe |
| Divide by zero | `#DIV/0!`, propagates visibly | `inf` / `-inf` / `nan` | `inf` poisons downstream aggregates quietly. Every division gets wrapped |
| Text-formatted numbers | coerced on the fly | stays `String` | Silent type mismatch on join keys. Dtypes are profiled at load |
| Dates | 1900 serial system, with the leap-year bug | proper temporal types | Off-by-one on pre-1901 dates |
| `VLOOKUP` exact | first hit wins | a join multiplies rows on a duplicate key | Key uniqueness is checked before the join, not after |

`kedge.xl` exists so generated code states its Excel-compatibility intent inline —
`col("amount").xl.round(2)`, `col("a").xl.add(col("b"))`, `col("n").xl.div(col("d"))`,
`col("x").xl.serial_to_date()`. Mention it in a stage's notes where the region depends on one of
these behaviours; it makes the assumption greppable for a reviewer.
