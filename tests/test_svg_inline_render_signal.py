"""Regression tests for the SVG inline-render signal.

Feature (2026-07): when ``read_files`` reads a ``.svg`` file, its XML source
still enters the model stream **as text** (the model should see the markup),
but the read layer ALSO signals the source out-of-band so the frontend can
render it inline like an image (via ``meta.imageDataUris`` — the same field
PNG reads and browser screenshots use).

The signal uses a thread-local collector (mirroring
``write_tools._signal_root_added``): ``_signal_svg_render`` appends a
``{filename, format:'svg', uri:'data:image/svg+xml;base64,…'}`` descriptor,
and the project tool handler drains it via ``drain_svg_render_signals`` right
after the read (same thread).

These tests pin:
  (a) an ``.svg`` read returns the XML text AND emits exactly one drained
      signal with a valid ``data:image/svg+xml;base64,`` URI (relative + abs);
  (b) a non-SVG read emits nothing;
  (c) an oversized SVG (> ``_MAX_SVG_RENDER_BYTES``) is skipped;
  (d) the drain is one-shot (empties after read);
  (e) the dedup-cache-hit path reattaches the render URIs so a repeat read
      renders identically to the fresh read (not a bare text row);
  NEUTER — disabling ``_maybe_signal_svg`` makes the positive case fail,
      proving the signal is load-bearing.

Run:  pytest tests/test_svg_inline_render_signal.py -v
"""
from __future__ import annotations

import base64
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        '<rect width="10" height="10" fill="red"/></svg>')


@pytest.fixture(autouse=True)
def _drain_before_each():
    """Ensure no stray signal leaks in from a prior test on this thread."""
    from lib.project_mod.read_tools import drain_svg_render_signals
    drain_svg_render_signals()
    yield
    drain_svg_render_signals()


# ═══════════════════════════════════════════════════════════════════════
#  (a) SVG read → text in stream + exactly one render signal
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestSvgRenderSignal:

    def test_relative_svg_returns_text_and_signals_once(self, tmp_path):
        (tmp_path / 'logo.svg').write_text(_SVG)

        from lib.project_mod.read_tools import (
            tool_read_files, drain_svg_render_signals,
        )
        result = tool_read_files(str(tmp_path), [{'path': 'logo.svg'}])

        # Model stream: the XML source is present as TEXT (not a dict).
        assert isinstance(result, str)
        assert '<rect' in result and 'fill="red"' in result

        sig = drain_svg_render_signals()
        assert len(sig) == 1
        d = sig[0]
        assert d['filename'] == 'logo.svg'
        assert d['format'] == 'svg'
        assert d['uri'].startswith('data:image/svg+xml;base64,')
        # The base64 payload round-trips back to the original source.
        b64 = d['uri'].split(',', 1)[1]
        assert base64.b64decode(b64).decode('utf-8') == _SVG

    def test_absolute_svg_signals_once(self, tmp_path):
        p = tmp_path / 'abs.svg'
        p.write_text(_SVG)

        from lib.project_mod.read_tools import (
            tool_read_files, drain_svg_render_signals,
        )
        result = tool_read_files(str(tmp_path), [{'path': str(p)}])

        assert isinstance(result, str)
        assert '<svg' in result
        sig = drain_svg_render_signals()
        assert len(sig) == 1
        assert sig[0]['uri'].startswith('data:image/svg+xml;base64,')

    def test_mixed_batch_signals_only_the_svg(self, tmp_path):
        (tmp_path / 'a.svg').write_text(_SVG)
        (tmp_path / 'b.txt').write_text('hello world')

        from lib.project_mod.read_tools import (
            tool_read_files, drain_svg_render_signals,
        )
        tool_read_files(str(tmp_path), [{'path': 'a.svg'}, {'path': 'b.txt'}])
        sig = drain_svg_render_signals()
        assert len(sig) == 1
        assert sig[0]['filename'] == 'a.svg'


# ═══════════════════════════════════════════════════════════════════════
#  (b) non-SVG read → no signal
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestNonSvgNoSignal:

    def test_text_file_emits_no_signal(self, tmp_path):
        (tmp_path / 'main.py').write_text('def hello():\n    return 42\n')

        from lib.project_mod.read_tools import (
            tool_read_files, drain_svg_render_signals,
        )
        tool_read_files(str(tmp_path), [{'path': 'main.py'}])
        assert drain_svg_render_signals() == []


# ═══════════════════════════════════════════════════════════════════════
#  (c) oversized SVG → skipped
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestOversizedSvgSkipped:

    def test_svg_over_cap_is_not_signalled(self, tmp_path):
        from lib.project_mod.read_tools import (
            tool_read_files, drain_svg_render_signals, _MAX_SVG_RENDER_BYTES,
        )
        # Pad the SVG past the render cap with a comment so it's still valid XML.
        pad = 'x' * (_MAX_SVG_RENDER_BYTES + 1024)
        big = (f'<svg xmlns="http://www.w3.org/2000/svg"><!-- {pad} -->'
               '<rect/></svg>')
        (tmp_path / 'huge.svg').write_text(big)

        result = tool_read_files(str(tmp_path), [{'path': 'huge.svg'}])
        # Still readable as text (subject to the normal read truncation).
        assert isinstance(result, str)
        # But no inline render is signalled — the payload would bloat the meta.
        assert drain_svg_render_signals() == []

    def test_signal_helper_rejects_oversize_source_directly(self):
        from lib.project_mod.read_tools import (
            _signal_svg_render, drain_svg_render_signals, _MAX_SVG_RENDER_BYTES,
        )
        _signal_svg_render('x.svg', 'A' * (_MAX_SVG_RENDER_BYTES + 1))
        assert drain_svg_render_signals() == []
        _signal_svg_render('y.svg', '<svg/>')
        assert len(drain_svg_render_signals()) == 1


# ═══════════════════════════════════════════════════════════════════════
#  (d) drain is one-shot
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestDrainOneShot:

    def test_second_drain_is_empty(self, tmp_path):
        (tmp_path / 'logo.svg').write_text(_SVG)

        from lib.project_mod.read_tools import (
            tool_read_files, drain_svg_render_signals,
        )
        tool_read_files(str(tmp_path), [{'path': 'logo.svg'}])
        assert len(drain_svg_render_signals()) == 1
        assert drain_svg_render_signals() == []


# ═══════════════════════════════════════════════════════════════════════
#  (e) dedup-cache-hit path renders identically (root-cause robustness)
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestCacheHitRendersSvg:
    """A second identical read is served from the dedup cache, bypassing the
    handler (so no fresh signal fires). The cache-hit meta builder must
    reattach the memoized render URIs so the inline image does NOT vanish on
    the repeat read."""

    def test_cache_hit_meta_reattaches_svg_uris(self):
        from lib.tasks_pkg.tool_dispatch._flags import _build_cache_hit_meta
        uris = [{'filename': 'a.svg', 'format': 'svg',
                 'uri': 'data:image/svg+xml;base64,PHN2Lz4='}]
        meta = _build_cache_hit_meta(
            'read_files', {'path': 'a.svg'},
            'File: a.svg\n<svg/>', False, cached_display=uris,
        )
        assert meta.get('imageDataUris') == uris
        assert 'svg' in meta.get('badge', '')

    def test_cache_hit_without_display_has_no_uris(self):
        """A non-SVG text read hit (no memoized display) must NOT fabricate
        an imageDataUris field."""
        from lib.tasks_pkg.tool_dispatch._flags import _build_cache_hit_meta
        meta = _build_cache_hit_meta(
            'read_files', {'path': 'main.py'},
            'File: main.py\ndef f(): ...', False, cached_display=None,
        )
        assert 'imageDataUris' not in meta


# ═══════════════════════════════════════════════════════════════════════
#  NEUTER — disabling the signal makes the positive case fail
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestNeuterBites:

    def test_disabling_maybe_signal_svg_breaks_positive(self, tmp_path, monkeypatch):
        (tmp_path / 'logo.svg').write_text(_SVG)

        import lib.project_mod.read_tools as rt
        # Neuter: _maybe_signal_svg becomes a no-op → no signal is recorded.
        monkeypatch.setattr(rt, '_maybe_signal_svg', lambda *a, **k: None)

        result = rt.tool_read_files(str(tmp_path), [{'path': 'logo.svg'}])
        # Text read is unaffected (the neuter only kills the render signal)…
        assert isinstance(result, str) and '<svg' in result
        # …but the render signal is now GONE — proving it is load-bearing.
        assert rt.drain_svg_render_signals() == []
