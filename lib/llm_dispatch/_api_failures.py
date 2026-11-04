"""One failed-attempt policy for sync/async streams and non-stream chat.

DispatchFailurePolicy owns exception classification, slot settlement, exclusions,
retry notifications and the request's one OAuth refresh. Existing retry state,
route/credential authority and budget helpers remain their own owners. handle()
returns the already-established wait duration; callers own cancellable waiting,
queue timing, transport execution and attempt observation.
"""

from lib.log import get_logger
from lib.llm.llm_errors import (
    AbortedError,
    BadRequestError,
    ContentFilterError,
    EndpointUnreachableError,
    InvalidImageError,
    LocalRequestPreparationError,
    ModelRouteMissingError,
    ModelRouteUnavailableError,
    PermissionError_,
    PromptTooLongError,
    RateLimitError,
    RequestScopedError,
    RequestTransportError,
    StreamOnlyError,
    is_subscription_quota_error,
    parse_subscription_retry_after,
)
from ._api_budget import (
    _force_oauth_token_refresh,
    _raise_if_429_attempt_budget_exhausted,
    _record_route_missing_model,
)
from ._api_contention import _shared_contention_retry_delay
from ._api_errors import DispatchSharedContentionDeferred
from ._endpoint_outage import log_endpoint_outage
from ._route_unavailable import log_route_unavailable, route_unavailable_reason

logger = get_logger('lib.llm_dispatch.api')


def _notify_retry(
    callback,
    slot,
    *,
    strict_model: bool,
    attempt: int,
    reason: str,
    status_code: int,
) -> None:
    """Report a retry with the physical route currently being attempted."""
    if callback is None:
        return
    callback(
        attempt=attempt,
        reason=reason,
        status_code=status_code,
        attempt_model=slot.model,
        provider_id=(slot.routing_provider_id or slot.provider_id),
        strict_model=bool(strict_model),
    )


class DispatchFailurePolicy:
    def __init__(
        self,
        state,
        dispatcher,
        *,
        capability,
        strict_model,
        max_429_attempts,
        owner_user_id,
        log_prefix,
        on_retry=None,
        restart=None,
        attempt_observer=None,
        on_failure=None,
    ):
        self.state = state
        self.dispatcher = dispatcher
        self.capability = capability
        self.strict_model = strict_model
        self.max_429_attempts = max_429_attempts
        self.owner_user_id = owner_user_id
        self.log_prefix = log_prefix
        self.on_retry = on_retry
        self.restart = restart
        self.attempt_observer = attempt_observer
        self.on_failure = on_failure
        self._oauth_refresh_used = False

    def _retry_notice(self, slot, *, attempt, reason, status_code):
        _notify_retry(
            self.on_retry,
            slot,
            strict_model=self.strict_model,
            attempt=attempt,
            reason=reason,
            status_code=status_code,
        )

    def _restart(self, reason):
        if self.restart is not None:
            self.restart(reason)

    def _note_failure_cause(self, error):
        if self.on_failure is not None:
            self.on_failure(type(error).__name__)

    def _remember_failure(self, error):
        self.state.last_err = error
        self._note_failure_cause(error)

    def handle(self, error, slot, *, tag):
        """Settle one failed reservation; retry with a delay or raise unchanged."""
        if isinstance(error, LocalRequestPreparationError):
            return self._local_preparation(error, slot, tag)
        if isinstance(error, DispatchSharedContentionDeferred):
            return self._deferred(error, slot, tag)
        if isinstance(error, ModelRouteUnavailableError):
            return self._route_unavailable(error, slot, tag)
        if isinstance(error, RateLimitError):
            return self._rate_limit(error, slot, tag)
        if isinstance(error, PermissionError_):
            return self._permission(error, slot, tag)
        if isinstance(error, EndpointUnreachableError):
            return self._endpoint_unreachable(error, slot, tag)
        if isinstance(error, AbortedError):
            return self._aborted(error, slot, tag)
        if isinstance(error, ContentFilterError):
            return self._content_filter(error, slot, tag)
        if isinstance(error, PromptTooLongError):
            return self._prompt_too_long(error, slot, tag)
        if isinstance(error, InvalidImageError):
            return self._invalid_image(error, slot, tag)
        if isinstance(error, StreamOnlyError):
            return self._stream_only(error, slot, tag)
        if isinstance(error, RequestTransportError):
            return self._transport(error, slot, tag)
        if isinstance(error, RequestScopedError):
            return self._request_scoped(error, slot, tag)
        if isinstance(error, ModelRouteMissingError):
            return self._model_missing(error, slot, tag)
        if isinstance(error, BadRequestError):
            return self._bad_request(error, slot, tag)
        if isinstance(error, Exception):
            return self._unexpected(error, slot, tag)
        if isinstance(error, BaseException):
            return self._cancelled(error, slot, tag)
        raise TypeError('A stream failure must be an exception')

    def _local_preparation(self, error, slot, tag):
        slot.release()
        if self._skip_unrepresentable_route(error, slot, tag):
            return 0.0
        if not self.state.credential_outage.absorb(error, slot, log_prefix=tag):
            raise error
        self._remember_failure(error)
        if self.attempt_observer is not None:
            self.attempt_observer.note_cause(type(error).__name__)
        return 0.0

    def _skip_unrepresentable_route(self, error, slot, tag):
        """Let a non-strict pick pass over a route that cannot carry the request.

        Native continuity (signed reasoning, opaque replay items) binds the
        request to protocols that represent it; another route may. A strict
        pick cannot change model or protocol and still surfaces the error.
        """
        from lib.llm.conversion import ProtocolConversionError
        if self.strict_model or not (
                isinstance(error, ProtocolConversionError)
                and error.code == 'mandatory_continuity_unrepresentable'):
            return False
        self.state.note_unrepresentable_route(slot, self.dispatcher)
        # A provider failure seen earlier stays the actionable terminal cause.
        if self.state.last_err is None:
            self.state.last_err = error
        if self.attempt_observer is not None:
            self.attempt_observer.note_cause(type(error).__name__)
        logger.info(
            '%s %s cannot represent this request\'s native continuity '
            '— route excluded for the call, trying other eligible routes',
            tag, slot.model)
        return True

    def _deferred(self, error, slot, tag):
        slot.release()
        raise error

    def _route_unavailable(self, error, slot, tag):
        slot.release()
        self._remember_failure(error)
        _probe_in_s = self.state.route_unavailable.absorb(error, slot)
        self.state.note_free_429(is_gateway=True)
        _raise_if_429_attempt_budget_exhausted(
            max_429_attempts=self.max_429_attempts,
            upstream_attempts=self.state._429_count,
            last_error=error,
        )
        log_route_unavailable(
            self.log_prefix,
            slot,
            error,
            verdicts=self.state.route_unavailable.verdicts,
            probe_in_s=_probe_in_s,
        )
        self._retry_notice(
            slot,
            attempt=self.state._429_count,
            reason=route_unavailable_reason(error),
            status_code=error.status_code,
        )
        self._restart('model route unavailable — holding route')
        return 0.0

    def _rate_limit(self, error, slot, tag):
        """Preserve quota, gateway and shared-contention accounting and existing waits."""
        _is_quota = bool(getattr(error, 'is_quota', False))
        _is_gateway = bool(getattr(error, 'is_gateway', False))
        _is_contention = bool(getattr(error, 'is_shared_contention', False))
        _is_subscription = bool(getattr(error, 'is_subscription_quota', False))
        _is_account_quota = bool(_is_quota and int(getattr(error, 'status_code', 0) or 0) == 402)
        _err_str = str(error)[:200]
        slot.record_error(
            is_rate_limit=True,
            is_quota_exhausted=_is_quota,
            is_account_quota=_is_account_quota,
            is_gateway=_is_gateway,
            is_shared_contention=_is_contention,
            cooldown_s=getattr(error, 'retry_after_s', None),
            error=_err_str if _is_quota else '',
        )
        self._remember_failure(error)
        _retry_delay_s = _shared_contention_retry_delay(
            self.dispatcher, slot, _is_contention, self.log_prefix
        )
        if _is_quota:
            self.state.note_quota_key(slot)
            logger.warning(
                '%s Quota exhausted on %s:%s — disabling key for today: %s',
                self.log_prefix,
                slot.key_name,
                slot.model,
                _err_str,
            )
            self._retry_notice(
                slot,
                attempt=self.state.hard_attempts,
                reason='Key balance exhausted',
                status_code=429,
            )
            self._restart('key balance exhausted')
            return 0.0
        self.state.note_free_429(is_gateway=bool(getattr(error, 'is_gateway', False)))
        _credential_anomaly_exhausted = self.state.note_credential_delivery_anomaly(error)
        _raise_if_429_attempt_budget_exhausted(
            max_429_attempts=self.max_429_attempts,
            upstream_attempts=self.state._429_count,
            last_error=error,
        )
        if _credential_anomaly_exhausted:
            logger.error(
                '%s Credential-delivery anomaly persisted for %d actual responses on %s — stopping bounded gateway rotation without excluding the key or recording an auth failure',
                self.log_prefix,
                getattr(error, 'credential_delivery_anomaly_attempts', 0),
                slot.model,
            )
            raise error
        _err_body = str(error)[:300]
        if self.state._429_count <= 3 or self.state._429_count % 100 == 0:
            logger.info(
                '%s 429 rate-limited on %s:%s (cycle #%d) — body: %s',
                self.log_prefix,
                slot.key_name,
                slot.model,
                self.state._429_count,
                _err_body,
            )
        else:
            logger.debug(
                '%s 429 rate-limited on %s:%s (cycle #%d)',
                self.log_prefix,
                slot.key_name,
                slot.model,
                self.state._429_count,
            )
        if _is_subscription:
            self._retry_notice(
                slot,
                attempt=self.state._429_count,
                reason='Subscription quota reached',
                status_code=429,
            )
        elif _is_gateway:
            self._retry_notice(
                slot,
                attempt=self.state._429_count,
                reason='Upstream error',
                status_code=int(getattr(error, 'status_code', 0) or 0),
            )
        else:
            self._retry_notice(
                slot,
                attempt=self.state._429_count,
                reason='Rate limited (429)',
                status_code=429,
            )
        self._restart('rate limited (429) — rotating slot')
        if _retry_delay_s > 0:
            return _retry_delay_s
        return 0.0

    def _permission(self, error, slot, tag):
        """Refresh OAuth once per request; 401 excludes the key, 403 only its pair."""
        if slot.oauth and (not self._oauth_refresh_used):
            self._oauth_refresh_used = True
            if _force_oauth_token_refresh(slot.oauth, self.log_prefix, self.owner_user_id):
                slot.release()
                logger.warning(
                    '%s Auth rejected on OAuth slot %s:%s (%s) — token refreshed, retrying once',
                    self.log_prefix,
                    slot.key_name,
                    slot.model,
                    slot.oauth,
                )
                self._restart('oauth token refreshed — retrying')
                return 0.0
            logger.warning(
                '%s Auth rejected on OAuth slot %s:%s (%s) — forced refresh failed; normal failover',
                self.log_prefix,
                slot.key_name,
                slot.model,
                slot.oauth,
            )
        slot.record_error(is_rate_limit=False, error=error)
        self._remember_failure(error)
        if slot.oauth and getattr(error, 'status_code', 0) == 401:
            self.state.note_permission_key(slot)
            logger.warning(
                '%s OAuth credential rejected on %s — excluding whole key',
                self.log_prefix,
                slot.key_name,
            )
        elif not self.state.note_permission_pair(
            slot, self.dispatcher, self.capability, self.log_prefix
        ):
            logger.warning(
                '%s Permission denied on %s:%s — excluding pair, remaining slots: %s',
                self.log_prefix,
                slot.key_name,
                slot.model,
                self.dispatcher.summarize_slots(self.capability),
            )
        self._restart('permission denied — rotating slot')
        return 0.0

    def _endpoint_unreachable(self, error, slot, tag):
        """Hold the unreachable network endpoint; its slots are not at fault.

        Only a caller with an explicit upstream budget spends a hard attempt;
        otherwise the outage is waitable like a gateway 5xx storm.
        """
        slot.release()
        self._remember_failure(error)
        outage = self.state.endpoint_outage
        probe_in_s = outage.absorb(slot)
        if self.max_429_attempts is None:
            self.state.note_endpoint_outage()
            attempt = outage.observations
        else:
            self.state.hard_attempts += 1
            attempt = self.state.hard_attempts
        log_endpoint_outage(
            self.log_prefix, slot, error,
            observations=outage.observations, probe_in_s=probe_in_s)
        self._retry_notice(
            slot,
            attempt=attempt,
            reason='Endpoint unreachable',
            status_code=0,
        )
        self._restart('endpoint unreachable — holding network endpoint')
        return 0.0

    def _aborted(self, error, slot, tag):
        slot.release()
        logger.debug('%s User aborted — stopping dispatch immediately', tag)
        raise error

    def _content_filter(self, error, slot, tag):
        slot.release()
        logger.warning('%s Content filter (HTTP 450) — not retrying', tag, exc_info=True)
        raise error

    def _prompt_too_long(self, error, slot, tag):
        slot.release()
        logger.warning(
            '%s Prompt/request too large — not retrying on other slots (same payload = same rejection)',
            tag,
        )
        raise error

    def _invalid_image(self, error, slot, tag):
        slot.release()
        logger.warning(
            '%s Image content error — not retrying on other slots (same image = same rejection)',
            tag,
        )
        raise error

    def _stream_only(self, error, slot, tag):
        return self._unexpected(error, slot, tag)

    def _transport(self, error, slot, tag):
        """Only response-read failure is pair evidence; upload/ambiguous delivery surfaces."""
        if error.failure_stage != 'read':
            slot.release()
            logger.warning(
                '%s Transport failure (%s, stage=%s) on %s:%s — surfacing to caller: %.300s',
                self.log_prefix,
                type(error).__name__,
                error.failure_stage,
                slot.key_name,
                slot.model,
                str(error),
            )
            raise error
        slot.record_error(is_rate_limit=False)
        self._remember_failure(error)
        self.state.note_generic_error(slot, is_timeout=True, strict_model=self.strict_model)
        self._retry_notice(
            slot,
            attempt=self.state.hard_attempts,
            reason='Request timed out',
            status_code=0,
        )
        logger.warning(
            '%s Transport timeout on %s:%s (stage=%s) — excluded pair, rotating: %.200s',
            self.log_prefix,
            slot.key_name,
            slot.model,
            error.failure_stage,
            str(error),
        )
        self._restart('transport timeout — rotating slot')
        return 0.0

    def _request_scoped(self, error, slot, tag):
        slot.release()
        logger.warning(
            '%s Request-scoped failure (%s, stage=%s, HTTP=%s) on %s:%s — surfacing to caller: %.300s',
            self.log_prefix,
            type(error).__name__,
            getattr(error, 'failure_stage', 'provider_response'),
            getattr(error, 'status_code', 0) or 'none',
            slot.key_name,
            slot.model,
            str(error),
        )
        raise error

    def _model_missing(self, error, slot, tag):
        """Persist model-route evidence without treating it as credential health."""
        slot.release()
        self.state.note_route_missing_model(slot.model, error)
        self._note_failure_cause(error)
        _record_route_missing_model(self.dispatcher, slot, error)
        # Only the protocol-unsupported verdict records its 404; the 400 one is 0.
        status_code = error.status_code or 400
        logger.warning(
            '%s Model %s has no route on this gateway (HTTP %d) — excluded model, trying next: %.300s',
            self.log_prefix,
            slot.model,
            status_code,
            str(error),
        )
        self._retry_notice(
            slot,
            attempt=self.state.hard_attempts,
            reason='Upstream error',
            status_code=status_code,
        )
        self._restart('model route missing — rotating slot')
        return 0.0

    def _bad_request(self, error, slot, tag):
        slot.release()
        self._remember_failure(error)
        if self.state.first_bad_request_err is None:
            self.state.first_bad_request_err = error
        self.state.note_bad_request_route(slot, self.dispatcher)
        logger.warning(
            '%s Bad request (HTTP 400, deterministic) on %s:%s — excluded route without credential retry: %.500s',
            self.log_prefix,
            slot.key_name,
            slot.model,
            str(error),
        )
        self._restart('bad request — different route only')
        return 0.0

    def _unexpected(self, error, slot, tag):
        if is_subscription_quota_error(str(error)):
            _ra = parse_subscription_retry_after(str(error))
            slot.record_error(is_rate_limit=True, cooldown_s=_ra, error=str(error)[:200])
            self._remember_failure(error)
            self.state.note_subscription_quota()
            _raise_if_429_attempt_budget_exhausted(
                max_429_attempts=self.max_429_attempts,
                upstream_attempts=self.state._429_count,
                last_error=error,
            )
            logger.warning(
                '%s Subscription quota/capacity signal on %s:%s (cooldown %ss) — rotating slot: %s',
                self.log_prefix,
                slot.key_name,
                slot.model,
                f'{_ra:.0f}' if _ra else '0.5',
                str(error)[:160],
            )
            self._retry_notice(
                slot,
                attempt=self.state._429_count,
                reason='Subscription quota reached',
                status_code=429,
            )
            self._restart('subscription quota reached — rotating slot')
            return 0.3
        slot.record_error(is_rate_limit=False)
        self._remember_failure(error)
        _is_timeout = 'timed out' in str(error).lower() or 'timeout' in type(error).__name__.lower()
        _status = getattr(error, 'status_code', 0) or 0
        _reason = str(error)[:120]
        if _is_timeout:
            _reason = 'Request timed out'
        elif _status:
            _reason = f'HTTP {_status}'
        self._retry_notice(
            slot,
            attempt=self.state.hard_attempts + 1,
            reason=_reason,
            status_code=_status,
        )
        self.state.note_generic_error(slot, is_timeout=_is_timeout, strict_model=self.strict_model)
        self._restart('stream failure — rotating slot')
        logger.debug(
            '%s Stream error on %s:%s: %s — next slot',
            tag,
            slot.key_name,
            slot.model,
            str(error)[:200],
            exc_info=True,
        )
        return 0.0

    def _cancelled(self, error, slot, tag):
        """Explicit cancellation releases a reservation without a health failure."""
        slot.release()
        raise error


class ChatFailurePolicy(DispatchFailurePolicy):
    """Whole-response delivery stays request-scoped; stream-only models retire."""

    def _transport(self, error, slot, tag):
        return self._request_scoped(error, slot, tag)

    def _stream_only(self, error, slot, tag):
        slot.stream_only = True
        slot.record_error(is_rate_limit=False)
        self._remember_failure(error)
        self.state.note_stream_only(slot)
        logger.warning(
            '%s Model %s only supports streaming — excluding '
            'from non-streaming dispatch, trying next model',
            self.log_prefix, slot.model,
        )
        return 0.0
