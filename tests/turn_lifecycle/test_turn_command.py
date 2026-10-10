"""Continue/resume attempt commands must carry an integer input-projection fence."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from lib.conversation_sync.validation import ContractViolation, decode
from tests._runtime_sections import runtime_section


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_turn_command_builder_sends_integer_input_projection_fence():
    run = subprocess.run(
        ['node', '--test', 'tests/turn_command.test.cjs'],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_create_attempt_continue_with_ctx_update_accepts_integer_fence():
    body = {
        'commandId': 'continue:turn-a:2',
        'operation': 'continue',
        'expectedProjectionRevision': 2,
        'config': {'model': 'kimi-k3'},
        'inputUpdate': {
            'contextSnapshot': {
                'blockId': 'turn-context',
                'snapshot': {'model': 'kimi-k3'},
            },
        },
        'expectedInputProjectionRevision': 0,
    }
    assert decode('CreateAttemptRequest', body) == body


def test_create_attempt_rejects_null_input_projection_fence():
    with pytest.raises(ContractViolation) as caught:
        decode('CreateAttemptRequest', {
            'commandId': 'continue:turn-a:2',
            'operation': 'continue',
            'expectedProjectionRevision': 2,
            'config': {},
            'inputUpdate': {'contextSnapshot': {
                'blockId': 'turn-context',
                'snapshot': {'model': 'kimi-k3'},
            }},
            'expectedInputProjectionRevision': None,
        })
    assert any(
        'expectedInputProjectionRevision' in item and 'expected integer' in item
        for item in caught.value.violations
    )


def test_retained_resume_adapter_ships_parent_projection_fence():
    adapter = runtime_section('main/conversation_turn_store.js')
    assert 'expectedInputProjectionRevision: Number(inputTurn?.projectionRevision || 0)' in adapter
    assert "_nativeResumeCtxOptions(resolved, operation)" in adapter
    assert "_nativeResumeCtxOptions(resolved, 'answer_guidance')" in adapter
