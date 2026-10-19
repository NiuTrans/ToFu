"""Actual large PDF source validation, extraction and atomic publication."""
import hashlib
import io

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(scope='module')
def large_pdf():
    import lib.pdf_parser.text as parser
    if parser.pymupdf is None:
        pytest.skip('requires a working real PyMuPDF backend')
    output = io.BytesIO()
    output.write(b'%PDF-1.4\n')
    offsets = [0]
    content = b'BT /F1 12 Tf 30 80 Td (Full PDF source marker) Tj ET'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>',
               b'<< /Length ' + str(len(content)).encode() + b' >>\nstream\n' + content + b'\nendstream',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    for number, body in enumerate(objects, 1):
        offsets.append(output.tell())
        output.write(f'{number} 0 obj\n'.encode() + body + b'\nendobj\n')
    offsets.append(output.tell())
    size = 201 * 1024 * 1024
    output.write(f'6 0 obj\n<< /Length {size} >>\nstream\n'.encode())
    chunk = b'x' * (1024 * 1024)
    for _ in range(201):
        output.write(chunk)
    output.write(b'\nendstream\nendobj\n')
    xref = output.tell()
    output.write(b'xref\n0 7\n0000000000 65535 f \n')
    for offset in offsets[1:]:
        output.write(f'{offset:010d} 00000 n \n'.encode())
    output.write(f'trailer\n<< /Size 7 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    raw = output.getvalue()
    assert len(raw) > 200 * 1024 * 1024
    assert parser.validate_pdf_bytes(raw) == (True, 1, '')
    return raw


def test_large_pdf_has_real_text_and_atomic_exact_source(large_pdf, tmp_path):
    from lib.pdf_parser.text import extract_pdf_text
    from routes.paper_pkg._common import _store_uploaded_pdf_atomic
    text = extract_pdf_text(large_pdf, mode='fast')
    assert 'Full PDF source marker' in text
    final = tmp_path / 'complete.pdf'
    stored = _store_uploaded_pdf_atomic(io.BytesIO(large_pdf), str(final))
    expected = hashlib.sha256(large_pdf).digest()
    assert hashlib.sha256(stored).digest() == expected
    assert hashlib.sha256(final.read_bytes()).digest() == expected
    assert list(tmp_path.iterdir()) == [final]


def test_large_harvest_stream_accepts_complete_source_and_closes(large_pdf, monkeypatch):
    import lib.paper.harvest as harvest
    class Response:
        headers = {'Content-Length': str(len(large_pdf))}
        closed = False
        def raise_for_status(self):
            pass
        def iter_content(self, chunk_size):
            for offset in range(0, len(large_pdf), chunk_size):
                yield large_pdf[offset:offset + chunk_size]
        def close(self):
            self.closed = True
    response = Response()
    monkeypatch.setattr(harvest, 'http_get', lambda *a, **kw: response)
    raw = harvest._download_pdf_bytes('fixture')
    assert hashlib.sha256(raw).digest() == hashlib.sha256(large_pdf).digest()
    assert response.closed


@pytest.mark.auth_mode('open')
def test_large_pdf_actual_http_parse(large_pdf, flask_client):
    boundary = 'complete-pdf-source'
    prefix = (f'--{boundary}\r\nContent-Disposition: form-data; name="textMode"\r\n\r\nfast\r\n'
              f'--{boundary}\r\nContent-Disposition: form-data; name="maxImages"\r\n\r\n0\r\n'
              f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="complete.pdf"\r\n'
              'Content-Type: application/pdf\r\n\r\n').encode()
    response = flask_client.post('/api/pdf/parse', data=prefix + large_pdf +
        f'\r\n--{boundary}--\r\n'.encode(),
        headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    assert response.status_code == 200, response.get_json()
    assert 'Full PDF source marker' in response.get_json()['text']
