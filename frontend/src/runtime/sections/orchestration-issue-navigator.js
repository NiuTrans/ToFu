/* ===== migrated source: orchestration-issue-navigator.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-issue-navigator.js — inspection issue list + navigation

   Consumes backend-authored JSON Pointer diagnostics. It never infers a
   target from human copy; graph selection, nested-workspace navigation and
   Inspector rendering stay behind injected commands.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationIssueNavigator(options) {
  options = options || {};
  var doc = options.document || document;
  var current = null;
  var open = false;
  var issueKeyboard = null;
  var presentation = options.presentation
    || createOrchestrationIssuePresentation(options);

  function _scrollBehavior() {
    var view = options.window || doc.defaultView;
    return view && typeof view.prefersReducedMotion === 'function'
      && view.prefersReducedMotion() ? 'auto' : 'smooth';
  }

  function resolve(diagnostic, definition) {
    var projector = typeof options.resolveTarget === 'function'
      ? options.resolveTarget : resolveOrchestrationDiagnosticTarget;
    return projector(diagnostic, definition);
  }

  function _focusTarget(target) {
    if (target && (target.kind === 'node' || target.kind === 'edge')
        && target.navigable !== false
        && typeof options.focusSelection === 'function'
        && options.focusSelection()) return true;
    var canvas = doc.getElementById('orchCanvas');
    if (!canvas || typeof canvas.focus !== 'function') return false;
    try { canvas.focus({ preventScroll: true }); }
    catch (_error) { canvas.focus(); }
    return true;
  }

  function navigate(diagnostic, descriptionId) {
    var definition = typeof options.definition === 'function'
      ? options.definition() : null;
    var target = resolve(diagnostic, definition);
    if (!target) return false;
    if (typeof options.navigateGroups === 'function'
        && options.navigateGroups(target.groups) === false) return false;
    var selected;
    var navigable = target.navigable !== false;
    if (navigable && target.kind === 'node'
        && typeof options.selectNode === 'function') {
      selected = options.selectNode(target.id);
    } else if (navigable && target.kind === 'edge'
               && typeof options.selectEdgeAt === 'function') {
      selected = options.selectEdgeAt(target.index);
    }
    if (selected === false) return false;
    if (typeof options.showInspector === 'function'
        && target.kind !== 'document' && navigable) options.showInspector();

    var focused = navigable && typeof options.focusDiagnostic === 'function'
      ? options.focusDiagnostic(
        target, diagnostic, _scrollBehavior(), descriptionId) : null;
    if (!focused) _focusTarget(target);
    close();
    return true;
  }

  function render() {
    var panel = doc.getElementById('orchIssuePanel');
    if (!panel || !current) return;
    var definition = typeof options.definition === 'function'
      ? options.definition() : null;
    presentation.render(panel, current, definition);
    var closeButton = panel.querySelector('[data-orch-issues-close]');
    if (closeButton) {
      closeButton.addEventListener('click', function () { close(true); });
    }
    Array.prototype.forEach.call(
      panel.querySelectorAll('[data-orch-issue-index]'), function (button) {
        var index = Number(button.getAttribute('data-orch-issue-index'));
        button.addEventListener('click', function () {
          navigate(current.diagnostics[index],
            button.getAttribute('data-orch-issue-message-id') || '');
        });
      }
    );
    if (issueKeyboard) issueKeyboard.sync();
  }

  function _syncExpanded() {
    var trigger = doc.getElementById('orchDocState');
    var panel = doc.getElementById('orchIssuePanel');
    if (trigger) trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (panel) panel.hidden = !open;
  }

  function close(restoreFocus) {
    if (!open) return false;
    open = false;
    _syncExpanded();
    var trigger = doc.getElementById('orchDocState');
    if (restoreFocus && trigger && typeof trigger.focus === 'function') {
      trigger.focus();
    }
    return true;
  }

  function show(state) {
    current = presentation.snapshot(state);
    open = !open;
    _syncExpanded();
    if (open) render();
    return open;
  }

  function sync(state) {
    current = presentation.snapshot(state);
    if (open) render();
  }

  var panel = doc.getElementById('orchIssuePanel');
  var trigger = doc.getElementById('orchDocState');
  if (panel) issueKeyboard = createOrchestrationRovingItemsController({
    root: panel,
    entry: trigger,
    selector: '.orch-issue-item',
    wrap: true,
    onEntry: function () {
      if (!current || open) return;
      open = true;
      _syncExpanded();
      render();
    },
  });
  doc.addEventListener('pointerdown', function (event) {
    if (!open) return;
    var wrap = doc.querySelector('.orch-doc-state-wrap');
    if (wrap && !wrap.contains(event.target)) close();
  });
  return {
    resolve: resolve,
    navigate: navigate,
    render: render,
    show: show,
    sync: sync,
    close: close,
    isOpen: function () { return open; },
  };
}

