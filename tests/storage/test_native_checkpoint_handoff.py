"""Real native ROOT hands a completed checkpoint to an accepted fresh executor."""
import copy
import logging
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytestmark=pytest.mark.api


@pytest.mark.parametrize('billing,lost_wallet_ack',[(False,False),(True,False),(True,True)],
    ids=['unbilled','wallet','lost-wallet-ack'])
def test_priced_native_checkpoint_handoff_follows_real_accepted_turn(tmp_path,monkeypatch,billing,lost_wallet_ack):
    binary=os.environ.get('TOFU_CHECKPOINT_CABI_TEST_BINARY')
    library=os.environ.get('TOFU_CHECKPOINT_CABI_TEST_LIBRARY')
    expected=os.environ.get('TOFU_CHECKPOINT_CABI_TEST_SOURCE_SHA256')
    if not binary or not library or not expected:
        pytest.skip('explicit private native checkpoint engine pair required')
    from tests.agent_core.test_native_fee_checkpoint_installed import (
        test_installed_priced_checkpoint_preserves_fees_before_next_http as run_fixture)
    from lib.identity import PrincipalContext
    from lib.storage import service
    from lib.storage.v2_embedded_client import EmbeddedStorageV2Client
    from lib.tasks_pkg.native_checkpoint import NativeCheckpointPolicy
    from lib.tasks_pkg.native_terminal import NativeTaskTerminalPolicy,project_native_terminal
    from lib.turn_lifecycle import (create_turn_pair,create_attempt,claim_attempt_start,
        bind_task,mark_task_started,get_turn,get_attempt)
    from tofu_db.embedded import EmbeddedRuntime
    directory=tmp_path/'authority';directory.mkdir()
    subprocess.run([binary,'init','--data-dir',str(directory)],check=True,capture_output=True)
    owner=2 if billing else 7
    principal=PrincipalContext.user(subject_id='native-task-test',owner_user_id=owner)
    conversation=uuid4().hex; task_ids=('source-'+uuid4().hex,'target-'+uuid4().hex)
    pair=None; receipts=[]; source_task=None
    lost=[]
    runtime=EmbeddedRuntime(Path(library),directory,owner_id=owner,tenant_id=3,secret=b'x'*32)
    assert runtime.build_identity()['source_sha256']==expected
    client=EmbeddedStorageV2Client(runtime,b'x'*32,owner_id=owner,tenant_id=3)
    monkeypatch.setattr(service,'storage_runtime',lambda:SimpleNamespace(client=lambda **kwargs:client))
    if billing:
        from lib.billing import users,deposit,reserve,get_balance
        from lib.billing.request_flow import settle_task
        wallet=users.create_user(uuid4().hex+'@native-handoff.example')
        assert wallet.owner_user_id==owner
        principal=PrincipalContext.user(subject_id=wallet.id,owner_user_id=owner)
        deposit(wallet.id,10000,ref_id=uuid4().hex)
        monkeypatch.setattr('lib.relay_config.billing_enabled',lambda:True)
        monkeypatch.setattr('lib.billing.compute_request_cost',lambda *a,**kw:pytest.fail('native handoff must not reprice'))
        if lost_wallet_ack:
            import lib.billing as wallet_api
            original_settle=wallet_api.settle
            def lose_ack(*args,**kwargs):
                result=original_settle(*args,**kwargs)
                if kwargs.get('ref_id')==task_ids[1] and not lost:
                    lost.append(task_ids[1]);raise OSError('lost wallet settlement acknowledgement')
                return result
            monkeypatch.setattr(wallet_api,'settle',lose_ack)
    def factory(task,executor,iteration):
        nonlocal pair,source_task
        if iteration==0:
            pair=create_turn_pair(conversation,command_id=uuid4().hex,user_id=owner,
                input_projection={'content':'Resume paid native work'},config={},
                conversation_defaults={'allowCreate':True,'title':'Native handoff','settings':{}})
            source_task=task
        else:
            current=get_turn(conversation,pair['turn']['turnId'],user_id=owner)
            assert current['status']=='interrupted'
            assert any(option['operation']=='continue' for option in current['settlement']['resumeOptions']), current['settlement']
            pair=create_attempt(conversation,pair['turn']['turnId'],operation='continue',
                command_id=uuid4().hex,expected_projection_revision=current['projectionRevision'],user_id=owner,
                config={'_resumeSource':{'taskId':'forged'}})
            # Same accepted carry shape used by ordinary public resume. Exact
            # quote/usage equality is checked again against the native journal.
            for target,source in (('apiRounds','apiRounds'),('_checkpointApiRounds','apiRounds'),
                    ('usage','usage'),('_checkpointUsage','usage'),('cost','cost')):
                task[target]=copy.deepcopy(source_task[source])
        attempt=pair['attempt']['attemptId'];turn=pair['turn']['turnId']
        assert claim_attempt_start(attempt,user_id=owner)
        assert bind_task(attempt,task['id'],user_id=owner)['taskId']==task['id']
        assert mark_task_started(attempt,task['id'],user_id=owner)['status']=='running'
        if billing:
            amount=31 if iteration==0 else 61
            reserve(wallet.id,amount,ref_id=task['id'])
            task.update(_billingUserId=wallet.id,_billing_reservation_micro=amount)
        task.update(convId=conversation,_turnId=turn,_attemptId=attempt,model='alpha',
            created_at=1.,content='',thinking='',toolRounds=[],segments=[])
        async def restore(request,state):
            receipts.append({'target_task':state['task_id'],
                'account_root':state['shared_model_account']['root_task_id'],
                'fee_owner':state['host_fee_projection']['task_id'],
                'quote_origin':state['host_fee_projection']['rounds'][0]['task_id']})
        class ObservedCheckpoint(NativeCheckpointPolicy):
            async def load(self,request):
                try:return await super().load(request)
                except Exception:
                    logging.getLogger(__name__).exception('native handoff acquisition failed')
                    raise
            async def close(self,*,terminal_saved=False):
                await super().close(terminal_saved=terminal_saved)
                if billing and terminal_saved:
                    if lost_wallet_ack and iteration==1:
                        with pytest.raises(OSError,match='lost wallet'):
                            settle_task(task,user_id=wallet.id,model='alpha',raise_on_error=True)
                    assert settle_task(task,user_id=wallet.id,model='alpha',raise_on_error=True)
                    balance=get_balance(wallet.id)
                    assert settle_task(task,user_id=wallet.id,model='alpha',raise_on_error=True)
                    assert get_balance(wallet.id)==balance
        checkpoint=ObservedCheckpoint(principal=principal,task=task,storage=client,executor=executor,
            epoch='epoch',binding='fees-binding',token=uuid4().hex,header_bytes=4096,snapshot_bytes=65536,
            identity_bytes=1024,io_timeout_ms=5000,restore=restore,suspend=iteration==0,
            handoff_from_accepted=iteration==1)
        terminal=NativeTaskTerminalPolicy(principal=principal,task=task,executor=executor,
            operation_id=f'commit-{iteration}',project=project_native_terminal,candidate_bytes=65536)
        return checkpoint,terminal
    try:
        run_fixture(tmp_path,'resume',checkpoint_factory=factory,task_ids=task_ids,project_lookup=True,principal_context=principal)
        assert receipts==[{'target_task':task_ids[1],'account_root':task_ids[1],
            'fee_owner':task_ids[1],'quote_origin':task_ids[0]}]
        assert get_turn(conversation,pair['turn']['turnId'],user_id=owner)['status']=='completed'
        assert get_attempt(pair['attempt']['attemptId'],user_id=owner)['taskId']==task_ids[1]
        original=client.query('task_results.replay_get',{'key':task_ids[0],'user_id':owner,'include_metadata':True})
        assert original['status']=='aborted' and original['metadata']['apiRounds'][0]['nativeCredits']['total']==31
        if billing:assert get_balance(wallet.id)==9908
        assert bool(lost)==lost_wallet_ack
    finally:client.close()
