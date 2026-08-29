# kedge — conventions for contributors (human and agent)

Read `PLAN.md` for *what* to build. This file is *how*. It is binding: reviewers reject on it.

## Non-negotiables

1. **polars, never pandas.** No `import pandas` anywhere in `src/`, tests, fixtures, or generated
   code. The validation gate enforces this for generated code; you are expected to hold the line
   in hand-written code without being told.
2. **`marimo._code_mode` is imported in exactly one place**: `src/kedge/notebook/driver.py`.
   Nothing else in the repo may reference it, import it, or emit code that mentions it.
   Grep `_code_mode` — if it appears outside `driver.py` and its tests, that is a bug (PLAN §6.1).
3. **Excel semantics go through `kedge.xl`, and SQL literals through `kedge.sql`.** Never
   open-code half-away-from-zero rounding or null-as-zero arithmetic; never build a statement
   with `+` or an f-string. If either module lacks what you need, add it there with tests. The
   SQL half is the same class of bug as the Excel half: an apostrophe in a counterparty name, a
   null, a date, a money value at the edge of exponent notation. Every one is ordinary in a
   finance extract and every one breaks a concatenated statement, silently, at the moment
   somebody runs it against production. Unlike 1, 2, 6, 7 and 8 this one is **not** machine
   enforced -- no AST pass can tell SQL-shaped string building from any other -- so it holds
   only as long as reviewers hold it.
4. **Every extractor degrades gracefully.** An analyser sub-extractor returns an "absent" or
   "unparseable" result and keeps going. It never raises past its own boundary. A malformed
   workbook produces a `Finding`, not a traceback (PLAN §M1).
5. **Never auto-discover a marimo server.** kedge owns its own process, targeted by explicit
   url + session id. Reading the registry is permitted *only* to identify a stale
   kedge-owned process via our own marker file (PLAN §2.9).
6. **marimo's HTTP API is spoken by `src/kedge/marimo_http.py` alone.**
   `src/kedge/notebook/kernel.py` is the one deliberate exception — streaming
   `POST /api/kernel/execute` asynchronously is a different shape from the rest — and even it
   imports the path and the auth headers from `marimo_http`. One marimo release moving an
   endpoint should cost one file, not a hunt. Enforced by `scripts/guardrails.py`.
7. **Certificate trust is decided in `src/kedge/tls.py` alone.** The model endpoint is the only
   thing kedge speaks TLS to, and the machines this runs on sit behind TLS-inspecting proxies
   whose root is in the OS trust store and never in `certifi`. Build outbound clients with
   `tls.client()` / `tls.async_client()`, and give the `openai` SDK an explicit `http_client=` —
   left to itself it makes one against `certifi` and the first model call dies with an error
   that mentions no proxy. **There is no setting that turns verification off**, and adding one
   would be a regression, not a feature: `[model] ca_bundle` names a PEM instead, which is
   visible to whoever reads that config next. Enforced by `scripts/guardrails.py`.
8. **marimo's own config file is composed by `src/kedge/marimo_config.py` alone.** kedge writes a
   `.marimo.toml` into the directory it launches marimo from, because marimo's config search
   reads that directory before the user's home, and that is what turns marimo's built-in AI
   assistant off — an assistant that would otherwise send workbook values to an endpoint outside
   kedge's tool surface and outside the outbound payload log (PLAN 2.3). The filename and the
   keys inside it are marimo's, not kedge's, so a release renaming either should cost one file:
   same argument as 6, one layer down. The realistic second home is not a rival module but a
   one-line special case in something that already walks the project directory — `handover.py`
   and `server/hub.py` both do — which is exactly the mention a rename would strand. Enforced by
   `scripts/guardrails.py`, as a live string literal rather than an import, because the whole
   coupling *is* the string.

## Python style

These follow the author's sibling projects (`mooring`, `curfew`, `regcite`, `rwa_calculator`).
Where this file and a sibling repo disagree, this file wins — but the default is to match them.

- Python 3.12+. `X | None`, never `Optional[X]`. `list[str]`, never `List[str]`.
  The sibling repos contain literally zero uses of `Optional`.
- `from __future__ import annotations` at the top of every module.
- Type-annotate every public function signature. Internal helpers: annotate where non-obvious.
- **Type checker is `ty` (Astral), not mypy.** `uv run ty check src/`.
- Prefer `pathlib.Path` over `os.path` (ruff `PTH` enforces this).
- Prefer functions over classes when a function will do.
- Prefer early returns to reduce nesting. No dead code — delete it, don't comment it out.
- **British spelling** in comments, docstrings and user-facing output: "behaviour",
  "normalise", "minimised", "initialise". **No emoji anywhere** — code, commits, docs, output.
- Docstrings: public functions get them, private helpers usually don't. Labelled-section
  dialect where a function has real parameters — a prose line, blank, then `Args:` /
  `Returns:` / `References:`. Class docstrings carry a runnable `Example:`.
- Private helpers prefixed `_`. Modules export via explicit `__all__` where the surface matters.
  `src/kedge/__init__.py` re-exports the public API and its docstring names what is public
  versus internal.

## Logging

A maintained contract in the sibling repos, inherited wholesale:

- Module-level `logger = logging.getLogger(__name__)` after imports.
- **Lazy `%`-formatting**: `logger.info("loaded %d regions", n)` — never an f-string inside a
  log call. Enforced by ruff `G`.
- **No `print()` in library code** — ruff `T20`. Permitted only in `cli.py`, `tests/`, and
  `scripts/`. `rich` is for CLI output and report rendering only.
- **No `logging.basicConfig()`.** Handler setup happens once, at the entry point, idempotent,
  attached only to the `kedge` namespace logger.
- INFO for stage entry/exit, DEBUG for branch decisions, WARNING for fallbacks, ERROR only for
  genuinely unexpected exceptions.

## Models

- The house default is **`@dataclass(frozen=True, slots=True)`**. Use it for internal
  structures that never cross a boundary.
- **pydantic v2 only where it earns its place**: config validation (good error messages naming
  the file and key), and schemas serialised to or from the LLM, where the JSON Schema export
  and the retry-on-mismatch behaviour are the whole point. Do not reach for it by reflex.
- `src/kedge/analysis/model.py` is **the contract**. Changing it is a cross-cutting change —
  say so loudly rather than editing it quietly.

## The `.xl` namespace is a façade

`rwa_calculator` registered `col.irb.*`, then retired it and now **bans**
`@pl.api.register_*_namespace` outright — it accumulated dead methods and forced six `ty`
rules off project-wide. kedge still wants `col("x").xl.round(2)` because §2.6 makes
greppability the point, so the shape is a compromise:

- Real logic lives in plain typed module functions — `round_half_away(expr, digits) -> pl.Expr`
  — importable, testable, composable via `.pipe(fn, ...)`.
- `xl/namespace.py` registers `.xl` and every method is a one-line delegation. The class holds
  only `self._expr`. No logic, no state, no config capture.
- Deleting `namespace.py` must therefore be a clean removal, never a rewrite.

## Errors

- Define exceptions in the module that raises them; inherit from `kedge.errors.KedgeError`.
- Error messages state what was being attempted, what was found, and what the user can do.
  `"could not decode DataMashup in customXml/item3.xml: not a zip archive"` — not `"parse error"`.
- Never swallow an exception without either logging it or turning it into a `Finding`.

## Tests

- `tests/unit/` — pure, fast, no I/O beyond fixtures. The default.
- `tests/corpus/` — runs the analyser over `tests/fixtures/*.xlsx`. Marked `@pytest.mark.corpus`.
- `tests/contract/` — needs a live marimo kernel. Marked `@pytest.mark.contract`. These must be
  skippable and must clean up their subprocess even on failure.
- LLM-dependent tests marked `@pytest.mark.llm` and skipped unless a model endpoint is configured.
- Test names describe the behaviour: `test_round_half_away_from_zero_matches_excel`, not
  `test_round_2`.
- **`kedge.xl` is tested against a table of known Excel outputs**, values transcribed from
  Excel's actual behaviour. That table is the tripwire for a polars version bump.

## Layout

- `src/` layout. Import as `from kedge.analysis.model import WorkbookAnalysis`.
- One module, one job. If a module exceeds ~400 lines, it is probably two modules.
- `src/kedge/errors.py` holds the exception hierarchy.
- No module-level mutable global state. Anything process-wide hangs off `Workspace` (PLAN §2.9).

## Config & secrets

- Layered: `~/.kedge/config.toml` overridden by `./kedge.toml`. Loaded once into a pydantic
  settings object owned by `Workspace`.
- **API keys live in the OS keyring**, never in config files, never in env vars committed
  anywhere, never in a log line. Config stores `api_key_ref` — a keyring entry name.
- The server binds `127.0.0.1` only. No CORS config, no accounts, no cookies.

## Commands

```bash
uv run pytest                     # unit + corpus
uv run pytest -m contract         # live-kernel tests (slow, spawns marimo)
uv run ruff check --fix .
uv run ruff format .
uv run kedge --help
```

Before you hand work back: `uv run ruff check .` and `uv run ruff format --check .` clean, and
`uv run pytest` green. If something is genuinely blocked, say so explicitly — do not leave a
failing test unmentioned or a `pass` stub presented as finished.
