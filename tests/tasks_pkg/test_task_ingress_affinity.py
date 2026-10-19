"""Task ingress affinity preserves owner/birth metadata without eager web load."""
import asyncio
import builtins
import json
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from lib.tasks_pkg.manager import _registry

pytestmark = pytest.mark.unit


def create(**options):
    return _registry.create_task('', [], {}, user_id=71, transient=True,
                                 supersede=False, **options)


def discard(task):
    _registry.chat_task_runtime.discard(task['id'])
    assert _registry.chat_task_runtime.get(task['id']) is None


def test_first_embedded_task_does_not_import_quart():
    script = '''
import builtins, sys, json
from lib.tasks_pkg.manager import create_task, task_user_id
from lib.tasks_pkg.manager.runtime import chat_task_runtime
assert 'quart' not in sys.modules
original = builtins.__import__
attempts = []
def import_checked(name, *args, **kwargs):
    if name == 'quart' or name.startswith('quart.'):
        attempts.append(name)
        raise ImportError('unexpected web initialization')
    return original(name, *args, **kwargs)
builtins.__import__ = import_checked
task = create_task('', [], {}, user_id=71, transient=True, supersede=False)
assert task_user_id(task) == 71
assert task['_transientRuntime'] and not task['_reconnectable']
assert not attempts and 'quart' not in sys.modules
chat_task_runtime.discard(task['id'])
assert chat_task_runtime.get(task['id']) is None
print(json.dumps({'quart_import_attempts': attempts, 'owner': 71}))
'''
    result = subprocess.run([sys.executable, '-c', script], check=True,
                            capture_output=True, text=True, timeout=60)
    assert json.loads(result.stdout) == {'quart_import_attempts': [], 'owner': 71}


def test_loaded_quart_without_request_is_not_reconnectable():
    import quart
    task = create()
    try:
        assert not task['_reconnectable']
        assert '_affinityKey' not in task
    finally:
        discard(task)


@pytest.mark.parametrize('value', ['', 'fixture-routing-key', 'x' * 500, '路径' * 200])
def test_actual_request_header_is_clipped_and_cannot_change_owner(value, tmp_path):
    from quart import Quart
    app = Quart('ingress-affinity-fixture', root_path=str(tmp_path))
    async def check():
        async with app.test_request_context('/', headers={
                'X-Tofu-Affinity-Key': value, 'X-User-Id': '999'}):
            task = create()
            try:
                assert task.get('_affinityKey', '') == value[:256]
                assert task['_reconnectable'] is bool(value)
                assert _registry.task_user_id(task) == 71
                assert task['config']['userId'] == 71
            finally:
                discard(task)
    asyncio.run(check())


def test_loaded_request_read_failure_keeps_non_reconnectable_fallback(monkeypatch):
    class FailedHeaders:
        def get(self, *args):
            raise RuntimeError('owned ingress fault')
    monkeypatch.setitem(sys.modules, 'quart', SimpleNamespace(
        has_request_context=lambda: True, request=SimpleNamespace(headers=FailedHeaders())))
    task = create()
    try:
        assert not task['_reconnectable']
        assert '_affinityKey' not in task
        assert _registry.task_user_id(task) == 71
    finally:
        discard(task)


@pytest.fixture
def durable_ports(monkeypatch):
    from lib.automations import events
    from lib.runtime import runtime_state_store
    from lib.tasks_pkg.manager import runtime
    values = {}; birth = []
    class Store:
        def get_value(self, kind, key):
            return values.get((kind, key))
        def set_value(self, kind, key, value, ttl):
            values[kind, key] = value
        def pop(self, kind, key):
            return values.pop((kind, key), None)
        def delete_value(self, kind, key):
            values.pop((kind, key), None)
    store = Store()
    monkeypatch.setattr(runtime_state_store, 'get_store', lambda: store)
    monkeypatch.setattr(events, 'task_start_context', lambda *a, **k: None)
    monkeypatch.setattr(_registry, '_upsert_task_row', lambda *a, **k: birth.append(k))
    conv = 'owned-affinity-' + uuid4().hex
    yield conv, values, birth
    runtime._clear_latest_task(conv)


def test_background_task_inherits_affinity_and_preserves_birth_metadata(durable_ports):
    conv, values, birth = durable_ports
    values['conv-affinity', conv] = 'stored-affinity'
    task = _registry.create_task(conv, [], {}, user_id=71, supersede=False)
    try:
        assert task['_affinityKey'] == 'stored-affinity'
        assert not task['_reconnectable']
        meta = json.loads(birth[0]['meta_json'])
        assert meta['affinityKey'] == 'stored-affinity' and meta['userId'] == '71'
        assert 'reconnectable' not in meta
    finally:
        discard(task)


def test_durable_request_affinity_is_mirrored_and_written_at_birth(durable_ports, tmp_path):
    from quart import Quart
    conv, values, birth = durable_ports
    app = Quart('durable-affinity-fixture', root_path=str(tmp_path))
    async def check():
        async with app.test_request_context('/', headers={'X-Tofu-Affinity-Key': 'direct-key'}):
            task = _registry.create_task(conv, [], {}, user_id=71, supersede=False)
            try:
                assert task['_affinityKey'] == 'direct-key' and task['_reconnectable']
                assert values['conv-affinity', conv] == 'direct-key'
                meta = json.loads(birth[0]['meta_json'])
                assert meta['affinityKey'] == 'direct-key' and meta['reconnectable'] is True
                assert meta['userId'] == '71'
            finally:
                discard(task)
    asyncio.run(check())
