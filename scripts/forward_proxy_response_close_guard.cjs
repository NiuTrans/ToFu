'use strict';

/** Operator-installed http-proxy response cancellation adapter.
 * Entry: installTofuResponseCloseGuard(proxy, port); no automatic installation.
 * Dependencies: public Node HTTP/EventEmitter APIs and http-proxy's proxyReq event.
 * Scope and installation authority: docs/PROXY_RUNTIME.md.
 */
function installTofuResponseCloseGuard(proxy, port) {
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new RangeError('An explicit TCP port is required');
  }
  const key = Symbol.for('tofu.responseCloseGuard.v1.' + port);
  if (proxy[key]) return proxy[key];
  const state = { port, attached: 0, cancelled: 0 };
  function onProxyRequest(upstream, _request, response, options) {
    const target = options && options.target;
    if (!target || Number(target.port) !== port || !['0.0.0.0', '127.0.0.1'].includes(target.hostname)) return;
    state.attached++;
    let cleaned = false;
    function cleanup() {
      if (cleaned) return;
      cleaned = true;
      response.removeListener('close', cancelAbandonedResponse);
      response.removeListener('finish', cleanup);
      upstream.removeListener('close', cleanup);
      upstream.removeListener('error', cleanup);
    }
    function cancelAbandonedResponse() {
      cleanup();
      if (!response.writableFinished && !upstream.destroyed) {
        state.cancelled++;
        upstream.destroy();
      }
    }
    response.once('close', cancelAbandonedResponse);
    response.once('finish', cleanup);
    upstream.once('close', cleanup);
    upstream.once('error', cleanup);
    if (response.destroyed) cancelAbandonedResponse();
    else if (response.writableFinished) cleanup();
  }
  state.listener = onProxyRequest;
  Object.defineProperty(proxy, key, { value: state, configurable: true });
  proxy.on('proxyReq', onProxyRequest);
  return state;
}

module.exports = { installTofuResponseCloseGuard };
