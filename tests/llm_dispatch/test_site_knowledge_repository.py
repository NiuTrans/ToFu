"""Learned executable rules are isolated and published through the real record CAS."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest

from lib.identity import PrincipalContext
from lib.storage.errors import StorageError
from lib.site_knowledge.repository import KnowledgeConflict, SiteKnowledgeRepository

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.unit

DOMAIN = 'xiaohongshu.com'


def repository(authority, *, tenant='tenant-a', client='browser-a', profile='profile-a', factory=None):
    client_factory, owner, _engine = authority
    principal = PrincipalContext.user(subject_id=f'test:{owner}', owner_user_id=owner,
                                      tenant_id=tenant)
    return SiteKnowledgeRepository(principal, client_id=client, profile=profile,
                                   client_factory=factory or client_factory)


def rule(label='first'):
    return {'extractor_js': '() => []', 'wait_selector': '.note', 'scrolls': 2,
            'verified_at': 100.0, 'verified_by': 'fixture-task',
            'evidence': {'verified_count': 3}, 'notes': label, 'access': 'read'}


def test_rule_identity_includes_owner_tenant_device_and_profile(semantic_authority):
    first = repository(semantic_authority)
    saved = first.publish(DOMAIN, rule(), expected_version=0)
    assert saved.version == 1
    for changes in ({'tenant': 'tenant-b'}, {'client': 'browser-b'}, {'profile': 'profile-b'}):
        other = repository(semantic_authority, **changes)
        assert other.read(DOMAIN).entry is None
        other.publish(DOMAIN, rule(str(changes)), expected_version=0)
        assert other.read(DOMAIN).entry['notes'] != first.read(DOMAIN).entry['notes']
    factory, owner, _engine = semantic_authority
    foreign = SiteKnowledgeRepository(PrincipalContext.user(
        subject_id='foreign', owner_user_id=owner + 1, tenant_id='tenant-a'),
        client_id='browser-a', profile='profile-a', client_factory=factory)
    assert foreign.read(DOMAIN).entry is None


def test_only_one_candidate_can_replace_the_verified_revision(semantic_authority):
    repo = repository(semantic_authority)
    first = repo.publish(DOMAIN, rule(), expected_version=0)

    def publish(index):
        try:
            return repo.publish(DOMAIN, rule(str(index)), expected_version=first.version)
        except KnowledgeConflict:
            return None

    with ThreadPoolExecutor(max_workers=4) as workers:
        winners = [result for result in workers.map(publish, range(4)) if result is not None]
    assert len(winners) == 1
    assert repo.read(DOMAIN) == winners[0]
    assert winners[0].version == 2


@pytest.mark.parametrize('newer_write', [False, True])
def test_lost_ack_needs_exact_revision_and_value(semantic_authority, newer_write):
    factory, _owner, _engine = semantic_authority
    calls = []

    class LoseAck:
        def query(self, *args, **kwargs):
            return factory().query(*args, **kwargs)

        def command(self, operation, payload, command_id, **kwargs):
            calls.append(command_id)
            result = factory(write=True).command(operation, payload, command_id, **kwargs)
            if newer_write:
                replacement = deepcopy(payload)
                replacement['expected_version'] = result['version']
                replacement['value']['entry']['notes'] = 'newer verified rule'
                factory(write=True).command(operation, replacement, command_id + '-newer')
            raise StorageError('database_timeout', 'lost acknowledgement')

    repo = repository(semantic_authority, factory=lambda **kw: LoseAck())
    if newer_write:
        with pytest.raises(StorageError, match='lost acknowledgement'):
            repo.publish(DOMAIN, rule(), expected_version=0)
    else:
        assert repo.publish(DOMAIN, rule(), expected_version=0).version == 1
    assert len(calls) == 1
    actual = repository(semantic_authority).read(DOMAIN)
    assert actual.entry['notes'] == ('newer verified rule' if newer_write else 'first')


def test_retirement_preserves_revision_and_does_not_resurrect_old_proof(semantic_authority):
    repo = repository(semantic_authority)
    first = repo.publish(DOMAIN, rule(), expected_version=0)
    retired = repo.retire(DOMAIN, expected_version=first.version)
    assert retired.version == 2 and retired.entry is None
    with pytest.raises(KnowledgeConflict):
        repo.publish(DOMAIN, rule('stale'), expected_version=first.version)
    assert repo.publish(DOMAIN, rule('new'), expected_version=retired.version).version == 3


def test_returned_rows_and_input_do_not_alias_durable_state(semantic_authority):
    repo = repository(semantic_authority)
    candidate = rule()
    published = repo.publish(DOMAIN, candidate, expected_version=0)
    candidate['evidence']['verified_count'] = 999
    published.entry['evidence']['verified_count'] = 0
    assert repo.read(DOMAIN).entry['evidence']['verified_count'] == 3


@pytest.mark.parametrize('lose_ack', [False, True])
def test_in_flight_input_mutation_cannot_rewrite_the_published_receipt(semantic_authority, lose_ack):
    factory, _owner, _engine = semantic_authority
    candidate = rule()

    class MutateDuringCommit:
        def query(self, *args, **kwargs):
            return factory().query(*args, **kwargs)

        def command(self, *args, **kwargs):
            result = factory(write=True).command(*args, **kwargs)
            candidate['evidence']['verified_count'] = 999
            if lose_ack:
                raise StorageError('database_timeout', 'lost acknowledgement')
            return result

    repo = repository(semantic_authority, factory=lambda **kw: MutateDuringCommit())
    published = repo.publish(DOMAIN, candidate, expected_version=0)
    assert published.entry['evidence']['verified_count'] == 3
    assert published == repo.read(DOMAIN)


@pytest.mark.parametrize('change', ['owner', 'device', 'profile', 'domain', 'version'])
def test_foreign_or_malformed_storage_reply_fails_closed(semantic_authority, change):
    factory, _owner, _engine = semantic_authority
    repository(semantic_authority).publish(DOMAIN, rule(), expected_version=0)

    class AlterReply:
        def query(self, *args, **kwargs):
            row = deepcopy(factory().query(*args, **kwargs))
            if change == 'version':
                row['version'] = True
            elif change == 'domain':
                row['value']['domain'] = 'foreign.test'
            else:
                field = {'owner': 'owner_user_id', 'device': 'client_id', 'profile': 'profile'}[change]
                row['value']['scope'][field] = 999 if change == 'owner' else 'foreign'
            return row

    repo = repository(semantic_authority, factory=lambda **kw: AlterReply())
    with pytest.raises(ValueError, match='identity|revision'):
        repo.read(DOMAIN)
