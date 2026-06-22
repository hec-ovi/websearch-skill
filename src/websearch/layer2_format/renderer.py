"""The default FORMAT renderer: one layout-stable Markdown document per page.

Each result is a delimited block carrying its stable id in open/close markers (so a
consumer can locate and resolve any result), an ``H2`` rank-and-title heading, a
key-value metadata line, and a clean Markdown body or a progressive-disclosure preview.
A status line and a footer carry pagination. The layout is stable across pages and
modes so a downstream parser never has to special-case position.

There is no output-length cap. In full mode a body longer than ``body_char_budget`` is
shown up to the budget with a resolve hint; the full body still lives in the sidecar
and the store and is recoverable by id. ``body_char_budget=None`` inlines every body.
"""

from __future__ import annotations

from .models import FormattedResult

_RESOLVE_HINT = "_full body available by id `{id}`_"


def _meta_line(r: FormattedResult) -> str:
    parts: list[str] = []
    if r.site:
        parts.append(r.site)
    if r.published_date:
        parts.append(str(r.published_date))
    if r.author:
        parts.append(str(r.author))
    if r.score is not None:
        parts.append(f"score={r.score:.4f}")
    if r.quality_score is not None:
        parts.append(f"quality={r.quality_score:.2f}")
    if r.lang:
        parts.append(str(r.lang))
    if r.page_type:
        parts.append(str(r.page_type))
    if r.fetched_at:
        parts.append(f"fetched {r.fetched_at}")
    return " · ".join(parts)


def _preview(r: FormattedResult, body: str, budget: int | None) -> str:
    """The compact body shown in index mode, per the ``body`` selector."""
    if body == "summary" and r.summary:
        text = r.summary
    elif body == "highlights" and r.highlights:
        text = "\n\n".join(h.text for h in r.highlights if h.text.strip())
    else:  # "text", or a fallback when the preferred field is empty
        first_highlight = r.highlights[0].text if r.highlights else ""
        text = r.summary or first_highlight or (r.body_markdown or "")
    text = text.strip()
    if not text:
        return "_(no preview; resolve for the full body)_"
    limit = budget if budget is not None else len(text)
    if len(text) > limit:
        text = text[:limit].rstrip() + " ..."
    return text


class MarkdownRenderer:
    name = "markdown"

    def render(
        self,
        results: list[FormattedResult],
        *,
        query: str | None,
        mode: str,
        body: str,
        page: int,
        page_size: int,
        total_results: int,
        total_pages: int,
        next_cursor: str | None,
        total_dropped_duplicates: int,
        page_token_estimate: int,
        body_char_budget: int | None,
    ) -> str:
        lines: list[str] = []
        heading = f"# Search results: {query}" if query else "# Search results"
        lines.append(heading)
        lines.append("")
        status = (
            f"> page {page + 1} of {max(total_pages, 1)} · "
            f"{len(results)} of {total_results} result(s) · "
            f"mode {mode} · ~{page_token_estimate} tokens"
        )
        if total_dropped_duplicates:
            status += f" · {total_dropped_duplicates} duplicate(s) folded"
        lines.append(status)
        lines.append("")

        for r in results:
            lines.append(f"<!-- result {r.id} rank {r.rank} -->")
            lines.append(f"## {r.rank}. {r.title or '(untitled)'}")
            lines.append(f"- url: {r.url}")
            meta = _meta_line(r)
            if meta:
                lines.append(f"- {meta}")
            if r.dropped_duplicates:
                dup_urls = ", ".join(d.url for d in r.dropped_duplicates)
                lines.append(f"- folds {len(r.dropped_duplicates)} duplicate(s): {dup_urls}")
            lines.append("")

            if mode == "full":
                full = (r.body_markdown or "").strip()
                if not full:
                    lines.append("_(no body)_")
                elif body_char_budget is not None and len(full) > body_char_budget:
                    lines.append(full[:body_char_budget].rstrip() + " ...")
                    lines.append("")
                    lines.append(_RESOLVE_HINT.format(id=r.id))
                else:
                    lines.append(full)
            else:  # index mode: a preview plus a resolve hint
                lines.append(_preview(r, body, body_char_budget))
                lines.append("")
                lines.append(_RESOLVE_HINT.format(id=r.id))

            lines.append("")
            lines.append(f"<!-- /result {r.id} -->")
            lines.append("")

        lines.append("---")
        lines.append(f"next_cursor: {next_cursor if next_cursor else '(none; last page)'}")
        lines.append(f"total_results: {total_results}")
        return "\n".join(lines)
