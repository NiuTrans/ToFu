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
