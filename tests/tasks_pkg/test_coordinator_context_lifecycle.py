"""Canonical coordinator evidence survives rejected manual context lifecycle work.

These fixtures use only a fresh temporary semantic authority. No application
instance, model, scheduler, alternate transcript or generated bundle is used.
"""
from copy import deepcopy
from types import SimpleNamespace
import time

import pytest

from lib.tasks_pkg.compaction._context_lifecycle import (
    COORDINATOR_COMPACTION_UNSUPPORTED, PROJECT_COMPACTION_UNSUPPORTED,
    ManualCompactionRefused,
)
from lib.conversations.compaction_policy import manual_compaction_rejection

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


@pytest.fixture
def coordinator(semantic_authority, monkeypatch):
    factory, owner, _ = semantic_authority
    from lib import storage
    from lib.agent_core.store import get_conversation_store
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.conversations.coordinator_reports import CoordinatorReports
    from lib.conversations.coordinator_budget import CoordinatorBudgetSettings
    from lib.project_dispatch.repository import ProjectDispatchRepository
    from lib.identity import PrincipalContext
    from lib.turn_lifecycle import create_turn_pair, bind_task, mark_task_started, get_turn
    from tests._seed import seed_conversation
    from lib.tasks_pkg.compaction import _manual

    monkeypatch.setattr(storage, 'get_storage_client', factory)
    # Any accidental paid work/archive publication is an immediate test failure.
    monkeypatch.setattr(_manual, '_generate_query_aware_summary', lambda *a, **kw: pytest.fail('summary purchased'))
    monkeypatch.setattr(_manual, '_archive_transcript', lambda *a, **kw: pytest.fail('archive created'))
    project = '/isolated-coordinator-context'
    bindings = ProjectCoordinationRepository(owner)
    binding = bindings.create(project, folder_id='test', expected_revision=0, command_id='create')
    conv = binding['conversationId']
    settings = CoordinatorBudgetSettings(owner, project)
    initial = settings.read()
    settings.update({'path': project, 'conversationId': conv, 'bindingRevision': binding['revision'],
        'expectedRevision': initial['revision'], 'settings': {**initial['settings'], 'maxTokens': 65536}})
    accepted = create_turn_pair(conv, user_id=owner, command_id='accepted-source',
        input_projection={'content': 'Coordinate this exact human request'}, config={})
    attempt = accepted['attempt']['attemptId']
    task = 'coordinator-context-source-task'
    bind_task(attempt, task, user_id=owner)
    mark_task_started(attempt, task, user_id=owner)
    human = get_turn(conv, accepted['turn']['parentTurnId'], user_id=owner)
    seed_conversation('independent-worker', user_id=owner, settings={'projectPath': project})
    repo = ProjectDispatchRepository(owner, project)
    request = {'projectPath': project, 'coordinatorId': conv, 'recipientId': 'independent-worker',
        'authorizationSource': {'conversationId': conv, 'turnId': human['turnId'],
                                'projectionRevision': human['projectionRevision']},
        'brief': 'Consult this request without new authority', 'model': 'model-a',
        'budget': {'maxTokens': 1000, 'maxModelRequests': 2, 'deadline': time.time() + 600}}
    grant = repo.authorize(request, PrincipalContext.user(
        subject_id=f'user:{owner}', owner_user_id=owner, scopes=['chat']), 'live-grant')
    scope = {'owner': owner, 'projectPath': project, 'coordinatorId': conv,
             'bindingRevision': binding['revision']}
    reports = CoordinatorReports(owner, project, notify=lambda *a: None)
    published = reports.publish(scope, {'title': 'Verified progress', 'message': 'Evidence remains addressable.',
        'kind': 'progress', 'idempotency_key': 'report'})
    factory(write=True).command('turn.event.record', dict(user_id=owner, attempt_id=attempt,
        task_id=task, terminal=True, status='completed', projection={'content': 'Source coordination complete'},
        settlement={'outcome': 'completed', 'cause': 'stop', 'resumeOptions': []}), 'settled-source')
    return SimpleNamespace(factory=factory, owner=owner, project=project, conv=conv, bindings=bindings,
        reports=reports, settings=settings, repo=repo, grant=grant, published=published,
        attempt=attempt, human=human, store=get_conversation_store())


def snapshot(r):
    from lib.conversations.repository import ConversationRepository
    from lib.turn_lifecycle import list_turns, get_attempt
    return deepcopy({'binding': r.bindings.read(r.project), 'budget': r.settings.read(),
        'document': dict(ConversationRepository().get(r.conv, user_id=r.owner)),
        'turns': list_turns(r.conv, user_id=r.owner)['turns'], 'attempt': get_attempt(r.attempt, user_id=r.owner),
        'grant': r.repo.read(r.grant['id']), 'report': r.reports.read()})


@pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)
@pytest.mark.parametrize('entry', ['engine', 'adapter'])
def test_rejection_keeps_canonical_source_attempt_report_grant_binding_and_budgets(coordinator, entry):
    r = coordinator
    before = snapshot(r)
    if entry == 'engine':
        from lib.tasks_pkg.compaction._manual import compact_conversation_now
        result = compact_conversation_now(r.conv, user_id=r.owner,
            config={'projectPath': '/forged-to-bypass-binding'})
        assert result == {'ok': False, 'error': COORDINATOR_COMPACTION_UNSUPPORTED}
    else:
        messages, _, revision = r.store.load_transcript(r.conv, user_id=r.owner)
        with pytest.raises(ManualCompactionRefused, match=COORDINATOR_COMPACTION_UNSUPPORTED):
            r.store.compact_turn_transcript(r.conv, messages, messages[-1:], revision,
                                          command_id='dangerous-direct', user_id=r.owner)
    assert snapshot(r) == before
    assert r.repo.validate(r.repo.admission(r.grant['id'])) == r.repo.read(r.grant['id'])
    assert before['report']['latest']['turnId'] == r.published['turnId']


@pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)
def test_unbound_moved_creation_identity_remains_protected(coordinator):
    r = coordinator
    from lib.conversations.settings_store import set_conversation_settings
    current = r.bindings.read(r.project)
    r.bindings.bind(r.project, '', expected_revision=current['revision'], command_id='clear')
    set_conversation_settings(r.conv, {'projectPath': '/another-project'}, user_id=r.owner, notify=False)
    before = snapshot(r)
    assert r.store.manual_compaction_rejection(r.conv, user_id=r.owner) == COORDINATOR_COMPACTION_UNSUPPORTED
    assert snapshot(r) == before


@pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)
def test_legacy_binding_and_former_project_identity_fail_closed(coordinator):
    r = coordinator
    from tests._seed import seed_conversation, seed_coordinator_binding
    seed_conversation('legacy-coordinator', user_id=r.owner, settings={'projectPath': '/legacy-project'})
    binding = seed_coordinator_binding('/legacy-project', 'legacy-coordinator', user_id=r.owner)
    assert r.store.manual_compaction_rejection('legacy-coordinator', user_id=r.owner) == COORDINATOR_COMPACTION_UNSUPPORTED
    r.bindings.bind('/legacy-project', '', expected_revision=binding['revision'], command_id='clear-legacy')
    assert r.store.manual_compaction_rejection('legacy-coordinator', user_id=r.owner) == PROJECT_COMPACTION_UNSUPPORTED
    # Bound-project workers also conservatively retain any canonical source refs.
    assert r.store.manual_compaction_rejection('independent-worker', user_id=r.owner) == PROJECT_COMPACTION_UNSUPPORTED


@pytest.mark.parametrize(
    ('semantic_authority', 'expected_error'),
    [('sqlite', None), ('native', 'database_forbidden')],
    indirect=['semantic_authority'],
)
def test_foreign_owner_never_learns_or_modifies_coordinator_state(
        coordinator, expected_error):
    from lib.agent_core.storage_errors import StorageError

    r = coordinator
    before = snapshot(r)
    if expected_error:
        # Native authenticates an owner before querying; SQL filters the row.
        with pytest.raises(StorageError) as raised:
            r.store.manual_compaction_rejection(r.conv, user_id=r.owner + 1)
        assert raised.value.code == expected_error
        assert raised.value.retryable is False
    else:
        assert r.store.manual_compaction_rejection(r.conv, user_id=r.owner + 1) == 'not_found'
    assert snapshot(r) == before


def test_owned_report_kind_survives_legacy_identity_project_move():
    document = SimpleNamespace(metadata={'settings': {'projectPath': '/moved'}})
    conversations = SimpleNamespace(get=lambda *a, **kw: document)
    bindings = SimpleNamespace(read=lambda project: pytest.fail('report provenance already protects identity'))
    assert manual_compaction_rejection('legacy', user_id=7,
        messages=[{'_turnKind': 'coordinator_report.delivery'}],
        conversations=conversations, bindings=bindings) == COORDINATOR_COMPACTION_UNSUPPORTED


def test_ordinary_never_coordinated_conversation_is_not_globally_disabled():
    document = SimpleNamespace(metadata={'settings': {'projectPath': '/ordinary'}})
    conversations = SimpleNamespace(get=lambda *a, **kw: document)
    bindings = SimpleNamespace(read=lambda project: {'conversationId': None, 'revision': 0})
    assert manual_compaction_rejection('ordinary', user_id=7,
        conversations=conversations, bindings=bindings) is None


def test_binding_read_failure_is_not_compaction_permission():
    from lib.storage.errors import StorageError
    document = SimpleNamespace(metadata={'settings': {'projectPath': '/owned'}})
    conversations = SimpleNamespace(get=lambda *a, **kw: document)

    def broken(project):
        raise StorageError('database_timeout', 'unknown binding')

    with pytest.raises(StorageError):
        manual_compaction_rejection('legacy', user_id=7, conversations=conversations,
                                    bindings=SimpleNamespace(read=broken))


@pytest.mark.parametrize('semantic_authority', ['sqlite', 'native'], indirect=True)
def test_adapter_rechecks_new_project_binding_on_its_selected_authority(
        semantic_authority, monkeypatch):
    from lib import storage
    from lib.conversations.project_coordination import ProjectCoordinationRepository
    from lib.tasks_pkg import persistence_store
    from tests._seed import seed_conversation

    factory, owner, _ = semantic_authority
    monkeypatch.setattr(storage, 'get_storage_client', factory)
    seed_conversation('ordinary', user_id=owner,
                      settings={'projectPath': '/newly-coordinated'},
                      messages=[{'role': 'user', 'content': 'Keep original request'},
                                {'role': 'assistant', 'content': 'Original evidence'}])
    monkeypatch.setattr(persistence_store, '_storage_client', factory)
    # The adapter supplied a client: repository reads must not resolve another.
    monkeypatch.setattr(storage, 'get_storage_client',
                        lambda **kw: pytest.fail('Admission escaped selected authority'))
    store = persistence_store.DefaultConversationStore()
    before = store.load_transcript('ordinary', user_id=owner)
    messages, _, revision = before
    assert store.manual_compaction_rejection(
        'ordinary', user_id=owner, messages=messages) is None

    bindings = ProjectCoordinationRepository(owner, client=factory)
    bindings.create('/newly-coordinated', folder_id='test',
                    expected_revision=0, command_id='bind-during-preparation')
    with pytest.raises(ManualCompactionRefused, match=PROJECT_COMPACTION_UNSUPPORTED):
        store.compact_turn_transcript(
            'ordinary', messages, messages[-1:], revision,
            command_id='prepared-before-binding', user_id=owner)
    assert store.load_transcript('ordinary', user_id=owner) == before
