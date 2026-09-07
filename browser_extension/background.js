/**
 * Tofu Browser Bridge — Background Service Worker (v5.4.4)
 *
 * Single-endpoint architecture:
 *   Every poll is a POST to /api/browser/poll with:
 *     Body:     { results: [{id, result, error}, ...] }
 *     Response: { commands: [{id, type, params}, ...] }
 *
 *   Results are piggy-backed on the next poll request.
 *   No separate result POST = no dropped packets through VSCode proxy.
 */

// ══════════════════════════════════════════
//  Configuration
// ══════════════════════════════════════════

const FETCH_TIMEOUT    = 12000;   // Abort fetch after 12s (server long-polls 8s)
const POLL_INTERVAL    = 100;     // ms between polls (server blocks, so no busy-loop)
const POLL_RETRY_DELAY = 3000;    // ms to wait after an error before retrying
const COMMAND_TIMEOUT  = 25000;   // Per-command execution timeout
// 401 handling: a wrong/missing bridge secret will NEVER succeed, so retrying
// at a fixed cadence just floods the server's auth log (measured ~400 401s per
// hour on 2026-08-01 from this very loop). Back off exponentially and park at a
// slow probe — the parked probe keeps self-healing alive for the case where the
// secret is fixed server-side, without the log spam.
const AUTH_RETRY_BASE_DELAY = 9000;    // first 401 retry (~POLL_RETRY_DELAY × 3)
const AUTH_RETRY_MAX_DELAY  = 300000;  // parked probe cadence (5 min)
const AUTH_GIVE_UP_AFTER    = 5;       // consecutive 401s → needs-re-pair state
const UPGRADE_RETRY_DELAY   = 300000;  // old protocol cannot heal by busy retry
// Some commands can legitimately take longer than the default; override here.
const COMMAND_TIMEOUT_OVERRIDES = {
  fetch_url: 35000,       // navigation + bounded SPA/network settle
  research_url: 80000,    // bounded scroll/pagination + network/body capture
  devtools: 35000,        // bounded console observation / debugger command
  screenshot_tab: 55000,  // full-page CDP capture + lazy-load wait
  wait_download: 65000,
  fetch_file_to_server: 125000, // bounded browser response → server stream
};
const PROTOCOL_VERSION = 2;
const BROWSER_CAPABILITIES = [
  'tabs', 'navigate', 'read', 'snapshot', 'click', 'fill', 'press',
  'select', 'scroll', 'wait', 'execute', 'iframes', 'network_capture',
  'network_body', 'deep_collect', 'research_hints', 'devtools_console', 'js_debugger',
  'upload', 'file_export', 'downloads', 'screenshot',
];
// Auto re-pair (owner decree 2026-08-04): the extension must NEVER send the
// user hunting for a bridge secret. A 401 kicks a silent re-pair ladder that
// mints a fresh agents:bridge key through the user's OWN Tofu session (an
// open Tofu tab's page context — already authenticated, the same grant the
// panel's mint button makes). With no Tofu tab open a hidden background tab
// is tried at most once per REPAIR_TAB_COOLDOWN; a FOREGROUND tab only ever
// opens from the popup's re-pair button (a real user gesture).
const REPAIR_TAB_COOLDOWN = 30 * 60 * 1000;  // hidden-tab repair, twice/hour cap

// ══════════════════════════════════════════
//  State
// ══════════════════════════════════════════

let SERVER_URL = '';
let CLIENT_ID = '';               // Stable per-device client identifier
let PROFILE_NAME = '';
let BRIDGE_SECRET = '';           // Owner-scoped agents:bridge credential
let pollActive = false;
let connected = false;
let lastError = '';
let authFailures = 0;             // consecutive 401s (reset on any success)
let needsRepair = false;          // parked: auto re-pair keeps running (see attemptAutoRepair)
let _retryTimer = null;           // pending setTimeout(poll) handle (cancelable)
let _repairInFlight = false;      // one repair ladder at a time
let _lastRepairTabAt = 0;         // last repair that had to OPEN a tab

// Chromium major version (parsed once at load). Reported to the server on every
// poll so the Tofu UI can surface Chrome 142+ "Local Network Access" prompt
// guidance — those prompts fire on the browser RUNNING the extension, so the
// version must come from HERE, not the (possibly different) UI viewer's UA.
let CHROME_MAJOR = 0;
try {
  const _cm = (navigator.userAgent || '').match(/Chrom(?:e|ium)\/(\d+)/);
  if (_cm) CHROME_MAJOR = parseInt(_cm[1], 10);
} catch (e) { /* navigator.userAgent unavailable in this context */ }

// Our OWN version, reported on every poll. The server compares it against
// the version it would serve in a fresh zip, which is what lets the panel
// tell "installed and healthy" from "installed but outdated" — and, when a
// poll dies at the bridge gate, "installed but locked out" from "never
// installed" (the stranded-fleet fix, 2026-08-04). Side-loaded extensions
// have no update channel, so this telemetry is the only way the panel can
// point a stale install at its one-click cure.
let EXT_VERSION = '';
try { EXT_VERSION = (chrome.runtime.getManifest() || {}).version || ''; } catch (e) { /* */ }

// Result-nudge: track the in-flight poll so a freshly-completed command can
// abort the idle long-poll and be delivered immediately (see executeAndReport).
let _activePollController = null;
let _flushPending = false;        // true ⇒ active poll aborted to flush a result

// Result queue: completed results waiting to be sent with next poll
const _resultQueue = [];        // [{id, result, error}, ...]
const _inflight = new Set();    // Command IDs currently executing
const POLL_RESULT_BATCH_MAX = 32;
let _pollResultBatchMax = POLL_RESULT_BATCH_MAX;
// The smallest server profile accepts a 16 MiB poll body. Keep exact UTF-8
// result bytes below 12 MiB so frame metadata and non-result fields retain
// headroom. This also prevents one screenshot from becoming a memory bomb.
const POLL_RESULT_BODY_MAX_BYTES = 12 * 1024 * 1024;
const POLL_RESULT_OVERSIZE_ERROR =
  `Browser command result exceeded the ${POLL_RESULT_BODY_MAX_BYTES / (1024 * 1024)} MiB poll transport limit`;

function _compactOversizeResult(candidate) {
  return {
    id: candidate && candidate.id,
    result: null,
    error: POLL_RESULT_OVERSIZE_ERROR,
  };
}

function _takeBoundedResultBatch() {
  const candidates = _resultQueue.splice(
    0, Math.min(_pollResultBatchMax, _resultQueue.length));
  const batch = [];
  let encodedBytes = 0;
  for (let index = 0; index < candidates.length; index += 1) {
    let candidate = candidates[index];
    let serialized = '';
    try {
      serialized = JSON.stringify(candidate);
    } catch (_) {
      candidate = _compactOversizeResult(candidate);
      serialized = JSON.stringify(candidate);
    }
    let candidateBytes = new TextEncoder().encode(serialized).byteLength;
    if (candidateBytes > POLL_RESULT_BODY_MAX_BYTES) {
      candidate = _compactOversizeResult(candidate);
      serialized = JSON.stringify(candidate);
      candidateBytes = new TextEncoder().encode(serialized).byteLength;
    }
    if (batch.length && encodedBytes + candidateBytes > POLL_RESULT_BODY_MAX_BYTES) {
      _resultQueue.unshift(...candidates.slice(index));
      break;
    }
    batch.push(candidate);
    encodedBytes += candidateBytes;
  }
  return batch;
}
// Response-body capture is deliberately bounded.  The payload is transient
// reconstructible transport data: at most 1 MiB per capture, 384 KiB per
// response, 80 metadata rows, and 12 recently navigated tabs.
const NETWORK_CAPTURE_MAX_ENTRIES = 80;
const NETWORK_CAPTURE_MAX_TRACKED_REQUESTS = 160;
const NETWORK_CAPTURE_MAX_BODY_CHARS = 384 * 1024;
const NETWORK_CAPTURE_MAX_TOTAL_BODY_CHARS = 1024 * 1024;
const NETWORK_CAPTURE_HINT_RESERVE_CHARS = 256 * 1024;
const NETWORK_CAPTURE_RECENT_TABS = 12;
const NETWORK_CAPTURE_MAX_WEBSOCKET_FRAMES = 40;
const NETWORK_CAPTURE_MAX_ACTIVE = 4;
const NETWORK_CAPTURE_SETTLE_MAX_MS = 4500;
const NETWORK_CAPTURE_IDLE_MS = 650;
const _networkCaptures = new Map(); // captureId -> owned transient capture
const _networkCaptureByTab = new Map(); // tabId -> automatic body-capture id
const _recentNetworkByTab = new Map(); // tabId -> bounded public snapshot
let _networkListenerInstalled = false;

// One extension-owned CDP attachment per tab. Network capture, screenshots,
// trusted input and DevTools commands take independent leases on this broker;
// the last lease detaches. This prevents a screenshot from tearing down an
// active console/network session and prevents two simultaneous attach calls
// from racing each other.
const _cdpSessions = new Map(); // tabId -> {target, holders, attachPromise, tail}
let _cdpLeaseSequence = 0;

// Console/debugger state is transient and explicitly bounded. It is useful
// only while diagnosing the page and is never written to extension storage.
const DEVTOOLS_MAX_ACTIVE_DEBUG_SESSIONS = 2;
const DEVTOOLS_MAX_OBSERVERS = 4;
const DEVTOOLS_MAX_LOG_ENTRIES = 200;
const DEVTOOLS_MAX_LOG_CHARS = 256 * 1024;
const DEVTOOLS_MAX_CONTEXTS = 80;
const DEVTOOLS_MAX_SCRIPTS = 120;
const DEVTOOLS_MAX_BREAKPOINTS = 24;
const DEVTOOLS_MAX_OBJECT_NODES = 400;
const DEVTOOLS_MAX_OBJECT_CHARS = 60 * 1024;
const DEVTOOLS_DEBUG_TTL_MS = 120000;
const DEVTOOLS_PAUSE_FAILSAFE_MS = 30000;
const DEVTOOLS_RECENT_TABS = 12;
const _devtoolsObservers = new Map(); // observerId -> bounded temporary sink
const _debugSessions = new Map(); // tabId -> persistent bounded debug session
const _recentDevtoolsByTab = new Map(); // tabId -> last bounded console snapshot

// MV3 event listeners are registered synchronously so Chrome can wake the
// service worker for debugger/tab lifecycle events.
chrome.debugger.onEvent.addListener(_onNetworkDebuggerEvent);
chrome.debugger.onEvent.addListener(_onDevtoolsDebuggerEvent);
chrome.debugger.onDetach.addListener(_onNetworkDebuggerDetach);
chrome.debugger.onDetach.addListener(_onCdpDebuggerDetach);
function _invalidateRecentNetworkOnUncapturedNavigation(details) {
  if (!details || Number(details.frameId) !== 0) return;
  const tabId = Number(details.tabId);
  if (!_networkCaptureByTab.has(tabId)) _recentNetworkByTab.delete(tabId);
}
chrome.webNavigation.onCommitted.addListener(
  _invalidateRecentNetworkOnUncapturedNavigation);
chrome.webNavigation.onHistoryStateUpdated.addListener(
  _invalidateRecentNetworkOnUncapturedNavigation);
chrome.webNavigation.onReferenceFragmentUpdated.addListener(
  _invalidateRecentNetworkOnUncapturedNavigation);
chrome.tabs.onRemoved.addListener((tabId) => {
  _recentNetworkByTab.delete(Number(tabId));
  _recentDevtoolsByTab.delete(Number(tabId));
  _stopDebugSession(Number(tabId), 'tab-closed').catch(() => {});
  for (const capture of Array.from(_networkCaptures.values())) {
    if (capture.tabId === Number(tabId)) {
      _stopNetworkCaptureInternal(
        capture.captureId, { remember: false }).catch(() => {});
    }
  }
});

// Stats
let commandsExecuted = 0;
let commandsFailed = 0;

// ══════════════════════════════════════════
//  Lifecycle
// ══════════════════════════════════════════

chrome.runtime.onInstalled.addListener(() => {
  console.log('[Bridge] onInstalled');
  init();
});

chrome.runtime.onStartup.addListener(() => {
  console.log('[Bridge] onStartup');
  init();
});

// Keep-alive: restart poll if Service Worker was killed and restarted
chrome.alarms.create('keepAlive', { periodInMinutes: 0.4 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === 'keepAlive' && !pollActive && SERVER_URL) {
    console.log('[Bridge] Alarm keepAlive: restarting poll loop');
    startPolling();
  }
});

// Zero-input pairing: a zip downloaded from the server carries
// bridge_preseed.json with a freshly-minted agents:bridge key + the server
// URL the browser used to reach it. Adopt it ONLY into empty slots — a
// user-configured value always wins, so re-downloading never clobbers a
// working setup. An absent file (dev-loaded from the repo) is normal: skip.
function adoptBridgePreseed(storageData) {
  if (storageData.bridgeSecret && storageData.serverUrl) {
    return Promise.resolve();
  }
  return fetch(chrome.runtime.getURL('bridge_preseed.json'))
    .then((r) => (r && r.ok ? r.json() : null))
    .then((pre) => {
      if (!pre || typeof pre !== 'object') return;
      if (!storageData.bridgeSecret &&
          typeof pre.bridgeSecret === 'string' && pre.bridgeSecret) {
        console.log('[Bridge] Adopting pre-paired bridge secret from the downloaded package');
        setBridgeSecret(pre.bridgeSecret);
      }
      if (!storageData.serverUrl &&
          typeof pre.serverUrl === 'string' && pre.serverUrl) {
        console.log('[Bridge] Adopting pre-paired server URL:', pre.serverUrl);
        chrome.storage.local.set({ serverUrl: pre.serverUrl });
      }
    })
    .catch(() => { /* no preseed in this package — manual pairing still works */ });
}

function init() {
  // Generate or restore a stable client ID for per-device command routing.
  // Then adopt the download-time preseed (if any) BEFORE server detection,
  // so a freshly-installed package pairs with zero user input.
  // v4 base keys: ['clientId', 'bridgeSecret', 'serverUrl']; profileName is
  // optional v5 identity metadata and never changes preseed adoption.
  chrome.storage.local.get(['clientId', 'bridgeSecret', 'serverUrl', 'profileName'], (data) => {
    if (data.clientId) {
      CLIENT_ID = data.clientId;
    } else {
      CLIENT_ID = crypto.randomUUID();
      chrome.storage.local.set({ clientId: CLIENT_ID });
    }
    BRIDGE_SECRET = data.bridgeSecret || '';
    PROFILE_NAME = data.profileName || '';
    console.log('[Bridge] Client ID:', CLIENT_ID,
                BRIDGE_SECRET ? '(bridge secret configured)' : '');
    adoptBridgePreseed(data).then(autoDetectServer);
  });
}

function setBridgeSecret(secret) {
  BRIDGE_SECRET = (secret || '').trim();
  chrome.storage.local.set({ bridgeSecret: BRIDGE_SECRET });
  console.log('[Bridge] Bridge secret', BRIDGE_SECRET ? 'set' : 'cleared');
  // The user may have just fixed a wrong secret: drop the backoff and cancel
  // a parked 5-minute probe so the new credentials are tried NOW.
  _resetAuthBackoff();
  if (pollActive) {
    if (_activePollController) { try { _activePollController.abort(); } catch (_) {} }
    _scheduleNextPoll(0);
  }
}

function buildHeaders() {
  const h = { 'Content-Type': 'application/json', 'Accept': 'application/json' };
  if (BRIDGE_SECRET) h['X-Bridge-Secret'] = BRIDGE_SECRET;
  return h;
}

function buildPollHeaders() {
  return {
    ...buildHeaders(),
    // Pre-auth recovery hint only. The authenticated JSON frame below remains
    // the authoritative protocol declaration.
    'X-Browser-Protocol-Version': String(PROTOCOL_VERSION),
  };
}

// ══════════════════════════════════════════
//  Server Detection
// ══════════════════════════════════════════

function autoDetectServer() {
  chrome.storage.local.get(['serverUrl'], (data) => {
    if (data.serverUrl) {
      setServer(data.serverUrl);
      return;
    }
    // Scan open tabs for a Tofu page
    chrome.tabs.query({}, (tabs) => {
      for (const tab of tabs) {
        if (tab.title && tab.title.includes('Tofu') && tab.url) {
          try {
            const u = new URL(tab.url);
            const origin = u.origin + (u.pathname.match(/^(\/proxy\/\d+)/)?.[1] || '');
            setServer(origin);
            return;
          } catch {}
        }
      }
    });
  });
}

function setServer(url) {
  url = url.replace(/\/+$/, '');
  if (url === SERVER_URL) return;
  SERVER_URL = url;
  console.log('[Bridge] Server:', SERVER_URL);
  chrome.storage.local.set({ serverUrl: url });
  registerOriginMarker(url);
  _resetAuthBackoff();
  stopPolling();
  startPolling();
}

/* The web app pins browser automation to THIS machine by reading the DOM
 * stamp left by origin_marker.js — without it the server sees an anonymous
 * fleet of polling devices (two computers, one account) and any can win a
 * given call. Registration must be dynamic: the paired origin is only known
 * at runtime. Same-id re-registration is a cheap replace. */
function registerOriginMarker(serverUrl) {
  if (!chrome.scripting || !chrome.scripting.registerContentScripts) return;
  let origin;
  try {
    origin = new URL(serverUrl).origin;
  } catch (e) {
    return;
  }
  chrome.scripting.registerContentScripts([{
    id: 'tofu-origin-marker',
    js: ['origin_marker.js'],
    matches: [origin + '/*'],
    runAt: 'document_start',
  }]).catch((e) => console.warn('[Bridge] origin marker registration failed:', e));
}

// ══════════════════════════════════════════
//  Auto re-pair — ZERO user input (owner decree 2026-08-04)
// ══════════════════════════════════════════
//
// The credential this bridge needs is an agents:bridge key, and minting one
// requires the user's OWN authenticated Tofu session — which this browser
// already has whenever a Tofu tab is open. So a stale key heals itself:
// run the panel's OWN mint call in the Tofu tab's page context and adopt
// what comes back. The user never sees a secret, never pastes anything,
// never opens a tunnel by hand.

/* Runs INSIDE the Tofu app tab (MAIN world). Uses the page's own API
 * client, so whatever auth the app carries (cookie session / SSO /
 * bearer) applies exactly as it does for the panel's mint button.
 * Returns {token} or {error}. */
function _tofuMintBridgeKey() {
  try {
    const api = window.Api;
    if (!api || !api.desktop || typeof api.desktop.mintToken !== 'function') {
      return Promise.resolve({ error: 'tofu-api-unavailable' });
    }
    return Promise.resolve(api.desktop.mintToken('browser-ext-autorepair'))
      .then((r) => ((r && r.token) ? { token: r.token } : { error: 'mint-refused' }))
      .catch((e) => ({ error: String(e) }));
  } catch (e) {
    return Promise.resolve({ error: String(e) });
  }
}

async function _mintKeyViaTab(tabId) {
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    world: 'MAIN',
    func: _tofuMintBridgeKey,
  });
  const r = results && results[0] && results[0].result;
  if (r && r.token) {
    console.log('[Bridge] Auto re-pair: fresh bridge key minted via a Tofu tab');
    setBridgeSecret(r.token);   // resets the auth backoff + polls immediately
    return true;
  }
  console.warn('[Bridge] Auto re-pair: mint via tab failed:', r && r.error);
  return false;
}

/* The repair ladder. Silent by design; the only visible surface is the
 * popup's repair row, whose button calls this with {forceTab:true}.
 *
 *   1. An already-open Tofu tab on OUR server → mint in its page context.
 *      Invisible, costs nothing, safe to run on every backed-off 401.
 *   2. No Tofu tab → open one ourselves (hidden in the background; a
 *      FOREGROUND tab only from the popup button's user gesture), mint,
 *      close it. Cooldown-bound so a permanently-dead server never flashes
 *      a tab every 5 minutes. A tab that lands on an SSO login wall fails
 *      the mint and is closed (hidden) or left open (foreground — the user
 *      signs in there and the next ladder run completes the re-pair). */
// A tab hosting the Tofu client itself. Own-server tabs are never
// navigation targets (replacing one would yank the chat out from under the
// user) and never working-tab candidates — the server skips the isClient
// rows cmdListTabs flags.
function _isOwnServerTab(tab) {
  return !!(tab && tab.url && SERVER_URL && tab.url.startsWith(SERVER_URL));
}

async function attemptAutoRepair(opts) {
  opts = opts || {};
  if (_repairInFlight || !SERVER_URL) return false;
  _repairInFlight = true;
  try {
    let tabs = [];
    try { tabs = await chrome.tabs.query({}); } catch (e) { /* tabs unavailable */ }
    const mine = tabs.filter((t) => t.id != null && _isOwnServerTab(t));
    for (const t of mine) {
      try {
        if (await _mintKeyViaTab(t.id)) return true;
      } catch (e) {
        console.warn('[Bridge] Auto re-pair: tab', t.id,
                     'not usable:', e && e.message);
      }
    }
    // A Tofu tab exists but refused the mint — opening another copy changes
    // nothing; the next backed-off probe retries this same ladder.
    if (mine.length) return false;
    const now = Date.now();
    if (!opts.forceTab && now - _lastRepairTabAt < REPAIR_TAB_COOLDOWN) {
      return false;
    }
    let tab = null;
    try {
      tab = await chrome.tabs.create({ url: SERVER_URL,
                                       active: !!opts.forceTab });
    } catch (e) {
      console.warn('[Bridge] Auto re-pair: could not open a Tofu tab:',
                   e && e.message);
      return false;
    }
    _lastRepairTabAt = now;
    try {
      await waitForTabLoad(tab.id, 20000);
      const ok = await _mintKeyViaTab(tab.id).catch(() => false);
      if (ok || !opts.forceTab) {
        try { await chrome.tabs.remove(tab.id); } catch (_) {}
      }
      // Foreground + failed: leave the tab open — the user completes the
      // sign-in there, and the next ladder run finishes the re-pair.
      return ok;
    } catch (e) {
      if (!opts.forceTab) {
        try { await chrome.tabs.remove(tab.id); } catch (_) {}
      }
      return false;
    }
  } finally {
    _repairInFlight = false;
  }
}
// ══════════════════════════════════════════
//  Polling — Single Endpoint
// ══════════════════════════════════════════

// Single pending-timer invariant: every path that schedules the next poll
// goes through here, so two timers can never coexist (a pre-existing double-
// loop hazard) and a user action (new secret / new server) can cancel a parked
// 5-minute probe and reconnect instantly.
function _scheduleNextPoll(delay) {
  if (_retryTimer) { clearTimeout(_retryTimer); _retryTimer = null; }
  if (pollActive) {
    _retryTimer = setTimeout(() => { _retryTimer = null; poll(); }, delay);
  }
}

function _resetAuthBackoff() {
  authFailures = 0;
  needsRepair = false;
}

function startPolling() {
  if (pollActive) return;
  if (!SERVER_URL) return;
  pollActive = true;
  console.log('[Bridge] Polling started');
  poll();
}

function stopPolling() {
  if (!pollActive) return;
  pollActive = false;
  if (_retryTimer) { clearTimeout(_retryTimer); _retryTimer = null; }
  console.log('[Bridge] Polling stopped');
}

async function poll() {
  if (!pollActive || !SERVER_URL) return;

  // Declared outside the try so the catch can restore them on a flush abort.
  let resultsToSend = [];
  let timeoutId = null;
  try {
    // Drain one bounded result batch. Remaining completions ride the next
    // poll; command settlement is idempotent and no result is discarded.
    resultsToSend = _takeBoundedResultBatch();

    const controller = new AbortController();
    _activePollController = controller;
    _flushPending = false;
    timeoutId = setTimeout(() => controller.abort(), FETCH_TIMEOUT);

    const resp = await fetch(`${SERVER_URL}/api/browser/poll`, {
      method: 'POST',
      signal: controller.signal,
      headers: buildPollHeaders(),
      // Carry the browser's OWN cookies for the server host: behind an
      // SSO-fronted gateway (cloud-IDE preview proxy) the bridge secret
      // alone can never pass the edge — the user's live SSO session can.
      // The <all_urls> host permission makes Chrome attach them on this
      // cross-origin extension fetch. This is what lets the bridge work
      // through such proxies with zero configuration.
      credentials: 'include',
      body: JSON.stringify({
        results: resultsToSend, clientId: CLIENT_ID, chromeMajor: CHROME_MAJOR,
        extVersion: EXT_VERSION, protocolVersion: PROTOCOL_VERSION,
        capabilities: BROWSER_CAPABILITIES, profile: PROFILE_NAME,
      }),
    });
    clearTimeout(timeoutId);
    _activePollController = null;

    if (!resp.ok) {
      if (resp.status === 401) {
        // Hold the results so they survive the re-pair.
        // Two DIFFERENT 401s land here and they are not fixed the same way:
        //   * Tofu's own bridge gate ({error:'bridge_auth_required'}) — the
        //     stored key is stale/revoked ⇒ silently mint a fresh one
        //     through the user's Tofu tab (attemptAutoRepair);
        //   * an SSO/proxy edge intercepting BEFORE Tofu — the poll now
        //     carries the browser's cookies, so a live SSO session passes on
        //     its own; a dead one recovers the next time a Tofu tab exists.
        // Neither is EVER fixed by the user pasting a secret by hand.
        _resultQueue.unshift(...resultsToSend);
        authFailures += 1;
        connected = false;
        needsRepair = authFailures >= AUTH_GIVE_UP_AFTER;
        const errBody = await resp.json().catch(() => null);
        const isBridgeAuth = !!(errBody && errBody.error === 'bridge_auth_required');
        lastError = isBridgeAuth
          ? (needsRepair
              ? `Bridge auth failed (401) ×${authFailures} — re-pairing automatically; an open Tofu tab finishes it instantly`
              : 'Bridge auth failed (401) — re-pairing automatically…')
          : (needsRepair
              ? `Bridge blocked by a proxy/SSO edge (401 ×${authFailures}) — it clears by itself once your Tofu panel is open in a tab`
              : 'Bridge blocked by a proxy/SSO edge (401) — retrying with your browser session…');
        updateBadge(needsRepair ? 'repair' : 'error');
        console.warn(`[Bridge] ${lastError}`);
        attemptAutoRepair().catch(() => {});
        const delay = Math.min(
          AUTH_RETRY_BASE_DELAY * (2 ** (authFailures - 1)),
          AUTH_RETRY_MAX_DELAY);
        _scheduleNextPoll(delay);
        return;
      }
      if (resp.status === 426) {
        // Authentication succeeded, but this binary cannot enter the strict
        // command protocol. Preserve completed results and park: retrying
        // every three seconds cannot upgrade a side-loaded extension and only
        // floods server diagnostics. The Local Control status endpoint sees
        // the rejected device and offers the current pre-paired ZIP.
        _resultQueue.unshift(...resultsToSend);
        _resetAuthBackoff();
        connected = false;
        const errBody = await resp.json().catch(() => null);
        const required = Number(
          errBody && errBody.requiredProtocolVersion) || '?';
        lastError = `Extension upgrade required (protocol ${PROTOCOL_VERSION} → ${required})`;
        updateBadge('repair');
        console.warn(`[Bridge] ${lastError}; parked until the next upgrade probe`);
        _scheduleNextPoll(UPGRADE_RETRY_DELAY);
        return;
      }
      if (resp.status === 429) {
        // Admission pressure is transient. Preserve every result, remain
        // visually connected, and obey the server instead of retrying harder.
        _resultQueue.unshift(...resultsToSend);
        const errBody = await resp.json().catch(() => null);
        const headerSeconds = Number(resp.headers.get('Retry-After')) || 0;
        const bodySeconds = Number(errBody && errBody.retryAfter) || 0;
        const delay = Math.min(
          AUTH_RETRY_MAX_DELAY,
          Math.max(1000, (headerSeconds || bodySeconds || 3) * 1000));
        connected = true;
        lastError = '';
        updateBadge('on');
        console.warn(`[Bridge] Server admission busy; retrying in ${Math.ceil(delay / 1000)}s`);
        _scheduleNextPoll(delay);
        return;
      }
      if (resp.status === 413) {
        // A lower proxy/server payload ceiling may be smaller than our known
        // server floor. Preserve ordinary results and bisect the batch first;
        // only a single result that still cannot fit becomes an explicit
        // command error, so replay can never loop forever.
        let payloadRetryDelay = 0;
        if (resultsToSend.length > 1) {
          _pollResultBatchMax = Math.max(
            1, Math.floor(resultsToSend.length / 2));
          _resultQueue.unshift(...resultsToSend);
        } else if (resultsToSend.length === 1
                   && resultsToSend[0].error !== POLL_RESULT_OVERSIZE_ERROR) {
          _resultQueue.unshift(...resultsToSend.map(_compactOversizeResult));
        } else {
          // Even the compact floor was rejected. Retain it for recovery but
          // back off, rather than manufacturing a zero-delay 413 loop.
          _resultQueue.unshift(...resultsToSend);
          payloadRetryDelay = POLL_RETRY_DELAY;
        }
        connected = true;
        lastError = '';
        updateBadge('on');
        _scheduleNextPoll(payloadRetryDelay);
        return;
      }
      if (resp.status >= 500) {
        // Proxy error — put results back so they're not lost
        _resultQueue.unshift(...resultsToSend);
        console.warn(`[Bridge] Server/proxy returned ${resp.status}, retrying...`);
        connected = true;
        _scheduleNextPoll(POLL_RETRY_DELAY);
        return;
      }
      throw new Error(`HTTP ${resp.status}`);
    }

    const data = await resp.json();
    connected = true;
    lastError = '';
    _resetAuthBackoff();
    updateBadge('on');

    // Fire-and-forget: do NOT await command execution
    if (data.commands && data.commands.length > 0) {
      for (const cmd of data.commands) {
        if (_inflight.has(cmd.id)) {
          console.warn(`[Bridge] Skipping duplicate command: ${cmd.id}`);
          continue;
        }
        executeAndReport(cmd);
      }
    }

    _scheduleNextPoll(POLL_INTERVAL);

  } catch (err) {
    if (timeoutId) clearTimeout(timeoutId);
    _activePollController = null;
    // The server may or may not have received the frame. Settlement is
    // owner/device/idempotency keyed, so replay is always safer than losing a
    // completed result on a timeout, proxy reset, malformed response, or
    // other transport error.
    if (resultsToSend.length) _resultQueue.unshift(...resultsToSend);
    if (err.name === 'AbortError') {
      if (_flushPending) {
        // Deliberate abort: a command result just landed, so we cut the idle
        // long-poll short. Re-poll INSTANTLY (not the 100ms reconnect path) so
        // the result goes out now instead of waiting the server's 8s window.
        _flushPending = false;
        _scheduleNextPoll(0);
        return;
      }
      // Fetch timeout — normal (server long-poll returned nothing), just reconnect
      connected = true;
      _scheduleNextPoll(POLL_INTERVAL);
      return;
    }

    connected = false;
    lastError = err.message || 'Connection failed';
    updateBadge('error');
    console.warn(`[Bridge] Poll error: ${lastError}`);
    _scheduleNextPoll(POLL_RETRY_DELAY);
  }
}

// ══════════════════════════════════════════
//  Command Execution (non-blocking)
// ══════════════════════════════════════════

async function executeAndReport(cmd) {
  _inflight.add(cmd.id);
  let result = null;
  let error = null;

  try {
    console.log(`[Bridge] ▶ ${cmd.type} (${cmd.id.slice(0, 8)})`);
    const start = Date.now();

    const timeoutMs = COMMAND_TIMEOUT_OVERRIDES[cmd.type] || COMMAND_TIMEOUT;
    result = await withTimeout(
      executeCommand(cmd.type, cmd.params || {}),
      timeoutMs,
      `Command '${cmd.type}' timed out after ${timeoutMs / 1000}s`
    );

    commandsExecuted++;
    console.log(`[Bridge] ✓ ${cmd.type} (${Date.now() - start}ms)`);
  } catch (err) {
    error = err.message || String(err);
    commandsFailed++;
    console.error(`[Bridge] ✗ ${cmd.type}: ${error}`);
  }

  // Queue the result — it will be sent with the next poll
  _resultQueue.push({ id: cmd.id, result, error });
  _inflight.delete(cmd.id);

  // ★ Result-nudge: if a long-poll is currently in-flight, abort it so a fresh
  // poll carries this result out immediately instead of waiting up to the
  // server's 8s long-poll window. _flushPending lets poll()'s catch distinguish
  // this deliberate abort from the 12s fetch-timeout abort (instant re-poll vs
  // the normal 100ms reconnect).
  if (_activePollController && pollActive) {
    _flushPending = true;
    try { _activePollController.abort(); } catch (_) {}
  }
}

function withTimeout(promise, ms, timeoutMsg) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(timeoutMsg)), ms);
    promise.then(
      (val) => { clearTimeout(timer); resolve(val); },
      (err) => { clearTimeout(timer); reject(err); },
    );
  });
}

// ══════════════════════════════════════════
//  Command Router
// ══════════════════════════════════════════

async function executeCommand(type, params) {
  switch (type) {
    case 'list_tabs':      return cmdListTabs(params);
    case 'read_tab':       return cmdReadTab(params);
    case 'execute_js':     return cmdExecuteJs(params);
    case 'screenshot_tab': return cmdScreenshotTab(params);
    case 'get_cookies':    return cmdGetCookies(params);
    case 'set_cookie':     return cmdSetCookie(params);
    case 'remove_cookie':  return cmdRemoveCookie(params);
    case 'get_history':    return cmdGetHistory(params);
    case 'get_bookmarks':  return cmdGetBookmarks(params);
    case 'create_tab':     return cmdCreateTab(params);
    case 'close_tab':      return cmdCloseTab(params);
    case 'update_tab':     return cmdUpdateTab(params);
    case 'navigate':       return cmdNavigate(params);
    case 'get_interactive_elements': return cmdGetInteractiveElements(params);
    case 'click_element':  return cmdClickElement(params);
    case 'hover_element':  return cmdHoverElement(params);
    case 'keyboard_input': return cmdKeyboardInput(params);
    case 'type_text':      return cmdTypeText(params);
    case 'scroll_page':    return cmdScrollPage(params);
    case 'go_back':        return cmdGoBack(params);
    case 'go_forward':     return cmdGoForward(params);
    case 'wait_for_element': return cmdWaitForElement(params);
    case 'summarize_page': return cmdSummarizePage(params);
    case 'get_app_state':  return cmdGetAppState(params);
    case 'download':       return cmdDownload(params);
    case 'notify':         return cmdNotify(params);
    case 'fetch_url':      return cmdFetchUrl(params);
    case 'fetch_file_to_server': return cmdFetchFileToServer(params);
    case 'research_url':   return cmdResearchUrl(params);
    case 'devtools':       return cmdDevtools(params);
    case 'page_state':     return cmdPageState(params);
    case 'page_snapshot':  return cmdPageSnapshot(params);
    case 'page_click':     return cmdPageClick(params);
    case 'page_fill':      return cmdPageFill(params);
    case 'page_press':     return cmdPagePress(params);
    case 'page_select':    return cmdPageSelect(params);
    case 'page_execute':   return cmdPageExecute(params);
    case 'page_upload':    return cmdPageUpload(params);
    case 'network_capture_start': return cmdNetworkCaptureStart(params);
    case 'network_capture_stop': return cmdNetworkCaptureStop(params);
    case 'wait_download':  return cmdWaitDownload(params);
    default:
      throw new Error(`Unknown command: ${type}`);
  }
}

// ══════════════════════════════════════════
//  Tab Commands
// ══════════════════════════════════════════

async function cmdListTabs(params) {
  const queryOpts = {};
  if (params.active !== undefined) queryOpts.active = params.active;
  if (params.currentWindow !== undefined) queryOpts.currentWindow = params.currentWindow;
  if (params.url) queryOpts.url = params.url;

  const tabs = await chrome.tabs.query(queryOpts);
  return tabs.map(t => ({
    id: t.id,
    title: t.title || '',
    url: t.url || '',
    active: t.active,
    windowId: t.windowId,
    index: t.index,
    status: t.status,
    pinned: t.pinned,
    isClient: _isOwnServerTab(t),
  }));
}

async function cmdReadTab(params) {
  const tabId = params.tabId;
  const selector = params.selector || null;
  const maxChars = params.maxChars || 50000;

  if (tabId == null) throw new Error('No tabId specified');

  let tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch (e) {
    throw new Error(`Tab ${tabId} not found: ${e.message}`);
  }
  if (tab.url && isProtectedUrl(tab.url)) {
    throw new Error(`Cannot read protected page: ${tab.url}`);
  }
  await _assertExpectedDomain(params, tab);

  // Wait for tab to finish loading
  if (tab.status !== 'complete') {
    await waitForTabLoad(tabId, 10000);
  }

  const target = { tabId };
  if (params.frameId != null) target.frameIds = [Number(params.frameId)];
  const results = await chrome.scripting.executeScript({
    target,
    func: _extractContent,
    args: [selector, maxChars],
  });

  if (results && results[0] && results[0].result) {
    const r = results[0].result;
    r.title = tab.title || '';
    r.url = tab.url || '';
    const network = _recentNetworkByTab.get(Number(tabId));
    if (network && network.pageUrl === (tab.url || '')) {
      r.network = network;
    } else if (network) {
      // Never attach a prior document's API data to the current page. Manual
      // browser navigations normally invalidate through webNavigation; this
      // exact-URL check is the fail-closed backstop for event races.
      _recentNetworkByTab.delete(Number(tabId));
    }
    return r;
  }

  return { text: '', title: tab.title || '', url: tab.url || '', error: 'No content extracted' };
}

function waitForTabLoad(tabId, maxWait = 10000) {
  return new Promise((resolve) => {
    const timeout = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      resolve();
    }, maxWait);

    const listener = (updatedId, changeInfo) => {
      if (updatedId === tabId && changeInfo.status === 'complete') {
        clearTimeout(timeout);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    };
    chrome.tabs.onUpdated.addListener(listener);

    chrome.tabs.get(tabId).then(t => {
      if (t.status === 'complete') {
        clearTimeout(timeout);
        chrome.tabs.onUpdated.removeListener(listener);
        resolve();
      }
    }).catch(() => {
      clearTimeout(timeout);
      chrome.tabs.onUpdated.removeListener(listener);
      resolve();
    });
  });
}

function _extractContent(selector, maxChars) {
  if (selector) {
    const elements = document.querySelectorAll(selector);
    const results = [];
    elements.forEach((el, i) => {
      if (i >= 100) return;
      results.push({
        tag: el.tagName.toLowerCase(),
        text: el.innerText || el.textContent || '',
        html: el.innerHTML.substring(0, 500),
        attrs: Object.fromEntries(
          Array.from(el.attributes).slice(0, 10).map(a => [a.name, a.value.substring(0, 200)])
        ),
      });
    });
    return { elements: results, count: elements.length };
  }

  // Page HTML is the PRIMARY payload: the server runs trafilatura/BS4
  // extraction on it (same pipeline as fetch_page_content) and discards
  // innerText whenever extraction succeeds. Cap at 2MB to avoid message bloat.
  const MAX_HTML = 2 * 1024 * 1024;
  let html = document.documentElement ? document.documentElement.outerHTML : '';
  let htmlTruncated = false;
  if (html.length > MAX_HTML) {
    html = html.substring(0, MAX_HTML);
    htmlTruncated = true;
  }

  const meta = {};
  document.querySelectorAll('meta').forEach(m => {
    const name = m.getAttribute('name') || m.getAttribute('property');
    if (name) meta[name] = (m.getAttribute('content') || '').substring(0, 200);
  });

  // innerText is only a FALLBACK for when HTML is too small for the server to
  // extract from (server gates extraction on html.length > 200). read_tab waits
  // for load, so outerHTML reflects the live post-render DOM — a real content
  // page (incl. a rendered SPA) always has substantial HTML. Below this
  // threshold the page is an empty/error/redirect shell, so we ship innerText
  // and skip its (reflow-inducing) computation entirely on the common path.
  const MIN_HTML_FOR_EXTRACT = 2048;
  const out = { html, htmlTruncated, meta };
  if (html.length < MIN_HTML_FOR_EXTRACT) {
    let text = document.body ? (document.body.innerText || document.body.textContent || '') : '';
    out.textLength = text.length;
    out.truncated = false;
    if (text.length > maxChars) {
      text = text.substring(0, maxChars);
      out.truncated = true;
    }
    out.text = text;
  }
  return out;
}

// ══════════════════════════════════════════
//  Execute JS — MV3 Compliant
// ══════════════════════════════════════════

async function cmdExecuteJs(params) {
  const tabId = params.tabId;
  const code = params.code;

  if (tabId == null) throw new Error('No tabId specified');
  if (!code) throw new Error('No code specified');

  let tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch (e) {
    throw new Error(`Tab ${tabId} not found: ${e.message}`);
  }

  if (tab.url && isProtectedUrl(tab.url)) {
    throw new Error(`Cannot execute JS in protected page: ${tab.url}`);
  }

  // Try MAIN world first (full page context), fall back to ISOLATED
  for (const world of ['MAIN', 'ISOLATED']) {
    try {
      const results = await chrome.scripting.executeScript({
        target: _pageTarget(params),
        world,
        func: _executeInPage,
        args: [code],
      });

      if (results && results[0]) {
        const r = results[0].result;
        if (r && r.__error && world === 'MAIN' &&
            (r.message.includes('Content Security Policy') ||
             r.message.includes('unsafe-eval') ||
             r.message.includes("'eval'"))) {
          console.log(`[Bridge] MAIN world blocked by CSP on tab ${tabId}, trying ISOLATED`);
          continue;
        }
        return r;
      }
      return null;
    } catch (e) {
      if (world === 'MAIN') {
        console.log(`[Bridge] MAIN world failed on tab ${tabId}: ${e.message}, trying ISOLATED`);
        continue;
      }
      throw new Error(`JS execution failed: ${e.message}`);
    }
  }
  throw new Error('JS execution failed in both MAIN and ISOLATED worlds');
}

function _executeInPage(code) {
  try {
    const indirectEval = eval;
    const result = indirectEval(code);

    if (result && typeof result === 'object' && typeof result.then === 'function') {
      return result.then(v => {
        try { return JSON.parse(JSON.stringify(v)); } catch { return String(v); }
      }).catch(e => ({ __error: true, message: e.message || String(e) }));
    }

    try { return JSON.parse(JSON.stringify(result)); } catch { return String(result); }
  } catch (e) {
    return { __error: true, message: e.message || String(e) };
  }
}

// ══════════════════════════════════════════
//  Screenshot
// ══════════════════════════════════════════
//
// Two modes:
//   fullPage=true  (default) — uses chrome.debugger + CDP Page.captureScreenshot
//                              with captureBeyondViewport:true. Captures the
//                              ENTIRE scrollable page in one shot, triggering
//                              lazy-loaded content as it renders.
//                              Shows Chrome's "extension is debugging" banner
//                              while attached (detached immediately after).
//   fullPage=false — legacy chrome.tabs.captureVisibleTab path (viewport only).
//                    No debugger banner; used as automatic fallback if CDP
//                    fails (e.g. DevTools already attached to the tab).

const FULL_PAGE_MAX_HEIGHT_PX = 16000;  // Chrome texture/CDP safety cap

async function cmdScreenshotTab(params) {
  await _assertExpectedDomain(params);
  const format   = params.format || 'png';
  const quality  = params.quality || 80;
  const fullPage = params.fullPage !== false;  // default true

  // Resolve tabId — CDP requires an explicit tabId, so fetch the active one
  // if the caller didn't specify.
  let tabId = params.tabId;
  if (tabId == null) {
    const [activeTab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!activeTab) throw new Error('No active tab available for screenshot');
    tabId = activeTab.id;
  }

  // Both CDP paths screenshot the target tab IN THE BACKGROUND — no tab
  // activation, no focus stealing. Only the last-resort captureVisibleTab path
  // must bring the tab to the front (the visible "navigation" flicker), so it
  // runs solely when every CDP attempt fails (e.g. DevTools already attached).
  if (fullPage) {
    try {
      return await _screenshotFullPageCDP(tabId, format, quality);
    } catch (err) {
      console.warn('[Screenshot] Full-page CDP failed, trying viewport CDP:', err && err.message);
      try {
        const res = await _screenshotViewportCDP(tabId, format, quality);
        res.fallbackReason = String((err && err.message) || err || 'full-page CDP failed');
        return res;
      } catch (err2) {
        console.warn('[Screenshot] Viewport CDP failed, falling back to captureVisibleTab:', err2 && err2.message);
        const res = await _screenshotViewport(tabId, format, quality);
        res.fullPage = false;
        res.fallbackReason = String((err2 && err2.message) || err2 || 'CDP unavailable');
        return res;
      }
    }
  }

  // Viewport-only request: still prefer the background CDP capture so we don't
  // yank the tab to the foreground; only fall back to captureVisibleTab if CDP
  // can't attach.
  try {
    return await _screenshotViewportCDP(tabId, format, quality);
  } catch (err) {
    console.warn('[Screenshot] Viewport CDP failed, falling back to captureVisibleTab:', err && err.message);
    const res = await _screenshotViewport(tabId, format, quality);
    res.fallbackReason = String((err && err.message) || err || 'CDP unavailable');
    return res;
  }
}

// A desktop-class viewport width forced via CDP so full-page capture is
// DECOUPLED from the user's real window size. If the user shrinks the window,
// a responsive page reflows to a narrow/mobile layout (or skips rendering
// off-viewport content); overriding device metrics to a stable large viewport
// makes it re-render the full desktop layout before we capture. Height floor
// gives lazy-loaded content a tall "viewport" so it triggers on reflow.
const FULL_PAGE_OVERRIDE_MIN_WIDTH_PX  = 1280;
const FULL_PAGE_OVERRIDE_MIN_HEIGHT_PX = 800;

// Layout-stability convergence params: after forcing the viewport we must wait
// for the page to finish reflowing AND for async result lists (flights, tickets)
// to render — a fixed sleep would either truncate a slow list or waste time on a
// fast one. We poll getLayoutMetrics until the content size stops changing for
// STABLE_READS consecutive polls (and readyState is 'complete'), capped so a
// perpetually-animating page can't hang the capture.
const STABILITY_MAX_WAIT_MS   = 4000;
const STABILITY_POLL_MS       = 200;
const STABILITY_STABLE_READS  = 2;   // consecutive unchanged reads to declare stable

// Poll until the CDP-reported content size is stable across STABLE_READS polls
// and document.readyState is 'complete', or the budget elapses. Returns the
// reason so the caller can log convergence vs timeout.
async function _waitForContentStable(target) {
  const deadline = Date.now() + STABILITY_MAX_WAIT_MS;
  let prevW = -1, prevH = -1;
  let stableCount = 0;
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, STABILITY_POLL_MS));

    let cs;
    try {
      const m = await chrome.debugger.sendCommand(target, 'Page.getLayoutMetrics');
      cs = m.cssContentSize || m.contentSize || { width: 0, height: 0 };
    } catch (e) {
      // A transient metrics error shouldn't abort — keep trying until deadline.
      continue;
    }

    let ready = true;
    try {
      const r = await chrome.debugger.sendCommand(target, 'Runtime.evaluate', {
        expression: "document.readyState === 'complete'",
        returnByValue: true,
      });
      ready = !!(r && r.result && r.result.value);
    } catch (e) {
      // If readyState can't be read, fall back to size-stability alone.
      ready = true;
    }

    const w = Math.ceil(cs.width);
    const h = Math.ceil(cs.height);
    if (ready && w === prevW && h === prevH) {
      stableCount += 1;
      if (stableCount >= STABILITY_STABLE_READS) {
        return { stable: true, width: w, height: h, waitedMs: STABILITY_MAX_WAIT_MS - (deadline - Date.now()) };
      }
    } else {
      stableCount = 0;
    }
    prevW = w;
    prevH = h;
  }
  return { stable: false, width: prevW, height: prevH, waitedMs: STABILITY_MAX_WAIT_MS };
}

async function _screenshotFullPageCDP(tabId, format, quality) {
  return _cdpRun(tabId, async (target) => {
    let overridden = false;
    try {

      // Page domain must be enabled before layout/screenshot commands
      await chrome.debugger.sendCommand(target, 'Page.enable');

    // Read the content size FIRST so we can size the forced viewport to it.
    const pre = await chrome.debugger.sendCommand(target, 'Page.getLayoutMetrics');
    const preCs = pre.cssContentSize || pre.contentSize || { width: 0, height: 0 };

    // Force a stable desktop viewport that is never smaller than the content —
    // independent of how small the user shrank the real window. deviceScaleFactor:1
    // and mobile:false keep it a plain desktop render at native resolution.
    const overrideWidth = Math.min(
      Math.max(Math.ceil(preCs.width), FULL_PAGE_OVERRIDE_MIN_WIDTH_PX),
      FULL_PAGE_MAX_HEIGHT_PX,
    );
    const overrideHeight = Math.min(
      Math.max(Math.ceil(preCs.height), FULL_PAGE_OVERRIDE_MIN_HEIGHT_PX),
      FULL_PAGE_MAX_HEIGHT_PX,
    );
    try {
      await chrome.debugger.sendCommand(target, 'Emulation.setDeviceMetricsOverride', {
        width: overrideWidth,
        height: overrideHeight,
        deviceScaleFactor: 1,
        mobile: false,
        screenWidth: overrideWidth,
        screenHeight: overrideHeight,
      });
      overridden = true;
      // Wait for the page to converge to the forced viewport instead of a
      // fixed sleep: reflow + async result lists (flights/tickets) may render
      // well after 350ms, and a fixed delay would capture a half-loaded page.
      const stab = await _waitForContentStable(target);
      if (!stab.stable) {
        console.warn('[Screenshot] content did not stabilize within budget; capturing best-effort at', stab.width + 'x' + stab.height);
      }
    } catch (errOverride) {
      // Non-fatal: if the override is rejected we still capture, just without
      // the window-size decoupling (better a viewport-derived shot than none).
      console.warn('[Screenshot] setDeviceMetricsOverride failed, capturing without override:', errOverride && errOverride.message);
      overridden = false;
    }

    // Re-measure AFTER the reflow so the clip matches the forced layout.
    const metrics = await chrome.debugger.sendCommand(target, 'Page.getLayoutMetrics');
    // Prefer CSS content size (Chromium 90+); fall back to legacy contentSize.
    const cs = metrics.cssContentSize || metrics.contentSize || { width: 0, height: 0 };
    const width  = Math.max(1, Math.ceil(cs.width));
    const height = Math.max(1, Math.ceil(cs.height));
    const clipHeight = Math.min(height, FULL_PAGE_MAX_HEIGHT_PX);

    const shotParams = {
      format,
      captureBeyondViewport: true,
      fromSurface: true,
      clip: { x: 0, y: 0, width, height: clipHeight, scale: 1 },
    };
    if (format === 'jpeg') shotParams.quality = quality;

    const shot = await chrome.debugger.sendCommand(target, 'Page.captureScreenshot', shotParams);
    if (!shot || !shot.data) throw new Error('CDP returned empty screenshot');

      const mime = format === 'jpeg' ? 'image/jpeg' : 'image/png';
      return {
        dataUrl: `data:${mime};base64,${shot.data}`,
        format,
        fullPage: true,
        width,
        height: clipHeight,
        contentHeight: height,
        truncatedHeight: height > FULL_PAGE_MAX_HEIGHT_PX,
      };
    } finally {
      // ALWAYS clear the override before releasing this lease. The broker may
      // keep the shared attachment alive for network/console capture.
      if (overridden) {
        try {
          await chrome.debugger.sendCommand(target, 'Emulation.clearDeviceMetricsOverride');
        } catch (errClear) {
          console.warn('[Screenshot] clearDeviceMetricsOverride failed:', errClear && errClear.message);
        }
      }
    }
  }, 'full-page-screenshot');
}

// Background viewport capture via CDP — captures the tab's current viewport
// WITHOUT activating/focusing it (unlike chrome.tabs.captureVisibleTab, which
// can only grab the foreground tab). captureBeyondViewport:false keeps it to
// the visible area, so it's fast and never triggers the tab-switch flicker.
async function _screenshotViewportCDP(tabId, format, quality) {
  return _cdpRun(tabId, async (target) => {
    await chrome.debugger.sendCommand(target, 'Page.enable');

    const shotParams = { format, captureBeyondViewport: false, fromSurface: true };
    if (format === 'jpeg') shotParams.quality = quality;

    const shot = await chrome.debugger.sendCommand(target, 'Page.captureScreenshot', shotParams);
    if (!shot || !shot.data) throw new Error('CDP returned empty screenshot');

    const mime = format === 'jpeg' ? 'image/jpeg' : 'image/png';
    return {
      dataUrl: `data:${mime};base64,${shot.data}`,
      format,
      fullPage: false,
    };
  }, 'viewport-screenshot');
}

async function _screenshotViewport(tabId, format, quality) {
  // Remember which tab was active so we can switch back
  let originalTabId = null;
  let targetWindowId = null;

  if (tabId) {
    const targetTab = await chrome.tabs.get(tabId);
    targetWindowId = targetTab.windowId;

    const [activeTab] = await chrome.tabs.query({ active: true, windowId: targetWindowId });
    if (activeTab) originalTabId = activeTab.id;

    // Activate the target tab (required by captureVisibleTab)
    if (originalTabId !== tabId) {
      await chrome.tabs.update(tabId, { active: true });
      await new Promise(r => setTimeout(r, 500));  // Wait for render
    }
  }

  const opts = { format };
  if (format === 'jpeg') opts.quality = quality;

  try {
    const dataUrl = await chrome.tabs.captureVisibleTab(targetWindowId, opts);

    // Switch back to the original tab silently
    if (originalTabId && originalTabId !== tabId) {
      await chrome.tabs.update(originalTabId, { active: true });
    }

    return { dataUrl, format, fullPage: false };
  } catch (err) {
    // Switch back even on error
    if (originalTabId && originalTabId !== tabId) {
      try { await chrome.tabs.update(originalTabId, { active: true }); } catch {}
    }
    throw err;
  }
}

// ══════════════════════════════════════════
//  Get Interactive Elements
// ══════════════════════════════════════════

async function cmdGetInteractiveElements(params) {
  const tabId = params.tabId;
  if (tabId == null) throw new Error('No tabId specified');

  let tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch (e) {
    throw new Error(`Tab ${tabId} not found: ${e.message}`);
  }

  if (tab.url && isProtectedUrl(tab.url)) {
    throw new Error(`Cannot read protected page: ${tab.url}`);
  }

  if (tab.status !== 'complete') {
    await waitForTabLoad(tabId, 10000);
  }

  const enumerate = () => chrome.scripting.executeScript({
    target: { tabId },
    func: _getInteractiveElements,
    args: [params.maxElements || 200, params.viewport || false],
  });

  let results = await enumerate();

  /* SPA shell race: tab.status==='complete' only means the INITIAL document
   * loaded; XHR-driven rendering lands later and an enumeration fired in the
   * gap returns zero elements, which the model experiences as "no element
   * matches" on a page that visibly has buttons. One bounded settle +
   * re-enumeration closes the gap; an empty second result is the truth. */
  let first = results && results[0] && results[0].result;
  if (!first || !Array.isArray(first.elements) || first.elements.length === 0) {
    await new Promise((resolve) => setTimeout(resolve, 1200));
    results = await enumerate();
  }

  if (results && results[0] && results[0].result) {
    const r = results[0].result;
    r.title = tab.title || '';
    r.url = tab.url || '';
    return r;
  }
  return { elements: [], title: tab.title || '', url: tab.url || '' };
}

function _getInteractiveElements(maxElements, viewportOnly) {
  // ★ SOTA Element Indexing System (Set-of-Marks style)
  // Each element gets a stable numeric index. LLM only needs to say click(3) instead of a long CSS selector.
  const selectors = [
    'a[href]',
    'button',
    'input',
    'select',
    'textarea',
    '[role="button"]',
    '[role="link"]',
    '[role="tab"]',
    '[role="menuitem"]',
    '[role="option"]',
    '[role="checkbox"]',
    '[role="radio"]',
    '[role="switch"]',
    '[onclick]',
    '[ng-click]',
    '[v-on\\:click]',
    '[@click]',
    'summary',
    'details',
    '[tabindex]',
    '[contenteditable="true"]',
  ];

  const allEls = document.querySelectorAll(selectors.join(','));
  const elements = [];
  const selectorMap = {};  // index → selector (for server-side caching)
  const seen = new Set();      // selector-string dedup
  const seenEls = new Set();   // element-identity dedup across both passes
  let index = 1;  // 1-based index

  const _isVisible = (el) => {
    if (el.offsetWidth === 0 && el.offsetHeight === 0) return false;
    const style = window.getComputedStyle(el);
    return !(style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0');
  };
  const _inViewport = (el) => {
    const rect = el.getBoundingClientRect();
    return !(rect.bottom < 0 || rect.top > window.innerHeight ||
             rect.right < 0 || rect.left > window.innerWidth);
  };

  // Build a concise CSS selector for an element
  const _conciseSelector = (el) => {
    if (el.id) return `#${CSS.escape(el.id)}`;
    const tag = el.tagName.toLowerCase();
    const classes = Array.from(el.classList).slice(0, 3).map(c => `.${CSS.escape(c)}`).join('');
    const nthType = (() => {
      const siblings = el.parentElement ? Array.from(el.parentElement.children).filter(s => s.tagName === el.tagName) : [];
      if (siblings.length <= 1) return '';
      const idx = siblings.indexOf(el) + 1;
      return `:nth-of-type(${idx})`;
    })();
    let selector = tag + classes + nthType;
    // Make it more specific by prepending parent
    if (el.parentElement && el.parentElement !== document.body && el.parentElement !== document.documentElement) {
      const parent = el.parentElement;
      if (parent.id) {
        selector = `#${CSS.escape(parent.id)} > ${selector}`;
      } else {
        const ptag = parent.tagName.toLowerCase();
        const pcls = Array.from(parent.classList).slice(0, 2).map(c => `.${CSS.escape(c)}`).join('');
        selector = ptag + pcls + ' > ' + selector;
      }
    }
    return selector;
  };

  // Gather useful info + push (shared by both passes). Returns true when added.
  const _pushElement = (el, extra) => {
    const selector = _conciseSelector(el);
    if (seen.has(selector)) return false;
    seen.add(selector);
    seenEls.add(el);
    const text = (el.innerText || el.textContent || '').trim().substring(0, 100);
    const tag = el.tagName.toLowerCase();
    const info = { index, selector, tag, text };
    if (el.href) info.href = el.href.substring(0, 200);
    if (el.type) info.type = el.type;
    if (el.name) info.name = el.name;
    if (el.value && tag === 'input') info.value = el.value.substring(0, 100);
    if (el.placeholder) info.placeholder = el.placeholder.substring(0, 100);
    if (el.getAttribute('aria-label')) info.ariaLabel = el.getAttribute('aria-label').substring(0, 100);
    if (el.getAttribute('title')) info.title = el.getAttribute('title').substring(0, 100);
    if (el.disabled) info.disabled = true;
    if (el.getAttribute('role')) info.role = el.getAttribute('role');
    if (el.checked !== undefined) info.checked = el.checked;
    if (el.selectedIndex !== undefined && tag === 'select') {
      info.selectedOption = el.options[el.selectedIndex]?.text?.substring(0, 50) || '';
    }
    if (extra) Object.assign(info, extra);

    // Position info (viewport-relative coordinates)
    const rect = el.getBoundingClientRect();
    info.rect = {
      x: Math.round(rect.x), y: Math.round(rect.y),
      w: Math.round(rect.width), h: Math.round(rect.height)
    };

    // ★ Store mapping: index → selector
    selectorMap[index] = selector;
    elements.push(info);
    index++;
    return true;
  };

  for (const el of allEls) {
    if (elements.length >= maxElements) break;
    if (!_isVisible(el)) continue;
    if (viewportOnly && !_inViewport(el)) continue;
    _pushElement(el);
  }

  // ── cursor:pointer sweep (v4.8) ─────────────────────────────────────
  // SPA frameworks (React/Vue/Angular) attach listeners at the ROOT, so a
  // clickable CARD is a plain <div> whose ONLY tell is the computed cursor.
  // Without this sweep such a page enumerates ZERO elements, text= clicks
  // can never resolve, and the model burns rounds on JS DOM archaeology
  // (the 2026-08-05 钱管家 card incident, conv msft42tqheea8x).
  const POINTER_SCAN_BUDGET = 8000;  // worst-case nodes to style-scan
  const cursorMemo = new Map();      // element → computed cursor (shared ancestors)
  const _cursorOf = (n) => {
    let c = cursorMemo.get(n);
    if (c === undefined) {
      c = window.getComputedStyle(n).cursor;
      cursorMemo.set(n, c);
    }
    return c;
  };
  let scanned = 0;
  let pointerAdded = 0;
  const descendants = document.body ? document.body.querySelectorAll('*') : [];
  for (const el of descendants) {
    if (elements.length >= maxElements || scanned >= POINTER_SCAN_BUDGET) break;
    scanned++;
    if (seenEls.has(el)) continue;
    if (el.offsetWidth === 0 && el.offsetHeight === 0) continue;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
    if (style.cursor !== 'pointer') continue;
    cursorMemo.set(el, 'pointer');
    // cursor INHERITS: every descendant of a clickable card also reports
    // pointer — keep only the OUTERMOST one (the card itself), or every
    // card would flood the list with its own children.
    let nested = false;
    for (let p = el.parentElement; p && p !== document.documentElement; p = p.parentElement) {
      if (_cursorOf(p) === 'pointer') { nested = true; break; }
    }
    if (nested) continue;
    if (viewportOnly && !_inViewport(el)) continue;
    if (_pushElement(el, { pointer: true })) pointerAdded++;
  }

  // Canvas detection
  const canvases = document.querySelectorAll('canvas');
  const svgs = document.querySelectorAll('svg');
  const canvasDetected = canvases.length > 0 && elements.length < 10;

  // ★ Return selectorMap for server-side caching
  const result = { elements, total: allEls.length + pointerAdded, selectorMap };
  if (pointerAdded) result.pointerSweep = { scanned, added: pointerAdded };

  // ★ Page scroll info
  result.scroll = {
    scrollY: Math.round(window.scrollY),
    scrollHeight: document.documentElement.scrollHeight,
    viewportHeight: window.innerHeight,
    viewportWidth: window.innerWidth,
    scrollPercent: Math.round((window.scrollY / Math.max(1, document.documentElement.scrollHeight - window.innerHeight)) * 100),
  };

  if (canvasDetected) {
    result.canvasDetected = true;
    result.canvasCount = canvases.length;
    result.svgCount = svgs.length;
    result.hint = "⚠️ This page uses Canvas/SVG rendering. Use browser_screenshot to see layout, browser_execute_js to access app data.";
  }
  return result;
}

// ══════════════════════════════════════════
//  Summarize Page
// ══════════════════════════════════════════

async function cmdSummarizePage(params) {
  const tabId = params.tabId;
  if (tabId == null) throw new Error('No tabId specified');

  let tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch (e) {
    throw new Error(`Tab ${tabId} not found: ${e.message}`);
  }

  if (tab.url && isProtectedUrl(tab.url)) {
    throw new Error(`Cannot read protected page: ${tab.url}`);
  }

  if (tab.status !== 'complete') {
    await waitForTabLoad(tabId, 10000);
  }

  const results = await chrome.scripting.executeScript({
    target: { tabId },
    func: _summarizePage,
    args: [],
  });

  if (results && results[0] && results[0].result) {
    const r = results[0].result;
    r.title = tab.title || '';
    r.url = tab.url || '';
    return r;
  }
  return { error: 'Failed to summarize page' };
}

function _summarizePage() {
  const detectFramework = () => {
    if (window.__VUE_DEVTOOLS_GLOBAL_HOOK__ || window.Vue) return 'Vue';
    if (window.__REACT_DEVTOOLS_GLOBAL_HOOK__ || window.React) return 'React';
    if (window.angular) return 'Angular';
    if (window.jQuery) return 'jQuery';
    if (window.graph?.getNodes || window.G6) return 'G6 (Graph)';
    if (window.echarts) return 'ECharts';
    if (window.d3) return 'D3';
    return 'Unknown/Vanilla';
  };

  const getSelector = (el) => {
    if (el.id) return '#' + CSS.escape(el.id);
    const tag = el.tagName.toLowerCase();
    const classes = Array.from(el.classList).slice(0, 2).map(c => '.' + CSS.escape(c)).join('');
    return tag + classes;
  };

  const canvases = document.querySelectorAll('canvas');
  const svgs = document.querySelectorAll('svg');

  return {
    title: document.title,
    url: location.href,
    framework: detectFramework(),
    canvasCount: canvases.length,
    svgCount: svgs.length,
    domElementCount: document.documentElement.querySelectorAll('*').length,
    mainButtons: Array.from(document.querySelectorAll('button, [role="button"], [onclick]'))
      .slice(0, 20)
      .map(el => ({ text: (el.innerText || el.textContent || '').trim().substring(0, 50), selector: getSelector(el) })),
    mainLinks: Array.from(document.querySelectorAll('a[href]'))
      .slice(0, 20)
      .map(el => ({ text: (el.innerText || el.textContent || '').trim().substring(0, 50), href: el.href })),
    forms: Array.from(document.querySelectorAll('form'))
      .map(f => ({
        action: f.action,
        method: f.method,
        inputCount: f.querySelectorAll('input,select,textarea,button').length
      })),
    tables: Array.from(document.querySelectorAll('table'))
      .map(t => ({ rows: t.rows?.length || 0, cols: t.rows[0]?.cells?.length || 0 })),
    hasModal: !!(document.querySelector('[role="dialog"]') || document.querySelector('.modal, .popup, [class*="modal"], [class*="dialog"]')),
    inputs: Array.from(document.querySelectorAll('input:not([type="hidden"]), textarea'))
      .slice(0, 15)
      .map(el => ({ type: el.type, name: el.name, placeholder: el.placeholder?.substring(0, 30) })),
  };
}

// ══════════════════════════════════════════
//  Get App State (Vue/React/G6 data layer)
// ══════════════════════════════════════════

async function cmdGetAppState(params) {
  const tabId = params.tabId;
  if (tabId == null) throw new Error('No tabId specified');

  let tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch (e) {
    throw new Error(`Tab ${tabId} not found: ${e.message}`);
  }

  if (tab.url && isProtectedUrl(tab.url)) {
    throw new Error(`Cannot read protected page: ${tab.url}`);
  }

  const results = await chrome.scripting.executeScript({
    target: { tabId },
    world: 'MAIN',
    func: _getAppState,
    args: [params.depth || 'shallow'],
  });

  if (results && results[0] && results[0].result) {
    return results[0].result;
  }
  return { error: 'Failed to get app state' };
}

function _getAppState(深度) {
  const result = { framework: null, data: {}, chartData: null, globalVars: {} };

  // Detect Vue
  if (window.__VUE_DEVTOOLS_GLOBAL_HOOK__ || window.Vue) {
    result.framework = 'Vue';
    try {
      const apps = document.querySelectorAll('[data-v-app], #app, .app, [id^="vue"]');
      for (const appEl of apps) {
        if (appEl.__vue_app__?._instance) {
          const vm = appEl.__vue_app__._instance;
          result.vueInstance = {
            globalProperties: vm.appContext?.config?.globalProperties || {},
            hasRouter: !!(vm.appContext?.config?.globalProperties?.$router),
            hasStore: !!(vm.appContext?.config?.globalProperties?.$store),
          };
          // Try to extract component tree (simplified)
          try {
            const compTree = [];
            const processComp = (comp, depth = 0) => {
              if (depth > 3 || !comp) return;
              compTree.push({
                name: comp.type?.name || comp.type?.__name || 'Anonymous',
                hasChildren: !!(comp.subTree?.children || comp.component?.subTree),
              });
              if (comp.subTree?.component) processComp(comp.subTree.component, depth + 1);
            };
            if (vm.component) processComp(vm.component);
            result.vueInstance.componentTree = compTree.slice(0, 20);
          } catch (e) {}
          break;
        }
      }
    } catch (e) {
      result.vueError = e.message;
    }
  }

  // Detect React
  if (window.__REACT_DEVTOOLS_GLOBAL_HOOK__ || window.React) {
    result.framework = 'React';
    result.reactVersion = window.React?.version || 'unknown';
  }

  // Detect G6 graph library
  if (window.graph?.getNodes || window.G6) {
    result.chartLib = 'G6';
    try {
      const g = window.graph || (window.G6?.instances?.[0]);
      if (g) {
        result.chartData = {
          nodes: (g.getNodes?.() || []).map(n => {
            const model = n.getModel?.() || n;
            return { id: n.getID?.() || model.id, label: model.label || model.title, type: model.type };
          }).slice(0, 50),
          edges: (g.getEdges?.() || []).map(e => {
            const model = e.getModel?.() || e;
            return { source: model.source, target: model.target, label: model.label };
          }).slice(0, 50),
        };
      }
    } catch (e) {
      result.chartError = e.message;
    }
  }

  // Detect ECharts
  if (window.echarts?.getInstanceByDom) {
    result.chartLib = 'ECharts';
    try {
      const charts = Array.from(document.querySelectorAll('.echart, [data-echarts]'));
      result.chartData = { chartCount: charts.length, series: [] };
    } catch (e) {}
  }

  // Common global variables that might be useful
  const interestingGlobals = ['apiBase', 'API_BASE', 'config', 'CONFIG', 'store', 'state', 'appData', 'pageData', 'taskData', 'experimentData'];
  for (const key of interestingGlobals) {
    if (window[key] !== undefined) {
      try {
        result.globalVars[key] = JSON.parse(JSON.stringify(window[key]));
      } catch {
        result.globalVars[key] = String(window[key]).substring(0, 500);
      }
    }
  }

  return result;
}

// ══════════════════════════════════════════
//  Trusted Input (CDP)
// ══════════════════════════════════════════
// Synthetic JS events (el.dispatchEvent) carry isTrusted=false — some sites
// ignore them, and CSS :hover never fires at all. chrome.debugger's
// Input.dispatch* events are REAL input as far as the page is concerned.
// Same attach/detach pattern as the screenshot path: the "debugging" banner
// flashes only for the duration of the command, and every failure falls
// back to the synthetic path (e.g. DevTools already attached to the tab).

async function _acquireCdp(tabId, purpose) {
  const normalizedTabId = Number(tabId);
  if (!Number.isInteger(normalizedTabId) || normalizedTabId <= 0) {
    throw new Error('A valid tabId is required for DevTools access');
  }
  // A release removes the session only after Chrome confirms detach. Wait for
  // that transition before attempting a new attach to the same target.
  for (;;) {
    let session = _cdpSessions.get(normalizedTabId);
    if (session && session.closing) {
      await session.closing.catch(() => {});
      continue;
    }
    if (!session) {
      const target = { tabId: normalizedTabId };
      session = {
        tabId: normalizedTabId,
        target,
        holders: new Map(),
        tail: Promise.resolve(),
        attached: false,
        detached: false,
        closing: null,
      };
      _cdpSessions.set(normalizedTabId, session);
      session.attachPromise = chrome.debugger.attach(target, '1.3')
        .then(() => { session.attached = true; })
        .catch((error) => {
          session.detached = true;
          if (_cdpSessions.get(normalizedTabId) === session) {
            _cdpSessions.delete(normalizedTabId);
          }
          throw error;
        });
    }
    await session.attachPromise;
    if (session.detached || _cdpSessions.get(normalizedTabId) !== session) {
      throw new Error('Chrome detached the DevTools session while it was starting');
    }
    const leaseId = `${normalizedTabId}:${++_cdpLeaseSequence}`;
    session.holders.set(leaseId, {
      purpose: String(purpose || 'command').slice(0, 80),
      acquiredAt: Date.now(),
    });
    return {tabId: normalizedTabId, target: session.target, leaseId, session};
  }
}

async function _releaseCdp(lease) {
  if (!lease || lease.released) return;
  lease.released = true;
  const session = lease.session;
  if (!session) return;
  session.holders.delete(lease.leaseId);
  if (session.holders.size || session.closing || session.detached ||
      _cdpSessions.get(lease.tabId) !== session) return;
  session.closing = (async () => {
    try {
      await chrome.debugger.detach(session.target);
    } catch (_) {
      // Chrome may already have detached because the user opened DevTools or
      // closed the tab. Either way this broker no longer owns the target.
    } finally {
      session.attached = false;
      session.detached = true;
      if (_cdpSessions.get(lease.tabId) === session) {
        _cdpSessions.delete(lease.tabId);
      }
    }
  })();
  await session.closing;
}

function _onCdpDebuggerDetach(source, reason) {
  const tabId = Number(source && source.tabId);
  const session = _cdpSessions.get(tabId);
  if (!session) return;
  session.attached = false;
  session.detached = true;
  session.detachReason = String(reason || 'detached').slice(0, 120);
  _cdpSessions.delete(tabId);
  const debug = _debugSessions.get(tabId);
  if (debug) {
    _debugSessions.delete(tabId);
    debug.active = false;
    debug.detachReason = session.detachReason;
    if (debug.expiryTimer) clearTimeout(debug.expiryTimer);
    if (debug.pauseFailsafe) clearTimeout(debug.pauseFailsafe);
    _rememberDevtoolsSnapshot(tabId, {
      url: debug.url, entries: debug.consoleEntries,
      droppedEntries: debug.droppedConsoleEntries, capturedAt: Date.now(),
    });
  }
}

async function _runWithCdpLease(lease, fn) {
  const session = lease && lease.session;
  if (!session || session.detached) {
    throw new Error('DevTools session is no longer attached');
  }
  const run = session.tail.catch(() => {}).then(() => {
    if (session.detached) throw new Error('DevTools session was detached');
    return fn(lease.target);
  });
  session.tail = run.catch(() => {});
  return run;
}

async function _cdpRun(tabId, fn, purpose = 'command') {
  const lease = await _acquireCdp(tabId, purpose);
  try {
    return await _runWithCdpLease(lease, fn);
  } finally {
    await _releaseCdp(lease);
  }
}

// MAIN-world locator: scroll + element-center viewport coords + label bits.
// Shared by the CDP click/hover paths. An {error} result means the element
// is absent — the synthetic path would fail identically, so callers return
// it directly instead of falling back.
function _locateElement(selector, scrollTo) {
  const el = document.querySelector(selector);
  if (!el) return { error: `Element not found: ${selector}` };
  if (scrollTo) {
    el.scrollIntoView({ behavior: 'instant', block: 'center' });
  }
  const rect = el.getBoundingClientRect();
  return {
    x: rect.left + rect.width / 2,
    y: rect.top + rect.height / 2,
    tag: el.tagName.toLowerCase(),
    text: (el.innerText || '').trim().substring(0, 100),
  };
}

async function _cdpLocate(tabId, selector, scrollTo) {
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    world: 'MAIN',
    func: _locateElement,
    args: [selector, scrollTo],
  });
  const loc = results && results[0] && results[0].result;
  if (!loc) throw new Error('No result from locator script');
  return loc;
}

async function _cdpClick(tabId, selector, rightClick, scrollTo) {
  const loc = await _cdpLocate(tabId, selector, scrollTo);
  if (loc.error) return { clicked: false, error: loc.error };
  const button = rightClick ? 'right' : 'left';
  const buttons = rightClick ? 2 : 1;
  await _cdpRun(tabId, async (target) => {
    await chrome.debugger.sendCommand(target, 'Input.dispatchMouseEvent',
      { type: 'mouseMoved', x: loc.x, y: loc.y });
    await chrome.debugger.sendCommand(target, 'Input.dispatchMouseEvent',
      { type: 'mousePressed', x: loc.x, y: loc.y, button, buttons, clickCount: 1 });
    await new Promise(r => setTimeout(r, 40));
    await chrome.debugger.sendCommand(target, 'Input.dispatchMouseEvent',
      { type: 'mouseReleased', x: loc.x, y: loc.y, button, buttons, clickCount: 1 });
  });
  return {
    clicked: true, rightClick: !!rightClick, trusted: true,
    tag: loc.tag, text: loc.text,
    position: { x: Math.round(loc.x), y: Math.round(loc.y) },
  };
}

async function _cdpHover(tabId, selector) {
  const loc = await _cdpLocate(tabId, selector, true);
  if (loc.error) return { hovered: false, error: loc.error };
  // A trusted mouseMoved sets CSS :hover — the synthetic event sequence
  // (mouseenter/mouseover/mousemove) provably cannot.
  await _cdpRun(tabId, (target) =>
    chrome.debugger.sendCommand(target, 'Input.dispatchMouseEvent',
      { type: 'mouseMoved', x: loc.x, y: loc.y }));
  return {
    hovered: true, trusted: true,
    tag: loc.tag, text: loc.text,
    position: { x: Math.round(loc.x), y: Math.round(loc.y) },
  };
}

// CDP modifier bitmask: Alt=1, Ctrl=2, Meta=4, Shift=8.
const _CDP_MODIFIER_BITS = { Alt: 1, Control: 2, Meta: 4, Shift: 8 };

// Named (non-printable) keys → [code, windowsVirtualKeyCode, text?].
const _CDP_NAMED_KEYS = {
  Enter: ['Enter', 13, '\r'], Escape: ['Escape', 27], Tab: ['Tab', 9],
  Backspace: ['Backspace', 8], Delete: ['Delete', 46],
  ArrowUp: ['ArrowUp', 38], ArrowDown: ['ArrowDown', 40],
  ArrowLeft: ['ArrowLeft', 37], ArrowRight: ['ArrowRight', 39],
  Home: ['Home', 36], End: ['End', 35],
  PageUp: ['PageUp', 33], PageDown: ['PageDown', 34],
  F1: ['F1', 112], F2: ['F2', 113], F3: ['F3', 114], F4: ['F4', 115],
  F5: ['F5', 116], F6: ['F6', 117], F7: ['F7', 118], F8: ['F8', 119],
  F9: ['F9', 120], F10: ['F10', 121], F11: ['F11', 122], F12: ['F12', 123],
  ' ': ['Space', 32, ' '],
};

// Parse "Ctrl+Shift+P" / "Enter" / "a" into a CDP key descriptor + bitmask.
function _cdpKeyDescriptor(keys) {
  const parts = String(keys).split('+');
  let mainKey = parts.pop();
  const aliases = { Return: 'Enter', Esc: 'Escape', Space: ' ' };
  mainKey = aliases[mainKey] || mainKey;

  let modifiers = 0;
  for (const part of parts) {
    if (/^(ctrl|control)$/i.test(part)) modifiers |= _CDP_MODIFIER_BITS.Control;
    else if (/^alt$/i.test(part)) modifiers |= _CDP_MODIFIER_BITS.Alt;
    else if (/^shift$/i.test(part)) modifiers |= _CDP_MODIFIER_BITS.Shift;
    else if (/^(meta|cmd|command)$/i.test(part)) modifiers |= _CDP_MODIFIER_BITS.Meta;
  }

  let descriptor;
  if (mainKey.length === 1) {
    const upper = mainKey.toUpperCase();
    const isLetter = upper >= 'A' && upper <= 'Z';
    const isDigit = mainKey >= '0' && mainKey <= '9';
    descriptor = {
      key: mainKey,
      code: isLetter ? 'Key' + upper : (isDigit ? 'Digit' + mainKey : ''),
      vk: (isLetter || isDigit) ? upper.charCodeAt(0) : 0,
      text: (modifiers & _CDP_MODIFIER_BITS.Shift) && isLetter ? upper : mainKey,
    };
  } else if (_CDP_NAMED_KEYS[mainKey]) {
    const [code, vk, text] = _CDP_NAMED_KEYS[mainKey];
    descriptor = { key: mainKey, code, vk, text };
  } else {
    descriptor = { key: mainKey, code: '', vk: 0, text: undefined };
  }
  // A text payload is only a character when no command modifier rides along —
  // Ctrl+S must NOT type "s" into the page.
  if (modifiers & (_CDP_MODIFIER_BITS.Alt | _CDP_MODIFIER_BITS.Control | _CDP_MODIFIER_BITS.Meta)) {
    descriptor.text = undefined;
  }
  return { descriptor, modifiers };
}

async function _cdpKeyboard(tabId, keys, selector) {
  if (selector) {
    // Trusted key events go to the focused element — focus the target first.
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      world: 'MAIN',
      func: (sel) => {
        const el = document.querySelector(sel);
        if (!el) return { error: `Element not found: ${sel}` };
        el.focus();
        return { ok: true, tag: el.tagName.toLowerCase() };
      },
      args: [selector],
    });
    const r = results && results[0] && results[0].result;
    if (!r) throw new Error('No result from focus script');
    if (r.error) return { success: false, error: r.error };
  }
  const { descriptor, modifiers } = _cdpKeyDescriptor(keys);
  await _cdpRun(tabId, async (target) => {
    const base = {
      key: descriptor.key,
      code: descriptor.code,
      windowsVirtualKeyCode: descriptor.vk,
      modifiers,
    };
    await chrome.debugger.sendCommand(target, 'Input.dispatchKeyEvent',
      descriptor.text !== undefined
        ? { type: 'keyDown', text: descriptor.text, ...base }
        : { type: 'rawKeyDown', ...base });
    await chrome.debugger.sendCommand(target, 'Input.dispatchKeyEvent',
      { type: 'keyUp', ...base });
  });
  return { success: true, keys, trusted: true, target: selector || 'activeElement' };
}

// ══════════════════════════════════════════
//  Click Element
// ══════════════════════════════════════════

async function cmdClickElement(params) {
  const tabId = params.tabId;
  if (tabId == null) throw new Error('No tabId specified');
  if (!params.selector) throw new Error('No selector specified');

  let tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch (e) {
    throw new Error(`Tab ${tabId} not found: ${e.message}`);
  }

  if (tab.url && isProtectedUrl(tab.url)) {
    throw new Error(`Cannot interact with protected page: ${tab.url}`);
  }

  // CDP coordinates are top-frame coordinates. For an explicitly targeted
  // iframe use frame-scoped script injection; otherwise preserve trusted CDP
  // input for the common top-frame path.
  if (params.frameId == null) {
    try {
      return await _cdpClick(tabId, params.selector,
                             params.rightClick || false, params.scrollTo !== false);
    } catch (err) {
      console.warn('[Bridge] CDP click failed, falling back to synthetic events:',
                   err && err.message);
    }
  }

  const results = await chrome.scripting.executeScript({
    target: _pageTarget(params),
    world: 'MAIN',
    func: _clickElement,
    args: [params.selector, params.rightClick || false, params.scrollTo !== false],
  });

  if (results && results[0] && results[0].result) {
    const r = results[0].result;
    if (r.clicked) {
      r.trusted = false;
      r.fallbackReason = 'CDP attach/dispatch failed — synthetic events';
    }
    return r;
  }
  return { clicked: false, error: 'No result from script' };
}

function _clickElement(selector, rightClick, scrollTo) {
  const el = document.querySelector(selector);
  if (!el) return { clicked: false, error: `Element not found: ${selector}` };

  // Scroll into view
  if (scrollTo) {
    el.scrollIntoView({ behavior: 'instant', block: 'center' });
  }

  const rect = el.getBoundingClientRect();
  const x = rect.left + rect.width / 2;
  const y = rect.top + rect.height / 2;

  if (rightClick) {
    // Dispatch contextmenu event (right-click)
    const contextEvent = new MouseEvent('contextmenu', {
      bubbles: true, cancelable: true, view: window,
      clientX: x, clientY: y, button: 2,
    });
    el.dispatchEvent(contextEvent);
    return {
      clicked: true, rightClick: true,
      tag: el.tagName.toLowerCase(),
      text: (el.innerText || '').trim().substring(0, 100),
      position: { x: Math.round(x), y: Math.round(y) },
    };
  }

  // Standard left-click sequence: mousedown → mouseup → click
  for (const eventType of ['mousedown', 'mouseup', 'click']) {
    const event = new MouseEvent(eventType, {
      bubbles: true, cancelable: true, view: window,
      clientX: x, clientY: y, button: 0,
    });
    el.dispatchEvent(event);
  }

  // Also call .click() for good measure (some frameworks only listen for this)
  try { el.click(); } catch {}

  return {
    clicked: true, rightClick: false,
    tag: el.tagName.toLowerCase(),
    text: (el.innerText || '').trim().substring(0, 100),
    position: { x: Math.round(x), y: Math.round(y) },
  };
}

// ══════════════════════════════════════════
//  Hover Element (Playwright-style hover)
// ══════════════════════════════════════════

async function cmdHoverElement(params) {
  const tabId = params.tabId;
  if (tabId == null) throw new Error('No tabId specified');
  if (!params.selector) throw new Error('No selector specified');

  let tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch (e) {
    throw new Error(`Tab ${tabId} not found: ${e.message}`);
  }

  if (tab.url && isProtectedUrl(tab.url)) {
    throw new Error(`Cannot interact with protected page: ${tab.url}`);
  }

  try {
    return await _cdpHover(tabId, params.selector);
  } catch (err) {
    console.warn('[Bridge] CDP hover failed, falling back to synthetic events:',
                 err && err.message);
  }

  const results = await chrome.scripting.executeScript({
    target: { tabId },
    world: 'MAIN',
    func: _hoverElement,
    args: [params.selector],
  });

  if (results && results[0] && results[0].result) {
    const r = results[0].result;
    if (r.hovered) {
      r.trusted = false;
      r.fallbackReason = 'CDP attach/dispatch failed — synthetic events (CSS :hover NOT set)';
    }
    return r;
  }
  return { hovered: false, error: 'No result from script' };
}

function _hoverElement(selector) {
  const el = document.querySelector(selector);
  if (!el) return { hovered: false, error: `Element not found: ${selector}` };

  el.scrollIntoView({ behavior: 'instant', block: 'center' });

  const rect = el.getBoundingClientRect();
  const x = rect.left + rect.width / 2;
  const y = rect.top + rect.height / 2;

  // Trigger hover event sequence (mouseenter → mouseover → mousemove)
  for (const eventType of ['mouseenter', 'mouseover', 'mousemove']) {
    const event = new MouseEvent(eventType, {
      bubbles: true, cancelable: true, view: window,
      clientX: x, clientY: y, button: 0,
    });
    el.dispatchEvent(event);
  }

  return {
    hovered: true,
    tag: el.tagName.toLowerCase(),
    text: (el.innerText || '').trim().substring(0, 100),
    position: { x: Math.round(x), y: Math.round(y) },
  };
}

// ══════════════════════════════════════════
//  Keyboard Input (Playwright/Selenium-style)
// ══════════════════════════════════════════

async function cmdKeyboardInput(params) {
  const tabId = params.tabId;
  if (tabId == null) throw new Error('No tabId specified');

  let tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch (e) {
    throw new Error(`Tab ${tabId} not found: ${e.message}`);
  }

  if (tab.url && isProtectedUrl(tab.url)) {
    throw new Error(`Cannot interact with protected page: ${tab.url}`);
  }

  if (params.frameId == null) {
    try {
      return await _cdpKeyboard(tabId, params.keys, params.selector || null);
    } catch (err) {
      console.warn('[Bridge] CDP keyboard failed, falling back to synthetic events:',
                   err && err.message);
    }
  }

  const results = await chrome.scripting.executeScript({
    target: _pageTarget(params),
    world: 'MAIN',
    func: _keyboardInput,
    args: [params.keys, params.selector || null],
  });

  if (results && results[0] && results[0].result) {
    const r = results[0].result;
    if (r.success) {
      r.trusted = false;
      r.fallbackReason = 'CDP attach/dispatch failed — synthetic events';
    }
    return r;
  }
  return { success: false, error: 'No result from script' };
}

function _keyboardInput(keys, selector) {
  // Key mapping for special keys
  const keyMap = {
    'Enter': 'Enter', 'Return': 'Enter',
    'Escape': 'Escape', 'Esc': 'Escape',
    'Tab': 'Tab', 'Backspace': 'Backspace',
    'Delete': 'Delete', 'ArrowUp': 'ArrowUp',
    'ArrowDown': 'ArrowDown', 'ArrowLeft': 'ArrowLeft',
    'ArrowRight': 'ArrowRight', 'Home': 'Home',
    'End': 'End', 'PageUp': 'PageUp', 'PageDown': 'PageDown',
    'F1': 'F1', 'F2': 'F2', 'F3': 'F3', 'F4': 'F4',
    'F5': 'F5', 'F6': 'F6', 'F7': 'F7', 'F8': 'F8',
    'F9': 'F9', 'F10': 'F10', 'F11': 'F11', 'F12': 'F12',
  };

  // Parse modifier keys
  const modifiers = [];
  if (keys.includes('Ctrl') || keys.includes('Control')) modifiers.push('Control');
  if (keys.includes('Alt')) modifiers.push('Alt');
  if (keys.includes('Shift')) modifiers.push('Shift');
  if (keys.includes('Meta') || keys.includes('Command') || keys.includes('Cmd')) modifiers.push('Meta');

  // Find target element
  let target = selector ? document.querySelector(selector) : document.activeElement;
  if (!target) target = document.body;

  target.focus();

  // Extract main key (last part if using + notation like "Ctrl+S")
  let mainKey = keys.split('+').pop();
  mainKey = keyMap[mainKey] || mainKey;

  // Dispatch keydown with modifiers
  const keyDownEvent = new KeyboardEvent('keydown', {
    bubbles: true, cancelable: true, view: window,
    key: mainKey,
    ctrlKey: modifiers.includes('Control'),
    altKey: modifiers.includes('Alt'),
    shiftKey: modifiers.includes('Shift'),
    metaKey: modifiers.includes('Meta'),
  });
  target.dispatchEvent(keyDownEvent);

  // Dispatch keyup
  const keyUpEvent = new KeyboardEvent('keyup', {
    bubbles: true, cancelable: true, view: window,
    key: mainKey,
    ctrlKey: modifiers.includes('Control'),
    altKey: modifiers.includes('Alt'),
    shiftKey: modifiers.includes('Shift'),
    metaKey: modifiers.includes('Meta'),
  });
  target.dispatchEvent(keyUpEvent);

  // For Enter key, also trigger click on focused button
  if (mainKey === 'Enter' && (target.tagName === 'BUTTON' || target.role === 'button')) {
    target.click();
  }

  return {
    success: true,
    keys: keys,
    target: selector || 'activeElement',
    tagName: target.tagName.toLowerCase(),
  };
}

// ══════════════════════════════════════════
//  Wait For Element (Selenium-style explicit wait)
// ══════════════════════════════════════════

async function cmdWaitForElement(params) {
  const tabId = params.tabId;
  if (tabId == null) throw new Error('No tabId specified');
  if (!params.selector && params.time == null) {
    throw new Error('Either selector or time must be specified');
  }

  let tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch (e) {
    throw new Error(`Tab ${tabId} not found: ${e.message}`);
  }
  if (tab.url && isProtectedUrl(tab.url)) {
    throw new Error(`Cannot interact with protected page: ${tab.url}`);
  }
  await _assertExpectedDomain(params, tab);

  const timeout = params.timeout || 5000; // Default 5s
  const interval = params.interval || 100; // Poll every 100ms

  const startTime = Date.now();

  while (Date.now() - startTime < timeout) {
    try {
      const results = await chrome.scripting.executeScript({
        target: _pageTarget(params),
        world: 'MAIN',
        func: _checkElement,
        args: [params.selector, params.condition || 'present'],
      });

      if (results && results[0] && results[0].result) {
        const result = results[0].result;
        if (result.found) return result;
      }
    } catch (e) {
      // Element check failed, continue waiting
    }

    // If just waiting for time, check less frequently
    if (params.time) {
      const elapsed = Date.now() - startTime;
      if (elapsed >= params.time * 1000) {
        return { found: true, waited: params.time * 1000, reason: 'time_elapsed' };
      }
    }

    await new Promise(resolve => setTimeout(resolve, interval));
  }

  return {
    found: false,
    selector: params.selector,
    timeout: timeout,
    error: `Element not found within ${timeout}ms`,
  };
}

function _checkElement(selector, condition) {
  const el = document.querySelector(selector);

  if (!el) {
    return { found: false, selector };
  }

  const rect = el.getBoundingClientRect();
  const isVisible = rect.width > 0 && rect.height > 0;

  if (condition === 'present') {
    return { found: true, selector, visible: isVisible };
  } else if (condition === 'visible') {
    return { found: isVisible, selector, visible: isVisible };
  } else if (condition === 'clickable') {
    const style = window.getComputedStyle(el);
    const isClickable = isVisible &&
      style.pointerEvents !== 'none' &&
      el.offsetParent !== null;
    return { found: isClickable, selector, visible: isVisible, clickable: isClickable };
  }

  return { found: true, selector, visible: isVisible };
}

// ══════════════════════════════════════════
//  Type Text (dedicated text input — more reliable than keyboard_input for forms)
// ══════════════════════════════════════════

async function cmdTypeText(params) {
  const tabId = params.tabId;
  if (tabId == null) throw new Error('No tabId specified');
  if (!params.selector && !params.index) throw new Error('No selector or index specified');
  if (params.text === undefined && params.text === null) throw new Error('No text specified');

  let tab;
  try { tab = await chrome.tabs.get(tabId); } catch (e) { throw new Error(`Tab ${tabId} not found: ${e.message}`); }
  if (tab.url && isProtectedUrl(tab.url)) throw new Error(`Cannot interact with protected page: ${tab.url}`);

  const results = await chrome.scripting.executeScript({
    target: _pageTarget(params),
    world: 'MAIN',
    func: _typeText,
    args: [params.selector || null, params.text, params.clearFirst !== false, params.pressEnter || false],
  });

  if (results && results[0] && results[0].result) return results[0].result;
  return { success: false, error: 'No result from script' };
}

function _typeText(selector, text, clearFirst, pressEnter) {
  const el = selector ? document.querySelector(selector) : document.activeElement;
  if (!el) return { success: false, error: `Element not found: ${selector}` };

  // Scroll into view and focus
  el.scrollIntoView({ behavior: 'instant', block: 'center' });
  el.focus();

  // Clear existing value
  if (clearFirst) {
    // Select all + delete for maximum compatibility
    el.value = '';
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }

  // Type character by character for frameworks that listen to individual keystrokes
  // But set .value directly first for reliability
  const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype, 'value'
  )?.set || Object.getOwnPropertyDescriptor(
    window.HTMLTextAreaElement.prototype, 'value'
  )?.set;

  if (nativeInputValueSetter) {
    nativeInputValueSetter.call(el, text);
  } else {
    el.value = text;
  }

  // Dispatch the full event sequence that React/Vue/Angular listen to
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: text.slice(-1) || '' }));

  // Optionally press Enter after typing
  if (pressEnter) {
    el.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'Enter', keyCode: 13 }));
    el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: 'Enter', keyCode: 13 }));
    // Also try form submission
    const form = el.closest('form');
    if (form) { try { form.requestSubmit(); } catch(e) { try { form.submit(); } catch(e2) {} } }
  }

  return {
    success: true,
    typed: text,
    selector: selector || '(activeElement)',
    tag: el.tagName.toLowerCase(),
    name: el.name || '',
    newValue: el.value?.substring(0, 100) || '',
  };
}

// ══════════════════════════════════════════
//  Scroll Page
// ══════════════════════════════════════════

async function cmdScrollPage(params) {
  const tabId = params.tabId;
  if (tabId == null) throw new Error('No tabId specified');

  let tab;
  try { tab = await chrome.tabs.get(tabId); } catch (e) { throw new Error(`Tab ${tabId} not found: ${e.message}`); }
  if (tab.url && isProtectedUrl(tab.url)) throw new Error(`Cannot interact with protected page: ${tab.url}`);
  await _assertExpectedDomain(params, tab);

  const results = await chrome.scripting.executeScript({
    target: _pageTarget(params),
    world: 'MAIN',
    func: _scrollPage,
    args: [params.direction || 'down', params.amount || null, params.selector || null],
  });

  if (results && results[0] && results[0].result) return results[0].result;
  return { scrolled: false, error: 'No result from script' };
}

function _scrollPage(direction, amount, selector) {
  // If a selector is given, scroll that element into view
  if (selector) {
    const el = document.querySelector(selector);
    if (!el) return { scrolled: false, error: `Element not found: ${selector}` };
    el.scrollIntoView({ behavior: 'instant', block: 'center' });
    const rect = el.getBoundingClientRect();
    return {
      scrolled: true, method: 'scrollIntoView', selector,
      elementPosition: { x: Math.round(rect.x), y: Math.round(rect.y) },
      scrollY: Math.round(window.scrollY),
      scrollHeight: document.documentElement.scrollHeight,
      viewportHeight: window.innerHeight,
      scrollPercent: Math.round((window.scrollY / Math.max(1, document.documentElement.scrollHeight - window.innerHeight)) * 100),
    };
  }

  const pixels = amount || Math.round(window.innerHeight * 0.75);  // Default: 75% viewport
  const beforeY = window.scrollY;

  switch (direction) {
    case 'up':     window.scrollBy(0, -pixels); break;
    case 'down':   window.scrollBy(0, pixels); break;
    case 'top':    window.scrollTo(0, 0); break;
    case 'bottom': window.scrollTo(0, document.documentElement.scrollHeight); break;
    case 'left':   window.scrollBy(-pixels, 0); break;
    case 'right':  window.scrollBy(pixels, 0); break;
    default:       window.scrollBy(0, pixels); break;
  }

  const afterY = window.scrollY;
  const maxScroll = document.documentElement.scrollHeight - window.innerHeight;
  return {
    scrolled: true,
    direction,
    pixelsMoved: Math.round(Math.abs(afterY - beforeY)),
    scrollY: Math.round(afterY),
    scrollHeight: document.documentElement.scrollHeight,
    viewportHeight: window.innerHeight,
    scrollPercent: Math.round((afterY / Math.max(1, maxScroll)) * 100),
    atTop: afterY <= 0,
    atBottom: afterY >= maxScroll - 1,
  };
}

// ══════════════════════════════════════════
//  Navigation: go_back / go_forward
// ══════════════════════════════════════════

async function cmdGoBack(params) {
  const tabId = params.tabId;
  if (tabId == null) throw new Error('No tabId specified');

  await chrome.scripting.executeScript({
    target: { tabId },
    func: () => window.history.back(),
  });

  // Wait for navigation
  await new Promise(r => setTimeout(r, 500));
  await waitForTabLoad(tabId, 10000);

  const tab = await chrome.tabs.get(tabId);
  return { id: tab.id, url: tab.url, title: tab.title, status: tab.status, action: 'back' };
}

async function cmdGoForward(params) {
  const tabId = params.tabId;
  if (tabId == null) throw new Error('No tabId specified');

  await chrome.scripting.executeScript({
    target: { tabId },
    func: () => window.history.forward(),
  });

  await new Promise(r => setTimeout(r, 500));
  await waitForTabLoad(tabId, 10000);

  const tab = await chrome.tabs.get(tabId);
  return { id: tab.id, url: tab.url, title: tab.title, status: tab.status, action: 'forward' };
}

// ══════════════════════════════════════════
//  Cookies
// ══════════════════════════════════════════

async function cmdGetCookies(params) {
  const details = {};
  if (params.url) details.url = params.url;
  if (params.domain) details.domain = params.domain;
  if (params.name) details.name = params.name;

  const cookies = await chrome.cookies.getAll(details);
  return cookies.map(c => ({
    name: c.name,
    value: c.value,
    domain: c.domain,
    path: c.path,
    secure: c.secure,
    httpOnly: c.httpOnly,
    expirationDate: c.expirationDate,
  }));
}

async function cmdSetCookie(params) {
  const details = { url: params.url };
  if (params.name) details.name = params.name;
  if (params.value !== undefined) details.value = params.value;
  if (params.domain) details.domain = params.domain;
  if (params.path) details.path = params.path;
  if (params.secure !== undefined) details.secure = params.secure;
  if (params.expirationDate) details.expirationDate = params.expirationDate;

  const cookie = await chrome.cookies.set(details);
  return cookie;
}

async function cmdRemoveCookie(params) {
  await chrome.cookies.remove({ url: params.url, name: params.name });
  return { removed: true };
}

// ══════════════════════════════════════════
//  History & Bookmarks
// ══════════════════════════════════════════

async function cmdGetHistory(params) {
  const results = await chrome.history.search({
    text: params.query || '',
    maxResults: params.maxResults || 100,
    startTime: params.startTime || 0,
  });
  return results.map(h => ({
    id: h.id,
    url: h.url,
    title: h.title,
    lastVisitTime: h.lastVisitTime,
    visitCount: h.visitCount,
  }));
}

async function cmdGetBookmarks(params) {
  const tree = await chrome.bookmarks.getTree();
  function flatten(nodes) {
    const result = [];
    for (const node of (nodes || [])) {
      if (node.url) {
        result.push({ id: node.id, title: node.title, url: node.url });
      }
      if (node.children) result.push(...flatten(node.children));
    }
    return result;
  }
  return flatten(tree);
}

// ══════════════════════════════════════════
//  Tab Management
// ══════════════════════════════════════════

async function cmdCreateTab(params) {
  const requestedUrl = params.url || 'about:blank';
  const captureNavigation = params.waitForLoad === true && requestedUrl !== 'about:blank';
  // A response capture must exist BEFORE the first application request.  When
  // load waiting was requested, create an inert tab first and navigate only
  // after CDP Network is enabled.  The non-waiting path preserves the cheap
  // historical tab-create behavior.
  const opts = { url: captureNavigation ? 'about:blank' : requestedUrl };
  // Default to background (active: false) unless explicitly requested
  opts.active = params.active === true ? true : false;
  if (params.pinned !== undefined) opts.pinned = params.pinned;
  if (params.windowId) opts.windowId = params.windowId;

  const tab = await chrome.tabs.create(opts);
  let captureId = null;
  if (captureNavigation) {
    const timeoutMs = Math.max(1000, Math.min(30000, Number(params.timeoutMs) || 15000));
    try {
      captureId = (await _startNetworkCapture({
        tabId: tab.id, captureBodies: true,
      }, { allowInertBlank: true })).captureId;
      await chrome.tabs.update(tab.id, { url: requestedUrl });
      await waitForTabLoad(tab.id, timeoutMs);
      await _waitForCapturedPageSettle(tab.id, captureId, timeoutMs);
      await _stopNetworkCaptureInternal(captureId, { remember: true });
      captureId = null;
    } finally {
      if (captureId) {
        await _stopNetworkCaptureInternal(captureId, { remember: true }).catch(() => {});
      }
    }
  }
  const current = await chrome.tabs.get(tab.id);
  return { id: current.id, url: current.url, title: current.title,
           windowId: current.windowId, status: current.status };
}

async function cmdCloseTab(params) {
  const tabIds = Array.isArray(params.tabIds) ? params.tabIds : [params.tabId];
  await chrome.tabs.remove(tabIds);
  for (const tabId of tabIds) _recentNetworkByTab.delete(Number(tabId));
  return { closed: tabIds };
}

async function cmdUpdateTab(params) {
  const updateProps = {};
  if (params.url) updateProps.url = params.url;
  if (params.active !== undefined) updateProps.active = params.active;
  if (params.pinned !== undefined) updateProps.pinned = params.pinned;
  if (params.muted !== undefined) updateProps.muted = params.muted;

  const tab = await chrome.tabs.update(params.tabId, updateProps);
  return { id: tab.id, url: tab.url, title: tab.title };
}

// ══════════════════════════════════════════
//  Fetch URL — background tab with user cookies
// ══════════════════════════════════════════

/**
 * Opens a URL in a hidden background tab (inheriting the user's session/cookies),
 * extracts the text content, and closes the tab. This allows fetching pages that
 * require authentication (e.g. HuggingFace private datasets, Medium articles).
 *
 * params: { url, maxChars?, timeoutMs? }
 * returns: { text, title, url, textLength, truncated, meta }
 */
async function cmdFetchUrl(params) {
  const url = params.url;
  if (!url) throw new Error('No url specified');
  const maxChars = params.maxChars || 50000;
  const timeoutMs = Math.max(
    1000, Math.min(20000, Number(params.timeoutMs) || 20000));

  if (isProtectedUrl(url)) {
    throw new Error(`Cannot fetch protected URL: ${_urlForDiagnostic(url)}`);
  }

  // Refuse binary assets by extension. Navigating a tab to a PDF/zip/media URL
  // makes Chrome's download manager save it to the user's machine (and yields
  // no scrapable text) — these are fetched/parsed server-side, never here.
  // (The server-side bridge already filters these, but a redirect could still
  // land us on one, so guard defensively.)
  if (isBinaryAssetUrl(url)) {
    throw new Error(
      `Refusing to open binary asset in a tab (would download): ${_urlForDiagnostic(url)}`);
  }

  // Extensionless download endpoints (for example `/download?version=latest`)
  // cannot be recognized from the URL. Inspect authenticated response
  // headers with fetch and cancel its body before opening a tab. `fetch()`
  // never invokes Chrome's download manager, so an attachment cannot leak to
  // the client device. A blocked/unclassifiable probe fails closed; a known
  // file response reuses that exact Response for server transfer.
  const fileReceipt = await _refuseFileResponseBeforeNavigation(
    url, timeoutMs, params.fileTransfer || null);
  if (fileReceipt) return fileReceipt;

  // Create an inert background tab first.  Navigating directly in
  // chrome.tabs.create loses the initial XHR/fetch responses before Network
  // capture can attach — exactly the SPA failure this command must solve.
  let tab;
  try {
    tab = await chrome.tabs.create({ url: 'about:blank', active: false });
  } catch (e) {
    throw new Error(
      `Failed to create tab for ${_urlForDiagnostic(url)}: ${_textForDiagnostic(e)}`);
  }

  let captureId = null;
  try {
    captureId = (await _startNetworkCapture({
      tabId: tab.id, captureBodies: true,
    }, { allowInertBlank: true })).captureId;
    await chrome.tabs.update(tab.id, { url });
    // Wait for the tab to fully load
    await waitForTabLoad(tab.id, timeoutMs);
    await _waitForCapturedPageSettle(tab.id, captureId, timeoutMs);
    const network = await _stopNetworkCaptureInternal(
      captureId, { remember: false });
    captureId = null;

    // Re-fetch tab info for final URL (after redirects)
    tab = await chrome.tabs.get(tab.id);

    // If it ended up on a protected page (e.g. login redirect), bail
    if (tab.url && isProtectedUrl(tab.url)) {
      throw new Error(
        `Redirected to protected page: ${_urlForDiagnostic(tab.url)}`);
    }
    if (tab.url && isBinaryAssetUrl(tab.url)) {
      throw new Error(
        `Redirected to binary asset (refusing download): ${_urlForDiagnostic(tab.url)}`);
    }

    // Extract text content
    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: _extractContent,
      args: [null, maxChars],
    });

    if (results && results[0] && results[0].result) {
      const r = results[0].result;
      r.title = tab.title || '';
      r.url = tab.url || '';
      if (network) r.network = network;
      return r;
    }

    return { text: '', title: tab.title || '', url: tab.url || '', error: 'No content extracted' };
  } finally {
    if (captureId) {
      await _stopNetworkCaptureInternal(
        captureId, { remember: false }).catch(() => {});
    }
    // Always close the background tab, even on error
    try { await chrome.tabs.remove(tab.id); } catch (_) {}
    _recentNetworkByTab.delete(Number(tab.id));
  }
}

function _isTextualResponseType(contentType) {
  const value = String(contentType || '').split(';', 1)[0].trim().toLowerCase();
  if (!value) return false;
  return value.startsWith('text/') ||
    value === 'application/json' || value.endsWith('+json') ||
    value === 'application/xml' || value.endsWith('+xml') ||
    value === 'application/xhtml+xml' ||
    value === 'application/javascript' ||
    value === 'application/x-javascript' ||
    value === 'image/svg+xml';
}

function _responseLooksLikeFile(response) {
  const disposition = response.headers.get('Content-Disposition') || '';
  if (/(?:^|;)\s*(?:attachment\b|filename\*?\s*=)/i.test(disposition)) {
    return true;
  }
  const contentType = response.headers.get('Content-Type') || '';
  // Only a positively textual response is safe to open in a hidden tab.
  // Missing/unknown metadata stays on the byte-transfer path: guessing
  // "page" here can make Chrome's download manager write to the client.
  return !_isTextualResponseType(contentType);
}

async function _refuseFileResponseBeforeNavigation(
  url, timeoutMs, fileTransfer,
) {
  const controller = new AbortController();
  const probeStartedAt = _monotonicNowMs();
  const fileOperationBudgetMs = fileTransfer
    ? Math.max(1000, Math.min(
      30000, Number(fileTransfer.timeoutMs) || Number(timeoutMs) || 20000))
    : null;
  let timer = setTimeout(
    () => controller.abort(), Math.max(1000, Math.min(
      8000, Number(timeoutMs) || 20000,
      fileOperationBudgetMs || 8000)));
  let response = null;
  try {
    response = await fetch(url, {
      method: 'GET', credentials: 'include', redirect: 'follow',
      cache: 'no-store', signal: controller.signal,
    });
    if (response.url && isProtectedUrl(response.url)) {
      const error = new Error(
        `Redirected to protected page: ${_urlForDiagnostic(response.url)}`);
      error.tofuFileProbeDecision = true;
      throw error;
    }
    if (_responseLooksLikeFile(response)) {
      if (fileTransfer && fileTransfer.transferId && fileTransfer.transferToken) {
        clearTimeout(timer);
        timer = null;
        // Reuse this exact authenticated response. Download URLs can be
        // single-use or signed, so probing once and fetching again is not an
        // acceptable transport contract.
        try {
          const remainingTransferMs = Math.floor(
            fileOperationBudgetMs - (_monotonicNowMs() - probeStartedAt));
          if (remainingTransferMs < 1000) {
            throw new Error(
              'Browser file-transfer deadline elapsed during response classification');
          }
          return await cmdFetchFileToServer({
            ...fileTransfer, url,
            timeoutMs: remainingTransferMs,
            _response: response, _controller: controller,
          });
        } catch (error) {
          // A classified file must fail closed. Falling through to tab
          // navigation here would resurrect the client-download bug.
          error.tofuFileProbeDecision = true;
          throw error;
        }
      }
      const contentType = response.headers.get('Content-Type') || 'unknown type';
      const error = new Error(
        `Resource is a file (${contentType}); use browser-to-server file transfer`);
      error.tofuFileProbeDecision = true;
      throw error;
    }
  } catch (error) {
    if (error && error.tofuFileProbeDecision) throw error;
    // A failed/blocked probe says nothing about the response type. Navigating
    // anyway would turn an unclassified attachment into a client download, so
    // this read path must fail closed.
    const guarded = new Error(
      `Could not safely classify response before navigation: ${_textForDiagnostic(error)}`);
    guarded.tofuFileProbeDecision = true;
    throw guarded;
  } finally {
    if (timer) clearTimeout(timer);
    if (response && response.body) {
      try { await response.body.cancel(); } catch (_) {}
    }
  }
}

function _transferHeaders(token, contentType, chunkSha256) {
  const headers = buildHeaders();
  headers['X-Browser-Client-Id'] = CLIENT_ID;
  headers['X-Transfer-Token'] = String(token || '');
  headers['Content-Type'] = contentType || 'application/json';
  if (chunkSha256) headers['X-Chunk-SHA256'] = chunkSha256;
  return headers;
}

function _transferErrorMessage(data, status) {
  const raw = data && data.error;
  const message = typeof raw === 'string'
    ? raw
    : (raw && (raw.message || raw.detail)) || `HTTP ${status}`;
  const code = data && data.code;
  return code ? `${code}: ${message}` : message;
}

async function _transferRequest(path, options) {
  const opts = options || {};
  let lastError = null;
  const attempts = opts.retry ? 2 : 1;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      const response = await fetch(`${SERVER_URL}${path}`, {
        method: opts.method || 'POST',
        headers: _transferHeaders(
          opts.token, opts.contentType, opts.chunkSha256),
        credentials: 'include',
        body: opts.body,
        signal: opts.signal,
      });
      const data = await response.json().catch(() => null);
      if (!response.ok) {
        const error = new Error(_transferErrorMessage(data, response.status));
        error.status = response.status;
        if (response.status < 500 || attempt + 1 >= attempts) throw error;
        lastError = error;
        continue;
      }
      return data || {};
    } catch (error) {
      lastError = error;
      if (error && error.status && error.status < 500) throw error;
      if (attempt + 1 >= attempts) throw error;
    }
  }
  throw lastError || new Error('Browser file-transfer request failed');
}

function _suggestedResponseFilename(response, fallbackUrl) {
  // Content-Disposition parsing is server-authoritative. This hint is only
  // the final response URL basename when the header has no usable filename.
  try {
    return decodeURIComponent(new URL(response.url || fallbackUrl).pathname
      .split('/').filter(Boolean).pop() || '');
  } catch (_) { return ''; }
}

async function _sha256Hex(bytes) {
  const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  const exact = view.byteOffset === 0 && view.byteLength === view.buffer.byteLength
    ? view.buffer
    : view.buffer.slice(view.byteOffset, view.byteOffset + view.byteLength);
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', exact));
  return Array.from(digest, (value) => value.toString(16).padStart(2, '0')).join('');
}

/**
 * Read a response with credentials allowed by Chrome's cookie policy and
 * stream it into the issuing Tofu server's bounded staging store. This
 * function never extracts/replays cookies and never calls chrome.downloads,
 * so it never writes to the client Downloads folder.
 */
async function cmdFetchFileToServer(params) {
  const url = String(params.url || '').trim();
  const transferId = String(params.transferId || '').trim();
  const transferToken = String(params.transferToken || '').trim();
  const maxBytes = Math.max(1, Number(params.maxBytes) || 0);
  const chunkBytes = Math.max(
    16 * 1024, Math.min(256 * 1024, Number(params.chunkBytes) || 256 * 1024));
  const timeoutMs = Math.max(
    1000, Math.min(115000, Number(params.timeoutMs) || 110000));
  if (!url || !transferId || !transferToken || !maxBytes) {
    throw new Error('Incomplete browser-to-server file-transfer command');
  }
  if (isProtectedUrl(url)) {
    throw new Error(`Cannot fetch protected URL: ${_urlForDiagnostic(url)}`);
  }

  const controller = params._controller || new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const basePath = `/api/browser/file-transfers/${encodeURIComponent(transferId)}`;
  let completed = false;
  let reader = null;
  try {
    const response = params._response || await fetch(url, {
        method: 'GET', credentials: 'include', redirect: 'follow',
        cache: 'no-store', signal: controller.signal,
      });
    if (!response.ok) {
      throw new Error(`Upstream file request returned HTTP ${response.status}`);
    }
    if (!response.url || isProtectedUrl(response.url)) {
      throw new Error(
        `File request redirected to a protected URL: ${_urlForDiagnostic(response.url)}`);
    }
    const rawLength = response.headers.get('Content-Length');
    const contentEncoding = (
      response.headers.get('Content-Encoding') || '').trim().toLowerCase();
    // Fetch exposes decoded body bytes, while Content-Length can describe the
    // compressed wire body. Only compare lengths when those units match.
    const contentLength = (!contentEncoding || contentEncoding === 'identity')
      && rawLength && /^\d+$/.test(rawLength.trim())
      ? Number(rawLength) : null;
    if (contentLength != null && contentLength > maxBytes) {
      throw new Error(`Browser response exceeds the ${maxBytes}-byte limit`);
    }
    await _transferRequest(`${basePath}/start`, {
      token: transferToken,
      signal: controller.signal,
      body: JSON.stringify({
        finalUrl: response.url,
        responseStatus: response.status,
        contentType: (response.headers.get('Content-Type') || '').slice(0, 200),
        contentDisposition: (
          response.headers.get('Content-Disposition') || '').slice(0, 1024),
        contentLength,
        suggestedFilename: _suggestedResponseFilename(response, url).slice(0, 240),
      }),
      retry: true,
    });
    if (!response.body) throw new Error('Browser response body is not streamable');

    reader = response.body.getReader();
    let totalBytes = 0;
    let sequence = 0;
    let pendingBytes = 0;
    const pending = new Uint8Array(chunkBytes);
    const maxChunks = Math.max(1, Math.ceil(maxBytes / (16 * 1024)));

    const sendPendingChunk = async () => {
      if (!pendingBytes) return;
      if (sequence >= maxChunks) {
        throw new Error('Browser response exceeded the bounded chunk count');
      }
      const chunk = pending.subarray(0, pendingBytes);
      const chunkSha256 = await _sha256Hex(chunk);
      await _transferRequest(`${basePath}/chunks/${sequence}`, {
        method: 'PUT', token: transferToken,
        contentType: 'application/octet-stream', chunkSha256,
        body: chunk, signal: controller.signal, retry: true,
      });
      totalBytes += pendingBytes;
      pendingBytes = 0;
      sequence += 1;
    };

    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      const incoming = value instanceof Uint8Array ? value : new Uint8Array(value || []);
      if (totalBytes + pendingBytes + incoming.byteLength > maxBytes) {
        controller.abort();
        throw new Error(`Browser response exceeds the ${maxBytes}-byte limit`);
      }
      let offset = 0;
      while (offset < incoming.byteLength) {
        const copied = Math.min(
          chunkBytes - pendingBytes, incoming.byteLength - offset);
        pending.set(incoming.subarray(offset, offset + copied), pendingBytes);
        pendingBytes += copied;
        offset += copied;
        if (pendingBytes === chunkBytes) await sendPendingChunk();
      }
    }
    await sendPendingChunk();
    const receipt = await _transferRequest(`${basePath}/complete`, {
      token: transferToken,
      signal: controller.signal,
      body: JSON.stringify({totalBytes, chunkCount: sequence}),
      retry: true,
    });
    if (receipt.transferId !== transferId || receipt.location !== 'server_staging') {
      throw new Error('Server returned a mismatched file-transfer receipt');
    }
    completed = true;
    return receipt;
  } finally {
    clearTimeout(timer);
    if (reader && !completed) {
      try { await reader.cancel(); } catch (_) {}
    }
    if (!completed) {
      // Cleanup uses a fresh request rather than the aborted transfer signal.
      const cleanupController = new AbortController();
      const cleanupTimer = setTimeout(() => cleanupController.abort(), 5000);
      try {
        await _transferRequest(basePath, {
          method: 'DELETE', token: transferToken,
          signal: cleanupController.signal, retry: false,
        });
      } catch (_) {
        // The server registry also owns an inactivity TTL; cleanup is bounded
        // best effort and must never hold command settlement open forever.
      } finally {
        clearTimeout(cleanupTimer);
      }
    }
  }
}

function _researchPageSignals(maxChars) {
  const cap = Math.max(1000, Math.min(30000, Number(maxChars) || 30000));
  const textParts = [];
  const bodyText = document.body ? (document.body.innerText || document.body.textContent || '') : '';
  if (bodyText) textParts.push(bodyText);
  // Open shadow roots are part of the user-visible page but are absent from
  // document.body.innerText in several component libraries.
  let shadowCount = 0;
  let shadowScanned = 0;
  for (const el of document.querySelectorAll('*')) {
    if (shadowScanned++ >= 5000) break;
    if (!el.shadowRoot || shadowCount >= 40) continue;
    const shadowText = el.shadowRoot.innerText || el.shadowRoot.textContent || '';
    if (shadowText) textParts.push(shadowText);
    shadowCount++;
  }
  const fullText = textParts.join('\n');
  const stateNames = [
    '__INITIAL_STATE__', '__PRELOADED_STATE__', '__NEXT_DATA__', '__NUXT__',
    '__APOLLO_STATE__', '__REMIX_CONTEXT__',
  ];
  const initialState = {};
  const initialStatePayloads = {};
  let stateChars = 0;
  const preview = (value) => {
    const seen = new WeakSet();
    let remaining = 60000;
    let nodes = 0;
    const clone = (child, depth) => {
      if (remaining <= 0 || nodes++ >= 1200) return '[truncated]';
      if (child == null || typeof child === 'boolean' || typeof child === 'number') {
        remaining -= 16;
        return child;
      }
      if (typeof child === 'string') {
        const out = child.slice(0, Math.min(2000, Math.max(0, remaining)));
        remaining -= out.length + 2;
        return out;
      }
      if (typeof child === 'bigint') return String(child);
      if (typeof child === 'function') return '[function]';
      if (!child || typeof child !== 'object') return String(child);
      if (seen.has(child)) return '[circular]';
      if (depth >= 8) return '[depth limit]';
      seen.add(child);
      if (Array.isArray(child)) {
        const out = [];
        for (const item of child.slice(0, 50)) {
          if (remaining <= 0) break;
          out.push(clone(item, depth + 1));
        }
        if (child.length > out.length) out.push(`[${child.length - out.length} more items]`);
        return out;
      }
      const out = {};
      for (const key of Object.keys(child).slice(0, 50)) {
        if (remaining <= 0) break;
        remaining -= String(key).length + 4;
        try { out[key] = clone(child[key], depth + 1); }
        catch (_) { out[key] = '[unavailable]'; }
      }
      return out;
    };
    try {
      return JSON.stringify(clone(value, 0));
    } catch (_) {
      return '';
    }
  };
  for (const name of stateNames) {
    let value;
    try { value = window[name]; } catch (_) { value = undefined; }
    if (name === '__NEXT_DATA__' && value == null) {
      try {
        const node = document.querySelector('script#__NEXT_DATA__[type="application/json"]');
        if (node && node.textContent) value = JSON.parse(node.textContent);
      } catch (_) {}
    }
    const present = value != null;
    initialState[name] = present;
    if (!present || stateChars >= 160000) continue;
    const serialized = preview(value);
    if (!serialized) continue;
    const remaining = 160000 - stateChars;
    initialStatePayloads[name] = serialized.slice(0, Math.min(60000, remaining));
    stateChars += initialStatePayloads[name].length;
  }
  if (stateChars < 160000) {
    const jsonLd = [];
    for (const node of document.querySelectorAll('script[type="application/ld+json"]')) {
      if (jsonLd.length >= 8) break;
      const value = String(node.textContent || '').trim();
      if (value) jsonLd.push(value.slice(0, 20000));
    }
    if (jsonLd.length) initialStatePayloads.JSON_LD = `[${jsonLd.join(',')}]`.slice(0, 60000);
  }
  let framework = 'unknown';
  if (window.__NEXT_DATA__) framework = 'Next.js';
  else if (window.__NUXT__) framework = 'Nuxt';
  else if (window.__VUE_DEVTOOLS_GLOBAL_HOOK__ || window.Vue) framework = 'Vue';
  else if (window.__REACT_DEVTOOLS_GLOBAL_HOOK__ || window.React) framework = 'React';
  else if (window.angular) framework = 'Angular';
  const root = document.scrollingElement || document.documentElement;
  const text = fullText.slice(0, cap);
  return {
    url: location.href, title: document.title || '', text,
    textLength: fullText.length,
    initialState, initialStatePayloads, framework, shadowRootCount: shadowCount,
    fingerprint: [location.href, document.title, fullText.length,
      fullText.slice(-400), root ? root.scrollHeight : 0].join('|'),
  };
}

function _researchScrollStep() {
  const root = document.scrollingElement || document.documentElement;
  let target = root;
  let bestScore = root ? Math.max(0, root.scrollHeight - root.clientHeight) *
    Math.max(1, Math.min(root.clientWidth || window.innerWidth, window.innerWidth)) : 0;
  const candidates = document.querySelectorAll('main,section,article,div,ul,ol,[role="main"],[role="list"]');
  const scanLimit = Math.min(candidates.length, 5000);
  for (let index = 0; index < scanLimit; index++) {
    const el = candidates[index];
    const range = el.scrollHeight - el.clientHeight;
    if (range < 160 || el.clientHeight < 120 || el.clientWidth < 160) continue;
    const style = getComputedStyle(el);
    if (!/(?:auto|scroll)/.test(style.overflowY || '')) continue;
    const score = range * Math.min(el.clientWidth, window.innerWidth || el.clientWidth);
    if (score > bestScore) {
      bestScore = score;
      target = el;
    }
  }
  if (!target) return {scrolled: false, atBottom: true, reason: 'no-scroll-root'};
  const isDocument = target === root;
  const before = isDocument ? window.scrollY : target.scrollTop;
  const viewport = isDocument ? window.innerHeight : target.clientHeight;
  const maximum = Math.max(0, target.scrollHeight - viewport);
  const amount = Math.max(400, Math.round(viewport * 0.88));
  const next = Math.min(maximum, before + amount);
  if (isDocument) window.scrollTo(0, next);
  else target.scrollTop = next;
  const after = isDocument ? window.scrollY : target.scrollTop;
  return {
    scrolled: after !== before, atBottom: after >= maximum - 2,
    before: Math.round(before), after: Math.round(after),
    scrollHeight: target.scrollHeight, viewportHeight: viewport,
    target: isDocument ? 'document' : `${target.tagName.toLowerCase()}${target.id ? '#' + target.id : ''}`,
  };
}

function _researchAdvancePagination(mode) {
  const normalizedMode = String(mode || 'auto');
  const roots = Array.from(document.querySelectorAll(
    'nav,[role="navigation"],[aria-label*="pagin" i],[class*="pagination" i],[class*="pager" i]'));
  const candidates = [];
  const seen = new Set();
  const addCandidate = (el) => {
    if (!el || seen.has(el)) return;
    seen.add(el);
    candidates.push(el);
  };
  // Outside a semantic pagination container, only an explicit rel=next link
  // is trusted. A generic button named "Next" may submit a wizard or form.
  for (const el of document.querySelectorAll('a[rel~="next"][href],link[rel~="next"][href]')) {
    addCandidate(el);
  }
  for (const root of roots.slice(0, 30)) {
    if (root.matches && root.matches('a[href],button,[role="button"]')) addCandidate(root);
    for (const el of root.querySelectorAll('a[href],button,[role="button"]')) {
      addCandidate(el);
      if (candidates.length >= 300) break;
    }
  }
  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.display !== 'none' &&
      style.visibility !== 'hidden' && !el.disabled &&
      el.getAttribute('aria-disabled') !== 'true' &&
      !/(?:^|\s)(?:disabled|is-disabled)(?:\s|$)/i.test(el.className || '');
  };
  const labelOf = (el) => String(
    (el && el.getAttribute && el.getAttribute('aria-label')) ||
    (el && el.getAttribute && el.getAttribute('title')) ||
    el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  const isNext = (el) => {
    const label = labelOf(el);
    const rel = String(el.getAttribute('rel') || '');
    const classes = `${el.className || ''} ${(el.parentElement && el.parentElement.className) || ''}`;
    if (/\bnext\b/i.test(rel)) return true;
    if (/^(?:next(?: page)?|下一页|下页|后一页|更多|加载更多|load more|show more|›|»|>)$/i.test(label)) return true;
    return /(?:^|[-_\s])next(?:[-_\s]|$)/i.test(classes) &&
      !/(?:^|[-_\s])prev(?:ious)?(?:[-_\s]|$)/i.test(classes);
  };
  let target = candidates.find((el) => visible(el) && isNext(el));
  if (!target) {
    for (const root of roots) {
      const current = root.querySelector('[aria-current="page"],.active,.is-active,.selected');
      const currentNumber = Number(labelOf(current || {}));
      if (!Number.isFinite(currentNumber)) continue;
      target = Array.from(root.querySelectorAll('a[href],button,[role="button"]'))
        .find((el) => visible(el) && Number(labelOf(el)) === currentNumber + 1);
      if (target) break;
    }
  }
  if (!target) return {advanced: false, reason: 'no-safe-next-control'};
  const label = labelOf(target).slice(0, 120);
  const href = /^(?:A|LINK)$/.test(target.tagName || '') ? target.href : '';
  if (href) {
    let next;
    try { next = new URL(href, location.href); } catch (_) {
      return {advanced: false, reason: 'invalid-next-url'};
    }
    if (next.origin !== location.origin) {
      return {advanced: false, reason: 'cross-origin-next-blocked'};
    }
    return {advanced: true, kind: 'link', href: next.href, label};
  }
  if (normalizedMode !== 'auto') {
    return {advanced: false, reason: 'button-pagination-disabled'};
  }
  target.scrollIntoView({block: 'center', behavior: 'instant'});
  target.click();
  return {advanced: true, kind: 'click', label};
}

function _appendResearchText(accumulator, rawText, maxChars) {
  const lines = String(rawText || '').split(/\r?\n/);
  let added = 0;
  for (const raw of lines) {
    if (accumulator.seen.size >= 10000) {
      accumulator.truncated = true;
      break;
    }
    const line = raw.replace(/[\t ]+/g, ' ').trim().slice(0, 2000);
    if (!line || accumulator.seen.has(line)) continue;
    const extra = line.length + (accumulator.lines.length ? 1 : 0);
    if (accumulator.chars + extra > maxChars) {
      accumulator.truncated = true;
      break;
    }
    accumulator.seen.add(line);
    accumulator.lines.push(line);
    accumulator.chars += extra;
    added += extra;
  }
  return added;
}

async function _readResearchSignals(tabId, maxChars) {
  try {
    const results = await chrome.scripting.executeScript({
      target: {tabId: Number(tabId)}, world: 'MAIN',
      func: _researchPageSignals, args: [maxChars],
    });
    return results && results[0] && results[0].result || null;
  } catch (_) {
    return null;
  }
}

async function cmdResearchUrl(params) {
  const requestedUrl = String(params.url || '');
  if (!requestedUrl) throw new Error('No url specified');
  if (isProtectedUrl(requestedUrl)) {
    throw new Error(
      `Cannot research protected URL: ${_urlForDiagnostic(requestedUrl)}`);
  }
  if (isBinaryAssetUrl(requestedUrl)) {
    throw new Error(
      `Refusing to research binary asset: ${_urlForDiagnostic(requestedUrl)}`);
  }
  const maxChars = Math.max(1000, Math.min(80000, Number(params.maxChars) || 60000));
  const maxScrolls = Math.max(0, Math.min(8, Number(params.maxScrolls) || 0));
  const maxPages = Math.max(1, Math.min(5, Number(params.maxPages) || 1));
  const pagination = ['auto', 'links', 'none'].includes(String(params.pagination))
    ? String(params.pagination) : 'auto';
  const timeoutMs = Math.max(10000, Math.min(65000, Number(params.timeoutMs) || 65000));
  const deadline = Date.now() + timeoutMs;
  let requestedOrigin;
  try { requestedOrigin = new URL(requestedUrl).origin; }
  catch (_) { throw new Error(`Invalid URL: ${requestedUrl}`); }

  let tab = await chrome.tabs.create({url: 'about:blank', active: false});
  let captureId = null;
  const accumulator = {lines: [], seen: new Set(), chars: 0, truncated: false};
  let firstSignals = null;
  let lastSignals = null;
  let pagesVisited = 1;
  let scrollsCompleted = 0;
  let stopReason = 'complete';
  const seenFingerprints = new Set();
  try {
    captureId = (await _startNetworkCapture({
      tabId: tab.id, captureBodies: true, captureHints: params.captureHints,
    }, { allowInertBlank: true })).captureId;
    await chrome.tabs.update(tab.id, {url: requestedUrl});
    await waitForTabLoad(tab.id, Math.min(20000, timeoutMs));
    await _waitForCapturedPageSettle(tab.id, captureId, Math.min(15000, timeoutMs));
    tab = await chrome.tabs.get(tab.id);
    const initialOrigin = (() => { try { return new URL(tab.url || '').origin; } catch (_) { return ''; } })();
    lastSignals = await _readResearchSignals(tab.id, maxChars);
    firstSignals = lastSignals;
    if (lastSignals) {
      seenFingerprints.add(String(lastSignals.fingerprint || ''));
      _appendResearchText(accumulator, lastSignals.text, maxChars);
    }
    if (initialOrigin !== requestedOrigin) {
      stopReason = 'cross_origin_redirect';
    } else {
      for (let pageIndex = 0; pageIndex < maxPages && Date.now() < deadline; pageIndex++) {
        let stagnantScrolls = 0;
        for (let scrollIndex = 0; scrollIndex < maxScrolls && Date.now() < deadline; scrollIndex++) {
          const scrollResult = await chrome.scripting.executeScript({
            target: {tabId: tab.id}, world: 'MAIN', func: _researchScrollStep,
          });
          const scroll = scrollResult && scrollResult[0] && scrollResult[0].result || {};
          await _waitForCapturedPageSettle(tab.id, captureId, Math.min(5000, deadline - Date.now()));
          const signals = await _readResearchSignals(tab.id, maxChars);
          const added = signals ? _appendResearchText(accumulator, signals.text, maxChars) : 0;
          lastSignals = signals || lastSignals;
          scrollsCompleted++;
          stagnantScrolls = added > 0 ? 0 : stagnantScrolls + 1;
          if ((!scroll.scrolled && scroll.atBottom) || (scroll.atBottom && stagnantScrolls >= 2)) break;
        }
        if (pageIndex + 1 >= maxPages || pagination === 'none' || Date.now() >= deadline) {
          stopReason = Date.now() >= deadline ? 'time_budget' :
            (pageIndex + 1 >= maxPages ? 'page_limit' : 'scroll_complete');
          break;
        }
        const advanceRows = await chrome.scripting.executeScript({
          target: {tabId: tab.id}, world: 'MAIN', func: _researchAdvancePagination,
          args: [pagination],
        });
        const advance = advanceRows && advanceRows[0] && advanceRows[0].result || {};
        if (!advance.advanced) {
          stopReason = advance.reason || 'no-next-page';
          break;
        }
        if (advance.kind === 'link') await chrome.tabs.update(tab.id, {url: advance.href});
        await waitForTabLoad(tab.id, Math.min(15000, Math.max(1000, deadline - Date.now())));
        await _waitForCapturedPageSettle(tab.id, captureId, Math.min(5000, deadline - Date.now()));
        tab = await chrome.tabs.get(tab.id);
        let currentOrigin = '';
        try { currentOrigin = new URL(tab.url || '').origin; } catch (_) {}
        if (currentOrigin !== requestedOrigin) {
          stopReason = 'cross_origin_pagination_blocked';
          break;
        }
        const signals = await _readResearchSignals(tab.id, maxChars);
        const fingerprint = String(signals && signals.fingerprint || '');
        const added = signals ? _appendResearchText(accumulator, signals.text, maxChars) : 0;
        if (!signals || (seenFingerprints.has(fingerprint) && added === 0)) {
          stopReason = 'pagination_stalled';
          break;
        }
        seenFingerprints.add(fingerprint);
        lastSignals = signals;
        pagesVisited++;
      }
    }
    const network = await _stopNetworkCaptureInternal(captureId, {remember: false});
    captureId = null;
    tab = await chrome.tabs.get(tab.id);
    let cookieNames = [];
    try {
      const cookies = await chrome.cookies.getAll({url: tab.url || requestedUrl});
      cookieNames = Array.from(new Set(cookies.map((cookie) => cookie.name))).slice(0, 80);
    } catch (_) {}
    return {
      requestedUrl, url: tab.url || '', title: tab.title || '',
      collectedText: accumulator.lines.join('\n'),
      textLength: accumulator.chars, truncated: accumulator.truncated,
      framework: (firstSignals && firstSignals.framework) ||
        (lastSignals && lastSignals.framework) || 'unknown',
      initialState: firstSignals && firstSignals.initialState || {},
      initialStatePayloads: firstSignals && firstSignals.initialStatePayloads || {},
      cookieNames, network,
      research: {pagesVisited, scrollsCompleted, stopReason,
        maxPages, maxScrolls, pagination, elapsedMs: timeoutMs - Math.max(0, deadline - Date.now())},
    };
  } finally {
    if (captureId) await _stopNetworkCaptureInternal(captureId, {remember: false}).catch(() => {});
    try { await chrome.tabs.remove(tab.id); } catch (_) {}
    if (tab && tab.id != null) _recentNetworkByTab.delete(Number(tab.id));
  }
}

async function cmdNavigate(params) {
  const tabId = params.tabId;
  const url = params.url;
  if (!tabId) throw new Error('No tabId specified');
  if (!url) throw new Error('No url specified');

  const target = await chrome.tabs.get(tabId);
  if (_isOwnServerTab(target)) {
    // The Tofu client tab is never navigated — open the destination in a
    // new foreground tab (same capture-before-navigation flow) and tell the
    // server, which re-binds the working tab to the new id.
    const created = await cmdCreateTab({
      url, active: true,
      waitForLoad: params.waitForLoad,
      timeoutMs: params.timeoutMs,
    });
    created.redirectedToNewTab = true;
    created.protectedTabId = tabId;
    return created;
  }

  let captureId = null;
  try {
    if (params.waitForLoad) {
      captureId = (await cmdNetworkCaptureStart({
        tabId, captureBodies: true,
      })).captureId;
    }
    await chrome.tabs.update(tabId, { url });

    if (params.waitForLoad) {
      await waitForTabLoad(tabId, 15000);
      await _waitForCapturedPageSettle(tabId, captureId, 15000);
      await _stopNetworkCaptureInternal(captureId, { remember: true });
      captureId = null;
    }
  } finally {
    if (captureId) {
      await _stopNetworkCaptureInternal(captureId, { remember: true }).catch(() => {});
    }
  }

  const tab = await chrome.tabs.get(tabId);
  return { id: tab.id, url: tab.url, title: tab.title, status: tab.status };
}

// ══════════════════════════════════════════
//  Downloads & Notifications
// ══════════════════════════════════════════

async function cmdDownload(params) {
  const opts = { url: params.url };
  if (params.filename) opts.filename = params.filename;
  if (params.saveAs !== undefined) opts.saveAs = params.saveAs;
  const downloadId = await chrome.downloads.download(opts);
  return { location: 'device_downloads', clientId: CLIENT_ID, downloadId };
}

// ══════════════════════════════════════════
//  Protocol-v2 Page primitives
// ══════════════════════════════════════════

function _pageTarget(params) {
  const target = { tabId: Number(params.tabId) };
  if (!target.tabId) throw new Error('No tabId specified');
  if (params.frameId != null) target.frameIds = [Number(params.frameId)];
  return target;
}

async function _assertExpectedDomain(params, knownTab) {
  if (!params.expectedDomain) return;
  const tab = knownTab || await chrome.tabs.get(Number(params.tabId));
  let actual = '';
  try {
    actual = (new URL(tab.url || '')).hostname.toLowerCase().replace(/^www\./, '').replace(/\.$/, '');
  } catch (_) { /* handled by mismatch below */ }
  const expected = String(params.expectedDomain).toLowerCase().replace(/^www\./, '').replace(/\.$/, '');
  if (!actual || actual !== expected) {
    throw new Error(`Page origin changed before action (expected ${expected}, got ${actual || 'unknown'})`);
  }
}

function _selectorForPageParams(params) {
  if (params.ref) {
    const escaped = String(params.ref).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
    return `[data-tofu-ref="${escaped}"]`;
  }
  if (params.selector) return String(params.selector);
  throw new Error('selector or ref is required');
}

async function cmdPageState(params) {
  const tab = await chrome.tabs.get(Number(params.tabId));
  return {
    tabId: tab.id, url: tab.url || '', title: tab.title || '',
    status: tab.status || '', active: !!tab.active, windowId: tab.windowId,
  };
}

function _snapshotPage(maxElements) {
  let next = 1;
  const selector = [
    'a[href]', 'button', 'input', 'textarea', 'select',
    '[role="button"]', '[role="link"]', '[contenteditable="true"]',
    '[tabindex]:not([tabindex="-1"])',
  ].join(',');
  const elements = [];
  for (const el of document.querySelectorAll(selector)) {
    if (elements.length >= maxElements) break;
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    if (!rect.width || !rect.height || style.visibility === 'hidden' || style.display === 'none') continue;
    let ref = el.getAttribute('data-tofu-ref');
    if (!ref) {
      ref = `t${Date.now().toString(36)}-${next++}`;
      el.setAttribute('data-tofu-ref', ref);
    }
    elements.push({
      ref, tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '',
      text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 300),
      type: el.getAttribute('type') || '', href: el.href || '',
      name: el.getAttribute('name') || '', disabled: !!el.disabled,
      rect: {x: Math.round(rect.x), y: Math.round(rect.y),
             width: Math.round(rect.width), height: Math.round(rect.height)},
    });
  }
  return {url: location.href, title: document.title || '', elements, count: elements.length};
}

async function cmdPageSnapshot(params) {
  const tab = await chrome.tabs.get(Number(params.tabId));
  if (tab.url && isProtectedUrl(tab.url)) throw new Error(`Cannot snapshot protected page: ${tab.url}`);
  await _assertExpectedDomain(params, tab);
  const results = await chrome.scripting.executeScript({
    target: _pageTarget(params), func: _snapshotPage,
    args: [Math.max(1, Math.min(1000, Number(params.maxElements) || 250))],
  });
  const value = results && results[0] ? results[0].result : {elements: [], count: 0};
  return {value, page: await cmdPageState(params)};
}

async function cmdPageClick(params) {
  await _assertExpectedDomain(params);
  const result = await cmdClickElement(Object.assign({}, params, {
    selector: _selectorForPageParams(params),
  }));
  return {value: result, page: await cmdPageState(params)};
}

async function cmdPageFill(params) {
  await _assertExpectedDomain(params);
  const result = await cmdTypeText(Object.assign({}, params, {
    selector: _selectorForPageParams(params), text: String(params.value == null ? '' : params.value),
    clearFirst: true, pressEnter: false,
  }));
  return {value: result, page: await cmdPageState(params)};
}

async function cmdPagePress(params) {
  await _assertExpectedDomain(params);
  const p = Object.assign({}, params);
  if (params.ref || params.selector) p.selector = _selectorForPageParams(params);
  const result = await cmdKeyboardInput(p);
  return {value: result, page: await cmdPageState(params)};
}

function _selectPageOption(selector, value) {
  const el = document.querySelector(selector);
  if (!el) return {error: `Element not found: ${selector}`};
  if (!(el instanceof HTMLSelectElement)) return {error: 'Target is not a select element'};
  const wanted = Array.isArray(value) ? value.map(String) : [String(value)];
  for (const option of el.options) option.selected = wanted.includes(option.value);
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return {selected: Array.from(el.selectedOptions).map(o => o.value)};
}

async function cmdPageSelect(params) {
  const tab = await chrome.tabs.get(Number(params.tabId));
  if (tab.url && isProtectedUrl(tab.url)) throw new Error(`Cannot interact with protected page: ${tab.url}`);
  await _assertExpectedDomain(params, tab);
  const results = await chrome.scripting.executeScript({
    target: _pageTarget(params), func: _selectPageOption,
    args: [_selectorForPageParams(params), params.value],
  });
  const result = results && results[0] && results[0].result;
  if (result && result.error) throw new Error(result.error);
  return {value: result, page: await cmdPageState(params)};
}

async function cmdPageExecute(params) {
  if (!params.expression) throw new Error('No expression specified');
  const tab = await chrome.tabs.get(Number(params.tabId));
  if (tab.url && isProtectedUrl(tab.url)) throw new Error(`Cannot execute on protected page: ${tab.url}`);
  await _assertExpectedDomain(params, tab);
  const results = await chrome.scripting.executeScript({
    target: _pageTarget(params), world: 'MAIN', func: _executeInPageWithArgs,
    args: [String(params.expression), params.args == null ? {} : params.args],
  });
  const value = results && results[0] && results[0].result;
  if (value && value.__error) throw new Error(value.message || 'Page execution failed');
  return {value, page: await cmdPageState(params)};
}

function _executeInPageWithArgs(expression, args) {
  try {
    // The executable body and structured arguments travel separately.  This
    // prevents query/filter values from being interpolated into JavaScript.
    return (new Function('args', `"use strict"; return (${expression});`))(args);
  } catch (e) {
    return {__error: true, message: e.message || String(e)};
  }
}

function _uploadPageFile(selector, base64, filename, mimeType) {
  const input = document.querySelector(selector);
  if (!input || !(input instanceof HTMLInputElement) || input.type !== 'file') {
    return {error: 'Target is not a file input'};
  }
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  const transfer = new DataTransfer();
  transfer.items.add(new File([bytes], filename, {type: mimeType || 'application/octet-stream'}));
  input.files = transfer.files;
  input.dispatchEvent(new Event('input', {bubbles: true}));
  input.dispatchEvent(new Event('change', {bubbles: true}));
  return {filename, size: bytes.length, type: mimeType || 'application/octet-stream'};
}

async function cmdPageUpload(params) {
  if (!params.data || String(params.data).length > 28 * 1024 * 1024) {
    throw new Error('Upload data is missing or exceeds the 20 MB decoded limit');
  }
  const tab = await chrome.tabs.get(Number(params.tabId));
  if (tab.url && isProtectedUrl(tab.url)) throw new Error(`Cannot upload on protected page: ${tab.url}`);
  await _assertExpectedDomain(params, tab);
  const results = await chrome.scripting.executeScript({
    target: _pageTarget(params), func: _uploadPageFile,
    args: [_selectorForPageParams(params), String(params.data),
           String(params.filename || 'upload.bin'), String(params.mimeType || '')],
  });
  const result = results && results[0] && results[0].result;
  if (result && result.error) throw new Error(result.error);
  return {value: result, page: await cmdPageState(params)};
}

// ══════════════════════════════════════════
//  DevTools Bridge (Console / Runtime / Debugger)
// ══════════════════════════════════════════

function _devtoolsText(value, limit = 4000) {
  const text = String(value == null ? '' : value);
  return text.length > limit ? text.slice(0, limit) + '…' : text;
}

function _remoteObjectPreview(remote) {
  const value = remote || {};
  if (Object.prototype.hasOwnProperty.call(value, 'value')) {
    return typeof value.value === 'string'
      ? _devtoolsText(value.value) : value.value;
  }
  if (value.unserializableValue != null) return String(value.unserializableValue);
  if (value.description) return _devtoolsText(value.description, 1000);
  return value.type || 'undefined';
}

function _appendDevtoolsEntry(sink, entry) {
  if (!sink || !entry) return;
  sink.consoleEntries = Array.isArray(sink.consoleEntries) ? sink.consoleEntries : [];
  sink.consoleChars = Number(sink.consoleChars) || 0;
  sink.droppedConsoleEntries = Number(sink.droppedConsoleEntries) || 0;
  let chars = 0;
  try { chars = JSON.stringify(entry).length; } catch (_) { chars = 1000; }
  if (sink.consoleEntries.length >= DEVTOOLS_MAX_LOG_ENTRIES ||
      sink.consoleChars + chars > DEVTOOLS_MAX_LOG_CHARS) {
    sink.droppedConsoleEntries++;
    return;
  }
  sink.consoleEntries.push(entry);
  sink.consoleChars += chars;
  sink.lastActivityAt = Date.now();
}

function _rememberDevtoolsSnapshot(tabId, snapshot) {
  const key = Number(tabId);
  if (!key || !snapshot) return;
  const entries = Array.isArray(snapshot.entries)
    ? snapshot.entries.slice(0, DEVTOOLS_MAX_LOG_ENTRIES) : [];
  _recentDevtoolsByTab.delete(key);
  _recentDevtoolsByTab.set(key, {
    url: String(snapshot.url || ''), entries,
    droppedEntries: Number(snapshot.droppedEntries) || 0,
    capturedAt: Number(snapshot.capturedAt) || Date.now(),
  });
  while (_recentDevtoolsByTab.size > DEVTOOLS_RECENT_TABS) {
    _recentDevtoolsByTab.delete(_recentDevtoolsByTab.keys().next().value);
  }
}

function _devtoolsEventEntry(source, method, params) {
  const sessionId = String((source && source.sessionId) || '');
  if (method === 'Runtime.consoleAPICalled') {
    const stack = params.stackTrace && params.stackTrace.callFrames || [];
    const top = stack[0] || {};
    return {
      kind: 'console', level: String(params.type || 'log'),
      text: (params.args || []).map(_remoteObjectPreview)
        .map((value) => typeof value === 'string' ? value : JSON.stringify(value))
        .join(' ').slice(0, 12000),
      args: (params.args || []).slice(0, 20).map(_remoteObjectPreview),
      timestamp: Number(params.timestamp) || Date.now(),
      url: String(top.url || ''), line: Number(top.lineNumber) || 0,
      column: Number(top.columnNumber) || 0,
      executionContextId: Number(params.executionContextId) || 0,
      ...(sessionId ? {sessionId} : {}),
    };
  }
  if (method === 'Runtime.exceptionThrown') {
    const detail = params.exceptionDetails || {};
    const stack = detail.stackTrace && detail.stackTrace.callFrames || [];
    const top = stack[0] || {};
    return {
      kind: 'exception', level: 'error',
      text: _devtoolsText(
        (detail.exception && detail.exception.description) || detail.text || 'Exception',
        12000),
      timestamp: Number(params.timestamp) || Date.now(),
      url: String(detail.url || top.url || ''),
      line: Number(detail.lineNumber != null ? detail.lineNumber : top.lineNumber) || 0,
      column: Number(detail.columnNumber != null ? detail.columnNumber : top.columnNumber) || 0,
      ...(sessionId ? {sessionId} : {}),
    };
  }
  if (method === 'Log.entryAdded') {
    const row = params.entry || {};
    return {
      kind: 'log', level: String(row.level || 'info'),
      source: String(row.source || ''), text: _devtoolsText(row.text, 12000),
      timestamp: Number(row.timestamp) || Date.now(),
      url: String(row.url || ''), line: Number(row.lineNumber) || 0,
      ...(sessionId ? {sessionId} : {}),
    };
  }
  return null;
}

function _sameOriginUrl(left, right) {
  try { return new URL(String(left)).origin === new URL(String(right)).origin; }
  catch (_) { return false; }
}

function _publicCallFrame(frame) {
  const location = frame.location || {};
  return {
    callFrameId: String(frame.callFrameId || ''),
    functionName: String(frame.functionName || '(anonymous)'),
    url: String(frame.url || ''),
    lineNumber: Number(location.lineNumber) || 0,
    columnNumber: Number(location.columnNumber) || 0,
    scopeChain: (frame.scopeChain || []).slice(0, 12).map((scope) => ({
      type: String(scope.type || ''), name: String(scope.name || ''),
      object: _remoteObjectPreview(scope.object),
    })),
  };
}

async function _attachDebugChild(session, source, params) {
  if (!session || !session.active) return;
  const sessionId = String(params.sessionId || '');
  const info = params.targetInfo || {};
  if (!sessionId) return;
  // A top-page grant must never silently authorize a cross-origin iframe or
  // worker. Same-origin related targets are enough for framework workers and
  // OOPIFs while preserving the server's exact-domain authority boundary.
  if (!_sameOriginUrl(session.url, info.url || session.url) ||
      session.targets.size >= DEVTOOLS_MAX_CONTEXTS) {
    try {
      await chrome.debugger.sendCommand(source, 'Target.detachFromTarget', {sessionId});
    } catch (_) {}
    return;
  }
  session.targets.set(sessionId, {
    sessionId, targetId: String(info.targetId || ''),
    type: String(info.type || ''), title: String(info.title || ''),
    url: String(info.url || ''),
  });
  const child = {tabId: session.tabId, sessionId};
  try {
    await chrome.debugger.sendCommand(child, 'Runtime.enable');
    await chrome.debugger.sendCommand(child, 'Log.enable');
    await chrome.debugger.sendCommand(child, 'Debugger.enable', {
      maxScriptsCacheSize: 2 * 1024 * 1024,
    });
    await chrome.debugger.sendCommand(child, 'Target.setAutoAttach', {
      autoAttach: true, waitForDebuggerOnStart: false, flatten: true,
      filter: [{type: 'iframe', exclude: false}, {type: 'worker', exclude: false}],
    });
  } catch (error) {
    session.targets.delete(sessionId);
    console.warn('[DevTools] related target attach failed:', error && error.message);
  }
}

function _debugSessionForSource(source) {
  const session = _debugSessions.get(Number(source && source.tabId));
  return session && session.active ? session : null;
}

function _onDevtoolsDebuggerEvent(source, method, params) {
  const tabId = Number(source && source.tabId);
  const relatedSessionId = String((source && source.sessionId) || '');
  const session = _debugSessionForSource(source);
  const relatedSourceAllowed = !relatedSessionId || !!(
    session && session.targets.has(relatedSessionId));
  const entry = _devtoolsEventEntry(source, method, params || {});
  if (entry) {
    const sinks = new Set();
    const capture = _networkCaptureForTab(tabId);
    if (relatedSourceAllowed && capture && capture.cdpAttached && !capture.stopping) {
      sinks.add(capture);
    }
    if (session && relatedSourceAllowed) sinks.add(session);
    for (const observer of _devtoolsObservers.values()) {
      if (relatedSourceAllowed && observer.tabId === tabId) sinks.add(observer);
    }
    for (const sink of sinks) _appendDevtoolsEntry(sink, entry);
  }

  if (!session) {
    if (method === 'Runtime.executionContextCreated') {
      for (const observer of _devtoolsObservers.values()) {
        if (observer.tabId !== tabId || observer.contexts.length >= DEVTOOLS_MAX_CONTEXTS) continue;
        const context = params.context || {};
        observer.contexts.push({
          id: Number(context.id) || 0, name: String(context.name || ''),
          origin: String(context.origin || ''),
          frameId: String((context.auxData && context.auxData.frameId) || ''),
          isDefault: !!(context.auxData && context.auxData.isDefault),
        });
      }
    }
    return;
  }

  if (method === 'Target.attachedToTarget') {
    _attachDebugChild(session, source, params || {}).catch(() => {});
    return;
  }
  if (method === 'Target.detachedFromTarget') {
    session.targets.delete(String(params.sessionId || ''));
    return;
  }
  // A related session is inserted into ``targets`` only after its target URL
  // passes the same-origin gate. Ignore any event Chrome races ahead of the
  // detach for a rejected cross-origin iframe/worker.
  if (!relatedSourceAllowed) return;
  if (method === 'Runtime.executionContextCreated') {
    const context = params.context || {};
    const key = `${String((source && source.sessionId) || 'root')}:${Number(context.id) || 0}`;
    if (session.contexts.size >= DEVTOOLS_MAX_CONTEXTS && !session.contexts.has(key)) {
      session.contexts.delete(session.contexts.keys().next().value);
    }
    session.contexts.set(key, {
      id: Number(context.id) || 0, name: String(context.name || ''),
      origin: String(context.origin || ''),
      frameId: String((context.auxData && context.auxData.frameId) || ''),
      isDefault: !!(context.auxData && context.auxData.isDefault),
      sessionId: String((source && source.sessionId) || ''),
    });
    return;
  }
  if (method === 'Runtime.executionContextDestroyed') {
    const id = Number(params.executionContextId) || 0;
    for (const [key, context] of session.contexts) {
      if (context.id === id && context.sessionId === String((source && source.sessionId) || '')) {
        session.contexts.delete(key);
      }
    }
    return;
  }
  if (method === 'Debugger.scriptParsed') {
    const scriptId = String(params.scriptId || '');
    if (!scriptId) return;
    const scriptSessionId = String((source && source.sessionId) || '');
    const scriptKey = `${scriptSessionId || 'root'}:${scriptId}`;
    if (session.scripts.size >= DEVTOOLS_MAX_SCRIPTS && !session.scripts.has(scriptKey)) {
      session.scripts.delete(session.scripts.keys().next().value);
    }
    session.scripts.set(scriptKey, {
      scriptId, url: String(params.url || ''),
      startLine: Number(params.startLine) || 0,
      startColumn: Number(params.startColumn) || 0,
      endLine: Number(params.endLine) || 0,
      endColumn: Number(params.endColumn) || 0,
      length: Number(params.length) || 0,
      hash: String(params.hash || ''),
      sessionId: scriptSessionId,
    });
    return;
  }
  if (method === 'Debugger.paused') {
    if (session.pauseFailsafe) clearTimeout(session.pauseFailsafe);
    session.paused = {
      reason: String(params.reason || 'other'),
      data: params.data && typeof params.data === 'object' ? params.data : {},
      hitBreakpoints: (params.hitBreakpoints || []).slice(0, DEVTOOLS_MAX_BREAKPOINTS),
      callFrames: (params.callFrames || []).slice(0, 20).map(_publicCallFrame),
      sessionId: String((source && source.sessionId) || ''),
      pausedAt: Date.now(),
    };
    session.pauseFailsafe = setTimeout(() => {
      _resumeDebugSession(session, 'resume', true).catch(() => {});
    }, DEVTOOLS_PAUSE_FAILSAFE_MS);
    return;
  }
  if (method === 'Debugger.resumed') {
    if (session.pauseFailsafe) clearTimeout(session.pauseFailsafe);
    session.pauseFailsafe = null;
    session.paused = null;
  }
}

function _debugTarget(session, sessionId = '') {
  return sessionId
    ? {tabId: session.tabId, sessionId: String(sessionId)}
    : session.lease.target;
}

function _debugState(session) {
  if (!session || !session.active) return {active: false};
  return {
    active: true, startedAt: session.startedAt,
    expiresInMs: Math.max(0, session.expiresAt - Date.now()),
    paused: session.paused,
    breakpoints: Array.from(session.breakpoints.values()),
    contexts: Array.from(session.contexts.values()).slice(0, DEVTOOLS_MAX_CONTEXTS),
    targets: Array.from(session.targets.values()).slice(0, DEVTOOLS_MAX_CONTEXTS),
    scripts: Array.from(session.scripts.values()).slice(-DEVTOOLS_MAX_SCRIPTS),
    consoleEntries: session.consoleEntries.slice(-DEVTOOLS_MAX_LOG_ENTRIES),
    droppedConsoleEntries: session.droppedConsoleEntries,
  };
}

async function _startDebugSession(tab, ttlMs) {
  const tabId = Number(tab.id);
  const existing = _debugSessions.get(tabId);
  if (existing && existing.active) return existing;
  if (_debugSessions.size >= DEVTOOLS_MAX_ACTIVE_DEBUG_SESSIONS) {
    throw new Error(`DevTools debug capacity reached (${DEVTOOLS_MAX_ACTIVE_DEBUG_SESSIONS} active tabs)`);
  }
  const lease = await _acquireCdp(tabId, 'javascript-debugger');
  const session = {
    tabId, url: String(tab.url || ''), lease, active: true,
    startedAt: Date.now(), expiresAt: Date.now() + ttlMs,
    consoleEntries: [], consoleChars: 0, droppedConsoleEntries: 0,
    contexts: new Map(), targets: new Map(), scripts: new Map(),
    breakpoints: new Map(), paused: null, pauseFailsafe: null,
    expiryTimer: null,
  };
  _debugSessions.set(tabId, session);
  try {
    await _runWithCdpLease(lease, async (target) => {
      await chrome.debugger.sendCommand(target, 'Runtime.enable');
      await chrome.debugger.sendCommand(target, 'Log.enable');
      await chrome.debugger.sendCommand(target, 'Debugger.enable', {
        maxScriptsCacheSize: 2 * 1024 * 1024,
      });
      await chrome.debugger.sendCommand(target, 'Target.setAutoAttach', {
        autoAttach: true, waitForDebuggerOnStart: false, flatten: true,
        filter: [{type: 'iframe', exclude: false}, {type: 'worker', exclude: false}],
      });
    });
    session.expiryTimer = setTimeout(() => {
      _stopDebugSession(tabId, 'timeout').catch(() => {});
    }, ttlMs);
    return session;
  } catch (error) {
    _debugSessions.delete(tabId);
    session.active = false;
    await _releaseCdp(lease).catch(() => {});
    throw error;
  }
}

async function _resumeDebugSession(session, action = 'resume', failsafe = false) {
  if (!session || !session.active || !session.paused) {
    return {resumed: false, reason: 'not-paused'};
  }
  const commands = {
    resume: 'Debugger.resume', step_over: 'Debugger.stepOver',
    step_into: 'Debugger.stepInto', step_out: 'Debugger.stepOut',
  };
  const command = commands[action];
  if (!command) throw new Error(`Unsupported debugger continuation: ${action}`);
  const target = _debugTarget(session, session.paused.sessionId);
  await _runWithCdpLease(
    session.lease, () => chrome.debugger.sendCommand(target, command));
  if (session.pauseFailsafe) clearTimeout(session.pauseFailsafe);
  session.pauseFailsafe = null;
  session.paused = null;
  return {resumed: true, action, failsafe: !!failsafe};
}

async function _stopDebugSession(tabId, reason = 'requested') {
  const key = Number(tabId);
  const session = _debugSessions.get(key);
  if (!session) return {stopped: false, reason: 'not-active'};
  _debugSessions.delete(key);
  session.active = false;
  if (session.expiryTimer) clearTimeout(session.expiryTimer);
  if (session.pauseFailsafe) clearTimeout(session.pauseFailsafe);
  await _runWithCdpLease(session.lease, async () => {
    try {
      if (session.paused) {
        await chrome.debugger.sendCommand(
          _debugTarget(session, session.paused.sessionId), 'Debugger.resume');
      }
    } catch (_) {}
    try {
      await chrome.debugger.sendCommand(session.lease.target, 'Target.setAutoAttach', {
        autoAttach: false, waitForDebuggerOnStart: false, flatten: true,
      });
    } catch (_) {}
    try {
      await chrome.debugger.sendCommand(session.lease.target, 'Debugger.disable');
    } catch (_) {}
  }).catch(() => {});
  _rememberDevtoolsSnapshot(key, {
    url: session.url, entries: session.consoleEntries,
    droppedEntries: session.droppedConsoleEntries, capturedAt: Date.now(),
  });
  await _releaseCdp(session.lease).catch(() => {});
  return {stopped: true, reason};
}

function _objectBudgetText(state, value, limit = 4000) {
  const text = _devtoolsText(value, Math.min(limit, Math.max(0, state.maxChars - state.chars)));
  state.chars += text.length;
  if (state.chars >= state.maxChars) state.truncated = true;
  return text;
}

async function _serializeRemoteObject(target, remote, depth, state) {
  state.nodes++;
  if (state.nodes > state.maxNodes || state.chars >= state.maxChars) {
    state.truncated = true;
    return '[truncated]';
  }
  const value = remote || {};
  if (Object.prototype.hasOwnProperty.call(value, 'value')) {
    if (typeof value.value === 'string') return _objectBudgetText(state, value.value);
    return value.value;
  }
  if (value.unserializableValue != null) {
    return _objectBudgetText(state, value.unserializableValue, 200);
  }
  if (!value.objectId) {
    return _objectBudgetText(state, value.description || value.type || 'undefined', 1000);
  }
  if (state.seen.has(value.objectId)) return '[circular]';
  const descriptor = {
    type: String(value.type || 'object'),
    ...(value.subtype ? {subtype: String(value.subtype)} : {}),
    ...(value.className ? {className: String(value.className)} : {}),
    ...(value.description
      ? {description: _objectBudgetText(state, value.description, 1000)} : {}),
  };
  if (depth >= state.maxDepth) return descriptor;
  state.seen.add(value.objectId);
  let response;
  try {
    response = await chrome.debugger.sendCommand(target, 'Runtime.getProperties', {
      objectId: value.objectId, ownProperties: true,
      accessorPropertiesOnly: false, generatePreview: true,
    });
  } catch (error) {
    descriptor.error = _objectBudgetText(
      state, (error && error.message) || error || 'properties unavailable', 500);
    return descriptor;
  }
  const properties = {};
  for (const property of (response.result || []).slice(0, 80)) {
    if (state.nodes >= state.maxNodes || state.chars >= state.maxChars) {
      state.truncated = true;
      break;
    }
    const name = _objectBudgetText(state, property.name, 300);
    if (property.value) {
      properties[name] = await _serializeRemoteObject(
        target, property.value, depth + 1, state);
    } else if (property.get || property.set) {
      // Never invoke getters while inspecting: getter side effects would turn
      // a structural read into hidden page mutation.
      properties[name] = property.get && property.set
        ? '[Getter/Setter]' : property.get ? '[Getter]' : '[Setter]';
    } else {
      properties[name] = '[unavailable]';
    }
  }
  descriptor.properties = properties;
  return descriptor;
}

function _exceptionPayload(details) {
  if (!details) return null;
  const stack = details.stackTrace && details.stackTrace.callFrames || [];
  return {
    text: _devtoolsText(
      (details.exception && details.exception.description) || details.text || 'Evaluation failed',
      12000),
    url: String(details.url || ''),
    lineNumber: Number(details.lineNumber) || 0,
    columnNumber: Number(details.columnNumber) || 0,
    stack: stack.slice(0, 30).map((frame) => ({
      functionName: String(frame.functionName || '(anonymous)'),
      url: String(frame.url || ''), lineNumber: Number(frame.lineNumber) || 0,
      columnNumber: Number(frame.columnNumber) || 0,
    })),
  };
}

async function _evaluateWithTarget(target, expression, params) {
  const objectGroup = `tofu-devtools-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  try {
    const request = {
      expression: String(expression), objectGroup,
      includeCommandLineAPI: true, silent: false,
      awaitPromise: params.awaitPromise !== false,
      userGesture: true, returnByValue: false, generatePreview: true,
    };
    if (params.contextId != null) request.contextId = Number(params.contextId);
    const response = await chrome.debugger.sendCommand(
      target, 'Runtime.evaluate', request);
    if (response.exceptionDetails) {
      return {ok: false, exception: _exceptionPayload(response.exceptionDetails)};
    }
    const state = {
      maxDepth: Math.max(0, Math.min(6, Number(params.maxDepth) || 3)),
      maxNodes: DEVTOOLS_MAX_OBJECT_NODES,
      maxChars: DEVTOOLS_MAX_OBJECT_CHARS,
      nodes: 0, chars: 0, truncated: false, seen: new Set(),
    };
    const value = await _serializeRemoteObject(target, response.result, 0, state);
    return {
      ok: true, value,
      resultType: String((response.result && response.result.type) || ''),
      resultSubtype: String((response.result && response.result.subtype) || ''),
      nodesInspected: state.nodes, truncated: state.truncated,
    };
  } finally {
    try {
      await chrome.debugger.sendCommand(
        target, 'Runtime.releaseObjectGroup', {objectGroup});
    } catch (_) {}
  }
}

function _debugContextTarget(session, params) {
  const requestedSessionId = String(params.sessionId || '');
  if (requestedSessionId) return _debugTarget(session, requestedSessionId);
  if (params.contextId == null) return session.lease.target;
  const contextId = Number(params.contextId);
  const matches = Array.from(session.contexts.values())
    .filter((context) => context.id === contextId);
  if (matches.length > 1) {
    throw new Error('context_id is ambiguous; pass session_id from context_list');
  }
  return _debugTarget(session, matches[0] && matches[0].sessionId);
}

function _debugScript(session, scriptId, requestedSessionId = '') {
  const matches = Array.from(session.scripts.values()).filter((script) => (
    script.scriptId === scriptId
    && (!requestedSessionId || script.sessionId === requestedSessionId)
  ));
  if (matches.length > 1) {
    throw new Error('script_id is ambiguous; pass its session_id');
  }
  return matches[0] || null;
}

function _debugBreakpoint(session, breakpointId, requestedSessionId = '') {
  const matches = Array.from(session.breakpoints.values()).filter((breakpoint) => (
    breakpoint.breakpointId === breakpointId
    && (!requestedSessionId || breakpoint.sessionId === requestedSessionId)
  ));
  if (matches.length > 1) {
    throw new Error('breakpoint_id is ambiguous; pass its session_id');
  }
  return matches[0] || null;
}

async function _observeDevtools(tabId, observeMs, includeContexts = false) {
  if (_devtoolsObservers.size >= DEVTOOLS_MAX_OBSERVERS) {
    throw new Error(`Console observer capacity reached (${DEVTOOLS_MAX_OBSERVERS} active)`);
  }
  const observerId = crypto.randomUUID();
  const observer = {
    observerId, tabId: Number(tabId), consoleEntries: [], consoleChars: 0,
    droppedConsoleEntries: 0, contexts: [], startedAt: Date.now(),
  };
  const lease = await _acquireCdp(tabId, `console-observer:${observerId}`);
  _devtoolsObservers.set(observerId, observer);
  try {
    await _runWithCdpLease(lease, async (target) => {
      await chrome.debugger.sendCommand(target, 'Runtime.enable');
      await chrome.debugger.sendCommand(target, 'Log.enable');
    });
    await new Promise((resolve) => setTimeout(
      resolve, Math.max(50, Math.min(5000, Number(observeMs) || 250))));
    return {
      entries: observer.consoleEntries,
      droppedEntries: observer.droppedConsoleEntries,
      contexts: includeContexts ? observer.contexts : undefined,
      observedMs: Date.now() - observer.startedAt,
    };
  } finally {
    _devtoolsObservers.delete(observerId);
    await _releaseCdp(lease).catch(() => {});
  }
}

function _mergeConsoleEntries(recent, observed) {
  const out = [];
  const seen = new Set();
  for (const entry of [...(recent || []), ...(observed || [])]) {
    let key;
    try { key = JSON.stringify(entry); } catch (_) { key = String(entry); }
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(entry);
    if (out.length >= DEVTOOLS_MAX_LOG_ENTRIES) break;
  }
  return out;
}

async function cmdDevtools(params) {
  const tabId = Number(params.tabId);
  if (!Number.isInteger(tabId) || tabId <= 0) throw new Error('No valid tabId specified');
  const tab = await chrome.tabs.get(tabId);
  if (tab.url && isProtectedUrl(tab.url)) {
    throw new Error(`Cannot use DevTools on protected page: ${tab.url}`);
  }
  await _assertExpectedDomain(params, tab);
  const action = String(params.action || 'console_read');
  const result = {action, url: String(tab.url || ''), title: String(tab.title || '')};

  if (action === 'console_clear') {
    _recentDevtoolsByTab.delete(tabId);
    const capture = _networkCaptureForTab(tabId);
    const debug = _debugSessions.get(tabId);
    for (const sink of [capture, debug]) {
      if (!sink) continue;
      sink.consoleEntries = [];
      sink.consoleChars = 0;
      sink.droppedConsoleEntries = 0;
    }
    return {...result, cleared: true};
  }
  if (action === 'console_read' || action === 'context_list') {
    const recent = _recentDevtoolsByTab.get(tabId);
    const observed = await _observeDevtools(
      tabId, params.observeMs, action === 'context_list');
    return {
      ...result,
      entries: action === 'console_read'
        ? _mergeConsoleEntries(recent && recent.entries, observed.entries) : undefined,
      droppedEntries: Number((recent && recent.droppedEntries) || 0)
        + Number(observed.droppedEntries || 0),
      contexts: action === 'context_list' ? observed.contexts : undefined,
      observedMs: observed.observedMs,
    };
  }
  if (action === 'evaluate' || action === 'inspect') {
    const expression = String(params.expression || '');
    if (!expression || expression.length > 50000) {
      throw new Error('expression is required and must not exceed 50,000 characters');
    }
    const active = _debugSessions.get(tabId);
    const evalParams = {...params, maxDepth: action === 'inspect'
      ? Math.max(1, Number(params.maxDepth) || 4)
      : Math.max(0, Number(params.maxDepth) || 2)};
    const evaluation = active && active.active
      ? await _runWithCdpLease(active.lease, () =>
          _evaluateWithTarget(
            _debugContextTarget(active, evalParams), expression, evalParams))
      : await _cdpRun(tabId, async (target) => {
          await chrome.debugger.sendCommand(target, 'Runtime.enable');
          return _evaluateWithTarget(target, expression, evalParams);
        }, `devtools-${action}`);
    return {...result, ...evaluation};
  }
  if (action === 'debug_start') {
    const ttlMs = Math.max(10000, Math.min(
      DEVTOOLS_DEBUG_TTL_MS, Number(params.sessionTtlMs) || 60000));
    const session = await _startDebugSession(tab, ttlMs);
    // Give Runtime/Debugger a brief turn to report existing contexts/scripts.
    await new Promise((resolve) => setTimeout(resolve, 100));
    return {...result, ..._debugState(session)};
  }
  if (action === 'debug_stop') {
    return {...result, ...(await _stopDebugSession(tabId, 'requested'))};
  }

  const session = _debugSessions.get(tabId);
  if (!session || !session.active) {
    throw new Error('No active debug session; call action=debug_start first');
  }
  if (action === 'debug_state') return {...result, ..._debugState(session)};
  if (['resume', 'step_over', 'step_into', 'step_out'].includes(action)) {
    return {...result, ...(await _resumeDebugSession(session, action)),
            state: _debugState(session)};
  }
  if (action === 'pause') {
    const target = _debugTarget(session, String(params.sessionId || ''));
    await _runWithCdpLease(
      session.lease,
      () => chrome.debugger.sendCommand(target, 'Debugger.pause'));
    await new Promise((resolve) => setTimeout(resolve, 80));
    return {...result, requested: true, state: _debugState(session)};
  }
  if (action === 'breakpoint_set') {
    if (session.breakpoints.size >= DEVTOOLS_MAX_BREAKPOINTS) {
      throw new Error(`Breakpoint capacity reached (${DEVTOOLS_MAX_BREAKPOINTS})`);
    }
    const sourceUrl = String(params.sourceUrl || '');
    if (!sourceUrl) throw new Error('source_url is required for breakpoint_set');
    let breakpointSessionId = String(params.sessionId || '');
    if (!breakpointSessionId) {
      const scriptSessions = new Set(
        Array.from(session.scripts.values())
          .filter((script) => script.url === sourceUrl)
          .map((script) => script.sessionId));
      if (scriptSessions.size === 1) {
        breakpointSessionId = scriptSessions.values().next().value;
      }
    }
    const breakpointTarget = _debugTarget(session, breakpointSessionId);
    const response = await _runWithCdpLease(session.lease, () =>
      chrome.debugger.sendCommand(
      breakpointTarget, 'Debugger.setBreakpointByUrl', {
        url: sourceUrl,
        lineNumber: Math.max(0, Number(params.lineNumber) || 0),
        columnNumber: Math.max(0, Number(params.columnNumber) || 0),
        condition: String(params.condition || '').slice(0, 10000),
      }));
    const breakpoint = {
      breakpointId: String(response.breakpointId || ''), sourceUrl,
      sessionId: breakpointSessionId,
      lineNumber: Math.max(0, Number(params.lineNumber) || 0),
      columnNumber: Math.max(0, Number(params.columnNumber) || 0),
      locations: (response.locations || []).slice(0, 20),
    };
    session.breakpoints.set(
      `${breakpointSessionId || 'root'}:${breakpoint.breakpointId}`,
      breakpoint);
    return {...result, breakpoint};
  }
  if (action === 'breakpoint_remove') {
    const breakpointId = String(params.breakpointId || '');
    if (!breakpointId) throw new Error('breakpoint_id is required');
    const breakpoint = _debugBreakpoint(
      session, breakpointId, String(params.sessionId || ''));
    if (!breakpoint) throw new Error('Unknown or expired breakpoint_id');
    const breakpointTarget = _debugTarget(session, breakpoint.sessionId);
    await _runWithCdpLease(session.lease, () =>
      chrome.debugger.sendCommand(
        breakpointTarget, 'Debugger.removeBreakpoint', {breakpointId}));
    session.breakpoints.delete(
      `${breakpoint.sessionId || 'root'}:${breakpointId}`);
    return {...result, removed: breakpointId, sessionId: breakpoint.sessionId};
  }
  if (action === 'frame_evaluate') {
    if (!session.paused) throw new Error('The debugger is not paused');
    const expression = String(params.expression || '');
    const callFrameId = String(params.callFrameId || '');
    if (!expression || !callFrameId) {
      throw new Error('expression and call_frame_id are required');
    }
    const target = _debugTarget(session, session.paused.sessionId);
    const response = await _runWithCdpLease(session.lease, () =>
      chrome.debugger.sendCommand(target, 'Debugger.evaluateOnCallFrame', {
        callFrameId, expression, includeCommandLineAPI: true,
        silent: false, returnByValue: true, generatePreview: true,
      }));
    return {
      ...result,
      ok: !response.exceptionDetails,
      value: response.result ? _remoteObjectPreview(response.result) : null,
      exception: _exceptionPayload(response.exceptionDetails),
    };
  }
  if (action === 'script_source') {
    const scriptId = String(params.scriptId || '');
    const script = _debugScript(
      session, scriptId, String(params.sessionId || ''));
    if (!script) throw new Error('Unknown or expired script_id');
    const target = _debugTarget(session, script.sessionId);
    const response = await _runWithCdpLease(session.lease, () =>
      chrome.debugger.sendCommand(
        target, 'Debugger.getScriptSource', {scriptId}));
    const source = String(response.scriptSource || '');
    return {
      ...result, script,
      source: source.slice(0, DEVTOOLS_MAX_OBJECT_CHARS),
      sourceLength: source.length,
      truncated: source.length > DEVTOOLS_MAX_OBJECT_CHARS,
    };
  }
  throw new Error(`Unknown DevTools action: ${action}`);
}

function _networkPatternMatches(url, patterns) {
  if (!patterns || !patterns.length) return true;
  return patterns.some(p => {
    const escaped = String(p).replace(/[.+?^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*');
    try { return new RegExp(`^${escaped}$`).test(url); } catch (_) { return false; }
  });
}

function _normalizedResearchCaptureHints(rawHints) {
  const hints = [];
  for (const raw of Array.isArray(rawHints) ? rawHints.slice(0, 5) : []) {
    if (!raw || !['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE'].includes(String(raw.method))) continue;
    let origin;
    try {
      const parsed = new URL(String(raw.origin || ''));
      if (!['http:', 'https:'].includes(parsed.protocol) || parsed.origin !== String(raw.origin)) continue;
      origin = parsed.origin;
    } catch (_) { continue; }
    const pathTemplate = String(raw.pathTemplate || '');
    const segments = pathTemplate === '/' ? [] : pathTemplate.split('/').slice(1);
    if (!pathTemplate.startsWith('/') || pathTemplate.length > 512 ||
        segments.length > 24 || segments.some((segment) =>
          !/^(?:[a-z][a-z_-]{0,39}|\{segment\}|\{truncated\})$/.test(segment))) continue;
    hints.push({method: String(raw.method), origin, segments});
  }
  return hints;
}

function _networkCaptureMatchesHint(capture, row) {
  if (!capture.priorityHints.length) return false;
  let parsed;
  try { parsed = new URL(String(row.url || '')); } catch (_) { return false; }
  const actual = parsed.pathname.split('/').filter(Boolean);
  return capture.priorityHints.some((hint) => {
    if (hint.method !== String(row.method || 'GET').toUpperCase() || hint.origin !== parsed.origin) return false;
    const truncatedAt = hint.segments.indexOf('{truncated}');
    const expectedLength = truncatedAt >= 0 ? truncatedAt : hint.segments.length;
    if (actual.length < expectedLength || (truncatedAt < 0 && actual.length !== expectedLength)) return false;
    return hint.segments.slice(0, expectedLength).every(
      (segment, index) => segment === '{segment}' || segment === actual[index].toLowerCase());
  });
}

function _networkCaptureForTab(tabId) {
  const captureId = _networkCaptureByTab.get(Number(tabId));
  return captureId ? _networkCaptures.get(captureId) : null;
}

function _networkBodyTypeAllowed(type, mimeType) {
  const kind = String(type || '').toLowerCase();
  const mime = String(mimeType || '').toLowerCase();
  if (['image', 'media', 'font', 'stylesheet', 'script', 'document'].includes(kind)) {
    return false;
  }
  return kind === 'xhr' || kind === 'fetch' || kind === 'eventsource' ||
    !mime || /(?:json|text|xml|graphql|javascript)/i.test(mime);
}

function _decodeBase64Utf8(value) {
  const binary = atob(String(value || ''));
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new TextDecoder('utf-8', { fatal: false }).decode(bytes);
}

async function _captureResponseBody(capture, row, encodedDataLength) {
  const fullSizeHint = Math.max(0, Number(encodedDataLength) || 0);
  if (fullSizeHint > NETWORK_CAPTURE_MAX_BODY_CHARS) {
    row.responseBodyTruncated = true;
    row.responseBodyFullSize = fullSizeHint;
    capture.droppedBodies++;
    return;
  }
  const priority = _networkCaptureMatchesHint(capture, row);
  const ceiling = priority ? NETWORK_CAPTURE_MAX_TOTAL_BODY_CHARS :
    NETWORK_CAPTURE_MAX_TOTAL_BODY_CHARS - capture.priorityReserveChars;
  const remaining = ceiling - capture.totalBodyChars;
  if (remaining <= 0) {
    row.responseBodyTruncated = true;
    row.responseBodyFullSize = fullSizeHint || null;
    capture.droppedBodies++;
    return;
  }
  try {
    const response = await chrome.debugger.sendCommand(
      capture.target, 'Network.getResponseBody', { requestId: row.requestId });
    let text = response && response.body ? String(response.body) : '';
    if (response && response.base64Encoded) text = _decodeBase64Utf8(text);
    // A missing MIME type may still be a JSON API.  Refuse obvious binary
    // content before it enters the transient result envelope.
    if (!text || text.includes('\u0000')) return;
    const allowed = Math.max(0, Math.min(
      NETWORK_CAPTURE_MAX_BODY_CHARS, remaining));
    const fullSize = text.length;
    if (fullSize > allowed) {
      text = text.slice(0, allowed);
      row.responseBodyTruncated = true;
      row.responseBodyFullSize = fullSize;
    }
    row.responsePreview = text;
    capture.totalBodyChars += text.length;
    if (priority) capture.priorityBodyMatches++;
    capture.lastActivityAt = Date.now();
  } catch (error) {
    // Bodies can be unavailable for redirects, cached entries, preflight or a
    // response evicted from CDP's bounded buffer.  Metadata remains useful and
    // the failure is intentionally not promoted to a page-fetch failure.
    row.bodyError = String((error && error.message) || error || 'body unavailable').slice(0, 160);
  }
}

function _onNetworkDebuggerEvent(source, method, params) {
  const capture = _networkCaptureForTab(source && source.tabId);
  if (!capture || !capture.cdpAttached || capture.stopping) return;
  capture.lastActivityAt = Date.now();
  if (method === 'Network.requestWillBeSent') {
    if (!capture.requestMethods.has(String(params.requestId)) &&
        capture.requestMethods.size >= NETWORK_CAPTURE_MAX_TRACKED_REQUESTS) {
      const oldest = capture.requestMethods.keys().next().value;
      capture.requestMethods.delete(oldest);
    }
    capture.requestMethods.set(
      String(params.requestId), String((params.request && params.request.method) || 'GET'));
    return;
  }
  if (method === 'Network.webSocketCreated') {
    const url = String(params.url || '');
    if (_networkPatternMatches(url, capture.patterns)) {
      if (capture.webSocketUrls.size >= NETWORK_CAPTURE_MAX_WEBSOCKET_FRAMES) {
        capture.webSocketUrls.delete(capture.webSocketUrls.keys().next().value);
      }
      capture.webSocketUrls.set(String(params.requestId), url);
    }
    return;
  }
  if (method === 'Network.webSocketFrameReceived') {
    const requestId = String(params.requestId || '');
    const url = capture.webSocketUrls.get(requestId) || '';
    const frame = params.response || {};
    if (!url || Number(frame.opcode) !== 1) return;
    if (capture.webSocketFrameCount >= NETWORK_CAPTURE_MAX_WEBSOCKET_FRAMES ||
        capture.responses.length >= NETWORK_CAPTURE_MAX_ENTRIES) {
      capture.droppedEntries++;
      return;
    }
    let text = String(frame.payloadData || '');
    if (!text || text.includes('\u0000')) return;
    // WebSocket frames are unhinted evidence, so they cannot consume the
    // response-body reserve held for previously observed HTTP endpoints.
    const remaining = NETWORK_CAPTURE_MAX_TOTAL_BODY_CHARS -
      capture.priorityReserveChars - capture.totalBodyChars;
    if (remaining <= 0) {
      capture.droppedBodies++;
      return;
    }
    const fullSize = text.length;
    const allowed = Math.min(NETWORK_CAPTURE_MAX_BODY_CHARS, remaining);
    const truncated = fullSize > allowed;
    if (truncated) {
      text = text.slice(0, allowed);
      capture.droppedBodies++;
    }
    capture.responses.push({
      url, method: 'WS', status: 101, responseStatus: 101,
      contentType: 'application/websocket+json',
      responseContentType: 'application/websocket+json', type: 'WebSocket',
      responsePreview: text, responseBodyTruncated: truncated,
      responseBodyFullSize: truncated ? fullSize : undefined,
      timestamp: Date.now(),
    });
    capture.totalBodyChars += text.length;
    capture.webSocketFrameCount++;
    capture.lastActivityAt = Date.now();
    return;
  }
  if (method === 'Network.webSocketClosed') {
    capture.webSocketUrls.delete(String(params.requestId || ''));
    return;
  }
  if (method === 'Network.responseReceived') {
    const response = params.response || {};
    const url = String(response.url || '');
    if (!_networkPatternMatches(url, capture.patterns) ||
        !_networkBodyTypeAllowed(params.type, response.mimeType)) return;
    if (capture.responses.length >= NETWORK_CAPTURE_MAX_ENTRIES) {
      capture.droppedEntries++;
      return;
    }
    const row = {
      requestId: String(params.requestId),
      url,
      method: capture.requestMethods.get(String(params.requestId)) || 'GET',
      status: Number(response.status) || 0,
      responseStatus: Number(response.status) || 0,
      contentType: String(response.mimeType || ''),
      responseContentType: String(response.mimeType || ''),
      type: String(params.type || ''),
      fromCache: !!response.fromDiskCache || !!response.fromPrefetchCache,
      timestamp: Date.now(),
    };
    capture.responses.push(row);
    capture.responseByRequest.set(row.requestId, row);
    return;
  }
  if (method === 'Network.loadingFinished') {
    const requestId = String(params.requestId);
    const row = capture.responseByRequest.get(requestId);
    capture.responseByRequest.delete(requestId);
    capture.requestMethods.delete(requestId);
    if (!row) return;
    // Serialize body reads so every request observes the updated shared byte
    // budget. Concurrent getResponseBody calls could otherwise each reserve
    // the same remaining 1 MiB and exceed the personal-computer budget.
    const pending = capture.bodyCaptureTail.then(
      () => _captureResponseBody(capture, row, params.encodedDataLength));
    capture.bodyCaptureTail = pending.catch(() => {});
    capture.pendingBodies.add(pending);
    pending.finally(() => capture.pendingBodies.delete(pending));
    return;
  }
  if (method === 'Network.loadingFailed') {
    const requestId = String(params.requestId);
    capture.responseByRequest.delete(requestId);
    capture.requestMethods.delete(requestId);
  }
}

function _onNetworkDebuggerDetach(source, reason) {
  const capture = _networkCaptureForTab(source && source.tabId);
  if (!capture) return;
  capture.cdpAttached = false;
  capture.detachReason = String(reason || 'detached').slice(0, 120);
}

function _onNetworkCompleted(details) {
  for (const capture of _networkCaptures.values()) {
    // CDP owns the richer path. webRequest is only the explicit fallback when
    // DevTools or another extension already holds the debugger attachment.
    if (capture.cdpAttached) continue;
    if (details.tabId !== capture.tabId || !_networkPatternMatches(details.url, capture.patterns)) continue;
    if (capture.responses.length >= NETWORK_CAPTURE_MAX_ENTRIES) {
      capture.droppedEntries++;
      continue;
    }
    capture.responses.push({
      url: details.url, status: details.statusCode,
      responseStatus: details.statusCode, method: details.method,
      type: details.type, fromCache: !!details.fromCache,
      timestamp: details.timeStamp,
    });
    capture.lastActivityAt = Date.now();
  }
}

function _publicNetworkSnapshot(capture) {
  if (!capture) return { responses: [], capturedAt: Date.now() };
  return {
    responses: capture.responses.map((row) => {
      const clean = Object.assign({}, row);
      delete clean.requestId;
      delete clean.bodyError;
      return clean;
    }),
    capturedAt: Date.now(),
    startedAt: capture.startedAt,
    bodyCapture: !!capture.everCdpAttached,
    droppedEntries: capture.droppedEntries,
    droppedBodies: capture.droppedBodies,
    webSocketFrameCount: capture.webSocketFrameCount,
    totalBodyChars: capture.totalBodyChars,
    priorityHintCount: capture.priorityHints.length,
    priorityBodyMatches: capture.priorityBodyMatches,
    priorityReserveChars: capture.priorityReserveChars,
    consoleEntries: Array.isArray(capture.consoleEntries)
      ? capture.consoleEntries.slice(0, DEVTOOLS_MAX_LOG_ENTRIES) : [],
    droppedConsoleEntries: Number(capture.droppedConsoleEntries) || 0,
    pageUrl: capture.pageUrl || '',
    ...(capture.attachError ? { captureError: capture.attachError } : {}),
  };
}

function _rememberNetworkSnapshot(tabId, snapshot) {
  const key = Number(tabId);
  if (!key || !snapshot) return;
  _recentNetworkByTab.delete(key);
  _recentNetworkByTab.set(key, snapshot);
  while (_recentNetworkByTab.size > NETWORK_CAPTURE_RECENT_TABS) {
    const oldest = _recentNetworkByTab.keys().next().value;
    _recentNetworkByTab.delete(oldest);
  }
}

async function cmdNetworkCaptureStart(params) {
  return _startNetworkCapture(params);
}

async function _startNetworkCapture(params, { allowInertBlank = false } = {}) {
  const tab = await chrome.tabs.get(Number(params.tabId));
  const isAllowedInertBlank = allowInertBlank && tab.url === 'about:blank';
  if (tab.url && isProtectedUrl(tab.url) && !isAllowedInertBlank) {
    throw new Error(`Cannot capture protected page: ${tab.url}`);
  }
  await _assertExpectedDomain(params, tab);
  const captureBodies = params.captureBodies === true;
  const priorityHints = _normalizedResearchCaptureHints(params.captureHints);
  const priorBodyCapture = _networkCaptureByTab.get(Number(params.tabId));
  if (captureBodies && priorBodyCapture) {
    await _stopNetworkCaptureInternal(priorBodyCapture, { remember: true });
  }
  if (_networkCaptures.size >= NETWORK_CAPTURE_MAX_ACTIVE) {
    throw new Error(
      `Network capture capacity reached (${NETWORK_CAPTURE_MAX_ACTIVE} active tasks)`);
  }
  const captureId = crypto.randomUUID();
  const capture = {
    captureId, tabId: Number(params.tabId),
    target: { tabId: Number(params.tabId) },
    patterns: Array.isArray(params.urlPatterns) ? params.urlPatterns : [],
    responses: [], responseByRequest: new Map(), requestMethods: new Map(),
    webSocketUrls: new Map(),
    pendingBodies: new Set(), totalBodyChars: 0, droppedEntries: 0,
    droppedBodies: 0, webSocketFrameCount: 0,
    startedAt: Date.now(), lastActivityAt: Date.now(),
    bodyCaptureTail: Promise.resolve(), pageUrl: '', captureBodies,
    priorityHints, priorityBodyMatches: 0,
    priorityReserveChars: priorityHints.length ? NETWORK_CAPTURE_HINT_RESERVE_CHARS : 0,
    cdpAttached: false, everCdpAttached: false, stopping: false,
    cdpLease: null,
    consoleEntries: [], consoleChars: 0, droppedConsoleEntries: 0,
  };
  _networkCaptures.set(captureId, capture);
  if (captureBodies) _networkCaptureByTab.set(capture.tabId, captureId);
  if (!_networkListenerInstalled) {
    chrome.webRequest.onCompleted.addListener(_onNetworkCompleted, {urls: ['<all_urls>']});
    _networkListenerInstalled = true;
  }
  if (captureBodies) {
    try {
      capture.cdpLease = await _acquireCdp(
        capture.tabId, `network-capture:${capture.captureId}`);
      capture.target = capture.cdpLease.target;
      capture.cdpAttached = true;
      capture.everCdpAttached = true;
      await _runWithCdpLease(capture.cdpLease, async (target) => {
        await chrome.debugger.sendCommand(target, 'Network.enable', {
          maxTotalBufferSize: 2 * 1024 * 1024,
          maxResourceBufferSize: 512 * 1024,
          maxPostDataSize: 0,
        });
        // Capture console/errors over the same pre-navigation lifetime. These
        // entries are bounded separately and exposed only on explicit
        // DevTools reads; normal network rendering ignores them.
        await chrome.debugger.sendCommand(target, 'Runtime.enable');
        await chrome.debugger.sendCommand(target, 'Log.enable');
      });
    } catch (error) {
      capture.attachError = String(
        (error && error.message) || error || 'CDP attach failed').slice(0, 200);
      capture.cdpAttached = false;
      if (capture.cdpLease) {
        await _releaseCdp(capture.cdpLease).catch(() => {});
        capture.cdpLease = null;
      }
      console.warn('[NetworkCapture] CDP body capture unavailable; URL metadata only:',
                   capture.attachError);
    }
  }
  return {captureId, page: await cmdPageState(params)};
}

async function _stopNetworkCaptureInternal(captureId, { remember = true } = {}) {
  const key = String(captureId || '');
  const capture = _networkCaptures.get(key);
  if (!capture) return { responses: [], capturedAt: Date.now() };
  capture.stopping = true;
  if (capture.pendingBodies.size) {
    await Promise.race([
      Promise.allSettled(Array.from(capture.pendingBodies)),
      new Promise((resolve) => setTimeout(resolve, 1500)),
    ]);
  }
  if (capture.cdpAttached) {
    try {
      await _runWithCdpLease(capture.cdpLease, (target) =>
        chrome.debugger.sendCommand(target, 'Network.disable'));
    } catch (_) {}
  }
  if (capture.cdpLease) {
    await _releaseCdp(capture.cdpLease).catch(() => {});
    capture.cdpLease = null;
  }
  try {
    const tab = await chrome.tabs.get(capture.tabId);
    capture.pageUrl = String((tab && tab.url) || '');
  } catch (_) {
    capture.pageUrl = '';
  }
  _networkCaptures.delete(key);
  if (_networkCaptureByTab.get(capture.tabId) === key) {
    _networkCaptureByTab.delete(capture.tabId);
  }
  if (!_networkCaptures.size && _networkListenerInstalled) {
    chrome.webRequest.onCompleted.removeListener(_onNetworkCompleted);
    _networkListenerInstalled = false;
  }
  const snapshot = _publicNetworkSnapshot(capture);
  if (remember && capture.captureBodies) {
    _rememberNetworkSnapshot(capture.tabId, snapshot);
    _rememberDevtoolsSnapshot(capture.tabId, {
      url: snapshot.pageUrl || '',
      entries: snapshot.consoleEntries || [],
      droppedEntries: snapshot.droppedConsoleEntries || 0,
      capturedAt: snapshot.capturedAt,
    });
  }
  return snapshot;
}

async function cmdNetworkCaptureStop(params) {
  const captureId = String(params.captureId || '');
  const snapshot = await _stopNetworkCaptureInternal(captureId, { remember: true });
  return Object.assign({ captureId, stoppedAt: Date.now() }, snapshot);
}

function _pageStabilitySignature() {
  const body = document.body;
  const root = document.documentElement;
  return {
    readyState: document.readyState,
    textLength: body ? (body.textContent || '').length : 0,
    elementCount: body ? body.getElementsByTagName('*').length : 0,
    scrollHeight: root ? root.scrollHeight : 0,
    resourceCount: performance.getEntriesByType('resource').length,
  };
}

async function _waitForCapturedPageSettle(tabId, captureId, timeoutMs) {
  const maxWait = Math.max(600, Math.min(
    NETWORK_CAPTURE_SETTLE_MAX_MS, Math.floor((Number(timeoutMs) || 15000) / 3)));
  const deadline = Date.now() + maxWait;
  let priorSignature = '';
  let stableReads = 0;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 250));
    const capture = _networkCaptures.get(String(captureId || ''));
    if (!capture) return;
    let signature = '';
    try {
      const results = await chrome.scripting.executeScript({
        target: { tabId: Number(tabId) }, func: _pageStabilitySignature,
      });
      signature = JSON.stringify(results && results[0] && results[0].result || {});
    } catch (_) {
      stableReads = 0;
      continue;
    }
    stableReads = signature === priorSignature ? stableReads + 1 : 0;
    priorSignature = signature;
    const idleFor = Date.now() - capture.lastActivityAt;
    if (stableReads >= 2 && idleFor >= NETWORK_CAPTURE_IDLE_MS) return;
  }
}

async function cmdWaitDownload(params) {
  const downloadId = Number(params.downloadId);
  const timeoutMs = Math.max(1000, Math.min(120000, Number(params.timeoutMs) || 60000));
  const current = await chrome.downloads.search({id: downloadId});
  if (!current.length) throw new Error(`Download ${downloadId} not found`);
  if (current[0].state === 'complete') {
    return {...current[0], location: 'device_downloads', clientId: CLIENT_ID};
  }
  if (current[0].state === 'interrupted') throw new Error(current[0].error || 'Download interrupted');
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.downloads.onChanged.removeListener(listener);
      reject(new Error(`Download ${downloadId} timed out`));
    }, timeoutMs);
    const listener = async (delta) => {
      if (delta.id !== downloadId || !delta.state) return;
      if (delta.state.current === 'complete') {
        clearTimeout(timer); chrome.downloads.onChanged.removeListener(listener);
        const rows = await chrome.downloads.search({id: downloadId});
        resolve({
          ...(rows[0] || {id: downloadId, state: 'complete'}),
          location: 'device_downloads', clientId: CLIENT_ID,
        });
      } else if (delta.state.current === 'interrupted') {
        clearTimeout(timer); chrome.downloads.onChanged.removeListener(listener);
        reject(new Error('Download interrupted'));
      }
    };
    chrome.downloads.onChanged.addListener(listener);
  });
}

async function cmdNotify(params) {
  const id = await chrome.notifications.create({
    type: 'basic',
    iconUrl: params.iconUrl || 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".9em" font-size="90">✦</text></svg>',
    title: params.title || 'Tofu',
    message: params.message || '',
    priority: params.priority || 0,
  });
  return { notificationId: id };
}

// ══════════════════════════════════════════
//  Utility
// ══════════════════════════════════════════

function isProtectedUrl(url) {
  return /^(chrome|edge|chrome-extension|moz-extension|about|file|view-source|chrome-search|devtools):/i
    .test(String(url || '').trim());
}

function _monotonicNowMs() {
  if (typeof performance !== 'undefined' && performance
      && typeof performance.now === 'function') {
    return performance.now();
  }
  return Date.now();
}

function _urlForDiagnostic(value) {
  try {
    const parsed = new URL(String(value || ''));
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
      return `${parsed.protocol || 'unknown:'}[redacted]`;
    }
    const hasCapabilityParts = (
      parsed.username || parsed.password || parsed.pathname !== '/'
      || parsed.search || parsed.hash);
    return `${parsed.protocol}//${parsed.host}/${hasCapabilityParts ? '…' : ''}`;
  } catch (_) {
    return '[invalid-url]';
  }
}

function _textForDiagnostic(value) {
  return String(value && value.message || value || '')
    .replace(/https?:\/\/[^\s'"<>\\]+/gi, (url) => _urlForDiagnostic(url))
    .replace(/[\r\n\t]+/g, ' ')
    .slice(0, 240);
}

// Binary assets that Chrome downloads (instead of rendering) when a tab
// navigates to them, and which yield no scrapable text. Mirrors the
// server-side _BROWSER_UNRENDERABLE_EXTS list in lib/search_bridge.py.
// `.svg` is intentionally excluded (it renders as text/markup).
function isBinaryAssetUrl(url) {
  let path;
  try { path = new URL(url).pathname.toLowerCase().replace(/\/+$/, ''); }
  catch { return false; }
  return /\.(pdf|zip|tar|gz|tgz|rar|7z|bz2|xz|jpg|jpeg|png|gif|webp|bmp|ico|mp4|mp3|wav|avi|mov|webm|mkv|flac|ogg|docx?|xlsx?|pptx?|exe|dmg|iso|apk|bin|woff2?|ttf|otf|eot)$/.test(path);
}

function updateBadge(state) {
  const colors = { on: '#4CAF50', error: '#f44336', off: '#9E9E9E', repair: '#FF9800' };
  const texts = { on: 'ON', error: 'ERR', off: 'OFF', repair: 'KEY' };
  try {
    chrome.action.setBadgeBackgroundColor({ color: colors[state] || '#9E9E9E' });
    chrome.action.setBadgeText({ text: texts[state] || '' });
  } catch {}
}

// ══════════════════════════════════════════
//  Popup Communication
// ══════════════════════════════════════════

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === 'getStatus') {
    sendResponse({
      connected,
      serverUrl: SERVER_URL,
      clientId: CLIENT_ID,
      hasBridgeSecret: !!BRIDGE_SECRET,
      pollActive,
      lastError,
      authFailures,
      needsRepair,
      inflight: _inflight.size,
      resultQueue: _resultQueue.length,
      repairBusy: _repairInFlight,
      commandsExecuted,
      commandsFailed,
    });
    return true;
  }
  if (msg.type === 'setServer') {
    setServer(msg.url);
    sendResponse({ ok: true });
    return true;
  }
  if (msg.type === 'setBridgeSecret') {
    setBridgeSecret(msg.secret);
    // Trigger a poll attempt soon so the user sees the new auth state.
    sendResponse({ ok: true, hasBridgeSecret: !!BRIDGE_SECRET });
    return true;
  }
  if (msg.type === 'repairNow') {
    // The popup's one-click repair — a real user gesture, so the ladder may
    // open a FOREGROUND Tofu tab (a dead SSO session is re-signed-in there;
    // the mint then completes on the next run).
    attemptAutoRepair({ forceTab: true })
      .then((ok) => sendResponse({ ok }));
    return true;   // async sendResponse
  }
  if (msg.type === 'toggle') {
    if (pollActive) { stopPolling(); updateBadge('off'); }
    else { startPolling(); }
    sendResponse({ pollActive });
    return true;
  }
});

// Initialize
updateBadge('off');
init();
