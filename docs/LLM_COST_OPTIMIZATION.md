# LLM request cost and context policy

Latency takes precedence over speculative cache savings: `TOFU_CACHE_SETTLE` and `TOFU_CONV_STICKY_HOLD` default off. Setting either to `1` restores its bounded cost-saving wait. Stable cache prefixes, cache accounting and immediate reuse of an eligible warm key remain active. Provider 429/backpressure still uses the mandatory retry/admission policy. The policy owner and offline paired measurement are in [LLM I/O](modules/llm_io.md). This note records the request-path cost audit and the defaults introduced on 2026-08-08. It is about repeated agent/tool rounds, not ordinary one-shot chat.

The 2026-08-14 GPT-5.6 P0/P1 rollout follows OpenAI's current builder guide: official providers use Responses, the Sol/Terra/Luna family and exact current limits/prices; Pro is a reasoning mode rather than a model slug; lean prompts, PTC and Multi-agent are task-gated and represented as independent benchmark arms rather than assumed wins.

## Measured baseline

A representative six-round Kimi task submitted 1,160,102 prompt tokens and 4,839 output tokens. Its per-round prompts grew from 185K to 194K tokens. Cache reuse was generally high, but one byte-stable round still returned zero cached tokens, so automatic provider caching cannot be treated as a cost bound.

The first-round snapshot contained 279 messages and 240 MCP schemas; schema JSON alone was 223,534 bytes (about 55K tokens), roughly one third of the request. Longer production tasks accumulated 5–25 million prompt tokens in tool loops.

A 2026-09-01 trace demonstrated why total input/output alone cannot explain price: adjacent rounds processed 56,412/158 and 57,143/163 tokens, but cache reads were 10,752 (19%) versus 55,680 (97%), so displayed cost differed by 5.95× (¥1.7261 versus ¥0.2899) under the same unit prices. In that trace, 25 of 34 comparable working-set rewrites were followed by another rewrite before their projected task-age payback. Cost policy and UI must therefore retain the uncached/cache-read split and reason about prefix lifetime, not total tokens or total task age.

## Policy

### 1. MCP schema exposure

Default `mcpToolExposure=auto` preselects at most `mcpActiveToolLimit` (default 8) native MCP schemas instead of shipping the full catalog. Auto state is scoped by owner plus conversation; low-signal turns reuse the prior surface and previous MCP calls before deterministic fallback.
Search cards are ranked/paginated views, not proof of wire absence. The full catalog remains Tool Search/dispatch authority; `inline` exposes it all and `progressive` retains the legacy read/write wrappers (~1.7 KB).

Controls (request-config keys; no `TOFU_*` environment variable):

```text
request config: mcpToolExposure=auto|inline|progressive
request config: mcpActiveToolLimit=8
```

### 2. Compaction timing

Automatic semantic L2 compaction uses the existing route-window safety trigger,
including output and recovery reserves (roughly 778K on a 1M route with the
standard reserves). Fixed economic working-set settings have been removed;
there is one production timing policy and no opt-in threshold. The shared local
compactor owns rewrites across routes.

“Pays back eventually” is not “now is the best time.” Waiting can reuse a cheap
warm prefix and fold more material in a single later summary. Compare both
policies over equal work, including cached reads, summary calls, cache rebuilding
and post-summary re-reads; the larger absolute reduction at 300K does not by
itself prove that it beats 128K. The current cost equation cannot predict that
whole trajectory, so positive ROI alone does not trigger automatic L2. Decision,
evidence and migration: [context budgets](CONTEXT_BUDGET_POLICY.md#automatic-compaction-preserves-execution-continuity).

A final fail-closed admission boundary remeasures the rendered request after inbox/tool injection. First and later dispatches use the route-derived physical safety ceiling and requested output reserve, not a fixed 256K first-dispatch cap or the economic target. A bounded L1 candidate is considered before one semantic compaction + recompose. Failed live summaries preserve source and refuse irreducible input; explicit utility recovery remains separate. Complete generated text survives local receipt-shaping faults, and local compaction defects surface as internal errors rather than provider prompt overflow. Each API-round persists a content-free message/schema/total/target/method/action snapshot, separating admitted prompt size, cache reuse and billed internal compaction calls in the cost panel.
The accepted complete-input count is forwarded only through that round's call stack to the completion-window clamp; absent or invalid evidence keeps the standalone body builder's local estimate instead of reusing stale task state. Compression-first model selection defaults to `aa-cheapest`: token-weighted cost first, highest exact fresh AA within minimum-cost ties, no hidden band, strict same-scope savings; explicit `main`/`disableModelFallback` and missing evidence preserve main. AA is a proxy, not measured summary fidelity or speed. Ownership: [AA-cheapest routing](CONTEXT_BUDGET_POLICY.md#aa-cheapest-compression-first-routing).

Optional candidate economics still resolve current/candidate prices separately,
count the observed warm prefix and preserve summary-cost provenance. Those checks
can reject an optional rewrite, but are not an automatic L2 scheduling rule.
Mandatory window recovery and explicit manual compaction keep their own authority.

Default L1 request projection is bounded by both 40 results and 48K estimated tokens. The newest complete tool-call batch always survives even if it alone exceeds the token budget; older results become placeholders only in the current model request, either outside the previously sent request or in its last tool step when the next-request price admits the edit ([section 8](#8-l1-edits-of-the-previously-sent-request)). Settled Turn projections remain byte-identical: L1 performs no transcript-authority read or write. This slows working-set growth before an expensive L2 prefix rewrite without deleting durable user history.

Each `[CacheRoundRecord]` log line carries `lane` (`conversation` for the root agent, `isolated` for a child or transient invocation, or `unscoped`), `tools` (the task's logical tool count), `wire_tools` and `wire_tool_name_bytes` (the provider-visible surface actually sent, `sum(len(name) + 1)`), and `cold_group` while the round belongs to the cold side of a live split. `lib/tasks_pkg/cache_tracking/_health.py` owns the split: per model it compares read share across lane, tool-surface size (`<=16`, `17-32`, `33-64`, `>64`) and sent name bytes (`<=128` … `>1024`), and flags a model whose worst group (at least 20 rounds) reads back less than half of its best group's share. `scripts/cache_waste_report.py`, the in-process monitor (one `[CacheHealth]` WARNING per model per hour over a rolling 60-minute window, an INFO line when it clears) and the nightly optimizer evidence (see section 6) all use that one rule. A gateway can serve requests with the same key, URL and beta header from caches that behave differently by request shape, even for byte-identical prefixes: when root agents and sub-agents diverge, check whether the split follows the tool surface rather than the lane before attributing a miss to request bytes or to orchestration. `tools` counts definitions only; for Claude on the Sankuai Anthropic face the measured discriminator is the joined byte length of the tool names ([section 6](#6-gateway-tool-name-budget)).

The collapsed finish footer shows total input, uncached input, cache reads and output together. Top-level turn cost is the sum of per-API-round prices under each round's own model/provider/tier; mixed main, fallback and compaction calls are never flattened and repriced under the final model.

Summary retry ordering preserves cancellation, local request errors and
incomplete-output handling before the generic transport fallback. An unqualified
cheap request may retry once on the text tier; selected routes and bounded tasks
retain their existing restrictions. Once transport returns, local accounting or
validation failure cannot authorize another paid call. A blanket exception
handler both hides the intended fallback and conflates local failures with
provider outages; regressions exercise both chat and streaming dispatch.

### 3. GPT-5.6 Responses caching and compaction

Only GPT-5.6-family Responses requests receive these OpenAI-specific fields; generic Responses-compatible providers are unchanged. The ChatGPT Codex subscription profile keeps the fields its internal backend supports and omits the public-only explicit breakpoint and `context_management` request fields:

- `prompt_cache_key` is a stable SHA-256-derived conversation namespace; raw conversation/task identifiers never leave the process in this field.
- On the public Responses profile, the last text block in the stable developer prefix receives an explicit cache breakpoint. The Codex subscription profile uses its implicit cache plus the stable `prompt_cache_key` because its Luna backend rejects `prompt_cache_breakpoint`.
- Encrypted reasoning output items are saved and replayed with `store=false`, with `reasoning.context=all_turns`. The local counter sees the returned reasoning summary, not the encrypted payload. To close the latest-round lag, the usage cache carries provider-reported `reasoning_tokens` as a one-round replay reserve only when an encrypted reasoning item is actually appended.
- On the public Responses profile, `context_management` uses the same working-set threshold and is the primary L2 path. The Codex subscription profile keeps Tofu's local compaction path because its product backend does not accept the public request field. Returned opaque compaction items are persisted; input before the latest item is pruned while current system instructions are retained.
- Usage conversion preserves both `cached_tokens` and `cache_write_tokens`, and cache fingerprints inspect Responses `input` rather than an empty Chat-Completions `messages` field.
- When explicitly enabled, the Codex subscription settle gate protects cold cacheable requests and keeps the 4.2-second per-key send interval, but a warm round arms the additional five-second visibility window only after `input_tokens - cached_tokens` reaches 8,192. A 95,723-line production-log replay on 2026-08-27 matched 1,217 holds to the immediately preceding usage: this policy would remove 872 holds / 2,493.07 seconds (70.4% of matched wait) while retaining 345 holds / 1,046.18 seconds for cold or material tails. This is a counterfactual latency measurement, not a claim of deployed upstream cache-hit improvement. Local L2 summaries now use a content-free digest of validated owner + conversation + L2 stage in both dispatch affinity and the stream body, stabilizing their Codex session without overwriting the ordinary conversation's settle clock. A frozen six-hour window found 129 summaries, 43 summary-side holds/84.61 seconds, and 100 ordinary-round holds/218.61 seconds within 30 seconds after a summary; the 303.22-second sum is the matched interference opportunity removed by the namespace split, not a deployed end-to-end or unrelated-wait claim. Separately, reconstructible project summaries and non-terminal translation previews now use atomic immediate-only shared-contention admission: blocked work returns before transport without advancing the family probe clock, while every durable/default caller keeps the wait-and-probe contract. In a frozen six-hour response-log replay, 11/30 project-summary 429s and at least one first 429 across 27 incremental translation threads arrived within 30 seconds of an earlier same-family rejection; the 12 records bound an opportunity window rather than proving their unlogged selection instants or deployed savings. A later full-log replay found 117 shared-limit responses 30–120 seconds after the preceding family rejection and 157 further same-family responses within 15 seconds of those resets. The gate now decays at 30 seconds to one immediate-plus-serialized recovery seed and retires at 120 seconds (or two successes), retaining a bounded 256-family map without claiming the opportunity count as avoided traffic. Tool-result reuse remains latency-positive—2,084 of 2,093 speculative injections matched a same-task receipt within 60 seconds—but its old plain dict had no active-task ceiling and survived terminal work for the one-hour TaskRuntime TTL. In the same frozen window, 35 terminal snapshots retained 1,330 entries (median 4, p95 134, maximum 466); projecting the 8 GiB default cap of 128 onto those snapshots retains 745 instead, a 585-entry/44.0% terminal-residency opportunity, not a lifetime-peak or measured-RSS claim. The shipped FIFO keeps recent prefetch/dedup wins, safely re-executes an evicted receipt, and releases all survivors immediately after terminal settlement.

Proactive LLM/hybrid polls now build one status snapshot for both dispatch and audit, and read only the target conversation's final two messages. On the largest current frozen archive (1,163 messages), the per-poll conversation projection changed from two 13.04 MiB / 120.8 ms full reads to one 11.3 KiB / 12.3 ms tail read (-99.96% wire, -94.90% read time, -93.43% traced peak). Metadata-only and bounded-window Sidecar reads no longer select `search_text`; legacy JSON tails parse only their selected suffix with full-decode fallback. Explicit offline maintenance can now compress individually large frozen messages without hiding those JSON boundaries: the same 1,163-message sample stores 12,122,003 as 10,079,123 bytes (-16.85%), and its exact two-message tail remains on the fast path (1.042 versus 1.008 ms). Across all 4,544 current frozen rows, a read-only production encoding sweep validates zero invalid/oversize documents and projects 5,697,009,096 bytes to at most 2,526,642,300 (-55.65%, at least 3,170,366,796 bytes); the largest two-message row saves 70.79% but full decode costs 0.353 versus 0.154 seconds, an explicit CPU-for-storage tradeoff. Eleven additional project/scheduler/autopilot/turn-lifecycle settings, title, revision, and existence probes now declare `derive_messages=false`; on that archive each changes from 13.04 MiB / 120.8 ms / 178.35 MiB traced peak to 1.1 KiB / 0.18 ms / 0.017 MiB, without adding a cache. Legacy-autopilot close-out now resolves a live settings pin metadata-only, then uses a 128-message tail plus an exact full fallback for disarmed runs/anchors. The largest current pinned sample changes from 37.94 MiB / 258.6 ms / 560.8 MiB traced peak to 14.7 KiB / 0.42 ms / 0.21 MiB. A 128 KiB legacy JSON scan budget prevents large tails from trading allocation savings for seconds of Python work; on the largest disarmed-summary sample it restored 42.1 ms latency while still reducing response bytes from 8.15 to 4.44 MiB. Project-summary generation now reads exact first-eight/last-eight pages after its metadata freshness gate and takes a coherent full fallback on insufficient visible edges or an epoch mismatch. On the largest current 1,163-message project conversation, the selected prompt source remains byte-identical while projection bytes fall from 13.04 MiB to 181 KiB and traced peak from 178.35 to 13.51 MiB; uninstrumented direct-read median remains 46.08 versus 45.56 ms. Readable past-conversation references now request exact first-three/recent pages with a coherent fallback instead of hydrating discarded middles; the same largest sample keeps its selected messages while wire falls from 13.04 MiB to 400 KiB and direct-read median from 46.12 to 40.56 ms. Raw reference fitting now starts from 64 recent messages and falls back only when all candidates fit; the sample's exact 70,324-character result changes from 13.04 MiB to 428 KiB of read wire and 47.59 to 44.06 ms direct read-plus-fit median. Their structured human digest uses a revision-matched suffix probe plus exact anchored tail/head pages; its default selected rows stay identical while wire falls from 13.04 MiB to 642 KiB and direct median remains 41.13 versus 40.46 ms. Unsearched conversation listing now pushes the requested result bound into storage: on the current 4,786-row catalog, a 20-result read changes from 57.9 ms / 21.41 MiB traced peak / 1.66 MiB wire to 0.22 ms / 0.07 MiB / 7.9 KiB. Keyword listing now merges at most 200 body IDs with an authority-side exact-Unicode title scan that keeps only the deliverable rows. A common 20-title hit changes the old 10,000-row candidate read from 59.31 ms / 21.41 MiB / 1,698 KiB to 1.22 ms / 0.26 MiB / 7.05 KiB; a complete miss scans all lightweight title pages but still costs only 28.19 ms / 0.49 MiB and returns two bytes. Project Attention now reuses one Board snapshot and one deduplicated title-only owner query across Board/Charter items; on the largest current conversation a single provenance lookup falls from 59.97 MiB / 364.25 ms / 1,126.89 MiB traced peak to 0.36 KiB / 0.324 ms / 0.004 MiB, and an empty settings whitelist is constant-projected before JSON decoding. The collaboration summary passes its same-request Board and pending-proposal snapshots into Attention, eliminating one duplicate of each: on the current largest samples, 200 Feed events plus 34 Board tasks remove 155.52 KiB response material, 0.97 ms direct median, and 0.62 MiB traced temporary allocation per summary without retaining state. Influence now renders its prompt mirror from the same Board snapshot it classifies, and Status derives pending proposals plus the first 80 recent-block candidates from one 200-event Feed; the current largest samples remove another 78.15 KiB, 0.57 ms direct median, and 0.32 MiB traced temporary allocation across those reads. Team/Feed title backfill is now metadata-first for the 4,747/4,786 (99.19%) current conversations with usable titles, reducing the largest row from 59.97 MiB / 358.66 ms / 1,126.89 MiB traced peak to 0.36 KiB / 0.324 ms / 0.004 MiB. Untitled rows retain exact first-user fallback through a revision-matched eight-message head probe and full fallback on an unloaded first user or epoch change.
The Daily Optimizer's recent tool-distribution signal now selects metadata first, then lazily hydrates shared four-ID batches and recursively splits an oversized response; settings never cross either phase and any lazy-frame error discards the whole best-effort signal. The current latest-200 transcript/settings material is a modest 0.508 MiB, while its largest four-row transcript group is 0.219 MiB (-56.9% body-frame material); this is a frame-peak measurement and the extra bounded local round trips are intentional, not an end-to-end latency claim. Daily-report day extraction now adds a candidate `created_at < day_end` bound before exact message filtering and treats lazy hydration as all-or-nothing. On the frozen 2026-07-01 sample, candidates fall from 1,749 to 203 and hydrated message material from 3,697.59 MiB to 53.86 MiB (-98.54%); cloned conversations use their new creation epoch, so copied old turns do not manufacture historical work. Calendar and exact-day counts now send explicit local-time interval boundaries to `conversation.activity_dates`: Turn-native SQL returns only typed timestamp scalars, frozen archives decode in bounded four-row batches, and only distinct counts cross the RPC. The frozen July owner month changes at least 2,912.35 MiB of application-side message material across 1,336 candidates into a 127-byte result; its legacy-heavy Sidecar calculation remains 9.10 s / 255.22 MiB peak, so this is not a claim that archive parsing vanished. Daily-report digest extraction now combines statistics and transcript construction in one pass and retains at most 128 visible turns before its existing 800-character render. On the decoded 1,163-message sample, the exact 846-character output and 261 rounds stay fixed while pure processing changes from 1.371 to 1.250 ms median and traced temporary peak from 0.215 to 0.061 MiB; JSON decoding is outside this microbenchmark. Optimizer evidence gathering now reads each eligible bounded log tail once per run, reuses immutable request-local lines, and releases one log family before loading the next; distributed mode still reads only owner-filterable audit evidence. Post-apply metrics compute every tracked block domain plus total tool failures in one candidate-prefiltered application-log pass instead of two full tail scans per action. On the current 4 MiB application, 2.997 MiB audit, and 2 MiB error tails with ten tracked domains, equivalent pure application-log processing changes from 1,718.71 to 370.17 ms median (4.64x), while total logical tail material changes from 97.01 to 9.00 MiB (-90.72%) and traced peak remains 28.50 MiB; this excludes database collectors and is not an end-to-end optimizer-run claim. The audit and error snapshots now stream once into all event, switch, excerpt, and issue projections rather than decoding JSON three times and timestamps twice. On the current preloaded 9,803-line audit and 1,705-line error tails, byte-identical pure projection changes from 232.90 to 154.60 ms median (-33.62%, 1.51x) while additional traced peak remains about 0.03 MiB; snapshot loading and database collectors are outside this comparison. The main application-log projection now rejects signal-free lines before timestamp parsing and runs only marker-relevant regex groups. Lowercasing is capped at 8,192 characters; longer lines use allocation-free case-insensitive markers. On one exact 11,943-line snapshot, the projection remains byte-identical while changing from 348.76 to 54.18 ms median (-84.47%, 6.44x) with 0.106 MiB additional traced peak. The independent post-apply pass measured only 19.21 ms / a 5.33% maximum fusion opportunity, so it remains decoupled from action storage/writeback. The provider-usage token tier is now a launch-probed, content-bounded LRU with atomic expiration/replacement and pressure clearing; capacity or TTL misses safely continue to the existing local counter chain. In a 10,000 one-shot-conversation fixture, the old unbounded mapping retained 3.900 MiB while the 128-entry fallback retains 0.060 MiB (-98.47%, 0.066 MiB peak); full-capacity recording averaged 57.15 microseconds per provider completion. Login-wall admission now begins before the synchronous browser probe and persists through background polling, collapsing concurrent same-route probes and bounding ten-minute daemon work by owner/process. Its cooldown and live-session maps are now LRU/TTL bounded and pressure-clearable where reconstructible. In a 10,000-route fixture, combined retained heap changes from 6.005 to 0.189 MiB (-96.85%; 0.207 MiB peak); bounded construction costs 17.61 versus 12.24 ms total, about 0.54 microseconds extra per route. Working-tab affinity is now a launch-budgeted, 30-minute action-sliding LRU that is pressure-clearable and immediately forgets tabs proven closed. In a 10,000-route fixture, the old lifetime map retains 2.017 MiB while the 64-route lean bound retains 0.024 MiB (-98.81%; 0.029 MiB peak); bounded remember costs 17.35 versus 1.94 ms total, about 1.54 microseconds extra per route. MCP pre-request Tool Search now uses deterministic inverted postings plus launch-derived LRU indexes/sticky state; state retains query digests instead of longform text and every record path enforces capacity. Isolated old/new replay returned identical tool arrays while a 256-tool, 8,000-character query changed from 48.11 to 13.13 ms median (-72.71%, 3.66x) and a 20,000-word stress query from 971.40 to 40.74 ms (-95.81%, 23.84x). Thirty-two catalog revisions changed retained heap from 22.129 to 3.079 MiB (-86.09%, four-index lean bound); 1,024 maximum-size sticky queries changed from 8.299 to 0.596 MiB (-92.82%). These are pure local-search/residency measurements, not end-to-end LLM latency claims.
Stable bridge generations now reuse one ordered authoritative-reference tuple and one catalog fingerprint instead of sorting, copying, and hashing the same 256-tool projection every round. An output-identical 8,000-character synthetic replay changed the combined projection-plus-selection median from 5.712 to 4.464 ms (-21.86%, 1.28x), p95 from 6.563 to 5.160 ms, and 100-round traced peak from 0.972 to 0.618 MiB (-36.45%). Registry authority assembly now maintains one request-local name index rather than rebuilding it for every appended dynamic tool: with contract compilation held constant, 256 tools changed from 4.826 to 0.649 ms (-86.55%, 7.44x), and 1,024 tools from 67.026 to 2.654 ms (-96.04%, 25.25x). Request-owned ToolContract cloning now uses a memoized JSON fast path with `deepcopy` fallback for extension values: on the same 64-tool surface, registry assembly changed from 1.906 to 1.648 ms (-13.54%, 1.16x), and subsequent execution-document compilation from 1.306 to 0.944 ms (-27.71%, 1.38x). Resource-budget resolution now consumes a valid launch/operator value before computing its fallback, and Tool Search retains its one process-level term capacity; the same output-identical 256-tool pre-request build changed from 9.971 to 0.842 ms median (-91.56%, 11.84x) and p95 from 10.850 to 0.859 ms. Caching the private search-text mapping in that catalog generation then changed a same-scope `_build_mcp` from 0.591 to 0.049 ms median (-91.74%, 12.11x), p95 from 0.613 to 0.055 ms, and 100-round traced peak from 0.108 to 0.066 MiB (-38.85%) with identical sidecar text. Ordered-prefix sticky-state expiration under a process-monotonic clock changed the 4,096-live-state cleanup median from 0.3740 to 0.000481 ms (-99.87%, about 778x), with p95 changing from 0.4009 to 0.000524 ms. A 64-byte raw-plus-normalized digest pair then lets an unchanged 8,000-character sticky query bypass repeated term iteration: with 256 tools, selection changed from 1.6129 to 0.0139 ms median (-99.14%, about 116x), and p95 from 1.6525 to 0.0148 ms; its measured object is 8 bytes smaller than the former hexadecimal digest. Precomputing the shared-name fallback tuple changed empty-query selection from 0.0855 to 0.0119 ms median (-86.08%, 7.18x) and an unmatched short query from 0.2319 to 0.1686 ms (-27.30%, 1.38x); the tuple is 2,088 bytes for 256 tools, or about 8.16/65.25 KiB across the four/32 retained-index bounds. Skipping impossible phrase boosts changed an output-identical fresh 8,000-character query from 1.9073 to 1.7500 ms (-8.25%, 1.09x). Reusing sparse posting candidates for phrase boosts, with a dense tuple-scan crossover and punctuation-only fallback, then changed an unmatched short query from 0.1686 to 0.0176 ms (-89.91%, 9.58x) without adding retained index state; exact and broad-query measurements stayed within 0.2%. These are pure local projection/search/assembly measurements, not end-to-end LLM latency.
Swarm output directories are now lazy until a real stream chunk, while one launch-profiled startup worker scans at most `clamp(sessionCapacity*1024, 512, 16384)` entries and removes only atomically verified empty directories. The current root contains 5,811 empty directories among 7,542 (77.05%); every existing transcript file remains durable. On a temporary 5,811-empty/200-valuable-directory fixture, the bounded sweep took 0.0568 seconds, preserved all 200 valuable directories, and changed a missing cross-turn agent-log lookup from 18.218 to 0.592 ms median (-96.75%). This is a pure local filesystem fixture, not startup or end-to-end latency.
Mutable observers now execute on every call instead of inheriting idempotent-implies-cacheable staleness: conversation references, Project Brain, memory search, schedule listing, swarm artifact listing, and motion-video checks are fresh, while a separate idempotency resolver preserves loop/progress semantics. Selected observations and `get_agent_result` may return a fresh-unchanged receipt only when the new bytes match, the exact prior paid projection remains in active context, and the receipt is no more than half its characters and strictly fewer model tokens; otherwise the full result returns. The independent launch-bounded digest map stores no result body, cannot evict expensive dedup entries, and is dropped at task settlement. A conservative offline replay of failed conversation `mtdx825fjmhmx5` (99 selected calls among 743 task rows) converted 47 repetitions and changed their model-visible projection from 49,486 to 24,797 characters (-49.89%) and 13,553 to 6,619 counted GPT-5.6 tokens (-51.16%); this is a historical counterfactual, not observed billed savings or an end-to-end latency claim. Readable 2026-08-25..30 application logs contained no dedup hit for the newly added non-Project families, so their change is preventive correctness with no production saving claimed. The same failed trace had 955 unique tool-call IDs and 1,048,944 model-visible tool-result characters kept alive by task-lifetime settlement/correlation maps, including at least 310,633 characters already removed from hot context by L1; under the personal profile those references could remain for the 600-second terminal TTL. Settlement bodies are now invocation-local and call-ID receipts contain only three bounded metadata fields. Collision correctness comes from one per-pipeline history index instead of the evictable receipt map: on a 955-message plus 955-round synthetic history with 100 conflicts, median remint time changed from 17.831 to 0.459 ms (-97.42%, 38.83x). The largest assistant Turn in that trace also carried 4,785,739 serialized bytes of `toolRounds` plus 15,284,001 bytes of derived `segments` (20,069,740 combined) in its terminal carrier; turn-native settlement now releases both graphs, any checkpoint rounds, and persisted `programRuns` immediately after their durable authorities commit instead of waiting through the 600-second personal hot-task TTL. Inline/headless response carriers are excluded. The commit observer captures one boolean before release and preference consolidation patches provenance without a full structural fold. On that largest Turn, 592 explicit L1 rounds still had their original full result in the sibling segment; the stable projection now synchronizes only unique identity-compatible mirrors, reducing segments from 15,284,001 to 1,981,839 serialized bytes (-87.03%) and the whole normalized projection from 20,145,745 to 6,843,583 (-66.03%) without mutating the source. These are local serialized-retention/storage-frame/CPU facts from a read-only projection, not observed RSS, disk backfill, API savings, or end-to-end latency.
Structural Turn events now send one revision patch without a duplicated event-envelope projection, retain one live baseline until settlement, and rebase a stale CAS once. On the read-only 955-tool Turn from `mtdx825fjmhmx5`, a `tool_start` command changed from 13,687,938 to 695 serialized bytes (-99.99492%); its 301-byte patch built in 0.338 ms median. The Sidecar now retains a separately bounded, revision-exact baseline too: with the still-required full encode held constant, a cache hit changed local median processing from 51.985 to 26.407 ms (-49.20%, 1.97x), incremental traced peak from 70.269 to 8.635 MiB (-87.71%), and avoided one 5,235,567-byte BLOB read. Its 10,583,620-byte measured retained heap is charged as 15,706,701 bytes under the 32 MiB reference budget. These are local serialization/CPU/allocation proxies; changed Sidecar rows still receive a full encode/write.

### 4. Claude public API versus Claude Code subscription

The direct public Anthropic Messages route enables the `compact-2026-01-12` beta only for Anthropic's documented model families. It sends `context_management.edits[type=compact_20260112]` at the same working-set threshold, retains a valid returned `compaction` block verbatim, and uses its effective post-compaction prompt for the context gauge. Billed usage still sums all `usage.iterations`, including the internal summary pass. A documented `content: null` compaction failure is not replayed as an authoritative summary; the intact history is retried and the local hard-window fallback remains.

Claude Code Pro/Max OAuth is a separate subscription product route. Its captured beta set includes context editing but not the public `compact-2026-01-12` contract, so Tofu does not inject that beta or strategy there. Local L2 remains primary. Visible signed thinking is counted from `reasoning_content`; opaque `redacted_thinking` is retained verbatim and, when the provider reports `thinking_tokens`, receives the same one-round replay reserve as encrypted Responses reasoning.
### 5. Serial single-tool round trips

A 2026-08-28 audit of 12 highest-round production conversations projected 2,597 tool calls into 1,890 tool-bearing model rounds. It found 61 chains of at least six consecutive rounds with exactly one inspection/command-family call: 615 rounds total, with 249 beyond the sixth-round threshold across 29 assistant turns.
Those 249 rounds are an ideal counterfactual opportunity ceiling, not saved calls. In one active Kimi attempt, 81 logged model calls consumed 1,724.2 seconds while 59 timestamp-complete tool receipts consumed 66.795 seconds (0.609-second median, 4.284-second P95), so model round trips dominated the sample.
Separately, none of 2,437 cache records combined a write above 20K tokens with zero cache read; provider-cache repair was not the next bottleneck.
A 2026-08-30 delivery-aware replay of one 739-tool-row Kimi trace found 127 `await_agents` calls. Of 92 no-ID waits, 91 returned only completion payloads already delivered through a prior await/get-result or automatic inbox injection, repeating 221,140 characters / 70,652 raw tool tokens; 90 occurred in await-only model rounds, with a longest seven-round chain.
No-ID waits now consume only unseen completion deltas across synchronous returns, automatic inbox delivery, and restart rehydration; explicit-ID waits and `get_agent_result` still permit deliberate rereads. The historical calls immediately following those 90 await-only returns cost ¥16.4289 and read 6,560,000 cached tokens, 10.72% of the trace's ¥153.1896 regular main-loop API cost.
That ¥16.4289 is an opportunity ceiling, not an observed saving: under the corrected protocol a wait may still incur a later model call after a new completion or timeout. The directly removed behavior is old completed payloads immediately satisfying another no-ID `mode=any` call.

Serial-call observations remain diagnostic, not grounds for model intervention. The six-round efficiency reminder and three-round local-PTC adoption prompt are removed: successful serial calls do not establish independence, and a batched read is still one tool call. Existing tool instructions describe batching; actual tool results and measured quality/cost remain the evidence for optimization.

### 6. Gateway tool-name budget

**Problem (2026-10-27/28):** root-lane Claude rounds through `your-llm-gateway.example.com/v1/anthropic` (`kunlunbedrock.claude-opus-5.5-m17`) read back 3.4% of their prompt (779 rounds: 6.7M read versus 192M written, 679 zero-read rounds), while sub-agents on the same key, URL, beta header and model read 96.5%. General-role sub-agents with the root-sized surface read only 3.6%. Raw archives showed byte-identical tools, system and message prefixes round over round, including the 1h tools/system breakpoint, so the `upstream_identical`/`cache_write_unsettled` verdicts were correct but not actionable. Each root round re-prefilled 100K–280K uncached tokens.

**Evidence:** standalone probes built by the repository's `prepare_request` and posted through the shared transport, each sent identically two or three times 10–15 s apart with a unique ~6K-token tail, varying one input at a time (about 45 variants). Reuse followed the tool *names* alone. With `sum(len(name) + 1)` at or below 492 bytes a probe read its own write back; at 496 bytes or more it never did, at 8, 10, 20 and 35 tools alike. Thirty-five synthetic tools flipped from hit to miss when only their names became the root's real names; the root's real schemas and descriptions under synthetic names hit. Tool count, schema size (35 small tools; 11 tools padded past the root's schema tokens), description text, tool order, the system prompt, salted leading bytes and an `Mt-Session-Id` header changed nothing on this edge (the header pins the upstream account and cannot repair a miss keyed on names; account hopping is [section 7](#7-cache-affinity-carriers)). The root surface carried 606 bytes (35 tools, including eight `mcp__server__tool` names worth 233 bytes); the 11-tool coder surface carried 150. Replaying the real root round R61 (124 messages, 117K tokens): as-is it wrote 117,399 tokens on three sends and read nothing; with the projected 30-tool surface (448 bytes) it wrote 109,677 once and then read 109,677 on both repeats. The only healthy historical root window (2026-10-28 01:43–01:49, 585 bytes, 81–96% read) streamed a different SSE flavor (`estimated_tokens`, `context_management`), so it was served by a different upstream path. The edge is Claude-specific: `kimi-k3.1-pro` on the same face read 98% across 70 root rounds with the 53-tool root surface.

**Decision:** `lib/llm/tool_wire.py` enforces a route-scoped provider constraint at the final tool-visibility boundary. For Claude models (`is_claude`) on the Anthropic protocol at host `your-llm-gateway.example.com`, provider-visible names must fit `SANKUAI_ANTHROPIC_TOOL_NAME_BUDGET_BYTES` (448, about 44 bytes below the measured edge to absorb gateway key material Tofu cannot observe). Only `kunlunbedrock.claude-opus-5.5-m17` was measured; the probe key had no quota for the face's other Claude deployments, which share the gateway's Claude routing and the low cost of a false positive. Over budget, the longest optional names are hidden first, which keeps the most tools directly callable; equal lengths hide the later wire position. Hiding happens only while `search_tools` and `execute_tools` are on the wire, and never touches the floor: discovery/execution gateways, `poll`/`kill`, required direct names (code floor, `tool_choice`, declared direct tools) and the local swarm launch/result pair. Hidden tools stay in `_executable_tool_catalog`, remain discoverable through `search_tools` and callable through `execute_tools`, and are reported in `tool_wire_projection.budgetDroppedNames` plus one `[ToolWire] tool-name budget` INFO line. Every budgeted request also records `toolNameBytes` and `toolNameBudgetBytes` on `tool_wire_projection`; a request still over budget after projection logs `[ToolWire] tool-name budget … NOT met` at WARNING with the reason. Survivors keep their order and bytes, so the cached prefix stays stable; the surface changes only when its inputs change. Other routes are unchanged. Specifications: `tests/llm/test_tool_wire_name_budget.py`, `tests/cache/test_cache_tool_name_budget_wire.py`, `tests/tasks_pkg/test_root_lane_tool_name_budget_wire.py`.

**Alternatives rejected:** client prefix repairs (the prefix was already byte-stable); a session-affinity header, tool reordering or salted leading bytes (measured no effect on this edge); ranking pins above built-ins as `tools.schemaBudgetTokens` does (R61 would hide ten built-ins to keep eight auto-preselected MCP schemas instead of hiding five long names); a budget on every route or every model on the face (Kimi on the same face reads 98% with the same surface, and other providers showed no such edge); aliasing long names (model-visible names are shared by guidance, search results and execution authority); waiting for a gateway fix (every root round pays a full rewrite meanwhile). The mechanism is inside the gateway, which looks consistent with a ~512-byte key built from the joined names; report it to the gateway owner and re-measure the edge before raising the budget.

**Explicit Tool Search `off` (2026-10-28):** `tools.toolSearch=off` resolves to the `full` backend, which keeps every tool direct with no `search_tools`/`execute_tools`, so hiding has no discovery path. **Decision:** the setting is honored; such a request goes out over budget and logs the WARNING above (`no search_tools/execute_tools discovery path, Tool Search backend=full`) on every request, so the cost is visible per round and in the read-share split. Under `local`, a small or all-eager catalog keeps its gateway whenever the budget would hide a name (Coordinator-role surfaces, below). No deployment default sets `off` (`tools.toolSearch` defaults to `local`). **Alternatives rejected:** forcing the discovery pair onto this route when over budget (it overrides an explicit setting for cost, not correctness: the request still succeeds); hiding tools without a discovery path (they would become unreachable); aliasing names (rejected above).

**Detection (2026-10-28):** the lane split was in the 2026-10-23 logs (`cache_waste_report.py` printed root 0.03 against children 0.94) but nothing ran the report, and each cold round's own `upstream_identical`/`cache_write_unsettled` verdict pointed away from the request. **Decision:** `_health.read_share_monitor` folds every round record into a bounded per-minute window and warns while one request group on a model is cold and another warm; the cold side's break reports say so instead of attributing the miss upstream. The nightly optimizer streams the last 24 hours of rotated `app.log` files (personal deployments only, since process logs carry no owner), puts any split first in the proposer prompt with a rule to propose an investigation and never a TTL change, and keeps it in the run summary's `evidence_summary`. `scripts/cache_ab_probe.py --arm` is the manual single-variable A/B for the next quirk; the diagnosis order is in `lib/tasks_pkg/cache_tracking/AGENTS.md`. **Alternatives rejected:** duplicate, keep-alive or warm-up requests to raise reuse (they spend money to hide the signal); automatic probing from production (unbudgeted model calls outside execution accounting); a per-round verdict change (one round cannot know another group is warm, and the bucket stays the factual evidence class). Specifications: `tests/cache/test_cache_read_share_health.py`, `tests/cache/test_cache_record_lane.py`, `tests/optimizer/test_cache_read_share_evidence.py`, `tests/cache/test_cache_ab_probe.py`.

**Over-budget attribution (2026-11-08):** Coordinator-role surfaces were all required-direct (`prepare_coordinator_tool_surface`), and an all-eager local surface carried no discovery pair, so the budget could not hide anything there. The Global Router's Claude rounds carried 35 tools / 687 name bytes, logged `tool-name budget 448 bytes NOT met` on every request and read back 1.2% of their prompt (56 rounds, 2026-11-07 20:57 to 11-08 16:46; 16 of 16 after the 16:55 restart), but each record said `upstream_identical` or `cache_write_unsettled`, so those misses were blamed on the gateway account pool. **Decision:** `_wire_tool_surface` relays the route's budget (`name_budget_bytes`), the record carries `wire_tool_name_budget_bytes`, and a confirmed break on a request sent over its route's budget is bucketed `tool_name_budget` (client-side; counted as recoverable by `scripts/cache_waste_report.py`) ahead of the boundary, write-visibility and upstream verdicts. Unlike the read-share split, the round itself knows it was sent over the measured budget, so this per-round verdict is a fact, not an inference. Spec: `tests/cache/test_cache_tool_name_budget_attribution.py`. **Alternatives rejected:** relying on the `[CacheHealth]` split (it qualifies only the text, needs 20 rounds in both groups, and still bucketed the Router upstream).

**Coordinator-role surfaces (2026-11-08).** **Problem:** with every role tool required-direct and no discovery pair, the budget could not fit any Coordinator-family surface. Eager tool-name bytes per role (`wire_tool_name_bytes` of `[CacheRoundRecord]`, 2026-11-08 about 17:50 to 21:14): Global Router 35 tools / 687 bytes; Project Routers 18/355, 18/355 and 21/416; Watchdog 8/171. Only the Global Router was over 448. Its Claude rounds read 0.87% of 34.5M prompt tokens (97 rounds, 11-07 21:11 to 11-08 21:52), while its Kimi rounds read 58–74%.

**Decision:** reuse the budget fit and the local discovery pair; add no selection mechanism.

- `local_wire_tools(keep_discovery=True)` keeps `search_tools`/`execute_tools` on a small or all-eager surface when this route's budget will hide one of its names (`tool_wire._hides_a_name`), as an explicit schema budget already does. The longest-first fit then hides names on that route only.
- The Global Router's required-direct set is `ROUTER_DIRECT_TOOLS` (`lib/router/policy.py`) instead of every admitted tool. All its role tools stay eager, so routes without a name budget still send the whole surface unchanged.
- The core comes from the Router's recorded calls (2026-11-03 08:18 to 11-08 21:05, 132.8 h, 950 calls, `debug/inspect_conversation.py --json`), most-called first: `get_conversation` 249, `update_task_progress` 112, `route_to_project` 99, `list_conversations` 98, `get_project_workers` 48, `await_project_workers` 48, `channel_send` 37, `channel_outbox` 37, `report_to_user` 37, `get_conversation_status` 31, `get_router_projects` 28, `channel_read` 27, `cancel_project_work` 16, `get_project_assignment` 15, `todo_write` 15, `ask_human` 11, `router_instruction_list` 10, `delegate_project_work` 10, `send_project_message` 5, `router_instruction_create` 4, `review_project_work` 4, `restart_server` 4. That is 408 bytes, 435 with the discovery pair. The next tools by use (`channel_threads` 2, `finish_project_request` 2, `get_project_messages` 1) would push the core and pair past 448, so the budget ends the core, not a chosen count.
- On Claude at `your-llm-gateway.example.com` the fit hides the 12 longest optional names: `get_project_messages`, `consider_project_message`, `control_conversation`, `create_conversation`, `dispatch_project_assignment`, `finish_project_request`, `router_instruction_cancel`, `channel_threads`, `channel_configure_thread`, `channel_subscribe`, `channel_unsubscribe`, `channel_outbox_approve`. The wire keeps 25 tools, 448 bytes. Hidden tools stay in the executable catalog: exact-name calls, `search_tools` and `execute_tools` reach them, and `coordinator_call_rejection` admits them inside the ceiling. This includes `control_conversation(owner_delegation=…)`; `restart_server` stays direct.
- The Watchdog keeps every tool required-direct: its whole ceiling is 211 bytes. Project Coordinators keep their own core (below). A request whose names are all required gets no gateway and logs `NOT met … required direct tools alone exceed it`.

**Evidence:**

- Controlled replay of the archived Router request head (M-TraceId 471826bef57b484bb1e120cf96fdefaa; system and tools; run nonce in the first tool description; one key; one `Mt-Session-Id` per arm; 90 s apart; 2026-11-08 21:49 to 21:55; ¥4.56). With 687 bytes, all three sends wrote 22,600 tokens and the two resends read nothing. With 448 bytes, the first send wrote 18,583 tokens; of four resends, two read the full prefix (miss, hit, hit, miss).
- That matches the read share of in-budget Claude conversation rounds at that spacing (46.0% at 80–160 s). Weighting the in-budget read share per gap (88.8% under 10 s, 27.5% at 160–300 s, 1.2% beyond) by the Router's own recorded gaps gives an expected 44% of its Claude prompt read, against 0.87% now. That is about ¥0.4–0.9K/day of write premium (¥506/day over the 24.7 h window).
- Calls to the hidden tools on Claude rounds: 3 in 132.8 h (0.5/day; 0.9/day across models). At one extra in-turn round each (about ¥2), they cost under ¥2/day.

Specs: `tests/router/test_router_tool_name_budget.py`, `tests/llm/test_tool_wire_name_budget.py`.

**Alternatives rejected:**

- Shorter tool names. Model-visible names are shared by role prompts, tool guidance, search results, execution authority, recorded Turns and every other route; renaming a dozen tools migrates all of them for one gateway's edge (aliasing is rejected above for the same reason).
- Kimi as the Router's primary model. Kimi reads its cache with the whole surface, but the Router's model is the owner's choice (ROUTER.md R5) and changes Router quality; this fix removes the client-side cause and keeps the model.
- Leaving it as is. Every Router Claude round re-writes its whole prompt (about ¥12.6 at 350K tokens); the attribution only names the loss.
- Longest-first with no core. It hides `await_project_workers` (48 calls), `get_conversation_status` (31), `get_project_assignment` (15), `router_instruction_list` (10) and `router_instruction_create` (4), about 20 gateway calls a day, while keeping five never-called tools direct.
- Marking non-core Router tools `searchable`. That defers them on every route, including Kimi, which reads its cache with the whole surface.
- Pinning the tools the role prompts name. That is 20 tools, four never called, and it leaves out `list_conversations` (98 calls) and the channel tools.
- A runtime core ranked by usage. That is a tuned top-k (principle 5) with per-owner state.

**Project Coordinator core (2026-11-08).** **Problem:** STATUS-BOUND added `read_tool_artifact` and `search_tool_artifact` to `COORDINATOR_TOOLS`, so a Project Coordinator's human Turn assembles all 23 role tools, 456 bytes, every one required-direct. From the next restart each of its Claude rounds would go out over 448 bytes and re-write its prompt. Claude-route wire per surface on the current head, measured through `assemble_round_tools` and `project_tool_wire` (the Global Router with its channel tools and readers):

| Surface | Role tools | Before | After |
|---|---|---|---|
| Project Coordinator, human Turn (includes `dispatch_project_assignment`) | 23 / 456 B | 23 / 456 B, no discovery pair | 23 / 435 B, hides `dispatch_project_assignment` and `create_conversation` |
| Project Coordinator, Plan Mode | 10 / 196 B | 10 / 196 B | unchanged |
| Project Coordinator, continuation grant | 18 / 355 B | 18 / 355 B | unchanged |
| Global Router | 37 / 727 B | 25 / 448 B | unchanged |
| Global Router, channel review Turn (`REVIEW_TOOLS`) | 6 / 102 B | 6 / 102 B | unchanged |
| Watchdog | 10 / 211 B | 10 / 211 B | unchanged |

**Decision:** the Router's mechanism, unchanged. `COORDINATOR_DIRECT_TOOLS` (`lib/tasks_pkg/coordinator_policy.py`) is the Project Coordinator's required-direct core. Its other role tools stay eager, and the fit hides the longest of them on that route only.

- The core follows recorded use. The three Project Coordinators' recorded calls (`debug/inspect_conversation.py --json`: coordinator-1a390889 2026-10-16 09:07 to 11-08 21:55, coordinator-edae46bf 11-03 16:30 to 11-08 22:16, coordinator-2666b08e 11-06 11:26 to 11-08 19:38; 3,613 calls), most-called first: `await_project_workers` 1,046, `delegate_project_work` 451, `send_project_message` 325, `review_project_work` 298, `get_conversation` 226, `update_task_progress` 213, `get_project_workers` 185, `consider_project_message` 176, `cancel_project_work` 134, `report_to_user` 121, `get_conversation_status` 95, `get_project_assignment` 84, `get_project_messages` 73, `todo_write` 55, `list_conversations` 44, `ask_human` 36, `finish_project_request` 16, `control_conversation` 15, `dispatch_project_assignment` 4, `create_conversation` 2, `search_tool_artifact` 1 (refused), `read_tool_artifact` 0, `await_device` 0.
- The readers' call counts are censored: this ceiling refused them until STATUS-BOUND. Their use is the partial results whose only recovery they are (PROJECT_COORDINATION.md, "Partial results are readable by every Coordinator role"). The record holds 222 such envelopes from the results STATUS-BOUND left unbounded (`get_conversation` 132, `get_project_assignment` 79, `await_project_workers` 7, `dispatch_project_assignment` 4), about 30 a day across the three in 11-04..08. That ranks the readers after `get_conversation`.
- In that order the core is the first 18 tools (the continuation grant's whole set) and both readers: 20 tools, 395 bytes, 422 with the discovery pair. The next tool, `dispatch_project_assignment`, would bring it to 450, so the budget ends the core.
- On Claude at `your-llm-gateway.example.com` the fit hides `dispatch_project_assignment` and `create_conversation`. The wire keeps 23 tools, 435 bytes; `await_device` (13 bytes) still fits. Other routes are unchanged. Continuation grants are unchanged too: their 18 tools fit and are all in the core, so they get no discovery pair.
- Hidden tools stay reachable: `search_tools` finds them, and `execute_tools` and exact-name calls are admitted. Parsing admits any name in the executable catalog (`tool_dispatch._labels._known_tool_names`), and `coordinator_call_rejection` checks the ceiling. The partial-result recovery is direct here. The Router's Claude wire hides both readers; an exact-name call with the hint's arguments parses and is admitted there.
- As for the Router, an explicit `tools.schemaBudgetTokens` (default 0) can also defer non-core role tools behind the discovery pair, and a narrower replayed initial visibility (set only by child policies) can leave them off the wire. They stay executable by exact name.

**Evidence and effect:**

- Project Coordinator Claude rounds from 2026-11-03 06:20 to 11-08 22:16 (5.66 days): 276 on human Turns (48.7/day, 60.3M prompt tokens, 39.6% read), 1,178 on continuations (28.5% read). At 456 bytes the human-Turn rounds would read nothing. Writing their 23.9M read tokens instead would cost about ¥830 over the window, ¥147/day (the records' ¥1.45/M read and ¥36.24/M write).
- How Claude reaches a reader missing from its request: after three partial results (2026-11-04 and 11-05, all `claude-opus-5.5`) the Coordinator called `search_tool_artifact` four times, once by exact name and three times through `execute_tools`, each looking up one field (`checkpointSha`, `grantRevision`). None went through `search_tools` first. Without the schema, two of the four were malformed (no arguments; `tool` for `name`), each costing a retry round. All four were refused, since the ceiling lacked the readers.
- Hidden-tool use: `dispatch_project_assignment` 4 calls (11-03 and 11-06, three on Claude), `create_conversation` none since 10-16 (four that day, all on Kimi). That is under one extra round a day at about ¥1–2 each.

Specs: `tests/tasks_pkg/test_coordinator_tool_name_budget.py` covers every Coordinator-family surface on the Claude route, through its real assembly or the role policy over its whole ceiling; it fails first at 456 bytes. Also `tests/project_mod/test_coordinator_continuation.py::test_continuation_surface_goes_whole_on_the_claude_name_budget_route`.

**Alternatives rejected:**

- Ranking by recorded calls alone. It hides both readers to keep `dispatch_project_assignment` (4 calls) and `create_conversation` (2) direct, yet about 30 partial results a day name the readers as their recovery. Calls to a reader without its schema were malformed half the time above.
- Pinning only `read_tool_artifact`, the reader the continuation marker names. That keeps `dispatch_project_assignment` (4 calls) direct and hides `search_tool_artifact`, yet every reader call in the record was `search_tool_artifact` (4 calls), two of them malformed without its schema.
- Dropping `dispatch_project_assignment` or `create_conversation` from the ceiling. That changes the role's authority to save bytes.
- Dropping the readers from the ceiling. Partial results would be unreadable again, the problem STATUS-BOUND fixed.

### 7. Cache affinity carriers

**Problem (2026-11-02):** each hop between Tofu and the model picks a cache domain for every request: Tofu picks an API key, a gateway picks an upstream account, a provider picks a shard. FRIDAY (`your-llm-gateway.example.com`) serves each model from several accounts, keeps caches per account, and hashes the `Mt-Session-Id` header to pick the vendor and then the account (学城 2762588777 《Claude 模型接入推荐实践》 §5.1). Tofu sent only a random per-request `M-TraceId`, so the gateway placed each request independently.

- Conversation `mvywcu5qqy5ocy` (21 rounds, `kunlunbedrock.claude-opus-5.5-m17`, one Tofu key throughout) sent byte-identical prefixes less than a minute apart (TTL 5 min). Thirteen of its 20 follow-up rounds landed on a different account than their predecessor: six read nothing and seven read exactly an *older* round's cached total. Two of them answered in another vendor's SSE format. Input cost was ¥31.03, against ¥7.44 had every round read its predecessor; ¥23.48 of the gap was account hopping.
- Across 3,954 Claude rounds that afternoon, previous-entry reads stayed at 30–42% whether 3 or more than 25 Claude requests were in flight, while Kimi on the same gateway read 94–99.7%.
- From 14:54 to 16:39, `upstream_identical` was 95.8% of recoverable cache waste.

**Decision:** `lib/llm/cache_affinity.py` derives one key per lineage and hands it to every hop through the carrier that hop documents.

- **Lineage key.** `cache_affinity_key` is a UUID-shaped SHA-256 digest of the request lineage. The lineage is the child invocation for a child (`invocation_affinity_id`, the scope Tofu's own key stickiness already uses), else the conversation, else the L2 summary series' own id, else the task. Round requests and rebuilt fallback attempts carry it in `_cache_affinity_scope`. The digest equals what Responses `prompt_cache_key` and the Codex session headers already sent, so live keys did not rotate. One key per lineage keeps each key at the pace of one agent loop.
- **Carriers.** Each carrier is a provider contract, declared per route as a closed-world route feature `cache_affinity_<carrier>` (`lib/llm/route_features.py`) and bound to its header in `CACHE_AFFINITY_HEADER_CARRIERS`.
- **Where headers are added.** Both outbound header assemblies (`_sse_core.prepare_request` and non-streaming `chat.py`) call `merge_cache_affinity_headers`. An owner-configured header of the same name wins.
- **YourProvider declaration.** The template declares `cache_affinity_mt_session_id` for native chat and its `anthropic` and `kunlun` faces. Fresh installs inherit it on every face. Template supply converges declared face Connections; already-installed default chat Connections remain owner-managed and need an explicit owner-scoped configuration update. The TB evaluation Connection declares the same carrier and its relay passes it through for all clients and retries without synthesizing absent identities.
- **Feature merge.** Feature maps now resolve per feature across model, offering, connection and deployment, so a face's binding no longer erases model-level features.
- **Evidence.** The routing fingerprint carries `affinity` (the carrier slots plus a short digest of the key), so a lineage whose key or carrier changes shows as `<ns>affinity`. `[CacheRoundRecord]` records `affinity`, and the read-source table of `scripts/cache_waste_report.py` splits by it. An `older` read on a lineage that carried a key means a hop ignored it; report it to the provider with the M-TraceIds.
- **Adding a provider.** Add one carrier entry with its source, register the feature (a gateway header in `_GATEWAY_FEATURES`; a body field gets its own protocol binding and serializer, like `responses_prompt_cache`), declare the feature on the template face, and add a test.

Specs: `tests/llm/test_cache_affinity.py`, `tests/providers/test_yourprovider_cache_affinity.py`, `tests/evaluations/test_terminal_bench_cache_affinity.py`, `tests/model_routing/test_template_supply_sync.py::test_cache_affinity_carrier_converges_onto_installed_claude_face`, `tests/model_routing/test_route_inference_contract.py::test_route_features_resolve_per_feature_across_entities`, `tests/cache/test_cache_waste_report.py`.

**Verification:** the effect is measured on ordinary traffic after deployment with that read-source table: rows with `Mt-Session-Id` should read `older` close to 0%. No warm-up or duplicate request is sent (owner decision 1). Result on 2026-11-03 (`--model claude-opus-5.5 --since '2026-11-03 09:14:32' --until '2026-11-03 10:57:49'`): not met. Claude follow-up rounds read their predecessor in 72–75% of rounds (30–42% before the carrier) and `older` in 5–7%, while Kimi on the same gateway and carrier read 98–100% with no `older` rows. `upstream_identical` and `cache_write_unsettled` were 96% of the ¥1,369 recoverable in those 1.7 hours. The Claude hops are synchronized across independent lineages (a miss made a miss in another lineage sent within ±5 s 2.8× likelier), 21% of misses returned to an older round's exact entry on the next round, and three inspected A→B→A hops kept the same SSE format: account moves inside one vendor, which FRIDAY's hash-modulo affinity (§5.1) produces for nearly every session whenever its account set changes. Thinking parameters stayed constant inside every lineage, and one `cache_namespace_switch` remained after the 2026-11-03 account-status fix. Tofu cannot see the serving account: the gateway forwards no workspace header and does not deliver the request `diagnostics` object to the API (a two-request synthetic check with a changed system prompt returned `null`, which [cache diagnostics](https://platform.claude.com/docs/en/build-with-claude/cache-diagnostics) rules out once the object arrives). Open with the gateway owner, with M-TraceIds: consistent or rendezvous hashing so a membership change moves only the affected account's sessions, and forwarding `diagnostics` plus an opaque account label.

**Controlled replay (2026-11-08):** with `Mt-Session-Id` on every round (3,238/3,238 Claude rounds 11-07 20:57 to 11-08 09:10), Claude still read only 62-69% of its prompt while Kimi on the same face read 90-99%. Of the lost Claude reuse 11-07 20:57 to 11-08 16:46 (97% of Claude write tokens re-wrote an already-sent prefix), 87% came from in-TTL rounds whose sealed per-message bytes, system/tools/controls region and routing (key, beta, endpoint, carrier digest) all matched the previous round; per-minute miss rates across independent lineages are over-dispersed (dispersion index 5.3: some minutes miss 90-100% of all rounds, others 4%). Eight synthetic requests through one key, the production header set and one `Mt-Session-Id` (identical body SHA, ~11K-token prefix, 5m markers) gave: cold write; identical resend right after the response MISS; +30 s MISS; +90 s hit; append-only growth hit; two parallel identical sends both wrote, a third read. Byte-identical bytes plus a controlled miss place this class upstream; it is not the visibility window `cache_write_unsettled` assumes either (the +30 s resend missed). The Global Router's misses were the exception: they were client-side (`tool_name_budget`, section 6).

**Per-request upstream routing (2026-11-08, second replay):** the misses are per-request server routing, not a delay before a write becomes readable. Nineteen synthetic sends and one metadata probe (key 0, production headers, one `Mt-Session-Id` per session, 6.2K–11.4K-token prefixes, ¥4.8 in total):

| test | sends | result |
|---|---|---|
| one write, identical resend at +150 s | 3 sessions | 0/3 read |
| one write, three identical resends back-to-back | 1 session | miss, miss, read |
| one 1h write, identical resend at +390 s | 2 sessions | 0/2 read |
| prefix P, then P + 30 one-block messages with a marker at P's end | 2 | read 11,080 (all of P), wrote 321; one send missed outright |
| control: same, no marker at P's end (30 blocks back) | 2 | read 6,142 (tools and system), wrote 5,259 |

- **No visibility delay.** A delay shorter than 150 s predicts three reads at +150 s; a longer one cannot produce the back-to-back read. In production, after a cold write the next round read it less often the longer it waited (see section 11). Amazon Bedrock's [prompt-caching guide](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html) states the mechanism: a cache entry is stored on the server that processed the request, and a request served by a different server, or sent before an identical request has started its response, writes a new entry.
- **No upstream identifier.** Response headers are `Server`, `Date`, `Content-Type`, `Connection`, `M-TraceId` and `Vary`. Message IDs are time-ordered `msg_011Cgq…`, `diagnostics` is `null`, and usage reports `inference_geo: "global"`, which [lets inference run in any available geography](https://platform.claude.com/docs/en/manage-claude/data-residency). `"us"` costs 1.1× on every token category, and FRIDAY's support for it is unverified.
- **Tofu sends one stable key.** The header is a digest of the lineage (`invocation_affinity_id`: the conversation, or the child invocation for a sub-agent), and so does not change across rounds, turns, retries, key rotation, fallback or restarts. All 4,532 post-restart Claude records (16:55 to 21:25) had an empty `routing_diff`. No Claude round changed key or retried, and the 13 key rebinds were all on Kimi. Of the post-restart Worker re-write premium (¥127.8K/day at that burst's rate), 87% was in-TTL identical-bytes rounds and 11% a round whose reported prompt was shorter than its predecessor's. Those followed a missed round 85% of the time, and where inspected Tofu's own `promptAdmission` count kept growing. The remaining 2% was gaps over 5 min (1.6%) and first rounds (0.4%). Detector-confirmed byte changes (L1 tool-result stubs, four compactions, rebuilt first rounds) were under 1%, inside those classes.
- **Older breakpoints.** A marker at an older boundary reads that entry while the request appends (rows 4–5). Older-entry reads in production stop at nine requests back, the tail marker's lookback. Extrapolating their geometric decay, about 70 such reads per 19.8 h (before the restart) and 47 per 4.5 h (after) were lost beyond it. That is at most ¥0.9K/day and ¥2.6K/day at those rates, even if a marker caught every one. The free marker would have to come from the system pair, and the 2026-07-20 mid-anchor A/B (`lib/llm/cache.py`) found a trailing anchor net-negative. Layout unchanged.
- **A 1h write before a long wait.** Per role, consecutive Claude requests in one lane more than 5 and at most 60 minutes apart were 70/58/31/38/25/14 (chats, Workers, Project Routers, chat sub-agents, Watchdog, Global Router) of 1,862/3,831/167/4,315/79/42 requests before the restart. Writing 1h exactly before those gaps nets ¥+0.06K, +0.09K, −0.08K, +0.04K, −0.16K and −0.10K a day at a 31% read after the wait, the in-TTL rate at 160–300 s. The replay read 0/2. No request precedes a declared wait: the wait arrives in the response, and a warm-up request is excluded by owner decision 1. A role-wide 1h rule loses on every role, by up to ¥8.8K/day. Re-writes after 5–60 min gaps were 9.2% (before) and 2.5% (after) of Claude write tokens, under the 40% merge condition of the held uniform 1h tail. A read cuts provider TTFT on prompts of 300K tokens and up from 18.7 s to 9.7 s (median).
- **Concurrent sibling starts.** Swarm siblings that start within seconds of each other each write their ~25K-token first prompt: 21 extra starts in 19.8 h, about ¥18/day. Staggering them would add the first sibling's TTFT to every swarm; not changed.

**Long-context Workers (2026-11-09).** **Problem:** the Terminal-Bench 2.1 Opus comparison found no Tofu-caused misses, but its prompts were 18–30K tokens and it never compacted. After the 2026-11-08 restart, Workers sent most Claude spend at 200K–779K tokens. **Evidence:** every Worker Claude round from 11-08 16:55:56 to 11-09 06:35:46 (13.7 h), chained per conversation. A miss is a round whose read fell more than 2,048 tokens short of what its predecessor left cached; its premium is the re-written tokens × (1.25 − 0.10) × input. Per day at that window's rate:

| prompt | requests | hit | misses | miss premium | identical bytes (gateway) | idle > 5 min | Tofu change |
|---|---:|---:|---:|---:|---:|---:|---:|
| 200–500K | 4,864 | 60.3% | 51% | ¥39.5K | 97.8% | 1.8% | 0.4% |
| ≥ 500K | 2,615 | 42.7% | 67% | ¥53.2K | 96.8% | 3.2% | 0 |

- "Identical bytes" means the detector compared every previously sent message, the system and tool regions, the cache controls and the routing (key, beta, endpoint, `Mt-Session-Id` digest), found them unchanged, and the round came inside the TTL. The archived request bodies confirm it independently. In 349 of 349 consecutive pairs (41 Workers, ≥ 200K tokens), each request's block stream in cache order begins with the previous request's, byte for byte. The controls are unchanged, and the tail marker sits 2–8 blocks after the previous one, inside the 20-block lookback. 220 of those rounds missed. Kimi Workers on the same gateway read 97%.
- One in fifteen identical-bytes misses reported a smaller prompt for a longer body (in one pair 16,551 fewer tokens for 9 KB more): the serving upstream counted differently.
- The only Tofu-side divergences were the ten route-window compactions. Each took the prompt from about 776K tokens to 258–474K (L1 tool-result clearing plus an L2 summary) and re-wrote it once for ¥8–16, ¥177/day in total. Before the restart (19.8 h, 2,146 requests ≥ 200K) the shares were 92.9% identical bytes and 6.9% idle > 5 min.
- Misses did not follow Tofu's concurrency (50–60% at 0–2 to 10+ Claude requests in flight) or its credential (54–59% on each of three). After the restart they rose with prompt size: at an idle under 10 s the next round read everything 57%, 50% and 35% of the time below 200K, at 200–500K and at ≥ 500K (58%, 65% and 64% before the restart).

**Decision:** no Tofu change. The ¥90K/day identical-bytes residual is FRIDAY's routing (above). **Alternatives rejected:**

- Compacting on a stable cache boundary. Each compaction already keeps the system+tools head and starts re-writing at wire message 2–4, because the cold tool results it clears start there. A later boundary leaves less to compact, and the most any boundary could recover is the ¥177/day the compactions cost, at the roughly even chance that the next round lands on the same account.
- Another credential or a concurrency limit: no dependence (above).
- A 1h TTL against the idle share: every account hop re-writes at 2.0× ([section 9](#9-stable-head-cache-ttl)).

**Native chat evidence (2026-11-07):** a paired GLM-5.3-Flash replay of ten conversation histories (264 requests per arm) read 82.8% of input tokens without the carrier and 95.6% with it, keeping system prompts fixed. Of 254 follow-up requests per arm, 197 versus 2 read at least 64 tokens less than their predecessor's input. This measures incomplete prefix reuse, not zero cache hits: only one baseline follow-up read zero. The replay generated 16 tokens per request and used user-message nonces to isolate histories; it is mechanism evidence, not a full-task or production performance measurement. Prefix placement and sharing changed reuse, but neither a fixed 10K-token routing boundary nor a particular serving replica is established. Gateway affinity remains best-effort.

**Read-source chain identity (2026-11-07):** the table chains rounds by the log `[rid:]` plus the record's `conv`. Durable tasks all log `[rid:conversa]`, and `conv` held only the first 8 characters, so concurrent `worker-<hex>`/`coordinator-<hex>` conversations merged: in the 2026-11-07 20:41–22:13 logs ten 8-character keys each named 2–10 conversations (joined to whole IDs by `trace_id`/`M-TraceId`), and the merged chains dropped 214 of 1,350 classifiable `claude-opus-5.5` follow-up rounds while shifting `unmatched` from 3.5% to 6.0%. Records now carry the whole conversation ID; earlier logs keep the truncated key, so read their tables with that bias.

**Alternatives rejected:**

- A session marker in system messages or tool definitions. It changes model-visible context and cross-session prefix sharing to influence undocumented routing, while the header carries the identity separately.
- Keep-alive, duplicate or warm-up requests (owner decision 1).
- Concurrency gates or launch staggering: previous-entry reads did not depend on concurrency.
- One conversation-wide key for every child. It concentrates a swarm on one upstream account, and on OpenAI it exceeds the documented per-key overflow rate.
- Deriving carriers from the gateway host at request time. That bypasses the routing aggregate that owns route capabilities and is invisible per Connection.
- A placeholder value in `extra_headers`. Every other sender of those headers (probes, discovery, native routes) would have to substitute it or would leak it.
- A new Connection schema field. It needs a migration and editor work for what the closed-world feature map already expresses.
- A 1h tail TTL. It makes each hop dearer, because 1h writes cost 2.0× input against 1.25×.

### 8. L1 edits of the previously sent request

**Problem (2026-11-02):** prefix protection left the last `EDITABLE_TAIL_COUNT` (2) api-form messages of the previous request editable, and L1 never priced an edit there. In round 3 of conversation `mvywcu5qqy5ocy` the previous request ended in a four-result parallel batch, so the count-2 tail split one Anthropic user message. `strip_cold_images` replaced one 778×76 crop (~80 tokens) with a ~60-token stub. That re-wrote the whole merged message: 3,624 tokens at the 1.25× write rate to remove about 20 tokens.

**Decision:**

- **Protection.** The previous request's api-form messages are the provider-cached prefix. `get_cache_prefix_count` returns the whole sent count, and the durable high-water mark records the same count.
- **Admission.** L1 may edit only that request's last tool step, after a detached dry run, and only when `after_tokens × cacheWriteMul < before_tokens × cacheReadMul` on the main route's verified rates (`_cache_economics.sent_step_rewrite_economics`). Unverified pricing keeps the bytes.
- **Advertising.** Admitted edits are advertised as bounded rewrites (`notify_compaction(intended_rewrite_index=...)`), and every decision logs one `[L1] ... sent-step edit admitted|refused` line.
- **Image pricing.** On Claude routes, images in that estimate and in `strip_cold_images`' savings report are priced from their pixel size (Anthropic's documented `w×h/750` after downscaling), not a flat 800.
- **Effect.** The incident edit is now refused. A consumed 20K-token result shrunk to a 200-token stub still compacts (375 < 2,010 input-token equivalents).
- **Diagnosis.** `EDITABLE_TAIL_COUNT` remains a diagnosis tolerance for wire-tail representation only. The detector's prior-prefix position test now counts wire messages on both sides, so a re-bill of the previous request's last wire message is recorded as a tail re-bill (`wire_changed`, culprits named per tool result), not a whole-prefix break.

Specs: `tests/compaction/test_l1_sent_step_admission.py` and `tests/cache/test_cache_prefix_mutation_attribution.py`.

**Alternatives rejected:**

- Protecting every sent byte. This loses compaction of large consumed results that pays for itself on the next request.
- A multi-round payback horizon. That is a tuned knob.
- Keeping the count-2 tail. It splits parallel batches and prices nothing.

### 9. Stable-head cache TTL

**Problem (2026-11-03):** `cache_extended_ttl` was on by default, so every write of the Claude system+tools head was billed at the 1h rate (2.0× input) instead of 1.25×. On the Sankuai Claude route the upstream account hops of §7 re-write the head on most misses. The retained logs for 2026-11-03 (06:48 to 16:00) hold 5,681 Claude `[CacheRoundRecord]` rounds. The conversation lane wrote its ~28.2K-token head 1,277 times: 19 first rounds and 1,258 zero-read rounds inside a chain. The isolated lane wrote its ~10.2K-token head 120 times. Together that is a 27.94M base-input-token premium. Only 31 rounds read an entry after a 5–60 min gap, 281 read only the head, and 11 first rounds read a head another lineage wrote. Even counting all 323 as saved re-writes (1.2 × head each), the 1h head saved at most 10.65M, a net loss of at least 17.29M (conversation lane 16.53M, isolated lane 0.76M). Every log window was negative on its own. Conversation `mw0a6pf2lv6qf4` (17 rounds) re-wrote its head 12 times (334K tokens) and cost ¥34.83; with a 5-minute head it would have cost ¥27.58. FRIDAY's guidance (学城 2762588777 §1.3) recommends 5m for high-frequency agent loops and considering 1h only at a steady read share ≥ 90% with call intervals over 5 minutes; this route read its previous entry on 72–75% of rounds.

**Decision:** `cache_extended_ttl` defaults to off. Every Claude breakpoint is a bare 5-minute marker, and the `extended-cache-ttl` beta header is not sent. The per-task latch is unchanged: a running task keeps its layout and the next task uses the new one, so each conversation writes its prefix once under the new markers. Owners can still opt in through `cache_extended_ttl` in `features.json`, `POST /api/v1/features` (hot reload) or `CACHE_EXTENDED_TTL=1`; the opt-in keeps the 1h head and the 5-minute tail. Spec: `tests/cache/test_cache_stable_head_ttl_default.py`. Method for re-measuring: per lane, take the head size H as the median head-only read; the premium is 0.75 × H per head write (a first round or a zero-read round), and the saving is at most 1.2 × H per read after a 5–60 min gap, head-only read, or first-round read of another lineage's head.

**Alternatives rejected:**

- Keeping the 1h head on by default. Measured net-negative above.
- Gating the 1h head per route or lane on a measured read share. That is a tuned threshold (owner decision 5).
- Predicting long gaps from history. The tail measurement above found precision 0.09.
- Removing the option. A route that keeps a session on one cache domain and has long user pauses can still profit.

### 10. Cache breakpoints on profiled routes

**Problem (2026-11-03):** `_gateway_honors_cache_markers` skipped all markers when `route_feature(profile, 'chat_cache_breakpoints', ...)` was `False`. `route_feature` is closed-world: it answers `False` for an undeclared feature and for a feature of another wire. So every request with a route profile, on any wire, left without `cache_control`. The first boot after that gate landed (21:20) sent every `kunlunbedrock.claude-opus-5.5-m17` request with 0 markers, and every Claude round read and wrote 0 cache tokens. Each prompt token was billed at the input rate (20× the read rate), and the cache diagnostics filed these rounds as `no_break`, because a route that meters no writes looks like an unmetered gateway.

**Decision:** only an explicit OpenAI-wire declaration other than `supported` skips the markers, as [llm_io](modules/llm_io.md) states. An undeclared profiled route, or another wire, keeps the model-family policy. Spec: `tests/cache/test_cache_markers_route_profile.py`, which covers both wires and the final Anthropic wire of a profiled Claude route.

**Alternatives rejected:** declaring `chat_cache_breakpoints: supported` on every Claude face. The feature is bound to the OpenAI wire, so no declaration can enable it on the Anthropic wire, and a missing declaration must not cost the whole cache.


**Detection (2026-11-04):** Claude caches only at explicit markers, so a follow-up Claude round whose prompt reaches the largest documented minimum cacheable length (`MIN_CACHEABLE_PROMPT_TOKENS`, 4,096) and reads and writes 0 paid the base input rate for its whole prompt. Such a round is recorded in the `uncached` bucket instead of `no_break`, and `[CacheTrack] … UNCACHED` names the side from the wire-marker count: 0 markers implicates the client's breakpoints, one or more the gateway or route. `scripts/cache_waste_report.py` prints the count next to the zero-readback rounds. The detector still returns no break, so the round's `cacheBreak` and the cost popover are unchanged. Specs: `tests/cache/test_cache_verdict_uncached_bucket.py`, `tests/cache/test_cache_waste_report.py`. **Alternatives rejected:** a confirmed break (the round lost no previous read, and without wire markers no culprit can be named); counting the first round of a thread (an OpenAI-compatible gateway may meter only reads, which reports a cold first write as 0); every model family (automatically caching providers report 0 on legitimately cold prompts).

### 11. Cache-break attribution corrections

**Problem (2026-11-03, 578 Claude rounds 19:49 to 20:42):** three classifier gaps misplaced cache spend.

- A head-only read (the shared ~28K system+tools) right after a zero-read miss was `no_break`: `partial_no_reuse` compared the read with the previous READ (0). That was 18 rounds and about ¥104. The same test flagged a fully read prefix extended by 20K+ writes.
- A change of `speed`, `tool_choice`, `thinking` or `output_config.effort` invalidates cached messages with identical prompt bytes ([invalidation table](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)). The byte region hashed only system and tools, so such a client change would have been filed as `upstream_identical`. The retained round parameters showed no such change in the zero-read rounds.
- Dispatch rebinds the sticky key to a second FRIDAY AppId on a 429. The rounds right after a rebind read the entry the other AppId wrote, and back (`f0194ed0` R865 and R870, `8e4ec142` R97, R99 and R109): FRIDAY picks the account from `Mt-Session-Id`, not from the AppId. The two post-restart zero-reads after a rebind were upstream misses, yet were filed as `cache_namespace_switch`.

**Decision:**

- `partial_no_reuse` measures the read against the previous round's read + write.
- `wire_byte_region` carries a canonical hash of those controls, and `<bytes>controls` counts only when the read fell short of the cached prefix: an equivalent value, such as explicit default effort, invalidates nothing.
- `CACHE_DOMAIN_CARRIERS` (`lib/llm/cache_affinity.py`) names carriers whose hop picks the cache domain itself. With such a carrier unchanged, a key flip is not a namespace switch, and the upstream verdict no longer claims identical routing.

Specs: `tests/cache/test_cache_break_prefix_baseline.py`, `tests/tasks_pkg/test_wire_cache_controls.py`, `tests/cache/test_cache_namespace_fingerprint.py`.

**Alternatives rejected:**

- Moving `api_break` to the read + write baseline. That starves the more specific `no_reuse` branch and shifts `_read_collapsed` everywhere.
- Counting every control change as a culprit. A cache-hit round with a 2K+ tail write would become a false prefix-mutation break.
- Ignoring `<ns>key` on every route that carries a lineage key. `prompt_cache_key` and the Codex session headers are read by providers whose cache follows the credential's organization or account.

**Measured residual (same window):** 77% of Claude cached-input spend was re-written prefix, about ¥572 in 53 minutes, all of it on byte-identical bodies. Head-only and older-entry reads followed a Claude 429 ("This request would exceed your account's rate limit") in the preceding 90 s in 57% and 76% of cases, against 12% for clean hits. That fits §7's hash-modulo affinity re-mapping sessions whenever an upstream account saturates. The gateway owner's fix (consistent or rendezvous hashing) remains the only lever.

**Write-visibility verdict (2026-11-08).** **Problem:** a read collapse with identical bytes and routing that came within the settle gate's cold window (18 s) of the previous round's cold write was filed `cache_write_unsettled`: "a CLIENT-side timing race … the cache-settle gate is meant to hold the next send". After the 16:55 restart that was 227 Claude rounds in 4.5 h and 9.8% of the Worker re-write premium. The premise did not hold:

- An entry is readable once its own request has started its response (Bedrock [prompt caching](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html), §7). Tofu sends the next round only after the previous response ended.
- After a cold write, the next round read the new entry in 51–54% of rounds at 5–10 s, 39–43% at 10–18 s, 25–31% at 18–30 s and 17–28% at 60–300 s. That is in-TTL Claude-after-Claude rounds, 11-07 20:57 to 11-08 21:25, excluding the Global Router. The rate falls with the wait instead of recovering after a window; after a warm round it falls from 84% to 53–60%.
- Single writes re-sent identically at +150 s read 0/3 (§7, second replay).

**Decision:** the detector no longer names `cache_write_unsettled`; these rounds get the byte-identical upstream verdict (`upstream_identical`). The bucket and its legacy rendering stay for records written earlier. The opt-in settle gate (`TOFU_CACHE_SETTLE`, default off) is unchanged, and its module header records that its premise failed here. Spec: `tests/cache/test_cache_sequential_cold_write_attribution.py`.

**Alternatives rejected:**

- A shorter window. No window fits, because reads fall with time.
- Enabling the gate. It holds the next round up to 18 s after every cold write and cannot change which server serves it.
- Keeping the label as a hypothesis. It counts upstream misses as a Tofu timing cause and points at a fix with no effect.

## Claude effort and prompt cache

Anthropic's [effort](https://platform.claude.com/docs/en/build-with-claude/effort) and [thinking/cache](https://platform.claude.com/docs/en/build-with-claude/thinking#thinking-and-prompt-caching) contracts render resolved effort/thinking configuration into the prompt: changing effort changes the prefix, not necessarily deleting an entry. Message breakpoints miss; tool/system breakpoints may also miss. Omitted versus explicit default high is not an effort change. Keep effort stable when cache reuse matters.

The [Opus 5 migration](https://platform.claude.com/docs/en/models/opus-5/migration-guide) documents `mid-conversation-output-config-2026-07-01`: an empty system message can update effort before the next user turn while preserving the initial top-level value. Tofu does not enable this or task/tool-change betas: current system hoisting is not lossless and Sankuai support is unverified. Old `cache_edits` experiments prove neither beta's support.

Exact-route verification holds prefix, breakpoints, model/account/headers and TTL constant, varying only effort in A/A/B/B/A. Establish A's warm read, then B's miss/rewarm and A's reuse from raw usage. Missing cache fields mean unknown, not zero; HTTP 200, latency and offline final-wire tests do not prove upstream consumption or cache hits.

### Long waits and the conversation-tail TTL

**Problem:** two waits returned early only so another provider request would land before the 5-minute conversation-tail cache entry expired: `poll` clamped `wait_s` to 60 s and `run_command` handed a still-running command to the background after 60 s. Each forced round re-reads the whole prefix and adds output tokens and latency; on 2026-10-27/28 the root lane made 87 such polls (median prefix 382K tokens, ~3.3M base-input equivalents) and swarm workers 34 (~0.58M). Owner decision 1 (2026-10-28): no model request may be added just to keep a prompt cache warm, because extra requests cost money and time.

**Decision:** waiting costs no model request. `poll` blocks up to `POLL_WAIT_CAP_S` (1500 s) and returns as soon as the handle finishes, input is waiting for a conversation-owning turn (a user steer or another background completion, both drained at the next round boundary), or Stop arrives; Stop ends a swarm wait without cancelling the longer-lived session. `run_command` hands a running command off after `AUTO_BACKGROUND_AFTER_S` (120 s, owner decision 2 below). Both clamp through `reaper_safe_wait_s` to `TOFU_STUCK_TASK_MAX_SILENT_SECS` (default 1800 s) minus 300 s, never below the former 60 s: an ordinary tool's heartbeat does not refresh the reaper clocks (owner ruling 2026-07-31), so a silent wait must end first. `poll` declares a 1560 s tool timeout so the 300 s parallel-pool ceiling cannot cut it short ([timeouts](contracts/TOOL_EXECUTION_POLICY.md#timeouts-and-loop-guards)). The tail keeps the 5-minute TTL: a wait longer than five minutes re-writes the tail once (1.15 × body in base-input units), which costs less than the removed 60 s polls (0.1 × prefix each) beyond about 11.5 minutes and more between 5 and 11.5 minutes. Cache diagnostics take the expected-expiry window from the previous round's shortest wire-marker TTL, so only a gap beyond the TTL actually sent is filed as `ttl_expiry`.

**Alternatives rejected:** keep-alive requests during a wait (on the same history 82 extra requests costing ~1.68M base-input equivalents plus output tokens and latency; owner decision 1); keeping the 60 s forced rounds (the cost above); a reaper exemption for a waiting `poll` (reserved for human waits; a bounded wait needs none); a history-predicted 1h tail ("previous gap was long" marked 68 requests, 6 then waited long: precision 0.09, net −6.0M).


**Owner decision 2 (2026-11-03): hand off at 120 s in every composition that can collect the result.** Problem: at 1500 s a foreground command held its tool call, and with it the agent, for up to 25 minutes, and a task without a conversation (headless requests, storage-free runs) never handed off at all. On the 2026-11-02 Terminal-Bench 2.1 run (storage-free runtime, 6 h budget per task), 22.8 h of the Tofu arm's 65.7 h agent time over 79 verified trials was spent in tool calls still running when the budget expired; one command held its run for 5.5 h. A return after more than five minutes also re-reads the whole prefix uncached. Decision: `AUTO_BACKGROUND_AFTER_S` is 120 s, inside the 5-minute tail TTL, so the next request reads a warm prefix and the model can inspect, `kill` or `poll` the command; a longer wait is an explicit `poll`, which costs no model request and returns as soon as the command finishes. A task without a conversation receives the result at its next round boundary (`background_commands.take_run_completions`). The handoff adds one request per command that outlives 120 s; it returns control of the agent rather than only keeping a cache warm, so decision 1 still governs waits. Alternatives rejected: keeping 1500 s (a stuck command holds the agent for 25 minutes and returns to a cold prefix); no handoff without a conversation (the loss above); a forced per-command timeout (destroys running work; models cannot predict durations).

**Held: a uniform 1h tail.** Giving the tail the latched 1h TTL (every Claude breakpoint 1h) removes the re-write after any wait under an hour. It is implemented, tested and live-verified as the commit "perf(cache): give the Claude conversation-tail breakpoint the latched 1h TTL" on branch `perf/ttl-waits-20261028b`, but not merged. Its proposal priced the premium at 0.75 × min(write, median steady delta ≈ 1.8K) per round: +7.5M net on 1,695 swarm rounds (1.74M premium against 9.29M saved on 46 long-gap re-writes of 8.08M). A 1h marker, however, bills every token it writes at 2.0 instead of 1.25: first rounds, large tool results, compaction rebuilds and re-writes after upstream misses. Over every Claude `[CacheRoundRecord]` from 2026-10-27 00:00 to 2026-10-28 16:50:

| lane | rounds | 5–60 min gaps | saved | tail writes (upstream misses) | net, all writes | net, misses excluded |
|---|---:|---:|---:|---:|---:|---:|
| root | 2,897 | 6 | 3.6M | 487M (418M) | −359M | −46M |
| swarm | 7,522 | 115 | 28.5M | 847M (609M) | −588M | −131M |

Since the tool-name budget took effect (2026-10-28 14:14, about 2.6 hours) the nets are −30M / −6.4M (root) and −128M / −11.2M (swarm). They bound the premium from above, because records do not separate the system/tools writes that are already 1h. Merge condition: on a fresh measurement, long-gap re-writes exceed about 40% of all tail writes (each re-written token saves 1.15, every other written token costs 0.75 more), which first needs the remaining `upstream_identical` misses fixed (after 14:14 the 54-tool swarm surface missed on 354 of 1,009 rounds; cause and fix in [Sibling cache affinity](#sibling-cache-affinity)).

Live A/B (2026-10-28, `claude-opus-5.5` as `kunlunbedrock.claude-opus-5.5-m17` on the Sankuai Anthropic face, `scripts/cache_tail_ttl_probe.py` through the repository request path; identical bodies re-sent after 402 s):

| arm | send | prompt | cache_read | cache_write 1h / 5m |
|---|---|---:|---:|---:|
| tail 1h | first | 59,208 | 0 | 59,204 / 0 |
| tail 5m (control) | first | 59,208 | 0 | 10,710 / 48,494 |
| tail 1h | after 6.7 min | 59,208 | 59,204 | 0 / 0 |
| tail 5m (control) | after 6.7 min | 59,208 | 10,710 | 0 / 48,494 |

The gateway accepts a 1h tail marker and serves it after the gap, while the control re-writes its body. In base-input units the 1h arm costs 124K against 144K with one long gap, and 118K against 82K without one.

### Sibling cache affinity

**Problem (2026-10-28):** a change merged that morning and live from the 14:04 worker restart moved each swarm worker's run identity into a `worker_team` carrier after the objective, so same-role siblings sent byte-identical tools+system prefixes and could share one system entry. Afterwards sub-agent rounds stopped reading back their own previous round. A round read the previous round's entry when `cache_read` equals that round's `cache_read + cache_write`; it read an *older* entry when the read equals an earlier round's total, which happens only when consecutive rounds are served by different upstream cache domains that each keep their own entries. Wire hashes and routing were identical on these rounds and gaps were far below the TTL, so they are neither byte drift nor expiry. Isolated-lane `[CacheRoundRecord]` rounds on `claude-opus-5.5`, excluding first rounds and `ttl_expiry`:

| window (worker process) | requesters | rounds | previous entry | older entry | tools+system only | none |
|---|---|---:|---:|---:|---:|---:|
| 10-27 22:18–10-28 03:46 | 11-tool siblings, per-sibling system | 1,295 | 98.6% | 0.1% | 0.3% | 1.0% |
| 10-28 03:46–14:03 | 11-tool siblings, per-sibling system | 2,212 | 99.0% | 0.0% | 0.1% | 0.9% |
| 10-28 14:04–21:24 | 11-tool siblings, shared system | 1,424 | 84.1% | 5.8% | 6.0% | 4.1% |
| 10-28 14:04–21:24 | 54-tool siblings, shared system | 1,433 | 49.7% | 21.6% | 23.4% | 5.2% |
| 10-28 14:04–21:24 | root conversations (own system) | 2,060 | 89.1% | 2.6% | 2.1% | 6.1% |

Between the windows before 05:20 and after 16:00 the comparable-round read share fell from 98.3% to 83.9% and `upstream_identical` rose from 0.8% to 13.4% of comparable rounds. The loss followed same-prefix concurrency: a 54-tool round read its previous entry 81% of the time with no same-prefix sibling in flight at request start, 54% with two and 30–35% with three to five; by hour it was 28–57% while six siblings ran (14–16h) and 88–100% once one to three remained (17–20h), while root conversations read 85–92% during 14–16h. The shared entry itself had no measured benefit: all 17 first calls after 14:04 read nothing, 16 because their launch wave started before any sibling had written it and the one later same-role sibling because its request landed where the entry was absent.
**Decision:** the run identity again ends the role part of the `worker_policy` system contract (`build_child_messages`), so every sibling sends its own system prefix, as before 14:04, when swarms of up to 18 such siblings read their previous entry on 98.6–99.0% of rounds. The `worker_team` injection ID stays registered only for checkpoints written in between; rebuild drops those carriers. Spec: `tests/context/test_context_injection_aux.py`.
**Live A/B and its limit:** concurrent synthetic siblings (4–6 per arm, arms interleaved) through the repository request path (`build_body` → `stream_chat`, 39–57K-token prompts; 284 requests including controls, about 12.7M base-input-equivalent tokens) read their own previous or older entries on 31 of 92 rounds with per-sibling bytes at the start of the system region against 17 of 92 with a shared system (Fisher two-sided p ≈ 0.03); a unique final system block after the shared ones read 6 of 35. The runs (21:51–22:26) fell into a gateway-wide degradation: single sequential conversations in the same window read their previous entry on only 8 of 25 rounds, and the production root lane read nothing on 56% of its rounds at 22:30, so the absolute levels say nothing about a healthy gateway. Re-measure with the read-source table of `scripts/cache_waste_report.py` (it reproduces the table above from the logs) once root conversations read their previous entry again; until then the production before/after comparison is the evidence.
**Alternatives rejected:** keeping the shared prefix (386 `upstream_identical` re-writes in seven hours for six 54-tool siblings at a mean prompt of ~400K tokens, against at most one ~12–21K-token system write per later-wave sibling); a unique final system block that keeps the shared entry (no better than the shared arm in the A/B, and it needs a composer ordering change); staggering launches or capping same-prefix concurrency (delays user work, and the latency policy above keeps holds opt-in); keep-alive or duplicate requests (owner decision 1); a mid-history stepping-stone breakpoint (it shortens lookback distance, but a different cache domain holds no entry to step on).

## Model continuation lifecycle

- Completed ordinary chat tasks retain their final bounded working context through `lib/storage/model_continuation.py`, not the disposable node-local replay cache.
- One latest value per owner/conversation/lane is capped at 8 MiB, 16,384 messages and 4,096 Turn witnesses; no TTL, resident payload index or new worker. Publication runs once after settlement.
- Sidecar ownership and frontier CAS prevent foreign/stale replacement; restore validates the complete historical prefix and policy. Deletion removes derived state.
- Edits, incompatible policy, missing media or unavailable storage trigger explicit cold-rebuild diagnostics. A valid durable checkpoint survives restart and temporary cache eviction.
- Route and effort switches (v2 ModelRef, preferred provider, routing, preset, thinking depth/enablement) reuse the verified continuation's portable text without old composer carriers or model-specific wire latches; provider reasoning, signatures and opaque native items are stripped. History-carrier and inbox private fields are portable text metadata; unknown private or multimodal state remains conservatively nonportable ([decision](CONTEXT_COMPACTION.md#continuation-checkpoints)).
- For Opus 4.7+ with thinking still enabled, an additional fixed-size policy witness permits depth-only changes to retain the full signed working view and tool latch (`restored_effort`). On that effort witness, model, v2 ModelRef/provider preference/routing intent, policy, permissions, sources and on/off changes remain guarded. Old checkpoints without that witness may cold-rebuild once. This checks requested policy, not actual provider/credential affinity; it does not promise an upstream cache hit or newly certify arbitrary gateway/failover replay. The official thinking signature format is portable across Claude API/Bedrock/Google Cloud, so a raw-key equality rule would not be an API-derived requirement.
- Deferred input translation re-pins the history proof. **Problem:** the command pins the proof before the worker records the input's translation verdict, a model-visible content write to the input Turn, so every translated turn either refused publication (`authority_changed`) or stored a proof the next restore rejected (`history_changed`); translated conversations never replayed and re-sent all injected context each turn. **Decision:** `restore_prefix` also pins a fingerprint of the proven suffix headers without the input's revision; after its own verdict the worker (`model_replay.rebind_history_proof`, called from `spawn.py`) re-reads the context and adopts the fresh proof only when that fingerprint, the settled input revision and the model view's input text all match. Any other change keeps the pinned proof and its refusal. **Alternatives rejected:** counting the verdict as a display-only revision (it changes model-visible content, so the proof must change); rebuilding the model view after the verdict (re-derives ingress policy witnesses from a runtime-mutated config); translating in the command (restores the acceptance latency removed in `docs/modules/ingest_media.md`). Specs: `tests/model_routing/test_model_context_read.py` (both authorities) and `tests/conversation/test_deferred_input_translation.py`.
- Full transcript authority is unchanged; no persistence failure rolls back a settled user turn.
- `tests/model_routing/test_model_continuation_store.py` covers real-Sidecar restart, owner/lane isolation, stale publication, bounds and deletion; `tests/model_routing/test_model_replay_stability.py` and `tests/model_routing/test_model_continuation_policy.py` cover prefix parity and cross-model compatibility.
- The old bounded `ModelReplayCache` remains a disposable repository utility/test fixture, not the production continuation lifecycle.

## Cache diagnostic ownership

Problem: unused warm-prefix aggregation and same-model activity counters still
looked like live compaction or cache-miss policy. Decision: cache consumers use
`cache_tracking._state.get_prev_turn_cache_read`, which freezes the completed
same-owner, same-lane predecessor at invocation entry. Active sibling reads do
not replace it; prefix protection stays in `_prefix`, and `_ttl` owns only the
session-stable TTL latch. The unused aggregators and their isolated tests are
removed. Alternatives rejected: reusing the largest recent read would replace
an actual cold predecessor with older warm history; counting concurrent callers
cannot establish provider cache contention. Verify cache accounting lifecycle,
prefix protection, TTL-latch and miss-attribution tests.

## Expected interpretation

Prompt caching reduces a matching prefix's unit price; it does not make growth free. Progressive schemas reduce the fixed floor while working-set policy bounds history. Cache misses from eviction, rerouting or real prefix changes remain distinguishable through request-wire and cache-write evidence. Native result-surface policies compact repeated launch input, declared artifact defaults and gateway transport presentation only after canonical capture and before unchanged budgets; original decoded/program values and frontend task objectives remain intact. Unknown application fields, failures, signed cursors and media survive; no generic lossy truncation or cache is added. `benchmarks/tool_result_projection.py` compares exact UTF-8 view sizes and declared repository-counter estimates offline, not provider invoices or full-agent quality. Source-read budgets/ranges are unchanged: wrapper savings do not solve repeated wide reads.

Partial text previews: **Problem:** ordinary truncated tool summaries still paid
JSON escaping costs for code, quoted records and multiline output. **Decision:**
`partialProjectionText` in `tool_result_surfaces_v1.json` selects the shared
length-labelled Unicode framing for the summary when the complete view uses fewer
UTF-8 bytes. Existing budgets still measure the delivered model view. Body text
stays opaque; items, exact integers, source references, continuation positions,
recovery advice and program-read metadata retain their values. Canonical capture,
decoded program input and stored source bytes remain unchanged. Observation and
persisted launch-receipt readers use the common decoder, including exact run
identity matching. **Alternatives rejected:** stripping recovery metadata loses
access to omitted evidence; increasing budgets preserves escaping overhead;
parsing framed body text as application JSON invents authority.

Seven fixed previews preserved every body and metadata value while exact
`o200k_base` counts fell from 7,299 to 6,532 tokens (10.5%). Plain paragraphs,
short quotes and non-display control-character samples stayed byte-identical.
This measures preview formatting, not provider invoices, latency or whole-task
savings; byte-based selection does not guarantee fewer tokens for every string.
The shared formatter uses transient strings under existing result bounds, with
no new cache, worker or persisted copy. Verify partial-text projection/backends,
program snapshots, aggregate budgets, restart/ownership recovery, launch snapshots
and exact OpenAI/Responses/Anthropic request text.

## Safe A/B channel

Settings → Advanced exposes **Cost Optimization A/B Experiment**, off by default: unchanged request config, no task metadata. First enablement uses a 10% conversation canary, evenly split between arms; the other 90% keeps its configuration.

| Arm | MCP exposure | Automatic L2 timing |
|---|---|---|
| `control` | `inline` | Shared route-window safety |
| `optimized` | `auto` | Shared route-window safety |

Only enrollment, optimized share, experiment ID and minimum sample are editable;
policies are not arbitrary JSON. SHA-256 bucketing over experiment + conversation
ID keeps conversations in one arm. Requests explicitly setting `mcpToolExposure`
are excluded. The context-cost plugin is version 2.0.0; prior resolved specs keep
their version/digest and require a new experiment ID, preventing old fixed-target
results from being silently reused as new exposure. Saves use the atomic server-config
writer and `server_config_change` audit event.

An experiment ID locks enrollment and arm percentages; changing either requires a new ID to prevent silent rebucketing. Enablement and report sample gates remain editable without changing assignment.

Every assigned assistant turn persists:

- provider-reported prompt/output/cache token counts;
- the all-in cost snapshot after compaction-model usage has been folded into
  the turn total;
- pricing provenance (`provider_override`, `model_table`, `qwen_tier`, or an
  estimate label), latency, API rounds, actual compaction mutations and the
  terminal-without-error operational proxy;
- the experiment/arm tag on the assistant message and API-round records.

The 14-day report samples by **conversation**, not by turn. It shows both the
conversation and turn counts, cost per fully priced conversation (the primary
metric), cost per priced turn (diagnostic), pricing coverage, prompt tokens,
cache-read ratio, latency, error-free-terminal rate, model/provider mix and
compactions. The readiness gate also requires the configured number of fully
priced conversations in each arm. Unknown models keep the legacy estimated amount in the normal
cost UI, but `default_estimate` is counted as **unpriced** in the experiment —
never as zero and never as a real-cost observation. The error-free rate is not
a semantic-quality score; evaluate task completeness/evidence separately
before promoting the optimized policy.

Rollback is one switch: disable the experiment. Existing tagged history stays
available for analysis, while all new requests immediately return to their
ordinary configuration. Per-request explicit overrides remain an additional escape hatch.

## Open-source harness comparison

The production policy borrows the convergent parts of three current harnesses,
without copying their implementation blindly:

| Harness | Current compaction shape | Tofu alignment |
|---|---|---|
| [OpenAI Codex](https://github.com/openai/codex/blob/main/codex-rs/core/src/compact.rs) | Builds a local summary, retains recent real user messages under a bounded budget, and reinjects initial context. It also records compaction telemetry and warns on repeated compaction. | Tofu retains stable system instructions, keeps a hot tail, archives boundaries and records experiment outcomes. |
| [OpenCode](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/compaction.ts) | Prunes old tool results before full summarization, protects a recent token/turn tail, then uses a separate compaction agent/model and folds the prior summary forward. | Tofu L1 performs zero-cost old-tool-result reduction; L2 uses a cheap summary model and preserves a bounded recent working set. |
| [Aider](https://github.com/Aider-AI/aider/blob/main/aider/history.py) | Keeps a recent tail, summarizes the older head, and recursively re-summarizes if the result still exceeds the budget; it can use a weaker model for the summary. | Tofu uses the same old-head/recent-tail split and folds summary-model usage into the all-in cost instead of hiding it. |

The initial experiment intentionally compares the compatibility baseline with
the shipped economic policy. It does not simultaneously vary summary prompts,
tail algorithms or models: changing multiple mechanisms at once would make a
cost delta impossible to attribute. Those can become later experiment IDs once
this channel has enough coverage and a semantic-quality evaluator.

### Upstream code-orchestration comparison (2026-08-10)

- [OpenCode CodeMode](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/tool/code-mode.ts), at the reviewed 2026-08-10 revision, is analogous local code orchestration rather than OpenAI Responses PTC. Its experimental `execute` tool derives MCP callables from visibility, rechecks permission/hooks per child, preserves derived IDs, lifecycle, structured results, attachments and cancellation, and bounds live child concurrency at eight.
  Its runtime supports timeout/call/output budgets, but the reviewed host supplies none. Tofu retains explicit authority, lifecycle and UTF-8 budgets. Hosted PTC excludes writes, approvals, citations and native artifacts; local JavaScript delegates every catalog child through ordinary contract/authority/approval checks.
- [Pi's agent loop](https://github.com/earendil-works/pi/blob/main/packages/agent/src/agent-loop.ts)
  contains no `programmatic_tool_calling`, `allowed_callers`, `caller_id`, or
  `program_output` protocol path at the reviewed revision. It executes ordinary
  assistant tool calls either sequentially or in parallel, lets a tool declare
  `executionMode="sequential"`, preflights calls in order, runs the prepared
  parallel batch with `Promise.all`, and emits result messages in original call
  order. Tofu already preserves those useful scheduling/ordering properties,
  but adds a first-class program parent, explicit eligibility and per-program
  safety/evaluation records.

## Context-efficiency controls

**Static-context decision (2026-11-07).** Problem: the full prompt repeats
comment-writing, parallel-call and output-style guidance, while every large
local tool surface preloads a result-recovery tool whose normal completion
updates already contain the result. Decision: consolidate those prose rules
without changing block IDs, safety/authority rules or profile selection, and
defer `get_agent_result` through the existing registry/search/execution path.
The direct `spawn_agents` description points to search when recovery is needed;
launch and background controls remain direct. Small catalogs and Tool Search
`off` retain the recovery schema. An explicit named tool choice restores only
the current authorized schema through the shared direct-wire mechanism.

Offline measurement against the captured 20-tool TB surface at
`3cab0434d126b3788821b7def69e9ef3a34eeb93`, using canonical swarm schemas,
`tool_schema_tokens` for sorted compact JSON and `count_text` for the joined
`build_static_blocks` output (`glm-5.3-flash`, code context, Tool Search present,
environment/date omitted):

| Component | Before | After |
| --- | ---: | ---: |
| Direct tool schemas | 7,139 | 6,760 |
| Platform static prompt | 3,412 | 3,147 |
| Combined local token estimate | 10,551 | 9,907 |

This is a 644-token (6.1%) reduction in those two components, not provider
billed usage, total conversation context, or a quality/latency result. The
recovery path can add a discovery round when used. Production routing and the
TB CLI are distinct: production defaults to `tools.nativeExposure=routed`,
whereas the CLI defaults to `full` for reproducible evaluation exposure; both
still apply declared-searchable deferral through local Tool Search.

Alternatives: deferring the launch tool conflicts with the current direct
multi-agent policy; hiding file/lifecycle tools adds discovery to routine work;
switching GLM to the lean prompt is a behavioral experiment requiring paired
task-quality evidence. Existing deferred specialist families already cover
slides, image generation and browser operations. No prompt nonce, context-size
cutoff or new tuning setting is introduced, and these savings do not establish
a gateway routing boundary. Specs: `tests/tools/test_tool_registry.py`,
`tests/context/test_full_prompt_integrity.py`,
`tests/tasks_pkg/test_prompt_profile_adoption.py`, and the named-tool-choice
contract in `tests/tasks_pkg/test_unified_tool_gateway.py`.

Request-local switches support single-factor ablation without process-global
state. Every tool-capable request defers the declared-searchable residual
catalog behind the local Tool Search gateway; eager tools stay direct:

```json
{
  "cache": {"gpt56BreakpointMode": "explicit"},
  "tools": {
    "nativeExposure": "routed",
    "programmaticCalling": "on",
    "programmaticExposure": "additive",
    "toolSearch": "local"
  },
  "responses": {
    "transport": "sse",
    "reasoningMode": "standard",
    "verbosity": "medium",
    "imageDetail": "auto",
    "promptProfile": "auto"
  },
  "orchestration": {
    "multiAgent": "full"
  },
  "compaction": {"evidenceLedger": false}
}
```

- `cache.gpt56BreakpointMode=explicit` adds the GPT-5.6 public Responses API
  `prompt_cache_options.mode=explicit` control. The stable developer-prefix
  breakpoint remains; the dynamic implicit breakpoint is disabled. The
  default `implicit` value preserves the prior stable-explicit plus dynamic-
  implicit behavior.
- `tools.nativeExposure=routed` conservatively selects native tool families
  from the latest task while retaining read/inspection, MCP discovery and
  custom/plugin safety surfaces. The selection narrows eager families only;
  searchable families stay candidates that every Tool Search backend defers
  and search-off sends direct. `full` is unchanged. The report records both
  exposed tool count and exact schema-token cost so routing is judged on token
  reduction and discovery recall, not catalog size alone. The routed set is
  union-sticky per owner+conversation: a family any earlier turn exposed stays
  on the wire for the conversation's life (canonical assembly order unchanged),
  so the tools array never shrinks or reshuffles at an ordinary turn boundary —
  measured live, a per-turn flip re-billed the whole cached prefix at the tools
  region for a ~1–2k-token schema saving. Turn-1 routing keeps its cold-start
  saving; explicit allow/denylists still apply after assembly, and the complete
  executable catalog is preserved before routing, so the union neither grants
  nor retracts authority. With discovery disabled, the full catalog stays direct;
  a provider's external tool-name cache limit can therefore make reuse
  impossible. Capability is preserved instead of silently hiding those tools.
  Local admission measures the initial projected local surface, and native
  admission measures its actual projected surface, not the deferred catalog.
- `tools.toolSearch=local` never removes tools from the task authority catalog.
  Eager tools, explicit caller tools, the current task's MCP active set and a
  forced `tool_choice` remain direct; searchable residual tools are reached
  through the local `search_tools`/`execute_tools` gateway on every provider.
  Provider-hosted Tool Search is not used, so the wire never depends on a
  route's hosted-search support; `off` sends every schema directly. Each model turn assembles its
  wire catalog and discovery metadata from current settings; provider
  conversion uses that request-local projection.
- L2 always uses the source-derived continuity packet in
  [CONTEXT_COMPACTION.md](CONTEXT_COMPACTION.md), independently of model prose.
  Selected source observations use the existing request budget without a
  newest-N prefilter; only a packet that cannot fit beside the smallest tail
  folds its oldest observations into the narrative. The exact checklist and folded execution observations are
  appended by the runtime and digest-verified on reuse; receipt retention is
  computed from that accepted packet, not substring matches in model output.
- `compaction.evidenceLedger=true` additionally persists exact L1 cold tool
  results and adds their recovery handles to that ledger. It remains opt-in
  because durable raw-result storage has a different privacy/I/O trade-off.
- `tools.programmaticCalling=on|off` defaults to `on` (a retired `auto` reads as `on`). Verified provider features plus reviewed eligibility select native OpenAI PTC; other tool-capable routes keep direct tools and add local async JavaScript (`execute_program`) over the complete task-executable catalog; `off` disables programs. [The gateway contract](contracts/TOOL_SEARCH_EXECUTION_GATEWAY.md) owns interface selection, eligibility, approval, runtime bounds, replay and recovery; local execution never gains hosted-only authority.
  Local projection is additive and cache-stable: active modes expose the resident program tier whenever executable tools exist, independent of request wording or observed fan-out, and repeated authoring failures move one task to JSON batches. `TOFU_PTC_TIER=program|batch` is an explicit evaluation/operator override, never a model-size heuristic. Native-to-local fallback uses the local program tier. Productive single-read chains neither force adoption nor change the wire schema; historical `serial_gateway` settings remain readable only for explicit experiments. Actual completed `programRuns` and backend/evidence identity establish adoption; byte counts and `expectedSavings.basis=program_surface_unmeasured` do not establish model cost or latency gains.
  Historical production calibration: 93 calls, 107 tools, 7.149M prompt tokens (6.945M cache reads), ¥23.05, 2,286.3 seconds; 45 gateway offers and zero program runs. Five serial-read chains covered 24 model rounds; 19-call and nine-call counterfactual opportunity ceilings are theoretical, not savings. The [paired public-SDK pilot](../evaluations/programmatic_pilot/README.md) measures optional adoption, complete-evidence answer quality, actual usage and latency before promotion.
- `responses.promptProfile=auto` selects compact GPT-5.6 but keeps Kimi on full.
  Explicit Kimi lean/ablation arms emit applied profile, SHA-256 and token proof;
  the benchmark rejects missing/mismatched adoption before any promotion.
- `orchestration.multiAgent=full` (shipped default) keeps `spawn_agents` and the parallel-execution guidance on the wire every round, and workers inherit the parent's full authority catalog minus the leaf-worker denylist (no nested orchestration, no blocking on the human). `read_only` is the API-only analysis lane; the retired `auto` and `off` values read as `full`, and there is no concurrency setting because accepted workers start on demand. The provider-neutral policy in `lib/tasks_pkg/tool_orchestration_policy.py` selects this control plane and the programmatic data plane independently. Both may therefore be active: agents partition independent workstreams, while the root and each eligible local worker use PTC to reduce repeated reads inside its own workstream. `lib/swarm/routing.py` selects native OpenAI acceleration only for a verified public GPT-5.6 Responses face carrying a `read_only` decision — a `full` lane never routes to the analysis-only native extension and otherwise projects the existing local `spawn_agents` runtime for every model whose task authority includes it. Read-only workers (native or local) share the read-only dispatch filter: the shared execution boundary rejects file, shell, artifact, scheduler, project-state, integration, and other non-root mutations even if prompt guidance is ignored. Full-authority local workers keep writes and run under the ordinary per-tool approval/authority gates. Streaming and non-streaming surfaces expose only `/root`'s `final_answer`. Benchmark arms remain single-factor experiments for causal attribution; that experimental isolation is not a production mutual-exclusion rule.
- Each terminal task result keeps bounded reason, `expectedSavings`, and
  `projectionEvidence` per routing decision. Provider wire projection is never
  adoption: only a real program run, native multi-agent call, or launched local
  agent wave enters `adoptionEvidence`; `adoptionStatus` is re-derived from
  those trajectories whenever the task is persisted or benchmarked.
- `responses.reasoningMode=pro` keeps the selected GPT-5.6 model and adds
  `reasoning.mode=pro`; it never invents a `gpt-5.6-pro` slug. Reasoning effort
  stays independent and legacy `ultra` is normalized to official `max`.

The terminal outcome now stores `oraclePassed`/`oracleType` separately from
`terminalWithoutError`. The latter is only a health signal. It also stores the
dataset/task/agent/model/effort/arm identity, actual and frozen-public-price
costs, uncached/cache-read/cache-write/output/reasoning tokens, per-round
prefix/schema/tool-result shape and fingerprint, per-round API usage/latency,
compaction reason and evidence retention, plus MCP searches/misses. Historical
assistant rows remain valid because every new field is optional.

### Live long-task decision record — 2026-09-01

Four real SWE-bench Multilingual issue prompts were run against the same
frozen Tofu source and `kimi-k3` provider route. The controlled effort pair
changed only `thinkingDepth`: control used `xhigh` (Kimi `max`) and candidate
used `medium` (Kimi `high`). Costs below reprice provider-reported cache/input/
output usage with the manifest's frozen Kimi card.

| Real issue prompt | Official oracle | Cost, max → high | Model rounds | Model-phase latency |
|---|---:|---:|---:|---:|
| Carbon 3098, high-precision interval seconds (PHP) | pass → pass | $0.6021 → $0.3134 (-48.0%) | 31 → 24 | -16.6% |
| bat 562, filenames matching the `cache` prefix (Rust) | pass → pass | $0.4098 → $0.4117 (+0.5%) | 19 → 22 | +34.4% |
| nlohmann/json 4237, unsigned enum serialization (C++) | pass → pass | $0.2275 → $0.1553 (-31.7%) | 19 → 16 | -31.7% |

The valid aggregate preserves quality at 3/3 for both arms, reduces normalized
cost from $1.2394 to $0.8803 (-29.0%), and reduces model rounds from 69 to 62
(-10.1%). It does **not** show an aggregate latency win: 2,796.8s became
2,934.1s (+4.9%), dominated by bat's build-time variance. The bat result is
retained specifically to prevent a cherry-picked claim.

Lucene 12022 (flat-polygon `CONTAINS`, Java) produced a patch in both arms, but
the official grader tried to download `gradle-wrapper.jar` inside the
network-isolated environment and hit `UnknownHostException` before grading any
test. It is infrastructure-invalid and excluded from quality/cost aggregates;
the evaluator now classifies that narrow bootstrap signature accordingly.
Historical JSONL remains append-only.

The separately tested one-shot serial gateway also failed its economic gate on
Carbon: both arms passed, while cost rose from $0.7543 to $1.0605 (+40.6%) and
model-phase latency from 925.5s to 1,312.2s (+41.8%). The model invoked the
gateway but did not batch useful child reads. Consequently additive exposure
is the default and `serial_gateway` remains opt-in.

Decision: retain the existing product default `defaultThinkingDepth=medium`
(Kimi `high`) for unspecified effort, preserve explicit user-selected
`xhigh`/`max`, and do not promote serial gateway. This calibration demonstrates
quality-preserving cost/round reduction, not population-level quality uplift;
larger pilot and confirmation gates remain mandatory for a broader policy.

## Reproducible benchmark contract

`lib.benchmark_contract` defines an append-only `tofu-benchmark/v1` JSONL
format: one manifest followed by task records. A manifest freezes the task
list, environment snapshot, agent/model/effort/arm, timeout, network policy,
agent constraint, retry rule and budget. The Multi-agent arm records
`singleAgent=false`; all other arms remain single-agent. A task record carries the final
patch, oracle/tests, per-round usage, prefix fingerprints, costs, context and
compaction telemetry, artifacts and infrastructure error.

Only failures explicitly classified as `infrastructure` may be retried, up to
the manifest's predeclared limit; agent failures are final. The budget helper
pauses new tasks at $1,200 for a reforecast and stops at the $1,500 hard cap.
It also computes cost from a frozen public price card so subscription runs can
report throughput/limit consumption and a comparable shadow cost without
pretending the monthly subscription is a per-task API invoice.

Exact cost evidence reads provider counters only through `lib.cost`.
Problem: the Responses branch of `canonical_benchmark_usage` read
`input_tokens_details.cached_tokens` itself. Pricing re-read cache keys from
the canonical dictionary. Both reads bypassed the alias SSOT that
`tests/meta/test_usage_cache_ssot_guard.py` protects. Decision: a declared
Responses usage is respelled as the cache-inclusive OpenAI shape
(`input_tokens` -> `prompt_tokens`, `input/output_tokens_details` ->
`prompt/completion_tokens_details`). `validate_reported_usage_counters`,
`normalize_usage` and `split_input_tokens` then own every alias read and
rejection, and pricing reads `normalize_usage(canonical)`. Alternatives
rejected: allowlisting the module in the guard exempts every future raw read.
The Responses SSE translator clamps malformed counters to zero, but benchmark
evidence must reject them. Verify
`tests/benchmark_contract/test_benchmark_usage_evidence.py` and the SSOT guard.

Confirmation uses paired oracle results. A candidate with no fewer resolved
tasks but a negative one-sided 95% lower bound is reported as an observed tie
without a population-level non-inferiority claim. Cost per successful task is
considered only after the quality gate, and release additionally limits p90
latency regression to 20%. Rollout remains an operational 5% → 25% → 100%
decision; every arm can be disabled independently with the request switches.

Before a paid PTC arm, `python scripts/ptc_live_smoke.py --dry-run` validates
the request fixture. With an explicitly configured `OPENAI_API_KEY`, the same
script performs a deterministic read-only live check of program/caller/output/
final-message replay. No live check runs implicitly.

## Long-agent Codex parity contract (v2)

### Quality-first release policy (owner decision 2026-10-26)

Problem: a mandatory cost/success reduction could reject a candidate with
verified quality gains solely because it spent more. The owner explicitly
prioritizes making Tofu the leading agent harness without a cost constraint.

Decision: `tofu-long-agent-quality-first/v1` makes monetary cost diagnostic,
never a release veto or a substitute for quality. The existing comparison
matrix, paired quality/family gates, blind judgments, critical-incident gate,
latency gate, infrastructure evidence and actual orchestration checks remain.
Per-call usage, failed calls, compaction and paid tools still reconcile against
the frozen price card. Report contract v2 explicitly names this policy, so a
new assessment cannot be confused with the former cost-gated report. Earlier
immutable evidence and historical v1 experiment decisions are not rewritten.

Alternatives rejected: merely increasing a cost ceiling still vetoes quality;
dropping cost evidence hides failed attempts and breaks reproducibility;
loosening quality or safety to make expensive candidates pass defeats the goal.
Verify `tests/benchmark_contract/test_quality_first_release.py` and the paired
release-report/store contracts. A policy change is not evidence of a leaderboard
win; a complete, reproducible comparison is still required.

The 2026-08-24 comparison keeps the model fixed: Tofu and Codex CLI 0.149.1
both call the same YourProvider `kimi-k3` slot with the same thinking setting. The
isolated loopback-only `evaluations/codex_kimi_proxy` translates one Codex
Responses request into exactly one Kimi Chat Completions request. It preserves
instructions/messages, strict function schemas, tool choice, thinking, usage,
SSE order, errors, and cancellation. It never joins production routing, reads
user configuration, or logs a key. `/responses/compact` invalidates the trial.
Codex uses local compaction and its pinned 0.149.1 fallback: 272,000 context tokens and a 244,800 compact limit;
metrics separate raw wall, proxy CPU, translation CPU and Codex-favored wall.
Namespace functions flatten; native `web_search` is suppressed and recorded;
unknown native types fail closed. A real CLI smoke proves one Kimi call.
Proxy shutdown joins accepted request handlers before closing their shared HTTP
client, so both global and per-trial metrics are durable when close returns.
Daemon handlers were rejected because a completed response could outlive teardown
and leave evaluation evidence incomplete. Verify
`tests/evaluations/test_codex_kimi_proxy_lifecycle.py` and the loopback proxy
contract in `tests/tasks_pkg/test_long_agent_v2_contracts.py`.
The formal Harbor `codex-kimi` profile now owns the proxy for the whole
start/resume lifecycle, uploads and re-hashes the pinned CLI inside each
disposable guest, removes the Kimi URL/key variables from Harbor's child
environment, and retains tagged raw JSONL plus per-call provider usage. QEMU
exposes only `10.0.2.101:<fixed guest port>` through a private Unix relay whose
host destination is predeclared; the real host port never appears in QEMU's
arguments. Audit replays every completed trial through the raw/proxy projector.
The runtime freezes provider/Harbor/runner identities and preclaims release tasks
before dispatch; export binds raw/proxy/ATIF evidence and full retry wall/cost.
Dirty runners, hidden internal retries, dropped failure attempts, and identity
drift fail closed; launch failures never become completions.

The paired Harbor `tofu-kimi` profile runs the public production `AgentRuntime` with host-only credentials and only two exclusive guest client tools. Native
events, sanitized runtime/tool evidence, and ATIF-v1.7 are reconciled by current audit and `export-tofu-harbor`, including every model/compaction round, prompt,
schema, tool call/result, usage/cache/timing, final output, and verifier lifecycle. Frozen runtime/prompt/tool/provider/slot/thinking/arm/revision digests bind
the trial; candidate latency is raw task-start-to-oracle-ready wall with no proxy correction, and failed attempts, compactions, and paid tools remain charged.

`tofu-benchmark/v2` remains read-compatible with v1 and freezes pair/role/arm,
harness and agent hash, Kimi face/slot/thinking, prompt/tool-schema SHA-256,
permissions, sandbox/network, timeout, retry rule, three artifact byte limits,
dataset snapshot, price card, and task table. Its formal matrix is 1,845 trials:
SWE-bench Verified 500, Terminal-Bench 2.1 89×5, and frozen integrated-tool,
continuity, research, writing, and fault sets of 200/200/200/200/100. Each task
retains per-call usage/cache, queue/TTFT/model/tool/oracle-ready timing, context,
schemas/results, compactions, call graph, retries, oracle, incidents, judges,
and content-addressed raw trajectory. Failed/compaction calls and paid tools
count toward agent cost; the recorder reprices evidence with the frozen card.
Simulator and judge calls remain separate. Kimi costs are input $2.76/M,
output $13.81/M, with cache reads at 0.1× input.

The release decision is deliberately conjunctive: paired quality must meet the
Codex point/lower-bound gates, every family remain within -5pp, both blind
judges pass, critical incidents remain zero, P90 oracle-ready remains at most
85% of Codex, and infrastructure failures stay preregistered. Cost/success is
reported with no acceptance threshold under the quality-first policy above.
The full task records must also contain real program and agent trajectories
with zero projection-as-adoption claims. A failed gate yields `not
demonstrated`; no family or unused orchestration lane is hidden by an average.
Immutable `pair-report` re-audits finalized stores and their attempt ledgers,
derives all gates, and keeps pilots at `releaseEligible=false`.

The request-local experiment plugin exposes fixed, independently reversible
arms: `prompt_lean_kimi` plus five prompt ablations, `tool_surface_v2`,
`tool_result_v2`, context budgets 64k/96k/128k, `adaptive_compaction_v2`, and
`orchestration_v2`. `combined_v2` cannot be created through the maintained
helper until all six mechanisms are independently registered as winners.
Enrollment defaults to a 10% pilot; full paired evaluation and the operational
5% → 25% → 100% rollout remain separate gates.

The v2 runtime contracts are:

- `ContextPlanV2` and rebuildable `TaskStateSnapshotV1`, with locked required
  blocks, one deterministic global budget, explicit suppression/hash/token/
  recovery evidence, and cache epochs that advance only on semantic changes;
- `ToolContractV2`/`ToolResultEnvelopeV2`: every model defaults uncapped; an explicit
  neutral local budget preserves the code core and the never-compacted gateway pair, with 8,000/24,000-token result/round targets and owner-scoped artifact cursors. The envelope is internal evidence; provider messages receive a sparse semantic projection while a bounded non-model sidecar preserves harness and loop-guard facts;
- four explainable orchestration shapes—direct, bounded PTC reduction,
  independent read-only agents, or a verified loop—and a no-progress ledger
  that cannot infer completion from non-empty prose;
- adaptive compaction based on projected cache-adjusted savings, remaining
  rounds, call cost, and evidence loss, while window safety always wins and a
  summary that drops pending work or evidence is rejected.

`evaluations/long_agent_release` compiles locked SWE 500, TB 89×5, and five private packs into exactly 1,845 rows; drift or synthetic rows fail closed. Its run
store binds arm/task/oracle, reprices usage, verifies raw evidence, and finalizes only complete paired JSONL. Paired SWE/TB Codex and production-Tofu
launch/export paths are present, but the private 900 tasks, simulator launchers, and paid matrix remain absent, so no Codex-leading claim exists.
Root/endpoint share `run_agent_loop`; Paper remains 3,702 schema tokens and 29 executable tools (arms: 8,823/3,496).

Tool-authority preflight: `python -m evaluations.evolution.tool_authority --owner-id OWNER --model MODEL --output NEW_JSON`.
Problem: schema budgets can change executable authority, confounding surface-only comparisons. Decision: freeze schemas, authoritative contract hashes and arm policies; mismatches exit 2 without provider calls. Missing/degraded contracts refuse; prior outputs are preserved.
Alternatives: equal counts miss substituted tools, and policy-field checks miss derived registry differences. Historical controls remain intact; matched execution catalogs are required for schema-only attribution.
This offline artifact provides no quality or activation authority and is not wired into every live experiment caller.

## Public runtime usage accounting

`AgentExecution.evidence_snapshot()` exposes `usageAccounting` (`complete`, `observedTokens`, `scope`, `rootTaskId`) from the shared account. Model leases, tool attempts, children and recovered debt keep its atomic tree snapshot incomplete. Known unknown usage stays incomplete even without an account; an absent account is otherwise unobserved (`complete: null`). Reading evidence never creates or settles an account and does not certify task acceptance or coverage of uninstrumented calls.

Ordinary tasks bind `model_budget_admission` on their first model invocation even without execution limits. The existing account observes streaming, non-streaming and retries; omitted provider output caps stay omitted. Live unbounded work may retry after unknown usage, but its evidence remains incomplete. Finite authorization and unreconciled restored checkpoints retain their refusal rules.
Problem: counter-only projections lost uncertainty, and unbounded ordinary calls had no account. Decision: expose the shared account and remove the entry-point exemption. Alternatives rejected: infer completeness from counters, create accounts while reading evidence, or duplicate settlement with a second ledger/retry policy. Verify public usage-evidence and model-budget admission tests.

The account lives with the task and retains totals and active leases, not completed request history. A local synthetic measurement found about 4.5 KB per active account, 40 microseconds per reserve/dispatch/settle, and 32 retained bytes of growth across 10,000 further settlements; this is not provider or end-to-end latency evidence. See the measurement and verification receipt.

<!-- Evidence note: former docs/evidence/ artifact snapshots cited here were retired from the tree; cited JSON artifacts remain recoverable from git history. -->
