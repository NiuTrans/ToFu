"""Complete default text and actual document HTTP source acceptance."""
import pytest

from lib.doc_parser import extract_document_text

pytestmark = pytest.mark.unit


def test_default_document_text_has_no_private_character_ceiling():
    text = 'original evidence ' + 'x' * 2_100_000 + ' tail_document_marker'
    result = extract_document_text(text.encode(), 'large.txt')
    assert result['text'] == text
    assert result['textLength'] == len(text)
    assert not result['warnings']
    bounded = extract_document_text(text.encode(), 'large.txt', max_chars=128)
    assert bounded['text'] == text[:128]
    assert bounded['warnings']


@pytest.mark.auth_mode('open')
def test_document_route_receives_actual_source_above_http_and_fetch_caps(flask_client):
    raw = b'Original evidence ' + b'x' * (51 * 1024 * 1024) + b' tail_document_marker'
    boundary = 'owned-document-source'
    head = (f'--{boundary}\r\nContent-Disposition: form-data; name="maxTextChars"\r\n\r\n128\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="large.txt"\r\n'
            'Content-Type: text/plain\r\n\r\n').encode()
    body = head + raw + f'\r\n--{boundary}--\r\n'.encode()
    response = flask_client.post('/api/doc/parse', data=body,
        headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload['fileSize'] == len(raw)
    assert payload['text'] == raw[:128].decode()
    assert str(len(raw)) in str(payload['warnings']).replace(',', '')


@pytest.mark.auth_mode('open')
def test_document_receive_failure_is_redacted(flask_client, monkeypatch):
    from types import SimpleNamespace
    import routes.upload as upload
    class FailedSource:
        def read(self):
            raise OSError('private receive details')
    monkeypatch.setattr(upload, 'request_files', lambda: {
        'file': SimpleNamespace(filename='source.txt', read=FailedSource().read)})
    response = flask_client.post('/api/doc/parse')
    assert response.status_code == 500
    assert 'private receive details' not in str(response.get_json())


def test_sparse_wide_table_does_not_expand_missing_cells():
    from lib.doc_parser._tables import render_markdown_table
    rows = [[f'row-{i}'] for i in range(1000)]
    rows[-1] += [None] * 1998 + ['far_column_evidence']
    rendered = render_markdown_table(rows)
    assert 'far_column_evidence' in rendered
    assert '| 1000 | 2000 | far_column_evidence |' in rendered
    assert len(rendered) < 100_000
    assert all(f'row-{i}' in rendered for i in range(1000))


def test_workbook_closes_when_traversal_fails(monkeypatch):
    import io
    openpyxl = pytest.importorskip('openpyxl')
    authored = openpyxl.Workbook()
    authored.active.append(['original'])
    buffer = io.BytesIO()
    authored.save(buffer)
    raw = buffer.getvalue()
    workbook = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    closed = []
    original_close = workbook.close
    def close():
        closed.append(True)
        original_close()
    def fail(*args, **kwargs):
        raise OSError('actual traversal failure')
    monkeypatch.setattr(workbook, 'close', close)
    monkeypatch.setattr(workbook.active, 'iter_rows', fail)
    monkeypatch.setattr(openpyxl, 'load_workbook', lambda *args, **kwargs: workbook)
    with pytest.raises(OSError, match='actual traversal failure'):
        extract_document_text(raw, 'source.xlsx')
    assert closed == [True]
