import { buildSync } from 'esbuild';
/** List clients expose domain arrays while admitting only canonical wire data. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { test } from 'node:test';

const source = fs.readFileSync(new URL('../frontend/src/runtime/sections/api.js', import.meta.url), 'utf8');
class ApiError extends Error {
  constructor(message, properties) { super(message); Object.assign(this, properties); }
}
const registryBundle = buildSync({ entryPoints: ['frontend/src/api/endpoint-registry/index.ts'], bundle: true, format: 'iife', globalName: 'EndpointRegistryOwner', write: false }).outputFiles[0].text;

function client() {
  let result, failure;
  const calls = [];
  const context = vm.createContext({runtimeScope: {}, requiredApiTransport: {
    ApiError, resolvePath: (path) => '/proxy/30000' + path,
    request: async (path, options) => {
      calls.push({path, options});
      if (failure) throw failure;
      return result;
    },
  }});
  vm.runInContext(registryBundle, context);
  context.createEndpointRegistry = context.EndpointRegistryOwner.createEndpointRegistry;
  vm.runInContext(source, context);
  return {api: context.runtimeScope.Api, calls,
    respond(value) { result = value; failure = null; },
    fail(error) { failure = error; },
  };
}

for (const [name, route, invoke] of [
  ['folders', '/api/v4/folders', (api) => api.folders.list()],
  ['paper folders', '/api/v4/paper-folders', (api) => api.paperFolders.list()],
  ['provider templates', '/api/v4/providers/templates', (api) => api.providers.templates()],
]) {
  test(`${name} distinguishes valid empty data, malformed wire, logical and HTTP failures`, async () => {
    const owner = client();
    for (const items of [[], [{id: 'one'}]]) {
      owner.respond({ok: true, items});
      assert.equal(await invoke(owner.api), items);
      assert.equal(owner.calls.at(-1).path, route);
    }
    for (const body of [null, [], [{id: 'legacy'}], {}, {items: null}, {ok: false, items: []}]) {
      owner.respond(body);
      await assert.rejects(() => invoke(owner.api), (error) => {
        assert.ok(error instanceof ApiError);
        assert.equal(error.code, 'parse');
        assert.equal(error.url, '/proxy/30000' + route);
        assert.equal(error.body, body);
        return true;
      });
    }
    const denied = new ApiError('Denied', {status: 403, code: 'permission_denied'});
    owner.fail(denied);
    await assert.rejects(() => invoke(owner.api), (error) => error === denied);
  });
}

for (const [name, invoke, fallback] of [
  ['conversation search', (api) => api.conversations.search('query'), []],
  ['chat queue', (api) => api.chat.queueGet('conversation'), []],
  ['translation batch probe', (api) => api.translate.pollBatch(['task']), null],
]) {
  test(`${name} projects canonical items and preserves its declared fallback`, async () => {
    const owner = client();
    for (const items of [[], [{id: 'one'}]]) {
      owner.respond({ok: true, items});
      assert.equal(await invoke(owner.api), items);
    }
    for (const body of [null, [], [{id: 'legacy'}], {}, {items: null}]) {
      owner.respond(body);
      assert.deepEqual(JSON.parse(JSON.stringify(await invoke(owner.api))), fallback);
    }
    const failed = new ApiError('Unavailable', {status: 503});
    owner.fail(failed);
    await assert.rejects(() => invoke(owner.api), (error) => error === failed);
  });
}
