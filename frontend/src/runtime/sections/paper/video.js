/* ===== migrated source: paper/video.js ===== */
/* ═══════════════════════════════════════════
   paper/video.js — Paper Video Abstract tab

   Turns a paper report into a short narrated MG video
   (docs/modules/ingest_media.md, P3). Server owns the task
   (POST /api/v1/paper/video/start → motion engine); this module
   renders + polls, and hosts the per-scene preview/regen panel
   (the backend rides /api/v1/motion/videos/* directly).

   States rendered into #paperVideoContent:
     idle / generating (phase progress) / done (player + scene grid)
     report_required (chain to the Report tab) / lookup_failed / error
   ═══════════════════════════════════════════ */

// ── State ──
var _pvideo = {
  paperHash: '',
  lang: 'zh',
  voice: '',
  model: '',           // the NEXT run's pick (persisted; see _pmSeedModel)
  artifactModel: '',   // what the DISPLAYED film was made with ('' = unknown)
  narration: true,
  burnIn: false,
  quality: 'standard',
  visual: 'authored',      // composition tier — NOT the render preset above
  quality_axis: null,      // {degraded, reason} from the server, or null
  taskId: '',
  cursor: 0,
  pollTimer: null,
  pollBusy: false,
  status: 'idle',          // idle|loading|generating|done|report_required|lookup_failed|lost|interrupted|error
  errorText: '',
  progress: { done: 0, total: 0, phase: '' },
  result: null,            // poll done → {final_path, duration, scenes, narrated}
  scenes: [],              // GET /scenes payload
  regenSceneId: '',
  regenTaskId: '',
  ttsAvailable: true,
  defaultVoice: '',
  // P-UX progress perception (docs/modules/ingest_media.md §3.4)
  pollFails: 0,
  phases: [],
  phaseIndex: 0,
  genStartedAt: 0,
  lastEventAt: 0,
  tickTimer: null,
  _rateFirstTick: 0,       // wall-clock of the first countable event (ETA)
  _rateFirstDone: 0,
  etaSec: 0,
  _gridLoaded: false,      // scenes skeleton already fetched this run
};
runtimeScope._pvideo = _pvideo;
// Poll cadence — a var (not const) so the JSDOM harness can shrink it.
var _PVIDEO_POLL_MS = 1500;
var _PV_POLL_FAIL_LIMIT = 5;

function _pvT(key, fallback) {
  return (typeof t === 'function') ? t(key) : (fallback || key);
}

function _pvEl() { return document.getElementById('paperVideoContent'); }

function _pvEsc(s) {
  return (typeof escapeHtml === 'function') ? escapeHtml(s == null ? '' : s)
    : String(s == null ? '' : s);
}

/* Stop the poll timer ONLY — see the note in podcast.js:_pcStopPoll: the 1s
 * activity ticker must survive _pvSchedulePoll()'s re-arm, or the elapsed /
 * last-activity stopwatch freezes at 0:00 on the first poll. */
function _pvHeroIconSvg(kind) {
  if (kind === 'video') {
    return '<svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="M7 4v16M17 4v16M2 9h5M2 15h5M17 9h5M17 15h5"/></svg>';
  }
  return '<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>';
}

function _pvPhaseLabel(phase) {
  var map = {
    parse: 'paper.videoPhaseParse', storyboard: 'paper.videoPhaseStoryboard',
    narrate: 'paper.videoPhaseNarrate', compose: 'paper.videoPhaseCompose',
    render: 'paper.videoPhaseRender',
    concat: 'paper.videoPhaseConcat', mux: 'paper.videoPhaseMux',
    burn_in: 'paper.videoPhaseBurnIn', regen: 'paper.videoPhaseRegen',
  };
  var fallbacks = {
    parse: 'Parsing subtitles', storyboard: 'Storyboarding',
    narrate: 'Voicing scenes', compose: 'Composing scenes',
    render: 'Rendering scenes',
    concat: 'Joining scenes', mux: 'Mixing audio',
    burn_in: 'Burning subtitles', regen: 'Re-rendering scene',
  };
  return _pvT(map[phase] || '', fallbacks[phase] || phase || '');
}

function _pvRenderProgress() {
  var el = document.getElementById('videoProgressLine');
  if (!el) return;
  var p = _pvideo.progress;
  var label = _pvPhaseLabel(p.phase);
  var line = (p.total > 0)
    ? label + ' ' + p.done + '/' + p.total
    : (label || _pvT('paper.videoStarting', 'Starting…'));
  if (_pvideo.etaSec > 0 && (p.phase === 'render' || p.phase === 'narrate')) {
    line += ' · ' + _pvT('paper.mediaEtaPrefix', '≈') + _pvFmtSec(_pvideo.etaSec);
  }
  el.textContent = line;
}

function _pvFmtSec(x) {
  x = Math.max(0, Math.floor(x || 0));
  return Math.floor(x / 60) + ':' + ('0' + (x % 60)).slice(-2);
}

/** Phase stepper (P-UX2) — vocabulary comes from the server's phase_started. */
function _pvStepper() {
  var phases = _pvideo.phases.length ? _pvideo.phases :
    ['storyboard', 'narrate', 'compose', 'render', 'concat', 'mux'];
  var cur = Math.max(_pvideo.phaseIndex, 1);
  var h = '<div class="paper-stepper">';
  phases.forEach(function(ph, i) {
    var idx = i + 1;
    var state = idx < cur ? 'is-done' : (idx === cur ? 'is-active' : '');
    var mark = idx < cur ? '✓' : (idx === cur ? '●' : '○');
    h += '<span class="paper-step ' + state + '">' +
      '<span class="paper-step-mark">' + mark + '</span>' +
      _pvEsc(_pvPhaseLabel(ph)) + '</span>';
    if (idx < phases.length) h += '<span class="paper-step-sep"></span>';
  });
  return h + '</div>';
}

/** Liveness line (P-UX2): elapsed + last-activity; stale tint after 30s. */
function _pvRenderActivity() {
  var el = document.getElementById('videoActivityLine');
  if (!el || _pvideo.status !== 'generating') return;
  var elapsed = Math.max(0, Math.round((Date.now() - _pvideo.genStartedAt) / 1000));
  var quiet = Math.max(0, Math.round((Date.now() - _pvideo.lastEventAt) / 1000));
  el.classList.toggle('is-stale', quiet > 30);
  el.textContent = _pvT('paper.mediaElapsed', 'elapsed') + ' ' + _pvFmtSec(elapsed) +
    ' · ' + _pvT('paper.mediaLastActive', 'last activity') + ' ' + _pvFmtSec(quiet) +
    (quiet > 30 ? ' — ' + _pvT('paper.mediaStillRunning',
      'still running (this step can take minutes)') : '');
}

/**
 * Degrade notice for a film that PLAYED but was not made at the quality asked
 * for (artifact_quality.degraded).
 *
 * WHY THIS IS NOT OPTIONAL: a degraded job keeps `status='done'` by design
 * (lifecycle axis vs product axis), so without this the film where all 8
 * scenes fell back to the plain template card renders EXACTLY like a good
 * one — same player, same badges. The user's only signal would be watching
 * it and being disappointed again.
 */
function _pvQualityBanner() {
  var q = _pvideo.quality_axis;
  if (!q || !q.degraded) return '';
  var reason = (q.reason || '').trim();
  return '<div class="paper-podcast-banner">' +
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>' +
    '<span>' + _pvEsc(_pvT('paper.videoDegraded',
      'This film played, but was not produced at the quality requested.')) +
    (reason ? ' ' + _pvEsc(reason) : '') + '</span></div>';
}

function _pvDegradeBanner() {
  return '<div class="paper-podcast-banner">' +
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>' +
    '<span>' + _pvEsc(_pvT('paper.videoNoTts',
      'No TTS voice slot is configured — this run generates a silent video.')) +
    '</span></div>';
}

/* Studio icon set (SVG only, never emoji). */
function _pvIconSvg(name) {
  if (name === 'zap') {
    return '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>';
  }
  if (name === 'gauge') {
    return '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 19a9 9 0 1 1 16 0"/><path d="M12 15l3.5-5.5"/></svg>';
  }
  if (name === 'gem') {
    return '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3h12l4 6-10 12L2 9l4-6z"/><path d="M2 9h20" opacity=".6"/><path d="M9 3L7 9l5 12 5-12-2-6" opacity=".6"/></svg>';
  }
  if (name === 'mic') {
    return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0 0 14 0"/><line x1="12" y1="17" x2="12" y2="21"/></svg>';
  }
  if (name === 'play') {
    return '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M7 4.5v15l13-7.5z"/></svg>';
  }
  if (name === 'film') {
    return '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="M7 4v16M17 4v16M2 9h5M2 15h5M17 9h5M17 15h5"/></svg>';
  }
  if (name === 'download') {
    return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>';
  }
  if (name === 'file') {
    return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>';
  }
  if (name === 'refresh') {
    return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>';
  }
  return '';
}

/** One rich option card bound to a hidden select (see _pmPick). */
function _pvOptCard(selId, value, icon, title, sub, selected) {
  return '<button type="button" class="pm-opt' + (selected ? ' is-selected' : '') +
    '" data-sel="' + selId + '" data-value="' + value + '" data-tofu-action="_pmPick(this)">' +
    '<span class="pm-opt-icon">' + _pvIconSvg(icon) + '</span>' +
    '<span class="pm-opt-title">' + _pvEsc(title) + '</span>' +
    '<span class="pm-opt-sub">' + _pvEsc(sub) + '</span></button>';
}

/** One segment of a segmented control bound to a hidden select. */
function _pvSegBtn(selId, value, label, selected) {
  return '<button type="button" class="pm-seg-btn' + (selected ? ' is-selected' : '') +
    '" data-sel="' + selId + '" data-value="' + value + '" data-tofu-action="_pmPick(this)">' +
    _pvEsc(label) + '</button>';
}

function _pvRender() {
  var host = _pvEl();
  if (!host) return;
  var s = _pvideo;
  var h = '';

  if (s.status === 'loading') {
    host.innerHTML = '<div class="paper-report-empty"><p>…</p></div>';
    return;
  }

  if (s.status === 'report_required') {
    host.innerHTML =
      '<div class="paper-podcast-hero">' +
      '<div class="paper-podcast-hero-icon">' + _pvHeroIconSvg('video') + '</div>' +
      '<div class="paper-podcast-hero-title">' +
      _pvEsc(_pvT('paper.videoHeroTitle', 'Watch this paper')) + '</div>' +
      '<div class="paper-podcast-hero-sub">' +
      _pvEsc(_pvT('paper.videoNeedReport',
        'The video abstract is adapted from the analysis report — generate the report first.')) + '</div>' +
      '<button class="paper-podcast-btn" data-tofu-action="_switchPaperTab(\'report\')">' +
      _pvEsc(_pvT('paper.videoGoReport', 'Go generate the report')) + '</button>' +
      '</div>';
    return;
  }

  if (s.status === 'lookup_failed') {
    host.innerHTML =
      '<div class="paper-podcast-hero">' +
      '<div class="paper-podcast-hero-icon is-warn">' + _pvHeroIconSvg('warn') + '</div>' +
      '<div class="paper-podcast-hero-sub">' + _pvEsc(s.errorText ||
        _pvT('paper.videoLookupFailed', 'Video status lookup failed — check the server log.')) + '</div>' +
      '<button class="paper-podcast-btn paper-podcast-btn-ghost" data-tofu-action="_initVideoTab(true)">' +
      _pvEsc(_pvT('paper.videoRetry', 'Retry')) + '</button>' +
      '</div>';
    return;
  }

  /* Studio console card (lang/quality/voice/toggles + generate CTA). The
   * rich option cards write into the hidden selects (_pmPick) — the
   * generate path still reads the selects, so the contract never leaves
   * the DOM. */
  if (s.status === 'idle' || s.status === 'error') {
    h += '<div class="paper-podcast-card pm-studio">';
    h += '<div class="pm-studio-head">' +
      '<div class="pm-studio-badge is-video">' + _pvHeroIconSvg('video') + '</div>' +
      '<div class="pm-studio-head-text">' +
      '<div class="pm-studio-title">' +
      _pvEsc(_pvT('paper.videoStudioTitle', 'Video studio')) + '</div>' +
      '<div class="pm-studio-sub">' + _pvEsc(_pvT('paper.videoHint',
        'A short narrated motion-graphic video of this paper — beats, charts and kinetic type.')) +
      '</div></div></div>';
    if (!s.ttsAvailable) h += _pvDegradeBanner();
    h += '<div class="pm-field"><div class="pm-field-label">' +
      _pvEsc(_pvT('paper.mediaOptLang', 'Language')) + '</div>' +
      '<div class="pm-seg">' +
      _pvSegBtn('videoLangSel', 'zh', '中文', s.lang === 'zh') +
      _pvSegBtn('videoLangSel', 'en', 'English', s.lang === 'en') +
      '</div>' +
      '<select id="videoLangSel" class="pm-sr" tabindex="-1" aria-hidden="true">' +
      '<option value="zh"' + (s.lang === 'zh' ? ' selected' : '') + '>中文</option>' +
      '<option value="en"' + (s.lang === 'en' ? ' selected' : '') + '>English</option></select></div>';
    h += _pmModelFieldHtml('video', s);
    h += '<div class="pm-field"><div class="pm-field-label">' +
      _pvEsc(_pvT('paper.mediaOptQuality', 'Quality')) + '</div>' +
      '<div class="pm-options cols-3">' +
      _pvOptCard('videoQualSel', 'draft', 'zap',
        _pvT('paper.videoQualityDraft', 'Draft (fast)'),
        _pvT('paper.videoQualityDraftSub', 'fast preview'), s.quality === 'draft') +
      _pvOptCard('videoQualSel', 'standard', 'gauge',
        _pvT('paper.videoQualityStandard', 'Standard'),
        _pvT('paper.videoQualityStandardSub', 'recommended'), s.quality === 'standard') +
      _pvOptCard('videoQualSel', 'high', 'gem',
        _pvT('paper.videoQualityHigh', 'High'),
        _pvT('paper.videoQualityHighSub', 'slower, finer'), s.quality === 'high') +
      '</div>' +
      '<select id="videoQualSel" class="pm-sr" tabindex="-1" aria-hidden="true">' +
      '<option value="draft"' + (s.quality === 'draft' ? ' selected' : '') + '>' +
      _pvEsc(_pvT('paper.videoQualityDraft', 'Draft (fast)')) + '</option>' +
      '<option value="standard"' + (s.quality === 'standard' ? ' selected' : '') + '>' +
      _pvEsc(_pvT('paper.videoQualityStandard', 'Standard')) + '</option>' +
      '<option value="high"' + (s.quality === 'high' ? ' selected' : '') + '>' +
      _pvEsc(_pvT('paper.videoQualityHigh', 'High')) + '</option></select></div>';
    /* Composition tier — a SEPARATE control from the render preset above.
     * They were conflated before: draft/standard/high governs bitrate/scale,
     * so a user picking 'High (slower, finer)' still received the plain
     * template card and read that as the product being bad. */
    h += '<div class="pm-field"><div class="pm-field-label">' +
      _pvEsc(_pvT('paper.videoVisual', 'Composition')) + '</div>' +
      '<div class="pm-options cols-2">' +
      _pvOptCard('videoVisualSel', 'authored', 'gem',
        _pvT('paper.videoVisualAuthored', 'Designed (recommended)'),
        _pvT('paper.videoVisualAuthoredSub', 'bespoke layout per scene'),
        s.visual !== 'template') +
      _pvOptCard('videoVisualSel', 'template', 'zap',
        _pvT('paper.videoVisualTemplate', 'Plain cards'),
        _pvT('paper.videoVisualTemplateSub', 'fastest, one line per card'),
        s.visual === 'template') +
      '</div>' +
      '<select id="videoVisualSel" class="pm-sr" tabindex="-1" aria-hidden="true">' +
      '<option value="authored"' + (s.visual !== 'template' ? ' selected' : '') + '>' +
      _pvEsc(_pvT('paper.videoVisualAuthored', 'Designed (recommended)')) + '</option>' +
      '<option value="template"' + (s.visual === 'template' ? ' selected' : '') + '>' +
      _pvEsc(_pvT('paper.videoVisualTemplate', 'Plain cards')) + '</option></select></div>';
    h += '<div class="pm-field"><div class="pm-field-label">' +
      _pvEsc(_pvT('paper.mediaOptVoice', 'Voice')) +
      '<span class="pm-field-opt">' +
      _pvEsc(_pvT('paper.mediaOptional', 'optional')) + '</span></div>' +
      '<div class="pm-voice-wrap">' + _pvIconSvg('mic') +
      '<input id="videoVoiceInp" type="text" value="' +
      _pvEsc(s.voice) + '" placeholder="' + _pvEsc(s.defaultVoice ||
        _pvT('paper.podcastVoice', 'voice (optional)')) + '" /></div></div>';
    h += '<div class="pm-field"><div class="pm-field-label">' +
      _pvEsc(_pvT('paper.mediaOptExtras', 'Options')) + '</div>' +
      '<div class="pm-toggles">' +
      '<label class="pm-toggle">' +
      '<input id="videoNarrChk" type="checkbox"' + (s.narration ? ' checked' : '') + ' />' +
      '<span class="pm-toggle-track"><span class="pm-toggle-thumb"></span></span>' +
      '<span class="pm-toggle-text"><b>' +
      _pvEsc(_pvT('paper.videoNarration', 'Narration')) + '</b><small>' +
      _pvEsc(_pvT('paper.videoNarrationSub', 'TTS voice-over')) +
      '</small></span></label>' +
      '<label class="pm-toggle">' +
      '<input id="videoBurnChk" type="checkbox"' + (s.burnIn ? ' checked' : '') + ' />' +
      '<span class="pm-toggle-track"><span class="pm-toggle-thumb"></span></span>' +
      '<span class="pm-toggle-text"><b>' +
      _pvEsc(_pvT('paper.videoBurnIn', 'Burn-in subtitles')) + '</b><small>' +
      _pvEsc(_pvT('paper.videoBurnInSub', 'subtitles baked into the frame')) +
      '</small></span></label>' +
      '</div></div>';
    h += '<button class="paper-podcast-btn pm-cta" data-tofu-action="_videoGenerate()">' +
      _pvIconSvg('play') + '<span>' +
      _pvEsc(_pvT('paper.videoGenerate', 'Generate video')) + '</span></button>';
    if (s.status === 'error' && s.errorText) {
      h += '<div class="paper-podcast-error">' + _pvEsc(s.errorText) + '</div>';
    }
    h += '</div>';
    host.innerHTML = h;
    return;
  }

  if (s.status === 'generating') {
    h += '<div class="paper-podcast-card pm-console">';
    h += '<div class="pm-console-head">' +
      '<span class="pm-clap" aria-hidden="true">' + _pvIconSvg('film') + '</span>' +
      '<span class="pm-console-title">' +
      _pvEsc(_pvT('paper.videoMakingTitle', 'Producing your video')) + '</span>' +
      '<button class="paper-podcast-btn paper-podcast-btn-ghost pm-console-abort" data-tofu-action="_videoAbort()">' +
      _pvEsc(_pvT('paper.podcastAbort', 'Abort')) + '</button></div>';
    h += _pvStepper();
    h += '<div class="paper-podcast-progress">';
    h += '<span class="paper-podcast-spinner"></span>';
    h += '<span id="videoProgressLine">' +
      _pvEsc(_pvT('paper.videoStarting', 'Starting…')) + '</span>';
    h += '</div>';
    h += '<div class="pm-renderbar" aria-hidden="true"></div>';
    h += '<div class="paper-media-activity" id="videoActivityLine"></div>';
    // P-UX3: the grid fills in scene-by-scene as scene_done events land.
    h += '<div class="paper-video-grid" id="paperVideoGrid"></div>';
    h += '</div>';
    host.innerHTML = h;
    _pvRenderProgress();
    _pvRenderActivity();
    _pvStartTick();
    _pvRenderSceneGrid(0);
    return;
  }

  /* P-UX1/P-UX4 terminal honest states. */
  if (s.status === 'lost' || s.status === 'interrupted') {
    var lost = s.status === 'lost';
    host.innerHTML =
      '<div class="paper-podcast-hero">' +
      '<div class="paper-podcast-hero-icon is-warn">' + _pvHeroIconSvg('warn') + '</div>' +
      '<div class="paper-podcast-hero-sub">' + _pvEsc(lost
        ? _pvT('paper.podcastLost', 'Task lost or connection dropped — the generation task can no longer be reached.')
        : _pvT('paper.podcastInterrupted', 'The last generation was cut short by a server restart.')) + '</div>' +
      '<div class="paper-podcast-actions">' +
      '<button class="paper-podcast-btn paper-podcast-btn-ghost" data-tofu-action="_initVideoTab(true)">' +
      _pvEsc(_pvT('paper.podcastRecheck', 'Re-check status')) + '</button>' +
      '<button class="paper-podcast-btn" data-tofu-action="_videoGenerate(true)">' +
      _pvEsc(_pvT('paper.podcastRegenerate', 'Regenerate')) + '</button>' +
      '</div></div>';
    return;
  }

  // done
  var r = s.result || {};
  var tid = s._doneTaskId || '';
  var fileUrl = tid ? Api.motion.fileUrl(tid) : '';
  h += '<div class="paper-podcast-card pm-studio">';
  h += '<div class="paper-podcast-head">';
  h += '<span class="paper-podcast-title">' +
    _pvEsc(_pvT('paper.videoHeroTitle', 'Watch this paper')) + '</span>';
  h += '<span class="paper-podcast-badge">' + _pvEsc(s.quality) + ' · ' +
    _pvEsc(s.lang) + '</span>';
  if (s.artifactModel) {
    h += '<span class="paper-podcast-badge" id="videoModelBadge" title="' +
      _pvEsc(_pvT('paper.mediaModelTitle', 'Model used for generation')) + '">' +
      _pvEsc(_pmShortName(s.artifactModel)) + '</span>';
  }
  if (r.duration) {
    var mm = Math.floor(r.duration / 60), ss = Math.round(r.duration % 60);
    h += '<span class="paper-podcast-badge">' + mm + ':' +
      (ss < 10 ? '0' : '') + ss + '</span>';
  }
  if (r.narrated === false) {
    h += '<span class="paper-podcast-badge">' +
      _pvEsc(_pvT('paper.videoSilent', 'silent')) + '</span>';
  }
  if (r.audio && r.audio.enabled) {
    var audioBits = [];
    if (r.audio.bgm) audioBits.push('BGM');
    if (r.audio.cues) audioBits.push(String(r.audio.cues) + ' SFX');
    h += '<span class="paper-podcast-badge">' +
      _pvEsc(audioBits.join(' · ')) + '</span>';
  }
  h += '</div>';
  h += _pvQualityBanner();
  if (fileUrl) {
    h += '<video id="paperVideoPlayer" class="paper-video-player" controls ' +
      'preload="metadata" src="' + _pvEsc(fileUrl) + '"></video>';
    h += '<div class="paper-podcast-actions">';
    h += _pmModelInlineHtml('video', s);
    h += '<a class="paper-podcast-btn" href="' + _pvEsc(fileUrl) +
      '" download="paper-video-' + (s.paperHash || '').slice(0, 8) + '.mp4">' +
      _pvIconSvg('download') + '<span>' +
      _pvEsc(_pvT('paper.videoDownload', 'Download video')) + '</span></a>';
    h += '<a class="paper-podcast-btn paper-podcast-btn-ghost" href="' +
      _pvEsc(Api.motion.fileUrl(tid, 'srt')) + '" download="paper-video-' +
      (s.paperHash || '').slice(0, 8) + '.srt">' + _pvIconSvg('file') +
      '<span>' + _pvEsc(_pvT('paper.videoDownloadSrt', 'Download SRT')) + '</span></a>';
    if (r.audio && r.audio.enabled) {
      h += '<a class="paper-podcast-btn paper-podcast-btn-ghost" href="' +
        _pvEsc(Api.motion.fileUrl(tid, 'audio-attribution')) +
        '" download="paper-video-audio-attribution.txt">' +
        _pvIconSvg('file') + '<span>' +
        _pvEsc(_pvT('paper.videoDownloadAudioAttribution',
                    'Audio attribution')) + '</span></a>';
    }
    h += '<button class="paper-podcast-btn paper-podcast-btn-ghost" data-tofu-action="_videoGenerate(true)">' +
      _pvIconSvg('refresh') + '<span>' +
      _pvEsc(_pvT('paper.podcastRegenerate', 'Regenerate')) + '</span></button>';
    h += '</div>';
  }
  h += '<div class="paper-video-grid" id="paperVideoGrid"></div>';
  h += '</div>';
  host.innerHTML = h;
  _pvRenderSceneGrid(0);
}

/** Scene grid: per-scene preview (its own mp4) + regen button. */
function _pvRenderSceneGrid(cacheBust) {
  var grid = document.getElementById('paperVideoGrid');
  if (!grid) return;
  var s = _pvideo;
  var tid = s._doneTaskId || s.taskId || '';
  if (!tid || !(s.scenes || []).length) { grid.innerHTML = ''; return; }
  var h = '<div class="paper-video-grid-title">' +
    _pvEsc(_pvT('paper.videoScenesTitle', 'Scenes — preview or re-render one')) +
    '</div><div class="paper-video-grid-row">';
  var generating = s.status === 'generating';
  s.scenes.forEach(function(sc) {
    var sid = sc.scene_id;
    var regening = s.regenSceneId === sid;
    var src = Api.motion.sceneFileUrl(tid, sid) +
      (cacheBust ? '?v=' + cacheBust : '');
    h += '<div class="paper-video-cell' + (regening ? ' is-regening' : '') +
      (generating && !sc.has_video ? ' is-pending' : '') + '">';
    if (sc.has_video) {
      h += '<video class="paper-video-thumb" preload="metadata" muted ' +
        'src="' + _pvEsc(src) + '"' +
        ' data-tofu-action="this.paused?this.play():this.pause()"></video>';
    } else {
      h += '<div class="paper-video-thumb paper-video-thumb-empty">…</div>';
    }
    if (sc.shot_recipe || sc.motion_family) {
      var energy = sc.shot_energy != null
        ? ' · E' + Number(sc.shot_energy) : '';
      var transitionDuration = Number(sc.transition_in_duration_s || 0);
      var transition = transitionDuration > 0
        ? ' · ' + String(sc.transition_in || 'transition') + ' ' +
          transitionDuration.toFixed(2).replace(/0+$/, '').replace(/\.$/, '') + 's'
        : '';
      h += '<div class="paper-video-cell-meta" title="' +
        _pvEsc(sc.motion_family || '') + '">' +
        _pvEsc(sc.shot_recipe || sc.motion_family || '') + _pvEsc(energy) +
        _pvEsc(transition) +
        '</div>';
    }
    h += '<div class="paper-video-cell-text" title="' + _pvEsc(sc.text || '') + '">' +
      _pvEsc((sc.text || '').slice(0, 42)) + '</div>';
    if (!generating) {
      h += '<button class="paper-video-regen" data-scene="' + _pvEsc(sid) + '"' +
        (regening ? ' disabled' : '') +
        ' data-tofu-action="_videoRegenScene(\'' + _pvEsc(sid) + '\')">' +
        (regening ? _pvEsc(_pvT('paper.videoRegening', 'Re-rendering…'))
                  : _pvEsc(_pvT('paper.videoRegen', 'Re-render'))) + '</button>';
    }
    h += '</div>';
  });
  h += '</div>';
  grid.innerHTML = h;
}

