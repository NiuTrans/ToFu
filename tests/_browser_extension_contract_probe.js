/* Behavioral probes for the real MV3 service-worker runtime.

   Python tests execute this file with ``node`` and pass background.js as an
   argument.  The shipped worker is evaluated in a mocked Chrome context; no
   test copies function bodies or asserts source-text needles. */

'use strict';

const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');

const mode = process.argv[2];
const backgroundPath = process.argv[3];

function listeners() {
  const handlers = [];
  return {
    addListener(fn) { handlers.push(fn); },
    removeListener(fn) {
      const index = handlers.indexOf(fn);
      if (index >= 0) handlers.splice(index, 1);
    },
    emit(...args) {
      for (const handler of [...handlers]) handler(...args);
    },
  };
}

const debuggerEvents = listeners();
const tabRemovedEvents = listeners();
const operations = [];
const createdTabOptions = [];
const runtimeCalls = {reload: 0};
const debuggerCommands = [];
const sessionData = mode === 'temporaryTabRecovery'
  ? {temporarySearchTabIds: [19, 23, 29, 31, 37, 41, 43, 47]} : {};
const removedTabIds = [];
let currentUrl = 'about:blank';
let nextTabId = 7;

const chrome = {
  debugger: {
    onEvent: debuggerEvents,
    onDetach: listeners(),
    attach: async () => { operations.push('debugger.attach'); },
    detach: async () => { operations.push('debugger.detach'); },
    sendCommand: async (_target, method) => {
      debuggerCommands.push(method);
      operations.push(`debugger.${method}`);
      if (method === 'Network.getResponseBody') {
        return {body: JSON.stringify({data: [{id: 'captured'}]}),
                base64Encoded: false};
      }
      return {};
    },
  },
  webNavigation: {
    onCommitted: listeners(),
    onHistoryStateUpdated: listeners(),
    onReferenceFragmentUpdated: listeners(),
  },
  tabs: {
    onRemoved: tabRemovedEvents,
    onUpdated: listeners(),
    create: async (options) => {
      operations.push(`tabs.create:${options.url}`);
      createdTabOptions.push({url: options.url, active: options.active});
      currentUrl = options.url;
      return {id: nextTabId, url: currentUrl, title: 'Probe', status: 'complete'};
    },
    update: async (id, options) => {
      operations.push(`tabs.update:${options.url || ''}`);
      if (options.url) currentUrl = options.url;
      return {id, url: currentUrl, title: 'Probe', status: 'complete'};
    },
    get: async (id) => ({id, url: currentUrl, title: 'Probe', status: 'complete'}),
    remove: async (id) => { operations.push('tabs.remove'); removedTabIds.push(id); },
    query: async () => [],
  },
  scripting: {
    executeScript: async (details) => {
      const name = details.func && details.func.name;
      if (name === '_researchPageSignals') {
        return [{result: {
          text: 'page one\nrecord one', fingerprint: 'page-one',
          framework: 'react', initialState: {}, initialStatePayloads: {},
        }}];
      }
      if (name === '_researchScrollStep') {
        return [{result: {scrolled: false, atBottom: true}}];
      }
      if (name === '_researchAdvancePagination') {
        return [{result: {advanced: false, reason: 'no-safe-next-control'}}];
      }
      return [{result: {
        text: 'rendered page', textLength: 13, truncated: false,
      }}];
    },
  },
  cookies: {getAll: async () => [{name: 'session'}]},
  runtime: {
    getManifest: () => ({version: 'test'}),
    getURL: (path) => path,
    reload: () => { runtimeCalls.reload += 1; },
    onInstalled: listeners(),
    onStartup: listeners(),
    onMessage: listeners(),
  },
  alarms: {create() {}, onAlarm: listeners()},
  storage: {
    session: {
      async get(key) { return {[key]: sessionData[key]}; },
      async set(values) { Object.assign(sessionData, values); },
      async remove(key) { delete sessionData[key]; },
    },
    local: {
      get(_keys, callback) {
        if (callback) callback({});
        return Promise.resolve({});
      },
      set() { return Promise.resolve(); },
    },
  },
  action: {setBadgeBackgroundColor() {}, setBadgeText() {}},
  webRequest: {onCompleted: listeners()},
};

const context = vm.createContext({
  chrome,
  console: {log() {}, warn() {}, error() {}},
  crypto: webcrypto,
  navigator: {userAgent: 'Chrome/140'},
  URL,
  AbortController,
  TextEncoder,
  TextDecoder,
  Uint8Array,
  setTimeout,
  clearTimeout,
  fetch: async () => ({
    ok: true,
    status: 200,
    url: 'https://example.test/app',
    headers: {get(name) {
      return String(name).toLowerCase() === 'content-type'
        ? 'text/html; charset=utf-8' : null;
    }},
    body: {cancel: async () => {}},
  }),
  atob: (value) => Buffer.from(value, 'base64').toString('binary'),
});

vm.runInContext(fs.readFileSync(backgroundPath, 'utf8'), context, {
  filename: backgroundPath,
});

async function emitCapturedTraffic(tabId) {
  debuggerEvents.emit(
    {tabId}, 'Network.requestWillBeSent',
    {requestId: 'request-1', request: {method: 'GET'}});
  debuggerEvents.emit(
    {tabId}, 'Network.responseReceived',
    {requestId: 'request-1', type: 'XHR', response: {
      url: 'https://example.test/api/data', status: 200,
      mimeType: 'application/json',
    }});
  debuggerEvents.emit(
    {tabId}, 'Network.loadingFinished',
    {requestId: 'request-1', encodedDataLength: 32});
  debuggerEvents.emit(
    {tabId}, 'Network.webSocketCreated',
    {requestId: 'socket-1', url: 'wss://example.test/events'});
  debuggerEvents.emit(
    {tabId}, 'Network.webSocketFrameReceived',
    {requestId: 'socket-1', response: {
      opcode: 1, payloadData: JSON.stringify({event: 'ready'}),
    }});
  await new Promise((resolve) => setTimeout(resolve, 0));
}

async function fetchProbe() {
  let publicBlankRejected = false;
  try {
    await vm.runInContext(
      'cmdNetworkCaptureStart({tabId: 7, captureBodies: true})', context);
  } catch (error) {
    publicBlankRejected = String(error && error.message).includes(
      'Cannot capture protected page');
  }
  context.__emitCapturedTraffic = emitCapturedTraffic;
  vm.runInContext(
    'waitForTabLoad = async (tabId) => { await __emitCapturedTraffic(tabId); };\n' +
    '_waitForCapturedPageSettle = async () => { await Promise.resolve(); };',
    context,
  );
  const result = await vm.runInContext(
    "cmdFetchUrl({url: 'https://example.test/app', timeoutMs: 20000})",
    context,
  );
  const limits = vm.runInContext(`({
    entries: NETWORK_CAPTURE_MAX_ENTRIES,
    trackedRequests: NETWORK_CAPTURE_MAX_TRACKED_REQUESTS,
    totalBodyChars: NETWORK_CAPTURE_MAX_TOTAL_BODY_CHARS,
    active: 0,
  })`, context);
  return {result, limits, operations, debuggerCommands, publicBlankRejected};
}

async function protocolProbe() {
  const scheduled = [];
  const cleared = [];
  let requestHeaders = null;
  let timerId = 0;
  context.setTimeout = (_callback, delay) => {
    timerId += 1;
    scheduled.push({id: timerId, delay});
    return timerId;
  };
  context.clearTimeout = (id) => { cleared.push(id); };
  context.fetch = async (_url, options = {}) => {
    requestHeaders = options.headers || null;
    return {
      ok: false,
      status: 426,
      json: async () => ({requiredProtocolVersion: 3}),
    };
  };
  vm.runInContext(`
    SERVER_URL = 'https://tofu.test';
    CLIENT_ID = 'probe-client';
    BRIDGE_SECRET = 'probe-secret';
    pollActive = true;
    _resultQueue.push({id: 'completed-result', result: {ok: true}});
  `, context);
  await vm.runInContext('poll()', context);
  const state = vm.runInContext(`({
    queue: _resultQueue.map((item) => item.id),
    connected,
    lastError,
    upgradeDelay: UPGRADE_RETRY_DELAY,
    ordinaryDelay: POLL_RETRY_DELAY,
  })`, context);
  return {
    state,
    scheduled,
    cleared,
    reportedProtocolHeader: requestHeaders && requestHeaders['X-Browser-Protocol-Version'],
  };
}

async function rateLimitProbe() {
  const scheduled = [];
  let timerId = 0;
  context.setTimeout = (_callback, delay) => {
    timerId += 1;
    scheduled.push({id: timerId, delay});
    return timerId;
  };
  context.clearTimeout = () => {};
  context.fetch = async () => ({
    ok: false,
    status: 429,
    headers: {get: (name) => String(name).toLowerCase() === 'retry-after' ? '7' : null},
    json: async () => ({retryAfter: 2}),
  });
  vm.runInContext(`
    SERVER_URL = 'https://tofu.test';
    CLIENT_ID = 'probe-client';
    BRIDGE_SECRET = 'probe-secret';
    pollActive = true;
    _resultQueue.push({id: 'completed-result', result: {ok: true}});
  `, context);
  await vm.runInContext('poll()', context);
  const state = vm.runInContext(`({
    queue: _resultQueue.map((item) => item.id),
    connected,
    lastError,
  })`, context);
  return {state, scheduled};
}

function resultBatchProbe() {
  const small = vm.runInContext(`(() => {
    _resultQueue.splice(0, _resultQueue.length);
    for (let index = 0; index < 40; index += 1) {
      _resultQueue.push({id: 'small-' + index, result: {ok: true}});
    }
    const batch = _takeBoundedResultBatch();
    return {
      batchIds: batch.map((item) => item.id),
      remainingIds: _resultQueue.map((item) => item.id),
    };
  })()`, context);
  const oversize = vm.runInContext(`(() => {
    _resultQueue.splice(0, _resultQueue.length);
    _resultQueue.push({id: 'oversize', result: {data: 'x'.repeat(13 * 1024 * 1024)}});
    const batch = _takeBoundedResultBatch();
    return {id: batch[0].id, result: batch[0].result, error: batch[0].error};
  })()`, context);
  return {small, oversize};
}

async function payloadLimitProbe() {
  const scheduled = [];
  let timerId = 0;
  context.setTimeout = (_callback, delay) => {
    timerId += 1;
    scheduled.push({id: timerId, delay});
    return timerId;
  };
  context.clearTimeout = () => {};
  context.fetch = async () => ({ok: false, status: 413});
  vm.runInContext(`
    SERVER_URL = 'https://tofu.test';
    CLIENT_ID = 'probe-client';
    BRIDGE_SECRET = 'probe-secret';
    pollActive = true;
    _resultQueue.splice(0, _resultQueue.length);
    for (let index = 0; index < 4; index += 1) {
      _resultQueue.push({id: 'result-' + index, result: {ok: true}});
    }
  `, context);
  await vm.runInContext('poll()', context);
  const preserved = vm.runInContext(
    '_resultQueue.map((item) => item.id)', context);
  const nextBatch = vm.runInContext(
    '_takeBoundedResultBatch().map((item) => item.id)', context);
  return {
    preserved,
    nextBatch,
    batchLimit: vm.runInContext('_pollResultBatchMax', context),
    scheduled,
  };
}

async function transportFailureProbe() {
  const scheduled = [];
  let timerId = 0;
  context.setTimeout = (_callback, delay) => {
    timerId += 1;
    scheduled.push({id: timerId, delay});
    return timerId;
  };
  context.clearTimeout = () => {};
  context.fetch = async () => { throw new Error('proxy reset'); };
  vm.runInContext(`
    SERVER_URL = 'https://tofu.test';
    CLIENT_ID = 'probe-client';
    BRIDGE_SECRET = 'probe-secret';
    pollActive = true;
    _resultQueue.splice(0, _resultQueue.length);
    _resultQueue.push({id: 'completed-result', result: {ok: true}});
  `, context);
  await vm.runInContext('poll()', context);
  return {
    queue: vm.runInContext(
      '_resultQueue.map((item) => item.id)', context),
    scheduled,
  };
}

async function researchProbe() {
  context.__emitCapturedTraffic = emitCapturedTraffic;
  vm.runInContext(
    'waitForTabLoad = async (tabId) => { await __emitCapturedTraffic(tabId); };\n' +
    '_waitForCapturedPageSettle = async () => { await Promise.resolve(); };',
    context,
  );
  const result = await vm.runInContext(`cmdResearchUrl({
    url: 'https://example.test/list', maxChars: 60000,
    maxScrolls: 999, maxPages: 999, pagination: 'auto', timeoutMs: 65000,
    captureHints: [{method: 'GET', origin: 'https://example.test',
      pathTemplate: '/api/data'}],
  })`, context);
  const limits = vm.runInContext(`({
    websocketFrames: NETWORK_CAPTURE_MAX_WEBSOCKET_FRAMES,
    activeCaptures: _networkCaptures.size,
    hintReserveChars: NETWORK_CAPTURE_HINT_RESERVE_CHARS,
    capabilities: [...BROWSER_CAPABILITIES],
  })`, context);
  return {result, limits, operations, debuggerCommands};
}

async function researchHintBudgetProbe() {
  const normalizeHints = vm.runInContext('_normalizedResearchCaptureHints', context);
  const captureBody = vm.runInContext('_captureResponseBody', context);
  const maximum = vm.runInContext('NETWORK_CAPTURE_MAX_TOTAL_BODY_CHARS', context);
  const reserve = vm.runInContext('NETWORK_CAPTURE_HINT_RESERVE_CHARS', context);
  const capture = {
    target: {tabId: 7}, totalBodyChars: 0, droppedBodies: 0,
    priorityHints: normalizeHints([{method: 'GET', origin: 'https://example.test',
      pathTemplate: '/api/{segment}'}]),
    priorityReserveChars: reserve, priorityBodyMatches: 0,
    lastActivityAt: 0,
  };
  const sizes = {normal1: 300000, normal2: 300000, normal3: 300000, priority: 200000};
  const originalSendCommand = chrome.debugger.sendCommand;
  chrome.debugger.sendCommand = async (_target, method, params) => {
    if (method === 'Network.getResponseBody') {
      return {body: 'x'.repeat(sizes[params.requestId]), base64Encoded: false};
    }
    return {};
  };
  const rows = [
    {requestId: 'normal1', method: 'GET', url: 'https://example.test/noise/one'},
    {requestId: 'normal2', method: 'GET', url: 'https://example.test/noise/two'},
    {requestId: 'normal3', method: 'GET', url: 'https://example.test/noise/three'},
    {requestId: 'priority', method: 'GET', url: 'https://example.test/api/data'},
  ];
  try {
    for (const row of rows) await captureBody(capture, row, sizes[row.requestId]);
  } finally {
    chrome.debugger.sendCommand = originalSendCommand;
  }
  return {
    maximum, reserve, totalBodyChars: capture.totalBodyChars,
    priorityBodyMatches: capture.priorityBodyMatches,
    normalBeforePriorityChars: rows.slice(0, 3)
      .reduce((total, row) => total + String(row.responsePreview || '').length, 0),
    priorityChars: String(rows[3].responsePreview || '').length,
  };
}

async function fileTransferProbe() {
  const targetUrl = 'https://files.example.test/download?signature=once';
  const targetChunks = [
    new Uint8Array(5000).fill(1),
    new Uint8Array(7000).fill(2),
    new Uint8Array(9504).fill(3),
  ];
  const uploadedChunks = [];
  const controls = [];
  let targetFetches = 0;

  function targetResponse() {
    let index = 0;
    return {
      ok: true,
      status: 200,
      url: targetUrl,
      headers: {get(name) {
        const key = String(name).toLowerCase();
        if (key === 'content-type') return 'application/zip';
        if (key === 'content-disposition') {
          return 'attachment; filename="once.zip"';
        }
        return null;
      }},
      body: {
        getReader() {
          return {
            async read() {
              if (index >= targetChunks.length) return {done: true};
              return {done: false, value: targetChunks[index++]};
            },
            async cancel() {},
          };
        },
        async cancel() {},
      },
    };
  }

  context.fetch = async (url, options = {}) => {
    if (url === targetUrl) {
      targetFetches += 1;
      return targetResponse();
    }
    const value = String(url);
    if (value.endsWith('/start')) {
      controls.push({kind: 'start', body: JSON.parse(options.body)});
      return {ok: true, status: 200, json: async () => ({ok: true})};
    }
    if (value.includes('/chunks/')) {
      uploadedChunks.push({
        sequence: Number(value.split('/').pop()),
        bytes: Array.from(options.body),
      });
      return {ok: true, status: 200, json: async () => ({ok: true})};
    }
    if (value.endsWith('/complete')) {
      const body = JSON.parse(options.body);
      controls.push({kind: 'complete', body});
      return {ok: true, status: 200, json: async () => ({
        ok: true,
        transferId: 'transfer-once',
        location: 'server_staging',
        sizeBytes: body.totalBytes,
        sha256: 'server-authored',
      })};
    }
    throw new Error(`unexpected probe request: ${url}`);
  };
  vm.runInContext(`
    SERVER_URL = 'https://tofu.test';
    CLIENT_ID = 'probe-client';
    BRIDGE_SECRET = 'probe-secret';
  `, context);
  const result = await vm.runInContext(`cmdFetchUrl({
    url: '${targetUrl}', timeoutMs: 20000,
    fileTransfer: {
      transferId: 'transfer-once', transferToken: 'one-time-token',
      maxBytes: 65536, chunkBytes: 16384, timeoutMs: 30000,
    },
  })`, context);
  return {
    result,
    targetFetches,
    chunkLengths: uploadedChunks.map((row) => row.bytes.length),
    chunkSequences: uploadedChunks.map((row) => row.sequence),
    controls,
    operations,
  };
}

function diagnosticUrlProbe() {
  context.__signedUrl = (
    'https://alice:password@files.example.test/private/path-token.zip'
    + '?X-Amz-Signature=signature#fragment');
  return {
    projected: vm.runInContext('_urlForDiagnostic(__signedUrl)', context),
    embedded: vm.runInContext(
      "_textForDiagnostic('failed at ' + __signedUrl + '\\nretry')", context),
    uppercaseProtected: vm.runInContext(
      "isProtectedUrl('  CHROME://settings  ')", context),
    malformed: vm.runInContext("_urlForDiagnostic('not a url')", context),
  };
}

async function fileTransferCleanupProbe() {
  const targetUrl = 'https://files.example.test/failing.bin';
  const scheduled = [];
  const cleared = [];
  let cleanupAborted = false;
  let deletes = 0;
  let nextTimer = 0;
  context.setTimeout = (callback, delay) => {
    nextTimer += 1;
    scheduled.push(delay);
    if (delay === 5000) Promise.resolve().then(callback);
    return nextTimer;
  };
  context.clearTimeout = (id) => { cleared.push(id); };
  context.fetch = async (url, options = {}) => {
    const value = String(url);
    if (value === targetUrl) {
      return {
        ok: true, status: 200, url: targetUrl,
        headers: {get() { return null; }},
        body: {getReader() { return {
          async read() { throw new Error('probe stream failure'); },
          async cancel() {},
        }; }},
      };
    }
    if (value.endsWith('/start')) {
      return {ok: true, status: 200, json: async () => ({ok: true})};
    }
    if (options.method === 'DELETE') {
      deletes += 1;
      return new Promise((_resolve, reject) => {
        options.signal.addEventListener('abort', () => {
          cleanupAborted = true;
          reject(new Error('cleanup aborted'));
        }, {once: true});
      });
    }
    throw new Error(`unexpected cleanup probe request: ${url}`);
  };
  vm.runInContext(`
    SERVER_URL = 'https://tofu.test';
    CLIENT_ID = 'probe-client';
    BRIDGE_SECRET = 'probe-secret';
  `, context);
  let error = '';
  try {
    await vm.runInContext(`cmdFetchFileToServer({
      url: '${targetUrl}', transferId: 'transfer-cleanup',
      transferToken: 'one-time-token', maxBytes: 65536,
      chunkBytes: 16384, timeoutMs: 30000,
    })`, context);
  } catch (caught) {
    error = String(caught && caught.message || caught);
  }
  return {scheduled, cleared, deletes, cleanupAborted, error};
}

async function fileTransferDeadlineProbe() {
  const targetUrl = 'https://files.example.test/slow-first-byte';
  const scheduled = [];
  const cleared = [];
  let nextTimer = 0;
  const clock = [1000, 7000];
  context.Date = {now: () => clock.length ? clock.shift() : 7000};
  context.setTimeout = (_callback, delay) => {
    nextTimer += 1;
    scheduled.push(delay);
    return nextTimer;
  };
  context.clearTimeout = (id) => { cleared.push(id); };
  let read = false;
  const response = {
    ok: true, status: 200, url: targetUrl,
    headers: {get(name) {
      return String(name).toLowerCase() === 'content-type'
        ? 'application/octet-stream' : null;
    }},
    body: {
      getReader() { return {
        async read() {
          if (read) return {done: true};
          read = true;
          return {done: false, value: new Uint8Array([1])};
        },
        async cancel() {},
      }; },
      async cancel() {},
    },
  };
  context.fetch = async (url, options = {}) => {
    const value = String(url);
    if (value === targetUrl) return response;
    if (value.endsWith('/start') || value.includes('/chunks/')) {
      return {ok: true, status: 200, json: async () => ({ok: true})};
    }
    if (value.endsWith('/complete')) {
      return {ok: true, status: 200, json: async () => ({
        ok: true, transferId: 'transfer-deadline',
        location: 'server_staging', sizeBytes: 1, sha256: 'server-authored',
      })};
    }
    throw new Error(`unexpected deadline probe request: ${url}`);
  };
  vm.runInContext(`
    SERVER_URL = 'https://tofu.test';
    CLIENT_ID = 'probe-client';
    BRIDGE_SECRET = 'probe-secret';
  `, context);
  const result = await vm.runInContext(`
    _refuseFileResponseBeforeNavigation(
      '${targetUrl}', 20000,
      {transferId: 'transfer-deadline', transferToken: 'one-time-token',
       maxBytes: 65536, chunkBytes: 16384, timeoutMs: 30000})
  `, context);
  return {scheduled, cleared, result};
}

function installDom(html) {
  const {JSDOM} = require('jsdom');
  const dom = new JSDOM(html, {url: 'https://example.com/list?page=1'});
  context.document = dom.window.document;
  context.location = dom.window.location;
  context.window = dom.window;
  context.getComputedStyle = () => ({display: 'block', visibility: 'visible'});
  // The extension's interaction paths dispatch real DOM events; surface the
  // constructors as context globals (jsdom owns them, the bare vm does not).
  for (const k of ['MouseEvent', 'KeyboardEvent', 'Event', 'CustomEvent',
                   'HTMLInputElement', 'HTMLTextAreaElement', 'HTMLSelectElement',
                   'Node', 'Element', 'HTMLElement', 'CSS']) {
    if (dom.window[k] !== undefined && context[k] === undefined) context[k] = dom.window[k];
  }
  for (const element of context.document.querySelectorAll('*')) {
    // Real component-library radios hide their <input> (display:none) and
    // render a styled wrapper — model that here so the collector sees the
    // same hidden-input signal a real browser reports.
    const hiddenInput = element.tagName === 'INPUT' &&
      (element.type === 'radio' || element.type === 'checkbox');
    const i = Math.min(20, Array.prototype.indexOf.call(
      element.parentElement ? element.parentElement.children : [element], element) + 1);
    element.getBoundingClientRect = () => (hiddenInput
      ? {width: 0, height: 0, left: 0, top: 0, right: 0, bottom: 0}
      : {width: 80, height: 24, left: i * 4, top: i * 30, right: i * 4 + 80, bottom: i * 30 + 24});
    element.scrollIntoView = () => {};
  }
  return dom;
}

function customControlsProbe() {
  // MTD/Element-UI render radios & selects with a HIDDEN input + styled
  // wrapper. The collector must surface the visible wrapper + open options.
  installDom(`
    <div class="mtd-modal-content-wrapper">
      <label class="mtd-radio">
        <span class="mtd-radio__input"><input type="radio" name="dfs" value="aipnlp"></span>
        <span class="mtd-radio__label">dolphinfs_aipnlp</span>
      </label>
      <label class="mtd-radio is-checked">
        <span class="mtd-radio__input"><input type="radio" name="dfs" value="your-username" checked></span>
        <span class="mtd-radio__label">dolphinfs_your-username</span>
      </label>
      <div class="mtd-select">
        <div class="mtd-select__selection"><input placeholder="请选择队列"></div>
      </div>
      <ul class="mtd-select-dropdown">
        <li class="mtd-select-dropdown__item">autoresearch_agent</li>
        <li class="mtd-select-dropdown__item">cpu_job</li>
      </ul>
    </div>`);
  // jsdom's selector engine (nwsapi) rejects the Vue-attribute tokens in
  // the collector's base selector list. Neutralize just those tokens in
  // the probe context — a test-harness shim, not a source change.
  const out = vm.runInContext(`(() => {
    const _qsa = document.querySelectorAll.bind(document);
    document.querySelectorAll = (sel) => _qsa(
      sel.split(',').filter((t) => !/[\\[v@]/.test(t)).join(',') || 'input');
    if (typeof CSS === 'undefined') {
      globalThis.CSS = {escape: (v) => String(v).replace(/[^a-zA-Z0-9_-]/g, (c) => String.fromCharCode(92) + c)};
    }
    try { return JSON.stringify(_getInteractiveElements(300, false)); }
    finally { document.querySelectorAll = _qsa; }
  })()`, context);
  const parsed = JSON.parse(out);
  const els = parsed.elements;
  const byRole = {};
  for (const e of els) byRole[e.role] = (byRole[e.role] || 0) + 1;
  const texts = els.map((e) => e.text).join(' | ');
  return {
    total: els.length,
    byRole,
    radioTexts: els.filter((e) => e.role === 'radio').map((e) => ({
      text: e.text, checked: e.checked, target: e.targetSelector,
    })),
    hasCombobox: !!byRole.combobox,
    optionTexts: els.filter((e) => e.role === 'option').map((e) => e.text),
    sweep: parsed.customControlSweep || null,
    joined: texts,
  };
}

// v5.7.0 interaction layer: drive the real MAIN-world functions the bridge
// commands inject (_setChecked / _selectOptionAsync / _actionablePoint) and
// pin their behaviour + TARGET_* taxonomy on MTD-like hidden-input markup.
async function isolatedInteractionProbe() {
  currentUrl = "https://example.com/list";
  const dom = installDom('<label id="box"><input type="checkbox"></label><select id="choice"><option value="one">One</option></select><textarea id="notes"></textarea><input id="name">');
  context.document.elementFromPoint = () => context.document.querySelector('#box');
  const page = vm.createContext({document: context.document, window: dom.window,
    CSS: {escape: String}, Event: dom.window.Event, MouseEvent: dom.window.MouseEvent,
    KeyboardEvent: dom.window.KeyboardEvent,
    setTimeout, clearTimeout});
  chrome.scripting.executeScript = async (details) => {
    page.__args = details.args;
    const result = await vm.runInContext('(' + details.func.toString() + ')(...__args)', page);
    return [{result}];
  };
  const checked = await vm.runInContext("cmdSetChecked({tabId:7, selector:'#box', checked:true})", context);
  const located = await vm.runInContext("_cdpLocate(7, '#box', false, '#box input')", context);
  const missing = await vm.runInContext("cmdSetChecked({tabId:7, selector:'#missing', checked:true})", context);
  const selected = await vm.runInContext("cmdSelectOption({tabId:7, selector:'#choice', option:'One'})", context);
  // The synthetic fallback is also serialized independently of the worker.
  const clicked = await vm.runInContext("cmdClickElement({tabId:7, selector:'#missing'})", context);
  const textarea = await vm.runInContext("cmdTypeText({tabId:7, selector:'#notes', text:'New notes'})", context);
  const input = await vm.runInContext("cmdTypeText({tabId:7, selector:'#name', text:'New name'})", context);
  return {checked, located, missing, selected, clicked, textarea, input};
}

function customControlInteractProbe() {
  installDom(`
    <div class="mtd-modal-content-wrapper">
      <label class="mtd-radio" id="rA">
        <span class="mtd-radio__input"><input type="radio" name="dfs" value="aipnlp"></span>
        <span class="mtd-radio__label">dolphinfs_aipnlp</span>
      </label>
      <label class="mtd-radio is-checked" id="rB">
        <span class="mtd-radio__input"><input type="radio" name="dfs" value="your-username" checked></span>
        <span class="mtd-radio__label">dolphinfs_your-username</span>
      </label>
      <label class="mtd-checkbox" id="cA">
        <span class="mtd-checkbox__input"><input type="checkbox" name="agree"></span>
        <span class="mtd-checkbox__label">同意协议</span>
      </label>
      <div class="mtd-select" id="selA">
        <div class="mtd-select__selection"><input placeholder="请选择队列"></div>
      </div>
      <ul class="mtd-select-dropdown" id="selA-list">
        <li class="mtd-select-dropdown__item">autoresearch_agent</li>
        <li class="mtd-select-dropdown__item">cpu_job</li>
      </ul>
      <select id="nativeSel"><option value="">请选择</option>
        <option value="bj">北京</option><option value="sh">上海</option></select>
    </div>`);
  // The option list only "opens" (becomes rendered) after the trigger is
  // clicked in a real MTD page; jsdom renders everything, so hide it until
  // the trigger's click listener reveals it — modelling the open behaviour.
  const out = vm.runInContext(`(async () => {
    const _qsa = document.querySelectorAll.bind(document);
    document.querySelectorAll = (sel) => _qsa(
      sel.split(',').filter((t) => !/[\\[v@]/.test(t)).join(',') || 'input');
    if (typeof CSS === 'undefined') {
      globalThis.CSS = {escape: (v) => String(v).replace(/[^a-zA-Z0-9_-]/g, (c) => String.fromCharCode(92) + c)};
    }
    // jsdom has no elementFromPoint; model it from our fixed overlay so the
    // OBSCURED classification can be exercised deterministically.
    document.elementFromPoint = (x, y) => (document.__overlayEl || null);
    const result = {};

    // ── _setChecked on a hidden radio wrapper (idempotent set) ──
    result.checkRadio = _pageInteraction('_setChecked', ['#rA', null, true, true]);
    result.radioAChecked = document.querySelector('#rA input').checked;
    result.radioBChecked = document.querySelector('#rB input').checked;
    // ── _setChecked idempotency: setting an already-true state is a no-op ──
    _pageInteraction('_setChecked', ['#cA', null, true, true]);   // ensure cA is checked first
    result.checkAlreadySet = _pageInteraction('_setChecked', ['#cA', null, true, true]);
    // ── _setChecked checkbox on then off ──
    result.checkBox = _pageInteraction('_setChecked', ['#cA', null, true, true]);
    result.boxCheckedAfterOn = document.querySelector('#cA input').checked;
    result.uncheckBox = _pageInteraction('_setChecked', ['#cA input', null, false, true]);
    result.boxCheckedAfterOff = document.querySelector('#cA input').checked;
    // ── _setChecked on a non-checkable → TARGET_NOT_CHECKABLE ──
    result.notCheckable = _pageInteraction('_setChecked', ['#selA', null, true, true]);
    // ── _setChecked missing target → TARGET_NOT_FOUND ──
    result.notFound = _pageInteraction('_setChecked', ['#doesNotExist', null, true, true]);

    // ── _actionablePoint on hidden radio wrapper forwards to input ──
    result.pointForward = _pageInteraction('_actionablePoint', ['#rA', true, '#rA input']);
    // ── _actionablePoint obscured → TARGET_OBSCURED (overlay on top) ──
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;z-index:9999';
    document.body.appendChild(overlay);
    document.__overlayEl = overlay;
    result.pointObscured = _pageInteraction('_actionablePoint', ['#rA', false, null]);
    document.__overlayEl = null;
    overlay.remove();

    // ── _selectOptionAsync on the custom MTD select ──
    const list = document.querySelector('#selA-list');
    const items = Array.from(list.querySelectorAll('.mtd-select-dropdown__item'));
    // Start hidden: override getBoundingClientRect for items to 0 until open.
    const realRects = new Map();
    for (const it of items) {
      realRects.set(it, it.getBoundingClientRect.bind(it));
      it.getBoundingClientRect = () => ({width:0,height:0,left:0,top:0,right:0,bottom:0});
    }
    document.querySelector('#selA').addEventListener('click', () => {
      for (const it of items) it.getBoundingClientRect = realRects.get(it);
    });
    result.selectCustom = await _pageInteraction('_selectOptionAsync', ['#selA', null, 'cpu_job', true, 1500]);
    // ── native <select> fast path ──
    result.selectNative = await _pageInteraction('_selectOptionAsync', ['#nativeSel', null, '北京', true, 1000]);
    // ── no matching option → error + options list ──
    result.selectMissing = await _pageInteraction('_selectOptionAsync', ['#selA', null, 'nonexistent_queue', true, 800]);
    return result;
  })()`, context);
  // runInContext returns the IIFE's Promise (async functions resolve across
  // the boundary). Await it host-side so the probe's async runner gets a
  // plain object, whatever it contained.
  return Promise.resolve(out).then((r) => {
    if (r && typeof r === 'object') return r;
    if (context.__interactError) throw new Error(context.__interactError);
    throw new Error('interact probe returned no result: ' + String(r));
  });
}

function paginationProbe() {
  installDom('<button id="wizard">Next</button>');
  const wizard = vm.runInContext("_researchAdvancePagination('auto')", context);

  installDom('<nav aria-label="pagination"><button id="next">下一页</button></nav>');
  let clicked = false;
  context.document.querySelector('#next').addEventListener(
    'click', () => { clicked = true; });
  const semantic = vm.runInContext("_researchAdvancePagination('auto')", context);

  installDom('<a rel="next" href="https://evil.example/page2">Next</a>');
  const crossOrigin = vm.runInContext(
    "_researchAdvancePagination('links')", context);

  installDom('<link rel="next" href="/list?page=2">');
  const sameOrigin = vm.runInContext(
    "_researchAdvancePagination('links')", context);

  return {wizard, semantic, clicked, crossOrigin, sameOrigin};
}

async function createTabDegradedProbe() {
  vm.runInContext(
    'waitForTabLoad = async () => {};\n' +
    '_waitForCapturedPageSettle = async () => {};',
    context,
  );
  // Existing captures do not prevent navigation; inject a real CDP error
  // to retain the best-effort capture failure/cleanup contract.
  const capacity = 64;
  const originalAttach = chrome.debugger.attach;
  chrome.debugger.attach = async () => { throw new Error('injected CDP unavailable'); };
  vm.runInContext(`
    for (let index = 0; index < ${capacity}; index += 1) {
      _networkCaptures.set('wedged-' + index, {
        captureId: 'wedged-' + index, tabId: 900 + index,
        captureBodies: false, responses: [], pendingBodies: new Set(),
        startedAt: Date.now(), lastActivityAt: Date.now(),
        cdpAttached: false, stopping: false,
      });
    }
  `, context);

  // 1. A genuine capture attach failure still permits ordinary navigation.
  operations.length = 0;
  const degradedResult = await vm.runInContext(
    "cmdCreateTab({url: 'https://example.test/target', active: false, waitForLoad: true})",
    context);
  const degradedOps = operations.slice();

  // 2. A navigation that itself fails must close the inert tab, not strand it.
  const originalUpdate = chrome.tabs.update;
  chrome.tabs.update = async () => {
    operations.push('tabs.update:FAIL');
    throw new Error('navigation blocked');
  };
  operations.length = 0;
  let navFailError = '';
  try {
    await vm.runInContext(
      "cmdCreateTab({url: 'https://example.test/blocked', active: false, waitForLoad: true})",
      context);
  } catch (error) {
    navFailError = String(error && error.message);
  } finally {
    chrome.tabs.update = originalUpdate;
  }
  const navFailOps = operations.slice();

  chrome.debugger.attach = originalAttach;
  return {capacity, degradedResult, degradedOps, navFailError, navFailOps};
}

async function navigateGuardProbe() {
  vm.runInContext(`
    SERVER_URL = 'https://tofu.test';
    CLIENT_ID = 'probe-client';
    BRIDGE_SECRET = 'probe-secret';
  `, context);

  // cmdListTabs flags the Tofu client tab so the server never seeds it as
  // the working tab.
  chrome.tabs.query = async () => ([
    {id: 1, url: 'https://tofu.test/app', title: 'Tofu', active: true,
     windowId: 1, index: 0, status: 'complete', pinned: false},
    {id: 2, url: 'https://other.example/', title: 'Other', active: false,
     windowId: 1, index: 1, status: 'complete', pinned: false},
  ]);
  const listTabs = await vm.runInContext('cmdListTabs({})', context);

  // Case 1: the target tab IS the Tofu client — must open a new tab, never
  // tabs.update the client tab — and that tab stays in the background.
  currentUrl = 'https://tofu.test/app';
  operations.length = 0;
  createdTabOptions.length = 0;
  const clientResult = await vm.runInContext(
    "cmdNavigate({tabId: 7, url: 'https://dev.example.test/', waitForLoad: false})",
    context);
  const clientOps = operations.slice();

  // Case 2: a normal tab navigates in place.
  currentUrl = 'https://elsewhere.example/page';
  operations.length = 0;
  const normalResult = await vm.runInContext(
    "cmdNavigate({tabId: 7, url: 'https://dev.example.test/', waitForLoad: false})",
    context);
  const normalOps = operations.slice();

  // Case 3: pre-pairing (no SERVER_URL) there is nothing to protect — even a
  // tofu.test-looking tab navigates in place.
  vm.runInContext("SERVER_URL = '';", context);
  currentUrl = 'https://tofu.test/app';
  operations.length = 0;
  const unpairedResult = await vm.runInContext(
    "cmdNavigate({tabId: 7, url: 'https://dev.example.test/', waitForLoad: false})",
    context);
  const unpairedOps = operations.slice();

  return {listTabs, clientResult, clientOps, normalResult, normalOps,
          unpairedResult, unpairedOps,
          clientCreateOptions: createdTabOptions.slice()};
}

async function automationOwnershipProbe() {
  vm.runInContext(`
    SERVER_URL = 'https://tofu.test';
    CLIENT_ID = 'probe-client';
  `, context);

  // A tab the extension opens for automation is recorded as owned.
  await vm.runInContext(
    "cmdCreateTab({url: 'https://example.test/a', active: false, waitForLoad: false})",
    context);
  createdTabOptions.length = 0;

  chrome.tabs.query = async () => ([
    {id: 7, url: 'https://example.test/a', title: 'Agent', active: false,
     windowId: 1, index: 1, status: 'complete', pinned: false},
    {id: 8, url: 'https://user.example/', title: 'User', active: true,
     windowId: 1, index: 2, status: 'complete', pinned: false},
  ]);
  const listed = await vm.runInContext('cmdListTabs({})', context);

  // An MV3 worker restart wipes memory; the session ledger must restore it.
  const restored = await vm.runInContext(`
    _automationTabIds.clear();
    _automationTabRestore = null;
    cmdListTabs({}).then((rows) => rows.map((t) => t.automation));
  `, context);

  // Closing the tab retires ownership so a recycled id is never adopted.
  const afterClose = await vm.runInContext(`
    _forgetAutomationTab(7);
    cmdListTabs({}).then((rows) => rows.map((t) => t.automation));
  `, context);

  // The same path Chrome itself uses when the user closes the tab.
  await vm.runInContext('_rememberAutomationTab(7)', context);
  tabRemovedEvents.emit(7);
  await new Promise((resolve) => setImmediate(resolve));
  const afterRemovalEvent = await vm.runInContext(
    '_automationTabIds.has(7)', context);

  return {
    listed: listed.map((t) => ({id: t.id, automation: t.automation})),
    restored, afterClose, afterRemovalEvent,
  };
}

async function temporaryTabTimeoutProbe() {
  const timers = new Map();
  let timerId = 0;
  context.setTimeout = (callback, delay) => {
    timers.set(++timerId, {callback, delay});
    return timerId;
  };
  context.clearTimeout = (id) => timers.delete(id);
  let finishBlockedOperation;
  const blocked = () => new Promise((resolve) => { finishBlockedOperation = resolve; });
  const phase = mode.split(':')[1] || 'attach';
  const commandType = mode.split(':')[2] || 'fetch_url';
  if (phase === 'attach') chrome.debugger.attach = blocked;
  const sendCommand = chrome.debugger.sendCommand;
  if (phase === 'enable' || phase === 'stop') {
    chrome.debugger.sendCommand = (target, method) =>
      method === (phase === 'enable' ? 'Network.enable' : 'Network.disable')
        ? blocked() : sendCommand(target, method);
  }
  if (phase === 'extract') chrome.scripting.executeScript = blocked;
  const create = chrome.tabs.create;
  if (phase === 'create') chrome.tabs.create = async (opts) => { await blocked(); return create(opts); };
  const fetch = context.fetch;
  if (phase === 'probe') context.fetch = async (...args) => { await blocked(); return fetch(...args); };
  vm.runInContext('waitForTabLoad = async () => {}; _waitForCapturedPageSettle = async () => {};', context);
  let removed = false;
  chrome.tabs.get = async (id) => {
    if (removed) throw new Error('No tab with id');
    return {id, url: currentUrl, status: 'complete'};
  };
  chrome.tabs.remove = async (id) => {
    removed = true;
    operations.push(`tabs.remove:${id}`);
    chrome.debugger.onDetach.emit({tabId: id}, 'target_closed');
    chrome.tabs.onRemoved.emit(id);
  };
  const execution = vm.runInContext(`executeAndReport({
    id: 'search-fetch-timeout', type: '${commandType}',
    params: {url: 'https://example.test/app'},
  })`, context);
  for (let i = 0; i < 100; i++) await Promise.resolve();
  if (!finishBlockedOperation) throw new Error('Probe never reached ' + phase);
  const timeout = Array.from(timers.values()).find((timer) => timer.delay === (commandType === 'research_url' ? 80000 : 35000));
  timeout.callback();
  await execution;
  const removedAtTimeout = removed;
  finishBlockedOperation();
  for (let i = 0; i < 100; i++) await Promise.resolve();
  return {
    removedAtTimeout, removedEventually: removed, operations, sessionData,
    results: vm.runInContext('_resultQueue.slice()', context),
    captures: vm.runInContext('_networkCaptures.size', context),
  };
}

async function temporaryTabCapacityProbe() {
  const timers = new Map();
  let timerId = 0;
  context.setTimeout = (callback, delay) => {
    timers.set(++timerId, {callback, delay});
    return timerId;
  };
  context.clearTimeout = (id) => timers.delete(id);
  const live = new Set();
  let peakTabs = 0;
  chrome.tabs.create = async () => {
    const tab = {id: nextTabId++, url: 'about:blank'};
    live.add(tab.id);
    peakTabs = Math.max(peakTabs, live.size);
    return tab;
  };
  chrome.tabs.get = async (id) => {
    if (!live.has(id)) throw new Error('No tab with id');
    return {id, url: currentUrl, status: 'complete'};
  };
  chrome.tabs.remove = async (id) => {
    live.delete(id);
    chrome.debugger.onDetach.emit({tabId: id}, 'target_closed');
    chrome.tabs.onRemoved.emit(id);
  };
  chrome.debugger.attach = () => new Promise(() => {});
  const executions = Array.from({length: 12}, (_, index) => vm.runInContext(
    `executeAndReport({id: 'search-${index}', type: 'fetch_url', params: {url: 'https://example.test/app'}})`, context));
  for (let i = 0; i < 200; i++) await Promise.resolve();
  for (const timer of [...timers.values()]) if (timer.delay === 35000) timer.callback();
  await Promise.all(executions);
  const results = vm.runInContext('_resultQueue.slice()', context);
  chrome.debugger.attach = async () => {};
  vm.runInContext('waitForTabLoad = async () => {}; _waitForCapturedPageSettle = async () => {};', context);
  await vm.runInContext(`executeAndReport({id: 'after-timeouts', type: 'fetch_url', params: {url: 'https://example.test/app'}})`, context);
  return {
    peakTabs, remainingTabs: live.size, sessionData,
    results, recoveredResult: vm.runInContext('_resultQueue[_resultQueue.length - 1]', context),
  };
}

async function debuggerSetupSettlementProbe() {
  currentUrl = 'https://example.test/app';
  const cancel = mode.endsWith(':cancel');
  const originalSend = chrome.debugger.sendCommand;
  let finishEnable;
  chrome.debugger.sendCommand = async (target, method, params) => {
    if (method === 'Runtime.enable') {
      if (!cancel) throw new Error('injected debugger setup failure');
      await new Promise(resolve => {finishEnable = resolve;});
    }
    return originalSend(target, method, params);
  };
  const starting = vm.runInContext("_startDebugSession({id: 4500, url: 'https://example.test/app'}, 30000)", context);
  const settledStart = starting.then(() => '', error => String(error.message));
  if (cancel) {
    for (let index = 0; index < 100; index += 1) await Promise.resolve();
    const stopped = vm.runInContext("_stopDebugSession(4500, 'cancelled')", context);
    finishEnable();
    await stopped;
  }
  const error = await settledStart;
  chrome.debugger.sendCommand = originalSend;
  return {error, remaining: vm.runInContext(
    '({debugSessions: _debugSessions.size, cdpSessions: _cdpSessions.size})', context)};
}

async function duplicateDebuggerStartProbe() {
  currentUrl = 'https://example.test/app';
  const originalAttach = chrome.debugger.attach;
  let resolveAttach;
  chrome.debugger.attach = () => new Promise(resolve => {resolveAttach = resolve;});
  const starters = vm.runInContext(`Array.from({length: 16}, () =>
    _startDebugSession({id: 4000, url: 'https://example.test/app'}, 30000))`, context);
  for (let index = 0; index < 100; index += 1) await Promise.resolve();
  resolveAttach();
  const sessions = await Promise.all(starters);
  const holders = vm.runInContext('_cdpSessions.get(4000).holders.size', context);
  const sameSession = sessions.every(session => session === sessions[0]);
  await vm.runInContext("_stopDebugSession(4000, 'test-complete')", context);
  chrome.debugger.attach = originalAttach;
  return {holders, sameSession, remaining: vm.runInContext(
    '({debugSessions: _debugSessions.size, cdpSessions: _cdpSessions.size})', context)};
}

async function demandResourceBurstProbe() {
  const started = process.hrtime.bigint();
  const heapBefore = process.memoryUsage().heapUsed;
  currentUrl = 'https://example.test/app';
  const originalSend = chrome.debugger.sendCommand;
  let breakpointNumber = 0;
  chrome.debugger.sendCommand = async (target, method, params) => {
    if (method === 'Debugger.setBreakpointByUrl') {
      return {breakpointId: `breakpoint-${++breakpointNumber}`, locations: []};
    }
    return originalSend(target, method, params);
  };
  const captures = await vm.runInContext(`Promise.all(Array.from({length: 64}, (_, index) =>
    _startNetworkCapture({tabId: 1000 + index, captureBodies: true})))`, context);
  const sessions = await vm.runInContext(`Promise.all(Array.from({length: 16}, (_, index) =>
    _startDebugSession({id: 2000 + index, url: 'https://example.test/app'}, 30000)))`, context);
  const counts = vm.runInContext('({captures: _networkCaptures.size, debugSessions: _debugSessions.size})', context);
  try {
    for (let index = 0; index < 40; index += 1) {
      await vm.runInContext(`cmdDevtools({action: 'breakpoint_set', tabId: 2000,
        sourceUrl: 'https://example.test/app.js', lineNumber: ${index}})`, context);
    }
    const breakpointIds = vm.runInContext(`Array.from(_debugSessions.get(2000).breakpoints.values())
      .map(value => value.breakpointId)`, context);
    debuggerEvents.emit({tabId: 2000}, 'Debugger.paused', {
      reason: 'other', callFrames: [], hitBreakpoints: breakpointIds,
    });
    counts.breakpoints = vm.runInContext('_debugSessions.get(2000).breakpoints.size', context);
    counts.pausedBreakpoints = vm.runInContext('_debugSessions.get(2000).paused.hitBreakpoints.length', context);
    const observers = vm.runInContext(`Promise.all(Array.from({length: 16}, (_, index) =>
      _observeDevtools(3000 + index, 200)))`, context);
    for (let index = 0; index < 200; index += 1) await Promise.resolve();
    counts.observers = vm.runInContext('_devtoolsObservers.size', context);
    await observers;
  } finally {
    for (const capture of captures) {
      await vm.runInContext(`_stopNetworkCaptureInternal(${JSON.stringify(capture.captureId)}, {remember: false})`, context);
    }
    for (const session of sessions) {
      await vm.runInContext(`_stopDebugSession(${session.tabId}, 'test-complete')`, context);
    }
    chrome.debugger.sendCommand = originalSend;
  }
  return {counts, elapsedMs: Number(process.hrtime.bigint() - started) / 1e6,
    heapGrowthBytes: process.memoryUsage().heapUsed - heapBefore,
    remaining: vm.runInContext(`({captures: _networkCaptures.size,
    debugSessions: _debugSessions.size, observers: _devtoolsObservers.size,
    cdpSessions: _cdpSessions.size})`, context)};
}

async function fileProbeFailureProbe() {
  context.fetch = async () => { throw new Error('probe unavailable'); };
  await vm.runInContext(`executeAndReport({id: 'probe-failed', type: 'fetch_url', params: {url: 'https://example.test/app'}})`, context);
  return {operations, result: vm.runInContext('_resultQueue[0]', context)};
}

async function temporaryTabFailureProbe() {
  const phase = mode.split(':')[1];
  const remove = chrome.tabs.remove;
  chrome.tabs.remove = async (id) => {
    if (phase === 'cleanup') throw new Error('Chrome refused cleanup');
    await remove(id);
    chrome.debugger.onDetach.emit({tabId: id}, 'target_closed');
    chrome.tabs.onRemoved.emit(id);
  };
  if (phase === 'storage') chrome.storage.session.set = async () => { throw new Error('storage unavailable'); };
  if (phase === 'navigation') chrome.tabs.update = async () => { throw new Error('navigation blocked'); };
  vm.runInContext('waitForTabLoad = async () => {}; _waitForCapturedPageSettle = async () => {};', context);
  await vm.runInContext(`executeAndReport({id: 'failed-operation', type: 'fetch_url', params: {url: 'https://example.test/app'}})`, context);
  return {operations, sessionData, removedTabIds, result: vm.runInContext('_resultQueue[0]', context)};
}

async function selfUpdateProbe() {
  // The harness manifest reports version 'test'; the on-disk copy is
  // whatever this fetch stub serves.
  const disk = {version: 'test'};
  context.fetch = async () => ({
    ok: true,
    status: 200,
    json: async () => ({version: disk.version}),
  });
  const flush = async () => {
    for (let index = 0; index < 60; index += 1) await Promise.resolve();
  };

  // Same version on disk → the watchdog stays put.
  chrome.alarms.onAlarm.emit({name: 'keepAlive'});
  await flush();
  const sameVersionReloads = runtimeCalls.reload;

  // Fresh ZIP unzipped over the loaded folder → version moved → reload.
  disk.version = 'test+1';
  chrome.alarms.onAlarm.emit({name: 'keepAlive'});
  await flush();
  const afterDiskUpgrade = runtimeCalls.reload;

  // Queued results live only in worker memory → defer, never drop.
  vm.runInContext(
    "_resultQueue.push({id: 'held-result', result: {ok: true}});", context);
  chrome.alarms.onAlarm.emit({name: 'keepAlive'});
  await flush();
  const busyDeferred = runtimeCalls.reload;
  vm.runInContext('_resultQueue.splice(0, _resultQueue.length);', context);

  return {sameVersionReloads, afterDiskUpgrade, busyDeferred};
}

const probes = {
  customControls: customControlsProbe,
  customControlInteract: customControlInteractProbe,
  isolatedInteraction: isolatedInteractionProbe,
  selfUpdate: selfUpdateProbe,
  fileProbeFailure: fileProbeFailureProbe,
  temporaryTabCapacity: temporaryTabCapacityProbe,
  demandResourceBurst: demandResourceBurstProbe,
  duplicateDebuggerStart: duplicateDebuggerStartProbe,
  'debuggerSetupSettlement:cancel': debuggerSetupSettlementProbe,
  'debuggerSetupSettlement:failure': debuggerSetupSettlementProbe,
  temporaryTabRecovery: async () => ({removedTabIds, sessionData}),
  temporaryTabTimeout: temporaryTabTimeoutProbe,
  diagnosticUrl: diagnosticUrlProbe,
  createTabDegraded: createTabDegradedProbe,
  navigateGuard: navigateGuardProbe,
  automationOwnership: automationOwnershipProbe,
  fetch: fetchProbe,
  fileTransferCleanup: fileTransferCleanupProbe,
  fileTransferDeadline: fileTransferDeadlineProbe,
  fileTransfer: fileTransferProbe,
  protocol426: protocolProbe,
  rate429: rateLimitProbe,
  resultBatch: resultBatchProbe,
  payload413: payloadLimitProbe,
  transportFailure: transportFailureProbe,
  research: researchProbe,
  researchHintBudget: researchHintBudgetProbe,
  pagination: paginationProbe,
};

Promise.resolve().then(async () => {
  // Let startup storage reconciliation finish before a probe replaces timers.
  for (let i = 0; i < 50; i++) await Promise.resolve();
  const probe = probes[mode] || (mode.startsWith('temporaryTabTimeout:') && temporaryTabTimeoutProbe)
    || (mode.startsWith('temporaryTabFailure:') && temporaryTabFailureProbe);
  if (!probe) throw new Error(`unknown probe mode: ${mode}`);
  return probe();
})
  .then((result) => {
    process.stdout.write(JSON.stringify(result));
  })
  .catch((error) => {
    console.error(error);
    process.exit(1);
  });
