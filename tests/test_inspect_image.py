"""Tests for the ``inspect_image`` tool — multi-resolution image viewer.

``inspect_image`` re-renders a region of a local image FROM THE ORIGINAL
FILE at full resolution (crop → rotate → zoom → fit), so the model can read
detail the initial read-time downscale discarded. It rides the existing
``__screenshot__`` protocol, so the result must:

  - be a ``__screenshot__`` dict (never raw base64 text in the stream),
  - keep the inspected view within the per-view pixel budget,
  - apply crop / rotate / zoom correctly,
  - resolve through ``execute_tool`` and the dispatch handler.

Run:  pytest tests/test_inspect_image.py -v
"""
from __future__ import annotations

import base64
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PIL = pytest.importorskip('PIL')
from PIL import Image  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────

def _write_image(path, *, w, h, mode='RGB', color=(128, 64, 32)):
    """Write a real decodable image of exact WxH to *path*."""
    img = Image.new(mode, (w, h), color)
    img.save(path)


def _decode_view(result):
    """Pull (PIL.Image, raw_bytes) out of a __screenshot__ result dict."""
    assert isinstance(result, dict), f'expected dict, got {type(result).__name__}: {result!r}'
    assert result.get('__screenshot__') is True
    du = result['dataUrl']
    assert du.startswith('data:')
    b64 = du.split(',', 1)[1]
    raw = base64.b64decode(b64)
    return Image.open(io.BytesIO(raw)), raw


# ═══════════════════════════════════════════════════════════════════════
#  Core transform behaviour
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestInspectImageFile:

    def test_full_frame_returns_screenshot_dict(self, tmp_path):
        p = tmp_path / 'a.png'
        _write_image(str(p), w=200, h=120)
        from lib.file_reader import inspect_image_file
        out = inspect_image_file(str(p))
        img, raw = _decode_view(out)
        assert img.size == (200, 120)
        assert out['sourceSize'] == [200, 120]
        assert out['viewSize'] == [200, 120]
        # base64 lives in dataUrl, never in any text field
        assert 'base64' not in out.get('_text_fallback', '')

    def test_fractional_crop(self, tmp_path):
        p = tmp_path / 'b.png'
        _write_image(str(p), w=1000, h=800)
        from lib.file_reader import inspect_image_file
        # top-right quadrant
        out = inspect_image_file(str(p), crop=[0.5, 0.0, 1.0, 0.5])
        img, _ = _decode_view(out)
        assert img.size == (500, 400)

    def test_pixel_crop(self, tmp_path):
        p = tmp_path / 'c.png'
        _write_image(str(p), w=1000, h=800)
        from lib.file_reader import inspect_image_file
        out = inspect_image_file(str(p), crop=[100, 100, 300, 250])
        img, _ = _decode_view(out)
        assert img.size == (200, 150)

    def test_zoom_center_crop(self, tmp_path):
        p = tmp_path / 'd.png'
        _write_image(str(p), w=400, h=400)
        from lib.file_reader import inspect_image_file
        out = inspect_image_file(str(p), zoom=2.0)
        img, _ = _decode_view(out)
        assert img.size == (200, 200)

    def test_rotate_swaps_dimensions(self, tmp_path):
        p = tmp_path / 'e.png'
        _write_image(str(p), w=300, h=100)
        from lib.file_reader import inspect_image_file
        out = inspect_image_file(str(p), rotate=90)
        img, _ = _decode_view(out)
        assert img.size == (100, 300)

    def test_crop_then_fit_to_budget(self, tmp_path):
        # A crop bigger than the per-view budget must be downscaled to fit,
        # but it's still rendered from the ORIGINAL pixels (full detail).
        from lib.file_reader import _INSPECT_MAX_PX, inspect_image_file
        p = tmp_path / 'big.png'
        _write_image(str(p), w=_INSPECT_MAX_PX * 2, h=_INSPECT_MAX_PX)
        out = inspect_image_file(str(p))
        img, _ = _decode_view(out)
        assert max(img.size) <= _INSPECT_MAX_PX
        assert out['sourceSize'][0] == _INSPECT_MAX_PX * 2

    def test_grid_overlay_is_png(self, tmp_path):
        p = tmp_path / 'g.jpg'
        _write_image(str(p), w=300, h=300)
        from lib.file_reader import inspect_image_file
        out = inspect_image_file(str(p), grid=True)
        assert out['format'] == 'png'
        assert 'grid' in out['inspectOps']

    # ── error paths (never raise; return Error: strings) ──

    def test_missing_file(self, tmp_path):
        from lib.file_reader import inspect_image_file
        out = inspect_image_file(str(tmp_path / 'nope.png'))
        assert isinstance(out, str) and out.startswith('Error:')

    def test_non_image_rejected(self, tmp_path):
        p = tmp_path / 'x.txt'
        p.write_text('hello')
        from lib.file_reader import inspect_image_file
        out = inspect_image_file(str(p))
        assert isinstance(out, str) and out.startswith('Error:')

    def test_bad_rotate(self, tmp_path):
        p = tmp_path / 'r.png'
        _write_image(str(p), w=50, h=50)
        from lib.file_reader import inspect_image_file
        out = inspect_image_file(str(p), rotate=45)
        assert isinstance(out, str) and out.startswith('Error:')

    def test_bad_crop_shape(self, tmp_path):
        p = tmp_path / 's.png'
        _write_image(str(p), w=50, h=50)
        from lib.file_reader import inspect_image_file
        out = inspect_image_file(str(p), crop=[0, 0, 1])
        assert isinstance(out, str) and out.startswith('Error:')


# ═══════════════════════════════════════════════════════════════════════
#  Dispatch wiring — execute_tool + tool_inspect_image
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestInspectImageDispatch:

    def test_execute_tool_relative_path(self, tmp_path):
        _write_image(str(tmp_path / 'rel.png'), w=400, h=200)
        from lib.project_mod import execute_tool
        out = execute_tool('inspect_image', {'path': 'rel.png', 'crop': [0, 0, 0.5, 1]},
                           str(tmp_path), conv_id='c1', task_id='t1')
        img, _ = _decode_view(out)
        assert img.size == (200, 200)

    def test_execute_tool_missing_path_arg(self, tmp_path):
        from lib.project_mod import execute_tool
        out = execute_tool('inspect_image', {}, str(tmp_path), conv_id='c1', task_id='t1')
        assert isinstance(out, str) and out.startswith('Error:')

    def test_tool_inspect_image_absolute_path(self, tmp_path):
        p = tmp_path / 'abs.png'
        _write_image(str(p), w=100, h=100)
        from lib.project_mod import tool_inspect_image
        out = tool_inspect_image(str(tmp_path), str(p), zoom=2)
        img, _ = _decode_view(out)
        assert img.size == (50, 50)


# ═══════════════════════════════════════════════════════════════════════
#  Registration — tool schema, dispatch, display, tool list
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestInspectImageRegistration:

    def test_schema_exported(self):
        from lib.tools import INSPECT_IMAGE_TOOL, IMAGE_EDIT_TOOL_NAMES
        assert INSPECT_IMAGE_TOOL['function']['name'] == 'inspect_image'
        assert 'inspect_image' in IMAGE_EDIT_TOOL_NAMES

    def test_in_dispatch_registry(self):
        import lib.tasks_pkg.handlers.project  # noqa: F401 — side-effect registration
        from lib.tasks_pkg.executor import tool_registry
        names = [n for n, _c, _d in tool_registry.list_tools()]
        assert 'inspect_image' in names

    def test_always_on_in_tool_list(self):
        # No project, no toggles — inspect_image must still be offered, just
        # like read_files (both classify by file type at call time).
        from lib.tasks_pkg.model_config import _assemble_tool_list
        tl, _has, _mx = _assemble_tool_list(
            {'messages': []}, None, False, 't-il', 'off', False, True,
            False, False, False, False)
        names = [t['function']['name'] for t in tl]
        assert 'inspect_image' in names
        assert 'read_files' in names

    def test_display_handler(self):
        from lib.tasks_pkg.tool_display import _TOOL_DISPLAY_DISPATCH
        handler = _TOOL_DISPLAY_DISPATCH.get('inspect_image')
        assert handler is not None
        display, extra = handler('inspect_image',
                                 {'path': 'd/diagram.png', 'crop': [0, 0, 1, 1], 'zoom': 2},
                                 'tc1', '')
        assert 'diagram.png' in display
        assert extra['toolName'] == 'inspect_image'
