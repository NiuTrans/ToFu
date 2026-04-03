"""Unit tests for refactored utilities.

Tests:
  - lib.utils.repair_json — JSON repair for malformed LLM outputs
  - lib.file_reader._compress_image — image compression (with in-memory PNG)
  - lib.model_info — model detection via model_info module + _clamp_max_tokens edges
  - lib.tasks_pkg.executor — ToolRegistry after handler extraction
  - Backward-compat imports (orchestrator._repair_json, llm_client.is_claude, etc.)

Run:  pytest tests/test_refactored_utils.py -m unit -v
"""
from __future__ import annotations

import json
import os
import sys

import pytest

# Ensure project root on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ═══════════════════════════════════════════════════════════
#  1. repair_json
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestRepairJson:
    """Test repair_json from lib.utils."""

    def test_empty_input(self):
        from lib.utils import repair_json
        assert repair_json('') == {}
        assert repair_json('   ') == {}

    def test_valid_json_passthrough(self):
        from lib.utils import repair_json
        data = {'key': 'value', 'num': 42, 'nested': {'a': [1, 2, 3]}}
        assert repair_json(json.dumps(data)) == data

    def test_trailing_commas(self):
        from lib.utils import repair_json
        assert repair_json('{"a": 1, "b": 2,}') == {'a': 1, 'b': 2}
        assert repair_json('{"list": [1, 2, 3,]}') == {'list': [1, 2, 3]}

    def test_unterminated_string(self):
        from lib.utils import repair_json
        result = repair_json('{"key": "unterminated value')
        assert result['key'] == 'unterminated value'

    def test_missing_closing_brace(self):
        from lib.utils import repair_json
        result = repair_json('{"a": 1, "b": 2')
        assert result == {'a': 1, 'b': 2}

    def test_missing_closing_bracket_and_brace(self):
        from lib.utils import repair_json
        # Missing both ] and } — repair adds them
        result = repair_json('{"list": [1, 2, 3')
        assert result['list'] == [1, 2, 3]

    def test_windows_path_escapes(self):
        """Windows paths like C:\\Users produce invalid JSON escapes."""
        from lib.utils import repair_json
        # \U is not a valid JSON escape — should be fixed
        raw = '{"path": "C:\\\\Users\\\\test\\\\file.txt"}'
        result = repair_json(raw)
        assert 'path' in result

    def test_invalid_escape_sequence(self):
        """Test repair of \\m, \\. etc. that LLMs produce."""
        from lib.utils import repair_json
        # Construct a string with an invalid \m escape
        raw = '{"msg": "test\\message"}'
        result = repair_json(raw)
        assert 'msg' in result

    def test_json_decode_error_raised_on_hopeless(self):
        """Truly broken input should raise JSONDecodeError."""
        from lib.utils import repair_json
        with pytest.raises(json.JSONDecodeError):
            repair_json('not json at all {{{{')

    def test_backward_compat_alias(self):
        """_repair_json is an alias for repair_json."""
        from lib.utils import _repair_json, repair_json
        assert _repair_json is repair_json


# ═══════════════════════════════════════════════════════════
#  2. Backward-compat imports
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestBackwardCompatImports:
    """Verify refactored code is still importable from old paths."""

    def test_repair_json_from_orchestrator(self):
        from lib.tasks_pkg.orchestrator import _repair_json
        from lib.utils import repair_json
        assert _repair_json is repair_json

    def test_model_detection_from_llm_client(self):
        """All model detection functions still importable from llm_client."""
        from lib.llm_client import (
            is_claude,
            is_doubao,
            is_gemini,
            is_glm,
            is_gpt,
            is_longcat,
            is_minimax,
            is_qwen,
        )
        # Quick sanity
        assert is_claude('claude-4')
        assert is_gpt('gpt-4o')

    def test_clamp_max_tokens_from_llm_client(self):
        from lib.llm_client import _clamp_max_tokens
        assert _clamp_max_tokens('qwen-turbo', 100000) == 16384

    def test_model_info_direct_import(self):
        """model_info module is importable directly."""
        from lib.model_info import _clamp_max_tokens, is_claude, is_qwen
        assert is_claude('claude-sonnet-4')
        assert is_qwen('qwq-plus')
        assert _clamp_max_tokens('gpt-4o', 100000) == 32768


# ═══════════════════════════════════════════════════════════
#  3. _clamp_max_tokens edge cases
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestClampMaxTokens:
    """Test _clamp_max_tokens edge cases."""

    def test_below_limit_passthrough(self):
        from lib.model_info import _clamp_max_tokens
        assert _clamp_max_tokens('qwen-turbo', 1024) == 1024
        assert _clamp_max_tokens('gpt-4o', 1024) == 1024

    def test_above_limit_clamped(self):
        from lib.model_info import _clamp_max_tokens
        assert _clamp_max_tokens('qwen-turbo', 100000) == 16384
        assert _clamp_max_tokens('gpt-4o', 100000) == 32768
        assert _clamp_max_tokens('gemini-2.5-pro', 200000) == 65536

    def test_unknown_model_passthrough(self):
        from lib.model_info import _clamp_max_tokens
        assert _clamp_max_tokens('unknown-model-xyz', 999999) == 999999

    def test_qwen_variant_limits(self):
        from lib.model_info import _qwen_max_output
        assert _qwen_max_output('qwq-plus') == 65536
        assert _qwen_max_output('qwen3-coder-plus') == 65536
        assert _qwen_max_output('qwen-turbo') == 16384
        assert _qwen_max_output('qwen-plus') == 32768
        assert _qwen_max_output('qwen-max') == 32768
        assert _qwen_max_output('qwen-unknown') == 16384

    def test_glm_high_limit(self):
        from lib.model_info import _clamp_max_tokens
        # GLM has 131072 limit
        assert _clamp_max_tokens('glm-4-plus', 200000) == 131072
        assert _clamp_max_tokens('glm-4-plus', 50000) == 50000


# ═══════════════════════════════════════════════════════════
#  4. _compress_image
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestCompressImage:
    """Test _compress_image from lib.file_reader."""

    def _make_test_png(self, width=100, height=100) -> bytes:
        """Create a small valid PNG in memory."""
        import io

        from PIL import Image
        img = Image.new('RGB', (width, height), color=(255, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue()

    def test_small_image_compressed_to_jpeg(self):
        from lib.file_reader import _compress_image
        raw = self._make_test_png()
        result_bytes, mime, was_compressed = _compress_image(raw, max_kb=1024)
        assert mime == 'image/jpeg'
        assert isinstance(result_bytes, bytes)
        assert len(result_bytes) > 0

    def test_rgba_to_rgb_conversion(self):
        """RGBA images should be converted to RGB for JPEG."""
        import io

        from PIL import Image

        from lib.file_reader import _compress_image
        img = Image.new('RGBA', (100, 100), color=(255, 0, 0, 128))
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        raw = buf.getvalue()
        result_bytes, mime, was_compressed = _compress_image(raw, max_kb=1024)
        assert mime == 'image/jpeg'
        # Verify the result is valid JPEG
        result_img = Image.open(io.BytesIO(result_bytes))
        assert result_img.mode == 'RGB'

    def test_only_one_definition_exists(self):
        """Ensure _compress_image is only defined in lib/file_reader.py."""
        import subprocess
        result = subprocess.run(
            ['grep', '-rn', '--include=*.py', 'def _compress_image', 'lib/', 'routes/'],
            capture_output=True, text=True,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        lines = [l for l in result.stdout.strip().split('\n') if l]
        assert len(lines) == 1, f"Expected 1 definition, found {len(lines)}: {lines}"
        assert 'lib/file_reader.py' in lines[0]


# ═══════════════════════════════════════════════════════════
#  5. ToolRegistry after handler extraction
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestToolRegistryPostRefactor:
    """Verify all handlers are registered after extraction to submodules."""

    def test_tool_registry_has_expected_tools(self):
        from lib.tasks_pkg.executor import tool_registry
        tools = {name for name, _, _ in tool_registry.list_tools()}
        # Core tools that must be present
        expected = {
            'web_search', 'fetch_url', 'tool_search', 'ask_human',
            '__code_exec__',
        }
        for t in expected:
            assert t in tools, f"Missing tool: {t}"

    def test_tool_registry_lookup_works(self):
        from lib.tasks_pkg.executor import tool_registry
        assert tool_registry.lookup('web_search') is not None
        assert tool_registry.lookup('fetch_url') is not None
        assert tool_registry.lookup('ask_human') is not None
        assert tool_registry.lookup('nonexistent_tool_xyz') is None

    def test_execute_tool_one_importable(self):
        from lib.tasks_pkg.executor import _execute_tool_one
        assert callable(_execute_tool_one)

    def test_lazy_import_from_tasks_pkg(self):
        """tool_registry importable via lib.tasks_pkg (lazy import)."""
        from lib.tasks_pkg import _execute_tool_one, tool_registry
        assert tool_registry is not None
        assert callable(_execute_tool_one)


# ═══════════════════════════════════════════════════════════
#  6. _parse_token_limit_from_error
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestParseTokenLimit:
    """Test _parse_token_limit_from_error edge cases."""

    def test_range_format(self):
        from lib.model_info import _parse_token_limit_from_error
        result = _parse_token_limit_from_error(
            'Range of max_tokens should be [1, 65536]', 'test-model'
        )
        assert result == 65536

    def test_at_most_format(self):
        from lib.model_info import _parse_token_limit_from_error
        result = _parse_token_limit_from_error(
            'max_tokens must be at most 32768', 'test-model'
        )
        assert result == 32768

    def test_between_format(self):
        from lib.model_info import _parse_token_limit_from_error
        result = _parse_token_limit_from_error(
            'max_tokens value must be between 1 and 16384', 'test-model'
        )
        assert result == 16384

    def test_no_match_returns_none(self):
        from lib.model_info import _parse_token_limit_from_error
        result = _parse_token_limit_from_error(
            'Something completely different error', 'test-model'
        )
        assert result is None
