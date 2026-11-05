"""Citation continuation is source-bound, bounded and honest about coverage."""
from pathlib import Path

import pytest

from lib.paper import citations
from lib.paper.builds.errors import BuildError
from lib.tasks_pkg.handlers.paper_citations import _audit_verdict
from tests.paper.test_paper_citations import _Resp, _work

pytestmark = pytest.mark.unit


@pytest.fixture
def document(tmp_path, monkeypatch):
    (tmp_path / 'main.tex').write_text('\\bibliography{refs}\n')
    (tmp_path / 'refs.bib').write_text('\n'.join(
        f'@article{{paper{n}, title={{Synthetic paper {n}}}, year={{2025}}}}' for n in range(19)))
    calls = []

    def verify(entry):
        calls.append(entry['key'])
        return {'key': entry['key'], 'status': 'verified', 'evidence': {}}

    monkeypatch.setattr(citations, '_verify_one', verify)
    monkeypatch.setattr(citations, '_VERIFY_POLITE_DELAY_SECONDS', 0)
    return str(tmp_path), calls


def test_all_entries_are_reachable_without_rechecking_earlier_pages(document):
    base, calls = document
    first = citations.audit_document(base, main_file='main.tex')
    second = citations.audit_document(base, main_file='main.tex', cursor=first['next_cursor'])
    last = citations.audit_document(base, main_file='main.tex', cursor=second['next_cursor'])
    assert calls == [f'paper{n}' for n in range(19)]
    assert [len(page['verifications']) for page in (first, second, last)] == [8, 8, 3]
    assert len({page['source_digest'] for page in (first, second, last)}) == 1
    assert last['next_cursor'] is None
    assert last['coverage']['start'] == 16 and last['coverage']['end'] == 19
    assert not last['coverage']['complete']  # A cursor does not certify prior pages.


@pytest.mark.parametrize('edit', ['bib', 'tex', 'add', 'main', 'root'])
def test_continuation_rejects_changed_or_foreign_source_before_network(document, tmp_path, edit):
    base, calls = document
    first = citations.audit_document(base, main_file='main.tex')
    main = 'main.tex'
    if edit == 'bib':
        (tmp_path / 'refs.bib').write_text((tmp_path / 'refs.bib').read_text() + '\n% edit\n')
    elif edit == 'tex':
        (tmp_path / 'main.tex').write_text('Changed manuscript\n')
    elif edit == 'add':
        (tmp_path / 'extra.bib').write_text('@book{extra,title={Extra}}')
    elif edit == 'main':
        main = 'other.tex'
    else:
        other = tmp_path / 'other'
        other.mkdir()
        for name in ('main.tex', 'refs.bib'):
            (other / name).write_text((tmp_path / name).read_text())
        base = str(other)
    calls.clear()
    with pytest.raises(BuildError) as failure:
        citations.audit_document(base, main_file=main, cursor=first['next_cursor'])
    assert failure.value.code == 'citation_source_changed'
    assert calls == []


def test_source_change_during_remote_verification_refuses_stale_result(document, monkeypatch):
    base, _ = document

    def edit(entry):
        Path(base, 'main.tex').write_text('changed while verifying\n')
        return {'key': entry['key'], 'status': 'verified', 'evidence': {}}

    monkeypatch.setattr(citations, '_verify_one', edit)
    with pytest.raises(BuildError) as failure:
        citations.audit_document(base, main_file='main.tex')
    assert failure.value.code == 'citation_source_changed'


def test_unavailable_rows_have_retry_cursor_and_never_certify_the_document(document, monkeypatch):
    base, _ = document

    def unavailable(_entry):
        raise ConnectionError('offline')

    monkeypatch.setattr(citations, '_verify_one', unavailable)
    report = citations.audit_document(base, main_file='main.tex')
    assert report['retry_cursor'] == report['cursor']
    assert all(row['status'] == 'skipped' for row in report['verifications'])
    assert not report['coverage']['complete']
    text, status = _audit_verdict(base, 'main.tex', cursor=report['cursor'])
    assert status == 'done' and '引用核验未完成' in text
    assert report['next_cursor'] in text and report['retry_cursor'] in text


def test_deadline_continues_at_first_unattempted_entry(document, monkeypatch):
    base, calls = document
    now = [0.0]
    monkeypatch.setattr(citations.time, 'monotonic', lambda: now[0])

    def slow(entry):
        calls.append(entry['key'])
        now[0] += 13
        return {'key': entry['key'], 'status': 'verified', 'evidence': {}}

    monkeypatch.setattr(citations, '_verify_one', slow)
    first = citations.audit_document(base, main_file='main.tex')
    assert calls == ['paper0', 'paper1']
    assert first['coverage']['end'] == 2
    citations.audit_document(base, main_file='main.tex', cursor=first['next_cursor'])
    assert calls == ['paper0', 'paper1', 'paper2', 'paper3']


@pytest.mark.parametrize('cursor', [True, 1, 'nonsense', 'c1:' + 'a' * 64 + ':-1', 'x' * 1000])
def test_malformed_cursor_fails_before_network(document, cursor):
    base, calls = document
    with pytest.raises(BuildError) as failure:
        citations.audit_document(base, cursor=cursor)
    assert failure.value.code == 'citation_cursor' and calls == []


def test_scan_limit_never_reports_incomplete_local_sources_as_all_clear(document, monkeypatch):
    base, calls = document
    monkeypatch.setattr(citations, '_MAX_SOURCE_FILES', 1)
    with pytest.raises(BuildError) as failure:
        citations.audit_document(base)
    assert failure.value.code == 'source_limit' and calls == []


def test_unreadable_bibliography_is_a_typed_failure(document, monkeypatch):
    base, calls = document
    original = citations._read_source_bytes

    def unreadable(root, rel, cap):
        if rel.endswith('.bib'):
            raise OSError('unreadable source')
        return original(root, rel, cap)

    monkeypatch.setattr(citations, '_read_source_bytes', unreadable)
    with pytest.raises(BuildError) as failure:
        citations.audit_document(base)
    assert failure.value.code == 'source_path' and calls == []


@pytest.mark.parametrize('limit', ['_MAX_SOURCE_BYTES', 'MAX_BIB_ENTRIES'])
def test_source_budget_cannot_silently_shorten_the_audited_document(document, monkeypatch, limit):
    base, calls = document
    monkeypatch.setattr(citations, limit, 3)
    with pytest.raises(BuildError) as failure:
        citations.audit_document(base)
    assert failure.value.code in ('source_limit', 'bib_limit') and calls == []


def test_audit_refreshes_cached_work_and_restores_network_deadline(monkeypatch):
    citations._cached_openalex_work.cache_clear()
    work = _work(doi='https://doi.org/10.1/example')
    requests = []

    def get(url, **kwargs):
        requests.append(kwargs['timeout'])
        return _Resp(200, dict(work))

    monkeypatch.setattr(citations, 'http_get', get)
    citations._openalex_work_by_doi('10.1/example')
    work['is_retracted'] = True
    rows = citations.verify_entries([{'key': 'paper', 'doi': '10.1/example', 'title': '', 'year': '', 'author': ''}])
    assert rows[0]['status'] == 'retracted' and len(requests) == 2
    assert citations._verification_deadline.get() is None
    citations._cached_openalex_work.cache_clear()


def test_http_timeout_borrows_remaining_page_budget(monkeypatch):
    now = [0.0]
    timeouts = []

    def get(url, **kwargs):
        timeouts.append(kwargs['timeout'])
        now[0] += 13
        return _Resp(200, _work())

    monkeypatch.setattr(citations.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(citations, 'http_get', get)
    monkeypatch.setattr(citations, '_VERIFY_POLITE_DELAY_SECONDS', 0)
    rows = citations.verify_entries([{'key': str(n), 'doi': f'10.1/{n}'} for n in range(3)])
    assert timeouts == [20.0, 12.0]
    assert rows[-1]['status'] == 'skipped' and rows[-1]['reason'] == 'deadline'
    assert citations._verification_deadline.get() is None


def test_malformed_backend_payload_stays_unverified(monkeypatch):
    monkeypatch.setattr(citations, 'http_get', lambda *args, **kwargs: _Resp(200, []))
    rows = citations.verify_entries([{'key': 'paper', 'doi': '10.1/example'}])
    assert rows[0]['status'] == 'skipped'


def test_empty_bibliography_is_not_an_authenticity_verdict(tmp_path):
    (tmp_path / 'main.tex').write_text('No citations yet\n')
    text, status = _audit_verdict(str(tmp_path), 'main.tex')
    assert status == 'done' and '没有可进行外部核验' in text
    assert '全部已核验条目均为真实文献' not in text
