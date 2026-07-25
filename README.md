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
   translation checks itself against evidence rather than declaring itself finished.

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

Or, offline and standalone — the analyser is useful on its own:

```bash
kedge inspect process.xlsx --out analysis.json --report report.html
```

## Design notes

- Generated code is **polars**, never pandas — enforced in the validation gate.
- Excel's semantics do not match polars' (rounding mode, empty-vs-null, divide-by-zero).
  `kedge.xl` is a registered polars namespace that makes each compatibility choice
  explicit and greppable: `col("amount").xl.round(2)`.
- Single-user, local, loopback-bound. No accounts, no server deployment.

## Contributing

`CONVENTIONS.md` is binding — read it before opening a pull request. `CLAUDE.md` is the shortest
useful orientation to the codebase, and `RELEASING.md` covers how a version tag becomes a release.

```bash
uv sync
uv run pytest                       # unit + corpus
uv run pytest -m contract           # live-kernel tests; spawns a real marimo
uv run ruff check . && uv run ruff format --check .
uv run python scripts/guardrails.py
```

Apache-2.0.
