"""Autopilot's transient reducer preserves terminal tool rejection verdicts."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
BUNDLER = ROOT / 'scripts' / 'vite_test_bundle.mjs'
ENTRY = (
    ROOT / 'frontend/src/conversation/application/autopilot-vu-transient.ts')


def test_tool_complete_keeps_rejection_status_and_descriptor(tmp_path: Path):
    if shutil.which('node') is None:
        pytest.skip('node is required for the transient reducer test')
    bundle = tmp_path / 'autopilot-vu.cjs'
    built = subprocess.run(
        [str(BUNDLER), str(ENTRY), '--bundle', '--format=cjs',
         '--platform=node', f'--outfile={bundle}'],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert built.returncode == 0, built.stderr
    script = f"""
const feature = require({json.dumps(str(bundle))});
let turn = feature.createAutopilotVuTransientTurn({{
  conversationId:'conv-a', vuMsgId:'vu-1', timestamp:1,
}});
turn = feature.reduceAutopilotVuTransientTurn(turn, {{
  type:'autopilot_vu_event', vuMsgId:'vu-1', inner:{{
    type:'tool_start', roundNum:1, toolCallId:'call-1',
    toolName:'run_command', query:'python3 fix.py',
  }},
}}, 2);
const rejection = {{
  kind:'project_write_authorization_required', tool:'run_command',
  reason:'approval required', retryable:false,
}};
turn = feature.reduceAutopilotVuTransientTurn(turn, {{
  type:'autopilot_vu_event', vuMsgId:'vu-1', inner:{{
    type:'tool_complete', roundNum:1, toolCallId:'call-1',
    toolName:'run_command', status:'rejected',
    toolContent:'approval required', rejection,
  }},
}}, 3);
console.log(JSON.stringify(turn.projection.toolRounds[0]));
"""
    reduced = subprocess.run(
        ['node', '-e', script], cwd=ROOT,
        capture_output=True, text=True, timeout=30,
    )
    assert reduced.returncode == 0, reduced.stderr
    round_entry = json.loads(reduced.stdout.strip().splitlines()[-1])

    assert round_entry['status'] == 'rejected'
    assert round_entry['toolContent'] == 'approval required'
    assert round_entry['rejection']['kind'] == \
        'project_write_authorization_required'
