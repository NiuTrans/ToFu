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

// Ordered stages of an apply run, rendered as the live stepper.
var _UPDATE_STAGES = ['fetch', 'pull', 'deps'];

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
  if (!modal) return;
  modal.classList.add('open');
  _runUpdateCheck();
}

/** Run the version check with a visible spinner + bounded timeout.
 *  The check hits GitHub's tags API server-side; on a slow/blocked
 *  network we must NOT sit on a bare label forever — show a spinner,
 *  cap the wait, and offer an explicit retry. */
async function _runUpdateCheck() {
  const body = document.getElementById('updateModalBody');
  if (!body) return;
  body.innerHTML =
    '<div class="upd-checking-wrap"><span class="upd-big-spin"></span><span>' +
    escapeHtml(t('update.checking')) + '</span></div>';

  let r = null;
  try {
    // Bounded wait — the default 30s feels frozen. 12s is plenty for a
    // reachable GitHub; beyond that we surface a retry instead of hanging.
    r = await Api.update.check({ timeout: 12000 });
  } catch (e) {
    if (typeof debugLog === 'function') debugLog('[Update] check failed: ' + (e && e.message), 'error');
  }
  if (!r || !r.ok) {
    body.innerHTML =
      '<div class="upd-checking-wrap"><p class="upd-error">' +
      escapeHtml(t('update.checkFailed')) + '</p>' +
      '<button class="upd-retry-btn" onclick="_runUpdateCheck()">' +
      escapeHtml(t('update.retry')) + '</button></div>';
    return;
  }
  _updateState = r;
  _renderUpdateBadge();
  _renderUpdateDialogBody(r);
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

  let actionHtml = '';
  if (!r.git_available) {
    // Not a git checkout — in-place update isn't possible.
    actionHtml = '<div class="upd-badge warn"><span class="upd-badge-icon">⚠️</span><span>' +
      escapeHtml(t('update.noGit')) + '</span></div>';
  } else if (!r.update_available) {
    actionHtml = '<div class="upd-badge ok"><span class="upd-badge-icon">' +
      '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">' +
      '<polyline points="20 6 9 17 4 12"/></svg></span><span>' +
      escapeHtml(t('update.upToDate').replace(/^✅\s*/, '')) + '</span></div>';
  } else if (r.dirty) {
    // Genuine tracked-source edits block the pull. List a few, never auto-stash.
    const sample = (r.blocking || []).slice(0, 8).map(escapeHtml).join('<br>');
    actionHtml =
      '<div class="upd-badge warn"><span class="upd-badge-icon">⚠️</span><span>' +
      escapeHtml(t('update.dirty').replace(/^⚠️\s*/, '')) + '</span></div>' +
      (sample ? '<pre class="upd-files">' + sample + '</pre>' : '');
  } else {
    actionHtml =
      '<p class="upd-ready">' + escapeHtml(t('update.ready')) + '</p>' +
      '<button class="upd-apply-btn" id="updateApplyBtn" onclick="applyUpdate()">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
      'stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/>' +
      '<polyline points="7 10 12 15 17 10"/><path d="M5 21h14"/></svg>' +
      escapeHtml(t('update.applyBtn')) + '</button>';
  }

  body.innerHTML =
    _updateHeroHtml(r) +
    '<div class="upd-action" id="updateActionArea">' + actionHtml + '</div>';
}

/** Render the live stepper (one row per stage) into the action area. */
function _renderUpdateStepper() {
  const area = document.getElementById('updateActionArea');
  if (!area) return;
  const labels = {
    fetch: t('update.step.fetch'),
    pull: t('update.step.pull'),
    deps: t('update.step.deps'),
  };
  const items = _UPDATE_STAGES.map(function (stage) {
    return '<li class="upd-step" data-stage="' + stage + '">' +
      '<span class="upd-step-dot"></span>' +
      '<span class="upd-step-label">' + escapeHtml(labels[stage]) + '</span>' +
      '<span class="upd-step-detail"></span></li>';
  }).join('');
  area.innerHTML = '<ul class="upd-stepper">' + items + '</ul>';
  _updateStageEls = {};
  _UPDATE_STAGES.forEach(function (stage) {
    _updateStageEls[stage] = area.querySelector('.upd-step[data-stage="' + stage + '"]');
  });
}

/** Apply a {stage,status,detail} frame to the stepper. */
function _applyStageFrame(frame) {
  if (!_updateStageEls) return;
  const el = _updateStageEls[frame.stage];
  if (!el) return;
  el.classList.remove('is-active', 'is-done', 'is-error');
  if (frame.status === 'active') {
    el.classList.add('is-active');
  } else if (frame.status === 'done') {
    el.classList.add('is-done');
  } else if (frame.status === 'skip') {
    el.classList.add('is-done');
    const lbl = el.querySelector('.upd-step-label');
    if (lbl) lbl.textContent = t('update.step.depsSkip');
  } else if (frame.status === 'error') {
    el.classList.add('is-error');
  }
  const det = el.querySelector('.upd-step-detail');
  if (det && frame.detail && frame.status !== 'error') det.textContent = frame.detail;
}

/** Kick off the update. The backend runs it in a background thread and
 *  streams stage progress over the 'update' push channel; we render a live
 *  stepper and act on the terminal 'done' frame. This keeps the modal
 *  responsive no matter how long git pull / pip install takes. */
async function applyUpdate() {
  if (_updateBusy) return;
  _updateBusy = true;

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
  // surface a timeout instead of an eternal spinner.
  let watchdog = setTimeout(function () {
    _finishUpdateSub(taskId);
    _showUpdateError(t('update.applyTimeout'));
    _updateBusy = false;
  }, 15 * 60 * 1000);

  const handler = function (frame) {
    if (!frame || frame.taskId !== taskId) return;
    clearTimeout(watchdog);
    watchdog = setTimeout(function () {
      _finishUpdateSub(taskId);
      _showUpdateError(t('update.applyTimeout'));
      _updateBusy = false;
    }, 15 * 60 * 1000);

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

/** Terminal 'done' frame — mirror the apply_update() result dict. */
function _onUpdateDone(r) {
  const area = document.getElementById('updateActionArea');
  if (!r.ok) {
    // Code WAS pulled but pip install failed → still offer a restart.
    if (r.changed && r.deps_changed && !r.deps_installed) {
      _renderDepsFailed(r);
    } else {
      _showUpdateError(r.error || t('update.applyFailed'));
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
  const depsNote = (r.deps_changed && r.deps_installed)
    ? '<p class="upd-uptodate">' + escapeHtml(t('update.depsInstalled')) + '</p>'
    : '';
  if (area) {
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
}

/** Render a terminal error message in the action area. */
function _showUpdateError(msg) {
  const area = document.getElementById('updateActionArea');
  if (area) area.innerHTML = '<p class="upd-error">' + escapeHtml(String(msg)) + '</p>';
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
function _restartSucceed(version) {
  if (_restartDone) return;
  _restartDone = true;
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
  // Brief pause so the user sees the completed state before the reload.
  setTimeout(function () { window.location.reload(); }, 750);
}

/** Restart failed to come back within the ceiling — stop and tell the user. */
function _restartTimeout() {
  if (_restartDone) return;
  _restartDone = true;
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
  // The footer button is available with no pending update — confirm first so
  // a stray click never interrupts running tasks.
  if (opts && opts.confirm && !await showConfirm(t('update.restartConfirm'), { danger: true })) return;
  const btn = document.getElementById('updateRestartBtn')
    || document.getElementById('updateRestartNowBtn');
  if (btn) { btn.disabled = true; btn.textContent = t('update.restarting'); }

  _renderRestartProgress();
  try {
    await Api.update.restart();
  } catch (e) {
    if (typeof debugLog === 'function') debugLog('[Update] restart request failed: ' + (e && e.message), 'warning');
  }

  _restartDone = false;
  _restartT0 = Date.now();
  _restartRaf = requestAnimationFrame(_restartAnimate);
  // Wait out the backend's 0.6s pre-exec sleep before the first probe so we
  // never mistake the still-alive OLD process for a successful restart.
  setTimeout(function () { _restartPoll = setInterval(_restartCheckHealth, 1500); }, 2500);
}

/** One health probe; on success finish, on overall timeout bail. */
async function _restartCheckHealth() {
  if (_restartDone) return;
  if ((Date.now() - _restartT0) / 1000 > 80) { _restartTimeout(); return; }
  let info = null;
  try {
    info = await Api.health.info();  // parsed JSON → carries version
  } catch (e) { info = null; }
  if (info && info.ok) _restartSucceed(info.version || '');
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
