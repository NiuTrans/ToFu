"""tests/slides/test_slides_versions.py — deck version snapshot/revert pins.

Executable specification for lib/slides/versions.py: every conversational
edit step snapshots the manifest + page sources (bounded LRU, media-free by
design), lists newest-first, and a revert restores files atomically then
re-renders + re-exports ONCE — snapshots survive their own revert.
"""

from __future__ import annotations

import os

import pytest
import yaml

pytestmark = pytest.mark.unit

from lib.slides.versions import (MAX_VERSIONS, list_versions, revert_deck,
                                 snapshot_deck)

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
        <p>第 {n} 版标题</p>
"""

_THEME = {'colors': {'bg': '#FFFFFF', 'ink': '#111111'},
          'textStyles': {'title': {'fontSize': 40, 'color': '$ink'}}}


def _write_deck(tmp_path, n: int = 1):
    deck_dir = tmp_path / 'deck'
    (deck_dir / 'pages').mkdir(parents=True, exist_ok=True)
    (deck_dir / 'pages' / '01.page').write_text(
        _PAGE.format(n=n), encoding='utf-8')
    manifest = {'version': 'v2', 'title': f'v{n} deck', 'size': [1280, 720],
                'theme': _THEME, 'pages': ['pages/01.page']}
    (deck_dir / 'deck.pptd').write_text(
        yaml.safe_dump(manifest, allow_unicode=True), encoding='utf-8')
    return str(deck_dir)


@pytest.fixture
def render_export_stub(monkeypatch):
    monkeypatch.setattr('lib.slides.render_png.render_previews',
                        lambda deck, out, **kw: {'ok': True, 'pages': [],
                                                 'failed': []})
    monkeypatch.setattr('lib.slides.export_pptx.export_pptx',
                        lambda deck, out, **kw: out)


def _page_text(deck_dir):
    return open(os.path.join(deck_dir, 'pages', '01.page'),
                encoding='utf-8').read()


def test_snapshot_list_and_revert_roundtrip(tmp_path, render_export_stub):
    deck_dir = _write_deck(tmp_path, n=1)
    v1 = snapshot_deck(deck_dir, op='patch', summary='page 1: set_text')
    assert v1 == 1
    # Mutate, snapshot again.
    _write_deck(tmp_path, n=2)
    v2 = snapshot_deck(deck_dir, op='restyle', summary='midnight-navy')
    assert v2 == 2

    rows = list_versions(deck_dir)
    assert [r['version'] for r in rows] == [2, 1]   # newest first
    assert rows[1]['op'] == 'patch'
    assert 'set_text' in rows[1]['summary']

    res = revert_deck(deck_dir, v1)
    assert res['ok'], res
    assert res['version'] == 1
    assert set(res['restored']) == {'deck.pptd', 'pages/01.page'}
    assert '第 1 版标题' in _page_text(deck_dir)
    # The snapshot survives its own revert (bounce between versions).
    res2 = revert_deck(deck_dir, v2)
    assert res2['ok'] and '第 2 版标题' in _page_text(deck_dir)


def test_revert_unknown_version_lists_available(tmp_path,
                                                render_export_stub):
    deck_dir = _write_deck(tmp_path)
    snapshot_deck(deck_dir, op='patch')
    res = revert_deck(deck_dir, 99)
    assert not res['ok']
    assert res['available_versions'] == [1]


def test_lru_bound_prunes_oldest(tmp_path, render_export_stub):
    deck_dir = _write_deck(tmp_path)
    for i in range(MAX_VERSIONS + 3):
        snapshot_deck(deck_dir, op='patch', summary=f'v{i + 1}')
    rows = list_versions(deck_dir)
    assert len(rows) == MAX_VERSIONS
    assert rows[-1]['version'] == 4          # v1..v3 pruned
    assert not os.path.isdir(
        os.path.join(deck_dir, '.versions', '1'))


def test_snapshot_never_raises_and_zero_is_honest(tmp_path):
    empty = tmp_path / 'empty'
    empty.mkdir()
    assert snapshot_deck(str(empty), op='patch') == 0


def test_revert_reports_export_failure(tmp_path, monkeypatch):
    monkeypatch.setattr('lib.slides.render_png.render_previews',
                        lambda deck, out, **kw: {'ok': True, 'pages': [],
                                                 'failed': []})

    def _boom(deck, out, **kw):
        raise RuntimeError('disk full')
    monkeypatch.setattr('lib.slides.export_pptx.export_pptx', _boom)
    deck_dir = _write_deck(tmp_path)
    snapshot_deck(deck_dir, op='patch')
    res = revert_deck(deck_dir, 1)
    assert not res['ok'] and 'export failed' in res['errors'][0]
