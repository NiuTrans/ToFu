"""Static end-state guard for the single API-key authentication authority."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parent.parent


def test_retired_tunnel_auth_vocabulary_cannot_return():
    forbidden = (
        'TUNNEL_' + 'TOKEN',
        'X-' + 'Tunnel-Token',
        'via_' + 'tunnel_token',
        'tunnelToken' + 'Header',
    )
    command = ['rg', '-n', '--fixed-strings', '--glob', '!docs/archive/**']
    for token in forbidden:
        command.extend(('-e', token))
    command.extend(('lib', 'routes', 'docs', 'tests',
                    'server.py', '.env.example'))
    result = subprocess.run(
        command, cwd=ROOT, check=False, text=True, capture_output=True)
    assert result.returncode in (0, 1), result.stderr
    assert result.returncode == 1, (
        'Retired tunnel authentication resurfaced. API keys are the only '
        'credential authority; browsers transport the same key in the '
        'HttpOnly session cookie:\n' + result.stdout)
