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
];
var _IG_MODEL_SHORT = {
  'gemini-3.1-flash-image-preview': 'Gemini 3.1 Flash',
  'gemini-3-pro-image-preview': 'Gemini 3 Pro',
  'gemini-2.5-flash-image': 'Gemini 2.5 Flash',
  'gpt-image-1.5': 'GPT Image 1.5',
};

function enterImageGenMode() {
  if (imageGenMode) { exitImageGenMode(); return; }
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
  /* ★ Reflow: model label width may have changed → recalculate toolbar width */
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

  // ── Collect source images for editing ──
  const sourceImages = [...pendingImages];
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
    sessionStorage.setItem('chatui_activeConvId', conv.id);
    _saveConvToolState();
    if (typeof renderConversationList === 'function') renderConversationList();
  }

  // ── Add user prompt as a message (with source images if editing) ──
  const userMsg = { role: 'user', content: prompt, timestamp: Date.now(), _isImageGen: true };
  if (isEdit) {
    userMsg.images = sourceImages;
    userMsg._isImageEdit = true;
  }
  conv.messages.push(userMsg);

  // ── Set title from prompt on first user message ──
  if (conv.messages.filter(m => m.role === 'user').length === 1) {
    const titleText = isEdit ? prompt : prompt;
    conv.title = titleText.slice(0, 60) + (titleText.length > 60 ? '...' : '');
    if (activeConvId === conv.id)
      document.getElementById('topbarTitle').textContent = conv.title;
    renderConversationList();
  }

  renderChat(conv, true);

  // ── Clear input and pending images ──
  textarea.value = '';
  textarea.style.height = 'auto';
  pendingImages = [];
  renderImagePreviews();

  // ── Show loading card ──
  const chatDiv = document.getElementById('chatInner');
  const loadingId = 'ig-loading-' + Date.now();
  const resLabel = _igSelectedResolution !== '1K' ? ` · ${_igSelectedResolution}` : '';
  const actionLabel = isEdit ? 'Editing image…' : 'Generating image…';
  const loadingHtml = `<div class="ig-generating" id="${loadingId}">
    <div class="ig-gen-spinner"></div>
    <div class="ig-gen-title">${actionLabel}</div>
    <div class="ig-gen-subtitle">${_escapeHtmlBasic(prompt.slice(0, 100))}${prompt.length > 100 ? '…' : ''}</div>
    <div class="ig-gen-timer" id="${loadingId}-timer">0s${resLabel}</div>
    <button class="ig-gen-cancel" onclick="_igCancelGeneration()" title="Cancel">✕ Cancel</button>
  </div>`;
  chatDiv.insertAdjacentHTML('beforeend', loadingHtml);
  chatDiv.scrollTop = chatDiv.scrollHeight;

  // ── Save early so page refresh doesn't lose the user message ──
  saveConversations(conv.id);

  // ── Timer ──
  const t0 = Date.now();
  const timerInterval = setInterval(() => {
    const el = document.getElementById(loadingId + '-timer');
    if (el) el.textContent = ((Date.now() - t0) / 1000).toFixed(0) + 's' + resLabel;
  }, 1000);

  // ── AbortController with 150s timeout ──
  _igAbortController = new AbortController();
  const abortTimer = setTimeout(() => _igAbortController?.abort(), 150_000);

  // ── Collect multi-turn history from prior image gen rounds ──
  const igHistory = [];
  for (const m of conv.messages) {
    if (m._igResult && m._igResult.image_url) {
      igHistory.push({
        prompt: m._igResult.prompt || '',
        image_url: m._igResult.remote_image_url || m._igResult.image_url || '',
        text: m._igResult.response_text || '',
      });
    }
  }

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

    const resp = await fetch(apiUrl('/api/images/generate'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: _igAbortController.signal,
      body: JSON.stringify(reqBody),
    });
    clearTimeout(abortTimer);
    const data = await resp.json();
    clearInterval(timerInterval);
    const loadingEl = document.getElementById(loadingId);

    if (data.ok) {
      const imgSrc = data.image_url
        ? apiUrl(data.image_url)
        : (data.image_b64 ? `data:${data.mime_type || 'image/png'};base64,${data.image_b64}` : '');

      const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
      const sizeStr = data.file_size ? _formatFileSize(data.file_size) : '';

      // ── Build result card ──
      const resultHtml = `<div class="ig-result-wrapper">
        <div class="ig-result-card">
          <img src="${imgSrc}" alt="${_escapeHtmlBasic(prompt.slice(0,100))}" loading="lazy"
               onclick="_openImageFullscreen(this.src)" />
          <div class="ig-result-footer">
            <span class="ig-result-prompt" title="${_escapeHtmlBasic(prompt)}">${_escapeHtmlBasic(prompt.slice(0,80))}${prompt.length > 80 ? '…' : ''}</span>
            <div class="ig-result-meta">
              <span class="ig-meta-pill">${_escapeHtmlBasic(_igSelectedAspect)}</span>
              ${sizeStr ? `<span class="ig-meta-pill">${sizeStr}</span>` : ''}
              <span class="ig-meta-pill">${elapsed}s</span>
            </div>
            <div class="ig-result-actions">
              <button onclick="event.stopPropagation();_downloadGenImage(this)" title="Download">⬇</button>
              <button onclick="event.stopPropagation();_openImageFullscreen(this.closest('.ig-result-card').querySelector('img').src)" title="Fullscreen">⛶</button>
            </div>
          </div>
        </div>
      </div>`;

      if (loadingEl) loadingEl.outerHTML = resultHtml;

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
                     response_text: data.text || '' },
      };
      conv.messages.push(assistantMsg);
      // ── Re-render chat from messages so the assistant bubble is properly shown ──
      // Without this, the image card is a raw div outside any message wrapper,
      // and visibilitychange→loadConversationsFromServer→renderChat can wipe it.
      renderChat(conv, true);
      saveConversations(conv.id);
      syncConversationToServer(conv);

    } else {
      // ── API returned an error — show card + save error as assistant msg ──
      const errText = data.error || 'Unknown error';
      const errHtml = `<div class="ig-result-wrapper">
        <div class="ig-error-card">
          <div class="ig-error-title">Image generation failed</div>
          <div class="ig-error-text">${_escapeHtmlBasic(errText)}</div>
          ${data.text ? `<div class="ig-error-detail">${_escapeHtmlBasic(data.text)}</div>` : ''}
          <button class="ig-retry-btn" onclick="_igRetryLastPrompt()">Retry</button>
        </div>
      </div>`;
      if (loadingEl) loadingEl.outerHTML = errHtml;

      const errMsg = { role: 'assistant', content: `Image generation failed: ${errText}`,
                       timestamp: Date.now(), _isImageGen: true };
      conv.messages.push(errMsg);
      renderChat(conv, true);
      saveConversations(conv.id);
      syncConversationToServer(conv);
    }

  } catch (err) {
    clearTimeout(abortTimer);
    clearInterval(timerInterval);
    const loadingEl = document.getElementById(loadingId);
    const isAbort = err.name === 'AbortError';
    const errText = isAbort ? 'Request timed out (150s). The server may still be generating — please try again.'
                            : (err.message || 'Failed to connect to server');
    const errHtml = `<div class="ig-result-wrapper">
      <div class="ig-error-card">
        <div class="ig-error-title">${isAbort ? 'Generation timed out' : 'Network error'}</div>
        <div class="ig-error-text">${_escapeHtmlBasic(errText)}</div>
        <button class="ig-retry-btn" onclick="_igRetryLastPrompt()">Retry</button>
      </div>
    </div>`;
    if (loadingEl) loadingEl.outerHTML = errHtml;
    console.error('[ImageGen] Direct generation error:', err);

    // ★ CRITICAL: Always push an assistant error message to prevent orphaned user messages
    const errMsg = { role: 'assistant', content: `${isAbort ? 'Image generation timed out' : 'Image generation network error'}: ${errText}`,
                     timestamp: Date.now(), _isImageGen: true };
    conv.messages.push(errMsg);
    renderChat(conv, true);
    saveConversations(conv.id);
    syncConversationToServer(conv);
  } finally {
    _igGenerating = false;
    _igAbortController = null;
    if (genBtn) genBtn.disabled = false;
    if (chatDiv) chatDiv.scrollTop = chatDiv.scrollHeight;
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
 */
async function _igGenerateBatch(prompt, count) {
  _igGenerating = true;
  const genBtn = document.getElementById('igGenerateBtn');
  if (genBtn) genBtn.disabled = true;

  // ── Ensure conversation exists ──
  let conv = getActiveConv();
  if (!conv) {
    newChat();
    conv = getActiveConv();
    if (!conv) { _igGenerating = false; if (genBtn) genBtn.disabled = false; return; }
  }
  conv.imageGenMode = true;

  // ── Add user prompt as a message ──
  const userMsg = { role: 'user', content: prompt, timestamp: Date.now(), _isImageGen: true };
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

  // ── Collect multi-turn history (same as single mode) ──
  const igHistory = [];
  for (const m of conv.messages) {
    if (m._igResult && m._igResult.image_url) {
      igHistory.push({
        prompt: m._igResult.prompt || '',
        image_url: m._igResult.remote_image_url || m._igResult.image_url || '',
        text: m._igResult.response_text || '',
      });
    }
    // Also collect from batch results
    if (m._igResults) {
      for (const r of m._igResults) {
        if (r.ok && r.image_url) {
          igHistory.push({ prompt: r.prompt || '', image_url: r.remote_image_url || r.image_url || '', text: r.response_text || '' });
        }
      }
    }
  }

  // ── Render user message + loading grid ──
  renderChat(conv);

  const isAllModels = _igSelectedModel === '__all__';
  const bannerText = isAllModels ? `全模型 ${count}连抽!` : `${count}连抽!`;
  const gridHtml = `<div class="ig-batch-wrapper" id="${batchId}">
    <div class="ig-batch-banner">${bannerText}</div>
    <div class="ig-batch-grid ig-cols-${Math.min(count, 2)}">
      ${models.map((m, i) => `<div class="ig-batch-slot" id="${batchId}-slot-${i}">
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
    chatDiv.insertAdjacentHTML('beforeend', gridHtml);
    chatDiv.scrollTop = chatDiv.scrollHeight;
  }

  // ── Start per-slot timers ──
  const slotTimers = models.map((_, i) => {
    const timerId = `${batchId}-timer-${i}`;
    return setInterval(() => {
      const el = document.getElementById(timerId);
      if (el) el.textContent = ((Date.now() - t0) / 1000).toFixed(1) + 's';
    }, 100);
  });

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

    return fetch(apiUrl('/api/images/generate'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: _igAbortControllers[i]?.signal,
    }).then(async resp => {
      const data = await resp.json();
      clearInterval(slotTimers[i]);
      const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
      const slotEl = document.getElementById(`${batchId}-slot-${i}`);
      if (!slotEl) return { ...data, _slotIndex: i, _elapsed: elapsed, _model: model };

      if (data.ok && (data.image_url || data.image_b64)) {
        const imgSrc = data.image_url
          ? (data.image_url.startsWith('/') ? apiUrl(data.image_url) : data.image_url)
          : `data:${data.mime_type || 'image/png'};base64,${data.image_b64}`;
        const sizeStr = data.file_size ? _formatFileSize(data.file_size) : '';
        slotEl.innerHTML = `<div class="ig-result-card ig-batch-reveal">
          <img src="${imgSrc}" alt="${_escapeHtmlBasic(prompt.slice(0, 60))}" loading="lazy"
               onclick="_openImageFullscreen(this.src)" />
          <div class="ig-result-footer">
            <span class="ig-result-prompt" title="${_escapeHtmlBasic(prompt)}">${_escapeHtmlBasic(_IG_MODEL_SHORT[model] || model)}</span>
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
      } else {
        const errText = data.error || 'Generation failed';
        slotEl.innerHTML = `<div class="ig-batch-error">
          <div class="ig-error-title">${_escapeHtmlBasic(_IG_MODEL_SHORT[model] || model)}</div>
          <div class="ig-error-text">${_escapeHtmlBasic(errText.slice(0, 300))}</div>
        </div>`;
      }
      if (chatDiv) chatDiv.scrollTop = chatDiv.scrollHeight;
      return { ...data, _slotIndex: i, _elapsed: elapsed, _model: model };
    });
  }));

  // ── Clear all timers ──
  slotTimers.forEach(t => clearInterval(t));

  // ── Remove cancel button ──
  const footerEl = document.querySelector(`#${batchId} .ig-batch-footer`);
  if (footerEl) footerEl.remove();

  // ── Build result array and save to conversation ──
  const results = [];
  let anyOk = false;
  for (const s of settled) {
    if (s.status === 'fulfilled' && s.value) {
      const d = s.value;
      const ok = d.ok && (d.image_url || d.image_b64);
      if (ok) anyOk = true;
      results.push({
        ok: !!ok,
        prompt,
        model: d.model || d._model || '',
        provider_id: d.provider_id || '',
        aspect_ratio: _igSelectedAspect,
        resolution: _igSelectedResolution,
        image_url: d.image_url || '',
        remote_image_url: d.remote_image_url || '',
        file_size: d.file_size || 0,
        elapsed: d._elapsed || '',
        response_text: d.text || '',
        error: ok ? '' : (d.error || ''),
      });
    } else {
      const reason = s.reason?.message || 'Request failed';
      results.push({
        ok: false, prompt, model: '', aspect_ratio: _igSelectedAspect,
        resolution: _igSelectedResolution, image_url: '', remote_image_url: '',
        file_size: 0, elapsed: '', response_text: '', error: reason,
      });
    }
  }

  // ── Save assistant message with batch results ──
  const contentParts = results.filter(r => r.ok).map(r =>
    `![Generated Image](${r.image_url || 'data:image'})`
  );
  const assistantContent = contentParts.length > 0
    ? contentParts.join('\n\n')
    : `All ${count} image generations failed`;

  const assistantMsg = {
    role: 'assistant',
    content: assistantContent,
    timestamp: Date.now(),
    _igResults: results,
    _isImageGen: true,
  };
  conv.messages.push(assistantMsg);
  // ── Re-render chat from messages so the batch results survive DOM wipes ──
  renderChat(conv, true);
  saveConversations(conv.id);
  syncConversationToServer(conv);

  // ── Cleanup ──
  _igGenerating = false;
  _igAbortControllers = [];
  if (genBtn) genBtn.disabled = false;
  if (chatDiv) chatDiv.scrollTop = chatDiv.scrollHeight;

  debugLog(`Batch generation complete: ${results.filter(r => r.ok).length}/${count} succeeded`, anyOk ? 'success' : 'warning');
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
    const resp = await fetch(apiUrl('/api/images/models'));
    const data = await resp.json();
    const models = data.models || [];
    if (models.length === 0) return;

    const dropdown = document.getElementById('igModelDropdown');
    if (!dropdown) return;

    // Brand-specific SVG icons (detect from model name)
    function _igIcon(model) {
      const brand = typeof _detectBrand === 'function' ? _detectBrand(model) : 'generic';
      return typeof _brandSvg === 'function' ? _brandSvg(brand, 14) : '✦';
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
      <span class="ig-model-check">✓</span>
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
          <span class="ig-model-check">✓</span>
        </div>`;
        idx++;
      }
    }
    dropdown.innerHTML = html;
    /* ★ BUG FIX: Recalculate toolbar width after model label may have changed.
     * Without this, --toolbar-w stays at the stale value from the initial
     * measurement (before models loaded), causing the ig-toolbar to overflow
     * .input-inner on narrow viewports. */
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

/**
 * Open an image in fullscreen overlay.
 * Called from tool result image click handlers.
 */
function _openImageFullscreen(src) {
  // Remove existing
  document.querySelectorAll(".imagegen-fullscreen").forEach((el) => el.remove());
  const overlay = document.createElement("div");
  overlay.className = "imagegen-fullscreen";
  overlay.onclick = () => overlay.remove();
  overlay.innerHTML = `<img src="${src}" />`;
  document.body.appendChild(overlay);
  const handler = (e) => {
    if (e.key === "Escape") {
      overlay.remove();
      document.removeEventListener("keydown", handler);
    }
  };
  document.addEventListener("keydown", handler);
}

/**
 * Download a generated image from a tool result card.
 */
function _downloadGenImage(btn) {
  const card = btn.closest(".imagegen-card") || btn.closest(".ig-result-card");
  if (!card) return;
  const img = card.querySelector("img");
  if (!img) return;
  const a = document.createElement("a");
  a.href = img.src;
  a.download = `generated_${Date.now()}.png`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}
