# Tofu — Development Direction

> **Status:** Direction-setting document. No code changes are proposed here;
> see `docs/refactor_decomposition_proposal.md` for plan-only refactor splits
> and `docs/SECURITY_AUDIT_REPORT.md` / `docs/RATE_LIMITING_DOS_AUDIT_REPORT.md`
> for already-scoped hardening work. Every item that touches CLAUDE.md §10
> gated areas (hyperparameters, model routing, DB schema, security-sensitive
> code) is tagged and requires explicit user approval before execution.
>
> **Author:** Worker (endpoint mode) • **Date:** 2026-04-21 • **Version baseline:** 0.9.2 (`CHANGELOG.md`)
>
> **Also note:** `ROADMAP.md` at the repo root has been rewritten as a thin
> pointer to this document (during Phase A). Treat this file as the
> canonical source; `ROADMAP.md` is a navigation stub only.

---

## 1. Executive Summary

Tofu is a mature self-hosted AI assistant (v0.9.2). Its agentic core —
layered compaction, streaming tool accumulator, endpoint Planner→Worker→Critic,
streaming DAG swarm, cross-session skills/memory — is already at or ahead of
Claude Code in most orchestration dimensions
(`docs/agentic-development-experience.md §5`,
`docs/omc-claude-code-backport-analysis.md §5`). What now limits the project
is not agent cleverness but **surface sprawl** (13 oversized modules per
`docs/refactor_inventory.md §7`, four overlapping `trading*` packages per §8a),
**upstream reliability** (`a.md` documents 312 gateway 502s and 52
premature-SSE-closes in one day, all against `aws.claude-opus-4.7`), and
**security posture still at 4/10** (`docs/SECURITY_AUDIT_REPORT.md` — auth
stub, hardcoded Flask key, `shell=True` in scheduler/desktop_agent, SVG XSS,
in-memory rate limiter).

**Top-3 functionality bets.** (1) **TodoWriteTool + continuation enforcer**
(the single highest-leverage backport — see
`docs/agentic-development-experience.md §5 Backlog` and
`docs/omc-claude-code-backport-analysis.md Rec 1/2`). (2) **Paper Reader
maturation** (still tagged beta in `README.md §Paper Reader`; the 9-section
report pipeline is the most complex single product feature and routes/paper.py
is 2547 LOC). (3) **Project Co-Pilot hardening** (Tofu's most-used capability
end-to-end; biggest remaining source of `run_command` timeouts in
`logs/error.log` — 25 hits in the captured window).

**Top-3 robustness bets.** (1) **Upstream-gateway resilience** — absorb the
opus-4.7 premature-close pattern from `a.md §3` into per-upstream health
scoring inside `lib/llm_dispatch/`. (2) **Security P0 closeout** — Feishu
`APP_SECRET` rotation, Flask `secret_key` randomisation, SVG upload removal,
browser/desktop bridge auth (`docs/SECURITY_AUDIT_REPORT.md §P0`). (3)
**Code-health Phase P1** — execute the already-approved-in-principle
`compaction.py` split per `docs/refactor_decomposition_proposal.md §P1`
(2620 LOC, best test coverage, lowest risk).

---

## 2. Functionality Direction

### 2.1 Strengthen (mature existing capabilities)

| # | Item | Intent | Rationale | Effort | §10 gate |
|---|------|--------|-----------|--------|----------|
| 2.1.1 | **Endpoint mode Critic discipline** | Adopt "run, don't read" + evidence-format + anti-rationalization prompts | `omc-claude-code-backport-analysis.md §Rec 3` — Critic currently can skip verification; false STOPs still require the `_parse_verdict` defense-in-depth guard in `endpoint_review.py` | S | §10.1 (prompt = hyperparameter-adjacent) |
| 2.1.2 | **Swarm 6-section delegation template** | Replace free-form `SubTaskSpec.objective` with Task/Expected Outcome/Required Tools/Must Do/Must Not Do/Context | `omc-claude-code-backport-analysis.md §Rec 4` — reduces scope drift across the 20-file `lib/swarm/` package | M | no |
| 2.1.3 | **Paper Reader GA** | Remove beta label on Paper Reader (`README.md §Paper Reader`), cover the Q&A + 9-section report pipeline with regression tests for arxiv edge cases | `routes/paper.py` is 2547 LOC with no dedicated test file in `tests/`; report is §13-sensitive (`max_tokens=128000` contract must not regress) | M | no |
| 2.1.4 | **Daily Report / My Day** | Stabilise cost-heatmap, calendar aggregation, TODO carry-over; add PG-vs-SQLite smoke coverage | `routes/daily_report.py` has grown to 2938 LOC (larger than the 2669 measured in `refactor_inventory.md §7`) and has no dedicated test file | M | no |
| 2.1.5 | **Project Co-Pilot ergonomics** | Tighten `run_command` interactive detection, lengthen the 60 s default timeout only via config (not hardcoded), add per-project undo-log export | 25 hits for `run_command timed out after 60s (interactive)` in the captured `logs/error.log` window; `lib/compat.py` already acknowledges interactive detection is Linux-only | S | §10.1 (timeout default) |
| 2.1.6 | **MCP server curation** | Keep expanding the curated registry (Overleaf shipped in 0.9.2 — `CHANGELOG.md`); add one-click auth helpers for tokens | `routes/mcp.py` is already 517 LOC; MCP surface is the cheapest way to extend tool reach | S | no |
| 2.1.7 | **Browser extension** | Keep parity with Chrome Manifest V3 upgrades; add a session-keep-alive health probe so the bridge doesn't silently stall | `routes/browser.py` is a known auth gap (see §3.2); usage grows → resilience matters | S | §10.4 when auth lands |

### 2.2 Extend (new capabilities)

| # | Item | Intent | Rationale | Effort | §10 gate |
|---|------|--------|-----------|--------|----------|
| 2.2.1 | **`todo_write` tool + structured `task['_todos']`** | Machine-readable checklist; UI panel renders progress | Highest-leverage Backlog item in `docs/agentic-development-experience.md §5` and Rec 1 of `omc-claude-code-backport-analysis.md`; unlocks 2.2.2 | M | no |
| 2.2.2 | **Continuation enforcer hook** | Injects "incomplete todos" reminder on `finish_reason='stop'` when todos remain | Saves a full Critic turn per premature stop; leverages existing `lib/tasks_pkg/tool_hooks.py` infra | S | no (depends on 2.2.1) |
| 2.2.3 | **Planner write-block hook** | During endpoint Planner phase, block `write_file` / `apply_diff` / mutating `run_command` via pre-hook | Planner prompt says "plan, don't execute" but tool access is unrestricted today; see `omc-claude-code-backport-analysis.md Rec 5` and `agentic-development-experience.md §5 Backlog` | S | §10.4 (tool policy) |
| 2.2.4 | **Speculation / overlay** | Parallel "speculative" branch rendering in the chat UI, inspired by Claude Code's branch speculation | `agentic-development-experience.md §5 Backlog` lists this as 🔜; pure UX addition, no server changes | L | no (until backend support) |
| 2.2.5 | **Richer conversation export** | Markdown + HTML + JSON exports including attachments and tool-result previews | Exists partially inside `static/js/ui.js` but inconsistent across modes (paper/translate/my_day) | S | no |
| 2.2.6 | **MT provider expansion** | Add DeepL / Volcengine / Baidu MT to `lib/mt_provider.py` behind the same fallback contract | `README.md §Machine Translation` currently ships NiuTrans + Custom only | S | no |
| 2.2.7 | **CLI backend parity gaps** | Close remaining feature asymmetries in Claude Code / Codex backends documented in `README.md §CLI Backend Switching` (image gen, browser tools marked ❌) | `lib/agent_backends/` already has the abstraction; just wire feature flags through | M | no |
| 2.2.8 | **Mobile polish** | Build on existing tofu-theme mobile work (memories `mobile-tofu-theme-css-specificity-war`, `mobile-keyboard-dismiss-scroll-jump-fix`) — add install-as-PWA manifest, refine swarm / endpoint panels | Mobile is a differentiator vs. most self-hosted assistants | M | no |

### 2.3 Consolidate / de-emphasize

| # | Item | Intent | Rationale | Effort | §10 gate |
|---|------|--------|-----------|--------|----------|
| 2.3.1 | **Trading package consolidation** | Unify `lib/trading/`, `lib/trading_autopilot/`, `lib/trading_backtest_engine/`, `lib/trading_strategy_engine/` under one `lib/trading/` with submodules; routes count: `routes/trading_*.py` × 6 | `refactor_inventory.md §8a` explicitly flags "large reorganization affecting many import sites — ask first"; default-off via `TRADING_ENABLED=1` | L | §10 (user approval explicitly required) |
| 2.3.2 | **Trading as plugin** (alternative to 2.3.1) | Make trading an optional plugin directory loaded by feature flag, not a first-class module set | Better match for the open-source posture (CLAUDE.md §11 already strips trading-on in exports); keeps the core lean | L | §10 |
| 2.3.3 | **Remove `niumark/` and `swebench_full/` stubs** | Both appear as empty directories in repo root; benchmarks live in `swebench_workdir/` and `benchmarks/` | Housekeeping; trivially verifiable | S | no |
| 2.3.4 | **Scratch files cleanup** | `a.md`, `thesis_references.bib`, `fix_openreview_latex.py` at root are utility/one-off artefacts that belong in `debug/` or `docs/` | `export.py` already strips `a.md` in internal/opensource modes (CLAUDE.md §11.2) — decide on canonical home | S | no |

**Coverage check (Acceptance Criteria 1 + 3):** Every top-level capability in
`README.md §Features` — chat, web search, tools/agents, project co-pilot,
swarm, MT, CLI backend switching, browser extension, desktop agent, paper
reader, image gen, MCP, daily reports / My Day, scheduled tasks, Feishu bot,
memory, conversation branching — appears under 2.1/2.2/2.3.

---

## 3. Robustness Direction

### 3.1 Reliability

Recurring signatures in `logs/error.log` (4489 lines in current slice):

| Signature | Count (captured window) | Source |
|---|---|---|
| `PREFIX MUTATION DETECTED … This will cause a cache miss` | **140** | `lib/tasks_pkg/cache_tracking.py` |
| `429 rate-limited on example-corp_key_0:…` | **187** | `lib/llm_dispatch/api.py` |
| `NoneType` / `AttributeError` / `Traceback` | **126** | various |
| `⚠ PREMATURE STREAM CLOSE: Server never sent [DONE]` | **17** | `lib/llm_client.py` |
| `run_command timed out after 60s (interactive)` | **25** | `lib/project_mod/tools.py` |
| `SSE stream … DISCONNECTED PREMATURELY` | **4** | `routes/chat.py` |

Combined with `a.md §§2–3` (312 × gateway 502 bursts + 52 × zero-byte SSE
close, 50/52 on `aws.claude-opus-4.7`, 10-ish-second elapsed suggesting an
upstream `proxy_read_timeout` of ~10 s), these are the actionable items:

| # | Item | Intent | Rationale | Effort | §10 gate |
|---|------|--------|-----------|--------|----------|
| 3.1.1 | **Per-upstream health scoring** | In `lib/llm_dispatch/`, cool down slot/upstream pairs on repeated premature-close (not just 5xx) — use the `_premature_retry_count` signal already surfaced by `orchestrator.py` | `a.md §3.4` rule #3 — 50/52 premature closes landed on a single upstream (`aws.claude-opus-4.7`); current dispatcher rotates on 5xx (memory `gateway-5xx-treated-as-429`) but not on `[DONE]`-absence | M | §10.2 (dispatch routing) |
| 3.1.2 | **Reactive-compact maturity** | Verify the 2-retry cap + cooldown still holds for `PromptTooLong` (memory `http-413-prompt-too-long-and-429-cap`); add unit test for the `_reactive_compact_attempts` map not leaking across conversations | Only 33 test files in `tests/`, no dedicated `test_reactive_compact.py` | S | no |
| 3.1.3 | **Streaming retry semantics** | Confirm `stream-retry-cap-split-by-signature` memory is still enforced (zero-byte: 16, classic: 2) and add CI test for the WHILE-loop cap extension in `orchestrator.py` | Retry cap drift would silently double cost; the WHILE loop (not FOR) per `agentic-development-experience.md §3.1` is load-bearing | S | §10.1 (retry caps) |
| 3.1.4 | **Tool-timeout circuit breaker** | Already present (`_MAX_CONSECUTIVE_TOOL_TIMEOUTS=3` — see `agentic-development-experience.md §3.4`); extend to per-tool telemetry so a flaky `fetch_url` doesn't kill an otherwise healthy task | Today the breaker trips the whole task; a per-tool breaker keeps progress on unrelated tools | M | §10.1 |
| 3.1.5 | **Crash-recovery checkpoint coverage** | Every 5 s throttled checkpoint exists (`agentic-development-experience.md §3.4`). Add a server-startup sweep that resurrects `status='running'` tasks older than N min into `crashed` state with the last checkpoint preserved | Prevents "orphan task" log lines observed in the error window (`routes.common: Recovering orphan task …`) | M | §10.3 if new column is needed |
| 3.1.6 | **Background-thread safety** | `bootstrap` CLAUDE.md §5.4 requires all daemon loops wrap in `try/except`; sweep `lib/swarm/scheduler.py`, `lib/scheduler/`, `lib/tasks_pkg/session_memory.py` for compliance | 126 tracebacks in error.log include at least one `LLM call failed at round 2 … SSE error:` that bubbled up from a bg thread | S | no |
| 3.1.7 | **Prefix-mutation residual** | 140 `PREFIX MUTATION DETECTED` warnings in window — root cause per memory `prefix-mutation-detection-bug-fix` is largely diagnostic, but clustered `swebench` conv ids suggest a real cache-break path in long tool runs | M — investigate before tuning | §10.1 if any constants move |

### 3.2 Security (open items from `docs/SECURITY_AUDIT_REPORT.md`)

Only items the audit marked unresolved are surfaced. Status column
distinguishes (a) already-resolved → **not listed here**, (b) flagged-and-open,
(c) newly-observed.

| # | Item | Intent | Rationale | Effort | §10 gate |
|---|------|--------|-----------|--------|----------|
| 3.2.1 | **Feishu `APP_SECRET` rotation** | Remove the hardcoded fallback literal; fail-closed when env vars missing | `SECURITY_AUDIT_REPORT.md L1 / P0` — `lib/feishu_bot.py:43-44` still has a real-looking secret literal, which is the highest-severity leak if repo is shared | S | §10.4 + §11 (`export.py _SECRETS`) |
| 3.2.2 | **Random Flask `secret_key`** | Replace `'not-needed-single-user'` with `os.environ.get('FLASK_SECRET_KEY') or os.urandom(32).hex()` | `SECURITY_AUDIT_REPORT.md L2 / P1` — `server.py:179`; known key = session forgeability when multi-user arrives | S | §10.4 |
| 3.2.3 | **Browser / Desktop bridge auth** | `X-Bridge-Secret` header + CORS allowlist | `SECURITY_AUDIT_REPORT.md A3–A4 / P0` — `/api/browser/poll` and `/api/desktop/poll` have neither auth nor origin check | M | §10.4 |
| 3.2.4 | **SVG upload removal** | Drop `.svg` from allowed image extensions; add `imghdr` magic-bytes check | `SECURITY_AUDIT_REPORT.md D3 / P0` — `routes/common.py:238` ext-only check enables stored XSS | S | §10.4 |
| 3.2.5 | **Harden `DANGEROUS_PATTERNS` or switch to whitelist** | Three `shell=True` sites: `lib/scheduler.py:328`, `lib/project_mod/tools.py:437`, `lib/desktop_agent.py:155` | `SECURITY_AUDIT_REPORT.md I1–I3 / P1` — blacklist easily bypassed; `python -c`, `curl | sh`, `find / -delete` patterns missing | M | §10.4 |
| 3.2.6 | **Uniform error envelope** | One `@app.errorhandler(500)` that logs `exc_info=True` but returns a generic body; sweep 10+ `return jsonify({'error': str(e)})` sites | `SECURITY_AUDIT_REPORT.md L3 / P1` | S | no |
| 3.2.7 | **Tunnel-token external-access guard** | When `request.remote_addr` is not in the private-network set and `TUNNEL_TOKEN` is empty → `abort(403)` with a warning log | `SECURITY_AUDIT_REPORT.md A2` — current default silently disables auth | S | §10.4 |

### 3.3 DoS / Rate-Limiting (remaining items from `docs/RATE_LIMITING_DOS_AUDIT_REPORT.md`)

The audit explicitly lists six "Recommendations for Future Enhancement":

| # | Item | Intent | Rationale | Effort | §10 gate |
|---|------|--------|-----------|--------|----------|
| 3.3.1 | **Persistent rate-limit store** | Back `lib/rate_limiter.py` with the existing `lib/database/` (PG or SQLite) so multi-worker Gunicorn deployments share counters | `RATE_LIMITING_DOS_AUDIT_REPORT.md §Rec 1` | M | §10.3 (new table) |
| 3.3.2 | **Per-user limits** | Key by `TUNNEL_TOKEN` identity when auth is enabled, fall back to IP | `§Rec 2`; depends on 3.2.7 | S | §10.4 |
| 3.3.3 | **`X-Forwarded-For` handling** | Trust only configured proxy hops; the audit warns today's `request.remote_addr` collapses all users behind a reverse proxy to one bucket | `§Rec 6` + `SECURITY_AUDIT_REPORT.md D1` | S | §10.4 |
| 3.3.4 | **Rate-limit response headers** | `X-RateLimit-Limit/Remaining/Reset` | `§Rec 4` — makes client-side backoff implementable | S | no |
| 3.3.5 | **Global fallback limiter** | Per-IP 120 req/min `@app.before_request` cap for any un-annotated route (endpoints still without `@rate_limit`: `routes/common.py` uploads, `routes/memory.py`, scheduler CRUD, trading intel/decision, desktop, MCP) | `§Rec` + audit's "Endpoints NOT Rate-Limited" list | M | §10.1 (new default cap) |
| 3.3.6 | **Monitoring / whitelist** | Log 429 counts to `logs/audit.log`; allow-list for loopback + private CIDR | `§Rec 5` + `§Rec 6` | S | no |

### 3.4 Observability & Ops

| # | Item | Intent | Rationale | Effort | §10 gate |
|---|------|--------|-----------|--------|----------|
| 3.4.1 | **Audit-log coverage expansion** | Add `audit_log()` calls for model-switch, rate-limit-violation, bridge-auth-fail, config-change, task-abort | `CLAUDE.md §2.5` mandates audit for "significant state changes"; present coverage is patchy | S | no |
| 3.4.2 | **Error-log triage tool** | A `debug/triage_errors.py` that clusters `logs/error.log` by signature (PREMATURE STREAM, PREFIX MUTATION, run_command timeout, 429, Traceback) and prints top-N | The 4489-line log is readable but clustering is manual today | S | no |
| 3.4.3 | **`healthcheck.py` expansion** | Cover: PG userspace boot, SQLite file presence, DB migrations current, MCP subprocesses alive, at least one LLM slot reachable, rate-limiter store reachable | Current `healthcheck.py` is 542 LOC; exit codes drive Docker/CI gating | M | no |
| 3.4.4 | **Cache-break telemetry** | Surface `cache_read_tokens` vs. `cache_creation_tokens` in daily-report cost heatmap; dashboards for BP4 advancement | Ties into the `cache-optimization-improvements-2026-04` A/B lineage | M | no |
| 3.4.5 | **Swarm per-agent cost tracking** | Persist per-`SubAgent` usage so swarm master-review decisions can use real cost, not just token count | `lib/swarm/` has 18 files; cost shows up in aggregates today | M | §10.3 (new column) |
| 3.4.6 | **Cross-DC latency regression test** | Add a `debug/` smoke script that exercises `lib/cross_dc.py` classifier under simulated latency | `CLAUDE.md §3.5` explicitly builds on auto-detection; regression hedge is cheap | S | no |

### 3.5 Code-Health / Refactor

Execute the plan-only splits from `docs/refactor_decomposition_proposal.md`
**in priority order** — every step is §10-gated because the fat modules touch
token budgets, retry caps, and model routing:

| Phase | Target | LOC (per `refactor_inventory.md §7` / live count) | Risk | §10 gate |
|-------|--------|----------|------|----------|
| **P1** | `lib/tasks_pkg/compaction.py` | 2620 | **Low** — excellent test coverage (`tests/test_compaction_improvements.py`, `test_keep_tool_history_*`) | §10.1 (token budgets) |
| **P2** | `routes/paper.py` → `lib/paper/` | 2547 | Medium — §13 `max_tokens=128000` contract must be preserved | §10.1 |
| **P2** | `routes/daily_report.py` → `lib/daily_report/` | **2938** (live) — grown past the 2669 in the inventory | Medium — scheduler integration ordering | no (unless schema) |
| **P3** | `routes/chat.py` | 2172 | Medium-high — auth/session ordering (`chat_bp` name must stay) | §10.4 |
| **P3** | `lib/tasks_pkg/manager.py` · `orchestrator.py` · `tool_dispatch.py` | 1848 · 1899 · 1726 | High — hot path, event-order invariants | §10.1 |
| **P3** | `lib/llm_client.py` | 3652 | **Highest** — 10+ provider adapters, retry/fallback, tool-call streaming | §10.1 / §10.2 |
| **P4** | `static/js/ui.js` · `settings.js` · `main.js` · `core.js` | 8716 · 5572 · 5472 · 3618 (live) | High — no build step, cross-script `var`, function hoisting | separate approval gate |
| **P4** | `routes/translate.py` | 1309 | Medium | no |

### 3.6 Testing

33 test files in `tests/` today. Modules that are oversized **and** have no
dedicated test file — the obvious coverage gaps:

- `routes/paper.py` (2547 LOC) — no `test_paper_*.py`
- `routes/daily_report.py` (2938 LOC) — no `test_daily_report_*.py`
- `routes/translate.py` (1309 LOC) — no `test_translate_*.py`
- `lib/llm_client.py` (3652 LOC) — coverage is indirect via
  `test_streaming_and_prefetch.py` / `test_cc_alignment.py`; no unit tests
  for `build_body` provider matrix
- `lib/mt_provider.py` — no provider-adapter smoke
- `lib/scheduler/` — covered only through `routes/scheduler.py`

**Proposed smoke matrix:**

| Axis | Values |
|---|---|
| DB backend | `CHATUI_DB_BACKEND=sqlite` · `=postgres` (both must pass — CLAUDE.md §10.3 invariant) |
| LLM provider adapter | OpenAI · Anthropic · YourProvider · Qwen · Doubao · DeepSeek · one local (vLLM/Ollama) |
| Agent backend | builtin · Claude Code · Codex (`lib/agent_backends/`) |
| OS | Linux · macOS · Windows (memory `cross-platform-compat` already codifies this) |

---

## 4. Cross-Cutting Concerns

- **Open-source sanitization (CLAUDE.md §11).** Any new capability that
  introduces a secret literal, internal endpoint, provider identifier, or
  absolute path **must** be reflected in `export.py` (`_SECRETS`,
  `_ENDPOINTS`, `_INTERNAL_DOMAIN_LITERALS`, or the path-cleaning block)
  in the same change set. Candidates from §2/§3 that will trigger updates:
  2.2.6 (new MT providers → likely new env vars), 3.2.1 (removing the
  Feishu literal → update `_SECRETS`), 3.3.1 (rate-limit table → no secrets
  but `_schema_pg.py`+`_schema_sqlite.py` dual update per §10.3).
- **Cross-platform parity (CLAUDE.md §9).** Windows CI coverage lags
  Linux; `debug/test_cross_platform.py` is a smoke, not a full gate. Items
  that interact with `lib/compat.py` (2.1.5 interactive `run_command`,
  3.1.6 background threads, 3.1.5 checkpoint resurrection) must be re-run
  on macOS + Windows before sign-off.
- **Config discipline (CLAUDE.md §3.5).** No new hardcoded hostnames,
  cluster names, or absolute paths. All new defaults should be env-driven
  with sensible fall-backs — pattern demonstrated by `lib/cross_dc.py`
  (`CROSS_DC_CLUSTER_MOUNTS`) and `lib/proxy.py` (`PROXY_BYPASS_DOMAINS`).
- **Documentation upkeep.** `ROADMAP.md` has been rewritten as a pointer
  to this document during Phase A (so drift is contained to one file
  going forward). Additionally, keep `CHANGELOG.md` current — a
  surface-level scan shows entries for 0.9.0, 0.9.1, 0.9.2 only; past
  micro-releases are consolidated under "0.9.0 — previous release".

---

## 5. Suggested Sequencing

Each phase has a **gate** (whether CLAUDE.md §10 approval is required) and
an **exit criterion** that can be objectively checked.

### Phase A — Hygiene & P0 security (approval-light)

- **Goal:** Close the highest-severity leaks and remove stale signposts.
- **Scope:** 3.2.1 (Feishu rotation), 3.2.2 (Flask key), 3.2.4 (SVG),
  3.2.6 (error envelope), 2.3.3 (empty dirs), ROADMAP.md cleanup, 3.4.1
  (audit coverage), 3.4.2 (triage tool).
- **Gate:** §10.4 on 3.2.1/3.2.2/3.2.4 + §11 export.py sync.
- **Exit:** `python3 export.py --mode opensource --dry-run`'s built-in leak
  scan reports 0 findings; `git grep 'not-needed-single-user'` returns empty.

### Phase B — Compaction split + Todo tool + Reactive-compact hardening

- **Goal:** Make the most-cited oversized module testable in pieces; land
  the highest-leverage agentic backport.
- **Scope:** P1 of `refactor_decomposition_proposal.md` (compaction),
  2.2.1 (todo_write), 2.2.2 (continuation enforcer), 3.1.2 (reactive
  compact coverage), 3.1.3 (retry-cap test).
- **Gate:** §10.1 on compaction (token budgets preserved, not tuned).
- **Exit:** `tests/test_compaction_improvements.py` + new
  `tests/test_todo_tool.py` green; `tests/test_keep_tool_history_*` green;
  a manual endpoint run shows `[SYSTEM: TODO CONTINUATION REQUIRED]`
  firing when expected.

### Phase C — Routes splits + persistent rate-limiter + bridge auth

- **Goal:** Slim the two largest route files; close bridge P0; make rate
  limiting production-shaped.
- **Scope:** P2 of the proposal (paper, daily_report), 3.2.3 (bridge
  auth), 3.2.5 (shell hardening), 3.3.1 (persistent store), 3.3.3
  (X-Forwarded-For), 3.3.4 (headers), 3.3.5 (global limiter).
- **Gate:** §10.3 on 3.3.1 (new table in both `_schema_pg.py` and
  `_schema_sqlite.py`); §10.4 on 3.2.3/3.2.5.
- **Exit:** All paper / daily_report routes resolve to thin handlers in
  `routes/`; under a 2-worker Gunicorn, rate-limit counters are shared;
  CORS + `X-Bridge-Secret` enforced.

### Phase D — `tasks_pkg` monoliths + `llm_client` + frontend (separate approval)

- **Goal:** Split the hottest paths, with the highest-coverage test matrix
  already in place from Phases A–C.
- **Scope:** P3 of the proposal (manager, orchestrator, tool_dispatch,
  llm_client), then P4 (`static/js/*`).
- **Gate:** §10.1 + §10.2 across the board; frontend is a **separate**
  approval session per the proposal's P4 note.
- **Exit:** Test suite green on both PG and SQLite; `tests/test_cache_breakpoints.py`
  unchanged in outcome; streaming-tool-accumulator behavior byte-identical
  under the same seed on the mock LLM server (`tests/mock_llm_server.py`).

### Phase E — New capabilities

- **Goal:** Ship the "Extend" bucket, now on a de-risked code base.
- **Scope:** 2.1.1 (Critic prompt — §10.1 approval), 2.1.2 (swarm
  delegation), 2.2.3 (planner write-block), 2.2.4 (speculation),
  2.2.5–2.2.8 (export/MT/CLI-parity/mobile), 3.4.3–3.4.6 (observability).
- **Gate:** §10.1 on 2.1.1; §10.4 on 2.2.3; §10.3 on 3.4.5 if a new column
  is introduced.
- **Exit:** Each item has its own acceptance PR; no regressions on
  `tests/run_all.py`.

### Phase F — Consolidation (optional, user-led)

- **Goal:** Decide on trading's first-class status.
- **Scope:** 2.3.1 vs. 2.3.2. This is explicitly a user decision per
  `refactor_inventory.md §8a`.
- **Gate:** §10 (explicit sign-off).
- **Exit:** Either a single `lib/trading/` package or a plugin directory
  loaded behind `TRADING_ENABLED=1`.

---

## 6. What This Document Is NOT

This document deliberately **does not** propose:

- Any concrete rewrite of `export.py`'s sanitization matrix (CLAUDE.md §11
  changes are triggered item-by-item, not pre-planned).
- Any change to `lib/llm_dispatch/config.py` model-routing tables or the
  alias-group for DeepSeek V3.2 mirrors (CLAUDE.md §10.2 — needs approval
  per decision).
- Any database schema change (new column, new table, new index) — those are
  surfaced only as "requires dual-backend update" flags (CLAUDE.md §10.3).
- Any security-sensitive middleware change — only the ones already listed in
  `docs/SECURITY_AUDIT_REPORT.md`'s resolution plan (CLAUDE.md §10.4).
- Any **date, SLO, or dollar figure**. Tofu does not measure uptime, cost
  ceilings, or latency targets at the project level today; inventing those
  here would be unfounded. Concrete numbers belong in a separate proposal
  once metrics exist.

It **is**:

- A prioritization of the capability direction (Functionality) and the
  hardening direction (Robustness).
- A map onto the existing planning artefacts: `ROADMAP.md` (pointer
  stub, rewritten in Phase A), `CHANGELOG.md` (0.9.2 baseline),
  `docs/refactor_decomposition_proposal.md` (executed-in-plan-only),
  `docs/refactor_inventory.md` (Phase 1–2 executed, §7 still open),
  `docs/SECURITY_AUDIT_REPORT.md`, `docs/RATE_LIMITING_DOS_AUDIT_REPORT.md`,
  `docs/agentic-development-experience.md`,
  `docs/omc-claude-code-backport-analysis.md`, `docs/pg-to-sqlite-analysis.md`,
  `a.md`, `CLAUDE.md`.
- A §10-aware sequencing proposal — every item tagged so the user can
  approve in the granularity they prefer.

---

_Generated by the Worker per the Planner's brief, 2026-04-21._
_Baseline: commit producing `VERSION=0.9.2`, `routes/daily_report.py=2938L`,
`lib/tasks_pkg/compaction.py=2620L`, `lib/llm_client.py=3664L`,
`static/js/ui.js=8716L`._
