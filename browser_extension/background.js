/**
 * ChatUI Browser Bridge — Background Service Worker (v4.1)
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

// ══════════════════════════════════════════
//  State
// ══════════════════════════════════════════

let SERVER_URL = '';
let CLIENT_ID = '';               // Stable per-device client identifier
let pollActive = false;
let connected = false;
let lastError = '';

// Result queue: completed results waiting to be sent with next poll
const _resultQueue = [];        // [{id, result, error}, ...]
const _inflight = new Set();    // Command IDs currently executing

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

function init() {
  // Generate or restore a stable client ID for per-device command routing
  chrome.storage.local.get(['clientId'], (data) => {
    if (data.clientId) {
      CLIENT_ID = data.clientId;
    } else {
      CLIENT_ID = crypto.randomUUID();
      chrome.storage.local.set({ clientId: CLIENT_ID });
    }
    console.log('[Bridge] Client ID:', CLIENT_ID);
    autoDetectServer();
  });
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
    // Scan open tabs for a ChatUI page
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
  stopPolling();
  startPolling();
}

// ══════════════════════════════════════════
//  Polling — Single Endpoint
// ══════════════════════════════════════════

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
  console.log('[Bridge] Polling stopped');
}

async function poll() {
  if (!pollActive || !SERVER_URL) return;

  try {
    // Drain the result queue — send all completed results with this poll
    const resultsToSend = _resultQueue.splice(0, _resultQueue.length);

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), FETCH_TIMEOUT);

    const resp = await fetch(`${SERVER_URL}/api/browser/poll`, {
      method: 'POST',
      signal: controller.signal,
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify({ results: resultsToSend, clientId: CLIENT_ID }),
    });
    clearTimeout(timeoutId);

    if (!resp.ok) {
      if (resp.status >= 500) {
        // Proxy error — put results back so they're not lost
        _resultQueue.unshift(...resultsToSend);
        console.warn(`[Bridge] Server/proxy returned ${resp.status}, retrying...`);
        connected = true;
        if (pollActive) setTimeout(poll, POLL_RETRY_DELAY);
        return;
      }
      throw new Error(`HTTP ${resp.status}`);
    }

    const data = await resp.json();
    connected = true;
    lastError = '';
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

    if (pollActive) setTimeout(poll, POLL_INTERVAL);

  } catch (err) {
    if (err.name === 'AbortError') {
      // Fetch timeout — normal (server long-poll returned nothing), just reconnect
      connected = true;
      if (pollActive) setTimeout(poll, POLL_INTERVAL);
      return;
    }

    connected = false;
    lastError = err.message || 'Connection failed';
    updateBadge('error');
    console.warn(`[Bridge] Poll error: ${lastError}`);
    if (pollActive) setTimeout(poll, POLL_RETRY_DELAY);
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

    result = await withTimeout(
      executeCommand(cmd.type, cmd.params || {}),
      COMMAND_TIMEOUT,
      `Command '${cmd.type}' timed out after ${COMMAND_TIMEOUT / 1000}s`
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

  // Nudge the poll loop: if we have results and no active poll is running,
  // the next poll() will pick them up automatically via setTimeout.
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

  // Wait for tab to finish loading
  if (tab.status !== 'complete') {
    await waitForTabLoad(tabId, 10000);
  }

  const results = await chrome.scripting.executeScript({
    target: { tabId },
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

  let text = document.body ? (document.body.innerText || document.body.textContent || '') : '';
  const textLength = text.length;
  let truncated = false;
  if (text.length > maxChars) {
    text = text.substring(0, maxChars);
    truncated = true;
  }

  // Return full page HTML so the server can run trafilatura/BS4 extraction
  // (same pipeline as fetch_page_content). Cap at 2MB to avoid message bloat.
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

  return { text, textLength, truncated, html, htmlTruncated, meta };
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
        target: { tabId },
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

async function cmdScreenshotTab(params) {
  const format = params.format || 'png';
  const quality = params.quality || 80;

  // Remember which tab was active so we can switch back
  let originalTabId = null;
  let targetWindowId = null;

  if (params.tabId) {
    // Find the currently active tab in the target tab's window
    const targetTab = await chrome.tabs.get(params.tabId);
    targetWindowId = targetTab.windowId;

    const [activeTab] = await chrome.tabs.query({ active: true, windowId: targetWindowId });
    if (activeTab) originalTabId = activeTab.id;

    // Activate the target tab (required by captureVisibleTab)
    await chrome.tabs.update(params.tabId, { active: true });
    await new Promise(r => setTimeout(r, 500));  // Wait for render
  }

  const opts = { format };
  if (format === 'jpeg') opts.quality = quality;

  try {
    const dataUrl = await chrome.tabs.captureVisibleTab(targetWindowId, opts);

    // Switch back to the original tab silently
    if (originalTabId && originalTabId !== params.tabId) {
      await chrome.tabs.update(originalTabId, { active: true });
    }

    return { dataUrl, format };
  } catch (err) {
    // Switch back even on error
    if (originalTabId && originalTabId !== params.tabId) {
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
  const seen = new Set();
  let index = 1;  // 1-based index

  for (const el of allEls) {
    if (elements.length >= maxElements) break;
    // Skip hidden elements
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
    if (el.offsetWidth === 0 && el.offsetHeight === 0) continue;

    // Viewport filter
    if (viewportOnly) {
      const rect = el.getBoundingClientRect();
      if (rect.bottom < 0 || rect.top > window.innerHeight ||
          rect.right < 0 || rect.left > window.innerWidth) continue;
    }

    // Build a concise CSS selector for this element
    let selector = '';
    if (el.id) {
      selector = `#${CSS.escape(el.id)}`;
    } else {
      const tag = el.tagName.toLowerCase();
      const classes = Array.from(el.classList).slice(0, 3).map(c => `.${CSS.escape(c)}`).join('');
      const nthType = (() => {
        if (el.id) return '';
        const siblings = el.parentElement ? Array.from(el.parentElement.children).filter(s => s.tagName === el.tagName) : [];
        if (siblings.length <= 1) return '';
        const idx = siblings.indexOf(el) + 1;
        return `:nth-of-type(${idx})`;
      })();
      selector = tag + classes + nthType;
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
    }

    // Dedup
    if (seen.has(selector)) continue;
    seen.add(selector);

    // Gather useful info — ★ include index
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
  }

  // Canvas detection
  const canvases = document.querySelectorAll('canvas');
  const svgs = document.querySelectorAll('svg');
  const canvasDetected = canvases.length > 0 && elements.length < 10;

  // ★ Return selectorMap for server-side caching
  const result = { elements, total: allEls.length, selectorMap };

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

  const results = await chrome.scripting.executeScript({
    target: { tabId },
    world: 'MAIN',
    func: _clickElement,
    args: [params.selector, params.rightClick || false, params.scrollTo !== false],
  });

  if (results && results[0] && results[0].result) {
    return results[0].result;
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

  const results = await chrome.scripting.executeScript({
    target: { tabId },
    world: 'MAIN',
    func: _hoverElement,
    args: [params.selector],
  });

  if (results && results[0] && results[0].result) {
    return results[0].result;
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

  const results = await chrome.scripting.executeScript({
    target: { tabId },
    world: 'MAIN',
    func: _keyboardInput,
    args: [params.keys, params.selector || null],
  });

  if (results && results[0] && results[0].result) {
    return results[0].result;
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

  const timeout = params.timeout || 5000; // Default 5s
  const interval = params.interval || 100; // Poll every 100ms

  const startTime = Date.now();

  while (Date.now() - startTime < timeout) {
    try {
      const results = await chrome.scripting.executeScript({
        target: { tabId },
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
    target: { tabId },
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

  const results = await chrome.scripting.executeScript({
    target: { tabId },
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
  return { id: tab.id, url: tab.url, title: tab.title, windowId: tab.windowId };
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

async function cmdNotify(params) {
  const id = await chrome.notifications.create({
    type: 'basic',
    iconUrl: params.iconUrl || 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".9em" font-size="90">✦</text></svg>',
    title: params.title || 'ChatUI',
    message: params.message || '',
    priority: params.priority || 0,
  });
  return { notificationId: id };
}

// ══════════════════════════════════════════
//  Utility
// ══════════════════════════════════════════

function isProtectedUrl(url) {
  return /^(chrome|chrome-extension|about|chrome-search|devtools):/.test(url);
}

function updateBadge(state) {
  const colors = { on: '#4CAF50', error: '#f44336', off: '#9E9E9E' };
  const texts = { on: 'ON', error: 'ERR', off: 'OFF' };
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
      pollActive,
      lastError,
      inflight: _inflight.size,
      resultQueue: _resultQueue.length,
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
