"""Vite API transport ownership and deployment-prefix resolution."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.unit
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
ESBUILD = os.path.join(ROOT, 'scripts', 'vite_test_bundle.mjs')
ENTRY = os.path.join(ROOT, 'frontend/src/api/transport.ts')
ERRORS_ENTRY = os.path.join(ROOT, 'frontend/src/api/errors.ts')
CONVERSATION_SYNC_ENTRY = os.path.join(
    ROOT, 'frontend/src/api/conversation-sync.generated.ts')


@pytest.mark.skipif(
    not shutil.which('node') or not os.path.isfile(ESBUILD),
    reason='node + vite test bundler unavailable',
)
def test_transport_resolves_main_admin_and_proxy_paths(tmp_path):
    built = tmp_path / 'api-transport.js'
    compiled = subprocess.run(
        [ESBUILD, ENTRY, '--bundle', '--format=cjs', '--platform=node',
         f'--outfile={built}'],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert compiled.returncode == 0, compiled.stderr
    harness = r"""
global.window = global;
global.location = { pathname: '/' };
global.document = {
  config: { entry: 'main' },
  getElementById: () => ({ textContent: JSON.stringify(global.document.config) }),
};
global.sessionStorage = { getItem: () => null, setItem: () => {} };
const TofuNativeApi = require(BUILT);
function at(pathname, entry, target = '/api/v1/auth/mode') {
  global.location.pathname = pathname;
  global.document.config = { entry };
  return TofuNativeApi.resolvePath(target);
}
console.log(JSON.stringify({
  root: at('/', 'main'),
  mainProxy: at('/proxy/15000/', 'main'),
  indexProxy: at('/proxy/15000/index.html', 'main'),
  admin: at('/admin', 'admin'),
  adminSlash: at('/admin/', 'admin'),
  adminProxy: at('/proxy/15000/admin', 'admin'),
  absolute: at('/admin', 'admin', 'https://example.test/value'),
}));
""".replace('BUILT', json.dumps(str(built)))
    proc = subprocess.run(
        ['node', '-e', harness], cwd=ROOT,
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, (proc.stdout or '') + (proc.stderr or '')
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == {
        'root': '/api/v1/auth/mode',
        'mainProxy': '/proxy/15000/api/v1/auth/mode',
        'indexProxy': '/proxy/15000/api/v1/auth/mode',
        'admin': '/api/v1/auth/mode',
        'adminSlash': '/api/v1/auth/mode',
        'adminProxy': '/proxy/15000/api/v1/auth/mode',
        'absolute': 'https://example.test/value',
    }


@pytest.mark.skipif(
    not shutil.which('node') or not os.path.isfile(ESBUILD),
    reason='node + vite test bundler unavailable',
)
def test_transport_keeps_failure_channels_and_request_ids_distinct(tmp_path):
    entry = tmp_path / 'api-transport-errors-entry.ts'
    entry.write_text(
        f'export * from {json.dumps(ENTRY)};\n'
        f'export {{ apiFailure }} from {json.dumps(ERRORS_ENTRY)};\n',
        encoding='utf-8',
    )
    built = tmp_path / 'api-transport-errors.js'
    compiled = subprocess.run(
        [ESBUILD, str(entry), '--bundle', '--format=cjs', '--platform=node',
         f'--outfile={built}'],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert compiled.returncode == 0, compiled.stderr
    harness = r"""
const assert = require('assert');
global.window = globalThis;
global.location = { pathname: '/' };
global.document = { getElementById() { return null; } };
global.sessionStorage = { getItem() { return null; }, setItem() {} };
const transport = require(BUILT);

function response(status, body, contentType = 'application/json', requestId = null) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get(name) {
      if (name.toLowerCase() === 'content-type') return contentType;
      if (name.toLowerCase() === 'x-request-id') return requestId;
      return null;
    } },
    async json() { return body; },
    async text() { return typeof body === 'string' ? body : JSON.stringify(body); },
  };
}

async function rejected(promise) {
  try {
    await promise;
  } catch (error) {
    return error;
  }
  throw new Error('expected request to reject');
}

(async () => {
  global.fetch = async () => response(409, {
    type: 'urn:tofu:problem:turn_conflict',
    title: 'Turn conflict',
    status: 409,
    detail: 'The turn revision changed.',
    instance: '/api/v4/turns/turn-1',
    code: 'turn_conflict',
    requestId: 'problem-rid',
  }, 'application/problem+json; charset=utf-8', 'header-rid');
  const problem = await rejected(transport.request('/api/v4/turns/turn-1'));
  assert(problem instanceof transport.ApiError);
  assert.strictEqual(problem.code, 'turn_conflict');
  assert.strictEqual(problem.message, 'The turn revision changed.');
  assert.strictEqual(problem.problem.code, 'turn_conflict');
  assert.strictEqual(problem.envelope, null);
  assert.strictEqual(problem.requestId, 'problem-rid');
  assert.strictEqual(problem.serverRequestId, 'header-rid');
  assert.match(problem.clientRequestId, /^[a-z0-9]+-\d+$/);
  const problemFailure = transport.apiFailure(problem);
  assert.strictEqual(problemFailure.problem.code, 'turn_conflict');
  assert.strictEqual(problemFailure.envelope, null);

  global.fetch = async () => response(409, {
    type: 'urn:tofu:problem:wrong-status',
    title: 'Wrong status',
    status: 500,
    detail: 'The body contradicts HTTP.',
    instance: '/api/v4/turns/turn-1',
    code: 'wrong_status',
    requestId: 'invalid-problem-rid',
  }, 'application/problem+json');
  const invalidProblem = await rejected(transport.request('/api/v4/turns/turn-1'));
  assert.strictEqual(invalidProblem.code, 'invalid_problem');
  assert.strictEqual(invalidProblem.problem, null);
  assert.strictEqual(invalidProblem.envelope, null);
  assert.strictEqual(invalidProblem.requestId, 'invalid-problem-rid');

  global.fetch = async () => response(409, {
    type: 'urn:tofu:problem:extra-field',
    title: 'Unexpected extension',
    status: 409,
    detail: 'The closed problem schema was widened.',
    instance: '/api/v4/turns/turn-1',
    code: 'extra_field',
    requestId: 'extra-problem-rid',
    privateDebug: 'must not be accepted as a typed problem',
  }, 'application/problem+json');
  const extraProblem = await rejected(transport.request('/api/v4/turns/turn-1'));
  assert.strictEqual(extraProblem.code, 'invalid_problem');
  assert.strictEqual(extraProblem.problem, null);

  global.fetch = async () => response(409, {
    type: 'urn:tofu:problem:not-declared',
    title: 'JSON conflict',
    status: 409,
    detail: 'Plain JSON is not the problem channel.',
    instance: '/api/v1/plain-json',
    code: 'json_conflict',
    requestId: 'plain-json-rid',
  }, 'application/json');
  const plainJson = await rejected(transport.request('/api/v1/plain-json'));
  assert.strictEqual(plainJson.code, 'json_conflict');
  assert.strictEqual(plainJson.problem, null);

  global.fetch = async () => response(429, {
    ok: false,
    error: { kind: 'ratelimit', message: 'Provider window is full' },
    request_id: 'envelope-rid',
  }, 'application/json', 'envelope-header-rid');
  const typed = await rejected(transport.request('/api/v1/tasks/start'));
  assert.strictEqual(typed.code, 'ratelimit');
  assert.strictEqual(typeof typed.code, 'string');
  assert.strictEqual(typed.envelope.kind, 'ratelimit');
  assert.strictEqual(typed.problem, null);
  assert.strictEqual(typed.requestId, 'envelope-rid');
  const typedFailure = transport.apiFailure(typed);
  assert.strictEqual(typedFailure.problem, null);
  assert.strictEqual(typedFailure.envelope.kind, 'ratelimit');

  global.fetch = async () => response(503, {
    ok: false,
    error: 'database_busy',
    message: 'Storage is temporarily busy',
    request_id: 'storage-rid',
  });
  const storage = await rejected(transport.request('/api/v1/conversations'));
  assert.strictEqual(storage.code, 'database_busy');
  assert.strictEqual(storage.message, 'Storage is temporarily busy');

  global.fetch = async () => response(
    200, '<html>proxy error</html>', 'text/html', 'parse-server-rid');
  const parse = await rejected(transport.request('/api/v1/config'));
  assert.strictEqual(parse.code, 'parse');
  assert.strictEqual(parse.status, 200);
  assert.strictEqual(parse.requestId, 'parse-server-rid');
  assert.strictEqual(parse.serverRequestId, 'parse-server-rid');
  assert.match(parse.clientRequestId, /^[a-z0-9]+-\d+$/);

  global.fetch = (_url, init) => new Promise((_resolve, reject) => {
    init.signal.addEventListener('abort', () => {
      reject(init.signal.reason || Object.assign(new Error('aborted'), { name: 'AbortError' }));
    }, { once: true });
  });
  const timeout = await rejected(transport.request('/api/v1/slow', { timeout: 5 }));
  assert(timeout instanceof transport.ApiError);
  assert.strictEqual(timeout.code, 'timeout');
  assert.notStrictEqual(timeout.code, 'network');
  assert.match(timeout.requestId, /^[a-z0-9]+-\d+$/);
  assert.strictEqual(timeout.clientRequestId, timeout.requestId);

  const controller = new AbortController();
  const aborting = transport.request('/api/v1/abort', { signal: controller.signal });
  controller.abort(new Error('navigation changed'));
  const aborted = await rejected(aborting);
  assert(aborted instanceof transport.ApiError);
  assert.strictEqual(aborted.code, 'aborted');
  assert.notStrictEqual(aborted.code, 'timeout');
  assert.notStrictEqual(aborted.code, 'network');

  global.fetch = async () => { throw new TypeError('connection reset'); };
  const network = await rejected(transport.request('/api/v1/offline'));
  assert.strictEqual(network.code, 'network');
})().catch((error) => {
  console.error(error && error.stack || error);
  process.exitCode = 1;
});
""".replace('BUILT', json.dumps(str(built)))
    proc = subprocess.run(
        ['node', '-e', harness], cwd=ROOT,
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, (proc.stdout or '') + (proc.stderr or '')


def test_main_and_admin_publish_one_shared_transport():
    transport = open(ENTRY, encoding='utf-8').read()
    main = open(os.path.join(ROOT, 'frontend/src/main.ts'), encoding='utf-8').read()
    admin = open(os.path.join(ROOT, 'frontend/src/admin.ts'), encoding='utf-8').read()
    runtime = open(os.path.join(
        ROOT, 'frontend/src/runtime/app-runtime.js'), encoding='utf-8').read()
    assert transport.count('fetch(url, init)') == 1
    assert "from './api/transport'" in main
    assert "from './api/transport'" in admin
    assert 'installLegacyApiBindings();' in main
    assert 'global.Api = Api' in runtime
    assert 'publicWindow.Api = apiTransport' in admin


def test_idempotent_turn_commands_retry_contract_transients_with_same_command_id():
    node = shutil.which('node')
    if not node:
        pytest.skip('node is required for the API retry contract')
    api_path = native_module_path(
        'conversation-sync-api-browser.js', CONVERSATION_SYNC_ENTRY)
    harness = r'''
const assert = require('assert');
global.window = globalThis;
global.location = { pathname: '/' };
global.document = { getElementById() { return null; } };
global.sessionStorage = {
  getItem() { return null; },
  setItem() {},
};

let transientCalls = 0;
let networkCalls = 0;
const payloads = [];
let mode = 'transient';
let mutableBody = null;
function response(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get(name) {
      if (name.toLowerCase() === 'content-type') return 'application/json';
      if (name === 'Retry-After') return '0';
      return null;
    } },
    async json() { return body; },
    async text() { return JSON.stringify(body); },
  };
}
global.fetch = async (_url, init) => {
  const payload = JSON.parse(init.body);
  payloads.push(payload);
  if (mode === 'hard') {
    return response(500, {
      error: { kind: 'internal', message: 'internal', retryable: false },
    });
  }
  if (mode === 'network') {
    networkCalls += 1;
    if (networkCalls < 3) throw new TypeError('connection reset');
    return response(200, {
      ok: true,
      conversationId: 'conv id',
      conversationRevision: 2,
      attempt: {
        attemptId: 'attempt-network', conversationId: 'conv id',
        turnId: 'turn-network', commandId: 'network-command-id',
        taskId: 'task-network', operation: 'generate', status: 'running',
        baseProjectionRevision: 0, resumeAnchor: {}, createdAt: 2,
      },
    });
  }
  transientCalls += 1;
  if (transientCalls < 3) {
    if (transientCalls === 1) mutableBody.inputTurn.content = 'mutated-after-send';
    return response(503, {
      ok: false,
      error: {
        kind: 'server_busy', message: 'Storage is temporarily busy',
        retryable: true, storageCode: 'database_timeout',
      },
      storageCode: 'database_timeout', retryAfterMs: 1,
    });
  }
  return response(200, {
    ok: true,
    conversationId: 'conv id',
    conversationRevision: 1,
    attempt: {
      attemptId: 'attempt-1', conversationId: 'conv id', turnId: 'turn id',
      commandId: 'stable-command-id', taskId: 'task-1',
      operation: 'generate', status: 'running', baseProjectionRevision: 0,
      resumeAnchor: {}, createdAt: 1,
    },
  });
};
require(process.argv[1]);

(async () => {
  const body = mutableBody = {
    commandId: 'stable-command-id',
    inputTurn: { content: 'hello' },
    config: {},
  };
  const acceptedBody = {
    commandId: 'stable-command-id',
    inputTurn: { content: 'hello' },
    config: {},
  };
  const result = await global.conversationSyncApi.createTurn(
    'conv id', body, {});
  assert.strictEqual(result.attempt.attemptId, 'attempt-1');
  assert.strictEqual(transientCalls, 3);
  assert.strictEqual(payloads.length, 3);
  for (const payload of payloads) {
    assert.deepStrictEqual(payload, acceptedBody);
    assert.strictEqual(payload.commandId, 'stable-command-id');
  }

  mode = 'network';
  const beforeNetwork = payloads.length;
  const networkBody = {
    commandId: 'network-command-id', inputTurn: { content: 'again' }, config: {},
  };
  const networkResult = await global.conversationSyncApi.createTurn(
    'conv id', networkBody, {});
  assert.strictEqual(networkResult.attempt.attemptId, 'attempt-network');
  assert.strictEqual(networkCalls, 3);
  assert.strictEqual(payloads.length - beforeNetwork, 3);
  for (const payload of payloads.slice(beforeNetwork)) {
    assert.deepStrictEqual(payload, networkBody);
  }

  mode = 'hard';
  const beforeHard = payloads.length;
  await assert.rejects(
    global.conversationSyncApi.createAttempt(
      'conv id', 'turn id',
      {
        commandId: 'hard-failure', operation: 'regenerate',
        expectedProjectionRevision: 1, config: {},
      }, {}),
    /internal/,
  );
  assert.strictEqual(payloads.length - beforeHard, 1);
})().catch((error) => {
  console.error(error && error.stack || error);
  process.exitCode = 1;
});
'''
    result = subprocess.run(
        [node, '-e', harness, api_path],
        cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr or result.stdout
