"""Task ownership is explicit from creation through conv-scoped mutation."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def isolated_registry(monkeypatch):
    import lib.tasks_pkg.manager._registry as registry
    from lib.tasks_pkg.manager.runtime import chat_task_runtime

    before = set(chat_task_runtime.task_ids())
    monkeypatch.setattr(registry, "_upsert_task_row", lambda *_a, **_k: None)
    monkeypatch.setattr(
        registry, "_write_aborted_terminal_floor", lambda *_a, **_k: None
    )
    yield registry
    for task_id in set(chat_task_runtime.task_ids()) - before:
        chat_task_runtime.discard(task_id)


def test_create_task_requires_and_captures_positive_owner(isolated_registry):
    registry = isolated_registry
    with pytest.raises(ValueError, match="numeric user_id"):
        registry.create_task("conv-a", [], {})
    with pytest.raises(ValueError, match="positive user_id"):
        registry.create_task("conv-a", [], {}, user_id=0)

    task = registry.create_task(
        "conv-a",
        [{"role": "user", "content": "hello"}],
        {},
        user_id=41,
        supersede=False,
    )
    assert registry.task_user_id(task) == 41
    assert task["config"]["userId"] == 41
    assert task["_profileScope"] == "41"


def test_task_user_id_never_guesses_an_owner():
    from lib.tasks_pkg.manager._registry import task_user_id

    for value in (None, {}, {"_userId": ""}, {"_userId": 0}):
        with pytest.raises(ValueError):
            task_user_id(value)


def test_task_user_id_stamps_but_never_replaces_explicit_owner():
    from lib.tasks_pkg.manager._registry import task_user_id

    legacy_task = {'_userId': 9}
    assert task_user_id(legacy_task) == 9
    assert legacy_task['_principalContext']['owner_user_id'] == 9

    mismatched = {
        '_userId': 9,
        '_principalContext': {
            'kind': 'user', 'subject_id': 'user:10', 'owner_user_id': 10,
            'tenant_id': None, 'scopes': [],
        },
    }
    with pytest.raises(ValueError, match='does not match'):
        task_user_id(mismatched)


def test_conv_abort_is_scoped_by_owner(isolated_registry):
    registry = isolated_registry
    first = registry.create_task(
        "shared-conv", [], {}, user_id=41, supersede=False
    )
    second = registry.create_task(
        "shared-conv", [], {}, user_id=42, supersede=False
    )

    assert registry.abort_running_tasks_for_conv(
        "shared-conv", user_id=41
    ) == 1
    assert first["aborted"] is True
    assert second["aborted"] is False
