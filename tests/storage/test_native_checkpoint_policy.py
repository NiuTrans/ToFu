"""Selected embedded storage and application policy over a real private library."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import threading

import pytest

from lib.agent_core.native_process import CheckpointRequest, CheckpointTerminalRequest, TerminalRequest
from lib.identity import PrincipalContext
from lib.storage.errors import StorageError
from lib.storage.v2_embedded_client import EmbeddedStorageV2Client
from lib.tasks_pkg.native_checkpoint import NativeCheckpointPolicy
from tofu_db.embedded import EmbeddedRuntime, EmbeddedTransportError


@pytest.mark.unit
def test_selected_checkpoint_client_preserves_owner_and_transport_failure():
    class Runtime:
        closed = False
        checkpoint_supported = True
        calls = []
        def checkpoint_control(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            raise EmbeddedTransportError('lost checkpoint response')
    runtime = Runtime()
    client = EmbeddedStorageV2Client(runtime, b'x' * 32, owner_id=7, tenant_id=3)
    for owner in [8, True]:
        with pytest.raises(StorageError, match='owner differs'):
            client.native_checkpoint_available(user_id=owner)
    assert runtime.calls == []
    with pytest.raises(StorageError) as caught:
        client.native_checkpoint_control('finish', {}, user_id=7, deadline_unix_ms=1, response_bytes=4096)
    assert isinstance(caught.value.__cause__, EmbeddedTransportError)
    assert len(runtime.calls) == 1
    client.close()
    with pytest.raises(StorageError, match='client closed'):
        client.native_checkpoint_available(user_id=7)
    assert len(runtime.calls) == 1


@pytest.mark.api
@pytest.mark.parametrize('terminal_saved', [False, True])
def test_real_application_checkpoint_pause_requires_terminal_ack_before_resume(tmp_path, terminal_saved):
    library = os.environ.get('TOFU_CHECKPOINT_CABI_TEST_LIBRARY')
    binary = os.environ.get('TOFU_CHECKPOINT_CABI_TEST_BINARY')
    expected = os.environ.get('TOFU_CHECKPOINT_CABI_TEST_SOURCE_SHA256')
    if not library or not binary or not expected:
        pytest.skip('explicit private checkpoint library, binary and source identity required')
    directory = tmp_path / 'authority'
    directory.mkdir()
    subprocess.run([binary, 'init', '--data-dir', str(directory)], check=True, capture_output=True)
    principal = PrincipalContext.user(subject_id='checkpoint-app-contract', owner_user_id=7)
    task = {'id': 'task', '_userId': 7}
    request = CheckpointRequest(principal, 'task', 'load', 0, 'epoch', 'binding', threading.Event())
    state = dict(format='tofu.native-agent-checkpoint/v1', owner_user_id=7,
        task_id='task', epoch='epoch', binding='binding', next_round=1, tool_calls=1,
        messages=[dict(role='user', content='question'), dict(role='tool', content='result' * 10000)],
        usage=[{}], round_limit=3, tool_limit=3, no_progress_limit=0, failure_limit=0,
        progress=dict(codec='sha256-token-sequence/v1', last_calls=None, last_world='',
            last_evidence=None, last_failure=None, no_progress_streak=0, failure_streak=0))
    state['deadline_unix_ms'] = 18446744073709551615
    restored = []
    async def restore(callback, snapshot):
        assert callback.principal == principal
        restored.append(snapshot)
    def opened():
        return EmbeddedRuntime(Path(library), directory, owner_id=7, tenant_id=3, secret=b'x' * 32)
    def policy(client, executor, token):
        return NativeCheckpointPolicy(principal=principal, task=task, storage=client,
            executor=executor, epoch='epoch', binding='binding', token=token,
            header_bytes=4096, snapshot_bytes=1048576, identity_bytes=1024,
            io_timeout_ms=30000, restore=restore, suspend=True)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with opened() as runtime:
            assert runtime.build_identity()['source_sha256'] == expected
            client = EmbeddedStorageV2Client(runtime, b'x' * 32, owner_id=7, tenant_id=3)
            async def pause():
                checkpoint = policy(client, executor, 'first')
                assert await checkpoint.load(request) is None
                await checkpoint.begin(replace(request, action='begin'))
                assert await checkpoint.complete(replace(request, action='complete', state=state)) == 'suspend'
                terminal = TerminalRequest(principal, 'task', 'commit', 1,
                    json.dumps({'status': 'aborted'}).encode(), b'{}')
                callback = CheckpointTerminalRequest(principal, 'task', 'epoch', 'binding',
                    terminal, True, request.cancelled)
                assert await checkpoint.seal(callback) == 'accepted'
                assert await checkpoint.seal(callback) == 'accepted'
                with pytest.raises(ValueError, match='invalid'):
                    await checkpoint.seal(replace(callback, suspended=False))
                await checkpoint.close(terminal_saved=terminal_saved)
            asyncio.run(pause())
            client.close()
        with opened() as runtime:
            client = EmbeddedStorageV2Client(runtime, b'x' * 32, owner_id=7, tenant_id=3)
            async def resume():
                checkpoint = policy(client, executor, 'second')
                try:
                    if terminal_saved:
                        assert await checkpoint.load(request) == state
                        assert restored == [state]
                    else:
                        with pytest.raises(ValueError, match='ineligible'):
                            await checkpoint.load(request)
                        assert restored == []
                finally:
                    await checkpoint.close()
            asyncio.run(resume())
            client.close()


@pytest.mark.api
@pytest.mark.parametrize('lost_terminal_ack',[False,True])
@pytest.mark.parametrize('with_priced_child',[False,True])
def test_real_priced_checkpoint_reopens_with_canonical_terminal_ack(tmp_path,monkeypatch,lost_terminal_ack,with_priced_child):
    """Real stdio, one selected DB, actual task-result writer and reopened fees."""
    library=os.environ.get('TOFU_CHECKPOINT_CABI_TEST_LIBRARY')
    binary=os.environ.get('TOFU_CHECKPOINT_CABI_TEST_BINARY')
    expected=os.environ.get('TOFU_CHECKPOINT_CABI_TEST_SOURCE_SHA256')
    if not library or not binary or not expected:
        pytest.skip('explicit private checkpoint library, binary and source identity required')
    from tests.agent_core.test_native_fee_checkpoint_installed import (
        test_installed_priced_checkpoint_preserves_fees_before_next_http as run_fixture)
    from lib.tasks_pkg.native_terminal import NativeTaskTerminalPolicy,project_native_terminal
    from lib.tasks_pkg import native_terminal
    from lib.storage import service
    from types import SimpleNamespace
    directory=tmp_path/'priced-authority';directory.mkdir()
    subprocess.run([binary,'init','--data-dir',str(directory)],check=True,capture_output=True)
    principal=PrincipalContext.user(subject_id='native-task-test',owner_user_id=7)
    observations=[]
    canonical_persist=native_terminal._persist
    dropped=[]
    def persist(task):
        accepted=canonical_persist(task)
        if lost_terminal_ack and accepted and not dropped:
            dropped.append(task['id'])
            return False
        return accepted
    monkeypatch.setattr(native_terminal,'_persist',persist)
    def factory(task,executor,iteration):
        runtime=EmbeddedRuntime(Path(library),directory,owner_id=7,tenant_id=3,secret=b'x'*32)
        assert runtime.build_identity()['source_sha256']==expected
        client=EmbeddedStorageV2Client(runtime,b'x'*32,owner_id=7,tenant_id=3)
        monkeypatch.setattr(service,'storage_runtime',lambda:SimpleNamespace(client=lambda **kwargs:client))
        task.update(convId='',model='alpha',created_at=1.,content='',thinking='',toolRounds=[],segments=[])
        async def restore(request,state):
            observations.append(('load',state['shared_model_account']['tokens'],
                state['host_fee_projection']['rounds'][0]['cost']['credits']['total']))
        class SelectedCheckpoint(NativeCheckpointPolicy):
            async def complete(self,request):
                answer=await super().complete(request)
                observations.append(('complete',request.state['shared_model_account']['tokens'],
                    request.state['host_fee_projection']['rounds'][0]['cost']['credits']['total']))
                return answer
            async def close(self,*,terminal_saved=False):
                try:
                    await super().close(terminal_saved=terminal_saved)
                    row=client.query('task_results.replay_get',
                        {'key':task['id'],'user_id':7,'include_metadata':True})
                    if not terminal_saved:
                        observations.append(('terminal',False))
                        return
                    observations.append(('terminal',terminal_saved,row['status'],
                        [item['nativeCredits']['total'] for item in row['metadata']['apiRounds']]))
                finally:
                    client.close()
                    runtime.close()
        checkpoint=SelectedCheckpoint(principal=principal,task=task,storage=client,
            executor=executor,epoch='epoch',binding='fees-binding',token=f'writer-{iteration}',
            header_bytes=4096,snapshot_bytes=65536,identity_bytes=1024,
            io_timeout_ms=30000,restore=restore,suspend=True)
        terminal=NativeTaskTerminalPolicy(principal=principal,task=task,
            operation_id=f'commit-{iteration}',executor=executor,project=project_native_terminal,
            candidate_bytes=65536)
        return checkpoint,terminal
    run_fixture(tmp_path,'resume',checkpoint_factory=factory,with_priced_child=with_priced_child)
    assert observations==[('complete',10 if with_priced_child else 5,31),
        ('terminal',True,'aborted',[31,31] if with_priced_child else [31]),
        ('load',10 if with_priced_child else 5,31),
        ('terminal',True,'done',[31,31,61] if with_priced_child else [31,61])]
    assert len(dropped)==int(lost_terminal_ack)

@pytest.mark.api
def test_sdk_paid_pending_frontier_preserves_usage_before_another_round(tmp_path):
    """The generated operation inventory reaches the actual selected C ABI."""
    import time
    from tofu_db.generated_checkpoint_control import FORMAT, RESPONSE_HEADROOM_BYTES
    library = os.environ.get('TOFU_CHECKPOINT_CABI_TEST_LIBRARY')
    binary = os.environ.get('TOFU_CHECKPOINT_CABI_TEST_BINARY')
    expected = os.environ.get('TOFU_CHECKPOINT_CABI_TEST_SOURCE_SHA256')
    if not library or not binary or not expected:
        pytest.skip('explicit private checkpoint library, binary and source identity required')
    directory = tmp_path / 'pending-authority'
    directory.mkdir()
    subprocess.run([binary, 'init', '--data-dir', str(directory)], check=True, capture_output=True)
    with EmbeddedRuntime(Path(library), directory, owner_id=7, tenant_id=3, secret=b'x' * 32) as runtime:
        assert runtime.build_identity()['source_sha256'] == expected
        client = EmbeddedStorageV2Client(runtime, b'x' * 32, owner_id=7, tenant_id=3)
        def invoke(operation, **fields):
            return client.native_checkpoint_control(operation, dict(format=FORMAT, **fields),
                user_id=7, deadline_unix_ms=time.time_ns() // 1000000 + 30000,
                response_bytes=1048576 + RESPONSE_HEADROOM_BYTES)
        created = invoke('create', parameters=dict(owner_user_id=7, task_id='paid-pending',
            epoch='epoch', binding='binding', token='writer', header_bytes=4096,
            snapshot_bytes=1048576, identity_bytes=1024))
        assert created['accepted']
        capability = created['capability_id']
        assert invoke('load', capability_id=capability)['accepted']
        assert invoke('begin', capability_id=capability, round=0)['accepted']
        saved = dict(format='tofu.native-agent-checkpoint/v1', owner_user_id=7,
            task_id='paid-pending', epoch='epoch', binding='binding', next_round=1,
            tool_calls=1, tools_pending=True, usage=[dict(total_tokens=23, cost_usd=.01)],
            round_limit=3, tool_limit=3, no_progress_limit=0, failure_limit=0,
            messages=[dict(role='user', content='write result'), dict(role='assistant', tool_calls=[
                dict(id='write-1', type='function', function=dict(name='write_file', arguments='{}'))])],
            progress=dict(codec='sha256-token-sequence/v1', last_calls=None, last_world='',
                last_evidence=None, last_failure=None, no_progress_streak=0, failure_streak=0))
        assert invoke('pending', capability_id=capability, state=saved)['accepted']
        assert invoke('begin', capability_id=capability, round=1)['error'] == 'ineligible'
        assert invoke('release', capability_id=capability)['error'] == 'ineligible'
        completed = {key: value for key, value in saved.items() if key != 'tools_pending'}
        completed['messages'] = [*saved['messages'], dict(role='tool', tool_call_id='write-1', content='observed')]
        changed = dict(completed, usage=[dict(total_tokens=0)])
        assert invoke('complete', capability_id=capability, state=changed)['error'] == 'invalid'
        assert invoke('complete', capability_id=capability, state=completed)['accepted']
        assert invoke('begin', capability_id=capability, round=1)['accepted']
        client.close()
