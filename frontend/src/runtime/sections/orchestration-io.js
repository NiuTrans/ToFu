/* ===== migrated source: orchestration-io.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-io.js — Typed-I/O Inspector editor

   Applies immutable I/O edits and binds the Inspector controls. Pure option
   and HTML projection lives in orchestration-io-presentation.js; contract
   adoption and immutable port operations live in orchestration-io-tools.js.

   MUST load after orchestration-io-tools.js and before orchestration.js.
   ═══════════════════════════════════════════════════════════════════ */


function createOrchestrationIoEditor(options) {
  options = options || {};
  var ioTools = options.ioTools;
  var translate = options.translate || function (key) { return key; };
  var validity = options.fieldValidity
    || createOrchestrationFieldValidity();
  var presentation = options.presentation
    || createOrchestrationIoPresentation(options);

  function _nodes() {
    return typeof options.nodes === 'function' ? options.nodes() : [];
  }

  function _find(id) {
    if (typeof options.findNode === 'function') return options.findNode(id);
    return _nodes().filter(function (node) { return node.id === id; })[0] || null;
  }

  function _selectedNode() {
    return typeof options.selectedNode === 'function'
      ? options.selectedNode() : null;
  }

  function _targetNode(nodeId) {
    return nodeId ? _find(nodeId) : _selectedNode();
  }

  function _notifyChange(renderInspector, renderNodes, historyGroup) {
    if (typeof options.onChange === 'function') {
      options.onChange({
        renderInspector: !!renderInspector,
        renderNodes: !!renderNodes,
        historyGroup: historyGroup || '',
      });
    }
  }

  function _reject(result) {
    var maxPortsCode = typeof ioTools.failureCode === 'function'
      ? ioTools.failureCode('maxPorts') : '';
    if (result && (result.code
      ? result.code === maxPortsCode : result.reason === 'max-ports')
        && typeof options.toast === 'function') {
      options.toast(translate('orch.io.maxPorts', { n: result.maxPorts }), true);
    }
    return false;
  }

  function _adoptResult(node, result, renderInspector, renderNodes,
                        historyGroup) {
    if (!result || !result.ok) {
      _reject(result);
      return result || { ok: false, reason: 'invalid-result' };
    }
    var changed = result.changed !== false;
    if (changed) {
      node.params = node.params || {};
      if (result.io) node.params.io = result.io;
      else delete node.params.io;
      _notifyChange(renderInspector, renderNodes, historyGroup);
    }
    return { ok: true, changed: changed,
      code: result.code || '', reason: result.reason || '' };
  }

  function _adopt(node, result, renderInspector, renderNodes, historyGroup) {
    return _adoptResult(
      node, result, renderInspector, renderNodes, historyGroup).ok;
  }

  function add(side, nodeId) {
    var node = _targetNode(nodeId);
    return node
      ? _adopt(node, ioTools.addPort(node.params && node.params.io, side), true, true)
      : false;
  }

  function remove(side, index, nodeId) {
    var node = _targetNode(nodeId);
    return node
      ? _adopt(node, ioTools.removePort(node.params && node.params.io, side, index), true, true)
      : false;
  }

  function setResult(side, index, key, value, nodeId, coalesce) {
    var node = _targetNode(nodeId);
    return node
      ? _adoptResult(node, ioTools.setPort(
          node.params && node.params.io, side, index, key, value),
          false, key !== 'name', coalesce
            ? 'io:' + node.id + ':' + side + ':' + index + ':' + key : '')
      : { ok: false, reason: 'missing-target' };
  }

  function set(side, index, key, value, nodeId, coalesce) {
    return setResult(side, index, key, value, nodeId, coalesce).ok;
  }

  function bindInput(targetId, index, ref) {
    var node = _find(targetId);
    return node
      ? _adopt(node, ioTools.setPort(node.params && node.params.io,
          'inputs', index, 'from', ref),
          false, true)
      : false;
  }

  function applyPreset(name, nodeId) {
    var node = _targetNode(nodeId);
    return node
      ? _adopt(node, ioTools.applyPreset(node.params && node.params.io, name), true, true)
      : false;
  }

  function bindSection(element, nodeId) {
    if (!element || typeof element.querySelectorAll !== 'function') return;
    Array.prototype.forEach.call(
      element.querySelectorAll('[data-orch-io-action]'), function (control) {
        var action = control.getAttribute('data-orch-io-action');
        var side = control.getAttribute('data-orch-io-side') || '';
        var index = Number(control.getAttribute('data-orch-io-index'));
        if (action === 'set') {
          var eventName = control.tagName === 'SELECT' ? 'change' : 'input';
          control.addEventListener(eventName, function () {
            var result = setResult(side, index,
                control.getAttribute('data-orch-io-key') || '', control.value,
                nodeId, eventName === 'input');
            validity.setLocal(control, result.ok, '',
              String(result.code || result.reason || ''));
          });
        } else if (action === 'add') {
          control.addEventListener('click', function () { add(side, nodeId); });
        } else if (action === 'remove') {
          control.addEventListener('click', function () { remove(side, index, nodeId); });
        } else if (action === 'preset') {
          control.addEventListener('click', function () {
            applyPreset(control.getAttribute('data-orch-io-preset') || '', nodeId);
          });
        }
      }
    );
  }

  return {
    sectionBody: presentation.sectionBody,
    upstreamIds: presentation.upstreamIds,
    fromOptions: presentation.fromOptions,
    bindSection: bindSection,
    add: add,
    remove: remove,
    set: set,
    setResult: setResult,
    bindInput: bindInput,
    applyPreset: applyPreset,
  };
}

