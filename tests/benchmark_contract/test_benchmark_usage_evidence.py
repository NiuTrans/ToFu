"""Exact benchmark costs require counters; missing provider evidence is not free."""

import pytest

from lib.benchmark_contract import BenchmarkContractError, public_price_cost_from_usage


pytestmark = pytest.mark.unit
_PRICES = {
    "inputUsdPerMillion": 2,
    "cacheReadUsdPerMillion": 0.2,
    "cacheWriteUsdPerMillion": 3,
    "outputUsdPerMillion": 10,
}


@pytest.mark.parametrize("usage", [
    None, {}, {"total_tokens": 0}, {"prompt_tokens": 0}, {"completion_tokens": 0},
    {"cache_read_tokens": 10},
    {"prompt_tokens": "10", "completion_tokens": 0},
    {"prompt_tokens": 10, "completion_tokens": False},
    {"prompt_tokens": 10, "completion_tokens": 0.5},
    {"prompt_tokens": 10, "completion_tokens": 0, "effectiveCachedTokens": -1},
    {"prompt_tokens": 10, "completion_tokens": 0, "cached_content_token_count": "5"},
    {"prompt_tokens": 10, "completion_tokens": 0, "prompt_tokens_details": []},
    {"prompt_tokens": 10, "completion_tokens": 0,
     "completion_tokens_details": {"reasoning_tokens": 1}},
    {"prompt_tokens": 10, "completion_tokens": 0, "input_tokens": False},
])
def test_exact_price_rejects_unknown_or_malformed_usage(usage):
    with pytest.raises(BenchmarkContractError):
        public_price_cost_from_usage(usage, _PRICES)


@pytest.mark.parametrize("usage,total,cached,output,cost", [
    ({"prompt_tokens": 0, "completion_tokens": 0}, 0, 0, 0, 0),
    ({"input_tokens": 0, "output_tokens": 0}, 0, 0, 0, 0),
    ({"prompt_tokens": 1000, "completion_tokens": 100,
      "prompt_tokens_details": {"cached_tokens": 400}}, 1000, 400, 100, 0.00228),
    ({"input_tokens": 600, "output_tokens": 100,
      "cache_read_input_tokens": 400}, 1000, 400, 100, 0.00228),
    ({"input_tokens": 0, "output_tokens": 100,
      "cache_read_input_tokens": 1000}, 1000, 1000, 100, 0.0012),
    ({"prompt_tokens": 1000, "completion_tokens": 100,
      "cache_read_tokens": 0, "effectiveCachedTokens": 400}, 1000, 400, 100, 0.00228),
])
def test_exact_price_preserves_explicit_zero_and_provider_cache_conventions(
        usage, total, cached, output, cost):
    result = public_price_cost_from_usage(usage, _PRICES)
    assert result["promptTokens"] == total
    assert result["cacheReadTokens"] == cached
    assert result["outputTokens"] == output
    assert result["costUsd"] == pytest.approx(cost)


@pytest.mark.parametrize("cached", [0, 400, 1000])
def test_responses_retry_and_success_use_identical_inclusive_cache_semantics(cached):
    raw = {"input_tokens": 1000, "output_tokens": 100,
           "input_tokens_details": {"cached_tokens": cached},
           "output_tokens_details": {"reasoning_tokens": 20}}
    canonical = {"prompt_tokens": 1000, "completion_tokens": 100,
                 "cache_read_tokens": cached, "reasoning_tokens": 20}
    assert public_price_cost_from_usage(raw, _PRICES, usage_format="responses") == \
        public_price_cost_from_usage(canonical, _PRICES)


@pytest.mark.parametrize("details", [[], False, {"cached_tokens": 1001}, {"cached_tokens": "10"}])
def test_responses_usage_rejects_invalid_cache_subsets(details):
    with pytest.raises(BenchmarkContractError):
        public_price_cost_from_usage(
            {"input_tokens": 1000, "output_tokens": 100, "input_tokens_details": details},
            _PRICES, usage_format="responses")
