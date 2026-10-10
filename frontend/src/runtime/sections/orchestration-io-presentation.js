/* ===== migrated source: orchestration-io-presentation.js ===== */
/* Pure Typed-I/O Inspector option and HTML projection. */

function createOrchestrationIoPresentation(options) {
  options = options || {};
  var ioTools = options.ioTools;
  var escape = options.escape || function (value) { return String(value || ''); };
  var translate = options.translate || function (key) { return key; };
  var icons = options.icons || {};

  function _nodes() {
    return typeof options.nodes === 'function' ? options.nodes() : [];
  }

  function _edges() {
    return typeof options.edges === 'function' ? options.edges() : [];
  }

  function _find(id) {
    if (typeof options.findNode === 'function') return options.findNode(id);
    return _nodes().filter(function (node) { return node.id === id; })[0] || null;
  }

  function _label(node) {
    return typeof options.nodeLabel === 'function'
      ? options.nodeLabel(node) : (node.name || node.id);
  }

  function upstreamIds(id) {
    var seen = {};
    var stack = [id];
    while (stack.length) {
      var current = stack.pop();
      _edges().forEach(function (edge) {
        if (edge.to === current && !seen[edge.from]) {
          seen[edge.from] = true;
          stack.push(edge.from);
        }
      });
    }
    return seen;
  }

  function fromOptions(self, currentRef) {
    var upstream = upstreamIds(self.id);
    var startRef = ioTools.startRef();
    var choices = [
      ['', translate('orch.edge.bindNone')],
      [startRef, translate('orch.io.fromStart')],
    ];
    var currentListed = !currentRef || currentRef === startRef;
    _nodes().forEach(function (node) {
      if (node.id === self.id || node.kind === 'start' || node.kind === 'stop') return;
      if (!upstream[node.id]) return;
      var outputs = ioTools.nodeOutputs(node);
      outputs.forEach(function (port) {
        var ref = ioTools.outputRef(node.id, outputs, port);
        choices.push([ref, _label(node) + ' · ' + port.name]);
        if (ref === currentRef) currentListed = true;
      });
    });
    if (!currentListed) {
      var dot = currentRef.indexOf('.');
      var sourceId = dot === -1 ? currentRef : currentRef.slice(0, dot);
      var source = _find(sourceId);
      choices.push([
        currentRef,
        translate('orch.io.fromStale', {
          node: source ? _label(source) : sourceId,
        }),
      ]);
    }
    return choices.map(function (choice) {
      return '<option value="' + escape(choice[0]) + '"'
        + (choice[0] === currentRef ? ' selected' : '') + '>'
        + escape(choice[1]) + '</option>';
    }).join('');
  }

  function _typeOptions(current) {
    var types = ioTools.types();
    if (current && types.indexOf(current) === -1) types.unshift(current);
    if (!types.length) types.push(ioTools.defaultOutput().type);
    return types.map(function (type) {
      return '<option value="' + escape(type) + '"'
        + (type === current ? ' selected' : '') + '>'
        + escape(type) + '</option>';
    }).join('');
  }

  function _portRow(side, port, index) {
    var defaultType = ioTools.defaultOutput().type;
    var nameRules = typeof ioTools.portNameRules === 'function'
      ? ioTools.portNameRules() : {};
    return '<div class="orch-io-port">'
      + '<input class="orch-input orch-io-name" value="' + escape(port.name || '') + '" '
      + 'placeholder="' + escape(translate('orch.io.namePlaceholder')) + '" '
      + 'aria-label="' + escape(translate('orch.io.namePlaceholder')) + '" '
      + 'data-orch-io-action="set" data-orch-io-side="' + side + '" '
      + 'data-orch-io-index="' + index + '" data-orch-io-key="name"'
      + (nameRules.required ? ' required' : '') + '>'
      + '<select class="orch-input orch-io-type" aria-label="'
      + escape(translate('orch.io.typeLabel')) + '" '
      + 'data-orch-io-action="set" data-orch-io-side="' + side + '" '
      + 'data-orch-io-index="' + index + '" data-orch-io-key="type">'
      + _typeOptions(port.type || defaultType) + '</select>'
      + '<button type="button" class="orch-io-del" title="' + escape(translate('orch.io.removePort'))
      + '" aria-label="' + escape(translate('orch.io.removePort')) + '" '
      + 'data-orch-io-action="remove" data-orch-io-side="' + side + '" '
      + 'data-orch-io-index="' + index + '">'
      + (icons.reject || '×') + '</button></div>';
  }

  function sectionBody(node) {
    var io = node.params && node.params.io || {};
    var inputs = Array.isArray(io.inputs) ? io.inputs : [];
    var outputs = Array.isArray(io.outputs) ? io.outputs : [];
    var html = '<div class="orch-io-head">'
      + escape(translate('orch.io.outputs')) + '</div>';
    if (!outputs.length) {
      html += '<div class="orch-io-implicit">'
        + escape(translate('orch.io.implicitOut')) + '</div>';
    }
    outputs.forEach(function (port, index) {
      html += _portRow('outputs', port, index);
    });
    html += '<button type="button" class="orch-btn orch-btn-ghost orch-io-add" '
      + 'data-orch-io-action="add" data-orch-io-side="outputs">'
      + icons.plus + ' ' + escape(translate('orch.io.addOutput')) + '</button>';

    html += '<div class="orch-io-head orch-io-head-in">'
      + escape(translate('orch.io.inputs')) + '</div>';
    if (inputs.length) {
      html += '<div class="orch-io-subhint">'
        + escape(translate('orch.io.inputsHint')) + '</div>';
    }
    var upstream = upstreamIds(node.id);
    var hasUpstream = _nodes().some(function (candidate) {
      return candidate.id !== node.id && candidate.kind !== 'start'
        && candidate.kind !== 'stop' && upstream[candidate.id];
    });
    inputs.forEach(function (port, index) {
      html += '<div class="orch-io-portbox">'
        + _portRow('inputs', port, index)
        + '<div class="orch-io-fromrow"><span class="orch-io-fromlbl">'
        + escape(translate('orch.io.fromLabel')) + '</span>'
        + '<select class="orch-input orch-io-from" aria-label="'
        + escape(translate('orch.io.fromLabel')) + '" '
        + 'data-orch-io-action="set" data-orch-io-side="inputs" '
        + 'data-orch-io-index="' + index + '" data-orch-io-key="from">'
        + fromOptions(node, port.from) + '</select></div></div>';
    });
    if (inputs.length && !hasUpstream) {
      html += '<div class="orch-io-empty">'
        + escape(translate('orch.io.noUpstream')) + '</div>';
    }
    html += '<button type="button" class="orch-btn orch-btn-ghost orch-io-add" '
      + 'data-orch-io-action="add" data-orch-io-side="inputs">'
      + icons.plus + ' ' + escape(translate('orch.io.addInput')) + '</button>';

    if (node.type === 'role') {
      var preset = ioTools.preset('toolHeavyWorker');
      if (preset && (!preset.appliesTo || preset.appliesTo.indexOf('role') !== -1)) {
        html += '<div class="orch-io-subhint">'
          + escape(translate('orch.io.presetHint')) + '</div>'
          + '<button type="button" class="orch-btn orch-btn-ghost orch-io-preset" '
          + 'data-orch-io-action="preset" data-orch-io-preset="toolHeavyWorker">'
          + escape(translate('orch.io.toolHeavyPreset')) + '</button>';
      }
    }
    return html;
  }

  return {
    sectionBody: sectionBody,
    upstreamIds: upstreamIds,
    fromOptions: fromOptions,
  };
}

