# Prompt-cache diagnostics

`_detect.detect_cache_break` owns round ordering and the shared state lock.
Read `docs/modules/context_engineering.md` for the diagnostic boundary.

- `_policy` owns thresholds, cause precedence and verdict buckets; it has no
  storage, provider or logging dependencies. Live and replay use the same rules.
  The TTL-expiry gap is the previous round's shortest wire marker TTL
  (`cache_ttl_window_s`), never a fixed five minutes.
- `_wire` compares provider evidence without mutating the previous baseline.
  Preserve canonical/raw-byte/layout/routing precedence and position evidence;
  positions and the prior-prefix boundary both count wire messages, never the
  API-form message count.
- `_advance` updates the baseline and stages existing guarded durable sinks.
  Only unscoped observations may use direct persistence fallback; omitted reads
  are not zero reads. Comparison must finish before advancement.
- `_report` renders confirmed-break evidence; `_detect` retains exactly one
  round-record publication for every observation, including indeterminate ones.
- `_health` owns the same-route read-share split: record dimensions, the
  divergence rule, log-line parsing, the streaming tally shared with
  `scripts/cache_waste_report.py` and the nightly optimizer, and the bounded
  in-process monitor that `_detect` feeds after each record. It never changes
  a bucket or a request, and takes only its own lock (never `_cache_lock`),
  so callers may hold `_cache_lock` while feeding it.
- `_state`, `_persist`, `_hashing`, `_roi` and `_ttl` retain their existing
  lifecycle, persistence, fingerprint, accounting and TTL ownership.

## Diagnosing a cold request group

A `[CacheHealth]` WARNING or a `!!` line from `scripts/cache_waste_report.py`
means one group of requests on a model stopped reusing its cache while another
group on the same route kept reusing it. A byte-identical prefix and an empty
`routing_diff` rule out only round-over-round drift inside the cold group; they
are not evidence of an upstream fault.

1. Compare what the cold and warm groups send: Request Inspector raw archives
   (`/api/v4/tasks/<task>/raw-archives/<id>/request`) and the record fields
   (`wire_tools`, `wire_tool_name_bytes`, `tools`, lane). The report's
   read-source table separates misses by what was read back: `older` means
   consecutive rounds hit different upstream cache domains, which no request
   byte comparison can show. Its `affinity` column names the carrier that sent
   the lineage key (`lib/llm/cache_affinity.py`). If a route has no carrier,
   find the provider's documented one and declare it. If rows that carry a key
   still read `older`, the hop ignores the key.
2. A/B each property that differs with `scripts/cache_ab_probe.py --arm`:
   otherwise-identical synthetic bodies, 2-3 sends 10-15 s apart.
3. Enforce the measured constraint at the owning projection boundary with a
   test, and record Problem/Decision/Alternatives in
   `docs/LLM_COST_OPTIMIZATION.md`.
4. Report the minimal repro to the provider; re-measure before relaxing.

Never send duplicate, keep-alive or warm-up model requests to raise reuse.

Verify with `tests/cache/` and the wire fingerprint/byte-gate tests under
`tests/tasks_pkg/`. `benchmarks/wire_fingerprint_compare.py` compares frozen
source against current comparison code with complete-output parity and paired
CPU/wall/allocation measurements; it does not measure upstream cache behavior.
