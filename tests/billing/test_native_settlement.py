"""Owned terminal Rust fees survive a fresh reader and lost wallet ACK."""
import time
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from lib.billing.native_settlement import freeze, decode, recover
from tests.billing.test_native_task_cost import task as quoted_task
pytest_plugins = ('tests._billing_user_sidecar',)
pytestmark = pytest.mark.unit


def owner_task(owner, wallet_id, key):
    task=quoted_task()
    task.update(id=key,_userId=owner,status='done',model='alpha',config={},
        _nativePricingRequired=True,_billingUserId=wallet_id,_billing_reservation_micro=100,
        content='answer',thinking='',toolRounds=[],segments=[],created_at=time.time())
    return task


def committed(capsule, *, owner, task_id, native=True):
    from lib.storage import get_storage_client
    get_storage_client(write=True).command('record.put',dict(namespace='task_results',key=task_id,
        value=dict(task_id=task_id,user_id=owner,conv_id='',status='done',
            created_at=time.time(),metadata=dict(nativePricingRequired=native,
                nativeBillingSettlement=capsule))), 'native-result-'+task_id)


@pytest.mark.parametrize('lose_ack',[False,True])
def test_resumed_invoice_recovery_charges_current_task_without_recharging_paid_prefix(monkeypatch,lose_ack):
    import copy
    from lib.billing import users,deposit,get_balance,reserve
    from lib.billing.request_flow import settle_task
    from lib.billing import wallet, wallet_janitor
    from lib.billing.wallet import _stable_id
    from lib.storage import get_storage_client
    user=users.create_user(uuid.uuid4().hex+'@native-resume.example')
    key=uuid.uuid4().hex
    deposit(user.id,30000,ref_id='seed-'+key)
    first=owner_task(user.owner_user_id,user.id,'source-'+key)
    first['apiRounds'][0]['taskId']=first['id']
    reserve(user.id,100,ref_id=first['id'],note='first owned hold')
    monkeypatch.setattr('lib.relay_config.billing_enabled',lambda:True)
    monkeypatch.setattr('lib.billing.compute_request_cost',lambda *a,**k:pytest.fail('must not reprice'))
    capsule=freeze(first)
    committed(capsule,owner=user.owner_user_id,task_id=first['id'])
    settle_task(first,user_id=user.id,model='alpha',raise_on_error=True)
    target=owner_task(user.owner_user_id,user.id,'target-'+key)
    old=copy.deepcopy(first['apiRounds'][0])
    current=target['apiRounds'][0];current.update(round=2,taskId=target['id'])
    target['apiRounds']=[old,current]
    target['_checkpointApiRounds']=[copy.deepcopy(old)]
    target['_checkpointUsage']={'prompt_tokens':10000,'completion_tokens':200}
    target['_executionBudget']=SimpleNamespace(consumption_snapshot=lambda:(20400,0,True))
    client=get_storage_client(write=True)
    client.command('billing.wallet.apply',dict(user_id=user.id,amount_micro=-100,
        kind='reserve',ref_type='reserve',ref_id=target['id'],note='resumed owned hold',
        allow_negative=False,ledger_id=_stable_id('led_',user.id,'reserve',target['id']),
        occurred_at=int(time.time())-3600),'resume-reserve-'+key)
    capsule=freeze(target)
    assert capsule['roundCount']==1 and capsule['componentsMicro']['totalMicro']==7593
    committed(capsule,owner=user.owner_user_id,task_id=target['id'])
    target.clear() # The recovery port has only canonical metadata and wallet evidence.
    original=wallet.settle
    def lost(*args,**kwargs):
        original(*args,**kwargs);raise OSError('wallet committed ACK lost')
    if lose_ack:
        with patch('lib.billing.settle',side_effect=lost):
            assert wallet_janitor.sweep_stale_reserves(ttl_seconds=60)['errors']==1
        assert get_balance(user.id)==14814
    assert wallet_janitor.sweep_stale_reserves(ttl_seconds=60)['errors']==0
    assert get_balance(user.id)==14814


@pytest.mark.parametrize('lose_ack',[False,True])
def test_metadata_commit_recovers_exact_charge_after_lost_ack(monkeypatch,lose_ack):
    from lib.billing import users, deposit, get_balance, list_entries
    from lib.billing.wallet import _stable_id
    from lib.storage import get_storage_client
    from lib.tasks_pkg.manager._persist import build_result_meta
    from lib.billing import wallet_janitor, wallet
    user=users.create_user(uuid.uuid4().hex+'@native.example')
    key='native-'+uuid.uuid4().hex
    deposit(user.id,10000,ref_id='seed-'+key)
    get_storage_client(write=True).command('billing.wallet.apply',dict(user_id=user.id,
        amount_micro=-100,kind='reserve',ref_type='reserve',ref_id=key,note='original hold',
        allow_negative=False,ledger_id=_stable_id('led_',user.id,'reserve',key),
        occurred_at=int(time.time())-3600),'native-reserve-'+key)
    task=owner_task(user.owner_user_id,user.id,key)
    capsule=build_result_meta(task)['nativeBillingSettlement']
    assert capsule['componentsMicro']['totalMicro']==7593
    committed(capsule,owner=user.owner_user_id,task_id=key)
    task.clear()  # Recovery has no live task, account, usage or price resolver.
    original=wallet.settle
    def lost_ack(*args,**kwargs):
        original(*args,**kwargs)
        raise OSError('committed wallet acknowledgement lost')
    monkeypatch.setattr('lib.relay_config.billing_enabled',lambda:True)
    monkeypatch.setattr('lib.billing.compute_request_cost',lambda *a,**k:pytest.fail('must not reprice'))
    if lose_ack:
        with patch('lib.billing.settle',side_effect=lost_ack):
            summary=wallet_janitor.sweep_stale_reserves(ttl_seconds=60)
        assert summary['errors']==1
    else:
        assert wallet_janitor.sweep_stale_reserves(ttl_seconds=60)['settled_native']==1
    assert get_balance(user.id)==2407
    client=get_storage_client()
    command=[sys.executable]
    proc=Path('/proc/self/cmdline')
    if proc.exists():
        argv=[part.decode() for part in proc.read_bytes().split(b'\0') if part]
        for index,arg in enumerate(argv):
            if arg==sys.executable:
                command=argv[:index+1];break
    child = subprocess.run(command+['-c',"""
import json,os,sys
payload=json.load(sys.stdin)
from lib.storage.client import StorageClient
client=StorageClient(payload['host'],payload['port'],payload['token'])
import lib.storage,lib.billing.wallet,lib.billing.users,lib.billing.ledger,lib.relay_config,lib.billing
for module in (lib.storage,lib.billing.wallet,lib.billing.users,lib.billing.ledger):
    module.get_storage_client=lambda **kwargs:client
lib.relay_config.billing_enabled=lambda:True
def no_repricing(*args,**kwargs):raise AssertionError('fresh reader must not reprice')
lib.billing.compute_request_cost=no_repricing
from lib.billing.native_settlement import recover
accepted=recover(wallet_user_id=payload['wallet'],task_id=payload['task'],held_micro=100)
print(json.dumps({'accepted':accepted,'pid':os.getpid()}))
"""],input=json.dumps(dict(host=client.endpoint[0],port=client.endpoint[1],
        token=client._token,wallet=user.id,task=key)),text=True,capture_output=True,
        cwd=Path(__file__).resolve().parents[2],timeout=30,check=True)
    observation=json.loads(child.stdout.strip().splitlines()[-1])
    assert observation['accepted'] is True and observation['pid']!=os.getpid()
    assert get_balance(user.id)==2407
    assert wallet_janitor.sweep_stale_reserves(ttl_seconds=60)['candidates']==0
    rows=[row for row in list_entries(user.id) if row.ref_id==key]
    assert sorted((row.kind,row.amount_micro) for row in rows)==[
        ('debit',-7593),('reserve',-100),('reserve_release',100)]


@pytest.mark.parametrize('fault',['owner','wallet','sum','bool','float','overflow','extra'])
def test_capsule_refuses_changed_identity_or_amount(fault):
    capsule=freeze(owner_task(7,'wallet','task'))
    if fault=='owner':capsule['owner_user_id']=8
    elif fault=='wallet':capsule['wallet_user_id']='foreign'
    elif fault=='sum':capsule['componentsMicro']['totalMicro']+=1
    elif fault=='bool':capsule['reserved_micro']=True
    elif fault=='float':capsule['componentsMicro']['marginMicro']=843.0
    elif fault=='overflow':capsule['componentsMicro']['totalMicro']=9223372036854775808
    else:capsule['unexpected']=True
    with pytest.raises(ValueError):decode(capsule,task_id='task',owner_user_id=7,wallet_user_id='wallet')


def test_missing_native_terminal_capsule_never_refunds_paid_debt(monkeypatch):
    from lib.billing import users,deposit,get_balance
    from lib.billing.wallet import _stable_id
    from lib.billing.wallet_janitor import sweep_stale_reserves
    from lib.storage import get_storage_client
    user=users.create_user(uuid.uuid4().hex+'@missing-native.example')
    key='missing-'+uuid.uuid4().hex
    deposit(user.id,1000,ref_id='seed-'+key)
    get_storage_client(write=True).command('billing.wallet.apply',dict(user_id=user.id,
        amount_micro=-100,kind='reserve',ref_type='reserve',ref_id=key,note='original hold',
        allow_negative=False,ledger_id=_stable_id('led_',user.id,'reserve',key),
        occurred_at=int(time.time())-3600),'native-reserve-'+key)
    committed(None,owner=user.owner_user_id,task_id=key)
    monkeypatch.setattr('lib.relay_config.billing_enabled',lambda:True)
    summary=sweep_stale_reserves(ttl_seconds=60)
    assert summary['errors']==1 and summary['reclaimed']==summary['settled_native']==0
    assert get_balance(user.id)==900


def test_running_task_guard_resolves_opaque_wallet_subject_to_owner():
    from lib.billing.wallet_janitor import _is_task_still_running
    with patch('lib.billing.users.get_user',return_value=SimpleNamespace(owner_user_id=7)), \
         patch('lib.tasks_pkg.manager.runtime.chat_task_runtime.get_owned',return_value={'status':'running'}) as owned:
        assert _is_task_still_running('task',user_id='usr_opaque') is True
    owned.assert_called_once_with('task',user_id=7)



def test_reordered_durable_capsule_keeps_exact_wallet_idempotency_candidate(monkeypatch):
    from lib.billing.request_flow import settle_task
    monkeypatch.setattr('lib.relay_config.billing_enabled', lambda: True)
    original = owner_task(7, 'opaque-wallet', 'stable-task')
    capsule = freeze(original)
    reordered = json.loads(json.dumps(capsule, sort_keys=True))
    recovered = dict(id=original['id'], _userId=7, _billing_reservation_micro=100,
        _nativeBillingSettlement=reordered)
    with patch('lib.billing.settle', return_value=SimpleNamespace(balance_micro=2407)) as settle:
        first = settle_task(original, user_id='opaque-wallet', model='alpha', raise_on_error=True)
        second = settle_task(recovered, user_id='opaque-wallet', model='other-caller-alias', raise_on_error=True)
    assert first['cost_micro'] == second['cost_micro'] == 7593
    assert settle.call_args_list[0] == settle.call_args_list[1]


@pytest.mark.parametrize('fault', ['none', 'pending', 'missing', 'passive', 'owner', 'wallet', 'reserve'])
def test_retired_native_invoice_requires_owned_durable_proof_before_wallet_recovery(monkeypatch, fault):
    task = owner_task(7, 'opaque-wallet', 'source')
    capsule = freeze(task)
    if fault == 'owner': capsule['owner_user_id'] = 8
    if fault == 'wallet': capsule['wallet_user_id'] = 'foreign-wallet'
    if fault == 'reserve': capsule['reserved_micro'] += 1
    state = dict(phase='tools_pending' if fault == 'pending' else 'terminal',
        wallet_settlement=None if fault == 'missing' else capsule)
    calls = []
    def query(operation, payload, **options):
        assert operation == 'task_results.replay_get' and payload['user_id'] == 7
        return None  # Source retirement is stronger than an absent result projection.
    def invoice(operation, payload, **options):
        assert operation == 'invoice' and payload['invoice']['owner_user_id'] == options['user_id'] == 7
        assert payload['invoice']['task_id'] == 'source'
        return dict(accepted=fault != 'passive', state=state, error='ineligible' if fault == 'passive' else None)
    client = SimpleNamespace(query=query, native_checkpoint_available=lambda **kw: True,
        native_checkpoint_control=invoice)
    monkeypatch.setattr('lib.storage.get_storage_client', lambda: client)
    monkeypatch.setattr('lib.billing.users.get_user', lambda key: SimpleNamespace(owner_user_id=7))
    monkeypatch.setattr('lib.billing.request_flow.settle_task', lambda task, **kw: calls.append((task,kw)) or {'settled': True})
    if fault == 'none':
        assert recover(wallet_user_id='opaque-wallet', task_id='source', held_micro=100)
        assert len(calls) == 1 and calls[0][0]['_nativeBillingSettlement'] == capsule
        assert calls[0][1]['raise_on_error'] is True
    else:
        with pytest.raises(ValueError): recover(wallet_user_id='opaque-wallet', task_id='source', held_micro=100)
        assert calls == []
