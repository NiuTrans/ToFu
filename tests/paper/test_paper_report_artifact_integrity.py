"""A canonical paper report is done only after durable publication."""

from __future__ import annotations

import uuid

import pytest


pytestmark = pytest.mark.unit
TEST_OWNER_USER_ID = 1
REPORT_BODY = '# Durable Report\n\n## ⚡ TL;DR\nA complete grounded report.'


@pytest.fixture(autouse=True)
def _cleanup_report_runtime():
    yield
    import lib.paper.report_runtime as runtime

    original_ttl = runtime._report_runtime.ttl
    runtime._report_runtime.ttl = -1
    try:
        runtime._cleanup_stale_report_tasks()
    finally:
        runtime._report_runtime.ttl = original_ttl


def _new_task():
    from lib.paper.report_runtime import _new_report_task

    suffix = uuid.uuid4().hex
    return _new_report_task(
        f'paper-report-integrity-{suffix}',
        f'paper-report-integrity-hash-{suffix}',
        'en',
        'model-a',
        config={
            'paperInsightEnabled': False,
            'paperTermfillEnabled': False,
            'paperCheckpointsEnabled': False,
        },
        user_id=TEST_OWNER_USER_ID,
    )


def _patch_terminal_report(monkeypatch, report_engine, body):
    def dispatch(_messages, *, on_content=None, **_kwargs):
        if body and on_content:
            on_content(body)
        return (
            {'role': 'assistant', 'content': body, 'tool_calls': []},
            'stop',
            {'_dispatch': {'model': 'model-a'}},
        )

    monkeypatch.setattr(report_engine, 'dispatch_stream', dispatch)
    monkeypatch.setattr(
        report_engine, 'lookup_paper_title', lambda *_args, **_kwargs: '')
    monkeypatch.setattr(
        report_engine, 'backfill_library_title', lambda *_args, **_kwargs: '')
    monkeypatch.setattr(report_engine, '_maybe_run_insight', lambda *_a, **_k: None)
    monkeypatch.setattr(report_engine, '_maybe_run_termfill', lambda *_a, **_k: None)


@pytest.mark.parametrize('repository_outcome', [
    OSError('storage unavailable'),
    False,
])
def test_persistence_failure_never_publishes_done(
    monkeypatch, repository_outcome,
):
    import lib.paper.report_engine.worker as report_engine

    class Repository:
        def __init__(self, owner_user_id):
            assert owner_user_id == TEST_OWNER_USER_ID

        def put_report(self, *_args, **_kwargs):
            if isinstance(repository_outcome, BaseException):
                raise repository_outcome
            return repository_outcome

    _patch_terminal_report(monkeypatch, report_engine, REPORT_BODY)
    monkeypatch.setattr(report_engine, 'PaperArtifactRepository', Repository)
    task = _new_task()

    report_engine.run_report_task(
        task,
        [
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': 'paper'},
        ],
        [],
    )

    assert task['status'] == 'error'
    assert task['result'] is None
    assert task['full_text'] == REPORT_BODY
    assert not any(event.get('type') == 'done' for event in task['events'])
    assert task['events'][-1]['type'] == 'error'


def test_empty_terminal_report_fails_before_repository_access(monkeypatch):
    import lib.paper.report_engine.worker as report_engine

    class ForbiddenRepository:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError('empty report reached repository boundary')

    _patch_terminal_report(monkeypatch, report_engine, '')
    monkeypatch.setattr(
        report_engine, 'PaperArtifactRepository', ForbiddenRepository)
    task = _new_task()

    report_engine.run_report_task(
        task,
        [
            {'role': 'system', 'content': 'system'},
            {'role': 'user', 'content': 'paper'},
        ],
        [],
    )

    assert task['status'] == 'error'
    assert task['result'] is None
    assert not any(event.get('type') == 'done' for event in task['events'])
    assert 'non-empty body' in task['error']['detail']
