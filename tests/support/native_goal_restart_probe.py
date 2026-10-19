"""Resume a genuinely killed Goal, then wait for a second owned process kill."""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace


def run(values):
    import pytest
    from lib.billing import get_balance
    from lib.storage import service
    from lib.storage.v2_embedded_client import EmbeddedStorageV2Client
    from lib.tasks_pkg.native_checkpoint import NativeCheckpointPolicy
    from tests.support.native_goal_restart import accept_recovery, claim_recovery, file_provider
    from tests.support.native_goal_resume_probe import resume
    from tofu_db.embedded import EmbeddedRuntime

    directory=Path(values['directory'])
    source=values['source']
    snapshots,waiting={},set()
    with EmbeddedRuntime(Path(values['library']),directory/'authority',owner_id=2,
                         tenant_id=1,secret=b'x'*32) as runtime, pytest.MonkeyPatch.context() as patch:
        assert runtime.build_identity()['source_sha256']==values['source_sha256']
        storage=EmbeddedStorageV2Client(runtime,b'x'*32,owner_id=2,tenant_id=1)
        patch.setattr(service,'storage_runtime',lambda:SimpleNamespace(client=lambda **kwargs:storage))
        patch.setattr('lib.project_mod.modifications.SESSIONS_DIR',str(directory/'file-sessions'))
        patch.setattr('lib.relay_config.billing_enabled',lambda:True)
        try:
            context=accept_recovery(storage,source,target_id='intermediate-root')
            initial_balance=get_balance(source['wallet_id'])
            principal,claim=claim_recovery(context,storage,source)
            pending,complete=NativeCheckpointPolicy.pending,NativeCheckpointPolicy.complete
            async def hold(policy):
                waiting.add(policy.task['id'])
                if len(waiting)==values['actors']:
                    assert waiting==set(snapshots)
                    task=context.task
                    receipt=dict(run_id=task['_goalRunId'],task_id=task['id'],
                        conversation_id=task['convId'],attempt_id=task['_attemptId'],
                        wallet_id=task['_billingUserId'],held_micro=task['_billing_reservation_micro'],
                        pending=snapshots,phase=values['phase'],
                        account=task['_executionBudget'].continuity_snapshot(),
                        source_invoice=claim.request['invoice'],source_claim_request=claim.request,
                        source_claim_receipt=claim.receipt,initial_balance=initial_balance,
                        balance=get_balance(source['wallet_id']),provider_calls=len(bodies))
                    ready=Path(values['ready']);staging=ready.with_suffix('.staging')
                    staging.write_text(json.dumps(receipt)+'\n');staging.replace(ready)
                await asyncio.Future()
            async def after_pending(policy,request):
                await pending(policy,request)
                assert policy.task['_parentTaskId']==context.task['id']
                assert policy.task['id'] not in snapshots
                snapshots[policy.task['id']]=json.loads(json.dumps(dict(request.state,
                    tool_recovery=policy.pending_metadata)))
                if values['phase']=='file_not_published':
                    await hold(policy)
            async def before_complete(policy,request):
                if values['phase']=='file_published' and policy.task['id'] in snapshots:
                    await hold(policy)
                return await complete(policy,request)
            patch.setattr(NativeCheckpointPolicy,'pending',after_pending)
            patch.setattr(NativeCheckpointPolicy,'complete',before_complete)
            with file_provider() as (endpoint,bodies):
                asyncio.run(resume(task=context.task,seed=context.seed,inputs=context.inputs,
                    claim=claim,storage=storage,runs=context.runs,principal=principal,
                    directory=directory,endpoint=endpoint,archive=values['archive'],flow_library=values['flow']))
        finally:
            storage.close()
    raise AssertionError('Resumed Goal returned before the requested second crash boundary')


if __name__=='__main__':
    run(json.loads(Path(sys.argv[1]).read_text()))
