"""Opt-in source binding preserves project read ranges and exact file grants."""
import json
import re

import pytest

from tests.project_mod.test_project_dispatch_scope import granted as granted
from tests.tasks_pkg.test_native_gateway import task as gateway_task, principal
from lib.tasks_pkg.working_file_sources import FileReadCapture, file_witness_resolver
from lib.project_mod.tools import execute_tool

pytestmark = pytest.mark.unit


def test_file_grant_keeps_path_fence_when_issuing_source(granted):
    original = granted.task.copy()
    granted.task.update(gateway_task())
    granted.task.update(original)
    granted.task['_principalContext'] = principal().to_payload()
    capture = FileReadCapture(granted.task, 'scoped-read')
    result = execute_tool('read_files', {'path': 'src/a.txt', 'start_line': 2, 'end_line': 2,
        'bind_sources': True}, str(granted.root), task=granted.task,
        conv_id=granted.task['convId'], task_id=granted.task['id'], file_source_capture=capture)
    assert 'beta' in result and 'alpha' not in result
    source = json.loads(re.search(r'\[File source: (\{.*\})\]', result)[1])
    capture.publish({'status': 'done'})
    assert file_witness_resolver(granted.task)(source).path == str(granted.root / 'src/a.txt')
    from lib.llm.llm_errors import AbortedError
    granted.live['revoked'] = True
    with pytest.raises(AbortedError): file_witness_resolver(granted.task)(source)


@pytest.mark.parametrize('case', ['absolute', 'symlink', 'invalid_utf8', 'nul', 'no_root'])
def test_explicit_binding_does_not_borrow_unverified_or_unsafe_roots(tmp_path, case):
    root = tmp_path / 'project'; root.mkdir()
    task = gateway_task() | {'convId': 'file-binding-scope', '_principalContext': principal().to_payload()}
    task['config']['projectPath'] = str(root)
    path = root / 'source.txt'; path.write_text('source evidence')
    if case == 'symlink':
        path.unlink(); outside = tmp_path / 'outside.txt'; outside.write_text('outside evidence'); path.symlink_to(outside)
    if case == 'invalid_utf8': path.write_bytes(b'evidence\xff')
    if case == 'nul': path.write_bytes(b'evidence\x00')
    if case == 'no_root': task['config'].pop('projectPath')
    capture = FileReadCapture(task, 'read')
    result = execute_tool('read_files', {'path': str(path) if case in {'absolute', 'no_root'} else 'source.txt',
        'bind_sources': True}, str(root), task=task, conv_id=task['convId'], task_id=task['id'], file_source_capture=capture)
    capture.publish({'status': 'done'})
    assert bool(task.get('_working_file_sources')) == (case == 'absolute'), result
    assert ('[File source:' in result) == (case == 'absolute')
