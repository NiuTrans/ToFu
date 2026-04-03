/* ═══════════════════════════════════════════
   translation.js — Async Translation Tasks
   ═══════════════════════════════════════════ */
async function _callTranslateAPI(text, targetLang, sourceLang, timeoutMs) {
  // Scale timeout with text length: large texts need more time for LLM to complete
  if (!timeoutMs) timeoutMs = text.length > 6000 ? 120000 : text.length > 3000 ? 90000 : 60000;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let r;
  try {
    r = await fetch(apiUrl("/api/translate"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, targetLang, sourceLang }),
      signal: ctrl.signal,
    });
  } catch (fetchErr) {
    clearTimeout(timer);
    if (fetchErr.name === 'AbortError') {
      throw new Error("Translation timed out — server may be overloaded");
    }
    throw fetchErr;
  } finally {
    clearTimeout(timer);
  }
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    const detail = body.error || r.statusText || r.status;
    throw new Error(`Translation failed: ${detail}`);
  }
  const d = await r.json();
  if (!d.translated) throw new Error("Translation returned empty result");
  return d.translated;
}

// ═══════════════════════════════════════════════════════════
//  ★ Async Translation Tasks — fire-and-forget, survive page reload
// ═══════════════════════════════════════════════════════════

/**
 * Start a server-side async translation task.
 * Returns taskId immediately. The server will:
 * 1) Run the translation in a background thread
 * 2) Auto-commit the result into the DB conversation
 * So even if the user closes the tab, the translation completes.
 */
async function _startTranslateTask(text, targetLang, sourceLang, convId, msgIdx, field) {
  try {
    const r = await fetch(apiUrl("/api/translate/start"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, targetLang, sourceLang, convId, msgIdx, field }),
    });
    if (!r.ok) {
      console.error("[TranslateTask] start failed:", r.status);
      return null;
    }
    const d = await r.json();
    console.log(`%c[TranslateTask] Started ${d.taskId} for conv=${convId?.slice(0,8)} msg=${msgIdx} field=${field}`, 'color:#8b5cf6');
    return d.taskId;
  } catch (e) {
    console.error("[TranslateTask] start error:", e);
    return null;
  }
}

/**
 * Poll a single translation task. Returns {status, translated?, error?}
 */
async function _pollTranslateTask(taskId) {
  try {
    const r = await fetch(apiUrl(`/api/translate/poll/${taskId}`));
    if (r.status === 404) return { status: 'not_found' };
    if (!r.ok) return { status: 'error', error: `HTTP ${r.status}` };
    return await r.json();
  } catch (e) {
    return { status: 'error', error: e.message };
  }
}

/**
 * Poll multiple translation tasks in one request.
 */
async function _pollTranslateTaskBatch(taskIds) {
  if (!taskIds.length) return [];
  try {
    const r = await fetch(apiUrl("/api/translate/poll_batch"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ taskIds }),
    });
    if (!r.ok) return taskIds.map(id => ({ taskId: id, status: 'error' }));
    return await r.json();
  } catch (e) {
    return taskIds.map(id => ({ taskId: id, status: 'error', error: e.message }));
  }
}

/**
 * Resume any pending translation tasks for a conversation.
 * Called on page load and when switching to a conversation.
 * Scans messages for _translateTaskId without completed translation,
 * polls the server, and applies results.
 */
async function _resumePendingTranslations(convId) {
  const conv = conversations.find(c => c.id === convId);
  if (!conv || !conv.messages) return;

  // ★ FIX: never auto-translate while streaming — content is still growing,
  //   translating partial content produces incomplete translations.
  //   finishStream() will trigger translation when the stream is done.
  if (activeStreams.has(convId)) {
    console.log(`%c[TranslateTask] ⏭ Skipping _resumePendingTranslations — stream active for conv=${convId.slice(0,8)}`, 'color:#8b5cf6');
    return;
  }

  // ── Phase 0: detect the LAST assistant msg that should have been translated but wasn't ──
  // (e.g. page closed before finishStream could start the task)
  // Only check the last assistant message — don't retroactively translate old history
  const _convAutoTranslate = conv.autoTranslate !== undefined ? !!conv.autoTranslate : true;
  if (_convAutoTranslate && conv.messages.length > 0) {
    // Find the last assistant message (skip empty ghost messages)
    for (let i = conv.messages.length - 1; i >= 0; i--) {
      const msg = conv.messages[i];
      if (msg.role !== 'assistant') continue;
      if (!msg.content) continue;  // ★ skip ghost empty assistant msgs, don't break
      // ★ FIX: detect stale partial translations before skipping —
      // if translatedContent exists but is < 15% of content length, it was translated
      // from partial content (mid-stream) and needs re-translation.
      if (msg.translatedContent && msg.content.length > 500 &&
          msg.translatedContent.length < msg.content.length * 0.15) {
        console.warn(`[TranslateTask] 🔄 Stale partial translation in resume — ` +
          `translated=${msg.translatedContent.length} vs content=${msg.content.length} ` +
          `(${(msg.translatedContent.length/msg.content.length*100).toFixed(1)}%) — re-translating msg ${i}`);
        delete msg.translatedContent;
        delete msg._translatedCache;
        delete msg._translateDone;
        delete msg._translateTaskId;
        msg._translateField = 'translatedContent';
        msg._translateDone = false;
        _startAutoTranslateForMsg(conv, convId, i, msg);
        break;
      }
      if (msg.translatedContent || msg._translateTaskId || msg._translateDone !== undefined) break;
      // ★ Skip image gen results — nothing meaningful to translate
      if (msg._igResult || msg._isImageGen || msg._igResults) break;
      // ★ FIX: When autoTranslate is ON, always translate — don't rely on the
      // language heuristic which fails for bilingual/mixed-language responses.
      console.log(`%c[TranslateTask] 🔄 Auto-starting missed translation for msg ${i} in conv=${convId.slice(0,8)}`, 'color:#8b5cf6');
      msg._translateField = 'translatedContent';
      msg._translateDone = false;
      _startAutoTranslateForMsg(conv, convId, i, msg);
      break; // only check the last assistant msg
    }
  }

  // ── Phase 1: collect messages with active translate tasks ──
  const pending = [];
  conv.messages.forEach((msg, idx) => {
    if (msg._translateTaskId && !msg._translateDone) {
      // Check if the translation already landed (e.g. server auto-committed and we reloaded)
      if (msg._translateField === 'translatedContent' && msg.translatedContent) {
        msg._translateDone = true;
        return;
      }
      if (msg._translateField === 'content' && msg.originalContent && msg.content !== msg.originalContent) {
        msg._translateDone = true;
        return;
      }
      pending.push({ taskId: msg._translateTaskId, idx, msg });
    }
  });

  if (!pending.length) return;
  console.log(`%c[TranslateTask] Resuming ${pending.length} pending translations for conv=${convId.slice(0,8)}`, 'color:#8b5cf6;font-weight:bold');

  // ── Phase 2: immediately re-render to show "翻译中…" indicators ──
  // ★ Use forceScroll=false to preserve scroll position (prevents jump-to-top flash)
  if (activeConvId === convId) {
    renderChat(conv, false);
  }

  const taskIds = pending.map(p => p.taskId);
  const results = await _pollTranslateTaskBatch(taskIds);

  let anyApplied = false;
  let anyStillRunning = false;
  for (const result of results) {
    const p = pending.find(x => x.taskId === result.taskId);
    if (!p) continue;

    if (result.status === 'done' && result.translated) {
      // Apply the translation
      const field = p.msg._translateField || 'translatedContent';
      if (field === 'translatedContent') {
        p.msg.translatedContent = result.translated;
        p.msg._showingTranslation = true;
      } else if (field === 'content') {
        if (!p.msg.originalContent) p.msg.originalContent = p.msg.content;
        p.msg.content = result.translated;
      }
      p.msg._translateDone = true;
      anyApplied = true;
      console.log(`%c[TranslateTask] ✓ Applied ${result.taskId} to msg ${p.idx} (${field})`, 'color:#22c55e');
    } else if (result.status === 'running') {
      // Still running — will re-poll
      anyStillRunning = true;
      console.log(`%c[TranslateTask] ⏳ ${result.taskId} still running, will re-poll...`, 'color:#f59e0b');
    } else if (result.status === 'error' || result.status === 'not_found') {
      // Task expired from memory or errored — check DB for auto-committed result
      console.warn(`[TranslateTask] ${result.taskId} ${result.status} — checking DB for auto-committed translation...`);
      let dbRecovered = false;
      try {
        const resp = await fetch(apiUrl(`/api/conversations/${convId}`));
        if (resp.ok) {
          const data = await resp.json();
          const dbMsg = data.messages?.[p.idx];
          if (dbMsg) {
            const field = p.msg._translateField || 'translatedContent';
            if (field === 'translatedContent' && dbMsg.translatedContent) {
              p.msg.translatedContent = dbMsg.translatedContent;
              p.msg._showingTranslation = true;
              p.msg._translateDone = true;
              anyApplied = true;
              dbRecovered = true;
              console.log(`%c[TranslateTask] ✓ DB recovery: found translatedContent for msg ${p.idx}`, 'color:#22c55e');
            } else if (field === 'content' && dbMsg.content && p.msg.originalContent && dbMsg.content !== p.msg.originalContent) {
              p.msg.content = dbMsg.content;
              p.msg._translateDone = true;
              anyApplied = true;
              dbRecovered = true;
              console.log(`%c[TranslateTask] ✓ DB recovery: found translated content for msg ${p.idx}`, 'color:#22c55e');
            }
          }
        }
      } catch (e2) { /* ignore DB check error */ }
      if (!dbRecovered) {
        // If autoTranslate is on, retry the translation instead of giving up
        if (_convAutoTranslate && p.msg._translateField === 'translatedContent' && p.msg.content) {
          console.log(`%c[TranslateTask] 🔄 Re-starting expired translation for msg ${p.idx}`, 'color:#8b5cf6');
          p.msg._translateTaskId = null;
          p.msg._translateDone = false;
          delete p.msg._translateError;
          _startAutoTranslateForMsg(conv, convId, p.idx, p.msg);
        } else {
          p.msg._translateDone = true;
          p.msg._translateError = result.error || 'Task expired';
          anyApplied = true;  // need re-render to show error state
          console.warn(`[TranslateTask] ✗ ${result.taskId} failed, no DB recovery: ${result.error || 'not found'}`);
        }
      }
    }
  }

  if (anyApplied) {
    saveConversations(convId);
    syncConversationToServer(conv);
    // ★ Use forceScroll=false to preserve scroll position (prevents jump-to-top flash)
    if (activeConvId === convId) {
      renderChat(conv, false);
    }
  }

  // Schedule re-poll for tasks still running
  if (anyStillRunning) {
    setTimeout(() => _resumePendingTranslations(convId), 3000);
  }
}
