"""Dependency-free token estimation.

The default is the character heuristic ``ceil(len(text) / chars_per_token)`` with
``chars_per_token = 4`` (OpenAI's documented "~4 characters per token" rule of thumb).
It is chosen over ``words * 1.33`` because the word heuristic collapses on exactly the
payloads this layer ships: a one-line JSON or code block is a single whitespace token,
so ``words * 1.33`` wildly under-counts it, while ``chars / 4`` stays stable across
prose, Markdown, and code. The estimate carries roughly +/-10-20% error on prose and
under-counts dense code/Markdown, which is why a caller may inject a real tokenizer.

To use an exact tokenizer without adding a dependency to the default closure, pass an
``estimator`` callable (e.g. ``tiktoken``'s ``enc.encode`` length, or Anthropic's
``count_tokens``). Lowering ``chars_per_token`` (e.g. to 3.5 for Claude) biases the
estimate upward so the progressive-disclosure gate errs toward keeping content inline.
"""

from __future__ import annotations

import math
from collections.abc import Callable

from .models import DEFAULT_CHARS_PER_TOKEN

Estimator = Callable[[str], int]


def estimate_tokens(
    text: str | None,
    *,
    chars_per_token: float = DEFAULT_CHARS_PER_TOKEN,
    estimator: Estimator | None = None,
) -> int:
    """Estimate the token count of ``text``. Returns 0 for empty/None input."""
    if estimator is not None:
        return max(0, int(estimator(text or "")))
    if not text:
        return 0
    return math.ceil(len(text) / chars_per_token)
