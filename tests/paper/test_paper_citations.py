"""PID-anchored citation stack: search/fetch/verify/mutate + handler verdicts.

All network is mocked at ``lib.paper.citations.http_get``; file mutations run
through the real journaled write tools against tmp_path roots.
"""
from __future__ import annotations

import pytest

from lib.paper import citations
from lib.paper.builds.errors import BuildError

pytestmark = pytest.mark.unit


# ── Fake HTTP boundary ───────────────────────────────────────────────

class _Resp:
    def __init__(self, status=200, payload=None, text=''):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError('no json body')
        return self._payload


def _work(**over):
    raw = {
        'id': 'https://openalex.org/W2741809807',
        'doi': 'https://doi.org/10.48550/arxiv.1706.03762',
        'display_name': 'Attention Is All You Need',
        'publication_year': 2017,
        'authorships': [{'author': {'display_name': 'Ashish Vaswani'}},
                        {'author': {'display_name': 'Noam Shazeer'}}],
        'primary_location': {'source': {'display_name': 'NeurIPS'}},
        'best_oa_location': {'pdf_url': 'https://arxiv.org/pdf/1706.03762.pdf'},
        'cited_by_count': 90000,
        'is_retracted': False,
        'type': 'proceedings-article',
    }
    raw.update(over)
    return raw


def _router(monkeypatch, routes, *, recorder=None):
    """routes: list of (url_substring, response-or-callable)."""
    def fake_get(url, *, timeout=None, headers=None, params=None, **kw):
        if recorder is not None:
            recorder.append((url, params))
        for needle, value in routes:
            if needle in url:
                return value(url) if callable(value) else value
        raise AssertionError('unexpected URL: %s' % url)
    monkeypatch.setattr(citations, 'http_get', fake_get)


@pytest.fixture(autouse=True)
def _clear_pid_cache():
    citations._cached_openalex_work.cache_clear()
    yield
    citations._cached_openalex_work.cache_clear()


@pytest.fixture(autouse=True)
def _no_polite_delay(monkeypatch):
    monkeypatch.setattr(citations, '_VERIFY_POLITE_DELAY_SECONDS', 0.0)


# ── search_works ─────────────────────────────────────────────────────

class TestSearchWorks:
    def test_normalization(self, monkeypatch):
        payload = {'results': [_work()]}
        _router(monkeypatch, [('api.openalex.org/works',
                               _Resp(200, payload))])
        works = citations.search_works('attention is all you need')
        assert len(works) == 1
        work = works[0]
        assert work['openalex_id'] == 'W2741809807'
        assert work['doi'] == '10.48550/arxiv.1706.03762'
        assert work['title'] == 'Attention Is All You Need'
        assert work['authors'] == ['Ashish Vaswani', 'Noam Shazeer']
        assert work['year'] == 2017
        assert work['venue'] == 'NeurIPS'
        assert work['cited_by_count'] == 90000
        assert work['is_retracted'] is False
        assert work['oa_pdf_url'].endswith('1706.03762.pdf')
        assert work['bibtex_key'] == 'vaswani2017attention'

    def test_search_params_and_limit_clamp(self, monkeypatch):
        calls = []
        _router(monkeypatch, [('api.openalex.org/works',
                               _Resp(200, {'results': []}))], recorder=calls)
        citations.search_works('q', limit=999)
        url, params = calls[0]
        assert params['per-page'] == 25
        assert params['mailto'] == 'tofu@localhost'

    def test_defensive_defaults_for_missing_fields(self, monkeypatch):
        _router(monkeypatch, [('api.openalex.org/works',
                               _Resp(200, {'results': [{'id': None}]})),])
        work = citations.search_works('q')[0]
        assert work['authors'] == [] and work['doi'] == ''
        assert work['bibtex_key']  # still a usable proposed key

    def test_network_failure_raises_typed_error(self, monkeypatch):
        def boom(url, **kw):
            raise ConnectionError('offline')
        monkeypatch.setattr(citations, 'http_get', boom)
        with pytest.raises(BuildError) as err:
            citations.search_works('anything')
        assert err.value.code == 'citation_backend'

    def test_empty_query_is_typed(self):
        with pytest.raises(BuildError) as err:
            citations.search_works('   ')
        assert err.value.code == 'citation_query'


# ── key proposal / validation ────────────────────────────────────────

class TestKeys:
    def test_propose_key_ascii_folds(self):
        key = citations.propose_key({
            'authors': ['Lénaïc Dœ, Jr.'], 'year': 2024,
            'title': 'The Ópera of Štorms'})
        assert key == 'doe2024opera'
        assert citations.validate_key(key) == key

    def test_propose_key_skips_stopwords_and_anonymous(self):
        assert citations.propose_key({'authors': [], 'year': '',
                                      'title': 'A of the'}) == 'work'

    def test_validate_key_rejects_bad(self):
        for bad in ('', 'has space', 'x' * 65, 'semi;colon', '中'):
            with pytest.raises(BuildError) as err:
                citations.validate_key(bad)
            assert err.value.code == 'invalid_key'

    def test_rewrite_bibtex_key(self):
        bib = '@article{oldkey,\n  title = {T}\n}'
        assert citations.rewrite_bibtex_key(bib, 'new2024x').startswith(
            '@article{new2024x,')


# ── fetch_bibtex ─────────────────────────────────────────────────────

_CROSSREF_BIB = ('@article{vaswani2017attention,\n'
                 '  author = {Vaswani, Ashish and Shazeer, Noam},\n'
                 '  title = {Attention Is All You Need},\n'
                 '  year = {2017},\n'
                 '  doi = {10.48550/arxiv.1706.03762}\n}')


class TestFetchBibtex:
    def test_crossref_transform_roundtrip(self, monkeypatch):
        _router(monkeypatch, [('api.crossref.org/works/',
                               _Resp(200, text=_CROSSREF_BIB))])
        bibtex, key = citations.fetch_bibtex('10.48550/arxiv.1706.03762')
        assert key == 'vaswani2017attention'
        assert bibtex.startswith('@article{vaswani2017attention,')

    def test_doi_org_negotiation_fallback(self, monkeypatch):
        calls = []

        def route(url):
            calls.append(url)
            if 'api.crossref.org' in url:
                return _Resp(404)
            return _Resp(200, text=_CROSSREF_BIB)

        _router(monkeypatch, [('api.crossref.org', route),
                              ('doi.org/10.', route)])
        bibtex, key = citations.fetch_bibtex(
            'https://doi.org/10.48550/arxiv.1706.03762')
        assert key == 'vaswani2017attention'
        assert any('api.crossref.org' in u for u in calls)
        assert any(u.startswith('https://doi.org/') for u in calls)

    def test_openalex_synthesis_fallback(self, monkeypatch):
        _router(monkeypatch, [
            ('api.crossref.org', _Resp(404)),
            ('doi.org/10.', _Resp(404)),
            ('api.openalex.org/works/doi:', _Resp(200, _work(
                type='book', display_name='Deep Learning',
                publication_year=2016,
                doi='https://doi.org/10.5555/dl-book',
                authorships=[{'author': {'display_name': 'Ian Goodfellow'}}]))),
        ])
        bibtex, key = citations.fetch_bibtex('10.5555/dl-book')
        assert key == 'goodfellow2016deep'
        assert bibtex.startswith('@book{goodfellow2016deep,')
        assert 'author = {Ian Goodfellow}' in bibtex
        assert 'publisher' not in bibtex  # venue maps to booktitle for books
        assert 'doi = {10.5555/dl-book}' in bibtex

    def test_arxiv_id_maps_to_datacite_doi(self, monkeypatch):
        calls = []
        _router(monkeypatch, [('api.crossref.org/works/',
                               lambda url: calls.append(url)
                               or _Resp(200, text=_CROSSREF_BIB))])
        citations.fetch_bibtex('arXiv:1706.03762')
        assert '10.48550%2FarXiv.1706.03762' in calls[0]

    def test_openalex_id_input(self, monkeypatch):
        _router(monkeypatch, [
            ('api.openalex.org/works/W2741809807', _Resp(200, _work())),
            ('api.crossref.org', _Resp(200, text=_CROSSREF_BIB)),
        ])
        bibtex, key = citations.fetch_bibtex('W2741809807')
        assert key == 'vaswani2017attention'

    def test_work_dict_without_doi_synthesizes(self):
        bibtex, key = citations.fetch_bibtex({
            'authors': ['Jane Doe'], 'year': 2022, 'title': 'Solo Work',
            'doi': '', 'openalex_id': '', 'venue': 'Nowhere',
            'oa_pdf_url': ''})
        assert key == 'doe2022solo'
        assert bibtex.startswith('@article{doe2022solo,')

    def test_unknown_identifier_is_typed(self):
        with pytest.raises(BuildError) as err:
            citations.fetch_bibtex('not-an-identifier')
        assert err.value.code == 'citation_query'

    def test_all_backends_down_raises_backend(self, monkeypatch):
        def boom(url, **kw):
            raise TimeoutError('offline')
        monkeypatch.setattr(citations, 'http_get', boom)
        with pytest.raises(BuildError) as err:
            citations.fetch_bibtex('10.1038/nature14539')
        assert err.value.code == 'citation_backend'


# ── verify_entries ───────────────────────────────────────────────────

class TestVerifyEntries:
    def _routes(self, monkeypatch, by_doi, search_payload=None):
        def doi_route(url):
            for doi, resp in by_doi.items():
                if doi in url:
                    return resp
            return _Resp(404)
        routes = [('api.openalex.org/works/doi:', doi_route)]
        if search_payload is not None:
            routes.append(('api.openalex.org/works',
                           _Resp(200, search_payload)))
        _router(monkeypatch, routes)

    def test_verified_by_doi(self, monkeypatch):
        self._routes(monkeypatch, {
            '10.48550%2Farxiv.1706.03762': _Resp(200, _work())})
        rows = citations.verify_entries([{
            'key': 'vaswani2017attention',
            'doi': '10.48550/arXiv.1706.03762',
            'title': 'Attention Is All You Need', 'author': 'Vaswani, Ashish',
            'year': '2017'}])
        assert rows[0]['status'] == 'verified'
        assert rows[0]['evidence']['matched_venue'] == 'NeurIPS'
        assert rows[0]['evidence']['similarity'] == 1.0

    def test_mismatch_on_year_and_author(self, monkeypatch):
        self._routes(monkeypatch, {
            '10.48550%2Farxiv.1706.03762': _Resp(200, _work())})
        rows = citations.verify_entries([{
            'key': 'k', 'doi': '10.48550/arXiv.1706.03762',
            'title': 'Attention Is All You Need',
            'author': 'Smith, John', 'year': '2010'}])
        assert rows[0]['status'] == 'mismatch'

    def test_not_found(self, monkeypatch):
        self._routes(monkeypatch, {'10.9999%2Fghost': _Resp(404)})
        rows = citations.verify_entries([{
            'key': 'ghost2024', 'doi': '10.9999/ghost', 'title': 'Ghost',
            'author': '', 'year': ''}])
        assert rows[0]['status'] == 'not_found'

    def test_retracted(self, monkeypatch):
        self._routes(monkeypatch, {
            '10.48550%2Farxiv.1706.03762': _Resp(200, _work(is_retracted=True))})
        rows = citations.verify_entries([{
            'key': 'k', 'doi': '10.48550/arXiv.1706.03762',
            'title': 'Attention Is All You Need', 'author': 'Vaswani, Ashish',
            'year': '2017'}])
        assert rows[0]['status'] == 'retracted'
        assert rows[0]['evidence']['matched_title'] == ('Attention Is All '
                                                        'You Need')

    def test_title_search_resolution(self, monkeypatch):
        self._routes(monkeypatch, {},
                     search_payload={'results': [_work()]})
        rows = citations.verify_entries([{
            'key': 'k', 'doi': '', 'title': 'Attention Is All You Need',
            'author': 'Vaswani, Ashish', 'year': '2017'}])
        assert rows[0]['status'] == 'verified'
        assert rows[0]['evidence']['doi'] == '10.48550/arxiv.1706.03762'

    def test_title_search_below_threshold_not_found(self, monkeypatch):
        self._routes(monkeypatch, {},
                     search_payload={'results': [_work(
                         display_name='Completely Different Paper')]})
        rows = citations.verify_entries([{
            'key': 'k', 'doi': '', 'title': 'Attention Is All You Need',
            'author': '', 'year': ''}])
        assert rows[0]['status'] == 'not_found'

    def test_skipped_when_backend_down(self, monkeypatch):
        def boom(url, **kw):
            raise ConnectionError('offline')
        monkeypatch.setattr(citations, 'http_get', boom)
        rows = citations.verify_entries([{
            'key': 'k', 'doi': '10.1/x', 'title': 'T', 'author': '',
            'year': ''}])
        assert rows[0]['status'] == 'skipped'
        assert 'unavailable' in rows[0]['note']

    def test_entry_budget_marks_extras_skipped(self, monkeypatch):
        self._routes(monkeypatch, {
            '10.1%2Fx': _Resp(200, _work(doi='https://doi.org/10.1/x'))})
        entries = [{'key': 'k%d' % i, 'doi': '10.1/x', 'title': '',
                    'author': '', 'year': ''} for i in range(10)]
        rows = citations.verify_entries(entries)
        assert len(rows) == 10
        assert [row['status'] for row in rows[:8]] == ['verified'] * 8
        assert all(row['status'] == 'skipped' for row in rows[8:])

    def test_time_budget_skips_remainder(self, monkeypatch):
        monkeypatch.setattr(citations.time, 'monotonic',
                            iter([0, 100, 100, 100]).__next__)
        rows = citations.verify_entries(
            [{'key': 'a', 'doi': '', 'title': '', 'author': '', 'year': ''},
             {'key': 'b', 'doi': '', 'title': '', 'author': '', 'year': ''}],
            budget_seconds=25)
        assert rows[1]['status'] == 'skipped'


# ── collect_entries ──────────────────────────────────────────────────

class TestCollectEntries:
    def test_fields_and_order(self):
        entries = citations.collect_entries(
            '@article{doe2024x,\n'
            '  author = {Doe, Jane and Roe, Richard},\n'
            '  title = {A {B}raced Title},\n'
            '  year = {2024},\n'
            '  doi = {10.1/abc}\n'
            '}\n'
            '@misc{roe2023y, title = "Quoted", year = 2023}')
        assert [e['key'] for e in entries] == ['doe2024x', 'roe2023y']
        first = entries[0]
        assert first['entry_type'] == 'article' and first['line'] == 1
        assert first['doi'] == '10.1/abc'
        assert first['title'] == 'A {B}raced Title'
        assert first['author'] == 'Doe, Jane'
        assert first['year'] == '2024'
        assert entries[1]['title'] == 'Quoted'


# ── journaled mutations (real write tools, tmp roots) ────────────────

@pytest.fixture
def journal(tmp_path, monkeypatch):
    from lib.project_mod import modifications as mods
    from lib.project_mod.write_tools import _ops
    monkeypatch.setattr(mods, 'SESSIONS_DIR', str(tmp_path / 'journals'))
    monkeypatch.setattr(_ops, '_should_record_modification',
                        lambda *a, **kw: True)
    monkeypatch.setattr(_ops, '_mod_attribution',
                        lambda target, root, rel, **kw: (root, rel))
    return tmp_path


_BIB_ONE = ('@article{doe2024x,\n  author = {Doe, Jane},\n'
            '  title = {Real Paper},\n  year = {2024},\n'
            '  doi = {10.1/abc}\n}')
_BIB_TWO = ('@article{roe2025y,\n  author = {Roe, Richard},\n'
            '  title = {Another Real Paper},\n  year = {2025}\n}')
_BIB_DUP_DOI = ('@article{alias2024,\n  title = {Same DOI},\n'
                '  doi = {10.1/abc}\n}')


class TestAppendBibEntries:
    def test_creates_refs_bib_and_dedups(self, journal):
        base = journal / 'proj'
        base.mkdir()
        result = citations.append_bib_entries(
            str(base), 'refs.bib', [(_BIB_ONE, 'doe2024x')], task_id='t1')
        assert result['created'] is True and result['added'] == ['doe2024x']
        text = (base / 'refs.bib').read_text()
        assert '@article{doe2024x,' in text
        again = citations.append_bib_entries(
            str(base), 'refs.bib',
            [(_BIB_ONE, 'doe2024x'), (_BIB_TWO, 'roe2025y'),
             (_BIB_DUP_DOI, 'alias2024')], task_id='t2')
        assert again['added'] == ['roe2025y']
        assert sorted(again['skipped']) == ['alias2024', 'doe2024x']
        text = (base / 'refs.bib').read_text()
        assert text.count('@article{') == 2
        assert 'alias2024' not in text

    def test_appends_to_existing_file(self, journal):
        base = journal / 'proj'
        base.mkdir()
        (base / 'refs.bib').write_text('@article{old, title={O}}')
        result = citations.append_bib_entries(
            str(base), 'refs.bib', [(_BIB_TWO, 'roe2025y')], task_id='t')
        assert result['created'] is False
        text = (base / 'refs.bib').read_text()
        assert '@article{old,' in text and '@article{roe2025y,' in text

    def test_undoable_through_journal(self, journal):
        from lib.project_mod import modifications as mods
        base = journal / 'proj'
        base.mkdir()
        (base / 'refs.bib').write_text('')
        citations.append_bib_entries(
            str(base), 'refs.bib', [(_BIB_ONE, 'doe2024x')], task_id='t')
        assert (base / 'refs.bib').read_text().strip()
        undone = mods.undo_task_modifications(str(base), 't')
        assert undone['failed'] == 0
        assert (base / 'refs.bib').read_text() == ''

    def test_rejects_non_bib_and_traversal(self, journal):
        base = journal / 'proj'
        base.mkdir()
        with pytest.raises(BuildError) as err:
            citations.append_bib_entries(str(base), 'refs.tex', [])
        assert err.value.code == 'source_path'
        with pytest.raises(BuildError):
            citations.append_bib_entries(str(base), '../refs.bib',
                                         [(_BIB_ONE, 'doe2024x')])

    def test_entry_cap(self, journal, monkeypatch):
        monkeypatch.setattr(citations, 'MAX_BIB_ENTRIES', 1)
        base = journal / 'proj'
        base.mkdir()
        (base / 'refs.bib').write_text('@article{old, title={O}}')
        with pytest.raises(BuildError) as err:
            citations.append_bib_entries(
                str(base), 'refs.bib', [(_BIB_TWO, 'roe2025y')])
        assert err.value.code == 'bib_limit'


class TestInsertCite:
    def test_insert_after_anchor(self, journal):
        base = journal / 'proj'
        base.mkdir()
        (base / 'main.tex').write_text(
            'Transformers dominate seq2seq modeling. We extend them.\n')
        result = citations.insert_cite(
            str(base), 'main.tex', 'seq2seq modeling', 'vaswani2017attention',
            task_id='t')
        assert result['inserted'] is True
        assert ('seq2seq modeling\\cite{vaswani2017attention}. We'
                in (base / 'main.tex').read_text())

    def test_insert_before_anchor(self, journal):
        base = journal / 'proj'
        base.mkdir()
        (base / 'main.tex').write_text('as shown recently.\n')
        citations.insert_cite(str(base), 'main.tex', 'as shown recently',
                              'k', position='before', task_id='t')
        assert (base / 'main.tex').read_text().startswith('\\cite{k}as shown')

    def test_idempotent_when_already_cited(self, journal):
        base = journal / 'proj'
        base.mkdir()
        (base / 'main.tex').write_text(
            'seq2seq modeling\\cite{vaswani2017attention}. We extend.\n')
        result = citations.insert_cite(
            str(base), 'main.tex', 'seq2seq modeling', 'vaswani2017attention',
            task_id='t')
        assert result['inserted'] is False
        assert result['reason'] == 'already cited at the anchor'

    def test_anchor_missing_and_ambiguous(self, journal):
        base = journal / 'proj'
        base.mkdir()
        (base / 'main.tex').write_text('dup dup\nalpha alpha\n')
        with pytest.raises(BuildError) as err:
            citations.insert_cite(str(base), 'main.tex', 'missing anchor', 'k')
        assert err.value.code == 'anchor_not_found'
        with pytest.raises(BuildError) as err:
            citations.insert_cite(str(base), 'main.tex', 'alpha', 'k')
        assert err.value.code == 'anchor_ambiguous'


class TestDocumentHelpers:
    def test_default_bib_path_prefers_main_dir_then_refs(self, journal):
        base = journal / 'proj'
        (base / 'paper').mkdir(parents=True)
        (base / 'paper' / 'a.bib').write_text('')
        (base / 'other.bib').write_text('')
        assert citations.default_bib_path(
            str(base), 'paper/main.tex') == 'paper/a.bib'
        empty = journal / 'empty'
        empty.mkdir()
        assert citations.default_bib_path(
            str(empty), 'paper/main.tex') == 'paper/refs.bib'

    def test_bibliography_status(self, journal):
        base = journal / 'proj'
        base.mkdir()
        (base / 'main.tex').write_text('Text. \\bibliography{refs}\n')
        assert citations.bibliography_status(
            str(base), 'refs.bib')['referenced'] is True
        (base / 'main.tex').write_text('Text only.\n')
        status = citations.bibliography_status(str(base), 'refs.bib')
        assert status['referenced'] is False
        (base / 'main.tex').write_text(
            '\\addbibresource{refs.bib}\nText.\n')
        assert citations.bibliography_status(
            str(base), 'refs.bib')['via'] == 'addbibresource'


# ── handler-level (mocked citations module / discovery) ──────────────

_LOCAL_DOC = {'source': 'local', 'path': 'paper/main.tex',
              'project_root': '', 'main_file': 'paper/main.tex',
              'label': 'paper/main.tex', 'document_id': 'd' * 64}


def _task(**over):
    task = {'id': 'task_paper_citations_test', 'aborted': False,
            '_userId': 1, 'convId': 'conv-1'}
    task.update(over)
    return task


def _invoke(handler_args, task=None, fn_name='paper_cite_search'):
    from lib.tasks_pkg.handlers.paper_citations import (
        _handle_paper_citations_tool)
    task = task or _task()
    round_entry = {'toolCallId': 'tc_1', 'status': 'running', 'query': ''}
    return _handle_paper_citations_tool(
        task, None, fn_name, 'tc_1', handler_args, 1, round_entry,
        {}, None, True)


@pytest.fixture
def discovery(monkeypatch, tmp_path):
    import lib.paper.builds.documents as documents
    doc = dict(_LOCAL_DOC, project_root=str(tmp_path))
    state = {'documents': [doc], 'selected': doc}

    def fake(payload, *, owner_user_id):
        assert owner_user_id == 1
        return {'documents': list(state['documents']),
                'selected': state['selected'],
                'diagnostics': [], 'overleaf_servers': []}

    monkeypatch.setattr(documents, 'discover_documents', fake)
    return state


class TestHandlerSearch:
    def test_requires_conversation_context(self):
        _tc, content, _ = _invoke(
            {'query': 'x'}, task=_task(convId=None, conv_id=None))
        assert '会话' in content

    def test_requires_query(self):
        _tc, content, _ = _invoke({})
        assert 'query' in content

    def test_search_verdict_lists_candidates(self, monkeypatch):
        monkeypatch.setattr(citations, 'search_works', lambda q, *, limit: [{
            'bibtex_key': 'vaswani2017attention',
            'title': 'Attention Is All You Need', 'year': 2017,
            'venue': 'NeurIPS', 'cited_by_count': 90000,
            'is_retracted': False, 'authors': ['Ashish Vaswani'],
            'doi': '10.48550/arxiv.1706.03762',
            'openalex_id': 'W2741809807'}])
        _tc, content, _ = _invoke({'query': 'attention'})
        assert '真实文献' in content
        assert 'vaswani2017attention' in content
        assert 'paper_cite_insert' in content

    def test_search_backend_error_is_friendly(self, monkeypatch):
        def boom(q, *, limit):
            raise BuildError('citation_backend', 'offline')
        monkeypatch.setattr(citations, 'search_works', boom)
        _tc, content, _ = _invoke({'query': 'x'})
        assert '文献服务' in content and '稍后' in content


class TestHandlerInsert:
    def test_insert_flow_composes_verdict(self, monkeypatch, discovery,
                                          journal):
        monkeypatch.setattr(
            citations, 'fetch_bibtex',
            lambda ident: (_BIB_ONE, 'doe2024x'))
        base = discovery['selected']['project_root']
        import os
        os.makedirs(os.path.join(base, 'paper'), exist_ok=True)
        with open(os.path.join(base, 'paper', 'main.tex'), 'w') as fh:
            fh.write('Prior work seq2seq modeling. Ours.\n'
                     '\\bibliography{refs}\n')
        tc_id, content, _ = _invoke(
            {'doi': '10.1/abc', 'anchor': 'seq2seq modeling'},
            fn_name='paper_cite_insert')
        assert tc_id == 'tc_1'
        assert 'paper/refs.bib' in content
        assert '\\cite{doe2024x}' in content
        text = open(os.path.join(base, 'paper', 'refs.bib')).read()
        assert '@article{doe2024x,' in text
        text = open(os.path.join(base, 'paper', 'main.tex')).read()
        assert 'seq2seq modeling\\cite{doe2024x}' in text

    def test_insert_without_identifier_guides(self, discovery):
        _tc, content, _ = _invoke({}, fn_name='paper_cite_insert')
        assert 'paper_cite_search' in content

    def test_insert_warns_when_bibliography_missing(self, monkeypatch,
                                                    discovery, journal):
        monkeypatch.setattr(
            citations, 'fetch_bibtex',
            lambda ident: (_BIB_ONE, 'doe2024x'))
        base = discovery['selected']['project_root']
        import os
        os.makedirs(os.path.join(base, 'paper'), exist_ok=True)
        with open(os.path.join(base, 'paper', 'main.tex'), 'w') as fh:
            fh.write('No bibliography here.\n')
        _tc, content, _ = _invoke({'doi': '10.1/abc'},
                                  fn_name='paper_cite_insert')
        assert '\\bibliography' in content

    def test_insert_requires_document(self, monkeypatch):
        import lib.paper.builds.documents as documents
        monkeypatch.setattr(documents, 'discover_documents',
                            lambda payload, *, owner_user_id: {
                                'documents': [], 'selected': None,
                                'diagnostics': [], 'overleaf_servers': []})
        _tc, content, _ = _invoke({'doi': '10.1/abc'},
                                  fn_name='paper_cite_insert')
        assert 'TeX' in content


class TestHandlerAudit:
    def _report(self, **over):
        report = {
            'bib_files': ['refs.bib'], 'tex_files': ['main.tex'],
            'entries_total': 2,
            'verifications': [
                {'key': 'good', 'status': 'verified',
                 'evidence': {'matched_title': 'T', 'matched_year': 2020,
                              'matched_venue': 'V', 'similarity': 0.99}},
                {'key': 'ghost2024', 'status': 'not_found',
                 'evidence': {'similarity': 0.1}}],
            'duplicates': [], 'undefined': ['lost2023'],
            'unused': ['idle'],
            'bibliography': {'referenced': True, 'via': 'bibliography',
                             'file': 'main.tex', 'bib': 'refs.bib'},
        }
        report.update(over)
        return report

    def test_audit_verdict_counts(self, monkeypatch, discovery):
        monkeypatch.setattr(citations, 'audit_document',
                            lambda base, *, main_file: self._report())
        _tc, content, _ = _invoke({}, fn_name='paper_citation_audit')
        assert '疑似幻觉 1' in content
        assert 'ghost2024' in content
        assert 'lost2023' in content
        assert 'paper_cite_search' in content

    def test_audit_all_clear(self, monkeypatch, discovery):
        monkeypatch.setattr(citations, 'audit_document',
                            lambda base, *, main_file: self._report(
                                verifications=[{'key': 'good',
                                                'status': 'verified',
                                                'evidence': {}}],
                                undefined=[], unused=[]))
        _tc, content, _ = _invoke({}, fn_name='paper_citation_audit')
        assert '真实文献' in content and 'paper_build' in content

    def test_audit_retracted_listed(self, monkeypatch, discovery):
        monkeypatch.setattr(citations, 'audit_document',
                            lambda base, *, main_file: self._report(
                                verifications=[{
                                    'key': 'bad', 'status': 'retracted',
                                    'evidence': {'matched_title': 'Bad Paper',
                                                 'matched_year': 2019,
                                                 'similarity': 0.97}}],
                                undefined=[], unused=[]))
        _tc, content, _ = _invoke({}, fn_name='paper_citation_audit')
        assert '已撤稿' in content and 'bad' in content
