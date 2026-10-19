"""Hold after the real atomic handoff commit, before its acknowledgement escapes."""
import asyncio
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace


def run(values):
    import pytest
    from lib.billing import get_balance
    from lib.storage import service
    from lib.storage.v2_embedded_client import EmbeddedStorageV2Client
    from tests.support.native_goal_restart import accept_recovery, claim_recovery, file_provider
    from tests.support.native_goal_resume_probe import resume
    from tofu_db.embedded import EmbeddedRuntime

    directory = Path(values['directory'])
    source = values['source']
    with EmbeddedRuntime(Path(values['library']), directory/'authority', owner_id=2,
                         tenant_id=1, secret=b'x'*32) as runtime, pytest.MonkeyPatch.context() as patch:
        assert runtime.build_identity()['source_sha256'] == values['source_sha256']
        storage = EmbeddedStorageV2Client(runtime, b'x'*32, owner_id=2, tenant_id=1)
        patch.setattr(service, 'storage_runtime', lambda: SimpleNamespace(client=lambda **kwargs: storage))
        patch.setattr('lib.project_mod.modifications.SESSIONS_DIR', str(directory/'file-sessions'))
        patch.setattr('lib.relay_config.billing_enabled', lambda: True)
        try:
            context = accept_recovery(storage, source, target_id=values.get('target_id', 'handoff-root'))
            initial_balance = get_balance(source['wallet_id'])
            principal, claim = claim_recovery(context, storage, source)
            control = storage.native_checkpoint_control
            def hold_after_handoff(operation, payload, **kwargs):
                response = control(operation, payload, **kwargs)
                if operation == 'goal_handoff':
                    assert response['accepted'] is True
                    task = context.task
                    assert not bodies
                    receipt = dict(run_id=task['_goalRunId'], task_id=task['id'],
                        conversation_id=task['convId'], attempt_id=task['_attemptId'],
                        wallet_id=task['_billingUserId'], held_micro=task['_billing_reservation_micro'],
                        source_claim_request=claim.request, source_claim_receipt=claim.receipt,
                        handoff_request=payload, handoff_response=response,
                        initial_balance=initial_balance, balance=get_balance(source['wallet_id']),
                        provider_calls=len(bodies))
                    ready = Path(values['ready']); staging = ready.with_suffix('.staging')
                    staging.write_text(json.dumps(receipt)+'\n'); staging.replace(ready)
                    # The parent owns SIGKILL of this entire process group.
                    threading.Event().wait(90)
                    raise AssertionError('Parent did not kill the committed-handoff writer')
                return response
            patch.setattr(storage, 'native_checkpoint_control', hold_after_handoff)
            with file_provider() as (endpoint, bodies):
                asyncio.run(resume(task=context.task, seed=context.seed, inputs=context.inputs,
                    claim=claim, storage=storage, runs=context.runs, principal=principal,
                    directory=directory, endpoint=endpoint, archive=values['archive'],
                    flow_library=values['flow']))
        finally:
            storage.close()
    raise AssertionError('Goal returned before the committed handoff crash boundary')


if __name__ == '__main__':
    run(json.loads(Path(sys.argv[1]).read_text()))
