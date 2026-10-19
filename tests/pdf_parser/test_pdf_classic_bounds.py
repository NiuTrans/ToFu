"""Page, text, and image amplification bounds for classic PDF extraction."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.unit


def _pdf_bytes(labels: list[str]) -> bytes:
    from lib.pdf_parser.text import pymupdf
    if pymupdf is None:
        pytest.skip('requires a working real PyMuPDF backend')
    document = pymupdf.open()
    for label in labels:
        page = document.new_page()
        page.insert_text((72, 72), label)
    payload = document.tobytes()
    document.close()
    return payload


def test_core_reports_page_truncation_and_preserves_image_request(monkeypatch):
    from lib.pdf_parser.core import parse_pdf

    monkeypatch.setenv('TOFU_PDF_MAX_PAGES', '2')
    monkeypatch.setenv('TOFU_PDF_MAX_TEXT_MIB', '1')
    result = parse_pdf(
        _pdf_bytes(['page one', 'page two', 'page three']),
        max_pages=2,
        max_images=999_999,
        max_image_width=999_999,
        text_mode='fast',
    )

    assert result['totalPages'] == 3
    assert result['processedPages'] == 2
    assert result['truncated'] is True
    assert '2 of 3 pages' in result['text']
    assert result['limits'] == {
        'maxPages': 2,
        'maxTextChars': 0,
        'maxImages': 999_999,
        'maxImageWidth': 999_999,
    }
    assert any('resource budget' in warning for warning in result['warnings'])


def test_rich_text_limit_is_strict_and_keeps_visible_evidence(monkeypatch):
    from lib.pdf_parser import text

    monkeypatch.setattr(text, 'HAS_PYMUPDF4LLM', True)
    monkeypatch.setattr(text, '_classic_header_info', lambda _doc: False)
    monkeypatch.setattr(
        text,
        '_to_markdown_classic',
        lambda _doc, **_kwargs: [{'text': 'x' * 10_000}],
    )

    rendered, extractor = text.extract_pdf_text_with_meta(
        _pdf_bytes(['short source']),
        max_chars=256,
    )

    assert extractor == 'pymupdf4llm'
    assert len(rendered) == 256
    assert 'resource budget' in rendered


def _figure_pdf(count):
    import io
    import random
    from PIL import Image
    from lib.pdf_parser.text import pymupdf
    if pymupdf is None:pytest.skip('requires a working real PyMuPDF backend')
    pixels=random.Random(42).randbytes(256*128*3)
    image=Image.frombytes('RGB',(256,128),pixels);buffer=io.BytesIO()
    image.save(buffer,format='PNG');image.close()
    document=pymupdf.open()
    for i in range(count):
        page=document.new_page(width=400,height=500)
        page.insert_image(pymupdf.Rect(40,40,296,168),stream=buffer.getvalue())
        page.insert_text((40,195),f'Figure {i+1}: selected source diagram')
    raw=document.tobytes(deflate=True);document.close();return raw


def test_real_pdf_retains_requested_figures_after_old_count_cap():
    from lib.pdf_parser.core import parse_pdf
    result=parse_pdf(_figure_pdf(75),max_images=75,max_image_width=300,text_mode='fast')
    assert len(result['images'])==75
    assert [image['page'] for image in result['images']]==list(range(1,76))
    assert result['limits']['maxImages']==75


def test_real_pdf_figure_uses_requested_width_above_old_pixel_and_zoom_caps():
    import base64
    import io
    from PIL import Image
    from lib.pdf_parser.core import parse_pdf
    result=parse_pdf(_figure_pdf(1),max_images=1,max_image_width=3500,text_mode='fast')
    assert len(result['images'])==1
    with Image.open(io.BytesIO(base64.b64decode(result['images'][0]['base64']))) as image:
        assert 3000<image.width<=3500
        assert image.width==result['images'][0]['width']
    assert result['limits']['maxImageWidth']==3500


def test_default_core_captures_all_figures_and_explicit_zero_disables():
    from lib.pdf_parser.core import parse_pdf
    raw=_figure_pdf(75)
    result=parse_pdf(raw,max_image_width=300,text_mode='fast')
    assert len(result['images'])==75 and result['limits']['maxImages'] is None
    assert parse_pdf(raw,max_images=0,text_mode='fast')['images']==[]
    assert len(parse_pdf(raw,max_images=3,max_image_width=300,text_mode='fast')['images'])==3


def test_paper_cache_rebuilds_limited_selection_and_reuses_complete_demand(tmp_path,monkeypatch):
    import json
    from lib.paper.images import figures
    from lib.pdf_parser import images
    monkeypatch.setattr(figures,'PAPER_IMG_DIR',str(tmp_path/'figures'))
    source=tmp_path/'source.pdf';source.write_bytes(_figure_pdf(75));identity='a'*64
    assert len(figures.extract_paper_figures(source,identity,max_images=20,max_image_width=300))==20
    assert figures.load_image_manifest(identity)==[]
    complete=figures.extract_paper_figures(source,identity,max_image_width=300)
    assert len(complete)==75 and figures.load_image_manifest(identity)==complete
    with monkeypatch.context() as patch:
        patch.setattr(images,'detect_and_clip_figures',lambda *a,**kw: (_ for _ in ()).throw(AssertionError('cache miss')))
        assert figures.extract_paper_figures(source,identity,max_image_width=300)==complete
    widened=figures.extract_paper_figures(source,identity,max_image_width=320)
    assert len(widened)==75 and widened[0]['width']>complete[0]['width']
    manifest=json.loads((tmp_path/'figures'/identity/'manifest.json').read_text())
    assert manifest['selection']=={'max_images':None,'max_image_width':320}
    assert manifest['complete']


def test_paper_partial_page_failure_does_not_cache_success(tmp_path,monkeypatch):
    from lib.paper.images import figures
    from lib.pdf_parser import images
    monkeypatch.setattr(figures,'PAPER_IMG_DIR',str(tmp_path/'figures'))
    source=tmp_path/'source.pdf';source.write_bytes(_figure_pdf(3));identity='b'*64
    original=images.detect_and_clip_figures
    def fail_first(page,index,*args,**kwargs):
        if index==0:raise OSError('injected page failure')
        return original(page,index,*args,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(images,'detect_and_clip_figures',fail_first)
        assert len(figures.extract_paper_figures(source,identity,max_image_width=300))==2
        assert figures.load_image_manifest(identity)==[]
    assert len(figures.extract_paper_figures(source,identity,max_image_width=300))==3


def test_continuation_renders_at_base_detail_without_fivefold_clamp():
    from lib.pdf_parser.text import pymupdf
    from lib.pdf_parser.images._detect import _try_stitch_next_page_table
    if pymupdf is None:pytest.skip('requires a working real PyMuPDF backend')
    document=pymupdf.open();base=document.new_page(width=400,height=120)
    base.draw_rect(pymupdf.Rect(40,5,296,30),color=(1,0,0),fill=(1,0,0))
    clip=pymupdf.Rect(40,5,296,30)
    pix=base.get_pixmap(matrix=pymupdf.Matrix(3500/256,3500/256),clip=clip)
    next_page=document.new_page(width=400,height=120)
    next_page.insert_text((40,30),'continued row',fontsize=8)
    next_page.insert_text((40,100),'Figure 2: next figure',fontsize=8)
    observed=[]
    class ObservedPage:
        def __getattr__(self,name):return getattr(next_page,name)
        def get_pixmap(self,**kwargs):
            observed.append(kwargs['matrix'].a)
            return next_page.get_pixmap(**kwargs)
    result=_try_stitch_next_page_table(ObservedPage(),clip,pix,max_image_width=3500)
    document.close()
    assert result is not None and result[1]==pix.width
    assert observed[0]>5


@pytest.mark.auth_mode('open')
def test_http_default_retains_complete_figures(flask_client):
    boundary='complete-figure-demand'
    fields=(f'--{boundary}\r\nContent-Disposition: form-data; name="textMode"\r\n\r\nfast\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="maxImageWidth"\r\n\r\n300\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="figures.pdf"\r\n'
        'Content-Type: application/pdf\r\n\r\n').encode()
    response=flask_client.post('/api/pdf/parse',data=fields+_figure_pdf(35)+f'\r\n--{boundary}--\r\n'.encode(),
        headers={'Content-Type':f'multipart/form-data; boundary={boundary}'})
    assert response.status_code==200,response.get_json()
    result=response.get_json()
    assert len(result['images'])==35 and result['limits']['maxImages'] is None
