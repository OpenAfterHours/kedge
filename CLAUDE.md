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
| `CONTRIBUTING.md` | The short version for a human arriving from GitHub. Points back at this file and `CONVENTIONS.md`. |
| `docs/analyser-worked-example.md` | Real `kedge inspect` output over two fixture workbooks, annotated. The fastest way to see what the analyser actually produces. |
| `docs/ty-diagnostics.md` | Every outstanding `ty` diagnostic, why it is there, and what would clear it. |
| `SECURITY.md` | The actual trust boundary: loopback, no auth, machine-level. Read before changing anything that binds a socket or logs a payload. |

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
7. **marimo's HTTP API is spoken by `src/kedge/marimo_http.py` alone.** `notebook/kernel.py` is
   the one deliberate exception — async SSE streaming of `POST /api/kernel/execute` is a
   different shape — and even it imports the path and headers from `marimo_http`. Also enforced
   by `scripts/guardrails.py`. (CONVENTIONS.md numbers this one 6; it has no reconciliation
   entry.)
8. **Certificate trust for the model endpoint is decided in `src/kedge/tls.py` alone.** It is the
   only thing kedge speaks TLS to. Build clients with `tls.client()` / `tls.async_client()` and
   always hand the `openai` SDK an explicit `http_client=` -- left alone it verifies against
   `certifi`, which never holds the root a corporate TLS-inspecting proxy re-signs with. There is
   no setting that turns verification off; `[model] ca_bundle` names a PEM instead. Also enforced
   by `scripts/guardrails.py`. (CONVENTIONS.md numbers this one 7.)

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
uv run python scripts/guardrails.py        # non-negotiables 1, 2, 7 and 8, by AST not by grep
uv run python scripts/version.py v0.2.0    # does that tag match __version__?
uv run python scripts/release.py 0.2.0     # bump, gate, tag, push -- the whole release

uv run python tests/fixtures/generate.py   # regenerate the fixture corpus
uv run kedge --help
```

CI runs all of the above on Ubuntu and Windows across 3.12 and 3.13 (`.github/workflows/ci.yml`),
including the contract tests. `ty` is advisory there until its three known diagnostics are cleared
(`docs/ty-diagnostics.md`). One matrix leg — Ubuntu, 3.12 — also measures coverage against the
`fail_under` in `pyproject.toml`.

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
- The `openai` SDK translates transport errors around the **request only**. With `stream=True` it
  has already returned by the time the body is drained, so a stall mid-answer surfaces as a raw
  `httpx.ReadTimeout` — carrying an empty message, since httpx maps it from a bare `TimeoutError`.
  `OpenAIClient.stream` catches it. Note also that `[model] timeout_seconds` is httpx's gap
  *between reads*, not a budget for the whole answer: an endpoint that goes quiet while a
  reasoning model thinks trips it while working perfectly.
- Anything reaching the agent loop's catch-all is reported as **unrecoverable**, and `app.js`
  renders that as "Fatal". So every model-endpoint failure must leave `OpenAIClient.stream` as an
  `AgentError` — `stream` translates transport errors *and* `APIStatusError`, which is where a
  429, a 5xx and a refusal the dialect negotiation declined all land. Quote the endpoint's prose
  from `body["error"]["message"]`, never the SDK's `message`: that one is
  `"Error code: 429 - {repr of the whole body}"`.
- A turn is up to `[agent] max_steps` completions and **every one re-sends the whole prompt** —
  around 8,000 tokens of system prompt, tool schemas and pinned blocks before the conversation.
  Two consequences. `_window_for` pins **least volatile first** (analysis, plan, registry, state)
  because a prompt cache keys on the prefix, so anything ahead of a block that changes stays
  cached and anything behind it does not; reordering that list is a silent cost regression. And
  `_Meter` accumulates **per step** — a single step's figure understates a turn by up to
  `max_steps`.
- **A carried turn is re-dated, and its age must not be.** `ConversationWindow.resume` moves a
  carried message into the turn it is resumed as — it has to, or it sorts behind history that is
  older than it — and `KedgeAgent._carry` now hands a turn's tool traffic on whether or not the
  turn answered. Read a message's age off `message.turn` after that and it is one turn old for
  ever: `_age_out_tool_results` never fires, the span grows by a turn every turn, and the
  mechanism meant to bound the window is the thing the carry disarms. Age is
  `ContextMessage.age_at`, which adds the `carried_age` that `suspend()` stamps on the way out.
  `turn` measures position; only `carried_age` measures time.
- **The carried span sits on a moving boundary, and moving it every turn is a cost regression.**
  The prompt is `head | flattened history | carried span | this message`. A leg that leaves the
  span comes back in history *flattened* — one assistant message holding the answer, where the span
  held interim prose, tool calls and results — so the moment that boundary moves, the new prompt
  stops matching the old one at the first carried message and the whole span is re-sent uncached.
  Measured at the default horizon against a 9k head: **1,408 uncached tokens a turn** to carry
  payloads worth a few hundred. `ConversationWindow._recut` therefore stubs and drops legs together
  on one turn in `CARRY_BLOCK_TURNS`, leaving the other turns append-only; the same measurement
  becomes 1,292 / 254 alternating, a mean of 773. The re-cut turn's cost is irreducible — the leg
  has to cross to history eventually and it is a different shape there. Anything that stubs or
  drops one leg per turn puts the per-turn cost back, and stamping the stub on the *sending* side
  is what keeps `_age_out_tool_results` from doing exactly that on arrival.
- **A count of messages is not a bound on a cost.** One `sample_data` payload at the 32KB cap is
  some fifteen thousand tokens, so `MAX_CARRIED_MESSAGES` of them is twenty times a 128k context.
  `CARRY_BUDGET_SHARE` bounds the span in characters as well, and it is measured on the raw
  payloads rather than on what they render as, because the same bound has to hold on what a session
  *holds* — the alternative is six megabytes per session and a carry `fit()` throws away in full at
  the far end. `[context] max_tool_result_tokens` is not this knob: it is a per-result truncation
  ceiling, and it is read nowhere in `src/`.
- Chat completions reports token usage only if asked (`stream_options={"include_usage": True}`,
  dropped from the ladder in `_recover` if the endpoint refuses it); responses reports it
  unprompted on `response.completed`. Prefer it over `TokenCounter`, which is an estimate over a
  fixed `cl100k_base` and cannot see `cached_tokens` at all.
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
