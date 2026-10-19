"""Passive roster reads must discover empty shells without starting work."""
from types import SimpleNamespace
import json
from pathlib import Path
from unittest.mock import Mock

import jsonschema
import pytest

from lib.conversations.coordinator_conversations import PAGE_SIZE, conversation_page

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
        {'conversationId': 'worker-empty', 'title': 'worker-empty', 'turnCount': 0, 'coordinatorId': 'coordinator-root'},
        {'conversationId': 'ordinary', 'title': 'ordinary', 'turnCount': 2, 'coordinatorId': None}],
        'nextCursor': None, 'coverage': 'complete'}
    args = repo.list_catalog_page.call_args.kwargs
    assert args['user_id'] == 7 and args['limit'] == PAGE_SIZE
    assert 'include_messages' not in args
    assert set(args['settings_keys']) == {'projectPath', '_coordinatorSource', 'archived', 'readOnly'}
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


def test_authority_failure_is_not_empty_success():
    repo = repository()
    repo.list_catalog_page.side_effect = RuntimeError('unavailable')
    with pytest.raises(RuntimeError, match='unavailable'):
        conversation_page(1, '/project', repository=repo)
