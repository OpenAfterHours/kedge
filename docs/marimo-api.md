# marimo bridge — empirically verified reference

Verified against **marimo 0.23.15** on Windows 11 / Python 3.12 by launching real servers and
capturing real responses. Everything here was observed, not recalled.

**This document supersedes PLAN.md §1.1–§1.3 wherever they disagree.** The corrections in §7
are the ones that change the design.

Re-run the verification on every marimo bump. `tests/contract/test_driver_live.py` is the
automated form of this document — 40 tests that spawn a real headless server, exercise every
driver method against it, and assert on real results. Run them with `uv run pytest -m contract`
(~50s). **A marimo bump does not merge until they pass.** They are excluded from the default
suite, so a green `uv run pytest` says nothing about the bridge.

---

## 1. `_code_mode` entry point

```python
import marimo._code_mode as cm

async with cm.get_context() as ctx:
    cid = ctx.create_cell("x = 1", name="my_cell", hide_code=False)
    ctx.run_cell(cid)
```

- The class is **`AsyncCodeModeContext`**. There is no `CodeModeContext`, and no module-level
  singleton — `cm` is just the conventional alias for the module.
- `__all__ == ['AsyncCodeModeContext', 'CellStatusType', 'NotebookCell', 'StaleCellError', 'get_context']`
- `get_context(*, skip_validation: bool = False, skip_staleness_check: bool = False)`
- Only works inside a kernel driven through the execute endpoint. Outside, it raises
  `RuntimeError("code mode requires a running kernel context")`.

Its own docstring:

> **Internal, agent-only API.** Not part of marimo's public API. No versioning guarantees.
> May change or be removed without notice.

## 2. Verified signatures

```python
# properties
graph    -> DirectedGraph
globals  -> dict[str, Any]
cells    -> _CellsView
packages -> Packages

# SYNC methods — queue an op; require `async with`
create_cell(code: str, *, before=None, after=None, hide_code=True, disabled=False,
            column=None, name=None) -> CellId_t
edit_cell(target: str, code: str | None = None, *, hide_code=None, disabled=None,
          column=None, name=None) -> None
delete_cell(target: str) -> None
move_cell(target: str, *, before=None, after=None) -> None
run_cell(target: str) -> None
set_ui_value(element, value) -> None
find_cell_defining_object(obj) -> CellId_t | None
enqueue_command(command) -> None

# IMMEDIATE — not queued, fires straight away
broadcast_raw_notification(notification) -> None

# ASYNC
await screenshot(target=None, *, timeout_ms=30000, as_data_url=False, save_to=None)
await close_screenshot_session()
await execute_command(command)
```

`ctx.cells[key]` accepts **int index (negative allowed) | cell id | cell name**.
`NotebookCell` exposes `id, code, name, config, status, errors, output, console_outputs`.
`CellStatusType`: `idle, exception, stale, cancelled, interrupted, marimo-error, disabled,
queued, running`.

Cell ids are short opaque tokens (`Hbol`, `UMIT`) — not UUIDs. Cells loaded from disk as
`def _():` report `name == "_"`, **not** `""`, despite the docstring saying otherwise.

## 3. Queue semantics — confirmed

`async with` is mandatory. Calling a mutator outside the block raises:

```
RuntimeError: Cell operations require 'async with'. ...
Without 'async with', operations are silently lost.
```

Flush order on **clean** exit: packages → `_validate_ops` → dry-run compile → structural ops →
queued `run_cell` batch → UI updates → a printed stdout summary.

On **exception**, the queue is discarded — verified: a `create_cell` followed by a `raise` left
no cell behind.

The flush prints a summary to **stdout**, e.g. `created and ran cell 'UMIT' (kedge_cell)`.
This arrives as an `event: stdout` in the HTTP response and the driver must expect and filter it.

## 4. Two sharp edges the plan does not mention

### 4.1 `StaleCellError`

`edit_cell(target, code=...)` raises `StaleCellError` unless the agent has read that cell at its
current version:

```
Cell 'Hbol' was modified since the agent last read it.
Read it first (e.g. `ctx.cells['Hbol'].code`) before editing.
To override and overwrite without re-reading, pass skip_staleness_check=True to cm.get_context().
Other stale cells: MJUe, vblA.
```

Reads are recorded by touching `NotebookCell.code` — **including implicitly via
`repr(ctx.cells)`**, because `_CellsView.__repr__` reads every cell's code. A driver that logs
`repr(ctx.cells)` silently disarms the staleness guard for the whole notebook. Do not log it.

This is a feature for kedge, not an obstacle: it is exactly the "user edited a cell between
turns" protection PLAN §6.2 asks for. Read before editing, and let the error propagate as a
retryable tool result rather than suppressing it with `skip_staleness_check`.

**Narrower than it first appears** (established by the live contract suite, and worth knowing
before you write a test that expects it to fire). `_apply_ops` records a read for every cell the
agent wrote in that batch — its effective view *is* the post-write version — and the check is
skipped entirely when the target's existing code is empty or the cell was added in the same
batch. So `StaleCellError` **never fires for a cell kedge itself created or edited this
session**. It protects only cells kedge has not written: ones already on disk, or ones the user
has since changed. That is still precisely the protection PLAN §6.2 wants, but it means any test
of the guard must operate on cells seeded on disk by a separate server.

### 4.3 Code-mode mutations are auto-saved to disk by the server

Not mentioned in PLAN.md at all, and load-bearing for anything that asserts on the notebook file.
`_session/extensions/extensions.py::_maybe_autosave` intercepts transactions whose `source` is
`"code-mode"` and rewrites the `.py` on a worker thread. Edit mode only, named notebooks only,
and **best-effort** — a failure becomes an `AlertNotification` that the driver never sees.

Two consequences:

- Asserting that `hide_code=False` really renders as a bare `@app.cell` on disk is possible at
  all *because* of this.
- The write is asynchronous, so a read immediately after the call can catch the old file. Poll
  for the expected content rather than reading once.

### 4.2 Validation is enforced kernel-side

The single-definition rule is enforced at flush:

```
RuntimeError: Multiply-defined names:
  - 'seed_value' is already defined in cell 'MJUe' (_)
To skip validation, use: async with cm.get_context(skip_validation=True) as ctx
```

kedge's own validation gate should catch this *before* submitting, so the model gets a clean
violation list rather than a kernel traceback.

## 5. HTTP kernel API

### 5.1 Route

`POST {base}/api/kernel/execute` — declared in **`_server/api/endpoints/execution.py`**
(not `execute.py`), mounted at prefix `/api/kernel`, excluded from the OpenAPI schema.

Headers:
- `Marimo-Session-Id: <session-id>` — the session **must already exist** (see §5.3)
- Auth, any one of: `Authorization: Bearer <token>`, `?access_token=<token>`, or the
  `session_<port>` cookie
- `Content-Type: application/json`
- **Not** required: `Marimo-Server-Token`. `/api/kernel/execute` is explicitly exempt from skew
  protection. Every *other* POST (`/run`, `/shutdown`, `/instantiate`) does require it.

Body: `{"code": "..."}`

### 5.2 SSE response — exact shapes

```
event: stdout
data: {"data": "<text>"}

event: stderr
data: {"data": "<text>"}

event: done
data: {"success": true, "output": {"mimetype": "text/plain", "data": ""}}
```

Only three event names. **There is no `error` event** — tracebacks arrive as `stderr` and
`done.success` goes `false`. `done.output` carries the scratch cell's rendered value on success.
Multi-line payloads are split into one `data:` line each, so a parser must rejoin `data:` lines
with `\n`. Client disconnect interrupts the kernel.

### 5.3 Sessions are NOT auto-created — the biggest integration gotcha

A freshly launched `marimo edit` has **zero sessions**, and `/api/kernel/execute` returns 500
until one exists. A session is created only when a transport connects with a caller-chosen
`session_id`.

**Preferred bootstrap (0.23.15+, plain HTTP, no WebSocket client):**

```
GET {base}/sse?session_id=<our-id>
```

Hold it open ~2 seconds, then close. **The session survives the stream closing** — in edit mode
without `--session-ttl` there is no TTL close, so a one-shot request is a sufficient bootstrap.
`file=` is optional when the server serves a single notebook.

WebSocket `ws://{host}:{port}/ws?session_id=<id>&access_token=<token>` also works and is the
only option on < 0.23.15.

**Takeover caveat:** edit mode holds at most one session. Bootstrapping a *second* `session_id`
evicts the first, and the old id then 500s. Pick one stable session id and reuse it.

### 5.4 Health and sessions

- `GET /health` and `/healthz` — **unauthenticated**, returns `{"status":"healthy"}`. This is the
  liveness check to use on Windows, where PID checks are unreliable.
- `GET /api/sessions` — requires edit auth. Shape `{session_id: {"filename": str|None, "path": str|None}}`,
  `{}` when no session exists.

### 5.5 Auth and framing

`TOKEN_QUERY_PARAM = "access_token"` confirmed in `_server/api/auth.py`. A successful
query-param or bearer auth also sets the `session_<port>` cookie.

`X-Frame-Options: DENY` appears **only** on `/auth/login`. The notebook page sets no framing
header and embeds fine. So framing
`http://127.0.0.1:<port>/?file=<nb>.py&access_token=<token>` never reaches the login page.

### 5.6 Clean shutdown

`POST /api/kernel/shutdown` is **not** exempt from skew protection. It needs
`Marimo-Server-Token`, which is served in the notebook page HTML as
`<marimo-server-token data-token="...">`. Fetch the page, scrape the token, then POST. Verified
to exit 0 and release the port.

## 6. CLI flags — all eleven claimed flags exist

`--headless`, `--port`, `--token-password`, `--base-url`, `--proxy`, `--allow-origins`,
`--watch`, `--session-ttl`, `--sandbox`, `--timeout`, `--no-token`.

- **`--timeout` is in MINUTES** (float): "shut down the server after N minutes of no connection".
- `--host` defaults to `127.0.0.1`.
- `--token-password-file` is an alternative to `--token-password` (path, or `-` for stdin) and is
  mutually exclusive with it. Worth preferring — it keeps the token out of the process table.

## 7. Corrections to PLAN.md, plainly

1. **`_server/api/endpoints/execute.py` does not exist** — the file is `execution.py`.
2. **A session is not auto-created.** `POST /api/kernel/execute` 500s until a transport has
   connected with our `session_id`. The lifecycle must bootstrap one via `GET /sse`. This is the
   single biggest gotcha and PLAN §1.2 omits it entirely.
3. **`GET /sse` is 0.23.15-only.** It does not exist in 0.23.9. Our exact pin is what makes the
   WebSocket-free bootstrap possible.
4. **Server-registry files are written ONLY for `--no-token` servers.** A `--token-password`
   server — which is what kedge launches — is invisible to `~/.marimo/servers`. The registry is
   therefore useless to us for our own process, which reinforces PLAN §2.9's "never
   auto-discover": the marker file is the only mechanism. The Windows path
   `~/.marimo/servers/<host>_<port>.json` is otherwise correct, fields
   `server_id, pid, host, port, base_url, started_at, version`, and it never contains a token.
5. `create_cell` defaults `hide_code=True` — confirmed. kedge must pass `hide_code=False`.
6. `broadcast_raw_notification` is **immediate, not queued**, unlike every other mutator.
7. `repr(ctx.cells)` silently records reads and defeats `StaleCellError`.
8. Cell ids are short opaque tokens, not UUIDs; unnamed disk cells report `name == "_"`.

## 8. Notification payloads

```python
from marimo._messaging.notification import BannerNotification, AlertNotification

BannerNotification(title: str, description: str,
                   variant: Literal["danger"] | None = None,
                   action: Literal["restart"] | None = None)
AlertNotification(title: str, description: str, variant: ...)
```

## 9. 0.23.9 → 0.23.15 diff of the surface we depend on

`_code_mode/__init__.py`, `_packages.py`, `_plan.py`, `screenshot.py` are byte-identical.
`_context.py` differs only in screenshot metadata constants — **every public signature is
unchanged**. `auth.py`, `server_registry.py`, `login.py`, `xdg.py` byte-identical.

New in 0.23.15 and relied upon: `GET /sse` session transport.
