"""Passive roster reads must discover empty shells without starting work."""
from copy import deepcopy
from types import SimpleNamespace
import json
from pathlib import Path
from unittest.mock import Mock

import jsonschema
import pytest

@pytest.fixture(autouse=True)
def no_dispatch_reads(monkeypatch):
    from lib.conversations import coordinator_conversations
    monkeypatch.setattr(coordinator_conversations, 'project_catalog_origins', lambda owner, project, **kwargs: {})

from lib.conversations.coordinator_conversations import PAGE_SIZE, conversation_page, project_catalog_origins
from lib.project_dispatch.history import RecipientOrigins
from lib.project_dispatch.segmented import FIELDS, FORMAT, key
from lib.storage.errors import StorageError
from lib.storage.generated_project_dispatch_contract import SPEC

pytestmark = pytest.mark.unit


def row(identity, project='/project', turns=0, **settings):
    return SimpleNamespace(metadata={'id': identity, 'title': identity,
        'updated_at': 20, 'msg_count': turns,
        'settings': {'projectPath': project, **settings}})


def repository(*pages):
    return SimpleNamespace(list_catalog_page=Mock(side_effect=[
        SimpleNamespace(items=items, has_more=more) for items, more in pages]))


def test_empty_worker_is_discoverable_without_assignment_or_folder():
    repo = repository(([row('worker-empty', _coordinatorSource='coordinator-root'),
        row('ordinary', turns=2), row('other', '/other'), row('archived', archived=True)], False))
    result = conversation_page(7, '/project/', repository=repo)
    assert result == {'conversations': [
        {'conversationId': 'worker-empty', 'title': 'worker-empty', 'turnCount': 0,
         'createdAt': 0, 'updatedAt': 20, 'coordinatorId': 'coordinator-root', 'folderId': None},
        {'conversationId': 'ordinary', 'title': 'ordinary', 'turnCount': 2,
         'createdAt': 0, 'updatedAt': 20, 'coordinatorId': None, 'folderId': None}],
        'nextCursor': None, 'coverage': 'complete'}
    args = repo.list_catalog_page.call_args.kwargs
    assert args['user_id'] == 7 and args['limit'] == PAGE_SIZE
    assert 'include_messages' not in args
    assert set(args['settings_keys']) == {'projectPath', '_coordinatorSource', 'archived', 'readOnly',
                                          'folderId'}
    schema = json.loads((Path(__file__).resolve().parents[2] / 'contracts/project_coordination_v1.schema.json').read_text())
    jsonschema.validate(result, {'$ref': '#/$defs/conversationPage', '$defs': schema['$defs']})


def test_rows_carry_their_sidebar_folder_so_adopted_shells_keep_membership():
    """The sidebar adopts roster rows its catalog lacks as catalog shells. A row
    without its folder projects as uncategorized although storage files it."""
    repo = repository(([row('filed', folderId='f_project'), row('unfiled'),
                        row('malformed', folderId=42), row('overlong', folderId='f' * 513)], False))
    result = conversation_page(7, '/project', repository=repo)
    assert [(item['conversationId'], item['folderId']) for item in result['conversations']] == [
        ('filed', 'f_project'), ('unfiled', None), ('malformed', None), ('overlong', None)]
    schema = json.loads((Path(__file__).resolve().parents[2] / 'contracts/project_coordination_v1.schema.json').read_text())
    jsonschema.validate(result, {'$ref': '#/$defs/conversationPage', '$defs': schema['$defs']})


def test_live_worker_is_stamped_busy_and_idle_rows_carry_no_flag():
    """An unopened running worker must not be presented as finished: the page
    carries the owner's live-task projection exactly like sidebar catalog rows."""
    repo = repository(([row('worker-live', _coordinatorSource='root'),
                        row('worker-idle', _coordinatorSource='root'),
                        row('other-live', '/other')], False))
    result = conversation_page(7, '/project', repository=repo,
                               busy_conversation_ids=frozenset({'worker-live', 'other-live'}))
    entries = {item['conversationId']: item for item in result['conversations']}
    assert set(entries) == {'worker-live', 'worker-idle'}
    assert entries['worker-live']['busy'] is True
    assert 'busy' not in entries['worker-idle']
    schema = json.loads((Path(__file__).resolve().parents[2] / 'contracts/project_coordination_v1.schema.json').read_text())
    jsonschema.validate(result, {'$ref': '#/$defs/conversationPage', '$defs': schema['$defs']})


def test_nonmatching_page_has_cursor_and_does_not_claim_complete():
    repo = repository(([row('z', '/other'), row('y', '/other')], True),
                      ([row('x')], False))
    first = conversation_page(7, '/project', repository=repo)
    assert not first['conversations'] and first['nextCursor'] and first['coverage'] == 'page'
    second = conversation_page(7, '/project/', first['nextCursor'], repository=repo)
    assert second['conversations'][0]['conversationId'] == 'x'
    assert second['coverage'] == 'complete'
    assert repo.list_catalog_page.call_count == 2
    assert repo.list_catalog_page.call_args.kwargs['before_updated_at'] == 20
    assert repo.list_catalog_page.call_args.kwargs['before_id'] == 'y'


def test_cursor_cannot_cross_project_or_owner():
    repo = repository(([row('z')], True))
    cursor = conversation_page(7, '/project', repository=repo)['nextCursor']
    for owner, project in [(8, '/project'), (7, '/other')]:
        with pytest.raises(ValueError, match='cursor'):
            conversation_page(owner, project, cursor, repository=repo)
    assert repo.list_catalog_page.call_count == 1


@pytest.mark.parametrize('cursor', ['%', 'e30', 'W10', 'x' * 1025, 42])
def test_invalid_cursor_refuses_before_storage(cursor):
    repo = repository()
    with pytest.raises(ValueError, match='cursor'):
        conversation_page(1, '/project', cursor, repository=repo)
    repo.list_catalog_page.assert_not_called()


def test_nonadvancing_authority_page_is_not_an_infinite_load_more_loop():
    repo = repository(([row('z')], True), ([row('z')], True))
    cursor = conversation_page(1, '/project', repository=repo)['nextCursor']
    with pytest.raises(RuntimeError, match='nonadvancing'):
        conversation_page(1, '/project', cursor, repository=repo)


def test_conversation_page_prefers_explicit_origin_to_assignment_backfill(monkeypatch):
    from lib.conversations import coordinator_conversations
    monkeypatch.setattr(coordinator_conversations, 'project_catalog_origins',
        lambda owner, project, **kwargs: {'legacy-worker': 'coordinator-root'})
    result = conversation_page(7, '/project', repository=repository(([
        row('legacy-worker'), row('worker-empty', _coordinatorSource='coordinator-explicit')], False)))
    assert [item['coordinatorId'] for item in result['conversations']] == [
        'coordinator-root', 'coordinator-explicit']


class Dispatch:
    """Owned segmented dispatch reads: one head, admission documents and their order index."""

    def __init__(self, *phases):
        self.phases, self.admitted, self.legacy = list(phases or ['ready']), 0, None
        self.documents, self.order, self.calls, self.during_get = {}, [], [], None

    def admit(self, ident, recipient, coordinator, **profile):
        self.admitted += 1
        request = {'recipientId': recipient, 'coordinatorId': coordinator, **({'profile': profile} if profile else {})}
        self.documents[key(ident)] = {'version': 1, 'value': {'sequence': self.admitted,
                                      'record': {'id': ident, 'request': request}}}
        self.order.append({'key': f'7:project:indexes:assignments:order:{self.admitted:020d}',
                           'version': 1, 'value': {'id': ident, 'sequence': self.admitted}})

    def repository(self, owner, project):
        assert (owner, project) == (7, '/project')
        return SimpleNamespace(owner=owner, project=project, storage_version=2, key='7:project',
                               binding={'owner': owner, 'projectPath': project},
                               client=lambda write=False: self, _check_record=lambda value: None)

    def query(self, operation, payload):
        collection = payload.get('collection')
        self.calls.append(collection or operation)
        if operation == 'record.get':
            return None if self.legacy is None else {'version': 1, 'value': deepcopy(self.legacy)}
        if collection == 'head':
            phase = self.phases.pop(0) if len(self.phases) > 1 else self.phases[0]
            counts = {**{field: 0 for field in FIELDS}, 'assignments': self.admitted}
            return None if phase is None else {'version': self.admitted + 1, 'value': {
                'format': SPEC['format'], 'owner': 7, 'projectPath': '/project', 'state': {
                    'format': FORMAT, 'phase': phase, 'nextSequence': self.admitted + 1, 'counts': counts}}}
        if collection == 'indexes':
            assert payload['prefix'] == 'assignments:order:' and not payload.get('reverse')
            return deepcopy([row for row in self.order if row['key'] > payload['after_key']][:payload['limit']])
        assert (operation, collection) == ('project.dispatch.get', 'assignments')
        during, self.during_get = self.during_get, None
        if during:
            during()
        return deepcopy(self.documents.get(payload['id']))


def read_origins(dispatch, memo=None):
    return project_catalog_origins(7, '/project/', repository_factory=dispatch.repository,
                                   recipient_origins=memo)


def test_catalog_origins_read_each_admission_once_and_keep_the_original_coordinator():
    dispatch, memo = Dispatch(), RecipientOrigins()
    dispatch.admit('a1', 'legacy-worker', 'coordinator-root')
    dispatch.admit('a2', 'coordinator-root', 'coordinator-root', mode='coordinator')
    dispatch.admit('a3', 'legacy-worker', 'later-coordinator')
    assert read_origins(dispatch, memo) == {'legacy-worker': 'coordinator-root'}
    assert dispatch.calls == ['head', 'indexes', 'assignments', 'assignments', 'assignments']
    dispatch.calls.clear()
    assert read_origins(dispatch, memo) == {'legacy-worker': 'coordinator-root'}
    assert dispatch.calls == ['head']
    dispatch.admit('a4', 'new-worker', 'later-coordinator')
    dispatch.calls.clear()
    assert read_origins(dispatch, memo) == {'legacy-worker': 'coordinator-root', 'new-worker': 'later-coordinator'}
    assert dispatch.calls == ['head', 'indexes', 'assignments']


def test_catalog_origins_page_the_order_index_by_its_storage_limit():
    dispatch = Dispatch()
    for n in range(SPEC['limits']['pageRows'] + 1):
        dispatch.admit(f'a{n}', f'worker-{n}', 'coordinator-root')
    assert len(read_origins(dispatch)) == SPEC['limits']['pageRows'] + 1
    assert dispatch.calls.count('indexes') == 2
    assert dispatch.calls.count('assignments') == SPEC['limits']['pageRows'] + 1


def test_a_writer_during_the_read_neither_retries_nor_fails_it():
    dispatch, memo = Dispatch(), RecipientOrigins()
    dispatch.admit('a1', 'legacy-worker', 'coordinator-root')
    dispatch.during_get = lambda: dispatch.admit('a2', 'racing-worker', 'coordinator-root')
    assert read_origins(dispatch, memo) == {'legacy-worker': 'coordinator-root'}
    assert dispatch.calls == ['head', 'indexes', 'assignments']
    dispatch.calls.clear()
    assert read_origins(dispatch, memo) == {'legacy-worker': 'coordinator-root', 'racing-worker': 'coordinator-root'}
    assert dispatch.calls == ['head', 'indexes', 'assignments']


def test_a_replaced_history_or_storage_client_starts_over():
    dispatch, memo = Dispatch(), RecipientOrigins()
    dispatch.admit('a1', 'worker-a', 'coordinator-old')
    dispatch.admit('a2', 'worker-b', 'coordinator-old')
    assert read_origins(dispatch, memo) == {'worker-a': 'coordinator-old', 'worker-b': 'coordinator-old'}
    other = Dispatch()
    other.admit('b1', 'worker-c', 'coordinator-new')
    other.admit('b2', 'worker-d', 'coordinator-new')
    assert read_origins(other, memo) == {'worker-c': 'coordinator-new', 'worker-d': 'coordinator-new'}
    other.documents, other.order, other.admitted = {}, [], 0
    other.admit('c1', 'worker-e', 'coordinator-restored')
    assert read_origins(other, memo) == {'worker-e': 'coordinator-restored'}


@pytest.mark.parametrize('damage', ['index', 'document', 'sequence', 'provenance', 'profile'])
def test_missing_or_changed_admission_evidence_is_an_integrity_failure(damage):
    dispatch = Dispatch()
    dispatch.admit('a1', 'worker', 'coordinator-root')
    stored = dispatch.documents[key('a1')]['value']
    if damage == 'index':
        dispatch.order.clear()
    elif damage == 'document':
        dispatch.documents.clear()
    elif damage == 'sequence':
        stored['sequence'] = 2
    elif damage == 'provenance':
        stored['record']['request']['recipientId'] = ''
    else:
        stored['record']['request']['profile'] = None
    with pytest.raises(StorageError) as error:
        read_origins(dispatch)
    assert error.value.code == 'database_integrity'


@pytest.mark.parametrize('phase', [None, 'copying'])
def test_legacy_record_is_read_whole_until_the_cutover_completes(phase):
    dispatch, memo = Dispatch(phase), RecipientOrigins()
    dispatch.legacy = {'binding': {'owner': 7, 'projectPath': '/project'}, 'assignments': [
        {'request': {'recipientId': 'worker', 'coordinatorId': 'coordinator-root'}},
        {'request': {'recipientId': 'worker', 'coordinatorId': 'later-coordinator'}},
        {'request': {'recipientId': 'coordinator-root', 'coordinatorId': 'x', 'profile': {'mode': 'coordinator'}}}]}
    for _ in range(2):
        assert read_origins(dispatch, memo) == {'worker': 'coordinator-root'}
    assert dispatch.calls == ['head', 'record.get'] * 2


def test_a_reclaimed_frozen_source_follows_the_completed_cutover():
    dispatch = Dispatch('copying', 'ready')
    dispatch.admit('a1', 'worker', 'coordinator-root')
    dispatch.legacy = {'format': FORMAT, 'binding': {'owner': 7, 'projectPath': '/project'}, 'original': None}
    assert read_origins(dispatch) == {'worker': 'coordinator-root'}
    assert dispatch.calls == ['head', 'record.get', 'head', 'indexes', 'assignments']


def test_shared_origins_are_read_only():
    dispatch = Dispatch()
    dispatch.admit('a1', 'worker', 'coordinator-root')
    with pytest.raises(TypeError):
        read_origins(dispatch, RecipientOrigins())['worker'] = 'other'


def test_authority_failure_is_not_empty_success():
    repo = repository()
    repo.list_catalog_page.side_effect = RuntimeError('unavailable')
    with pytest.raises(RuntimeError, match='unavailable'):
        conversation_page(1, '/project', repository=repo)


@pytest.mark.parametrize('items,has_more', [
    ([], False), ([row('explicit', _coordinatorSource='coordinator-root')], False),
    ([row('other', '/other'), row('archived', archived=True), row('readonly', readOnly=True)], True),
    ([row('invalid-source', _coordinatorSource=42)], False),
    ([row('overlong-source', _coordinatorSource='x' * 257)], False),
])
def test_page_without_missing_origin_does_not_depend_on_history(monkeypatch, items, has_more):
    from lib.conversations import coordinator_conversations
    history = Mock(side_effect=RuntimeError('History authority unavailable'))
    monkeypatch.setattr(coordinator_conversations, 'project_catalog_origins', history)
    result = conversation_page(7, '/project/', repository=repository((items, has_more)))
    assert result['coverage'] == ('page' if has_more else 'complete')
    assert bool(result['nextCursor']) is has_more
    history.assert_not_called()
    if result['conversations']:
        source = 'coordinator-root' if items[0].metadata['id'] == 'explicit' else None
        assert result['conversations'][0]['coordinatorId'] == source


def test_mixed_page_backfills_once_after_project_visibility_filter(monkeypatch):
    from lib.conversations import coordinator_conversations
    history = Mock(return_value={'explicit': 'stale-coordinator', 'legacy-a': 'older-coordinator',
                                 'legacy-b': 'older-coordinator'})
    monkeypatch.setattr(coordinator_conversations, 'project_catalog_origins', history)
    items = [row('explicit', _coordinatorSource='current-coordinator'), row('legacy-a'),
             row('legacy-b'), row('ordinary'), row('other', '/other'),
             row('archived', archived=True), row('readonly', readOnly=True)]
    memo = RecipientOrigins()
    result = conversation_page(7, '/project/', repository=repository((items, False)), recipient_origins=memo)
    assert [(item['conversationId'], item['coordinatorId']) for item in result['conversations']] == [
        ('explicit', 'current-coordinator'), ('legacy-a', 'older-coordinator'),
        ('legacy-b', 'older-coordinator'), ('ordinary', None)]
    history.assert_called_once_with(7, '/project', recipient_origins=memo)


def test_missing_origin_still_propagates_history_failure(monkeypatch):
    from lib.conversations import coordinator_conversations
    history = Mock(side_effect=RuntimeError('History authority unavailable'))
    monkeypatch.setattr(coordinator_conversations, 'project_catalog_origins', history)
    with pytest.raises(RuntimeError, match='History authority unavailable'):
        conversation_page(7, '/project', repository=repository(([row('legacy')], False)))
    history.assert_called_once_with(7, '/project', recipient_origins=None)


def test_explicit_origin_map_remains_the_complete_backfill_port(monkeypatch):
    from lib.conversations import coordinator_conversations
    history = Mock(side_effect=AssertionError('Unexpected history read'))
    monkeypatch.setattr(coordinator_conversations, 'project_catalog_origins', history)
    result = conversation_page(7, '/project', origins={},
                               repository=repository(([row('ordinary')], False)))
    assert result['conversations'][0]['coordinatorId'] is None
    history.assert_not_called()


@pytest.mark.parametrize('owner,project', [(0, '/project'), (True, '/project'),
                                         ('7', '/project'), (7, '')])
def test_invalid_scope_refuses_before_any_inventory_read(monkeypatch, owner, project):
    from lib.conversations import coordinator_conversations
    history = Mock()
    monkeypatch.setattr(coordinator_conversations, 'project_catalog_origins', history)
    repo = repository()
    with pytest.raises(ValueError):
        conversation_page(owner, project, repository=repo)
    repo.list_catalog_page.assert_not_called()
    history.assert_not_called()
