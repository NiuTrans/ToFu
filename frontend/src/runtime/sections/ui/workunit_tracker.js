/* ===== migrated source: ui/workunit_tracker.js ===== */
/* ui/workunit_tracker.js — WorkUnit live hydration for produce_* cards.
 *
 * docs/contracts/API_CONTRACT.md §2.18: the server projects every production task into
 * ONE workUnit block; this tracker is its only client-side consumer. A
 * produce_* tool round's ACCEPTANCE (badge queued|running + meta.workUnitRef)
 * is all the card knows at render time — the model's turn has ended by
 * design, so everything after acceptance (stage progress, retries, degraded
 * quality, the download link) arrives HERE, never via another LLM message.
 *
 * Rendering semantics: the card's HTML string carries only the re-attach
 * handle (data-wu-task / data-wu-kind) and an empty .ptool-wu-live region;
 * this tracker hydrates that region in place, mirroring the fingerprint-gate
 * in-place updates of _tickTimerCountdowns. A conversation re-render simply
 * re-arms the scan (the handle is durable in the persisted round meta), and
 * the next poll re-hydrates from the server-projected snapshot — the client
 * NEVER derives lifecycle state itself.
 *
 * Residency: demand-scoped. Cards call _wuScanSoon() while their HTML string
 * is being built; a debounced scan registers [data-wu-task] elements and
 * starts ONE shared poll loop (chained setTimeout, never setInterval): 2s
 * cadence backing off towards 5s while nothing changes, ticks skipped while
 * the tab is hidden, loop stops when the registry empties. No boot listener,
 * no SSE channel — terminal truth is the task snapshot, never an event.
 */

const _WU_TERMINAL = { done: 1, degraded: 1, failed: 1, aborted: 1 };
const _WU_DELAY_MIN = 2000;
const _WU_DELAY_MAX = 5000;

/* taskId → { kind, fp, seen, scenes } — `fp` fingerprints the last applied
 * snapshot so unchanged polls skip DOM writes and drive the backoff; `scenes`
 * caches the one-shot motion scene-grid fetch after a terminal unit. */
const _wuRegistry = new Map();
let _wuScanTimer = null;
let _wuPollTimer = null;
let _wuDelay = _WU_DELAY_MIN;

function _wuT(k, d) {
  return (typeof t === 'function') ? t(k, d) : (d || k);
}

/* Debounced scan entry — called by _renderMotionVideoBlock while the card
 * string is being built (before it reaches the DOM). */
/* Registry probes — the registry itself stays a module-local const (eval
 * scopes it away from tests); these function declarations are the readable
 * surface for the jsdom harness and console debugging. */
function _wuTrackedCount() { return _wuRegistry.size; }
function _wuTracked(taskId) { return _wuRegistry.has(taskId); }

function _wuScanSoon() {
  if (_wuScanTimer) return;
  _wuScanTimer = setTimeout(function () {
    _wuScanTimer = null;
    _wuScanNow();
  }, 50);
}

function _wuScanNow() {
  if (typeof document === 'undefined' || !document.querySelectorAll) return;
  _wuArmChipListener();
  const els = document.querySelectorAll('[data-wu-task]');
  for (const el of els) {
    const id = el.getAttribute('data-wu-task');
    if (!id || _wuTracked(id)) continue;
    _wuRegistry.set(id, {
      kind: el.getAttribute('data-wu-kind') || '',
      fp: '',
    });
  }
  if (_wuTrackedCount()) _wuEnsureLoop();
}

function _wuTasksApi() {
  return (typeof Api !== 'undefined' && Api && Api.tasks &&
    typeof Api.tasks.get === 'function') ? Api.tasks : null;
}

function _wuEnsureLoop() {
  if (_wuPollTimer) return;
  _wuPollTimer = setTimeout(_wuTick, _wuDelay);
}

/* One tick polls every registered (non-terminal) unit, applies changed
 * snapshots, and reschedules. The loop self-terminates when the registry
 * empties (all units terminal or their cards gone). */
function _wuTick() {
  _wuPollTimer = null;
  if (!_wuTrackedCount()) return;
  if (typeof document !== 'undefined' && document.hidden) {
    _wuEnsureLoop();
    return;
  }
  const api = _wuTasksApi();
  if (!api) { _wuEnsureLoop(); return; }
  /* Cards whose element left the DOM (conversation deleted, fingerprint
   * re-render in flight) are NOT dropped here — a re-render re-arms the scan
   * and the same taskId re-registers; a truly gone card's unit still reaches
   * terminal state and is then forgotten. */
  const ids = Array.from(_wuRegistry.keys());
  let changed = false;
  let pending = ids.length;
  const settle = function () {
    pending -= 1;
    if (pending > 0) return;
    _wuDelay = changed ? _WU_DELAY_MIN
      : Math.min(_WU_DELAY_MAX, _wuDelay + 1500);
    if (_wuTrackedCount()) _wuEnsureLoop();
  };
  for (const id of ids) {
    Promise.resolve(api.get(id)).then(function (snap) {
      if (snap && snap.workUnit) {
        if (_wuApply(id, snap.workUnit)) changed = true;
      }
      /* null (onError:'null' / 404 after TTL sweep): keep the registration;
       * a restarted server re-serves the unit by id via the durable path. */
    }).catch(function () { /* transient — next tick retries */ })
      .then(settle);
  }
}

/* Apply one server-projected workUnit to every card carrying the handle.
 * Returns true when the snapshot differed from the last applied one. */
function _wuApply(taskId, unit) {
  const entry = _wuRegistry.get(taskId);
  if (!entry || !unit || typeof unit !== 'object') return false;
  const stages = Array.isArray(unit.stages) ? unit.stages : [];
  const pages = (unit.pages && typeof unit.pages === 'object')
    ? unit.pages : null;
  const fp = JSON.stringify([
    unit.status, unit.stageTotal || 0,
    stages.map(function (s) {
      return [s.name, s.state, s.attempt, s.elapsedS,
        (s.lastErrors || []).length];
    }),
    unit.quality || null, unit.failure || null,
    (Array.isArray(unit.artifacts) ? unit.artifacts : []).length,
    pages ? [pages.total || 0,
      (Array.isArray(pages.rendered) ? pages.rendered : []).length,
      pages.rev || null] : null,
    (unit.pendingGate && unit.pendingGate.guidanceId) || null,
  ]);
  if (fp === entry.fp) return false;
  entry.fp = fp;
  if (_WU_TERMINAL[unit.status] && unit.kind === 'motion-video') {
    _wuFetchScenesOnce(taskId, entry);
  }
  const html = _wuLiveHtml(unit, entry);
  const cards = document.querySelectorAll('[data-wu-task="' + taskId + '"]');
  for (const card of cards) {
    const live = card.querySelector('.ptool-wu-live');
    if (live) live.innerHTML = html;
    const badge = card.querySelector('.ptool-convmeta-header .ptool-badge');
    if (badge) {
      badge.textContent = _wuStatusLabel(unit.status);
      badge.className = 'ptool-badge ptool-wu-badge-' +
        (_WU_TERMINAL[unit.status] || unit.status === 'running' ||
         unit.status === 'queued' || unit.status === 'retrying'
          ? unit.status : 'failed');
    }
  }
  if (_WU_TERMINAL[unit.status]) {
    _wuRegistry.delete(taskId);
    _wuAnnounce(unit);
  }
  return true;
}

function _wuStatusLabel(status) {
  return _wuT('wu.status.' + status, status || '');
}

function _wuEsc(s) {
  return (typeof escapeHtml === 'function')
    ? escapeHtml(String(s == null ? '' : s)) : String(s == null ? '' : s);
}

function _wuIcon(name, size) {
  return (typeof Icon === 'function') ? Icon(name, size || 12) : '';
}

/* Streaming per-page gallery: the server projects {total, rendered, rev,
 * href} from page_rendered events (docs/contracts/EVENTS.md production extras); the
 * client substitutes {n} in href and appends ?v=<rev> so a QA re-render of
 * the same page number still refreshes the thumbnail. Pending slots render
 * only while the unit is live — after a terminal failure the gallery shows
 * exactly what exists, never a promise of pages that will not arrive. */
function taskIdOf(hrefTemplate) {
  const m = String(hrefTemplate || '').match(/\/slides\/([^/]+)\/pages/);
  return m ? m[1] : '';
}

function _wuGalleryHtml(unit) {
  const pages = (unit && unit.pages && typeof unit.pages === 'object')
    ? unit.pages : null;
  if (!pages) return '';
  const href = String(pages.href || '');
  if (href.indexOf('{n}') < 0) return '';
  const rendered = Array.isArray(pages.rendered) ? pages.rendered : [];
  if (!rendered.length) return '';
  const rev = (pages.rev && typeof pages.rev === 'object') ? pages.rev : {};
  const total = Math.min(99, Number(pages.total || 0));
  const live = !_WU_TERMINAL[unit.status];
  const seen = {};
  let html = '<div class="ptool-wu-gallery">';
  for (const raw of rendered) {
    const n = Number(raw);
    if (!(n >= 1 && n <= 99) || seen[n]) continue;
    seen[n] = true;
    const url = href.replace('{n}', String(n)) +
      '?v=' + (Number(rev[String(n)]) || 1);
    const label = _wuT('wu.pageN', 'Page {n}').replace('{n}', n);
    html += '<button type="button" class="ptool-wu-thumb" ' +
      'data-wu-edit-task="' + _wuEsc(taskIdOf(href)) +
      '" data-wu-edit-page="' + n + '" data-wu-edit-total="' +
      _wuEsc(total || n) + '" title="' + _wuEsc(label) + '">' +
      '<img loading="lazy" src="' + _wuEsc(url) + '" alt="' +
      _wuEsc(label) + '">' +
      '<span class="ptool-wu-thumb-n">' + n + '</span></button>';
  }
  if (live && total > 0) {
    for (let n = 1; n <= total; n++) {
      if (seen[n]) continue;
      html += '<span class="ptool-wu-thumb ptool-wu-thumb-pending">' +
        '<span class="ptool-wu-thumb-n">' + n + '</span></span>';
    }
  }
  return html + '</div>';
}

/* Quick-action chips on terminal cards (C2): each chip either SENDS a
 * complete pre-built instruction or PREFILLS the composer for the pieces
 * only the user knows (which scene, what change). Messages carry the task
 * id so the model resolves the right revision tool (edit_slides /
 * restyle_slides / edit_video / revert_*) without guessing. */
const _WU_CHIP_DEFS = {
  'slides-deck': [
    { key: 'wu.chipRestyle', send: true,
      msg: '把这个 PPT（任务 {id}）整体换一种视觉风格（restyle_slides）' },
    { key: 'wu.chipSimplify', send: true,
      msg: '把这个 PPT（任务 {id}）的文字精简一下，突出每页核心信息' },
    { key: 'wu.chipTranslate', send: true,
      msg: '把这个 PPT（任务 {id}）全部翻译成英文，保持版式不变' },
    { key: 'wu.chipEditPage', send: false,
      msg: '编辑这个 PPT（任务 {id}）：把第　页改成——' },
  ],
  'motion-video': [
    { key: 'wu.chipEditScene', send: false,
      msg: '编辑视频任务 {id}：把第　个镜头改成——' },
    { key: 'wu.chipNarration', send: false,
      msg: '替换视频任务 {id} 第　个镜头的口播文案为：' },
  ],
};

function _wuChipsHtml(unit) {
  if (!_WU_TERMINAL[unit.status] || unit.status === 'failed' ||
      unit.status === 'aborted') return '';
  const defs = _WU_CHIP_DEFS[unit.kind];
  if (!defs || !defs.length) return '';
  const taskId = _wuUnitTaskId(unit);
  if (!taskId) return '';
  let html = '<div class="ptool-wu-chips">';
  for (const def of defs) {
    const message = def.msg.split('{id}').join(taskId);
    html += '<button type="button" class="ptool-wu-chip" data-wu-chip="' +
      _wuEsc(def.send ? 'send' : 'prefill') + '" data-wu-message="' +
      _wuEsc(message) + '">' + _wuEsc(_wuT(def.key, def.key)) + '</button>';
  }
  return html + '</div>';
}

/* The workUnit projection intentionally omits the raw task id (the card's
 * data-wu-task handle owns it); chips re-derive it from the links block,
 * whose events URL is contract-stable. */
function _wuUnitTaskId(unit) {
  const events = unit && unit.links && unit.links.events
    ? String(unit.links.events) : '';
  const m = events.match(/\/api\/v\d+\/tasks\/([^/]+)\/events/);
  return m ? m[1] : '';
}

function _wuFillComposer(message, send) {
  const input = document.getElementById('userInput');
  if (!input) return;
  input.value = message;
  /* The synthetic input event must come from the element's OWN window —
   * a cross-realm Event (e.g. the outer global in a jsdom/iframe world)
   * is rejected by dispatchEvent. */
  const view = input.ownerDocument && input.ownerDocument.defaultView;
  const EventCtor = (view && view.Event) || Event;
  input.dispatchEvent(new EventCtor('input', { bubbles: true }));
  if (typeof updateSendButton === 'function') updateSendButton();
  input.focus();
  if (send && typeof _doSendOrGenerate === 'function') {
    _doSendOrGenerate();
  }
}

/* Interactive outline gate (A6): a chat-initiated deck parks before the
 * authoring fan-out and the card becomes the review surface — outline
 * pages, an answer box, approve-as-is / send-changes buttons. Answers post
 * to the SHARED human-response endpoint (same owner-bound registry as
 * ask_human); the next poll's projection clears the gate. */
function _wuGateHtml(unit) {
  const gate = (unit && unit.pendingGate &&
    typeof unit.pendingGate === 'object') ? unit.pendingGate : null;
  if (!gate || !gate.guidanceId) return '';
  const pages = Array.isArray(gate.pages) ? gate.pages : [];
  let rows = '';
  for (const p of pages) {
    rows += '<div class="ptool-wu-gate-page">' +
      '<span class="ptool-wu-gate-page-n">' + _wuEsc(p.page || '') + '</span>' +
      '<span class="ptool-wu-gate-page-type">' + _wuEsc(p.pageType || '') +
      '</span>' +
      '<span class="ptool-wu-gate-page-msg">' + _wuEsc(p.key_message || '') +
      '</span></div>';
  }
  return '<div class="ptool-wu-gate" data-wu-gate="' +
    _wuEsc(gate.guidanceId) + '">' +
    '<div class="ptool-wu-gate-title">' +
    _wuIcon('clock', 12) +
    '<span>' + _wuEsc(_wuT('wu.gateTitle', '确认大纲后开始生成（可提出修改意见）')) +
    '</span></div>' +
    (gate.title ? '<div class="ptool-wu-gate-deck">' + _wuEsc(gate.title) +
      '</div>' : '') +
    '<div class="ptool-wu-gate-pages">' + rows + '</div>' +
    '<textarea class="ptool-wu-gate-input" rows="2" placeholder="' +
    _wuEsc(_wuT('wu.gatePlaceholder',
                '留空=直接生成；或写修改意见，如「删掉第 3 页，结尾加一页行动建议」')) +
    '"></textarea>' +
    '<div class="ptool-wu-gate-actions">' +
    '<button type="button" class="ptool-wu-chip" data-wu-gate-send="approve">' +
    _wuEsc(_wuT('wu.gateApprove', '直接生成')) + '</button>' +
    '<button type="button" class="ptool-wu-chip" data-wu-gate-send="feedback">' +
    _wuEsc(_wuT('wu.gateSubmit', '提交修改意见')) + '</button>' +
    '</div></div>';
}

function _wuSubmitGate(gateEl, mode) {
  const guidanceId = gateEl.getAttribute('data-wu-gate') || '';
  if (!guidanceId) return;
  const input = gateEl.querySelector('.ptool-wu-gate-input');
  const text = mode === 'approve'
    ? '继续'
    : String((input && input.value) || '').trim();
  if (!text) {
    if (input) input.focus();
    return;
  }
  const api = (typeof Api !== 'undefined' && Api && Api.chat &&
    typeof Api.chat.humanResponse === 'function') ? Api.chat : null;
  if (!api) return;
  const buttons = gateEl.querySelectorAll('button');
  for (const btn of buttons) btn.disabled = true;
  Promise.resolve(api.humanResponse(guidanceId, text)).then(function (res) {
    if (res && res.ok !== false) {
      gateEl.classList.add('ptool-wu-gate-done');
      if (input) input.disabled = true;
    } else {
      for (const btn of buttons) btn.disabled = false;
    }
  }).catch(function () {
    for (const btn of buttons) btn.disabled = false;
  });
}

/* Cross-feature composer channel: the slides editor's 「问 AI 改这页」 button
 * prefills through this port instead of re-deriving the composer plumbing. */
if (typeof runtimeScope !== 'undefined') {
  runtimeScope.TofuComposerPrefill = _wuFillComposer;
}

let _wuChipListenerArmed = false;
function _wuArmChipListener() {
  if (_wuChipListenerArmed || typeof document === 'undefined') return;
  _wuChipListenerArmed = true;
  document.addEventListener('click', function (event) {
    const thumb = event.target && event.target.closest
      ? event.target.closest('.ptool-wu-thumb[data-wu-edit-task]') : null;
    if (thumb) {
      const editTask = thumb.getAttribute('data-wu-edit-task') || '';
      const editPage = Number(thumb.getAttribute('data-wu-edit-page')) || 1;
      const editTotal = Number(thumb.getAttribute('data-wu-edit-total')) ||
        editPage;
      if (editTask && typeof openSlidesEditor === 'function') {
        openSlidesEditor(editTask, editPage, editTotal);
      }
      return;
    }
    const gateBtn = event.target && event.target.closest
      ? event.target.closest('[data-wu-gate-send]') : null;
    if (gateBtn) {
      const gateEl = gateBtn.closest('.ptool-wu-gate');
      if (gateEl) _wuSubmitGate(gateEl,
        gateBtn.getAttribute('data-wu-gate-send'));
      return;
    }
    const chip = event.target && event.target.closest
      ? event.target.closest('.ptool-wu-chip') : null;
    if (chip) {
      const message = chip.getAttribute('data-wu-message') || '';
      if (message) _wuFillComposer(message,
        chip.getAttribute('data-wu-chip') === 'send');
      return;
    }
    const sceneBtn = event.target && event.target.closest
      ? event.target.closest('.ptool-wu-scene-btn') : null;
    if (sceneBtn) {
      const action = sceneBtn.getAttribute('data-wu-scene-action');
      const taskId = sceneBtn.getAttribute('data-wu-task-id') || '';
      const sceneId = sceneBtn.getAttribute('data-wu-scene-id') || '';
      if (!taskId || !sceneId) return;
      if (action === 'regen') {
        const api = (typeof Api !== 'undefined' && Api && Api.motion)
          ? Api.motion : null;
        if (api && typeof api.regenScene === 'function') {
          sceneBtn.disabled = true;
          Promise.resolve(api.regenScene(taskId, sceneId)).then(function (res) {
            if (typeof showToast === 'function') {
              try {
                showToast('', _wuT('wu.sceneRegenStarted', 'Re-rendering'),
                  sceneId, 2200);
              } catch (e) { /* cosmetic only */ }
            }
            sceneBtn.disabled = !(res && res.ok);
          });
        }
      } else if (action === 'edit') {
        _wuFillComposer(
          _wuT('wu.sceneEditPrefill', '编辑视频任务 {id}：把第 {n} 个镜头改成——')
            .replace('{id}', taskId).replace('{n}', sceneId), false);
      }
    }
  });
}

/* B4: scene grid for a terminal motion-video card — one compact row per
 * scene (id + duration + status), each with a deterministic 重渲 button
 * (regen API) and an 编辑 button that prefills the composer. Fetched ONCE
 * per unit; scene mp4 playback stays in the video panel, not the chat. */
function _wuScenesHtml(unit, entry) {
  if (unit.kind !== 'motion-video' || !_WU_TERMINAL[unit.status] ||
      unit.status === 'failed' || unit.status === 'aborted') return '';
  const scenes = entry && Array.isArray(entry.scenes) ? entry.scenes : null;
  if (!scenes || !scenes.length) return '';
  const taskId = _wuUnitTaskId(unit);
  let html = '<div class="ptool-wu-scenes">';
  for (const sc of scenes.slice(0, 24)) {
    const sid = _wuEsc(sc.scene_id || '');
    const dur = (sc.content_duration_s != null)
      ? _wuEsc(Number(sc.content_duration_s).toFixed(1) + 's') : '';
    const state = sc.has_video ? '' : ' ptool-wu-scene-missing';
    html += '<div class="ptool-wu-scene' + state + '">' +
      '<span class="ptool-wu-scene-id">' + sid + '</span>' +
      '<span class="ptool-wu-scene-dur">' + dur + '</span>' +
      '<button type="button" class="ptool-wu-scene-btn" ' +
        'data-wu-scene-action="regen" data-wu-task-id="' + _wuEsc(taskId) +
        '" data-wu-scene-id="' + sid + '">' +
        _wuEsc(_wuT('wu.sceneRegen', '重新渲染')) + '</button>' +
      '<button type="button" class="ptool-wu-scene-btn" ' +
        'data-wu-scene-action="edit" data-wu-task-id="' + _wuEsc(taskId) +
        '" data-wu-scene-id="' + sid + '">' +
        _wuEsc(_wuT('wu.sceneEdit', '编辑')) + '</button>' +
      '</div>';
  }
  return html + '</div>';
}

function _wuFetchScenesOnce(taskId, entry) {
  if (entry.scenes !== undefined || entry.scenesFailed) return;
  const api = (typeof Api !== 'undefined' && Api && Api.motion &&
    typeof Api.motion.scenes === 'function') ? Api.motion : null;
  if (!api) { entry.scenesFailed = true; return; }
  Promise.resolve(api.scenes(taskId)).then(function (res) {
    if (res && Array.isArray(res.scenes)) {
      entry.scenes = res.scenes;
      entry.fp = '';   // force one re-render with the grid hydrated
    } else {
      entry.scenesFailed = true;
    }
  }).catch(function () { entry.scenesFailed = true; });
}

/* The .ptool-wu-live region: a stage stepper (observed stages + the
 * not-yet-started remainder implied by stageTotal) plus, for terminal units,
 * ONE honest outcome line — download links for done/degraded, the failure
 * envelope (message + hint) for failed, muted for aborted. */
function _wuLiveHtml(unit, entry) {
  const stages = Array.isArray(unit.stages) ? unit.stages : [];
  const total = Number(unit.stageTotal || 0);
  let html = '<div class="ptool-wu-stages">';
  for (const s of stages) {
    if (!s || typeof s !== 'object') continue;
    const state = (s.state === 'done' || s.state === 'active' ||
      s.state === 'skipped' || s.state === 'failed') ? s.state : 'pending';
    const icon = state === 'done' ? _wuIcon('check')
      : state === 'active' ? '<span class="ptool-spinner"></span>'
      : state === 'skipped' ? _wuIcon('chevronDown')
      : state === 'failed' ? _wuIcon('x')
      : _wuIcon('clock');
    const attempt = (Number(s.attempt || 0) > 1)
      ? '<span class="ptool-wu-attempt">' + _wuEsc(
        _wuT('wu.attempt', 'attempt {n}').replace('{n}', Number(s.attempt))) +
        '</span>'
      : '';
    const elapsed = (s.elapsedS != null && state !== 'pending')
      ? '<span class="ptool-wu-elapsed">' + _wuEsc(Number(s.elapsedS)) + 's</span>'
      : '';
    const lastErr = (Array.isArray(s.lastErrors) && s.lastErrors.length &&
      (state === 'failed' || Number(s.attempt || 0) > 1))
      ? '<span class="ptool-wu-stage-err">' + _wuEsc(
        String(s.lastErrors[s.lastErrors.length - 1]).slice(0, 120)) + '</span>'
      : '';
    html += '<div class="ptool-wu-stage ptool-wu-stage-' + state + '">' +
      '<span class="ptool-wu-stage-icon">' + icon + '</span>' +
      '<span class="ptool-wu-stage-name">' + _wuEsc(s.name || '') + '</span>' +
      attempt + elapsed + lastErr + '</div>';
  }
  for (let i = stages.length; i < total; i++) {
    html += '<div class="ptool-wu-stage ptool-wu-stage-pending">' +
      '<span class="ptool-wu-stage-icon">' + _wuIcon('clock') + '</span>' +
      '<span class="ptool-wu-stage-name">' + _wuEsc(
        _wuT('wu.stagePending', 'stage {n}').replace('{n}', i + 1)) +
      '</span></div>';
  }
  html += '</div>';
  html += _wuGateHtml(unit);
  html += _wuGalleryHtml(unit);

  const artifacts = Array.isArray(unit.artifacts) ? unit.artifacts : [];
  const links = artifacts.map(function (a) {
    if (!a || !a.href) return '';
    const bits = [a.label || 'file'];
    if (a.pages != null) bits.push(_wuT('wu.pages', '{n} pages')
      .replace('{n}', a.pages));
    if (a.bytes) bits.push(_wuFmtBytes(a.bytes));
    return '<a class="ptool-wu-download" href="' + _wuEsc(a.href) +
      '" download>' + _wuIcon('save', 12) + '<span>' + _wuEsc(
        _wuT('wu.download', 'Download {label}')
          .replace('{label}', bits.join(' · '))) + '</span></a>';
  }).join('');

  if (unit.status === 'done' || unit.status === 'degraded') {
    const degraded = unit.status === 'degraded';
    const reason = degraded && unit.quality && unit.quality.reason
      ? '<span class="ptool-wu-line-note">' + _wuEsc(unit.quality.reason) +
        '</span>' : '';
    html += '<div class="ptool-wu-line ' +
      (degraded ? 'ptool-wu-line-warn' : 'ptool-wu-line-ok') + '">' +
      _wuIcon(degraded ? 'alertTriangle' : 'check', 13) +
      '<span>' + _wuEsc(degraded
        ? _wuT('wu.degradedLine', 'Finished with reduced quality')
        : _wuT('wu.doneLine', 'Finished')) + '</span>' + reason +
      links + '</div>';
  } else if (unit.status === 'failed') {
    const f = unit.failure || {};
    const msg = f.message ? '<span class="ptool-wu-line-note">' +
      _wuEsc(String(f.message).slice(0, 200)) + '</span>' : '';
    const hint = f.hint ? '<div class="ptool-wu-hint">' +
      _wuEsc(String(f.hint).slice(0, 300)) + '</div>' : '';
    html += '<div class="ptool-wu-line ptool-wu-line-err">' +
      _wuIcon('x', 13) +
      '<span>' + _wuEsc(_wuT('wu.failedLine', 'Job failed')) + '</span>' +
      msg + '</div>' + hint;
  } else if (unit.status === 'aborted') {
    html += '<div class="ptool-wu-line ptool-wu-line-muted">' +
      _wuIcon('ban', 13) +
      '<span>' + _wuEsc(_wuT('wu.abortedLine', 'Stopped')) + '</span></div>';
  }
  html += _wuScenesHtml(unit, entry);
  html += _wuChipsHtml(unit);
  return html;
}

function _wuFmtBytes(n) {
  const v = Number(n) || 0;
  if (v >= 1048576) return (v / 1048576).toFixed(1) + ' MB';
  if (v >= 1024) return Math.round(v / 1024) + ' KB';
  return v + ' B';
}

/* Ephemeral completion notice — the durable record is the card itself; the
 * toast only catches a user reading further down the transcript. */
function _wuAnnounce(unit) {
  if (typeof showToast !== 'function') return;
  const title = _wuStatusLabel(unit.status);
  const body = unit.title || '';
  try { showToast('', title, body, 2600); } catch (e) { /* cosmetic only */ }
}
