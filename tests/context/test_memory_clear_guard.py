"""Bulk-memory policy runs before storage, independently of route decorators.

The frontend's old source-string guard could not prove either server refusal.
Exercise the real route body here; authentication middleware has its own suite.
"""
import inspect

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('multi_user,confirm,preview,expected', [
    (True, True, False, 403), (True, True, True, 403),
    (False, False, False, 400), (False, 1, False, 400),
    (False, True, False, 200), (False, None, True, 200),
])
def test_bulk_memory_guard_precedes_storage(monkeypatch, multi_user, confirm, preview, expected):
    import lib.auth_mode
    import lib.memory.storage
    from routes.api_v1 import memory

    calls = []
    monkeypatch.setattr(lib.auth_mode, 'is_multi_user', lambda: multi_user)
    monkeypatch.setattr(memory, 'parse_body', lambda **kwargs: {'confirm': confirm})
    monkeypatch.setattr(memory, '_project_path', lambda: '/synthetic/project')
    monkeypatch.setattr(memory, 'api_forbidden', lambda message: (message, 403))
    monkeypatch.setattr(memory, 'api_bad_request', lambda message: (message, 400))
    monkeypatch.setattr(memory, 'api_ok', lambda result: (result, 200))

    def clear(**kwargs):
        calls.append(kwargs)
        return {'deleted_ids': [], 'failed_ids': []}

    monkeypatch.setattr(lib.memory.storage, 'clear_memories', clear)
    route = memory.preview_clear_memories_v1 if preview else memory.clear_memories_v1
    result = inspect.unwrap(route)()
    assert result[1] == expected
    assert calls == ([{'project_path': '/synthetic/project', 'dry_run': preview}]
                     if expected == 200 else [])
