"""Cleanup uses the current owned aggregate, not a caller's older snapshot."""
from itertools import count

import pytest

from lib.model_routing import InMemoryModelRoutingRepository, OwnerBoundary, empty_document
from lib.storage import StorageSupervisor
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit


def referenced_document(reference, *, revision=1):
    document = empty_document()
    document['revision'] = revision
    document['providers'] = [{'provider_id': 'provider', 'name': 'Provider', 'scope': 'owner'}]
    document['provider_accesses'] = [{
        'provider_access_id': 'access', 'provider_id': 'provider',
        'enabled': True, 'quota_policy': {},
    }]
    document['credentials'] = [{
        'credential_id': 'credential', 'provider_access_id': 'access',
        'kind': 'api_key', 'secret_reference': reference, 'key_hint': 'fixture',
        'enabled': False, 'quota_policy': {},
        'authorization': {'connection_ids': [], 'models': []},
    }]
    return document


def test_memory_cleanup_preserves_a_newly_referenced_secret():
    repository = InMemoryModelRoutingRepository()
    owner = OwnerBoundary(7, 'workspace')
    repository.put_secret(owner, 'kept-value', secret_reference='shared-reference')
    # A cleanup candidate was selected before this later authority commit.
    first = repository.compare_and_swap(
        owner, referenced_document('shared-reference'), expected_revision=0)
    assert repository.delete_secret(owner, 'shared-reference') is False
    assert repository.resolve_secret(owner, 'shared-reference') == 'kept-value'
    candidate = first.document
    candidate['credentials'] = []
    repository.compare_and_swap(owner, candidate, expected_revision=first.revision)
    assert repository.delete_secret(owner, 'shared-reference') is True


@pytest.fixture(params=['sqlite', 'tofudb'])
def authority(request, tmp_path, monkeypatch):
    if request.param == 'tofudb':
        monkeypatch.setenv('TOFUDB_OWNER_ID', '7')
        monkeypatch.setenv('TOFUDB_TRANSPORT', 'daemon')
        from lib.storage.supervisor import _resolve_tofudb_binary
        try:
            _resolve_tofudb_binary()
        except RuntimeError as exc:
            pytest.skip(f'Explicit matching native test engine unavailable: {exc}')
    supervisor = StorageSupervisor(
        project_root=tmp_path / 'authority', backend=request.param, startup_timeout=60)
    supervisor.start()
    sequence = count()

    def command(operation, payload):
        return supervisor.client.command(operation, payload, f'secret-lifecycle-{next(sequence)}')

    try:
        yield supervisor.client, command, request.param
    finally:
        supervisor.stop()


@pytest.mark.parametrize('cleanup', ['delete', 'prune'])
@pytest.mark.parametrize('other', [{'owner_user_id': 8, 'tenant_id': 'workspace'},
                                  {'owner_user_id': 7, 'tenant_id': 'other-workspace'}])
def test_cleanup_rechecks_current_references_and_last_reference_removal(authority, cleanup, other):
    client, command, backend = authority
    owner = {'owner_user_id': 7, 'tenant_id': 'workspace'}
    reference = 'shared-reference'
    other_admitted = backend == 'sqlite' or other['owner_user_id'] == owner['owner_user_id']
    if not other_admitted:
        # The native connection authenticates one owner before semantic dispatch.
        with pytest.raises(StorageError) as denied:
            command('model_routing.secret.put', {
                **other, 'secret_reference': reference, 'ciphertext': 'foreign-ciphertext',
                'key_hint': 'fixture', 'updated_at': 1.0,
            })
        assert denied.value.code == 'database_forbidden'
    for boundary in (owner, other) if other_admitted else (owner,):
        command('model_routing.secret.put', {
            **boundary, 'secret_reference': reference, 'ciphertext': 'kept-ciphertext',
            'key_hint': 'fixture', 'updated_at': 1.0,
        })
        command('model_routing.commit', {
            **boundary, 'expected_revision': 0, 'document': referenced_document(reference),
            'updated_at': 2.0,
        })
    # The caller still holds the empty reference set from before revision 1.
    payload = {**owner, 'secret_reference': reference} if cleanup == 'delete' else {
        **owner, 'active_secret_references': [], 'updated_before': 100.0,
    }
    operation = 'model_routing.secret.' + cleanup
    result = client.command(operation, payload, 'retained-cleanup')
    assert result['deleted' if cleanup == 'delete' else 'count'] == 0
    assert client.query('model_routing.secret.get', {
        **owner, 'secret_reference': reference})['ciphertext'] == 'kept-ciphertext'
    document = referenced_document(reference, revision=2)
    document['credentials'] = []
    command('model_routing.commit', {
        **owner, 'expected_revision': 1, 'document': document, 'updated_at': 3.0,
    })
    # Delete has durable receipts; prune deliberately repeats its current scan.
    if cleanup == 'delete':
        assert client.command(operation, payload, 'retained-cleanup') == result
    result = command('model_routing.secret.' + cleanup, payload)
    assert result['deleted' if cleanup == 'delete' else 'count'] == 1
    assert client.query('model_routing.secret.get', {
        **owner, 'secret_reference': reference}) is None
    if other_admitted:
        assert client.query('model_routing.secret.get', {
            **other, 'secret_reference': reference})['ciphertext'] == 'kept-ciphertext'


def test_cleanup_refuses_unreadable_reference_authority(authority):
    client, command, _ = authority
    owner = {'owner_user_id': 7, 'tenant_id': 'workspace'}
    reference = 'retained-on-integrity-error'
    command('model_routing.secret.put', {
        **owner, 'secret_reference': reference, 'ciphertext': 'kept-ciphertext',
        'key_hint': 'fixture', 'updated_at': 1.0,
    })
    for revision, credentials in enumerate((None, {}, [None], [{'secret_reference': 3}]), 1):
        # Raw persistence admits opaque aggregate JSON; reclamation must not
        # interpret unreadable reference metadata as permission to delete.
        document = {'contract_version': 'tofu.model-routing/v2',
                    'revision': revision, 'credentials': credentials}
        command('model_routing.commit', {
            **owner, 'expected_revision': revision - 1, 'document': document, 'updated_at': 2.0,
        })
        for operation, payload in (
            ('delete', {'secret_reference': reference}),
            ('prune', {'active_secret_references': [], 'updated_before': 100.0}),
        ):
            with pytest.raises(StorageError) as refused:
                command('model_routing.secret.' + operation, {**owner, **payload})
            assert refused.value.code == 'database_integrity'
            assert client.query('model_routing.secret.get', {
                **owner, 'secret_reference': reference})['ciphertext'] == 'kept-ciphertext'
    # A legacy empty aggregate has no credentials; that differs from malformed data.
    command('model_routing.commit', {
        **owner, 'expected_revision': 4,
        'document': {'contract_version': 'tofu.model-routing/v2', 'revision': 5},
        'updated_at': 3.0,
    })
    assert command('model_routing.secret.delete', {
        **owner, 'secret_reference': reference})['deleted'] is True
