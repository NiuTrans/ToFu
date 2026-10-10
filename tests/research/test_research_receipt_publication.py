"""Only verified receipt prefixes may become published workspace history."""
from copy import deepcopy
import pytest

from lib.research.receipt_pages import prepare_publication, ReceiptPages, empty_reference
from lib.research.persistence import research_direction_hash
from lib.research.program import dependency_digests, normalize_run_evidence
from lib.research.workspace import empty_workspace, save_workspace, load_workspace
from lib.storage.errors import StorageError
from tests.research.test_research_receipt_pages import fact, restore_chronological
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit
DIRECTION = 'Unfinished receipt continuity'


def initial():
    workspace = empty_workspace(DIRECTION)
    workspace['runs'] = normalize_run_evidence([dict(id='long-experiment', status='running',
        execution_status='running', tool_receipts=[fact(n) for n in range(40)],
        **dependency_digests(workspace))])
    return workspace


def prepare(client, workspace, **overrides):
    args = dict(user_id=1, paper_hash=research_direction_hash(DIRECTION), lang='en',
                run_id='long-experiment', revision=workspace['revision'])
    return prepare_publication(client, workspace, **(args | overrides))


def repo(client, run):
    return ReceiptPages(client, user_id=1, paper_hash=research_direction_hash(DIRECTION), lang='en',
        run_id=run['id'], hypothesis_digest=run['hypothesis_digest'], protocol_digest=run['protocol_digest'])


def test_workspace_cas_publishes_complete_prefix_and_preserves_tail_after_restart(storage):
    supervisor, _ = storage
    saved = save_workspace(DIRECTION, 'en', initial(), expected_revision=0, user_id=1)
    publication = prepare(supervisor.client, saved)
    assert 'receipt_history' not in load_workspace(DIRECTION, user_id=1)['runs'][0]
    folded = save_workspace(DIRECTION, 'en', saved, expected_revision=1, user_id=1,
                            receipt_publication=publication)
    assert folded['runs'][0]['receipt_history']['count'] == 8
    supervisor.stop(); supervisor.start()
    restored = load_workspace(DIRECTION, user_id=1); run = restored['runs'][0]
    assert restore_chronological(repo(supervisor.client, run), run['receipt_history']) + run['tool_receipts'] == saved['runs'][0]['tool_receipts']


def test_repeated_publication_retains_over_64_receipts_with_bounded_workspace(storage):
    supervisor, _ = storage
    saved = save_workspace(DIRECTION, 'en', initial(), expected_revision=0, user_id=1)
    for start in (40, 72, 104, 136):
        # Fold before extending: the port keeps the working tail bounded while
        # publishing every prefix. Production action reduction is a separate gate.
        saved = save_workspace(DIRECTION, 'en', saved, expected_revision=saved['revision'], user_id=1,
                               receipt_publication=prepare(supervisor.client, saved, keep_inline=8))
        candidate = deepcopy(saved)
        candidate['runs'][0]['tool_receipts'].extend(fact(n) for n in range(start, start+32))
        saved = save_workspace(DIRECTION, 'en', candidate, expected_revision=saved['revision'], user_id=1)
        assert len(saved['runs'][0]['tool_receipts']) <= 40
    run = saved['runs'][0]
    assert restore_chronological(repo(supervisor.client, run), run['receipt_history']) + run['tool_receipts'] == [fact(n) for n in range(168)]


@pytest.mark.parametrize('mutation', ['source', 'revision', 'owner', 'reference'])
def test_changed_preparation_cannot_publish(storage, mutation):
    supervisor, _ = storage
    saved = save_workspace(DIRECTION, 'en', initial(), expected_revision=0, user_id=1)
    publication = prepare(supervisor.client, saved)
    candidate = deepcopy(saved)
    if mutation == 'source': candidate['runs'][0]['tool_receipts'][0]['result_digest'] = 'changed'
    elif mutation == 'revision': publication.revision += 1
    elif mutation == 'owner': publication.binding['user_id'] = 2
    else: publication.reference = empty_reference()
    with pytest.raises((ValueError, StorageError)):
        save_workspace(DIRECTION, 'en', candidate, expected_revision=1, user_id=1, receipt_publication=publication)
    assert load_workspace(DIRECTION, user_id=1) == saved


def test_direct_pointer_and_failed_cas_cannot_publish_orphan_pages(storage, monkeypatch):
    supervisor, _ = storage
    saved = save_workspace(DIRECTION, 'en', initial(), expected_revision=0, user_id=1)
    publication = prepare(supervisor.client, saved)
    forged = deepcopy(saved); forged['runs'][0]['receipt_history'] = publication.reference
    with pytest.raises(ValueError, match='server-owned'):
        save_workspace(DIRECTION, 'en', forged, expected_revision=1, user_id=1)
    command = supervisor.client.command
    def refuse(operation, *args, **kwargs):
        if operation == 'research.workspace.put': raise StorageError('database_conflict', 'injected CAS refusal')
        return command(operation, *args, **kwargs)
    monkeypatch.setattr(supervisor.client, 'command', refuse)
    with pytest.raises(StorageError, match='injected CAS refusal'):
        save_workspace(DIRECTION, 'en', saved, expected_revision=1, user_id=1, receipt_publication=publication)
    assert load_workspace(DIRECTION, user_id=1) == saved


@pytest.mark.parametrize('mutation', ['delete', 'tail', 'settle', 'basis'])
def test_published_pending_history_cannot_be_erased_or_settled_from_partial_tail(storage, mutation):
    supervisor, _ = storage
    saved = save_workspace(DIRECTION, 'en', initial(), expected_revision=0, user_id=1)
    saved = save_workspace(DIRECTION, 'en', saved, expected_revision=1, user_id=1,
                           receipt_publication=prepare(supervisor.client, saved))
    candidate = deepcopy(saved)
    if mutation == 'delete': candidate['runs'] = []
    elif mutation == 'tail': candidate['runs'][0]['tool_receipts'] = []
    elif mutation == 'settle': candidate['runs'][0]['execution_status'] = 'completed'
    else: candidate['runs'][0]['hypothesis_digest'] = 'f' * 64
    with pytest.raises(ValueError): save_workspace(DIRECTION, 'en', candidate, expected_revision=2, user_id=1)
    assert load_workspace(DIRECTION, user_id=1) == saved


def test_paged_tail_cannot_grant_scientific_authority_or_silent_action_continuation(storage):
    from lib.research.action import apply_action_result
    from lib.research.decisions import authoritative
    supervisor, _ = storage
    saved = save_workspace(DIRECTION, 'en', initial(), expected_revision=0, user_id=1)
    saved = save_workspace(DIRECTION, 'en', saved, expected_revision=1, user_id=1,
                           receipt_publication=prepare(supervisor.client, saved))
    partial = dict(saved['runs'][0], validity='current', execution_status='completed')
    assert not authoritative(partial)
    with pytest.raises(ValueError, match='complete streamed evidence reducer'):
        apply_action_result(action='experiment', workspace=saved, task_id='poll', receipts=[fact(100)],
                            payload={'run': {'id': 'long-experiment'}},
                            bindings=[{'tool': 'poll', 'provider': 'cluster', 'capability': 'compute.status'}])
