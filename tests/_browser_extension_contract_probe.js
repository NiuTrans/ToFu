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
const operations = [];
const debuggerCommands = [];
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
    onRemoved: listeners(),
    onUpdated: listeners(),
    create: async (options) => {
      operations.push(`tabs.create:${options.url}`);
      currentUrl = options.url;
      return {id: nextTabId, url: currentUrl, title: 'Probe', status: 'complete'};
    },
    update: async (id, options) => {
      operations.push(`tabs.update:${options.url || ''}`);
      if (options.url) currentUrl = options.url;
      return {id, url: currentUrl, title: 'Probe', status: 'complete'};
    },
    get: async (id) => ({id, url: currentUrl, title: 'Probe', status: 'complete'}),
    remove: async () => { operations.push('tabs.remove'); },
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
    onInstalled: listeners(),
    onStartup: listeners(),
    onMessage: listeners(),
  },
  alarms: {create() {}, onAlarm: listeners()},
  storage: {
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
    active: NETWORK_CAPTURE_MAX_ACTIVE,
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
    active: NETWORK_CAPTURE_MAX_ACTIVE,
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
  for (const element of context.document.querySelectorAll('*')) {
    element.getBoundingClientRect = () => ({width: 80, height: 24});
    element.scrollIntoView = () => {};
  }
  return dom;
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
  // tabs.update the client tab.
  currentUrl = 'https://tofu.test/app';
  operations.length = 0;
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
          unpairedResult, unpairedOps};
}

const probes = {
  diagnosticUrl: diagnosticUrlProbe,
  navigateGuard: navigateGuardProbe,
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

Promise.resolve(probes[mode] && probes[mode]())
  .then((result) => {
    if (!probes[mode]) throw new Error(`unknown probe mode: ${mode}`);
    process.stdout.write(JSON.stringify(result));
  })
  .catch((error) => {
    console.error(error);
    process.exit(1);
  });
