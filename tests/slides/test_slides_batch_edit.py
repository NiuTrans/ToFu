"""tests/slides/test_slides_batch_edit.py — edit_slides batch-path pins.

The batch form of edit_slides re-authors ≤8 pages behind a 2-worker bound,
lets per-page failures stay local (an unedited page keeps its layout), and
re-exports the PPTX exactly ONCE after every page settled — never
concurrently (``export=False`` per page).
"""

from __future__ import annotations

import os

import pytest
import yaml

pytestmark = pytest.mark.unit

from lib.tasks_pkg.handlers.motion_video import _batch_edit_pages

_PAGE = """\
pageType: content
background:
  type: solid
  color: '#FFFFFF'
elements:
  - elementId: title-1
    elementType: text
    bounds: [72, 60, 800, 80]
    content:
      style: "$title"
      text: |
        <p>标题</p>
"""

_THEME = {'colors': {'bg': '#FFFFFF', 'ink': '#111111'},
          'textStyles': {'title': {'fontSize': 40, 'color': '$ink'}}}


def _write_deck(tmp_path, pages: int = 3):
    deck_dir = tmp_path / 'deck'
    (deck_dir / 'pages').mkdir(parents=True)
    names = []
    for i in range(1, pages + 1):
        name = f'pages/{i:02d}.page'
        (deck_dir / name).write_text(_PAGE, encoding='utf-8')
        names.append(name)
    manifest = {'version': 'v2', 'title': 'batch 测试', 'size': [1280, 720],
                'theme': _THEME, 'pages': names}
    (deck_dir / 'deck.pptd').write_text(
        yaml.safe_dump(manifest, allow_unicode=True), encoding='utf-8')
    return str(deck_dir)


def test_batch_edits_each_page_and_exports_once(tmp_path, monkeypatch):
    deck_dir = _write_deck(tmp_path)
    calls = []

    def fake_edit(deck, index, instruction, **kw):
        assert kw.get('export') is False, 'batch must defer the export'
        calls.append(index)
        return {'ok': True, 'mode': 'edited', 'rounds': 1,
                'pptx_path': '', 'preview': f'p{index}.png'}
    monkeypatch.setattr('lib.slides.author.edit_page', fake_edit)
    exports = []
    monkeypatch.setattr(
        'lib.slides.export_pptx.export_pptx',
        lambda deck, out, **kw: exports.append(out) or out)

    res = _batch_edit_pages(deck_dir, 'slides_x', [1, 2, 3], '换成深色',
                            lang='zh', owner=1)
    assert res['ok'], res
    assert sorted(calls) == [0, 1, 2]
    assert res['edited'] == [1, 2, 3]
    assert len(exports) == 1, 'exactly ONE export after the batch'
    assert res['preview_url'].endswith('/pages/1.png')


def test_batch_per_page_failure_stays_local(tmp_path, monkeypatch):
    deck_dir = _write_deck(tmp_path)

    def fake_edit(deck, index, instruction, **kw):
        if index == 1:
            return {'ok': False, 'detail': 'validation failed'}
        return {'ok': True, 'mode': 'edited', 'rounds': 1,
                'pptx_path': '', 'preview': f'p{index}.png'}
    monkeypatch.setattr('lib.slides.author.edit_page', fake_edit)
    monkeypatch.setattr('lib.slides.export_pptx.export_pptx',
                        lambda deck, out, **kw: out)

    res = _batch_edit_pages(deck_dir, 'slides_x', [1, 2, 3], 'x',
                            lang='zh', owner=1)
    assert res['ok']           # pages 1 and 3 landed
    assert res['edited'] == [1, 3]
    assert res['failed'] == {2: 'validation failed'}
    assert res['pages']['2']['ok'] is False


def test_batch_all_failed_exports_nothing(tmp_path, monkeypatch):
    deck_dir = _write_deck(tmp_path)
    monkeypatch.setattr(
        'lib.slides.author.edit_page',
        lambda deck, index, instruction, **kw: {'ok': False,
                                                'detail': 'nope'})
    exported = []
    monkeypatch.setattr(
        'lib.slides.export_pptx.export_pptx',
        lambda deck, out, **kw: exported.append(out) or out)
    res = _batch_edit_pages(deck_dir, 'slides_x', [1, 2], 'x',
                            lang='zh', owner=1)
    assert not res['ok']
    assert exported == []


def test_batch_export_failure_is_honest(tmp_path, monkeypatch):
    deck_dir = _write_deck(tmp_path)
    monkeypatch.setattr(
        'lib.slides.author.edit_page',
        lambda deck, index, instruction, **kw: {'ok': True, 'mode': 'edited',
                                                'rounds': 1, 'pptx_path': '',
                                                'preview': 'p.png'})

    def _boom(deck, out, **kw):
        raise RuntimeError('disk full')
    monkeypatch.setattr('lib.slides.export_pptx.export_pptx', _boom)
    res = _batch_edit_pages(deck_dir, 'slides_x', [1], 'x', lang='zh',
                            owner=1)
    assert not res['ok'] and 'export failed' in res['detail']
