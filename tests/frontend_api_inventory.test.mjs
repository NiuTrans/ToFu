import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { collectApiSources, scanApiSources } from '../scripts/frontend_api_inventory.mjs';

test('typed request aliases, path functions, concatenation and option overrides', () => {
  const inventory = scanApiSources({
    'frontend/src/api/sample.ts': [
      "import { request as send, resolvePath } from './transport';",
      "const base = '/api/v4/sample';",
      "const item = (id: string) => base + '/' + encodeURIComponent(id);",
      "const write = { method: 'POST', signal };",
      'send<{ value: string }>(item(id), write);',
      "send(item(id), {...write, method: 'DELETE'});",
      "send(base, Object.assign({method: 'POST'}, {method: 'PUT'}));",
      'resolvePath(item(id) + "/pages/" + `${encodeURIComponent(n)}.png`);',
    ].join('\n'),
  });
  assert.deepEqual(inventory.errors, []);
  assert.deepEqual(inventory.deferred, []);
  assert.deepEqual(inventory.endpoints.map(({ path, method }) => [path, method]), [
    ['/api/v4/sample/<dynamic>', 'POST'],
    ['/api/v4/sample/<dynamic>', 'DELETE'],
    ['/api/v4/sample', 'PUT'],
    ['/api/v4/sample/<dynamic>/pages/<dynamic>.png', null],
  ]);
});

test('generated catalogues, branch paths, dynamic verbs and caller-owned URLs stay visible', () => {
  const inventory = scanApiSources({
    'frontend/src/api/sample.ts': `
      import { request } from './transport';
      request(enabled ? '/api/v4/a' : '/api/v4/b', {method: action});
      request(downloadUrl, {parse: 'response'});
      const endpoints = {read: {route: '/api/v4/items/<item_id>', method: 'GET'}};
      // request('/api/v4/comment', {method: 'POST'});
      const text = "request('/api/v4/quoted')";
    `,
  });
  assert.deepEqual(inventory.errors, []);
  assert.deepEqual(inventory.endpoints.map(({ path, method }) => [path, method]), [
    ['/api/v4/a', '<dynamic>'], ['/api/v4/b', '<dynamic>'],
    ['/api/v4/items/<item_id>', 'GET'],
  ]);
  assert.equal(inventory.deferred.length, 1);
  assert.equal(inventory.deferred[0].expression, 'downloadUrl');
});

test('malformed source fails inventory instead of disappearing', () => {
  const inventory = scanApiSources({'frontend/src/api/broken.ts': 'const x = ;'});
  assert.ok(inventory.errors.length);
});

test('injected and namespace transports and wrapper callers are inventoried', () => {
  const inventory = scanApiSources({
    'frontend/src/api/sample.ts': `
      import * as transport from './transport';
      async function _api(url, opts) { return transport.request(url, opts); }
      _api('/api/v4/items', {method: 'POST'});
      transport.request('/api/v4/items');
      function boot(request) { request('/api/v4/boot/retry', {method: 'POST'}); }
      function irrelevant(controller) { controller.request(50); }
    `,
  });
  assert.deepEqual(inventory.endpoints.map(({ path, method }) => [path, method]), [
    ['/api/v4/items', 'POST'], ['/api/v4/items', 'GET'],
    ['/api/v4/boot/retry', 'POST'],
  ]);
  assert.deepEqual(inventory.deferred, []);
  assert.equal(inventory.forwarders.length, 1);
});

test('aliased arrow and named forwarders preserve fixed methods and bound options', () => {
  const inventory = scanApiSources({
    'frontend/src/api/example.ts': `
      import {request as send} from './transport';
      const write = (url) => send(url, {method: 'POST'});
      write('/api/v4/write');
      function remove(url) { return send(url, {method: 'DELETE'}); }
      remove('/api/v4/remove');
      const forward = (url, options) => send(url, {...options, parse: 'response'});
      forward('/api/v4/forward', {method: 'PATCH'});
      const relay = (url, options) => forward(url, options);
      relay('/api/v4/relay', {method: 'PUT'});
      const source = (url, options = {}) => send(url, {method: options.method || 'GET'});
      source('/api/v4/default');
      source('/api/v4/options', {method: 'POST'});
      source('/api/v4/spread', {method: 'PUT', ...{}});
      source('/api/v4/empty', {method: ''});
    `,
  });
  assert.deepEqual(inventory.errors, []);
  assert.deepEqual(inventory.deferred, []);
  assert.deepEqual(inventory.endpoints.map(({path, method}) => [path, method]), [
    ['/api/v4/write', 'POST'], ['/api/v4/remove', 'DELETE'],
    ['/api/v4/forward', 'PATCH'], ['/api/v4/relay', 'PUT'],
    ['/api/v4/default', 'GET'], ['/api/v4/options', 'POST'],
    ['/api/v4/spread', 'PUT'], ['/api/v4/empty', 'GET'],
  ]);
});

test('imported forwarders resolve by symbol without confusing same-named functions', () => {
  const inventory = scanApiSources({
    'frontend/src/api/owner.ts': `
      import {request} from './transport';
      export const send = (url, opts) => request(url, {method:'POST', ...opts});
    `,
    'frontend/src/features/page.ts': `
      import {send as write} from '../api/owner';
      write('/api/v4/imported', {method:'PATCH'});
      function send(url) { return String(url); }
      send('/api/v4/not-a-request');
    `,
  });
  assert.deepEqual(inventory.errors, []);
  assert.deepEqual(inventory.endpoints.map(({path, method}) => [path, method]), [
    ['/api/v4/imported', 'PATCH'],
  ]);
});


test('source census includes new clients and ignores deleted and generated runtime files', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'tofu-api-census-'));
  try {
    const sourceRoot = path.join(root, 'frontend/src/api');
    fs.mkdirSync(sourceRoot, { recursive: true });
    execFileSync('git', ['init', '-q'], {cwd: root});
    fs.writeFileSync(path.join(sourceRoot, 'deleted.ts'), "request('/api/v4/deleted');");
    fs.writeFileSync(path.join(sourceRoot, 'retained.ts'), "request('/api/v4/retained');");
    execFileSync('git', ['add', '.'], {cwd: root});
    fs.unlinkSync(path.join(sourceRoot, 'deleted.ts'));
    fs.writeFileSync(path.join(sourceRoot, 'new.ts'), "request('/api/v4/new');");
    fs.writeFileSync(path.join(sourceRoot, 'composed.generated.js'), "request('/api/v4/ignored');");
    const sources = collectApiSources(root);
    assert.deepEqual(Object.keys(sources).sort(), [
      'frontend/src/api/new.ts', 'frontend/src/api/retained.ts',
    ]);
    assert.deepEqual(scanApiSources(sources).endpoints.map((entry) => entry.path).sort(), [
      '/api/v4/new', '/api/v4/retained',
    ]);
  } finally {
    fs.rmSync(root, {recursive: true, force: true});
  }
});

test('recursive forwarding reports an audit failure without dropping known calls', () => {
  const inventory = scanApiSources({'frontend/src/api/retry.ts': `
    import {request} from './transport';
    function retry(url, again) { if (again) return retry(url, false); return request(url); }
    retry('/api/v4/recursive', true);
  `});
  assert.ok(inventory.errors.some((entry) => entry.reason.includes('Recursive HTTP forwarder')));
  assert.ok(inventory.endpoints.some((entry) => entry.path === '/api/v4/recursive'));
});


test('constant aliases of imported and namespace requests remain visible', () => {
  const inventory = scanApiSources({'frontend/src/api/aliases.ts': `
    import {request} from './transport';
    import * as http from './transport';
    const first = request;
    const second = http.request;
    first('/api/v4/one', {method:'PUT'});
    second('/api/v4/two', {method:'DELETE'});
    const owner = (url) => request(url, {method:'POST'});
    const third = owner;
    third('/api/v4/three');
  `});
  assert.deepEqual(inventory.endpoints.map(({path, method}) => [path, method]), [
    ['/api/v4/one', 'PUT'], ['/api/v4/two', 'DELETE'], ['/api/v4/three', 'POST'],
  ]);
  assert.deepEqual(inventory.errors, []);
});
