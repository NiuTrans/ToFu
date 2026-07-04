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

// Known cache-break reason keys stamped by the backend
// (lib/tasks_pkg/cache_tracking.detect_cache_break). The human-readable label
// for each resolves at render time via t('finishInfo.cb.<key>') so it follows
// the current UI language (zh/en). Membership in this Set is the "is this a
// known flag key?" predicate used by _cacheBreakReason. server_side /
// no_cache_reuse / prefix_mutation carry a descriptive backend value that we
// render verbatim (translated) instead of a fixed label.
const _CACHE_BREAK_KEYS = new Set([
  'system_prompt', 'tools', 'model', 'message_count',
  'server_side', 'no_cache_reuse', 'prefix_mutation',
]);

// The backend's free-form `cause_str` (server_side / no_cache_reuse value) is
// English — translate the known fragments to Chinese so the popover reads
// naturally. Order matters: longer / more specific phrases first.
const _CACHE_CAUSE_PHRASES = [
  // ⚠ ORDER MATTERS: longer / more-specific phrases MUST precede any phrase
  // that is a SUBSTRING of them, otherwise the short alias matches first and
  // leaves the remainder untranslated (e.g. the bare 'stochastic server-side
  // cache miss' alias would eat the prefix of the full sentence). Each block
  // below is sorted full-sentence → clause → short alias.
  // ── Wire-fingerprint verdicts (2026-07: PROVEN vs UNPROVEN — see
  //    lib/tasks_pkg/wire_fingerprint.py + _resolve_break_cause). The miss is
  //    only called server-side as FACT when the post-translation wire bytes
  //    were confirmed byte-identical to the previous round; otherwise it is
  //    honestly hedged UNPROVEN. FULL sentences precede their clauses. ──
  ['server-side cache miss — PROVEN: the wire bytes were byte-identical to the previous round (only the body past the static prefix was not read back)',
   '服务端缓存未命中——已实证：本轮发出的字节与上一轮逐字节相同（仅静态前缀之后的正文未被读回）'],
  ['server-side cache miss — PROVEN: the wire bytes were byte-identical to the previous round (whole prefix not reused)',
   '服务端缓存未命中——已实证：本轮发出的字节与上一轮逐字节相同（整段前缀未被复用）'],
  ['likely server-side cache miss (UNPROVEN — no wire fingerprint; body re-billed, static prefix still cached)',
   '疑似服务端缓存未命中（未证实——无线上指纹；正文重新计费，静态前缀仍命中）'],
  ['prefix not reused — likely server-side miss or TTL expiry (UNPROVEN — no wire fingerprint)',
   '前缀未被复用——疑似服务端未命中或 TTL 过期（未证实——无线上指纹）'],
  ['prefix not reused — likely server-side miss, TTL expiry, or a silent prefix byte change (UNPROVEN — no wire fingerprint)',
   '前缀未被复用——疑似服务端未命中、TTL 过期，或前缀字节被静默改动（未证实——无线上指纹）'],
  // clauses / short aliases (after the full sentences above)
  ['the wire bytes were byte-identical to the previous round', '本轮发出的字节与上一轮逐字节相同'],
  ['only the body past the static prefix was not read back', '仅静态前缀之后的正文未被读回'],
  ['whole prefix not reused', '整段前缀未被复用'],
  ['UNPROVEN — no wire fingerprint', '未证实——无线上指纹'],
  ['server-side cache miss — PROVEN', '服务端缓存未命中——已实证'],
  ['likely server-side miss or TTL expiry', '疑似服务端未命中或 TTL 过期'],
  ['likely server-side miss, TTL expiry, or a silent prefix byte change', '疑似服务端未命中、TTL 过期，或前缀字节被静默改动'],
  ['likely server-side cache miss', '疑似服务端缓存未命中'],
  ['UNPROVEN', '未证实'],
  ['PROVEN', '已实证'],
  // The named-culprit suffix appended to prefix_mutation causes:
  //   "… [changed: user:abc.content, tool_result(c1).tool_result]"
  ['likely cause of the miss', '很可能就是本次未命中的原因'],
  ['changed: ', '改动: '],
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

/** Resolve a backend cause string to the CURRENT UI language.
 *
 * The backend (lib/tasks_pkg/cache_tracking) emits the cause as free-form
 * ENGLISH. On an English UI we therefore return it verbatim; on a Chinese UI
 * we substring-rewrite the known English fragments to their Chinese
 * equivalents (_CACHE_CAUSE_PHRASES). The phrase map is the 'zh' side of the
 * translation — it is applied ONLY on the zh path so an English string is
 * never wrongly Sinicized. */
function _translateCacheCause(s) {
  let out = String(s || '');
  if (_i18nLang !== 'zh') return out;  // English UI: backend string is already English
  for (const [en, zh] of _CACHE_CAUSE_PHRASES) {
    if (out.includes(en)) out = out.split(en).join(zh);
  }
  return out;
}

// SVG glyphs (no emoji — see CLAUDE.md §3.4): key icon for the API-key slot,
// warning triangle for the cache-invalidation line.
const _CP_KEY_SVG = '<svg class="cp-ico" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="7.5" cy="15.5" r="4.5"/><path d="M10.7 12.3 21 2"/><path d="m16 6 3 3"/><path d="m13 9 3 3"/></svg>';
const _CP_WARN_SVG = '<svg class="cp-ico" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>';

/** Render the human-readable cache-break reason for one round, or ''.
 *
 * Display-only: the backend (lib/tasks_pkg/cache_tracking) is the single
 * source of truth for the CAUSE. For keys that carry a descriptive value
 * (server_side / no_cache_reuse / prefix_mutation) we render that backend
 * cause VERBATIM (translated word-for-word) and do NOT prepend a fixed
 * Chinese label — a hard label like '服务端缓存失效' used to CONTRADICT a
 * cause string that says eviction is unlikely. Flag-only keys
 * (system_prompt / tools / model / message_count) keep their concrete
 * label since the backend sends no free-form text for them. */
function _cacheBreakReason(cb) {
  if (!cb || typeof cb !== 'object') return '';
  const bits = [];
  for (const k of Object.keys(cb)) {
    const val = cb[k];
    if (k === 'server_side' || k === 'no_cache_reuse' || k === 'prefix_mutation') {
      // Render the backend's own cause string verbatim (translated). No
      // fixed label that could contradict it.
      if (val) bits.push(escapeHtml(_translateCacheCause(val)));
    } else if (k === 'model' || k === 'message_count') {
      bits.push(t('finishInfo.cbWithVal', { label: t('finishInfo.cb.' + k), val: escapeHtml(String(val)) }));
    } else if (k === 'tools' && typeof val === 'string' && val.includes('changed:')) {
      bits.push(`${t('finishInfo.cb.tools')}: ${escapeHtml(val.replace('changed:', '').trim())}`);
    } else if (_CACHE_BREAK_KEYS.has(k)) {
      bits.push(t('finishInfo.cb.' + k));
    } else if (typeof val === 'string' && val) {
      // Unknown/future key: show the backend value verbatim rather than
      // fabricating a label from the raw dict key.
      bits.push(escapeHtml(_translateCacheCause(val)));
    }
    // Unknown key with no descriptive value → omit (don't invent a cause).
  }
  return bits.join(t('finishInfo.listSep'));
}

/** Classify a cache-break dict into ONE of three user-facing fault states so
 * the popover can visually distinguish them (the whole point of the 2026-07
 * traceability upgrade — the user must SEE whose fault the miss was):
 *   'culprit' — WE mutated the cached prefix; the cause names the exact
 *               msg.field. Actionable, our fault. (prefix_mutation key, OR any
 *               client-side change: system_prompt/tools/model/message_count.)
 *   'proven'  — the wire bytes were PROVEN byte-identical → genuinely
 *               server-side, NOT our fault, nothing to fix.
 *   'unproven'— server-side is only a guess (no wire fingerprint captured).
 *   ''        — no break.
 * Keyed off the backend cause text so it can never drift from what
 * _cacheBreakReason renders. */
function _cacheBreakState(cb) {
  if (!cb || typeof cb !== 'object') return '';
  const keys = Object.keys(cb);
  if (!keys.length) return '';
  // A named prefix mutation, or any concrete client-side change, is OUR fault.
  if ('prefix_mutation' in cb) return 'culprit';
  if (keys.some(k => k === 'system_prompt' || k === 'tools'
                  || k === 'model' || k === 'message_count')) return 'culprit';
  // Otherwise inspect the server_side / no_cache_reuse cause text.
  const txt = String(cb.server_side || cb.no_cache_reuse || '');
  if (txt.includes('PROVEN') && !txt.includes('UNPROVEN')) return 'proven';
  if (txt.includes('UNPROVEN')) return 'unproven';
  // A cause we can't classify (legacy string) → treat as unproven guess.
  return txt ? 'unproven' : '';
}

/** Extract the named culprit list from a prefix_mutation cause, or ''.
 * Backend appends "[changed: key.field, …]" — pull it out so the popover can
 * show WHICH message broke cache, front-and-center, not buried in the sentence.
 */
function _cacheBreakCulprits(cb) {
  if (!cb || typeof cb !== 'object') return '';
  const txt = String(cb.prefix_mutation || cb.no_cache_reuse || cb.server_side || '');
  const m = txt.match(/\[changed:\s*([^\]]+)\]/);
  return m ? m[1].trim() : '';
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
    html += `<div class="cp-section-title">${escapeHtml(t('finishInfo.apiRoundsTitle', { n: numRounds }))}</div>`;
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
      const _body = _parts.join(t('finishInfo.listSepDot'));
      return _sum > 0 ? `${_body}${t('finishInfo.metaSum', { v: fmt(_sum) })}` : _body;
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
        if (_wb.prevOutput > 0)   _terms.push(t('finishInfo.wbPrevOutput', { v: fmt(_wb.prevOutput) }));
        if (_wb.toolResults > 0)  _terms.push(t('finishInfo.wbToolResults', { v: fmt(_wb.toolResults) }));
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
      }
      if (!_wbShown && _inflowMeta.length) {
        // Legacy fallback (rounds persisted before writeBreakdown shipped):
        // just name the previous round's tool results that flowed in. No `=`,
        // no fake equation — these local counts don't equal write.
        const _inflowStr = _fmtToolMeta(_inflowMeta);
        const _tip = t('finishInfo.inflowTip', { n: _inflowMeta.length });
        html += `<div class="cp-round-act cp-round-inflow" title="${escapeHtml(_tip)}">${escapeHtml(t('finishInfo.inflowLabel', { detail: _inflowStr }))}</div>`;
      }
      // ★ Activity line: what the model DID this round (the tool calls it
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
        html += `<div class="cp-round-break${_stCls}">${_CP_WARN_SVG}${_stLabel}${t('finishInfo.cacheBreakLabel', { reason: cbReason })}</div>`;
        // The named culprit — WHICH message(s) broke cache — surfaced on its
        // own line so the user can act on it, not hunt error.log.
        if (cbCulprits) {
          html += `<div class="cp-break-culprit">${t('finishInfo.cbCulpritLabel', { culprits: escapeHtml(cbCulprits) })}</div>`;
        }
      }
      html += `</div>`;
    });
    html += `</div>`;
    // ★ Legend — explains the cache/write/→ semantics so a user isn't
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
    html += `<div class="cp-taskid-row" title="${escapeHtml(t('finishInfo.taskIdTip', { id: taskId }))}" onclick="event.stopPropagation();_safeClipboardWrite('${_tidSafe}');this.classList.add('cp-copied')">Task ID: <span class="cp-taskid-val">${escapeHtml(taskId)}</span></div>`;
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
      parts.push(`<span class="finish-tag err">✕ ${escapeHtml(t('finishInfo.reasonError'))}</span>`);
    } else if (msg.finishReason === "aborted") {
      parts.push(`<span class="finish-tag warn">${escapeHtml(t('finishInfo.reasonStopped'))}</span>`);
    } else if (msg.finishReason === "interrupted") {
      parts.push(`<span class="finish-tag warn"><span title="${escapeHtml(t('finishInfo.reasonInterruptedTip'))}">${escapeHtml(t('finishInfo.reasonInterrupted'))}</span></span>`);
    } else if (msg.finishReason === "server_offline") {
      parts.push(
        `<span class="finish-tag err"><span title="${escapeHtml(t('finishInfo.reasonServerOfflineTip'))}">${escapeHtml(t('finishInfo.reasonServerOffline'))}</span></span>` +
        ` <button class="finish-reconnect-btn" onclick="_recoverOfflineConversations('manual_button')" ` +
        `title="${escapeHtml(t('finishInfo.reconnectTip'))}" style="` +
        `font-size:11px;padding:1px 8px;margin-left:4px;cursor:pointer;` +
        `background:var(--accent);color:#fff;border:none;border-radius:4px;` +
        `vertical-align:middle;opacity:0.9` +
        `">${Icon('refresh', 12)} ${escapeHtml(t('finishInfo.reconnect'))}</button>`
      );
    } else {
      const labels = {
        length: escapeHtml(t('finishInfo.reasonTruncated')),
        tool_use: escapeHtml(t('finishInfo.reasonTool')),
        tool_calls: escapeHtml(t('finishInfo.reasonTool')),
        content_filter: "<span title='" + t('msg.contentFiltered') + "'>" + escapeHtml(t('finishInfo.reasonFiltered')) + "</span>",
        tool_rounds_exhausted: escapeHtml(t('finishInfo.reasonToolLimit')),
        max_tokens: escapeHtml(t('finishInfo.reasonTruncated')),
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
        // Only assert a cause when the backend actually supplied one. If
        // every broken round came back with an empty reason, state the
        // round count plainly instead of inventing '未复用缓存'.
        const _tip = _reasons.length
          ? t('finishInfo.cacheBreakSummary', { n: _brokenRounds.length, reasons: _reasons.join(t('finishInfo.listSepSemi')) })
          : t('finishInfo.cacheBreakSummaryPlain', { n: _brokenRounds.length });
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
        `<span class="finish-tag" style="cursor:pointer;opacity:0.6;font-size:0.8em" title="${escapeHtml(t('finishInfo.traceCopyTip', { ids: _allStr }))}" onclick="_safeClipboardWrite('${_jsSafe}');this.textContent='copied'">${escapeHtml(_lastTrace.slice(0,8))}</span>`
      );
    }
  }
  if (msg.fallbackModel) {
    const _fbReason = msg.fallbackReason || msg.fallbackKind || "";
    const _reasonLine = _fbReason
      ? t('finishInfo.fallbackReason', { reason: _fbReason })
      : "";
    const _tip = t('finishInfo.fallbackTip', { from: msg.fallbackFrom || "?", to: msg.fallbackModel, reason: _reasonLine });
    parts.push(
      `<span class="finish-tag warn" title="${escapeHtml(_tip)}">${escapeHtml(t('finishInfo.fallbackTag'))} → ${escapeHtml(msg.fallbackModel)}</span>`,
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
//
// ★ ISOLATION BY CONSTRUCTION (not by hash). The derived list is cached
//   ON THE OWNING MESSAGE OBJECT via a WeakMap — a message can only ever
//   read ITS OWN entry, so a stale/colliding fingerprint can never surface
//   another conversation's file list. (Derivation itself is backend
//   single-source-of-truth: lib/tool_changes.py via
//   POST /api/v1/messages/extract-file-changes — the frontend never
//   re-parses toolRounds.) The stored fp is only a per-message STALENESS
//   token: "is the list I cached still the one this message's toolRounds
//   would produce?". The pending map is a global keyed by fp PURELY to
//   dedup concurrent identical in-flight requests — it holds Promises, is
//   never read as a message's rendered result, and identical inputs yield
//   identical output, so coalescing is safe and leak-free.
const _fcResultByMsg = new WeakMap();          // msg → { fp, files }
let _extractedFileChangesPending = new Map();   // fp → Promise<files[]> (dedup only)

// Tools whose rounds carry file-change info (mirror of
// lib/tool_changes.py `_WRITE_TOOLS` + the separately-handled run_command).
const _FC_FP_WRITE_TOOLS = new Set([
  'write_file', 'apply_diff', 'apply_diffs',
  'insert_content', 'insert_contents', 'run_command',
]);

function _fcFingerprint(toolRounds) {
  if (!toolRounds || !toolRounds.length) return '';
  // ★ CONTENT-FAITHFUL fingerprint = a per-message STALENESS token: "does
  //   the file list I cached on this message still match what its current
  //   toolRounds would produce?". The authoritative cache is now
  //   owner-scoped (`_fcResultByMsg` WeakMap), so this fp is NOT a global
  //   cross-conversation cache key anymore — it also serves as the dedup
  //   key for coalescing identical in-flight requests. Either way the
  //   invariant is `same fingerprint ⟺ same extractor inputs ⟹ same
  //   output`, so it must reflect EVERY field lib/tool_changes.py
  //   consumes, across ALL rounds — not just the last. (Historically a
  //   coarse key + a global content-keyed cache let two same-shape
  //   messages in different conversations alias each other's file list.)
  //
  //   Projected fields only (NOT the whole toolArgs — write_file's args
  //   carry the entire file `content`, which must never bloat a cache
  //   key): the path / edits[].path the extractor reads from toolArgs,
  //   and results[0]'s fileChanges / writeOk / badge / title.
  const parts = [];
  for (let i = 0; i < toolRounds.length; i++) {
    const r = toolRounds[i];
    if (!r) continue;
    const tn = r.toolName || '';
    if (!_FC_FP_WRITE_TOOLS.has(tn)) continue;
    let args = r.toolArgs;
    if (typeof args === 'string') {
      try { args = JSON.parse(args); } catch (_) { args = null; }
    }
    const argPaths = [];
    if (args && typeof args === 'object') {
      if (Array.isArray(args.edits)) {
        for (const e of args.edits) if (e && e.path) argPaths.push(e.path);
      }
      if (args.path) argPaths.push(args.path);
    }
    const res0 = r.results && r.results[0];
    let resSig = '';
    if (res0 && typeof res0 === 'object') {
      const fcPaths = Array.isArray(res0.fileChanges)
        ? res0.fileChanges
            .map(fc => (fc && fc.path ? fc.path + '>' + (fc.action || '') : ''))
            .join(',')
        : '';
      resSig = fcPaths + '\u00a7' + (res0.writeOk === false ? '0' : '1') +
               '\u00a7' + (res0.badge || '') + '\u00a7' + (res0.title || '');
    }
    parts.push(i + '\u00a7' + tn + '\u00a7' + (r.status || '') +
               '\u00a7' + argPaths.join(',') + '\u00a7' + resSig);
  }
  // No write-capable rounds → both messages extract to [] — sharing a
  // cache entry is correct (identical empty output). Distinct suffix keeps
  // it separate from the "has writes" keyspace.
  return toolRounds.length + '|' + parts.join('~');
}

async function _extractFileChangesFromRoundsAsync(toolRounds, msg) {
  if (!toolRounds || !toolRounds.length) return [];
  const fp = _fcFingerprint(toolRounds);
  // Per-message cache hit (owner-scoped, staleness-checked).
  if (msg) {
    const owned = _fcResultByMsg.get(msg);
    if (owned && owned.fp === fp) return owned.files;
  }
  // Coalesce identical concurrent requests (dedup only — the Promise is
  // never a message's rendered result; it just avoids duplicate POSTs).
  const inflight = _extractedFileChangesPending.get(fp);
  const store = (files) => {
    // Only write the result onto the message that ASKED for it, and only
    // if its toolRounds still match the fp we fetched for (guards against a
    // reused/mutated message object). No global content-keyed store, so no
    // cross-conversation aliasing is possible.
    if (msg && _fcFingerprint(msg.toolRounds) === fp) {
      _fcResultByMsg.set(msg, { fp, files });
    }
    return files;
  };
  if (inflight) return inflight.then(store);

  const promise = (async () => {
    try {
      const body = await Api.conversations.extractFileChanges(toolRounds);
      if (!body) {
        if (typeof debugLog === 'function') {
          debugLog('[FileChanges] extract failed', 'warn');
        }
        return [];
      }
      return Array.isArray(body.files) ? body.files : [];
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
  return promise.then(store);
}

/**
 * Batch-prefetch file-change lists for every message in a conversation
 * that needs server-side derivation (i.e. lacks `modifiedFileList`).
 * Writes each result onto its OWNING message (the `_fcResultByMsg`
 * WeakMap) so the synchronous render paths inside `renderFileChangesBar`
 * hit the per-message cache instead of firing one POST per message.
 *
 * Returns a Promise<boolean>: true if fresh entries landed (caller
 * should force a re-render), false if everything was already cached.
 */
async function _prefetchConvFileChanges(conv) {
  if (!conv || !conv.messages || !conv.messages.length) return false;
  const items = [];
  const fps = [];
  const owners = [];          // fps[i] → [msg, ...] messages sharing that fp
  const _fpIndex = new Map(); // fp → position in fps/items/owners
  for (const m of conv.messages) {
    if (!m || m.modifiedFileList && m.modifiedFileList.length) continue;
    if (!Array.isArray(m.toolRounds) || !m.toolRounds.length) continue;
    const fp = _fcFingerprint(m.toolRounds);
    if (!fp) continue;
    // Already resolved for THIS message (owner-scoped, staleness-checked)?
    const owned = _fcResultByMsg.get(m);
    if (owned && owned.fp === fp) continue;
    // Coalesce identical requests within this batch — one POST per fp,
    // fanned out to every owning message afterward.
    let idx = _fpIndex.get(fp);
    if (idx === undefined) {
      idx = fps.length;
      _fpIndex.set(fp, idx);
      fps.push(fp);
      items.push({ toolRounds: m.toolRounds });
      owners.push([]);
    }
    owners[idx].push(m);
  }
  if (!items.length) return false;
  try {
    const body = await Api.conversations.extractFileChangesBatch(items);
    const results = body && Array.isArray(body.results) ? body.results : [];
    for (let i = 0; i < fps.length; i++) {
      const files = Array.isArray(results[i]) ? results[i] : [];
      const fp = fps[i];
      // Write onto each owning message, re-checking its current fp so a
      // mutation between request and response never mis-stamps a message.
      for (const m of owners[i]) {
        if (_fcFingerprint(m.toolRounds) === fp) {
          _fcResultByMsg.set(m, { fp, files });
        }
      }
    }
    return true;
  } catch (err) {
    if (typeof debugLog === 'function') {
      debugLog(`[FileChanges] batch prefetch failed: ${err && err.message}`, 'warn');
    }
    return false;
  }
}
window._prefetchConvFileChanges = _prefetchConvFileChanges;

/**
 * Synchronous accessor — returns the cached file-change list for a
 * message, or `null` if not yet fetched (or stale). Owner-scoped: reads
 * ONLY this message's own WeakMap entry, and only if the stored
 * fingerprint still matches its current toolRounds. Render paths read
 * this to render the bar on the SAME tick the rounds change, then the
 * async fetch (kicked off in parallel) refreshes it for the NEXT tick.
 */
function _extractFileChangesFromRoundsCached(msg) {
  if (!msg || !Array.isArray(msg.toolRounds) || !msg.toolRounds.length) return null;
  const owned = _fcResultByMsg.get(msg);
  if (!owned) return null;
  return owned.fp === _fcFingerprint(msg.toolRounds) ? owned.files : null;
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
  const cached = _extractFileChangesFromRoundsCached(msg);
  if (cached !== null) {
    if (!cached.length) return '';
    return _renderFileChangesHtml(cached, false, msgIdx);
  }
  // No cached entry yet — kick off an async fetch and trigger a re-render
  // when it lands. Returning empty for THIS render tick is fine; the bar
  // appears on the next tick.
  _extractFileChangesFromRoundsAsync(msg.toolRounds, msg).then(() => {
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
  if (okCount > 0) summaryParts.push(t('fileChanges.filesChanged', { n: okCount, s: okCount > 1 ? 's' : '' }));
  if (pendingCount > 0) summaryParts.push(t('fileChanges.inProgress', { n: pendingCount }));
  if (failCount > 0) summaryParts.push(t('fileChanges.failed', { n: failCount }));
  const summaryText = summaryParts.join(t('fileChanges.summarySep'));
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
    // Localize the backend action verb; unknown/future actions render verbatim
    // (t() would otherwise echo the missing key string).
    const _actKey = 'fileChanges.action.' + f.action;
    const _actLabel = (typeof _i18n !== 'undefined' && _i18n[_actKey]) ? t(_actKey) : f.action;
    return `<div class="fc-file${f.ok ? '' : ' fc-file-err'}${pendingCls}" title="${escapeHtml(fullPath)}">
      ${actionIcon(f.action, f.ok, f.pending)}
      <span class="fc-path">${rootPrefix}<span class="fc-dir">${escapeHtml(dir)}</span><span class="fc-fname">${escapeHtml(fname)}</span></span>
      <span class="fc-action">${escapeHtml(_actLabel)}${countBadge}</span>
    </div>`;
  }).join('');

  // ★ Undo button — only for finalized (non-streaming) messages with a valid msgIdx
  const undoBtn = (!isStreaming && typeof msgIdx === 'number')
    ? `<button class="fc-undo-btn" onclick="event.stopPropagation();undoConvModifications(${msgIdx})" title="${escapeHtml(t('fileChanges.undoTip'))}">` +
      `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7v6h6"/><path d="M21 17a9 9 0 0 0-15-6.7L3 13"/></svg>` +
      `<span>${escapeHtml(t('fileChanges.undo'))}</span></button>`
    : '';

  // ★ Undo All button — always available as a separate interaction point
  const undoAllBtn = (!isStreaming && typeof msgIdx === 'number')
    ? `<button class="fc-undo-all-btn" onclick="event.stopPropagation();undoAllModifications()" title="${escapeHtml(t('fileChanges.undoAllTip'))}">` +
      `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7v6h6"/><path d="M21 17a9 9 0 0 0-15-6.7L3 13"/><line x1="12" y1="7" x2="12" y2="3"/><line x1="8" y1="7" x2="12" y2="7"/></svg>` +
      `<span>${escapeHtml(t('fileChanges.undoAll'))}</span></button>`
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

