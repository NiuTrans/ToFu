"""All push consumers delegate to one fail-closed owner predicate."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

import pytest

from tests._runtime_sections import (
    native_module_path,
    runtime_section,
    runtime_section_names,
)


pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
OWNER = ROOT / 'frontend/src/core/frame-identity.ts'
OWNER_JS = native_module_path('.native/frame-identity-contract.js', OWNER)


def _code_without_comments(source: str) -> str:
    return re.sub(r'/\*.*?\*/|//[^\n]*', '', source, flags=re.S)


def test_identity_comparison_has_one_implementation():
    readers = []
    for name in runtime_section_names():
        code = _code_without_comments(runtime_section(name))
        if '_currentUserId' in code:
            readers.append(name)
    assert readers == []
    assert 'core/current_user.js' not in runtime_section_names()
    assert 'core/frame_identity.js' not in runtime_section_names()

    consumers = runtime_section('core/conversation_invalidation.js')
    assert consumers.count('_frameIsOurs(') == 3
    assert '_currentUserId' not in _code_without_comments(consumers)


def test_identity_gate_rejects_unresolved_unscoped_and_foreign_frames():
    source = OWNER.read_text(encoding='utf-8')
    assert '_currentUserId' not in source
    assert 'runtimeScope' not in source
    assert 'globalThis' not in source
    script = r"""
const fs = require('fs');
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));
const result = {};
result.unresolvedScoped = frameBelongsToOwner(null, 1);
result.unresolvedUnscoped = frameBelongsToOwner(null, null);
result.ownNumber = frameBelongsToOwner(7, 7);
result.ownString = frameBelongsToOwner(7, '7');
result.foreign = frameBelongsToOwner(7, 8);
result.missingOwner = frameBelongsToOwner(7, undefined);
result.objectIdentityRejected = frameBelongsToOwner({}, {});
result.nonFiniteRejected = frameBelongsToOwner(Infinity, Infinity);
process.stdout.write(JSON.stringify(result));
"""
    proc = subprocess.run(
        ['node', '-e', script, OWNER_JS], text=True, capture_output=True,
        check=True, timeout=30,
    )
    assert json.loads(proc.stdout) == {
        'unresolvedScoped': False,
        'unresolvedUnscoped': False,
        'ownNumber': True,
        'ownString': True,
        'foreign': False,
        'missingOwner': False,
        'objectIdentityRejected': False,
        'nonFiniteRejected': False,
    }
