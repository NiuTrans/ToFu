/* ════════════════════════════════════
   image-gen-batch.js — Gacha (batch) image generation
   Extracted from image-gen.js (2026-07). _igBatchModels + _igGenerateBatch:
   fire N parallel generation requests, render the slot grid, save partial
   results incrementally. Plain window-scope concatenation (NOT an IIFE) —
   called at runtime from generateImageDirect; shares _igGenerating /
   _igAbortControllers / _IG_ALL_MODELS with image-gen.js. Load order is free
   (both before main.js).
   ════════════════════════════════════ */

// ═══════════════════════════════════════════════════
// ★ Gacha Mode — Batch Image Generation
// ═══════════════════════════════════════════════════

/**
 * Determine which models to use for each batch slot.
 * - All Models: cycle through _IG_ALL_MODELS
 * - Specific model: repeat it `count` times
 */
function _igBatchModels(count) {
  if (_igSelectedModel === '__all__') {
    const models = [];
    for (let i = 0; i < count; i++) models.push(_IG_ALL_MODELS[i % _IG_ALL_MODELS.length]);
    return models;
  }
  return Array(count).fill(_igSelectedModel);
}

/**
 * Fire N parallel image generation requests and display results in a grid.
 * Each slot shows an independent loading spinner → reveal animation.
 * Results are saved incrementally — partial results survive page refresh.
 */
async function _igGenerateBatch(prompt, count) {
  _igGenerating = true;
  const genBtn = document.getElementById('igGenerateBtn');
  if (genBtn) genBtn.disabled = true;

  // ★ Wrap the entire batch body in try/finally so that if ANY step throws
  //   (renderChat, DOM ops, saveConversations, a .then handler re-throw, etc.)
  //   we still release _igGenerating and re-enable the button.  Without this,
  //   a single exception would leave the button stuck in cursor:not-allowed
  //   state, requiring a page refresh.
  try {
  // ── Ensure conversation exists (manual creation — same as generateImageDirect) ──
  let conv = getActiveConv();
  if (!conv) {
    const now = Date.now();
    conv = { id: 'conv-' + now + '-' + Math.random().toString(36).slice(2,8),
             title: 'New Chat', messages: [], createdAt: now, updatedAt: now,
             activeTaskId: null };
    conversations.unshift(conv);
    activeConvId = conv.id;
    sessionStorage.setItem('tofu_activeConvId', conv.id);
    _saveConvToolState();
    if (typeof renderConversationList === 'function') renderConversationList();
  }
  conv.imageGenMode = true;

  // ── Add user prompt as a message ──
  const userMsg = { role: 'user', content: prompt, timestamp: Date.now(), _isImageGen: true };
  _ensureMsgId(userMsg);
  conv.messages.push(userMsg);

  // ── Set title from prompt on first user message ──
  if (conv.messages.filter(m => m.role === 'user').length === 1) {
    conv.title = prompt.slice(0, 50);
    renderConversationList();
  }

  const textarea = document.getElementById('userInput');
  if (textarea) { textarea.value = ''; textarea.style.height = 'auto'; }

  const chatDiv = document.getElementById('chatInner');

  // ── Determine models for each slot ──
  const models = _igBatchModels(count);
  const batchId = 'ig-batch-' + Date.now();
  const t0 = Date.now();

  // ── Collect multi-turn history (unified) ──
  const igHistory = _igCollectHistory(conv);
  const historyCount = igHistory.length;

  // ── Pre-create assistant message with pending results (for incremental save) ──
  const pendingResults = models.map((m, i) => ({
    ok: false, prompt, model: m, provider_id: '',
    aspect_ratio: _igSelectedAspect, resolution: _igSelectedResolution,
    image_url: '', remote_image_url: '', file_size: 0, elapsed: '',
    response_text: '', error: 'pending', errorType: '',
  }));
  const assistantMsg = {
    role: 'assistant',
    content: 'Generating…',
    timestamp: Date.now(),
    _igResults: pendingResults,
    _isImageGen: true,
    _igBatchPending: true,  // flag: batch still in progress
  };
  _ensureMsgId(assistantMsg);
  const msgIdx = conv.messages.length;
  conv.messages.push(assistantMsg);

  // ── Render user message + loading grid ──
  window.ConvView.replaceAll(conv.id);

  const isAllModels = _igSelectedModel === '__all__';
  const bannerText = isAllModels ? `全模型 ${count}连抽!` : `${count}连抽!`;
  const historyBadge = historyCount > 0 ? ` · ${historyCount} prior turn${historyCount > 1 ? 's' : ''}` : '';
  const gridHtml = `<div class="ig-batch-wrapper" id="${batchId}">
    <div class="ig-batch-banner">${bannerText}${historyBadge}</div>
    <div class="ig-batch-grid ig-cols-${Math.min(count, 2)}">
      ${models.map((m, i) => `<div class="ig-batch-slot" id="${batchId}-slot-${i}" data-slot-idx="${i}" data-msg-idx="${msgIdx}">
        <div class="ig-generating ig-batch-loading">
          <div class="ig-gen-spinner"></div>
          <div class="ig-gen-title">${_escapeHtmlBasic(_IG_MODEL_SHORT[m] || m)}</div>
          <div class="ig-gen-subtitle">生成中… (${i + 1}/${count})</div>
          <div class="ig-gen-timer" id="${batchId}-timer-${i}">0.0s</div>
        </div>
      </div>`).join('')}
    </div>
    <div class="ig-batch-footer">
      <button class="ig-gen-cancel" onclick="_igCancelGeneration()">✕ 取消全部</button>
    </div>
  </div>`;
  if (chatDiv) {
    /* Tail insert via the shared furniture-aware primitive — a raw `beforeend`
     * lands BELOW a bottom lazy-window sentinel. */
    if (typeof chatInnerInsert === 'function') {
      chatInnerInsert(chatDiv, gridHtml, {
        position: 'tail', conv: conv, site: '_igGenerateBatch:grid',
      });
    } else {
      chatDiv.insertAdjacentHTML('beforeend', gridHtml);
    }
    chatDiv.scrollTop = chatDiv.scrollHeight;
  }

  // ── Save early with pending results ──
  saveConversations(conv.id);

  // ── Start per-slot timers ──
  const slotTimers = models.map((_, i) => {
    const timerId = `${batchId}-timer-${i}`;
    return setInterval(() => {
      const el = document.getElementById(timerId);
      if (el) el.textContent = ((Date.now() - t0) / 1000).toFixed(1) + 's';
    }, 100);
  });

  if (historyCount > 0) {
    _igToast(`Sending ${historyCount} prior turn${historyCount > 1 ? 's' : ''} for multi-turn editing`, 'info');
  }

  // ── Track completed count for progressive save ──
  let completedCount = 0;

  // ── Fire parallel requests ──
  _igAbortControllers = models.map(() => new AbortController());
  const settled = await Promise.allSettled(models.map((model, i) => {
    const body = {
      prompt,
      model: model,
      aspect_ratio: _igSelectedAspect,
      resolution: _igSelectedResolution,
    };
    if (igHistory.length > 0) body.history = igHistory;

    return Api.images.generate(body, { signal: _igAbortControllers[i]?.signal }).then(async data => {
      clearInterval(slotTimers[i]);
      const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
      const slotEl = document.getElementById(`${batchId}-slot-${i}`);

      if (data.ok && (data.image_url || data.image_b64)) {
        const imgSrc = data.image_url
          ? (data.image_url.startsWith('/') ? apiUrl(data.image_url) : data.image_url)
          : `data:${data.mime_type || 'image/png'};base64,${data.image_b64}`;
        const sizeStr = data.file_size ? _formatFileSize(data.file_size) : '';

        if (slotEl) {
          slotEl.innerHTML = `<div class="ig-result-card ig-batch-reveal" style="animation-delay:${i * 0.1}s">
            <img src="${imgSrc}" alt="${_escapeHtmlBasic(prompt.slice(0, 60))}" loading="lazy"
                 onclick="_openImageFullscreen(this.src)" />
            <div class="ig-result-footer">
              <span class="ig-result-prompt" title="${_escapeHtmlBasic(prompt)}">${_escapeHtmlBasic(_IG_MODEL_SHORT[data.model || model] || data.model || model)}</span>
              <div class="ig-result-meta">
                ${sizeStr ? `<span class="ig-meta-pill">${sizeStr}</span>` : ''}
                <span class="ig-meta-pill">${elapsed}s</span>
              </div>
              <div class="ig-result-actions">
                <button onclick="event.stopPropagation();_downloadGenImage(this)" title="Download">⬇</button>
                <button onclick="event.stopPropagation();_openImageFullscreen(this.closest('.ig-result-card').querySelector('img').src)" title="Fullscreen">⛶</button>
              </div>
            </div>
          </div>`;
        }

        // ── Incrementally update saved result ──
        assistantMsg._igResults[i] = /** @type {any} */ ({
          ok: true, prompt, model: data.model || model, provider_id: data.provider_id || '',
          aspect_ratio: _igSelectedAspect, resolution: _igSelectedResolution,
          image_url: data.image_url || '', remote_image_url: data.remote_image_url || '',
          file_size: data.file_size || 0, elapsed, response_text: data.text || '', error: '',
        });
      } else {
        const errInfo = _igClassifyError(data, data._status);
        if (slotEl) {
          slotEl.setAttribute('data-slot-idx', String(i));
          slotEl.setAttribute('data-msg-idx', String(msgIdx));
          slotEl.innerHTML = _igBatchErrorSlotHtml(errInfo, model, msgIdx, i, prompt);
        }

        // Show toast for specific error types
        if (errInfo.isRateLimit) {
          _igToast(`⏳ Slot ${String(i + 1)} rate limited`, 'warning');
        } else if (errInfo.isContentBlocked) {
          _igToast(`🚫 Slot ${String(i + 1)} content blocked`, 'error');
        }

        assistantMsg._igResults[i] = {
          ok: false, prompt, model: data.model || model, provider_id: data.provider_id || '',
          aspect_ratio: _igSelectedAspect, resolution: _igSelectedResolution,
          image_url: '', remote_image_url: '', file_size: 0, elapsed,
          response_text: data.text || '', error: errInfo.text, errorType: errInfo.errorType,
        };
      }

      // ── Progressive save after each slot completes ──
      completedCount++;
      const okSoFar = assistantMsg._igResults.filter(r => r.ok);
      assistantMsg.content = okSoFar.length > 0
        ? okSoFar.map(r => `![Generated Image](${r.image_url || 'data:image'})`).join('\n\n')
        : (completedCount < count ? 'Generating…' : 'All image generations failed');
      saveConversations(conv.id);

      if (chatDiv) chatDiv.scrollTop = chatDiv.scrollHeight;
      return { ...data, _slotIndex: i, _elapsed: elapsed, _model: model };
    }).catch(err => {
      clearInterval(slotTimers[i]);
      const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
      const isAbort = err.name === 'AbortError';
      const errText = isAbort ? 'Cancelled' : (err.message || 'Request failed');
      const slotEl = document.getElementById(`${batchId}-slot-${i}`);

      const errInfo = { title: isAbort ? 'Cancelled' : 'Network error', text: errText, errorType: isAbort ? 'cancelled' : 'network', isRateLimit: false, isContentBlocked: false };
      if (slotEl) {
        slotEl.setAttribute('data-slot-idx', String(i));
        slotEl.setAttribute('data-msg-idx', String(msgIdx));
        slotEl.innerHTML = _igBatchErrorSlotHtml(errInfo, model, msgIdx, i, prompt);
      }

      assistantMsg._igResults[i] = {
        ok: false, prompt, model: model, provider_id: '',
        aspect_ratio: _igSelectedAspect, resolution: _igSelectedResolution,
        image_url: '', remote_image_url: '', file_size: 0, elapsed,
        response_text: '', error: errText, errorType: errInfo.errorType,
      };

      completedCount++;
      saveConversations(conv.id);
      throw err;  // re-throw so Promise.allSettled captures it
    });
  }));

  // ── Clear all timers ──
  slotTimers.forEach(t => clearInterval(t));

  // ── Remove cancel button, mark batch complete ──
  const footerEl = document.querySelector(`#${batchId} .ig-batch-footer`);
  if (footerEl) footerEl.remove();
  delete assistantMsg._igBatchPending;

  // ── Final save with all results ──
  const results = assistantMsg._igResults;
  const okResults = results.filter(r => r.ok);
  assistantMsg.content = okResults.length > 0
    ? okResults.map(r => `![Generated Image](${r.image_url || 'data:image'})`).join('\n\n')
    : `All ${count} image generations failed`;

  // ── Re-render chat from messages so the batch results survive DOM wipes ──
  if (conv.id === activeConvId) window.ConvView.replaceAll(conv.id, { forceScroll: true });
  saveConversations(conv.id);
  syncConversationToServer(conv);

  // ── Cleanup (happy path) ──
  if (conv.id === activeConvId && chatDiv) chatDiv.scrollTop = chatDiv.scrollHeight;

  const anyOk = okResults.length > 0;
  debugLog(`Batch generation complete: ${okResults.length}/${count} succeeded`, anyOk ? 'success' : 'warning');
  } catch (err) {
    // ★ Log but don't rethrow — button-unlock happens in finally.
    console.error('[ImageGen] _igGenerateBatch threw:', err);
    debugLog(`Batch generation error: ${err?.message || err}`, 'error');
  } finally {
    // ★ ALWAYS release the lock and re-enable the button, even on throw.
    _igGenerating = false;
    _igAbortControllers = [];
    if (genBtn) genBtn.disabled = false;
  }
}
