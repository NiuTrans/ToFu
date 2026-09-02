"""All push consumers delegate to one fail-closed owner predicate."""

from __future__ import annotations

import json
import re
import subprocess

import pytest

from tests._runtime_sections import (
    runtime_section,
    runtime_section_names,
    runtime_section_path,
)


pytestmark = pytest.mark.unit


def _code_without_comments(source: str) -> str:
    return re.sub(r'/\*.*?\*/|//[^\n]*', '', source, flags=re.S)


def test_identity_comparison_has_one_implementation():
    readers = []
    for name in runtime_section_names():
        code = _code_without_comments(runtime_section(name))
        if '_currentUserId' in code:
            readers.append(name)
    assert readers == ['core/current_user.js', 'core/frame_identity.js']

    consumers = runtime_section('core/conversation_invalidation.js')
    assert consumers.count('_frameIsOurs(') == 3
    assert '_currentUserId' not in _code_without_comments(consumers)


def test_identity_gate_rejects_unresolved_unscoped_and_foreign_frames():
    source = runtime_section_path('core/frame_identity.js')
    script = r"""
const fs = require('fs');
global.window = global;
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));
const result = {};
window._currentUserId = null;
result.unresolvedScoped = _frameIsOurs(1);
result.unresolvedUnscoped = _frameIsOurs(null);
window._currentUserId = 7;
result.ownNumber = _frameIsOurs(7);
result.ownString = _frameIsOurs('7');
result.foreign = _frameIsOurs(8);
result.missingOwner = _frameIsOurs(undefined);
process.stdout.write(JSON.stringify(result));
"""
    proc = subprocess.run(
        ['node', '-e', script, source], text=True, capture_output=True,
        check=True, timeout=30,
    )
    assert json.loads(proc.stdout) == {
        'unresolvedScoped': False,
        'unresolvedUnscoped': False,
        'ownNumber': True,
        'ownString': True,
        'foreign': False,
        'missingOwner': False,
    }
