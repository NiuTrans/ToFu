"""PDF uploads are byte-bounded and invisible until fully validated."""

import io

import pytest

pytestmark = pytest.mark.unit


def test_oversized_upload_leaves_no_final_or_partial(monkeypatch, tmp_path):
    import routes.paper_pkg._common as paper

    monkeypatch.setattr(paper, '_paper_pdf_limit', lambda: 8)
    final = tmp_path / 'paper.pdf'
    with pytest.raises(paper._PaperDownloadTooLarge):
        paper._store_uploaded_pdf_atomic(io.BytesIO(b'123456789'), str(final))

    assert not final.exists()
    assert list(tmp_path.iterdir()) == []


def test_invalid_upload_cannot_replace_existing_file(monkeypatch, tmp_path):
    import lib.pdf_parser.text as pdf_text
    import routes.paper_pkg._common as paper

    monkeypatch.setattr(paper, '_paper_pdf_limit', lambda: 1024)
    monkeypatch.setattr(
        pdf_text, 'validate_pdf_bytes',
        lambda _body: (False, 0, 'test rejection'),
    )
    final = tmp_path / 'paper.pdf'
    final.write_bytes(b'old-good-file')

    with pytest.raises(paper._PaperInvalidPDF, match='test rejection'):
        paper._store_uploaded_pdf_atomic(io.BytesIO(b'bad-new-file'), str(final))

    assert final.read_bytes() == b'old-good-file'
    assert [p.name for p in tmp_path.iterdir()] == ['paper.pdf']


def test_valid_upload_is_atomically_published(monkeypatch, tmp_path):
    import lib.pdf_parser.text as pdf_text
    import routes.paper_pkg._common as paper

    body = b'%PDF-valid-test-body'
    monkeypatch.setattr(paper, '_paper_pdf_limit', lambda: 1024)
    monkeypatch.setattr(
        pdf_text, 'validate_pdf_bytes',
        lambda candidate: (candidate == body, 1, ''),
    )
    final = tmp_path / 'paper.pdf'

    returned = paper._store_uploaded_pdf_atomic(io.BytesIO(body), str(final))

    assert returned == body
    assert final.read_bytes() == body
    assert [p.name for p in tmp_path.iterdir()] == ['paper.pdf']
