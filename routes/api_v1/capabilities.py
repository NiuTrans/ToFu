"""routes/api_v1/capabilities.py — Self-describing surface.

Lets a headless client discover what THIS deployment supports without
hardcoding. Inferred at request time from runtime state:

  * **models**   — configured models (from persisted server config);
                   capability flags (vision/thinking/embedding/image_gen/
                   transcription/audio_chat/cheap)
  * **tools**    — currently-registered tool definitions
  * **agents**   — list of agent endpoints (paper, translate, swarm, …)
  * **presets**  — supported preset/effort values
  * **backends** — agent backends available (builtin, codex, claude_code)
  * **scopes**   — closed enum of API key scopes
  * **config_schema** — JSON-schema-ish description of `cfg` fields
"""

from __future__ import annotations

from quart import Blueprint

from lib.api_response import api_ok
from lib.log import get_logger
from lib.openapi import api_meta
from lib.ttl_cache import TTLCache

logger = get_logger(__name__)

api_v1_capabilities_bp = Blueprint('api_v1_capabilities', __name__)

# The capabilities payload is a pure function of the persisted server config
# plus static registries (tools/events/scopes). It's a public, poll-friendly
# endpoint, so cache the assembled dict for a short window. The cache key is
# ``id(lib._SAVED_CONFIG)``: ``lib.reload_config()`` rebinds that attribute to
# a freshly-allocated dict (built while the old one is still referenced, so the
# new id differs), which invalidates the entry immediately on a settings change.
# The 30s TTL is a backstop for any config path that doesn't route through
# reload_config().
_CAPS_CACHE = TTLCache(ttl=30.0, max_size=8, name='capabilities')

# The built-in system prompt is a pure function of (project, tools) — cwd,
# model, and the date line are fixed/omitted — so it's safe to cache on that
# 2-bool key. It never depends on server config, so a longer TTL is fine.
_PROMPT_CACHE = TTLCache(ttl=300.0, max_size=8, name='capabilities_prompt')


def _caps_cache_key() -> int:
    try:
        import lib as _lib  # type: ignore
        return id(_lib._SAVED_CONFIG)
    except Exception as e:
        logger.debug('[capabilities] cache-key resolve failed: %s', e)
        return 0


def _models_summary() -> list[dict]:
    """Describe each configured model.

    Reads the persisted server config (``lib._SAVED_CONFIG['providers']``) —
    i.e. the providers/models saved via the Settings UI or auto-discovered
    and persisted. Runtime-only dispatcher slots that were never written back
    to config do not appear here.
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
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
            identity = (str(prov_id), mid)
            if not mid or identity in seen:
                continue
            seen.add(identity)
            from lib.model_info import resolved_context_profile
            from lib.model_profiles import build_model_profile
            context = resolved_context_profile(mid, str(prov_id))
            profile = build_model_profile(
                mid, provider_id=str(prov_id), model_entry=m)
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
                'transcription': 'transcription' in caps,
                'audio_chat': 'audio_chat' in caps,
                'cheap': 'cheap' in caps,
                'aliases': list(m.get('aliases') or []),
                'context': context,
                'capability_profile': profile,
                'rpm': m.get('rpm'),
                'cost_per_1k': m.get('cost'),
                'input_price_per_1m': m.get('input_price'),
                'output_price_per_1m': m.get('output_price'),
            })
    return out


def _tools_summary() -> list[dict]:
    """Describe registered native tools (server-side only).

    Derived from the live registry inventory (``_introspect``) since
    2026-08-06 — the previous hand-maintained 5-group list had silently
    drifted from the real registry (memory/scheduler/swarm/motion/conv-ref
    families all missing). Shape kept: ``{name, group, description}`` flat
    rows; ``group`` is the spec category.
    """
    out = []
    try:
        from lib.tools.registry._introspect import build_tool_inventory
        inv = build_tool_inventory()
        for group in inv.get('groups', []):
            gid = group.get('id') or 'other'
            for fam in group.get('families', []):
                for t in fam.get('tools', []):
                    name = t.get('name') or ''
                    if not name:
                        continue
                    out.append({
                        'name': name,
                        'group': gid,
                        'description': (t.get('description') or '')[:300],
                    })
                for t in fam.get('mcp_tools', []):
                    name = t.get('name') or ''
                    if not name:
                        continue
                    out.append({
                        'name': name,
                        'group': 'mcp',
                        'description': (t.get('description') or '')[:300],
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
        # NOTE: swarm has NO /agents/swarm/run route — a swarm is launched
        # in-band via `config.swarmEnabled=true` on /chat/completions; only
        # status/abort are exposed (see routes/api_v1/agents.py swarm block).
        {'id': 'agent.run', 'path': '/api/v1/agent/run',
         'scope': 'agents:run'},
        {'id': 'audio.transcribe', 'path': '/api/v1/audio/transcribe',
         'scope': 'chat'},
        {'id': 'audio.capabilities', 'path': '/api/v1/audio/capabilities',
         'scope': 'chat'},
        {'id': 'scheduler', 'path': '/api/v1/scheduler/tasks',
         'scope': 'agents:scheduler'},
        {'id': 'mcp', 'path': '/api/v1/mcp/servers',
         'scope': 'agents:mcp'},
    ]


def _extensibility_summary() -> dict:
    """Advertise how a caller can REGISTER its own tools on this deployment.

    Two mechanisms (see docs/CUSTOM_TOOLS.md + docs/TOOL_PLUGINS.md), both
    reflected from their canonical sources so this can't drift:

    * **custom_tools** — per-request ``tools=[…]`` on ``/api/v1/agent/run``.
      The ``custom__`` name contract, the ``client``/``webhook``/``sandbox``
      execution modes (with each mode's enabled flag — ``sandbox`` reflects
      ``TOFU_CUSTOM_TOOLS_ALLOW_SANDBOX``), the per-request limits, and the
      ``POST /api/v1/tasks/{id}/tool_result`` client-handoff callback.
    * **plugins** — operator-installed ``tofu.tools`` entry-point plugins,
      listed via ``available_plugins()``, opted into per request via
      ``config.plugins``.
    """
    out: dict = {'custom_tools': {}, 'plugins': {}}
    try:
        from lib.tools.tool_env import (
            CUSTOM_TOOL_PREFIX, _NAME_RE, ToolLimits, _sandbox_allowed,
        )
        lim = ToolLimits()
        sandbox_on = _sandbox_allowed()
        out['custom_tools'] = {
            'description': 'Per-request tools the caller brings in the request '
                           'body; scoped to one task then disposed. Never '
                           'pollutes the process-global registry.',
            'submit_via': '/api/v1/agent/run',
            'request_field': 'tools',
            'scope': 'agents:run',
            'name_prefix': CUSTOM_TOOL_PREFIX,
            'name_pattern': _NAME_RE.pattern,
            'result_callback': '/api/v1/tasks/{id}/tool_result',
            'modes': {
                'client': {
                    'enabled': True,
                    'description': 'Zero-trust handoff: server emits a '
                                   'custom_tool_call event and blocks until '
                                   'the client POSTs the result back. No '
                                   'caller code runs on the server.'},
                'webhook': {
                    'enabled': True,
                    'description': 'Server POSTs args to the caller-supplied '
                                   'URL (SSRF-guarded at mint + call time).'},
                'sandbox': {
                    'enabled': bool(sandbox_on),
                    'requires_env': 'TOFU_CUSTOM_TOOLS_ALLOW_SANDBOX',
                    'description': 'Server runs a shell command (RCE) — '
                                   'operator opt-in only; disabled by '
                                   'default.'},
            },
            'limits': {
                'max_tools': lim.max_tools,
                'max_total_schema_bytes': lim.max_total_schema_bytes,
                'per_call_timeout_s': lim.per_call_timeout_s,
                'max_result_chars': lim.max_result_chars,
            },
        }
    except Exception as e:
        logger.debug('[capabilities] custom_tools summary failed: %s', e)

    try:
        from lib.tools.registry import available_plugins
        out['plugins'] = {
            'description': 'Operator-installed tofu.tools entry-point plugins. '
                           'Hidden by default on a shared server; opt in per '
                           'request.',
            'available': available_plugins(),
            'opt_in_field': 'config.plugins',
            'env_default': 'TOFU_DEFAULT_TOOL_PLUGINS',
        }
    except Exception as e:
        logger.debug('[capabilities] plugins summary failed: %s', e)
    return out


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
                            'enum': ['off', 'multi']},
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
        # Voice input (speech-to-text) — advertised as OFF when no
        # transcription-capable slot is configured, so a foreign frontend
        # hides the mic affordance rather than offering a feature that 503s.
        try:
            from lib.transcription import transcription_available
            feats['voice_input'] = bool(transcription_available())
        except Exception as _ve:
            logger.debug('[capabilities] voice_input probe failed: %s', _ve)
            feats['voice_input'] = False
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


def _build_capabilities() -> dict:
    """Assemble the full capabilities payload (uncached)."""
    from lib.api_keys import ALL_SCOPES
    from lib.model_info.capability_taxonomy import taxonomy_payload
    try:
        from lib.version import __version__ as ver
    except ImportError as e:
        logger.debug('[capabilities] lib.version unavailable: %s', e)
        ver = 'unknown'
    return {
        'tofu_version': ver,
        'api_version': 'v1',
        'relay': _relay_summary(),
        'features': _features(),
        'models': _models_summary(),
        'tools': _tools_summary(),
        'extensibility': _extensibility_summary(),
        'agents': _agents_summary(),
        'presets': _presets(),
        'backends': _backends(),
        'scopes': sorted(ALL_SCOPES),
        'config_schema': _config_schema(),
        'events': _events_contract(),
        # Single-source-of-truth capability classification. Foreign frontends
        # use ``chat_excluded_caps`` to filter model pickers; the dispatcher's
        # own ``issubset`` set is exposed for parity/debugging.
        'capability_taxonomy': taxonomy_payload(),
        'compat': {
            'openai_chat_completions': '/v1/chat/completions',
            'openai_models': '/v1/models',
            'anthropic_messages': '/v1/messages',
        },
        'push_channel': {'ws': '/api/push'},
    }


@api_v1_capabilities_bp.route('/api/v1/capabilities', methods=['GET'])
@api_meta(summary='Capabilities — runtime model/tool/agent registry',
          description='Public endpoint. Use for client auto-config.',
          tags=['capabilities'], public=True)
async def capabilities():
    payload = _CAPS_CACHE.get_or_compute(_caps_cache_key(), _build_capabilities)
    return api_ok(payload)


@api_v1_capabilities_bp.route('/api/v1/system-prompt/default', methods=['GET'])
@api_meta(summary='Default (built-in) system prompt',
          description='Returns the freshly-built Claude-Code-style static '
                      'system prompt. Used by the Settings panel to pre-fill '
                      'the system-prompt editor so users can fine-tune or '
                      'fully replace it. Query flags: project (bool), '
                      'tools (bool) — match the preview to the user\'s mode.',
          tags=['capabilities'], public=True)
async def system_prompt_default():
    from quart import request

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
                'write_file', 'edit_file',
                'find_files', 'grep_search', 'run_command',
            }
    def _build():
        from lib.tasks_pkg import system_prompt_cc
        return system_prompt_cc.build_static_prompt(
            cwd='', is_git=False, model='',
            has_real_tools=tools,
            is_code_context=project,
            tool_names=_preview_tool_names,
            # Omit the trailing "Current date:" line — the date is injected
            # dynamically at request time, so baking it into the editor text
            # would freeze a stale date if the user saves it in replace mode.
            include_date=False,
        )
    try:
        text = _PROMPT_CACHE.get_or_compute(('default', project, tools), _build)
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
    from quart import request

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
                'write_file', 'edit_file',
                'find_files', 'grep_search', 'run_command',
            }
    def _build():
        from lib.tasks_pkg import system_prompt_cc
        return system_prompt_cc.build_static_blocks(
            cwd='', is_git=False, model='',
            has_real_tools=tools,
            is_code_context=project,
            tool_names=_preview_tool_names,
            include_date=False,  # date is dynamic; don't expose in editor
        )
    try:
        blocks = _PROMPT_CACHE.get_or_compute(('blocks', project, tools), _build)
    except Exception as e:
        logger.error('[capabilities] build system prompt blocks failed: %s',
                     e, exc_info=True)
        return api_ok({'blocks': [], 'error': str(e)})
    return api_ok({'blocks': blocks, 'project': project, 'tools': tools})


__all__ = ['api_v1_capabilities_bp']
