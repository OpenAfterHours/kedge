# Conventions for converted processes

This file is yours to edit. It is loaded into the copilot's context on every turn, so keep it
short and factual — it is a place for the things that are true about *your* processes and that
no amount of reading the workbook would reveal.

Delete the examples below and replace them with your own.

## Naming

- Frames are named after the business step, not the mechanics: `exposures_after_haircut`,
  not `df2` or `tmp`. A notebook nobody can review is not an improvement on a spreadsheet
  nobody can review.
- Monetary columns carry their currency: `ead_gbp`, not `ead`.
- Dates are `_date` suffixed and are real temporal types, never strings and never Excel serials
  once past the ingest boundary.

## Rounding and money

- Any figure that appears in a published output is rounded with `col(...).xl.round(2)`, which
  reproduces Excel's rounding exactly. Never `.round()` — polars uses banker's rounding and
  Excel does not, and the difference is a penny that reconciliation will find.
- Intermediate calculations are not rounded. Round once, at the point of publication.

## Reconciliation tolerance

- Default is 1e-6 absolute. A process that cannot meet that has a real difference, not a
  floating-point one, and the difference should be explained rather than the tolerance widened.
- Record in the notebook why any looser tolerance was chosen.

## What must never be automated

- Anything a human currently signs off stays a `checkpoint` stage. Converting a judgement call
  into code fabricates logic that was never there and quietly removes a control.

## Reporting calendar

- The hand-in arrives on the third working day. The process runs against the prior month end.
- Figures are reported in thousands unless the output sheet says otherwise.
