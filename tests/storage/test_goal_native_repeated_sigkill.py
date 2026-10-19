"""Two actual native Goal process deaths retain one file effect and each payer."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from tests.support.native_goal_restart import accept_recovery, claim_recovery, file_provider, kill_at_ready

pytestmark=pytest.mark.api


@pytest.mark.parametrize('actors,phase', [(1,'file_not_published'),(1,'file_published'),
                                        (2,'file_not_published'),(2,'file_published')])
def test_goal_recovers_a_second_genuine_checkpoint_without_repeating_files_or_payers(tmp_path,monkeypatch,actors,phase):
    from lib.billing import get_balance
    from lib.billing.native_settlement import freeze,recover
    from lib.storage import service
    from lib.storage.v2_embedded_client import EmbeddedStorageV2Client
    from lib.tasks_pkg.manager import persist_task_result
    from lib.tasks_pkg.manager._terminal import stamp_chat_task_terminal
    from lib.turn_lifecycle import get_attempt
    from tests.support.native_goal_resume_probe import resume
    from tofu_db.embedded import EmbeddedRuntime

    names=('TOFU_CHECKPOINT_CABI_TEST_BINARY','TOFU_CHECKPOINT_CABI_TEST_LIBRARY',
        'TOFU_CHECKPOINT_CABI_TEST_SOURCE_SHA256','TOFU_NATIVE_TERMINAL_TEST_ARCHIVE','TOFU_NATIVE_FLOW_TEST_LIBRARY')
    binary,library,source_hash,archive,flow=[os.environ.get(name) for name in names]
    if sys.platform!='linux' or not all((binary,library,source_hash,archive,flow)):
        pytest.skip('explicit native engine/ROOT/Flow and Linux SIGKILL required')
    directory=tmp_path/'authority';directory.mkdir()
    subprocess.run([binary,'init','--data-dir',str(directory)],check=True,capture_output=True)
    values=dict(directory=str(tmp_path),library=library,source_sha256=source_hash,actors=actors)
    first=kill_at_ready('tests.support.native_goal_crash_probe',dict(values,phase='file_not_published'),
        directory=tmp_path,name='first')
    assert len(first['pending'])==actors and not list(tmp_path.glob('actor-*.txt'))
    second=kill_at_ready('tests.support.native_goal_restart_probe',dict(values,phase=phase,source=first,
        archive=archive,flow=flow),directory=tmp_path,name='second')
    assert second['task_id']!=first['task_id']
    assert second['provider_calls']==actors
    assert second['source_invoice']['roundCount']==actors
    assert second['source_invoice']['componentsMicro']['totalMicro']==28*actors
    assert second['balance']==second['initial_balance']+first['held_micro']-28*actors-second['held_micro']
    files=list(tmp_path.glob('actor-*.txt'))
    assert len(files)==(actors if phase=='file_published' else 0)
    assert all(path.read_text()=='once\n' for path in files)
    assert len(second['pending'])==actors
    with EmbeddedRuntime(Path(library),directory,owner_id=2,tenant_id=1,secret=b'x'*32) as runtime:
        assert runtime.build_identity()['source_sha256']==source_hash
        storage=EmbeddedStorageV2Client(runtime,b'x'*32,owner_id=2,tenant_id=1)
        monkeypatch.setattr(service,'storage_runtime',lambda:SimpleNamespace(client=lambda **kwargs:storage))
        monkeypatch.setattr('lib.project_mod.modifications.SESSIONS_DIR',str(tmp_path/'file-sessions'))
        monkeypatch.setattr('lib.relay_config.billing_enabled',lambda:True)
        try:
            context=accept_recovery(storage,second,target_id='final-root')
            seed,task=context.seed,context.task
            assert len(seed.pending_actors.actors)==actors
            for row in seed.pending_actors.actors.values():
                child=row['admission']['invocation_id']
                state=row['frontier']['snapshot']
                assert state==second['pending'][child]
                assert state['tool_recovery']['producer_task_id']==child
                assert state['shared_model_account']['root_task_id']==second['task_id']
                quotes=state['host_fee_projection']['rounds']
                assert {quote['billable_task_id'] for quote in quotes}=={first['task_id'],second['task_id']}
            principal,claim=claim_recovery(context,storage,second)
            assert claim.request['invoice']['roundCount']==actors
            assert claim.request['invoice']['componentsMicro']['totalMicro']==55*actors
            assert get_balance(first['wallet_id'])==second['initial_balance']+first['held_micro']-83*actors
            control = storage.native_checkpoint_control
            handoffs = []
            def observe_handoff(operation, payload, **kwargs):
                response = control(operation, payload, **kwargs)
                if operation == 'goal_handoff':
                    handoffs.append(response)
                    (tmp_path/'final-handoff.json').write_text(json.dumps(dict(
                        request=payload, response=response))+'\n')
                return response
            monkeypatch.setattr(storage, 'native_checkpoint_control', observe_handoff)
            with file_provider() as (endpoint,bodies):
                try:
                    result=asyncio.run(resume(task=task,seed=seed,inputs=context.inputs,claim=claim,
                        storage=storage,runs=context.runs,principal=principal,directory=tmp_path,
                        endpoint=endpoint,archive=archive,flow_library=flow))
                except BaseException as error:
                    error.add_note('Second native handoff observations: '+str(handoffs))
                    raise
            fresh=actors*(2 if phase=='file_not_published' else 1)+1
            assert len(bodies)==fresh and len(result['restored'])==actors
            files=list(tmp_path.glob('actor-*.txt'))
            assert len(files)==actors and all(path.read_text()=='once\n' for path in files)
            assert task['_executionBudget'].consumption_snapshot()==(
                (2*actors+fresh)*5,actors*(3 if phase=='file_not_published' else 2),True)
            assert not task['_executionBudget'].continuity_snapshot()['active_child_task_ids']
            task['content']=result['outcome'].result['final']
            assert context.goal_service.complete(task,result['outcome'].terminal_outcome)['status']=='completed'
            invoice=freeze(task)
            assert invoice['roundCount']==fresh and invoice['componentsMicro']['totalMicro']==55*fresh
            assert stamp_chat_task_terminal(task,status='done',finish_reason='stop')
            assert persist_task_result(task) is not False
            assert get_attempt(task['_attemptId'],user_id=2)['status']=='completed'
            # Earlier roots settle through atomic Goal claims; the final root
            # settles from its committed terminal capsule. Retry each owner.
            from tofu_db.generated_checkpoint_control import FORMAT, RESPONSE_HEADROOM_BYTES
            for _ in range(2):
                for request, receipt in ((second['source_claim_request'], second['source_claim_receipt']),
                                         (claim.request, claim.receipt)):
                    repeated = storage.native_checkpoint_control('goal_claim', dict(format=FORMAT,
                        goal_claim=dict(owner_user_id=2, header_bytes=4096, snapshot_bytes=1<<20,
                            evidence_bytes=8<<20, request=request)), user_id=2,
                        deadline_unix_ms=time.time_ns()//1000000+10000, response_bytes=(8<<20)+RESPONSE_HEADROOM_BYTES)
                    assert repeated['accepted'] is True and repeated['state'] == receipt
                assert recover(wallet_user_id=first['wallet_id'], task_id=task['id'], held_micro=100)
            assert get_balance(first['wallet_id'])==second['initial_balance']+first['held_micro']-83*actors-55*fresh
            for run_id in (second['run_id'],task['_goalRunId']):
                events=context.runs.replay(run_id,0).events
                consumed=[event for event in events if event['type']=='execution_recovery_consumed']
                assert len(consumed)==1
                assert all(event['seq']>consumed[0]['seq'] for event in events if event['type']=='execution_effect_started')
        finally:
            storage.close()
