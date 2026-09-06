"""Materialize model-routing v2 candidates and select bounded runtime Slots."""

import json
import os
import threading
import time
from dataclasses import dataclass

from lib.log import get_logger
from lib.model_info.capability_taxonomy import DISPATCHER_NON_CHAT_CAPS

from .config import MANAGED_TIER_TAGS, MODEL_ALIASES
from .conv_affinity import (
    get_conv_affinity,
    get_preferred_key,
    record_conv_key,
    sticky_routing_enabled,
)
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
        # Compatibility projection retained for the diagnostics endpoint.
        # Explicit v2 Connections make protocol-face refusal unnecessary.
        self.face_refusals: list[dict] = []

    def initialize(self):
        """Build the v2 slot pool and apply optional benchmark seeds."""
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

    def _build_slots(self):
        """Create operator slots exclusively from model-routing v2."""
        # ── Benchmark / multi-tenant fail-loud mode ──
        # When set, build NO operator-curated slots at all. The only way to
        # dispatch is then an owner-authorized model-routing v2 candidate
        # group (ephemeral slots, injected at request time). This is
        # defense-in-depth on top of the per-task provider pin: with the
        # operator's configured slots absent there is literally no key
        # to leak onto, so an isolation bug fails LOUDLY (clean "no slot"
        # error) instead of silently consuming shared internal quota.
        if os.environ.get('TOFU_DISABLE_CONFIGURED_SLOTS', '').strip().lower() \
                in ('1', 'true', 'yes', 'on'):
            logger.warning('[Dispatch] TOFU_DISABLE_CONFIGURED_SLOTS set — '
                           'building 0 operator slots; request-scoped v2 '
                           'route groups remain available')
            return
        self._build_slots_from_model_routing()

    def _build_slots_from_model_routing(self) -> None:
        """Materialize the personal operator pool from the v2 repository.

        Request handlers use owner-specific ephemeral route groups. The shared
        pool exists for personal background tasks that have no HTTP request
        boundary; distributed mode intentionally requires owner enumeration.
        """
        from lib.identity import PERSONAL_USER_ID
        from lib.model_routing import (
            ModelRef,
            ModelRoutingError,
            ModelRoutingRepository,
            NativeModelSelection,
            OwnerBoundary,
            RoutePolicy,
            RouteSnapshotBuilder,
            compile_candidates,
            decode_credential_secret,
        )
        from runtime_guards import load_deployment_configuration

        deployment_configuration = load_deployment_configuration()
        if deployment_configuration.mode != 'personal':
            logger.info(
                '[Dispatch] shared v2 slots disabled in %s mode; requests '
                'must supply an authenticated owner route group',
                deployment_configuration.mode,
            )
            return
        repository = ModelRoutingRepository()
        boundary = OwnerBoundary.create(PERSONAL_USER_ID)
        try:
            authority = repository.get(boundary)
        except ModelRoutingError as exc:
            # The shared dispatcher is also used by request-scoped ephemeral
            # routes.  A temporarily unavailable durable authority must not
            # make that in-memory capability impossible to initialise.  Only
            # the typed authority outage degrades to an empty operator pool;
            # malformed routing documents and programming errors still fail
            # loudly instead of being hidden by this lean fallback.
            if exc.kind != 'model_routing_storage_unavailable':
                raise
            logger.warning(
                '[Dispatch] model-routing authority unavailable; starting '
                'with 0 shared slots: %s', exc)
            return
        if not authority.revision:
            logger.error('[Dispatch] model-routing v2 authority is not active')
            return

        emitted: set[tuple[str, str]] = set()
        entry_groups: list[tuple[str, set[str]]] = []
        for model in authority.document['models']:
            selection = NativeModelSelection(
                ModelRef(model['creator_id'], model['model_id']), None, '')
            # The shared personal pool serves non-chat subsystems too (speech,
            # embedding, image generation). Chat selection applies its own
            # capability gate later; requiring ``text`` here would silently
            # erase a valid transcription-only Offering.
            candidates = compile_candidates(
                authority.document,
                selection,
                policy=RoutePolicy(required_capabilities=frozenset()),
            )
            entry_group = {model['model_id']}
            for candidate in candidates:
                unique = (
                    candidate.deployment['deployment_id'],
                    candidate.credential['credential_id'],
                )
                if unique in emitted:
                    continue
                emitted.add(unique)
                secret = repository.resolve_secret(
                    boundary, candidate.credential['secret_reference']) \
                    if candidate.credential['secret_reference'] else ''
                api_key, oauth, secret_headers = decode_credential_secret(
                    secret, kind=candidate.credential['kind'])
                quota = candidate.credential.get('quota_policy') or {}
                access_quota = candidate.provider_access.get('quota_policy') or {}
                pricing = candidate.offering.get('actual_pricing') or {}
                snapshot = RouteSnapshotBuilder(selection)
                snapshot.record_transition(
                    source=None,
                    target=candidate,
                    reason='computed_v2_route',
                    kind='initial',
                )
                slot = Slot(
                    key_name=(candidate.credential['credential_id'] + ':'
                              + candidate.deployment['deployment_id']),
                    api_key=api_key,
                    model=candidate.deployment['wire_model_id'],
                    logical_model=model['model_id'],
                    capabilities=set(candidate.offering['capabilities']),
                    base_url=candidate.connection['base_url'],
                    provider_id=candidate.provider_id,
                    routing_provider_id=candidate.provider_id,
                    routing_owner_user_id=boundary.owner_user_id,
                    route_offering_id=candidate.offering['offering_id'],
                    route_deployment_id=candidate.deployment['deployment_id'],
                    route_connection_id=candidate.connection['connection_id'],
                    route_credential_id=candidate.credential['credential_id'],
                    max_output_tokens=int(
                        candidate.deployment.get('max_output_tokens') or 0),
                    route_snapshot=snapshot.finalize(candidate),
                    extra_headers={
                        **(candidate.connection.get('extra_headers') or {}),
                        **secret_headers,
                    },
                    thinking_format=str(
                        candidate.connection.get('thinking_format') or ''),
                    protocol=candidate.connection['protocol'],
                    responses_profile=str(
                        candidate.connection.get('responses_profile') or ''),
                    oauth=oauth,
                    adapter=dict(candidate.connection.get('adapter') or {}),
                    rpm_limit=float(
                        quota.get('rpm') or access_quota.get('rpm') or 30),
                    latency_ema=float(
                        candidate.deployment.get('latency_ms') or 3000),
                    cost_per_1k_tokens=(
                        float(pricing.get('input') or 0.0)
                        + float(pricing.get('output') or 0.0)
                    ) / 2000.0,
                )
                self.slots.append(slot)
                entry_group.add(candidate.deployment['wire_model_id'])
            entry_groups.append((model['model_id'], entry_group))
        self._build_logical_index(entry_groups)
        logger.info(
            '[Dispatch] Built %d slots from model-routing v2 revision %d',
            len(self.slots), authority.revision)

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
        serving it. Request-scoped v2 slots are injected after this index is
        built, so their explicit ``logical_model`` is also an exact match.
        """
        if not prefer_model:
            return None
        logical = self._route_logical(prefer_model)
        if logical is not None:
            return lambda s: s.logical_model == logical
        return lambda s: (
            s.model == prefer_model or s.logical_model == prefer_model)

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

    # Capabilities that are NOT chat-compatible — after pricing-only tags are
    # ignored, a slot is treated as non-chat when its remaining capabilities
    # are a subset of ``_NON_CHAT_CAPS``. 'transcription' (audio → text via
    # /audio/transcriptions) is selected directly by lib/transcription.py
    # scanning the slot pool, never through the chat picker; 'audio_chat'
    # is here so a slot carrying ONLY {audio_chat} (no text) is excluded,
    # while real omni chat slots carrying {text, audio_chat, ...} are NOT
    # a subset and remain chat-eligible. Single source of truth lives in
    # lib.model_info.capability_taxonomy (DISPATCHER_NON_CHAT_CAPS is
    # CHAT_EXCLUDED_CAPS | {'audio_chat'} — the difference is intentional).
    _NON_CHAT_CAPS = DISPATCHER_NON_CHAT_CAPS

    def _is_chat_compatible(self, slot) -> bool:
        """Return whether operational capabilities expose a chat endpoint.

        Pricing tiers such as ``cheap`` describe cost, not protocol support.
        Ignoring them here prevents a stale ``{embedding, cheap}`` model from
        escaping the non-chat guard and reaching ``/chat/completions``.
        """
        operational_capabilities = (
            set(slot.capabilities) - set(MANAGED_TIER_TAGS)
        )
        return bool(operational_capabilities) and not (
            operational_capabilities.issubset(self._NON_CHAT_CAPS)
        )

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
                return _key_enabled(
                    s.key_stats_provider_id(),
                    s.key_stats_key_name(),
                    model=s.model)
            except Exception as e:
                logger.debug('[Dispatch] is_key_enabled probe failed for %s/%s: %s',
                             s.provider_id, s.key_name, e)
                return True

        # ── Hard provider pin (multi-tenant isolation) ──
        # When the current task thread is bound to a request-scoped v2 route
        # group, the picker may ONLY select that group's slots — for EVERY
        # capability, never silently falling back to an operator-curated
        # key. See lib/llm_dispatch/provider_pin.py for the full rationale
        # (the cross-request credential leak this prevents).
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
                selection_now = time.monotonic()
                self._expire_shared_contention_locked(selection_now)
                pool = self._earliest_shared_contention_pool_locked(
                    pool, selection_now)
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

    def mark_model_route_missing(
            self, *, provider_id: str, model: str, error: str = '') -> int:
        """Disable an unserved provider/wire-model route until pool rebuild.

        The gateway's explicit route-missing verdict is model-scoped: trying
        another key on the same provider repeats the same deterministic 400.
        Disable every matching slot in this process so later LLM rounds do not
        pay for the same failed probe.  Settings/catalog refresh calls
        ``reset_dispatcher()``, which rebuilds the slots and is the explicit
        recovery boundary if the upstream route later appears.

        Returns the number of slots whose availability changed.
        """
        provider_id = str(provider_id or '')
        model = str(model or '')
        if not model:
            return 0
        changed = 0
        with self._lock:
            for slot in self.slots:
                if slot.provider_id != provider_id or slot.model != model:
                    continue
                if slot.mark_route_missing(error):
                    changed += 1
        if changed:
            logger.warning(
                '[Dispatch] Disabled %d slot(s) for unserved route %s/%s '
                'until the dispatcher is rebuilt',
                changed, provider_id or '?', model)
        return changed

    def pick_key_for_model(self, model: str) -> tuple:
        """Pick the best API key for a given model based on current load.

        This is the **key rotation** API — for callers who already know which
        model they want (e.g. orchestrator with user-selected preset) but need
        to spread load across keys.

        Returns ``(api_key, key_name, slot)``. Unknown models fail closed as
        ``('', '', None)``; a process-global key must never substitute for an
        owner-authorized route.
        """
        self.initialize()
        with self._lock:
            candidates = [s for s in self.slots
                          if s.model == model and s.is_available]
            if not candidates:
                return '', '', None

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

        Only durable disqualifiers (capability, hard exclusions,
        chat-compatibility) are checked here; cooldown / inflight / rpm are
        deliberately ignored. Inside ``strict_billing_stop_admission()``,
        key-health rejection is durable for the optional request too: treating
        a recorded 402/quota stop as cooldown would otherwise poll every 300ms
        until the caller deadline despite having no admissible route.

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
        from lib.key_stats import (
            is_key_enabled as _key_enabled,
            is_strict_billing_stop_admission,
        )
        _strict_key_admission = is_strict_billing_stop_admission()

        def _key_policy_admits(slot) -> bool:
            if not _strict_key_admission:
                return True
            try:
                return _key_enabled(
                    slot.provider_id, slot.key_name, model=slot.model)
            except Exception as exc:
                logger.debug(
                    '[Dispatch] strict key admission probe failed for %s/%s: %s',
                    slot.provider_id, slot.key_name, exc)
                return True

        with self._lock:
            for s in self.slots:
                if capability not in s.capabilities:
                    continue
                if not s.is_available:
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
                if not _key_policy_admits(s):
                    continue
                return True
        return False


    # ── Shared-project contention (external saturation) ──
    # A 429 naming a PROJECT-level limit (RateLimitError.is_shared_contention)
    # means rotating our own API keys cannot create capacity. Never park slots
    # or feed health: serialize only request admission from this process. The
    # first strike arms the existing 0.3s retry. Repeated rejections increase
    # the provider/model probe interval 1→2→4→8→15s while every wait remains
    # abortable in bounded 3s slices. This is admission timing, never slot
    # parking or health. Automatic work also prefers the family whose probe is
    # due first; explicit model/provider boundaries remain unchanged.
    # A shared RPM/TPM window commonly survives 30 seconds. After that quiet
    # period retain only a one-second serialization seed: the first recovery
    # probe remains immediate, but concurrent arrivals cannot recreate a herd.
    # Two full minute windows without another rejection retire the state.
    _CONTENTION_DECAY_GRACE_S = 30.0
    _CONTENTION_RESET_GRACE_S = 120.0
    _CONTENTION_BASE_RETRY_S = 0.3
    _CONTENTION_PROBE_SPACING_S = 1.0
    _CONTENTION_MAX_PROBE_SPACING_S = 15.0
    _CONTENTION_STRIKES_PER_SPACING_STEP = 2
    _CONTENTION_MAX_WAIT_SLICE_S = 3.0
    _CONTENTION_RECOVERY_SUCCESSES = 2
    _CONTENTION_MAX_FAMILIES = 256

    @classmethod
    def _contention_probe_spacing_s(cls, strikes: int) -> float:
        """Return a bounded exponential interval for one rejection streak."""
        normalized_strikes = max(1, int(strikes))
        step = min(
            16,
            (normalized_strikes - 1)
            // cls._CONTENTION_STRIKES_PER_SPACING_STEP,
        )
        return min(
            cls._CONTENTION_MAX_PROBE_SPACING_S,
            cls._CONTENTION_PROBE_SPACING_S * (2 ** step),
        )

    def _expire_shared_contention_locked(self, now: float) -> None:
        """Decay then drop quiet family gates while ``self._lock`` is held."""
        expired_keys = [
            family_key
            for family_key, state in self._contention_strikes.items()
            if now >= state.last_strike_at + self._CONTENTION_RESET_GRACE_S
        ]
        for expired_key in expired_keys:
            self._contention_strikes.pop(expired_key, None)
        for family_key, state in tuple(self._contention_strikes.items()):
            if (
                state.strikes > 1
                and now >= (
                    state.last_strike_at + self._CONTENTION_DECAY_GRACE_S
                )
            ):
                self._contention_strikes[family_key] = (
                    _SharedContentionProbeState(
                        strikes=1,
                        last_strike_at=state.last_strike_at,
                        next_probe_at=min(state.next_probe_at, now),
                        recovery_successes=state.recovery_successes,
                    )
                )

    def _earliest_shared_contention_pool_locked(
        self,
        slots: list[Slot],
        now: float,
    ) -> list[Slot]:
        """Prefer a ready family without crossing the caller's candidate set."""
        if len(slots) < 2:
            return slots
        delays = [
            max(
                0.0,
                self._contention_strikes.get(
                    (slot.provider_id, slot.model),
                    _SharedContentionProbeState(0, 0.0, now),
                ).next_probe_at - now,
            )
            for slot in slots
        ]
        earliest_delay = min(delays)
        return [
            slot for slot, delay in zip(slots, delays)
            if delay <= earliest_delay + 1e-6
        ]

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
                probe_spacing = self._contention_probe_spacing_s(strikes)
                next_probe_at = max(
                    now + probe_spacing,
                    previous.next_probe_at,
                )
            next_probe_delay = max(
                self._CONTENTION_BASE_RETRY_S, next_probe_at - now)
            retry_delay = min(
                self._CONTENTION_MAX_WAIT_SLICE_S, next_probe_delay)
            self._contention_strikes[key] = _SharedContentionProbeState(
                strikes=strikes,
                last_strike_at=now,
                next_probe_at=next_probe_at,
                recovery_successes=0,
            )
        spacing_changed = (
            strikes > 1
            and self._contention_probe_spacing_s(strikes)
            != self._contention_probe_spacing_s(strikes - 1)
        )
        if strikes <= 3 or spacing_changed or strikes % 100 == 0:
            logger.info('[Dispatch] shared-project contention on %s:%s — '
                        'next probe no sooner than %.2fs (streak %d)',
                        slot.provider_id, slot.model, next_probe_delay, strikes)
        else:
            logger.debug('[Dispatch] shared-project contention on %s:%s '
                         '(next probe %.2fs, streak %d)',
                         slot.provider_id, slot.model, next_probe_delay, strikes)
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
                    probe_at
                    + self._contention_probe_spacing_s(previous.strikes)
                ),
                recovery_successes=previous.recovery_successes,
            )
            return _SharedContentionAdmission(
                delay_s=delay_s,
                admitted=True,
            )

    def reserve_shared_contention_probe_now(
            self, slot) -> _SharedContentionAdmission:
        """Reserve a probe only when it can start now; otherwise do not mutate.

        Reconstructible background work uses this atomic admission form to
        yield instead of joining an already-known provider/model wait. A due
        probe is still reserved so concurrent optional arrivals cannot all
        observe the same zero-delay window and form a new request herd.
        """
        key = (slot.provider_id, slot.model)
        now = time.monotonic()
        with self._lock:
            self._expire_shared_contention_locked(now)
            previous = self._contention_strikes.get(key)
            if previous is None:
                return _SharedContentionAdmission(
                    delay_s=0.0,
                    admitted=True,
                )
            delay_s = max(0.0, previous.next_probe_at - now)
            if delay_s > 0:
                return _SharedContentionAdmission(
                    delay_s=delay_s,
                    admitted=False,
                )
            self._contention_strikes[key] = _SharedContentionProbeState(
                strikes=previous.strikes,
                last_strike_at=previous.last_strike_at,
                next_probe_at=(
                    now + self._contention_probe_spacing_s(previous.strikes)
                ),
                recovery_successes=previous.recovery_successes,
            )
            return _SharedContentionAdmission(
                delay_s=0.0,
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
            probe_spacing = self._contention_probe_spacing_s(previous.strikes)
            reservations_drained = now >= (
                previous.next_probe_at - probe_spacing
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

    def get_shared_contention_info(self) -> list[dict]:
        """Return the bounded live family-gate state for diagnostics."""
        self.initialize()
        now = time.monotonic()
        with self._lock:
            self._expire_shared_contention_locked(now)
            return [
                {
                    'provider_id': provider_id,
                    'model': model,
                    'strikes': state.strikes,
                    'probe_spacing_s': self._contention_probe_spacing_s(
                        state.strikes),
                    'next_probe_in_s': round(
                        max(0.0, state.next_probe_at - now), 3),
                    'recovery_successes': state.recovery_successes,
                }
                for (provider_id, model), state in sorted(
                    self._contention_strikes.items())
            ]

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
