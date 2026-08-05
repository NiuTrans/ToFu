/* ═══════════════════════════════════════════════════════════════════
   core/conv_persist_helpers.js — pure persist / freshness / rebase helpers.

   Extracted from core/conversations.js (pt_3879f00e sub-part 2, slice 3):
   the pure-helper cluster covering PUT-payload cleansing, the
   segments/translation/image freshness signals, and the 409 CAS
   rebase. Zero runtime state, zero IIFE-load side effects — every read
   of an external symbol happens at CALL time via bundle-level `window`
   scope.

   Symbols:
     _stripUsageTransient(u)                        — strip _wire_fp/_wire_static from usage
     _trimMsgForPersist(m)                          — cleanse segments/toolRounds/apiRounds/live-usage for PUT
     _serverHasSegmentsLocalLacks(server, local)    — segments-freshness signal
     _serverHasTranslationLocalLacks(server, local) — translation-freshness signal
     _serverHasImagesLocalLacks(server, local)      — image-strip freshness signal
     _isErrorOnlyAssistant(m)                       — error-only-tail probe (rebase drop)
     _rebaseUnackedTail(serverMsgs, localMsgs)      — 409 CAS append-missing-tail rebase

   And the module-local constant `_USAGE_TRANSIENT_KEYS`, which
   _stripUsageTransient reads at call time.

   This file is concatenated into the core bundle BEFORE
   core/conversations.js (guarded by _BUNDLE_FILES ordering +
   tests/test_frontend_conv_persist_helpers_extracted.py).
   ═══════════════════════════════════════════════════════════════════ */

/* ★ Strip transient/diagnostic bloat before a message rides the PUT wire.
 *
 * Three fields balloon the persisted conversation without any render value,
 * so a fat conv OOMs the browser on load (see the server-side
 * _sanitize_*_for_persist twins in lib/tasks_pkg/manager.py — this is the
 * frontend mirror so a client PUT can never re-bloat what the server trimmed):
 *   1. usage._wire_fp / _wire_static inside apiRounds[] — backend-only SSE
 *      cache-miss diagnostics (~226 KB/round), read by NO frontend code.
 *   2. toolRounds[]._partialOutput on a DONE round — the live run_command
 *      terminal buffer; the authoritative output is in results[0].output /
 *      toolContent. A still-running round keeps it (mid-stream replay).
 * Inline base64 imageDataUris are the render source, so they are NOT stripped
 * from the PUT/DB copy — only from the IndexedDB cache (idb-cache _stripMessage).
 * Returns a shallow clone only when it actually trims (never mutates the live
 * message object). */
const _USAGE_TRANSIENT_KEYS = ['_wire_fp', '_wire_static'];
function _stripUsageTransient(u) {
  if (!u || typeof u !== 'object') return u;
  if (!_USAGE_TRANSIENT_KEYS.some((k) => k in u)) return u;
  const o = {};
  for (const k in u) if (!_USAGE_TRANSIENT_KEYS.includes(k) && Object.prototype.hasOwnProperty.call(u, k)) o[k] = u[k];
  return o;
}
function _trimMsgForPersist(m) {
  let r = m;
  /* ★ segments (epic pt_cb8f98b0cb9b47fb): the backend OWNS task['segments']
   *   as the authoritative typed-timeline SoT — it re-derives + re-persists it
   *   on every task finalization (manager.py _sync_result_to_conversation).
   *   The frontend does NOT consume it yet (step-5 cutover) and must NEVER echo
   *   it back on a full-conv PUT: doing so (a) roughly doubles the assistant
   *   payload (segments restate content+thinking+tool-result text) and (b) can
   *   overwrite the server-fresh segments with a STALE client copy after a
   *   local mutation (regen/translate that didn't update segments). Strip it —
   *   same contract as _partialOutput/_wire_fp above. */
  if ('segments' in m) {
    r = { ...r };
    delete r.segments;
  }
  if (Array.isArray(m.toolRounds) && m.toolRounds.some((rd) => rd && rd.status === 'done' && rd._partialOutput)) {
    r = { ...r, toolRounds: m.toolRounds.map((rd) => {
      if (rd && rd.status === 'done' && rd._partialOutput) {
        const c = { ...rd }; delete c._partialOutput; return c;
      }
      return rd;
    }) };
  }
  /* ★ Inbox-inject wire-purity belt (epic pt_d022c86a00fc4580): the live SSE
   *   handlers (_handleSwarmInboxInject / _handlePeerInboxInject) push SYNTHETIC
   *   display-only rows (flagged `_inboxInject` / `_peerInject`, roundNum 9e6+,
   *   no toolCallId/toolContent) into the LIVE conv.messages[].toolRounds so the
   *   in-timeline chip shows the instant results land. Those rows are DISPLAY-
   *   ONLY: their durable home is the underscore sidecar (_inboxInjects /
   *   _peerInjects / _userSteerInjects), and getToolRoundsFromMsg rebuilds them
   *   at render time. They must NEVER reach the DB `toolRounds`, because that
   *   array is ALSO the wire-replay / prefix-cache source — a row lacking
   *   toolCallId/toolContent collapses the whole assistant turn to a lossy
   *   summary (breaking tool-turn continuation) AND shifts the wire prefix
   *   (cache miss). A full-conv PUT fired mid-stream (before the terminal
   *   committedMessage overwrites toolRounds with the clean backend list) would
   *   otherwise persist them. The server-side reconstructor guard
   *   (is_synthetic_inbox_round) already filters them from the wire; this belt
   *   keeps the DB blob itself clean so the two never diverge. Clone-and-strip,
   *   never mutating the live array (the live chip stays visible this session).
   *   Keeps the marker key list in lock-step with
   *   lib/tasks_pkg/segments/_types.py::SYNTHETIC_INBOX_MARKERS. */
  if (Array.isArray(r.toolRounds)
      && r.toolRounds.some((rd) => rd && (rd._inboxInject || rd._peerInject || rd._userSteerInject))) {
    r = { ...r, toolRounds: r.toolRounds.filter(
      (rd) => !(rd && (rd._inboxInject || rd._peerInject || rd._userSteerInject))) };
  }
  if (Array.isArray(m.apiRounds) && m.apiRounds.some((rd) => rd && rd.usage && _USAGE_TRANSIENT_KEYS.some((k) => k in rd.usage))) {
    r = { ...r, apiRounds: m.apiRounds.map((rd) => (
      rd && rd.usage ? { ...rd, usage: _stripUsageTransient(rd.usage) } : rd
    )) };
  }
  // _liveLastRoundUsage.usage carries the same raw usage dict (with _wire_fp);
  // the reader (context-bar.js) only uses .tokensIn, never usage._wire_fp.
  if (m._liveLastRoundUsage && m._liveLastRoundUsage.usage
      && _USAGE_TRANSIENT_KEYS.some((k) => k in m._liveLastRoundUsage.usage)) {
    r = { ...r, _liveLastRoundUsage: { ...m._liveLastRoundUsage, usage: _stripUsageTransient(m._liveLastRoundUsage.usage) } };
  }
  return r;
}

/* ★ Segment-recovery freshness signal (epic pt_cb8f98b0cb9b47fb).
 *
 * The display-only GET-path backstop (_rehydrate_segments_from_task_results)
 * fills `segments` onto assistant messages that lack them, but does NOT bump
 * the conversation `updatedAt` or change the message count (it's a read-time
 * enrichment, never persisted on GET). So for a HISTORICAL turn whose cached
 * copy was seeded segment-less (before the sse_pipeline committedMessage fix,
 * or by any client PUT that stripped segments), the Phase-2 freshness check
 * (`serverMsgs.length !== conv.messages.length || serverUpdatedAt > cached`)
 * sees NO difference and keeps the segment-less cache — so the interleaved
 * tool/thinking timeline stays empty until a hard refresh happens to move
 * updatedAt. This predicate makes "server has segments the local copy lacks"
 * an explicit staleness signal so the rehydrated server copy always wins.
 *
 * Positional compare (both arrays are the same turn order at equal length; the
 * length-mismatch case is already handled by the caller's count check). Returns
 * true as soon as ANY aligned assistant message has server segments but the
 * local copy has none — cheap early-out, no allocation. */
function _serverHasSegmentsLocalLacks(serverMsgs, localMsgs) {
  if (!Array.isArray(serverMsgs) || !Array.isArray(localMsgs)) return false;
  const n = Math.min(serverMsgs.length, localMsgs.length);
  for (let i = 0; i < n; i++) {
    const sm = serverMsgs[i], lm = localMsgs[i];
    if (!sm || !lm || sm.role !== 'assistant') continue;
    const sHas = Array.isArray(sm.segments) && sm.segments.length > 0;
    const lHas = Array.isArray(lm.segments) && lm.segments.length > 0;
    if (sHas && !lHas) return true;
  }
  return false;
}
if (typeof window !== 'undefined') window._serverHasSegmentsLocalLacks = _serverHasSegmentsLocalLacks;

/* ★ Translation-recovery freshness signal (symmetric to
 * _serverHasSegmentsLocalLacks).
 *
 * Server-side auto-translate commits AFTER a turn settles: it stamps
 * `translatedContent` on the deliverable AND `segments[].translatedText` on
 * each per-round narration segment (lib/translate/commit.py), bumping
 * `updated_at`. But the IDB cache may already hold a copy whose `updatedAt`
 * equals-or-exceeds that value (a later client PUT of a still-English in-memory
 * copy can re-stamp cachedAt/updatedAt), so the `serverUpdatedAt > cached`
 * disjunct in cacheIsStale silently misses it — and the message count is
 * unchanged and segments are present, so the other two disjuncts miss it too.
 * Result: the stale English cache is judged FRESH and kept, and the reopened
 * conversation renders English narration even though the server has Chinese.
 *
 * This predicate makes "server carries a translation (deliverable OR per-round
 * narration) the aligned local copy lacks" an explicit staleness signal, the
 * exact mirror of the segments-missing backstop. Positional compare within
 * equal-length arrays (the length-mismatch case is handled by the caller's
 * count check); cheap early-out on the first gap. */
function _serverHasTranslationLocalLacks(serverMsgs, localMsgs) {
  if (!Array.isArray(serverMsgs) || !Array.isArray(localMsgs)) return false;
  const n = Math.min(serverMsgs.length, localMsgs.length);
  for (let i = 0; i < n; i++) {
    const sm = serverMsgs[i], lm = localMsgs[i];
    if (!sm || !lm || sm.role !== 'assistant') continue;
    // Identity guard: only compare translations of the SAME turn.
    if ((sm.content || '') !== (lm.content || '')) continue;
    // Deliverable-level gap.
    if (sm.translatedContent && !lm.translatedContent) return true;
    // Per-round narration gap (segments[].translatedText).
    if (Array.isArray(sm.segments) && Array.isArray(lm.segments)) {
      const k = Math.min(sm.segments.length, lm.segments.length);
      for (let j = 0; j < k; j++) {
        const ss = sm.segments[j], ls = lm.segments[j];
        if (!ss || !ls) continue;
        if (ss.type !== 'text' || ss.deliverable) continue;
        if (ss.type !== ls.type || ss.llmRound !== ls.llmRound) continue;
        const zh = ss.translatedText;
        if (zh && zh.trim() && !(ls.translatedText && ls.translatedText.trim())) return true;
      }
    }
  }
  return false;
}
if (typeof window !== 'undefined') window._serverHasTranslationLocalLacks = _serverHasTranslationLocalLacks;

/* ★ Image-strip recovery freshness signal (symmetric to
 * _serverHasSegmentsLocalLacks / _serverHasTranslationLocalLacks).
 *
 * The IndexedDB cache write (_stripToolRound in idb-cache.js) deliberately
 * drops toolRounds[].results[].imageDataUris[].uri — the multi-MB inline
 * base64 that IS the render source for read_files / inspect_image /
 * browser_screenshot / browser_preview_page inline thumbnails — keeping only
 * format/filename so a fat conversation can't OOM the tab. The PUT/DB copy
 * KEEPS the uris (_trimMsgForPersist above, and the server-side
 * manager/_persist.py twin, both exempt them). But the Phase-2 cacheIsStale
 * disjuncts (count / updatedAt / segments / translation) see NO difference
 * between that stripped cache and the server copy, so a cache-fresh reload
 * kept the stripped copy and every image round silently degraded to its
 * badge-only line — the inline thumbnail (and click-to-fullscreen) vanished
 * until some unrelated mutation happened to bump the conversation. (The
 * idb-cache comment's "a cache read that needs them re-fetches from server"
 * described exactly this missing signal.)
 *
 * This predicate makes "server carries an image uri the aligned local copy
 * lacks" an explicit staleness signal: the cache is judged stale, the server
 * copy is adopted, and image thumbnails survive reloads. Positional compare
 * within equal-length arrays (the length-mismatch case is already handled by
 * the caller's count check). Identity guard: only compare rounds of the SAME
 * turn (content equality) so a locally regenerated/edited turn aligned
 * positionally is never misread as an images gap. Cheap early-out on the
 * first gap. */
function _serverHasImagesLocalLacks(serverMsgs, localMsgs) {
  if (!Array.isArray(serverMsgs) || !Array.isArray(localMsgs)) return false;
  const n = Math.min(serverMsgs.length, localMsgs.length);
  for (let i = 0; i < n; i++) {
    const sm = serverMsgs[i], lm = localMsgs[i];
    if (!sm || !lm || sm.role !== 'assistant') continue;
    // Identity guard: only compare image rounds of the SAME turn.
    if ((sm.content || '') !== (lm.content || '')) continue;
    const sRounds = sm.toolRounds, lRounds = lm.toolRounds;
    if (!Array.isArray(sRounds) || !Array.isArray(lRounds)) continue;
    const k = Math.min(sRounds.length, lRounds.length);
    for (let j = 0; j < k; j++) {
      const sRes = sRounds[j] && sRounds[j].results;
      const lRes = lRounds[j] && lRounds[j].results;
      if (!Array.isArray(sRes) || !Array.isArray(lRes)) continue;
      const sHas = sRes.some((r) => r && Array.isArray(r.imageDataUris)
        && r.imageDataUris.some((d) => d && d.uri));
      if (!sHas) continue;
      const lHas = lRes.some((r) => r && Array.isArray(r.imageDataUris)
        && r.imageDataUris.some((d) => d && d.uri));
      if (!lHas) return true;
    }
  }
  return false;
}
if (typeof window !== 'undefined') window._serverHasImagesLocalLacks = _serverHasImagesLocalLacks;

/* ═══════════════════════════════════════════════════════════════════
   rev-based CAS rebase (append-missing-tail, keyed on _msgId)
   ───────────────────────────────────────────────────────────────────
   When the server rejects a full-conv PUT with 409 `blocked_rev_conflict`,
   the client's baseRev is stale — another tab/device/server-write advanced
   the row's rev. A blind re-PUT would clobber that fresher server truth, so
   instead we three-way rebase: take the SERVER messages as the authoritative
   base, then APPEND any local messages the server doesn't yet have (matched by
   stable `_msgId`), preserving each surviving message's `_msgId` verbatim.

   Append-missing-tail (NOT a full ordered merge) is deliberate: the only race
   CAS actually catches is a concurrent APPEND (edits/regens bypass CAS via the
   allowTruncate user-action path), so a full merge would risk reordering or
   resurrecting a message the user edited away. Keeping `_msgId` verbatim is
   load-bearing — reassigning it makes the message look new to the DB and would
   spuriously bump rev on the very next write.

   Poor-network correctness (lost-ACK): the send can SUCCEED server-side (real
   user turn + real assistant reply persisted, rev bumped) while its RESPONSE is
   lost, so the client thinks it failed, appends a local error bubble, and
   rescue-PUTs → 409 → here. Two hazards this must avoid:
     • DUPLICATE user turn — deduped by `_msgId` (client now ships the id on the
       send payload so the server persists the SAME id) AND, defensively, by
       matching (role,timestamp) which the backend's own idempotent append uses.
     • Stale ERROR bubble coexisting with the real answer — a local
       assistant message that is ERROR-ONLY (no content/thinking/toolRounds) is
       DROPPED when the server already carries a real assistant reply for the
       same turn, so the user never sees "error + answer" for one send.

   Returns the rebased message array (server base + appended local-only tail). */
function _isErrorOnlyAssistant(m) {
  return !!(m && m.role === 'assistant' && m.error
    && !(m.content || '').trim()
    && !(m.thinking || '').trim()
    && !(m.toolRounds && m.toolRounds.length)
    && !m._igResult && !(m._igResults && m._igResults.length));
}
function _rebaseUnackedTail(serverMsgs, localMsgs) {
  const base = Array.isArray(serverMsgs) ? serverMsgs.slice() : [];
  const serverIds = new Set();
  const serverUserTs = new Set();       // (role=user) timestamps present on server
  const serverAsstTaskIds = new Set();  // (role=assistant) _taskId present on server
  let serverHasTrailingRealAssistant = false;
  for (const m of base) {
    if (!m) continue;
    if (m._msgId) serverIds.add(m._msgId);
    if (m.role === 'user' && m.timestamp) serverUserTs.add(m.timestamp);
    if (m.role === 'assistant' && m._taskId) serverAsstTaskIds.add(m._taskId);
  }
  const lastServer = base.length ? base[base.length - 1] : null;
  if (lastServer && lastServer.role === 'assistant' && !_isErrorOnlyAssistant(lastServer)) {
    serverHasTrailingRealAssistant = true;
  }
  for (const lm of (Array.isArray(localMsgs) ? localMsgs : [])) {
    if (!lm) continue;
    if (lm._msgId && serverIds.has(lm._msgId)) continue;   // server already has this exact msg
    // Defensive dedup: a user turn the server persisted under a matching
    // timestamp (the backend's own idempotency key) — skip even if _msgId
    // somehow diverged (old client mid-rollout).
    if (lm.role === 'user' && lm.timestamp && serverUserTs.has(lm.timestamp)) continue;
    // Defensive dedup by _taskId: a local assistant bubble whose _taskId the
    // server already carries on an assistant row IS that same committed turn —
    // its _msgId diverged only because a pre-fix client minted a tmp_ id while
    // the server minted a UUID (the duplicate-assistant-bubble bug). Skip it so
    // the rescue PUT can't re-append the tmp_-id twin. Guarded on role so a
    // user turn is never dropped by an assistant's taskId.
    if (lm.role === 'assistant' && lm._taskId && serverAsstTaskIds.has(lm._taskId)) {
      console.warn('[rebase] dropping local assistant bubble whose _taskId '
        + `${String(lm._taskId).slice(0,8)} already has a server-committed reply `
        + '(tmp_-id twin; duplicate-bubble guard)');
      continue;
    }
    // Drop a stale local error-only assistant bubble when the server already
    // answered this turn for real (lost-ACK: send succeeded, response lost).
    if (_isErrorOnlyAssistant(lm) && serverHasTrailingRealAssistant) {
      console.warn('[rebase] dropping stale local error-only assistant bubble — '
        + 'server has a real reply for this turn (lost-ACK recovery)');
      continue;
    }
    base.push(lm);                                         // append verbatim (keeps _msgId)
    if (lm._msgId) serverIds.add(lm._msgId);
    if (lm.role === 'user' && lm.timestamp) serverUserTs.add(lm.timestamp);
    if (lm.role === 'assistant' && lm._taskId) serverAsstTaskIds.add(lm._taskId);
  }
  return base;
}


/* ═══════════════════════════════════════════════════════════════════
   _lightMessageForSync(m) — per-message WIRE-shape reducer used to
   build the outbound PUT payload in syncConversationToServer.

   Extracted 2026-07-31 (pt_3879f00e sub-part 2 slice 14) from a nested
   arrow closure inside conversations.js::syncConversationToServer (~L136).
   The closure had drifted into a private helper of a 364L function
   while its per-message primitive (_trimMsgForPersist, above) lived
   here in the persist-helper family — this promotion completes the
   family and gives the WIRE reducer one reusable home instead of a
   50-line inline arrow that could shard if a second consumer emerges.

   Behaviour is byte-identical to the original closure:
     * m.images[] → { url (verbatim), preview (apiUrl(url) for
       server-canonical urls, else truncated at 200 chars + '...'),
       mediaType, sizeKB, optional pdfPage/pdfTotal/pdfName/caption }.
     * m.pdfTexts[] → flat { name, pages, textLength, isScanned,
       method, text } (drops any extra decorators).
     * _pendingSync — CLONE-and-STRIP: this is a CLIENT-ONLY
       durability marker (set when a send failed on a poor network so
       a refresh keeps the message and a retry re-attempts the PUT).
       It must NEVER be persisted to the server — otherwise it echoes
       back on the next load and could wrongly trigger the KEEP_LOCAL
       reconcile. Clone-and-strip preserves the local marker until the
       PUT actually succeeds.
     * Final _trimMsgForPersist pass — the SIBLING primitive that
       strips transient-bloat (usage._wire_fp, _partialOutput on done
       rounds, inbox-inject synthetic toolRounds).

   Pure reducer over one message dict; reads `apiUrl` (typeof-guarded)
   and delegates to `_trimMsgForPersist` (bundle-window scope). No
   DOM, no globals, load-order-safe.
   ═══════════════════════════════════════════════════════════════════ */
function _lightMessageForSync(m) {
  let r = m;
  if (m.images?.length > 0)
    r = {
      ...r,
      images: m.images.map((img) => {
        const o = { mediaType: img.mediaType, sizeKB: img.sizeKB };
        if (img.url) {
          // Persist the canonical '/api/images/<f>' url unchanged, but the
          // preview is a render src — prefix with apiUrl() so it resolves
          // through the reverse-proxy base path.
          o.url = img.url;
          o.preview = (img.url.charAt(0) === "/" && typeof apiUrl === "function")
            ? apiUrl(img.url) : img.url;
        } else {
          o.preview = (img.preview || "").slice(0, 200) + "...";
        }
        if (img.pdfPage) o.pdfPage = img.pdfPage;
        if (img.pdfTotal) o.pdfTotal = img.pdfTotal;
        if (img.pdfName) o.pdfName = img.pdfName;
        if (img.caption) o.caption = img.caption;
        return o;
      }),
    };
  if (m.pdfTexts?.length > 0)
    r = {
      ...r,
      pdfTexts: m.pdfTexts.map((p) => ({
        name: p.name,
        pages: p.pages,
        textLength: p.textLength,
        isScanned: p.isScanned,
        method: p.method,
        text: p.text || "",
      })),
    };
  /* ★ `_pendingSync` is a CLIENT-ONLY durability marker (see docstring). */
  if (r._pendingSync) { r = { ...r }; delete r._pendingSync; }
  /* ★ Drop transient bloat (usage._wire_fp diagnostics, done-round
   *   _partialOutput) so a client PUT never re-inflates the DB payload
   *   the server-side sanitizer just trimmed. See _trimMsgForPersist. */
  r = _trimMsgForPersist(r);
  return r;
}


if (typeof window !== 'undefined') {
  window._stripUsageTransient = _stripUsageTransient;
  window._trimMsgForPersist = _trimMsgForPersist;
  window._lightMessageForSync = _lightMessageForSync;
  window._isErrorOnlyAssistant = _isErrorOnlyAssistant;
  window._rebaseUnackedTail = _rebaseUnackedTail;
  // _serverHasSegmentsLocalLacks / _serverHasTranslationLocalLacks /
  // _serverHasImagesLocalLacks are already exposed inline within their
  // function definitions above.
}
