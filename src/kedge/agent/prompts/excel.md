# Excel semantics that polars does not share — the reconciliation-breaker checklist

Every item below was verified against polars 1.43.0, and **every one of them produces silently
wrong numbers rather than an error**. Work through this list first whenever reconciliation reports
a small unexplained delta; in practice the overwhelming majority of them are on this page.

| Behaviour | Excel | polars | Consequence |
|---|---|---|---|
| Rounding at `.5` | half away from zero: `0.5 -> 1`, `2.5 -> 3` | banker's, half-to-even: `0.5 -> 0`, `2.5 -> 2` | **The single most likely source of pennies-out reconciliation failures.** Never translate `ROUND()` to `.round()` |
| Empty cell in arithmetic | treated as `0`: `1 + <empty> = 1` | null propagates: `1 + null = null` | Rows silently become null and vanish from downstream sums |
| Row-wise addition | `=A1+B1+C1` ignores empties | `pl.col("a") + pl.col("b")` propagates null | Use `pl.sum_horizontal(...)`, which matches Excel — verified |
| Column aggregate | `SUM()` ignores empties | `.sum()` ignores nulls | Matches. Safe |
| Divide by zero | `#DIV/0!` error, propagates visibly | `inf` / `-inf` / `nan` | `inf` poisons downstream aggregates quietly. Wrap every division |
| `VLOOKUP(..., FALSE)` | exact match, first hit | — | `join(..., how="left")`; check key uniqueness first or rows multiply |
| `VLOOKUP(..., TRUE)` | approximate, assumes sorted, takes largest <= key | — | `join_asof(strategy="backward")` — verified default |
| Text-formatted numbers | coerced on the fly | stays `String` | Silent type mismatch on join keys. Profile dtypes at load |
| Dates | 1900 serial system, including the 1900 leap-year bug | proper temporal types | Off-by-one on pre-1901 dates; rare but real |

## The rounding rule in full — this is not in most people's mental model

Excel's `ROUND` does **two** things, and getting only the first one right still leaves you a penny
out on real data.

1. It collapses the operand to **15 significant decimal digits** before doing anything else.
2. It then rounds **half away from zero**.

The first step is the one everybody misses. Verified against Excel itself:

```
ROUND(4491760.574999999, 2)  ->  4491760.58     not 4491760.57
```

The operand has 16 significant digits. Excel first collapses it to 15 — giving
`4491760.57500000` — and only then applies half-away-from-zero, which rounds *up*. Python,
polars and every naive implementation see `...574999999`, decide it is below the halfway point,
and round *down*. A one-penny error, on every affected row, propagating into every total
downstream of it.

`kedge.xl` implements both rules. **Use it, and never hand-roll rounding:**

```python
col("amount").xl.round(2)  # half away from zero, 15-significant-digit collapse, matching Excel
col("a").xl.add(col("b"))  # nulls treated as zero, matching Excel arithmetic
col("n").xl.div(col("d"))  # null on a zero denominator, never inf
col("x").xl.serial_to_date()  # 1900 serial system, leap-year bug included
```

A bare `.round()` on a value derived from an Excel `ROUND` region is rejected by the validation
gate, and the message will point you here. If `kedge.xl` is missing something you need, say so —
it is added there with tests, never open-coded in a notebook cell.

## Triage order when reconciliation fails

1. **Rounding mode.** If every mismatch is within half a unit of the last retained decimal place,
   it is `ROUND` semantics. Fix with `xl.round`.
2. **Null versus empty.** If mismatching rows are null on one side and a number on the other, it
   is null propagation. Fix with `xl.add` / `pl.sum_horizontal`.
3. **Join multiplication.** If the row *count* differs, the lookup key was not unique. Check with
   `probe` before changing anything.
4. **Type coercion.** If a join produced nulls where it should have matched, one side is `String`
   and the other is numeric.

Only once all four are excluded is it worth suspecting the translation itself.
