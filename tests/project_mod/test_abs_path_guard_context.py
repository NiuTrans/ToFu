"""Scoped absolute-path authority must match direct and gateway project reads."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from lib.project_mod import abs_path_guard as guard
from lib.project_mod import config
from lib.project_mod.tools import execute_tool

pytestmark = pytest.mark.unit


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    from collections import OrderedDict

    root = tmp_path / 'app'
    root.mkdir()
    inside = root / 'file.txt'
    inside.write_text('scoped-project-content\n')
    outside = tmp_path / 'other' / 'file.txt'
    outside.parent.mkdir()
    outside.write_text('outside-secret-content\n')
    (root / 'escape.txt').symlink_to(outside)
    monkeypatch.setattr(config, '_roots', {})
    monkeypatch.setattr(config, '_conv_roots', OrderedDict())
    monkeypatch.setattr(config, '_conv_primary', {})
    config.set_conv_roots('scope-a', str(root))
    config.set_conv_roots('scope-b', str(outside.parent))
    token = guard.set_restricted(True)
    yield root, inside, outside
    guard.reset_restricted(token)


@pytest.mark.parametrize('batch', [False, True])
@pytest.mark.parametrize('path_kind', ['relative', 'absolute', 'outside', 'symlink', 'traversal'])
def test_scoped_project_reads(workspace, batch, path_kind):
    root, inside, outside = workspace
    path = {'relative': 'file.txt', 'absolute': str(inside),
            'outside': str(outside), 'symlink': str(root / 'escape.txt'),
            'traversal': '../other/file.txt'}[path_kind]
    args = {'reads': [{'path': path}]} if batch else {'path': path}
    result = execute_tool('read_files', args, str(root), conv_id='scope-a')
    if path_kind in ('relative', 'absolute'):
        assert 'scoped-project-content' in result
    else:
        assert 'outside-secret-content' not in result
        assert any(word in result.lower() for word in ('denied', 'outside', 'blocked'))


def test_scoped_authority_does_not_borrow_global_or_other_task(workspace, monkeypatch):
    root, inside, outside = workspace
    monkeypatch.setattr(config, '_roots', {'other': {'path': str(outside.parent)}})
    with guard.registered_root_scope('scope-a'):
        guard.enforce_abs_read(str(inside))
        guard.enforce_abs_write(str(inside))
        for path in (outside, root / 'escape.txt', outside.parent / 'new.txt'):
            with pytest.raises(guard.AbsPathDenied):
                guard.enforce_abs_write(str(path))
            with pytest.raises(guard.AbsPathDenied):
                guard.enforce_abs_read(str(path))
    with guard.registered_root_scope('missing-scope'):
        with pytest.raises(guard.AbsPathDenied):
            guard.enforce_abs_read(str(outside))
    # The unscoped legacy registry remains usable after token restoration.
    guard.enforce_abs_read(str(outside))


def test_scope_restores_after_exception_and_isolates_workers(workspace):
    _, inside, outside = workspace
    def check(scope, allowed, denied):
        token = guard.set_restricted(True)
        try:
            with guard.registered_root_scope(scope):
                with pytest.raises(RuntimeError):
                    with guard.registered_root_scope('missing-scope'):
                        raise RuntimeError('injected')
                guard.enforce_abs_read(str(allowed))
                with pytest.raises(guard.AbsPathDenied):
                    guard.enforce_abs_read(str(denied))
        finally:
            guard.reset_restricted(token)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(check, 'scope-a', inside, outside),
                   pool.submit(check, 'scope-b', outside, inside)]
        for future in futures:
            future.result()


def test_execute_tools_gateway_uses_same_scoped_guard(workspace):
    from lib.tasks_pkg.handlers.tool_gateway import _execute_call_batch

    root, inside, outside = workspace
    catalog = [{'type': 'function', 'function': {
        'name': 'read_files', 'description': 'Read project files',
        'parameters': {'type': 'object', 'properties': {
            'path': {'type': 'string'}}, 'required': ['path']}}}]
    task = {
        'id': 'scope-a', 'convId': '', '_userId': 1,
        '_via_agent_run': True, '_transient': True,
        'status': 'running', 'aborted': False, 'model': 'test', 'events': [],
        'config': {'projectPath': str(root), 'tools': {'resultEnvelope': 'legacy'}},
        'events_lock': threading.Lock(), '_attended': True,
        '_dispatch_heartbeat': 0.0, '_t_last_event': 0.0, 'toolRounds': [],
        '_executable_tool_catalog': catalog,
    }
    paths = ['file.txt', str(inside), str(outside), str(root / 'escape.txt')]
    calls = [{'id': f'path-{i}', 'type': 'function', 'function': {
        'name': 'read_files', 'arguments': json.dumps({'path': path})}}
        for i, path in enumerate(paths)]
    results = _execute_call_batch(
        task, calls, cfg={'autoApply': True}, project_path=str(root),
        project_enabled=True, model='test', llm_round=0)
    assert len(results) == len(paths)
    for i, receipt in enumerate(results):
        assert receipt.canonical is not None
        rendered = receipt.canonical.text()
        if i < 2:
            assert 'scoped-project-content' in rendered
        else:
            assert 'outside-secret-content' not in rendered
            assert 'Access denied' in rendered
