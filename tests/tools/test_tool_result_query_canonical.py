"""Canonical query authority tests with an isolated SQLite session adapter."""
import json
import sqlite3
from uuid import uuid4
import pytest
from lib.storage_authority_sql.operations_pkg._artifacts import (
    _tool_result_artifact_put as put, _tool_result_artifact_read as read,
    _tool_result_artifact_search as search)
from lib.storage_authority_sql.operations_pkg._tool_result_query import MEDIA_TYPE

pytestmark = pytest.mark.unit

class Session:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
    def lock_key(self, *args):
        pass
    def execute(self, sql, args=()):
        result = self.db.execute(sql, args)
        return result.rowcount
    def fetch_one(self, sql, args=()):
        return self.db.execute(sql, args).fetchone()
    def fetch_all(self, sql, args=()):
        return self.db.execute(sql, args).fetchall()

@pytest.fixture
def store(tmp_path):
    s = Session(tmp_path / 'authority.db')
    s.execute('CREATE TABLE storage_records(namespace TEXT,record_key TEXT,value_json TEXT,version INT,updated_at_ms INT,PRIMARY KEY(namespace,record_key))')
    s.execute('CREATE TABLE tool_result_artifacts(user_id INT,content_sha256 TEXT,content TEXT,media_type TEXT,size_bytes INT,created_at_ms INT,expires_at_ms INT,last_accessed_at_ms INT,PRIMARY KEY(user_id,content_sha256))')
    yield s
    s.db.close()

def save(s, text, owner=1, media='text/plain', expires=1000):
    with s.db:
        return put(s, dict(user_id=owner,content=text,media_type=media,created_at_ms=1,expires_at_ms=expires))['artifactRef']

def args(ref, **kw):
    return dict(user_id=1, artifact_ref=ref, now_ms=2, **kw)

def manifest(s, parts, children=(), **kw):
    return save(s,json.dumps(dict(resultId=str(uuid4()),executionStatus='success',sourceCoverage='complete',retentionCoverage='complete',parts=parts,children=list(children))),media=MEDIA_TYPE, **kw)

def test_exact_unicode_ranges_and_restart_cursor(store, tmp_path):
    text='ß中🙂é\n' * 12
    ref=save(store,text)
    page=read(store,args(ref,limit=7))
    token=page['nextCursor']
    other=Session(tmp_path/'authority.db')
    output=page['content']
    while token:
        page=read(other,args(ref,cursor=token,limit=7))
        output+=page['content']; token=page['nextCursor']
    other.db.close()
    assert output.encode()==text.encode()

def test_unicode_adjacent_and_cursor_scope(store):
    ref=save(store,'ßSSsss 中中')
    page=search(store,args(ref,query='ss',limit=1))
    assert page['items'][0]['offset']==0
    token=page['nextCursor']
    assert search(store,args(ref,query='ss',cursor=token,limit=20))['items'][0]['offset']==1
    for payload, op in [(args(ref,query='xx',cursor=token),search),(args(ref,cursor=token),read),(args(ref,query='ss',cursor=token+'x'),search),(args(ref,query='ss',cursor='2'),search)]:
        with pytest.raises(Exception): op(store,payload)
    with pytest.raises(Exception): search(store,{**args(ref,query='ss',cursor=token),'user_id':2})

def test_transparent_children_owner_checks_and_forged_markers(store):
    child=save(store,'needle needle')
    secret=save(store,'secret needle',owner=2)
    root=manifest(store,[{'kind':'text','text':'header'}],[{'name':'tool','callId':'one','artifactRef':child},{'name':'tool','callId':'two','artifactRef':secret}])
    page=search(store,args(root,query='needle'))
    assert len(page['items'])==2
    assert page['items'][0]['path'][0]['callId']=='one'
    assert page['coverage']=='incomplete'
    fake=save(store,json.dumps({'artifactRef':secret}))
    assert search(store,args(fake,query='secret'))['items']==[]
    assert read(store,{**args(child),'user_id':2}) is None

def test_manifest_identity_selector_and_expiry(store):
    parts=[{'kind':'json','text':'{ "a/b": {"~": [1,2]} }'}]
    first=manifest(store,parts); second=manifest(store,parts)
    # Capture metadata differs, while the canonical content identity is shared.
    assert first == second
    assert manifest(store, [{"kind": "text", "text": "different"}]) != first
    assert read(store,args(first,selector='/a~1b/~0/1'))['content']=='2'
    assert read(store,args(first))['content']==parts[0]['text']
    assert read(store,{**args(first),'now_ms':1000}) is None
    with pytest.raises(Exception): read(store,args('tool-result:forged'))

def test_quota_and_storage_failure_are_explicit(store, monkeypatch):
    save(store,'existing')
    store.execute('UPDATE tool_result_artifacts SET size_bytes=?',(64*1024*1024,))
    with pytest.raises(Exception,match='quota'): save(store,'new body')
    def fail(*a): raise RuntimeError('offline')
    monkeypatch.setattr(store,'fetch_one',fail)
    with pytest.raises(RuntimeError,match='offline'): read(store,args('tool-result:'+'a'*64))

def test_expired_child_is_not_exhaustive_zero(store):
    child=save(store,'gone',expires=2)
    root=manifest(store,[],[{'name':'x','callId':'expired','artifactRef':child}])
    result=search(store,args(root,query='gone'))
    assert result['items']==[] and result['coverage']=='incomplete'


def test_forced_scan_budget_keeps_later_routes_reachable(store, monkeypatch):
    import lib.storage_authority_sql.operations_pkg._tool_result_query as q
    children = [dict(name='leaf',callId=str(i),artifactRef=save(store,'needle'+str(i))) for i in range(10)]
    root = manifest(store, [], children)
    root_size = store.fetch_one('SELECT size_bytes FROM tool_result_artifacts WHERE content_sha256=?', (root.split(':')[1],))['size_bytes']
    monkeypatch.setattr(q, 'MAX_BYTES', root_size + 12)
    cursor, seen, found = 0, set(), []
    for _ in range(20):
        page = search(store,args(root,query='needle',cursor=cursor,limit=20))
        found.extend(page['items'])
        cursor = page['nextCursor']
        if cursor is None: break
        assert cursor not in seen
        seen.add(cursor)
    else: pytest.fail('scan budget loop')
    assert len(found) == 10


def test_seek_does_not_load_completed_sibling_bodies(store, monkeypatch):
    children = [dict(name='leaf',callId=str(i),artifactRef=save(store,'needle'+str(i))) for i in range(80)]
    root = manifest(store, [], children)
    cursor = 0
    for _ in range(75):
        page = search(store,args(root,query='needle',cursor=cursor,limit=1))
        cursor = page['nextCursor']
    calls = []
    fetch = store.fetch_one
    def traced(sql, params=()):
        calls.append(sql)
        return fetch(sql,params)
    monkeypatch.setattr(store,'fetch_one',traced)
    page = search(store,args(root,query='needle',cursor=cursor,limit=1))
    assert page['items'][0]['path'][0]['callId'] == '75'
    assert len(calls) < 12


def test_depth_and_no_progress_deadline_are_explicit(store, monkeypatch):
    import lib.storage_authority_sql.operations_pkg._tool_result_query as q
    root = save(store,'needle')
    for _ in range(10):
        root = manifest(store,[],[dict(name='n'*200,callId='c'*200,artifactRef=root)])
    page = search(store,args(root,query='needle'))
    assert any(x['reason']=='depth_budget' for x in page['unavailable'])
    assert page['nextCursor'] is None
    leaf = save(store,'needle')
    monkeypatch.setitem(q.LIMITS,'scanSeconds',0)
    from lib.agent_core.storage_errors import StorageError
    with pytest.raises(StorageError, match='progress') as failure:
        search(store,args(leaf,query='needle'))
    assert failure.value.code == 'tool_result_query_budget_exhausted'
    assert failure.value.retryable is True


@pytest.mark.parametrize('change', [
    {'parts':{}}, {'children':'bad'}, {'executionStatus':False},
    {'sourceCoverage':{}}, {'parts':[{'kind':'json','text':'{'}]},
    {'parts':[{'kind':'text','text':'ok','unexpected':1}]},
    {'parts':[{'kind':'text','text':'ok','sourceOrdinal':True}]},
    {'parts':[{'kind':'text','text':'x'}]*129},
])
def test_manifest_validation_never_publishes_partial_success(store, change):
    value = dict(resultId=str(uuid4()),executionStatus='ok',sourceCoverage='complete',retentionCoverage='complete',parts=[],children=[])
    value.update(change)
    before = store.fetch_one('SELECT COUNT(*) AS n FROM tool_result_artifacts')['n']
    with pytest.raises(Exception): save(store,json.dumps(value),media=MEDIA_TYPE)
    assert store.fetch_one('SELECT COUNT(*) AS n FROM tool_result_artifacts')['n'] == before


@pytest.mark.parametrize('query, tail', [('needle', 'needle' + 'x' * 3), ('sss', 'ssss')])
def test_window_overlap_neither_duplicates_nor_drops_matches(store, query, tail):
    from lib.storage_authority_sql.operations_pkg._tool_result_query import WINDOW
    text = 'x' * (WINDOW - len(tail)) + tail + 'needle' + 'ssss'
    ref = save(store, text)
    expected = [i for i in range(len(text)) if text.startswith(query, i)]
    for page_limit in (1, 20):
        cursor, found = 0, []
        for _ in range(30):
            page = search(store, args(ref, query=query, cursor=cursor, limit=page_limit))
            found.extend(row['offset'] for row in page['items'])
            cursor = page['nextCursor']
            if cursor is None:
                break
        else:
            pytest.fail('window boundary search did not finish')
        assert found == expected


def test_utf8_page_yields_instead_of_rescanning_a_codepoint_that_does_not_fit(store, monkeypatch):
    ref = save(store, 'ß中🙂é\n' * 100)
    reads = []
    original = store.fetch_one
    def tracked(sql, parameters=()):
        if 'substr(content' in sql:
            reads.append(parameters)
        return original(sql, parameters)
    monkeypatch.setattr(store, 'fetch_one', tracked)
    page = read(store, args(ref, limit=37))
    assert page['content'] == 'ß中🙂é\n' * 3
    assert page['nextCursor'] and len(reads) <= 2
