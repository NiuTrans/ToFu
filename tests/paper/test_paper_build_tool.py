"""Agent paper_build tool: registry wiring, document resolution, verdicts."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _ctx(**over):
    from lib.tools.registry._spec import ToolContext
    base = dict(
        cfg={}, task_id='task_ctx_test', project_path='/tmp/p',
        project_enabled=True, search_mode='off', search_enabled=False,
        fetch_enabled=False, code_exec_enabled=False,
        browser_enabled=False, desktop_enabled=False,
        has_base_tools=True)
    base.update(over)
    return ToolContext(**base)


class TestRegistryWiring:
    def test_spec_partitions_and_timeout(self):
        from lib.tools.registry import all_specs, declared_tool_timeout_ms
        spec = next(s for s in all_specs() if s.key == 'paper_build')
        assert spec.phase == 'base'
        assert spec.provides == frozenset({'paper_build', 'paper_layout',
                                           'paper_import_arxiv',
                                           'paper_propose_edits'})
        # Compiling is a bounded verify action, not a user-file mutation:
        # approval-gating it would break the edit→compile→fix loop. The arXiv
        # import DOES create a workspace directory, so it rides the write
        # partition (Manual approval) like other project mutations.
        # paper_propose_edits only parks a review proposal — the journaled
        # write happens via the user-driven accept route, so it is not a
        # write tool either.
        assert spec.write_tools == frozenset({'paper_import_arxiv'})
        assert declared_tool_timeout_ms('paper_build') == 280_000
        assert declared_tool_timeout_ms('paper_layout') == 30_000
        assert declared_tool_timeout_ms('paper_import_arxiv') == 180_000
        assert declared_tool_timeout_ms('paper_propose_edits') == 60_000

    def test_builder_follows_project_gate(self):
        from lib.tools.registry._build import _build_paper_build
        ready = _build_paper_build(_ctx(project_enabled=True))
        assert [t['function']['name'] for t in ready] == [
            'paper_build', 'paper_layout', 'paper_import_arxiv',
            'paper_propose_edits']
        assert _build_paper_build(_ctx(project_enabled=False)) == []

    def test_paper_citations_spec_partitions(self):
        from lib.tools.registry import all_specs, declared_tool_timeout_ms
        spec = next(s for s in all_specs() if s.key == 'paper_citations')
        assert spec.provides == frozenset({'paper_cite_search',
                                           'paper_cite_insert',
                                           'paper_citation_audit'})
        # Only the insert mutates the project (.bib + .tex via the journaled
        # write tools); search and audit are read-only network lookups.
        assert spec.write_tools == frozenset({'paper_cite_insert'})
        assert declared_tool_timeout_ms('paper_cite_search') == 30_000
        assert declared_tool_timeout_ms('paper_cite_insert') == 60_000
        assert declared_tool_timeout_ms('paper_citation_audit') == 90_000

    def test_paper_quality_spec_partitions(self):
        from lib.tools.registry import all_specs, declared_tool_timeout_ms
        spec = next(s for s in all_specs() if s.key == 'paper_quality')
        assert spec.provides == frozenset({'paper_check', 'paper_review',
                                           'paper_scaffold'})
        # Read-only gate: live tree reads + retained build artifacts only.
        assert spec.write_tools == frozenset()
        assert declared_tool_timeout_ms('paper_check') == 60_000
        assert declared_tool_timeout_ms('paper_review') == 60_000
        assert declared_tool_timeout_ms('paper_scaffold') == 15_000


def _task(**over):
    task = {'id': 'task_paper_build_test', 'aborted': False,
            '_userId': 1, 'convId': 'conv-1'}
    task.update(over)
    return task


def _invoke(handler_args, task=None, fn_name='paper_build'):
    from lib.tasks_pkg.handlers.paper_build import _handle_paper_build_tool
    task = task or _task()
    round_entry = {'toolCallId': 'tc_1', 'status': 'running', 'query': ''}
    return _handle_paper_build_tool(
        task, None, fn_name, 'tc_1', handler_args, 1, round_entry,
        {}, None, True)


_LOCAL_DOC = {'source': 'local', 'path': 'paper/main.tex',
              'project_root': '/tmp/p', 'main_file': 'paper/main.tex',
              'label': 'paper/main.tex', 'document_id': 'd' * 64}

_RECEIPT = {'schema_version': 1, 'build_id': 'b' * 32, 'document_id': 'd' * 64,
            'owner_user_id': 1, 'target': 'host', 'source_digest': 'x' * 64,
            'version_verified': True, 'main_file': 'main.tex',
            'engine': 'xelatex', 'toolchain': 'TeX Live 2025',
            'status': 'failed', 'started_at': 0, 'completed_at': 2500,
            'artifacts': [{'name': 'build.log', 'sha256': 'y' * 64, 'size_bytes': 10}],
            'diagnostics': [
                {'severity': 'error', 'code': 'tex_error', 'file': 'main.tex',
                 'line': 5, 'message': 'Undefined control sequence.'},
                {'severity': 'warning', 'code': 'undefined_citation',
                 'file': 'main.tex', 'line': 9,
                 'message': "Citation `doe' on page 1 undefined on input line 9."}]}


class _FakeRuntime:
    def __init__(self, row):
        self._row = row

    def get_owned(self, task_id, *, user_id):
        assert user_id == 1
        return self._row


class _FakeScheduler:
    def __init__(self, row):
        self.production = type('P', (), {'runtime': _FakeRuntime(row)})()
        self.detached = []

    def renew(self, task_id, *, owner_user_id, subscriber_id):
        return True

    def detach(self, task_id, *, owner_user_id, subscriber_id):
        self.detached.append((task_id, subscriber_id))
        return True


@pytest.fixture
def discovery(monkeypatch):
    import lib.paper.builds.documents as documents
    state = {'documents': [_LOCAL_DOC], 'selected': _LOCAL_DOC, 'diagnostics': []}

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
        _tc, content, _ = _invoke({})
        assert 'TeX' in content

    def test_lists_ambiguous_candidates(self, discovery):
        other = dict(_LOCAL_DOC, path='paper/other.tex', document_id='e' * 64)
        discovery['selected'] = None
        discovery['documents'] = [_LOCAL_DOC, other]
        _tc, content, _ = _invoke({})
        assert 'paper/main.tex' in content and 'paper/other.tex' in content

    def test_compile_returns_structured_failure(self, monkeypatch, discovery):
        import lib.tasks_pkg.handlers.paper_build as handler
        accepted = {'task_id': 'paper_build_1', 'document_id': 'd' * 64,
                    'subscriber_id': 'agent:task_paper_build_test'}
        monkeypatch.setattr(handler, 'start_preview',
                            lambda payload, *, owner_user_id: accepted)
        row = {'status': 'error', 'result': {'receipt': _RECEIPT}}
        fake = _FakeScheduler(row)
        monkeypatch.setattr(handler, 'get_scheduler', lambda: fake)
        monkeypatch.setattr(handler, '_log_tail', lambda *a: 'tail-bytes')
        tc_id, content, _ = _invoke({'action': 'compile'})
        assert tc_id == 'tc_1'
        assert '编译失败' in content
        assert 'main.tex:5' in content
        assert 'Undefined control sequence.' in content
        assert 'undefined_citation' in content
        assert 'tail-bytes' in content

    def test_compile_success_verdict(self, monkeypatch, discovery):
        import lib.tasks_pkg.handlers.paper_build as handler
        accepted = {'task_id': 'paper_build_1', 'document_id': 'd' * 64,
                    'subscriber_id': 'agent:x'}
        monkeypatch.setattr(handler, 'start_preview',
                            lambda payload, *, owner_user_id: accepted)
        receipt = {**_RECEIPT, 'status': 'success', 'diagnostics': [],
                   'artifacts': [{'name': 'document.pdf', 'sha256': 'y' * 64,
                                  'size_bytes': 10}]}
        row = {'status': 'done', 'result': {'receipt': receipt}}
        monkeypatch.setattr(handler, 'get_scheduler', lambda: _FakeScheduler(row))
        _tc, content, _ = _invoke({'engine': 'xelatex'})
        assert '编译成功' in content

    def test_compile_timeout_guides_retry(self, monkeypatch, discovery):
        import lib.tasks_pkg.handlers.paper_build as handler
        monkeypatch.setattr(handler, 'start_preview',
                            lambda payload, *, owner_user_id:
                            {'task_id': 't', 'document_id': 'd' * 64,
                             'subscriber_id': 'agent:x'})
        monkeypatch.setattr(handler, '_WAIT_DEADLINE_SECONDS', 0)
        fake = _FakeScheduler({'status': 'running'})
        monkeypatch.setattr(handler, 'get_scheduler', lambda: fake)
        _tc, content, _ = _invoke({})
        assert 'diagnostics' in content
        assert fake.detached

    def test_build_error_is_typed(self, monkeypatch, discovery):
        import lib.tasks_pkg.handlers.paper_build as handler
        from lib.paper.builds.errors import BuildError

        def boom(payload, *, owner_user_id):
            raise BuildError('build_busy', 'queue full')

        monkeypatch.setattr(handler, 'start_preview', boom)
        _tc, content, _ = _invoke({})
        assert 'queue full' in content and '稍后' in content

    def test_diagnostics_action_reads_retained(self, monkeypatch, discovery):
        import lib.paper.builds.document_repository as repository

        class FakeRepo:
            def __init__(self, owner):
                assert owner == 1

            def get(self, document_id):
                return {'document_id': document_id, 'status': 'published',
                        'receipt': {**_RECEIPT, 'status': 'success',
                                    'diagnostics': []},
                        'reading': {}, 'reading_revision': 0}

        monkeypatch.setattr(repository, 'PaperDocumentRepository', FakeRepo)
        _tc, content, _ = _invoke({'action': 'diagnostics'})
        assert '编译成功' in content

    def test_layout_tool_reports_page_budget(self, monkeypatch, discovery):
        import lib.paper.builds.document_repository as repository
        import lib.paper.builds.layout as layout

        class FakeRepo:
            def __init__(self, owner):
                pass

            def get(self, document_id):
                return {'document_id': document_id, 'status': 'published',
                        'source': _LOCAL_DOC,
                        'receipt': {**_RECEIPT, 'status': 'success',
                                    'diagnostics': []},
                        'reading': {}, 'reading_revision': 0}

        monkeypatch.setattr(repository, 'PaperDocumentRepository', FakeRepo)
        monkeypatch.setattr(layout, 'document_layout',
                            lambda document, build_id, *, owner_user_id: {
                                'build_id': 'b' * 32, 'pages': 9,
                                'sections': [{'level': 'section', 'title': 'Method',
                                              'file': 'main.tex', 'line': 10, 'page': 4}],
                                'end': {'page': 9, 'fullness_page': 0.42,
                                        'fullness_text': None}})
        _tc, content, _ = _invoke({}, fn_name='paper_layout')
        assert '共 9 页' in content
        assert 'Method' in content and 'main.tex:10' in content
        assert '42%' in content

    def test_layout_without_build_guides_compile(self, monkeypatch, discovery):
        import lib.paper.builds.document_repository as repository

        class FakeRepo:
            def __init__(self, owner):
                pass

            def get(self, document_id):
                return {'document_id': document_id, 'status': 'pending',
                        'source': _LOCAL_DOC, 'receipt': None,
                        'reading': {}, 'reading_revision': 0}

        monkeypatch.setattr(repository, 'PaperDocumentRepository', FakeRepo)
        _tc, content, _ = _invoke({}, fn_name='paper_layout')
        assert 'paper_build' in content

    def test_import_arxiv_reports_selection(self, monkeypatch):
        import lib.paper.builds.imports as imports
        monkeypatch.setattr(imports, 'import_arxiv_source', lambda payload, *, owner_user_id: {
            'directory': 'arxiv-2301.12345', 'files': 3, 'bytes': 4096, 'skipped': [],
            'candidates': ['arxiv-2301.12345/main.tex'],
            'document': {'source': 'local', 'path': 'arxiv-2301.12345/main.tex',
                         'project_root': '/tmp/p', 'main_file': 'arxiv-2301.12345/main.tex',
                         'label': 'arxiv-2301.12345/main.tex', 'document_id': 'd' * 64}})
        _tc, content, _ = _invoke({'arxiv': '2301.12345'}, fn_name='paper_import_arxiv')
        assert 'arxiv-2301.12345' in content
        assert 'paper_build' in content

    def test_import_arxiv_typed_failure(self, monkeypatch):
        import lib.paper.builds.imports as imports
        from lib.paper.builds.errors import BuildError

        def boom(payload, *, owner_user_id):
            raise BuildError('arxiv_source_unavailable', 'PDF-only paper')

        monkeypatch.setattr(imports, 'import_arxiv_source', boom)
        _tc, content, _ = _invoke({'arxiv': '2301.12345'}, fn_name='paper_import_arxiv')
        assert 'PDF-only' in content

    def test_import_arxiv_requires_id(self):
        _tc, content, _ = _invoke({}, fn_name='paper_import_arxiv')
        assert 'arxiv' in content

    def test_success_verdict_includes_page_count(self, monkeypatch, discovery):
        import lib.tasks_pkg.handlers.paper_build as handler
        accepted = {'task_id': 'paper_build_1', 'document_id': 'd' * 64,
                    'subscriber_id': 'agent:x'}
        monkeypatch.setattr(handler, 'start_preview',
                            lambda payload, *, owner_user_id: accepted)
        receipt = {**_RECEIPT, 'status': 'success', 'diagnostics': [],
                   'artifacts': [{'name': 'document.pdf', 'sha256': 'y' * 64,
                                  'size_bytes': 10}]}
        row = {'status': 'done', 'result': {'receipt': receipt}}
        monkeypatch.setattr(handler, 'get_scheduler', lambda: _FakeScheduler(row))
        monkeypatch.setattr(handler, '_page_count', lambda *a: 8)
        _tc, content, _ = _invoke({})
        assert '共 8 页' in content
