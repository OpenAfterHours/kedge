# kedge

[![CI](https://github.com/OpenAfterHours/kedge/actions/workflows/ci.yml/badge.svg)](https://github.com/OpenAfterHours/kedge/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/kedge.svg)](https://pypi.org/project/kedge/)
[![Python](https://img.shields.io/pypi/pyversions/kedge.svg)](https://pypi.org/project/kedge/)
[![Licence](https://img.shields.io/badge/licence-Apache--2.0-blue.svg)](LICENSE)

Turn manual Excel processes into reviewable, reproducible [marimo](https://marimo.io) notebooks,
with an AI copilot that operates the notebook through a controlled tool surface.

> **Status:** under active construction. See `PLAN.md` for the full brief.

## What it does

1. **Analyses** a workbook offline — formula regions, dependency graph, SQL connections,
   Power Query M, cached values, and a findings list (circular refs, volatile functions,
   `IFERROR` swallowing, inconsistent formulas within a region).
2. **Plans** the conversion — an AI-proposed, user-approved `ProcessPlan` describing stages,
   open questions, and what it intends to drop.
3. **Scaffolds** a marimo notebook from the approved plan.
4. **Reconciles** the generated Python against the values Excel last cached, so the
   translation checks itself against evidence rather than declaring itself finished. That is an
   acceptance test on the *conversion*: it is measured once, recorded, and cited afterwards —
   the workbook is no baseline for next quarter's data.

Most of the spreadsheets worth converting are not calculations. They are the record of a
**process**: run an extract, work out an adjustment, run an update, re-extract to prove it took,
write a memo somebody signs. So the notebook kedge produces is a **runbook**, not a pipeline. It
hands the user each statement in turn, takes the results back through a managed hand-in, does
the arithmetic in between where it can be checked, blocks on a recorded decision before anything
touches production, and verifies the re-extract row by row against what it predicted.

kedge runs none of that SQL. It holds no database connection and issues no statement. What it
does is work out exactly what needs running, say where, wait, and refuse to claim the change
worked until it has seen evidence that it did — keeping the state on disk in between, because a
process that takes two days is one you have to be able to walk away from.

## Quick start

```bash
uv tool install kedge
kedge hub                  # a browser landing page: every workbook kedge has seen
kedge open process.xlsx    # straight to one, if you already know which
```

`kedge hub` starts the server with nothing open and puts a browser on the list. Add a workbook by
browsing the filesystem, pasting a path, or dropping one on the page; each row shows whether the
file is still there, whether a notebook and an approved plan exist, how many findings the analysis
turned up and how much of the workbook the plan believes it can convert. Opening one runs the same
sequence `kedge open` runs — clean up, analyse, plan, scaffold, spawn marimo, bootstrap the session
— with the progress streamed into the page, and lands you in the chat-plus-notebook view. Where a
marimo kedge started is still running, the hub offers to reattach rather than starting a second.

The AI half needs an endpoint. Open **Settings** on the hub and give it an OpenAI-compatible base
URL, a key and a model — the hosted API, a gateway, or something local. The base URL and model go
to `~/.kedge/config.toml`; the key goes to the operating system's keyring and never to a file.
Until a key is stored, workbooks open in demo mode, where a scripted agent answers and nothing is
sent to a model — so the analysis, the scaffold and the notebook all work with no endpoint at all.

**Behind a corporate proxy**, kedge verifies the endpoint against your operating system's trust
store rather than against Python's bundled `certifi`, so a TLS-inspecting proxy whose root your
IT department has already installed just works. Where it does not, `kedge doctor` says which
certificates are in play and what to do; the fix is `ca_bundle` under `[model]`, pointing at the
proxy's root as a PEM. There is no option to disable verification (`SECURITY.md` says why).

**Reasoning** is set in the same panel, and left unset by default. kedge prefers the responses API
because it is the only one that carries a reasoning model's thinking across a tool call, and every
kedge turn is tool calls. Neither that choice nor the reasoning setting can end a turn: an endpoint
with no `/responses` route is discovered on the first call and spoken to in chat completions from
then on, and a request refused over reasoning is retried without it. Pin `api` under `[model]` in
`~/.kedge/config.toml` to skip the probe.

Or, offline and standalone — the analyser is useful on its own:

```bash
kedge inspect process.xlsx --out analysis.json --report report.html
```

`docs/analyser-worked-example.md` walks that output line by line over two of the test fixtures —
a clean pipeline and a deliberately hostile workbook — which is the quickest way to see what the
analyser actually finds before pointing it at anything of your own.

The planning step is a set of commands as well as a conversation, and the review gate is the same
either way:

```bash
kedge plan propose process.xlsx --dry-run   # read the plan; write nothing
kedge plan propose process.xlsx             # save it, as a draft
kedge plan show process.xlsx                # stages, open questions, drops, what blocks approval
kedge plan acknowledge process.xlsx --all   # sign off the ranges it proposes to drop
kedge plan approve process.xlsx             # nothing is scaffolded before this
```

Only `propose` needs a model. Every other verb reads a plan from disk and writes a decision back,
so a plan can be read, questioned and approved with no endpoint configured at all. Approving is
always a separate act — there is no flag that proposes and approves in one breath — and a plan
that proposes dropping a range cannot be approved until each drop has been confirmed or refused,
because silent removal is indistinguishable from a bug. `kedge plan reject` and
`kedge plan request-changes` are the other two answers, and `kedge plan history` lists every
version with its approval state: when the process changes next quarter, the diff of the plan is
the change record, which only works if last quarter's plan is still there.

Replacing a plan that is already approved shows you what changes before it happens. `propose`
prints the diff against the version in force, and `approve` prints it again and asks, so nobody
swaps one decomposition for another without seeing the two side by side; `--yes` skips the
question for scripts. Taking an approval back is deliberate in the same way: `reject` and
`request-changes` refuse an approved plan unless you pass `--withdraw-approval`, because a
notebook may already have been scaffolded from it. A rejection is terminal — the way on from one
is a new plan, not an edit to the rejected one.

`propose` exits **2** when triage recommends against converting the workbook at all. That is a
result rather than a failure, and it is a different exit code from an ordinary error so a script
can tell "this workbook should not be converted" from "no such workbook". `--force` overrides it.

Once the notebook exists, hand-ins arrive through a watched folder rather than by hand:

```bash
kedge watch process.xlsx --dir \\share\inbox --once   # sweep and exit; for a scheduled task
kedge watch process.xlsx                              # watch until Ctrl-C
```

Every file is copied into the workbook's managed store, hashed, dated and receipted, so "this run
consumed this file" is a claim you can defend. `--once` is idempotent: a file already in the store
is skipped by hash. `--dir` is relative to where you are standing; set `ingest.watch_dir` in a
`kedge.toml` beside the workbook instead and it is relative to the workbook, so a scheduled sweep
finds the same folder you did.

## Design notes

- Generated code is **polars**, never pandas — enforced in the validation gate.
- Excel's semantics do not match polars' (rounding mode, empty-vs-null, divide-by-zero).
  `kedge.xl` is a registered polars namespace that makes each compatibility choice
  explicit and greppable: `col("amount").xl.round(2)`.
- Single-user, local, loopback-bound. No accounts, no server deployment.

## Contributing

`CONTRIBUTING.md` is the short version; `CONVENTIONS.md` is binding — read it before opening a
pull request. `CLAUDE.md` is the shortest useful orientation to the codebase, `RELEASING.md`
covers how a version tag becomes a release, and `SECURITY.md` describes the actual trust boundary
(loopback, no auth, the machine rather than the account), which is worth reading before touching
anything that binds a socket or logs a payload.

```bash
uv sync
uv run pytest                       # unit + corpus
uv run pytest -m contract           # live-kernel tests; spawns a real marimo
uv run ruff check . && uv run ruff format --check .
uv run python scripts/guardrails.py
```

Apache-2.0.
