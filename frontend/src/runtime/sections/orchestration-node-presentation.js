/* ===== migrated source: orchestration-node-presentation.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-node-presentation.js — Pure Studio node-card projection

   Converts backend-authored node/catalogue values into escaped card HTML.
   It owns no DOM listeners or graph state; orchestration-node-view.js binds
   the projected controls to editor callbacks.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationNodePresentation(options) {
  options = options || {};
  var catalogue = options.catalogue || createOrchestrationNodeCatalogue({
    controls: options.controls,
    nodeRuntimeDefaults: options.nodeRuntimeDefaults,
    roles: options.roles,
  });

  function _edges() {
    return typeof options.edges === 'function' ? options.edges() : [];
  }

  function _escape(value) {
    return typeof options.escape === 'function'
      ? options.escape(value) : String(value == null ? '' : value);
  }

  function _translate(key, params) {
    return typeof options.translate === 'function'
      ? options.translate(key, params) : key;
  }

  function _issueSummary(nodeId) {
    return typeof options.issueSummary === 'function'
      ? options.issueSummary(nodeId) : null;
  }

  function _issueLabel(summary) {
    return _translate('orch.issues.objectSummary', {
      errors: summary.errors || 0,
      warnings: summary.warnings || 0,
    });
  }

  function _role(role) {
    return catalogue.role(role);
  }

  function _control(kind) {
    return catalogue.control(kind);
  }

  function ioBadge(node) {
    var io = node.params && node.params.io;
    if (!io) return '';
    var inputs = Array.isArray(io.inputs) ? io.inputs.length : 0;
    var outputs = Array.isArray(io.outputs) ? io.outputs.length : 0;
    if (!inputs && !outputs) return '';
    return ' · <span class="orch-io-badge">I/O '
      + inputs + '/' + outputs + '</span>';
  }

  function autoLabel(node) {
    if (node.type === 'subflow') return _translate('orch.group.defaultLabel');
    if (node.type === 'role') {
      var role = _role(node.role);
      return role ? role.label : node.role;
    }
    var control = _control(node.kind);
    return control ? control.label : node.kind;
  }

  function kindLabel(node) {
    return node.type === 'subflow' ? _translate('orch.kind.group')
      : node.type === 'role' ? _translate('orch.kind.agent')
        : _translate('orch.kind.control');
  }

  function nodeBlurb(node) {
    if (node.type === 'role') {
      var role = _role(node.role);
      return role ? role.blurb : '';
    }
    if (node.type === 'subflow') return '';
    var control = _control(node.kind);
    return control ? control.blurb : '';
  }

  function controlSubtitle(node) {
    return _escape(projectOrchestrationControlSummary(
      node, _edges(), _translate, {
        profile: 'studio',
        nodeParam: catalogue.runtimeParam,
      }).text);
  }

  function groupSubtitle(node) {
    return _escape(projectOrchestrationSubflowSummary(
      node, _translate, { nodeParam: catalogue.runtimeParam }).text)
      + ioBadge(node);
  }

  function inspectorAvatar(node) {
    var glyphs = options.glyphs || {};
    if (node.type === 'role') {
      var role = _role(node.role);
      var src = typeof options.iconSrc === 'function'
        ? options.iconSrc(role ? role.icon : 'tofu-general') : '';
      return '<img class="orch-insp-avatar" src="' + _escape(src) + '" alt="">';
    }
    if (node.type === 'subflow') {
      return '<span class="orch-insp-avatar orch-insp-glyph">'
        + (glyphs.group || '') + '</span>';
    }
    var glyph = glyphs[catalogue.controlGlyph(node)] || glyphs.play || '';
    var accent = catalogue.accent(node, 'var(--accent)');
    return '<span class="orch-insp-avatar orch-insp-glyph" '
      + 'style="--node-accent:' + _escape(accent) + '">' + glyph + '</span>';
  }

  function _presentation(node) {
    var glyphs = options.glyphs || {};
    if (node.type === 'subflow') {
      return {
        accent: catalogue.accent(node), typeClass: ' orch-node-group',
        icon: glyphs.group || '', subtitle: groupSubtitle(node),
      };
    }
    if (node.type === 'role') {
      var role = _role(node.role) || {};
      var src = typeof options.iconSrc === 'function' ? options.iconSrc(role.icon) : '';
      var summary = projectOrchestrationRoleExecutionSummary(
        node, _translate, {
          defaultEmits: options.defaultEmits,
          nodeParam: catalogue.runtimeParam,
        });
      var subtitle = _escape(summary.text);
      if (summary.emitsValue === 'user') {
        subtitle += ' · ' + ((options.icons || {}).speak || '')
          + _escape(summary.emits);
      }
      return {
        accent: catalogue.accent(node), typeClass: ' orch-node-role',
        icon: '<img src="' + _escape(src) + '" alt="">',
        subtitle: subtitle + ioBadge(node),
      };
    }
    return {
      accent: catalogue.accent(node, '#888'),
      typeClass: ' orch-node-ctrl orch-node-' + _escape(node.kind || 'ctrl'),
      icon: glyphs[catalogue.controlGlyph(node)] || '',
      subtitle: controlSubtitle(node),
    };
  }

  function cardHtml(node, selected, connectingFrom) {
    var view = _presentation(node);
    var title = _escape(node.name || autoLabel(node));
    var id = _escape(node.id);
    var hasInput = node.kind !== 'start';
    var hasOutput = node.kind !== 'stop';
    var selectedClass = selected === node.id ? ' is-selected' : '';
    var connectingClass = connectingFrom === node.id ? ' is-connecting' : '';
    var issues = _issueSummary(node.id);
    var issueClass = issues && issues.total
      ? ' has-issues ' + (issues.errors ? 'has-errors' : 'has-warnings') : '';
    var issueLabel = issues && issues.total ? _issueLabel(issues) : '';
    var accessibleTitle = title
      + (issueLabel ? ' · ' + _escape(issueLabel) : '');
    var selectedNode = selected === node.id;
    var localTab = selectedNode ? '' : ' tabindex="-1"';
    var inputTab = selectedNode || connectingFrom && connectingFrom !== node.id
      ? '' : ' tabindex="-1"';
    var html = '<div class="orch-node' + view.typeClass + selectedClass
      + connectingClass + issueClass + '" id="orch-node-' + id
      + '" data-node-id="' + id + '" '
      + 'style="left:' + Number(node.x || 0) + 'px;top:' + Number(node.y || 0)
      + 'px;--node-accent:' + _escape(view.accent) + '" role="group" '
      + 'aria-label="' + accessibleTitle
      + '"' + (selectedNode ? ' aria-current="true"' : '') + '>';
    if (node.kind === 'start') {
      html += '<span class="orch-node-ribbon orch-ribbon-in">'
        + _escape(_translate('orch.ribbon.input')) + '</span>';
    } else if (node.kind === 'stop') {
      html += '<span class="orch-node-ribbon orch-ribbon-out">'
        + _escape(_translate('orch.ribbon.result')) + '</span>';
    }
    if (hasInput) {
      html += '<button type="button" class="orch-port orch-port-in" '
        + inputTab + ' aria-label="'
        + _escape(_translate('orch.port.input', { name: title }))
        + '"></button>';
    }
    html += '<div class="orch-node-head"'
      + (node.type === 'subflow'
        ? ' title="' + _escape(_translate('orch.group.chipTip')) + '"' : '') + '>'
      + '<button type="button" class="orch-node-select" aria-label="' + accessibleTitle
      + '" aria-pressed="' + selectedNode + '">'
      + '<span class="orch-node-icon">' + view.icon + '</span>'
      + '<span class="orch-node-title">' + title + '</span>'
      + (issues && issues.total
        ? '<span class="orch-node-issues" title="' + _escape(issueLabel)
          + '" aria-hidden="true">' + (issues.errors ? '!' : '△')
          + issues.total + '</span>' : '') + '</button>'
      + '<button type="button" class="orch-node-del" title="'
      + _escape(_translate('orch.btn.deleteNode')) + '"' + localTab
      + ' aria-label="'
      + _escape(_translate('orch.btn.deleteNode')) + '">'
      + ((options.icons || {}).reject || '') + '</button></div>'
      + '<div class="orch-node-sub">' + view.subtitle + '</div>';
    if (hasOutput) {
      html += '<button type="button" class="orch-port orch-port-out" aria-label="'
        + _escape(_translate('orch.port.output', { name: title }))
        + '"' + localTab + ' aria-pressed="'
        + (connectingFrom === node.id) + '"></button>';
    }
    return html + '</div>';
  }

  return {
    cardHtml: cardHtml,
    autoLabel: autoLabel,
    kindLabel: kindLabel,
    nodeBlurb: nodeBlurb,
    inspectorAvatar: inspectorAvatar,
    controlSubtitle: controlSubtitle,
    groupSubtitle: groupSubtitle,
    ioBadge: ioBadge,
  };
}

