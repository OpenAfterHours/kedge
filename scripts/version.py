"""Read the package version, and check a release tag against it.

`pyproject.toml` declares the version dynamic and points hatchling at ``__version__`` in
`src/kedge/__init__.py`, so there is exactly one place to bump -- but that also means neither
`tomllib` nor ``uv version`` can read it, and a release tag that disagrees with the package would
otherwise only be caught after the artifacts were built and pushed.

Run it before tagging::

    uv run python scripts/version.py            # print the version
    uv run python scripts/version.py v0.2.0     # and check the tag agrees

The `--github-output` form emits `key=value` lines for a workflow step to append to
``$GITHUB_OUTPUT``.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = REPO_ROOT / "src" / "kedge" / "__init__.py"

# PEP 440's pre-release, dev-release and post-release spellings, as far as we need them: enough to
# decide whether a GitHub release should be flagged as a pre-release.
PRERELEASE = re.compile(r"(a|b|rc|alpha|beta|dev)\d*", re.IGNORECASE)


def read_version(path: Path = VERSION_FILE) -> str:
    """Return the ``__version__`` string assigned in `path`.

    Parsed rather than imported: importing the package pulls in every dependency, which the
    release preflight should not need, and executing code to read a constant is a poor trade.

    Args:
        path: The module holding the assignment.

    Returns:
        The version string.

    Raises:
        SystemExit: The file has no literal ``__version__`` assignment.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "__version__" in targets and isinstance(node.value, ast.Constant):
            value = node.value.value
            if isinstance(value, str):
                return value
    raise SystemExit(f"no literal __version__ assignment in {path}")


def is_prerelease(version: str) -> bool:
    """True when the version carries a PEP 440 pre-release or dev segment."""
    return bool(PRERELEASE.search(version))


def main(argv: list[str] | None = None) -> int:
    """Print the version, and compare it with a tag when one is given."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "tag",
        nargs="?",
        help="a release tag such as v0.2.0; checked against the package version",
    )
    parser.add_argument(
        "--github-output",
        action="store_true",
        help="emit key=value lines for $GITHUB_OUTPUT rather than a bare version",
    )
    args = parser.parse_args(argv)

    version = read_version()

    if args.tag is not None:
        expected = args.tag.removeprefix("v")
        if expected != version:
            print(
                f"tag {args.tag} does not match the package version.\n"
                f"  tag says:     {expected}\n"
                f"  {VERSION_FILE.relative_to(REPO_ROOT).as_posix()} says: {version}\n"
                f"Bump __version__ and commit it before tagging, or delete and re-cut the tag.",
                file=sys.stderr,
            )
            return 1

    if args.github_output:
        print(f"version={version}")
        print(f"prerelease={str(is_prerelease(version)).lower()}")
    else:
        print(version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
