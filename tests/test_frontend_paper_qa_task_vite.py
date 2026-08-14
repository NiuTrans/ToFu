"""End-to-end task contract for the native Paper Q&A owner."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._esm_feature_harness import compile_feature_owner


pytestmark = pytest.mark.unit
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
SOURCE = os.path.join(
    ROOT, 'frontend', 'src', 'features', 'paper', 'qa.ts')
CLASSIC_PUSH = os.path.join(
    ROOT, 'static', 'js', 'paper', 'push_transport.js')
CLASSIC_QA = os.path.join(ROOT, 'static', 'js', 'paper', 'qa.js')
ESBUILD = os.path.join(ROOT, 'node_modules', '.bin', 'esbuild')


_HARNESS = r"""
const fs = require('fs');
global.window = global;
global.console = { log: console.log, warn() {}, debug() {}, error() {} };
global.setTimeout = global.window.setTimeout = (fn) => { fn(); return 1; };
global.clearTimeout = global.window.clearTimeout = () => {};
global.escapeHtml = (value) => String(value == null ? '' : value);
global.renderMarkdown = (value) => String(value || '');
global.renderToolRoundsHTML = () => '';
global.Icon = () => '';
global.t = (key) => key;
global.errorEnvelopeMessage = (error) => String(error || 'Error');
global.debugLog = () => {};

const input = { value: 'How does it work?', focus() {} };
global.document = {
  getElementById(id) { return id === 'paperQAInput' ? input : null; },
  createElement() { return { textContent: '', innerHTML: '' }; },
};
const subscriptions = [];
global.pushSubscribe = (channel, taskId, handler) => {
  subscriptions.push({ channel, taskId, handler });
};
global.pushUnsubscribe = (channel, taskId, handler) => {
  for (let i = subscriptions.length - 1; i >= 0; i--) {
    const row = subscriptions[i];
    if (row.channel === channel && row.taskId === taskId
        && (!handler || row.handler === handler)) subscriptions.splice(i, 1);
  }
};

eval(fs.readFileSync(process.argv[1], 'utf8'));
if (process.argv[2] !== '-') eval(fs.readFileSync(process.argv[2], 'utf8'));

global._paperQAHistory = [{ role: 'user', content: 'Earlier question' }];
global._paperQAStreaming = false;
global._paperQAAbort = null;
global._paperQAAbortRequested = false;
global._paperParsedText = 'paper body';
global._activePaperId = 'paper-1';
global._paperHash = 'hash-1';
global._i18nLang = 'zh';
global._paperReportModel = 'model-1';
global._paperFileName = 'Paper title';
global._saveCount = 0;
global._saveActivePaperState = () => { global._saveCount += 1; };
global._ensurePaperText = async () => true;

let startBody = null;
const pollCursors = [];
global.Api = {
  paper: {
    qaStart: async (body) => { startBody = body; return { ok: true, task_id: 'qa-1' }; },
    qaPoll: async (_taskId, cursor) => {
      pollCursors.push(cursor);
      // Push seq=0 arrives first. The overlapping poll page must reject it
      // and apply only seq=1, leaving exactly "AB".
      subscriptions[0].handler({ seq: 0, type: 'delta', delta: 'A' });
      return {
        ok: true, status: 200,
        json: async () => ({
          ok: true,
          events: [
            { seq: 0, type: 'delta', delta: 'A' },
            { seq: 1, type: 'delta', delta: 'B' },
          ],
          cursor: { requested: 0, next: 2, reset: false },
          status: 'done', done: true,
        }),
      };
    },
    qaAbort: async () => {},
  },
};

const checks = [];
const check = (name, value) => checks.push((value ? 'PASS ' : 'FAIL ') + name);

(async () => {
  await _sendPaperQuestion();
  const assistant = _paperQAHistory[_paperQAHistory.length - 1];
  check('request_identity', startBody.paper_hash === 'hash-1'
    && startBody.lang === 'zh' && startBody.model === 'model-1');
  check('request_question_and_text', startBody.question === 'How does it work?'
    && startBody.paper_text === 'paper body' && startBody.title === 'Paper title');
  check('history_excludes_new_question', startBody.history.length === 1
    && startBody.history[0].content === 'Earlier question');
  check('push_poll_exactly_once', assistant.content === 'AB');
  check('terminal_state', assistant.status === 'done');
  check('incremental_cursor', pollCursors.length === 1 && pollCursors[0] === 0);
  check('subscription_released', subscriptions.length === 0);
  check('finalized_once', _paperQAStreaming === false && _saveCount === 1
    && input.value === '');
  console.log(checks.join('\n'));
  process.exit(checks.some((line) => line.startsWith('FAIL')) ? 1 : 0);
})().catch((error) => { console.error(error); process.exit(2); });
"""


def _assert_contract(first: str, second: str) -> None:
    proc = subprocess.run(
        ['node', '-e', _HARNESS, first, second],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr + '\n' + proc.stdout
    failures = [line for line in proc.stdout.splitlines()
                if line.startswith('FAIL')]
    assert not failures, proc.stdout
    assert proc.stdout.count('PASS') == 8


@pytest.mark.skipif(not shutil.which('node') or not os.path.isfile(ESBUILD),
                    reason='node + esbuild dev dependency required')
def test_native_qa_task_owns_push_poll_contract(tmp_path):
    assert not os.path.exists(CLASSIC_PUSH)
    assert not os.path.exists(CLASSIC_QA)
    built = tmp_path / 'paper-qa.js'
    compiled = compile_feature_owner(ESBUILD, SOURCE, built, tmp_path)
    assert compiled.returncode == 0, compiled.stderr
    _assert_contract(str(built), '-')
