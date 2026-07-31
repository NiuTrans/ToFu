/* ═══════════════════════════════════════════════════════════════════
   project-brain-attention.js — the "Needs you" tab.

   The Project Brain has five data surfaces; only a FEW rows across them are
   actually waiting ON the human. Before this tab, answering "is anything
   waiting on me?" meant visiting four tabs, and the always-visible collab bar
   led with the LEAST urgent of them (charter proposals — which block nothing,
   since agents self-commit decisions).

   This tab renders the backend's attention SSOT verbatim:

     Api.project.brainAttention(path, convId)
       → {items:[...], blocking, advisory, needsYou, waiting}

   `items` arrives PRIORITY-ORDERED (blocking first). This module does NOT
   re-sort, re-filter or re-classify — the whole point of the SSOT is that the
   count on the bar and the cards in the panel are the same judgment. It is a
   pure renderer of the server's verdict (the same invariant the influence lens
   holds).

   Two severities only (docs/PROJECT_BRAIN_ATTENTION_REDESIGN.md §D2):
     • blocking — work is STOPPED, only a human restarts it.
     • advisory — progress continues; a human may improve the outcome.

   Resolving controls are the SAME calls the owning tabs make
   (Api.project.boardAnswer / commitCharter / dismissProposal), so there is one
   backend contract per action, not a second implementation that can drift.
   Items that cannot be resolved in two clicks (a file conflict) render a
   deep-link INTO the owning tab instead of a copy of it.

   Bundled by lib/js_bundler.py (_DEFERRED_FILES, after project-brain.js —
   reads window.ProjectBrain._state at runtime). Strings live under
   projectBrain.* in i18n.js. Icons via Icon() (§3.4 — no emoji).
   ═══════════════════════════════════════════════════════════════════ */

(function () {
  'use strict';

  function _t(key, fallback) {
    try { return (typeof t === 'function') ? t(key) : fallback; }
    catch (_e) { return fallback; }
  }

  function _esc(s) {
    if (typeof escapeHtml === 'function') return escapeHtml(String(s == null ? '' : s));
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function _bodyEl() { return document.getElementById('projectBrainAttentionBody'); }

  /**
   * Report a FAILED resolving action to the operator.
   *
   * Delegates to project-brain.js's `_reportFailure` so the panel has ONE
   * failure surface rather than two that can drift — the same reason this
   * module borrows its clamp/markdown/tab-switch primitives. The local
   * fallback below only runs if project-brain.js is somehow absent (this
   * module is bundled after it) and must still SAY something: these actions
   * previously only console.warn'd, so a refused mutation (e.g. the commit
   * route's 400 when `summary` is missing) was indistinguishable from a dead
   * button.
   */
  function _reportFailure(key, fallback, err) {
    try {
      if (window.ProjectBrain &&
          typeof window.ProjectBrain._reportFailure === 'function') {
        window.ProjectBrain._reportFailure(key, fallback, err);
        return;
      }
    } catch (_e) { /* fall through to the local surface */ }
    var detail = (err && err.message) ? String(err.message) : '';
    if (typeof console !== 'undefined') {
      console.warn('[Attention] %s: %s', fallback, detail || err);
    }
    if (typeof showToast === 'function') {
      showToast(_t(key, fallback) + (detail ? ' — ' + detail : ''), 'error');
    }
  }

  /** Displayed project path — same accessor the sibling modules use. */
  function _path() {
    try {
      if (typeof window.ProjectBrain !== 'undefined' &&
          window.ProjectBrain._state && window.ProjectBrain._state.path) {
        return window.ProjectBrain._state.path;
      }
    } catch (_e) { /* fall through */ }
    return '';
  }

  /** The operator's acting conversation (their proxy for a mutation). */
  function _convId() {
    try {
      return (typeof activeConvId !== 'undefined' && activeConvId) ? activeConvId : '';
    } catch (_e) { return ''; }
  }

  /** Delegate to project-brain.js's markdown-lite + clamp so a long question
   *  or proposal collapses the same way it does in its own tab (one clamp
   *  grammar across the panel, not two). Falls back to escaped text. */
  function _rich(text) {
    try {
      if (window.ProjectBrain && typeof window.ProjectBrain._clampBlock === 'function' &&
          typeof window.ProjectBrain._mdLite === 'function') {
        return window.ProjectBrain._clampBlock(
          window.ProjectBrain._mdLite(text), String(text || ''));
      }
    } catch (_e) { /* fall through */ }
    return _esc(text);
  }

  /**
   * One attention card. Every item type renders the SAME primitive —
   * severity rail · title · body · meta · actions — so the eye learns one
   * shape instead of four (§3.3.1). Only the accent and the action row differ.
   */
  function _card(item, inner) {
    return '<div class="pb-attn-card pb-attn-' + _esc(item.severity) +
      ' pb-attn-type-' + _esc(item.type) + '" data-attn-id="' + _esc(item.id) +
      '" data-attn-type="' + _esc(item.type) + '">' + inner + '</div>';
  }

  /** Severity pill — the ONE place the two-severity vocabulary is spelled. */
  function _sevPill(severity) {
    var isBlocking = severity === 'blocking';
    var label = isBlocking
      ? _t('projectBrain.attnBlocking', 'Stopped')
      : _t('projectBrain.attnAdvisory', 'Advisory');
    var glyph = isBlocking ? 'alertTriangle' : 'messageSquare';
    return '<span class="pb-attn-sev pb-attn-sev-' + _esc(severity) + '">' +
      ((typeof Icon === 'function') ? Icon(glyph, 11) : '') +
      '<span>' + _esc(label) + '</span></span>';
  }

  /** A "go to the tab that owns this" link (used when the item is not
   *  resolvable inline — we deep-link rather than duplicate the surface). */
  function _openTabBtn(tab, labelKey, fallback) {
    return '<button type="button" class="pb-attn-goto" data-goto-tab="' + _esc(tab) + '">' +
      ((typeof Icon === 'function') ? Icon('arrowRight', 12) : '') +
      '<span>' + _esc(_t(labelKey, fallback)) + '</span></button>';
  }

  /**
   * A halted epic. Renders the question as the primary content with the same
   * one-click option chips + free-text input the Board's awaiting lane has,
   * because answering IS the resolution — making the operator hop to another
   * tab to type one word is exactly the scatter this tab removes.
   */
  function _questionCard(item) {
    var opts = Array.isArray(item.options) ? item.options : [];
    var chips = opts.map(function (o, i) {
      var label = (o && o.label) ? String(o.label) : '';
      if (!label) return '';
      var desc = (o && o.description) ? String(o.description) : '';
      return '<button type="button" class="pb-chip pb-attn-act" data-act="answerOpt"' +
        ' data-idx="' + i + '"' + (desc ? ' title="' + _esc(desc) + '"' : '') +
        ' data-pb-src="' + _esc(label) + '">' + _esc(label) + '</button>';
    }).join('');
    var meta = [];
    if (item.blockCount) {
      meta.push(_t('projectBrain.blockedCount', 'blocked %d×')
        .replace('%d', item.blockCount));
    }
    if (item.reason) meta.push(item.reason);
    return _card(item,
      '<div class="pb-attn-head">' + _sevPill(item.severity) +
        '<span class="pb-attn-kind">' +
        _esc(_t('projectBrain.attnKindEpic', 'Epic halted')) + '</span></div>' +
      '<div class="pb-attn-title">' + _rich(item.title) + '</div>' +
      '<div class="pb-attn-q" data-pb-src="' + _esc(item.question) + '">' +
        _esc(item.question) + '</div>' +
      (chips ? '<div class="pb-chip-row">' + chips + '</div>' : '') +
      '<div class="pb-attn-input-row">' +
        '<input type="text" class="pb-attn-answer" placeholder="' +
        _esc(_t('projectBrain.answerPlaceholder',
                'Type your answer (or pick an option above)…')) + '">' +
        '<button type="button" class="pb-attn-act pb-btn-primary" data-act="answerSubmit">' +
        ((typeof Icon === 'function') ? Icon('check', 12) : '') +
        '<span>' + _esc(_t('projectBrain.answerSubmit', 'Submit answer')) +
        '</span></button>' +
      '</div>' +
      (meta.length ? '<div class="pb-attn-meta">' + _esc(meta.join(' · ')) + '</div>' : ''));
  }

  /** A pending charter proposal — commit / reject inline (same routes the
   *  Charter tab calls).
   *
   *  The commit route REQUIRES a one-line `summary` (the binding rule the
   *  per-turn injection renders) and 400s without it, so the card must offer
   *  the same editable input the Charter tab does — pre-filled with the
   *  proposal's first line. A bare Commit button here submitted no summary and
   *  was rejected on every click. */
  function _proposalCard(item) {
    var firstLine = (String(item.text || '').split('\n', 1)[0] || '').trim();
    if (firstLine.length > 200) firstLine = firstLine.slice(0, 200).trim();
    return _card(item,
      '<div class="pb-attn-head">' + _sevPill(item.severity) +
        '<span class="pb-attn-kind">' +
        _esc(_t('projectBrain.attnKindProposal', 'Proposed decision')) + '</span></div>' +
      '<div class="pb-attn-body">' + _rich(item.text) + '</div>' +
      '<div class="pb-proposal-summary-row">' +
        '<input type="text" class="pb-proposal-summary" maxlength="240" placeholder="' +
        _esc(_t('projectBrain.summaryPlaceholder', 'One-line summary (required)')) +
        '" value="' + _esc(firstLine) + '">' +
      '</div>' +
      '<div class="pb-attn-actions">' +
        '<button type="button" class="pb-attn-act pb-btn-primary" data-act="commit"' +
        (firstLine ? '' : ' disabled') + '>' +
        _esc(_t('projectBrain.commit', 'Commit')) + '</button>' +
        '<button type="button" class="pb-attn-act" data-act="reject">' +
        _esc(_t('projectBrain.reject', 'Reject')) + '</button>' +
      '</div>');
  }

  /** A live file-overlap. Not resolvable by a button — the operator decides
   *  whether to intervene — so this deep-links into Team rather than cloning
   *  the peer controls. */
  function _conflictCard(item) {
    return _card(item,
      '<div class="pb-attn-head">' + _sevPill(item.severity) +
        '<span class="pb-attn-kind">' +
        _esc(_t('projectBrain.attnKindConflict', 'File conflict')) + '</span></div>' +
      '<div class="pb-attn-body">' + _esc(item.text) + '</div>' +
      '<div class="pb-attn-actions">' +
        _openTabBtn('peers', 'projectBrain.attnOpenTeam', 'Open Team') +
      '</div>');
  }

  var _RENDERERS = {
    board_question: _questionCard,
    charter_proposal: _proposalCard,
    conflict: _conflictCard,
  };

  /**
   * The empty state is a deliberate POSITIVE statement, not a dashed "no data"
   * box: the operator's question was "is anything waiting on me?" and the
   * answer "no — and here's what is moving on its own" is genuinely useful.
   */
  function _emptyState(res) {
    var waiting = Number(res.waiting) || 0;
    var sub = waiting > 0
      ? _t('projectBrain.attnEmptyWaiting', '{n} waiting on their own gates — no action needed')
        .replace('{n}', waiting)
      : _t('projectBrain.attnEmptySub', 'Every workstream is moving on its own.');
    return '<div class="pb-attn-empty">' +
      ((typeof Icon === 'function') ? Icon('check', 22) : '') +
      '<div class="pb-attn-empty-title">' +
      _esc(_t('projectBrain.attnEmpty', 'Nothing needs you')) + '</div>' +
      '<div class="pb-attn-empty-sub">' + _esc(sub) + '</div></div>';
  }

  /**
   * Render the tab from the backend verdict. Pure renderer — `res` is the
   * brainAttention payload; the order of `res.items` is the server's and is
   * preserved exactly.
   */
  function renderAttention(res) {
    var el = _bodyEl();
    if (!el) return;
    res = res || {};
    var items = res.items || [];
    // Tab badge = everything awaiting the human. Set here so it can never
    // drift from the rendered cards.
    _setBadge(Number(res.needsYou) || items.length || 0,
              Number(res.blocking) || 0);

    if (!items.length) {
      el.innerHTML = _emptyState(res);
      return;
    }

    var html = '';
    // A lead line naming what is stopped vs. merely suggested, so the operator
    // knows the shape of the list before reading the cards.
    var blocking = Number(res.blocking) || 0;
    var advisory = Number(res.advisory) || 0;
    var bits = [];
    if (blocking) {
      bits.push(_t('projectBrain.attnLeadBlocking', '{n} stopped')
        .replace('{n}', blocking));
    }
    if (advisory) {
      bits.push(_t('projectBrain.attnLeadAdvisory', '{n} advisory')
        .replace('{n}', advisory));
    }
    html += '<div class="pb-attn-lead' + (blocking ? ' pb-attn-lead-blocking' : '') + '">' +
      _esc(bits.join(' · ')) + '</div>';

    for (var i = 0; i < items.length; i++) {
      var it = items[i] || {};
      var fn = _RENDERERS[it.type];
      if (fn) html += fn(it);
    }
    var waiting = Number(res.waiting) || 0;
    if (waiting > 0) {
      // The reassurance line: these need NOTHING from the human, so they are
      // deliberately not cards (§D3) — surfacing them as tasks would train the
      // operator to ignore the surface.
      html += '<div class="pb-attn-waiting">' +
        ((typeof Icon === 'function') ? Icon('clock', 12) : '') +
        '<span>' + _esc(_t('projectBrain.attnWaiting',
          '{n} waiting on their own gates — no action needed')
          .replace('{n}', waiting)) + '</span></div>';
    }
    el.innerHTML = html;

    if (window.ProjectBrain && typeof window.ProjectBrain._wireClampToggles === 'function') {
      try { window.ProjectBrain._wireClampToggles(el); } catch (_e) { /* best-effort */ }
    }
    if (typeof ProjectBrainI18n !== 'undefined' && ProjectBrainI18n &&
        typeof ProjectBrainI18n.apply === 'function') {
      try { ProjectBrainI18n.apply(el); } catch (_e) { /* best-effort */ }
    }
    _wireActions(el);
  }

  /** Tab badge. `blocking` drives the alarm class so an advisory-only project
   *  reads calm — the same rule the collab bar uses. */
  function _setBadge(n, blocking) {
    var el = document.getElementById('pbTabCountAttention');
    if (!el) return;
    if (n > 0) {
      el.textContent = n > 99 ? '99+' : String(n);
      el.hidden = false;
      el.classList.toggle('pb-tab-count-blocking', blocking > 0);
    } else {
      el.textContent = '';
      el.hidden = true;
      el.classList.remove('pb-tab-count-blocking');
    }
  }

  /** Resolve an option chip's label from the rendered card (the labels live
   *  in the DOM the server's payload produced — no second lookup table). */
  function _optLabel(card, idx) {
    var chip = card.querySelector('.pb-attn-act[data-act="answerOpt"][data-idx="' + idx + '"]');
    return chip ? (chip.getAttribute('data-pb-src') || chip.textContent || '') : '';
  }

  function _wireActions(el) {
    // Deep-links into an owning tab.
    var gotos = el.querySelectorAll('.pb-attn-goto');
    for (var g = 0; g < gotos.length; g++) {
      gotos[g].addEventListener('click', function (ev) {
        var tab = ev.currentTarget.getAttribute('data-goto-tab');
        if (tab && window.ProjectBrain &&
            typeof window.ProjectBrain._selectTab === 'function') {
          window.ProjectBrain._selectTab(tab);
        }
      });
    }
    // Enter in an answer input submits (mirrors the Board lane).
    var inputs = el.querySelectorAll('.pb-attn-answer');
    for (var n = 0; n < inputs.length; n++) {
      inputs[n].addEventListener('keydown', function (ev) {
        if (ev.key !== 'Enter') return;
        ev.preventDefault();
        var card = ev.currentTarget.closest ? ev.currentTarget.closest('.pb-attn-card') : null;
        var submit = card ? card.querySelector('.pb-attn-act[data-act="answerSubmit"]') : null;
        if (submit) submit.click();
      });
    }
    // The commit route REQUIRES a one-line summary — keep Commit unclickable
    // while it is empty rather than letting the click 400.
    var sums = el.querySelectorAll('.pb-proposal-summary');
    for (var s = 0; s < sums.length; s++) {
      sums[s].addEventListener('input', function (ev) {
        var card = ev.currentTarget.closest
          ? ev.currentTarget.closest('.pb-attn-card') : null;
        var commit = card
          ? card.querySelector('.pb-attn-act[data-act="commit"]') : null;
        if (commit) commit.disabled = !((ev.currentTarget.value || '').trim());
      });
    }
    var acts = el.querySelectorAll('.pb-attn-act');
    for (var a = 0; a < acts.length; a++) {
      acts[a].addEventListener('click', function (ev) {
        var btn = ev.currentTarget;
        var card = btn.closest ? btn.closest('.pb-attn-card') : null;
        if (!card) return;
        _dispatch(btn.getAttribute('data-act'), card, btn);
      });
    }
  }

  /**
   * Perform a resolving action, then refresh. Every branch calls the SAME
   * Api.project route the owning tab calls — one backend contract per action.
   * On success we refresh the whole brain (the item leaves this tab AND its
   * home tab), never a local DOM removal, so the surface stays
   * backend-authoritative.
   */
  function _dispatch(act, card, btn) {
    var api = (typeof Api !== 'undefined' && Api.project) ? Api.project : null;
    var path = _path();
    if (!api || !path || !act) return;
    var id = card.getAttribute('data-attn-id') || '';
    var convId = _convId();

    if (act === 'answerOpt') {
      var label = _optLabel(card, btn.getAttribute('data-idx'));
      if (label) _submitAnswer(api, path, id, convId, label, btn);
      return;
    }
    if (act === 'answerSubmit') {
      var input = card.querySelector('.pb-attn-answer');
      var text = input ? (input.value || '').trim() : '';
      if (text) _submitAnswer(api, path, id, convId, text, btn);
      return;
    }
    if (act === 'commit') {
      var body = card.querySelector('.pb-attn-body [data-pb-src]') ||
                 card.querySelector('.pb-attn-body');
      // The ORIGINAL text (data-pb-src), never the translation overlay — an
      // active translated VIEW must never leak into a committed decision.
      var txt = body
        ? (body.getAttribute('data-pb-src') != null
            ? body.getAttribute('data-pb-src') : (body.textContent || ''))
        : '';
      if (!txt) return;
      var sumEl = card.querySelector('.pb-proposal-summary');
      var summary = sumEl ? (sumEl.value || '').trim() : '';
      // The route REQUIRES it; committing without one is a guaranteed 400.
      if (!summary) { if (sumEl) sumEl.focus(); return; }
      btn.disabled = true;
      Promise.resolve(api.commitCharter(path, {
        add_decision: txt, summary: summary, resolves_proposal: id,
      })).then(_refreshAll).catch(function (e) {
        _reportFailure('projectBrain.commitFailed', 'Commit failed', e);
        btn.disabled = false;
      });
      return;
    }
    if (act === 'reject') {
      if (typeof api.dismissProposal !== 'function') return;
      btn.disabled = true;
      Promise.resolve(api.dismissProposal(path, id))
        .then(_refreshAll)
        .catch(function (e) {
          _reportFailure('projectBrain.rejectFailed', 'Reject failed', e);
          btn.disabled = false;
        });
    }
  }

  function _submitAnswer(api, path, taskId, convId, answer, btn) {
    if (typeof api.boardAnswer !== 'function') return;
    btn.disabled = true;
    Promise.resolve(api.boardAnswer(path, taskId, convId, answer))
      .then(_refreshAll)
      .catch(function (e) {
        _reportFailure('projectBrain.answerFailed', 'Submitting the answer failed', e);
        btn.disabled = false;
      });
  }

  /** Re-pull every surface: a resolved item must vanish from BOTH this tab and
   *  its home tab in one go (they render the same backend state). */
  function _refreshAll() {
    var path = _path();
    if (!path) return;
    refreshAttention(path);
    try {
      if (window.ProjectBrain) {
        if (typeof window.ProjectBrain.refreshBoard === 'function') {
          window.ProjectBrain.refreshBoard(path);
        }
        if (typeof window.ProjectBrain.refreshCharter === 'function') {
          window.ProjectBrain.refreshCharter(path);
        }
        if (typeof window.ProjectBrain.refreshInfluence === 'function') {
          window.ProjectBrain.refreshInfluence(path);
        }
      }
    } catch (_e) { /* best-effort */ }
  }

  /** Fetch + render for `path`. Best-effort; a failed fetch leaves the last
   *  render in place rather than blanking the surface. */
  function refreshAttention(path) {
    var api = (typeof Api !== 'undefined' && Api.project) ? Api.project : null;
    if (!api || !path || typeof api.brainAttention !== 'function') return;
    Promise.resolve(api.brainAttention(path, _convId())).then(function (res) {
      // Guard against a project switch mid-flight.
      if (path !== _path()) return;
      renderAttention(res || {});
    }).catch(function (e) {
      _reportFailure('projectBrain.loadFailed',
                     'Loading the project brain failed', e);
    });
  }

  window.ProjectBrainAttention = {
    renderAttention: renderAttention,
    refreshAttention: refreshAttention,
    _card: _card,
    _sevPill: _sevPill,
    _setBadge: _setBadge,
  };
})();
