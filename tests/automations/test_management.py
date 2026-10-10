"""Real-authority management, immutable manual admission, and indexed history."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import pytest

from lib.automations.errors import AutomationConflict, AutomationError
from lib.automations.management import AutomationManagement
from lib.automations.management_contract import validate
from lib.identity import PrincipalContext
from tests.support.automation_fixtures import clock_definition

pytestmark = pytest.mark.api
pytest_plugins = ('tests.support.semantic_authorities',)


def service(authority, *, owner=None, tenant='management', scopes=None, **options):
    factory, actual_owner, _ = authority
    principal = PrincipalContext.user(subject_id='management-publisher',
        owner_user_id=owner or actual_owner, tenant_id=tenant,
        scopes=scopes if scopes is not None else
        {'automations:read', 'automations:manage', 'automations:execute'})
    return AutomationManagement(principal, client_factory=factory, **options)


def publish(manager, *, revision=0, request='publish-1', rule=None):
    rule = rule or clock_definition()
    return manager.publish(rule['id'], {'expectedRevision': revision,
                                       'requestId': request, 'definition': rule})


def test_publication_pause_retire_replays_and_isolation(semantic_authority):
    manager = service(semantic_authority)
    first = publish(manager)
    assert publish(manager) == first
    validate('Definition', first)
    assert first['status'] == 'published'
    assert set(first) == {'id', 'revision', 'definition', 'digest', 'status', 'updatedAt'}
    with pytest.raises(AutomationConflict):
        publish(manager, request='new-command')
    body = {'expectedRevision': 1, 'requestId': 'pause-1'}
    paused = manager.inactive(first['id'], body)
    assert paused['revision'] == 2 and paused['status'] == 'paused'
    assert manager.inactive(first['id'], body) == paused
    with pytest.raises(AutomationConflict):
        manager.inactive(first['id'], body, retire=True)
    retired = manager.inactive(first['id'], {'expectedRevision': 2, 'requestId': 'retire-1'}, retire=True)
    assert retired['status'] == 'retired'
    assert manager.inactive(first['id'], {'expectedRevision': 2, 'requestId': 'retire-1'}, retire=True) == retired
    for foreign in (service(semantic_authority, owner=semantic_authority[1]+1),
                    service(semantic_authority, tenant='other')):
        assert foreign.definitions()['items'] == []
        with pytest.raises(AutomationError) as exc:
            foreign.definition(first['id'])
        assert exc.value.code == 'automation_not_found'


def test_manual_retry_freezes_server_time_and_revision(semantic_authority):
    manager = service(semantic_authority)
    published = publish(manager)
    body = {'expectedRevision': 1, 'requestId': 'manual-1'}
    first = manager.start(published['id'], body)
    assert first['trigger']['origin'] == 'human'
    assert first['trigger']['kind'] == 'manual'
    assert first['trigger']['evidence'] == {}
    assert first['createdAt'] == first['trigger']['occurredAt']
    validate('Run', first)
    assert 'principal' not in first and 'effectKey' not in first['steps']['show']
    changed = deepcopy(published['definition']); changed['name'] = 'Edited rule'
    publish(manager, revision=1, request='publish-2', rule=changed)
    assert manager.start(published['id'], body) == first
    with pytest.raises(AutomationConflict):
        manager.start(published['id'], {**body, 'expectedRevision': 2})
    with pytest.raises(AutomationConflict):
        manager.start(published['id'], {**body, 'requestId': 'stale-new'})
    assert manager.run(first['id']) == first
    cancelled = manager.cancel(first['id'], {'requestId': 'cancel-1', 'reason': 'User stopped'})
    assert cancelled['status'] == 'cancelled'
    assert manager.cancel(first['id'], {'requestId': 'cancel-1', 'reason': 'User stopped'}) == cancelled
    assert manager.history(published['id'])['items'] == [cancelled]


def test_concurrent_manual_first_delivery_creates_one_occurrence(semantic_authority):
    manager = service(semantic_authority)
    definition = publish(manager)
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda _: manager.start(definition['id'],
            {'expectedRevision': 1, 'requestId': 'same-manual'}), range(4)))
    assert all(row == rows[0] for row in rows)
    assert len(manager.history(definition['id'])['items']) == 1


def test_newest_history_cursor_is_bounded_and_rule_scoped(semantic_authority):
    manager = service(semantic_authority)
    definition = publish(manager)
    rows = [manager.start(definition['id'], {'expectedRevision': 1, 'requestId': 'run-'+str(i)})
            for i in range(4)]
    rule = clock_definition(); rule['id'] = 'other-definition'; publish(manager, rule=rule, request='other')
    foreign = manager.start(rule['id'], {'expectedRevision': 1, 'requestId': 'other-run'})
    first = manager.history(definition['id'], limit=2)
    second = manager.history(definition['id'], limit=2, before=first['nextCursor'])
    assert [row['id'] for row in first['items']+second['items']] == [row['id'] for row in sorted(rows, key=lambda row: (int(row['createdAt']*1000), row['id']), reverse=True)]
    assert manager.history(definition['id'], limit=2, before=second['nextCursor'])['items'] == []
    assert foreign['id'] not in {row['id'] for row in first['items']+second['items']}
    validate('RunPage', first)
    for options in ({'limit': 9}, {'limit': True}, {'before': 'foreign:run'}, {'before': '../'}):
        with pytest.raises(AutomationError):
            manager.history(definition['id'], **options)


def test_action_grants_and_project_ownership_are_explicit(semantic_authority,tmp_path):
    read = service(semantic_authority, scopes={'automations:read'})
    assert read.actions()['items']
    with pytest.raises(PermissionError):
        publish(read)
    manager = service(semantic_authority,project_resolver=None)
    from lib.automations.projects import project_id
    project=project_id(str(tmp_path.resolve()))
    rule = clock_definition(); rule['projectId'] = project
    with pytest.raises(AutomationError) as unavailable:
        publish(manager, rule=rule)
    assert unavailable.value.code == 'automation_project_unavailable'
    manager.update_project({'root':str(tmp_path),'label':'Owned','active':True,'expectedRevision':0,'requestId':'register'})
    manager.project_resolver = manager.projects_repository.owns
    published = publish(manager, rule=rule)
    run = manager.start(published['id'], {'expectedRevision': 1, 'requestId': 'project-run'})
    assert run['trigger']['projectId'] == project
    manager.project_resolver = lambda *args: False
    with pytest.raises(AutomationError) as denied:
        manager.start(published['id'], {'expectedRevision': 1, 'requestId': 'new-project-run'})
    assert denied.value.code == 'automation_not_found'
    assert manager.start(published['id'], {'expectedRevision': 1, 'requestId': 'project-run'}) == run


def test_real_scoped_credential_preserves_narrow_automation_capabilities(semantic_authority, monkeypatch):
    from lib.api_keys import _crud, _validate
    from tests.support.automation_fixtures import skill_definition
    factory, owner, _ = semantic_authority
    monkeypatch.setattr(_crud, 'get_storage_client', factory)
    monkeypatch.setattr(_validate, 'get_storage_client', factory)
    scopes = ['automations:read', 'automations:manage', 'automations:execute',
              'skills:read', 'skills:install', 'conversation.execute']
    row, token = _crud.create_key(name='automation-key', scopes=scopes,
                                 owner_user_id=owner, tenant_id='management')
    context = _validate.validate_token(token)
    assert context is not None and context.scopes == frozenset(scopes)
    assert context.owner_user_id == owner and context.tenant_id == 'management'
    assert not context.has_scope('admin')
    principal = PrincipalContext.user(subject_id=context.key_id, owner_user_id=owner,
                                      tenant_id=context.tenant_id, scopes=context.scopes)
    manager = AutomationManagement(principal, client_factory=factory)
    definition = publish(manager, rule=skill_definition())
    assert definition['status'] == 'published'
    grant = manager.repository.get('definition', definition['id']).value['grant']['principal']
    assert set(grant['scopes']) == set(scopes) - {'automations:manage'}
