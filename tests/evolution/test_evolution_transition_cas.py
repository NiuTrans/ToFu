"""Concurrent gate decisions preserve the authority's accepted decision."""
import pytest
from lib import evolution
from lib.evolution import gate, records
from lib.storage.errors import StorageError
from tests.evolution.test_evolution_gate import _settled_episode, USER

pytestmark = pytest.mark.unit

@pytest.fixture
def store(monkeypatch):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)

@pytest.mark.parametrize('outer,inner', [('confirmed','rejected'),('rejected','confirmed')])
def test_competing_decision_cannot_overwrite_accepted_decision(store,monkeypatch,outer,inner):
    episode=_settled_episode(store)
    mutation=evolution.propose_mutation('prompt_variant',{'text':'candidate'},[episode],user_id=USER)
    original=gate.put_record
    inserted=False
    def interleave(namespace,key,value,**kwargs):
        nonlocal inserted
        if namespace==records.NAMESPACE_MUTATIONS and value['status']==outer and not inserted:
            inserted=True
            gate._transition(mutation['mutation_id'],inner,user_id=USER)
        return original(namespace,key,value,**kwargs)
    monkeypatch.setattr(gate,'put_record',interleave)
    with pytest.raises(StorageError,match='version conflict'):
        gate._transition(mutation['mutation_id'],outer,user_id=USER)
    assert gate._load(mutation['mutation_id'],user_id=USER)['status']==inner

@pytest.mark.parametrize('bad_ack',[False,True])
def test_exact_committed_decision_survives_lost_or_bad_ack(store,monkeypatch,bad_ack):
    episode=_settled_episode(store)
    mutation=evolution.propose_mutation('prompt_variant',{'text':'candidate'},[episode],user_id=USER)
    original=gate.put_record
    def lose_ack(*args,**kwargs):
        version=original(*args,**kwargs)
        if bad_ack:return version+1
        raise StorageError('database_timeout','ACK lost')
    monkeypatch.setattr(gate,'put_record',lose_ack)
    assert evolution.confirm_mutation(mutation['mutation_id'],user_id=USER)['status']=='confirmed'
