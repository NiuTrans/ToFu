/* ===== migrated source: orchestration-flow-catalog.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-flow-catalog.js — shared chat flow picker catalogue

   Owns definition-list discovery, compact picker projection, freshness and
   single-flight reads for the desktop toolbar and mobile bottom sheet.
   Consumers never interpret HTTP results or maintain parallel caches.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationFlowCatalog(options) {
  options = options || {};
  var items = [];
  var loaded = false;
  var hasSnapshot = false;
  var lastFailure = null;
  var updatedAt = 0;
  var pending = null;
  var generation = 0;
  var maxAgeMs = Number(options.maxAgeMs);
  if (!Number.isFinite(maxAgeMs) || maxAgeMs < 0) maxAgeMs = 30000;

  function api() {
    if (typeof options.api === 'function') return options.api();
    if (options.api != null) return options.api;
    return typeof runtimeScope.resolveOrchestrationApiClient === 'function'
      ? runtimeScope.resolveOrchestrationApiClient() : null;
  }

  function now() {
    return typeof options.now === 'function' ? options.now() : Date.now();
  }

  function snapshot() {
    return Object.freeze(items.slice());
  }

  function project(values) {
    var seen = Object.create(null);
    return values.reduce(function (projected, value) {
      if (!value || typeof value !== 'object') return projected;
      var id = String(value.id == null ? '' : value.id).trim();
      if (!id || seen[id]) return projected;
      seen[id] = true;
      projected.push(Object.freeze({
        id: id,
        name: typeof value.name === 'string' ? value.name.trim() : '',
      }));
      return projected;
    }, []);
  }

  function status() {
    var state = lastFailure !== null
      ? (hasSnapshot ? 'stale' : 'failed')
      : (pending ? (hasSnapshot ? 'refreshing' : 'loading')
        : (loaded ? 'ready' : (hasSnapshot ? 'invalidated' : 'idle')));
    return Object.freeze({
      state: state,
      hasSnapshot: hasSnapshot,
      failure: lastFailure,
    });
  }

  function failureCause(value) {
    var cause = typeof ApiHttpResult !== 'undefined'
        && ApiHttpResult && typeof ApiHttpResult.error === 'function'
      ? ApiHttpResult.error(value) : null;
    if (cause == null && value && typeof value.message === 'string') {
      cause = value;
    }
    if (cause && typeof cause === 'object') {
      cause = Object.freeze(Object.assign({}, cause));
    }
    return cause;
  }

  function report(error) {
    lastFailure = failureCause(error);
    // Keep a distinct sentinel for malformed failures with no message so the
    // state remains failed without leaking the whole transport object.
    if (lastFailure === null) lastFailure = '';
    reportOrchestrationDiagnostic(options.onError, error);
  }

  function notifyChange(adopted) {
    if (typeof options.onChange !== 'function') return;
    try {
      options.onChange(adopted);
    } catch (error) {
      // A presentation observer cannot turn a successful catalogue read into
      // a transport failure. Keep those fault domains separate for debugging.
      reportOrchestrationDiagnostic(options.onObserverError, error);
    }
  }

  async function read(owner) {
    var client = api();
    try {
      var values = null;
      if (client && typeof client.listResult === 'function') {
        var result = await client.listResult();
        if (owner !== generation) return snapshot();
        if (!result || result.accepted !== true
            || !Array.isArray(result.items)) {
          report(result);
          return snapshot();
        }
        values = result.items;
      } else if (client && typeof client.list === 'function') {
        values = await client.list();
        if (owner !== generation) return snapshot();
        if (!Array.isArray(values)) {
          report(values);
          return snapshot();
        }
      } else {
        report(null);
        return snapshot();
      }
      items = project(values);
      loaded = true;
      hasSnapshot = true;
      lastFailure = null;
      updatedAt = now();
      var adopted = snapshot();
      notifyChange(adopted);
      return adopted;
    } catch (error) {
      if (owner === generation) report(error);
      return snapshot();
    }
  }

  async function refresh() {
    if (pending) return pending;
    var request = read(generation);
    pending = request;
    try { return await request; }
    finally { if (pending === request) pending = null; }
  }

  function load() {
    return loaded && now() - updatedAt <= maxAgeMs
      ? Promise.resolve(snapshot()) : refresh();
  }

  return Object.freeze({
    load: load,
    refresh: refresh,
    snapshot: snapshot,
    status: status,
    invalidate: function () {
      generation += 1;
      loaded = false;
      lastFailure = null;
      pending = null;
      return generation;
    },
  });
}


var _orchestrationFlowCatalog = createOrchestrationFlowCatalog({
  onError: function (error) {
    if (typeof console !== 'undefined' && console.warn) {
      console.warn('[Flow catalog] list failed:', error && error.message
        ? error.message : error);
    }
  },
  onChange: function (items) {
    if (typeof _reconcileActiveFlowCatalog === 'function') {
      _reconcileActiveFlowCatalog(items);
    }
    if (typeof _syncActiveFlowLabel === 'function') {
      _syncActiveFlowLabel();
    }
  },
  onObserverError: function (error) {
    if (typeof console !== 'undefined' && console.error) {
      console.error('[Flow catalog] change observer failed:', error);
    }
  },
});

