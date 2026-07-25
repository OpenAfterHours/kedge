# CLAUDE.md — working on `kedge`

`kedge` turns manual Excel processes into reviewable, reproducible marimo notebooks, with an AI
copilot that operates the notebook through a controlled tool surface. Published under
**OpenAfterHours**, Apache-2.0. Single-user, local, loopback-only.

Read this first, then the document you actually need:

| Document | What it is |
|---|---|
| `PLAN.md` | The author's brief. The *what* and the *why*. Section numbers are referenced everywhere. |
| `CONVENTIONS.md` | Binding style rules. Reviewers reject on these. |
| `docs/marimo-api.md` | **Empirically verified** marimo behaviour. Supersedes `PLAN.md` §1.1–§1.3 wherever they disagree. |
| `RELEASING.md` | How a version tag becomes a PyPI release, and how to recover when one fails. |

## Non-negotiables

1. **polars, never pandas.** Anywhere. The validation gate enforces it for generated code; hold
   the line yourself in hand-written code.
2. **`marimo._code_mode` is imported in exactly one module** — `src/kedge/notebook/driver.py`.
   Mostly it appears there inside the code strings driver.py POSTs to the kernel; `check_bridge()`
   also imports it directly, because proving the private API still has the shape kedge assumes
   means introspecting it (PLAN §6.1 mitigation 5). Nothing else may import it, and no library
   code a user runs may reach it. Enforced by `scripts/guardrails.py`, which parses imports —
   grep is no good here, since a dozen module docstrings mention `_code_mode` to explain why they
   do *not* import it.
3. **Excel semantics go through `kedge.xl`.** Never open-code half-away-from-zero rounding or
   null-as-zero arithmetic. If `kedge.xl` lacks something, add it there with tests.
4. **Every analyser extractor degrades gracefully.** Absent and unparseable are first-class
   results. A malformed workbook produces a `Finding`, never a traceback. `analyse()` always
   returns a complete `WorkbookAnalysis`.
5. **Never auto-discover a marimo server.** kedge owns its own process, addressed by explicit url
   and session id. The registry is readable only for diagnostics — and note it is written only
   for `--no-token` servers, so our own process never appears in it.
6. **Reconciliation never reports "passed" when it has no baseline.** A workbook with no cached
   values degrades to "not reconciled". This is the most dangerous failure mode in the project.

## Architecture in one paragraph

Three processes on one machine. A FastAPI server on `127.0.0.1` serves a chat pane and an
`<iframe>` holding a separate `marimo edit --headless` process, authenticated by a token in the
query string. The server talks to the marimo kernel over `POST /api/kernel/execute`, which runs
code in a scratchpad — so persistence must go through `_code_mode`, and reads are cheap and
non-polluting. Facts about the workbook are extracted deterministically (`analysis/`), the
conversion *plan* is proposed by the model and approved by the user (`plan/`), and the generated
code is validated before it ever reaches the kernel (`agent/validate.py`).

The layering that matters: `analysis/` → `plan/` → `notebook/` → `agent/` → `server/`.
`reconcile/` is consumed by the CLI, the notebook and the agent, so it sits below all three.

## Commands

```bash
uv run pytest                      # unit + corpus
uv run pytest -m contract          # live-kernel tests; spawns a real marimo
uv run pytest -m llm               # needs a configured model endpoint; skipped by default
uv run ruff check --fix . && uv run ruff format .
uv run ty check src/
uv run python scripts/guardrails.py        # non-negotiables 1 and 2, by AST not by grep
uv run python scripts/version.py v0.2.0    # does that tag match __version__?
uv run python tests/fixtures/generate.py   # regenerate the fixture corpus
uv run kedge --help
```

CI runs all of the above on Ubuntu and Windows across 3.12 and 3.13 (`.github/workflows/ci.yml`),
including the contract tests. `ty` is advisory there until its six known diagnostics are cleared.

## Testing

- `tests/unit/` — fast, pure. The default.
- `tests/corpus/` — the analyser over `tests/fixtures/*.xlsx`, asserted against `manifest.py`.
  The manifest is hand-written independently of the generator so the assertions are not
  tautological.
- `tests/contract/` — needs a live marimo kernel. Must clean up its subprocess even on failure.
- **Excel is installed and COM-drivable on this machine.** Where ground truth about Excel's
  behaviour is needed, drive Excel rather than reasoning about it — that is how the
  15-significant-digit rounding rule was found. Mark such tests opt-in and Windows-only; the
  committed expectations are the artifact, Excel is how you generate them.

## Things that have already bitten

- A fresh `marimo edit` has **zero sessions**, and `/api/kernel/execute` 500s until one exists.
  Bootstrap with `GET /sse?session_id=<ours>` held ~2s; the session survives the stream closing.
- `edit_cell` raises `StaleCellError` unless the cell was read at its current version — and
  `repr(ctx.cells)` silently records reads for every cell, disarming the guard. Never log it.
- `create_cell` defaults `hide_code=True`. kedge always passes `False`.
- Excel's `ROUND` collapses the operand to **15 significant decimal digits** before rounding
  half-away-from-zero. Missing this is a one-penny error that propagates.
- marimo's two file inputs return different things: `mo.ui.file` gives bytes with no path,
  `mo.ui.file_browser` gives a path with no bytes. An uploaded hand-in is **not reproducible**,
  which is why everything converges on a managed `HandIn` record.

## Style, briefly

British spelling. No emoji anywhere — code, commits, docs, or output. `from __future__ import
annotations` at the top of every module. `X | None`, never `Optional`. Frozen dataclasses by
default; pydantic only where validation or JSON Schema genuinely earns it. Module-level
`logger = logging.getLogger(__name__)` with lazy `%` formatting. No `print()` outside `cli.py`,
`tests/` and `scripts/`. Conventional Commits.
