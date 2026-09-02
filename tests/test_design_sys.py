"""tests/test_design_sys.py — design-system P1 contract pins.

Covers: font registry integrity (licenses, pins, scenarios), the theme
registry (palette keys, font-id validity, per-scenario defaults), scenario
classification, the prompt block's no-ghost-family rule, the bibles' presence
and hard content, and the theme threading through motion_video's template
(the fallback must carry the film's palette, and matches_template must
re-render with the SAME theme or it misfires).
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lib.design_sys.fonts as fonts  # noqa: E402
import lib.design_sys.themes as themes  # noqa: E402

pytestmark = pytest.mark.unit


# ── Font registry integrity ───────────────────────────────

class TestFontRegistry:
    def test_unique_ids(self):
        ids = [f.id for f in fonts.FONT_REGISTRY]
        assert len(ids) == len(set(ids))

    def test_every_source_is_pinned(self):
        for f in fonts.FONT_REGISTRY:
            assert f.sources, f'{f.id} ships no weight'
            for s in f.sources:
                assert s.url.startswith('https://'), f'{f.id} url'
                assert len(s.sha256) == 64, f'{f.id} w{s.weight} sha256'
                assert s.size > 8000, f'{f.id} w{s.weight} size floor'
                assert s.fmt in ('woff2', 'opentype', 'truetype')

    def test_every_license_exists_and_has_evidence_url(self):
        for f in fonts.FONT_REGISTRY:
            lic = fonts.LICENSES.get(f.license_id)
            assert lic, f'{f.id} references unknown license {f.license_id}'
            assert lic['url'].startswith('https://')
            assert lic['note']

    def test_every_scenario_tag_is_known(self):
        for f in fonts.FONT_REGISTRY:
            for sid in f.scenarios:
                assert sid in themes.SCENARIOS, f'{f.id} → unknown {sid}'

    def test_every_scenario_has_a_cjk_display_and_body(self):
        """A scenario whose pairing lacks a CJK face renders Chinese in the
        host's serif fallback — the exact defect this registry exists to
        kill."""
        for sid in themes.SCENARIOS:
            display, body, latin = fonts.get_pairing(sid)
            assert display and body and latin
            assert 'latin' in latin.roles
            for f in (display, body):
                assert 'latin' not in f.roles, (
                    f'{sid}: {f.id} cannot serve CJK {f.roles}')

    def test_cjk_coverage(self):
        """The starter set must cover the CJK display/body needs of every
        scenario — i.e. each pairing's display+body faces are CJK-capable."""
        cjk = {f.id for f in fonts.FONT_REGISTRY if 'latin' not in f.roles}
        assert len(cjk) >= 6

    def test_unknown_font_id(self):
        assert fonts.get_font('no-such-font') is None

    def test_ensure_font_unknown_id_returns_empty(self):
        assert fonts.ensure_font('no-such-font', 400, download=False) == ''


# ── ensure_font store behaviour (mocked network) ─────────

class TestEnsureFont:
    def _fake_bytes(self, src):
        return b'\0' * src.size

    def test_download_verify_store_and_alias(self, tmp_path, monkeypatch):
        import hashlib

        import lib.design_sys._store as store
        monkeypatch.setattr(store, 'store_root', lambda: str(tmp_path))
        monkeypatch.setattr(fonts, '_store_dir',
                            lambda: str(tmp_path / 'fonts'))

        import dataclasses
        face = fonts.get_font('misans')
        src = face.sources[0]
        payload = b'\x00\x01\x02\x03' * 4096  # 16 KB
        real = src.__class__(src.weight, src.url,
                             hashlib.sha256(payload).hexdigest(),
                             len(payload), src.fmt)
        patched = dataclasses.replace(face, sources=(real,))
        monkeypatch.setattr(fonts, '_BY_ID',
                            {**fonts._BY_ID, 'misans': patched})

        class _Resp:
            status_code = 200
            content = payload

        import lib.http_client as hc
        monkeypatch.setattr(hc, 'http_get', lambda url, timeout=0: _Resp())
        path = fonts.ensure_font('misans', 400)
        assert path and os.path.isfile(path)
        assert os.path.getsize(path) == len(payload)
        # Second call = cache hit (no network).
        def _boom(url, timeout=0):
            raise AssertionError('network hit on a cached font')
        monkeypatch.setattr(hc, 'http_get', _boom)
        assert fonts.ensure_font('misans', 400) == path

    def test_sha_mismatch_refused(self, tmp_path, monkeypatch):
        import lib.design_sys._store as store
        monkeypatch.setattr(store, 'store_root', lambda: str(tmp_path))
        monkeypatch.setattr(fonts, '_store_dir',
                            lambda: str(tmp_path / 'fonts'))

        class _Resp:
            status_code = 200
            content = b'\xAA' * 16384

        import lib.http_client as hc
        monkeypatch.setattr(hc, 'http_get', lambda url, timeout=0: _Resp())
        # Real registry pins will NOT match b'\xAA'*16k → refused.
        assert fonts.ensure_font('misans', 400) == ''


# ── Themes ────────────────────────────────────────────────

class TestThemes:
    _COLOR_KEYS = {'bg', 'ink', 'primary', 'accent', 'muted', 'hairline'}

    def test_every_theme_has_full_palette_and_valid_fonts(self):
        for t in themes.THEMES:
            assert set(t.colors) == self._COLOR_KEYS, t.id
            for k, v in t.colors.items():
                assert v.startswith('#') and len(v) in (4, 7, 9), (t.id, k, v)
            assert set(t.fonts) == {'display', 'body', 'latin'}, t.id
            for role, fid in t.fonts.items():
                assert fonts.get_font(fid), f'{t.id}.{role} → unknown {fid}'
            assert t.scenario in themes.SCENARIOS

    def test_every_scenario_has_a_default_theme(self):
        for sid in themes.SCENARIOS:
            t = themes.get_theme(themes.default_theme_id(sid))
            assert t is not None and t.scenario == sid

    def test_two_themes_per_scenario(self):
        for sid in themes.SCENARIOS:
            assert len(themes.list_themes(scenario=sid)) >= 2, sid

    def test_classify(self):
        assert themes.classify_scenario('分布式系统架构评审') == 'tech-engineering'
        assert themes.classify_scenario('新能源汽车融资路演') == 'business-plan'
        assert themes.classify_scenario('硕士学位论文答辩') == 'academic-research'
        assert themes.classify_scenario('Q3 经营业绩汇报') == 'management-report'
        assert themes.classify_scenario('少儿编程入门课件') == 'education-training'
        assert themes.classify_scenario('品牌创意作品集') == 'brand-creative'
        assert themes.classify_scenario('行业研究投资分析') == 'analysis-decision'
        # Unclassifiable still lands on ONE coherent default, never per-scene
        assert themes.classify_scenario('xyzzy foobar') == 'tech-engineering'

    def test_prompt_block_binds_palette_and_prohibitions(self):
        t = themes.get_theme('paper-engineer')
        block = themes.theme_prompt_block(t, for_video=True)
        assert '#F7F7F5' in block and '#C0652B' in block
        assert 'MiSans' in block and 'Liter' in block
        assert 'no blue-purple gradients' in block
        assert 'staggered' in block  # motion note only for video
        assert themes.theme_prompt_block(t, for_video=False).find(
            'staggered') == -1

    def test_prompt_block_remaps_unstaged_families(self):
        """Naming an unstaged family is the silent-substitution trap: when a
        face failed to stage, its role must remap to a staged one."""
        t = themes.get_theme('paper-engineer')
        block = themes.theme_prompt_block(
            t, staged_font_ids={'misans'})  # liter failed to stage
        assert 'Liter' not in block
        block2 = themes.theme_prompt_block(t, staged_font_ids=set())
        assert 'MiSans' not in block2  # nothing staged → no family named


# ── Bibles ────────────────────────────────────────────────

class TestBibles:
    def test_every_scenario_bible_exists(self):
        index = themes.BIBLE_INDEX()
        for sid in themes.SCENARIOS:
            assert index.get(sid), f'missing bible for {sid}'

    def test_general_bible_carries_the_hard_prohibitions(self):
        text = themes.design_bible_text('tech-engineering')
        assert 'No card walls' in text
        assert 'No formulaic AI palettes' in text
        assert 'One accent color' in text

    def test_scenario_bible_content(self):
        text = themes.design_bible_text('academic-research',
                                        include_general=False)
        assert 'research question' in text.lower()

    def test_missing_bible_degrades_to_empty(self):
        assert themes.design_bible_text('no-such-scenario',
                                        include_general=False) == ''


# ── motion_video template theme threading ─────────────────

class TestTemplateTheme:
    def test_themed_template_carries_palette(self):
        from lib.motion_video._template import render_scene_html
        t = themes.get_theme('deep-console')
        html = render_scene_html(
            {'id': 'scene-001', 'text': '测试标题', 'start': 0, 'end': 5},
            duration=5.0, theme=t)
        assert '#101418' in html          # bg
        assert '#F4F7F6' in html          # ink
        assert '#F5B700' in html          # accent rule

    def test_legacy_template_unchanged_without_theme(self):
        from lib.motion_video._template import _GRADIENTS, render_scene_html
        html = render_scene_html(
            {'id': 'scene-001', 'text': 'x', 'start': 0, 'end': 5},
            duration=5.0, scene_index=2)
        assert _GRADIENTS[1] in html

    def test_matches_template_same_theme_required(self):
        """The marker catches marked cards regardless of theme; the re-render
        comparison (for MARKERLESS legacy cards) must hold theme constant —
        a themed card must not match an unthemed re-render and vice versa,
        or fallback cards get pinned as 'authored'."""
        from lib.motion_video._template import (TEMPLATE_MARKER,
                                                matches_template,
                                                render_scene_html)
        t = themes.get_theme('paper-engineer')
        scene = {'id': 'scene-001', 'text': '标题', 'start': 0, 'end': 5}
        themed = render_scene_html(scene, duration=5.0, theme=t)
        plain = render_scene_html(scene, duration=5.0)
        # Marker path: always detected, theme-independent.
        assert matches_template(themed, scene, duration=5.0)
        assert matches_template(plain, scene, duration=5.0, theme=t)
        # Markerless re-render path: theme must match exactly.
        themed_nm = themed.replace(TEMPLATE_MARKER, '')
        plain_nm = plain.replace(TEMPLATE_MARKER, '')
        assert matches_template(themed_nm, scene, duration=5.0, theme=t)
        assert not matches_template(themed_nm, scene, duration=5.0)
        assert matches_template(plain_nm, scene, duration=5.0)
        assert not matches_template(plain_nm, scene, duration=5.0, theme=t)
