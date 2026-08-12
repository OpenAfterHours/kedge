<!--
Delete any section that does not apply. The dependency sections at the bottom exist so that a
Dependabot pin bump walks its reviewer through PLAN 6.1 rather than relying on them to remember
it; on an ordinary change they are noise and should go.
-->

## What and why

<!-- One paragraph. What changed and what it is for. Cite PLAN sections where they apply. -->

## The gate

CI runs all of this on Ubuntu and Windows across 3.12 and 3.13. The list is here so you find out
before the reviewer does.

- [ ] `uv run ruff check .` and `uv run ruff format --check .` are clean
- [ ] `uv run python scripts/guardrails.py` passes — polars never pandas, and `marimo._code_mode`
      imported only by `src/kedge/notebook/driver.py`
- [ ] `uv run pytest` is green (unit + corpus)
- [ ] `uv run pytest -m contract` is green, or nothing on the notebook bridge changed
- [ ] `CONVENTIONS.md` holds: British spelling, no emoji, `from __future__ import annotations`,
      `X | None`, frozen dataclasses by default, lazy `%` formatting in log calls
- [ ] Commits are Conventional Commits
- [ ] Anything left undone or knowingly imperfect is said out loud above, not left to be found

## If this changes the `marimo` pin

`marimo==0.23.15` is exact because `_code_mode` is a private API with no versioning guarantees
(PLAN 6.1). Every one of these, or the bump does not merge.

- [ ] `uv run pytest -m contract` is green **on both platforms** — this is the only thing in the
      suite that would notice marimo changing the API under the pin (PLAN 6.1 mitigation 3)
- [ ] `check_bridge()` reports no problems:
      `uv run python -c "from kedge.notebook import check_bridge; print(check_bridge())"`
      should print `problems=()` and the new version (PLAN 6.1 mitigation 5)
- [ ] `MARIMO_PIN` in `src/kedge/lifecycle.py` matches `pyproject.toml` — Dependabot changes only
      the manifest. `test_the_installed_marimo_matches_the_pin` fails until you update the
      constant, and `uv run kedge doctor` says `marimo ... matches the pin` once you have
- [ ] `docs/marimo-api.md` re-verified against the new version by launching a real server, and its
      "Verified against" line updated. That document supersedes PLAN 1.1–1.3, so a stale one is
      worse than none
- [ ] The `GET /sse` session bootstrap still yields a usable session id — a fresh `marimo edit`
      has zero sessions and `/api/kernel/execute` 500s until one exists
- [ ] Not auto-merged, and no auto-merge added

## If this changes the `polars` pin

`polars==1.43.2` is exact because polars has changed rounding behaviour between versions
(PLAN 6.2). The `kedge.xl` tests are the tripwire and they are transcribed from Excel's actual
output, so a failure there means Excel and polars now disagree, not that the table is stale.

- [ ] `uv run pytest tests/unit/test_xl_rounding.py tests/unit/test_xl_nulls.py
      tests/unit/test_xl_dates.py tests/unit/test_xl_namespace.py` is green
- [ ] `uv run pytest -m corpus` is green — the analyser profiles every fixture through polars
- [ ] Any changed expectation was re-derived from Excel, not adjusted to make the suite pass
