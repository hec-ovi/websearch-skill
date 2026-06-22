"""Split Markdown into passages for indexing and per-passage citation.

Two strategies, both returning ``(text, start, end)`` triples whose ``[start, end)``
character offsets index into the ORIGINAL Markdown verbatim (so a passage slices back
out exactly, and citations point at real spans):

- ``heading`` (default): break at Markdown ATX headings (``#``..``######``), then split
  any oversized section into character windows. Keeps semantically-coherent units.
- ``fixed``: sliding character windows of ``max_chars`` with ``overlap``.

A passage is the minimal citable unit, so chunks are kept modest (default 1200 chars).
"""

from __future__ import annotations

import re

_HEADING = re.compile(r"^#{1,6}[ \t]+\S", re.MULTILINE)


def _window(text: str, start: int, end: int, max_chars: int) -> list[tuple[str, int, int]]:
    """Break ``text[start:end]`` into non-overlapping windows of at most max_chars."""
    out: list[tuple[str, int, int]] = []
    i = start
    while i < end:
        j = min(i + max_chars, end)
        out.append((text[i:j], i, j))
        i = j
    return out


def chunk_markdown(
    markdown: str,
    *,
    strategy: str = "heading",
    max_chars: int = 1200,
    overlap: int = 0,
) -> list[tuple[str, int, int]]:
    """Return ``(text, start, end)`` passages over ``markdown``.

    Empty/blank input yields no passages. Offsets are absolute into ``markdown``.
    """
    if not markdown or not markdown.strip():
        return []

    if strategy == "fixed":
        step = max(1, max_chars - max(0, overlap))
        out: list[tuple[str, int, int]] = []
        i = 0
        n = len(markdown)
        while i < n:
            j = min(i + max_chars, n)
            seg = markdown[i:j]
            if seg.strip():
                out.append((seg, i, j))
            if j >= n:
                break
            i += step
        return out

    # heading-aware: section boundaries at heading line starts.
    boundaries = [m.start() for m in _HEADING.finditer(markdown)]
    starts = [0] + [b for b in boundaries if b > 0]
    starts = sorted(set(starts))
    spans = list(zip(starts, starts[1:] + [len(markdown)], strict=True))

    passages: list[tuple[str, int, int]] = []
    for s, e in spans:
        if not markdown[s:e].strip():
            continue
        if e - s <= max_chars:
            passages.append((markdown[s:e], s, e))
        else:
            passages.extend(w for w in _window(markdown, s, e, max_chars) if w[0].strip())
    return passages
