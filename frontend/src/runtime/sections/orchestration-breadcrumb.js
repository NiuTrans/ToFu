/* ===== migrated source: orchestration-breadcrumb.js ===== */
/* Accessible nested-flow breadcrumb view. Graph transitions stay in the
 * navigation controller; this module only projects hierarchy and focus. */

function createOrchestrationBreadcrumbView(options) {
  options = options || {};
  var doc = options.document || document;

  function _mount() { return doc.getElementById('orchCrumb'); }
  function _fallbackName() {
    return typeof options.fallbackName === 'function'
      ? options.fallbackName() : 'Group';
  }
  function _translate(key) {
    return typeof options.translate === 'function'
      ? options.translate(key) : key;
  }

  function _separator() {
    var separator = doc.createElement('span');
    separator.className = 'orch-crumb-sep';
    separator.textContent = '\u203a';
    separator.setAttribute('aria-hidden', 'true');
    return separator;
  }

  function _ancestor(label, depth, onNavigate) {
    var button = doc.createElement('button');
    button.type = 'button';
    button.className = 'orch-crumb-item';
    button.textContent = label;
    button.addEventListener('click', function () {
      if (typeof onNavigate === 'function') onNavigate(depth);
    });
    return button;
  }

  function _current(label) {
    var current = doc.createElement('span');
    current.className = 'orch-crumb-item orch-crumb-current';
    current.textContent = label;
    current.tabIndex = -1;
    current.setAttribute('aria-current', 'page');
    return current;
  }

  function _frameLabel(frame) {
    var group = options.graph.findNode(frame.nodes || [], frame.groupId);
    var label = group && typeof options.nodeLabel === 'function'
      ? options.nodeLabel(group) : '';
    return label || _fallbackName();
  }

  function render(frames, onNavigate) {
    var element = _mount();
    if (!element) return null;
    frames = Array.isArray(frames) ? frames : [];
    element.replaceChildren();
    element.hidden = !frames.length;
    if (!frames.length) return null;

    element.appendChild(_ancestor(_translate('orch.crumb.root'), 0, onNavigate));
    frames.forEach(function (frame, index) {
      element.appendChild(_separator());
      var label = _frameLabel(frame);
      element.appendChild(index === frames.length - 1
        ? _current(label) : _ancestor(label, index + 1, onNavigate));
    });
    return element.querySelector('[aria-current="page"]');
  }

  function focusAfterNavigation(workspace) {
    var element = _mount();
    var current = element && !element.hidden
      ? element.querySelector('[aria-current="page"]') : null;
    var selected = workspace && workspace.selected;
    var card = selected ? doc.getElementById('orch-node-' + selected) : null;
    var target = current || (card && card.querySelector('.orch-node-select'))
      || doc.getElementById('orchCanvas');
    if (!target || typeof target.focus !== 'function') return false;
    try { target.focus({ preventScroll: true }); }
    catch (_error) { target.focus(); }
    return true;
  }

  return { render: render, focusAfterNavigation: focusAfterNavigation };
}

