"""tests/tasks_pkg/test_image_durable_refs.py — durable image handles across compaction.

Root cause: L1 image compaction stripped cold ``image_url`` blocks with only
"re-call tool if needed" left — even though the bytes were ALREADY retained
in the owner-scoped media store (``tofu-media:<id>`` / ``att_media_<id>``).
And any lane that JSON-encoded the ``__screenshot__`` dict put raw base64
into the text stream, where spilled artifacts paged undecodable slices.

Assertions:
  - ``_image_recovery_note`` converts tofu-media refs to att_media_ form.
  - ``strip_cold_images`` / ``compact_tool_results`` placeholders name the
    durable ref (fallback "re-call tool" preserved when none exists).
  - ``_resolve_media_ref`` returns kind 'image' bytes for image documents,
    so ``inspect_image("att_media_<id>")`` works on a retained screenshot.
  - ``budget_tool_result_v2`` and the PTC lanes degrade a ``__screenshot__``
    dict to its text fallback instead of JSON-dumping the dataUrl.

Run:  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/tasks_pkg/test_image_durable_refs.py -v
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

PIL = pytest.importorskip('PIL')
from PIL import Image  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────

def _ctx(messages, *, task=None):
    import lib.tasks_pkg.compaction._constants as compaction_constants
    from lib.tasks_pkg.compaction._steps import CompactionContext, make_constants
    import lib.tasks_pkg.compaction._builtin_steps  # noqa: F401
    return CompactionContext(
        messages=messages, conv_id='sim', task=task,
        constants=make_constants(compaction_constants, None),
        cache_prefix_count=0, ignore_cache_prefix=False,
        stamp_fn=lambda *a, **k: None)


def _image_msg(tc_id, char, *, sources=None):
    msg = {
        'role': 'tool', 'tool_call_id': tc_id, 'name': 'browser_preview_page',
        'content': [
            {'type': 'image_url', 'image_url': {
                'url': 'data:image/jpeg;base64,' + char * 4000}},
            {'type': 'text', 'text': 'Page preview — index.html'},
        ],
    }
    if sources:
        msg['_modelImageSources'] = list(sources)
    return msg


# ═══════════════════════════════════════════════════════════════════════
#  _image_recovery_note
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestImageRecoveryNote:

    def test_tofu_media_source_becomes_att_media(self):
        from lib.tasks_pkg.compaction._builtin_steps._shared import (
            _image_recovery_note,
        )
        msg = {'content': 'x', '_modelImageSources': ['tofu-media:abc123']}
        note = _image_recovery_note(msg)
        assert 'att_media_abc123' in note
        assert 'inspect_image' in note

    def test_tofu_media_url_inside_content_blocks(self):
        # Checkpoint-restored messages carry tofu-media URLs in the blocks.
        from lib.tasks_pkg.compaction._builtin_steps._shared import (
            _image_recovery_note,
        )
        msg = {'content': [
            {'type': 'image_url',
             'image_url': {'url': 'tofu-media:restored9'}},
        ]}
        assert 'att_media_restored9' in _image_recovery_note(msg)

    def test_upload_path_passes_through(self):
        from lib.tasks_pkg.compaction._builtin_steps._shared import (
            _image_recovery_note,
        )
        msg = {'content': 'x', '_modelImageSources': ['/api/images/u.png']}
        assert '/api/images/u.png' in _image_recovery_note(msg)

    def test_dedupe_and_limit(self):
        from lib.tasks_pkg.compaction._builtin_steps._shared import (
            _image_recovery_note,
        )
        msg = {'content': 'x', '_modelImageSources': [
            'tofu-media:a', 'tofu-media:a', 'tofu-media:b',
            'tofu-media:c', 'tofu-media:d', 'tofu-media:e']}
        note = _image_recovery_note(msg, limit=4)
        assert note.count('att_media_a') == 1
        assert 'att_media_e' not in note

    def test_no_sources_returns_empty(self):
        from lib.tasks_pkg.compaction._builtin_steps._shared import (
            _image_recovery_note,
        )
        assert _image_recovery_note({'content': 'x'}) == ''
        assert _image_recovery_note({'content': [
            {'type': 'image_url',
             'image_url': {'url': 'data:image/png;base64,AAAA'}}]}) == ''


# ═══════════════════════════════════════════════════════════════════════
#  strip_cold_images — placeholder carries the durable ref
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestStripColdImagesRecoveryRef:

    def _run(self, messages):
        from lib.tasks_pkg.compaction._steps import run_steps
        ctx = _ctx(messages)
        return run_steps(['strip_cold_images'], ctx)

    def test_cold_image_placeholder_names_att_media_ref(self):
        # _IMAGE_HOT_TAIL keeps the 2 newest images; the oldest is stripped.
        msgs = []
        for i, char in enumerate('ABC'):
            msgs.append({'role': 'assistant', 'tool_calls': [
                {'id': f't{i}', 'type': 'function',
                 'function': {'name': 'browser_preview_page', 'arguments': '{}'}}]})
            msgs.append(_image_msg(
                f't{i}', char,
                sources=['tofu-media:shot0'] if i == 0 else None))
        assert self._run(msgs) > 0
        cold = msgs[1]['content']
        assert isinstance(cold, str)
        assert 'att_media_shot0' in cold
        assert 'inspect_image' in cold
        assert 're-call tool' not in cold
        assert 'Text was: Page preview' in cold
        # Hot tail untouched.
        assert isinstance(msgs[3]['content'], list)
        assert isinstance(msgs[5]['content'], list)

    def test_no_durable_ref_keeps_recall_fallback(self):
        msgs = []
        for i, char in enumerate('ABC'):
            msgs.append({'role': 'assistant', 'tool_calls': [
                {'id': f't{i}', 'type': 'function',
                 'function': {'name': 'browser_preview_page', 'arguments': '{}'}}]})
            msgs.append(_image_msg(f't{i}', char))
        assert self._run(msgs) > 0
        assert 're-call tool if image needed' in msgs[1]['content']


# ═══════════════════════════════════════════════════════════════════════
#  compact_tool_results — multimodal image branch
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestCompactToolResultsRecoveryRef:

    def test_image_branch_placeholder_names_att_media_ref(self):
        import lib.tasks_pkg.compaction._constants as cc
        from lib.tasks_pkg.compaction._steps import (
            CompactionContext, make_constants, run_steps,
        )
        import lib.tasks_pkg.compaction._builtin_steps  # noqa: F401
        msgs = [
            {'role': 'assistant', 'tool_calls': [
                {'id': 'cold', 'type': 'function',
                 'function': {'name': 'browser_preview_page', 'arguments': '{}'}}]},
            _image_msg('cold', 'A', sources=['tofu-media:shot7']),
            # A protected newest batch so the image result is cold.
            {'role': 'assistant', 'tool_calls': [
                {'id': 'hot', 'type': 'function',
                 'function': {'name': 'grep_search', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'hot', 'name': 'grep_search',
             'content': 'short'},
        ]
        ctx = CompactionContext(
            messages=msgs, conv_id='sim', task=None,
            constants=make_constants(cc, {'MICRO_HOT_TAIL': 1}),
            cache_prefix_count=0, ignore_cache_prefix=False,
            stamp_fn=lambda *a, **k: None)
        assert run_steps(['compact_tool_results'], ctx) > 0
        cold = msgs[1]['content']
        assert isinstance(cold, str)
        assert 'att_media_shot7' in cold
        assert 're-call tool' not in cold
        # The protected newest batch is untouched.
        assert msgs[3]['content'] == 'short'


# ═══════════════════════════════════════════════════════════════════════
#  _resolve_media_ref — image documents resolve to bytes
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestResolveMediaRefImage:

    @pytest.fixture()
    def image_doc(self, tmp_path, monkeypatch):
        path = tmp_path / 'mcp-image-shot.jpg'
        Image.new('RGB', (400, 300), (10, 200, 90)).save(path)
        import lib.knowledge
        monkeypatch.setattr(
            lib.knowledge, 'get_document_metadata',
            lambda doc_id, *, user_id: {
                'id': doc_id, 'name': 'mcp-image-shot.jpg',
                'media_metadata': {'media_kind': 'document',
                                   'mime_type': 'image/jpeg'}})
        monkeypatch.setattr(
            lib.knowledge, 'read_source_path',
            lambda doc_id, *, user_id: path)
        return path

    def test_att_media_image_resolves_to_bytes(self, image_doc):
        from lib.attachments import resolve_attachment
        out = resolve_attachment('att_media_shot0001', user_id=1)
        assert out and out['kind'] == 'image'
        assert out['mime_type'] == 'image/jpeg'
        assert out['raw'] == image_doc.read_bytes()
        assert base64.b64decode(out['image_b64']) == out['raw']

    def test_inspect_image_on_att_media_ref(self, image_doc):
        from lib.project_mod import tool_inspect_image
        out = tool_inspect_image('/proj/base', 'att_media_shot0001',
                                 user_id=1, crop=[0.0, 0.0, 0.5, 1.0])
        assert isinstance(out, dict) and out.get('__screenshot__') is True
        assert out['sourceSize'] == [400, 300]

    def test_owner_required(self, image_doc):
        from lib.attachments import resolve_attachment
        assert resolve_attachment('att_media_shot0001') is None

    def test_text_document_still_text(self, monkeypatch):
        import lib.knowledge
        monkeypatch.setattr(
            lib.knowledge, 'get_document_metadata',
            lambda doc_id, *, user_id: {
                'id': doc_id, 'name': 'report.pdf',
                'media_metadata': {'media_kind': 'document',
                                   'mime_type': 'application/pdf'}})
        monkeypatch.setattr(
            lib.knowledge, 'read_source_path',
            lambda doc_id, *, user_id: __import__('pathlib').Path('/x.pdf'))
        monkeypatch.setattr(
            lib.knowledge, 'get_document_content',
            lambda doc_id, *, user_id, offset, limit: {
                'chunks': [{'content': 'pdf body'}]})
        from lib.attachments import resolve_attachment
        out = resolve_attachment('att_media_pdf00001', user_id=1)
        assert out and out['kind'] == 'text' and out['text'] == 'pdf body'


# ═══════════════════════════════════════════════════════════════════════
#  Text-lane guards — a __screenshot__ dict is never JSON-dumped
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestScreenshotTextLaneGuards:

    _SHOT = {
        '__screenshot__': True,
        'dataUrl': 'data:image/jpeg;base64,' + 'QUJD' * 4000,
        'format': 'jpeg',
        '_text_fallback': 'Page preview — index.html\nConsole messages: 0',
        '_modelReplaySources': ['tofu-media:env9'],
    }

    def test_budget_preserves_screenshot_until_text_boundary(self):
        from copy import deepcopy
        from lib.tasks_pkg.compaction.api import budget_tool_result_v2
        from lib.tools.result_value import capture_result
        from lib.tools.programmatic import encode_programmatic_output

        shot = deepcopy(self._SHOT)
        before = deepcopy(shot)
        out = budget_tool_result_v2('browser_preview_page', shot,
                                    user_id=0, model='test-model')
        assert out is shot
        assert shot == before
        captured = capture_result(out)
        assert captured.text() == shot['_text_fallback']
        assert any(part.kind == 'media' and part.reference == 'tofu-media:env9'
                   for part in captured.parts)
        encoded, _, truncated = encode_programmatic_output(out)
        assert json.loads(encoded)['content'] == shot['_text_fallback']
        assert not truncated
        # No text outlet may serialize encoded image bytes; the native carrier
        # and the canonical media identity survive projection unchanged.
        for text in (captured.text(), json.dumps(captured.manifest()), encoded):
            assert 'data:image/jpeg;base64' not in text
            assert 'QUJDQUJD' not in text
        assert shot == before

    def test_programmatic_encode_degrades_screenshot_dict(self):
        from lib.tools.programmatic import encode_programmatic_output
        text, _consumed, _trunc = encode_programmatic_output(dict(self._SHOT))
        payload = json.loads(text)
        assert payload['content'].startswith('Page preview — index.html')
        assert 'base64' not in payload['content']

    def test_programmatic_capture_degrades_batch_images(self):
        from lib.tools.programmatic import ProgrammaticResultBudget
        budget = ProgrammaticResultBudget(max_bytes=100_000)
        batch = budget.begin_batch(['c1'])
        batch.capture('c1', {
            '__batch_images__': {0: dict(self._SHOT)},
            '_text_content': '[1] shot.png — image loaded',
        })
        batch.finish()
        out = batch.result('c1')
        assert out['content'] == '[1] shot.png — image loaded'

    def test_programmatic_plain_dict_unchanged(self):
        from lib.tools.programmatic import _text_lane_content
        plain = {'status': 'ok', 'rows': [1, 2]}
        assert _text_lane_content(plain) is plain
        assert _text_lane_content('s') == 's'
