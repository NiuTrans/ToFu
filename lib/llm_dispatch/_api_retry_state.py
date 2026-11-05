"""Shared request-local dispatch retry state and capacity-recovery decisions.

DispatchRetryState owns counters and transient/durable exclusions for streaming;
ChatRetryState preserves non-stream response policy on the same transitions.
Transport, failure settlement and waiting belong to their existing owners.
"""
import time

from lib.log import get_logger
from ._api_budget import _remember_route_missing_error
from ._api_errors import DispatchWaitStatus
from ._api_hygiene import _advance_credential_delivery_anomaly
from ._credential_outage import CredentialOutageRouting
from ._route_unavailable import RouteUnavailableRouting

logger = get_logger('lib.llm_dispatch.api')


def responses_state_bound_pairs(dispatcher, body_or_messages, *, prefer_model,
                                log_prefix=''):
    """Pairs this request may never use: they cannot decrypt its replayed state.

    Empty unless the request replays encrypted Responses state whose minting
    Deployment is still in scope (``LLMDispatcher.responses_state_binding``).
    """
    from lib.llm.responses_state_origin import responses_state_origins
    origins = responses_state_origins(body_or_messages)
    if not origins:
        return frozenset()
    pairs = dispatcher.responses_state_binding(origins, prefer_model=prefer_model)
    if pairs:
        logger.info(
            '%s Responses state minted on deployment %s binds this dispatch: '
            '%d slot(s) on other deployments excluded for the whole call',
            log_prefix, ','.join(sorted(origins)), len(pairs))
    return pairs


def _cycling_can_ever_serve(dispatcher, capability, *, initial_exclude_models,
                            durable_models, durable_keys, durable_pairs,
                            strict_model, prefer_model):
    """True iff the slot-cooldown cycle can EVER yield a slot again.

    Applies caller model bans and durable payload/route/permission/quota
    exclusions, including non-stream model bans. These exclusions survive every
    transient reset in this request. An empty answer means no slot can become
    eligible under this request's recovery policy, so
    sleeping-and-retrying would cycle forever: the 2026-08-05 CI hang
    (233daa6 serial lane, insight second-pass) spun on
    ``_429_count > 0 or _slots_exist`` — self-sustaining, because
    ``note_cooldown_cycle()`` increments ``_429_count``, so after ANY single
    cooldown cycle the condition stayed true even with every key durably
    excluded by 401.

    Transient exclusions (unreachable / timeout pairs and rate-limit cooldown)
    are deliberately IGNORED here: the 60s exclusion reset resurrects them
    and each resurrection burns a bounded hard attempt, so that cycling
    terminates on its own.
    """
    return dispatcher.has_capable_slots(
        capability,
        exclude_models=initial_exclude_models | durable_models,
        exclude_keys=durable_keys,
        exclude_pairs=durable_pairs,
        prefer_model=prefer_model if strict_model else None)


class DispatchRetryState:
    """Request-local retry and exclusion state shared by chat and stream dispatch.

    Transport execution and cancellable waits stay with each caller. Upstream
    responses and local cooldown polling have separate counters; only actual
    upstream attempts consume the caller's 429 budget. Durable exclusions
    survive periodic transient recovery. ChatRetryState specializes only the
    established non-stream model-exclusion and subscription-wrapper policies.
    """

    _EXCLUSION_RESET_INTERVAL = 60  # reset hard-error exclusions every 60s of 429 cycling

    def __init__(self, exclude_models=None, avoid_pairs=None, *,
                 dispatcher=None, owner_user_id=None, state_bound_pairs=()):
        self.exclude = set(exclude_models) if exclude_models else set()
        # Credential-source outages exclude every slot sharing the failed
        # source for this call; learned cooldowns from earlier calls demote
        # a credential only while another eligible slot remains.
        self.credential_outage = CredentialOutageRouting(dispatcher, owner_user_id)
        # Provider/wire models whose gateway reported no enabled channel are
        # held until their next probe (see _route_unavailable.py).
        self.route_unavailable = RouteUnavailableRouting(dispatcher, owner_user_id)
        # Caller-provided model exclusions are permanent for this dispatch —
        # remembered so the periodic reset doesn't re-introduce them.
        self._initial_exclude_models = set(self.exclude)
        self.exclude_keys = set()
        self.exclude_pairs = set()
        # DURABLE exclusions: deterministic payload/permission (401/403)
        # pairs, route-missing models, and quota-dead keys. Transient sets clear
        # every 60s of 429 cycling
        # (a 502/timeout may be a gateway restart that recovered);
        # rejection classes cannot heal inside one dispatch call, so
        # resurrecting them only re-burns a hard attempt on a guaranteed
        # failure (2026-08-03: a dead kimi-k3 key was re-tried every 60s,
        # consuming all 3 hard attempts over ~2min). They survive
        # maybe_reset_exclusions; the NEXT dispatch call still starts fresh,
        # so a fixed key self-heals between calls.
        self.exclude_keys_durable = set()
        self.exclude_pairs_durable = set()
        # Pairs on Deployments that cannot decrypt the Responses state this
        # request replays (``responses_state_bound_pairs``). Held as durable
        # pairs so every capacity check sees them, and applied from the first
        # pick: unlike failure-driven exclusions they precede any attempt.
        self._state_bound_pairs = frozenset(state_bound_pairs or ())
        self.exclude_pairs_durable |= self._state_bound_pairs
        # A provider's explicit "model route not found" verdict is as durable
        # as permission/quota for this dispatch.  Unlike a 502/timeout, retrying
        # the same wire ID after the 60s transient reset cannot succeed.
        self.exclude_models_durable = set()
        # Caller-provided avoid set seeds exclude_pairs so the first pick
        # already steers around it; tracked separately so it can be relaxed
        # as a last resort when every other slot is cooled/excluded.
        self._initial_avoid = set(avoid_pairs) if avoid_pairs else set()
        if self._initial_avoid:
            self.exclude_pairs |= self._initial_avoid
        self.last_err = None
        # First deterministic PAYLOAD HTTP 400 of this dispatch. Route-missing
        # catalogue errors are deliberately excluded: when every provider-
        # reaching pair fails with different payload 400s, the first (usually
        # the preferred model's rejection) is the actionable cause — the last
        # last-ditch fallback's 400 would mask it at raise time.
        self.first_bad_request_err = None
        self.hard_attempts = 0
        self._429_count = 0
        self._credential_delivery_anomaly_count = 0
        self._cooldown_cycles = 0
        self._started_at = time.monotonic()
        self.queue_wait_ms = 0.0
        self.route_selection_ms = 0.0
        self._last_exclusion_reset = time.monotonic()
        # Monotonic timestamp when the current UNBROKEN gateway-5xx streak
        # began (None = no active streak). Set on the first gateway 502/503/504,
        # cleared by any real per-key 429 or a success. Used to bound a
        # whole-upstream outage without capping genuine per-key contention.
        self._gateway_streak_start = None
        # Monotonic timestamp of the FIRST genuine-429 starvation signal in
        # this dispatch call (None = not starving). Feeds the bounded
        # saturation escalation (): real per-key/contention 429s
        # and 429-equivalent cooldown cycles start the clock; gateway 5xx
        # has its own streak budget above and never starts this one.
        self._saturation_start = None

    def record_queue_wait(self, started_monotonic: float) -> None:
        """Accumulate only explicit dispatcher backpressure waits."""
        self.queue_wait_ms += max(
            0.0, (time.monotonic() - started_monotonic) * 1000)

    def record_route_selection(self, started_monotonic: float) -> None:
        """Accumulate every slot pick of this dispatch, empty picks included."""
        self.route_selection_ms += max(
            0.0, (time.monotonic() - started_monotonic) * 1000)

    @property
    def total_attempts(self):
        return self.hard_attempts + self._429_count

    @property
    def capacity_wait_cycles(self):
        return self._429_count + self._cooldown_cycles

    def wait_status(self, cooldown_cause: str = ''):
        elapsed = max(0.0, time.monotonic() - self._started_at)
        return DispatchWaitStatus(
            kind='waiting_slot',
            request_elapsed_s=elapsed,
            transport_idle_s=elapsed,
            semantic_idle_s=elapsed,
            cooldown_cause=cooldown_cause,
        )

    def gateway_outage_exceeded(self, budget_s):
        """True when nothing but gateway 5xx has come back for > ``budget_s``.

        ``budget_s <= 0`` disables the cap (legacy infinite-rotation behaviour).
        The streak must be active (a gateway 5xx was seen and no real 429 /
        success has cleared it since)."""
        if budget_s <= 0 or self._gateway_streak_start is None:
            return False
        return (time.monotonic() - self._gateway_streak_start) > budget_s

    def maybe_reset_exclusions(self, log_prefix, label):
        """Periodically clear hard-error exclusions during 429 cycling.

        502/timeout exclusions may be transient (gateway restart) but are
        otherwise permanent for the dispatch call.  After 60s of 429 cycling
        give excluded slots another chance — still-broken ones get re-excluded
        quickly.  Caller-provided ``exclude_models`` are preserved.  Returns
        the set of currently-active exclusions (for the caller's log line)
        before the reset, or None when nothing was reset.
        """
        if self.capacity_wait_cycles > 0 and (
                time.monotonic() - self._last_exclusion_reset) >= self._EXCLUSION_RESET_INTERVAL:
            if self.exclude or self.exclude_keys or self.exclude_pairs:
                logger.info(
                    '%s %s: resetting hard-error exclusions after %ds of 429 '
                    'cycling (cycle #%d) — exclude_models=%s exclude_keys=%s '
                    'exclude_pairs=%s',
                    log_prefix, label, self._EXCLUSION_RESET_INTERVAL,
                    self.capacity_wait_cycles, self.exclude, self.exclude_keys,
                    self.exclude_pairs)
                self.exclude.clear()
                self.exclude |= self._initial_exclude_models
                self.exclude |= self.exclude_models_durable
                self.exclude_keys.clear()
                self.exclude_pairs.clear()
            if (self.exclude_models_durable or self.exclude_keys_durable
                    or self.exclude_pairs_durable):
                logger.debug(
                    '%s %s: durable exclusions survive the reset '
                    '(payload/route/permission/quota stay excluded mid-call): '
                    'models=%s keys=%s pairs=%s',
                    log_prefix, label, self.exclude_models_durable,
                    self.exclude_keys_durable,
                    self.exclude_pairs_durable)
            self._last_exclusion_reset = time.monotonic()

    def eff_exclude_models(self):
        """``exclude_models`` to pass to pick_and_reserve.

        Caller-provided exclusions apply from attempt 1; failure-driven ones
        only after the first attempt.
        """
        effective = self.exclude | self.exclude_models_durable
        return effective if (self.total_attempts > 0
                             or self._initial_exclude_models) else None

    def eff_exclude_keys(self):
        outage_keys = self.credential_outage.keys
        if self.total_attempts <= 0:
            return outage_keys or None
        return self.exclude_keys | self.exclude_keys_durable | outage_keys

    def eff_exclude_pairs(self):
        # Caller-provided avoid pairs are specifically a *first-pick* hint:
        # the previous semantic attempt already proved that slot unhealthy.
        # Waiting for this fresh dispatch call to fail once before exposing the
        # set silently routes the retry straight back to the poisoned slot.
        if (self.total_attempts <= 0 and not self._initial_avoid
                and not self._state_bound_pairs):
            return None
        return (self.exclude_pairs | self.exclude_pairs_durable
                | self.route_unavailable.pairs)

    def relax_avoid_if_exhausted(self):
        """Drop the caller-provided avoid set when every other slot is gone.

        Zero-byte force-rotate uses ``avoid_pairs`` to steer away from a
        freshly-poisoned slot; if the rest of the pool is exhausted we'd
        rather retry the bad slot than fail outright.  Relaxes at most once.
        Returns True if it relaxed (caller should ``continue``).
        """
        if self._initial_avoid and self._initial_avoid <= self.exclude_pairs:
            self.exclude_pairs -= self._initial_avoid
            self._initial_avoid = set()
            return True
        return False

    def note_free_429(self, *, is_gateway=False):
        """A routine (non-quota) 429 — free retry, does not count toward the cap.

        ``is_gateway`` distinguishes a genuine per-key 429 (contention — rotate
        forever) from a gateway 5xx mapped onto this path (whole-upstream
        outage — bounded by ``gateway_outage_exceeded``). A gateway 5xx opens /
        extends the outage streak; a REAL 429 (or a success) clears it, because
        it proves the upstream is answering and only this key is throttled."""
        self._429_count += 1
        if is_gateway:
            if self._gateway_streak_start is None:
                self._gateway_streak_start = time.monotonic()
        else:
            self._gateway_streak_start = None
            if self._saturation_start is None:
                self._saturation_start = time.monotonic()

    def note_credential_delivery_anomaly(self, error) -> bool:
        """Count a contradictory missing-credential response.

        Returns True when its dedicated actual-response budget is exhausted.
        The counter is independent of hard attempts and ordinary 429 cycling.
        """
        (self._credential_delivery_anomaly_count,
         exhausted) = _advance_credential_delivery_anomaly(
             error, self._credential_delivery_anomaly_count)
        return exhausted

    def note_success(self):
        """A slot answered — clear any active gateway-outage streak."""
        self._gateway_streak_start = None
        self._saturation_start = None

    def saturation_exceeded(self, budget_s):
        """True when every slot has been 429-starved for > ``budget_s``.

        ``budget_s <= 0`` disables the cap (legacy infinite-rotation
        behaviour). Requires an active starvation clock — a dispatch that
        never saw a genuine 429 (e.g. hard-error churn) is not 'saturated'.
        """
        if budget_s <= 0 or self._saturation_start is None:
            return False
        return (time.monotonic() - self._saturation_start) > budget_s

    def note_cooldown_cycle(self):
        """Record one pool poll with no upstream request attempt."""
        self._cooldown_cycles += 1
        if self._saturation_start is None:
            self._saturation_start = time.monotonic()

    def note_quota_key(self, slot):
        """Quota-exhausted 429 — disable the key for this dispatch; hard attempt.

        Durable: a dead balance cannot recover inside one dispatch call, so
        the periodic 429-cycling reset must not resurrect it."""
        self.exclude_keys_durable.add(slot.key_name)
        self.hard_attempts += 1

    def note_route_missing_model(self, model, error):
        """Durably exclude one unserved wire ID without making it the cause.

        Route-missing is catalog/routing evidence, not a payload rejection.
        Keep it as the terminal error only when no actionable error from a
        provider-reaching route has already been observed.
        """
        model = str(model or '')
        if model:
            self.exclude.add(model)
            self.exclude_models_durable.add(model)
        self.last_err = _remember_route_missing_error(self.last_err, error)
        self.hard_attempts += 1

    def note_bad_request_route(self, slot, dispatcher):
        """Durably exclude the rejecting route, not every credential or wire ID.

        The same endpoint and wire ID receive the same adapted body, so a
        deterministic payload rejection cannot be repaired by another
        credential on that route. Another provider endpoint may compile the
        same logical model differently and remains eligible. A response-state
        binding is the exception: the request may only use its minting
        Deployment, so retain every other credential that can serve it.
        """
        from ._route_unavailable import _route_identity
        route = _route_identity(slot)
        for candidate in getattr(dispatcher, 'slots', ()):
            pair = (candidate.key_name, candidate.model)
            if pair in self._state_bound_pairs:
                continue
            if _route_identity(candidate) != route:
                continue
            if (self._state_bound_pairs and candidate.route_deployment_id
                    and candidate.route_deployment_id == slot.route_deployment_id):
                continue
            self.exclude_pairs_durable.add(pair)
        self.exclude_pairs_durable.add((slot.key_name, slot.model))
        self.hard_attempts += 1

    def note_permission_pair(self, slot, dispatcher, capability, log_prefix):
        """Permission denied — exclude the (key, model) PAIR; escalate to a
        whole-key exclusion only if EVERY model for this key is now excluded.

        Durable: an auth rejection cannot heal inside one dispatch call (see
        ``exclude_pairs_durable``)."""
        self.exclude_pairs_durable.add((slot.key_name, slot.model))
        self.hard_attempts += 1
        _key_pairs = {(kn, m) for kn, m in self.exclude_pairs_durable
                      if kn == slot.key_name}
        _key_models = {s.model for s in dispatcher.slots
                       if s.key_name == slot.key_name
                       and (not capability or capability in s.capabilities)}
        if _key_models and _key_models <= {m for _, m in _key_pairs}:
            self.exclude_keys_durable.add(slot.key_name)
            logger.warning('%s Permission denied on ALL models for key %s — '
                           'excluding entire key', log_prefix, slot.key_name)
            return True
        return False

    def note_permission_key(self, slot):
        """Credential-wide rejection — durably exclude the whole key.

        An OAuth HTTP 401 rejects the shared bearer credential, not one model
        route. Trying every sibling model with that same token only repeats a
        guaranteed failure and can fan one expired login out across a task.
        """
        self.exclude_keys_durable.add(slot.key_name)
        self.hard_attempts += 1

    def note_unreachable_pair(self, slot):
        """Endpoint unreachable — exclude the pair; hard attempt.  (The caller
        sets the slot cooldown — that's slot mutation, not retry bookkeeping.)"""
        self.exclude_pairs.add((slot.key_name, slot.model))
        self.hard_attempts += 1

    def note_generic_error(self, slot, *, is_timeout, strict_model):
        """Generic stream error: a timeout or a strict-model error excludes the
        PAIR (other keys of the model still tried); anything else excludes the
        whole MODEL.  Hard attempt either way."""
        if is_timeout or strict_model:
            self.exclude_pairs.add((slot.key_name, slot.model))
        else:
            self.exclude.add(slot.model)
        self.hard_attempts += 1

    def note_subscription_quota(self):
        """Account for a quota signal carried by a legacy transport wrapper."""
        self.note_free_429()


class ChatRetryState(DispatchRetryState):
    """Non-stream model exclusions last for the whole unchanged request."""

    def note_generic_error(self, slot, *, is_timeout, strict_model):
        super().note_generic_error(slot, is_timeout=is_timeout, strict_model=strict_model)
        if not is_timeout and not strict_model:
            self.exclude_models_durable.add(slot.model)

    def note_stream_only(self, slot):
        self.exclude.add(slot.model)
        self.exclude_models_durable.add(slot.model)
        self.hard_attempts += 1

    def note_subscription_quota(self):
        # Non-stream wrapper signals historically consume the upstream budget
        # without starting a new saturation clock; preserve that policy.
        self._429_count += 1
