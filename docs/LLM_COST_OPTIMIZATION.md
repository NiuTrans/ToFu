# LLM request cost and context policy

This note records the request-path cost audit and the defaults introduced on 2026-08-08. It is about repeated agent/tool rounds, not ordinary one-shot chat.

The 2026-08-14 GPT-5.6 P0/P1 rollout follows OpenAI's current builder guide: official providers use Responses, the Sol/Terra/Luna family and exact current limits/prices; Pro is a reasoning mode rather than a model slug; lean prompts, PTC and Multi-agent are task-gated and represented as independent benchmark arms rather than assumed wins.

## Measured baseline

A representative six-round Kimi task submitted 1,160,102 prompt tokens and 4,839 output tokens. Its per-round prompts grew from 185K to 194K tokens. Cache reuse was generally high, but one byte-stable round still returned zero cached tokens, so automatic provider caching cannot be treated as a cost bound.

The first-round snapshot contained 279 messages and 240 MCP schemas; schema JSON
alone was 223,534 bytes (about 55K tokens), roughly one third of the request.
Longer production tasks accumulated 5–25 million prompt tokens in tool loops.

A 2026-09-01 trace demonstrated why total input/output alone cannot explain price: adjacent rounds processed 56,412/158 and 57,143/163 tokens, but cache reads were 10,752 (19%) versus 55,680 (97%), so displayed cost differed by 5.95× (¥1.7261 versus ¥0.2899) under the same unit prices. In that trace, 25 of 34 comparable working-set rewrites were followed by another rewrite before their projected task-age payback. Cost policy and UI must therefore retain the uncached/cache-read split and reason about prefix lifetime, not total tokens or total task age.

## Policy

### 1. MCP schema exposure

Default `mcpToolExposure=auto` preselects at most `mcpActiveToolLimit` (default
8) native MCP schemas instead of shipping the full catalog. Auto state is scoped
by owner plus conversation; low-signal turns reuse the prior surface and previous MCP calls before deterministic fallback.
Search cards are ranked/paginated views, not proof of wire absence. The full
catalog remains Tool Search/dispatch authority; `inline` exposes it all and
`progressive` retains the legacy read/write wrappers (~1.7 KB).

Controls (request-config keys; no `TOFU_*` environment variable):

```text
request config: mcpToolExposure=auto|inline|progressive
request config: mcpActiveToolLimit=8
```

### 2. Economic working set

Window safety and economic efficiency are separate constraints. The existing
90%-of-usable-window trigger prevents overflow, but on a 1M model it waits until
roughly 778K tokens. The effective automatic summary trigger is now:

```text
min(window_safety_threshold, working_set_tokens)
```

The fallback working set is 128K and includes live tool schemas. When the active provider/model rate card declares context tiers, the default instead uses 90% of the first boundary followed by a higher effective input, output, cache-write, or cache-read rate. For example, a 272K price boundary yields a 244,800-token working set. Flat, unknown, or cross-currency tiers retain 128K; an explicit request/environment value remains authoritative. On neutral/subscription routes the result controls local Layer-2 preservation; on verified public OpenAI/Anthropic routes it controls provider-rendered input while local L2 waits for the hard window/reactive fallback. Local L1, manual compaction, archives and receipts remain active.

A final fail-closed admission boundary remeasures the rendered request after inbox/tool injection. First dispatch targets 93.75% of the resolved working set and has an independent 256K host ceiling (the fallback remains exactly 120K/128K): one semantic compaction + recompose is allowed; when its summary call is unavailable, that attempt uses a bounded transcript-derived recovery receipt before refusing irreducible input. Usable generated text survives local receipt-shaping faults, and local compaction defects surface as internal errors rather than provider prompt overflow. Each API-round persists a content-free message/schema/total/target/method/action snapshot, separating admitted prompt size, cache reuse and billed internal compaction calls in the cost panel.
The accepted complete-input count is forwarded only through that round's call
stack to the completion-window clamp; absent or invalid evidence keeps the
standalone body builder's local estimate instead of reusing stale task state.

Controls:

```text
request config: compaction.workingSetTokens=128000
environment:    TOFU_WORKING_CONTEXT_TOKENS=128000
opt out:        set either value to 0
```

Fixed automatic L2 starts with a one-round ROI horizon, then predicts prefix lifetime from the shortest of the four most recent successful compaction gaps and the current window's already-observed age, capped at six and by the hard remaining API-round budget. Total task age no longer grants a longer horizon. Break-even resolves prices separately for the current and candidate prompt, compares only the observed warm prefix before/after, and records any pricing tier crossing. Manual, reactive and hard-window compaction still bypass the economic gate; adaptive policy retains its explicit expected-value horizon.

Default L1 tool-result retention is bounded by both 40 results and 48K estimated tokens. The newest complete tool-call batch always survives even if it alone exceeds the token budget; older results become durable placeholders outside the cache prefix. This slows working-set growth before an expensive L2 prefix rewrite instead of relying on repeated summaries as the first bound.

On 2026-08-27, nine distinct long tasks' earliest declines projected 1.18–3.89 rounds to repay; all ran past break-even, a rough 70.3M prompt-token exposure upper bound. This is not billed savings: cache discounts, prompt evolution and behavioral effects require actual receipts. Retry witnesses record their horizon; a longer observed compaction window or a cooler cache witness invalidates them and reruns exact economics.

The collapsed finish footer shows total input, uncached input, cache reads and output together. Top-level turn cost is the sum of per-API-round prices under each round's own model/provider/tier; mixed main, fallback and compaction calls are never flattened and repriced under the final model.

### 3. GPT-5.6 Responses caching and compaction

Only GPT-5.6-family Responses requests receive these OpenAI-specific fields;
generic Responses-compatible providers are unchanged. The ChatGPT Codex
subscription profile keeps the fields its internal backend supports and omits
the public-only explicit breakpoint and `context_management` request fields:

- `prompt_cache_key` is a stable SHA-256-derived conversation namespace; raw
  conversation/task identifiers never leave the process in this field.
- On the public Responses profile, the last text block in the stable developer prefix receives an explicit cache breakpoint. The Codex subscription profile
  uses its implicit cache plus the stable `prompt_cache_key` because its Luna
  backend rejects `prompt_cache_breakpoint`.
- Encrypted reasoning output items are saved and replayed with `store=false`,
  with `reasoning.context=all_turns`. The local counter sees the returned
  reasoning summary, not the encrypted payload. To close the latest-round lag,
  the usage cache carries provider-reported `reasoning_tokens` as a one-round
  replay reserve only when an encrypted reasoning item is actually appended.
- On the public Responses profile, `context_management` uses the same
  working-set threshold and is the primary L2 path. The Codex subscription
  profile keeps Tofu's local compaction path because its product backend does
  not accept the public request field. Returned opaque compaction items are
  persisted; input before the latest item is pruned while current system
  instructions are retained.
- Usage conversion preserves both `cached_tokens` and `cache_write_tokens`, and
  cache fingerprints inspect Responses `input` rather than an empty Chat-Completions `messages` field.
- The Codex subscription settle gate always protects cold cacheable requests
  and keeps the 4.2-second per-key send interval, but a warm round arms the
  additional five-second visibility window only after
  `input_tokens - cached_tokens` reaches 8,192. A 95,723-line production-log
  replay on 2026-08-27 matched 1,217 holds to the immediately preceding usage:
  this policy would remove 872 holds / 2,493.07 seconds (70.4% of matched wait)
  while retaining 345 holds / 1,046.18 seconds for cold or material tails. This
  is a counterfactual latency measurement, not a claim of deployed upstream
  cache-hit improvement. Local L2 summaries now use a content-free digest of validated owner + conversation + L2 stage in both dispatch affinity and the stream body, stabilizing their Codex session without overwriting the ordinary conversation's settle clock. A frozen six-hour window found 129 summaries, 43 summary-side holds/84.61 seconds, and 100 ordinary-round holds/218.61 seconds within 30 seconds after a summary; the 303.22-second sum is the matched interference opportunity removed by the namespace split, not a deployed end-to-end or unrelated-wait claim. Separately, reconstructible project summaries and non-terminal translation previews now use atomic immediate-only shared-contention admission: blocked work returns before transport without advancing the family probe clock, while every durable/default caller keeps the wait-and-probe contract. In a frozen six-hour response-log replay, 11/30 project-summary 429s and at least one first 429 across 27 incremental translation threads arrived within 30 seconds of an earlier same-family rejection; the 12 records bound an opportunity window rather than proving their unlogged selection instants or deployed savings. A later full-log replay found 117 shared-limit responses 30–120 seconds after the preceding family rejection and 157 further same-family responses within 15 seconds of those resets. The gate now decays at 30 seconds to one immediate-plus-serialized recovery seed and retires at 120 seconds (or two successes), retaining a bounded 256-family map without claiming the opportunity count as avoided traffic. Tool-result reuse remains latency-positive—2,084 of 2,093 speculative injections matched a same-task receipt within 60 seconds—but its old plain dict had no active-task ceiling and survived terminal work for the one-hour TaskRuntime TTL. In the same frozen window, 35 terminal snapshots retained 1,330 entries (median 4, p95 134, maximum 466); projecting the 8 GiB default cap of 128 onto those snapshots retains 745 instead, a 585-entry/44.0% terminal-residency opportunity, not a lifetime-peak or measured-RSS claim. The shipped FIFO keeps recent prefetch/dedup wins, safely re-executes an evicted receipt, and releases all survivors immediately after terminal settlement.

Proactive LLM/hybrid polls now build one status snapshot for both dispatch and audit, and read only the target conversation's final two messages. On the largest current frozen archive (1,163 messages), the per-poll conversation projection changed from two 13.04 MiB / 120.8 ms full reads to one 11.3 KiB / 12.3 ms tail read (-99.96% wire, -94.90% read time, -93.43% traced peak). Metadata-only and bounded-window Sidecar reads no longer select `search_text`; legacy JSON tails parse only their selected suffix with full-decode fallback. Explicit offline maintenance can now compress individually large frozen messages without hiding those JSON boundaries: the same 1,163-message sample stores 12,122,003 as 10,079,123 bytes (-16.85%), and its exact two-message tail remains on the fast path (1.042 versus 1.008 ms). Across all 4,544 current frozen rows, a read-only production encoding sweep validates zero invalid/oversize documents and projects 5,697,009,096 bytes to at most 2,526,642,300 (-55.65%, at least 3,170,366,796 bytes); the largest two-message row saves 70.79% but full decode costs 0.353 versus 0.154 seconds, an explicit CPU-for-storage tradeoff. Eleven additional project/scheduler/autopilot/turn-lifecycle settings, title, revision, and existence probes now declare `derive_messages=false`; on that archive each changes from 13.04 MiB / 120.8 ms / 178.35 MiB traced peak to 1.1 KiB / 0.18 ms / 0.017 MiB, without adding a cache. Legacy-autopilot close-out now resolves a live settings pin metadata-only, then uses a 128-message tail plus an exact full fallback for disarmed runs/anchors. The largest current pinned sample changes from 37.94 MiB / 258.6 ms / 560.8 MiB traced peak to 14.7 KiB / 0.42 ms / 0.21 MiB. A 128 KiB legacy JSON scan budget prevents large tails from trading allocation savings for seconds of Python work; on the largest disarmed-summary sample it restored 42.1 ms latency while still reducing response bytes from 8.15 to 4.44 MiB. Project-summary generation now reads exact first-eight/last-eight pages after its metadata freshness gate and takes a coherent full fallback on insufficient visible edges or an epoch mismatch. On the largest current 1,163-message project conversation, the selected prompt source remains byte-identical while projection bytes fall from 13.04 MiB to 181 KiB and traced peak from 178.35 to 13.51 MiB; uninstrumented direct-read median remains 46.08 versus 45.56 ms. Readable past-conversation references now request exact first-three/recent pages with a coherent fallback instead of hydrating discarded middles; the same largest sample keeps its selected messages while wire falls from 13.04 MiB to 400 KiB and direct-read median from 46.12 to 40.56 ms. Raw reference fitting now starts from 64 recent messages and falls back only when all candidates fit; the sample's exact 70,324-character result changes from 13.04 MiB to 428 KiB of read wire and 47.59 to 44.06 ms direct read-plus-fit median. Their structured human digest uses a revision-matched suffix probe plus exact anchored tail/head pages; its default selected rows stay identical while wire falls from 13.04 MiB to 642 KiB and direct median remains 41.13 versus 40.46 ms. Unsearched conversation listing now pushes the requested result bound into storage: on the current 4,786-row catalog, a 20-result read changes from 57.9 ms / 21.41 MiB traced peak / 1.66 MiB wire to 0.22 ms / 0.07 MiB / 7.9 KiB. Keyword listing now merges at most 200 body IDs with an authority-side exact-Unicode title scan that keeps only the deliverable rows. A common 20-title hit changes the old 10,000-row candidate read from 59.31 ms / 21.41 MiB / 1,698 KiB to 1.22 ms / 0.26 MiB / 7.05 KiB; a complete miss scans all lightweight title pages but still costs only 28.19 ms / 0.49 MiB and returns two bytes. Project Attention now reuses one Board snapshot and one deduplicated title-only owner query across Board/Charter items; on the largest current conversation a single provenance lookup falls from 59.97 MiB / 364.25 ms / 1,126.89 MiB traced peak to 0.36 KiB / 0.324 ms / 0.004 MiB, and an empty settings whitelist is constant-projected before JSON decoding. The collaboration summary passes its same-request Board and pending-proposal snapshots into Attention, eliminating one duplicate of each: on the current largest samples, 200 Feed events plus 34 Board tasks remove 155.52 KiB response material, 0.97 ms direct median, and 0.62 MiB traced temporary allocation per summary without retaining state. Influence now renders its prompt mirror from the same Board snapshot it classifies, and Status derives pending proposals plus the first 80 recent-block candidates from one 200-event Feed; the current largest samples remove another 78.15 KiB, 0.57 ms direct median, and 0.32 MiB traced temporary allocation across those reads. Team/Feed title backfill is now metadata-first for the 4,747/4,786 (99.19%) current conversations with usable titles, reducing the largest row from 59.97 MiB / 358.66 ms / 1,126.89 MiB traced peak to 0.36 KiB / 0.324 ms / 0.004 MiB. Untitled rows retain exact first-user fallback through a revision-matched eight-message head probe and full fallback on an unloaded first user or epoch change.
The Daily Optimizer's recent tool-distribution signal now selects metadata first, then lazily hydrates shared four-ID batches and recursively splits an oversized response; settings never cross either phase and any lazy-frame error discards the whole best-effort signal. The current latest-200 transcript/settings material is a modest 0.508 MiB, while its largest four-row transcript group is 0.219 MiB (-56.9% body-frame material); this is a frame-peak measurement and the extra bounded local round trips are intentional, not an end-to-end latency claim. Daily-report day extraction now adds a candidate `created_at < day_end` bound before exact message filtering and treats lazy hydration as all-or-nothing. On the frozen 2026-07-01 sample, candidates fall from 1,749 to 203 and hydrated message material from 3,697.59 MiB to 53.86 MiB (-98.54%); cloned conversations use their new creation epoch, so copied old turns do not manufacture historical work. Calendar and exact-day counts now send explicit local-time interval boundaries to `conversation.activity_dates`: Turn-native SQL returns only typed timestamp scalars, frozen archives decode in bounded four-row batches, and only distinct counts cross the RPC. The frozen July owner month changes at least 2,912.35 MiB of application-side message material across 1,336 candidates into a 127-byte result; its legacy-heavy Sidecar calculation remains 9.10 s / 255.22 MiB peak, so this is not a claim that archive parsing vanished. Daily-report digest extraction now combines statistics and transcript construction in one pass and retains at most 128 visible turns before its existing 800-character render. On the decoded 1,163-message sample, the exact 846-character output and 261 rounds stay fixed while pure processing changes from 1.371 to 1.250 ms median and traced temporary peak from 0.215 to 0.061 MiB; JSON decoding is outside this microbenchmark. Optimizer evidence gathering now reads each eligible bounded log tail once per run, reuses immutable request-local lines, and releases one log family before loading the next; distributed mode still reads only owner-filterable audit evidence. Post-apply metrics compute every tracked block domain plus total tool failures in one candidate-prefiltered application-log pass instead of two full tail scans per action. On the current 4 MiB application, 2.997 MiB audit, and 2 MiB error tails with ten tracked domains, equivalent pure application-log processing changes from 1,718.71 to 370.17 ms median (4.64x), while total logical tail material changes from 97.01 to 9.00 MiB (-90.72%) and traced peak remains 28.50 MiB; this excludes database collectors and is not an end-to-end optimizer-run claim. The audit and error snapshots now stream once into all event, switch, excerpt, and issue projections rather than decoding JSON three times and timestamps twice. On the current preloaded 9,803-line audit and 1,705-line error tails, byte-identical pure projection changes from 232.90 to 154.60 ms median (-33.62%, 1.51x) while additional traced peak remains about 0.03 MiB; snapshot loading and database collectors are outside this comparison. The main application-log projection now rejects signal-free lines before timestamp parsing and runs only marker-relevant regex groups. Lowercasing is capped at 8,192 characters; longer lines use allocation-free case-insensitive markers. On one exact 11,943-line snapshot, the projection remains byte-identical while changing from 348.76 to 54.18 ms median (-84.47%, 6.44x) with 0.106 MiB additional traced peak. The independent post-apply pass measured only 19.21 ms / a 5.33% maximum fusion opportunity, so it remains decoupled from action storage/writeback. The provider-usage token tier is now a launch-probed, content-bounded LRU with atomic expiration/replacement and pressure clearing; capacity or TTL misses safely continue to the existing local counter chain. In a 10,000 one-shot-conversation fixture, the old unbounded mapping retained 3.900 MiB while the 128-entry fallback retains 0.060 MiB (-98.47%, 0.066 MiB peak); full-capacity recording averaged 57.15 microseconds per provider completion. Login-wall admission now begins before the synchronous browser probe and persists through background polling, collapsing concurrent same-route probes and bounding ten-minute daemon work by owner/process. Its cooldown and live-session maps are now LRU/TTL bounded and pressure-clearable where reconstructible. In a 10,000-route fixture, combined retained heap changes from 6.005 to 0.189 MiB (-96.85%; 0.207 MiB peak); bounded construction costs 17.61 versus 12.24 ms total, about 0.54 microseconds extra per route. Working-tab affinity is now a launch-budgeted, 30-minute action-sliding LRU that is pressure-clearable and immediately forgets tabs proven closed. In a 10,000-route fixture, the old lifetime map retains 2.017 MiB while the 64-route lean bound retains 0.024 MiB (-98.81%; 0.029 MiB peak); bounded remember costs 17.35 versus 1.94 ms total, about 1.54 microseconds extra per route. MCP pre-request Tool Search now uses deterministic inverted postings plus launch-derived LRU indexes/sticky state; state retains query digests instead of longform text and every record path enforces capacity. Isolated old/new replay returned identical tool arrays while a 256-tool, 8,000-character query changed from 48.11 to 13.13 ms median (-72.71%, 3.66x) and a 20,000-word stress query from 971.40 to 40.74 ms (-95.81%, 23.84x). Thirty-two catalog revisions changed retained heap from 22.129 to 3.079 MiB (-86.09%, four-index lean bound); 1,024 maximum-size sticky queries changed from 8.299 to 0.596 MiB (-92.82%). These are pure local-search/residency measurements, not end-to-end LLM latency claims.
Stable bridge generations now reuse one ordered authoritative-reference tuple and one catalog fingerprint instead of sorting, copying, and hashing the same 256-tool projection every round. An output-identical 8,000-character synthetic replay changed the combined projection-plus-selection median from 5.712 to 4.464 ms (-21.86%, 1.28x), p95 from 6.563 to 5.160 ms, and 100-round traced peak from 0.972 to 0.618 MiB (-36.45%). Registry authority assembly now maintains one request-local name index rather than rebuilding it for every appended dynamic tool: with contract compilation held constant, 256 tools changed from 4.826 to 0.649 ms (-86.55%, 7.44x), and 1,024 tools from 67.026 to 2.654 ms (-96.04%, 25.25x). Request-owned ToolContract cloning now uses a memoized JSON fast path with `deepcopy` fallback for extension values: on the same 64-tool surface, registry assembly changed from 1.906 to 1.648 ms (-13.54%, 1.16x), and subsequent execution-document compilation from 1.306 to 0.944 ms (-27.71%, 1.38x). Resource-budget resolution now consumes a valid launch/operator value before computing its fallback, and Tool Search retains its one process-level term capacity; the same output-identical 256-tool pre-request build changed from 9.971 to 0.842 ms median (-91.56%, 11.84x) and p95 from 10.850 to 0.859 ms. Caching the private search-text mapping in that catalog generation then changed a same-scope `_build_mcp` from 0.591 to 0.049 ms median (-91.74%, 12.11x), p95 from 0.613 to 0.055 ms, and 100-round traced peak from 0.108 to 0.066 MiB (-38.85%) with identical sidecar text. Ordered-prefix sticky-state expiration under a process-monotonic clock changed the 4,096-live-state cleanup median from 0.3740 to 0.000481 ms (-99.87%, about 778x), with p95 changing from 0.4009 to 0.000524 ms. A 64-byte raw-plus-normalized digest pair then lets an unchanged 8,000-character sticky query bypass repeated term iteration: with 256 tools, selection changed from 1.6129 to 0.0139 ms median (-99.14%, about 116x), and p95 from 1.6525 to 0.0148 ms; its measured object is 8 bytes smaller than the former hexadecimal digest. Precomputing the shared-name fallback tuple changed empty-query selection from 0.0855 to 0.0119 ms median (-86.08%, 7.18x) and an unmatched short query from 0.2319 to 0.1686 ms (-27.30%, 1.38x); the tuple is 2,088 bytes for 256 tools, or about 8.16/65.25 KiB across the four/32 retained-index bounds. Skipping impossible phrase boosts changed an output-identical fresh 8,000-character query from 1.9073 to 1.7500 ms (-8.25%, 1.09x). Reusing sparse posting candidates for phrase boosts, with a dense tuple-scan crossover and punctuation-only fallback, then changed an unmatched short query from 0.1686 to 0.0176 ms (-89.91%, 9.58x) without adding retained index state; exact and broad-query measurements stayed within 0.2%. These are pure local projection/search/assembly measurements, not end-to-end LLM latency.
Swarm output directories are now lazy until a real stream chunk, while one launch-profiled startup worker scans at most `clamp(sessionCapacity*1024, 512, 16384)` entries and removes only atomically verified empty directories. The current root contains 5,811 empty directories among 7,542 (77.05%); every existing transcript file remains durable. On a temporary 5,811-empty/200-valuable-directory fixture, the bounded sweep took 0.0568 seconds, preserved all 200 valuable directories, and changed a missing cross-turn agent-log lookup from 18.218 to 0.592 ms median (-96.75%). This is a pure local filesystem fixture, not startup or end-to-end latency.
Mutable observers now execute on every call instead of inheriting idempotent-implies-cacheable staleness: conversation references, Project Brain, memory search, schedule listing, swarm artifact listing, and motion-video checks are fresh, while a separate idempotency resolver preserves loop/progress semantics. Selected observations and `get_agent_result` may return a fresh-unchanged receipt only when the new bytes match, the exact prior paid projection remains in active context, and the receipt is no more than half its characters and strictly fewer model tokens; otherwise the full result returns. The independent launch-bounded digest map stores no result body, cannot evict expensive dedup entries, and is dropped at task settlement. A conservative offline replay of failed conversation `mtdx825fjmhmx5` (99 selected calls among 743 task rows) converted 47 repetitions and changed their model-visible projection from 49,486 to 24,797 characters (-49.89%) and 13,553 to 6,619 counted GPT-5.6 tokens (-51.16%); this is a historical counterfactual, not observed billed savings or an end-to-end latency claim. Readable 2026-08-25..30 application logs contained no dedup hit for the newly added non-Project families, so their change is preventive correctness with no production saving claimed. The same failed trace had 955 unique tool-call IDs and 1,048,944 model-visible tool-result characters kept alive by task-lifetime settlement/correlation maps, including at least 310,633 characters already removed from hot context by L1; under the personal profile those references could remain for the 600-second terminal TTL. Settlement bodies are now invocation-local and call-ID receipts contain only three bounded metadata fields. Collision correctness comes from one per-pipeline history index instead of the evictable receipt map: on a 955-message plus 955-round synthetic history with 100 conflicts, median remint time changed from 17.831 to 0.459 ms (-97.42%, 38.83x). The largest assistant Turn in that trace also carried 4,785,739 serialized bytes of `toolRounds` plus 15,284,001 bytes of derived `segments` (20,069,740 combined) in its terminal carrier; turn-native settlement now releases both graphs, any checkpoint rounds, and persisted `programRuns` immediately after their durable authorities commit instead of waiting through the 600-second personal hot-task TTL. Inline/headless response carriers are excluded. The commit observer captures one boolean before release and preference consolidation patches provenance without a full structural fold. On that largest Turn, 592 explicit L1 rounds still had their original full result in the sibling segment; the stable projection now synchronizes only unique identity-compatible mirrors, reducing segments from 15,284,001 to 1,981,839 serialized bytes (-87.03%) and the whole normalized projection from 20,145,745 to 6,843,583 (-66.03%) without mutating the source. These are local serialized-retention/storage-frame/CPU facts from a read-only projection, not observed RSS, disk backfill, API savings, or end-to-end latency.
Structural Turn events now send one revision patch without a duplicated event-envelope projection, retain one live baseline until settlement, and rebase a stale CAS once. On the read-only 955-tool Turn from `mtdx825fjmhmx5`, a `tool_start` command changed from 13,687,938 to 695 serialized bytes (-99.99492%); its 301-byte patch built in 0.338 ms median. The Sidecar now retains a separately bounded, revision-exact baseline too: with the still-required full encode held constant, a cache hit changed local median processing from 51.985 to 26.407 ms (-49.20%, 1.97x), incremental traced peak from 70.269 to 8.635 MiB (-87.71%), and avoided one 5,235,567-byte BLOB read. Its 10,583,620-byte measured retained heap is charged as 15,706,701 bytes under the 32 MiB reference budget. These are local serialization/CPU/allocation proxies; changed Sidecar rows still receive a full encode/write.

### 4. Claude public API versus Claude Code subscription

The direct public Anthropic Messages route enables the `compact-2026-01-12` beta only for Anthropic's documented model families. It sends
`context_management.edits[type=compact_20260112]` at the same working-set
threshold, retains a valid returned `compaction` block verbatim, and uses its
effective post-compaction prompt for the context gauge. Billed usage still sums
all `usage.iterations`, including the internal summary pass. A documented
`content: null` compaction failure is not replayed as an authoritative summary;
the intact history is retried and the local hard-window fallback remains.

Claude Code Pro/Max OAuth is a separate subscription product route. Its captured beta set includes context editing but not the public `compact-2026-01-12`
contract, so Tofu does not inject that beta or strategy there. Local L2 remains
primary. Visible signed thinking is counted from `reasoning_content`; opaque
`redacted_thinking` is retained verbatim and, when the provider reports
`thinking_tokens`, receives the same one-round replay reserve as encrypted
  Responses reasoning.
### 5. Serial single-tool round trips

A 2026-08-28 audit of 12 highest-round production conversations projected 2,597 tool calls into 1,890 tool-bearing model rounds. It found 61 chains of at least six consecutive rounds with exactly one inspection/command-family call: 615 rounds total, with 249 beyond the sixth-round threshold across 29 assistant turns.
Those 249 rounds are an ideal counterfactual opportunity ceiling, not saved calls. In one active Kimi attempt, 81 logged model calls consumed 1,724.2 seconds while 59 timestamp-complete tool receipts consumed 66.795 seconds (0.609-second median, 4.284-second P95), so model round trips dominated the sample.
Separately, none of 2,437 cache records combined a write above 20K tokens with zero cache read; provider-cache repair was not the next bottleneck.
A 2026-08-30 delivery-aware replay of one 739-tool-row Kimi trace found 127 `await_agents` calls. Of 92 no-ID waits, 91 returned only completion payloads already delivered through a prior await/get-result or automatic inbox injection, repeating 221,140 characters / 70,652 raw tool tokens; 90 occurred in await-only model rounds, with a longest seven-round chain.
No-ID waits now consume only unseen completion deltas across synchronous returns, automatic inbox delivery, and restart rehydration; explicit-ID waits and `get_agent_result` still permit deliberate rereads. The historical calls immediately following those 90 await-only returns cost ¥16.4289 and read 6,560,000 cached tokens, 10.72% of the trace's ¥153.1896 regular main-loop API cost.
That ¥16.4289 is an opportunity ceiling, not an observed saving: under the corrected protocol a wait may still incur a later model call after a new completion or timeout. The directly removed behavior is old completed payloads immediately satisfying another no-ID `mode=any` call.

After six successful one-call rounds, the post-dispatch guard may append one
fixed `_isMeta` efficiency reminder. It only recommends grouping independent
direct calls, existing batch arrays, and bounded read-only shell verification;
it never executes, reorders dependencies, combines writes/state changes,
bypasses approval, or changes explicit thinking depth. Failed/parallel rounds,
interactive/MCP tools, `search_tools`/`execute_tools`, safety corrections, and
real user steering block it. It shares one per-task budget with the specific
local-PTC adoption hint, and persistence retains only one content-free witness.

## Expected interpretation

Prompt caching reduces a matching prefix's unit price; it does not make growth
free. Progressive schemas reduce the fixed floor while working-set policy bounds
history. Cache misses from eviction, rerouting or real prefix changes remain
distinguishable through request-wire and cache-write evidence.

## Safe A/B channel

Settings → Advanced exposes a low-code **Cost Optimization A/B Experiment**,
disabled by default. Off returns the original request config and adds no task
metadata. First enablement defaults to a 10% conversation canary with an even
arm split; the other 90% keeps its ordinary configuration.

The experiment has two fixed, reviewable arms:

| Arm | MCP exposure | Automatic L2 working set |
|---|---|---:|
| `control` | `inline` | `0` (window-safety trigger only) |
| `optimized` | `auto` | `128000` tokens |

Only enrollment, optimized share, experiment ID and minimum sample are editable;
policies are not arbitrary JSON. SHA-256 bucketing over experiment + conversation
ID keeps conversations in one arm. Requests explicitly setting `mcpToolExposure`
or `compaction.workingSetTokens` are excluded. Saves use the atomic server-config
writer and `server_config_change` audit event.

An experiment ID locks enrollment and arm percentages. Changing either requires
a new ID, preventing silent rebucketing. Enablement and the report sample gate
remain editable without changing assignment.

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

- [OpenCode CodeMode](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/tool/code-mode.ts)
  is a real analogous implementation, but not the OpenAI Responses PTC
  protocol. Behind `OPENCODE_EXPERIMENTAL_CODE_MODE`, it replaces individually
  exposed MCP calls with one `execute` tool backed by a confined local
  JavaScript subset. It derives the callable tree from visible MCP permissions,
  checks permission and plugin hooks again for every child, gives each child a
  stable derived ID, records child lifecycle, carries structured MCP output and
  attachments, supports cancellation and caps live child concurrency at eight.
  Its reusable runtime supports timeout/call/output budgets, but the OpenCode
  host integration currently supplies none of those three limits. Tofu adopts
  the explicit authority boundary, lifecycle, UTF-8 output bound and concurrency
  cap, while retaining OpenAI's hosted V8/runtime/replay protocol and excluding writes, approvals, citations and native artifacts from hosted program eligibility.
  Local ToolScript instead delegates every catalog child to the ordinary contract, authority, and approval pipeline.
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

Request-local switches support single-factor ablation without process-global
state. Public GPT-5.6 Responses requests additionally get automatic native
Tool Search for the non-pinned residual catalog:

```json
{
  "cache": {"gpt56BreakpointMode": "explicit"},
  "tools": {
    "nativeExposure": "routed",
    "programmaticCalling": "on",
    "programmaticExposure": "additive",
    "toolSearch": "auto"
  },
  "responses": {
    "transport": "sse",
    "reasoningMode": "standard",
    "verbosity": "medium",
    "imageDetail": "auto",
    "promptProfile": "auto"
  },
  "orchestration": {
    "multiAgent": "auto",
    "maxConcurrentAgents": 3
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
  custom/plugin safety surfaces. `full` is unchanged. The report records both
  exposed tool count and exact schema-token cost so routing is judged on token
  reduction and discovery recall, not catalog size alone.
- `tools.toolSearch=auto` never removes tools from the task authority catalog.
  Eager tools, explicit caller tools, the current task's MCP active set and a
  forced `tool_choice` remain direct; searchable residual tools are deferred
  natively or reached through the local gateway. Each model turn assembles its
  wire catalog and discovery metadata from current settings; provider
  conversion uses that request-local projection.
- L2 always receives a bounded in-memory working-state ledger of modified
  files, tests, errors, query conclusions, command/agent results and unfinished
  work. This closes the state-loss gap created by intentionally excluding raw
  tool messages from the prose summary input. Stable evidence IDs make retained
  and lost facts auditable after compaction; the transient ledger is released
  after that measurement. Entries are past observations rather than proof of
  current mutable state, so the summary tells the agent to revalidate when freshness matters.
- `compaction.evidenceLedger=true` additionally persists exact L1 cold tool
  results and adds their recovery handles to that ledger. It remains opt-in
  because durable raw-result storage has a different privacy/I/O trade-off.
- `tools.programmaticCalling` accepts `on` (default), `auto`, and `off`. Per request, `resolve_programmatic_backend` (`lib/tools/programmatic.py`) selects `native_openai` for public GPT-5.6 Responses or `local` for every other tool-capable wire; no eligible reviewed read tool and `off` both resolve off. Hosted OpenAI PTC admits only explicit `ToolSpec.programmatic_tools`; retry/idempotency is never a safety proxy. Local ToolScript instead accepts any exact member of the task executable catalog, whether found by `search_tools` or already known, after the same request-owned schema validation and ordinary authority/approval checks as `execute_tools.calls`.
  Both backends use `{content: string, truncated: boolean}`. Hosted writes, approvals, plugins, web/citation, image/native-artifact, skill-loading, and MCP-discovery surfaces stay direct. Enforcement rejects hosted-direct-only/excess calls and caps a program at 16 children, eight concurrent client tools, 1 MiB UTF-8 output, and four continuations; hosted V8 owns timeout.
  `programRuns` records raw/delivered bytes, truncation, and execution source, so local ToolScript cannot masquerade as native PTC; promotion requires a completed run without rejection, budget violation, or truncation. `on` exposes the resident tier whenever a reviewed read tool is assembled (`resident_eligible_read_tools`). `auto` additionally requires many-result intent or recent parallel/consecutive read fan-out (`observed_read_fanout`); `off` is immediate rollback.
  Stateless replay preserves `program`, nested `caller`, structured `function_call_output`, and `program_output` order. Chat persists canonical `programRuns`, then projects a source-labelled parent card while ordinary child views remain below it.
  The local backend projects `execute_tools` through `ptc_local_wire_tools`, resolved in `prepare_request`; all model sizes author bounded server-side ToolScript reductions, and malformed programs return typed retryable interpreter errors behind catalog/schema/call/byte limits. `TOFU_PTC_TIER=batch` is the parallel-`calls[]` operator/benchmark override; fixed-tier schemas stay stable while hosted-eligibility observations remain outside the wire.
  `task['_ptc_local']` selects the local tier and retains activation evidence; it does not narrow child authority. Local programs and ordinary `execute_tools` batches share the executable catalog, admission/approval pipeline, bookkeeping, and UI.
  A local projection followed by three productive single eligible-read rounds adds at most one fixed, non-forcing `_isMeta` adoption hint. Safety corrections preempt it, native/off lanes and genuine user steering exclude it, and terminal metadata keeps one content-free witness; actual `programRuns`, not the hint, remain the adoption authority.
  `programmaticExposure=additive` remains the shipped default. The
  `serial_gateway` value performs one explicit gateway-only trial after the
  same three-round evidence threshold and then restores direct tools; it is an
  opt-in experiment, not a quality or cost assumption.
  Production calibration found one 93-call coding task with 107 tools, 7.149M prompt tokens (6.945M cache reads), ¥23.05 cost, and 2,286.3 seconds wall time; its retained decision tail offered the local gateway 45 times but recorded no program run.
  Five observed serial-read chains covered 24 model rounds. Perfectly collapsing every chain to one call gives a 19-call theoretical ceiling; counting only calls after each third-read detection gives a stricter nine-call counterfactual opportunity ceiling. Neither number is an observed saving, so rollout still requires post-hint `programRuns`, quality parity, and measured latency/cost.
- `responses.promptProfile=auto` selects compact GPT-5.6 but keeps Kimi on full.
  Explicit Kimi lean/ablation arms emit applied profile, SHA-256 and token proof;
  the benchmark rejects missing/mismatched adoption before any promotion.
- `orchestration.multiAgent=auto` is first-round-only and requires independent,
  complex workstreams; `read_only` forces the lane and `off` is the immediate
  rollback. The provider-neutral policy in
  `lib/tasks_pkg/tool_orchestration_policy.py` selects this control plane and
  the programmatic data plane independently. Both may therefore be active:
  agents partition independent workstreams, while the root and each eligible
  local worker use PTC to reduce repeated reads inside its own workstream.
  `lib/swarm/routing.py` selects native OpenAI acceleration only for a verified
  public GPT-5.6 Responses face and otherwise projects the existing local
  `spawn_agents` runtime for every model whose task authority includes it.
  Native and local workers are analysis-only: the shared execution boundary
  rejects file, shell, artifact, scheduler, project-state, integration, and
  other non-root mutations even if prompt guidance is ignored. Streaming and
  non-streaming surfaces expose only `/root`'s `final_answer`.
  Benchmark arms remain single-factor experiments for causal attribution; that
  experimental isolation is not a production mutual-exclusion rule.
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

The 2026-08-24 comparison keeps the model fixed: Tofu and Codex CLI 0.149.1
both call the same Meituan `kimi-k3` slot with the same thinking setting. The
isolated loopback-only `evaluations/codex_kimi_proxy` translates one Codex
Responses request into exactly one Kimi Chat Completions request. It preserves
instructions/messages, strict function schemas, tool choice, thinking, usage,
SSE order, errors, and cancellation. It never joins production routing, reads
user configuration, or logs a key. `/responses/compact` invalidates the trial.
Codex uses local compaction and its pinned 0.149.1 fallback: 272,000 context tokens and a 244,800 compact limit;
metrics separate raw wall, proxy CPU, translation CPU and Codex-favored wall.
Namespace functions flatten; native `web_search` is suppressed and recorded;
unknown native types fail closed. A real CLI smoke proves one Kimi call.
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
judges pass, critical incidents remain zero, cost/success and P90 oracle-ready
remain at most 85% of Codex, and infrastructure failures stay preregistered.
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
  neutral local budget preserves the code core, with 600/8,000/24,000-token gateway/result/round targets and owner-scoped artifact cursors. The envelope is internal evidence; provider messages receive a sparse semantic projection while a bounded non-model sidecar preserves harness and loop-guard facts;
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
