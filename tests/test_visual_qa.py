"""tests/test_visual_qa.py — visual-QA stage contracts (design-system P2).

Pins: the degrade ladder (skip ≠ fail ≠ clean), findings parsing discipline,
the author's extra_findings channel (a QA call must force the repair loop —
the zero-spend draft adoption would otherwise swallow it), and the engine's
QA round wiring (template scenes skipped, repair guarded by the
no-regression commit).
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.design_sys import visual_qa as vqa  # noqa: E402

pytestmark = pytest.mark.unit


# ── findings parsing ──────────────────────────────────────

class TestParseFindings:
    def test_valid_payload(self):
        content = ('{"findings": [{"check": "contrast", "element": "标题", '
                   '"issue": "副标题与背景对比不足", "severity": "major", '
                   '"fix": "把 muted 换成 ink"}]}')
        out = vqa._parse_findings(content)
        assert out and out[0]['severity'] == 'major'
        assert out[0]['check'] == 'contrast'

    def test_fenced_and_noisy_reply(self):
        content = '好的,我来看一下:\n```json\n{"findings": []}\n```'
        assert vqa._parse_findings(content) == []

    def test_junk_is_none(self):
        assert vqa._parse_findings('完全不是 JSON') is None
        assert vqa._parse_findings('{"no_findings": true}') is None

    def test_severity_normalised_and_empty_issue_dropped(self):
        content = ('{"findings": ['
                   '{"issue": "a", "severity": "BLOCKER"}, '
                   '{"issue": "b", "severity": "weird"}, '
                   '{"issue": ""}, '
                   '{"issue": "c", "check": "not-a-check"}'
                   ']}')
        out = vqa._parse_findings(content)
        assert [f['severity'] for f in out] == ['blocker', 'minor', 'minor']
        assert out[2]['check'] == ''   # unknown check id is not kept

    def test_findings_text(self):
        text = vqa.findings_text([
            {'severity': 'major', 'issue': '溢出', 'fix': '缩短'},
            {'severity': 'minor', 'issue': '对齐', 'fix': ''}])
        assert '[major] 溢出' in text and '修法: 缩短' in text
        assert '[minor] 对齐' in text


# ── degrade ladder ────────────────────────────────────────

class TestDegrade:
    def test_missing_image_skips(self):
        out = vqa.qa_frame('/no/such/frame.png')
        assert out['skipped'] and not out['ok']

    def test_no_vision_slot_skips(self, tmp_path, monkeypatch):
        img = tmp_path / 'f.png'
        img.write_bytes(b'\x89PNG\r\n\x1a\n' + b'\0' * 100)
        monkeypatch.setattr(vqa, '_vision_model', lambda: '')
        out = vqa.qa_frame(str(img))
        assert out['skipped'] and 'vision' in out['reason']

    def test_non_vision_model_skips(self, tmp_path, monkeypatch):
        img = tmp_path / 'f.png'
        img.write_bytes(b'\x89PNG\r\n\x1a\n' + b'\0' * 100)
        monkeypatch.setattr(vqa, '_vision_model', lambda: 'text-only-model')
        monkeypatch.setattr(
            'lib.model_info._capabilities.model_supports_vision',
            lambda m: False)
        out = vqa.qa_frame(str(img), model='text-only-model')
        assert out['skipped']

    def test_dispatch_failure_is_not_ok_not_skipped(self, tmp_path,
                                                    monkeypatch):
        img = tmp_path / 'f.png'
        img.write_bytes(b'\x89PNG\r\n\x1a\n' + b'\0' * 100)
        monkeypatch.setattr(vqa, '_vision_model', lambda: 'vlm-x')
        monkeypatch.setattr(
            'lib.model_info._capabilities.model_supports_vision',
            lambda m: True)

        def _boom(messages, **kw):
            raise RuntimeError('gateway 500')
        monkeypatch.setattr('lib.llm_dispatch.api.dispatch_chat', _boom)
        out = vqa.qa_frame(str(img))
        assert not out['ok'] and not out['skipped']
        assert 'gateway 500' in out['reason']

    def test_happy_path_with_theme(self, tmp_path, monkeypatch):
        img = tmp_path / 'f.png'
        img.write_bytes(b'\x89PNG\r\n\x1a\n' + b'\0' * 100)
        monkeypatch.setattr(vqa, '_vision_model', lambda: 'vlm-x')
        monkeypatch.setattr(
            'lib.model_info._capabilities.model_supports_vision',
            lambda m: True)
        seen = {}

        def _dispatch(messages, **kw):
            seen['msg'] = messages
            return ('{"findings": [{"check": "overflow", "element": "标题", '
                    '"issue": "标题溢出", "severity": "blocker", "fix": "缩短"}]}',
                    {})
        monkeypatch.setattr('lib.llm_dispatch.api.dispatch_chat', _dispatch)

        from lib.design_sys.themes import get_theme
        out = vqa.qa_frame(str(img), theme=get_theme('deep-console'),
                           label='scene-001')
        assert out['ok'] and out['has_blocker']
        # The theme palette must reach the prompt (theme-fidelity check).
        parts = seen['msg'][0]['content']
        assert any('#101418' in (p.get('text') or '') for p in parts)
        assert any(p.get('type') == 'image_url' for p in parts)


# ── author extra_findings channel ─────────────────────────

class TestAuthorExtraFindings:
    def test_extra_findings_force_the_repair_loop(self, tmp_path,
                                                  monkeypatch):
        """A QA call with findings must NOT take the zero-spend draft
        adoption — the draft passes the programmatic gates (that is why it
        is on disk), so adoption would swallow the aesthetic findings."""
        from lib.motion_video import _scene_author as sa

        scene_dir = str(tmp_path)
        sa.save_draft(scene_dir,
                      '<html><div data-composition-id="main" '
                      'data-duration="5">draft</div></html>')
        called = {}

        monkeypatch.setattr(sa, '_full_gate', lambda *a, **k: [])

        def _fake_once(scene, scene_dir, **kw):
            called.update(kw)
            return {'outcome': 'authored', 'html': '<html>fixed</html>',
                    'rounds': 1, 'tokens': 100, 'detail': ''}
        monkeypatch.setattr(sa, '_author_once', _fake_once)
        monkeypatch.setattr(sa, 'run_agent_loop', None, raising=False)

        res = sa.author_scene({'id': 'scene-001', 'text': 'x',
                               'start': 0, 'end': 5},
                              scene_dir, width=1080, height=1440, duration=5.0,
                              scene_index=1, total_scenes=1,
                              extra_findings=['- [major] 对比度不足'])
        assert res['mode'] == 'authored'
        assert called.get('extra_findings') == ['- [major] 对比度不足']
        assert 'draft' in (called.get('seed_html') or '')

    def test_zero_spend_adoption_intact_without_findings(self, tmp_path,
                                                         monkeypatch):
        from lib.motion_video import _scene_author as sa
        scene_dir = str(tmp_path)
        sa.save_draft(scene_dir,
                      '<html><div data-composition-id="main" '
                      'data-duration="5">clean draft</div></html>')
        monkeypatch.setattr(sa, '_full_gate', lambda *a, **k: [])
        res = sa.author_scene({'id': 's', 'text': 'x', 'start': 0, 'end': 5},
                              scene_dir, width=1080, height=1440, duration=5.0,
                              scene_index=1, total_scenes=1)
        assert res['detail'] == 'adopted draft' and res['rounds'] == 0


# ── engine wiring ─────────────────────────────────────────

class TestEngineRound:
    def _scene(self):
        return {'id': 'scene-001', 'text': '标题', 'start': 0, 'end': 5}

    def test_template_scene_skips_qa(self, tmp_path, monkeypatch):
        from lib.motion_video import engine
        from lib.motion_video._template import render_scene_html
        scene = self._scene()
        html = render_scene_html(scene, duration=5.0)
        task = {}
        called = {'shot': False}
        monkeypatch.setattr(vqa, 'screenshot_composition',
                            lambda *a, **k: called.__setitem__('shot', True))
        out = engine._visual_qa_round(
            task, scene, str(tmp_path), str(tmp_path / 'index.html'), html,
            width=1080, height=1440, duration=5.0, scene_index=1,
            total_scenes=1)
        assert out == html and not called['shot']

    def test_blocker_findings_trigger_guarded_repair(self, tmp_path,
                                                     monkeypatch):
        from lib.motion_video import engine
        from lib.motion_video import _scene_author as sa
        scene = self._scene()
        scene_dir = str(tmp_path)
        index_path = os.path.join(scene_dir, 'index.html')
        html = '<html>authored composition with a graphic</html>'
        task = {'_emit': [], 'topic': 'x', 'task_id': 't-qa-1'}

        monkeypatch.setattr(vqa, 'visual_qa_available',
                            lambda: (True, ''))
        monkeypatch.setattr(vqa, 'screenshot_composition',
                            lambda *a, **k: a[1])
        monkeypatch.setattr(vqa, 'qa_frame', lambda *a, **k: {
            'ok': True, 'skipped': False, 'reason': '',
            'findings': [{'check': 'contrast', 'element': '标题',
                          'issue': '对比不足', 'severity': 'major',
                          'fix': '换色'}],
            'has_blocker': False, 'summary': '1'})
        seen = {}

        def _author(sc, sd, **kw):
            seen.update(kw)
            return {'mode': 'authored', 'html': '<html>repaired</html>',
                    'rounds': 1, 'tokens': 10}
        monkeypatch.setattr(sa, 'author_scene', _author)
        monkeypatch.setattr(engine, 'author_scene', _author, raising=False)
        monkeypatch.setattr(sa, 'save_draft', lambda sd, h: None)

        import lib.motion_video._scene_author  # ensure module import
        monkeypatch.setattr('lib.motion_video._scene_author.author_scene',
                            _author)
        monkeypatch.setattr('lib.motion_video._scene_author.save_draft',
                            lambda sd, h: None)

        out = engine._visual_qa_round(
            task, scene, scene_dir, index_path, html,
            width=1080, height=1440, duration=5.0, scene_index=1,
            total_scenes=1)
        assert seen.get('extra_findings'), 'repair got no QA findings'
        assert '对比不足' in seen['extra_findings'][0]
        # Repaired html was committed (no prior index.html → no regression).
        assert out == '<html>repaired</html>'
        assert open(index_path).read() == '<html>repaired</html>'

    def test_clean_findings_keep_html(self, tmp_path, monkeypatch):
        from lib.motion_video import engine
        scene = self._scene()
        html = '<html>authored</html>'
        monkeypatch.setattr(vqa, 'visual_qa_available', lambda: (True, ''))
        monkeypatch.setattr(vqa, 'screenshot_composition',
                            lambda *a, **k: a[1])
        monkeypatch.setattr(vqa, 'qa_frame', lambda *a, **k: {
            'ok': True, 'skipped': False, 'reason': '',
            'findings': [{'severity': 'minor', 'issue': 'x', 'fix': '',
                          'check': '', 'element': ''}],
            'has_blocker': False, 'summary': ''})
        out = engine._visual_qa_round(
            {}, scene, str(tmp_path), str(tmp_path / 'index.html'), html,
            width=1080, height=1440, duration=5.0, scene_index=1,
            total_scenes=1)
        assert out == html

    def test_qa_outage_keeps_html(self, tmp_path, monkeypatch):
        from lib.motion_video import engine
        scene = self._scene()
        html = '<html>authored</html>'
        monkeypatch.setattr(vqa, 'visual_qa_available', lambda: (True, ''))

        def _boom(*a, **k):
            raise RuntimeError('chromium gone')
        monkeypatch.setattr(vqa, 'screenshot_composition', _boom)
        out = engine._visual_qa_round(
            {}, scene, str(tmp_path), str(tmp_path / 'index.html'), html,
            width=1080, height=1440, duration=5.0, scene_index=1,
            total_scenes=1)
        assert out == html
