"""Real multi-page text is complete by default; explicit output cuts stay exact."""
import pytest

pytestmark=pytest.mark.unit


@pytest.fixture(scope='module')
def long_pdf():
    from lib.pdf_parser import text
    if text.pymupdf is None:
        pytest.skip('requires a working real PyMuPDF backend')
    document=text.pymupdf.open()
    for page_number in range(220):
        page=document.new_page(width=600,height=1000)
        lines=[f'PAGE_{page_number:03d}_LINE_{line_number:03d} '+'source '*9 for line_number in range(100)]
        page.insert_text((10,10),lines,fontname='cour',fontsize=6)
    raw=document.tobytes(deflate=True);document.close()
    return raw


def test_default_pdf_text_and_core_include_tail_after_retired_mib_quota(long_pdf,monkeypatch):
    from lib.pdf_parser.text import extract_pdf_text
    from lib.pdf_parser.core import parse_pdf
    monkeypatch.setenv('TOFU_PDF_MAX_TEXT_MIB','1')
    full=extract_pdf_text(long_pdf,mode='fast')
    assert len(full)>1024*1024
    assert 'PAGE_219_LINE_099' in full
    for page_number in range(220):
        assert f'PAGE_{page_number:03d}_LINE_000' in full
    result=parse_pdf(long_pdf,text_mode='fast',max_images=0)
    assert 'PAGE_219_LINE_099' in result['text']
    assert result['processedPages']==result['totalPages']==220
    assert result['truncated'] is False
    assert result['limits']['maxTextChars']==0


def test_explicit_text_limit_above_old_process_quota_is_not_clamped(long_pdf,monkeypatch):
    from lib.pdf_parser.core import parse_pdf
    monkeypatch.setenv('TOFU_PDF_MAX_TEXT_MIB','1')
    requested=1200000
    result=parse_pdf(long_pdf,text_mode='fast',max_images=0,max_text_chars=requested)
    assert result['limits']['maxTextChars']==requested
    assert 1024*1024<len(result['text'])<=requested
    assert result['truncated'] is True
