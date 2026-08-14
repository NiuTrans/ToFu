from __future__ import annotations

import re

import pytest

from lib.tasks_pkg.endpoint_prompts import PLANNER_SYSTEM_PROMPT


pytestmark = pytest.mark.unit


def test_planner_prompt_uses_evidence_not_tool_call_targets():
    prompt = PLANNER_SYSTEM_PROMPT.lower()

    assert 'bounded, batched discovery pass' in prompt
    assert 'tool-call count is not a quality target' in prompt
    assert 'do not target a number of calls' in prompt
    assert not re.search(r'\b\d+\s*[-–]\s*\d+\s+(?:targeted\s+)?tool calls\b',
                         prompt)
