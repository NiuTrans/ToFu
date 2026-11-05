"""Project media keeps every image/text channel across handler phase boundaries."""
from __future__ import annotations

import pytest
from PIL import Image

pytestmark = pytest.mark.unit


def test_mixed_image_read_settles_once_and_does_not_leak_svg_signals(tmp_path, monkeypatch):
    from lib.project_mod.read_tools import drain_svg_render_signals
    from lib.tasks_pkg.handlers import project
    from lib.tools import rich_response

    for name, color in [('one.png', 'red'), ('two.png', 'blue')]:
        Image.new('RGB', (8, 8), color).save(tmp_path / name)
    svg = '<svg xmlns="http://www.w3.org/2000/svg"><text>vector source</text></svg>'
    (tmp_path / 'diagram.svg').write_text(svg)
    (tmp_path / 'notes.txt').write_text('mixed read text')
    assets = [{'id': 'host-image-receipt', 'type': 'image'}]
    monkeypatch.setattr(rich_response, 'image_posthook', lambda *args: assets)
    settled = []
    monkeypatch.setattr(project, '_finalize_tool_round',
                        lambda _task, _rn, _entry, metas, **kwargs: settled.append(metas))
    task = {'id': 'media-reader', '_userId': 17, 'messages': [],
            'toolRounds': [], '_invocationStorageFree': True}
    drain_svg_render_signals()
    try:
        files = [{'path': str(tmp_path / name)} for name in
                 ['one.png', 'notes.txt', 'two.png', 'diagram.svg']]
        _, content, _ = project._handle_project_tool(
            task, {}, 'read_files', 'mixed', {'reads': files}, 1, {}, {},
            str(tmp_path), True)
        assert len(settled) == 1 and len(settled[0]) == 1
        meta = settled[0][0]
        assert len(content['images']) == 2
        assert 'mixed read text' in content['_text_fallback']
        assert svg in content['_text_fallback']
        assert {d['filename'] for d in meta['imageDataUris']} == {
            'one.png', 'two.png', 'diagram.svg'}
        assert content['responseAssets'] == meta['responseAssets'] == assets
        assert drain_svg_render_signals() == []

        _, text, _ = project._handle_project_tool(
            task, {}, 'read_files', 'text', {'path': str(tmp_path / 'notes.txt')},
            2, {}, {}, str(tmp_path), True)
        assert 'mixed read text' in text
        assert len(settled) == 2 and 'imageDataUris' not in settled[1][0]
    finally:
        drain_svg_render_signals()
