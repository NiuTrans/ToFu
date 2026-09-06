/* ===== migrated source: ui/finish_info.js ===== */
/* ═══════════════════════════════════════════════════════════════════
   finish info — extracted from ui.js (split 2026-05-28)

   renderFinishInfo (usage/cost) + file-changes bar.

   This file is concatenated by Vite's module graph — symbols share
   the same window scope as every other frontend/src/runtime/*.js file. No
   exports / imports needed.
   ═══════════════════════════════════════════════════════════════════ */

// ═══════════════════════════════════════════════════════════════════
// Cost popover — a styled, click-to-open breakdown panel that replaces
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
  // ── Inline-built verdicts (detect_cache_break returns these keys DIRECTLY,
  //    not via _resolve_break_cause, so they were previously missing from the
  //    table and rendered as raw English on the zh UI). Fully-static prose
  //    (no dynamic numbers) — keep the longer compaction variant FIRST. ──
  ['zero read-back on a substantial write, but no break gate could fire because this round followed a compaction, which is structurally exempt from every break predicate. The spend is real and counted here; the CAUSE is unresolved — this round is NOT evidence of a healthy cache.',
   '一次可观的写入零读回，但没有任何断点闸门被触发，因为本轮紧随一次压缩之后——压缩在结构上被所有断点判定豁免。这笔花费是真实的并已计入；但原因未解——本轮不能作为缓存健康的证据。'],
  ['zero read-back on a substantial write, but no break gate could fire. The spend is real and counted here; the CAUSE is unresolved — this round is NOT evidence of a healthy cache.',
   '一次可观的写入零读回，但没有任何断点闸门被触发。这笔花费是真实的并已计入；但原因未解——本轮不能作为缓存健康的证据。'],
  ['mid-history cache anchor drifted past Anthropic\'s ~20-block cache lookback window behind the rolling tail — the tail could not extend the prior cache entry, so the whole prefix past the mid anchor was re-billed uncached even though the body bytes were identical. A client-side breakpoint-layout miss (the stepping-stone trail/step params), NOT a server-side or gateway fault.',
   '历史中间的缓存锚点漂移到了 Anthropic 约 20 块缓存回看窗口之后——尾部无法延续先前的缓存条目，因此即使正文字节完全相同，中间锚点之后的整段前缀仍被按未命中重新计费。这是一次客户端断点布局失误（步进锚点的 trail/step 参数），不是服务端或网关故障。'],
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
  ['provider-bound system/tools structure changed between turns — the client changed cache-key bytes in the hoisted prefix, so the cached prefix was re-billed uncached',
   '发往模型的 system/tools 结构在轮次间发生了变化——客户端改动了被上提前缀中的缓存键字节，因此缓存前缀按未命中重新计费'],
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
  // Structured sibling fields the backend attaches next to the two
  // parameterized verdicts below. They are INPUT to the t() templates, not
  // standalone display lines — skipping them keeps a raw "262000" from leaking
  // into the popover as its own bullet.
  const _meta = new Set(['prev_read', 'read', 'gap_s', 'cold_gap_s']);
  const _num = (v) => {
    const n = Number(v);
    return Number.isFinite(n) ? String(n) : '';
  };
  for (const k of Object.keys(cb)) {
    const val = cb[k];
    if (_meta.has(k)) continue;
    if (k === 'server_side' || k === 'no_cache_reuse' || k === 'prefix_mutation') {
      // Render the backend's own cause string verbatim (translated). No
      // fixed label that could contradict it.
      if (val) bits.push(escapeHtml(_translateCacheCause(val)));
    } else if (k === 'turn_boundary_rebill') {
      // Parameterized i18n when the backend attached structured fields; the
      // free-form string cause is the back-compat fallback for OLD persisted
      // rows that carry only the plain string.
      if (cb.prev_read !== undefined && cb.read !== undefined) {
        bits.push(t('finishInfo.cb.turnBoundaryRebill', {
          prev: _num(cb.prev_read), read: _num(cb.read), gap: _num(cb.gap_s),
        }));
      } else if (val) {
        bits.push(escapeHtml(_translateCacheCause(val)));
      }
    } else if (k === 'cache_write_unsettled') {
      if (cb.cold_gap_s !== undefined) {
        bits.push(t('finishInfo.cb.cacheWriteUnsettled', {
          coldGap: _num(cb.cold_gap_s), gap: _num(cb.gap_s),
        }));
      } else if (val) {
        bits.push(escapeHtml(_translateCacheCause(val)));
      }
    } else if (k === 'codex_cache' && val && typeof val === 'object') {
      // Provider-specific Codex responses-endpoint verdict (stamped by
      // cache_settle via _cache_round_accounting). It is a STRUCTURED dict,
      // not a free-form cause string, so the old "unknown dict key" path
      // silently DROPPED it. Render a label from the status + measured drop.
      if (val.status === 'implicit_breakpoint_fallback') {
        const _drop = Number(val.drop_tokens) || 0;
        bits.push(t('finishInfo.cb.codexCacheFallback', { drop: String(_drop) }));
      } else {
        bits.push(t('finishInfo.cb.codexCacheMiss'));
      }
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

/** Classify cache-break evidence for presentation. Concrete client mutations
 * are culprits; byte-identical proven misses are upstream/legacy eviction;
 * absent wire evidence stays unproven. Keep keys aligned with
 * _cacheBreakReason. */
function _cacheBreakState(cb) {
  if (!cb || typeof cb !== 'object') return '';
  const keys = Object.keys(cb);
  if (!keys.length) return '';
  // A named prefix mutation, or any concrete client-side change, is OUR fault.
  if ('prefix_mutation' in cb) return 'culprit';
  if (keys.some(k => k === 'system_prompt' || k === 'tools'
                  || k === 'model' || k === 'message_count')) return 'culprit';
  // Cache-namespace switch (byte-identical body, routing flipped: upstream
  //   key / anthropic-beta / endpoint). A CLIENT-side cold-namespace miss (a
  //   dispatch rebind on cooldown/429, or a per-task TTL-latch flip) — NOT a
  //   server fault. Its own state so the popover shows the actionable routing
  //   switch instead of no badge (the backend already returns the key +
  //   verbatim cause; this just classifies it). Keyed on the dict key, which
  //   is stable regardless of the free-form cause wording.
  if ('cache_namespace_switch' in cb) return 'namespace';
  // Round-1 (new-turn) boundary re-bill (backend detect_cache_break, 2026-07):
  //   the FIRST round of a new user turn read back far less than the previous
  //   turn's warm cached prefix — the prefix was not reused across the turn
  //   boundary and got re-billed. That round is invisible to the detector's
  //   other predicates (they gate on call_count>0), so it used to show NO badge
  //   and look benign — the user-facing half of "too optimistic". Its own state
  //   so the popover surfaces it as a real, client-visible miss (likely a
  //   tail-TTL window boundary; see the tail-TTL ticket). Keyed on the dict key.
  if ('turn_boundary_rebill' in cb) return 'boundary';
  // Codex responses-endpoint verdict: wire-proven provider behaviour
  // (wire_append_only) — NOT our client change → 'proven' (server-side).
  if ('codex_cache' in cb) return 'proven';
  // Cache-write-settle race (SDK #1451) and mid-anchor-out-of-window are
  // CLIENT-side causes (fast tool loop / breakpoint-layout params) → culprit.
  if ('cache_write_unsettled' in cb) return 'culprit';
  if ('cache_mid_out_of_window' in cb) return 'culprit';
  // Indeterminate: the detector reached NO conclusion (often a compaction
  // zero-read rebuild) — honest 'unproven', not a reassuring no-badge.
  if ('indeterminate' in cb) return 'unproven';
  // Otherwise inspect the server_side / no_cache_reuse cause text.
  const txt = String(cb.server_side || cb.no_cache_reuse || '');
  // LEGACY persisted rows: the old 'upstream cache eviction' verdict + the
  // older 'server-side … PROVEN' wording fold into 'eviction' so history
  // renders consistently (never the reassuring teal).
  if (txt.includes('upstream cache eviction')
      || (txt.includes('PROVEN') && !txt.includes('UNPROVEN'))) return 'eviction';
  // CURRENT wire-fingerprint verdict (the DOMINANT real-traffic case): the
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

/* ── cost-popover family MOVED to ui/finish_info_rich.js (the migrated lazy-module graph, Epic-E  sub-8, 2026-08-01) — the popover builds lazily on first open from the _costCtxByMsg stash; _toggleCostPopover is a feature-bridge entry point. ── */


// ── Scroll branch panel to bottom ──
/* Lazily built cost-popover input keyed by stable Turn identity. The renderer
 * receives this id explicitly; it never re-resolves a positional message. */
var _costCtxByTurnId = new Map();
const _COST_CONTEXT_LIMIT = 500;

function _rememberCostContext(turnId, context) {
  if (!turnId) return;
  // Refresh insertion order for active Turns and keep this presentation cache
  // bounded even when the tab visits thousands of historical conversations.
  _costCtxByTurnId.delete(turnId);
  _costCtxByTurnId.set(turnId, context);
  while (_costCtxByTurnId.size > _COST_CONTEXT_LIMIT) {
    const oldestTurnId = _costCtxByTurnId.keys().next().value;
    if (!oldestTurnId) break;
    _costCtxByTurnId.delete(oldestTurnId);
  }
}

function clearFinishInfoPresentation(turnIds) {
  for (const turnId of turnIds || []) _costCtxByTurnId.delete(turnId);
}

function _quotaPct(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '';
  return (Math.round(n * 10) / 10).toFixed(1).replace(/\.0$/, '');
}

function _quotaWindowLabel(minutes) {
  const n = Number(minutes || 0);
  if (n === 300) return t('quota.window5h');
  if (n === 10080) return t('quota.window7d');
  if (n > 0 && n % 1440 === 0) return t('quota.windowDays', { n: n / 1440 });
  if (n > 0 && n % 60 === 0) return t('quota.windowHours', { n: n / 60 });
  if (n > 0) return t('quota.windowMinutes', { n });
  return t('quota.windowUnknown');
}

function _quotaUsageTokens(usage) {
  if (!usage || typeof usage !== 'object') return 0;
  const total = Number(usage.total_tokens);
  if (Number.isFinite(total) && total >= 0) return total;
  const input = Number(usage.prompt_tokens ?? usage.input_tokens ?? 0);
  const output = Number(usage.completion_tokens ?? usage.output_tokens ?? 0);
  return (Number.isFinite(input) && input > 0 ? input : 0)
    + (Number.isFinite(output) && output > 0 ? output : 0);
}

/** Latest account-wide Codex quota snapshot + observed deltas for this turn.
 *
 * Every apiRound carries the upstream response-header snapshot.  Summing the
 * adjacent-snapshot differences gives the best available per-turn signal, but
 * it remains account-wide (concurrent conversations may move the same
 * counter), so the UI explicitly calls it "observed" rather than exact cost.
 */
function _subscriptionQuotaForMessage(msg) {
  const snapshots = [];
  let turnTokens = 0;
  for (const rd of (msg.apiRounds || [])) {
    turnTokens += _quotaUsageTokens(rd && rd.usage);
    const q = rd && rd.usage && rd.usage._subscription_quota;
    if (q) snapshots.push(q);
  }
  if (!(msg.apiRounds || []).length) turnTokens = _quotaUsageTokens(msg.usage);
  if (!snapshots.length && msg.usage && msg.usage._subscription_quota) {
    snapshots.push(msg.usage._subscription_quota);
    if (!turnTokens) turnTokens = _quotaUsageTokens(msg.usage);
  }
  if (!snapshots.length) return null;
  const latest = snapshots[snapshots.length - 1];
  const deltas = { primary: 0, secondary: 0 };
  const baselines = { primary: false, secondary: false };
  for (const snap of snapshots) {
    for (const name of ['primary', 'secondary']) {
      const win = snap && snap[name];
      if (!win || !win.has_previous_snapshot) continue;
      baselines[name] = true;
      const delta = Number(win.observed_delta_percent);
      if (Number.isFinite(delta) && delta > 0) deltas[name] += delta;
    }
  }
  return { latest, deltas, baselines, turnTokens };
}

/** Wall-clock turn duration: 8.4s → "8.4s", 65s → "1m05s", 75m → "1h15m". */
function _formatTurnDuration(ms) {
  const totalSeconds = Math.max(0, Number(ms) || 0) / 1000;
  if (totalSeconds < 10) {
    return `${(Math.round(totalSeconds * 10) / 10).toFixed(1)}s`;
  }
  const seconds = Math.round(totalSeconds);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remSeconds = seconds % 60;
  if (minutes < 60) return `${minutes}m${String(remSeconds).padStart(2, '0')}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h${String(minutes % 60).padStart(2, '0')}m`;
}




const _waitingBlockPolls = new Map();
function _waitText(key, fallback, values) {
  const value = typeof t === 'function' ? t(key, values) : '';
  return value && value !== key ? value : fallback;
}
function _waitingAgentChips(agents) {
  return (Array.isArray(agents) ? agents : []).map((agent) =>
    `<span class="wait-block-agent-chip"><b>${escapeHtml(String(agent?.role || '?'))}</b> ${escapeHtml(String(agent?.status || '?'))}</span>`).join('');
}
function renderWaitingOnBlock(waitingOn, context) {
  if (!waitingOn || waitingOn.kind !== 'swarm' || !waitingOn.swarmKey) return '';
  const note = waitingOn.autoResume
    ? _waitText('waitBlock.autoResume', 'Will continue automatically when the blocker settles.')
    : _waitText('waitBlock.manualResume', 'Resume manually when the blocker settles.');
  const key = String(waitingOn.swarmKey);
  return `<details class="wait-block" data-wait-swarm-key="${escapeHtml(key)}" data-wait-conv-id="${escapeHtml(context?.conversationId || '')}"><summary><span class="wait-block-icon">${Icon('hourglass', 14)}</span><span class="wait-block-title">${escapeHtml(_waitText('waitBlock.title', 'Waiting on background work'))}</span><span class="wait-block-note">${escapeHtml(note)}</span><span class="wait-block-snapshot">${_waitingAgentChips(waitingOn.agents)}</span></summary><div class="wait-block-body"><div class="wait-block-live">${escapeHtml(_waitText('waitBlock.viewStatus', 'View live status'))}</div></div></details>`;
}
function _stopWaitingBlockPoll(details) {
  const timer = _waitingBlockPolls.get(details);
  if (timer != null) clearInterval(timer);
  _waitingBlockPolls.delete(details);
}
function _renderWaitingBlockStatus(details, status) {
  const body = details.querySelector('.wait-block-body');
  if (!body) return;
  const rows = (Array.isArray(status?.agents) ? status.agents : []).map((agent) => `<div class="wait-block-agent-row"><code>${escapeHtml(String(agent?.id || '?'))}</code><span>${escapeHtml(String(agent?.role || '?'))}</span><span>${escapeHtml(String(agent?.status || '?'))}${agent?.rounds != null ? ` · ${escapeHtml(String(agent.rounds))} rounds` : ''}</span></div>`).join('');
  body.innerHTML = rows || `<div class="wait-block-live">${escapeHtml(_waitText('waitBlock.agentStatus', 'No live agent status yet.'))}</div>`;
}
async function _pollWaitingBlock(details) {
  if (!details.isConnected || !details.open) {
    _stopWaitingBlockPoll(details);
    return;
  }
  if (typeof Api === 'undefined' || !Api.swarm?.status) return;
  try {
    const status = await Api.swarm.status(details.getAttribute('data-wait-swarm-key'));
    if (!details.isConnected || !details.open) return;
    _renderWaitingBlockStatus(details, status);
    if (status?.terminated || (status?.known === true && status?.active === false)) {
      const body = details.querySelector('.wait-block-body');
      if (body) body.insertAdjacentHTML('afterbegin', `<div class="wait-block-resolved">${escapeHtml(_waitText('waitBlock.resolved', 'Background work resolved.'))}</div>`);
      _stopWaitingBlockPoll(details);
    }
  } catch (_ignored) {}
}
function _startWaitingBlockPoll(details) {
  _stopWaitingBlockPoll(details);
  _pollWaitingBlock(details);
  _waitingBlockPolls.set(details, setInterval(() => _pollWaitingBlock(details), 3000));
}
/* Feature-detect, not existence-detect (the push.js pattern): bare-node
 * harnesses stub `document = {}` without addEventListener, and an existence
 * check alone crashes module evaluation there. */
if (typeof document !== 'undefined' && document.addEventListener) document.addEventListener('toggle', (event) => {
  const details = event.target;
  if (!details?.classList?.contains('wait-block')) return;
  if (details.open) _startWaitingBlockPoll(details);
  else _stopWaitingBlockPoll(details);
}, true);

function renderFinishInfo(msg, turnId) {
  // A recovery command is pending locally but the authoritative ACK has not
  // arrived yet. Keep the same bubble visibly active and never flash its old
  // terminal settlement during that window.
  if (msg._commandPending) return "";
  const _turnStatus = msg._turnStatus || '';
  const _turnTerminal = ['completed', 'interrupted', 'truncated', 'failed']
    .includes(_turnStatus);
  if (!_turnTerminal) return "";
  const parts = [];
  const _mid = msg.model || msg.preset || msg.effort || "";
  const _pid = msg.provider_id || msg.providerId || "";
  const u = msg.usage || {};
  const fmt = (n) => (n >= 1000000 ? (n / 1000000).toFixed(1) + "m" : n >= 1000 ? (n / 1000).toFixed(1) + "k" : n.toString());
  const thk = u.reasoning_tokens || u.thinking_tokens || 0;

  // Model tag — auto-detect brand from model_id
  const depthIcons = { medium: '', high: '', max: '', ultra: '' };
  const depthLabels = { medium: "Med", high: "Hi", max: "Max", ultra: "Ultra" };
  // The typed owner reads the canonical response-authoring round. It may use
  // a bounded main-round compatibility scan for old projections, but internal
  // compaction and discarded billing rows can never become the finish route.
  const _route = typeof resolveTurnServingRoute === 'function'
    ? resolveTurnServingRoute(msg)
    : { model: _mid, providerId: _pid, keyName: '', keyTail: '' };
  const _realModel = _route.model || _mid;
  const _realProvider = _route.providerId || _pid;
  // Friendly key display: prefer the last 4 chars of the real API key
  //   (rendered as ••1234), else fall back to the raw slot name.
  const _keyTail = _route.keyTail;
  const _keyDisplay = _keyTail ? ('••' + _keyTail) : _route.keyName || "";

  if (_realModel) {
    const _brand = typeof _modelBrand === 'function'
      ? _modelBrand(_realModel)
      : (typeof _detectBrand === 'function' ? _detectBrand(_realModel) : 'generic');
    const icon = (typeof _brandSvg === 'function') ? _brandSvg(_brand, 12) : Icon('star', 12);
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
      `<span class="finish-tag preset" data-preset="${_brand}" title="Model: ${escapeHtml(_realModel)}${depth ? ' · Depth: ' + escapeHtml(depth) : ''}">${icon} ${escapeHtml(displayName)}${depthStr}</span>`,
    );
  }

  const _modelRoute = msg.orchestration?.modelRoute;
  if (_modelRoute?.selectedModel && _modelRoute.resolvedModel
      && _modelRoute.selectedModel !== _modelRoute.resolvedModel) {
    const _modelRouteLabel = t('finishInfo.modelRouteTag', {
      from: _modelRoute.selectedModel,
      to: _modelRoute.resolvedModel,
    });
    const _modelRouteTip = t('finishInfo.modelRouteTip', {
      from: _modelRoute.selectedModel,
      to: _modelRoute.resolvedModel,
      role: _modelRoute.role || '?',
      tier: _modelRoute.tier || '?',
    });
    parts.push(
      `<span class="finish-tag warn model-route" title="${escapeHtml(_modelRouteTip)}">${escapeHtml(_modelRouteLabel)}</span>`,
    );
  }

  // Route tag — the actual provider + API-key slot that served this turn.
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

  // Sticky experiment arm — persisted with the real usage/cost snapshot so
  // live completion and reload show the same assignment.
  const _costExperiment = msg.costExperiment;
  if (_costExperiment && _costExperiment.status === 'assigned' && _costExperiment.arm) {
    const _arm = _costExperiment.arm;
    const _armLabel = _arm === 'optimized'
      ? t('settings.costExperimentArmOptimized')
      : t('settings.costExperimentArmControl');
    const _expTip = `${t('settings.costExperiment')}: ${_costExperiment.experiment_id || ''}`;
    parts.push(`<span class="finish-tag cost-experiment ${escapeHtml(_arm)}" title="${escapeHtml(_expTip)}">A/B · ${escapeHtml(_armLabel)}</span>`);
  }

  const _presentation = (typeof runtimeScope.ConversationTurnStore !== 'undefined')
    ? runtimeScope.ConversationTurnStore.finishPresentation({
        status: _turnStatus, settlement: msg._turnSettlement || {},
      }) : null;
  const _terminalError = (msg._turnSettlement || {}).error || msg.error;
  const _terminalEnv = (typeof normalizeErrorEnvelope === 'function')
    ? normalizeErrorEnvelope(_terminalError) : null;
  const _kindLabel = (typeof errorEnvelopeKindLabel === 'function')
    ? errorEnvelopeKindLabel(_terminalError) : '';
  const _detail = _presentation?.detail;
  const _presentationDetail = (typeof _detail === 'string')
    ? _detail : (_detail ? JSON.stringify(_detail) : '');
  const _detailText = _terminalEnv && typeof errorEnvelopeMessage === 'function'
    ? errorEnvelopeMessage(_terminalEnv)
    : (_presentationDetail
       || (typeof _terminalError === 'string' ? _terminalError : ''));
  /* A goal-mode/flow worker step can fail inside a turn that later
   * completes: its durable message carries msg.error. Render the error tag
   * on THAT message instead of a ✓ over the failure text. */
  if (_turnStatus === 'completed' && !_terminalError) {
    const _completedLabel = _presentation?.label || 'Completed';
    parts.push(`<span class="finish-tag ok" title="${escapeHtml(_completedLabel)}" aria-label="${escapeHtml(_completedLabel)}">${Icon('check', 12)}</span>`);
  } else if (_turnStatus === 'failed' || _terminalError) {
    const _failedTone = (_terminalEnv?.severity === 'warning'
      || _presentation?.tone === 'warning') ? 'warn' : 'err';
    const _failedLabel = _kindLabel || t('finishInfo.reasonFailed');
    const _failedTip = _detailText || t('finishInfo.reasonFailedTip');
    const _failedIcon = _failedTone === 'warn'
      ? Icon('alertTriangle', 12) : Icon('x', 12);
    const _kindAttr = _terminalEnv?.kind
      ? ` data-error-kind="${escapeHtml(_terminalEnv.kind)}"` : '';
    parts.push(`<span class="finish-tag ${_failedTone} terminal-failure"${_kindAttr} title="${escapeHtml(_failedTip)}">${_failedIcon}${escapeHtml(_failedLabel)}</span>`);
  } else {
    parts.push(`<span class="finish-tag warn" title="${escapeHtml(_detailText)}">${escapeHtml(_presentation?.label || _turnStatus)}</span>`);
  }
  /* Task completion timing — settle clock time + wall-clock duration from
   * the authoritative TurnRecord lifecycle (createdAt → updatedAt), passed
   * through by the turn store as _turnCreatedAt/_turnUpdatedAt. */
  const _tCreated = Number(msg._turnCreatedAt) || 0;
  const _tSettled = Number(msg._turnUpdatedAt) || 0;
  if (_tSettled > 0) {
    const _settledDate = new Date(_tSettled);
    if (!Number.isNaN(_settledDate.getTime())) {
      const _clockText = _settledDate.toLocaleTimeString([], {
        hour: '2-digit', minute: '2-digit', second: '2-digit',
      });
      const _hasDuration = _tCreated > 0 && _tSettled >= _tCreated
        && (typeof _formatTurnDuration === 'function');
      const _durationText = _hasDuration
        ? _formatTurnDuration(_tSettled - _tCreated) : '';
      const _fullStamp = _settledDate.toLocaleString([], {
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit',
      });
      const _timingTip = _hasDuration
        ? t('finishInfo.timingTip', { time: _fullStamp, duration: _durationText })
        : t('finishInfo.timingTipNoDuration', { time: _fullStamp });
      const _timingLabel = _durationText
        ? `${_clockText} · ${_durationText}` : _clockText;
      parts.push(
        `<span class="finish-tag timing" title="${escapeHtml(_timingTip)}">` +
        `${Icon('clock', 12)} ${escapeHtml(_timingLabel)}</span>`,
      );
    }
  }
  if (u) {
    const inp = u.prompt_tokens || u.input_tokens || 0;
    const out = u.completion_tokens || u.output_tokens || 0;
    // API rounds info
    const rounds = msg.apiRounds || [];
    const numRounds = rounds.length;
    const _cw0 = u.cache_write_tokens || u.cache_creation_input_tokens || 0;
    const _cr0 = u.cache_read_tokens || u.cache_read_input_tokens || 0;
    // Cost is computed server-side by lib.cost.compute_cost and carries the
    // authoritative convention split. Never infer Anthropic/OpenAI semantics
    // from token magnitudes in the browser.
    const _settledCost = msg.cost || null;
    const _displayInp = Number(_settledCost?.totalInputTokens ?? inp) || 0;
    const _displayOut = Number(_settledCost?.outputTokens ?? out) || 0;
    if (_displayInp > 0 || _displayOut > 0) {
      // Equal total input/output can have radically different prices when one
      // request reuses a warm prefix and another re-bills it uncached. Keep
      // the collapsed footer honest by showing the backend's convention-safe
      // split at a glance; the rich popover remains the per-round drill-down.
      let tokText = _settledCost
        ? escapeHtml(t('finishInfo.inputSplit', {
            total: fmt(_displayInp),
            uncached: fmt(Number(_settledCost.inputTokens) || 0),
            cache: fmt(Number(_settledCost.cacheReadTokens) || 0),
            output: fmt(_displayOut),
          }))
        : `${fmt(_displayInp)} → ${fmt(_displayOut)}`;
      if (thk > 0)
        tokText += ` <span style="color:#a78bfa;opacity:0.8">(${fmt(thk)}${t('msg.thinking')})</span>`;
      parts.push(`<span class="token-tag">${tokText}</span>`);
    }
    // typeof-guarded for the repository's isolated-function harnesses and
    // script-tag degraded mode, which can load renderFinishInfo without the
    // helper prefix.  The production bundle always provides the helper.
    const _quota = (typeof _subscriptionQuotaForMessage === 'function')
      ? _subscriptionQuotaForMessage(msg) : null;
    if (_quota) {
      const _labels = [];
      const _tips = [];
      let _delta = 0;
      let _hasBaseline = false;
      for (const name of ['primary', 'secondary']) {
        const win = _quota.latest && _quota.latest[name];
        if (!win || !Number.isFinite(Number(win.remaining_percent))) continue;
        const wl = _quotaWindowLabel(win.window_minutes);
        const remain = _quotaPct(win.remaining_percent);
        const used = _quotaPct(win.used_percent);
        _labels.push(t('finishInfo.quotaRemainingShort', { window: wl, remaining: remain }));
        _tips.push(t('finishInfo.quotaWindowDetail', {
          window: wl, used, remaining: remain,
        }));
        _delta = Math.max(_delta, Number(_quota.deltas[name]) || 0);
        _hasBaseline = _hasBaseline || _quota.baselines[name];
      }
      if (_labels.length) {
        let observed = '';
        if (_delta > 0) {
          observed = t('finishInfo.quotaObservedDelta', { delta: _quotaPct(_delta) });
          _tips.push(observed);
        } else if (_hasBaseline) {
          observed = t('finishInfo.quotaObservedNoTick');
          _tips.push(observed);
        }
        const exactTokens = _quota.turnTokens > 0
          ? t('finishInfo.quotaTurnTokens', { tokens: fmt(_quota.turnTokens) }) : '';
        if (exactTokens) _tips.push(t('finishInfo.quotaTurnTokensDetail', {
          tokens: fmt(_quota.turnTokens),
        }));
        _tips.push(t('finishInfo.quotaCaveat'));
        /* One flex item per fact, never one monolithic joined blob: the
         * finish row's flex-wrap can only break BETWEEN items, and every
         * tag is white-space:nowrap, so a single joined quota string
         * (~half the row wide) dropped whole to the next line whenever it
         * missed the remaining space by a pixel — stranding half of line 1
         * empty. Split facts pack greedily and wrap one by one. The group
         * prefix rides on the first fact; every fact keeps the full detail
         * tooltip so hover anywhere in the group shows the same breakdown. */
        const _facts = _labels.slice();
        if (exactTokens) _facts.push(exactTokens);
        if (observed) _facts.push(observed);
        _facts[0] = `${t('finishInfo.quotaPrefix')} · ${_facts[0]}`;
        const _quotaTip = escapeHtml(_tips.join('\n'));
        for (const _fact of _facts) {
          parts.push(`<span class="subscription-quota-tag" title="${_quotaTip}">${escapeHtml(_fact)}</span>`);
        }
      }
    }
    const cw = u.cache_write_tokens || u.cache_creation_input_tokens || 0;
    const cr = u.cache_read_tokens || u.cache_read_input_tokens || 0;
    // Enhanced cost display with per-round breakdown.
    // Prefer cost from the authoritative settled-turn projection. Fall back
    // to lazy calculation while the live terminal projection is arriving.
    const costInfo = _settledCost || calcCostCny(u, _mid, _pid);
    if (costInfo && costInfo.costCny > 0) {
      /* Epic-E sub-8: the popover builds LAZILY on first open — the
       * builder lives in ui/finish_info_rich.js and the feature bridge
       * prepares its lazy ESM domain before dispatching _toggleCostPopover.
       * Stash the build context by stable Turn identity; embed nothing. */
      if (turnId) _rememberCostContext(turnId, {
        costInfo, rounds, numRounds, u, inp, out, cw, cr, thk,
        mid: _mid, pid: _pid, taskId: msg._taskId || '',
        toolRounds: msg.toolRounds || [],
      });
      let savingsHtml = "";
      if (costInfo.cacheSavingsCny > 0)
        savingsHtml = ` <span class="cost-savings">↓${(costInfo.cacheSavingsCny >= 0.01 ? "¥" + costInfo.cacheSavingsCny.toFixed(3) : "¥" + costInfo.cacheSavingsCny.toFixed(4))}</span>`;
      // At-a-glance cache-miss marker: if any round had a cache break,
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
        `data-tofu-action="_toggleCostPopover(event,this)">` +
        `${formatCny(costInfo.costCny)}${savingsHtml}${breakHtml}` +
        `<span class="cost-popover-data" hidden></span>` +
        `</span>`,
      );
    }
  }
  // Clickable trace tag — click to copy full trace_id (debug mode only)
  if (typeof _featureFlags !== 'undefined' && _featureFlags.debug_mode) {
    const _allTraces = (msg.apiRounds || []).map(rd => (rd.usage || {}).trace_id).filter(Boolean);
    const _lastTrace = _allTraces.length ? _allTraces[_allTraces.length - 1] : ((msg.usage || {}).trace_id || '');
    if (_lastTrace) {
      const _allStr = _allTraces.length > 1 ? _allTraces.join('\n') : _lastTrace;
      // Escape for safe embedding inside inline onclick JS string literal (single-quoted)
      const _jsSafe = _allStr.replace(/\\/g, '\\\\').replace(/'/g, "\\'").replace(/\n/g, '\\n');
      parts.push(
        `<span class="finish-tag" style="cursor:pointer;opacity:0.6;font-size:0.8em" title="${escapeHtml(t('finishInfo.traceCopyTip', { ids: _allStr }))}" data-tofu-action="_safeClipboardWrite('${_jsSafe}');this.textContent='copied'">${escapeHtml(_lastTrace.slice(0,8))}</span>`
      );
    }
  }
  if (msg.fallbackModel) {
    /* The cause is rendered as VISIBLE text, not only in the hover title.
     *   A tooltip is unreachable on touch and easy to miss on desktop, so a
     *   settled fallback used to read as "回退 → kimi-k3" with no cause at
     *   all — the reason (often an entire upstream HTML error page) was
     *   locked inside `title`. Formatting comes from the shared
     *   typed error-presentation owner so this tag and the live streaming
     *   banner can never name the same failure differently. */
    const _fb = (typeof fallbackCauseParts === 'function')
      ? fallbackCauseParts(msg)
      /* The composition prelude binds the typed presentation owner before this
       * retained adapter. The guard is for isolated JSDOM harnesses that load
       * finish_info.js alone; degrade to the verbatim cause rather than
       * throwing and killing the whole finish bar. */
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
  const waitingBlock = renderWaitingOnBlock(msg.waitingOn, {
    conversationId: msg._conversationId,
  });
  if (waitingBlock) parts.push(waitingBlock);
  if (parts.length === 0) return "";
  return `<div class="message-finish">${parts.join("")}</div>`;
}
