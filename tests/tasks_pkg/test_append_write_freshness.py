"""Append consumes live bytes; stale replacement and anchor tokens still refuse."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from lib import write_freshness
from lib.project_mod.write_tools import tool_write_file, tool_apply_diff

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.delenv('TOFU_WRITE_FRESHNESS_GATE', raising=False)
    monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', '5')
    write_freshness._reset_for_tests()
    import lib.tasks_pkg.handlers.project as hp

    def finalize(task, rn, entry, results, **kwargs):
        entry['results'] = results
        entry['status'] = 'done'

    monkeypatch.setattr(hp, '_finalize_tool_round', finalize)
    yield
    write_freshness._reset_for_tests()


def drive(task, root, name, args):
    from lib.tasks_pkg.handlers.project import _handle_project_tool
    result = _handle_project_tool(
        task, {}, name, 'call', args, 1,
        {'query': name, 'toolCallId': 'call'}, None, str(root), True)
    task['toolRounds'].append({'toolName': name, 'toolArgs': json.dumps(args),
                               'status': 'done', 'roundNum': 1})
    return result[1]


def task():
    return {'id': 'append-task', 'convId': 'append-owner',
            'messages': [], 'toolRounds': []}


@pytest.mark.parametrize('handler', [False, True])
@pytest.mark.parametrize('change', ['formatter', 'sibling'])
def test_append_preserves_changed_prefix_and_records_final_version(tmp_path, handler, change):
    path = tmp_path / 'a.txt'
    owner = task()

    key = owner['id'] if handler else owner['convId']
    assert tool_write_file(str(tmp_path), 'a.txt', 'old\n', conv_id=key)['ok']
    if handler:
        drive(owner, tmp_path, 'read_files', {'path': 'a.txt'})
        message = drive(owner, tmp_path, 'edit_file', {
            'description': 'own edit', 'edits': [{'path': 'a.txt', 'operation': 'replace',
                                                  'anchor': 'old', 'content': 'own'}]})
        assert 'Error' not in message, message
    prefix = '\ufeffformatted café\r\n第二行\r\n'.encode('utf-8')
    if change == 'formatter':
        # Same task's formatter-like disk change, deliberately without re-read.
        path.write_bytes(prefix)
    else:
        assert tool_write_file(str(tmp_path), 'a.txt', prefix.decode(), conv_id='sibling')['ok']
    assert write_freshness.is_stale(key, str(path))
    suffix = '追加\r\n'
    if handler:
        message = drive(owner, tmp_path, 'write_file',
                        {'path': 'a.txt', 'content': suffix, 'append': True})
        assert message.startswith('File updated:'), message
    else:
        result = tool_write_file(str(tmp_path), 'a.txt', suffix,
                                 conv_id=key, append=True)
        assert result['ok'], result
    assert path.read_bytes() == prefix + suffix.encode()
    assert not write_freshness.is_stale(key, str(path))
    # A full replacement now succeeds using the fresh own post-write token.
    assert tool_write_file(str(tmp_path), 'a.txt', 'fresh\n', conv_id=key)['ok']
    path.write_bytes(b'changed again\r\n')
    refused = tool_write_file(str(tmp_path), 'a.txt', 'lost', conv_id=key)
    assert not refused['ok'] and refused['error_kind'] == 'write-freshness-stale'
    assert 'observed content version differs' in refused['error']
    anchored = tool_apply_diff(str(tmp_path), 'a.txt', 'changed', 'lost', conv_id=key)
    assert not anchored['ok']
    if handler:
        message = drive(owner, tmp_path, 'write_file', {'path': 'a.txt', 'content': 'lost'})
        assert message.startswith('Error: write_file refused'), message
        assert 'observed content version differs' in message
        assert 'Another conversation' not in message and 'to disable' not in message
    assert path.read_bytes() == b'changed again\r\n'


@pytest.mark.parametrize('handler', [False, True])
def test_append_creates_missing_file(tmp_path, handler):
    if handler:
        message = drive(task(), tmp_path, 'write_file',
                        {'path': 'new.txt', 'content': '新\r\n', 'append': True})
        assert message.startswith('File created:'), message
    else:
        result = tool_write_file(str(tmp_path), 'new.txt', '新\r\n', append=True, conv_id='creator')
        assert result['ok'] and result['created'], result
    assert (tmp_path / 'new.txt').read_bytes() == '新\r\n'.encode()


def test_concurrent_append_preserves_both_suffixes(tmp_path):
    path = tmp_path / 'a.txt'
    path.write_bytes(b'original\r\n')
    for owner in ('a', 'b'):
        write_freshness.record(owner, str(path))
    path.write_bytes(b'live prefix\r\n')
    start = threading.Barrier(2)

    def append(owner):
        start.wait(timeout=5)
        return tool_write_file(str(tmp_path), 'a.txt', owner + '\r\n',
                               conv_id=owner, append=True)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(append, ('a', 'b')))
    assert all(result['ok'] for result in results), results
    assert path.read_bytes() in (b'live prefix\r\na\r\nb\r\n',
                                 b'live prefix\r\nb\r\na\r\n')


def test_append_undecodable_prefix_refuses_without_loss(tmp_path):
    path = tmp_path / 'a.txt'
    path.write_bytes(b'\xff\r\n')
    write_freshness.record('owner', str(path))
    path.write_bytes(b'\xfe\r\n')
    result = tool_write_file(str(tmp_path), 'a.txt', 'suffix', conv_id='owner', append=True)
    assert not result['ok'] and result['errorKind'] == 'read-failed'
    assert result['mutationApplied'] is False
    assert path.read_bytes() == b'\xfe\r\n'


def test_stale_append_undo_restores_live_preimage(tmp_path, monkeypatch):
    from lib.project_mod import modifications as mods
    from lib.project_mod.write_tools import _ops
    monkeypatch.setattr(mods, 'SESSIONS_DIR', str(tmp_path / 'journals'))
    monkeypatch.setattr(_ops, '_should_record_modification', lambda *a, **kw: True)
    monkeypatch.setattr(_ops, '_mod_attribution',
                        lambda target, root, path, **kw: (str(tmp_path), path))
    path = tmp_path / 'a.txt'
    path.write_bytes(b'old\r\n')
    write_freshness.record('owner', str(path))
    live = '\ufeff当前\r\n'.encode()
    path.write_bytes(live)
    result = tool_write_file(str(tmp_path), 'a.txt', '追加\r\n', append=True,
                             conv_id='owner', task_id='append-undo')
    assert result['ok'], result
    records = mods.get_modifications(str(tmp_path))
    assert records[0]['originalContent'].encode() == live
    undone = mods.undo_task_modifications(str(tmp_path), 'append-undo')
    assert undone['undone'] == 1, undone
    assert path.read_bytes() == live


def test_stale_append_failed_preimage_read_does_not_mutate(tmp_path, monkeypatch):
    import builtins
    from lib.project_mod.write_tools import _ops
    path = tmp_path / 'a.txt'
    path.write_bytes(b'old\r\n')
    write_freshness.record('owner', str(path))
    path.write_bytes(b'live\r\n')

    def deny(target, *args, **kwargs):
        if str(target) == str(path):
            raise PermissionError('injected preimage read failure')
        return builtins.open(target, *args, **kwargs)

    monkeypatch.setattr(_ops, 'open', deny, raising=False)
    result = tool_write_file(str(tmp_path), 'a.txt', 'suffix', append=True, conv_id='owner')
    assert not result['ok'] and result['errorKind'] == 'read-failed'
    assert result['mutationApplied'] is False
    assert path.read_bytes() == b'live\r\n'
