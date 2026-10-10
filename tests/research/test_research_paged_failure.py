"""Failed paged jobs settle only from complete acknowledged producer observations."""
from copy import deepcopy
from dataclasses import replace
import pytest

from lib.research.workspace import save_workspace, load_workspace, open_run_receipts
from lib.research.receipt_settlement import prepare_failure_settlement
from lib.research.persistence import research_direction_hash
from lib.storage.errors import StorageError
from tests.research.test_research_pending_paging import save, apply, initial, receipt, DIRECTION
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit


def prepare(client, candidate, prior):
    return prepare_failure_settlement(client, candidate, prior, user_id=1,
        paper_hash=research_direction_hash(DIRECTION), lang='en', run_id='run', revision=prior['revision'])


def failed_candidate(client, prior):
    return apply(prior, 'failed-A', [receipt('job-A', 'failed', 'poll', 'failed-A')], client)


def test_failed_paged_run_settles_and_preserves_all_receipts_after_restart(storage):
    supervisor, _ = storage; prior = save(initial())
    candidate = failed_candidate(supervisor.client, prior)
    assert candidate['runs'][0]['execution_status'] == 'failed'
    assert candidate['runs'][0]['status'] == 'failed'
    port = prepare(supervisor.client, candidate, prior)
    saved = save_workspace(DIRECTION, 'en', candidate, expected_revision=prior['revision'], user_id=1,
                           failure_settlement=port)
    assert saved['runs'][0]['scientific_outcome'] == 'inconclusive'
    assert not saved['runs'][0]['artifact_refs'] and saved['history']['source_index']['count'] == 0
    supervisor.stop(); supervisor.start()
    assert load_workspace(DIRECTION, user_id=1) == saved
    with open_run_receipts(DIRECTION, user_id=1, run_id='run') as stream:
        rows = list(stream['receipts'])
    assert len(rows) == 3 and rows[-1]['execution_observation']['state'] == 'failed'
    # Unrelated workspace saves preserve settled failures exactly.
    save_workspace(DIRECTION, 'en', saved, expected_revision=saved['revision'], user_id=1)


@pytest.mark.parametrize('mutation', ['source', 'revision', 'bool_revision', 'owner', 'without_port', 'active'])
def test_forged_changed_or_incomplete_failed_projection_cannot_publish(storage, mutation):
    supervisor, _ = storage; prior = save(initial()); candidate = failed_candidate(supervisor.client, prior)
    port = prepare(supervisor.client, candidate, prior)
    if mutation == 'source': candidate['runs'][0]['tool_receipts'][-1]['result_digest'] = 'changed'
    elif mutation == 'revision': port = replace(port, revision=port.revision+1)
    elif mutation == 'bool_revision': port = replace(port, revision=True)
    elif mutation == 'owner': port = replace(port, user_id=2)
    elif mutation == 'without_port': port = None
    else:
        candidate = apply(prior, 'failed-B', [receipt('job-B', 'failed', 'poll', 'failed-B')], supervisor.client)
        candidate['runs'][0].update(status='failed', execution_status='failed')
        with pytest.raises(ValueError, match='complete failed job observations'):
            prepare(supervisor.client, candidate, prior)
        assert load_workspace(DIRECTION, user_id=1) == prior
        return
    with pytest.raises(ValueError):
        save_workspace(DIRECTION, 'en', candidate, expected_revision=prior['revision'], user_id=1,
                       failure_settlement=port)
    assert load_workspace(DIRECTION, user_id=1) == prior


@pytest.mark.parametrize('mutation', ['delete', 'receipt', 'revive'])
def test_settled_failure_cannot_be_erased_rewritten_or_reopened(storage, mutation):
    supervisor, _ = storage; prior = save(initial()); candidate = failed_candidate(supervisor.client, prior)
    saved = save_workspace(DIRECTION, 'en', candidate, expected_revision=prior['revision'], user_id=1,
        failure_settlement=prepare(supervisor.client, candidate, prior))
    changed = deepcopy(saved)
    if mutation == 'delete': changed['runs'] = []
    elif mutation == 'receipt': changed['runs'][0]['tool_receipts'][0]['result_digest'] = 'changed'
    else: changed['runs'][0].update(status='running', execution_status='running')
    with pytest.raises(ValueError): save_workspace(DIRECTION, 'en', changed, expected_revision=saved['revision'], user_id=1)
    assert load_workspace(DIRECTION, user_id=1) == saved


def test_failed_settlement_cas_refusal_cannot_publish_and_retry_preserves_sources(storage, monkeypatch):
    supervisor, _ = storage; prior = save(initial()); candidate = failed_candidate(supervisor.client, prior)
    port = prepare(supervisor.client, candidate, prior)
    command = supervisor.client.command
    def refuse(operation, *args, **kwargs):
        if operation == 'research.workspace.put': raise StorageError('database_conflict', 'refused failure CAS')
        return command(operation, *args, **kwargs)
    monkeypatch.setattr(supervisor.client, 'command', refuse)
    with pytest.raises(StorageError, match='refused failure CAS'):
        save_workspace(DIRECTION, 'en', candidate, expected_revision=prior['revision'], user_id=1, failure_settlement=port)
    assert load_workspace(DIRECTION, user_id=1) == prior
    monkeypatch.setattr(supervisor.client, 'command', command)
    saved = save_workspace(DIRECTION, 'en', candidate, expected_revision=prior['revision'], user_id=1, failure_settlement=port)
    assert saved['runs'][0]['execution_status'] == 'failed'
