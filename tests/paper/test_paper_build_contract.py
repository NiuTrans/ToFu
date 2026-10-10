"""Executable paper-build wire constraints and generated consumer parity."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from lib.paper.builds.contract import validate
from lib.paper.builds.errors import BuildError

pytestmark = pytest.mark.unit


def test_generated_contract_is_current():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, 'scripts/generate_paper_build_contract.py', '--check'],
        cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, (
        f'paper build generated contract drifted:\n{result.stdout}{result.stderr}')


@pytest.mark.parametrize('payload', [
    {'source': 'local', 'conv_id': 'c', 'path': 'main.tex'},
    {'source': 'overleaf', 'conv_id': 'c', 'project_id': 'a' * 24},
])
def test_request_requires_owned_conversation_and_source_identity(payload):
    payload.update(request_id='edit-1', subscriber_id='viewer-1')
    assert validate(payload, 'PaperBuildRequestV1') is payload
    del payload['conv_id']
    with pytest.raises(BuildError):
        validate(payload, 'PaperBuildRequestV1')


@pytest.mark.parametrize('payload', [
    {'source': 'local', 'conv_id': 'c'},
    {'source': 'overleaf', 'conv_id': 'c'},
    {'source': 'local', 'conv_id': 'c', 'path': 'main.tex', 'owner_user_id': 2},
    {'source': 'local', 'conv_id': 'c', 'path': 'main.tex', 'needle': 'x' * 4001},
    {'source': 'local', 'conv_id': 'c', 'path': 'main.tex', 'line': True},
])
def test_request_rejects_ambiguous_source_spoofed_owner_and_unbounded_input(payload):
    payload.update(request_id='edit-1', subscriber_id='viewer-1')
    with pytest.raises(BuildError):
        validate(payload, 'PaperBuildRequestV1')
