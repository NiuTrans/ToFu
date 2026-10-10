# Tool-Result Return-Format Audit

Static audit of every tool-producing return site in the repo, run by
`scripts/audit_tool_result_formats.py`. The question: how close are we to a
**unified auditing method** for tool-result formats, and where do results
still return JSON (or key-value pairs) the model never needed?

## TL;DR

You **already have the unified mechanism** — `lib/tools/result_envelope.py`
(`ToolResultEnvelopeV2` + the `_model_projection` sparse projection). It is
the single authority that decides "plain text vs JSON" and strips constant
plumbing keys before anything reaches the model. What is missing is
**coverage and an audit**: only a handful of tools produce envelopes today,
and until now nothing measured which ones still hand-roll JSON.

The audit's job, then, is not to design a new format system — it is to (1)
enumerate the ad-hoc return sites the projection can't help, and (2) classify
each by whether it needs JSON at all.

## The existing contract (the "unified method" you're asking for)

`lib/tools/result_envelope.py` already implements exactly the policy you
described:

- **"Some cases don't need JSON at all."** `_model_projection`
  (result_envelope.py:52) maps each internal envelope to one of four
  projections: `text` (plain-text success, no JSON wrapper), `json_value`
  (a single structured value emitted as *bare* JSON, no envelope keys),
  `summary_items`, and `error`/`partial` (sparse JSON). Complete plain-text
  successes never become JSON.
- **"Some key-value pairs don't need to be returned."** The sparse projection
  strips constant plumbing before model delivery: `contractVersion`,
  `cursor: "0"`, `historicalRecovery: false`, `kind: "source"`, empty
  `arguments`, `visibleBytes`, `freshness`, and any `summary` that merely
  restates the error code/message. The fat evidence record is kept server-side
  via `split_tool_result_delivery`, out of `role='tool'` content.
- It's gated by `tools.resultEnvelope` (default `v2`, `legacy` rollback) in
  `lib/context_experiment_flags.py:41`, applied centrally in the dispatch
  pipeline at `lib/tasks_pkg/tool_dispatch/_pipeline.py:715-798`.

**Crucially:** legacy results that carry no `contractVersion` marker pass
through the projection **byte-for-byte** (`split_tool_result_delivery`
returns them unchanged). So any tool that hand-rolls JSON gets *no* sparse
stripping and *no* typed-error semantics — which is exactly the population
the audit needs to find.

## Audit method

`scripts/audit_tool_result_formats.py` parses the AST of every `.py` under
the tool-producing roots and classifies each `return` site:

| kind | meaning |
|---|---|
| `envelope_v2` | produces `ToolResultEnvelopeV2` / `typed_tool_error` / `TrustedResultText(envelope=True)` |
| `trusted_text` | `TrustedResultText` / `capture_result` receipt (canonical, non-envelope) |
| `plain_text` | bare str / f-string / literal — no JSON wrapper |
| `legacy_json` | returns `json.dumps(...)` — ad-hoc JSON, bypasses projection |
| `passthrough` | returns another call's result / variable (indeterminate) |
| `empty` | bare `return` / `return None` |

Scanned roots: `lib/tools`, `lib/tasks_pkg/handlers`, `lib/agent_core`,
`lib/mcp`, `lib/tasks_pkg/tool_dispatch`, `lib/paper`, `lib/swarm`.

## Headline numbers

- Files with return sites: **240**
- Total return sites: **4192**

| kind | count | pct |
|---|---|---|
| `passthrough` | 3255 | 77.6% |
| `empty` | 552 | 13.2% |
| `plain_text` | 332 | 7.9% |
| `legacy_json` | **48** | 1.1% |
| `trusted_text` | 3 | 0.1% |
| `envelope_v2` | 2 | 0.05% |

The dominant `passthrough`/`empty` mass is expected — most tool bodies return
helper results or finalize via `_finalize_tool_round` rather than returning
content directly. The signal is in the tail.

**Most of the 48 `legacy_json` hits are false positives for this audit** —
they're `json.dumps` used for hashing (`todo_state_fingerprint`,
`manifest_digest`), SSE framing (`_chunk_frame`), byte-size accounting
(`_serialized_event_bytes`), or the envelope's own serializer
(`result_envelope.py:_compact_json`). Filtering those out, the **real ad-hoc
model-facing JSON population is ~25 sites, and 23 of them are in one file.**

## Finding 1 — one file is almost the entire migration surface

`lib/swarm/integration/_tools.py` holds **23 of the ~25 real ad-hoc JSON
sites**, all in the swarm control tools (`spawn_agents`, `await_agents`,
`get_agent_result`). Example shapes:

```python
return json.dumps({'error': 'no agents specified', 'status': 'error'})
return json.dumps({'status': 'error', 'error': 'swarm_wave_capacity',
                   'limit': wave_limit, 'requested': len(agents_data),
                   'message': '...'})
return json.dumps({'status': 'ok', 'results': results}, ensure_ascii=False)
```

These reach the model (via `_handle_swarm_tool` → `simple_call` → dispatch
pipeline), but because they're legacy JSON with no `contractVersion`, they
get **zero** of the contract's benefits.

**Error-shape drift (the most important concrete finding).** The swarm error
shape is `{'status': 'error', 'error': <str>, 'message': <str>}` — note
`error` is a *string*, and there is **no `retryable` and no `next_action`**.
The V2 typed-error contract (`ToolResultErrorV2`, parsed by
`tool_result_error` at result_envelope.py:377) requires `error` to be an
*object* with a real boolean `retryable` and a stable `code`. Verified:
`grep retryable lib/swarm/integration/_tools.py` → **no matches**.

Consequence: every swarm error fails open in `tool_result_error` (returns
`None`). So `nonretryable_tool_error_code` — the predicate the terminal-
failure policy and tool-loop breaker (`lib/tasks_pkg/orchestrator/_tool_loop_breaker.py`,
`lib/agent_core/` terminal-failure tests) rely on — **cannot see a swarm
failure as terminal**. A non-retryable swarm error (e.g.
`swarm_session_capacity`, `orchestration_session_mode_conflict`) is
indistinguishable from prose, so the retry guards keep retrying what the
contract would have stopped.

This is a real behavioral gap, not a style nit — and it's invisible until you
audit the shape, which is precisely the case for having the audit.

## Finding 2 — who's already on the contract (the good population)

The contract is proven in production by the tools that matter most for cost:

- **Search / fetch / browser-download** — `lib/tasks_pkg/handlers/search/_handlers.py`
  uses `typed_tool_error(...)` for every failure path (verified at lines
  438/458/492). These are the highest-volume tools, and they're fully typed.
- **Browser** — `lib/browser/dispatch.py` and `lib/browser/site_tool.py`
  produce `typed_tool_error`; `lib/tasks_pkg/handlers/browser.py:130`
  consumes `ToolResultEnvelopeV2.to_model_text()` directly.
- **Artifacts / code-exec** — produce `TrustedResultText` canonical receipts
  (`tool_result_artifacts.py:345`, `code_exec.py:670`).

So the contract is not aspirational — it's the shipped default for the
expensive tools. The gap is the long tail.

## Finding 3 — a second, inconsistent ad-hoc shape is emerging

`lib/tasks_pkg/handlers/tool_result_artifacts.py` and `lib/tools/gateway.py`
return yet another error shape:

```python
{'status': 'error', 'error': {'code': ..., 'retryable': False,
                              'nextAction': '...'}}   # camelCase nextAction
```

`tool_result_error` accepts this *only* because it special-cases both
`next_action` and `nextAction` (result_envelope.py:399-407). So there are now
**three** coexisting error spellings — swarm string-error, gateway
camelCase-object-error, and the canonical V2 `next_action` object — and the
parser is quietly papering over the divergence. An audit rule ("error objects
must use `next_action`, must carry real-boolean `retryable`") would catch
this at the source instead of in the parser.

## Findings mapped back to your two concerns

1. **"We often return JSON even when JSON isn't needed."** True for the swarm
   tools: their *success* handles (`json.dumps({'status': 'ok', 'results': ...})`)
   are structured, but the sparse projection would emit them as bare
   `json_value` (no `status`/`contractVersion` wrapper) if they were envelopes.
   Today they carry the wrapper to the model. The other ~90% of the surface
   already returns plain text or helper receipts, so this concern is narrow,
   not systemic.
2. **"Some key-value pairs don't need to be returned."** The projection
   already strips these — **but only for envelopes.** The fix for the ad-hoc
   tail is not to strip keys per-site; it's to route those sites through
   `ToolResultEnvelopeV2` so the existing stripper applies. That's a
   coverage fix, not a new policy.

## Recommendations

**R1 — Migrate `lib/swarm/integration/_tools.py` to `typed_tool_error`.**
~23 sites, one file, highest payoff. Converts string-errors into typed errors
with real `retryable`/`next_action`, which immediately makes terminal-failure
policy and the loop breaker see swarm failures correctly. Pattern is already
established in `search/_handlers.py`.

**R2 — Make the audit the unified method's enforcement arm.** Keep
`scripts/audit_tool_result_formats.py` as a CI gate:
- flag any new `json.dumps(...)` return in a tool handler that isn't hashing/
  framing/size-accounting (the whitelist above);
- flag any `{'status': 'error', 'error': <str>}` (string-error) — must be an
  object with boolean `retryable` + `code`;
- flag `nextAction` (camelCase) in new code — canonical is `next_action`.

**R3 — Optional runtime sampling.** If you want numbers on "results that
didn't need JSON" rather than static sites, hook the single choke point
`split_tool_result_delivery` to log `projectionKind` + `envelopeBytes` vs
`visibleBytes` per tool name. That measures the wrapper overhead the
projection is *already* removing, per tool. Only worth it if you want
per-tool cost attribution; the static audit already localizes the migration.

## What I did NOT do (verification boundary)

- The audit is **static/lexical**. It cannot resolve dynamic dispatch
  (`passthrough` = 77.6%), so per-tool *runtime* JSON rates are not measured —
  that's what R3 would add. The `legacy_json` classification is exact; the
  "needs JSON?" judgment for each site is mine from reading the call flow,
  not machine-verified.
- I did not change any tool behavior. Findings are reported, not fixed —
  R1 is a proposed migration for your sign-off, not started.
- The 48 `legacy_json` hits include ~23 non-model-facing uses (hashing,
  framing, sizing) that I excluded by inspection; the whitelist in R2 encodes
  that judgment but should be sanity-checked if you adopt the gate.

## Artifacts

- `scripts/audit_tool_result_formats.py` — the audit (rerunnable; `--json`
  for machine output, `--kind legacy_json` to dump sites).
- This report.

## R1 status: DONE (migrated)

`lib/swarm/integration/_tools.py` was migrated off string-shaped errors.
All 16 error paths now route through a single `_swarm_tool_error(...)`
helper that emits the **typed** shape `tool_result_error` actually parses.

Key design constraint discovered during implementation: these payloads must
NOT be wrapped in `ToolResultEnvelopeV2`. A top-level `contractVersion:
tofu.tool-result/v2` + `status` marker is detected as a model envelope by
`split_tool_result_delivery` / `budget_tool_result_v2` and *projected*,
which strips the context fields (`limit` / `requested` / `remaining_tokens`)
into the evidence sidecar — hiding them from the model. So the helper emits
the error triple (`code` / `retryable` / `next_action`) at the TOP level of a
FLAT legacy dict (the exact shape `tool_result_error`'s sparse branch
matches), with a nested `error` mirror for structural readers and the
context fields left model-visible.

Result: `nonretryable_tool_error_code` now returns real codes for swarm
failures (`execution_budget_exhausted`, `swarm_not_found`,
`agent_dependency_cycle`), so the tool-loop breaker and terminal-failure
policy can stop retrying non-retryable swarm failures instead of treating
them as prose. Success handles (`json.dumps({'status': 'ok' | ...})`) were
deliberately left as-is — only errors needed the typed contract.

Verification: 514 passed / 5 skipped across `tests/swarm/`,
`tests/tools/test_tool_loop_breaker.py`, and
`tests/agent_core/test_agent_terminal_failure_breaker.py`. New contract
tests in `TestTypedErrorContract` (test_swarm_async.py) assert the payloads
parse via `tool_result_error`, surface non-retryable codes via
`nonretryable_tool_error_code`, and keep `limit`/`requested` model-visible.
(One unrelated Node frontend test, `test_swarm_terminal_snapshot_authority`,
fails only in a bare worktree lacking `node_modules/typescript`; it is green
in the main checkout and loads only frontend `.ts`, never the migrated file.)
