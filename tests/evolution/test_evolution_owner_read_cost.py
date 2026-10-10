"""Workspace ownership reads never hydrate unrelated archived stage evidence."""
import pytest
from lib.evolution import coordination,records,run_receipts
from lib.evolution.contracts import EvolutionStateError
from tests.evolution.test_evolution_runs import runtime as runtime,OWNER
from tests.evolution.test_evolution_run_receipt_archive import long_run

pytestmark=pytest.mark.unit


def test_snapshot_reads_only_owner_header_and_workspace(runtime,monkeypatch):
    runs,store,run,version=long_run(runtime);runs._save(run,version,OWNER)
    before=dict(store.versions);queries=[];read=records._read_with_retry
    def observed(operation,payload,**kwargs):
        queries.append((operation,payload['namespace']))
        return read(operation,payload,**kwargs)
    monkeypatch.setattr(records,'_read_with_retry',observed)
    assert coordination.snapshot(run['run_id'],user_id=OWNER)['revision']==0
    assert queries==[('record.get',records.NAMESPACE_RUNS),('record.get',records.NAMESPACE_LIBRARIES)]
    assert store.versions==before


def test_missing_history_does_not_authorize_task_through_workspace_read(runtime):
    runs,store,run,version=long_run(runtime);runs._save(run,version,OWNER)
    key=next(key for key in store.rows if key[0]==run_receipts.NAMESPACE);store.rows.pop(key)
    # Independent workspace inspection establishes ownership, not stage authority.
    assert coordination.snapshot(run['run_id'],user_id=OWNER)['revision']==0
    with pytest.raises(EvolutionStateError,match='archive is unavailable'):
        coordination.authorize_task(runtime[2][0])


@pytest.mark.parametrize('case',['foreign_owner','missing_run','changed_identity'])
def test_workspace_snapshot_denies_missing_or_mismatched_owner_record(runtime,case):
    runs,store,run,_=long_run(runtime)
    owner=OWNER+1 if case=='foreign_owner' else OWNER
    key=(records.NAMESPACE_RUNS,runs._key(run['run_id'],OWNER))
    if case=='missing_run':store.rows.pop(key)
    elif case=='changed_identity':store.rows[key]['run_id']='foreign-run'
    with pytest.raises(EvolutionStateError,match='run not found for this owner'):
        coordination.snapshot(run['run_id'],user_id=owner)
