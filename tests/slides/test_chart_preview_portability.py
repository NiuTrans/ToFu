"""Charts retain every plotted category and the audited CJK label font."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile

import pytest
import yaml

pytestmark = pytest.mark.unit


def _deck(tmp_path, *, width=640, height=340, categories=2, series=1,
          family='Noto Sans CJK SC'):
    from lib.slides.pptd import parse_deck

    chart = {
        'elementId': 'evidence', 'elementType': 'chart',
        'bounds': [40, 40, width, height], 'chartType': 'bar',
        'options': {'legend': True, 'dataLabels': True},
        'data': {
            'categories': [f'条件{i}' for i in range(categories)],
            'series': [{'name': f'准确率{j}',
                        'values': [78 + i + j for i in range(categories)]}
                       for j in range(series)],
        },
    }
    (tmp_path / '01.page').write_text(yaml.safe_dump({
        'pageType': 'content', 'elements': [chart],
        'background': {'type': 'solid', 'color': '#FFFFFF'},
    }, allow_unicode=True), encoding='utf-8')
    manifest = tmp_path / 'deck.pptd'
    manifest.write_text(yaml.safe_dump({
        'version': 'v2', 'title': 'chart evidence', 'size': [1280, 720],
        'theme': {'textStyles': {'body': {'fontFamily': family}}},
        'pages': ['01.page'],
    }), encoding='utf-8')
    return parse_deck(str(manifest))


def _svg(deck, monkeypatch):
    from lib.slides import render_html

    monkeypatch.setattr(render_html, '_font_face_css', lambda _: '')
    html = render_html.render_page_html(deck, deck.pages[0])
    return ET.fromstring(re.search(r'<svg\b.*?</svg>', html, re.S).group())


@pytest.mark.parametrize('width,height', [(900, 240), (640, 340), (400, 600)])
@pytest.mark.parametrize('categories,series', [(2, 1), (4, 2)])
def test_horizontal_categories_and_series_remain_inside_plot(
        tmp_path, monkeypatch, width, height, categories, series):
    deck = _deck(tmp_path, width=width, height=height,
                 categories=categories, series=series)
    svg = _svg(deck, monkeypatch)
    # The first rectangles are legend swatches; remaining rectangles are data.
    bars = svg.findall('rect')[series:]
    assert len(bars) == categories * series
    for bar in bars:
        x, y, w, h = (float(bar.attrib[key]) for key in ('x', 'y', 'width', 'height'))
        assert w > 0 and h > 0
        assert 0 <= x < x + w <= width
        assert 30 <= y < y + h <= height
    for label in svg.findall('text'):
        assert 0 <= float(label.attrib['y']) <= height


def test_chart_preview_uses_canonical_body_font(tmp_path, monkeypatch):
    svg = _svg(_deck(tmp_path), monkeypatch)
    assert svg.attrib.get('font-family') == 'Noto Sans SC'


def test_chart_font_subset_includes_categories_series_and_axis_digits(tmp_path):
    from lib.slides.export_pptx import _collect_font_usage

    usage = _collect_font_usage(_deck(tmp_path))
    assert set(usage) == {'Noto Sans SC'}
    assert set('条件准确率0123456789.%−-') <= usage['Noto Sans SC']['regular']


def test_native_chart_binds_latin_and_cjk_typefaces(tmp_path):
    from lib.slides.export_pptx import export_pptx

    deck = _deck(tmp_path)
    output = tmp_path / 'chart.pptx'
    export_pptx(deck, str(output), embed_fonts=False)
    with zipfile.ZipFile(output) as archive:
        chart = ET.fromstring(archive.read('ppt/charts/chart1.xml'))
    namespaces = {'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
                  'c': 'http://schemas.openxmlformats.org/drawingml/2006/chart'}
    for script in ('latin', 'ea', 'cs'):
        face = chart.find(f'c:txPr/a:p/a:pPr/a:defRPr/a:{script}', namespaces)
        assert face is not None and face.attrib['typeface'] == 'Noto Sans SC'


def test_chart_only_strict_export_refuses_missing_font(tmp_path):
    from lib.slides.export_pptx import ExportError, export_pptx

    deck = _deck(tmp_path, family='Unregistered Chart Font')
    with pytest.raises(ExportError, match='embedding incomplete'):
        export_pptx(deck, str(tmp_path / 'chart.pptx'), require_embedded_fonts=True)
