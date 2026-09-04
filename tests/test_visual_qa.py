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
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lib.design_sys.visual_qa as vqa  # noqa: E402
import lib.design_sys.temporal_qa as tqa  # noqa: E402

pytestmark = pytest.mark.unit


def test_temporal_qa_waits_for_render_ready_assets():
    assert 'document.fonts.ready' in tqa._READINESS_JS
    assert 'img.decode' in tqa._READINESS_JS
    assert len(tqa.DEFAULT_PROGRESS_POINTS) == 4
    assert tqa.DEFAULT_PROGRESS_POINTS[-2:] == (0.8, 0.94)


# ── findings parsing ──────────────────────────────────────

class TestParseFindings:
    def test_annotation_grounding_is_a_first_class_axis(self):
        ids = {item[0] for item in vqa.QA_CHECKLIST}
        assert 'annotation-grounding' in ids
        assert '窗户' in vqa._QA_PROMPT_ZH

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
        monkeypatch.setenv('TOFU_PRODUCTION_LLM_MAX_429_ATTEMPTS', '7')
        img = tmp_path / 'f.png'
        img.write_bytes(b'\x89PNG\r\n\x1a\n' + b'\0' * 100)
        monkeypatch.setattr(vqa, '_vision_model', lambda: 'vlm-x')
        monkeypatch.setattr(
            'lib.model_info._capabilities.model_supports_vision',
            lambda m: True)
        seen = {}

        def _dispatch(messages, **kw):
            seen['msg'] = messages
            seen['kwargs'] = kw
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
        assert any('逐条从标注文字沿线检查到端点' in (p.get('text') or '')
                   for p in parts)
        assert any(p.get('type') == 'image_url' for p in parts)
        assert seen['kwargs']['max_retries'] == 2
        assert seen['kwargs']['max_429_attempts'] == 7

    def test_abort_during_dispatch_discards_late_vlm_reply(
            self, tmp_path, monkeypatch):
        img = tmp_path / 'f.png'
        img.write_bytes(b'\x89PNG\r\n\x1a\n' + b'\0' * 100)
        monkeypatch.setattr(vqa, '_vision_model', lambda: 'vlm-x')
        monkeypatch.setattr(
            'lib.model_info._capabilities.model_supports_vision',
            lambda m: True)
        abort_event = threading.Event()

        def late_reply(messages, **kwargs):
            assert kwargs['abort_check']() is False
            abort_event.set()
            return '{"findings": []}', {}

        monkeypatch.setattr('lib.llm_dispatch.api.dispatch_chat', late_reply)
        out = vqa.qa_frame(
            str(img), abort_check=abort_event.is_set,
            max_429_attempts=7)

        assert out['skipped'] and not out['ok']
        assert out['reason'] == 'aborted after visual QA'

    def test_oversized_frame_is_rejected_before_dispatch(
            self, tmp_path, monkeypatch):
        img = tmp_path / 'large.png'
        img.write_bytes(b'x' * 101)
        monkeypatch.setattr(vqa, '_MAX_QA_IMAGE_BYTES', 100)
        monkeypatch.setattr(vqa, '_vision_model', lambda: 'vlm-x')
        calls = []
        monkeypatch.setattr(
            'lib.llm_dispatch.api.dispatch_chat',
            lambda *args, **kwargs: calls.append('dispatch'))

        out = vqa.qa_frame(str(img))

        assert out['skipped'] and 'limit' in out['reason']
        assert calls == []

    def test_input_digest_matches_dispatch_and_changes_with_pixels(
            self, tmp_path, monkeypatch):
        img = tmp_path / 'frame.png'
        img.write_bytes(b'frame-v1')
        monkeypatch.setattr(
            'lib.model_info._capabilities.model_supports_vision',
            lambda _model: True)
        monkeypatch.setattr(
            'lib.llm_dispatch.api.dispatch_chat',
            lambda *_args, **_kwargs: ('{"findings": []}', {}))

        identity = vqa.qa_frame_input_sha256(
            str(img), subject='幻灯片页面', model='vlm-x')
        result = vqa.qa_frame(
            str(img), subject='幻灯片页面', model='vlm-x')
        assert result['input_sha256'] == identity

        img.write_bytes(b'frame-v2')
        changed = vqa.qa_frame_input_sha256(
            str(img), subject='幻灯片页面', model='vlm-x')
        assert changed != identity
        assert vqa.qa_frame_input_sha256(
            str(img), subject='different', model='vlm-x') != changed
        assert vqa.qa_frame_input_sha256(
            str(img), subject='幻灯片页面', model='vlm-y') != changed

    def test_shared_visual_cache_roundtrip_rejects_tampering(self, tmp_path):
        cache_path = tmp_path / 'visual-cache.json'
        identity = 'a' * 64
        cache = vqa.load_visual_qa_cache(
            str(cache_path), version='test-v1', max_entries=1,
            max_bytes=64 * 1024)
        result = {
            'ok': True, 'skipped': False, 'reason': '',
            'findings': [{'check': 'contrast', 'element': 'title',
                          'issue': 'low contrast', 'severity': 'major',
                          'fix': 'darken'}],
            'has_blocker': False, 'summary': '1 finding(s)',
            'input_sha256': identity,
        }

        assert vqa.remember_visual_qa_result(
            cache, str(cache_path), 'frame', identity, result,
            max_entries=1, max_bytes=64 * 1024, max_findings=8)
        loaded = vqa.load_visual_qa_cache(
            str(cache_path), version='test-v1', max_entries=1,
            max_bytes=64 * 1024)
        reused = vqa.cached_visual_qa_result(
            loaded['entries']['frame'], identity, max_findings=8)
        assert reused and reused['reused'] is True
        assert reused['findings'][0]['issue'] == 'low contrast'
        assert vqa.cached_visual_qa_result(
            loaded['entries']['frame'], 'b' * 64, max_findings=8) is None

        loaded['entries']['frame']['result']['findings'][0]['issue'] = 'fake'
        assert vqa.cached_visual_qa_result(
            loaded['entries']['frame'], identity, max_findings=8) is None

    def test_shared_visual_cache_refuses_one_result_over_file_budget(
            self, tmp_path):
        cache_path = tmp_path / 'tiny-cache.json'
        identity = 'c' * 64
        cache = {'version': 'test-v1', 'entries': {}}
        result = {
            'ok': True,
            'findings': [{'check': 'contrast', 'element': 'title',
                          'issue': 'x' * 400, 'severity': 'major',
                          'fix': 'y' * 400}],
            'input_sha256': identity,
        }

        assert not vqa.remember_visual_qa_result(
            cache, str(cache_path), 'frame', identity, result,
            max_entries=1, max_bytes=128, max_findings=8)
        assert cache['entries'] == {}
        assert not cache_path.exists()


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
        monkeypatch.setattr(tqa, "screenshot_timeline_contact_sheet",
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
        monkeypatch.setattr(tqa, "screenshot_timeline_contact_sheet",
                            lambda *a, **k: a[1])
        monkeypatch.setattr(vqa, 'qa_frame', lambda *a, **k: {
            'ok': True, 'skipped': False, 'reason': '',
            'findings': [{'check': 'contrast', 'element': '标题',
                          'issue': '对比不足', 'severity': 'major',
                          'fix': '换色'}],
            'has_blocker': False, 'summary': '1'})
        seen = {}
        prompt_context = object()
        context_calls = []

        def _context_provider():
            context_calls.append('prepare')
            return prompt_context

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
            total_scenes=1,
            author_prompt_context_provider=_context_provider)
        assert seen.get('extra_findings'), 'repair got no QA findings'
        assert seen.get('prompt_context') is prompt_context
        assert context_calls == ['prepare']
        assert '对比不足' in seen['extra_findings'][0]
        # Repaired HTML is sealed to the local runtime *before* the sole
        # guarded commit (no prior index.html → no regression).
        assert '<html>repaired</html>' in out
        assert 'assets/gsap-3.14.2.min.js' in out
        assert 'https://' not in out
        assert open(index_path).read() == out

    def test_clean_findings_keep_html(self, tmp_path, monkeypatch):
        from lib.motion_video import engine
        scene = self._scene()
        html = '<html>authored</html>'
        monkeypatch.setattr(vqa, 'visual_qa_available', lambda: (True, ''))
        monkeypatch.setattr(tqa, "screenshot_timeline_contact_sheet",
                            lambda *a, **k: a[1])
        monkeypatch.setattr(vqa, 'qa_frame', lambda *a, **k: {
            'ok': True, 'skipped': False, 'reason': '',
            'findings': [{'severity': 'minor', 'issue': 'x', 'fix': '',
                          'check': '', 'element': ''}],
            'has_blocker': False, 'summary': ''})
        context_calls = []
        out = engine._visual_qa_round(
            {}, scene, str(tmp_path), str(tmp_path / 'index.html'), html,
            width=1080, height=1440, duration=5.0, scene_index=1,
            total_scenes=1,
            author_prompt_context_provider=lambda: context_calls.append(
                'unexpected'))
        assert out == html
        assert context_calls == []

    def test_motion_visual_qa_reuses_exact_pixels_and_reruns_changes(
            self, tmp_path, monkeypatch):
        from pathlib import Path

        from lib.motion_video import engine

        scene = self._scene()
        scene_dir = str(tmp_path)
        html = '<html>authored composition with a graphic</html>'
        pixels = {'value': b'contact-sheet-v1'}
        calls = []
        monkeypatch.setattr(vqa, 'visual_qa_available', lambda: (True, ''))

        def _capture(_scene_dir, output, **_kwargs):
            Path(output).write_bytes(pixels['value'])
            return output

        def _qa(path, **kwargs):
            calls.append(kwargs['label'])
            identity = vqa.qa_frame_input_sha256(
                path, theme=kwargs.get('theme'),
                subject=kwargs.get('subject', '视频帧'),
                model=kwargs.get('model', ''),
                max_tokens=kwargs.get('max_tokens', 1500))
            return {
                'ok': True, 'skipped': False, 'reason': '', 'findings': [],
                'has_blocker': False, 'summary': '0 finding(s)',
                'input_sha256': identity,
            }

        monkeypatch.setattr(tqa, 'screenshot_timeline_contact_sheet',
                            _capture)
        monkeypatch.setattr(vqa, 'qa_frame', _qa)

        for _ in range(2):
            task = {'qa_model': 'vlm-x'}
            out = engine._visual_qa_round(
                task, scene, scene_dir, str(tmp_path / 'index.html'), html,
                width=1080, height=1440, duration=5.0, scene_index=1,
                total_scenes=1)
            assert out == html
        assert calls == ['scene-001']

        pixels['value'] = b'contact-sheet-v2'
        engine._visual_qa_round(
            {'qa_model': 'vlm-x'}, scene, scene_dir,
            str(tmp_path / 'index.html'), html, width=1080, height=1440,
            duration=5.0, scene_index=1, total_scenes=1)
        assert calls == ['scene-001', 'scene-001']

    def test_cached_actionable_motion_findings_still_enter_repair(
            self, tmp_path, monkeypatch):
        from pathlib import Path

        from lib.motion_video import _scene_author as sa
        from lib.motion_video import engine

        scene = self._scene()
        html = '<html>authored composition with a graphic</html>'
        qa_calls = []
        author_calls = []
        monkeypatch.setattr(vqa, 'visual_qa_available', lambda: (True, ''))
        monkeypatch.setattr(engine, '_emit', lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            tqa, 'screenshot_timeline_contact_sheet',
            lambda _scene_dir, output, **_kwargs: (
                Path(output).write_bytes(b'unchanged-pixels') or output))

        def _qa(path, **kwargs):
            qa_calls.append(kwargs['label'])
            identity = vqa.qa_frame_input_sha256(
                path, theme=kwargs.get('theme'),
                subject=kwargs.get('subject', '视频帧'),
                model=kwargs.get('model', ''))
            return {
                'ok': True, 'skipped': False, 'reason': '',
                'findings': [{'check': 'contrast', 'element': 'title',
                              'issue': 'low contrast', 'severity': 'major',
                              'fix': 'darken'}],
                'has_blocker': False, 'summary': '1 finding(s)',
                'input_sha256': identity,
            }

        def _author(*_args, **kwargs):
            author_calls.append(kwargs['extra_findings'])
            return {'mode': 'template', 'html': html,
                    'rounds': 1, 'tokens': 0}

        monkeypatch.setattr(vqa, 'qa_frame', _qa)
        monkeypatch.setattr(sa, 'author_scene', _author)
        monkeypatch.setattr(sa, 'save_draft', lambda *_args: None)

        for _ in range(2):
            out = engine._visual_qa_round(
                {'qa_model': 'vlm-x'}, scene, str(tmp_path),
                str(tmp_path / 'index.html'), html, width=1080, height=1440,
                duration=5.0, scene_index=1, total_scenes=1,
                author_prompt_context_provider=lambda: object())
            assert out == html

        assert qa_calls == ['scene-001']
        assert len(author_calls) == 2
        assert all('low contrast' in findings[0]
                   for findings in author_calls)

    def test_recipe_anchors_drive_temporal_capture_and_vlm_brief(
            self, tmp_path, monkeypatch):
        from lib.motion_video import engine
        from lib.motion_video._creative_plan import normalise_scene_plan

        scene = self._scene()
        scene['text'] = '性能提升 44%'
        normalise_scene_plan(scene, 1, 3)
        scene['qa_progresses'] = [0.05, 0.46, 0.88]
        html = '<html>authored composition with a metric graphic</html>'
        seen = {}
        monkeypatch.setattr(vqa, 'visual_qa_available', lambda: (True, ''))

        def _capture(*args, **kwargs):
            seen['progresses'] = kwargs.get('progresses')
            return args[1]

        def _qa(*args, **kwargs):
            seen['subject'] = kwargs.get('subject')
            return {'ok': True, 'skipped': False, 'reason': '',
                    'findings': [], 'has_blocker': False, 'summary': '0'}

        monkeypatch.setattr(tqa, 'screenshot_timeline_contact_sheet',
                            _capture)
        monkeypatch.setattr(vqa, 'qa_frame', _qa)
        out = engine._visual_qa_round(
            {}, scene, str(tmp_path), str(tmp_path / 'index.html'), html,
            width=1080, height=1440, duration=5.0, scene_index=1,
            total_scenes=3)
        assert out == html
        assert seen['progresses'] == [0.05, 0.46, 0.88]
        assert '5% / 46% / 88%' in seen['subject']
        assert 'hook-counter-burst' in seen['subject']
        assert '最低落定停留' in seen['subject']

    def test_qa_outage_keeps_html(self, tmp_path, monkeypatch):
        from lib.motion_video import engine
        scene = self._scene()
        html = '<html>authored</html>'
        monkeypatch.setattr(vqa, 'visual_qa_available', lambda: (True, ''))

        def _boom(*a, **k):
            raise RuntimeError('chromium gone')
        monkeypatch.setattr(tqa, "screenshot_timeline_contact_sheet", _boom)
        out = engine._visual_qa_round(
            {}, scene, str(tmp_path), str(tmp_path / 'index.html'), html,
            width=1080, height=1440, duration=5.0, scene_index=1,
            total_scenes=1)
        assert out == html
