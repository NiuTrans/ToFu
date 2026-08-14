"""Behavior contract for the native TypeScript paper push transport."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._esm_feature_harness import compile_feature_owner


pytestmark = pytest.mark.unit
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
SOURCE = os.path.join(
    ROOT, 'frontend', 'src', 'features', 'paper', 'push-transport.ts')
CLASSIC_SOURCE = os.path.join(
    ROOT, 'static', 'js', 'paper', 'push_transport.js')
ESBUILD = os.path.join(ROOT, 'node_modules', '.bin', 'esbuild')


_HARNESS = r"""
const fs = require('fs');
global.window = global;
global.console = { debug() {}, log: console.log };
const subscriptions = [];
global.pushSubscribe = (channel, taskId, handler) => {
  subscriptions.push({ channel, taskId, handler });
};
global.pushUnsubscribe = (channel, taskId, handler) => {
  for (let i = subscriptions.length - 1; i >= 0; i--) {
    const row = subscriptions[i];
    if (row.channel === channel && row.taskId === taskId &&
        (!handler || row.handler === handler)) subscriptions.splice(i, 1);
  }
};
eval(fs.readFileSync(process.argv[1], 'utf8'));

const out = [];
const check = (name, value) => out.push((value ? 'PASS ' : 'FAIL ') + name);
const state = {};
let content = '';
const apply = (_state, event) => { content += event.delta || ''; return true; };
paperIngestEvent(state, { seq: 0, type: 'delta', delta: 'A' }, apply);
paperIngestEvent(state, { seq: 0, type: 'delta', delta: 'A' }, apply);
paperIngestEvent(state, { seq: 1, type: 'delta', delta: 'B' }, apply);
check('exactly_once', content === 'AB');

let seen = 0;
paperAttachPush(state, 'task-1', { onEvent: () => { seen++; } });
paperAttachPush(state, 'task-1', { onEvent: () => { seen++; } });
check('idempotent_attach', subscriptions.length === 1);
subscriptions[0].handler({ seq: 2, type: 'delta' });
check('frame_delivered', seen === 1);
subscriptions[0].handler({ seq: 3, type: 'done' });
check('terminal_detaches', subscriptions.length === 0);

const sharedA = {};
const sharedB = {};
paperAttachPush(sharedA, 'same-task', { onEvent() {} });
paperAttachPush(sharedB, 'same-task', { onEvent() {} });
paperDetachPush(sharedA);
check('exact_handler_detach', subscriptions.length === 1);
paperDetachPush(sharedB);
check('all_released', subscriptions.length === 0);

const resetState = { _seqSeen: 99 };
let resetContent = '';
const resetApply = (_state, event) => {
  resetContent += event.delta || '';
  return true;
};
const resetPage = taskReplayIngestPage(resetState, {
  events: [
    { seq: 5, type: 'delta', delta: 'C' },
    { seq: 6, type: 'delta', delta: 'D' },
  ],
  next_cursor: 7,
  cursor: { requested: 99, next: 7, reset: true },
  status: 'running',
  done: false,
}, resetApply, 99);
check('cursor_reset_repairs_seq_gate', resetContent === 'CD' &&
  resetPage.cursorReset && resetPage.accepted === 2 && resetPage.nextCursor === 7);

const duplicatePage = taskReplayIngestPage(resetState, {
  events: [
    { seq: 5, type: 'delta', delta: 'C' },
    { seq: 6, type: 'delta', delta: 'D' },
  ],
  next_cursor: 7,
  cursor: { requested: 7, next: 7, reset: false },
}, resetApply, 7);
check('poll_push_duplicate_rejected', duplicatePage.accepted === 0 &&
  resetContent === 'CD');

const futureReset = taskReplayIngestPage(resetState, {
  events: [], next_cursor: 7,
  cursor: { requested: 99, next: 7, reset: true },
}, resetApply, 99);
paperIngestEvent(resetState, { seq: 7, type: 'delta', delta: 'E' }, resetApply);
check('empty_reset_allows_future_event', futureReset.cursorReset &&
  resetContent === 'CDE');

const legacyPage = taskReplayIngestPage(resetState, {
  events: [], cursor: 11, status: 'done', done: true,
}, resetApply, 7);
check('legacy_numeric_cursor', legacyPage.nextCursor === 11 &&
  legacyPage.done && resetState._replayCursor === 11);
console.log(out.join('\n'));
"""


@pytest.mark.skipif(not shutil.which('node') or not os.path.isfile(ESBUILD),
                    reason='node + esbuild dev-deps not installed')
def test_native_push_transport_contract(tmp_path):
    assert not os.path.exists(CLASSIC_SOURCE)
    built = tmp_path / 'push-transport.js'
    compiled = compile_feature_owner(ESBUILD, SOURCE, built, tmp_path)
    assert compiled.returncode == 0, compiled.stderr
    proc = subprocess.run(
        ['node', '-e', _HARNESS, str(built)],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    failures = [line for line in proc.stdout.splitlines()
                if line.startswith('FAIL')]
    assert not failures, proc.stdout
    assert proc.stdout.count('PASS') == 10
