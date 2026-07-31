/* ═════════════════════════════════════════════════
   paper/research.js — auto-research: a DIRECTION → scored ideas

   The Reading-Mode landing entry for the auto-research capability
   (harvest → survey → ideate; docs/AUTO_RESEARCH_SYSTEM_DESIGN.md).

   ── Why the landing screen and not a 7th paper tab ──
   Every `.paper-tab-panel` is PAPER-HASH scoped: `_switchPaperTab` dispatches
   `_loadOrGenerateReport` / `_initVideoTab`, and the video tab looks its job up
   with `Api.paper.videoLookup(_paperHash)`. A research DIRECTION is pre-paper —
   there is no open paper when you start one — so a tab would force the user to
   first open an unrelated paper. This module therefore reads NO paper state.

   ── Why it does not go through the LLM tool ──
   `produce_research` exists as a model tool, but it is (a) only invoked if the
   model elects to and (b) search-gated in `_build_produce`, so it vanishes when
   a user turns web search off. A UI control cannot be built on either. We call
   the capability-agnostic `POST /api/v1/tasks/start` instead.

   ── The three contracts this file owes the user ──
   1. START with no paper open.
   2. Progress that survives a refresh / tab switch — the elapsed clock is
      adopted from the SERVER's `createdAt`, never minted locally (a local
      stopwatch re-mints on every render and washes a long-running job into
      looking fresh).
   3. A degraded job must LOOK degraded. Research keeps `status='done'` when
      its structural gate wipes every idea (status is the LIFECYCLE axis);
      `artifact_quality` is the PRODUCT axis and is the only thing separating
      "good artifact" from "valid artifact out of a broken pipeline".
   ═════════════════════════════════════════════════ */

/** Live research job state — the single source the renderer paints from. */
var _researchStream = null;
var _researchPollTimer = null;

/** Phases the research recipe walks, in order (stepper vocabulary). */
var _RESEARCH_PHASES = ['harvest', 'survey', 'ideate'];

function _newResearchStream(direction) {
  return {
    direction: direction,
    taskId: null,
    status: 'pending',      // pending|running|done|error|aborted
    phase: '',              // harvest|survey|ideate
    startedAt: 0,           // SERVER createdAt (epoch ms) — never Date.now()
    lastEventAt: 0,         // SERVER updatedAt (epoch ms)
    degraded: false,
    degradedReason: '',
    gateReached: '',        // accepted|rubric|structural|none
    accepted: 0,
    rejected: 0,
    corpusSize: 0,
    folderId: '',           // library folder the harvested papers landed in
    error: '',
    // ★ The ARTIFACTS themselves — not just their counts. The pipeline pays
    // many LLM calls to produce these; rendering only `accepted.length` threw
    // away every idea, mechanism and rubric score the user paid for.
    acceptedIdeas: [],      // [{title, core_mechanism, novelty_claim, …}]
    rejectedIdeas: [],      // [{title, reject_reason, scores{4 axes}, overall}]
    threshold: null,        // IDEATE_GATE_THRESHOLD the run was judged against
    surveyMd: '',           // the fan-in survey markdown
    openGaps: null,         // the machine-readable gap map (R3's contract)
    hydrated: false,        // has the durable lookup filled the above in?
  };
}

/**
 * Adopt the server's clocks onto the local state.
 *
 * Min-guard (mirrors `_pmAdoptServerClocks` in the media tabs): the start
 * instant may only ever move EARLIER, and an implausible value is ignored —
 * so a re-attach after a refresh continues the real elapsed time and can never
 * jump backward.
 */
function _researchAdoptServerClocks(st, src) {
  if (!st || !src) return;
  var started = Number(src.createdAt);
  if (isFinite(started) && started > 1e12 && started <= Date.now() + 60000) {
    if (!st.startedAt || started < st.startedAt) st.startedAt = started;
  }
  var seen = Number(src.updatedAt);
  if (isFinite(seen) && seen > 1e12 && seen >= (st.lastEventAt || 0)) {
    st.lastEventAt = seen;
  }
}

/** Fold one task snapshot into the stream state. */
function _researchApplySnapshot(st, snap) {
  if (!st || !snap) return;
  _researchAdoptServerClocks(st, snap);
  if (snap.status) st.status = snap.status;

  // ★ The product-quality axis. A degraded job reports status='done' by
  // design, so reading `status` alone would render a 100% gate wipe as a
  // clean success — the exact bug artifact_quality exists to expose.
  var q = snap.artifact_quality;
  if (q && typeof q === 'object') {
    st.degraded = !!q.degraded;
    st.degradedReason = q.reason || '';
  }

  var res = snap.result;
  if (res && typeof res === 'object') {
    st.accepted = Array.isArray(res.accepted) ? res.accepted.length : 0;
    st.rejected = Array.isArray(res.rejected) ? res.rejected.length : 0;
    st.corpusSize = Number(res.corpus_size) || 0;
    st.gateReached = res.gate_reached || '';
    if (res.folder_id) st.folderId = res.folder_id;
    // The live snapshot already carries the full arrays (the task API returns
    // the whole result dict), so adopt them here too — the durable lookup is
    // the RE-ATTACH path, not the only way to ever see an idea.
    if (Array.isArray(res.accepted)) st.acceptedIdeas = res.accepted;
    if (Array.isArray(res.rejected)) st.rejectedIdeas = res.rejected;
    if (res.threshold != null) st.threshold = res.threshold;
    if (res.survey_md) st.surveyMd = res.survey_md;
    if (res.open_gaps) st.openGaps = res.open_gaps;
  }
  var meta = snap.meta;
  if (meta && meta.direction && !st.direction) st.direction = meta.direction;
}

/** Entry point from the landing screen's research input. */
function _submitResearchDirection() {
  var input = document.getElementById('paperResearchInput');
  var q = input && input.value ? input.value.trim() : '';
  if (!q) { debugLog('Describe the research direction to explore', 'warning'); return; }
  _startResearchJob(q);
}

/**
 * Landing-screen bridge: start a research job from the SHARED describe box.
 *
 * The landing screen's free-text area is already direction-shaped ("describe
 * what you're looking for"), so research reuses it rather than adding a second
 * input the user has to choose between. Recommend and research are two verbs
 * over one direction — that is why neither needs an open paper.
 */
function _startResearchFromDescribe() {
  var input = document.getElementById('paperDescribeInput');
  var q = input && input.value ? input.value.trim() : '';
  if (!q) { debugLog('Describe the research direction to explore', 'warning'); return; }
  _startResearchJob(q);
}

/**
 * Start an auto-research job for `direction` and stream its progress.
 *
 * Reads NO paper state on purpose — see the file header.
 */
async function _startResearchJob(direction) {
  var s = _newResearchStream(direction);
  _researchStream = s;
  _paintResearch();

  try {
    var data = await Api.tasks.start('research', { direction: direction });
    if (!data || !data.ok || !data.taskId) {
      throw new Error((data && data.error) || 'research start failed');
    }
    if (_researchStream !== s) return;      // superseded by a newer submit
    s.taskId = data.taskId;
    s.status = 'running';

    // Live progress over the push channel the runtime already broadcasts on
    // (lib/research/runtime.py sets push_channel='research'); the poll below
    // is the reconnect/catch-up net, not the primary transport.
    try { pushSubscribe('research', s.taskId, function (ev) {
      if (_researchStream !== s) return;
      if (ev && ev.phase) s.phase = ev.phase;
      if (ev && ev.type === 'final') _pollResearchOnce(s);
      _paintResearch();
    }); } catch (e) { console.debug('[Research] push subscribe failed:', e); }

    await _pollResearchOnce(s);
    _scheduleResearchPoll(s);
  } catch (e) {
    console.error('[Research] start failed:', e);
    if (_researchStream === s) {
      s.status = 'error';
      s.error = e && e.message ? e.message : String(e);
      _paintResearch();
    }
  }
}

/** One snapshot poll. Also the re-attach path after a refresh. */
async function _pollResearchOnce(s) {
  if (!s || !s.taskId) return;
  var snap = await Api.tasks.get(s.taskId);
  if (_researchStream !== s) return;
  if (!snap || snap.ok === false) return;
  _researchApplySnapshot(s, snap);
  _paintResearch();
  // A terminal snapshot must stop the poll loop here, not merely fail to
  // schedule the NEXT tick somewhere else: a job that finishes between ticks
  // would otherwise keep a timer alive for the life of the page.
  if (!_researchIsRunning(s)) {
    _stopResearchPoll();
    // Fill in anything the live snapshot did not carry (and prove the durable
    // path works while the user is still looking at it).
    if (!s.hydrated) _hydrateResearchFromStore(s);
  }
}

/** True while the job can still change state. */
function _researchIsRunning(s) {
  return !!s && (s.status === 'pending' || s.status === 'running');
}

function _stopResearchPoll() {
  if (_researchPollTimer) { clearTimeout(_researchPollTimer); _researchPollTimer = null; }
}

function _scheduleResearchPoll(s) {
  _stopResearchPoll();
  if (!_researchIsRunning(s)) return;
  _researchPollTimer = setTimeout(async function () {
    if (_researchStream !== s) return;
    try { await _pollResearchOnce(s); } catch (e) {
      console.debug('[Research] poll failed:', e);
    }
    if (_researchIsRunning(s)) _scheduleResearchPoll(s);
  }, 2000);
}

/** Stop the running job (the generic abort verb). */
async function _abortResearchJob() {
  var s = _researchStream;
  if (!s || !s.taskId) return;
  try { await Api.tasks.abort(s.taskId); } catch (e) {
    console.debug('[Research] abort failed:', e);
  }
  s.status = 'aborted';
  _stopResearchPoll();
  try { pushUnsubscribe('research', s.taskId); } catch (e) {
    console.debug('[Research] push unsubscribe failed:', e);
  }
  _paintResearch();
}

/** Open the library folder the harvested corpus landed in. */
function _openResearchFolder() {
  var s = _researchStream;
  if (!s || !s.folderId) return;
  if (typeof _showPaperLanding === 'function') _showPaperLanding();
  if (typeof _renderPaperLibrary === 'function') _renderPaperLibrary();
}

/**
 * Fold the DURABLE lookup payload into a stream state.
 *
 * Shared by the post-finish hydrate and the cold restore, so there is exactly
 * one place that maps the persisted shape onto what the renderer reads.
 */
function _researchApplyArtifacts(s, got) {
  if (!s || !got || got.found === false) return false;
  if (Array.isArray(got.accepted)) {
    s.acceptedIdeas = got.accepted;
    s.accepted = got.accepted.length;
  }
  if (Array.isArray(got.rejected)) {
    s.rejectedIdeas = got.rejected;
    s.rejected = got.rejected.length;
  }
  if (got.threshold != null) s.threshold = got.threshold;
  if (got.survey_md) s.surveyMd = got.survey_md;
  if (got.open_gaps) s.openGaps = got.open_gaps;
  if (got.gate_reached) s.gateReached = got.gate_reached;
  if (got.degraded) {
    s.degraded = true;
    s.degradedReason = got.degraded_reason || s.degradedReason;
  }
  s.hydrated = true;
  return true;
}

/**
 * Pull the persisted artifacts for the finished job and repaint.
 *
 * The live task snapshot disappears when the registry is TTL-swept or the
 * server restarts; the durable row does not. Calling this on finish means the
 * panel shows the same thing today and next week.
 */
async function _hydrateResearchFromStore(s) {
  if (!s || !s.direction) return;
  try {
    var got = await Api.research.lookup(s.direction, s.lang || 'en');
    if (_researchStream !== s) return;
    if (_researchApplyArtifacts(s, got)) _paintResearch();
  } catch (e) {
    console.debug('[Research] hydrate from store failed:', e);
  }
}

/**
 * COLD restore: rebuild the whole panel for a direction with NO live task.
 *
 * This is what makes a finished run re-openable after a refresh, a TTL sweep
 * or a server restart — it never touches the task registry, only the durable
 * store. Returns true when something was found and painted.
 */
async function _restoreResearchFromStore(direction, lang) {
  if (!direction) return false;
  var s = _newResearchStream(direction);
  s.lang = lang || 'en';
  s.status = 'done';
  _researchStream = s;
  try {
    var got = await Api.research.lookup(direction, s.lang);
    if (_researchStream !== s) return false;
    if (!_researchApplyArtifacts(s, got)) {
      // Honest empty: the direction has no artifacts (never researched, or
      // swept before the persistence layer existed).
      s.status = 'error';
      s.error = 'no stored research for this direction';
      _paintResearch();
      return false;
    }
  } catch (e) {
    console.error('[Research] restore failed:', e);
    if (_researchStream === s) {
      s.status = 'error';
      s.error = e && e.message ? e.message : String(e);
      _paintResearch();
    }
    return false;
  }
  _paintResearch();
  return true;
}

/** One labelled line inside an idea card; omitted entirely when empty so a
 *  sparse idea does not render a column of dangling labels. */
function _researchField(label, value) {
  if (!value) return '';
  return '<div class="pm-idea-field">' +
           '<span class="pm-idea-label">' + escapeHtml(label) + '</span>' +
           '<span class="pm-idea-value">' + escapeHtml(String(value)) + '</span>' +
         '</div>';
}

/** The four-axis rubric as chips. These ARE the calibration data for
 *  IDEATE_GATE_THRESHOLD, so they are shown, not summarised away. */
function _researchScoresHtml(scores, overall) {
  if (!scores || typeof scores !== 'object') return '';
  var chips = Object.keys(scores).map(function (axis) {
    return '<span class="pm-score-chip">' + escapeHtml(axis) + ' ' +
           escapeHtml(String(scores[axis])) + '</span>';
  }).join('');
  if (overall != null) {
    chips += '<span class="pm-score-chip is-overall">' +
             escapeHtml(String(overall)) + '</span>';
  }
  return '<div class="pm-idea-scores">' + chips + '</div>';
}

/** The ACCEPTED ideas — the actual product of the whole pipeline. */
function _researchIdeasHtml(s, _tt) {
  var ideas = s.acceptedIdeas || [];
  if (!ideas.length) {
    // An honest zero is a legitimate outcome (宁缺毋滥), not an error — say so
    // rather than rendering nothing and looking broken.
    return '<div class="pm-research-empty">' +
             escapeHtml(_tt('paper.research.noIdeas')) + '</div>';
  }
  var cards = ideas.map(function (idea) {
    return '<div class="pm-idea-card">' +
      '<div class="pm-idea-title">' +
        escapeHtml(idea.title || _tt('paper.research.untitled')) + '</div>' +
      _researchField(_tt('paper.research.mechanism'), idea.core_mechanism) +
      _researchField(_tt('paper.research.novelty'), idea.novelty_claim) +
      _researchField(_tt('paper.research.prediction'), idea.falsifiable_prediction) +
      _researchField(_tt('paper.research.whyNotAB'), idea.why_not_AB) +
      _researchScoresHtml(idea.scores, idea.overall) +
    '</div>';
  }).join('');
  return '<div class="pm-research-section">' +
           '<div class="pm-research-section-title">' +
             escapeHtml(_tt('paper.research.acceptedTitle')) + '</div>' +
           cards +
         '</div>';
}

/** The rejection audit — COLLAPSED by default with a one-line summary.
 *
 *  Owner ruling: an honest 0-accepted / 6-rejected run must be legible at a
 *  glance ("was the gate too strict, or were the ideas bad?") without a wall
 *  of failures dominating the panel. The summary answers exactly that by
 *  pairing the best rejected score against the threshold it had to clear. */
function _researchRejectedHtml(s, _tt) {
  var rej = s.rejectedIdeas || [];
  if (!rej.length) return '';
  var best = null;
  rej.forEach(function (r) {
    var v = Number(r.overall);
    if (isFinite(v) && (best === null || v > best)) best = v;
  });
  var summary = _tt('paper.research.rejectedSummary')
    .replace('{n}', String(rej.length))
    .replace('{best}', best === null ? '—' : String(best))
    .replace('{threshold}', s.threshold == null ? '—' : String(s.threshold));
  var rows = rej.map(function (r) {
    return '<div class="pm-idea-card is-rejected">' +
      '<div class="pm-idea-title">' +
        escapeHtml(r.title || _tt('paper.research.untitled')) + '</div>' +
      _researchField(_tt('paper.research.rejectReason'), r.reject_reason) +
      _researchField(_tt('paper.research.rejectStage'), r.reject_stage) +
      _researchScoresHtml(r.scores, r.overall) +
    '</div>';
  }).join('');
  return '<details class="pm-research-rejected">' +
           '<summary>' + escapeHtml(summary) + '</summary>' +
           rows +
         '</details>';
}

/** The survey markdown + the open-gap map that R3 reasoned against. */
function _researchSurveyHtml(s, _tt) {
  var out = '';
  if (s.surveyMd) {
    var md = (typeof renderMarkdown === 'function')
      ? renderMarkdown(s.surveyMd)
      : '<pre>' + escapeHtml(s.surveyMd) + '</pre>';
    out += '<details class="pm-research-survey">' +
             '<summary>' + escapeHtml(_tt('paper.research.surveyTitle')) +
             '</summary>' +
             '<div class="pm-research-md">' + md + '</div>' +
           '</details>';
  }
  var gaps = (s.openGaps && s.openGaps.open_gaps) || [];
  if (gaps.length) {
    var items = gaps.map(function (g) {
      return '<li class="pm-gap-item">' +
        '<span class="pm-gap-text">' + escapeHtml(g.gap || '') + '</span>' +
        (g.why_open
          ? '<span class="pm-gap-why">' + escapeHtml(g.why_open) + '</span>'
          : '') +
      '</li>';
    }).join('');
    out += '<details class="pm-research-gaps">' +
             '<summary>' + escapeHtml(_tt('paper.research.gapsTitle')) +
             '</summary>' +
             '<ul class="pm-gap-list">' + items + '</ul>' +
           '</details>';
  }
  return out;
}

/**
 * Paint the "recent research" list onto the landing screen.
 *
 * ★ WHY THIS EXISTS. A persisted run is addressed by
 * ``sha256(normalised direction)`` — a ONE-WAY hash. Without an index a user
 * who no longer remembers their exact original wording can never reach their
 * own artifacts again, which is user-indistinguishable from the data having
 * been deleted. The original text is recovered from the stored metadata, so
 * this list is what makes past work reachable at all.
 *
 * Renders nothing when there is no past research — the landing screen must
 * not grow an empty box.
 */
async function _renderRecentResearch() {
  var host = document.getElementById('paperRecentResearch');
  if (!host) return;
  var _tt = (typeof t === 'function') ? t : function (k) { return k; };
  var data = null;
  try {
    data = await Api.research.list(20);
  } catch (e) {
    console.debug('[Research] recent list failed:', e);
    return;
  }
  var items = (data && data.items) || [];
  if (!items.length) { host.innerHTML = ''; return; }

  var rows = items.map(function (it) {
    var counts = _tt('paper.research.recentCounts')
      .replace('{accepted}', String(it.accepted || 0))
      .replace('{rejected}', String(it.rejected || 0));
    return '<button class="pm-recent-item" onclick="_restoreResearchFromStore(' +
             JSON.stringify(it.direction).replace(/"/g, '&quot;') + ',' +
             JSON.stringify(it.lang || 'en').replace(/"/g, '&quot;') + ')">' +
      '<span class="pm-recent-dir">' + escapeHtml(it.direction) + '</span>' +
      '<span class="pm-recent-meta">' + escapeHtml(counts) +
        (it.degraded
          ? ' · ' + escapeHtml(_tt('paper.research.degraded'))
          : '') +
      '</span>' +
    '</button>';
  }).join('');

  host.innerHTML =
    '<div class="pm-recent-head">' +
      '<span class="pm-recent-title">' +
        escapeHtml(_tt('paper.research.recentTitle')) + '</span>' +
      '<span class="pm-recent-hint">' +
        escapeHtml(_tt('paper.research.recentHint')) + '</span>' +
    '</div>' +
    '<div class="pm-recent-list">' + rows + '</div>';
}

/** Render the research console from `_researchStream`. */
function _paintResearch() {
  var s = _researchStream;
  if (!s) return;
  var viewer = document.getElementById('paperPdfViewer');
  if (!viewer) return;
  var _tt = (typeof t === 'function') ? t : function (k) { return k; };
  var running = (s.status === 'pending' || s.status === 'running');

  var steps = _RESEARCH_PHASES.map(function (p) {
    var done = _RESEARCH_PHASES.indexOf(p) < _RESEARCH_PHASES.indexOf(s.phase);
    var active = (p === s.phase);
    return '<div class="paper-step' + (active ? ' is-active' : '') +
           (done ? ' is-done' : '') + '">' + escapeHtml(_tt('paper.research.' + p)) +
           '</div>';
  }).join('');

  // The quality banner. Shown for a degraded job REGARDLESS of status='done'.
  var quality = '';
  if (s.degraded) {
    quality =
      '<div class="pm-quality is-degraded" role="alert">' +
        '<div class="pm-quality-title">' +
          escapeHtml(_tt('paper.research.degraded')) + '</div>' +
        '<div class="pm-quality-reason">' + escapeHtml(s.degradedReason) + '</div>' +
      '</div>';
  }

  var body;
  if (running) {
    body =
      '<div class="pm-console-head">' +
        '<div class="pm-console-title">' + escapeHtml(_tt('paper.research.running')) + '</div>' +
        '<button class="pm-console-abort" onclick="_abortResearchJob()">' +
          escapeHtml(_tt('paper.research.abort')) + '</button>' +
      '</div>' +
      '<div class="paper-stepper">' + steps + '</div>' +
      '<div class="paper-media-activity" data-research-elapsed data-started-at="' +
        String(s.startedAt || 0) + '"></div>';
  } else {
    body =
      '<div class="pm-console-head">' +
        '<div class="pm-console-title">' +
          escapeHtml(_tt('paper.research.finished')) + '</div>' +
      '</div>' + quality +
      '<div class="pm-research-tally">' +
        '<span>' + escapeHtml(String(s.accepted)) + ' accepted</span>' +
        '<span>' + escapeHtml(String(s.rejected)) + ' rejected</span>' +
        '<span>' + escapeHtml(String(s.corpusSize)) + ' papers</span>' +
      '</div>' +
      _researchIdeasHtml(s, _tt) +
      _researchRejectedHtml(s, _tt) +
      _researchSurveyHtml(s, _tt) +
      (s.folderId
        ? '<button class="paper-retry-btn" onclick="_openResearchFolder()">' +
            escapeHtml(_tt('paper.research.openFolder')) + '</button>'
        : '');
  }

  viewer.innerHTML =
    '<div class="pm-console" data-research-shell="1">' +
      '<div class="pm-studio-head">' +
        '<div class="pm-studio-head-text">' +
          '<div class="pm-studio-title">' + escapeHtml(s.direction) + '</div>' +
          '<div class="pm-studio-sub">' +
            escapeHtml(_tt('paper.research.subtitle')) + '</div>' +
        '</div>' +
      '</div>' + body +
    '</div>';
}
