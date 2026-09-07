/* ===== migrated source: orchestration-feedback.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   orchestration-feedback.js — shared Studio/Task Mode notifications

   Owns safe toast DOM construction, readable validator details and timeout
   cleanup. Feature controllers depend on its small toast/warn interface.
   Task Mode reaches it through the explicit Studio API; orchestration.js
   retains thin global compatibility facades for older extensions.

   MUST load before orchestration.js.
   ═══════════════════════════════════════════════════════════════════ */

function createOrchestrationFeedback(options) {
  options = options || {};

  function _document() {
    return options.document || document;
  }

  function _setTimeout(callback, delay) {
    var schedule = typeof options.setTimeout === 'function'
      ? options.setTimeout : setTimeout;
    return schedule(callback, delay);
  }

  function _translate(key, params) {
    return typeof options.translate === 'function'
      ? options.translate(key, params) : key;
  }

  function toast(text, isError, opts) {
    opts = opts || {};
    var doc = _document();
    var element = doc.createElement('div');
    element.className = 'orch-toast' + (isError ? ' is-err' : '')
      + (opts.warn ? ' is-warn' : '');
    element.appendChild(doc.createTextNode(text));

    var detail = opts.detail;
    if (detail && detail.length) {
      var lines = Array.isArray(detail) ? detail : [String(detail)];
      var box = doc.createElement('div');
      box.className = 'orch-toast-detail';
      lines.forEach(function (line) {
        var row = doc.createElement('div');
        row.textContent = String(line);
        box.appendChild(row);
      });
      element.appendChild(box);
    }

    doc.body.appendChild(element);
    var dwell = opts.dwell || 2600;
    _setTimeout(function () {
      element.style.opacity = '0';
      _setTimeout(function () { element.remove(); }, 300);
    }, dwell);
    return element;
  }

  function warn(prefix, warnings, isError) {
    var values = Array.isArray(warnings)
      ? warnings : (warnings == null ? [] : [warnings]);
    var issues = typeof options.issueMessages === 'function'
      ? options.issueMessages(values)
      : values.filter(function (warning) { return warning; });
    if (!issues.length) return toast(prefix, !!isError);
    var count = issues.length;
    var noun = isError ? ' issue' : ' warning';
    var countKey = isError
      ? 'orch.feedback.issueCount' : 'orch.feedback.warningCount';
    var countText = _translate(countKey, { count: count });
    if (!countText || countText === countKey) {
      countText = count + noun + (count > 1 ? 's' : '');
    }
    return toast(
      prefix + ' — ' + countText,
      !!isError,
      { warn: !isError, detail: issues, dwell: 6500 }
    );
  }

  return { toast: toast, warn: warn };
}

