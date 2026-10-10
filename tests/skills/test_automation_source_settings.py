"""Owned provider settings, real vault stages and selected authority recovery."""
import json
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from lib import credentials_vault as vault
from lib.automations.errors import AutomationConflict, AutomationError
from lib.identity import PrincipalContext
from lib.skills.automation.source_settings import AutomationSkillSources
from lib.storage.errors import StorageError

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api
SCOPES = {'automations:read', 'automations:manage', 'skills:install', 'skills:read'}
TOKEN = 'fixture-source-owned-credential-0123456789'


@pytest.fixture(autouse=True)
def private_vault(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, '_STORE_PATH', tmp_path / 'private-vault.json')
    monkeypatch.setattr(vault, '_KEY_PATH', tmp_path / '.private-vault.key')
    monkeypatch.setattr(vault, '_fernet', None)


def service(authority, *, tenant='source-settings', scopes=SCOPES, factory=None):
    client, owner, _ = authority
    principal = PrincipalContext.user(subject_id='source-owner', owner_user_id=owner,
                                      tenant_id=tenant, scopes=scopes)
    return AutomationSkillSources(principal, client_factory=factory or client)


def body(*, revision=0, request='configure', credential=TOKEN, active=True):
    return {'expectedRevision': revision, 'requestId': request, 'environment': 'prod',
            'active': active, 'credential': credential}


def test_config_replay_rotate_retire_and_private_projection(semantic_authority):
    sources = service(semantic_authority)
    assert sources.settings()['revision'] == 0
    first = sources.update(body())
    reference = sources.binding().value['credentialRef']
    assert sources.update(body()) == first
    assert len(vault.list_entries()) == 1
    entity, credential = sources.credential()
    assert credential.token == TOKEN and entity.version == 1
    assert TOKEN not in repr(credential)
    public = json.dumps(first)
    assert all(key not in public for key in [TOKEN, reference, 'credentialSeal', 'command'])
    assert TOKEN not in vault._STORE_PATH.read_text()
    second = sources.update(body(revision=1, request='rotate', credential=TOKEN + '-new'))
    assert second['revision'] == 2
    assert vault.get_entry('skill.' + reference) is None
    assert sources.credential()[1].token == TOKEN + '-new'
    retired = sources.update(body(revision=2, request='retire', credential=None, active=False))
    assert retired['active'] is False and retired['credentialConfigured'] is False
    assert vault.list_entries() == []
    with pytest.raises(AutomationError):
        sources.credential()


def test_foreign_tenant_and_scope_checks_precede_secret_access(semantic_authority, monkeypatch):
    own = service(semantic_authority)
    own.update(body())
    foreign = service(semantic_authority, tenant='foreign')
    assert foreign.settings()['revision'] == 0
    monkeypatch.setattr(vault, 'get_entry', lambda *_: pytest.fail('Unauthorized secret read'))
    with pytest.raises(AutomationError):
        foreign.credential()
    reader = service(semantic_authority, scopes={'automations:read'})
    assert reader.settings()['credentialConfigured'] is True
    with pytest.raises(PermissionError):
        reader.credential()
    with pytest.raises(PermissionError):
        reader.update(body(revision=1))


def test_rejected_cas_reclaims_only_its_new_stage(semantic_authority):
    sources = service(semantic_authority)
    sources.update(body())
    before = vault.list_entries()
    with pytest.raises(AutomationConflict):
        sources.update(body(request='stale', credential=TOKEN + '-other'))
    assert vault.list_entries() == before
    assert sources.credential()[1].token == TOKEN


def test_identical_replay_succeeds_with_full_vault(semantic_authority, monkeypatch):
    sources = service(semantic_authority)
    first = sources.update(body())
    monkeypatch.setattr(vault, '_MAX_ENTRIES', 1)
    assert sources.update(body()) == first
    assert len(vault.list_entries()) == 1


def test_concurrent_identical_configuration_selects_one_credential(semantic_authority):
    barrier = threading.Barrier(2)

    def configure():
        sources = service(semantic_authority)
        barrier.wait(timeout=10)
        return sources.update(body())

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(configure) for _ in range(2)]
        results = [future.result(timeout=60) for future in futures]
    assert results[0] == results[1]
    assert len(vault.list_entries()) == 1
    assert service(semantic_authority).credential()[1].token == TOKEN


def test_mutation_behind_immutable_reference_fails_seal(semantic_authority):
    sources = service(semantic_authority)
    sources.update(body())
    reference = sources.binding().value['credentialRef']
    vault.set_entry('skill.' + reference, TOKEN + '-tampered')
    with pytest.raises(AutomationError, match='version has changed'):
        sources.credential()


def acknowledgement_factory(factory, *, commit):
    class LostAck:
        def query(self, *args, **kwargs):
            return factory().query(*args, **kwargs)

        def command(self, *args, **kwargs):
            if commit:
                factory(write=True).command(*args, **kwargs)
            raise StorageError('database_timeout', 'Injected lost acknowledgement')
    return lambda **_: LostAck()


def test_lost_ack_reads_back_committed_version(semantic_authority):
    sources = service(semantic_authority, factory=acknowledgement_factory(semantic_authority[0], commit=True))
    result = sources.update(body())
    assert result['revision'] == 1 and len(vault.list_entries()) == 1
    assert sources.credential()[1].token == TOKEN


def test_unknown_ack_preserves_stage_until_cas_cannot_win(semantic_authority):
    unknown = service(semantic_authority, factory=acknowledgement_factory(semantic_authority[0], commit=False))
    with pytest.raises(StorageError):
        unknown.update(body())
    assert len(vault.list_entries()) == 1
    sources = service(semantic_authority)
    sources.update(body(request='next', credential=TOKEN + '-accepted'))
    assert len(vault.list_entries()) == 1
    assert sources.credential()[1].token == TOKEN + '-accepted'


@pytest.mark.parametrize('token', ['bad token', 'secret\nline', '敏感信息', 123])
def test_invalid_secret_is_rejected_without_echo_or_vault_write(semantic_authority, token):
    with pytest.raises(AutomationError) as rejected:
        service(semantic_authority).update(body(credential=token))
    assert str(token) not in str(rejected.value)
    assert vault.list_entries() == []


@pytest.mark.parametrize('payload', [
    [{'credential': TOKEN}], {**body(), 'environment': TOKEN},
    {**body(), 'expectedRevision': TOKEN}, {**body(), 'requestId': TOKEN + ' invalid'},
])
def test_malformed_body_never_echoes_secrets_in_other_fields(semantic_authority, payload):
    with pytest.raises(AutomationError) as rejected:
        service(semantic_authority).update(payload)
    assert TOKEN not in str(rejected.value)
    assert vault.list_entries() == []
