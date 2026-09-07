# Changelog

## 0.10.3 — 2026-08-21

### Fixed
- **The fetch layer now honours the configured proxy.**
  ``fetch/http.py::do_request`` — the single low-level GET path behind
  ``fetch_page_content`` / ``fetch_url`` / ``fetch_url_bytes`` — never
  passed ``proxies=`` at all: engines searched fine through the proxy
  while every page fetch went direct. On a host without direct egress that
  silently killed ALL full_content (DNS resolution failures; 2026-08-21
  incident: search returned 5 results, zero page bodies). ``do_request``
  now shares the search side's adaptive proxy plan
  (``search/proxy_mode.py``) keyed per HOST: proxy chain → direct fallback
  on connect-level failures (ProxyError/SSLError/ConnectTimeout/DNS),
  sticky per-host learning, and an answered HTTP status never path-switches
  (a real 403 is a page answer, not a network fault). The no-br decoding
  retry reuses the winning path. With no proxy configured the request is
  byte-identical to before (one direct attempt, no ``proxies=`` kwarg).
  Verified live: ``proxy-path learned: fetch:github.com -> proxy`` and page
  bodies flowing through the proxy where every fetch previously failed.
- Tests: ``tests/test_fetch_proxy.py`` (5) — no-proxy byte-identity,
  proxy-dead→direct fallback (+ learning), direct-dead→proxy fallback,
  dual-off single attempt, HTTP status never path-switches.

## 0.10.2 — 2026-08-21

### Fixed
- **SearXNG: total blockage is now an engine ERROR, never a fake "no
  matches".** The engine hand-rolled its own request loop outside
  ``_common.http_search_get`` and returned ``[]`` for every failure mode —
  429 rate-limits, 302 bot-block redirects, connect timeouts, SSL errors,
  and 200-OK anti-bot interstitials (Anubis "Making sure you're not a
  bot!", Cloudflare "Just a moment"). The orchestrator therefore classified
  SearXNG as ``engine_empty`` while every default instance was in fact
  blocked/dead (probed 2026-08-21 through the proxy egress: 5×429, 1×SSL,
  1×timeout, 1×Anubis) — a dead engine masquerading as the one survivor.
  The engine now detects bot-wall pages, tracks genuine vs blocked
  per-instance outcomes, and RAISES ``requests.RequestException`` when
  every tried instance was blocked/unreachable (same contract as
  ``_common.http_search_get``); ``[]`` is returned ONLY for a genuine
  no-match or a circuit-breaker skip. It also joins the shared engine
  circuit breaker (3 consecutive total blockages → 120s bench) instead of
  paying its full timeout budget on every query.
- **SearXNG defaults pruned + wider rotation.** Removed instances verified
  dead from a non-JS datacenter client (charliewhiskey: SSL; freestater:
  connect timeout; hbubli: Anubis PoW). Public-instance attempts per call
  raised 2 → 4 — with most of the list rate-limiting datacenter IPs, two
  random picks almost never land on an open instance.
- Tests: ``tests/test_searxng.py`` (6) — all-429 raises, bot-wall counts as
  blocked, genuine empty page returns [], JSON results reset the circuit,
  breaker trips after repeated blockage and benches without network,
  unconfigured engine returns []. ``test_cleanup.py``'s two searxng cases
  updated to the raise-on-total-blockage contract.

## 0.10.1 — 2026-08-21

### Fixed
- **Per-call config overrides now reach the engine/fetch layer.**
  ``search(query, **kwargs)`` and ``perform_web_search(config=...)`` built a
  per-call ``SearchConfig``, but code that runs in worker threads — the
  engine proxy plan in ``search/_common.py``, fetch, deepen — read the
  GLOBAL ``get_config()``. A per-call override (e.g. ``proxy_url``,
  ``proxy_dual_attempt``) was silently dropped: every engine went direct,
  and on a host without direct egress that reproduced as a total search
  outage, identical to a dead-proxy incident. ``perform_web_search`` now
  binds the resolved config via the new
  ``tofu_search.config.bind_call_config`` ContextVar for the whole call, and
  worker threads inherit it through ``submit_with_provider_context``'s
  ``copy_context``. The global config is untouched and restored on exit, so
  concurrent calls with different overrides no longer stomp each other.
- Tests: ``tests/test_per_call_config.py`` (4) — override scoping, worker
  thread propagation, the bite test (per-call ``proxy_url`` visible inside
  the engine function on the orchestrator's worker thread; global restored
  after), concurrent isolation.

## 0.10.0 — 2026-08-21

### Added
- **Multi-proxy failover chain with parallel path racing.** Until now the
  adaptive proxy layer knew exactly one proxy: when the primary died mid-run
  (an allowlist gateway refusing CONNECT with a tunnel `403`, or a hard-down
  endpoint), each engine's only recourse was the direct path — and on a host
  without direct egress every engine failed, surfacing as a total search
  outage. Two new knobs fix it:
  - `SearchConfig.proxy_fallback_urls` (env `TOFU_SEARCH_PROXY_FALLBACK_URLS`,
    comma/whitespace separated) — an ordered, de-duped chain of extra proxies
    behind the primary. Each becomes its own labelled attempt
    (`proxy`, `proxy#1`, … — stable across calls so the per-engine sticky
    learning can pin ANY of them, and credential-free so a resolved URL's
    userinfo never reaches a log line), inserted between the primary and the
    direct path. Consulted only when `proxy_dual_attempt` is on; with it off
    the plan stays the single primary-proxy attempt.
  - `SearchConfig.proxy_race` (default on, env `TOFU_SEARCH_PROXY_RACE`) —
    once the FIRST planned path fails at connect level and TWO OR MORE
    alternates remain, they are raced CONCURRENTLY (small process-wide thread
    pool) and the first genuine success wins. A dead primary usually fails in
    milliseconds, but a wedged one would otherwise cost a full per-request
    timeout before the next path was even tried; racing bounds failover at
    ONE timeout instead of N. Losers are abandoned in flight and never touch
    the learning; completed losers still unpin a pinned-but-dead path.
  - Single-proxy deployments are byte-identical to 0.9.0 (one remaining
    alternate never triggers a race), and the per-engine throttle is stamped
    once per LOGICAL request — racers are one request, not N.
- Tests: `tests/test_proxy_failover_race.py` (9) — chain ordering/dedup,
  env-derived primary + explicit fallbacks, plan labels/kwargs, sticky pin on
  a fallback proxy, a STRUCTURAL parallelism proof (a rendezvous event that a
  sequential executor provably fails), race-disabled sequential chain,
  soft-block loses to a healthy path, and the NC bites (dual-off ignores the
  chain; all racers failing RAISES — never misreported as "no matches").

## 0.9.0 — 2026-08-14

### Added
- Query-focused passage selection for the LLM relevance gate and MCP output.
  `web_search` now shares a configurable 18,000-character body budget across
  sources while the ranker still sees full fetched pages.
- Preferred self-hosted SearXNG configuration (`TOFU_SEARCH_SEARXNG_URL` /
  `TOFU_SEARCH_SEARXNG_ENGINES`). Empty engine selection now respects the
  instance configuration instead of hard-coding Google/Bing/DuckDuckGo.
- MCP read-only/open-world tool annotations and explicit context-budget schema.
- MCP 2026-07-28 support through the stable Python SDK v2, including direct
  stateless requests while retaining legacy ChatUI handshake compatibility.

### Changed
- URL normalization removes fragments, default ports and known tracking query
  parameters; duplicate engine hits contribute reciprocal-rank consensus.
- Ranking now combines title-weighted BM25, primary-source authority, engine
  consensus, successful extraction, entity coverage and host diversity.
- Fetch work is capped at `max(12, max_results * 3)` candidates and streaming
  submission reserves capacity across engines.
- The minimal outbound MCP vertical client sends modern per-request metadata
  and routing headers first, with a legacy initialize/session fallback.

### Fixed
- Race-to-N and engine timeout paths no longer rejoin the very slow worker
  threads they had just cancelled, so their advertised latency bounds now hold.
- Empty-result retries and the 25-second browser fallback are not started when
  the remaining whole-search deadline cannot accommodate them.
- Fetched mirrors/syndicated copies are removed before optional LLM filtering.

## 0.8.0

### Added
- **FlyAI (飞猪) travel provider — the travel vertical's anonymous backend
  (flight AND hotel now work with zero configuration).** RollingGo ended
  anonymous flight access on 2026-08-04 (its endpoint now answers 401 to
  keyless calls) and its hotel endpoint was always credentialed, which left
  the whole `travel` domain dark without `ROLLINGGO_API_KEY`. The public
  flyai-cli npm package (`@fly-ai/flyai-cli`) ships a built-in trial
  credential and signs its MCP calls with a documented-in-bundle HMAC scheme;
  the new `travel_flyai.py` re-implements that exact wire protocol on the
  proxy-aware vertical HTTP layer (no Node runtime, no shell-out) and both
  type handlers now run a provider chain: RollingGo first when
  `ROLLINGGO_API_KEY` is set (paid quota, richer args), FlyAI as the
  anonymous fallback — and the only path when no key is configured. FlyAI
  flights come back WITH booking links (`bookable: true`, previously the
  flight vertical could only say "query only"); trial-tier hotel prices are
  masked upstream (`¥2xx`) and the content says so honestly. Override the
  bundled credential via `FLYAI_API_KEY` / `FLYAI_SIGN_SECRET` (new config
  fields `flyai_api_key` / `flyai_sign_secret` / `flyai_mcp_endpoint`). A 401
  from FlyAI latches the provider off for the process, mirroring the old
  RollingGo latch — anonymous upstreams are ephemeral by nature, the chain
  absorbs it.

### Fixed
- **Handler-level travel tests rotted with the calendar.** `travel_slots` is
  deliberately clock-free (`today` is injected), but the flight/hotel
  handlers called `date.today()` directly, so every handler test pinning a
  literal query date (e.g. `2026-08-03`) began failing the day the calendar
  moved past it. The clock read now sits behind a one-line `_today()` seam
  in both handlers and the test suite pins it.

## 0.6.0

### Added
- **`filter_mode` — the LLM content filter gains a verdict-only `gate` mode
  (new default).** The pre-0.6 filter asked the model to REGENERATE the whole
  cleaned page (`rewrite`): output tokens ≈ page length, so generation time
  dominated at 10-60s+ per page and a 6-page search step-5 routinely took
  30-120s — users reported the filter made search "practically unusable".
  `gate` mode asks only for the relevance verdict (`[USEFUL]` /
  `§§IRRELEVANT§§`, a handful of output tokens) on the CAPPED head of the
  page (new `gate_input_max_chars`, default 12,000), and useful pages keep
  their ORIGINAL extracted text — boilerplate removal stays with the
  (already applied) HTML extractor. `filter_mode='rewrite'` preserves the
  old behaviour for maximum cleaning quality. An unknown mode logs a
  warning and falls back to `gate`.
- **Result cache for the LLM filter** (`filter_cache_ttl` 600s /
  `filter_cache_max_size` 500, 0 disables), keyed on
  (mode, url, query, user_question, raw_text) via the same TTL+LRU
  `_FetchCache` pattern as the fetch cache. A repeated search of the same
  pages costs zero LLM calls. Failures are never cached.

### Fixed
- **Empty LLM responses no longer drop pages or poison the filter cache.**
  An empty/whitespace completion was treated as an irrelevance verdict: the
  page vanished from results AND the bogus verdict was cached for the TTL.
  An empty completion is an anomaly (rate-limited empty, content-policy
  refusal, gateway truncation), not a verdict — it now fails open (raw text
  served, nothing cached), consistent with the exception path. Explicit
  irrelevance verdicts are unchanged (sentinel + cached).

### Changed
- **`filter_timeout` default 300 → 45s.** The old ceiling let a single
  wedged LLM call stall step 5 for five minutes. On timeout/error the raw
  text is served (filtering is an enhancement, never a blocker) — 45s is
  generous for gate mode; raise it when running `rewrite` on big pages.
- **The orchestrator no longer forces `min_chars=0`.** Step 5 passed the
  override explicitly, silently disabling the `filter_min_chars=3000`
  short-circuit so EVERY page — however short — paid an LLM call. Short
  pages skip the filter again.

### Measured
- `examples/bench_content_filter.py` (fake LLM, documented latency model:
  20k chars/s prompt, 600 chars/s generation, 6 pages = 4×60k + 10k + 1.5k
  chars): BEFORE (rewrite + min_chars=0 + no cache) step5 ≈ **96.7s
  simulated, 6 LLM calls** → AFTER (0.6.0 defaults) ≈ **0.82s, 5 calls**
  (117×) → warm cache ≈ **0.08s, 0 calls** (1288×).
- Tests: `tests/test_content_filter_modes.py` (15) — gate verdict paths
  (useful/irrelevant/both sentinel forms), input capping, prompt
  separation, unknown-mode fallback, min_chars short-circuit (unit, batch,
  and an orchestrator source pin), rewrite parity, error fallback, cache
  hit / irrelevant caching / mode in the key / ttl=0 disable.

## 0.5.3
# Changelog

## 0.5.4

### Fixed
- **PDF extraction no longer degrades on OCR-triggering pages.** `pdf_extract`
  called the top-level `pymupdf4llm.to_markdown`, which (pymupdf4llm ≥1.26,
  with `pymupdf.layout` importable) routes through the NEW layout/OCR
  pipeline. That pipeline's OCR adapters call `RapidOCR.text_detector` — an
  attribute that only existed on rapidocr-onnxruntime ≤1.2; modern 1.3.x and
  1.4.x both name it `text_det`. Every page whose layout analysis votes
  needs_ocr (bad glyphs, or scan-like images with high variance/edge energy)
  crashed with `'RapidOCR' object has no attribute 'text_detector'` and the
  WHOLE document fell back to raw `get_text` — measured ×21/day in
  production, reproduced on arXiv 1706.03762 (crash → 39,512 raw chars).
  Upstream 1.28.0 keeps the same broken call behind a louder RuntimeError,
  so upgrading is not a fix. The extractor now calls the classic
  implementation at `pymupdf4llm.helpers.pymupdf_rag.to_markdown` directly —
  the seam that honors `page_chunks` / `table_strategy` / `show_progress`
  and never touches the OCR pipeline (same seam the host project's own PDF
  parser uses). Measured after the fix on the same PDF: 40,608 chars of rich
  markdown with 35 table rows.
- Tests: `tests/test_pdf_extract_classic_seam.py` (3) — a hermetic
  needs_ocr trigger PDF (synthetic noise image matching analyze_page's
  variance/edge signature) must keep rich markdown with no raw-fallback
  warning; the extract path must ride the rag seam with the kwargs intact
  (top-level call is a test-failing regression); a source pin guards the
  wiring. NEUTER-verified: reverting the call site to top-level turns the
  two behavioural tests red.

## 0.5.3

### Added
- **`allow_private_hosts` — an explicit, HOSTNAME-anchored SSRF exemption.**
  The SSRF guard (`block_private_addresses`) is unconditional, so an internal
  host was unreachable no matter how deliberately the operator wanted it. The
  only way through was a side effect: a registered auth-source short-circuits
  the gate, which meant *connecting an account silently granted an SSRF
  exemption* — a permission travelling on the wrong noun. `allow_private_hosts`
  makes the intent first-class and keeps the two gates separate.
  Anchored on the HOSTNAME, never the resolved IP: an internal load balancer
  rotates its address between lookups (one host was observed answering as both
  `10.176.18.71` and `10.192.19.176` minutes apart), so an IP allowlist rots
  silently while the hostname stays true. Matching is exact or parent-suffix on
  a DOT BOUNDARY (`example-corp.com` admits `your-llm-gateway.example.com`, but never
  `evil-example-corp.com`), and it is consulted AFTER the literal-IP branch so
  naming a host can never launder a bare-IP target. Default empty ⇒ byte-identical
  to 0.5.2.
- **Failure-reason propagation (`diag` out-param).** `fetch_page_content()`
  returned `str | None`, so SSRF-blocked / skip-domain / circuit-open / HTTP
  status / timeout / SPA-shell / bot-wall all collapsed into one
  indistinguishable "Failed to fetch" at the tool surface — the pipeline knew
  the cause and threw it away. Callers may now pass `diag={}` to receive
  `reason` (stable token) and `detail` (human sentence). `_should_fetch()`
  gained the same optional out-param. Both are strictly additive: the parameter
  is optional, and the `_should_fetch` monkeypatch seam tolerates a substituted
  1-arg predicate, so existing callers and test doubles are unaffected.
- Tests: `tests/test_ssrf_allowlist.py` (19) — default posture unchanged,
  admission by exact/suffix entry, IP-independence across LB rotation, and the
  security boundaries (dot-boundary anchoring vs `evil-example-corp.com`, bare
  private/loopback/metadata IPs never admitted, blank entries never degrading
  into allow-all). Neuter-verified: removing the allowlist hook turns exactly
  the 4 admission tests red while all 15 boundary tests stay green.

## 0.5.2

### Added
- **Identity fallback seams — “got a shell, not content” now reaches the host
  browser.** Previously `try_browser_fetch` was only wired to TRANSPORT-level
  failures (HTTP 401/403/406/429/5xx, timeout, connection errors). A 200 that
  carries an SPA shell or a login wall — the dominant failure shape of
  SSO-protected sites — went: static GET → shell/wall detection → anonymous
  Playwright → `None`, and the host browser (the user's live, logged-in
  session) was never consulted. `fetch_page_content` now offers the URL to the
  registered `BrowserProvider` at five such dead-ends, each with a distinct
  `reason`: `spa_shell` (200 + too little extracted text), `login_wall`
  (bot/login wall in the raw HTML or in the extracted text), `known_spa`
  (known-SPA domain whose anonymous render came back empty), and
  `auth_source_failed` (an enabled auth-source replay yielded nothing — stored
  cookies may be expired while the user's live session is still valid; the
  anonymous pipeline only runs afterwards if the browser also yields nothing).
  All five sites go through the existing deadline-aware wrappers, so the
  per-URL budget still bounds the whole chain; with no provider registered
  (or unconnected) every path is byte-identical to 0.5.1. The five seam sites
  and their `reason` values above ARE the design contract; each is covered by a
  test in `tests/test_fetch_identity_fallback.py`.
- Tests: `tests/test_fetch_identity_fallback.py` (10) — drives the public
  `fetch_page_content` offline and asserts outcomes (exactly one browser call
  with the right reason; browser text wins; browser-empty preserves the
  pre-seam result incl. the >50-char partial-shell path; Playwright success
  never consults the browser). Neuter-verified: removing the `spa_shell`
  seam turns exactly its three tests red.

## 0.5.1

### Added
- **Per-engine request throttle (self-inflicted rate-limit guard).** Two
  CONCURRENT `perform_web_search()` calls (e.g. two parallel paper-recommend
  batches) could fire the same query at one HTML engine within the same second
  and trip its rate-limit — the observed DuckDuckGo `202 (rate-limited)` that
  emptied a whole batch. New process-global `search/_common.py::host_throttle`
  (`_HostThrottle`, mirroring `engine_circuit`) enforces a minimum interval
  between requests to the SAME engine, consulted inside `http_search_get` right
  after the circuit-breaker skip and just before the GET:
  - Per-engine locking — a wait on a busy engine never serializes a request to
    a DIFFERENT engine, so the orchestrator's engine+fetch overlap is preserved.
  - Upward-only jitter (`[0, +30%]` of the interval) so two colliding threads
    desynchronize instead of re-colliding on the next tick; realized spacing is
    always ≥ the configured interval.
  - The wait is clamped to the per-request `timeout`, so it consumes budget the
    caller already has and never pushes a query past its wall-clock deadline. A
    circuit-open engine returns BEFORE the throttle and spends zero interval.
  - Only the HTML-engine envelope is throttled. The arXiv / Semantic Scholar
    JSON vertical path uses a separate `http_get` and stays UNTHROTTLED — it is
    the breaker-independent fast path.
- `SearchConfig.min_request_interval_ms` (default 400, env
  `TOFU_SEARCH_MIN_REQUEST_INTERVAL_MS`). Set to 0 to disable the throttle
  (byte-identical to the old unthrottled path).
- Tests: `tests/test_host_throttle.py` (8) — same-engine spacing, different
  engines not serialized, jitter present + never below the interval floor,
  clamp-to-timeout, `http_search_get` ordering (throttle consulted for a
  healthy engine, skipped for a breaker-open one), plus two NEUTER bites
  (interval=0 removes spacing; removing the wiring fails the ordering test).

## 0.5.0

### Added
- **Hard wall-clock deadlines (robustness against wedged/dead hosts).** Two new
  `SearchConfig` knobs, both env-gated with safe defaults so a single-box
  install is unchanged (set to 0 to restore the old unbounded behaviour):
  - `search_deadline_secs` (default 45, env `TOFU_SEARCH_DEADLINE_SECS`) —
    total budget for one `perform_web_search()`. The ONLY prior caps were a 20s
    engine `as_completed` and a 90s fetch `as_completed`, and the 90s only
    short-circuits once `kept_ok >= target_ok` — a count a niche-domain query
    (mostly paywalled/dead hosts) never reaches, so the call hung the full 90s
    plus the LLM-filter/deepen/rerank tail. The deadline now bounds the
    fetch-wait loop (`min(90, budget_left)`), does NOT `shutdown(wait=True)` on
    a hit (that would re-introduce the hang), and short-circuits the
    filter/deepen/rerank stages. Force-returns partial results tagged
    `SearchResultList._deadline_hit=True`; a zero-result deadline sets
    `_search_diag['reason']='deadline'`. Emits a `[Fetch] ⏱ DEADLINE` /
    `[Search] ⏱ returned PARTIAL` log line.
  - `fetch_url_deadline_secs` (default 25, env
    `TOFU_SEARCH_FETCH_URL_DEADLINE_SECS`) — per-URL cap bounding the WHOLE
    fallback chain (HTTP body-download via a new `do_request(deadline_ts=…)`
    arg + browser + Playwright), so one dead host can't stack per-hop timeouts
    (body ≈ timeout×3, +browser 15-25s, +Playwright 15s) into 60s+. Soft bound:
    once blown, remaining fallback hops are skipped (not killed mid-flight), so
    worst case ≈ deadline + one in-flight hop.
  - Tests: `tests/test_deadline.py` (4) — deadline forces partial return within
    budget + per-URL cap skips the slow chain, each with a NEUTER-BITE sibling
    (knob=0 → the call provably exceeds the budget).

## 0.4.3

### Added
- **Entity-diversified rerank top-K for multi-entity comparison queries.**
  `search/rerank.py::_diversify_by_entity` (wired into `rerank_by_bm25`): when a
  query names ≥2 distinct entities that are actually present in the candidate
  hosts, the selected top-K is guaranteed to cover each named entity—its best
  BM25+authority candidate is picked first—before remaining slots are filled by
  global score. Fixes the "deep on 1 of N entities" weakness where the
  highest-scoring entity's pages could monopolise the whole top-K (e.g.
  comparing Cloudflare/Fastly/CloudFront). Within one entity the existing
  authority boost still decides the winner (official/primary over aggregator).
  Single-entity queries are unaffected (falls back to plain global top-K).
- New shared helper `search/authority.py::host_brand_labels(url)` — non-generic
  host brand labels, now the single source of truth for both OFFICIAL detection
  in `classify_authority` and entity attribution in rerank.

## 0.4.2

### Changed
- **Docs/packaging hygiene only — no behavioural change.** Documented in
  `pyproject.toml` that `lxml` is a *transitive* dependency of `trafilatura`,
  NOT imported by `tofu_search` directly: all BeautifulSoup parsing uses the
  `html.parser` backend (`search/_common.py:soup_of`) because lxml 6.x/libxml2
  can segfault under concurrent threads, and the sole lxml consumer
  (`trafilatura.extract`) is already serialized under `_TRAFILATURA_LOCK` in
  `fetch/html_extract.py`. The `lxml>=5.3` floor is retained to keep a
  known-good version resolved. This is a clarifying comment so future
  contributors do not switch any bs4 construction to the `lxml` parser.

## 0.4.1

### Added
- **Adaptive per-engine proxy strategy** (`search/proxy_mode.py`). The
  HTML-scraping engines share one `requests.Session` whose proxy behaviour was
  otherwise dictated entirely by ambient `HTTP(S)_PROXY` env vars, with a
  single attempt and no recovery — so "did search work?" depended purely on the
  installer's network topology (a container behind a proxy with no env var; a
  host with a stale/dead proxy env var; a datacenter/proxy egress IP soft-
  blocked by an engine). When a proxy IS available, each engine now tries BOTH
  network paths (proxied ↔ direct) and REMEMBERS which one worked (sticky,
  TTL'd), so steady state stays one request per engine. A fast connect/proxy
  failure, a blocking status (403/407/429/5xx), or a soft block (a substantial
  200 body that parses to 0 results) on the first path transparently retries
  the other; a read-timeout does NOT (switching paths won't make a slow
  endpoint fast, and it would blow the time budget). **With no proxy configured
  this is a no-op** — a single direct attempt, byte-identical to before.
- `SearchConfig.proxy_url` (host-injected proxy; falls back to
  `https_proxy`/`http_proxy`/`all_proxy` env vars) and
  `SearchConfig.proxy_dual_attempt` (default on). Env overrides
  `TOFU_SEARCH_PROXY_URL` / `TOFU_SEARCH_PROXY_DUAL_ATTEMPT`.

## 0.3.2

### Added
- **Pre-fetch relevance gate** (`search/prefetch_gate.py`). The pipeline used
  to submit *every* engine-returned URL to the fetch pool the instant an engine
  responded, and only judge relevance afterwards via the optional LLM content
  filter (which runs AFTER the expensive fetch). A junk SERP result (e.g. a
  consumer-health page returned for an academic query) was therefore fetched in
  full — wasting the fetch budget, flooding a host's browser/transport, and only
  then dropped. The new gate runs a cheap, pure-Python, no-LLM lexical check
  (`title + snippet` vs query terms, reusing the BM25 tokenizer) and declines to
  FETCH results with zero query-term overlap. It is deliberately **fail-open**:
  short queries (`< prefetch_gate_min_query_terms`, default 2) and the leading
  `prefetch_gate_min_fetch` (default 3) candidates always pass, and a skipped
  result is NOT dropped — it stays as a snippet-only candidate so rerank/format
  still see it. Only the page fetch is skipped.
- `SearchConfig.prefetch_gate_enabled` / `prefetch_gate_min_query_terms` /
  `prefetch_gate_min_fetch` knobs (default on; conservative). Set
  `prefetch_gate_enabled=False` to restore the old fetch-everything behaviour.

## 0.3.1

### Changed
- **Logging now defers to the embedding application.** `tofu_search.log` only
  attaches its own stderr handler when the ROOT logger has no handlers (true
  standalone use). When embedded in a host that already configured logging
  (handlers on the root logger), records propagate to the host's handlers
  instead of being double-printed to stderr — so the host controls routing and
  the pipeline diagnostics land in the host's log files.
- Routine Playwright worker timeouts (`queue.Empty`) no longer log a full
  traceback (`exc_info=True`). A network render that exceeds its budget is
  expected, not a crash; the warning now states the timeout value instead.

### Added
- `BrowserProvider.fetch_html(url, *, timeout=20)` seam — a host browser can
  return the RAW HTML of a page so the library parses it. `search_via_browser`
  now prefers this (fetching the DuckDuckGo SERP via the host browser and
  parsing it with the engine-grade bs4 parser) and only falls back to the
  host's `search()` when `fetch_html` is unavailable. This keeps SERP parsing
  inside the library instead of duplicated in every host.
- `tofu_search.search.engines.ddg.parse_ddg_html_text(html, ...)` — parse a raw
  DDG lite HTML string into result dicts, reusing the in-engine selectors.

## 0.3.0

### Added
- `fetch_url_bytes(url, timeout=None, max_bytes=None)` — download the raw bytes
  of a (binary) file asset, returning `(bytes, content_type)` or `None`.
  Enforces the same scheme / SSRF / size-cap policy as the text pipeline.
- `looks_like_text_asset(url)` — classify a URL by extension as a source/markup
  file (`.svg`, `.json`, `.py`, `.css`, …) vs. a prose web page.

### Changed
- **`fetch_page_content` now returns text-based file assets as their raw
  source** instead of `None`. SVG, JSON, XML, YAML, CSS, JS and source-code
  URLs (matched by Content-Type) are returned verbatim, bypassing the
  HTML/article extraction and the article-oriented bot/SPA/min-length gates so
  small-but-complete files (e.g. a 40-char JSON) are not dropped.
- `_should_fetch` no longer rejects `.svg` URLs (SVG is text). Binary media
  (`.jpg/.jpeg/.png/.gif/.mp4/.mp3/.zip/.tar/.gz/.exe`) is still skipped.

### Migration note
This changes public fetch behavior: SVG/JSON/source URLs that previously
returned `None` now return their source text. Callers that relied on `None`
for those content types should branch on `looks_like_text_asset()` instead.

## 0.2.0

- Initial standalone release: multi-engine search, vertical/structured search,
  concurrent fetching, optional LLM content filter, BM25 rerank, provider seams.
