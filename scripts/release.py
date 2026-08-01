"""Cut a release end to end: bump, gate, commit, tag, push.

`RELEASING.md` is right that a release is a pushed tag. The cost sits in the half-dozen commands
in front of that push, because every one of them is a step you can skip by accident and only find
out about afterwards. Pushing `v0.0.2` while ``__version__`` still said ``0.0.1`` is the cheap
version of that mistake -- the `verify` job rejects it in five seconds -- and recovering still
means deleting a tag from the remote. The expensive version is a tag pushed over a red tree.

So this runs the whole sequence in the order that fails cheapest first, and only touches git once
everything before it is green::

    uv run python scripts/release.py 0.2.0             # the whole thing
    uv run python scripts/release.py 0.2.0 --dry-run   # everything except commit, tag and push
    uv run python scripts/release.py 0.2.0 --yes       # no confirmation prompt (for CI or cron)
    uv run python scripts/release.py 0.2.0 --skip-contract --skip-build-check   # when in a hurry

Nothing here uploads anything, and there is no credential to leak: the push *is* the trigger, and
`.github/workflows/release.yml` builds the artifacts and hands them to PyPI over OIDC. The last
thing this script does is print the URL of the run to watch.

It deliberately does not import `kedge` -- a release preflight should not need the package's
dependency tree installed and importable to tell you that a tag is wrong. `scripts/version.py`
stays the authority on what the version *is*; this script bumps the assignment and then asks that
script, as a subprocess, whether it agrees. Same check the pipeline runs, same code path.
"""

from __future__ import annotations

import argparse
import ast
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = REPO_ROOT / "src" / "kedge" / "__init__.py"
DIST = REPO_ROOT / "dist"

# Both are baked into the trusted-publishing claim on PyPI's side, so neither is a knob worth
# exposing: an upload from any other branch or remote would be rejected anyway.
RELEASE_BRANCH = "main"
REMOTE = "origin"

ACTIONS_URL = "https://github.com/OpenAfterHours/kedge/actions/workflows/release.yml"
PYPI_URL = "https://pypi.org/p/kedge"

# Enough PEP 440 to catch a typo -- a stray space, a leading `=`, `0.2` where `0.2.0` was meant --
# without reimplementing the specification or taking a dependency on `packaging` just to parse
# three numbers. Anything this accepts, hatchling and PyPI will accept too.
VERSION_PATTERN = re.compile(
    r"^\d+(\.\d+)*"  # release segment
    r"((a|b|rc)\d+)?"  # pre-release
    r"(\.post\d+)?"  # post-release
    r"(\.dev\d+)?$"  # development release
)


class ReleaseError(RuntimeError):
    """Something is wrong with the tree, the tooling, or the release itself.

    Local to this script rather than inheriting from `kedge.errors.KedgeError`: importing the
    package would pull in every runtime dependency, which is exactly what a preflight that has to
    work on a half-broken tree cannot rely on.
    """


@dataclass(frozen=True, slots=True)
class Gate:
    """One command that must pass before a tag is cut.

    Example:
        >>> Gate("ruff check", ("uv", "run", "ruff", "check", ".")).name
        'ruff check'
    """

    name: str
    argv: tuple[str, ...]
    advisory: bool = False


@cache
def _executable(name: str) -> str:
    """Resolve `name` on PATH, so a missing tool fails before anything is written.

    Args:
        name: The executable to find.

    Returns:
        Its absolute path.

    Raises:
        ReleaseError: It is not on PATH.
    """
    found = shutil.which(name)
    if found is None:
        raise ReleaseError(f"{name} is not on PATH; a release needs both git and uv")
    return found


def _capture(argv: Sequence[str], *, allow_failure: bool = False, raw: bool = False) -> str:
    """Run `argv` in the repo root and return its stdout, stripped.

    Args:
        argv: The command, with its executable name unresolved.
        allow_failure: Return an empty string on a non-zero exit rather than raising.
        raw: Return stdout untouched. Needed for ``git status --porcelain``, whose first column
            is a significant space that stripping would silently eat -- and eating it takes the
            first character of the path with it.

    Returns:
        Captured stdout, with surrounding whitespace removed unless `raw`.

    Raises:
        ReleaseError: The command failed and `allow_failure` is false.
    """
    resolved = (_executable(argv[0]), *argv[1:])
    result = subprocess.run(
        resolved, cwd=REPO_ROOT, capture_output=True, text=True, check=False, encoding="utf-8"
    )
    if result.returncode != 0:
        if allow_failure:
            return ""
        detail = (result.stderr or result.stdout).strip()
        raise ReleaseError(f"`{' '.join(argv)}` failed:\n  {detail}")
    stdout = result.stdout or ""
    return stdout if raw else stdout.strip()


def _stream(argv: Sequence[str]) -> int:
    """Run `argv` in the repo root with its output going straight to the terminal."""
    resolved = (_executable(argv[0]), *argv[1:])
    return subprocess.run(resolved, cwd=REPO_ROOT, check=False).returncode


# ── the version assignment ───────────────────────────────────────────────────────────────


def _find_assignment(source: str) -> tuple[int, str]:
    """Locate the literal ``__version__`` assignment in `source`.

    Parsed rather than matched by regex for the same reason `scripts/version.py` parses it: the
    string appears in `__all__` two lines below, and a regex that is careless about which line it
    rewrites is a regex that will one day rewrite the wrong one.

    Args:
        source: The full text of the module.

    Returns:
        The zero-based index of the assignment's line, and the version it currently holds.

    Raises:
        ReleaseError: There is no literal ``__version__ = "..."`` in the module.
    """
    tree = ast.parse(source, filename=str(VERSION_FILE))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "__version__" in targets and isinstance(node.value, ast.Constant):
            value = node.value.value
            if isinstance(value, str):
                return node.lineno - 1, value
    raise ReleaseError(f"no literal __version__ assignment in {_relative(VERSION_FILE)}")


def bump(source: str, version: str) -> tuple[str, str]:
    """Rewrite the ``__version__`` assignment in `source` to `version`.

    Only the one line changes, and its existing line ending is carried across verbatim -- a
    release commit that also flips the file from LF to CRLF is a release commit nobody can read.

    Args:
        source: The full text of the module, read without newline translation.
        version: The version to assign.

    Returns:
        The rewritten source, and the version that was there before.
    """
    index, current = _find_assignment(source)
    lines = source.splitlines(keepends=True)
    line = lines[index]
    ending = line[len(line.rstrip("\r\n")) :]
    lines[index] = f'__version__ = "{version}"{ending}'
    return "".join(lines), current


def _read_source() -> str:
    """The version module's text, with line endings left exactly as they are on disk."""
    with VERSION_FILE.open(encoding="utf-8", newline="") as handle:
        return handle.read()


def _write_source(source: str) -> None:
    """Write the version module's text back without translating line endings."""
    with VERSION_FILE.open("w", encoding="utf-8", newline="") as handle:
        handle.write(source)


# ── preflight ────────────────────────────────────────────────────────────────────────────


def _relative(path: Path) -> str:
    """A repo-relative, forward-slashed path, as git and the error messages both spell it."""
    return path.relative_to(REPO_ROOT).as_posix()


def changed_paths(porcelain: str) -> list[str]:
    """Every path named by ``git status --porcelain`` output.

    Args:
        porcelain: Raw v1 porcelain output, exactly as git wrote it. The two-character status
            field is column-aligned, so the leading space on a ` M path` line is load-bearing and
            the caller must not have stripped it.

    Returns:
        The paths, with rename arrows resolved to the destination and any quoting stripped.
    """
    paths: list[str] = []
    for line in porcelain.splitlines():
        if not line.strip():
            continue
        entry = line[3:]
        # `R  old -> new`: the destination is the one that is actually dirty.
        _, _, destination = entry.partition(" -> ")
        paths.append((destination or entry).strip().strip('"'))
    return paths


def _check_tree_is_clean(target: str) -> None:
    """Refuse to release from a dirty tree, tolerating a bump this script itself would make.

    The one permitted exception makes retrying survivable: a gate fails, you fix the cause, and
    re-running finds `src/kedge/__init__.py` already carrying the target version. Anything else
    uncommitted, including an untracked file, stops the release.

    Args:
        target: The version being released.

    Raises:
        ReleaseError: The tree holds changes that are not that bump.
    """
    porcelain = _capture(("git", "status", "--porcelain"), raw=True)
    if not porcelain.strip():
        return

    paths = changed_paths(porcelain)
    _, current = _find_assignment(_read_source())
    if paths == [_relative(VERSION_FILE)] and current == target:
        print(f"  tree holds only the {target} bump from an earlier run; carrying on")
        return

    listed = "\n".join(f"    {path}" for path in paths)
    raise ReleaseError(
        "the working tree is not clean. A release is cut from committed code only.\n"
        f"  uncommitted:\n{listed}\n"
        "  Commit or stash it, then re-run."
    )


def _check_branch() -> None:
    """Refuse to release from anywhere but the release branch."""
    branch = _capture(("git", "rev-parse", "--abbrev-ref", "HEAD"))
    if branch != RELEASE_BRANCH:
        raise ReleaseError(
            f"on branch {branch}, but releases are cut from {RELEASE_BRANCH}.\n"
            f"  The pipeline builds whatever the tag points at, so a tag off {branch} would "
            "publish unreviewed code."
        )


def _check_up_to_date() -> None:
    """Refuse to release when the local branch is behind or has diverged from the remote."""
    print(f"  fetching {REMOTE}")
    _capture(("git", "fetch", "--quiet", REMOTE, RELEASE_BRANCH, "--tags"), allow_failure=True)

    upstream = f"{REMOTE}/{RELEASE_BRANCH}"
    if not _capture(("git", "rev-parse", "--verify", "--quiet", upstream), allow_failure=True):
        print(f"  no {upstream} yet; skipping the up-to-date check")
        return

    counts = _capture(("git", "rev-list", "--left-right", "--count", f"{upstream}...HEAD"))
    behind, _, ahead = counts.partition("\t")
    if int(behind or 0) > 0:
        raise ReleaseError(
            f"HEAD is {behind} commit(s) behind {upstream} and {ahead.strip()} ahead.\n"
            f"  Pull and re-run, so the tag names a commit that includes what is already on "
            f"{upstream}."
        )


def _check_tag_is_free(tag: str) -> None:
    """Refuse to reuse a tag, locally or on the remote.

    PyPI will not accept a version twice even after a delete, so a tag already on the remote means
    either the release happened or it failed on its way there. Both need a decision from a human.

    Args:
        tag: The tag about to be cut.

    Raises:
        ReleaseError: The tag exists in either place.
    """
    # The remote first, and not only because it is the more serious of the two. `_check_up_to_date`
    # has just run `git fetch --tags`, which re-creates locally any tag you deleted locally while
    # it was still on the remote -- so checking locally first would answer "delete it locally",
    # which fetch would undo on the next run, for as many runs as you cared to try.
    if _capture(("git", "ls-remote", "--tags", REMOTE, f"refs/tags/{tag}"), allow_failure=True):
        raise ReleaseError(
            f"tag {tag} already exists on {REMOTE}.\n"
            f"  Check whether it published: {ACTIONS_URL}\n"
            f"  If the run failed before the upload, delete it from the remote and re-run:\n"
            f"    git push {REMOTE} :refs/tags/{tag}\n"
            f"    git tag -d {tag}\n"
            "  If it published, that version number is spent -- bump to the next one."
        )

    if _capture(("git", "tag", "--list", tag)):
        raise ReleaseError(
            f"tag {tag} exists locally but not on {REMOTE}, so a previous run stopped before "
            "pushing it.\n"
            f"  To finish that release:   git push {REMOTE} {RELEASE_BRANCH} && "
            f"git push {REMOTE} {tag}\n"
            f"  To abandon it and re-cut:  git tag -d {tag}"
        )


def preflight(version: str, tag: str) -> None:
    """Everything that can refuse a release before a byte is written."""
    print("preflight")
    _executable("git")
    _executable("uv")
    if not _capture(("git", "rev-parse", "--git-dir"), allow_failure=True):
        raise ReleaseError(f"{REPO_ROOT} is not a git repository")
    _check_branch()
    _check_tree_is_clean(version)
    _check_up_to_date()
    _check_tag_is_free(tag)
    print("  clean tree, right branch, tag is free")


# ── gates ────────────────────────────────────────────────────────────────────────────────


def gates(tag: str, *, contract: bool) -> tuple[Gate, ...]:
    """The checks a tag must survive, cheapest first.

    Deliberately the same commands `.github/workflows/ci.yml` runs, in the same spellings, so that
    a green run here means the same thing as a green run there. The ordering is the point: the
    tag/version check costs a second and catches the mistake most likely to be made, so it goes
    first, and the contract tests -- which spawn a real marimo -- go last.

    Args:
        tag: The tag being cut, checked against ``__version__``.
        contract: Include the live-kernel contract tests.

    Returns:
        The gates, in the order they should run.
    """
    ordered = [
        Gate("tag matches __version__", ("uv", "run", "python", "scripts/version.py", tag)),
        # --locked, not --frozen: this is where a uv.lock that has drifted from pyproject.toml
        # gets caught, rather than resolving something nobody committed.
        Gate("uv sync --locked", ("uv", "sync", "--locked")),
        Gate("ruff check", ("uv", "run", "ruff", "check", ".")),
        Gate("ruff format", ("uv", "run", "ruff", "format", "--check", ".")),
        Gate("non-negotiables", ("uv", "run", "python", "scripts/guardrails.py")),
        # Advisory here because it is advisory in CI: `ty check src/` reports six known
        # diagnostics, and until those are cleared it cannot gate anything.
        Gate("ty (advisory)", ("uv", "run", "ty", "check", "src/"), advisory=True),
        Gate("pytest", ("uv", "run", "pytest", "-q")),
    ]
    if contract:
        ordered.append(Gate("pytest -m contract", ("uv", "run", "pytest", "-m", "contract", "-q")))
    return tuple(ordered)


def _run_gates(ordered: Sequence[Gate]) -> None:
    """Run each gate in turn, stopping at the first non-advisory failure.

    Raises:
        ReleaseError: A gate that gates anything failed.
    """
    for number, gate in enumerate(ordered, start=1):
        print(f"\n[{number}/{len(ordered)}] {gate.name}")
        code = _stream(gate.argv)
        if code == 0:
            continue
        if gate.advisory:
            print(f"  {gate.name} failed, which is advisory. Carrying on.")
            continue
        raise ReleaseError(
            f"{gate.name} failed (exit {code}). Nothing has been committed, tagged or pushed.\n"
            f"  {_relative(VERSION_FILE)} is bumped and uncommitted -- fix the failure and "
            "re-run, or `git checkout` it to abandon the release."
        )


def build_check(version: str) -> None:
    """Build the artifacts and check the metadata PyPI will render.

    The wheel filename is what actually gets uploaded, so this asserts on that rather than
    trusting that hatchling read the same version everything else did. Mirrors the `build` job in
    `release.yml`, which would otherwise be the first place a broken README or a bad classifier
    turned up -- after the tag was pushed.

    Args:
        version: The version the artifacts must carry.

    Raises:
        ReleaseError: The build failed, the metadata is bad, or the filenames disagree.
    """
    print("\nbuild and metadata check")
    if DIST.exists():
        print(f"  clearing {_relative(DIST)}")
        shutil.rmtree(DIST)

    if _stream(("uv", "build")) != 0:
        raise ReleaseError("uv build failed")

    # Only the distributions. `uv build` also drops a `dist/.gitignore` holding `*`, and twine
    # --strict rejects anything it cannot recognise as a distribution. CI never trips over this
    # because its `dist/*` is a shell glob, and a shell glob does not match a dotfile.
    artifacts = sorted(path for path in DIST.iterdir() if path.suffix in {".whl", ".gz"})
    if not artifacts:
        raise ReleaseError(f"uv build reported success but {_relative(DIST)} holds no distribution")

    if _stream(("uv", "tool", "run", "twine", "check", "--strict", *map(str, artifacts))) != 0:
        raise ReleaseError(
            "twine rejected the metadata. PyPI would reject the same upload after the tag was "
            "pushed, which is a far worse place to find out."
        )

    expected = {f"kedge-{version}-py3-none-any.whl", f"kedge-{version}.tar.gz"}
    built = {path.name for path in artifacts}
    if missing := expected - built:
        raise ReleaseError(
            f"the build did not produce {', '.join(sorted(missing))}.\n"
            f"  {_relative(DIST)} holds: {', '.join(sorted(built)) or 'nothing'}"
        )
    print(f"  {', '.join(sorted(built))}")


# ── the irreversible half ────────────────────────────────────────────────────────────────


def _commit_and_tag(version: str, tag: str) -> str:
    """Commit the bump if there is one, then cut the annotated tag.

    Args:
        version: The version being released.
        tag: The tag to cut.

    Returns:
        The short SHA the tag points at.
    """
    if _capture(("git", "status", "--porcelain")):
        print(f"\ncommitting {_relative(VERSION_FILE)}")
        _capture(("git", "add", "--", _relative(VERSION_FILE)))
        _capture(("git", "commit", "-m", f"chore: release {version}"))
    else:
        print(f"\n{_relative(VERSION_FILE)} already committed at {version}; nothing to commit")

    print(f"tagging {tag}")
    _capture(("git", "tag", "-a", tag, "-m", f"kedge {version}"))
    return _capture(("git", "rev-parse", "--short", "HEAD"))


def _no_terminal(tag: str) -> str:
    """What to say when there is nobody at the keyboard to approve the push."""
    return (
        "there is no terminal to confirm the push at, and silence is not consent.\n"
        "  The commit and the tag exist locally, so nothing is lost. Either push them by hand:\n"
        f"    git push {REMOTE} {RELEASE_BRANCH} && git push {REMOTE} {tag}\n"
        "  or re-run with --yes."
    )


def _confirm(version: str, tag: str, sha: str, *, assume_yes: bool) -> None:
    """Show exactly what is about to leave the machine, and ask.

    The push is the only step here that cannot be undone: it triggers an upload to a PyPI index
    that will not accept the same version twice. Silence is never taken for consent.

    Args:
        version: The version being released.
        tag: The tag about to be pushed.
        sha: The commit the tag points at.
        assume_yes: Skip the prompt.

    Raises:
        ReleaseError: The user declined, or there is no terminal to ask at.
    """
    print(
        f"\nabout to push:\n"
        f"  branch  {RELEASE_BRANCH} -> {REMOTE}/{RELEASE_BRANCH}  (at {sha})\n"
        f"  tag     {tag}\n"
        f"This triggers release.yml, which uploads kedge {version} to PyPI. PyPI will not accept "
        f"{version} a second time."
    )
    if assume_yes:
        return

    # Both checks, because neither is sufficient on its own: `isatty` is false when stdin is a
    # pipe, but it is *true* under some terminal multiplexers and agent harnesses that still
    # deliver EOF to the first read. An unhandled EOFError at this point would dump a traceback
    # over a repository that already has a release commit and a tag in it.
    if not sys.stdin.isatty():
        raise ReleaseError(_no_terminal(tag))
    try:
        answer = input(f"push {tag}? [y/N] ")
    except EOFError:
        raise ReleaseError(_no_terminal(tag)) from None

    if answer.strip().lower() not in {"y", "yes"}:
        raise ReleaseError(
            f"declined. The commit and tag are still local:\n"
            f"    git tag -d {tag}\n"
            f"    git reset --hard HEAD~1   # only if the release commit was made by this run"
        )


def _push(tag: str) -> None:
    """Push the branch, then the tag -- in that order, and never the tag alone.

    A tag whose commit is not on the remote produces a release nobody can check out. Pushing the
    branch first means a rejected push (someone else got there first) costs a local tag and
    nothing else.

    Args:
        tag: The tag to push.

    Raises:
        ReleaseError: Either push was rejected.
    """
    print(f"\npushing {RELEASE_BRANCH}")
    if _stream(("git", "push", REMOTE, RELEASE_BRANCH)) != 0:
        raise ReleaseError(
            f"pushing {RELEASE_BRANCH} was rejected, so {tag} has not been pushed and nothing "
            "will be published.\n"
            f"  Sort the branch out, then either push the tag or re-cut it:\n"
            f"    git push {REMOTE} {tag}\n"
            f"    git tag -d {tag}"
        )

    print(f"pushing {tag}")
    if _stream(("git", "push", REMOTE, tag)) != 0:
        raise ReleaseError(
            f"pushing {tag} failed. The release commit is on {REMOTE} but the tag is not, so "
            "nothing has been published.\n"
            f"  Retry with:  git push {REMOTE} {tag}"
        )


# ── driver ───────────────────────────────────────────────────────────────────────────────


def _plan(version: str, tag: str, previous: str) -> None:
    """Print what a dry run would have done."""
    print(
        f"\ndry run. Nothing is committed, tagged or pushed, and "
        f"{_relative(VERSION_FILE)} is left at {previous}.\n"
        f"Re-run without --dry-run to:\n"
        f"    git add {_relative(VERSION_FILE)}\n"
        f'    git commit -m "chore: release {version}"\n'
        f'    git tag -a {tag} -m "kedge {version}"\n'
        f"    git push {REMOTE} {RELEASE_BRANCH}\n"
        f"    git push {REMOTE} {tag}"
    )


def _release(version: str, args: argparse.Namespace) -> int:
    """Run the release, start to finish."""
    tag = f"v{version}"
    print(f"kedge {version}  (tag {tag})\n")

    preflight(version, tag)

    original = _read_source()
    updated, previous = bump(original, version)
    bumped = updated != original
    try:
        if bumped:
            print(f"\nbumping __version__: {previous} -> {version}")
            _write_source(updated)
        else:
            print(f"\n__version__ is already {version}")

        _run_gates(gates(tag, contract=not args.skip_contract))
        if not args.skip_build_check:
            build_check(version)

        if args.dry_run:
            _plan(version, tag, previous)
            return 0

        sha = _commit_and_tag(version, tag)
        _confirm(version, tag, sha, assume_yes=args.yes)
        _push(tag)
    finally:
        # A dry run leaves nothing behind, the bump included. A real run that failed keeps it, so
        # that fixing the cause and re-running picks up where it stopped -- which is why
        # _check_tree_is_clean tolerates exactly that one dirty file.
        if args.dry_run and bumped:
            _write_source(original)

    print(
        f"\nreleased. The tag is what does the work from here:\n"
        f"  watch the run:  {ACTIONS_URL}\n"
        f"  it lands at:    {PYPI_URL}\n"
        f"If the run fails before the upload, see the recovery notes in RELEASING.md."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and cut the release."""
    parser = argparse.ArgumentParser(
        description="Bump, gate, tag and push a kedge release.",
        epilog="The push triggers release.yml, which builds and uploads to PyPI over OIDC.",
    )
    parser.add_argument("version", help="the version to release, such as 0.2.0 or v0.2.0")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run every check, then restore the version file and stop before git",
    )
    parser.add_argument("--yes", action="store_true", help="do not prompt before pushing")
    parser.add_argument(
        "--skip-contract",
        action="store_true",
        help="skip the live-kernel contract tests; CI still runs them on the tag",
    )
    parser.add_argument(
        "--skip-build-check",
        action="store_true",
        help="skip uv build and the twine metadata check (they need network on first use)",
    )
    args = parser.parse_args(argv)

    # The gates write straight to this process's stdout, so a block-buffered stdout -- which is
    # what a pipe or a `tee` gets -- would file every step header behind the output of the step it
    # introduces. Line buffering keeps the log readable when it is not going to a terminal.
    sys.stdout.reconfigure(line_buffering=True)

    version = args.version.removeprefix("v").strip()
    if not VERSION_PATTERN.match(version):
        print(
            f"{args.version!r} is not a version this project can release.\n"
            "  Expected PEP 440, such as 0.2.0, 0.2.0rc1 or 1.0.0.dev1.",
            file=sys.stderr,
        )
        return 1

    try:
        return _release(version, args)
    except ReleaseError as exc:
        print(f"\nrelease aborted: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "\nrelease interrupted. Check `git status` and `git tag` before re-running.",
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    sys.exit(main())
