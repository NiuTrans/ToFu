"""Caller-owned HTML projection follows documents, nested formats and reindex."""
from email.message import EmailMessage
import io
import zipfile
import pytest

from lib.knowledge import ingest
from lib.knowledge.asset_spool import close_extracted_assets

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._knowledge_sidecar',)


def documents():
    html = b'<h1>Exact source</h1><p>body &amp; evidence</p>'
    book = io.BytesIO()
    with zipfile.ZipFile(book, 'w') as archive:
        archive.writestr('mimetype', 'application/epub+zip')
        archive.writestr('chapter.xhtml', html)
    email = EmailMessage()
    email['Subject'] = 'Projection'
    email.set_content(html.decode(), subtype='html')
    email.add_attachment(b'<h2>Attachment</h2>', maintype='text', subtype='html', filename='child.html')
    return [(html, 'document.html', 1), (book.getvalue(), 'book.epub', 1), (email.as_bytes(), 'message.eml', 2)]


@pytest.mark.parametrize('raw,name,expected_calls', documents(), ids=['html', 'epub', 'email'])
def test_explicit_projection_reaches_html_epub_and_email_without_global_mutation(raw, name, expected_calls):
    calls = []
    def project(value):
        calls.append(value)
        return 'projected-' + str(len(calls))
    result = ingest.extract(raw, name, html_projector=project)
    try:
        assert len(calls) == expected_calls
        assert all('projected-' + str(i) in result['text'] for i in range(1, expected_calls+1))
    finally: close_extracted_assets(result)
    plain = ingest.extract(b'<h1>Unchanged</h1>', 'ordinary.html')
    assert plain['text'] == '# Unchanged'


@pytest.mark.parametrize('failure', [RuntimeError('explicit failure'), KeyboardInterrupt('interrupted')])
def test_explicit_projection_failure_is_not_hidden_by_fallback(failure):
    def fail(_): raise failure
    with pytest.raises(type(failure)) as caught:
        ingest.extract(b'<p>visible</p>', 'doc.html', html_projector=fail)
    assert caught.value is failure
    with pytest.raises(TypeError, match='return a string'):
        ingest.extract(b'<p>visible</p>', 'doc.html', html_projector=lambda _: None)
    with pytest.raises(TypeError, match='callable'):
        ingest.extract(b'<p>visible</p>', 'doc.html', html_projector=False)


def test_nested_projection_interruption_closes_accepted_email_assets(monkeypatch):
    accepted = []
    sequence = ingest.OwnedAssetSequence
    def create():
        value = sequence()
        accepted.append(value)
        return value
    monkeypatch.setattr(ingest, 'OwnedAssetSequence', create)
    raw, name, _ = documents()[-1]
    calls = []
    failure = KeyboardInterrupt('nested projector interrupted')
    def project(value):
        calls.append(value)
        if len(calls) == 2: raise failure
        return 'body'
    with pytest.raises(KeyboardInterrupt) as caught:
        ingest.extract(raw, name, html_projector=project)
    assert caught.value is failure
    assert len(accepted) == 1 and accepted[0]._closed


def test_store_add_duplicate_and_reindex_borrow_the_projector(isolated_knowledge):
    calls = []
    def project(value): calls.append(value); return 'caller-owned exact text'
    raw = b'<h1>Stored original</h1>'
    created = isolated_knowledge.add_document(raw, 'file.html', user_id=1, html_projector=project)
    assert not created['duplicate'] and len(calls) == 1
    duplicate = isolated_knowledge.add_document(raw, 'file.html', user_id=1, html_projector=project)
    assert duplicate['duplicate'] and len(calls) == 1
    reindexed = isolated_knowledge.reindex_document(created['id'], user_id=1, html_projector=project)
    assert reindexed['id'] == created['id'] and len(calls) == 2
    page = isolated_knowledge.get_document_content(created['id'], user_id=1)
    assert 'caller-owned exact text' in '\n'.join(chunk['content'] for chunk in page['chunks'])
    before = page
    def fail(_): raise RuntimeError('projection refused')
    with pytest.raises(RuntimeError, match='projection refused'):
        isolated_knowledge.reindex_document(created['id'], user_id=1, html_projector=fail)
    assert isolated_knowledge.get_document_content(created['id'], user_id=1) == before
