"""Targeted paper-body reads replace full-bookshelf research scans."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def test_sidecar_projection_selects_only_requested_owner_papers():
    from lib.storage.errors import StorageError
    from lib.storage_sidecar import operations

    captured = {}

    class Session:
        def fetch_all(self, sql, args):
            captured['sql'] = sql
            captured['args'] = args
            return [{
                'id': 'p1', 'title': 'One', 'arxiv_id': '2608.00001',
                'paper_hash': 'h1', 'parsed_text': 'body',
                'parsed_text_length': 80_000,
                'parser_version': 'v1', 'page_count': 2, 'folder_id': 'f',
                'created_at': 1, 'updated_at': 2,
            }]

    got = operations._paper_library_inputs(Session(), {
        'user_id': 7,
        'arxiv_ids': ['2608.00001', '2608.00001', '2608.00002'],
        'max_text_chars': 6_000,
    })
    assert len(got) == 1 and got[0]['parsedText'] == 'body'
    assert got[0]['parsedTextLength'] == 80_000
    assert captured['args'] == (
        6_000, 7, '2608.00001', '2608.00002')
    assert 'user_id=?' in captured['sql']
    assert 'substr(parsed_text, 1, ?)' in captured['sql']
    assert 'qa_history' not in captured['sql']
    assert 'images' not in captured['sql']
    assert 'paper_reports' not in captured['sql']

    with pytest.raises(StorageError, match='at most 40'):
        operations._paper_library_inputs(Session(), {
            'user_id': 7,
            'arxiv_ids': [f'2608.{index:05d}' for index in range(41)],
        })


def test_sidecar_identity_projection_bounds_text_before_owner_result():
    from lib.storage_sidecar import operations

    calls = []

    class Session:
        def fetch_one(self, sql, args):
            calls.append((sql, args))
            return {
                'title': 'Owned', 'arxiv_id': '2608.00001',
                'parsed_text': 'body', 'parsed_text_length': 90_000,
            }

    bounded = operations._paper_library_identity(Session(), {
        'user_id': 7, 'paper_hash': 'hash', 'max_text_chars': 120_000,
    })
    assert bounded['parsed_text_length'] == 90_000
    assert calls[0][1] == (120_000, 7, 'hash')
    assert 'substr(parsed_text, 1, ?)' in calls[0][0]
    assert 'WHERE user_id = ? AND paper_hash = ?' in calls[0][0]

    operations._paper_library_identity(Session(), {
        'user_id': 7, 'paper_hash': 'hash',
    })
    assert calls[1][1] == (7, 'hash')
    assert 'substr(' not in calls[1][0]


def test_bookshelf_summaries_are_light_and_compute_report_presence_once():
    from lib.storage_sidecar import operations

    captured = {}

    class Session:
        def fetch_all(self, sql, args):
            captured['sql'] = sql
            captured['args'] = args
            return [{
                'id': 'p1', 'title': 'One', 'pdf_url': '',
                'pdf_filename': '', 'arxiv_id': '2608.00001',
                'paper_hash': 'h1', 'page_count': 1, 'folder_id': '',
                'created_at': 1, 'updated_at': 2, 'has_report': 1,
            }]

        def fetch_one(self, *args, **kwargs):
            raise AssertionError('bookshelf projection must not query per row')

    got = operations._paper_library_summaries(Session(), {'user_id': 7})
    assert got[0]['hasReport'] is True
    assert captured['args'] == (7,)
    assert 'EXISTS(SELECT 1 FROM paper_reports' in captured['sql']
    assert 'report.user_id=library.user_id' in captured['sql']
    assert 'parsed_text' not in captured['sql']
    assert 'qa_history' not in captured['sql']
    assert 'images' not in captured['sql']
    assert 'babel_cache' not in captured['sql']
    assert not {'parsedText', 'qaHistory', 'images', 'babelCache'} & got[0].keys()


def test_compatibility_bookshelf_list_keeps_one_statement_full_projection():
    from lib.storage_sidecar import operations

    class Session:
        def fetch_all(self, _sql, args):
            assert args == (7,)
            return [{
                'id': 'p1', 'title': 'One', 'pdf_url': '',
                'pdf_filename': '', 'arxiv_id': '2608.00001',
                'paper_hash': 'h1', 'parsed_text': 'body',
                'qa_history': '[]', 'images': '[]', 'babel_cache': '{}',
                'page_count': 1, 'folder_id': '', 'parser_version': 'v1',
                'created_at': 1, 'updated_at': 2, 'has_report': 1,
            }]

        def fetch_one(self, *args, **kwargs):
            raise AssertionError('compatibility list must not query per row')

    got = operations._paper_library_list(Session(), {'user_id': 7})
    assert got[0]['parsedText'] == 'body'
    assert got[0]['hasReport'] is True


def test_bookshelf_detail_reads_one_owned_complete_row():
    from lib.storage_sidecar import operations

    captured = {}

    class Session:
        def fetch_one(self, sql, args):
            captured['sql'] = sql
            captured['args'] = args
            return {
                'id': 'p1', 'title': 'One', 'pdf_url': '/p.pdf',
                'pdf_filename': 'p.pdf', 'arxiv_id': '2608.00001',
                'paper_hash': 'h1', 'parsed_text': 'body',
                'qa_history': '[{"q":"why"}]', 'images': '[]',
                'babel_cache': '{}', 'page_count': 1, 'folder_id': '',
                'parser_version': 'v1', 'created_at': 1, 'updated_at': 2,
                'has_report': 1,
            }

        def fetch_all(self, *args, **kwargs):
            raise AssertionError('one-paper detail must not scan the bookshelf')

    got = operations._paper_library_get(
        Session(), {'user_id': 7, 'id': 'p1'})
    assert captured['args'] == (7, 'p1')
    assert 'library.user_id = ? AND library.id = ?' in captured['sql']
    assert got['parsedText'] == 'body'
    assert got['qaHistory'] == [{'q': 'why'}]
    assert got['hasReport'] is True


def test_reader_detail_never_selects_or_decodes_legacy_babel_cache():
    from lib.storage_sidecar import operations

    captured = {}

    class Session:
        def fetch_one(self, sql, args):
            captured['sql'] = sql
            captured['args'] = args
            return {
                'id': 'p1', 'title': 'One', 'pdf_url': '/p.pdf',
                'pdf_filename': 'p.pdf', 'arxiv_id': '2608.00001',
                'paper_hash': 'h1', 'parsed_text': 'body',
                'qa_history': '[]', 'images': '[]', 'page_count': 1,
                'folder_id': '', 'parser_version': 'v1', 'created_at': 1,
                'updated_at': 2, 'has_report': 0,
            }

    got = operations._paper_library_reader(
        Session(), {'user_id': 7, 'id': 'p1'})

    assert captured['args'] == (7, 'p1')
    assert 'babel_cache' not in captured['sql']
    assert 'babelCache' not in got
    assert got['parsedText'] == 'body'


def test_sidecar_report_excerpt_omits_unneeded_large_metadata():
    from lib.storage_sidecar import operations

    captured = {}

    class Session:
        def fetch_one(self, sql, args):
            captured['sql'] = sql
            captured['args'] = args
            return {
                'report': 'excerpt', 'model': '', 'meta': '{}',
                'created_at': 1,
            }

    got = operations._paper_report_get(Session(), {
        'user_id': 7, 'paper_hash': 'hash', 'lang': 'en',
        'max_report_chars': 6_000,
    })
    assert got['report'] == 'excerpt' and got['meta'] == {}
    assert captured['args'] == (6_000, 7, 'hash', 'en')
    assert 'substr(report, 1, ?)' in captured['sql']
    assert "'{}' AS meta" in captured['sql']


def test_sidecar_report_excerpts_batch_is_owner_scoped_and_text_only():
    from lib.storage_sidecar import operations

    captured = {}

    class Session:
        def fetch_all(self, sql, args):
            captured['sql'] = sql
            captured['args'] = args
            return [{
                'paper_hash': 'hash-1', 'report': 'excerpt',
                'created_at': 1,
            }]

    got = operations._paper_report_excerpts(Session(), {
        'user_id': 7, 'lang': 'en',
        'paper_hashes': ['hash-1', 'hash-1', 'hash-2'],
        'max_report_chars': 6_000,
    })
    assert got == [{
        'user_id': 7, 'paper_hash': 'hash-1', 'lang': 'en',
        'report': 'excerpt', 'created_at': 1,
    }]
    assert captured['args'] == (6_000, 7, 'en', 'hash-1', 'hash-2')
    assert 'user_id=? AND lang=?' in captured['sql']
    assert 'substr(report, 1, ?)' in captured['sql']
    assert 'model' not in captured['sql'] and 'meta' not in captured['sql']


def test_sidecar_report_reopen_is_one_owner_scoped_query(monkeypatch):
    from lib.storage_sidecar import operations
    from lib.storage_sidecar.operations_pkg import _papers

    calls = []
    original_load = _papers._load

    def load_selected_metadata(raw):
        if raw == 'unused-metadata-must-not-decode':
            raise AssertionError('unused fallback sibling metadata was decoded')
        return original_load(raw)

    monkeypatch.setattr(_papers, '_load', load_selected_metadata)

    class Session:
        def fetch_all(self, sql, args):
            calls.append((sql, args))
            return [
                {
                    'lang': 'insight:zh', 'report': 'unused insight',
                    'model': '', 'meta': 'unused-metadata-must-not-decode',
                    'created_at': 4,
                },
                {
                    'lang': 'en', 'report': 'base', 'model': 'm',
                    'meta': '{}', 'created_at': 1,
                },
                {
                    'lang': 'insight:en', 'report': 'insight', 'model': '',
                    'meta': '{"items": {}}', 'created_at': 2,
                },
                {
                    'lang': 'checkpoints:en', 'report': '', 'model': '',
                    'meta': '{"items": []}', 'created_at': 3,
                },
            ]

        def fetch_one(self, *_args, **_kwargs):
            raise AssertionError('reopen split one aggregate into extra reads')

    got = operations._paper_report_reopen(Session(), {
        'user_id': 7,
        'paper_hash': 'hash',
        'preferred_lang': 'zh',
        'fallback_lang': 'en',
        'sibling_langs_by_base': {
            'zh': ['insight:zh'],
            'en': ['insight:en', 'checkpoints:en'],
        },
    })

    assert len(calls) == 1
    assert calls[0][1] == (
        7, 'hash', 'zh', 'en', 'zh', 7, 'hash',
        'zh', 'insight:zh', 'en', 'insight:en', 'checkpoints:en')
    assert calls[0][0].count('SELECT') == 2
    assert 'WITH resolved_lang AS' in calls[0][0]
    assert 'p.user_id = ? AND p.paper_hash = ?' in calls[0][0]
    assert got['report']['lang'] == 'en'
    assert [row['lang'] for row in got['siblings']] == [
        'insight:en', 'checkpoints:en']
    assert all(row['report'] != 'unused insight' for row in got['siblings'])


def test_survey_requests_only_bounded_target_bodies(monkeypatch):
    import lib.paper.artifact_repository as artifacts
    import lib.paper.library_repository as library
    import lib.paper.survey as survey

    requested = []

    def targeted(self, arxiv_ids, *, max_text_chars=0):
        requested.extend(arxiv_ids)
        assert max_text_chars == 6_000
        return [
            library.PaperLibraryEntry(
                paper_id=f'p-{arxiv_id}', title=f'Paper {arxiv_id}',
                arxiv_id=arxiv_id, paper_hash=f'h-{arxiv_id}',
                parsed_text='x' * max_text_chars,
                parsed_text_length=10_000,
            )
            for arxiv_id in arxiv_ids
        ]

    monkeypatch.setattr(library.PaperLibraryRepository, 'by_arxiv_ids', targeted)
    monkeypatch.setattr(
        library.PaperLibraryRepository, 'list_entries',
        lambda self: (_ for _ in ()).throw(
            AssertionError('survey must not scan the full bookshelf')))
    report_batches = []

    def report_excerpts(self, paper_hashes, lang, *, max_chars):
        report_batches.append((list(paper_hashes), lang, max_chars))
        return {}

    monkeypatch.setattr(
        artifacts.PaperArtifactRepository, 'report_excerpts', report_excerpts)
    monkeypatch.setattr(
        artifacts.PaperArtifactRepository, 'get_report',
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError('survey must not read reports one at a time')))

    offered = [f'2608.{index:05d}' for index in range(10_000)]
    loaded = survey._load_paper_inputs(
        offered, lang='en', user_id=7,
        max_papers=1_000_000, per_paper_chars=1_000_000)
    assert requested == offered[:40]
    assert report_batches == [(
        [f'h-{arxiv_id}' for arxiv_id in offered[:40]], 'en', 6_000)]
    assert len(loaded) == 40
    assert all(len(row['content']) == 6_000 for row in loaded)


def test_survey_library_gate_resolves_only_bounded_referenced_ids(monkeypatch):
    import lib.paper.library_repository as library
    import lib.paper.survey as survey

    requested = []

    def targeted(self, arxiv_ids, *, max_text_chars=0):
        requested.extend(arxiv_ids)
        assert max_text_chars == 0
        return [
            library.PaperLibraryEntry(
                paper_id=f'p-{arxiv_id}', arxiv_id=arxiv_id,
                folder_id='wanted')
            for arxiv_id in arxiv_ids
        ]

    monkeypatch.setattr(library.PaperLibraryRepository, 'by_arxiv_ids', targeted)
    monkeypatch.setattr(
        library.PaperLibraryRepository, 'list_entries',
        lambda self: (_ for _ in ()).throw(
            AssertionError('library gate must not scan the full bookshelf')))
    offered = [f'2608.{index:05d}' for index in range(10_000)]
    got = survey._library_id_set(
        offered, user_id=7, folder_id='wanted')
    assert requested == offered[:40]
    assert got == set(offered[:40])


def test_survey_batch_report_failure_falls_back_without_per_paper_rpc(
        monkeypatch):
    import lib.paper.artifact_repository as artifacts
    import lib.paper.library_repository as library
    import lib.paper.survey as survey

    monkeypatch.setattr(
        library.PaperLibraryRepository, 'by_arxiv_ids',
        lambda self, arxiv_ids, **kwargs: [library.PaperLibraryEntry(
            paper_id='p1', arxiv_id='2608.00001', paper_hash='h1',
            parsed_text='bounded body')])
    monkeypatch.setattr(
        artifacts.PaperArtifactRepository, 'report_excerpts',
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError('sidecar unavailable')))
    monkeypatch.setattr(
        artifacts.PaperArtifactRepository, 'get_report',
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError('batch failure must not fan out per-paper RPCs')))

    got = survey._load_paper_inputs(
        ['2608.00001'], lang='en', user_id=7)
    assert got[0]['source'] == 'parsed_text'
    assert got[0]['content'] == 'bounded body'


def test_batch_harvest_prefetches_once_and_skips_per_paper_probe(monkeypatch):
    import lib.paper.harvest as harvest

    ids = [f'2608.{index:05d}' for index in range(20)]
    probes = []

    def prefetch(arxiv_ids, user_id):
        probes.append((list(arxiv_ids), user_id))
        return {
            arxiv_id: {
                'id': f'p-{arxiv_id}', 'paper_hash': f'h-{arxiv_id}',
                'title': arxiv_id, 'parsed_text': 'body', 'page_count': 1,
            }
            for arxiv_id in arxiv_ids
        }

    monkeypatch.setattr(harvest, '_existing_rows_for_arxiv_ids', prefetch)
    monkeypatch.setattr(
        harvest, 'harvest_arxiv_id',
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError('a prefetched cache hit must not probe/download')))

    result = harvest.harvest_arxiv_batch(ids, user_id=7)
    assert probes == [(ids, 7)]
    assert result['cache_hits'] == 20
    assert result['parsed'] == result['errors'] == 0


def test_batch_harvest_prefetch_miss_bypasses_duplicate_lookup(monkeypatch):
    import lib.paper.harvest as harvest

    calls = []
    monkeypatch.setattr(
        harvest, '_existing_rows_for_arxiv_ids', lambda arxiv_ids, user_id: {})

    def one(arxiv_id, **kwargs):
        calls.append((
            arxiv_id, kwargs.get('force_reparse'),
            kwargs.get('title_hint'), kwargs.get('allow_title_lookup')))
        return harvest.HarvestResult(arxiv_id, status='parsed')

    monkeypatch.setattr(harvest, 'harvest_arxiv_id', one)
    result = harvest.harvest_arxiv_batch(
        ['2608.00001', '2608.00002'], user_id=7,
        titles_by_arxiv_id={
            '2608.00001': 'One', '2608.00002': 'Two',
        })
    assert calls == [
        ('2608.00001', True, 'One', False),
        ('2608.00002', True, 'Two', False),
    ]
    assert result['parsed'] == 2


def test_batch_harvest_resolves_missing_titles_once_without_serial_fallback(
        monkeypatch):
    import lib.paper.arxiv as arxiv
    import lib.paper.harvest as harvest

    ids = ['2608.00001', '2608.00002']
    title_batches = []
    calls = []
    monkeypatch.setattr(
        harvest, '_existing_rows_for_arxiv_ids', lambda arxiv_ids, user_id: {})

    def titles(arxiv_ids):
        title_batches.append(list(arxiv_ids))
        return {'2608.00001': 'One'}

    def one(arxiv_id, **kwargs):
        calls.append((
            arxiv_id, kwargs['title_hint'], kwargs['allow_title_lookup']))
        return harvest.HarvestResult(
            arxiv_id, status='parsed',
            title=kwargs['title_hint'] or f'arXiv:{arxiv_id}')

    monkeypatch.setattr(arxiv, 'fetch_arxiv_titles_batch', titles)
    monkeypatch.setattr(harvest, 'harvest_arxiv_id', one)
    result = harvest.harvest_arxiv_batch(ids, user_id=7)

    assert title_batches == [ids]
    assert calls == [
        ('2608.00001', 'One', False),
        ('2608.00002', '', False),
    ]
    assert result['parsed'] == 2


def test_batch_harvest_rejects_oversize_before_storage_or_network(monkeypatch):
    import lib.paper.harvest as harvest

    consumed = []

    def offered():
        for index in range(10_000):
            consumed.append(index)
            yield f'2608.{index:05d}'

    monkeypatch.setattr(
        harvest, '_existing_rows_for_arxiv_ids',
        lambda *_args: (_ for _ in ()).throw(
            AssertionError('oversize input must fail before storage')))
    with pytest.raises(ValueError, match='at most 40'):
        harvest.harvest_arxiv_batch(offered(), user_id=7)
    assert consumed == list(range(41))
