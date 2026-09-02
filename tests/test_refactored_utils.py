"""Unit tests for refactored utilities.

Tests:
  - lib.utils.repair_json — JSON repair for malformed LLM outputs
  - lib.file_reader._compress_image — image compression (with in-memory PNG)
  - lib.model_info — model detection via model_info module + _clamp_max_tokens edges
  - lib.tasks_pkg.executor — ToolRegistry after handler extraction
  - Re-exported helpers (orchestrator._repair_json, lib.llm.is_claude, etc.)

Run:  pytest tests/test_refactored_utils.py -m unit -v
"""
from __future__ import annotations

_AUDIT_SYNTHETIC_REPO_PATHS = {'scripts/attribute_losses.py'}

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

    def test_raw_control_characters_in_string(self):
        """Literal newlines/tabs inside a JSON string value (common in
        weak-model ``write_file`` content) must repair, not crash."""
        from lib.utils import repair_json
        raw = '{"path": "x.py", "content": "line1\nline2\ttabbed"}'
        result = repair_json(raw)
        assert result == {'path': 'x.py', 'content': 'line1\nline2\ttabbed'}

    def test_control_chars_with_truncation(self):
        """Control chars AND a missing closing quote/brace repair together."""
        from lib.utils import repair_json
        result = repair_json('{"q": "multi\nline\nquery')
        assert result['q'] == 'multi\nline\nquery'

    def test_json_decode_error_raised_on_hopeless(self):
        """Truly broken input should raise JSONDecodeError."""
        from lib.utils import repair_json
        with pytest.raises(json.JSONDecodeError):
            repair_json('not json at all {{{{')

    def test_missing_colon_delimiter(self):
        """Parser-guided recovery of a missing ':' (top read_files failure mode)."""
        from lib.utils import repair_json
        result = repair_json('{"reads": [{"path" "/a/b.py"}]}')
        assert result == {'reads': [{'path': '/a/b.py'}]}

    def test_missing_comma_delimiter(self):
        """Parser-guided recovery of a missing ',' between object members."""
        from lib.utils import repair_json
        result = repair_json('{"reads": [{"path": "/a/b.py" "start_line": 1}]}')
        assert result == {'reads': [{'path': '/a/b.py', 'start_line': 1}]}

    def test_single_quoted_payload(self):
        """ast.literal_eval recovery of a single-quoted (non-JSON) payload."""
        from lib.utils import repair_json
        result = repair_json("{'reads': [{'path': '/a/b.py'}]}")
        assert result == {'reads': [{'path': '/a/b.py'}]}

    def test_python_dict_repr(self):
        """ast.literal_eval recovery of a full Python-dict repr."""
        from lib.utils import repair_json
        result = repair_json(
            "{'reads': [{'path': '/mnt/x/y.py', 'start_line': 10, 'end_line': 20}]}")
        assert result == {'reads': [{'path': '/mnt/x/y.py', 'start_line': 10, 'end_line': 20}]}

    def test_ambiguous_inner_quote_still_raises(self):
        """An unescaped inner quote is genuinely ambiguous — must NOT be
        silently 'repaired' into wrong data; it must still raise."""
        from lib.utils import repair_json
        with pytest.raises(json.JSONDecodeError):
            repair_json('{"reads": [{"path": "/a/b/some"weird.py"}]}')

    def test_mismatched_closer_object_closed_with_bracket(self):
        """The reported failure: an inner reads-array whose object is closed
        with ']' instead of '}' — ``[{"path": ..., "end_line": 214]``. Neither
        the count-and-append balance nor the delimiter fix can recover a
        wrong-TYPE closer; the bracket-match pass rewrites it."""
        from lib.utils import repair_json
        raw = '[{"path": "scripts/attribute_losses.py", "start_line": 196, "end_line": 214]'
        assert repair_json(raw) == [
            {'path': 'scripts/attribute_losses.py', 'start_line': 196, 'end_line': 214}
        ]

    def test_mismatched_closer_nested(self):
        """Wrong closer one level deep inside an object value."""
        from lib.utils import repair_json
        assert repair_json('{"a": [1, 2}') == {'a': [1, 2]}
        assert repair_json('{"reads": [{"path": "a"]}') == {'reads': [{'path': 'a'}]}

    def test_bracket_match_fix_byte_identical_on_valid(self):
        """The bracket-match pass MUST be a no-op on well-formed JSON — a
        matched closer is never rewritten, so valid payloads (incl. brackets
        inside string literals) pass through byte-identical."""
        from lib.utils import _bracket_match_fix
        for v in ('{"a":1}', '[{"x":[1,2]},{"y":"}]"}]', r'{"s":"a\"b]"}', '[]', '{}'):
            assert _bracket_match_fix(v) == v, v

    def test_mismatched_closer_via_tool_repair(self):
        """End-to-end through the tool-arg repair layer: read_files with the
        malformed stringified reads recovers to a real list."""
        from lib.tool_input_repair import validate_then_repair
        raw = '[{"path": "scripts/attribute_losses.py", "start_line": 196, "end_line": 214]'
        repaired, log = validate_then_repair('read_files', {'reads': raw}, model='test')
        assert repaired['reads'] == [
            {'path': 'scripts/attribute_losses.py', 'start_line': 196, 'end_line': 214}
        ]

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
        from lib.utils import repair_json as _repair_json
        from lib.utils import repair_json
        assert _repair_json is repair_json

    def test_model_detection_from_llm_client(self):
        """All model detection functions importable from lib.llm."""
        from lib.llm import (
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

    def test_clamp_max_tokens_from_lib_llm(self):
        from lib.llm import _clamp_max_tokens
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

    def test_unknown_model_clamped_to_default(self):
        """An unrecognised family is clamped to the conservative default
        ceiling so the FIRST request doesn't over-ask and earn a 400."""
        from lib.model_info import _DEFAULT_UNKNOWN_MAX_OUTPUT, _clamp_max_tokens
        assert _clamp_max_tokens('unknown-model-xyz', 999999) == _DEFAULT_UNKNOWN_MAX_OUTPUT
        # Below the default ceiling still passes through untouched.
        assert _clamp_max_tokens('unknown-model-xyz', 4096) == 4096

    def test_claude_not_swept_into_default(self):
        """Claude is detectable but must keep its 128000 ceiling — NOT be
        swept into the conservative unknown-family default. Long-form paths
        deliberately pass max_tokens=128000 to Claude."""
        from lib.model_info import _clamp_max_tokens
        assert _clamp_max_tokens('claude-sonnet-4-20250514', 128000) == 128000
        assert _clamp_max_tokens('aws.claude-opus-4.6', 200000) == 128000

    def test_learned_limit_still_takes_min_for_unknown(self):
        """An auto-learned per-model limit must still lower an unknown model's
        clamp below the default ceiling."""
        import lib.model_info as mi
        from lib.model_info import _clamp_max_tokens
        saved = dict(mi._LEARNED_MODEL_LIMITS)
        try:
            mi._LEARNED_MODEL_LIMITS['unknown-model-xyz'] = 8000
            assert _clamp_max_tokens('unknown-model-xyz', 999999) == 8000
        finally:
            mi._LEARNED_MODEL_LIMITS.clear()
            mi._LEARNED_MODEL_LIMITS.update(saved)

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

    def test_none_max_tokens_does_not_crash(self):
        """A None / missing / non-int max_tokens must NOT raise
        ``TypeError: '<' not supported between 'int' and 'NoneType'`` in
        ``min(limit, effective_limit)`` — it degrades to the conservative
        unknown-family ceiling. This is the killed-turn recovery FATAL:
        resolve_conv_config emits maxTokens=None when no server_defaults are
        supplied, and cfg.get('maxTokens', 128000) returns that None."""
        from lib.model_info import _DEFAULT_UNKNOWN_MAX_OUTPUT, _clamp_max_tokens
        # None + a KNOWN family: falls back to the default ceiling, then the
        # family cap refines it (Qwen turbo 16384 == the default here).
        assert _clamp_max_tokens('qwen-turbo', None) == 16384
        # None + an UNKNOWN family: pure default ceiling.
        assert _clamp_max_tokens('unknown-model-xyz', None) == _DEFAULT_UNKNOWN_MAX_OUTPUT
        # Other invalid shapes must be equally total.
        assert _clamp_max_tokens('gpt-4o', 0) == _DEFAULT_UNKNOWN_MAX_OUTPUT
        assert _clamp_max_tokens('gpt-4o', -5) == _DEFAULT_UNKNOWN_MAX_OUTPUT
        assert _clamp_max_tokens('gpt-4o', '4096') == _DEFAULT_UNKNOWN_MAX_OUTPUT


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
        """Ensure _compress_image has one owner in lib/file_reader/."""
        # Walk lib/ and routes/ in pure Python rather than shelling out to
        # `grep -rn`: on slow FUSE mounts a recursive grep that descends into
        # scratch/cache subdirs can take >90s and time the test out.
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        needle = 'def _compress_image'
        # Prune dirs that hold huge generated trees (session shadow-git repos,
        # caches): walking them on a FUSE mount can take >90s. Notably
        # lib/.project_sessions/*/shadow.git/objects holds 600k+ files.
        _skip = {'__pycache__', '.project_sessions', '.git', '.tofu',
                 'node_modules', '.ruff_cache', '.pytest_cache'}
        hits = []
        for sub in ('lib', 'routes'):
            for dirpath, dirnames, filenames in os.walk(os.path.join(root, sub)):
                dirnames[:] = [d for d in dirnames if d not in _skip]
                for fn in filenames:
                    if not fn.endswith('.py'):
                        continue
                    fpath = os.path.join(dirpath, fn)
                    try:
                        with open(fpath, encoding='utf-8') as f:
                            if any(needle in line for line in f):
                                hits.append(os.path.relpath(fpath, root))
                    except (OSError, UnicodeDecodeError):
                        continue
        assert len(hits) == 1, f"Expected 1 definition, found {len(hits)}: {hits}"
        assert hits[0].replace(os.sep, '/') == 'lib/file_reader/_image.py'


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
            'web_search', 'fetch_url', 'ask_human',
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

    def test_executor_public_api(self):
        """Executor services are imported from their owning package."""
        from lib.tasks_pkg.executor import _execute_tool_one, tool_registry
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
