"""tests/slides/test_slides_theme_ops.py — deck-level deterministic restyle pins.

Executable specification for lib/slides/theme_ops.py: theme/palette/font
swaps mutate ONLY the manifest token block, validate, re-render, re-export;
unknown ids/slots/faces are rejected with actionable errors; any downstream
failure restores the pre-op manifest. The token assembly is pinned as the
SAME function the recipe design stage consumes (one source of truth).
"""

from __future__ import annotations

import os

import pytest
import yaml

pytestmark = pytest.mark.unit

from lib.slides.theme_ops import (apply_deck_palette, apply_deck_restyle,
                                  apply_deck_theme, build_theme_tokens)

_PAGE = """\
pageType: content
background:
  type: solid
  color: "$bg"
elements:
  - elementId: title-1
    elementType: text
    bounds: [72, 60, 800, 80]
    content:
      style: "$title"
      text: |
        <p>标题</p>
"""


def _write_deck(tmp_path, theme_tokens):
    deck_dir = tmp_path / 'deck'
    (deck_dir / 'pages').mkdir(parents=True)
    (deck_dir / 'pages' / '01.page').write_text(_PAGE, encoding='utf-8')
    manifest = {'version': 'v2', 'title': 'restyle 测试', 'size': [1280, 720],
                'theme': theme_tokens, 'pages': ['pages/01.page']}
    (deck_dir / 'deck.pptd').write_text(
        yaml.safe_dump(manifest, allow_unicode=True), encoding='utf-8')
    return str(deck_dir)


@pytest.fixture
def deck_dir(tmp_path, monkeypatch):
    from lib.design_sys.themes import get_theme
    tokens = build_theme_tokens(get_theme('paper-engineer'))
    path = _write_deck(tmp_path, tokens)
    monkeypatch.setattr('lib.slides.render_png.render_previews',
                        lambda deck, out, **kw: {'ok': True, 'pages': [],
                                                 'failed': []})
    monkeypatch.setattr('lib.slides.export_pptx.export_pptx',
                        lambda deck, out, **kw: out)
    # Font downloads are infrastructure; pretend every weight stages.
    monkeypatch.setattr('lib.design_sys.fonts.ensure_font',
                        lambda *a, **k: True)
    return path


def _manifest(deck_dir):
    with open(os.path.join(deck_dir, 'deck.pptd'), encoding='utf-8') as f:
        return yaml.safe_load(f)


# ── token assembly is the design stage's own contract ─────

def test_build_theme_tokens_matches_design_stage_shape():
    from lib.design_sys.themes import get_theme
    theme = get_theme('paper-engineer')
    tokens = build_theme_tokens(theme)
    assert set(tokens['colors']) == {
        'bg', 'ink', 'primary', 'accent', 'muted', 'hairline'}
    assert set(tokens['textStyles']) == {'title', 'body', 'caption', 'bignum'}
    assert tokens['textStyles']['title']['color'] == '$primary'
    assert tokens['textStyles']['bignum']['fontFamily'] == 'MiSans' or \
        tokens['textStyles']['bignum']['fontFamily']
    assert 'default' in tokens['tableStyles']

    # The recipe design stage consumes THIS function (single source).
    import inspect
    import lib.slides.recipe as recipe
    src = inspect.getsource(recipe._run_design)
    assert 'build_theme_tokens' in src
    assert 'stage_theme_fonts' in src


# ── apply_deck_theme ──────────────────────────────────────

def test_apply_deck_theme_swaps_token_block(deck_dir):
    res = apply_deck_theme(deck_dir, 'midnight-navy')
    assert res['ok'], res
    theme = _manifest(deck_dir)['theme']
    from lib.design_sys.themes import get_theme
    expected = build_theme_tokens(get_theme('midnight-navy'))
    assert theme == expected
    assert res['changes'][0]['after'] == 'midnight-navy'


def test_apply_deck_theme_unknown_id_lists_available(deck_dir):
    before = open(os.path.join(deck_dir, 'deck.pptd'),
                  encoding='utf-8').read()
    res = apply_deck_theme(deck_dir, 'no-such-theme')
    assert not res['ok']
    assert 'available' in res['errors'][0]
    assert 'paper-engineer' in res['available_themes']
    assert open(os.path.join(deck_dir, 'deck.pptd'),
                encoding='utf-8').read() == before


# ── apply_deck_palette ────────────────────────────────────

def test_apply_deck_palette_overrides_only_named_slots(deck_dir):
    res = apply_deck_palette(deck_dir, {'primary': '#0E7A5F', 'bg': '#101418'})
    assert res['ok'], res
    colors = _manifest(deck_dir)['theme']['colors']
    assert colors['primary'] == '#0E7A5F' and colors['bg'] == '#101418'
    # Untouched slots keep the bound theme's values.
    from lib.design_sys.themes import get_theme
    base = build_theme_tokens(get_theme('paper-engineer'))['colors']
    assert colors['ink'] == base['ink']


def test_apply_deck_palette_rejects_unknown_slot_and_bad_hex(deck_dir):
    res = apply_deck_palette(deck_dir, {'brand': '#123456'})
    assert not res['ok'] and 'unknown palette slots' in res['errors'][0]
    res2 = apply_deck_palette(deck_dir, {'primary': 'red'})
    assert not res2['ok'] and 'hex' in res2['errors'][0]


# ── apply_deck_fonts / composite restyle ──────────────────

def test_apply_deck_fonts_resolves_registry_faces(deck_dir):
    res = apply_deck_restyle(deck_dir, fonts={'title': 'noto-serif-sc',
                                              'body': 'MiSans'})
    assert res['ok'], res
    styles = _manifest(deck_dir)['theme']['textStyles']
    assert styles['title']['fontFamily'] == 'Noto Serif SC'
    assert styles['body']['fontFamily'] == 'MiSans'
    assert res['changes'][0]['op'] == 'set_fonts'

    bad = apply_deck_restyle(deck_dir, fonts={'title': 'Comic Sans MS'})
    assert not bad['ok'] and 'unknown face' in bad['errors'][0]
    bad_role = apply_deck_restyle(deck_dir, fonts={'footnote': 'MiSans'})
    assert not bad_role['ok'] and 'unknown font roles' in bad_role['errors'][0]


def test_composite_restyle_orders_theme_then_palette_then_fonts(deck_dir):
    res = apply_deck_restyle(
        deck_dir, theme_id='midnight-navy',
        colors={'primary': '#FF5500'}, fonts={'title': 'MiSans'})
    assert res['ok'], res
    theme = _manifest(deck_dir)['theme']
    # Palette override beats the NEW theme's default (ordering contract).
    assert theme['colors']['primary'] == '#FF5500'
    from lib.design_sys.themes import get_theme
    navy = build_theme_tokens(get_theme('midnight-navy'))
    assert theme['colors']['bg'] == navy['colors']['bg']
    assert theme['textStyles']['title']['fontFamily'] == 'MiSans'
    assert [c['op'] for c in res['changes']] == [
        'set_theme', 'set_palette', 'set_fonts']


def test_restyle_requires_at_least_one_mutation(deck_dir):
    res = apply_deck_restyle(deck_dir)
    assert not res['ok']


def test_export_failure_restores_manifest(deck_dir, monkeypatch):
    def _boom(deck, out, **kw):
        raise RuntimeError('pptx writer exploded')
    monkeypatch.setattr('lib.slides.export_pptx.export_pptx', _boom)
    before = open(os.path.join(deck_dir, 'deck.pptd'),
                  encoding='utf-8').read()
    res = apply_deck_theme(deck_dir, 'midnight-navy')
    assert not res['ok'] and 'export failed' in res['errors'][0]
    assert open(os.path.join(deck_dir, 'deck.pptd'),
                encoding='utf-8').read() == before
