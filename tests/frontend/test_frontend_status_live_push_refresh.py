"""A project push frame refreshes every Project Brain evidence lane."""
import json
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
BRAIN_JS = REPO / "frontend/src/runtime/sections/project-brain.js"


def _extract_fn(src: str, name: str) -> str:
    match = re.search(r"(?:async\s+)?function %s\s*\(" % re.escape(name), src)
    assert match, f"{name} not found"
    start = src.index("{", match.start())
    depth = 0
    for index in range(start, len(src)):
        if src[index] == "{":
            depth += 1
        elif src[index] == "}":
            depth -= 1
            if depth == 0:
                return src[match.start():index + 1]
    raise AssertionError(f"unbalanced braces extracting {name}")


def _run(subscribe_source: str, refresh_source: str) -> dict:
    script = r'''
const calls = [];
const _state = { path: '/proj/x', pushHandler: null, pushKey: '' };
let handler = null;
function pushSubscribe(channel, key, fn) { handler = fn; }
function pushUnsubscribe() {}
function projectKeyHash(path) { return path; }
__REFRESH__
__SUBSCRIBE__
_subscribe('/proj/x');
if (!handler) throw new Error('no project push handler');
handler({ type: 'project_brain_changed' });
console.log(JSON.stringify({ calls }));
'''.replace('__SUBSCRIBE__', subscribe_source).replace('__REFRESH__', refresh_source)
    result = subprocess.run(['node', '-e', script], cwd=REPO,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_push_frame_refreshes_status_and_all_live_columns():
    calls = _run(
        _extract_fn(BRAIN_JS.read_text(), '_subscribe'),
        'function refreshAll(path) { calls.push(path); }',
    )['calls']
    assert calls == ['/proj/x']


def test_neuter_removing_refresh_status_goes_stale():
    source = _extract_fn(BRAIN_JS.read_text(), '_subscribe')
    neutered = source.replace('refreshAll(path);', '')
    assert neutered != source
    assert _run(neutered, 'function refreshAll(path) { calls.push(path); }')['calls'] == []
