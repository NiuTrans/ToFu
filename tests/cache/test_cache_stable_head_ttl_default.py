"""The 1h stable-head cache TTL is opt-in; the default sends 5-minute markers.

Decision record: docs/LLM_COST_OPTIMIZATION.md, "Stable-head cache TTL". Each
fresh process resolves ``CACHE_EXTENDED_TTL`` once at import, so the default is
checked in a child interpreter with no env override and an empty data dir.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]

_PROBE = (
    "import json, lib\n"
    "from lib.llm.cache import add_cache_breakpoints\n"
    "body = {'model': 'claude-opus-5.5', 'messages': [\n"
    "    {'role': 'system', 'content': 'You are a helpful assistant.'},\n"
    "    {'role': 'user', 'content': 'Hello'}],\n"
    "  'tools': [{'type': 'function', 'function': {'name': 'read_files',\n"
    "    'description': 'Read files', 'parameters': {'type': 'object'}}}]}\n"
    "add_cache_breakpoints(body)\n"
    "markers = {\n"
    "  'system': body['messages'][0]['content'][-1].get('cache_control'),\n"
    "  'tool': body['tools'][-1]['function'].get('cache_control'),\n"
    "  'tail': body['messages'][-1]['content'][-1].get('cache_control')}\n"
    "print(json.dumps({'flag': lib.CACHE_EXTENDED_TTL, 'markers': markers}))\n"
)


def _resolve(tmp_path, **env):
    environment = {k: v for k, v in os.environ.items()
                   if k != 'CACHE_EXTENDED_TTL'}
    environment['TOFU_DATA_DIR'] = str(tmp_path / 'data')
    environment.update(env)
    completed = subprocess.run(
        [sys.executable, '-c', _PROBE], cwd=ROOT, env=environment,
        capture_output=True, text=True, timeout=60, check=False)
    assert completed.returncode == 0, completed.stderr[-2000:]
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_default_marks_every_claude_breakpoint_five_minutes(tmp_path):
    result = _resolve(tmp_path)
    assert result['flag'] is False
    assert result['markers'] == {
        'system': {'type': 'ephemeral'},
        'tool': {'type': 'ephemeral'},
        'tail': {'type': 'ephemeral'},
    }


def test_opt_in_keeps_the_one_hour_head_and_five_minute_tail(tmp_path):
    result = _resolve(tmp_path, CACHE_EXTENDED_TTL='1')
    assert result['flag'] is True
    assert result['markers'] == {
        'system': {'type': 'ephemeral', 'ttl': '1h'},
        'tool': {'type': 'ephemeral', 'ttl': '1h'},
        'tail': {'type': 'ephemeral'},
    }
