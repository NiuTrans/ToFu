"""Optional indexed cache bounds never clip originals or defeat invalidation."""
from copy import deepcopy
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from lib.tasks_pkg import request_inspector as ri
from lib.tasks_pkg.snapshot_delta import SnapshotProjector

pytestmark=pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch):
    ri._EVENTS_CACHE.clear();monkeypatch.setattr(ri,'_task_may_append',lambda _:False)
    yield
    ri._EVENTS_CACHE.clear()


def events(count=3, text='Exact original'):
    projector=SnapshotProjector();messages=[];rows=[]
    for number in range(1,count+1):
        messages=messages+[{'role':'user','content':text+str(number)}]
        payload=projector.project('controlled',{'type':'messages_snapshot','kind':'request','roundNum':number,'messages':messages,'tools':[]})
        rows.append({'event_id':number,'type':'messages_snapshot','payload':payload,'ts_ms':number})
    return rows


def source(monkeypatch, rows):
    reads=[]
    def read(tid,*,rebuild):
        assert not rebuild;reads.append(tid);return deepcopy(rows),True
    monkeypatch.setattr(ri,'_read_events_uncached',read)
    return reads


def test_round_and_final_wire_reads_materialize_only_selected_snapshots(monkeypatch):
    source(monkeypatch,events(20));counts=[]
    from lib.tasks_pkg.snapshot_delta import SnapshotReplayIndex
    original=SnapshotReplayIndex.payload
    def observed(index,position):counts.append(position);return original(index,position)
    monkeypatch.setattr(SnapshotReplayIndex,'payload',observed)
    payload=ri.get_request_payload('controlled',20,fold=True)
    assert len(payload['messages'])==20 and counts==[18,19]
    counts.clear();rows,ok=ri._read_snapshot_events('controlled',last=True)
    assert ok and counts==[19] and len(rows)==1


def test_oversized_original_is_readable_but_not_retained(monkeypatch):
    monkeypatch.setattr(ri,'_cache_budget_bytes',lambda:4096)
    reads=source(monkeypatch,events(2,'🧪'*5000))
    for _ in range(2):
        payload=ri.get_request_payload('large',2)
        assert payload['messages'][-1]['content']=='🧪'*5000+'2'
    assert len(reads)==2 and not ri._EVENTS_CACHE


def test_aggregate_byte_budget_is_enforced_without_truncating_evicted_sources(monkeypatch):
    budget=16000;monkeypatch.setattr(ri,'_cache_budget_bytes',lambda:budget)
    reads=source(monkeypatch,events(2,'x'*1000))
    for number in range(12):
        result=ri.get_request_payload(f'task-{number}',2)
        assert result['messages'][-1]['content']=='x'*1000+'2'
        assert sum(entry[4] for entry in ri._EVENTS_CACHE.values())<=budget
    before=len(reads);assert ri.get_request_payload('task-0',2)['messages']
    assert len(reads)>before


def test_invalidation_during_raw_read_prevents_old_rows_reentering_cache(monkeypatch):
    reads=[]
    def read(tid,*,rebuild):
        reads.append(tid)
        if len(reads)==1:ri.invalidate_task_cache(tid)
        return events(1,'old' if len(reads)==1 else 'new'),True
    monkeypatch.setattr(ri,'_read_events_uncached',read)
    assert ri.get_request_payload('live',1)['messages'][0]['content']=='old1'
    assert not ri._EVENTS_CACHE
    assert ri.get_request_payload('live',1)['messages'][0]['content']=='new1'
    assert len(reads)==2


def test_invalidation_during_index_build_refuses_stale_index_admission(monkeypatch):
    reads=source(monkeypatch,events(2));from lib.tasks_pkg.snapshot_delta import SnapshotReplayIndex
    original=SnapshotReplayIndex.__init__
    def build(index,rows):original(index,rows);ri.invalidate_task_cache('controlled')
    monkeypatch.setattr(SnapshotReplayIndex,'__init__',build)
    assert ri.get_request_payload('controlled',2)['messages']
    assert not ri._EVENTS_CACHE
    assert ri.get_request_payload('controlled',2)['messages'] and len(reads)==2


def test_concurrent_reads_and_invalidation_keep_one_bounded_cache_authority(monkeypatch):
    monkeypatch.setattr(ri,'_cache_budget_bytes',lambda:12000);source(monkeypatch,events(4,'body'*20))
    def worker(position):
        tid=f'controlled-{position%4}'
        if position%3==0:ri.invalidate_task_cache(tid)
        payload=ri.get_request_payload(tid,4)
        assert len(payload['messages'])==4
    with ThreadPoolExecutor(max_workers=6) as workers:list(workers.map(worker,range(48)))
    assert len(ri._EVENTS_CACHE)<=ri._EVENTS_CACHE_MAX
    assert sum(entry[4] for entry in ri._EVENTS_CACHE.values())<=12000
    assert all(key[1]!='reading' for key in ri._EVENTS_CACHE)


def test_index_integrity_failure_is_retryable_read_failure_not_expired(monkeypatch):
    rows=events(1);rows[0]['payload']['prefixLen']='not-an-integer';source(monkeypatch,rows)
    with pytest.raises(ri.RequestInspectorReadError):ri.get_request_payload('corrupt',1)
    assert ('corrupt',True) not in ri._EVENTS_CACHE


@pytest.mark.parametrize('capacity,available,expected', [(2048,512,8),(8192,4096,32),(262144,131072,1024),(262144,1024,16)])
def test_cache_default_scales_with_both_capacity_and_available_memory(capacity,available,expected):
    import runtime_guards as guards
    from tests.deploy.test_deployment_configuration import _resource_snapshot
    snapshot=_resource_snapshot(capacity_mb=capacity,available_mb=available)
    assert guards.deployment_resource_default('TOFU_TASK_INSPECTOR_CACHE_MAX_MIB',{},snapshot=snapshot)==expected


def test_explicit_cache_budget_and_probe_failure_are_finite():
    import runtime_guards as guards
    from tests.deploy.test_deployment_configuration import _resource_snapshot
    from dataclasses import replace
    unknown=replace(_resource_snapshot(),effective_memory_capacity_mb=None,effective_memory_available_mb=None)
    assert guards.deployment_resource_default('TOFU_TASK_INSPECTOR_CACHE_MAX_MIB',{},snapshot=unknown)==16
    assert guards.resolve_resource_budget('TOFU_TASK_INSPECTOR_CACHE_MAX_MIB',{'TOFU_TASK_INSPECTOR_CACHE_MAX_MIB':'12'},minimum=4,maximum=65536)==12
    assert guards.resolve_resource_budget('TOFU_TASK_INSPECTOR_CACHE_MAX_MIB',{'TOFU_TASK_INSPECTOR_CACHE_MAX_MIB':'999999'},minimum=4,maximum=65536)==65536
