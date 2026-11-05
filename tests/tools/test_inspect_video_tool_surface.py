"""inspect_video tool surface: one declaration shared by the live registry and
the declared spec list, read-only/parallel-safe classification, eager wire
exposure (results name it) that Tool Search still ranks by video and audio
intents, and the first-party
dispatch, display and presentation wiring shared with inspect_image.
"""
from __future__ import annotations

import dataclasses

import pytest

pytestmark = pytest.mark.unit


def _spec(specs):
    return next(spec for spec in specs if spec.key == 'inspect_video')


def _context(**overrides):
    from lib.tools.registry import ToolContext
    fields = dict(cfg={'memoryEnabled': False, 'mcpEnabled': False},
                  task_id='inspect-video-surface', project_path='',
                  project_enabled=False, search_mode='off', search_enabled=False,
                  fetch_enabled=False, code_exec_enabled=False,
                  browser_enabled=False, desktop_enabled=False,
                  enabled_plugins=set(), owner_user_id=7,
                  conv_id='inspect-video-surface',
                  messages=[{'role': 'user', 'content': 'What happens in this clip?'}])
    fields.update(overrides)
    return ToolContext(**fields)


def test_live_registry_and_declared_spec_lists_carry_identical_inspect_video_specs():
    from lib.tools.registry import all_specs
    from lib.tools.registry._build import build_builtin_specs as live_seed
    from lib.tools.registry._builtin_specs import build_builtin_specs

    live = _spec(all_specs())
    for declared in (_spec(build_builtin_specs()), _spec(live_seed())):
        for field in dataclasses.fields(live):
            assert getattr(declared, field.name) == getattr(live, field.name), field.name


def test_inspect_video_is_read_only_parallel_safe_and_needs_no_approval():
    from lib.tasks_pkg.tool_dispatch._flags import _registry_tool_flags
    from lib.tools.registry import all_specs

    spec = _spec(all_specs())
    assert spec.provides == frozenset({'inspect_video'})
    assert spec.write_tools == frozenset()
    assert spec.confirmation_tools == frozenset()
    assert spec.idempotent_tools == frozenset({'inspect_video'})
    # A staged path can change outside this task's write fence; decode fresh.
    assert spec.cacheable_tools == frozenset()
    assert spec.phase == 'base' and spec.exposure_gate is None
    writes, cacheable = _registry_tool_flags()
    assert 'inspect_video' not in writes
    assert 'inspect_video' not in cacheable


def test_inspect_video_schema_contract():
    from lib.tools.video_inspect import INSPECT_VIDEO_TOOL

    function = INSPECT_VIDEO_TOOL['function']
    parameters = function['parameters']
    assert function['name'] == 'inspect_video'
    assert parameters['type'] == 'object'
    assert parameters['required'] == ['path']
    assert set(parameters['properties']) == {'path', 'start', 'end'}
    assert parameters['properties']['start']['type'] == 'number'
    assert parameters['properties']['end']['type'] == 'number'
    description = function['description'].lower()
    assert 'read-only' in description
    assert 'frames' in description and 'transcript' in description
    assert 'audio' in description
    path = parameters['properties']['path']['description']
    assert 'att_media_' in path and 'browser_download_url_to_server' in path


@pytest.mark.parametrize('exposure', ['routed', 'full'])
@pytest.mark.parametrize('query', [
    'watch a video file and tell me what happens in it',
    'analyze this mp4 video',
    'understand a downloaded video: extract frames and a transcript',
    'extract key frames from a video',
    '看看这个视频讲了什么',
    'listen to this mp3 and tell me what is said',
    'transcribe audio from a downloaded podcast episode',
    'summarize the podcast recording',
    '音频 转写',
])
def test_inspect_video_is_eager_and_ranked_by_video_intents(exposure, query):
    from lib.tools.gateway import local_wire_tools, search_executable_catalog
    from lib.tools.registry import assemble_tool_list

    ctx = _context(cfg={'memoryEnabled': False, 'mcpEnabled': False,
                        'tools.nativeExposure': exposure})
    surface, _ = assemble_tool_list(ctx)

    assert 'inspect_video' in {
        item['function']['name'] for item in ctx.executable_tool_catalog}
    # fetch_url's staged video/audio note and the page-media footer name it,
    # and a pointer-named tool stays on the wire (no dead-end pointer).
    assert ctx.discovery_policy_by_name.get('inspect_video') != 'searchable'
    projected = local_wire_tools(
        surface, discovery_policy_by_name=ctx.discovery_policy_by_name)
    assert 'inspect_video' in {
        item['function']['name'] for item in projected}
    result = search_executable_catalog(
        ctx.executable_tool_catalog, query, limit=3,
        namespace_by_name=ctx.tool_namespace_by_name,
        search_text_by_name=ctx.search_text_by_name,
        contract_documents_by_name=ctx.tool_contract_documents_by_name)
    assert result['items'][0]['name'] == 'inspect_video', result['items']
    assert result['items'][0]['permission'] == 'read'


def test_first_party_dispatch_executor_and_display_wiring():
    import lib.tasks_pkg.handlers.project  # noqa: F401 - decorator registration
    from lib.project_mod import tool_inspect_video  # noqa: F401 - public facade
    from lib.project_mod.tools import _EXEC_HANDLERS
    from lib.tasks_pkg.executor import tool_registry
    from lib.tasks_pkg.tool_display import _TOOL_DISPLAY_DISPATCH

    assert tool_registry.lookup('inspect_video') is tool_registry.lookup('inspect_image')
    assert 'inspect_video' in _EXEC_HANDLERS
    display, extra = _TOOL_DISPLAY_DISPATCH['inspect_video'](
        'inspect_video',
        {'path': 'data/fetched/server-download-1.mp4', 'start': 75, 'end': 90},
        'tc-1', '')
    assert 'server-download-1.mp4' in display and '01:15–01:30' in display
    assert extra['toolName'] == 'inspect_video'


def test_presentation_renders_every_frame_and_keeps_text_for_text_only_models():
    from lib.tasks_pkg.handlers._project_presentation import (
        prepare_project_image_result,
    )

    def frame(index):
        return {'__screenshot__': True, 'format': 'jpeg',
                'dataUrl': f'data:image/jpeg;base64,{index}',
                'originalSize': 10, 'compressedSize': 10,
                'compressionApplied': False,
                'filename': f'clip.mp4 @ 00:0{index}'}

    batch = {
        '__batch_images__': {0: frame(0), 1: frame(1), 2: frame(2)},
        '_text_content': 'inspect_video: clip.mp4\nProbe: duration 3.00s',
        '_no_vision_fallback': 'inspect_video: clip.mp4 [frames not shown]',
        'videoInspect': {'name': 'clip.mp4', 'durationS': 3.0,
                         'width': 1920, 'height': 1080, 'frames': 3,
                         'extractedFrames': 3, 'window': [0.0, 3.0],
                         'transcript': 'unavailable'},
    }
    content, meta = prepare_project_image_result(
        'inspect_video', {'path': 'media/clip.mp4'}, batch,
        svg_renders=[], response_assets=None)

    assert len(content['images']) == 3
    assert content['_text_fallback'].startswith('inspect_video: clip.mp4')
    assert content['_no_vision_fallback'] == batch['_no_vision_fallback']
    assert [item['filename'] for item in meta['imageDataUris']] == [
        'clip.mp4 @ 00:00', 'clip.mp4 @ 00:01', 'clip.mp4 @ 00:02']
    assert meta['badge'] == '3 frames'
    assert meta['snippet'] == ('clip.mp4 · 3.00s · 1920x1080 · 3 frames · '
                               'transcript unavailable')


def test_typed_error_result_metadata_is_marked_failed():
    from lib.tools.meta import build_project_tool_meta
    from lib.tools.result_envelope import typed_tool_error

    content = typed_tool_error(
        'video_file_not_found', retryable=False,
        next_action='Pass an existing video path.',
        message='Video file not found: /x.mp4').to_model_text()
    meta = build_project_tool_meta('inspect_video', {'path': '/x.mp4'}, content)

    assert meta['type'] == 'error'
    assert meta['badge'] == 'video_file_not_found'
    assert 'not found' in meta['snippet']
