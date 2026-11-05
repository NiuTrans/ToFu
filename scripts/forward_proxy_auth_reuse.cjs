'use strict';

/** Exact successful-cookie reuse for an operator-owned code-server forwarder.
 * Entry: installTofuForwardAuthReuse(httpModule, port); no automatic installation.
 * The reviewed code-server authenticated() authority depends only on auth mode,
 * current configured credentials and sanitized session cookie. Only its costly
 * plaintext-password branch is adapted, only on the explicit forwarded port.
 * No password, session or authorization policy changes. Installation must pin
 * the upstream module implementation; see docs/PROXY_RUNTIME.md.
 */
function installTofuForwardAuthReuse(httpModule, port) {
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new RangeError('An explicit TCP port is required');
  }
  if (!httpModule || typeof httpModule.authenticated !== 'function') {
    throw new TypeError('The original authenticated verifier is required');
  }
  const key = Symbol.for('tofu.forwardAuthReuse.v1.' + port);
  if (httpModule[key]) return httpModule[key];
  const original = httpModule.authenticated;
  const generations = new WeakMap();
  const route = new RegExp('^/(?:absproxy|proxy)/' + port + '(?:/|\\?|$)');
  const state = { port, checks: 0, hits: 0, coalesced: 0 };

  async function authenticated(request) {
    const args = request && request.args;
    const rawCookie = request && request.cookies && request.cookies['code-server-session'];
    if (!args || args.auth !== 'password' || args['hashed-password'] || args['disable-proxy']
        || typeof args.password !== 'string' || !args.password
        || !request.params || request.params.port !== String(port)
        || typeof request.originalUrl !== 'string' || !route.test(request.originalUrl)
        || typeof rawCookie !== 'string' || !rawCookie.trim()) {
      return original.call(this, request);
    }
    const cookie = rawCookie.trim(); // Same normalization as code-server's sanitizeString.
    let generation = generations.get(args);
    if (!generation || generation.password !== args.password) {
      generation = { password: args.password, approved: new Set(), pending: new Map() };
      generations.set(args, generation);
    }
    if (generation.approved.has(cookie)) {
      state.hits++;
      return true;
    }
    if (generation.pending.has(cookie)) {
      state.coalesced++;
      return generation.pending.get(cookie);
    }
    state.checks++;
    const verification = Promise.resolve(original.call(this, request));
    generation.pending.set(cookie, verification);
    try {
      const valid = await verification;
      // A result belongs to the exact credential generation that started it.
      // Never let a late verifier seed a rotated or replaced authority.
      if (valid === true && generations.get(args) === generation
          && args.auth === 'password' && args.password === generation.password
          && !args['hashed-password'] && !args['disable-proxy']) {
        generation.approved.add(cookie);
      }
      return valid;
    } finally {
      generation.pending.delete(cookie);
    }
  }

  state.uninstall = () => {
    if (httpModule.authenticated === authenticated) httpModule.authenticated = original;
    if (httpModule[key] === state) delete httpModule[key];
  };
  Object.defineProperty(httpModule, key, { value: state, configurable: true });
  httpModule.authenticated = authenticated;
  return state;
}

module.exports = { installTofuForwardAuthReuse };
