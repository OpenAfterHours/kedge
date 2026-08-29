# Security

kedge is a single-user, local tool. One person, one machine, no accounts, no deployment — that is
a design decision rather than a stage it is passing through (PLAN 2.9). The consequence is that
most of what a security policy usually covers does not exist here, and the part that does is
narrow enough to describe precisely. This document is that description, and it is deliberately
specific about where the boundaries are not.

## Supported versions

The latest release. kedge is pre-1.0 and there are no maintenance branches; a fix ships in the
next version rather than being backported.

## Reporting

Use [private vulnerability reporting](https://github.com/OpenAfterHours/kedge/security/advisories/new)
on the repository's Security tab. That opens a draft advisory only the maintainers can see, and it
is the right channel for anything you would rather not describe in public.

If that page is not available to you, open an ordinary
[issue](https://github.com/OpenAfterHours/kedge/issues) saying only that you have found something
and how to be reached — no detail — and wait to be asked.

There is no bounty and no response-time commitment. This is a small project with one maintainer,
and promising a 48-hour acknowledgement it cannot honour would be worse than saying so.

## What kedge actually is

Three processes on one machine:

- a FastAPI server bound to `127.0.0.1`, serving the chat pane;
- a `marimo edit --headless` process, also on loopback, held in an `<iframe>`;
- the marimo kernel, driven over HTTP from the server.

**The server has no authentication.** No accounts, no cookies, no CORS configuration, no CSRF
tokens. `kedge.server.app.require_loopback` refuses to bind anything but `127.0.0.1`, `localhost`
or `::1`, and raises with an explanation rather than warning and continuing — because no auth is
only defensible while nothing off the machine can reach it.

The boundary this draws is **the machine, not the user account**. Any process on the host can
open a loopback socket. On a shared or multi-user machine, another user's process can talk to a
running kedge server, and kedge does not attempt to stop it. Do not run kedge on a host you share
with people who should not see the workbook you are converting.

The two ports kedge does open — its own server and the marimo it spawns — are loopback and nothing
else, and there is no update channel that fetches and runs code.

## Credentials

**The model endpoint's API key lives in the OS keyring** — Windows Credential Manager,
Keychain, Secret Service — under the service name `kedge` and the entry named by
`model.api_key_ref`. It is never written to a config file, never read from an environment
variable, and never logged.

This is enforced rather than requested. `kedge.config` refuses to load a configuration file
containing a key that looks like a secret (`api_key`, `token`, `secret_key` and near-spellings),
naming the file and the offending key and printing the `keyring set` command that should have been
used instead. `kedge config` and `kedge doctor` report only whether the configured entry resolves —
set, not set, or the keyring itself unreachable — and never the value.

**That rule covers the files kedge loads. There is one file kedge writes that it cannot enforce
it on, and it sits beside the workbook.** To disable marimo's own AI assistant, kedge writes
`<workbook>.kedge/.marimo.toml` before each launch (`src/kedge/marimo_config.py`, and the section
below on what leaves the machine). That is marimo's *user* config, and marimo writes back to
whichever file its search found — so changing any setting from inside the editor makes marimo save
its whole configuration there. Its schema has `api_key` on five provider configs, plus
`completion.codeium_api_key` and two Bedrock credentials, all plaintext. A model key typed into
marimo's settings panel lands in that file. `ai.enabled = false` withdraws the panels that ask for
one, which makes it unlikely rather than impossible.

kedge does the two things it can here, and neither is prevention:

- **It preserves what it finds.** Silently deleting a key somebody deliberately entered is its own
  surprise, and a tool that quietly edits your credentials is not one you can reason about.
- **It reports it by name.** Every launch scans that file and lists any secret-shaped key on
  `AssistantLockdown.secret_keys` — dotted names such as `ai.open_ai.api_key`, never values, in
  the return value and in the log alike. `kedge.lifecycle.assistant_status` re-reads it on demand,
  because marimo can write a key into that file an hour after the launch that found it clean.

**Note where this file is.** Every other credential kedge touches lives under `~/.kedge` or in the
keyring, machine-scoped, specifically so it cannot reach a directory that might be a git
repository or a shared drive. This one is in the project directory next to the workbook, and it is
the only such place in kedge. It is a dotfile, so `kedge`'s own hand-over does not ship it and a
directory listing will not show it — which cuts both ways, because `git add .` does not care. If
the project directory is under version control or a syncing folder, put `.marimo.toml` in
`.gitignore`; kedge rewrites it on every launch and nothing is lost by not tracking it.

**The marimo token is generated per launch.** 32 bytes from `secrets.token_urlsafe`, fresh every
time kedge spawns a server, and it is the whole of the auth story between the kedge server and the
marimo kernel. Three things about it are worth knowing:

- It is passed to marimo as `--token-password-file`, not `--token-password`, because on Windows
  any process running as this user can read another process's command line. The file lives at
  `~/.kedge/run/<workspace>.token`, is created `0o600` where the platform honours that, and is
  removed on teardown.
- It reaches the browser as an `access_token` query parameter on the iframe URL. Query parameters
  end up in browser history; on loopback with a per-launch token that expires with the process
  this is an accepted cost, not an oversight.
- The marker file kedge writes so the *next* launch can clean up after a crashed one
  (`~/.kedge/markers/*.marker.json`) records the token alongside the PID and port. That file is
  machine-scoped, under the user profile, specifically so a credential never lands in a directory
  that might be a git repository or a shared drive. `kedge doctor` lists stale markers by filename
  and port; it does not print their tokens.

kedge never auto-discovers a marimo server and never attaches to one it did not start
(PLAN 2.9). The `~/.marimo/servers` registry is read for diagnostics only, and since marimo
records only `--no-token` servers there, a kedge-owned process never appears in it at all.

## What leaves the machine

Once a model endpoint is configured, kedge sends workbook structure and **workbook data** to it.
This is the deliberate design choice in PLAN 2.3 — in Excel the logic/data boundary genuinely
collapses, and a model that cannot see a rate table cannot translate the formula that reads it —
but it means the sensitivity of the workbook is the sensitivity of the conversation.

What is sent, and what limits it:

- **Profiles by default, values on request.** Per column: dtype, null and distinct counts,
  min/max/mean/sum, top-k values, and head, tail and random samples of five rows each.
- **Hard caps on every value-returning tool.** 100 rows and 32KB per payload by default, truncated
  with an explicit omission marker (`[sampling] max_rows`, `max_payload_bytes`). The model can ask
  for many small reads; it cannot ask for the sheet.
- **Column redaction, off by default.** Setting `[redaction] enabled = true` with
  `column_patterns` makes matching columns report dtype and null count while their values are
  hashed. Off by default because most workbooks do not need it, and one config line when they do.
- **Nothing at all in demo mode.** Until a key is stored, workbooks open against a scripted agent
  and no request is made. The analyser, the scaffold and the notebook all work with no endpoint
  configured.

**The outbound payload log** records what left *through a tool call*, at
`~/.kedge/logs/outbound-<session>.jsonl`: one JSON line per value-returning tool call carrying
timestamp, session, turn, tool, sheet, column names, row count, byte count, whether it was
truncated and how many columns were redacted. It does not carry values, and that is structural
rather than a matter of remembering — `OutboundRecord` has a fixed set of scalar fields, every one
a name or a count, and there is no field a cell value could travel in. Column names are the one
judgement call: they are metadata, capped at 64 names of 64 characters, and a log that cannot say
which columns went out answers none of the questions it exists to answer.

**It is not a complete account of what leaves the machine, and reading it as one would be a
mistake.** Three things are outside it. The first two are kedge requests that write no line; the
third is not a kedge request at all.

- **The log is per chat session.** It is opened against a session id when the agent loop builds
  that session's tool registry, so a request made outside a chat session writes no line at all.
  `kedge plan propose` is the case to know about: it speaks to the same endpoint through its own
  client and sends the workbook digest, the column profiles and the logical operations with their
  R1C1 formulas, and logs nothing anywhere. Its profiles carry **cell values**: the top five
  distinct values of each of up to 100 columns go out unconditionally, and it is only the head and
  tail rows that sit behind `include_sample_values`. Redacted columns contribute none of it.
- **The pinned system header is never a tool call.** Every completion of every turn re-sends the
  whole prompt, and pinned into it is the workbook analysis block: the sheet names, and for up to
  60 columns the header, dtype, row/null/distinct counts, min/max/sum and the first three values
  of the column, each truncated to 24 characters. Redacted columns contribute no values. None of
  this is a tool result, so none of it appears in the log, and it goes out again on every step of
  every turn rather than once.
- **marimo's own assistant is a second channel — controlled, not absent.** marimo ships an AI
  assistant of its own: a chat panel, cell-generation actions, inline completion. It is configured
  from the user's personal `~/.marimo.toml`, not from anything kedge owns, and points at whatever
  endpoint that names. It runs inside the `<iframe>` kedge serves, one click from the workbook
  data, and nothing it sends is a kedge tool call — so none of it meets the log, the sampling
  caps, or the redaction patterns. kedge switches it off by writing a `.marimo.toml` that marimo's
  config search finds ahead of the user's. Three limits qualify that, and none of them is
  theoretical: `ai.enabled` is a front-end gate, so marimo's `/api/ai/*` endpoints stay reachable
  to anything holding the marimo token; a `[tool.marimo]` section in the nearest `pyproject.toml`
  is merged *over* the file kedge writes and can re-enable the assistant; and because marimo saves
  its own settings back into whichever config its search found, that file is a plaintext location
  for a model API key, sitting beside the workbook. The next section is the detail.

So the log answers "which sampled payloads did the model ask for", accurately and per session. It
does not answer "what has this endpoint seen about this workbook" — for that, the honest answers
are the ones above and the design choice at the top of this section.

### How partially marimo's assistant is switched off

kedge writes `<workbook>.kedge/.marimo.toml` before each launch, forcing `ai.enabled = false` and
`completion.copilot = false`. marimo's config search starts at the process's working directory and
walks up, and kedge launches marimo from that directory, so those two settings are found ahead of
the user's own (`src/kedge/marimo_config.py`; verified against 0.23.15 by running marimo's
resolver, not by reading it). One module composes that file and nothing else names it, enforced by
`scripts/guardrails.py`, because the filename and the keys are marimo's rather than kedge's.

An unlogged second channel out of a tool pointed at finance data is not a rough edge; it is the
account above failing quietly. So the point of this section is not that the channel is closed. It
is **how narrowly**, stated plainly:

- **`ai.enabled` is a front-end gate.** It withdraws the AI panels and actions from the editor.
  It does not gate the server — marimo's `/api/ai/*` endpoints stay reachable to anything holding
  the marimo token, which against the boundary this document already draws (the machine, not the
  user account) is a real difference. What the setting buys is that the affordance is not on
  screen. It does not buy that the endpoint is sealed.
- **A nearer `pyproject.toml` outranks it.** `.marimo.toml` is marimo's *user* config, and marimo
  merges `[tool.marimo]` from the nearest `pyproject.toml` on top of it. A workbook converted from
  inside a Python project whose pyproject enables the assistant re-enables it, and nothing kedge
  writes into its own directory changes that. kedge detects that case and names the file rather
  than claiming a control it does not have.
- **A file kedge cannot write, or cannot read, leaves the assistant live.** A read-only
  `.marimo.toml` cannot be corrected. One that is locked or mid-sync is not overwritten *by
  choice*: kedge cannot merge a file it cannot read, so replacing it would discard the whole of
  the user's marimo configuration in order to assert two keys, and that is the worse trade.
  Either way kedge logs it at ERROR and reports it, rather than refusing to open the notebook
  over a file the user can fix in seconds.
- **It is also a new place a credential can sit.** See "Credentials" above: marimo saves its own
  settings into this file, and its schema has plaintext API keys.
- **It says nothing about what you type.** The gate is on marimo's assistant, not on you. A cell
  you write yourself is between you and marimo, the same boundary as the last paragraph of "Code
  the model writes".

`kedge.lifecycle.assistant_status` is where all of that surfaces: it re-reads the file and returns
whether the assistant is disabled, why not if it is not, and any credential the file has acquired.
Read it rather than assuming the launch succeeded.

One route back in is genuinely closed. A notebook's own PEP 723 header cannot re-enable the
assistant: marimo's script-config allowlist excludes `ai` and `completion`, so a header written by
the model has nothing to say on the subject.

The endpoint itself is yours to choose and yours to trust. kedge speaks to whatever
OpenAI-compatible `base_url` is configured, over TLS if you give it an `https` URL and not if you
do not.

## How that one TLS connection is verified

The model endpoint is the only thing kedge connects to over TLS. The marimo subprocess is plain
HTTP on loopback, and the analyser never opens a socket at all. So there is exactly one trust
decision here, and `src/kedge/tls.py` is the only module that makes it — enforced by
`scripts/guardrails.py`, which fails the build on an outbound client built anywhere else.

**kedge verifies against the operating system's trust store**, via `truststore`: SChannel on
Windows, Security.framework on macOS, OpenSSL's default paths on Linux, supplemented with
`certifi` so a stripped container with no distribution roots still has the public ones. This is
deliberately not Python's default. `httpx` and the `openai` SDK both verify against `certifi`
alone, which is a fixed list of public roots — so on a machine behind a TLS-inspecting proxy,
where the connection is re-signed by a corporate root that IT has already pushed to the OS store,
every model call fails with `unable to get local issuer certificate` and nothing in the message
suggests that the machine itself trusts the certificate and only Python disagrees. Reading the OS
store does not widen trust beyond what the machine's administrator has already established; it
narrows the gap between what the browser accepts and what kedge accepts.

**Where the root is not in the OS store**, `[model] ca_bundle` in config names a PEM to verify
against instead. `kedge doctor` reports which of the two is in force, and turns a certificate
failure into an explanation naming the likely cause and the command that identifies the signer.

**There is no setting that disables verification, and a pull request adding one will be
declined.** `verify=False` is the usual answer to this problem and it is the wrong one: it is
invisible in a config file six months later, it applies to every future connection rather than
the one that was failing, and on the interception proxies this exists for it discards the
proxy's own guarantees along with everybody else's. `ca_bundle` says the same thing explicitly,
narrowly, and in a form a reviewer can see. Nothing in kedge connects without verifying —
including the diagnostics, which is why `doctor` hands you an `openssl s_client` command to read
the issuer rather than completing an unverified handshake to read it for you.

## Code the model writes

The agent generates polars code and kedge runs it in the marimo kernel. Every cell passes the
validation gate in `kedge.agent.validate` first — syntax, then the marimo single-definition
contract against the live graph, then policy, then output style — and a rejected cell goes back to
the model rather than to the kernel. The policy stage refuses five things:

- **Shell execution.** `subprocess`, `pty` and friends, by import and by call, plus the calls that
  delete files.
- **Network access**, unless the hostname in the cell is listed in `[policy] network_allowlist`.
  Recognised by import: the HTTP and socket clients (`requests`, `httpx`, `urllib`, `socket`,
  `paramiko` and the rest of that list).
- **Database access**, unless what the connection names is listed in `[policy]
  database_allowlist`. Recognised three ways, because one is not enough: by import, for the
  drivers that exist only to connect (`pyodbc`, `psycopg`, `pymssql`, `snowflake`, `connectorx`,
  `oracledb`, `asyncpg`, the `adbc_driver_*` family and so on); by call, for `pl.read_database`,
  `pl.read_database_uri`, `DataFrame.write_database` and `sqlalchemy.create_engine`, which import
  nothing suspicious — a cell that reads a warehouse through polars imports only polars, and the
  import-shaped check above cannot see it; and by literal, for a string that *is* a connection
  string naming a server, which is what an unlisted driver still leaves behind.
- **Writes outside the working directory.** A relative path is fine; an absolute one has to sit
  under the project directory.
- **A string literal that looks like a credential** — including a connection string with an
  embedded password, in either the `connection_string = "mssql+pyodbc://user:pass@host"` spelling
  or the same URI handed straight to a call. This one rejects the cell exactly as the other four
  do, and it is reported first when a DSN trips both it and an allowlist.

**Both allowlists are empty by default**, so out of the box no recognised driver or entry point in
a generated cell reaches the network or a database. `[policy]` in `~/.kedge/config.toml` or in the
`kedge.toml` beside the workbook is where you widen that, and the two lists are separate on
purpose: `network_allowlist` holds hostnames, matched against the host in an `http(s)://` literal,
while a database connection frequently names no host at all — an ODBC `DSN=RiskWarehouse` entry, a
Snowflake account locator, or a DSN assembled at run time, in which case what there is to permit
is the driver (`pyodbc`) or the entry point (`read_database`, `write_database`, `create_engine`).
The last of those is not a nicety: marimo's single-definition rule puts the engine in one cell and
the read in the next, and the reading cell contains no host and no import to name. A refusal names
what it found and which entry would permit it.

**Each connection is judged on its own targets**, not on the cell's. A permitted warehouse named
anywhere in a cell used to excuse every other connection beside it, which turned widening the list
— the only reason it exists — into a way of disarming the check.

**Neither list is unioned across config layers.** The `kedge.toml` beside the workbook replaces
whatever `~/.kedge/config.toml` set, key by key. That file often travels with the workbook, so it
is the one setting by which a file arriving from elsewhere can loosen a security control without
saying so; `kedge config` prints which file each value came from.

**This is a quality gate, not a sandbox.** It is static analysis over an AST, it is deliberately
approximate in the direction of false passes rather than false rejections, and the kernel it feeds
runs with your privileges. Concretely, in the direction that matters here: it recognises names, so
a driver not on the list *and* not naming its server in a literal, a connection opened through a
wrapper it has not heard of, or a URL assembled from parts all go through. `duckdb` and
`sqlalchemy` are refused only where a call on them shows a connection — otherwise a duckdb query
over a local parquet file would be refused as database access — so `duckdb.connect("md:...")`
against MotherDuck and `duckdb.sql("select * from 'https://host/x.parquet'")` are both false
passes kedge accepts knowingly. On the network side the hosts are still read cell-wide rather than
per call, because a URL is routinely composed from a base and a path and refusing that would be a
false rejection; only a network call naming no literal at all is refused outright, so
`httpx.get(f"{BASE}/latest")` is permitted by an allowlisted `BASE` while `httpx.get(build_url())`
is not. And the gate sees the cells *kedge* submits: a cell you type into the notebook yourself is
between you and marimo.

If you point kedge at a model endpoint you do not trust, you are running that endpoint's code on
your machine. Treat the model as a fast, well-read colleague with commit access, and read the
notebook.

Workbooks are read as data and never executed. `.xlsx` and `.xlsm` are the readable formats;
`.xlsb` and `.xls` are refused with a finding rather than half-parsed. A macro-enabled workbook is
still only read — `has_vba` is recorded as a fact in the analysis, `xl/vbaProject.bin` is never
opened, and no macro is ever run.

## Dependencies

`marimo` and `polars` are pinned exactly, for reasons that are about correctness rather than
security (PLAN 6.1, 6.2) but have the same effect: nothing moves under kedge without a pull
request. `.github/dependabot.yml` raises those bumps weekly so a pin cannot quietly go stale, and
neither is ever auto-merged.
