/* ═══════════════════════════════════════════════════════════════════
   finish info — extracted from ui.js (split 2026-05-28)

   renderFinishInfo (usage/cost) + file-changes bar.

   This file is concatenated by lib/js_bundler.py — symbols share
   the same window scope as every other static/js/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

// ═══════════════════════════════════════════════════════════════════
// ★ Cost popover — a styled, click-to-open breakdown panel that replaces
//   the old raw `title=""` browser tooltip. Shows per-round token/cost,
//   the API key that served each round, and the cache-invalidation reason.
// ═══════════════════════════════════════════════════════════════════

// Human-friendly labels for the cache-break reason keys stamped by the
// backend (lib/tasks_pkg/cache_tracking.detect_cache_break).
const _CACHE_BREAK_LABELS = {
  system_prompt: 'System prompt 改变',
  tools: '工具定义改变',
  model: '模型切换',
  message_count: '上下文压缩',
  server_side: '服务端缓存失效',
  no_cache_reuse: '上下文重新计费（缓存未复用）',
  // Keep this label SHORT — _cacheBreakReason renders it as `label（cause）`,
  // and the backend cause_str already spells out "非幂等编辑 → 整段上下文重新计费".
  // A long label here would duplicate that detail. This is the most ACTIONABLE
  // break: our own code rewrote bytes inside the cached prefix.
  prefix_mutation: '缓存前缀被改写',
};

// The backend's free-form `cause_str` (server_side / no_cache_reuse value) is
// English — translate the known fragments to Chinese so the popover reads
// naturally. Order matters: longer / more specific phrases first.
const _CACHE_CAUSE_PHRASES = [
  // ⚠ ORDER MATTERS: longer / more-specific phrases MUST precede any phrase
  // that is a SUBSTRING of them, otherwise the short alias matches first and
  // leaves the remainder untranslated (e.g. the bare 'stochastic server-side
  // cache miss' alias would eat the prefix of the full sentence). Each block
  // below is sorted full-sentence → clause → short alias.
  // ── Current cause strings (2026-06-23: stochastic server miss, verified
  //    by identical-prompt live replay — see lib/tasks_pkg/cache_tracking.py) ──
  ['stochastic server-side cache miss (body re-billed; static prefix still cached) — a per-request gateway miss reproduced at <5min gaps with identical input',
   '服务端随机性缓存未命中（正文重新计费，静态前缀仍命中）——网关偶发的单次请求未命中，相同输入在 <5 分钟内复现'],
  ['prefix not reused — stochastic server-side cache miss or TTL expiry (prefix bytes unchanged)',
   '前缀未被复用——服务端随机性缓存未命中或 TTL 过期（前缀字节未变）'],
  ['prefix not reused — stochastic server-side cache miss, TTL expiry, or a silent prefix byte change',
   '前缀未被复用——服务端随机性缓存未命中、TTL 过期，或前缀字节被静默改动'],
  ['a per-request gateway miss reproduced at <5min gaps with identical input', '网关偶发的单次请求未命中，相同输入在 <5 分钟内复现'],
  ['body re-billed; static prefix still cached', '正文重新计费，静态前缀仍命中'],
  ['stochastic server-side cache miss', '服务端随机性缓存未命中'],
  // ── TTL / gap fragments (specific gap clauses before the generic ones) ──
  ['TTL expiry (>5min gap, prompt unchanged)', 'TTL 过期（两次调用间隔 >5 分钟，上下文未变）'],
  ['TTL expiry', 'TTL 过期'],
  ['>5min gap, prompt unchanged', '间隔 >5 分钟，上下文未变'],
  ['<5min gaps with identical input', '<5 分钟、相同输入'],
  ['<5min gap', '间隔 <5 分钟'],
  // ── Legacy cause strings (older persisted rounds) ──
  ['breakpoint advancement (BP4 tail marker moved; the conversation body past it was not read back) — server-side eviction unlikely (static prefix still cached)',
   '缓存断点前移（尾部 BP4 标记移动，其后的会话正文未被读回）——服务端驱逐可能性低（静态前缀仍命中缓存）'],
  ['prefix not reused — breakpoint advancement or server-side eviction (prefix bytes unchanged)',
   '前缀未被复用——缓存断点前移或服务端驱逐（前缀字节未变）'],
  ['server-side eviction unlikely (static prefix still cached)', '服务端驱逐可能性低（静态前缀仍命中缓存）'],
  ['the conversation body past it was not read back', '其后的会话正文未被读回'],
  ['BP4 tail marker moved', '尾部 BP4 标记移动'],
  ['breakpoint advancement or server-side eviction', '缓存断点前移或服务端驱逐'],
  ['prefix bytes unchanged', '前缀字节未变'],
  ['breakpoint advancement', '缓存断点前移'],
  ['server-side eviction', '服务端缓存被驱逐'],
  ['cached prefix bytes changed between turns (non-idempotent history edit) — the whole body was re-billed uncached',
   '两轮之间缓存前缀的字节被改写（历史被非幂等编辑）——整段上下文按未命中重新计费'],
  ['cached prefix bytes changed between turns', '两轮之间缓存前缀字节被改写'],
  ['non-idempotent history edit', '历史被非幂等编辑'],
  ['the whole body was re-billed uncached', '整段上下文按未命中重新计费'],
  ['a silent prefix byte change', '前缀字节被静默改动'],
  ['silent prefix byte change', '前缀字节被静默改动'],
  ['prefix not reused', '前缀未被复用'],
];

/** Translate a backend cause string's known English fragments to Chinese. */
function _translateCacheCause(s) {
  let out = String(s || '');
  for (const [en, zh] of _CACHE_CAUSE_PHRASES) {
    if (out.includes(en)) out = out.split(en).join(zh);
  }
  return out;
}

// SVG glyphs (no emoji — see CLAUDE.md §3.4): key icon for the API-key slot,
// warning triangle for the cache-invalidation line.
const _CP_KEY_SVG = '<svg class="cp-ico" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="7.5" cy="15.5" r="4.5"/><path d="M10.7 12.3 21 2"/><path d="m16 6 3 3"/><path d="m13 9 3 3"/></svg>';
const _CP_WARN_SVG = '<svg class="cp-ico" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>';

/** Render the human-readable cache-break reason for one round, or ''. */
function _cacheBreakReason(cb) {
  if (!cb || typeof cb !== 'object') return '';
  const bits = [];
  for (const k of Object.keys(cb)) {
    const label = _CACHE_BREAK_LABELS[k] || k;
    const val = cb[k];
    // server_side / no_cache_reuse / model carry a descriptive value;
    // the others are flags. server_side / no_cache_reuse values are
    // free-form English from the backend — translate the known fragments.
    if (k === 'server_side' || k === 'no_cache_reuse' || k === 'prefix_mutation') {
      bits.push(`${label}（${escapeHtml(_translateCacheCause(val))}）`);
    } else if (k === 'model' || k === 'message_count') {
      bits.push(`${label}（${escapeHtml(String(val))}）`);
    } else if (k === 'tools' && typeof val === 'string' && val.includes('changed:')) {
      bits.push(`${label}: ${escapeHtml(val.replace('changed:', '').trim())}`);
    } else {
      bits.push(label);
    }
  }
  return bits.join('，');
}

/**
 * Build the inner HTML of the cost popover for one assistant message.
 * Returns an HTML string (already escaped where needed).
 */
function _buildCostPopover(ctx) {
  const { costInfo, rounds, numRounds, u, inp, out, cw, cr, thk, mid, pid, taskId, toolRounds } = ctx;
  const fmt = (n) => (n >= 1000000 ? (n / 1000000).toFixed(1) + "m" : n >= 1000 ? (n / 1000).toFixed(1) + "k" : (n || 0).toString());
  const fCny = (v) => (v >= 0.01 ? "¥" + v.toFixed(3) : v > 0 ? "¥" + v.toFixed(4) : "¥0");
  const _dbg = (typeof _featureFlags !== 'undefined' && _featureFlags.debug_mode);

  let html = '';

  // ── Per-round breakdown table ──
  if (numRounds > 1) {
    html += `<div class="cp-section-title">${escapeHtml(numRounds + '')} 轮 API 调用</div>`;
    html += `<div class="cp-rounds">`;
    // ★ Per-round tool names. The backend stamps `rd.toolCalls` (authoritative,
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
      const _body = _parts.join('、');
      return _sum > 0 ? `${_body}（计 ${fmt(_sum)}）` : _body;
    };
    rounds.forEach((rd, i) => {
      const ru = rd.usage || {};
      const ri = ru.prompt_tokens || ru.input_tokens || 0;
      const ro = ru.completion_tokens || ru.output_tokens || 0;
      const rt = ru.reasoning_tokens || ru.thinking_tokens || 0;
      const rcw = ru.cache_write_tokens || ru.cache_creation_input_tokens || 0;
      const rcr = ru.cache_read_tokens || ru.cache_read_input_tokens || 0;
      const rdCost = rd.cost || calcCostCny(ru, mid, rd.provider_id || rd.providerId || pid);
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
        const _billable = (ri + ro + rt + rcw + rcr) > 0;
        rdCnyStr = _billable ? "…" : "¥0";
      }
      let rdLabel = t("toolPanel.roundTag", { n: i + 1 });
      if (rd.tag && rd.tag.includes("FALLBACK")) rdLabel += ` 回退`;
      // API key that served this round (from dispatch metadata).
      const _disp = ru._dispatch || {};
      const _keyTail = _disp.key_tail;
      const _keyStr = _keyTail ? ('••' + _keyTail) : (_disp.key || '');
      const _model = _disp.model || rd.model || '';
      // Cache-break reason stamped by the backend onto this round.
      const cbReason = _cacheBreakReason(rd.cacheBreak);

      html += `<div class="cp-round">`;
      html += `<div class="cp-round-head">`;
      html += `<span class="cp-round-label">${escapeHtml(rdLabel)}</span>`;
      html += `<span class="cp-round-cost">${escapeHtml(rdCnyStr)}</span>`;
      html += `</div>`;
      html += `<div class="cp-round-tokens">`;
      html += `<span>${escapeHtml(fmt(ri))} → ${escapeHtml(fmt(ro))}</span>`;
      if (rt > 0) html += `<span class="cp-think">✶${escapeHtml(fmt(rt))}</span>`;
      if (rcr > 0) html += `<span class="cp-hit">cache ${escapeHtml(fmt(rcr))}</span>`;
      if (rcw > 0) html += `<span class="cp-write">write ${escapeHtml(fmt(rcw))}</span>`;
      html += `</div>`;
      // ★ Inflow line: the tool RESULTS that flowed INTO this round's
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
        if (_wb.prevOutput > 0)   _terms.push(`上一轮回复 ${fmt(_wb.prevOutput)}`);
        if (_wb.toolResults > 0)  _terms.push(`工具结果 ${fmt(_wb.toolResults)}`);
        if (_wb.contextWrite > 0) _terms.push(`首次缓存上下文 ${fmt(_wb.contextWrite)}`);
        if (_wb.recacheBody > 0)  _terms.push(`重新缓存正文 ${fmt(_wb.recacheBody)}`);
        if (_wb.envelope > 0)     _terms.push(`消息开销 ${fmt(_wb.envelope)}`);
        if (_terms.length) {
          let _tip = `本轮 write = ${fmt(_wb.write)} tok，是“自上一轮以来新写入缓存的上下文”，`
            + `不是模型本轮生成的内容。它由以下几部分构成（后端按真实用量计算，相加恰好等于 write）：`;
          if (_wb.prevOutput > 0)  _tip += `\n· 上一轮回复 ${fmt(_wb.prevOutput)} —— 上一轮模型生成的文本/推理 + 工具调用参数`;
          if (_wb.toolResults > 0) {
            _tip += `\n· 工具结果 ${fmt(_wb.toolResults)} —— 上一轮各工具返回结果`;
            if (_inflowMeta.length) _tip += `（明细：${_fmtToolMeta(_inflowMeta)}；按本地分词统计，与服务端口径略有出入）`;
          }
          if (_wb.contextWrite > 0) _tip += `\n· 首次缓存上下文 ${fmt(_wb.contextWrite)} —— 系统提示、工具定义与历史消息首次写入缓存（通常是首轮的前缀预热；本轮 cache 读取未下降，预期下一轮可低价复用，若未命中会在该轮标注为“重新缓存正文”）`;
          if (_wb.recacheBody > 0) {
            _tip += `\n· 重新缓存正文 ${fmt(_wb.recacheBody)} —— 此前已缓存的会话正文未被读回、被重新计费（真实浪费）`;
            if (_wb.readDrop > 0) _tip += `；本轮 cache 读取较上一轮下降 ${fmt(_wb.readDrop)} tok`;
            if (cbReason) _tip += `，详见下方“缓存失效”`;
          }
          if (_wb.envelope > 0)    _tip += `\n· 消息开销 ${fmt(_wb.envelope)} —— 每条消息的 JSON/role 信封开销（仅占少量，已封顶）`;
          if (_wb.capped) _tip += `\n（注：各分项按本地分词统计，与服务端 write 口径略有差异，已按 write 总量校准，为近似值。）`;
          html += `<div class="cp-round-act cp-round-inflow" title="${escapeHtml(_tip)}">↳ write ${fmt(_wb.write)} 的来源：${escapeHtml(_terms.join(' + '))}</div>`;
          _wbShown = true;
        }
      }
      if (!_wbShown && _inflowMeta.length) {
        // Legacy fallback (rounds persisted before writeBreakdown shipped):
        // just name the previous round's tool results that flowed in. No `=`,
        // no fake equation — these local counts don't equal write.
        const _inflowStr = _fmtToolMeta(_inflowMeta);
        const _tip = '本轮 write 里含上一轮 ' + _inflowMeta.length + ' 个工具的返回结果（首次写入缓存）。'
          + '每项 token 数与工具面板徽章一致，按本地分词统计，与服务端 write 口径略有出入。';
        html += `<div class="cp-round-act cp-round-inflow" title="${escapeHtml(_tip)}">↳ 含上一轮工具结果：${escapeHtml(_inflowStr)}</div>`;
      }
      // ★ Activity line: what the model DID this round (the tool calls it
      //   emitted). This is the causal driver of the NEXT round's `write`.
      const _tcNames = _roundToolNames[i] || [];
      if (_tcNames.length) {
        const _counts = {};
        for (const n of _tcNames) _counts[n] = (_counts[n] || 0) + 1;
        const _actStr = Object.keys(_counts)
          .map(n => _counts[n] > 1 ? `${n}×${_counts[n]}` : n).join('、');
        html += `<div class="cp-round-act" title="${escapeHtml('本轮模型调用了 ' + _tcNames.length + ' 个工具；它们的调用参数与返回结果会在下一轮写入缓存')}">↳ 调用 ${_tcNames.length} 个工具：${escapeHtml(_actStr)}</div>`;
      } else if (i === rounds.length - 1) {
        // Last round with no tool calls = the model's final text answer.
        // Tag it so a user doesn't wonder why the round count (API rounds)
        // exceeds the tool-batch count in the ptool panel.
        html += `<div class="cp-round-act cp-round-final" title="${escapeHtml('本轮没有调用工具，是模型生成的最终回答。这是 API 轮数比工具批次多 1 的原因。')}">↳ 最终回答（无工具）</div>`;
      }
      // ★ Explain a write that's much larger than this round's own output:
      //   the `write` is NOT what the model generated — it's the PREVIOUS
      //   round's output + the tool RESULTS that came back, newly cached.
      //   Only annotate when the gap is real (write ≫ output) to avoid noise.
      const _prev = i > 0 ? rounds[i - 1] : null;
      const _prevTcs = i > 0 ? (_roundToolNames[i - 1] || []).length : 0;
      // Suppress the "healthy warming write" note when this round is flagged
      // as a real cache miss — the cbReason line below explains it instead,
      // and showing both would be contradictory.
      if (!cbReason && !_inflowMeta.length && rcw > 2000 && rcw > ro * 2 && (_prev || _prevTcs)) {
        const _why = _prevTcs
          ? `本轮 write ${fmt(rcw)} ≈ 上一轮输出 + ${_prevTcs} 个工具的返回结果（首次写入缓存），并非本轮模型新生成`
          : `本轮 write ${fmt(rcw)} 主要是上一轮输出 + 返回内容首次写入缓存，并非本轮模型新生成`;
        html += `<div class="cp-round-note" title="${escapeHtml(_why)}">ⓘ write 来源：上一轮产出 + 工具结果</div>`;
      }
      // Meta line: key + (debug) trace.
      const metaBits = [];
      if (_keyStr) metaBits.push(`<span class="cp-key" title="${escapeHtml('Key: ' + _keyStr + (_model ? '  ·  Model: ' + _model : ''))}">${_CP_KEY_SVG}${escapeHtml(_keyStr)}</span>`);
      if (_dbg && ru.trace_id) metaBits.push(`<span class="cp-trace">${escapeHtml(ru.trace_id.slice(0, 8))}</span>`);
      if (metaBits.length) html += `<div class="cp-round-meta">${metaBits.join('')}</div>`;
      if (cbReason) html += `<div class="cp-round-break">${_CP_WARN_SVG}缓存失效：${cbReason}</div>`;
      html += `</div>`;
    });
    html += `</div>`;
    // ★ Legend — explains the cache/write/→ semantics so a user isn't
    //   puzzled why "531 output" becomes "1.5k write" next round.
    html += `<div class="cp-legend">`
      + `<span class="cp-legend-item"><b>X → Y</b>：本轮输入 X / 模型生成 Y</span>`
      + `<span class="cp-legend-item"><b class="cp-hit">cache</b>：复用上文缓存（便宜）</span>`
      + `<span class="cp-legend-item"><b class="cp-write">write</b>：本轮新写入缓存的上下文（贵，预期下一轮可复用——未命中则计为重新缓存正文）；来源＝上一轮回复 ＋ 工具结果 ＋ 首次缓存上下文 ＋ 消息开销，并非模型本轮新生成</span>`
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
  if (thk > 0) html += row('Thinking', `${fmt(thk)} (含在 output 中)`, 'cp-row-sub');
  if (costInfo.cacheSavingsCny > 0) html += row('Cache 节省', fCny(costInfo.cacheSavingsCny), 'cp-row-save');
  html += `</div>`;

  // ── Total ──
  html += `<div class="cp-total-row"><span>Total</span><span class="cp-total-val">${escapeHtml(formatCny(costInfo.costCny))}</span></div>`;

  // ── Task ID (the whole user→assistant turn, across ALL tool rounds) ──
  //   Always shown (not debug-gated): this is the single id the user quotes
  //   back to us so we can grep the matching '[Task:<id>]' lines in app.log
  //   for root-cause analysis. Click to copy.
  if (taskId) {
    const _tidSafe = String(taskId).replace(/\\/g, '\\\\').replace(/'/g, "\\'");
    html += `<div class="cp-taskid-row" title="${escapeHtml('点击复制任务 ID（用于定位日志 / 提供给排查）\nTask ID: ' + taskId)}" onclick="event.stopPropagation();_safeClipboardWrite('${_tidSafe}');this.classList.add('cp-copied')">Task ID: <span class="cp-taskid-val">${escapeHtml(taskId)}</span></div>`;
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

// ── Scroll branch panel to bottom ──
function renderFinishInfo(msg) {
  if (!msg.finishReason && !msg.usage && !msg.model && !msg.preset && !msg.effort) return "";
  const parts = [];
  const _mid = msg.model || msg.preset || msg.effort || "";
  const _pid = msg.provider_id || msg.providerId || "";
  const u = msg.usage || {};
  const fmt = (n) => (n >= 1000000 ? (n / 1000000).toFixed(1) + "m" : n >= 1000 ? (n / 1000).toFixed(1) + "k" : n.toString());
  const thk = u.reasoning_tokens || u.thinking_tokens || 0;

  // ★ Model tag — auto-detect brand from model_id
  const depthIcons = { medium: '', high: '', max: '' };
  const depthLabels = { medium: "Med", high: "Hi", max: "Max" };
  // ★ Resolve the ACTUAL slot that served this turn — real model / key /
  //   provider, recorded by the dispatcher in usage._dispatch. The preset
  //   ("opus") and msg.model can be an alias that routes to a different
  //   upstream model, so prefer the resolved values for an honest finish bar.
  let _disp = null;
  const _dispRounds = msg.apiRounds || [];
  for (let i = _dispRounds.length - 1; i >= 0; i--) {
    const d = ((_dispRounds[i] && _dispRounds[i].usage) || {})._dispatch;
    if (d && (d.model || d.provider_id || d.key)) { _disp = d; break; }
  }
  if (!_disp && u && u._dispatch) _disp = u._dispatch;
  const _realModel = (_disp && _disp.model) || _mid;
  const _realProvider = (_disp && _disp.provider_id) || _pid;
  // Friendly key display: prefer the last 4 chars of the real API key
  //   (rendered as ••1234), else fall back to the raw slot name.
  const _keyTail = _disp && _disp.key_tail;
  const _keyDisplay = _keyTail ? ('••' + _keyTail) : (_disp && _disp.key) || "";

  if (_mid) {
    const _brand = typeof _detectBrand === 'function' ? _detectBrand(_realModel) : 'generic';
    const icon = (typeof _brandSvg === 'function') ? _brandSvg(_brand, 12) : '✦';
    // Show the actual model id (e.g. "aws.claude-opus-4.8"), not the
    // friendly short name — the user wants the real upstream model here.
    const displayName = _realModel;
    // Append thinking depth ONLY for thinking-capable models
    const depth = msg.thinkingDepth || "";
    let depthStr = "";
    const _isThinkModel = typeof _isThinkingCapable === 'function' ? _isThinkingCapable(_realModel) : false;
    if (depth && depthLabels[depth] && _isThinkModel) {
      depthStr = ` ${depthIcons[depth] || ""}${depthLabels[depth]}`;
    }
    parts.push(
      `<span class="finish-tag preset" data-preset="${_brand}" title="Model: ${escapeHtml(_realModel)}${depth ? ' · Depth: ' + escapeHtml(depth) : ''}">${icon} ${displayName}${depthStr}</span>`,
    );
  }

  // ★ Route tag — the actual provider + API-key slot that served this turn.
  if (_realProvider || _keyDisplay) {
    const _provName = (typeof _providerDisplayName === 'function')
      ? _providerDisplayName(_realProvider) : (_realProvider || "");
    const _routeBits = [];
    if (_provName) _routeBits.push(escapeHtml(_provName));
    if (_keyDisplay) _routeBits.push(escapeHtml(_keyDisplay));
    if (_routeBits.length) {
      const _routeTip = [
        _realProvider ? `Provider: ${escapeHtml(_realProvider)}` : "",
        _keyDisplay ? `Key: ${escapeHtml(_keyDisplay)}` : "",
        _realModel ? `Actual model: ${escapeHtml(_realModel)}` : "",
      ].filter(Boolean).join("\n");
      parts.push(`<span class="finish-tag route" title="${_routeTip}">${_routeBits.join(" · ")}</span>`);
    }
  }

  // ★ Finish reason tag — separate from model
  if (msg.finishReason) {
    const normReasons = ["stop", "end_turn", "stop_sequence"];
    const isNorm = normReasons.includes(msg.finishReason);
    const warnReasons = [
      "length",
      "tool_rounds_exhausted",
      "max_tokens",
      "content_filter",
      "premature_close",
      "abnormal_stop",
    ];
    if (isNorm) {
      parts.push(`<span class="finish-tag ok">✓</span>`);
    } else if (msg.finishReason === "error") {
      parts.push(`<span class="finish-tag err">✕ Error</span>`);
    } else if (msg.finishReason === "aborted") {
      parts.push(`<span class="finish-tag warn">Stopped</span>`);
    } else if (msg.finishReason === "interrupted") {
      parts.push(`<span class="finish-tag warn"><span title="Server crashed during generation. Content recovered from last checkpoint — may be incomplete.">Interrupted</span></span>`);
    } else if (msg.finishReason === "server_offline") {
      parts.push(
        `<span class="finish-tag err"><span title="Server went offline during generation (e.g. VSCode disconnect, network drop). Partial response saved.">Server Offline</span></span>` +
        ` <button class="finish-reconnect-btn" onclick="_recoverOfflineConversations('manual_button')" ` +
        `title="Check server for completed result" style="` +
        `font-size:11px;padding:1px 8px;margin-left:4px;cursor:pointer;` +
        `background:var(--accent);color:#fff;border:none;border-radius:4px;` +
        `vertical-align:middle;opacity:0.9` +
        `">${Icon('refresh', 12)} Reconnect</button>`
      );
    } else {
      const labels = {
        length: "Truncated",
        tool_use: "Tool",
        tool_calls: "Tool",
        content_filter: "<span title='" + t('msg.contentFiltered') + "'>Filtered</span>",
        tool_rounds_exhausted: "Tool limit",
        max_tokens: "Truncated",
        premature_close: "<span title='" + t('msg.prematureClose') + "'>" + t('msg.gatewayInterrupt') + "</span>",
        abnormal_stop: "<span title='" + t('msg.abnormalStop') + "'>" + t('msg.abnormalInterrupt') + "</span>",
      };
      const label = labels[msg.finishReason] || msg.finishReason;
      const cls = warnReasons.includes(msg.finishReason) ? "warn" : "";
      parts.push(`<span class="finish-tag ${cls}">${label}</span>`);
    }
  }
  if (u) {
    const inp = u.prompt_tokens || u.input_tokens || 0;
    const out = u.completion_tokens || u.output_tokens || 0;
    // ★ API rounds info
    const rounds = msg.apiRounds || [];
    const numRounds = rounds.length;
    /* ★ Compute display input: for Anthropic-style APIs, prompt_tokens is
     *   only the uncached portion. The total input = uncached + cw + cr. */
    const _cw0 = u.cache_write_tokens || u.cache_creation_input_tokens || 0;
    const _cr0 = u.cache_read_tokens || u.cache_read_input_tokens || 0;
    const _displayInp = (inp <= _cw0 + _cr0 && (_cw0 > 0 || _cr0 > 0))
      ? inp + _cw0 + _cr0   /* Anthropic: inp is uncached only */
      : inp;                /* OpenAI: inp is already total */
    if (_displayInp > 0 || out > 0) {
      let tokText = `${fmt(_displayInp)} → ${fmt(out)}`;
      if (thk > 0)
        tokText += ` <span style="color:#a78bfa;opacity:0.8">(${fmt(thk)}${t('msg.thinking')})</span>`;
      if (numRounds > 1)
        tokText += ` <span style="opacity:0.7">[${numRounds}${t('msg.rounds')}]</span>`;
      parts.push(`<span class="token-tag">${tokText}</span>`);
    }
    const cw = u.cache_write_tokens || u.cache_creation_input_tokens || 0;
    const cr = u.cache_read_tokens || u.cache_read_input_tokens || 0;
    // ★ Enhanced cost display with per-round breakdown.
    //   Prefer the persisted `msg.cost` (stamped server-side at sync time
    //   in lib/tasks_pkg/manager._sync_result_to_conversation). Falls back
    //   to the lazy calcCostCny fetch for legacy messages stored before
    //   the persistence change.
    const costInfo = msg.cost || calcCostCny(u, _mid, _pid);
    if (costInfo && costInfo.costCny > 0) {
      const popHtml = _buildCostPopover({
        costInfo, rounds, numRounds, u, inp, out, cw, cr, thk,
        mid: _mid, pid: _pid, taskId: msg._taskId || '',
        toolRounds: msg.toolRounds || [],
      });
      let savingsHtml = "";
      if (costInfo.cacheSavingsCny > 0)
        savingsHtml = ` <span class="cost-savings">↓${(costInfo.cacheSavingsCny >= 0.01 ? "¥" + costInfo.cacheSavingsCny.toFixed(3) : "¥" + costInfo.cacheSavingsCny.toFixed(4))}</span>`;
      // ★ At-a-glance cache-miss marker: if any round had a cache break,
      //   surface a small warning glyph on the COLLAPSED tag so the user
      //   notices without opening the popover. The full reason is inside.
      let breakHtml = "";
      const _brokenRounds = rounds.filter(rd => rd.cacheBreak);
      if (_brokenRounds.length) {
        const _reasons = _brokenRounds
          .map(rd => _cacheBreakReason(rd.cacheBreak)).filter(Boolean);
        const _tip = '缓存失效（' + _brokenRounds.length + ' 轮）：' +
          (_reasons.join('；') || '未复用缓存');
        breakHtml = ` <span class="cost-cache-warn" title="${escapeHtml(_tip)}">${_CP_WARN_SVG}</span>`;
      }
      parts.push(
        `<span class="cost-tag cost-tag-detail${_brokenRounds.length ? ' cost-tag-warn' : ''}" tabindex="0" role="button" ` +
        `onclick="_toggleCostPopover(event,this)">` +
        `${formatCny(costInfo.costCny)}${savingsHtml}${breakHtml}` +
        `<span class="cost-popover-data" hidden>${popHtml}</span>` +
        `</span>`,
      );
    }
  }
  // ★ Clickable trace tag — click to copy full trace_id (debug mode only)
  if (typeof _featureFlags !== 'undefined' && _featureFlags.debug_mode) {
    const _allTraces = (msg.apiRounds || []).map(rd => (rd.usage || {}).trace_id).filter(Boolean);
    const _lastTrace = _allTraces.length ? _allTraces[_allTraces.length - 1] : ((msg.usage || {}).trace_id || '');
    if (_lastTrace) {
      const _allStr = _allTraces.length > 1 ? _allTraces.join('\n') : _lastTrace;
      // Escape for safe embedding inside inline onclick JS string literal (single-quoted)
      const _jsSafe = _allStr.replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, '\\n');
      parts.push(
        `<span class="finish-tag" style="cursor:pointer;opacity:0.6;font-size:0.8em" title="点击复制 trace_id:\n${escapeHtml(_allStr)}" onclick="_safeClipboardWrite('${_jsSafe}');this.textContent='copied'">${escapeHtml(_lastTrace.slice(0,8))}</span>`
      );
    }
  }
  if (msg.fallbackModel) {
    const _fbReason = msg.fallbackReason || msg.fallbackKind || "";
    const _reasonLine = _fbReason
      ? `\n失败原因 / Reason: ${_fbReason}`
      : "";
    const _tip = `原模型 ${msg.fallbackFrom || "?"} 失败，已回退到 ${msg.fallbackModel}${_reasonLine}`;
    parts.push(
      `<span class="finish-tag warn" title="${escapeHtml(_tip)}">Fallback → Opus</span>`,
    );
  }
  if (parts.length === 0) return "";
  return `<div class="message-finish">${parts.join("")}</div>`;
}

// ═══════════════════════════════════════════════════════════════════
// ★ File Changes Tracker — shows which files were written/patched
// ═══════════════════════════════════════════════════════════════════

/**
 * Extract file change info from toolRounds (during/after streaming).
 * Returns array of {path, action, ok} objects.
 */
// ── File-change extraction (server-authoritative) ─────────────────
//
// The derivation logic — which tool rounds count as file changes, how
// 'rootname:path' is parsed, dedup rules, pending-state tracking —
// lives in `lib/tool_changes.py`. The UI fetches the result from
// `POST /api/v1/messages/extract-file-changes`.
//
// During streaming we'd fire many requests, so the response is cached
// per-fingerprint. The streaming hot path is already fingerprint-gated
// at the call sites (search for `_roundsFp`), so the only async cost is
// one fetch per genuine state change.
//
// `_extractFileChangesFromRoundsAsync` returns a Promise<Array>.
// `_extractedFileChangesCache` caches the LAST computed result so
// synchronous render paths can read the cached entry while a fresh
// fetch is in flight.
const _extractedFileChangesCache = new Map();  // fp → files[]
let _extractedFileChangesPending = new Map();   // fp → Promise<files[]>

function _fcFingerprint(toolRounds) {
  if (!toolRounds || !toolRounds.length) return '';
  // Coarse fingerprint — enough to detect changes worth refetching.
  const last = toolRounds[toolRounds.length - 1];
  return toolRounds.length + ':' + (last.status || '') + ':' +
         (last.toolName || '') + ':' +
         ((last.results && last.results.length) || 0);
}

async function _extractFileChangesFromRoundsAsync(toolRounds) {
  if (!toolRounds || !toolRounds.length) return [];
  const fp = _fcFingerprint(toolRounds);
  if (_extractedFileChangesCache.has(fp)) {
    return _extractedFileChangesCache.get(fp);
  }
  const inflight = _extractedFileChangesPending.get(fp);
  if (inflight) return inflight;

  const promise = (async () => {
    try {
      const body = await Api.conversations.extractFileChanges(toolRounds);
      if (!body) {
        if (typeof debugLog === 'function') {
          debugLog('[FileChanges] extract failed', 'warn');
        }
        return [];
      }
      const files = Array.isArray(body.files) ? body.files : [];
      _extractedFileChangesCache.set(fp, files);
      // Cap the cache so it doesn't grow unbounded over a long session.
      if (_extractedFileChangesCache.size > 64) {
        const firstKey = _extractedFileChangesCache.keys().next().value;
        _extractedFileChangesCache.delete(firstKey);
      }
      return files;
    } catch (err) {
      if (typeof debugLog === 'function') {
        debugLog(`[FileChanges] extract failed: ${err && err.message}`, 'warn');
      }
      return [];
    } finally {
      _extractedFileChangesPending.delete(fp);
    }
  })();
  _extractedFileChangesPending.set(fp, promise);
  return promise;
}

/**
 * Batch-prefetch file-change lists for every message in a conversation
 * that needs server-side derivation (i.e. lacks `modifiedFileList`).
 * Seeds `_extractedFileChangesCache` so the synchronous render paths
 * inside `renderFileChangesBar` hit the cache instead of firing one
 * POST per message.
 *
 * Returns a Promise<boolean>: true if fresh entries landed (caller
 * should force a re-render), false if everything was already cached.
 */
async function _prefetchConvFileChanges(conv) {
  if (!conv || !conv.messages || !conv.messages.length) return false;
  const items = [];
  const fps = [];
  const _seen = new Set();
  for (const m of conv.messages) {
    if (!m || m.modifiedFileList && m.modifiedFileList.length) continue;
    if (!Array.isArray(m.toolRounds) || !m.toolRounds.length) continue;
    const fp = _fcFingerprint(m.toolRounds);
    if (!fp || _seen.has(fp)) continue;
    if (_extractedFileChangesCache.has(fp)) continue;
    if (_extractedFileChangesPending.has(fp)) continue;
    _seen.add(fp);
    items.push({ toolRounds: m.toolRounds });
    fps.push(fp);
  }
  if (!items.length) return false;
  // Per-fp resolvers so concurrent single-shot fetches that hit the
  // pending map get a Promise<files[]> with the right shape (the
  // single-shot contract from _extractFileChangesFromRoundsAsync).
  const _resolvers = fps.map(() => {
    /** @type {(v:any)=>void} */
    let resolve = () => {};
    const p = new Promise((r) => { resolve = r; });
    return { p, resolve };
  });
  for (let i = 0; i < fps.length; i++) {
    _extractedFileChangesPending.set(fps[i], _resolvers[i].p);
  }
  try {
    const body = await Api.conversations.extractFileChangesBatch(items);
    const results = body && Array.isArray(body.results) ? body.results : [];
    for (let i = 0; i < fps.length; i++) {
      const files = Array.isArray(results[i]) ? results[i] : [];
      _extractedFileChangesCache.set(fps[i], files);
      _resolvers[i].resolve(files);
    }
    while (_extractedFileChangesCache.size > 64) {
      const firstKey = _extractedFileChangesCache.keys().next().value;
      _extractedFileChangesCache.delete(firstKey);
    }
    return true;
  } catch (err) {
    if (typeof debugLog === 'function') {
      debugLog(`[FileChanges] batch prefetch failed: ${err && err.message}`, 'warn');
    }
    for (const r of _resolvers) r.resolve([]);
    return false;
  } finally {
    for (const fp of fps) _extractedFileChangesPending.delete(fp);
  }
}
window._prefetchConvFileChanges = _prefetchConvFileChanges;

/**
 * Synchronous accessor — returns the cached file-change list for a given
 * toolRounds blob, or `null` if not yet fetched. Render paths read this
 * to render the bar on the SAME tick the rounds change, then the async
 * fetch (kicked off in parallel) refreshes the cache for the NEXT tick.
 */
function _extractFileChangesFromRoundsCached(toolRounds) {
  if (!toolRounds || !toolRounds.length) return null;
  const fp = _fcFingerprint(toolRounds);
  return _extractedFileChangesCache.has(fp)
    ? _extractedFileChangesCache.get(fp)
    : null;
}

/**
 * Render the file-changes bar for a message.
 * Dual source: prefers modifiedFileList from done event; falls back to toolRounds extraction.
 */
function renderFileChangesBar(msg, msgIdx) {
  // Server-derived list (preferred — git-history backed, authoritative).
  if (msg.modifiedFileList && msg.modifiedFileList.length) {
    const files = msg.modifiedFileList.map(f => ({
      path: f.path, action: f.action, ok: true, count: 1,
      root: f.root || ''
    }));
    return _renderFileChangesHtml(files, false, msgIdx);
  }
  // Fallback: extract from toolRounds via /api/v1/messages/extract-file-changes.
  if (!msg.toolRounds || !msg.toolRounds.length) return '';
  const cached = _extractFileChangesFromRoundsCached(msg.toolRounds);
  if (cached !== null) {
    if (!cached.length) return '';
    return _renderFileChangesHtml(cached, false, msgIdx);
  }
  // No cached entry yet — kick off an async fetch and trigger a re-render
  // when it lands. Returning empty for THIS render tick is fine; the bar
  // appears on the next tick.
  _extractFileChangesFromRoundsAsync(msg.toolRounds).then(() => {
    // Re-render the message list once the cache is fresh.
    const conv = (typeof getActiveConv === 'function') ? getActiveConv() : null;
    if (conv && typeof renderChat === 'function') renderChat(conv);
  });
  return '';
}

/**
 * Core HTML renderer for file changes bar.
 * @param {Array} files - [{path, action, ok, count}]
 * @param {boolean} isStreaming - if true, add pulse animation
 */
function _renderFileChangesHtml(files, isStreaming, msgIdx) {
  if (!files.length) return '';
  const pendingCount = files.filter(f => f.pending).length;
  const okCount = files.filter(f => f.ok && !f.pending).length;
  const failCount = files.filter(f => !f.ok).length;
  const totalFiles = files.length;

  // Action icons
  const actionIcon = (action, ok, pending) => {
    if (pending) return '<span class="fc-icon fc-pending">⟳</span>';
    if (!ok) return '<span class="fc-icon fc-fail">✕</span>';
    if (action === 'created') return '<span class="fc-icon fc-created">+</span>';
    if (action === 'deleted') return '<span class="fc-icon fc-deleted">-</span>';
    if (action === 'modified') return '<span class="fc-icon fc-modified">∆</span>';
    if (action === 'patched') return '<span class="fc-icon fc-patched">~</span>';
    return '<span class="fc-icon fc-written">⇢</span>';
  };

  // Summary line
  const summaryParts = [];
  if (okCount > 0) summaryParts.push(`${okCount} file${okCount > 1 ? 's' : ''} changed`);
  if (pendingCount > 0) summaryParts.push(`${pendingCount} in progress`);
  if (failCount > 0) summaryParts.push(`${failCount} failed`);
  const summaryText = summaryParts.join(', ');
  const pulseClass = isStreaming ? ' fc-pulse' : '';
  const summaryIcon = '';

  // ★ Multi-root: show 'rootname:' prefix when > 1 workspace root is active
  //   so files from different projects are visually distinguishable.
  //   Uses window.projectState (defined in core.js) to detect multi-root mode.
  //   Safe when projectState is undefined (e.g. standalone rendering).
  const _ps = (typeof projectState !== 'undefined') ? projectState : null;
  const _extrasCount = (_ps && Array.isArray(_ps.extraRoots)) ? _ps.extraRoots.length : 0;
  const _multiRoot = _extrasCount > 0;
  // Also collect rootnames seen among the files — if multiple distinct roots
  // appear, force prefix display even if projectState is stale.
  const _rootsSeen = new Set();
  for (const f of files) if (f.root) _rootsSeen.add(f.root);
  const _showRootPrefix = _multiRoot || _rootsSeen.size > 1;

  // File list items
  const fileItems = files.map(f => {
    const dir = f.path.includes('/') ? f.path.substring(0, f.path.lastIndexOf('/') + 1) : '';
    const fname = f.path.includes('/') ? f.path.substring(f.path.lastIndexOf('/') + 1) : f.path;
    const countBadge = f.count > 1 ? ` <span class="fc-count">×${f.count}</span>` : '';
    const pendingCls = f.pending ? ' fc-file-pending' : '';
    const rootPrefix = (_showRootPrefix && f.root)
      ? `<span class="fc-root">${escapeHtml(f.root)}:</span>`
      : '';
    const fullPath = (f.root ? f.root + ':' : '') + f.path;
    return `<div class="fc-file${f.ok ? '' : ' fc-file-err'}${pendingCls}" title="${escapeHtml(fullPath)}">
      ${actionIcon(f.action, f.ok, f.pending)}
      <span class="fc-path">${rootPrefix}<span class="fc-dir">${escapeHtml(dir)}</span><span class="fc-fname">${escapeHtml(fname)}</span></span>
      <span class="fc-action">${escapeHtml(f.action)}${countBadge}</span>
    </div>`;
  }).join('');

  // ★ Undo button — only for finalized (non-streaming) messages with a valid msgIdx
  const undoBtn = (!isStreaming && typeof msgIdx === 'number')
    ? `<button class="fc-undo-btn" onclick="event.stopPropagation();undoConvModifications(${msgIdx})" title="撤销本轮修改">` +
      `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7v6h6"/><path d="M21 17a9 9 0 0 0-15-6.7L3 13"/></svg>` +
      `<span>Undo</span></button>`
    : '';

  // ★ Undo All button — always available as a separate interaction point
  const undoAllBtn = (!isStreaming && typeof msgIdx === 'number')
    ? `<button class="fc-undo-all-btn" onclick="event.stopPropagation();undoAllModifications()" title="撤销所有对话中的所有修改">` +
      `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7v6h6"/><path d="M21 17a9 9 0 0 0-15-6.7L3 13"/><line x1="12" y1="7" x2="12" y2="3"/><line x1="8" y1="7" x2="12" y2="7"/></svg>` +
      `<span>Undo All</span></button>`
    : '';

  const actionBtns = (undoBtn || undoAllBtn)
    ? `<div class="fc-actions">${undoBtn}${undoAllBtn}</div>` : '';

  // Auto-expand for ≤ 5 files so users see details immediately
  const autoExpand = totalFiles <= 5 ? ' fc-expanded' : '';
  return `<div class="file-changes-bar${pulseClass}${autoExpand}" data-fc-count="${totalFiles}">
    <div class="fc-summary" onclick="this.parentElement.classList.toggle('fc-expanded')">
      <span class="fc-summary-icon">${summaryIcon}</span>
      <span class="fc-summary-text">${summaryText}</span>
      ${actionBtns}
      <span class="fc-chevron">›</span>
    </div>
    <div class="fc-details">${fileItems}</div>
  </div>`;
}

