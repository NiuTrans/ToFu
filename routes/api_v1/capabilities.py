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
            'memoryEnabled': {'type': 'boolean', 'default': True,
                               'description': 'Proactive memory-store '
                               'injection. UI default ON; on the headless API '
                               '(agent/run, chat/completions, compat) it fails '
                               'CLOSED (default off) — opt in explicitly. The '
                               'search_memories/create_memory tools are '
                               'unaffected. See personal_scope.'},
            'preferencesEnabled': {'type': 'boolean',
                                    'description': "Inject the operator's "
                                    'personal preference profile. Decoupled '
                                    'from memoryEnabled. UI falls back to the '
                                    'Memory toggle; headless fails CLOSED so '
                                    'the operator\'s personal preferences are '
                                    'never spliced into an API caller\'s '
                                    'prompt. See personal_scope.'},
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
            'disableModelFallback': {
                'type': 'boolean', 'default': False,
                'description': 'Opt OUT of automatic model fallback for this '
                               'request. The fallback TARGET model is a global '
                               'server setting (admin-only); this flag only '
                               'lets a caller suppress the silent switch so a '
                               'primary-model error surfaces instead '
                               '(error envelope context="fallback-disabled").'},
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
        feats = {
            'optimizer_enabled': bool(getattr(_lib, 'OPTIMIZER_ENABLED', False)),
            'artifacts_enabled': bool(getattr(_lib, 'ARTIFACTS_ENABLED', False)),
            'pptx_translate_enabled': bool(
                getattr(_lib, 'PPTX_TRANSLATE_ENABLED', False)),
        }
        # Registered plugin flags (e.g. trading_enabled when tofu-trading is
        # installed) are added dynamically so core names no optional feature.
        try:
            from lib.feature_registry import registered_flags
            for f in registered_flags():
                feats[f.json_key] = bool(getattr(_lib, f.env_key, f.default))
        except Exception as _pe:
            logger.debug('[capabilities] plugin flags unavailable: %s', _pe)
        return feats
    except Exception as e:
        logger.debug('[capabilities] features lookup failed: %s', e)
        return {}


def _relay_summary() -> dict:
    """Auth mode + relay policy a foreign frontend uses to branch its UI.

    * ``mode``           — open / private / multi-user (the auth gate).
    * ``billing_enabled``— relay charges credits (full relay) vs
                            agent-only (users bring their own model key).
    * ``model_relay_enabled`` — tenant users may invoke the operator's
                            model slot pool (``chat``) vs BYO-only.
    * ``signup_enabled`` — public self-registration open?
    """
    out = {'mode': 'open', 'billing_enabled': True,
           'model_relay_enabled': True, 'signup_enabled': False}
    try:
        from lib.auth_mode import get_mode
        out['mode'] = get_mode()
    except Exception as e:
        logger.debug('[capabilities] auth mode unavailable: %s', e)
    try:
        from lib.relay_config import public_summary
        out.update(public_summary())
    except Exception as e:
        logger.debug('[capabilities] relay policy unavailable: %s', e)
    return out


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
        'relay': _relay_summary(),
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


@api_v1_capabilities_bp.route('/api/v1/system-prompt/default', methods=['GET'])
@api_meta(summary='Default (built-in) system prompt',
          description='Returns the freshly-built Claude-Code-style static '
                      'system prompt. Used by the Settings panel to pre-fill '
                      'the system-prompt editor so users can fine-tune or '
                      'fully replace it. Query flags: project (bool), '
                      'tools (bool) — match the preview to the user\'s mode.',
          tags=['capabilities'], public=True)
async def system_prompt_default():
    from flask import request

    def _flag(name: str, default: bool) -> bool:
        v = (request.args.get(name) or '').strip().lower()
        if not v:
            return default
        return v in ('1', 'true', 'yes', 'on')

    project = _flag('project', False)
    tools = _flag('tools', True)
    # Build a representative tool-name set so the "# Using your tools"
    # section in the preview matches what the model would really see in
    # each mode. read_files is always-on; the rest of the file/shell tools
    # only exist when project mode is on.
    _preview_tool_names: set[str] | None = None
    if tools:
        _preview_tool_names = {'web_search', 'fetch_url', 'read_files'}
        if project:
            _preview_tool_names |= {
                'write_file', 'apply_diff', 'insert_content',
                'find_files', 'grep_search', 'run_command',
            }
    try:
        from lib.tasks_pkg import system_prompt_cc
        text = system_prompt_cc.build_static_prompt(
            cwd='', is_git=False, model='',
            has_real_tools=tools,
            is_code_context=project,
            tool_names=_preview_tool_names,
            # Omit the trailing "Current date:" line — the date is injected
            # dynamically at request time, so baking it into the editor text
            # would freeze a stale date if the user saves it in replace mode.
            include_date=False,
        )
    except Exception as e:
        logger.error('[capabilities] build default system prompt failed: %s',
                     e, exc_info=True)
        return api_ok({'prompt': '', 'error': str(e)})
    return api_ok({'prompt': text, 'project': project, 'tools': tools})


@api_v1_capabilities_bp.route('/api/v1/system-prompt/blocks', methods=['GET'])
@api_meta(summary='Built-in system prompt, split into toggleable blocks',
          description='Returns the built-in static system prompt as an '
                      'ordered list of blocks (id/title/text/dynamic) for '
                      'the per-block editor. Each block carries a stable id '
                      'used to persist keep/drop toggles. Query flags: '
                      'project (bool), tools (bool) — shape the preview to '
                      'the chosen mode (some blocks only appear in code '
                      'mode or when tools are on).',
          tags=['capabilities'], public=True)
async def system_prompt_blocks():
    from flask import request

    def _flag(name: str, default: bool) -> bool:
        v = (request.args.get(name) or '').strip().lower()
        if not v:
            return default
        return v in ('1', 'true', 'yes', 'on')

    project = _flag('project', False)
    tools = _flag('tools', True)
    _preview_tool_names: set[str] | None = None
    if tools:
        _preview_tool_names = {'web_search', 'fetch_url', 'read_files'}
        if project:
            _preview_tool_names |= {
                'write_file', 'apply_diff', 'insert_content',
                'find_files', 'grep_search', 'run_command',
            }
    try:
        from lib.tasks_pkg import system_prompt_cc
        blocks = system_prompt_cc.build_static_blocks(
            cwd='', is_git=False, model='',
            has_real_tools=tools,
            is_code_context=project,
            tool_names=_preview_tool_names,
            include_date=False,  # date is dynamic; don't expose in editor
        )
    except Exception as e:
        logger.error('[capabilities] build system prompt blocks failed: %s',
                     e, exc_info=True)
        return api_ok({'blocks': [], 'error': str(e)})
    return api_ok({'blocks': blocks, 'project': project, 'tools': tools})


__all__ = ['api_v1_capabilities_bp']
