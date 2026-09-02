"""Typed tool rejections render by cause instead of by generic status."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._runtime_sections import runtime_section_path


pytestmark = pytest.mark.unit
HERE = Path(__file__).resolve().parent
TOOL_ROUNDS = Path(runtime_section_path('ui/tool_rounds.js'))
HARNESS = HERE / '_tool_rounds_wire_parity_harness.js'


def test_policy_block_is_not_rendered_as_a_fake_tool(tmp_path: Path):
    if shutil.which('node') is None:
        pytest.skip('node is required for the tool rejection renderer')
    reason = 'Project write blocked: approval required by project policy.'
    rounds = [
        {
            '_name': 'hallucinated',
            'status': 'rejected',
            'toolName': 'search_web',
            'toolContent': (
                'Error: `search_web` is not a real tool and was NOT executed.'),
            'rejection': {
                'kind': 'hallucinated',
                'attempted': 'search_web',
                'suggestions': ['web_search'],
            },
            'results': [],
            'roundNum': 1,
        },
        {
            '_name': 'policy',
            'status': 'rejected',
            'toolName': 'run_command',
            'query': 'python3 fix.py',
            'toolContent': reason,
            'rejection': {
                'kind': 'project_write_authorization_required',
                'tool': 'run_command',
                'reason': reason,
                'retryable': False,
            },
            'results': [],
            'roundNum': 2,
        },
    ]
    fixture = tmp_path / 'rounds.json'
    fixture.write_text(json.dumps(rounds), encoding='utf-8')
    process = subprocess.run(
        ['node', str(HARNESS), str(TOOL_ROUNDS), str(fixture)],
        capture_output=True, text=True, timeout=30,
    )
    assert process.returncode == 0, process.stderr
    rendered = json.loads(process.stdout)

    hallucinated = rendered[0]['html']
    assert 'not a real tool' in hallucinated
    assert 'ptool-reject-name' in hallucinated
    assert 'web_search' in hallucinated

    blocked = rendered[1]['html']
    assert 'blocked' in blocked
    assert 'not a real tool' not in blocked
    assert 'ptool-reject-name' not in blocked
    assert 'python3 fix.py' in blocked
    assert 'approval required' in blocked
