# Incident anchor: 2026-08-06 owner screenshot — motion_video_check /
# motion_video_render cards rendered as a bare fn-name + an ok/rendered badge,
# no label, no result body. The tools were never registered in the display
# layer, so every call fell through to _tool_display_generic (which also
# logged a spurious WARNING per call) and the frontend's generic line.
"""tests/test_motion_video_display.py — motion-video / produce tool-card contract.

Two layers, one contract (mirrors tests/test_browser_display_labels.py):

  * Backend labels (lib/tasks_pkg/tool_display/_renderers.py): every shipped
    motion_video_* / produce_* tool rendered with {} args and with typical
    arg shapes — never the raw fn_name, never '?', never a dangling colon.
  * Frontend rich-card dispatch remains connected to the typed presentation
    owner. Family/catalogue parity is exercised through that owner's public
    API in ``test_frontend_tool_round_presentation.py``.

The structured result card (_renderMotionVideoBlock in the same ESM owner)
is exercised under jsdom by tests/test_frontend_motion_tool_render.py.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent
TOOL_ROUNDS = ROOT / 'frontend' / 'src' / 'runtime' / 'app-runtime.js'


def _backend_family():
    from lib.tools.motion_video import MOTION_VIDEO_TOOL_NAMES
    from lib.tools.produce import PRODUCE_TOOL_NAMES
    return set(MOTION_VIDEO_TOOL_NAMES) | set(PRODUCE_TOOL_NAMES)


# ── 1. Every shipped tool is registered in the display dispatch ───────

def test_every_family_tool_is_in_the_dispatch_table():
    from lib.tasks_pkg.tool_display import _TOOL_DISPLAY_DISPATCH
    missing = _backend_family() - set(_TOOL_DISPLAY_DISPATCH)
    assert not missing, f'family tools with no display handler: {sorted(missing)}'


def test_update_search_settings_is_registered_too():
    # Registered in the same batch (it produced the identical bare-name card).
    from lib.tasks_pkg.tool_display import _TOOL_DISPLAY_DISPATCH
    assert 'update_search_settings' in _TOOL_DISPLAY_DISPATCH


# ── 2. The all-defaults call ({} args) renders a clean label ──────────

def test_empty_args_labels_are_clean_and_not_the_raw_name():
    from lib.tasks_pkg.tool_display import tool_round_label
    for name in sorted(_backend_family()):
        label = tool_round_label(name, {})
        assert label and label != name, f'{name}: empty-args label is {label!r}'
        assert '?' not in label, f'{name}: empty-args label contains ?: {label!r}'
        assert not label.endswith(':'), f'{name}: dangling colon: {label!r}'
        assert not label.endswith(': '), f'{name}: dangling colon: {label!r}'
        assert label.count('(') == label.count(')'), (
            f'{name}: unbalanced parens: {label!r}')


# ── 3. Typical arg shapes render their salient target ─────────────────

def test_render_label_names_scene_output_and_quality():
    from lib.tasks_pkg.tool_display import tool_round_label
    label = tool_round_label('motion_video_render', {
        'project_dir': '.tofu/motion_video/x/scenes/scene-001',
        'output': '.tofu/motion_video/x/scenes/scene-001/scene-001.mp4',
        'quality': 'draft',
    })
    assert 'scene-001' in label
    assert 'scene-001.mp4' in label
    assert 'draft' in label


def test_check_label_names_the_scene():
    from lib.tasks_pkg.tool_display import tool_round_label
    label = tool_round_label('motion_video_check', {'project_dir': 'scenes/scene-002'})
    assert 'scene-002' in label


def test_concat_label_counts_inputs_and_names_output():
    from lib.tasks_pkg.tool_display import tool_round_label
    label = tool_round_label('motion_video_concat', {
        'inputs': ['a.mp4', 'b.mp4', 'c.mp4'], 'output': 'final.mp4'})
    assert '3' in label and 'final.mp4' in label


def test_mux_label_names_the_deliverable():
    from lib.tasks_pkg.tool_display import tool_round_label
    label = tool_round_label('motion_video_mux', {
        'video': 'v.mp4', 'audio': 'a.wav', 'output': 'final.mp4'})
    assert 'final.mp4' in label


def test_env_check_no_install_variant_is_clean():
    from lib.tasks_pkg.tool_display import tool_round_label
    assert tool_round_label('motion_video_env_check', {}) != \
        tool_round_label('motion_video_env_check', {'install': False})


def test_storyboard_label_names_both_files():
    from lib.tasks_pkg.tool_display import tool_round_label
    label = tool_round_label('motion_video_storyboard_check', {
        'srt_path': 'x/t.srt', 'scenes_path': 'x/scenes.json'})
    assert 'scenes.json' in label and 't.srt' in label


def test_probe_label_names_the_file():
    from lib.tasks_pkg.tool_display import tool_round_label
    assert 'scene-001.mp4' in tool_round_label(
        'motion_video_probe', {'path': 'a/b/scene-001.mp4'})


def test_narrate_strict_alignment_is_surfaced():
    from lib.tasks_pkg.tool_display import tool_round_label
    label = tool_round_label('motion_video_narrate', {
        'scenes_path': 's.json', 'out_dir': 'audio', 'alignment': 'strict'})
    assert 'strict' in label and 'audio' in label


def test_produce_labels_surface_the_topic():
    from lib.tasks_pkg.tool_display import tool_round_label
    assert '为什么天空是蓝色的' in tool_round_label(
        'produce_video', {'topic': '为什么天空是蓝色的'})
    assert '(deep)' in tool_round_label(
        'produce_report', {'topic': 't', 'depth': 'deep'})
    assert 'KV cache' in tool_round_label(
        'produce_research', {'direction': 'KV cache 压缩'})


def test_update_search_settings_read_vs_write():
    from lib.tasks_pkg.tool_display import tool_round_label
    read = tool_round_label('update_search_settings', {})
    write = tool_round_label('update_search_settings',
                             {'fetch_top_n': 8, 'fetch_timeout': 30})
    assert read != write
    assert 'fetch_top_n' in write and 'fetch_timeout' in write


def test_frontend_card_probe_is_wired_in_render_pipeline():
    # The card probe in _renderUnifiedToolLine must reference the rich renderer
    # through a typeof guard (deferred-load contract) and the family predicate.
    src = TOOL_ROUNDS.read_text(encoding='utf-8')
    assert 'isMotionToolRound(round) && round.status === "done"' in src
    assert "typeof _renderMotionVideoBlock === 'function'" in src


def test_rich_renderer_is_shipping_in_the_deferred_module():
    rich = TOOL_ROUNDS.read_text(encoding='utf-8')
    assert 'function _renderMotionVideoBlock(round, svg, q, badgeHtml)' in rich
    # The retained upgrade sweep must include motion rounds.
    assert 'isMotionToolRound' in rich


# ── 5. Census: no shipped tool family may be missing from the display
#       dispatch — the guard that would have caught this incident class. ──

def test_every_shipped_tool_family_has_a_display_handler():
    """Every name any built-in ToolSpec can put in front of the model must
    resolve to a non-generic display handler — otherwise it renders as the
    bare fn-name line this incident shipped."""
    from lib.tasks_pkg.tool_display import _TOOL_DISPLAY_DISPATCH, _tool_display_generic
    from lib.tools.registry._build import _register_builtins  # noqa: F401  (side-effect: populates registry at import)
    from lib.tools.registry._spec import _TOOL_SPECS

    missing = {}
    for spec in _TOOL_SPECS:
        # Only BUILTIN specs belong to this repo's display contract — plugins
        # (e.g. a private plugin's read-only lookup tool) register via the
        # entry-point channel and own their own rendering.
        if getattr(spec, 'source', 'builtin') != 'builtin':
            continue
        for tool_name in (spec.provides or ()):
            handler = _TOOL_DISPLAY_DISPATCH.get(tool_name)
            if handler is None or handler is _tool_display_generic:
                missing.setdefault(spec.key, []).append(tool_name)
    assert not missing, (
        'shipped tools falling through to the generic bare-name card: '
        + '; '.join(f'{k}: {sorted(v)}' for k, v in sorted(missing.items())))


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
