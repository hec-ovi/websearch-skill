# Benchmark: this tool vs a hosted agent web search

A quick head-to-head against a strong baseline: the web search built into
Claude Code (a hosted, paid, US-only search the assistant calls as a native tool). It
is a good yardstick because it is what an agent reaches for when it has no other search.

This is not a formal benchmark suite. It is a same-query, same-moment spot check run on
2026-06-22, recorded so the comparison is reproducible rather than asserted. Search
results drift day to day and engines vary moment to moment, so treat the specifics as a
snapshot, not a leaderboard. For rigorous numbers, measure the retriever in isolation
(see the 7-axis scorecard in the README), not a downstream model's answer.

## Method

Same query, issued to both at the same time. The tool ran with zero configuration (the
keyless `ddgs` metasearch, no SearXNG, no API key):

```bash
uv run websearch web-search "<query>" --max-results 5
```

The baseline is the native Claude Code web search tool on the identical query. We compare
what each surfaces (titles, URLs, freshness, relevance), which is the retriever's job.

## Query 1: "latest AI models released June 2026"

Both returned fresh, on-topic results, and the top sources overlapped heavily:
`llm-stats.com`, `aireleasetracker.com`, `buildfastwithai.com`, and
`blog.mean.ceo` appeared in both rankings, with recency markers like "22 minutes ago"
and "1 week ago". The tool returned in about 2.8s.

## Query 2: "open source vector database comparison 2026"

Both returned relevant, current 2026 comparison pages. Here the specific domains
diverged more: the tool surfaced `ztabs.co`, `mudassirkhan.me`, `swarmsignal.net`,
and `tokenmix.ai`; the baseline surfaced `redis.io`, `instaclustr.com`, `encore.dev`,
`firecrawl.dev`, and `datacamp.com`. Both sets are reasonable answers to the query;
neither is obviously better, which is the typical result.

## What the comparison shows

| | This tool | Native hosted search |
|---|---|---|
| Finding relevant pages | comparable | comparable |
| Freshness | current | current |
| Returns | ranked links + snippets + a reusable `handle` | links plus a written summary |
| Full page content | `web-fetch` extracts clean Markdown on demand, fenced | snippet level |
| Setup | a `uv sync`, no key | none, it is built in |
| Cost | free, runs locally | hosted, paid, US-only |
| Privacy | nothing leaves your machine | queries go to the vendor |
| Configurable | engines, SearXNG, paid adapters | fixed |

On the core retrieval job, finding relevant, fresh pages, the two are comparable. The
hosted search has two advantages: it is frictionless because it is built in, and it
writes a summary in the same call. This tool wins on cost, privacy, control, full clean
extraction (`web-fetch`), multi-engine recall with de-correlated fusion, and the niche
tools (`arxiv`, `github`) that general web search does not cover.

The takeaway: this is a Pareto win rather than a clean sweep. Use the tool when cost,
privacy, self-hosting, configurability, or clean extraction matter; the gap on raw
"find me a relevant page" is small.

## Reproduce it

```bash
# this tool (zero config, keyless)
uv run websearch web-search "open source vector database comparison 2026" --max-results 5

# force specific engines (power-user knob, on the lower-level `search` command)
uv run websearch search "..." --ddgs-backends google,brave,mojeek

# add your own SearXNG for broader recall (works on web-search too)
export WEBSEARCH_SEARXNG_URL=http://localhost:8080   # see docker/searxng/
```

Pair the output against whatever search your agent harness ships, on the same query at
the same time, and judge relevance and freshness yourself.
