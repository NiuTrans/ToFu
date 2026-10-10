"""Warm schema proofs cannot replace fresh originals, owner checks or contracts."""
from copy import deepcopy
from types import SimpleNamespace
import hashlib
import pytest
import jsonschema
from lib.evolution import receipt_validation,run_receipts,records
from lib.evolution.contracts import EvolutionStateError
from lib.storage.errors import StorageError
from tests.evolution.test_evolution_run_receipt_archive import long_run
from tests.evolution.test_evolution_runs import runtime as runtime,OWNER

pytestmark=pytest.mark.unit

@pytest.fixture(autouse=True)
def proofs():
    receipt_validation.clear_proofs()
    yield
    receipt_validation.clear_proofs()


def warm(runtime):
    runs,store,run,version=long_run(runtime)
    runs._save(run,version,OWNER)
    assert runs.get_run(run['run_id'],user_id=OWNER)['receipts']==run['receipts']
    header=store.rows[records.NAMESPACE_RUNS,runs._key(run['run_id'],OWNER)]
    pointer=header['receipt_archive']
    key=(run_receipts.NAMESPACE,run_receipts._key(run['run_id'],pointer['sha256'],OWNER))
    return runs,store,run,header,key


@pytest.mark.parametrize('value',[-1,True])
@pytest.mark.parametrize('resealed',[False,True])
def test_modified_original_cannot_reuse_a_warm_success(runtime,value,resealed):
    runs,store,run,header,key=warm(runtime)
    page=deepcopy(store.rows[key]);page['receipts'][0]['actuals']['tokens']=value
    if resealed:
        sha=hashlib.sha256(run_receipts._json(page)).hexdigest()
        key=(run_receipts.NAMESPACE,run_receipts._key(run['run_id'],sha,OWNER))
        header['receipt_archive']['sha256']=sha
    store.rows[key]=page
    with pytest.raises(EvolutionStateError):runs.get_run(run['run_id'],user_id=OWNER)


def test_warm_proof_never_hides_an_original_read_failure(runtime,monkeypatch):
    runs,store,run,header,key=warm(runtime)
    def refused(*args,**kwargs):raise StorageError('database_unavailable','original unavailable')
    monkeypatch.setattr(records,'get_record',refused)
    with pytest.raises(StorageError,match='original unavailable'):
        runs.get_run(run['run_id'],user_id=OWNER)


def test_owner_binding_is_checked_even_on_a_schema_proof_hit(runtime,monkeypatch):
    runs,store,run,header,key=warm(runtime);page=deepcopy(store.rows[key])
    before=receipt_validation.proof_stats()['hits']
    monkeypatch.setattr(records,'get_record',lambda *args,**kwargs:deepcopy(page))
    with pytest.raises(EvolutionStateError,match='identity changed'):
        run_receipts.expand(deepcopy(header),user_id=OWNER+1)
    assert receipt_validation.proof_stats()['hits']>before


def test_new_compiled_contract_cannot_reuse_old_proofs(runtime,monkeypatch):
    runs,store,run,header,key=warm(runtime);original=run_receipts.contract_validator
    refused=jsonschema.Draft202012Validator({'not':{}})
    monkeypatch.setattr(run_receipts,'contract_validator',lambda name,definition=None:
        refused if definition=='receipt_archive_page' else original(name,definition))
    with pytest.raises(EvolutionStateError,match='contract changed'):
        runs.get_run(run['run_id'],user_id=OWNER)


def test_eviction_retains_no_pages_and_revalidates_evicted_material(monkeypatch):
    monkeypatch.setattr(receipt_validation,'_capacity',lambda:2)
    validator=jsonschema.Draft202012Validator({'type':'object','required':['index']})
    def validate(index):
        page={'index':index};digest=hashlib.sha256(run_receipts._json(page)).digest()
        receipt_validation.validate_page(page,digest,validator)
    for index in range(10):validate(index)
    assert receipt_validation.proof_stats()['entries']==2
    misses=receipt_validation.proof_stats()['misses'];validate(0)
    assert receipt_validation.proof_stats()['misses']==misses+1
    assert receipt_validation.proof_stats()['entries']==2


@pytest.mark.parametrize('capacity,available,expected',[(32,16,16),(128,64,64),(128,0,1),(None,None,1),(128,None,1)])
def test_capacity_tracks_launch_memory_and_falls_back_lean(monkeypatch,capacity,available,expected):
    monkeypatch.setattr('runtime_guards.probe_system_resources',lambda:SimpleNamespace(
        effective_memory_capacity_mb=capacity,effective_memory_available_mb=available))
    assert receipt_validation.proof_stats()['capacity']==expected


def test_success_proof_does_not_retain_the_original_page(runtime):
    import gc
    import weakref
    runs,store,run,header,key=warm(runtime)
    class Page(dict):pass
    page=Page(deepcopy(store.rows[key]));reference=weakref.ref(page)
    receipt_validation.validate_page(page,hashlib.sha256(run_receipts._json(page)).digest(),
        run_receipts.contract_validator(run_receipts.SCHEMA,'receipt_archive_page'))
    del page;gc.collect()
    assert reference() is None
