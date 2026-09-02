/* ===== migrated source: ui/stream_lifecycle.js ===== */
/* ═══════════════════════════════════════════════════════════════
   ui/stream_lifecycle.js — post-Turn orchestration side effects

   Downstream helpers: request a Surface refresh, translate Human-Guidance
   responses, and refresh the context gauge after a Turn settles.

   These are downstream lifecycle callers. Turn content is always painted by
   ConversationSurface; this module retains only orchestration side effects.

   Concatenated through the runtime manifest; it never owns conversation DOM.
   ═══════════════════════════════════════════════════════════════ */

function showStreamingUIForConv(convId, opts) {
  const conv = conversations.find((item) => item.id === convId);
  if (!conv || convId !== activeConvId) return;
  runtimeScope.requestAuthoritativeConversationRender(
    convId, { force: Boolean(opts?.forceRebuild), forceScroll: false },
  );
  updateSendButton();
}
function _retriggerHgTranslations(convId) {
  const conv = conversations.find(c => c.id === convId);
  if (!conv) return;
  const _hgAutoTrans = convAutoTranslate(conv);
  if (!_hgAutoTrans) return;
  const assistantTurn = runtimeScope.ConversationTurnRead?.latest?.(
    conv,
    (turn) => turn.actor === 'assistant'
      && Array.isArray(turn.projection?.toolRounds),
  );
  for (const r of assistantTurn?.projection?.toolRounds || []) {
    const presentation = runtimeScope.HumanGuidancePresentation?.read?.(
      convId, r.guidanceId || '',
    );
    if (r.status === 'awaiting_human' && r.guidanceQuestion
        && !presentation?.translatedQuestion && !presentation?.translating) {
      console.log(`[HG-Translate] Re-triggering translation for guidance=${r.guidanceId} after reconnect`);
      _autoTranslateHumanGuidance(convId, r.roundNum, r.guidanceQuestion, r.guidanceType || 'free_text', r.guidanceOptions || []);
    }
  }
}

/**
 * Auto-translate Human Guidance question & options (EN→CN).
 * Called when a `human_guidance_request` SSE event arrives and conv.autoTranslate is ON.
 * Translates asynchronously; re-renders the HG card when translation completes.
 */
function _findHumanGuidanceRound(conv, roundNum, guidanceId) {
  const turn = runtimeScope.ConversationTurnRead?.latest?.(
    conv,
    (candidate) => candidate.actor === 'assistant'
      && Array.isArray(candidate.projection?.toolRounds)
      && candidate.projection.toolRounds.some((round) => (
        guidanceId ? round.guidanceId === guidanceId : round.roundNum === roundNum
      )),
  );
  return turn?.projection?.toolRounds?.find((round) => (
    guidanceId ? round.guidanceId === guidanceId : round.roundNum === roundNum
  )) || null;
}

async function _autoTranslateHumanGuidance(convId, roundNum, question, responseType, options) {
  const conv = conversations.find(c => c.id === convId);
  if (!conv) return;
  const round = _findHumanGuidanceRound(conv, roundNum, '');
  if (!round || round.status !== 'awaiting_human') return;
  const guidanceId = String(round.guidanceId || '');
  if (!guidanceId) return;

  // Mark as translating (shows spinner in the card)
  runtimeScope.HumanGuidancePresentation?.patch?.(
    convId, guidanceId, { translating: true },
  );

  // ── Build a single translation batch: question + all option labels + descriptions ──
  // Concatenate all texts with a separator to make a single API call (cheaper & faster)
  const SEP = '\n‖‖‖\n'; // unique separator unlikely to appear in content
  const parts = [question];
  /* Defensive: ensure `options` is an array before iterating. Some
   *   upstream callers (e.g. legacy persisted rounds) can pass null,
   *   a JSON string, or an object. */
  let _optsArr = options;
  if (typeof _optsArr === 'string') {
    try { _optsArr = JSON.parse(_optsArr); }
    catch (_e) { _optsArr = []; }
  }
  if (!Array.isArray(_optsArr)) _optsArr = [];
  if (responseType === 'choice' && _optsArr.length > 0) {
    for (const opt of _optsArr) {
      parts.push((opt && opt.label) || '');
      parts.push((opt && opt.description) || '');
    }
  }
  const batchText = parts.join(SEP);

  try {
    console.log(`[HG-Translate] Starting EN→CN translation for guidance=${round.guidanceId}, parts=${parts.length}`);
    const translated = await _callTranslateAPI(batchText, 'Chinese', 'English');
    // Split back by separator
    const translatedParts = translated.split(/\n?‖‖‖\n?/);

    // Re-find the authoritative round (it may have settled during the request).
    const conv2 = conversations.find(c => c.id === convId);
    if (!conv2) return;
    const round2 = _findHumanGuidanceRound(conv2, roundNum, guidanceId);
    if (!round2 || round2.status !== 'awaiting_human') return;

    const translatedQuestion = translatedParts[0] || question;
    const translatedOptions = [];
    if (responseType === 'choice' && Array.isArray(_optsArr)
        && translatedParts.length > 1) {
      for (let i = 0; i < _optsArr.length; i++) {
        const labelIdx = 1 + i * 2;
        const descIdx = 2 + i * 2;
        translatedOptions.push({
          ...(translatedParts[labelIdx]
            ? { label: translatedParts[labelIdx] } : {}),
          ...(translatedParts[descIdx] && _optsArr[i]?.description
            ? { description: translatedParts[descIdx] } : {}),
        });
      }
    }
    runtimeScope.HumanGuidancePresentation?.patch?.(convId, guidanceId, {
      translating: false,
      translatedQuestion,
      translatedOptions,
    });

    console.log(`[HG-Translate] ✓ Translation done for guidance=${guidanceId}, ` +
      `question: ${question.length}→${translatedQuestion.length} chars`);
  } catch (e) {
    console.warn(`[HG-Translate] Translation failed: ${e.message} — showing original`);
    runtimeScope.HumanGuidancePresentation?.patch?.(
      convId, guidanceId, { translating: false },
    );
  }
}

/* Projection repaint is the per-round refresh point for the context gauge;
 * the updater is frame-coalesced and skips unchanged writes. */
function _cvRefreshContextGauge() {
  if (typeof runtimeScope !== 'undefined'
      && typeof runtimeScope.updateContextBar === 'function') {
    runtimeScope.updateContextBar();
  }
}
