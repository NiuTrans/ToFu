"""Route-independent capability description for the headless runtime."""

from __future__ import annotations

from lib.agent_core.run_contract import (
    THINKING_DEPTHS, TOOLS_ALL, TOOL_TAG_MAP,
)


def runtime_capabilities(runtime=None) -> dict:
    try:
        from lib.version import __version__ as version
    except ImportError:  # pragma: no cover
        version = 'unknown'
    default_model = str(getattr(runtime, 'default_model', '') or '')
    provider = getattr(runtime, 'provider', None)
    return {
        'tofu_version': version,
        'api_version': 'v1',
        'runtime': 'tofu-agent',
        'features': {
            'agent_run': True,
            'streaming': True,
            'task_replay': True,
            'abort': True,
            'idempotency': True,
            'custom_tools': True,
            'custom_tools_modes': ['augment', 'exclusive'],
            'trajectory_export': True,
            'database': False,
            'frontend': False,
            'durable_memory': False,
            'durable_scheduler': False,
            'cross_conversation_state': False,
            'provider_setup_ui': True,
        },
        'state': {
            'authority': 'process-memory',
            'survives_restart': False,
            'resume_scope': 'current process lifetime',
        },
        'provider': {
            'modes': ['managed-default', 'runtime-default', 'request-override'],
            'required_fields': ['base_url', 'model'],
            'optional_fields': ['api_key', 'extra_headers', 'thinking_format'],
            'api_key_may_be_empty_for_local_engines': True,
            'configured': provider is not None,
            'source': str(getattr(runtime, 'provider_source', '') or 'runtime'),
            'default': provider.public_dict() if provider is not None else None,
        },
        'models': ([{'id': default_model, 'default': True}]
                   if default_model else []),
        'config_schema': {
            'thinking': sorted(THINKING_DEPTHS),
            'tool_tags': sorted(
                tag for tag in TOOL_TAG_MAP
                if tag not in {'memory', 'scheduler'}),
            'durable_only_tool_tags': ['memory', 'scheduler'],
            'tools_all_defaults': dict(TOOLS_ALL),
            'raw_orchestrator_keys': 'pass-through',
        },
        'events': {
            'cursor': 'absolute next sequence',
            'terminal': ['done', 'error', 'aborted'],
            'common': [
                'phase', 'delta', 'delta_reset', 'tool_start', 'tool_result',
                'tool_output', 'retry', 'done', 'error', 'aborted',
            ],
        },
    }


__all__ = ['runtime_capabilities']
