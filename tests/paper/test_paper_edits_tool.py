"""Agent paper_propose_edits tool: schema, document resolution, verdicts."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

MAIN = '\\documentclass{article}\n\\begin{document}\nHello world.\n\\end{document}\n'


def test_tool_schema_shape():
    from lib.tools.paper_edits import PAPER_EDITS_TOOL_NAMES, PAPER_PROPOSE_EDITS_TOOL
    assert PAPER_EDITS_TOOL_NAMES == frozenset({'paper_propose_edits'})
    function = PAPER_PROPOSE_EDITS_TOOL['function']
    assert function['name'] == 'paper_propose_edits'
    parameters = function['parameters']
    assert parameters['required'] == ['edits']
    assert parameters['properties']['edits']['maxItems'] == 20
    assert parameters['properties']['edits']['items']['required'] == ['path', 'search']
    assert 'USER REVIEW' in function['description']


def _task(**over):
    task = {'id': 'task_paper_edits_test', 'aborted': False,
            '_userId': 1, 'convId': 'conv-1'}
    task.update(over)
    return task


def _invoke(handler_args, task=None, fn_name='paper_propose_edits'):
    from lib.tasks_pkg.handlers.paper_edits import _handle_paper_edits_tool
    task = task or _task()
    round_entry = {'toolCallId': 'tc_1', 'status': 'running', 'query': ''}
    return _handle_paper_edits_tool(
        task, None, fn_name, 'tc_1', handler_args, 1, round_entry,
        {}, None, True)


_LOCAL_DOC = {'source': 'local', 'path': 'paper/main.tex',
              'project_root': '/tmp/p', 'main_file': 'paper/main.tex',
              'label': 'paper/main.tex', 'document_id': 'd' * 64}


@pytest.fixture
def discovery(monkeypatch):
    import lib.paper.builds.documents as documents
    state = {'documents': [_LOCAL_DOC], 'selected': _LOCAL_DOC}

    def fake(payload, *, owner_user_id):
        assert owner_user_id == 1
        return {'documents': list(state['documents']),
                'selected': state['selected'],
                'diagnostics': [], 'overleaf_servers': []}

    monkeypatch.setattr(documents, 'discover_documents', fake)
    return state


class TestHandler:
    def test_requires_conversation_context(self):
        _tc, content, _ = _invoke({}, task=_task(convId=None, conv_id=None))
        assert '会话' in content

    def test_reports_document_problems(self, discovery):
        discovery['selected'] = None
        discovery['documents'] = []
        _tc, content, _ = _invoke({'edits': [{'path': 'main.tex', 'search': 'a'}]})
        assert 'TeX' in content

    def test_rejects_malformed_edits(self, discovery):
        _tc, content, _ = _invoke({'edits': 'not-a-list'})
        assert 'edits' in content
        _tc, content, _ = _invoke({'edits': [{'path': 'main.tex'}]})
        assert 'path' in content and 'search' in content

    def test_verdict_carries_machine_readable_proposal_id(self, monkeypatch, discovery):
        import lib.paper.builds.proposals as proposals
        captured = {}

        def fake_create(*, owner_user_id, document_id, conv_id, edits):
            captured.update(owner=owner_user_id, document_id=document_id,
                            conv_id=conv_id, edits=edits)
            return {'schema_version': 1, 'proposal_id': 'ab' * 16,
                    'document_id': document_id, 'conv_id': conv_id,
                    'created_at': 1, 'status': 'pending',
                    'edits': [{'index': 0, 'path': 'main.tex', 'search': 'a',
                               'replace': 'b', 'description': '', 'status': 'pending'},
                              {'index': 1, 'path': 'main.tex', 'search': 'gone',
                               'replace': 'b', 'description': '', 'status': 'failed',
                               'error': 'Search text not found in main.tex.'}]}

        monkeypatch.setattr(proposals, 'create_proposal', fake_create)
        tc_id, content, _ = _invoke({'edits': [
            {'path': 'main.tex', 'search': 'a', 'replace': 'b'},
            {'path': 'main.tex', 'search': 'gone', 'replace': 'b',
             'description': 'x' * 500}]})
        assert tc_id == 'tc_1'
        assert content.split('\n', 1)[0] == 'proposal_id: ' + 'ab' * 16
        assert '已生成修改提案' in content
        assert '2 处修改待确认（1 处预检失败）' in content
        assert '请在预览面板中逐条接受或拒绝' in content
        assert '预检失败' in content.split('\n')[2]
        assert captured['owner'] == 1
        assert captured['document_id'] == 'd' * 64
        assert captured['conv_id'] == 'conv-1'
        assert captured['edits'][1]['description'] == 'x' * 200  # bounded

    def test_typed_failure_is_a_verdict(self, monkeypatch, discovery):
        import lib.paper.builds.proposals as proposals
        from lib.paper.builds.errors import BuildError

        def boom(*, owner_user_id, document_id, conv_id, edits):
            raise BuildError('source_path', 'Paper source is unavailable')

        monkeypatch.setattr(proposals, 'create_proposal', boom)
        _tc, content, _ = _invoke({'edits': [{'path': 'main.tex', 'search': 'a'}]})
        assert '无法生成修改提案' in content


@pytest.fixture
def paper_env(tmp_path, monkeypatch, discovery):
    """Real tmp paper tree + isolated storage; end-to-end handler coverage."""
    from tests.support.paper_storage import isolated_paper_storage
    root = tmp_path / 'paper'
    root.mkdir()
    (root / 'main.tex').write_text(MAIN)
    with isolated_paper_storage(tmp_path, monkeypatch):
        from lib.paper.builds.document_repository import PaperDocumentRepository
        row = PaperDocumentRepository(1).register({
            'source': 'local', 'path': 'paper/main.tex',
            'project_root': str(tmp_path), 'main_file': 'paper/main.tex',
            'label': 'paper/main.tex'})
        document = {'source': 'local', 'path': 'paper/main.tex',
                    'project_root': str(tmp_path), 'main_file': 'paper/main.tex',
                    'label': 'paper/main.tex', 'document_id': row['document_id']}
        discovery['documents'] = [document]
        discovery['selected'] = document
        yield {'root': root, 'document_id': row['document_id']}


def test_handler_creates_a_real_prevalidated_proposal(paper_env):
    from lib.paper.builds import proposals
    _tc, content, _ = _invoke({'edits': [
        {'path': 'main.tex', 'search': 'Hello world.', 'replace': 'Hello tofu.'},
        {'path': 'main.tex', 'search': 'missing anchor', 'replace': 'x'}]})
    first = content.split('\n', 1)[0]
    assert first.startswith('proposal_id: ')
    proposal_id = first.split('proposal_id: ', 1)[1].strip()
    assert len(proposal_id) == 32
    assert '1 处预检失败' in content
    stored = proposals.get_proposal(proposal_id, owner_user_id=1)
    assert stored['document_id'] == paper_env['document_id']
    assert stored['conv_id'] == 'conv-1'
    assert stored['edits'][0]['status'] == 'pending'
    assert stored['edits'][1]['status'] == 'failed'
    # The handler only proposes; the file on disk is untouched.
    assert (paper_env['root'] / 'main.tex').read_text() == MAIN
