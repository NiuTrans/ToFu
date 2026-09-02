"""Public agent-run request translation and terminal result projection.

This module is the transport-neutral contract shared by the full HTTP app,
the storage-free ``tofu_agent`` runtime, and the headless sidecar.  It may
depend on the agent kernel, but never on routes, authentication, billing,
storage, or a web framework.

Entry points:

* :func:`build_agent_config` translates developer-facing aliases into the
  orchestrator's configuration vocabulary.
* :func:`project_agent_result` turns a terminal task into the stable
  ``agent.run`` response shape.
"""

from __future__ import annotations

import copy
import hashlib
import time

from lib.ids import short_id
from lib.log import get_logger
from lib.turn_verdict import terminal_finish_reason

logger = get_logger(__name__)

THINKING_DEPTHS = frozenset(
    {'low', 'medium', 'high', 'xhigh', 'max', 'ultra'})

TOOL_TAG_MAP = {
    'search': ('searchMode', 'multi'),
    'search:multi': ('searchMode', 'multi'),
    'fetch': ('fetchEnabled', True),
    'memory': ('memoryEnabled', True),
    'mcp': ('mcpEnabled', True),
    'browser': ('browserEnabled', True),
    'desktop': ('desktopEnabled', True),
    'code_exec': ('codeExecEnabled', True),
    'image_gen': ('imageGenEnabled', True),
    'human_guidance': ('humanGuidanceEnabled', True),
    'scheduler': ('schedulerEnabled', True),
}

# ``tools='*'`` enables only capabilities that require no out-of-band desktop
# or human connection. Potentially dangerous execution remains opt-in.
TOOLS_ALL = {
    'searchMode': 'multi',
    'fetchEnabled': True,
    'memoryEnabled': True,
    'mcpEnabled': True,
    'codeExecEnabled': False,
    'imageGenEnabled': False,
    'humanGuidanceEnabled': False,
    'schedulerEnabled': False,
}

AGENT_EVIDENCE_VERSION = 'tofu.agent-runtime-evidence/v1'

_PUBLIC_DISPATCH_EVIDENCE_FIELDS = frozenset({
    'model', 'provider_id', 'protocol', 'responses_profile', 'latency_ms',
    'ttft_ms', 'stream_started_at_unix_ns', 'first_content_at_unix_ns',
    'stream_completed_at_unix_ns', 'attempt', '429_retries',
    'ttft_measurement',
    'upstream_429_retries', 'slot_wait_cycles', 'queue_wait_ms',
    'queue_wait_measurement',
})


def _public_usage_evidence(value):
    """Detach exact billing/timing fields while removing routing secrets."""
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    result = {
        key: copy.deepcopy(child)
        for key, child in value.items()
        if not (isinstance(key, str) and key.startswith('_wire_'))
        and key != '_dispatch'
    }
    dispatch = value.get('_dispatch')
    if isinstance(dispatch, dict):
        result['_dispatch'] = {
            key: copy.deepcopy(child)
            for key, child in dispatch.items()
            if key in _PUBLIC_DISPATCH_EVIDENCE_FIELDS
        }
    extra = result.get('_extra_billing_rounds')
    if isinstance(extra, list):
        result['_extra_billing_rounds'] = [
            ({**row, 'usage': _public_usage_evidence(row.get('usage'))}
             if isinstance(row, dict) else copy.deepcopy(row))
            for row in extra
        ]
    return result


def _public_api_round_evidence(value):
    if not isinstance(value, list):
        return []
    return [
        ({**copy.deepcopy(row),
          'usage': _public_usage_evidence(row.get('usage'))}
         if isinstance(row, dict) else {'invalidRound': True})
        for row in value
    ]


def project_agent_event_evidence(event: dict) -> dict:
    """Return a detached public event with provider-routing secrets removed."""
    if not isinstance(event, dict):
        return {}
    result = copy.deepcopy(event)
    if isinstance(result.get('usage'), dict):
        result['usage'] = _public_usage_evidence(result['usage'])
    if isinstance(result.get('apiRounds'), list):
        result['apiRounds'] = _public_api_round_evidence(result['apiRounds'])
    return result


def _set_thinking(config: dict, value) -> None:
    if isinstance(value, str) and value.lower() in THINKING_DEPTHS:
        config['thinkingEnabled'] = True
        config['thinkingDepth'] = value.lower()
    elif isinstance(value, bool):
        config['thinkingEnabled'] = value


def _set_search(config: dict, value) -> None:
    if isinstance(value, str) and value.lower() in {'off', 'multi'}:
        config['searchMode'] = value.lower()
    elif value is False:
        config['searchMode'] = 'off'
    elif value is True:
        config['searchMode'] = 'multi'


def _set_if_truthy(config: dict, key: str, value) -> None:
    if value:
        config[key] = str(value)


def _alias_setters():
    return {
        'thinking': _set_thinking,
        'search': _set_search,
        'memory': lambda c, v: c.__setitem__('memoryEnabled', bool(v)),
        'preferences': lambda c, v: c.__setitem__('preferencesEnabled', bool(v)),
        'mcp': lambda c, v: c.__setitem__('mcpEnabled', bool(v)),
        'browser': lambda c, v: c.__setitem__('browserEnabled', bool(v)),
        'desktop': lambda c, v: c.__setitem__('desktopEnabled', bool(v)),
        'code_exec': lambda c, v: c.__setitem__('codeExecEnabled', bool(v)),
        'image_gen': lambda c, v: c.__setitem__('imageGenEnabled', bool(v)),
        'human_guidance': lambda c, v: c.__setitem__(
            'humanGuidanceEnabled', bool(v)),
        'scheduler': lambda c, v: c.__setitem__('schedulerEnabled', bool(v)),
        'project': lambda c, v: _set_if_truthy(c, 'projectPath', v),
        'max_tokens': lambda c, v: c.__setitem__('maxTokens', v),
        'temperature': lambda c, v: c.__setitem__('temperature', v),
        'plugins': lambda c, v: c.__setitem__('plugins', v),
    }


def build_agent_config(
    model_id: str,
    raw_config: dict | None,
    capabilities_legacy: dict | None = None,
) -> dict:
    """Translate the public ``agent.run`` configuration into kernel config.

    Curated snake-case aliases and raw orchestrator keys may be mixed. Raw
    keys pass through for forward compatibility, while absent personal memory
    and preferences fail closed on every headless surface.
    """
    config: dict = {'model': str(model_id or '').strip()}
    merged: dict = {}
    if capabilities_legacy:
        merged.update(capabilities_legacy)
    if raw_config:
        merged.update(raw_config)

    tools = merged.pop('tools', None)
    if isinstance(tools, str) and tools in {'*', 'all'}:
        config.update(TOOLS_ALL)
    elif isinstance(tools, list):
        if tools in (['*'], ['all']):
            config.update(TOOLS_ALL)
        else:
            for tag in tools:
                mapping = TOOL_TAG_MAP.get(str(tag))
                if mapping:
                    key, value = mapping
                    config[key] = value
                else:
                    logger.debug('[agent.run] unknown tool tag: %r', tag)
    elif isinstance(tools, dict):
        # Context-efficiency switches share this namespace; the orchestrator
        # remains their validation authority.
        config['tools'] = dict(tools)
    elif tools is not None:
        logger.debug('[agent.run] ignoring unknown tools shape: %r', tools)

    for alias, setter in _alias_setters().items():
        if alias in merged:
            setter(config, merged.pop(alias))

    config.update(merged)

    from lib.agent_core.personal_scope import apply_headless_personal_defaults
    apply_headless_personal_defaults(config)
    return config


def apply_storage_free_runtime_policy(config: dict) -> dict:
    """Mark an agent config as transient and remove durable-only features.

    The public wheel does not ship a database/storage authority. Filesystem
    project tools, network tools, MCP, custom tools, and in-process swarm stay
    available; knowledge, cross-conversation state, durable memory, and the
    scheduler belong to the full application composition.
    """
    config['_storageFreeRuntime'] = True
    config['memoryEnabled'] = False
    config['schedulerEnabled'] = False
    return config


def project_agent_result(
    task: dict,
    *,
    model: str,
    requested_id: str = '',
    trajectory_fmt: str | None = None,
    byo_provider: dict | None = None,
    provider_id: str = '',
) -> dict:
    """Project a terminal task into the stable top-level ``agent.run`` shape."""
    rounds = task.get('toolRounds') or []
    last_round = rounds[-1] if rounds else None
    result: dict = {
        'id': requested_id or short_id('run-'),
        'object': 'agent.run',
        'created': int(time.time()),
        'model': model,
        'task_id': task.get('id'),
        'status': task.get('status'),
        'finish_reason': terminal_finish_reason(task),
        'content': task.get('content') or '',
        'thinking': task.get('thinking') or '',
        'usage': task.get('usage') or {},
        'n_tool_rounds': len(rounds),
    }
    if task.get('compactionUsage'):
        result['compaction_usage'] = task['compactionUsage']
    if (last_round and isinstance(last_round, dict)
            and last_round.get('tool_calls')):
        result['tool_calls'] = last_round['tool_calls']
    if task.get('error'):
        result['error'] = task['error']
    public_provider_id = provider_id or (byo_provider or {}).get('id') or ''
    if public_provider_id:
        result['provider_id'] = public_provider_id
    if trajectory_fmt:
        try:
            from lib.trajectory import flatten
            shaped = flatten(task, trajectory_fmt)
            result['trajectory_format'] = shaped['format']
            result['trajectory'] = shaped['trajectory']
        except ValueError as exc:
            logger.warning(
                '[agent.run] trajectory flatten failed fmt=%s task=%s: %s',
                trajectory_fmt, task.get('id', '?')[:8], exc)
            result['trajectory_error'] = str(exc)
    return result


def project_agent_evidence(
    task: dict,
    *,
    model: str,
    requested_id: str = '',
    provider_id: str = '',
) -> dict:
    """Project exact, bounded optimization evidence without ambient secrets.

    Raw native events remain the event authority. This projection carries the
    task-side fields that are otherwise unavailable to an embedded evaluator:
    per-call usage, context/compaction telemetry, the effective clean custom
    schema, and truth-checked orchestration adoption evidence.
    """
    config = task.get('config') if isinstance(task.get('config'), dict) else {}
    schemas = config.get('_explicitToolSchemas')
    if not isinstance(schemas, list):
        schemas = config.get('_customToolSchemas')
    if not isinstance(schemas, list):
        schemas = []
    content = str(task.get('content') or '')
    from lib.orchestration_adoption import public_orchestration_decisions

    return {
        'contractVersion': AGENT_EVIDENCE_VERSION,
        'requestId': str(requested_id or task.get('_requestId') or ''),
        'taskId': str(task.get('id') or ''),
        'model': str(model or task.get('model') or config.get('model') or ''),
        'providerId': str(provider_id or ''),
        'status': str(task.get('status') or ''),
        'finishReason': str(task.get('finishReason') or ''),
        'createdAtUnixMs': int(float(task.get('created_at') or 0) * 1000),
        'finishedAtUnixMs': int(float(task.get('finished_at') or 0) * 1000),
        'usage': _public_usage_evidence(task.get('usage') or {}),
        'apiRounds': _public_api_round_evidence(task.get('apiRounds') or []),
        'contextTelemetryRounds': copy.deepcopy(
            task.get('_contextTelemetryRounds') or []),
        'contextCompactionEvents': copy.deepcopy(
            task.get('_contextCompactionEvents') or []),
        'compactionUsage': copy.deepcopy(task.get('compactionUsage') or {}),
        'toolExposureTelemetry': copy.deepcopy(
            task.get('_toolExposureTelemetry') or {}),
        'toolSchemas': copy.deepcopy(schemas),
        'customToolsMode': str(config.get('_customToolsMode') or 'augment'),
        'programRuns': copy.deepcopy((task.get('programRuns') or [])[-64:]),
        'orchestrationDecisions': public_orchestration_decisions(task),
        'output': {
            'content': content,
            'charCount': len(content),
            'sha256': hashlib.sha256(content.encode('utf-8')).hexdigest(),
        },
    }


__all__ = [
    'AGENT_EVIDENCE_VERSION',
    'THINKING_DEPTHS',
    'TOOLS_ALL',
    'TOOL_TAG_MAP',
    'apply_storage_free_runtime_policy',
    'build_agent_config',
    'project_agent_event_evidence',
    'project_agent_evidence',
    'project_agent_result',
]
