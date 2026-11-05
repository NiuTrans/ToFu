"""The base distribution reads PDF text locally without native/OCR dependencies."""

import io

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from test_lazy_pdf_dependencies import run_isolated

from tofu_search import configure, fetch_page_content
from tofu_search.fetch import core, pdf_extract

pytestmark = pytest.mark.unit


def make_pdf(*page_streams, password=None, unicode_map=False, compressed=False):
    """Construct synthetic PDF source operators, independently of extraction."""
    writer = PdfWriter()
    font = DictionaryObject({
        NameObject('/Type'): NameObject('/Font'),
        NameObject('/Subtype'): NameObject('/Type1'),
        NameObject('/BaseFont'): NameObject('/Helvetica'),
    })
    if unicode_map:
        cmap = DecodedStreamObject()
        cmap.set_data(b'''/CIDInit /ProcSet findresource begin 12 dict begin
begincmap /CMapName /SyntheticChinese def /CMapType 2 def
1 begincodespacerange <00> <FF> endcodespacerange
2 beginbfchar <41> <4E2D> <42> <6587> endbfchar
endcmap CMapName currentdict /CMap defineresource pop end end''')
        font[NameObject('/ToUnicode')] = writer._add_object(cmap)
    for operators in page_streams:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject('/Resources')] = DictionaryObject({
            NameObject('/Font'): DictionaryObject({NameObject('/F1'): font}),
        })
        stream = DecodedStreamObject()
        stream.set_data(operators)
        if compressed:
            stream = stream.flate_encode()
        page[NameObject('/Contents')] = writer._add_object(stream)
    if password is not None:
        writer.encrypt(password, algorithm='RC4-40')
    output = io.BytesIO()
    writer.write(output)
    writer.close()
    return output.getvalue()


@pytest.mark.parametrize('native_failure', ['ModuleNotFoundError', 'ImportError', 'OSError'])
def test_default_reader_survives_absent_or_unloadable_native_dependencies(native_failure):
    payload = make_pdf(b'BT /F1 12 Tf 72 720 Td (Reference number: 42.) Tj ET')
    run_isolated('PAYLOAD=' + repr(payload) + '\nFAILURE=' + native_failure + r'''
import importlib.abc,sys
class NativeUnavailable(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname.split('.')[0] in {'pymupdf','pymupdf4llm','onnxruntime','rapidocr'}:
            raise FAILURE('Synthetic absent module or incompatible native loader')
sys.meta_path.insert(0,NativeUnavailable())
from tofu_search.fetch.pdf_extract import extract_pdf_text
assert 'pypdf' not in sys.modules
assert extract_pdf_text(PAYLOAD).strip() == 'Reference number: 42.'
assert 'pypdf' in sys.modules
assert not any(name in sys.modules for name in ('pymupdf4llm','onnxruntime','rapidocr'))
''')


@pytest.fixture
def pure_reader(monkeypatch):
    import pypdf
    monkeypatch.setattr(pdf_extract, '_dependencies_loaded', True)
    monkeypatch.setattr(pdf_extract, '_pymupdf', None)
    monkeypatch.setattr(pdf_extract, '_pymupdf4llm', None)
    monkeypatch.setattr(pdf_extract, '_pypdf', pypdf, raising=False)
    return pypdf


def test_default_reader_preserves_unicode_rotated_text_numbers_and_small_budgets(pure_reader):
    payload = make_pdf(
        b'BT /F1 12 Tf 72 720 Td <4142> Tj 0 -20 Td (2010) Tj ET',
        b'BT /F1 12 Tf 0 1 -1 0 300 300 Tm <4142> Tj ET',
        unicode_map=True,
    )
    body = pdf_extract.extract_pdf_text(payload)
    assert body.count('中文') == 2
    assert '2010' in body
    assert pdf_extract.extract_pdf_text(payload, max_chars=2).startswith('中文\n[...truncated')


@pytest.mark.parametrize('kind', ['malformed', 'empty', 'scan', 'encrypted'])
def test_unreadable_default_pdf_stays_out_of_public_source_and_cache(monkeypatch, pure_reader, kind):
    payloads = {
        'malformed': b'%PDF-malformed',
        'empty': make_pdf(),
        'scan': make_pdf(b'0 0 100 100 re f'),
        'encrypted': make_pdf(b'BT /F1 12 Tf (Private source) Tj ET', password='secret'),
    }
    configure(block_private_addresses=False)
    from types import SimpleNamespace
    response = SimpleNamespace(headers={'Content-Type': 'application/pdf'}, encoding='utf-8')
    monkeypatch.setattr(core, '_do_request', lambda *a, **kw: (response, payloads[kind]))
    url = f'https://pdf.example/pure-{kind}.pdf'
    diag = {}
    assert fetch_page_content(url, diag=diag) is None
    assert diag['reason'] == 'pdf_extraction_error'
    assert core._fetch_cache.get(url) is None


def test_empty_password_pdf_is_readable_without_password_configuration(pure_reader):
    payload = make_pdf(b'BT /F1 12 Tf (Public source) Tj ET', password='')
    assert 'Public source' in pdf_extract.extract_pdf_text(payload)


@pytest.mark.parametrize('readable', [True, False])
def test_default_pdf_source_and_failure_through_mcp(monkeypatch, pure_reader, readable):
    pytest.importorskip('mcp')
    from types import SimpleNamespace

    from test_mcp_tool_calls import _sync_call

    from tofu_search.mcp_server.server import build_server

    configure(block_private_addresses=False)
    payload = (make_pdf(b'BT /F1 12 Tf (Reference number: 42.) Tj ET')
               if readable else b'%PDF-malformed')
    response = SimpleNamespace(headers={'Content-Type': 'application/pdf'}, encoding='utf-8')
    monkeypatch.setattr(core, '_do_request', lambda *a, **kw: (response, payload))
    text, _ = _sync_call(build_server(), 'fetch_page',
                         {'url': f'https://pdf.example/mcp-default-{readable}', 'max_chars': 200})
    if readable:
        assert 'Reference number: 42.' in text
    else:
        assert 'PDF' in text
        assert 'Reference number' not in text


@pytest.mark.parametrize('limit', [5, len('First-page source')])
def test_default_reader_closes_after_failure_and_stops_after_text_budget(monkeypatch, pure_reader, limit):
    closed, visited = [], []
    class Page:
        def __init__(self, index):
            self.index = index
        def extract_text(self):
            visited.append(self.index)
            if self.index:
                raise ValueError('later broken page')
            return 'First-page source'
    class Reader:
        def __init__(self, stream):
            self.pages = [Page(0), Page(1)]
        def close(self):
            closed.append(True)
    monkeypatch.setattr(pure_reader, 'PdfReader', Reader)
    assert pdf_extract.extract_pdf_text(b'synthetic', limit).startswith('First-page source'[:limit] + '\n[...truncated')
    assert visited == [0]
    assert closed == [True]
    with pytest.raises(pdf_extract.PDFExtractionError):
        pdf_extract.extract_pdf_text(b'synthetic')
    assert closed == [True, True]


def test_oversize_input_is_rejected_before_loading_pdf_libraries(monkeypatch):
    monkeypatch.setattr(pdf_extract, 'MAX_PDF_BYTES', 4)
    monkeypatch.setattr(pdf_extract, '_load_pdf_dependencies', lambda: pytest.fail('imported'))
    with pytest.raises(pdf_extract.PDFExtractionError, match='size limit'):
        pdf_extract.extract_pdf_text(b'%PDF-too-large')


def test_caller_decompression_limit_remains_active(pure_reader):
    from contextvars import copy_context

    from pypdf import get_configuration, overwrite_configuration
    before = get_configuration()
    payload = make_pdf(b'BT /F1 12 Tf (' + b'Source ' * 100 + b') Tj ET', compressed=True)
    def limited_read():
        overwrite_configuration(zlib_maximum_output_length=32)
        with pytest.raises(pdf_extract.PDFExtractionError):
            pdf_extract.extract_pdf_text(payload)
        assert get_configuration().zlib_maximum_output_length == 32
    copy_context().run(limited_read)
    assert get_configuration() == before
