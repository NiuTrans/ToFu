/* ===== migrated source: orchestration-node-editor.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-node-editor.js — node field and parameter mutations

   Owns the canonical Inspector-to-graph write seam. It normalizes typed
   values, omits empty optional parameters, coalesces text-edit history and
   requests presentation refreshes without depending on the DOM.

   MUST load before orchestration.js.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationNodeEditor(options) {
  options = options || {};

  function _findNode(id) {
    return typeof options.findNode === 'function' ? options.findNode(id) : null;
  }

  function _selectedNodeId() {
    return typeof options.selectedNodeId === 'function'
      ? options.selectedNodeId() : null;
  }

  function _targetNode(nodeId) {
    return _findNode(nodeId || _selectedNodeId());
  }

  function _historyGroup(node, key, coalesce) {
    return coalesce ? 'param:' + node.id + ':' + key : '';
  }

  function _changed(node, key, coalesce, renderInspector) {
    if (typeof options.markDirty === 'function') {
      options.markDirty(_historyGroup(node, key, coalesce));
    }
    if (typeof options.renderNodes === 'function') options.renderNodes();
    if (renderInspector && typeof options.renderInspector === 'function') {
      options.renderInspector();
    }
  }

  function setParamResult(nodeId, key, value, kind, coalesce) {
    var node = _targetNode(nodeId);
    if (!node || !key) return { ok: false, reason: !node
      ? 'missing-target' : 'missing-key' };

    if (key === 'name') {
      if (node.name === value) return { ok: true };
      node.name = value;
      _changed(node, key, coalesce, false);
      return { ok: true };
    }

    // A subflow role is its outward face, so it remains a node field rather
    // than leaking into the execution params object.
    if (key === 'role') {
      if (node.role === value) return { ok: true };
      node.role = value;
      _changed(node, key, coalesce, true);
      return { ok: true };
    }

    var spec = typeof options.fieldSpec === 'function'
      ? options.fieldSpec(node, key) : null;
    var normalized = normalizeOrchestrationFieldDraftValue(
      kind, value, spec, options.fieldValueContract);
    if (!normalized.ok) return normalized;

    var params = node.params && typeof node.params === 'object'
      && !Array.isArray(node.params) ? node.params : null;
    if (!normalized.present) {
      if (!params || !Object.prototype.hasOwnProperty.call(params, key)) {
        return { ok: true };
      }
      delete params[key];
    } else {
      if (params && Object.prototype.hasOwnProperty.call(params, key)
          && orchestrationFieldDraftValuesEqual(
            params[key], normalized.value)) return { ok: true };
      if (!params) {
        params = {};
        node.params = params;
      }
      params[key] = normalized.value;
    }
    _changed(node, key, coalesce, key === 'mode');
    return { ok: true };
  }

  function setParam(nodeId, key, value, kind, coalesce) {
    return setParamResult(nodeId, key, value, kind, coalesce).ok;
  }

  return { setParam: setParam, setParamResult: setParamResult };
}

