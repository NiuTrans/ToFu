"""Native export rejects incomplete objects and broken OOXML before replacement."""

from __future__ import annotations

import zipfile
import pytest

from lib.slides.export_pptx import ExportError, export_pptx
from lib.slides.pptd import Deck, Page
from lib.slides.pptx_qa import inspect_pptx_package

pytestmark = pytest.mark.unit


def deck(root):
    return Deck(title='Native QA', size=(1280, 720), theme={}, root=str(root), pages=[
        Page(path='pages/01.page', elements=[
            {'elementId': 'headline', 'elementType': 'text', 'bounds': [80, 80, 800, 160],
             'content': {'text': 'Editable title'}},
            {'elementId': 'data', 'elementType': 'chart', 'bounds': [80, 260, 800, 360],
             'chartType': 'column', 'data': {'categories': ['A', 'B'],
                                          'series': [{'name': 'Values', 'values': [1, 2]}]}},
            {'elementId': 'cells', 'elementType': 'table', 'bounds': [900, 200, 260, 200],
             'columnWidths': [1.0], 'rowHeights': [0.5, 0.5],
             'rows': [[{'text': 'A'}], [{'text': 'B'}]]}])])


def rewrite(path, change):
    with zipfile.ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    change(parts)
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)


def test_export_records_editable_objects_and_native_tables_and_charts(tmp_path):
    output = tmp_path / 'deck.pptx'
    summary = export_pptx(deck(tmp_path), str(output), embed_fonts=False)
    quality = summary['packageValidation']
    assert quality['ok'] and quality['slides'] == 1
    assert quality['editableTextBoxes'] == 1
    assert quality['nativeTables'] == quality['nativeCharts'] == 1
    assert summary['output'] == str(output)


def test_export_element_failure_preserves_existing_file(tmp_path, monkeypatch):
    output = tmp_path / 'deck.pptx'
    output.write_bytes(b'previous user deliverable')
    import lib.slides.export_pptx as exporter

    def fail(*args, **kwargs):
        raise RuntimeError('chart rendering failed')

    monkeypatch.setattr(exporter, '_add_chart', fail)
    with pytest.raises(ExportError, match='data.*chart rendering failed'):
        export_pptx(deck(tmp_path), str(output), embed_fonts=False)
    assert output.read_bytes() == b'previous user deliverable'
    assert list(tmp_path.glob('.tofu-pptx-*')) == []


@pytest.mark.parametrize('corruption', ['missing_target', 'bad_xml', 'chart_axis', 'slide_count'])
def test_package_rejects_structural_corruption(tmp_path, corruption):
    path = tmp_path / 'deck.pptx'
    export_pptx(deck(tmp_path), str(path), embed_fonts=False)

    def corrupt(parts):
        if corruption == 'missing_target':
            del parts['ppt/charts/chart1.xml']
        elif corruption == 'bad_xml':
            parts['ppt/slides/slide1.xml'] = b'<broken'
        elif corruption == 'chart_axis':
            from xml.etree import ElementTree as ET
            chart = ET.fromstring(parts['ppt/charts/chart1.xml'])
            ns = '{http://schemas.openxmlformats.org/drawingml/2006/chart}'
            chart.find('.//' + ns + 'barChart/' + ns + 'axId').set('val', '999999')
            parts['ppt/charts/chart1.xml'] = ET.tostring(chart)

    rewrite(path, corrupt)
    result = inspect_pptx_package(str(path), expected_pages=2 if corruption == 'slide_count' else 1)
    assert not result['ok']
    assert result['findings']


def test_duplicate_zip_parts_are_rejected(tmp_path):
    path = tmp_path / 'deck.pptx'
    export_pptx(deck(tmp_path), str(path), embed_fonts=False)
    with zipfile.ZipFile(path, 'a') as archive:
        with pytest.warns(UserWarning, match='Duplicate'):
            archive.writestr('ppt/presentation.xml', b'<duplicate/>')
    assert 'duplicate part names' in inspect_pptx_package(str(path))['findings'][0]


def test_failed_package_verification_cannot_replace_previous_output(tmp_path, monkeypatch):
    import lib.slides.export_pptx as exporter
    output = tmp_path / 'deck.pptx'
    output.write_bytes(b'previous deck')

    def reject(*args, **kwargs):
        raise ExportError('package invalid')

    monkeypatch.setattr(exporter, '_verify', reject)
    with pytest.raises(ExportError, match='package invalid'):
        export_pptx(deck(tmp_path), str(output), embed_fonts=False)
    assert output.read_bytes() == b'previous deck'
