"""Pre-publication authority and post-commit projections keep write order."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import threading

import pytest

from lib import file_history
from lib.file_history import store as history_store
from lib.project_mod import modifications as mods, post_commit, session_lock as locks
from lib.project_mod.write_tools import _ops

pytestmark = pytest.mark.unit


@pytest.fixture
def project(tmp_path, monkeypatch):
    base = tmp_path / 'project'
    base.mkdir()
    (base / 'first.txt').write_bytes(b'A')
    monkeypatch.setattr(mods, 'SESSIONS_DIR', str(tmp_path / 'sessions'))
    monkeypatch.setattr(mods, '_live_change_listeners', [])
    monkeypatch.setenv('TOFU_FILE_HISTORY', '1')
    monkeypatch.setattr(_ops, '_should_record_modification', lambda *a, **kw: True)
    monkeypatch.setattr(_ops, '_mod_attribution', lambda target, base, path, **kw: (base, path))
    monkeypatch.setattr(_ops, '_resolve_write_path', lambda base, path, **kw: os.path.join(base, path))
    monkeypatch.setattr(_ops, '_touch_for_vscode', lambda *a: None)
    monkeypatch.setattr(_ops, '_record_write_freshness', lambda *a, **kw: None)
    return str(base)


def write(base, content, task):
    return _ops.tool_write_file(base, 'first.txt', content, conv_id='conv', task_id=task)


@pytest.mark.parametrize('invalid', ['{broken', '[]', '{"modifications":{}}', '{"modifications":[null]}'])
@pytest.mark.parametrize('operation', ['write', 'edit', 'upload'])
def test_corrupt_journal_rejects_before_target_or_intent_mutation(project, invalid, operation):
    session = Path(mods._get_session_dir(project))
    journal = session / 'modifications.json'
    journal.write_text(invalid)
    if operation == 'write':
        result = write(project, 'B', 'task')
    elif operation == 'upload':
        result = _ops.save_uploaded_file(project, 'first.txt', b'B', on_conflict='overwrite')
    else:
        result = _ops.tool_edit_file(project, [{'path': 'first.txt', 'operation': 'replace',
                                               'anchor': 'A', 'content': 'B'}])
    if isinstance(result, dict):
        assert result['ok'] is False
        assert not result.get('mutationApplied')
        assert result.get('errorKind') != 'undo-record-failed'
    else:
        assert 'FAIL' in result
    assert Path(project, 'first.txt').read_bytes() == b'A'
    assert journal.read_text() == invalid
    assert not list(session.glob('write-intents-v1/*.json'))
    assert not list(Path(project).glob('.tofu-write-*'))


def test_delayed_same_file_listener_cannot_regress_round_snapshot(project, monkeypatch):
    entered, release, second_committed = (threading.Event() for _ in range(3))
    flush = mods._flush_to_disk
    notifications = []

    def slow_listener(task, entry):
        if task == 'first':
            assert locks.session_state(mods._get_session_dir(project)) is None
            entered.set()
            assert release.wait(10)
        notifications.append(task)

    def observe_commit(session, rows):
        flush(session, rows)
        if rows[-1]['taskId'] == 'second':
            second_committed.set()

    monkeypatch.setattr(mods, '_flush_to_disk', observe_commit)
    monkeypatch.setattr(mods, '_live_change_listeners', [slow_listener])
    monkeypatch.setattr(locks, '_wait_seconds', lambda: 0.05)

    def second_round():
        result = write(project, 'C', 'second')
        snapshot = file_history.make_snapshot(project, task_id='second', rel_paths=['first.txt'])
        return result, snapshot

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(write, project, 'B', 'first')
        try:
            assert entered.wait(10)
            second = pool.submit(second_round)
            assert second_committed.wait(10), 'presentation listener blocked durable journal admission'
            assert Path(project, 'first.txt').read_bytes() == b'C'
            assert len(mods.get_modifications(project)) == 2
            assert not second.done(), 'same-file callback overtook earlier notification'
        finally:
            release.set()
        assert first.result(timeout=5)['ok']
        result, snapshot = second.result(timeout=5)
        assert result['ok'] and snapshot
    assert notifications == ['first', 'second']
    tracked = history_store.load_tracked(project)['first.txt']
    assert tracked['last_writer_task_id'] == 'second'
    assert history_store.read_blob(project, 'first.txt', tracked['latest_version']) == b'C'
    assert file_history.make_snapshot(project, task_id='later')
    assert history_store.load_tracked(project)['first.txt'] == tracked
    assert post_commit._lanes == {}


def test_reentrant_listener_delivers_complete_older_notification_first(project, monkeypatch):
    seen = []

    def first_listener(task, entry):
        seen.append(('first-listener', task))
        if task == 'first':
            assert write(project, 'C', 'reentrant')['ok']

    def second_listener(task, entry):
        seen.append(('second-listener', task))

    monkeypatch.setattr(mods, '_live_change_listeners', [first_listener, second_listener])
    assert write(project, 'B', 'first')['ok']
    assert seen == [('first-listener', 'first'), ('second-listener', 'first'),
                    ('first-listener', 'reentrant'), ('second-listener', 'reentrant')]
    assert Path(project, 'first.txt').read_bytes() == b'C'
    tracked = history_store.load_tracked(project)['first.txt']
    assert tracked['last_writer_task_id'] == 'reentrant'
    assert post_commit._lanes == {}


def test_ready_later_ticket_cannot_overtake_unreleased_registration(tmp_path):
    first = post_commit.register(str(tmp_path), 'same', lambda: seen.append('first'))
    second = post_commit.register(str(tmp_path), 'same', lambda: seen.append('second'))
    entered = threading.Event()
    seen = []

    def publish_second():
        entered.set()
        post_commit.publish([second])

    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(publish_second)
        try:
            assert entered.wait(5)
            assert seen == []
        finally:
            post_commit.publish([first])
        waiting.result(timeout=5)
    assert seen == ['first', 'second']
    assert post_commit._lanes == {}


def test_cross_path_reentrant_callbacks_do_not_deadlock(tmp_path):
    barrier = threading.Barrier(2)
    seen = []
    guard = threading.Lock()

    def callback(name, other):
        barrier.wait(timeout=5)
        nested = post_commit.register(str(tmp_path), other, lambda: seen.append(name))
        post_commit.publish([nested])

    left = post_commit.register(str(tmp_path), 'left', lambda: callback('from-left', 'right'))
    right = post_commit.register(str(tmp_path), 'right', lambda: callback('from-right', 'left'))
    errors = []

    def run(ticket):
        try:
            post_commit.publish([ticket])
        except BaseException as exc:
            with guard:
                errors.append(exc)

    threads = [threading.Thread(target=run, args=(ticket,), daemon=True) for ticket in (left, right)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(8)
    assert not any(thread.is_alive() for thread in threads), 'cross-path reentrant wait cycle'
    assert not errors
    assert sorted(seen) == ['from-left', 'from-right']
    assert post_commit._lanes == {}


@pytest.mark.skipif(not hasattr(os, 'fork'), reason='requires POSIX fork/flock')
def test_cross_process_history_staging_retains_journal_commit_order(project, monkeypatch):
    import multiprocessing

    ctx = multiprocessing.get_context('fork')
    entered, release = ctx.Event(), ctx.Event()
    track_edit = file_history.track_edit
    monkeypatch.setattr(locks, '_wait_seconds', lambda: 0.04)

    def slow_first_history(base, path, **kwargs):
        if kwargs['task_id'] == 'first':
            assert locks.session_state(mods._get_session_dir(base)) is not None
            entered.set()
            assert release.wait(10)
        return track_edit(base, path, **kwargs)

    monkeypatch.setattr(file_history, 'track_edit', slow_first_history)
    children = []
    pipes = []

    def start(content, task, snapshot=False):
        receive, send = ctx.Pipe(duplex=False)
        pipes.extend((receive, send))

        def run():
            receive.close()
            try:
                result = write(project, content, task)
                if result.get('ok') and snapshot:
                    result['snapshot'] = file_history.make_snapshot(
                        project, task_id=task, rel_paths=['first.txt'])
                send.send(result)
            except BaseException as exc:
                send.send({'childError': repr(exc)})
            finally:
                send.close()

        child = ctx.Process(target=run, daemon=True)
        children.append(child)
        child.start()
        send.close()
        return child, receive

    def result(child, receive):
        assert receive.poll(10), 'child did not finish within bounded wait'
        value = receive.recv()
        child.join(5)
        assert child.exitcode == 0
        assert 'childError' not in value
        return value

    try:
        first, first_pipe = start('B', 'first')
        assert entered.wait(10)
        competitor, competitor_pipe = start('C', 'second')
        busy = result(competitor, competitor_pipe)
        assert busy['ok'] is False
        assert busy['errorKind'] == 'journal-busy'
        assert busy['mutationApplied'] is False
        assert Path(project, 'first.txt').read_bytes() == b'B'
        release.set()
        assert result(first, first_pipe)['ok']
        second, second_pipe = start('C', 'second', snapshot=True)
        committed = result(second, second_pipe)
        assert committed['ok'] and committed['snapshot']
        assert [row['taskId'] for row in mods.get_modifications(project)] == ['first', 'second']
        tracked = history_store.load_tracked(project)['first.txt']
        assert tracked['last_writer_task_id'] == 'second'
        assert history_store.read_blob(project, 'first.txt', tracked['latest_version']) == b'C'
        assert file_history.make_snapshot(project, task_id='later')
        assert history_store.load_tracked(project)['first.txt'] == tracked
    finally:
        release.set()
        for child in children:
            child.join(2)
            if child.is_alive():
                child.terminate()
                child.join(5)
        for pipe in pipes:
            pipe.close()
