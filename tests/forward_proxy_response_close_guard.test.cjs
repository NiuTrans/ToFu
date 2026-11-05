'use strict';
// Cancellation/cleanup contract for the opt-in forwarding-process adapter.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { installTofuResponseCloseGuard } = require('../scripts/forward_proxy_response_close_guard.cjs');

function fixture({ hostname = '127.0.0.1', port = '15000', closed = false, finished = false } = {}) {
  const proxy = new EventEmitter();
  const state = installTofuResponseCloseGuard(proxy, 15000);
  const upstream = new EventEmitter();
  upstream.destroyed = false;
  upstream.destroy = () => { upstream.destroyed = true; upstream.emit('close'); };
  // http-proxy owns the error response; the adapter must leave its listener alone.
  upstream.on('error', () => {});
  const response = new EventEmitter();
  response.destroyed = closed;
  response.writableFinished = finished;
  proxy.emit('proxyReq', upstream, {}, response, { target: { hostname, port } });
  return { proxy, state, upstream, response };
}

function assertClean({ upstream, response }) {
  assert.equal(response.listenerCount('close'), 0);
  assert.equal(response.listenerCount('finish'), 0);
  assert.equal(upstream.listenerCount('close'), 0);
  assert.equal(upstream.listenerCount('error'), 1);
}

test('downstream abandonment cancels the upstream exactly once', () => {
  const f = fixture();
  f.response.destroyed = true;
  f.response.emit('close');
  f.response.emit('close');
  assert.equal(f.upstream.destroyed, true);
  assert.equal(f.state.cancelled, 1);
  assertClean(f);
});

test('downstream closed before proxyReq also cancels the upstream', () => {
  const f = fixture({ closed: true });
  assert.equal(f.upstream.destroyed, true);
  assert.equal(f.state.cancelled, 1);
  assertClean(f);
});

test('slow active responses keep their upstream until actual disconnect', () => {
  const f = fixture();
  f.response.emit('drain');
  assert.equal(f.upstream.destroyed, false);
  assert.equal(f.state.cancelled, 0);
  f.response.emit('close');
  assert.equal(f.upstream.destroyed, true);
  assertClean(f);
});

test('successful finish removes cancellation before downstream close', () => {
  const f = fixture();
  f.response.writableFinished = true;
  f.response.emit('finish');
  f.response.emit('close');
  assert.equal(f.upstream.destroyed, false);
  assert.equal(f.state.cancelled, 0);
  assertClean(f);
});

test('a response already finished is immediately detached', () => {
  const f = fixture({ closed: true, finished: true });
  assert.equal(f.upstream.destroyed, false);
  assert.equal(f.state.cancelled, 0);
  assertClean(f);
});

for (const event of ['close', 'error']) {
  test(`upstream ${event} removes only adapter listeners`, () => {
    const f = fixture();
    f.upstream.emit(event, new Error('fixture upstream failure'));
    f.response.emit('close');
    assert.equal(f.state.cancelled, 0);
    assertClean(f);
  });
}

test('another port or non-loopback host is untouched', () => {
  for (const target of [{ port: '15001' }, { hostname: 'example.invalid' }]) {
    const f = fixture(target);
    assert.equal(f.state.attached, 0);
    f.response.emit('close');
    assert.equal(f.upstream.destroyed, false);
    assertClean(f);
  }
});

test('installation is idempotent and can be removed without other listeners', () => {
  const f = fixture({ hostname: '0.0.0.0' });
  assert.equal(installTofuResponseCloseGuard(f.proxy, 15000), f.state);
  assert.equal(f.proxy.listenerCount('proxyReq'), 1);
  f.proxy.removeListener('proxyReq', f.state.listener);
  delete f.proxy[Symbol.for('tofu.responseCloseGuard.v1.15000')];
  assert.equal(f.proxy.listenerCount('proxyReq'), 0);
  f.response.emit('close');
  assertClean(f);
});

test('installation requires an explicit valid port', () => {
  for (const port of [undefined, null, '15000', 0, 65536]) {
    assert.throws(() => installTofuResponseCloseGuard(new EventEmitter(), port), RangeError);
  }
});
