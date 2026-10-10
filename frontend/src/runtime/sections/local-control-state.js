/* ===== migrated source: local-control-state.js ===== */
/* Responsibility: retained Local Control badge projection only.
 * Entry: _lcUpdateBadge, called when conversation tool flags change.
 * Dependencies: retained browser/desktop wire flags and the optional
 * demand-loaded LocalControlPresentationState reachability port. */

function _lcBadgeT(key, fallback) {
  if (typeof t === 'function') {
    var translated = t(key);
    if (translated && translated !== key) return translated;
  }
  return fallback;
}

/* ONE summary badge on the merged toolbar entry, counting whichever
 * capabilities are on. The merged row is `active` when either is.
 *
 * Enabled is not the same as working: an enabled capability confirmed
 * unreachable ships zero tools. The presentation owner publishes its live
 * reachability object only after that chunk is requested; absence means
 * "not probed", which must never be presented as broken. */
function _lcUpdateBadge() {
  var bOn = (typeof browserEnabled !== 'undefined' && browserEnabled);
  var dOn = (typeof desktopEnabled !== 'undefined' && desktopEnabled);
  var n = (bOn ? 1 : 0) + (dOn ? 1 : 0);
  var presentation = runtimeScope.LocalControlPresentationState;
  var reach = (presentation && typeof presentation.reach === 'object'
      && presentation.reach)
    ? presentation.reach : { browser: null, desktop: null };
  var stale = ((bOn && reach.browser === false) ? 1 : 0)
            + ((dOn && reach.desktop === false) ? 1 : 0);
  var badge = document.getElementById('localControlBadge');
  if (badge) {
    badge.textContent = n > 0 ? String(n) : '';
    badge.style.display = n > 0 ? '' : 'none';
    badge.classList.toggle('visible', n > 0);
    badge.classList.toggle('lc-badge-stale', stale > 0);
    if (stale > 0) {
      badge.title = _lcBadgeT('local.badgeStale',
        '已开启，但当前未连接 —— AI 实际拿不到这些工具。');
    } else {
      badge.removeAttribute('title');
    }
  }
  var row = document.getElementById('localControlToggle');
  if (row) {
    row.classList.toggle('active', n > 0);
    row.classList.toggle('lc-row-stale', stale > 0);
    if (typeof _paintToolExposureState === 'function') {
      var available = !(reach.browser === false
                     && reach.desktop === false);
      _paintToolExposureState(row, n > 0 && stale < n, available);
    }
  }
}
