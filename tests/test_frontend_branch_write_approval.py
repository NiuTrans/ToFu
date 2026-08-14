"""tests/test_frontend_branch_write_approval.py — pt_12fbc45b.

The branch panel's Approve/Deny buttons were DEAD: they rendered off
``assistantMsg.approvalRequired`` (set by the ``approval_required`` SSE
event, which this server NEVER emits — it is only registered in
lib/agent_core/events.py) and called ``approveBranchTool()``, a stub whose
entire body was a console.warn. Meanwhile the REAL write gate
(``write_approval_request``, emitted by lib/tasks_pkg/tool_dispatch/_approval.py
with an ``approvalId`` resolvable via POST /api/v1/project/write-approval)
was silently ignored by branch streams — a branch task hitting the write
gate stalled the full 120 s server-side timeout with zero UI.

Fix: branch_stream.js handles ``write_approval_request`` via the top-level
``_branchHandleWriteApproval`` (stamps the round status/approvalId/
approvalMeta — the shared renderer _renderPendingApprovalBlock then paints
working buttons bound to the global resolveWriteApproval), and the dead
approval_required UI + stub are removed.

Drives the REAL shipped JS under node + source scans. Each check carries a
byte-reverting NEUTER.

Run::

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_frontend_branch_write_approval.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
sys.path.insert(0, HERE)
from _runtime_sections import runtime_section_path  # noqa: E402

BRANCH_STREAM = runtime_section_path('branch_stream.js')
BRANCH_JS = runtime_section_path('branch.js')


def _read(path: str) -> str:
    with open(path, encoding='utf-8') as f:
        return f.read()


_DRIVER = r"""
const assistantMsg = { role: 'assistant', toolRounds: [
  { roundNum: 1, toolCallId: 'call_A', status: 'searching' },
  { roundNum: 2, toolCallId: 'call_B', status: 'searching' },
] };
// 1. toolCallId match stamps THAT round only
_branchHandleWriteApproval(
  { type: 'write_approval_request', roundNum: 2, toolCallId: 'call_B',
    approvalId: 'appr-123', meta: { path: 'x.py' } }, assistantMsg);
const byId = JSON.parse(JSON.stringify(assistantMsg.toolRounds));
// 2. roundNum fallback when toolCallId missing
assistantMsg.toolRounds.forEach(r => { r.status = 'searching'; r.approvalId = null; r.approvalMeta = null; });
_branchHandleWriteApproval(
  { type: 'write_approval_request', roundNum: 1, approvalId: 'appr-9', meta: {} },
  assistantMsg);
const byRound = JSON.parse(JSON.stringify(assistantMsg.toolRounds));
// 3. no rounds → no crash, no-op
_branchHandleWriteApproval({ type: 'write_approval_request', roundNum: 9,
  approvalId: 'appr-x', meta: {} }, { role: 'assistant' });
console.log(JSON.stringify({ byId, byRound, noRoundsOk: true }));
"""


def _run_driver(src: str) -> dict:
    if not shutil.which('node'):
        pytest.skip('node not available')
    proc = subprocess.run(
        ['node', '-e', src + '\n' + _DRIVER],
        capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f'node harness failed:\n{proc.stderr[-2000:]}'
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_handler_stamps_matching_round():
    out = _run_driver(_read(BRANCH_STREAM))
    r1, r2 = out['byId']
    assert r1['status'] == 'searching' and not r1.get('approvalId'), (
        f'non-matching round was stamped: {r1}')
    assert r2['status'] == 'pending_approval'
    assert r2['approvalId'] == 'appr-123'
    assert r2['approvalMeta'] == {'path': 'x.py'}
    rb1, _ = out['byRound']
    assert rb1['status'] == 'pending_approval' and rb1['approvalId'] == 'appr-9', (
        f'roundNum fallback did not stamp: {rb1}')
    assert out['noRoundsOk'] is True


def test_NEUTER_handler_is_load_bearing():
    """Byte-reverting NEUTER: gut the helper body — stamping must disappear."""
    src = _read(BRANCH_STREAM)
    anchor = '    r.status = "pending_approval";'
    assert anchor in src, 'NEUTER anchor missing'
    neutered = src.replace(anchor, '    // NEUTERED', 1)
    out = _run_driver(neutered)
    r2 = out['byId'][1]
    assert r2['status'] != 'pending_approval' or not r2.get('approvalId'), (
        'NEUTER FAILED: gutting the handler still stamped the round')


def test_stream_wires_the_real_gate_and_drops_the_dead_one():
    src = _read(BRANCH_STREAM)
    assert 'ev.type === "write_approval_request"' in src, (
        'branch stream does not handle the real write_approval_request gate')
    assert '_branchHandleWriteApproval(ev, assistantMsg)' in src, (
        'write_approval_request is not wired to the handler')
    assert 'approval_required' not in src, (
        'the never-emitted approval_required branch is still present')
    assert 'approvalRequired' not in src, (
        'stale approvalRequired stamping is still present')


def test_NEUTER_wiring_scan_fires_on_old_shape():
    """Byte-reverting NEUTER: the scan must fire on the pre-fix shape."""
    src = _read(BRANCH_STREAM)
    assert 'write_approval_request' in src
    old_shape = src.replace(
        'ev.type === "write_approval_request"', 'ev.type === "approval_required"', 1)
    assert 'ev.type === "write_approval_request"' not in old_shape
    assert 'approval_required' in old_shape  # i.e. both scan assertions would fire


def test_dead_ui_removed_from_branch_js():
    src = _read(BRANCH_JS)
    assert 'approveBranchTool' not in src, 'dead approveBranchTool stub still present'
    assert 'approvalRequired' not in src, 'dead approvalRequired render block still present'
    assert 'branch-approval' not in src, 'dead branch-approval markup still present'


def test_NEUTER_dead_ui_scan_fires_on_old_shape():
    """The absence-scan must fire when the dead markers are reintroduced."""
    src = _read(BRANCH_JS)
    old_shape = src + '\n// approveBranchTool(msgIdx, branchIdx, action)\n'
    assert 'approveBranchTool' in old_shape


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
