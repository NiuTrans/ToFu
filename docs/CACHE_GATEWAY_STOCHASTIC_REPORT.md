# Prompt-Cache Residual Floor-Collapse — Gateway-Side Stochastic Write-Visibility

> **Audience:** the LLM gateway / Bedrock platform team.
> **Ask:** investigate a server-side prompt-cache **read-after-write visibility
> race** that re-bills the entire cached prefix on a fraction of requests whose
> input bytes are provably identical to a previously-cached request.
> **Status:** the client side has been exhausted (see §4); the residual is not
> client-fixable. A client-side mitigation (identical-resend) works but is a
> band-aid — the root fix is server-side.

---

## 1. Symptom

On a multi-round agent tool loop against `aws.claude-opus-4.8`, a fraction of
rounds report `cache_read` pinned at the static system+tools **floor** (~28k–74k
tokens) while `cache_creation_input_tokens` re-bills 40k–70k tokens — i.e. the
whole conversation body past the static prefix is **not read back from cache**,
even though:

- the request's wire bytes are **byte-identical** in the shared prefix to the
  immediately-preceding round (verified field-by-field, see §4.1);
- the cache-relevant **block geometry is well inside** Anthropic's ~20-block
  backward lookback window (tail sits 3–7 blocks past the last breakpoint, never
  >20, see §4.2);
- the routing namespace (key hash / endpoint / `anthropic-beta`) is unchanged.

## 2. Proof it is STOCHASTIC (server-side), not deterministic (client-side)

We replayed the **same** stored conversation, reconstructing the **byte-identical**
per-round wire bodies, against the live gateway **four times** (mode=drop, no
mid-anchor, so the layout is fixed and minimal). The rounds that floor-collapse
**differ every run**, and the rate swings widely:

| run | inter-round gap | floor-collapse% | which rounds collapsed |
|---|---|---|---|
| A | 2 s | 36.8% | R2, R3, R4, R6, R9, R11, R18 |
| B | 2 s | 13.3% | R8, R15 |
| C | 8 s | 28.6% | R3, R5, R8, R11 |
| D | 2 s | 40.0% | R2, R6, R8, R12 |

**Interpretation:**
- Same bytes + same gap (A vs B vs D) collapse **different rounds** at **different
  rates** → NOT a deterministic client-layout bug (else the same rounds would
  collapse every run).
- Widening the inter-round gap 2 s → 8 s (C) does **not** reduce the rate → NOT a
  simple read-after-write timing lag a client can outrun by waiting.
- The floor VALUE itself varies run-to-run (28654 / 74095 / 0) — a server-side
  signal.

This is consistent with a **cache-write-visibility race** in the gateway/Bedrock
prompt-cache tier (cf. Anthropic SDK issue #1451): the cache entry written by
round *k* is not yet consistently visible to the lookup performed by round *k+1*,
so the lookup falls all the way back to the static prefix and re-bills the body.

## 3. Cost impact

Across the real miss-heavy conversations in production logs (grep
`[CacheRoundRecord] bucket=cache_mid_out_of_window`), this pattern accounted for
**~170 rounds / ~20M re-billed `cache_creation` tokens** in a single day's
traffic before the client-side mid-anchor fix (§4.3), and a residual ~8–35% of
rounds after it.

## 4. Everything the client already did (why this is not client-fixable)

### 4.1 Prefix is byte-stable
A per-field wire fingerprint (`wire_byte_field_prefix` → `diff_byte_field_prefix`)
of the final post-translation Anthropic body shows **zero** in-place field
mutation in the shared prefix between consecutive collapsing rounds — the only
delta is the appended new round (pure growth). So no client re-serialization,
reasoning-details rebuild, tool-arg reorder, or role-merge is changing the cached
bytes.

### 4.2 Block geometry is in-window
The conversation tail breakpoint sits 3–7 content blocks past the previous
round's breakpoint every round (measured on the flattened Anthropic content-block
stream) — never exceeding the ~20-block lookback. So the miss is not the client
losing the prior cache entry outside the lookback window.

### 4.3 Mid-anchor removed (net-negative)
A mid-history "stepping-stone" breakpoint was found to be **net-negative** on
byte-stable prefixes (it jumps every few rounds and each jump writes a fresh
entry the tail can't chain back to). Removing it (`TOFU_CACHE_MID_MODE=drop`, now
the default) cut floor-collapse materially with no downside. The residual in §2
is what remains **after** this fix.

### 4.4 TTL namespace locked
The stable-block `cache_control.ttl` (1h/5m) is latched per session so it can't
flip mid-conversation and re-key the cache.

## 5. Client-side mitigation that works (band-aid, not root fix)

Because the collapse is independent per request, **resending the identical
byte-stable body on a detected floor-collapse** re-rolls the dice and usually
recovers:

| conv | resends allowed | floor% before | **effective floor% after** |
|---|---|---|---|
| mrsfs9d6 | 3 | 20.0% | **0.0%** (all 3 collapses recovered on resend) |
| mrt1ijef | 2 | 23.5% | **11.8%** (2 of 4 recovered; the other 2 resends hit HTTP 503 throttle) |

The recovery of a **delayed identical resend** further supports the
write-visibility-lag hypothesis: the cache write becomes visible a moment later,
so the resend hits it. The mitigation's only practical limiter is that gateway
**HTTP 503 throttling** eats the resend budget — which is itself a reason to fix
this server-side rather than push more retry load onto the gateway.

## 6. The ask

1. Investigate read-after-write visibility consistency in the prompt-cache tier
   for `aws.claude-opus-4.8` (and siblings) — a lookup should deterministically
   see a cache entry written by the immediately-preceding request on the same
   key/endpoint once that write has been acknowledged.
2. If there is an eventual-consistency window, document its bound so clients can
   set a principled post-write delay instead of blind resend.
3. Confirm whether HTTP 503 throttling and the cache-miss are correlated (a
   throttled request may skip the cache write entirely).

---

## Appendix — how the evidence was produced (reproducible)

`debug/cache_db_replay_live.py` (local diagnostic; `debug/` is gitignored)
reconstructs the byte-identical per-round wire body of a real stored
conversation via the production `build_api_messages_from_db` +
`_inject_system_contexts`, then sends each growing prefix to the live gateway and
records `cache_read` / `cache_creation_input_tokens`, the per-field prefix
fingerprint diff, and the tail block geometry. Arms:

```
# 4-run stochasticity proof (identical bytes, mode=drop)
python debug/cache_db_replay_live.py --conv mrt1ijef --arms drop --max-rounds 18 --gap 2
# retry mitigation
python debug/cache_db_replay_live.py --conv mrsfs9d6 --arms drop --retry-on-floor 3
```

The offline verdict logic (floor classification, culprit filter, summary) is
guarded by `tests/test_cache_db_replay_live.py`.
