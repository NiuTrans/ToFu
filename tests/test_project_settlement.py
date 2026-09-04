"""Project settlement delegates only to signal-driven work finalization."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.unit


def test_settlement_finishes_derived_work_without_dispatch(monkeypatch):
    from lib.conversations import project_brain
    from lib.conversations.project_settlement import on_project_task_settled

    calls = []
    monkeypatch.setattr(
        project_brain,
        'settle_work_item',
        lambda task, path: calls.append((task, path)) or 'completed',
    )
    task = {
        'id': 'task-a',
        'convId': 'conv-a',
        '_userId': 61,
        '_projectWorkId': 'pw-a',
    }

    on_project_task_settled(task, '/project/a', user_id=61)

    assert calls == [(task, '/project/a')]


def test_cancelled_settlement_uses_the_same_finalizer(monkeypatch):
    from lib.conversations import project_brain
    from lib.conversations.project_settlement import on_project_task_settled

    calls = []
    monkeypatch.setattr(
        project_brain,
        'settle_work_item',
        lambda task, path: calls.append((task['aborted'], path)) or 'cancelled',
    )

    on_project_task_settled(
        {'convId': 'conv-a', 'aborted': True},
        '/project/a',
        user_id=61,
    )

    assert calls == [(True, '/project/a')]


def test_settlement_failure_is_fail_soft(monkeypatch):
    from lib.conversations import project_brain
    from lib.conversations.project_settlement import on_project_task_settled

    def fail(*_args, **_kwargs):
        raise RuntimeError('boom')

    monkeypatch.setattr(project_brain, 'settle_work_item', fail)

    on_project_task_settled(
        {'convId': 'conv-a'}, '/project/a', user_id=61)


def test_missing_identity_skips_project_settlement(monkeypatch):
    from lib.conversations import project_brain
    from lib.conversations.project_settlement import on_project_task_settled

    monkeypatch.setattr(
        project_brain,
        'settle_work_item',
        lambda *_args, **_kwargs: pytest.fail('invalid identity was settled'),
    )

    on_project_task_settled({}, '/project/a', user_id=61)
    on_project_task_settled({'convId': 'conv-a'}, '', user_id=61)
