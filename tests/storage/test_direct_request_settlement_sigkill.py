"""Abrupt process loss preserves protected requests and exact frozen invoices."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.api

WRITER = r'''
import json,signal,sys,time
from pathlib import Path
from types import SimpleNamespace
from tofu_db.embedded import EmbeddedRuntime
from lib.storage import service
from lib.storage.v2_embedded_client import EmbeddedStorageV2Client
from lib.billing import users,deposit,get_balance,compute_request_cost
from lib.billing.request_settlement import RequestSettlement
from lib.llm.request import seal_request
library,directory,ready,source,phase=sys.argv[1:]
native=EmbeddedRuntime(Path(library),Path(directory),owner_id=2,tenant_id=1,secret=b'x'*32)
assert native.build_identity()['source_sha256']==source
client=EmbeddedStorageV2Client(native,b'x'*32,owner_id=2,tenant_id=1)
service.storage_runtime=lambda:SimpleNamespace(client=lambda **kwargs:client)
user=users.create_user('request-crash@example.test');assert user.owner_user_id==2
deposit(user.id,10000,ref_id='seed')
client.command('billing.wallet.apply',dict(user_id=user.id,amount_micro=-100,kind='reserve',
 ref_type='reserve',ref_id='request',note='',allow_negative=False,ledger_id='original-hold',
 occurred_at=int(time.time())-3600),'original-hold')
owner=RequestSettlement(request_id='request',owner=2,wallet=user.id,held=100)
owner.protect()
lease=owner(input_tokens=100,max_output_tokens=100)
lease.prepare_input(seal_request({'model':'gpt-4o-mini','messages':[]},model='gpt-4o-mini',
 input_tokens=100,semantic_id='synthetic'))
lease.mark_dispatched()
usage={'prompt_tokens':1000,'completion_tokens':10}
expected=compute_request_cost('gpt-4o-mini',raw_usage=usage).micro
if phase=='invoice':
 lease.settle(usage)
 lease.confirm_input(successful=True)
 import lib.billing
 def fail_before_wallet(*a,**kw):raise OSError('stop after durable invoice')
 lib.billing.settle=fail_before_wallet
 try:owner.settle()
 except OSError:pass
 else:raise AssertionError('wallet fault missing')
Path(ready).write_text(json.dumps(dict(wallet=user.id,balance=get_balance(user.id),expected=expected)))
while True:signal.pause()
'''


@pytest.mark.parametrize('phase', ['sent_unknown', 'invoice'])
@pytest.mark.parametrize('lost_wallet_ack', [False, True])
def test_sigkill_recovers_only_acknowledged_request_invoice(tmp_path, monkeypatch, phase, lost_wallet_ack):
    from tofu_db.embedded import EmbeddedRuntime
    from lib.storage.v2_embedded_client import EmbeddedStorageV2Client
    from lib.storage import service
    from lib.billing import get_balance, wallet_janitor, settle
    binary = os.environ.get('TOFUDB_TEST_BINARY')
    library = os.environ.get('TOFUDB_TEST_LIBRARY')
    source = os.environ.get('TOFUDB_TEST_SOURCE')
    if not all((binary, library, source)) or sys.platform != 'linux':
        pytest.skip('private source-matched native engine and Linux required')
    directory = tmp_path / 'authority'
    directory.mkdir()
    subprocess.run([binary, 'init', '--data-dir', str(directory)], check=True, capture_output=True)
    ready, errors = tmp_path / 'ready.json', tmp_path / 'writer.stderr'
    with errors.open('wb') as stderr:
        writer = subprocess.Popen([sys.executable, '-c', WRITER, library,
            str(directory), str(ready), source, phase], stdout=subprocess.DEVNULL, stderr=stderr)
        try:
            expires = time.monotonic() + 30
            while not ready.exists():
                assert writer.poll() is None, errors.read_text()
                assert time.monotonic() < expires, errors.read_text()
                time.sleep(.02)
            writer.kill()
            assert writer.wait(10) == -signal.SIGKILL
        finally:
            if writer.poll() is None:
                writer.kill()
                writer.wait(10)
    receipt = json.loads(ready.read_text())
    assert receipt['balance'] == 9900
    with EmbeddedRuntime(Path(library), directory, owner_id=2, tenant_id=1, secret=b'x'*32) as native:
        assert native.build_identity()['source_sha256'] == source
        client = EmbeddedStorageV2Client(native, b'x'*32, owner_id=2, tenant_id=1)
        monkeypatch.setattr(service, 'storage_runtime', lambda: SimpleNamespace(client=lambda **kwargs: client))
        monkeypatch.setattr('lib.billing.compute_request_cost', lambda *a, **k: pytest.fail('cold repricing'))
        try:
            def lose_ack(*args, **kwargs):
                settle(*args, **kwargs)
                raise OSError('wallet committed ACK lost')
            with monkeypatch.context() as fault:
                if lost_wallet_ack:
                    fault.setattr('lib.billing.settle', lose_ack)
                first = wallet_janitor.sweep_stale_reserves(ttl_seconds=60)
            second = wallet_janitor.sweep_stale_reserves(ttl_seconds=60)
            assert first['reclaimed'] == second['reclaimed'] == 0
            if phase == 'sent_unknown':
                assert first['errors'] == second['errors'] == 1
                assert get_balance(receipt['wallet']) == 9900
            else:
                assert first['errors'] == int(lost_wallet_ack) and second['errors'] == 0
                assert get_balance(receipt['wallet']) == 10000 - receipt['expected']
        finally:
            client.close()
