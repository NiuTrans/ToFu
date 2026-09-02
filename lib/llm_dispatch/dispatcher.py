"""lib/llm_dispatch/dispatcher.py — LLMDispatcher: slot pool management and selection.

Manages a pool of (key, model) Slots, builds them from environment config +
benchmark data, and provides the slot-picking algorithms (best single, top-N,
best-for-model, etc.).
"""

import json
import os
import threading
import time
from dataclasses import dataclass

from lib.log import get_logger
from lib.model_info.capability_taxonomy import DISPATCHER_NON_CHAT_CAPS

from .config import (
    DEFAULT_SLOT_CONFIGS,
    MANAGED_TIER_TAGS,
    MODEL_ALIASES,
    get_pricing_tiers,
)
from .conv_affinity import (
    get_conv_affinity,
    get_preferred_key,
    record_conv_key,
    sticky_routing_enabled,
)
from .model_entry import resolve_request_ids, routing_group
from .openai_provider import normalize_official_openai_provider
from .provider_face import DEFAULT_FACE, dual_face_hosts, resolve_face
from .slot import Slot

logger = get_logger(__name__)

__all__ = [
    'LLMDispatcher',
]


@dataclass(frozen=True)
class _SharedContentionProbeState:
    """Process-local admission state for one shared provider/model project."""

    strikes: int
    last_strike_at: float
    next_probe_at: float
    recovery_successes: int = 0


@dataclass(frozen=True)
class _SharedContentionAdmission:
    """One bounded wait slice and whether it owns the following probe."""

    delay_s: float
    admitted: bool


def _oauth_wire_protocol(provider: dict) -> str:
    """The wire protocol a subscription backend ACTUALLY speaks.

    The Codex backend accepts ONLY the Responses API, so an oauth='codex'
    provider's effective protocol is 'responses' regardless of what a
    stored config says (entries written before  carry
    'openai', a lie the old URL-sniff gate papered over). Returns '' for
    non-codex providers — no coercion.
    """
    if isinstance(provider, dict) and (provider.get('oauth') or '') == 'codex':
        return 'responses'
    return ''


class LLMDispatcher:
    """Manages a pool of (key, model) slots and picks the best one per request."""

    def __init__(self):
        self.slots: list[Slot] = []
        self._initialized = False
        self._lock = threading.Lock()
        # name → logical model_id, built ONLY from each entry's own
        # {model_id} ∪ wire ids — never merged across entries/providers.
        # Owner directive 2026-08-06: routing keys on model_id alone; config
        # aliases are wire spellings, NOT routing widenings. Rebuilt by
        # _build_logical_index during slot build.
        self._logical_index: dict[str, str] = {}
        self._direct_models: set = set()
        # Shared-project retry admission is scoped to provider + model because
        # rotating API keys inside one upstream project cannot create TPM.
        self._contention_strikes: dict[
            tuple[str, str], _SharedContentionProbeState
        ] = {}
        # Model entries REFUSED at slot-build time because their wire face
        # could not be resolved safely (e.g. a Claude model on a dual-face
        # gateway whose provider declares no anthropic face). Surfaced to the
        # UI — a refusal the user cannot see is its own silent failure.
        self.face_refusals: list[dict] = []

    def initialize(self):
        """Build slot pool from env vars + benchmark data. Idempotent."""
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            self._build_slots()
            self._load_benchmark_data()
            self._initialized = True
            logger.info('Initialized %d slots:', len(self.slots))
            for s in self.slots:
                caps = ','.join(sorted(s.capabilities))
                logger.debug('  %s:%s rpm=%.0f '
                      'lat=%.0fms caps=[%s]', s.key_name, s.model, s.rpm_limit, s.latency_ema, caps)

    # Legacy env-var model names are only useful when the base URL matches
    # the default (i.e. no custom provider has been configured).
    _DEFAULT_BASE_URL = 'https://api.openai.com/v1'

    def _build_slots(self):
        """Create slots from configured keys × models.

        Priority:
          1. server_config.json providers (multi-provider model, Settings UI)
          2. Auto-discovery via /v1/models (when endpoint is non-default)
          3. Legacy env-var config (env-var model names, fallback)

        Auto-discovery ensures that a friend deploying with their own endpoint
        (e.g. YEYSAI, OpenRouter) gets working slots without running migrate.py.
        """
        # ── Benchmark / multi-tenant fail-loud mode ──
        # When set, build NO operator-curated slots at all. The only way to
        # dispatch is then an inline `provider` block / @prov_xxx BYO
        # endpoint (ephemeral slots, injected at request time). This is
        # defense-in-depth on top of the per-task provider pin: with the
        # operator's Keys/Providers store absent there is literally no key
        # to leak onto, so an isolation bug fails LOUDLY (clean "no slot"
        # error) instead of silently consuming shared internal quota.
        if os.environ.get('TOFU_DISABLE_CONFIGURED_SLOTS', '').strip().lower() \
                in ('1', 'true', 'yes', 'on'):
            logger.warning('[Dispatch] TOFU_DISABLE_CONFIGURED_SLOTS set — '
                           'building 0 operator slots; only inline-provider / '
                           'BYO ephemeral slots can serve requests')
            return

        # Always re-read config from disk — the module-level
        #   _SAVED_CONFIG is a stale snapshot from server startup that
        #   misses providers added via the Settings UI.
        from lib import _load_server_config
        fresh_config = _load_server_config()

        saved_providers = fresh_config.get('providers', [])
        # Only use saved providers if they have nested models
        has_saved = saved_providers and any(
            p.get('models') for p in saved_providers if p.get('enabled', True)
        )

        if has_saved:
            self._migrate_provider_extra_headers(saved_providers, fresh_config)
            self._migrate_duplicate_account_faces(saved_providers)
            self._build_slots_from_providers(saved_providers)
        else:
            # Non-default endpoint → auto-discover models from /v1/models
            #   instead of using hardcoded model names that may not be available
            from lib import LLM_API_KEY, LLM_BASE_URL
            is_default = (LLM_BASE_URL == self._DEFAULT_BASE_URL)

            if not is_default and LLM_API_KEY:
                discovered = self._try_auto_discover(LLM_BASE_URL, LLM_API_KEY)
                if discovered:
                    self._build_slots_from_providers(discovered)
                    self._persist_discovered_config(discovered)
                    return

            # Fallback: env-var model names
            self._build_slots_from_env()

    def _try_auto_discover(self, base_url: str, api_key: str) -> list:
        """Attempt model auto-discovery from provider API.

        Returns a providers list suitable for _build_slots_from_providers,
        or [] on failure.
        """
        try:
            from lib import LLM_API_KEYS
            from lib.llm_dispatch.discovery import discover_models

            models = discover_models(base_url, api_key)
            if not models:
                logger.warning('[Dispatch] Auto-discovery returned no models '
                              'for %s — falling back to env config', base_url)
                return []

            # Build a single provider entry
            provider = {
                'id': 'default',
                'name': 'Auto-discovered',
                'base_url': base_url,
                'api_keys': list(LLM_API_KEYS),
                'enabled': True,
                'models': models,
            }

            n_cheap = sum(1 for m in models if 'cheap' in m.get('capabilities', []))
            logger.info('[Dispatch] Auto-discovered %d models (%d cheap) '
                       'from %s', len(models), n_cheap, base_url)
            return [provider]

        except Exception as e:
            logger.warning('[Dispatch] Auto-discovery failed for %s: %s',
                          base_url, e, exc_info=True)
            return []

    def _persist_discovered_config(self, providers: list):
        """Save auto-discovered provider config to server_config.json.

        This ensures discovery only happens once — subsequent restarts
        use the saved config (which the user can then edit in Settings).
        """
        try:
            from lib import _SERVER_CONFIG_PATH
            from lib.json_store import update_json_atomic

            def _mutate(config):
                if not isinstance(config, dict):
                    config = {}
                config['providers'] = providers
                # Don't set presets — let the system use the first model
                if 'presets' not in config:
                    config['presets'] = {}
                return config

            update_json_atomic(_SERVER_CONFIG_PATH, _mutate, default={})
            logger.info('[Dispatch] Saved auto-discovered config to %s',
                       _SERVER_CONFIG_PATH)
        except Exception as e:
            logger.warning('[Dispatch] Failed to persist discovered config: %s',
                          e, exc_info=True)

    # ── Known provider header migrations ──
    # When custom headers were moved from hardcoded _headers() to per-provider
    # extra_headers, existing saved providers need the headers injected.
    # Populated from the EXTRA_HEADER_MIGRATIONS env var (JSON object
    # mapping host-suffix → header dict) so internal infrastructure
    # routing details never leak into open-source builds.
    @staticmethod
    def _load_header_migrations() -> dict:
        raw = os.environ.get('EXTRA_HEADER_MIGRATIONS', '').strip()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning('[Dispatch] Invalid EXTRA_HEADER_MIGRATIONS JSON: %s', e)
            return {}

    _HEADER_MIGRATIONS = None  # lazy-loaded in _migrate_provider_extra_headers

    def _migrate_duplicate_account_faces(self, providers):
        """Fold pre-separation duplicate cards of ONE account into one card.

        Before ``base_url``/``protocol`` became per-model, a gateway account
        exposing two wire protocols had to be written as TWO provider entries
        sharing one set of API keys (your-llm-gateway.example.com: ``/v1/openai/native``
        + ``/v1/anthropic``). This collapses such a pair into a single card
        whose alternate face lives under ``faces{}``.

        Runs at load time for EVERY install rather than being a hand-edit of
        one machine's config: otherwise other installs keep the duplicate
        card AND, once the second template file is gone, that card can never
        match a template again (``_findMatchingTemplate`` keys on exact
        base_url), so "sync from template" silently dies for it.

        Persists once, mirroring ``_migrate_provider_extra_headers``.
        """
        from lib.llm_dispatch.provider_face import merge_duplicate_account_faces

        try:
            changed = merge_duplicate_account_faces(providers)
        except Exception as e:
            logger.error('[Dispatch] account-face merge failed, leaving '
                         'providers untouched: %s', e, exc_info=True)
            return
        if not changed:
            return

        try:
            from lib import _SERVER_CONFIG_PATH
            from lib.json_store import update_json_atomic

            def _mutate(cfg):
                if not isinstance(cfg, dict):
                    cfg = {}
                cfg['providers'] = providers
                return cfg

            update_json_atomic(_SERVER_CONFIG_PATH, _mutate, default={})
            logger.info('[Dispatch] Persisted account-face merge to %s',
                        _SERVER_CONFIG_PATH)
        except Exception as e:
            logger.warning('[Dispatch] Failed to persist account-face merge '
                           '(in-memory merge still applied): %s', e)

    def _migrate_provider_extra_headers(self, providers, config):
        """Auto-inject extra_headers for known providers missing them.

        This is a one-time migration for providers saved before the
        per-provider extra_headers feature was added. Persists the
        updated config so migration only runs once.
        """
        migrations = type(self)._HEADER_MIGRATIONS
        if migrations is None:
            migrations = self._load_header_migrations()
            type(self)._HEADER_MIGRATIONS = migrations
        if not migrations:
            return
        migrated = False
        for p in providers:
            base_url = p.get('base_url', '')
            existing_hdrs = p.get('extra_headers') or {}
            if existing_hdrs:
                continue  # already has headers — skip
            for domain_suffix, headers in migrations.items():
                if domain_suffix in base_url:
                    p['extra_headers'] = dict(headers)
                    migrated = True
                    logger.info('[Dispatch] Auto-migrated extra_headers for '
                               'provider %s (matched %s)',
                               p.get('id', '?'), domain_suffix)
                    break

        if migrated:
            # Persist so migration only runs once (locked RMW so a concurrent
            # Settings save isn't clobbered).
            try:
                from lib import _SERVER_CONFIG_PATH
                from lib.json_store import update_json_atomic

                def _mutate(cfg):
                    if not isinstance(cfg, dict):
                        cfg = {}
                    cfg['providers'] = providers
                    return cfg

                update_json_atomic(_SERVER_CONFIG_PATH, _mutate, default={})
                logger.info('[Dispatch] Persisted extra_headers migration '
                           'to %s', _SERVER_CONFIG_PATH)
            except Exception as e:
                logger.warning('[Dispatch] Failed to persist extra_headers '
                              'migration: %s', e)

    def _build_slots_from_providers(self, providers):
        """Build slots from saved multi-provider config (server_config.json).

        Each provider has its own base_url, api_keys, and model list.
        Slots = provider.api_keys × (provider.models + their aliases).

        Local providers (``brand == 'local'``) may carry a list of endpoint
        URLs in ``endpoints: [str, ...]`` instead of (or in addition to) the
        single ``base_url``. We fan out slots across every endpoint × model ×
        key combination so the dispatcher load-balances across the fleet.
        """
        self._direct_models = set()
        entry_groups: list[tuple[str, set]] = []
        # Derived ONCE per build: gateway hosts known to offer an
        # Anthropic-native face (read from the shipped templates). Passed
        # explicitly into resolve_face so the per-model call is a pure
        # function of already-computed state.
        _dual_face_hosts = dual_face_hosts()
        self.face_refusals = []
        from lib.llm_dispatch.discovery import normalize_base_url, should_bypass_proxy
        from lib.model_registration import clear_provider_models
        from lib.proxy import register_no_proxy_url

        for raw_provider in providers:
            provider = normalize_official_openai_provider(raw_provider)
            prov_id = provider.get('id', 'default')
            clear_provider_models(prov_id)
            if not provider.get('enabled', True):
                continue

            base_url = provider.get('base_url', '')
            api_keys = provider.get('api_keys', [])
            prov_extra_headers = provider.get('extra_headers') or {}
            prov_thinking_format = provider.get('thinking_format', '')
            prov_oauth = provider.get('oauth', '')
            # Subscription-adapter marker (CLIProxyAPI sidecar on the user's
            # desktop agent). Mirrors lib.desktop.adapter.is_adapter_provider
            # without importing the desktop layer here.
            _raw_adapter = provider.get('adapter')
            prov_adapter = dict(_raw_adapter) if isinstance(_raw_adapter, dict) else {}

            # A subscription backend's wire protocol is a fact of the
            # backend, not a user setting: the Codex backend speaks ONLY
            # Responses. Stored configs from the URL-sniff era carry
            # protocol:'openai' — coerce IN MEMORY (not persisted) so the
            # single api_protocol gate in lib/llm/_sse_core.py is always
            # fed the truth. New provisions write 'responses' outright
            # (lib/oauth/outbound.py _MANAGED_SPECS).
            _forced_proto = _oauth_wire_protocol(provider)
            if _forced_proto and (provider.get('protocol') or '') != _forced_proto:
                provider = dict(provider, protocol=_forced_proto)

            # ── Multi-endpoint expansion for local providers ──
            # Backwards-compatible: when 'endpoints' is absent we fall back
            # to the single base_url (existing behavior).
            raw_endpoints = provider.get('endpoints') or []
            if isinstance(raw_endpoints, list) and raw_endpoints:
                endpoint_urls = []
                seen = set()
                for url in raw_endpoints:
                    if not isinstance(url, str):
                        continue
                    norm = normalize_base_url(url.strip())
                    if not norm or norm in seen:
                        continue
                    seen.add(norm)
                    endpoint_urls.append(norm)
                if not endpoint_urls and base_url:
                    endpoint_urls = [base_url]
            else:
                endpoint_urls = [base_url] if base_url else []

            # ── Per-endpoint served-model binding ──
            # ``endpoint_models: {url: [root_model_id, ...]}`` is written by
            # the Settings probe and by the local health checker. Self-hosted
            # engines (vLLM / SGLang) serve exactly ONE model per URL, so
            # fanning every model out to every endpoint misroutes requests
            # into upstream 404s. An endpoint ABSENT from the map (or mapped
            # to a falsy list) has no probe data → legacy union fan-out; a
            # non-empty entry NARROWS that endpoint to the listed root ids.
            endpoint_binding = {}
            raw_binding = provider.get('endpoint_models')
            if isinstance(raw_binding, dict):
                for bk, bv in raw_binding.items():
                    if not isinstance(bk, str) or not isinstance(bv, list):
                        continue
                    bn = normalize_base_url(bk.strip())
                    if bn:
                        endpoint_binding[bn] = {x for x in bv
                                                if isinstance(x, str) and x}

            # Self-hosted endpoints sit on private (or pseudo-private) IPs
            # that corp HTTP proxies can't reach. Pre-register them for
            # proxy bypass so the very first request out of the gate goes
            # direct. Covers brand=='local' AND any bare-IP endpoint (a
            # raw-IP base URL is in practice always self-hosted, including
            # internal-but-publicly-routable corp ranges like 33.x that
            # is_local_endpoint can't classify). No-op for cloud providers.
            for url in endpoint_urls:
                if provider.get('brand') == 'local' or should_bypass_proxy(url):
                    register_no_proxy_url(url)

            # Local providers without keys are still valid (vLLM/SGLang/Ollama
            # typically run without auth). Treat empty key as a single
            # blank-key slot so the dispatcher still creates entries.
            effective_keys = api_keys or ([''] if provider.get('brand') == 'local' else [])
            if not effective_keys:
                logger.warning('[Dispatch] Provider %s has no API keys, skipping', prov_id)
                continue

            keys = [(f'{prov_id}_key_{i}', k) for i, k in enumerate(effective_keys)]

            # Collect models + their aliases for this provider
            for raw_model_entry in provider.get('models', []):
                # Every source (templates, Settings, discovery, BYO API) enters
                # through one model contract. Keep the old blended `cost` only
                # as an in-memory migration fallback; it is deliberately not
                # copied into the canonical row or returned to the frontend.
                legacy_cost = (raw_model_entry.get('cost')
                               if isinstance(raw_model_entry, dict) else None)
                try:
                    from lib.model_registration import register_model
                    model_entry = register_model(
                        raw_model_entry, provider_id=prov_id)
                except ValueError as exc:
                    logger.error('[Dispatch] Invalid model registration in '
                                 'provider %s: %s', prov_id, exc)
                    continue
                model_id = model_entry.get('model_id', '')
                if not model_id:
                    continue

                # Skip user-disabled models (per-model toggle in Settings).
                # The entry stays in server_config.json so re-enabling
                # preserves aliases / RPM / pricing.
                if model_entry.get('enabled') is False:
                    logger.debug('[Dispatch] Skipping disabled model %s in provider %s',
                                 model_id, prov_id)
                    continue

                # ── Identity for THIS entry ──
                # ``entry_group`` = {logical model_id} ∪ every wire id
                # (entry-level and per-cell) — see lib/llm_dispatch/model_entry.py.
                # The logical id is a member even when it never goes on the
                # wire, which is what lets a preset / picker name stay stable
                # while the gateway renames its deployments underneath.
                # Computed here because the endpoint-binding check below
                # consumes it.
                entry_group = routing_group(model_entry)

                # ── Wire FACE for THIS model (account/face separation) ──
                # base_url + protocol are per-MODEL, not per-provider: one
                # gateway account can expose several wire faces (the YourProvider
                # gateway speaks OpenAI on /v1/openai/native and Anthropic on
                # /v1/anthropic with the SAME keys). resolve_face owns the
                # decision — including the fail-loud refusal that stops a
                # Claude model from silently dispatching over a wire that
                # drops thinking-block signatures. See provider_face.py.
                face = resolve_face(provider, model_entry,
                                    dual_face_hosts=_dual_face_hosts)
                if not face.ok:
                    logger.error('[Dispatch] Model %s in provider %s NOT '
                                 'registered: %s', model_id, prov_id, face.error)
                    self.face_refusals.append({
                        'provider_id': prov_id,
                        'model_id': model_id,
                        'error': face.error,
                    })
                    continue
                if face.forced:
                    logger.warning('[Dispatch] Model %s in provider %s is '
                                   'pinned to face %r, overriding the family '
                                   'default', model_id, prov_id, face.face_name)

                # ── Endpoint pool for THIS model (wire-id binding check) ──
                # A probe reports what ``/v1/models`` lists, i.e. WIRE ids. The
                # logical ``model_id`` may not be one of them, so an endpoint
                # qualifies when it serves ANY id in this entry's group. Keying
                # this on the root id alone would drop every split-identity
                # entry on a bound local fleet. An empty pool means no probed
                # endpoint serves this model → honest absence (no slots) beats
                # guaranteed 404s.
                # A non-default face names exactly ONE URL, so it replaces the
                # endpoint fan-out (which exists for local multi-endpoint
                # fleets and is meaningless for a gateway's alternate face).
                if face.face_name != DEFAULT_FACE:
                    ep_pool = [face.base_url]
                elif endpoint_binding:
                    ep_pool = [u for u in (endpoint_urls or [face.base_url])
                               if not endpoint_binding.get(u)
                               or (entry_group & set(endpoint_binding[u]))]
                else:
                    ep_pool = endpoint_urls or [face.base_url]
                if not ep_pool:
                    logger.info('[Dispatch] Model %s skipped: no bound endpoint '
                                'serves it in provider %s', model_id, prov_id)
                    continue

                self._direct_models.add(model_id)

                # Parse capabilities
                caps_raw = model_entry.get('capabilities', ['text'])
                caps = set(caps_raw) if isinstance(caps_raw, list) else {'text'}
                rpm = model_entry.get('rpm', 30)
                latency = model_entry.get('latency', 2000)

                # Merge with DEFAULT_SLOT_CONFIGS for any missing fields
                default_cfg = DEFAULT_SLOT_CONFIGS.get(model_id, {})
                # ``stream_only`` is part of the provider model-entry
                # contract (managed Codex writes it on every model).  The old
                # slot builder consulted only DEFAULT_SLOT_CONFIGS, silently
                # dropping the persisted value and making each non-streaming
                # summary learn the same fact again via an upstream HTTP 400.
                model_stream_only = model_entry.get(
                    'stream_only', default_cfg.get('stream_only', False))
                from lib.model_registration import routing_cost_per_1k
                cost = routing_cost_per_1k(
                    model_entry, provider_id=prov_id,
                    legacy_fallback=(legacy_cost if legacy_cost is not None
                                     else default_cfg.get('cost')))
                if not caps_raw or caps_raw == ['text']:
                    caps = set(default_cfg.get('caps', caps))
                if rpm == 30 and 'rpm' in default_cfg:
                    rpm = default_cfg['rpm']

                # ── Per-(key, model) capability matrix ──
                # ``model_entry['key_access']`` maps a key index (as a string)
                # to a partial override dict:
                #   { "0": {"enabled": false},
                #     "1": {"rpm": 10, "aliases": [...], "capabilities": [...]} }
                # An absent index inherits the model-level defaults (the
                # historical behavior — every key gets every model). A present
                # entry overrides only the fields it names. Setting
                # ``enabled: false`` disables just that (key, model) cell,
                # leaving the model active for the other keys.
                key_access = model_entry.get('key_access') or {}

                # Publish this entry's identity set (logical model_id + its
                # wire ids) so a stored/wire name resolves back to THIS
                # entry's model_id. Intra-entry only — the index builder
                # never unions across entries (owner directive 2026-08-06).
                entry_groups.append((model_id, entry_group))

                for key_idx, (key_name, api_key) in enumerate(keys):
                    cell = key_access.get(str(key_idx)) or {}
                    if cell.get('enabled') is False:
                        logger.debug('[Dispatch] Model %s disabled for key #%d '
                                     'in provider %s', model_id, key_idx, prov_id)
                        continue

                    cell_caps = cell.get('capabilities')
                    cell_rpm = cell.get('rpm', rpm)
                    cell_cost = cell.get('cost', cost)
                    cell_stream_only = cell.get(
                        'stream_only', model_stream_only)

                    # ── Wire-id pool for this (entry, key) ──
                    # The ids actually sent as the ``"model"`` field. One slot
                    # per id, so the dispatcher rotates across interchangeable
                    # gateway deployments. ``disabled_ids`` (applied inside the
                    # resolver) subtracts concrete ids this key must not serve —
                    # each id can be a genuinely different upstream model, so a
                    # key may keep the root reachable while a dead deployment is
                    # dropped, or vice-versa.
                    all_ids = resolve_request_ids(model_entry, cell)
                    if not all_ids:
                        logger.debug('[Dispatch] Model %s has an empty wire pool '
                                     'for key #%d in provider %s — no slots',
                                     model_id, key_idx, prov_id)
                        continue

                    for mid in all_ids:
                        # Check DEFAULT_SLOT_CONFIGS for alias-specific overrides.
                        # Precedence: alias_cfg > cell override > model default.
                        alias_cfg = DEFAULT_SLOT_CONFIGS.get(mid, {})
                        if cell_caps is not None:
                            # Explicit per-cell capability set wins outright.
                            slot_caps = set(cell_caps)
                        else:
                            slot_caps = set(alias_cfg.get('caps', caps))
                        slot_rpm = alias_cfg.get('rpm', cell_rpm)
                        slot_cost = routing_cost_per_1k(
                            model_entry, provider_id=prov_id,
                            wire_model_id=mid,
                            legacy_fallback=alias_cfg.get('cost', cell_cost))
                        slot_lat = alias_cfg.get('latency', latency)

                        # Auto-tag managed pricing tiers ('cheap' + any future
                        # PRICING_TIERS rows) from real pricing data.
                        # Skip non-chat models — tier tags don't apply, and a
                        # spurious 'cheap' tag on a non-chat slot (e.g. a cheap
                        # 'transcription' model) would make it chat-pickable
                        # because {transcription,cheap} is no longer a subset of
                        # _NON_CHAT_CAPS.
                        if not (slot_caps & self._NON_CHAT_CAPS):
                            tiers = get_pricing_tiers(mid, fallback_cost_per_1k=slot_cost)
                            # Strip stale managed tags, then apply desired tier tags.
                            slot_caps -= (MANAGED_TIER_TAGS - tiers)
                            slot_caps |= tiers

                        # Precedence mirrors every other per-slot field:
                        # concrete wire alias > key/model cell > model entry >
                        # shipped default.  This keeps an explicit provider
                        # capability intact all the way to dispatch_chat's
                        # pre-exclusion gate.
                        slot_stream_only = alias_cfg.get(
                            'stream_only', cell_stream_only)

                        # One slot per (endpoint × key). For non-local providers
                        #   ep_pool collapses to a single entry, preserving
                        #   the historical N-key-only behavior.
                        slot_endpoints = ep_pool
                        for ep_idx, ep_url in enumerate(slot_endpoints):
                            # Distinguish key_names per endpoint so the slot pool
                            # has stable identifiers and per-key cooldowns don't
                            # clobber each other across endpoints.
                            ep_suffix = f'_ep{ep_idx}' if len(slot_endpoints) > 1 else ''
                            slot = Slot(
                                key_name=key_name + ep_suffix,
                                api_key=api_key,
                                model=mid,
                                logical_model=model_id,
                                capabilities=slot_caps,
                                base_url=ep_url,
                                provider_id=prov_id,
                                extra_headers=dict(prov_extra_headers),
                                thinking_format=prov_thinking_format,
                                protocol=face.protocol,
                                responses_profile=face.responses_profile,
                                oauth=prov_oauth,
                                adapter=dict(prov_adapter),
                                rpm_limit=slot_rpm,
                                latency_ema=slot_lat,
                                cost_per_1k_tokens=slot_cost,
                                stream_only=slot_stream_only,
                            )
                            self.slots.append(slot)

        self._build_logical_index(entry_groups)

        logger.info('[Dispatch] Built %d slots from %d saved providers '
                    '(%d direct models)',
                    len(self.slots),
                    sum(1 for p in providers if p.get('enabled', True)),
                    len(self._direct_models))

    def _build_slots_from_env(self):
        """Build slots from legacy env-var config (fallback when no server_config.json)."""
        # Late-import to avoid circular-import NameError during early boot
        # (lib/__init__.py may not have finished when dispatcher is first loaded)
        from lib import (
            CLAUDE_SONNET_MODEL as _claude_sonnet,
        )
        from lib import (
            DOUBAO_MODEL as _doubao,
        )
        from lib import (
            GEMINI_FLASH_PREVIEW_MODEL as _gemini_flash_prev,
        )
        from lib import (
            GEMINI_MODEL as _gemini,
        )
        from lib import (
            GEMINI_PRO_MODEL as _gemini_pro,
        )
        from lib import (
            GEMINI_PRO_PREVIEW_MODEL as _gemini_pro_prev,
        )
        from lib import (
            IMAGE_GEN_MODEL as _image_gen,
        )
        from lib import (
            LLM_API_KEYS as _keys_list,
        )
        from lib import (
            LLM_MODEL as _llm,
        )
        from lib import (
            MINIMAX_MODEL as _minimax,
        )
        from lib import (
            QWEN_MODEL as _qwen,
        )
        keys = [(f'key_{i}', k) for i, k in enumerate(_keys_list)]

        # Collect all configured model names
        configured_models = set()
        for var_name, model_name in [
            ('LLM_MODEL',           _llm),
            ('QWEN_MODEL',          _qwen),
            ('GEMINI_MODEL',        _gemini),
            ('GEMINI_PRO_MODEL',    _gemini_pro),
            ('GEMINI_PRO_PREVIEW_MODEL', _gemini_pro_prev),
            ('GEMINI_FLASH_PREVIEW_MODEL', _gemini_flash_prev),
            ('MINIMAX_MODEL',       _minimax),
            ('DOUBAO_MODEL',        _doubao),
            ('CLAUDE_SONNET_MODEL', _claude_sonnet),
            ('IMAGE_GEN_MODEL',     _image_gen),
        ]:
            if model_name:
                configured_models.add(model_name)

        # Expand alias groups: if aws.claude-opus-4.6 is configured,
        # also include aws.claude-opus-4.6-b and vertex.claude-opus-4.6
        self._direct_models = set(configured_models)  # save before expansion
        expanded = set()
        logical_of = {}
        for m in configured_models:
            expanded.add(m)
            logical_of.setdefault(m, m)
            if m in MODEL_ALIASES:
                # Static-group variants of a CONFIGURED name are that name's
                # wire spellings: they slot under its logical model_id.
                for _v in MODEL_ALIASES[m]:
                    logical_of.setdefault(_v, m)
                expanded |= MODEL_ALIASES[m]
        configured_models = expanded

        # All env-var models share LLM_BASE_URL
        from lib import LLM_BASE_URL
        base_url = LLM_BASE_URL

        # Create one slot per (key, model) if the model is in our config
        for key_name, api_key in keys:
            for model in configured_models:
                cfg = DEFAULT_SLOT_CONFIGS.get(model)
                if not cfg:
                    # Unknown model — create a basic text slot
                    cfg = {'caps': {'text'}, 'rpm': 30, 'latency': 3000, 'cost': 0.01}

                # Auto-tag managed pricing tiers ('cheap' + any future
                # PRICING_TIERS rows) from real pricing data.
                # Skip non-chat models — tier tags don't apply (see the
                # provider-build site for why a stray 'cheap' tag on a non-chat
                # slot is harmful).
                slot_caps = set(cfg['caps'])
                if not (slot_caps & self._NON_CHAT_CAPS):
                    tiers = get_pricing_tiers(model, fallback_cost_per_1k=cfg.get('cost'))
                    slot_caps -= (MANAGED_TIER_TAGS - tiers)
                    slot_caps |= tiers

                slot = Slot(
                    key_name=key_name,
                    api_key=api_key,
                    model=model,
                    logical_model=logical_of.get(model, model),
                    capabilities=slot_caps,
                    base_url=base_url,
                    provider_id='default',
                    rpm_limit=cfg['rpm'],
                    latency_ema=cfg.get('latency', 2000),
                    cost_per_1k_tokens=cfg.get('cost', 0.01),
                    stream_only=cfg.get('stream_only', False),
                )
                self.slots.append(slot)

        env_groups = [(m, {mm for mm, lg in logical_of.items() if lg == m})
                      for m in self._direct_models]
        self._build_logical_index(env_groups)

    def _load_benchmark_data(self):
        """Load benchmark_results.json to seed slot parameters and prune dead slots."""
        benchmark_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            'debug', 'benchmark_results.json'
        )
        if not os.path.exists(benchmark_file):
            logger.info('No benchmark data found — using defaults')
            return

        try:
            with open(benchmark_file) as f:
                data = json.load(f)
        except Exception as e:
            logger.error('Failed to load benchmark data: %s', e, exc_info=True)
            return

        models_data = data.get('models', {})

        # Build reverse map: benchmark key label -> our key_name
        # e.g. benchmark has "primary"/"secondary", we have "key_0"/"key_1"
        bench_keys = data.get('keys', {})        # {"primary": "...8427", ...}
        bench_label_to_ours = {}
        for bench_label, bench_suffix in bench_keys.items():
            for slot in self.slots:
                if slot.api_key.endswith(bench_suffix.lstrip('.')):
                    bench_label_to_ours[bench_label] = slot.key_name
                    break

        updated = 0
        dead_slots = []

        for slot in self.slots:
            entry_key = f'{slot.key_name}:{slot.model}'
            entry = models_data.get(entry_key)
            # Also try matching via benchmark label mapping
            if not entry:
                for bench_label, our_name in bench_label_to_ours.items():
                    if our_name == slot.key_name:
                        entry = models_data.get(f'{bench_label}:{slot.model}')
                        if entry:
                            break
            if not entry:
                continue

            # Check if probe showed this pair is *permanently* dead
            # Only prune on clear "invalid model" / HTTP 400 — NOT on
            # transient errors, parsing bugs, or rate-limiting (429)
            probe = entry.get('probe', {})
            if not probe.get('alive', True):
                err = str(probe.get('error', '')).lower()
                if 'invalid model' in err or ('http 400' in err and 'rate' not in err):
                    dead_slots.append(slot)
                    continue
                # Otherwise treat as transient — keep the slot

            # Seed RPM from benchmark
            rpm_data = entry.get('rpm', {})
            if rpm_data and 'rpm_effective' in rpm_data:
                rpm_val = rpm_data['rpm_effective']
                if rpm_val <= 0:
                    # All requests got 429 — this key has no quota for this model
                    dead_slots.append(slot)
                    continue
                slot.set_rpm_ceiling(rpm_val)

            # Seed latency from benchmark (use speed data first, then latency)
            speed = entry.get('speed', {})
            lat = entry.get('latency', {})

            if speed and 'avg_ttft_ms' in speed:
                slot.ttft_ema = speed['avg_ttft_ms']
            if lat and 'avg_latency_ms' in lat:
                slot.latency_ema = lat['avg_latency_ms']
            elif speed and 'avg_ttft_ms' in speed:
                # Estimate E2E latency from TTFT + generation time
                tps = speed.get('avg_tokens_per_sec', 30)
                avg_tokens = speed.get('avg_total_tokens', 100)
                slot.latency_ema = speed['avg_ttft_ms'] + (avg_tokens / max(tps, 1)) * 1000

            # Update vision capability from benchmark
            vision = entry.get('vision', {})
            if vision.get('vision_ok') is True:
                slot.capabilities.add('vision')
            elif vision.get('vision_ok') is False:
                slot.capabilities.discard('vision')

            updated += 1

        # Remove dead slots
        if dead_slots:
            for s in dead_slots:
                self.slots.remove(s)
                logger.debug('  [Dispatch] Removed dead slot: %s:%s', s.key_name, s.model)

        logger.info('Loaded benchmark data: %d slots updated, %d dead removed',
                    updated, len(dead_slots))

    def _build_logical_index(self, entry_groups: list):
        """Build name → logical model_id from each entry's OWN identity set.

        Owner directive 2026-08-06: routing keys on ``model_id`` ALONE.
        ``entry_groups`` is a list of ``(model_id, {model_id} ∪ wire ids)``
        pairs — one per configured model entry. A name resolves back to the
        entry that owns it, but two entries are NEVER unioned into one
        routing group. The old union-find over ``{model_id} ∪ aliases`` ×
        static ``MODEL_ALIAS_GROUPS`` let one provider's alias hijack
        another provider's model (2026-08-06 incident: the official-DeepSeek
        entry ``deepseek-v4-flash1`` carried alias ``deepseek-v4-flash`` →
        connected-component merging glued it to the gateway's same-named
        deployment, and every «official» request silently routed to the
        gateway mirror).

        Conflict rule: an exact ``model_id`` always owns its own name; a
        wire id claimed by two entries resolves to the FIRST claim and is
        logged — an ambiguous config must be loud, not silently widen.
        """
        index: dict[str, str] = {}
        direct = {mid for mid, _ in entry_groups if mid}
        for model_id, _ in entry_groups:
            if model_id:
                index[model_id] = model_id
        claimed_by: dict[str, str] = {}
        conflicts: dict[str, set] = {}
        for model_id, names in entry_groups:
            for name in (names or ()):
                if not name or name == model_id:
                    continue
                if name in direct:
                    # Another entry's model_id owns this name outright.
                    conflicts.setdefault(name, set()).update({name, model_id})
                    continue
                prior = claimed_by.get(name)
                if prior is not None and prior != model_id:
                    conflicts.setdefault(name, set()).update({prior, model_id})
                    continue  # first claim wins
                claimed_by[name] = model_id
                index[name] = model_id
        for name, claimants in sorted(conflicts.items()):
            logger.warning('[Dispatch] model id %r is claimed by multiple '
                           'entries %s — routing it by exact model_id only; '
                           'rename or drop the duplicate alias to silence '
                           'this', name, sorted(claimants))
        self._logical_index = index

    def _route_logical(self, model: str) -> str | None:
        """Resolve a picker/preset/stored-conv name to its logical model_id.

        Returns None when nothing configured owns the name. Order:
        the name→entry index (covers every entry's own wire spellings),
        exact configured model_id, then a static ``MODEL_ALIAS_GROUPS``
        group with EXACTLY ONE configured member (lets a conversation
        persisted against a legacy cross-naming spelling — e.g. a
        Bedrock-native id — keep routing to its one configured home).
        """
        if not model:
            return None
        hit = self._logical_index.get(model)
        if hit:
            return hit
        if model in self._direct_models:
            return model
        group = MODEL_ALIASES.get(model)
        if group:
            configured = [m for m in group if m in self._direct_models]
            if len(configured) == 1:
                return configured[0]
        return None

    def _prefer_matcher(self, prefer_model):
        """Return a slot predicate for ``prefer_model`` (None → no preference).

        model_id-only routing: a slot serves the preference iff its
        ``logical_model`` equals the resolved logical id. When the name
        resolves to nothing configured (unknown / removed entry), the
        predicate falls back to exact wire-id equality so a conversation
        persisted against such a spelling still lands on a slot literally
        serving it — and never on a stranger.
        """
        if not prefer_model:
            return None
        logical = self._route_logical(prefer_model)
        if logical is not None:
            return lambda s: s.logical_model == logical
        return lambda s: s.model == prefer_model

    def pick_slot(self, capability='text', prefer_model=None,
                  exclude_models=None, exclude_keys=None,
                  exclude_pairs=None, strict_model=False) -> Slot | None:
        """Pick the best available slot for the given capability.

        Args:
            capability: Required capability ('text', 'vision', 'thinking', 'cheap')
            prefer_model: If set, prefer this specific model name
            exclude_models: Set of model names to exclude
            exclude_keys: Set of key names to exclude (e.g. after a key-level failure)
            exclude_pairs: Set of (key_name, model) tuples to exclude (e.g. after
                           a permission error on a specific key+model combination)
            strict_model: If True AND prefer_model is set, NEVER fall back to a
                          different model — return None instead.  Use this when the
                          frontend user explicitly chose a model.

        Returns:
            Best Slot, or None if nothing is available.
        """
        return self._pick(capability, prefer_model, exclude_models,
                          exclude_keys, exclude_pairs=exclude_pairs,
                          reserve=False, strict_model=strict_model)

    def pick_and_reserve(self, capability='text', prefer_model=None,
                         exclude_models=None, exclude_keys=None,
                         exclude_pairs=None, strict_model=False) -> Slot | None:
        """Atomically pick the best slot AND increment its inflight counter.

        This prevents the thundering-herd problem where N concurrent threads
        all see inflight=0 and pick the same slot.  The caller MUST call
        ``slot.record_success(...)`` or ``slot.record_error(...)`` when done
        to decrement inflight.

        Args:
            exclude_pairs: Set of (key_name, model) tuples to exclude (e.g. after
                           a permission error on a specific key+model combination)
            strict_model: If True AND prefer_model is set, NEVER fall back to a
                          different model — return None instead.

        Returns:
            Best Slot with inflight already incremented, or None.
        """
        return self._pick(capability, prefer_model, exclude_models,
                          exclude_keys, exclude_pairs=exclude_pairs,
                          reserve=True, strict_model=strict_model)

    # Capabilities that are NOT chat-compatible — a slot is treated as
    # non-chat when ``slot.capabilities.issubset(_NON_CHAT_CAPS)`` (used
    # below in ``_is_chat_compatible``). 'transcription' (audio → text via
    # /audio/transcriptions) is selected directly by lib/transcription.py
    # scanning the slot pool, never through the chat picker; 'audio_chat'
    # is here so a slot carrying ONLY {audio_chat} (no text) is excluded,
    # while real omni chat slots carrying {text, audio_chat, ...} are NOT
    # a subset and remain chat-eligible. Single source of truth lives in
    # lib.model_info.capability_taxonomy (DISPATCHER_NON_CHAT_CAPS is
    # CHAT_EXCLUDED_CAPS | {'audio_chat'} — the difference is intentional).
    _NON_CHAT_CAPS = DISPATCHER_NON_CHAT_CAPS

    def _is_chat_compatible(self, slot) -> bool:
        """Return True if the slot is a chat-capable model (not embedding/image_gen only)."""
        return not slot.capabilities.issubset(self._NON_CHAT_CAPS)

    def _pick(self, capability, prefer_model, exclude_models,
              exclude_keys, *, exclude_pairs=None, reserve=False,
              strict_model=False) -> Slot | None:
        """Internal pick logic — optionally atomic with record_request.

        Args:
            strict_model: When True AND prefer_model is set, the picker will
                NEVER fall back to a different model.  If no slot serving
                the preferred model's logical id is available, returns
                None so the retry loop can wait for cooldown to expire.
                Use this for **user-facing requests** where the frontend
                explicitly chose a model (e.g. "opus" preset).
                Leave False (default) for **backend auto tasks** (compaction,
                daily reports, analysis) where cross-model fallback is fine.
        """
        self.initialize()

        # ── Daily key-health filter ──
        # A key may be auto-disabled for the rest of today if its success rate
        # dropped below the threshold, or a user may have manually toggled it
        # off in Settings. Look up once per pick call.
        try:
            from lib.key_stats import is_key_enabled as _key_enabled
        except ImportError as e:
            logger.debug('[Dispatch] key_stats.is_key_enabled unavailable: %s', e)
            _key_enabled = None

        def _slot_key_enabled(s):
            if _key_enabled is None:
                return True
            try:
                return _key_enabled(s.provider_id, s.key_name, model=s.model)
            except Exception as e:
                logger.debug('[Dispatch] is_key_enabled probe failed for %s/%s: %s',
                             s.provider_id, s.key_name, e)
                return True

        # ── Hard provider pin (multi-tenant isolation) ──
        # When the current task thread is bound to a provider (an inline
        # `provider` block or a registered @prov_xxx BYO endpoint), the
        # picker may ONLY select that provider's slot — for EVERY
        # capability, never silently falling back to an operator-curated
        # key. See lib/llm_dispatch/provider_pin.py for the full rationale
        # (the 429 / no-fallback cross-tenant leak this prevents).
        from lib.llm_dispatch.provider_pin import get_pinned_provider
        _pinned_provider = get_pinned_provider()

        def _slot_provider_ok(s):
            return (not _pinned_provider) or s.provider_id == _pinned_provider

        with self._lock:
            candidates = []
            for slot in self.slots:
                if capability not in slot.capabilities:
                    continue
                # Guard: never dispatch embedding/image_gen-only slots
                #   for chat operations (safety net against capability leaks).
                #   Skip the guard when the caller explicitly asks for a
                #   non-chat capability (image_gen, embedding).
                if capability not in self._NON_CHAT_CAPS and not self._is_chat_compatible(slot):
                    continue
                if exclude_models and slot.model in exclude_models:
                    continue
                if exclude_keys and slot.key_name in exclude_keys:
                    continue
                if exclude_pairs and (slot.key_name, slot.model) in exclude_pairs:
                    continue
                if not slot.is_available:
                    continue
                if not _slot_key_enabled(slot):
                    continue
                if not _slot_provider_ok(slot):
                    continue
                candidates.append(slot)

            if not candidates:
                # Pinned-provider isolation: a pinned task whose own slot
                #   is momentarily unavailable (cooldown/excluded) must WAIT,
                #   never widen onto an operator key. Return None so the
                #   dispatch retry loop keeps cycling within the provider.
                if _pinned_provider:
                    return None
                # strict_model: if the user chose a specific model and all
                #   its slots are in cooldown, return None immediately so the
                #   retry loop waits — do NOT fall back to another model.
                if strict_model and prefer_model:
                    return None
                # Fallback: try ignoring capability constraint for text
                if capability != 'text':
                    for slot in self.slots:
                        if 'text' in slot.capabilities and slot.is_available:
                            if not self._is_chat_compatible(slot):
                                continue
                            if not _slot_key_enabled(slot):
                                continue
                            if not _slot_provider_ok(slot):
                                continue
                            if not (exclude_models and slot.model in exclude_models):
                                if not (exclude_keys and slot.key_name in exclude_keys):
                                    if not (exclude_pairs and (slot.key_name, slot.model) in exclude_pairs):
                                        candidates.append(slot)
                if not candidates:
                    return None

            # ── Conversation-sticky routing ──
            # Anthropic's prompt cache is keyed per API key, so a conversation
            # must keep landing on the SAME key round-to-round or every flip
            # costs a full cache_creation write + 0% read. When this thread is
            # bound to a conv (run_task sets it) and that conv has a recent
            # sticky key, prefer the eligible candidate on that key over the
            # raw min-score pick. The sticky key is a SOFT preference: if it's
            # not among the eligible candidates (cooled down / excluded /
            # disabled), we fall through to score-based selection and rebind.
            _sticky = (sticky_routing_enabled() and get_conv_affinity()) or None
            _sticky_route_key = str(prefer_model or f'cap:{capability}')
            _sticky_key = (get_preferred_key(
                _sticky, route_key=_sticky_route_key) if _sticky else None)

            def _select(pool):
                """Pick the best slot in *pool*, honoring the sticky key when eligible."""
                if _sticky_key:
                    on_key = [s for s in pool if s.key_name == _sticky_key]
                    # Only honor the sticky key when it isn't in cooldown
                    # (score=inf). Otherwise let the normal picker route around it.
                    if on_key:
                        best_sticky = min(on_key, key=lambda s: s.score())
                        if best_sticky.score() != float('inf'):
                            return best_sticky
                return min(pool, key=lambda s: s.score())

            if prefer_model:
                # model_id-only routing (owner directive 2026-08-06): a slot
                # is preferred iff it serves the resolved logical model_id.
                matcher = self._prefer_matcher(prefer_model)
                preferred = [s for s in candidates if matcher(s)]
                if preferred:
                    chosen = _select(preferred)
                elif strict_model:
                    # User explicitly chose this model — all its slots are
                    #   in candidates but none match the logical id (shouldn't
                    #   happen normally, but guard against it).  Return None.
                    return None
                else:
                    chosen = _select(candidates)
            else:
                chosen = _select(candidates)

            # strict_model: if the best candidate has score=inf it means
            #   all matching slots are in cooldown.  Return None so the
            #   retry loop waits — don't silently dispatch a cooldown'd slot
            #   or fall back to a different model.
            if strict_model and chosen.score() == float('inf'):
                return None

            # ── Record the chosen key as this conv's sticky key ──
            # Done for every pick (not just sticky hits) so the FIRST round of
            # a conversation seeds the affinity, and a forced fallback (sticky
            # key cooled down) rebinds to the healthy key it landed on.
            if _sticky and chosen is not None:
                # A churn signal worth grepping: the conv had a sticky key but
                # the picker landed elsewhere (cooled down / excluded), which
                # costs a fresh per-key prompt-cache write this round. Logged at
                # INFO (not DEBUG) because this is the exact event that re-bills
                # the prompt cache — production app.log is INFO+, so a DEBUG line
                # left us blind to the most expensive routing decision.
                _fell_back = bool(_sticky_key and chosen.key_name != _sticky_key)
                if _fell_back:
                    logger.info('[Dispatch] conv=%s sticky key %s unavailable '
                                '— rebinding to %s (model=%s); prompt cache will '
                                'be re-written on the new key',
                                _sticky[:8], _sticky_key, chosen.key_name,
                                chosen.model)
                # Diagnostic: record WHY the key differed (soft-fallback vs
                # no-affinity) so the cache byte-probe can classify a routing
                # flip. Best-effort, never affects routing.
                try:
                    from lib.llm_dispatch.conv_affinity import record_pick_decision
                    record_pick_decision(
                        preferred_key=_sticky_key, chosen_key=chosen.key_name,
                        fell_back=_fell_back)
                except Exception as _pd_err:
                    logger.debug('[Dispatch] pick-decision record failed: %s', _pd_err)
                record_conv_key(_sticky, chosen.key_name,
                                route_key=_sticky_route_key)

            # ── Isolation observability ──
            # One line per pick so a provider leak is a single grep:
            #   pinned=ephemeral:… but provider=… mismatched → leak.
            # Only emitted when a pin is active (operator UI traffic stays
            # quiet). debug-level: high volume, on the hot path.
            if _pinned_provider:
                logger.debug('[Dispatch] pick model=%s provider=%s key=%s '
                             'pinned=%s', chosen.model, chosen.provider_id,
                             chosen.key_name, _pinned_provider)

            if reserve:
                chosen.record_request()  # atomic: inflight++ while still holding lock

            return chosen

    def pick_top_n(self, n=2, capability='text', prefer_model=None,
                   exclude_models=None, reserve=True) -> list[Slot]:
        """Pick the top N slots for racing (dispatch_fastest).

        Args:
            reserve: If True, atomically increment inflight on each
                     returned slot (default True).
        """
        self.initialize()

        with self._lock:
            candidates = []
            for slot in self.slots:
                if capability not in slot.capabilities:
                    continue
                if exclude_models and slot.model in exclude_models:
                    continue
                if not slot.is_available:
                    continue
                candidates.append(slot)

            if not candidates:
                return []

            # Sort by score (lower = better)
            candidates.sort(key=lambda s: s.score())

            # If prefer_model, ensure its logical-model slots lead the list
            if prefer_model:
                matcher = self._prefer_matcher(prefer_model)
                preferred = [s for s in candidates if matcher(s)]
                others = [s for s in candidates if not matcher(s)]
                result = preferred[:n]
                for s in others:
                    if len(result) >= n:
                        break
                    result.append(s)
            else:
                result = candidates[:n]

            if reserve:
                for s in result:
                    s.record_request()

            return result

    def pick_best_slots(self, capability='text', n=5) -> list[Slot]:
        """Return the top-N available slots for a capability, sorted by score.

        Useful for callers that need a list of models for their own
        round-robin or parallel dispatch (e.g. pdf_parser VLM).
        """
        self.initialize()
        with self._lock:
            candidates = [s for s in self.slots
                          if capability in s.capabilities and s.is_available]
            candidates.sort(key=lambda s: s.score())
            return candidates[:n]

    def record_truncation(self, key_name: str, model: str, error: str = '') -> bool:
        """Record a truncated/empty-output event against a specific (key, model) slot.

        This is a soft failure path used by callers like the translate retry
        loop: the HTTP call succeeded but the body was unusable (mid-output
        truncation, blank reply on non-empty input). Bumping the slot's
        consecutive_errors makes ``score()`` deprioritize it on the next pick
        across the whole process — not just within the current retry loop.

        Returns True if a matching slot was found and recorded.
        """
        with self._lock:
            for s in self.slots:
                if s.key_name == key_name and s.model == model:
                    s.record_truncation(error=error)
                    return True
        logger.debug('[Dispatch] record_truncation: no slot %s:%s found',
                     key_name, model)
        return False

    def pick_key_for_model(self, model: str) -> tuple:
        """Pick the best API key for a given model based on current load.

        This is the **key rotation** API — for callers who already know which
        model they want (e.g. orchestrator with user-selected preset) but need
        to spread load across keys.

        Returns:
            (api_key: str, key_name: str, slot: Slot)
            Falls back to first available key if model has no slot.
        """
        self.initialize()
        with self._lock:
            candidates = [s for s in self.slots
                          if s.model == model and s.is_available]
            if not candidates:
                # Model not in dispatch (maybe new) — return first available key
                from lib import LLM_API_KEY
                return LLM_API_KEY, 'key_0', None

            best = min(candidates, key=lambda s: s.score())
            return best.api_key, best.key_name, best

    def has_capable_slots(self, capability: str = 'text',
                          exclude_models=None, exclude_keys=None,
                          exclude_pairs=None, prefer_model=None) -> bool:
        """True if at least one slot CAN serve ``capability`` ignoring
        transient cooldown / rpm state.

        Used by the dispatch retry loops to distinguish two ``pick_slot``
        ``None`` outcomes that need OPPOSITE handling:
          * slots exist but are all in 0.5s rate-limit cooldown → the
            request should keep fast-polling (a 429-equivalent), NOT give
            up — otherwise a fresh concurrent request that arrives while
            every slot is cooling fails immediately on attempt 1.
          * no slot has the capability at all (or all are permanently
            excluded) → genuinely unservable, give up.

        Only the durable disqualifiers (capability, hard exclusions,
        chat-compatibility) are checked here; cooldown / inflight / rpm
        are deliberately ignored.

        ``prefer_model`` (passed by the dispatch loops ONLY under
        ``strict_model``) narrows the answer to the preferred model's
        logical id: a strict-pinned loop can never pick outside that
        model, so healthy slots of OTHER models must not keep it cycling
        (2026-08-03 incident: kimi-k3's sole key permission-excluded →
        the pool's healthy opus/glm slots answered True here, the loop
        spun ~2min resurrecting the dead pair every 60s instead of
        failing over to the pool rescue immediately)."""
        self.initialize()
        ex_models = exclude_models or set()
        ex_keys = exclude_keys or set()
        ex_pairs = exclude_pairs or set()
        matcher = self._prefer_matcher(prefer_model) if prefer_model else None
        # Respect the thread's hard provider pin (same isolation rule as
        # _pick): a pinned task only "has capable slots" among its own
        # provider's slots, so the retry loop waits for THAT provider to
        # recover instead of treating operator slots as a fallback.
        from lib.llm_dispatch.provider_pin import get_pinned_provider
        _pinned_provider = get_pinned_provider()
        with self._lock:
            for s in self.slots:
                if capability not in s.capabilities:
                    continue
                if s.model in ex_models or s.key_name in ex_keys:
                    continue
                if (s.key_name, s.model) in ex_pairs:
                    continue
                if not self._is_chat_compatible(s):
                    continue
                if _pinned_provider and s.provider_id != _pinned_provider:
                    continue
                if matcher is not None and not matcher(s):
                    continue
                return True
        return False


    # ── Shared-project contention (external saturation) ──
    # A 429 naming a PROJECT-level limit (RateLimitError.is_shared_contention)
    # means rotating our own API keys cannot create capacity. Never park slots
    # or feed health: serialize only request admission from this process. The
    # first strike arms the existing 0.3s retry; later callers (including other
    # tasks) reserve one provider/model probe per second before network I/O.
    # A caller rechecks after bounded 3s wait slices, so a deep queue never
    # collapses into a same-time probe herd. This replaces both the retired
    # 2s→60s family parking and the 0.3s retry herd without reviving either.
    _CONTENTION_RESET_GRACE_S = 30.0  # quiet window → streak resets
    _CONTENTION_BASE_RETRY_S = 0.3
    _CONTENTION_PROBE_SPACING_S = 1.0
    _CONTENTION_MAX_WAIT_SLICE_S = 3.0
    _CONTENTION_RECOVERY_SUCCESSES = 2
    _CONTENTION_MAX_FAMILIES = 256

    def _expire_shared_contention_locked(self, now: float) -> None:
        """Drop quiet family gates while ``self._lock`` is held."""
        expired_keys = [
            family_key
            for family_key, state in self._contention_strikes.items()
            if now >= state.last_strike_at + self._CONTENTION_RESET_GRACE_S
        ]
        for expired_key in expired_keys:
            self._contention_strikes.pop(expired_key, None)

    def note_shared_contention(self, slot) -> float:
        """Arm a pre-request family gate; never park or penalize slots."""
        key = (slot.provider_id, slot.model)
        now = time.monotonic()
        with self._lock:
            self._expire_shared_contention_locked(now)
            previous = self._contention_strikes.get(key)
            if previous is None:
                if len(self._contention_strikes) >= self._CONTENTION_MAX_FAMILIES:
                    oldest_key = min(
                        self._contention_strikes,
                        key=lambda candidate: self._contention_strikes[
                            candidate
                        ].last_strike_at,
                    )
                    self._contention_strikes.pop(oldest_key, None)
                strikes = 1
                next_probe_at = now + self._CONTENTION_BASE_RETRY_S
            else:
                strikes = previous.strikes + 1
                next_probe_at = max(
                    now + self._CONTENTION_BASE_RETRY_S,
                    previous.next_probe_at,
                )
            retry_delay = min(
                self._CONTENTION_MAX_WAIT_SLICE_S,
                max(self._CONTENTION_BASE_RETRY_S, next_probe_at - now),
            )
            self._contention_strikes[key] = _SharedContentionProbeState(
                strikes=strikes,
                last_strike_at=now,
                next_probe_at=next_probe_at,
                recovery_successes=0,
            )
        if strikes <= 3 or strikes % 100 == 0:
            logger.info('[Dispatch] shared-project contention on %s:%s — '
                        'next probe no sooner than %.2fs (streak %d)',
                        slot.provider_id, slot.model, retry_delay, strikes)
        else:
            logger.debug('[Dispatch] shared-project contention on %s:%s '
                         '(next probe %.2fs, streak %d)',
                         slot.provider_id, slot.model, retry_delay, strikes)
        return retry_delay

    def reserve_shared_contention_probe(
            self, slot) -> _SharedContentionAdmission:
        """Reserve this family's next probe, or return one bounded wait slice.

        No state means normal admission. If the queue is deeper than one wait
        slice, the caller sleeps and rechecks without reserving; this prevents
        every caller beyond the three-second horizon from waking together.
        """
        key = (slot.provider_id, slot.model)
        now = time.monotonic()
        with self._lock:
            self._expire_shared_contention_locked(now)
            previous = self._contention_strikes.get(key)
            if previous is None:
                return _SharedContentionAdmission(delay_s=0.0, admitted=True)
            delay_s = max(0.0, previous.next_probe_at - now)
            if delay_s > self._CONTENTION_MAX_WAIT_SLICE_S:
                return _SharedContentionAdmission(
                    delay_s=self._CONTENTION_MAX_WAIT_SLICE_S,
                    admitted=False,
                )
            probe_at = now + delay_s
            self._contention_strikes[key] = _SharedContentionProbeState(
                strikes=previous.strikes,
                last_strike_at=previous.last_strike_at,
                next_probe_at=(
                    probe_at + self._CONTENTION_PROBE_SPACING_S
                ),
                recovery_successes=previous.recovery_successes,
            )
            return _SharedContentionAdmission(
                delay_s=delay_s,
                admitted=True,
            )

    def note_shared_success(self, slot) -> bool:
        """Clear a family gate only after sustained, drained recovery.

        A single lucky admission can coexist with an exhausted shared TPM
        window. Two consecutive successes are required, and reservations that
        were already spaced ahead are allowed to drain before the gate clears.
        """
        key = (slot.provider_id, slot.model)
        now = time.monotonic()
        with self._lock:
            self._expire_shared_contention_locked(now)
            previous = self._contention_strikes.get(key)
            if previous is None:
                return False
            recovery_successes = previous.recovery_successes + 1
            reservations_drained = now >= (
                previous.next_probe_at - self._CONTENTION_PROBE_SPACING_S
            )
            if (recovery_successes >= self._CONTENTION_RECOVERY_SUCCESSES
                    and reservations_drained):
                self._contention_strikes.pop(key, None)
                return True
            self._contention_strikes[key] = _SharedContentionProbeState(
                strikes=previous.strikes,
                last_strike_at=previous.last_strike_at,
                next_probe_at=previous.next_probe_at,
                recovery_successes=min(
                    recovery_successes,
                    self._CONTENTION_RECOVERY_SUCCESSES,
                ),
            )
            return False

    def cooling_cause_summary(self, capability: str = 'text',
                              exclude_models=None, exclude_keys=None,
                              exclude_pairs=None) -> set:
        """Set of ``Slot.cooldown_reason`` values among capable slots that are
        currently IN cooldown (``cooldown_until > now``).

        Mirrors :meth:`has_capable_slots` filtering (capability, durable
        exclusions, chat-compatibility, provider pin) and answers the ONE
        question the dispatch wait-loop needs for an honest HUD label: is
        this wait rate-limit contention, or error/upstream backoff? The old
        wait loop hardcoded the "rate-limited" label for EVERY cooldown —
        so a hard-error 300s backoff masqueraded as 限流排队 (yuju opus-5
        vendor-4xx storm, 2026-07-26). Reading guide: empty set → nothing
        is cooling (caller falls back to the legacy rate-limit label, the
        common contention case); 'rate_limit' present → per-key contention;
        anything else → error/upstream backoff.
        """
        self.initialize()
        ex_models = exclude_models or set()
        ex_keys = exclude_keys or set()
        ex_pairs = exclude_pairs or set()
        from lib.llm_dispatch.provider_pin import get_pinned_provider
        _pinned_provider = get_pinned_provider()
        now = time.time()
        causes = set()
        with self._lock:
            for s in self.slots:
                if capability not in s.capabilities:
                    continue
                if s.model in ex_models or s.key_name in ex_keys:
                    continue
                if (s.key_name, s.model) in ex_pairs:
                    continue
                if not self._is_chat_compatible(s):
                    continue
                if _pinned_provider and s.provider_id != _pinned_provider:
                    continue
                if s.cooldown_until > now:
                    # A cooldown stamped before cooldown_reason existed
                    # ('') is bucketed 'error' — it self-heals within one
                    # cooldown lifetime and never mislabels as rate-limit.
                    causes.add(s.cooldown_reason or 'error')
        return causes

    def sticky_cooldown_remaining_s(self, conv_id: str, prefer_model=None,
                                    *, exclude_keys=None, exclude_pairs=None):
        """Seconds until ``conv_id``'s warm sticky key becomes pickable again.

        Returns ``(remaining_seconds, key_name)`` when the conversation has a
        recorded sticky key, that key has a slot serving ``prefer_model``'s
        logical id, the slot is NOT hard-excluded, and its ONLY disqualifier is a
        live cooldown (``now < cooldown_until``). Returns ``None`` when there is
        no warm key worth waiting for (no affinity, key excluded, or the slot is
        already eligible — in which case the normal picker will land on it).

        Used by the dispatch retry loop to decide whether to briefly HOLD for
        the conv's warm key (preserving its prompt-cache prefix) instead of
        rebinding to a cold key. The caller gates the returned ``remaining`` on
        a budget, which is what distinguishes a transient 0.5s rate-limit nudge
        (worth waiting) from a long consecutive-error / quota cooldown (not).
        """
        if not conv_id:
            return None
        sticky_key = get_preferred_key(
            conv_id, route_key=str(prefer_model or 'cap:text'))
        if not sticky_key:
            return None
        ex_keys = exclude_keys or set()
        ex_pairs = exclude_pairs or set()
        if sticky_key in ex_keys:
            return None
        matcher = self._prefer_matcher(prefer_model) if prefer_model else None
        now = time.time()
        best_remaining = None
        with self._lock:
            for s in self.slots:
                if s.key_name != sticky_key:
                    continue
                if matcher is not None and not matcher(s):
                    continue
                if (s.key_name, s.model) in ex_pairs:
                    continue
                if not self._is_chat_compatible(s):
                    continue
                remaining = s.cooldown_until - now
                if remaining <= 0:
                    # The warm key is already eligible — no need to wait; the
                    # normal picker will choose it. Signal "nothing to hold for".
                    return None
                if best_remaining is None or remaining < best_remaining:
                    best_remaining = remaining
        if best_remaining is None:
            return None
        return (best_remaining, sticky_key)

    def summarize_slots(self, capability: str = None) -> str:
        """Return a compact one-line summary of all slots for logging.

        Format: ``key_0/model:rpm=45/60 inf=2 err=0 | key_1/model:rpm=...``
        Only includes slots matching *capability* if specified.
        """
        self.initialize()
        parts = []
        with self._lock:
            for s in sorted(self.slots, key=lambda s: s.score()):
                if capability and capability not in s.capabilities:
                    continue
                rpm = s.current_rpm_usage
                parts.append(
                    f'{s.key_name}/{s.model}:'
                    f'rpm={rpm:.0f}/{s.rpm_limit:.0f} '
                    f'inf={s.inflight} err={s.consecutive_errors}'
                )
        return ' | '.join(parts) if parts else '(no slots)'

    def get_slots_info(self) -> list[dict]:
        """Return current slot info for monitoring."""
        self.initialize()
        return [
            {
                'key': s.key_name,
                'model': s.model,
                'capabilities': sorted(s.capabilities),
                'rpm_limit': s.rpm_limit,
                'rpm_current': s.current_rpm_usage,
                'rpm_headroom_pct': round(s.rpm_headroom * 100, 1),
                'latency_ema_ms': round(s.latency_ema, 1),
                'ttft_ema_ms': round(s.ttft_ema, 1),
                'throughput_ema_tps': round(s.throughput_ema, 1),
                'inflight': s.inflight,
                'consecutive_errors': s.consecutive_errors,
                'success_rate': round(s.success_rate, 3),
                'total_requests': s.total_requests,
                'total_errors': s.total_errors,
                'contention_errors': s.contention_errors,
                'gateway_errors': s.gateway_errors,
                'requests_5h': s.requests_5h,
                'provider_id': s.provider_id,
                'base_url': s.base_url,
                'available': s.is_available,
                'cooldown_until': s.cooldown_until,
                'cooldown_reason': s.cooldown_reason,
                'last_success_time': s.last_success_time,
                'last_error_time': s.last_error_time,
                'last_error_msg': s.last_error_msg,
                'score': round(s.score(), 1),
            }
            for s in sorted(self.slots, key=lambda s: s.score())
        ]
