/* ===== migrated source: orchestration-contract-loader.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-contract-loader.js — status-aware contract transport

   Owns single-flight reads, retry state and transport diagnostics. Contract
   normalization/application stays
   in orchestration-contract.js.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationContractLoader(options) {
  options = options || {};
  var flights = createOrchestrationSingleFlight();
  var isSettled = false;
  var lastError = null;

  function _snapshot() {
    return typeof options.snapshot === 'function' ? options.snapshot() : {};
  }

  function _failure(read, endpoint) {
    var reason = read.notFound ? 'not-found'
      : read.unsupportedFormat ? 'unsupported-format'
        : read.malformed ? 'malformed-response'
          : read.retryable ? 'temporarily-unavailable' : 'request-rejected';
    return {
      name: 'OrchestrationContractReadError',
      message: read.error || 'Authoring contract read failed',
      endpoint: endpoint,
      status: Number(read.status || 0),
      reason: reason,
      retryable: !!read.retryable,
      missingFields: Array.isArray(read.missingFields)
        ? read.missingFields.slice() : [],
    };
  }

  function load() {
    if (typeof options.ready === 'function' && options.ready()) {
      return Promise.resolve(_snapshot());
    }
    var api = typeof options.api === 'function'
      ? options.api() : (options.api || null);
    var requests = createOrchestrationEndpointRequestClient({
      api: function () { return api; },
      normalizeRead: options.normalizeRead,
    });
    var hasPrimary = requests.available('authoring-contract');
    if (!hasPrimary) {
      isSettled = false;
      lastError = {
        name: 'OrchestrationContractReadError',
        message: 'Authoring contract client is unavailable',
        endpoint: 'authoring-contract', status: 0,
        reason: 'client-contract-missing', retryable: false,
        missingFields: [],
      };
      reportOrchestrationDiagnostic(options.onError, lastError);
      return Promise.resolve(_snapshot());
    }

    return flights.share('contract', function () {
      return Promise.resolve().then(async function () {
        var read = await requests.request('authoring-contract');
        if (read.ok) return read.contract;
        throw _failure(read, 'authoring-contract');
      }).then(function (contract) {
        isSettled = true;
        lastError = null;
        return contract && typeof options.apply === 'function'
          ? options.apply(contract) : _snapshot();
      }).catch(function (error) {
        isSettled = false;
        lastError = error && typeof error === 'object' ? error : {
          name: 'OrchestrationContractReadError',
          message: String(error || 'Authoring contract read failed'),
          endpoint: '', status: 0,
          reason: 'temporarily-unavailable', retryable: true, missingFields: [],
        };
        reportOrchestrationDiagnostic(options.onError, lastError);
        return _snapshot();
      });
    });
  }

  return {
    load: load,
    settled: function () { return isSettled; },
    error: function () { return lastError; },
  };
}
