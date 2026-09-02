/* ===== migrated source: api/orchestration-endpoints.js ===== */
/* api/orchestration-endpoints.js — orchestration client endpoint registry
   Combines the backend-generated HTTP catalogue with browser response and
   method contracts. Request projection and execution stay in the shared
   transport; this module only joins registries by endpoint ID.
   ═══════════════════════════════════════════════════════════════════════ */
(function (global) {
  'use strict';
  const _HTTP = runtimeScope.ApiOrchestrationHttpContract;
  const _RESPONSES = runtimeScope.ApiOrchestrationResponseContracts;
  const _METHODS = runtimeScope.ApiOrchestrationClientMethods;
  const _TRANSPORT = runtimeScope.ApiOrchestrationEndpointTransport;
  if (!_HTTP || typeof _HTTP.contract !== 'function'
      || !_RESPONSES || typeof _RESPONSES.contract !== 'function'
      || !_METHODS || typeof _METHODS.contract !== 'function'
      || !_TRANSPORT || typeof _TRANSPORT.create !== 'function') {
    throw new Error(
      'api/orchestration-endpoints.js requires the generated HTTP contract, '
      + 'response contracts, client methods and endpoint transport');
  }
  function _endpoint(name) {
    const http = _HTTP.contract(name);
    if (!http) {
      throw new Error('Unknown orchestration HTTP endpoint: ' + name);
    }
    const response = _RESPONSES.contract(http.responseContract);
    if (!response) {
      throw new Error(
        'Unknown orchestration response contract: ' + http.responseContract);
    }
    const methods = _METHODS.contract(name);
    if (!methods) {
      throw new Error(
        'Unknown orchestration client method contract: ' + name);
    }
    return Object.freeze({
      route: http.route, method: http.method,
      pathArgs: http.pathArgs || null,
      queryArgs: http.queryArgs || null, bodyArgs: http.bodyArgs || null,
      bodyArg: Number.isInteger(http.bodyArg) ? http.bodyArg : null,
      requestOptionsArg: Number.isInteger(http.requestOptionsArg)
        ? http.requestOptionsArg : null,
      writeOperation: http.writeOperation || '',
      writeVersionArg: Number.isInteger(http.writeVersionArg)
        ? http.writeVersionArg : null,
      writeContractArg: Number.isInteger(http.writeContractArg)
        ? http.writeContractArg : null,
      resultMethod: methods.resultMethod,
      directMethod: methods.directMethod,
      optionName: response.optionName,
      responseContract: response.name,
      responseRequiredFields: response.requiredFields,
    });
  }
  const _HTTP_CONTRACTS = _HTTP.contracts();
  const _METHOD_CONTRACTS = _METHODS.contracts();
  const httpNames = Object.keys(_HTTP_CONTRACTS).sort();
  const methodNames = Object.keys(_METHOD_CONTRACTS).sort();
  if (JSON.stringify(httpNames) !== JSON.stringify(methodNames)) {
    throw new Error(
      'Orchestration HTTP/client method contract coverage mismatch');
  }
  const endpoints = {};
  httpNames.forEach((name) => { endpoints[name] = _endpoint(name); });
  const _ENDPOINTS = Object.freeze(endpoints);
  function _contract(name, spec) {
    if (!spec) return null;
    return Object.freeze({
      name: name,
      method: spec.method,
      route: spec.route,
      resultMethod: spec.resultMethod,
      directMethod: spec.directMethod,
      optionName: spec.optionName,
      responseContract: spec.responseContract,
      responseRequiredFields: Object.freeze(
        Array.prototype.slice.call(spec.responseRequiredFields || [])),
      pathArgs: Object.freeze(Object.assign({}, spec.pathArgs || {})),
      queryArgs: Object.freeze(Object.assign({}, spec.queryArgs || {})),
      bodyArgs: Object.freeze(Object.assign({}, spec.bodyArgs || {})),
      bodyArg: spec.bodyArg,
      requestOptionsArg: spec.requestOptionsArg,
      writeOperation: spec.writeOperation,
      writeVersionArg: spec.writeVersionArg,
      writeContractArg: spec.writeContractArg,
    });
  }
  const contracts = {};
  httpNames.forEach((name) => {
    contracts[name] = _contract(name, _ENDPOINTS[name]);
  });
  const _CONTRACTS = Object.freeze(contracts);
  function endpointContract(name) {
    return _CONTRACTS[name] || null;
  }
  function endpointContracts() { return _CONTRACTS; }
  function createEndpointTransport(options) {
    return _TRANSPORT.create(options, (name) => _ENDPOINTS[name] || null);
  }
  runtimeScope.ApiOrchestrationEndpoints = Object.freeze({
    contract: endpointContract,
    contracts: endpointContracts,
    createTransport: createEndpointTransport,
    resolveClient: _TRANSPORT.resolveClient,
  });
  runtimeScope.resolveOrchestrationApiClient = _TRANSPORT.resolveClient;
})(typeof window !== 'undefined' ? window : globalThis);

