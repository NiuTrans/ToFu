/* ═══════════════════════════════════════════
   image-gen.js — Image Generation — Creative Mode
   ═══════════════════════════════════════════ */
/* ═══════════════════════════════════════════
   ★ Image Generation — Creative Mode
   ═══════════════════════════════════════════ */
var _igSelectedModel = 'gemini-3.1-flash-image-preview';
var _igSelectedAspect = '1:1';
var _igSelectedResolution = '1K';   // 1K | 2K
var _igSelectedCount = 1;           // 1 | 2 | 4 — batch count
let _igGenerating = false;
let _igAbortController = null;       // AbortController for single request
let _igAbortControllers = [];        // AbortControllers for batch requests

// All available image gen models (order matches dropdown)
const _IG_ALL_MODELS = [
  'gemini-3.1-flash-image-preview',
  'gemini-3-pro-image-preview',
  'gemini-2.5-flash-image',
  'gpt-image-1.5',
  'gpt-image-2',
];
var _IG_MODEL_SHORT = {
  'gemini-3.1-flash-image-preview': 'Gemini 3.1 Flash',
  'gemini-3-pro-image-preview': 'Gemini 3 Pro',
  'gemini-2.5-flash-image': 'Gemini 2.5 Flash',
  'gpt-image-1.5': 'GPT Image 1.5',
  'gpt-image-2': 'GPT Image 2',
};

// ═══════════════════════════════════════════════════
// ★ Unified history collection for multi-turn editing
// ═══════════════════════════════════════════════════

/**
 * Collect multi-turn image generation history from conversation messages.
 * Scans both _igResult (single) and _igResults (batch) messages.
 *
 * @param {Object} conv — conversation object
 * @returns {Array<{prompt: string, image_url: string, text: string}>}
 */
function _igCollectHistory(conv) {
  const history = [];
  if (!conv || !conv.messages) return history;
  for (const m of conv.messages) {
    // Single-mode result
    if (m._igResult && m._igResult.image_url) {
      history.push({
        prompt: m._igResult.prompt || '',
        image_url: m._igResult.remote_image_url || m._igResult.image_url || '',
        text: m._igResult.response_text || '',
      });
    }
    // Batch-mode results — pick the first successful one as representative
    if (m._igResults) {
      for (const r of m._igResults) {
        if (r.ok && r.image_url) {
          history.push({
            prompt: r.prompt || '',
            image_url: r.remote_image_url || r.image_url || '',
            text: r.response_text || '',
          });
          break; // one representative per batch round
        }
      }
    }
  }
  return history;
}

// ═══════════════════════════════════════════════════
// ★ Error type classification & toast helpers
// ═══════════════════════════════════════════════════

/**
 * Classify an error response from the image gen API into a structured _igError.
 *
 * @param {Object} data — response JSON from /api/v1/images/generate
 * @param {number} httpStatus — HTTP status code
 * @returns {{title: string, text: string, detail: string, errorType: string, isTimeout: boolean, isRateLimit: boolean, isContentBlocked: boolean}}
 */
function _igClassifyError(data, httpStatus) {
  const errorType = data.error_type || '';
  const errText = data.error || 'Unknown error';
  const blockReason = data.block_reason || '';

  let title = 'Image generation failed';
  let isRateLimit = false;
  let isContentBlocked = false;
  let isTimeout = false;

  if (errorType === 'rate_limited' || httpStatus === 429 || data.rate_limited) {
    title = 'Rate limited';
    isRateLimit = true;
  } else if (errorType === 'content_blocked' || blockReason) {
    title = 'Content blocked';
    isContentBlocked = true;
  } else if (errorType === 'timeout') {
    title = 'Generation timed out';
    isTimeout = true;
  } else if (errorType === 'no_slot') {
    title = 'No model available';
  }

  return /** @type {any} */ ({
    title,
    text: errText,
    detail: data.text || '',
    errorType: errorType || 'generation_failed',
    blockReason,
    isTimeout,
    isRateLimit,
    isContentBlocked,
  });
}

/**
 * Show a toast notification for image generation state changes.
 */
function _igToast(message, type) {
  if (typeof debugLog === 'function') {
    debugLog(message, type || 'info');
  }
}

function enterImageGenMode() {
  if (imageGenMode) { exitImageGenMode(); return; }
  // Exit paper mode if active (mutually exclusive)
  if (typeof paperMode !== 'undefined' && paperMode && typeof exitPaperMode === 'function') exitPaperMode();
  _applyImageGenUI(true);
  _saveConvToolState();
  if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
  debugLog('Image Gen Mode: ENTER', 'success');
  // Focus the textarea
  document.getElementById('userInput')?.focus();
}
function exitImageGenMode() {
  _applyImageGenUI(false);
  _saveConvToolState();
  if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
  debugLog('Image Gen Mode: EXIT', 'info');
}

function toggleIgModelDropdown(e) {
  e.stopPropagation();
  const wrapper = document.getElementById('igModelPicker');
  if (!wrapper) return;
  wrapper.classList.toggle('open');
  // Same close-on-outside-click pattern as togglePresetDropdown()
  if (wrapper.classList.contains('open')) {
    const closeHandler = function (ev) {
      if (!wrapper.contains(ev.target)) {
        wrapper.classList.remove('open');
        document.removeEventListener('click', closeHandler);
      }
    };
    setTimeout(() => document.addEventListener('click', closeHandler), 0);
  }
}
function selectIgModel(el) {
  _igSelectedModel = el.dataset.model;
  // Update active state — highlight all instances of the same model (may appear under multiple providers)
  el.closest('.ig-preset-dropdown').querySelectorAll('.ig-model-option').forEach(o => {
    o.classList.toggle('active', o.dataset.model === _igSelectedModel);
  });
  // Update toggle label + brand icon (same pattern as preset toggle)
  const label = document.getElementById('igModelLabel');
  const iconEl = document.getElementById('igModelIcon');
  const toggle = document.querySelector('.ig-preset');
  if (_igSelectedModel === '__all__') {
    if (label) label.textContent = 'All Models';
    if (iconEl) iconEl.innerHTML = '';
    if (toggle) toggle.setAttribute('data-brand', 'generic');
    // Auto-set count to 4 (one per model) when switching to All Models
    if (_igSelectedCount < 2) {
      _igSelectedCount = 4;
      document.querySelectorAll('#igCountBar .ig-pill').forEach(b => {
        b.classList.toggle('active', b.dataset.count === '4');
      });
      const genText = document.querySelector('.ig-gen-text');
      if (genText) genText.textContent = '4连抽!';
    }
  } else {
    const name = el.querySelector('.ig-model-name')?.textContent || _igSelectedModel;
    if (label) label.textContent = name;
    // Update brand icon + color on the toggle
    const brand = typeof _detectBrand === 'function' ? _detectBrand(_igSelectedModel) : 'generic';
    if (iconEl && typeof _brandSvg === 'function') iconEl.innerHTML = _brandSvg(brand, 14);
    if (toggle) toggle.setAttribute('data-brand', brand);
  }

  // Close dropdown
  document.getElementById('igModelPicker')?.classList.remove('open');
  /* ★ Reflow toolbar after model label change */
  if (typeof _scheduleReflow === 'function') _scheduleReflow();
}
function selectIgAspect(el) {
  _igSelectedAspect = el.dataset.ar;
  document.querySelectorAll('#igAspectBar .ig-pill').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
}
function selectIgResolution(el) {
  _igSelectedResolution = el.dataset.res;
  document.querySelectorAll('#igResolutionBar .ig-pill').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
}
function selectIgCount(el) {
  _igSelectedCount = parseInt(el.dataset.count, 10) || 1;
  document.querySelectorAll('#igCountBar .ig-pill').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
  // Update generate button label — gacha style
  const genText = document.querySelector('.ig-gen-text');
  if (genText) genText.textContent = _igSelectedCount > 1 ? `${_igSelectedCount}连抽!` : '生成';
}

// Outside-click is now handled inside toggleIgModelDropdown() (same pattern as preset toggle).

async function generateImageDirect() {
  if (_igGenerating) return;
  const textarea = document.getElementById('userInput');
  const prompt = (textarea?.value || '').trim();
  if (!prompt) {
    debugLog('Please describe the image you want to create or edit', 'warning');
    textarea?.focus();
    return;
  }

  // ── Wait for any still-compressing/uploading source images (mobile) so an
  //    edit never fires with an entry whose base64 isn't ready yet. ──
  if (typeof _waitForImageProcessing === 'function') await _waitForImageProcessing();

  // ── Collect source images for editing ──
  const sourceImages = [...pendingImages].filter(im => im && (im.base64 || im.url));
  const isEdit = sourceImages.length > 0;

  // ── Route to batch generation when count > 1 or All Models selected ──
  // (batch mode not supported with editing — single only)
  const effectiveCount = _igSelectedModel === '__all__'
    ? Math.max(_igSelectedCount, _IG_ALL_MODELS.length)
    : _igSelectedCount;
  if (effectiveCount > 1 && !isEdit) {
    return _igGenerateBatch(prompt, effectiveCount);
  }

  _igGenerating = true;
  const genBtn = document.getElementById('igGenerateBtn');
  if (genBtn) genBtn.disabled = true;

  // ── Ensure conversation exists ──
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

  // ── Add user prompt as a message (with source images if editing) ──
  const userMsg = { role: 'user', content: prompt, timestamp: Date.now(), _isImageGen: true };
  if (isEdit) {
    userMsg.images = sourceImages;
    userMsg._isImageEdit = true;
  }
  _ensureMsgId(userMsg);
  conv.messages.push(userMsg);

  // ── Set title from prompt on first user message ──
  if (conv.messages.filter(m => m.role === 'user').length === 1) {
    const titleText = isEdit ? prompt : prompt;
    conv.title = titleText.slice(0, 60) + (titleText.length > 60 ? '...' : '');
    if (activeConvId === conv.id) {
      const _tt = document.getElementById('topbarTitle');
      if (_tt) _tt.textContent = conv.title;
    }
    renderConversationList();
  }

  window.ConvView.replaceAll(conv.id, { forceScroll: true });

  // ── Clear input and pending images ──
  textarea.value = '';
  textarea.style.height = 'auto';
  pendingImages = [];
  renderImagePreviews();

  // ── Collect multi-turn history (unified) ──
  const igHistory = _igCollectHistory(conv);
  const historyCount = igHistory.length;

  // ── Show loading card with model info and history indicator ──
  const chatDiv = document.getElementById('chatInner');
  const loadingId = 'ig-loading-' + Date.now();
  const resLabel = _igSelectedResolution !== '1K' ? ` · ${_igSelectedResolution}` : '';
  const modelLabel = _IG_MODEL_SHORT[_igSelectedModel] || _igSelectedModel;
  const actionLabel = isEdit ? 'Editing image…' : 'Generating image…';
  const historyBadge = historyCount > 0 ? `<span class="ig-history-badge" title="${historyCount} prior editing turn${historyCount > 1 ? 's' : ''}">${historyCount} prior turn${historyCount > 1 ? 's' : ''}</span>` : '';
  const loadingHtml = `<div class="ig-generating" id="${loadingId}">
    <div class="ig-gen-spinner"></div>
    <div class="ig-gen-title">${actionLabel}</div>
    <div class="ig-gen-model-info">${_escapeHtmlBasic(modelLabel)}${historyBadge}</div>
    <div class="ig-gen-subtitle">${_escapeHtmlBasic(prompt.slice(0, 100))}${prompt.length > 100 ? '…' : ''}</div>
    <div class="ig-gen-timer" id="${loadingId}-timer">0s${resLabel}</div>
    <div class="ig-gen-status" id="${loadingId}-status"></div>
    <button class="ig-gen-cancel" onclick="_igCancelGeneration()" title="Cancel">${Icon('x', 13)} Cancel</button>
  </div>`;
  /* Tail insert via the shared furniture-aware primitive — a raw `beforeend`
   * lands BELOW a bottom lazy-window sentinel. */
  if (typeof chatInnerInsert === 'function') {
    chatInnerInsert(chatDiv, loadingHtml, {
      position: 'tail', conv: conv, site: 'generateImageDirect:loading',
    });
  } else {
    chatDiv.insertAdjacentHTML('beforeend', loadingHtml);
  }
  scrollToBottom();

  // ── Save early so page refresh doesn't lose the user message ──
  saveConversations(conv.id);

  // ── Timer ──
  const t0 = Date.now();
  const timerInterval = setInterval(() => {
    const el = document.getElementById(loadingId + '-timer');
    if (el) el.textContent = ((Date.now() - t0) / 1000).toFixed(0) + 's' + resLabel;
  }, 1000);

  // ── AbortController ──
  // NO watchdog timer. Image generation is a WAIT, not a crash: a 2K
  // multi-turn edit legitimately runs past any fixed ceiling, and the old
  // 150s abort threw away an image the server was still rendering (and
  // still billing) while telling the user it "timed out". The user ends it
  // with Cancel, which calls _igCancelGeneration() -> abort().
  _igAbortController = new AbortController();

  try {
    const reqBody = {
      prompt,
      aspect_ratio: _igSelectedAspect,
      resolution: _igSelectedResolution,
      model: _igSelectedModel,
    };
    if (igHistory.length > 0) reqBody.history = igHistory;

    // ── Add source images for editing ──
    if (isEdit) {
      reqBody.source_images = sourceImages.map(img => ({
        image_b64: img.base64,
        mime_type: img.mediaType || 'image/png',
        // Also pass image_url if available (server will prefer b64 but needs URL for resolution)
        image_url: img.url || '',
      }));
    }

    if (historyCount > 0) {
      _igToast(`Sending ${historyCount} prior turn${historyCount > 1 ? 's' : ''} for multi-turn editing`, 'info');
    }

    const data = await Api.images.generate(reqBody, { signal: _igAbortController.signal });
    clearInterval(timerInterval);
    const loadingEl = document.getElementById(loadingId);

    if (data.ok) {
      const imgSrc = data.image_url
        ? (data.image_url.startsWith('/') ? apiUrl(data.image_url) : data.image_url)
        : (data.image_b64 ? `data:${data.mime_type || 'image/png'};base64,${data.image_b64}` : '');

      const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
      const sizeStr = data.file_size ? _formatFileSize(data.file_size) : '';

      if (loadingEl) loadingEl.remove();

      // Save as assistant message
      const assistantContent = data.text
        ? `${data.text}\n\n![Generated Image](${data.image_url || 'data:image'})`
        : `![Generated Image](${data.image_url || 'data:image'})`;
      const assistantMsg = {
        role: 'assistant',
        content: assistantContent,
        timestamp: Date.now(),
        _igResult: { prompt, aspect_ratio: _igSelectedAspect, resolution: _igSelectedResolution,
                     model: data.model || _igSelectedModel,
                     provider_id: data.provider_id || '',
                     image_url: data.image_url || '', elapsed,
                     file_size: data.file_size || 0,
                     remote_image_url: data.remote_image_url || '',
                     response_text: data.text || '',
                     history_turns: data.history_resolved || 0 },
      };
      _ensureMsgId(assistantMsg);
      conv.messages.push(assistantMsg);
      if (conv.id === activeConvId) window.ConvView.replaceAll(conv.id, { forceScroll: true });
      saveConversations(conv.id);
      syncConversationToServer(conv);

    } else {
      // ── API returned an error — classify and save with structured error type ──
      const errInfo = _igClassifyError(data, data._status);
      if (loadingEl) loadingEl.remove();

      // Show a toast for specific error types
      if (errInfo.isRateLimit) {
        _igToast('⏳ Rate limited — all model slots exhausted', 'warning');
      } else if (errInfo.isContentBlocked) {
        _igToast('🚫 Content policy: prompt was blocked', 'error');
      }

      const errMsg = { role: 'assistant', content: `Image generation failed: ${errInfo.text}`,
                       timestamp: Date.now(), _isImageGen: true,
                       _igError: errInfo };
      _ensureMsgId(errMsg);
      conv.messages.push(errMsg);
      if (conv.id === activeConvId) window.ConvView.replaceAll(conv.id, { forceScroll: true });
      saveConversations(conv.id);
      syncConversationToServer(conv);
    }

  } catch (err) {
    clearInterval(timerInterval);
    const loadingEl = document.getElementById(loadingId);
    const isAbort = err.name === 'AbortError';
    /* With the 150s watchdog gone, an abort can only be the user pressing
     * Cancel (or a page teardown) — never a client-side timeout. */
    const isUserCancel = isAbort;
    const errText = isUserCancel ? 'Cancelled by user.'
                                 : (err.message || 'Failed to connect to server');
    if (loadingEl) loadingEl.remove();
    console.error('[ImageGen] Direct generation error:', err);

    // ★ CRITICAL: Always push an assistant error message to prevent orphaned user messages
    const errTitle = isUserCancel ? 'Cancelled' : 'Network error';
    const errType = isUserCancel ? 'cancelled' : 'network';
    const errMsg = { role: 'assistant', content: `${isUserCancel ? 'Image generation cancelled' : 'Image generation network error'}: ${errText}`,
                     timestamp: Date.now(), _isImageGen: true,
                     _igError: { title: errTitle, text: errText, detail: '', errorType: errType, isTimeout: false, isRateLimit: false, isContentBlocked: false } };
    _ensureMsgId(errMsg);
    conv.messages.push(errMsg);
    if (conv.id === activeConvId) window.ConvView.replaceAll(conv.id, { forceScroll: true });
    saveConversations(conv.id);
    syncConversationToServer(conv);
  } finally {
    _igGenerating = false;
    _igAbortController = null;
    if (genBtn) genBtn.disabled = false;
    if (conv.id === activeConvId && chatDiv) chatDiv.scrollTop = chatDiv.scrollHeight;
  }
}

/** Update the generate button text based on whether images are pending (edit mode) */
function _igUpdateGenButton() {
  const genText = document.querySelector('.ig-gen-text');
  if (!genText) return;
  const isEdit = pendingImages.length > 0;
  // Only update if not in batch/all-models mode
  if (_igSelectedCount <= 1 && _igSelectedModel !== '__all__') {
    genText.textContent = isEdit ? '编辑' : '生成';
  }
}

/** Cancel an in-flight image generation (single or batch) */
function _igCancelGeneration() {
  if (_igAbortController) {
    _igAbortController.abort();
  }
  if (_igAbortControllers.length > 0) {
    _igAbortControllers.forEach(ac => ac.abort());
    _igAbortControllers = [];
  }
  debugLog('Image generation cancelled', 'info');
}

/** Retry the last image gen prompt from the current conversation */
function _igRetryLastPrompt() {
  const conv = getActiveConv();
  if (!conv || conv.messages.length === 0) return;
  // Find the last user image-gen message
  for (let i = conv.messages.length - 1; i >= 0; i--) {
    const m = conv.messages[i];
    if (m.role === 'user' && m._isImageGen) {
      const prompt = m.content?.trim() || '';
      const textarea = document.getElementById('userInput');
      if (textarea) { textarea.value = prompt; textarea.focus(); }
      return;
    }
  }
}

/**
 * Retry a single failed slot in a batch generation.
 * Re-fires one request for the given slot index and updates the DOM + saved results.
 */
async function _igRetryBatchSlot(msgIdx, slotIdx, prompt, model) {
  const conv = getActiveConv();
  if (!conv || !conv.messages[msgIdx]) return;
  const msg = conv.messages[msgIdx];
  if (!msg._igResults || !msg._igResults[slotIdx]) return;

  const slotEl = document.querySelector(`.ig-batch-slot[data-slot-idx="${slotIdx}"][data-msg-idx="${msgIdx}"]`);
  if (!slotEl) return;

  // Show loading in the slot
  const useModel = model || _igSelectedModel;
  const modelLabel = _IG_MODEL_SHORT[useModel] || useModel;
  slotEl.innerHTML = `<div class="ig-generating ig-batch-loading">
    <div class="ig-gen-spinner"></div>
    <div class="ig-gen-title">${_escapeHtmlBasic(modelLabel)}</div>
    <div class="ig-gen-subtitle">Retrying…</div>
    <div class="ig-gen-timer" id="ig-retry-timer-${slotIdx}">0.0s</div>
  </div>`;

  const t0 = Date.now();
  const timer = setInterval(() => {
    const el = document.getElementById(`ig-retry-timer-${slotIdx}`);
    if (el) el.textContent = ((Date.now() - t0) / 1000).toFixed(1) + 's';
  }, 100);

  try {
    const igHistory = _igCollectHistory(conv);
    const body = { prompt, model: useModel, aspect_ratio: _igSelectedAspect, resolution: _igSelectedResolution };
    if (igHistory.length > 0) body.history = igHistory;

    const data = await Api.images.generate(body);
    clearInterval(timer);
    const elapsed = ((Date.now() - t0) / 1000).toFixed(1);

    if (data.ok && (data.image_url || data.image_b64)) {
      const imgSrc = data.image_url
        ? (data.image_url.startsWith('/') ? apiUrl(data.image_url) : data.image_url)
        : `data:${data.mime_type || 'image/png'};base64,${data.image_b64}`;
      const sizeStr = data.file_size ? _formatFileSize(data.file_size) : '';
      slotEl.innerHTML = `<div class="ig-result-card ig-batch-reveal">
        <img src="${imgSrc}" alt="${_escapeHtmlBasic(prompt.slice(0, 60))}" loading="lazy"
             onclick="_openImageFullscreen(this.src)" />
        <div class="ig-result-footer">
          <span class="ig-result-prompt" title="${_escapeHtmlBasic(prompt)}">${_escapeHtmlBasic(_IG_MODEL_SHORT[data.model || useModel] || data.model || useModel)}</span>
          <div class="ig-result-meta">
            ${sizeStr ? `<span class="ig-meta-pill">${sizeStr}</span>` : ''}
            <span class="ig-meta-pill">${elapsed}s</span>
          </div>
          <div class="ig-result-actions">
            <button onclick="event.stopPropagation();_downloadGenImage(this)" title="Download">${Icon('download', 15)}</button>
            <button onclick="event.stopPropagation();_openImageFullscreen(this.closest('.ig-result-card').querySelector('img').src)" title="Fullscreen">${Icon('maximize', 15)}</button>
          </div>
        </div>
      </div>`;

      // Update the saved result
      msg._igResults[slotIdx] = {
        ok: true, prompt, model: data.model || useModel, provider_id: data.provider_id || '',
        aspect_ratio: _igSelectedAspect, resolution: _igSelectedResolution,
        image_url: data.image_url || '', remote_image_url: data.remote_image_url || '',
        file_size: data.file_size || 0, elapsed, response_text: data.text || '', error: '',
      };
      _igToast(`Slot ${slotIdx + 1} retry succeeded`, 'success');
    } else {
      const errInfo = _igClassifyError(data, data._status);
      slotEl.innerHTML = _igBatchErrorSlotHtml(errInfo, useModel, msgIdx, slotIdx, prompt);
      msg._igResults[slotIdx].error = errInfo.text;
      msg._igResults[slotIdx].errorType = errInfo.errorType;
    }

    // Update content summary
    const okResults = msg._igResults.filter(r => r.ok);
    msg.content = okResults.length > 0
      ? okResults.map(r => `![Generated Image](${r.image_url || 'data:image'})`).join('\n\n')
      : 'All image generations failed';

    saveConversations(conv.id);
    syncConversationToServer(conv);
  } catch (e) {
    clearInterval(timer);
    console.error('[ImageGen] Retry slot error:', e);
    const errInfo = { title: 'Retry failed', text: e.message || 'Network error', errorType: 'network', isRateLimit: false, isContentBlocked: false };
    slotEl.innerHTML = _igBatchErrorSlotHtml(errInfo, useModel, msgIdx, slotIdx, prompt);
  }
}

/**
 * Build HTML for an error slot in batch mode, with error-type differentiation.
 */
function _igBatchErrorSlotHtml(errInfo, model, msgIdx, slotIdx, prompt) {
  const modelLabel = _IG_MODEL_SHORT[model] || model || '?';
  let typeClass = 'ig-error-generic';
  let icon = Icon('zap', 24);
  if (errInfo.isRateLimit) {
    typeClass = 'ig-error-ratelimit';
    icon = Icon('hourglass', 24);
  } else if (errInfo.isContentBlocked) {
    typeClass = 'ig-error-blocked';
    icon = Icon('ban', 24);
  } else if (errInfo.isTimeout || errInfo.errorType === 'timeout') {
    typeClass = 'ig-error-timeout';
    icon = Icon('timer', 24);
  }
  return `<div class="ig-batch-error ${typeClass}">
    <div class="ig-error-icon">${icon}</div>
    <div class="ig-error-title">${_escapeHtmlBasic(modelLabel)}</div>
    <div class="ig-error-text">${_escapeHtmlBasic((errInfo.text || 'Failed').slice(0, 200))}</div>
    <button class="ig-slot-retry-btn" onclick="_igRetryBatchSlot(${msgIdx},${slotIdx},${JSON.stringify(prompt).replace(/"/g, '&quot;')},${JSON.stringify(model).replace(/"/g, '&quot;')})" title="Retry this slot">${Icon('refresh', 13)} Retry</button>
  </div>`;
}

/** Format file size as human-readable string */
function _formatFileSize(bytes) {
  if (!bytes || bytes <= 0) return '';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

/* _escapeHtmlBasic — alias for escapeHtml from core.js */
const _escapeHtmlBasic = escapeHtml;

/* ── Dynamic model dropdown population ── */

async function _loadIgModels() {
  try {
    const data = await Api.images.models();
    const models = (data && data.models) || [];
    if (models.length === 0) return;

    const dropdown = document.getElementById('igModelDropdown');
    if (!dropdown) return;

    // Brand-specific SVG icons (detect from model name)
    function _igIcon(model) {
      const brand = typeof _detectBrand === 'function' ? _detectBrand(model) : 'generic';
      return typeof _brandSvg === 'function' ? _brandSvg(brand, 14) : Icon('image', 14);
    }

    // Filter out hidden image gen models
    const visible = models.filter(m => !_hiddenIgModels.has(m.model));
    if (visible.length === 0) {
      dropdown.innerHTML = '<div class="ig-model-option" style="opacity:.5;pointer-events:none"><span class="ig-model-name">No models visible</span></div>';
      return;
    }

    /* Group by provider (transit endpoint) for section labels */
    const grouped = {};  // provider_id → { name, models: [] }
    for (const m of visible) {
      const pid = m.provider_id || 'default';
      if (!grouped[pid]) grouped[pid] = { name: m.provider_name || pid, models: [] };
      grouped[pid].models.push(m);
    }

    // Update the global model list from API data
    _IG_ALL_MODELS.length = 0;
    for (const m of visible) _IG_ALL_MODELS.push(m.model);

    // ── Always start with "All Models" option ──
    const isAllActive = _igSelectedModel === '__all__';
    let html = `<div class="ig-model-option ${isAllActive ? 'active' : ''}" data-model="__all__" onclick="selectIgModel(this)">
      <span class="ig-model-icon"><svg width="14" height="14" viewBox="0 0 24 24" fill="#f472b6"><rect x="2" y="2" width="9" height="9" rx="2"/><rect x="13" y="2" width="9" height="9" rx="2"/><rect x="2" y="13" width="9" height="9" rx="2"/><rect x="13" y="13" width="9" height="9" rx="2"/></svg></span>
      <span class="ig-model-info"><span class="ig-model-name">All Models</span></span>
      <span class="ig-model-check">${Icon('check', 14)}</span>
    </div><div class="ig-model-divider"></div>`;

    let idx = 0;
    const providerIds = Object.keys(grouped);
    for (const pid of providerIds) {
      const group = grouped[pid];
      /* Only show section headers when there are multiple providers */
      if (providerIds.length > 1) {
        html += `<div class="ig-model-section">${_escapeHtmlBasic(group.name)}</div>`;
      }
      for (const m of group.models) {
        const friendlyName = typeof _modelShortName === 'function' ? _modelShortName(m.model) : m.model;
        const isActive = !isAllActive && (m.model === _igSelectedModel || (idx === 0 && !visible.find(v => v.model === _igSelectedModel)));
        if (isActive) {
          _igSelectedModel = m.model;
          const label = document.getElementById('igModelLabel');
          if (label) label.textContent = friendlyName;
          // Set brand icon + color on the toggle (same as preset-toggle)
          const brand = typeof _detectBrand === 'function' ? _detectBrand(m.model) : 'generic';
          const iconEl = document.getElementById('igModelIcon');
          const toggle = document.querySelector('.ig-preset');
          if (iconEl && typeof _brandSvg === 'function') iconEl.innerHTML = _brandSvg(brand, 14);
          if (toggle) toggle.setAttribute('data-brand', brand);
        }
        // Update short name map
        _IG_MODEL_SHORT[m.model] = friendlyName;
        html += `<div class="ig-model-option ${isActive ? 'active' : ''}" data-model="${_escapeHtmlBasic(m.model)}" onclick="selectIgModel(this)">
          <span class="ig-model-icon">${_igIcon(m.model)}</span>
          <span class="ig-model-info"><span class="ig-model-name">${_escapeHtmlBasic(friendlyName)}</span></span>
          <span class="ig-model-check">${Icon('check', 14)}</span>
        </div>`;
        idx++;
      }
    }
    dropdown.innerHTML = html;
    /* ★ Reflow toolbar after models loaded (toolbar width may have changed) */
    if (typeof _scheduleReflow === 'function') _scheduleReflow();
  } catch (e) {
    console.warn('[ImageGen] Failed to load models:', e);
  }
}

// Load models on startup — called from _loadServerConfigAndPopulate() after
// _hiddenIgModels is populated, to avoid the race condition where models load
// before the hidden-set is ready.
// Fallback: if server config hasn't triggered it within 5s, load anyway.
var _igModelsLoaded = false;
setTimeout(function() { if (!_igModelsLoaded) _loadIgModels(); }, 5000);

// ── Image Generation — Utility functions for displaying
//    images generated via the generate_image tool ──
//
// NOTE: `_openImageFullscreen` + `_downloadGenImage` were MOVED to the CORE
// bundle (static/js/ui/image_fullscreen.js). They are called via inline
// onclick= from tool-panel / chat image thumbnails that render in the core
// bundle BEFORE Image-Gen mode (which loads this deferred file) is ever
// opened, so they must always be present. Do not re-add them here.
