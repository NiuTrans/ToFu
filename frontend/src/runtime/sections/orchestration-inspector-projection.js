/* ===== migrated source: orchestration-inspector-projection.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-inspector-projection.js — pure Inspector HTML projection

   Projects node, edge and empty selections from injected graph/catalogue
   ports. DOM replacement, focus, scroll and interaction binding stay in the
   Inspector View.
   ═══════════════════════════════════════════════════════════════════ */


function createOrchestrationInspectorProjection(options) {
  options = options || {};

  function _translate(key, params) {
    return typeof options.translate === 'function'
      ? options.translate(key, params) : key;
  }
  function _escape(value) {
    return typeof options.escape === 'function'
      ? options.escape(value) : String(value == null ? '' : value);
  }
  function _nodes() {
    return typeof options.nodes === 'function' ? options.nodes() : [];
  }
  function _edges() {
    return typeof options.edges === 'function' ? options.edges() : [];
  }
  function findNode(id) {
    if (typeof options.findNode === 'function') return options.findNode(id);
    return _nodes().filter(function (node) { return node.id === id; })[0] || null;
  }
  function findEdge(id) {
    return _edges().filter(function (edge) { return edge.id === id; })[0] || null;
  }
  function _nodeLabel(id, node) {
    if (typeof options.nodeLabel === 'function') return options.nodeLabel(id);
    return node ? (node.name || options.autoLabel(node)) : id;
  }
  function _section(key, icon, open, inner, hint) {
    return typeof options.section === 'function'
      ? options.section(key, icon, open, inner, hint) : inner;
  }
  function _select(label, key, value, choices) {
    return typeof options.selectField === 'function'
      ? options.selectField(label, key, value, choices) : '';
  }
  function _nodeParam(node, key) {
    if (typeof options.nodeParam === 'function') {
      return options.nodeParam(node, key);
    }
    var params = node && node.params;
    return params && typeof params === 'object'
      && Object.prototype.hasOwnProperty.call(params, key)
      ? params[key] : null;
  }

  function _executionChoices(axis) {
    var contract = typeof options.executionOptions === 'function'
      ? options.executionOptions() : {};
    var values = Array.isArray(contract[axis]) ? contract[axis] : [];
    return values.map(function (value) {
      return [value, orchestrationExecutionOptionLabel(
        axis, value, _translate, 'editor')];
    });
  }

  function _roleChoices() {
    var roles = typeof options.roles === 'function' ? options.roles() : [];
    return roles.map(function (role) {
      var key = 'orch.roleName.' + role.role;
      var translated = _translate(key);
      return [role.role, translated && translated !== key
        ? translated : (role.label || role.role)];
    });
  }

  function _mobileHeader(label) {
    var icons = options.icons || {};
    return '<div class="orch-sheet-head orch-m-only"><span>'
      + (icons.gear || '') + ' ' + _escape(label) + '</span>'
      + '<button type="button" class="orch-icon-btn orch-inspector-close" title="'
      + _escape(_translate('orch.tip.close')) + '" aria-label="'
      + _escape(_translate('orch.tip.close')) + '">'
      + (icons.reject || '') + '</button></div>';
  }

  function edgeHtml(edge) {
    var from = findNode(edge.from);
    var to = findNode(edge.to);
    var fromLabel = _nodeLabel(edge.from, from);
    var toLabel = _nodeLabel(edge.to, to);
    var fromHtml = _escape(fromLabel);
    var toHtml = _escape(toLabel);
    var html = _mobileHeader(_translate('orch.edge.title'))
      + '<div class="orch-insp-head"><span class="orch-insp-kind">'
      + _escape(_translate('orch.edge.title')) + '</span>'
      + '<span class="orch-insp-type">' + fromHtml + ' → ' + toHtml
      + '</span></div><div class="orch-edge-flow"><b>' + fromHtml
      + '</b> <span class="orch-edge-arrowtxt">→</span> <b>' + toHtml
      + '</b></div>';

    var inputPorts = to && typeof options.nodeInputs === 'function'
      ? options.nodeInputs(to) : [];
    if (inputPorts.length && from) {
      var sourceOutputs = typeof options.nodeOutputs === 'function'
        ? options.nodeOutputs(from) : [];
      html += '<div class="orch-note orch-note-wire">'
        + _escape(_translate('orch.edge.bindNote')) + '</div>';
      inputPorts.forEach(function (port, index) {
        var choices = [['', _translate('orch.edge.bindNone')]];
        sourceOutputs.forEach(function (output) {
          var ref = typeof options.outputRef === 'function'
            ? options.outputRef(from.id, sourceOutputs, output) : from.id;
          choices.push([ref, output.name + ' (' + (output.type || 'any') + ')']);
        });
        var current = port.from && (port.from === from.id
          || port.from.indexOf(from.id + '.') === 0) ? port.from : '';
        var choicesHtml = choices.map(function (choice) {
          return '<option value="' + _escape(choice[0]) + '"'
            + (choice[0] === current ? ' selected' : '') + '>'
            + _escape(choice[1]) + '</option>';
        }).join('');
        html += '<label class="orch-fld"><span>'
          + _escape(_translate('orch.edge.bindTo', { port: port.name }))
          + '</span><select class="orch-input orch-edge-binding" data-input-index="'
          + index + '">' + choicesHtml + '</select></label>';
      });
    }
    return html + '<div class="orch-edge-btns">'
      + '<button type="button" class="orch-btn orch-btn-ghost orch-btn-block" '
      + 'data-orch-inspector-action="reverse-edge">'
      + _escape(_translate('orch.edge.reverse')) + '</button>'
      + '<button type="button" class="orch-btn orch-btn-danger orch-btn-block" '
      + 'data-orch-inspector-action="delete-edge">'
      + _escape(_translate('orch.edge.delete')) + '</button></div>';
  }

  function _groupHtml(node) {
    var icons = options.icons || {};
    var params = node.params || {};
    var definition = params.definition || {};
    var html = '<button type="button" class="orch-btn orch-btn-primary '
      + 'orch-btn-block orch-insp-cta" data-orch-inspector-action="enter-group">'
      + _escape(_translate('orch.group.open'))
      + ' <span class="orch-insp-cta-sub">'
      + _escape(_translate('orch.group.summary', {
        n: (definition.nodes || []).length,
        m: (definition.edges || []).length,
      })) + '</span></button>';
    var identity = options.labelField(node)
      + _select(_translate('orch.fld.groupFace'), 'role', node.role, _roleChoices());
    html += _section('orch.sec.identity', icons.gear, true, identity);
    var execution = _select(
      _translate('orch.fld.groupScope'), 'scope', _nodeParam(node, 'scope'),
      _executionChoices('scopes')
    ) + _select(
      _translate('orch.fld.emits'), 'emits', _nodeParam(node, 'emits'),
      [['', _translate('orch.emits.auto', {
        role: options.defaultEmits(node.role),
      })]].concat(_executionChoices('emits'))
    );
    html += _section('orch.sec.execution', icons.gear, false, execution,
                     'orch.note.group');
    html += _section('orch.sec.io', icons.package, false,
                     options.ioSectionBody(node), 'orch.io.note');
    return html;
  }

  function _roleHtml(node) {
    var icons = options.icons || {};
    var params = node.params || {};
    var html = _section('orch.sec.task', icons.flag, true,
                        options.roleTaskBody(node), 'orch.task.note');
    var trace = options.runTraceBody(node);
    if (trace) {
      html += _section('orch.sec.lastRun', icons.rocket, true, trace,
                       'orch.run.note');
    }
    var execution = options.labelField(node)
      + _select(_translate('orch.fld.tier'), 'tier', _nodeParam(node, 'tier'),
                _executionChoices('tiers'))
      + _select(_translate('orch.fld.context'), 'isolation',
                _nodeParam(node, 'isolation'),
                _executionChoices('isolation'))
      + _select(_translate('orch.fld.emits'), 'emits',
                _nodeParam(node, 'emits'),
        [['', _translate('orch.emits.auto', {
          role: options.defaultEmits(node.role),
        })]].concat(_executionChoices('emits')));
    html += _section('orch.sec.execution', icons.gear, false, execution,
                     'orch.note.exec');
    var ioContract = params.io;
    var hasExplicitIo = !!(ioContract && (
      Array.isArray(ioContract.inputs) && ioContract.inputs.length
      || Array.isArray(ioContract.outputs) && ioContract.outputs.length
    ));
    html += _section('orch.sec.io', icons.package, hasExplicitIo,
                     options.ioSectionBody(node), 'orch.io.note');
    html += _section('orch.sec.persona', icons.bot, false,
                     options.personaBody(node), 'orch.persona.note');
    return html;
  }

  function _controlHtml(node) {
    var icons = options.icons || {};
    var fields = typeof options.controlFields === 'function'
      ? options.controlFields(node.kind) : [];
    var settings = options.labelField(node)
      + options.controlSchemaSection(node, fields);
    var hint = ({
      loop: 'orch.note.loop', artifact: 'orch.note.artifact',
      human: 'orch.note.human', start: 'orch.note.start',
      stop: 'orch.note.stop',
    })[node.kind] || null;
    return _section('orch.sec.flow', icons.package, true,
                    options.flowSummaryBody(node), 'orch.flow.note')
      + _section('orch.sec.settings', icons.gear, true, settings, hint);
  }

  function nodeHtml(node) {
    var html = _mobileHeader(options.kindLabel(node));
    html += options.header(node);
    if (node.type === 'subflow') html += _groupHtml(node);
    else if (node.type === 'role') html += _roleHtml(node);
    else html += _controlHtml(node);

    var connections = orchestrationConnections(_edges(), node.id);
    return html + '<div class="orch-insp-foot"><div class="orch-conn-box">'
      + '<div class="orch-conn-row">' + _escape(_translate('orch.conn.in'))
      + ' <b>' + connections.incoming.length
      + '</b></div><div class="orch-conn-row">'
      + _escape(_translate('orch.conn.out')) + ' <b>'
      + connections.outgoing.length
      + '</b> →</div></div><button type="button" '
      + 'class="orch-btn orch-btn-danger orch-btn-block" '
      + 'data-orch-inspector-action="delete-node">'
      + _escape(_translate('orch.btn.deleteNode')) + '</button></div>';
  }

  function emptyHtml() {
    return _mobileHeader(_translate('orch.toolbar.edit'))
      + '<div class="orch-insp-empty"><div class="orch-insp-empty-icon">'
      + ((options.icons || {}).gear || '') + '</div>'
      + _escape(_translate('orch.insp.empty'))
      + '<div class="orch-insp-stats">'
      + _escape(_translate('orch.insp.stats', {
        n: _nodes().length, m: _edges().length,
      })) + '</div></div>';
  }

  return Object.freeze({
    findNode: findNode,
    findEdge: findEdge,
    edgeHtml: edgeHtml,
    nodeHtml: nodeHtml,
    emptyHtml: emptyHtml,
  });
}

