/* ===== migrated source: ui/swarm_push.js ===== */
/* Conversation-scoped swarm presentation overlay.
 *
 * Push frames are lifecycle telemetry, not durable Turn facts. They reduce into
 * a copied projection attached through ConversationTransientTurns; TurnStore
 * and durable TurnState remain immutable. A terminal frame hydrates the
 * backend snapshot before removing the overlay.
 */

(function _wireServerPushSwarm() {
  if (typeof pushSubscribe !== 'function' || runtimeScope.__swarmPushWired) return;
  runtimeScope.__swarmPushWired = true;

  function _attachAutoContinue(convId) {
    if (!convId || typeof refreshConversationRuntime !== 'function') return;
    const conv = conversations.find((item) => item?.id === convId);
    if (conv) refreshConversationRuntime(convId);
  }

  function _cloneSwarmValue(value) {
    if (value == null) return value;
    if (typeof structuredClone === 'function') {
      try { return structuredClone(value); } catch (_ignored) { /* plain fallback */ }
    }
    return JSON.parse(JSON.stringify(value));
  }

  function _turnHasSwarm(turn) {
    return (turn?.projection?.toolRounds || []).some((round) =>
      round && (round._swarm || round.toolName === 'spawn_agents'));
  }

  function _swarmTurnCandidates(state) {
    const ids = Object.values(state?.laneOrder || {}).flatMap((lane) => lane || []);
    return [...new Set(ids)].reverse().map((turnId) => state.turnsById[turnId])
      .filter((turn) => turn
        && (turn.actor === 'assistant' || turn.actor === 'planner'));
  }

  function _findSwarmTurn(conv, frame) {
    const service = runtimeScope.ConversationTurnStore;
    const state = service?.ensureRuntimeStore?.(conv.id)?.getState?.();
    if (!state) return null;
    const candidates = _swarmTurnCandidates(state);
    for (const durable of candidates) {
      const overlay = runtimeScope.ConversationTransientTurns?.get?.(
        conv.id, durable.turnId,
      );
      if (_turnHasSwarm(overlay || durable)) return overlay || durable;
    }
    if (frame.type === 'swarm_phase'
        && ['planning', 'spawning', 'spawn_more'].includes(frame.phase)) {
      return candidates.find((turn) =>
        turn.status === 'running' || turn.status === 'completed') || null;
    }
    return null;
  }

  function _cloneSwarmTurn(turn) {
    const projection = _cloneSwarmValue(turn.projection || {});
    projection.toolRounds = Array.isArray(projection.toolRounds)
      ? projection.toolRounds : [];
    return {
      ...turn,
      projection,
      projectionRevision: Number(turn.projectionRevision || 0) + 1,
      updatedAt: Date.now(),
    };
  }

  function _syncSwarmSegmentRounds(projection) {
    if (!Array.isArray(projection.segments)
        || !Array.isArray(projection.toolRounds)) return;
    projection.segments = projection.segments.map((segment) => {
      if (!segment || segment.type !== 'tool_use') return segment;
      const round = projection.toolRounds.find((candidate) =>
        (segment.id && candidate?.toolCallId === segment.id)
        || (segment.llmRound != null
          && Number(candidate?.llmRound) === Number(segment.llmRound)));
      return round ? { ...segment, _round: round } : segment;
    });
  }

  function _settleSwarmOverlay(conv, turnId) {
    const service = runtimeScope.ConversationTurnStore;
    if (!service?.hydrateConversation) return;
    Promise.resolve(service.hydrateConversation(conv)).then(() => {
      runtimeScope.ConversationTransientTurns?.remove?.(conv, turnId);
    }).catch((error) => {
      console.warn(
        '[SwarmPush] authoritative hydration failed after terminal frame:',
        error?.message || error,
      );
    });
  }

  function _swarmPresentationCandidates(conv) {
    const service = runtimeScope.ConversationTurnStore;
    const state = service?.ensureRuntimeStore?.(conv?.id)?.getState?.();
    if (!state) return [];
    return _swarmTurnCandidates(state).map((durable) => (
      runtimeScope.ConversationTransientTurns?.get?.(conv.id, durable.turnId)
        || durable
    ));
  }

  function _updateSwarmPresentation(conv, turnId, updateProjection) {
    if (!conv || !turnId || typeof updateProjection !== 'function') return null;
    const service = runtimeScope.ConversationTurnStore;
    const state = service?.ensureRuntimeStore?.(conv.id)?.getState?.();
    const durable = state?.turnsById?.[turnId];
    const source = runtimeScope.ConversationTransientTurns?.get?.(
      conv.id, turnId,
    ) || durable;
    if (!source) return null;
    const overlay = _cloneSwarmTurn(source);
    const changed = updateProjection(overlay.projection, overlay) !== false;
    if (!changed) return null;
    _syncSwarmSegmentRounds(overlay.projection);
    runtimeScope.ConversationTransientTurns?.upsert?.(conv, overlay);
    return overlay;
  }

  /* Shared presentation seam for the push subscriber and the detached-panel
   * reconciler.  It deliberately exposes copied Turn updates rather than
   * copied projections: status probes are session telemetry and must never
   * mutate or persist the durable TurnStore read view. */
  runtimeScope.ConversationSwarmPresentation = Object.freeze({
    candidates: _swarmPresentationCandidates,
    update: _updateSwarmPresentation,
    settle: _settleSwarmOverlay,
  });

  pushSubscribe('swarm', '*', (frame) => {
    try {
      if (!frame?.type) return;
      const convId = frame.convId || frame.taskId;
      if (!convId || convId === '*') return;
      if (frame.type === 'swarm_autocontinue_started') {
        _attachAutoContinue(convId);
        return;
      }
      const conv = conversations.find((item) => item?.id === convId);
      if (!conv) return;
      const sourceTurn = _findSwarmTurn(conv, frame);
      if (!sourceTurn) {
        console.debug('[SwarmPush] no authoritative owning Turn for', frame.type);
        return;
      }
      const overlay = _updateSwarmPresentation(
        conv, sourceTurn.turnId, (projectedTurn) => {
          const context = {
            convId,
            taskId: convId,
            assistantProjection: projectedTurn,
          };
          if (frame.type === 'swarm_phase') {
            _handleSwarmPhase?.(frame, context);
          } else if ([
            'swarm_agent_phase',
            'swarm_agent_progress',
            'swarm_agent_complete',
            'swarm_agent_error',
            'swarm_agent_tool_call',
          ].includes(frame.type)) {
            _handleSwarmAgent?.(frame, context);
          } else {
            return false;
          }
          return true;
        },
      );
      if (!overlay) return;
      if (frame.type === 'swarm_phase'
          && (frame.phase === 'complete' || frame.phase === 'error')) {
        _settleSwarmOverlay(conv, overlay.turnId);
      }
    } catch (error) {
      console.debug('[SwarmPush] handler error:', error?.message || error);
    }
  });

  console.info('[SwarmPush] subscribed with transient Turn projection ownership');
})();
