/* ===== migrated source: ui/finish_info_rich.js ===== */
/* ui/finish_info_rich.js — DEFERRED cost-popover family (Epic-E
 *  sub-8, split out of ui/finish_info.js 2026-08-01).
 *
 * _buildCostPopover (19KB per-round cost/cache-break breakdown) + the
 * popover interaction cluster (_toggleCostPopover / _hideCostPopover /
 * outside-click / scroll-dismiss). The finish bar used to embed the
 * fully-built popover HTML into every painted message; now
 * renderFinishInfo (core) stashes the build ctx in _costCtxByTurnId and
 * the popover builds on FIRST open after the feature bridge prepares this
 * lazy ESM domain → _toggleCostPopover builds + shows. The cache-break
 * phrase family (_CACHE_CAUSE_PHRASES / _translateCacheCause /
 * _cacheBreakReason / _cacheBreakState / _cacheBreakCulprits / _CP_*_SVG)
 * STAYS in core (the collapsed bar's warn tooltip renders at paint).
 */

function _buildCostPopover(ctx) {
  const { costInfo, rounds, numRounds, u, inp, out, cw, cr, thk, mid, pid, taskId, toolRounds } = ctx;
  const fmt = (n) => (n >= 1000000 ? (n / 1000000).toFixed(1) + "m" : n >= 1000 ? (n / 1000).toFixed(1) + "k" : (n || 0).toString());
  const fCny = (v) => (v >= 0.01 ? "¥" + v.toFixed(3) : v > 0 ? "¥" + v.toFixed(4) : "¥0");
  const _dbg = (typeof _featureFlags !== 'undefined' && _featureFlags.debug_mode);

  let html = '';

  // ── Per-round breakdown table ──
  if (numRounds > 0) {
    const _mainCalls = rounds.filter((rd) => rd?.kind !== 'compaction').length;
    const _compactionCalls = rounds.filter((rd) => rd?.kind === 'compaction').length;
    const _titleKey = _compactionCalls
      ? 'finishInfo.apiCallsWithCompactionTitle' : 'finishInfo.apiRoundsTitle';
    html += `<div class="cp-section-title">${escapeHtml(t(_titleKey, {
      n: numRounds, main: _mainCalls, compaction: _compactionCalls,
    }))}</div>`;
    html += `<div class="cp-rounds">`;
    // Per-round tool names. The backend stamps `rd.toolCalls` (authoritative,
    //   exactly the tool_calls the model emitted), but it's absent on every
    //   message persisted before that stamp shipped. Derive a fallback from
    //   the message's toolRounds[] — each carries toolName + llmRound (0-based),
    //   and a displayed round with numeric `round` (1-based) maps as
    //   round === llmRound + 1. This makes the activity line work for EVERY
    //   message (old + new) with zero backend round-trip.
    const _toolsByLlmRound = {};
    // Per-llmRound tool RESULT token metadata — drives the "工具结果流入" line.
    // Each entry: {name, tokens}. tokens come from toolRounds[].toolTokens
    // (the same local count the ptool panel badge shows), 0 when absent.
    const _toolMetaByLlmRound = {};
    for (const tr of (Array.isArray(toolRounds) ? toolRounds : [])) {
      if (!tr || typeof tr !== 'object') continue;
      const _lr = tr.llmRound;
      const _nm = tr.toolName || tr.name;
      if (typeof _lr !== 'number' || !_nm) continue;
      (_toolsByLlmRound[_lr] = _toolsByLlmRound[_lr] || []).push(_nm);
      (_toolMetaByLlmRound[_lr] = _toolMetaByLlmRound[_lr] || [])
        .push({ name: _nm, tokens: (typeof tr.toolTokens === 'number' ? tr.toolTokens : 0) });
    }
    const _roundToolNames = rounds.map((rd) => {
      if (Array.isArray(rd.toolCalls) && rd.toolCalls.length) return rd.toolCalls;
      const _rnum = rd.round;
      if (typeof _rnum === 'number' && _toolsByLlmRound[_rnum - 1]) {
        return _toolsByLlmRound[_rnum - 1];
      }
      return [];
    });
    // Parallel to _roundToolNames, but with per-tool RESULT token sizes (the
    // same numbers the ptool panel badges show). A round whose apiRound `round`
    // is R maps to llmRound R-1, so display-round index i (round R=i+1) carries
    // the tools tagged llmRound i. Used to render the concrete inflow line —
    // these results flow into the NEXT round's context/write.
    const _roundToolMeta = rounds.map((rd) => {
      const _rnum = rd.round;
      if (typeof _rnum === 'number' && _toolMetaByLlmRound[_rnum - 1]) {
        return _toolMetaByLlmRound[_rnum - 1];
      }
      return [];
    });
    // Format a tool-meta list as "name S、name S（计 T）" with token sizes.
    const _fmtToolMeta = (meta) => {
      let _sum = 0;
      const _parts = meta.map((m) => {
        _sum += (m.tokens || 0);
        return m.tokens ? `${m.name} ${fmt(m.tokens)}` : m.name;
      });
      const _body = _parts.join(t('finishInfo.listSepDot'));
      return _sum > 0 ? `${_body}${t('finishInfo.metaSum', { v: fmt(_sum) })}` : _body;
    };
    let _compactionCallIndex = 0;
    rounds.forEach((rd, i) => {
      const ru = rd.usage || {};
      const _rawInput = ru.prompt_tokens || ru.input_tokens || 0;
      const ro = ru.completion_tokens || ru.output_tokens || 0;
      const rt = ru.reasoning_tokens || ru.thinking_tokens || 0;
      const rcw = ru.cache_write_tokens || ru.cache_creation_input_tokens || 0;
      const rcr = ru.cache_read_tokens || ru.cache_read_input_tokens || 0;
      const rdCost = rd.cost || calcCostCny(ru, mid, rd.provider_id || rd.providerId || pid);
      const ri = rdCost ? (Number(rdCost.totalInputTokens) || 0) : 0;
      const rUncached = rdCost ? (Number(rdCost.inputTokens) || 0) : 0;
      // Honest cost label. calcCostCny returns null for BOTH "genuinely no
      // charge" AND "couldn't obtain the server number" (fetch pending or
      // failed). Only print ¥0 in the FIRST case — when the round consumed no
      // billable tokens at all. If the round DID consume tokens but we have no
      // server cost yet, show "…" (待计算), never a fabricated ¥0 for a round
      // that cost money. The only source of truth is the backend.
      let rdCnyStr;
      if (rdCost) {
        rdCnyStr = fCny(rdCost.costCny);
      } else {
        const _billable = (_rawInput + ro + rt + rcw + rcr) > 0;
        rdCnyStr = _billable ? "…" : "¥0";
      }
      let rdLabel;
      if (rd.kind === 'compaction') {
        _compactionCallIndex += 1;
        rdLabel = t('finishInfo.compactionCallLabel', {
          n: _compactionCallIndex,
        });
      } else {
        rdLabel = t("toolPanel.roundTag", { n: i + 1 - _compactionCallIndex });
      }
      if (rd.tag && rd.tag.includes("FALLBACK")) rdLabel += t('finishInfo.fallbackSuffix');
      // API key that served this round (from dispatch metadata).
      const _disp = ru._dispatch || {};
      const _keyTail = _disp.key_tail;
      const _keyStr = _keyTail ? ('••' + _keyTail) : (_disp.key || '');
      const _model = _disp.model || rd.model || '';
      // Cache-break reason stamped by the backend onto this round.
      const cbReason = _cacheBreakReason(rd.cacheBreak);
      // Fault STATE (proven-server / unproven / our-culprit) + the named
      // culprit list, so the popover shows WHOSE fault and WHICH message.
      const cbState = _cacheBreakState(rd.cacheBreak);
      const cbCulprits = _cacheBreakCulprits(rd.cacheBreak);

      html += `<div class="cp-round">`;
      html += `<div class="cp-round-head">`;
      html += `<span class="cp-round-label">${escapeHtml(rdLabel)}</span>`;
      html += `<span class="cp-round-cost">${escapeHtml(rdCnyStr)}</span>`;
      html += `</div>`;
      html += `<div class="cp-round-tokens">`;
      html += `<span>${escapeHtml(t('finishInfo.inputSplit', {
        total: rdCost ? fmt(ri) : t('finishInfo.pendingValue'),
        uncached: rdCost ? fmt(rUncached) : t('finishInfo.pendingValue'),
        cache: fmt(rcr), output: fmt(ro),
      }))}</span>`;
      if (rt > 0) html += `<span class="cp-think">${Icon('brain', 11)}${escapeHtml(fmt(rt))}</span>`;
      if (rcr > 0) html += `<span class="cp-hit">cache ${escapeHtml(fmt(rcr))}</span>`;
      if (rcw > 0) html += `<span class="cp-write">write ${escapeHtml(fmt(rcw))}</span>`;
      html += `</div>`;

      const _admission = rd.promptAdmission;
      if (_admission && Number(_admission.totalTokens) > 0) {
        const _admissionKey = _admission.action === 'compact_then_admit'
          ? 'finishInfo.promptAdmissionCompacted'
          : 'finishInfo.promptAdmissionAdmitted';
        html += `<div class="cp-round-meta"><span>${escapeHtml(t(_admissionKey, {
          total: fmt(Number(_admission.totalTokens) || 0),
          messages: fmt(Number(_admission.messageTokens) || 0),
          tools: fmt(Number(_admission.toolSchemaTokens) || 0),
          target: fmt(Number(_admission.targetTokens) || 0),
        }))}</span></div>`;
      }
      // Inflow line: the tool RESULTS that flowed INTO this round's
      //   context (= the PREVIOUS round's tool calls, fed back). This is ONE
      //   component of this round's `write` — NOT the whole thing. The write
      //   also covers the previous round's assistant turn (reasoning text +
      //   the serialized tool_call argument blocks) plus per-message JSON/role
      //   envelope overhead. We reconcile EXPLICITLY against `rcw` so the two
      //   numbers visibly add up instead of looking contradictory: the seam is
      //   end-to-end traceable: ptool badge → 工具结果 → +其余 = this round's write.
      const _inflowMeta = i > 0 ? (_roundToolMeta[i - 1] || []) : [];
      // The authoritative `write` decomposition computed on the BACKEND
      // (lib/tasks_pkg/orchestrator._compute_write_breakdown) from real
      // recorded usage, stamped on rd.writeBreakdown as
      // {write, toolResults, prevOutput, recacheBody, envelope}. Its sub-items
      // sum to EXACTLY `write` by construction, so we render it as a single
      // plain equation a reader can verify adds up. The per-tool sizes (the
      // ptool-badge numbers) are measured with a DIFFERENT tokenizer and do
      // NOT match these provider-side components, so they live in the tooltip
      // only — never on the same line next to a `=` (that was the
      // "833 = 214 / 工具结果=833 vs 工具结果=42" contradiction users hit).
      const _wb = rd.writeBreakdown;
      let _wbShown = false;
      if (_wb && _wb.write > 0) {
        const _terms = [];
        if (_wb.prevOutput > 0)   _terms.push(t('finishInfo.wbPrevOutput', { v: fmt(_wb.prevOutput) }));
        if (_wb.toolResults > 0) {
          // Make the offset-by-one explicit ON THE ROW (not buried in the
          // tooltip): the tool RESULTS in round (i+1)'s write came from the
          // PREVIOUS round's tool batch, which the tool panel labels 第i轮.
          // Round display index i maps to llmRound i; its inflow is llmRound
          // i-1 = tool batch label i. Annotating the batch number lets a
          // reader cross-check the two panels directly instead of inferring
          // the offset (the "第3轮 vs 批次3 never matches" confusion).
          let _tr = t('finishInfo.wbToolResults', { v: fmt(_wb.toolResults) });
          if (i > 0) _tr += t('finishInfo.wbBatchRef', { n: i });
          _terms.push(_tr);
        }
        if (_wb.contextWrite > 0) _terms.push(t('finishInfo.wbContextWrite', { v: fmt(_wb.contextWrite) }));
        if (_wb.recacheBody > 0)  _terms.push(t('finishInfo.wbRecacheBody', { v: fmt(_wb.recacheBody) }));
        if (_wb.envelope > 0)     _terms.push(t('finishInfo.wbEnvelope', { v: fmt(_wb.envelope) }));
        if (_terms.length) {
          let _tip = t('finishInfo.wbTipHead', { v: fmt(_wb.write) });
          if (_wb.prevOutput > 0)  _tip += t('finishInfo.wbTipPrevOutput', { v: fmt(_wb.prevOutput) });
          if (_wb.toolResults > 0) {
            _tip += t('finishInfo.wbTipToolResults', { v: fmt(_wb.toolResults) });
            if (_inflowMeta.length) _tip += t('finishInfo.wbTipToolResultsDetail', { detail: _fmtToolMeta(_inflowMeta) });
          }
          if (_wb.contextWrite > 0) _tip += t('finishInfo.wbTipContextWrite', { v: fmt(_wb.contextWrite) });
          if (_wb.recacheBody > 0) {
            _tip += t('finishInfo.wbTipRecacheBody', { v: fmt(_wb.recacheBody) });
            if (_wb.readDrop > 0) _tip += t('finishInfo.wbTipReadDrop', { v: fmt(_wb.readDrop) });
            if (cbReason) _tip += t('finishInfo.wbTipSeeBreak');
          }
          if (_wb.envelope > 0)    _tip += t('finishInfo.wbTipEnvelope', { v: fmt(_wb.envelope) });
          if (_wb.capped) _tip += t('finishInfo.wbTipCapped');
          // When the components were capped (local-vs-provider tokenizer
          // mismatch) they do NOT add up exactly — use ≈ and an explicit
          // "约" so the row isn't presented as an exact equation.
          const _sumLabel = _wb.capped
            ? t('finishInfo.wbSumApprox', { v: fmt(_wb.write), terms: escapeHtml(_terms.join(' + ')) })
            : t('finishInfo.wbSum', { v: fmt(_wb.write), terms: escapeHtml(_terms.join(' + ')) });
          html += `<div class="cp-round-act cp-round-inflow" title="${escapeHtml(_tip)}">${_sumLabel}</div>`;
          _wbShown = true;
        }
        // Re-cache WASTE line. When the backend attributed part of this
        //   round's write to `recacheBody` (already-cached body re-billed) but
        //   the banner-level detector stayed SILENT (no rd.cacheBreak — the
        //   sub-threshold / cross-turn round-1 read drop the Stage-1 fix now
        //   catches), the "重新缓存正文" term would otherwise sit bare in the
        //   equation with no explanation. Surface it from the breakdown's own
        //   data (recacheBody + readDrop) so the user SEES why the round cost
        //   money. Suppressed when a banner cbReason IS present — the
        //   cp-round-break line below explains it and two lines would duplicate.
        if (_wb.recacheBody > 0 && !cbReason) {
          const _wasteTip = t('finishInfo.wbWasteTip', {
            v: fmt(_wb.recacheBody), drop: fmt(_wb.readDrop || 0) });
          html += `<div class="cp-round-waste" title="${escapeHtml(_wasteTip)}">${escapeHtml(t('finishInfo.wbWasteLabel', { v: fmt(_wb.recacheBody), drop: fmt(_wb.readDrop || 0) }))}</div>`;
        }
      }
      if (!_wbShown && _inflowMeta.length) {
        // Legacy fallback (rounds persisted before writeBreakdown shipped):
        // just name the previous round's tool results that flowed in. No `=`,
        // no fake equation — these local counts don't equal write.
        const _inflowStr = _fmtToolMeta(_inflowMeta);
        const _tip = t('finishInfo.inflowTip', { n: _inflowMeta.length });
        html += `<div class="cp-round-act cp-round-inflow" title="${escapeHtml(_tip)}">${escapeHtml(t('finishInfo.inflowLabel', { detail: _inflowStr }))}</div>`;
      }
      // Activity line: what the model DID this round (the tool calls it
      //   emitted). This is the causal driver of the NEXT round's `write`.
      const _tcNames = _roundToolNames[i] || [];
      if (_tcNames.length) {
        const _counts = {};
        for (const n of _tcNames) _counts[n] = (_counts[n] || 0) + 1;
        const _actStr = Object.keys(_counts)
          .map(n => _counts[n] > 1 ? `${n}×${_counts[n]}` : n).join(t('finishInfo.listSepDot'));
        html += `<div class="cp-round-act" title="${escapeHtml(t('finishInfo.actTip', { n: _tcNames.length }))}">${escapeHtml(t('finishInfo.actLabel', { n: _tcNames.length, tools: _actStr }))}</div>`;
      } else if (i === rounds.length - 1) {
        // Last round with no tool calls = the model's final text answer.
        // Tag it so a user doesn't wonder why the round count (API rounds)
        // exceeds the tool-batch count in the ptool panel.
        html += `<div class="cp-round-act cp-round-final" title="${escapeHtml(t('finishInfo.finalTip'))}">${escapeHtml(t('finishInfo.finalLabel'))}</div>`;
      }
      // Explain a write that's much larger than this round's own output:
      //   the `write` is NOT what the model generated — it's the PREVIOUS
      //   round's output + the tool RESULTS that came back, newly cached.
      //   Only annotate when the gap is real (write ≫ output) to avoid noise.
      const _prev = i > 0 ? rounds[i - 1] : null;
      const _prevTcs = i > 0 ? (_roundToolNames[i - 1] || []).length : 0;
      // Suppress the "healthy warming write" note when this round is flagged
      // as a real cache miss (cbReason) OR the authoritative writeBreakdown
      // equation was already shown (_wbShown). The legacy heuristic note used
      // to fire redundantly ON TOP of the exact breakdown — worse, on a turn's
      // round-1 it claimed "上一轮产出 + 工具结果" for a round that has no
      // previous output, directly contradicting the equation above it. The
      // breakdown row is authoritative; only fall back to the heuristic note
      // when NO breakdown was rendered (legacy rounds persisted before it).
      if (!cbReason && !_wbShown && !_inflowMeta.length && rcw > 2000 && rcw > ro * 2 && (_prev || _prevTcs)) {
        const _why = _prevTcs
          ? t('finishInfo.writeNoteTipTools', { v: fmt(rcw), n: _prevTcs })
          : t('finishInfo.writeNoteTipPlain', { v: fmt(rcw) });
        html += `<div class="cp-round-note" title="${escapeHtml(_why)}">${escapeHtml(t('finishInfo.writeNoteLabel'))}</div>`;
      }
      // Meta line: key + (debug) trace.
      const metaBits = [];
      if (_keyStr) metaBits.push(`<span class="cp-key" title="${escapeHtml('Key: ' + _keyStr + (_model ? '  ·  Model: ' + _model : ''))}">${_CP_KEY_SVG}${escapeHtml(_keyStr)}</span>`);
      if (_dbg && ru.trace_id) metaBits.push(`<span class="cp-trace">${escapeHtml(ru.trace_id.slice(0, 8))}</span>`);
      if (metaBits.length) html += `<div class="cp-round-meta">${metaBits.join('')}</div>`;
      if (cbReason) {
        // State-tagged line: green-ish 'proven server' (not our fault),
        // amber 'unproven' (unknown), red 'culprit' (our fault, actionable).
        const _stCls = cbState ? ` cp-break-${cbState}` : '';
        const _stLabel = cbState ? `<span class="cp-break-badge cp-break-badge-${cbState}">${t('finishInfo.cbState.' + cbState)}</span>` : '';
        // The reason prose MUST be wrapped in its own span: as a bare text
        // node it becomes an anonymous flex item whose CJK min-content is ONE
        // character, so a long state badge (e.g. 'upstream') squeezes it to a
        // 1-char-per-line column (2026-07-24 overflow bug).
        html += `<div class="cp-round-break${_stCls}">${_CP_WARN_SVG}${_stLabel}<span class="cp-break-text">${t('finishInfo.cacheBreakLabel', { reason: cbReason })}</span></div>`;
        // The named culprit — WHICH message(s) broke cache — surfaced on its
        // own line so the user can act on it, not hunt error.log.
        if (cbCulprits) {
          html += `<div class="cp-break-culprit">${t('finishInfo.cbCulpritLabel', { culprits: escapeHtml(cbCulprits) })}</div>`;
        }
      }
      html += `</div>`;
    });
    html += `</div>`;
    // Legend — explains the cache/write/→ semantics so a user isn't
    //   puzzled why "531 output" becomes "1.5k write" next round.
    html += `<div class="cp-legend">`
      + `<span class="cp-legend-item">${t('finishInfo.legendXY')}</span>`
      + `<span class="cp-legend-item">${t('finishInfo.legendCache')}</span>`
      + `<span class="cp-legend-item">${t('finishInfo.legendWrite')}</span>`
      + `</div>`;
  }

  // ── Aggregate cost rows ──
  html += `<div class="cp-totals">`;
  const _si = costInfo.inputTokens || 0;
  const _totalInp = costInfo.totalInputTokens || inp;
  const _inTokens = (_totalInp > _si && _si >= 0) ? _si : inp;
  const row = (label, val, cls) =>
    `<div class="cp-row${cls ? ' ' + cls : ''}"><span class="cp-row-label">${label}</span><span class="cp-row-val">${escapeHtml(val)}</span></div>`;
  html += row('Input', `${fmt(_inTokens)} → ${fCny(costInfo.inputCostCny)}`);
  if (cw > 0) html += row('Cache write', `${fmt(cw)} → ${fCny(costInfo.cacheWriteCostCny)}`);
  if (cr > 0) html += row('Cache read', `${fmt(cr)} → ${fCny(costInfo.cacheReadCostCny)}`);
  html += row('Output', `${fmt(out)} → ${fCny(costInfo.outputCostCny)}`);
  if (thk > 0) html += row('Thinking', `${fmt(thk)} ${t('finishInfo.thinkingInOutput')}`, 'cp-row-sub');
  if (costInfo.cacheSavingsCny > 0) html += row(t('finishInfo.cacheSavings'), fCny(costInfo.cacheSavingsCny), 'cp-row-save');
  html += `</div>`;

  // ── Total ──
  html += `<div class="cp-total-row"><span>Total</span><span class="cp-total-val">${escapeHtml(formatCny(costInfo.costCny))}</span></div>`;

  // ── Task ID (the whole user→assistant turn, across ALL tool rounds) ──
  //   Always shown (not debug-gated): this is the single id the user quotes
  //   back to us so we can grep the matching '[Task:<id>]' lines in app.log
  //   for root-cause analysis. Click to copy.
  if (taskId) {
    const _tidSafe = String(taskId).replace(/\\/g, '\\\\').replace(/'/g, "\\'");
    html += `<div class="cp-taskid-row" title="${escapeHtml(t('finishInfo.taskIdTip', { id: taskId }))}" data-tofu-action="event.stopPropagation();_safeClipboardWrite('${_tidSafe}');this.classList.add('cp-copied')">Task ID: <span class="cp-taskid-val">${escapeHtml(taskId)}</span></div>`;
  }

  // ── Trace ids (debug only) ──
  if (_dbg) {
    const traceIds = rounds.map(rd => (rd.usage || {}).trace_id).filter(Boolean);
    const lastTrace = traceIds.length ? traceIds[traceIds.length - 1] : (u.trace_id || '');
    if (lastTrace) {
      html += `<div class="cp-trace-row" title="${escapeHtml(traceIds.join('\n') || lastTrace)}">TraceId: ${escapeHtml(lastTrace)}</div>`;
    }
  }

  return html;
}

// One floating popover element, reused across cost tags.
let _costPopoverEl = null;

function _hideCostPopover() {
  if (_costPopoverEl) { _costPopoverEl.remove(); _costPopoverEl = null; }
  document.removeEventListener('click', _costPopoverOutside, true);
  window.removeEventListener('scroll', _costPopoverScroll, true);
  window.removeEventListener('resize', _hideCostPopover, true);
}

function _costPopoverOutside(e) {
  if (_costPopoverEl && !_costPopoverEl.contains(e.target) && !e.target.closest('.cost-tag-detail')) {
    _hideCostPopover();
  }
}

// Scroll-dismiss handler. Registered with capture:true so it fires for
// ANY scroll on the page — but a scroll INSIDE the popover (its own
// .cp-rounds / body overflow) must NOT close it, else the panel can never
// be scrolled. Ignore scrolls whose target is within the popover.
function _costPopoverScroll(e) {
  if (_costPopoverEl && _costPopoverEl.contains(e.target)) return;
  _hideCostPopover();
}

/** Toggle the floating cost popover anchored to the clicked cost tag. */
function _toggleCostPopover(ev, tagEl) {
  ev.stopPropagation();
  const wasOpen = _costPopoverEl && _costPopoverEl._anchor === tagEl;
  _hideCostPopover();
  if (wasOpen) return;

  const data = tagEl.querySelector('.cost-popover-data');
  if (!data) return;
  /* Lazy build (Epic-E sub-8): the core bar no longer embeds pre-built
   * popover HTML — renderFinishInfo stashes the build ctx in the
   * _costCtxByTurnId Map and this module (deferred) builds on FIRST
   * open. Embedded content (a mixed-shape bundle where core still
   * embeds) wins when present. */
  if (!data.innerHTML.trim()) {
    const _turnId = tagEl.closest('[data-turn-id]')?.dataset?.turnId || '';
    const _ctx = _turnId && (typeof _costCtxByTurnId !== 'undefined')
      ? _costCtxByTurnId.get(_turnId) : null;
    if (_ctx) data.innerHTML = _buildCostPopover(_ctx);
  }
  if (!data.innerHTML.trim()) return;

  const pop = document.createElement('div');
  pop.className = 'cost-popover';
  pop._anchor = tagEl;
  pop.innerHTML = data.innerHTML;
  pop.style.position = 'fixed';
  pop.style.top = '-9999px';
  pop.style.left = '-9999px';
  document.body.appendChild(pop);

  const M = 8;                       // viewport margin
  const GAP = 8;                     // gap between tag and popover
  const vh = window.innerHeight;
  const r = tagEl.getBoundingClientRect();
  const pw = pop.offsetWidth || 320;
  let ph = pop.offsetHeight || 200;

  // Horizontal: align left edge to the tag, clamp into the viewport.
  let left = Math.round(r.left);
  const maxLeft = window.innerWidth - pw - M;
  if (left > maxLeft) left = Math.max(M, maxLeft);
  if (left < M) left = M;

  // Vertical: pick the side (above / below) with more room, then cap the
  // popover's max-height to the available space so a tall breakdown gets an
  // internal scrollbar instead of overflowing off-screen.
  const spaceAbove = r.top - GAP - M;
  const spaceBelow = vh - r.bottom - GAP - M;
  let top;
  if (ph <= spaceAbove) {
    top = Math.round(r.top - ph - GAP);          // fits above
  } else if (ph <= spaceBelow) {
    top = Math.round(r.bottom + GAP);            // fits below
  } else if (spaceAbove >= spaceBelow) {
    pop.style.maxHeight = `${Math.max(120, Math.floor(spaceAbove))}px`;
    ph = pop.offsetHeight;
    top = Math.round(Math.max(M, r.top - ph - GAP));
  } else {
    pop.style.maxHeight = `${Math.max(120, Math.floor(spaceBelow))}px`;
    top = Math.round(r.bottom + GAP);
  }
  pop.style.left = `${left}px`;
  pop.style.top = `${top}px`;
  _costPopoverEl = pop;

  setTimeout(() => {
    document.addEventListener('click', _costPopoverOutside, true);
    window.addEventListener('scroll', _costPopoverScroll, true);
    window.addEventListener('resize', _hideCostPopover, true);
  }, 0);
}
