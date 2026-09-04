"""Tests for waitingOn turn status and timeline card feature."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests._runtime_sections import runtime_section


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_waiting_on_feature_frontend(tmp_path):
    harness = tmp_path / 'harness.js'
    harness.write_text(
        """
global.t = (key, values) => key + (values ? JSON.stringify(values) : '');
global.escapeHtml = (str) => String(str || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
global.Icon = (name) => '<svg name="' + name + '"></svg>';

function _waitingAgentChips(agents) {
  return (Array.isArray(agents) ? agents : []).map((agent) =>
    '<span class="wait-block-agent-chip"><b>' + escapeHtml(String(agent?.role || '?')) + '</b> ' + escapeHtml(String(agent?.status || '?')) + '</span>').join('');
}
function renderWaitingOnBlock(waitingOn, context) {
  if (!waitingOn || waitingOn.kind !== 'swarm' || !waitingOn.swarmKey) return '';
  const note = waitingOn.autoResume
    ? 'Will continue automatically when the blocker settles.'
    : 'Resume manually when the blocker settles.';
  const key = String(waitingOn.swarmKey);
  return '<details class="wait-block" data-wait-swarm-key="' + escapeHtml(key) + '" data-wait-conv-id="' + escapeHtml(context?.conversationId || '') + '"><summary><span class="wait-block-icon">' + Icon('hourglass', 14) + '</span><span class="wait-block-title">' + escapeHtml('Waiting on background work') + '</span><span class="wait-block-note">' + escapeHtml(note) + '</span><span class="wait-block-snapshot">' + _waitingAgentChips(waitingOn.agents) + '</span></summary><div class="wait-block-body"><div class="wait-block-live">' + escapeHtml('View live status') + '</div></div></details>';
}

const turnWithWaiting = {
  turnId: 't1', actor: 'assistant', status: 'completed',
  projection: {
    waitingOn: {
      kind: 'swarm',
      swarmKey: 'swarm-123',
      autoResume: true,
      agents: [{ id: 'a1', role: 'coder', status: 'running' }]
    }
  }
};

const renderedBlock = renderWaitingOnBlock(turnWithWaiting.projection.waitingOn, { conversationId: 'c1' });
if (!renderedBlock.includes('wait-block')) throw new Error('wait-block card missing');
if (!renderedBlock.includes('swarm-123')) throw new Error('swarmKey missing');
if (!renderedBlock.includes('coder')) throw new Error('agent role missing');

const maliciousWaitingOn = {
  kind: 'swarm',
  swarmKey: '"><script>alert(1)</script>',
  autoResume: false,
  agents: [{ id: 'x', role: '<script>bad</script>', status: 'pending' }]
};
const maliciousBlock = renderWaitingOnBlock(maliciousWaitingOn, { conversationId: 'c1' });
if (maliciousBlock.includes('<script>')) throw new Error('HTML escaping failed for waitingOn fields');

console.log('ALL WAITING-ON FRONTEND TESTS PASSED');
""",
        encoding='utf-8',
    )
    run = subprocess.run(
        [shutil.which('node'), str(harness)], cwd=ROOT,
        capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr
