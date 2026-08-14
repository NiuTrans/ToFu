"""Real-browser contracts for the typed V2 attempt event transport."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.visual


def test_attempt_stream_owns_cursor_recovery_and_terminal_close(
        page, assert_no_js_errors):
    page.wait_for_function(
        "typeof window.TofuModules?.createAttemptEventStream === 'function'",
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
        emit(name, body) {
          this.dispatchEvent(new MessageEvent(name, {
            data: JSON.stringify(body),
          }));
        }
      }

      const statuses = [];
      const events = [];
      const snapshots = [];
      const terminals = [];
      const continuations = [];
      const protocolErrors = [];
      let source;
      let fetchCount = 0;
      let releaseSnapshot;

      const connection = window.TofuModules.createAttemptEventStream({
        attemptId: 'attempt-a',
        url: '/attempts/attempt-a/events?after=2',
        after: 2,
        eventSourceFactory(url) {
          source = new FakeEventSource(url);
          return source;
        },
        onTransport(status) { statuses.push(status); },
        onEvent(event) { events.push(event); },
        fetchSnapshot() {
          fetchCount += 1;
          return new Promise(resolve => { releaseSnapshot = resolve; });
        },
        onSnapshot(snapshot) { snapshots.push(snapshot); },
        onTerminal(event) { terminals.push(event); },
        onContinuation(value) { continuations.push(value); },
        onProtocolError(error) { protocolErrors.push(error.message); },
      });

      source.onopen();
      source.emit('projection_updated', {
        type: 'projection_updated', seq: 5, attemptId: 'attempt-a',
        requestId: 'req-1', payload: { projection: { content: 'hello' } },
      });
      source.emit('status_changed', {
        type: 'status_changed', seq: 3, attemptId: 'attempt-a',
        payload: { status: 'running' },
      });
      source.emit('projection_updated', {
        type: 'projection_updated', seq: 99, attemptId: 'attempt-other',
        payload: { projection: { content: 'wrong stream' } },
      });
      source.onmessage(new MessageEvent('message', { data: '{bad json' }));

      source.onerror();
      source.onerror();
      await Promise.resolve();
      const fetchCountBeforeRelease = fetchCount;
      releaseSnapshot({ conversationRevision: 9, turns: [] });
      await new Promise(resolve => setTimeout(resolve, 0));

      source.emit('terminal_settlement', {
        type: 'terminal_settlement', seq: 6, attemptId: 'attempt-a',
        payload: { settlement: { continuation: { attemptId: 'attempt-b' } } },
      });
      connection.close();

      return {
        url: source.url,
        statuses,
        eventTypes: events.map(event => event.type),
        cursor: connection.cursor,
        fetchCountBeforeRelease,
        snapshotCount: snapshots.length,
        terminalCount: terminals.length,
        continuationIds: continuations.map(value => value.attemptId),
        protocolErrorCount: protocolErrors.length,
        closed: source.closed,
      };
    }
    """)

    assert result == {
        'url': '/attempts/attempt-a/events?after=2',
        'statuses': ['connecting', 'connected', 'reconnecting', 'reconnecting'],
        'eventTypes': [
            'projection_updated', 'status_changed', 'terminal_settlement',
        ],
        'cursor': 6,
        'fetchCountBeforeRelease': 1,
        'snapshotCount': 1,
        'terminalCount': 1,
        'continuationIds': ['attempt-b'],
        'protocolErrorCount': 2,
        'closed': True,
    }


def test_send_startup_lease_owns_timeout_stop_and_race_cleanup(
        page, assert_no_js_errors):
    page.wait_for_function(
        "typeof window.TofuModules?.createSendStartupLease === 'function'",
        timeout=30_000,
    )

    result = page.evaluate(r"""
    async () => {
      const timeoutOwner = {};
      const timeoutLease = window.TofuModules.createSendStartupLease(
        timeoutOwner, { timeoutMs: 5 });
      const timeoutWasOwned = timeoutOwner._genStartCtrl === timeoutLease.controller;
      await new Promise(resolve => setTimeout(resolve, 20));
      const timedOut = timeoutLease.signal.aborted && timeoutLease.reason === 'timeout';
      timeoutLease.finish();

      const stopOwner = {};
      const stopLease = window.TofuModules.createSendStartupLease(
        stopOwner, { timeoutMs: 0 });
      stopOwner._genStartStop = stopLease.controller;
      stopOwner._genStartCtrl = null;
      stopLease.controller.abort();
      const userStopped = stopLease.stoppedByUser()
        && stopLease.reason === 'user-stop';
      stopLease.finish();

      const raceOwner = {};
      const older = window.TofuModules.createSendStartupLease(
        raceOwner, { timeoutMs: 0 });
      const newer = window.TofuModules.createSendStartupLease(
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


def test_turn_projection_is_pure_ordered_and_branch_aware(
        page, assert_no_js_errors):
    page.wait_for_function(
        "typeof window.TofuModules?.projectTurnState === 'function'",
        timeout=30_000,
    )

    result = page.evaluate(r"""
    () => {
      const previousMessages = [{
        _turnId: 'human-1',
        branches: [{ _laneId:'lane-b', title:'Preserved title',
          icon:'B', messages:[{content:'stale'}] }],
      }];
      const state = {
        conversationRevision: 7,
        transport: 'connected',
        commandPending: { 'assistant-1': 'regenerate' },
        laneOrder: {
          main: ['human-1', 'assistant-1'],
          'lane-b': ['branch-1'],
        },
        turnsById: {
          'human-1': {
            turnId:'human-1', actor:'human', laneId:'main', status:'completed',
            projectionRevision:1, createdAt:10,
            projection:{ role:'assistant', content:'question', _branchLanes:[{
              laneId:'lane-b', kind:'branch', anchorText:'selection',
            }] },
          },
          'assistant-1': {
            turnId:'assistant-1', actor:'assistant', laneId:'main',
            status:'running', currentAttemptId:'attempt-main',
            projectionRevision:2, createdAt:20,
            projection:{ content:'answer' },
          },
          'branch-1': {
            turnId:'branch-1', actor:'critic', laneId:'lane-b',
            parentTurnId:'human-1', status:'pending',
            currentAttemptId:'attempt-branch', projectionRevision:3,
            projection:{ content:'branch answer' },
          },
        },
      };
      const before = JSON.stringify({ previousMessages, state });
      const projected = window.TofuModules.projectTurnState({
        state, previousMessages, now: () => 99,
      });
      const after = JSON.stringify({ previousMessages, state });
      const main = projected.messages;
      const branch = main[0].branches[0];
      return {
        sourceUnchanged: before === after,
        roles: main.map(item => item.role),
        orderedIds: main.map(item => item._turnId),
        maliciousRoleRemoved: main[0].role === 'user',
        commandPending: main[1]._commandPending,
        activeAttemptId: projected.activeAttemptId,
        activeBranchAttemptIds: projected.activeBranchAttemptIds,
        branchTitle: branch.title,
        branchRole: branch.messages[0].role,
        branchContent: branch.messages[0].content,
        fingerprintHasRevision: projected.fingerprint.includes(
          'main:assistant-1:2:running:attempt-main:regenerate'),
      };
    }
    """)

    assert result == {
        'sourceUnchanged': True,
        'roles': ['user', 'assistant'],
        'orderedIds': ['human-1', 'assistant-1'],
        'maliciousRoleRemoved': True,
        'commandPending': 'regenerate',
        'activeAttemptId': 'attempt-main',
        'activeBranchAttemptIds': ['attempt-branch'],
        'branchTitle': 'Preserved title',
        'branchRole': 'user',
        'branchContent': 'branch answer',
        'fingerprintHasRevision': True,
    }


def test_native_turn_runtime_owns_hydrate_submit_stream_and_projection(
        page, assert_no_js_errors):
    page.wait_for_function(
        "typeof window.TofuModules?.createConversationTurnRuntime === 'function'",
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
        emit(name, body) {
          this.dispatchEvent(new MessageEvent(name, {
            data: JSON.stringify(body),
          }));
        }
      }

      const sources = [];
      const submitted = [];
      const persisted = [];
      const api = {
        async list(conversationId) {
          return {
            cutoverActive: true,
            conversationRevision: 1,
            authoritativeFull: true,
            turns: [{
              conversationId, turnId:'human-1', laneId:'main', ordinal:0,
              actor:'human', kind:'input', status:'completed',
              projectionRevision:1, projection:{content:'hello'},
            }],
          };
        },
        async submit(conversationId, payload, requestOptions) {
          submitted.push({conversationId, payload, requestOptions});
          return {
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
            streamCursor: 4,
          };
        },
        async attempt() { throw Error('not used'); },
        streamUrl(attemptId, after) {
          return `/attempts/${attemptId}?after=${after}`;
        },
        async update() { throw Error('not used'); },
        async createLane() { throw Error('not used'); },
        async deleteLane() { throw Error('not used'); },
        async deleteTurns() { throw Error('not used'); },
        async abort(attemptId) { return {attemptId}; },
      };
      const runtime = window.TofuModules.createConversationTurnRuntime({
        api,
        eventSourceFactory(url) {
          const source = new FakeEventSource(url);
          sources.push(source);
          return source;
        },
        persist(conv) { persisted.push(conv.messages.map(item => item.content)); },
      });
      const signal = new AbortController().signal;
      const conv = {id:'conv-1', title:'Typed', createdAt:10, messages:[]};

      await runtime.hydrateConversation(conv);
      await runtime.submitConversation(conv, 'question', {model:'test'}, {
        commandId:'cmd-1', settings:{search:true},
        requestOptions:{signal, headers:{'Idempotency-Key':'cmd-1'}},
      });
      const source = sources[0];
      source.emit('projection_updated', {
        type:'projection_updated', seq:5, turnId:'assistant-1',
        attemptId:'attempt-1', projectionRevision:2,
        payload:{projection:{content:'typed stream'}},
      });
      source.emit('terminal_settlement', {
        type:'terminal_settlement', seq:6, turnId:'assistant-1',
        attemptId:'attempt-1', projectionRevision:3,
        payload:{status:'completed', settlement:{cause:'stop'},
          projection:{content:'typed done'}},
      });

      return {
        nativeMarker: runtime.emptyState === window.TofuModules.createTurnState
          && typeof window.TurnStoreV2?.hydrateConversation === 'function',
        cutover: runtime.isCutoverActive(),
        payloadCommandId: submitted[0].payload.commandId,
        payloadMessage: submitted[0].payload.message,
        requestSignalPreserved: submitted[0].requestOptions.signal === signal,
        sourceUrl: source.url,
        sourceClosed: source.closed,
        messageIds: conv.messages.map(item => item._turnId),
        messageContents: conv.messages.map(item => item.content),
        status: conv.messages[1]._turnStatus,
        activeAttemptId: conv._activeAttemptId,
        persistedCount: persisted.length,
      };
    }
    """)

    assert result == {
        'nativeMarker': True,
        'cutover': True,
        'payloadCommandId': 'cmd-1',
        'payloadMessage': 'question',
        'requestSignalPreserved': True,
        'sourceUrl': '/attempts/attempt-1?after=4',
        'sourceClosed': True,
        'messageIds': ['human-1', 'assistant-1'],
        'messageContents': ['hello', 'typed done'],
        'status': 'completed',
        'activeAttemptId': None,
        # Persist durable projections only: hydrate, submit, projection and
        # settlement. The transient connecting transport updates chrome but
        # deliberately does not rewrite conversation storage.
        'persistedCount': 4,
    }


def test_native_turn_runtime_does_not_probe_an_unsaved_conversation(
        page, assert_no_js_errors):
    page.wait_for_function(
        "typeof window.TofuModules?.createConversationTurnRuntime === 'function'",
        timeout=30_000,
    )

    result = page.evaluate(r"""
    async () => {
      let listCalls = 0;
      let submitCalls = 0;
      const runtime = window.TofuModules.createConversationTurnRuntime({
        api: {
          async list() { listCalls += 1; throw Error('must not probe'); },
          async submit(conversationId) {
            submitCalls += 1;
            return {
              conversationRevision: 1,
              turn: {
                conversationId, turnId:'assistant-local', laneId:'main', ordinal:1,
                actor:'assistant', kind:'reply', status:'completed',
                projectionRevision:1, projection:{content:'done'},
              },
              attempt: {attemptId:'', turnId:'assistant-local', status:'completed'},
              streamCursor: 0,
            };
          },
          async attempt() { throw Error('not used'); },
          streamUrl() { throw Error('not used'); },
          async update() { throw Error('not used'); },
          async createLane() { throw Error('not used'); },
          async deleteLane() { throw Error('not used'); },
          async deleteTurns() { throw Error('not used'); },
          async abort() { throw Error('not used'); },
        },
      });
      const conversation = {
        id:'local-only-conversation', title:'New Chat', createdAt:10,
        messages:[], _localOnly:true,
      };
      await runtime.submitConversation(conversation, 'question', {}, {});
      return {listCalls, submitCalls, localOnly:conversation._localOnly};
    }
    """)

    assert result == {'listCalls': 0, 'submitCalls': 1, 'localOnly': False}


def test_typed_turn_reducer_rejects_stale_ingress_and_replays_unknown_turn(
        page, assert_no_js_errors):
    page.wait_for_function(
        "typeof window.TofuModules?.reduceTurnState === 'function'",
        timeout=30_000,
    )

    result = page.evaluate(r"""
    () => {
      const modules = window.TofuModules;
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
        "typeof window.TofuModules?.createTurnStore === 'function'",
        timeout=30_000,
    )

    result = page.evaluate(r"""
    async () => {
      let fetchCount = 0;
      let releaseSnapshot;
      let notifications = 0;
      const errors = [];
      const store = window.TofuModules.createTurnStore('conv-store', {
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
      const retryStore = window.TofuModules.createTurnStore('conv-retry', {
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
        "typeof window.TofuModules?.presentTurnFinish === 'function'",
        timeout=30_000,
    )

    result = page.evaluate(r"""
    () => {
      const present = window.TofuModules.presentTurnFinish;
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
            'tone': 'error', 'label': 'Failed', 'detail': 'provider_down',
            'resumeOptions': [],
        },
    }


def test_lifecycle_scope_releases_listeners_timers_and_cleanups(
        page, assert_no_js_errors):
    page.wait_for_function(
        "typeof window.TofuModules?.createLifecycleScope === 'function'",
        timeout=30_000,
    )

    result = page.evaluate(r"""
    async () => {
      const scope = window.TofuModules.createLifecycleScope();
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
        "typeof window.TurnStoreV2?.renderInto === 'function' && "
        "typeof window.TofuModules?.reduceTurnState === 'function'",
        timeout=30_000,
    )

    result = page.evaluate(r"""
    () => {
      const modules = window.TofuModules;
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

      window.TurnStoreV2.renderInto(container, state, renderer);
      const initialAppends = appendCalls;
      appendCalls = 0;
      window.TurnStoreV2.renderInto(container, state, renderer);
      const steadyAppends = appendCalls;
      const steadyRenders = {...renderCounts};

      state = modules.reduceTurnState(state, {
        type:'command_response', response:{turn:turn('t2', 2, 2, 'two-new')},
      });
      window.TurnStoreV2.renderInto(container, state, renderer);
      const updateAppends = appendCalls;
      const updateRenders = {...renderCounts};

      appendCalls = 0;
      state = modules.reduceTurnState(state, {
        type:'command_response', response:{turn:turn('t2', 0, 2, 'two-new')},
      });
      window.TurnStoreV2.renderInto(container, state, renderer);
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
