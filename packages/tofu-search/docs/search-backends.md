# Free/open-source search backend review

Reviewed on 2026-08-10. The production criterion is not the number of upstream
names: it is independent coverage, a stable machine-readable interface, bounded
latency, and an operating model that does not make every ChatUI request depend
on one public volunteer instance.

The product constraint is free search without mandatory API keys or paid
services. Reliability and deployment boundaries live in
[Free search reliability](free-search.md).

| Project | What it adds | Decision for tofu-search |
|---|---|---|
| [SearXNG](https://github.com/searxng/searxng) | Actively maintained metasearch with a documented [HTTP search API](https://docs.searxng.org/dev/search_api.html) and adapters for Mwmbl, OpenAlex, PubMed, GitHub, Marginalia, YaCy and many others | **Preferred optional aggregation layer.** A self-host set with `TOFU_SEARCH_SEARXNG_URL` is tried first. Empty `TOFU_SEARCH_SEARXNG_ENGINES` respects that instance's enabled engines. Direct DDG/Brave/Bing/Marginalia paths remain independent fallbacks. |
| [Marginalia Search](https://github.com/MarginaliaSearch/MarginaliaSearch) | Independent crawler/index focused on text-heavy “small web” pages, complementary to commercial indexes | **Already integrated directly.** Keep it for diversity, but never rely on its shared public quota as the only source. |
| [Mwmbl](https://github.com/mwmbl/mwmbl) | Independent nonprofit/community index; its own documentation notes that the index is smaller than commercial engines | **Use through self-hosted SearXNG first.** Its engine attribution is retained as a consensus signal. A second direct scraper would add maintenance without a stronger availability contract. |
| [4get](https://git.lolcat.ca/lolcat/4get) | Lightweight, actively maintained proxy search frontend with many scraper choices | **Adapter candidate, not a default dependency.** It overlaps tofu-search's direct scrapers and moves availability to another instance. Add only behind an explicit configured URL and offline parser fixtures if SearXNG coverage proves insufficient. |
| [Crawl4AI](https://github.com/unclecode/crawl4ai) | Browser crawler, bounded deep crawling, pruning and query-aware BM25 content filtering | **Patterns adopted, dependency deferred.** tofu-search already owns HTTP/Playwright/auth fallbacks; importing another browser runtime would duplicate that stack. The useful patterns—bounded candidates, query-focused passages and race-to-N—stay lightweight in core. |
| [Trafilatura](https://github.com/adbar/trafilatura) | Focused main-text and metadata extraction | **Already a core extractor.** Keep the local HTML/Playwright fallbacks for pages it cannot represent. |
| [Stract](https://github.com/StractOrg/stract) | Independent index and customizable ranking | **Do not integrate now.** The upstream repository was archived on 2026-04-02, so it is not a sound new availability dependency. |

## Recommended deployment

Run one tofu-search MCP process and, when broader recall is required, one
self-hosted SearXNG instance on the same private network:

```bash
export TOFU_SEARCH_SEARXNG_URL=http://searxng:8080
# Empty means “use the engines enabled in SearXNG settings.yml”.
export TOFU_SEARCH_SEARXNG_ENGINES=
export TOFU_SEARCH_MCP_CONTENT_BUDGET_CHARS=18000
tofu-search-mcp --transport http
```

Enable independent indexes/verticals in SearXNG (for example Mwmbl, OpenAlex,
PubMed and Marginalia) instead of adding another copy of Google/Bing/DDG only.
tofu-search merges canonical duplicate URLs, retains engine/upstream consensus,
then balances relevance, primary-source authority and host diversity.

Public SearXNG instances remain best-effort fallback only. Its official API
documentation warns that many public instances disable JSON output; tofu-search
therefore supports HTML fallback, but a self-host is the reproducible ChatUI
configuration.

## Agent search patterns reviewed on 2026-10-20

These are capability comparisons from upstream documentation, not measured
claims that one provider outperforms another.

| Source | Useful capability | Current tofu-search boundary |
|---|---|---|
| [Exa Search](https://exa.ai/docs/reference/search) | Query-specific highlights alongside source identity | Reuse the evidence-selection pattern locally; no paid adapter is included. |
| [Tavily Search](https://docs.tavily.com/documentation/api-reference/endpoint/search) | Multiple relevant chunks per source and explicit search-depth choices | Complementary local passages share the existing output budget without a service dependency. |
| [Firecrawl Search](https://docs.firecrawl.dev/features/search) | Search with optional extraction of result pages | The existing overlapping search/fetch pipeline owns this lifecycle; importing a second crawler would duplicate ownership. |
| [Crawl4AI Fit Markdown](https://docs.crawl4ai.com/core/fit-markdown/) | Query-aware content filtering separately from raw content | Source bodies remain intact while query-focused spans enter the ranker and tool preview. |
| [WebCode](https://exa.ai/blog/webcode) and [ATLAS](https://exa.ai/blog/atlas-benchmark) | Evaluate coding search and search-intensive multi-hop/entity tasks beyond a single SERP | Relevant external benchmark candidates; tofu-search has not been evaluated on them here. Provider-authored results are not neutral proof of superiority. |

The adopted local mechanisms and reproducible regression measurements are
specified in [Search evidence quality](relevance-quality.md). A future live
provider comparison must fix the agent/model, task set and budget, retain source
provenance, and report failures and coverage gaps alongside successes.

## Keyed search gateway (optional paid sources)

Free search stays the default: without gateway configuration nothing below
runs. A deployment that owns a billing key for a search gateway (one JSON
endpoint fronting several commercial search APIs) can add those indexes as
first-class engines. `search/engines/gateway.py` speaks the gateway's
"universal-search v1" wire format: `POST {query, isFast, timeout, sources, topK}`
with `Authorization: Bearer <key>`, answering
`{status, data: {code, msg, results[]}}`.

**Observed protocol facts (probed 2026-10-27, eight sources).**

- Failures are HTTP 200 with a nonzero `data.code`: 400 (invalid query,
  sources or key), 1029 (requests-per-minute limit), 500 (upstream failure,
  detail in `data.internal_err_map`). `data.searchParameters` echoes the key,
  so bodies are never logged or copied into errors.
- A request naming several sources returns one merged list truncated to
  `topK`; a four-source request returned ten rows, all from the first source.
- `isFast=true` returns snippets in 0.5–1.3 s per source. `isFast=false` asks
  the gateway to crawl result pages: 1–10 s per source, and a source that
  misses the request `timeout` returns empty bodies rather than an error.
- Rows carry `publish_time` in several spellings (`2025-04-07`,
  `2026/04/02 00:00:00`, ISO 8601 with offsets or seven fractional digits).
  Bing-backed rows wrap links in `bing.com/ck/a` redirects.

**Decisions.**

| Problem | Decision | Alternative rejected |
|---|---|---|
| Merged multi-source requests lose all but one source and wait for the slowest | One request per configured source, each its own engine tagged `API:<id>` | One merged request: fewer calls, but no per-source rank evidence for reciprocal-rank fusion and no partial results |
| Every search waits for all engines; in recent host logs the free engines averaged 9.6 s (Marginalia), 4.1 s (SearXNG) and 3.7 s (Brave) | `free_engines` policy: `fallback` (default) runs DDG/Brave/Bing/SearXNG/Marginalia only when the gateway produced no query-relevant candidate or failed; `parallel` always runs them; `off` never does, including the DDG/Brave retry and the browser-search fallback | Always parallel: the slowest free engine sets the latency floor even when the gateway already answered |
| The protocol has no time filter | Normalize `publish_time` to `published_date` (shown to the model) and drop rows whose known date is outside the freshness window; undated rows stay | Ignoring freshness, or dropping undated rows and silently losing recall |
| Our own page fetch is the slowest stage and fails on anti-bot sites | Opt-in `gateway_full_content`: a provider body of at least 200 characters that exceeds its snippet replaces our fetch and counts toward Race-to-N; other rows are fetched as before | Trusting every provider body: one source answered with 65-character placeholders |
| Rate-limited sources (code 1029) would cost every query a round trip | 60 s per-source cooldown reported as `engine_unavailable: rate_limited`; repeated transport failures trip the shared engine circuit | Retrying immediately, which spends quota the limit is protecting |

**Measured end to end (2026-10-27, four sources, four queries, behind a
corporate proxy, LLM filter off, five results).** Mean wall time was 18.9 s
with free engines only, 14.3 s with gateway sources plus free-engine fallback
(the fallback never triggered), and 10.1 s with `gateway_full_content`, which
returned page bodies for all results. On a Chinese finance query the free
engines all failed (0 results after 40.8 s) while full-content gateway search
answered in 9.3 s. Full content is still opt-in because its per-call cost is
set by the gateway operator, not measured here.

The gateway URL, key and source ids are configuration, never code: a host
supplies them through `configure()` or `TOFU_SEARCH_GATEWAY_*` variables, and
the Tofu host keeps company endpoints in release-tiered bundles. Responses are
bounded at 8 MiB and `topK` at 50. An explicit `engines=[...]` selection is
honoured exactly; gateway sources and the free-engine policy apply only to the
default Web selection.
