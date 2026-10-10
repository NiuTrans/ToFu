"""Local MCP paths share deterministic task roots with file and command tools."""
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from lib.mcp.result_content import MCPToolResult
from lib.project_mod.config import build_root_policy
from lib.tasks_pkg.handlers._mcp_files import call_with_file_receipts
from lib.tasks_pkg.handlers._mcp_local_paths import path_contract

pytestmark = pytest.mark.unit


class Bridge:
    def __init__(self, tool='prepare_doc_edit', transport='stdio', meta=None):
        self.info = {'server_name': 'xuecheng', 'tool_name': tool,
                     'input_schema': {'properties': {
                         'out_path': {'type': 'string'}, 'from_file': {'type': 'string'}}},
                     'meta': meta or {}, 'read_only_hint': True}
        self.transport = transport
        self.calls = []
        self.result = MCPToolResult('saved', structured_json='{"ok":true}')

    def get_tool_info(self, name):
        return self.info

    def list_servers(self):
        return [{'name': self.info['server_name'], 'transport': self.transport}]

    def call_tool(self, name, args, **options):
        self.calls.append((name, deepcopy(args)))
        if 'out_path' in args:
            Path(args['out_path']).write_text('CitadelMD')
        return self.result


@pytest.fixture
def workspace(tmp_path):
    primary, canonical, isolated, readonly = [tmp_path / name for name in ('autoharness', 'llm-mcp', 'task-42', 'reference')]
    for path in (primary, canonical, isolated, readonly):
        path.mkdir()
    policy = build_root_policy(str(primary), [str(canonical), str(readonly)], [str(readonly)])
    policy['roots']['llm-mcp']['path'] = str(isolated)
    return {'id': 'mcp-path-task', '_userId': 7,
            'config': {'projectPath': str(primary), 'projectPaths': [str(primary), str(isolated), str(readonly)]},
            '_workspaceRootPolicy': policy,
            '_workspaceDelivery': {'repositories': [
                {'canonicalRoot': str(canonical), 'workspacePath': str(isolated), 'workId': 'w'}]}}, primary, canonical, isolated, readonly


def call(bridge, task, args):
    return call_with_file_receipts(bridge, 'mcp__xuecheng__' + bridge.info['tool_name'], args, task=task)


@pytest.mark.parametrize('form', ['relative', 'namespaced', 'canonical', 'execution'])
def test_output_parent_prepared_at_exact_root(workspace, form):
    task, primary, canonical, isolated, _ = workspace
    target = primary / 'work/doc.md' if form == 'relative' else isolated / 'work/doc.md'
    value = {'relative': 'work/doc.md', 'namespaced': 'llm-mcp:work/doc.md',
             'canonical': str(canonical / 'work/doc.md'), 'execution': str(target)}[form]
    bridge = Bridge()
    result = call(bridge, task, {'doc': '42', 'out_path': value})
    assert not result.is_error, result
    assert target.read_text() == 'CitadelMD'
    assert bridge.calls[0][1]['out_path'] == str(target)
    assert str(target) in result
    assert result.structured_json == bridge.result.structured_json
    assert not (canonical / 'work').exists()


def test_read_uses_same_mapping_as_write(workspace):
    task, _, _, isolated, _ = workspace
    bridge = Bridge()
    assert not call(bridge, task, {'out_path': 'llm-mcp:work/doc.md'}).is_error
    bridge.info['tool_name'] = 'update_doc'
    result = call(bridge, task, {'from_file': 'llm-mcp:work/doc.md'})
    assert not result.is_error
    assert bridge.calls[-1][1]['from_file'] == str(isolated / 'work/doc.md')


@pytest.mark.parametrize('failure', ['readonly', 'outside', 'traversal', 'unknown_root', 'symlink', 'cancelled', 'missing_input', 'directory'])
def test_invalid_paths_refuse_without_invocation_or_parent_creation(workspace, tmp_path, failure):
    task, primary, _, isolated, reference = workspace
    outside = tmp_path / 'outside'
    outside.mkdir()
    (primary / 'escape').symlink_to(outside, target_is_directory=True)
    value = {'readonly': str(reference / 'new/doc.md'), 'outside': str(outside / 'new/doc.md'),
             'traversal': '../outside/new/doc.md', 'unknown_root': 'absent:new/doc.md',
             'symlink': 'escape/new/doc.md', 'cancelled': 'work/new/doc.md',
             'missing_input': 'llm-mcp:new/doc.md', 'directory': str(primary)}[failure]
    bridge = Bridge('update_doc' if failure == 'missing_input' else 'prepare_doc_edit')
    if failure == 'cancelled':
        task['aborted'] = True
    args = {'from_file' if failure == 'missing_input' else 'out_path': value}
    result = call(bridge, task, args)
    assert result.is_error, result
    assert bridge.calls == []
    assert not (outside / 'new').exists()
    assert not (reference / 'new').exists()
    assert not (primary / 'work').exists()
    assert not (isolated / 'new').exists()


@pytest.mark.parametrize('transport', ['sse', 'streamable-http'])
def test_remote_server_path_is_not_interpreted_as_local(workspace, transport):
    task, primary, *_ = workspace
    bridge = Bridge(transport=transport)
    result = call(bridge, task, {'out_path': 'work/doc.md'})
    assert result.is_error
    assert bridge.calls == []
    assert not (primary / 'work').exists()


def test_undeclared_tool_arguments_remain_opaque(workspace):
    task, *_ = workspace
    bridge = Bridge('another_tool')
    bridge.info['server_name'] = 'another-server'
    # Its fake executor must not interpret arbitrary fields as local files.
    args = {'out_path_like': 'server-specific:path'}
    result = call(bridge, task, args)
    assert result is bridge.result
    assert bridge.calls[0][1] == args


def test_schema_declared_paths_support_other_servers(workspace):
    task, primary, *_ = workspace
    declaration = {'version': 1, 'filesystem': 'server-local',
                   'parameters': {'out_path': {'access': 'write', 'create_parents': True}}}
    Draft202012Validator(path_contract()).validate(declaration)
    bridge = Bridge(meta={'tofu/local-paths': declaration})
    bridge.info['server_name'] = 'documents'
    result = call(bridge, task, {'out_path': 'generated/doc.md'})
    assert not result.is_error
    assert (primary / 'generated/doc.md').exists()


def test_invalid_declaration_does_not_fall_back_to_unrouted_invocation(workspace):
    task, *_ = workspace
    bridge = Bridge(meta={'tofu/local-paths': {'version': 99}})
    result = call(bridge, task, {'out_path': 'work/doc.md'})
    assert result.is_error
    assert bridge.calls == []


def test_bare_file_path_does_not_follow_file_existence(workspace):
    from lib.project_mod.config import execution_root_scope
    from lib.project_mod.path_resolution import _resolve_base
    task, primary, _, isolated, _ = workspace
    (isolated / 'only-secondary.txt').write_text('secondary')
    with execution_root_scope(task['id'], task['_workspaceRootPolicy']):
        assert _resolve_base(str(primary), 'only-secondary.txt', conv_id=task['id']) == (str(primary), 'only-secondary.txt')
        (primary / 'only-secondary.txt').write_text('primary')
        assert _resolve_base(str(primary), 'only-secondary.txt', conv_id=task['id']) == (str(primary), 'only-secondary.txt')


def test_direct_and_progressive_handlers_share_local_path_preparation(workspace, monkeypatch):
    import lib.mcp
    from lib.tasks_pkg.handlers import mcp
    task, primary, *_ = workspace
    bridge = Bridge()
    monkeypatch.setattr(lib.mcp, 'get_bridge', lambda owner: bridge)
    direct = mcp._run_mcp('mcp__xuecheng__prepare_doc_edit', {'out_path': 'work/direct.md'}, task=task)
    progressive = mcp._run_progressive_mcp('call_mcp_read_tool',
        {'name': 'mcp__xuecheng__prepare_doc_edit', 'arguments': {'out_path': 'work/progressive.md'}}, task=task)
    assert not direct.is_error, direct
    assert not progressive.is_error, progressive
    assert (primary / 'work/direct.md').exists()
    assert (primary / 'work/progressive.md').exists()


def test_all_parameters_are_validated_before_output_directory_creation(workspace):
    task, primary, *_ = workspace
    declaration = {'version': 1, 'filesystem': 'server-local', 'parameters': {
        'out_path': {'access': 'write', 'create_parents': True}, 'from_file': {'access': 'read'}}}
    bridge = Bridge(meta={'tofu/local-paths': declaration})
    result = call(bridge, task, {'out_path': 'new/doc.md', 'from_file': 'missing.md'})
    assert result.is_error
    assert not (primary / 'new').exists()
    assert bridge.calls == []


def test_symlink_change_while_waiting_for_lease_refuses_invocation(workspace, tmp_path, monkeypatch):
    from contextlib import contextmanager
    from lib.project_mod import workspace_lease
    task, primary, *_ = workspace
    outside = tmp_path / 'outside-race'
    outside.mkdir()
    bridge = Bridge()

    @contextmanager
    def switched(path, **kwargs):
        (primary / 'new').symlink_to(outside, target_is_directory=True)
        yield

    monkeypatch.setattr(workspace_lease, 'workspace_write_lease', switched)
    result = call(bridge, task, {'out_path': 'new/nested/doc.md'})
    assert result.is_error
    assert bridge.calls == []
    assert not (outside / 'nested').exists()


def test_success_receipt_retains_all_runtime_result_payloads(workspace):
    task, *_ = workspace
    bridge = Bridge()
    bridge.result = MCPToolResult('saved', document_text='canonical source',
        content_blocks=({'type': 'text', 'text': 'source'},), image_contents=({'data': 'media'},))
    bridge.result.media_refs = ('owned-media',)
    result = call(bridge, task, {'out_path': 'work/doc.md'})
    assert result.__dict__ == bridge.result.__dict__
    assert 'Resolved local MCP files' in result


def test_transport_failure_after_invocation_does_not_claim_not_started(workspace):
    task, *_ = workspace
    bridge = Bridge()

    def failed(name, args, **options):
        bridge.calls.append((name, args))
        raise OSError('transport disconnected')

    bridge.call_tool = failed
    with pytest.raises(OSError, match='transport disconnected'):
        call(bridge, task, {'out_path': 'work/doc.md'})
    assert len(bridge.calls) == 1


def test_native_write_receipt_exposes_actual_primary_file(workspace, monkeypatch):
    from lib.project_mod.config import execution_root_scope
    from lib.project_mod.tools import _exec_write_file
    from lib.project_mod.tool_admission import tool_admission_scope
    from lib.project_mod import write_intents
    monkeypatch.setattr(write_intents, 'recovery_file_bytes', lambda: 1024 * 1024)
    task, primary, _, isolated, _ = workspace
    with execution_root_scope(task['id'], task['_workspaceRootPolicy']), tool_admission_scope(task):
        result = _exec_write_file({'path': 'work/script.py', 'content': 'print(42)\n'},
                                  str(primary), task['id'], task['id'], {'task': task})
    assert 'File created' in result
    assert str(primary / 'work/script.py') in result
    assert 'workspace root: autoharness' in result
    assert (primary / 'work/script.py').read_text() == 'print(42)\n'
    assert not (isolated / 'work/script.py').exists()


def test_unknown_root_cannot_self_heal_from_directory_basename(workspace):
    from lib.project_mod.path_resolution import _resolve_base
    from lib.project_mod.config import execution_root_scope, UnknownWorkspaceRootError
    task, primary, *_ = workspace
    with execution_root_scope(task['id'], task['_workspaceRootPolicy']):
        with pytest.raises(UnknownWorkspaceRootError):
            _resolve_base(str(primary / 'invented'), 'invented:doc.md', conv_id=task['id'])
