"""The system prompt, kept as files rather than as string constants in Python.

The prompt is the largest single artifact in this milestone and the one most likely to be edited
by someone who is not editing code — so it lives as Markdown, one file per concern, and is
assembled here in a fixed order. A diff to ``excel.md`` is then a readable diff about Excel
semantics rather than a wall of escaped quotes inside a ``.py``.

The parts are separate because they change for different reasons. ``polars.md`` changes when the
house style changes; ``excel.md`` changes when someone finds another place polars and Excel
disagree; ``vocabulary.md`` changes when the translation reference grows. Concatenating them at
load time costs nothing and keeps the blame history honest.

A missing part is a warning and an empty string, never an exception. A prompt file that failed to
ship should degrade the model's briefing, not stop the conversation — and the warning names the
file, so the cause is obvious the first time anyone looks.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "PROMPTS_DIR",
    "SYSTEM_PARTS",
    "build_system_prompt",
    "load_prompt",
]

PROMPTS_DIR = Path(__file__).parent
"""Where the Markdown parts live."""

SYSTEM_PARTS: tuple[str, ...] = (
    "role.md",
    "marimo.md",
    "polars.md",
    "excel.md",
    "vocabulary.md",
    "tools.md",
)
"""The parts of the system prompt, in the order they are concatenated.

Order is deliberate: who you are, the rule that will otherwise break every turn, the house style,
the semantics that produce silently wrong numbers, the vocabulary, then the tool discipline.
"""


def load_prompt(name: str) -> str:
    """Return the text of one prompt file.

    Args:
        name: File name relative to :data:`PROMPTS_DIR`, e.g. ``"excel.md"``.

    Returns:
        The file's text, stripped of trailing whitespace, or an empty string if it is missing or
        unreadable.
    """
    path = PROMPTS_DIR / name
    try:
        return path.read_text(encoding="utf-8").rstrip()
    except OSError as exc:
        logger.warning("could not read the prompt part %s: %s", path, exc)
        return ""


def build_system_prompt(*, parts: Sequence[str] = SYSTEM_PARTS, extra: Sequence[str] = ()) -> str:
    """Assemble the system prompt from its parts.

    Args:
        parts: File names to concatenate, in order. Defaults to :data:`SYSTEM_PARTS`.
        extra: Additional blocks appended verbatim after the files — used by callers that want to
            state something about this particular workspace without editing a shipped file.

    Returns:
        The full system prompt.
    """
    blocks = [text for text in (load_prompt(name) for name in parts) if text]
    blocks.extend(block.rstrip() for block in extra if block.strip())
    return "\n\n".join(blocks)
