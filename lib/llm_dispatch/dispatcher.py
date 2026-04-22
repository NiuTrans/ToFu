"""lib/llm_dispatch/dispatcher.py — LLMDispatcher: slot pool management and selection.

Manages a pool of (key, model) Slots, builds them from environment config +
benchmark data, and provides the slot-picking algorithms (best single, top-N,
best-for-model, etc.).
"""

import json
import os
import threading

from lib.log import get_logger

from .config import DEFAULT_SLOT_CONFIGS, MODEL_ALIASES, is_model_cheap
from .slot import Slot

logger = get_logger(__name__)

__all__ = [
    'LLMDispatcher',
]


class LLMDispatcher:
    """Manages a pool of (key, model) slots and picks the best one per request."""

    def __init__(self):
        self.slots: list[Slot] = []
        self._initialized = False
        self._lock = threading.Lock()

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
        # ★ Always re-read config from disk — the module-level
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
            self._build_slots_from_providers(saved_providers)
        else:
            # ★ Non-default endpoint → auto-discover models from /v1/models
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
            import json as _json

            from lib import _SERVER_CONFIG_PATH, _load_server_config

            config = _load_server_config()
            config['providers'] = providers
            # Don't set presets — let the system use the first model
            if 'presets' not in config:
                config['presets'] = {}

            os.makedirs(os.path.dirname(_SERVER_CONFIG_PATH), exist_ok=True)
            with open(_SERVER_CONFIG_PATH, 'w') as f:
                _json.dump(config, f, indent=2, ensure_ascii=False)

            logger.info('[Dispatch] Saved auto-discovered config to %s',
                       _SERVER_CONFIG_PATH)
        except Exception as e:
            logger.warning('[Dispatch] Failed to persist discovered config: %s',
                          e, exc_info=True)

    # ── Known provider header migrations ──
    # When custom headers were moved from hardcoded _headers() to per-provider
    # extra_headers, existing saved providers need the headers injected.
    _HEADER_MIGRATIONS = {
        # 'your-domain.com': {'X-Custom-Header': 'value'},  # per-provider custom headers
    }

    def _migrate_provider_extra_headers(self, providers, config):
        """Auto-inject extra_headers for known providers missing them.

        This is a one-time migration for providers saved before the
        per-provider extra_headers feature was added. Persists the
        updated config so migration only runs once.
        """
        migrated = False
        for p in providers:
            base_url = p.get('base_url', '')
            existing_hdrs = p.get('extra_headers') or {}
            if existing_hdrs:
                continue  # already has headers — skip
            for domain_suffix, headers in self._HEADER_MIGRATIONS.items():
                if domain_suffix in base_url:
                    p['extra_headers'] = dict(headers)
                    migrated = True
                    logger.info('[Dispatch] Auto-migrated extra_headers for '
                               'provider %s (matched %s)',
                               p.get('id', '?'), domain_suffix)
                    break

        if migrated:
            # Persist so migration only runs once
            try:
                from lib import _SERVER_CONFIG_PATH
                config['providers'] = providers
                os.makedirs(os.path.dirname(_SERVER_CONFIG_PATH), exist_ok=True)
                with open(_SERVER_CONFIG_PATH, 'w') as f:
                    json.dump(config, f, indent=2, ensure_ascii=False)
                logger.info('[Dispatch] Persisted extra_headers migration '
                           'to %s', _SERVER_CONFIG_PATH)
            except Exception as e:
                logger.warning('[Dispatch] Failed to persist extra_headers '
                              'migration: %s', e)

    def _build_slots_from_providers(self, providers):
        """Build slots from saved multi-provider config (server_config.json).

        Each provider has its own base_url, api_keys, and model list.
        Slots = provider.api_keys × (provider.models + their aliases).
        """
        self._direct_models = set()

        for provider in providers:
            if not provider.get('enabled', True):
                continue

            prov_id = provider.get('id', 'default')
            base_url = provider.get('base_url', '')
            api_keys = provider.get('api_keys', [])
            prov_extra_headers = provider.get('extra_headers') or {}
            prov_thinking_format = provider.get('thinking_format', '')
            if not api_keys:
                logger.warning('[Dispatch] Provider %s has no API keys, skipping', prov_id)
                continue

            keys = [(f'{prov_id}_key_{i}', k) for i, k in enumerate(api_keys)]

            # Collect models + their aliases for this provider
            for model_entry in provider.get('models', []):
                model_id = model_entry.get('model_id', '')
                if not model_id:
                    continue

                self._direct_models.add(model_id)

                # Parse capabilities
                caps_raw = model_entry.get('capabilities', ['text'])
                caps = set(caps_raw) if isinstance(caps_raw, list) else {'text'}
                rpm = model_entry.get('rpm', 30)
                cost = model_entry.get('cost', 0.01)
                latency = model_entry.get('latency', 2000)

                # Merge with DEFAULT_SLOT_CONFIGS for any missing fields
                default_cfg = DEFAULT_SLOT_CONFIGS.get(model_id, {})
                if not caps_raw or caps_raw == ['text']:
                    caps = set(default_cfg.get('caps', caps))
                if rpm == 30 and 'rpm' in default_cfg:
                    rpm = default_cfg['rpm']
                if cost == 0.01 and 'cost' in default_cfg:
                    cost = default_cfg['cost']

                # All model IDs to create slots for: primary + aliases
                aliases = model_entry.get('aliases', [])
                all_ids = [model_id] + [a for a in aliases if a]

                for mid in all_ids:
                    # Check DEFAULT_SLOT_CONFIGS for alias-specific overrides
                    alias_cfg = DEFAULT_SLOT_CONFIGS.get(mid, {})
                    slot_caps = set(alias_cfg.get('caps', caps))
                    slot_rpm = alias_cfg.get('rpm', rpm)
                    slot_cost = alias_cfg.get('cost', cost)
                    slot_lat = alias_cfg.get('latency', latency)

                    # Auto-tag 'cheap' from real pricing data
                    # (skip image_gen and embedding models — they aren't chat models)
                    if ('image_gen' not in slot_caps
                            and 'embedding' not in slot_caps
                            and 'cheap' not in slot_caps):
                        if is_model_cheap(mid, fallback_cost_per_1k=slot_cost):
                            slot_caps.add('cheap')

                    # Check stream_only flag from default config
                    slot_stream_only = alias_cfg.get('stream_only', default_cfg.get('stream_only', False))

                    for key_name, api_key in keys:
                        slot = Slot(
                            key_name=key_name,
                            api_key=api_key,
                            model=mid,
                            capabilities=slot_caps,
                            base_url=base_url,
                            provider_id=prov_id,
                            extra_headers=dict(prov_extra_headers),
                            thinking_format=prov_thinking_format,
                            rpm_limit=slot_rpm,
                            latency_ema=slot_lat,
                            cost_per_1k_tokens=slot_cost,
                            stream_only=slot_stream_only,
                        )
                        self.slots.append(slot)

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
        for m in configured_models:
            expanded.add(m)
            if m in MODEL_ALIASES:
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

                # Auto-tag 'cheap' from real pricing data
                # (skip image_gen and embedding models — they aren't chat models)
                slot_caps = set(cfg['caps'])
                if ('image_gen' not in slot_caps
                        and 'embedding' not in slot_caps
                        and 'cheap' not in slot_caps):
                    if is_model_cheap(model, fallback_cost_per_1k=cfg.get('cost')):
                        slot_caps.add('cheap')

                slot = Slot(
                    key_name=key_name,
                    api_key=api_key,
                    model=model,
                    capabilities=slot_caps,
                    base_url=base_url,
                    provider_id='default',
                    rpm_limit=cfg['rpm'],
                    latency_ema=cfg.get('latency', 2000),
                    cost_per_1k_tokens=cfg.get('cost', 0.01),
                    stream_only=cfg.get('stream_only', False),
                )
                self.slots.append(slot)

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
        matched_slots = set()   # track which slots have benchmark entries

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

            matched_slots.add(id(slot))

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
                slot.rpm_limit = max(5, rpm_val)

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

        # Remove alias-expanded slots not confirmed by benchmark.
        # These were added speculatively from _MODEL_ALIAS_GROUPS but the
        # benchmark never saw them for this specific key — so the deployment
        # likely doesn't exist on this API gateway.
        unconfirmed = []
        if models_data:
            direct = getattr(self, '_direct_models', set())
            unconfirmed = [s for s in self.slots
                           if s.model not in direct
                           and id(s) not in matched_slots]
            for s in unconfirmed:
                self.slots.remove(s)
                logger.debug('  [Dispatch] Removed unconfirmed alias slot: '
                            '%s:%s', s.key_name, s.model)

        logger.info('Loaded benchmark data: %d slots updated, '
              '%d dead removed, '
              '%d unconfirmed aliases removed', updated, len(dead_slots), len(unconfirmed))

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

    # Capabilities that are NOT chat-compatible — never dispatch these for
    # chat/stream/cheap/text/vision/thinking operations.
    _NON_CHAT_CAPS = frozenset({'embedding', 'image_gen'})

    def _is_chat_compatible(self, slot) -> bool:
        """Return True if the slot is a chat-capable model (not embedding/image_gen only)."""
        return not slot.capabilities.issubset(self._NON_CHAT_CAPS)

    def _pick(self, capability, prefer_model, exclude_models,
              exclude_keys, *, exclude_pairs=None, reserve=False,
              strict_model=False) -> Slot | None:
        """Internal pick logic — optionally atomic with record_request.

        Args:
            strict_model: When True AND prefer_model is set, the picker will
                NEVER fall back to a different model.  If no slot for the
                preferred model (or its alias group) is available, returns
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
        except Exception:
            _key_enabled = None

        def _slot_key_enabled(s):
            if _key_enabled is None:
                return True
            try:
                return _key_enabled(s.provider_id, s.key_name)
            except Exception:
                return True

        with self._lock:
            candidates = []
            for slot in self.slots:
                if capability not in slot.capabilities:
                    continue
                # ★ Guard: never dispatch embedding/image_gen-only slots
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
                candidates.append(slot)

            if not candidates:
                # ★ strict_model: if the user chose a specific model and all
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
                            if not (exclude_models and slot.model in exclude_models):
                                if not (exclude_keys and slot.key_name in exclude_keys):
                                    if not (exclude_pairs and (slot.key_name, slot.model) in exclude_pairs):
                                        candidates.append(slot)
                if not candidates:
                    return None

            if prefer_model:
                # Use alias group so interchangeable deployments are all "preferred"
                alias_set = MODEL_ALIASES.get(prefer_model, {prefer_model})
                preferred = [s for s in candidates if s.model in alias_set]
                if preferred:
                    chosen = min(preferred, key=lambda s: s.score())
                elif strict_model:
                    # ★ User explicitly chose this model — all its slots are
                    #   in candidates but none match the alias group (shouldn't
                    #   happen normally, but guard against it).  Return None.
                    return None
                else:
                    chosen = min(candidates, key=lambda s: s.score())
            else:
                chosen = min(candidates, key=lambda s: s.score())

            # ★ strict_model: if the best candidate has score=inf it means
            #   all matching slots are in cooldown.  Return None so the
            #   retry loop waits — don't silently dispatch a cooldown'd slot
            #   or fall back to a different model.
            if strict_model and chosen.score() == float('inf'):
                return None

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

            # If prefer_model, ensure it (or alias group members) are in the list
            if prefer_model:
                alias_set = MODEL_ALIASES.get(prefer_model, {prefer_model})
                preferred = [s for s in candidates if s.model in alias_set]
                others = [s for s in candidates if s.model not in alias_set]
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
                'inflight': s.inflight,
                'consecutive_errors': s.consecutive_errors,
                'success_rate': round(s.success_rate, 3),
                'total_requests': s.total_requests,
                'requests_5h': s.requests_5h,
                'provider_id': s.provider_id,
                'available': s.is_available,
                'score': round(s.score(), 1),
            }
            for s in sorted(self.slots, key=lambda s: s.score())
        ]
