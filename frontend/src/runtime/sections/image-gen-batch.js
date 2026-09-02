/* ===== migrated source: image-gen-batch.js ===== */
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
// Gacha Mode — Batch Image Generation
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
  let conv = getActiveConv();
  let sessionId = '';
  try {
    if (!conv) {
      const now = Date.now();
      conv = { id: 'conv-' + now + '-' + Math.random().toString(36).slice(2,8),
               title: 'New Chat', createdAt: now, updatedAt: now,
               _localOnly: true };
      conversations.unshift(conv);
      activeConvId = conv.id;
      sessionStorage.setItem('tofu_activeConvId', conv.id);
      captureActiveConversationSettings();
    }
    conv.imageGenMode = true;
    const hadHumanTurn = Boolean(
      runtimeScope.ConversationTurnRead?.hasActor?.(conv, 'human'),
    );
    const history = _igCollectHistory(conv);
    if (!hadHumanTurn) conv.title = prompt.slice(0, 50);
    const createdAt = Date.now();
    sessionId = `${createdAt}:${Math.random().toString(36).slice(2, 10)}`;
    await _igAppendSettledTurn(conv, 'human', {
      content: prompt,
      timestamp: createdAt,
    }, {
      kind: 'image_generation_prompt',
      createdAt,
      commandId: `image-generation:${sessionId}:input`,
    });
    if (typeof renderConversationList === 'function') renderConversationList();
    const textarea = document.getElementById('userInput');
    if (textarea) { textarea.value = ''; textarea.style.height = 'auto'; }

    const models = _igBatchModels(count);
    const results = models.map(model => ({
      ok: false,
      prompt,
      model,
      aspectRatio: _igSelectedAspect,
      resolution: _igSelectedResolution,
      error: 'pending',
    }));
    let revision = 1;
    _igShowTransient(conv, _igTransientTurn(
      conv, sessionId, 'batch', results, revision,
    ));
    if (history.length > 0) {
      _igToast(`Sending ${history.length} prior turn${history.length > 1 ? 's' : ''} for multi-turn editing`, 'info');
    }

    const startedAt = Date.now();
    _igAbortControllers = models.map(() => new AbortController());
    await Promise.all(models.map(async (model, index) => {
      const body = {
        prompt,
        model,
        aspect_ratio: _igSelectedAspect,
        resolution: _igSelectedResolution,
        ...(history.length ? { history } : {}),
      };
      try {
        const data = await Api.images.generate(body, {
          signal: _igAbortControllers[index]?.signal,
        });
        if (data.ok && (data.image_url || data.image_b64)) {
          const imageUrl = data.image_url ||
            `data:${data.mime_type || 'image/png'};base64,${data.image_b64}`;
          results[index] = _igTypedResult(data, {
            prompt, model, aspectRatio: _igSelectedAspect,
            resolution: _igSelectedResolution, imageUrl,
            elapsedSeconds: (Date.now() - startedAt) / 1000,
          });
        } else {
          const error = _igClassifyError(data, data._status);
          results[index] = _igTypedResult(data, {
            prompt, model, aspectRatio: _igSelectedAspect,
            resolution: _igSelectedResolution,
            elapsedSeconds: (Date.now() - startedAt) / 1000,
            error: error.text, errorType: error.errorType,
          });
          if (error.isRateLimit) _igToast(`⏳ Slot ${index + 1} rate limited`, 'warning');
          else if (error.isContentBlocked) _igToast(`🚫 Slot ${index + 1} content blocked`, 'error');
        }
      } catch (error) {
        const cancelled = error?.name === 'AbortError';
        results[index] = _igTypedResult({}, {
          prompt, model, aspectRatio: _igSelectedAspect,
          resolution: _igSelectedResolution,
          elapsedSeconds: (Date.now() - startedAt) / 1000,
          error: cancelled ? 'Cancelled' : (error?.message || 'Request failed'),
          errorType: cancelled ? 'cancelled' : 'network',
        });
      }
      revision += 1;
      _igShowTransient(conv, _igTransientTurn(
        conv, sessionId, 'batch', results.slice(), revision,
      ));
    }));

    const okResults = results.filter(result => result.ok);
    const generationStatus = okResults.length ? 'completed'
      : (results.every(result => result.errorType === 'cancelled')
        ? 'cancelled' : 'failed');
    const responseText = okResults.map(result => result.responseText)
      .filter(Boolean).join('\n\n');
    const terminalProjection = {
      content: responseText || (okResults.length
        ? `${okResults.length} image${okResults.length === 1 ? '' : 's'} generated.`
        : `All ${count} image generations failed`),
      timestamp: Date.now(),
      imageGeneration: {
        blockId: 'image-generation',
        mode: 'batch',
        status: generationStatus,
        results,
      },
    };
    try {
      await _igAppendSettledTurn(conv, 'assistant', terminalProjection, {
        kind: 'image_generation_result',
        commandId: `image-generation:${sessionId}:result`,
      });
      _igRemoveTransient(conv, sessionId);
    } catch (error) {
      const retained = _igTransientTurn(
        conv, sessionId, 'batch', results, revision + 1,
      );
      retained.status = 'completed';
      retained.projection = terminalProjection;
      retained.settlement = {
        outcome: 'interrupted', cause: 'persistence_failed', resumeOptions: [],
      };
      _igShowTransient(conv, retained);
      _igToast('Image results are visible but could not be saved. Keep this tab open.', 'error');
      console.error('[ImageGen] Batch turn persistence failed:', error);
    }

    debugLog(
      `Batch generation complete: ${okResults.length}/${count} succeeded`,
      okResults.length ? 'success' : 'warning',
    );
  } catch (err) {
    console.error('[ImageGen] _igGenerateBatch threw:', err);
    debugLog(`Batch generation error: ${err?.message || err}`, 'error');
  } finally {
    _igGenerating = false;
    _igAbortControllers = [];
    if (genBtn) genBtn.disabled = false;
  }
}
