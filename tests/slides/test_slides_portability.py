"""Portable-font and geometry-parity contracts for PPTX delivery."""

from __future__ import annotations

import zipfile

import pytest
import yaml


pytestmark = pytest.mark.unit


def _deck(tmp_path, page: dict, *, family: str = 'MiSans'):
    from lib.slides.pptd import parse_deck

    pages = tmp_path / 'pages'
    pages.mkdir()
    (pages / '01.page').write_text(
        yaml.safe_dump(page, allow_unicode=True, sort_keys=False),
        encoding='utf-8')
    manifest = {
        'version': 'v2', 'title': 'portable', 'size': [1280, 720],
        'theme': {
            'colors': {
                'bg': '#FFFFFF', 'ink': '#111111', 'primary': '#111111',
                'accent': '#FFD100', 'muted': '#666666',
                'hairline': '#DDDDDD',
            },
            'textStyles': {
                'body': {'fontSize': 24, 'fontFamily': family},
            },
        },
        'pages': ['pages/01.page'],
    }
    path = tmp_path / 'deck.pptd'
    path.write_text(yaml.safe_dump(manifest, allow_unicode=True,
                                   sort_keys=False), encoding='utf-8')
    return parse_deck(str(path))


def test_common_cjk_alias_resolves_to_embeddable_registry_family(tmp_path):
    from lib.slides.export_pptx import _collect_font_usage

    page = {
        'pageType': 'content',
        'background': {'type': 'solid', 'color': '$bg'},
        'elements': [{
            'elementId': 'copy', 'elementType': 'text',
            'bounds': [80, 80, 800, 100],
            'content': {'text': '美团大模型', 'style': '$body'},
        }],
    }
    usage = _collect_font_usage(
        _deck(tmp_path, page, family='Noto Sans CJK SC'))
    assert set(usage) == {'Noto Sans SC'}


def test_strict_portable_export_refuses_unembedded_family(tmp_path):
    from lib.slides.export_pptx import ExportError, export_pptx

    page = {
        'pageType': 'content',
        'background': {'type': 'solid', 'color': '$bg'},
        'elements': [{
            'elementId': 'copy', 'elementType': 'text',
            'bounds': [80, 80, 800, 100],
            'content': {'text': '不能静默替换', 'style': '$body'},
        }],
    }
    deck = _deck(tmp_path, page, family='Unregistered Office Font')
    with pytest.raises(ExportError, match='embedding incomplete'):
        export_pptx(deck, str(tmp_path / 'strict.pptx'),
                    require_embedded_fonts=True)


def test_straight_arrow_exports_as_native_connector(tmp_path):
    from lib.slides.export_pptx import export_pptx

    page = {
        'pageType': 'content',
        'background': {'type': 'solid', 'color': '$bg'},
        'elements': [{
            'elementId': 'exact-arrow', 'elementType': 'line',
            'bounds': [100, 200, 400, 20], 'viewBox': [400, 20],
            'points': '0,10 400,10', 'curve': 'round',
            'arrow': [None, 'arrow'],
            'border': {'style': 'solid', 'width': 4, 'color': '$accent'},
        }],
    }
    out = tmp_path / 'connector.pptx'
    export_pptx(_deck(tmp_path, page), str(out), embed_fonts=False)
    with zipfile.ZipFile(out) as archive:
        xml = archive.read('ppt/slides/slide1.xml').decode('utf-8')
    assert '<p:cxnSp>' in xml
    assert 'name="exact-arrow"' in xml
    assert '<a:tailEnd type="triangle"' in xml


def test_process_chevrons_pin_browser_and_powerpoint_adjustment():
    from lib.slides.components import expand_page_components

    page = {
        'elements': [],
        'components': [{
            'componentId': 'flow', 'componentType': 'process',
            'bounds': [100, 100, 800, 300],
            'items': [{'label': '发现'}, {'label': '验证'}],
        }],
    }
    expanded = expand_page_components(page)
    chevrons = [element for element in expanded
                if element.get('shapeName') == 'chevron']
    assert len(chevrons) == 2
    assert all(item['adjustments'] == [25000] for item in chevrons)


def test_outline_gate_rejects_layout_monoculture():
    from lib.slides.recipe import _outline_errors

    pages = [{
        'purpose': f'advance argument {index}',
        'key_message': f'judgment {index}',
        'layout_archetype': 'split-editorial',
        'visual_modality': 'comparison',
    } for index in range(4)]
    errors = _outline_errors(
        {'artifacts': {'research': {'cards': []}}}, {'pages': pages})
    assert 'layout variety too low (1; need 4)' in errors
    assert 'visual modality variety too low (1; need 4)' in errors
