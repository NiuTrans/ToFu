/* ═══════════════════════════════════════════════════════════════════
   core/conversations.js — extracted from core.js (split 2026-05-28)

   Conversation persistence: saveConversations (debounced), syncConversationToServer, loadConversationsFromServer, loadConversationMessages, forceRecoverFromServer, auditConversations, recoverAll.

   This file is concatenated by lib/js_bundler.py AFTER the slim
   core.js shell — symbols share `window` scope so no exports needed.
   ═══════════════════════════════════════════════════════════════════ */

function saveConversations(changedConvId) {
  const now = Date.now();
  if (changedConvId) {
    const c = conversations.find((x) => x.id === changedConvId);
    /* ── Don't bump updatedAt during periodic streaming saves ──
     * When multiple conversations stream simultaneously, each calls
     * saveConversations every ~3s.  Bumping updatedAt each time makes
     * them compete for the top sort position, causing the sidebar to
     * flicker as conversations constantly swap order.
     * Fix: only bump updatedAt when the conversation is NOT actively
     * streaming.  The timestamp is already set when the user sends a
     * message (before streaming starts) and again in finishStream()
     * (after activeStreams.delete, so the guard passes). */
    if (c && !activeStreams.has(changedConvId)) c.updatedAt = now;
  }
  /* ★ DB-first: in-memory array is truth for this tab, DB across tabs/sessions. */
  conversations.sort(_convSorter);
  _broadcastToTabs("conv_saved", { convId: changedConvId });

  /* ── Throttled sidebar refresh during streaming ──
   * During active streaming, saveConversations is called every ~3s but
   * renderConversationList was NEVER called — so the sidebar sort order
   * and streaming dot were stale until the stream finished or user clicked
   * another conversation.  We now refresh the sidebar on a 2s throttle
   * so users see the active conversation bubble to the top promptly. */
  if (changedConvId && activeStreams.size > 0) {
    const _now = Date.now();
    if (!saveConversations._lastSidebarRefresh || _now - saveConversations._lastSidebarRefresh > 2000) {
      saveConversations._lastSidebarRefresh = _now;
      requestAnimationFrame(() => {
        if (typeof renderConversationList === 'function') renderConversationList();
      });
    }
  }
}


/**
 * Hydrate image base64 from server URLs.
 * After server restart, images loaded from DB only have url (base64 stripped).
 * Needed for UI rendering of images in chat messages.
 * (The backend now handles image resolution for LLM API calls independently
 * via _validate_image_blocks in conv_message_builder.py.)
 */
function _hydrateImageBase64(conv) {
  if (!conv || !conv.messages) { conv._hydratePromise = Promise.resolve(); return; }
  const promises = [];
  for (const msg of conv.messages) {
    if (!msg.images || msg.images.length === 0) continue;
    for (const img of msg.images) {
      if (img.base64) continue;  // already has base64
      const url = img.url || img.preview || "";
      if (!url || url.endsWith("...")) continue;  // truncated placeholder
      // Fetch in background — tracked via promise so message builder can use base64
      const p = fetch(url)
        .then(resp => { if (!resp.ok) throw new Error(`HTTP ${resp.status}`); return resp.blob(); })
        .then(blob => new Promise(resolve => {
          const reader = new FileReader();
          reader.onload = () => {
            const dataUrl = reader.result;
            const commaIdx = dataUrl.indexOf(",");
            if (commaIdx > 0) {
              img.base64 = dataUrl.slice(commaIdx + 1);
              if (!img.mediaType) {
                const match = dataUrl.match(/^data:([^;]+)/);
                if (match) img.mediaType = match[1];
              }
              if (!img.preview || img.preview === url)
                img.preview = dataUrl;
            }
            resolve();
          };
          reader.onerror = () => resolve();  // don't block on read errors
          reader.readAsDataURL(blob);
        }))
        .catch(e => {
          console.warn(`[hydrate] Failed to fetch base64 for image url=${url.slice(0, 80)}: ${e.message}`);
        });
      promises.push(p);
    }
  }
  if (promises.length > 0) {
    console.info(`[hydrate] Fetching base64 for ${promises.length} image(s) in conv=${conv.id.slice(0, 8)}`);
    conv._hydratePromise = Promise.all(promises).then(() => {
      console.info(`[hydrate] Completed ${promises.length} image(s) for conv=${conv.id.slice(0, 8)}`);
    });
  } else {
    conv._hydratePromise = Promise.resolve();
  }
}

// ── Debounced sync: coalesces rapid settings toggles into one request ──
// finishStream() calls syncConversationToServer() directly (immediate).
// Settings/toggle changes call syncConversationToServerDebounced() which
// waits 1.5s for additional changes before firing.
const _syncDebounceTimers = new Map();  // convId → timeoutId
function syncConversationToServerDebounced(conv, delayMs = 1500) {
  const existing = _syncDebounceTimers.get(conv.id);
  if (existing) clearTimeout(existing);
  _syncDebounceTimers.set(conv.id, setTimeout(() => {
    _syncDebounceTimers.delete(conv.id);
    syncConversationToServer(conv);
  }, delayMs));
}

async function syncConversationToServer(conv, { allowTruncate = false } = {}) {
  try {
    /* Guard: skip sync while actively streaming — the assistant message is
     * incomplete, and uploading it would overwrite the server-side accumulator
     * with a partial snapshot.  finishStream() will trigger sync after done. */
    if (activeStreams.has(conv.id)) {
      debugLog(`[syncToServer] Skipped — conv ${conv.id.slice(0,8)} is actively streaming`, 'info');
      return;
    }
    /* Guard: skip sync while POST /chat/send is in flight for this conv.
     * The backend's _chat_send is the sole owner of the first-turn persist
     * (it appends a fresh translated user_msg + writes the row).  Any sync
     * that lands during that window — most commonly the
     * "rescue local-only conv" PUT triggered by loadConversationsFromServer
     * (60s timer / visibilitychange / cross-tab broadcast) — would plant
     * the optimistic untranslated msg as row #0, and chat_send would then
     * append its own translated msg as row #1 → duplicate user message.
     * Cleared by sendMessage()'s finally block. */
    if (conv._sendInFlight) {
      console.info(`[syncToServer] Skipped — conv ${conv.id.slice(0,8)} has POST /chat/send in flight `+
        `(backend owns the first-turn persist; rescue PUT here would duplicate the user message)`);
      return;
    }
    /* Guard: never sync a conversation with zero messages to the server.
     * This prevents the race where _saveConvToolState fires before the user
     * message is pushed, overwriting the server with messages:[]. */
    if (!conv.messages || conv.messages.length === 0) {
      console.log(`[syncToServer] Skipped — conv ${conv.id.slice(0,8)} has 0 messages (nothing to sync)`);
      return;
    }
    /* Guard: never overwrite server with fewer messages (data loss prevention).
     * Also never sync a completely empty conversation if _needsLoad is still set
     * (means we haven't successfully loaded from server yet). */
    if (conv._serverMsgCount && conv.messages.length < conv._serverMsgCount) {
      console.warn(`[syncToServer] ⚠️ SKIPPED sync for conv=${conv.id.slice(0,8)} — local ${conv.messages.length} msgs < server ${conv._serverMsgCount} msgs. ` +
        `This guard prevents overwriting server data, but local changes (including streamed content) will NOT be persisted to server!`);
      return;
    }
    /* ★ CROSS-TALK DETECTION: check for sudden message count jumps that indicate injection */
    if (conv._lastSyncMsgCount !== undefined && conv.messages.length > conv._lastSyncMsgCount + 3) {
      console.error(
        `[syncToServer] ⛔ MESSAGE COUNT JUMP: conv=${conv.id.slice(0,8)} jumped from ` +
        `${conv._lastSyncMsgCount} to ${conv.messages.length} msgs (+${conv.messages.length - conv._lastSyncMsgCount}) ` +
        `since last sync — possible cross-talk injection! ` +
        `activeConvId=${activeConvId?.slice(0,8)||'null'} ` +
        `activeStreams=[${[...activeStreams.keys()].map(k=>k.slice(0,8)).join(',')}]`
      );
      /* Log the extra messages for forensic analysis */
      for (let i = conv._lastSyncMsgCount; i < conv.messages.length; i++) {
        const m = conv.messages[i];
        console.error(
          `[syncToServer] ⛔ INJECTED MSG #${i}: role=${m.role} ` +
          `contentLen=${(m.content||'').length} taskId=${m._taskId?.slice(0,8)||'N/A'} ` +
          `model=${m.model||'N/A'} timestamp=${m.timestamp}`
        );
      }
    }
    conv._lastSyncMsgCount = conv.messages.length;
    if (conv.messages.length === 0 && conv._needsLoad) {
      console.warn(`[syncToServer] ⚠️ SKIPPED sync for conv=${conv.id.slice(0,8)} — 0 local messages and _needsLoad=true (not yet loaded from server)`);
      return;
    }
    const lastMsg = conv.messages[conv.messages.length - 1];
    /* ★ CROSS-TALK DETECTION: check if any messages have foreign task IDs or
     *   unexpected model/content patterns that suggest they belong to another conv */
    const _convTaskId = conv.activeTaskId;
    let _foreignMsgCount = 0;
    for (const m of conv.messages) {
      if (m._taskId && _convTaskId && m._taskId !== _convTaskId && m.role === 'assistant') {
        _foreignMsgCount++;
      }
    }
    if (_foreignMsgCount > 0) {
      console.error(
        `[syncToServer] ⛔ CROSS-TALK DETECTED: conv=${conv.id.slice(0,8)} has ${_foreignMsgCount} ` +
        `assistant message(s) with foreign taskId (conv.activeTaskId=${_convTaskId?.slice(0,8)||'null'}). ` +
        `These messages may have been injected from another conversation's SSE stream!`
      );
    }
    console.info(`[syncToServer] conv=${conv.id.slice(0,8)} msgs=${conv.messages.length} lastRole=${lastMsg?.role} ` +
      `contentLen=${lastMsg?.content?.length||0} thinkingLen=${lastMsg?.thinking?.length||0} hasError=${!!lastMsg?.error} ` +
      `activeTaskId=${_convTaskId?.slice(0,8)||'null'} foreignMsgCount=${_foreignMsgCount}`);
    const lightMsgs = conv.messages.map((m) => {
      let r = m;
      if (m.images?.length > 0)
        r = {
          ...r,
          images: m.images.map((img) => {
            const o = { mediaType: img.mediaType, sizeKB: img.sizeKB };
            if (img.url) {
              o.url = img.url;
              o.preview = img.url;
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
      return r;
    });
    const settings = {
      preset: conv.model || conv.preset,
      model: conv.model || conv.preset,
      thinkingDepth: conv.thinkingDepth || config.defaultThinkingDepth,
      defaultThinkingDepth: config.defaultThinkingDepth,
      searchMode: conv.searchMode,
      fetchEnabled: conv.fetchEnabled,
      codeExecEnabled: conv.codeExecEnabled,
      browserEnabled: conv.browserEnabled,
      desktopEnabled: conv.desktopEnabled || false,
      memoryEnabled: conv.memoryEnabled !== undefined ? conv.memoryEnabled : true,
      schedulerEnabled: conv.schedulerEnabled || false,
      swarmEnabled: conv.swarmEnabled || false,
      endpointEnabled: conv.endpointEnabled || false,
      autopilotEnabled: conv.autopilotEnabled || false,
      imageGenEnabled: conv.imageGenEnabled || false,
      imageGenMode: conv.imageGenMode || false,
      imageGenModel: conv.imageGenModel || null,
      humanGuidanceEnabled: conv.humanGuidanceEnabled || false,
      projectPath: conv.projectPath,
      projectPaths: conv.projectPaths || [],
      autoTranslate: conv.autoTranslate,
      pinned: conv.pinned || false,
      pinnedAt: conv.pinnedAt || 0,
      folderId: conv.folderId || null,
      /* ★ Persist activeTaskId so the server knows which task is associated
       *   with this conversation.  On page reload, initActiveTasks reads this
       *   to recover completed task results even when the SSE stream died. */
      activeTaskId: conv.activeTaskId || null,
      /* ★ Persist last message info so initActiveTasks can detect orphaned user
       *   messages even for _needsLoad shell convs (metadata-only, no messages loaded).
       *   Without this, Case E is skipped for shell convs → orphan stuck forever. */
      lastMsgRole: lastMsg?.role || null,
      lastMsgTimestamp: lastMsg?.timestamp || null,
    };
    /* ★ FIX: Pre-send staleness check — if conv.messages grew since lightMsgs
     * was captured (due to sendMessage/startAssistantResponse running while we
     * were computing lightMsgs), this PUT would overwrite newer data.
     * Cancel and let the fresher sync win. */
    if (!allowTruncate && conv.messages.length > lightMsgs.length) {
      console.warn(
        `[syncToServer] ⏭ CANCELLED stale sync for conv=${conv.id.slice(0,8)} — ` +
        `lightMsgs=${lightMsgs.length} but conv.messages=${conv.messages.length} (grew by ${conv.messages.length - lightMsgs.length} during async). ` +
        `A fresher sync should follow.`
      );
      return;
    }
    const resp = await Api.conversations.put(conv.id, {
      title: conv.title,
      messages: lightMsgs,
      createdAt: conv.createdAt,
      updatedAt: conv.updatedAt || Date.now(),
      settings,
      ...(allowTruncate ? { allowTruncate: true } : {}),
    });
    if (resp && resp.ok) {
      conv._serverMsgCount = lightMsgs.length;
      debugLog(`[syncToServer] ✅ Conv ${conv.id.slice(0,8)} synced ${lightMsgs.length} msgs to server`, 'info');
      /* ★ Write-through: update IndexedDB cache with the synced state.
       *   This ensures the cache always reflects the latest server-confirmed data,
       *   so the next page load gets an instant cache hit with fresh content. */
      ConvCache.put(conv);
    } else {
      const errBody = (resp ? await resp.json().catch(() => ({})) : {});
      const status = resp ? resp.status : 0;
      debugLog(`[syncToServer] ⚠️ Conv ${conv.id.slice(0,8)} sync rejected: ${status} ${errBody.error || ''}`, 'warn');
      /* ★ FIX: When server rejects with blocked_stale_checkpoint (409), the local
       *   data is stale (e.g. IDB cache from interrupted streaming).  Reload from
       *   server to get the correct completed data with finishReason/usage. */
      if (status === 409 && errBody.error === 'blocked_stale_checkpoint') {
        console.warn(`[syncToServer] 🔄 Stale checkpoint detected for conv=${conv.id.slice(0,8)} — reloading from server`);
        try {
          const freshData = await Api.conversations.get(conv.id);
          if (freshData) {
            const freshMsgs = freshData.messages || [];
            if (freshMsgs.length > 0) {
              conv.messages = freshMsgs;
              conv.title = freshData.title || conv.title;
              conv._serverMsgCount = freshMsgs.length;
              ConvCache.put(conv);
              if (activeConvId === conv.id) {
                renderChat(conv, false);
                if (typeof _restoreConvToolState === "function") _restoreConvToolState(conv);
              }
              console.info(`[syncToServer] ✅ Recovered ${freshMsgs.length} msgs from server for conv=${conv.id.slice(0,8)}`);
            }
          }
        } catch (recoverErr) {
          console.error(`[syncToServer] Recovery fetch failed:`, recoverErr.message);
        }
      }
    }
  } catch (e) {
    debugLog(`[syncToServer] ❌ Sync failed for ${conv.id.slice(0,8)}: ${e.message}`, "warn");
  }
}
function _applySettingsToConv(conv, settings) {
  if (!settings) return;
  if (settings.model || settings.effort || settings.preset)
    conv.model = settings.model || settings.preset || settings.effort;
  if (settings.thinkingDepth) conv.thinkingDepth = settings.thinkingDepth;
  if (settings.searchMode) conv.searchMode = settings.searchMode;
  if (settings.fetchEnabled !== undefined)
    conv.fetchEnabled = settings.fetchEnabled;
  if (settings.codeExecEnabled !== undefined)
    conv.codeExecEnabled = settings.codeExecEnabled;
  if (settings.browserEnabled !== undefined)
    conv.browserEnabled = settings.browserEnabled;
  if (settings.desktopEnabled !== undefined)
    conv.desktopEnabled = settings.desktopEnabled;
  if (settings.memoryEnabled !== undefined)
    conv.memoryEnabled = settings.memoryEnabled;
  if (settings.schedulerEnabled !== undefined)
    conv.schedulerEnabled = settings.schedulerEnabled;
  if (settings.swarmEnabled !== undefined)
    conv.swarmEnabled = settings.swarmEnabled;
  if (settings.endpointEnabled !== undefined)
    conv.endpointEnabled = settings.endpointEnabled;
  if (settings.autopilotEnabled !== undefined)
    conv.autopilotEnabled = settings.autopilotEnabled;
  if (settings.imageGenEnabled !== undefined)
    conv.imageGenEnabled = settings.imageGenEnabled;
  if (settings.imageGenMode !== undefined)
    conv.imageGenMode = settings.imageGenMode;
  if (settings.humanGuidanceEnabled !== undefined)
    conv.humanGuidanceEnabled = settings.humanGuidanceEnabled;
  if (settings.imageGenModel)
    conv.imageGenModel = settings.imageGenModel;
  if (settings.projectPath !== undefined)
    conv.projectPath = settings.projectPath;
  if (settings.projectPaths !== undefined)
    conv.projectPaths = settings.projectPaths;
  if (settings.autoTranslate !== undefined)
    conv.autoTranslate = settings.autoTranslate;
  if (settings.pinned !== undefined) conv.pinned = settings.pinned;
  if (settings.pinnedAt !== undefined) conv.pinnedAt = settings.pinnedAt;
  if (settings.folderId !== undefined) conv.folderId = settings.folderId;
  if (settings.source) conv.source = settings.source;
  if (settings.feishuUser) conv.feishuUser = settings.feishuUser;
  /* ★ Persist last message info for Case E orphan detection on _needsLoad shells */
  if (settings.lastMsgRole) conv.lastMsgRole = settings.lastMsgRole;
  if (settings.lastMsgTimestamp) conv.lastMsgTimestamp = settings.lastMsgTimestamp;
  /* ★ Restore activeTaskId from server settings — enables Case B recovery
   *   even on a fresh browser session (no localStorage).
   *   Guard: if activeTaskId was cleared locally during this session
   *   (_activeTaskClearedAt exists), NEVER restore from server metadata.
   *   This prevents stale activeTaskId values stuck in DB from causing
   *   phantom purple dots on the sidebar.  On a true page refresh,
   *   _activeTaskClearedAt won't exist (ephemeral), so initActiveTasks
   *   will properly validate against /api/chat/active before restoring. */
  if (settings.activeTaskId && !conv.activeTaskId) {
    if (!conv._activeTaskClearedAt) {
      conv.activeTaskId = settings.activeTaskId;
    }
  }
}
let _convMetaEtag = null;   // ETag for 304 Not Modified support
async function loadConversationsFromServer(prefetchId) {
  try {
    /* ── Fast path: only metadata for sidebar (no messages) ── */
    const headers = {};
    /* When prefetching, skip ETag/304 — we need fresh data + the conv body */
    if (!prefetchId && _convMetaEtag) headers['If-None-Match'] = _convMetaEtag;
    const url = prefetchId
      ? apiUrl(`/api/v1/conversations?meta=1&prefetch=${encodeURIComponent(prefetchId)}`)
      : apiUrl("/api/v1/conversations?meta=1");
    let resp;
    for (let _attempt = 0; _attempt < 3; _attempt++) {
      resp = await fetch(url, { headers });
      if (resp.status === 503) {
        const delay = (resp.headers.get('Retry-After') || (_attempt + 1)) * 1000;
        debugLog(`[loadConvs] 503 DB busy, retry ${_attempt + 1}/2 in ${delay}ms`, 'warn');
        await new Promise(r => setTimeout(r, delay));
        continue;
      }
      break;
    }
    if (resp.status === 304) return;   // nothing changed — skip all work
    if (!resp.ok) return;
    let serverConvs, prefetchedConv = null;
    if (prefetchId) {
      /* Combo response: { conversations: [...], prefetched: {...} | null } */
      const combo = await resp.json();
      serverConvs = combo.conversations || [];
      prefetchedConv = combo.prefetched || null;
    } else {
      _convMetaEtag = resp.headers.get('ETag') || null;
      serverConvs = await resp.json();
    }
    console.log(`[loadConversationsFromServer] Got ${serverConvs.length} convs from server, local has ${conversations.length}`);
    if (!serverConvs.length) return;
    const localMap = new Map(conversations.map((c) => [c.id, c]));
    let merged = false,
      acChanged = false;
    for (const sc of serverConvs) {
      const local = localMap.get(sc.id);
      if (!local) {
        /* New conversation from server — create shell with empty messages */
        const nc = {
          id: sc.id,
          title: sc.title,
          messages: [],
          _serverMsgCount: sc.messageCount || 0,
          _needsLoad: (sc.messageCount || 0) > 0,
          createdAt: sc.createdAt,
          updatedAt: sc.updatedAt || sc.createdAt,
          activeTaskId: null,
        };
        _applySettingsToConv(nc, sc.settings);
        conversations.push(nc);
        merged = true;
      } else if (!activeStreams.has(sc.id) && !local.activeTaskId) {
        /* Update metadata for existing conversations */
        const sT = sc.updatedAt || sc.createdAt || 0,
          mT = local.updatedAt || local.createdAt || 0;
        const serverMsgCount = sc.messageCount || 0;
        if (serverMsgCount > local.messages.length || sT > mT) {
          local.title = sc.title;
          local.updatedAt = sc.updatedAt || sc.createdAt;
          local._serverMsgCount = serverMsgCount;
          if (serverMsgCount > local.messages.length) {
            local._needsLoad = true;
          }
          /* Preserve local pinned/folder state — these are client-side
             preferences and must not be overwritten by a potentially
             stale server snapshot during periodic refresh.           */
          const keepPinned = local.pinned, keepPinnedAt = local.pinnedAt;
          const keepFolderId = local.folderId;
          _applySettingsToConv(local, sc.settings);
          local.pinned = keepPinned; local.pinnedAt = keepPinnedAt;
          /* ★ Only restore local folderId if it was set locally but server
           *   doesn't have it yet (PATCH in-flight race). If server has a
           *   folderId, trust the server (it's the source of truth). */
          if (keepFolderId && !local.folderId) local.folderId = keepFolderId;
          if (sc.id === activeConvId) acChanged = true;
          merged = true;
        }
      }
    }
    /* ★ Rescue local-only conversations that exist in memory (from this
     *   session) but haven't been synced to the server yet.  This handles
     *   the edge case where the user sent a message while the server was
     *   briefly unreachable.  Ghost convs (empty, not on server) are simply
     *   dropped — they have no data worth saving. */
    const serverIdSet = new Set(serverConvs.map(sc => sc.id));
    for (const lc of conversations) {
      if (!serverIdSet.has(lc.id) && !activeStreams.has(lc.id)) {
        if (lc.messages.length > 0) {
          console.warn(`[loadConversationsFromServer] ★ Rescuing local-only conv ${lc.id.slice(0,8)} ` +
            `(${lc.messages.length} msgs) — syncing to server`);
          syncConversationToServer(lc);
        } else if (lc.id !== activeConvId) {
          /* Empty local-only conv — drop it silently (it was never meaningful) */
          conversations = conversations.filter(c => c.id !== lc.id);
          merged = true;
        }
      }
    }

    /* ── Apply prefetched conversation data (eliminates second round-trip) ── */
    if (prefetchedConv && prefetchedConv.id) {
      const pc = conversations.find(c => c.id === prefetchedConv.id);
      /* ★ FIX: Allow prefetch even when activeTaskId is set.  After a server
       *   crash, the backend's recover_stale_tasks_on_startup() clears
       *   activeTaskId and merges interrupted content into conversation
       *   messages.  So by the time this runs, the conv has clean data.
       *   Previously, `!pc.activeTaskId` blocked prefetch for the exact
       *   case where it was most needed (crash recovery of the active conv),
       *   forcing an extra round-trip through loadConversationMessages. */
      if (pc && pc._needsLoad && !activeStreams.has(pc.id)) {
        const serverMsgs = prefetchedConv.messages || [];
        pc.messages = serverMsgs;
        pc.title = prefetchedConv.title || pc.title;
        pc.updatedAt = prefetchedConv.updatedAt || prefetchedConv.updated_at || pc.updatedAt;
        const keepPinned = pc.pinned, keepPinnedAt = pc.pinnedAt;
        _applySettingsToConv(pc, prefetchedConv.settings);
        pc.pinned = keepPinned; pc.pinnedAt = keepPinnedAt;
        pc._needsLoad = false;
        pc._serverMsgCount = Math.max(serverMsgs.length, pc.messages.length);
        console.log(`[loadConversationsFromServer] ⚡ Prefetched conv ${pc.id.slice(0,8)}: ${serverMsgs.length} msgs — no second fetch needed`);
        /* ★ Update IndexedDB cache with the prefetched data */
        ConvCache.put(pc);
        merged = true;
      }
    }

    console.log(`[loadConversationsFromServer] merged=${merged}, total conversations now: ${conversations.length}, ` +
      `visible: ${conversations.filter(c => c.messages.length > 0 || (c._serverMsgCount||0) > 0 || c._needsLoad).length}`);
    if (merged) {
      conversations.sort(_convSorter);
      renderConversationList();
      /* If the active conversation needs a full reload, do it now */
      if (activeConvId) {
        const ac = getActiveConv();
        if (ac && ac._needsLoad) {
          await loadConversationMessages(activeConvId);
        } else if (acChanged && ac && !ac.activeTaskId) {
          renderChat(ac, false);
          if (typeof _restoreConvToolState === "function")
            _restoreConvToolState(ac);
        }
      }
    }
  } catch (e) {
    debugLog(`Server load: ${e.message}`, "warn");
  }
}

/**
 * Load full messages for a single conversation on demand.
 * Returns the conversation object or null.
 */
async function loadConversationMessages(convId) {
  const conv = conversations.find((c) => c.id === convId);
  if (!conv) return null;
  /* Skip if already loaded and not stale */
  if (!conv._needsLoad && conv.messages.length > 0) return conv;

  /* ═══ Phase 1: Try IndexedDB cache for instant render ═══ */
  let cacheHit = false;
  try {
    const cached = await ConvCache.get(convId);
    if (cached && cached.messages && cached.messages.length > 0) {
      /* Serve from cache immediately — user sees content with zero network wait */
      conv.messages = cached.messages;
      conv.title = cached.title || conv.title;
      /* Apply cached settings (preserving local overrides like pinned) */
      const keepPinned = conv.pinned, keepPinnedAt = conv.pinnedAt;
      if (cached.settings) _applySettingsToConv(conv, cached.settings);
      conv.pinned = keepPinned; conv.pinnedAt = keepPinnedAt;
      conv._needsLoad = false;
      conv._serverMsgCount = Math.max(cached.messages.length, conv._serverMsgCount || 0);
      conv._cachedUpdatedAt = cached.updatedAt || 0;
      cacheHit = true;
      // ★ Re-attach compaction markers (lazy, fire-and-forget) so the
      //   inline chips reappear after page reload. The viewer module
      //   handles missing gracefully if not yet loaded.
      try {
        if (typeof attachCompactionMarkersToConversation === 'function') {
          attachCompactionMarkersToConversation(convId, conv.messages).then(() => {
            if (convId === activeConvId && typeof renderChat === 'function') {
              renderChat(conv, false);
            }
          }).catch(e => console.debug('[compaction] attach (cache) failed:', e));
        }
      } catch (e) { console.debug('[compaction] attach (cache) hook error:', e); }
      // ★ Hydrate renderable artifacts after cache load so chips reappear
      //   on page reload without waiting for the server fetch.
      try {
        if (typeof window.Artifacts !== 'undefined' && window.Artifacts.hydrateConversation) {
          window.Artifacts.hydrateConversation(conv).then(() => {
            if (convId === activeConvId && typeof renderChat === 'function') {
              renderChat(conv, false);
            }
          }).catch(e => console.debug('[artifacts] hydrate (cache) failed:', e));
        }
      } catch (e) { console.debug('[artifacts] hydrate (cache) hook error:', e); }
      console.info(`[loadConvMsgs] ⚡ CACHE HIT conv=${convId.slice(0,8)}: ${cached.messages.length} msgs (cachedAt=${new Date(cached.cachedAt).toISOString()})`);
      /* Hydrate images from cache (URLs only, base64 stripped) */
      _hydrateImageBase64(conv);
      /* Render immediately from cache */
      if (convId === activeConvId) {
        if (activeStreams.has(convId)) {
          if (typeof showStreamingUIForConv === "function") showStreamingUIForConv(convId);
        } else {
          renderChat(conv, false);
          if (typeof _restoreConvToolState === "function") _restoreConvToolState(conv);
        }
      }
    }
  } catch (cacheErr) {
    console.warn(`[loadConvMsgs] Cache read failed for ${convId.slice(0,8)}: ${cacheErr.message}`);
  }

  /* ═══ Phase 2: Fetch from server (verify freshness or first load) ═══ */
  /* ★ Snapshot local state BEFORE the server fetch begins.  The user can
   *   call sendMessage() during the (potentially multi-second) fetch — that
   *   pushes an optimistic user message into conv.messages with a fresh
   *   timestamp.  If we don't snapshot, the post-fetch reconciliation can't
   *   distinguish "cache + new local push" from "stale cache".  See the
   *   user-msg-disappears-on-send race below. */
  const _preFetchMsgCount = conv.messages.length;
  const _preFetchNewest = conv.messages.length > 0
    ? conv.messages.reduce((mx, m) => Math.max(mx, m.timestamp || 0), 0)
    : 0;
  const _preFetchActiveTaskId = conv.activeTaskId || null;
  try {
    let resp;
    /* Timeout: if server is frozen (FUSE/dolphins), don't hang forever.
     * With cache hit: user already sees content, 10s is generous for background check.
     * Without cache hit: 10s before showing retry button is acceptable. */
    const _fetchTimeout = cacheHit ? 10000 : 15000;
    const _mkSignal = typeof AbortSignal !== 'undefined' && AbortSignal.timeout
      ? (ms) => AbortSignal.timeout(ms)
      : (ms) => { const c = new AbortController(); setTimeout(() => c.abort(), ms); return c.signal; };
    for (let _attempt = 0; _attempt < 3; _attempt++) {
      resp = await Api.conversations.getResponse(convId, { signal: _mkSignal(_fetchTimeout) });
      if (!resp) { /* network/abort — retry */ continue; }
      if (resp.status === 503) {
        /* DB temporarily busy — wait and retry */
        const delay = (resp.headers.get('Retry-After') || (_attempt + 1)) * 1000;
        debugLog(`[loadConvMsgs] ${convId.slice(0,8)}: 503 DB busy, retry ${_attempt + 1}/2 in ${delay}ms`, 'warn');
        await new Promise(r => setTimeout(r, delay));
        continue;
      }
      break;
    }
    /* All retries exhausted with null resp (network/abort/timeout). Treat
     * as a transient fetch failure: keep whatever we already rendered from
     * cache and bail out — caller decides whether to show the retry UI. */
    if (!resp) {
      debugLog(`Load conv ${convId}: fetch failed after retries (network/timeout)`, 'warn');
      if (!cacheHit && convId === activeConvId) {
        const inner = document.getElementById('chatInner');
        if (inner && conv._needsLoad && conv.messages.length === 0) {
          inner.innerHTML = `<div class="welcome" id="welcome" style="opacity:0.7"><div class="welcome-icon">⚡</div><h2>Failed to load conversation</h2><p>Network timeout — server unreachable.</p><p style="margin-top:1em"><button onclick="loadConversation('${convId}')" class="action-btn" style="padding:8px 16px;cursor:pointer">🔄 Retry</button></p></div>`;
        }
      }
      return conv;
    }
    if (!resp.ok) {
      /* ── 404 ghost: conversation exists in sidebar but not on server ──
       * This happens when a conv was created locally but never synced to the DB
       * (e.g. disk I/O error, race condition, or the conv was only ever empty).
       * Without cleanup, the conv stays in the sidebar forever with _needsLoad=true,
       * and every click triggers another 404 → permanent "redirect to New Chat" loop.
       * Fix: mark it as fully loaded (empty) so _purgeEmptyConvs can remove it. */
      if (resp.status === 404) {
        debugLog(`[loadConvMsgs] ${convId.slice(0,8)}: 404 NOT FOUND — conversation not on server`, 'warn');
        /* Clean up stale cache entry too */
        ConvCache.remove(convId);
        /* If this is the active conversation, show a user-friendly message */
        if (convId === activeConvId) {
          const inner = document.getElementById('chatInner');
          if (inner) {
            inner.innerHTML = `<div class="welcome" id="welcome"><div class="welcome-icon">⚠️</div><h2>Conversation Not Found</h2><p>This conversation (<code>${convId}</code>) was not saved to the server.<br>It may have been created during a server error or was never synced.</p><p style="margin-top:1em"><button onclick="deleteConversation('${convId}')" class="action-btn" style="padding:8px 16px;cursor:pointer">🗑️ Remove from sidebar</button>&nbsp;&nbsp;<button onclick="newChat()" class="action-btn" style="padding:8px 16px;cursor:pointer">✨ New Chat</button></p></div>`;
          }
        }
        /* Remove the orphan from the in-memory array + sidebar */
        conversations = conversations.filter(c => c.id !== convId);
        if (typeof renderConversationList === 'function') renderConversationList();
      }
      /* If we had a cache hit, the user already sees content — just return */
      return conv;
    }
    const data = await resp.json();
    const serverMsgs = data.messages || [];
    const serverUpdatedAt = data.updatedAt || data.updated_at || 0;

    /* ── Freshness check: is server data newer than what we rendered from cache? ── */
    const hasLocalData = conv.messages.length > 0;
    const isStreaming = activeStreams.has(convId);
    /* Use reduce instead of Math.max(...) to avoid stack overflow on huge conversations */
    const localNewest = hasLocalData ? conv.messages.reduce((mx, m) => Math.max(mx, m.timestamp || 0), 0) : 0;
    const serverNewest = serverMsgs.length > 0 ? serverMsgs.reduce((mx, m) => Math.max(mx, m.timestamp || 0), 0) : 0;

    /* ★ Detect local activity that happened DURING the server fetch
     *   (sendMessage pushed an optimistic user/assistant msg, or
     *   startAssistantResponse set activeTaskId).  This must NOT be wiped
     *   by the server response — the server's snapshot was taken before
     *   that local push and overwriting would erase the user's just-typed
     *   message from chatInner until the next refresh.
     *
     *   Conditions for "local has fresh activity that server doesn't know about yet":
     *     a) conv.messages grew during the fetch (optimistic user/assistant push), OR
     *     b) the newest local timestamp moved forward, OR
     *     c) activeTaskId was set during the fetch (POST returned with a task)
     */
    const _localGrewDuringFetch = conv.messages.length > _preFetchMsgCount;
    const _localTsMovedDuringFetch = localNewest > _preFetchNewest;
    const _activeTaskIdAppearedDuringFetch =
      !_preFetchActiveTaskId && !!conv.activeTaskId;
    const _hasFreshLocalActivity =
      _localGrewDuringFetch || _localTsMovedDuringFetch || _activeTaskIdAppearedDuringFetch;

    /* Treat local data as "unsynced" if either:
     *   (a) the user pushed something during this fetch (cache hit or not), or
     *   (b) the originally-loaded data wasn't from cache and is newer than server.
     *
     *   ★ FIX: Previously this was gated by `!cacheHit`, which silently
     *   suppressed (a) — when sendMessage pushed an optimistic user msg
     *   during a Phase 2 fetch right after a cache hit, the user msg's
     *   newer timestamp was ignored and the branch below overwrote it.
     *   Symptom: user sends a message, only the assistant streams in
     *   chatInner; the user's prompt only reappears after manual refresh
     *   when the persisted server data is reloaded. */
    const localHasUnsynced =
      _hasFreshLocalActivity ||
      (hasLocalData && !cacheHit && localNewest > serverNewest);

    /* ── One-line reconciliation snapshot ──
     *  Logged BEFORE the branch dispatch so that postmortem of any
     *  message-disappearance bug has a complete picture: what the cache
     *  loaded, what the server returned, what local activity happened
     *  during the fetch, and which branch the code is about to take.
     *  Server-side `/api/client-error` mirrors warn/error to logs/app.log. */
    const _reconBranch =
      localHasUnsynced ? 'KEEP_LOCAL'
      : (conv.activeTaskId && hasLocalData) ? 'MERGE_ACTIVE_TASK'
      : (!hasLocalData || (!activeStreams.has(convId) && !conv.activeTaskId && !_hasFreshLocalActivity)) ? 'OVERWRITE'
      : 'NOOP';
    console.info(
      `[loadConvMsgs] 📊 Phase2 reconcile conv=${convId.slice(0,8)} ` +
      `cacheHit=${cacheHit} preLen=${_preFetchMsgCount} curLen=${conv.messages.length} ` +
      `serverLen=${serverMsgs.length} ` +
      `preNewest=${_preFetchNewest} curNewest=${localNewest} serverNewest=${serverNewest} ` +
      `preTaskId=${_preFetchActiveTaskId ? _preFetchActiveTaskId.slice(0,8) : 'null'} ` +
      `curTaskId=${conv.activeTaskId ? conv.activeTaskId.slice(0,8) : 'null'} ` +
      `streaming=${activeStreams.has(convId)} ` +
      `freshLocal=${_hasFreshLocalActivity}(grew=${_localGrewDuringFetch},ts=${_localTsMovedDuringFetch},task=${_activeTaskIdAppearedDuringFetch}) ` +
      `→ branch=${_reconBranch}`
    );

    /* ★ Always-on translation merge: regardless of which branch below
     *   handles the main message-list reconciliation, copy server-side
     *   translations (translatedContent + showingTranslation + translateDone
     *   + translateModel + originalContent) into matching local messages.
     *   This covers endpoint-mode convs where the backend committed
     *   translatedContent for planner / worker / critic turns but the IDB
     *   cache was populated before those commits ran.  Without this pass
     *   the user sees English until the cache expires, even though the
     *   server has the Chinese ready.  Matches by index + role identity +
     *   content equality to avoid resurrecting stale translations.
     */
    const _mergeServerTranslations = (sourceMsgs, destMsgs) => {
      if (!Array.isArray(sourceMsgs) || !Array.isArray(destMsgs)) return 0;
      const overlap = Math.min(sourceMsgs.length, destMsgs.length);
      let merged = 0;
      for (let i = 0; i < overlap; i++) {
        const sm = sourceMsgs[i], lm = destMsgs[i];
        if (!sm || !lm) continue;
        if (!sm.translatedContent) continue;
        if (lm.translatedContent) continue;  // already have — don't overwrite
        // Identity check
        if (sm.role !== lm.role) continue;
        if (!!sm._isEndpointPlanner !== !!lm._isEndpointPlanner) continue;
        if (!!sm._isEndpointReview !== !!lm._isEndpointReview) continue;
        if (sm._epIteration !== lm._epIteration) continue;
        // Content must match byte-for-byte — stale content would make the
        // preserved translation incorrect for the (edited) new content.
        if ((sm.content || '') !== (lm.content || '')) continue;
        lm.translatedContent = sm.translatedContent;
        lm._showingTranslation = sm._showingTranslation !== false;
        lm._translateDone = true;
        if (sm._translateModel && !lm._translateModel) lm._translateModel = sm._translateModel;
        if (sm.originalContent && !lm.originalContent) lm.originalContent = sm.originalContent;
        merged++;
      }
      return merged;
    };

    if (localHasUnsynced) {
      console.warn(`[loadConvMsgs] ⚠️ KEPT local data for conv=${convId.slice(0,8)} — ` +
        `local has ${conv.messages.length} msgs (newest=${new Date(localNewest).toISOString()}) ` +
        `vs server ${serverMsgs.length} msgs (newest=${new Date(serverNewest).toISOString()}) ` +
        `freshLocalActivity=${_hasFreshLocalActivity} (grew=${_localGrewDuringFetch} ` +
        `tsMoved=${_localTsMovedDuringFetch} taskAppeared=${_activeTaskIdAppearedDuringFetch}). ` +
        `${_hasFreshLocalActivity ? 'Skipping resync — backend send/start in flight owns next write.' : 'Will re-sync to server.'}`);
      /* ★ Skip the resync when fresh local activity caused this branch:
       *   sendMessage()/startAssistantResponse() are mid-flight and the
       *   backend's /api/chat/send (or /api/chat/start) will persist the
       *   optimistic message itself.  A racing PUT here would re-upload
       *   the un-translated user msg and could overwrite the backend's
       *   freshly-committed translatedContent. */
      if (!_hasFreshLocalActivity) {
        syncConversationToServer(conv);
      }
      // Even when keeping local, merge server translations in — they are
      // strictly additive and can't harm unsynced newer content.
      const _mergedHU = _mergeServerTranslations(serverMsgs, conv.messages);
      if (_mergedHU > 0) {
        console.info(`[loadConvMsgs] 🈯 Merged ${_mergedHU} server translation(s) ` +
          `into local-unsynced conv=${convId.slice(0,8)}`);
      }
    } else if (conv.activeTaskId && hasLocalData) {
      /* ★ FIX: Active task with stale local data (e.g. IDB cache from before
       *   task started).  We can't replace conv.messages (would orphan the
       *   assistantMsg ref held by connectToTask), but we CAN merge server
       *   checkpoint data into the existing assistant message so the UI shows
       *   accumulated content immediately — instead of "Waiting…" until SSE. */
      const lastLocal = conv.messages[conv.messages.length - 1];
      if (lastLocal && lastLocal.role === 'assistant' && serverMsgs.length > 0) {
        const lastServer = serverMsgs[serverMsgs.length - 1];
        if (lastServer && lastServer.role === 'assistant') {
          /* Only upgrade: server content longer than local → server had checkpoint */
          if ((lastServer.content || '').length > (lastLocal.content || '').length) {
            lastLocal.content = lastServer.content;
          }
          if ((lastServer.thinking || '').length > (lastLocal.thinking || '').length) {
            lastLocal.thinking = lastServer.thinking;
          }
          if (lastServer.toolRounds?.length && !lastLocal.toolRounds?.length) {
            lastLocal.toolRounds = lastServer.toolRounds;
          }
          /* Also update the stream buffer if one exists (for showStreamingUIForConv) */
          const buf = streamBufs.get(convId);
          if (buf) {
            if (!buf.content && lastLocal.content) buf.content = lastLocal.content;
            if (!buf.thinking && lastLocal.thinking) buf.thinking = lastLocal.thinking;
            if (!buf.toolRounds?.length && lastLocal.toolRounds?.length)
              buf.toolRounds = lastLocal.toolRounds.map(r => ({...r}));
          }
        }
      }
      /* ★ FIX: Merge translatedContent from server messages into local messages.
       *   When entering this branch (activeTaskId set), we keep local messages
       *   to avoid orphaning refs, but the IDB cache may be stale and missing
       *   translations that the server has (from _commit_translation_to_db).
       *   Without this merge, translations disappear when viewing a conv with
       *   a stale activeTaskId, then get unnecessarily regenerated.
       *   Uses the strict identity check from _mergeServerTranslations. */
      const _mergedAT = _mergeServerTranslations(serverMsgs, conv.messages);
      if (_mergedAT > 0) {
        console.info(`[loadConvMsgs] 🈯 Merged ${_mergedAT} server translation(s) ` +
          `into active-task conv=${convId.slice(0,8)}`);
      }
      /* Also merge non-translation fields the local copy may lack */
      const _mergeLen = Math.min(conv.messages.length, serverMsgs.length);
      for (let _mi = 0; _mi < _mergeLen; _mi++) {
        const lm = conv.messages[_mi], sm = serverMsgs[_mi];
        if (sm.finishReason && !lm.finishReason) lm.finishReason = sm.finishReason;
        if (sm.usage && !lm.usage) lm.usage = sm.usage;
        if (sm.model && !lm.model) lm.model = sm.model;
      }
      /* ★ FIX (autopilot VU + post-finish messages invisible after reload):
       *   When the IDB cache predates a finished autopilot follow-up, conv
       *   has fewer messages than the server but conv.activeTaskId is still
       *   set from server settings.  This branch previously merged only
       *   metadata at overlapping indices and never appended the trailing
       *   server messages — so the synthetic VU user message + the
       *   autopilot follow-up's assistant reply were silently dropped.
       *   Append them now, but ONLY when no stream is actually live for
       *   this conv (otherwise an orphaned assistantMsg ref held by
       *   connectToTask could be invalidated mid-stream). */
      if (!activeStreams.has(convId) && serverMsgs.length > conv.messages.length) {
        const _appendStart = conv.messages.length;
        const _appended = serverMsgs.slice(_appendStart);
        conv.messages.push(..._appended);
        console.info(`[loadConvMsgs] 📥 MERGE_ACTIVE_TASK appended ${_appended.length} ` +
          `trailing server msg(s) (idx ${_appendStart}..${serverMsgs.length - 1}) into ` +
          `conv=${convId.slice(0,8)} — cache predated post-finish writes ` +
          `(autopilot VU / queue dispatch / late persist).`);
        try { ConvCache.put(conv); } catch (_e) { /* best-effort */ }
        if (convId === activeConvId && typeof renderChat === 'function') {
          renderChat(conv, false);
        }
      }
      conv._needsLoad = false;
      conv._serverMsgCount = Math.max(serverMsgs.length, conv.messages.length);
    } else if (!hasLocalData || (!activeStreams.has(convId) && !conv.activeTaskId && !_hasFreshLocalActivity)) {
      /* Apply server data when:
       *  - No local data (first load, no cache)
       *  - Not actively streaming AND no active task starting AND
       *    no fresh local activity occurred during the fetch
       * ★ FIX: Previously, `cacheHit` bypassed the activeTaskId guard, causing
       *   Phase 2 server response to overwrite conv.messages even when
       *   startAssistantResponse had just pushed an assistant message and was
       *   awaiting POST /api/chat/start. This race condition caused connectToTask
       *   to see a user message as the last message → bail out → no SSE stream
       *   → sidebar shows pulsing dot but no Agent icon in chat area.
       *   Now we ALWAYS re-check activeTaskId/activeStreams at overwrite time.
       * ★ FIX 2: Also gate on _hasFreshLocalActivity — sendMessage pushes the
       *   optimistic user msg BEFORE awaiting POST, so activeTaskId is still
       *   null at this branch evaluation.  Without this guard, the user's
       *   just-typed prompt is wiped from chatInner; only the assistant's
       *   streamed reply is visible until manual refresh restores the user
       *   msg from the persisted server copy. */
      const cacheIsStale = !cacheHit ||
        serverMsgs.length !== conv.messages.length ||
        serverUpdatedAt > (conv._cachedUpdatedAt || 0);

      if (cacheIsStale) {
        /* ★ Diagnostic: if we're about to wipe a non-empty local that
         *   doesn't match the server, leave a warn breadcrumb (forwarded
         *   to logs/app.log via /api/client-error).  This is the place
         *   the chatInner-disappearance bug was born; if it ever recurs,
         *   the postmortem starts here. */
        if (hasLocalData && conv.messages.length > serverMsgs.length) {
          if (typeof debugLog === 'function') {
            debugLog(
              `[loadConvMsgs] ⚠️ OVERWRITE conv=${convId.slice(0,8)} ` +
              `wiping local len=${conv.messages.length} → server len=${serverMsgs.length} ` +
              `(cacheHit=${cacheHit} preLen=${_preFetchMsgCount} ` +
              `freshLocal=${_hasFreshLocalActivity} taskId=${conv.activeTaskId || 'null'} ` +
              `streaming=${activeStreams.has(convId)})`,
              'warn'
            );
          }
        }
        conv.messages = serverMsgs;
        conv.title = data.title || conv.title;
        conv.updatedAt = serverUpdatedAt || conv.updatedAt;
        const keepPinned = conv.pinned, keepPinnedAt = conv.pinnedAt;
        _applySettingsToConv(conv, data.settings);
        conv.pinned = keepPinned; conv.pinnedAt = keepPinnedAt;

        if (cacheHit) {
          console.info(`[loadConvMsgs] 🔄 Cache STALE for conv=${convId.slice(0,8)} — ` +
            `server has ${serverMsgs.length} msgs (updatedAt=${serverUpdatedAt}), re-rendering`);
        }
      } else {
        /* Cache-fresh path: message count+timestamp matched, so we are not
         * replacing conv.messages.  But server-side auto-translate may have
         * committed translatedContent into the DB AFTER the cache was
         * written (this is the common endpoint-mode case).  Merge those
         * translations in-place so the user sees Chinese immediately. */
        const _mergedFresh = _mergeServerTranslations(serverMsgs, conv.messages);
        if (_mergedFresh > 0) {
          console.info(`[loadConvMsgs] 🈯 Cache FRESH but merged ${_mergedFresh} ` +
            `server translation(s) — conv=${convId.slice(0,8)}`);
          // Trigger re-render so Chinese appears now rather than on next action
          if (convId === activeConvId) {
            const _active = conversations.find(c => c.id === convId);
            if (_active && typeof renderChat === 'function') renderChat(_active, false);
          }
          ConvCache.put(conv);  // persist merged translations into cache
        } else {
          console.info(`[loadConvMsgs] ✅ Cache FRESH for conv=${convId.slice(0,8)} — no re-render needed`);
        }
      }

      conv._needsLoad = false;
      conv._serverMsgCount = Math.max(serverMsgs.length, conv.messages.length);

      /* ★ Update IndexedDB cache with authoritative server data */
      ConvCache.put(conv);

      /* ★ Re-attach compaction markers from transcript_archive — see
       *    static/js/compaction-viewer.js. Fire-and-forget; the viewer
       *    will trigger a re-render once markers are populated. */
      try {
        if (typeof attachCompactionMarkersToConversation === 'function') {
          attachCompactionMarkersToConversation(convId, conv.messages).then(() => {
            if (convId === activeConvId && typeof renderChat === 'function') {
              renderChat(conv, false);
            }
          }).catch(e => console.debug('[compaction] attach (server) failed:', e));
        }
      } catch (e) { console.debug('[compaction] attach (server) hook error:', e); }

      /* ★ Hydrate renderable artifacts (md/html/svg) — chips appear next
       *    to the file-changes bar and open the right-side panel.  See
       *    lib/artifacts/ + static/js/artifacts.js.  Fire-and-forget. */
      try {
        if (typeof window.Artifacts !== 'undefined' && window.Artifacts.hydrateConversation) {
          window.Artifacts.hydrateConversation(conv).then(() => {
            if (convId === activeConvId && typeof renderChat === 'function') {
              renderChat(conv, false);
            }
          }).catch(e => console.debug('[artifacts] hydrate (server) failed:', e));
        }
      } catch (e) { console.debug('[artifacts] hydrate (server) hook error:', e); }

      /* ★ Clear stale "server_offline" errors: if the last assistant message
       *   has finishReason='server_offline' but we just successfully loaded
       *   from the server (proving it's online), clear the misleading error text.
       *   The "Server Offline" finish badge remains as a historical marker. */
      {
        const _lastMsg = conv.messages[conv.messages.length - 1];
        if (_lastMsg && _lastMsg.role === 'assistant' &&
            _lastMsg.finishReason === 'server_offline' &&
            _lastMsg.error && errorEnvelopeKind(_lastMsg.error) === 'server_offline') {
          console.info(`[loadConvMsgs] Clearing stale server_offline error for conv=${convId.slice(0,8)}`);
          delete _lastMsg.error;
          // Re-save to IDB and server to persist the cleanup
          ConvCache.put(conv);
          syncConversationToServer(conv);
        }
      }

      debugLog(`[loadConvMsgs] ${convId.slice(0,8)}: server=${serverMsgs.length} msgs, local=${conv.messages.length} msgs, _serverMsgCount=${conv._serverMsgCount}, cacheHit=${cacheHit}`, 'info');

      /* Hydrate image base64 from server URLs */
      _hydrateImageBase64(conv);

      /* Re-render if server data was newer (or first load with no cache) */
      if (cacheIsStale && convId === activeConvId) {
        if (activeStreams.has(convId)) {
          if (typeof showStreamingUIForConv === "function") showStreamingUIForConv(convId);
        } else {
          renderChat(conv, false);
          if (typeof _restoreConvToolState === "function") _restoreConvToolState(conv);
        }
      }
    }

    /* ★ Re-trigger HG translations for any awaiting_human rounds after load.
     *   On page refresh, translation state is lost (only in-memory). This
     *   re-fires translation for pending guidance cards so users see Chinese. */
    if (typeof _retriggerHgTranslations === 'function') {
      _retriggerHgTranslations(convId);
    }

    /* Clean up transient cache tracking field */
    delete conv._cachedUpdatedAt;
    return conv;
  } catch (e) {
    debugLog(`Load conv ${convId}: ${e.message}`, "warn");
    /* If we had a cache hit, the user already sees content — just log the fetch failure */
    if (cacheHit) {
      console.warn(`[loadConvMsgs] ⚠️ Server fetch failed for ${convId.slice(0,8)} but cache was served: ${e.message}`);
      return conv;
    }
    /* Network errors with no cache: show a retry-friendly message if this is the active conv */
    if (convId === activeConvId) {
      const inner = document.getElementById('chatInner');
      if (inner && conv._needsLoad && conv.messages.length === 0) {
        inner.innerHTML = `<div class="welcome" id="welcome" style="opacity:0.7"><div class="welcome-icon">⚡</div><h2>Failed to load conversation</h2><p>${e.message}</p><p style="margin-top:1em"><button onclick="loadConversation('${convId}')" class="action-btn" style="padding:8px 16px;cursor:pointer">🔄 Retry</button></p></div>`;
      }
    }
    return conv;
  }
}

/**
 * Force-recover a conversation from server, ignoring local state.
 * Use when local messages appear truncated or missing.
 * Can be called from browser console: forceRecoverFromServer(convId)
 */
async function forceRecoverFromServer(convId) {
  convId = convId || activeConvId;
  if (!convId) { debugLog('[recover] No conversation ID', 'error'); return null; }
  const conv = conversations.find((c) => c.id === convId);
  if (!conv) { debugLog(`[recover] Conversation not found: ${convId}`, 'error'); return null; }
  try {
    const data = await Api.conversations.get(convId);
    if (!data) { debugLog('[recover] Server returned no data', 'error'); return null; }
    const serverMsgs = data.messages || [];
    const localMsgs = conv.messages || [];
    console.log(`[recover] Conv ${convId}: local has ${localMsgs.length} msgs, server has ${serverMsgs.length} msgs`);
    if (serverMsgs.length > localMsgs.length) {
      conv.messages = serverMsgs;
      conv.title = data.title || conv.title;
      conv.updatedAt = data.updatedAt || data.updated_at || conv.updatedAt;
      conv._serverMsgCount = serverMsgs.length;
      conv._needsLoad = false;
      const keepPinned = conv.pinned, keepPinnedAt = conv.pinnedAt;
      _applySettingsToConv(conv, data.settings);
      conv.pinned = keepPinned; conv.pinnedAt = keepPinnedAt;
      saveConversations(convId);
      if (convId === activeConvId) {
        renderChat(conv, false);
        if (typeof _restoreConvToolState === 'function') _restoreConvToolState(conv);
      }
      console.log(`[recover] ✅ Restored ${serverMsgs.length} messages (was ${localMsgs.length})`);
      return conv;
    } else {
      console.log(`[recover] ℹ️ Server has same or fewer messages — no recovery needed`);
      return conv;
    }
  } catch (e) {
    debugLog(`[recover] Failed: ${e.message}`, 'error');
    return null;
  }
}

/**
 * Audit all conversations for data loss: compare local message count vs server.
 * Run from console: auditConversations()
 */
async function auditConversations() {
  console.log('[audit] Checking all conversations for data loss...');
  const issues = [];
  for (const conv of conversations) {
    try {
      const data = await Api.conversations.get(conv.id);
      if (!data) continue;
      const serverCount = (data.messages || []).length;
      const localCount = (conv.messages || []).length;
      if (serverCount > localCount) {
        issues.push({ id: conv.id, title: conv.title, localCount, serverCount, diff: serverCount - localCount });
        console.warn(`[audit] ⚠️ "${conv.title}" — local: ${localCount}, server: ${serverCount} (+${serverCount - localCount} recoverable)`);
      }
    } catch (e) { /* skip */ }
  }
  if (issues.length === 0) {
    console.log('[audit] ✅ No data loss detected — all conversations match server');
  } else {
    console.log(`[audit] Found ${issues.length} conversation(s) with recoverable data:`);
    console.table(issues);
    console.log('[audit] Run forceRecoverFromServer("conv_id") to recover, or recoverAll() to fix all');
  }
  return issues;
}

/**
 * Batch recover all conversations that have more messages on server than locally.
 * Run from console: recoverAll()
 */
async function recoverAll() {
  const issues = await auditConversations();
  if (issues.length === 0) return;
  let recovered = 0;
  for (const issue of issues) {
    const result = await forceRecoverFromServer(issue.id);
    if (result) recovered++;
    await new Promise(r => setTimeout(r, 200)); /* small delay to not hammer server */
  }
  console.log(`[recoverAll] ✅ Recovered ${recovered}/${issues.length} conversations`);
}

