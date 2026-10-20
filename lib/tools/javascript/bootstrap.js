/* Retained guest capability bridge. No host objects cross the WASM boundary.
 * Python owns admission and executes queued calls after the async body yields.
 * Receipt references use an internal WeakMap and a null-prototype value codec.
 */
(() => {
  'use strict';
  const bridge = globalThis.__tofu_rpc;
  delete globalThis.__tofu_rpc;
  const parse = JSON.parse;
  const stringify = JSON.stringify;
  const keys = Object.keys;
  const hasOwn = Function.prototype.call.bind(Object.prototype.hasOwnProperty);
  const create = Object.create;
  const getPrototypeOf = Object.getPrototypeOf;
  const objectPrototype = Object.prototype;
  const isArray = Array.isArray;
  const isFinite = Number.isFinite;
  const isSafeInteger = Number.isSafeInteger;
  const integerToken = Function.prototype.call.bind(RegExp.prototype.test, /^-?[0-9]+$/);
  const asString = String;
  const asBigInt = BigInt;
  const defineProperty = Object.defineProperty;
  const NativePromise = Promise;
  const NativeTypeError = TypeError;
  const receipts = new WeakMap();
  const weakGet = Function.prototype.call.bind(WeakMap.prototype.get);
  const weakSet = Function.prototype.call.bind(WeakMap.prototype.set);
  const pending = new Map();
  const pendingGet = pending.get.bind(pending);
  const pendingSet = pending.set.bind(pending);
  const pendingDelete = pending.delete.bind(pending);
  const descriptor = {writable:false, configurable:false, enumerable:false};

  function wire(tag) {
    const result = create(null);
    result.t = tag;
    return result;
  }
  function pack(value, ancestors = [], depth = 0, allowReceipt = true, integerArguments = false, plainValue = false) {
    if (depth > 32) throw new RangeError('Returned value nesting exceeds 32');
    if (value === null || typeof value !== 'object') {
      const result = wire(integerArguments && typeof value === 'bigint' ? 'i' : 'p');
      result.v = typeof value === 'bigint' ? asString(value)
        : (typeof value === 'number' ? (isFinite(value) ? value : null)
        : (typeof value === 'string' || typeof value === 'boolean' ? value : null));
      return result;
    }
    for (let i = 0; i < ancestors.length; i++) {
      if (ancestors[i] === value) throw new TypeError('Returned value contains a cycle');
    }
    const token = allowReceipt && !plainValue ? weakGet(receipts, value) : undefined;
    if (token !== undefined) {
      const result = wire('r');
      result.id = token;
      result.v = pack(value, ancestors, depth, false, true, plainValue);
      return result;
    }
    const array = isArray(value);
    // Enumerating exotic objects silently discards Date/Map/Error state.
    // Do not inspect constructor/toStringTag or call user-defined toJSON.
    if (!array) {
      const prototype = getPrototypeOf(value);
      if (prototype !== null && prototype !== objectPrototype) {
        throw new NativeTypeError('Unsupported object kind; convert to plain JSON data first '
          + '(Date: toISOString(); Map/Set: arrays; Error: {name, message, stack})');
      }
    }
    const result = wire(array ? 'a' : 'o');
    result.v = create(null);
    const next = create(null);
    next.length = ancestors.length + 1;
    for (let i = 0; i < ancestors.length; i++) next[i] = ancestors[i];
    next[ancestors.length] = value;
    if (array) {
      result.n = value.length;
      for (let i = 0; i < value.length; i++) {
        result.v[i] = pack(value[i], next, depth + 1, true, integerArguments, plainValue);
      }
    } else {
      const names = keys(value);
      for (let i = 0; i < names.length; i++) {
        const key = names[i];
        const child = value[key];
        if (child !== undefined && typeof child !== 'function' && typeof child !== 'symbol') {
          result.v[key] = pack(child, next, depth + 1, true, integerArguments, plainValue);
        }
      }
    }
    return result;
  }
  function hostValue(packet) {
    for (let i = 0; i < packet.integers.length; i++) {
      const path = packet.integers[i];
      let parent = packet;
      let key = 'value';
      for (let j = 0; j < path.length; j++) {
        parent = parent[key];
        key = path[j];
      }
      defineProperty(parent, key, {
        value:asBigInt(parent[key]), writable:true, configurable:true, enumerable:true,
      });
    }
    return packet.value;
  }
  function rpc(op, args) {
    const request = create(null);
    request.op = op;
    request.args = pack(args, [], 0, true, op !== 'observe' && op !== 'keep', op === 'keep');
    return hostValue(parse(bridge(stringify(request))));
  }
  function schedule(op, args) {
    return new NativePromise((resolve, reject) => {
      const id = rpc(op, args);
      pendingSet(id, {resolve, reject});
    });
  }
  function mark(receipt, token) {
    if (receipt && typeof receipt === 'object') weakSet(receipts, receipt, token);
  }
  function resolve(id, text) {
    const slot = pendingGet(id);
    if (!slot) throw new Error('Unknown scheduled tool request');
    const packet = parse(text);
    hostValue(packet);
    if (packet.receipt && packet.batch) {
      for (let i = 0; i < packet.value.length; i++) mark(packet.value[i], packet.tokens[i]);
    } else if (packet.receipt) {
      mark(packet.value, packet.tokens[0]);
    }
    pendingDelete(id);
    slot.resolve(packet.value);
  }
  // Raw artifact content is JSON text, unlike already-decoded invoke values.
  // Reviver source text recovers integer precision before Number rounding.
  // This is guest computation only: no RPC, provenance or coverage inference.
  function parseJSON(text) {
    if (typeof text !== 'string') throw new TypeError('tools.parseJSON expects JSON text');
    return parse(text, (_key, value, context) => {
      if (typeof value !== 'number' || isSafeInteger(value)) return value;
      if (!context || typeof context.source !== 'string') {
        throw new TypeError('JSON parser did not supply numeric source text');
      }
      return integerToken(context.source) ? asBigInt(context.source) : value;
    });
  }
  const helpers = {
    parseJSON,

    call: (...args) => schedule('call', args),
    callMany: (calls, execution = 'auto') => schedule('batch', [calls, execution]),
    parallel: calls => schedule('batch', [calls, 'parallel']),
    value: async receipt => rpc('value', [await receipt]),
    invoke: (name, args = {}) => schedule('invoke', [name, args]),
    result: (selector, args = {}) => rpc('result', [selector, args]),
    keep: value => rpc('keep', [value]),
    inputs: (query = {}) => rpc('inputs', [query]),
    observe: (reason, data) => rpc('observe', [reason, data]),
  };
  // Property calls share invoke's complete-evidence gate and host authority.
  // A null prototype prevents inherited names becoming accidental helpers.
  // then must stay absent so Promise assimilation never schedules a tool.
  const tools = new Proxy(Object.freeze(Object.assign(create(null), helpers)), {
    get: (target, name) => {
      if (typeof name !== 'string' || name === 'then') return undefined;
      if (hasOwn(target, name)) return target[name];
      return (args = {}) => schedule('invoke', [name, args]);
    },
  });
  const catalog = {search: (...args) => rpc('search', args)};
  Object.defineProperties(globalThis, {
    tools: {...descriptor, value:Object.freeze(tools)},
    catalog: {...descriptor, value:Object.freeze(catalog)},
  });
  return {resolve, pack: value => stringify(value === undefined ? wire('u') : pack(value))};
})();
