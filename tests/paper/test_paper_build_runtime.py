"""Preview burst coalescing, subscriber ownership and terminal task fencing."""
from __future__ import annotations

import threading
import time
import uuid

import pytest

from lib.paper.builds.compiler import CompiledBuild, artifact_metadata
from lib.paper.builds.errors import BuildError
from tests.support import paper_builds as build_fixtures

pytestmark = pytest.mark.unit
DOCUMENT = 'a' * 64
scheduler = build_fixtures.scheduler


def _start(scheduler, execute, revision='1', subscriber='viewer-a', owner=7, document=DOCUMENT):
    return scheduler.start(owner_user_id=owner, document_id=document,
                           request_key=revision, subscriber_id=subscriber, execute=execute)


def _wait(task):
    deadline = time.monotonic() + 5
    while task['status'] in ('pending', 'running'):
        assert time.monotonic() < deadline, task
        time.sleep(0.01)
    return task


def test_admission_and_retirement_follow_coalesced_work_not_subscribers(scheduler):
    started, release = threading.Event(), threading.Event()
    admitted, retired = [], []
    def start(key, subscriber='viewer'):
        return scheduler.start(owner_user_id=7, document_id=DOCUMENT, request_key=key,
            subscriber_id=subscriber, execute=_work(scheduler, started=started, release=release),
            on_admit=lambda: admitted.append(key), on_retire=lambda: retired.append(key))
    first = start('first')
    try:
        assert started.wait(3)
        assert start('first', 'second-viewer')['id'] == first['id']
        second = start('second')
        third = start('third')
        assert admitted == ['first', 'second', 'third']
        assert retired == ['second']
        assert second['status'] == 'aborted'
        assert scheduler.detach(third['id'], owner_user_id=7, subscriber_id='viewer')
        assert retired == ['second', 'third']
    finally:
        release.set()
    _wait(first)
    scheduler.shutdown()
    assert retired.count('first') == 1


def test_cache_preparation_failure_never_calls_durable_commit(scheduler, tmp_path, monkeypatch):
    from tests.paper.test_paper_build_cache import _build
    good = _build(tmp_path)
    scheduler.cache.publish(good)
    candidate = _build(tmp_path)
    committed = []
    def fail(*_a, **_kw):
        raise OSError('Injected cache copy failure')
    monkeypatch.setattr(scheduler.cache, 'publish', fail)
    task = _start(scheduler, lambda _task, publish: publish(candidate,
        commit=lambda: committed.append(True) or {'current': True, 'published': True}))
    assert _wait(task)['status'] == 'error'
    assert committed == []
    assert scheduler.cache.latest(7, DOCUMENT) == good.receipt


def test_retention_capacity_error_keeps_its_actionable_task_envelope(scheduler):
    from lib.storage.errors import StorageError
    def execute(_task, _publish):
        raise StorageError('database_busy', 'Retained manuscript capacity is full', retryable=True)
    task = _wait(_start(scheduler, execute))
    assert task['status'] == 'error'
    assert task['error']['kind'] == 'server_busy'
    assert task['error']['code'] == 'database_busy'
    assert task['error']['message'] == 'Retained manuscript capacity is full'


def _work(scheduler, *, started=None, release=None, executed=None, owner=7):
    def execute(task, publish):
        if executed is not None:
            executed.append(task['id'])
        if started is not None:
            started.set()
        if release is not None:
            assert release.wait(3)
        build_id = uuid.uuid4().hex
        with scheduler.cache.stage(owner, DOCUMENT, build_id) as stage:
            pdf = stage / 'document.pdf'
            pdf.write_bytes(b'%PDF-1.4\nfixture\n%%EOF\n')
            receipt = {'schema_version': 1, 'owner_user_id': owner, 'document_id': DOCUMENT,
                       'build_id': build_id, 'target': 'host', 'source_digest': 'b' * 64,
                       'version_verified': True, 'main_file': 'main.tex', 'engine': 'pdflatex',
                       'toolchain': 'fixture', 'status': 'success', 'started_at': 1,
                       'completed_at': 2, 'artifacts': [artifact_metadata(pdf)], 'diagnostics': []}
            return publish(CompiledBuild(receipt, stage))
    return execute


def test_edit_burst_executes_active_and_latest_only(scheduler):
    started, release = threading.Event(), threading.Event()
    executed = []
    first = _start(scheduler, _work(scheduler, started=started, release=release, executed=executed))
    assert started.wait(3)
    pending = _start(scheduler, _work(scheduler, executed=executed), '2')
    latest = _start(scheduler, _work(scheduler, executed=executed), '3')
    assert pending['status'] == 'aborted'
    release.set()
    _wait(first)
    _wait(latest)
    assert executed == [first['id'], latest['id']]
    assert first['result']['superseded'] is True
    assert latest['result']['superseded'] is False
    assert scheduler.cache.latest(7, DOCUMENT) == latest['result']['receipt']
    assert scheduler._documents == {}


def test_unexpected_adapter_failure_settles_and_releases_its_document_slot(scheduler):
    def broken(task, publish):
        raise OSError('injected adapter IO failure')
    first = _start(scheduler, broken)
    _wait(first)
    assert first['status'] == 'error'
    next_task = _start(scheduler, _work(scheduler), 'retry')
    _wait(next_task)
    assert next_task['status'] == 'done'
    assert scheduler.cache.latest(7, DOCUMENT) == next_task['result']['receipt']
    assert scheduler._documents == {}


def test_duplicate_request_joins_and_detach_preserves_other_viewer(scheduler):
    started, release = threading.Event(), threading.Event()
    work = _work(scheduler, started=started, release=release)
    task = _start(scheduler, work)
    assert started.wait(3)
    assert _start(scheduler, work, subscriber='viewer-b') is task
    for index in range(128):
        assert _start(scheduler, work, subscriber=f'burst-{index}') is task
    for index in range(128):
        assert scheduler.detach(task['id'], owner_user_id=7, subscriber_id=f'burst-{index}')
        assert not task['abort_event'].is_set()
    assert not scheduler.detach(task['id'], owner_user_id=8, subscriber_id='viewer-a')
    assert not scheduler.detach(task['id'], owner_user_id=7, subscriber_id='unknown')
    assert scheduler.detach(task['id'], owner_user_id=7, subscriber_id='viewer-a')
    assert not task['abort_event'].is_set()
    assert scheduler.detach(task['id'], owner_user_id=7, subscriber_id='viewer-b')
    release.set()
    assert _wait(task)['status'] == 'aborted'
    assert scheduler.cache.latest(7, DOCUMENT) is None
    # Reopening after cancellation creates usable work, not a dead subscription.
    retry = _start(scheduler, _work(scheduler))
    assert retry['id'] != task['id']
    assert _wait(retry)['status'] == 'done'


def test_detached_pending_never_executes(scheduler):
    started, release = threading.Event(), threading.Event()
    task = _start(scheduler, _work(scheduler, started=started, release=release))
    assert started.wait(3)
    pending = _start(scheduler, lambda *_: pytest.fail('detached work executed'), '2')
    scheduler.detach(pending['id'], owner_user_id=7, subscriber_id='viewer-a')
    assert pending['status'] == 'aborted'
    release.set()
    assert _wait(task)['result']['superseded'] is False


def test_different_owned_documents_start_above_old_queue_limit_and_identity_is_preserved(scheduler):
    started, release = threading.Event(), threading.Event()
    task = _start(scheduler, _work(scheduler, started=started, release=release))
    assert started.wait(3)
    queued = _start(scheduler, _work(scheduler, owner=8), owner=8)
    third_started = threading.Event()
    third = _start(scheduler, _work(scheduler, owner=9, started=third_started), owner=9)
    assert third_started.wait(1), 'another owner cannot be queued behind a fixed worker count'
    release.set()
    assert _wait(task)['status'] == _wait(queued)['status'] == _wait(third)['status'] == 'done'
    forged = _start(scheduler, _work(scheduler, owner=8), 'forged')
    assert _wait(forged)['status'] == 'error'
    assert 'different document owner' in str(forged.get('error'))


def test_worker_crash_does_not_orphan_latest_pending(scheduler):
    started, release = threading.Event(), threading.Event()
    def crash(*_):
        started.set()
        assert release.wait(3)
        raise RuntimeError('injected adapter crash')
    task = _start(scheduler, crash)
    assert started.wait(3)
    pending = _start(scheduler, _work(scheduler), '2')
    release.set()
    assert _wait(task)['status'] == 'error'
    assert _wait(pending)['status'] == 'done'
    assert scheduler._documents == {}


def test_expired_subscription_cancels_before_publication(scheduler):
    started, release = threading.Event(), threading.Event()
    task = _start(scheduler, _work(scheduler, started=started, release=release))
    assert started.wait(3)
    assert scheduler.renew(task['id'], owner_user_id=7, subscriber_id='viewer-a')
    assert not scheduler.renew(task['id'], owner_user_id=8, subscriber_id='viewer-a')
    task['_build_subscribers']['viewer-a'] = time.monotonic() - 1
    release.set()
    assert _wait(task)['status'] == 'aborted'
    assert scheduler.cache.latest(7, DOCUMENT) is None
