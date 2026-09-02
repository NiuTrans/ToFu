/* ===== migrated source: project-brain-attention.js ===== */
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

   Two severities only (docs/modules/conversations_project_brain.md §D2):
     • blocking — work is STOPPED, only a human restarts it.
     • advisory — progress continues; a human may improve the outcome.

   Resolving controls are the SAME calls the owning tabs make
   (Api.project.boardAnswer / commitCharter / dismissProposal), so there is one
   backend contract per action, not a second implementation that can drift.

   Bundled by Vite's module graph (the migrated lazy-module graph, after project-brain.js —
   reads runtimeScope.ProjectBrain._state at runtime). Strings live under
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
      if (runtimeScope.ProjectBrain &&
          typeof runtimeScope.ProjectBrain._reportFailure === 'function') {
        runtimeScope.ProjectBrain._reportFailure(key, fallback, err);
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
      if (typeof runtimeScope.ProjectBrain !== 'undefined' &&
          runtimeScope.ProjectBrain._state && runtimeScope.ProjectBrain._state.path) {
        return runtimeScope.ProjectBrain._state.path;
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
      if (runtimeScope.ProjectBrain && typeof runtimeScope.ProjectBrain._clampBlock === 'function' &&
          typeof runtimeScope.ProjectBrain._mdLite === 'function') {
        return runtimeScope.ProjectBrain._clampBlock(
          runtimeScope.ProjectBrain._mdLite(text), String(text || ''));
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

  /**
   * The provenance chip — "which conversation raised this?". ONE builder
   * shared by every decision card (halted epic, charter proposal): two
   * hand-copies would drift, and the chip is meaningless if only SOME
   * decisions carry it. The backend resolves the title (askedByTitle); the
   * chip deep-links INTO that chat via the 'openConv' action.
   */
  function _fromChip(item) {
    var fromId = item.askedByConvId || item.convId || '';
    if (!fromId) return '';
    var fromName = item.askedByTitle || fromId;
    return '<button type="button" class="pb-conv-chip pb-attn-from pb-attn-act"' +
      ' data-act="openConv" data-conv-id="' + _esc(fromId) + '" title="' +
      _esc(_t('projectBrain.attnOpenConv',
              'Open the conversation that raised this')) + '">' +
      ((typeof Icon === 'function') ? Icon('messageSquare', 11) : '') +
      '<span class="pb-attn-from-name">' +
      _esc(_t('projectBrain.attnFrom', 'from')) + ' ' + _esc(fromName) +
      '</span></button>';
  }

  /**
   * A halted epic. Renders the question as the primary content with the same
   * one-click option chips + free-text input the Board's awaiting lane has,
   * because answering IS the resolution — making the operator hop to another
   * tab to type one word is exactly the scatter this tab removes.
   */
  function _questionCard(item) {
    var opts = Array.isArray(item.options) ? item.options : [];
    // An option's description is the consequence the operator is choosing
    // BETWEEN — it must be READABLE ON THE CARD, not hidden behind a hover
    // tooltip (the 2026-08 owner complaint: the card asked for a decision
    // without showing what each choice meant).
    var chips = opts.map(function (o, i) {
      var label = (o && o.label) ? String(o.label) : '';
      if (!label) return '';
      var desc = (o && o.description) ? String(o.description) : '';
      return '<button type="button" class="pb-chip pb-attn-act pb-attn-opt" data-act="answerOpt"' +
        ' data-idx="' + i + '" data-pb-src="' + _esc(label) + '">' +
        '<span class="pb-attn-opt-label">' + _esc(label) + '</span>' +
        (desc ? '<span class="pb-attn-opt-desc">' + _esc(desc) + '</span>' : '') +
        '</button>';
    }).join('');
    var meta = [];
    if (item.blockCount) {
      meta.push(_t('projectBrain.blockedCount', 'blocked %d×')
        .replace('%d', item.blockCount));
    }
    // When it stopped — the same relative-time grammar the feed uses.
    var rel = '';
    try {
      if (runtimeScope.ProjectBrain &&
          typeof runtimeScope.ProjectBrain._relTime === 'function') {
        rel = runtimeScope.ProjectBrain._relTime(item.ts);
      }
    } catch (_e) { rel = ''; }
    if (rel) meta.push(rel);
    // The reason is the card's BACKGROUND section ("why did this stop?") —
    // promoted out of the one-line meta tail into its own labeled,
    // clamp-rendered block so the operator gets the context the decision
    // needs instead of a dense escaped footnote.
    var reasonHtml = item.reason
      ? '<div class="pb-attn-label">' +
        _esc(_t('projectBrain.attnWhyStopped', 'Why it stopped')) + '</div>' +
        '<div class="pb-attn-reason">' + _rich(item.reason) + '</div>'
      : '';
    // "Create conversation" — open a fresh chat about this epic (delegates to
    // project-brain.js's launcher). Rendered ONLY when that shared launcher
    // is actually loaded: a button whose handler is absent is a dead button.
    var convBtn = (runtimeScope.ProjectBrain &&
        typeof runtimeScope.ProjectBrain._openEpicConversation === 'function')
      ? '<button type="button" class="pb-attn-act pb-attn-act-conv"' +
        ' data-act="createConv" title="' +
        _esc(_t('projectBrain.actCreateConv', 'New chat')) + '">' +
        ((typeof Icon === 'function') ? Icon('messagePlus', 12) : '') +
        '<span>' + _esc(_t('projectBrain.actCreateConv', 'New chat')) +
        '</span></button>'
      : '';
    return _card(item,
      '<div class="pb-attn-head">' + _sevPill(item.severity) +
        '<span class="pb-attn-kind">' +
        _esc(_t('projectBrain.attnKindEpic', 'Epic halted')) + '</span>' +
        _fromChip(item) + '</div>' +
      '<div class="pb-attn-title">' + _rich(item.title) + '</div>' +
      reasonHtml +
      '<div class="pb-attn-label">' +
        _esc(_t('projectBrain.attnYourCall', 'Your call')) + '</div>' +
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
      (meta.length ? '<div class="pb-attn-meta">' + _esc(meta.join(' · ')) + '</div>' : '') +
      (convBtn ? '<div class="pb-attn-actions">' + convBtn + '</div>' : ''));
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
        _esc(_t('projectBrain.attnKindProposal', 'Proposed decision')) + '</span>' +
        _fromChip(item) + '</div>' +
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

  var _RENDERERS = {
    board_question: _questionCard,
    charter_proposal: _proposalCard,
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

  // ── Pending-focus channel (Board deep-link) ─────────────────────
  // The Board's compact awaiting card deep-links HERE with a specific epic
  // id. This tab's data loads ASYNC (refreshAttention → brainAttention →
  // renderAttention), so the focus request must survive until the card
  // exists: focusItem stores the id, _applyFocus honors it now if the card
  // is already rendered, and renderAttention retries it after every render.
  var _pendingFocusId = '';

  /** Scroll + flash the card for `id` (Board "go answer" deep-link). */
  function focusItem(id) {
    _pendingFocusId = String(id || '');
    _applyFocus();
  }

  function _applyFocus() {
    if (!_pendingFocusId) return;
    var el = _bodyEl();
    if (!el) return;
    var card = el.querySelector('.pb-attn-card[data-attn-id="' +
      _pendingFocusId.replace(/"/g, '') + '"]');
    if (!card) return;   // not rendered yet — renderAttention will retry
    _pendingFocusId = '';
    try { card.scrollIntoView({ block: 'center', behavior: 'smooth' }); }
    catch (_e) { /* jsdom / older browsers: best-effort */ }
    card.classList.remove('pb-attn-flash');
    void card.offsetWidth;   // reflow so re-adding re-triggers the animation
    card.classList.add('pb-attn-flash');
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

    if (runtimeScope.ProjectBrain && typeof runtimeScope.ProjectBrain._wireClampToggles === 'function') {
      try { runtimeScope.ProjectBrain._wireClampToggles(el); } catch (_e) { /* best-effort */ }
    }
    if (typeof ProjectBrainI18n !== 'undefined' && ProjectBrainI18n &&
        typeof ProjectBrainI18n.apply === 'function') {
      try { ProjectBrainI18n.apply(el); } catch (_e) { /* best-effort */ }
    }
    _wireActions(el);
    // A Board deep-link may have asked for a specific card BEFORE this render
    // resolved — honor it now that the card exists.
    _applyFocus();
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
    if (act === 'openConv') {
      // The provenance chip — jump into the conversation that raised the
      // question so the operator can read the full backstory there.
      var cid = btn.getAttribute('data-conv-id') || '';
      if (cid && typeof loadConversation === 'function') loadConversation(cid);
      return;
    }
    if (act === 'createConv') {
      var launcher = runtimeScope.ProjectBrain &&
        runtimeScope.ProjectBrain._openEpicConversation;
      if (typeof launcher !== 'function') return;
      // The ORIGINAL title (data-pb-src), never the translation overlay — the
      // same source-of-truth rule the commit path holds.
      var titleEl = card.querySelector('.pb-attn-title [data-pb-src]') ||
                    card.querySelector('.pb-attn-title');
      var ttl = titleEl
        ? (titleEl.getAttribute('data-pb-src') != null
            ? titleEl.getAttribute('data-pb-src') : (titleEl.textContent || ''))
        : '';
      launcher(id, ttl);
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
      if (runtimeScope.ProjectBrain) {
        if (typeof runtimeScope.ProjectBrain.refreshBoard === 'function') {
          runtimeScope.ProjectBrain.refreshBoard(path);
        }
        if (typeof runtimeScope.ProjectBrain.refreshCharter === 'function') {
          runtimeScope.ProjectBrain.refreshCharter(path);
        }
        if (typeof runtimeScope.ProjectBrain.refreshInfluence === 'function') {
          runtimeScope.ProjectBrain.refreshInfluence(path);
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

  runtimeScope.ProjectBrainAttention = {
    renderAttention: renderAttention,
    refreshAttention: refreshAttention,
    focusItem: focusItem,
    _card: _card,
    _sevPill: _sevPill,
    _setBadge: _setBadge,
  };
})();

