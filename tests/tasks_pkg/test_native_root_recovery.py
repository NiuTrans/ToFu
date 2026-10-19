"""Real ROOT pause/reopen keeps the durable frontier, tool effects and credit.

Uses one explicitly verified private embedded authority and loopback provider.
The journal owns recovery; the existing ROOT and Turn owners own preparation
and terminal publication. No storage switch or production service is involved.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.api


@pytest.mark.parametrize('phase', ['completed', 'inflight'])
@pytest.mark.parametrize('priced_tree,child_model',[(False,'alpha'),(True,'alpha'),(True,'beta')])
def test_actual_root_sigkill_recovery_requires_completed_frontier(tmp_path,phase,priced_tree,child_model):
    import signal
    import sys
    archive=os.environ.get('TOFU_NATIVE_TERMINAL_TEST_ARCHIVE')
    library=os.environ.get('TOFU_CHECKPOINT_CABI_TEST_LIBRARY')
    binary=os.environ.get('TOFU_CHECKPOINT_CABI_TEST_BINARY')
    source=os.environ.get('TOFU_CHECKPOINT_CABI_TEST_SOURCE_SHA256')
    if not all((archive,library,binary,source)):
        pytest.skip('verified native archive and private checkpoint engine required')
    if sys.platform!='linux':pytest.skip('Linux process-death proof contract')
    from benchmarks.native_agent_root import RootWorkload
    from lib.agent_core.native_process import Budget
    from lib.resource_control.runtime import launch_policy
    directory=tmp_path/'authority';directory.mkdir()
    subprocess.run([binary,'init','--data-dir',str(directory)],check=True,capture_output=True)
    launcher=json.loads(os.environ.get('TOFU_NATIVE_CRASH_TEST_PYTHON_COMMAND','null')) or [sys.executable]
    assert isinstance(launcher,list) and all(isinstance(v,str) for v in launcher)
    with ThreadPoolExecutor(max_workers=1) as executor:
        provider=RootWorkload(storage=None,host=None,executor=executor,directory=tmp_path,
            budget=Budget(1,16<<20,1<<20,1<<20,5),resource_policy=launch_policy({}),
            scenario='mixed_direct_first',expected_wire_models=('alpha-wire-a',child_model+'-wire-a'))
        process=None
        values={'directory':str(tmp_path),'archive':archive,'library':library,'source_sha256':source,
            'task_id':uuid.uuid4().hex,'conversation_id':uuid.uuid4().hex,'deadline':time.time()+300,
            'endpoint':f'http://127.0.0.1:{provider.server.server_port}/v1','resume':False,'phase':phase,
            'ready':str(tmp_path/'ready.json'),'result':str(tmp_path/'result.json'),'priced_tree':priced_tree,'child_model':child_model}
        configuration=tmp_path/'configuration.json'
        environment={**os.environ,'TOFU_PERSONAL_STORAGE_BACKEND':'tofudb','TOFUDB_TRANSPORT':'embedded',
                     'TOFUDB_BIN':binary}
        def launch(log):
            configuration.write_text(json.dumps(values)+'\n')
            return subprocess.Popen([*launcher,'-m','tests.support.native_root_crash_probe',str(configuration)],
                cwd=Path(__file__).resolve().parents[2],env=environment,stdout=log,stderr=subprocess.STDOUT,
                start_new_session=True)
        try:
            with (tmp_path/'first.log').open('wb') as log:
                process=launch(log)
                expires=time.monotonic()+30
                while not Path(values['ready']).exists():
                    assert process.poll() is None,(tmp_path/'first.log').read_text()[-3000:]
                    assert time.monotonic()<expires,'ROOT never reached the durable crash boundary'
                    time.sleep(.02)
                first=json.loads(Path(values['ready']).read_text())
                assert first['phase']==phase
                expected_tokens=(15 if priced_tree else 5) if phase=='completed' else 0
                expected_requests=(3 if priced_tree else 1) if phase=='completed' else 0
                assert first['paid_tokens']==expected_tokens
                assert len(provider.bodies)==expected_requests
                os.killpg(process.pid,signal.SIGKILL)
                assert process.wait(timeout=10)==-signal.SIGKILL
            values.update(resume=True,turn_id=first['turn_id'])
            if priced_tree:values['wallet_id']=first['wallet_id']
            with (tmp_path/'second.log').open('wb') as log:
                process=launch(log)
                assert process.wait(timeout=30)==0,(tmp_path/'second.log').read_text()[-3000:]
            result=json.loads(Path(values['result']).read_text())
            assert result['joined_cleanup'] and result['pending_model_requests']==0
            if phase=='completed':
                assert result['status']=='done' and result['attempt_status']=='completed',result
                assert result['paid_tokens']==(20 if priced_tree else 10) and result['tool_calls']==2,result
                assert result['restored_frontiers']==[1] and result['checklist_updates']==1,result
                assert result['fee_rounds']==(4 if priced_tree else 2) and len(provider.bodies)==(4 if priced_tree else 2),result
                if priced_tree:
                    expected_charge=70 if child_model=='beta' else 112
                    assert result['wallet_charge']==expected_charge and result['wallet_balance']==10000-expected_charge,result
                    assert result['fee_models'].count(child_model)==(2 if child_model=='beta' else 4)
                    assert result['original_child_quotes']==([7,7] if child_model=='beta' else [28,28])
                    assert result['root_tokens']==10 and result['wallet_ack_retry_idempotent']
                feedback=[m for m in provider.bodies[-1]['messages'] if m['role']=='tool']
                assert len(feedback)==2
            else:
                assert result['error'] and result['restored_frontiers']==[],result
                assert not provider.bodies and result['paid_tokens']==result['tool_calls']==0,result
                if priced_tree:assert result['wallet_balance']==9900 and result['wallet_hold_retained'],result
            destination=os.environ.get('TOFU_NATIVE_ROOT_RECOVERY_RECEIPTS')
            if destination:
                path=Path(destination);path.mkdir(parents=True,exist_ok=True)
                receipt={'scope':'actual_ROOT_SIGKILL','phase':phase,'native_source_sha256':source,
                    'provider_requests':len(provider.bodies),'old_process_exit':-signal.SIGKILL,
                    'resumed':result,'writer_process_json_bytes':first['writer_process_json_bytes'],
                    'same_executor_identity':True,'public_journal_takeover':False,'priced_tree':priced_tree,'child_model':child_model,
                    'checkpoint_json_bytes':first.get('checkpoint_json_bytes'),
                    'child_route_metadata_json_bytes':first.get('child_route_metadata_json_bytes'),
                    'default_selection_admitted':False,'performance_admitted':False}
                (path/('sigkill-'+phase+('-priced-tree-'+child_model if priced_tree else '')+'.json')).write_text(json.dumps(receipt,indent=2)+'\n')
        finally:
            if process is not None and process.poll() is None:
                os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=10)
            provider.close()


@pytest.mark.parametrize('lost_terminal_ack', [False, True])
@pytest.mark.parametrize('finite_native_limits', [False, True])
def test_actual_root_checkpoint_reopens_without_replaying_settled_tools(
        tmp_path, monkeypatch, lost_terminal_ack, finite_native_limits):
    archive = os.environ.get('TOFU_NATIVE_TERMINAL_TEST_ARCHIVE')
    library = os.environ.get('TOFU_CHECKPOINT_CABI_TEST_LIBRARY')
    binary = os.environ.get('TOFU_CHECKPOINT_CABI_TEST_BINARY')
    source = os.environ.get('TOFU_CHECKPOINT_CABI_TEST_SOURCE_SHA256')
    if not all((archive, library, binary, source)):
        pytest.skip('verified native archive and private checkpoint engine required')
    from benchmarks.native_agent_root import RootWorkload
    from lib.agent_core.abort_signal import AbortSignal
    from lib.agent_core.execution_budget import ExecutionBudget
    from lib.agent_core.native_image import NativeArchiveImage
    from lib.agent_core.native_process import Budget
    from lib.identity import PrincipalContext
    from lib.model_routing.dispatch_adapter import mint_routed_slot_group, dispose_routed_slot_group
    from lib.model_routing.native_route import ROUTE_OVERRIDE_FIELDS
    from lib.model_routing.repository import OwnerBoundary
    from lib.resource_control.runtime import launch_policy
    from lib.storage import service
    from lib.storage.v2_embedded_client import EmbeddedStorageV2Client
    from lib.tasks_pkg import native_terminal
    from lib.tasks_pkg.native_checkpoint import NativeCheckpointPolicy
    from lib.tasks_pkg.native_host import NativeHost
    from lib.tasks_pkg.native_invocation import NativePreparedInvocation
    from lib.tasks_pkg.native_pricing import NativeRoutePricing
    from lib.tasks_pkg.orchestrator._run import execute_task_invocation, ROOT_INVOCATION_POLICY
    from lib.turn_lifecycle import create_turn_pair, create_attempt, bind_task, get_turn, get_attempt
    from scripts.build_native_runtime import verify
    from tests.model_routing.test_native_route import Repository, selection
    from tests.support.native_process_probe import config
    from tofu_db.embedded import EmbeddedRuntime

    directory = tmp_path / 'authority'
    directory.mkdir()
    subprocess.run([binary, 'init', '--data-dir', str(directory)], check=True, capture_output=True)
    manifest = verify(Path(archive))
    budget = Budget(1, 16 << 20, 1 << 20, 1 << 20, 5)
    principal = PrincipalContext.user(subject_id='root-recovery', owner_user_id=1)
    task_id, conv_id = uuid.uuid4().hex, uuid.uuid4().hex
    settings = dict(model='alpha', maxTokens=64, thinkingEnabled=False,
        memoryEnabled=False, projectEnabled=False, searchMode='off',
        tools={'programmaticCalling': 'off'}, executionLimits={
            'max_tokens': 100000, 'max_tool_calls': 3, 'deadline': time.time() + 300})
    restored, snapshots, attempts, accounts = [], [], [], []
    persist = native_terminal._persist
    dropped = []
    def persist_with_fault(task):
        accepted = persist(task)
        if lost_terminal_ack and accepted and not dropped:
            dropped.append(task['id'])
            return False
        return accepted
    monkeypatch.setattr(native_terminal, '_persist', persist_with_fault)
    from lib.tasks_pkg.orchestrator import _root_agent_loop, _tool_request_metadata
    prepare, attach = _root_agent_loop.prepare_round_context, _tool_request_metadata.attach_tool_request_metadata
    marker = 'Verify the restored tool frontier after context admission.'
    def prepare_with_marker(task, messages, **values):
        prepare(task, messages, **values)
        if not any(message.get('content') == marker for message in messages):
            messages.append({'role':'user', 'content':marker})
    def attach_after_admission(task, body, messages, *args, **values):
        assert any(message.get('content') == marker for message in messages)
        return attach(task, body, messages, *args, **values)
    monkeypatch.setattr(_root_agent_loop, 'prepare_round_context', prepare_with_marker)
    monkeypatch.setattr(_tool_request_metadata, 'attach_tool_request_metadata', attach_after_admission)

    async def run():
        with ThreadPoolExecutor(max_workers=1) as executor:
            provider = RootWorkload(storage=None, host=None, executor=executor,
                directory=tmp_path, budget=budget, resource_policy=launch_policy({}),
                scenario='mixed_direct_first')
            try:
                pair = None
                for iteration in range(2):
                    with EmbeddedRuntime(Path(library), directory, owner_id=1,
                            tenant_id=1, secret=b'x' * 32) as native:
                        assert native.build_identity()['source_sha256'] == source
                        storage = EmbeddedStorageV2Client(native, b'x' * 32, owner_id=1, tenant_id=1)
                        monkeypatch.setattr(service, 'storage_runtime', lambda: SimpleNamespace(client=lambda **kw: storage))
                        execution_settings = dict(settings)
                        if pair is None:
                            pair = create_turn_pair(conv_id, command_id=conv_id, input_projection={'content':'question'},
                                config=settings, user_id=1, conversation_defaults={
                                    'allowCreate':True, 'title':'ROOT recovery', 'settings':{}})
                        else:
                            from lib.conversation_sync.command_service import _resume_executor_config
                            turn = get_turn(conv_id, pair['turn']['turnId'], user_id=1)
                            execution_settings.update(_resume_executor_config('continue', turn['projection']))
                            pair = create_attempt(conv_id, turn['turnId'], command_id=uuid.uuid4().hex,
                                operation='continue', expected_projection_revision=turn['projectionRevision'],
                                config=execution_settings, user_id=1)
                        attempt_id = pair['attempt']['attemptId']
                        attempts.append(attempt_id)
                        bind_task(attempt_id, task_id, user_id=1)
                        task = dict(id=task_id, convId=conv_id, _turnId=pair['turn']['turnId'],
                            _attemptId=attempt_id, _userId=1, _principalContext=principal.to_payload(),
                            status='running', aborted=False, content='', thinking='', toolRounds=[], segments=[],
                            config=execution_settings, created_at=time.time(), abort_event=AbortSignal(),
                            content_lock=threading.Lock(), events=[], events_lock=threading.Lock(),
                            _attended=True, _dispatch_heartbeat=0., _t_last_event=0.,
                            messages=[{'role':'user', 'content':'question'}])
                        if iteration:
                            # Display continuity comes from the selected Turn authority;
                            # the native journal restores executable model history later.
                            task['segments'] = turn['projection']['segments']
                        account = ExecutionBudget(task)
                        task['_executionBudget'] = account
                        accounts.append(account)
                        repo = Repository()
                        repo.boundary = OwnerBoundary.create(1)
                        repo.document['connections'][1]['enabled'] = False
                        repo.document['connections'][0]['base_url'] = f'http://127.0.0.1:{provider.server.server_port}/v1'
                        group = mint_routed_slot_group(repo, repo.boundary, selection(), owner_tag='task:' + task_id)
                        task['_model_routing_group'], task['_pinned_provider_id'] = group, group.pin_id
                        async def restore(request, state):
                            restored.append(state)
                        class Checkpoint(NativeCheckpointPolicy):
                            async def complete(self, request):
                                snapshots.append(request.state)
                                return await super().complete(request)
                        checkpoint = Checkpoint(principal=principal, task=task, storage=storage, executor=executor,
                            epoch='root-epoch', binding='same-model-tools-account', token=f'worker-{iteration}',
                            header_bytes=4096, snapshot_bytes=1 << 20, identity_bytes=1024,
                            io_timeout_ms=30000, restore=restore, suspend=iteration == 0)
                        profile = config('ignored', task_id)
                        for key in ({'version','type','owner_user_id','task_id','messages','tools',
                                'rounds','deadline_ms','tool_calls','event_bytes','output_bytes','request_bytes',
                                'retained_bytes','feedback_bytes','tool_frame_bytes'} | ROUTE_OVERRIDE_FIELDS):
                            profile.pop(key, None)
                        profile.update(epoch='root-epoch', checkpoint_shared_account=True,
                            checkpoint_frame_bytes=1 << 20, model_control={'ack_ms':5000, 'pricing':True},
                            model_counter=dict(input_bytes=1 << 20, text_bytes=1 << 20, tool_json_bytes=1 << 20,
                                depth=128, worker_stack_bytes=1 << 20),
                            approval_control=dict(receipts=1 << 20, identity_bytes=1 << 20),
                            terminal_persistence=dict(operation_id=f'commit-{iteration}', attempts=2,
                                ack_ms=5000, frame_bytes=1 << 20))
                        if finite_native_limits:
                            profile.update(rounds=3, total_tool_calls=8, deadline_ms=10000)
                        image = NativeArchiveImage(archive=Path(archive), sha256=hashlib.sha256(Path(archive).read_bytes()).hexdigest(),
                            application_version=manifest['build']['application_version'], budget=budget)
                        host = NativeHost(image)
                        task['_nativePreparedInvocation'] = NativePreparedInvocation(host=host,
                            loop=asyncio.get_running_loop(), principal=principal, repository=repo, selection=selection(),
                            configuration=profile, budget=budget, executor=executor, working_directory=tmp_path,
                            pending_calls=8, pending_bytes=1 << 20, resource_policy=launch_policy({}),
                            checkpoint_policy=checkpoint, pricing_resolver=NativeRoutePricing(principal=principal,
                                margin=0, lookup=lambda *a, **kw: {'input':2.5, 'output':10}, rates=lambda p:7.24))
                        try:
                            await asyncio.to_thread(execute_task_invocation, task, policy=ROOT_INVOCATION_POLICY)
                            assert task['status'] == ('aborted' if iteration == 0 else 'done'), task.get('error')
                            assert get_attempt(attempt_id, user_id=1)['status'] == ('interrupted' if iteration == 0 else 'completed')
                            assert not host._clients and checkpoint.closed
                            assert account.continuity_snapshot()['pending_model_requests'] == 0
                            if iteration:
                                final_turn = get_turn(conv_id, pair['turn']['turnId'], user_id=1)
                                assert final_turn['status'] == 'completed'
                                assert final_turn['projection']['usage']['total_tokens'] == 10
                                assert len(final_turn['projection']['apiRounds']) == len(task['apiRounds']) == 2
                                assert task['_todoState']['update_count'] == 1
                                assert task['apiRounds'][0]['nativePrice'] == restored[0]['host_fee_projection']['rounds'][0]['cost']['price']
                        finally:
                            dispose_routed_slot_group(group)
                            await host.close()
                            storage.close()
                assert len(provider.bodies) == 2
                assert len(restored) == 1 and restored[0]['next_round'] == 1
                assert accounts[0].consumption_snapshot()[0] == 5
                assert accounts[1].consumption_snapshot()[0] == 10
                assert accounts[0].consumption_snapshot()[1] == accounts[1].consumption_snapshot()[1]
                feedback = [m['content'] for m in provider.bodies[-1]['messages'] if m['role'] == 'tool']
                assert len(feedback) == 2 and any('delegated' in value for value in feedback)
                assert snapshots[0]['shared_model_account']['tokens'] == 5
                assert snapshots[0]['host_fee_projection']['rounds']
                assert snapshots[0]['round_limit'] == (3 if finite_native_limits else None)
                assert snapshots[0]['tool_limit'] == (8 if finite_native_limits else None)
                assert snapshots[0]['shared_model_account']['limits']['deadline'] == settings['executionLimits']['deadline']
                assert dropped == ([task_id] if lost_terminal_ack else [])
                destination = os.environ.get('TOFU_NATIVE_ROOT_RECOVERY_RECEIPTS')
                if destination:
                    receipt = dict(scope='actual_root_pause_reopen', native_source_sha256=source,
                        archive_sha256=image.sha256, native_limits='finite' if finite_native_limits else 'null',
                        terminal_ack='retry' if lost_terminal_ack else 'first', attempts=len(attempts),
                        provider_requests=len(provider.bodies), restored_frontier=restored[0]['next_round'],
                        tokens=[account.consumption_snapshot()[0] for account in accounts],
                        tool_calls=[account.consumption_snapshot()[1] for account in accounts],
                        fee_rounds=len(restored[0]['host_fee_projection']['rounds']),
                        final_fee_rounds=len(task['apiRounds']), checklist_updates=task['_todoState']['update_count'],
                        restored_tool_feedback=len(feedback), joined_cleanup=True,
                        default_selection_admitted=False, performance_admitted=False)
                    path = Path(destination)
                    path.mkdir(parents=True, exist_ok=True)
                    (path / (receipt['native_limits'] + '-' + receipt['terminal_ack'] + '.json')).write_text(
                        json.dumps(receipt, indent=2) + '\n')
            finally:
                provider.close()
    asyncio.run(run())
