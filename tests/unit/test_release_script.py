"""The pure half of the release script, and the one promise its driver makes about the tree.

Everything in `scripts/release.py` that touches git or spawns `uv` is exercised by actually
cutting a release; there is no honest way to unit test it and no value in mocking it into a
tautology. What can be tested is the part that would silently corrupt the release commit: the
rewrite of the ``__version__`` assignment, the porcelain parsing that decides whether the tree is
clean enough, and the version pattern that catches a typed tag before anything is written.

The driver is tested too, but only for where it leaves the version file. The steps are stubbed
and the assertion is bytes on disk rather than which stub was called, so this pins the contract
-- a run that does not reach the release commit leaves the tree as it found it -- without pinning
the shape of the code that keeps it.

`tests/conftest.py` puts the repo root on `sys.path`, so `scripts` imports as a namespace package
the same way `utils` does.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from scripts import release
from scripts.release import VERSION_PATTERN, ReleaseError, bump, changed_paths

_MODULE = '''"""A module docstring mentioning __version__ to be unhelpful."""

from __future__ import annotations

__version__ = "0.0.1"

__all__ = [
    "__version__",
]
'''


def test_bump_rewrites_only_the_assignment() -> None:
    updated, previous = bump(_MODULE, "0.2.0")

    assert previous == "0.0.1"
    assert '__version__ = "0.2.0"' in updated
    # The docstring and the __all__ entry both contain the string; neither may be touched.
    assert updated.count("__version__") == _MODULE.count("__version__")
    assert '"""A module docstring mentioning __version__ to be unhelpful."""' in updated
    assert '    "__version__",' in updated


def test_bump_is_a_no_op_when_the_version_already_matches() -> None:
    updated, previous = bump(_MODULE, "0.0.1")

    assert previous == "0.0.1"
    assert updated == _MODULE


@pytest.mark.parametrize("ending", ["\n", "\r\n"])
def test_bump_preserves_the_line_ending(ending: str) -> None:
    """A release commit that also flips the file's line endings is unreviewable."""
    source = _MODULE.replace("\n", ending)

    updated, _ = bump(source, "1.0.0")

    assert f'__version__ = "1.0.0"{ending}' in updated
    assert updated.count(ending) == source.count(ending)


def test_bump_rejects_a_module_with_no_literal_assignment() -> None:
    with pytest.raises(ReleaseError, match="no literal __version__"):
        bump("__version__ = compute_it()\n", "0.2.0")


def test_changed_paths_reads_porcelain_entries() -> None:
    porcelain = " M src/kedge/__init__.py\n?? notes.txt\nA  tests/unit/test_new.py\n"

    assert changed_paths(porcelain) == [
        "src/kedge/__init__.py",
        "notes.txt",
        "tests/unit/test_new.py",
    ]


def test_changed_paths_needs_the_leading_column_intact() -> None:
    """The regression that named `pyproject.toml` as `yproject.toml`.

    ``git status --porcelain`` column-aligns a two-character status field, so ` M path` starts
    with a significant space. Stripping the captured output -- which is the sensible default for
    every other git call this script makes -- shifts the whole line left by one and takes the
    first character of the path with it. The parser assumes the caller passed raw output; this
    pins the assumption so a future tidy-up of `_capture` cannot quietly reintroduce it.
    """
    assert changed_paths(" M pyproject.toml\n") == ["pyproject.toml"]
    assert changed_paths("M pyproject.toml\n".strip()) != ["pyproject.toml"]


def test_changed_paths_resolves_renames_and_quoting() -> None:
    """A rename is dirty at its destination, and git quotes paths that need it."""
    porcelain = 'R  scripts/old.py -> scripts/new.py\n M "src/kedge/a b.py"\n'

    assert changed_paths(porcelain) == ["scripts/new.py", "src/kedge/a b.py"]


# ── what a run leaves on disk ────────────────────────────────────────────────────────────────


@pytest.fixture
def version_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in for `src/kedge/__init__.py`, in a stand-in repo root, past preflight."""
    path = tmp_path / "src" / "kedge" / "__init__.py"
    path.parent.mkdir(parents=True)
    path.write_text(_MODULE, encoding="utf-8")
    monkeypatch.setattr(release, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(release, "VERSION_FILE", path)
    monkeypatch.setattr(release, "preflight", lambda version, tag: None)
    return path


def _args(**overrides: bool) -> argparse.Namespace:
    """The flags of a run that asks nothing of the network and nobody at the keyboard."""
    settings = {"dry_run": False, "yes": True, "skip_contract": True, "skip_build_check": True}
    return argparse.Namespace(**{**settings, **overrides})


def test_a_failed_gate_puts_the_version_file_back(
    version_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug this pins: 0.0.1 -> 0.0.3 left on disk by a release that never happened.

    Nobody reads an uncommitted one-line version bump as debris three days later. They read it as
    a change somebody meant to make, and commit it.
    """

    def fail(ordered: object) -> None:
        raise ReleaseError("pytest failed (exit 1)")

    monkeypatch.setattr(release, "_run_gates", fail)

    with pytest.raises(ReleaseError, match="pytest failed"):
        release._release("0.2.0", _args())

    assert version_file.read_text(encoding="utf-8") == _MODULE


def test_a_dry_run_puts_the_version_file_back(
    version_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(release, "_run_gates", lambda ordered: None)

    assert release._release("0.2.0", _args(dry_run=True)) == 0
    assert version_file.read_text(encoding="utf-8") == _MODULE


def test_the_bump_stays_once_the_release_commit_holds_it(
    version_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(release, "_run_gates", lambda ordered: None)
    monkeypatch.setattr(release, "_commit_and_tag", lambda version, tag: "abc1234")
    monkeypatch.setattr(release, "_confirm", lambda *a, **kw: None)
    monkeypatch.setattr(release, "_push", lambda tag: None)

    assert release._release("0.2.0", _args()) == 0
    assert '__version__ = "0.2.0"' in version_file.read_text(encoding="utf-8")


def test_a_declined_push_leaves_the_committed_bump_alone(
    version_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the commit the bump is git's, not the working tree's; reverting it would desync them."""

    def decline(*args: object, **kwargs: object) -> None:
        raise ReleaseError("declined")

    monkeypatch.setattr(release, "_run_gates", lambda ordered: None)
    monkeypatch.setattr(release, "_commit_and_tag", lambda version, tag: "abc1234")
    monkeypatch.setattr(release, "_confirm", decline)

    with pytest.raises(ReleaseError, match="declined"):
        release._release("0.2.0", _args())

    assert '__version__ = "0.2.0"' in version_file.read_text(encoding="utf-8")


@pytest.mark.parametrize("version", ["0.2.0", "1.0.0rc1", "0.2.0a1", "1.0.0.dev1", "2.0"])
def test_version_pattern_accepts_pep_440(version: str) -> None:
    assert VERSION_PATTERN.match(version)


@pytest.mark.parametrize("version", ["v0.2.0", "0.2.0 ", "=0.2.0", "0.2.0-rc1", "latest", ""])
def test_version_pattern_rejects_a_typo(version: str) -> None:
    assert not VERSION_PATTERN.match(version)
