"""Backend unit tests — no server, no browser, no network.

Tests pure logic modules:
  - lib.llm (build_body, model detection, max_tokens clamping)
  - lib.protocols (Protocol interfaces)
  - lib.pricing (cost calculation)
  - lib.utils (safe_json, etc.)
  - lib.database (schema, CRUD)
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

    def test_unknown_model_no_clamping(self):
        from lib.llm import build_body

        # An unknown model is not subject to the per-model output-ceiling
        # clamp (_clamp_max_tokens). 500000 stays well under the 1M default
        # context window, so the context-window clamp
        # (_clamp_completion_to_context_window) leaves it untouched too.
        body = build_body("unknown-model-xyz", self.DUMMY_MSGS,
                         max_tokens=500000, stream=False)
        assert body["max_tokens"] == 500000
        assert "thinking" not in body
        assert "enable_thinking" not in body


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
        for tf in ('', 'enable_thinking', 'thinking_type', 'reasoning_effort',
                    'chat_template_kwargs', 'none'):
            self._slot(thinking_format=tf)  # must not raise

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


# ═══════════════════════════════════════════════════════════
#  9. BYO provider thinking_format persistence
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestByoProviderThinkingFormat:
    """The BYO store must round-trip thinking_format and reject
    typos. This is the path that registered-suffix runs
    (``model="qwen35-0p8b@prov_xxx"``) actually take."""

    def _isolate(self, monkeypatch, tmp_path):
        """Point the BYO store at an empty tmp file + reset the cache."""
        from lib import byo_providers as _byo
        store = tmp_path / 'byo_providers.json'
        monkeypatch.setattr(_byo, '_STORE_PATH', str(store))
        # Reset the in-memory cache so reads see the fresh tmp file.
        with _byo._lock:
            _byo._cache.clear()
            _byo._cache_loaded = False
        return _byo

    def test_create_persists_thinking_format(self, monkeypatch, tmp_path):
        byo = self._isolate(monkeypatch, tmp_path)
        row = byo.create_provider(
            owner_key_id='k_test', name='sglang-cluster',
            base_url='http://10.0.0.1:8080/v1', api_key='', models=[],
            thinking_format='chat_template_kwargs',
        )
        assert row['thinking_format'] == 'chat_template_kwargs'
        # Round-trip via lookup.
        full = byo.get_provider(row['id'], 'k_test')
        assert full['thinking_format'] == 'chat_template_kwargs'

    def test_create_default_is_empty(self, monkeypatch, tmp_path):
        byo = self._isolate(monkeypatch, tmp_path)
        # Use a private IP literal (like the sibling tests) so the SSRF egress
        # guard in _validate_base_url passes without a live DNS lookup — CI
        # runners have no outbound DNS, and a real hostname here would raise
        # EgressDenied('DNS resolution failed').
        row = byo.create_provider(
            owner_key_id='k_test', name='cloud',
            base_url='http://10.0.0.1:8080/v1', api_key='', models=[],
        )
        assert row['thinking_format'] == ''

    def test_create_rejects_typo(self, monkeypatch, tmp_path):
        byo = self._isolate(monkeypatch, tmp_path)
        with pytest.raises(ValueError):
            byo.create_provider(
                owner_key_id='k_test', name='oops',
                base_url='http://10.0.0.1:8080/v1', api_key='', models=[],
                thinking_format='chat_template_kwarg',  # missing 's'
            )

    def test_update_persists_thinking_format(self, monkeypatch, tmp_path):
        byo = self._isolate(monkeypatch, tmp_path)
        row = byo.create_provider(
            owner_key_id='k_test', name='sglang',
            base_url='http://10.0.0.1:8080/v1', api_key='', models=[],
        )
        ok = byo.update_provider(row['id'], 'k_test',
                                  thinking_format='chat_template_kwargs')
        assert ok
        assert byo.get_provider(row['id'], 'k_test')['thinking_format'] == \
            'chat_template_kwargs'

    def test_update_rejects_typo(self, monkeypatch, tmp_path):
        byo = self._isolate(monkeypatch, tmp_path)
        row = byo.create_provider(
            owner_key_id='k_test', name='sglang',
            base_url='http://10.0.0.1:8080/v1', api_key='', models=[],
        )
        with pytest.raises(ValueError):
            byo.update_provider(row['id'], 'k_test',
                                 thinking_format='glm-style')
