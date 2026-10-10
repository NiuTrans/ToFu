"""Canonical gateway evidence: complete batches, ownership, budgets and reset."""
import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from lib.agent_core.native_process import ProgressLimits, ProgressRequest
from lib.agent_core.progress_evidence import visible_result_evidence, nonretryable_failure_signature
from lib.identity import PrincipalContext
from lib.tasks_pkg.native_progress import NativeGatewayProgress
from lib.tools.result_envelope import ToolResultEnvelopeV2, ToolResultErrorV2

pytestmark = pytest.mark.unit


def principal(owner=7):
    return PrincipalContext.user(subject_id='progress-test', owner_user_id=owner)


def policy(budget=4096):
    gateway = SimpleNamespace(_closed=False, owner=7, task_id='task',
                              task=dict(id='task', _userId=7, status='running', _worldVersion='v1'))
    return NativeGatewayProgress(gateway, ProgressLimits(2, 2), budget)


def request(round_num=0, calls=1, owner=7):
    return ProgressRequest(principal(owner), 'task', round_num, calls, threading.Event())


def call(value=1):
    return {'id':'response-id', 'function': {'name':'read', 'arguments': '{"value":%d}' % value}}


def failure(retryable=False):
    return ToolResultEnvelopeV2(status='error', summary='blocked', error=ToolResultErrorV2(
        code='permission', retryable=retryable, next_action='ask_owner', message='blocked')).to_model_text()


def test_complete_result_batch_is_detached_and_consumed_once():
    p=policy();p.record(0,call(),'visible')
    observation=asyncio.run(p.observe(request()))
    assert observation==dict(worldVersion='v1', evidenceIds=[visible_result_evidence('visible')],
                            resultEvidenceComplete=True, verification='', nonretryableFailureSignatures=[])
    assert p.batch is None
    observation['evidenceIds'].append('mutation')
    assert not asyncio.run(p.observe(request()))['resultEvidenceComplete']


@pytest.mark.parametrize('kind',['missing','extra','different_round','cancelled','expired'])
def test_missing_or_mismatched_evidence_never_claims_progress_or_terminal_failure(kind):
    p=policy();p.record(0,call(),failure());r=request(calls=2 if kind=='missing' else 1)
    if kind=='extra':p.record(0,call(),failure())
    if kind=='different_round':r=request(round_num=1)
    if kind=='cancelled':r.cancelled.set()
    if kind=='expired':r=ProgressRequest(principal(),'task',0,1,threading.Event(),time.monotonic_ns()-1)
    result=asyncio.run(p.observe(r))
    assert not result['resultEvidenceComplete']
    assert result['evidenceIds']==result['nonretryableFailureSignatures']==[]


def test_only_all_nonretryable_failures_supply_operation_signatures():
    p=policy();result=failure();p.record(0,call(),result);p.record(0,call(2),result)
    observed=asyncio.run(p.observe(request(calls=2)))
    assert observed['nonretryableFailureSignatures']==sorted({nonretryable_failure_signature(call(),result),nonretryable_failure_signature(call(2),result)})
    p.record(1,call(),result);p.record(1,call(2),'ok')
    assert asyncio.run(p.observe(request(round_num=1,calls=2)))['nonretryableFailureSignatures']==[]
    p.record(2,call(),failure(retryable=True))
    assert asyncio.run(p.observe(request(round_num=2)))['nonretryableFailureSignatures']==[]


def test_evidence_overflow_is_bounded_and_resets_at_next_round():
    p=policy(64)
    for index in range(1000):p.record(0,call(),str(index))
    assert p.batch.bytes<=64 and p.batch.evidence==p.batch.failures==()
    assert not asyncio.run(p.observe(request(calls=1000)))['resultEvidenceComplete']
    p=policy(128);p.record(0,call(),'visible')
    p.gateway.task['_worldVersion']='w'*129
    assert not asyncio.run(p.observe(request()))['resultEvidenceComplete']
    p.gateway.task['_worldVersion']='v2';p.record(1,call(),'new')
    assert asyncio.run(p.observe(request(round_num=1)))['resultEvidenceComplete']


@pytest.mark.parametrize('change',['owner','task','closed','terminal'])
def test_owner_and_lifecycle_changes_refuse_observation(change):
    p=policy();p.record(0,call(),'visible');r=request()
    if change=='owner':r=request(owner=8)
    if change=='task':p.gateway.task['id']='other'
    if change=='closed':p.gateway._closed=True
    if change=='terminal':p.gateway.task['status']='done'
    with pytest.raises(ValueError):asyncio.run(p.observe(r))


def test_failure_signature_normalizes_arguments_and_ignores_response_ids():
    result=failure();a=call();b=call();b['id']='new';b['function']['arguments']=' { "value" : 1 } '
    assert nonretryable_failure_signature(a,result)==nonretryable_failure_signature(b,result)
    assert nonretryable_failure_signature(call(2),result)!=nonretryable_failure_signature(a,result)
    assert nonretryable_failure_signature(a,'tool failed') is None
