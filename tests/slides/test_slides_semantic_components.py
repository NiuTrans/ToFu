"""Semantic components and expanded native-chart surface stay editable."""

from __future__ import annotations

import zipfile

import pytest
import yaml

pytestmark = pytest.mark.unit


def _deck(tmp_path, page: dict) -> str:
    root = tmp_path / 'deck'
    (root / 'pages').mkdir(parents=True)
    theme = {
        'colors': {'bg': '#F7F7F5', 'ink': '#1B2430',
                   'primary': '#16283C', 'accent': '#C0652B',
                   'muted': '#6B7280', 'hairline': '#D8D5CE'},
        'textStyles': {
            'title': {'fontSize': 40, 'color': '$primary'},
            'body': {'fontSize': 18, 'color': '$ink'},
            'caption': {'fontSize': 12, 'color': '$muted'},
            'bignum': {'fontSize': 88, 'color': '$accent'},
        },
    }
    (root / 'pages' / '01.page').write_text(
        yaml.safe_dump(page, allow_unicode=True), encoding='utf-8')
    (root / 'deck.pptd').write_text(yaml.safe_dump({
        'version': 'v2', 'title': 'Semantic', 'size': [1280, 720],
        'theme': theme, 'pages': ['pages/01.page'],
    }, allow_unicode=True), encoding='utf-8')
    return str(root / 'deck.pptd')


def test_components_expand_before_validation_render_and_export(tmp_path):
    from lib.slides.export_pptx import export_pptx
    from lib.slides.pptd import parse_deck, validate_deck
    from lib.slides.render_html import render_page_html

    manifest = _deck(tmp_path, {
        'pageType': 'content',
        'background': {'type': 'solid', 'color': '$bg'},
        'components': [
            {'componentId': 'north-star', 'componentType': 'metric',
             'bounds': [72, 100, 480, 300], 'value': '37%',
             'label': '转化率提升', 'support': '同口径 A/B 样本'},
            {'componentId': 'steps', 'componentType': 'process',
             'bounds': [600, 100, 600, 300],
             'items': [{'label': '发现', 'detail': '证据'},
                       {'label': '解释', 'detail': '机制'},
                       {'label': '行动', 'detail': '决策'}]},
        ],
    })
    deck = parse_deck(manifest)
    assert validate_deck(deck) == []
    assert any(element['elementId'] == 'north-star--value'
               for element in deck.pages[0].elements)
    html = render_page_html(deck, deck.pages[0])
    assert '37%' in html and '同口径 A/B 样本' in html
    out = str(tmp_path / 'components.pptx')
    summary = export_pptx(deck, out)
    assert summary['slides'] == 1 and summary['bytes'] > 4096


def test_area_doughnut_and_radar_are_native_charts(tmp_path):
    from lib.slides.export_pptx import export_pptx
    from lib.slides.pptd import parse_deck, validate_deck
    from lib.slides.render_html import render_page_html

    elements = []
    for index, chart_type in enumerate(('area', 'doughnut', 'radar')):
        elements.append({
            'elementId': chart_type, 'elementType': 'chart',
            'bounds': [30 + index * 410, 130, 380, 350],
            'chartType': chart_type,
            'data': {'categories': ['A', 'B', 'C'],
                     'series': [{'name': 'Score', 'values': [4, 7, 6]}]},
        })
    deck = parse_deck(_deck(tmp_path, {
        'pageType': 'content',
        'background': {'type': 'solid', 'color': '$bg'},
        'elements': elements,
    }))
    assert validate_deck(deck) == []
    html = render_page_html(deck, deck.pages[0])
    assert html.count('class="el chart"') == 3
    out = str(tmp_path / 'charts.pptx')
    export_pptx(deck, out)
    with zipfile.ZipFile(out) as archive:
        xml = '\n'.join(
            archive.read(name).decode('utf-8')
            for name in archive.namelist()
            if name.startswith('ppt/charts/chart'))
    assert '<c:areaChart>' in xml
    assert '<c:doughnutChart>' in xml
    assert '<c:radarChart>' in xml

    from lib.slides.import_pptx import import_pptx
    imported = tmp_path / 'imported'
    import_pptx(out, str(imported))
    page = yaml.safe_load(
        next((imported / 'pages').glob('*.page')).read_text(encoding='utf-8'))
    imported_types = {
        element.get('chartType') for element in page.get('elements') or []
        if element.get('elementType') == 'chart'
    }
    assert imported_types == {'area', 'doughnut', 'radar'}
