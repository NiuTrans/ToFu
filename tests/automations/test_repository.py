"""Real authority tests for owner isolation, concurrent CAS, and lost ACKs."""

from concurrent.futures import ThreadPoolExecutor
import uuid

import pytest

from lib.automations.definitions import prepare_definition
from lib.automations.errors import AutomationConflict, AutomationError
from lib.automations.repository import AutomationRepository
from lib.automations.execution_repository import AutomationExecutionRepository
from lib.identity import PrincipalContext
from lib.storage.errors import StorageError
from tests.support.automation_fixtures import clock_definition, trigger

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = pytest.mark.unit


def principal(owner=71, tenant='tenant-a', scopes=None):
    return PrincipalContext.user(subject_id=f'test:{owner}', owner_user_id=owner,
                                 tenant_id=tenant, scopes=scopes or {
                                     'automations:read', 'automations:manage', 'automations:execute'})


def make_definition():
    value = clock_definition()
    value['id'] = 'clock-' + uuid.uuid4().hex
    return prepare_definition(value)


def test_same_id_isolated_across_owners_and_tenants(chat_sidecar):
    prepared = make_definition()
    repositories = [AutomationRepository(principal()), AutomationRepository(principal(72)),
                    AutomationRepository(principal(71, 'tenant-b'))]
    first = repositories[0].save_definition(prepared, expected_version=0, now=100)
    assert first.version == 1
    assert repositories[1].get('definition', prepared.definition['id']) is None
    assert repositories[2].get('definition', prepared.definition['id']) is None
    for repository in repositories[1:]:
        repository.save_definition(prepared, expected_version=0, now=101)
    for repository in repositories:
        own = repository.get('definition', prepared.definition['id'])
        assert own.value['ownerId'] == repository.owner_id
        assert own.value['tenantId'] == repository.principal.tenant_id


def test_concurrent_revision_allows_only_one_writer(chat_sidecar):
    repository = AutomationRepository(principal())
    prepared = make_definition()
    first = repository.save_definition(prepared, expected_version=0, now=100)

    def write(index):
        value = prepared.definition
        value['name'] = f'writer-{index}'
        try:
            return repository.save_definition(prepare_definition(value), expected_version=first.version,
                                              now=101 + index)
        except AutomationConflict:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(write, range(4)))
    successful = [value for value in results if value is not None]
    assert len(successful) == 1
    current = repository.get('definition', prepared.definition['id'])
    assert current.version == 2
    assert current.value['definition']['name'] == successful[0].value['definition']['name']


def test_lost_commit_ack_is_resolved_by_exact_authority_read(chat_sidecar):
    from lib.storage import get_storage_client

    class LoseOneAcknowledgement:
        def __init__(self):
            self.failed = False

        def command(self, *args, **kwargs):
            result = get_storage_client(write=True).command(*args, **kwargs)
            if not self.failed:
                self.failed = True
                raise StorageError('database_timeout', 'simulated lost commit acknowledgement')
            return result

        def query(self, *args, **kwargs):
            return get_storage_client().query(*args, **kwargs)

    client = LoseOneAcknowledgement()
    repository = AutomationRepository(principal(), client_factory=lambda **kwargs: client)
    prepared = make_definition()
    first = repository.save_definition(prepared, expected_version=0, now=100)
    assert first.version == 1
    persisted = repository.get('definition', prepared.definition['id'])
    assert persisted.value == first.value
    # Identical replay is one acknowledged command and never a second version.
    replay = repository.save_definition(prepared, expected_version=0, now=100)
    assert replay.version == 1
    assert repository.get('definition', prepared.definition['id']).version == 1


def test_uncommitted_timeout_is_not_reported_as_success(chat_sidecar):
    from lib.storage import get_storage_client

    class RejectWrite:
        def command(self, *args, **kwargs):
            raise StorageError('database_timeout', 'request never committed')

        def query(self, *args, **kwargs):
            return get_storage_client().query(*args, **kwargs)

    repository = AutomationRepository(principal(), client_factory=lambda **kwargs: RejectWrite())
    with pytest.raises(StorageError, match='never committed'):
        repository.save_definition(make_definition(), expected_version=0, now=100)


def test_read_only_principal_cannot_save_or_dispatch(chat_sidecar):
    repository = AutomationRepository(principal(scopes={'automations:read'}))
    with pytest.raises(PermissionError):
        repository.save_definition(make_definition(), expected_version=0, now=100)
    with pytest.raises(PermissionError):
        repository.put('cursor', 'not-authorized', {}, expected_version=0)


def test_retirement_preserves_definition_and_stale_editor_is_rejected(chat_sidecar):
    repository = AutomationRepository(principal())
    prepared = make_definition()
    saved = repository.save_definition(prepared, expected_version=0, now=100)
    retired = repository.retire_definition(saved, now=110)
    assert retired.value['deleted'] is True
    assert retired.value['definition']['enabled'] is False
    assert repository.get('definition', saved.id).value['compiled'] == saved.value['compiled']
    changed = prepared.definition
    changed['name'] = 'stale editor'
    with pytest.raises(AutomationConflict):
        repository.save_definition(prepare_definition(changed), expected_version=saved.version, now=111)


def test_record_evidence_has_a_byte_budget_and_pages_have_explicit_bounds(chat_sidecar):
    repository = AutomationRepository(principal())
    with pytest.raises(AutomationError) as caught:
        repository.put('cursor', 'too-large', {'output': 'a' * (512 * 1024)}, expected_version=0)
    assert caught.value.code == 'automation_record_too_large'
    with pytest.raises(AutomationError):
        repository.page('run', limit=65)
    with pytest.raises(AutomationError):
        repository.page('run', after='../other-owner')


def test_paginated_reads_converge_without_cross_owner_rows(chat_sidecar):
    owner = int(uuid.uuid4().hex[:6], 16) + 1000
    repository = AutomationRepository(principal(owner))
    other = AutomationRepository(principal(owner + 1))
    for index in range(5):
        repository.put('cursor', f'cursor-{index}', {'observed': index}, expected_version=0)
        other.put('cursor', f'cursor-{index}', {'observed': 99}, expected_version=0)
    first, cursor = repository.page('cursor', limit=2)
    second, cursor = repository.page('cursor', limit=2, after=cursor)
    third, cursor = repository.page('cursor', limit=2, after=cursor)
    assert [row.value['observed'] for row in [*first, *second, *third]] == list(range(5))
    assert cursor == ''


def test_one_trigger_is_one_occurrence_under_concurrent_delivery(chat_sidecar):
    repository = AutomationExecutionRepository(principal())
    prepared = make_definition()
    repository.publish(prepared, expected_version=0, now=100)
    event = trigger(event_id='event-' + uuid.uuid4().hex)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: repository.admit(prepared.definition['id'], event, now=101), range(4)))
    assert len({row.id for row in results}) == 1
    # Identical command replay is acknowledged to concurrent callers, so the
    # stable run/effect ids are the dispatch authority.
    stored = repository.get('run', results[0].id)
    assert stored.version == 1


def test_replayed_trigger_keeps_original_snapshot_after_edit_and_retirement(chat_sidecar):
    repository = AutomationExecutionRepository(principal())
    prepared = make_definition()
    saved = repository.publish(prepared, expected_version=0, now=100)
    event = trigger(event_id='event-' + uuid.uuid4().hex)
    first = repository.admit(saved.id, event, now=101)
    changed = prepared.definition
    changed['name'] = 'edited rule'
    saved = repository.publish(prepare_definition(changed), expected_version=saved.version, now=102)
    repository.retire_definition(saved, now=103)
    replay = repository.admit(saved.id, event, now=104)
    assert replay.value['snapshot']['name'] == 'Show task start time'
    assert replay.value['definitionRevision'] == first.value['definitionRevision'] == 1
    assert replay.id == first.id


def test_conflicting_trigger_evidence_cannot_reuse_an_occurrence(chat_sidecar):
    repository = AutomationExecutionRepository(principal())
    prepared = make_definition()
    saved = repository.publish(prepared, expected_version=0, now=100)
    event = trigger(event_id='event-' + uuid.uuid4().hex)
    event['evidence']['changed'] = True
    repository.admit(saved.id, event, now=101)
    event['evidence']['changed'] = 1
    with pytest.raises(AutomationConflict):
        repository.admit(saved.id, event, now=102)
