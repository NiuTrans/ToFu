"""fetch_url must carry reader-routed MCP images all the way to the model.

Regression: the 学城 doc reader returns an MCPToolResult (text + out-of-band
ImageContent), but fetch_url's content filter returns a fresh plain str, so
the image attribute was dropped before the model ever saw it — document
images silently vanished from every KM doc read through fetch_url.

Pins:
  1. ``_fetch_url_one`` captures ``image_contents`` before filtering and
     returns them on the item (cleared on an IRRELEVANT verdict).
  2. ``_handle_fetch_url`` re-attaches them to the tool content.
  3. The dispatch pipeline projects them as multimodal tool-message blocks
     (tofu-media refs) for vision models and keeps plain text for
     text-only models.
"""

from __future__ import annotations

import contextlib
import threading
from types import SimpleNamespace

import pytest

from lib.mcp.result_content import MCPToolResult

pytestmark = pytest.mark.unit

_DOC_URL = 'https://km.internal.example.com/collabpage/123456'
_IMAGE = {'data': 'aW1n', 'mimeType': 'image/png'}


def _reader_result(text='doc body'):
    return MCPToolResult(text, image_contents=(dict(_IMAGE),))


def _fetch_item(**overrides):
    item = {
        'url': _DOC_URL, 'page_content': 'doc body', 'is_pdf': False,
        'raw_chars': 8, 'filtered_chars': 8, 'error_msg': None,
        'saved_path': None, 'is_asset': False, 'reason': 'extracted_ok',
        'location': None, 'transport': None, 'sha256': None,
        'image_contents': (dict(_IMAGE),),
    }
    item.update(overrides)
    return item


def test_fetch_url_one_preserves_reader_images_through_filter(monkeypatch):
    from lib.tasks_pkg.handlers.search import _core

    monkeypatch.setattr(
        _core, 'fetch_page_content', lambda *a, **k: _reader_result())
    # The filter returns a FRESH plain str — the exact slice that used to
    # strip the attribute.
    monkeypatch.setattr(
        _core, 'filter_web_content',
        lambda content, **k: str(content) + ' [filtered]')

    item = _core._fetch_url_one(_DOC_URL, 'q')

    assert item['reason'] == 'extracted_ok'
    assert item['page_content'].endswith('[filtered]')
    assert item['image_contents'] == (_IMAGE,)


def test_fetch_url_one_drops_images_with_irrelevant_content(monkeypatch):
    from lib.tasks_pkg.handlers.search import _core
    from tofu_search.fetch.content_filter import IRRELEVANT_SENTINEL

    monkeypatch.setattr(
        _core, 'fetch_page_content', lambda *a, **k: _reader_result())
    monkeypatch.setattr(
        _core, 'filter_web_content', lambda *a, **k: IRRELEVANT_SENTINEL)

    item = _core._fetch_url_one(_DOC_URL, 'q')

    assert item['reason'] == 'irrelevant'
    assert item['page_content'] is None
    assert item['image_contents'] == ()


def test_handle_fetch_url_reattaches_reader_images(monkeypatch):
    from lib.tasks_pkg.handlers.search import _handlers

    item = _fetch_item()
    monkeypatch.setattr(
        _handlers, '_load_search_backend',
        lambda: (SimpleNamespace(_fetch_url_one=lambda *a, **k: item), None))
    monkeypatch.setattr(
        'lib.search_bridge.bind_search_browser',
        lambda **k: contextlib.nullcontext())
    monkeypatch.setattr(_handlers, '_finalize_tool_round', lambda *a, **k: None)

    task = {'lastUserQuery': 'q'}
    tc_id, content, is_search = _handlers._handle_fetch_url(
        task, {}, 'fetch_url', 'call-1', {'url': _DOC_URL}, 0, {}, {}, None,
        False)

    assert tc_id == 'call-1' and is_search is True
    assert isinstance(content, MCPToolResult)
    assert 'doc body' in content
    assert content.image_contents == (_IMAGE,)


def test_handle_fetch_url_batch_concatenates_reader_images(monkeypatch):
    from lib.tasks_pkg.handlers.search import _handlers

    items = {
        'https://a.example/1': _fetch_item(url='https://a.example/1'),
        'https://b.example/2': _fetch_item(
            url='https://b.example/2',
            image_contents=({'data': 'aW1nMg', 'mimeType': 'image/png'},)),
    }
    monkeypatch.setattr(
        _handlers, '_load_search_backend',
        lambda: (SimpleNamespace(
            _fetch_url_one=lambda url, *a, **k: items[url]), None))
    monkeypatch.setattr(
        'lib.search_bridge.bind_search_browser',
        lambda **k: contextlib.nullcontext())
    monkeypatch.setattr(_handlers, '_finalize_tool_round', lambda *a, **k: None)

    task = {'lastUserQuery': 'q', 'aborted': False}
    tc_id, content, is_search = _handlers._handle_fetch_url_batch(
        task, {}, 'fetch_url', 'call-b',
        {'urls': [{'url': u} for u in items]},
        [{'url': u} for u in items], 0, {}, {}, None, False)

    assert tc_id == 'call-b'
    assert isinstance(content, MCPToolResult)
    assert content.image_contents == (
        _IMAGE, {'data': 'aW1nMg', 'mimeType': 'image/png'})


def _fake_capture(task, value, **kwargs):
    refs = []
    for ordinal, payload in enumerate(value.image_contents):
        ref = {
            'attachmentId': f'att-{ordinal}',
            'mimeType': payload['mimeType'],
            'sourceTool': kwargs['source_tool'],
            'toolCallId': kwargs['tool_call_id'],
            'sourceOrdinal': ordinal,
        }
        task.setdefault(kwargs['reference_field'], []).append(ref)
        refs.append(ref)
    return len(refs)


def test_dispatch_settle_freezes_mcp_image_replay(monkeypatch):
    from lib.tasks_pkg.tool_dispatch import _pipeline

    monkeypatch.setattr(
        'lib.tasks_pkg.mcp_result_media.capture_mcp_result_images',
        _fake_capture)
    monkeypatch.setattr(_pipeline, 'model_supports_vision', lambda m: True)

    content = _reader_result('doc text')
    task = {'_userId': 1}
    row = {}
    out = _pipeline._dispatch_settle_content(
        task, row, content, 'vision-model', 'fetch_url', 'call-1')

    assert out is content  # text settles unchanged; images ride the blocks
    blocks = row['toolModelContent']
    assert blocks[0] == {'type': 'text', 'text': 'doc text'}
    assert blocks[1] == {'type': 'image_url',
                         'image_url': {'url': 'tofu-media:att-0'}}
    assert 'aW1n' not in repr(blocks) or 'tofu-media' in repr(blocks)

    # Existing refs (adapter-captured direct MCP lane) are reused, not
    # re-ingested.
    row_again = {}
    task2 = {'_userId': 1, '_mcpImages': [{
        'attachmentId': 'att-existing', 'mimeType': 'image/png',
        'sourceTool': 'fetch_url', 'toolCallId': 'call-9',
        'sourceOrdinal': 0}]}
    _pipeline._dispatch_settle_content(
        task2, row_again, content, 'vision-model', 'fetch_url', 'call-9')
    assert row_again['toolModelContent'][1]['image_url']['url'] == (
        'tofu-media:att-existing')
    assert '_toolModelImages' not in task2

    # Text-only model: no freeze, no refs — plain text settles.
    monkeypatch.setattr(_pipeline, 'model_supports_vision', lambda m: False)
    row_text = {}
    out_text = _pipeline._dispatch_settle_content(
        {'_userId': 1}, row_text, content, 'text-model', 'fetch_url', 'c-2')
    assert out_text is content
    assert 'toolModelContent' not in row_text


def test_pipeline_projects_reader_images_to_vision_model(monkeypatch):
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    from lib.tasks_pkg.tool_display import _build_tool_round_entry
    import lib.tasks_pkg.tool_dispatch._heartbeat as _heartbeat
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline

    content = _reader_result(
        f'Content from {_DOC_URL} (8 chars):\n\ndoc body')

    def fake_execute(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                     cfg, project_path, project_enabled, all_tools=None):
        return tc_id, content, False

    monkeypatch.setattr(_pipeline, '_execute_tool_one', fake_execute)
    monkeypatch.setattr(_heartbeat, '_execute_tool_one', fake_execute)
    monkeypatch.setattr(_pipeline, 'model_supports_vision', lambda m: True)
    monkeypatch.setattr(
        'lib.tasks_pkg.mcp_result_media.capture_mcp_result_images',
        _fake_capture)

    task = {
        'id': 'fetch-task', 'convId': 'convfetch1', 'status': 'running',
        '_userId': 1,
        'aborted': False, 'model': 'vision-model', 'events': [],
        'config': {'tools': {'resultEnvelope': 'legacy'}},
        'events_lock': threading.Lock(), '_attended': False,
        '_dispatch_heartbeat': 0.0, '_t_last_event': 0.0,
        'toolRounds': [],
    }
    args = {'url': _DOC_URL}
    _, row, _ = _build_tool_round_entry(
        'fetch_url', args, 'call-1', '{"url":"%s"}' % _DOC_URL, 1, False)
    task['toolRounds'].append(row)
    tc = {'id': 'call-1', 'type': 'function',
          'function': {'name': 'fetch_url',
                       'arguments': '{"url":"%s"}' % _DOC_URL}}

    messages = []
    execute_tool_pipeline(
        task, [(tc, 'fetch_url', 'call-1', args, row['roundNum'], row, None)],
        cfg={'autoApply': True}, project_path=None, project_enabled=False,
        tool_list=[], messages=messages, all_search_results_text=[],
        round_num=0, model='vision-model')

    msg = messages[-1]
    assert msg['role'] == 'tool' and msg['tool_call_id'] == 'call-1'
    blocks = msg['content']
    assert isinstance(blocks, list)
    assert blocks[0]['type'] == 'text' and 'doc body' in blocks[0]['text']
    assert blocks[1] == {'type': 'image_url',
                         'image_url': {'url': 'tofu-media:att-0'}}
    # The frozen replay blocks match the live message (no base64 retained).
    assert row['toolModelContent'] == blocks
    assert 'aW1n' not in repr(row['toolModelContent'])


def test_pipeline_keeps_plain_text_for_text_only_model(monkeypatch):
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    from lib.tasks_pkg.tool_display import _build_tool_round_entry
    import lib.tasks_pkg.tool_dispatch._heartbeat as _heartbeat
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline

    content = _reader_result('doc body')

    def fake_execute(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                     cfg, project_path, project_enabled, all_tools=None):
        return tc_id, content, False

    monkeypatch.setattr(_pipeline, '_execute_tool_one', fake_execute)
    monkeypatch.setattr(_heartbeat, '_execute_tool_one', fake_execute)
    monkeypatch.setattr(_pipeline, 'model_supports_vision', lambda m: False)
    monkeypatch.setattr(
        'lib.tasks_pkg.mcp_result_media.capture_mcp_result_images',
        _fake_capture)

    task = {
        'id': 'fetch-task', 'convId': 'convfetch2', 'status': 'running',
        '_userId': 1,
        'aborted': False, 'model': 'text-model', 'events': [],
        'config': {'tools': {'resultEnvelope': 'legacy'}},
        'events_lock': threading.Lock(), '_attended': False,
        '_dispatch_heartbeat': 0.0, '_t_last_event': 0.0,
        'toolRounds': [],
    }
    args = {'url': _DOC_URL}
    _, row, _ = _build_tool_round_entry(
        'fetch_url', args, 'call-2', '{"url":"%s"}' % _DOC_URL, 1, False)
    task['toolRounds'].append(row)
    tc = {'id': 'call-2', 'type': 'function',
          'function': {'name': 'fetch_url',
                       'arguments': '{"url":"%s"}' % _DOC_URL}}

    messages = []
    execute_tool_pipeline(
        task, [(tc, 'fetch_url', 'call-2', args, row['roundNum'], row, None)],
        cfg={'autoApply': True}, project_path=None, project_enabled=False,
        tool_list=[], messages=messages, all_search_results_text=[],
        round_num=0, model='text-model')

    assert isinstance(messages[-1]['content'], str)
    assert 'doc body' in messages[-1]['content']
    assert 'toolModelContent' not in row
    assert not task.get('_toolModelImages')
