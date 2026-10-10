"""Device commits carry bounded, requested-path facts without changing tool text."""
import json

import pytest

from lib.desktop import format_desktop_result
from lib.desktop_agent import _project as project
from lib.project_mod.receipts import FileReceiptError, decode_write_receipts

pytestmark = pytest.mark.unit


@pytest.fixture
def device_project(tmp_path, monkeypatch):
    root = tmp_path / 'paper'
    root.mkdir()
    config = tmp_path / 'device.json'
    config.write_text(json.dumps({'share_roots': [{'name': 'paper', 'path': str(root)}]}))
    monkeypatch.setenv('TOFU_DESKTOP_CONFIG', str(config))
    monkeypatch.setattr(project, '_freshness', {})
    return root


def write(path='main.tex', content='first version', **extra):
    return project.cmd_project_write_file({'root': 'paper', 'path': path,
        'content': content, 'file_receipts_version': 1, **extra})


def test_committed_writes_have_distinct_receipts_and_ordinary_output(device_project):
    first = write()
    second = write(content='second version')
    a = decode_write_receipts(first, ['main.tex'])[0]
    b = decode_write_receipts(second, ['main.tex'])[0]
    assert a['action'] == 'created' and b['action'] == 'written'
    assert a['changeId'] != b['changeId']
    assert (device_project / 'main.tex').read_text() == 'second version'
    assert format_desktop_result('project_write_file', first) == format_desktop_result(
        'project_write_file', first['output'])
    legacy = write('legacy.tex', file_receipts_version=0)
    assert 'file_receipts_version' not in legacy
    assert decode_write_receipts(legacy, ['legacy.tex']) == []


def test_failed_atomic_write_and_stale_read_produce_no_commit(device_project, monkeypatch):
    write()
    (device_project / 'main.tex').write_text('user edit since read')
    stale = write(content='must not overwrite')
    assert 'error' in stale and decode_write_receipts(stale, ['main.tex']) == []
    assert (device_project / 'main.tex').read_text() == 'user edit since read'
    def fail(*args):
        raise OSError('injected atomic failure')
    monkeypatch.setattr(project, '_atomic_write', fail)
    with pytest.raises(OSError, match='injected atomic failure'):
        write('new.tex')
    assert not (device_project / 'new.tex').exists()


def test_partial_edit_batch_only_reports_successful_siblings(device_project):
    write(content='first version')
    result = project.cmd_project_edit_file({'root': 'paper', 'file_receipts_version': 1,
        'edits': [
            {'path': 'main.tex', 'operation': 'replace', 'anchor': 'first', 'content': 'second'},
            {'path': 'main.tex', 'operation': 'replace', 'anchor': 'missing', 'content': 'bad'},
            {'path': 'main.tex', 'operation': 'replace', 'anchor': 'second', 'content': 'third'},
        ]})
    entries = decode_write_receipts(result, ['main.tex'])
    assert len(entries) == 2 and len({entry['changeId'] for entry in entries}) == 2
    assert 'Applied 2/3' in result['output'] and '1 failed' in result['output']
    assert (device_project / 'main.tex').read_text() == 'third version'


def test_apply_diff_delivers_a_committed_patch(device_project):
    write()
    result = project.cmd_project_apply_diff({'root': 'paper', 'path': 'main.tex',
        'search': 'first', 'replace': 'second', 'file_receipts_version': 1})
    assert decode_write_receipts(result, ['main.tex'])[0]['action'] == 'patched'
    assert (device_project / 'main.tex').read_text() == 'second version'


@pytest.mark.parametrize('patch', [
    {'path': '../outside.tex'}, {'path': '/outside.tex'}, {'path': 'C:\\outside.tex'},
    {'path': 'other.tex'}, {'owner_user_id': 99}, {'rootPath': 'remote:foreign:root'},
])
def test_receiver_refuses_unrequested_or_spoofed_facts(device_project, patch):
    result = write()
    result['file_changes'][0].update(patch)
    with pytest.raises(FileReceiptError):
        decode_write_receipts(result, ['main.tex'])


@pytest.mark.parametrize('version', [True, 1.0, 2])
def test_receiver_requires_exact_integer_protocol_version(device_project, version):
    result = write()
    result['file_receipts_version'] = version
    with pytest.raises(FileReceiptError):
        decode_write_receipts(result, ['main.tex'])


def test_receiver_bounds_batch_before_projection(device_project):
    result = write()
    result['file_changes'] *= 31
    with pytest.raises(FileReceiptError):
        decode_write_receipts(result, ['main.tex'])


def test_checkpoint_merge_preserves_same_named_files_in_distinct_sources():
    from lib.project_mod.receipts import is_host_file_change, merge_file_changes
    host = {'path': 'main.tex', 'rootPath': '/project', 'changeId': 'a' * 32}
    device = {'path': 'main.tex', 'rootPath': 'remote:device:project', 'changeId': 'b' * 32}
    first = {'source': 'overleaf', 'projectId': 'a' * 24, 'mcpServer': 'papers',
             'path': 'main.tex', 'changeId': 'c' * 32}
    second = {**first, 'projectId': 'b' * 24}
    third = {**first, 'mcpServer': 'other-server'}
    latest = {**first, 'changeId': 'd' * 32, 'sourceRevision': 'e' * 40}
    merged = merge_file_changes([host, device, first, second, third], [latest])
    assert merged == [host, device, latest, second, third]
    assert [entry for entry in merged if is_host_file_change(entry)] == [host]
