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
var _updateTaskId = null;  // current apply task; push frames keyed on it
var _updateStageEls = null;// {fetch, pull, deps} → <li> step elements
var _updateStageState = {};   // stage → last frame seen (replays on modal re-open)
var _updateDoneResult = null; // terminal apply result awaiting the restart decision

// Ordered stages of an apply run, rendered as the live stepper.
var _UPDATE_STAGES = ['fetch', 'pull', 'deps'];

var _pendingRestartToastFor = null;  // version already toasted (once per page load)

/** Silent background check — called once at boot. Populates the badge.
 *  ALSO the reload-robust completion path: a download outlives the page, so
 *  the server persists the apply outcome — a landed update awaiting restart
 *  toasts the restart offer here even if the original page is long gone, and
 *  a still-running download gets its push subscription re-attached so the
 *  terminal frame still fires (and toasts) without the user re-opening the
 *  dialog. */
async function _updateBootCheck() {
  try {
    const r = await Api.update.check();
    if (!r || !r.ok) return;
    _updateState = r;
    _renderUpdateBadge();
    if (r.pending_restart && r.pending_restart.new_version) {
      _updateDoneResult = _doneResultFromPending(r.pending_restart);
      if (_pendingRestartToastFor !== r.pending_restart.new_version) {
        _pendingRestartToastFor = r.pending_restart.new_version;
        showToast('✅', t('update.bgDoneTitle').replace('%s', 'v' + r.pending_restart.new_version),
          t('update.bgDoneBody'), 30000,
          { hint: t('update.bgDoneHint'), onClick: function () { restartServer(); } });
      }
      return;
    }
    if (r.apply_in_progress && r.apply_in_progress.task_id && !_updateBusy) {
      _updateBusy = true;
      _updateTaskId = r.apply_in_progress.task_id;
      _subscribeUpdateProgress(r.apply_in_progress.task_id);
    }
  } catch (e) {
    if (typeof debugLog === 'function') debugLog('[Update] boot check failed: ' + (e && e.message), 'warning');
  }
}

/** Shape a /update/check pending_restart projection into the done-frame
 *  result dict the terminal-card renderer already understands. */
function _doneResultFromPending(p) {
  const depsFailed = !!(p.deps_changed && !p.deps_installed);
  return { ok: !depsFailed, changed: true, needs_restart: true,
           new_version: p.new_version || '', old_version: p.old_version || '',
           deps_changed: !!p.deps_changed, deps_installed: !!p.deps_installed,
           error: p.error || '', detail: p.detail || '' };
}

/** Render the terminal apply card: restart-to-apply on success, the
 *  deps-failed variant when code landed but pip install failed. */
function _renderDoneCard(r) {
  if (!r.ok) { _renderDepsFailed(r); return; }
  _renderUpdateDone(r);
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
  _renderSettingsUpdatePill();
}

/** Mirror the "update available" state onto the Settings › General card.
 *  The update entry point now lives there (the topbar button is a hidden
 *  stub), so the pill must follow the same state rather than being computed
 *  a second time — one source of truth, two surfaces. Safe to call before
 *  the settings panel is in the DOM; it simply no-ops. */
function _renderSettingsUpdatePill() {
  const pill = document.getElementById('settingsUpdatePill');
  if (!pill) return;
  pill.style.display = (_updateState && _updateState.update_available) ? '' : 'none';
}

/** Open the update dialog. Re-checks live so the dialog is never stale.
 *  EXCEPTION: an in-flight apply or a finished download awaiting the restart
 *  decision owns the modal body — restore THAT card instead of re-running the
 *  check, which would clobber the live stepper / restart prompt with the
 *  "checking…" spinner (the apply itself keeps running server-side, so the
 *  user would lose all visibility into it). */
async function openUpdateDialog() {
  const modal = document.getElementById('updateModal');
  if (!modal) return;
  modal.classList.add('open');
  if (_updateBusy) {
    _ensureUpdateScaffold();
    _renderUpdateStepper();
    _replayUpdateStageState();
    return;
  }
  if (_updateDoneResult) {
    _ensureUpdateScaffold();
    _renderDoneCard(_updateDoneResult);
    return;
  }
  _runUpdateCheck();
  _renderPendingLifecycleApprovals();
}

/** True when the update modal is currently visible. */
function _updateModalOpen() {
  const m = document.getElementById('updateModal');
  return !!(m && m.classList.contains('open'));
}

/** Ensure #updateModalBody carries the action-area scaffold the stepper and
 *  the done card render into. Closing the modal only hides it (the scaffold
 *  survives), but a fresh page or an interim re-render may have removed it —
 *  recreate it so a re-opened dialog never goes blank mid-apply /
 *  pending-restart. */
function _ensureUpdateScaffold() {
  const body = document.getElementById('updateModalBody');
  if (!body || document.getElementById('updateActionArea')) return;
  body.innerHTML =
    (_updateState ? _updateHeroHtml(_updateState) : '') +
    '<div class="upd-action" id="updateActionArea"></div>' +
    '<div id="updateLifecycleApprovals" style="display:none"></div>';
}

/** Run the version check with a visible spinner + bounded timeout.
 *  The check hits GitHub's tags API server-side; on a slow/blocked
 *  network we must NOT sit on a bare label forever — show a spinner,
 *  cap the wait, and surface the CONCRETE failure reason (never a vague
 *  "try again later"). */
async function _runUpdateCheck() {
  const body = document.getElementById('updateModalBody');
  if (!body) return;
  // A restart or an in-flight apply owns the modal body — re-opening the
  // dialog (or a retry click) must not paint the "checking…" spinner over
  // the live progress card.
  if (_restartActive || _updateBusy) return;
  body.innerHTML =
    '<div class="upd-checking-wrap"><span class="upd-big-spin"></span><span>' +
    escapeHtml(t('update.checking')) + '</span></div>';

  let r = null;
  let failure = null;  // {title, reason} — set on any failure
  try {
    // Bounded wait — the default 30s feels frozen. 12s is plenty for a
    // reachable GitHub; beyond that we surface a reason instead of hanging.
    // onError:'throw' (overriding api.js's default null) so we can read the
    // real cause — backend down vs HTTP error vs timeout — and say so.
    r = await Api.update.check({ timeout: 12000, onError: 'throw' });
  } catch (e) {
    failure = _classifyCheckError(e);
    if (typeof debugLog === 'function') debugLog('[Update] check failed: ' + (e && e.message), 'error');
  }

  // The request succeeded at the HTTP layer but the backend could not reach
  // GitHub — it tells us the concrete cause via error_kind/error_detail.
  if (!failure && r && r.error_kind) {
    failure = _githubFailureReason(r.error_kind, r.error_detail);
  }
  // Defensive: a malformed/empty payload with no explicit error.
  if (!failure && (!r || !r.ok)) {
    failure = { title: t('update.checkFailTitle'), reason: t('update.errUnknown') };
  }

  if (failure) {
    _renderCheckError(failure);
    return;
  }
  _updateState = r;
  _renderUpdateBadge();
  _renderUpdateDialogBody(r);
}

/** Map a thrown ApiError (backend side) to a concrete {title, reason}.
 *  Distinguishes "backend unreachable" / "request timed out" / "backend
 *  returned HTTP N" so the user always learns the real cause. */
function _classifyCheckError(e) {
  const title = t('update.checkFailTitle');
  // AbortController timeout (api.js) surfaces as AbortError or code 'timeout'.
  const code = e && e.code;
  const name = e && e.name;
  if (code === 'timeout' || name === 'AbortError') {
    return { title, reason: t('update.errTimeout') };
  }
  // Network-layer failure reaching our OWN backend (server down / restarting).
  if (code === 'network' || (typeof e !== 'undefined' && e instanceof TypeError)) {
    return { title, reason: t('update.errBackend') };
  }
  // HTTP error from the backend route itself (e.g. 500/502/auth).
  if (e && typeof e.status === 'number' && e.status > 0) {
    return { title, reason: t('update.errBackendHttp').replace('%s', String(e.status)) };
  }
  return { title, reason: t('update.errBackend') };
}

/** Map the backend's GitHub-side error_kind to localized {title, reason}. */
function _githubFailureReason(kind, detail) {
  const title = t('update.checkFailTitle');
  const map = {
    network: t('update.errNetwork'),
    rate_limited: t('update.errRateLimited'),
    http: t('update.errHttp').replace('%s', escapeHtml(String(detail || '').replace(/^HTTP\s*/i, '').split(' ')[0] || '')),
    parse: t('update.errParse'),
    no_tags: t('update.errNoTags'),
  };
  return { title, reason: map[kind] || t('update.errUnknown'), detail: detail };
}

/** Render a concrete error card: heading + the real reason + retry.
 *  Always names WHY the check failed — backend down, timeout, GitHub
 *  unreachable, rate-limited, etc. */
function _renderCheckError(failure) {
  const body = document.getElementById('updateModalBody');
  if (!body) return;
  const icon =
    '<span class="upd-err-icon"><svg width="22" height="22" viewBox="0 0 24 24" ' +
    'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
    'stroke-linejoin="round"><circle cx="12" cy="12" r="10"/>' +
    '<line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg></span>';
  body.innerHTML =
    '<div class="upd-err-card">' +
      '<div class="upd-err-head">' + icon +
        '<div class="upd-err-title">' + escapeHtml(failure.title || t('update.checkFailTitle')) + '</div>' +
      '</div>' +
      '<p class="upd-err-reason">' + escapeHtml(failure.reason || t('update.errUnknown')) + '</p>' +
      '<button class="upd-retry-btn" onclick="_runUpdateCheck()">' +
        '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
        'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
        '<path d="M23 4v6h-6"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>' +
        escapeHtml(t('update.retry')) + '</button>' +
    '</div>';
}

/** current → latest version hero card. */
function _updateHeroHtml(r) {
  const upToDate = !r.update_available;
  const cur = 'v' + escapeHtml(r.current || '?');
  const latest = r.latest ? 'v' + escapeHtml(r.latest) : '—';
  const arrow = '<span class="upd-arrow"><svg width="20" height="20" viewBox="0 0 24 24" ' +
    'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
    'stroke-linejoin="round"><line x1="5" y1="12" x2="19" y2="12"/>' +
    '<polyline points="12 5 19 12 12 19"/></svg></span>';
  return '<div class="upd-hero' + (upToDate ? ' up-to-date' : '') + '">' +
    '<div class="upd-ver"><span class="upd-ver-label">' + escapeHtml(t('update.current')) +
    '</span><span class="upd-ver-num">' + cur + '</span></div>' +
    arrow +
    '<div class="upd-ver latest"><span class="upd-ver-label">' + escapeHtml(t('update.latest')) +
    '</span><span class="upd-ver-num">' + latest + '</span></div>' +
  '</div>';
}

function _renderUpdateDialogBody(r) {
  const body = document.getElementById('updateModalBody');
  if (!body) return;

  // Server-persisted apply outcomes outrank the fresh-check view: a landed
  // update awaiting restart shows the restart card; a download still running
  // (started before a page reload) re-attaches the live stepper — otherwise
  // the user would be offered a SECOND, duplicate 50MB+ download.
  if (r.pending_restart && r.pending_restart.new_version) {
    _updateDoneResult = _doneResultFromPending(r.pending_restart);
    body.innerHTML =
      _updateHeroHtml(r) +
      '<div class="upd-action" id="updateActionArea"></div>' +
      '<div id="updateLifecycleApprovals" style="display:none"></div>';
    _renderDoneCard(_updateDoneResult);
    return;
  }
  if (r.apply_in_progress && r.apply_in_progress.task_id && !_updateBusy) {
    _updateBusy = true;
    _updateStageState = {};
    _updateDoneResult = null;
    body.innerHTML =
      _updateHeroHtml(r) +
      '<div class="upd-action" id="updateActionArea"></div>' +
      '<div id="updateLifecycleApprovals" style="display:none"></div>';
    _renderUpdateStepper();
    _subscribeUpdateProgress(r.apply_in_progress.task_id);
    return;
  }

  let actionHtml = '';
  if (!r.update_available) {
    actionHtml = '<div class="upd-badge ok"><span class="upd-badge-icon">' +
      '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">' +
      '<polyline points="20 6 9 17 4 12"/></svg></span><span>' +
      escapeHtml(t('update.upToDate').replace(/^✅\s*/, '')) + '</span></div>';
  } else if (r.dirty) {
    // Genuine tracked-source edits block the pull. List a few, never auto-stash.
    const sample = (r.blocking || []).slice(0, 8).map(escapeHtml).join('<br>');
    actionHtml =
      '<div class="upd-badge warn"><span class="upd-badge-icon"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg></span><span>' +
      escapeHtml(t('update.dirty')) + '</span></div>' +
      (sample ? '<pre class="upd-files">' + sample + '</pre>' : '');
  } else {
    // A non-git deployment (exported copy / zip) updates via a downloaded
    // release-tarball overlay rather than git pull. Note the method so the
    // user understands the one limitation (can't delete files removed upstream).
    const methodNote = (r.update_method === 'tarball')
      ? '<p class="upd-hint">' + escapeHtml(t('update.tarballNote')) + '</p>'
      : '';
    actionHtml =
      '<p class="upd-ready">' + escapeHtml(t('update.ready')) + '</p>' +
      methodNote +
      '<button class="upd-apply-btn" id="updateApplyBtn" onclick="applyUpdate()">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
      'stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/>' +
      '<polyline points="7 10 12 15 17 10"/><path d="M5 21h14"/></svg>' +
      escapeHtml(t('update.applyBtn')) + '</button>';
  }

  body.innerHTML =
    _updateHeroHtml(r) +
    '<div class="upd-action" id="updateActionArea">' + actionHtml + '</div>' +
    '<div id="updateLifecycleApprovals" style="display:none"></div>';
  _renderPendingLifecycleApprovals();
}

/** Render the live stepper (one row per stage) into the action area. */
function _renderUpdateStepper() {
  const area = document.getElementById('updateActionArea');
  if (!area) return;
  const isTarball = !!(_updateState && _updateState.update_method === 'tarball');
  const labels = {
    fetch: isTarball ? t('update.step.fetchDl') : t('update.step.fetch'),
    pull: isTarball ? t('update.step.pullOverlay') : t('update.step.pull'),
    deps: t('update.step.deps'),
  };
  const items = _UPDATE_STAGES.map(function (stage) {
    return '<li class="upd-step" data-stage="' + stage + '">' +
      '<span class="upd-step-dot"></span>' +
      '<span class="upd-step-label">' + escapeHtml(labels[stage]) + '</span>' +
      '<span class="upd-step-detail"></span></li>';
  }).join('');
  area.innerHTML = '<ul class="upd-stepper">' + items + '</ul>' +
    '<p class="upd-hint">' + escapeHtml(t('update.bgHint')) + '</p>';
  _updateStageEls = {};
  _UPDATE_STAGES.forEach(function (stage) {
    _updateStageEls[stage] = area.querySelector('.upd-step[data-stage="' + stage + '"]');
  });
}

/** Apply a {stage,status,detail,pct,loaded,total,speed} frame to the stepper.
 *  A stage may report determinate progress (fetch download with a
 *  Content-Length → pct 0-100) or indeterminate activity (pip install lines,
 *  git object counting with no total). We render a thin per-step bar so a
 *  long-running stage NEVER looks frozen: determinate fills to pct, otherwise
 *  it shows an animated indeterminate sweep. */
function _applyStageFrame(frame) {
  // Remember the last frame per stage so the stepper can be rebuilt exactly
  // when the modal is closed mid-apply and re-opened later.
  if (frame && frame.stage) _updateStageState[frame.stage] = frame;
  if (!_updateStageEls) return;
  const el = _updateStageEls[frame.stage];
  if (!el) return;
  el.classList.remove('is-active', 'is-done', 'is-error');
  if (frame.status === 'active') {
    el.classList.add('is-active');
  } else if (frame.status === 'done') {
    el.classList.add('is-done');
    _setStepBar(el, null, false);  // clear the bar on completion
  } else if (frame.status === 'skip') {
    el.classList.add('is-done');
    _setStepBar(el, null, false);
    const lbl = el.querySelector('.upd-step-label');
    if (lbl) lbl.textContent = t('update.step.depsSkip');
  } else if (frame.status === 'error') {
    el.classList.add('is-error');
    _setStepBar(el, null, false);
  }
  const det = el.querySelector('.upd-step-detail');
  if (det && frame.detail && frame.status !== 'error') det.textContent = frame.detail;

  // Live progress bar while a stage is active.
  if (frame.status === 'active') {
    if (typeof frame.pct === 'number' && frame.pct >= 0) {
      _setStepBar(el, Math.max(0, Math.min(100, frame.pct)), false);
    } else {
      // No total → indeterminate sweep (still visibly "working").
      _setStepBar(el, null, true);
    }
  }
}

/** Ensure a stage row carries a <div class="upd-step-bar"> and drive it.
 *  pct=number → determinate width; indeterminate=true → animated sweep;
 *  pct=null & indeterminate=false → remove the bar entirely. */
function _setStepBar(el, pct, indeterminate) {
  let wrap = el.querySelector('.upd-step-bar');
  if (pct === null && !indeterminate) {
    if (wrap) wrap.remove();
    return;
  }
  if (!wrap) {
    wrap = document.createElement('div');
    wrap.className = 'upd-step-bar';
    wrap.innerHTML = '<div class="upd-step-bar-fill"></div>';
    el.appendChild(wrap);
  }
  const fill = wrap.querySelector('.upd-step-bar-fill');
  if (!fill) return;
  if (indeterminate) {
    wrap.classList.add('is-indeterminate');
    fill.style.width = '';
  } else {
    wrap.classList.remove('is-indeterminate');
    fill.style.width = pct.toFixed(1) + '%';
  }
}

/** Re-render a freshly-built stepper to the last known state of every
 *  stage (used when the dialog re-opens mid-apply). */
function _replayUpdateStageState() {
  _UPDATE_STAGES.forEach(function (stage) {
    const fr = _updateStageState[stage];
    if (fr) _applyStageFrame(fr);
  });
}

/** Kick off the update. The backend runs it in a background thread and
 *  streams stage progress over the 'update' push channel; we render a live
 *  stepper and act on the terminal 'done' frame. This keeps the modal
 *  responsive no matter how long git pull / pip install takes. The modal may
 *  be CLOSED at any point — the download continues server-side regardless and
 *  the terminal frame raises a toast (see _onUpdateDone). */
async function applyUpdate() {
  if (_updateBusy) return;
  _updateBusy = true;
  _updateStageState = {};
  _updateDoneResult = null;

  // Swap the action area to the live stepper immediately so the UI never
  // appears frozen between the click and the first push frame.
  _renderUpdateStepper();
  if (_updateStageEls && _updateStageEls.fetch) {
    _updateStageEls.fetch.classList.add('is-active');
  }

  let r = null;
  try {
    r = await Api.update.apply();
  } catch (e) {
    const msg = (e && e.message) || t('update.applyStartFailed');
    _showUpdateError(msg);
    if (typeof debugLog === 'function') debugLog('[Update] apply start failed: ' + msg, 'error');
    _updateBusy = false;
    return;
  }

  if (!r || !r.taskId) {
    _showUpdateError(t('update.applyStartFailed'));
    _updateBusy = false;
    return;
  }

  _updateTaskId = r.taskId;
  _subscribeUpdateProgress(r.taskId);
}

/** Subscribe to the apply task's push channel; route stage + done frames. */
function _subscribeUpdateProgress(taskId) {
  if (typeof pushSubscribe !== 'function') {
    // No WebSocket layer — without it we can't observe progress. Tell the
    // user the update is running server-side and to refresh shortly.
    _showUpdateError(t('update.applyStartFailed'));
    _updateBusy = false;
    return;
  }
  // Safety net: if no frame ever lands (server died / channel wedged),
  // surface a timeout instead of an eternal spinner. When the modal is closed
  // the in-dialog error is invisible — raise a toast as well.
  const _onWatchdog = function () {
    _finishUpdateSub(taskId);
    _showUpdateError(t('update.applyTimeout'));
    if (!_updateModalOpen()) {
      showToast('⚠️', t('update.bgFailTitle'), t('update.applyTimeout'), 8000);
    }
    _updateBusy = false;
  };
  let watchdog = setTimeout(_onWatchdog, 15 * 60 * 1000);

  const handler = function (frame) {
    if (!frame || frame.taskId !== taskId) return;
    clearTimeout(watchdog);
    watchdog = setTimeout(_onWatchdog, 15 * 60 * 1000);

    if (frame.type === 'stage') {
      _applyStageFrame(frame);
      return;
    }
    if (frame.type === 'done') {
      clearTimeout(watchdog);
      _finishUpdateSub(taskId);
      _updateBusy = false;
      _onUpdateDone(frame);
    }
  };
  _updateActiveHandler = handler;
  pushSubscribe('update', taskId, handler);
}

var _updateActiveHandler = null;
function _finishUpdateSub(taskId) {
  if (typeof pushUnsubscribe === 'function' && _updateActiveHandler) {
    pushUnsubscribe('update', taskId, _updateActiveHandler);
  }
  _updateActiveHandler = null;
}

/** Terminal 'done' frame — mirror the apply_update() result dict.
 *  The modal may be closed (background apply): the restart decision is then
 *  surfaced as a toast whose click fires the same restart flow the in-dialog
 *  button would, and the result is kept in _updateDoneResult so re-opening
 *  the dialog still shows the restart card. */
function _onUpdateDone(r) {
  const area = document.getElementById('updateActionArea');
  _updateStageState = {};   // terminal — no live frames left to replay
  if (!r.ok) {
    // Code WAS pulled but pip install failed → still offer a restart.
    if (r.changed && r.deps_changed && !r.deps_installed) {
      _updateDoneResult = r;
      _renderDepsFailed(r);
    } else {
      _showUpdateError(r.error || t('update.applyFailed'), r.detail || r.deps_detail || '');
    }
    if (!_updateModalOpen()) {
      showToast('⚠️', t('update.bgFailTitle'), (r.error || t('update.applyFailed')),
        8000, { hint: t('update.bgFailHint'), onClick: function () { openUpdateDialog(); } });
    }
    if (typeof debugLog === 'function') {
      debugLog('[Update] apply failed: ' + (r.error || ''), 'error');
    }
    return;
  }

  if (!r.changed) {
    if (area) {
      area.innerHTML = '<div class="upd-badge ok"><span class="upd-badge-icon">' +
        '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
        'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">' +
        '<polyline points="20 6 9 17 4 12"/></svg></span><span>' +
        escapeHtml(t('update.upToDate').replace(/^✅\s*/, '')) + '</span></div>';
    }
    return;
  }

  // Pulled new code — needs an explicit restart to take effect.
  _updateDoneResult = r;
  _renderUpdateDone(r);
  if (!_updateModalOpen()) {
    showToast('✅', t('update.bgDoneTitle').replace('%s', 'v' + (r.new_version || '')),
      t('update.bgDoneBody'), 30000,
      { hint: t('update.bgDoneHint'), onClick: function () { restartServer(); } });
  }
}

/** Render the post-download "restart to apply" card (shared by the live done
 *  frame and a re-opened dialog). */
function _renderUpdateDone(r) {
  const area = document.getElementById('updateActionArea');
  if (!area) return;
  const depsNote = (r.deps_changed && r.deps_installed)
    ? '<p class="upd-uptodate">' + escapeHtml(t('update.depsInstalled')) + '</p>'
    : '';
  area.innerHTML =
    '<p class="upd-ready">' + escapeHtml(t('update.pulled').replace('%s', 'v' + (r.new_version || ''))) + '</p>' +
    depsNote +
    '<button class="upd-apply-btn" id="updateRestartBtn" onclick="restartServer()">' +
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round"><path d="M23 4v6h-6"/>' +
    '<path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>' +
    escapeHtml(t('update.restartBtn')) + '</button>' +
    '<p class="upd-hint">' + escapeHtml(t('update.restartHint')) + '</p>';
}

/** Build a full, scrollable log block with a "copy" button.
 *  The COMPLETE text is shown verbatim (never truncated) so the operator can
 *  read and copy-paste the whole error — the reported gap was a mangled tail.
 *  The raw log is stashed on the element (dataset) so the copy button lifts
 *  the exact bytes, not the HTML-escaped/DOM-reflowed version. */
function _updateLogBlockHtml(logText) {
  const text = String(logText || '');
  if (!text) return '';
  // Base64-stash the raw log so the copy handler recovers exact bytes without
  // re-reading escaped DOM text (encodeURIComponent handles any UTF-8).
  let stash = '';
  try { stash = btoa(unescape(encodeURIComponent(text))); } catch (e) { stash = ''; }
  const copyIcon =
    '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
    '<rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>' +
    '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
  return '<div class="upd-log" data-log="' + escapeHtml(stash) + '">' +
    '<div class="upd-log-head">' +
      '<span class="upd-log-label">' + escapeHtml(t('update.logLabel')) + '</span>' +
      '<button type="button" class="upd-log-copy" onclick="_copyUpdateLog(this)">' +
        copyIcon + '<span class="upd-log-copy-txt">' + escapeHtml(t('update.copyLog')) + '</span>' +
      '</button>' +
    '</div>' +
    '<pre class="upd-files upd-log-pre">' + escapeHtml(text) + '</pre>' +
  '</div>';
}

/** Copy the full raw log of the nearest .upd-log block to the clipboard. */
function _copyUpdateLog(btn) {
  const wrap = btn && btn.closest ? btn.closest('.upd-log') : null;
  if (!wrap) return;
  let text = '';
  try { text = decodeURIComponent(escape(atob(wrap.dataset.log || ''))); }
  catch (e) { text = (wrap.querySelector('.upd-log-pre') || {}).textContent || ''; }
  const done = function () {
    const txt = btn.querySelector('.upd-log-copy-txt');
    const orig = txt ? txt.textContent : '';
    if (txt) txt.textContent = t('update.logCopied');
    btn.classList.add('copied');
    setTimeout(function () {
      btn.classList.remove('copied');
      if (txt) txt.textContent = orig;
    }, 1500);
  };
  if (typeof _safeClipboardWrite === 'function') {
    _safeClipboardWrite(text).then(done).catch(function () {});
  } else if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(function () {});
  }
}

/** Render a terminal error message in the action area.
 *  When a diagnostic ``detail`` log is available (e.g. an unexpected apply
 *  failure), show it IN FULL with a copy button so the user can paste it. */
function _showUpdateError(msg, detail) {
  const area = document.getElementById('updateActionArea');
  if (!area) return;
  area.innerHTML =
    '<p class="upd-error">' + escapeHtml(String(msg)) + '</p>' +
    _updateLogBlockHtml(detail);
}

/** Code was pulled but pip install failed — explain + still allow restart.
 *  Shows the COMPLETE dependency-install log (no truncation) with a copy
 *  button so the operator can paste the whole error verbatim. */
function _renderDepsFailed(b) {
  const area = document.getElementById('updateActionArea');
  if (!area) return;
  area.innerHTML =
    '<p class="upd-warn">' + escapeHtml(t('update.depsFailed').replace('%s', 'v' + (b.new_version || ''))) + '</p>' +
    _updateLogBlockHtml(b.deps_detail || '') +
    '<button class="upd-apply-btn" id="updateRestartBtn" onclick="restartServer()">' +
    escapeHtml(t('update.restartBtn')) + '</button>' +
    '<p class="upd-hint">' + escapeHtml(t('update.restartHint')) + '</p>';
}

// ── Restart progress model ───────────────────────────────────────────
// During an in-place re-exec there is NO server listening (the old process
// os.execv's into the new one; Hypercorn only binds after DB init + import
// validation + service startup). So we can't stream live boot logs over the
// wire. Instead we drive a determinate bar through the server's REAL boot
// phases (mirrors server.py's _boot() sequence), time-estimated, and treat
// /api/health as the single source of truth: the instant it answers we snap
// to 100% with the genuine returned version. The bar eases toward each
// phase's cap but never reaches 100% on its own — it can only *under*-report
// progress, never claim "done" before the server actually is.
var _RESTART_PHASES = [
  { key: 'shutdown', dur: 1.5, to: 12 },  // graceful drain + os.execv
  { key: 'reload',   dur: 3.0, to: 34 },  // re-import core modules
  { key: 'db',       dur: 2.5, to: 52 },  // init_db / warmup
  { key: 'imports',  dur: 3.0, to: 72 },  // critical-import validation
  { key: 'services', dur: 3.0, to: 86 },  // background workers / MCP
  { key: 'bind',     dur: 6.0, to: 94 },  // _wait_port_free + Hypercorn bind
];
var _restartT0 = 0;
var _restartRaf = null;
var _restartPoll = null;
var _restartDone = false;
// The server's bootId captured BEFORE we trigger the restart. The re-exec
// mints a fresh bootId (os.execv keeps the PID + start-time, so bootId is the
// only reliable 'a NEW process answered' signal). _restartCheckHealth only
// declares success when health returns a DIFFERENT bootId — so the OLD process
// still answering during the drain window is never mistaken for a done restart.
var _restartPreBootId = null;
// The server's codeFingerprint.digest captured BEFORE the restart. After the
// re-exec, the fresh process recomputes it from the source it actually loaded
// (HEAD + uncommitted tracked edits). Comparing pre vs post lets us tell the
// operator not just "a new process answered" (bootId) but whether that process
// loaded DIFFERENT source — i.e. whether their edits are actually live. null
// when the server can't produce one (non-git deploy) → degrade to bootId only.
var _restartPreFingerprint = null;
// True from the moment a restart is kicked off until it succeeds or times
// out. Single source of truth that the dialog-render paths consult so they
// never clobber the live restart progress card (e.g. re-opening the dialog
// or the always-live footer "Restart now" button mid-restart). Distinct from
// _restartDone, which starts false and so can't tell "not started" from "done".
var _restartActive = false;

/** Map elapsed seconds → {pct, key} along the phase timeline (ease-out per
 *  phase). The final phase holds at its cap if the server overruns, so a
 *  slow boot looks "still working" rather than falsely complete. */
function _restartProgress(elapsed) {
  var from = 0, acc = 0;
  for (var i = 0; i < _RESTART_PHASES.length; i++) {
    var ph = _RESTART_PHASES[i];
    var last = (i === _RESTART_PHASES.length - 1);
    if (elapsed < acc + ph.dur || last) {
      var t = Math.min(1, (elapsed - acc) / ph.dur);
      var eased = 1 - Math.pow(1 - t, 2);
      return { pct: Math.min(from + (ph.to - from) * eased, ph.to), key: ph.key };
    }
    acc += ph.dur; from = ph.to;
  }
  return { pct: 94, key: 'bind' };
}

/** Render the restart progress card into the dialog body. */
function _renderRestartProgress() {
  const body = document.getElementById('updateModalBody');
  if (!body) return;
  body.innerHTML =
    '<div class="upd-restart" id="updRestartCard">' +
      '<div class="upd-restart-head">' +
        '<span class="upd-restart-spin" id="updRestartSpin"></span>' +
        '<div class="upd-restart-headtext">' +
          '<div class="upd-restart-title" id="updRestartTitle">' + escapeHtml(t('update.restartTitle')) + '</div>' +
          '<div class="upd-restart-sub">' + escapeHtml(t('update.restartSub')) + '</div>' +
        '</div>' +
      '</div>' +
      '<div class="upd-restart-bar"><div class="upd-restart-fill" id="updRestartFill"></div></div>' +
      '<div class="upd-restart-foot">' +
        '<span class="upd-restart-phase" id="updRestartPhase">' + escapeHtml(t('update.phase.shutdown')) + '</span>' +
        '<span class="upd-restart-pct" id="updRestartPct">0%</span>' +
      '</div>' +
    '</div>';
}

/** Animation frame: advance the bar from the phase timeline. */
function _restartAnimate() {
  if (_restartDone) return;
  const elapsed = (Date.now() - _restartT0) / 1000;
  const p = _restartProgress(elapsed);
  const fill = document.getElementById('updRestartFill');
  const pctEl = document.getElementById('updRestartPct');
  const phaseEl = document.getElementById('updRestartPhase');
  if (fill) fill.style.width = p.pct.toFixed(1) + '%';
  if (pctEl) pctEl.textContent = Math.round(p.pct) + '%';
  if (phaseEl) {
    const label = t('update.phase.' + p.key);
    const secs = ' · ' + t('update.restartElapsed').replace('%s', String(Math.round(elapsed)));
    phaseEl.textContent = label + secs;
  }
  _restartRaf = requestAnimationFrame(_restartAnimate);
}

/** Snap to 100% "Back online · vX", then reload the page. */
function _restartSucceed(version, info) {
  if (_restartDone) return;
  _restartDone = true;
  _restartActive = false;
  if (_restartRaf) cancelAnimationFrame(_restartRaf);
  if (_restartPoll) clearInterval(_restartPoll);
  const fill = document.getElementById('updRestartFill');
  const pctEl = document.getElementById('updRestartPct');
  const phaseEl = document.getElementById('updRestartPhase');
  const spin = document.getElementById('updRestartSpin');
  const card = document.getElementById('updRestartCard');
  if (card) card.classList.add('is-online');
  if (spin) spin.classList.add('is-done');
  if (fill) fill.style.width = '100%';
  if (pctEl) pctEl.textContent = '100%';
  if (phaseEl) {
    phaseEl.textContent = t('update.phase.online') +
      (version ? ' · v' + version : '');
  }
  // Surface whether the fresh process actually loaded DIFFERENT source. A
  // 'unchanged' verdict is the useful safety signal: the restart succeeded
  // (new process) yet loaded byte-identical source — a heads-up that the
  // operator's edits did NOT reach the running server (e.g. edited a different
  // checkout, or the diff was reverted). 'changed' confirms edits are live.
  const verdict = _restartCodeVerdict(info);
  if (verdict === 'unchanged') {
    showToast('⚠️', t('update.codeUnchangedTitle'), t('update.codeUnchangedHint'), 8000);
    if (typeof debugLog === 'function') debugLog('[Update] restart: new process but source unchanged (edits not applied?)', 'warning');
  } else if (verdict === 'changed') {
    if (typeof debugLog === 'function') debugLog('[Update] restart: new source loaded (edits are live)', 'success');
  }
  // Brief pause so the user sees the completed state before the reload.
  setTimeout(function () { window.location.reload(); }, 750);
}

/** Restart failed to come back within the ceiling — stop and tell the user. */
function _restartTimeout() {
  if (_restartDone) return;
  _restartDone = true;
  _restartActive = false;
  if (_restartRaf) cancelAnimationFrame(_restartRaf);
  if (_restartPoll) clearInterval(_restartPoll);
  const phaseEl = document.getElementById('updRestartPhase');
  const spin = document.getElementById('updRestartSpin');
  if (spin) spin.classList.add('is-error');
  if (phaseEl) phaseEl.textContent = t('update.restartTimeout');
  showToast('⚠️', t('update.restartTimeout'), '', 6000);
}

/** Explicit restart — re-execs the server, then waits for it to come back.
 *  Invoked from the post-pull "Restart now" button AND the always-available
 *  footer button (no git pull needed). The backend re-execs in place via
 *  os.execv reclaiming the SAME port, so the page can reconnect to the same
 *  URL once /api/health answers again. */
async function restartServer(opts) {
  // Already restarting — ignore a re-entry (footer button stays live, the
  // post-pull button could be double-clicked) so we never spawn a second
  // poll loop or reset the progress timeline.
  if (_restartActive) return;
  _restartActive = true;
  const btn = document.getElementById('updateRestartBtn')
    || document.getElementById('updateRestartNowBtn');
  const _restoreBtn = function () {
    if (btn) { btn.disabled = false; btn.textContent = t('update.restartBtn'); }
  };
  if (btn) { btn.disabled = true; btn.textContent = t('update.restarting'); }

  // Human-approval gate (pt_40d00fd526e5479a): a restart POST without an
  // approvalId only REGISTERS a pending request (202). The human's click on
  // this very button IS the approval gesture, so we approve the pending
  // request immediately and retry with the token — the flow stays seamless
  // for the operator while agent shells (no UI, no gesture) stay gated.
  // A pre-approved id (from the pending-approvals card) rides in via opts.
  var _approvalId = (opts && opts.approvalId) || '';
  async function _requestRestart(forceFlag) {
    const payload = { convId: _ownConv };
    if (forceFlag) payload.force = true;
    if (_approvalId) payload.approvalId = _approvalId;
    const r = await Api.update.restart(payload);
    if (r && r.pendingApproval) {
      _approvalId = r.pendingApproval.id;
      await Api.update.decideLifecycleApproval(_approvalId, true);
      return _requestRestart(forceFlag);
    }
    return r;
  }

  // Capture the CURRENT process's bootId first, so we can require the
  // post-restart health to report a DIFFERENT one (proof a new process
  // answered). Best-effort: if this probe fails we fall back to null, and the
  // success rule then accepts any bootId-bearing health (still better than the
  // old 'any ok' rule) — see _restartCheckHealth.
  _restartPreBootId = null;
  _restartPreFingerprint = null;
  try {
    const pre = await Api.health.info();
    if (pre && pre.bootId) _restartPreBootId = pre.bootId;
    if (pre && pre.codeFingerprint && pre.codeFingerprint.digest) {
      _restartPreFingerprint = pre.codeFingerprint.digest;
    }
  } catch (e) { _restartPreBootId = null; _restartPreFingerprint = null; }

  // Our own conversation id — the backend excludes it when counting in-flight
  // tasks, so the running-task count reflects only OTHER conversations a
  // restart would interrupt (never counts our own idle conv against us).
  var _ownConv = '';
  try { _ownConv = activeConvId || ''; } catch (_e) { _ownConv = ''; }

  // Two-stage informed restart. Stage 1: request WITHOUT force. The backend
  // 409s with {needsForce, runningTasks} only when OTHER conversations have
  // in-flight tasks a restart would kill; an idle server accepts the
  // force-less call immediately (no confirm). Only on that 409 do we surface a
  // themed confirm NAMING the running-task count and, on explicit consent,
  // retry WITH force. This replaces the old blind generic pre-flight confirm
  // (so there is no double-confirm) and never silently kills sibling tasks.
  var _triggered = false;
  try {
    await _requestRestart(false);
    _triggered = true;
  } catch (e) {
    if (e && e.status === 429) {
      // Cooldown: the server was restarted moments ago — surface the
      // remaining seconds and stay put (NO progress card, nothing fired).
      const secs = (e.body && e.body.retryAfterSec) || '?';
      showToast('⏳', t('update.restartCooldown').replace('%s', String(secs)), '', 6000);
      _restartActive = false;
      _restoreBtn();
      return;
    }
    if (e && (e.status === 400 || e.status === 403 || e.status === 404)) {
      // Approval rejected/expired — definitive refusal, nothing is scheduled.
      showToast('⚠️', (e && e.message) || t('update.errUnknown'), '', 6000);
      if (typeof debugLog === 'function') debugLog('[Update] restart refused: HTTP ' + e.status + ' ' + (e && e.message), 'warning');
      _restartActive = false;
      _restoreBtn();
      return;
    }
    if (e && e.status === 409 && e.body && e.body.needsForce) {
      const count = (e.body.runningTasks || []).length;
      const ok = await showConfirm(
        t('update.restartForceConfirm').replace('%s', String(count)),
        { danger: true });
      if (!ok) {
        // Declined — deny the still-unconsumed approval so a stray approved
        // token cannot be fired later, then abort cleanly.
        if (_approvalId) {
          try { await Api.update.decideLifecycleApproval(_approvalId, false); } catch (_e) { /* best effort */ }
        }
        _restartActive = false;
        _restoreBtn();
        return;
      }
      try {
        await _requestRestart(true);
      } catch (e2) {
        if (typeof debugLog === 'function') debugLog('[Update] forced restart request failed: ' + (e2 && e2.message), 'warning');
      }
      // The re-exec is scheduled server-side (daemon thread) before the
      // response, so even a read error on the response means it is underway.
      _triggered = true;
    } else {
      // Non-guard error (e.g. a network blip while the backend already
      // scheduled the fire-and-forget re-exec). Proceed to the health-poll
      // rather than abort — the poll is the source of truth for "came back".
      if (typeof debugLog === 'function') debugLog('[Update] restart request failed: ' + (e && e.message), 'warning');
      _triggered = true;
    }
  }
  if (!_triggered) { _restartActive = false; _restoreBtn(); return; }

  _renderRestartProgress();
  _restartDone = false;
  _restartT0 = Date.now();
  _restartRaf = requestAnimationFrame(_restartAnimate);
  // Wait out the backend's 0.6s pre-exec sleep before the first probe so we
  // never mistake the still-alive OLD process for a successful restart.
  setTimeout(function () { _restartPoll = setInterval(_restartCheckHealth, 1500); }, 2500);
}

/** Manual graceful shutdown — writes the manual-shutdown marker so the next
 *  boot won't mistake this for an OS kill, then stops the server (no re-exec,
 *  so it does NOT come back on its own). Admin-only; always confirms. */
async function shutdownServer(opts) {
  if (_restartActive) return;   // a restart already owns the modal body
  if (!await showConfirm(t('update.shutdownConfirm'), { danger: true })) return;
  const rBtn = document.getElementById('updateRestartNowBtn');
  const sBtn = document.getElementById('updateShutdownBtn');
  if (rBtn) rBtn.disabled = true;
  if (sBtn) { sBtn.disabled = true; }
  await _fireShutdown((opts && opts.approvalId) || '');
}

/** Fire the shutdown through the approval gate (shared by the button and
 *  the pending-approvals card). The confirm click IS the human gesture, so
 *  a freshly-pended request is approved immediately and retried. */
async function _fireShutdown(approvalId) {
  try {
    const payload = approvalId ? { approvalId: approvalId } : {};
    const r = await Api.update.shutdown(payload);
    if (r && r.pendingApproval) {
      await Api.update.decideLifecycleApproval(r.pendingApproval.id, true);
      await Api.update.shutdown({ approvalId: r.pendingApproval.id });
    }
  } catch (e) {
    if (typeof debugLog === 'function') debugLog('[Shutdown] request failed: ' + (e && e.message), 'warning');
  }
  const body = document.getElementById('updateModalBody');
  if (body) {
    body.innerHTML =
      '<div class="upd-checking-wrap"><span>' +
      escapeHtml(t('update.shuttingDown')) + '</span></div>';
  }
  showToast('◐', t('update.shuttingDown'), t('update.shutdownHint'), 8000);
}

// ── Pending lifecycle approvals (agent-initiated restart/shutdown requests) ──
// An agent curl against /api/v1/update/restart|shutdown now ONLY registers a
// pending request; the human reviews the queue here and approves/denies.
// Approving executes the action immediately through the same UX as the
// operator's own button (force-confirm on running tasks included).
async function _renderPendingLifecycleApprovals() {
  const host = document.getElementById('updateLifecycleApprovals');
  if (!host) return;
  let records = [];
  try {
    const r = await Api.update.listLifecycleApprovals({ status: 'pending' });
    records = (r && r.records) || [];
  } catch (e) { records = []; }
  if (!records.length) {
    host.innerHTML = '';
    host.style.display = 'none';
    return;
  }
  host.style.display = '';
  host.innerHTML = '<div class="upd-lc-card">' +
    '<div class="upd-lc-title">' + escapeHtml(t('update.pendingApprovals')) + '</div>' +
    records.map(function (rec) {
      const o = rec.origin || {};
      let when = '';
      try { when = new Date((rec.requested_at || 0) * 1000).toLocaleTimeString(); } catch (_e) { when = ''; }
      const meta = [o.ua, o.conv_id, o.remote_addr].filter(Boolean).join(' · ');
      const btnLabel = rec.action === 'shutdown'
        ? t('update.approveExecuteShutdown') : t('update.approveExecuteRestart');
      return '<div class="upd-lc-row" data-id="' + escapeHtml(rec.id) +
        '" data-action="' + escapeHtml(rec.action) + '">' +
        '<div class="upd-lc-meta"><span class="upd-lc-action">' + escapeHtml(rec.action) +
        '</span> · ' + escapeHtml(when) + (meta ? ' · ' + escapeHtml(meta) : '') + '</div>' +
        '<div class="upd-lc-btns">' +
        '<button class="upd-lc-approve" onclick="_lcDecide(this,true)">' + escapeHtml(btnLabel) + '</button>' +
        '<button class="upd-lc-deny" onclick="_lcDecide(this,false)">' + escapeHtml(t('update.deny')) + '</button>' +
        '</div></div>';
    }).join('') + '</div>';
}

/** Approve (and execute) or deny one pending lifecycle request. */
async function _lcDecide(btn, approved) {
  const row = btn && btn.closest ? btn.closest('.upd-lc-row') : null;
  const id = row && row.dataset ? row.dataset.id : '';
  const action = row && row.dataset ? row.dataset.action : '';
  if (!id) return;
  btn.disabled = true;
  try {
    await Api.update.decideLifecycleApproval(id, approved);
  } catch (e) {
    showToast('⚠️', (e && e.message) || t('update.errUnknown'), '', 5000);
    btn.disabled = false;
    return;
  }
  if (!approved) {
    showToast('◐', t('update.approvalDenied'), '', 4000);
    _renderPendingLifecycleApprovals();
    return;
  }
  if (action === 'shutdown') {
    await _fireShutdown(id);
  } else {
    // The approved id rides into the standard restart flow (progress card,
    // force-confirm on running tasks, health-poll, auto-reload).
    restartServer({ approvalId: id });
  }
}

/** One health probe; on success finish, on overall timeout bail.
 *  Success requires a genuinely NEW process answered — health.ok AND a bootId
 *  that DIFFERS from the one captured before the restart. os.execv keeps the
 *  PID + start-time, so bootId is the only reliable 'different process' signal;
 *  keying on it stops the old process (still draining) from being mistaken for
 *  a completed restart. If we could not capture a pre-restart bootId, or the
 *  server is an old build that doesn't report bootId, we degrade gracefully:
 *  accept the first ok health that carries ANY bootId, else (no bootId field at
 *  all) accept ok — never worse than the old rule, and still bounded by the
 *  overall timeout. */
async function _restartCheckHealth() {
  if (_restartDone) return;
  if ((Date.now() - _restartT0) / 1000 > 80) { _restartTimeout(); return; }
  let info = null;
  try {
    info = await Api.health.info();  // parsed JSON → carries version + bootId
  } catch (e) { info = null; }
  if (!info || !info.ok) return;
  // Preferred, robust path: we know the old bootId AND the server reports one.
  if (_restartPreBootId && info.bootId) {
    if (info.bootId === _restartPreBootId) return;  // still the OLD process — keep waiting
    _restartSucceed(info.version || '', info);
    return;
  }
  // Degraded path (no pre-id captured, or old build without bootId): accept ok.
  _restartSucceed(info.version || '', info);
}

/** Classify whether the restarted process loaded DIFFERENT source than the
 *  pre-restart one, using the code fingerprint. Returns:
 *    'changed'   — post digest differs from pre → the operator's edits are live
 *    'unchanged' — identical digest → a new process, but same source as before
 *    null        — indeterminate (no pre/post digest; non-git deploy or old build)
 *  Only a confident 'changed'/'unchanged' is surfaced; null stays silent so we
 *  never make a claim the fingerprint can't support. */
function _restartCodeVerdict(info) {
  const post = info && info.codeFingerprint && info.codeFingerprint.digest;
  if (!_restartPreFingerprint || !post) return null;
  return (post === _restartPreFingerprint) ? 'unchanged' : 'changed';
}

function closeUpdateModal() {
  // A restart owns the modal: the progress card is the only feedback the user
  // has while the server is down (no live logs over the wire). Dismissing it —
  // via the × button or a backdrop click — would strand the user staring at a
  // dead page with no indication the restart is still in flight. Pin it open
  // until the restart resolves (auto-reloads on success, or shows a timeout).
  if (_restartActive) return;
  const modal = document.getElementById('updateModal');
  if (modal) modal.classList.remove('open');
  // Closing mid-apply must not read as a cancel: the download keeps running
  // server-side and the completion toast will offer the restart. Say so once.
  if (_updateBusy) {
    showToast('ℹ️', t('update.bgRunningToast'), '', 5000);
  }
}

// Boot check shortly after load (don't block first paint / chat boot).
// _onReady (feature-loader.js, core) — NOT window 'load': a deferred module
// lands AFTER the load event fired, so a load listener would never run and
// the update check would silently never happen (Epic-E sub-9).
if (typeof window !== 'undefined') {
  _onReady(function () {
    setTimeout(_updateBootCheck, 3000);
  });
}
