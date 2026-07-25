# Glossary

Terms that appear in the workbooks and mean something specific here. The copilot reads this, so
an entry is worth adding whenever a column name would otherwise be guessed at.

Replace these examples with your own.

**Hand-in** — the file delivered by another team that starts the process. Always consumed
through a `HandIn` record so the run records exactly which bytes it used.

**COB** — close of business. `cob_date` is the position date, not the date the file arrived.
The two differ, and using the wrong one is a whole-run error that reconciles cleanly against
the wrong month.

**EAD** — exposure at default. In these workbooks it is post-CRM and post-credit-conversion
unless a column name says otherwise.

**Haircut** — the proportional reduction applied to collateral value by asset class. Looked up
exactly, never approximately: an approximate match here silently values collateral at the wrong
band.

**Netting set** — the group of trades that may be offset against one another. The grain of most
counterparty tables is one row per netting set per COB date, *not* one row per counterparty,
which is the single most common cause of a join multiplying rows.

**Override** — a value typed by an analyst that replaces a calculated one. Overrides are
checkpoint stages: recorded with a reason, never inferred.

**Staging** — the intermediate sheet a Power Query load lands on. Usually not part of the logic;
check for downstream references before treating it as dead.
