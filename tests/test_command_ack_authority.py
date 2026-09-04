"""Lost command ACKs are recovered from every authoritative command owner."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from tests._runtime_sections import native_module_path
from tests.test_frontend_authoritative_composer import _run_pipeline


pytestmark = pytest.mark.unit


def test_committed_attempt_command_id_consumes_the_duplicate_draft() -> None:
    """A normal turn is authoritative in attempts, not in the pending queue."""
    _run_pipeline(r"""
let authoritativeState = { queueItems: [], attemptsById: {} };
composerInput.value = 'committed before the ACK was delivered';
runtimeScope.ConversationTurnStore.ensureRuntimeStore = () => ({
  getState: () => authoritativeState,
});
runtimeScope.ConversationTurnStore.submitConversation = async (
  conv, payload, config, extra,
) => {
  authoritativeState = {
    queueItems: [],
    attemptsById: {
      'attempt-after-lost-ack': {
        attemptId: 'attempt-after-lost-ack',
        commandId: extra.commandId,
        status: 'running',
      },
    },
  };
  throw new TypeError('connection closed after durable commit');
};

await _submitComposerDraft();

check(composerInput.value === '',
  'authoritative attempt left a duplicate composer draft');
check(followLatestCalls === 1,
  'authoritative attempt recovery did not follow the accepted turn');
check(toastEntries.length === 0,
  'authoritative attempt recovery was presented as a send failure');
""")


def test_typed_runtime_recognizes_queue_and_attempt_command_owners() -> None:
    bundle = native_module_path(
        "command-authority-turn-runtime.js",
        "frontend/src/core/turn-runtime.ts",
    )
    source = r"""
      global.window = globalThis;
      require(BUNDLE);
      const conversationId = 'command-authority-contract';
      const runtime = window.createConversationTurnRuntime({
        api:{eventsUrl(){return '/unused';}},
      });
      const store = runtime.ensureRuntimeStore(conversationId);
      store.dispatch({type:'snapshot', snapshot:{
        conversationRevision:1,
        turns:[],
        attempts:[{
          attemptId:'attempt-1', conversationId, turnId:'turn-1',
          commandId:'attempt-command', taskId:'task-1', operation:'generate',
          status:'running', baseProjectionRevision:0, resumeAnchor:{}, createdAt:1,
        }],
        queueItems:[{queueId:'queue-1', sourceMessageId:'queue-command'}],
      }});
      const values = {
        attempt: runtime.hasAuthoritativeCommand(
          conversationId, 'attempt-command'),
        queue: runtime.hasAuthoritativeCommand(
          conversationId, 'queue-command'),
        missing: runtime.hasAuthoritativeCommand(
          conversationId, 'missing-command'),
      };
      runtime.disposeConversation(conversationId);
      console.log(JSON.stringify(values));
    """.replace("BUNDLE", json.dumps(bundle))
    completed = subprocess.run(
        [shutil.which("node"), "-e", source],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == {
        'attempt': True, 'queue': True, 'missing': False,
    }

