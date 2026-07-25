"""Shared pytest configuration.

Puts `tests/fixtures` on `sys.path` so corpus tests can `import manifest` directly rather
than carrying path juggling and `# noqa: E402` at the top of every file.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
FIXTURES_DIR = Path(__file__).parent / "fixtures"

# `utils/` and `context/` live at the repo root rather than inside the package: they are the
# user-editable halves of the project, imported from notebook cells. Tests reach them the same
# way a notebook would.
for directory in (FIXTURES_DIR, REPO_ROOT):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
