# Releasing kedge

A release is a pushed tag. Everything else — tests on both platforms, the build, the PyPI upload,
the GitHub release — happens in `.github/workflows/release.yml` off the back of it.

## One-time setup

### PyPI trusted publishing

kedge uploads to PyPI over OIDC. There is no API token in this repository and there should never
be one. PyPI needs to be told, once, which workflow it is willing to accept an upload from.

Because `kedge` has not been published yet, this is a **pending publisher** — it becomes an
ordinary publisher the moment the first release lands.

1. Sign in at <https://pypi.org>, then go to **Account settings → Publishing**.
2. Under *Add a new pending publisher*, choose **GitHub** and fill in exactly:

   | Field | Value |
   |---|---|
   | PyPI project name | `kedge` |
   | Owner | `OpenAfterHours` |
   | Repository name | `kedge` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

3. Save. All five must match or the upload is rejected — the environment name in particular is
   easy to leave blank, and it is part of the claim.

### The `pypi` environment

The `publish` job runs in a GitHub environment named `pypi`. It exists so that the trusted
publishing claim can be pinned to it, and it is the natural place to add a required reviewer if
you ever want a human to approve uploads:

**Settings → Environments → pypi → Required reviewers.**

Leave it unprotected and a pushed tag publishes without further intervention, which is the
intended default.

## Cutting a release

Versions are PEP 440 and the single source of truth is `__version__` in `src/kedge/__init__.py`;
`pyproject.toml` declares the version dynamic and reads it from there. Bump one place.

```bash
uv run python scripts/release.py 0.2.0
```

That is the whole thing. `scripts/release.py` bumps `__version__`, runs every gate CI runs — in
the order that fails cheapest first — commits, tags, shows you exactly what is about to leave the
machine, and pushes once you say yes. The push is the trigger; everything after it is
`release.yml`.

Useful flags:

| Flag | What it does |
|---|---|
| `--dry-run` | Runs every check, prints the git commands it would have run, and leaves `__version__` exactly as it found it. |
| `--yes` | Skips the confirmation prompt. Without a terminal, the script stops before pushing rather than assuming consent. |
| `--skip-contract` | Skips the live-kernel tests. They spawn a real marimo and take a couple of minutes; CI still runs them on the tag, on both platforms. |
| `--skip-build-check` | Skips `uv build` and the `twine` metadata check, which need the network on first use. |

It refuses to start on a dirty tree, off `main`, behind `origin/main`, or onto a tag that already
exists locally or on the remote — the last one because PyPI will not accept a version twice, so a
tag that is already out there needs a decision rather than a retry. The one dirty file it
tolerates is `src/kedge/__init__.py` already bumped to the version you asked for and nothing else,
which is what a hand bump, or a run killed before it could tidy up, leaves behind.

If a gate fails, nothing is committed, tagged or pushed and `__version__` is put back the way it
was: a run that does not reach the release commit leaves the tree exactly as it found it. Fix the
cause and re-run. Past the release commit the bump belongs to a commit rather than to your working
tree, so a declined or rejected push is recovered with the git commands the error prints.

### By hand

The script exists because this sequence is easy to half-do, not because the sequence is secret:

```bash
# 1. Bump the version.
#    Edit src/kedge/__init__.py -> __version__ = "0.2.0"

# 2. Check the tag you are about to cut agrees with it.
uv run python scripts/version.py v0.2.0

# 3. Make sure the tree is green locally. CI will run all of this again, on both platforms.
uv run ruff check . && uv run ruff format --check .
uv run python scripts/guardrails.py
uv run pytest
uv run pytest -m contract

# 4. Commit and tag.
git commit -am "chore: release 0.2.0"
git tag -a v0.2.0 -m "kedge 0.2.0"
git push origin main
git push origin v0.2.0
```

The tag must be `v` followed by the version — `v0.2.0` for `0.2.0`. A mismatch fails the first
job in about five seconds, before anything is built or uploaded. Skipping step 1 and tagging
anyway is how `v0.0.2` came to sit on the remote pointing at a tree that still said `0.0.1`.

Pre-releases work the same way: `__version__ = "0.2.0rc1"`, tag `v0.2.0rc1`. The pipeline detects
the PEP 440 pre-release segment and marks the GitHub release accordingly, and pip will not install
it without `--pre`.

## What the pipeline does

Each stage gates the next, so nothing is uploaded until everything before it is green.

| Job | What it does |
|---|---|
| `verify` | Parses `__version__` and checks the tag matches. Fails fast, installs nothing. |
| `ci` | Calls `ci.yml` — lint, format, guardrails, `ty` (advisory), tests on Ubuntu and Windows across 3.12 and 3.13, contract tests against a live marimo on both platforms, and a build smoke test. |
| `build` | `uv build`, `twine check --strict`, and an assertion that the wheel and sdist filenames carry the tagged version. |
| `publish` | Uploads to PyPI via OIDC in the `pypi` environment. No stored credential. |
| `github-release` | Creates the GitHub release last, with generated notes and the artifacts attached. |

Publishing runs before the GitHub release deliberately: a release that exists is a release whose
artifacts reached PyPI.

## When something goes wrong

**The tag disagrees with the version.** The `verify` job fails and nothing else runs. Fix
`__version__`, commit, then move the tag:

```bash
git tag -d v0.2.0
git push origin :refs/tags/v0.2.0
git tag -a v0.2.0 -m "kedge 0.2.0"
git push origin v0.2.0
```

**CI fails on the tag.** Nothing has been published. Fix it on `main`, then move the tag as above.

**The PyPI upload fails.** No GitHub release is created, so the tag is still safe to re-run. Fix
the cause and re-run the failed jobs from the Actions tab. Common causes: the pending publisher
was never created, or one of its five fields does not match.

**The upload succeeded but a later job failed.** Do not re-run `publish` — that version is on
PyPI now and PyPI refuses to accept the same version twice, even after a delete. Create the
GitHub release by hand, or bump to a new patch version.

**A bad release reached PyPI.** Yank it rather than deleting it: yanking leaves existing pins
working while stopping new resolutions from picking it up. On the project's *Manage* page, choose
the release, then **Yank**. Then fix forward with a new version — a deleted version number can
never be reused.
