/* ===== migrated source: core.js ===== */
/* ═══════════════════════════════════════════
   core.js — State, Config, Utils, Markdown
   ═══════════════════════════════════════════ */

const BASE_PATH = (() => {
  const p = window.location.pathname;
  return p.replace(/\/(index\.html)?$/, "");
})();
// Ambient lazy chunks (runtime/scene/*) resolve the prefix from here.
runtimeScope.BASE_PATH = BASE_PATH;
/* Idempotent by contract: resolver chains (safeAttachmentUrl → openVideoUrl,
 * the renderer's resolveMediaUrl port, …) legitimately hand an already-
 * prefixed URL through apiUrl again. A second concat produced
 * "/proxy/<port>/proxy/<port>/api/…", which the origin server 404s. */
function apiUrl(path) {
  if (BASE_PATH && (path === BASE_PATH || path.startsWith(BASE_PATH + "/"))) {
    return path;
  }
  return BASE_PATH + path;
}

/* ── Responsive breakpoints — SINGLE source of truth ───────────────────
 * The mobile breakpoint (768px) was hardcoded in ~7 JS call sites (bare
 * `innerWidth <= 768`, a local `MOBILE_BP`, two `matchMedia('(max-width:768px)')`
 * strings) that had to stay in lock-step with the CSS `@media(max-width:768px)`
 * master block. Any drift between them silently half-breaks the mobile layout
 * (e.g. the sidebar drawer opens with no backdrop). Consolidate onto ONE
 * constant + two tiny helpers so a future change is made in exactly one place.
 *
 * KEEP IN SYNC with the CSS master mobile block header in static/styles.css
 * (`@media(max-width:768px){ … OVERFLOW CONTAINMENT … }`) and the tablet-drawer
 * predicate (`@media(max-width:768px),(max-width:1024px) and (pointer:coarse)`).
 * If you change a number here, change it there too (guarded by
 * tests/test_breakpoint_coordination.py).
 *
 * `mobile` (768px, width-only) governs the phone compact layout + bottom sheet.
 * `tablet` (1024px, PAIRED WITH pointer:coarse) governs the portrait-tablet /
 * foldable slide-over drawer — the same viewport at which paper mode already
 * single-panes, so chat and paper stay consistent across our own surfaces. A
 * landscape tablet or a desktop at >1024px (or any fine-pointer device) keeps
 * the pinned two-pane layout because the pointer:coarse half is not satisfied. */
const TOFU_BP = Object.freeze({ mobile: 768, tablet: 1024 });
/** True when the viewport is at or below the mobile breakpoint (width test). */
function isMobileViewport() {
  return window.innerWidth <= TOFU_BP.mobile;
}
/** The mobile media-query string, e.g. '(max-width:768px)'. */
function mobileMediaQuery() {
  return '(max-width:' + TOFU_BP.mobile + 'px)';
}
/** The tablet-drawer media-query string — a coarse pointer at/below the tablet
 *  width. Matches the CSS paper-mode second predicate byte-for-byte. */
function tabletDrawerMediaQuery() {
  return '(max-width:' + TOFU_BP.tablet + 'px) and (pointer:coarse)';
}
/** True on a portrait tablet / foldable: touch-primary AND ≤ tablet width, but
 *  WIDER than a phone (a phone is already covered by isMobileViewport). Uses
 *  matchMedia so the pointer:coarse half is honored (a fine-pointer desktop
 *  narrowed to 900px stays on the desktop split). */
function isTabletDrawerViewport() {
  if (typeof window.matchMedia !== 'function') return false;
  return window.matchMedia(tabletDrawerMediaQuery()).matches
    && !isMobileViewport();
}
/** The union predicate the slide-over DRAWER behaviors gate on: a phone OR a
 *  portrait tablet. Any code that shows the backdrop / auto-collapses /
 *  swipe-toggles the sidebar must use THIS, not isMobileViewport alone, or the
 *  drawer opens on a tablet with no way to dismiss it. */
function isDrawerViewport() {
  return isMobileViewport() || isTabletDrawerViewport();
}
/** True when the user has asked the OS to minimize motion (accessibility /
 *  vestibular comfort). Animation code should check this and use instant
 *  scrolls / skip decorative transitions when it returns true. */
function prefersReducedMotion() {
  return typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}
if (typeof window !== 'undefined') {
  runtimeScope.TOFU_BP = TOFU_BP;
  runtimeScope.isMobileViewport = isMobileViewport;
  runtimeScope.mobileMediaQuery = mobileMediaQuery;
  runtimeScope.tabletDrawerMediaQuery = tabletDrawerMediaQuery;
  runtimeScope.isTabletDrawerViewport = isTabletDrawerViewport;
  runtimeScope.isDrawerViewport = isDrawerViewport;
  runtimeScope.prefersReducedMotion = prefersReducedMotion;
}

/* ── Lazy KaTeX ESM chunk ── */
let _katexLoading = null;
function _ensureKatex() {
  if (katex) return Promise.resolve();
  if (_katexLoading) return _katexLoading;
  _katexLoading = loadKatex().then((module) => {
    katex = module.default || module;
    if (typeof _mdCache !== 'undefined') _mdCache.clear();
    const conv = typeof getActiveConv === 'function' && getActiveConv();
    if (conv) runtimeScope.requestAuthoritativeConversationRender(conv.id);
    window.dispatchEvent(new CustomEvent('katex:loaded'));
  });
  return _katexLoading;
}

const TAB_ID = Math.random().toString(36).slice(2, 10);
/* Conversation Sync v3 uses this tab id for invalidation wake hints. */

/* DB-first: conversations start empty and are populated by
 *   loadConversationCatalog() in initActiveTasks().
 *   localStorage is NO LONGER used for conversation metadata.
 *   This eliminates an entire class of desync / ghost bugs. */
let conversations = [];
try { localStorage.removeItem('claude_conversations'); } catch(_) {} /* clean up stale data */


/* ═══ Folder management ═══ */
let _folders = [];  // Array of {id, name, color, collapsed, order, createdAt}
let _foldersLoaded = false;  // true after first loadFolders() completes


/* ── (folders.js extracted here) ── */

let activeConvId = sessionStorage.getItem('tofu_activeConvId') || null,
  pendingImages = [],
  pdfProcessing = 0;  // counter: # of in-flight PDF text-parses (see upload.js)
Object.defineProperties(runtimeScope, {
  activeConvId: {
    configurable: true,
    get: () => activeConvId,
  },
  conversations: {
    configurable: true,
    get: () => conversations,
  },
});
// The retained composer restores these four scalars before the image feature
// is ever requested. Keep the tiny state seam resident while the expensive
// image presenters and the owner-scoped image Offering list remain demand-loaded.
let _igSelectedModel = 'gemini-3.1-flash-image-preview';
let _igSelectedProviderId = '';
let _igSelectedAspect = '1:1';
let _igSelectedResolution = '1K';
let _igSelectedCount = 1;
let thinkingEnabled = true,
  fetchEnabled = true,
  codeExecEnabled = false,
  browserEnabled = false,
  desktopEnabled = false,
  memoryEnabled = true,
  schedulerEnabled = false,
  autopilotEnabled = false,
  activeFlow = "",   // "" | "builtin:autopilot" | <orchestration id>
  imageGenEnabled = false,
  imageGenMode = false,
  humanGuidanceEnabled = false,
  searchMode = "multi",
  /* Two-tier capability dial (chat/studio). The ONE user-facing toolbar
   * control; setChatMode() expands it into the atomic tool flags above
   * (mirrors the backend lib/tasks_pkg/chat_mode). 'studio' ⟺ a project is
   * attached, so the derived state stays truthful. Default 'chat' matches the
   * everyday all-rounder + DEFAULT_CHAT_MODE on the backend. (The old lean
   * 'air' tier merged into 'chat'; legacy air/pro normalise forward.) */
  chatMode = "chat",
  /* Plan Mode — orthogonal read-only planning toggle (Codex plan.md
   * analogue). Composes with the chatMode dial instead of being a third
   * tier: planning WITH a project's read-only tools attached is the primary
   * case. Backend authority: lib/tasks_pkg/plan_mode.py (assembly wire
   * filter + dispatch rejection lane + prompt contract). */
  planMode = false,
  debugVisible = false,
  sidebarSearchQuery = "";
Object.defineProperty(runtimeScope, 'memoryEnabled', {
  configurable: true,
  get: () => memoryEnabled,
});
/* Boot-path localStorage reads must never throw: one corrupted key (hand
 * edit, a truncated write in private mode, an older schema) would otherwise
 * kill module evaluation and white-screen the whole app (). */
function _safeJsonParse(raw, fallback) {
  if (raw == null) return fallback;
  try { return JSON.parse(raw); } catch (_) { return fallback; }
}
let serverModel = "aws.claude-opus-4.8";
let config = _safeJsonParse(
  localStorage.getItem("claude_client_config"),
  {
    temperature: 1,
    maxTokens: 128000,
    thinkingBudget: 64000,
    /* NOTE: no thinkingEffort here — it is a LEGACY preset key read only by
     * the cost.js migration. Shipping it in the defaults made every fresh
     * profile "migrate" to a hardcoded model id nobody chose. */
    imageMaxWidth: 0,           // 0 = follow server upload-shrink policy (recommended)
    systemPrompt: "",
    model: "",               // "" = 未存储任何选择；占位展示由 cost.js 播种并标记 provisional
  },
);

/* Whole-config persists must never launder a PROVISIONAL display model into
 * a stored choice. config.model can hold a placeholder the user never picked
 * (the hardcoded boot placeholder or the server default, painted by
 * _applyModelUI with _modelIsProvisional=true); storing it made the next boot
 * validate a "selection" nobody made — the "aws.claude-opus-4.8 I never
 * chose" loop. Every localStorage write of claude_client_config goes through
 * here so a provisional model persists as "" and the next boot flows from
 * the server default again. */
function _configForPersist() {
  if (!config || !config._modelIsProvisional) return config;
  return Object.assign({}, config, {
    model: '', modelRef: null, preferredProviderId: '', routing: {},
  });
}

/* Lazy bundles (settings-presenters save flow) persist through the same scrub
 * — expose it on the feature registry so the generated prelude can bind it. */
runtimeScope._configForPersist = _configForPersist;

/* ── (cost.js, debug_panel.js extracted here) ── */

function generateId() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

/* Client-side stable message id (Step 1 of unified chatInner rendering).
 *
 * The server backfills every persisted message with a UUID `_msgId` via
 * lib/tasks_pkg/manager.py:_assign_message_ids.  But a freshly created
 * client-side message (optimistic user push, streaming assistant
 * placeholder, image-gen result, …) is rendered into the DOM *before*
 * persistence — so the DOM has no stable handle for it yet.
 *
 * `_newClientMsgId()` mints a `tmp_<...>` id distinct from server UUIDs;
 * once the server persists the message and a Phase-2 reload arrives, the
 * server-assigned UUID overrides the temporary id (last-write-wins). */
function _newClientMsgId() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return 'tmp_' + crypto.randomUUID();
  }
  return 'tmp_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 10);
}

/* ── (HTML safety and error presentation now have typed owners) ── */

/* Look up a conversation object by id. Tolerates the `conversations`
 * global not being ready yet (very early init) and returns null when the
 * id is falsy or unknown. Canonical replacement for the open-coded
 * `conversations.find((c) => c.id === X)` scattered across the frontend;
 * `getActiveConv()` delegates to it. */
function getConvById(id) {
  if (!id || typeof conversations === "undefined" || !Array.isArray(conversations)) return null;
  return conversations.find((c) => c && c.id === id) || null;
}
// Stable read service for lazy domains. The function reads the live retained
// array on every call; publishing a captured conversation snapshot would drift.
runtimeScope.getConvById = getConvById;
function getActiveConv() {
  return getConvById(activeConvId);
}
/* Perf: cache chatContainer ref — avoids getElementById on every scroll check */
let _chatContainerEl = null;
function _getChatContainer() {
  if (!_chatContainerEl || !_chatContainerEl.isConnected) {
    _chatContainerEl = document.getElementById("chatContainer");
  }
  return _chatContainerEl;
}
function isNearBottom(threshold) {
  const c = _getChatContainer();
  if (!c) return true;
  return c.scrollHeight - c.scrollTop - c.clientHeight < (threshold || 150);
}
/* ── Instant-scroll seam ───────────────────────────────────────────────
 * Run `fn` with `scroll-behavior` forced to `auto` on `el`, then restore the
 * previous value. If any stylesheet sets `scroll-behavior:smooth` on a scroll
 * container, a `scrollTop` write becomes an ANIMATION: during streaming it
 * perpetually chases a growing scrollHeight (reader drifts off the bottom then
 * snaps back), and at turn-finalize it visibly slides instead of re-pinning.
 *
 * Every programmatic scroll write that must land IMMEDIATELY goes through
 * here, so the restore is never forgotten and callers cannot half-implement
 * it. Restores in a `finally` so a throwing `fn` cannot leave the container
 * stuck on `auto`. */
function _withInstantScroll(el, fn) {
  if (!el) { fn(); return; }
  const _prev = el.style.scrollBehavior;
  el.style.scrollBehavior = 'auto';
  try { fn(); }
  finally { el.style.scrollBehavior = _prev; }
}
if (typeof window !== 'undefined') runtimeScope._withInstantScroll = _withInstantScroll;

/* ── Bottom-follow suspension latch ────────────────────────────────────
 * The streaming auto-follow gates are POSITIONAL only (isNearBottom(80/200)).
 * While a rapidly-updating tail grows at up to 30fps, a reader who starts
 * scrolling up is yanked back to the bottom on every update until their
 * gesture crosses the threshold — and each yank resets their progress, so
 * the gesture and the programmatic pin fight every frame: the reported
 * "scrolling up during streaming makes the page shake/tremble".
 * The reader's UPWARD INTENT (wheel up, touch drag down, scrollbar drag /
 * PageUp moving away from the bottom) suspends the follow outright; landing
 * back at the bottom — or an explicit pin (scrollChatToBottom button,
 * `_forceScrollToBottom` turn transitions) — resumes it. */
let _followSuspended = false;
let _followListenersArmed = false;
function _armFollowSuspensionListeners() {
  if (_followListenersArmed) return;
  const c = _getChatContainer();
  if (!c) return;  // container not in DOM yet — retried by the next boot hook
  _followListenersArmed = true;
  c.addEventListener("wheel", (e) => {
    if (e.deltaY < 0) _followSuspended = true;  // wheel up = leave the bottom
  }, { passive: true });
  let _touchY = null;
  c.addEventListener("touchstart", (e) => {
    _touchY = e.touches.length ? e.touches[0].clientY : null;
  }, { passive: true });
  c.addEventListener("touchmove", (e) => {
    if (_touchY != null && e.touches.length
        && e.touches[0].clientY > _touchY + 4) {
      _followSuspended = true;  // drag down = scroll content up = leave the bottom
    }
  }, { passive: true });
  let _lastTop = null;
  c.addEventListener("scroll", () => {
    /* Scrollbar drags and PageUp/Home produce no wheel/touch event: any
     * user-driven move AWAY from the bottom suspends follow; landing back
     * at the bottom resumes it. Programmatic pins land here too — an
     * explicit pin IS a follow by definition. */
    const st = c.scrollTop;
    if (_lastTop != null && st < _lastTop - 4 && !isNearBottom(40)) {
      _followSuspended = true;
    } else if (isNearBottom(40)) {
      _followSuspended = false;
    }
    _lastTop = st;
  }, { passive: true });
}
if (typeof window !== 'undefined') runtimeScope._armFollowSuspensionListeners = _armFollowSuspensionListeners;

let _scrollRafId = null;
function scrollToBottom(force) {
  const c = _getChatContainer();
  if (!c) return;
  if (!force && (_followSuspended || !isNearBottom(200))) {
    /* Reader is scrolled up while content grows (e.g. live streaming) — no
     * scroll event fires, so refresh the scroll-to-bottom affordance here. */
    _updateScrollToBottomBtn();
    return;
  }
  /* PERF: Coalesce scroll updates and use single rAF (not double).
   * During streaming, the authoritative projection has already updated the
   * DOM. A single rAF is sufficient to scroll after layout; double-rAF added
   * 33ms of lag per frame. */
  if (_scrollRafId) return; // already scheduled
  _scrollRafId = requestAnimationFrame(() => {
    _scrollRafId = null;
    /* Re-check the suspension latch at WRITE time: the reader may have
     * scrolled up between the schedule call and this frame — writing anyway
     * is exactly the yank-down fight the latch exists to stop. */
    if (_followSuspended) return;
    /* SCROLL-JITTER FIX: during streaming this fires once per rAF while
     * scrollHeight is still growing. If any stylesheet sets
     * `scroll-behavior:smooth` on the chat container (some themes did),
     * the write becomes an animation that perpetually chases a moving
     * target — the reader visibly drifts off the bottom then snaps back.
     * Force instant scroll for the duration of the write. The tofu theme
     * no longer sets smooth; this is belt-and-suspenders for future
     * themes / user stylesheets. */
    _withInstantScroll(c, () => { c.scrollTop = c.scrollHeight; });
  });
}
/* ── Scroll-to-bottom button ──────────────────────────────────────────
 * A simple, always-available fallback affordance: when the reader scrolls
 * up away from the latest message, a floating pill appears; clicking it jumps
 * to the bottom via the real-height force-scroll path. */
function scrollChatToBottom() {
  /* ConversationSurface captures near-bottom state before every commit, so a
   * direct jump is sufficient to keep following later Turn revisions. */
  _followSuspended = false;
  const c = _getChatContainer();
  if (c) _withInstantScroll(c, () => { c.scrollTop = c.scrollHeight; });
  _updateScrollToBottomBtn();
}
function _updateScrollToBottomBtn() {
  const btn = document.getElementById("scrollToBottomBtn");
  if (!btn) return;
  const c = _getChatContainer();
  /* Show only when there's real overflow AND the reader is scrolled up. The
   * 120px threshold keeps the button hidden while effectively at the bottom
   * (matches the near-bottom slack the streaming auto-scroll uses). */
  const hasOverflow = !!c && c.scrollHeight - c.clientHeight > 40;
  const show = hasOverflow && !isNearBottom(120);
  btn.classList.toggle("visible", show);
}
if (typeof window !== "undefined") {
  runtimeScope.scrollChatToBottom = scrollChatToBottom;
  runtimeScope._updateScrollToBottomBtn = _updateScrollToBottomBtn;
}

function getToolRoundsFromMsg(msg) {
  // execute_tools is a wire/protocol adapter, not user-visible work. Keep the
  // persisted round intact for replay and diagnostics, but project only its
  // real child tools into every chat renderer.
  const visible = (rounds) => (rounds || []).filter(
    (r) => !(r && r.toolName === "execute_tools"));
  // The inject sidecars must be rehydrated onto REAL rounds too, not only
  // onto the empty base — after a reload or turn-projection refresh a
  // turn normally has both. Returning `msg.toolRounds` directly here dropped
  // every swarm/peer/steer chip from exactly the common case.
  if (msg.toolRounds && msg.toolRounds.length > 0)
    return _rehydrateInjectRows(msg, visible(msg.toolRounds));
  const base = [];
  return _rehydrateInjectRows(msg, base);
}

/* ── Inbox-inject render-time rehydration ────────────────────────────────
 * The swarm / peer / user-steer inject lanes each render as a synthetic
 * in-timeline toolRound (flagged `_inboxInject` / `_peerInject` /
 * `_userSteerInject`). Those synthetic rows are DISPLAY-ONLY and are NEVER
 * persisted into the DB `toolRounds` (that array is the wire-replay /
 * prefix-cache source — a synthetic row lacking toolCallId/toolContent would
 * collapse the whole assistant turn to a lossy summary AND shift the wire
 * prefix). Instead the backend persists a DISPLAY-ONLY underscore sidecar on
 * the message: `_inboxInjects` / `_peerInjects` / `_userSteerInjects`. After a
 * reload or authoritative turn refresh, `msg.toolRounds` holds
 * ONLY real tool rounds, so we rebuild the synthetic rows here — on a COPY,
 * never mutating the canonical turn projection. Idempotent: if a live row exists for a
 * round (dedup key) we don't add a duplicate. */
/* ── Anchor-position a synthetic inject row inside a toolRounds array ──────
 * A mid-turn inject (steer / peer / async swarm) is CONSUMED at the model's
 * loop round `round_num`; the backend emits its chip event with
 * `round = round_num + 1` (1-based), while the REAL tool rounds of that same
 * loop iteration carry `llmRound = round_num` (0-based). So the row belongs
 * immediately ABOVE the first real round whose `llmRound === injectRound - 1`
 * (the top of the round that consumed it — "user speaks first, model responds
 * below"). `_spliceInjectRow` inserts it there; when no anchor round exists yet
 * (e.g. the inject landed before any tool ran this round) it falls back to the
 * tail. The row keeps `llmRound` UNSET so it never folds into the real round's
 * parallel-batch group (which would inflate the "N parallel calls" count) — it
 * renders as its own solo group that simply sorts above the anchor.
 * Mutates `arr` in place and returns it. */
function _spliceInjectRow(arr, row, anchorLlmRound) {
  if (!Array.isArray(arr)) return arr;
  let at = -1;
  if (anchorLlmRound != null) {
    for (let i = 0; i < arr.length; i++) {
      const r = arr[i];
      if (r && !r._userSteerInject && !r._peerInject && !r._inboxInject
          && !r._bgCommandInject && !r._stallNudge
          && r.llmRound === anchorLlmRound) { at = i; break; }
    }
  }
  if (at >= 0) arr.splice(at, 0, row);
  else arr.push(row);
  return arr;
}
if (typeof window !== "undefined") runtimeScope._spliceInjectRow = _spliceInjectRow;

/* Synthetic inject-row roundNums must be STABLE across render passes: the
 * live DOM sync keys groups (`S{roundNum}`) and slots (`data-prn`) by
 * roundNum, so deriving it from `out.length` re-keys the same chip on every
 * pass as real rounds stream in — each new key spawns a fresh DOM group and
 * the stale one is never collected (conv mt2x5y77kk19qc, 2026-08-21: one
 * intent-stall nudge rendered as THREE identical chips). Lane bases keep the
 * four lanes collision-free with each other and with real (small sequential)
 * roundNums; `+ injectRound` keeps one inject event's key fixed for the whole
 * turn, so rehydrate passes and live appends land on the SAME DOM node. */
const _INJECT_ROUND_BASE = { inbox: 9000000, peer: 9100000, steer: 9200000, stall: 9300000, bgcmd: 9400000 };
function _rehydrateInjectRows(msg, base) {
  if (!msg) return base;
  const swarm = Array.isArray(msg._inboxInjects) ? msg._inboxInjects : [];
  const peer = Array.isArray(msg._peerInjects) ? msg._peerInjects : [];
  const steer = Array.isArray(msg._userSteerInjects) ? msg._userSteerInjects : [];
  const bgcmd = Array.isArray(msg._bgCommandInjects) ? msg._bgCommandInjects : [];
  const stall = Array.isArray(msg._stallNudges) ? msg._stallNudges : [];
  if (!swarm.length && !peer.length && !steer.length && !bgcmd.length && !stall.length) return base;
  const out = base.slice();
  const _has = (pred) => out.some(pred);
  for (const s of swarm) {
    const rnd = s.round || 0;
    if (_has(r => r._inboxInject && r._inboxKey === "inbox:" + rnd)) continue;
    _spliceInjectRow(out, {
      roundNum: _INJECT_ROUND_BASE.inbox + rnd,
      status: "done",
      _inboxInject: true,
      _inboxKey: "inbox:" + rnd,
      inboxRound: rnd,
      inboxCount: s.count || 0,
      inboxAgentIds: Array.isArray(s.agentIds) ? s.agentIds.filter(Boolean) : [],
      inboxPreviews: Array.isArray(s.previews) ? s.previews : [],
    }, rnd - 1);
  }
  for (const p of peer) {
    const rnd = p.round || 0;
    if (_has(r => r._peerInject && r._peerKey === "peer:" + rnd)) continue;
    _spliceInjectRow(out, {
      roundNum: _INJECT_ROUND_BASE.peer + rnd,
      status: "done",
      _peerInject: true,
      _peerKey: "peer:" + rnd,
      peerRound: rnd,
      peerCount: p.count || 0,
      peerPreviews: Array.isArray(p.previews) ? p.previews : [],
    }, rnd - 1);
  }
  for (const s of steer) {
    const rnd = s.round || 0;
    if (_has(r => r._userSteerInject && r._steerKey === "steer:" + rnd)) continue;
    _spliceInjectRow(out, {
      roundNum: _INJECT_ROUND_BASE.steer + rnd,
      status: "done",
      _userSteerInject: true,
      _steerKey: "steer:" + rnd,
      steerRound: rnd,
      steerCount: s.count || 0,
      steerPreviews: Array.isArray(s.previews) ? s.previews : [],
    }, rnd - 1);
  }
  for (const s of bgcmd) {
    const rnd = s.round || 0;
    if (_has(r => r._bgCommandInject && r._bgcmdKey === "bgcmd:" + rnd)) continue;
    _spliceInjectRow(out, {
      roundNum: _INJECT_ROUND_BASE.bgcmd + rnd,
      status: "done",
      _bgCommandInject: true,
      _bgcmdKey: "bgcmd:" + rnd,
      bgCommandRound: rnd,
      bgCommandCount: s.count || 0,
      bgCommandPreviews: Array.isArray(s.previews) ? s.previews : [],
    }, rnd - 1);
  }
  for (const s of stall) {
    const rnd = s.round || 0;
    if (_has(r => r._stallNudge && r._stallKey === "stall:" + rnd)) continue;
    _spliceInjectRow(out, {
      roundNum: _INJECT_ROUND_BASE.stall + rnd,
      status: "done",
      _stallNudge: true,
      _stallKey: "stall:" + rnd,
      stallRound: rnd,
      stallTool: s.tool || "",
      stallFailedRound: s.failedRound,
      stallBadge: s.badge || "",
      stallPrompt: s.prompt || "",
      stallMax: s.max || 1,
    }, rnd - 1);
  }
  return out;
}
if (typeof window !== "undefined") {
  runtimeScope._rehydrateInjectRows = _rehydrateInjectRows;
}


/* Conversation invalidation, catalog, cache statistics, Markdown, health, and
 * toast responsibilities live in their dedicated runtime sections. */
