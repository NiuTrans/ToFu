"""Budget planning keeps source scope and replay on both storage authorities."""
from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from lib.tasks_pkg.handlers import tool_result_artifacts as handler
from tests.tools.test_tool_result_query_backends import (
    repo as repo, storage as storage, manifest,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('alias', [False, True])
@pytest.mark.parametrize('operation', ['read_tool_artifact', 'search_tool_artifact'])
def test_planned_page_keeps_alias_scope_and_replays_after_restart(
        repo, storage, monkeypatch, alias, operation):
    is_read = operation == 'read_tool_artifact'
    if is_read:
        reference = manifest(repo, [{'kind': 'json', 'text': json.dumps({
            'selected': 'precise 中🙂 evidence ' * 420,
            'excluded': 'must stay outside the selector'}, ensure_ascii=False)}], ttl=120000)
        scope = {'selector': '/selected'}
    else:
        reference = manifest(repo, [{'kind': 'text', 'text':
            f'part {i} needle ' + 'distinct evidence 中🙂 ' * 35 + '\n'}
            for i in range(20)], ttl=120000)
        scope = {'query': 'needle'}
    source_query = repo.read_range if is_read else repo.search
    first = source_query(user_id=1, artifact_ref=reference,
                         limit=17 if is_read else 1, **scope)
    cursor_key = 'offset' if is_read else 'cursor'
    original = source_query(user_id=1, artifact_ref=reference,
        limit=65536 if is_read else 20, **{cursor_key: first['nextCursor']}, **scope)
    handle = 'tr:read:' + reference[-8:] if alias else reference
    task = {'_userId': 1, '_artifactAliases': {handle: reference}}
    arguments = {'artifact_ref': handle, 'cursor': first['nextCursor'],
                 'limit': 65536 if is_read else 20, **scope}
    calls, pages, snapshots = [], [], []

    def query(**values):
        calls.append(dict(values))
        page = source_query(**values)
        pages.append(page)
        snapshots.append(deepcopy(page))
        return page

    monkeypatch.setattr(handler, '_count_tokens', lambda text, model: len(text.encode()))
    result = handler._execute_one(operation, arguments, user_id=1, task=task,
        repository=SimpleNamespace(read_range=query, search=query), token_budget=2000)

    assert result['status'] == 'ok'
    assert len(calls) <= 3, 'Planning must avoid repeated halving queries on regular pages'
    assert handler._model_tokens(operation, result, '') <= 2000
    # Whole search hits are indivisible; alias-qualified source references can
    # leave more unused space than a read's codepoint-sized steps.
    assert len(result['content'].encode()) >= 1000 if is_read else len(result['items']) >= 2
    assert result == {'status': 'ok', **pages[-1], 'artifactRef': handle}
    assert pages == snapshots
    assert all(values['user_id'] == 1 and values['artifact_ref'] == reference
               and values[cursor_key] == first['nextCursor']
               and all(values[key] == value for key, value in scope.items()) for values in calls)
    assert result['nextCursor'].startswith('trq3.')
    storage.stop()
    storage.start()
    rest = source_query(user_id=1, artifact_ref=reference, **scope,
        **{cursor_key: result['nextCursor']}, limit=65536 if is_read else 20)
    field = 'content' if is_read else 'items'
    assert result[field] + rest[field] == original[field]
    replay = source_query(user_id=1, artifact_ref=reference, **scope,
        **{cursor_key: result['resumeCursor']}, limit=calls[-1]['limit'])
    assert replay == pages[-1]
