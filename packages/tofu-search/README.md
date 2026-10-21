# 🔍 tofu-search

**Multi-engine web search + content fetching with optional LLM filtering** — a
standalone Python library extracted from the Tofu AI assistant.

This is a full re-extraction that keeps **100% of Tofu's current search/fetch
capabilities**: every engine, the structured "vertical" lookups, one-hop
deepening, the SPA/bot-protection Playwright fallback, authenticated-source
fetching, and the host-browser fallback — the last two exposed through
optional [provider seams](#host-integration-provider-seams) so the library
stays dependency-free when used standalone.

## Features

- **Free core**: no paid search API or account is required. SearXNG recovers
  through configured instances when initial evidence is weak, preserves partial
  results and reports lost upstream coverage. See
  [free search reliability](docs/free-search.md) for limits and verification.
- **HTTP/2 search compatibility**: DDG HTML negotiates HTTP/2 while retaining
  the shared query, proxy, retry and parser policies. The lazy transport closes
  each response/client and bounds decoded HTML and remaining attempt time.
- **Multi-engine search (parallel)**: DuckDuckGo (HTML + API), Brave, Bing,
  SearXNG, Marginalia. Native site capabilities are **explicit only**: use
  `engines=['Site:catalog']` with a registered site provider, or
  `engines=['Xiaohongshu']` with an available session. Defaults never discover
  or invoke these sources. Explicit selections (including `[]`) never widen
  to other engines or generic browser search; unknown/unavailable selections
  return empty results with `_search_diag`. Native queries reject Web
  operators such as `site:` (even matching domains) and freshness rather than
  silently discarding constraints. Use a Web engine for those constraints.
- **Vertical / structured search**: auto-detects CVE IDs, arXiv IDs, DOIs,
  stock tickers, PyPI/npm packages, GitHub repos, IP addresses, Hugging Face
  daily papers, and Semantic Scholar related-work — answered from the relevant
  free API alongside web results.
- **Three-stage deduplication**: canonical URLs (fragments/tracking parameters
  removed), SERP title/snippet similarity, then fetched-body mirror detection.
  Cross-engine hits are retained as ranking consensus instead of discarded.

- **Automatic evidence quality**: distinguish substantive lexical evidence,
  weak generic-word matches and unverified candidates. Structured constraints
  and outbound link inventories do not count as subject evidence. Bounded
  recovery preserves the original query/freshness; explicit engines never widen.
  Site scopes constrain both initial results and one-hop links. Lexical evidence
  is not factual verification; uncertain candidates remain available for follow-up.
- **Concurrent page fetching**: Race-to-N strategy with SSL fallback + a
  per-domain circuit breaker.
- **Adaptive proxy strategy**: when a proxy is available each engine tries
  BOTH network paths (proxied ↔ direct) and remembers which one worked, so a
  container without a proxy env var, a host with a stale/dead proxy, or a
  soft-blocked egress IP no longer silently returns "no matches". A no-op
  (single direct attempt) when no proxy is configured.
  soft-blocked egress IP no longer silently returns "no matches". A no-op
  (single direct attempt) when no proxy is configured. A failover CHAIN of
  extra proxies can be configured behind the primary
  (`proxy_fallback_urls`): once the first path fails, the remaining paths
  are raced in PARALLEL and the first genuine success wins (and is
  remembered), so a dead primary costs milliseconds instead of a timeout
  per path.
- **One-hop deepening** *(opt-in)*: follow the best query-relevant outbound
  links one hop deeper, bounded like a crawl budget.
- **LLM content filter** *(optional)*: a few-token relevance verdict by default;
  opt-in `rewrite` mode can regenerate cleaned text. With no LLM, extracted
  text passes through unchanged.
- **BM25 + authority/coverage reranking**: pure Python, no external API calls;
  includes search-engine consensus, primary-source preference, entity coverage,
  and a two-result-per-domain diversity pass. Query-focused body passages bring
  late-document evidence into ranking; Unicode matching is shared with excerpts.
- **Token-bounded MCP context**: full pages are used for ranking, while
  `web_search` returns query-focused excerpts under one shared 18k-character
  budget by default. Complementary passages compete with repeated text while
  preserving original wording and marking gaps. See
  [quality contracts and reproducible measurements](docs/relevance-quality.md).
- **SPA / bot-protection support**: optional Playwright fallback for
  JS-rendered and challenge pages.
- **Browser evidence fidelity**: anonymous and authenticated rendering share
  bounded source projection and ordinary HTML extraction. Scripts and hidden
  content are excluded; links, code, tables and open shadow/slot text survive.
  Empty application main regions trigger recovery instead of treating menus
  as complete source. The browser dependency and executable remain optional.
- **PDF extraction**: optional pymupdf / pymupdf4llm integration.
- **Streamed HTML reads**: recognized React/Fizz completion segments already
  received in the response are restored before extraction and anchor selection.
  This uses no page JavaScript or browser; client-only content still needs
  rendering, and ordinary hidden content remains excluded.
- **HTML section reads**: a recognized `#fragment` focuses extraction on its
  heading section or definition, including code, tables and links. Unresolved
  or ambiguous anchors keep the whole page; remove the fragment to request the
  whole page explicitly. Plain-text readers and browser body-text responses
  may still return the complete document.
- **File assets**: text-based assets (SVG, JSON, XML, YAML, CSS, JS, source
  code) are returned as their raw source by `fetch_page_content`; binary
  assets can be downloaded with the size-/SSRF-guarded `fetch_url_bytes`.
- **Fetch failure diagnostics**: `fetch_page_content(url, diag=diagnostics)`
  still returns text or `None`. The mapping carries `reason`/`detail`, plus
  `http_status` and optional `retry_after` on HTTP failure. `retry_after` is
  raw response data capped at 64 characters, not an instruction or replay
  permission. The fetch-owned fields are cleared at the next call. Consumers
  can explain an unavailable source without downloading it again as a file.
- **Focused page reads**: pass `query` to `fetch_url`, `fetch_page_content`, or
  MCP `fetch_page` to select original passages from a long source within
  `max_chars`. This is local lexical selection, with explicit gaps and no
  model call. Empty queries keep ordinary reading; Python `max_chars=0`
  retains the whole received source. Focused reads bypass the preview cache,
  which may hold only a prefix, so they can incur a new bounded fetch even
  when an ordinary read would hit that cache. Omitted text does not prove
  absence. See [evidence selection](docs/relevance-quality.md#focused-page-reads).
- **Safe failure observations**: delivery adapters can project a fetch's `diag`
  with `tofu_search.fetch.diagnostics.fetch_failure_observation`. Known reasons
  and HTTP statuses remain actionable; private exception details and headers
  stay outside the projection. MCP uses the same owner. These facts grant no
  retry or access permission.
- **Citation verification**: check a bibliography for hallucinated references
  against CrossRef / arXiv / Semantic Scholar, with zero LLM calls.
- **Site readers**: pluggable per-domain handlers that read a site through its
  public endpoint instead of scraping the page.
- **Host integration seams**: register a browser provider (fetch/search via a
  real browser the user controls), a read-only site-search provider (host-owned
  authenticated adapters), and an auth-source provider (cookies/proxy for
  login-walled domains) — all no-ops by default.

## Quick Start

```bash
pip install tofu-search
```

### Basic search (no LLM required)

```python
from tofu_search import search

results = search("Python asyncio tutorial")
for r in results:
    print(f"{r['title']}: {r['url']}")
    if r.get('full_content'):
        print(f"  {r['full_content'][:200]}...")
```

### With OpenAI content filtering

```python
from tofu_search import search, configure

configure(
    llm_api_key="sk-...",
    llm_base_url="https://api.openai.com/v1",
    llm_model="gpt-4o-mini",
)

results = search("Python asyncio tutorial")
```

### With a custom LLM callable

```python
from tofu_search import search, configure

def my_llm(messages, **kwargs):
    # Your LLM call — receives OpenAI-format messages.
    # kwargs may include: stop, temperature, timeout
    return "response text"

configure(llm_function=my_llm)
results = search("Python asyncio tutorial")
```

### Fetch a single URL

```python
from tofu_search import fetch_url

content = fetch_url("https://example.com")
if content:
    print(f"Got {len(content)} characters")
```

Text-based file assets (SVG, JSON, XML, YAML, CSS, JS, source code) are
returned as their raw source — there's nothing to "extract":

```python
svg = fetch_url("https://example.com/icon.svg")   # the <svg>…</svg> markup
```

For **binary** assets (images, archives, fonts, Office docs) use
`fetch_url_bytes`, which returns the undecoded body + content-type under the
same scheme / SSRF / size-cap policy as the text pipeline:

```python
from tofu_search import fetch_url_bytes, looks_like_text_asset

got = fetch_url_bytes("https://example.com/logo.png")
if got:
    raw, content_type = got
    open("logo.png", "wb").write(raw)

looks_like_text_asset("https://example.com/a.svg")   # True
looks_like_text_asset("https://example.com/a.png")   # False
```

### Vertical (structured-identifier) search

```python
from tofu_search import detect_vertical_intent, search_vertical

domain, identifier, params = detect_vertical_intent("CVE-2021-44228")
record = search_vertical(domain, identifier, params)
print(record['content'])   # CVSS score, description, references from NVD

# Or force a domain-level fan-out (free-text → Hugging Face + Semantic Scholar):
from tofu_search import search_vertical_domain
print(search_vertical_domain('academic', 'mamba state space models')['content'])

# Which domains can serve a request right now? A domain whose handlers all
# need a missing credential is omitted, so you never advertise a dead one.
from tofu_search import list_domains
print(list_domains())        # ['academic', 'code', 'finance', 'security', ...]
```

## Citation verification

Detects likely-hallucinated references in a paper or `.bib` file by checking
each one against authoritative free catalogues — **no LLM calls**, just one or
two HTTP GETs per reference.

```python
from tofu_search import verify_bibtex, verify_references, summarize

results = verify_bibtex(open('refs.bib').read())   # or verify_references(text)
summary = summarize(results)

if summary['has_suspicious']:
    for r in summary['suspicious']:
        print(r['citation']['title'], '—', r['evidence'].get('reason'))
```

The verdict is deliberately **three-state**, not a boolean, because "we could
not find it" is not the same as "it is fake":

| Verdict | Meaning |
|---|---|
| `verified` | An authoritative record matches the claim. |
| `suspicious` | High-confidence contradiction — a concrete DOI/arXiv ID that definitively does not resolve, or resolves to a *different* paper. |
| `unverifiable` | Could neither confirm nor refute (no identifier, catalogue coverage gap, book/dataset, rate-limit, transport error). **Never** report these as fabrications. |

Only a claim carrying a concrete identifier can ever be `suspicious`. A
title-only claim that fails to match is a coverage gap, so it degrades to
`unverifiable`. Each result carries an `evidence` dict with the exact catalogue
URL checked, the matched title and a similarity score — quote those when
explaining a verdict rather than asserting it bare.

Lower-level pieces are exported too: `parse_bibtex` / `parse_references` turn
text into `Citation` objects, and `verify_citations` verifies a list of them
concurrently.

## Site readers

Some sites expose a public endpoint that returns cleaner content than their
rendered page. A `SiteReader` claims a domain and handles it before the generic
fetch pipeline runs.

```python
from tofu_search import SiteReader, register_reader

class MyReader(SiteReader):
    name = 'example'
    def matches(self, url): return 'example.com' in url
    def read(self, url, *, max_chars=None, timeout=15): ...

register_reader(MyReader())
```

## Owned Playwright rendering

`get_playwright_pool()` returns the lazy process-owned pool. Built-in page fetch,
authenticated search and PDF work run as independent coroutines with isolated
contexts on one async browser; no task-count slot or serial execution queue
limits them. `submit_task(kind, payload)` returns an owned call with `result`,
`cancel_and_join` and completion callbacks. A timeout caller cancels and joins
actual page/context and blocking-helper cleanup before returning.

Hosts register async callbacks with `register_async_task_kind`. Existing
`register_task_kind` synchronous callbacks keep their sync browser API in a
demand worker that owns and closes its own browser. `shutdown()` stops admission,
cancels active calls, drains physical helpers, then closes the shared browser,
driver and loop. Unused pools create no browser or owner thread. Browser launch
failure remains an unavailable capability with its existing retry cooldown.

Anonymous browser reads reuse the first route in the HTTP fetch plan, including
its learned preference and the effective per-call search configuration.
Environment proxy selection and `NO_PROXY` apply to the initial URL; all
redirects and subresources then share that browser context's route. HTTP(S)
proxy userinfo is passed through Playwright's authentication fields. This adds
no browser retry. Authenticated fetch/search continue to use only their source's
proxy, and self-contained PDF rendering does not consult the search proxy.

Rendered source requires a successful main-document response. A later successful
navigation during the existing wait may recover an initial error; subframe,
image and fetch/XHR errors cannot reject an otherwise successful document.
Navigation overlapping extraction invalidates that snapshot. The remaining
text/challenge guards still apply; an HTTP 200 alone does not prove useful content.

## Host integration (provider seams)

The standalone library never imports a host application. To unlock the two
host-only capabilities, register a provider — dependency points inward (host →
library), exactly like a plugin.

Eligible connected browser fallbacks call the bound provider directly; no local
concurrent-call count discards a request. The provider owns browser access and
physical call completion, while per-call timeouts and text checks still apply.

```python
from tofu_search import (
    BrowserProvider, SiteSearchProvider, AuthSourceProvider,
    register_browser_provider, register_site_search_provider,
    register_auth_source_provider,
)

class MyBrowser(BrowserProvider):
    def is_connected(self): return True
    def fetch_url(self, url, *, max_chars=None, timeout=15): ...
    def search(self, query, *, max_results=8): ...

class MyAuth(AuthSourceProvider):
    def match_source(self, url): ...      # → {'domain','cookies','proxy',...} | None
    def get_source(self, domain): ...

class MySites(SiteSearchProvider):
    def bind(self): ...                    # capture request/user identity
    def list_sources(self): ...            # read-capable sources only
    def search(self, source_id, query, *, max_results=10, freshness=''): ...

register_browser_provider(MyBrowser())       # last-resort fetch/search fallback
register_site_search_provider(MySites())     # authenticated site adapters
register_auth_source_provider(MyAuth())      # cookies for login-walled domains
```

When no provider is registered, the browser fallback and authenticated fetch
paths are inert no-ops — the anonymous HTTP + Playwright pipeline runs as normal.

## Configuration

```python
from tofu_search import configure

configure(
    # Search / fetch settings
    fetch_top_n=6,                 # Max results to return
    fetch_timeout=15,              # HTTP timeout per request (seconds)
    fetch_max_chars_search=60000,  # Max chars per page in search results
    fetch_max_chars_direct=200000, # Max chars for direct fetch_url()

    # Optional self-hosted SearXNG (preferred before public fallbacks)
    searxng_url="http://searxng:8080",
    searxng_engines="",            # Empty: respect the instance configuration
    mcp_content_budget_chars=18000,

    # LLM settings (for content filter)
    llm_api_key="sk-...",
    llm_base_url="https://api.openai.com/v1",
    llm_model="gpt-4o-mini",
    # Or a custom callable instead:
    # llm_function=my_callable,

    # Filter settings
    filter_enabled=True,           # Enable/disable LLM filter
    filter_min_chars=3000,         # Min chars to trigger LLM filter

    # Proxy (adaptive dual-attempt + failover chain + parallel racing)
    proxy_url="",                  # Explicit proxy; empty ⇒ use env vars
    proxy_dual_attempt=True,       # Try proxied ↔ direct + learn which works
    proxy_fallback_urls=[],        # Extra proxies tried (raced) after the primary
    proxy_race=True,               # Race remaining paths in parallel on failure
)
```

Many settings also read from environment variables: `FETCH_TOP_N`,
`FETCH_TIMEOUT`, `FETCH_MAX_CHARS_SEARCH`, `FETCH_MAX_CHARS_DIRECT`,
`FETCH_MAX_CHARS_PDF`, `FETCH_MAX_BYTES`. One-hop deepening is enabled with
`SEARCH_DEEPEN_HOPS=1` (or per call: `perform_web_search(..., deepen=True)`).
Semantic Scholar raises its rate limit with `SEMANTIC_SCHOLAR_API_KEY`. The
adaptive proxy honours `TOFU_SEARCH_PROXY_URL` and
`TOFU_SEARCH_PROXY_DUAL_ATTEMPT` (plus the standard `https_proxy` /
`http_proxy` / `all_proxy`), the failover chain reads
`TOFU_SEARCH_PROXY_FALLBACK_URLS` (comma/whitespace separated) and the
racing switch reads `TOFU_SEARCH_PROXY_RACE`.
For a stable, broader free backend, set `TOFU_SEARCH_SEARXNG_URL`; optionally
pin its comma-separated engine list with `TOFU_SEARCH_SEARXNG_ENGINES`. See the
[open-source backend review](docs/search-backends.md) for deployment tradeoffs.

## Pipeline

`perform_web_search` runs an overlapping streaming pipeline:

1. **Multi-engine search**: engines fire in parallel; each engine's URLs are
   deduped and submitted to the fetch pool the moment they arrive (the first
   page fetch starts before slow engines finish). When a proxy is configured
   each engine adaptively tries both the proxied and direct network path and
   learns which one works (see the adaptive-proxy feature above).
2. **URL aggregation**: canonicalize scheme/default ports/fragments/tracking
   parameters, merge engine evidence, and accumulate reciprocal-rank consensus.
3. **Snippet groups**: retain the first representative for snippet-only
   ranking and preserve similar discovered URLs as bounded follow-up candidates.
   Jaccard overlap does not establish page equivalence or corroboration.
4. **Page fetch**: rank a bounded candidate set, reserve streaming capacity
   across engines, then concurrent race-to-N with SSL retry, circuit breaker,
   and Playwright fallback for SPA/bot-protection pages.
   - **4a. Full-content dedup**: remove mirrors/syndicated copies before any
     optional LLM call. Already received alternative bodies participate even
     when their search snippets were grouped together.
   - **4b. Deepen** *(opt-in)*: one hop along the best query-relevant links.
5. **LLM content filter** *(optional)*: bounded relevance verdict by default;
   opt-in rewrite mode can regenerate cleaned text.
6. **BM25/authority/consensus rerank**: select a relevant, source-diverse top-N.
7. **MCP passage selection**: keep the full library result, but send ChatUI only
   the best query-focused passages under a shared context budget.

Step 5 is automatically skipped when no LLM is configured.

Optional `similar_snippets` metadata shares one `max_results` identity allowance
across primary results and the uncertainty preview. It exposes exact observed
URLs for follow-up fetching; `similar_snippets_omitted` reports further listings
outside the allowance. Related snippet text uses only the content budget left
after primary evidence. See the [evidence contract](docs/relevance-quality.md#snippet-groups-and-fetched-evidence).

Uncertain follow-ups use the same query-aware ranking before the three-entry
preview is selected. Their evidence status remains uncertain, and the received
candidate sidecar stays complete. `rerank=False` retains their arrival order.

## Optional Dependencies

```bash
# SPA / JS-rendered page support
pip install tofu-search[playwright]
python -m playwright install chromium

# PDF extraction
pip install tofu-search[pdf]

# Everything
pip install tofu-search[all]
```

Or just run `./install.sh` (see below).

## Install script

```bash
./install.sh            # core deps
./install.sh --all      # core + playwright + pdf, and installs chromium
./install.sh --playwright
./install.sh --pdf
```

## Run as an MCP server

The library can be exposed to any MCP client (Claude Desktop, IDE agents,
other agent frameworks) as a plugin.

```bash
pip install "tofu-search[mcp]"
tofu-search-mcp                      # stdio, for a local plugin
tofu-search-mcp --transport http     # Streamable HTTP (localhost:8000)
```

Add to a client config:

```json
{"mcpServers": {"tofu-search": {"command": "tofu-search-mcp"}}}
```

It exposes four tools — `web_search`, `fetch_page`, `search_vertical`,
`verify_citations` — plus a `health://status` resource. The surface is
deliberately narrow: `configure()` and the `register_*` seams are NOT tools,
because a per-request caller must not be able to change global state or
supply implementations.

The server uses MCP SDK v2 and speaks the current `2026-07-28` stateless
protocol while accepting legacy `2025-*` initialize/session clients on the
same stdio or Streamable HTTP endpoint. No ChatUI configuration split is
required during migration.

`web_search` accepts `content_budget_chars` (4,000–60,000). `0` uses
`mcp_content_budget_chars` / `TOFU_SEARCH_MCP_CONTENT_BUDGET_CHARS`, whose
default is 18,000 characters across all result bodies. Source metadata and URLs
are always retained, so a model can call `fetch_page` when one source needs more
depth instead of paying for every full page up front. This is a character cap,
not an exact token count; CJK and Latin text tokenize differently.

`fetch_page` keeps the common fetcher's safe failure category in its unavailable
reply, including HTTP status, timeout, access challenge and extraction failure.
It does not expose raw exception details or response headers, and an unknown
category retains the generic unavailable reply. Browser rendering and PDF
extraction depend on optional capabilities; neither guarantees source access.

Design constraints an embedder should know (each enforced by a test):

- **stdio is safe.** The library never writes to stdout; logs go to stderr.
- **One process.** `--workers > 1` refuses to start. The per-engine throttle
  and circuit-breaker state are per-process singletons, so N workers would
  multiply the real request rate to every engine by N.
- **Demand concurrency.** Eligible searches and page work start independently;
  `--max-concurrency` is a legacy idle hint. Health reports `limit=0` and actual
  running/resident counts. Engine rate policy and caller deadlines remain.
- **Synchronous core.** MCP awaits owned demand workers, keeping the asyncio
  loop responsive. Cancellation retains a call until its worker returns;
  transport shutdown joins published work.

## Concurrency & thread-safety

Read this before embedding the library in a server — it is the difference
between a working deployment and one that silently gets rate-limited.

**The library is synchronous.** There is no `async` API. `perform_web_search`
blocks for as long as the pipeline runs (bounded by `search_deadline_secs`,
default 45s). To call it from an asyncio application, push it to a worker
thread (`anyio.to_thread.run_sync` / `loop.run_in_executor`) — calling it
directly on the event loop will stall every other task.

**It is internally concurrent.** Engine, fetch, filter and verification work
uses demand workers; old worker numbers are idle hints rather than active-call
ceilings. One blocked call does not take a slot from another request. Actual
endpoint rate policies, configured result targets and network deadlines retain
their authority. Per-item startup failures retain sibling results, and close
joins accepted work. The packaged private worker implementation is generated
from the standard-library-only Tofu kernel, so standalone imports need no host
modules or sibling checkout.

**Its rate-limiting state is per-process.** The per-engine request throttle,
the engine circuit breaker, the domain circuit breaker, the fetch cache, the
Playwright pool and the registered providers are all module-level singletons
created at import time. They are thread-safe, but they are **not** shared
across processes. Running several worker processes therefore multiplies your
request rate to every engine while each process still believes it is respecting
`min_request_interval_ms` — which is exactly how a search engine starts
answering `202 (rate-limited)` and a whole batch comes back empty. **Run one
process** and scale with the in-process thread pools; if you genuinely need
multiple processes, externalize the throttle state first.

**Configuration is global; overrides are per-call.** `configure()` mutates
process-wide state and is meant to be called ONCE at startup. For anything that
varies per request, pass keyword overrides to `search(...)` — those are applied
to a copy and never touch the global config. In a multi-tenant server, calling
`configure()` per request would let one caller change another caller's search
behaviour.

**Logging goes to stderr, never stdout.** The library attaches a stderr handler
only when the root logger has none, and otherwise defers to the host's logging
configuration. Nothing in the package writes to stdout, which keeps it safe to
embed in a process whose stdout carries a protocol stream.

## Contributing

Work on a branch and open a pull request; run `ruff check .` and `pytest`
before pushing.

Do **not** edit this repository from several concurrent sessions sharing one
working tree. It is now a standalone component with its own release cycle, and
parallel in-place edits produce transient half-written states — a module that
imports a symbol its dependency has not gained yet will fail collection for
everyone, and the failure looks like a real bug rather than a race. One tree,
one editor, one branch.

## License

MIT


## Observation quality

Web search enforces explicit `site:` host/subdomain and path scopes, including
negative scopes, before fetching. Multiple positive scopes admit their union.
Wrong-scope URLs are excluded even at the fetch recall floor. Ranking and
fetch admission do not establish relevance or factual correctness.

A `SearchResultList` remains list-compatible. `_search_diag` now accompanies
both nonempty and empty results and reports result-bearing, empty, failed and
unavailable engines, rejected scope candidates, usable results and up to three
unverified candidate URLs. `_unverified_candidates` retains the full bounded
uncertain set for host artifact capture and standalone inspection. Circuit-open/unconfigured engines are unavailable,
not genuine no-matches. Unparseable large HTTP-200 engine responses remain
failures rather than evidence of absence. Formatting carries these distinctions
through library, host and MCP delivery; no extra model call is required.

Snippet candidates with no lexical overlap are excluded from the useful-result
list but remain available in bounded diagnostics. This is uncertainty, not a
semantic rejection: retrieved page content can preserve a cross-language or
alias candidate. Successful fetch alone is not factual verification. Empty or
partial search never proves a subject does not exist; follow a narrowed query
or an authoritative index using the existing bounded search/fetch interfaces.

HTML extraction supplements article text with received table rows and otherwise
omitted copy-button/preformatted text from the existing content DOM. It preserves
row relationships and identifiers without another request or model. Fetch byte,
character and deadline limits still apply.

Verification: `tests/test_observation_quality.py`, neighboring search-quality,
engine/proxy, content extraction and MCP delivery suites.
