"""Secret-free wire parameter receipts shared by model transport adapters.

Only numeric controls and fixed enums are recorded; prompts, headers, URLs,
tools and arbitrary provider strings are never copied. Missing means unknown
provider default, not an inferred effective value. Requests remain unchanged.
"""
from collections.abc import Mapping
import math


def request_parameter_receipt(request: Mapping) -> dict:
    receipt = {}
    for name in ("max_tokens", "max_completion_tokens", "temperature", "top_p"):
        value = request.get(name)
        receipt[name] = value if type(value) in (int, float) and (
            type(value) is int or math.isfinite(value)) else None
    effort = request.get("reasoning_effort")
    receipt["reasoning_effort"] = effort if isinstance(effort, str) and effort in {
        "none", "minimal", "low", "medium", "high", "xhigh", "max"} else None
    thinking = request.get("thinking")
    mode = thinking.get("type") if isinstance(thinking, dict) else None
    receipt["thinking_type"] = mode if isinstance(mode, str) and mode in {
        "enabled", "disabled", "adaptive"} else None
    return receipt
