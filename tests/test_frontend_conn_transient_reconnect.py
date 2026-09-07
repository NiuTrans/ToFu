"""Typed connection-health ownership contracts.

Transport health is presentation state only. A quiet task stream cannot
override a heartbeat-backed Conversation Sync state or settle a Turn.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
MODULE = native_module_path(
    'connection-health.js',
    ROOT / 'frontend' / 'src' / 'core' / 'connection-health.ts',
)
NODE = shutil.which('node')


@pytest.mark.skipif(not NODE, reason='node not available')
def test_connection_health_has_one_typed_non_terminal_owner():
    harness = r"""
const fs = require('fs');
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));

const store = new ConnectionHealthStore();
const direct = [];
const aggregate = [];
const stopDirect = store.subscribe('conv-a', value => direct.push(value.state));
const stopAggregate = store.subscribeAggregate(value =>
  aggregate.push({degraded:value.degraded, count:value.count}));

store.set('conv-a', {
  state:'live', transport:'conversation-sse', observedAt:1,
  generation:1, retryCount:0,
});
store.setTaskStreamDegraded('conv-a', true);
const coordinatorWins = store.get('conv-a');

store.setTaskStreamDegraded('task-only', true);
const taskHealth = store.get('task-only');
const frozen = Object.isFrozen(taskHealth);
store.setTaskStreamDegraded('task-only', true);
const duplicateSuppressed = aggregate.length;
store.clear('task-only');
store.set('conv-a', {
  state:'offline', transport:'conversation-sse', observedAt:2,
  generation:2, retryCount:1, reason:'heartbeat-expired',
});
const finalAggregate = store.aggregate();
stopDirect();
stopAggregate();

process.stdout.write(JSON.stringify({
  coordinatorWins: {
    state:coordinatorWins.state,
    transport:coordinatorWins.transport,
  },
  taskHealth: {
    state:taskHealth.state,
    transport:taskHealth.transport,
    reason:taskHealth.reason,
  },
  frozen,
  direct,
  duplicateSuppressed,
  finalAggregate: {
    degraded:finalAggregate.degraded,
    count:finalAggregate.count,
  },
}));
"""
    result = subprocess.run(
        [NODE, '-e', harness, MODULE],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload['coordinatorWins'] == {
        'state': 'live',
        'transport': 'conversation-sse',
    }
    assert payload['taskHealth'] == {
        'state': 'degraded',
        'transport': 'task-sse',
        'reason': 'task-stream-silence',
    }
    assert payload['frozen'] is True
    assert payload['direct'] == ['live', 'offline']
    assert payload['finalAggregate'] == {'degraded': True, 'count': 1}
    assert payload['duplicateSuppressed'] >= 3
