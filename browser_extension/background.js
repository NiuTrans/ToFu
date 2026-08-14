/**
 * Tofu Browser Bridge — Background Service Worker (v5.0)
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
// Some commands can legitimately take longer than the default; override here.
const COMMAND_TIMEOUT_OVERRIDES = {
  screenshot_tab: 55000,  // full-page CDP capture + lazy-load wait
  wait_download: 65000,
};
const PROTOCOL_VERSION = 2;
const BROWSER_CAPABILITIES = [
  'tabs', 'navigate', 'read', 'snapshot', 'click', 'fill', 'press',
  'select', 'scroll', 'wait', 'execute', 'iframes', 'network_capture',
  'upload', 'downloads', 'screenshot',
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
let BRIDGE_SECRET = '';           // Optional: matches server TOFU_BRIDGE_SECRET
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
const _networkCaptures = new Map(); // captureId -> {tabId, patterns, responses}
let _networkListenerInstalled = false;

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
  _resetAuthBackoff();
  stopPolling();
  startPolling();
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
async function attemptAutoRepair(opts) {
  opts = opts || {};
  if (_repairInFlight || !SERVER_URL) return false;
  _repairInFlight = true;
  try {
    let tabs = [];
    try { tabs = await chrome.tabs.query({}); } catch (e) { /* tabs unavailable */ }
    const mine = tabs.filter((t) => t.id != null && t.url &&
                              t.url.startsWith(SERVER_URL));
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
    // Drain the result queue — send all completed results with this poll
    resultsToSend = _resultQueue.splice(0, _resultQueue.length);

    const controller = new AbortController();
    _activePollController = controller;
    _flushPending = false;
    timeoutId = setTimeout(() => controller.abort(), FETCH_TIMEOUT);

    const resp = await fetch(`${SERVER_URL}/api/browser/poll`, {
      method: 'POST',
      signal: controller.signal,
      headers: buildHeaders(),
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
    if (err.name === 'AbortError') {
      if (_flushPending) {
        // Deliberate abort: a command result just landed, so we cut the idle
        // long-poll short. Re-poll INSTANTLY (not the 100ms reconnect path) so
        // the result goes out now instead of waiting the server's 8s window.
        // Restore the in-flight poll's drained results so none are lost.
        _flushPending = false;
        if (resultsToSend.length) _resultQueue.unshift(...resultsToSend);
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
  const target = { tabId };
  let attached = false;
  let overridden = false;
  try {
    await chrome.debugger.attach(target, '1.3');
    attached = true;

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
    // ALWAYS clear the override before detaching, on every path (success,
    // capture error, or empty-shot throw) — leaving it set would corrupt the
    // user's real page layout. clearDeviceMetricsOverride needs the debugger
    // session, so it must run before detach.
    if (overridden) {
      try {
        await chrome.debugger.sendCommand(target, 'Emulation.clearDeviceMetricsOverride');
      } catch (errClear) {
        console.warn('[Screenshot] clearDeviceMetricsOverride failed:', errClear && errClear.message);
      }
    }
    if (attached) {
      try { await chrome.debugger.detach(target); } catch (_) {}
    }
  }
}

// Background viewport capture via CDP — captures the tab's current viewport
// WITHOUT activating/focusing it (unlike chrome.tabs.captureVisibleTab, which
// can only grab the foreground tab). captureBeyondViewport:false keeps it to
// the visible area, so it's fast and never triggers the tab-switch flicker.
async function _screenshotViewportCDP(tabId, format, quality) {
  const target = { tabId };
  let attached = false;
  try {
    await chrome.debugger.attach(target, '1.3');
    attached = true;
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
  } finally {
    if (attached) {
      try { await chrome.debugger.detach(target); } catch (_) {}
    }
  }
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

  const results = await chrome.scripting.executeScript({
    target: { tabId },
    func: _getInteractiveElements,
    args: [params.maxElements || 200, params.viewport || false],
  });

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

async function _cdpRun(tabId, fn) {
  const target = { tabId };
  let attached = false;
  try {
    await chrome.debugger.attach(target, '1.3');
    attached = true;
    return await fn(target);
  } finally {
    if (attached) {
      try { await chrome.debugger.detach(target); } catch (_) {}
    }
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
  const opts = { url: params.url || 'about:blank' };
  // Default to background (active: false) unless explicitly requested
  opts.active = params.active === true ? true : false;
  if (params.pinned !== undefined) opts.pinned = params.pinned;
  if (params.windowId) opts.windowId = params.windowId;

  const tab = await chrome.tabs.create(opts);
  if (params.waitForLoad) {
    await waitForTabLoad(
      tab.id, Math.max(1000, Math.min(30000, Number(params.timeoutMs) || 15000)));
  }
  const current = await chrome.tabs.get(tab.id);
  return { id: current.id, url: current.url, title: current.title,
           windowId: current.windowId, status: current.status };
}

async function cmdCloseTab(params) {
  const tabIds = Array.isArray(params.tabIds) ? params.tabIds : [params.tabId];
  await chrome.tabs.remove(tabIds);
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
  const timeoutMs = params.timeoutMs || 20000;

  if (isProtectedUrl(url)) {
    throw new Error(`Cannot fetch protected URL: ${url}`);
  }

  // Refuse binary assets by extension. Navigating a tab to a PDF/zip/media URL
  // makes Chrome's download manager save it to the user's machine (and yields
  // no scrapable text) — these are fetched/parsed server-side, never here.
  // (The server-side bridge already filters these, but a redirect could still
  // land us on one, so guard defensively.)
  if (isBinaryAssetUrl(url)) {
    throw new Error(`Refusing to open binary asset in a tab (would download): ${url}`);
  }

  // Create a background tab (not active, so it doesn't steal focus)
  let tab;
  try {
    tab = await chrome.tabs.create({ url, active: false });
  } catch (e) {
    throw new Error(`Failed to create tab for ${url}: ${e.message}`);
  }

  try {
    // Wait for the tab to fully load
    await waitForTabLoad(tab.id, timeoutMs);

    // Re-fetch tab info for final URL (after redirects)
    tab = await chrome.tabs.get(tab.id);

    // If it ended up on a protected page (e.g. login redirect), bail
    if (tab.url && isProtectedUrl(tab.url)) {
      throw new Error(`Redirected to protected page: ${tab.url}`);
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
      return r;
    }

    return { text: '', title: tab.title || '', url: tab.url || '', error: 'No content extracted' };
  } finally {
    // Always close the background tab, even on error
    try { await chrome.tabs.remove(tab.id); } catch (_) {}
  }
}

async function cmdNavigate(params) {
  const tabId = params.tabId;
  const url = params.url;
  if (!tabId) throw new Error('No tabId specified');
  if (!url) throw new Error('No url specified');

  await chrome.tabs.update(tabId, { url });

  if (params.waitForLoad) {
    await waitForTabLoad(tabId, 15000);
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
  return { downloadId };
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

function _networkPatternMatches(url, patterns) {
  if (!patterns || !patterns.length) return true;
  return patterns.some(p => {
    const escaped = String(p).replace(/[.+?^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*');
    try { return new RegExp(`^${escaped}$`).test(url); } catch (_) { return false; }
  });
}

function _onNetworkCompleted(details) {
  for (const capture of _networkCaptures.values()) {
    if (details.tabId !== capture.tabId || !_networkPatternMatches(details.url, capture.patterns)) continue;
    if (capture.responses.length >= 500) capture.responses.shift();
    capture.responses.push({
      url: details.url, status: details.statusCode, method: details.method,
      type: details.type, fromCache: !!details.fromCache,
      timeStamp: details.timeStamp,
    });
  }
}

async function cmdNetworkCaptureStart(params) {
  const tab = await chrome.tabs.get(Number(params.tabId));
  if (tab.url && isProtectedUrl(tab.url)) throw new Error(`Cannot capture protected page: ${tab.url}`);
  await _assertExpectedDomain(params, tab);
  const captureId = crypto.randomUUID();
  _networkCaptures.set(captureId, {
    tabId: Number(params.tabId), patterns: Array.isArray(params.urlPatterns) ? params.urlPatterns : [],
    responses: [], startedAt: Date.now(),
  });
  if (!_networkListenerInstalled) {
    chrome.webRequest.onCompleted.addListener(_onNetworkCompleted, {urls: ['<all_urls>']});
    _networkListenerInstalled = true;
  }
  return {captureId, page: await cmdPageState(params)};
}

async function cmdNetworkCaptureStop(params) {
  const capture = _networkCaptures.get(String(params.captureId));
  _networkCaptures.delete(String(params.captureId));
  if (!_networkCaptures.size && _networkListenerInstalled) {
    chrome.webRequest.onCompleted.removeListener(_onNetworkCompleted);
    _networkListenerInstalled = false;
  }
  return {captureId: String(params.captureId), responses: capture ? capture.responses : [],
          startedAt: capture ? capture.startedAt : null, stoppedAt: Date.now()};
}

async function cmdWaitDownload(params) {
  const downloadId = Number(params.downloadId);
  const timeoutMs = Math.max(1000, Math.min(120000, Number(params.timeoutMs) || 60000));
  const current = await chrome.downloads.search({id: downloadId});
  if (!current.length) throw new Error(`Download ${downloadId} not found`);
  if (current[0].state === 'complete') return current[0];
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
        resolve(rows[0] || {id: downloadId, state: 'complete'});
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
  return /^(chrome|edge|chrome-extension|moz-extension|about|file|view-source|chrome-search|devtools):/.test(url);
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
