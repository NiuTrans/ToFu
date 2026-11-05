"""Large undo history cannot amplify small edits; exact evidence stays durable."""
import base64
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from lib.project_mod import modifications as mods, undo_payloads as payloads
from lib.project_mod.write_tools import _ops

pytestmark = pytest.mark.unit


@pytest.fixture
def project(tmp_path, monkeypatch):
    from lib import file_history
    base = tmp_path / 'project'; base.mkdir()
    monkeypatch.setattr(mods, 'SESSIONS_DIR', str(tmp_path / 'sessions'))
    monkeypatch.setattr(file_history, 'track_edit', lambda *a, **kw: None)
    monkeypatch.setattr(mods, '_live_change_listeners', [])
    monkeypatch.setattr(_ops, '_resolve_write_path', lambda root, name, **kw: str(base / name))
    monkeypatch.setattr(_ops, '_mod_attribution', lambda target, root, name, **kw: (str(base), name))
    monkeypatch.setattr(_ops, '_should_record_modification', lambda *a, **kw: True)
    return base


def test_reference_matches_contract_and_rejects_path_injection():
    contract = json.loads((Path(__file__).parents[2] / 'contracts/project_undo_payload_ref_v1.schema.json').read_text())
    valid = {'kind': payloads.KIND, 'digest': 'a' * 64, 'bytes': 42, 'pack': 'b' * 64, 'offset': 10}
    Draft202012Validator(contract).validate(valid)
    payloads.validate_reference(valid)
    for bad in ({**valid, 'digest': '../outside'}, {**valid, 'bytes': True}, {**valid, 'path': '/secret'}):
        with pytest.raises(ValueError): payloads.validate_reference(bad)
        assert list(Draft202012Validator(contract).iter_errors(bad))


def test_large_legacy_history_migrates_once_and_small_writes_do_not_read_payloads(project, monkeypatch):
    session = Path(mods._get_session_dir(str(project)))
    rows = [{'type': 'write_file', 'path': f'old-{i}', 'originalContent': str(i) + 'x' * (512 * 1024),
             'existed': True, 'convId': 'old'} for i in range(8)]
    journal = session / 'modifications.json'
    journal.write_text(json.dumps({'modifications': rows}))
    monkeypatch.setattr(mods, '_MODS_CACHE_MAX_SESSION_BYTES', 64 * 1024)
    assert mods._record_modification(str(project), 'write_file', 'first', original_content='first', conv_id='new')
    compact = json.loads(journal.read_text())['modifications']
    assert journal.stat().st_size < 10 * 1024
    assert len(compact) == 9
    assert all(payloads.is_reference(row['originalContent']) for row in compact)
    files = {p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in (session / 'undo-payloads-v1').iterdir()}
    loads = []
    original = mods._load_from_disk
    monkeypatch.setattr(mods, '_load_from_disk', lambda s: (loads.append(s), original(s))[1])
    with monkeypatch.context() as patch:
        patch.setattr(payloads, 'read_payload', lambda *a: pytest.fail('unrelated old contents were read'))
        for i in range(5):
            assert mods._record_modification(str(project), 'write_file', f'next-{i}', original_content='small', conv_id='new')
    assert loads == []
    assert [row['originalContent'] for row in mods.get_modifications(str(project), 'old')] == [row['originalContent'] for row in rows]
    assert all((session / 'undo-payloads-v1' / name).stat().st_mtime_ns == stamp for name, (_, stamp) in files.items())


@pytest.mark.parametrize('content', [b'old\r\n', b'\x00\xffbinary'])
def test_exact_binary_and_crlf_undo_survives_cache_eviction(project, content):
    (project / 'file').write_bytes(content)
    assert _ops.tool_write_file(str(project), 'file', 'updated', task_id='task')['ok']
    session = mods._get_session_dir(str(project))
    mods._invalidate_cache(session)
    row = mods.get_modifications(str(project))[0]
    original = mods._decode_original(row)
    assert (original.encode() if isinstance(original, str) else original) == content
    result = mods.undo_task_modifications(str(project), 'task')
    assert result['undone'] == 1 and result['failed'] == 0
    assert (project / 'file').read_bytes() == content
    assert not list((Path(session) / 'undo-payloads-v1').glob('*.pack'))


def test_corrupt_newer_payload_blocks_older_undo_on_same_path(project):
    (project / 'file').write_text('old')
    assert _ops.tool_write_file(str(project), 'file', 'middle', task_id='task')['ok']
    assert _ops.tool_write_file(str(project), 'file', 'new', task_id='task')['ok']
    session = Path(mods._get_session_dir(str(project)))
    rows = mods._load_from_disk(str(session))
    ref = rows[-1]['originalContent']
    (session / 'undo-payloads-v1' / (ref['pack'] + '.pack')).write_text('"corrupted"')
    result = mods.undo_task_modifications(str(project), 'task')
    assert result['undone'] == 0 and result['failed'] == 2
    assert (project / 'file').read_text() == 'new'
    assert len(mods._load_from_disk(str(session))) == 2


def test_index_failure_keeps_old_history_and_recovery_retry_is_idempotent(project, monkeypatch):
    session = Path(mods._get_session_dir(str(project)))
    journal = session / 'modifications.json'
    original = {'modifications': [{'type':'write_file','path':'old','originalContent':'preserved','existed':True}]}
    journal.write_text(json.dumps(original)); before = journal.read_bytes()
    write = mods._atomic_json_write
    def fail(path, data):
        if str(path) == str(journal): raise OSError('injected index failure')
        return write(path, data)
    with monkeypatch.context() as patch:
        patch.setattr(mods, '_atomic_json_write', fail)
        assert mods._record_modification(str(project), 'write_file', 'new', original_content='new', change_id='a' * 32) is False
    assert journal.read_bytes() == before
    assert mods._record_modification(str(project), 'write_file', 'new', original_content='new', change_id='a' * 32)
    assert mods._record_modification(str(project), 'write_file', 'new', original_content='new', change_id='a' * 32)
    assert len(mods.get_modifications(str(project))) == 2


def test_payload_directory_and_file_symlinks_fail_closed(tmp_path):
    session = tmp_path / 'session'; session.mkdir()
    ref = payloads.compact_records(str(session), [{'originalContent':'original'}])[0]['originalContent']
    path = session / 'undo-payloads-v1' / (ref['pack'] + '.pack')
    outside = tmp_path / 'outside'; outside.write_text('"original"')
    path.unlink(); path.symlink_to(outside)
    with pytest.raises(OSError): payloads.read_payload(str(session), ref)
    path.unlink(); (session / 'undo-payloads-v1').rmdir()
    (session / 'undo-payloads-v1').symlink_to(tmp_path)
    with pytest.raises(OSError): payloads.compact_records(str(session), [{'originalContent':'new'}])
    assert outside.read_text() == '"original"'


def test_metadata_projection_never_reads_unselected_content(project, monkeypatch):
    assert mods._record_modification(str(project), 'write_file', 'old', original_content='large' * 10000, conv_id='old')
    assert mods._record_modification(str(project), 'write_file', 'new', original_content='new', conv_id='new')
    with monkeypatch.context() as patch:
        patch.setattr(payloads, 'read_payload', lambda *args: pytest.fail('metadata requested payload bytes'))
        assert [row['path'] for row in mods.get_modifications(str(project), include_content=False)] == ['old', 'new']
    calls = []
    read = payloads.read_payload
    monkeypatch.setattr(payloads, 'read_payload', lambda *args: (calls.append(args[1]['bytes']), read(*args))[1])
    assert mods.get_modifications(str(project), 'new')[0]['originalContent'] == 'new'
    assert calls == [5]


def test_packed_migration_durability_failure_preserves_original_index(project, monkeypatch):
    session = Path(mods._get_session_dir(str(project)))
    journal = session / 'modifications.json'
    journal.write_text(json.dumps({'modifications':[{'type':'write_file','path':'old','originalContent':'exact original'}]}))
    before = journal.read_bytes()
    def fail(_): raise OSError('injected payload fsync failure')
    with monkeypatch.context() as patch:
        from types import SimpleNamespace
        patch.setattr(payloads, 'os', SimpleNamespace(**{**vars(payloads.os), 'fsync': fail}))
        assert mods._record_modification(str(project), 'write_file', 'new', original_content='new') is False
    assert journal.read_bytes() == before
    assert mods.get_modifications(str(project))[0]['originalContent'] == 'exact original'


def test_shared_pack_is_reclaimed_only_after_its_last_reference(project):
    session = mods._get_session_dir(str(project))
    rows=[{'type':'write_file','path':name,'originalContent':name} for name in ('first','second')]
    mods._flush_to_disk(session, rows)
    pack = Path(session) / 'undo-payloads-v1' / (rows[0]['originalContent']['pack'] + '.pack')
    assert rows[1]['originalContent']['pack'] == rows[0]['originalContent']['pack']
    mods._locked_rmw(session, lambda current: current[1:])
    assert pack.is_file()
    assert mods.get_modifications(str(project))[0]['originalContent'] == 'second'
    mods._locked_rmw(session, lambda current: [])
    assert not pack.exists()


def test_cold_cleanup_never_deletes_referenced_or_young_contents(project):
    import os
    import time
    session = mods._get_session_dir(str(project))
    keep = payloads.compact_records(session, [{'originalContent':'keep'}])
    orphan = payloads.compact_records(session, [{'originalContent':'orphan'}])
    young = payloads.compact_records(session, [{'originalContent':'young'}])
    directory = Path(session) / 'undo-payloads-v1'
    old = time.time() - 120
    for rows in (keep, orphan):
        path = directory / (rows[0]['originalContent']['pack'] + '.pack')
        os.utime(path, (old, old))
    assert payloads.reclaim_orphans(session, keep) == 1
    assert payloads.hydrate_record(session, keep[0])['originalContent'] == 'keep'
    assert payloads.hydrate_record(session, young[0])['originalContent'] == 'young'


def test_crash_artifact_sweep_is_one_complete_pass(tmp_path):
    import os
    import time
    session = str(tmp_path)
    keep = payloads.compact_records(session, [{'originalContent': 'keep'}])
    for index in range(40):
        payloads.compact_records(session, [{'originalContent': f'orphan-{index}'}])
    directory = tmp_path / 'undo-payloads-v1'
    (directory / ('.payload-' + 'a' * 32)).write_bytes(b'interrupted stage')
    old = time.time() - 120
    for path in directory.iterdir():
        os.utime(path, (old, old))
    assert payloads.reclaim_orphans(session, keep) == 41
    assert [path.name for path in directory.iterdir()] == [keep[0]['originalContent']['pack'] + '.pack']


def test_crash_artifacts_are_swept_once_per_process_and_on_crash_evidence(project, monkeypatch):
    import os
    import time
    monkeypatch.setattr(mods, '_PAYLOADS_SWEPT', set(), raising=False)
    sweeps = []
    reclaim = payloads.reclaim_orphans
    monkeypatch.setattr(payloads, 'reclaim_orphans',
                        lambda *args, **kwargs: (sweeps.append(args[0]), reclaim(*args, **kwargs))[1])
    (project / 'file').write_text('old')
    assert _ops.tool_write_file(str(project), 'file', 'new', conv_id='conv', task_id='first')['ok']
    session = mods._get_session_dir(str(project))
    assert sweeps == [session], 'the first admitted history load sweeps the payload directory'
    directory = Path(session) / 'undo-payloads-v1'
    old = time.time() - 120

    def orphan(text):
        pack = payloads.compact_records(session, [{'originalContent': text}])[0]['originalContent']['pack']
        path = directory / (pack + '.pack')
        os.utime(path, (old, old))
        return path

    artifact = orphan('crashed commit')
    mods._invalidate_cache(session)
    assert [row['originalContent'] for row in mods.get_modifications(str(project))] == ['old']
    assert _ops.tool_write_file(str(project), 'file', 'newer', conv_id='conv', task_id='second')['ok']
    assert sweeps == [session], 'cold reads and writes must not rescan the payload directory'
    assert artifact.exists()
    with monkeypatch.context() as patch:
        patch.setattr(_ops, '_record_modification', lambda *args, **kwargs: False)
        result = _ops.tool_write_file(str(project), 'file', 'crash', conv_id='conv', task_id='third')
    assert result['ok'] is False and result['mutationApplied'] is True
    rows = mods.get_modifications(str(project), include_content=False)
    assert [row['taskId'] for row in rows] == ['first', 'second', 'third']
    assert sweeps == [session, session], 'reconciling crash evidence sweeps again'
    assert not artifact.exists()
    assert mods.undo_task_modifications(str(project), 'third')['undone'] == 1
    assert (project / 'file').read_text() == 'newer'
