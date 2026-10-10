"""Pending source/job indexes stay scoped, immutable, bounded and non-authoritative."""
from copy import deepcopy
import pytest

from lib.research.progress_index import ProgressIndexes, empty_progress, normalize_progress, SOURCE_NAMESPACE, JOB_NAMESPACE
from lib.research.source_index import ReceiptSourceIndex, receipt_key, receipt_digest
from lib.research.execution_observation import reduce_execution_state, iter_execution_states
from lib.storage.errors import StorageError
from tests.research.test_research_receipt_pages import fact
from tests.research.test_research_action_observations import MemoryRecords
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit


def repo(client, **overrides):
    args = dict(user_id=1,paper_hash='a'*32,lang='en',run_id='run',hypothesis_digest='b'*64,protocol_digest='c'*64,cache_entries=65)
    return ProgressIndexes(client, **(args|overrides))


def observation(n, job, state, provider='cluster'):
    return dict(fact(n),execution_observation={'state':state,'job_id':job,'provider_scope':'provider:'+provider})


def test_delta_updates_match_complete_chronology_without_changing_prior_roots():
    client=MemoryRecords(); indexes=repo(client); rows=[observation(0,'old','running')]
    first=indexes.advance(empty_progress(),iter(rows)); progress=first
    for n in range(1,500):
        row=observation(n,f'job-{n}','completed');rows.append(row)
        progress=indexes.advance(progress,iter([row]))
        assert indexes.status(progress)==reduce_execution_state(iter_execution_states(iter(rows)))
    final=indexes.advance(progress,iter([observation(500,'old','completed')]))
    assert indexes.status(first)=='running' and indexes.status(final)=='completed'
    assert final['jobs']['count']==500 and final['sources']['count']==501
    assert len(indexes.sources.cache)<=65 and len(indexes.jobs.cache)<=65
    assert indexes.lookup_source(final,rows[0])==receipt_digest(rows[0])
    consumed = ReceiptSourceIndex(client,user_id=1,paper_hash='a'*32,lang='en',cache_entries=65)
    with pytest.raises(StorageError,match='missing or invalid'):
        consumed.lookup(final['sources'],receipt_key(rows[0]))


def test_pending_sources_reject_changed_facts_without_publishing_candidate():
    client=MemoryRecords();indexes=repo(client);row=observation(0,'job','running')
    prior=indexes.advance(empty_progress(),[row]);before=deepcopy(prior)
    with pytest.raises(ValueError,match='source identity conflicts'):
        indexes.advance(prior,[dict(row,result_digest='different')])
    assert prior==before and indexes.status(prior)=='running'


def test_provider_scope_and_transport_error_cannot_clear_original_job():
    indexes=repo(MemoryRecords());progress=indexes.advance(empty_progress(),[
        observation(0,'job','running'),observation(1,'job','completed','another'),
        dict(observation(2,'job','completed'),status='error')])
    assert indexes.status(progress)=='running' and progress['job_counts']['running']==1
    failed=indexes.advance(progress,[observation(3,'job','failed')])
    assert indexes.status(failed)=='failed' and failed['job_counts']['running']==0


@pytest.mark.parametrize('override',[dict(user_id=2),dict(lang='zh'),dict(run_id='other'),dict(hypothesis_digest='d'*64)])
def test_foreign_pending_index_roots_cannot_supply_source_or_job_facts(override):
    client=MemoryRecords();indexes=repo(client);row=observation(0,'job','running')
    progress=indexes.advance(empty_progress(),[row])
    with pytest.raises(StorageError,match='missing or invalid'):
        repo(client,**override).lookup_source(progress,row)


def test_lost_ack_keeps_old_roots_and_identical_preparation_is_idempotent():
    client=MemoryRecords();client.lose_ack=True;indexes=repo(client)
    before=empty_progress();row=observation(0,'job','running')
    first=indexes.advance(before,[row]);assert before==empty_progress()
    assert indexes.advance(before,[row])==first
    assert indexes.status(first)=='running'


@pytest.mark.parametrize('mutation',['bool_count','job_total','source_total','extra'])
def test_invalid_progress_counts_and_keys_are_rejected(mutation):
    progress=empty_progress()
    if mutation=='bool_count':progress['receipts']=False
    elif mutation=='job_total':progress['job_counts']['running']=1
    elif mutation=='source_total':progress['sources']['count']=1
    else:progress['unknown']=True
    with pytest.raises(ValueError):normalize_progress(progress)


def test_pending_indexes_survive_selected_storage_restart(storage):
    supervisor,_=storage;indexes=repo(supervisor.client)
    before=indexes.advance(empty_progress(),[observation(0,'job','running')])
    supervisor.stop();supervisor.start();reopened=repo(supervisor.client)
    final=reopened.advance(before,[observation(1,'job','completed')])
    assert reopened.status(final)=='completed' and reopened.status(before)=='running'
    assert reopened.lookup_source(final,observation(0,'job','running')) is not None
