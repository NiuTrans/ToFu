"""Receipt archival preserves complete authority across bounded header writes."""
from copy import deepcopy
import pytest
from lib.evolution import run_receipts,records
from lib.evolution.contracts import EvolutionStateError
from lib.storage.errors import StorageError
from tests.evolution.test_evolution_runs import runtime as runtime,OWNER

pytestmark=pytest.mark.unit


def long_run(runtime):
    runs,store,_=runtime
    run=runs.submit_run({'objective':'Preserve every repair receipt'},user_id=OWNER)
    _,version=runs._read(run['run_id'],OWNER)
    run['receipts']=[{'task_id':f'old-stage-{i}','stage':'executing',
        'actuals':{'tokens':i,'tool_calls':i%3,'wall_clock_seconds':1}} for i in range(129)]
    run['actuals']={key:sum(row['actuals'][key] for row in run['receipts']) for key in run['actuals']}
    return runs,store,run,version


def test_run_header_is_bounded_and_public_history_remains_exact(runtime):
    runs,store,run,version=long_run(runtime);original=deepcopy(run['receipts'])
    runs._save(run,version,OWNER)
    stored=records.get_record(records.NAMESPACE_RUNS,runs._key(run['run_id'],OWNER))
    assert len(stored['receipts'])<=64 and stored['receipt_archive']['count']==96
    loaded,version=runs._read(run['run_id'],OWNER)
    assert loaded['receipts']==original and loaded['actuals']==run['actuals']
    assert runs.list_runs(user_id=OWNER)[0]['receipts']==original
    page_versions={key:value for key,value in store.versions.items() if key[0]==run_receipts.NAMESPACE}
    runs._save(loaded,version,OWNER)
    assert {key:value for key,value in store.versions.items() if key[0]==run_receipts.NAMESPACE}==page_versions


@pytest.mark.parametrize('damage',['missing','body','owner','prefix'])
def test_missing_or_foreign_archive_refuses_before_recovery(runtime,damage):
    runs,store,run,version=long_run(runtime);runs._save(run,version,OWNER)
    key=next(key for key in store.rows if key[0]==run_receipts.NAMESPACE)
    if damage=='missing':store.rows.pop(key)
    elif damage=='body':store.rows[key]['receipts'][0]['actuals']['tokens']+=1
    elif damage=='owner':store.rows[key]['user_id']=OWNER+1
    else:
        header=store.rows[(records.NAMESPACE_RUNS,runs._key(run['run_id'],OWNER))]
        header['receipt_archive']['prefix_sha256']='f'*64
    with pytest.raises(EvolutionStateError):runs.get_run(run['run_id'],user_id=OWNER)


def test_archived_receipts_cannot_change_on_resave(runtime):
    runs,_,run,version=long_run(runtime);runs._save(run,version,OWNER)
    loaded,version=runs._read(run['run_id'],OWNER)
    loaded['receipts'][0]['actuals']['tokens']+=1
    with pytest.raises(EvolutionStateError,match='archived run receipts changed'):runs._save(loaded,version,OWNER)


def test_archive_ack_failure_and_stale_cas_cannot_publish_candidates(runtime,monkeypatch):
    runs,_,run,version=long_run(runtime)
    before=records.get_record(records.NAMESPACE_RUNS,runs._key(run['run_id'],OWNER))
    with monkeypatch.context() as scoped:
        scoped.setattr(run_receipts,'_put_immutable',lambda *args,**kwargs:(_ for _ in ()).throw(RuntimeError('lost archive ACK')))
        with pytest.raises(RuntimeError):runs._save(run,version,OWNER)
    assert records.get_record(records.NAMESPACE_RUNS,runs._key(run['run_id'],OWNER))==before
    with pytest.raises(StorageError):runs._save(run,version+100,OWNER)
    assert records.get_record(records.NAMESPACE_RUNS,runs._key(run['run_id'],OWNER))==before
    assert 'receipt_archive' not in run
    runs._save(run,version,OWNER)
    assert runs.get_run(run['run_id'],user_id=OWNER)['receipts']==run['receipts']


def test_archival_preserves_canonical_custom_run_id_contract(runtime):
    _,_,run,_=long_run(runtime);run['run_id']='legacy-custom.run_01'
    stored=run_receipts.compact(run,user_id=OWNER)
    assert run_receipts.expand(stored,user_id=OWNER)['receipts']==run['receipts']


def test_invalid_header_refuses_before_archive_publication(runtime):
    from lib.evolution.contracts import EvolutionContractError
    runs,store,run,version=long_run(runtime);run['status']='invented-status'
    with pytest.raises(EvolutionContractError):runs._save(run,version,OWNER)
    assert not any(key[0]==run_receipts.NAMESPACE for key in store.rows)


def test_history_larger_than_single_record_limit_still_commits(runtime):
    import json
    runs,_,run,version=long_run(runtime)
    template=run['receipts'][0]
    run['receipts']=[{**deepcopy(template),'task_id':f'archived-large-stage-{i}'} for i in range(9000)]
    run['actuals']={key:sum(row['actuals'][key] for row in run['receipts']) for key in run['actuals']}
    assert len(json.dumps(run).encode())>records.MAX_RECORD_BYTES
    runs._save(run,version,OWNER)
    stored=records.get_record(records.NAMESPACE_RUNS,runs._key(run['run_id'],OWNER))
    assert len(stored['receipts'])<=64
    assert len(json.dumps(stored).encode())<records.MAX_RECORD_BYTES
    assert runs.get_run(run['run_id'],user_id=OWNER)['receipts']==run['receipts']


def test_intermediate_prefix_commitment_must_match_complete_original(runtime):
    import hashlib
    runs,store,run,version=long_run(runtime);runs._save(run,version,OWNER)
    header=store.rows[(records.NAMESPACE_RUNS,runs._key(run['run_id'],OWNER))]
    pointer=header['receipt_archive']
    key=(run_receipts.NAMESPACE,run_receipts._key(run['run_id'],pointer['sha256'],OWNER))
    page=deepcopy(store.rows[key]);page['previous']['prefix_sha256']='f'*64
    # Preserve actual rows, total count, outer digest and full prefix digest.
    # The earlier prefix commitment is still inconsistent and must refuse.
    sha=hashlib.sha256(run_receipts._json(page)).hexdigest()
    store.rows[(run_receipts.NAMESPACE,run_receipts._key(run['run_id'],sha,OWNER))]=page
    pointer['sha256']=sha
    with pytest.raises(EvolutionStateError,match='prefix changed'):
        runs.get_run(run['run_id'],user_id=OWNER)


def test_declared_huge_count_cannot_allocate_untrusted_history(runtime):
    runs,store,run,version=long_run(runtime);runs._save(run,version,OWNER)
    header=store.rows[(records.NAMESPACE_RUNS,runs._key(run['run_id'],OWNER))]
    header['receipt_archive']['count']=10**30
    with pytest.raises(EvolutionStateError,match='identity changed'):
        runs.get_run(run['run_id'],user_id=OWNER)
