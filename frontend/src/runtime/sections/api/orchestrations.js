/* ===== migrated source: api/orchestrations.js ===== */
/* ═══════════════════════════════════════════════════════════════════════
   api/orchestrations.js — orchestration API compatibility facade

   Exposes the stable Api.orchestrations surface while delegating every HTTP
   route, verb, request body and projector-method contract to the canonical
   api/orchestration-endpoints.js registry.
   ═══════════════════════════════════════════════════════════════════════ */

(function (global) {
  'use strict';

  const Api = global.Api;
  // ``var ApiHttpResult`` is module-scoped after the classic runtime is
  // migrated into ESM; unlike a classic script it is not installed on
  // ``window``. Keep the facade on the lexical owner instead of reaching for
  // a global that cannot exist in the Vite graph.
  const httpResult = ApiHttpResult;
  const endpointRegistry = runtimeScope.ApiOrchestrationEndpoints;
  if (!Api || !Api.orchestrations || !httpResult || !endpointRegistry) {
    throw new Error(
      'api/orchestrations.js requires api.js, api/http-result.js and ' +
      'api/orchestration-endpoints.js');
  }
  const transport = endpointRegistry.createTransport({
    api: Api,
    httpResult: httpResult,
  });

  function _request(name, args, normalized) {
    return transport.request(name, args, normalized);
  }

  function _resultMethod(name) {
    return function () {
      return _request(name, Array.prototype.slice.call(arguments), true);
    };
  }

  function _directMethod(name) {
    return function () {
      return _request(name, Array.prototype.slice.call(arguments), false);
    };
  }

  async function _listResult() {
    const result = await _request('definition-list', [], true);
    const body = result.data;
    const items = body && Array.isArray(body.items)
      ? body.items : (Array.isArray(body) ? body : []);
    // Keep `ok` as the transport fact consumed by the full versioned
    // definition projector. Core consumers use `accepted`, which also rejects
    // 2xx logical failures and malformed bodies instead of mistaking them for
    // a successfully loaded empty catalogue.
    const accepted = result.ok === true && (Array.isArray(body)
      || !!body && typeof body === 'object' && body.ok !== false
        && Array.isArray(body.items));
    return Object.assign({}, result, {
      accepted: accepted,
      items: accepted ? items : [],
    });
  }

  // Only the two compatibility policies that cannot be expressed as one
  // endpoint descriptor live here. Every ordinary public method is derived
  // below from the endpoint registry, so adding a registered endpoint cannot
  // silently stop halfway before reaching Api.orchestrations.
  const orchestrations = {
    listResult: () => _listResult(),
    list: async () => {
      const result = await _listResult();
      return result.accepted ? result.items : [];
    },
    save: (id, definition, expectedUpdatedAt, writeContract) => _request(
      id ? 'definition-update' : 'definition-create',
      id
        ? [id, definition, expectedUpdatedAt, writeContract]
        : [definition],
      true
    ),
  };

  const contracts = endpointRegistry.contracts();
  const methodOwners = Object.create(null);
  function installMethod(endpoint, method, normalized) {
    if (method === 'list' || method === 'listResult' || method === 'save') return;
    const owner = methodOwners[method];
    if (owner && owner !== endpoint) {
      const error = new Error(
        'Orchestration API method ' + method + ' is owned by both '
        + owner + ' and ' + endpoint);
      error.name = 'OrchestrationEndpointFacadeError';
      throw error;
    }
    methodOwners[method] = endpoint;
    if (!orchestrations[method]) {
      orchestrations[method] = normalized
        ? _resultMethod(endpoint) : _directMethod(endpoint);
    }
  }
  Object.keys(contracts).forEach((endpoint) => {
    const contract = contracts[endpoint];
    // list/listResult have rolling-response compatibility above; save spans
    // the create + update endpoints and therefore has no one-endpoint wrapper.
    installMethod(endpoint, contract.resultMethod, true);
    // Install normalized methods first. When result/direct intentionally share
    // a public name (remove, runAbort, taskGet...), the status-preserving seam
    // wins exactly as it did in the hand-written facade.
    installMethod(endpoint, contract.directMethod, false);
  });

  Object.assign(Api.orchestrations, orchestrations);
})(typeof window !== 'undefined' ? window : globalThis);

