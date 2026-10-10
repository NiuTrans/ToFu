"""Resume admission failures preserve the interrupted action's durable facts."""
from copy import deepcopy
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import pytest

from lib.research.action_continuation import resume_action
from lib.research.action_runtime import _production, _research_action_runtime
from lib.research.model_accounting import INTENT_NAMESPACE
from lib.storage.errors import StorageError
from tests.research.test_research_process_recovery import VersionedRecords, setup
from tests.research.test_research_model_accounting import snapshot

pytestmark=pytest.mark.unit


def test_latest_admission_is_numeric_paged_and_rejects_holes(monkeypatch):
    client=VersionedRecords();repo,_,_,_,calls=setup(client,monkeypatch)
    original=deepcopy(calls.load_model_intent(1))
    for ordinal in range(2,34):
        value=deepcopy(original);value.update(ordinal=ordinal,round_index=ordinal-1)
        value['usage_before']=snapshot(ordinal-1);value['usage_before']['agent_dispatches']=ordinal
        client.rows[INTENT_NAMESPACE,repo.prefix+str(ordinal)]={'version':1,'value':value}
    reads=[];query=client.query
    def measured(operation,payload):
        reads.append((operation,payload.get('limit')));return query(operation,payload)
    client.query=measured
    assert calls.latest_model_ordinal()==33
    assert reads==[('record.list',16)]*3
    client.rows.pop((INTENT_NAMESPACE,repo.prefix+'2'))
    with pytest.raises(StorageError,match='incomplete'):calls.latest_model_ordinal()


@pytest.mark.parametrize('failure',['scheduler','admission'])
def test_failed_resume_has_no_claim_transfer_or_terminal_publication(monkeypatch,failure):
    import lib.research.action_continuation as continuation
    client=VersionedRecords();repo,_,_,_,_=setup(client,monkeypatch)
    monkeypatch.setattr('lib.storage.get_storage_client',lambda **kwargs:client)
    before=deepcopy(client.rows)
    workers=[]
    if failure=='scheduler':
        monkeypatch.setattr(_production,'submit_detached',lambda *args,**kwargs:(_ for _ in ()).throw(RuntimeError('scheduler refused')))
    else:
        monkeypatch.setattr(continuation,'prepare_action_continuation',lambda *args,**kwargs:(_ for _ in ()).throw(StorageError('database_conflict','frontier unavailable')))
        original=_production.submit_detached
        def submit(*args,**kwargs):
            worker=original(*args,**kwargs);workers.append(worker);return worker
        monkeypatch.setattr(_production,'submit_detached',submit)
    with pytest.raises((RuntimeError,StorageError)):resume_action('research_action_observed',user_id=1)
    for worker in workers:worker.join(timeout=5);assert not worker.is_alive()
    assert _research_action_runtime.get_owned('research_action_observed',user_id=1) is None
    assert client.rows==before


def test_duplicate_resume_waits_for_one_worker_admission(monkeypatch):
    import lib.research.action_continuation as continuation
    client=VersionedRecords();_,_,_,definition,_=setup(client,monkeypatch)
    monkeypatch.setattr('lib.storage.get_storage_client',lambda **kwargs:client)
    entered=Event();release=Event(); admissions=[];workers=[]
    def prepare(*args,**kwargs):
        admissions.append(args);entered.set();assert release.wait(5)
        return SimpleNamespace(meter=SimpleNamespace(snapshot=lambda:snapshot()))
    monkeypatch.setattr(continuation,'prepare_action_continuation',prepare)
    monkeypatch.setattr('lib.research.action.run_research_action',lambda task,**kwargs:_research_action_runtime.finish(task['id'],result={}))
    monkeypatch.setattr('lib.research.action_terminal.persist_action_terminal',lambda *args:None)
    # The runtime retains its captured terminal port; this test stops before paid work.
    monkeypatch.setattr(_research_action_runtime,'terminal_persist',None)
    original=_production.submit_detached
    def submit(*args,**kwargs):
        worker=original(*args,**kwargs);workers.append(worker);return worker
    monkeypatch.setattr(_production,'submit_detached',submit)
    try:
        with ThreadPoolExecutor(max_workers=2) as callers:
            first=callers.submit(resume_action,'research_action_observed',user_id=1)
            assert entered.wait(5)
            second=callers.submit(resume_action,'research_action_observed',user_id=1)
            release.set()
            results=[first.result(timeout=10),second.result(timeout=10)]
        assert len(admissions)==1 and len(workers)==1
        assert sorted(result['deduped'] for result in results)==[False,True]
        assert all(result['task_id']=='research_action_observed' for result in results)
    finally:
        release.set()
        for worker in workers:worker.join(timeout=5)
        _research_action_runtime.remove_owned('research_action_observed',user_id=1)
