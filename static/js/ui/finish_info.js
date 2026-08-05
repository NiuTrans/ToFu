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
  // ── CURRENT verdict wording ACTUALLY emitted today by
  //    _detect.py:_resolve_break_cause (2026-07). These MUST stay at the TOP
  //    of the array: several contain the word "endpoint", and the namespace
  //    block below adds a bare ['endpoint', …] clause — a full sentence has to
  //    match BEFORE that short alias can mangle it. The legacy "most likely …
  //    shared cache pool" rows further down are kept only for old persisted
  //    rounds. FULL sentences precede their shorter substrings. Guarded by
  //    tests/test_frontend_cache_verdict_render.py, which DERIVES these
  //    strings from the backend fn so any future drift turns the test red. ──
  ['The routing was also identical (key + anthropic-beta + endpoint all match last round), so this is not a client cache-namespace switch either.',
   '本轮的路由也完全相同（key、anthropic-beta 头、endpoint 均与上一轮一致），因此也不是客户端切换了缓存命名空间。'],
  ['The cached prefix was not reused upstream: an upstream cache miss (a per-request gateway miss or a TTL boundary).',
   '缓存前缀未在上游被复用：一次上游缓存未命中（网关偶发的单次未命中，或 TTL 边界）。'],
  ['The whole cached prefix was not reused upstream: an upstream cache miss (a per-request gateway miss or a TTL boundary).',
   '整段缓存前缀未在上游被复用：一次上游缓存未命中（网关偶发的单次未命中，或 TTL 边界）。'],
  ['Only the body past the static prefix was not read back.',
   '仅静态前缀之后的正文未被读回。'],
  ['an upstream cache miss (a per-request gateway miss or a TTL boundary)',
   '一次上游缓存未命中（网关偶发的单次未命中，或 TTL 边界）'],
  // ── Breakpoint-lost / byte-divergence / backend-history-rewrite verdicts
  //    (client-side culprits — also emitted today, previously untranslated). ──
  ['cache breakpoint lost between turns (a cache_control marker the client placed did not survive to the wire — e.g. dropped in the tool_result translation) — the body past the last surviving marker was re-billed uncached',
   '缓存断点在轮次间丢失（客户端放置的某个 cache_control 标记没能送到线上——例如在 tool_result 转换中被丢掉）——最后一个存活标记之后的正文按未命中重新计费'],
  ['hoisted system/tools bytes changed between turns while the lossy system fingerprint matched — a canonical-invisible change in the per-turn-injected system prefix (block reorder, wrapping flip, re-serialization, or tool-param key reorder) altered the exact bytes the gateway caches on → the cached prefix was re-billed uncached',
   '被上提的 system/tools 段字节在轮次间发生变化，而有损的 system 指纹却仍显示一致——每轮注入的 system 前缀里发生了一个规范化不可见的改动（块重排、包裹翻转、重新序列化，或工具参数键重排），改变了网关据以缓存的确切字节 → 缓存前缀按未命中重新计费'],
  ['wire bytes changed between turns while the lossy content fingerprint matched — a canonical-invisible change (reasoning_details rebuild, consecutive same-role merge, JSON field reorder, or an OpenAI↔Anthropic envelope/endpoint switch) altered the exact bytes the gateway caches on → the affected prefix was re-billed uncached',
   '线上字节在轮次间发生变化，而有损的内容指纹却仍显示一致——一个规范化不可见的改动（reasoning_details 重建、相邻同角色合并、JSON 字段重排，或 OpenAI↔Anthropic 信封/endpoint 切换）改变了网关据以缓存的确切字节 → 受影响的前缀按未命中重新计费'],
  ['backend history rewrite (reconcile / committed-dict projection) edited or deleted a cached message — the prefix was re-billed uncached',
   '后端历史重写（reconcile / committed-dict 投影）改写或删除了一条已缓存的消息——前缀按未命中重新计费'],
  ['the body past the last surviving marker was re-billed uncached', '最后一个存活标记之后的正文按未命中重新计费'],
  ['altered the exact bytes the gateway caches on', '改变了网关据以缓存的确切字节'],
  ['the affected prefix was re-billed uncached', '受影响的前缀按未命中重新计费'],
  ['the cached prefix was re-billed uncached', '缓存前缀按未命中重新计费'],
  ['the prefix was re-billed uncached', '前缀按未命中重新计费'],
  // ── Cache-NAMESPACE switch (2026-07: byte-identical body, routing flipped —
  //    upstream key / anthropic-beta / endpoint. A CLIENT-side cold-namespace
  //    miss, NOT a server fault. See _detect.py:_resolve_break_cause. FULL
  //    sentence first, then the flipped-attribute sub-names as clauses.) ──
  ['same prefix bytes routed to a different cache namespace — the ',
   '相同的前缀字节被路由到了不同的缓存命名空间——'],
  ['changed between turns (a client-side dispatch rebind on cooldown/429, or a per-task TTL-latch flip), so the byte-identical prefix landed on a COLD gateway cache and was re-billed uncached. NOT a server-side miss.',
   '在轮次间发生了变化（客户端在冷却/429 时重新选路，或 per-task 的 TTL 锁存翻转），于是逐字节相同的前缀落到了一个冷的网关缓存上、按未命中重新计费。这不是服务端未命中。'],
  ['anthropic-beta header (e.g. extended-cache-ttl)', 'anthropic-beta 头（如 extended-cache-ttl）'],
  ['upstream API key', '上游 API key'],
  ['endpoint', 'endpoint（服务端点）'],
  // ── Byte-identical upstream-miss verdict (2026-07: byte-identical prefix NOT
  //    read back — see lib/tasks_pkg/cache_tracking/_detect.py). Byte-identity
  //    proves the miss is NOT a client prefix change THIS round; it does NOT
  //    assert a confident cause (an ordinary gateway miss, a TTL boundary, or
  //    shared-pool contention are all possible). Most misses in this system
  //    are instead CLIENT-side (bytes re-serialized across turns) and are
  //    named per-field elsewhere. FULL sentences precede clauses. ──
  ['prefix not read back though the wire bytes were byte-identical to the previous round — so this round is NOT a client-side prefix change. The cached prefix was not reused upstream: most likely an upstream cache miss (a per-request gateway miss, a TTL boundary, or contention in this key\'s shared cache pool when several large prefixes are active at once). Only the body past the static prefix was not read back. (Most misses in this system are instead client-side and are named per-field above; this is not that class.)',
   '前缀未被读回，尽管本轮发出的字节与上一轮逐字节相同——因此本轮不是客户端的前缀改动。缓存前缀未在上游被复用：很可能是一次上游缓存未命中（网关偶发的单次未命中、TTL 边界，或该 key 的共享缓存池在多个大前缀同时活跃时的争抢）。仅静态前缀之后的正文未被读回。（本系统里大多数未命中其实是客户端引起的，并已在上方逐字段点名；本条不属于那一类。）'],
  ['prefix not read back though the wire bytes were byte-identical to the previous round — so this round is NOT a client-side prefix change. The whole cached prefix was not reused upstream: most likely an upstream cache miss (a per-request gateway miss, a TTL boundary, or contention in this key\'s shared cache pool when several large prefixes are active at once). (Most misses in this system are instead client-side and are named per-field above; this is not that class.)',
   '前缀未被读回，尽管本轮发出的字节与上一轮逐字节相同——因此本轮不是客户端的前缀改动。整段缓存前缀未在上游被复用：很可能是一次上游缓存未命中（网关偶发的单次未命中、TTL 边界，或该 key 的共享缓存池在多个大前缀同时活跃时的争抢）。（本系统里大多数未命中其实是客户端引起的，并已在上方逐字段点名；本条不属于那一类。）'],
  ['prefix not read back though the wire bytes were byte-identical to the previous round', '前缀未被读回，尽管本轮发出的字节与上一轮逐字节相同'],
  ['most likely an upstream cache miss (a per-request gateway miss, a TTL boundary, or contention in this key\'s shared cache pool when several large prefixes are active at once)', '很可能是一次上游缓存未命中（网关偶发的单次未命中、TTL 边界，或该 key 的共享缓存池在多个大前缀同时活跃时的争抢）'],
  ['Most misses in this system are instead client-side and are named per-field above; this is not that class.', '本系统里大多数未命中其实是客户端引起的，并已在上方逐字段点名；本条不属于那一类。'],
  ['so this round is NOT a client-side prefix change', '因此本轮不是客户端的前缀改动'],
  // ── Legacy byte-identical eviction verdicts (older persisted rounds said
  //    "upstream cache eviction …"; kept so historical messages still translate). ──
  ['upstream cache eviction — bytes were byte-identical to the previous round, so this is NOT a client change and NOT a random server failure: the cached prefix was evicted from the shared cache pool on this key before read (concurrent large prefixes on the same key LRU-evict one another; a prefix below the admission-gate threshold is not held resident). Only the body past the static prefix was not read back',
   '上游缓存被驱逐——本轮字节与上一轮逐字节相同，因此这既不是客户端改动、也不是服务端随机失败：缓存前缀在被读回前，就被同一 key 上的共享缓存池挤出了（同一 key 上多个大前缀并发时会互相 LRU 驱逐；低于准入门槛的前缀不会被驻留保护）。仅静态前缀之后的正文未被读回'],
  ['upstream cache eviction — bytes were byte-identical to the previous round, so this is NOT a client change and NOT a random server failure: the whole cached prefix was evicted from the shared cache pool on this key before read (concurrent large prefixes on the same key LRU-evict one another; a prefix below the admission-gate threshold is not held resident)',
   '上游缓存被驱逐——本轮字节与上一轮逐字节相同，因此这既不是客户端改动、也不是服务端随机失败：整段缓存前缀在被读回前，就被同一 key 上的共享缓存池挤出了（同一 key 上多个大前缀并发时会互相 LRU 驱逐；低于准入门槛的前缀不会被驻留保护）'],
  ['upstream cache eviction — bytes were byte-identical to the previous round', '上游缓存被驱逐——本轮字节与上一轮逐字节相同'],
  ['concurrent large prefixes on the same key LRU-evict one another; a prefix below the admission-gate threshold is not held resident', '同一 key 上多个大前缀并发时会互相 LRU 驱逐；低于准入门槛的前缀不会被驻留保护'],
  ['the cached prefix was evicted from the shared cache pool on this key before read', '缓存前缀在被读回前，就被同一 key 上的共享缓存池挤出了'],
  // ── Legacy wire-fingerprint verdicts (older persisted rounds said
  //    "server-side — PROVEN"; kept so historical messages still translate). ──
  ['server-side cache miss — PROVEN: the wire bytes were byte-identical to the previous round (only the body past the static prefix was not read back)',
   '服务端缓存未命中——已实证：本轮发出的字节与上一轮逐字节相同（仅静态前缀之后的正文未被读回）'],
  ['server-side cache miss — PROVEN: the wire bytes were byte-identical to the previous round (whole prefix not reused)',
   '服务端缓存未命中——已实证：本轮发出的字节与上一轮逐字节相同（整段前缀未被复用）'],
  ['likely server-side cache miss (UNPROVEN — no wire fingerprint; body re-billed, static prefix still cached)',
   '疑似服务端缓存未命中（未证实——无线上指纹；正文重新计费，静态前缀仍命中）'],
  // ── TTL-latch-bypass verdict (2026-07): the stable system/tools
  //    cache_control ttl flipped ("1h" ↔ default) between rounds — a
  //    body rebuild lost the per-task TTL latch → different cache key →
  //    full prefix miss. CLIENT-caused, not server-side. ──
  ['cache TTL marker flipped between turns (the stable system/tools cache_control ttl changed, e.g. "1h" ↔ default — a body rebuild lost the per-task TTL latch and read the live global) — the whole prefix was re-billed under a new cache key',
   '缓存 TTL 标记在轮次间翻转（稳定的 system/tools 段 cache_control 的 ttl 发生变化，如 "1h" ↔ 默认——某次请求体重建丢失了 per-task 的 TTL 锁存、改读了实时全局值）——整段前缀在新的缓存键下被重新计费'],
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
  // typeof-guarded: in pack mode the core bundle excludes i18n.js, so a failed
  // pack load leaves _i18nLang undefined and a BARE read throws here — the
  // exact ReferenceError that killed boot in production. 'zh' matches i18n.js's
  // own default, so the guarded path is behaviour-identical when the pack is
  // present. Enforced by tests/test_i18n_pack_boot_floor.py.
  const lang = (typeof _i18nLang !== 'undefined' && _i18nLang) ? _i18nLang : 'zh';
  if (lang !== 'zh') return out;  // English UI: backend string is already English
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
    if (k === 'server_side' || k === 'no_cache_reuse' || k === 'prefix_mutation'
        || k === 'turn_boundary_rebill') {
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
 *   'eviction'— LEGACY persisted rows only: an older byte-identical verdict
 *               that asserted an upstream eviction. (Also matches legacy
 *               'PROVEN' rows.)
 *   'upstream'— CURRENT dominant real-traffic verdict: the wire bytes were
 *               PROVEN byte-identical to the previous round, so the miss is
 *               NOT our client-side change — an upstream non-reuse we have
 *               cleared ourselves of. NOT an unproven guess; do not apologise.
 *   'unproven'— genuine guess: no wire fingerprint was captured (non-Claude /
 *               capture failure), so server-side is only a possibility.
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
  // ★ Cache-namespace switch (byte-identical body, routing flipped: upstream
  //   key / anthropic-beta / endpoint). A CLIENT-side cold-namespace miss (a
  //   dispatch rebind on cooldown/429, or a per-task TTL-latch flip) — NOT a
  //   server fault. Its own state so the popover shows the actionable routing
  //   switch instead of no badge (the backend already returns the key +
  //   verbatim cause; this just classifies it). Keyed on the dict key, which
  //   is stable regardless of the free-form cause wording.
  if ('cache_namespace_switch' in cb) return 'namespace';
  // ★ Round-1 (new-turn) boundary re-bill (backend detect_cache_break, 2026-07):
  //   the FIRST round of a new user turn read back far less than the previous
  //   turn's warm cached prefix — the prefix was not reused across the turn
  //   boundary and got re-billed. That round is invisible to the detector's
  //   other predicates (they gate on call_count>0), so it used to show NO badge
  //   and look benign — the user-facing half of "too optimistic". Its own state
  //   so the popover surfaces it as a real, client-visible miss (likely a
  //   tail-TTL window boundary; see the tail-TTL ticket). Keyed on the dict key.
  if ('turn_boundary_rebill' in cb) return 'boundary';
  // Otherwise inspect the server_side / no_cache_reuse cause text.
  const txt = String(cb.server_side || cb.no_cache_reuse || '');
  // LEGACY persisted rows: the old 'upstream cache eviction' verdict + the
  // older 'server-side … PROVEN' wording fold into 'eviction' so history
  // renders consistently (never the reassuring teal).
  if (txt.includes('upstream cache eviction')
      || (txt.includes('PROVEN') && !txt.includes('UNPROVEN'))) return 'eviction';
  // ★ CURRENT wire-fingerprint verdict (the DOMINANT real-traffic case): the
  //   post-translation wire bytes were byte-identical to the previous round,
  //   so we PROVED this miss is NOT a client-side prefix change. That is a
  //   POSITIVE proof about our own side — it must NOT be laundered into the
  //   apologetic 'unproven' badge (that read as a cache-miss "excuse"). Give
  //   it its own state: an upstream non-reuse we have cleared ourselves of.
  //   Keep this AFTER the legacy 'upstream cache eviction' check, whose text
  //   ALSO contains "byte-identical to the previous round".
  if (txt.includes('byte-identical to the previous round')
      && txt.includes('NOT a client-side prefix change')) return 'upstream';
  // Only a genuine no-wire-fingerprint fallback (non-Claude / capture failure)
  // remains a true guess.
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

/* ── cost-popover family MOVED to ui/finish_info_rich.js (_DEFERRED_FILES, Epic-E pt_3879f00e sub-8, 2026-08-01) — the popover builds lazily on first open from the _costCtxByMsg stash; _toggleCostPopover is a feature-loader entry point. ── */


// ── Scroll branch panel to bottom ──
/* Per-msg build ctx for the lazily-built cost popover (Epic-E sub-8).
 * renderFinishInfo stashes; the deferred ui/finish_info_rich.js
 * _toggleCostPopover reads on first open. WeakMap keyed by the msg
 * object — entries die with the message, no cleanup needed. */
var _costCtxByMsg = new WeakMap();  /* var (not const): the deferred rich module reads this across the bundle boundary — a top-level var becomes a global-object property reachable from any script/eval scope, while a const only lands in the shared lexical env (invisible to the deferred bundle's eval in the modes harness). */

function renderFinishInfo(msg, isLiveTail) {
  // A LEGITIMATELY-finished turn always carries a terminal signal
  // (finishReason or usage), stamped by the backend terminal sync
  // (build_result_meta → _sync_result_to_conversation). The mid-stream
  // checkpoint (_sync_partial_to_conversation) deliberately writes `model`
  // but WITHHOLDS finishReason/usage until completion.
  //
  // ★ That withholding is ENFORCED, not merely conventional (2026-07-31).
  //   It used to be a one-sided assumption: the orchestrator stamps
  //   task['finishReason'] ~111 lines before it flips task['status']='done'
  //   (lib/tasks_pkg/orchestrator/_finalize.py, the span containing the
  //   blocking _generate_tool_summary call), and every snapshot transport —
  //   /chat/poll (both branches) and the SSE `state` event (both branches) —
  //   copied metadata with a bare truthiness check. So a reconnect landing in
  //   that window received {status:'running', finishReason:'stop'} and this
  //   function painted a settled finish bar on a STILL-GENERATING turn.
  //   All four snapshot builders now route their metadata through
  //   lib/chat/terminal_gate.py::filtered_snapshot_meta, keyed on the status
  //   that same snapshot reports, so a non-terminal snapshot structurally
  //   cannot carry finishReason/usage/preset. `done` events are deliberately
  //   ungated — they ARE the terminal signal.
  const _terminal = msg.finishReason || msg.usage;
  if (!_terminal && !msg.model && !msg.preset && !msg.effort) return "";
  // ★ Premature-finish-bar guard. `model`/`preset`/`effort` alone are set
  //   mid-stream (SSE state/placeholder), so a model-only message rendered
  //   STATICALLY (not the live #streaming-msg bubble) would show a bogus
  //   "finished" bar carrying only the model tag — the symptom seen on a
  //   cross-device / poor-signal reload before SSE reconnects. Suppress it
  //   ONLY for the active running tail (isLiveTail). A finished-but-model-
  //   only message (legacy pre-usage-persistence, or a degenerate empty
  //   completion) is NOT the live tail, so it keeps its bar — no regression.
  // ★ An interrupted stub (interruptedReason stamped by the crash/restart
  //   recovery, no finishReason/usage yet) is NOT a finished turn either:
  //   the same bogus model-only bar would FREEZE into the bubble (no live
  //   stream ever tears it down — the ms43foj3 double-restart incident,
  //   both bubbles frozen with a bare "K kimi-k3" bar).
  if (!_terminal && (isLiveTail || msg.interruptedReason)) return "";
  const parts = [];
  const _mid = msg.model || msg.preset || msg.effort || "";
  const _pid = msg.provider_id || msg.providerId || "";
  const u = msg.usage || {};
  const fmt = (n) => (n >= 1000000 ? (n / 1000000).toFixed(1) + "m" : n >= 1000 ? (n / 1000).toFixed(1) + "k" : n.toString());
  const thk = u.reasoning_tokens || u.thinking_tokens || 0;

  // ★ Model tag — auto-detect brand from model_id
  const depthIcons = { medium: '', high: '', max: '', ultra: '' };
  const depthLabels = { medium: "Med", high: "Hi", max: "Max", ultra: "Ultra" };
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
    /* ★ pt_turn_settlement: the finish tag now reads the SINGLE settlement
     *   verdict (computeTurnSettlement) instead of re-deriving a conclusion
     *   from msg.finishReason independently. finishLabelForSettlement maps the
     *   verdict's outcome/cause to a label kind; the switch renders the SAME
     *   labels/styling/i18n as before (byte-identical output), so the bubble
     *   stays consistent with the verdict by construction. The interrupted
     *   3-way (killed/restart/unknown) reads the verdict's `cause` (which owns
     *   the interruptedReason mapping) instead of sniffing msg.interruptedReason
     *   here. 'fallback' = a reason the verdict deliberately does not classify
     *   (tool_use / tool_calls / a future reason) → the legacy labels map, so
     *   no current label regresses. model=null suffices — outcome/cause are
     *   model-independent (only resume.mode uses the prefill-capability gate,
     *   and the bubble never renders resume.mode). */
    const _tsV = (typeof computeTurnSettlement === 'function') ? computeTurnSettlement(msg, null) : null;
    let _k;
    if (typeof finishLabelForSettlement === 'function' && _tsV) {
      _k = finishLabelForSettlement(_tsV, msg.finishReason).kind;
    } else {
      /* Legacy kind-derivation fallback — used ONLY when core/turn_settlement.js
       * is not loaded (bundle-only module: dev-mode script-tag fallback, or a
       * JSDOM harness that loads finish_info.js alone). Derives the SAME label
       * kind from msg.finishReason that the verdict would, so the bubble stays
       * correct in EVERY context; in the browser the bundle always provides
       * the verdict (it loads before finish_info.js). This is a defensive
       * fallback, not the primary path — the verdict remains the SSOT. */
      const _fr = msg.finishReason;
      if (_fr === 'stop' || _fr === 'end_turn' || _fr === 'stop_sequence') _k = 'ok';
      else if (_fr === 'error') _k = 'error';
      else if (_fr === 'aborted') _k = 'stopped';
      else if (_fr === 'interrupted') {
        const _ir = msg.interruptedReason;
        _k = (_ir === 'killed') ? 'interruptedKilled'
           : (_ir === 'manual') ? 'interruptedRestart' : 'interruptedUnknown';
      }
      else if (_fr === 'incomplete') _k = 'incomplete';
      else if (_fr === 'server_offline') _k = 'serverOffline';
      else _k = 'fallback';
    }
    if (_k === 'ok') {
      parts.push(`<span class="finish-tag ok">✓</span>`);
    } else if (_k === 'error') {
      parts.push(`<span class="finish-tag err">✕ ${escapeHtml(t('finishInfo.reasonError'))}</span>`);
    } else if (_k === 'stopped') {
      parts.push(`<span class="finish-tag warn">${escapeHtml(t('finishInfo.reasonStopped'))}</span>`);
    } else if (_k === 'interruptedKilled' || _k === 'interruptedRestart' || _k === 'interruptedUnknown') {
      // The verdict's cause owns the interruptedReason → honest-label mapping:
      //   killed → unclean kill (SIGKILL/OOM/crash); restart → controlled
      //   restart (interruptedReason='manual'); unknown → legacy/first_boot.
      const _m = {
        interruptedKilled: ['finishInfo.reasonInterruptedKilled', 'finishInfo.reasonInterruptedKilledTip'],
        interruptedRestart: ['finishInfo.reasonInterruptedRestart', 'finishInfo.reasonInterruptedRestartTip'],
        interruptedUnknown: ['finishInfo.reasonInterruptedUnknown', 'finishInfo.reasonInterruptedUnknownTip'],
      }[_k];
      parts.push(`<span class="finish-tag warn"><span title="${escapeHtml(t(_m[1]))}">${escapeHtml(t(_m[0]))}</span></span>`);
    } else if (_k === 'incomplete') {
      // An autonomous loop (endpoint / autopilot) cut off by a safety cap —
      // the objective is UNVERIFIED. Flag it, not a silent clean ✓.
      parts.push(`<span class="finish-tag warn"><span title="${escapeHtml(t('finishInfo.reasonIncompleteTip'))}">${Icon('alertTriangle', 12)} ${escapeHtml(t('finishInfo.reasonIncomplete'))}</span></span>`);
    } else if (_k === 'serverOffline') {
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
      // truncated / toolLimit / filtered / abnormal / gateway / fallback —
      // rendered via the shared labels map (the exact current HTML + the
      // warnReasons class for these reasons).
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
      const warnReasons = [
        "length",
        "tool_rounds_exhausted",
        "max_tokens",
        "content_filter",
        "premature_close",
        "abnormal_stop",
      ];
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
      /* Epic-E sub-8: the popover builds LAZILY on first open — the
       * builder lives in DEFERRED ui/finish_info_rich.js and the
       * feature-loader stub on _toggleCostPopover loads it on click.
       * Stash the build ctx keyed by the msg object; embed nothing. */
      _costCtxByMsg.set(msg, {
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
        `<span class="cost-popover-data" hidden></span>` +
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
    /* ★ The cause is rendered as VISIBLE text, not only in the hover title.
     *   A tooltip is unreachable on touch and easy to miss on desktop, so a
     *   settled fallback used to read as "回退 → kimi-k3" with no cause at
     *   all — the reason (often an entire upstream HTML error page) was
     *   locked inside `title`. Formatting comes from the shared
     *   core/error_envelope.js helper so this tag and the live streaming
     *   banner can never name the same failure differently. */
    const _fb = (typeof fallbackCauseParts === 'function')
      ? fallbackCauseParts(msg)
      /* core/error_envelope.js is loaded before this file in BOTH the bundle
       * and the index.html script-tag path. The guard is for the isolated
       * eval contexts (dev fallback / JSDOM harnesses that load finish_info.js
       * alone) this file is already required to survive — degrade to the
       * verbatim cause rather than throwing and killing the whole finish bar. */
      : { kindLabel: '', detail: String(msg.fallbackReason || msg.fallbackKind || ''),
          shown: '', hasCause: false };
    const _reasonLine = _fb.detail || _fb.kindLabel
      ? t('finishInfo.fallbackReason', { reason: _fb.detail || _fb.kindLabel })
      : "";
    const _tip = t('finishInfo.fallbackTip', { from: msg.fallbackFrom || "?", to: msg.fallbackModel, reason: _reasonLine });
    parts.push(
      `<span class="finish-tag warn" title="${escapeHtml(_tip)}">${escapeHtml(t('finishInfo.fallbackTag'))} → ${escapeHtml(msg.fallbackModel)}</span>`,
    );
    if (_fb.hasCause) {
      parts.push(
        `<span class="finish-tag warn fb-cause" title="${escapeHtml(_fb.detail || _fb.kindLabel)}">` +
        (_fb.kindLabel ? `<span class="fb-cause-kind">${escapeHtml(_fb.kindLabel)}</span>` : '') +
        (_fb.shown ? `<span class="fb-cause-detail">${escapeHtml(_fb.shown)}</span>` : '') +
        `</span>`,
      );
    }
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
      /* RENDER_CONTRACT L2: move the per-message content version so renderChat's
       * surgical trigger repaints THIS row when the lazy extraction lands
       * (the WeakMap is invisible to _msgFingerprint). Display-only stamp;
       * modifiedFileList stays the authoritative git-backed field. */
      msg._fcResolvedFp = fp;
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
          m._fcResolvedFp = fp;  // RENDER_CONTRACT L2: version signal (see async store)
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
  // ★ Undone round: undo stashed the round's file list (clearing the live
  //   modifiedFiles counter so badges/fingerprints stay honest). Render the
  //   bar in a compact "undone → Redo" state so the action is reversible.
  if (Array.isArray(msg._undoneFileList) && msg._undoneFileList.length) {
    const files = msg._undoneFileList.map(f => ({
      path: f.path, action: f.action, ok: true, count: 1,
      root: f.root || ''
    }));
    return _renderFileChangesHtml(files, false, msgIdx, true);
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
    // Repaint once the file-change cache is fresh. ★ SCROLL FIX: use the
    // scroll-preserving in-place repaint (chat_render.js:_bgRefreshChat), NOT a
    // bare renderChat(conv) — the default (forceScroll=undefined) full render
    // force-scrolls to the bottom, yanking a scrolled-up reader down when a
    // late per-message file-change fetch lands. Fall back to renderChat only
    // where the helper isn't present (defensive; both live in the bundle).
    const conv = (typeof getActiveConv === 'function') ? getActiveConv() : null;
    if (!conv) return;
    if (typeof _bgRefreshChat === 'function') _bgRefreshChat(conv);
    else window.ConvView.replaceAll(conv.id, { forceScroll: false });
  });
  return '';
}

/**
 * Core HTML renderer for file changes bar.
 * @param {Array} files - [{path, action, ok, count}]
 * @param {boolean} isStreaming - if true, add pulse animation
 */
function _renderFileChangesHtml(files, isStreaming, msgIdx, isUndone) {
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
  if (isUndone) {
    summaryParts.push(t('fileChanges.undone', { n: totalFiles, s: totalFiles > 1 ? 's' : '' }));
  } else {
    if (okCount > 0) summaryParts.push(t('fileChanges.filesChanged', { n: okCount, s: okCount > 1 ? 's' : '' }));
    if (pendingCount > 0) summaryParts.push(t('fileChanges.inProgress', { n: pendingCount }));
    if (failCount > 0) summaryParts.push(t('fileChanges.failed', { n: failCount }));
  }
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

  // ★ Redo button — replaces Undo/Undo-All once the round has been undone.
  //   Mirror-image icon (arrow curving the other way) to read as "re-apply".
  const redoBtn = (isUndone && !isStreaming && typeof msgIdx === 'number')
    ? `<button class="fc-redo-btn" onclick="event.stopPropagation();redoConvModifications(_msgElIndex(this))" title="${escapeHtml(t('fileChanges.redoTip'))}">` +
      `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 7v6h-6"/><path d="M3 17a9 9 0 0 1 15-6.7L21 13"/></svg>` +
      `<span>${escapeHtml(t('fileChanges.redo'))}</span></button>`
    : '';

  // ★ Undo button — only for finalized (non-streaming) messages with a valid msgIdx
  const undoBtn = (!isUndone && !isStreaming && typeof msgIdx === 'number')
    ? `<button class="fc-undo-btn" onclick="event.stopPropagation();undoConvModifications(_msgElIndex(this))" title="${escapeHtml(t('fileChanges.undoTip'))}">` +
      `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7v6h6"/><path d="M21 17a9 9 0 0 0-15-6.7L3 13"/></svg>` +
      `<span>${escapeHtml(t('fileChanges.undo'))}</span></button>`
    : '';

  // ★ Undo All button — always available as a separate interaction point
  const undoAllBtn = (!isUndone && !isStreaming && typeof msgIdx === 'number')
    ? `<button class="fc-undo-all-btn" onclick="event.stopPropagation();undoAllModifications()" title="${escapeHtml(t('fileChanges.undoAllTip'))}">` +
      `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7v6h6"/><path d="M21 17a9 9 0 0 0-15-6.7L3 13"/><line x1="12" y1="7" x2="12" y2="3"/><line x1="8" y1="7" x2="12" y2="7"/></svg>` +
      `<span>${escapeHtml(t('fileChanges.undoAll'))}</span></button>`
    : '';

  const actionBtns = (redoBtn || undoBtn || undoAllBtn)
    ? `<div class="fc-actions">${redoBtn}${undoBtn}${undoAllBtn}</div>` : '';

  // Auto-expand for ≤ 5 files so users see details immediately
  const autoExpand = totalFiles <= 5 ? ' fc-expanded' : '';
  const undoneClass = isUndone ? ' fc-undone' : '';
  return `<div class="file-changes-bar${pulseClass}${autoExpand}${undoneClass}" data-fc-count="${totalFiles}">
    <div class="fc-summary" onclick="this.parentElement.classList.toggle('fc-expanded')">
      <span class="fc-summary-icon">${summaryIcon}</span>
      <span class="fc-summary-text">${summaryText}</span>
      ${actionBtns}
      <span class="fc-chevron">›</span>
    </div>
    <div class="fc-details">${fileItems}</div>
  </div>`;
}

