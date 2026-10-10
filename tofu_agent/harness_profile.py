"""Explicit standalone harness policy; never a second tool parser/executor.

Registry/discovery configuration supplies the direct schemas. Terminal recovery
may consume ``classify_longcat_tool_text`` but owns its task-wide retry budget.
The observed LongCat tags do not specify a safe argument encoding or call ID,
so even apparently complete packets require native-tool reissue, not execution.
"""

from __future__ import annotations

from copy import deepcopy

from tofu_agent.models import AgentConfigurationError
from lib.llm.terminal_tool_text import classify_longcat_tool_text as classify_longcat_tool_text

HARNESS_PROFILES = ('auto', 'off', 'longcat_flash')


def validate_harness_profile(value: str) -> str:
    if value not in HARNESS_PROFILES:
        raise AgentConfigurationError(
            'harnessProfile must be auto, off or longcat_flash')
    return value


def apply_harness_profile(config: dict, default: str = 'auto') -> dict:
    """Default native exposure without overriding caller policy or authority."""
    profile = validate_harness_profile(config.get('harnessProfile', default))
    result = deepcopy(config)
    if profile == 'off':
        return result
    result['harnessProfile'] = profile
    if profile == 'auto':
        nested = result.get('tools')
        if ('tools.nativeExposure' not in result
                and not (isinstance(nested, dict) and 'nativeExposure' in nested)):
            result['tools.nativeExposure'] = 'full'
        return result
    # Use the existing registry and wire projection, without cloned schemas or
    # newly enabled capabilities. Off disables discovery, not tool execution.
    overrides = {
        'nativeExposure': 'full',
        'toolSearch': 'off',
        'programmaticCalling': 'off',
    }
    for key, value in overrides.items():
        result[f'tools.{key}'] = value
    if isinstance(result.get('tools'), dict):
        result['tools'].update(overrides)
    return result
