"""Actual HTTP Goal/ROOT crash, accepted takeover, file reconciliation and settlement."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tests.support.native_goal_restart import accept_recovery, file_provider, kill_at_ready

pytestmark = pytest.mark.api


@pytest.mark.parametrize('actors,phase', [(1,'file_not_published'), (1,'file_published'),
                                       (2,'file_not_published'), (2,'file_published')])
@pytest.mark.parametrize('lost_handoff_ack', [False, True], ids=['acknowledged','lost_handoff_ack'])
def test_genuine_goal_source_checkpoint_recovers_or_fences_without_duplicate_effects(
        tmp_path, monkeypatch, actors, phase, lost_handoff_ack):
    from lib.billing import get_balance
    from lib.goal_runs.pending_claim import claim_pending_goal_actors
    from lib.identity import PrincipalContext
    from lib.storage import service
    from lib.storage.v2_embedded_client import EmbeddedStorageV2Client
    from lib.turn_lifecycle import get_attempt
    from tofu_db.embedded import EmbeddedRuntime

    names = ('TOFU_CHECKPOINT_CABI_TEST_BINARY', 'TOFU_CHECKPOINT_CABI_TEST_LIBRARY',
             'TOFU_CHECKPOINT_CABI_TEST_SOURCE_SHA256', 'TOFU_NATIVE_TERMINAL_TEST_ARCHIVE',
             'TOFU_NATIVE_FLOW_TEST_LIBRARY')
    binary, library, source, archive, flow = [os.environ.get(key) for key in names]
    if sys.platform != 'linux' or not all((binary,library,source,archive,flow)):
        pytest.skip('explicit matching native engine/ROOT/Flow libraries and Linux SIGKILL required')
    directory = tmp_path/'authority'
    directory.mkdir()
    subprocess.run([binary,'init','--data-dir',str(directory)], check=True, capture_output=True)
    original=kill_at_ready('tests.support.native_goal_crash_probe',dict(directory=str(tmp_path),
        library=library,source_sha256=source,actors=actors,phase=phase),directory=tmp_path,name='source')
    assert len(original['pending']) == actors
    files = list(tmp_path.glob('actor-*.txt'))
    assert len(files) == (actors if phase == 'file_published' else 0)
    assert all(path.read_text() == 'once\n' for path in files)
    with EmbeddedRuntime(Path(library), directory, owner_id=2, tenant_id=1, secret=b'x'*32) as runtime:
        assert runtime.build_identity()['source_sha256'] == source
        storage = EmbeddedStorageV2Client(runtime,b'x'*32,owner_id=2,tenant_id=1)
        try:
            provider = lambda **kwargs: storage
            monkeypatch.setattr(service,'storage_runtime',lambda:SimpleNamespace(client=provider))
            monkeypatch.setattr('lib.project_mod.modifications.SESSIONS_DIR',str(tmp_path/'file-sessions'))
            monkeypatch.setattr('lib.relay_config.billing_enabled',lambda:True)
            context=accept_recovery(storage,original,target_id='target-root')
            task,seed,inputs=context.task,context.seed,context.inputs
            runs,goals=context.runs,context.goals
            pending = seed.pending_actors
            assert len(inputs) == len(pending.actors) == actors
            assert pending.account_frontier['snapshot']['tokens'] == actors*5
            for key, observed in pending.actors.items():
                child_id = observed['admission']['invocation_id']
                state = observed['frontier']['snapshot']
                assert state == original['pending'][child_id]
                assert state['shared_model_account']['counter_policy']['wire_model'] == 'alpha-wire-a'
                assert state['tool_recovery']['producer_task_id'] == child_id
                assert inputs[key].input_hash == observed['admission']['input_hash']
            assert '_executionBudget' not in task
            before = get_balance(original['wallet_id'])
            principal = PrincipalContext.user(subject_id=original['wallet_id'],owner_user_id=2)
            claim = claim_pending_goal_actors(task,seed,principal=principal,storage=storage,
                wallet_user_id=original['wallet_id'],reserved_micro=original['held_micro'],
                header_bytes=4096,snapshot_bytes=1<<20,evidence_bytes=8<<20,io_timeout_ms=10000)
            invoice = claim.request['invoice']
            assert invoice['roundCount'] == actors and invoice['componentsMicro']['totalMicro'] == 28*actors
            assert get_balance(original['wallet_id']) == before+original['held_micro']-invoice['componentsMicro']['totalMicro']
            assert '_executionBudget' not in task
            assert len(list(tmp_path.glob('actor-*.txt'))) == len(files)
            assert all(path.read_text()=='once\n' for path in files)
            with file_provider() as (endpoint,bodies):
                control=storage.native_checkpoint_control
                handoff_observations=[]
                def observe_control(operation,payload,**kwargs):
                    response=control(operation,payload,**kwargs)
                    if operation=='goal_handoff':
                        handoff_observations.append(response)
                        # The native store accepts no started effect before takeover.
                        assert all(event['type']!='execution_effect_started'
                            for event in runs.replay(task['_goalRunId'],0).events)
                        if lost_handoff_ack:
                            assert response['accepted'] is True
                            raise OSError('lost acknowledged native handoff response')
                    return response
                monkeypatch.setattr(storage,'native_checkpoint_control',observe_control)
                try:
                    from tests.support.native_goal_resume_probe import resume
                    def execute():
                        return asyncio.run(resume(task=task,seed=seed,inputs=inputs,claim=claim,storage=storage,
                            runs=runs,principal=principal,directory=tmp_path,
                            endpoint=endpoint,archive=archive,flow_library=flow))
                    if lost_handoff_ack:
                        from lib.orchestration.execution_journal import RecoveryRefused
                        with pytest.raises(RecoveryRefused, match='Execution effect did not settle') as failed:
                            execute()
                    else:
                        resumed=execute()
                except BaseException as error:
                    error.add_note('Native handoff observations: '+str(handoff_observations))
                    raise
            if lost_handoff_ack:
                assert not bodies
                assert len(handoff_observations)==1
                events=runs.replay(task['_goalRunId'],0).events
                receipts=[event for event in events if event['type']=='execution_recovery_consumed']
                assert len(receipts)==1
                assert not any(event['type']=='execution_effect_started' for event in events)
                assert task['_executionBudget'].reason()=='execution_account_frontier_unavailable'
                assert get_balance(original['wallet_id'])==before+original['held_micro']-28*actors-100
                assert len(list(tmp_path.glob('actor-*.txt')))==len(files)
                assert all(path.read_text()=='once\n' for path in files)
                repeated=claim_pending_goal_actors(task,seed,principal=principal,storage=storage,
                    wallet_user_id=original['wallet_id'],reserved_micro=original['held_micro'],
                    header_bytes=4096,snapshot_bytes=1<<20,evidence_bytes=8<<20,io_timeout_ms=10000)
                assert repeated.receipt==claim.receipt
                assert runs.replay(task['_goalRunId'],0).events==events
                assert get_balance(original['wallet_id'])==before+original['held_micro']-28*actors-100
                from lib.orchestration.chat_failure import finalize_orchestration_chat_flow_exception
                finalize_orchestration_chat_flow_exception(task,failed.value,label='autopilot-recovery')
                assert get_attempt(task['_attemptId'],user_id=2)['status']=='failed'
                assert goals.latest_for_conversation(task['convId'])['status']=='failed'
                row=storage.query('task_results.replay_get',dict(key=task['id'],user_id=2,include_metadata=True))
                assert row['metadata']['usageComplete'] is False
                assert row['metadata']['nativePricingRequired'] is True
                assert 'nativeBillingSettlement' not in row['metadata']
                from lib.billing.native_settlement import recover
                for _ in range(2):
                    with pytest.raises(ValueError, match='no committed terminal settlement'):
                        recover(wallet_user_id=original['wallet_id'],task_id=task['id'],held_micro=100)
                assert get_balance(original['wallet_id'])==before+original['held_micro']-28*actors-100
                return
            assert len(resumed['restored'])==actors
            fresh_rounds=actors*(2 if phase=='file_not_published' else 1)+1
            assert len(bodies)==fresh_rounds
            assert all(any(row.get('role')=='tool' for row in body['messages']) for body in bodies[:actors])
            files=list(tmp_path.glob('actor-*.txt'))
            assert len(files)==actors and all(path.read_text()=='once\n' for path in files)
            actuals=task['_executionBudget'].continuity_snapshot()
            assert task['_executionBudget'].consumption_snapshot()==(
                (actors+fresh_rounds)*5,actors*(2 if phase=='file_not_published' else 1),True)
            assert not actuals['active_child_task_ids']
            assert len(handoff_observations)==1 and handoff_observations[0]['accepted'] is True
            events=runs.replay(task['_goalRunId'],0).events
            consumed=next(event['seq'] for event in events if event['type']=='execution_recovery_consumed')
            assert all(event['seq']>consumed for event in events if event['type']=='execution_effect_started')
            assert len([event for event in events if event['type']=='execution_effect_completed'])==actors+1
            task['content']=resumed['outcome'].result['final']
            completed=context.goal_service.complete(
                task,resumed['outcome'].terminal_outcome)
            assert completed['status']=='completed'
            from lib.billing.native_settlement import freeze, recover
            from lib.tasks_pkg.manager import persist_task_result
            from lib.tasks_pkg.manager._terminal import stamp_chat_task_terminal
            capsule=freeze(task)
            assert capsule['roundCount']==fresh_rounds
            assert capsule['componentsMicro']['totalMicro']==55*fresh_rounds
            assert stamp_chat_task_terminal(task,status='done',finish_reason='stop')
            assert persist_task_result(task) is not False
            assert get_attempt(task['_attemptId'],user_id=2)['status']=='completed'
            for _ in range(2):
                assert recover(wallet_user_id=original['wallet_id'],task_id=task['id'],held_micro=100)
            assert get_balance(original['wallet_id'])==before+original['held_micro']-28*actors-55*fresh_rounds
        finally:
            storage.close()
