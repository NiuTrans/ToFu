/* ===== migrated source: orchestration-canvas-view.js ===== */
/* Orchestration canvas view composition.
 *
 * Keeps DOM refresh order and empty-state rendering in one presentation-only
 * seam. Graph mutation, persistence and transport remain outside this module.
 */

function createOrchestrationCanvasView(options) {
  options = options || {};
  var doc = options.document
    || (typeof document !== 'undefined' ? document : null);
  var icons = options.icons || {};

  function mount(id) {
    return doc && typeof doc.getElementById === 'function'
      ? doc.getElementById(id) : null;
  }

  function nodeCount() {
    return typeof options.nodeCount === 'function'
      ? Number(options.nodeCount()) || 0 : 0;
  }

  function translate(key) {
    return typeof options.translate === 'function'
      ? options.translate(key) : key;
  }

  function renderNodes() {
    var root = mount('orchNodes');
    if (!root) return;
    if (options.nodeView && typeof options.nodeView.render === 'function') {
      options.nodeView.render(root);
    }
    if (options.viewport && typeof options.viewport.sync === 'function') {
      options.viewport.sync();
    }
  }

  function renderEdges() {
    var svg = mount('orchEdges');
    var canvas = mount('orchCanvas');
    if (!svg || !canvas || !options.edgeView
        || typeof options.edgeView.render !== 'function') return;
    return options.edgeView.render(svg, canvas);
  }

  function renderInspector() {
    var root = mount('orchInspector');
    if (!root || !options.inspectorView
        || typeof options.inspectorView.render !== 'function') return;
    return options.inspectorView.render(root);
  }

  function renderHint() {
    var root = mount('orchHint');
    if (!root) return false;
    var hasNodes = nodeCount() > 0;
    root.style.display = hasNodes ? 'none' : 'block';
    root.replaceChildren();
    if (hasNodes) return false;

    var card = doc.createElement('div');
    card.className = 'orch-hint-card';
    var icon = doc.createElement('div');
    icon.className = 'orch-hint-emoji';
    icon.innerHTML = icons.puzzle || '';
    var title = doc.createElement('div');
    title.className = 'orch-hint-title';
    title.textContent = translate('orch.hint.title');
    var text = doc.createElement('div');
    text.className = 'orch-hint-text';
    if (typeof options.richCopy === 'function') {
      text.innerHTML = options.richCopy(translate('orch.hint.text'));
    } else {
      text.textContent = translate('orch.hint.text');
    }
    card.appendChild(icon);
    card.appendChild(title);
    card.appendChild(text);
    root.appendChild(card);
    return true;
  }

  function renderBreadcrumb() {
    if (!mount('orchCrumb') || !options.navigation
        || typeof options.navigation.renderBreadcrumb !== 'function') return;
    return options.navigation.renderBreadcrumb();
  }

  function render() {
    var input = mount('orchNameInput');
    var name = typeof options.name === 'function' ? options.name() : '';
    if (input && input.value !== name) input.value = name;
    renderNodes();
    renderEdges();
    renderInspector();
    renderHint();
    renderBreadcrumb();
  }

  return {
    render: render,
    renderNodes: renderNodes,
    renderEdges: renderEdges,
    renderInspector: renderInspector,
    renderHint: renderHint,
    renderBreadcrumb: renderBreadcrumb,
  };
}

