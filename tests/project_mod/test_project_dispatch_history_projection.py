"""History reads only its result preview; exact evidence remains complete and detached.

A later page reads Turn metadata and reuses a preview only for the exact Turn
revision and status it was derived from; every other evidence port stays live.
Page rows carry the contract's historyRequest projection; exact lookups do not.
"""

from copy import deepcopy
from pathlib import Path
import subprocess
import sys

import pytest

from lib.identity import PrincipalContext
from lib.project_dispatch.contract import schema
from lib.project_dispatch.repository import CONTRACT, _validate_schema
from lib.project_dispatch.service import HistoryPreviews, ProjectDispatchService
from tests.project_mod.test_project_dispatch_repository import context as context

pytestmark = pytest.mark.unit


@pytest.fixture
def evidence(context):
    c = context
    assignment = c.repo.authorize(c.request, c.principal, 'history-preview')
    c.repo.bind_execution(assignment['id'], 'worker-turn', 'worker-attempt')
    copies = []

    class ToolDetail(dict):
        def __deepcopy__(self, memo):
            copies.append(True)
            return deepcopy(dict(self), memo)

    projection = {'content': 'worker evidence ' * 500,
                  'toolCalls': [ToolDetail(output='full tool evidence', nested={'value': 7})]}
    turn = {'turnId': 'worker-turn', 'conversationId': 'recipient', 'actor': 'assistant',
            'projectionRevision': 4, 'projection': projection}
    attempt = {'attemptId': 'worker-attempt', 'turnId': 'worker-turn', 'conversationId': 'recipient',
               'taskId': 'worker-task', 'status': 'completed'}
    reads = []

    def read_turn(conversation, identity, *, user_id, include_projection=True):
        reads.append((conversation, identity, user_id, include_projection))
        return turn if include_projection else {key: value for key, value in turn.items() if key != 'projection'}

    service = ProjectDispatchService(c.repo, commands=object(), turn_get=read_turn,
                                    attempt_get=lambda *args, **kwargs: attempt)
    return c, assignment, service, turn, attempt, copies, reads


def test_history_does_not_copy_discarded_detail_and_exact_lookups_keep_it(evidence):
    c, assignment, service, turn, _, copies, _ = evidence
    history = service.history_page(principal=c.principal)
    preview = history['assignments'][0]['execution']['result']
    assert preview == {'content': turn['projection']['content'][:2000]}
    assert copies == []
    for query in ({'assignment_id': assignment['id']}, {'command_id': 'history-preview'}):
        exact = service.history_page(principal=c.principal, **query)
        result = exact['assignments'][0]['execution']['result']
        assert result == turn['projection']
        result['toolCalls'][0]['nested']['value'] = -1
        assert turn['projection']['toolCalls'][0]['nested']['value'] == 7
    assert len(copies) == 2


def test_history_rows_carry_only_the_list_request_projection(evidence):
    c, assignment, service, *_ = evidence
    page = service.history_page(principal=c.principal)
    _validate_schema(page, 'historyPage')
    listed = CONTRACT['$defs']['historyRequest']['properties']
    assert page['assignments'][0]['request'] == {
        name: value for name, value in assignment['request'].items() if name in listed}
    assert {'authorizationSource', 'model', 'budget'} <= assignment['request'].keys() - listed.keys()
    for query in ({'assignment_id': assignment['id']}, {'command_id': 'history-preview'}):
        exact = service.history_page(principal=c.principal, **query)
        _validate_schema(exact, 'assignmentLookup')
        assert exact['assignments'][0]['request'] == assignment['request']
        with pytest.raises(ValueError):
            _validate_schema(exact, 'historyPage')


def test_history_row_contract_is_the_view_with_a_projected_request():
    view, row = schema('view'), schema('historyRow')
    request, listed = view['properties'].pop('request'), row['properties'].pop('request')
    assert row['required'] == view['required'] and row['properties'] == view['properties']
    assert listed['properties'] == {name: request['properties'][name] for name in listed['properties']}
    assert set(listed['required']) == set(listed['properties']) & set(request['required'])
    root = Path(__file__).resolve().parents[2]
    subprocess.run([sys.executable, 'scripts/gen_project_dispatch_contract.py', '--check'], cwd=root, check=True)


@pytest.mark.parametrize('content', [None, {'unexpected': 'shape'}, ['parts']])
def test_history_preserves_non_text_result_preview(evidence, content):
    c, _, service, turn, _, copies, reads = evidence
    turn['projection']['content'] = content
    for _ in range(2):
        assert service.history_page(principal=c.principal)['assignments'][0]['execution']['result'] == {}
    assert [read[3] for read in reads] == [True, False]
    assert copies == []


def test_unchanged_turn_reuses_its_exact_preview_without_reading_the_body(evidence):
    c, _, service, turn, _, copies, reads = evidence
    first = service.history_page(principal=c.principal)
    assert service.history_page(principal=c.principal) == first
    assert first['assignments'][0]['execution']['result'] == {'content': turn['projection']['content'][:2000]}
    assert reads == [('recipient', 'worker-turn', c.repo.owner, True),
                     ('recipient', 'worker-turn', c.repo.owner, False)]
    assert copies == []


@pytest.mark.parametrize('change', [{'projectionRevision': 5}, {'status': 'interrupted'}])
def test_changed_turn_revision_or_status_displays_the_new_body(evidence, change):
    c, _, service, turn, attempt, _, reads = evidence
    service.history_page(principal=c.principal)
    turn.update(change, projection={'content': 'revised evidence'})
    attempt['status'] = 'interrupted'
    execution = service.history_page(principal=c.principal)['assignments'][0]['execution']
    assert execution['result'] == {'content': 'revised evidence'}
    assert execution['resultRevision'] == turn['projectionRevision']
    assert execution['status'] == 'interrupted'
    assert [read[3] for read in reads] == [True, False, True]
    service.history_page(principal=c.principal)
    assert [read[3] for read in reads] == [True, False, True, False]


def test_retained_previews_are_only_the_latest_page_of_each_owner_project(evidence, context):
    c, _, service, turn, _, _, reads = evidence
    shared = HistoryPreviews()
    service.history_previews = shared
    service.history_page(principal=c.principal)
    other_project = ProjectDispatchService(context.repository(project='/other'), commands=object(),
        turn_get=service.turn_get, attempt_get=service.attempt_get, history_previews=shared)
    assert other_project.history_page(principal=c.principal)['assignments'] == []
    service.history_page(principal=c.principal)
    assert [read[3] for read in reads] == [True, False]
    # A build displays only its principal's tenant; its slot then retains nothing.
    tenant_b = PrincipalContext.user(subject_id='user:7', owner_user_id=7, tenant_id='tenant-b', scopes=['chat'])
    assert service.history_page(principal=tenant_b)['assignments'] == []
    assert service.history_page(principal=c.principal)['assignments'][0]['execution']['result'] == {
        'content': turn['projection']['content'][:2000]}
    assert [read[3] for read in reads] == [True, False, True]


@pytest.mark.parametrize('remembered', [False, True])
@pytest.mark.parametrize('record,key', [('turn', 'turnId'), ('turn', 'actor'), ('attempt', 'conversationId')])
def test_preview_does_not_bypass_execution_binding(evidence, record, key, remembered):
    c, _, service, turn, attempt, _, _ = evidence
    if remembered:
        service.history_page(principal=c.principal)
    {'turn': turn, 'attempt': attempt}[record][key] = 'foreign'
    with pytest.raises(ValueError, match='association changed'):
        service.history_page(principal=c.principal)


def test_settlement_still_reads_metadata_without_result(evidence):
    c, assignment, service, _, _, copies, reads = evidence
    scope = {'user_id': 7, 'project_path': '/project', 'conversation_id': 'coordinator', 'binding_revision': 1}
    view = service.view_for_coordinator(assignment['id'], scope=scope, settlement_only=True)
    assert view['execution']['result'] == {}
    assert reads == [('recipient', 'worker-turn', c.repo.owner, False)]
    assert copies == []


@pytest.fixture
def bound_rows(context):
    """Three bound rows whose Turn/attempt reads are recorded in page order (newest first)."""
    c = context
    for number in range(3):
        row = c.repo.authorize(c.request, c.principal, f'row-{number}')
        c.repo.bind_execution(row['id'], f'turn-{number}', f'attempt-{number}')
    reads = []

    def read_turn(conversation, identity, *, user_id, include_projection=True):
        reads.append(identity)
        turn = {'turnId': identity, 'conversationId': 'recipient', 'actor': 'assistant', 'projectionRevision': 1}
        return {**turn, 'projection': {'content': 'result ' + identity}} if include_projection else turn

    def read_attempt(identity, *, user_id):
        reads.append(identity)
        number = identity.rsplit('-', 1)[1]
        return {'attemptId': identity, 'turnId': 'turn-' + number, 'conversationId': 'recipient',
                'taskId': 'task-' + number, 'status': 'completed'}
    service = ProjectDispatchService(c.repo, commands=object(), turn_get=read_turn, attempt_get=read_attempt)
    return c, service, reads


def test_cancelled_build_reads_no_further_row_and_keeps_the_last_complete_page(bound_rows):
    from lib.project_dispatch.service import HistoryBuildCancelled
    c, service, reads = bound_rows
    complete = service.history_page(principal=c.principal)
    assert [view['execution']['turnId'] for view in complete['assignments']] == ['turn-2', 'turn-1', 'turn-0']
    slot = service.history_previews.previous(c.repo.owner, c.repo.project)
    reads.clear()
    # The client leaves while the first row is read: no later row is read.
    with pytest.raises(HistoryBuildCancelled):
        service.history_page(principal=c.principal, cancelled=lambda: bool(reads))
    assert reads == ['turn-2', 'attempt-2']
    # Only a complete build replaces the remembered previews.
    assert service.history_previews.previous(c.repo.owner, c.repo.project) is slot


def test_build_whose_client_left_before_it_started_reads_nothing(bound_rows):
    from lib.project_dispatch.service import HistoryBuildCancelled
    c, service, reads = bound_rows
    before = c.client.reads
    with pytest.raises(HistoryBuildCancelled):
        service.history_page(principal=c.principal, cancelled=lambda: True)
    assert c.client.reads == before and reads == []
