"""Slot hygiene: premature-close cooling and audited severity downgrades."""

from lib.log import get_logger

logger = get_logger('lib.llm_dispatch.api')


# Ordinary per-key 429 contention intentionally remains waitable forever.
# A credential-delivery contradiction is different: stop after four actual
# contradictory responses, regardless of pool shape. This prevents a
# persistent proxy/header-routing fault from becoming another infinite wait.
_CREDENTIAL_DELIVERY_ANOMALY_MAX_ATTEMPTS = 4


def _advance_credential_delivery_anomaly(error, attempts: int) -> tuple[int, bool]:
    """Advance the bounded counter for a credential-delivery contradiction.

    Returns ``(new_count, exhausted)``. Non-matching errors leave the counter
    unchanged. The exception is annotated only with numeric diagnostics; no
    credential material is ever retained.
    """
    if not bool(getattr(error, 'is_credential_delivery_anomaly', False)):
        return attempts, False
    attempts = max(0, int(attempts or 0)) + 1
    error.credential_delivery_anomaly_attempts = attempts
    error.credential_delivery_anomaly_limit = (
        _CREDENTIAL_DELIVERY_ANOMALY_MAX_ATTEMPTS)
    return (
        attempts,
        attempts >= _CREDENTIAL_DELIVERY_ANOMALY_MAX_ATTEMPTS,
    )


def _cool_slot_on_premature_close(slot, usage, *, stream_state=''):
    """Settle an unusable provider stream once as a slot soft failure.

    ``lib/llm/_sse_core.py::SSEAccumulator.finalize`` retains
    ``usage['_missing_done']`` as a compatibility diagnostic, but a provider
    finish frame can prove completion without the optional trailing ``[DONE]``.
    The closed ``_stream_state`` therefore decides health. Only an unusable
    state (or a legacy missing-DONE tuple with no closed state) enters the same
    ``record_truncation`` path used by translation retries; after three
    consecutive soft failures, the existing exponential-backoff/300s cap cools
    the slot.

    The reservation is released by ``record_truncation`` itself. No provisional
    success is recorded and no consecutive-error rollback is needed.

    The over-cooling guard is the parser's closed state (plus legacy
    ``_missing_done``): verified completion and client abort are never cooled.
    """
    if not isinstance(usage, dict) and not stream_state:
        return
    usage = usage if isinstance(usage, dict) else {}
    stream_state = str(stream_state or usage.get('_stream_state') or '')
    unusable_states = {
        'premature_close', 'malformed_stream',
        'semantic_progress_timeout', 'no_actionable_output',
        'empty_response', 'tool_payload_missing', 'unknown',
    }
    if stream_state:
        # Closed typed state is authoritative. A provider may omit ``[DONE]``
        # after an otherwise authoritative finish frame; that is verified
        # completion and must not be cooled merely because the compatibility
        # diagnostic still records ``_missing_done``.
        if stream_state not in unusable_states:
            return
    elif not usage.get('_missing_done'):
        # Historical tuple/plugin seam: no closed state exists, so retain the
        # conservative missing-DONE health signal until that producer migrates.
        return
    try:
        _elapsed = usage.get('stream_elapsed_ms', 0)
        _chunks = usage.get('_chunks_received', 0)
        slot.record_truncation(
            error='unusable provider stream state=%s '
                  'elapsed_ms=%s chunks=%s' % (
                      stream_state or 'legacy_missing_done',
                      _elapsed,
                      _chunks,
                  ),
            release_reservation=True,
        )
        from lib.log import audit_log
        audit_log('premature_close_cooldown',
                  key_name=slot.key_name, model=slot.model,
                  provider_id=slot.provider_id,
                  consecutive_errors=slot.consecutive_errors,
                  cooldown_until=round(slot.cooldown_until, 1),
                  elapsed_ms=_elapsed, chunks=_chunks,
                  stream_state=stream_state or 'legacy_missing_done',
                  trace_id=usage.get('trace_id', ''))
        logger.warning('[Dispatch] Unusable provider stream on %s:%s '
                       '(state=%s) — '
                       'recorded soft failure (consecutive_errors=%d)',
                       slot.key_name, slot.model,
                       stream_state or 'legacy_missing_done',
                       slot.consecutive_errors)
    except Exception as e:
        logger.warning('[Dispatch] _cool_slot_on_premature_close failed '
                       'for %s:%s: %s', slot.key_name, slot.model, e)


_AUDITED_SEVERITY_DOWNGRADE = False


def _audit_severity_downgrade() -> None:
    """Emit a one-time audit_log entry for the 2026-05-05 per-cycle 429
    severity downgrade (WARNING→INFO). Only logged once per process.

    This keeps CLAUDE.md §10 ("config changes must leave a trace") happy
    without flooding audit.log with the same entry on every dispatch.
    """
    global _AUDITED_SEVERITY_DOWNGRADE
    if _AUDITED_SEVERITY_DOWNGRADE:
        return
    _AUDITED_SEVERITY_DOWNGRADE = True
    try:
        from lib.log import audit_log
        audit_log('config_change',
                  param='dispatch_429_severity',
                  old='warning',
                  new='info',
                  reason='log-noise audit 2026-05-05 — per-cycle 429 is '
                         'routine backpressure, rotation handled by '
                         'dispatcher; only final key exhaustion is warning',
                  approved_by='plan')
    except Exception as e:
        logger.debug('[Dispatch] audit_log unavailable: %s', e)
