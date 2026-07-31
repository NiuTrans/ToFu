/* ═══════════════════════════════════════════
   core.js — State, Config, Utils, Markdown
   ═══════════════════════════════════════════ */

const BASE_PATH = (() => {
  const p = window.location.pathname;
  return p.replace(/\/(index\.html)?$/, "");
})();
function apiUrl(path) {
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
  window.TOFU_BP = TOFU_BP;
  window.isMobileViewport = isMobileViewport;
  window.mobileMediaQuery = mobileMediaQuery;
  window.tabletDrawerMediaQuery = tabletDrawerMediaQuery;
  window.isTabletDrawerViewport = isTabletDrawerViewport;
  window.isDrawerViewport = isDrawerViewport;
  window.prefersReducedMotion = prefersReducedMotion;
}

/* ── Lazy KaTeX loader (277KB single-line script freezes DevTools) ── */
let _katexLoading = null;
function _ensureKatex() {
  if (typeof katex !== 'undefined') return Promise.resolve();
  if (_katexLoading) return _katexLoading;
  _katexLoading = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = BASE_PATH + '/static/vendor/katex/katex.min.js';
    s.onload = () => {
      /* Flush markdown cache so math re-renders with KaTeX */
      if (typeof _mdCache !== 'undefined') _mdCache.clear();
      /* Trigger a re-render of current chat */
      const conv = typeof getActiveConv === 'function' && getActiveConv();
      if (conv) window.ConvView.replaceAll(conv.id);
      /* Notify other surfaces (paper reader, artifacts, etc.) that
       * placed math-pending fallback markup so they can repaint with
       * real KaTeX output instead of staying stuck on `<code>` spans. */
      try { window.dispatchEvent(new CustomEvent('katex:loaded')); } catch (_) {}
      resolve();
    };
    s.onerror = () => reject(new Error('Failed to load KaTeX'));
    document.head.appendChild(s);
  });
  return _katexLoading;
}

/* ── Lazy PDF.js loader (pdf.min.js + worker, heavy — lazy-loaded on first use) ── */
let _pdfJsLoading = null;
function _ensurePdfJs() {
  if (typeof pdfjsLib !== 'undefined') return Promise.resolve();
  if (_pdfJsLoading) return _pdfJsLoading;
  _pdfJsLoading = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = BASE_PATH + '/static/vendor/pdf.min.js';
    s.onload = () => {
      /* Configure worker path */
      if (typeof pdfjsLib !== 'undefined' && pdfjsLib.GlobalWorkerOptions) {
        pdfjsLib.GlobalWorkerOptions.workerSrc = BASE_PATH + '/static/vendor/pdf.worker.min.js';
      }
      resolve();
    };
    s.onerror = () => reject(new Error('Failed to load PDF.js'));
    document.head.appendChild(s);
  });
  return _pdfJsLoading;
}

const TAB_ID = Math.random().toString(36).slice(2, 10);
/* BroadcastChannel + listener registration RELOCATED to
 * core/cross_tab_sync.js (pt_3879f00e Epic-E sub-part 3). See that module
 * for the construction; TAB_ID stays here (leaf constant with multiple
 * consumers, incl. main.js's boot log). */

/* ★ DB-first: conversations start empty and are populated by
 *   loadConversationsFromServer() in initActiveTasks().
 *   localStorage is NO LONGER used for conversation metadata.
 *   This eliminates an entire class of desync / ghost bugs. */
let conversations = [];
try { localStorage.removeItem('claude_conversations'); } catch(_) {} /* clean up stale data */


/* ═══ Folder management ═══ */
let _folders = [];  // Array of {id, name, color, collapsed, order, createdAt}
let _foldersLoaded = false;  // true after first loadFolders() completes


/* ── (folders.js extracted here) ── */

let activeConvId = sessionStorage.getItem('tofu_activeConvId') || null,
  activeStreams = new Map(),
  pendingImages = [],
  pdfProcessing = 0;  // counter: # of in-flight PDF text-parses (see upload.js)
/** Message-queue MIRROR of server state (read-only on the client).
 *  The backend is the single source of truth: sending always POSTs to
 *  /api/chat/send, and the server decides queue-vs-dispatch. This Map is
 *  populated ONLY by _refreshServerQueue() (main_send_pipeline.js) to drive
 *  the queued-message UI — the client never optimistically enqueues here.
 *  Key = convId, Value = Array of { text, images, pdfTexts, replyQuotes, convRefs, timestamp } */
let pendingMessageQueue = new Map();
let _editingMsgIdx = null,
  _lastRenderedFingerprint = "";
/** Lightweight fingerprint of what's currently rendered — used to skip no-op re-renders from sync */
function _convRenderFingerprint(conv) {
  if (!conv) return "";
  const n = conv.messages.length;
  if (n === 0) return conv.id + ":0:" + (conv.title || "");
  const last = conv.messages[n - 1];
  const sr = last.toolRounds || last.searchResults;
  return (
    conv.id +
    ":" +
    n +
    ":" +
    (last.content || "").length +
    ":" +
    (last.thinking || "").length +
    ":" +
    (last.error || "").length +
    ":" +
    (last.finishReason || "") +
    ":" +
    (last.translatedContent || "").length +
    ":" +
    (sr ? sr.length : 0) +
    ":" +
    (last.modifiedFiles || 0) +
    ":" +
    (last._igResult ? "IG" : "") +
    ":" +
    (last._igResults ? last._igResults.length : 0) +
    ":" +
    (last._igError ? "IGE" : "") +
    ":" +
    (conv.title || "") +
    /* Autopilot run summaries live in a SIDECAR (conv.autopilotSummaries),
     * NOT in conv.messages — so a background-sync / settings-round-trip that
     * delivers a newly-concluded report leaves every message-derived term
     * above unchanged, and Guard 2 would skip the re-render that surfaces the
     * inline summary panel. Fold a cheap digest (count + per-run status/report
     * length) so an arriving/growing report bumps the fingerprint. */
    ":" +
    _apSummariesFp(conv)
  );
}
/* Cheap fingerprint of the autopilot run-summary sidecar — count of runs plus
 * each run's reason + report length, so a newly-arrived or newly-populated
 * concluded record changes the value and forces a re-render. */
function _apSummariesFp(conv) {
  const s = conv && conv.autopilotSummaries;
  if (!s || typeof s !== "object") return "0";
  const ids = Object.keys(s);
  let fp = ids.length + "|";
  for (const id of ids) {
    const r = s[id] || {};
    fp += (r.reason || "") + (r.content ? r.content.length : 0) + ";";
  }
  return fp;
}
let thinkingEnabled = true,
  fetchEnabled = true,
  codeExecEnabled = false,
  browserEnabled = false,
  desktopEnabled = false,
  memoryEnabled = true,
  schedulerEnabled = false,
  swarmEnabled = false,
  endpointEnabled = false,
  autopilotEnabled = false,
  activeFlow = "",   // "" | "builtin:endpoint" | "builtin:autopilot" | <orchId>
  imageGenEnabled = false,
  imageGenMode = false,
  humanGuidanceEnabled = false,
  searchMode = "multi",
  /* ★ Two-tier capability dial (chat/studio). The ONE user-facing toolbar
   * control; setChatMode() expands it into the atomic tool flags above
   * (mirrors the backend lib/tasks_pkg/chat_mode). 'studio' ⟺ a project is
   * attached, so the derived state stays truthful. Default 'chat' matches the
   * everyday all-rounder + DEFAULT_CHAT_MODE on the backend. (The old lean
   * 'air' tier merged into 'chat'; legacy air/pro normalise forward.) */
  chatMode = "chat",
  debugVisible = false,
  sidebarSearchQuery = "";
/* Boot-path localStorage reads must never throw: one corrupted key (hand
 * edit, a truncated write in private mode, an older schema) would otherwise
 * kill module evaluation and white-screen the whole app (pt_26a427d3). */
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
    thinkingEffort: "medium",
    imageMaxWidth: 0,           // 0 = follow server upload-shrink policy (recommended)
    systemPrompt: "",
    model: serverModel,
  },
);

/* ── (cost.js, debug_panel.js extracted here) ── */

function generateId() {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

/* Shared HH:MM clock formatter for message-bubble timestamps. Accepts an
 * epoch-ms timestamp (or falsy → now) and returns a locale HH:MM string.
 * Extracted from 5 copy-pasted `new Date(...).toLocaleTimeString([], {...})`
 * sites (streaming bubbles / translating bubble / SSE reconnect). */
function formatClockTime(ts) {
  return new Date(ts || Date.now()).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
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
 * server-assigned UUID overrides the temporary id (last-write-wins).
 *
 * `_ensureMsgId(msg)` is idempotent — safe to call repeatedly. */
function _newClientMsgId() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return 'tmp_' + crypto.randomUUID();
  }
  return 'tmp_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 10);
}
function _ensureMsgId(msg) {
  if (msg && typeof msg === 'object' && !msg._msgId) {
    msg._msgId = _newClientMsgId();
  }
  return msg;
}

/* ── (escape_html.js, error_envelope.js extracted here) ── */

/* Look up a conversation object by id. Tolerates the `conversations`
 * global not being ready yet (very early init) and returns null when the
 * id is falsy or unknown. Canonical replacement for the open-coded
 * `conversations.find((c) => c.id === X)` scattered across the frontend;
 * `getActiveConv()` delegates to it. */
function getConvById(id) {
  if (!id || typeof conversations === "undefined" || !Array.isArray(conversations)) return null;
  return conversations.find((c) => c && c.id === id) || null;
}
function getActiveConv() {
  return getConvById(activeConvId);
}
/* ★ Perf: cache chatContainer ref — avoids getElementById on every scroll check */
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
if (typeof window !== 'undefined') window._withInstantScroll = _withInstantScroll;

let _scrollRafId = null;
function scrollToBottom(force) {
  const c = _getChatContainer();
  if (!c) return;
  if (!force && !isNearBottom(200)) {
    /* Reader is scrolled up while content grows (e.g. live streaming) — no
     * scroll event fires, so refresh the scroll-to-bottom affordance here. */
    _updateScrollToBottomBtn();
    return;
  }
  /* ★ PERF: Coalesce scroll updates and use single rAF (not double).
   * During streaming, updateStreamingUI already runs inside a rAF callback
   * from twUpdate, so the DOM is already updated.  A single rAF is sufficient
   * to scroll after layout.  Double-rAF added 33ms of lag per frame. */
  if (_scrollRafId) return; // already scheduled
  _scrollRafId = requestAnimationFrame(() => {
    _scrollRafId = null;
    /* ★ SCROLL-JITTER FIX: during streaming this fires once per rAF while
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
/* One-time arming of the manual-scroll-up listeners that CLEAR the explicit
 * bottom latch: wheel-up and touch drag-down (= scroll content up) are the
 * user's "stop following the bottom" signal. Registered lazily on the first
 * click so the chat container is guaranteed to exist. */
let _stbLatchListenersArmed = false;
function _armStbLatchClearListeners() {
  if (_stbLatchListenersArmed) return;
  const c = _getChatContainer();
  if (!c) return;  // container not in DOM yet — retry on next click
  _stbLatchListenersArmed = true;
  c.addEventListener("wheel", (e) => {
    if (e.deltaY < 0) _explicitBottomLatch = null;
  }, { passive: true });
  let _touchY = null;
  c.addEventListener("touchstart", (e) => {
    _touchY = e.touches.length ? e.touches[0].clientY : null;
  }, { passive: true });
  c.addEventListener("touchmove", (e) => {
    if (_touchY != null && e.touches.length
        && e.touches[0].clientY > _touchY + 4) {
      _explicitBottomLatch = null;  // drag down = scroll content up
    }
  }, { passive: true });
}
function scrollChatToBottom() {
  /* ★ Explicit-bottom latch: this click is a COMMAND — "take me to the
   * latest". Latch it so EVERY render still to come during this open
   * (Phase-2 reconcile, .then fallback, background repaints) re-pins to the
   * TRUE bottom instead of the anchor / no-scroll-on-open heuristics, which
   * exist for unsolicited paints only. Cleared by manual scroll-up, an
   * explicit scrollToTurn navigation, a conversation switch, or open end. */
  const _latchConv = getActiveConv();
  if (_latchConv && typeof _explicitBottomLatch !== "undefined") {
    _explicitBottomLatch = _latchConv.id;
    _armStbLatchClearListeners();
  }
  /* ★ Hidden-tail restore (bounded-window fix). When the reader scrolled up
   * through history, `_loadOlderMessages` evicted the TAIL bubbles and
   * `_lazyRenderedTo` caps the window BELOW the newest message. A bare
   * `_forceScrollToBottom` then only reaches the bottom SENTINEL — the
   * IntersectionObserver drip-feeds one 20-message batch at a time INTO the
   * fold, and each insert strands the reader further above the real bottom
   * (the reported "scroll-to-bottom keeps bouncing me back to the middle").
   * Restore the tail FIRST, then scroll:
   *   • few hidden: walk the existing downward loader — continuous DOM, no
   *     repaint side effects (the same guarded pump scrollToTurn uses);
   *   • many hidden: cheaper to repaint just the tail window via
   *     ConvView.replaceAll. Pre-scroll to the old DOM's bottom first so
   *     renderChat's reader-parked-up anchor heuristic (meant for
   *     UNSOLICITED background repaints) does not override this EXPLICIT
   *     jump-to-bottom command. */
  const _stbConv = getActiveConv();
  if (_stbConv && !activeStreams.has(_stbConv.id)
      && typeof _loadNewerMessages === "function"
      && _lazyConvId === _stbConv.id
      && Number.isFinite(_lazyRenderedTo)
      && _lazyRenderedTo < _stbConv.messages.length) {
    const _hidden = _stbConv.messages.length - _lazyRenderedTo;
    const _PUMP_MAX = 60;  // 3 batches — beyond this a tail repaint is cheaper
    if (_hidden <= _PUMP_MAX) {
      let _guard = Math.ceil(_hidden / 20) + 2;
      while (_guard-- > 0 && Number.isFinite(_lazyRenderedTo)
             && _lazyRenderedTo < _stbConv.messages.length) {
        const _before = _lazyRenderedTo;
        _loadingNewer = false;
        _loadNewerMessages();
        if (_lazyRenderedTo === _before) break;  // stalled — never spin
      }
    } else if (window.ConvView && typeof window.ConvView.replaceAll === "function") {
      const _c = _getChatContainer();
      if (_c) _c.scrollTop = _c.scrollHeight;  // pre-pin: see comment above
      window.ConvView.replaceAll(_stbConv.id, { forceScroll: true });
      _updateScrollToBottomBtn();
      return;
    }
  }
  if (typeof _forceScrollToBottom === "function") {
    _forceScrollToBottom(null, true);
  } else {
    const c = _getChatContainer();
    if (c) c.scrollTop = c.scrollHeight;
  }
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
  window.scrollChatToBottom = scrollChatToBottom;
  window._updateScrollToBottomBtn = _updateScrollToBottomBtn;
}

function getToolRoundsFromMsg(msg) {
  // The inject sidecars must be rehydrated onto REAL rounds too, not only
  // onto the empty base — after a reload/poll/committedMessage projection a
  // turn normally has both. Returning `msg.toolRounds` directly here dropped
  // every swarm/peer/steer chip from exactly the common case.
  if (msg.toolRounds && msg.toolRounds.length > 0)
    return _rehydrateInjectRows(msg, msg.toolRounds);
  // ── Backward compat: old conversations stored under 'searchRounds' ──
  if (msg.searchRounds && msg.searchRounds.length > 0)
    return _rehydrateInjectRows(msg, msg.searchRounds);
  if (msg.searchResults && msg.searchResults.length > 0)
    return [
      {
        roundNum: 1,
        query: msg.searchQuery || "search",
        results: msg.searchResults,
        status: "done",
      },
    ];
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
 * reload / poll fallback / committedMessage projection, `msg.toolRounds` holds
 * ONLY real tool rounds, so we rebuild the synthetic rows here — on a COPY,
 * never mutating `msg.toolRounds` (so a later full-conv PUT can never leak them
 * back into the DB). Idempotent: if a live synthetic row already exists for a
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
          && !r._stallNudge && r.llmRound === anchorLlmRound) { at = i; break; }
    }
  }
  if (at >= 0) arr.splice(at, 0, row);
  else arr.push(row);
  return arr;
}
if (typeof window !== "undefined") window._spliceInjectRow = _spliceInjectRow;

function _rehydrateInjectRows(msg, base) {
  if (!msg) return base;
  const swarm = Array.isArray(msg._inboxInjects) ? msg._inboxInjects : [];
  const peer = Array.isArray(msg._peerInjects) ? msg._peerInjects : [];
  const steer = Array.isArray(msg._userSteerInjects) ? msg._userSteerInjects : [];
  const stall = Array.isArray(msg._stallNudges) ? msg._stallNudges : [];
  if (!swarm.length && !peer.length && !steer.length && !stall.length) return base;
  const out = base.slice();
  const _has = (pred) => out.some(pred);
  for (const s of swarm) {
    const rnd = s.round || 0;
    if (_has(r => r._inboxInject && r._inboxKey === "inbox:" + rnd)) continue;
    _spliceInjectRow(out, {
      roundNum: 9000000 + out.length,
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
      roundNum: 9000000 + out.length,
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
      roundNum: 9000000 + out.length,
      status: "done",
      _userSteerInject: true,
      _steerKey: "steer:" + rnd,
      steerRound: rnd,
      steerCount: s.count || 0,
      steerPreviews: Array.isArray(s.previews) ? s.previews : [],
    }, rnd - 1);
  }
  for (const s of stall) {
    const rnd = s.round || 0;
    if (_has(r => r._stallNudge && r._stallKey === "stall:" + rnd)) continue;
    _spliceInjectRow(out, {
      roundNum: 9000000 + out.length,
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
  window._rehydrateInjectRows = _rehydrateInjectRows;
}


/* ── (cross_tab_sync.js, conversations.js, cache_stats.js,
   markdown.js, health_stream_timer.js, toast.js extracted here) ── */
