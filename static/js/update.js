/* ═══════════════════════════════════════════════════════════════════
   update.js — Self-update topbar button

   Renders a small "check for updates" button in the topbar. On load it
   silently checks the official GitHub repo for a newer release tag and,
   when one exists, shows a dot badge. Clicking opens a dialog that
   reports current → latest, then lets an admin apply the update
   (git pull --ff-only) and explicitly restart.

   ALL backend calls go through window.Api.update.* (see api.js) — this
   file never calls fetch() directly (CLAUDE.md §3.2.0).

   Concatenated by lib/js_bundler.py — symbols share window scope.
   ═══════════════════════════════════════════════════════════════════ */

var _updateState = null;   // last /update/check payload
var _updateBusy = false;

/** Silent background check — called once at boot. Populates the badge. */
async function _updateBootCheck() {
  try {
    const r = await Api.update.check();
    if (!r || !r.ok) return;
    _updateState = r;
    _renderUpdateBadge();
  } catch (e) {
    if (typeof debugLog === 'function') debugLog('[Update] boot check failed: ' + (e && e.message), 'warning');
  }
}

/** Toggle the "update available" dot on the topbar button. */
function _renderUpdateBadge() {
  const btn = document.getElementById('updateBtn');
  if (!btn) return;
  const avail = !!(_updateState && _updateState.update_available);
  btn.classList.toggle('has-update', avail);
  if (avail && _updateState) {
    btn.title = t('update.availableTitle').replace('%s', _updateState.latest || '');
  } else {
    btn.title = t('update.checkTitle');
  }
}

/** Open the update dialog. Re-checks live so the dialog is never stale. */
async function openUpdateDialog() {
  const modal = document.getElementById('updateModal');
  const body = document.getElementById('updateModalBody');
  if (!modal || !body) return;
  modal.classList.add('open');
  body.innerHTML = '<p class="upd-loading">' + escapeHtml(t('update.checking')) + '</p>';

  let r = null;
  try {
    r = await Api.update.check();
  } catch (e) {
    if (typeof debugLog === 'function') debugLog('[Update] check failed: ' + (e && e.message), 'error');
  }
  if (!r || !r.ok) {
    body.innerHTML = '<p class="upd-error">' + escapeHtml(t('update.checkFailed')) + '</p>';
    return;
  }
  _updateState = r;
  _renderUpdateBadge();
  _renderUpdateDialogBody(r);
}

function _renderUpdateDialogBody(r) {
  const body = document.getElementById('updateModalBody');
  if (!body) return;

  const rows = [];
  rows.push('<div class="upd-row"><span class="upd-label">' + escapeHtml(t('update.current')) +
            '</span><span class="upd-val">v' + escapeHtml(r.current || '?') + '</span></div>');
  rows.push('<div class="upd-row"><span class="upd-label">' + escapeHtml(t('update.latest')) +
            '</span><span class="upd-val">' + (r.latest ? 'v' + escapeHtml(r.latest) : '—') + '</span></div>');

  let actionHtml = '';
  if (!r.git_available) {
    // Not a git checkout — in-place update isn't possible.
    actionHtml = '<p class="upd-warn">' + escapeHtml(t('update.noGit')) + '</p>';
  } else if (!r.update_available) {
    actionHtml = '<p class="upd-uptodate">' + escapeHtml(t('update.upToDate')) + '</p>';
  } else if (r.dirty) {
    // Genuine tracked-source edits block the pull. List a few, never auto-stash.
    const sample = (r.blocking || []).slice(0, 8).map(escapeHtml).join('<br>');
    actionHtml =
      '<p class="upd-warn">' + escapeHtml(t('update.dirty')) + '</p>' +
      (sample ? '<pre class="upd-files">' + sample + '</pre>' : '');
  } else {
    actionHtml =
      '<p class="upd-ready">' + escapeHtml(t('update.ready')) + '</p>' +
      '<button class="upd-apply-btn" id="updateApplyBtn" onclick="applyUpdate()">' +
      escapeHtml(t('update.applyBtn')) + '</button>';
  }

  body.innerHTML =
    '<div class="upd-rows">' + rows.join('') + '</div>' +
    '<div class="upd-action" id="updateActionArea">' + actionHtml + '</div>';
}

/** Run git pull. On success that changed files, prompt for restart. */
async function applyUpdate() {
  if (_updateBusy) return;
  _updateBusy = true;
  const area = document.getElementById('updateActionArea');
  const btn = document.getElementById('updateApplyBtn');
  if (btn) { btn.disabled = true; btn.textContent = t('update.applying'); }

  let r = null;
  try {
    r = await Api.update.apply();
  } catch (e) {
    // 409 (dirty / no-git / deps-failed) and 5xx surface here as ApiError.
    const b = (e && e.body) || {};
    const msg = b.error || (e && e.message) || t('update.applyFailed');
    // Special case: code WAS pulled but `pip install` failed (changed=true).
    // Offer a restart anyway — the running interpreter's missing imports
    // are recoverable via the launcher — but make the dep failure explicit.
    if (b.changed && b.deps_changed && !b.deps_installed) {
      _renderDepsFailed(b);
    } else if (area) {
      area.innerHTML = '<p class="upd-error">' + escapeHtml(String(msg)) + '</p>';
    }
    if (typeof debugLog === 'function') debugLog('[Update] apply failed: ' + msg, 'error');
    _updateBusy = false;
    return;
  }

  _updateBusy = false;
  if (!r || !r.ok) {
    const msg = (r && r.error) || t('update.applyFailed');
    if (area) area.innerHTML = '<p class="upd-error">' + escapeHtml(String(msg)) + '</p>';
    return;
  }

  if (!r.changed) {
    if (area) area.innerHTML = '<p class="upd-uptodate">' + escapeHtml(t('update.upToDate')) + '</p>';
    return;
  }

  // Pulled new code — needs an explicit restart to take effect.
  const depsNote = (r.deps_changed && r.deps_installed)
    ? '<p class="upd-uptodate">' + escapeHtml(t('update.depsInstalled')) + '</p>'
    : '';
  if (area) {
    area.innerHTML =
      '<p class="upd-ready">' + escapeHtml(t('update.pulled').replace('%s', 'v' + (r.new_version || ''))) + '</p>' +
      depsNote +
      '<button class="upd-apply-btn" id="updateRestartBtn" onclick="restartServer()">' +
      escapeHtml(t('update.restartBtn')) + '</button>' +
      '<p class="upd-hint">' + escapeHtml(t('update.restartHint')) + '</p>';
  }
}

/** Code was pulled but pip install failed — explain + still allow restart. */
function _renderDepsFailed(b) {
  const area = document.getElementById('updateActionArea');
  if (!area) return;
  const detail = (b.deps_detail || '').slice(-600);
  area.innerHTML =
    '<p class="upd-warn">' + escapeHtml(t('update.depsFailed').replace('%s', 'v' + (b.new_version || ''))) + '</p>' +
    (detail ? '<pre class="upd-files">' + escapeHtml(detail) + '</pre>' : '') +
    '<button class="upd-apply-btn" id="updateRestartBtn" onclick="restartServer()">' +
    escapeHtml(t('update.restartBtn')) + '</button>' +
    '<p class="upd-hint">' + escapeHtml(t('update.restartHint')) + '</p>';
}

/** Explicit restart — re-execs the server, then waits for it to come back. */
async function restartServer() {
  const btn = document.getElementById('updateRestartBtn');
  if (btn) { btn.disabled = true; btn.textContent = t('update.restarting'); }
  try {
    await Api.update.restart();
  } catch (e) {
    if (typeof debugLog === 'function') debugLog('[Update] restart request failed: ' + (e && e.message), 'warning');
  }
  showToast('🔄', t('update.restarting'), t('update.restartWait'), 8000);
  _waitForServerBack(0);
}

/** Poll /api/health until the server answers again, then reload. */
function _waitForServerBack(attempt) {
  if (attempt > 40) {  // ~80s ceiling
    showToast('⚠️', t('update.restartTimeout'), '', 6000);
    return;
  }
  setTimeout(async function () {
    let ok = false;
    try {
      const resp = await Api.health.check();
      ok = !!(resp && resp.ok);
    } catch (e) { ok = false; }
    if (ok) {
      window.location.reload();
    } else {
      _waitForServerBack(attempt + 1);
    }
  }, 2000);
}

function closeUpdateModal() {
  const modal = document.getElementById('updateModal');
  if (modal) modal.classList.remove('open');
}

// Boot check shortly after load (don't block first paint / chat boot).
if (typeof window !== 'undefined') {
  window.addEventListener('load', function () {
    setTimeout(_updateBootCheck, 3000);
  });
}
