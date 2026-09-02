/* ===== migrated source: api/orchestration-endpoint-transport.js ===== */
/* api/orchestration-endpoint-transport.js — request execution policy */
(function (global) {
  'use strict';

  function resolveClient() {
    const api = global.Api;
    return api && api.orchestrations ? api.orchestrations : null;
  }
  function _writeContractError(message) {
    const error = new Error(message);
    error.name = 'OrchestrationDefinitionWriteContractError';
    return error;
  }
  function _writeOptions(expectedUpdatedAt, contract, operation) {
    const options = { parse: 'response' };
    const declared = contract && typeof contract === 'object' ? contract : null;
    if (declared && Array.isArray(declared.operations)
        && declared.operations.indexOf(operation) === -1) {
      throw _writeContractError(
        'Definition write contract does not publish operation ' + operation);
    }
    if (!Number.isSafeInteger(expectedUpdatedAt) || expectedUpdatedAt < 0) {
      throw _writeContractError(
        'Definition write contract requires a version token');
    }
    const header = declared ? declared.preconditionHeader : 'If-Match';
    const syntax = declared ? declared.tokenSyntax : 'quoted-decimal';
    if (typeof header !== 'string' || !header
        || syntax !== 'quoted-decimal') {
      throw _writeContractError(
        'Unsupported definition write precondition contract');
    }
    options.headers = {};
    options.headers[header] = '"' + String(expectedUpdatedAt) + '"';
    return options;
  }
  function _responseOptions(normalized, extras) {
    const options = Object.assign({}, extras || {});
    if (normalized) options.parse = 'response';
    else options.onError = 'null';
    return options;
  }
  function _routeUrl(spec, args) {
    const mapping = spec.pathArgs || {};
    return spec.route.replace(
      /<(?:[^:<>]+:)?([^<>]+)>/g,
      function (_placeholder, field) {
        if (!Object.prototype.hasOwnProperty.call(mapping, field)) {
          const error = new Error(
            'Missing orchestration path argument mapping ' + field);
          error.name = 'OrchestrationEndpointTransportError';
          throw error;
        }
        const value = args[mapping[field]];
        if (value == null || value === '') {
          const error = new Error(
            'Missing orchestration path argument value ' + field);
          error.name = 'OrchestrationEndpointTransportError';
          throw error;
        }
        return encodeURIComponent(value);
      }
    );
  }
  function _queryFromContract(spec, args) {
    const query = {}, mapping = spec.queryArgs || {};
    Object.keys(mapping).forEach((field) => {
      const value = args[mapping[field]];
      query[field] = value == null || value === '' ? undefined : value;
    });
    return query;
  }
  function _bodyFromContract(spec, args) {
    if (Number.isInteger(spec.bodyArg)) return args[spec.bodyArg];
    const body = {}, mapping = spec.bodyArgs || {};
    const fields = Object.keys(mapping);
    if (!fields.length) return undefined;
    fields.forEach((field) => {
      body[field] = args[mapping[field]];
    });
    return body;
  }
  function _requestOptions(spec, args, normalized) {
    const hasQuery = spec.queryArgs && Object.keys(spec.queryArgs).length;
    const extras = hasQuery
      ? { query: _queryFromContract(spec, args) } : {};
    const declared = Number.isInteger(spec.requestOptionsArg)
      ? args[spec.requestOptionsArg] : null;
    if (declared && declared.signal) extras.signal = declared.signal;
    if (spec.writeOperation) {
      const options = Number.isInteger(spec.writeVersionArg)
        ? _writeOptions(
            args[spec.writeVersionArg],
            Number.isInteger(spec.writeContractArg)
              ? args[spec.writeContractArg] : null,
            spec.writeOperation)
        : { parse: 'response' };
      return Object.assign(options, extras);
    }
    return _responseOptions(normalized, extras);
  }
  function createEndpointTransport(options, endpointSpec) {
    options = options || {};
    const api = options.api;
    const httpResult = options.httpResult;
    if (!api || !httpResult || typeof httpResult.normalize !== 'function'
        || typeof endpointSpec !== 'function') {
      throw new Error(
        'orchestration endpoint transport requires Api, ApiHttpResult and '
        + 'an endpoint catalogue');
    }
    const verbs = {
      GET: api.get,
      POST: api.post,
      PUT: api.put,
      DELETE: api.del,
    };
    function request(name, args, normalized) {
      const spec = endpointSpec(name);
      const values = Array.isArray(args) ? args : [];
      if (!spec) {
        const error = new Error(
          'Unknown orchestration HTTP endpoint: ' + String(name || ''));
        error.name = 'OrchestrationEndpointTransportError';
        throw error;
      }
      const verb = verbs[spec.method];
      if (typeof verb !== 'function') {
        throw new Error(
          'Api does not implement orchestration HTTP verb ' + spec.method);
      }
      const url = _routeUrl(spec, values);
      const requestOptions = _requestOptions(spec, values, !!normalized);
      const response = spec.method === 'GET' || spec.method === 'DELETE'
        ? verb(url, requestOptions)
        : verb(url, _bodyFromContract(spec, values), requestOptions);
      return normalized ? httpResult.normalize(response) : response;
    }
    return Object.freeze({ request: request });
  }

  runtimeScope.ApiOrchestrationEndpointTransport = Object.freeze({
    create: createEndpointTransport,
    resolveClient: resolveClient,
  });
})(typeof window !== 'undefined' ? window : globalThis);
