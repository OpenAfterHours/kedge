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

**The outbound payload log** records what left, at `~/.kedge/logs/outbound-<session>.jsonl`: one
JSON line per value-returning tool call carrying timestamp, session, turn, tool, sheet, column
names, row count, byte count, whether it was truncated and how many columns were redacted. It does
not carry values, and that is structural rather than a matter of remembering — `OutboundRecord`
has a fixed set of scalar fields, every one a name or a count, and there is no field a cell value
could travel in. Column names are the one judgement call: they are metadata, capped at 64 names of
64 characters, and a log that cannot say which columns went out answers none of the questions it
exists to answer.

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
the model rather than to the kernel. The policy stage refuses shell execution (`subprocess`, `pty`
and friends), refuses network access unless a hostname is explicitly allowlisted (the allowlist is
empty by default), and refuses writes outside the working directory.

**This is a quality gate, not a sandbox.** It is static analysis over an AST, it is deliberately
approximate in the direction of false passes rather than false rejections, and the kernel it feeds
runs with your privileges. If you point kedge at a model endpoint you do not trust, you are running
that endpoint's code on your machine. Treat the model as a fast, well-read colleague with commit
access, and read the notebook.

Workbooks are read as data and never executed. `.xlsx` and `.xlsm` are the readable formats;
`.xlsb` and `.xls` are refused with a finding rather than half-parsed. A macro-enabled workbook is
still only read — `has_vba` is recorded as a fact in the analysis, `xl/vbaProject.bin` is never
opened, and no macro is ever run.

## Dependencies

`marimo` and `polars` are pinned exactly, for reasons that are about correctness rather than
security (PLAN 6.1, 6.2) but have the same effect: nothing moves under kedge without a pull
request. `.github/dependabot.yml` raises those bumps weekly so a pin cannot quietly go stale, and
neither is ever auto-merged.
