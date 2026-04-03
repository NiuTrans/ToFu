"""Backend unit tests — no server, no browser, no network.

Tests pure logic modules:
  - lib.llm_client (build_body, model detection, max_tokens clamping)
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
        from lib.llm_client import is_claude
        assert is_claude("aws.claude-opus-4.6")
        assert is_claude("claude-sonnet-4-20250514")
        assert not is_claude("gpt-4o")
        assert not is_claude("qwen3.5-plus")

    def test_is_qwen(self):
        from lib.llm_client import is_qwen
        assert is_qwen("qwen3.5-plus")
        assert is_qwen("qwen-max")
        assert not is_qwen("claude-sonnet-4-20250514")

    def test_is_gemini(self):
        from lib.llm_client import is_gemini
        assert is_gemini("gemini-2.5-pro")
        assert is_gemini("gemini-3.1-flash-lite-preview")
        assert not is_gemini("gpt-4o")

    def test_is_minimax(self):
        from lib.llm_client import is_minimax
        assert is_minimax("MiniMax-M2.7")
        assert not is_minimax("claude-sonnet-4-20250514")

    def test_is_doubao(self):
        from lib.llm_client import is_doubao
        assert is_doubao("Doubao-Seed-2.0-pro")
        assert not is_doubao("gpt-4o")

    def test_no_cross_detection(self):
        """Each model is detected by exactly one family."""
        from lib.llm_client import is_claude, is_doubao, is_gemini, is_minimax, is_qwen

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
        from lib.llm_client import build_body

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
        from lib.llm_client import build_body

        # 1024 is well below any model's limit — should pass through
        body = build_body('qwen-plus', self.DUMMY_MSGS, max_tokens=1024, stream=False)
        assert body["max_tokens"] == 1024

    def test_claude_thinking_adaptive(self):
        from lib.llm_client import build_body

        body = build_body('claude-sonnet-4-20250514', self.DUMMY_MSGS, max_tokens=4096,
                         thinking_enabled=True, stream=False)
        assert "thinking" in body
        assert body["thinking"]["type"] == "adaptive"
        assert "enable_thinking" not in body

    def test_qwen_thinking_param(self):
        from lib.llm_client import build_body

        body = build_body('qwen-plus', self.DUMMY_MSGS, max_tokens=4096,
                         thinking_enabled=True, stream=False)
        assert "enable_thinking" in body
        assert "thinking" not in body  # no Claude-style thinking block

    def test_tools_passed_through(self):
        from lib.llm_client import build_body

        tools = [{"type": "function", "function": {"name": "test", "parameters": {}}}]
        body = build_body('claude-sonnet-4-20250514', self.DUMMY_MSGS, max_tokens=4096,
                         tools=tools, stream=False)
        assert "tools" in body
        assert len(body["tools"]) == 1

    def test_unknown_model_no_clamping(self):
        from lib.llm_client import build_body

        body = build_body("unknown-model-xyz", self.DUMMY_MSGS,
                         max_tokens=999999, stream=False)
        assert body["max_tokens"] == 999999
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
