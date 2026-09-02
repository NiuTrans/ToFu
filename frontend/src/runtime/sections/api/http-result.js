/* ===== migrated source: api/http-result.js ===== */
/* ═══════════════════════════════════════════════════════════════════════
   api/http-result.js — shared raw-Response result adapter

   Owns the only browser conversion from a fetch-like Response into the
   status-preserving `{ok, status, data}` envelope. `normalize` guarantees an
   envelope for domain transports; `adapt` preserves legacy direct JSON bodies
   while still converting a raw Response for rolling-client compatibility.
   ═══════════════════════════════════════════════════════════════════════ */

var ApiHttpResult = (function () {
  'use strict';

  function _responseLike(value) {
    return !!value && typeof value.json === 'function';
  }

  async function _result(value, passthrough) {
    let response;
    try {
      response = await value;
    } catch (error) {
      if (passthrough) throw error;
      const failure = {
        ok: false,
        status: Number(error && error.status || 0),
        data: {},
      };
      // Preserve the stable enumerable envelope while retaining the original
      // transport exception for diagnostics and presentation boundaries.
      Object.defineProperty(failure, 'cause', {
        value: error,
        enumerable: false,
      });
      return failure;
    }
    if (!_responseLike(response)) {
      return passthrough ? response : {
        ok: false,
        status: 0,
        data: {},
      };
    }
    const data = await response.json().catch(() => ({}));
    return {
      ok: !!response.ok,
      status: Number(response.status || 0),
      data: data && typeof data === 'object' ? data : {},
    };
  }

  function _error(value) {
    if (value == null || value === '') return null;
    if (typeof value === 'string') return value;
    if (typeof value !== 'object') return null;
    if (value.cause) return value.cause;
    const data = value.data;
    if (data && typeof data === 'object'
        && Object.prototype.hasOwnProperty.call(data, 'error')) {
      return data.error;
    }
    if (Object.prototype.hasOwnProperty.call(value, 'error')) {
      return value.error;
    }
    // Transport exceptions are already the canonical failure value. Keep
    // their identity so presentation boundaries can use name/message without
    // manufacturing a second wrapper shape.
    if (typeof value.message === 'string') return value;
    return null;
  }

  return Object.freeze({
    normalize: (value) => _result(value, false),
    adapt: (value) => _result(value, true),
    error: _error,
  });
})();

