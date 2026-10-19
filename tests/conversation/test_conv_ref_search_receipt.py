"""Search reference receipt and paginated coverage outcomes (no real storage)."""
import pytest
from tests.conversation.test_conv_ref_sidecar_projection_guard import _FakeStorageClient, _doc, _run

pytestmark = pytest.mark.unit

class PagedClient(_FakeStorageClient):
    def __init__(self, documents, pages):
        super().__init__(documents)
        self.pages = iter(pages)
    def query(self, operation, payload=None):
        if operation == 'conversation.search':
            self.calls.append((operation, dict(payload or {})))
            return next(self.pages)
        return super().query(operation, payload)


def page(items=(), cursor=None, coverage='ready'):
    return {'items': list(items), 'next_cursor': cursor, 'incomplete': bool(cursor),
            'coverage': coverage, 'coverage_reasons': []}


def test_empty_page_continues_and_receipt_has_actual_scope(monkeypatch):
    client = PagedClient([_doc('hit', 'Past work', '/project')],
                        [page(cursor='next'), page([{'id': 'hit'}])])
    out = _run(monkeypatch, client, keyword=' test_opencode_exit_status.py ', project_path='/project')
    assert 'hit' in out
    receipt = out.conversation_search
    assert receipt['keyword'] == 'test_opencode_exit_status.py'
    assert receipt['scope'] == 'project' and receipt['returnedCount'] == 1
    assert receipt['incomplete'] is False
    assert [p['cursor'] for op, p in client.calls if op == 'conversation.search'] == [None, 'next']
    assert all(p.get('include_messages') is False for op, p in client.calls if op == 'conversation.list')


def test_other_projects_do_not_starve_later_page(monkeypatch):
    client = PagedClient([_doc('other', 'Elsewhere', '/other'), _doc('hit', 'Past work', '/project')],
                        [page([{'id': 'other'}], 'next'), page([{'id': 'hit'}])])
    out = _run(monkeypatch, client, keyword='needle', project_path='/project')
    assert out.conversation_search['items'] == [{'id': 'hit', 'title': 'Past work'}]


def test_all_scope_does_not_advise_all_again(monkeypatch):
    out = _run(monkeypatch, PagedClient([], [page()]), keyword='needle', scope='all')
    assert 'No conversations found' in out
    assert "scope='all'" not in out
    assert out.conversation_search['coverage'] == 'ready'


@pytest.mark.parametrize('coverage', ['partial', 'indexing', 'unknown'])
def test_incomplete_coverage_is_not_absence(monkeypatch, coverage):
    out = _run(monkeypatch, PagedClient([], [page(coverage=coverage)]), keyword='needle')
    assert 'No conversations found' not in out
    assert 'not proof of absence' in out


def test_four_page_bound_reports_incomplete(monkeypatch):
    out = _run(monkeypatch, PagedClient([], [page(cursor=str(i)) for i in range(4)]), keyword='needle')
    assert out.conversation_search['incomplete']
    assert 'No conversations found' not in out


@pytest.mark.parametrize('bad', [None, {}, False, [], {'items': [], 'next_cursor': None}])
def test_bad_protocol_is_error_not_empty(monkeypatch, bad):
    with pytest.raises(RuntimeError, match='malformed'):
        _run(monkeypatch, PagedClient([], [bad]), keyword='needle')


def test_explicit_project_without_context_fails(monkeypatch):
    with pytest.raises(ValueError, match='project context'):
        _run(monkeypatch, PagedClient([], []), scope='project')


def test_receipt_survives_copy_and_handler_metadata(monkeypatch):
    import copy
    import json
    from lib.conv_ref._query import ConversationListing
    from lib.tasks_pkg.handlers.misc import _brain
    receipt = {'keyword': 'needle', 'scope': 'all', 'returnedCount': 0, 'items': []}
    output = ConversationListing('No conversations found', receipt)
    assert copy.deepcopy(output).conversation_search == receipt
    assert json.loads(json.dumps(output)) == 'No conversations found'
    monkeypatch.setattr('lib.tasks_pkg.manager.task_user_id', lambda task: 7)
    monkeypatch.setattr(_brain, 'execute_conv_ref_tool', lambda *a, **kw: output)
    captured = {}
    def adapter(task, name, args, *a, executor, post_build, **kw):
        value = executor(name, args)
        post_build(captured, value, args)
        return value
    monkeypatch.setattr(_brain, 'simple_call', adapter)
    result = _brain._handle_conv_ref_tool({'convId': 'self'}, None,
        'list_conversations', 'call', {'keyword': 'needle'}, 1, {}, {}, None, False)
    assert result == output
    assert captured['conversationSearch'] == receipt
