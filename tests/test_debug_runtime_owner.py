"""Behavior contracts for the typed eager debug/diagnostics owner."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
BUNDLER = ROOT / "scripts/vite_test_bundle.mjs"
SOURCE = ROOT / "frontend/src/core/debug-runtime-owner.ts"


@pytest.mark.skipif(not BUNDLER.is_file(), reason="Vite test bundler unavailable")
def test_debug_owner_bounds_evidence_cleans_up_and_fails_soft(tmp_path: Path) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    built = tmp_path / "debug-runtime-owner.cjs"
    compiled = subprocess.run(
        [
            str(BUNDLER), str(SOURCE), "--bundle", "--format=cjs",
            "--platform=node", f"--outfile={built}",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stderr
    harness = textwrap.dedent(
        r"""
        const assert = require('node:assert/strict');
        const debug = require(BUILT_PATH);
        const reports = [];
        const listeners = {};
        let removedListeners = 0;
        let visible = false;
        let appended = 0;
        let removedTextareas = 0;
        let copyThrows = false;
        let nativeWrite = null;
        const turnState = {
          turnsById: {
            turn: { actor: 'assistant' },
            virtual: { actor: 'virtual_user' },
          },
          attemptsById: {
            older: { turnId: 'turn', taskId: 'task-old', createdAt: 1 },
            newer: { turnId: 'turn', taskId: 'task-new', createdAt: 2 },
          },
        };
        const owner = debug.createDebugRuntimeOwner({
          now: () => 1_700_000_000_000,
          writeConsole: () => undefined,
          warnConsole: () => undefined,
          currentUrl: () => 'https://example.test/chat',
          userAgent: () => 'test-agent',
          conversationCount: () => 3,
          report: (payload) => { reports.push(payload); return Promise.reject(new Error('offline')); },
          subscribeError: (listener) => {
            listeners.error = listener;
            return () => { removedListeners += 1; delete listeners.error; };
          },
          subscribeUnhandledRejection: (listener) => {
            listeners.rejection = listener;
            return () => { removedListeners += 1; delete listeners.rejection; };
          },
          resolveClipboardWrite: () => nativeWrite,
          createClipboardTextarea: () => ({
            value: '', style: { cssText: '' }, select() {},
          }),
          appendClipboardTextarea: () => { appended += 1; },
          removeClipboardTextarea: () => { removedTextareas += 1; },
          executeClipboardCopy: () => { if (copyThrows) throw new Error('copy denied'); },
          activeConversationId: () => 'conversation',
          conversations: () => [{ id: 'conversation' }],
          config: () => ({ systemPrompt: 'system' }),
          visible: () => visible,
          setVisible: (value) => { visible = value; },
          readTurnState: () => turnState,
        });

        (async () => {
          owner.start();
          owner.start();
          assert.deepEqual(Object.keys(listeners).sort(), ['error', 'rejection']);
          listeners.error({ message: 'boom', error: { stack: 'x'.repeat(2000) } });
          listeners.rejection({ reason: { message: 'rejected', stack: 'stack' } });
          assert.equal(reports.length, 2);
          assert.equal(reports[0].extra.stack.length, 1000);

          for (let index = 0; index < 85; index += 1) {
            owner.debugLog(`line-${index}`);
          }
          assert.equal(owner.diagnosticRing.length, debug.DEBUG_RUNTIME_LIMITS.diagnosticLines);
          assert.match(owner.diagnosticRing[0], /line-5$/);
          owner.debugLog('warning reaches report', 'WARNING');
          assert.match(reports.at(-1).message, /^\[debugLog\]\[warn\]/);

          for (let index = 0; index < 70; index += 1) {
            owner.shellState.recordSnapshot('task-a', {
              kind: 'request', roundNum: index, messages: [`round-${index}`],
            });
            owner.shellState.recordSnapshot('task-a', {
              kind: 'state', roundNum: index, messages: [`state-${index}`],
            });
          }
          assert.equal(owner.shellState.requests['task-a'].roundOrder.length, 64);
          assert.equal(owner.shellState.requests['task-a'].states.length, 64);
          owner.shellState.recordSnapshot('task-b', {
            kind: 'request', roundNum: 1, messages: ['current'],
          });
          assert.equal(owner.shellState.requests['task-a'].states.at(-1).messages, null);
          assert.equal(owner.shellState.requests['task-a'].states.at(-1)._stripped, true);
          for (let index = 0; index < 22; index += 1) {
            owner.shellState.recordSnapshot(`task-${index}`, {
              kind: 'request', roundNum: 1,
            });
          }
          assert.equal(Object.keys(owner.shellState.requests).length, 20);
          for (let index = 0; index < 25; index += 1) {
            owner.shellState.cache[`conversation-${index}`] = { messages: [index] };
          }
          assert.equal(Object.keys(owner.shellState.cache).length, 20);
          assert.equal(owner.shellState.cache['conversation-0'], undefined);

          assert.equal(owner.taskIdForRound({ taskId: 'direct' }), 'direct');
          assert.equal(owner.taskIdForRound({ attemptId: 'older' }), 'task-old');
          assert.equal(owner.taskIdForRound({ _turnId: 'turn' }), 'task-new');
          assert.equal(owner.taskIdForRound({ _turnId: 'virtual' }), '');
          owner.shellState.visible = true;
          assert.equal(visible, true);

          const nativeValues = [];
          nativeWrite = async (text) => { nativeValues.push(text); };
          await owner.safeClipboardWrite('native');
          assert.deepEqual(nativeValues, ['native']);
          nativeWrite = async () => { throw new Error('denied'); };
          await owner.safeClipboardWrite('fallback');
          assert.equal(appended, 1);
          assert.equal(removedTextareas, 1);
          nativeWrite = null;
          copyThrows = true;
          await assert.rejects(owner.safeClipboardWrite('fail'));
          assert.equal(removedTextareas, 2, 'textarea cleanup survives copy failure');

          owner.dispose();
          owner.dispose();
          assert.equal(removedListeners, 2);
          assert.deepEqual(Object.keys(listeners), []);
          await Promise.resolve();
        })().catch((error) => { console.error(error); process.exitCode = 1; });
        """
    ).replace("BUILT_PATH", json.dumps(str(built)))
    result = subprocess.run(
        [node, "-e", harness],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
