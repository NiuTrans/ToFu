/* ═══════════════════════════════════════════════════════════════════
   main send pipeline — extracted from main.js (split 2026-05-28)

   Send pipeline: startAssistantResponse, sendMessage, autopilot followup, queue UI, queued-task reconnect.

   This file is concatenated by lib/js_bundler.py BEFORE main.js so
   the boot IIFE can reference these symbols. Symbols share `window`
   scope — no imports / exports needed.
   ═══════════════════════════════════════════════════════════════════ */

/**
 * Ask the human how to deliver a message SENT WHILE a turn is generating.
 * Shown ONLY when a task is already running for `convId` (caller-gated). The
 * dialog auto-closes to the safe default ('queue') if that running turn ends
 * while it's open (liveCheck), so a moot choice never lingers on screen.
 *
 * @param {string} convId
 * @returns {Promise<'steer'|'queue'>}
 */
async function _promptInjectMode(convId) {
  if (typeof showChoice !== 'function') return 'queue';  // dialog base not loaded
  const _tt = (k, d) => (typeof t === 'function' ? (t(k) !== k ? t(k) : d) : d);
  // Inline SVGs (§3.4 — no emoji). steer = pen-to-line (interject into the
  // live reply); queue = stacked lines (a fresh turn in line).
  const steerIcon =
    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>';
  const queueIcon =
    '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>';
  const choice = await showChoice({
    title: _tt('inject.promptTitle', '正在生成中 — 这条消息怎么发送？'),
    options: [
      {
        value: 'steer',
        label: _tt('inject.labelSteer', '插入当前回复'),
        subtitle: _tt('inject.subSteer', '在下一个工具调用边界注入,让模型立刻看到'),
        icon: steerIcon,
        accent: true,
      },
      {
        value: 'queue',
        label: _tt('inject.labelQueue', '排到下一轮'),
        subtitle: _tt('inject.subQueue', '当前回复结束后作为新一轮自动发送'),
        icon: queueIcon,
      },
    ],
    dismissValue: 'queue',
    liveCheck: () => {
      const c = conversations.find((x) => x.id === convId);
      return !!(c && (c.activeTaskId || activeStreams.has(convId)));
    },
  });
  return choice === 'steer' ? 'steer' : 'queue';
}
if (typeof window !== 'undefined') window._promptInjectMode = _promptInjectMode;

async function startAssistantResponse(convId) {
  const conv = conversations.find((c) => c.id === convId);
  if (!conv || activeStreams.has(convId) || conv.activeTaskId) return;
  /* ★ Ensure messages are loaded — Case E recovery can call this for _needsLoad shell convs
   *   that were detected as orphans via metadata (lastMsgRole/lastMsgTimestamp).
   *   Without loading first, conv.messages is empty → the server-side message builder has no context. */
  if (conv._needsLoad) {
    console.info(`[startAssistantResponse] Loading messages for shell conv ${convId.slice(0,8)} before starting`);
    await loadConversationMessages(convId);
    if (conv.messages.length === 0) {
      console.warn(`[startAssistantResponse] conv ${convId.slice(0,8)} still has 0 messages after load — aborting`);
      return;
    }
  }
  /* ★ Use per-conv model — config.model is global and may reflect a different conv */
  const _convModel = (convId === activeConvId) ? (config.model || serverModel) : (conv.model || serverModel);
  /* ★ Mint the assistant message's stable id BEFORE the POST (same as the send
   *   / regenerate paths) so live per-round translation frames route to THIS
   *   bubble while it streams — it has no DB index yet. Shipped to the backend
   *   via config.assistantMsgId below; create_task copies it to
   *   task['_assistantMsgId']. Reused on the assistantMsg object + stamped on
   *   the bubble (data-msg-id) so the in-stream preview and the final committed
   *   translation address the same message. */
  const _saMsgId = (typeof _newClientMsgId === 'function')
    ? _newClientMsgId()
    : ('tmp_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8));
  const assistantMsg = {
    role: "assistant",
    content: "",
    thinking: "",
    timestamp: Date.now(),
    toolRounds: [],
    model: _convModel,
    _msgId: _saMsgId,
  };
  _ensureMsgId(assistantMsg);  // no-op when _msgId already set
  conv.messages.push(assistantMsg);
  if (activeConvId === convId) {
    const inner = document.getElementById("chatInner");
    if (inner) {
      /* ★ Endpoint mode: show Planner avatar/role from the start.
       * The endpoint_iteration(planning) event may fire before SSE connects,
       * so the initial bubble must already show "Planner". */
      const _isEndpoint = (convId === activeConvId) ? endpointEnabled : (!!conv.endpointEnabled);
      if (_isEndpoint) assistantMsg._isEndpointPlanner = true;
      const role = _isEndpoint ? 'planner' : 'worker';
      /* ★ Route through ConvView.startStreaming so the insert is deduped at the
       *   boundary (_evictByMsgId removes any node already bound to this _msgId
       *   + any stale #streaming-msg). No raw fallback — the boot-time ConvView
       *   hard check (main.js) turns a missing seam into a loud startup failure. */
      window.ConvView.startStreaming(convId, { role, msgId: _saMsgId });
      const el = document.getElementById('streaming-msg');
      if (el) {
        el.classList.add('message-new');
        el.addEventListener('animationend', () => el.classList.remove('message-new'), { once: true });
      }
      scrollToBottom();
    }
  }
  /* ★ Don't persist the empty assistant message yet — wait until we have a taskId.
        This prevents a "ghost" empty message if the user refreshes before POST returns. */
  buildTurnNav(conv);
  // ★ Server-side message building: the backend loads messages from DB,
  //   so we MUST sync to server BEFORE the POST to ensure DB is up-to-date.
  //   We sync all messages EXCEPT the trailing empty assistant (it's just a
  //   UI placeholder — the backend doesn't need it).
  await syncConversationToServer(conv);
  // Debug panel: single source of truth is the backend. The DB was just
  // synced above, so restoreDebugForConv fetches the exact api-form messages
  // the LLM will see (via /debug-messages → build_api_messages_from_db),
  // instead of the raw, not-API-processed client-side conv.messages. The
  // subsequent messages_snapshot SSE updates it in place as rounds run.
  if (typeof restoreDebugForConv === "function") restoreDebugForConv(convId);
  let taskId;
  /* ★ Use shared _buildConvConfig to avoid config divergence.
   * startAssistantResponse is now only used by continueAssistant() and
   * Case E orphan recovery. sendMessage and regenerateFromUser use the
   * atomic /api/chat/send and /api/chat/regenerate endpoints instead. */
  const baseConfig = await _buildConvConfig(conv);
  /* ★ Ship the assistant msg id minted above so the backend stamps it on the
   *   task (task['_assistantMsgId']) → live per-round translation frames route
   *   to the streaming bubble. Endpoint mode ignores it (its own translate
   *   path), so this is harmless when _ep is true. */
  baseConfig.assistantMsgId = _saMsgId;
  const _ep = !!baseConfig.endpointMode;
  const _pp = baseConfig.projectPath;
  /* Route through the unified Api client (CLAUDE.md §3.2.0) — endpoint mode
   * uses Api.endpoint.start, normal mode Api.chat.start. Both return the raw
   * Response (parse:'response') with the client's own timeout disabled, so
   * the 30s AbortSignal below stays the only startup timeout, and the
   * caller's resp.ok/resp.json() handling is byte-identical to the old raw
   * fetch. Abort/Timeout DOMExceptions are rethrown verbatim by the client
   * so the e.name branches in the catch keep working. */
  const _startBody = { convId, config: baseConfig };
  const _startSignal = typeof AbortSignal.timeout === 'function'
    ? AbortSignal.timeout(30000)    // 30s timeout to prevent infinite hang
    : undefined;
  try {
    const resp = _ep
      ? await Api.endpoint.start(_startBody, { signal: _startSignal })
      : await Api.chat.start(_startBody, { signal: _startSignal });
    if (!resp.ok) {
      const err = await resp
        .json()
        .catch(() => ({ error: `HTTP ${resp.status}` }));
      throw new Error(err.error || `Server ${resp.status}`);
    }
    const data = await resp.json();
    taskId = data.taskId;
    debugLog(
      `Task started: ${taskId} (${_ep ? "ENDPOINT" : "normal"} search:${baseConfig.searchMode} fetch:${baseConfig.fetchEnabled} project:${_pp ? "yes" : "no"} browser:${baseConfig.browserEnabled} autoApply:${baseConfig.autoApply}${convId === activeConvId ? "" : " bg-conv"})`,
      "success",
    );
  } catch (e) {
    const errMsg = e.name === 'TimeoutError'
      ? 'Request timed out — server may be overloaded or unreachable. Try restarting the server.'
      : e.name === 'AbortError' ? 'Request was aborted'
      : e.message;
    debugLog("Failed: " + errMsg, "error");
    console.error('[startAssistantResponse] POST failed:', e.name, e.message);
    /* Convert client-side request failure to a typed error envelope so the
     * error-block renders the same way as backend-emitted errors. */
    const _kind = e.name === 'TimeoutError' ? 'timeout'
      : e.name === 'AbortError' ? 'aborted'
      : 'network';
    /* Fix the empty-hint hole at the source: a client-side chat-start failure
     * MUST carry actionable guidance so it doesn't depend on which path
     * stamped the error. This is NOT the recoverable server_offline case — the
     * POST that starts the turn failed, so no task ran server-side and there is
     * nothing to recover. The honest advice is check the connection and Retry
     * (renderErrorEnvelope shows NO Recover button for kind=network). */
    const _hint = _kind === 'network'
      ? ((typeof t === 'function') ? t('err.net.hint')
          : "Couldn't reach the server — check your network or proxy, then Retry.")
      : '';
    assistantMsg.error = normalizeErrorEnvelope({
      kind: _kind,
      severity: _kind === 'aborted' ? 'warning' : 'error',
      retryable: _kind !== 'aborted',
      message: errMsg,
      hint: _hint, detail: e.message || '',
      model: baseConfig.model || '', context: 'chat-start', source: 'frontend-fetch', raw: e.message || '',
    });
    /* Funnel the streaming-finalize through the unified controller so
     * scroll preservation + truncation guard live in one place. */
    if (activeConvId === convId) {
      window.ConvView.finalizeStreaming(convId, assistantMsg);
    }
    saveConversations(convId);
    /* ★ Sync to server even on error — this conversation only exists in
     * memory until synced.  Without this, a page refresh loses it. */
    syncConversationToServer(conv);
    buildTurnNav(conv);
    return;
  }
  /* ★ Now we have taskId — persist atomically: activeTaskId + assistantMsg together */
  conv.activeTaskId = taskId;
  /* ★ CROSS-TALK DETECTION: log task→conv binding at creation time */
  console.info(
    `[startAssistantResponse] 🎯 Task bound: task=${taskId.slice(0,8)} → conv=${convId.slice(0,8)} ` +
    `msgs=${conv.messages.length} isActive=${activeConvId === convId} ` +
    `otherActiveStreams=[${[...activeStreams.keys()].filter(k=>k!==convId).map(k=>k.slice(0,8)).join(',')}]`
  );
  saveConversations(convId);
  syncConversationToServer(conv);
  /* ★ Only manipulate DOM if this conv is still active */
  if (activeConvId === convId) {
    const oldSm = document.getElementById("streaming-msg");
    if (oldSm) oldSm.remove();
  }
  connectToTask(convId, taskId);
}

async function sendMessage() {
  // ── Image Gen mode intercept: redirect to direct generation ──
  if (imageGenMode) { generateImageDirect(); return; }
  // ── Branch mode intercept: redirect to branch if active ──
  if (typeof isBranchModeActive === "function" && isBranchModeActive()) {
    const branchCtx = getActiveBranchContext();
    if (branchCtx) {
      const input = document.getElementById("userInput");
      const text = (input?.value || "").trim();
      if (!text && pendingImages.length === 0) return;
      // Collect images and clear, then delegate to branch sender
      const imgs = [...pendingImages];
      pendingImages = [];
      renderImagePreviews();
      input.value = "";
      input.style.height = "auto";
      sendBranchMessage(text, imgs.length ? imgs : null);
      return;
    }
  }
  const input = document.getElementById("userInput");
  const text = input.value.trim();
  if (!text && pendingImages.length === 0 && pendingPdfTexts.length === 0)
    return;
  // 2026-05-06 (Option C): `pdfProcessing` is now a COUNTER (# of in-flight
  // text-extract calls), not a single-flight mutex. Don't silently drop the
  // send when text parses are still running — instead, the VLM-wait loop
  // downstream will block once entries also have vlmStatus='parsing'. But
  // if text parse itself is still running (no vlmStatus yet set), we poll
  // until every entry has either text or a failed state.
  if ((pdfProcessing | 0) > 0 || pendingPdfTexts.some(p => p && p.method === 'parsing')) {
    // Non-blocking: let sendMessage proceed; _waitForPdfTextThenVlm below will
    // await both stages. We annotate the input UI so the user sees the wait.
    debugLog(`[Upload] Waiting for ${pendingPdfTexts.filter(p => p.method === 'parsing').length} PDF text parse(s) to finish before sending…`, 'info');
  }
  // If log noise banner is showing, ask user first
  if (_pendingLogClean) {
    const r = _pendingLogClean;
    const opsDesc = r.ops.map((o) => o.desc).join("、");
    const doClean = await showConfirm(
      t('send.logNoiseConfirm', { chars: r.savedChars.toLocaleString(), pct: r.savedPct, ops: opsDesc }),
      { okText: t('send.logNoiseClean'), cancelText: t('send.logNoiseKeep') },
    );
    if (doClean) {
      input.value = input.value.replace(r.originalText, r.cleanedText);
      debugLog(
        `Log noise auto-cleaned on send: saved ${r.savedChars} chars`,
        "success",
      );
    }
    hideLogCleanBanner();
    if (doClean) {
      // Re-read the cleaned text
      const newText = input.value.trim();
      if (
        !newText &&
        pendingImages.length === 0 &&
        pendingPdfTexts.length === 0
      )
        return;
    }
  }
  const finalText = input.value.trim();
  if (!finalText && pendingImages.length === 0 && pendingPdfTexts.length === 0)
    return;
  const sendGen = ++_sendGeneration;   // ★ capture generation for staleness checks
  let conv = getActiveConv();
  /* Ensure messages are loaded before sending */
  if (conv && conv._needsLoad) await loadConversationMessages(conv.id);
  /* ★ Race guard: user switched conv during the await above */
  if (_sendGeneration !== sendGen) { console.log('[sendMessage] aborted — conv switched during loadMessages'); return; }

  if (!conv) {
    const now = Date.now();
    conv = {
      id: generateId(),
      title: "New Chat",
      messages: [],
      createdAt: now,
      updatedAt: now,
      activeTaskId: null,
    };
    /* ★ FIX: Inherit projectPath from the currently visible projectState.
     * When the user clicks "New Chat" while a project is active and types
     * immediately (hasInput=true), the project UI stays visible but the new
     * conv object had no projectPath → backend received empty string → tools
     * disabled. Now the new conv inherits the displayed project. */
    if (projectState.active && projectState.path) {
      conv.projectPath = projectState.path;
      const allPaths = [projectState.path];
      if (projectState.extraRoots?.length) {
        for (const r of projectState.extraRoots) {
          const p = typeof r === 'string' ? r : r.path;
          if (p && !allPaths.includes(p)) allPaths.push(p);
        }
      }
      conv.projectPaths = allPaths;
    }
    /* ★ Auto-assign to active folder when creating a new conversation in folder view */
    const _curFolderId = typeof getActiveFolderId === 'function' ? getActiveFolderId() : null;
    if (_curFolderId) conv.folderId = _curFolderId;
    conversations.unshift(conv);
    activeConvId = conv.id;
    sessionStorage.setItem('tofu_activeConvId', conv.id);
    _saveConvToolState();
    renderConversationList();
  }
  const convId = conv.id;

  // ── Build message payload for backend ──
  const msgPayload = {
    text: finalText,
    images: [...pendingImages],
    pdfTexts: [...pendingPdfTexts],
    timestamp: Date.now(),
    // ★ Mint the turn's stable _msgId HERE and ship it to the server, so the
    //   persisted server row and the optimistic client row share ONE identity.
    //   Without this the server mints its own UUID (build_user_msg_from_payload
    //   → _assign_message_ids), and on a lost-ACK poor-network send the
    //   rescue-PUT rebase (keyed on _msgId) fails to match the server's copy
    //   and appends a DUPLICATE user bubble. Uses the same _newClientMsgId()
    //   generator _ensureMsgId uses, so the id format is consistent and the
    //   server (which only fills MISSING _msgId in _assign_message_ids) keeps it.
    _msgId: (typeof _newClientMsgId === 'function') ? _newClientMsgId()
            : ('tmp_' + Date.now() + '_' + Math.random().toString(36).slice(2, 10)),
  };
  // ── Per-turn context snapshot: freeze the workspace/tools/model that
  //    are active right now so each turn carries a note of what it ran
  //    with (rendered on the right of the user bubble). Persisted by the
  //    backend (whitelisted in build_user_msg_from_payload) so it survives
  //    reload. See static/js/info-rail.js.
  const _turnCtx = (typeof buildTurnCtxSnapshot === 'function') ? buildTurnCtxSnapshot() : null;
  if (_turnCtx) msgPayload.ctx = _turnCtx;
  // Reply quotes
  if (typeof getPendingReplyQuotes === "function") {
    const rqs = getPendingReplyQuotes();
    if (rqs && rqs.length > 0) {
      msgPayload.replyQuotes = rqs;
      clearReplyQuote();
    }
  }
  // Conversation references — send only {id, title}, backend resolves full text
  if (typeof getPendingConvRefs === "function") {
    const crs = getPendingConvRefs();
    if (crs && crs.length > 0) {
      msgPayload.convRefs = crs;
      clearConvRefs();
    }
  }
  // Auto-assign folder
  const _curFolderId = typeof getActiveFolderId === 'function' ? getActiveFolderId() : null;
  if (_curFolderId) msgPayload.folderId = _curFolderId;

  // ── Optimistic UI: render user message immediately ──
  // NOTE: We render the user bubble and dismiss the welcome card BEFORE the
  // VLM wait loop. The wait indicator then lives inside a normal conversation
  // view instead of floating under the welcome screen (which looked like the
  // welcome page itself was frozen parsing). See bug fix 2026-05-06.
  const userMsg = {
    role: "user",
    content: finalText,
    images: msgPayload.images,
    pdfTexts: msgPayload.pdfTexts,
    timestamp: msgPayload.timestamp,
    _msgId: msgPayload._msgId,
  };
  if (msgPayload.replyQuotes) userMsg.replyQuotes = msgPayload.replyQuotes;
  if (msgPayload.convRefs) userMsg.convRefs = msgPayload.convRefs;
  if (_turnCtx) userMsg._ctx = _turnCtx;
  _ensureMsgId(userMsg);  // no-op — _msgId already set from msgPayload

  conv._needsLoad = false;
  conv.messages.push(userMsg);
  const userMsgIdx = conv.messages.length - 1;
  /* ★ Mark this conv as having an in-flight POST /chat/send. The backend's
   *   _chat_send is the sole owner of the first-turn persist (it appends a
   *   fresh translated user_msg + writes the row).  Any concurrent
   *   syncConversationToServer / loadConversationsFromServer "rescue
   *   local-only conv" PUT in this window would plant the optimistic
   *   untranslated msg as row #0, and chat_send would then append its own
   *   translated msg as row #1 → duplicate user message.  syncConversationToServer
   *   reads this flag and skips the PUT; cleared in finally below. */
  conv._sendInFlight = true;
  /* ★ Diagnostic breadcrumb: this is the optimistic push that races
   *   with loadConversationMessages Phase 2.  If a chatInner-disappearance
   *   bug recurs, grep logs/app.log for this line + the matching
   *   "[loadConvMsgs] 📊 Phase2 reconcile" entry to see the order of
   *   events. */
  console.info(
    `[sendMessage] 📝 Pushed optimistic user msg conv=${convId.slice(0,8)} ` +
    `idx=${userMsgIdx} ts=${userMsg.timestamp} totalMsgs=${conv.messages.length} ` +
    `activeTaskId=${conv.activeTaskId ? String(conv.activeTaskId).slice(0,8) : 'null'}`
  );
  if (conv.messages.filter((m) => m.role === "user").length === 1 && finalText) {
    const titleText = stripNoTranslateTags(finalText);
    conv.title = titleText.slice(0, 60) + (titleText.length > 60 ? "..." : "");
    if (activeConvId === convId)
      document.getElementById("topbarTitle").textContent = conv.title;
    renderConversationList();
  }
  input.value = "";
  input.style.height = "auto";
  pendingImages = [];
  pendingPdfTexts = [];
  if (typeof _vlmClearState === 'function') _vlmClearState();
  renderImagePreviews();
  document.getElementById("pdfProgress").style.display = "none";
  if (activeConvId === convId) {
    const w = document.getElementById("welcome");
    if (w) w.remove();
    window.ConvView.apply(convId, userMsgIdx, userMsg);
    const newEl = document.getElementById("msg-" + userMsgIdx);
    if (newEl) {
      newEl.classList.add("message-new");
      newEl.addEventListener('animationend', () => newEl.classList.remove('message-new'), { once: true });
    }
    /* ★ SCROLL FIX (send-at-bottom lands mid-history): use the real-height
     *   path, NOT plain scrollToBottom. `.message` carries
     *   content-visibility:auto + contain-intrinsic-size:auto 120px, so a
     *   single-rAF `scrollTop = scrollHeight` reads an UNDER-estimated height
     *   whenever a rendered bubble's real size isn't cached — lazy-loaded
     *   older bubbles (_loadOlderMessages never runs a cv-off pass) or a
     *   far-off-screen bubble whose cached size the browser evicted. It then
     *   clamps short and the reader is parked in the middle. _forceScrollToBottom
     *   flips cv-off (content-visibility:visible → real heights), forces a
     *   reflow, and re-asserts across double-rAF + a 150ms timer — the exact
     *   dance conversation-open uses. */
    _forceScrollToBottom(null, true);
  }

  /* ★ pt_c03fae11 — the region between `_sendInFlight = true` (above) and
   *   the POST's try/finally (below) must NEVER leak the flag. A throw from
   *   any of these awaits (VLM parse / config build / inject prompt) or the
   *   conv-switch early return would otherwise wedge `_sendInFlight` forever,
   *   and syncConversationToServer's guard would silently skip every future
   *   PUT for this conversation — the user's later edits vanish on refresh.
   *   On ANY exit before the POST the backend provably did not persist this
   *   turn, so the recovery mirrors the main catch's failed-send branch:
   *   clear the flag, rescue-sync, fall back to the pending-sync poller. */
  let _sendConfig;
  let _assistantMsgId;
  let _injectMode = 'queue';
  // ★ Inject-mode decision — the optimistic user bubble is ALREADY in the
  //   timeline (inserted above). If a turn is CURRENTLY generating for this
  //   conversation, ask the human HOW to deliver this message: steer it into
  //   the running reply (injected at the next tool-call boundary) or queue it
  //   as a fresh turn. When nothing is running this is a normal send — no
  //   prompt, no extra UI — and injectMode stays 'queue' (a server-side no-op
  //   with no running task). See routes/chat.py's has_running_task branch.
  const _turnRunning = () => !!(conv.activeTaskId || activeStreams.has(convId));
  try {
    // ── Wait for VLM parsing before sending (after user bubble is visible) ──
    await _waitForVlmParsing(userMsg, convId, userMsgIdx);
    msgPayload.pdfTexts = userMsg.pdfTexts;

    // ── Atomic backend call: message creation + translate + task start ──
    _sendConfig = await _buildConvConfig(conv);

    // ★ Mint the assistant message's stable id BEFORE the POST and ship it to
    //   the backend (config.assistantMsgId). The server stamps it on the task
    //   (task['_assistantMsgId']) so live per-round translation frames route to
    //   THIS message while it's still streaming (it has no DB index yet), and
    //   the streaming bubble is stamped with the same data-msg-id below. Reused
    //   verbatim for the assistantMsg object so the in-stream preview and the
    //   final committed translation address the same message.
    _assistantMsgId = (typeof _newClientMsgId === 'function')
      ? _newClientMsgId()
      : ('tmp_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8));
    _sendConfig.assistantMsgId = _assistantMsgId;

    if (_turnRunning()) {
      _injectMode = await _promptInjectMode(convId);
      // Race: the running turn may have ENDED while the prompt was open. If so,
      // this is now a plain send (the backend's drainable check would fall a
      // 'steer' back to the queue anyway) — _promptInjectMode already auto-closed
      // via liveCheck and resolved to the safe default, so nothing to fix here.
      if (_sendGeneration !== sendGen) {
        console.log('[sendMessage] aborted — conv switched during inject-mode prompt');
        /* The bubble stays for editing (same semantics as the user-stop-
         * during-translate branch), but the backend never saw this send —
         * clear the in-flight marker and rescue-sync now, or the message
         * survives only in memory and vanishes on the next refresh. */
        conv._sendInFlight = false;
        const _syncedAbort = await syncConversationToServer(conv);
        if (!_syncedAbort) markConvPendingSync(conv);
        return;
      }
    }
  } catch (preSendErr) {
    /* Pre-POST failure (VLM parse / config build / inject prompt threw) — the
     * backend provably did not persist this turn. Same durability contract as
     * the main catch's generic branch: visible error bubble + clear the flag
     * BEFORE the rescue sync so its guard does not skip the PUT + pending-
     * sync fallback when the network itself is the problem. */
    console.error('[sendMessage] pre-send stage failed:', preSendErr);
    debugLog('Failed: ' + ((preSendErr && preSendErr.message) || preSendErr), 'error');
    conv._sendInFlight = false;
    const errAssistant = {
      role: "assistant", content: "", thinking: "",
      error: (preSendErr && preSendErr.message) || String(preSendErr),
      timestamp: Date.now(), toolRounds: [],
    };
    _ensureMsgId(errAssistant);
    conv.messages.push(errAssistant);
    if (activeConvId === convId) {
      window.ConvView.apply(convId, conv.messages.length - 1, errAssistant);
    }
    saveConversations(convId);
    const _syncedPre = await syncConversationToServer(conv);
    if (!_syncedPre) markConvPendingSync(conv);
    buildTurnNav(conv);
    return;
  }

  // ★ If autoTranslate is on and text has Chinese, show stop button immediately
  //   so the user can abort during server-side translation.
  // ★ Safety timer must comfortably exceed the backend translate cap
  //   (_TRANSLATE_SEND_TIMEOUT in routes/chat.py — 45 s) so the request gets
  //   a chance to fail cleanly with a concrete reason instead of being
  //   killed locally by an opaque AbortError.
  const _sendAbortCtrl = new AbortController();
  let _sendAbortReason = '';  // '' | 'timeout' | 'user-stop' | 'unmount'
  const _sendTimeout = setTimeout(() => {
    _sendAbortReason = 'timeout';
    _sendAbortCtrl.abort();
  }, 90000);
  const _willTranslate = _sendConfig.autoTranslate && /[\u4e00-\u9fff\u3400-\u4dbf]/.test(finalText);
  if (_willTranslate) {
    conv._translating = true;
    conv._translateAborted = false;
    conv._translateAbortCtrl = _sendAbortCtrl;
    updateSendButton();
    renderConversationList();
    if (activeConvId === convId) _renderTranslatingBubble();
  } else if (activeConvId === convId) {
    /* ★ No translation → the assistant side would otherwise be BLANK for the
     *   whole synchronous /api/chat/send POST (load → task-start). Render a
     *   deterministic '连接中…' placeholder on the SAME #translating-msg node so
     *   every exit path's existing _removeTranslatingBubble() tears it down,
     *   then upgrade it in place to the streaming bubble once the POST returns
     *   the taskId. This closes the send-side "dead-zone" (fix ①). */
    _renderTranslatingBubble(t('sidebar.connecting'));
  }

  try {
    // ★ If the user just aborted a task, pass its ID so the backend can
    //   abort it immediately — even if the fire-and-forget abort fetch
    //   hasn't arrived yet. This eliminates the race where the send
    //   arrives before the abort and the backend incorrectly enqueues.
    const _sendSettings = await _buildConvSettings(conv);
    const _sendBody = {
        convId,
        message: msgPayload,
        config: _sendConfig,
        settings: _sendSettings,
        /* Composer inject lane — only meaningful server-side when a task is
         * already running for this conversation. Decided by a post-send prompt
         * (_promptInjectMode) shown ONLY in that case; a normal (idle) send
         * never prompts and leaves this 'queue' (a harmless no-op server-side
         * when nothing is running). */
        injectMode: _injectMode,
    };
    if (conv._lastAbortedTaskId) {
      _sendBody.abortTaskId = conv._lastAbortedTaskId;
      delete conv._lastAbortedTaskId;
    }
    /* Refresh the context-health gauge at the moment the request goes out
     * so the user sees their pre-request reading captured BEFORE the model
     * starts streaming.  Each subsequent round is covered by the
     * 'phase: llm_thinking' SSE branch in ui.js. */
    if (typeof updateContextBar === 'function') updateContextBar();
    /* ★ #5 idempotency: key the send on the optimistic user message's stable
     *   _msgId (assigned once, above). A network retry of THIS send reuses the
     *   same key → the backend's @idempotent_post replays the cached {taskId}
     *   instead of spawning a duplicate task (and double-charging tokens).
     *   A genuinely new send gets a new _msgId → new key. */
    const _sendOpts = { signal: _sendAbortCtrl.signal };
    if (userMsg._msgId) _sendOpts.headers = { 'Idempotency-Key': String(userMsg._msgId) };
    const resp = await Api.chat.send(_sendBody, _sendOpts);
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ error: `HTTP ${resp.status}` }));
      throw new Error(err.error || `Server ${resp.status}`);
    }
    const result = await resp.json();

    // Update local state with server response
    if (result.userMessage) {
      // Server may have translated the message — update local copy
      Object.assign(userMsg, result.userMessage);
      if (activeConvId === convId) {
        const msgEl = document.getElementById('msg-' + userMsgIdx);
        if (msgEl) window.ConvView.apply(convId, userMsgIdx, userMsg);
      }
    }
    if (result.title && result.title !== conv.title) {
      conv.title = result.title;
      if (activeConvId === convId)
        document.getElementById("topbarTitle").textContent = conv.title;
      renderConversationList();
    }
    conv._serverMsgCount = result.msgCount || conv.messages.length;

    // ★ Backend injected this as a STEER into the currently-running turn
    //   (composer inject-mode = steer). The steer is NOT persisted as a
    //   standalone user message — the backend represents it as a display-only
    //   chip on the RUNNING assistant turn (USER_STEER_INJECT → the
    //   _userSteerInjects sidecar, rehydrated by _rehydrateInjectRows). So we
    //   MUST splice the optimistic user bubble here, exactly like the queue
    //   lane: leaving it would show a phantom user bubble live that vanishes
    //   into a chip on reload (the two states must match).
    if (result.steered) {
      console.log(
        `%c[Steer] ➡ Server injected message into running turn for conv=${convId.slice(0,8)}`,
        'color:#34d399;font-weight:bold'
      );
      if (conv.messages[userMsgIdx] === userMsg) {
        conv.messages.splice(userMsgIdx, 1);
        if (activeConvId === convId) {
          window.ConvView.removeMessage(convId, userMsg);
        }
      }
      saveConversations(convId);
      if (activeConvId === convId) {
        _removeTranslatingBubble();
      }
      buildTurnNav(conv);
      renderConversationList();
      updateSendButton();
      debugLog(t('steer.injected'), 'info');
      if (typeof showToast === 'function') showToast(t('steer.injected'), 'success');
      return;  // ← exit try block, falls through to finally
    }

    // ★ Backend decided to queue (active task running for this conversation)
    if (result.queued) {
      console.log(
        `%c[Queue] ✚ Server queued message %c#${result.position}%c for conv=${convId.slice(0,8)} queueId=${result.queueId?.slice(0,8)}`,
        'color:#a78bfa;font-weight:bold', 'color:#fbbf24;font-weight:bold', 'color:#a78bfa'
      );
      // ★ Remove the optimistic user message from conv.messages and DOM —
      //   queued messages should only appear in the queue bar, not in chatInner.
      //   The backend did NOT persist this message to the conversation DB.
      if (conv.messages[userMsgIdx] === userMsg) {
        conv.messages.splice(userMsgIdx, 1);
        if (activeConvId === convId) {
          window.ConvView.removeMessage(convId, userMsg);
        }
      }
      // Sync queue state from server for accurate UI
      _refreshServerQueue(convId);
      /* ★ Start the dispatch watcher: the backend auto-starts this message
       *   when the current task settles — but NOTHING on this tab discovers
       *   that while no local stream is live (the busy turn's stream may
       *   already have closed, e.g. the invisible autopilot-VU window, or
       *   belong to another device). Bounded backoff; the notify-push hook
       *   (cross_tab_sync.js) is the event-driven complement that usually
       *   fires first. Without this, the queued turn starts INVISIBLY and
       *   the bar sits silent until a manual refresh (2026-07-25 incident). */
      _watchQueuedDispatch(convId);
      saveConversations(convId);
      if (activeConvId === convId) {
        _removeTranslatingBubble();
      }
      buildTurnNav(conv);
      renderConversationList();
      updateSendButton();
      debugLog(`消息已排队 (#${result.position})，将在当前回复结束后自动发送`, 'info');
      if (typeof showToast === 'function')
        showToast(t('queue.queuedToast', { n: result.position }), 'info');
      return;  // ← exit try block, falls through to finally
    }

    // Push empty assistant msg + connect to task SSE
    const taskId = result.taskId;
    const assistantMsg = {
      role: "assistant", content: "", thinking: "",
      timestamp: Date.now(), toolRounds: [],
      model: _sendConfig.model || serverModel,
      // ★ Same id we minted + shipped in config.assistantMsgId, so the live
      //   per-round translation preview (routed by this id) lands on this
      //   exact message object.
      _msgId: _assistantMsgId,
    };
    // ★ Endpoint mode: mark as planner so SSE reconnection identifies it correctly
    if (_sendConfig.endpointMode) assistantMsg._isEndpointPlanner = true;
    _ensureMsgId(assistantMsg);  // no-op when _msgId already set
    conv.messages.push(assistantMsg);
    conv.activeTaskId = taskId;
    saveConversations(convId);

    /* ★ Diagnostic: closes the [sendMessage] timeline.  After this point,
     *   loadConversationMessages Phase 2 reconciliation that finds
     *   conv.activeTaskId set will MERGE rather than OVERWRITE — so any
     *   chatInner-disappearance bug must have happened in the window
     *   BEFORE this line, between the optimistic-push and here. */
    console.info(
      `[sendMessage] 🎯 Task bound conv=${convId.slice(0,8)} task=${taskId.slice(0,8)} ` +
      `msgIdx=${conv.messages.length - 1} totalMsgs=${conv.messages.length}`
    );

    debugLog(`Task started: ${taskId} (send)`, "success");

    if (activeConvId === convId) {
      _removeTranslatingBubble();
      _renderStreamingBubble(conv, _sendConfig, _assistantMsgId);
    }
    buildTurnNav(conv);
    connectToTask(convId, taskId);

  } catch (e) {
    const _userClickedStop = !!conv._translateAborted;
    // ★ User-clicked-Stop during translation: keep message for editing,
    //   no error bubble. Timer-fired abort or any other failure: surface
    //   a visible error so the chat doesn't silently get stuck (the bug
    //   captured in the screenshot — spinner removed, no reply, no error).
    if (e.name === 'AbortError' && _willTranslate && _userClickedStop) {
      console.log('%c[sendMessage] ✗ Aborted during translation by user — keeping message for editing', 'color:#f59e0b;font-weight:bold');
      _removeTranslatingBubble();
      if (activeConvId === convId) {
        const msgEl = document.getElementById('msg-' + userMsgIdx);
        if (msgEl) window.ConvView.apply(convId, userMsgIdx, userMsg);
      }
      saveConversations(convId);
      /* ★ The send fetch failed/aborted, so the backend's _chat_send did NOT
       *   persist this turn — clear the in-flight marker BEFORE the rescue
       *   sync so it is not silently skipped by syncConversationToServer's
       *   `_sendInFlight` guard.  Without this the PUT is a no-op and the
       *   user's message survives only in the in-memory array → it vanishes
       *   on the next page refresh (the poor-network data-loss bug).  The
       *   duplicate the guard protects against cannot happen here: chat_send
       *   threw, so there is no concurrent backend persist. */
      conv._sendInFlight = false;
      /* ★ Durability: if the rescue PUT fails (the same poor network that
       *   failed the send), mark the turn _pendingSync + persist to
       *   IndexedDB and start the retry poller so the message survives a
       *   reload and is re-synced on reconnect. */
      const _synced = await syncConversationToServer(conv);
      if (!_synced) markConvPendingSync(conv);
      buildTurnNav(conv);
      Api.chat.abortConv(convId);
    } else if (e.name === 'AbortError' && _sendAbortReason === 'timeout'
               && await _recoverTimedOutChatTask(convId, { endpointMode: _sendConfig.endpointMode })) {
      // ★ Client safety timer fired, but the server had already started the
      //   task — we reconnected to its live stream. No error, no Retry needed.
      console.log('%c[sendMessage] ✓ Recovered timed-out request by reconnecting to live task', 'color:#34d399;font-weight:bold');
    } else {
      let errMsg;
      if (e.name === 'AbortError' && _sendAbortReason === 'timeout') {
        errMsg = _willTranslate
          ? 'The server took too long to respond and the request was cancelled — it may be overloaded. Please try again; if this keeps happening with Chinese input, disabling auto-translate in Settings can reduce the delay.'
          : 'Request timed out — the server took too long to respond.';
      } else if (e.name === 'TimeoutError') {
        errMsg = 'Request timed out — server may be overloaded.';
      } else if (e.name === 'AbortError') {
        errMsg = 'Request was aborted before the server replied.';
      } else {
        errMsg = e.message;
      }
      debugLog("Failed: " + errMsg, "error");
      console.error('[sendMessage] /api/chat/send failed:', e.name, e.message,
                    'abortReason=' + _sendAbortReason);
      // Show error on the user message
      const errAssistant = {
        role: "assistant", content: "", thinking: "",
        error: errMsg, timestamp: Date.now(), toolRounds: [],
      };
      _ensureMsgId(errAssistant);
      conv.messages.push(errAssistant);
      if (activeConvId === convId) {
        window.ConvView.apply(convId, conv.messages.length - 1, errAssistant);
      }
      saveConversations(convId);
      /* ★ Same as the user-stop branch above: the send failed, so the backend
       *   is NOT persisting this turn.  Clear `_sendInFlight` before the rescue
       *   sync — otherwise syncConversationToServer's guard skips the PUT and
       *   the user message + error bubble disappear on the next refresh
       *   (poor-network data-loss). await so the PUT has a chance to land
       *   before the user can reload. */
      conv._sendInFlight = false;
      /* ★ Durability: if the rescue PUT also fails (poor network), mark the
       *   turn _pendingSync + persist to IndexedDB and start the retry poller
       *   so the user message + error bubble survive a reload and are
       *   re-synced automatically once connectivity returns. */
      const _synced = await syncConversationToServer(conv);
      if (!_synced) markConvPendingSync(conv);
      buildTurnNav(conv);
    }
  } finally {
    clearTimeout(_sendTimeout);
    // ★ Clear the in-flight marker — backend has either persisted the row
    //   (success path, /chat/send committed) or we've appended an error
    //   bubble locally (catch path, error fallback already syncs).  Either
    //   way the rescue PUT path is now safe to run again.
    conv._sendInFlight = false;
    // ★ Unconditional teardown: the pre-POST placeholder (#translating-msg)
    //   is now rendered whether or not we translated (fix ①), so the '连接中…'
    //   variant must be removed here too — otherwise a generic-error exit
    //   (which does not remove it inline) leaves an orphaned placeholder.
    //   Idempotent no-op once the success path already swapped it for the
    //   streaming bubble.
    _removeTranslatingBubble();
    // ★ Always clean up translating state
    if (_willTranslate) {
      conv._translating = false;
      conv._translateAborted = false;
      conv._translateAbortCtrl = null;
      updateSendButton();
      renderConversationList();
    }
  }
}

// ══════════════════════════════════════════════════════
//  ★ Non-blocking background translation → auto-start assistant
// ══════════════════════════════════════════════════════

// ★ REMOVED: _translateThenRespond() — dead code.
// Translation is now handled atomically by the backend in /api/chat/send,
// /api/chat/regenerate, and dispatch_next_queued() in lib/message_queue.py.

// ══════════════════════════════════════════════════════
//  ★ Pending Message Queue — dispatch, UI, cancel
// ══════════════════════════════════════════════════════

/**
 * Count of DISPATCHABLE queued messages for a conversation.
 *
 * Mirrors the backend's `_get_queue_depth` (lib/message_queue.py), which
 * excludes the autopilot armed-marker sentinel (kind='autopilot').  That
 * sentinel is a persistent flag consumed by the end-of-turn autopilot hook,
 * NOT a turn that ever gets dequeued & dispatched as a task.  Every frontend
 * gate that means "is there pending work the backend will start next?" MUST
 * use this — using the raw Map length instead makes an armed-but-idle
 * autopilot look like a permanently stuck queued message (ghost "Dispatching…"
 * bubble + a doomed ~15s _checkForQueuedTask retry loop).
 */
function _dispatchableQueueCount(convId) {
  const q = pendingMessageQueue.has(convId) ? pendingMessageQueue.get(convId) : null;
  if (!q || q.length === 0) return 0;
  return q.filter((it) => it && it.kind !== 'autopilot').length;
}
if (typeof window !== 'undefined') window._dispatchableQueueCount = _dispatchableQueueCount;

// ★ REMOVED: _dispatchQueuedMessage() — dead code.
// Queue dispatch is now handled server-side by dispatch_next_queued() in
// lib/message_queue.py. The frontend polls for auto-dispatched tasks via
// _checkForQueuedTask() (called from finishStream in ui.js).

/**
 * Attach to an autopilot follow-up task that the backend pre-spawned.
 *
 * Called from finishStream() when the done event carried
 * `autopilotNextTaskId` + `autopilotVuMessage`.  The backend has already:
 *   1. Run the VU LLM and produced the synthetic user reply.
 *   2. Appended the VU message (`_isVirtualUser: true`) to conversations.messages.
 *   3. Created a new task and started its run_task thread.
 *
 * We just need to: append the VU msg + an empty assistant placeholder to
 * the local conv.messages, render, and connect to the new task's SSE
 * stream.  No polling, no /api/chat/active round-trip, no race with
 * VU LLM latency — the next-turn bubble starts streaming immediately.
 *
 * @param {string} convId
 * @param {{nextTaskId: string, vuMessage: object}} payload
 */
function _attachAutopilotFollowup(convId, payload) {
  const conv = conversations.find(c => c.id === convId);
  if (!conv) {
    console.warn(`[Autopilot] _attachAutopilotFollowup: conv=${convId.slice(0,8)} not found`);
    return;
  }
  const { nextTaskId, vuMessage } = payload || {};
  if (!nextTaskId || !vuMessage) {
    console.warn('[Autopilot] _attachAutopilotFollowup: missing nextTaskId or vuMessage', payload);
    return;
  }
  /* Baton consumed \u2014 clear the authoritative conv-level copy so a later
   * finishStream / poll doesn't re-dispatch the same follow-up. */
  delete conv._apPendingBaton;
  console.info(
    `%c[Autopilot] ▶ Attaching to follow-up task=${nextTaskId.slice(0,8)} for conv=${convId.slice(0,8)} ` +
    `vu="${(vuMessage.content||'').slice(0,80)}${(vuMessage.content||'').length>80?'…':''}"`,
    'color:#a78bfa;font-weight:bold'
  );

  /* ★ Streaming VU pipeline already pushed the VU bubble into
   *   conv.messages via the autopilot_vu_start / _vu_done SSE events
   *   (see static/js/ui.js:_handleAutopilotVuEvent).  In that case the
   *   message exists at its stable _msgId — just refresh its fields
   *   from the authoritative copy so this code path agrees with the
   *   one taken on cold-replay / late-connect (where the streaming
   *   events were missed and `vuMessage` from the done event is the
   *   only signal we have).  Without this dedup, the VU bubble would
   *   appear twice — once streamed, once stamped by this fn. */
  let _vuLocal = null;
  const _existingVuIdx = vuMessage._msgId
    ? conv.messages.findIndex(m => m && m._msgId === vuMessage._msgId)
    : -1;
  if (_existingVuIdx >= 0) {
    /* Reconcile in place — the streaming pipeline already pushed it. */
    const existing = conv.messages[_existingVuIdx];
    existing.content = vuMessage.content || existing.content || "";
    if (Array.isArray(vuMessage.toolRounds) && vuMessage.toolRounds.length) {
      existing.toolRounds = vuMessage.toolRounds;
    }
    delete existing._streamingVu;
    _vuLocal = existing;
    console.info(
      `[Autopilot] ✓ VU bubble already streamed at idx=${_existingVuIdx} ` +
      `(msgId=${(vuMessage._msgId||'').slice(0,12)}) — reconciled in place`
    );
  } else {
    _vuLocal = Object.assign({}, vuMessage);
    _ensureMsgId(_vuLocal);
    conv.messages.push(_vuLocal);
  }

  /* Empty assistant placeholder for the new task to stream into. */
  const assistantMsg = {
    role: 'assistant',
    content: '',
    thinking: '',
    timestamp: Date.now(),
    toolRounds: [],
  };
  _ensureMsgId(assistantMsg);
  conv.messages.push(assistantMsg);
  conv.activeTaskId = nextTaskId;
  conv._needsLoad = false;

  /* Remove any leftover streaming-msg placeholder from finishStream's
   * own queued-dispatch path (shouldn't exist on the autopilot path,
   * but be defensive). */
  if (activeConvId === convId) {
    try {
      const _ghost = document.getElementById('streaming-msg');
      if (_ghost) _ghost.remove();
    } catch (e) {
      debugLog(`[send-pipeline][autopilot] ghost placeholder cleanup failed conv=${convId.slice(0,8)}: ${e && e.message}`, 'error');
    }
    /* ★ Force-bypass the fingerprint guard.  Without `true`, renderChat's
     *   surgical path may keep the corrupted msg-N from finishStream's
     *   ConvView.finalizeStreaming call (which can land BEFORE we get
     *   here in some paths) instead of repainting from conv.messages. */
    window.ConvView.replaceAll(convId, { forceScroll: true });
  } else {
    /* ★ Background-conv autopilot: we just pushed VU + assistant
     *   placeholder into conv.messages but can't render now.  When the
     *   user switches back, loadConversation's MERGE_ACTIVE_TASK
     *   reconcile branch sees local.length === server.length (both
     *   include the assistant placeholder), skips the trailing-tail
     *   append, and never repaints the VU bubble.  Force a fresh load
     *   on next visit so the server's authoritative copy wins. */
    conv._needsLoad = true;
  }
  saveConversations(convId);
  /* ★ FIX: persist the VU message into IDB immediately so a page reload
   *   while the follow-up streams (or after it finishes on a tab where
   *   finishStream didn't run for THIS task) doesn't leave the cache
   *   stuck on the pre-VU snapshot.  saveConversations doesn't touch
   *   IDB; only ConvCache.put does. */
  try { if (typeof ConvCache !== 'undefined') ConvCache.put(conv); }
  catch (e) { console.warn('[Autopilot] ConvCache.put after VU push failed:', e); }
  renderConversationList();

  connectToTask(convId, nextTaskId);

  /* ★ DIAGNOSTIC (autopilot-invisible bug): snapshot the streaming
   * substrate immediately after connectToTask returns control.  All
   * five must be true for content to appear during the follow-up turn.
   * If any is false, the rAF/zone-cache/dangling-ref family is
   * implicated and the [StableId] / [twFlush-skip] logs will pinpoint
   * which member. */
  try {
    const _smEl = document.getElementById('streaming-msg');
    const _sbEl = document.getElementById('streaming-body');
    const _zc = (typeof _streamZoneCache !== 'undefined') ? _streamZoneCache : null;
    console.info(
      `[Autopilot][post-connect] conv=${convId.slice(0,8)} ` +
      `streamSessions=${typeof streamSessions !== 'undefined' && streamSessions.has(convId)} ` +
      `activeStreams=${activeStreams.has(convId)} ` +
      `streaming-msg=${!!_smEl} streaming-body=${!!_sbEl} ` +
      `zoneCacheBodyMatches=${!!(_zc && _zc.body && _zc.body === _sbEl)} ` +
      `lastMsgRole=${conv.messages[conv.messages.length-1]?.role} ` +
      `lastMsgId=${(conv.messages[conv.messages.length-1]?._msgId || '').slice(0,12)}`
    );
  } catch (_e) { /* best-effort diagnostic */ }
}


// ══════════════════════════════════════════════════════
//  ★ VLM Mandatory Wait — block until all PDF VLM parses finish
// ══════════════════════════════════════════════════════

/**
 * Wait for all VLM-parsing PDFs in a user message to complete.
 * Shows a waiting indicator in the chat area while blocking.
 * This ensures the LLM always sees VLM-quality text, never rule-based.
 */
async function _waitForVlmParsing(userMsg, convId, userMsgIdx) {
  if (!userMsg.pdfTexts || userMsg.pdfTexts.length === 0) return;
  // 2026-05-06 (Option C): with parallel uploads, some entries may still be
  // in the TEXT-parse phase when sendMessage fires. Wait for those first —
  // `method === 'parsing'` means the text-extract call is still in flight.
  const MAX_TEXT_WAIT = 300; // 5 min safety cap (large PDFs can take 2 min)
  for (let i = 0; i < MAX_TEXT_WAIT; i++) {
    const stillExtracting = userMsg.pdfTexts.filter(p => p && p.method === 'parsing');
    if (stillExtracting.length === 0) break;
    if (i === 0) {
      console.log(`%c[Upload-Wait] Waiting for ${stillExtracting.length} PDF text-parse(s)…`, 'color:#f59e0b;font-weight:bold');
    }
    await new Promise(r => setTimeout(r, 1000));
  }
  // Check if any PDFs are still VLM-parsing
  const parsing = userMsg.pdfTexts.filter(p => p.vlmStatus === 'parsing');
  if (parsing.length === 0) {
    console.log('%c[VLM-Wait] All PDFs already done, no wait needed', 'color:#22c55e');
    return;
  }
  console.log(`%c[VLM-Wait] Waiting for ${parsing.length} PDF(s) to finish VLM parsing…`, 'color:#f59e0b;font-weight:bold');
  // Show waiting indicator
  let _vlmIndicator = null;
  if (activeConvId === convId) {
    _vlmIndicator = document.createElement('div');
    _vlmIndicator.id = 'vlm-wait-indicator';
    _vlmIndicator.className = 'message';
    _vlmIndicator.innerHTML = '<div class="message-avatar"></div><div class="message-content"><div class="message-body"><div class="stream-status"><div class="pulse"></div> Waiting for VLM PDF parsing…</div></div></div>';
    /* Tail insert via the shared furniture-aware primitive — a raw appendChild
     * lands BELOW a bottom lazy-window sentinel. */
    const _vlmInner = document.getElementById('chatInner');
    if (_vlmInner) {
      if (typeof chatInnerInsert === 'function') {
        chatInnerInsert(_vlmInner, _vlmIndicator, {
          position: 'tail',
          conv: conversations.find((c) => c.id === convId) || null,
          site: '_waitForVlmParsing',
        });
      } else {
        _vlmInner.appendChild(_vlmIndicator);
      }
    }
    scrollToBottom();
  }
  // Poll until all PDFs finish VLM (done/done-skipped/failed/timeout/unavailable)
  const MAX_VLM_WAIT = 180; // 180 × 1s = 3 minutes max
  for (let attempt = 0; attempt < MAX_VLM_WAIT; attempt++) {
    await new Promise(r => setTimeout(r, 1000));
    const stillParsing = userMsg.pdfTexts.filter(p => p.vlmStatus === 'parsing');
    if (stillParsing.length === 0) {
      console.log(`%c[VLM-Wait] ✓ All PDFs VLM-done (waited ${attempt}s)`, 'color:#22c55e;font-weight:bold');
      break;
    }
    // Update indicator with progress (lightweight — only touches the indicator div)
    if (_vlmIndicator && activeConvId === convId) {
      const progParts = stillParsing.map(p => `${p.name.slice(0,15)}${p.vlmProgress ? ': ' + p.vlmProgress : ''}`);
      _vlmIndicator.querySelector('.stream-status').innerHTML =
        `<div class="pulse"></div> VLM parsing: ${progParts.join(', ')} (${attempt}s)`;
    }
    // NOTE: Do NOT re-render the user message (outerHTML) during the loop —
    // it destroys and recreates the DOM node, causing the browser to clamp
    // scrollTop when the old node is removed, which jumps scroll to the top.
    // The indicator already shows real-time progress; badge update happens once at the end.
  }
  // Final re-render to show completed VLM badges — preserve scroll position
  if (activeConvId === convId) {
    const chatC = document.getElementById('chatContainer');
    const savedTop = chatC ? chatC.scrollTop : 0;
    const msgEl = document.getElementById('msg-' + userMsgIdx);
    if (msgEl) window.ConvView.apply(convId, userMsgIdx, userMsg);
    if (chatC) chatC.scrollTop = savedTop;
  }
  // Remove indicator
  if (_vlmIndicator && _vlmIndicator.parentNode) {
    _vlmIndicator.remove();
  }
}

/* ── Queue item sources + collapse state ────────────────────────────
 * Every queued row has exactly ONE source, distinguished visually by a
 * tinted left edge + number badge + (for non-human sources) an attribution
 * line above the preview:
 *   own       — typed here, queued behind the running turn (neutral accent)
 *   agent     — project_message/intervene from a SIBLING agent conversation
 *   operator  — a HUMAN operator nudge delivered through the peer channel
 *   workflow  — a Project-Brain autonomous epic kickoff (KIND_WORKFLOW)
 *   autopilot — the armed sentinel (not a message; cancel = DISARM)
 */
function _queueSourceOf(item) {
  if (item.kind === 'autopilot') return 'autopilot';
  if (item.kind === 'workflow_step') return 'workflow';
  if (item.isPeerMessage) return item.isPeerHuman ? 'operator' : 'agent';
  return 'own';
}

/* Per-source SVG glyphs (no emoji / unicode-glyph icons — CLAUDE.md §3.4). */
const _QUEUE_SRC_ICONS = {
  autopilot: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3"/><path d="M12 3v6M12 15v6M3 12h6M15 12h6"/></svg>`,
  agent: `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="8" width="16" height="12" rx="2"/><path d="M12 8V5"/><circle cx="12" cy="3.5" r="1"/><path d="M9 13.5h.01M15 13.5h.01"/><path d="M9.5 17h5"/></svg>`,
  operator: `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="8" r="4"/><path d="M4.5 20.5c0-4 3.4-6.5 7.5-6.5s7.5 2.5 7.5 6.5"/></svg>`,
  workflow: `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="5" r="2.2"/><circle cx="6" cy="19" r="2.2"/><circle cx="18" cy="7" r="2.2"/><path d="M6 7.2v9.6"/><path d="M18 9.4c0 4.6-6.8 3.2-10.2 6.4"/></svg>`,
};
const _QUEUE_ICON_CHEVRON = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>`;
const _QUEUE_ICON_X = `<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M5 5l14 14M19 5L5 19"/></svg>`;
const _QUEUE_ICON_CLOUD = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.5 18.5a4.3 4.3 0 0 0 .8-8.5 5.5 5.5 0 0 0-10.8 1.4A3.8 3.8 0 0 0 7 18.5h10.5z"/></svg>`;

/* Preview text for one queued item (shared by the row and the collapsed
 * "next up" header line). */
function _queueItemPreview(item) {
  return item.text
    ? item.text
    : (item.images?.length ? t('queue.imagesCount', { n: item.images.length }) : t('queue.attachment'));
}

/* Collapse preference — persisted per-conv because renderPendingQueueUI
 * rebuilds the bar's innerHTML on every poll, so the state cannot live in
 * the DOM. null = the user never toggled → the auto-collapse rule decides. */
const QUEUE_AUTO_COLLAPSE_MIN = 4;

function _queueCollapseRead(convId) {
  try {
    if (typeof localStorage === 'undefined') return null;
    const v = localStorage.getItem('tofu.queueCollapsed.' + convId);
    return v === null ? null : v === '1';
  } catch (e) {
    console.debug('[Queue] collapse pref read failed:', e);
    return null;
  }
}

function _queueCollapseWrite(convId, collapsed) {
  try {
    if (typeof localStorage === 'undefined') return;
    localStorage.setItem('tofu.queueCollapsed.' + convId, collapsed ? '1' : '0');
  } catch (e) {
    console.debug('[Queue] collapse pref write failed:', e);
  }
}

/* Effective collapse state: the explicit toggle wins; otherwise a queue of
 * QUEUE_AUTO_COLLAPSE_MIN+ dispatchable messages starts collapsed so a
 * flooded queue can never bury the transcript behind the input bar. */
function _queueCollapsedNow(convId, realCount) {
  const pref = _queueCollapseRead(convId);
  return pref !== null ? pref : realCount >= QUEUE_AUTO_COLLAPSE_MIN;
}

/* Header chevron handler (inline onclick) — flips + persists the state. */
function togglePendingQueueCollapsed(convId) {
  const queue = pendingMessageQueue.get(convId) || [];
  const realCount = queue.filter((it) => _queueSourceOf(it) !== 'autopilot').length;
  _queueCollapseWrite(convId, !_queueCollapsedNow(convId, realCount));
  renderPendingQueueUI(convId);
}
if (typeof window !== 'undefined') window.togglePendingQueueCollapsed = togglePendingQueueCollapsed;

/**
 * Render the pending queue indicator above the input area.
 *
 * ★ CROSS-CONV BLEED GUARD — the input-bar queue is a SINGLE shared DOM node
 * (``#pendingQueueBar`` in ``#pendingQueueContainer``) but ``pendingMessageQueue``
 * is a per-conv Map. Every DOM mutation must therefore be gated on
 * ``convId === activeConvId``: many callers (``_refreshServerQueue``,
 * ``_checkForQueuedTask``'s retry loop, autopilot arm/disarm, ``loadConversationMessages``
 * reconcile) run async, so a fetch dispatched for conv A can resolve AFTER the
 * user switched to conv B and unconditionally paint A's queue into B's visible
 * bar. The Map is still updated for the inactive conv (correct — switching back
 * to A will re-paint through ``loadConversation``'s explicit call), only the
 * paint is suppressed.
 *
 * The removal branch (``queue-removing``+timeout) is likewise gated so a stale
 * empty-queue render for an inactive conv can't tear down the bar that
 * currently belongs to the active conv.
 */
function renderPendingQueueUI(convId) {
  const _isActive = (typeof activeConvId !== 'undefined') && convId === activeConvId;
  let container = document.getElementById("pendingQueueBar");
  const queue = pendingMessageQueue.get(convId);
  if (!queue || queue.length === 0) {
    if (container && _isActive) {
      container.classList.add('queue-removing');
      setTimeout(() => {
        /* Idempotent: only remove if nothing repainted since. A later
         * ``renderPendingQueueUI(activeConvId)`` clears ``queue-removing`` when
         * it repopulates the same container. */
        if (container && container.parentNode && container.classList.contains('queue-removing')) {
          container.remove();
        }
      }, 200);
    }
    return;
  }
  if (!_isActive) return;
  if (!container) {
    container = document.createElement("div");
    container.id = "pendingQueueBar";
    container.className = "pending-queue-bar";
    const queueHost = document.getElementById("pendingQueueContainer");
    if (queueHost) queueHost.appendChild(container);
  }
  container.classList.remove('queue-removing');
  let _realCount = 0;   // human/workflow items get sequential numbers
  const items = queue.map((item) => {
    const _src = _queueSourceOf(item);
    /* ── Autopilot armed-marker sentinel — always rendered last (priority 90),
     *   distinct styling, cancel = DISARM (not just queue-remove). ── */
    if (_src === 'autopilot') {
      const apLabel = (typeof t === 'function') ? t('autopilot.pendingTakeover') : 'Autopilot will take over';
      const apCancelTitle = (typeof t === 'function') ? t('autopilot.cancelTakeover') : 'Cancel autopilot';
      return `<div class="pending-queue-item pending-queue-autopilot">
        <span class="queue-item-number queue-item-autopilot-icon">${_QUEUE_SRC_ICONS.autopilot}</span>
        <span class="queue-item-text">${escapeHtml(apLabel)}</span>
        <button class="queue-item-cancel" onclick="cancelAutopilotMarker('${convId}')" title="${escapeHtml(apCancelTitle)}">${_QUEUE_ICON_X}</button>
      </div>`;
    }
    const i = _realCount++;
    const preview = _queueItemPreview(item);
    // Attachment badges
    const badges = [];
    if (item.images?.length) badges.push(`<span>${item.images.length} img</span>`);
    if (item.pdfTexts?.length) badges.push(`<span>${item.pdfTexts.length} pdf</span>`);
    if (item.convRefs?.length) badges.push(`<span>${item.convRefs.length} ref</span>`);
    if (item.replyQuotes?.length) badges.push(`<span>↩ ${item.replyQuotes.length}</span>`);
    // ── Source attribution line ──
    // A peer turn (agent / operator) names the SOURCE conversation by its
    // TITLE (a raw id is meaningless) and jumps to it on click. A brain
    // workflow kickoff gets a static label (no conversation to jump to).
    let srcLine = '';
    if ((_src === 'agent' || _src === 'operator') && item.fromConv) {
      const _title = (typeof convTitleById === 'function')
        ? convTitleById(item.fromConv) : item.fromConv;
      const _lbl = (typeof t === 'function')
        ? t(_src === 'operator' ? 'queue.fromOperator' : 'queue.fromConv')
        : (_src === 'operator' ? 'from operator' : 'from');
      srcLine = `<div class="queue-item-src" onclick="loadConversation('${escapeHtml(item.fromConv)}')" `
        + `title="${escapeHtml(_title)}">${_QUEUE_SRC_ICONS[_src]}<span>${escapeHtml(_lbl)} «${escapeHtml(_title)}»</span></div>`;
    } else if (_src === 'workflow') {
      const _wfLbl = (typeof t === 'function') ? t('queue.fromWorkflow') : '项目大脑派发';
      srcLine = `<div class="queue-item-src queue-item-src-static">${_QUEUE_SRC_ICONS.workflow}<span>${escapeHtml(_wfLbl)}</span></div>`;
    }
    return `<div class="pending-queue-item qsrc-${_src}">
      <span class="queue-item-number">${i + 1}</span>
      <div class="queue-item-body">
        ${srcLine}
        <span class="queue-item-text">${escapeHtml(preview)}</span>
      </div>
      ${badges.length ? `<span class="queue-item-attachments">${badges.join('')}</span>` : ''}
      <button class="queue-item-cancel" onclick="removePendingQueueItem('${convId}', ${i})" title="${escapeHtml(t('queue.cancelMsg'))}">${_QUEUE_ICON_X}</button>
      ${item.queueId ? `<span class="queue-item-synced" title="${escapeHtml(t('queue.syncedToServer'))}">${_QUEUE_ICON_CLOUD}</span>` : ''}
    </div>`;
  }).join("");

  /* Collapse state — re-applied on every render (polls rebuild innerHTML),
   * so it is read from localStorage via _queueCollapsedNow, not the DOM. */
  const _collapsed = _queueCollapsedNow(convId, _realCount);
  if (_collapsed) container.classList.add('queue-collapsed');
  else container.classList.remove('queue-collapsed');

  const headerSvg = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2v4m0 12v4M4.93 4.93l2.83 2.83m8.48 8.48l2.83 2.83M2 12h4m12 0h4M4.93 19.07l2.83-2.83m8.48-8.48l2.83-2.83"/></svg>`;
  /* Header count reflects only real queued messages; the autopilot sentinel
   * is described by its own row, not counted as "排队消息". */
  const _headerLabel = _realCount > 0
    ? `${_realCount} ${(typeof t === 'function') ? t('queue.messagesQueued') : '条消息排队中'}`
    : ((typeof t === 'function') ? t('autopilot.armedShort') : 'Autopilot armed');
  /* One-line "next up" preview + an autopilot chip — CSS only reveals them
   * in the collapsed state, where they stand in for the hidden item list. */
  const _nextItem = queue.find((it) => _queueSourceOf(it) !== 'autopilot');
  const _nextText = _nextItem ? _queueItemPreview(_nextItem) : '';
  const _hasAutopilot = queue.some((it) => _queueSourceOf(it) === 'autopilot');
  const _apTitle = (typeof t === 'function') ? t('autopilot.pendingTakeover') : 'Autopilot will take over';
  const _toggleTitle = (typeof t === 'function')
    ? t(_collapsed ? 'queue.expand' : 'queue.collapse')
    : (_collapsed ? 'Expand queue' : 'Collapse queue');
  container.innerHTML = `<div class="queue-header">
    <button class="queue-toggle" onclick="togglePendingQueueCollapsed('${convId}')" title="${escapeHtml(_toggleTitle)}">${_QUEUE_ICON_CHEVRON}</button>
    ${headerSvg}
    <span class="queue-header-label">${escapeHtml(_headerLabel)}</span>
    ${_hasAutopilot ? `<span class="queue-header-ap" title="${escapeHtml(_apTitle)}">${_QUEUE_SRC_ICONS.autopilot}</span>` : ''}
    <span class="queue-next-preview" title="${escapeHtml(_nextText)}">${escapeHtml(_nextText)}</span>
    ${_realCount > 1 ? `<button class="queue-clear-all" onclick="clearPendingQueue('${convId}')">${(typeof t === 'function') ? t('queue.clearAll') : '全部清空'}</button>` : ''}
  </div><div class="queue-items">${items}</div>`;
}

/**
 * Cancel the autopilot armed-marker (disarm) from the queue bar.
 * Clears the persistent sentinel + flips any live task's autopilot off, and
 * turns the toolbar toggle off to keep the UI consistent.
 */
function cancelAutopilotMarker(convId) {
  if (typeof Api !== 'undefined' && Api.chat && Api.chat.disarmAutopilot) {
    Api.chat.disarmAutopilot(convId)
      .then((resp) => {
        /* Fold the just-concluded run instantly even with no live stream. */
        if (typeof _applyDisarmResponse === 'function') _applyDisarmResponse(convId, resp);
        if (typeof _refreshServerQueue === 'function') _refreshServerQueue(convId);
      })
      .catch((e) => console.warn('[Autopilot] disarm (queue cancel) failed:', e && e.message));
  }
  /* Reflect the cancel in the toolbar toggle for the active conv. */
  const _conv = (typeof getActiveConv === 'function') ? getActiveConv() : null;
  if (_conv && _conv.id === convId && typeof autopilotEnabled !== 'undefined' && autopilotEnabled
      && typeof _applyAutopilotUI === 'function') {
    _applyAutopilotUI(false);
    if (typeof _saveConvToolState === 'function') _saveConvToolState();
  }
  debugLog('Autopilot canceled — virtual user will not take over', 'info');
}
if (typeof window !== 'undefined') window.cancelAutopilotMarker = cancelAutopilotMarker;

/**
 * Remove a single item from the pending queue (server-backed).
 */
function removePendingQueueItem(convId, idx) {
  const queue = pendingMessageQueue.get(convId);
  if (!queue) return;
  const removed = queue[idx];
  const removedPreview = removed?.text ? removed.text.slice(0, 40) : '(attachment)';
  const queueId = removed?.queueId;

  // Optimistic local removal
  queue.splice(idx, 1);
  console.log(
    `%c[Queue] ✕ Removed%c item #${idx + 1} from conv=${convId.slice(0,8)} | "${removedPreview}" | remaining=${queue.length}`,
    'color:#f59e0b;font-weight:bold', 'color:#f59e0b'
  );
  if (queue.length === 0) pendingMessageQueue.delete(convId);
  renderPendingQueueUI(convId);
  updateSendButton();
  debugLog(`已取消排队消息 #${idx + 1}`, 'info');

  // Server removal
  if (queueId) {
    Api.chat.queueRemove(convId, queueId)
      .then(resp => {
        if (!resp || !resp.ok) console.error(`[Queue] Server remove failed: HTTP ${resp ? resp.status : 'no response'}`);
      })
      .catch(err => console.error('[Queue] Server remove error:', err));
  }
}

/**
 * Clear all pending queued messages for a conversation (server-backed).
 */
function clearPendingQueue(convId) {
  const count = pendingMessageQueue.get(convId)?.length || 0;
  console.log(
    `%c[Queue] ✕✕ Cleared ALL%c ${count} queued messages for conv=${convId.slice(0,8)}`,
    'color:#ef4444;font-weight:bold', 'color:#ef4444'
  );
  pendingMessageQueue.delete(convId);
  renderPendingQueueUI(convId);
  updateSendButton();
  debugLog(`已清空全部 ${count} 条排队消息`, 'info');

  // Server clear
  Api.chat.queueClear(convId)
    .then(resp => {
      if (!resp || !resp.ok) console.error(`[Queue] Server clear failed: HTTP ${resp ? resp.status : 'no response'}`);
    })
    .catch(err => console.error('[Queue] Server clear error:', err));
}

/**
 * Refresh the local queue state from the server.
 * Called after stream ends and after page load for active conversations.
 */
/**
 * Check if the server auto-dispatched a queued message and created a new task.
 * If found, load the new user message and connect to the task's SSE stream.
 */
/**
 * Recover a send/regenerate/edit request whose fetch was aborted by the
 * 90 s client-side safety timer.
 *
 * The backend caps auto-translate server-side and spawns the task thread
 * BEFORE the HTTP response is flushed, so a slow-to-respond server (FUSE/DB
 * latency) very often has the task already running by the time the timer
 * fires. In that case the work is NOT lost — poll /api/v1/chat/active and,
 * if a live task exists for this conv, transparently reload + reconnect to
 * its SSE stream instead of surfacing an error and making the user click
 * Retry.
 *
 * @param {string} convId
 * @param {{endpointMode?: boolean}} [opts]
 * @returns {Promise<boolean>} true if a live task was found and (re)connected.
 */
async function _recoverTimedOutChatTask(convId, opts = {}) {
  // The client safety timer fires at 90s, but on an overloaded server the
  // sync /chat/send handler runs in Hypercorn's thread pool and may not
  // create the task until well after the client aborts. The handler keeps
  // running to completion (the disconnect doesn't cancel the worker thread),
  // so the task reliably appears — we just have to poll long enough. Total
  // window ≈ 30s covers a late-starting handler; only past that do we fall
  // back to the visible error (where conversation reload still re-attaches
  // via the orphan-recovery path).
  const _delays = [0, 1000, 2000, 3000, 4000, 5000, 6000, 8000];
  for (let i = 0; i < _delays.length; i++) {
    if (_delays[i]) await new Promise(r => setTimeout(r, _delays[i]));
    let activeTasks;
    try {
      activeTasks = await Api.chat.active();
    } catch (e) {
      console.warn('[Recover] active() poll failed:', e);
      continue;
    }
    if (!Array.isArray(activeTasks)) continue;
    const task = activeTasks.find(
      t => t.convId === convId && t.status === 'running' && !t.aborted
    );
    if (!task) continue;

    const conv = conversations.find(c => c.id === convId);
    if (!conv) return false;

    // Already attached to this conv's stream (e.g. a prior task is mid-flight
    // and our message was queued behind it) — nothing more to do.
    if (activeStreams.has(convId)) {
      console.log(
        `%c[Recover] Conv=${convId.slice(0,8)} already streaming — treating timed-out request as recovered`,
        'color:#34d399'
      );
      return true;
    }

    console.log(
      `%c[Recover] ▶ Reconnecting to live server task ${task.id.slice(0,8)} for conv=${convId.slice(0,8)} after client timeout`,
      'color:#34d399;font-weight:bold'
    );

    // Reload from server to pick up the persisted (possibly translated) user
    // message before attaching the assistant placeholder.
    conv._needsLoad = true;
    try {
      await loadConversationMessages(convId);
    } catch (e) {
      console.warn('[Recover] loadConversationMessages failed:', e);
    }

    // Re-check after the (awaited) reload — a real SSE connection may have
    // been established in the meantime.
    if (activeStreams.has(convId)) return true;

    const assistantMsg = {
      role: 'assistant', content: '', thinking: '',
      timestamp: Date.now(), toolRounds: [],
    };
    if (opts.endpointMode) assistantMsg._isEndpointPlanner = true;
    _ensureMsgId(assistantMsg);
    conv.messages.push(assistantMsg);
    conv.activeTaskId = task.id;
    conv._needsLoad = false;
    if (activeConvId === convId) window.ConvView.replaceAll(convId);
    renderConversationList();
    connectToTask(convId, task.id);
    return true;
  }
  return false;
}

/* ★ Re-entry latch: at most ONE check chain per conv at a time. Callers
 *   now include finishStream, the queued-dispatch watcher, and the
 *   notify-push hook (cross_tab_sync.js) — without the latch, overlapping
 *   frames/ticks stack duplicate /api/v1/chat/active probes (and double
 *   attach attempts). Retries carry _retryCount > 0 — they ARE the same
 *   chain and pass through. */
const _queuedCheckInFlight = new Set();

async function _checkForQueuedTask(convId, _retryCount = 0) {
  if (_retryCount === 0) {
    if (_queuedCheckInFlight.has(convId)) return;
    _queuedCheckInFlight.add(convId);
  }
  /* Released at every chain end (skip / give-up / attach / error) — the ONLY
   *   non-release path is a retry reschedule (the chain continues there). */
  const _release = () => { _queuedCheckInFlight.delete(convId); };
  try {
    /* ★ ROUTE the pin, don't merely test it (pt_f7a292dc13de47f0).
     *   This guard used to read `_conv.activeTaskId || activeStreams.has(...)`
     *   as "someone else is driving this conv". That stopped being true once
     *   cold attach began pinning the VU CARRIER's id and the stale-pin sweep
     *   deliberately stopped clearing a live carrier's pin — the pin became
     *   DURABLE, so this guard was permanently satisfied and we returned
     *   BEFORE probing for the successor worker the backend had already
     *   spawned. Measured: carrier pin held → probes=0/attached=null; same
     *   backend state with the pin cleared → probes=1/attached=<worker>. That
     *   is the "VU user message on screen, nothing ever generates" report.
     *
     *   The verdict is a ROUTE with four cases (see computeFollowupRoute):
     *   a LIVE carrier is attached through the VU connector rather than
     *   probed for (its successor does not exist yet); a TERMINAL carrier or
     *   a pin the projection never heard of falls through to the probe; a
     *   plain worker or a live local stream still skips, which is this
     *   guard's real job. */
    const _conv = conversations.find(c => c.id === convId);
    const _route = (typeof computeFollowupRoute === 'function')
      ? computeFollowupRoute(_conv, activeStreams)
      /* Reducer missing (bundle-order miss) → legacy behaviour verbatim. */
      : { action: (_conv && (_conv.activeTaskId || activeStreams.has(convId)))
            ? 'skip' : 'probe', taskId: null, vuCarrier: false };
    if (_route.action === 'route-vu') {
      console.log(
        `%c[Queue] ▶ Live VU carrier for conv=${convId.slice(0,8)} — routing to the ` +
        `VU connector (task=${String(_route.taskId).slice(0,8)}) instead of probing`,
        'color:#a78bfa;font-weight:bold');
      _refreshServerQueue(convId);
      _release();
      if (typeof connectToTask === 'function') {
        connectToTask(convId, _route.taskId, 0, { vuCarrier: true });
      }
      return;
    }
    if (_route.action === 'skip') {
      console.log(`%c[Queue] Skipping _checkForQueuedTask — conv=${convId.slice(0,8)} already has active task/stream`, 'color:#a78bfa');
      _refreshServerQueue(convId);
      _release();
      return;
    }
    const activeTasks = await Api.chat.active();
    if (!Array.isArray(activeTasks)) { _release(); return; }
    // ★ Only connect to non-aborted running tasks — an aborted task
    //   is winding down and should not be reconnected to.
    const newTask = activeTasks.find(t => t.convId === convId && t.status === 'running' && !t.aborted);
    if (!newTask) {
      // Server may still be dispatching (persist → dispatch timing gap).
      // Retry if there are queued items, OR if autopilot is on (the
      // backend may still be running the VU LLM call to produce the
      // synthetic user reply, then will spawn a follow-up task).
      const _hasQueueItems = _dispatchableQueueCount(convId) > 0;
      const _autopilotOn = !!(_conv && _conv.autopilotEnabled);
      const _shouldRetry = _hasQueueItems || _autopilotOn;
      // ★ Extended retry schedule — covers slow aborts (mid-tool, slow LLM
      //   abort propagation). Previous [800,1500,3000] = ~5.3s total was too
      //   short when a run_task loop was in the middle of a long tool call
      //   at the moment Stop was clicked, leaving the queued message
      //   silently orphaned on the backend.  ~15s total is enough to cover
      //   almost any realistic abort latency.
      const _retryDelays = [300, 600, 1200, 2400, 4000, 6000];
      if (_shouldRetry && _retryCount < _retryDelays.length) {
        const _delay = _retryDelays[_retryCount];
        console.log(`%c[Queue] No dispatched task yet for conv=${convId.slice(0,8)} (autopilot=${_autopilotOn}, queue=${_hasQueueItems}), retry #${_retryCount + 1} in ${_delay}ms`, 'color:#a78bfa');
        setTimeout(() => _checkForQueuedTask(convId, _retryCount + 1), _delay);
        return;
      }
      // No new task after retries (or no queue) — refresh queue UI
      _refreshServerQueue(convId);
      // ★ Clean up the optimistic "Dispatching queued message…" placeholder
      //   inserted by finishStream().  If we never found a dispatched task,
      //   the placeholder would otherwise stay on screen forever.
      if (_hasQueueItems && activeConvId === convId) {
        try {
          const _ghost = document.getElementById('streaming-msg');
          if (_ghost) _ghost.remove();
        } catch (e) {
          debugLog(`[send-pipeline][queued-dispatch] ghost placeholder cleanup failed conv=${convId.slice(0,8)}: ${e && e.message}`, 'error');
        }
      }
      _release();
      return;
    }
    console.log(
      `%c[Queue] ▶ Found auto-dispatched task ${newTask.id.slice(0,8)} for conv=${convId.slice(0,8)}`,
      'color:#34d399;font-weight:bold'
    );

    const conv = conversations.find(c => c.id === convId);
    if (!conv) { _release(); return; }

    // ★ Remove the optimistic "Dispatching queued message…" placeholder
    //   inserted by finishStream().  renderChat will re-render everything
    //   below, and connectToTask will insert a fresh streaming bubble for
    //   the new task.  If we leave the placeholder in place, renderChat
    //   strips it but connectToTask's `existing` lookup on #streaming-msg
    //   sees the new bubble as already present in some race windows.
    if (activeConvId === convId) {
      try {
        const _ghost = document.getElementById('streaming-msg');
        if (_ghost) _ghost.remove();
      } catch (e) {
        debugLog(`[send-pipeline][cleanup] ghost placeholder cleanup failed conv=${convId.slice(0,8)}: ${e && e.message}`, 'error');
      }
    }

    // Reload messages from server to pick up the queued user message.
    // ★ FIX: loadConversationMessages short-circuits via
    //   `if (!conv._needsLoad && conv.messages.length > 0) return conv;`
    //   For an active conversation that hasn't been reloaded since send,
    //   _needsLoad is false → no Phase 2 fetch → conv.messages stays
    //   stuck at the pre-queued snapshot and the queued user bubble is
    //   invisible until manual page refresh.  Set _needsLoad=true so
    //   loadConversationMessages actually fetches from the server.
    conv._needsLoad = true;
    await loadConversationMessages(convId);

    // Create the empty assistant message placeholder
    const assistantMsg = {
      role: 'assistant',
      content: '',
      thinking: '',
      timestamp: Date.now(),
      toolRounds: [],
    };
    _ensureMsgId(assistantMsg);
    conv.messages.push(assistantMsg);
    conv.activeTaskId = newTask.id;
    conv._needsLoad = false;

    if (activeConvId === convId) {
      window.ConvView.replaceAll(convId);
    }
    renderConversationList();

    // Connect to the new task's SSE stream
    connectToTask(convId, newTask.id);
    debugLog(`服务器已自动发送排队消息`, 'info');

    // Refresh queue UI
    _refreshServerQueue(convId);
    _release();
  } catch (err) {
    console.warn('[Queue] _checkForQueuedTask error:', err);
    _release();
  }
}
if (typeof window !== 'undefined') window._checkForQueuedTask = _checkForQueuedTask;

/* Bounded backoff for the queued-dispatch watcher (9 ticks, ~90s total).
 * Longer horizons are covered event-driven (the notify-push hook in
 * cross_tab_sync.js) and by the push-reconnect snapshot. */
const _QUEUED_WATCH_DELAYS = [800, 1500, 3000, 5000, 8000, 12000, 15000, 20000, 30000];

/**
 * Watch a just-queued message until the backend's auto-dispatch becomes
 * visible locally. Started by the {queued:true} send branch: the backend
 * WILL start this message when the current task settles, but nothing on
 * this tab discovers that while no local stream is live — the busy turn's
 * stream may already have closed (e.g. the invisible autopilot-VU window)
 * or belong to another device entirely. Without this watcher the queued
 * turn starts INVISIBLY and the queue bar sits silent until a manual
 * refresh (production 2026-07-25: six minutes of dead air).
 *
 * Stop conditions: attached (a stream/task is bound locally), the local
 * queue mirror drained (dispatched elsewhere / cancelled), or the budget
 * is exhausted. Each tick reuses the SAME _checkForQueuedTask seam every
 * other caller uses (in-flight-latched, so overlapping ticks are cheap).
 */
function _watchQueuedDispatch(convId, _n = 0) {
  const conv = conversations.find((c) => c.id === convId);
  if (!conv) return;
  // Attached — this tab now drives the dispatched task. Done.
  if (conv.activeTaskId || activeStreams.has(convId)) return;
  // After the first tick, an empty local mirror means the queue was drained
  // elsewhere (dispatched + picked up, or cancelled by the user). Stop.
  // (Tick 0 skips this check: the branch's _refreshServerQueue may not have
  // landed yet, so the mirror can legitimately still be empty.)
  if (_n > 0 && _dispatchableQueueCount(convId) === 0) return;
  _checkForQueuedTask(convId);
  if (_n < _QUEUED_WATCH_DELAYS.length) {
    setTimeout(() => _watchQueuedDispatch(convId, _n + 1), _QUEUED_WATCH_DELAYS[_n]);
  }
}
if (typeof window !== 'undefined') window._watchQueuedDispatch = _watchQueuedDispatch;

async function _refreshServerQueue(convId) {
  try {
    const serverQueue = await Api.chat.queueGet(convId);
    if (!Array.isArray(serverQueue)) {
      console.warn(`[Queue] Server queue fetch failed`);
      return;
    }
    if (serverQueue.length > 0) {
      // Convert server format to local format
      const localQueue = serverQueue.map(item => ({
        queueId: item.queueId,
        kind: item.kind || 'real',
        text: item.text || '',
        images: item.hasImages ? ['(server)'] : [],
        pdfTexts: item.hasPdfs ? ['(server)'] : [],
        convRefs: item.hasRefs ? ['(server)'] : [],
        replyQuotes: item.hasQuotes ? ['(server)'] : [],
        timestamp: item.timestamp,
        // Peer/operator turn attribution (from a sibling conversation) so the
        // queue bar can name the SOURCE conversation by title, not a raw id.
        isPeerMessage: !!item.isPeerMessage,
        fromConv: item.fromConv || '',
        isPeerHuman: !!item.isPeerHuman,
      }));
      pendingMessageQueue.set(convId, localQueue);
    } else {
      pendingMessageQueue.delete(convId);
    }
    renderPendingQueueUI(convId);
    updateSendButton();
    console.log(`%c[Queue] Refreshed from server: ${serverQueue.length} items for conv=${convId.slice(0,8)}`, 'color:#6b7280');
  } catch (err) {
    console.warn('[Queue] Failed to refresh from server:', err);
  }
}

