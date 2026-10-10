"""tests/slides/test_slides_editor_api.py — slide-editor endpoint pins (A8).

The editor overlay's three endpoints share one contract: owner-scoped
reads of the CURRENT deck (page HTML re-renders on demand after every
patch), and the deterministic patch route shares the patch_slides engine
(validation/rollback) while snapshotting pre-edit state for revert.
"""

from __future__ import annotations

import os

import pytest
import yaml

pytestmark = pytest.mark.unit

TEST_OWNER_USER_ID = 1

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
        <p>编辑器标题</p>
"""

_THEME = {'colors': {'bg': '#FFFFFF', 'ink': '#111111'},
          'textStyles': {'title': {'fontSize': 40, 'color': '$ink'}}}


def _seed_deck_job(tmp_path, task_id='slides_edit1'):
    """A finished-on-disk slides job at slides_root/jobs/<task_id>."""
    from lib.production.jobs import write_manifest
    job = tmp_path / 'jobs' / task_id
    deck_dir = job / 'deck'
    (deck_dir / 'pages').mkdir(parents=True)
    (deck_dir / 'pages' / '01.page').write_text(_PAGE, encoding='utf-8')
    manifest = {'version': 'v2', 'title': 'editor 测试', 'size': [1280, 720],
                'theme': _THEME, 'pages': ['pages/01.page']}
    (deck_dir / 'deck.pptd').write_text(
        yaml.safe_dump(manifest, allow_unicode=True), encoding='utf-8')
    write_manifest(str(job), {
        'task_id': task_id, 'user_id': TEST_OWNER_USER_ID,
        'topic': 'editor 测试',
    }, fields=('task_id', 'user_id', 'topic'),
        kind='slides-deck', state='done', log_label='test')
    return job


@pytest.fixture
def deck_job(tmp_path, monkeypatch):
    monkeypatch.setattr('lib.slides.engine.slides_root',
                        lambda: str(tmp_path))
    monkeypatch.setattr('lib.slides.render_png.render_page_png',
                        lambda *a, **k: a[2] if len(a) > 2 else '')
    monkeypatch.setattr('lib.slides.export_pptx.export_pptx',
                        lambda deck, out, **kw: out)
    monkeypatch.setattr('lib.slides.patch._page_layout_findings',
                        lambda deck, index: 0)
    return _seed_deck_job(tmp_path)


def test_page_html_served_on_demand(flask_client, deck_job):
    r = flask_client.get('/api/v4/slides/slides_edit1/pages/1.html')
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert '编辑器标题' in body
    assert 'data-element-id="title-1"' in body
    assert flask_client.get(
        '/api/v4/slides/slides_edit1/pages/9.html').status_code == 404
    assert flask_client.get(
        '/api/v4/slides/slides_ghost/pages/1.html').status_code == 404


def test_page_elements_inventory(flask_client, deck_job):
    r = flask_client.get('/api/v4/slides/slides_edit1/pages/1/elements')
    assert r.status_code == 200
    body = r.get_json()
    assert body['ok'] and body['width'] == 1280
    el = body['elements'][0]
    assert el['elementId'] == 'title-1'
    assert el['elementType'] == 'text'
    assert '编辑器标题' in el['text']


def test_patch_route_applies_and_snapshots(flask_client, deck_job):
    r = flask_client.post('/api/v4/slides/slides_edit1/pages/1/patch',
                          json={'ops': [
                              {'op': 'set_text', 'target': 'title-1',
                               'text': '<p>改后的标题</p>'}]})
    assert r.status_code == 200
    body = r.get_json()
    assert body['ok'] and body['changes'][0]['target'] == 'title-1'
    page = yaml.safe_load((deck_job / 'deck' / 'pages' /
                           '01.page').read_text(encoding='utf-8'))
    assert page['elements'][0]['content']['text'] == '<p>改后的标题</p>'
    # Pre-edit snapshot exists for revert_slides (v1 holds the original).
    snap = (deck_job / 'deck' / '.versions' / '1' / 'pages' /
            '01.page').read_text(encoding='utf-8')
    assert '编辑器标题' in snap
    # The on-demand HTML reflects the patch immediately.
    html = flask_client.get(
        '/api/v4/slides/slides_edit1/pages/1.html').get_data(as_text=True)
    assert '改后的标题' in html


def test_patch_route_rolls_back_invalid_ops(flask_client, deck_job):
    r = flask_client.post('/api/v4/slides/slides_edit1/pages/1/patch',
                          json={'ops': [
                              {'op': 'set_text', 'target': 'ghost', 'text': 'x'}]})
    assert r.status_code == 200
    body = r.get_json()
    assert body['ok'] is False and body['errors']
    page = (deck_job / 'deck' / 'pages' / '01.page').read_text(
        encoding='utf-8')
    assert '编辑器标题' in page                     # untouched
    empty = flask_client.post('/api/v4/slides/slides_edit1/pages/1/patch',
                              json={'ops': []})
    assert empty.status_code == 400
