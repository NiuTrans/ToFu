/* ===== migrated source: main/conversation_turn_store.js ===== */
let _roleAvatarIcons = null;
function _resolveRoleAvatarIcons() {
  if (!_roleAvatarIcons && typeof createRoleAvatarIcons === 'function') {
    _roleAvatarIcons = createRoleAvatarIcons(
      typeof BASE_PATH === 'string' ? BASE_PATH : '',
    );
  }
  return _roleAvatarIcons;
}

function _throttledTurnCachePut(conv) {
  try {
    if (typeof ConvCache === 'undefined' || !conv) return;
    const now = Date.now();
    if (now - (conv._lastTurnCachePutAt || 0) < 1000) return;
    conv._lastTurnCachePutAt = now;
    ConvCache.put(conv);
  } catch (_e) { /* local cache is best-effort */ }
}

function _nativeTurnState(conv, turnId) {
  const store = runtimeScope.ConversationTurnStore?.ensureRuntimeStore?.(conv?.id);
  const state = store?.getState?.();
  const turn = state?.turnsById?.[turnId];
  return turn ? { store, state, turn } : null;
}

function _nextGeneratedNativeTurn(state, inputTurn) {
  const laneId = inputTurn?.laneId || 'main';
  const ordered = state?.laneOrder?.[laneId] || [];
  const position = ordered.indexOf(inputTurn?.turnId);
  if (position < 0) return null;
  for (const candidateId of ordered.slice(position + 1)) {
    const candidate = state.turnsById[candidateId];
    if (candidate && ['assistant', 'planner'].includes(candidate.actor)) return candidate;
    if (candidate?.actor === 'human') break;
  }
  return null;
}

function _nativeRegenerateInputOptions(resolved, inputEdit) {
  const inputTurn = resolved.state.turnsById?.[resolved.turn.parentTurnId];
  if (!inputTurn) return {};
  let inputProjection = inputTurn.projection || {};
  let shouldUpdateInput = false;
  if (inputEdit) {
    if (inputEdit.images?.length) {
      inputProjection = {
        ...inputProjection,
        images: [...(inputProjection.images || []), ...inputEdit.images],
      };
    }
    inputProjection = _nativeTurnEditProjection(
      inputProjection, String(inputEdit.content ?? ''),
    );
    shouldUpdateInput = true;
  }
  if (typeof runtimeScope.buildTurnCtxSnapshot === 'function'
      && typeof rebindTurnInputContext === 'function') {
    inputProjection = rebindTurnInputContext(
      inputProjection, runtimeScope.buildTurnCtxSnapshot(),
    );
    shouldUpdateInput = true;
  }
  return shouldUpdateInput ? {
    inputUpdate: inputProjection,
    expectedInputProjectionRevision: inputTurn.projectionRevision,
  } : {};
}

async function _operateNativeTurn(conv, turnId, operation, inputEdit) {
  const resolved = _nativeTurnState(conv, turnId);
  if (!resolved || typeof runtimeScope.ConversationTurnStore === 'undefined') return false;
  if (Object.values(resolved.state.turnsById || {}).some(turn =>
    turn && (turn.status === 'pending' || turn.status === 'running'))) {
    if (typeof showToast === 'function') {
      showToast('Stop the current attempt before starting another operation.', 'info');
    }
    return false;
  }
  try {
    const operationOptions = {
      commandId: `${operation}:${turnId}:${resolved.turn.projectionRevision || 0}`,
      ...(operation === 'regenerate'
        ? _nativeRegenerateInputOptions(resolved, inputEdit) : {}),
    };
    const cfg = await _buildConvConfig(conv);
    await runtimeScope.ConversationTurnStore.operateConversation(
      conv, turnId, operation, cfg, operationOptions,
    );
    return true;
  } catch (error) {
    try { await runtimeScope.ConversationTurnStore.hydrateConversation(conv); }
    catch (_ignored) { /* authoritative recovery is best effort */ }
    if (typeof showToast === 'function') {
      showToast(error?.body?.message || error?.message || `${operation} failed`, 'error');
    }
    return false;
  }
}

/* Late ask_human answer: the settled turn's settlement still offers
 * answer_guidance, so the answer completes the interrupted question round
 * inside a fresh attempt instead of reviving the dead process-local request. */
async function _answerHumanGuidanceLate(conv, turnId, responseText) {
  const resolved = _nativeTurnState(conv, turnId);
  if (!resolved || typeof runtimeScope.ConversationTurnStore === 'undefined') {
    throw new Error('Unknown turn; refresh the authoritative snapshot.');
  }
  if (Object.values(resolved.state.turnsById || {}).some(turn =>
    turn && (turn.status === 'pending' || turn.status === 'running'))) {
    throw new Error('Stop the current attempt before answering.');
  }
  const cfg = await _buildConvConfig(conv);
  await runtimeScope.ConversationTurnStore.operateConversation(
    conv, turnId, 'answer_guidance', cfg, {
      commandId: `answer_guidance:${turnId}:${resolved.turn.projectionRevision || 0}`,
      humanResponse: responseText,
    },
  );
}
runtimeScope.answerHumanGuidanceLate = _answerHumanGuidanceLate;

async function _regenerateNativeInput(conv, inputTurnId) {
  const resolved = _nativeTurnState(conv, inputTurnId);
  const target = resolved && _nextGeneratedNativeTurn(resolved.state, resolved.turn);
  if (!target) {
    if (typeof showToast === 'function') {
      showToast('No generated turn follows this input.', 'error');
    }
    return;
  }
  return _operateNativeTurn(conv, target.turnId, 'regenerate');
}

function _nativeTurnEditProjection(projection, content) {
  const nextProjection = { ...projection, content };
  delete nextProjection.originalContent;
  delete nextProjection.translatedContent;
  delete nextProjection.translation;
  return nextProjection;
}

async function _commitNativeTurnEdit(conv, turnId, projection, content) {
  try {
    await runtimeScope.ConversationTurnStore.updateConversationTurn(
      conv, turnId, _nativeTurnEditProjection(projection, content),
    );
    return true;
  } catch (error) {
    try { await runtimeScope.ConversationTurnStore.hydrateConversation(conv); }
    catch (_ignored) { /* authoritative recovery is best effort */ }
    if (typeof showToast === 'function') {
      showToast(error?.body?.message || error?.message || 'Edit failed', 'error');
    }
    return false;
  }
}

function _findRenderedNativeTurnNode(turnId) {
  const root = document.getElementById('chatInner');
  if (!root) return null;
  const safeId = typeof CSS !== 'undefined' && CSS.escape
    ? CSS.escape(turnId) : String(turnId).replace(/[^\w-]/g, '');
  return root.querySelector(`[data-turn-id="${safeId}"]`);
}

/* Transient copy chrome: swap the copy glyph for a check on clipboard
 * success.  Kept out of the view model on purpose — the surface only
 * re-renders the action bar when the action list changes, so a DOM-level
 * flash survives unrelated commits and harmlessly vanishes on a real one. */
function _flashTurnCopyFeedback(turnId) {
  const turnNode = _findRenderedNativeTurnNode(turnId);
  const button = turnNode?.querySelector?.(
    'button[data-conversation-action="copy"]',
  );
  const icon = button?.querySelector?.('svg.msg-action-icon');
  if (!button || !icon) return;
  if (!button._tofuCopyFlash) {
    const checkIcon = icon.cloneNode(false);
    const checkPath = icon.ownerDocument.createElementNS(
      'http://www.w3.org/2000/svg', 'path',
    );
    checkPath.setAttribute('d', 'M20 6 9 17l-5-5');
    checkPath.setAttribute('stroke', 'currentColor');
    checkPath.setAttribute('stroke-width', '2');
    checkPath.setAttribute('stroke-linecap', 'round');
    checkPath.setAttribute('stroke-linejoin', 'round');
    checkIcon.appendChild(checkPath);
    button._tofuCopyFlash = { originalIcon: icon, checkIcon, timer: 0 };
    icon.replaceWith(checkIcon);
    button.classList.add('msg-action-btn--copied');
  }
  clearTimeout(button._tofuCopyFlash.timer);
  button._tofuCopyFlash.timer = setTimeout(() => {
    const flash = button._tofuCopyFlash;
    button._tofuCopyFlash = null;
    if (flash?.checkIcon.parentNode === button) {
      flash.checkIcon.replaceWith(flash.originalIcon);
    }
    button.classList.remove('msg-action-btn--copied');
  }, 1500);
}

async function _editNativeTurn(conv, turnId) {
  const resolved = _nativeTurnState(conv, turnId);
  if (!resolved) return;
  const projection = resolved.turn.projection || {};
  const currentText = resolved.turn.actor === 'human'
    ? (projection.originalContent || projection.content || '')
    : (projection.content || '');
  const hasAttachments = Boolean(
    projection.images?.length || projection.videos?.length
      || projection.pdfTexts?.length || projection.attachments?.length,
  );
  if (typeof openTurnInlineEditor === 'function') {
    const session = openTurnInlineEditor({
      conversationId: conv.id,
      turnId,
      text: currentText,
      allowEmpty: hasAttachments,
      canResend: resolved.turn.actor === 'human'
        && Boolean(_nextGeneratedNativeTurn(resolved.state, resolved.turn)),
      findTurnNode: _findRenderedNativeTurnNode,
      translate: (key) => (typeof t === 'function' ? t(key) : key),
      /* Paste-to-attach inside the edit session: reuse the composer's shared
       * compress + upload core so the entry shape matches a send-draft image
       * exactly ({base64, mediaType, preview, sizeKB, url?}). The composer
       * pendingImages draft is NOT touched — this attaches to the turn. */
      onImageAttach: typeof processImageFile === 'function'
        ? async (file) => {
          try {
            const d = await processImageFile(file);
            return { payload: d, preview: d.preview || d.url || '' };
          } catch (_error) {
            if (typeof showToast === 'function') {
              showToast(
                typeof t === 'function' ? t('upload.imageSkipped') : 'Image skipped',
                'warning',
              );
            }
            return null;
          }
        }
        : undefined,
      async onSubmit({ text, resend, images }) {
        if (resend) {
          const fresh = _nativeTurnState(conv, turnId);
          const target = fresh
            && _nextGeneratedNativeTurn(fresh.state, fresh.turn);
          if (target) {
            return _operateNativeTurn(conv, target.turnId, 'regenerate', {
              content: text,
              images,
            });
          }
          if (typeof showToast === 'function') {
            showToast('No generated turn follows this input.', 'error');
          }
          return false;
        }
        const nextProjection = images?.length
          ? { ...projection, images: [...(projection.images || []), ...images] }
          : projection;
        return _commitNativeTurnEdit(conv, turnId, nextProjection, text);
      },
    });
    if (session) return;
  }
  /* Turns windowed out of the DOM keep the detached prompt path. */
  if (typeof showPrompt !== 'function') return;
  const edited = await showPrompt(
    typeof t === 'function' ? t('msgAction.edit') : 'Edit message',
    { defaultValue: currentText },
  );
  if (edited == null) return;
  const content = String(edited).trim();
  if (!content && !hasAttachments) return;
  await _commitNativeTurnEdit(conv, turnId, projection, content);
}

async function _deleteNativeTurn(conv, turnId) {
  const resolved = _nativeTurnState(conv, turnId);
  if (!resolved) return;
  const turnIds = [turnId];
  if (resolved.turn.actor === 'human') {
    const generated = _nextGeneratedNativeTurn(resolved.state, resolved.turn);
    if (generated) turnIds.push(generated.turnId);
  }
  const confirmed = typeof showConfirm !== 'function' || await showConfirm(
    turnIds.length > 1 ? 'Delete this turn and its reply?' : 'Delete this message?',
    { danger: true },
  );
  if (!confirmed) return;
  try {
    await runtimeScope.ConversationTurnStore.deleteConversationTurns(conv, turnIds);
  } catch (error) {
    try { await runtimeScope.ConversationTurnStore.hydrateConversation(conv); }
    catch (_ignored) { /* authoritative recovery is best effort */ }
    if (typeof showToast === 'function') {
      showToast(error?.body?.message || error?.message || 'Delete failed', 'error');
    }
  }
}

/** Human-only promotion of an assistant conclusion into executable Charter.
 * The conclusion is prefilled from the authoritative Turn projection, and a
 * registered immutable Checker version is mandatory. */
async function _promoteNativeDecision(conv, turnId, projection) {
  const projectPath = String(conv?.projectPath || '').trim();
  const api = typeof Api !== 'undefined' ? Api.project : null;
  if (!projectPath || !api?.brainCheckers || !api?.promoteDecision) {
    if (typeof showToast === 'function') {
      showToast('Attach a project before promoting a decision.', 'warning');
    }
    return;
  }
  try {
    const sourceText = String(
      projection?.originalContent || projection?.content || '',
    ).trim();
    const catalog = await api.brainCheckers(projectPath);
    const checkers = Array.isArray(catalog?.items)
      ? catalog.items.filter(item => item?.enabled) : [];
    if (!checkers.length) {
      const saveForTriage = typeof showConfirm !== 'function' || await showConfirm(
        'No checker is registered. Save this conclusion to Attention for later triage? You can also export the answer to docs.',
      );
      if (saveForTriage && api.brainAttentionAdd && sourceText) {
        await api.brainAttentionAdd(projectPath, sourceText.slice(0, 4000));
        if (typeof showToast === 'function') {
          showToast('Saved to Attention; it will not enter the model prompt.', 'success');
        }
      }
      if (typeof runtimeScope.openProjectBrain === 'function') {
        runtimeScope.openProjectBrain({ tab: 'charter', path: projectPath });
      }
      return;
    }
    if (typeof showPrompt !== 'function') return;
    const decisionText = await showPrompt('Promote as executable decision', {
      defaultValue: sourceText.slice(0, 4000),
    });
    if (!String(decisionText || '').trim()) return;
    const choices = checkers.map(item =>
      `${item.checkerId}@${item.version} — ${item.label || item.checkerId}`,
    );
    const selected = await showPrompt(
      `Select checker version:\n${choices.join('\n')}`,
      { defaultValue: `${checkers[0].checkerId}@${checkers[0].version}` },
    );
    if (!String(selected || '').trim()) return;
    const match = /^(.+)@(\d+)$/.exec(String(selected).trim());
    const checker = match && checkers.find(item =>
      String(item.checkerId) === match[1]
        && Number(item.version) === Number(match[2]),
    );
    if (!checker) {
      if (typeof showToast === 'function') {
        showToast('Choose an exact registered checker ID and version.', 'error');
      }
      return;
    }
    const identity = `decision:${conv.id}:${turnId}`.slice(0, 128);
    await api.promoteDecision(projectPath, {
      decisionId: identity,
      text: String(decisionText).trim(),
      checkerRef: { id: checker.checkerId, version: checker.version },
      sourceConversationId: conv.id,
      sourceTurnId: turnId,
    });
    if (typeof showToast === 'function') {
      showToast('Decision promoted with its checker.', 'success');
    }
  } catch (error) {
    if (typeof showToast === 'function') {
      showToast(error?.body?.message || error?.message || 'Decision promotion failed', 'error');
    }
  }
}

async function _createNativeBranch(conv, parentTurnId) {
  if (!conv || !parentTurnId
      || typeof runtimeScope.ConversationTurnStore === 'undefined') return;
  const title = typeof showPrompt === 'function'
    ? await showPrompt(typeof t === 'function' ? t('branch.namePrompt') : 'Branch name')
    : '';
  if (!title?.trim()) return;
  try {
    await runtimeScope.ConversationTurnStore.createBranchLane(
      conv, parentTurnId, { title: title.trim(), kind: 'branch' },
    );
  } catch (error) {
    if (typeof showToast === 'function') {
      showToast(error?.body?.message || error?.message || 'Branch creation failed', 'error');
    }
  }
}

async function _createNativeBranchFromSelection(
  conv, parentTurnId, title, selectedText,
) {
  if (!conv || !parentTurnId || !title?.trim()
      || typeof runtimeScope.ConversationTurnStore === 'undefined') return;
  try {
    await runtimeScope.ConversationTurnStore.createBranchLane(
      conv,
      parentTurnId,
      {
        title: title.trim(),
        kind: 'branch',
        anchorText: String(selectedText || '').slice(0, 200),
        parentSelection: String(selectedText || ''),
      },
    );
  } catch (error) {
    if (typeof showToast === 'function') {
      showToast(error?.body?.message || error?.message || 'Branch creation failed', 'error');
    }
  }
}

const _nativeBranchComposer = createBranchComposerSession();

function _nativeBranchDescriptor(conv, parentTurnId, laneId) {
  const state = runtimeScope.ConversationTurnStore
    ?.ensureRuntimeStore?.(conv.id)?.getState?.();
  const parent = state?.turnsById?.[parentTurnId];
  return parent?.projection?._branchLanes?.find(
    descriptor => descriptor?.laneId === laneId,
  ) || null;
}

function _renderNativeBranchComposerChrome() {
  const target = _nativeBranchComposer.current();
  const input = document.getElementById('userInput');
  if (input) {
    input.placeholder = target
      ? (typeof t === 'function'
        ? t('branch.inputPlaceholder', { title: target.title })
        : `Reply in ${target.title}`)
      : t('chat.messagePlaceholder');
  }
  let banner = document.getElementById('branch-mode-banner');
  if (!target) {
    if (banner) banner.remove();
    return;
  }
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'branch-mode-banner';
    banner.className = 'branch-mode-banner';
    const inputBox = document.querySelector('.input-box');
    if (inputBox?.parentElement) inputBox.parentElement.insertBefore(banner, inputBox);
  }
  const label = document.createElement('span');
  label.className = 'branch-mode-banner-text';
  label.textContent = typeof t === 'function'
    ? t('branch.modeBanner', { title: target.title })
    : `Replying in ${target.title}`;
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'branch-mode-banner-exit';
  close.textContent = `✕ ${typeof t === 'function' ? t('branch.exit') : 'Exit'}`;
  close.addEventListener('click', closeBranchPanel);
  banner.replaceChildren(label, close);
}

function _openNativeBranchComposer(conv, parentTurnId, laneId) {
  const descriptor = _nativeBranchDescriptor(conv, parentTurnId, laneId);
  if (!descriptor) return;
  _nativeBranchComposer.open({
    conversationId: conv.id,
    parentTurnId,
    laneId,
    title: descriptor.title || 'Branch',
  });
  _renderNativeBranchComposerChrome();
}

function _closeNativeBranchComposer(collapseSurface) {
  const previous = _nativeBranchComposer.close();
  _renderNativeBranchComposerChrome();
  if (collapseSurface && previous) {
    runtimeScope.ConversationSurfacePresentation?.setExpandedBranchLane?.(
      previous.conversationId, null,
    );
  }
}

function closeBranchPanel() {
  _closeNativeBranchComposer(true);
}

function isBranchModeActive() {
  return _nativeBranchComposer.isActive(
    typeof activeConvId === 'string' ? activeConvId : undefined,
  );
}

function getActiveBranchContext() {
  return _nativeBranchComposer.current();
}

window.addEventListener('tofu:language-change', _renderNativeBranchComposerChrome);

async function sendBranchMessage(text, images) {
  const target = _nativeBranchComposer.current();
  const conv = typeof getActiveConv === 'function' ? getActiveConv() : null;
  if (!target || !conv || conv.id !== target.conversationId) return;
  const store = runtimeScope.ConversationTurnStore.ensureRuntimeStore(conv.id);
  const state = store.getState();
  const laneTurnIds = state.laneOrder[target.laneId] || [];
  const live = [...laneTurnIds].reverse().map(turnId => state.turnsById[turnId])
    .find(turn => turn && (turn.status === 'pending' || turn.status === 'running'));
  if (live) return;
  let replyQuotes = [];
  if (typeof getPendingReplyQuotes === 'function') {
    replyQuotes = getPendingReplyQuotes() || [];
    if (replyQuotes.length) clearReplyQuote();
  }
  const payload = {
    text: String(text || ''),
    images: images || [],
    ...(replyQuotes.length ? { replyQuotes } : {}),
  };
  try {
    const config = await _buildConvConfig(conv);
    const descriptor = _nativeBranchDescriptor(
      conv, target.parentTurnId, target.laneId,
    ) || { laneId: target.laneId, title: target.title };
    await runtimeScope.ConversationTurnStore.submitBranch(
      conv,
      { ...descriptor, laneId: target.laneId },
      target.parentTurnId,
      payload,
      config,
      {
        commandId: typeof _newClientMsgId === 'function'
          ? _newClientMsgId() : `branch:${target.laneId}:${Date.now()}`,
        kind: 'branch_reply',
        actor: 'assistant',
      },
    );
  } catch (error) {
    try { await runtimeScope.ConversationTurnStore.hydrateConversation(conv); }
    catch (_ignored) { /* best-effort recovery */ }
    if (typeof showToast === 'function') {
      showToast(error?.body?.message || error?.message || 'Branch send failed', 'error');
    }
  }
}

function promptNewBranch(parentTurnId, preTitle, selectedText) {
  const conv = typeof getActiveConv === 'function' ? getActiveConv() : null;
  if (!conv || !parentTurnId) return;
  void _createNativeBranchFromSelection(
    conv, parentTurnId, preTitle || String(selectedText || '').slice(0, 40), selectedText,
  );
}

async function _deleteNativeBranch(conv, parentTurnId, laneId) {
  if (!conv || !parentTurnId || !laneId
      || typeof runtimeScope.ConversationTurnStore === 'undefined') return;
  const confirmed = typeof showConfirm !== 'function' || await showConfirm(
    typeof t === 'function' ? t('branch.deleteConfirm') : 'Delete this branch?',
    { danger: true },
  );
  if (!confirmed) return;
  const state = _nativeTurnState(conv, parentTurnId)?.state;
  const live = [...(state?.laneOrder?.[laneId] || [])].reverse()
    .map(id => state.turnsById[id])
    .find(turn => turn?.currentAttemptId
      && ['pending', 'running'].includes(turn.status));
  if (live?.currentAttemptId) {
    runtimeScope.ConversationTurnStore.abortAttempt(live.currentAttemptId)
      .catch(error => console.warn('[ConversationSurface] branch abort failed', error));
  }
  try {
    await runtimeScope.ConversationTurnStore.deleteBranchLane(
      conv, parentTurnId, laneId,
    );
  } catch (error) {
    try { await runtimeScope.ConversationTurnStore.hydrateConversation(conv); } catch (_e) { /* best effort */ }
    if (typeof showToast === 'function') showToast('Branch delete failed', 'error');
  }
}

function _stopNativeBranch(conv, laneId) {
  if (!conv || !laneId || typeof runtimeScope.ConversationTurnStore === 'undefined') return;
  const store = runtimeScope.ConversationTurnStore.ensureRuntimeStore(conv.id);
  const ids = store.getState().laneOrder[laneId] || [];
  const live = [...ids].reverse().map(id => store.getState().turnsById[id]).find(turn =>
    turn?.currentAttemptId && ['pending', 'running'].includes(turn.status));
  if (live?.currentAttemptId) {
    runtimeScope.ConversationTurnStore.abortAttempt(live.currentAttemptId)
      .catch(error => console.warn('[ConversationSurface] branch stop failed', error));
  }
}

async function _removeNativeQueueItem(conv, queueId) {
  const service = runtimeScope.ConversationTurnStore;
  if (!conv || !queueId || !service?.cancelQueuedTurn) return;
  try {
    const response = await service.cancelQueuedTurn(conv, queueId);
    if (!response?.cancelled) throw new Error('Queue removal was rejected');
    const projection = response.inputTurn?.projection || {};
    const attachments = Array.isArray(projection.attachments)
      ? projection.attachments : [];
    if (typeof _restoreCapturedComposerDraft === 'function') {
      _restoreCapturedComposerDraft({
        inputValue: projection.originalContent || projection.content || '',
        images: Array.isArray(projection.images) ? projection.images : [],
        pdfTexts: [
          ...(Array.isArray(projection.pdfTexts) ? projection.pdfTexts : []),
          ...attachments.filter((item) => item?.kind === 'document'),
        ],
        videos: [
          ...(Array.isArray(projection.videos) ? projection.videos : []),
          ...attachments.filter((item) => item?.kind === 'video'),
        ],
        replyQuotes: Array.isArray(projection.replyQuotes)
          ? projection.replyQuotes : [],
        convRefs: Array.isArray(projection.convRefs) ? projection.convRefs : [],
      });
    }
    if (typeof updateSendButton === 'function') updateSendButton();
  } catch (error) {
    if (typeof showToast === 'function') showToast('Queue removal failed', 'error');
    console.warn('[ConversationSurface] queue removal failed', error);
  }
}

async function _mutateNativeTurnFiles(conv, turnId, operation) {
  const service = runtimeScope.ConversationTurnStore;
  if (!conv || !turnId || !service?.mutateConversationFileChanges) return;
  const current = service.ensureRuntimeStore(conv.id)
    .getState().turnsById[turnId];
  const count = current?.projection?.fileChanges?.count || 0;
  if (operation === 'undo' && typeof showConfirm === 'function') {
    const confirmed = await showConfirm(
      typeof t === 'function'
        ? t('project.undoTurnConfirm', { count })
        : `Undo changes to ${count} file${count === 1 ? '' : 's'}?`,
      { danger: true },
    );
    if (!confirmed) return;
  }
  try {
    const response = await service.mutateConversationFileChanges(
      conv, turnId, operation,
    );
    const effect = response?.effect || {};
    if (typeof showToast === 'function') {
      const amount = operation === 'undo' ? effect.undone : effect.redone;
      showToast(
        operation === 'undo' ? '↩️' : '↪️',
        operation === 'undo' ? 'Undo Complete' : 'Redo Complete',
        Number.isFinite(Number(amount))
          ? `${operation === 'undo' ? 'Reverted' : 'Re-applied'} ${amount} file change${
              Number(amount) === 1 ? '' : 's'}`
          : '',
        4000,
      );
    }
    if (typeof rescanProject === 'function') void rescanProject();
  } catch (error) {
    if (typeof showToast === 'function') {
      showToast(
        error?.body?.message || error?.message
          || `${operation === 'undo' ? 'Undo' : 'Redo'} failed`,
        'error',
      );
    }
  }
}

const _nativeTurnRenderers = createClassicConversationRenderers({
  renderSafeMarkdownHtml(markdown, options) {
    return typeof renderMarkdown === 'function'
      ? renderMarkdown(markdown, options)
      : escapeHtml(markdown);
  },
  renderTurnAvatarHtml(turn) {
    const initiator = turn?.metadata?.origin?.initiator;
    if (initiator === 'peer' || initiator === 'operator') {
      return '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" '
        + 'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        + 'stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 '
        + '4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path '
        + 'd="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg>';
    }
    if (turn.actor === 'planner') {
      return _resolveRoleAvatarIcons()?.plannerHtml || '';
    }
    if (turn.actor === 'critic' || turn.actor === 'virtual_user') {
      return _resolveRoleAvatarIcons()?.criticHtml || '';
    }
    if (turn.actor === 'human' && (!initiator || initiator === 'human')) {
      return _resolveRoleAvatarIcons()?.userHtml || '';
    }
    return _resolveRoleAvatarIcons()?.workerHtml || '';
  },
  renderTurnContextParts(block) {
    const rendered = runtimeScope.renderTurnCtxNote?.(block?.value?.snapshot);
    return rendered && typeof rendered === 'object'
      ? { fold: rendered.fold || '', rail: rendered.rail || '' }
      : null;
  },
  renderToolBlockHtml(block, turn) {
    const sourceRound = block.round || {
      attemptId: block.source?.attemptId,
      taskId: block.source?.taskId,
      roundNum: block.source?.llmRound == null ? undefined : Number(block.source.llmRound) + 1,
      llmRound: block.source?.llmRound,
      toolCallId: block.toolCallId,
      toolName: block.name,
      toolArgs: block.input,
      toolContent: block.result?.content,
      result: block.result,
      status: block.result?.status,
    };
    /* A settled turn can never receive a human-guidance answer again — the
     * backend pending-request map is process-local and the blocked handler
     * either finalized the round itself (user Stop) or died with the process
     * (server restart, which leaves the persisted round at awaiting_human
     * forever). The renderer downgrades those leftovers to an expired,
     * non-interactive card off this flag. */
    const turnSettled = !(turn.status === 'pending' || turn.status === 'running');
    /* A settled turn that died inside ask_human keeps the persisted question
     * round at awaiting_human forever. When its settlement offers the
     * answer_guidance resume option for THIS question, the expired card
     * stays answerable: the late answer becomes the round's tool result in
     * a new attempt and generation continues from the answered call. */
    const resumeOptions = turn.source?.settlement?.resumeOptions;
    const answerGuidanceOfferedFor = (round) => {
      const guidanceId = typeof round?.guidanceId === 'string'
        ? round.guidanceId : '';
      return turnSettled && Boolean(guidanceId)
        && Array.isArray(resumeOptions)
        && resumeOptions.some((option) => (
          option?.operation === 'answer_guidance'
          && option?.anchor?.guidanceId === guidanceId
        ));
    };
    const decorateRound = (round) => {
      /* A resumed Turn contains rounds from several attempts. Prefer the
       * round-owned task; using the Turn's latest task made every historical
       * R-link open the resumed executor and erased the only presentation
       * boundary between repeated llmRound counters. */
      const ownedTaskId = round?.taskId || round?._taskId || '';
      const compatibleLatestTask = !round?.attemptId
        || round.attemptId === turn.attemptId;
      const taskId = ownedTaskId || (compatibleLatestTask ? turn.taskId : '');
      return {
        ...humanGuidancePresentation.decorate(
          turn.source.conversationId, round,
        ),
        _turnId: turn.turnId,
        _turnSettled: turnSettled,
        ...(answerGuidanceOfferedFor(round) ? { _hgAnswerGuidance: true } : {}),
        ...(taskId ? { _taskId: taskId } : {}),
      };
    };
    const round = decorateRound(sourceRound);
    const allRounds = Array.isArray(turn.source.projection.toolRounds)
      ? turn.source.projection.toolRounds.map(decorateRound) : [round];
    if (typeof _renderToolSlot === 'function') return _renderToolSlot(round, allRounds);
    return typeof renderToolRoundsHTML === 'function'
      ? renderToolRoundsHTML([round], turn.status === 'pending' || turn.status === 'running')
      : '';
  },
  renderInjectionBlockHtml(block, turn) {
    const fieldByChannel = {
      inbox: '_inboxInjects', peer: '_peerInjects',
      'user-steer': '_userSteerInjects', 'stall-nudge': '_stallNudges',
    };
    const field = fieldByChannel[block.channel];
    if (!field || typeof _rehydrateInjectRows !== 'function') return '';
    const projection = { [field]: block.items };
    const rounds = _rehydrateInjectRows(projection, []).map((round) => ({
      ...humanGuidancePresentation.decorate(
        turn.source.conversationId, round,
      ),
      _turnId: turn.turnId,
      ...(turn.taskId ? { _taskId: turn.taskId } : {}),
    }));
    if (typeof _renderToolSlot === 'function') {
      return rounds.map(round => _renderToolSlot(round, rounds)).join('');
    }
    return typeof renderToolRoundsHTML === 'function'
      ? renderToolRoundsHTML(rounds, false) : '';
  },
  renderTurnFooterHtml(turn) {
    if (turn.actor === 'human'
        || !['completed', 'interrupted', 'truncated', 'failed'].includes(turn.status)) {
      return '';
    }
    if (typeof renderFinishInfo !== 'function') return '';
    /* Finish presentation consumes the selected Turn directly.  Do not join
     * back through the positional compatibility message array: that would
     * make an otherwise keyed render depend on array order and a second
     * projection snapshot. */
    const finishProjection = {
      ...turn.source.projection,
      _conversationId: turn.source.conversationId,
      ...(turn.taskId ? { _taskId: turn.taskId } : {}),
      _turnStatus: turn.status,
      _turnSettlement: turn.source.settlement || {},
      /* Lifecycle clock from the authoritative TurnRecord (epoch ms): the
       * finish bar renders the task's completion time and wall-clock
       * duration; the projection itself carries no timing. */
      _turnCreatedAt: Number(turn.source.createdAt) || 0,
      _turnUpdatedAt: Number(turn.source.updatedAt) || 0,
      _commandPending: turn.commandPending || null,
      waitingOn: turn.source.projection.waitingOn,
    };
    if (turn.metadata.fallbackInTimeline) {
      delete finishProjection.fallbackModel;
      delete finishProjection.fallbackFrom;
      delete finishProjection.fallbackReason;
      delete finishProjection.fallbackKind;
    }
    const html = renderFinishInfo(finishProjection, turn.turnId);
    if (!html) return '';
    const holder = document.createElement('div');
    holder.innerHTML = html;
    const finish = holder.firstElementChild;
    return finish?.classList.contains('message-finish') ? finish.innerHTML : html;
  },
  renderProvenanceBlockHtml(block) {
    const value = block.value || {};
    return renderMcpLoginHintHtml(value.mcpLoginHint)
      + renderTurnProvenanceHtml(value)
      + renderPreferenceLearnedHtml(value.preferencesLearned);
  },
  resolveMediaUrl(url) {
    return typeof apiUrl === 'function' && String(url || '').startsWith('/')
      ? apiUrl(url) : String(url || '');
  },
  localizedText(key, fallback, values) {
    const localizedValues = values && typeof values === 'object'
      ? { ...values } : values;
    const reasonKey = localizedValues?.reasonKey;
    if (reasonKey && typeof t === 'function') {
      const reason = t(reasonKey);
      if (reason && reason !== reasonKey) localizedValues.reason = reason;
    }
    const translated = typeof t === 'function' ? t(key, localizedValues) : '';
    return translated && translated !== key ? translated : fallback;
  },
  roleLabel(turn) {
    const initiator = turn?.metadata?.origin?.initiator;
    const originLabels = {
      autopilot: ['initiator.autopilot', 'Autopilot'],
      proactive: ['initiator.proactive', 'Proactive Agent'],
      timer: ['initiator.timer', 'Timer'],
      brain: ['initiator.brain', 'Project Brain'],
      peer: ['peer.senderLabel', 'Peer'],
      operator: ['peer.operatorLabel', 'Operator'],
      swarm: ['initiator.swarm', 'Auto-continued'],
    };
    if (originLabels[initiator]) {
      const [key, fallback] = originLabels[initiator];
      const translated = typeof t === 'function' ? t(key) : '';
      return translated && translated !== key ? translated : fallback;
    }
    if (turn.actor === 'human') {
      return typeof t === 'function' ? t('role.you') : 'You';
    }
    return {
      assistant: 'Agent', planner: 'Planner', critic: 'Critic',
      virtual_user: 'Autopilot',
    }[turn.actor] || turn.actor;
  },
  actionLabel(action, turn, actionView) {
    const keys = {
      copy: 'msgAction.copy', inspect: 'msgAction.inspect',
      edit: 'msgAction.edit', regenerate: 'msgAction.regen',
      resume: 'msgAction.continue', translate: 'msgAction.translate',
      export: 'msgAction.export', 'promote-decision': 'msgAction.promoteDecision',
      branch: 'branch.add', delete: 'msgAction.delete',
    };
    const fallbacks = {
      copy: 'Copy', inspect: 'Inspect', edit: 'Edit', regenerate: 'Regen',
      resume: 'Continue',
      translate: 'Translate', export: 'Export',
      'promote-decision': 'Promote as decision', branch: 'Branch', delete: 'Delete',
    };
    /* A failed turn whose settlement offers no resumable checkpoint still
     * renders the generic 'resume' action; label it for what it does. */
    const labelAction = action === 'resume'
        && actionView?.operation === 'regenerate' ? 'regenerate' : action;
    const key = labelAction === 'translate'
        && turn?.metadata?.translation?.available
        && turn.metadata.translation.displayMode === 'translated'
      ? 'msgAction.original' : (keys[labelAction] || labelAction);
    const translated = typeof t === 'function' ? t(key) : '';
    const fallback = labelAction === 'translate' && key === 'msgAction.original'
      ? 'Original' : (fallbacks[labelAction] || labelAction);
    return translated && translated !== key ? translated : fallback;
  },
  formatTimestamp(timestamp) {
    const value = new Date(timestamp);
    return Number.isNaN(value.getTime()) ? '' : value.toLocaleString([], {
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit',
    });
  },
});

const _planDecisionBar = createPlanDecisionBar({
  translate(key) {
    return typeof t === 'function' ? t(key) : '';
  },
  onContinueDiscussion(conversationId) {
    if (typeof activeConvId !== 'undefined' && activeConvId !== conversationId) return;
    if (typeof setAgentMode === 'function') setAgentMode('plan');
    document.getElementById('userInput')?.focus();
  },
  async onExecute(conversationId, decision, contextMode) {
    const service = runtimeScope.ConversationTurnStore;
    const conv = service?.findConversation?.(conversationId)
      || (typeof conversations !== 'undefined'
        ? conversations.find(item => item?.id === conversationId) : null);
    if (!conv || !service?.executeConversationPlan
        || (typeof activeConvId !== 'undefined'
          && activeConvId !== conversationId)) {
      throw new Error('Conversation execution runtime is unavailable.');
    }
    const config = await _buildConvConfig(conv);
    const hadAutopilot = Boolean(conv.autopilotEnabled);
    await service.executeConversationPlan(
      conv,
      decision.sourceTurnId,
      decision.planId,
      decision.sourceProjectionRevision,
      contextMode,
      config,
    );
    if (typeof activeConvId !== 'undefined' && activeConvId === conversationId) {
      if (typeof _applyAgentModeUI === 'function') _applyAgentModeUI('standard');
      if (typeof _applyFlowUI === 'function') _applyFlowUI('');
      if (typeof _applyImageGenUI === 'function') _applyImageGenUI(false);
      if (hadAutopilot && typeof _disarmAutopilot === 'function') {
        _disarmAutopilot();
      }
      if (typeof updateSendButton === 'function') updateSendButton();
    }
    if (typeof ConvCache !== 'undefined') void ConvCache.put(conv);
  },
  onError(error, conversationId) {
    const conv = runtimeScope.ConversationTurnStore?.findConversation?.(
      conversationId,
    ) || (typeof conversations !== 'undefined'
      ? conversations.find(item => item?.id === conversationId) : null);
    if (conv) {
      void runtimeScope.ConversationTurnStore?.hydrateConversation?.(conv)
        .catch(() => {});
    }
    if (typeof showToast === 'function'
        && (typeof activeConvId === 'undefined'
          || activeConvId === conversationId)) {
      showToast(
        error?.body?.message || error?.message || 'Plan execution failed',
        'error',
      );
    }
  },
});

runtimeScope.PlanDecisionPresentation = Object.freeze({
  activateConversation(conversationId) {
    _planDecisionBar.activateConversation(conversationId || null);
  },
});

const _conversationSurfaceController = createConversationSurfaceController({
  isActive(conversationId) {
    return typeof activeConvId !== 'undefined' && activeConvId === conversationId;
  },
  getContainer() {
    return document.getElementById('chatInner');
  },
  schedule(render) {
    if (typeof requestAnimationFrame === 'function') {
      const handle = requestAnimationFrame(render);
      return () => cancelAnimationFrame(handle);
    }
    let cancelled = false;
    queueMicrotask(() => { if (!cancelled) render(); });
    return () => { cancelled = true; };
  },
  nativeRenderers: {
    ..._nativeTurnRenderers,
    renderPlanDecision(node, decision, context) {
      _planDecisionBar.activateConversation(context.conversationId);
      _planDecisionBar.render(node, context.conversationId, decision);
    },
  },
  requestInspectorEnabled() {
    return Boolean(
      typeof _featureFlags !== 'undefined' && _featureFlags?.debug_mode,
    );
  },
  /* Settled turns whose projection predates the server-side cost fold have
   * no authoritative msg.cost; renderFinishInfo falls back to calcCostCny,
   * whose micro-batch lands AFTER the render commit. The batch then requests
   * an authoritative re-render, but nothing in the Turn changed, so the
   * surface footer compare would skip the re-render and the ¥ tag never
   * appears. The signature makes the fill visible to that compare. Cache
   * misses return null and simply rejoin the already-queued batch — no extra
   * requests. */
  costSignatureSnapshot(state) {
    const signatures = new Map();
    if (typeof calcCostCny !== 'function') return signatures;
    const turnsById = state?.turnsById || {};
    for (const turnId of Object.keys(turnsById)) {
      const turn = turnsById[turnId];
      if (!turn || turn.actor !== 'assistant') continue;
      if (!['completed', 'interrupted', 'truncated', 'failed']
          .includes(turn.status)) continue;
      const projection = turn.projection || {};
      if (projection.cost || !projection.usage) continue;
      const cost = calcCostCny(
        projection.usage,
        projection.model || '',
        projection.providerId || projection.provider_id || '',
      );
      if (cost && cost.costCny > 0) {
        signatures.set(turnId, String(cost.costCny));
      }
    }
    return signatures;
  },
  getScrollViewport() {
    return document.getElementById('chatContainer');
  },
  windowing: {
    label(direction, count) {
      return t(
        direction === 'earlier' ? 'chat.loadEarlierTurns' : 'chat.loadLaterTurns',
        { n: count },
      );
    },
  },
  followLatest() {
    if (typeof scrollToBottom === 'function') scrollToBottom(true);
  },
  onIntent(intent) {
    const conv = typeof getActiveConv === 'function' ? getActiveConv() : null;
    if (!conv || conv.id !== intent.conversationId) return;
    if (intent.type === 'load-earlier-turns' && intent.laneId) {
      void runtimeScope.ConversationTurnStore?.loadConversationTurnPage?.(
        conv,
        intent.laneId,
        intent.beforeOrdinal,
        intent.limit,
      ).catch((error) => {
        if (typeof showToast === 'function') {
          showToast(error?.message || 'Unable to load earlier turns', 'error');
        }
      });
      return;
    }
    if (intent.type === 'remove-queue' && intent.queueId) {
      void _removeNativeQueueItem(conv, intent.queueId);
      return;
    }
    if (intent.type === 'open-project-brain') {
        if (typeof runtimeScope.openProjectBrain === 'function') {
          runtimeScope.openProjectBrain();
        }
      return;
    }
    if (intent.type === 'open-conversation' && intent.operation) {
      if (typeof loadConversation === 'function') loadConversation(intent.operation);
      return;
    }
    if (intent.type === 'open-compaction' && intent.operation) {
        if (typeof runtimeScope.openCompactionViewer === 'function') {
          runtimeScope.openCompactionViewer(conv.id, intent.operation);
      }
      return;
    }
    if (intent.type === 'cancel-image-generation') {
      runtimeScope._igCancelGeneration?.();
      return;
    }
    if (intent.type === 'open-artifact' && intent.operation) {
      runtimeScope.Artifacts?.open?.(intent.operation);
      return;
    }
    if (!intent.turnId) return;
    if (intent.type === 'undo-turn-files' || intent.type === 'redo-turn-files') {
      void _mutateNativeTurnFiles(
        conv,
        intent.turnId,
        intent.type === 'undo-turn-files' ? 'undo' : 'redo',
      );
      return;
    }
    const resolved = _nativeTurnState(conv, intent.turnId);
    const transientTurn = runtimeScope.ConversationTransientTurns?.get?.(
      conv.id, intent.turnId,
    );
    if (!resolved && !transientTurn) return;
    const projection = resolved?.turn?.projection || transientTurn?.projection || {};
    const attachmentIndex = Number.parseInt(intent.operation || '', 10);
    const safeAttachmentUrl = (value, allowImageData) => {
      const raw = typeof value === 'string' ? value.trim() : '';
      if (/^(?:https?:|blob:)/i.test(raw)) return raw;
      // Root-relative API URLs must carry the deployment BASE_PATH (same as
      // the renderer's resolveMediaUrl port), or the modal requests them
      // from the server root and 404s when the app is served under a subpath.
      if (raw.startsWith('/')) {
        return typeof apiUrl === 'function' ? apiUrl(raw) : raw;
      }
      if (/^\.\.?\//.test(raw)) return raw;
      return allowImageData && /^data:image\/(?:avif|gif|jpe?g|png|webp);base64,/i.test(raw)
        ? raw : '';
    };
    if (intent.type === 'toggle-branch' && intent.laneId) {
      if (intent.operation === 'open') {
        _openNativeBranchComposer(conv, intent.turnId, intent.laneId);
      } else {
        _closeNativeBranchComposer(false);
      }
      return;
    }
    else if (intent.type === 'delete-branch' && intent.laneId) {
      void _deleteNativeBranch(conv, intent.turnId, intent.laneId);
    }
    else if (intent.type === 'stop-branch' && intent.laneId) {
      _stopNativeBranch(conv, intent.laneId);
    }
    else if (intent.type === 'preview-image' && Number.isInteger(attachmentIndex)) {
      const source = safeAttachmentUrl(
        projection.images?.[attachmentIndex]?.preview, true,
      );
      if (source && typeof openImagePreview === 'function') openImagePreview(source);
    }
    else if (intent.type === 'preview-generated-image'
        && Number.isInteger(attachmentIndex)) {
      const source = safeAttachmentUrl(
        projection.imageGeneration?.results?.[attachmentIndex]?.imageUrl, true,
      );
      if (source && typeof openImagePreview === 'function') openImagePreview(source);
    }
    else if (intent.type === 'retry-image-generation'
        && Number.isInteger(attachmentIndex)) {
      runtimeScope._igRetryGenerationTurn?.(conv, intent.turnId, attachmentIndex);
    }
    else if (intent.type === 'open-media' && intent.operation) {
      const mediaAttachment = projection.attachments?.find(
        (item) => item?.attachmentId === intent.operation,
      );
      const source = safeAttachmentUrl(mediaAttachment?.sourceUrl, false);
      if (source && typeof openVideoUrl === 'function') openVideoUrl(source);
    }
    else if (intent.type === 'open-video' && Number.isInteger(attachmentIndex)) {
      const source = safeAttachmentUrl(
        projection.videos?.[attachmentIndex]?.video_url, false,
      );
      if (source && typeof openVideoUrl === 'function') openVideoUrl(source);
    }
    else if (intent.type === 'preview-document' && Number.isInteger(attachmentIndex)) {
      const documentAttachment = projection.pdfTexts?.[attachmentIndex];
      if (documentAttachment && typeof openTextPreview === 'function') {
        openTextPreview(
          documentAttachment.name || 'Document',
          `${documentAttachment.pages || '?'} pages`,
          documentAttachment.text || '',
        );
      }
    }
    else if (intent.type === 'copy' && typeof _safeClipboardWrite === 'function') {
      const translatedSegments = (projection.segments || []).filter(segment =>
        (segment.type === 'text' || segment.type === 'thinking')
          && segment.translatedText).map(segment => segment.translatedText);
      const copyText = intent.operation === 'copy-translated'
        ? (projection.translatedContent || translatedSegments.join('\n\n')
          || projection.content || '')
        : (projection.originalContent || projection.content || '');
      _safeClipboardWrite(copyText)
        .then(() => _flashTurnCopyFeedback(intent.turnId))
        .catch(() => {});
    }
    else if (intent.type === 'inspect' && intent.operation
        && typeof runtimeScope.openRequestInspectorForTask === 'function') {
      void runtimeScope.openRequestInspectorForTask(intent.operation);
    }
    else if (intent.type === 'edit') {
      void _editNativeTurn(conv, intent.turnId);
    }
    else if (intent.type === 'regenerate') {
      void _regenerateNativeInput(conv, intent.turnId);
    }
    else if (intent.type === 'resume' && intent.operation) {
      void _operateNativeTurn(conv, intent.turnId, intent.operation);
    }
    else if (intent.type === 'translate') {
      void _runManualTurnTranslation(
        conv, intent.turnId, projection.content || '',
      );
    }
    else if (intent.type === 'export') {
      if (typeof ExportImages !== 'undefined') {
        ExportImages.exportMessageWithPreview(intent.turnId);
      }
    }
    else if (intent.type === 'promote-decision') {
      void _promoteNativeDecision(conv, intent.turnId, projection);
    }
    else if (intent.type === 'branch') void _createNativeBranch(conv, intent.turnId);
    else if (intent.type === 'delete') void _deleteNativeTurn(conv, intent.turnId);
  },
  afterConversationCommit(conv, state, _force, viewModel) {
    /* Running and terminal revisions reconcile into the same data-turn-id
     * node; this adapter never creates an alternate message identity. */
    if (typeof buildTurnNav === 'function') buildTurnNav(conv);
    if (typeof _cvRefreshContextGauge === 'function') _cvRefreshContextGauge();
    if (typeof reconcileTurnInlineEditors === 'function') {
      reconcileTurnInlineEditors();
    }
  },
});

const _conversationTransientTurnOverlay = createTransientTurnOverlay();

runtimeScope.ConversationSurfacePresentation = Object.freeze({
  followLatest() {
    _conversationSurfaceController.followLatest();
  },
  setTranslationActivity(conversationId, turnId, activity) {
    _conversationSurfaceController.setTranslationActivity(
      conversationId, turnId, activity || null,
    );
  },
  setExpandedBranchLane(conversationId, laneId) {
    _conversationSurfaceController.setExpandedBranchLane(conversationId, laneId || null);
  },
  setArtifacts(conversationId, artifactsByTurn) {
    _conversationSurfaceController.setArtifacts(
      conversationId,
      artifactsByTurn instanceof Map ? artifactsByTurn : new Map(),
    );
  },
});

function _conversationReadState(conversationOrId) {
  const conversationId = typeof conversationOrId === 'string'
    ? conversationOrId : conversationOrId?.id;
  return conversationId
    ? runtimeScope.ConversationTurnStore?.readRuntimeState?.(conversationId) || null
    : null;
}

runtimeScope.ConversationTurnRead = Object.freeze({
  state: _conversationReadState,
  activeAttemptIds(conversationOrId) {
    return activeConversationAttemptIds(_conversationReadState(conversationOrId));
  },
  activeMainAttemptId(conversationOrId) {
    return activeMainConversationAttemptId(_conversationReadState(conversationOrId));
  },
  ordered(conversationOrId, laneId) {
    return orderedConversationTurns(
      _conversationReadState(conversationOrId), laneId || 'main',
    );
  },
  latest(conversationOrId, predicate, laneId) {
    return latestConversationTurn(
      _conversationReadState(conversationOrId), predicate, laneId || 'main',
    );
  },
  hasActor(conversationOrId, actor, laneId) {
    return conversationHasActor(
      _conversationReadState(conversationOrId), actor, laneId || 'main',
    );
  },
});

function _renderConversationSurfaceState(conv, state, repaint) {
  const transientState = _conversationTransientTurnOverlay.compose(state);
  const presentationState = runtimeScope.ConversationSwarmPresentation?.compose?.(
    conv, state, transientState,
  ) || transientState;
  return _conversationSurfaceController.render(
    conv,
    presentationState,
    repaint || {},
  );
}

runtimeScope.ConversationTransientTurns = Object.freeze({
  upsert(conv, turn) {
    if (!conv || !turn) return false;
    _conversationTransientTurnOverlay.upsert(turn);
    const store = runtimeScope.ConversationTurnStore?.ensureRuntimeStore?.(conv.id);
    return store ? _renderConversationSurfaceState(conv, store.getState()) : false;
  },
  remove(conv, turnId) {
    if (!conv || !turnId) return false;
    const removed = _conversationTransientTurnOverlay.remove(conv.id, turnId);
    const store = runtimeScope.ConversationTurnStore?.ensureRuntimeStore?.(conv.id);
    if (store) _renderConversationSurfaceState(conv, store.getState());
    return removed;
  },
  replace(conv, previousTurnId, turn) {
    if (!conv || !turn) return false;
    if (previousTurnId && previousTurnId !== turn.turnId) {
      _conversationTransientTurnOverlay.remove(conv.id, previousTurnId);
    }
    _conversationTransientTurnOverlay.upsert(turn);
    const store = runtimeScope.ConversationTurnStore?.ensureRuntimeStore?.(conv.id);
    return store ? _renderConversationSurfaceState(conv, store.getState()) : false;
  },
  get(conversationId, turnId) {
    return _conversationTransientTurnOverlay.get(conversationId, turnId);
  },
  clear(conversationId) {
    _conversationTransientTurnOverlay.clear(conversationId);
  },
});

/* Sole repaint bridge for retained feature modules. A side effect may request
 * convergence, but only the authoritative keyed Surface may write chat DOM. */
runtimeScope.renderAuthoritativeConversationSurface = function (conversationId, repaint) {
  const service = runtimeScope.ConversationTurnStore;
  const conv = typeof conversations !== 'undefined'
    ? conversations.find(item => item?.id === conversationId) : null;
  if (!service || !conv) return false;
  const store = service.ensureRuntimeStore(conversationId);
  const state = store?.getState?.();
  if (!state || (!store._snapshotLoaded
      && Object.keys(state.turnsById || {}).length === 0)) return false;
  return _renderConversationSurfaceState(conv, state, repaint);
};

/* Retained feature modules may request convergence, but never choose a DOM
 * renderer. If a snapshot is already present this commits synchronously;
 * otherwise one coalesced hydration feeds TurnStore and Surface. */
runtimeScope.requestAuthoritativeConversationRender = function (
  conversationId, repaint,
) {
  if (runtimeScope.renderAuthoritativeConversationSurface(
    conversationId, repaint || {},
  )) return true;
  const service = runtimeScope.ConversationTurnStore;
  const conv = service?.findConversation?.(conversationId)
    || (typeof conversations !== 'undefined'
      ? conversations.find(item => item?.id === conversationId) : null);
  if (!service?.ensureRuntimeStore || !conv) return false;
  const store = service.ensureRuntimeStore(conversationId);
  const state = store?.getState?.();
  const rendered = state
    ? _renderConversationSurfaceState(conv, state, repaint || {}) : false;
  if (store?._snapshotLoaded || conv._localOnly) return Boolean(rendered);
  if (!service.hydrateConversation) return Boolean(rendered);
  Promise.resolve(service.hydrateConversation(conv)).catch((error) => {
    console.warn(
      '[ConversationSurface] authoritative render hydration failed:',
      error?.message || error,
    );
  });
  return true;
};

(function installConversationTurnStore(global) {
  'use strict';
  runtimeScope.ConversationTurnStore = createConversationTurnRuntime({
    api: conversationSyncApi,
    streamClientId: requiredApiTransport.pageRequestId(),
    findConversation(conversationId) {
      if (typeof conversations === 'undefined' || !Array.isArray(conversations)) return null;
      return conversations.find(item => item?.id === conversationId) || null;
    },
    persist: _throttledTurnCachePut,
    isActive(conv) {
      return typeof activeConvId !== 'undefined' && activeConvId === conv.id;
    },
    isDomStale(conv) {
      return typeof activeConvId !== 'undefined' && activeConvId === conv.id
        && !_conversationSurfaceController.ownsConversation(conv.id);
    },
    applySettings(conv, settings) {
      if (typeof _applySettingsToConv === 'function') _applySettingsToConv(conv, settings);
    },
    applySnapshotMetadata(conv, snapshot) {
      void runtimeScope.Artifacts?.hydrateConversation?.(
        conv,
        typeof snapshot?.hasArtifacts === 'boolean'
          ? snapshot.hasArtifacts : undefined,
      );
    },
    renderState(conv, state, repaint) {
      return _renderConversationSurfaceState(conv, state, repaint);
    },
    disposeRenderedState(conversationId) {
      const state = runtimeScope.ConversationTurnRead?.state?.(conversationId);
      if (typeof clearFinishInfoPresentation === 'function') {
        clearFinishInfoPresentation(Object.keys(state?.turnsById || {}));
      }
      humanGuidancePresentation.clearConversation(conversationId);
      _conversationSurfaceController.disposeConversation(conversationId);
      _conversationTransientTurnOverlay.clear(conversationId);
    },
    deferTerminalRelease: typeof global.requestAnimationFrame === 'function'
      ? (release) => global.requestAnimationFrame(() =>
          global.requestAnimationFrame(() => release()))
      : undefined,
    buildNavigation(conv) {
      if (typeof buildTurnNav === 'function') buildTurnNav(conv);
    },
    renderConversationList: typeof renderConversationList === 'function'
      ? renderConversationList : undefined,
    updateSendButton: typeof updateSendButton === 'function'
      ? updateSendButton : undefined,
    onTurnSettled(conv, turn) {
      runtimeScope._maybeAutoGenerateTitleForSettledTurn?.(conv, turn);
    },
    onProtocolError(error) {
      console.warn('[ConversationSync] stream protocol error:', error && error.message);
    },
    onResyncError(error, turnId) {
      console.warn('[ConversationSync] snapshot recovery failed for turn',
                   turnId, error?.message || error);
    },
    onHealth(conversationId, health) {
      const conv = conversations.find(item => item?.id === conversationId);
      if (conv) conv._conversationSyncHealth = health;
      if (typeof renderConversationList === 'function') renderConversationList();
      if (conversationId === activeConvId) {
        runtimeScope.requestAuthoritativeConversationRender?.(
          conversationId, { force: false, forceScroll: false },
        );
      }
    },
  });
  runtimeScope.createTurnState = runtimeScope.ConversationTurnStore.emptyState;
  runtimeScope.reduceTurnState = runtimeScope.ConversationTurnStore.reducer;
})(window);
