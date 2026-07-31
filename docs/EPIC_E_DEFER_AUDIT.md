# Epic E — deferrability audit (pt_3879f00e sub-part 3)

Owner board epic: **`pt_3879f00e2d2f4bc4`** — "Frontend first-paint diet". Sub-part 3
is "defer `core/health_stream_timer.js` (60KB) + `core/cross_tab_sync.js` (53KB) into
`_DEFERRED_FILES`". A prior autonomous audit blocked the sub-part as
`[sibling-safe, needs-real-refactor]`; this document is the concrete site-by-site
work list a future dispatch can drive off.

The block reason boiled down to: **too many unguarded call sites make a pure
manifest-move impossible**. Deferring a file removes its symbol from window scope
during boot; if a runtime path then calls that symbol as a bare identifier, the
call throws `ReferenceError` — a hard boot break for the affected feature.

The `_DEFERRED_FILES` machinery in `lib/js_bundler.py` already implements the
right pattern: user-triggered entry points get typeof-guarded stubs in
`feature-loader.js`; on first invocation, the stub lazy-loads the deferred bundle
and swaps in the real function. `main.js:1189` already uses this idiom for
`_wireConvSyncPush`:

```js
if (typeof _wireConvSyncPush === 'function') _wireConvSyncPush();
```

That single line is the ONLY safe-to-defer touch point today. Every other
consumer of these two modules is unguarded and needs the same treatment BEFORE
the module can join `_DEFERRED_FILES`.

## Health-stream-timer audit (`core/health_stream_timer.js`)

Public API surface (read from the file's `window.*` exposes + its function
definitions):

- `twStart(convId)` — arms per-conv stream timer, seeds elapsed clock
- `twUpdate(convId)` — mark data received, request rAF repaint
- `twStop(convId)` — clear timers + finalize
- `_setStreamDegraded(convId, bool)` — degraded broadcast
- `streamHealthSubscribe(fn)` / `streamHealthGet()` — subscriber seam (already
  `window.*` exposed; safe for typeof-guarded consumers)
- Internal state: `_streamTimers`, `_streamBufs`, `_degradedStreams`,
  `_serverAlive`, `_lastHealthCheck`, `_consecutiveHealthFails`

Unguarded external call sites (evidence live-collected 2026-07-23):

| File | Line | Call | Guard? | Deferrable when |
|---|---|---|---|---|
| `project.js` | 182 | `twUpdate(activeConvId)` | ❌ | wrap in `if (typeof twUpdate === 'function')` |
| `ui/send_button.js` | 155 | `twStop(activeConvId)` (in try/catch) | already try-wrapped | need `typeof` gate BEFORE call, not just try |
| `ui/sse_handlers_io.js` | 38, 75, 114, 137, 160 | `twUpdate(convId)` × 5 | ❌ | 5 typeof gates |
| `ui/sse_handlers_lifecycle.js` | 123, 153, 198 | `twUpdate(convId)` × 3 | ❌ | 3 typeof gates |
| `ui/sse_handlers_misc.js` | 81, 139, 197, 217, 237, 259, 481 | `twUpdate(convId)` × 7 | ❌ | 7 typeof gates |
| `ui/sse_handlers_swarm.js` | 132, 245, 267, 305, 327, 361 | `twUpdate(convId)` × 6 | ❌ | 6 typeof gates |
| `ui/sse_handlers_tool.js` | 24, 75, 93, 164, 222, 266, 330 | `twUpdate(convId)` × 7 | ❌ | 7 typeof gates |
| `ui/sse_poll_fallback.js` | 90, 120, 146, 371, 423, 429, 457, 480 | `twStop`/`twUpdate` × 8 | ❌ | 8 typeof gates |
| `ui/sse_pipeline.js` | ~116 | (call chain not verified) | ❓ | audit needed |
| `main/main_conv_lifecycle.js` | 113 | inline comment refers to twStart armed by `connectToTask` | N/A | verify `connectToTask` calls path |

**Total: ~40 unguarded call sites in 8 files.** Each site is in a HOT streaming
path — SSE frame handlers fire on every content chunk during a live turn.

**Deferrability blocker:** deferring `health_stream_timer.js` today would trip
`ReferenceError` on the very first SSE frame of any active turn. The typeof-gate
sweep is the strangler-fig prerequisite; do NOT flip the `_DEFERRED_FILES`
manifest without landing the sweep first.

**Recommended order for a future dispatch:**

1. **Sweep sites file-by-file** with typeof guards — one commit per file (~7 commits, low risk each).
2. Add `feature-loader.js` stubs for `twStart` / `twUpdate` / `twStop` that
   lazy-load on first call, replacing the stub with the real function.
3. Add `_DEFERRED_ENTRY_POINTS` entries for the 3 names.
4. Move `'core/health_stream_timer.js'` from `_BUNDLE_FILES` to `_DEFERRED_FILES`.
5. NEUTER-verify: temporarily disable the auto-load in the stub, confirm the
   in-flight-turn path degrades gracefully (stream keeps advancing; UI just
   loses the elapsed-time badge until the module lands).

## Cross-tab-sync audit (`core/cross_tab_sync.js`)

Public API surface:

- `_applyRemoteConvDeleted(id)` — cross-device / cross-tab delete apply
- `_scheduleConvListRefresh()` — debounced sidebar refresh trigger
- `_handleCrossTabMsg(msg)` — BroadcastChannel message dispatcher
- `_crossDeviceReconcile()` — 25s poll cadence tick
- `_wireConvSyncPush()` — push-socket subscriber (already `window.*` exposed)
- Module-load side effect: `BroadcastChannel` listener wired via `core.js:132`

Consumers:

| File | Line | Symbol | Guard? |
|---|---|---|---|
| `core.js` | 132 | `_handleCrossTabMsg(e.data)` inside `BroadcastChannel.onmessage` | ❌ (called at module init) |
| `main.js` | 1189 | `_wireConvSyncPush()` | ✅ typeof-guarded |
| `conv_sync_push.js` | 29, 141 | `_wireConvSyncPush` reference (comment + window expose) | N/A |
| `main.js` | 1235 | comment mentions `_crossDeviceReconcile` — actual call? verify | ❓ |

**Deferrability blocker:** the `core.js:132` `_handleCrossTabMsg` call is
**inside a callback registered at module load** — it fires whenever a
BroadcastChannel message arrives. Deferring `cross_tab_sync.js` means
`_handleCrossTabMsg` is absent when the message lands. Two options:

- **A: Move the listener registration to `_handleCrossTabMsg`'s own file** (so
  it registers itself on load if present; absent module = no listener = no
  cross-tab sync, degraded but safe). Requires moving `_syncChannel` creation
  too, which is currently at `core.js:130`.
- **B: Bare-stub `_handleCrossTabMsg` in `feature-loader.js`** as
  `function _handleCrossTabMsg(msg) { /* deferred load */ }` — the module load
  wires the listener to the stub; on first cross-tab message, the stub
  lazy-loads and replays the message. More complex but preserves current
  registration site.

Option A is cleaner; Option B is safer under concurrent tab-switch traffic.

Recommend Option A: relocate the listener registration + `_syncChannel`
creation from `core.js` into `cross_tab_sync.js` itself, then defer.

## i18n boot-single-lang subset (sub-part 1)

Owner block reason (2026-07-23): requires splitting a 3275-line dict into a
runtime loader + fallback contract — genuinely non-trivial, not a manifest tweak.

Concrete recommendation for a future dispatch:

1. **Freeze the boot-critical key set.** Grep every `t('...')` call reachable
   from the boot IIFE + the ~526 static keys in `index.html`. Call this
   `_BOOT_I18N_KEYS`.
2. **Split `i18n.js` in two files:**
   - `i18n_boot.js` — only `_BOOT_I18N_KEYS` for CURRENT language (single-lang,
     ~30KB after minify), goes into `_BUNDLE_FILES`.
   - `i18n_full.js` — full dict for both languages, goes into `_DEFERRED_FILES`.
     Loaded on first `setLanguage()` OR first idle callback after boot.
3. **`t(key)` fallback contract**: when a key is not in the boot subset, first
   try the deferred dict (if loaded), else return the key string as a
   placeholder — matches how `t()` already behaves for missing keys today.
4. NEUTER-verify: temporarily prevent the deferred load, confirm all
   HTML strings render correctly (no visible `<em>i18n_key</em>` fallback), UI
   language switch fires the load synchronously.

Expected win: 308KB → ~30KB in the core bundle for i18n (~278KB savings on the
core bundle's compressed size before minify).

## `core/conversations.js` decomposition (sub-part 2)

134KB, 2396 lines. Package-split treatment like `ui/`, `settings/`, `main/`
took multiple sessions each. The natural seams inside `core/conversations.js`
(read from its symbol list):

- Conversation list CRUD + persistence (~40% of the file)
- Server-side reconcile / windowed read (windows.js already exists as a
  companion)
- IndexedDB paint-cache helpers (~15%)
- Optimistic-render + message projection (~25%)
- Utility formatters + fingerprint (~10%)

Each seam is ~3-5 sub-files under a `core/conversations/` sub-directory,
concatenated by the bundler in dependency order. This is a multi-commit epic
of its own — recommend spinning it out as `pt_conversations_decomp_XXX`.

## Overall verdict

None of the three sub-parts is a mechanical quick win. The `pt_3879f00e`
rollup should be **split into three separate epics** or the sole remaining
work item on THIS epic should be item 3's typeof-gate sweep (the smallest
concrete piece, ~7 file-scoped commits).

This document exists so a future dispatch (autonomous OR human) has a
concrete work list to drive from, instead of re-litigating the audit.
