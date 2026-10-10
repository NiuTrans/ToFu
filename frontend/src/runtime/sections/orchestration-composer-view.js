/* ===== migrated source: orchestration-composer-view.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-composer-view.js — AI Composer DOM presentation

   Owns Composer panel visibility, focus scheduling and input controls. Safe
   conversation projection lives in orchestration-composer-log-view.js.
   Request/history ownership stays in orchestration-composer.js.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationComposerView(options) {
  options = options || {};
  var focusTimer = null;
  var opened = false;
  var logView = createOrchestrationComposerLogView(options);
  var focusReturn = createOrchestrationPanelFocusReturn();

  function _document() {
    return options.document || document;
  }

  function _translate(key, params) {
    return typeof options.translate === 'function'
      ? options.translate(key, params) : key;
  }

  function _schedule(callback, delay) {
    return typeof options.schedule === 'function'
      ? options.schedule(callback, delay) : setTimeout(callback, delay);
  }

  function _cancel(timer) {
    if (typeof options.cancelSchedule === 'function') {
      options.cancelSchedule(timer);
    } else {
      clearTimeout(timer);
    }
  }

  function _cancelFocus() {
    if (focusTimer == null) return;
    _cancel(focusTimer);
    focusTimer = null;
  }

  function render(snapshot) {
    return logView.render(snapshot);
  }

  function setEnabled(enabled) {
    var send = _document().getElementById('orchAiSend');
    var input = _document().getElementById('orchAiText');
    if (send) send.disabled = !enabled;
    if (input) input.disabled = !enabled;
  }

  function requirement() {
    var input = _document().getElementById('orchAiText');
    return input ? input.value : '';
  }

  function clearRequirement() {
    var input = _document().getElementById('orchAiText');
    if (input) input.value = '';
  }

  function toggle(force, snapshot) {
    var doc = _document();
    var panel = doc.getElementById('orchAi');
    var button = doc.getElementById('orchAiToggle');
    if (!panel) return false;
    var open = typeof force === 'boolean' ? force : !opened;
    if (open) focusReturn.capture(doc);
    else focusReturn.prepare(doc, panel);
    setOrchestrationPanelState(panel, open, {
      document: doc,
      openClass: 'is-open',
      trigger: button,
      triggerActiveClass: 'is-active',
    });
    opened = open;
    if (typeof options.onVisibilityChange === 'function') {
      options.onVisibilityChange(opened);
    }
    if (!open) focusReturn.restore(doc);
    _cancelFocus();
    if (open) {
      if (!snapshot || !snapshot.history || !snapshot.history.length) {
        render(snapshot);
      }
      focusTimer = _schedule(function () {
        focusTimer = null;
        var input = doc.getElementById('orchAiText');
        if (panel.classList.contains('is-open') && input && !input.disabled) {
          input.focus();
        }
      }, 50);
    }
    return open;
  }

  function destroy() {
    _cancelFocus();
    focusReturn.clear();
  }

  return {
    render: render,
    setEnabled: setEnabled,
    requirement: requirement,
    clearRequirement: clearRequirement,
    isOpen: function () { return opened; },
    toggle: toggle,
    destroy: destroy,
  };
}

