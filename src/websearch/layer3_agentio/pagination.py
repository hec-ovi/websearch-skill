"""Token-budget pagination for a fetched page body.

Splits a Markdown body into pages each at most ``page_size_tokens`` (estimated), so a
single ``web_fetch`` response stays under a harness tool-output cap (Claude Code caps at
25,000 tokens) without ever dropping content. This is progressive disclosure, NOT a cap:
the split is LOSSLESS (the pages concatenate back to the exact original) and every page
is reachable via ``web_open``. Splitting prefers line boundaries; a single line longer
than the budget is hard-split so the guarantee holds for any input.
"""

from __future__ import annotations


def paginate(markdown: str, *, page_size_tokens: int, chars_per_token: float = 4.0) -> list[str]:
    """Split ``markdown`` into pages. ``"".join(paginate(md, ...)) == md`` always holds."""
    budget = max(1, int(page_size_tokens * chars_per_token))
    if len(markdown) <= budget:
        return [markdown]

    pages: list[str] = []
    current: list[str] = []
    current_len = 0

    for line in markdown.splitlines(keepends=True):
        if len(line) > budget:
            # A single oversized line: flush the buffer, emit full-budget chunks, and keep
            # the remainder as the running buffer so following lines can still join it.
            if current:
                pages.append("".join(current))
                current, current_len = [], 0
            start = 0
            while len(line) - start > budget:
                pages.append(line[start : start + budget])
                start += budget
            current = [line[start:]]
            current_len = len(line) - start
            continue
        if current and current_len + len(line) > budget:
            pages.append("".join(current))
            current, current_len = [line], len(line)
        else:
            current.append(line)
            current_len += len(line)

    if current:
        pages.append("".join(current))
    return pages or [""]
