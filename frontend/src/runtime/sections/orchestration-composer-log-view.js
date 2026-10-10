/* ===== migrated source: orchestration-composer-log-view.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-composer-log-view.js — AI Composer conversation projection

   Owns append-only history diffing, safe message nodes and ARIA-silent full
   repaints. Panel visibility, focus and input controls stay in the facade.
   ═══════════════════════════════════════════════════════════════════ */


function createOrchestrationComposerLogView(options) {
  options = options || {};
  var renderedHistory = [];

  function doc() { return options.document || document; }
  function translate(key, params) {
    return typeof options.translate === 'function'
      ? options.translate(key, params) : key;
  }
  function div(className, text) {
    var element = doc().createElement('div');
    element.className = className;
    if (text != null) element.textContent = String(text);
    return element;
  }
  function setRichCopy(element, value) {
    if (typeof options.richCopy === 'function') {
      element.innerHTML = options.richCopy(value);
    } else {
      element.textContent = String(value == null ? '' : value);
    }
  }
  function renderEmpty(log) {
    var empty = div('orch-ai-empty');
    var icon = div('orch-ai-empty-icon');
    // Catalog icons are trusted, code-owned SVG. Localized and remote
    // conversation content below is emitted through text nodes/safe rich copy.
    icon.innerHTML = (options.icons || {}).wand || '';
    empty.appendChild(icon);
    empty.appendChild(div(
      'orch-ai-empty-title', translate('orch.ai.emptyTitle')
    ));
    var copy = div('orch-ai-empty-text');
    setRichCopy(copy, translate('orch.ai.emptyText'));
    empty.appendChild(copy);
    log.appendChild(empty);
  }
  function renderMessage(log, message) {
    var role = message && message.role === 'user' ? 'user' : 'bot';
    log.appendChild(div(
      'orch-ai-msg orch-ai-' + role,
      message && message.content != null ? message.content : ''
    ));
  }
  function sameMessage(left, right) {
    return !!left && !!right && left.role === right.role
      && left.content === right.content;
  }
  function canAppend(history) {
    return renderedHistory.length <= history.length
      && renderedHistory.every(function (message, index) {
        return sameMessage(message, history[index]);
      });
  }
  function withoutAnnouncements(log, callback) {
    var hadLive = log.hasAttribute('aria-live');
    var live = log.getAttribute('aria-live');
    log.setAttribute('aria-live', 'off');
    callback();
    if (hadLive) log.setAttribute('aria-live', live);
    else log.removeAttribute('aria-live');
  }
  function remember(history) {
    renderedHistory = history.map(function (message) {
      return { role: message.role, content: message.content };
    });
  }

  function render(snapshot) {
    snapshot = snapshot || { history: [], busy: false };
    var log = doc().getElementById('orchAiLog');
    if (!log) return false;
    var history = Array.isArray(snapshot.history) ? snapshot.history : [];
    log.setAttribute('aria-busy', snapshot.busy ? 'true' : 'false');
    if (!history.length) {
      if (renderedHistory.length || !log.querySelector('.orch-ai-empty')) {
        withoutAnnouncements(log, function () {
          log.textContent = '';
          renderEmpty(log);
        });
      }
      renderedHistory = [];
      return true;
    }
    var appendOnly = canAppend(history);
    var typing = log.querySelector('.orch-ai-typing');
    if (typing) typing.remove();
    if (!appendOnly) {
      withoutAnnouncements(log, function () {
        log.textContent = '';
        history.forEach(function (message) { renderMessage(log, message); });
      });
    } else {
      if (!renderedHistory.length) {
        withoutAnnouncements(log, function () { log.textContent = ''; });
      }
      history.slice(renderedHistory.length).forEach(function (message) {
        renderMessage(log, message);
      });
    }
    remember(history);
    if (snapshot.busy) {
      var busyMessage = div(
        'orch-ai-msg orch-ai-bot orch-ai-typing',
        translate('orch.ai.composing') + ' '
      );
      var dot = doc().createElement('span');
      dot.className = 'orch-dot';
      dot.setAttribute('aria-hidden', 'true');
      busyMessage.appendChild(dot);
      log.appendChild(busyMessage);
    }
    log.scrollTop = log.scrollHeight;
    return true;
  }

  return Object.freeze({ render: render });
}

