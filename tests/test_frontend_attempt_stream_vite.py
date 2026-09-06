"""Real-browser contracts for typed turn state and conversation sync."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.visual
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def install_native_test_owners(page):
    """Exercise typed owners directly; the public bridge no longer exports internals."""
    sources = (
        ('conversation-sync-browser.js', 'frontend/src/core/conversation-sync.ts'),
        ('send-startup-browser.js', 'frontend/src/core/send-startup.ts'),
        ('turn-projection-browser.js', 'frontend/src/core/turn-projection.ts'),
        ('turn-runtime-browser.js', 'frontend/src/core/turn-runtime.ts'),
        ('turn-state-browser.js', 'frontend/src/core/turn-state.ts'),
        ('turn-presentation-browser.js', 'frontend/src/core/turn-presentation.ts'),
        ('lifecycle-browser.js', 'frontend/src/lifecycle.ts'),
    )
    for name, source in sources:
        page.add_script_tag(path=native_module_path(name, source))


def test_runtime_store_is_published_before_reentrant_initial_health(
        page, assert_no_js_errors):
    page.wait_for_function(
        "typeof window.createConversationTurnRuntime === 'function'",
        timeout=30_000,
    )

    result = page.evaluate(r"""
    async () => {
      let runtime;
      let idleHealthCount = 0;
      let storeReadFromHealth = null;
      let hydrateFromHealth = null;
      let snapshotCount = 0;
      runtime = window.createConversationTurnRuntime({
        api: {
          async snapshot(conversationId) {
            snapshotCount += 1;
            return {
              ok: true,
              contract: 'tofu.conversation-sync.snapshot/v1',
              conversationId,
              conversationRevision: 1,
              syncSeq: 1,
              cursor: 'cursor-1',
              serverBootId: 'boot-test',
              heartbeatIntervalMs: 15000,
              settings: {},
              turns: [],
              attempts: [],
            };
          },
          eventsUrl() { return '/unused'; },
        },
        onHealth(conversationId, health) {
          if (health.state !== 'idle' || idleHealthCount > 0) return;
          idleHealthCount += 1;
          storeReadFromHealth = runtime.ensureRuntimeStore(conversationId);
          hydrateFromHealth = runtime.hydrateConversation({
            id: conversationId,
            createdAt: 1,
          });
        },
      });
      const store = runtime.ensureRuntimeStore('conv-reentrant-health');
      await hydrateFromHealth;
      return {
        idleHealthCount,
        reusedPublishedStore: storeReadFromHealth === store,
        snapshotCount,
      };
    }
    """)

    assert result == {
        'idleHealthCount': 1,
        'reusedPublishedStore': True,
        'snapshotCount': 1,
    }


def test_snapshot_flight_is_claimed_before_reentrant_health_callback(
        page, assert_no_js_errors):
    page.wait_for_function(
        "typeof window.ConversationSyncCoordinator === 'function'",
        timeout=30_000,
    )

    result = page.evaluate(r"""
    async () => {
      let releaseSnapshot;
      const snapshotGate = new Promise(resolve => { releaseSnapshot = resolve; });
      let snapshotCount = 0;
      let projectionCount = 0;
      let didReenter = false;
      let reentrantHydrate = null;
      let coordinator;
      coordinator = new window.ConversationSyncCoordinator({
        conversationId: 'conv-reentrant-snapshot',
        api: {
          async snapshot(conversationId) {
            snapshotCount += 1;
            await snapshotGate;
            return {
              ok: true,
              contract: 'tofu.conversation-sync.snapshot/v1',
              conversationId,
              conversationRevision: 1,
              syncSeq: 1,
              cursor: 'cursor-1',
              serverBootId: 'boot-test',
              heartbeatIntervalMs: 15000,
              settings: {},
              turns: [],
              attempts: [],
            };
          },
          eventsUrl() { return '/unused'; },
        },
        onSnapshot() { projectionCount += 1; },
        onAttemptEvent() { return true; },
        onTurnDelta() { return true; },
        onHealth(_conversationId, health) {
          if (health.state !== 'connecting' || didReenter) return;
          didReenter = true;
          reentrantHydrate = coordinator.hydrate(false);
        },
      });

      const initialHydrate = coordinator.hydrate(false);
      releaseSnapshot();
      await Promise.all([initialHydrate, reentrantHydrate]);
      coordinator.close();
      return { didReenter, snapshotCount, projectionCount };
    }
    """)

    assert result == {
        'didReenter': True,
        'snapshotCount': 1,
        'projectionCount': 1,
    }


def test_snapshot_tool_segment_references_materialize_before_publication(
        page, assert_no_js_errors):
    page.wait_for_function(
        "typeof window.ConversationSyncCoordinator === 'function'",
        timeout=30_000,
    )

    result = page.evaluate(r"""
    async () => {
      const largeContent = 'result-'.repeat(10_000);
      const toolArgs = { query: 'bounded reference' };
      const roundResult = { content: largeContent, status: 'done' };
      const round = {
        toolCallId: 'call-ref-a', toolName: 'research', toolArgs,
        toolContent: largeContent, status: 'done', result: roundResult,
      };
      const wireSegment = {
        type: 'tool_use', blockId: 'tool:call-ref-a', id: 'call-ref-a',
        name: 'research', result: {}, roundRef: 'call-ref-a',
      };
      const wireSnapshot = {
        ok: true,
        contract: 'tofu.conversation-sync.snapshot/v1',
        conversationId: 'conv-reference-segments',
        conversationRevision: 1,
        syncSeq: 1,
        cursor: 'cursor-1',
        serverBootId: 'boot-test',
        heartbeatIntervalMs: 15000,
        settings: {},
        turns: [{
          turnId: 'turn-a', conversationId: 'conv-reference-segments',
          laneId: 'main', ordinal: 1, actor: 'assistant', kind: 'reply',
          runId: 'run-a', status: 'completed', currentAttemptId: null,
          projectionRevision: 1, projection: {
            content: 'done', segments: [wireSegment], toolRounds: [round],
          },
        }],
        attempts: [], queueItems: [],
      };
      let published = null;
      const coordinator = new window.ConversationSyncCoordinator({
        conversationId: wireSnapshot.conversationId,
        api: {
          async snapshot() { return wireSnapshot; },
          eventsUrl() { return '/unused'; },
        },
        onSnapshot(snapshot) { published = snapshot; },
        onAttemptEvent() { return true; },
        onTurnDelta() { return true; },
      });
      const returned = await coordinator.hydrate(false);
      coordinator.close();
      const segment = published.turns[0].projection.segments[0];
      return {
        returnedIsMaterialized: returned === published,
        copiedSnapshot: published !== wireSnapshot,
        sameRound: segment._round === round,
        sameInput: segment.input === toolArgs,
        sameResult: segment.result === roundResult,
        contentLength: segment.result.content.length,
        wireUntouched: wireSegment.result !== roundResult
          && !('input' in wireSegment) && !('_round' in wireSegment),
      };
    }
    """)

    assert result == {
        'returnedIsMaterialized': False,
        'copiedSnapshot': True,
        'sameRound': True,
        'sameInput': True,
        'sameResult': True,
        'contentLength': 70_000,
        'wireUntouched': True,
    }


def test_conversation_sync_owns_cursor_ordering_and_snapshot_recovery(
        page, assert_no_js_errors):
    page.wait_for_function(
        "typeof window.ConversationSyncCoordinator === 'function'",
        timeout=30_000,
    )

    result = page.evaluate(r"""
    async () => {
      class FakeEventSource extends EventTarget {
        constructor(url) {
          super();
          this.url = url;
          this.closed = false;
          this.onopen = null;
          this.onmessage = null;
          this.onerror = null;
        }
        close() { this.closed = true; }
        emit(name, body, lastEventId = '') {
          this.dispatchEvent(new MessageEvent(name, {
            data: JSON.stringify(body),
            lastEventId,
          }));
        }
      }

      const healthStates = [];
      const events = [];
      const eventReceipts = [];
      const deltas = [];
      const snapshots = [];
      const protocolErrors = [];
      const sources = [];
      let snapshotCount = 0;
      const api = {
        async snapshot(conversationId) {
          snapshotCount += 1;
          const syncSeq = snapshotCount === 1 ? 2 : 10;
          return {
            ok: true,
            contract: 'tofu.conversation-sync.snapshot/v1',
            conversationId,
            conversationRevision: syncSeq,
            syncSeq,
            cursor: `cursor-${syncSeq}`,
            serverBootId: 'boot-test',
            heartbeatIntervalMs: 15000,
            settings: {},
            turns: [], attempts: [],
          };
        },
        eventsUrl(conversationId, after) {
          return `/conversations/${conversationId}/events?after=${after}`;
        },
      };
      const coordinator = new window.ConversationSyncCoordinator({
        conversationId: 'conv-a',
        api,
        eventSourceFactory(url) {
          const source = new FakeEventSource(url);
          sources.push(source);
          return source;
        },
        onSnapshot(snapshot) { snapshots.push(snapshot); },
        onAttemptEvent(event, receivedAt, serverPublishedAt) {
          events.push(event);
          eventReceipts.push({ receivedAt, serverPublishedAt });
          return true;
        },
        onTurnDelta(delta) { deltas.push(delta); return true; },
        onHealth(_conversationId, health) { healthStates.push(health.state); },
        onProtocolError(error) { protocolErrors.push(error.message); },
      });

      await coordinator.hydrate(true);
      const first = sources[0];
      first.onopen();
      first.emit('attempt.event', {
        contract: 'tofu.conversation-sync.event/v1',
        type: 'attempt.event', conversationId: 'conv-a', syncSeq: 3,
        occurredAt: 3, turnId: 'turn-a', attemptId: 'attempt-a',
        payload: { event: {
          conversationId: 'conv-a', turnId: 'turn-a', attemptId: 'attempt-a',
          seq: 1, projectionRevision: 1, type: 'projection_updated',
          payload: { projection: { content: 'hello' } },
        } },
      }, 'cursor-3');
      first.emit('turn.patch', {
        contract: 'tofu.conversation-sync.event/v1',
        type: 'turn.patch', conversationId: 'conv-a', syncSeq: 4,
        occurredAt: 4, turnId: 'turn-a', payload: {
          conversationRevision: 4,
          turnPatches: [{
            turnId: 'turn-a', baseProjectionRevision: 1,
            targetProjectionRevision: 2, updatedAt: 4,
            projectionPatch: {
              version: 1, baseRevision: 1, targetRevision: 2,
              operations: [{ op: 'append_text', path: ['content'], value: '!' }],
            },
          }],
        },
      }, 'cursor-4');
      // Sequence 6 skips 5. The coordinator must close the stale pipe,
      // recover one authoritative snapshot, and resume from its opaque cursor.
      first.emit('turn.upsert', {
        contract: 'tofu.conversation-sync.event/v1',
        type: 'turn.upsert', conversationId: 'conv-a', syncSeq: 6,
        occurredAt: 6, payload: { turns: [] },
      }, 'cursor-6');
      await new Promise(resolve => setTimeout(resolve, 0));
      await new Promise(resolve => setTimeout(resolve, 0));
      const second = sources[1];
      coordinator.close();

      return {
        firstUrl: first.url,
        secondUrl: second.url,
        eventTypes: events.map(event => event.type),
        receiptPublishedAt: eventReceipts[0]?.serverPublishedAt,
        receiptHasBrowserClock: Number.isFinite(eventReceipts[0]?.receivedAt)
          && eventReceipts[0].receivedAt > 1_000_000,
        deltaPatchTargets: deltas.flatMap(delta => delta.turnPatches || [])
          .map(change => change.targetProjectionRevision),
        cursor: coordinator.cursor,
        snapshotCount: snapshots.length,
        protocolErrorCount: protocolErrors.length,
        firstClosed: first.closed,
        secondClosed: second.closed,
        finalHealth: healthStates[healthStates.length - 1],
      };
    }
    """)

    assert result == {
        'firstUrl': '/conversations/conv-a/events?after=cursor-2',
        'secondUrl': '/conversations/conv-a/events?after=cursor-10',
        'eventTypes': ['projection_updated'],
        'receiptPublishedAt': 3,
        'receiptHasBrowserClock': True,
        'deltaPatchTargets': [2],
        'cursor': 'cursor-10',
        'snapshotCount': 2,
        'protocolErrorCount': 0,
        'firstClosed': True,
        'secondClosed': True,
        'finalHealth': 'closed',
    }


def test_send_startup_lease_owns_timeout_stop_and_race_cleanup(
        page, assert_no_js_errors):
    page.wait_for_function(
        "typeof window.createSendStartupLease === 'function'",
        timeout=30_000,
    )

    result = page.evaluate(r"""
    async () => {
      const timeoutOwner = {};
      const timeoutLease = window.createSendStartupLease(
        timeoutOwner, { timeoutMs: 5 });
      const timeoutWasOwned = timeoutOwner._genStartCtrl === timeoutLease.controller;
      await new Promise(resolve => setTimeout(resolve, 20));
      const timedOut = timeoutLease.signal.aborted && timeoutLease.reason === 'timeout';
      timeoutLease.finish();

      const stopOwner = {};
      const stopLease = window.createSendStartupLease(
        stopOwner, { timeoutMs: 0 });
      stopOwner._genStartStop = stopLease.controller;
      stopOwner._genStartCtrl = null;
      stopLease.controller.abort();
      const userStopped = stopLease.stoppedByUser()
        && stopLease.reason === 'user-stop';
      stopLease.finish();

      const raceOwner = {};
      const older = window.createSendStartupLease(
        raceOwner, { timeoutMs: 0 });
      const newer = window.createSendStartupLease(
        raceOwner, { timeoutMs: 0 });
      older.finish();
      const newerSurvived = raceOwner._genStartCtrl === newer.controller;
      newer.abort('unmount');
      const explicitReason = newer.reason;
      newer.finish();

      return {
        timeoutWasOwned,
        timedOut,
        timeoutCleared: timeoutOwner._genStartCtrl === null
          && timeoutOwner._genStartStop === null,
        userStopped,
        stopCleared: stopOwner._genStartCtrl === null
          && stopOwner._genStartStop === null,
        newerSurvived,
        explicitReason,
        raceCleared: raceOwner._genStartCtrl === null
          && raceOwner._genStartStop === null,
      };
    }
    """)

    assert result == {
        'timeoutWasOwned': True,
        'timedOut': True,
        'timeoutCleared': True,
        'userStopped': True,
        'stopCleared': True,
        'newerSurvived': True,
        'explicitReason': 'unmount',
        'raceCleared': True,
    }


def test_browser_turn_reads_are_ordered_and_attempt_aware(
        page, assert_no_js_errors):
    page.wait_for_function(
        "typeof window.ConversationTurnRead?.ordered === 'function'",
        timeout=30_000,
    )

    result = page.evaluate(r"""
    () => {
      const conversationId = 'browser-read-contract';
      const store = window.ConversationTurnStore.ensureRuntimeStore(conversationId);
      store._snapshotLoaded = true;
      store.dispatch({type:'snapshot', snapshot:{conversationRevision:7, turns:[
          {
            turnId:'human-1', conversationId, actor:'human', kind:'input',
            laneId:'main', ordinal:1, status:'completed',
            projectionRevision:1, createdAt:10,
            projection:{ content:'question', _branchLanes:[{
              laneId:'lane-b', kind:'branch', anchorText:'selection',
            }] }, settlement:{outcome:'completed'},
          },
          {
            turnId:'assistant-1', conversationId, actor:'assistant', kind:'reply',
            laneId:'main', ordinal:2,
            status:'running', currentAttemptId:'attempt-main',
            projectionRevision:2, createdAt:20,
            projection:{ content:'answer' },
          },
          {
            turnId:'branch-1', conversationId, actor:'critic', kind:'reply',
            laneId:'lane-b', ordinal:1,
            parentTurnId:'human-1', status:'pending',
            currentAttemptId:'attempt-branch', projectionRevision:3,
            projection:{ content:'branch answer' },
          },
        ], attempts:[], queueItems:[]}});
      const main = window.ConversationTurnRead.ordered(conversationId);
      const branch = window.ConversationTurnRead.ordered(conversationId, 'lane-b');
      const output = {
        legacyProjectionGlobal:typeof window.projectTurnState,
        orderedIds:main.map(turn => turn.turnId),
        actors:main.map(turn => turn.actor),
        branchIds:branch.map(turn => turn.turnId),
        activeAttemptIds:[...window.ConversationTurnRead.activeAttemptIds(
          conversationId)].sort(),
        activeMainAttemptId:window.ConversationTurnRead.activeMainAttemptId(
          conversationId),
      };
      window.ConversationTurnStore.disposeConversation(conversationId);
      return output;
    }
    """)

    assert result == {
        'legacyProjectionGlobal': 'undefined',
        'orderedIds': ['human-1', 'assistant-1'],
        'actors': ['human', 'assistant'],
        'branchIds': ['branch-1'],
        'activeAttemptIds': ['attempt-branch', 'attempt-main'],
        'activeMainAttemptId': 'attempt-main',
    }


def test_native_turn_runtime_owns_hydrate_submit_stream_and_projection(
        page, assert_no_js_errors):
    page.wait_for_function(
        "typeof window.createConversationTurnRuntime === 'function'",
        timeout=30_000,
    )

    result = page.evaluate(r"""
    async () => {
      class FakeEventSource extends EventTarget {
        constructor(url) {
          super();
          this.url = url;
          this.closed = false;
          this.onopen = null;
          this.onmessage = null;
          this.onerror = null;
        }
        close() { this.closed = true; }
        emit(name, body, lastEventId = '') {
          this.dispatchEvent(new MessageEvent(name, {
            data: JSON.stringify(body),
            lastEventId,
          }));
        }
      }

      const sources = [];
      const submitted = [];
      const persisted = [];
      const renderedStates = [];
      const settledTurns = [];
      let legacyRepaints = 0;
      const api = {
        async snapshot(conversationId) {
          return {
            ok: true,
            contract: 'tofu.conversation-sync.snapshot/v1',
            conversationId,
            conversationRevision: 1,
            syncSeq: 1,
            cursor: 'cursor-1',
            serverBootId: 'boot-test',
            heartbeatIntervalMs: 15000,
            settings: {projectPath:'/workspace/current'},
            attempts: [],
            turns: [{
              conversationId, turnId:'human-1', laneId:'main', ordinal:0,
              actor:'human', kind:'input', status:'completed',
              projectionRevision:1, projection:{content:'hello'},
            }],
          };
        },
        async createTurn(conversationId, payload, requestOptions) {
          submitted.push({conversationId, payload, requestOptions});
          return {
            ok: true,
            conversationId,
            conversationRevision: 2,
            turn: {
              conversationId, turnId:'assistant-1', laneId:'main', ordinal:1,
              actor:'assistant', kind:'reply', status:'running',
              currentAttemptId:'attempt-1', projectionRevision:1,
              projection:{content:'starting'},
            },
            attempt: {
              attemptId:'attempt-1', turnId:'assistant-1', status:'running',
            },
          };
        },
        async createAttempt() { throw Error('not used'); },
        eventsUrl(conversationId, after) {
          return `/conversations/${conversationId}/events?after=${after}`;
        },
        async updateTurn() { throw Error('not used'); },
        async createLane() { throw Error('not used'); },
        async deleteLane() { throw Error('not used'); },
        async deleteTurns() { throw Error('not used'); },
        async abortAttempt(attemptId) { return {attemptId}; },
      };
      const runtime = window.createConversationTurnRuntime({
        api,
        eventSourceFactory(url) {
          const source = new FakeEventSource(url);
          sources.push(source);
          return source;
        },
        persist(conv) {
          persisted.push({
            revision: conv._serverRev,
            turnCount: conv._serverTurnCount,
          });
        },
        applySettings(conv, settings) { Object.assign(conv, settings); },
        isActive() { return true; },
        renderState(_conv, state) {
          renderedStates.push(Object.values(state.turnsById).map(turn => turn?.status));
          return true;
        },
        onTurnSettled(_conv, turn) {
          settledTurns.push(`${turn.turnId}:${turn.status}`);
        },
        replaceAll() { legacyRepaints += 1; },
      });
      const signal = new AbortController().signal;
      const conv = {id:'conv-1', title:'Typed', createdAt:10};

      await runtime.hydrateConversation(conv);
      await runtime.submitConversation(conv, 'question', {model:'test'}, {
        commandId:'cmd-1', settings:{search:true},
        requestOptions:{signal, headers:{'Idempotency-Key':'cmd-1'}},
      });
      const source = sources[0];
      source.emit('attempt.event', {
        contract:'tofu.conversation-sync.event/v1',
        type:'attempt.event', conversationId:'conv-1', syncSeq:2,
        occurredAt:2, turnId:'assistant-1', attemptId:'attempt-1',
        payload:{event:{
          conversationId:'conv-1', turnId:'assistant-1',
          attemptId:'attempt-1', seq:1, projectionRevision:2,
          type:'projection_updated',
          payload:{projection:{content:'typed stream'}},
        }},
      }, 'cursor-2');
      source.emit('attempt.event', {
        contract:'tofu.conversation-sync.event/v1',
        type:'attempt.event', conversationId:'conv-1', syncSeq:3,
        occurredAt:3, turnId:'assistant-1', attemptId:'attempt-1',
        payload:{event:{
          conversationId:'conv-1', turnId:'assistant-1',
          attemptId:'attempt-1', seq:2, projectionRevision:3,
          type:'terminal_settlement',
          payload:{status:'completed', settlement:{cause:'stop'},
            projection:{content:'typed done'}},
        }},
      }, 'cursor-3');

      const renderStateSawTerminal = renderedStates.some(statuses =>
        statuses.includes('completed') && statuses.length > 1);
      const finalState = runtime.ensureRuntimeStore('conv-1').getState();
      const finalTurns = finalState.laneOrder.main.map(
        turnId => finalState.turnsById[turnId],
      );
      runtime.disposeConversation('conv-1');

      return {
        runtimeOwner: runtime.emptyState('native-owner').conversationId === 'native-owner'
          && typeof window.ConversationTurnStore?.hydrateConversation === 'function',
        markerFree: !Object.prototype.hasOwnProperty.call(conv, '_turnNative'),
        transcriptFree: !Object.prototype.hasOwnProperty.call(conv, 'messages'),
        projectPath: conv.projectPath,
        payloadCommandId: submitted[0].payload.commandId,
        payloadMessage: submitted[0].payload.message,
        payloadKeys: Object.keys(submitted[0].payload).sort(),
        payloadHasTopLevelSettings: Object.hasOwn(submitted[0].payload, 'settings'),
        payloadHasRequestOptions: Object.hasOwn(submitted[0].payload, 'requestOptions'),
        nestedSettings: submitted[0].payload.conversation.settings,
        nestedTitle: submitted[0].payload.conversation.title,
        requestSignalPreserved: submitted[0].requestOptions.signal === signal,
        sourceUrl: source.url,
        sourceClosed: source.closed,
        turnIds: finalTurns.map(turn => turn.turnId),
        turnContents: finalTurns.map(turn => turn.projection.content),
        status: finalTurns[1].status,
        activeAttemptIds: finalTurns
          .filter(turn => turn.status === 'pending' || turn.status === 'running')
          .map(turn => turn.currentAttemptId),
        persistedCount: persisted.length,
        renderStateSawTerminal,
        settledTurns,
        legacyRepaints,
      };
    }
    """)

    assert result == {
        'runtimeOwner': True,
        'markerFree': True,
        'transcriptFree': True,
        'projectPath': '/workspace/current',
        'payloadCommandId': 'cmd-1',
        'payloadMessage': 'question',
        'payloadKeys': [
            'commandId', 'config', 'conversation', 'inputTurn', 'message',
        ],
        'payloadHasTopLevelSettings': False,
        'payloadHasRequestOptions': False,
        'nestedSettings': {'search': True},
        'nestedTitle': 'Typed',
        'requestSignalPreserved': True,
        'sourceUrl': '/conversations/conv-1/events?after=cursor-1',
        # This harness has no active-view callback; once its only turn settles,
        # the runtime pauses the conversation stream until the view resumes it.
        'sourceClosed': True,
        'turnIds': ['human-1', 'assistant-1'],
        'turnContents': ['hello', 'typed done'],
        'status': 'completed',
        'activeAttemptIds': [],
        # Persist shell invalidation metadata only: hydrate, submit, projection
        # and settlement. Turn content remains normalized in TurnStore.
        'persistedCount': 4,
        'renderStateSawTerminal': True,
        'settledTurns': ['assistant-1:completed'],
        'legacyRepaints': 0,
    }


def test_native_turn_runtime_does_not_probe_an_unsaved_conversation(
        page, assert_no_js_errors):
    page.wait_for_function(
        "typeof window.createConversationTurnRuntime === 'function'",
        timeout=30_000,
    )

    result = page.evaluate(r"""
    async () => {
      let snapshotCalls = 0;
      let submitCalls = 0;
      let submittedTitle = null;
      const runtime = window.createConversationTurnRuntime({
        api: {
          async snapshot() {
            snapshotCalls += 1;
            throw Error('must not probe');
          },
          async createTurn(conversationId, payload) {
            submitCalls += 1;
            submittedTitle = payload.conversation.title;
            return {
              ok: true,
              conversationId,
              conversationRevision: 1,
              turn: {
                conversationId, turnId:'assistant-local', laneId:'main', ordinal:1,
                actor:'assistant', kind:'reply', status:'completed',
                projectionRevision:1, projection:{content:'done'},
              },
              attempt: {attemptId:'', turnId:'assistant-local', status:'completed'},
            };
          },
          async createAttempt() { throw Error('not used'); },
          eventsUrl() { throw Error('not used'); },
          async updateTurn() { throw Error('not used'); },
          async createLane() { throw Error('not used'); },
          async deleteLane() { throw Error('not used'); },
          async deleteTurns() { throw Error('not used'); },
          async abortAttempt() { throw Error('not used'); },
        },
      });
      const conversation = {
        id:'local-only-conversation', title:'New Chat', createdAt:10,
        _localOnly:true,
      };
      await runtime.submitConversation(conversation, 'question', {}, {});
      return {
        snapshotCalls, submitCalls, submittedTitle,
        localOnly:conversation._localOnly,
      };
    }
    """)

    assert result == {
        'snapshotCalls': 0,
        'submitCalls': 1,
        'submittedTitle': '',
        'localOnly': False,
    }


def test_native_turn_runtime_wake_skips_local_only_draft(
        page, assert_no_js_errors):
    """Regression pin: wakeConversation used to hydrate a draft's sync
    snapshot, so every visibilitychange/periodic reconcile 404'd with
    'Conversation not found' until the first turn was submitted."""
    page.wait_for_function(
        "typeof window.createConversationTurnRuntime === 'function'",
        timeout=30_000,
    )

    result = page.evaluate(r"""
    async () => {
      let snapshotCalls = 0;
      const runtime = window.createConversationTurnRuntime({
        api: {
          async snapshot() {
            snapshotCalls += 1;
            throw Error('must not probe');
          },
          async createTurn() { throw Error('not used'); },
          async createAttempt() { throw Error('not used'); },
          eventsUrl() { throw Error('not used'); },
          async updateTurn() { throw Error('not used'); },
          async createLane() { throw Error('not used'); },
          async deleteLane() { throw Error('not used'); },
          async deleteTurns() { throw Error('not used'); },
          async abortAttempt() { throw Error('not used'); },
        },
      });
      const conversation = {
        id:'local-only-wake', title:'New Chat', createdAt:10,
        _localOnly:true,
      };
      const first = await runtime.wakeConversation(conversation);
      const second = await runtime.wakeConversation(conversation);
      return {
        snapshotCalls,
        sameStore: first === second,
        localOnly: conversation._localOnly,
      };
    }
    """)

    assert result == {
        'snapshotCalls': 0,
        'sameStore': True,
        'localOnly': True,
    }


def test_typed_turn_reducer_rejects_stale_ingress_and_replays_unknown_turn(
        page, assert_no_js_errors):
    page.wait_for_function(
        "typeof window.reduceTurnState === 'function'",
        timeout=30_000,
    )

    result = page.evaluate(r"""
    () => {
      const modules = window;
      let unknownTurn = null;
      let state = modules.createTurnState('conv-r');
      state = modules.reduceTurnState(state, {
        type:'command_response', response:{ conversationRevision:1,
          turn:{ turnId:'t2', laneId:'main', ordinal:2, actor:'assistant',
            status:'running', currentAttemptId:'a2', projectionRevision:2,
            projection:{content:'new'} },
          attempt:{ attemptId:'a2', turnId:'t2', status:'completed' },
        },
      });
      state = modules.reduceTurnState(state, {
        type:'command_response', response:{
          turn:{ turnId:'t1', laneId:'main', ordinal:1, actor:'human',
            status:'completed', currentAttemptId:'a1', projectionRevision:1,
            projection:{content:'question'} },
          attempt:{ attemptId:'a2', turnId:'t2', status:'running' },
        },
      });
      const beforeStale = JSON.stringify(state);
      const stale = modules.reduceTurnState(state, { type:'event', event:{
        type:'projection_updated', turnId:'t2', attemptId:'old-attempt',
        seq:99, projectionRevision:99, payload:{projection:{content:'stale'}},
      }});
      const sourceWasImmutable = JSON.stringify(state) === beforeStale;

      let queued = modules.reduceTurnState(stale, { type:'event', event:{
        type:'projection_updated', turnId:'t3', attemptId:'a3',
        seq:2, projectionRevision:2, payload:{projection:{content:'late'}},
      }}, { onUnknownTurn(turnId) { unknownTurn = turnId; } });
      queued = modules.reduceTurnState(queued, { type:'event', event:{
        type:'projection_updated', turnId:'t3', attemptId:'a3',
        seq:2, projectionRevision:2, payload:{projection:{content:'duplicate'}},
      }});
      const queuedCount = queued.pendingEventsByTurn.t3.length;
      const replayed = modules.reduceTurnState(queued, { type:'snapshot', snapshot:{
        conversationRevision:2,
        turns:[{ turnId:'t3', laneId:'main', ordinal:3, actor:'assistant',
          status:'pending', currentAttemptId:'a3', projectionRevision:1,
          projection:{content:'seed'} }],
      }});
      const oldSnapshot = modules.reduceTurnState(replayed, {
        type:'snapshot', snapshot:{ conversationRevision:1,
          authoritativeFull:true, turns:[] },
      });
      const authoritative = modules.reduceTurnState(replayed, {
        type:'snapshot', snapshot:{ conversationRevision:3,
          authoritativeFull:true, turns:[replayed.turnsById.t3] },
      });
      return {
        sourceWasImmutable,
        ordered: state.laneOrder.main,
        staleContent: stale.turnsById.t2.projection.content,
        attemptStayedTerminal: state.attemptsById.a2.status,
        unknownTurn,
        queuedCount,
        replayedContent: replayed.turnsById.t3.projection.content,
        replayedSeq: replayed.attemptsById.a3.lastSeq,
        pendingCleared: !replayed.pendingEventsByTurn.t3,
        oldSnapshotIgnored: Boolean(oldSnapshot.turnsById.t1),
        authoritativeIds: Object.keys(authoritative.turnsById),
      };
    }
    """)

    assert result == {
        'sourceWasImmutable': True,
        'ordered': ['t1', 't2'],
        'staleContent': 'new',
        'attemptStayedTerminal': 'completed',
        'unknownTurn': 't3',
        'queuedCount': 1,
        'replayedContent': 'late',
        'replayedSeq': 2,
        'pendingCleared': True,
        'oldSnapshotIgnored': True,
        'authoritativeIds': ['t3'],
    }


def test_typed_turn_store_singleflights_snapshot_recovery_and_unsubscribes(
        page, assert_no_js_errors):
    page.wait_for_function(
        "typeof window.createTurnStore === 'function'",
        timeout=30_000,
    )

    result = page.evaluate(r"""
    async () => {
      let fetchCount = 0;
      let releaseSnapshot;
      let notifications = 0;
      const errors = [];
      const store = window.createTurnStore('conv-store', {
        fetchSnapshot() {
          fetchCount += 1;
          return new Promise(resolve => { releaseSnapshot = resolve; });
        },
        onResyncError(error, turnId) {
          errors.push(`${turnId}:${error.message}`);
        },
      });
      const unsubscribe = store.subscribe(() => { notifications += 1; });
      const unknownEvent = {
        type:'event', event:{
          type:'projection_updated', turnId:'late-turn', attemptId:'late-attempt',
          seq:2, projectionRevision:2, payload:{projection:{content:'late'}},
        },
      };
      store.dispatch(unknownEvent);
      store.dispatch(unknownEvent);
      await Promise.resolve();
      const fetchesWhilePending = fetchCount;
      releaseSnapshot({
        conversationRevision:1,
        turns:[{ turnId:'late-turn', laneId:'main', ordinal:1,
          actor:'assistant', status:'pending', currentAttemptId:'late-attempt',
          projectionRevision:1, projection:{content:'seed'} }],
      });
      await new Promise(resolve => setTimeout(resolve, 0));
      const recovered = store.getState();
      const beforeUnsubscribe = notifications;
      unsubscribe();
      store.dispatch({type:'transport', status:'connected'});

      let retryFetches = 0;
      let retryErrors = 0;
      const retryStore = window.createTurnStore('conv-retry', {
        fetchSnapshot() {
          retryFetches += 1;
          if (retryFetches === 1) return Promise.reject(new Error('temporary'));
          return Promise.resolve({ conversationRevision:1, turns:[] });
        },
        onResyncError() { retryErrors += 1; },
      });
      retryStore.dispatch(unknownEvent);
      await new Promise(resolve => setTimeout(resolve, 0));
      retryStore.dispatch(unknownEvent);
      await new Promise(resolve => setTimeout(resolve, 0));

      return {
        fetchesWhilePending,
        recoveredContent: recovered.turnsById['late-turn'].projection.content,
        recoveredSeq: recovered.attemptsById['late-attempt'].lastSeq,
        pendingCleared: !recovered.pendingEventsByTurn['late-turn'],
        beforeUnsubscribe,
        notifications,
        retryFetches,
        retryErrors,
        errors,
      };
    }
    """)

    assert result == {
        'fetchesWhilePending': 1,
        'recoveredContent': 'late',
        'recoveredSeq': 2,
        'pendingCleared': True,
        'beforeUnsubscribe': 3,
        'notifications': 3,
        'retryFetches': 2,
        'retryErrors': 1,
        'errors': [],
    }


def test_typed_turn_finish_presentation_normalizes_terminal_states(
        page, assert_no_js_errors):
    page.wait_for_function(
        "typeof window.presentTurnFinish === 'function'",
        timeout=30_000,
    )

    result = page.evaluate(r"""
    () => {
      const present = window.presentTurnFinish;
      const running = present({turnId:'running', status:'running'});
      const completed = present({turnId:'done', status:'completed'});
      const interrupted = present({
        turnId:'stopped', status:'interrupted', settlement:{
          cause:'server_restart',
          resumeOptions:['regenerate', {operation:'continue'}, null, ''],
        },
      });
      const truncated = present({
        turnId:'limited', status:'truncated',
        settlement:{providerFinishReason:'max_tokens'},
      });
      const failed = present({
        turnId:'failed', status:'failed', settlement:{error:'provider_down'},
      });
      return {running, completed, interrupted, truncated, failed};
    }
    """)

    assert result == {
        'running': None,
        'completed': {
            'tone': 'success', 'label': 'Completed', 'detail': '',
        },
        'interrupted': {
            'tone': 'warning', 'label': 'Interrupted',
            'detail': 'server_restart',
            'resumeOptions': [
                {'operation': 'regenerate'}, {'operation': 'continue'},
            ],
        },
        'truncated': {
            'tone': 'warning', 'label': 'Truncated', 'detail': 'max_tokens',
            'resumeOptions': [],
        },
        'failed': {
            # Legacy string errors normalize to a warning-severity envelope
            # (kind=generic, non-retryable) — severity drives the tone.
            'tone': 'warning', 'label': 'Failed', 'detail': 'provider_down',
            'errorKind': 'generic', 'retryable': False,
            'resumeOptions': [],
        },
    }


def test_lifecycle_scope_releases_listeners_timers_and_cleanups(
        page, assert_no_js_errors):
    page.wait_for_function(
        "typeof window.createLifecycleScope === 'function'",
        timeout=30_000,
    )

    result = page.evaluate(r"""
    async () => {
      const scope = window.createLifecycleScope();
      const target = new EventTarget();
      const calls = [];
      scope.listen(target, 'tick', () => calls.push('event'));
      scope.add(() => calls.push('first-cleanup'));
      scope.add(() => calls.push('second-cleanup'));
      scope.timeout(() => calls.push('timeout'), 5);
      scope.interval(() => calls.push('interval'), 5);
      target.dispatchEvent(new Event('tick'));
      scope.destroy();
      scope.destroy();
      target.dispatchEvent(new Event('tick'));
      await new Promise(resolve => setTimeout(resolve, 20));
      return {
        calls,
        aborted: scope.signal.aborted,
      };
    }
    """)

    assert result == {
        'calls': ['event', 'second-cleanup', 'first-cleanup'],
        'aborted': True,
    }


def test_turn_renderer_skips_unchanged_long_conversation_dom_work(
        page, assert_no_js_errors):
    page.wait_for_function(
        "typeof window.ConversationTurnStore?.renderInto === 'function' && "
        "typeof window.reduceTurnState === 'function'",
        timeout=30_000,
    )

    result = page.evaluate(r"""
    () => {
      const modules = window;
      let state = modules.createTurnState('conv-render');
      const turn = (turnId, ordinal, revision, content) => ({
        turnId, laneId:'main', ordinal, actor:'assistant', status:'running',
        currentAttemptId:`attempt-${turnId}`, projectionRevision:revision,
        projection:{content},
      });
      state = modules.reduceTurnState(state, {
        type:'command_response', response:{turn:turn('t1', 1, 1, 'one')},
      });
      state = modules.reduceTurnState(state, {
        type:'command_response', response:{turn:turn('t2', 2, 1, 'two')},
      });

      const container = document.createElement('section');
      const nativeAppend = container.appendChild.bind(container);
      let appendCalls = 0;
      container.appendChild = node => {
        appendCalls += 1;
        return nativeAppend(node);
      };
      const renderCounts = {t1:0, t2:0};
      const renderer = (node, value) => {
        renderCounts[value.turnId] += 1;
        node.textContent = value.projection.content;
      };

      window.ConversationTurnStore.renderInto(container, state, renderer);
      const initialAppends = appendCalls;
      appendCalls = 0;
      window.ConversationTurnStore.renderInto(container, state, renderer);
      const steadyAppends = appendCalls;
      const steadyRenders = {...renderCounts};

      state = modules.reduceTurnState(state, {
        type:'command_response', response:{turn:turn('t2', 2, 2, 'two-new')},
      });
      window.ConversationTurnStore.renderInto(container, state, renderer);
      const updateAppends = appendCalls;
      const updateRenders = {...renderCounts};

      appendCalls = 0;
      state = modules.reduceTurnState(state, {
        type:'command_response', response:{turn:turn('t2', 0, 2, 'two-new')},
      });
      window.ConversationTurnStore.renderInto(container, state, renderer);
      return {
        initialAppends,
        steadyAppends,
        steadyRenders,
        updateAppends,
        updateRenders,
        reorderAppends: appendCalls,
        order: Array.from(container.children).map(node => node.dataset.turnId),
      };
    }
    """)

    assert result == {
        'initialAppends': 2,
        'steadyAppends': 0,
        'steadyRenders': {'t1': 1, 't2': 1},
        'updateAppends': 0,
        'updateRenders': {'t1': 1, 't2': 2},
        'reorderAppends': 2,
        'order': ['t2', 't1'],
    }
