# Module Design Doc — Unit 9: Infra / Runtime (`agent_core/`, `log`, `cross_dc`, `fs_keepalive`, `self_update`, `runtime_state_store`)

> Part of the per-module design-doc set (see `docs/ARCHITECTURE.md`). This unit
> is the foundation everything else is built on: the reusable agent base
> (`agent_core/`), logging, the cross-DC latency detector, the FUSE keepalive,
> the self-updater, and the scale-out lease/counter substrate.
>
> **Grounding:** every line count is `wc -l` on disk 2026-07-11. `list_dir`
> overcounts — all numbers are `wc -l`. Every MISCUT/BIG verdict cites competing
> responsibilities or line ranges; size alone is never the argument.
>
> **The analytical payload is BOUNDARY INTEGRITY**: does the `agent_core/`
> core/plugin boundary still hold on disk (AST test passing, manifest matching),
> and are the `task_runtime.py` / `push.py` shims still thin?

---

## 1. Boundary integrity — the core/plugin AST guard (verified LIVE on disk)

The charter + CLAUDE.md describe `agent_core/` as the reusable "agent base" with
a hard core/plugin boundary enforced by `lib/agent_core_manifest.py` +
`tests/test_agent_core_boundary.py`. The risk this unit checks: has the core
silently grown an import of a concrete plugin (the exact rot the boundary
exists to prevent)?

**Verdict: the boundary HOLDS. I ran the test on disk — 5 passed — and confirmed
the manifest matches the code.** This is not "the docstring claims it"; it is
verified:

### 1a. The AST test exists and PASSES (ran it)

```
$ python -m pytest tests/test_agent_core_boundary.py -q  →  5 passed
```

The 5 tests (read in full) assert, over EVERY file the manifest names as core:
1. `test_core_modules_resolve_to_files` — every `CORE_MODULES` prefix maps to a
   real file (no stale manifest entry).
2. `test_core_does_not_import_concrete_plugins` — **the load-bearing one:** walks
   the AST of every core file and fails if any imports a `CONCRETE_PLUGIN_MODULES`
   entry (the 8 concrete tool schema modules) outside the two `REGISTRY_SEAMS`.
3. `test_core_persistence_imports_within_ratchet` — no core file imports
   `lib.database` / `lib.conversations` beyond a per-file baseline that is
   **empty** (`_PERSISTENCE_IMPORT_BASELINE = {}`) → ZERO direct persistence
   imports in core, enforced (the ConversationStore seam from the
   `agent-core-persistence-seam` work).
4. `test_facade_members_are_within_core` — every `lib/agent_core/__init__.py`
   re-export resolves to a `CORE_MODULES` module (facade can't surface a
   non-core symbol).
5. `test_facade_reexports_resolve` — every `__all__` symbol is importable.

### 1b. The manifest matches disk (not drifted)

Read `agent_core_manifest.py` in full. `CORE_MODULES` names the base surface
(orchestrator/model_config/endpoint/compaction/tool_dispatch/executor + `lib.llm`
+ the three `llm_dispatch` leaves + the three `swarm` scheduling leaves + the two
relocated leaves `task_runtime`/`push` + `lib.agent_core`). `CONCRETE_PLUGIN_MODULES`
names the 8 tool-schema modules core must not import. `is_core_module` correctly
also admits the two `REGISTRY_SEAMS` + the two package facades (`lib.llm`,
`lib.llm_dispatch`). The `CORE_MEMBERS` map in `__init__.py` is in lockstep
(test #4 enforces it). **No manifest entry is stale (test #1) and no core file
imports a concrete plugin (test #2) — the guard is green on the current tree.**

### 1c. The boundary is the SAME manifest-first design the charter describes

Crucially, the boundary is enforced by the AST test reading the manifest, NOT by
directory layout — the manifest docstring is explicit: "a folder can't stop an
import; what enforces the boundary is the AST test." Most core members are still
"named-in-place" inside `tasks_pkg`/`llm_dispatch`/`swarm` (a physical move would
create back-imports + ~960 rewrites) — only self-contained leaves (`push`,
`task_runtime`, `profiles`) were physically relocated. This is the documented
Stage-1 state, and the test guards the logical boundary regardless of physical
location. **Integrity is intact.**

### 1d. The shims are still THIN re-exports (verified)

- `lib/task_runtime.py` = **19 lines**, body is `from lib.agent_core.task_runtime
  import TaskRuntime` + `__all__`. Zero logic.
- `lib/push.py` = **28 lines**, body is `from lib.agent_core.push import (PushClient,
  PushHub, broadcast, hub, push_event)` + `__all__`. Zero logic; preserves the
  `hub` singleton identity (`lib.push.hub is lib.agent_core.push.hub`).

Neither has accreted logic — both are pure compatibility shims, exactly as
claimed. **No rot.**

---

## 2. Module inventory (real `wc -l`, size verdict, status, tests)

### 2.1 `agent_core/` — the reusable base (2806 LOC, 13 files)

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `events.py` | 620 | **BIG (registry data)** | HOT | `test_event_registry` |
| `task_runtime.py` | 380 | OK | HOT | `test_task_runtime` |
| `admission.py` | 335 | OK | HOT | `test_admission` |
| `push.py` | 326 | OK | HOT | `test_push_hub_and_plugin_registry`, `test_push_fanout`, `test_push_latency` |
| `personal_scope.py` | 232 | OK | HOT | `test_personal_scope_headless` |
| `push_bus.py` | 182 | OK | live (scale) | `test_push_fanout` |
| `profiles.py` | 178 | OK | HOT | via profile e2e |
| `sse_limit.py` | 145 | OK | HOT | via admission e2e |
| `__init__.py` | 128 | OK (lazy facade) | HOT | `test_agent_core_boundary` |
| `activity.py` | 86 | OK (seam) | HOT | via project-brain e2e |
| `principal.py` | 66 | leaf | HOT | via admission e2e |
| `store.py` | 64 | OK (seam) | HOT | `test_agent_core_boundary` |
| `affinity.py` | 64 | leaf | live (scale) | `test_conv_affinity` |

`events.py` — **BIG (620) but justified — it is a REGISTRY of data, not logic.**
It is the single source of truth for the ~90 streaming-event `type` strings
(the `EventType` constants + one `EventSpec` per event with category/terminal/
fields). The only logic is `build_event`/`emit`/`to_capabilities_dict` (compact);
the bulk is the `_SPECS` table. It exists precisely to make the wire contract
explicit + machine-discoverable (`GET /api/v1/capabilities`) and is drift-checked
by `test_event_registry`. Do NOT split — fragmenting the event vocabulary defeats
its single-source purpose (same reasoning as `_core_schema` in Unit 7).

`__init__.py` — OK, and notable: a **lazy PEP-562 facade** (`__getattr__` over
`CORE_MEMBERS`) so `from lib.agent_core.push import hub` stays cheap and does NOT
drag in the orchestrator chain. The membership map is the human-readable mirror
of the manifest.

`admission.py` / `sse_limit.py` — OK. Backpressure primitives (global in-flight
ceiling + per-principal SSE cap), both re-keyed onto `runtime_state_store`'s
atomic `acquire_slot` (§2.4) so they're N-replica-invariant under Redis. Also
holds the event-driven waiter registry (`await_terminal` — replaces per-request
busy-poll threads).

`store.py` / `activity.py` — OK. The two host-overridable seams that keep core
DB-free: `get_conversation_store()` (persistence, the ratchet-enforced seam) and
`set_activity_sink()` (project-brain feed). Both are core-safe accessors that
lazily resolve the non-core adapter. **These are WHY test #3 passes** — core
reaches persistence/feed only through them.

`personal_scope.py` — OK. The app-personal-vs-headless capability registry (Unit
8 referenced it for billing gating; it's the memory/preferences/paper-insight
fail-closed seam). One clean concern.

The rest (`profiles`, `push_bus`, `principal`, `affinity`) are small
single-concern modules. **The whole `agent_core/` package is well-decomposed** —
nothing miscut, the one big file is a justified registry.

### 2.2 Infra singles

| Module | LOC | Verdict | Status | Tests |
|---|--:|---|---|---|
| `self_update.py` | 869 | **BIG** | live (topbar) | `test_self_update_tarball` |
| `cross_dc.py` | 569 | OK | live (startup) | via cross-dc e2e |
| `log.py` | 505 | OK | HOT | `test_log_clean`, `test_log_noise_reduction`, `test_db_shutdown_log_degrade` |
| `runtime_state_store.py` | 501 | OK | HOT (scale) | `test_runtime_state_store` |
| `protocols.py` | 479 | OK | HOT | `test_agent_core_boundary` (ConversationStore) |
| `fs_keepalive.py` | 298 | OK | live (Linux) | `test_fs_keepalive_data_root` |
| `agent_core_manifest.py` | 182 | OK (the boundary decl) | HOT | `test_agent_core_boundary` |
| `task_runtime.py` (shim) | 19 | OK (thin shim) | HOT | `test_task_runtime` |
| `push.py` (shim) | 28 | OK (thin shim) | HOT | `test_push_*` |

`self_update.py` — **BIG (869), bundles 2 update STRATEGIES.** (a) `_apply_via_git`
(fetch + `pull --ff-only` + dirty-tree refusal + requirements diff/install) and
(b) `_apply_via_tarball` (download + validate + reversible overlay + backup for
non-git deployments), plus the shared check/version/working-tree machinery. The
two strategies are a clean split seam: `self_update_git.py` + `self_update_tarball.py`
sharing `self_update.py` (check + dispatch + `_install_requirements`). Cited by
concern (two ~300-line apply paths). It's a safety-critical file (dirty-tree
refusal, reversible overlay) so a split needs care; classified BIG, split
candidate.

`cross_dc.py` — OK. One cohesive concern (FUSE cluster latency detect + adaptive
timeout), fully env-driven + auto-benchmarked (no hardcoded infra, per §3.5). The
two-phase init (fast path-index + background benchmark) is a single well-bounded
mechanism.

`log.py` — OK. The centralized logging facade: `get_logger`/`log_exception`/
`audit_log`/`log_context`/`log_route`/`log_external`/`log_suppressed` + the
writable-log-dir resolution (a deliberate byte-for-byte twin of
`runtime_paths._resolve_base`, kept inline to avoid an import cycle — documented).
Foundational, imported everywhere; correctly one file.

`runtime_state_store.py` — OK, and a **reference-quality pluggable seam.** The
single lease/counter/heartbeat substrate for scale-out (`InProcRuntimeStateStore`
default = byte-equivalent single-box; `RedisRuntimeStateStore` opt-in = N-replica
authoritative, guarded import, fail-open). Mirrors `rate_limit_store` exactly.
Both Epic-A caps (admission + SSE) re-key onto its atomic `acquire_slot`. This is
the charter's ratified scale-out primitive, cleanly bounded.

`protocols.py` — OK. Core-safe `@runtime_checkable` Protocol definitions
(`ConversationStore` etc.) — the type seam that lets core stay DB-free. No DB
import (that's the point). Well-bounded.

`fs_keepalive.py` — OK. Linux-only FUSE/NFS mount keepalive (single concern,
env-gated).

`agent_core_manifest.py` — OK. The executable boundary declaration itself (§1) —
small, pure data + three predicates.

---

## 3. Dependencies (in / out)

**Inbound:** `agent_core/` is imported by essentially everything — the facade
(`from lib.agent_core import run_task, dispatch_chat, ...`), the shims
(`lib.push`, `lib.task_runtime`), the headless surfaces (`admission`, `sse_limit`,
`personal_scope`), the event contract (`events` → orchestrator emissions +
`/api/v1/capabilities`). `log` is imported by every module in the tree.
`self_update` by `routes/api_v1/update.py`. `cross_dc`/`fs_keepalive` by `server.py`
startup. `runtime_state_store` by `admission`/`sse_limit`/`push`.

**Key internal edges (all boundary-respecting):**
- core → persistence ONLY via `agent_core.store.get_conversation_store()` →
  `protocols.ConversationStore` (the ratchet-enforced seam; test #3).
- core → project-brain feed ONLY via `agent_core.activity.set_activity_sink()`.
- core → plugins ONLY via the two `REGISTRY_SEAMS` (`tools.registry`,
  `llm_dispatch.provider_registry`; test #2).
- `admission`/`sse_limit`/`push` → `runtime_state_store.get_store()` (the scale
  substrate).
- `events.emit` → `manager.append_event` (LAZY import — importing the manager at
  module load would invert the dependency direction; documented).

**Outbound:** `log` → stdlib only (+ writable-dir resolution mirroring
`runtime_paths`). `self_update` → `http_client` + `runtime_layout` (the shared
skip-list registry) + `subprocess` (git). `cross_dc`/`fs_keepalive` → stdlib
(`os.stat`, `socket`). `runtime_state_store` → guarded `redis` import (opt-in).
**No back-edges up into `routes`; core has NO direct DB import (enforced).**

---

## 4. Invariants (must not be broken by a refactor)

1. **The core/plugin boundary is AST-enforced** (§1). No `CORE_MODULES` file may
   import a `CONCRETE_PLUGIN_MODULES` entry outside the two `REGISTRY_SEAMS`.
   `test_agent_core_boundary` fails CI on violation. Add a tool via `ToolSpec`,
   not an import.
2. **Core is DB-free** — `_PERSISTENCE_IMPORT_BASELINE` is EMPTY (zero tolerance).
   Reach persistence only via `get_conversation_store()`. Never raise a baseline.
3. **`CORE_MEMBERS` (facade) stays in lockstep with `CORE_MODULES` (manifest)** —
   test #4 enforces every re-export resolves to a core module.
4. **The `lib.push` / `lib.task_runtime` shims stay thin re-exports** and preserve
   the `hub` singleton identity — tests monkeypatching `push_event` must patch
   `lib.agent_core.push.push_event` (the canonical home), NOT the shim.
5. **`events.py` is the single event-contract source** — emit via
   `build_event(EventType.X)`, never a bare `{'type': ...}` literal; drift caught
   by `test_event_registry`. `EVENT_CONTRACT_VERSION` bumps only on a breaking
   shape change.
6. **`runtime_state_store` is fail-open** — a Redis error degrades (acquire→True,
   count→0) after one WARN; a cap substrate must never take down the request path.
   The atomic `acquire_slot` (ZSET rank / locked count) can never overshoot the cap.
7. **`self_update` git mode refuses hard on a dirty tree** — never auto-stash,
   never `--force`; tarball mode is always reversible (backup before overwrite,
   validate before touching). Runtime-state churn is tolerated via the shared
   `runtime_layout` skip-list (single source with export + .gitignore).
8. **`log`'s writable-dir resolution is a byte-for-byte twin of
   `runtime_paths._resolve_base`** (kept inline to avoid a cycle) — the two must
   agree or logs + data split to different roots (`test_desktop_install_paths`).
9. **`cross_dc` / `fs_keepalive` are silent no-ops without their env vars** — no
   hardcoded infra (§3.5 of CLAUDE.md).
10. **Personal-capability defaults are fail-closed on headless** (`personal_scope`)
    — `apply_headless_personal_defaults` setdefaults every capability OFF unless
    the caller opts in.

---

## 5. Known debt (grounded)

- **`self_update.py` (869) bundles the git + tarball apply strategies** (§2.2) —
  a clean split seam, but safety-critical; split with care.
- **CLAUDE.md's Unit-9 module list is incomplete** — it names
  `runtime_state_store`, `cross_dc`, `fs_keepalive`, `self_update`, `agent_core/`
  but the `agent_core/` package has grown to 13 modules (admission, sse_limit,
  push_bus, personal_scope, activity, principal, store, affinity beyond the
  original push/task_runtime/profiles/events) — all boundary-clean, but the map
  under-describes the base.
- No boundary rot, no shim accretion, no direct-DB creep — the three things this
  unit was tasked to check are all CLEAN.

---

## 6. Segmentation verdict (this unit)

**Correctly bounded — leave as-is:**
The ENTIRE `agent_core/` package (well-decomposed; `events.py` is a justified
registry, not a miscut), `log`, `cross_dc`, `runtime_state_store` (reference-quality
pluggable seam), `protocols`, `fs_keepalive`, `agent_core_manifest`, and both
shims (verified thin, §1d).

**Miscut — should split:**
1. **`self_update.py` (869) → `self_update_git.py` + `self_update_tarball.py`**,
   leaving the shared check/version/working-tree/dispatch + `_install_requirements`
   in `self_update.py`. The two `_apply_via_*` strategies (~300 lines each) are a
   clean concern boundary. Behind `test_self_update_tarball` (+ add a git-path
   test). RISK: safety-critical (dirty-tree refusal, reversible overlay) — preserve
   the refusal/backup invariants verbatim.

**Do NOT split:** `events.py` (single-source event registry — fragmenting defeats
its purpose, like `_core_schema`), `log.py` (foundational facade), the manifest,
the shims.

**No boundary action needed** — the core/plugin guard is green on disk and the
manifest is not drifted; this unit's headline is a CLEAN boundary, not a defect.

---

## 7. Comparison to Units 1–8 (the running thesis)

- **The boundary this unit was tasked to check is INTACT and TEST-ENFORCED** —
  the strongest structural guarantee in the survey: an AST test that fails CI if
  core grows a plugin import, plus a ratchet that keeps core DB-free. This is the
  "documentation that executes" pattern working exactly as designed. Where Unit 6
  had a single-sourced join guarded by convention and Unit 7 a single table source
  guarded by a parity test, here the *architectural layering itself* is guarded by
  an AST test.
- **`agent_core/` is a FIFTH reference-quality package** (with `swarm/`,
  `token_counter/`, `compaction/`, `billing/`) — well-decomposed, the one big file
  a justified registry. The base everything is built on is, encouragingly, among
  the cleanest code in the tree.
- **The only miscut is `self_update.py`** (two apply strategies in one file) — a
  concern-boundary split like Unit 8's `oauth/codex.py`, not a five-concerns giant.
- **`runtime_state_store` + the personal_scope / store / activity seams** show the
  project's seam-first discipline: caps, persistence, feed, and plugins all reach
  across boundaries through ONE named, tested seam each — the same
  single-source-of-truth philosophy verified in every prior unit.

---

*Next unit: Unit 10 (Scheduling / ops — `scheduler/`, `optimizer/`, `daily_report/`).*
