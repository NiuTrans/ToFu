"""routes/config.py — Server configuration API endpoints.

Extracted from routes/common.py for better separation of concerns.
Handles miscellaneous settings such as Feishu, search, proxy, and experiments.
Provider/model authority lives exclusively in ``routes.api_v1.providers``.
"""

import os
import sys

from quart import request

from lib.config_dir import config_path as _config_path
from lib.log import get_logger
from lib.api_response import (
    api_bad_request,
    api_internal_error,
    api_ok,
    safe_route,
)
from lib.request_parser import parse_body

logger = get_logger(__name__)

from routes.api_v1.config import api_v1_config_bp as config_bp  # noqa: E402
# Blueprint alias retained for the server's established registration import.

_SERVER_CONFIG_PATH = _config_path('server_config.json')

# Absorbs the settings-panel reopen / refresh burst: computing the report is
# cheap (bounded task_results projection), but every panel open fires it.
from lib.ttl_cache import TTLCache as _TTLCache  # noqa: E402
_COST_EXPERIMENT_REPORT_CACHE = _TTLCache(
    30, max_size=64, name='cost_experiment_report')


# ══════════════════════════════════════════════════════
#  Config File I/O
# ══════════════════════════════════════════════════════

def _read_server_config():
    """Read server_config.json and return as dict (empty dict on failure)."""
    from lib.json_store import read_json
    cfg = read_json(_SERVER_CONFIG_PATH, default={})
    return cfg if isinstance(cfg, dict) else {}


# ══════════════════════════════════════════════════════
#  Feishu Helpers
# ══════════════════════════════════════════════════════

def _get_feishu_config(saved_config: dict) -> dict:
    """Build Feishu config for the settings UI."""
    from lib.feishu._state import ALLOWED_USERS as ENV_ALLOWED_USERS
    from lib.feishu._state import APP_ID as ENV_APP_ID
    from lib.feishu._state import APP_SECRET as ENV_APP_SECRET
    from lib.feishu._state import DEFAULT_PROJECT_PATH as ENV_DEFAULT_PROJECT
    from lib.feishu._state import ENABLED as ENV_ENABLED
    from lib.feishu._state import WORKSPACE_ROOT as ENV_WORKSPACE_ROOT

    saved_feishu = saved_config.get('feishu', {})
    app_id = saved_feishu.get('app_id') or ENV_APP_ID
    has_secret = bool(saved_feishu.get('app_secret') or ENV_APP_SECRET)
    enabled = ENV_ENABLED or bool(app_id and has_secret)

    return {
        'enabled': enabled,
        'app_id': app_id,
        'app_id_masked': ('***' + app_id[-4:]) if len(app_id) > 4 else ('*' * len(app_id)) if app_id else '',
        'has_secret': has_secret,
        'allowed_users': saved_feishu.get('allowed_users', sorted(ENV_ALLOWED_USERS)),
        'default_project': saved_feishu.get('default_project') or ENV_DEFAULT_PROJECT,
        'workspace_root': saved_feishu.get('workspace_root') or ENV_WORKSPACE_ROOT,
        'connected': _feishu_is_connected(),
    }


def _feishu_is_connected() -> bool:
    """Check if the Feishu bot WebSocket is currently connected."""
    try:
        from lib.feishu._state import _lark_client
        return _lark_client is not None
    except Exception as _e:
        logger.debug('[Feishu] Connection check failed: %s', _e)
        return False


# ══════════════════════════════════════════════════════
#  Endpoints
# ══════════════════════════════════════════════════════

@config_bp.route('/api/v1/server-config')
def get_server_config():
    """GET — return full server configuration."""
    import lib as _lib

    logger.debug('[ServerConfig] GET /api/server-config requested')
    saved = _read_server_config()
    from lib.cost_experiments import normalize_cost_experiment_config
    cost_experiment = normalize_cost_experiment_config(
        saved.get('cost_experiment'))

    # Model/provider access is owner-scoped and served only by
    # /api/v1/model-routing. This miscellaneous settings response must never
    # regenerate providers[].models or model_catalog/v1 from migration input.
    providers = []
    presets = saved.get('presets', {})

    model_keys = [
        'LLM_MODEL', 'QWEN_MODEL',
        'GEMINI_MODEL', 'GEMINI_PRO_MODEL', 'GEMINI_PRO_PREVIEW_MODEL',
        'GEMINI_FLASH_PREVIEW_MODEL', 'MINIMAX_MODEL',
        'DOUBAO_MODEL', 'CLAUDE_SONNET_MODEL', 'IMAGE_GEN_MODEL',
    ]
    models = {}
    for k in model_keys:
        models[k] = getattr(_lib, k, '')
    if 'models' in saved:
        for k, v in saved['models'].items():
            models[k] = v

    search_info = {
        'profile': getattr(_lib, 'SEARCH_PROFILE', 'balanced'),
        'overrides': dict(getattr(_lib, 'SEARCH_OVERRIDES', {}) or {}),
        'fetch_top_n': getattr(_lib, 'FETCH_TOP_N', 6),
        'fetch_timeout': getattr(_lib, 'FETCH_TIMEOUT', 15),
        'max_chars_search': getattr(_lib, 'FETCH_MAX_CHARS_SEARCH', 60000),
        'max_chars_direct': getattr(_lib, 'FETCH_MAX_CHARS_DIRECT', 200000),
        'max_chars_pdf': getattr(_lib, 'FETCH_MAX_CHARS_PDF', 0),
        'max_bytes': getattr(_lib, 'FETCH_MAX_BYTES', 20 * 1024 * 1024),
        'skip_domains': sorted(getattr(_lib, 'SKIP_DOMAINS', set())),
        'llm_content_filter': getattr(_lib, 'LLM_CONTENT_FILTER_ENABLED', True),
        'deepen_enabled': getattr(_lib, 'SEARCH_DEEPEN_ENABLED', False),
    }
    if 'search' in saved:
        search_info.update(saved['search'])
        # Apply saved llm_content_filter on config load (page refresh / startup)
        if 'llm_content_filter' in saved['search']:
            _lib.LLM_CONTENT_FILTER_ENABLED = bool(saved['search']['llm_content_filter'])
            from lib.search_runtime import sync_search_config_if_loaded
            sync_search_config_if_loaded()

    # Live backend search status (tofu-search version / engines / extension
    # reachability / filter model+mode) — the piece that lets the Settings UI
    # show what the backend will ACTUALLY do, not just the saved knobs.
    try:
        from lib.search_settings import status_payload as _ss_status
        from routes.api_v1.auth import current_auth
        search_status = _ss_status(
            owner_user_id=current_auth().owner_user_id)
    except Exception as _e:
        logger.warning('[ServerConfig] search status unavailable: %s', _e)
        search_status = {'ok': False, 'error': str(_e)}

    server_info = {
        'Python': sys.version.split()[0],
        'Config Path': _SERVER_CONFIG_PATH,
        'Model Routing': '/api/v1/model-routing',
    }
    feishu_info = _get_feishu_config(saved)

    # Provider/model authority is returned independently by model-routing v2.
    # Keep these empty compatibility projections until all rolling clients stop
    # reading the miscellaneous settings payload for their initial shape.
    dropdown_models = []
    model_folds = {}
    provider_pricing = {}
    hidden_models = saved.get('hidden_models', [])
    hidden_ig_models = saved.get('hidden_ig_models', [])

    # Settings converts canonical prices only for presentation. The bounded
    # USD-pivot card lets the browser render without owning exchange rates.
    try:
        from lib.pricing import get_model_price_display_policy
        model_price_display = get_model_price_display_policy()
    except Exception as e:
        logger.warning('[ServerConfig] price display policy unavailable: %s', e)
        model_price_display = {
            'base_currency': 'USD', 'usd_rates': {'USD': 1.0},
            'updated_at': 0, 'source': 'unavailable',
        }

    model_pricing = {
        model_name: {
            'input': info.get('input', 0),
            'output': info.get('output', 0),
            'name': info.get('name', model_name),
        }
        for model_name, info in getattr(_lib, 'MODEL_PRICING', {}).items()
    }

    model_limits = saved.get('model_limits', {})
    model_context_limits = saved.get('model_context_limits', {})
    model_defaults = {
        'fallback_model': getattr(_lib, 'FALLBACK_MODEL', ''),
        'default_model': getattr(_lib, 'LLM_MODEL', ''),
    }
    model_defaults.update(saved.get('model_defaults', {}))

    from lib.proxy import get_proxy_config
    _pc = get_proxy_config()
    network_info = {
        'http_proxy': _pc['http_proxy'],
        'https_proxy': _pc['https_proxy'],
        'env_http_proxy': _pc['env_http_proxy'],
        'env_https_proxy': _pc['env_https_proxy'],
        'proxy_configured': _pc['configured'],
        'proxy_bypass_domains': saved.get('proxy_bypass_domains', []),
        'env_proxy_bypass': os.environ.get('PROXY_BYPASS_DOMAINS', ''),
    }
    try:
        from lib.proxy import get_proxy_pool
        network_info['proxy_pool'] = get_proxy_pool()
    except Exception as _e:
        logger.debug('get server config: proxy pool view failed (%s)', _e)
        network_info['proxy_pool'] = []
    try:
        from lib.netpath import status_summary as _np_status
        network_info['netpath'] = _np_status()
    except Exception as _e:
        logger.debug('get server config: failed (%s)', _e)
        pass

    # Machine translation provider config
    mt_provider_cfg = getattr(_lib, 'MT_PROVIDER_CONFIG', {})
    mt_provider_info = {
        'provider': mt_provider_cfg.get('provider', 'niutrans'),
        'api_url': mt_provider_cfg.get('api_url', ''),
        'api_key': mt_provider_cfg.get('api_key', ''),
        'app_id': mt_provider_cfg.get('app_id', ''),
        'enabled': mt_provider_cfg.get('enabled', False),
    }

    # Upload-shrink policy — single source of truth for image re-encode rules.
    # Frontend compressImage() reads this to mirror the backend exactly, so
    # the browser doesn't double-shrink what the server would have kept.
    try:
        from routes.upload import get_upload_policy
        upload_policy = get_upload_policy()
    except Exception as e:
        logger.warning('[ServerConfig] upload policy unavailable: %s', e)
        upload_policy = {}

    # Context-window policy — single source of truth for the Context Health
    # Bar (static/js/context-bar.js). The bar used to hard-code a copy of the
    # limit table + compaction thresholds, which silently drifted from the
    # Python constants (e.g. 0.82 vs the real 0.90). Now it reads these.
    # v2 Offering rows carry their own admission ceilings. This compatibility
    # policy therefore has no provider/model projection of its own.
    try:
        from lib.tasks_pkg.compaction.api import build_context_policy
        context_policy = build_context_policy()
        context_policy['per_model'] = {}
    except Exception as e:
        logger.warning('[ServerConfig] context policy unavailable: %s', e)
        context_policy = {}

    # Translation policy — single source of truth for the frontend's
    # stale-partial-translation heuristic (was hard-coded 0.15 in
    # static/js/translation.js). See lib/text_lang.stale_translation_policy().
    try:
        from lib.text_lang import stale_translation_policy
        translation_policy = stale_translation_policy()
    except Exception as e:
        logger.warning('[ServerConfig] translation policy unavailable: %s', e)
        translation_policy = {}

    # Language-detection cascade policy — single source of truth for the
    # Tier-1→Tier-2 escalation thresholds. See lib/text_lang.detect_language_policy().
    try:
        from lib.text_lang import detect_language_policy
        lang_detect_policy = detect_language_policy()
    except Exception as e:
        logger.warning('[ServerConfig] lang-detect policy unavailable: %s', e)
        lang_detect_policy = {}

    # Capability classification (single source of truth) — the frontend
    # reads this at boot to filter chat-model pickers, so ASR-only /
    # image-gen / embedding models don't leak into the model dropdown.
    try:
        from lib.model_info.capability_taxonomy import taxonomy_payload
        capability_taxonomy = taxonomy_payload()
    except Exception as e:
        logger.warning('[ServerConfig] capability taxonomy unavailable: %s', e)
        capability_taxonomy = {}

    # Compact MCP inventory piggybacks on this already-required first-screen
    # request.  The bridge projection reads only its cached names/counts: no
    # tools/list round trip and no description/schema copies.  This replaces
    # the browser's former second startup request to /api/v1/mcp/tools.
    try:
        from lib.mcp import get_bridge
        mcp_tool_summary = get_bridge().get_enabled_tool_summary()
    except Exception as e:
        logger.debug('[ServerConfig] MCP tool summary unavailable: %s', e)
        mcp_tool_summary = {'servers': [], 'total': 0}

    # Deployment feature flags are already needed by the first screen. Carry
    # the same live projection as GET /features so the browser does not follow
    # this expensive config response with a second request. The legacy endpoint
    # remains a rolling-deploy/failure-isolation fallback.
    try:
        from lib.features_store import feature_flags_snapshot
        feature_flags = feature_flags_snapshot()
    except Exception as e:
        logger.debug('[ServerConfig] feature flags unavailable: %s', e)
        feature_flags = {}

    return api_ok({
        'providers': providers, 'presets': presets,
        'models': models, 'search': search_info,
        'search_status': search_status,
        'server_info': server_info,
        'feishu': feishu_info,
        'mcp_tool_summary': mcp_tool_summary,
        'feature_flags': feature_flags,
        'dropdown_models': dropdown_models,
        'model_folds': model_folds,
        'hidden_models': hidden_models,
        'hidden_ig_models': hidden_ig_models,
        'provider_pricing': provider_pricing,
        'model_pricing': model_pricing,
        'model_price_display': model_price_display,
        'model_limits': model_limits,
        'model_context_limits': model_context_limits,
        'model_defaults': model_defaults,
        'network': network_info,
        'mt_provider': mt_provider_info,
        'upload': upload_policy,
        'context': context_policy,
        'translation': translation_policy,
        'langDetect': lang_detect_policy,
        'capability_taxonomy': capability_taxonomy,
        'cost_experiment': cost_experiment,
    })


@config_bp.route('/api/v1/experiments/capabilities')
@safe_route
def experiment_capabilities():
    """Return callback-free metadata for installed experiment plugins."""
    from lib.experiments import registry

    return api_ok(registry().catalog())


@config_bp.route('/api/v1/cost-experiments/report')
@safe_route
async def cost_experiment_report():
    """Aggregate persisted, provider-priced outcomes for the visible A/B UI.

    The data source is the per-task ``metadata.costExperiment`` outcome that
    the terminal persist already writes into ``task_results`` — a compact
    projection — rather than the retired full-transcript conversation scan
    (N+1 conversation loads in legacy mode; one event-loop-stalling,
    frame-cap-busting ``conversation.list(include_messages=True)`` RPC in
    sidecar mode, which was also BLIND to turn-native v2 conversations).
    Conversation remains the sampling unit. The bounded row cap protects
    the settings request on unusually large installations and is disclosed
    in the response instead of silently presenting a partial report as
    complete.
    """
    import asyncio
    import time

    from lib.cost_experiments import (
        aggregate_cost_experiment_rows,
        normalize_cost_experiment_config,
        task_outcome_report_rows,
    )
    from lib.cost_experiment_repository import scan_cost_experiment_outcomes
    from routes.api_v1.auth import request_user_id

    try:
        days = int(request.args.get('days', 14))
    except (TypeError, ValueError):
        return api_bad_request('days must be an integer between 1 and 90')
    if days < 1 or days > 90:
        return api_bad_request('days must be between 1 and 90')

    exp = normalize_cost_experiment_config(
        _read_server_config().get('cost_experiment'))
    owner_id = request_user_id()
    cache_key = (owner_id, days, exp['enabled'], exp.get('lifecycle'),
                 exp.get('started_at_ms'), exp.get('sealed_at_ms'),
                 exp['experiment_id'], exp.get('spec_digest'))
    cached = _COST_EXPERIMENT_REPORT_CACHE.get(cache_key)
    if cached is not None:
        return api_ok(cached)

    now_ms = int(time.time() * 1000)
    cutoff_ms = int(exp.get('started_at_ms') or 0) \
        or (now_ms - days * 86_400_000)
    row_cap = 5_000
    invalid_records = 0

    # The bounded repository walk stays off the event loop. Each semantic RPC
    # advances a durable-record cursor while keeping heavy task content and
    # thinking inside the authority.
    result = await asyncio.to_thread(
        scan_cost_experiment_outcomes,
        user_id=owner_id,
        completed_at_gte=cutoff_ms,
        experiment_id=exp['experiment_id'],
        limit=row_cap + 1,
    )
    result = result if isinstance(result, dict) else {}
    records = result.get('records') or []
    invalid_records += int(result.get('invalid') or 0)
    truncated = bool(result.get('capped')) or len(records) > row_cap
    report_rows, invalid = task_outcome_report_rows(records[:row_cap])
    report = await asyncio.to_thread(
        aggregate_cost_experiment_rows,
        report_rows,
        experiment_id=exp['experiment_id'],
        days=days,
        now_ms=now_ms,
        min_sample_size=exp['min_sample_size'],
        experiment_spec=exp.get('spec'),
        analysis_closed=exp.get('lifecycle') == 'sealed',
        analysis_start_ms=exp.get('started_at_ms') or 0,
        analysis_sealed_ms=exp.get('sealed_at_ms') or 0,
        truncated=truncated,
        source_invalid_rows=invalid + invalid_records,
    )
    report['enabled'] = exp['enabled']
    report['lifecycle'] = exp.get('lifecycle')
    if exp.get('invalid_reason'):
        report['configurationError'] = exp['invalid_reason']
    report['rowCap'] = row_cap
    report['source'] = 'task_results'
    _COST_EXPERIMENT_REPORT_CACHE.set(cache_key, report)
    return api_ok(report)


@config_bp.route('/api/v1/feishu/status')
@safe_route
def feishu_status():
    """Return Feishu bot runtime status.

    @safe_route ( batch 5): the ad-hoc "except Exception →
    api_internal_error(e)" wrap around the body was a pure logger.warning
    + api_internal_error with no distinct context / side effects; the
    decorator reproduces it via fn.__qualname__.
    """
    from lib.feishu._state import (
        ALLOWED_USERS,
        APP_ID,
        APP_SECRET,
        active_user_count,
        DEFAULT_PROJECT_PATH,
        ENABLED,
        WORKSPACE_ROOT,
    )
    active_users = active_user_count()
    return api_ok({
        'enabled': ENABLED,
        'connected': _feishu_is_connected(),
        'app_id_masked': ('***' + APP_ID[-4:]) if len(APP_ID) > 4 else '',
        'has_secret': bool(APP_SECRET),
        'active_users': active_users,
        'allowed_users': sorted(ALLOWED_USERS),
        'default_project': DEFAULT_PROJECT_PATH,
        'workspace_root': WORKSPACE_ROOT,
    })


def _hot_reload_feishu(feishu_data: dict):
    """Hot-apply GUI-saved Feishu configuration through its state owner."""

    try:
        import lib.feishu._state as state
        from lib.feishu.startup import is_bot_running, start_bot

        credentials_changed = state.apply_config(feishu_data)
        if state.ENABLED and not is_bot_running():
            start_bot()
        elif credentials_changed and is_bot_running():
            logger.warning(
                '[Feishu] Credentials changed; the connected bot keeps the '
                'old app until the server restarts')
        logger.info(
            '[Feishu] Hot-applied config: enabled=%s, app_id=%s, running=%s',
            state.ENABLED,
            ('***' + state.APP_ID[-4:]) if len(state.APP_ID) > 4 else '(empty)',
            is_bot_running(),
        )
    except Exception as error:
        logger.warning('[Feishu] Hot-apply failed: %s', error, exc_info=True)


@config_bp.route('/api/v1/network/proxy-test', methods=['POST'])
def test_network_proxy():
    """POST — live-probe ONE proxy entry against the subscription canaries.

    Body: ``{url, scope, credential_vault?, id?, name?}``. The complete URL
    may include userinfo; legacy structured auth fields remain accepted but
    are not part of the Settings product model.
    Works on UNSAVED rows (the Settings test button fires before saving):
    an inline ``credential`` (or URL userinfo) is used for this probe only
    and never persisted; a bare ``credential_vault`` reference resolves
    through the vault. Returns ``{ok, results: [{target, label, status,
    latency_ms, verdict}]}`` — 403 from the canary = geo/policy block,
    407 = the proxy itself rejected our credential (proxy_auth), any other
    HTTP answer = the app layer was reached.
    """
    data = parse_body()
    from lib.proxy import sanitize_proxy_pool, test_proxy_entry
    entries, creds, err = sanitize_proxy_pool([{
        'id': data.get('id') or 'probe',
        'name': data.get('name') or 'probe',
        'url': data.get('url') or '',
        'credential': data.get('credential') or '',
        'username': data.get('username') or '',
        'password': data.get('password') or '',
        'credential_vault': data.get('credential_vault') or '',
        'clear_credential': bool(data.get('clear_credential', False)),
        'scope': data.get('scope') or 'subscription',
        'enabled': True,
    }])
    if err or not entries:
        return api_bad_request('proxy-test: %s' % (err or 'empty entry'))
    result = test_proxy_entry(entries[0], credential=creds.get(entries[0]['id']))
    # 'ok' belongs to the envelope — the diagnostic verdict rides 'any_ok'.
    return api_ok({'any_ok': bool(result.get('ok')),
                   'results': result.get('results') or [],
                   'error': result.get('error') or ''})


@config_bp.route('/api/v1/server-config', methods=['POST'])
def save_server_config():
    """POST — save server configuration changes.

    All settings take effect immediately (hot-reload) — no server restart needed.
    The flow: write config to disk → reload_config() updates module-level vars →
    reset_dispatcher() rebuilds LLM slot pool → hot-reload Feishu/proxy/etc.
    """
    import lib as _lib
    from lib.log import audit_log
    from lib.json_store import update_json_atomic
    data = parse_body()
    if any(key in data for key in ('providers', 'model_catalog', 'models')):
        return api_bad_request(
            'providers[].models, server-config models, and model_catalog/v1 '
            'were removed as write authorities; use '
            '/api/v1/model-routing with revision CAS',
            error_kind='legacy_model_routing_state_removed',
        )
    changes = []
    dispatch_reset_needed = False

    cost_experiment_prep = None
    from lib.cost_experiments import CostExperimentTransitionError
    if 'cost_experiment' in data:
        try:
            from lib.cost_experiments import normalize_cost_experiment_config
            cost_experiment_prep = normalize_cost_experiment_config(
                data['cost_experiment'], strict=True)
        except ValueError as e:
            return api_bad_request('cost_experiment: %s' % e)

    # ── Bypass-list pre-processing ──
    # Validate every side-effect-free network input first. Otherwise a request
    # containing a valid new proxy secret plus an invalid bypass rule could
    # write the vault secret and then return 400.
    bypass_prep = None
    if 'proxy_bypass_domains' in data:
        from lib.proxy import sanitize_bypass_domains
        bypass_prep, bypass_err = sanitize_bypass_domains(
            data['proxy_bypass_domains'])
        if bypass_err:
            return api_bad_request('proxy_bypass_domains: %s' % bypass_err)

    # ── Proxy pool pre-processing (OUTSIDE the atomic mutator) ──
    # Sanitize + split URL userinfo into the credentials vault BEFORE the
    # config-lock write: vault CRUD is a side effect that must not run
    # inside the mutator. ``pool_prep`` is the sanitized persisted shape.
    pool_prep = None
    pool_old_credential_ids = set()
    if 'proxy_pool' in data:
        from lib.proxy import sanitize_proxy_pool
        entries, creds, err = sanitize_proxy_pool(data['proxy_pool'])
        if err:
            return api_bad_request('proxy_pool: %s' % err)
        try:
            from lib.credentials_vault import set_entry as _vault_set
            for _pid, _secret in creds.items():
                _vault_set('proxy_%s_auth' % _pid, _secret,
                           note='proxy pool credential')
        except ValueError as e:
            return api_bad_request('proxy_pool credential: %s' % e)
        pool_prep = entries
        try:
            _prior = _read_server_config()
            for _entry in (_prior.get('proxy_pool') or []):
                if not isinstance(_entry, dict) or not _entry.get('id'):
                    continue
                _pid = _entry['id']
                if _entry.get('credential_vault') == 'proxy_%s_auth' % _pid:
                    pool_old_credential_ids.add(_pid)
        except Exception as e:
            logger.debug('[ServerConfig] prior proxy_pool read failed: %s', e)

    # The whole read-modify-write runs inside ONE ``update_json_atomic``
    # mutator so it is serialised (per-path thread lock + cross-process
    # flock) against the background writers of this SAME file
    # (context_limits / model_info learned-state writers).
    # Reading at the top and writing at the bottom — as this used to do —
    # would make each individual write atomic but still lose a concurrent
    # writer's update that landed in the read→write gap. The mutator sees
    # the FRESH on-disk config under the lock, so learned model_limits /
    # context limits added meanwhile are preserved. Interleaved
    # side-effects (feishu/proxy/search hot-reload) keep their original
    # positions so behaviour is unchanged.
    def _mutate(existing):
        nonlocal dispatch_reset_needed
        if not isinstance(existing, dict):
            existing = {}
        final_cost_experiment = cost_experiment_prep

        # Validate before any other config mutation or hot-reload side effect.
        # One experiment ID must describe one stable routing shape; otherwise
        # an admin slider change could move an existing conversation to the
        # other arm and contaminate both samples.
        if cost_experiment_prep is not None:
            from lib.cost_experiments import validate_cost_experiment_transition
            final_cost_experiment = validate_cost_experiment_transition(
                existing.get('cost_experiment'), cost_experiment_prep)

        if 'presets' in data and isinstance(data['presets'], dict):
            existing['presets'] = data['presets']
            changes.append('presets')
            dispatch_reset_needed = True

        if 'search' in data and isinstance(data['search'], dict):
            existing['search'] = data['search']
            # LLM content filter is a separate module-level flag
            if 'llm_content_filter' in data['search']:
                _lib.LLM_CONTENT_FILTER_ENABLED = bool(data['search']['llm_content_filter'])
                from lib.search_runtime import sync_search_config_if_loaded
                sync_search_config_if_loaded()
                logger.info('[Config] LLM content filter → %s', _lib.LLM_CONTENT_FILTER_ENABLED)
            changes.append('search.*')

        if 'hidden_models' in data and isinstance(data['hidden_models'], list):
            existing['hidden_models'] = data['hidden_models']
            changes.append('hidden_models')

        if 'hidden_ig_models' in data and isinstance(data['hidden_ig_models'], list):
            existing['hidden_ig_models'] = data['hidden_ig_models']
            changes.append('hidden_ig_models')

        if 'model_defaults' in data and isinstance(data['model_defaults'], dict):
            existing['model_defaults'] = data['model_defaults']
            md = data['model_defaults']
            if md.get('default_model'):
                existing.setdefault('presets', {})['opus'] = md['default_model']
                existing.setdefault('models', {})['LLM_MODEL'] = md['default_model']
            existing.setdefault('models', {})['fallback_model'] = md.get('fallback_model', '')
            changes.append('model_defaults')
            dispatch_reset_needed = True

        if bypass_prep is not None:
            existing['proxy_bypass_domains'] = bypass_prep
            from lib.proxy import set_bypass_domains
            set_bypass_domains(bypass_prep)
            changes.append('proxy_bypass_domains')

        if 'proxy_config' in data and isinstance(data['proxy_config'], dict):
            pc = data['proxy_config']
            existing['proxy_config'] = {
                'http_proxy': (pc.get('http_proxy') or '').strip(),
                'https_proxy': (pc.get('https_proxy') or '').strip(),
            }
            existing['proxy_config'].pop('no_proxy', None)
            from lib.proxy import set_proxy_config
            set_proxy_config(
                http_proxy=existing['proxy_config']['http_proxy'],
                https_proxy=existing['proxy_config']['https_proxy'],
            )
            changes.append('proxy_config')

        if pool_prep is not None:
            existing['proxy_pool'] = pool_prep
            # The pool editor owns proxying once used — retire the legacy
            # single-proxy slot so both never apply at once (the pool's
            # 'global' rows replace it; env vars remain the last fallback).
            if existing.get('proxy_config') and any(
                    existing['proxy_config'].get(k)
                    for k in ('http_proxy', 'https_proxy')):
                existing['proxy_config'] = {'http_proxy': '', 'https_proxy': ''}
                from lib.proxy import set_proxy_config
                set_proxy_config()
                changes.append('proxy_config retired (migrated to pool)')
            from lib.proxy import set_proxy_pool
            set_proxy_pool(pool_prep)
            changes.append('proxy_pool (%d entries)' % len(pool_prep))

        if 'feishu' in data and isinstance(data['feishu'], dict):
            existing['feishu'] = data['feishu']
            changes.append('feishu')
            # Hot-reload Feishu state
            _hot_reload_feishu(data['feishu'])

        if 'mt_provider' in data and isinstance(data['mt_provider'], dict):
            mt = data['mt_provider']
            existing['mt_provider'] = {
                'provider': (mt.get('provider') or 'niutrans').strip(),
                'api_url': (mt.get('api_url') or '').strip(),
                'api_key': (mt.get('api_key') or '').strip(),
                'app_id': (mt.get('app_id') or '').strip(),
                'enabled': bool(mt.get('enabled', False)),
            }
            changes.append('mt_provider')
            logger.info('[Config] MT provider updated: provider=%s, enabled=%s',
                        existing['mt_provider']['provider'],
                        existing['mt_provider']['enabled'])

        if final_cost_experiment is not None:
            from lib.cost_experiments import normalize_cost_experiment_config
            prior_cost_experiment = normalize_cost_experiment_config(
                existing.get('cost_experiment'))
            if final_cost_experiment != prior_cost_experiment:
                existing['cost_experiment'] = final_cost_experiment
                changes.append(
                    'cost_experiment (lifecycle=%s traffic=%d%% optimized=%d%%)'
                    % (final_cost_experiment['lifecycle'],
                       final_cost_experiment['traffic_percent'],
                       final_cost_experiment['treatment_percent']))

        return existing

    # ── Persist to disk (locked read-modify-write) ──
    try:
        update_json_atomic(_SERVER_CONFIG_PATH, _mutate, default={})
        logger.info('[ServerConfig] Saved server_config.json')
    except CostExperimentTransitionError as e:
        logger.warning('[ServerConfig] rejected cost experiment transition: %s',
                       e)
        return api_bad_request('cost_experiment: %s' % e)
    except Exception as e:
        logger.error('[ServerConfig] Failed to write config file to %s: %s',
                     _SERVER_CONFIG_PATH, e, exc_info=True)
        return api_internal_error('Failed to write config file')

    # Vault hygiene: credentials die when their row is removed OR when the
    # user explicitly removes authentication from an existing row.
    if pool_prep is not None:
        try:
            from lib.credentials_vault import delete_entry as _vault_del
            new_credential_ids = {
                e['id'] for e in pool_prep
                if e.get('credential_vault') == 'proxy_%s_auth' % e['id']
            }
            for _rid in sorted(pool_old_credential_ids - new_credential_ids):
                _vault_del('proxy_%s_auth' % _rid)
        except Exception as e:
            logger.warning('[ServerConfig] stale proxy credential sweep failed: %s', e)

    # ── Hot-reload: update all module-level variables from disk ──
    try:
        _lib.reload_config()
    except Exception as e:
        logger.error('[ServerConfig] reload_config() failed: %s', e, exc_info=True)

    # ── Reset dispatcher if provider/model config changed ──
    if dispatch_reset_needed:
        try:
            from lib.llm_dispatch import reset_dispatcher
            reset_dispatcher()
            logger.info('[ServerConfig] Dispatcher reset — new config active immediately')
        except Exception as e:
            logger.warning('[ServerConfig] Dispatcher reset failed: %s', e, exc_info=True)

    if changes:
        audit_log('server_config_change', changes=changes)
        logger.info('[ServerConfig] Config changes applied (hot-reload): %s', changes)

    return api_ok({'needs_restart': False, 'changes': changes})
