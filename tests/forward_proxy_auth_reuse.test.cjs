'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { installTofuForwardAuthReuse } = require('../scripts/forward_proxy_auth_reuse.cjs');

function fixture(validate = async request => request.cookies['code-server-session'].trim() === request.args.password) {
  let calls = 0;
  const http = { authenticated(request) { calls++; return validate(request); } };
  const original = http.authenticated;
  const args = { auth: 'password', password: 'valid' };
  const state = installTofuForwardAuthReuse(http, 15000);
  const request = (cookie = 'valid', overrides = {}) => ({
    args, originalUrl: '/proxy/15000/api/health', params: { port: '15000' },
    cookies: { 'code-server-session': cookie }, ...overrides,
  });
  return { http, original, args, state, request, calls: () => calls };
}

test('repeated valid requests reuse only the exact verified cookie', async () => {
  const f = fixture();
  assert.equal(await f.http.authenticated(f.request()), true);
  assert.equal(await f.http.authenticated(f.request(' valid ')), true);
  assert.equal(f.calls(), 1);
  assert.equal(await f.http.authenticated(f.request('invalid')), false);
  assert.equal(await f.http.authenticated(f.request('invalid')), false);
  assert.equal(f.calls(), 3);
});

test('password rotation invalidates immediately, including rotation back', async () => {
  const f = fixture();
  await f.http.authenticated(f.request());
  f.args.password = 'rotated';
  assert.equal(await f.http.authenticated(f.request()), false);
  assert.equal(await f.http.authenticated(f.request('rotated')), true);
  f.args.password = 'valid';
  assert.equal(await f.http.authenticated(f.request()), true);
  assert.equal(f.calls(), 4);
});

test('another configuration object cannot inherit an authentication decision', async () => {
  const f = fixture();
  await f.http.authenticated(f.request());
  const other = { auth: 'password', password: 'other' };
  assert.equal(await f.http.authenticated(f.request('valid', { args: other })), false);
  assert.equal(f.calls(), 2);
});

test('only the explicit forwarded route with plaintext configured credentials is reused', async () => {
  for (const overrides of [
    { originalUrl: '/proxy/15001/api/health' },
    { originalUrl: '/proxy/150000/api/health' },
    { originalUrl: '/login' },
    { params: { port: '15001' } },
    { args: { auth: 'none', password: 'valid' } },
    { args: { auth: 'password', password: 'valid', 'hashed-password': '$argon2id$hash' } },
    { args: { auth: 'password', password: 'valid', 'disable-proxy': true } },
  ]) {
    const f = fixture();
    await f.http.authenticated(f.request('valid', overrides));
    await f.http.authenticated(f.request('valid', overrides));
    assert.equal(f.calls(), 2, JSON.stringify(overrides));
  }
});

test('absproxy and websocket requests use the same current credential authority', async () => {
  const f = fixture();
  assert.equal(await f.http.authenticated(f.request()), true);
  assert.equal(await f.http.authenticated(f.request('valid', {
    originalUrl: '/absproxy/15000/api/push', headers: { upgrade: 'websocket' },
  })), true);
  assert.equal(f.calls(), 1);
});

test('concurrent identical verification is coalesced without delaying distinct cookies', async () => {
  const resolvers = [];
  const f = fixture(() => new Promise(resolve => resolvers.push(resolve)));
  const same = Array.from({ length: 128 }, () => f.http.authenticated(f.request()));
  const distinct = f.http.authenticated(f.request('different'));
  await Promise.resolve();
  assert.equal(f.calls(), 2);
  resolvers[1](false);
  assert.equal(await distinct, false);
  resolvers[0](true);
  assert.deepEqual(await Promise.all(same), Array(128).fill(true));
  assert.equal(await f.http.authenticated(f.request()), true);
  assert.equal(f.calls(), 2);
});

test('a late successful verifier cannot seed the changed configuration', async () => {
  let finish;
  const f = fixture(request => request.args.password === 'valid'
    ? new Promise(resolve => { finish = resolve; }) : Promise.resolve(false));
  const old = f.http.authenticated(f.request());
  await Promise.resolve();
  f.args.password = 'changed';
  finish(true);
  assert.equal(await old, true); // Same result as the already-started original verifier.
  assert.equal(await f.http.authenticated(f.request()), false);
  assert.equal(f.calls(), 2);
});

test('verification errors are preserved and never cached', async () => {
  const failure = new Error('verifier failed');
  const f = fixture(() => { throw failure; });
  await assert.rejects(f.http.authenticated(f.request()), error => error === failure);
  await assert.rejects(f.http.authenticated(f.request()), error => error === failure);
  assert.equal(f.calls(), 2);
});

test('missing and malformed cookies always reach the original verifier', async () => {
  const f = fixture(async () => false);
  for (const cookie of [undefined, null, '', 9, ['valid']]) {
    assert.equal(await f.http.authenticated(f.request('unused', {
      cookies: { 'code-server-session': cookie },
    })), false);
  }
  assert.equal(f.calls(), 5);
});

test('installation and rollback preserve ownership of the exported verifier', async () => {
  const f = fixture();
  assert.equal(installTofuForwardAuthReuse(f.http, 15000), f.state);
  f.state.uninstall();
  assert.equal(f.http.authenticated, f.original);
  const reinstalled = installTofuForwardAuthReuse(f.http, 15000);
  const replacement = async () => false;
  f.http.authenticated = replacement;
  reinstalled.uninstall();
  assert.equal(f.http.authenticated, replacement);
});

test('installation refuses an ambiguous target or missing original verifier', () => {
  for (const port of [undefined, '15000', 0, 65536]) {
    assert.throws(() => installTofuForwardAuthReuse({ authenticated() {} }, port), RangeError);
  }
  assert.throws(() => installTofuForwardAuthReuse({}, 15000), TypeError);
});
