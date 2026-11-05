"""The 1h stable-head cache TTL is opt-in; the default sends 5-minute markers.

Decision record: docs/LLM_COST_OPTIMIZATION.md, "Stable-head cache TTL".
``CACHE_EXTENDED_TTL`` is resolved at import and again by ``reload_config()``,
so one child interpreter with no env override and an empty data dir checks
both resolution sites. The opt-in 1h-head/5m-tail shape is pinned in-process
by tests/cache/test_cache_breakpoints.py.
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
    "def resolved():\n"
    "    body = {'model': 'claude-opus-5.5', 'messages': [\n"
    "        {'role': 'system', 'content': 'You are a helpful assistant.'},\n"
    "        {'role': 'user', 'content': 'Hello'}],\n"
    "      'tools': [{'type': 'function', 'function': {'name': 'read_files',\n"
    "        'description': 'Read files', 'parameters': {'type': 'object'}}}]}\n"
    "    add_cache_breakpoints(body)\n"
    "    return {'flag': lib.CACHE_EXTENDED_TTL, 'markers': {\n"
    "        'system': body['messages'][0]['content'][-1].get('cache_control'),\n"
    "        'tool': body['tools'][-1]['function'].get('cache_control'),\n"
    "        'tail': body['messages'][-1]['content'][-1].get('cache_control')}}\n"
    "at_import = resolved()\n"
    "lib.reload_config()\n"
    "print(json.dumps({'import': at_import, 'reload': resolved()}))\n"
)

_FIVE_MINUTE_DEFAULT = {
    'flag': False,
    'markers': {
        'system': {'type': 'ephemeral'},
        'tool': {'type': 'ephemeral'},
        'tail': {'type': 'ephemeral'},
    },
}


def test_default_marks_every_claude_breakpoint_five_minutes(tmp_path):
    environment = {k: v for k, v in os.environ.items()
                   if k != 'CACHE_EXTENDED_TTL'}
    environment['TOFU_DATA_DIR'] = str(tmp_path / 'data')
    # No local timeout: a cold `import lib` on a network mount can take over a
    # minute; the suite-wide pytest timeout bounds this test.
    completed = subprocess.run(
        [sys.executable, '-c', _PROBE], cwd=ROOT, env=environment,
        capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr[-2000:]
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result == {'import': _FIVE_MINUTE_DEFAULT,
                      'reload': _FIVE_MINUTE_DEFAULT}
