"""Regression + property tests for the binary-blob-in-text-stream bug CLASS.

Origin: conv ``mqgfkmxy`` (2026-06-16). The model read four sub-512KB PNG
icons by **project-relative** path. The relative read path
(``_read_project_file``) had no image/binary detection — it opened the PNGs
with ``errors='replace'`` and returned ~1.7M chars of U+FFFD garbage as a
single tool result, which tokenised to ~1.36M tokens and hard-failed the
1M context limit (HTTP 400). Whole-message reactive compaction could not
recover because the overflow lived in one un-droppable tail message.

These tests pin the FOUR-LAYER defense so the bug class stays
unrepresentable:

  L1 (root)     — read_files classifies file type AFTER path resolution,
                  identically for relative and absolute paths. A relative
                  PNG → __screenshot__ dict (native image protocol); a
                  relative non-image binary → short stub, never raw bytes.
  L2 (catch-all)— clamp_tool_result_text caps ANY single tool-result text
                  (incl. budget-exempt read_files) at a hard ceiling.
  L4 (remediate)— reactive_compact._truncate_largest_message shrinks the
                  single largest text message IN PLACE.
  PROPERTY      — for every registered tool, a result feeding a binary blob
                  is either a __screenshot__ dict or ≤ the hard ceiling.

Run:  pytest tests/test_binary_blob_text_stream_guard.py -v
"""
from __future__ import annotations

import os
import struct
import sys
import zlib

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Helpers ──────────────────────────────────────────────────────────

def _make_png(path: str, *, target_bytes: int) -> None:
    """Write a real (decodable) PNG of roughly *target_bytes* to *path*.

    A valid 1×1 PNG header + a large ancillary tEXt chunk padded to size.
    The bytes are genuine PNG (magic header intact) so file_reader's
    magic-byte sniff classifies it as image/png, and large enough to blow
    past the 512KB MAX_FILE_SIZE guard / hard ceiling when mis-read.
    """
    sig = b'\x89PNG\r\n\x1a\n'
    # IHDR (1x1, 8-bit RGB)
    ihdr_data = struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)
    ihdr = struct.pack('>I', len(ihdr_data)) + b'IHDR' + ihdr_data
    ihdr += struct.pack('>I', zlib.crc32(b'IHDR' + ihdr_data) & 0xffffffff)
    # tEXt padding chunk to reach target size
    pad_len = max(0, target_bytes - len(sig) - len(ihdr) - 12 - 8)
    text_data = b'Comment\x00' + (b'A' * pad_len)
    text = struct.pack('>I', len(text_data)) + b'tEXt' + text_data
    text += struct.pack('>I', zlib.crc32(b'tEXt' + text_data) & 0xffffffff)
    iend = struct.pack('>I', 0) + b'IEND' + struct.pack('>I', zlib.crc32(b'IEND') & 0xffffffff)
    with open(path, 'wb') as f:
        f.write(sig + ihdr + text + iend)


def _make_binary_blob(path: str, *, n: int) -> None:
    """Write *n* bytes of non-image binary (high non-printable density).

    Cycles through control-byte values 0-6 (all < 8) so the first-8KB sniff
    sees ~100% non-printable — mirroring a real compiled object / archive,
    which is what triggers the stub path.
    """
    with open(path, 'wb') as f:
        f.write(bytes(i % 7 for i in range(n)))


# ═══════════════════════════════════════════════════════════════════════
#  Layer 1 — read_files classifies by file type AFTER path resolution
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestReadFilesBinaryRouting:
    """The root fix: a relative-path image must route to the
    __screenshot__ protocol — exactly like an absolute-path image — and a
    relative-path non-image binary must return a short stub, never raw
    decoded bytes."""

    def test_relative_png_under_512kb_returns_screenshot_dict(self, tmp_path):
        # 480KB — the exact regime that detonated conv mqgfkmxy (under the
        # 512KB MAX_FILE_SIZE guard, so the old code read it as text).
        png = tmp_path / 'icon.png'
        _make_png(str(png), target_bytes=480_000)

        from lib.project_mod.read_tools import _read_project_file
        result = _read_project_file(str(tmp_path), 'icon.png')

        assert isinstance(result, dict), (
            'relative PNG must return a dict, not decoded text — got '
            f'{type(result).__name__}'
        )
        assert result.get('__screenshot__') is True
        # The base64 lives in dataUrl (image protocol), NOT in any text field.
        assert 'dataUrl' in result

    def test_relative_png_does_not_leak_base64_as_text(self, tmp_path):
        png = tmp_path / 'big.png'
        _make_png(str(png), target_bytes=480_000)

        from lib.project_mod.read_tools import tool_read_files
        result = tool_read_files(str(tmp_path), [{'path': 'big.png'}])

        # Batch read of an image → __batch_images__ wrapper, text content is
        # only the short fallback (never the 480KB payload).
        assert isinstance(result, dict)
        assert '__batch_images__' in result
        assert len(result.get('_text_content', '')) < 2000

    def test_relative_nonimage_binary_returns_stub_not_garbage(self, tmp_path):
        blob = tmp_path / 'lib.so'
        _make_binary_blob(str(blob), n=400_000)

        from lib.project_mod.read_tools import _read_project_file
        result = _read_project_file(str(tmp_path), 'lib.so')

        assert isinstance(result, str)
        assert 'Binary file' in result
        # Never the 400KB of decoded bytes.
        assert len(result) < 2000

    def test_relative_text_file_still_reads_normally(self, tmp_path):
        src = tmp_path / 'main.py'
        src.write_text('def hello():\n    return 42\n' * 50)

        from lib.project_mod.read_tools import _read_project_file
        result = _read_project_file(str(tmp_path), 'main.py')

        assert isinstance(result, str)
        assert 'def hello' in result
        assert 'Binary file' not in result

    def test_svg_text_file_not_diverted_as_binary(self, tmp_path):
        # .svg is in BINARY_EXTENSIONS for SCANNING, but it's real XML text —
        # must still be readable (content-sniff passes, not image-routed).
        svg = tmp_path / 'logo.svg'
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg">'
                       '<rect width="10" height="10"/></svg>\n' * 100)

        from lib.project_mod.read_tools import _read_project_file
        result = _read_project_file(str(tmp_path), 'logo.svg')

        assert isinstance(result, str)
        assert '<svg' in result
        assert 'Binary file' not in result


# ═══════════════════════════════════════════════════════════════════════
#  Layer 2 — tool-agnostic hard ceiling (catch-all backstop)
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestHardCeilingClamp:
    """clamp_tool_result_text is the last line of defence: no single tool
    result text may exceed the hard ceiling, with NO per-tool exemption."""

    def test_within_ceiling_passes_through_unchanged(self):
        from lib.tasks_pkg.compaction.api import clamp_tool_result_text
        text = 'x' * 1000
        assert clamp_tool_result_text('grep_search', text) == text

    def test_over_ceiling_is_clamped(self):
        from lib.tasks_pkg.compaction.api import (
            clamp_tool_result_text,
        )
        from lib.tasks_pkg.compaction._constants import (
            _SINGLE_RESULT_HARD_CEILING_CHARS,
        )
        text = 'x' * (_SINGLE_RESULT_HARD_CEILING_CHARS + 500_000)
        out = clamp_tool_result_text('read_files', text, tc_id='toolu_abc')
        assert len(out) < len(text)
        assert len(out) <= _SINGLE_RESULT_HARD_CEILING_CHARS + 1000  # + marker
        assert 'elided by hard ceiling' in out

    def test_exempt_tool_is_NOT_exempt_here(self):
        """read_files is exempt from Layer 0 budget but MUST still be
        clamped by this Layer 2 backstop — that's the whole point."""
        from lib.tasks_pkg.compaction.api import (
            clamp_tool_result_text,
        )
        from lib.tasks_pkg.compaction._constants import (
            _BUDGET_EXEMPT_TOOLS,
            _SINGLE_RESULT_HARD_CEILING_CHARS,
        )
        assert 'read_files' in _BUDGET_EXEMPT_TOOLS  # premise
        huge = 'B' * (_SINGLE_RESULT_HARD_CEILING_CHARS * 2)
        out = clamp_tool_result_text('read_files', huge)
        assert len(out) < len(huge)

    def test_screenshot_dict_passes_through(self):
        """Non-str content (image dicts) is never touched — images ride the
        native protocol and never enter the text stream this guards."""
        from lib.tasks_pkg.compaction.api import clamp_tool_result_text
        img = {'__screenshot__': True, 'dataUrl': 'data:image/png;base64,' + 'A' * 9_000_000}
        assert clamp_tool_result_text('read_files', img) is img


# ═══════════════════════════════════════════════════════════════════════
#  Layer 4 — reactive in-place truncation of the largest message
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestReactiveInPlaceTruncation:
    """_truncate_largest_message shrinks the single fattest text message —
    the failure mode whole-message dropping cannot fix."""

    def test_truncates_the_largest_message(self):
        from lib.tasks_pkg.compaction._reactive._strip import (
            _truncate_largest_message,
        )
        messages = [
            {'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'hi'},
            {'role': 'tool', 'tool_call_id': 't1', 'content': 'Z' * 2_000_000},
            {'role': 'assistant', 'content': 'ok'},
        ]
        idx, freed = _truncate_largest_message(messages, ceiling_chars=800_000)
        assert idx == 2
        assert freed > 0
        assert len(messages[2]['content']) < 2_000_000
        assert 'emergency reactive truncation' in messages[2]['content']
        # Other messages untouched.
        assert messages[1]['content'] == 'hi'

    def test_noop_when_all_messages_small(self):
        from lib.tasks_pkg.compaction._reactive._strip import (
            _truncate_largest_message,
        )
        messages = [{'role': 'tool', 'content': 'x' * 100}]
        idx, freed = _truncate_largest_message(messages, ceiling_chars=800_000)
        assert idx == -1
        assert freed == 0

    def test_skips_system_and_multimodal_content(self):
        from lib.tasks_pkg.compaction._reactive._strip import (
            _truncate_largest_message,
        )
        messages = [
            {'role': 'system', 'content': 'S' * 2_000_000},  # protected
            {'role': 'user', 'content': [{'type': 'image_url',
                                          'image_url': {'url': 'd' * 2_000_000}}]},  # not str
        ]
        idx, freed = _truncate_largest_message(messages, ceiling_chars=800_000)
        assert idx == -1  # neither is an eligible large str message


# ═══════════════════════════════════════════════════════════════════════
#  PROPERTY — no registered tool can flood the text stream with a blob
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestNoToolFloodsContextProperty:
    """Generic property: for EVERY committed tool-result text, after the
    Layer 2 backstop, the content is either a __screenshot__ dict or within
    the hard ceiling. This fails the moment a future ingress point
    reintroduces the bug class — which per-incident tests cannot catch."""

    def test_every_tool_result_is_bounded_or_image(self):
        from lib.tasks_pkg.compaction.api import (
            clamp_tool_result_text,
        )
        from lib.tasks_pkg.compaction._constants import (
            _SINGLE_RESULT_HARD_CEILING_CHARS,
        )
        from lib.tasks_pkg.executor import tool_registry

        # Synthesise an oversized blob "result" attributed to each tool and
        # assert the backstop bounds it. Covers exact + set-based + special.
        oversized = 'Q' * (_SINGLE_RESULT_HARD_CEILING_CHARS * 2)
        tool_names = [name for name, _cat, _desc in tool_registry.list_tools()]
        assert tool_names, 'tool_registry returned no tools — registry not loaded'

        for name in tool_names:
            out = clamp_tool_result_text(name, oversized, tc_id='toolu_x')
            assert isinstance(out, str)
            assert len(out) <= _SINGLE_RESULT_HARD_CEILING_CHARS + 1000, (
                f'tool {name!r} result not bounded by hard ceiling — '
                f'len={len(out)}'
            )

    def test_image_dict_result_is_never_stringified_by_guard(self):
        """The guard must pass image dicts through untouched for every tool
        name (so a __screenshot__ never gets str()'d into base64 text)."""
        from lib.tasks_pkg.compaction.api import clamp_tool_result_text
        img = {'__screenshot__': True, 'dataUrl': 'data:image/png;base64,' + 'A' * 5_000_000}
        for name in ('read_files', 'grep_search', 'web_search', 'fetch_url'):
            assert clamp_tool_result_text(name, img) is img


# ═══════════════════════════════════════════════════════════════════════
#  Ownership reachability — each symbol has one explicit import path
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.unit
class TestNewSymbolsReachable:
    def test_public_clamp_is_in_api(self):
        from lib.tasks_pkg.compaction.api import clamp_tool_result_text
        assert callable(clamp_tool_result_text)

    def test_private_emergency_truncation_has_a_concrete_owner(self):
        from lib.tasks_pkg.compaction._reactive._strip import (
            _truncate_largest_message,
        )
        assert callable(_truncate_largest_message)

    def test_hard_ceiling_has_a_concrete_owner(self):
        from lib.tasks_pkg.compaction._constants import (
            _SINGLE_RESULT_HARD_CEILING_CHARS,
        )
        assert _SINGLE_RESULT_HARD_CEILING_CHARS > 0
