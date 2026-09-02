/* ===== migrated source: orchestration-store-browser-presentation.js ===== */
/* Pure saved-flow list formatting and safe HTML projection. */

function createOrchestrationStoreBrowserPresentation(options) {
  options = options || {};
  var escape = options.escape || function (value) {
    return String(value == null ? '' : value);
  };
  var translate = options.translate || function (key) { return key; };
  var icons = options.icons || {};

  function updatedTime(value) {
    var timestamp = Number(value);
    if (!Number.isSafeInteger(timestamp) || timestamp <= 0) return null;
    var date = new Date(timestamp);
    if (!Number.isFinite(date.getTime())) return null;
    var now = typeof options.now === 'function' ? options.now() : Date.now();
    var elapsed = Math.max(0, Number(now) - timestamp);
    var minutes = Math.floor(elapsed / 60000);
    var relative;
    if (minutes < 1) {
      relative = translate('orch.load.updatedJustNow');
    } else if (minutes < 60) {
      relative = translate('orch.load.updatedMinutesAgo', { n: minutes });
    } else if (minutes < 1440) {
      relative = translate('orch.load.updatedHoursAgo', {
        n: Math.floor(minutes / 60),
      });
    } else if (minutes < 10080) {
      relative = translate('orch.load.updatedDaysAgo', {
        n: Math.floor(minutes / 1440),
      });
    } else {
      try { relative = date.toLocaleDateString(); }
      catch (_error) { relative = ''; }
    }
    var absolute = '';
    try { absolute = date.toLocaleString(); }
    catch (_error) { absolute = relative; }
    return {
      relative: String(relative || ''),
      absolute: String(absolute || ''),
      datetime: date.toISOString(),
    };
  }

  function messageHtml(key, params) {
    return '<div class="orch-load-empty" role="status">'
      + escape(translate(key, params)) + '</div>';
  }

  function rowsHtml(entries, currentId) {
    return entries.map(function (entry, index) {
      var count = Number.isSafeInteger(entry.nodeCount) && entry.nodeCount >= 0
        ? entry.nodeCount
        : (entry.definition && (entry.definition.nodes || []).length || 0);
      var isCurrent = currentId != null && entry.id === currentId;
      var name = String(entry.name || translate('orch.load.untitled'));
      var updated = updatedTime(entry.updatedAt);
      var updatedHtml = updated && updated.relative
        ? '<time datetime="' + escape(updated.datetime) + '" title="'
          + escape(translate('orch.load.updatedTitle', {
            time: updated.absolute,
          })) + '">' + escape(updated.relative) + '</time>'
        : '';
      return '<div class="orch-load-row' + (isCurrent ? ' is-current' : '')
        + '" role="presentation'
        + '"><button type="button" class="orch-load-pick" role="menuitem" '
        + (isCurrent ? 'aria-current="true" ' : '')
        + 'data-load-index="' + index + '">'
        + '<span class="orch-load-title"><span class="orch-load-name">'
        + escape(name) + '</span>'
        + (isCurrent ? '<span class="orch-load-current">'
          + escape(translate('orch.load.current')) + '</span>' : '')
        + '</span>'
        + '<span class="orch-load-meta"><span>'
        + escape(translate('orch.load.nodes', { n: count })) + '</span>'
        + updatedHtml + '</span></button>'
        + '<button type="button" class="orch-load-del" role="menuitem" '
        + 'data-delete-index="' + index + '" title="'
        + escape(translate('orch.load.delete')) + '" aria-label="'
        + escape(translate('orch.load.deleteNamed', { name: name })) + '">'
        + (icons.reject || '') + '</button></div>';
    }).join('');
  }

  return {
    messageHtml: messageHtml,
    rowsHtml: rowsHtml,
    updatedTime: updatedTime,
  };
}

