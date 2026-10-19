"""Only table-caption crops need structured table detection; evidence stays exact."""
import pytest

from lib.pdf_parser.images import _detect

pytestmark = pytest.mark.unit


class ObservedPage:
    def __init__(self, page, failure=None):
        self.page = page
        self.failure = failure
        self.table_calls = 0

    def __getattr__(self, name):
        return getattr(self.page, name)

    def find_tables(self):
        self.table_calls += 1
        if self.failure is not None:
            raise self.failure
        return self.page.find_tables()


def table_document(caption):
    pymupdf = pytest.importorskip('pymupdf')
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=500)
    if caption == 'above':
        page.insert_text((40, 65), 'Table 1. Measured results')
    elif caption == 'below':
        page.insert_text((40, 235), 'Table 1. Measured results')
    else:
        page.insert_text((40, 65), 'Ordinary page without a caption')
    for x in (40, 130, 220, 310):
        page.draw_line((x, 90), (x, 210))
    for y in (90, 120, 150, 180, 210):
        page.draw_line((40, y), (310, y))
    for row in range(4):
        for col in range(3):
            page.insert_text((45+col*90, 110+row*30), f'cell {row}:{col}', fontsize=8)
    return doc


def test_uncaptioned_page_does_not_expand_table_glyphs():
    with table_document(None) as document:
        page = ObservedPage(document[0])
        assert _detect.detect_and_clip_figures(page, 0, 1) == []
        assert page.table_calls == 0


def test_figure_only_page_keeps_rendered_evidence_without_table_detection():
    from tests.pdf_parser.test_pdf_classic_bounds import _figure_pdf
    pymupdf = pytest.importorskip('pymupdf')
    with pymupdf.open(stream=_figure_pdf(1), filetype='pdf') as document:
        page = ObservedPage(document[0])
        result = _detect.detect_and_clip_figures(page, 0, 1, max_image_width=300, doc=document)
        assert page.table_calls == 0
        assert len(result) == 1 and result[0]['source'] == 'figure_clip'
        assert result[0]['page'] == 1 and result[0]['pages'] == [1]
        assert result[0]['caption'] == 'Figure 1: selected source diagram'
        left, top, right, bottom = result[0]['bbox']
        assert left <= 40 and top <= 40 and right >= 296 and bottom >= 195
        assert result[0]['base64'] and result[0]['width'] <= 300


@pytest.mark.parametrize('caption', ['above', 'below'])
@pytest.mark.parametrize('failed_detection', [False, True])
def test_table_caption_keeps_structured_bounds_and_failure_fallback(caption, failed_detection):
    with table_document(caption) as document:
        page = ObservedPage(document[0], OSError('detector refused') if failed_detection else None)
        result = _detect.detect_and_clip_figures(page, 0, 1, max_image_width=400, min_bytes=1, doc=document)
        assert page.table_calls == 1
        assert len(result) == 1 and result[0]['source'] == 'table_clip'
        assert result[0]['caption'] == 'Table 1. Measured results'
        assert result[0]['page'] == 1 and result[0]['pages'] == [1]
        assert result[0]['base64'] and len(result[0]['bbox']) == 4
