/* ===== migrated source: orchestration-io-tools.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-io-tools.js — pure Typed-I/O contract consumer

   Owns backend ioContract adoption, defaults, caps, presets and immutable
   port edits. It has no DOM or graph-state dependency; Inspector rendering
   HTML projection lives in orchestration-io-presentation.js; mutations and
   event binding live in orchestration-io.js.

   MUST load before orchestration-io.js.
   ═══════════════════════════════════════════════════════════════════ */


function createOrchestrationIoTools(initialContract) {
  var contract = null;
  var fallbackCodes = {
    maxPorts: 'io.side.max_ports', missingPort: 'io.port.missing',
    missingPortName: 'io.port.name.required', duplicatePortName:
      'io.port.name.duplicate', missingPreset: 'io.preset.missing',
  };

  function failureCode(name) {
    var published = contract && contract.failureCodes;
    return published && published[name] || fallbackCodes[name] || '';
  }

  function reject(name, reason, details) {
    return Object.assign({ ok: false, changed: false,
      code: failureCode(name), reason: reason }, details || {});
  }

  function clone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function setContract(next) {
    if (!next || !Array.isArray(next.types) || !next.types.length) return false;
    contract = clone(next);
    return true;
  }

  function getContract() { return contract ? clone(contract) : null; }

  function types() { return contract ? contract.types.slice() : []; }

  function defaultOutput() {
    var value = contract && contract.defaultOutput;
    return { name: value && value.name || 'text',
      type: value && value.type || 'text' };
  }

  function maxPorts() { var value = contract && Number(contract.maxPorts);
    return value > 0 ? value : null; }

  function portNameRules() { var value = contract && contract.portName;
    return value && typeof value === 'object' ? clone(value) : {}; }

  function startRef() { return contract && contract.startRef || 'start'; }

  function nodeInputs(node) { var io = node && node.params && node.params.io;
    return io && Array.isArray(io.inputs) ? io.inputs : []; }

  function nodeOutputs(node) { var io = node && node.params && node.params.io;
    if (io && Array.isArray(io.outputs) && io.outputs.length) return io.outputs;
    return [defaultOutput()]; }

  function outputRef(nodeId, outputs, port) { var implicit = defaultOutput();
    return port.name === implicit.name && outputs.length === 1
      ? nodeId : nodeId + '.' + port.name; }

  function nextPortName(ports, side) {
    var stem = side === 'outputs' ? 'out' : 'in';
    var used = {}, index = 1;
    ports.forEach(function (port) { used[port && port.name] = true; });
    while (used[stem + index]) index += 1;
    return stem + index;
  }

  function addPort(io, side) {
    var next = clone(io || {});
    var ports = Array.isArray(next[side]) ? next[side].slice() : [];
    var cap = maxPorts();
    if (cap !== null && ports.length >= cap) {
      return reject('maxPorts', 'max-ports', { maxPorts: cap, io: next });
    }
    var fallback = defaultOutput();
    ports.push({ name: nextPortName(ports, side), type: fallback.type });
    next[side] = ports;
    return { ok: true, changed: true, reason: '',
      io: next, index: ports.length - 1 };
  }

  function removePort(io, side, index) {
    var next = clone(io || {});
    if (!Array.isArray(next[side]) || index < 0 || index >= next[side].length) {
      return reject('missingPort', 'missing-port', { io: next });
    }
    next[side].splice(index, 1);
    if (!next[side].length) delete next[side];
    return { ok: true, changed: true, reason: '',
      io: Object.keys(next).length ? next : null };
  }

  function setPort(io, side, index, key, value) {
    var next = clone(io || {});
    if (!Array.isArray(next[side]) || !next[side][index]) {
      return reject('missingPort', 'missing-port', { io: next });
    }
    if (key === 'name') {
      var rules = portNameRules();
      if (rules.required && (typeof value !== 'string' || !value.trim())) {
        return reject('missingPortName', 'missing-port-name', { io: next });
      }
      var duplicate = rules.uniqueWithinSide && next[side].some(
        function (port, candidateIndex) {
          return candidateIndex !== index && port && port.name === value;
        }
      );
      if (duplicate) {
        return reject('duplicatePortName', 'duplicate-port-name', { io: next });
      }
    }
    var port = next[side][index];
    if (key === 'from' && !value) {
      if (!Object.prototype.hasOwnProperty.call(port, 'from')) {
        return { ok: true, changed: false, reason: '', io: next };
      }
      delete port.from;
    } else {
      if (port[key] === value) {
        return { ok: true, changed: false, reason: '', io: next };
      }
      port[key] = value;
    }
    return { ok: true, changed: true, reason: '', io: next };
  }

  function preset(name) { var spec = contract
    && contract.presets && contract.presets[name];
    return spec ? clone(spec) : null; }

  function applyPreset(io, name) {
    var spec = preset(name);
    if (!spec) return reject('missingPreset', 'missing-preset', {
      io: clone(io || {}),
    });
    var outputs = Array.isArray(spec.outputs) ? spec.outputs : [];
    var cap = maxPorts();
    if (cap !== null && outputs.length > cap) {
      return reject('maxPorts', 'max-ports',
        { maxPorts: cap, io: clone(io || {}) });
    }
    var next = clone(io || {});
    if (JSON.stringify(next.outputs || []) === JSON.stringify(outputs)) {
      return { ok: true, changed: false, reason: '', io: next };
    }
    next.outputs = clone(outputs);
    return { ok: true, changed: true, reason: '', io: next };
  }

  setContract(initialContract);
  return {
    setContract: setContract,
    getContract: getContract,
    types: types,
    defaultOutput: defaultOutput,
    maxPorts: maxPorts,
    portNameRules: portNameRules,
    failureCode: failureCode,
    startRef: startRef,
    nodeInputs: nodeInputs,
    nodeOutputs: nodeOutputs,
    outputRef: outputRef,
    addPort: addPort,
    removePort: removePort,
    setPort: setPort,
    preset: preset,
    applyPreset: applyPreset,
  };
}

