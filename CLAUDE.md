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
| `evals/README.md` | The evals: a workbook plus a rubric, declaring what kedge should *do* rather than what the analyser should *find*. Read it before confusing one with `tests/fixtures`. |
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
   **SQL literals go through `kedge.sql` for the same reason**, and it is the same class of bug:
   an apostrophe in a counterparty name, a null, a date, a money value at the edge of exponent
   notation. Every one is ordinary in a finance extract and every one breaks a statement built
   by concatenation -- at the moment somebody pastes it into a production client. Unlike 1, 2, 7
   and 8 this is *not* machine-enforced: no AST pass can tell SQL-shaped string concatenation
   from any other, so it holds only as long as reviewers hold it.
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
uv run python evals/adjustment_signoff/build_workbook.py   # regenerate the eval workbook
uv run python evals/run.py adjustment_signoff              # grade the reference conversion
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
- A turn is up to `[agent] max_steps` completions, **plus one**: a turn stopped by a retry cap gets
  a last word with the tools withheld (`KedgeAgent._final_word`), or it ends having said nothing at
  all. **Every one re-sends the whole prompt** — around 8,000 tokens of system prompt, tool schemas
  and pinned blocks before the conversation — except the last word, which re-sends everything but
  the schemas and is therefore the one step billed entirely uncached, since dropping them changes
  the prompt at byte zero. Two consequences. `_window_for` pins **least volatile first** (analysis,
  plan, registry, state) because a prompt cache keys on the prefix, so anything ahead of a block
  that changes stays cached and anything behind it does not; reordering that list is a silent cost
  regression. And `_Meter` accumulates **per step** — a single step's figure understates a turn by
  up to `max_steps + 1`.
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
- **Presence is not the absence of a null.** `reconcile.verify` compares a prediction against a
  re-extract with a full outer join, and the obvious way to tell "this row is missing" from "this
  row differs" is to look at whether a value column came back null. That is wrong on precisely
  the row the module exists for: a re-extract holding a legitimate `NULL` is *present and
  different*, and reading it as missing loses the break. Two explicit boolean marker columns are
  added before the join, named so they cannot collide with a hand-in's own columns. The eval's
  central case (`ACC-00001`, Excel's blank-as-zero against the warehouse's `NULL`) is a
  regression test for exactly this.
- **A paste is the one hand-in whose stored bytes are not the bytes that arrived.** Everywhere
  else the SHA-256 is a claim about a file the user still holds, so rewriting the payload would
  falsify it. A paste has no original, so `kedge.ingest.paste` normalises it to RFC 4180 CSV and
  the managed file becomes the artifact of record. It has to: `read_frame` picks its separator
  off the **extension**, so a tab-delimited paste stored under `.csv` reads as one column, and
  under `.tsv` every comma-delimited paste is stranded. Two consequences worth knowing — the same
  grid pasted as TSV and as CSV dedupes to one stored file, and the delimiter has to be resolved
  before the file is named.
- **A cell that only builds `mo.ui` elements reads nothing, so nothing can hide it.** marimo
  gates a cell on its dataflow edges; a selector that constructs widgets and references no
  upstream name has none, so it renders from the moment the notebook opens. In a runbook that
  means the re-extract box sits on screen before the UPDATE it is meant to follow — and a
  re-extract taken *before* the statement ran looks exactly like one taken after, with no way to
  tell afterwards. Such a cell must read the previous step's token (`_after_x = <token>`), and
  `_gate_map` in `scaffold.py` is what supplies it. The eval scored 38/38 while this was broken,
  because the harness executed cells linearly and had no way to express "should not have been
  visible"; `harness/drive.py:visible_cells` exists for that now.
- **In app mode an error renders as nothing at all.** Everything below a stopped or failed cell
  disappears, and `marimo run` hides the traceback, so a broken conversion and a conversion
  waiting patiently look identical: a page that just ends. Every `mo.stop` message therefore
  names its step (`Step 3 of 8`) and what to do — at that moment it is the entire user
  interface. This is why the first report of the typing bug below was "I was expecting the SQL
  to appear and I can't see it".
- **Excel copies what a cell *looks like*, not what it holds.** A column formatted `#,##0.00`
  reaches the clipboard as `364,422.95`, so a pasted grid arrives with `accrual_gbp` as `String`
  — and `empty_as_zero` was a *silent no-op* on it, because a paste has no nulls, it has empty
  strings. The failure surfaced four operations later as `arithmetic on dtypes str and dyn
  float`, inside a query plan, in a cell app mode drew as blank. Two fixes, and both matter:
  `read_data` now types unambiguously numeric text columns and reports each one on the `Layout`,
  and `empty_as_zero` casts strictly so a text column fails at *that* expression instead of
  three later. It refuses to coerce a leading zero or more than 15 significant digits — those
  are identifiers, and `00123` becoming `123.0` breaks every join it takes part in. Note polars'
  own CSV inference does that to `00123` before kedge sees it; that one is still open.
- **marimo's state dies with the kernel, and a runbook outlives it.** A process that says "run
  this update, then re-extract tomorrow" cannot keep its progress in widget values. `kedge.runs`
  writes one JSON file per run — hand-ins by hash, decisions with notes and times — so reopening
  resumes and the sign-off's audit line is derived rather than asserted. Starting fresh writes a
  *new* file beside the old one: a run record is evidence, and "start again" must never erase
  what was signed off last month.
- **A prompt is a hand-written copy of an enum, and prose does not go red.** `StageKind.HANDOFF`
  was added while `propose_system.md` went on listing four kinds, so the model could not propose
  a hand-off — it had never been given the word. `tests/unit/test_agent_prompts.py` now asserts
  every `StageKind`, `SourceOrigin` and `ExcelPattern` member appears in the prompt that offers
  it, the same way `test_every_tool_the_model_is_offered_is_described_in_the_prompt` already did
  for the tool list. It found a missing `aggregate` row on the first run.
- **A signal that is permanently amber is one people stop reading.** A conversion that
  deliberately improves on a workbook column -- rendering the UPDATEs through `kedge.sql`
  instead of copying the workbook's broken concatenated ones -- reproduces nothing for that
  region, so the report sat at `NOT RECONCILED` on every correct run, with the default message
  telling the reader to go and check that a cell ran. `reconcile_workbook(..., not_reproduced=
  {region_id: why})` is the fix: still `NOT_RECONCILED`, because nothing can make an unchecked
  region a pass, but reported as a decision with its reason, and the headline becomes `CHECKED
  WITH EXCEPTIONS` rather than the same amber as a genuine gap.
- **Reconciliation is evidence for a decision, so it goes before the decision.** It used to be
  the last cell in the file. In a runbook that is wrong twice: it depends only on the computing
  stages, so marimo renders it as soon as those finish -- which put a wall of reconciliation
  output directly beneath the sentence telling a blocked user what to type -- and it put the
  proof that the arithmetic matches the workbook *after* the approval it is proof for.
  `_with_reconciliation` now emits it straight after the last stage declaring any operations.
- **A blocking message must lead with the instruction.** "A reason is required, not optional --
  it is the whole improvement over somebody typing a number into Excel" tells a stuck user why
  the rule exists and never where to type. Instruction first, justification after; the eval
  asserts the message contains an actual verb.
- **Reconciliation is an acceptance test on the conversion, not a control on every run.** It
  asks whether the *translation* reproduces the workbook's own numbers, which is one question
  with one answer, measured against the data the spreadsheet holds. A converted notebook is then
  run monthly on *new* data, where the workbook is no baseline at all — so leaving the live
  comparison in the notebook reported FAILED on every run after the first, on runs where nothing
  was wrong, pointing at a spreadsheet nobody had opened since the process changed. The outcome
  is now recorded once (`kedge.reconcile.AcceptanceStore`, `<project>/reconciliation.json`) and
  cited afterwards, and the live check re-runs only when the hand-in digest matches the one the
  acceptance was measured on — the only case where a failure means somebody edited the notebook
  into disagreeing with the workbook. The panel names what *is* watching the run (the contract,
  the drift report, the checkpoints, the verification), because a bare citation reads as
  "nothing is being checked". `TranslationCheck.status` stays falsy for a citation; the question
  "is the translation accepted?" is `translation_accepted`, spelled differently on purpose.
- **The conversion used to throw away the only part nobody can reconstruct.** A workbook's
  Process Notes or Sign-off sheet — Purpose, Background, Known issues, written by somebody for
  exactly the person who opens the notebook eight months later — was extracted by the analyser
  into `WorkbookAnalysis.notes`, with the sheet and cells each note came from, and then dropped
  on the floor. `Briefing` on the plan is where it goes, and `_briefing_cell` renders it first,
  collapsed. Two halves with different reliability: the prose is model-extracted and **must cite
  its sources** (the validator refuses prose without them — invented background in a finance
  notebook is confident, plausible and unattributable, and an honest blank is a correct answer);
  the provenance below it is derived from the approved plan with no model involved. Unanswered
  open questions sit *outside* the accordion, because they qualify every number below them on
  every run and a caveat nobody opens is a caveat nobody has.
- Excel's `ROUND` collapses the operand to **15 significant decimal digits** before rounding
  half-away-from-zero. Missing this is a one-penny error that propagates.
- marimo's two file inputs return different things: `mo.ui.file` gives bytes with no path,
  `mo.ui.file_browser` gives a path with no bytes. An uploaded hand-in is **not reproducible**,
  which is why everything converges on a managed `HandIn` record.
- **A test that posts `json={}` is not testing the request the browser sends.** Every decision
  route declared its `DecisionBody` required and all twenty tests posted `json={}`, so the suite
  was green while every Approve button in the pane was a 422: `app.js` announced
  `Content-Type: application/json` on requests that carried no body. Request validation fails
  *before* the handler, so nothing was popped, nothing was written, the card sat there, and
  FastAPI's `detail` — a **list** of errors, where kedge's own `HTTPException` raises a string —
  reached `notice` as `[object Object]`. Hence three rules. A decision body is optional
  (`routes.NO_NOTE`), so a missing one costs a note rather than the click. `api()` sends the
  content type only when there is a body, as `hub.js` always did. And anything rendering a
  `detail` must handle both shapes, because the anonymous error is the one on the path where the
  user has done nothing wrong.

## Style, briefly

British spelling. No emoji anywhere — code, commits, docs, or output. `from __future__ import
annotations` at the top of every module. `X | None`, never `Optional`. Frozen dataclasses by
default; pydantic only where validation or JSON Schema genuinely earns it. Module-level
`logger = logging.getLogger(__name__)` with lazy `%` formatting. No `print()` outside `cli.py`,
`tests/` and `scripts/`. Conventional Commits.
