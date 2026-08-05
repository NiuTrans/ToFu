# Prompt-cache not reused on byte-identical prefixes — `example_corp_anthropic` gateway

**Reporter:** Tofu (chatui) · **Date:** 2026-07-29 · **Model:** `claude-opus-5`
**Endpoint:** `https://api.openai.com/v1/anthropic` (`protocol: anthropic`)

---

## Summary

On the `example_corp_anthropic` Anthropic-native face, a large fraction of our
requests re-send a prefix that is **byte-identical** to the immediately
preceding request on the same conversation, with **identical routing** (same
upstream key, same `anthropic-beta` header, same endpoint), after a gap that is
**far longer than Anthropic's documented cache write-visibility window** — and
the response comes back with `cache_read_input_tokens = 0`, so the entire
prefix is re-billed as `cache_creation`.

We are not asking you to accept a diagnosis. We are reporting a reproducible
observation with per-request trace IDs so it can be checked against your side.

**Fleet scale.**

> **Measurement window — pinned and reproducible.** Every figure below comes
> from one bounded run. The log is live and still growing, so an unbounded run
> reports larger totals; to reproduce THIS table exactly:
>
> ```
> python3 scripts/cache_waste_report.py --until '2026-07-29 15:59:50'
> ```
>
> Window: `2026-07-18 17:25:33` → `2026-07-29 15:59:50`, 47,227 instrumented
> rounds. Re-running that command reproduces every number here byte for byte
> (verified twice). An UNBOUNDED run later the same day reported 86.1 %
> upstream share against 84.6 % here — the shape is stable, only the totals
> accrue.
>
> Two rows are labelled from records stamped BEFORE the classifier fixes of
> 2026-07-29: `ttl_expiry` reads empty, and a group of unclassifiable rounds
> still reads `no_break` rather than `indeterminate`. Buckets are stamped at
> write time (that is what keeps offline and live counts from drifting), so
> historical rows keep their original label. Neither affects the
> `upstream_identical` claim, which is the subject of this report.

Of 2,113 rounds that wrote >20k cache tokens and read back **zero**:

| | |
|---|---|
| Rounds where the prefix was byte-identical and routing identical (`upstream_identical`) | **1,486** |
| Gap since previous round on the same conversation — median | **38.7 s** |
| Gap — p90 | **91.2 s** |
| Fraction with a gap under 18 s (i.e. plausibly the write-visibility race) | **4.0 %** |
| Tokens written that we believe should have been read back | **661,014,804** |
| Cost of those writes at our contracted Opus-5 rate | **≈ 29,911 CNY** |
| Cost had they been served from cache | **≈ 2,393 CNY** |
| **Difference** | **≈ 27,518 CNY** |

That difference is **84.6 %** of all avoidable cache spend we can measure,
after excluding two classes that are **not** waste and which we are explicitly
*not* reporting as a problem:

* **ordinary TTL expiry** — idle > 5 min. The entry expired on its own
  schedule and had to be rebuilt.
* **cold starts** — 238 rounds, first round of a conversation with no preceding
  request, so there was nothing to read back.

Full bucket breakdown:

| bucket | n | wasted tok | recoverable CNY | share | gap p50 | gap p90 | % < 18 s |
|---|---|---|---|---|---|---|---|
| `upstream_identical` | 1,486 | 661,014,804 | **27,518** | **84.6 %** | 38.7 s | 91.2 s | 4.0 % |
| `body_change` (our client) | 138 | 50,961,685 | 2,122 | 6.5 % | 37.8 s | 365.4 s | 33.3 % |
| `other` | 74 | 29,189,382 | 1,215 | 3.7 % | 314.6 s | 513.7 s | 18.9 % |
| `cache_write_unsettled` | 114 | 25,137,880 | 1,046 | 3.2 % | 14.7 s | 17.6 s | 98.2 % |
| `no_break` (unclassifiable; see note) | 46 | 11,017,397 | 459 | 1.4 % | 12.8 s | 112.8 s | 50.0 % |
| `cache_mid_out_of_window` | 10 | 2,226,155 | 93 | 0.3 % | 18.6 s | 53.5 s | 40.0 % |
| `ttl_flip` | 4 | 1,165,353 | 49 | 0.1 % | 33.1 s | 37.6 s | 0.0 % |
| `cache_namespace_switch` | 1 | 514,308 | 21 | 0.1 % | 54.1 s | 54.1 s | 0.0 % |
| `breakpoint_lost` | 2 | 501,064 | 21 | 0.1 % | 72.1 s | 309.6 s | 0.0 % |
| *excluded — cold start (not waste)* | 238 | 53,010,620 | — | — | 0 s | 0 s | — |

**Total recoverable: ≈ 32,543 CNY**, of which the byte-identical upstream class
is 84.6 %.

**The gap distribution separates the two candidate mechanisms cleanly**, which
is the single most important row-pair in this report:
`cache_write_unsettled` sits at p50 **14.7 s** with **98.2 %** under 18 s — a
textbook write-visibility race, and we handle it on our side.
`upstream_identical` sits at p50 **38.7 s** with only **4.0 %** under 18 s.
These are not the same phenomenon, and the second one is not a race we can win
by waiting longer.

---

## What we send, and how we know it is identical

Every outbound request is fingerprinted **at the wire layer** — after all
client-side serialisation, immediately before transmission. Per round we record:

* a hash of the `system` block,
* a hash of the `tools` array,
* per-message content hashes,
* the ordered list of content-block byte lengths,
* the resolved routing tuple (upstream key id, `anthropic-beta` value, endpoint).

A round is only counted in the table above when **all** of these are unchanged
from the previous round, i.e. we can state positively that our client did not
alter a single byte of the cached prefix and did not change cache namespace.

For the worked example below (conversation `ms5i5ydigs9j9w`), across its 28
rounds the `system` and `tools` hashes were **constant for every round**, and
the content-block count grew **monotonically 1 → 56** — the prefix is strictly
append-only, which is the shape prompt caching is designed for.

---

## Worked example — 11 requests, one conversation, one hour

Conversation `ms5i5ydigs9j9w`, model `claude-opus-5`, 28 rounds. Every row below
had a byte-identical prefix and identical routing versus its preceding round,
and returned `cache_read_input_tokens = 0`. Gaps are the measured elapsed time
since the previous request on this conversation.

| Round | `M-TraceId` | Gap vs prev | Re-written (tok) |
|---|---|---|---|
| R5  | `b7e6dd6b6f9748c483b36971a8afc74e` | 19.0 s | 172,894 |
| R7  | `d686fe69f68443b886ed38360bfdd4fc` | 15.7 s | 167,208 |
| R8  | `56f0bf2fdf9f4f419a69b7f54fa7fdb7` | 37.5 s | 174,356 |
| R10 | `5509f4457dd0459095f524cbc64cbce9` | 66.9 s | 180,700 |
| R12 | `0d4af8a4ffb941938e3471c776f22a9e` | 52.8 s | 196,162 |
| R15 | `c803e738b237468086314de6dd67c5bf` | 78.3 s | 219,104 |
| R16 | `a0663d08b8c44fa6b76db6d02757686e` | 52.3 s | 223,061 |
| R18 | `e2c16f208b6e494c89e8e7028749c6b3` | 14.8 s | 233,315 |
| R21 | `3ca1ba8fa25e48da9f20e921328bfe84` | 23.2 s | 238,474 |
| R23 | `76b9f58f1ce641cfa3d720535ed07e7c` | 38.5 s | 239,418 |
| R26 | `b02eb18e5c46403bacdd33e32509c07c` | 13.8 s | 241,431 |

**Interleaved control — the same conversation DID hit cache on the rounds in
between**, which is what makes these misses hard to explain as a configuration
problem on our side:

| Round | Gap vs prev | `cache_read_input_tokens` |
|---|---|---|
| R3  | 9.2 s  | 121,817 |
| R6  | 10.5 s | 126,858 |
| R9  | 24.4 s | 157,121 |
| R11 | 14.4 s | 168,137 |
| R13 | 13.2 s | 196,162 |
| R17 | 27.7 s | 219,104 |
| R19 | 100.7 s | 206,724 |
| R20 | 56.9 s | 222,550 |
| R22 | 13.9 s | 223,271 |
| R25 | 18.0 s | 226,362 |

Note R19: a **100.7 s** gap that **hit** cache, alongside R18's 14.8 s gap that
**missed**. Within one conversation, on one key, with a strictly append-only
prefix, hit and miss are interleaved and do not correlate with elapsed time in
the direction a TTL or a write-visibility window would predict. That is the
core of what we would like explained.

---

## What we ruled out on our side first

We do not want to send you a problem that is ours. Each of the following was
measured and excluded before writing this report:

| Hypothesis | How it was excluded |
|---|---|
| **Our client mutated the cached prefix** | Wire-layer fingerprint identical (system hash, tools hash, per-message hashes, block-length vector). Rounds where the fingerprint *did* change are classified separately (`body_change`, n=138) and are **not** in the 1,486. |
| **Cache-namespace switch** (key / `anthropic-beta` / endpoint changed between rounds) | Routing tuple recorded per round and verified identical. Rounds with a routing flip are a separate bucket (n=1). |
| **Anthropic write-visibility race** (a freshly written entry is not readable for ~15–20 s) | Measured per round. Only **4.0 %** of the 1,486 have a gap under 18 s; median gap is 38.7 s. We *do* separately observe and handle this race — `cache_write_unsettled`, n=114, median gap 14.7 s, 98.2 % under 18 s — and those rounds are excluded from the 1,486. |
| **Ordinary TTL expiry** (idle > 5 min, entry legitimately expired) | Excluded as a distinct bucket. These are *expected* rebuilds and we are **not** reporting them as a problem. |
| **Cold start** (first round of a conversation) | Excluded: 238 rounds. No preceding request exists, so there is nothing to read back. |
| **Rounds we could not classify** | Reported honestly in their own row (n=46) rather than folded into the upstream claim. |
| **Cross-conversation cache contention** | A/B tested previously: per-round `cache_read` is identical between solo and interleaved traffic (±0.0 %). Anthropic caches key on exact prefix bytes, so distinct conversations cannot evict each other. |
| **Compaction / history rewrite on our side** | Rounds following a context compaction or a backend history edit are flagged and bucketed separately. |

---

## What we would like to understand

1. Does the gateway maintain prompt-cache affinity across requests, or can two
   requests carrying the same prefix land on backends with independent caches?
2. What is the effective cache TTL on this face? Our observations would be
   consistent with an entry lifetime on the order of tens of seconds, which is
   much shorter than the 5-minute Anthropic default.
3. Is there a per-application or per-key cache capacity/eviction policy that
   could evict a large (100k–500k token) prefix within ~40 s of writing it?
4. Do you need anything further from our side — full `M-TraceId` lists,
   request-body hashes, or timing traces — to correlate against your logs?

We are happy to run any targeted experiment you suggest (e.g. a controlled
re-send at fixed intervals with a fixed prefix) and share the raw results.

---

## Reproducing our figures

All numbers above are derived from `[CacheRoundRecord]` entries in our
application log — one machine-readable record per LLM round, carrying the
verdict bucket, `cache_read`, `cache_write`, and the measured gap in seconds.
The classifier that produces them lives at
`lib/tasks_pkg/cache_tracking/_detect.py::classify_verdict`, and the same
function is used by both the live path and our offline replay harness, so
offline and live counts cannot drift.

```
python3 scripts/cache_waste_report.py --until '2026-07-29 15:59:50'
```
