"""Request-local preparation work stays bounded without stale cross-call caches."""
from __future__ import annotations

import hashlib
import json

import pytest

from lib.tasks_pkg.context_composer import ComposeRequest, render_context
from lib.tasks_pkg.context_composer import _providers

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('scope', ['', 'owner-a', 'owner-b'])
@pytest.mark.parametrize('empty', [False, True])
def test_profile_uses_one_owned_snapshot(monkeypatch, scope, empty):
    from lib.memory import user_profile as profile
    from lib.memory.user_profile import _context

    items = [] if empty else [
        {'type': 'identity', 'text': f'Identity {scope}'},
        {'type': 'work_rule', 'condition': 'editing', 'action': 'test\nthen report'},
        {'type': 'response_preference', 'text': 'Concise'},
    ]
    reads = []

    def load(owner=''):
        reads.append(owner)
        assert len(reads) == 1, 'prompt and event must not reread mutable storage'
        return {'items': items}

    monkeypatch.setattr(profile, 'load_context', load)
    monkeypatch.setattr(_context, 'load_context', load)
    task = {'_profileScope': scope}
    block = _providers._profile_block(ComposeRequest(task=task))
    body = profile.context_markdown(items)
    assert reads == [scope]
    assert block == (profile.render_profile_block(body) or '')
    if empty:
        assert '_appliedPreferences' not in task
    else:
        assert task['_appliedPreferences'] == {
            'chars': len(body),
            'items': [f'Identity {scope}', 'When editing → test\nthen report', 'Concise'],
        }


def test_disabled_profile_does_not_read(monkeypatch):
    from lib.memory import user_profile

    def forbidden(*args, **kwargs):
        pytest.fail('disabled preferences must not read')

    monkeypatch.setattr(user_profile, 'load_context', forbidden)
    assert _providers._profile_block(ComposeRequest(
        task={'config': {'preferencesEnabled': False}})) == ''


@pytest.mark.parametrize('schema', [None, [], [{'name': '工具', 'parameters': {'type': 'object'}}]])
def test_tool_serialization_once_and_epoch_hash_parity(monkeypatch, schema):
    from lib.tasks_pkg.context_composer import _render

    task = {} if schema is None else {'_tool_schema': schema}
    request = ComposeRequest(task=task, tool_names=frozenset({'z', 'a'}),
                             global_budget_tokens=1000)
    original_dumps = json.dumps
    calls = []

    def dumps(value, *args, **kwargs):
        if schema is not None and value is schema:
            calls.append(value)
        return original_dumps(value, *args, **kwargs)

    monkeypatch.setattr(_render.json, 'dumps', dumps)
    for iteration in range(3):
        if iteration == 2:
            task['_compactionBoundary'] = 'next'
            if schema:
                schema[0]['parameters']['description'] = 'changed'
        result = render_context([], [], request)
        canonical = (original_dumps(schema, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), default=str)
                     if schema is not None else 'a\x00z')
        digest = hashlib.sha256(canonical.encode()).hexdigest()[:24]
        assert result.plan.segment_hashes['toolManifest'] == digest
        state = {'permissions': [], 'tools': ['a', 'z'], 'toolManifestHash': digest,
                 'compactionBoundary': 'next' if iteration == 2 else ''}
        expected_key = hashlib.sha256(original_dumps(
            state, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:24]
        assert task['_contextCacheEpochKey'] == expected_key
        assert result.plan.cache_epoch == (1 if iteration == 2 else 0)
    assert len(calls) == (3 if schema is not None else 0)
