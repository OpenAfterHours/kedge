# Contributing to kedge

Short, because most of what you need is written down elsewhere and restating it here would only
give it somewhere to drift to.

| Read | For |
|---|---|
| [`CONVENTIONS.md`](CONVENTIONS.md) | **Binding.** Style, layering, testing, the non-negotiables. Reviewers reject on it. |
| [`CLAUDE.md`](CLAUDE.md) | The shortest useful orientation to the codebase. |
| [`PLAN.md`](PLAN.md) | The author's brief — the *what* and the *why*. Section numbers are cited throughout the code. |
| [`docs/marimo-api.md`](docs/marimo-api.md) | Empirically verified marimo behaviour. Supersedes PLAN 1.1–1.3 wherever they disagree. |
| [`docs/analyser-worked-example.md`](docs/analyser-worked-example.md) | What `kedge inspect` actually produces, walked through on two committed fixtures. |
| [`RELEASING.md`](RELEASING.md) | How a version tag becomes a PyPI release. |
| [`SECURITY.md`](SECURITY.md) | What kedge's security surface is, and where to report. |

## Getting set up

```bash
uv sync
uv run kedge --help
uv run kedge doctor        # python, marimo pin, config, keyring, endpoint, stale markers
```

## The gate

Everything below runs in CI on Ubuntu and Windows across 3.12 and 3.13, in roughly this order.
Running it locally first is faster than finding out from a red tick.

```bash
uv run ruff check --fix . && uv run ruff format .
uv run python scripts/guardrails.py     # the non-negotiables, by AST rather than by grep
uv run pytest                           # unit + corpus
uv run pytest -m contract               # live-kernel; spawns a real marimo, ~1 minute
uv run ty check src/                    # advisory - see docs/ty-diagnostics.md
```

`scripts/guardrails.py` is the one worth understanding before you trip it. It parses imports, so
it enforces what `CONVENTIONS.md` means rather than what a grep would catch: no `pandas` import
anywhere under `src/`, `tests/` or `utils/`, and `marimo._code_mode` imported by exactly one
module, `src/kedge/notebook/driver.py`. A docstring naming either is fine — a dozen of them do,
explaining why that module does not import it.

`ty` is Astral's type checker, not mypy, and it is advisory in CI while a small set of known
diagnostics is cleared. `docs/ty-diagnostics.md` lists them individually; do not add to it
silently.

## Commits and pull requests

- **[Conventional Commits](https://www.conventionalcommits.org/).** `feat:`, `fix:`, `docs:`,
  `test:`, `refactor:`, `chore:`, `ci:`, with an optional scope: `fix(reconcile): ...`.
- The pull request template lists the gate, and carries extra checklists for a `marimo` or
  `polars` bump. Delete the sections that do not apply.
- **British spelling, and no emoji anywhere** — code, commits, docs or output.
- Say what is unfinished. An honest gap in the description is useful; a `pass` stub presented as
  finished is a defect.

Dependency bumps arrive by Dependabot (`.github/dependabot.yml`) and are reviewed by hand. Nothing
is auto-merged, and `marimo` and `polars` least of all: the exact pins are the point, and the
notification is the value.

## The test tiers

| Tier | Command | What it needs |
|---|---|---|
| `tests/unit/` | `uv run pytest` | Nothing. Fast and pure. The default, and where most tests belong. |
| `tests/corpus/` | `uv run pytest -m corpus` | The committed `tests/fixtures/*.xlsx`, asserted against `manifest.py`. Runs as part of the default suite too. |
| `tests/contract/` | `uv run pytest -m contract` | Spawns a real headless marimo. Excluded by default; must clean up its subprocess even on failure. |
| LLM-dependent | `uv run pytest -m llm` | A configured model endpoint. Skipped by default. |

Two things about the corpus are easy to get wrong. `tests/fixtures/manifest.py` is written by hand
and independently of `tests/fixtures/generate.py`, so that asserting the analyser against it proves
something other than the generator agreeing with itself — keep it that way. And only
`clean_pipeline.xlsx` carries cached values, so it is the corpus's single positive path for
reconciliation; everything else must degrade to "not reconciled", never to "passed".

The contract tests are the only thing in the suite that would notice marimo changing `_code_mode`
under the pin, which is why a marimo bump does not merge until they are green (PLAN 6.1).

**Excel as an oracle.** Where ground truth about Excel's behaviour is needed, drive Excel rather
than reasoning about it — that is how the 15-significant-digit rounding rule was found. Such tests
are opt-in and Windows-only, they need `pywin32` which is not a project dependency, and they do not
run in CI. The committed expectations are the artifact; Excel is only how you generate them.

## Where things live

The layering is `analysis/` → `plan/` → `notebook/` → `agent/` → `server/`, with `reconcile/`
underneath all of them because the CLI, the notebook and the agent all consume it. A change that
crosses those layers, or that touches `src/kedge/analysis/model.py`, is a cross-cutting change —
say so loudly in the pull request rather than making it quietly.
