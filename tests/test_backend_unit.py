"""Backend unit tests — no server, no browser, no network.

Tests pure logic modules:
  - lib.llm (build_body, model detection, max_tokens clamping)
  - lib.protocols (Protocol interfaces)
  - lib.pricing (cost calculation)
  - lib.utils (safe_json, etc.)
  - lib.storage_sidecar (schema and semantic storage contracts)
  - lib.tests.validate_imports (all modules import cleanly)

Run:  pytest tests/test_backend_unit.py -m unit
"""
from __future__ import annotations

import json
import os
import sys

import pytest

# Ensure project root on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ═══════════════════════════════════════════════════════════
#  1. Model detection & build_body
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestModelDetection:
    """Verify model family detection helpers."""

    def test_is_claude(self):
        from lib.llm import is_claude
        assert is_claude("aws.claude-opus-4.6")
        assert is_claude("claude-sonnet-4-20250514")
        assert not is_claude("gpt-4o")
        assert not is_claude("qwen3.5-plus")

    def test_is_qwen(self):
        from lib.llm import is_qwen
        assert is_qwen("qwen3.5-plus")
        assert is_qwen("qwen-max")
        assert not is_qwen("claude-sonnet-4-20250514")

    def test_is_gemini(self):
        from lib.llm import is_gemini
        assert is_gemini("gemini-2.5-pro")
        assert is_gemini("gemini-3.1-flash-lite-preview")
        assert not is_gemini("gpt-4o")

    def test_is_minimax(self):
        from lib.llm import is_minimax
        assert is_minimax("MiniMax-M2.7")
        assert not is_minimax("claude-sonnet-4-20250514")

    def test_is_doubao(self):
        from lib.llm import is_doubao
        assert is_doubao("Doubao-Seed-2.0-pro")
        assert not is_doubao("gpt-4o")

    def test_no_cross_detection(self):
        """Each model is detected by exactly one family."""
        from lib.llm import is_claude, is_doubao, is_gemini, is_minimax, is_qwen

        models = {
            "aws.claude-opus-4.6": "claude",
            "qwen3.5-plus": "qwen",
            "gemini-2.5-pro": "gemini",
            "MiniMax-M2.7": "minimax",
            "Doubao-Seed-2.0-pro": "doubao",
        }
        detectors = {
            "claude": is_claude, "qwen": is_qwen, "gemini": is_gemini,
            "minimax": is_minimax, "doubao": is_doubao,
        }
        for model, expected_family in models.items():
            for family, fn in detectors.items():
                if family == expected_family:
                    assert fn(model), f"{model} should be {family}"
                else:
                    assert not fn(model), f"{model} should NOT be {family}"


@pytest.mark.unit
class TestBuildBody:
    """Verify build_body produces correct API parameters."""

    DUMMY_MSGS = [{"role": "user", "content": "Hello"}]

    def test_max_tokens_clamped_per_model(self):
        from lib.llm import build_body

        # Qwen: per-model limits (plus=32768, turbo=16384, etc.)
        # Use hardcoded model names — env-based QWEN_MODEL may be empty in CI
        body = build_body('qwq-plus', self.DUMMY_MSGS, max_tokens=200000, stream=False)
        assert body["max_tokens"] <= 65536  # family ceiling
        # Specific Qwen model limits
        body_turbo = build_body("qwen-turbo", self.DUMMY_MSGS, max_tokens=200000, stream=False)
        assert body_turbo["max_tokens"] == 16384
        body_plus = build_body("qwen-plus", self.DUMMY_MSGS, max_tokens=200000, stream=False)
        assert body_plus["max_tokens"] == 32768

        # Doubao: 16384 limit
        body = build_body('doubao-seed-1-6', self.DUMMY_MSGS, max_tokens=200000, stream=False)
        assert body["max_tokens"] <= 16384

    def test_small_max_tokens_passthrough(self):
        from lib.llm import build_body

        # 1024 is well below any model's limit — should pass through
        body = build_body('qwen-plus', self.DUMMY_MSGS, max_tokens=1024, stream=False)
        assert body["max_tokens"] == 1024

    def test_claude_thinking_adaptive(self):
        from lib.llm import build_body

        body = build_body('claude-sonnet-4-20250514', self.DUMMY_MSGS, max_tokens=4096,
                         thinking_enabled=True, stream=False)
        assert "thinking" in body
        assert body["thinking"]["type"] == "adaptive"
        assert "enable_thinking" not in body

    def test_qwen_thinking_param(self):
        from lib.llm import build_body

        body = build_body('qwen-plus', self.DUMMY_MSGS, max_tokens=4096,
                         thinking_enabled=True, stream=False)
        assert "enable_thinking" in body
        assert "thinking" not in body  # no Claude-style thinking block

    def test_chat_template_kwargs_thinking_enabled(self):
        """sglang / vLLM gate thinking via chat_template_kwargs.

        Top-level ``enable_thinking`` is silently ignored by these
        engines, so a self-hosted Qwen3 / GLM5 / DeepSeek-V4 needs the
        ``chat_template_kwargs`` body shape instead. Auto-verifier
        relies on this for fan-out across 7 sglang endpoints.
        """
        from lib.llm import build_body

        body = build_body('qwen35-0p8b', self.DUMMY_MSGS, max_tokens=4096,
                          thinking_enabled=True, stream=False,
                          thinking_format='chat_template_kwargs')
        assert body.get('chat_template_kwargs', {}).get('enable_thinking') is True
        # Top-level enable_thinking must NOT also be set — the engine
        # ignores it and a stale ``true`` value would mislead operators
        # reading server logs.
        assert 'enable_thinking' not in body
        assert 'thinking' not in body

    def test_chat_template_kwargs_thinking_disabled(self):
        from lib.llm import build_body

        body = build_body('qwen35-0p8b', self.DUMMY_MSGS, max_tokens=4096,
                          thinking_enabled=False, stream=False,
                          thinking_format='chat_template_kwargs')
        assert body.get('chat_template_kwargs', {}).get('enable_thinking') is False
        assert 'enable_thinking' not in body
        assert 'thinking' not in body

    def test_chat_template_kwargs_does_not_force_thinking_branch_for_other_models(self):
        """The new ``chat_template_kwargs`` thinking_format must NOT
        accidentally shadow the existing per-family branches when
        ``thinking_format`` is unset (auto-detect path)."""
        from lib.llm import build_body

        # Default-empty thinking_format on a Bailian Qwen still routes
        # through the original ``enable_thinking`` branch.
        body = build_body('qwen-plus', self.DUMMY_MSGS, max_tokens=4096,
                          thinking_enabled=True, stream=False)
        assert 'enable_thinking' in body
        assert 'chat_template_kwargs' not in body

    def test_gemini_reasoning_effort_ladder(self):
        """Gemini 3.x is a reasoning model whose depth is controlled ONLY by
        the OpenAI-style ``reasoning_effort`` string on the sankuai gateway.
        The legacy ``enable_thinking`` boolean and nested ``thinking_level``
        are silently ignored, so build_body must emit ``reasoning_effort``
        and nothing else."""
        from lib.llm import build_body

        cases = {'off': 'minimal', 'low': 'low', 'medium': 'medium',
                 'high': 'high', 'xhigh': 'high', 'max': 'high'}
        for depth, expected in cases.items():
            body = build_body('gemini-3.5-flash', self.DUMMY_MSGS,
                              max_tokens=4096,
                              thinking_enabled=(depth != 'off'),
                              thinking_depth=depth, stream=False)
            assert body.get('reasoning_effort') == expected, (depth, body)
            assert 'enable_thinking' not in body
            assert 'thinking' not in body

    def test_gemini_default_effort_medium(self):
        from lib.llm import build_body
        body = build_body('gemini-3.5-flash', self.DUMMY_MSGS, max_tokens=4096,
                          thinking_enabled=True, stream=False)
        assert body.get('reasoning_effort') == 'medium'

    def test_gpt5_reasoning_effort_ladder(self):
        """GPT-5 family is a reasoning model driven by the OpenAI-native
        ``reasoning_effort`` string. build_body maps Tofu's depth ladder onto
        the concrete generation's supported rungs. Current Codex models use
        none/low/medium/high/xhigh and GPT-5.6 adds max. No
        thinking/enable_thinking block is ever emitted for GPT."""
        from lib.llm import build_body

        cases_54 = {'off': 'none', 'low': 'low', 'medium': 'medium',
                    'high': 'high', 'xhigh': 'xhigh', 'max': 'xhigh',
                    'ultra': 'xhigh'}
        for depth, expected in cases_54.items():
            body = build_body('gpt-5.4', self.DUMMY_MSGS,
                              max_tokens=4096,
                              thinking_enabled=(depth != 'off'),
                              thinking_depth=depth, stream=False)
            assert body.get('reasoning_effort') == expected, (depth, body)

        # GPT-5.6's public top rung is max. Tofu's historical ``ultra`` label
        # is accepted only as a compatibility alias to that official value.
        cases_56 = {'off': 'none', 'low': 'low', 'medium': 'medium',
                    'high': 'high', 'xhigh': 'xhigh', 'max': 'max',
                    'ultra': 'max'}
        for depth, expected in cases_56.items():
            body = build_body('gpt-5.6', self.DUMMY_MSGS, max_tokens=4096,
                              thinking_enabled=(depth != 'off'),
                              thinking_depth=depth, stream=False)
            assert body.get('reasoning_effort') == expected, (depth, body)
            assert 'thinking' not in body
            assert 'enable_thinking' not in body

    def test_gpt5_ultra_downgrades_on_pre_56(self):
        """Tofu ``ultra`` aliases to GPT-5.4's highest ``xhigh`` rung."""
        from lib.llm import build_body
        body = build_body('gpt-5.4', self.DUMMY_MSGS, max_tokens=4096,
                          thinking_enabled=True, thinking_depth='ultra',
                          stream=False)
        assert body.get('reasoning_effort') == 'xhigh'

    def test_gpt5_default_effort_medium(self):
        from lib.llm import build_body
        body = build_body('gpt-5.6', self.DUMMY_MSGS, max_tokens=4096,
                          thinking_enabled=True, stream=False)
        assert body.get('reasoning_effort') == 'medium'

    def test_claude_ultra_maps_to_max(self):
        """``ultra`` has no Claude tier; build_body maps it to Claude's top
        rung (max) rather than dropping the effort."""
        from lib.llm import build_body
        body = build_body('claude-opus-4-8', self.DUMMY_MSGS, max_tokens=4096,
                          thinking_enabled=True, thinking_depth='ultra',
                          stream=False)
        assert body.get('effort') == 'max'

    def test_kimi_k3_reasoning_effort_ladder(self):
        """Kimi K3 takes the TOP-LEVEL ``reasoning_effort`` string
        (low/high/max, default max — official quickstart, verified live
        against the sankuai gateway 2026-07-24). Tofu's depth ladder
        collapses onto the three K3 rungs rounding UP; no thinking /
        enable_thinking block may be emitted."""
        from lib.llm import build_body

        cases = {'off': 'low', 'low': 'low', 'medium': 'high',
                 'high': 'high', 'xhigh': 'max', 'max': 'max',
                 'ultra': 'max'}
        for depth, expected in cases.items():
            body = build_body('kimi-k3', self.DUMMY_MSGS, max_tokens=4096,
                              thinking_enabled=(depth != 'off'),
                              thinking_depth=depth, stream=False)
            assert body.get('reasoning_effort') == expected, (depth, body)
            assert 'thinking' not in body
            assert 'enable_thinking' not in body

    def test_kimi_k3_never_emits_temperature(self):
        """K3 fixes temperature=1.0 — any other value is HTTP 400
        (``invalid temperature: only 1 is allowed for this model``,
        verified live 2026-07-24). build_body must OMIT temperature for
        K3 in both thinking states. Regression guard for the bug where
        depth='off' sent temperature=0.6 and every K3 request 400'd."""
        from lib.llm import build_body

        for thinking_enabled in (True, False):
            body = build_body('kimi-k3', self.DUMMY_MSGS, max_tokens=4096,
                              thinking_enabled=thinking_enabled,
                              temperature=0.6, stream=False)
            assert 'temperature' not in body, (thinking_enabled, body)
            assert 'top_p' not in body
            assert 'thinking' not in body
        # thinking off degrades to the cheapest legal rung (K3 always thinks)
        body = build_body('kimi-k3', self.DUMMY_MSGS, max_tokens=4096,
                          thinking_enabled=False, stream=False)
        assert body.get('reasoning_effort') == 'low'

    def test_kimi_k3_default_effort_high(self):
        from lib.llm import build_body
        body = build_body('kimi-k3', self.DUMMY_MSGS, max_tokens=4096,
                          thinking_enabled=True, stream=False)
        assert body.get('reasoning_effort') == 'high'

    def test_kimi_k2_shape_unchanged(self):
        """The K3 fix must not disturb the K2 line's wire shape."""
        from lib.llm import build_body

        body = build_body('kimi-k2.6', self.DUMMY_MSGS, max_tokens=4096,
                          thinking_enabled=True, stream=False)
        assert body.get('thinking') == {'type': 'enabled'}
        assert body.get('temperature') == 1.0
        assert 'reasoning_effort' not in body
        body = build_body('kimi-k2.6', self.DUMMY_MSGS, max_tokens=4096,
                          thinking_enabled=False, stream=False)
        assert body.get('thinking') == {'type': 'disabled'}
        assert body.get('temperature') == 0.6
        assert 'reasoning_effort' not in body

    def test_fable_detected_as_claude_family(self):
        """Anthropic Fable models take the Claude thinking shape."""
        from lib.llm import build_body, is_claude
        assert is_claude('fable-5')
        body = build_body('fable-5', self.DUMMY_MSGS, max_tokens=4096,
                          thinking_enabled=True, stream=False)
        assert body.get('thinking', {}).get('type') == 'adaptive'
        assert 'enable_thinking' not in body

    def test_tools_passed_through(self):
        from lib.llm import build_body

        tools = [{"type": "function", "function": {"name": "test", "parameters": {}}}]
        body = build_body('claude-sonnet-4-20250514', self.DUMMY_MSGS, max_tokens=4096,
                         tools=tools, stream=False)
        assert "tools" in body
        assert len(body["tools"]) == 1

    def test_response_format_passed_through(self):
        from lib.llm import build_body

        rf = {"type": "json_object"}
        body = build_body('claude-sonnet-4-20250514', self.DUMMY_MSGS,
                          max_tokens=4096, response_format=rf, stream=False)
        assert body["response_format"] == rf

    def test_response_format_absent_by_default(self):
        from lib.llm import build_body

        body = build_body('claude-sonnet-4-20250514', self.DUMMY_MSGS,
                          max_tokens=4096, stream=False)
        assert "response_format" not in body

    def test_extra_overrides_response_format(self):
        from lib.llm import build_body

        body = build_body('claude-sonnet-4-20250514', self.DUMMY_MSGS,
                          max_tokens=4096,
                          response_format={"type": "json_object"},
                          extra={"response_format": {"type": "text"}},
                          stream=False)
        assert body["response_format"] == {"type": "text"}

    def test_unknown_model_clamped_to_default(self):
        from lib.llm import build_body
        from lib.model_info import _DEFAULT_UNKNOWN_MAX_OUTPUT

        # An unknown model IS now clamped to the conservative default output
        # ceiling (so the first request doesn't over-ask and earn a 400),
        # rather than passing 500000 straight through.
        body = build_body("unknown-model-xyz", self.DUMMY_MSGS,
                         max_tokens=500000, stream=False)
        assert body["max_tokens"] == _DEFAULT_UNKNOWN_MAX_OUTPUT
        assert "thinking" not in body
        assert "enable_thinking" not in body

    def test_none_max_tokens_does_not_fatal_the_turn(self):
        """build_body must survive max_tokens=None (defense-in-depth).

        The killed-turn recovery crash was root-caused to resolve_conv_config
        emitting maxTokens=None with no server_defaults, which propagated to
        _clamp_max_tokens and FATALed the turn (``TypeError: '<' not supported
        between 'int' and 'NoneType'``). That None is now fixed AT SOURCE
        (resolve_conv_config coerces to 128000), but build_body/_clamp_max_tokens
        must still tolerate a None from any OTHER caller — this guards that
        backstop so the crash can never resurface via a different path."""
        from lib.llm import build_body
        from lib.model_info import _DEFAULT_UNKNOWN_MAX_OUTPUT

        body = build_body('claude-sonnet-4-20250514', self.DUMMY_MSGS,
                          max_tokens=None, stream=False)
        # Claude has an explicit 128000 ceiling; None degrades to the
        # conservative default first, which is <= that ceiling.
        assert body["max_tokens"] == _DEFAULT_UNKNOWN_MAX_OUTPUT


# ═══════════════════════════════════════════════════════════
#  2. Protocols
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestProtocols:
    """Verify protocol interfaces are properly defined."""

    def test_protocols_importable(self):
        from lib.protocols import (
            BodyBuilder,
            FetchService,
            LLMService,
            TaskEventSink,
            ToolHandler,
            TradingDataProvider,
        )
        # All should be Protocol classes
        assert hasattr(LLMService, "__protocol_attrs__") or hasattr(LLMService, "_is_protocol")

    def test_llm_service_is_runtime_checkable(self):
        from unittest.mock import MagicMock

        from lib.protocols import LLMService

        # A mock with the right methods should satisfy isinstance check
        mock = MagicMock()
        mock.chat = MagicMock()
        mock.stream = MagicMock()
        # runtime_checkable only checks method names exist
        assert isinstance(mock, LLMService)

    def test_llm_service_stream_exposes_typed_terminal_evidence(self):
        from typing import get_type_hints

        from lib.llm.stream_result import ProviderStreamResult
        from lib.protocols import LLMService

        assert get_type_hints(LLMService.stream)['return'] is ProviderStreamResult


# ═══════════════════════════════════════════════════════════
#  3. Utils
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestUtils:
    """Test utility functions."""

    def test_safe_json_valid(self):
        from lib.utils import safe_json
        assert safe_json('{"a":1}') == {"a": 1}

    def test_safe_json_invalid(self):
        from lib.utils import safe_json
        assert safe_json("not json", default={}) == {}

    def test_safe_json_none(self):
        from lib.utils import safe_json
        assert safe_json(None, default=None) is None


# ═══════════════════════════════════════════════════════════
#  4. Import validation
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
def test_all_modules_import():
    """Verify all lib/ modules import without errors."""
    from lib.tests.validate_imports import validate_imports
    assert validate_imports(), "Some lib modules failed to import"


# ═══════════════════════════════════════════════════════════
#  5. Pricing
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestPricing:
    """Test cost calculation logic."""

    def test_model_pricing_dict_exists(self):
        from lib import MODEL_PRICING
        assert isinstance(MODEL_PRICING, dict)
        assert len(MODEL_PRICING) > 0

    def test_pricing_has_common_models(self):
        from lib import MODEL_PRICING
        # At least one of these should be in pricing
        models_to_check = ["aws.claude-opus-4.6", "claude-sonnet-4-20250514",
                          "gpt-4o", "gemini-2.5-pro"]
        found = [m for m in models_to_check if m in MODEL_PRICING]
        assert len(found) > 0, f"None of {models_to_check} found in MODEL_PRICING"


# ═══════════════════════════════════════════════════════════
#  6. Thinking-format auto-detection
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestThinkingFormatDetection:
    """Verify _detect_thinking_format picks the right body shape per
    serving engine + brand combo. Critical for self-hosted endpoints —
    the same Qwen3 model needs different bodies depending on whether
    it's served by Alibaba Bailian (top-level enable_thinking) or by
    sglang / vLLM (chat_template_kwargs.enable_thinking)."""

    def test_sglang_owned_by_overrides_brand(self):
        from lib.llm_dispatch.discovery import _detect_thinking_format
        # Qwen brand normally maps to enable_thinking, but when the
        # /v1/models response declares owned_by=sglang we must switch.
        models = [{'model_id': 'qwen35-0p8b', 'owned_by': 'sglang'}]
        assert _detect_thinking_format(models, 'qwen') == 'chat_template_kwargs'

    def test_vllm_owned_by_recognized(self):
        from lib.llm_dispatch.discovery import _detect_thinking_format
        models = [{'model_id': 'glm5.1-fp8', 'owned_by': 'vllm'}]
        assert _detect_thinking_format(models, 'local') == 'chat_template_kwargs'

    def test_bailian_qwen_unaffected(self):
        from lib.llm_dispatch.discovery import _detect_thinking_format
        # Cloud Qwen via DashScope keeps the legacy top-level shape.
        models = [{'model_id': 'qwen-plus', 'owned_by': 'alibaba'}]
        assert _detect_thinking_format(models, 'qwen') == 'enable_thinking'

    def test_unknown_engine_falls_through_to_brand(self):
        from lib.llm_dispatch.discovery import _detect_thinking_format
        models = [{'model_id': 'claude-opus-5', 'owned_by': 'anthropic'}]
        assert _detect_thinking_format(models, 'claude') == 'thinking_type'

    def test_gemini_detected_as_reasoning_effort(self):
        """Gemini 3.x uses the OpenAI-style reasoning_effort string, not the
        legacy enable_thinking boolean — both brand override and name vote."""
        from lib.llm_dispatch.discovery import _detect_thinking_format
        assert _detect_thinking_format(
            [{'model_id': 'gemini-3.5-flash'}], 'gemini') == 'reasoning_effort'
        assert _detect_thinking_format(
            [{'model_id': 'gemini-3.5-flash'}], 'generic') == 'reasoning_effort'

    def test_owned_by_case_insensitive(self):
        from lib.llm_dispatch.discovery import _detect_thinking_format
        models = [{'model_id': 'qwen3-30b', 'owned_by': 'SGLang'}]
        assert _detect_thinking_format(models, 'generic') == 'chat_template_kwargs'

    def test_discover_passes_owned_by_through(self):
        """discover_models() must echo owned_by into each entry so the
        downstream detector can read it."""
        from unittest.mock import patch, MagicMock
        from lib.llm_dispatch import discovery as _disc

        fake_resp = MagicMock(ok=True, status_code=200)
        fake_resp.json.return_value = {
            'data': [{'id': 'qwen35-4b', 'owned_by': 'sglang',
                      'object': 'model'}]
        }
        with patch.object(_disc, 'http_get', return_value=fake_resp):
            out = _disc.discover_models('http://10.0.0.1:8080/v1', 'x')
        assert out, 'expected at least one model'
        assert out[0]['owned_by'] == 'sglang'

    def test_fable_discovery_capabilities_and_thinking_format(self):
        """Auto-discovery must treat Anthropic Fable as a Claude-family model
        everywhere: _infer_capabilities gives it vision+thinking (not plain
        text), and _detect_thinking_format gives it the Claude thinking_type
        shape even when the brand isn't exactly 'claude' (proxy / Bedrock).

        Regression guard for the discovery gap that would otherwise register a
        freshly-probed Fable as a text-only, non-thinking, no-vision model."""
        from lib.llm_dispatch.discovery import (
            _detect_thinking_format, _infer_capabilities,
        )
        assert _infer_capabilities('fable-5') == {'text', 'vision', 'thinking'}
        # Parity with a known-good Claude flagship.
        assert _infer_capabilities('claude-opus-4-8') >= {'text', 'vision'}
        # A gateway/Bedrock-hosted Fable whose brand isn't 'claude' still gets
        # the Claude thinking shape via the name-hint vote.
        assert _detect_thinking_format(
            [{'model_id': 'fable-5'}], 'generic') == 'thinking_type'
        assert _detect_thinking_format(
            [{'model_id': 'us.anthropic.fable-5-v1:0'}],
            'bedrock') == 'thinking_type'

    def test_kimi_k3_discovery_is_multimodal_thinking(self):
        """Auto-discovery must recognise Kimi K3 as natively multimodal
        (text + vision + VIDEO) AND always-on thinking.

        Regression guard for the discovery gap that shipped K3 as a plain
        text-only model — the K2.5/K2.6 vision regex left K3 out, and
        thinking wasn't inferable from the name (K3 hides its "always-on
        thinking" behind reasoning_effort). Both are now name-hinted so a
        freshly-probed K3 provider registers with the right caps without
        needing the DEFAULT_SLOT_CONFIGS fallback.

        Reference: platform.kimi.com/docs/guide/use-kimi-vision-model +
        platform.kimi.ai/docs/guide/kimi-k3-quickstart ('native visual
        understanding … always runs in thinking mode')."""
        from lib.llm_dispatch.discovery import _infer_capabilities
        caps = _infer_capabilities('kimi-k3')
        assert 'text' in caps
        assert 'vision' in caps, f"K3 vision missing: {caps}"
        assert 'video' in caps, f"K3 video missing: {caps}"
        assert 'thinking' in caps, f"K3 thinking missing: {caps}"
        # K2.5/K2.6 vision baseline stays intact (parity).
        assert 'vision' in _infer_capabilities('kimi-k2.6')
        assert 'vision' in _infer_capabilities('kimi-k2.5')
        # 'video' is opt-in — only kimi-k3 currently claims it via name hint.
        assert 'video' not in _infer_capabilities('kimi-k2.6')
        assert 'video' not in _infer_capabilities('claude-opus-4-8')
        assert 'video' not in _infer_capabilities('gpt-5.6')


# ═══════════════════════════════════════════════════════════
#  7. Slot.thinking_format validation
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestSlotThinkingFormat:
    """Slot is the single source of truth for legal thinking_format
    values. Construction with anything outside :data:`THINKING_FORMATS`
    must raise — silent typos here are how stream_anomaly bugs hide
    for weeks."""

    def _slot(self, **overrides):
        from lib.llm_dispatch.slot import Slot
        kwargs = dict(
            key_name='ephemeral_test', api_key='', model='qwen35-4b',
            capabilities={'text'},
        )
        kwargs.update(overrides)
        return Slot(**kwargs)

    def test_default_thinking_format_is_empty(self):
        slot = self._slot()
        assert slot.thinking_format == ''

    def test_known_values_accepted(self):
        accepted = []
        for tf in ('', 'enable_thinking', 'thinking_type', 'reasoning_effort',
                    'chat_template_kwargs', 'none'):
            accepted.append(self._slot(thinking_format=tf).thinking_format)
        assert accepted == ['', 'enable_thinking', 'thinking_type',
                            'reasoning_effort', 'chat_template_kwargs', 'none']

    def test_typo_rejected(self):
        with pytest.raises(ValueError) as excinfo:
            self._slot(thinking_format='chat_template_kwarg')
        assert 'chat_template_kwarg' in str(excinfo.value)
        assert 'chat_template_kwargs' in str(excinfo.value)

    def test_unknown_dialect_rejected(self):
        with pytest.raises(ValueError):
            self._slot(thinking_format='magic_thinking_v2')


# ═══════════════════════════════════════════════════════════
#  8. _readjust_thinking_params (dispatch path)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestReadjustThinkingParams:
    """The dispatcher's body re-adapter is the place every agent
    request passes through. It must understand all known dialects."""

    def test_chat_template_kwargs_rewrites_top_level_enable_thinking(self):
        """A body originally built for cloud Qwen carries
        ``enable_thinking: true`` at the top level. When dispatch
        rebinds it to a self-hosted slot whose ``thinking_format`` is
        ``chat_template_kwargs``, the dispatcher must (a) DROP the
        top-level field and (b) move the boolean into the
        ``chat_template_kwargs`` block. Forgetting (a) leaves a
        misleading top-level ``true`` that some sglang builds log
        loudly; forgetting (b) is the bug we shipped this PR for."""
        from lib.llm_dispatch.api import _readjust_thinking_params

        body = {'model': 'qwen35-4b', 'enable_thinking': True,
                'temperature': 0.7}
        _readjust_thinking_params(body, 'qwen35-4b', 'chat_template_kwargs')
        assert 'enable_thinking' not in body
        assert body['chat_template_kwargs']['enable_thinking'] is True

    def test_chat_template_kwargs_rewrites_disabled(self):
        from lib.llm_dispatch.api import _readjust_thinking_params

        body = {'model': 'qwen35-4b', 'enable_thinking': False}
        _readjust_thinking_params(body, 'qwen35-4b', 'chat_template_kwargs')
        assert 'enable_thinking' not in body
        assert body['chat_template_kwargs']['enable_thinking'] is False

    def test_chat_template_kwargs_rewrites_from_thinking_type(self):
        """Dispatch can swap families: a body originally built for
        Doubao (``thinking.type='enabled'``) re-routed to a self-hosted
        sglang slot must re-shape correctly."""
        from lib.llm_dispatch.api import _readjust_thinking_params

        body = {'model': 'glm5.1-fp8',
                'thinking': {'type': 'enabled'}}
        _readjust_thinking_params(body, 'glm5.1-fp8',
                                   'chat_template_kwargs')
        assert 'thinking' not in body
        assert body['chat_template_kwargs']['enable_thinking'] is True

    def test_none_strips_all_thinking_params(self):
        """``thinking_format='none'`` declares the engine doesn't
        accept any thinking flag — the body must arrive bare."""
        from lib.llm_dispatch.api import _readjust_thinking_params

        body = {'model': 'deepseek-reasoner', 'enable_thinking': True}
        _readjust_thinking_params(body, 'deepseek-reasoner', 'none')
        assert 'enable_thinking' not in body
        assert 'thinking' not in body
        assert 'chat_template_kwargs' not in body

    def test_no_thinking_params_means_noop(self):
        from lib.llm_dispatch.api import _readjust_thinking_params

        body = {'model': 'qwen35-4b', 'temperature': 0.5}
        before = dict(body)
        _readjust_thinking_params(body, 'qwen35-4b', 'chat_template_kwargs')
        # No thinking params present = nothing to rewrite.
        assert body == before

    def test_swap_to_gemini_emits_reasoning_effort(self):
        """A Claude body (thinking.adaptive + effort) re-routed to a Gemini
        slot must shed the Claude shape and carry ``reasoning_effort`` mapped
        from the original effort."""
        from lib.llm_dispatch.api import _readjust_thinking_params

        body = {'model': 'claude-sonnet-4', 'thinking': {'type': 'adaptive'},
                'effort': 'high', 'temperature': 1.0}
        _readjust_thinking_params(body, 'gemini-3.5-flash', '')
        assert body.get('reasoning_effort') == 'high'
        assert 'thinking' not in body
        assert 'enable_thinking' not in body
        assert 'effort' not in body

    def test_swap_from_gemini_to_qwen_drops_reasoning_effort(self):
        """A Gemini body (reasoning_effort) re-routed to cloud Qwen must drop
        ``reasoning_effort`` and carry the ``enable_thinking`` boolean."""
        from lib.llm_dispatch.api import _readjust_thinking_params

        body = {'model': 'gemini-3.5-flash', 'reasoning_effort': 'high'}
        _readjust_thinking_params(body, 'qwen3-max', '')
        assert 'reasoning_effort' not in body
        assert body.get('enable_thinking') is True

    def test_gemini_minimal_treated_as_thinking_off_on_swap(self):
        from lib.llm_dispatch.api import _readjust_thinking_params

        body = {'model': 'gemini-3.5-flash', 'reasoning_effort': 'minimal'}
        _readjust_thinking_params(body, 'doubao-pro', '')
        assert body.get('thinking') == {'type': 'disabled'}

    def test_swap_to_kimi_k3_emits_reasoning_effort_and_strips_temperature(self):
        """A Qwen body (enable_thinking + temperature=0.7) re-routed to K3
        must shed both — K3 rejects temperature != 1.0 with HTTP 400 and
        takes only top-level reasoning_effort."""
        from lib.llm_dispatch.api import _readjust_thinking_params

        body = {'model': 'qwen3.5-plus', 'enable_thinking': True,
                'temperature': 0.7}
        _readjust_thinking_params(body, 'kimi-k3', '')
        assert body.get('reasoning_effort') == 'high'
        assert 'enable_thinking' not in body
        assert 'thinking' not in body
        assert 'temperature' not in body

    def test_swap_to_kimi_k3_carries_effort(self):
        from lib.llm_dispatch.api import _readjust_thinking_params

        body = {'model': 'claude-sonnet-4',
                'thinking': {'type': 'adaptive'}, 'effort': 'max',
                'temperature': 1.0}
        _readjust_thinking_params(body, 'kimi-k3', '')
        assert body.get('reasoning_effort') == 'max'
        assert 'thinking' not in body
        assert 'effort' not in body
        assert 'temperature' not in body

    def test_swap_to_kimi_k3_thinking_off_degrades_to_low(self):
        from lib.llm_dispatch.api import _readjust_thinking_params

        body = {'model': 'qwen3.5-plus', 'enable_thinking': False,
                'temperature': 0.7}
        _readjust_thinking_params(body, 'kimi-k3', '')
        assert body.get('reasoning_effort') == 'low'
        assert 'temperature' not in body

    def test_swap_from_kimi_k3_to_qwen_drops_reasoning_effort(self):
        from lib.llm_dispatch.api import _readjust_thinking_params

        body = {'model': 'kimi-k3', 'reasoning_effort': 'high'}
        _readjust_thinking_params(body, 'qwen3.5-plus', '')
        assert 'reasoning_effort' not in body
        assert body.get('enable_thinking') is True

    def test_swap_to_kimi_k2_keeps_thinking_type(self):
        """K2 models re-routed via dispatch keep the thinking.type shape
        (previously the dispatcher had NO kimi branch and silently stripped
        thinking params on any swap to a kimi slot)."""
        from lib.llm_dispatch.api import _readjust_thinking_params

        body = {'model': 'qwen3.5-plus', 'enable_thinking': True}
        _readjust_thinking_params(body, 'kimi-k2.6', '')
        assert body.get('thinking') == {'type': 'enabled'}
        assert body.get('temperature') == 1.0
        assert 'enable_thinking' not in body
