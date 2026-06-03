"""routes/api_v1/capabilities.py — Self-describing surface.

Lets a headless client discover what THIS deployment supports without
hardcoding. Inferred at request time from runtime state:

  * **models**   — discovered models from the dispatcher; capability flags
                   (text/vision/thinking/embedding/image_gen/cheap)
  * **tools**    — currently-registered tool definitions
  * **agents**   — list of agent endpoints (paper, translate, swarm, …)
  * **presets**  — supported preset/effort values
  * **backends** — agent backends available (builtin, codex, claude_code)
  * **scopes**   — closed enum of API key scopes
  * **config_schema** — JSON-schema-ish description of `cfg` fields
"""

from __future__ import annotations

from flask import Blueprint

from lib.api_response import api_ok
from lib.log import get_logger
from lib.openapi import api_meta

logger = get_logger(__name__)

api_v1_capabilities_bp = Blueprint('api_v1_capabilities', __name__)


def _models_summary() -> list[dict]:
    """Describe each chat-capable model the dispatcher can route to."""
    out: list[dict] = []
    seen: set[str] = set()
    try:
        from lib import _SAVED_CONFIG  # type: ignore
        providers = _SAVED_CONFIG.get('providers', []) or []
    except Exception as e:
        logger.debug('[capabilities] saved config unavailable: %s', e)
        providers = []

    for prov in providers:
        if not isinstance(prov, dict):
            continue
        if not prov.get('enabled', True):
            continue
        prov_id = prov.get('id') or prov.get('brand') or 'unknown'
        prov_name = prov.get('name') or prov_id
        for m in prov.get('models', []) or []:
            if not isinstance(m, dict):
                continue
            mid = m.get('model_id') or ''
            if not mid or mid in seen:
                continue
            seen.add(mid)
            caps = list(m.get('capabilities') or [])
            out.append({
                'id': mid,
                'provider': prov_id,
                'provider_name': prov_name,
                'capabilities': caps,
                'thinking': 'thinking' in caps,
                'vision': 'vision' in caps,
                'embedding': 'embedding' in caps,
                'image_gen': 'image_gen' in caps,
                'cheap': 'cheap' in caps,
                'aliases': list(m.get('aliases') or []),
                'rpm': m.get('rpm'),
                'cost_per_1k': m.get('cost'),
                'input_price_per_1m': m.get('input_price'),
                'output_price_per_1m': m.get('output_price'),
            })
    return out


def _tools_summary() -> list[dict]:
    """Describe registered native tools (server-side only)."""
    out = []
    try:
        from lib.tools import (
            BROWSER_TOOLS, CODE_EXEC_TOOL, FETCH_URL_TOOL,
            PROJECT_TOOLS, READ_FILES_TOOL,
            SEARCH_TOOL_MULTI,
        )
        groups = {
            'search': [SEARCH_TOOL_MULTI],
            'fetch': [FETCH_URL_TOOL],
            'project': [READ_FILES_TOOL] + list(PROJECT_TOOLS),
            'code_exec': [CODE_EXEC_TOOL],
            'browser': list(BROWSER_TOOLS),
        }
        for group, tools in groups.items():
            for t in tools:
                if not isinstance(t, dict):
                    continue
                fn = t.get('function') or {}
                out.append({
                    'name': fn.get('name') or t.get('name') or '',
                    'group': group,
                    'description': fn.get('description', '')[:300],
                })
    except Exception as e:
        logger.debug('[capabilities] tools enumeration failed: %s', e)
    return out


def _agents_summary() -> list[dict]:
    """Describe agent endpoints registered under /api/v1/agents/*."""
    return [
        {'id': 'paper.report', 'path': '/api/v1/agents/paper/report',
         'scope': 'agents:paper'},
        {'id': 'paper.translate', 'path': '/api/v1/agents/paper/translate',
         'scope': 'agents:paper'},
        {'id': 'translate', 'path': '/api/v1/agents/translate',
         'scope': 'agents:translate'},
        {'id': 'swarm.run', 'path': '/api/v1/agents/swarm/run',
         'scope': 'agents:swarm'},
        {'id': 'memory.search', 'path': '/api/v1/agents/memory/search',
         'scope': 'agents:memory'},
        {'id': 'browser.fetch', 'path': '/api/v1/agents/browser/fetch',
         'scope': 'agents:browser'},
        {'id': 'image-gen', 'path': '/api/v1/agents/image-gen',
         'scope': 'agents:image'},
        {'id': 'search', 'path': '/api/v1/agents/search',
         'scope': 'agents:search'},
        {'id': 'search.async', 'path': '/api/v1/agents/search/async',
         'scope': 'agents:search'},
    ]


def _config_schema() -> dict:
    """Lightweight description of supported `config` fields.

    Mirrors ``lib/tasks_pkg/model_config.py:_resolve_model_config``.
    Generated dynamically so additions automatically show up.
    """
    return {
        'type': 'object',
        'description': 'Tofu task config. All fields optional; defaults '
                       'come from server settings.',
        'properties': {
            'model': {'type': 'string',
                       'description': 'Model id (see /capabilities.models).'},
            'preset': {'type': 'string',
                        'description': "Either a model id or a legacy "
                                       "preset (opus/medium/high/xhigh/max)."},
            'thinkingDepth': {'type': 'string',
                               'enum': ['off', 'medium', 'high', 'xhigh', 'max']},
            'thinkingEnabled': {'type': 'boolean'},
            'maxTokens': {'type': 'integer', 'minimum': 1},
            'temperature': {'type': 'number'},
            'searchMode': {'type': 'string',
                            'enum': ['off', 'single', 'multi']},
            'fetchEnabled': {'type': 'boolean', 'default': True},
            'projectPath': {'type': 'string'},
            'codeExecEnabled': {'type': 'boolean'},
            'memoryEnabled': {'type': 'boolean', 'default': True},
            'browserEnabled': {'type': 'boolean'},
            'desktopEnabled': {'type': 'boolean'},
            'swarmEnabled': {'type': 'boolean'},
            'imageGenEnabled': {'type': 'boolean'},
            'humanGuidanceEnabled': {'type': 'boolean'},
            'schedulerEnabled': {'type': 'boolean'},
            'mcpEnabled': {'type': 'boolean', 'default': True},
            'agentBackend': {'type': 'string',
                              'enum': ['builtin', 'codex', 'claude_code']},
            'endpointMode': {'type': 'boolean',
                              'description': 'Planner→Worker→Critic loop'},
            'autopilot': {'type': 'boolean'},
            'fallbackModel': {'type': 'string'},
            'systemPrompt': {'type': 'string'},
            'tools': {'type': 'array', 'items': {'type': 'object'}},
        },
    }


def _events_contract() -> dict:
    """Declared streaming-event contract (the frontend↔backend sync interface).

    Sourced from ``lib.agent_core.events`` — the single registry of every
    SSE/push event the runtime emits. Lets a foreign frontend discover the
    event vocabulary without reading our JS.
    """
    try:
        from lib.agent_core.events import to_capabilities_dict
        return to_capabilities_dict()
    except Exception as e:
        logger.debug('[capabilities] events contract unavailable: %s', e)
        return {}


def _presets() -> list[str]:
    return ['off', 'medium', 'high', 'xhigh', 'max']


def _backends() -> list[str]:
    return ['builtin', 'codex', 'claude_code']


def _features() -> dict:
    try:
        import lib as _lib  # type: ignore
        return {
            'trading_enabled': bool(getattr(_lib, 'TRADING_ENABLED', False)),
            'optimizer_enabled': bool(getattr(_lib, 'OPTIMIZER_ENABLED', False)),
            'artifacts_enabled': bool(getattr(_lib, 'ARTIFACTS_ENABLED', False)),
            'pptx_translate_enabled': bool(
                getattr(_lib, 'PPTX_TRANSLATE_ENABLED', False)),
        }
    except Exception as e:
        logger.debug('[capabilities] features lookup failed: %s', e)
        return {}


@api_v1_capabilities_bp.route('/api/v1/capabilities', methods=['GET'])
@api_meta(summary='Capabilities — runtime model/tool/agent registry',
          description='Public endpoint. Use for client auto-config.',
          tags=['capabilities'], public=True)
async def capabilities():
    from lib.api_keys import ALL_SCOPES
    try:
        from lib.version import __version__ as ver
    except ImportError as e:
        logger.debug('[capabilities] lib.version unavailable: %s', e)
        ver = 'unknown'
    return api_ok({
        'tofu_version': ver,
        'api_version': 'v1',
        'features': _features(),
        'models': _models_summary(),
        'tools': _tools_summary(),
        'agents': _agents_summary(),
        'presets': _presets(),
        'backends': _backends(),
        'scopes': sorted(ALL_SCOPES),
        'config_schema': _config_schema(),
        'events': _events_contract(),
        'compat': {
            'openai_chat_completions': '/v1/chat/completions',
            'openai_models': '/v1/models',
            'anthropic_messages': '/v1/messages',
        },
        'push_channel': {'ws': '/api/push'},
    })


__all__ = ['api_v1_capabilities_bp']
