/* SSE misc handlers (round-usage, artifact, compaction, memory-prefetch, project-edit, timer-poll) — extracted from ui/sse_pipeline.js dispatchSSEEvent (2026-06).
   Property-only handlers (no closure-local reassignment) taking (ev, c),
   a snapshot of the dispatch ctx. Bodies are byte-for-byte the originals.
   Concatenated by lib/js_bundler.py BEFORE ui/sse_pipeline.js.
   Behavior contract: tests/test_frontend_sse_dispatch.py. */

function _handleRoundUsage(ev, c) {
  const convId = c.convId, taskId = c.taskId;
  const assistantMsg = c.assistantMsg;
  const _epCriticPhase = c.epCriticPhase, _epCriticMsg = c.epCriticMsg;
      /* ── Per-round usage tick ──────────────────────────────────────────
       * The orchestrator emits this immediately after EACH LLM round
       * lands, carrying the raw usage dict + a pre-computed `tokensIn`
       * (input tokens including cache, Anthropic/OpenAI conventions
       * normalized server-side in lib/tasks_pkg/llm_fallback.py
       * :_emit_round_usage).
       *
       * Stash the latest reading on the in-flight assistant msg as
       * `_liveLastRoundUsage` so the context-health gauge reflects the
       * size of the prompt JUST sent to the model — without waiting
       * for the final `done` event to populate `apiRounds`.  This is
       * what makes the bar move on every tool round, not just at the
       * end of the user-visible turn.
       *
       * The reader is `static/js/context-bar.js:_lastUsageTokens`,
       * which prefers `_liveLastRoundUsage` over `apiRounds[-1]` and
       * falls back to `msg.usage / n` for older conversations. */
      if (assistantMsg) {
        assistantMsg._liveLastRoundUsage = {
          round: ev.roundNum,
          model: ev.model,
          tag: ev.tag,
          tokensIn: ev.tokensIn,
          tokensOut: ev.tokensOut,
          usage: ev.usage,
        };
      }
      if (typeof updateContextBar === 'function') updateContextBar();
      return false;

}

function _handleArtifact(ev, c) {
  const convId = c.convId, taskId = c.taskId;
  const assistantMsg = c.assistantMsg;
  const _epCriticPhase = c.epCriticPhase, _epCriticMsg = c.epCriticMsg;
      /* ── Renderable artifact (md/html/svg) — see lib/artifacts/ ───────
       * Producer A (write_file post-hook in lib/tasks_pkg/handlers/project.py)
       * persists the bytes server-side and emits this metadata-only event.
       * The actual content is fetched lazily via /api/artifacts/<id>/raw
       * when the user clicks the chip.
       *
       * We stash the meta on assistantMsg._artifacts so the chip survives
       * re-renders, and also into the global Artifacts cache so a click
       * after compaction (which strips toolRounds) still finds it. */
      if (typeof window.Artifacts !== "undefined" && window.Artifacts.attachToMessage) {
        try {
          window.Artifacts.attachToMessage(assistantMsg, {
            id:             ev.id,
            conv_id:        ev.conv_id || convId,
            task_id:        ev.task_id || taskId,
            msg_id:         ev.msg_id || (assistantMsg && assistantMsg._msgId) || "",
            source:         ev.source || "",
            source_ref:     ev.source_ref || {},
            format:         ev.format || "",
            title:          ev.title || "",
            size_bytes:     ev.size_bytes || 0,
            version:        ev.version || 1,
            parent_id:      ev.parent_id || "",
            pinned:         !!ev.pinned,
            created_at:     ev.created_at || 0,
            url:            ev.url || ("/api/v1/artifacts/" + (ev.id || "")),
          });
        } catch (e) {
          console.debug("[Artifacts] attachToMessage failed:", e);
        }
      }
      if (typeof twUpdate === 'function') twUpdate(convId);

}

function _handleCompaction(ev, c) {
  const convId = c.convId, taskId = c.taskId;
  const assistantMsg = c.assistantMsg;
  const _epCriticPhase = c.epCriticPhase, _epCriticMsg = c.epCriticMsg;
      /* ── Compaction marker ────────────────────────────────────────────
       * Emitted by lib/tasks_pkg/compaction.py when an archive row is
       * inserted (transcript_archive). Each marker becomes an inline
       * chip inside the assistant bubble; clicking it opens the right-
       * side Compaction Viewer drawer (see static/js/compaction-viewer.js)
       * which lazy-loads the pre-compaction message list.
       *
       * We store markers on the LIVE assistant message so they reappear
       * after re-render without a DB round-trip. On reload, the drawer
       * also pulls the authoritative list from
       * GET /api/conversations/<id>/compactions. */
      assistantMsg._compactions = assistantMsg._compactions || [];
      if (ev.type === "compaction") {
        const existing = assistantMsg._compactions.find(c => c.archiveId === ev.archiveId);
        if (!existing) {
          assistantMsg._compactions.push({
            archiveId:     ev.archiveId,
            convId:        ev.convId || convId,
            trigger:       ev.trigger || 'force',
            roundNum:      ev.roundNum || 0,
            tokensBefore:  ev.tokensBefore || 0,
            tokensAfter:   ev.tokensAfter || 0,
            msgsBefore:    ev.msgsBefore || 0,
            msgsAfter:     ev.msgsAfter || 0,
            model:         ev.model || '',
            reason:        ev.reason || '',
            ts:            ev.ts || Math.floor(Date.now() / 1000),
            status:        'in_progress',
          });
        }
      } else {
        // compaction_done — upgrade the matching marker with final numbers
        const marker = assistantMsg._compactions.find(c => c.archiveId === ev.archiveId);
        if (marker) {
          marker.tokensAfter = ev.tokensAfter || marker.tokensAfter;
          marker.msgsAfter   = ev.msgsAfter   || marker.msgsAfter;
          marker.reductionPct = ev.reductionPct;
          marker.status = 'done';
        }
      }
      /* Bind the gauge to the compaction event the moment it fires.
       * 'compaction' arrives before the LLM summary call and carries
       * tokensBefore — the new tick on the donut materializes here.
       * 'compaction_done' lands the final tokensAfter and we flash the
       * matching tick so the eye is drawn from chip → gauge in one beat. */
      if (typeof updateContextBar === 'function') updateContextBar();
      if (ev.type === 'compaction_done' && typeof window.flashGaugeForArchive === 'function') {
        window.flashGaugeForArchive(ev.archiveId);
      }
      if (typeof twUpdate === 'function') twUpdate(convId);

}

function _handleMemoryPrefetch(ev, c) {
  const convId = c.convId, taskId = c.taskId;
  const assistantMsg = c.assistantMsg;
  const _epCriticPhase = c.epCriticPhase, _epCriticMsg = c.epCriticMsg;
      /* ── Memory Prefetch indicator ────────────────────────────────────
       * Phases emitted by lib/memory/prefetch.py:
       *   started       — BM25 scoring about to run
       *   bm25_done     — coarse stage complete; cheap-LLM next
       *   rerank_started — cheap-model filter running
       *   done          — memories injected (or none picked)
       *   skipped       — no memories / empty query / bm25 empty
       *   failed        — unexpected error
       * We show a small chip inside the assistant bubble (above the tool panel)
       * so the user can see that a cheap model is filtering memories in the
       * background — otherwise the ~1-3s latency before the main model starts
       * producing tokens would feel unexplained.
       *
       * In ADDITION, we mirror the translate-pattern: while the cheap-model
       * filter is running we set conv._memoryPrefetching so the sidebar
       * shows a status dot + tag (parallel to conv._translating). */
      const prev = assistantMsg._memoryPrefetch || {};
      assistantMsg._memoryPrefetch = {
        ...prev,
        phase: ev.phase,
        totalMemories: ev.total_memories ?? prev.totalMemories,
        candidates: ev.candidates ?? prev.candidates,
        bm25Ms: ev.bm25_ms ?? prev.bm25Ms,
        rerankMs: ev.rerank_ms ?? prev.rerankMs,
        totalMs: ev.total_ms ?? prev.totalMs,
        selected: ev.selected ?? prev.selected,
        memories: ev.memories ?? prev.memories,
        reason: ev.reason ?? prev.reason,
        fellBack: ev.fell_back ?? prev.fellBack,
        startedAt: prev.startedAt || Date.now(),
      };

      // Sidebar status mirror — only the cheap-LLM running phases mark the
      // conversation as "filtering memories"; terminal phases clear it.
      const _conv = (typeof conversations !== 'undefined')
        ? conversations.find(c => c.id === convId) : null;
      if (_conv) {
        const RUNNING = new Set(['started', 'bm25_done', 'rerank_started']);
        const TERMINAL = new Set(['done', 'skipped', 'failed']);
        if (RUNNING.has(ev.phase)) {
          _conv._memoryPrefetching = true;
        } else if (TERMINAL.has(ev.phase)) {
          _conv._memoryPrefetching = false;
        }
        // Re-render sidebar so the dot/tag updates immediately.
        if (typeof renderConversationList === 'function') {
          renderConversationList();
        }
      }
      if (typeof twUpdate === 'function') twUpdate(convId);

}

function _handlePreferencesApplied(ev, c) {
  const convId = c.convId;
  const assistantMsg = c.assistantMsg;
      /* ── Preferences-applied indicator ────────────────────────────────
       * Emitted once at task start by the orchestrator when the bounded
       * personal-preference profile was injected onto the cache-safe
       * _isMeta tail (lib/tasks_pkg/system_context.py ★2.5). Drives the
       * quiet "preferences applied" chip so the user can SEE the assistant
       * is honouring their stored preferences. Payload: {chars, items}. */
      assistantMsg._preferencesApplied = {
        chars: ev.chars || 0,
        items: Array.isArray(ev.items) ? ev.items : [],
        core: Array.isArray(ev.core) ? ev.core : undefined,
        detail: Array.isArray(ev.detail) ? ev.detail : undefined,
      };
      if (typeof twUpdate === 'function') twUpdate(convId);

}

function _handleRelatedConversations(ev, c) {
  const convId = c.convId;
  const assistantMsg = c.assistantMsg;
      /* ── Related-conversations indicator ──────────────────────────────
       * Emitted once at task start by the orchestrator when the bounded
       * cross-conversation project digest was injected for ambient
       * awareness (lib/tasks_pkg/system_context.py ★4.4). Drives the quiet
       * "related conversations" provenance segment so the user can SEE — and
       * audit — the same sibling conversations the model was told about.
       * Payload: {count, items:[{id,title,summary}], toolsAvailable}. */
      assistantMsg._relatedConversations = {
        count: ev.count || 0,
        items: Array.isArray(ev.items) ? ev.items : [],
        toolsAvailable: !!ev.toolsAvailable,
      };
      if (typeof twUpdate === 'function') twUpdate(convId);

}

function _handlePreferenceLearned(ev, c) {
  const convId = c.convId;
  const assistantMsg = c.assistantMsg;
      /* ── Preference-learned moment ("Noted: you prefer X") ────────────
       * Emitted by the layer-3 consolidation pass (orchestrator) for each
       * reinforced / staged preference. We accumulate them on the assistant
       * message so the chip shows all learned items for this turn; a pending
       * (new) preference carries an id the Confirm/Dismiss buttons POST back
       * to /api/v1/profile/pending/<id>. */
      const list = assistantMsg._preferencesLearned || [];
      list.push({
        kind: ev.kind || 'pending',
        summary: ev.summary || '',
        pending: !!ev.pending,
        id: ev.id || '',
      });
      assistantMsg._preferencesLearned = list;
      if (typeof twUpdate === 'function') twUpdate(convId);

}

/* Confirm / dismiss a staged preference proposal (propose-then-confirm gate).
   Called from the inline buttons in renderPreferenceLearnedHtml. */
async function resolvePreference(btn, pendingId, accept) {
  try {
    const row = btn && btn.closest ? btn.closest('.pl-row') : null;
    if (row) { row.style.opacity = '0.5'; row.style.pointerEvents = 'none'; }
    await Api.post(`/api/v1/profile/pending/${encodeURIComponent(pendingId)}`,
                   { accept: !!accept });
    if (row) {
      const _t = (typeof t === 'function') ? t : (k => k);
      row.innerHTML = `<span class="pl-lead">${Icon(accept ? 'check' : 'x', 13)}</span>` +
        `<span class="pl-text">${accept ? _t('prefs.learnedReinforced') : _t('prefs.dismiss')}</span>`;
      row.classList.add('pl-resolved');
      row.style.opacity = '';
    }
  } catch (e) {
    console.warn('[resolvePreference] failed', e);
    if (typeof showToast === 'function') showToast('⚠️', 'Error', String(e), 4000);
    const row = btn && btn.closest ? btn.closest('.pl-row') : null;
    if (row) { row.style.opacity = ''; row.style.pointerEvents = ''; }
  }
}
window.resolvePreference = resolvePreference;

/* Resolve a conversation's display title from its id, with a graceful fallback.
   Used by background-event toasts to name the SOURCE conversation — critical
   when the event fires from a conv that is NOT the one on screen. */
function _toastConvTitle(convId) {
  const _t = (typeof t === 'function') ? t : (k => k);
  if (!convId) return '';
  try {
    const conv = (typeof conversations !== 'undefined')
      ? conversations.find(x => x.id === convId) : null;
    if (conv && conv.title) return conv.title;
  } catch (e) { console.debug('[toast] conv title lookup failed', e); }
  return _t('toast.untitledConv');
}

function _handleProjectExternalEdit(ev, c) {
  const convId = c.convId, taskId = c.taskId;
  const assistantMsg = c.assistantMsg;
  const _epCriticPhase = c.epCriticPhase, _epCriticMsg = c.epCriticMsg;
      // ★ Git-shim: external edits (an IDE / another tool changed tracked
      //   files outside a Tofu round). Tofu has AUTO-SNAPSHOTTED them into
      //   file-history so the next round's diff stays clean and the edits are
      //   revertible. The old toast was English-only, emoji-prefixed, named no
      //   conversation, and gave no next step. Make it conversation-aware and
      //   actionable: say WHERE it came from and WHAT the user can do.
      const files = ev.files || [];
      const sha = (ev.sha || '').slice(0, 7);
      try {
        if (typeof showToast === 'function') {
          const _t = (typeof t === 'function') ? t : (k => k);
          const n = files.length;
          const preview = files.slice(0, 3).join(', ')
            + (n > 3 ? ' ' + _t('externalEdit.moreN', { n: n - 3 }) : '');
          const title = _t('externalEdit.title', { n, s: n > 1 ? 's' : '' });
          const detail = sha ? _t('externalEdit.detail', { preview, sha })
                             : preview;
          // Full form (icon, title, detail, dur, opts). Icon is empty — the
          // typed 'info' circle is inferred from the neutral title text.
          showToast('', title, detail, 7000, {
            convId,
            convTitle: _toastConvTitle(convId),
            hint: _t('externalEdit.hint'),
          });
        }
      } catch (e) { console.warn('[project_external_edit] toast failed', e); }
      console.log('[project_external_edit]', { convId, sha, files });

}

function _handleWorkspaceRootAdded(ev, c) {
      /* ── Silent workspace-root auto-registration, now visible ─────────
       * Emitted by the project tool handler (lib/tasks_pkg/handlers/project.js)
       * when an absolute-path write outside all roots auto-registered the
       * nearest existing ancestor as a NEW extra workspace root
       * (lib/project_mod/write_tools.py _resolve_write_path §2). This used
       * to expand the workspace invisibly — no tool round, only an app.log
       * line — which is the exact surprise users hit ("it started writing
       * to project X and nothing showed it was added"). Surface a brief
       * toast naming the added root(s). Payload: {roots: [{rootName, path}]}. */
      const roots = Array.isArray(ev.roots) ? ev.roots : [];
      if (!roots.length) return;
      const convId = c && c.convId;
      try {
        if (typeof showToast === 'function') {
          const _t = (typeof t === 'function') ? t : (k => k);
          const names = roots.map(r => r.rootName || r.path || '?');
          const preview = names.slice(0, 3).join(', ')
            + (names.length > 3 ? ' ' + _t('externalEdit.moreN', { n: names.length - 3 }) : '');
          const msg = _t('workspaceRoot.added', { roots: preview });
          // Full form so we can attach the source-conversation badge + a hint
          // explaining that the assistant's write auto-expanded the workspace.
          showToast('', msg, '', 7000, {
            convId,
            convTitle: _toastConvTitle(convId),
            hint: _t('workspaceRoot.hint'),
          });
        }
      } catch (e) { console.warn('[workspace_root_added] toast failed', e); }
      console.log('[workspace_root_added]', { convId, roots });

      /* ── EPHEMERAL bar paint ONLY — provenance split ──────────────────
       * An absolute-path write auto-registered a NEW extra root. This is an
       * INCIDENTAL expansion of the workspace (a side effect of a write to a
       * path outside all roots), NOT an explicit workspace choice the user
       * made. So it may light up the bar for the CURRENT page load, but it
       * must NEVER be written into the DURABLE conv.projectPaths / synced to
       * the server. Persisting it was the "comes back after I delete it" bug:
       * the durable record got the root, so the next reload's
       * _restoreConvProject faithfully repainted exactly the root the user
       * had just removed.
       *
       * Only EXPLICIT additions (create_project, the project modal, picking a
       * folder) persist to conv.projectPaths — they go through their own
       * paths and thus survive a reload. Incidental auto-registers evaporate
       * on reload, as they should.
       *
       * GATE on the emitting conv being the ACTIVE one: the global project
       * _state a background task's write mutated may reflect a DIFFERENT
       * conversation's project, so refreshing projectState from an inactive
       * conv would apply the wrong workspace to the visible bar. */
      try {
        const _active = (typeof activeConvId !== 'undefined') ? activeConvId : null;
        if (convId && _active && convId === _active
            && typeof Api !== 'undefined' && Api.project
            && typeof Api.project.status === 'function') {
          Api.project.status(convId)
            .then(data => {
              if (!data) return;
              // Ephemeral: paint the bar so the new root is visible THIS
              // session. Deliberately do NOT touch conv.projectPath /
              // conv.projectPaths and do NOT persist/sync — see above.
              if (typeof _applyProjectData === 'function') _applyProjectData(data);
            })
            .catch(e => { console.warn('[workspace_root_added] status refresh failed', e); });
        }
      } catch (e) { console.warn('[workspace_root_added] ephemeral paint failed', e); }

}

function _handleTimerPollCheck(ev, c) {
  const convId = c.convId, taskId = c.taskId;
  const assistantMsg = c.assistantMsg;
  const _epCriticPhase = c.epCriticPhase, _epCriticMsg = c.epCriticMsg;
      /* ═══ Timer Watcher inline poll progress ═══
         Each poll emits a sub-event attached to the timer_create tool round.
         We store polls as _timerPolls[] on the round for collapsible rendering.
         ★ decision='skipped' is a lightweight heartbeat for polls where
           the check_command output was unchanged — we don't push it into
           _timerPolls[] (would spam), just bump skip metadata so the UI
           can render a subdued "N skipped — output unchanged" trailer. */
      if (assistantMsg.toolRounds) {
        const r = (ev.toolCallId
          ? assistantMsg.toolRounds.find(r => r.toolCallId === ev.toolCallId)
          : null
        ) || assistantMsg.toolRounds.find(r => r.roundNum === ev.roundNum);
        if (r) {
          r._timerTimerId = ev.timerId;
          // Capture the next-poll timestamp so the UI can render a countdown.
          if (ev.nextPollTs) r._timerNextPollTs = ev.nextPollTs;
          // Remember the model the poll LLM resolved to (shown in the header).
          if (ev.model) r._timerModel = ev.model;
          // The 'started' event carries the verification metadata (what is
          // being checked + how). Stash it on the round so the panel header
          // and detail can explain the timer to the user.
          if (ev.decision === "started") {
            if (ev.checkInstruction) r._timerCheckInstruction = ev.checkInstruction;
            if (ev.checkCommand) r._timerCheckCommand = ev.checkCommand;
            if (ev.pollInterval) r._timerPollInterval = ev.pollInterval;
            if (ev.maxPolls) r._timerMaxPolls = ev.maxPolls;
          }
          if (ev.decision === "skipped") {
            r._timerSkipCount = (r._timerSkipCount || 0) + 1;
            r._timerLastSkipTs = Date.now();
            r._timerLastSkipPollNum = ev.pollNum;
            // Keep the round in "searching" state while timer is polling
            r.status = "searching";
          } else {
            if (!r._timerPolls) r._timerPolls = [];
            // ★ Dedup: skip if this pollNum already exists (from state snapshot)
            const _alreadyHas = r._timerPolls.some(p => p.pollNum === ev.pollNum && p.decision === ev.decision);
            if (!_alreadyHas) {
              r._timerPolls.push({
                pollNum: ev.pollNum,
                pollId: ev.pollId || "",
                decision: ev.decision,
                reason: ev.reason || "",
                rawContent: ev.rawContent || "",
                tokensUsed: ev.tokensUsed || 0,
                timerId: ev.timerId || "",
                cmdOutput: ev.cmdOutput || "",
                parseError: !!ev.parseError,
                model: ev.model || "",
                toolTrace: Array.isArray(ev.toolTrace) ? ev.toolTrace : [],
                ts: Date.now(),
              });
            }
            // Keep the round in "searching" state while timer is polling
            if (ev.decision === "ready") {
              r.status = "done";
              r._timerTriggered = true;
            } else {
              r.status = "searching";
            }
          }
        }
      }
      if (typeof twUpdate === 'function') twUpdate(convId);

    /* ═══ Swarm mode events ═══ */
}
