"""Concurrent proposals reserve distinct durable numbers without approval rights."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import threading
import pytest
from lib import evolution
from lib.evolution import records,gate,mutation_versions
from lib.storage.errors import StorageError
from tests.evolution.test_evolution_gate import _settled_episode,USER
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark=pytest.mark.unit

@pytest.fixture
def store(monkeypatch):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)


def test_simultaneous_proposals_receive_distinct_versions(store,monkeypatch):
    eid=_settled_episode(store)
    barrier=threading.Barrier(2);local=threading.local()
    original=mutation_versions._read
    def synchronized(key,owner,kind):
        result=original(key,owner,kind)
        if not getattr(local,'seen',False):
            local.seen=True;barrier.wait(timeout=10)
        return result
    monkeypatch.setattr(mutation_versions,'_read',synchronized)
    # This synthetic record seam must model the real authority's atomic CAS.
    lock=threading.Lock();command=store.command
    def atomic(*args):
        with lock:return command(*args)
    monkeypatch.setattr(store,'command',atomic)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(evolution.propose_mutation,'prompt_variant',{'text':f'candidate-{n}'},[eid],user_id=USER) for n in range(2)]
        results=[future.result(timeout=15) for future in futures]
    assert sorted(row['version'] for row in results)==[1,2]
    assert all(row['status']=='proposed' and row['requires_confirmation'] for row in results)


def test_failed_publication_keeps_reserved_gap(store,monkeypatch):
    eid=_settled_episode(store);original=gate._save
    monkeypatch.setattr(gate,'_save',lambda *args,**kwargs:(_ for _ in ()).throw(StorageError('database_internal','publish refused')))
    with pytest.raises(StorageError):evolution.propose_mutation('prompt_variant',{'text':'first'},[eid],user_id=USER)
    monkeypatch.setattr(gate,'_save',original)
    result=evolution.propose_mutation('prompt_variant',{'text':'next'},[eid],user_id=USER)
    assert result['version']==2


def test_committed_reservation_lost_ack_uses_exact_readback(store,monkeypatch):
    eid=_settled_episode(store);command=store.command;lost=[]
    def lose(operation,payload,command_id):
        result=command(operation,payload,command_id)
        if payload['namespace']==mutation_versions.NAMESPACE and not lost:
            lost.append(True);raise StorageError('database_timeout','reservation ACK lost')
        return result
    monkeypatch.setattr(store,'command',lose)
    result=evolution.propose_mutation('prompt_variant',{'text':'candidate'},[eid],user_id=USER)
    assert result['version']==1 and lost==[True]


def test_unchanged_conflict_refuses_without_busy_retry(store,monkeypatch):
    eid=_settled_episode(store);command=store.command;attempts=[]
    def refuse(operation,payload,command_id):
        if payload['namespace']==mutation_versions.NAMESPACE:
            attempts.append(True);raise StorageError('database_conflict','no observable advancement')
        return command(operation,payload,command_id)
    monkeypatch.setattr(store,'command',refuse)
    with pytest.raises(StorageError):evolution.propose_mutation('prompt_variant',{'text':'candidate'},[eid],user_id=USER)
    assert attempts==[True]


def test_other_kind_has_independent_numbers_and_malformed_head_never_publishes(store):
    eid=_settled_episode(store)
    a=evolution.propose_mutation('prompt_variant',{'text':'a'},[eid],user_id=USER)
    b=evolution.propose_mutation('routing_change',{'model':'b'},[eid],user_id=USER)
    assert a['version']==b['version']==1
    key=records.entity_key('prompt_variant',USER,kind='mutation version kind')
    store.rows[mutation_versions.NAMESPACE,key]['user_id']=USER+1
    before=deepcopy(store.rows)
    with pytest.raises(StorageError):evolution.propose_mutation('prompt_variant',{'text':'c'},[eid],user_id=USER)
    assert store.rows==before


def test_real_selected_store_concurrent_reservations_survive_restart(storage,store,monkeypatch):
    eid=_settled_episode(store);originals=deepcopy(store.rows)
    supervisor,_=storage
    monkeypatch.setattr(records,'_storage',lambda **kwargs:supervisor.client)
    for (namespace,key),value in originals.items():records.put_record(namespace,key,value,kind='fixture')
    barrier=threading.Barrier(2);local=threading.local();original=mutation_versions._read
    def synchronized(key,owner,kind):
        result=original(key,owner,kind)
        if not getattr(local,'seen',False):
            local.seen=True;barrier.wait(timeout=10)
        return result
    with monkeypatch.context() as scoped:
        scoped.setattr(mutation_versions,'_read',synchronized)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(evolution.propose_mutation,'prompt_variant',{'text':f'candidate-{n}'},[eid],user_id=USER) for n in range(2)]
            values=[future.result(timeout=20) for future in futures]
    assert sorted(row['version'] for row in values)==[1,2]
    supervisor.stop();supervisor.start()
    following=evolution.propose_mutation('prompt_variant',{'text':'third'},[eid],user_id=USER)
    assert following['version']==3


def test_lost_ack_followed_by_another_reservation_skips_unproved_gap(store,monkeypatch):
    eid=_settled_episode(store);command=store.command;advanced=[]
    def lose_and_advance(operation,payload,command_id):
        result=command(operation,payload,command_id)
        if payload['namespace']==mutation_versions.NAMESPACE and not advanced:
            advanced.append(True)
            evolution.propose_mutation('prompt_variant',{'text':'other'},[eid],user_id=USER)
            raise StorageError('database_timeout','first reservation acknowledgement lost')
        return result
    monkeypatch.setattr(store,'command',lose_and_advance)
    result=evolution.propose_mutation('prompt_variant',{'text':'original'},[eid],user_id=USER)
    assert result['version']==3
    assert sorted(row['version'] for row in evolution.list_mutations(user_id=USER))==[2,3]


def test_mutation_identifier_collision_cannot_replace_an_existing_proposal(store,monkeypatch):
    from types import SimpleNamespace
    eid=_settled_episode(store)
    first=evolution.propose_mutation('prompt_variant',{'text':'original'},[eid],user_id=USER)
    original=deepcopy(store.rows[records.NAMESPACE_MUTATIONS,f"{USER}:{first['mutation_id']}"])
    monkeypatch.setattr(gate.uuid,'uuid4',lambda:SimpleNamespace(hex=first['mutation_id'].removeprefix('mut-')))
    with pytest.raises(StorageError):
        evolution.propose_mutation('prompt_variant',{'text':'replacement'},[eid],user_id=USER)
    assert store.rows[records.NAMESPACE_MUTATIONS,f"{USER}:{first['mutation_id']}"]==original
    head=store.rows[mutation_versions.NAMESPACE,records.entity_key('prompt_variant',USER,kind='mutation version kind')]
    assert head['allocated_version']==2


def test_mutation_publication_lost_ack_confirms_exact_original(store,monkeypatch):
    eid=_settled_episode(store);command=store.command;lost=[]
    def lose(operation,payload,command_id):
        result=command(operation,payload,command_id)
        if payload['namespace']==records.NAMESPACE_MUTATIONS and not lost:
            lost.append(True);raise StorageError('database_timeout','publication ACK lost')
        return result
    monkeypatch.setattr(store,'command',lose)
    value=evolution.propose_mutation('prompt_variant',{'text':'candidate'},[eid],user_id=USER)
    assert value['version']==1 and gate._load(value['mutation_id'],user_id=USER)==value
