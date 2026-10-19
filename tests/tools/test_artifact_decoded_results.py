"""Producer declarations match real bounded queries, including failure and batching."""
import json
import sqlite3
import threading
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator

from lib.storage_authority_sql.operations_pkg._artifacts import (
    _tool_result_artifact_put, _tool_result_artifact_read, _tool_result_artifact_search)
from lib.tools.tool_result_artifacts import READ_TOOL_ARTIFACT, SEARCH_TOOL_ARTIFACT
from lib.tool_results.tool_result_query_contract import MEDIA_TYPE

pytestmark = pytest.mark.unit


class Session:
    """Isolated session adapter for the production repository operations."""
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.RLock()
        self.db.row_factory = sqlite3.Row

    def lock_key(self, *args):
        pass

    def execute(self, sql, args=()):
        return self.db.execute(sql, args).rowcount

    def fetch_one(self, sql, args=()):
        return self.db.execute(sql, args).fetchone()

    def fetch_all(self, sql, args=()):
        return self.db.execute(sql, args).fetchall()


@pytest.fixture
def artifact_store(tmp_path):
    session = Session(tmp_path / 'artifacts.db')
    session.execute('CREATE TABLE storage_records(namespace TEXT,record_key TEXT,value_json TEXT,version INT,updated_at_ms INT,PRIMARY KEY(namespace,record_key))')
    session.execute('CREATE TABLE tool_result_artifacts(user_id INT,content_sha256 TEXT,content TEXT,media_type TEXT,size_bytes INT,created_at_ms INT,expires_at_ms INT,last_accessed_at_ms INT,PRIMARY KEY(user_id,content_sha256))')
    yield session
    session.db.close()


def save(store, text, *, owner=17, canonical=False, source_coverage="complete"):
    if canonical:
        text = json.dumps({'resultId': str(uuid4()), 'executionStatus': 'success',
            'sourceCoverage': source_coverage, 'retentionCoverage': 'complete',
            'parts': [{'kind': 'text', 'text': text}], 'children': []})
    return _tool_result_artifact_put(store, {'user_id': owner, 'content': text,
        'media_type': MEDIA_TYPE if canonical else 'text/plain',
        'created_at_ms': 1, 'expires_at_ms': 1000})['artifactRef']


def invoke(monkeypatch, store, name, arguments, *, owner=17):
    from lib.tasks_pkg.handlers import tool_result_artifacts as handler

    class Repository:
        def read_range(self, **values):
            with store.lock:
                return _tool_result_artifact_read(store, {**values, 'now_ms': 2})

        def search(self, **values):
            with store.lock:
                return _tool_result_artifact_search(store, {**values, 'now_ms': 2})

    monkeypatch.setattr(handler, 'ToolResultArtifactRepository', Repository)
    monkeypatch.setattr(handler, 'task_user_id', lambda task: owner)
    monkeypatch.setattr(handler, '_finalize_tool_round', lambda *args, **kwargs: None)
    _, output, _ = handler._handle_tool_result_artifact(
        {'model': ''}, {}, name, 'artifact-test', arguments, 1, {}, {}, '', False)
    result = json.loads(output)
    contract = READ_TOOL_ARTIFACT if name == 'read_tool_artifact' else SEARCH_TOOL_ARTIFACT
    Draft202012Validator(contract.decoded_result_schema).validate(result)
    return result


@pytest.mark.parametrize('canonical', [False, True])
@pytest.mark.parametrize('operation', ['read_tool_artifact', 'search_tool_artifact'])
def test_declared_single_and_indexed_batch_values_match_production(monkeypatch, artifact_store, canonical, operation):
    first = save(artifact_store, 'needle 中🙂 needle', canonical=canonical)
    second = save(artifact_store, 'other needle', canonical=canonical)
    values = {'artifact_ref': first, 'limit': 4 if operation == 'read_tool_artifact' else 1}
    if operation == 'search_tool_artifact':
        values['query'] = 'needle'
    page = invoke(monkeypatch, artifact_store, operation, values)
    assert page['status'] == 'ok' and page['coverage'] == 'incomplete'
    assert page['nextCursor'] and page['truncated'] is True
    terminal = invoke(monkeypatch, artifact_store, operation, {
        **values, 'cursor': page['nextCursor'], 'limit': 65536 if operation == 'read_tool_artifact' else 20})
    assert terminal['nextCursor'] is None and terminal['coverage'] == 'complete'
    batch_key = 'reads' if operation == 'read_tool_artifact' else 'searches'
    batch = invoke(monkeypatch, artifact_store, operation, {batch_key: [
        {**values, 'limit': 20}, {**values, 'artifact_ref': second, 'limit': 20}]})
    assert batch['status'] == 'ok'
    assert [item['index'] for item in batch['items']] == [0, 1]


@pytest.mark.parametrize('operation', ['read_tool_artifact', 'search_tool_artifact'])
def test_declared_refusal_and_partial_failure_preserve_owner_boundary(monkeypatch, artifact_store, operation):
    own = save(artifact_store, 'needle')
    foreign = save(artifact_store, 'secret needle', owner=18)
    values = {'artifact_ref': foreign}
    if operation == 'search_tool_artifact': values['query'] = 'needle'
    failed = invoke(monkeypatch, artifact_store, operation, values)
    assert failed['status'] == 'error' and failed['error']['code'] == 'artifact_unavailable'
    batch_key = 'reads' if operation == 'read_tool_artifact' else 'searches'
    batch = invoke(monkeypatch, artifact_store, operation, {batch_key: [
        {**values, 'artifact_ref': own}, values]})
    assert batch['status'] == 'partial_failure'
    assert batch['items'][0]['status'] == 'ok' and batch['items'][1]['status'] == 'error'
    assert 'secret' not in json.dumps(batch)


@pytest.mark.parametrize("source_coverage", ["complete", "partial"])
def test_real_vm_follows_query_cursors_without_inventing_source_completeness(monkeypatch, artifact_store, source_coverage):
    from lib.tools.javascript import execute_javascript_program
    from lib.tools.result_value import CanonicalReceipt
    records = [{'amount': n + 3, 'note': '中🙂'} for n in range(67)]
    ref = save(artifact_store, json.dumps(records, ensure_ascii=False), canonical=True, source_coverage=source_coverage)
    calls = []

    def call(name, arguments, call_id=None):
        READ_TOOL_ARTIFACT.validate_arguments(arguments)
        page = invoke(monkeypatch, artifact_store, name, arguments)
        calls.append((dict(arguments), page))
        return CanonicalReceipt({'status': 'done', 'output': json.dumps(page),
            'output_truncated': False}, canonical={'owned': True})

    source = 'const ref=' + json.dumps(ref) + ''';
    let cursor="0", text="", page;
    do {
      page=await tools.invoke("read_tool_artifact",{artifact_ref:ref,cursor,limit:512});
      if(page.status!=="ok") tools.observe("artifact unavailable",page);
      text+=page.content; cursor=page.nextCursor;
    } while(cursor!==null);
    if(page.coverage!=="complete") tools.observe("source incomplete",page);
    const rows=JSON.parse(text);
    return {count:rows.length,total:rows.reduce((sum,row)=>sum+row.amount,0)};
    '''
    result, stats = execute_javascript_program(source, search=lambda *args: [], call=call,
        call_many=lambda items, execution: [call(item['name'], item['arguments']) for item in items])
    if source_coverage == 'complete':
        assert result == {'count': 67, 'total': sum(row['amount'] for row in records)}
        assert stats['outcome'] == 'completed'
    else:
        assert result['kind'] == 'observation' and result['reason'] == 'source incomplete'
        assert stats['outcome'] == 'needs_model'
    assert len(calls) > 1 and stats['tool_calls'] == len(calls)
    assert calls[0][0]['cursor'] == '0'
    assert all(current[0]['cursor'] == previous[1]['nextCursor']
               for previous, current in zip(calls, calls[1:]))
    assert calls[-1][1]['nextCursor'] is None
    assert calls[-1][1]['coverage'] == ('complete' if source_coverage == 'complete' else 'incomplete')


def test_declared_alternatives_remain_visible_in_code_api_and_exact_schema():
    from lib.tools.code_api import _type
    for contract in (READ_TOOL_ARTIFACT, SEARCH_TOOL_ARTIFACT):
        declared = contract.decoded_result_schema
        signature = _type(declared)
        assert '"nextCursor"' in signature and '"coverage"' in signature
        assert '"index"' in signature and '"error"' in signature
        assert '| null' in signature
        assert contract.search_document()['decoded_result_schema'] == declared


def test_real_artifact_json_text_keeps_integer_ids_and_aggregates_exact(monkeypatch, artifact_store):
    from lib.tools.javascript import execute_javascript_program
    from lib.tools.result_value import CanonicalReceipt

    records = [{'id': 9223372036854775807, 'amount': 9007199254740993},
               {'id': -9223372036854775807, 'amount': 17}]
    ref = save(artifact_store, json.dumps(records), canonical=True)
    reads, selected = [], []

    def call(name, arguments, call_id=None):
        if name == 'read_tool_artifact':
            READ_TOOL_ARTIFACT.validate_arguments(arguments)
            value = invoke(monkeypatch, artifact_store, name, arguments)
            reads.append((dict(arguments), value))
        else:
            assert name == 'select_record'
            selected.append(arguments['id'])
            value = {'id': arguments['id']}
        return CanonicalReceipt({'status': 'done', 'output': json.dumps(value),
                                 'output_truncated': False}, canonical={'owned': True})

    source = 'const ref=' + json.dumps(ref) + ''';
      let text='',cursor='0',page;
      do {
        page=await tools.read_tool_artifact({artifact_ref:ref,cursor,limit:32});
        if(page.status!=='ok') tools.observe('artifact unavailable',page);
        text+=page.content; cursor=page.nextCursor;
      } while(cursor!==null);
      if(page.coverage!=='complete') tools.observe('source incomplete',page);
      const rows=tools.parseJSON(text);
      const selected=await tools.invoke('select_record',{id:rows[0].id});
      return {count:rows.length,id:String(selected.id),
        total:String(rows.reduce((sum,row)=>sum+BigInt(row.amount),0n))};'''
    value, stats = execute_javascript_program(source, search=lambda *args: [], call=call,
        call_many=lambda calls, execution: pytest.fail('Sequential cursor chain was batched'))
    assert value == {'count': 2, 'id': str(records[0]['id']),
                     'total': str(sum(row['amount'] for row in records))}
    assert selected == [records[0]['id']]
    assert len(reads) > 1 and stats['tool_calls'] == len(reads) + 1
    assert all(current[0]['cursor'] == previous[1]['nextCursor']
               for previous, current in zip(reads, reads[1:]))
    assert reads[-1][1]['coverage'] == 'complete'


@pytest.mark.parametrize("composition", ["anyOf", "oneOf", "allOf"])
def test_convenience_composition_does_not_replace_exact_constraints(composition):
    from lib.tools.code_api import _type
    from lib.tools.contracts import ToolContractV2
    parts = [{"type": "string", "minLength": 2}, {"type": "integer", "minimum": 4}]
    if composition == "allOf":
        parts = [{"type": "object", "properties": {"value": parts[0]}},
                 {"type": "object", "properties": {"count": parts[1]}}]
    declaration = {composition: parts}
    contract = ToolContractV2("composed", {}, "Read", decoded_result_schema=declaration)
    text = _type(contract.decoded_result_schema)
    assert "string" in text and "number | bigint" in text
    assert (" & " if composition == "allOf" else " | ") in text
    assert contract.search_document()["decoded_result_schema"] == declaration
    assert _type({composition: [None]}) == "unknown"
    if composition != "allOf":
        assert _type({composition: [{"type": "string"}, {}]}) == "unknown"
