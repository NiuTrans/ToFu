"""Crash boundary evidence is bounded and never authorizes overwriting user edits."""
import base64
import json
import os
from pathlib import Path
import uuid

import pytest

from lib.project_mod import write_intents as intents

pytestmark = pytest.mark.unit


def stage(tmp_path, original=b'old\r\n\xff', updated=b'new\n'):
    base = tmp_path / 'project'
    base.mkdir()
    target = base / 'file.txt'
    if original is not None:
        target.write_bytes(original)
    staged = base / ('.tofu_atomic_' + uuid.uuid4().hex)
    with staged.open('wb') as handle:
        handle.write(updated)
        handle.flush()
        os.fsync(handle.fileno())
    return base, target, staged, dict(base_path=str(base), path='file.txt',
        original=original, post_content=updated, stage_name=staged.name,
        conv_id='conversation', task_id='task')


@pytest.mark.parametrize('original', [None, b'', b'old\r\n\xff'])
def test_durable_intent_survives_reopen_and_classifies_rename(tmp_path, original):
    base, target, staged, request = stage(tmp_path, original)
    session = str(tmp_path / 'session')
    with intents.locked_intents(session) as store:
        record = store.prepare(**request)
        assert intents.classify(record) == 'unpublished'
    with intents.locked_intents(session) as store:
        assert store.read() == [record]
        assert (None if record['original'] is None else base64.b64decode(record['original'])) == original
        os.replace(staged, target)
        assert intents.classify(record) == 'applied'
        assert target.read_bytes() == b'new\n'
        store.acknowledge(record)
        assert store.read() == []


@pytest.mark.parametrize('change', ['same-content-new-inode', 'later-edit', 'symlink', 'root-swap', 'missing-stage'])
def test_uncertain_evidence_is_preserved_without_mutation(tmp_path, change):
    base, target, staged, request = stage(tmp_path)
    with intents.locked_intents(str(tmp_path / 'session')) as store:
        record = store.prepare(**request)
        if change == 'missing-stage':
            staged.unlink()
        else:
            os.replace(staged, target)
            if change == 'same-content-new-inode':
                other = base / 'other'
                other.write_bytes(b'new\n')
                os.replace(other, target)
            elif change == 'later-edit':
                target.write_bytes(b'user edit')
            elif change == 'symlink':
                outside = tmp_path / 'outside'
                outside.write_bytes(b'new\n')
                target.unlink()
                target.symlink_to(outside)
            else:
                base.rename(tmp_path / 'moved')
                base.mkdir()
                target.write_bytes(b'new\n')
        before = target.read_bytes()
        assert intents.classify(record) == 'conflict'
        assert store.read() == [record]
        assert target.read_bytes() == before


def test_refuses_changed_original_before_intent_is_published(tmp_path):
    base, target, staged, request = stage(tmp_path)
    target.write_bytes(b'user change')
    with intents.locked_intents(str(tmp_path / 'session')) as store:
        with pytest.raises(intents.IntentError, match='Original file changed'):
            store.prepare(**request)
        assert store.read() == []
    assert target.read_bytes() == b'user change'


def test_prepare_requires_durable_record_before_caller_can_publish(tmp_path, monkeypatch):
    from lib.project_mod import modifications
    base, target, staged, request = stage(tmp_path)
    with intents.locked_intents(str(tmp_path / 'session')) as store:
        def fail(*args):
            raise OSError('injected journal flush failure')
        monkeypatch.setattr(modifications, '_atomic_json_write', fail)
        with pytest.raises(OSError, match='journal flush'):
            store.prepare(**request)
        assert target.read_bytes() == request['original']
        assert staged.exists()
        assert store.read() == []


@pytest.mark.parametrize('field', ['original', 'post_content'])
def test_byte_budget_refuses_before_persistence(tmp_path, field):
    base, target, staged, request = stage(tmp_path)
    request[field] = b'x' * (intents._limit('originalBytes') + 1)
    with intents.locked_intents(str(tmp_path / 'session')) as store:
        with pytest.raises(intents.IntentError, match='byte budget'):
            store.prepare(**request)
        assert store.read() == []


def test_pending_budget_never_evicts_recovery_evidence(tmp_path, monkeypatch):
    base, target, staged, request = stage(tmp_path)
    real_limit = intents._limit
    monkeypatch.setattr(intents, '_limit', lambda key: 1 if key == 'pendingIntents' else real_limit(key))
    with intents.locked_intents(str(tmp_path / 'session')) as store:
        record = store.prepare(**request)
        with pytest.raises(intents.IntentError, match='budget exhausted'):
            store.prepare(**request)
        assert store.read() == [record]


@pytest.mark.parametrize('malformed', ['oversized', 'symlink', 'invalid-path', 'identity-mismatch'])
def test_corrupt_pending_evidence_fails_closed(tmp_path, malformed):
    base, target, staged, request = stage(tmp_path)
    with intents.locked_intents(str(tmp_path / 'session')) as store:
        record = store.prepare(**request)
        path = Path(store.directory) / (record['changeId'] + '.json')
        if malformed == 'oversized':
            path.write_bytes(b'x' * (intents._limit('recordBytes') + 1))
        elif malformed == 'symlink':
            outside = tmp_path / 'outside.json'
            path.rename(outside)
            path.symlink_to(outside)
        else:
            changed = dict(record)
            changed['path' if malformed == 'invalid-path' else 'changeId'] = ('../outside' if malformed == 'invalid-path' else 'f' * 32)
            path.write_text(json.dumps(changed))
        with pytest.raises(OSError):
            store.read()
        assert path.exists()
        assert target.read_bytes() == request['original']


def test_lock_contention_is_bounded_and_releases_after_failure(tmp_path, monkeypatch):
    session = str(tmp_path / 'session')
    real_limit = intents._limit
    monkeypatch.setattr(intents, '_limit', lambda key: 0 if key == 'lockWaitSeconds' else real_limit(key))
    import concurrent.futures
    from lib.project_mod.session_lock import SessionLockBusy
    def competing_thread():
        with pytest.raises(SessionLockBusy, match='lock is busy'):
            with intents.locked_intents(session):
                pytest.fail('other thread must not bypass advisory lock')
    with intents.locked_intents(session):
        # Same-thread journal publication must reenter the same session lock.
        with intents.locked_intents(session) as nested:
            assert nested.read() == []
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(competing_thread).result(timeout=5)
    with intents.locked_intents(session) as store:
        assert store.read() == []


def test_directory_scan_has_a_total_entry_budget(tmp_path, monkeypatch):
    real_limit = intents._limit
    monkeypatch.setattr(intents, '_limit', lambda key: 2 if key == 'directoryEntries' else real_limit(key))
    with intents.locked_intents(str(tmp_path / 'session')) as store:
        for index in range(3):
            (Path(store.directory) / f'abandoned-{index}.tmp').write_bytes(b'')
        with pytest.raises(intents.IntentError, match='entry budget'):
            store.read()
        assert len(list(Path(store.directory).iterdir())) == 3


@pytest.mark.parametrize('publish', [False, True])
def test_process_exit_releases_lock_and_preserves_recovery_evidence(tmp_path, publish):
    import subprocess
    import sys
    base, target, staged, request = stage(tmp_path)
    session = str(tmp_path / 'session')
    program = '''
import os, sys
from lib.project_mod.write_intents import locked_intents
with locked_intents(sys.argv[1]) as store:
    store.prepare(base_path=sys.argv[2], path='file.txt', original=b'old\\r\\n\\xff',
                  post_content=b'new\\n', stage_name=sys.argv[3], conv_id='c', task_id='t')
    if sys.argv[4] == 'publish':
        os.replace(os.path.join(sys.argv[2], sys.argv[3]), os.path.join(sys.argv[2], 'file.txt'))
    os._exit(23)
'''
    result = subprocess.run([sys.executable, '-c', program, session, str(base), staged.name,
                             'publish' if publish else 'prepare'], capture_output=True, timeout=90)
    assert result.returncode == 23, result.stderr.decode(errors='replace')
    with intents.locked_intents(session) as store:
        records = store.read()
        assert len(records) == 1
        assert base64.b64decode(records[0]['original']) == request['original']
        assert intents.classify(records[0]) == ('applied' if publish else 'unpublished')
    assert target.read_bytes() == (b'new\n' if publish else request['original'])


def test_aborted_cleanup_can_resume_after_stage_removed_before_ack(tmp_path, monkeypatch):
    base, target, staged, request = stage(tmp_path)
    session = str(tmp_path / 'session')
    with intents.locked_intents(session) as store:
        record = store.abort(store.prepare(**request))
        def interrupted_ack(*args):
            raise OSError('injected exit after stage cleanup')
        monkeypatch.setattr(store, 'acknowledge', interrupted_ack)
        with pytest.raises(OSError, match='after stage cleanup'):
            store.discard_aborted(record)
        assert not staged.exists()
    with intents.locked_intents(session) as store:
        assert store.read() == [record]
        store.discard_aborted(record)
        assert store.read() == []
    assert target.read_bytes() == request['original']


def test_admission_reserves_space_for_new_intent_entry(tmp_path, monkeypatch):
    base, target, staged, request = stage(tmp_path)
    real_limit = intents._limit
    monkeypatch.setattr(intents, '_limit', lambda key: 1 if key == 'directoryEntries' else real_limit(key))
    with intents.locked_intents(str(tmp_path / 'session')) as store:
        (Path(store.directory) / 'abandoned.tmp').write_bytes(b'')
        assert store.read() == []
        with pytest.raises(intents.IntentError, match='entry budget'):
            store.prepare(**request)
        assert len(list(Path(store.directory).iterdir())) == 1
    assert target.read_bytes() == request['original']
