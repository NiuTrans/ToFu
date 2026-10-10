"""Published index polling avoids history reads without weakening terminal checks."""
from copy import deepcopy
import pytest

from lib.research.action import apply_action_result
from lib.research.progress_checkpoint import ProgressCheckpointRepository
from lib.research.persistence import research_direction_hash
from lib.storage.errors import StorageError
from tests.research.test_research_pending_paging import save,initial,receipt,DIRECTION,BINDINGS,reader
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark=pytest.mark.unit


def progress_reader(client):
    def read(run):
        repository=ProgressCheckpointRepository(client,user_id=1,paper_hash=research_direction_hash(DIRECTION),lang='en',run=run)
        return repository,repository.read(run)
    return read


def apply(client, workspace, task, rows, *, indexed=True):
    def forbidden(run):raise AssertionError('indexed poll must not scan receipt history')
    return apply_action_result(action='experiment',workspace=workspace,task_id=task,receipts=rows,
        payload={'run':{'id':'run'}},bindings=BINDINGS,
        pending_receipt_reader=forbidden if indexed else reader(client),
        pending_progress_reader=progress_reader(client) if indexed else None)


@pytest.mark.parametrize('state',['running','completed','failed'])
def test_indexed_job_state_matches_full_history_without_page_scan(storage,state):
    supervisor,_=storage;saved=save(initial());rows=[receipt('job-A',state,'poll','new')]
    optimized=apply(supervisor.client,saved,'next',rows)
    full=apply(supervisor.client,saved,'next',rows,indexed=False)
    assert optimized['runs'][0]['execution_status']==full['runs'][0]['execution_status']==state


def test_archived_source_replay_and_conflict_are_checked_with_trie_lookup(storage):
    supervisor,_=storage;saved=save(initial());row=receipt('job-B','completed','poll','new')
    saved=save(apply(supervisor.client,saved,'same', [row]))
    replay=apply(supervisor.client,saved,'same',[dict(row,observed_at=row['observed_at']+100)])
    assert replay['runs'][0]['tool_receipts']==[]
    with pytest.raises(ValueError,match='source identity conflicts'):
        apply(supervisor.client,saved,'same',[dict(row,result_digest='changed')])


def test_invalid_checkpoint_does_not_fall_back_to_ignoring_history(storage):
    supervisor,_=storage;saved=save(initial());corrupt=deepcopy(saved)
    corrupt['runs'][0]['receipt_progress']='f'*64
    with pytest.raises(StorageError,match='checkpoint is missing or invalid'):
        apply(supervisor.client,corrupt,'next',[receipt('job-B','completed','poll','new')])


def test_preview_is_read_only_and_does_not_publish_candidate_indexes(storage,monkeypatch):
    supervisor,_=storage;saved=save(initial())
    def forbidden(*args,**kwargs):raise AssertionError('poll preview must not write records')
    monkeypatch.setattr(supervisor.client,'command',forbidden)
    result=apply(supervisor.client,saved,'next',[receipt('job-B','completed','poll','new')])
    assert result['runs'][0]['execution_status']=='running'
