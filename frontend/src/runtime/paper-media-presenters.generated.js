// @ts-check
/* Generated lazy retained runtime: paper-media-presenters. Do not edit directly. */
import { featureRegistry as runtimeScope } from '../feature-registry';
import { t } from '../i18n/index';
import { escapeHtml } from '../html-safety';
import { modelFieldHtml, modelInlineHtml, shortModelName } from '../features/paper/media-model-ui';
import { startPodcastTick } from '../features/paper/podcast-runtime';
import { startVideoTick } from '../features/paper/video-runtime';

const Api = runtimeScope.Api;
if (!Api || typeof Api !== 'object') throw new Error('paper-media-presenters runtime dependency is unavailable: Api');
/* ===== migrated source: paper/podcast.js ===== */
/* ═══════════════════════════════════════════
   paper/podcast.js — Paper Podcast tab

   Turns a paper report into a listenable solo podcast
   (docs/modules/ingest_media.md). Server owns the task
   (/api/v1/paper/podcast/*); this module only renders + polls —
   same transport shape as the report tab beside it.

   States rendered into #paperPodcastContent:
     idle / generating (progress) / done (player + transcript)
     script_only (no TTS slot → script + transcript, honest banner)
     report_required (chain to the Report tab) / error
   ═══════════════════════════════════════════ */

// ── State ──
var _podcast = {
  paperHash: '',
  mode: 'short',
  lang: 'zh',
  voice: '',
  model: '',           // the NEXT run's pick (persisted; see _pmSeedModel)
  artifactModel: '',   // what the DISPLAYED artifact was made with ('' = unknown)
  taskId: '',
  cursor: 0,
  pollTimer: null,
  pollBusy: false,
  status: 'idle',          // idle|generating|done|script_only|report_required|lookup_failed|lost|interrupted|error
  data: null,              // {script, meta, audioUrl, durationSec, scriptOnly}
  errorText: '',
  progress: { done: 0, total: 0 },
  ttsAvailable: true,
  defaultVoice: '',
  sleepTimerId: 0,
  sleepDeadline: 0,
  // P-UX progress perception (docs/modules/ingest_media.md §3.4)
  pollFails: 0,            // consecutive poll failures → 5 = lost state
  phases: [],              // server phase vocabulary (phase_started.phases)
  phaseIndex: 0,           // 1-based index of the current phase
  currentPhase: '',
  scriptStep: '',          // draft|validate|revise|critic (script sub-step)
  scriptChars: 0,          // chars streamed so far in the current draft pass
  scriptSegments: 0,       // segments started so far (counted from the stream)
  scriptCharTarget: 0,     // the char target the prompt actually instructed
  genStartedAt: 0,         // local stopwatch start
  lastEventAt: 0,          // last event/poll-success time (liveness)
  tickTimer: null,         // 1s UI ticker for elapsed/last-active
  _segFirstTick: 0,        // wall-clock of the first segment_done (ETA)
  etaSec: 0,
};
runtimeScope._podcast = _podcast;
/* Mode/lang persist like the model pick (paperPodcastModel): a full page
 * reload must re-issue the SAME (mode, lang) the run was started with —
 * the backend re-attach scan matches (paper_hash, mode, lang) exactly, so
 * a 'full' run was invisible to a panel that had reset to 'short'. Values
 * are validated on read; anything unexpected keeps the in-state default. */
function _pcSeedOptions() {
  var mode = '', lang = '';
  try {
    mode = localStorage.getItem('paperPodcastMode') || '';
    lang = localStorage.getItem('paperPodcastLang') || '';
  } catch (e) {}
  if (mode === 'short' || mode === 'full') _podcast.mode = mode;
  if (lang === 'zh' || lang === 'en') _podcast.lang = lang;
}

function _pcPersistOptions() {
  try {
    localStorage.setItem('paperPodcastMode', _podcast.mode);
    localStorage.setItem('paperPodcastLang', _podcast.lang);
  } catch (e) {}
}

/* _pmPick hook (called from the shared card picker): sync a podcast option
 * card into state + storage at PICK time, not only at generate time. A
 * no-op for video's selects — its lookup is paper_hash-only. */
function _pcPickPersist(selId, value) {
  if (selId === 'podcastModeSel' && (value === 'short' || value === 'full')) {
    _podcast.mode = value;
  } else if (selId === 'podcastLangSel' && (value === 'zh' || value === 'en')) {
    _podcast.lang = value;
  } else {
    return;
  }
  _pcPersistOptions();
}

// Poll cadence — a var (not const) so the JSDOM harness can shrink it.
var _PODCAST_POLL_MS = 1200;
// Consecutive poll failures before the honest 'lost' terminal state (拍板 A).
var _PC_POLL_FAIL_LIMIT = 5;

// Explicit retained presentation ports consumed by the lazy typed runtime.
// These stay module-private: featureRegistry resolves them through runtimeScope.
Object.assign(runtimeScope, {
  _pcSeedOptions,
  _pcPersistOptions,
  _pcPickPersist,
  _pcT,
  _pcEsc,
  _pcRender,
  _pcRenderProgress,
  _pcRenderActivity,
  _podcastSeekSegment,
  _podcastSleepTimerChange,
  _podcastExportScript,
});

function _pcT(key, fallback) {
  return (typeof t === 'function') ? t(key) : (fallback || key);
}

function _pcEl() { return document.getElementById('paperPodcastContent'); }

function _pcEsc(s) {
  return (typeof escapeHtml === 'function') ? escapeHtml(s == null ? '' : s)
    : String(s == null ? '' : s);
}

/* Stop the poll timer ONLY.
 *
 * The 1s activity ticker is deliberately NOT stopped here: _pcSchedulePoll()
 * calls this before arming the next poll, so folding the ticker in would kill
 * the elapsed/last-activity stopwatch on the FIRST poll and freeze the line
 * at 0:00 for the rest of the run — exactly the "looks stuck" symptom the
 * liveness line exists to prevent. Terminal states call _pcStopPolling()
 * instead, which stops both. */
function _pcHeroIconSvg(kind) {
  if (kind === 'podcast') {
    return '<svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3zM3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"/></svg>';
  }
  return '<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>';
}

function _pcRenderProgress() {
  var el = document.getElementById('podcastProgressLine');
  if (!el) return;
  var p = _podcast.progress;
  var line;
  if (p.total > 0) {
    line = _pcT('paper.podcastAudioPhase', 'Synthesizing audio') + ' ' +
      p.done + '/' + p.total;
    if (_podcast.etaSec > 0) {
      line += ' · ' + _pcT('paper.mediaEtaPrefix', '≈') + _pcFmtSec(_podcast.etaSec);
    }
  } else {
    line = _pcT('paper.podcastScriptPhase', 'Writing the spoken script…');
    var stepMap = { draft: 'paper.podcastStepDraft', validate: 'paper.podcastStepValidate',
                    revise: 'paper.podcastStepRevise', critic: 'paper.podcastStepCritic' };
    var fallback = { draft: 'draft done', validate: 'checking quality',
                     revise: 'revising', critic: 'editor review' };
    /* During the draft pass the counters stream in, so show what has really
     * been written instead of a label that cannot change for 1-3 minutes.
     * Only measured numbers: segments started, and chars against the target
     * the prompt instructed. No invented percentage or segment denominator —
     * the prompt bounds total LENGTH, never a segment count. */
    if (_podcast.scriptChars > 0 &&
        (_podcast.scriptStep === 'draft' || _podcast.scriptStep === 'revise')) {
      if (_podcast.scriptStep === 'revise') {
        line += ' · ' + _pcT(stepMap.revise, fallback.revise);
      }
      if (_podcast.scriptSegments > 0) {
        line += ' · ' + _pcT('paper.podcastStreamSegments', 'segment') + ' ' +
          _podcast.scriptSegments;
      }
      line += ' · ' + _podcast.scriptChars +
        (_podcast.scriptCharTarget > 0 ? '/~' + _podcast.scriptCharTarget : '') +
        ' ' + _pcT('paper.podcastStreamChars', 'chars');
    } else if (_podcast.scriptStep) {
      line += ' · ' + _pcT(stepMap[_podcast.scriptStep] || '',
                          fallback[_podcast.scriptStep] || _podcast.scriptStep);
    }
  }
  el.textContent = line;
}

/** Phase stepper (P-UX2): 素材 → 剧本 → 配音, done ✓ / active ● / todo ○. */
function _pcStepper() {
  var phases = _podcast.phases.length ? _podcast.phases : ['source', 'script', 'audio'];
  var labelMap = { source: ['paper.podcastPhaseSource', 'Material'],
                   script: ['paper.podcastPhaseScript', 'Script'],
                   audio: ['paper.podcastPhaseAudio', 'Voice-over'] };
  var cur = Math.max(_podcast.phaseIndex, 1);
  var h = '<div class="paper-stepper">';
  phases.forEach(function(ph, i) {
    var idx = i + 1;
    var state = idx < cur ? 'is-done' : (idx === cur ? 'is-active' : '');
    var mark = idx < cur ? '✓' : (idx === cur ? '●' : '○');
    var lab = labelMap[ph] || ['', ph];
    h += '<span class="paper-step ' + state + '">' +
      '<span class="paper-step-mark">' + mark + '</span>' +
      _pcEsc(_pcT(lab[0], lab[1])) + '</span>';
    if (idx < phases.length) h += '<span class="paper-step-sep"></span>';
  });
  return h + '</div>';
}

/** Liveness line (P-UX2): elapsed stopwatch + "last activity Xs ago";
 * goes visibly stale after 30s of silence (quiet ≠ dead). */
function _pcRenderActivity() {
  var el = document.getElementById('podcastActivityLine');
  if (!el || _podcast.status !== 'generating') return;
  var elapsed = Math.max(0, Math.round((Date.now() - _podcast.genStartedAt) / 1000));
  var quiet = Math.max(0, Math.round((Date.now() - _podcast.lastEventAt) / 1000));
  var txt = _pcT('paper.mediaElapsed', 'elapsed') + ' ' + _pcFmtSec(elapsed) +
    ' · ' + _pcT('paper.mediaLastActive', 'last activity') + ' ' +
    _pcFmtSec(quiet) + '';
  el.classList.toggle('is-stale', quiet > 30);
  el.textContent = txt + (quiet > 30 ? ' — ' +
    _pcT('paper.mediaStillRunning', 'still running (this step can take minutes)') : '');
}

function _pcDegradeBanner() {
  return '<div class="paper-podcast-banner">' +
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>' +
    '<span>' + _pcEsc(_pcT('paper.podcastNoTts',
      'No TTS voice slot is configured — this run generates the script + transcript only.')) +
    '</span></div>';
}

/* Studio icon set (SVG only, never emoji). */
function _pcIconSvg(name) {
  if (name === 'clock') {
    return '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15.5 14"/></svg>';
  }
  if (name === 'waves') {
    return '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M2 12c2-3 4-3 6 0s4 3 6 0 4-3 6 0"/><path d="M2 17c2-3 4-3 6 0s4 3 6 0 4-3 6 0" opacity=".55"/><path d="M2 7c2-3 4-3 6 0s4 3 6 0 4-3 6 0" opacity=".55"/></svg>';
  }
  if (name === 'mic') {
    return '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0 0 14 0"/><line x1="12" y1="17" x2="12" y2="21"/></svg>';
  }
  if (name === 'play') {
    return '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M7 4.5v15l13-7.5z"/></svg>';
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
  if (name === 'disc') {
    return '<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5.5" opacity=".45"/><circle cx="12" cy="12" r="1.8" fill="currentColor" stroke="none"/></svg>';
  }
  if (name === 'moon') {
    return '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
  }
  return '';
}

/** One rich option card bound to a hidden select (see _pmPick). */
function _pcOptCard(selId, value, icon, title, sub, selected) {
  return '<button type="button" class="pm-opt' + (selected ? ' is-selected' : '') +
    '" data-sel="' + selId + '" data-value="' + value + '" data-tofu-action="_pmPick(this)">' +
    '<span class="pm-opt-icon">' + _pcIconSvg(icon) + '</span>' +
    '<span class="pm-opt-title">' + _pcEsc(title) + '</span>' +
    '<span class="pm-opt-sub">' + _pcEsc(sub) + '</span></button>';
}

/** One segment of a segmented control bound to a hidden select. */
function _pcSegBtn(selId, value, label, selected) {
  return '<button type="button" class="pm-seg-btn' + (selected ? ' is-selected' : '') +
    '" data-sel="' + selId + '" data-value="' + value + '" data-tofu-action="_pmPick(this)">' +
    _pcEsc(label) + '</button>';
}

function _pcRender() {
  var host = _pcEl();
  if (!host) return;
  var s = _podcast;
  var h = '';

  if (s.status === 'loading') {
    host.innerHTML = '<div class="paper-report-empty"><p>…</p></div>';
    return;
  }

  if (s.status === 'report_required') {
    host.innerHTML =
      '<div class="paper-podcast-hero">' +
      '<div class="paper-podcast-hero-icon">' + _pcHeroIconSvg('podcast') + '</div>' +
      '<div class="paper-podcast-hero-title">' +
      _pcEsc(_pcT('paper.podcastHeroTitle', 'Listen to this paper')) + '</div>' +
      '<div class="paper-podcast-hero-sub">' +
      _pcEsc(_pcT('paper.podcastNeedReport',
        'The podcast is adapted from the analysis report — generate the report first.')) + '</div>' +
      '<div class="paper-podcast-hero-steps">' +
      '<span class="paper-podcast-hero-step is-active">' +
      _pcEsc(_pcT('paper.podcastStepReport', '1. Generate the report')) + '</span>' +
      '<span class="paper-podcast-hero-arrow">' +
      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg></span>' +
      '<span class="paper-podcast-hero-step">' +
      _pcEsc(_pcT('paper.podcastStepPodcast', '2. Adapt into a podcast')) + '</span>' +
      '</div>' +
      '<button class="paper-podcast-btn" data-tofu-action="_switchPaperTab(\'report\')">' +
      _pcEsc(_pcT('paper.podcastGoReport', 'Go generate the report')) + '</button>' +
      '</div>';
    return;
  }

  /* Lookup failure (server error / unreachable) — honest state with a retry;
   * deliberately NOT report_required: a 5xx proves nothing about the report. */
  if (s.status === 'lookup_failed') {
    host.innerHTML =
      '<div class="paper-podcast-hero">' +
      '<div class="paper-podcast-hero-icon is-warn">' + _pcHeroIconSvg('warn') + '</div>' +
      '<div class="paper-podcast-hero-sub">' + _pcEsc(s.errorText ||
        _pcT('paper.podcastLookupFailed', 'Podcast status lookup failed — check the server log.')) + '</div>' +
      '<button class="paper-podcast-btn paper-podcast-btn-ghost" data-tofu-action="_initPodcastTab(true)">' +
      _pcEsc(_pcT('paper.podcastRetry', 'Retry')) + '</button>' +
      '</div>';
    return;
  }

  // Studio console card (mode/lang/voice pickers + generate CTA) is always
  // available in idle/error so a re-roll is one click. The rich option cards
  // write into the hidden selects (_pmPick) — the generate path still reads
  // the selects, so the contract never leaves the DOM.
  if (s.status === 'idle' || s.status === 'error') {
    h += '<div class="paper-podcast-card pm-studio">';
    h += '<div class="pm-studio-head">' +
      '<div class="pm-studio-badge">' + _pcHeroIconSvg('podcast') + '</div>' +
      '<div class="pm-studio-head-text">' +
      '<div class="pm-studio-title">' +
      _pcEsc(_pcT('paper.podcastStudioTitle', 'Podcast studio')) + '</div>' +
      '<div class="pm-studio-sub">' + _pcEsc(_pcT('paper.podcastHint',
        'A solo spoken deep-read of this paper — for the commute or before sleep.')) +
      '</div></div></div>';
    if (!s.ttsAvailable) h += _pcDegradeBanner();
    h += '<div class="pm-field"><div class="pm-field-label">' +
      _pcEsc(_pcT('paper.mediaOptDuration', 'Duration')) + '</div>' +
      '<div class="pm-options">' +
      _pcOptCard('podcastModeSel', 'short', 'clock',
        _pcT('paper.podcastModeShortName', 'Quick brief'),
        _pcT('paper.podcastModeShortSub', '~5 min · for the commute'),
        s.mode === 'short') +
      _pcOptCard('podcastModeSel', 'full', 'waves',
        _pcT('paper.podcastModeFullName', 'Full deep-read'),
        _pcT('paper.podcastModeFullSub', '~15 min · before sleep'),
        s.mode === 'full') +
      '</div>' +
      '<select id="podcastModeSel" class="pm-sr" tabindex="-1" aria-hidden="true">' +
      '<option value="short"' + (s.mode === 'short' ? ' selected' : '') + '>' +
      _pcEsc(_pcT('paper.podcastModeShort', 'Short · ~5 min')) + '</option>' +
      '<option value="full"' + (s.mode === 'full' ? ' selected' : '') + '>' +
      _pcEsc(_pcT('paper.podcastModeFull', 'Full · ~15 min')) + '</option></select></div>';
    h += '<div class="pm-field"><div class="pm-field-label">' +
      _pcEsc(_pcT('paper.mediaOptLang', 'Language')) + '</div>' +
      '<div class="pm-seg">' +
      _pcSegBtn('podcastLangSel', 'zh', '中文', s.lang === 'zh') +
      _pcSegBtn('podcastLangSel', 'en', 'English', s.lang === 'en') +
      '</div>' +
      '<select id="podcastLangSel" class="pm-sr" tabindex="-1" aria-hidden="true">' +
      '<option value="zh"' + (s.lang === 'zh' ? ' selected' : '') + '>中文</option>' +
      '<option value="en"' + (s.lang === 'en' ? ' selected' : '') + '>English</option></select></div>';
    h += modelFieldHtml('podcast', s);
    h += '<div class="pm-field"><div class="pm-field-label">' +
      _pcEsc(_pcT('paper.mediaOptVoice', 'Voice')) +
      '<span class="pm-field-opt">' +
      _pcEsc(_pcT('paper.mediaOptional', 'optional')) + '</span></div>' +
      '<div class="pm-voice-wrap">' + _pcIconSvg('mic') +
      '<input id="podcastVoiceInp" type="text" value="' +
      _pcEsc(s.voice) + '" placeholder="' + _pcEsc(s.defaultVoice ||
        _pcT('paper.podcastVoice', 'voice (optional)')) + '" /></div></div>';
    h += '<button class="paper-podcast-btn pm-cta" data-tofu-action="_podcastGenerate()">' +
      _pcIconSvg('play') + '<span>' +
      _pcEsc(_pcT('paper.podcastGenerate', 'Generate podcast')) + '</span></button>';
    if (s.status === 'error' && s.errorText) {
      h += '<div class="paper-podcast-error">' + _pcEsc(s.errorText) + '</div>';
    }
    h += '</div>';
    host.innerHTML = h;
    return;
  }

  if (s.status === 'generating') {
    h += '<div class="paper-podcast-card pm-console">';
    h += '<div class="pm-console-head">' +
      '<span class="pm-eq" aria-hidden="true"><i></i><i></i><i></i><i></i></span>' +
      '<span class="pm-console-title">' +
      _pcEsc(_pcT('paper.podcastMakingTitle', 'Producing your podcast')) + '</span>' +
      '<button class="paper-podcast-btn paper-podcast-btn-ghost pm-console-abort" data-tofu-action="_podcastAbort()">' +
      _pcEsc(_pcT('paper.podcastAbort', 'Abort')) + '</button></div>';
    h += _pcStepper();
    h += '<div class="paper-podcast-progress">';
    h += '<span class="paper-podcast-spinner"></span>';
    h += '<span id="podcastProgressLine">' +
      _pcEsc(_pcT('paper.podcastScriptPhase', 'Writing the spoken script…')) + '</span>';
    h += '</div>';
    h += '<div class="paper-media-activity" id="podcastActivityLine"></div>';
    h += '</div>';
    host.innerHTML = h;
    _pcRenderProgress();
    _pcRenderActivity();
    startPodcastTick();
    return;
  }

  /* P-UX1/P-UX4 terminal honest states. */
  if (s.status === 'lost' || s.status === 'interrupted') {
    var lost = s.status === 'lost';
    host.innerHTML =
      '<div class="paper-podcast-hero">' +
      '<div class="paper-podcast-hero-icon is-warn">' + _pcHeroIconSvg('warn') + '</div>' +
      '<div class="paper-podcast-hero-sub">' + _pcEsc(lost
        ? _pcT('paper.podcastLost', 'Task lost or connection dropped — the generation task can no longer be reached.')
        : _pcT('paper.podcastInterrupted', 'The last generation was cut short by a server restart.')) + '</div>' +
      '<div class="paper-podcast-actions">' +
      '<button class="paper-podcast-btn paper-podcast-btn-ghost" data-tofu-action="_initPodcastTab(true)">' +
      _pcEsc(_pcT('paper.podcastRecheck', 'Re-check status')) + '</button>' +
      '<button class="paper-podcast-btn" data-tofu-action="_podcastGenerate(true)">' +
      _pcEsc(_pcT('paper.podcastRegenerate', 'Regenerate')) + '</button>' +
      '</div></div>';
    return;
  }

  // done / script_only
  var d = s.data || {};
  var script = d.script || {};
  var meta = d.meta || {};
  var segs = script.segments || [];
  var scriptOnly = s.status === 'script_only';
  var audioUrl = d.audioUrl || '';
  var ext = (meta.container === 'wav') ? 'wav' : (meta.container === 'mp3' ? 'mp3' : 'bin');
  var dlName = 'paper-podcast-' + s.mode + '-' + (s.paperHash || '').slice(0, 8) + '.' + ext;

  h += '<div class="paper-podcast-card pm-studio">';
  if (scriptOnly) h += _pcDegradeBanner();
  h += '<div class="paper-podcast-head">';
  h += '<span class="paper-podcast-title">' + _pcEsc(script.title || '') + '</span>';
  h += '<span class="paper-podcast-badge">' + _pcEsc(s.mode) + ' · ' + _pcEsc(s.lang) + '</span>';
  if (s.artifactModel) {
    h += '<span class="paper-podcast-badge" id="podcastModelBadge" title="' +
      _pcEsc(_pcT('paper.mediaModelTitle', 'Model used for generation')) + '">' +
      _pcEsc(shortModelName(s.artifactModel)) + '</span>';
  }
  if (d.durationSec) {
    var mm = Math.floor(d.durationSec / 60), ss = Math.round(d.durationSec % 60);
    h += '<span class="paper-podcast-badge">' + mm + ':' + (ss < 10 ? '0' : '') + ss +
      (meta.duration_estimated ? '≈' : '') + '</span>';
  }
  h += '</div>';
  if (meta.low_confidence) {
    h += '<div class="paper-podcast-banner paper-podcast-banner-warn">' +
      _pcEsc(_pcT('paper.podcastLowConfidence',
        'QA gates did not fully pass — some content may be imprecise.')) + '</div>';
  }

  if (!scriptOnly && audioUrl) {
    h += '<div class="pm-player" id="podcastPlayerWrap">' +
      '<span class="pm-player-disc">' + _pcIconSvg('disc') + '</span>' +
      '<audio id="podcastAudio" controls preload="metadata" src="' +
      _pcEsc(audioUrl) + '"></audio></div>';
    h += '<div class="paper-podcast-actions">';
    h += '<a class="paper-podcast-btn" href="' + _pcEsc(audioUrl) +
      '" download="' + _pcEsc(dlName) + '">' + _pcIconSvg('download') +
      '<span>' + _pcEsc(_pcT('paper.podcastDownloadAudio', 'Download audio')) +
      '</span></a>';
    h += '<label class="paper-podcast-sleep">' + _pcIconSvg('moon') +
      _pcEsc(_pcT('paper.podcastSleepTimer', 'Sleep timer')) + ' ' +
      '<select id="podcastSleepSel" class="paper-podcast-sel" data-tofu-action-change="_podcastSleepTimerChange()">' +
      '<option value="0">' + _pcEsc(_pcT('paper.podcastSleepOff', 'Off')) + '</option>' +
      '<option value="5">5 ' + _pcEsc(_pcT('paper.podcastSleepMin', 'min')) + '</option>' +
      '<option value="10">10 ' + _pcEsc(_pcT('paper.podcastSleepMin', 'min')) + '</option>' +
      '<option value="15">15 ' + _pcEsc(_pcT('paper.podcastSleepMin', 'min')) + '</option>' +
      '<option value="30">30 ' + _pcEsc(_pcT('paper.podcastSleepMin', 'min')) + '</option>' +
      '<option value="45">45 ' + _pcEsc(_pcT('paper.podcastSleepMin', 'min')) + '</option>' +
      '</select><span id="podcastSleepNote" class="paper-podcast-sleep-note"></span></label>';
    h += '</div>';
  }

  h += '<div class="paper-podcast-actions">';
  h += modelInlineHtml('podcast', s);
  h += '<button class="paper-podcast-btn paper-podcast-btn-ghost" data-tofu-action="_podcastExportScript()">' +
    _pcIconSvg('file') + '<span>' +
    _pcEsc(_pcT('paper.podcastExportScript', 'Export script (md)')) + '</span></button>';
  h += '<button class="paper-podcast-btn paper-podcast-btn-ghost" data-tofu-action="_podcastGenerate(true)">' +
    _pcIconSvg('refresh') + '<span>' +
    _pcEsc(_pcT('paper.podcastRegenerate', 'Regenerate')) + '</span></button>';
  h += '</div>';
  h += '<div class="pm-transcript-head">' +
    _pcEsc(_pcT('paper.podcastTranscriptTitle', 'Transcript')) + '</div>';

  // Transcript: click a segment to seek (audio mode); prefix sums of
  // est_seconds give the seek offsets.
  var starts = [], acc = 0;
  segs.forEach(function(sg) { starts.push(acc); acc += (sg.est_seconds || 0); });
  h += '<div class="paper-podcast-transcript" id="podcastTranscript">';
  segs.forEach(function(sg, i) {
    h += '<div class="paper-podcast-seg" data-seg="' + i + '"' +
      (!scriptOnly ? ' data-tofu-action="_podcastSeekSegment(' + i + ')"' : '') + '>' +
      '<span class="paper-podcast-seg-time">' + _pcFmtSec(starts[i]) + '</span>' +
      '<p>' + _pcEsc(sg.text) + '</p></div>';
  });
  h += '</div></div>';
  host.innerHTML = h;

  if (!scriptOnly && audioUrl) {
    var audio = /** @type {HTMLAudioElement} */ (document.getElementById('podcastAudio'));
    if (audio) {
      audio.addEventListener('timeupdate', function() {
        _pcHighlightSegment(audio.currentTime, starts);
        _pcSleepTick(audio);
      });
      /* Spinning vinyl while playing — pure presentation, no state. */
      audio.addEventListener('play', function() {
        var w = document.getElementById('podcastPlayerWrap');
        if (w) w.classList.add('is-playing');
      });
      audio.addEventListener('pause', function() {
        var w = document.getElementById('podcastPlayerWrap');
        if (w) w.classList.remove('is-playing');
      });
    }
  }
}

function _pcFmtSec(x) {
  x = Math.max(0, Math.floor(x || 0));
  return Math.floor(x / 60) + ':' + ('0' + (x % 60)).slice(-2);
}

function _pcHighlightSegment(now, starts) {
  var cur = 0;
  for (var i = 0; i < starts.length; i++) { if (now >= starts[i]) cur = i; }
  var list = document.querySelectorAll('#podcastTranscript .paper-podcast-seg');
  list.forEach(function(el) {
    el.classList.toggle('active', parseInt(el.dataset.seg, 10) === cur);
  });
}

/** Click-to-seek: jump the player to a transcript segment's start offset. */
function _podcastSeekSegment(i) {
  var audio = /** @type {HTMLAudioElement} */ (document.getElementById('podcastAudio'));
  var d = _podcast.data || {};
  var segs = (d.script && d.script.segments) || [];
  if (!audio || !segs.length) return;
  var start = 0;
  for (var k = 0; k < i && k < segs.length; k++) start += (segs[k].est_seconds || 0);
  try { audio.currentTime = start; } catch (e) { console.warn('[Paper:Podcast] seek failed:', e); }
}

// ── Sleep timer (owner P1: "listen before sleep" is a first-class case) ──

function _podcastSleepTimerChange() {
  var sel = document.getElementById('podcastSleepSel');
  var mins = sel ? parseInt(sel.value, 10) || 0 : 0;
  if (_podcast.sleepTimerId) { clearTimeout(_podcast.sleepTimerId); _podcast.sleepTimerId = 0; }
  _podcast.sleepDeadline = 0;
  var note = document.getElementById('podcastSleepNote');
  if (note) note.textContent = '';
  if (mins > 0) {
    _podcast.sleepDeadline = Date.now() + mins * 60000;
    _podcast.sleepTimerId = setTimeout(function() {
      var audio = /** @type {HTMLAudioElement} */ (document.getElementById('podcastAudio'));
      if (audio) { try { audio.pause(); } catch (e) { console.warn('[Paper:Podcast] sleep pause failed:', e); } }
      var n = document.getElementById('podcastSleepNote');
      if (n) n.textContent = '⏸';
      var s2 = document.getElementById('podcastSleepSel');
      if (s2) s2.value = '0';
      _podcast.sleepTimerId = 0;
      _podcast.sleepDeadline = 0;
    }, mins * 60000);
  }
}

/** Update the countdown note on playback ticks (cheap — timeupdate only). */
function _pcSleepTick(audio) {
  if (!_podcast.sleepDeadline) return;
  var note = document.getElementById('podcastSleepNote');
  if (!note) return;
  var left = Math.max(0, _podcast.sleepDeadline - Date.now());
  note.textContent = ' · ' + _pcFmtSec(left / 1000);
}

// ── Script export (client-side markdown) ──

function _podcastExportScript() {
  var d = _podcast.data || {};
  var script = d.script || {};
  var segs = script.segments || [];
  if (!segs.length) return;
  var lines = ['# ' + (script.title || 'Paper Podcast'), ''];
  segs.forEach(function(sg) {
    lines.push('## ' + (sg.section || ''));
    lines.push('');
    lines.push(sg.text || '');
    lines.push('');
  });
  var blob = new Blob([lines.join('\n')], { type: 'text/markdown;charset=utf-8' });
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'paper-podcast-script-' + (_podcast.mode || 'short') + '-' +
    (_podcast.paperHash || '').slice(0, 8) + '.md';
  document.body.appendChild(a);
  a.click();
  setTimeout(function() {
    URL.revokeObjectURL(a.href);
    if (a.parentNode) a.parentNode.removeChild(a);
  }, 0);
}
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

// Explicit retained presentation ports consumed by the lazy typed runtime.
// These stay module-private: featureRegistry resolves them through runtimeScope.
Object.assign(runtimeScope, {
  _pvT,
  _pvEsc,
  _pvRender,
  _pvRenderProgress,
  _pvRenderActivity,
  _pvRenderSceneGrid,
});

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
    h += modelFieldHtml('video', s);
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
    startVideoTick();
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
      _pvEsc(shortModelName(s.artifactModel)) + '</span>';
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
    h += modelInlineHtml('video', s);
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

// BEGIN GENERATED LAZY RUNTIME PORTS — paper-media-presenters
// END GENERATED LAZY RUNTIME PORTS
// BEGIN GENERATED LAZY RUNTIME ACTIONS — paper-media-presenters
runtimeScope._podcastExportScript = _podcastExportScript;
runtimeScope._podcastSeekSegment = _podcastSeekSegment;
runtimeScope._podcastSleepTimerChange = _podcastSleepTimerChange;
// END GENERATED LAZY RUNTIME ACTIONS
