"""Release-visible behavior contracts retained across the Vite migration.

These tests exercise compiled TypeScript owners. They replace historical
browser-runtime tests whose positional message document, polling transport,
and public globals no longer exist in the shipped application.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def _run_native(sources: list[tuple[str, Path]], body: str) -> dict:
    node = shutil.which("node")
    assert node is not None, "the release frontend lane requires Node.js"
    bundles = [native_module_path(name, source) for name, source in sources]
    harness = f"""
const fs = require('fs');
globalThis.window = globalThis;
for (const filename of process.argv.slice(1)) {{
  (0, eval)(fs.readFileSync(filename, 'utf8'));
}}
(async () => {{
{body}
}})().then(
  result => console.log(JSON.stringify(result)),
  error => {{ console.error(error?.stack || error); process.exitCode = 1; }},
);
"""
    result = subprocess.run(
        [node, "-e", harness, *bundles],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_send_startup_lease_rolls_back_every_connecting_exit() -> None:
    result = _run_native(
        [("migration-send-startup.js", ROOT / "frontend/src/core/send-startup.ts")],
        r"""
const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

const timeoutOwner = {};
const timeoutLease = createSendStartupLease(timeoutOwner, {timeoutMs: 5});
await delay(20);
const timeoutReason = timeoutLease.reason;
const timeoutAborted = timeoutLease.signal.aborted;
timeoutLease.finish();

const userOwner = {};
const userLease = createSendStartupLease(userOwner, {timeoutMs: 1000});
userOwner._genStartStop = userLease.controller;
userLease.controller.abort();
const userReason = userLease.reason;
userLease.finish();

const raceOwner = {};
const superseded = createSendStartupLease(raceOwner, {timeoutMs: 1000});
const current = createSendStartupLease(raceOwner, {timeoutMs: 1000});
superseded.abort('superseded');
superseded.finish();
const oldFinishPreservedCurrent = raceOwner._genStartCtrl === current.controller;
current.abort('unmount');
const unmountReason = current.reason;
current.finish();

return {
  timeoutReason,
  timeoutAborted,
  timeoutMarkersCleared: timeoutOwner._genStartCtrl === null
    && timeoutOwner._genStartStop === null,
  userReason,
  userMarkersCleared: userOwner._genStartCtrl === null
    && userOwner._genStartStop === null,
  oldFinishPreservedCurrent,
  unmountReason,
  raceMarkersCleared: raceOwner._genStartCtrl === null
    && raceOwner._genStartStop === null,
};
""",
    )

    assert result == {
        "timeoutReason": "timeout",
        "timeoutAborted": True,
        "timeoutMarkersCleared": True,
        "userReason": "user-stop",
        "userMarkersCleared": True,
        "oldFinishPreservedCurrent": True,
        "unmountReason": "unmount",
        "raceMarkersCleared": True,
    }


def test_turn_store_replays_pending_shell_and_rejects_stale_revisions() -> None:
    result = _run_native(
        [
            ("migration-turn-state.js", ROOT / "frontend/src/core/turn-state.ts"),
            (
                "migration-conversation-read-model.js",
                ROOT
                / "frontend/src/conversation/application/conversation-read-model.ts",
            ),
        ],
        r"""
const delay = () => new Promise(resolve => setTimeout(resolve, 0));
const turn = (revision, status, content, translatedText) => ({
  conversationId: 'conv-a', turnId: 'turn-a', laneId: 'main', ordinal: 1,
  actor: 'assistant', kind: 'reply', status,
  currentAttemptId: 'attempt-a', projectionRevision: revision,
  projection: {content, translatedText}, createdAt: 1, updatedAt: revision,
});
const event = (seq, revision, type, payload) => ({
  conversationId: 'conv-a', turnId: 'turn-a', attemptId: 'attempt-a',
  seq, projectionRevision: revision, type, payload,
});

let fetchCount = 0;
let releaseInitialSnapshot;
const store = createTurnStore('conv-a', {
  fetchSnapshot() {
    fetchCount += 1;
    if (fetchCount === 1) {
      return new Promise(resolve => { releaseInitialSnapshot = resolve; });
    }
    return Promise.resolve({
      conversationRevision: 7,
      authoritativeFull: true,
      turns: [turn(5, 'running', 'authoritative', '权威-5')],
      attempts: [{
        attemptId: 'attempt-a', turnId: 'turn-a', status: 'running', lastSeq: 3,
      }],
    });
  },
});
const pendingEvent = {
  type: 'event',
  event: event(2, 2, 'projection_updated', {
    projection: {content: 'replayed', translatedText: '重放-2'},
    turnState: {
      turnId: 'turn-a', status: 'running', currentAttemptId: 'attempt-a',
      updatedAt: 2,
    },
  }),
};
store.dispatch(pendingEvent);
store.dispatch(pendingEvent);
await Promise.resolve();
const fetchesWhilePending = fetchCount;
const queuedWhilePending = store.getState().pendingEventsByTurn['turn-a'].length;
releaseInitialSnapshot({
  conversationRevision: 5,
  turns: [turn(1, 'pending', 'seed', '初始-1')],
  attempts: [{
    attemptId: 'attempt-a', turnId: 'turn-a', status: 'pending', lastSeq: 1,
  }],
});
await delay();
await delay();
const replayed = store.getState();

store.dispatch({type: 'command_pending', turnId: 'turn-a', operation: 'retry'});
store.dispatch({
  type: 'snapshot',
  snapshot: {
    conversationRevision: 4,
    authoritativeFull: true,
    turns: [turn(99, 'running', 'stale-snapshot', '过期快照')],
  },
});
const afterStaleSnapshot = store.getState();

store.dispatch({
  type: 'event',
  event: event(3, 4, 'projection_updated', {
    projectionPatch: {
      version: 1, baseRevision: 3, targetRevision: 4,
      operations: [{op: 'replace', path: ['content'], value: 'bad-patch'}],
    },
    turnState: {
      turnId: 'turn-a', status: 'running', currentAttemptId: 'attempt-a',
      updatedAt: 4,
    },
  }),
});
await delay();
await delay();
const afterGapRecovery = store.getState();

store.dispatch({
  type: 'event',
  event: event(4, 6, 'terminal_settlement', {
    projection: {content: 'done', translatedText: '完成-6'},
    status: 'completed', settlement: {cause: 'stop'},
    turnState: {
      turnId: 'turn-a', status: 'completed', currentAttemptId: 'attempt-a',
      settlement: {cause: 'stop'}, updatedAt: 8,
    },
  }),
});
store.dispatch({
  type: 'event',
  event: event(5, 5, 'projection_updated', {
    projection: {content: 'stale-event', translatedText: '过期翻译'},
    turnState: {
      turnId: 'turn-a', status: 'running', currentAttemptId: 'attempt-a',
      updatedAt: 9,
    },
  }),
});
const finalState = store.getState();

return {
  fetchesWhilePending,
  queuedWhilePending,
  replayedContent: replayed.turnsById['turn-a'].projection.content,
  replayedTranslation: replayed.turnsById['turn-a'].projection.translatedText,
  pendingShellCleared: !replayed.pendingEventsByTurn['turn-a'],
  staleSnapshotContent: afterStaleSnapshot.turnsById['turn-a'].projection.content,
  staleSnapshotKeptPending: afterStaleSnapshot.commandPending['turn-a'] === 'retry',
  totalFetches: fetchCount,
  recoveredContent: afterGapRecovery.turnsById['turn-a'].projection.content,
  recoveredRevision: afterGapRecovery.turnsById['turn-a'].projectionRevision,
  finalContent: finalState.turnsById['turn-a'].projection.content,
  finalTranslation: finalState.turnsById['turn-a'].projection.translatedText,
  finalStatus: finalState.turnsById['turn-a'].status,
  activeAttemptIds: activeConversationAttemptIds(finalState),
  commandPendingCleared: !finalState.commandPending['turn-a'],
};
""",
    )

    assert result == {
        "fetchesWhilePending": 1,
        "queuedWhilePending": 1,
        "replayedContent": "replayed",
        "replayedTranslation": "重放-2",
        "pendingShellCleared": True,
        "staleSnapshotContent": "replayed",
        "staleSnapshotKeptPending": True,
        "totalFetches": 2,
        "recoveredContent": "authoritative",
        "recoveredRevision": 5,
        "finalContent": "done",
        "finalTranslation": "完成-6",
        "finalStatus": "completed",
        "activeAttemptIds": [],
        "commandPendingCleared": True,
    }


def test_sync_gap_and_reset_recover_from_durable_cursor() -> None:
    result = _run_native(
        [
            (
                "migration-conversation-sync.js",
                ROOT / "frontend/src/core/conversation-sync.ts",
            ),
        ],
        r"""
class FakeEventSource {
  constructor(url) {
    this.url = url;
    this.closed = false;
    this.listeners = new Map();
    this.onopen = null;
    this.onmessage = null;
    this.onerror = null;
  }
  addEventListener(name, listener) {
    const listeners = this.listeners.get(name) || [];
    listeners.push(listener);
    this.listeners.set(name, listeners);
  }
  removeEventListener(name, listener) {
    this.listeners.set(name, (this.listeners.get(name) || [])
      .filter(candidate => candidate !== listener));
  }
  emit(name, body, lastEventId = '') {
    const message = {type: name, data: JSON.stringify(body), lastEventId};
    for (const listener of this.listeners.get(name) || []) listener(message);
  }
  close() { this.closed = true; }
}
const eventually = async predicate => {
  for (let index = 0; index < 30; index += 1) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 0));
  }
  throw new Error('conversation sync recovery did not settle');
};

const sources = [];
const snapshots = [];
const protocolErrors = [];
const streamCalls = [];
let snapshotCount = 0;
const snapshotSequences = [2, 10, 20];
const api = {
  async snapshot(conversationId) {
    const syncSeq = snapshotSequences[snapshotCount++];
    return {
      ok: true,
      contract: 'tofu.conversation-sync.snapshot/v1',
      conversationId,
      conversationRevision: syncSeq,
      syncSeq,
      cursor: `cursor-${syncSeq}`,
      serverBootId: 'boot-test', heartbeatIntervalMs: 15000,
      settings: {}, turns: [], attempts: [],
    };
  },
  eventsUrl(conversationId, after, streamClientId, streamGeneration) {
    streamCalls.push({conversationId, after, streamClientId, streamGeneration});
    return `/conversations/${conversationId}/events?after=${after}`;
  },
};
const coordinator = new ConversationSyncCoordinator({
  conversationId: 'conv-a',
  streamClientId: 'page-a',
  api,
  eventSourceFactory(url) {
    const source = new FakeEventSource(url);
    sources.push(source);
    return source;
  },
  onSnapshot(snapshot) { snapshots.push(snapshot); },
  onAttemptEvent() { return true; },
  onTurnDelta() { return true; },
  onProtocolError(error) { protocolErrors.push(error.message); },
});

await coordinator.hydrate(true);
const first = sources[0];
first.onopen();
first.emit('turn.upsert', {
  contract: 'tofu.conversation-sync.event/v1',
  type: 'turn.upsert', conversationId: 'conv-a', syncSeq: 4,
  occurredAt: 4, payload: {turns: []},
}, 'cursor-4');
await eventually(() => sources.length === 2 && snapshots.length === 2);

const second = sources[1];
second.onopen();
second.emit('sync.reset_required', {
  contract: 'tofu.conversation-sync.event/v1',
  type: 'sync.reset_required', conversationId: 'conv-a',
  cursor: 'expired-cursor', reason: 'cursor_expired',
});
await eventually(() => sources.length === 3 && snapshots.length === 3);
const third = sources[2];
const cursor = coordinator.cursor;
coordinator.close();

return {
  sourceUrls: sources.map(source => source.url),
  snapshotCount: snapshots.length,
  protocolErrors,
  firstClosed: first.closed,
  secondClosed: second.closed,
  thirdClosed: third.closed,
  cursor,
  streamCalls,
};
""",
    )

    assert result == {
        "sourceUrls": [
            "/conversations/conv-a/events?after=cursor-2",
            "/conversations/conv-a/events?after=cursor-10",
            "/conversations/conv-a/events?after=cursor-20",
        ],
        "snapshotCount": 3,
        "protocolErrors": [],
        "firstClosed": True,
        "secondClosed": True,
        "thirdClosed": True,
        "cursor": "cursor-20",
        "streamCalls": [
            {
                "conversationId": "conv-a",
                "after": "cursor-2",
                "streamClientId": "page-a",
                "streamGeneration": 1,
            },
            {
                "conversationId": "conv-a",
                "after": "cursor-10",
                "streamClientId": "page-a",
                "streamGeneration": 3,
            },
            {
                "conversationId": "conv-a",
                "after": "cursor-20",
                "streamClientId": "page-a",
                "streamGeneration": 5,
            },
        ],
    }


def test_stale_projection_adopts_body_latest_turn_and_clears_pending() -> None:
    result = _run_native(
        [("migration-turn-runtime.js", ROOT / "frontend/src/core/turn-runtime.ts")],
        r"""
const initial = {
  turnId:'turn-stale', conversationId:'conv-stale', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', runId:'run-stale', status:'completed',
  currentAttemptId:null, projection:{content:'old'}, projectionRevision:3,
  settlement:{outcome:'completed'}, createdAt:1, updatedAt:3,
};
const latest = {
  ...initial, projection:{content:'authoritative'}, projectionRevision:4,
  updatedAt:4,
};
const stale = {
  status:409,
  body:{error:{kind:'stale_projection'}, latestTurn:latest},
};
const runtime = createConversationTurnRuntime({
  api:{
    async createAttempt() { throw stale; },
  },
});
const store = runtime.createStore('conv-stale');
store.dispatch({type:'snapshot', snapshot:{
  conversationRevision:4, turns:[initial], attempts:[], queueItems:[],
}});
let caught = null;
try {
  await runtime.runOperation(store, 'turn-stale', 'retry');
} catch (error) {
  caught = error;
}
const state = store.getState();
return {
  sameError:caught === stale,
  errorKind:caught?.body?.error?.kind,
  content:state.turnsById['turn-stale'].projection.content,
  projectionRevision:state.turnsById['turn-stale'].projectionRevision,
  pendingCleared:!state.commandPending['turn-stale'],
};
""",
    )

    assert result == {
        "sameError": True,
        "errorKind": "stale_projection",
        "content": "authoritative",
        "projectionRevision": 4,
        "pendingCleared": True,
    }
