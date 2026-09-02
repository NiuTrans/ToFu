/* ===== migrated source: orchestration-node-view.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-node-view.js — Studio node-card DOM interaction

   Reconciles projected cards with the Canvas and binds their local pointer,
   keyboard and focus behavior. Presentation lives in
   orchestration-node-presentation.js; graph mutations remain callbacks into
   the editor.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationNodeView(options) {
  options = options || {};
  var presentation = options.presentation
    || createOrchestrationNodePresentation(options);

  function _keyboardPort(event, id, side) {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    event.stopPropagation();
    if (typeof options.onPortKeyDown === 'function') {
      options.onPortKeyDown(event, id, side);
    }
  }

  function _bindCard(card) {
    var id = card.getAttribute('data-node-id');
    card.addEventListener('pointerdown', function () {
      if (typeof options.onSelect === 'function') options.onSelect(id);
    });
    var select = card.querySelector('.orch-node-select');
    if (select) select.addEventListener('keydown', function (event) {
      if (typeof options.onNodeKeyDown === 'function') options.onNodeKeyDown(event, id);
    });
    var head = card.querySelector('.orch-node-head');
    var avatar = card.querySelector('.orch-node-icon img');
    if (avatar) avatar.addEventListener('error', function () {
      avatar.style.display = 'none';
    });
    if (head) {
      head.addEventListener('pointerdown', function (event) {
        if (typeof options.onHeaderPointerDown === 'function') {
          options.onHeaderPointerDown(event, id);
        }
      });
      head.addEventListener('dblclick', function () {
        var nodes = typeof options.nodes === 'function' ? options.nodes() : [];
        var node = nodes.filter(function (item) { return item.id === id; })[0];
        if (node && node.type === 'subflow' && typeof options.onEnterGroup === 'function') {
          options.onEnterGroup(id);
        }
      });
    }
    var remove = card.querySelector('.orch-node-del');
    if (remove) {
      remove.addEventListener('pointerdown', function (event) { event.stopPropagation(); });
      remove.addEventListener('click', function (event) {
        event.stopPropagation();
        if (typeof options.onDelete === 'function') options.onDelete(id);
      });
    }
    var input = card.querySelector('.orch-port-in');
    if (input) {
      input.addEventListener('pointerup', function (event) {
        if (typeof options.onPortUp === 'function') options.onPortUp(event, id);
      });
      input.addEventListener('keydown', function (event) {
        _keyboardPort(event, id, 'in');
      });
    }
    var output = card.querySelector('.orch-port-out');
    if (output) {
      output.addEventListener('pointerdown', function (event) {
        if (typeof options.onPortDown === 'function') options.onPortDown(event, id);
      });
      output.addEventListener('keydown', function (event) {
        _keyboardPort(event, id, 'out');
      });
    }
  }

  function render(wrap) {
    if (!wrap) return;
    var focusedNodeId = null;
    var focusedControl = '';
    var active = (options.document || document).activeElement;
    var activeCard = active && typeof active.closest === 'function'
      ? active.closest('.orch-node') : null;
    if (activeCard && wrap.contains(activeCard)) {
      focusedNodeId = activeCard.getAttribute('data-node-id');
      [
        '.orch-node-select', '.orch-node-del',
        '.orch-port-in', '.orch-port-out',
      ].some(function (selector) {
        if (!active.matches(selector)) return false;
        focusedControl = selector;
        return true;
      });
    }
    var nodes = typeof options.nodes === 'function' ? options.nodes() : [];
    var selected = typeof options.selectedId === 'function' ? options.selectedId() : null;
    var connectingFrom = typeof options.connectingFrom === 'function'
      ? options.connectingFrom() : null;
    wrap.innerHTML = nodes.map(function (node) {
      return presentation.cardHtml(node, selected, connectingFrom);
    }).join('');
    var cards = Array.prototype.slice.call(wrap.querySelectorAll('.orch-node'));
    cards.forEach(_bindCard);
    var focusedCard = cards.filter(function (card) {
      return card.getAttribute('data-node-id') === focusedNodeId;
    })[0] || null;
    var focusedElement = focusedCard && focusedControl
      ? focusedCard.querySelector(focusedControl) : null;
    var keyboard = createOrchestrationRovingItemsController({
      root: wrap,
      selector: '.orch-node-select',
    });
    keyboard.sync((focusedControl === '.orch-node-select' && focusedElement)
      || wrap.querySelector('.orch-node.is-selected .orch-node-select'));
    if (focusedElement && focusedElement.tabIndex >= 0
        && typeof focusedElement.focus === 'function') {
      focusedElement.focus({ preventScroll: true });
    }
  }

  return {
    render: render,
    autoLabel: presentation.autoLabel,
    kindLabel: presentation.kindLabel,
    nodeBlurb: presentation.nodeBlurb,
    inspectorAvatar: presentation.inspectorAvatar,
    controlSubtitle: presentation.controlSubtitle,
    groupSubtitle: presentation.groupSubtitle,
    ioBadge: presentation.ioBadge,
  };
}

