"""SIGKILL cannot turn an acknowledged native root hold into a legacy refund."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.api

WRITER = r'''
import json,signal,sys,time
from pathlib import Path
from types import SimpleNamespace
from tofu_db.embedded import EmbeddedRuntime
from lib.storage import service
from lib.storage.v2_embedded_client import EmbeddedStorageV2Client
from lib.billing import users,deposit,get_balance
from lib.billing.native_admission import protect_before_dispatch,is_protected
from lib.agent_core.execution_budget import _ensure_execution_budget,inherit_execution_budget
library,directory,ready,source=sys.argv[1:]
native=EmbeddedRuntime(Path(library),Path(directory),owner_id=2,tenant_id=1,secret=b'x'*32)
assert native.build_identity()['source_sha256']==source
client=EmbeddedStorageV2Client(native,b'x'*32,owner_id=2,tenant_id=1)
service.storage_runtime=lambda:SimpleNamespace(client=lambda **kwargs:client)
user=users.create_user('native-crash@example.test');assert user.owner_user_id==2
deposit(user.id,10000,ref_id='seed')
client.command('billing.wallet.apply',dict(user_id=user.id,amount_micro=-100,kind='reserve',
 ref_type='reserve',ref_id='root',note='',allow_negative=False,ledger_id='reserve-root',
 occurred_at=int(time.time())-3600),'reserve-root')
root=dict(id='root',_userId=2,status='running',config={},_nativePricingRequired=True,
 _billingUserId=user.id,_billing_reservation_micro=100)
budget=_ensure_execution_budget(root)
child=dict(id='child',_userId=2,_parentTaskId='root',status='running',config={})
inherit_execution_budget(root,child)
protect_before_dispatch(child,budget=budget,owner=2)
assert is_protected(client,wallet_user_id=user.id,task_id='root')
assert client.query('task_results.replay_get',dict(key='root',user_id=2)) is None
path=Path(ready);temporary=path.with_suffix('.pending')
temporary.write_text(json.dumps(dict(wallet_id=user.id,balance=get_balance(user.id))))
temporary.replace(path)
while True:signal.pause()
'''


@pytest.mark.parametrize('lost_settlement_ack', [False, True])
def test_cold_janitor_preserves_hold_and_later_original_invoice_settles_once(tmp_path, monkeypatch, lost_settlement_ack):
    from types import SimpleNamespace
    from tofu_db.embedded import EmbeddedRuntime
    from lib.storage.v2_embedded_client import EmbeddedStorageV2Client
    from lib.storage import service
    from lib.billing import get_balance, wallet_janitor
    from lib.billing.native_settlement import recover, freeze
    from tests.billing.test_native_settlement import owner_task
    from lib.task_replay.task_result_checkpoint_contract import TASK_RESULT_CHECKPOINT_GUARD_CONTRACT
    binary=os.environ.get('TOFU_CHECKPOINT_CABI_TEST_BINARY')
    library=os.environ.get('TOFU_CHECKPOINT_CABI_TEST_LIBRARY')
    source=os.environ.get('TOFU_CHECKPOINT_CABI_TEST_SOURCE_SHA256')
    if not all((binary,library,source)) or sys.platform!='linux':
        pytest.skip('private native engine and Linux SIGKILL required')
    directory=tmp_path/'authority';directory.mkdir()
    subprocess.run([binary,'init','--data-dir',str(directory)],check=True,capture_output=True)
    ready=tmp_path/'ready.json';errors=tmp_path/'writer.stderr'
    with errors.open('wb') as stderr:
        writer=subprocess.Popen([sys.executable,'-c',WRITER,library,str(directory),str(ready),source],
            stdout=subprocess.DEVNULL,stderr=stderr)
        try:
            expires=time.monotonic()+30
            while not ready.exists():
                assert writer.poll() is None,errors.read_text()
                assert time.monotonic()<expires,errors.read_text()
                time.sleep(.02)
            writer.kill();assert writer.wait(10)==-signal.SIGKILL
        finally:
            if writer.poll() is None:writer.kill();writer.wait(10)
    receipt=json.loads(ready.read_text());assert receipt['balance']==9900
    with EmbeddedRuntime(Path(library),directory,owner_id=2,tenant_id=1,secret=b'x'*32) as native:
        assert native.build_identity()['source_sha256']==source
        client=EmbeddedStorageV2Client(native,b'x'*32,owner_id=2,tenant_id=1)
        monkeypatch.setattr(service,'storage_runtime',lambda:SimpleNamespace(client=lambda **kwargs:client))
        monkeypatch.setattr('lib.relay_config.billing_enabled',lambda:True)
        monkeypatch.setattr('lib.billing.compute_request_cost',lambda *a,**kw:pytest.fail('must not reprice'))
        try:
            summary=wallet_janitor.sweep_stale_reserves(ttl_seconds=60)
            assert summary['errors']==1 and summary['reclaimed']==0
            assert get_balance(receipt['wallet_id'])==9900
            # A later owned terminal producer supplies the original frozen fee.
            # The admission marker itself never invents this invoice or usage.
            task=owner_task(2,receipt['wallet_id'],'root');task['apiRounds'][0]['taskId']='root'
            capsule=freeze(task)
            client.command('task_results.checkpoint',dict(key='root',expected_version=0,
                guard_contract=TASK_RESULT_CHECKPOINT_GUARD_CONTRACT,require_parent=False,
                value=dict(task_id='root',user_id=2,conv_id='',status='done',created_at=1,
                    metadata=dict(nativePricingRequired=True,nativeBillingSettlement=capsule))),'terminal-invoice')
            if lost_settlement_ack:
                from lib.billing import settle
                def lose(*args,**kwargs):
                    settle(*args,**kwargs)
                    raise OSError('settlement ACK lost')
                with monkeypatch.context() as fault:
                    fault.setattr('lib.billing.settle',lose)
                    with pytest.raises(OSError):recover(wallet_user_id=receipt['wallet_id'],task_id='root',held_micro=100)
            assert recover(wallet_user_id=receipt['wallet_id'],task_id='root',held_micro=100)
            assert get_balance(receipt['wallet_id'])==10000-capsule['componentsMicro']['totalMicro']
            assert wallet_janitor.sweep_stale_reserves(ttl_seconds=60)['candidates']==0
        finally:client.close()
