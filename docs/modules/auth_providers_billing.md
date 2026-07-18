# Module Design Doc — Unit 8: Auth / Providers / Billing (`oauth/`, `byo_*`, `api_keys`, `billing/`, `pricing`, `rate_limit_*`)

> Part of the per-module design-doc set (see `docs/ARCHITECTURE.md`). This unit
> is the security-and-money boundary: BYO provider registration + egress guard,
> subscription OAuth bridging, API-key auth, per-request credit accounting, and
> rate limiting.
>
> **Grounding:** every line count is `wc -l` on disk 2026-07-11. `list_dir`
> overcounts — all numbers are `wc -l`. Every MISCUT/BIG verdict cites competing
> responsibilities or line ranges; size alone is never the argument.
>
> **The analytical payload is TRUST-BOUNDARY + SECRET-HANDLING integrity**, not
> file size: (1) is there ONE egress choke-point, and (2) is cost accounting
> single-sourced so a request can't spend without being debited?

---

## 1. Trust boundary #1 — the egress (SSRF) choke-point

The risk: a caller registers a BYO provider `base_url` and the server makes
outbound requests to it (model discovery, balance probe, chat proxy). Without a
single guard, a caller could point it at the cloud-metadata service
(`169.254.169.254`), a loopback admin port, or use it as a blind-SSRF network
oracle.

**Verdict: there IS one guard — `lib/byo_egress.py::validate_egress_url` — and
every outbound path that takes a caller-supplied URL calls it. It is enforced at
BOTH registration time AND use time (the security-critical one, defeating DNS
rebinding).** Verified by grepping every call site (only 6, all lazy-imported):

| Enforcement site | When | Path guarded |
|---|---|---|
| `byo_providers.py:269` (`_validate_base_url`) | registration | `create_provider` / `update_provider` |
| `llm_dispatch/ephemeral.py:198` | slot mint (use) | every ephemeral BYO slot |
| `llm_dispatch/discovery.py:369` | model discovery | `GET /v1/models` probe |
| `llm_dispatch/discovery.py:696` | balance probe | provider origin probe |
| `tools/tool_env.py:311` | custom-tool webhook mint | per-request custom tool |
| `tools/tool_env.py:403` | custom-tool webhook call | use-time (DNS-rebind defense) |

The design is sound (verified by reading `byo_egress.py`):
- **Resolves EVERY candidate IP** (`getaddrinfo`) and checks each — a
  single-record DNS-rebind can't slip a blocked address through. Its docstring
  explicitly mirrors `routes/upload.py::_safe_image_fetch`'s same-guarantee.
- **Always-deny** the dangerous ranges (link-local/metadata, multicast,
  reserved, unspecified) regardless of config; **loopback + RFC1918** allowed by
  default (the legitimate self-hosted vLLM/Ollama case) but lockable via
  `TOFU_BYO_BLOCK_LOOPBACK` / `TOFU_BYO_BLOCK_PRIVATE` for multi-tenant relays.
- **IPv4-mapped IPv6** (`::ffff:a.b.c.d`) is judged by the embedded v4 addr (a
  common bypass, closed).
- The docstring names the use-time check as security-critical: "DNS can change
  between registration and use, so the use-time check is the security-critical
  one" — and `discovery`/`ephemeral`/`tool_env` all re-check at use, not just at
  registration. **No path reaches the network with a caller URL without passing
  the guard.**

**Secondary secret-handling boundary — `extra_headers` allowlist.**
`byo_providers.sanitise_extra_headers` refuses a `_FORBIDDEN_EXTRA_HEADERS` set
(`authorization`, `x-api-key`, `cookie`, `host`, `proxy-authorization`, …) so a
BYO caller can't inject headers that impersonate Tofu's own outbound auth or leak
cookies. BYO api_keys are stored plaintext (the proxy needs them) but the store
is caller-scoped by `owner_key_id` and the API only ever echoes a redacted
`key_hint` (`redact()`), never the raw key. **This is a clean, single-choke-point
egress + secret boundary.**

---

## 2. Trust boundary #2 — the billing/ledger single-source seam

The risk: cost accounting diverges between the pricing tables, per-request
accounting, and the wallet/ledger, so a request spends without being debited (or
is double-debited, or the displayed ¥ disagrees with the charge).

**Verdict: cost is SINGLE-SOURCED through `lib.cost.compute_cost`, and the
reserve→settle→ledger flow is atomic + idempotent + crash-recoverable. A request
cannot spend without being debited on the billed path, and the wallet can't drift
from the ledger.** This was itself a fixed defect — the design docstrings record
the 2026-06-24 unification. Evidence:

### 2a. ONE rate engine, not two (the fixed divergence)

`billing/cost.py`'s docstring is explicit: "★ Single source of truth
(2026-06-24): the per-token RATE math lives in ONE place —
`lib.cost.compute_cost` over the rich `lib/pricing.py` table." `billing/cost.py`
is now a THIN ADAPTER that delegates rate math to `compute_cost`, converts USD→
micro-credits, and layers the relay margin. The pre-2026-06-24 state was the
defect: billing carried its OWN sparse `pricing.json` table (7 models → generic
default) AND dropped cache tokens on settle — so "a cache-heavy turn was silently
under-debited and the debited amount disagreed with the displayed cost." That is
exactly the divergence this unit was tasked to check for, and it is FIXED:
- `billing/pricing.py` per-model rate rows are now READ-ONLY legacy (rate-card
  display only); its docstring forbids reintroducing a rate writer
  (`save_margin` persists ONLY the relay margin). So there is no second driftable
  rate table.
- The SAME `compute_cost` feeds both the wallet debit (`settle_task`) and the
  user-facing ¥/$ display — "they can never drift" (verified: `settle_task`
  passes the FULL usage shape incl. cache_read/cache_write/reasoning to
  `compute_request_cost` → `compute_cost`).

### 2b. The ledger is the source of truth; the wallet is a recomputable cache

`billing/ledger.py` docstring: "The ledger is the **source of truth** … the
wallet table is a denormalized cache: `wallet.balance_micro == SUM(ledger.amount_micro)`.
If the cache ever drifts … we recompute from the ledger" (`recompute_balance`).
Append-only (no UPDATE/DELETE; a refund is a positive entry), idempotent by
`(kind, ref_type, ref_id)`, and INSERT-ledger + UPDATE-wallet live in ONE DB
transaction so the cache never lags the truth (`wallet._apply_signed` /
`settle`). PG locks the row `FOR UPDATE`; SQLite uses `BEGIN IMMEDIATE` + a
per-user lock — two debits can't race past the balance check.

### 2c. Reserve→settle makes a request unspendable-without-debit AND crash-safe

`request_flow.py` is the ONE choreography: pre-flight `reserve_for_task`
(estimate + hold, 402 if wallet can't cover) BEFORE dispatch → `settle_task`
after terminal (settle reservation against actual usage, or direct debit).
`_billing_reservation_micro` on the task dict is the single contract. A crash
mid-stream leaves a dangling reserve that `wallet_janitor.sweep_stale_reserves`
reclaims after `TOFU_BILLING_RESERVE_TTL` (30 min). Enforcement sites (grepped):
`routes/api_v1/chat.py` + `agent_run.py` both route through `request_flow`, so
the two surfaces can't drift the reserve/settle contract.

### 2d. The one honest caveat — billing is a GATED path, not universal

`request_flow` every helper short-circuits to a no-op when `user_id` is empty OR
`billing_enabled()` is false. So on a **personal/private/open install** (no owning
user, or agent-only mode where users BYO their own keys) ZERO ledger calls
happen — by design. "A request can spend without being debited" is TRUE for those
installs, but that is correct: there is no wallet to debit and no relay margin to
collect. The billed path (multi-user relay with `billing_enabled`) is the one
where the invariant matters, and there it holds. The gate is symmetric
(reserve and settle both check `billing_enabled`) so a deployment can't flip
billing off and strand a reservation. **Documented as a deliberate gate, not a
bypass bug.**

### 2e. Two cost surfaces, ONE engine (the Unit-6 echo)

Note the parallel to Unit 6's composite-reader finding: there are TWO price
*tables* touched — `lib/pricing.py` (the authoritative 100+ model rate table, the
cost engine's source) and `billing/pricing.py` (`pricing.json`, now margin-only +
legacy rate-card display). But unlike Unit 6, this is NOT a live duplication: the
`billing/pricing.py` rate rows are explicitly demoted to read-only display and
have no writer, so they cannot drift into the cost path. One ENGINE
(`lib.cost.compute_cost`), one authoritative TABLE (`lib/pricing.py`).

---

## 3. Module inventory (real `wc -l`, size verdict, status, tests)

### 3.1 BYO / egress / providers

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `byo_providers.py` | 463 | OK | HOT | `test_byo_providers`, `test_api_v1_byo_surface_polish` |
| `byo_resolve.py` | 154 | OK | HOT | via agent_run/chat e2e |
| `byo_egress.py` | 144 | OK (the guard) | HOT | `test_byo_egress` |
| `auth_sources.py` | 399 | OK | live (search auth) | `test_auth_sources_xhs` |

All OK. `byo_egress` is the single-purpose SSRF guard (§1). `byo_resolve` is
itself a DE-duplication (docstring: the resolve/dispose dance "used to be
duplicated (subtly divergently) across `agent_run.py` and `chat.py`" + the compat
adapters had none — centralised here). `byo_providers` is the caller-scoped store
+ the `extra_headers` allowlist. `auth_sources` is the authenticated-fetch source
store (xhs cookies for `search_bridge`).

### 3.2 OAuth (`lib/oauth/`, 2019 LOC)

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `codex.py` | 651 | **BIG** | live | `test_oauth_exchange_errors` |
| `manager.py` | 541 | **BIG** | live | `test_oauth_exchange_errors` |
| `claude.py` | 374 | OK | live | `test_oauth_outbound` |
| `outbound.py` | 282 | OK | HOT (per-request) | `test_oauth_outbound` |
| `token_store.py` | 95 | OK | HOT | via oauth e2e |
| `pkce.py` | 36 | leaf | live | via oauth e2e |
| `__init__.py` | 40 | OK (facade) | — | — |

`codex.py` — **BIG, bundles 2 concerns:** the OAuth token lifecycle (login/
refresh/`codex_get_valid_token`) AND the Responses-API body translation
(`codex_translate_request` — OpenAI chat-completions → ChatGPT Codex Responses
shape). The translation is a separable concern (it's request-shape mapping, not
auth). Split candidate: `codex_translate.py`. Cited by concern, not size.

`manager.py` — **BIG.** The OAuth flow orchestration (PKCE authorize → callback →
exchange → store) for both providers + subscription-plan detection. Cohesive-ish
(all "run the 3-legged OAuth dance") but at the split threshold; defer.

`outbound.py` — OK, and security-critical: it holds the subscription-token
identity spec in ONE place (Claude `x-api-key` + mandatory identity system block
+ betas; Codex `originator`/`User-Agent`/`chatgpt-account-id`). The docstring
documents each header requirement against the upstream's 401/403 behavior. A
subscription token is resolved LIVE per request (expires hourly) — never cached
in a slot. Correctly bounded.

### 3.3 Billing (`lib/billing/`, 2748 LOC)

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `wallet.py` | 364 | OK | HOT | `test_billing`, `test_billing_phase2` |
| `users.py` | 284 | OK | HOT | `test_billing_phase2` |
| `pricing.py` | 271 | OK (margin + legacy) | live | `test_billing` |
| `payments/stripe.py` | 269 | OK | live | `test_billing_phase2` |
| `payments/_common.py` | 256 | OK | live | `test_billing_phase2` |
| `janitor.py` | 216 | OK | live (timer) | `test_billing_janitor` |
| `ledger.py` | 199 | OK (SoT) | HOT | `test_billing` |
| `payments/alipay.py` | 194 | OK | live | `test_billing_phase2` |
| `cost.py` | 194 | OK (thin adapter) | HOT | `test_cost_estimator`, `test_api_billing_terminal_settle` |
| `request_flow.py` | 180 | OK (choreography) | HOT | `test_api_billing_terminal_settle`, `test_relay_billing_gate` |
| `wallet_janitor.py` | 164 | OK | live (timer) | `test_billing_janitor` |
| `__init__.py` | 104 | OK (facade) | — | — |

**The entire billing package is well-decomposed** — each module is a single clean
layer (pricing→cost→ledger→wallet→users, choreographed by request_flow, swept by
two janitors). No file exceeds 364 lines; the layering matches the `__init__`
docstring's stated design exactly. This is a reference-quality package like
`token_counter/` (Unit 5). The `payments/` sub-package cleanly isolates the two
processors (stripe/alipay) behind `_common`.

### 3.4 Keys, cost engine, rate limiting (top-level)

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `key_stats.py` | 741 | **BIG** | HOT | via dispatch e2e |
| `api_keys.py` | 553 | **BIG** | HOT | `test_api_keys` |
| `pricing.py` (top-level) | 440 | **BIG (data)** | HOT | `test_cost` |
| `cost.py` | 288 | OK (the single engine) | HOT | `test_cost`, `test_cost_estimator` |
| `rate_limit_api.py` | 288 | OK | HOT | `test_rate_limit_api` |
| `rate_limit_store.py` | 250 | OK | HOT | `test_rate_limit_store` |
| `rate_limiter.py` | 72 | OK (wrapper) | HOT | `test_rate_limit_store` |

`key_stats.py` — **BIG (741).** Per-key daily health tracking (success/error/
truncation/latency EMA, the data feeding `Slot.record_*` from Unit 2). Bundles
the stats store + the aggregation/reporting + the daily-rollover. Split candidate
(store vs reporting) but cohesive; defer.

`api_keys.py` — **BIG (553).** The API-key auth store: key CRUD + hashing/verify +
scope/permission checks + the auth-context resolution middleware consumes. This is
a genuine trust boundary; it's large because it's the auth surface. Cohesive but
at threshold — a split (store vs verify/scope) is plausible; defer.

`pricing.py` (top-level, 440) — **BIG but ~90% DATA** (the `MODEL_PRICING` table
of 100+ models + `QWEN_PRICING_CNY` tiers + `PROVIDER_PRICING` overrides). The
logic (`lookup_pricing`, the online-refresh fetchers) is compact. Data-heavy, not
miscut. Do NOT split (like `llm_dispatch/config.py` in Unit 2).

`cost.py` (288) — OK, and it is THE single cost engine (§2a): `compute_cost` reads
`lib/pricing.py` with cache-convention detection (Anthropic vs OpenAI), Qwen CNY
tiers, provider overrides, live USD→CNY. Both billing AND the UI display go
through it. Correctly bounded and load-bearing.

`rate_limit_*` — all OK. `rate_limiter` is a thin wrapper over `rate_limit_store`
(the pluggable memory/DB backend, `record_and_check`); `rate_limit_api` is the
`@rate_limit` decorator + endpoint policy. Clean 3-file split (the memory
documents it was extracted for multi-worker correctness).

---

## 4. Dependencies (in / out)

**Inbound:** `routes/api_v1/providers.py` (BYO CRUD), `routes/api_v1/oauth.py`
(subscription login), `routes/api_v1/billing.py` + `keys.py` (wallet/keys admin),
`routes/api_v1/chat.py` + `agent_run.py` (the billed completion path → `request_flow`),
the auth middleware (`api_keys` → `g.auth_ctx`), every route via `@rate_limit`.

**Key internal edges:**
- egress: `byo_providers` / `ephemeral` / `discovery` / `tool_env` →
  `byo_egress.validate_egress_url` (the single guard, §1).
- cost: `billing/cost` → `lib.cost.compute_cost` → `lib/pricing.py`
  (the single engine + single table, §2a/§2e).
- billing flow: `request_flow` → `billing.{reserve,settle,estimate_request_cost,
  compute_request_cost}` → `wallet` → `ledger` → `lib/database` (DOMAIN_SYSTEM).
- oauth: `llm._sse_core.prepare_request` / `llm.chat` → `oauth.outbound.resolve_oauth_request`
  (live token per request when a slot is `oauth=`) → `oauth.{claude,codex}.get_valid_token`.
- `oauth.outbound.provision_oauth_provider` → `server_config.json` (registers the
  subscription as a managed dispatch provider).

**Outbound:** `lib/database`, `lib/json_store` (atomic config writes),
`lib/http_client` (pricing/exchange fetch, proxied), `lib/config_dir`.
`byo_egress` uses stdlib `socket`/`ipaddress` only. **No back-edges up into
routes** — this is a service layer consumed by routes.

---

## 5. Invariants (must not be broken by a refactor)

1. **`byo_egress.validate_egress_url` is the SINGLE egress guard** and MUST be
   called at USE time (not only registration) on every path that fetches a
   caller-supplied URL (§1). It resolves ALL candidate IPs (DNS-rebind defense).
2. **`_FORBIDDEN_EXTRA_HEADERS` blocks auth-impersonation headers** on BYO
   providers; BYO api_keys are never echoed raw (only `key_hint`).
3. **`lib.cost.compute_cost` is the SINGLE rate engine** — the wallet debit and
   the displayed ¥/$ MUST both go through it (the 2026-06-24 unification). Do NOT
   reintroduce a rate table/writer in `billing/pricing.py` (margin-only there).
4. **The ledger is append-only + the source of truth**; the wallet is a
   recomputable cache. INSERT-ledger + UPDATE-wallet are ONE transaction; every
   movement is idempotent by `(kind, ref_type, ref_id)`.
5. **Every billable request is reserve→settle** (`request_flow`), crash-recovered
   by `wallet_janitor`. `settle_task` forwards the FULL usage shape (incl. cache/
   reasoning tokens) so the debit matches the displayed cost.
6. **Billing is gated by `user_id` AND `billing_enabled()`** — symmetric across
   reserve/settle so personal/agent-only installs make ZERO ledger calls and a
   deployment can flip billing off without stranding a reservation (§2d).
7. **Subscription OAuth tokens are resolved LIVE per request** (`outbound.resolve_oauth_request`),
   never cached in a slot (they expire hourly); the identity headers + Claude
   system-block are mandatory (401/403 upstream otherwise).
8. **Rate-limit store fails OPEN** (returns allowed on a missing table / SQL
   error) — a rate limiter must never take down the server.
9. **Auth-key / margin / rate-limit caps are §10 config** — sign-off to change.

---

## 6. Known debt (grounded)

- **`oauth/codex.py` (651) bundles token lifecycle + Responses-API body
  translation** (§3.2) — a clean split seam (`codex_translate.py`).
- **`key_stats.py` (741)** and **`api_keys.py` (553)** are BIG — plausible
  store-vs-reporting / store-vs-verify splits, but cohesive; defer.
- **`oauth/manager.py` (541)** — BIG OAuth-flow orchestration; defer.
- **Two price tables exist** (`lib/pricing.py` authoritative + `billing/pricing.json`
  margin+legacy-display) — NOT a live duplication (the billing rows are read-only,
  no writer), but a reader should know only `lib/pricing.py` feeds cost (§2e).
- No egress or billing bypass found — the two trust boundaries this unit exists to
  protect are single-sourced and enforced.

---

## 7. Segmentation verdict (this unit)

**Correctly bounded — leave as-is:**
`byo_egress` (the single guard), `byo_resolve` (itself a de-dup), `byo_providers`,
`auth_sources`, ALL of `billing/` (reference-quality layered package),
`oauth/{claude,outbound,token_store,pkce}`, `cost.py` (the single engine),
all three `rate_limit_*` modules.

**Miscut — should split:**
1. **`oauth/codex.py` (651) → extract `oauth/codex_translate.py`** for the
   Responses-API body translation (`codex_translate_request` + its shape
   helpers), leaving the token lifecycle in `codex.py`. The translation is
   request-shape mapping, not auth — a clean concern boundary. Behind
   `test_oauth_exchange_errors` + the codex-path e2e.

**Big but optional (defer unless touched):**
`key_stats.py` (741 — store vs reporting), `api_keys.py` (553 — store vs verify/
scope), `oauth/manager.py` (541 — flow orchestration).

**Do NOT split:** `lib/pricing.py` (440, ~90% data table — like `llm_dispatch/config.py`),
`cost.py` (the single engine), the `billing/` layer modules (each already a clean
single concern).

---

## 8. Comparison to Units 1–7 (the running thesis)

- **Both trust boundaries this unit was tasked to check are SINGLE-SOURCED and
  enforced** — the highest-consequence outcomes (SSRF, credential leak, spending
  without debit) are structurally guarded: one egress choke-point re-checked at
  use time, one cost engine feeding both debit and display, an append-only ledger
  the wallet can't drift from.
- **The billing divergence was a REAL past defect, now fixed** (the 2026-06-24
  single-engine unification — two rate tables that under-debited cache-heavy
  turns). This is the same "convert a symptom-patch into a single-source
  mechanism" program seen in Unit 7's `_CRITICAL_COLUMNS` fix — the codebase
  actively collapsed the duplication rather than papering over it.
- **`billing/` is a fourth reference-quality package** (with `swarm/`,
  `token_counter/`, and the `compaction/` split) — clean layered decomposition,
  nothing oversized. The security-and-money boundary is, encouragingly, among the
  BEST-organized code in the tree.
- **The only real miscut is `oauth/codex.py`** (auth + body-translation in one
  file) — a concern-boundary split, modest and low-risk, in the same family as
  Unit 3's `tool_env` misplacement (a cohesive-but-mislocated concern) rather than
  the `manager.py`/`_core.py` five-concerns-in-one giants.

---

*Next unit: Unit 9 (Infra / runtime — `log`, `runtime_state_store`, `cross_dc`,
`fs_keepalive`, `self_update`, `agent_core/`).*
