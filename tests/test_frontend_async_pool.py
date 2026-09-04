"""Bounded typed work-pool and conversation-wake adapter contracts."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tests._runtime_sections import native_module_path


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
OWNER = ROOT / 'frontend/src/core/async-pool.ts'
OWNER_BUNDLE = native_module_path(
    '.native/async-pool-contract.js',
    OWNER,
)
INVALIDATION = (
    ROOT / 'frontend/src/runtime/sections/core/conversation_invalidation.js'
)
WAKE_OWNER = ROOT / (
    'frontend/src/conversation/application/conversation-wake-recovery.ts'
)
WAKE_OWNER_BUNDLE = native_module_path(
    '.native/conversation-wake-recovery.js',
    WAKE_OWNER,
)


def _run_node(script: str) -> str:
    proc = subprocess.run(
        ['node', '-e', script], cwd=ROOT,
        capture_output=True, text=True, timeout=60,
    )
    output = (proc.stdout or '') + (proc.stderr or '')
    assert proc.returncode == 0, output
    return output


@pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')
def test_async_pool_public_behavior_and_resource_ceiling():
    script = r'''
const fs = require('fs');
eval(fs.readFileSync(OWNER_PATH, 'utf8'));
const checks = [];
const check = (name, value) => checks.push((value ? 'PASS ' : 'FAIL ') + name);

function statefulWorker(state, rejectEvery) {
  return (item, index) => {
    state.active += 1;
    state.peak = Math.max(state.peak, state.active);
    state.seen.push([item, index]);
    return new Promise((resolve, reject) => setTimeout(() => {
      state.active -= 1;
      if (rejectEvery && item % rejectEvery === 0) {
        reject(new Error('boom ' + item));
      } else {
        resolve();
      }
    }, 3));
  };
}

(async () => {
  const items = Array.from({ length: 24 }, (_, index) => index);
  const bounded = { active: 0, peak: 0, seen: [] };
  const result = await runWithConcurrency(
    items, statefulWorker(bounded, 5), DEFAULT_ASYNC_POOL_CONCURRENCY);
  check('default_budget_is_four', DEFAULT_ASYNC_POOL_CONCURRENCY === 4);
  check('peak_reaches_but_never_exceeds_budget', bounded.peak === 4);
  check('each_item_and_index_is_visited_once',
    bounded.seen.length === items.length
      && new Set(bounded.seen.map(([item, index]) => item + ':' + index)).size
        === items.length);
  check('rejections_do_not_abort_later_items', result.completed === items.length);
  check('rejections_are_collected', result.errors.length === 5);

  const serial = { active: 0, peak: 0, seen: [] };
  await runWithConcurrency(items, statefulWorker(serial, 0), 1);
  check('caller_can_tighten_the_budget', serial.peak === 1);

  const synchronous = await runWithConcurrency([1, 2, 3], (item) => {
    if (item === 2) throw new Error('sync');
  }, 2);
  check('synchronous_worker_failure_is_isolated',
    synchronous.completed === 3 && synchronous.errors.length === 1);
  const empty = await runWithConcurrency('not-an-array', () => {}, 4);
  check('invalid_or_empty_input_is_a_noop',
    empty.completed === 0 && empty.errors.length === 0);

  console.log(checks.join('\n'));
  if (checks.some(line => line.startsWith('FAIL'))) process.exitCode = 1;
})().catch(error => { console.error(error); process.exit(1); });
'''.replace('OWNER_PATH', json.dumps(OWNER_BUNDLE))
    output = _run_node(script)
    assert output.count('PASS') == 8, output


@pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')
def test_conversation_wake_adapters_share_the_typed_pool_budget():
    script = r'''
const fs = require('fs');
eval(fs.readFileSync(OWNER_PATH, 'utf8'));
eval(fs.readFileSync(WAKE_OWNER_PATH, 'utf8'));

function sliceFunction(source, signature) {
  const start = source.indexOf(signature);
  if (start < 0) throw new Error('missing function: ' + signature);
  const body = source.indexOf('{', start);
  let depth = 0;
  for (let index = body; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1;
    if (source[index] === '}' && --depth === 0) {
      return source.slice(start, index + 1);
    }
  }
  throw new Error('unbalanced function: ' + signature);
}

const invalidation = fs.readFileSync(INVALIDATION_PATH, 'utf8');
eval(sliceFunction(invalidation, 'async function _recoverOfflineConversations()'));

const checks = [];
const check = (name, value) => checks.push((value ? 'PASS ' : 'FAIL ') + name);
global.conversations = Array.from({ length: 12 }, (_, index) => ({
  id: 'conv-' + index,
  _conversationSyncHealth: { state: index % 2 ? 'offline' : 'healthy' },
}));
global.activeConvId = 'conv-0';
let state = null;
function resetState() {
  state = { active: 0, peak: 0, calls: [], warnings: [] };
}
global.runtimeScope = {
  ConversationTurnRead: { activeAttemptIds: () => ['attempt'] },
  ConversationTurnStore: {
    wakeConversation(conversation) {
      state.active += 1;
      state.peak = Math.max(state.peak, state.active);
      state.calls.push(conversation.id);
      return new Promise((resolve, reject) => setTimeout(() => {
        state.active -= 1;
        if (conversation.id === 'conv-3' || conversation.id === 'conv-9') {
          reject(new Error('offline ' + conversation.id));
        } else {
          resolve();
        }
      }, 3));
    },
  },
};
console.warn = (...values) => state.warnings.push(values);
const wakeController = createConversationWakeRecovery({
  readConversations: () => conversations,
  activeAttemptIds: (conversation) => (
    runtimeScope.ConversationTurnRead.activeAttemptIds(conversation)
  ),
  wakeConversation: (conversation) => (
    runtimeScope.ConversationTurnStore.wakeConversation(conversation)
  ),
  warn: (error) => console.warn(
    '[ConversationSync] wake recovery failed:', error),
});

(async () => {
  resetState();
  const recovered = await _recoverOfflineConversations();
  check('offline_recovery_uses_four_lanes', state.peak === 4);
  check('offline_recovery_visits_every_target', state.calls.length === 12);
  check('offline_recovery_preserves_fulfilled_count', recovered === 10);

  resetState();
  const probed = await wakeController.probe();
  check('live_attempt_probe_uses_four_lanes', state.peak === 4);
  check('live_attempt_probe_visits_every_target', state.calls.length === 12);
  check('live_attempt_probe_reports_each_failure',
    probed === 10 && state.warnings.length === 2);

  const listeners = new Map();
  const eventTarget = {
    addEventListener(type, listener) {
      const bucket = listeners.get(type) || new Set();
      bucket.add(listener); listeners.set(type, bucket);
    },
    removeEventListener(type, listener) {
      listeners.get(type)?.delete(listener);
    },
  };
  wakeController.start(eventTarget);
  wakeController.start(eventTarget);
  check('wake_listeners_are_idempotent',
    listeners.get('pageshow').size === 1
      && listeners.get('online').size === 1
      && listeners.get('beforeunload').size === 1);
  wakeController.destroy();
  check('wake_listeners_are_disposed',
    [...listeners.values()].every((bucket) => bucket.size === 0));

  console.log(checks.join('\n'));
  if (checks.some(line => line.startsWith('FAIL'))) process.exitCode = 1;
})().catch(error => { console.error(error); process.exit(1); });
'''
    replacements = {
        'WAKE_OWNER_PATH': WAKE_OWNER_BUNDLE,
        'OWNER_PATH': OWNER_BUNDLE,
        'INVALIDATION_PATH': str(INVALIDATION),
    }
    for marker, path in replacements.items():
        script = script.replace(marker, json.dumps(path))
    output = _run_node(script)
    assert output.count('PASS') == 8, output
