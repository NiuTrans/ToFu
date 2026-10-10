"""Real Git push acknowledgement and request-bound canonical Overleaf file facts."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

# GitPython ships only with the excluded overleaf-MCP environment; skip the
# whole module elsewhere instead of breaking suite collection.
_git = pytest.importorskip('git', reason='overleaf-MCP environment not installed')
Repo = _git.Repo
GitCommandError = _git.exc.GitCommandError

from lib.tasks_pkg.handlers import _mcp_files as file_receipts
from lib.mcp.result_content import MCPToolResult
from lib.project_mod.receipts import contract

pytestmark = pytest.mark.unit
PROJECT = 'a' * 24
SERVER = 'renamed-papers'
TOOL = f'mcp__{SERVER}__rewrite_file'
ARGS = {'project_id': PROJECT, 'file_path': 'main.tex', 'content': 'Updated paper.'}
PACKAGE = Path(__file__).resolve().parents[2] / 'packages/tofu-overleaf-mcp'


@pytest.fixture
def git_writer(tmp_path, monkeypatch):
    """Pinned package interpreter is supplied by the standalone MCP test lane."""
    upstream = pytest.importorskip('overleaf_mcp.server')
    monkeypatch.syspath_prepend(str(PACKAGE))
    from tofu_overleaf_mcp.writes import WriteReceipts
    remote = Repo.init(tmp_path / 'remote.git', bare=True, initial_branch='master')
    local = Repo.init(tmp_path / 'working', initial_branch='master')
    with local.config_writer() as config:
        config.set_value('user', 'name', 'Fixture')
        config.set_value('user', 'email', 'fixture@example.test')
    (Path(local.working_tree_dir) / 'main.tex').write_text('\\section{Intro}\nOriginal text.\n')
    local.index.add(['main.tex'])
    local.index.commit('Initial paper')
    origin = local.create_remote('origin', str(remote.git_dir))
    origin.push('master:master').raise_if_error()
    local.head.reference.set_tracking_branch(origin.refs.master)
    client = upstream.git_client
    monkeypatch.setattr(client, '_repo_path', lambda project: Path(local.working_tree_dir))
    monkeypatch.setattr(client, 'ensure_repo', lambda project: Repo(local.working_tree_dir))
    monkeypatch.setattr(client, '_project_tag', lambda project: 'fixture paper')
    monkeypatch.setattr(client, '_PUSH_RETRIES', 0)
    monkeypatch.setattr(upstream, '_auto_setup_guard', lambda name: None)
    monkeypatch.setattr(upstream, 'get_project', lambda project: SimpleNamespace(project_id=project))
    monkeypatch.setattr(upstream, '_tofu_write_receipts', None, raising=False)
    monkeypatch.setattr(upstream, '_tofu_git_operations', None, raising=False)
    # Restore the pinned push seam even though the adapter installs it at startup.
    monkeypatch.setattr(client, '_retry_push', client._retry_push)
    writer = WriteReceipts(upstream)
    try:
        yield writer, local, remote
    finally:
        local.close()
        remote.close()


def test_transport_factories_share_the_same_checked_write_adapter(git_writer):
    writer, _, remote = git_writer
    from tofu_overleaf_mcp.writes import upstream_writes
    from tofu_overleaf_mcp.server import create_server
    create_server(None, writer.upstream)
    create_server(None, writer.upstream)
    receipt = asyncio.run(upstream_writes(writer.upstream).invoke('rewrite_file', ARGS))
    assert receipt['source_revision'] == remote.head.commit.hexsha
    assert (remote.head.commit.tree / 'main.tex').data_stream.read() == ARGS['content'].encode()


@pytest.mark.parametrize('name,changes', [
    ('create_file', {'file_path': 'chapters/new.tex', 'content': 'New chapter.'}),
    ('rewrite_file', {'content': 'Replacement paper.'}),
    ('edit_file', {'old_string': 'Original text.', 'new_string': 'Edited text.'}),
    ('update_section', {'section_title': 'Intro', 'new_content': 'Updated section.'}),
    ('delete_file', {}),
    ('upload_file', {'overwrite': True}),
])
def test_each_upstream_write_returns_the_acknowledged_remote_commit(git_writer, tmp_path, name, changes):
    writer, local, remote = git_writer
    args = {'project_id': PROJECT, 'file_path': 'main.tex', **changes,
            'tofu_write_receipt': {'request_id': 'b' * 32, 'owner_user_id': 7}}
    if name == 'upload_file':
        source = tmp_path / 'figure.bin'
        source.write_bytes(b'\x00\xffbinary figure')
        args['source_path'] = str(source)
    receipt = asyncio.run(writer.invoke(name, args))
    assert receipt['request_id'] == 'b' * 32 and receipt['owner_user_id'] == 7
    assert receipt['project_id'] == PROJECT and receipt['source_revision'] == remote.head.commit.hexsha
    entry, = receipt['file_changes']
    assert entry['path'] == args['file_path']
    assert entry['action'] == contract()['x-mcp-write-tools'][name]
    if name == 'delete_file':
        assert 'sourceHash' not in entry and not (Path(local.working_tree_dir) / entry['path']).exists()
    else:
        blob = remote.head.commit.tree / entry['path']
        assert hashlib.sha256(blob.data_stream.read()).hexdigest() == entry['sourceHash']
    assert writer._active is None


def test_a_rejected_push_cannot_confirm_or_silently_publish_with_the_next_write(git_writer):
    writer, local, remote = git_writer
    previous = remote.head.commit.hexsha
    hook = Path(remote.git_dir) / 'hooks/pre-receive'
    hook.write_text('#!/bin/sh\nexit 1\n')
    hook.chmod(0o700)
    with pytest.raises(GitCommandError):
        asyncio.run(writer.invoke('rewrite_file', ARGS))
    unpublished = local.head.commit.hexsha
    assert remote.head.commit.hexsha == previous and unpublished != previous
    hook.unlink()
    with pytest.raises(ValueError, match='write_cache_unpublished'):
        asyncio.run(writer.invoke('rewrite_file', {**ARGS, 'content': 'Next write.'}))
    assert local.head.commit.hexsha == unpublished and remote.head.commit.hexsha == previous
    assert (Path(local.working_tree_dir) / 'main.tex').read_text() == ARGS['content']


def test_transport_failure_cannot_confirm_a_committed_local_file(git_writer, monkeypatch):
    writer, _, remote = git_writer
    previous = remote.head.commit.hexsha
    from tofu_overleaf_mcp import writes
    def fail(*args, **kwargs):
        raise GitCommandError('git push', 128, stderr='disconnected')
    monkeypatch.setattr(writes, 'push_commit', fail)
    with pytest.raises(GitCommandError):
        asyncio.run(writer.invoke('rewrite_file', ARGS))
    assert remote.head.commit.hexsha == previous


def test_cancelled_writer_keeps_its_slot_until_the_upstream_thread_retires(git_writer, monkeypatch):
    writer, _, remote = git_writer
    previous = remote.head.commit.hexsha
    entered, resume = threading.Event(), threading.Event()
    original = writer._prepare
    def held_prepare(state):
        entered.set()
        assert resume.wait(5)
        return original(state)
    monkeypatch.setattr(writer, '_prepare', held_prepare)
    async def run():
        caller = asyncio.create_task(writer.invoke('rewrite_file', ARGS))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            running = writer._active
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            with pytest.raises(ValueError, match='write_busy'):
                await writer.invoke('rewrite_file', ARGS)
        finally:
            resume.set()
            if writer._active:
                await asyncio.gather(writer._active, return_exceptions=True)
        assert running.done() and writer._active is None
    asyncio.run(run())
    assert remote.head.commit.hexsha == previous


@pytest.mark.parametrize('cancelled', [False, True])
def test_native_push_timeout_or_cancel_kills_and_reaps_the_process(tmp_path, monkeypatch, cancelled):
    if sys.platform == 'win32':
        pytest.skip('The executable fixture uses a POSIX shebang')
    monkeypatch.syspath_prepend(str(PACKAGE))
    from tofu_overleaf_mcp import git_push
    from tofu_overleaf_mcp.writes import _Write, WriteError
    import psutil
    executable = tmp_path / 'git-fixture'
    executable.write_text(f'#!{sys.executable}\nimport time\ntime.sleep(60)\n')
    executable.chmod(0o700)
    state = _Write(PROJECT, 'main.tex', 'written', 'b' * 32, 7, time.monotonic() + 3)
    started = []
    original = git_push.subprocess.Popen
    def start(*args, **kwargs):
        command = args[0]
        assert 'pack.threads=1' in command
        assert f'pack.windowMemory={contract()["x-resource-limits"]["overleaf_pack_window_bytes"]}' in command
        assert f'pack.deltaCacheSize={contract()["x-resource-limits"]["overleaf_pack_delta_bytes"]}' in command
        process = original(*args, **kwargs)
        started.append(process)
        if cancelled:
            state.cancel.set()
        else:
            state.deadline = time.monotonic() + 0.15
        return process
    monkeypatch.setattr(git_push.subprocess, 'Popen', start)
    repo = SimpleNamespace(git=SimpleNamespace(GIT_PYTHON_GIT_EXECUTABLE=str(executable)), working_tree_dir=str(tmp_path))
    with pytest.raises(WriteError, match='write_cancelled'):
        git_push.push_commit(repo, 'c' * 40, 'refs/heads/master', state, limits=contract()['x-resource-limits'])
    assert len(started) == 1 and started[0].poll() is not None
    assert not psutil.pid_exists(started[0].pid)


@pytest.mark.parametrize('path', ['../main.tex', '/main.tex', '.git/config', 'a/.GIT/config', 'a\\main.tex'])
def test_unsafe_write_paths_are_refused_before_source_or_remote_mutation(git_writer, path):
    writer, local, remote = git_writer
    before = local.head.commit.hexsha
    with pytest.raises(ValueError, match='write_path_invalid'):
        asyncio.run(writer.invoke('rewrite_file', {**ARGS, 'file_path': path}))
    assert local.head.commit.hexsha == remote.head.commit.hexsha == before


def test_symlink_aliases_cannot_redirect_a_write_into_git_metadata(git_writer):
    writer, local, remote = git_writer
    alias = Path(local.working_tree_dir) / 'metadata'
    alias.symlink_to('.git', target_is_directory=True)
    local.index.add(['metadata'])
    local.index.commit('Symlink fixture')
    local.remotes.origin.push('master:master').raise_if_error()
    before = local.head.commit.hexsha
    with pytest.raises(ValueError, match='write_path_invalid'):
        asyncio.run(writer.invoke('rewrite_file', {**ARGS, 'file_path': 'metadata/config'}))
    assert local.head.commit.hexsha == remote.head.commit.hexsha == before


def test_write_payload_budget_is_enforced_before_mutating_a_file(git_writer):
    writer, local, remote = git_writer
    before = local.head.commit.hexsha
    content = 'x' * (contract()['x-resource-limits']['overleaf_file_bytes'] + 1)
    with pytest.raises(ValueError, match='write_file_limit'):
        asyncio.run(writer.invoke('rewrite_file', {**ARGS, 'content': content}))
    assert local.head.commit.hexsha == remote.head.commit.hexsha == before


def _definition(name):
    value = contract()
    return {**value['$defs'][name],
            **({'$defs': value['$defs']} if name == 'OverleafWriteReceiptV1' else {})}


class Bridge:
    def __init__(self):
        self.info = {'server_name': SERVER, 'tool_name': 'rewrite_file', 'read_only_hint': False,
            'meta': {'tofu/project-file-receipts': {'schema_version': 1, 'source': 'overleaf'}},
            'openai_def': {'function': {'parameters': {'type': 'object', 'properties': {
                'tofu_write_receipt': _definition('OverleafWriteControlV1')}}}},
            'output_schema': _definition('OverleafWriteReceiptV1')}
        self.calls = []
        self.mutate = lambda value: None
        self.after_call = lambda: None
        self.structured = True
        self.error = False

    def get_tool_info(self, name):
        assert name == TOOL
        return copy.deepcopy(self.info)

    def call_tool(self, name, arguments, **options):
        self.calls.append((name, copy.deepcopy(arguments), options))
        identity = arguments.get('tofu_write_receipt', {'request_id': 'e' * 32, 'owner_user_id': 7})
        value = {'file_receipts_version': 1, 'source': 'overleaf', 'project_id': PROJECT, **identity,
            'source_revision': 'f' * 40, 'output': 'Written',
            'file_changes': [{'changeId': 'd' * 32, 'path': 'main.tex', 'action': 'written', 'sourceHash': 'c' * 64}]}
        self.mutate(value)
        self.after_call()
        return MCPToolResult('Written', structured_json=json.dumps(value) if self.structured else None, is_error=self.error)


@pytest.fixture
def host(monkeypatch):
    from lib.tasks_pkg.commit_round import _derive
    projected = []
    monkeypatch.setattr(_derive, 'note_overleaf_file_changes', lambda *args: projected.append(args))
    return Bridge(), {'id': 'task-a', '_userId': 7, 'abort_event': threading.Event()}, projected


def test_host_binds_fresh_request_owner_server_and_actual_write_contract(host):
    bridge, task, projected = host
    original = copy.deepcopy(ARGS)
    result = file_receipts.call_with_file_receipts(bridge, TOOL, ARGS, task=task)
    assert not result.is_error and ARGS == original
    assert projected[0][:4] == ('task-a', 7, SERVER, PROJECT)
    assert projected[0][4][0]['sourceRevision'] == 'f' * 40
    options = bridge.calls[0][2]
    assert options['retry_on_disconnect'] is False and options['cancel'] is task['abort_event']
    assert options['timeout_override'] == contract()['x-resource-limits']['overleaf_write_seconds']
    file_receipts.call_with_file_receipts(bridge, TOOL, ARGS, task=task)
    assert bridge.calls[0][1]['tofu_write_receipt']['request_id'] != bridge.calls[1][1]['tofu_write_receipt']['request_id']


@pytest.mark.parametrize('field', ['request_id', 'owner_user_id', 'project_id', 'source_revision', 'path', 'action', 'extra'])
def test_host_rejects_foreign_replayed_and_malformed_receipts(host, field):
    bridge, task, projected = host
    def mutate(value):
        if field in ('path', 'action'):
            value['file_changes'][0][field] = 'other.tex' if field == 'path' else 'deleted'
        elif field == 'extra':
            value['file_changes'].append(dict(value['file_changes'][0]))
        else:
            value[field] = {'request_id': 'e' * 32, 'owner_user_id': 8,
                            'project_id': 'b' * 24, 'source_revision': 'not-a-commit'}[field]
    bridge.mutate = mutate
    result = file_receipts.call_with_file_receipts(bridge, TOOL, ARGS, task=task)
    assert result.is_error and not projected


@pytest.mark.parametrize('fault', ['legacy', 'error', 'changed_before', 'changed_after', 'changed_server'])
def test_text_and_changed_capabilities_never_establish_commit_success(host, fault):
    bridge, task, projected = host
    if fault == 'legacy':
        bridge.structured = False
    elif fault == 'error':
        bridge.error = True
    elif fault == 'changed_before':
        bridge.info['output_schema'] = {}
    elif fault == 'changed_after':
        bridge.after_call = lambda: bridge.info.update(output_schema={})
    else:
        bridge.after_call = lambda: bridge.info.update(server_name='other-server')
    result = file_receipts.call_with_file_receipts(bridge, TOOL, ARGS, task=task)
    assert result.is_error and not projected
    assert len(bridge.calls) == (0 if fault == 'changed_before' else 1)


def test_foreign_terminal_and_aborted_tasks_cannot_receive_facts_or_lose_them_at_settlement(monkeypatch):
    from lib.tasks_pkg.commit_round import _derive
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    tasks = [{'id': 'task-a', '_userId': owner, 'status': status, 'aborted': aborted}
             for owner, status, aborted in [(7, 'running', False), (8, 'running', False),
                                             (7, 'done', False), (7, 'running', True)]]
    def update_matching(*, predicate, updater):
        matched = [task for task in tasks if predicate(task)]
        for task in matched:
            updater(task)
        return matched
    monkeypatch.setattr(chat_task_runtime, 'update_matching', update_matching)
    entry = {'changeId': 'd' * 32, 'path': 'main.tex', 'action': 'written', 'sourceHash': 'c' * 64}
    assert _derive.note_overleaf_file_changes('task-a', 7, SERVER, PROJECT, [entry])
    assert not any('modifiedFileList' in task for task in tasks[1:])
    facts, count, fallback = _derive.derive_round_modified_files(tasks[0], None, [])
    assert count == 1 and not fallback and facts == tasks[0]['modifiedFileList']
    assert facts[0]['source'] == 'overleaf' and facts[0]['mcpServer'] == SERVER
    assert 'rootPath' not in facts[0]
    assert _derive.note_overleaf_file_changes('task-a', 7, SERVER, 'b' * 24, [entry])
    assert _derive.note_overleaf_file_changes('task-a', 7, 'other-server', PROJECT, [entry])
    assert len(tasks[0]['modifiedFileList']) == 3
    merged = _derive.merge_live_file_entry(tasks[0]['modifiedFileList'], {'path': 'main.tex', 'action': 'written'})
    assert len(merged) == 4


def test_real_stdio_direct_and_progressive_writes_reach_canonical_receipts(git_writer, host, tmp_path, monkeypatch):
    from lib.mcp.client import MCPBridge
    from lib.tasks_pkg.handlers import mcp as handler
    import lib.mcp
    _, local, remote = git_writer
    _, task, projected = host
    bootstrap = tmp_path / 'stdio_writer.py'
    from tests.support.overleaf_git import worker_command
    command = worker_command(tmp_path, remote.git_dir)
    bootstrap.write_text('from tofu_overleaf_mcp.git_operations import GitOperations\n'
        + '''from pathlib import Path
from types import SimpleNamespace
import sys
from git import Repo
from overleaf_mcp import server as upstream
from tofu_overleaf_mcp.server import main
root = Path(sys.argv[1])
upstream.git_client._repo_path = lambda project: root
upstream.git_client.ensure_repo = lambda project: Repo(root)
upstream.git_client._project_tag = lambda project: 'fixture paper'
upstream.git_client._PUSH_RETRIES = 0
upstream.get_project = lambda project: SimpleNamespace(project_id=project)
upstream._auto_setup_guard = lambda name: None
''' + f'upstream._tofu_git_operations = GitOperations(command={command!r}, temporary_root={str(tmp_path / "stdio-jobs")!r})\nmain()\n')
    bridge = MCPBridge()
    monkeypatch.setattr(lib.mcp, 'get_bridge', lambda owner: bridge)
    try:
        bridge.connect_server(SERVER, {'command': sys.executable,
            'args': [str(bootstrap), local.working_tree_dir], 'enabled': True,
            'env': {'OVERLEAF_TEMP_DIR': str(tmp_path / 'unused-cache'), 'OVERLEAF_GIT_TOKEN': 'fixture-git'}})
        for progressive in (False, True):
            args = {**ARGS, 'content': f'Committed through MCP: {progressive}'}
            result = (handler._run_progressive_mcp('call_mcp_write_tool', {'name': TOOL, 'arguments': args}, task=task)
                      if progressive else handler._run_mcp(TOOL, args, task=task))
            assert not getattr(result, 'is_error', True), str(result)
            receipt = json.loads(result.structured_json)
            assert receipt['source_revision'] == remote.head.commit.hexsha
            assert (remote.head.commit.tree / 'main.tex').data_stream.read() == args['content'].encode()
        assert len(projected) == 2
        assert all(row[:4] == ('task-a', 7, SERVER, PROJECT) for row in projected)
        assert projected[0][4][0]['changeId'] != projected[1][4][0]['changeId']
    finally:
        bridge.disconnect_all()
