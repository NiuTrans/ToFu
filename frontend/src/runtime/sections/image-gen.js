/* ===== migrated source: image-gen.js ===== */
/* ═══════════════════════════════════════════
   image-gen.js — Image Generation — Creative Mode
   ═══════════════════════════════════════════ */
/* ═══════════════════════════════════════════
   Image Generation — Creative Mode
   ═══════════════════════════════════════════ */
let _igGenerating = false;
let _igAbortController = null;       // AbortController for single request
let _igAbortControllers = [];        // AbortControllers for batch requests

// Owner-scoped model/provider routes loaded from model-routing v2.  Static
// markup is presentation-only and must never become a routing fallback.
const _IG_ALL_MODELS = [];
const _IG_ALL_ROUTES = [];
var _IG_MODEL_SHORT = {
  'gemini-3.1-flash-image-preview': 'Gemini 3.1 Flash',
  'gemini-3-pro-image-preview': 'Gemini 3 Pro',
  'gemini-2.5-flash-image': 'Gemini 2.5 Flash',
  'gpt-image-1.5': 'GPT Image 1.5',
  'gpt-image-2': 'GPT Image 2',
};

// ═══════════════════════════════════════════════════
// Unified history collection for multi-turn editing
// ═══════════════════════════════════════════════════

/**
 * Collect multi-turn image generation history from the read-only Turn view.
 * Typed imageGeneration results are authoritative.
 *
 * @param {Object} conv — conversation object
 * @returns {Array<{prompt: string, image_url: string, text: string}>}
 */
function _igCollectHistory(conv) {
  const history = [];
  if (!conv) return history;
  for (const turn of runtimeScope.ConversationTurnRead?.ordered?.(conv) || []) {
    const typedResults = turn.projection?.imageGeneration?.results;
    if (Array.isArray(typedResults)) {
      const result = typedResults.find(item => item?.ok && item?.imageUrl);
      if (result) {
        history.push({
          prompt: result.prompt || '',
          image_url: result.remoteImageUrl || result.imageUrl || '',
          text: result.responseText || '',
        });
        continue;
      }
    }
  }
  return history;
}

function _igProjectionImages(sourceImages) {
  return (sourceImages || []).map((image, index) => ({
    attachmentId: image.attachmentId || `image-gen-source-${index}`,
    preview: image.url || image.base64 || '',
    caption: image.name || 'Image generation source',
    ...(image.sizeKB == null ? {} : { sizeKB: image.sizeKB }),
  }));
}

async function _igAppendSettledTurn(conv, actor, projection, options) {
  const service = runtimeScope.ConversationTurnStore;
  if (!service?.appendSettledConversationTurn) {
    throw new Error('Conversation TurnStore is unavailable.');
  }
  const settings = typeof _buildConvSettings === 'function'
    ? await _buildConvSettings(conv) : {};
  return service.appendSettledConversationTurn(conv, actor, projection, {
    kind: options?.kind || 'image_generation',
    status: options?.status || 'completed',
    settlement: options?.settlement || {
      outcome: options?.status || 'completed',
      cause: 'image_generation',
      resumeOptions: [],
    },
    createdAt: options?.createdAt || Date.now(),
    commandId: options?.commandId,
    settings,
  });
}

function _igTransientTurn(conv, sessionId, mode, results, revision) {
  const timestamp = Date.now();
  return {
    turnId: `transient:image-generation:${sessionId}`,
    conversationId: conv.id,
    laneId: 'main',
    parentTurnId: null,
    ordinal: Number.MAX_SAFE_INTEGER,
    actor: 'assistant',
    kind: 'image_generation',
    runId: sessionId,
    status: 'running',
    currentAttemptId: null,
    projection: {
      content: '',
      segments: [],
      timestamp,
      imageGeneration: {
        blockId: 'image-generation',
        mode,
        status: 'running',
        results,
      },
    },
    projectionRevision: revision || 1,
    settlement: {},
    createdAt: timestamp,
    updatedAt: timestamp,
  };
}

function _igShowTransient(conv, turn) {
  runtimeScope.ConversationTransientTurns?.upsert?.(conv, turn);
}

function _igRemoveTransient(conv, sessionId) {
  runtimeScope.ConversationTransientTurns?.remove?.(
    conv, `transient:image-generation:${sessionId}`,
  );
}

function _igTypedResult(data, fallback) {
  return {
    ok: Boolean(data?.ok),
    prompt: fallback.prompt || '',
    model: data?.model || fallback.model || '',
    providerId: data?.provider_id || '',
    aspectRatio: fallback.aspectRatio || '',
    resolution: fallback.resolution || '',
    imageUrl: data?.image_url || fallback.imageUrl || '',
    remoteImageUrl: data?.remote_image_url || '',
    fileSize: Math.max(0, Number(data?.file_size || 0)),
    elapsedSeconds: Math.max(0, Number(fallback.elapsedSeconds || 0)),
    responseText: data?.text || '',
    ...(fallback.error ? { error: fallback.error } : {}),
    ...(fallback.errorType ? { errorType: fallback.errorType } : {}),
  };
}

// ═══════════════════════════════════════════════════
// Error type classification & toast helpers
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
  if (ImageGenerationComposerState.imageGenMode) { exitImageGenMode(); return; }
  // Exit paper mode if active (mutually exclusive)
  if (runtimeScope.paperMode && typeof runtimeScope.exitPaperMode === 'function') {
    runtimeScope.exitPaperMode();
  }
  // Research mode is mutually exclusive too; its exit is a no-op when inactive.
  if (typeof runtimeScope.exitResearchMode === 'function') runtimeScope.exitResearchMode();
  if (typeof runtimeScope.setAgentMode === 'function'
      && (ImageGenerationComposerState.planMode || ImageGenerationComposerState.autopilotEnabled || ImageGenerationComposerState.activeFlow)) {
    if (runtimeScope.setAgentMode('standard') !== true) return false;
  }
  _applyImageGenUI(true);
  captureActiveConversationSettings();
  if (typeof updateSubmenuCounts === 'function') updateSubmenuCounts();
  if (!_igModelsLoaded) void _loadIgModels();
  debugLog('Image Gen Mode: ENTER', 'success');
  // Focus the textarea
  document.getElementById('userInput')?.focus();
  return true;
}
function exitImageGenMode() {
  _applyImageGenUI(false);
  captureActiveConversationSettings();
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
  ImageGenerationComposerState.selectedModel = el.dataset.model;
  ImageGenerationComposerState.selectedProviderId = el.dataset.provider || '';
  // Provider + model is the identity of a selectable v2 Offering.
  el.closest('.ig-preset-dropdown').querySelectorAll('.ig-model-option').forEach(o => {
    o.classList.toggle('active',
      o.dataset.model === ImageGenerationComposerState.selectedModel
      && (o.dataset.provider || '') === ImageGenerationComposerState.selectedProviderId);
  });
  // Update toggle label + brand icon (same pattern as preset toggle)
  const label = document.getElementById('igModelLabel');
  const iconEl = document.getElementById('igModelIcon');
  const toggle = document.querySelector('.ig-preset');
  if (ImageGenerationComposerState.selectedModel === '__all__') {
    if (label) label.textContent = 'All Models';
    if (iconEl) iconEl.innerHTML = '';
    if (toggle) toggle.setAttribute('data-brand', 'generic');
    // Auto-set count to 4 (one per model) when switching to All Models
    if (ImageGenerationComposerState.selectedCount < 2) {
      ImageGenerationComposerState.selectedCount = 4;
      document.querySelectorAll('#igCountBar .ig-pill').forEach(b => {
        b.classList.toggle('active', b.dataset.count === '4');
      });
      const genText = document.querySelector('.ig-gen-text');
      if (genText) genText.textContent = '4连抽!';
    }
  } else {
    const name = el.querySelector('.ig-model-name')?.textContent || ImageGenerationComposerState.selectedModel;
    if (label) label.textContent = name;
    // Update brand icon + color on the toggle
    const brand = typeof _detectBrand === 'function' ? _detectBrand(ImageGenerationComposerState.selectedModel) : 'generic';
    if (iconEl && typeof _brandSvg === 'function') iconEl.innerHTML = _brandSvg(brand, 14);
    if (toggle) toggle.setAttribute('data-brand', brand);
  }

  // Close dropdown
  document.getElementById('igModelPicker')?.classList.remove('open');
  /* Reflow toolbar after model label change */
  if (typeof _scheduleReflow === 'function') _scheduleReflow();
}
function selectIgAspect(el) {
  ImageGenerationComposerState.selectedAspect = el.dataset.ar;
  document.querySelectorAll('#igAspectBar .ig-pill').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
}
function selectIgResolution(el) {
  ImageGenerationComposerState.selectedResolution = el.dataset.res;
  document.querySelectorAll('#igResolutionBar .ig-pill').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
}
function selectIgCount(el) {
  ImageGenerationComposerState.selectedCount = parseInt(el.dataset.count, 10) || 1;
  document.querySelectorAll('#igCountBar .ig-pill').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
  // Update generate button label — gacha style
  const genText = document.querySelector('.ig-gen-text');
  if (genText) genText.textContent = ImageGenerationComposerState.selectedCount > 1
    ? `${ImageGenerationComposerState.selectedCount}连抽!` : '生成';
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

  // Wait for the owner-scoped v2 catalogue before choosing a route.  This
  // closes the click-before-load race and prevents hard-coded markup models
  // from escaping the repository authority.
  await _loadIgModels();
  if (_IG_ALL_ROUTES.length === 0) {
    _igToast('No image generation model is available for this account.', 'warning');
    return;
  }

  // ── Wait for any still-compressing/uploading source images (mobile) so an
  //    edit never fires with an entry whose base64 isn't ready yet. ──
  await _waitForImageProcessing();

  // ── Collect source images for editing ──
  const sourceImages = [...ImageGenerationComposerState.pendingImages].filter(im => im && (im.base64 || im.url));
  const isEdit = sourceImages.length > 0;

  // ── Route to batch generation when count > 1 or All Models selected ──
  // (batch mode not supported with editing — single only)
  const effectiveCount = ImageGenerationComposerState.selectedModel === '__all__'
    ? Math.max(ImageGenerationComposerState.selectedCount, _IG_ALL_MODELS.length)
    : ImageGenerationComposerState.selectedCount;
  if (effectiveCount > 1 && !isEdit) {
    return _igGenerateBatch(prompt, effectiveCount);
  }

  _igGenerating = true;
  const genBtn = document.getElementById('igGenerateBtn');
  if (genBtn) genBtn.disabled = true;
  let conv = getActiveConv();
  let sessionId = '';
  try {
    // ── Ensure conversation exists ──
    if (!conv) {
      const now = Date.now();
      conv = { id: 'conv-' + now + '-' + Math.random().toString(36).slice(2,8),
               title: 'New Chat', createdAt: now, updatedAt: now,
               _localOnly: true };
      ImageGenerationComposerState.conversations.unshift(conv);
      ImageGenerationComposerState.activeConversationId = conv.id;
      sessionStorage.setItem('tofu_activeConvId', conv.id);
      captureActiveConversationSettings();
    }
    conv.imageGenMode = true;
    const hadHumanTurn = Boolean(
      runtimeScope.ConversationTurnRead?.hasActor?.(conv, 'human'),
    );
    const igHistory = _igCollectHistory(conv);
    const historyCount = igHistory.length;
    if (!hadHumanTurn) {
      conv.title = prompt.slice(0, 60) + (prompt.length > 60 ? '...' : '');
      const title = document.getElementById('topbarTitle');
      if (title) title.textContent = conv.title;
    }
    const inputCreatedAt = Date.now();
    sessionId = `${inputCreatedAt}:${Math.random().toString(36).slice(2, 10)}`;
    await _igAppendSettledTurn(conv, 'human', {
      content: prompt,
      timestamp: inputCreatedAt,
      ...(isEdit ? { images: _igProjectionImages(sourceImages) } : {}),
    }, {
      kind: isEdit ? 'image_edit_prompt' : 'image_generation_prompt',
      createdAt: inputCreatedAt,
      commandId: `image-generation:${sessionId}:input`,
    });
    if (typeof renderConversationList === 'function') renderConversationList();

    textarea.value = '';
    textarea.style.height = 'auto';
    ImageGenerationComposerState.pendingImages = [];
    renderImagePreviews();

    const pendingResult = {
      ok: false,
      prompt,
      model: ImageGenerationComposerState.selectedModel,
      aspectRatio: ImageGenerationComposerState.selectedAspect,
      resolution: ImageGenerationComposerState.selectedResolution,
      error: 'pending',
    };
    const transient = _igTransientTurn(
      conv, sessionId, isEdit ? 'edit' : 'generate', [pendingResult], 1,
    );
    _igShowTransient(conv, transient);
    if (typeof scrollToBottom === 'function') scrollToBottom();

    // Image generation is a WAIT, not a crash. Only explicit Cancel aborts it.
    _igAbortController = new AbortController();
    const t0 = Date.now();
    const reqBody = {
      prompt,
      aspect_ratio: ImageGenerationComposerState.selectedAspect,
      resolution: ImageGenerationComposerState.selectedResolution,
      model: ImageGenerationComposerState.selectedModel,
    };
    if (ImageGenerationComposerState.selectedProviderId) {
      reqBody.provider_id = ImageGenerationComposerState.selectedProviderId;
    }
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
    let generationStatus = 'completed';
    let content = '';
    let result;
    let data;
    try {
      data = await Api.images.generate(reqBody, { signal: _igAbortController.signal });
      if (data.ok) {
        const imageUrl = data.image_url
          ? (data.image_url.startsWith('/') ? apiUrl(data.image_url) : data.image_url)
          : (data.image_b64
            ? `data:${data.mime_type || 'image/png'};base64,${data.image_b64}` : '');
        result = _igTypedResult(data, {
          prompt, model: ImageGenerationComposerState.selectedModel, aspectRatio: ImageGenerationComposerState.selectedAspect,
          resolution: ImageGenerationComposerState.selectedResolution, imageUrl,
          elapsedSeconds: (Date.now() - t0) / 1000,
        });
        content = data.text || '';
      } else {
        const error = _igClassifyError(data, data._status);
        generationStatus = 'failed';
        content = `Image generation failed: ${error.text}`;
        result = _igTypedResult(data, {
          prompt, model: ImageGenerationComposerState.selectedModel, aspectRatio: ImageGenerationComposerState.selectedAspect,
          resolution: ImageGenerationComposerState.selectedResolution,
          elapsedSeconds: (Date.now() - t0) / 1000,
          error: error.text, errorType: error.errorType,
        });
        if (error.isRateLimit) _igToast('⏳ Rate limited — all model slots exhausted', 'warning');
        else if (error.isContentBlocked) _igToast('🚫 Content policy: prompt was blocked', 'error');
      }
    } catch (error) {
      const cancelled = error?.name === 'AbortError';
      generationStatus = cancelled ? 'cancelled' : 'failed';
      const detail = cancelled ? 'Cancelled by user.'
        : (error?.message || 'Failed to connect to server');
      content = `${cancelled ? 'Image generation cancelled' : 'Image generation network error'}: ${detail}`;
      result = _igTypedResult({}, {
        prompt, model: ImageGenerationComposerState.selectedModel, aspectRatio: ImageGenerationComposerState.selectedAspect,
        resolution: ImageGenerationComposerState.selectedResolution,
        elapsedSeconds: (Date.now() - t0) / 1000,
        error: detail, errorType: cancelled ? 'cancelled' : 'network',
      });
      console.error('[ImageGen] Direct generation error:', error);
    }

    const terminalProjection = {
      content,
      timestamp: Date.now(),
      imageGeneration: {
        blockId: 'image-generation',
        mode: isEdit ? 'edit' : 'generate',
        status: generationStatus,
        results: [result],
      },
    };
    try {
      await _igAppendSettledTurn(conv, 'assistant', terminalProjection, {
        kind: 'image_generation_result',
        commandId: `image-generation:${sessionId}:result`,
      });
      _igRemoveTransient(conv, sessionId);
    } catch (error) {
      const retained = {
        ...transient,
        status: 'completed',
        projectionRevision: transient.projectionRevision + 1,
        projection: terminalProjection,
        settlement: { outcome: 'interrupted', cause: 'persistence_failed', resumeOptions: [] },
        updatedAt: Date.now(),
      };
      _igShowTransient(conv, retained);
      _igToast('Image result is visible but could not be saved. Keep this tab open.', 'error');
      console.error('[ImageGen] Turn persistence failed:', error);
    }
  } catch (error) {
    _igToast('Image generation could not start because the conversation was not saved.', 'error');
    console.error('[ImageGen] Turn start failed:', error);
  } finally {
    _igGenerating = false;
    _igAbortController = null;
    if (genBtn) genBtn.disabled = false;
    if (conv?.id === ImageGenerationComposerState.activeConversationId && typeof scrollToBottom === 'function') scrollToBottom();
  }
}

/** Update the generate button text based on whether images are pending (edit mode) */
function _igUpdateGenButton() {
  const genText = document.querySelector('.ig-gen-text');
  if (!genText) return;
  const isEdit = ImageGenerationComposerState.pendingImages.length > 0;
  // Only update if not in batch/all-models mode
  if (ImageGenerationComposerState.selectedCount <= 1 && ImageGenerationComposerState.selectedModel !== '__all__') {
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

/**
 * Retry a single failed result by stable turn identity. A transient overlay
 * owns the running presentation; the terminal replacement is a CAS update of
 * the authoritative turn projection.
 */
async function _igRetryGenerationTurn(conv, turnId, slotIdx) {
  const service = runtimeScope.ConversationTurnStore;
  const store = service?.ensureRuntimeStore?.(conv?.id);
  const turn = store?.getState?.().turnsById?.[turnId];
  const generation = turn?.projection?.imageGeneration;
  const previous = generation?.results?.[slotIdx];
  if (!turn || !generation || !previous) return;
  const results = generation.results.map(result => ({ ...result }));
  results[slotIdx] = { ...previous, ok: false, error: 'pending' };
  const overlay = {
    ...turn,
    status: 'running',
    currentAttemptId: null,
    projectionRevision: Number(turn.projectionRevision || 0) + 1,
    projection: {
      ...turn.projection,
      imageGeneration: { ...generation, status: 'running', results },
    },
    settlement: {},
    updatedAt: Date.now(),
  };
  _igShowTransient(conv, overlay);
  _igGenerating = true;
  _igAbortController = new AbortController();
  const t0 = Date.now();
  try {
    const igHistory = _igCollectHistory(conv);
    const body = {
      prompt: previous.prompt,
      model: previous.model || ImageGenerationComposerState.selectedModel,
      aspect_ratio: previous.aspectRatio || ImageGenerationComposerState.selectedAspect,
      resolution: previous.resolution || ImageGenerationComposerState.selectedResolution,
    };
    const retryProviderId = previous.providerId
      || ImageGenerationComposerState.selectedProviderId;
    if (retryProviderId) body.provider_id = retryProviderId;
    if (igHistory.length > 0) body.history = igHistory;
    const data = await Api.images.generate(body, {
      signal: _igAbortController.signal,
    });
    if (data.ok && (data.image_url || data.image_b64)) {
      const imageUrl = data.image_url ||
        `data:${data.mime_type || 'image/png'};base64,${data.image_b64}`;
      results[slotIdx] = _igTypedResult(data, {
        prompt: previous.prompt,
        model: previous.model,
        aspectRatio: previous.aspectRatio,
        resolution: previous.resolution,
        imageUrl,
        elapsedSeconds: (Date.now() - t0) / 1000,
      });
      _igToast(`Slot ${slotIdx + 1} retry succeeded`, 'success');
    } else {
      const errInfo = _igClassifyError(data, data._status);
      results[slotIdx] = _igTypedResult(data, {
        prompt: previous.prompt,
        model: previous.model,
        aspectRatio: previous.aspectRatio,
        resolution: previous.resolution,
        elapsedSeconds: (Date.now() - t0) / 1000,
        error: errInfo.text,
        errorType: errInfo.errorType,
      });
    }
  } catch (error) {
    const cancelled = error?.name === 'AbortError';
    results[slotIdx] = _igTypedResult({}, {
      prompt: previous.prompt,
      model: previous.model,
      aspectRatio: previous.aspectRatio,
      resolution: previous.resolution,
      elapsedSeconds: (Date.now() - t0) / 1000,
      error: cancelled ? 'Cancelled' : (error?.message || 'Network error'),
      errorType: cancelled ? 'cancelled' : 'network',
    });
    console.error('[ImageGen] Retry slot error:', error);
  }
  const successes = results.filter(result => result.ok);
  const nextProjection = {
    ...turn.projection,
    content: successes.map(result => result.responseText).filter(Boolean).join('\n\n')
      || (successes.length
        ? `${successes.length} image${successes.length === 1 ? '' : 's'} generated.`
        : 'All image generations failed'),
    imageGeneration: {
      ...generation,
      status: successes.length ? 'completed' : 'failed',
      results,
    },
  };
  try {
    await service.updateConversationTurn(conv, turnId, nextProjection);
    runtimeScope.ConversationTransientTurns?.remove?.(conv, turnId);
  } catch (error) {
    _igShowTransient(conv, {
      ...overlay,
      status: 'completed',
      projectionRevision: overlay.projectionRevision + 1,
      projection: nextProjection,
      settlement: {
        outcome: 'interrupted', cause: 'persistence_failed', resumeOptions: [],
      },
      updatedAt: Date.now(),
    });
    _igToast('Retried image is visible but could not be saved.', 'error');
    console.error('[ImageGen] Retry persistence failed:', error);
  } finally {
    _igGenerating = false;
    _igAbortController = null;
  }
}

/* _escapeHtmlBasic — alias for escapeHtml from core.js */
const _escapeHtmlBasic = escapeHtml;

/* ── Dynamic model dropdown population ── */

let _igModelsLoaded = false;
let _igModelsRequest = null;

async function _requestIgModels() {
  try {
    const data = await Api.images.models();
    const models = (data && data.models) || [];

    const dropdown = document.getElementById('igModelDropdown');
    if (!dropdown) return;

    _IG_ALL_MODELS.length = 0;
    _IG_ALL_ROUTES.length = 0;
    if (models.length === 0) {
      dropdown.innerHTML = '<div class="ig-model-option" style="opacity:.5;pointer-events:none"><span class="ig-model-name">No image models available</span></div>';
      _igModelsLoaded = true;
      return;
    }

    // Brand-specific SVG icons (detect from model name)
    function _igIcon(model) {
      const brand = typeof _detectBrand === 'function' ? _detectBrand(model) : 'generic';
      return typeof _brandSvg === 'function' ? _brandSvg(brand, 14) : Icon('image', 14);
    }

    // Filter out hidden image gen models
    const visible = models.filter(m => !ImageGenerationComposerState.hiddenModels.has(m.model));
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

    // Update the owner-scoped route list from API data.
    for (const m of visible) {
      _IG_ALL_MODELS.push(m.model);
      _IG_ALL_ROUTES.push({ model: m.model, providerId: m.provider_id || '' });
    }

    // ── Always start with "All Models" option ──
    const isAllActive = ImageGenerationComposerState.selectedModel === '__all__';
    let html = `<div class="ig-model-option ${isAllActive ? 'active' : ''}" data-model="__all__" data-tofu-action="selectIgModel(this)">
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
        const exactSelection = m.model === ImageGenerationComposerState.selectedModel
          && (!ImageGenerationComposerState.selectedProviderId
            || m.provider_id === ImageGenerationComposerState.selectedProviderId);
        const hasSelectedRoute = visible.some(v =>
          v.model === ImageGenerationComposerState.selectedModel
          && (!ImageGenerationComposerState.selectedProviderId
            || v.provider_id === ImageGenerationComposerState.selectedProviderId));
        const isActive = !isAllActive && (exactSelection || (idx === 0 && !hasSelectedRoute));
        if (isActive) {
          ImageGenerationComposerState.selectedModel = m.model;
          ImageGenerationComposerState.selectedProviderId = m.provider_id || '';
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
        html += `<div class="ig-model-option ${isActive ? 'active' : ''}" data-model="${_escapeHtmlBasic(m.model)}" data-provider="${_escapeHtmlBasic(m.provider_id || '')}" data-tofu-action="selectIgModel(this)">
          <span class="ig-model-icon">${_igIcon(m.model)}</span>
          <span class="ig-model-info"><span class="ig-model-name">${_escapeHtmlBasic(friendlyName)}</span></span>
          <span class="ig-model-check">${Icon('check', 14)}</span>
        </div>`;
        idx++;
      }
    }
    dropdown.innerHTML = html;
    _igModelsLoaded = true;
    /* Reflow toolbar after models loaded (toolbar width may have changed) */
    if (typeof _scheduleReflow === 'function') _scheduleReflow();
  } catch (e) {
    console.warn('[ImageGen] Failed to load models:', e);
  }
}

function _loadIgModels() {
  if (_igModelsRequest) return _igModelsRequest;
  _igModelsRequest = _requestIgModels().finally(function () {
    _igModelsRequest = null;
  });
  return _igModelsRequest;
}

// No ambient startup request: enterImageGenMode owns first demand. If the
// server-config response arrives after this chunk, it calls the exported live
// port once more so hidden-model changes are reconciled without a race.

// ── Image Generation — Utility functions for displaying
//    images generated via the generate_image tool ──
//
// NOTE: `_openImageFullscreen` + `_downloadGenImage` were MOVED to the CORE
// bundle (frontend/src/runtime/ui/image_fullscreen.js). They are called via inline
// onclick= from tool-panel / chat image thumbnails that render in the core
// bundle BEFORE Image-Gen mode (which loads this deferred file) is ever
// opened, so they must always be present. Do not re-add them here.
